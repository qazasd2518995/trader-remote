"""
Browser-based one-click launcher.

The app starts a localhost control panel in the user's browser. This avoids
requiring Tk/PySide on member machines while still giving non-technical users a
Start/Stop interface.
"""
from __future__ import annotations

import json
import logging
import queue
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from copy_trader.config import DATA_DIR, load_config

logger = logging.getLogger(__name__)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "啟用"}


def _infer_role(default_role: Optional[str] = None) -> str:
    if default_role in {"central", "client"}:
        return default_role
    if "--role" in sys.argv:
        try:
            role = sys.argv[sys.argv.index("--role") + 1].strip().lower()
            if role in {"central", "client"}:
                return role
        except Exception:
            pass
    name = Path(sys.argv[0]).stem.lower()
    if any(token in name for token in ("central", "signal", "hub", "訊號")):
        return "central"
    return "client"


def _lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


# fly.io 邊緣（與多數 HTTP 反向代理）對閒置連線約 ~5 分鐘會強制 RST，
# Windows 上會冒出 WinError 10054 / ConnectionResetError；下一輪 polling
# 會立即重連成功，不是真正的服務異常。把這類瞬斷壓低成 debug 噪音。
def _is_transient_disconnect(exc: BaseException) -> bool:
    inner = getattr(exc, "reason", exc)
    if isinstance(inner, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
        return True
    msg = str(exc)
    return any(token in msg for token in ("10054", "10053", "Connection reset", "EOF occurred"))


class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: "queue.Queue[str]"):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:
            pass


class LauncherState:
    def __init__(self, role: str):
        self.role = role
        self.title = "黃金訊號中心" if role == "central" else "黃金跟單會員端"
        self.settings_path = DATA_DIR / f"{role}_web_launcher_settings.json"
        self.settings = self._load_settings()
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.logs = []
        self.lock = threading.Lock()
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.client_agent = None
        self.collector = None
        self.status = "尚未啟動"
        self.service_started_at: Optional[float] = None
        self.should_exit = False
        self.control_server: Optional[ThreadingHTTPServer] = None

    def defaults(self) -> Dict[str, Any]:
        if self.role == "central":
            return {
                "external_hub_url": "",
                "token": secrets.token_urlsafe(24),
                "copy_mode": "all",
                "interval": "1.0",
            }
        return {
            "hub_url": "http://中央電腦IP:8765",
            "token": "",
            "mt5_files_dir": "",
            "interval": "1.0",
            # 下單參數（覆寫 config.py 預設）
            "default_lot_size": "0.01",
            "use_martingale": "true",
            "martingale_multiplier": "2.0",
            "martingale_max_level": "4",
            "martingale_lots": "",
            "partial_close_ratios": "0.5,0.3,0.2",
            "cancel_pending_after_seconds": "7200",
            "cancel_if_price_beyond_percent": "1.0",
            "signal_dedup_minutes": "10",
            "max_open_positions": "10",
        }

    def _load_settings(self) -> Dict[str, Any]:
        data = self.defaults()
        try:
            if self.settings_path.exists():
                with self.settings_path.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data.update(loaded)
        except Exception:
            pass
        return data

    def save_settings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        merged = self.defaults()
        merged.update({k: v for k, v in data.items() if k in merged})
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings_path.open("w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        self.settings = merged
        logger.info("設定已儲存：%s", self.settings_path)
        return merged

    def is_running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def start_service(self) -> None:
        if self.is_running():
            return
        self.stop_event.clear()
        target = self._run_central if self.role == "central" else self._run_client
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()
        self.status = "啟動中"

    def stop_service(self) -> None:
        self.stop_event.set()
        self.status = "停止中"

    def _run_central(self) -> None:
        try:
            from copy_trader.central.signal_collector import CentralSignalCollector, HubPublisher

            external_hub_url = str(self.settings.get("external_hub_url") or "").strip().rstrip("/")
            if not external_hub_url:
                logger.error("未設定外部 Hub URL；本程式為雲端模式，請填入雲端 Hub 網址後再開始")
                self.status = "啟動失敗"
                return

            token = str(self.settings.get("token") or "")
            interval = max(0.2, float(self.settings.get("interval") or 1.0))
            copy_mode = str(self.settings.get("copy_mode") or "all")

            logger.info("雲端 Hub 模式：訊號將推送到 %s", external_hub_url)
            publisher = HubPublisher(external_hub_url, token)

            collector = CentralSignalCollector(load_config(), publisher, copy_mode=copy_mode)
            self.collector = collector
            self.status = "運行中"
            self.service_started_at = time.time()

            consecutive_errors = 0
            transient_quiet_threshold = 5

            while not self.stop_event.is_set():
                try:
                    published = collector.run_cycle()
                    if published:
                        logger.info("本輪發布 %s 筆訊號", published)
                    if consecutive_errors:
                        if consecutive_errors > transient_quiet_threshold:
                            logger.info("雲端 Hub 連線恢復（之前連續斷線 %s 次）", consecutive_errors)
                        consecutive_errors = 0
                except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
                    consecutive_errors += 1
                    if _is_transient_disconnect(exc) and consecutive_errors <= transient_quiet_threshold:
                        logger.debug("發布到雲端 Hub 短暫斷線（第 %s 次），自動重試中：%s", consecutive_errors, exc)
                    else:
                        logger.warning("發布到雲端 Hub 失敗（連續 %s 次）：%s", consecutive_errors, exc)
                except Exception as exc:
                    consecutive_errors += 1
                    logger.exception("中央擷取錯誤：%s", exc)
                self.stop_event.wait(interval)
        except Exception as exc:
            logger.exception("中央訊號中心啟動失敗：%s", exc)
            self.status = "啟動失敗"
        finally:
            self.collector = None
            self.status = "已停止"
            self.service_started_at = None

    def _run_client(self) -> None:
        try:
            from copy_trader.central.mt5_client_agent import HubClient, MT5ClientAgent

            hub_url = str(self.settings.get("hub_url") or "").rstrip("/")
            token = str(self.settings.get("token") or "")
            mt5_dir = str(self.settings.get("mt5_files_dir") or "")
            interval = max(0.5, float(self.settings.get("interval") or 1.0))

            self.client_agent = MT5ClientAgent(
                HubClient(hub_url, token),
                DATA_DIR / "central_client_state.json",
                mt5_files_dir=mt5_dir,
                replay=False,
                overrides=self.settings,
            )
            self.client_agent.trade_manager.start()
            logger.info("會員端已啟動，Hub=%s，last_seq=%s", hub_url, self.client_agent.last_seq)
            self.status = "運行中"
            self.service_started_at = time.time()

            consecutive_errors = 0
            transient_quiet_threshold = 5

            while not self.stop_event.is_set():
                try:
                    count = self.client_agent.run_cycle()
                    if count:
                        logger.info("本輪送出 %s 筆 MT5 指令", count)
                    if consecutive_errors:
                        if consecutive_errors > transient_quiet_threshold:
                            logger.info("Hub 連線恢復（之前連續斷線 %s 次）", consecutive_errors)
                        consecutive_errors = 0
                except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
                    consecutive_errors += 1
                    if _is_transient_disconnect(exc) and consecutive_errors <= transient_quiet_threshold:
                        logger.debug("Hub 短暫斷線（第 %s 次），自動重連中：%s", consecutive_errors, exc)
                    else:
                        logger.warning("Hub 連線失敗（連續 %s 次）：%s", consecutive_errors, exc)
                except Exception as exc:
                    consecutive_errors += 1
                    logger.exception("會員端執行錯誤：%s", exc)
                self.stop_event.wait(interval)
        except Exception as exc:
            logger.exception("會員端啟動失敗：%s", exc)
            self.status = "啟動失敗"
        finally:
            if self.client_agent is not None:
                try:
                    self.client_agent.trade_manager.stop()
                except Exception:
                    pass
                self.client_agent = None
            self.status = "已停止"
            self.service_started_at = None

    def drain_logs(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.logs.append(line)
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]

    def snapshot(self) -> Dict[str, Any]:
        self.drain_logs()
        snap = {
            "role": self.role,
            "title": self.title,
            "settings": self.settings,
            "status": self.status,
            "running": self.is_running(),
            "logs": self.logs[-200:],
            "lan_ip": _lan_ip(),
            "uptime_seconds": int(time.time() - self.service_started_at) if self.service_started_at else 0,
        }
        if self.role == "central" and self.collector is not None:
            snap["latest_captures"] = list(self.collector.latest_captures)
        if self.role == "client" and self.client_agent is not None:
            snap["recent_events"] = list(self.client_agent.recent_events)
            snap["last_seq"] = self.client_agent.last_seq
            snap["orders"] = _orders_for_ui(self.client_agent.trade_manager)
            snap["martingale_level"] = self.client_agent.trade_manager.current_martingale_level
            snap["martingale_enabled"] = bool(self.client_agent.config.use_martingale)
        return snap


def _orders_for_ui(trade_manager) -> list:
    """把 trade_manager 的訂單轉成簡單 dict 給前端渲染。"""
    out = []
    try:
        orders = trade_manager.get_all_orders()
    except Exception:
        return out
    for o in orders[-20:]:  # 最近 20 筆就夠
        sig = o.signal
        try:
            tps = ", ".join(f"{float(tp):.2f}" for tp in (sig.take_profit or []) if tp is not None)
        except Exception:
            tps = ""
        out.append({
            "signal_id": getattr(o, "signal_id", ""),
            "status": getattr(o.status, "value", str(o.status)),
            "ticket": getattr(o, "ticket", None),
            "direction": (sig.direction or "").upper(),
            "entry": (float(sig.entry_price) if sig.entry_price is not None else None) if not getattr(sig, "is_market_order", False) else "市價",
            "sl": float(sig.stop_loss) if sig.stop_loss is not None else None,
            "tps": tps,
            "lot": getattr(o, "remaining_volume", 0.0),
            "source": getattr(o, "source_window", ""),
            "entry_time": getattr(o, "entry_time", None),
        })
    return out


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else {}


def make_handler(state: LauncherState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug(fmt, *args)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                _html_response(self, _page_html(state))
                return
            if parsed.path == "/api/status":
                _json_response(self, 200, {"ok": True, **state.snapshot()})
                return
            _json_response(self, 404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            try:
                if parsed.path == "/api/settings":
                    settings = state.save_settings(_read_json(self))
                    _json_response(self, 200, {"ok": True, "settings": settings})
                    return
                if parsed.path == "/api/start":
                    data = _read_json(self)
                    if data:
                        state.save_settings(data)
                    state.start_service()
                    _json_response(self, 200, {"ok": True})
                    return
                if parsed.path == "/api/stop":
                    state.stop_service()
                    _json_response(self, 200, {"ok": True})
                    return
                if parsed.path == "/api/test-hub":
                    from copy_trader.central.mt5_client_agent import HubClient

                    settings = state.save_settings(_read_json(self))
                    health = HubClient(str(settings.get("hub_url") or ""), str(settings.get("token") or "")).health()
                    _json_response(self, 200, {"ok": True, "health": health})
                    return
                if parsed.path == "/api/quit":
                    state.stop_service()
                    state.should_exit = True
                    _json_response(self, 200, {"ok": True})
                    threading.Thread(target=state.control_server.shutdown, daemon=True).start()
                    return
            except Exception as exc:
                logger.exception("request failed: %s", exc)
                _json_response(self, 500, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 404, {"ok": False, "error": "not_found"})

    return Handler


def _page_html(state: LauncherState) -> str:
    is_central = state.role == "central"
    if is_central:
        fields = """
      <label>雲端 Hub URL<input id="external_hub_url" placeholder="例如 https://gold-signal-hub-tw.fly.dev" /></label>
      <label>Hub 密碼<input id="token" type="password" /></label>
      <label>複製模式<select id="copy_mode"><option value="all">全選複製</option><option value="tail">底部幾屏</option></select></label>
      <label>輪詢秒數<input id="interval" /></label>
    """
        extra_button = ""
    else:
        fields = """
      <h3 class="grouph">連線</h3>
      <label>中央 Hub URL<input id="hub_url" placeholder="http://中央電腦IP:8765" /></label>
      <label>Hub 密碼<input id="token" type="password" /></label>
      <label>MT5 Files 路徑<input id="mt5_files_dir" placeholder="可留空自動偵測" /></label>
      <label>輪詢秒數<input id="interval" /></label>

      <h3 class="grouph">下單</h3>
      <label>預設手數<input id="default_lot_size" placeholder="0.01" /></label>
      <label>啟用馬丁<input id="use_martingale" type="checkbox" /></label>
      <label>馬丁倍數<input id="martingale_multiplier" placeholder="2.0" /></label>
      <label>馬丁最大層級<input id="martingale_max_level" placeholder="4" /></label>
      <label>自訂每層手數<input id="martingale_lots" placeholder="例 0.01,0.02,0.04,0.08（留空 = 用倍數公式）" /></label>
      <label>分批 TP 比例<input id="partial_close_ratios" placeholder="0.5,0.3,0.2" /></label>

      <h3 class="grouph">風控</h3>
      <label>掛單取消秒數<input id="cancel_pending_after_seconds" placeholder="7200" /></label>
      <label>取消偏離 %<input id="cancel_if_price_beyond_percent" placeholder="1.0" /></label>
      <label>訊號 dedup 分鐘<input id="signal_dedup_minutes" placeholder="10" /></label>
      <label>最大同時持倉<input id="max_open_positions" placeholder="10" /></label>
    """
        extra_button = "<button id=\"testHub\">測試 Hub</button>"

    sections_html = f"""
      <section><h2>設定</h2>{fields}<p class="muted" id="hint"></p></section>
      <section><button class="primary" id="start">開始</button><button id="stop">停止</button>{extra_button}<button class="danger" id="quit">關閉程式</button></section>
      <section><h2>狀態紀錄</h2><div id="logs"></div></section>"""

    if is_central:
        right_panel = """
    <aside class="col-right">
      <section>
        <h2>複製內容預覽</h2>
        <p class="muted" id="capturesHint">服務啟動後每次複製到的訊息會出現在這裡（最多保留 8 筆）</p>
        <div id="captures"></div>
      </section>
    </aside>"""
    else:
        right_panel = """
    <aside class="col-right">
      <section>
        <h2>馬丁狀態</h2>
        <p id="martingaleStatus" class="muted">尚未啟動</p>
      </section>
      <section>
        <h2>最近事件</h2>
        <p class="muted">收到 Hub 訊號 / 跳過 / 送 MT5 / 失敗（最多保留 30 筆）</p>
        <div id="events"></div>
      </section>
      <section>
        <h2>MT5 訂單狀態</h2>
        <p class="muted">最近 20 筆，含掛單 / 已成交 / 已平倉 / 已取消</p>
        <div id="orders"></div>
      </section>
    </aside>"""

    body_main = f"""
  <main class="split">
    <div class="col-left">{sections_html}
    </div>{right_panel}
  </main>"""

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{state.title}</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f5f7f8; color: #17202a; }}
    header {{ padding: 18px 24px; background: #fff; border-bottom: 1px solid #dfe4e8; display: flex; justify-content: space-between; align-items: center; }}
    h1 {{ margin: 0; font-size: 21px; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 22px; }}
    main.split {{ max-width: 1400px; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; align-items: start; }}
    @media (max-width: 1100px) {{ main.split {{ grid-template-columns: 1fr; }} }}
    .col-left, .col-right {{ min-width: 0; }}
    section {{ background: #fff; border: 1px solid #dfe4e8; border-radius: 8px; padding: 18px; margin-bottom: 14px; }}
    label {{ display: grid; grid-template-columns: 160px 1fr; align-items: center; gap: 12px; margin: 10px 0; color: #52616f; }}
    input, select {{ font: inherit; padding: 9px 10px; border: 1px solid #cad2d8; border-radius: 6px; }}
    button {{ font: inherit; padding: 9px 14px; border: 1px solid #9aa7b2; border-radius: 6px; background: #fff; cursor: pointer; margin-right: 8px; }}
    button.primary {{ background: #1450a3; color: #fff; border-color: #1450a3; }}
    button.danger {{ color: #a12a2a; border-color: #d7a4a4; }}
    #logs {{ height: 240px; overflow: auto; white-space: pre-wrap; background: #111820; color: #d7e3ee; padding: 12px; border-radius: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }}
    .muted {{ color: #6b7785; }}
    .capture-card {{ background: #fbfcfd; border: 1px solid #e2e6ea; border-radius: 6px; padding: 10px 12px; margin-bottom: 10px; }}
    .capture-meta {{ color: #6b7785; font-size: 12px; margin-bottom: 6px; display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; }}
    .capture-source {{ font-weight: 600; color: #1450a3; }}
    .capture-text {{ white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; max-height: 220px; overflow: auto; background: #f3f5f7; padding: 8px; border-radius: 4px; color: #1f2933; }}
    .capture-msgs {{ margin-top: 8px; padding-top: 8px; border-top: 1px dashed #e2e6ea; }}
    .capture-msg {{ background: #eef5ff; padding: 6px 8px; border-radius: 4px; margin: 4px 0; font-size: 12px; white-space: pre-wrap; }}
    .capture-msg-meta {{ color: #6b7785; font-size: 11px; margin-bottom: 2px; }}
    .capture-empty {{ color: #6b7785; font-size: 12px; padding: 20px; text-align: center; }}
    h3.grouph {{ margin: 18px 0 6px 0; padding: 0; font-size: 13px; color: #1450a3; border-bottom: 1px solid #e2e6ea; padding-bottom: 4px; }}
    h3.grouph:first-child {{ margin-top: 0; }}
    .event-card {{ display: grid; grid-template-columns: 70px 90px 1fr; gap: 8px; padding: 6px 10px; border-bottom: 1px solid #f0f3f5; font-size: 12px; align-items: baseline; }}
    .event-card:last-child {{ border-bottom: none; }}
    .event-time {{ color: #8a96a4; font-family: ui-monospace, monospace; }}
    .event-kind {{ font-weight: 600; }}
    .event-kind.signal_received {{ color: #1450a3; }}
    .event-kind.submitted {{ color: #2d8659; }}
    .event-kind.skipped_dedup {{ color: #b08400; }}
    .event-kind.skipped_invalid {{ color: #b08400; }}
    .event-kind.submit_failed {{ color: #a12a2a; }}
    .event-summary {{ color: #1f2933; word-break: break-word; }}
    .event-extra {{ color: #6b7785; font-size: 11px; }}
    .order-card {{ display: grid; grid-template-columns: 70px 70px 1fr; gap: 8px; padding: 6px 10px; border-bottom: 1px solid #f0f3f5; font-size: 12px; align-items: baseline; }}
    .order-card:last-child {{ border-bottom: none; }}
    .order-status {{ font-weight: 600; padding: 2px 6px; border-radius: 4px; font-size: 11px; text-align: center; }}
    .order-status.pending {{ background: #fff3cd; color: #856404; }}
    .order-status.sent {{ background: #cce5ff; color: #004085; }}
    .order-status.filled {{ background: #d4edda; color: #155724; }}
    .order-status.partial {{ background: #d4edda; color: #155724; }}
    .order-status.closed {{ background: #e2e3e5; color: #383d41; }}
    .order-status.cancelled {{ background: #e2e3e5; color: #6c757d; }}
    .order-status.failed {{ background: #f8d7da; color: #721c24; }}
    .empty-block {{ color: #6b7785; font-size: 12px; padding: 16px; text-align: center; }}
  </style>
</head>
<body>
  <header><h1>{state.title}</h1><strong id="status">載入中</strong></header>
{body_main}
  <script>
    const role = {json.dumps(state.role)};
    let snapshot = null;
    let didFill = false;
    function ids() {{
      if (role === "central") return ["external_hub_url","token","copy_mode","interval"];
      return ["hub_url","token","mt5_files_dir","interval",
              "default_lot_size","use_martingale","martingale_multiplier","martingale_max_level",
              "martingale_lots","partial_close_ratios",
              "cancel_pending_after_seconds","cancel_if_price_beyond_percent",
              "signal_dedup_minutes","max_open_positions"];
    }}
    function collect() {{
      const out = {{}};
      for (const id of ids()) {{
        const el = document.getElementById(id);
        out[id] = el.type === "checkbox" ? (el.checked ? "true" : "false") : el.value;
      }}
      return out;
    }}
    function fill(settings) {{
      for (const id of ids()) if (document.getElementById(id)) {{
        const el = document.getElementById(id);
        if (el.type === "checkbox") el.checked = ["true", "1", "yes", "on"].includes(String(settings[id] || "").toLowerCase());
        else el.value = settings[id] || "";
      }}
    }}
    function escapeHtml(s) {{
      return String(s ?? "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}})[c]);
    }}
    function renderCaptures(captures) {{
      const root = document.getElementById("captures");
      if (!root) return;
      if (!captures || captures.length === 0) {{
        root.innerHTML = '<p class="capture-empty">尚未有複製內容</p>';
        return;
      }}
      const newest = captures.slice().reverse();
      root.innerHTML = newest.map(c => {{
        const t = new Date((c.captured_at || 0) * 1000);
        const stamp = t.toLocaleTimeString("zh-TW", {{ hour12: false }});
        const status = c.ok ? `新訊息 ${{c.new_count || 0}} 則` : `失敗：${{escapeHtml(c.error || "")}}`;
        const msgs = (c.new_messages || []).map(m =>
          `<div class="capture-msg"><div class="capture-msg-meta">${{escapeHtml(m.time_str || "")}} ${{escapeHtml(m.sender || "")}}</div>${{escapeHtml(m.body || "")}}</div>`
        ).join("");
        const msgsBlock = msgs ? `<div class="capture-msgs">${{msgs}}</div>` : "";
        return `<div class="capture-card">
          <div class="capture-meta"><span class="capture-source">${{escapeHtml(c.source || "")}}</span><span>${{stamp}} · ${{status}}</span></div>
          <div class="capture-text">${{escapeHtml(c.raw_text || "")}}</div>
          ${{msgsBlock}}
        </div>`;
      }}).join("");
    }}
    async function post(path, data={{}}) {{
      const res = await fetch(path, {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: JSON.stringify(data) }});
      const json = await res.json();
      if (!json.ok) throw new Error(json.error || "request failed");
      return json;
    }}
    async function refresh() {{
      const res = await fetch("/api/status");
      snapshot = await res.json();
      if (!snapshot.ok) return;
      if (!didFill) {{
        fill(snapshot.settings);
        didFill = true;
      }}
      document.getElementById("status").textContent = snapshot.status + (snapshot.running ? ` (${{snapshot.uptime_seconds}}s)` : "");
      document.getElementById("logs").textContent = (snapshot.logs || []).join("\\n");
      document.getElementById("logs").scrollTop = document.getElementById("logs").scrollHeight;
      if (role === "central") {{
        const url = snapshot.settings.external_hub_url || "";
        document.getElementById("hint").textContent = url
          ? `會員端 Hub URL 填：${{url}}`
          : "請填入雲端 Hub URL 後再按開始";
        renderCaptures(snapshot.latest_captures);
      }} else {{
        renderClientPanels(snapshot);
      }}
    }}
    function fmtTime(ts) {{
      if (!ts) return "";
      return new Date(ts * 1000).toLocaleTimeString("zh-TW", {{ hour12: false }});
    }}
    const KIND_LABEL = {{
      signal_received: "收到訊號",
      submitted: "送 MT5",
      skipped_dedup: "跳過（重複）",
      skipped_invalid: "跳過（不完整）",
      submit_failed: "MT5 失敗",
    }};
    function renderClientPanels(snap) {{
      // 馬丁狀態
      const ms = document.getElementById("martingaleStatus");
      if (ms) {{
        if (snap.martingale_enabled) {{
          ms.textContent = `啟用中 — 目前層級 ${{snap.martingale_level || 0}}（last_seq=${{snap.last_seq || 0}}）`;
          ms.classList.remove("muted");
        }} else {{
          ms.textContent = `關閉（均注模式）— last_seq=${{snap.last_seq || 0}}`;
          ms.classList.add("muted");
        }}
      }}
      // 事件
      const evRoot = document.getElementById("events");
      if (evRoot) {{
        const events = (snap.recent_events || []).slice().reverse();
        if (events.length === 0) {{
          evRoot.innerHTML = '<p class="empty-block">尚未有事件</p>';
        }} else {{
          evRoot.innerHTML = events.map(e => {{
            const label = KIND_LABEL[e.kind] || e.kind;
            const extra = e.kind === "submitted" ? `<div class="event-extra">手數 ${{e.lot_size || ""}} · 馬丁 L${{e.martingale_level || 0}} · seq=${{e.seq || 0}}</div>` :
                          e.seq != null ? `<div class="event-extra">seq=${{e.seq}}</div>` : "";
            return `<div class="event-card">
              <span class="event-time">${{fmtTime(e.time)}}</span>
              <span class="event-kind ${{e.kind}}">${{escapeHtml(label)}}</span>
              <div class="event-summary">${{escapeHtml(e.summary || "")}}<div class="event-extra">${{escapeHtml(e.source || "")}}</div>${{extra}}</div>
            </div>`;
          }}).join("");
        }}
      }}
      // 訂單
      const orRoot = document.getElementById("orders");
      if (orRoot) {{
        const orders = (snap.orders || []).slice().reverse();
        if (orders.length === 0) {{
          orRoot.innerHTML = '<p class="empty-block">尚未有訂單</p>';
        }} else {{
          orRoot.innerHTML = orders.map(o => {{
            const t = o.entry_time ? fmtTime(o.entry_time) : "—";
            const entry = (o.entry === null || o.entry === undefined) ? "-" : (typeof o.entry === "number" ? o.entry.toFixed(2) : o.entry);
            const sl = (o.sl === null || o.sl === undefined) ? "-" : Number(o.sl).toFixed(2);
            const ticket = o.ticket ? `#${{o.ticket}}` : "—";
            return `<div class="order-card">
              <span class="event-time">${{t}}</span>
              <span class="order-status ${{o.status}}">${{escapeHtml(o.status)}}</span>
              <div>
                <strong>${{escapeHtml(o.direction || "")}}</strong> @ ${{escapeHtml(String(entry))}} · SL ${{escapeHtml(String(sl))}} · TP ${{escapeHtml(o.tps || "-")}}
                <div class="event-extra">手數 ${{escapeHtml(String(o.lot ?? ""))}} · ${{ticket}} · ${{escapeHtml(o.source || "")}}</div>
              </div>
            </div>`;
          }}).join("");
        }}
      }}
    }}
    document.getElementById("start").onclick = () => post("/api/start", collect()).then(refresh).catch(e => alert(e.message));
    document.getElementById("stop").onclick = () => post("/api/stop").then(refresh).catch(e => alert(e.message));
    document.getElementById("quit").onclick = () => post("/api/quit").then(() => document.body.innerHTML = "<main><section><h1>程式已關閉</h1></section></main>").catch(e => alert(e.message));
    if (document.getElementById("testHub")) document.getElementById("testHub").onclick = () => post("/api/test-hub", collect()).then(j => alert(`連線成功 latest_seq=${{j.health.latest_seq}}`)).catch(e => alert(e.message));
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>"""


def _install_logging(state: LauncherState) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    handler = QueueLogHandler(state.log_queue)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(handler)


def main(default_role: Optional[str] = None) -> None:
    role = _infer_role(default_role)
    state = LauncherState(role)
    _install_logging(state)
    logger.info("%s 已啟動", state.title)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    state.control_server = server
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    webbrowser.open(url)
    logger.info("控制台：%s", url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_service()
        server.server_close()


if __name__ == "__main__":
    main()

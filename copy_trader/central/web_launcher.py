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
            "member_name": "",  # 會員顯示名稱（給訊號中心管理面板看）
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
            # 來源過濾（多筆以逗號分隔，留空 = 不過濾）
            "source_whitelist": "",
            "source_blacklist": "",
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
        # 熱套用：服務運行中且為 client 模式時，把新設定即時推到 agent 不必重啟
        if self.role == "client" and self.client_agent is not None:
            try:
                self.client_agent.apply_overrides_live(merged)
            except Exception as exc:
                logger.warning("熱套用設定失敗：%s", exc)
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
            self.client_agent._safe_register()
            logger.info(
                "會員端已啟動，Hub=%s，client_id=%s，last_seq=%s",
                hub_url, self.client_agent.client_id, self.client_agent.last_seq,
            )
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
            snap["paused"] = bool(self.client_agent.paused)
            snap["active_positions"] = self.client_agent.trade_manager.active_position_count()
            snap["max_open_positions"] = int(getattr(self.client_agent.config, "max_open_positions", 0) or 0)
            snap["client_id"] = self.client_agent.client_id
            snap["member_name"] = self.client_agent._display_name()
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
                if parsed.path == "/api/pause":
                    if state.client_agent is None:
                        _json_response(self, 400, {"ok": False, "error": "agent_not_running"})
                        return
                    data = _read_json(self)
                    paused = bool(data.get("paused", True))
                    state.client_agent.set_paused(paused)
                    _json_response(self, 200, {"ok": True, "paused": paused})
                    return
                if parsed.path == "/api/close-all":
                    if state.client_agent is None:
                        _json_response(self, 400, {"ok": False, "error": "agent_not_running"})
                        return
                    result = state.client_agent.close_all(reason="ui_close_all")
                    _json_response(self, 200, {"ok": True, "result": result})
                    return
                if parsed.path == "/api/reset-martingale":
                    if state.client_agent is None:
                        _json_response(self, 400, {"ok": False, "error": "agent_not_running"})
                        return
                    state.client_agent.reset_martingale()
                    _json_response(self, 200, {"ok": True})
                    return
            except Exception as exc:
                logger.exception("request failed: %s", exc)
                _json_response(self, 500, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 404, {"ok": False, "error": "not_found"})

    return Handler


_LAUNCHER_CSS = """
:root {
  --bg: #0a0e14;
  --bg-elev: #0f141b;
  --bg-card: #131a22;
  --bg-soft: #181f28;
  --bg-input: #0c1117;
  --border: #1f2832;
  --border-strong: #2b3744;
  --border-input: #2a3441;
  --text: #e6edf3;
  --text-dim: #8b96a3;
  --text-faint: #4d5864;
  --gold: #d4a24c;
  --gold-bright: #e8b964;
  --gold-soft: rgba(212, 162, 76, 0.10);
  --gold-line: rgba(212, 162, 76, 0.30);
  --green: #4ade80;
  --green-soft: rgba(74, 222, 128, 0.13);
  --green-line: rgba(74, 222, 128, 0.32);
  --red: #ef4444;
  --red-soft: rgba(239, 68, 68, 0.13);
  --red-line: rgba(239, 68, 68, 0.32);
  --amber: #f5b041;
  --amber-soft: rgba(245, 176, 65, 0.14);
  --amber-line: rgba(245, 176, 65, 0.32);
  --blue: #6ea8ff;
  --blue-soft: rgba(110, 168, 255, 0.13);
  --font-display: 'Fraunces', 'Noto Serif TC', 'Iowan Old Style', Georgia, serif;
  --font-mono: 'JetBrains Mono', 'Noto Sans TC', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 13px; line-height: 1.5;
  letter-spacing: 0.01em;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  min-height: 100vh;
}
body::before {
  content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 200;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.55'/%3E%3C/svg%3E");
  opacity: 0.03; mix-blend-mode: overlay;
}
body::after {
  content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 199;
  background: repeating-linear-gradient(180deg, transparent 0 2px, rgba(255,255,255,0.012) 2px 3px);
}
/* ---------- topbar ---------- */
.topbar {
  position: sticky; top: 0; z-index: 50;
  background: rgba(10,14,20,0.85);
  backdrop-filter: blur(14px) saturate(160%);
  -webkit-backdrop-filter: blur(14px) saturate(160%);
  border-bottom: 1px solid var(--border);
  padding: 13px 26px;
  display: grid; grid-template-columns: auto 1fr auto;
  gap: 24px; align-items: center;
}
.brand { display: flex; align-items: baseline; gap: 14px; min-width: 0; }
.brand-mark {
  font-family: var(--font-display);
  font-size: 24px; font-weight: 400; font-style: italic;
  letter-spacing: -0.025em; color: var(--gold); line-height: 1;
}
.brand-mark::after {
  content: ''; display: inline-block; width: 6px; height: 6px;
  background: var(--gold); margin-left: 6px; vertical-align: middle;
  transform: translateY(-3px);
}
.brand-meta {
  font-size: 9px; letter-spacing: 0.22em;
  color: var(--text-faint); text-transform: uppercase;
}
.role-tag {
  display: inline-block; margin-left: 8px;
  padding: 3px 8px; font-size: 9px;
  letter-spacing: 0.22em; text-transform: uppercase;
  color: var(--gold); border: 1px solid var(--gold-line);
  background: var(--gold-soft);
}
.title-display {
  font-family: var(--font-display);
  font-style: italic; font-size: 14px;
  color: var(--text-dim); letter-spacing: -0.01em;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.topbar-status { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; justify-content: flex-end; }
.live-pill {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 5px 11px; border: 1px solid var(--border-strong);
  font-size: 9px; letter-spacing: 0.22em;
  text-transform: uppercase; color: var(--text-dim);
  background: var(--bg-card);
}
.live-pill.running { border-color: var(--green-line); color: var(--green); }
.live-pill.warn { border-color: var(--amber-line); color: var(--amber); }
.live-pill.err { border-color: var(--red-line); color: var(--red); }
.live-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--text-faint);
}
.live-pill.running .live-dot {
  background: var(--green); box-shadow: 0 0 9px var(--green);
  animation: pulse 1.6s ease-in-out infinite;
}
.live-pill.warn .live-dot {
  background: var(--amber); box-shadow: 0 0 9px var(--amber);
  animation: pulse 1.6s ease-in-out infinite;
}
.live-pill.err .live-dot {
  background: var(--red); box-shadow: 0 0 9px var(--red);
  animation: pulse 0.7s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.3; transform: scale(0.6); }
}
.meta-pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 11px; border: 1px solid var(--border);
  font-size: 10px; letter-spacing: 0.06em; color: var(--text-dim);
  font-variant-numeric: tabular-nums; background: var(--bg-card);
}
.meta-pill .lbl {
  font-size: 9px; letter-spacing: 0.22em; text-transform: uppercase;
  color: var(--text-faint);
}
.clock {
  font-variant-numeric: tabular-nums;
  color: var(--text-dim); font-size: 12px;
  letter-spacing: 0.04em;
}
/* ---------- main grid ---------- */
main {
  padding: 22px 26px 26px;
  display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  gap: 18px; max-width: 1640px; margin: 0 auto;
  align-items: start;
}
@media (max-width: 1100px) {
  main { grid-template-columns: 1fr; padding: 16px; }
  .topbar { grid-template-columns: 1fr auto; }
  .topbar .topbar-status { grid-column: 1 / -1; justify-content: flex-start; }
}
.col { display: flex; flex-direction: column; gap: 18px; min-width: 0; }
/* ---------- panel ---------- */
.panel {
  background: var(--bg-card);
  border: 1px solid var(--border);
  position: relative;
}
.panel::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
  background: linear-gradient(90deg, transparent, var(--gold-line), transparent);
  opacity: 0.55;
}
.panel-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid var(--border); gap: 12px;
}
.panel-title {
  font-family: var(--font-display); font-size: 18px;
  font-weight: 400; font-style: italic;
  letter-spacing: -0.015em; color: var(--text);
}
.panel-title-tag {
  display: inline-block; margin-left: 8px;
  font-family: var(--font-mono); font-style: normal;
  font-size: 9px; letter-spacing: 0.2em; text-transform: uppercase;
  color: var(--gold); padding: 2px 6px; border: 1px solid var(--gold-line);
  vertical-align: middle;
}
.panel-meta {
  font-size: 10px; color: var(--text-faint);
  letter-spacing: 0.18em; text-transform: uppercase;
}
.panel-body { padding: 18px; }
.panel-body.compact { padding: 4px 0; }
/* ---------- form ---------- */
.group {
  border-bottom: 1px solid var(--border);
  padding: 14px 18px 18px;
}
.group:last-child { border-bottom: none; }
.group-head {
  display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px;
}
.group-num {
  font-size: 9px; letter-spacing: 0.22em; color: var(--gold);
}
.group-title {
  font-family: var(--font-display);
  font-style: italic; font-size: 15px;
  color: var(--text); letter-spacing: -0.01em;
}
.group-hint {
  font-size: 10px; letter-spacing: 0.04em;
  color: var(--text-faint); margin-left: auto;
}
.field {
  display: grid; grid-template-columns: 160px 1fr;
  align-items: center; gap: 14px;
  padding: 7px 0; border-bottom: 1px dashed var(--border);
}
.field:last-child { border-bottom: none; }
.field-label {
  font-size: 10px; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--text-dim);
}
.field-control { min-width: 0; }
input[type="text"], input:not([type]), input[type="password"], select {
  width: 100%; font: inherit; font-size: 12px;
  padding: 8px 10px; background: var(--bg-input);
  color: var(--text); border: 1px solid var(--border-input);
  border-radius: 0; outline: none;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  transition: border-color 0.12s ease, background 0.12s ease;
}
input[type="text"]:focus, input:not([type]):focus, input[type="password"]:focus, select:focus {
  border-color: var(--gold); background: var(--bg-soft);
}
input::placeholder { color: var(--text-faint); }
select { appearance: none; background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path fill='%238b96a3' d='M0 0l5 6 5-6z'/></svg>"); background-repeat: no-repeat; background-position: right 12px center; padding-right: 30px; }
/* checkbox as toggle */
.toggle { display: inline-flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
.toggle input { display: none; }
.toggle-track {
  width: 36px; height: 18px; background: var(--bg-input);
  border: 1px solid var(--border-input);
  position: relative; transition: background 0.15s ease, border-color 0.15s ease;
}
.toggle-thumb {
  position: absolute; top: 1px; left: 1px;
  width: 14px; height: 14px; background: var(--text-faint);
  transition: transform 0.18s ease, background 0.18s ease;
}
.toggle input:checked + .toggle-track { background: var(--gold-soft); border-color: var(--gold); }
.toggle input:checked + .toggle-track .toggle-thumb { transform: translateX(18px); background: var(--gold); }
.toggle-label { font-size: 11px; color: var(--text-dim); letter-spacing: 0.06em; }
/* ---------- buttons ---------- */
.button-row {
  display: flex; gap: 8px; flex-wrap: wrap;
  padding: 14px 18px;
  background: var(--bg-elev);
  border-bottom: 1px solid var(--border);
}
.button-row.runtime {
  background: var(--bg-soft);
  border-bottom: 1px solid var(--border);
}
button.btn {
  font: inherit; font-family: var(--font-mono);
  font-size: 11px; letter-spacing: 0.16em; text-transform: uppercase;
  padding: 8px 14px; background: transparent;
  color: var(--text-dim); border: 1px solid var(--border-strong);
  cursor: pointer; transition: color 0.12s ease, border-color 0.12s ease, background 0.12s ease;
  display: inline-flex; align-items: center; gap: 6px;
}
button.btn:hover { color: var(--text); border-color: var(--text-faint); background: var(--bg-soft); }
button.btn:disabled { opacity: 0.4; cursor: not-allowed; }
button.btn.primary {
  background: var(--gold); color: #0a0e14; border-color: var(--gold);
  font-weight: 600;
}
button.btn.primary:hover { background: var(--gold-bright); border-color: var(--gold-bright); color: #0a0e14; }
button.btn.danger { color: var(--red); border-color: var(--red-line); }
button.btn.danger:hover { background: var(--red-soft); color: var(--red); }
button.btn.warn { color: var(--amber); border-color: var(--amber-line); }
button.btn.warn:hover { background: var(--amber-soft); color: var(--amber); }
.button-glyph {
  display: inline-block; width: 6px; height: 6px;
  background: currentColor;
}
button.btn.primary .button-glyph { background: #0a0e14; }
.hint-row {
  padding: 10px 18px; font-size: 11px; color: var(--text-faint);
  letter-spacing: 0.04em; border-bottom: 1px solid var(--border);
  background: var(--bg-elev);
}
.hint-row.gold { color: var(--gold); background: var(--gold-soft); border-bottom: 1px solid var(--gold-line); }
/* ---------- logs ---------- */
.logs {
  height: 300px; overflow: auto; white-space: pre-wrap;
  background: #06090d; color: #b8d4c4;
  padding: 14px 16px; font-family: var(--font-mono); font-size: 11.5px;
  line-height: 1.55; border-top: 1px solid var(--border);
  position: relative;
}
.logs::before {
  content: '$ tail -f service.log'; display: block;
  color: var(--gold); font-size: 10px; letter-spacing: 0.16em;
  text-transform: uppercase; margin-bottom: 8px; opacity: 0.7;
}
/* ---------- right panel: captures (central) ---------- */
.capture-card {
  border-bottom: 1px solid var(--border);
  padding: 14px 18px;
  transition: background 0.12s ease;
}
.capture-card:hover { background: var(--bg-soft); }
.capture-meta {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: 10px; margin-bottom: 8px; flex-wrap: wrap;
}
.capture-source {
  font-size: 11px; color: var(--gold);
  letter-spacing: 0.04em; font-weight: 500;
}
.capture-stamp {
  font-size: 10px; color: var(--text-faint);
  letter-spacing: 0.06em; font-variant-numeric: tabular-nums;
}
.capture-status {
  font-size: 10px; letter-spacing: 0.16em; text-transform: uppercase;
  padding: 1px 7px; border: 1px solid;
}
.capture-status.ok { color: var(--green); border-color: var(--green-line); background: var(--green-soft); }
.capture-status.bad { color: var(--red); border-color: var(--red-line); background: var(--red-soft); }
.capture-text {
  font-size: 11px; color: var(--text-dim);
  background: var(--bg-input); padding: 10px 12px;
  border-left: 2px solid var(--border-strong);
  white-space: pre-wrap; word-break: break-word;
  max-height: 180px; overflow: auto;
  font-variant-numeric: tabular-nums;
}
.capture-msgs { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
.capture-msg {
  font-size: 11px; padding: 8px 10px;
  background: var(--bg-soft); border-left: 2px solid var(--gold);
  white-space: pre-wrap; word-break: break-word;
  color: var(--text);
}
.capture-msg-meta {
  font-size: 9px; color: var(--text-faint);
  letter-spacing: 0.12em; text-transform: uppercase; margin-bottom: 4px;
}
/* ---------- right panel: martingale strip (client) ---------- */
.mg-strip {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 1px; background: var(--border); margin: 0;
}
.mg-cell {
  background: var(--bg-card); padding: 14px 18px;
  display: flex; flex-direction: column; gap: 4px;
}
.mg-label {
  font-size: 9px; letter-spacing: 0.22em; text-transform: uppercase;
  color: var(--text-faint);
}
.mg-value {
  font-size: 18px; color: var(--text);
  font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
}
.mg-value.gold { color: var(--gold-bright); }
.mg-value.green { color: var(--green); }
.mg-value.red { color: var(--red); }
.mg-value.amber { color: var(--amber); }
.mg-value.dim { color: var(--text-dim); font-size: 14px; }
.mg-sub {
  font-size: 10px; color: var(--text-dim);
  letter-spacing: 0.06em;
}
/* ---------- right panel: events (client) ---------- */
.events { max-height: 360px; overflow-y: auto; }
.event {
  display: grid; grid-template-columns: 70px 100px 1fr;
  gap: 12px; padding: 9px 18px; align-items: baseline;
  border-bottom: 1px solid var(--border);
  font-size: 11.5px;
}
.event:last-child { border-bottom: none; }
.event:hover { background: var(--bg-soft); }
.event-time {
  color: var(--text-faint); font-size: 10px;
  font-variant-numeric: tabular-nums; letter-spacing: 0.04em;
}
.event-kind {
  font-size: 9px; letter-spacing: 0.2em; text-transform: uppercase;
  padding: 2px 7px; text-align: center;
  font-weight: 500;
}
.event-kind.signal_received { background: var(--blue-soft); color: var(--blue); }
.event-kind.submitted { background: var(--green-soft); color: var(--green); }
.event-kind.skipped_dedup, .event-kind.skipped_invalid { background: var(--amber-soft); color: var(--amber); }
.event-kind.submit_failed { background: var(--red-soft); color: var(--red); }
.event-kind.unknown { background: transparent; color: var(--text-faint); border: 1px solid var(--border); }
.event-summary {
  color: var(--text); font-size: 11.5px; word-break: break-word;
  font-variant-numeric: tabular-nums;
}
.event-extra {
  color: var(--text-faint); font-size: 10px;
  letter-spacing: 0.04em; margin-top: 2px;
}
/* ---------- right panel: orders (client) ---------- */
.orders { max-height: 380px; overflow-y: auto; }
.order {
  display: grid; grid-template-columns: 70px 84px 1fr auto;
  gap: 12px; padding: 11px 18px; align-items: center;
  border-bottom: 1px solid var(--border);
}
.order:last-child { border-bottom: none; }
.order:hover { background: var(--bg-soft); }
.order-time {
  font-size: 10px; color: var(--text-faint);
  font-variant-numeric: tabular-nums; letter-spacing: 0.04em;
}
.order-status-tag {
  font-size: 9px; letter-spacing: 0.18em; text-transform: uppercase;
  padding: 3px 7px; text-align: center; font-weight: 500;
}
.order-status-tag.pending { background: var(--amber-soft); color: var(--amber); }
.order-status-tag.sent { background: var(--blue-soft); color: var(--blue); }
.order-status-tag.filled { background: var(--green-soft); color: var(--green); }
.order-status-tag.partial { background: var(--green-soft); color: var(--green); }
.order-status-tag.closed { background: var(--bg-input); color: var(--text-dim); border: 1px solid var(--border); }
.order-status-tag.cancelled { background: var(--bg-input); color: var(--text-faint); border: 1px solid var(--border); }
.order-status-tag.failed { background: var(--red-soft); color: var(--red); }
.order-main {
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
  font-variant-numeric: tabular-nums;
}
.order-dir {
  font-size: 11px; font-weight: 600; letter-spacing: 0.16em;
  padding: 1px 6px;
}
.order-dir.buy { color: var(--green); background: var(--green-soft); }
.order-dir.sell { color: var(--red); background: var(--red-soft); }
.order-price { font-size: 12px; color: var(--text); }
.order-meta {
  display: block; font-size: 10px; color: var(--text-faint);
  margin-top: 3px; letter-spacing: 0.04em;
  font-variant-numeric: tabular-nums;
  width: 100%;
}
.order-lot {
  font-size: 11px; color: var(--gold);
  font-variant-numeric: tabular-nums; letter-spacing: 0.04em;
  white-space: nowrap;
}
/* ---------- empty state ---------- */
.empty-state {
  padding: 32px 18px; text-align: center;
  color: var(--text-faint); font-size: 10px;
  letter-spacing: 0.22em; text-transform: uppercase;
}
/* scrollbar */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-strong); }
::-webkit-scrollbar-thumb:hover { background: var(--text-faint); }
"""


def _field(label: str, control_html: str) -> str:
    return (
        '<div class="field">'
        f'<label class="field-label">{label}</label>'
        f'<div class="field-control">{control_html}</div>'
        '</div>'
    )


def _input(field_id: str, placeholder: str = "", input_type: str = "text") -> str:
    pl = f' placeholder="{placeholder}"' if placeholder else ""
    return f'<input id="{field_id}" type="{input_type}"{pl} />'


def _select(field_id: str, options: list) -> str:
    opts = "".join(f'<option value="{v}">{label}</option>' for v, label in options)
    return f'<select id="{field_id}">{opts}</select>'


def _toggle(field_id: str, label_text: str = "啟用") -> str:
    return (
        '<label class="toggle">'
        f'<input id="{field_id}" type="checkbox" />'
        '<span class="toggle-track"><span class="toggle-thumb"></span></span>'
        f'<span class="toggle-label">{label_text}</span>'
        '</label>'
    )


def _central_settings_html() -> str:
    inner = (
        '<div class="group">'
        '<div class="group-head">'
        '<span class="group-num">01 ·</span>'
        '<span class="group-title">Cloud Hub</span>'
        '<span class="group-hint">將擷取到的訊號推送至雲端</span>'
        '</div>'
        + _field("Hub URL", _input("external_hub_url", "https://gold-signal-hub-tw.fly.dev"))
        + _field("Hub 密碼", _input("token", input_type="password"))
        + _field("複製模式", _select("copy_mode", [("all", "全選複製"), ("tail", "底部幾屏")]))
        + _field("輪詢秒數", _input("interval", "1.0"))
        + '</div>'
    )
    return inner


def _client_settings_html() -> str:
    groups = []
    # group 1 — connection
    groups.append(
        '<div class="group">'
        '<div class="group-head">'
        '<span class="group-num">01 ·</span>'
        '<span class="group-title">Connection</span>'
        '<span class="group-hint">與訊號中心 Hub 連線</span>'
        '</div>'
        + _field("Hub URL", _input("hub_url", "http://中央電腦IP:8765"))
        + _field("Hub 密碼", _input("token", input_type="password"))
        + _field("會員顯示名稱", _input("member_name", "留空 = 自動產生"))
        + _field("MT5 Files 路徑", _input("mt5_files_dir", "可留空自動偵測"))
        + _field("輪詢秒數", _input("interval", "1.0"))
        + '</div>'
    )
    # group 2 — orders
    groups.append(
        '<div class="group">'
        '<div class="group-head">'
        '<span class="group-num">02 ·</span>'
        '<span class="group-title">Order Sizing</span>'
        '<span class="group-hint">手數 / 馬丁加碼 / 分批 TP</span>'
        '</div>'
        + _field("預設手數", _input("default_lot_size", "0.01"))
        + _field("啟用馬丁", _toggle("use_martingale", "Martingale"))
        + _field("馬丁倍數", _input("martingale_multiplier", "2.0"))
        + _field("馬丁最大層級", _input("martingale_max_level", "4"))
        + _field("自訂每層手數", _input("martingale_lots", "例 0.01,0.02,0.04,0.08"))
        + _field("分批 TP 比例", _input("partial_close_ratios", "0.5,0.3,0.2"))
        + '</div>'
    )
    # group 3 — risk
    groups.append(
        '<div class="group">'
        '<div class="group-head">'
        '<span class="group-num">03 ·</span>'
        '<span class="group-title">Risk Control</span>'
        '<span class="group-hint">掛單時效 / 去重 / 持倉上限</span>'
        '</div>'
        + _field("掛單取消秒數", _input("cancel_pending_after_seconds", "7200"))
        + _field("取消偏離 %", _input("cancel_if_price_beyond_percent", "1.0"))
        + _field("訊號 dedup 分鐘", _input("signal_dedup_minutes", "10"))
        + _field("最大同時持倉", _input("max_open_positions", "10"))
        + '</div>'
    )
    # group 4 — source filters
    groups.append(
        '<div class="group">'
        '<div class="group-head">'
        '<span class="group-num">04 ·</span>'
        '<span class="group-title">Source Filter</span>'
        '<span class="group-hint">逗號分隔，留空 = 不過濾</span>'
        '</div>'
        + _field("白名單來源", _input("source_whitelist"))
        + _field("黑名單來源", _input("source_blacklist"))
        + '</div>'
    )
    return "".join(groups)


def _central_right_panel() -> str:
    return (
        '<div class="panel">'
        '<div class="panel-header">'
        '<span class="panel-title">Capture Stream<span class="panel-title-tag">LINE</span></span>'
        '<span class="panel-meta" id="capture-summary">—</span>'
        '</div>'
        '<div class="panel-body compact">'
        '<div id="captures"><div class="empty-state">服務啟動後此處會出現複製內容</div></div>'
        '</div>'
        '</div>'
    )


def _client_right_panel() -> str:
    return (
        # martingale + position strip
        '<div class="panel">'
        '<div class="panel-header">'
        '<span class="panel-title">Runtime Telemetry</span>'
        '<span class="panel-meta" id="telemetry-meta">—</span>'
        '</div>'
        '<div class="mg-strip" id="mg-strip">'
        '<div class="mg-cell"><span class="mg-label">Status</span><span class="mg-value dim" id="mg-status">—</span></div>'
        '<div class="mg-cell"><span class="mg-label">Martingale</span><span class="mg-value gold" id="mg-level">—</span><span class="mg-sub" id="mg-mode">—</span></div>'
        '<div class="mg-cell"><span class="mg-label">Open Positions</span><span class="mg-value" id="mg-positions">—</span><span class="mg-sub" id="mg-positions-cap">—</span></div>'
        '<div class="mg-cell"><span class="mg-label">Last Seq</span><span class="mg-value dim" id="mg-seq">—</span></div>'
        '</div>'
        '</div>'
        # events
        '<div class="panel">'
        '<div class="panel-header">'
        '<span class="panel-title">Event Feed<span class="panel-title-tag">Live</span></span>'
        '<span class="panel-meta">last 30</span>'
        '</div>'
        '<div class="events" id="events"><div class="empty-state">no events yet</div></div>'
        '</div>'
        # orders
        '<div class="panel">'
        '<div class="panel-header">'
        '<span class="panel-title">MT5 Orders</span>'
        '<span class="panel-meta">last 20</span>'
        '</div>'
        '<div class="orders" id="orders"><div class="empty-state">no orders yet</div></div>'
        '</div>'
    )


_LAUNCHER_JS = r"""
(function () {
  const role = "__ROLE__";
  let snapshot = null;
  let didFill = false;
  function ids() {
    if (role === "central") return ["external_hub_url","token","copy_mode","interval"];
    return ["hub_url","token","member_name","mt5_files_dir","interval",
            "default_lot_size","use_martingale","martingale_multiplier","martingale_max_level",
            "martingale_lots","partial_close_ratios",
            "cancel_pending_after_seconds","cancel_if_price_beyond_percent",
            "signal_dedup_minutes","max_open_positions",
            "source_whitelist","source_blacklist"];
  }
  function $(id) { return document.getElementById(id); }
  function collect() {
    const out = {};
    for (const id of ids()) {
      const el = $(id);
      if (!el) continue;
      out[id] = el.type === "checkbox" ? (el.checked ? "true" : "false") : el.value;
    }
    return out;
  }
  function fill(settings) {
    for (const id of ids()) {
      const el = $(id);
      if (!el) continue;
      if (el.type === "checkbox") {
        el.checked = ["true","1","yes","on"].includes(String(settings[id] || "").toLowerCase());
      } else {
        el.value = settings[id] || "";
      }
    }
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c];
    });
  }
  function fmtTime(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleTimeString("zh-TW", { hour12: false });
  }
  function fmtRel(ts) {
    if (!ts) return "—";
    const diff = (Date.now() / 1000) - ts;
    if (diff < 5) return "now";
    if (diff < 60) return Math.floor(diff) + "s";
    if (diff < 3600) return Math.floor(diff / 60) + "m";
    if (diff < 86400) return Math.floor(diff / 3600) + "h";
    return Math.floor(diff / 86400) + "d";
  }
  function fmtUptime(s) {
    if (!s) return "0s";
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
  }
  function clockTick() {
    const t = new Date();
    const local = t.toLocaleTimeString("zh-TW", { hour12: false });
    const utc = t.toISOString().slice(11, 19);
    const el = $("clock");
    if (el) el.textContent = local + " · " + utc + "Z";
  }
  setInterval(clockTick, 1000);
  clockTick();
  async function post(path, data) {
    data = data || {};
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const json = await res.json();
    if (!json.ok) throw new Error(json.error || "request failed");
    return json;
  }
  function setLivePill(snap) {
    const pill = $("live-pill");
    const dot = $("live-dot");
    const txt = $("live-text");
    if (!pill || !dot || !txt) return;
    pill.classList.remove("running", "warn", "err");
    if (snap.running) {
      pill.classList.add("running");
      txt.textContent = snap.paused ? "Paused" : "Running";
      if (snap.paused) { pill.classList.remove("running"); pill.classList.add("warn"); }
    } else if (snap.status && /失敗|fail|error/i.test(snap.status)) {
      pill.classList.add("err");
      txt.textContent = "Failed";
    } else {
      txt.textContent = "Idle";
    }
    const upt = $("uptime");
    if (upt) upt.textContent = snap.running ? fmtUptime(snap.uptime_seconds || 0) : "—";
    const ip = $("lan-ip");
    if (ip) ip.textContent = snap.lan_ip || "—";
  }
  function renderCaptures(captures) {
    const root = $("captures");
    if (!root) return;
    if (!captures || captures.length === 0) {
      root.innerHTML = '<div class="empty-state">尚未有複製內容</div>';
      $("capture-summary").textContent = "0 captures";
      return;
    }
    const newest = captures.slice().reverse();
    const newCount = newest.reduce(function (a, c) { return a + (c.new_count || 0); }, 0);
    $("capture-summary").textContent = newest.length + " captures · " + newCount + " new msgs";
    root.innerHTML = newest.map(function (c) {
      const stamp = fmtTime(c.captured_at);
      const status = c.ok
        ? '<span class="capture-status ok">+' + (c.new_count || 0) + ' new</span>'
        : '<span class="capture-status bad">fail</span>';
      const msgs = (c.new_messages || []).map(function (m) {
        return '<div class="capture-msg"><div class="capture-msg-meta">' +
          esc(m.time_str || "") + " · " + esc(m.sender || "") +
          '</div>' + esc(m.body || "") + '</div>';
      }).join("");
      const msgsBlock = msgs ? '<div class="capture-msgs">' + msgs + '</div>' : "";
      const errBlock = (!c.ok && c.error) ? '<div class="capture-msg" style="border-left-color: var(--red); color: var(--red);">' + esc(c.error) + '</div>' : "";
      return '<div class="capture-card">' +
        '<div class="capture-meta">' +
          '<span class="capture-source">▸ ' + esc(c.source || "—") + '</span>' +
          '<span class="capture-stamp">' + stamp + '</span>' +
          status +
        '</div>' +
        '<div class="capture-text">' + esc(c.raw_text || "—") + '</div>' +
        msgsBlock + errBlock +
      '</div>';
    }).join("");
  }
  const KIND_LABEL = {
    signal_received: "received",
    submitted: "sent",
    skipped_dedup: "dedup",
    skipped_invalid: "invalid",
    submit_failed: "failed",
  };
  function renderClientPanels(snap) {
    // pause button + telemetry meta
    const pauseBtn = $("pauseToggle");
    if (pauseBtn) pauseBtn.textContent = snap.paused ? "▶  恢復跟單" : "❚❚  暫停跟單";
    const tm = $("telemetry-meta");
    if (tm) tm.textContent = (snap.member_name || snap.client_id || "—") + " · " + (snap.client_id || "—");
    // martingale strip
    const mst = $("mg-status");
    if (mst) {
      if (!snap.running) { mst.textContent = "IDLE"; mst.className = "mg-value dim"; }
      else if (snap.paused) { mst.textContent = "PAUSED"; mst.className = "mg-value amber"; }
      else { mst.textContent = "ACTIVE"; mst.className = "mg-value green"; }
    }
    const ml = $("mg-level");
    const mm = $("mg-mode");
    if (ml) ml.textContent = "L" + (snap.martingale_level || 0);
    if (mm) mm.textContent = snap.martingale_enabled ? "enabled · escalating" : "disabled · flat";
    const mp = $("mg-positions");
    const mpc = $("mg-positions-cap");
    if (mp) mp.textContent = String(snap.active_positions || 0);
    if (mpc) mpc.textContent = snap.max_open_positions ? "cap " + snap.max_open_positions : "no cap";
    const ms = $("mg-seq");
    if (ms) ms.textContent = "#" + (snap.last_seq || 0);
    // events
    const evRoot = $("events");
    if (evRoot) {
      const events = (snap.recent_events || []).slice().reverse();
      if (events.length === 0) {
        evRoot.innerHTML = '<div class="empty-state">no events yet</div>';
      } else {
        evRoot.innerHTML = events.map(function (e) {
          const kindClass = KIND_LABEL[e.kind] ? e.kind : "unknown";
          const label = KIND_LABEL[e.kind] || (e.kind || "—");
          let extra = "";
          if (e.kind === "submitted") {
            extra = "lot " + (e.lot_size || "—") + " · L" + (e.martingale_level || 0) + " · seq #" + (e.seq || 0);
          } else if (e.seq != null) {
            extra = "seq #" + e.seq;
          }
          if (e.source) extra = (extra ? extra + " · " : "") + e.source;
          return '<div class="event">' +
            '<span class="event-time">' + fmtTime(e.time) + '</span>' +
            '<span class="event-kind ' + kindClass + '">' + esc(label) + '</span>' +
            '<div>' +
              '<div class="event-summary">' + esc(e.summary || "—") + '</div>' +
              (extra ? '<div class="event-extra">' + esc(extra) + '</div>' : "") +
            '</div>' +
          '</div>';
        }).join("");
      }
    }
    // orders
    const orRoot = $("orders");
    if (orRoot) {
      const orders = (snap.orders || []).slice().reverse();
      if (orders.length === 0) {
        orRoot.innerHTML = '<div class="empty-state">no orders yet</div>';
      } else {
        orRoot.innerHTML = orders.map(function (o) {
          const t = o.entry_time ? fmtTime(o.entry_time) : "—";
          let entry;
          if (o.entry === null || o.entry === undefined) entry = "—";
          else if (typeof o.entry === "number") entry = o.entry.toFixed(2);
          else entry = String(o.entry);
          const sl = (o.sl == null) ? "—" : Number(o.sl).toFixed(2);
          const ticket = o.ticket ? "#" + o.ticket : "—";
          const dirCls = (o.direction || "").toLowerCase() === "buy" ? "buy" : ((o.direction || "").toLowerCase() === "sell" ? "sell" : "");
          const status = (o.status || "").toLowerCase();
          return '<div class="order">' +
            '<span class="order-time">' + t + '</span>' +
            '<span class="order-status-tag ' + esc(status) + '">' + esc(o.status || "—") + '</span>' +
            '<div class="order-main">' +
              '<span class="order-dir ' + dirCls + '">' + esc(o.direction || "—") + '</span>' +
              '<span class="order-price">' + esc(String(entry)) + '</span>' +
              '<span style="color: var(--text-faint); font-size: 10px;">SL ' + esc(sl) + '</span>' +
              '<span style="color: var(--text-dim); font-size: 10px;">TP ' + esc(o.tps || "—") + '</span>' +
              '<span class="order-meta">' + ticket + ' · ' + esc(o.source || "—") + '</span>' +
            '</div>' +
            '<span class="order-lot">' + esc(String(o.lot != null ? o.lot : "—")) + '</span>' +
          '</div>';
        }).join("");
      }
    }
  }
  async function refresh() {
    try {
      const res = await fetch("/api/status");
      snapshot = await res.json();
      if (!snapshot.ok) return;
      if (!didFill) { fill(snapshot.settings); didFill = true; }
      const statusText = $("status-text");
      if (statusText) statusText.textContent = snapshot.status;
      setLivePill(snapshot);
      const logs = $("logs");
      if (logs) {
        logs.textContent = (snapshot.logs || []).join("\n");
        logs.scrollTop = logs.scrollHeight;
      }
      if (role === "central") {
        const url = (snapshot.settings || {}).external_hub_url || "";
        const hint = $("hint");
        if (hint) {
          if (url) {
            hint.classList.add("gold");
            hint.textContent = "▸ 會員端 Hub URL 填：" + url;
          } else {
            hint.classList.remove("gold");
            hint.textContent = "請填入雲端 Hub URL 後再按 START";
          }
        }
        renderCaptures(snapshot.latest_captures);
      } else {
        renderClientPanels(snapshot);
      }
    } catch (e) {
      const pill = $("live-pill"); const txt = $("live-text");
      if (pill && txt) { pill.classList.remove("running","warn"); pill.classList.add("err"); txt.textContent = "Offline"; }
    }
  }
  // bindings
  function bind(id, handler) { const el = $(id); if (el) el.onclick = handler; }
  bind("start", function () { post("/api/start", collect()).then(refresh).catch(function (e) { alert(e.message); }); });
  bind("stop", function () { post("/api/stop").then(refresh).catch(function (e) { alert(e.message); }); });
  bind("quit", function () {
    post("/api/quit").then(function () {
      document.body.innerHTML = '<main style="padding:60px 24px;text-align:center;"><div style="font-family:var(--font-display);font-style:italic;color:var(--gold);font-size:32px;">— 程式已關閉 —</div><div style="margin-top:18px;color:var(--text-faint);letter-spacing:0.16em;text-transform:uppercase;font-size:11px;">XAU/Signal · session ended</div></main>';
    }).catch(function (e) { alert(e.message); });
  });
  bind("testHub", function () {
    post("/api/test-hub", collect()).then(function (j) {
      alert("連線成功 latest_seq=" + j.health.latest_seq);
    }).catch(function (e) { alert(e.message); });
  });
  bind("pauseToggle", function () {
    const next = !(snapshot && snapshot.paused);
    post("/api/pause", { paused: next }).then(refresh).catch(function (e) { alert(e.message); });
  });
  bind("closeAll", function () {
    if (!confirm("確認要全部撤單 + 全部平倉嗎？")) return;
    post("/api/close-all").then(function (j) {
      const r = j.result || {};
      alert("完成：撤 " + (r.cancelled || 0) + " 平 " + (r.closed || 0) + " 失敗 " + (r.failed || 0));
      refresh();
    }).catch(function (e) { alert(e.message); });
  });
  bind("resetMg", function () {
    if (!confirm("把馬丁層級歸零嗎？")) return;
    post("/api/reset-martingale").then(refresh).catch(function (e) { alert(e.message); });
  });
  refresh();
  setInterval(refresh, 1000);
})();
"""


def _page_html(state: LauncherState) -> str:
    is_central = state.role == "central"
    role_tag = "Signal Capture" if is_central else "Copy Trader"
    role_subtitle = "LINE → Cloud Hub" if is_central else "Cloud Hub → MT5"

    if is_central:
        settings_html = _central_settings_html()
        right_panel = _central_right_panel()
        runtime_buttons = ""
        extra_button = ""
        hint_html = '<div class="hint-row" id="hint">請填入雲端 Hub URL 後再按 START</div>'
    else:
        settings_html = _client_settings_html()
        right_panel = _client_right_panel()
        runtime_buttons = (
            '<div class="button-row runtime">'
            '<button class="btn warn" id="pauseToggle"><span class="button-glyph"></span>暫停跟單</button>'
            '<button class="btn danger" id="closeAll"><span class="button-glyph"></span>一鍵全平倉</button>'
            '<button class="btn" id="resetMg"><span class="button-glyph"></span>重置馬丁</button>'
            '</div>'
        )
        extra_button = '<button class="btn" id="testHub"><span class="button-glyph"></span>Test Hub</button>'
        hint_html = ""

    primary_button_label = "Start Service"

    template = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__TITLE__</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..900;1,9..144,300..900&family=JetBrains+Mono:wght@300;400;500;600;700&family=Noto+Sans+TC:wght@300;400;500;600;700&family=Noto+Serif+TC:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
  <style>__CSS__</style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <span class="brand-mark">XAU/Signal</span>
      <span class="brand-meta">__ROLE_TAG__</span>
      <span class="role-tag">__ROLE_UPPER__</span>
    </div>
    <div class="title-display">__ROLE_SUBTITLE__</div>
    <div class="topbar-status">
      <span class="meta-pill"><span class="lbl">LAN</span><span id="lan-ip">—</span></span>
      <span class="meta-pill"><span class="lbl">Uptime</span><span id="uptime">—</span></span>
      <span class="meta-pill"><span class="lbl">Status</span><span id="status-text">載入中</span></span>
      <span class="live-pill" id="live-pill"><span class="live-dot" id="live-dot"></span><span id="live-text">Idle</span></span>
      <span class="clock" id="clock">—</span>
    </div>
  </div>
  <main>
    <div class="col">
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Configuration<span class="panel-title-tag">__ROLE_UPPER__</span></span>
          <span class="panel-meta">設定即時儲存</span>
        </div>
        <div class="button-row">
          <button class="btn primary" id="start"><span class="button-glyph"></span>__PRIMARY_LABEL__</button>
          <button class="btn" id="stop"><span class="button-glyph"></span>Stop</button>
          __EXTRA_BUTTON__
          <button class="btn danger" id="quit" style="margin-left:auto;"><span class="button-glyph"></span>Quit</button>
        </div>
        __RUNTIME_BUTTONS__
        __HINT__
        __SETTINGS__
      </div>
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Service Log<span class="panel-title-tag">tail</span></span>
          <span class="panel-meta" id="log-meta">last 200 lines</span>
        </div>
        <div class="logs" id="logs">awaiting log stream…</div>
      </div>
    </div>
    <div class="col">
      __RIGHT_PANEL__
    </div>
  </main>
  <script>__JS__</script>
</body>
</html>"""

    js = _LAUNCHER_JS.replace("__ROLE__", state.role)
    return (
        template
        .replace("__CSS__", _LAUNCHER_CSS)
        .replace("__JS__", js)
        .replace("__TITLE__", state.title)
        .replace("__ROLE_TAG__", role_tag)
        .replace("__ROLE_UPPER__", state.role.upper())
        .replace("__ROLE_SUBTITLE__", role_subtitle)
        .replace("__SETTINGS__", settings_html)
        .replace("__RIGHT_PANEL__", right_panel)
        .replace("__RUNTIME_BUTTONS__", runtime_buttons)
        .replace("__EXTRA_BUTTON__", extra_button)
        .replace("__HINT__", hint_html)
        .replace("__PRIMARY_LABEL__", primary_button_label)
    )


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

"""
Client-side MT5 execution agent.

Run one copy on each user's computer. It polls the central hub and writes MT5
bridge commands locally through the existing TradeManager.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from copy_trader.config import DATA_DIR, DEFAULT_SYMBOL, load_config
from copy_trader.signal_parser.regex_parser import ParsedSignal
from copy_trader.trade_manager import OrderStatus, TradeManager

logger = logging.getLogger(__name__)


class HubClient:
    def __init__(self, hub_url: str, token: str = "", timeout: float = 8.0):
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, path: str) -> Dict:
        req = urllib.request.Request(f"{self.hub_url}{path}", method="GET")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.hub_url}{path}",
            data=raw,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def health(self) -> Dict:
        return self._request("/health")

    def signals_after(self, after: int, limit: int = 50) -> List[Dict]:
        data = self._request(f"/signals?after={after}&limit={limit}")
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "hub_request_failed")
        return list(data.get("signals") or [])

    def register(self, client_id: str, name: str = "", meta: Optional[Dict[str, Any]] = None) -> Dict:
        return self._post("/members/register", {
            "client_id": client_id,
            "name": name,
            "meta": meta or {},
        })

    def heartbeat(self, client_id: str, meta: Optional[Dict[str, Any]] = None) -> Dict:
        return self._post("/members/heartbeat", {
            "client_id": client_id,
            "meta": meta or {},
        })

    def ack(self, payload: Dict[str, Any]) -> Dict:
        return self._post("/ack", payload)


def _load_state(path: Path) -> Dict:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("failed to load state %s: %s", path, e)
    return {}


def _save_state(path: Path, state: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _parsed_signal_from_payload(payload: Dict) -> ParsedSignal:
    return ParsedSignal(
        is_valid=True,
        symbol=str(payload.get("symbol") or DEFAULT_SYMBOL),
        direction=str(payload.get("direction") or ""),
        entry_price=payload.get("entry_price"),
        is_market_order=bool(payload.get("is_market_order")),
        stop_loss=payload.get("stop_loss"),
        take_profit=list(payload.get("take_profit") or []),
        lot_size=payload.get("lot_size"),
        confidence=float(payload.get("confidence") or 0.95),
        raw_text_summary=str(payload.get("raw_text_summary") or ""),
        parse_method=str(payload.get("parse_method") or "hub"),
        error=str(payload.get("error") or ""),
    )


def _is_executable_signal(signal: ParsedSignal) -> bool:
    has_entry = signal.entry_price is not None or bool(signal.is_market_order)
    return bool(
        signal.is_valid
        and signal.direction in {"buy", "sell"}
        and has_entry
        and signal.stop_loss is not None
        and signal.take_profit
    )


# 與 signal_collector.PRICE_BUCKET 對齊：價格四捨五入到最近的 2 USD，
# 吸收 OCR jitter / 作者修字。
_CLIENT_PRICE_BUCKET = 2.0


def _client_bucket_price(value) -> float:
    if value is None:
        return 0.0
    try:
        return round(float(value) / _CLIENT_PRICE_BUCKET) * _CLIENT_PRICE_BUCKET
    except (TypeError, ValueError):
        return 0.0


def _client_signal_key(signal: ParsedSignal, source: str) -> Tuple:
    """會員端用的訊號去重 key — 與中央 _signal_key 同邏輯。

    刻意忽略 entry / is_market_order：同一筆訊號常見變體會讓這兩欄漂移，
    但 (source, direction, sl_bucket, tps_bucket) 已足以識別「這是同一張單」。
    """
    tps = tuple(sorted(_client_bucket_price(tp) for tp in (signal.take_profit or []) if tp is not None))
    return (
        str(source or ""),
        str(signal.direction or ""),
        _client_bucket_price(signal.stop_loss),
        tps,
    )


def _signal_summary(signal: ParsedSignal) -> str:
    """給 UI 顯示用的訊號摘要文字。"""
    direction = (signal.direction or "?").upper()
    entry = "市價" if signal.is_market_order else (f"{float(signal.entry_price):.2f}" if signal.entry_price is not None else "-")
    sl = f"{float(signal.stop_loss):.2f}" if signal.stop_loss is not None else "-"
    tps = ", ".join(f"{float(tp):.2f}" for tp in (signal.take_profit or []) if tp is not None) or "-"
    return f"{direction} @ {entry} | SL: {sl} | TP: {tps}"


def _normalize_client_key(raw) -> Tuple:
    """JSON 反序列化後 list 還原成 _client_signal_key 的 tuple。

    新格式 4 欄：(source, direction, sl_bucket, tps_bucket)。
    舊紀錄(6 欄含 entry/is_market) 也兼容 — 取 sl + tps 即可，舊紀錄不會匹配新訊號
    但不會 crash；只要 TTL 過後就清掉。
    """
    try:
        if len(raw) >= 6:
            return (
                str(raw[0]),
                str(raw[1]),
                float(raw[4]),
                tuple(sorted(float(x) for x in (raw[5] or []))),
            )
        return (
            str(raw[0]),
            str(raw[1]),
            float(raw[2]),
            tuple(sorted(float(x) for x in (raw[3] or []))),
        )
    except (TypeError, ValueError, IndexError):
        return ("", "", 0.0, tuple())


class MT5ClientAgent:
    def __init__(
        self,
        hub: HubClient,
        state_file: Path,
        mt5_files_dir: str = "",
        replay: bool = False,
        overrides: Optional[Dict[str, Any]] = None,
    ):
        self.hub = hub
        self.state_file = state_file
        self.config = load_config()
        if mt5_files_dir:
            self.config.mt5_files_dir = mt5_files_dir

        # 套用 web UI 上的下注設定覆寫（存在 web_launcher settings 內，不污染 config.json）
        self._apply_overrides(overrides or {})

        # 第二層保護：MT5 端持久化已下單訊號 key，避免同一筆 (來源,方向,進場,SL,TPs) 重複下單
        self._processed_ttl = max(86400, int(getattr(self.config, "signal_dedup_minutes", 10) or 10) * 60)
        self._processed_signals: Dict[Tuple, float] = {}

        # 給 UI 看的事件 buffer（收到 Hub 訊號 / 跳過 / 送 MT5 / 失敗 等）
        self.recent_events: Deque[Dict[str, Any]] = deque(maxlen=30)

        # 暫停跟單旗標 — UI 可以 toggle，paused 期間 run_cycle 直接 return 不寫 MT5
        self.paused: bool = False

        # Source filter（來源白名單/黑名單）— 空 list = 不限制
        self.source_whitelist: List[str] = []
        self.source_blacklist: List[str] = []
        self._apply_source_filter_overrides(overrides or {})

        # 會員身份（給訊號中心 dashboard 看誰收到/下單）
        # client_id 在 self.state 載入後才設定（見後段 _ensure_client_id）
        self.member_name: str = str((overrides or {}).get("member_name") or "").strip()
        self.client_id: str = ""

        # 訂單狀態變化追蹤 — 用來在 FILLED / CLOSED 時補 ack 給 hub
        # signal_id -> last seen status
        self._order_status_seen: Dict[str, str] = {}
        # signal_id -> {seq, source, signal_summary}（用來 ack 時補方向/來源資訊）
        self._signal_meta: Dict[str, Dict[str, Any]] = {}

        self._last_heartbeat_at: float = 0.0

        self.trade_manager = TradeManager(self.config.mt5_files_dir)
        self.trade_manager.default_lot_size = self.config.default_lot_size
        self.trade_manager.set_symbol_name(getattr(self.config, "symbol_name", DEFAULT_SYMBOL))
        self.trade_manager.partial_close_ratios = self.config.partial_close_ratios
        self.trade_manager.use_martingale = self.config.use_martingale
        self.trade_manager.martingale_multiplier = self.config.martingale_multiplier
        self.trade_manager.martingale_max_level = self.config.martingale_max_level
        self.trade_manager.martingale_lots = getattr(self.config, "martingale_lots", [])
        self.trade_manager.martingale_per_source = getattr(self.config, "martingale_per_source", False)
        self.trade_manager.martingale_source_lots = getattr(self.config, "martingale_source_lots", {})

        self.state = _load_state(state_file)
        self._ensure_client_id()
        self._restore_processed_signals()
        if replay:
            self.state["last_seq"] = 0
            self.state["hub_url"] = self.hub.hub_url
            self._processed_signals.clear()
            self.state["processed_signals"] = []
            _save_state(self.state_file, self.state)
        else:
            health = self.hub.health()
            latest_seq = int(health.get("latest_seq") or 0)
            state_hub_url = str(self.state.get("hub_url") or "")
            state_last_seq = int(self.state.get("last_seq") or 0)
            if (
                "last_seq" not in self.state
                or state_hub_url != self.hub.hub_url
                or state_last_seq > latest_seq
            ):
                self.state["last_seq"] = latest_seq
            self.state["hub_url"] = self.hub.hub_url
            _save_state(self.state_file, self.state)

    def _restore_processed_signals(self) -> None:
        """從 state 還原已下單訊號 key 集合，順便丟掉 TTL 過期的。"""
        raw_list = self.state.get("processed_signals") or []
        now = time.time()
        for entry in raw_list:
            try:
                key = _normalize_client_key(entry.get("key") or [])
                ts = float(entry.get("ts", 0))
                if now - ts > self._processed_ttl:
                    continue
                self._processed_signals[key] = ts
            except Exception:
                continue
        if self._processed_signals:
            logger.info("已載入 %s 筆會員端下單去重紀錄", len(self._processed_signals))

    def _persist_processed_signals(self) -> None:
        self.state["processed_signals"] = [
            {"key": list(k), "ts": ts} for k, ts in self._processed_signals.items()
        ]
        _save_state(self.state_file, self.state)

    def _record_event(self, kind: str, **fields: Any) -> None:
        """記錄一筆 UI 用事件（kind: signal_received | skipped_dedup | skipped_invalid | submitted | submit_failed）"""
        self.recent_events.append({
            "time": time.time(),
            "kind": kind,
            **fields,
        })

    def _ensure_client_id(self) -> None:
        """從 state 載入或產生新的 client_id 並持久化。"""
        existing = str(self.state.get("client_id") or "").strip()
        if not existing:
            existing = "cli_" + uuid.uuid4().hex[:12]
            self.state["client_id"] = existing
            _save_state(self.state_file, self.state)
        self.client_id = existing

    def _display_name(self) -> str:
        return self.member_name or self.client_id

    def _safe_register(self) -> None:
        if not self.client_id:
            return
        try:
            self.hub.register(self.client_id, self._display_name(), meta={"agent": "mt5_client"})
            self._last_heartbeat_at = time.time()
        except Exception as exc:
            logger.debug("hub register 失敗（離線中?）：%s", exc)

    def _safe_heartbeat(self, force: bool = False, interval: float = 30.0) -> None:
        if not self.client_id:
            return
        now = time.time()
        if not force and (now - self._last_heartbeat_at) < interval:
            return
        try:
            self.hub.heartbeat(self.client_id)
            self._last_heartbeat_at = now
        except Exception as exc:
            logger.debug("hub heartbeat 失敗：%s", exc)

    def _safe_ack(self, seq: int, status: str, **fields: Any) -> None:
        """送 ack 到 hub。網路失敗或舊版 hub 沒有此 endpoint 都靜默吞掉,不拖慢主迴圈。"""
        if not self.client_id or not seq:
            return
        payload = {
            "client_id": self.client_id,
            "name": self._display_name(),
            "seq": int(seq),
            "status": status,
        }
        for k, v in fields.items():
            if v is not None and v != "":
                payload[k] = v
        try:
            self.hub.ack(payload)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # 舊版 hub 沒這 endpoint — 不再嘗試這次 ack
                return
            logger.debug("hub ack 失敗：%s", exc)
        except Exception as exc:
            logger.debug("hub ack 失敗：%s", exc)

    def _scan_order_status_changes(self) -> None:
        """檢查所有 trade_manager 訂單的狀態變化，碰到 FILLED/CLOSED/FAILED/CANCELLED 就 ack 給 hub。"""
        try:
            orders = self.trade_manager.get_all_orders()
        except Exception:
            return
        for order in orders:
            sid = getattr(order, "signal_id", "")
            if not sid:
                continue
            new_status = getattr(order.status, "value", str(order.status))
            old = self._order_status_seen.get(sid)
            if new_status == old:
                continue
            self._order_status_seen[sid] = new_status
            meta = self._signal_meta.get(sid)
            if not meta:
                continue
            seq = int(meta.get("seq") or 0)
            ack_status = None
            extra: Dict[str, Any] = {}
            if new_status == "filled":
                ack_status = "filled"
                extra["ticket"] = getattr(order, "ticket", None)
            elif new_status == "closed":
                ack_status = "closed"
                extra["ticket"] = getattr(order, "ticket", None)
                extra["profit"] = getattr(order, "last_known_profit", None)
            elif new_status == "cancelled":
                ack_status = "cancelled"
            elif new_status == "failed":
                ack_status = "failed"
                extra["error"] = "trade_manager reported failed"
            if ack_status:
                self._safe_ack(
                    seq, ack_status,
                    signal_id=sid,
                    direction=meta.get("direction"),
                    **extra,
                )

    def _apply_source_filter_overrides(self, ov: Dict[str, Any]) -> None:
        def _split(raw):
            if raw is None:
                return []
            if isinstance(raw, list):
                return [str(x).strip() for x in raw if str(x).strip()]
            return [p.strip() for p in str(raw).replace("，", ",").split(",") if p.strip()]
        self.source_whitelist = _split(ov.get("source_whitelist"))
        self.source_blacklist = _split(ov.get("source_blacklist"))

    def _is_source_allowed(self, source: str) -> bool:
        s = (source or "").strip()
        if self.source_blacklist and any(bl == s for bl in self.source_blacklist):
            return False
        if self.source_whitelist and not any(wl == s for wl in self.source_whitelist):
            return False
        return True

    def apply_overrides_live(self, overrides: Dict[str, Any]) -> None:
        """在運行中熱套設定（不必停止/重啟服務）。

        會更新 config 上的下注/風控欄位，並把跟手數/馬丁/分批 TP 相關的欄位
        即時 push 到 trade_manager。Source filter 也即時生效。
        """
        self._apply_overrides(overrides)
        self._apply_source_filter_overrides(overrides)
        tm = self.trade_manager
        tm.default_lot_size = self.config.default_lot_size
        tm.use_martingale = self.config.use_martingale
        tm.martingale_multiplier = self.config.martingale_multiplier
        tm.martingale_max_level = self.config.martingale_max_level
        tm.martingale_lots = list(getattr(self.config, "martingale_lots", []) or [])
        tm.partial_close_ratios = list(self.config.partial_close_ratios or [])
        # 重新計算 TTL（dedup window 改了的話）
        self._processed_ttl = max(86400, int(getattr(self.config, "signal_dedup_minutes", 10) or 10) * 60)
        logger.info(
            "已熱更新會員端設定：lot=%s 馬丁=%s pause=%s 白名單=%s 黑名單=%s",
            tm.default_lot_size, tm.use_martingale, self.paused,
            self.source_whitelist, self.source_blacklist,
        )

    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        logger.info("paused=%s", self.paused)
        self._record_event("paused" if self.paused else "resumed", source="", summary="")

    def close_all(self, reason: str = "manual") -> Dict[str, int]:
        result = self.trade_manager.close_all(reason=reason)
        self._record_event(
            "close_all", source="", summary=f"撤 {result['cancelled']} 平 {result['closed']} 失 {result['failed']}"
        )
        return result

    def reset_martingale(self) -> None:
        self.trade_manager.reset_martingale()
        self._record_event("martingale_reset", source="", summary="馬丁手動重置 → 0")

    def _apply_overrides(self, ov: Dict[str, Any]) -> None:
        """把會員端 web UI 設定的下注參數覆蓋到 self.config（restart-time 一次性套用）。"""
        def _f(name, cast):
            raw = ov.get(name)
            if raw in (None, "", "null"):
                return
            try:
                setattr(self.config, name, cast(raw))
            except (TypeError, ValueError):
                logger.warning("override 失敗：%s=%r 無法 cast", name, raw)

        def _flist(name):
            raw = ov.get(name)
            if not raw:
                return
            try:
                if isinstance(raw, str):
                    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
                    parsed = [float(p) for p in parts]
                else:
                    parsed = [float(x) for x in raw]
                setattr(self.config, name, parsed)
            except (TypeError, ValueError):
                logger.warning("override list 失敗：%s=%r", name, raw)

        def _fbool(name):
            raw = ov.get(name)
            if raw in (None, ""):
                return
            setattr(self.config, name, str(raw).strip().lower() in {"1", "true", "yes", "on", "啟用"})

        _f("default_lot_size", float)
        _fbool("use_martingale")
        _f("martingale_multiplier", float)
        _f("martingale_max_level", int)
        _flist("martingale_lots")
        _flist("partial_close_ratios")
        _f("cancel_pending_after_seconds", int)
        _f("cancel_if_price_beyond_percent", float)
        _f("max_open_positions", int)
        _f("signal_dedup_minutes", int)

    @property
    def last_seq(self) -> int:
        return int(self.state.get("last_seq") or 0)

    def _mark_seq(self, seq: int) -> None:
        self.state["last_seq"] = max(self.last_seq, int(seq or 0))
        self.state["updated_at"] = time.time()
        _save_state(self.state_file, self.state)

    def run_cycle(self) -> int:
        count = 0
        # 暫停模式下不下單，但仍然推進 last_seq（避免恢復後一次補幾百筆舊單）
        # 這個語意是「按下暫停 = 跳過當下 hub feed」，恢復後從現在開始追訊號
        # TTL 清理（每輪都做一次，便宜）
        now = time.time()
        expired = [k for k, ts in self._processed_signals.items() if now - ts > self._processed_ttl]
        if expired:
            for k in expired:
                self._processed_signals.pop(k, None)
            self._persist_processed_signals()

        # 每輪 heartbeat + 訂單狀態變化掃描
        self._safe_heartbeat()
        self._scan_order_status_changes()

        for item in self.hub.signals_after(self.last_seq):
            seq = int(item.get("seq") or 0)
            if item.get("type") != "trade_signal":
                self._mark_seq(seq)
                continue

            payload = item.get("signal") or {}
            signal = _parsed_signal_from_payload(payload)
            source = str(item.get("source") or item.get("source_name") or "central")
            sig_summary = _signal_summary(signal)

            self._record_event("signal_received", seq=seq, source=source, summary=sig_summary)
            self._safe_ack(seq, "received", direction=signal.direction, message=sig_summary)

            if self.paused:
                logger.info("已暫停跟單，跳過 seq=%s: %s", seq, signal)
                self._record_event("skipped_paused", seq=seq, source=source, summary=sig_summary)
                self._safe_ack(seq, "skipped_paused", direction=signal.direction)
                self._mark_seq(seq)
                continue

            if not self._is_source_allowed(source):
                logger.info("來源 %s 不在許可名單內，跳過 seq=%s", source, seq)
                self._record_event("skipped_source", seq=seq, source=source, summary=sig_summary)
                self._safe_ack(seq, "skipped_source", direction=signal.direction)
                self._mark_seq(seq)
                continue

            if not _is_executable_signal(signal):
                logger.warning("invalid hub signal seq=%s: incomplete signal payload=%s", seq, payload)
                self._record_event("skipped_invalid", seq=seq, source=source, summary=sig_summary)
                self._safe_ack(seq, "skipped_invalid", direction=signal.direction)
                self._mark_seq(seq)
                continue

            # 持倉上限檢查
            max_open = int(getattr(self.config, "max_open_positions", 0) or 0)
            if max_open > 0:
                active = self.trade_manager.active_position_count()
                if active >= max_open:
                    logger.info("達持倉上限 %s/%s，跳過 seq=%s", active, max_open, seq)
                    self._record_event(
                        "skipped_limit", seq=seq, source=source, summary=sig_summary,
                        extra=f"active={active} max={max_open}",
                    )
                    self._safe_ack(
                        seq, "skipped_limit", direction=signal.direction,
                        message=f"持倉 {active}/{max_open}",
                    )
                    self._mark_seq(seq)
                    continue

            # 第二層保護：本機 MT5 端去重 — 同一筆 (來源,方向,SL,TPs) 不再下
            signal_key = _client_signal_key(signal, source)
            if signal_key in self._processed_signals:
                logger.info("本機已下單過此訊號，跳過 seq=%s: %s", seq, signal)
                self._record_event("skipped_dedup", seq=seq, source=source, summary=sig_summary)
                self._safe_ack(seq, "skipped_dedup", direction=signal.direction)
                self._mark_seq(seq)
                continue

            signal_id = self.trade_manager.submit_signal(
                signal,
                auto_execute=True,
                cancel_after_seconds=self.config.cancel_pending_after_seconds,
                cancel_if_price_beyond=self.config.cancel_if_price_beyond_percent,
                source_window=source,
            )
            order = self.trade_manager.get_order_status(signal_id)
            if order is not None and order.status == OrderStatus.FAILED:
                self._record_event("submit_failed", seq=seq, source=source, summary=sig_summary, signal_id=signal_id)
                self._safe_ack(
                    seq, "submit_failed", direction=signal.direction,
                    signal_id=signal_id, error="failed to write MT5 command",
                )
                raise RuntimeError(f"failed to write MT5 command for hub seq={seq}")

            self._processed_signals[signal_key] = time.time()
            self._persist_processed_signals()

            logger.info("submitted hub seq=%s as local signal %s: %s", seq, signal_id, signal)
            mt_lot = self.trade_manager.get_martingale_lot_size(source) if self.config.use_martingale else self.config.default_lot_size
            self._record_event(
                "submitted", seq=seq, source=source, summary=sig_summary,
                signal_id=signal_id, lot_size=mt_lot,
                martingale_level=(self.trade_manager.current_martingale_level if self.config.use_martingale else 0),
            )
            # 為 _scan_order_status_changes 留 metadata,之後 fill / close 才能 ack 對應 seq
            self._signal_meta[signal_id] = {
                "seq": seq,
                "source": source,
                "direction": signal.direction,
                "summary": sig_summary,
            }
            self._order_status_seen[signal_id] = getattr(order.status, "value", str(order.status)) if order else "sent"
            self._safe_ack(
                seq, "submitted", direction=signal.direction,
                signal_id=signal_id, lot_size=mt_lot,
            )
            self._mark_seq(seq)
            count += 1
        return count

    def run_forever(self, interval: float = 1.0) -> None:
        self.trade_manager.start()
        self._safe_register()
        logger.info("MT5 client agent running; client_id=%s last_seq=%s", self.client_id, self.last_seq)
        try:
            while True:
                try:
                    self.run_cycle()
                except (urllib.error.URLError, TimeoutError) as e:
                    logger.warning("hub connection failed: %s", e)
                except Exception as e:
                    logger.exception("agent cycle failed: %s", e)
                time.sleep(max(0.5, interval))
        except KeyboardInterrupt:
            logger.info("MT5 client agent stopped")
        finally:
            self.trade_manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local MT5 agent that executes central hub signals.")
    parser.add_argument("--hub-url", default=os.environ.get("COPY_TRADER_HUB_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.environ.get("COPY_TRADER_HUB_TOKEN", ""))
    parser.add_argument("--mt5-files-dir", default=os.environ.get("COPY_TRADER_MT5_FILES_DIR", ""))
    parser.add_argument("--state-file", default=os.environ.get("COPY_TRADER_AGENT_STATE", str(DATA_DIR / "central_client_state.json")))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("COPY_TRADER_AGENT_INTERVAL", "1.0")))
    parser.add_argument("--replay", action="store_true", help="Process existing hub history. Default starts from the current latest seq.")
    parser.add_argument("--log-level", default=os.environ.get("COPY_TRADER_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    hub = HubClient(args.hub_url, args.token)
    agent = MT5ClientAgent(hub, Path(args.state_file), mt5_files_dir=args.mt5_files_dir, replay=args.replay)
    agent.run_forever(interval=args.interval)


if __name__ == "__main__":
    main()

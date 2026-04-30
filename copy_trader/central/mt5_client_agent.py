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

    def health(self) -> Dict:
        return self._request("/health")

    def signals_after(self, after: int, limit: int = 50) -> List[Dict]:
        data = self._request(f"/signals?after={after}&limit={limit}")
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "hub_request_failed")
        return list(data.get("signals") or [])


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


def _client_signal_key(signal: ParsedSignal, source: str) -> Tuple:
    """會員端用的訊號去重 key — 與中央 _signal_key 同邏輯，避免同一訊號 MT5 端重下單。"""
    tps = tuple(round(float(tp), 2) for tp in (signal.take_profit or []) if tp is not None)
    return (
        str(source or ""),
        str(signal.direction or ""),
        round(float(signal.entry_price or 0), 2),
        bool(signal.is_market_order),
        round(float(signal.stop_loss or 0), 2),
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
    """JSON 反序列化後 list 還原成 _client_signal_key 的 tuple。"""
    return (
        str(raw[0]),
        str(raw[1]),
        float(raw[2]),
        bool(raw[3]),
        float(raw[4]),
        tuple(float(x) for x in (raw[5] or [])),
    )


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
        # TTL 清理（每輪都做一次，便宜）
        now = time.time()
        expired = [k for k, ts in self._processed_signals.items() if now - ts > self._processed_ttl]
        if expired:
            for k in expired:
                self._processed_signals.pop(k, None)
            self._persist_processed_signals()

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

            if not _is_executable_signal(signal):
                logger.warning("invalid hub signal seq=%s: incomplete signal payload=%s", seq, payload)
                self._record_event("skipped_invalid", seq=seq, source=source, summary=sig_summary)
                self._mark_seq(seq)
                continue

            # 第二層保護：本機 MT5 端去重 — 同一筆 (來源,方向,進場,SL,TPs) 不再下
            signal_key = _client_signal_key(signal, source)
            if signal_key in self._processed_signals:
                logger.info("本機已下單過此訊號，跳過 seq=%s: %s", seq, signal)
                self._record_event("skipped_dedup", seq=seq, source=source, summary=sig_summary)
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
            self._mark_seq(seq)
            count += 1
        return count

    def run_forever(self, interval: float = 1.0) -> None:
        self.trade_manager.start()
        logger.info("MT5 client agent running; last_seq=%s", self.last_seq)
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

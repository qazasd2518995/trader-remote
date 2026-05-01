"""
Central LINE signal collector.

Run this on the always-on signal computer. It uses the existing LINE clipboard
reader and regex parser, then publishes normalized signals to the hub.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from copy_trader.config import DATA_DIR, Config, load_config
from copy_trader.signal_capture.clipboard_reader import ClipboardReaderService, ClipboardWindow
from copy_trader.signal_capture.line_text_parser import LineMessage
from copy_trader.signal_parser.keyword_filter import is_potential_signal
from copy_trader.signal_parser.regex_parser import ParsedSignal, RegexSignalParser

logger = logging.getLogger(__name__)


def _window_label(window) -> str:
    return getattr(window, "display_name", "") or getattr(window, "window_name", "") or getattr(window, "name", "")


def _signal_key(signal: ParsedSignal, source: str) -> Tuple:
    tps = tuple(round(float(tp), 2) for tp in (signal.take_profit or []) if tp is not None)
    return (
        source,
        signal.direction,
        round(float(signal.entry_price or 0), 2),
        bool(signal.is_market_order),
        round(float(signal.stop_loss or 0), 2),
        tps,
    )


def _merge_signal(base: ParsedSignal, new: ParsedSignal) -> ParsedSignal:
    if not base.direction and new.direction:
        base.direction = new.direction
    if base.entry_price is None and new.entry_price is not None:
        base.entry_price = new.entry_price
    if not base.is_market_order and new.is_market_order:
        base.is_market_order = True
        base.entry_price = None
    if base.stop_loss is None and new.stop_loss is not None:
        base.stop_loss = new.stop_loss
    if not base.take_profit and new.take_profit:
        base.take_profit = list(new.take_profit)
    if base.lot_size is None and new.lot_size is not None:
        base.lot_size = new.lot_size
    base.is_valid = bool(base.direction)
    base.confidence = max(base.confidence or 0, new.confidence or 0)
    return base


def _is_complete(signal: ParsedSignal) -> bool:
    has_entry = signal.entry_price is not None or bool(signal.is_market_order)
    return bool(signal.is_valid and signal.direction and has_entry and signal.stop_loss and signal.take_profit)


def _signal_payload(signal: ParsedSignal) -> Dict:
    return {
        "symbol": signal.symbol or "XAUUSD",
        "direction": signal.direction,
        "entry_price": signal.entry_price,
        "is_market_order": bool(signal.is_market_order),
        "stop_loss": signal.stop_loss,
        "take_profit": list(signal.take_profit or []),
        "lot_size": signal.lot_size,
        "confidence": signal.confidence,
        "parse_method": signal.parse_method,
        "raw_text_summary": signal.raw_text_summary,
        "error": signal.error,
    }


class HubPublisher:
    def __init__(self, hub_url: str, token: str = "", timeout: float = 5.0):
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def publish(self, payload: Dict) -> Dict:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.hub_url}/signals",
            data=raw,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


class CentralSignalCollector:
    # 訊息時間超過這個值就不送（避免會員端剛開機補單時把幾小時前的舊訊號當新的送出去）
    MAX_MESSAGE_AGE_SECONDS = 30 * 60

    def __init__(
        self,
        config: Config,
        publisher: HubPublisher,
        copy_mode: str = "all",
        stale_seconds: Optional[float] = None,
    ):
        self.config = config
        self.publisher = publisher
        self.parser = RegexSignalParser()
        self.copy_mode = copy_mode
        self._pending: Dict[str, Dict] = {}
        self._processed: Dict[Tuple, float] = {}
        # 已發布訊號的 dedup TTL — 至少 24 小時（避免歷史訊息在使用者方便的時間視窗內重複下單）
        self._processed_ttl = max(86400, int(config.signal_dedup_minutes or 10) * 60)
        self._processed_path = DATA_DIR / "central_processed.json"
        self._load_processed()
        # 給 UI 看的「最近抓到的剪貼板原文」環形 buffer，central web_launcher 會讀
        self.latest_captures: Deque[Dict[str, Any]] = deque(maxlen=8)

        windows = [
            ClipboardWindow(
                name=w.name,
                window_name=w.window_name,
                display_name=_window_label(w),
                window_id=getattr(w, "window_id", None),
                screens=int(getattr(config, "clipboard_screens", 2) or 2),
                copy_mode=copy_mode,
            )
            for w in (config.capture_windows or [])
        ]
        if not windows:
            raise RuntimeError("no capture_windows configured")

        self.clipboard = ClipboardReaderService(
            windows,
            stale_seconds=float(stale_seconds if stale_seconds is not None else getattr(config, "clipboard_stale_seconds", 10.0) or 10.0),
        )
        logger.info("collector initialized: windows=%s copy_mode=%s", len(windows), copy_mode)

    def _cleanup(self) -> None:
        now = time.time()
        removed = 0
        for key, ts in list(self._processed.items()):
            if now - ts > self._processed_ttl:
                self._processed.pop(key, None)
                removed += 1
        if removed:
            self._save_processed()
        for source, item in list(self._pending.items()):
            if now - item.get("time", now) > 120:
                logger.warning("pending signal expired for %s", source)
                self._pending.pop(source, None)

    def run_cycle(self) -> int:
        self._cleanup()
        published = 0
        for cap in self.clipboard.capture_all():
            self._record_capture(cap)
            if not cap.ok:
                if cap.error:
                    logger.warning("capture failed for %s: %s", cap.display_name, cap.error)
                continue
            if not cap.new_messages:
                continue
            published += self._process_capture(cap)
        return published

    def _process_capture(self, cap) -> int:
        """處理某個 source 一輪內所有新訊息：只發布**最新一筆**完整訊號，舊的全部跳過。

        - 收集所有 messages 走完 pending merge 之後 complete 的訊號（依在 LINE 中出現的順序）
        - 從中取最後一筆（最新的）
        - 檢查 _processed dedup：若最新訊號已發布過 → 整批跳過、不退回更舊的
        - 否則發布最新訊號 → mark seen 全部 messages
        """
        source_name = cap.source_name
        source_display = cap.display_name

        ready: List[Tuple[LineMessage, ParsedSignal]] = []
        for msg in cap.new_messages:
            for sig in self._evaluate_message(msg, source_name, source_display):
                ready.append((msg, sig))

        if not ready:
            self.clipboard.mark_seen(source_name, cap.new_messages)
            return 0

        final_msg, final_signal = ready[-1]
        skipped = len(ready) - 1
        if skipped > 0:
            logger.info(
                "source=%s 本輪收到 %s 筆訊號，僅取最新一筆；跳過 %s 筆較舊的",
                source_display, len(ready), skipped,
            )

        final_key = _signal_key(final_signal, source_display)
        if final_key in self._processed:
            logger.info("最新訊號已下單過 → 整批跳過：%s", final_signal)
            self.clipboard.mark_seen(source_name, cap.new_messages)
            return 0

        try:
            self._publish_signal(final_signal, final_msg, source_name, source_display)
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            logger.warning("發布失敗，下一輪重試 source=%s: %s", source_display, exc)
            self.clipboard.force_retry(source_name)
            return 0
        except Exception as exc:
            logger.exception("發布訊號異常：%s", exc)
            self.clipboard.mark_seen(source_name, cap.new_messages)
            return 0

        self.clipboard.mark_seen(source_name, cap.new_messages)
        return 1

    def _evaluate_message(
        self, msg: LineMessage, source_name: str, source_display: str
    ) -> List[ParsedSignal]:
        """從一則 LINE 訊息抽出 complete 且有效的訊號（已 merge pending）。

        Pending 不完整的訊號會被存到 self._pending 等下一條補齊；只有達 complete 的才回傳。
        """
        body = (msg.body or "").strip()
        if len(body) < 5:
            return []

        # 第一層保護：訊息時間 >30 分鐘 → 不發布（避免重啟 / 補單把舊訊號當新單下）
        if msg.timestamp is not None:
            age = time.time() - msg.timestamp.timestamp()
            if age > self.MAX_MESSAGE_AGE_SECONDS:
                logger.info(
                    "訊息超過 %s 分鐘（%.0f 分鐘前），跳過：%s %r",
                    self.MAX_MESSAGE_AGE_SECONDS // 60, age / 60, source_display, body[:60],
                )
                return []

        has_pending = source_name in self._pending
        is_signal, reason = is_potential_signal(body)
        if not is_signal and not has_pending:
            logger.debug("filtered %s: %s", source_display, reason)
            return []

        logger.info("LINE message [%s %s %s]: %r", source_display, msg.time_str, msg.sender, body[:100])
        candidates = self._candidate_signals(body)
        if not candidates:
            return []

        ready: List[ParsedSignal] = []
        for signal in candidates:
            pending = self._pending.get(source_name)
            if pending:
                signal = _merge_signal(pending["signal"], signal)
            if not signal.is_valid:
                continue
            if not _is_complete(signal):
                self._pending[source_name] = {
                    "signal": signal,
                    "time": pending["time"] if pending else time.time(),
                    "raw_text": body,
                }
                logger.info(
                    "pending signal for %s: direction=%s entry=%s sl=%s tp=%s",
                    source_display, signal.direction, signal.entry_price, signal.stop_loss, signal.take_profit,
                )
                continue
            if source_name in self._pending:
                self._pending.pop(source_name, None)
            ready.append(signal)
        return ready

    def _publish_signal(
        self, signal: ParsedSignal, msg: LineMessage, source_name: str, source_display: str
    ) -> None:
        payload = {
            "type": "trade_signal",
            "source": source_display,
            "source_name": source_name,
            "sender": msg.sender,
            "message_time": msg.timestamp.isoformat() if msg.timestamp else msg.time_str,
            "captured_at": time.time(),
            "signal": _signal_payload(signal),
            "raw_text": (msg.body or "").strip(),
            "raw_message_key": list(msg.key),
        }
        response = self.publisher.publish(payload)
        if not response.get("ok"):
            raise RuntimeError(f"hub rejected signal: {response}")
        key = _signal_key(signal, source_display)
        self._processed[key] = time.time()
        self._save_processed()
        logger.info("published signal to hub: %s", signal)

    @staticmethod
    def _normalize_key(raw) -> Tuple:
        """JSON 反序列化後 list 還原成 _signal_key 的 tuple 形態，否則 dedup 比對對不上。"""
        return (
            str(raw[0]),
            str(raw[1]),
            float(raw[2]),
            bool(raw[3]),
            float(raw[4]),
            tuple(float(x) for x in (raw[5] or [])),
        )

    def _load_processed(self) -> None:
        path = self._processed_path
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            loaded = 0
            for entry in data.get("processed", []):
                try:
                    key = self._normalize_key(entry.get("key") or [])
                    ts = float(entry.get("ts", 0))
                    if now - ts > self._processed_ttl:
                        continue
                    self._processed[key] = ts
                    loaded += 1
                except Exception:
                    continue
            logger.info("已載入 %s 筆已發布訊號去重紀錄（%s）", loaded, path)
        except Exception as exc:
            logger.warning("讀取 dedup 紀錄失敗：%s", exc)

    def _save_processed(self) -> None:
        try:
            path = self._processed_path
            path.parent.mkdir(parents=True, exist_ok=True)
            items = [{"key": list(k), "ts": ts} for k, ts in self._processed.items()]
            with path.open("w", encoding="utf-8") as f:
                json.dump({"version": 1, "processed": items}, f, ensure_ascii=False)
        except Exception as exc:
            logger.warning("寫 dedup 紀錄失敗：%s", exc)

    def _record_capture(self, cap) -> None:
        # skipped 的視窗（沒搶焦點、沒實際複製）就不記錄，避免 UI 看到一堆空的
        if cap.skipped:
            return
        new_msgs = [
            {
                "time_str": getattr(msg, "time_str", "") or "",
                "sender": getattr(msg, "sender", "") or "",
                "body": (getattr(msg, "body", "") or "")[:1500],
            }
            for msg in (cap.new_messages or [])
        ]
        self.latest_captures.append({
            "captured_at": time.time(),
            "source": cap.display_name,
            "ok": bool(cap.ok),
            "error": cap.error or "",
            "raw_text": (cap.raw_text or "")[:4000],
            "new_messages": new_msgs,
            "new_count": len(new_msgs),
        })

    def _candidate_signals(self, body: str) -> List[ParsedSignal]:
        signals = self.parser.parse_all_latest(body)
        if signals:
            return signals
        sig = self.parser.parse_latest(body)
        return [sig] if sig.is_valid or sig.direction else []

    def _process_message(self, msg: LineMessage, source_name: str, source_display: str) -> int:
        body = (msg.body or "").strip()
        if len(body) < 5:
            return 0

        has_pending = source_name in self._pending
        is_signal, reason = is_potential_signal(body)
        if not is_signal and not has_pending:
            logger.debug("filtered %s: %s", source_display, reason)
            return 0

        logger.info("LINE message [%s %s %s]: %r", source_display, msg.time_str, msg.sender, body[:100])
        published = 0
        candidates = self._candidate_signals(body)
        if not candidates:
            return 0

        for signal in candidates:
            pending = self._pending.get(source_name)
            if pending:
                signal = _merge_signal(pending["signal"], signal)

            if not signal.is_valid:
                continue

            if not _is_complete(signal):
                self._pending[source_name] = {
                    "signal": signal,
                    "time": pending["time"] if pending else time.time(),
                    "raw_text": body,
                }
                logger.info("pending signal for %s: direction=%s entry=%s sl=%s tp=%s", source_display, signal.direction, signal.entry_price, signal.stop_loss, signal.take_profit)
                continue

            if source_name in self._pending:
                self._pending.pop(source_name, None)

            key = _signal_key(signal, source_display)
            if key in self._processed:
                logger.info("duplicate signal skipped: %s", signal)
                continue

            payload = {
                "type": "trade_signal",
                "source": source_display,
                "source_name": source_name,
                "sender": msg.sender,
                "message_time": msg.timestamp.isoformat() if msg.timestamp else msg.time_str,
                "captured_at": time.time(),
                "signal": _signal_payload(signal),
                "raw_text": body,
                "raw_message_key": list(msg.key),
            }
            response = self.publisher.publish(payload)
            if not response.get("ok"):
                raise RuntimeError(f"hub rejected signal: {response}")
            self._processed[key] = time.time()
            published += 1
            logger.info("published signal to hub: %s", signal)

        return published

    def run_forever(self, interval: float = 1.0) -> None:
        logger.info("collector running")
        try:
            while True:
                self.run_cycle()
                time.sleep(max(0.2, interval))
        except KeyboardInterrupt:
            logger.info("collector stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run central LINE clipboard signal collector.")
    parser.add_argument("--hub-url", default=os.environ.get("COPY_TRADER_HUB_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.environ.get("COPY_TRADER_HUB_TOKEN", ""))
    parser.add_argument("--copy-mode", choices=["all", "tail"], default=os.environ.get("COPY_TRADER_COPY_MODE", "all"))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("COPY_TRADER_COLLECTOR_INTERVAL", "1.0")))
    parser.add_argument("--stale-seconds", type=float, default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--log-level", default=os.environ.get("COPY_TRADER_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    publisher = HubPublisher(args.hub_url, args.token)
    collector = CentralSignalCollector(config, publisher, copy_mode=args.copy_mode, stale_seconds=args.stale_seconds)
    if args.once:
        collector.run_cycle()
    else:
        collector.run_forever(interval=args.interval)


if __name__ == "__main__":
    main()

"""
Central signal hub.

The hub is intentionally small and dependency-free: one always-on signal
computer posts normalized trading signals, and each client agent polls them.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

try:
    from copy_trader.config import DATA_DIR
except Exception:
    DATA_DIR = Path.cwd()

logger = logging.getLogger(__name__)


# 會員 heartbeat 超過這個秒數沒進來 → dashboard 顯示為離線
MEMBER_OFFLINE_AFTER = 90.0


class MemberRegistry:
    """會員清單 + heartbeat。記憶體為主,以 JSON 檔案持久化讓 hub 重啟不丟。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._members: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                members = data.get("members") or {}
                if isinstance(members, dict):
                    self._members = {
                        str(k): dict(v) for k, v in members.items() if isinstance(v, dict)
                    }
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("failed to load member registry %s: %s", self.path, e)

    def _save_locked(self) -> None:
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump({"members": self._members}, f, ensure_ascii=False)
            tmp.replace(self.path)
        except OSError as e:
            logger.warning("failed to persist member registry: %s", e)

    def upsert(self, client_id: str, name: str = "", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        client_id = str(client_id or "").strip()
        if not client_id:
            raise ValueError("client_id required")
        now = time.time()
        with self._lock:
            existing = self._members.get(client_id) or {}
            existing.setdefault("first_seen", now)
            existing["client_id"] = client_id
            if name:
                existing["name"] = str(name)
            existing.setdefault("name", existing.get("name") or client_id)
            existing["last_seen"] = now
            if meta:
                existing.setdefault("meta", {}).update({k: v for k, v in meta.items()})
            self._members[client_id] = existing
            self._save_locked()
            return dict(existing)

    def heartbeat(self, client_id: str, meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        client_id = str(client_id or "").strip()
        if not client_id:
            return None
        with self._lock:
            existing = self._members.get(client_id)
            if not existing:
                # 自動補一個——這樣即使會員還沒呼叫 register 就 heartbeat 也能正常顯示
                existing = {
                    "client_id": client_id,
                    "name": client_id,
                    "first_seen": time.time(),
                }
            existing["last_seen"] = time.time()
            if meta:
                existing.setdefault("meta", {}).update({k: v for k, v in meta.items()})
            self._members[client_id] = existing
            self._save_locked()
            return dict(existing)

    def list_all(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            out = []
            for m in self._members.values():
                row = dict(m)
                row["online"] = (now - float(row.get("last_seen") or 0)) < MEMBER_OFFLINE_AFTER
                out.append(row)
        out.sort(key=lambda x: float(x.get("last_seen") or 0), reverse=True)
        return out


class DispatchStore:
    """每筆訊號 → 每位會員的執行回報（received / submitted / filled / closed / failed / skipped_*）。

    用 (seq, client_id) 為主鍵。同一個 (seq, client_id) 可以多次 ack，會記錄狀態變化。
    最近狀態存成 _latest dict 給 dashboard 拉最終結果，全紀錄寫 JSONL 給 audit。
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # _latest[seq][client_id] = {client_id, name, status, message, ticket, error, ts}
        self._latest: Dict[int, Dict[str, Dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    seq = int(rec.get("seq") or 0)
                    cid = str(rec.get("client_id") or "")
                    if not seq or not cid:
                        continue
                    self._latest.setdefault(seq, {})[cid] = rec
        except OSError as e:
            logger.warning("failed to load dispatch store %s: %s", self.path, e)

    def record(self, seq: int, client_id: str, name: str, status: str, **fields: Any) -> Dict[str, Any]:
        seq = int(seq or 0)
        cid = str(client_id or "").strip()
        if not seq or not cid:
            raise ValueError("seq and client_id required")
        rec = {
            "seq": seq,
            "client_id": cid,
            "name": name or cid,
            "status": str(status or ""),
            "ts": time.time(),
        }
        for k, v in fields.items():
            if v is not None and v != "":
                rec[k] = v
        with self._lock:
            self._latest.setdefault(seq, {})[cid] = rec
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            except OSError as e:
                logger.warning("failed to append dispatch record: %s", e)
        return rec

    def for_seq(self, seq: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in (self._latest.get(int(seq) or 0) or {}).values()]

    def responded_client_ids(self, seq: int) -> set:
        with self._lock:
            return set((self._latest.get(int(seq) or 0) or {}).keys())

    def summary_for_seqs(self, seqs: List[int]) -> Dict[int, Dict[str, int]]:
        """每個 seq 的派發匯總: {seq: {total, submitted, filled, closed, failed, skipped}}."""
        out: Dict[int, Dict[str, int]] = {}
        with self._lock:
            for seq in seqs:
                rows = self._latest.get(int(seq) or 0) or {}
                summary = {"total": 0, "submitted": 0, "filled": 0, "closed": 0, "failed": 0, "skipped": 0}
                for r in rows.values():
                    summary["total"] += 1
                    st = str(r.get("status") or "")
                    if st == "submitted":
                        summary["submitted"] += 1
                    elif st == "filled":
                        summary["filled"] += 1
                    elif st == "closed":
                        summary["closed"] += 1
                    elif st in ("failed", "submit_failed"):
                        summary["failed"] += 1
                    elif st.startswith("skipped"):
                        summary["skipped"] += 1
                out[seq] = summary
        return out


class LogBuffer(logging.Handler):
    """環狀記錄體 — 給 dashboard 拉最近 N 條 hub log,支援增量輪詢。"""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self.capacity = capacity
        self._lock = threading.Lock()
        self._records: List[Dict[str, Any]] = []
        self._next_id = 1

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._lock:
            entry = {
                "id": self._next_id,
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "msg": msg,
            }
            self._next_id += 1
            self._records.append(entry)
            if len(self._records) > self.capacity:
                self._records = self._records[-self.capacity:]

    def list_after(self, after: int = 0, limit: int = 200) -> Dict[str, Any]:
        limit = max(1, min(int(limit or 200), 500))
        after = max(0, int(after or 0))
        with self._lock:
            records = [r for r in self._records if r["id"] > after][-limit:]
            cursor = self._records[-1]["id"] if self._records else 0
            return {"logs": [dict(r) for r in records], "cursor": cursor}


class SignalStore:
    """Append-only JSONL store for hub signals."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._records: List[Dict[str, Any]] = []
        self._latest_seq = 0
        self._load()

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._latest_seq

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    seq = int(record.get("seq") or 0)
                    if seq <= 0:
                        continue
                    self._records.append(record)
                    self._latest_seq = max(self._latest_seq, seq)
        except OSError as e:
            logger.warning("failed to load signal store %s: %s", self.path, e)

    def publish(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        record = dict(payload)
        with self._lock:
            self._latest_seq += 1
            record["seq"] = self._latest_seq
            record.setdefault("id", f"sig_{self._latest_seq}_{uuid.uuid4().hex[:8]}")
            record.setdefault("type", "trade_signal")
            record.setdefault("published_at", now)
            self._records.append(record)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        return record

    def list_after(self, after: int, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 100), 500))
        after = max(0, int(after or 0))
        with self._lock:
            return [r for r in self._records if int(r.get("seq") or 0) > after][:limit]


class HubRequestHandler(BaseHTTPRequestHandler):
    server_version = "CopyTraderHub/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    @property
    def store(self) -> SignalStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def members(self) -> MemberRegistry:
        return self.server.members  # type: ignore[attr-defined]

    @property
    def dispatch(self) -> DispatchStore:
        return self.server.dispatch  # type: ignore[attr-defined]

    @property
    def logbuf(self) -> "LogBuffer":
        return self.server.logbuf  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.token  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Hub-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        query_token = (qs.get("token") or [""])[0]
        header_token = self.headers.get("X-Hub-Token", "")
        auth = self.headers.get("Authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return self.token in {query_token, header_token, bearer}

    def _read_body(self) -> Optional[Dict[str, Any]]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return None
        if not isinstance(data, dict):
            self._send_json(400, {"ok": False, "error": "body_must_be_object"})
            return None
        return data

    def do_OPTIONS(self) -> None:
        self._send_json(200, {"ok": True})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {
                "ok": True,
                "latest_seq": self.store.latest_seq,
                "count": self.store.count,
                "auth_required": bool(self.token),
            })
            return

        if parsed.path == "/":
            if not self._authorized():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self._send_html(200, self._dashboard_html())
            return

        if parsed.path == "/signals":
            if not self._authorized():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            qs = parse_qs(parsed.query)
            after = int((qs.get("after") or ["0"])[0] or 0)
            limit = int((qs.get("limit") or ["100"])[0] or 100)
            records = self.store.list_after(after=after, limit=limit)
            self._send_json(200, {
                "ok": True,
                "latest_seq": self.store.latest_seq,
                "signals": records,
            })
            return

        if parsed.path == "/members":
            if not self._authorized():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self._send_json(200, {"ok": True, "members": self.members.list_all()})
            return

        if parsed.path == "/logs":
            if not self._authorized():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            qs = parse_qs(parsed.query)
            after = int((qs.get("after") or ["0"])[0] or 0)
            limit = int((qs.get("limit") or ["200"])[0] or 200)
            payload = self.logbuf.list_after(after=after, limit=limit)
            self._send_json(200, {"ok": True, **payload})
            return

        if parsed.path == "/dispatch/recent":
            if not self._authorized():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            qs = parse_qs(parsed.query)
            limit = max(1, min(int((qs.get("limit") or ["20"])[0] or 20), 200))
            recent = self.store.list_after(after=0, limit=10000)
            recent = recent[-limit:] if recent else []
            seqs = [int(r.get("seq") or 0) for r in recent]
            summary = self.dispatch.summary_for_seqs(seqs)
            all_members = self.members.list_all()
            registered = len(all_members)
            online = sum(1 for m in all_members if m.get("online"))
            online_ids = {m["client_id"] for m in all_members if m.get("online")}
            for r in recent:
                seq = int(r.get("seq") or 0)
                s = dict(summary.get(seq, {}))
                responded = self.dispatch.responded_client_ids(seq)
                # missing = 目前在線會員之中,沒有回過任何 ack 的人數
                s["registered"] = registered
                s["online"] = online
                s["missing"] = len(online_ids - responded)
                r["dispatch"] = s
            self._send_json(200, {"ok": True, "signals": list(reversed(recent))})
            return

        # /signals/{seq}/dispatch — 該訊號每個會員的回報 + 沒回報的離線/在線會員
        if parsed.path.startswith("/signals/") and parsed.path.endswith("/dispatch"):
            if not self._authorized():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                seq = int(parsed.path.split("/")[2])
            except (ValueError, IndexError):
                self._send_json(400, {"ok": False, "error": "bad_seq"})
                return
            acked = self.dispatch.for_seq(seq)
            acked_ids = {str(r.get("client_id") or "") for r in acked}
            synthetic: List[Dict[str, Any]] = []
            for m in self.members.list_all():
                cid = str(m.get("client_id") or "")
                if not cid or cid in acked_ids:
                    continue
                synthetic.append({
                    "seq": seq,
                    "client_id": cid,
                    "name": m.get("name") or cid,
                    "status": "no_ack" if m.get("online") else "offline",
                    "ts": 0,
                    "synthetic": True,
                })
            # 排序:已 ack 的依時間倒序,離線的放最後
            acked_sorted = sorted(acked, key=lambda r: float(r.get("ts") or 0), reverse=True)
            synthetic_sorted = sorted(synthetic, key=lambda r: r.get("status") == "offline")
            self._send_json(200, {
                "ok": True,
                "seq": seq,
                "dispatch": acked_sorted + synthetic_sorted,
            })
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if parsed.path == "/members/register":
            data = self._read_body()
            if data is None:
                return
            try:
                row = self.members.upsert(
                    client_id=str(data.get("client_id") or ""),
                    name=str(data.get("name") or ""),
                    meta={k: v for k, v in (data.get("meta") or {}).items()} if isinstance(data.get("meta"), dict) else None,
                )
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, "member": row})
            return

        if parsed.path == "/members/heartbeat":
            data = self._read_body()
            if data is None:
                return
            row = self.members.heartbeat(
                client_id=str(data.get("client_id") or ""),
                meta={k: v for k, v in (data.get("meta") or {}).items()} if isinstance(data.get("meta"), dict) else None,
            )
            if row is None:
                self._send_json(400, {"ok": False, "error": "client_id_required"})
                return
            self._send_json(200, {"ok": True, "member": row})
            return

        if parsed.path == "/ack":
            data = self._read_body()
            if data is None:
                return
            try:
                rec = self.dispatch.record(
                    seq=int(data.get("seq") or 0),
                    client_id=str(data.get("client_id") or ""),
                    name=str(data.get("name") or ""),
                    status=str(data.get("status") or ""),
                    message=str(data.get("message") or "") or None,
                    ticket=data.get("ticket"),
                    error=str(data.get("error") or "") or None,
                    signal_id=str(data.get("signal_id") or "") or None,
                    direction=str(data.get("direction") or "") or None,
                    lot_size=data.get("lot_size"),
                    profit=data.get("profit"),
                )
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            # ack 一律當作 heartbeat
            try:
                self.members.heartbeat(client_id=str(data.get("client_id") or ""))
            except Exception:
                pass
            self._send_json(200, {"ok": True, "record": rec})
            return

        if parsed.path == "/signals":
            data = self._read_body()
            if data is None:
                return

            items = data.get("signals")
            if items is None:
                items = [data]
            if not isinstance(items, list):
                self._send_json(400, {"ok": False, "error": "signals_must_be_list"})
                return

            published = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                published.append(self.store.publish(item))

            self._send_json(200, {
                "ok": True,
                "published": published,
                "latest_seq": self.store.latest_seq,
            })
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def _dashboard_html(self) -> str:
        return """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>XAU/Signal · Hub Central</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..900;1,9..144,300..900&family=JetBrains+Mono:wght@300;400;500;600;700&family=Noto+Sans+TC:wght@300;400;500;600;700&family=Noto+Serif+TC:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg: #0a0e14;
      --bg-elev: #0f141b;
      --bg-card: #131a22;
      --bg-soft: #181f28;
      --border: #1f2832;
      --border-strong: #2b3744;
      --text: #e6edf3;
      --text-dim: #8b96a3;
      --text-faint: #4d5864;
      --gold: #d4a24c;
      --gold-bright: #e8b964;
      --gold-soft: rgba(212, 162, 76, 0.10);
      --gold-line: rgba(212, 162, 76, 0.28);
      --green: #4ade80;
      --green-soft: rgba(74, 222, 128, 0.12);
      --red: #ef4444;
      --red-soft: rgba(239, 68, 68, 0.12);
      --amber: #f5b041;
      --amber-soft: rgba(245, 176, 65, 0.13);
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
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      letter-spacing: 0.01em;
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
      background: rgba(10,14,20,0.82);
      backdrop-filter: blur(14px) saturate(160%);
      -webkit-backdrop-filter: blur(14px) saturate(160%);
      border-bottom: 1px solid var(--border);
      padding: 14px 28px;
      display: grid; grid-template-columns: auto 1fr auto; gap: 36px; align-items: center;
    }
    .brand { display: flex; align-items: baseline; gap: 14px; }
    .brand-mark {
      font-family: var(--font-display);
      font-size: 26px; font-weight: 400; font-style: italic;
      letter-spacing: -0.025em; color: var(--gold);
      line-height: 1;
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
    .topbar-stats { display: flex; gap: 36px; justify-content: center; }
    .stat { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
    .stat-label {
      font-size: 9px; letter-spacing: 0.2em;
      color: var(--text-faint); text-transform: uppercase;
    }
    .stat-value {
      font-size: 15px; font-weight: 500; color: var(--text);
      font-variant-numeric: tabular-nums;
    }
    .stat-value.accent { color: var(--gold-bright); }
    .topbar-status { display: flex; align-items: center; gap: 14px; }
    .live-pill {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 5px 11px; border: 1px solid var(--border-strong);
      font-size: 9px; letter-spacing: 0.22em;
      text-transform: uppercase; color: var(--text-dim);
      background: var(--bg-card);
    }
    .live-dot {
      width: 6px; height: 6px; border-radius: 50%;
      background: var(--green); box-shadow: 0 0 9px var(--green);
      animation: pulse 1.6s ease-in-out infinite;
    }
    .live-dot.warn { background: var(--amber); box-shadow: 0 0 9px var(--amber); }
    .live-dot.err { background: var(--red); box-shadow: 0 0 9px var(--red); animation-duration: 0.7s; }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.3; transform: scale(0.6); }
    }
    .clock {
      font-variant-numeric: tabular-nums;
      color: var(--text-dim); font-size: 12px;
      letter-spacing: 0.04em; min-width: 200px; text-align: right;
    }
    /* ---------- ticker tape ---------- */
    .ticker {
      border-bottom: 1px solid var(--border);
      background: linear-gradient(180deg, var(--bg-elev), var(--bg));
      overflow: hidden; height: 32px; position: relative;
    }
    .ticker-track {
      display: flex; gap: 48px; padding: 8px 0;
      white-space: nowrap; font-size: 11px;
      animation: ticker-scroll 60s linear infinite;
      will-change: transform;
    }
    .ticker:hover .ticker-track { animation-play-state: paused; }
    @keyframes ticker-scroll {
      0% { transform: translateX(0); }
      100% { transform: translateX(-50%); }
    }
    .ticker-item {
      display: inline-flex; align-items: center; gap: 8px;
      color: var(--text-dim); letter-spacing: 0.05em;
    }
    .ticker-item .seq-tag {
      color: var(--text-faint); font-size: 10px;
    }
    .ticker-item .dir-up { color: var(--green); font-weight: 600; }
    .ticker-item .dir-dn { color: var(--red); font-weight: 600; }
    .ticker-item .price { color: var(--text); font-variant-numeric: tabular-nums; }
    .ticker-item .sep { color: var(--text-faint); }
    /* ---------- main ---------- */
    main {
      padding: 22px 28px;
      display: grid; grid-template-columns: minmax(0, 1fr) 360px;
      gap: 18px; max-width: 1640px; margin: 0 auto;
      align-items: start;
    }
    @media (max-width: 1100px) {
      main { grid-template-columns: 1fr; padding: 18px; }
      .topbar-stats { display: none; }
      .topbar { grid-template-columns: auto auto; gap: 16px; }
    }
    .panel {
      background: var(--bg-card);
      border: 1px solid var(--border);
      position: relative;
    }
    .panel::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
      background: linear-gradient(90deg, transparent, var(--gold-line), transparent);
      opacity: 0.6;
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
    .filter-bar { display: flex; gap: 0; }
    .filter-btn {
      background: transparent; border: none;
      color: var(--text-faint); font-family: var(--font-mono);
      font-size: 10px; letter-spacing: 0.2em; text-transform: uppercase;
      padding: 6px 12px; cursor: pointer; border-bottom: 1px solid transparent;
      transition: color 0.12s ease, border-color 0.12s ease;
    }
    .filter-btn:hover { color: var(--text-dim); }
    .filter-btn.active { color: var(--gold-bright); border-bottom-color: var(--gold); }
    /* ---------- signals table ---------- */
    .table-wrap { overflow-x: auto; }
    table.signals {
      width: 100%; border-collapse: collapse;
      font-variant-numeric: tabular-nums;
    }
    table.signals thead th {
      text-align: left; padding: 9px 14px;
      font-size: 9px; letter-spacing: 0.22em;
      text-transform: uppercase; color: var(--text-faint);
      font-weight: 500; border-bottom: 1px solid var(--border-strong);
      background: var(--bg-elev); position: sticky; top: 0; z-index: 1;
    }
    table.signals thead th.num { text-align: right; }
    table.signals tbody tr.signal-row {
      border-bottom: 1px solid var(--border);
      cursor: pointer;
      transition: background 0.12s ease;
    }
    table.signals tbody tr.signal-row:hover { background: var(--gold-soft); }
    table.signals tbody tr.signal-row.flash {
      animation: flash-in 1.4s ease-out;
    }
    @keyframes flash-in {
      0% { background: rgba(212,162,76,0.22); transform: translateX(-3px); }
      40% { background: rgba(212,162,76,0.18); transform: translateX(0); }
      100% { background: transparent; }
    }
    table.signals td {
      padding: 11px 14px; font-size: 12px; color: var(--text);
      vertical-align: middle;
    }
    table.signals td.num { text-align: right; }
    .seq {
      color: var(--text-dim); font-size: 11px;
      font-variant-numeric: tabular-nums;
    }
    .seq::before { content: '#'; color: var(--text-faint); opacity: 0.6; margin-right: 1px; }
    .dir-pill {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 3px 9px 3px 7px;
      font-size: 10px; font-weight: 600;
      letter-spacing: 0.18em; text-transform: uppercase;
    }
    .dir-pill.buy {
      background: var(--green-soft); color: var(--green);
      border: 1px solid rgba(74,222,128,0.32);
    }
    .dir-pill.sell {
      background: var(--red-soft); color: var(--red);
      border: 1px solid rgba(239,68,68,0.32);
    }
    .dir-pill.muted {
      background: transparent; color: var(--text-faint);
      border: 1px solid var(--border-strong);
    }
    .dir-arrow { width: 0; height: 0; border-left: 4px solid transparent; border-right: 4px solid transparent; }
    .dir-pill.buy .dir-arrow { border-bottom: 6px solid var(--green); }
    .dir-pill.sell .dir-arrow { border-top: 6px solid var(--red); }
    .price { color: var(--text); font-variant-numeric: tabular-nums; }
    .price-mkt {
      color: var(--gold); font-size: 10px; letter-spacing: 0.18em;
      padding: 1px 5px; border: 1px solid var(--gold-line);
    }
    .price-list { color: var(--text-dim); font-size: 11px; }
    .source-cell {
      color: var(--text-dim); font-size: 11px;
      max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .ts-cell {
      color: var(--text-faint); font-size: 11px;
      font-variant-numeric: tabular-nums; white-space: nowrap;
    }
    .dispatch-summary { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
    .ds-pill {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 1px 6px; font-size: 10px; letter-spacing: 0.06em;
      font-variant-numeric: tabular-nums;
    }
    .ds-pill.ok { background: var(--green-soft); color: var(--green); }
    .ds-pill.bad { background: var(--red-soft); color: var(--red); }
    .ds-pill.warn { background: var(--amber-soft); color: var(--amber); }
    .ds-pill.neutral { background: var(--blue-soft); color: var(--blue); }
    .ds-pill.muted {
      background: transparent; color: var(--text-faint);
      border: 1px solid var(--border);
    }
    .ds-pill .lbl { letter-spacing: 0.18em; text-transform: uppercase; opacity: 0.85; font-size: 9px; }
    /* expand row */
    .expand-row > td { padding: 0 !important; background: var(--bg-elev); border-bottom: 1px solid var(--border); }
    .expand-content {
      padding: 14px 18px 16px 38px;
      border-left: 2px solid var(--gold);
      background: linear-gradient(90deg, var(--gold-soft) 0%, transparent 9%);
    }
    .expand-title {
      font-size: 9px; letter-spacing: 0.22em; text-transform: uppercase;
      color: var(--gold); margin-bottom: 10px;
    }
    table.dispatch-detail { width: 100%; border-collapse: collapse; }
    table.dispatch-detail th, table.dispatch-detail td {
      padding: 6px 10px; font-size: 11px; text-align: left;
      border-bottom: 1px dashed var(--border);
    }
    table.dispatch-detail th {
      color: var(--text-faint); font-size: 9px;
      letter-spacing: 0.18em; text-transform: uppercase; font-weight: 500;
    }
    table.dispatch-detail tbody tr:last-child td { border-bottom: none; }
    .status-tag {
      display: inline-block; padding: 1px 6px;
      font-size: 10px; letter-spacing: 0.06em;
      font-variant-numeric: tabular-nums;
    }
    .status-tag.filled, .status-tag.submitted { background: var(--green-soft); color: var(--green); }
    .status-tag.failed, .status-tag.submit_failed, .status-tag.error { background: var(--red-soft); color: var(--red); }
    .status-tag.closed { background: var(--blue-soft); color: var(--blue); }
    .status-tag.skipped, .status-tag.skipped_dedup, .status-tag.skipped_invalid, .status-tag.skipped_paused, .status-tag.skipped_source, .status-tag.skipped_limit, .status-tag.received { background: var(--amber-soft); color: var(--amber); }
    .status-tag.no_ack { background: var(--amber-soft); color: var(--amber); border: 1px dashed var(--amber); padding: 0 5px; }
    .status-tag.offline { background: transparent; color: var(--text-faint); border: 1px solid var(--border-strong); padding: 0 5px; }
    .dispatch-detail tr.synthetic td { opacity: 0.78; }
    .dispatch-detail tr.synthetic.offline td { opacity: 0.55; }
    /* ---------- members panel ---------- */
    .members { padding: 4px 0; max-height: calc(100vh - 240px); overflow-y: auto; }
    .member {
      display: grid; grid-template-columns: auto 1fr auto;
      align-items: center; gap: 12px;
      padding: 12px 18px; border-bottom: 1px solid var(--border);
      transition: background 0.12s ease;
    }
    .member:hover { background: var(--bg-soft); }
    .member-status {
      width: 8px; height: 8px; border-radius: 50%;
    }
    .member-status.online { background: var(--green); box-shadow: 0 0 9px var(--green); }
    .member-status.offline { background: var(--text-faint); }
    .member-name {
      font-size: 13px; color: var(--text);
      max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .member-id {
      display: block; font-size: 10px; color: var(--text-faint);
      margin-top: 2px; letter-spacing: 0.04em;
      max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .member-time {
      font-size: 10px; color: var(--text-dim);
      font-variant-numeric: tabular-nums; letter-spacing: 0.04em;
      text-align: right; white-space: nowrap;
    }
    .empty-state {
      padding: 32px 18px; text-align: center;
      color: var(--text-faint); font-size: 10px;
      letter-spacing: 0.22em; text-transform: uppercase;
    }
    /* ---------- service log ---------- */
    .log-section {
      padding: 0 28px 22px;
      max-width: 1640px; margin: 0 auto;
    }
    .log-panel {
      background: var(--bg-card);
      border: 1px solid var(--border);
      position: relative;
    }
    .log-panel::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
      background: linear-gradient(90deg, transparent, var(--gold-line), transparent);
      opacity: 0.55;
    }
    .log-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 14px 18px; border-bottom: 1px solid var(--border); gap: 12px;
    }
    .log-meta {
      font-size: 10px; color: var(--text-faint);
      letter-spacing: 0.18em; text-transform: uppercase;
    }
    .log-controls { display: flex; gap: 4px; align-items: center; }
    .log-controls .filter-btn { padding: 4px 10px; }
    .logs-body {
      height: 320px; overflow: auto;
      background: #06090d; padding: 14px 16px;
      font-family: var(--font-mono); font-size: 11.5px;
      line-height: 1.55;
    }
    .logs-body::before {
      content: '$ tail -f hub.log';
      display: block; color: var(--gold);
      font-size: 10px; letter-spacing: 0.16em;
      text-transform: uppercase; margin-bottom: 10px; opacity: 0.7;
    }
    .log-line {
      display: grid; grid-template-columns: 70px 50px 130px 1fr;
      gap: 10px; padding: 2px 0;
      align-items: baseline; word-break: break-word;
    }
    .log-line:hover { background: rgba(255,255,255,0.025); }
    .log-line .lv { font-size: 10px; letter-spacing: 0.16em; text-align: center; padding: 0 4px; }
    .log-line.INFO .lv { color: var(--blue); }
    .log-line.WARNING .lv { color: var(--amber); }
    .log-line.ERROR .lv { color: var(--red); }
    .log-line.CRITICAL .lv { color: var(--red); background: var(--red-soft); }
    .log-line.DEBUG .lv { color: var(--text-faint); }
    .log-line .ts { color: var(--text-faint); font-variant-numeric: tabular-nums; }
    .log-line .src { color: var(--text-dim); font-size: 11px; opacity: 0.75; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .log-line .msg { color: #b8d4c4; }
    .log-line.WARNING .msg { color: #e3c08c; }
    .log-line.ERROR .msg { color: #f5b5b5; }
    .logs-empty {
      color: var(--text-faint); font-size: 10px;
      letter-spacing: 0.22em; text-transform: uppercase;
      padding: 20px 0;
    }
    /* ---------- footer ---------- */
    .footer {
      padding: 14px 28px; border-top: 1px solid var(--border);
      display: flex; justify-content: space-between; gap: 16px;
      font-size: 10px; letter-spacing: 0.18em;
      text-transform: uppercase; color: var(--text-faint);
      flex-wrap: wrap;
    }
    .footer-key { color: var(--text-dim); margin-right: 6px; }
    /* scrollbar */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border-strong); }
    ::-webkit-scrollbar-thumb:hover { background: var(--text-faint); }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <span class="brand-mark">XAU/Signal</span>
      <span class="brand-meta">Hub · Central</span>
    </div>
    <div class="topbar-stats">
      <div class="stat">
        <span class="stat-label">Latest Seq</span>
        <span class="stat-value accent" id="stat-seq">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">Stream Size</span>
        <span class="stat-value" id="stat-today">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">Members</span>
        <span class="stat-value" id="stat-members">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">Last Signal</span>
        <span class="stat-value" id="stat-last">—</span>
      </div>
    </div>
    <div class="topbar-status">
      <span class="live-pill"><span class="live-dot" id="live-dot"></span><span id="live-text">Live</span></span>
      <span class="clock" id="clock">—</span>
    </div>
  </div>
  <div class="ticker">
    <div class="ticker-track" id="ticker-track">
      <span class="ticker-item"><span class="sep">···</span> awaiting signal stream <span class="sep">···</span></span>
    </div>
  </div>
  <main>
    <div class="panel">
      <div class="panel-header">
        <div>
          <span class="panel-title">Signal Stream<span class="panel-title-tag">Live</span></span>
        </div>
        <div class="filter-bar">
          <button class="filter-btn active" data-filter="all">All</button>
          <button class="filter-btn" data-filter="buy">Buy</button>
          <button class="filter-btn" data-filter="sell">Sell</button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="signals">
          <thead>
            <tr>
              <th style="width: 60px;">Seq</th>
              <th style="width: 130px;">Source</th>
              <th style="width: 80px;">Side</th>
              <th class="num" style="width: 90px;">Entry</th>
              <th class="num" style="width: 80px;">SL</th>
              <th>TP</th>
              <th style="width: 280px;">Dispatch</th>
              <th style="width: 90px;">Time</th>
            </tr>
          </thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
      <div id="empty-rows" class="empty-state" style="display:none;">No signals yet · standing by</div>
    </div>
    <div class="panel">
      <div class="panel-header">
        <div>
          <span class="panel-title">Members</span>
        </div>
        <span class="panel-meta" id="member-summary">—</span>
      </div>
      <div class="members" id="members">
        <div class="empty-state">Loading roster…</div>
      </div>
    </div>
  </main>
  <div class="log-section">
    <div class="log-panel">
      <div class="log-header">
        <span class="panel-title">Service Log<span class="panel-title-tag">tail · hub</span></span>
        <div class="log-controls">
          <button class="filter-btn log-filter active" data-lv="ALL">All</button>
          <button class="filter-btn log-filter" data-lv="INFO">Info</button>
          <button class="filter-btn log-filter" data-lv="WARNING">Warn</button>
          <button class="filter-btn log-filter" data-lv="ERROR">Error</button>
          <span class="log-meta" id="log-meta" style="margin-left: 12px;">—</span>
        </div>
      </div>
      <div class="logs-body" id="logs-body"><div class="logs-empty">awaiting log stream…</div></div>
    </div>
  </div>
  <div class="footer">
    <span><span class="footer-key">Protocol</span>CopyTraderHub v1.0</span>
    <span><span class="footer-key">Hint</span>click row → inspect dispatch</span>
    <span id="footer-clock">—</span>
  </div>
  <script>
    const params = new URLSearchParams(location.search);
    const token = params.get("token") || "";
    const tokenQs = token ? `&token=${encodeURIComponent(token)}` : "";
    const tokenQs2 = token ? `?token=${encodeURIComponent(token)}` : "";
    const $ = (id) => document.getElementById(id);
    const expanded = new Set();
    const seenSeqs = new Set();
    let firstLoad = true;
    let currentFilter = "all";
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    function fmtPrice(v, d) {
      if (v === null || v === undefined || v === "") return '—';
      const n = Number(v);
      if (!isFinite(n)) return esc(String(v));
      return n.toFixed(d == null ? 2 : d);
    }
    function fmtTime(ts) {
      if (!ts) return '—';
      return new Date(ts * 1000).toLocaleTimeString("zh-TW", { hour12: false });
    }
    function fmtRel(ts) {
      if (!ts) return '—';
      const diff = (Date.now() / 1000) - ts;
      if (diff < 5) return 'now';
      if (diff < 60) return Math.floor(diff) + 's';
      if (diff < 3600) return Math.floor(diff / 60) + 'm';
      if (diff < 86400) return Math.floor(diff / 3600) + 'h';
      return Math.floor(diff / 86400) + 'd';
    }
    function dispatchSummary(d) {
      d = d || {};
      const parts = [];
      if (d.filled) parts.push('<span class="ds-pill ok"><span class="lbl">fill</span> ' + d.filled + '</span>');
      if (d.submitted) parts.push('<span class="ds-pill ok"><span class="lbl">sent</span> ' + d.submitted + '</span>');
      if (d.closed) parts.push('<span class="ds-pill neutral"><span class="lbl">close</span> ' + d.closed + '</span>');
      if (d.failed) parts.push('<span class="ds-pill bad"><span class="lbl">fail</span> ' + d.failed + '</span>');
      if (d.skipped) parts.push('<span class="ds-pill warn"><span class="lbl">skip</span> ' + d.skipped + '</span>');
      if (d.missing) parts.push('<span class="ds-pill bad" title="online member did not ack"><span class="lbl">miss</span> ' + d.missing + '</span>');
      if (d.total || d.online) {
        const totalTxt = (d.total || 0) + '/' + (d.online || 0);
        parts.push('<span class="ds-pill muted" title="acked / online">' + totalTxt + '</span>');
      }
      if (!parts.length) return '<span class="ds-pill muted"><span class="lbl">no members</span></span>';
      return '<div class="dispatch-summary">' + parts.join('') + '</div>';
    }
    function clockTick() {
      const t = new Date();
      const local = t.toLocaleTimeString("zh-TW", { hour12: false });
      const utc = t.toISOString().slice(11, 19);
      $('clock').textContent = local + ' · ' + utc + ' UTC';
      $('footer-clock').textContent = t.toISOString().slice(0, 19).replace('T', ' ') + 'Z';
    }
    setInterval(clockTick, 1000);
    clockTick();
    document.querySelectorAll('.filter-btn').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentFilter = btn.dataset.filter;
        applyFilter();
      };
    });
    function applyFilter() {
      document.querySelectorAll('tr.signal-row').forEach(tr => {
        const dir = tr.dataset.dir;
        const visible = currentFilter === 'all' || currentFilter === dir;
        tr.style.display = visible ? '' : 'none';
        const seq = tr.dataset.seq;
        const ex = $('expand-' + seq);
        if (ex) ex.style.display = (visible && expanded.has(Number(seq))) ? '' : 'none';
      });
    }
    function renderTicker(signals) {
      const slice = signals.slice(0, 8);
      if (!slice.length) return;
      const items = slice.map(item => {
        const sig = item.signal || {};
        const dir = String(sig.direction || '').toUpperCase();
        const dirCls = dir === 'BUY' ? 'dir-up' : (dir === 'SELL' ? 'dir-dn' : '');
        const entry = sig.is_market_order ? 'MKT' : (sig.entry_price != null ? Number(sig.entry_price).toFixed(2) : '—');
        const sl = sig.stop_loss != null ? Number(sig.stop_loss).toFixed(2) : '—';
        return '<span class="ticker-item">' +
          '<span class="seq-tag">#' + item.seq + '</span>' +
          '<span class="' + dirCls + '">' + esc(dir) + '</span>' +
          '<span class="price">' + entry + '</span>' +
          '<span class="sep">·</span>' +
          '<span>SL ' + sl + '</span>' +
          '<span class="sep">·</span>' +
          '<span>' + esc((item.source || '').slice(0, 24)) + '</span>' +
          '<span class="sep">◆</span>' +
          '</span>';
      }).join('');
      $('ticker-track').innerHTML = items + items;
    }
    async function pollMembers() {
      const res = await fetch('/members' + tokenQs2);
      const data = await res.json();
      if (!data.ok) return;
      const members = data.members || [];
      const onlineCount = members.filter(m => m.online).length;
      $('member-summary').textContent = onlineCount + ' online · ' + members.length + ' total';
      $('stat-members').textContent = onlineCount + ' / ' + members.length;
      const root = $('members');
      if (!members.length) {
        root.innerHTML = '<div class="empty-state">No members registered</div>';
        return;
      }
      root.innerHTML = members.map(m => {
        const cid = esc(m.client_id || '');
        const name = esc(m.name || m.client_id || 'unnamed');
        const last = m.last_seen ? fmtRel(m.last_seen) : '—';
        return '<div class="member">' +
          '<span class="member-status ' + (m.online ? 'online' : 'offline') + '"></span>' +
          '<div><span class="member-name">' + name + '</span><span class="member-id">' + cid + '</span></div>' +
          '<span class="member-time">' + last + '</span>' +
          '</div>';
      }).join('');
    }
    async function pollSignals() {
      const res = await fetch('/dispatch/recent?limit=80' + tokenQs);
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'request_failed');
      const signals = data.signals || [];
      $('stat-seq').textContent = signals.length ? signals[0].seq : '—';
      $('stat-today').textContent = signals.length || '—';
      $('stat-last').textContent = signals.length ? fmtRel(signals[0].published_at) : '—';
      $('live-dot').classList.remove('warn', 'err');
      $('live-text').textContent = 'Live';
      renderTicker(signals);
      const rows = $('rows');
      const empty = $('empty-rows');
      if (!signals.length) {
        rows.innerHTML = '';
        empty.style.display = 'block';
        return;
      }
      empty.style.display = 'none';
      rows.innerHTML = '';
      for (const item of signals) {
        const sig = item.signal || {};
        const seq = Number(item.seq || 0);
        const dir = String(sig.direction || '').toLowerCase();
        const isNew = !seenSeqs.has(seq);
        if (isNew) seenSeqs.add(seq);
        const tr = document.createElement('tr');
        tr.className = 'signal-row' + (isNew && !firstLoad ? ' flash' : '');
        tr.dataset.seq = seq;
        tr.dataset.dir = dir;
        const tps = (sig.take_profit || []).map(tp => fmtPrice(tp)).join(' · ');
        const entryHtml = sig.is_market_order
          ? '<span class="price-mkt">MKT</span>'
          : '<span class="price">' + fmtPrice(sig.entry_price) + '</span>';
        tr.innerHTML =
          '<td><span class="seq">' + seq + '</span></td>' +
          '<td><span class="source-cell" title="' + esc(item.source || '') + '">' + esc(item.source || '—') + '</span></td>' +
          '<td><span class="dir-pill ' + (dir || 'muted') + '"><span class="dir-arrow"></span>' + esc(sig.direction || '—') + '</span></td>' +
          '<td class="num">' + entryHtml + '</td>' +
          '<td class="num"><span class="price">' + fmtPrice(sig.stop_loss) + '</span></td>' +
          '<td><span class="price-list">' + (tps || '—') + '</span></td>' +
          '<td>' + dispatchSummary(item.dispatch) + '</td>' +
          '<td><span class="ts-cell">' + fmtTime(item.published_at) + '</span></td>';
        tr.onclick = () => toggleExpand(seq);
        rows.appendChild(tr);
        if (expanded.has(seq)) await renderExpand(seq);
      }
      // 首次載入時自動展開最新一筆訊號,讓使用者立刻看到派發明細
      if (firstLoad && signals.length) {
        const latestSeq = Number(signals[0].seq || 0);
        if (latestSeq && !expanded.has(latestSeq)) {
          expanded.add(latestSeq);
          await renderExpand(latestSeq);
        }
      }
      firstLoad = false;
      applyFilter();
    }
    async function toggleExpand(seq) {
      seq = Number(seq);
      if (expanded.has(seq)) {
        expanded.delete(seq);
        const ex = $('expand-' + seq);
        if (ex) ex.remove();
        return;
      }
      expanded.add(seq);
      await renderExpand(seq);
    }
    async function renderExpand(seq) {
      const res = await fetch('/signals/' + seq + '/dispatch' + tokenQs2);
      const data = await res.json();
      const dispatch = (data && data.ok && data.dispatch) || [];
      const existing = $('expand-' + seq);
      if (existing) existing.remove();
      const baseRow = document.querySelector('tr.signal-row[data-seq="' + seq + '"]');
      if (!baseRow) return;
      const tr = document.createElement('tr');
      tr.id = 'expand-' + seq;
      tr.className = 'expand-row';
      const inner = dispatch.length === 0
        ? '<div style="color: var(--text-faint); font-size: 11px; padding: 6px 0;">no members registered…</div>'
        : '<table class="dispatch-detail"><thead><tr><th>Member</th><th>Status</th><th>Ticket</th><th>Note</th><th>Time</th></tr></thead><tbody>' +
          dispatch.map(d => {
            const status = String(d.status || '').toLowerCase();
            const synthetic = d.synthetic ? (status === 'offline' ? 'synthetic offline' : 'synthetic') : '';
            const noteText = d.error || d.message || (status === 'no_ack' ? 'online but no ack received' : (status === 'offline' ? 'member offline at dispatch time' : '—'));
            return '<tr class="' + synthetic + '">' +
              '<td>' + esc(d.name || d.client_id) + '</td>' +
              '<td><span class="status-tag ' + esc(status.replace(/[^a-z_]/gi, '')) + '">' + esc(d.status || '—') + '</span></td>' +
              '<td>' + (d.ticket ? '#' + esc(d.ticket) : '—') + '</td>' +
              '<td style="color: var(--text-dim);">' + esc(noteText) + '</td>' +
              '<td><span class="ts-cell">' + (d.ts ? fmtTime(d.ts) : '—') + '</span></td>' +
            '</tr>';
          }).join('') +
          '</tbody></table>';
      tr.innerHTML = '<td colspan="8"><div class="expand-content"><div class="expand-title">▸ Dispatch · seq ' + seq + '</div>' + inner + '</div></td>';
      baseRow.parentNode.insertBefore(tr, baseRow.nextSibling);
    }
    function setError(err) {
      $('live-dot').classList.remove('warn');
      $('live-dot').classList.add('err');
      $('live-text').textContent = 'Offline';
    }
    // ---------- service log polling ----------
    let logCursor = 0;
    let logBuffer = [];
    let logFilter = 'ALL';
    const LOG_MAX = 500;
    document.querySelectorAll('.log-filter').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('.log-filter').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        logFilter = btn.dataset.lv;
        renderLogs(true);
      };
    });
    function renderLogs(rebuild) {
      const body = $('logs-body');
      if (!body) return;
      const visible = logBuffer.filter(r => logFilter === 'ALL' || r.level === logFilter);
      $('log-meta').textContent = visible.length + ' / ' + logBuffer.length + ' lines';
      if (!visible.length) {
        body.innerHTML = '<div class="logs-empty">no log entries</div>';
        return;
      }
      const stickToBottom = (body.scrollHeight - body.scrollTop - body.clientHeight) < 40;
      body.innerHTML = visible.map(r => {
        const ts = new Date(r.ts * 1000).toLocaleTimeString('zh-TW', { hour12: false });
        const shortName = (r.name || '').split('.').slice(-2).join('.');
        return '<div class="log-line ' + esc(r.level) + '">' +
          '<span class="ts">' + ts + '</span>' +
          '<span class="lv">' + esc(r.level) + '</span>' +
          '<span class="src" title="' + esc(r.name || '') + '">' + esc(shortName) + '</span>' +
          '<span class="msg">' + esc(r.msg.replace(/^.*?:\\s*/, '')) + '</span>' +
        '</div>';
      }).join('');
      if (stickToBottom || rebuild) body.scrollTop = body.scrollHeight;
    }
    async function pollLogs() {
      const res = await fetch('/logs?after=' + logCursor + '&limit=200' + tokenQs);
      const data = await res.json();
      if (!data.ok) return;
      const newLogs = data.logs || [];
      if (newLogs.length) {
        logBuffer = logBuffer.concat(newLogs);
        if (logBuffer.length > LOG_MAX) logBuffer = logBuffer.slice(-LOG_MAX);
        logCursor = data.cursor || logCursor;
        renderLogs(false);
      } else if (data.cursor && logCursor === 0) {
        logCursor = data.cursor;
      }
    }
    async function tick() {
      try { await pollSignals(); } catch (e) { setError(e.message); }
      try { await pollMembers(); } catch (e) {}
      try { await pollLogs(); } catch (e) {}
    }
    tick();
    setInterval(tick, 2500);
  </script>
</body>
</html>"""


class HubHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple,
        handler_class: type,
        store: SignalStore,
        members: MemberRegistry,
        dispatch: DispatchStore,
        token: str,
        logbuf: "LogBuffer",
    ):
        super().__init__(server_address, handler_class)
        self.store = store
        self.members = members
        self.dispatch = dispatch
        self.token = token
        self.logbuf = logbuf


def run_server(host: str, port: int, store_path: Path, token: str = "") -> None:
    store = SignalStore(store_path)
    members_path = store_path.parent / "central_hub_members.json"
    dispatch_path = store_path.parent / "central_hub_dispatch.jsonl"
    members = MemberRegistry(members_path)
    dispatch = DispatchStore(dispatch_path)
    logbuf = LogBuffer(capacity=500)
    logbuf.setLevel(logging.INFO)
    logbuf.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"))
    root_logger = logging.getLogger()
    root_logger.addHandler(logbuf)
    # 確保 hub 日誌能流到 buffer — 若 root 還停留在 WARNING (Python 預設,且呼叫端沒呼 basicConfig)
    # 就降到 INFO,否則 dashboard 的 Service Log 會永遠空白
    if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)
    httpd = HubHTTPServer((host, port), HubRequestHandler, store, members, dispatch, token, logbuf)
    logger.info(
        "signal hub listening on http://%s:%s (store=%s, members=%s, dispatch=%s)",
        host, port, store_path, members_path, dispatch_path,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("signal hub stopped")
    finally:
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the central copy-trader signal hub.")
    parser.add_argument("--host", default=os.environ.get("COPY_TRADER_HUB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("COPY_TRADER_HUB_PORT", "8765")))
    parser.add_argument("--store", default=os.environ.get("COPY_TRADER_HUB_STORE", str(DATA_DIR / "central_hub_signals.jsonl")))
    parser.add_argument("--token", default=os.environ.get("COPY_TRADER_HUB_TOKEN", ""))
    parser.add_argument("--log-level", default=os.environ.get("COPY_TRADER_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_server(args.host, args.port, Path(args.store), args.token)


if __name__ == "__main__":
    main()

"""Anima Brain memory provider for xiaozhi-esp32-server (Python 3.10 compatible HTTP client).

- Sub-15ms read path (/xiaozhi/recall) before streaming TTS voice reply
- Durable local SQLite outbox (data/anima_outbox.sqlite) + single worker with
  exponential backoff retry and request_id idempotency for /xiaozhi/ingest
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable

import httpx
import mei_emotion_router
import xz_common
from core.providers.memory.base import MemoryProviderBase, logger

TAG = __name__

_GLOBAL_CONFIG = {
    "service_url": "http://anima-brain:18787",
    "character_id": "xiaozhi",
    "token_file": "/opt/xiaozhi-esp32-server/data/anima/studio_token.txt",
    "outbox_db": "/opt/xiaozhi-esp32-server/data/anima_outbox.sqlite",
}

_SENSITIVE_ERR_RE = re.compile(
    r"(?i)(Bearer\s+[A-Za-z0-9._\-~+/=]+|sk-[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{16,})"
)


def _read_token() -> str:
    return xz_common.read_token_file(_GLOBAL_CONFIG["token_file"])


def _sanitize_err(msg: Any, limit: int = 200) -> str:
    cleaned = _SENSITIVE_ERR_RE.sub("[REDACTED]", str(msg or ""))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def _canonical_turn_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    u_msg = str(item.get("user_message") or "").strip()
    a_msg = str(item.get("assistant_reply") or "").strip()
    if not u_msg or not a_msg:
        return None
    req_id = str(item.get("request_id") or f"xz-{int(time.time() * 1000)}").strip()[:110]
    if not req_id:
        req_id = f"xz-{int(time.time() * 1000)}"
    payload: dict[str, Any] = {
        "request_id": req_id,
        "user_message": u_msg,
        "assistant_reply": a_msg,
        "source": str(item.get("source") or "esp32").strip()[:32] or "esp32",
        "used_tools": bool(item.get("used_tools", False)),
    }
    raw_occ = item.get("occurred_at")
    if isinstance(raw_occ, (int, float)) and not isinstance(raw_occ, bool) and raw_occ > 0:
        payload["occurred_at"] = float(raw_occ)
    raw_dev = item.get("device_id")
    if isinstance(raw_dev, str) and raw_dev.strip():
        payload["device_id"] = raw_dev.strip()[:64]
    return payload


def _merge_turn_payloads(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge duplicate request_id updates without ever losing reply length, tool flag, or earliest timestamp."""
    merged = dict(existing)
    merged["request_id"] = incoming["request_id"]
    merged["user_message"] = incoming["user_message"] or existing.get("user_message", "")

    old_reply = str(existing.get("assistant_reply") or "")
    new_reply = str(incoming.get("assistant_reply") or "")
    if len(new_reply) > len(old_reply):
        merged["assistant_reply"] = new_reply
    elif len(new_reply) == len(old_reply) and new_reply != old_reply:
        merged["assistant_reply"] = new_reply
    else:
        merged["assistant_reply"] = old_reply

    merged["used_tools"] = bool(existing.get("used_tools", False)) or bool(
        incoming.get("used_tools", False)
    )
    merged["source"] = str(incoming.get("source") or existing.get("source") or "esp32")

    old_occ = existing.get("occurred_at")
    new_occ = incoming.get("occurred_at")
    valid_occ = [
        float(v)
        for v in (old_occ, new_occ)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
    ]
    if valid_occ:
        merged["occurred_at"] = min(valid_occ)

    dev = str(incoming.get("device_id") or existing.get("device_id") or "").strip()
    if dev:
        merged["device_id"] = dev[:64]
    return merged


def _payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AnimaOutbox:
    """Durable SQLite outbox + single background delivery worker with exponential backoff."""

    def __init__(
        self,
        db_path: str,
        *,
        service_url_getter: Callable[[], str] | None = None,
        character_id_getter: Callable[[], str] | None = None,
        token_getter: Callable[[], str] | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
        base_backoff: float = 2.0,
        max_backoff: float = 300.0,
        keep_sent_rows: int = 500,
        auto_start: bool = True,
    ):
        self.db_path = db_path
        self._service_url_getter = service_url_getter or (
            lambda: str(_GLOBAL_CONFIG["service_url"]).rstrip("/")
        )
        self._character_id_getter = character_id_getter or (
            lambda: str(_GLOBAL_CONFIG["character_id"]).strip() or "xiaozhi"
        )
        self._token_getter = token_getter or _read_token
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=8.0, trust_env=False)
        )
        self.base_backoff = max(0.05, float(base_backoff))
        self.max_backoff = max(self.base_backoff, float(max_backoff))
        self.keep_sent_rows = max(20, int(keep_sent_rows))
        self._auto_start = bool(auto_start)

        self._lock = threading.RLock()
        self._drain_lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_error: str = ""
        self._prune_counter: int = 0

    def _ensure_db(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is not None:
                return self._db
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_turns (
                    request_id TEXT PRIMARY KEY,
                    character_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    sent_at REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_pending "
                "ON outbox_turns(status, next_retry_at, created_at)"
            )
            conn.commit()
            self._db = conn
            return conn

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._ensure_db()
            t = threading.Thread(target=self._worker_loop, name="anima-outbox-worker", daemon=True)
            t.start()
            self._worker = t
            self._wake.set()

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        w = self._worker
        if w is not None and w.is_alive() and w is not threading.current_thread():
            w.join(timeout=timeout)
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                except Exception:
                    pass
                self._db = None
            self._worker = None

    def enqueue(self, turns_payload: list[dict[str, Any]], *, character_id: str | None = None) -> int:
        if not turns_payload:
            return 0
        cid = (character_id or self._character_id_getter()).strip() or "xiaozhi"
        now = time.time()
        queued = 0
        with self._lock:
            db = self._ensure_db()
            for raw in turns_payload:
                item = _canonical_turn_payload(raw)
                if item is None:
                    continue
                req_id = item["request_id"]
                existing = db.execute(
                    "SELECT payload_json, payload_hash, status FROM outbox_turns WHERE request_id=?",
                    (req_id,),
                ).fetchone()
                if existing is not None:
                    try:
                        prev_payload = json.loads(existing["payload_json"])
                    except Exception:
                        prev_payload = {}
                    merged = _merge_turn_payloads(prev_payload, item)
                    new_hash = _payload_hash(merged)
                    if existing["payload_hash"] == new_hash and existing["status"] in ("sent", "pending"):
                        continue
                    db.execute(
                        "UPDATE outbox_turns "
                        "SET character_id=?, payload_json=?, payload_hash=?, status='pending', "
                        "next_retry_at=0, updated_at=? "
                        "WHERE request_id=?",
                        (cid, json.dumps(merged, ensure_ascii=False), new_hash, now, req_id),
                    )
                    queued += 1
                else:
                    h = _payload_hash(item)
                    db.execute(
                        "INSERT INTO outbox_turns("
                        "request_id, character_id, payload_json, payload_hash, status, "
                        "attempts, next_retry_at, last_error, created_at, updated_at, sent_at"
                        ") VALUES(?,?,?,?, 'pending', 0, 0, '', ?, ?, NULL)",
                        (req_id, cid, json.dumps(item, ensure_ascii=False), h, now, now),
                    )
                    queued += 1
            db.commit()

        if queued > 0:
            if self._auto_start:
                self.start()
            self._wake.set()
        return queued

    def _schedule_backoff(self, rows: list[sqlite3.Row], err_msg: str) -> None:
        now = time.time()
        sanitized = _sanitize_err(err_msg)
        self._last_error = sanitized
        with self._lock:
            db = self._ensure_db()
            for r in rows:
                attempts = int(r["attempts"] or 0) + 1
                delay = min(self.max_backoff, self.base_backoff * (2 ** min(attempts - 1, 8)))
                db.execute(
                    "UPDATE outbox_turns SET attempts=?, next_retry_at=?, last_error=?, updated_at=? "
                    "WHERE request_id=? AND status='pending'",
                    (attempts, now + delay, sanitized, now, r["request_id"]),
                )
            db.commit()

    def _mark_sent(self, req_ids: list[str], snapshot_hashes: dict[str, str]) -> int:
        if not req_ids:
            return 0
        now = time.time()
        marked = 0
        with self._lock:
            db = self._ensure_db()
            for rid in req_ids:
                # Only mark sent if payload hasn't been superseded by a newer update while in-flight!
                cur = db.execute(
                    "UPDATE outbox_turns SET status='sent', last_error='', updated_at=?, sent_at=? "
                    "WHERE request_id=? AND status='pending' AND payload_hash=?",
                    (now, now, rid, snapshot_hashes[rid]),
                )
                marked += max(0, cur.rowcount or 0)
            self._prune_counter += marked
            if self._prune_counter >= 50:
                self._prune_counter = 0
                db.execute(
                    "DELETE FROM outbox_turns WHERE request_id IN ("
                    "  SELECT request_id FROM outbox_turns WHERE status='sent' "
                    "  ORDER BY coalesce(sent_at, updated_at) DESC LIMIT -1 OFFSET ?"
                    ")",
                    (self.keep_sent_rows,),
                )
            db.commit()
        return marked

    def drain_once(self, *, batch_size: int = 16, force_due: bool = False) -> dict[str, Any]:
        with self._drain_lock:
            return self._drain_once_locked(batch_size=batch_size, force_due=force_due)

    def _quarantine_corrupt_rows(self, req_ids: list[str]) -> None:
        if not req_ids:
            return
        now = time.time()
        with self._lock:
            db = self._ensure_db()
            for rid in req_ids:
                db.execute(
                    "UPDATE outbox_turns SET status='corrupt', last_error='corrupt_payload_json', updated_at=? "
                    "WHERE request_id=? AND status='pending'",
                    (now, rid),
                )
            db.commit()

    def _drain_once_locked(self, *, batch_size: int = 16, force_due: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            db = self._ensure_db()
            if force_due:
                rows = db.execute(
                    "SELECT * FROM outbox_turns WHERE status='pending' "
                    "ORDER BY created_at ASC LIMIT ?",
                    (max(1, int(batch_size)),),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM outbox_turns WHERE status='pending' AND next_retry_at <= ? "
                    "ORDER BY created_at ASC LIMIT ?",
                    (now, max(1, int(batch_size))),
                ).fetchall()

        if not rows:
            return {"sent": 0, "failed": 0, "remaining": self.pending_count()}

        # Group due rows by character_id while preserving order
        by_char: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_char.setdefault(str(r["character_id"] or "xiaozhi"), []).append(r)

        token = (self._token_getter() or "").strip()
        if not token:
            self._schedule_backoff(rows, "missing_anima_token")
            return {"sent": 0, "failed": len(rows), "remaining": self.pending_count()}

        url = self._service_url_getter().rstrip("/") + "/xiaozhi/ingest"
        total_sent = 0
        total_failed = 0

        for cid, group_rows in by_char.items():
            turns_batch = []
            snapshot_hashes: dict[str, str] = {}
            valid_rows: list[sqlite3.Row] = []
            corrupt_ids: list[str] = []
            for r in group_rows:
                try:
                    parsed = json.loads(r["payload_json"])
                    if not isinstance(parsed, dict):
                        raise ValueError("payload_json is not a dict")
                    turns_batch.append(parsed)
                    snapshot_hashes[r["request_id"]] = r["payload_hash"]
                    valid_rows.append(r)
                except Exception:
                    corrupt_ids.append(r["request_id"])
            if corrupt_ids:
                self._quarantine_corrupt_rows(corrupt_ids)
                logger.bind(tag=TAG).warning(
                    f"Anima 本地发件箱隔离 {len(corrupt_ids)} 条损坏记录，防止阻塞重试队列"
                )
            if not valid_rows:
                continue

            try:
                with self._client_factory() as client:
                    resp = client.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        json={"character_id": cid, "turns": turns_batch},
                    )
                if resp.status_code == 200:
                    data = {}
                    try:
                        data = resp.json() if resp.content else {}
                    except Exception:
                        data = {}
                    rejected = set(data.get("rejected_ids") or [])
                    if "accepted_ids" in data:
                        accepted_set = set(data.get("accepted_ids") or [])
                        ok_ids = [
                            r["request_id"]
                            for r in valid_rows
                            if r["request_id"] in accepted_set and r["request_id"] not in rejected
                        ]
                        ok_set = set(ok_ids)
                        fail_rows = [r for r in valid_rows if r["request_id"] not in ok_set]
                    elif "rejected_ids" in data:
                        ok_ids = [
                            r["request_id"]
                            for r in valid_rows
                            if r["request_id"] not in rejected
                        ]
                        ok_set = set(ok_ids)
                        fail_rows = [r for r in valid_rows if r["request_id"] not in ok_set]
                    else:
                        queued_n = int(data.get("queued", len(valid_rows)))
                        if queued_n >= len(valid_rows):
                            ok_ids = [r["request_id"] for r in valid_rows]
                            fail_rows = []
                        else:
                            ok_ids = []
                            fail_rows = valid_rows

                    if ok_ids:
                        total_sent += self._mark_sent(ok_ids, snapshot_hashes)
                        self._last_error = ""
                        logger.bind(tag=TAG).info(
                            f"已向 Anima Brain 可靠提交 {len(ok_ids)} 轮对话记忆与情绪更新"
                        )
                    if fail_rows:
                        total_failed += len(fail_rows)
                        self._schedule_backoff(fail_rows, "ingest_partial_or_queue_full")
                        logger.bind(tag=TAG).warning(
                            f"Anima Brain ingest 队列繁忙，{len(fail_rows)} 轮已保留在本地发件箱稍后重试"
                        )
                else:
                    total_failed += len(valid_rows)
                    err = f"HTTP {resp.status_code}: {resp.text[:160]}"
                    self._schedule_backoff(valid_rows, err)
                    logger.bind(tag=TAG).warning(
                        f"Anima Brain ingest 返回 HTTP {resp.status_code}，已保留在本地发件箱指数退避重试"
                    )
            except Exception as e:
                total_failed += len(valid_rows)
                self._schedule_backoff(valid_rows, str(e))
                logger.bind(tag=TAG).warning(
                    f"连接 Anima Brain ingest 失败，已保留在本地发件箱等待重试: {e}"
                )

        return {"sent": total_sent, "failed": total_failed, "remaining": self.pending_count()}

    def pending_count(self) -> int:
        with self._lock:
            db = self._ensure_db()
            row = db.execute("SELECT count(*) FROM outbox_turns WHERE status='pending'").fetchone()
            return int(row[0] if row else 0)

    def _next_wait_seconds(self) -> float:
        now = time.time()
        with self._lock:
            db = self._ensure_db()
            row = db.execute(
                "SELECT min(next_retry_at) FROM outbox_turns WHERE status='pending'"
            ).fetchone()
        if not row or row[0] is None:
            return 15.0
        return max(0.1, min(15.0, float(row[0]) - now))

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                res = self.drain_once(batch_size=16, force_due=False)
                if res["sent"] > 0 and res["remaining"] > 0 and res["failed"] == 0:
                    continue
            except Exception as exc:
                self._last_error = _sanitize_err(exc)
            wait_sec = self._next_wait_seconds()
            self._wake.wait(timeout=wait_sec)
            self._wake.clear()

    def flush(self, timeout: float = 4.0) -> dict[str, Any]:
        """Synchronously attempt to push pending outbox rows before process exit/restart."""
        deadline = time.monotonic() + max(0.2, float(timeout))
        sent_total = 0
        failed_total = 0
        while time.monotonic() < deadline:
            if self.pending_count() == 0:
                break
            res = self.drain_once(batch_size=16, force_due=True)
            sent_total += res["sent"]
            failed_total += res["failed"]
            if res["remaining"] == 0 or res["failed"] > 0:
                # If server is unreachable right now, stop spinning so restart isn't stalled;
                # rows stay durably persisted in SQLite for automatic delivery after restart.
                break
        return {
            "sent": sent_total,
            "failed": failed_total,
            "remaining": self.pending_count(),
        }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            db = self._ensure_db()
            pending = int(
                db.execute("SELECT count(*) FROM outbox_turns WHERE status='pending'").fetchone()[0]
            )
            sent = int(
                db.execute("SELECT count(*) FROM outbox_turns WHERE status='sent'").fetchone()[0]
            )
            return {
                "db_path": self.db_path,
                "pending": pending,
                "sent": sent,
                "last_error": self._last_error,
                "worker_alive": bool(self._worker and self._worker.is_alive()),
            }


_outbox_lock = threading.Lock()
_default_outbox: AnimaOutbox | None = None
_pre_flush_hooks: list[Callable[[], None]] = []


def register_pre_flush_hook(hook: Callable[[], None]) -> None:
    """Register a callback run before flush_outbox() (e.g. flushing Dialogue debounce timers)."""
    with _outbox_lock:
        if hook not in _pre_flush_hooks:
            _pre_flush_hooks.append(hook)


def get_default_outbox(*, auto_start: bool = True) -> AnimaOutbox:
    global _default_outbox
    target_db = str(_GLOBAL_CONFIG.get("outbox_db") or "/opt/xiaozhi-esp32-server/data/anima_outbox.sqlite")
    with _outbox_lock:
        if _default_outbox is not None and _default_outbox.db_path != target_db:
            _default_outbox.close()
            _default_outbox = None
        if _default_outbox is None:
            _default_outbox = AnimaOutbox(target_db, auto_start=auto_start)
        if auto_start:
            _default_outbox.start()
        return _default_outbox


def push_turns_async(turns_payload: list):
    """Persist turns into durable local SQLite outbox and wake background delivery worker."""
    if not turns_payload:
        return
    try:
        outbox = get_default_outbox(auto_start=True)
        outbox.enqueue(turns_payload)
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"写入 Anima 本地发件箱失败: {exc}")


def flush_outbox(timeout: float = 4.0) -> dict[str, Any]:
    """Run pre-flush hooks and drain pending outbox turns before restart/shutdown."""
    with _outbox_lock:
        hooks = list(_pre_flush_hooks)
    for fn in hooks:
        try:
            fn()
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"执行记忆 pre-flush 钩子异常: {exc}")
    try:
        outbox = get_default_outbox(auto_start=False)
        return outbox.flush(timeout=timeout)
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"刷新 Anima 本地发件箱失败: {exc}")
        return {"sent": 0, "failed": 0, "remaining": -1}


def get_outbox_stats() -> dict[str, Any]:
    try:
        return get_default_outbox(auto_start=False).stats()
    except Exception:
        return {"pending": 0, "sent": 0, "last_error": "", "worker_alive": False}


def _has_real_tool_calls(msg: Any) -> bool:
    tc = getattr(msg, "tool_calls", None)
    return isinstance(tc, (list, tuple)) and len(tc) > 0


def _read_occurred_at(msg: Any) -> float | None:
    raw = getattr(msg, "_occurred_at", None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return None


def extract_clean_pairs(messages, *, device_id: str = "", source: str = "esp32") -> list:
    """Pair genuine non-temporary user utterances with full assistant replies and turn metadata."""
    pairs = []
    cur_uid = None
    cur_user = None
    cur_occurred_at: float | None = None
    cur_used_tools = False
    cur_assistant_parts: list[str] = []

    def _emit_current():
        if not (cur_user and cur_assistant_parts):
            return
        pair: dict[str, Any] = {
            "request_id": f"xz-{cur_uid}",
            "user_message": cur_user,
            "assistant_reply": " ".join(cur_assistant_parts).strip(),
            "source": source or "esp32",
            "used_tools": bool(cur_used_tools),
        }
        if cur_occurred_at is not None:
            pair["occurred_at"] = cur_occurred_at
        if device_id:
            pair["device_id"] = str(device_id).strip()[:64]
        pairs.append(pair)

    for m in messages or []:
        if getattr(m, "is_temporary", False):
            continue
        role = getattr(m, "role", "")
        content = (getattr(m, "content", None) or "").strip()
        if role == "user":
            if content.startswith("[系统提示]"):
                continue
            _emit_current()
            cur_uid = str(getattr(m, "uniq_id", ""))[:64]
            cur_user = content
            cur_occurred_at = _read_occurred_at(m)
            cur_used_tools = getattr(m, "_used_tools", False) is True
            cur_assistant_parts = []
        elif role == "tool" and cur_user:
            cur_used_tools = True
        elif role == "assistant" and cur_user:
            if getattr(m, "_standalone_assistant", False) is True:
                continue
            if _has_real_tool_calls(m) or getattr(m, "_used_tools", False) is True:
                cur_used_tools = True
            if content:
                clean_reply = mei_emotion_router.strip_emotion_tags(content).strip()
                if clean_reply:
                    if not cur_assistant_parts or cur_assistant_parts[-1] != clean_reply:
                        cur_assistant_parts.append(clean_reply)

    _emit_current()
    return pairs


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory=None):
        super().__init__(config)
        cfg = config or {}
        _GLOBAL_CONFIG["service_url"] = str(
            cfg.get("service_url") or "http://anima-brain:18787"
        ).rstrip("/")
        _GLOBAL_CONFIG["character_id"] = str(cfg.get("character_id") or "xiaozhi").strip()
        if cfg.get("token_file"):
            _GLOBAL_CONFIG["token_file"] = str(cfg["token_file"])
        if cfg.get("outbox_db"):
            _GLOBAL_CONFIG["outbox_db"] = str(cfg["outbox_db"])

        # P1-9: Hard per-phase HTTP timeout + outer asyncio.wait_for total budget + circuit breaker + SWR cache
        http_timeout = max(0.2, float(cfg.get("recall_http_timeout", 0.8)))
        connect_timeout = max(0.05, min(http_timeout, float(cfg.get("recall_connect_timeout", 0.2))))
        self._http_timeout = httpx.Timeout(http_timeout, connect=connect_timeout)
        self._recall_budget_seconds = max(0.05, float(cfg.get("recall_budget_seconds", 0.6)))
        self._bg_budget_seconds = max(self._recall_budget_seconds, float(cfg.get("recall_bg_budget_seconds", 0.8)))
        self._stale_while_revalidate = bool(cfg.get("stale_while_revalidate", True))
        self._cache_ttl_seconds = max(1.0, float(cfg.get("recall_cache_ttl_seconds", 600.0)))

        # Circuit breaker parameters
        self._cb_failure_threshold = max(1, int(cfg.get("recall_cb_threshold", 3)))
        self._cb_cooldown_seconds = max(0.1, float(cfg.get("recall_cb_cooldown_seconds", 30.0)))
        self._cb_consecutive_failures = 0
        self._cb_open_until = 0.0

        # Stale-while-revalidate state (previous turn recall returns immediately with 0ms blocking)
        self._cached_context: str | None = None
        self._cache_ts: float = 0.0
        self._bg_refresh_task: asyncio.Task | None = None
        self._pending_refresh_query: str | None = None

        self._recall_stats: dict[str, Any] = {
            "sync_calls": 0,
            "cache_hits": 0,
            "bg_refreshes": 0,
            "timeouts": 0,
            "errors": 0,
            "cb_short_circuits": 0,
            "last_latency_ms": 0.0,
        }

        self._client = httpx.AsyncClient(timeout=self._http_timeout, trust_env=False)
        self._client_loop: asyncio.AbstractEventLoop | None = None
        try:
            # Start outbox worker on boot so any unsent turns from before restart drain immediately
            get_default_outbox(auto_start=True)
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"初始化 Anima 本地发件箱失败: {exc}")
        logger.bind(tag=TAG).info(
            f"Anima Brain 记忆引擎桥接已就绪 -> {_GLOBAL_CONFIG['service_url']} "
            f"(角色: {_GLOBAL_CONFIG['character_id']}, 预算: {self._recall_budget_seconds:.2f}s, "
            f"SWR={'on' if self._stale_while_revalidate else 'off'})"
        )

    def _get_async_client(self):
        cur_loop = asyncio.get_running_loop()
        if isinstance(self._client, httpx.AsyncClient):
            if self._client.is_closed or (
                self._client_loop is not None and self._client_loop is not cur_loop
            ):
                self._client = httpx.AsyncClient(timeout=self._http_timeout, trust_env=False)
                self._client_loop = cur_loop
            elif self._client_loop is None:
                self._client_loop = cur_loop
        return self._client

    def _is_circuit_open(self) -> bool:
        return self._cb_open_until > 0.0 and time.monotonic() < self._cb_open_until

    def _record_recall_success(self, ctx: str, elapsed_ms: float) -> None:
        self._cb_consecutive_failures = 0
        self._cb_open_until = 0.0
        self._cached_context = ctx
        self._cache_ts = time.monotonic()
        self._recall_stats["last_latency_ms"] = round(elapsed_ms, 2)

    def _record_recall_failure(self, *, is_timeout: bool = False) -> None:
        if is_timeout:
            self._recall_stats["timeouts"] += 1
        else:
            self._recall_stats["errors"] += 1
        self._cb_consecutive_failures += 1
        if self._cb_consecutive_failures >= self._cb_failure_threshold:
            self._cb_open_until = time.monotonic() + self._cb_cooldown_seconds
            logger.bind(tag=TAG).warning(
                f"Anima Brain recall 连续失败 {self._cb_consecutive_failures} 次，熔断开启 {self._cb_cooldown_seconds:.0f}s"
            )

    def get_recall_stats(self) -> dict[str, Any]:
        return {
            **self._recall_stats,
            "circuit_open": self._is_circuit_open(),
            "consecutive_failures": self._cb_consecutive_failures,
            "cache_warm": self._cached_context is not None,
            "budget_seconds": self._recall_budget_seconds,
            "stale_while_revalidate": self._stale_while_revalidate,
        }

    async def _fetch_recall_once(self, query: str, token: str) -> str:
        url = _GLOBAL_CONFIG["service_url"] + "/xiaozhi/recall"
        client = self._get_async_client()
        t0 = time.monotonic()
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "character_id": _GLOBAL_CONFIG["character_id"],
                "query": query or "",
            },
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        data = resp.json()
        mood_str = str(data.get("mood") or "")
        mei_emotion_router.set_anima_mood(mood_str)
        ctx = (data.get("context_text") or "").strip()
        # 剥离重复的【角色身份设定】段落，以小智主配置 prompt 为唯一人设权威，避免双倍 Token 与人设冲突
        ctx = re.sub(
            r"^【角色身份设定（来自 Companion Studio）】.*?(?=\n\n【|\Z)",
            "",
            ctx,
            flags=re.DOTALL,
        ).strip()
        self._record_recall_success(ctx, elapsed_ms)
        logger.bind(tag=TAG).info(
            f"Anima Brain 极速召回成功 ({elapsed_ms:.0f}ms): 事实={data.get('facts_count', 0)}, "
            f"待跟进={data.get('open_loops_count', 0)}, 状态={data.get('mood', '')}"
        )
        return ctx

    async def _bg_refresh_loop(self, initial_query: str) -> None:
        next_query: str | None = initial_query
        try:
            while next_query is not None:
                q = next_query
                next_query = None
                if self._is_circuit_open():
                    self._pending_refresh_query = None
                    self._recall_stats["cb_short_circuits"] += 1
                    return
                token = _read_token()
                if not token:
                    self._pending_refresh_query = None
                    return
                self._recall_stats["bg_refreshes"] += 1
                try:
                    await asyncio.wait_for(
                        self._fetch_recall_once(q, token),
                        timeout=self._bg_budget_seconds,
                    )
                except asyncio.TimeoutError:
                    self._record_recall_failure(is_timeout=True)
                    logger.bind(tag=TAG).warning(
                        f"Anima Brain 后台召回刷新超时 (>{self._bg_budget_seconds:.2f}s)，继续沿用缓存"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._record_recall_failure(is_timeout=False)
                    logger.bind(tag=TAG).warning(f"Anima Brain 后台召回刷新失败: {e}")

                if self._pending_refresh_query is not None:
                    next_query = self._pending_refresh_query
                    self._pending_refresh_query = None
        finally:
            cur_task = asyncio.current_task()
            if self._bg_refresh_task is cur_task:
                self._bg_refresh_task = None

    def _schedule_bg_refresh(self, query: str) -> None:
        if self._is_circuit_open():
            self._recall_stats["cb_short_circuits"] += 1
            return
        if self._bg_refresh_task is not None and not self._bg_refresh_task.done():
            self._pending_refresh_query = query
            return
        try:
            task = asyncio.create_task(self._bg_refresh_loop(query))
            self._bg_refresh_task = task

            def _consume_exc(t: asyncio.Task) -> None:
                try:
                    t.exception()
                except (asyncio.CancelledError, Exception):
                    pass

            task.add_done_callback(_consume_exc)
        except RuntimeError:
            pass

    async def query_memory(self, query: str) -> str:
        token = _read_token()
        if not token:
            return ""

        now = time.monotonic()
        # Stale-while-revalidate: serve previous turn's recall immediately (0ms blocking) and refresh in background
        if (
            self._stale_while_revalidate
            and self._cached_context is not None
            and (now - self._cache_ts) <= self._cache_ttl_seconds
        ):
            self._recall_stats["cache_hits"] += 1
            self._schedule_bg_refresh(query or "")
            return self._cached_context

        # Circuit breaker open: degrade immediately without waiting on network
        if self._is_circuit_open():
            self._recall_stats["cb_short_circuits"] += 1
            return self._cached_context or ""

        self._recall_stats["sync_calls"] += 1
        try:
            return await asyncio.wait_for(
                self._fetch_recall_once(query or "", token),
                timeout=self._recall_budget_seconds,
            )
        except asyncio.TimeoutError:
            self._record_recall_failure(is_timeout=True)
            logger.bind(tag=TAG).warning(
                f"Anima Brain recall 超出总预算 ({self._recall_budget_seconds:.2f}s)，降级直接回复"
            )
        except Exception as e:
            self._record_recall_failure(is_timeout=False)
            logger.bind(tag=TAG).warning(f"Anima Brain recall 超时或暂不可用，降级直接回复: {e}")
        return self._cached_context or ""

    async def save_memory(self, msgs, session_id=None):
        dev_id = str(getattr(self, "role_id", "") or "")
        pairs = extract_clean_pairs(msgs, device_id=dev_id)
        if pairs:
            push_turns_async(pairs)
        return "ok"

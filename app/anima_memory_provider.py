"""Anima Brain memory provider for xiaozhi-esp32-server (Python 3.10 compatible HTTP client).

- Sub-15ms read path (/xiaozhi/recall) before streaming TTS voice reply
- Durable local SQLite outbox (data/anima_outbox.sqlite) + single worker with
  exponential backoff retry and request_id idempotency for /xiaozhi/ingest
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable

import httpx
import mei_emotion_router
import memory_archives
import xz_common
from core.providers.memory.base import MemoryProviderBase, logger

_CURRENT_DEVICE_ID_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "anima_current_device_id", default=None
)
_RETRYABLE_HTTP_4XX = frozenset({401, 408, 429})


def get_current_device_id() -> str | None:
    return _CURRENT_DEVICE_ID_VAR.get()


def set_current_device_id(device_id: str | None) -> contextvars.Token:
    cleaned = str(device_id or "").strip()
    return _CURRENT_DEVICE_ID_VAR.set(cleaned or None)


def reset_current_device_id(token: contextvars.Token) -> None:
    try:
        _CURRENT_DEVICE_ID_VAR.reset(token)
    except Exception:
        pass

TAG = __name__

_GLOBAL_CONFIG = {
    "service_url": "http://anima-brain:18787",
    "character_id": "xiaozhi",
    "device_character_map": {},
    "isolate_character_by_device": False,
    "token_file": xz_common.ANIMA_TOKEN_PATH,
    "outbox_db": os.path.join(xz_common.STATE_DIR, "anima_outbox.sqlite"),
}

_SAFE_CHAR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


@dataclass(frozen=True)
class MemoryTarget:
    """The device and character selected when this connection opened."""

    device_key: str
    archive: str
    character_id: str
    chat_key: str = ""
    handoff: tuple[tuple[str, str], ...] = field(default=(), compare=False, repr=False)
    handoff_index: int = field(default=-1, compare=False)

    @property
    def mood_key(self):
        if self.archive == "daily":
            return None if self.device_key == "__default__" else self.device_key
        return (self.device_key, self.character_id)

    def acknowledge_handoff(self):
        if self.handoff:
            memory_archives.acknowledge_handoff(self.device_key, self.chat_key, self.handoff, self.handoff_index)


def connection_memory_target(device_key: str, base_character_id: str) -> MemoryTarget:
    if device_key == "__default__":
        return MemoryTarget(device_key, "daily", base_character_id)
    archive, chat_key, turns, index = memory_archives.selected_binding(device_key)
    character_id = memory_archives.scoped_character_id(base_character_id, archive, device_key, chat_key)
    return MemoryTarget(device_key, archive, character_id, chat_key, turns, index)


def _parse_device_character_map(raw: Any) -> dict[str, str]:
    """Parse optional `device_id -> character_id` map for multi-member multi-ESP32 households (P2-4)."""
    mapping: dict[str, str] = {}
    source: dict[Any, Any] = {}
    if isinstance(raw, dict):
        source = raw
    elif isinstance(raw, str) and raw.strip():
        s = raw.strip()
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    source = parsed
            except Exception:
                source = {}
        else:
            for item in s.split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    source[k] = v
    for raw_dev, raw_cid in source.items():
        dev_k = str(raw_dev or "").strip().lower()[:64]
        cid_v = str(raw_cid or "").strip().lower()[:48]
        if dev_k and cid_v and _SAFE_CHAR_ID_RE.match(cid_v):
            mapping[dev_k] = cid_v
    return mapping


def resolve_character_id_for_device(
    device_id: str | None = None,
    *,
    base_character_id: str | None = None,
    device_map: dict[str, str] | None = None,
    isolate_by_device: bool | None = None,
) -> str:
    """Resolve Anima character_id for an ESP32 device without collapsing per-device warm recall caches (P2-4)."""
    base_cid = str(base_character_id or _GLOBAL_CONFIG.get("character_id") or "xiaozhi").strip() or "xiaozhi"
    dev_k = str(device_id or "").strip().lower()[:64]
    if not dev_k or dev_k == "__default__":
        return base_cid
    eff_map = device_map if isinstance(device_map, dict) else (_GLOBAL_CONFIG.get("device_character_map") or {})
    mapped = eff_map.get(dev_k)
    if mapped and _SAFE_CHAR_ID_RE.match(mapped):
        return mapped
    eff_iso = (
        bool(_GLOBAL_CONFIG.get("isolate_character_by_device", False))
        if isolate_by_device is None
        else bool(isolate_by_device)
    )
    if eff_iso:
        slug = re.sub(r"[^a-z0-9]+", "", dev_k)[:16]
        if slug:
            cand = f"{base_cid.lower()}_{slug}"[:48]
            if _SAFE_CHAR_ID_RE.match(cand):
                return cand
    return base_cid

_SENSITIVE_ERR_RE = re.compile(
    r"(?i)(Bearer\s+[A-Za-z0-9._\-~+/=]+|sk-[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{16,})"
)


def _read_token() -> str:
    return xz_common.get_anima_token(_GLOBAL_CONFIG["token_file"])


def _sanitize_err(msg: Any, limit: int = 200) -> str:
    cleaned = _SENSITIVE_ERR_RE.sub("[REDACTED]", str(msg or ""))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def _new_request_id() -> str:
    return f"xz-{uuid.uuid4().hex}"


def _canonical_turn_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    u_msg = str(item.get("user_message") or "").strip()
    a_msg = str(item.get("assistant_reply") or "").strip()
    if not u_msg or not a_msg:
        return None
    raw_req = str(item.get("request_id") or "").strip()[:110]
    req_id = raw_req or _new_request_id()
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


def _merge_turn_payloads(existing: dict[str, Any], incoming: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Merge duplicate request_id updates safely (B-4):
    - Refuse overwriting if user_message differs OR assistant_reply diverges (non-prefix continuation).
    - Allow legitimate post-tool continuation when user_message matches and new_reply extends old_reply.
    Returns (merged_payload, conflict_detected).
    """
    old_user = str(existing.get("user_message") or "").strip()
    new_user = str(incoming.get("user_message") or "").strip()
    old_reply = str(existing.get("assistant_reply") or "").strip()
    new_reply = str(incoming.get("assistant_reply") or "").strip()

    if old_user and new_user and new_user != old_user:
        return dict(existing), True

    if old_reply and new_reply and new_reply != old_reply:
        is_continuation = len(new_reply) > len(old_reply) and new_reply.startswith(old_reply)
        is_shorter_prefix = old_reply.startswith(new_reply)
        if not is_continuation and not is_shorter_prefix:
            return dict(existing), True

    merged = dict(existing)
    merged["request_id"] = incoming["request_id"]
    merged["user_message"] = old_user or new_user

    if len(new_reply) > len(old_reply) and (not old_reply or new_reply.startswith(old_reply)):
        merged["assistant_reply"] = new_reply
    else:
        merged["assistant_reply"] = old_reply or new_reply

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
    return merged, False


def _payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AnimaOutbox:
    """Durable SQLite outbox + single background delivery worker with exponential backoff & dead-lettering (B-5)."""

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
        max_retries: int | None = None,
        dead_ttl_days: float | None = None,
        keep_sent_rows: int = 500,
        wal_journal_size_limit: int = 8 * 1024 * 1024,
        wal_checkpoint_interval: float = 180.0,
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
        if max_retries is None:
            raw_mr = (os.environ.get("ANIMA_OUTBOX_MAX_RETRIES") or "").strip()
            try:
                max_retries = int(raw_mr) if raw_mr else 8
            except ValueError:
                max_retries = 8
        self.max_retries = max(1, int(max_retries))
        if dead_ttl_days is None:
            raw_ttl = (os.environ.get("ANIMA_OUTBOX_DEAD_TTL_DAYS") or "").strip()
            try:
                dead_ttl_days = float(raw_ttl) if raw_ttl else 14.0
            except ValueError:
                dead_ttl_days = 14.0
        self.dead_ttl_days = max(0.0, float(dead_ttl_days))
        self.keep_sent_rows = max(20, int(keep_sent_rows))
        self.wal_journal_size_limit = max(65536, int(wal_journal_size_limit))
        self.wal_checkpoint_interval = max(1.0, float(wal_checkpoint_interval))
        self._auto_start = bool(auto_start)

        self._lock = threading.RLock()
        self._drain_lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_error: str = ""
        self._prune_counter: int = 0
        self._last_wal_checkpoint_ts: float | None = None
        self._last_cleanup_ts: float | None = None

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
            conn.execute(f"PRAGMA journal_size_limit={self.wal_journal_size_limit}")
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
        explicit_cid = str(character_id or "").strip()
        default_cid = explicit_cid or (self._character_id_getter().strip() or "xiaozhi")
        now = time.time()
        queued = 0
        with self._lock:
            db = self._ensure_db()
            for raw in turns_payload:
                item = _canonical_turn_payload(raw)
                if item is None:
                    continue
                cid = explicit_cid or raw.get("_anima_character_id")
                if not cid:
                    device_key = str(item.get("device_id") or "").strip().lower() or "__default__"
                    base = resolve_character_id_for_device(item.get("device_id"), base_character_id=default_cid)
                    cid = connection_memory_target(device_key, base).character_id
                req_id = item["request_id"]
                existing = db.execute(
                    "SELECT character_id, payload_json, payload_hash, status FROM outbox_turns WHERE request_id=?",
                    (req_id,),
                ).fetchone()
                if existing is not None:
                    if existing["character_id"] != cid:
                        logger.bind(tag=TAG).warning("Anima 本地发件箱拒绝将已有回合移动到其他角色")
                        continue
                    try:
                        prev_payload = json.loads(existing["payload_json"])
                    except Exception:
                        prev_payload = {}
                    merged, conflict = _merge_turn_payloads(prev_payload, item)
                    if conflict:
                        logger.bind(tag=TAG).warning(
                            f"Anima 本地发件箱检测到 request_id={req_id} 内容冲突，拒绝覆盖历史记录"
                        )
                        continue
                    new_hash = _payload_hash(merged)
                    if existing["payload_hash"] == new_hash and existing["status"] in (
                        "sent",
                        "pending",
                        "dead",
                        "corrupt",
                    ):
                        continue
                    if existing["status"] == "pending":
                        db.execute(
                            "UPDATE outbox_turns "
                            "SET character_id=?, payload_json=?, payload_hash=?, status='pending', "
                            "next_retry_at=0, updated_at=? "
                            "WHERE request_id=?",
                            (cid, json.dumps(merged, ensure_ascii=False), new_hash, now, req_id),
                        )
                    else:
                        db.execute(
                            "UPDATE outbox_turns "
                            "SET character_id=?, payload_json=?, payload_hash=?, status='pending', "
                            "attempts=0, next_retry_at=0, last_error='', updated_at=? "
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

    def _mark_dead(self, rows: list[sqlite3.Row], err_msg: str) -> int:
        if not rows:
            return 0
        now = time.time()
        sanitized = _sanitize_err(err_msg)
        self._last_error = sanitized
        marked = 0
        with self._lock:
            db = self._ensure_db()
            for r in rows:
                attempts = int(r["attempts"] or 0) + 1
                cur = db.execute(
                    "UPDATE outbox_turns "
                    "SET status='dead', attempts=?, next_retry_at=0, last_error=?, updated_at=? "
                    "WHERE request_id=? AND status='pending' AND payload_hash=?",
                    (attempts, sanitized, now, r["request_id"], r["payload_hash"]),
                )
                marked += max(0, cur.rowcount or 0)
            db.commit()
        return marked

    def _schedule_backoff(self, rows: list[sqlite3.Row], err_msg: str) -> int:
        now = time.time()
        sanitized = _sanitize_err(err_msg)
        self._last_error = sanitized
        dead_count = 0
        with self._lock:
            db = self._ensure_db()
            for r in rows:
                attempts = int(r["attempts"] or 0) + 1
                if attempts > self.max_retries:
                    cur = db.execute(
                        "UPDATE outbox_turns "
                        "SET status='dead', attempts=?, next_retry_at=0, last_error=?, updated_at=? "
                        "WHERE request_id=? AND status='pending' AND payload_hash=?",
                        (attempts, sanitized, now, r["request_id"], r["payload_hash"]),
                    )
                    dead_count += max(0, cur.rowcount or 0)
                else:
                    delay = min(self.max_backoff, self.base_backoff * (2 ** min(attempts - 1, 8)))
                    db.execute(
                        "UPDATE outbox_turns SET attempts=?, next_retry_at=?, last_error=?, updated_at=? "
                        "WHERE request_id=? AND status='pending' AND payload_hash=?",
                        (attempts, now + delay, sanitized, now, r["request_id"], r["payload_hash"]),
                    )
            db.commit()
        if dead_count > 0:
            logger.bind(tag=TAG).warning(
                f"Anima 本地发件箱 {dead_count} 轮重试超过上限 ({self.max_retries})，已转入 dead 死信队列: {sanitized}"
            )
        return dead_count

    def cleanup_expired_rows(
        self,
        *,
        ttl_days: float | None = None,
        now: float | None = None,
    ) -> int:
        """Delete aged 'dead' and 'corrupt' rows past TTL (default 14 days, B-5)."""
        days = self.dead_ttl_days if ttl_days is None else max(0.0, float(ttl_days))
        ts = time.time() if now is None else float(now)
        cutoff = ts - (days * 86400.0)
        with self._lock:
            db = self._ensure_db()
            cur = db.execute(
                "DELETE FROM outbox_turns "
                "WHERE status IN ('dead', 'corrupt') "
                "AND coalesce(updated_at, created_at) < ?",
                (cutoff,),
            )
            db.commit()
            self._last_cleanup_ts = time.monotonic()
            return max(0, cur.rowcount or 0)

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
                cutoff = now - (self.dead_ttl_days * 86400.0)
                db.execute(
                    "DELETE FROM outbox_turns "
                    "WHERE status IN ('dead', 'corrupt') "
                    "AND coalesce(updated_at, created_at) < ?",
                    (cutoff,),
                )
                self._last_cleanup_ts = time.monotonic()
            db.commit()
        return marked

    def drain_once(self, *, batch_size: int = 16, force_due: bool = False) -> dict[str, Any]:
        with self._drain_lock:
            return self._drain_once_locked(batch_size=batch_size, force_due=force_due)

    def _quarantine_corrupt_rows(self, rows_or_ids: list) -> None:
        if not rows_or_ids:
            return
        now = time.time()
        with self._lock:
            db = self._ensure_db()
            for item in rows_or_ids:
                if isinstance(item, tuple) and len(item) == 2:
                    rid, phash = item
                    db.execute(
                        "UPDATE outbox_turns SET status='corrupt', last_error='corrupt_payload_json', updated_at=? "
                        "WHERE request_id=? AND status='pending' AND payload_hash=?",
                        (now, rid, phash),
                    )
                else:
                    db.execute(
                        "UPDATE outbox_turns SET status='corrupt', last_error='corrupt_payload_json', updated_at=? "
                        "WHERE request_id=? AND status='pending'",
                        (now, item),
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
            return {"sent": 0, "failed": 0, "dead": 0, "remaining": self.pending_count()}

        # Group due rows by character_id while preserving order
        by_char: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_char.setdefault(str(r["character_id"] or "xiaozhi"), []).append(r)

        token = (self._token_getter() or "").strip()
        if not token:
            dead_n = self._schedule_backoff(rows, "missing_anima_token")
            return {"sent": 0, "failed": len(rows), "dead": dead_n, "remaining": self.pending_count()}

        url = self._service_url_getter().rstrip("/") + "/xiaozhi/ingest"
        total_sent = 0
        total_failed = 0
        total_dead = 0

        for cid, group_rows in by_char.items():
            turns_batch = []
            snapshot_hashes: dict[str, str] = {}
            valid_rows: list[sqlite3.Row] = []
            corrupt_items: list[tuple[str, str]] = []
            for r in group_rows:
                try:
                    parsed = json.loads(r["payload_json"])
                    if not isinstance(parsed, dict):
                        raise ValueError("payload_json is not a dict")
                    turns_batch.append(parsed)
                    snapshot_hashes[r["request_id"]] = r["payload_hash"]
                    valid_rows.append(r)
                except Exception:
                    corrupt_items.append((r["request_id"], r["payload_hash"]))
            if corrupt_items:
                self._quarantine_corrupt_rows(corrupt_items)
                logger.bind(tag=TAG).warning(
                    f"Anima 本地发件箱隔离 {len(corrupt_items)} 条损坏记录，防止阻塞重试队列"
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
                    data = resp.json()
                    if not isinstance(data, dict):
                        raise ValueError("invalid ingest acknowledgement")
                    for field in ("accepted_ids", "rejected_ids"):
                        if field in data and not (
                            isinstance(data[field], list) and all(isinstance(i, str) for i in data[field])
                        ):
                            raise ValueError("invalid ingest acknowledgement IDs")
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
                    else:
                        queued_n = data.get("queued")
                        if type(queued_n) is int and queued_n == len(valid_rows) and not rejected:
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
                        total_dead += self._schedule_backoff(fail_rows, "ingest_partial_or_queue_full")
                        logger.bind(tag=TAG).warning(
                            f"Anima Brain ingest 队列繁忙，{len(fail_rows)} 轮已保留在本地发件箱稍后重试"
                        )
                elif 400 <= resp.status_code < 500 and resp.status_code not in _RETRYABLE_HTTP_4XX:
                    total_failed += len(valid_rows)
                    err = f"HTTP {resp.status_code}: {resp.text[:160]}"
                    dead_n = self._mark_dead(valid_rows, err)
                    total_dead += dead_n
                    logger.bind(tag=TAG).warning(
                        f"Anima Brain ingest 返回 HTTP {resp.status_code} (4xx)，{dead_n} 轮已转入 dead 死信队列停止重试"
                    )
                else:
                    total_failed += len(valid_rows)
                    err = f"HTTP {resp.status_code}: {resp.text[:160]}"
                    total_dead += self._schedule_backoff(valid_rows, err)
                    logger.bind(tag=TAG).warning(
                        f"Anima Brain ingest 返回 HTTP {resp.status_code}，已保留在本地发件箱指数退避重试"
                    )
            except Exception as e:
                total_failed += len(valid_rows)
                total_dead += self._schedule_backoff(valid_rows, str(e))
                logger.bind(tag=TAG).warning(
                    f"连接 Anima Brain ingest 失败，已保留在本地发件箱等待重试: {e}"
                )

        return {
            "sent": total_sent,
            "failed": total_failed,
            "dead": total_dead,
            "remaining": self.pending_count(),
        }

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

    def checkpoint_wal(self) -> tuple[int, int, int] | None:
        """Truncate SQLite WAL file while idle to bound disk growth (B-3)."""
        with self._lock:
            db = self._ensure_db()
            if db.in_transaction:
                return None
            try:
                row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                self._last_wal_checkpoint_ts = time.monotonic()
                if row is not None:
                    return (int(row[0]), int(row[1]), int(row[2]))
            except Exception as exc:
                self._last_error = _sanitize_err(exc)
        return None

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                res = self.drain_once(batch_size=16, force_due=False)
                if res["sent"] > 0 and res["remaining"] > 0 and res["failed"] == 0:
                    continue
                if res["remaining"] == 0:
                    now_mono = time.monotonic()
                    if (
                        self._last_cleanup_ts is None
                        or (now_mono - self._last_cleanup_ts) >= 3600.0
                    ):
                        self.cleanup_expired_rows()
                    if (
                        self._last_wal_checkpoint_ts is None
                        or (now_mono - self._last_wal_checkpoint_ts) >= self.wal_checkpoint_interval
                    ):
                        self.checkpoint_wal()
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
        dead_total = 0
        while time.monotonic() < deadline:
            if self.pending_count() == 0:
                break
            res = self.drain_once(batch_size=16, force_due=True)
            sent_total += res["sent"]
            failed_total += res["failed"]
            dead_total += res.get("dead", 0)
            if res["remaining"] == 0 or res["failed"] > 0:
                # If server is unreachable right now, stop spinning so restart isn't stalled;
                # rows stay durably persisted in SQLite for automatic delivery after restart.
                break
        return {
            "sent": sent_total,
            "failed": failed_total,
            "dead": dead_total,
            "remaining": self.pending_count(),
        }

    def stats(self) -> dict[str, Any]:
        counts = {"pending": 0, "dead": 0, "corrupt": 0, "sent": 0}
        with self._lock:
            db = self._ensure_db()
            for r in db.execute("SELECT status, count(*) AS cnt FROM outbox_turns GROUP BY status").fetchall():
                st = str(r["status"] or "")
                if st in counts:
                    counts[st] = int(r["cnt"] or 0)
            return {
                "db_path": self.db_path,
                "pending": counts["pending"],
                "dead": counts["dead"],
                "corrupt": counts["corrupt"],
                "sent": counts["sent"],
                "max_retries": self.max_retries,
                "dead_ttl_days": self.dead_ttl_days,
                "last_error": self._last_error,
                "worker_alive": bool(self._worker and self._worker.is_alive()),
            }

    def replay_dead_turns(
        self,
        *,
        limit: int | None = None,
        request_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Move dead-lettered outbox turns back to 'pending' with reset attempts and wake worker (B-5)."""
        now = time.time()
        replayed = 0
        with self._lock:
            db = self._ensure_db()
            if request_ids is not None:
                clean_ids = [str(rid).strip() for rid in request_ids if str(rid or "").strip()]
                if clean_ids:
                    placeholders = ",".join("?" for _ in clean_ids)
                    cur = db.execute(
                        f"UPDATE outbox_turns "
                        f"SET status='pending', attempts=0, next_retry_at=0, last_error='', updated_at=? "
                        f"WHERE status='dead' AND request_id IN ({placeholders})",
                        (now, *clean_ids),
                    )
                    replayed = max(0, cur.rowcount or 0)
            elif limit is not None:
                lim = max(0, int(limit))
                if lim > 0:
                    cur = db.execute(
                        "UPDATE outbox_turns "
                        "SET status='pending', attempts=0, next_retry_at=0, last_error='', updated_at=? "
                        "WHERE request_id IN ("
                        "  SELECT request_id FROM outbox_turns WHERE status='dead' "
                        "  ORDER BY created_at ASC LIMIT ?"
                        ")",
                        (now, lim),
                    )
                    replayed = max(0, cur.rowcount or 0)
            else:
                cur = db.execute(
                    "UPDATE outbox_turns "
                    "SET status='pending', attempts=0, next_retry_at=0, last_error='', updated_at=? "
                    "WHERE status='dead'",
                    (now,),
                )
                replayed = max(0, cur.rowcount or 0)
            db.commit()

        if replayed > 0:
            if self._auto_start:
                self.start()
            self._wake.set()
            logger.bind(tag=TAG).info(f"Anima 本地发件箱已重放 {replayed} 条 dead 死信记录")
        return {
            "ok": True,
            "replayed": replayed,
            "stats": self.stats(),
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
    target_db = str(_GLOBAL_CONFIG.get("outbox_db") or os.path.join(xz_common.STATE_DIR, "anima_outbox.sqlite"))
    with _outbox_lock:
        if _default_outbox is not None and _default_outbox.db_path != target_db:
            _default_outbox.close()
            _default_outbox = None
        if _default_outbox is None:
            _default_outbox = AnimaOutbox(target_db, auto_start=auto_start)
        if auto_start:
            _default_outbox.start()
        return _default_outbox


def push_turns_async(turns_payload: list) -> bool:
    """Persist turns into durable local SQLite outbox and wake background delivery worker."""
    if not turns_payload:
        return False
    try:
        outbox = get_default_outbox(auto_start=True)
        return outbox.enqueue(turns_payload) > 0
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"写入 Anima 本地发件箱失败: {exc}")
        return False


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
        return {"sent": 0, "failed": 0, "dead": 0, "remaining": -1}


def get_outbox_stats() -> dict[str, Any]:
    try:
        return get_default_outbox(auto_start=False).stats()
    except Exception:
        return {
            "db_path": str(_GLOBAL_CONFIG.get("outbox_db") or ""),
            "pending": 0,
            "dead": 0,
            "corrupt": 0,
            "sent": 0,
            "max_retries": 8,
            "dead_ttl_days": 14.0,
            "last_error": "",
            "worker_alive": False,
        }


def replay_dead_outbox_turns(
    *,
    limit: int | None = None,
    request_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Module-level helper for 18080 console to replay dead-lettered Anima outbox turns (B-5)."""
    try:
        outbox = get_default_outbox(auto_start=True)
        return outbox.replay_dead_turns(limit=limit, request_ids=request_ids)
    except Exception as exc:
        err = _sanitize_err(exc)
        logger.bind(tag=TAG).warning(f"重放 Anima 本地发件箱 dead 记录失败: {err}")
        return {
            "ok": False,
            "replayed": 0,
            "error": err,
            "stats": get_outbox_stats(),
        }


def _has_real_tool_calls(msg: Any) -> bool:
    tc = getattr(msg, "tool_calls", None)
    return isinstance(tc, (list, tuple)) and len(tc) > 0


def _read_occurred_at(msg: Any) -> float | None:
    raw = getattr(msg, "_occurred_at", None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return None


def extract_clean_pairs(messages, *, device_id: str = "", source: str = "esp32", target: MemoryTarget | None = None) -> list:
    """Pair genuine non-temporary user utterances with full assistant replies and turn metadata."""
    pairs = []
    cur_user_msg = None
    cur_uid = None
    cur_user = None
    cur_occurred_at: float | None = None
    cur_used_tools = False
    cur_assistant_parts: list[str] = []

    def _emit_current():
        if not (cur_user and cur_assistant_parts):
            return
        if cur_uid:
            req_id = f"xz-{cur_uid}"
        else:
            cached_req = getattr(cur_user_msg, "_anima_fallback_req_id", None) if cur_user_msg is not None else None
            if cached_req:
                req_id = str(cached_req)
            else:
                req_id = _new_request_id()
                if cur_user_msg is not None:
                    try:
                        cur_user_msg._anima_fallback_req_id = req_id
                    except Exception:
                        pass
        pair: dict[str, Any] = {
            "request_id": req_id,
            "user_message": cur_user,
            "assistant_reply": " ".join(cur_assistant_parts).strip(),
            "source": source or "esp32",
            "used_tools": bool(cur_used_tools),
        }
        if cur_occurred_at is not None:
            pair["occurred_at"] = cur_occurred_at
        if device_id:
            pair["device_id"] = str(device_id).strip()[:64]
        if target is not None:
            pair["_anima_character_id"] = target.character_id
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
            cur_user_msg = m
            raw_uid = getattr(m, "uniq_id", None)
            uid_s = str(raw_uid).strip()[:64] if raw_uid is not None else ""
            cur_uid = "" if uid_s.lower() in ("none", "null") else uid_s
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
        self._base_character_id = str(cfg.get("character_id") or "xiaozhi").strip()
        _GLOBAL_CONFIG["character_id"] = self._base_character_id
        raw_dev_map = cfg.get("device_character_map")
        if raw_dev_map is None:
            raw_dev_map = os.environ.get("ANIMA_DEVICE_CHARACTER_MAP", "")
        self._device_character_map = _parse_device_character_map(raw_dev_map)
        raw_iso = cfg.get("isolate_character_by_device")
        if raw_iso is None:
            raw_iso = (os.environ.get("ANIMA_ISOLATE_CHARACTER_BY_DEVICE") or "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
        self._isolate_character_by_device = bool(raw_iso)
        _GLOBAL_CONFIG["device_character_map"] = dict(self._device_character_map)
        _GLOBAL_CONFIG["isolate_character_by_device"] = self._isolate_character_by_device
        if cfg.get("token_file"):
            _GLOBAL_CONFIG["token_file"] = str(cfg["token_file"])
        if cfg.get("outbox_db"):
            _GLOBAL_CONFIG["outbox_db"] = str(cfg["outbox_db"])

        # P1-9 + C-6: Per-phase HTTP timeout + outer asyncio.wait_for budget + circuit breaker +
        # per-device recall cache with short-budget (300ms) synchronous attempt on cache hit.
        http_timeout = max(0.2, float(cfg.get("recall_http_timeout", 0.8)))
        connect_timeout = max(0.05, min(http_timeout, float(cfg.get("recall_connect_timeout", 0.2))))
        self._http_timeout = httpx.Timeout(http_timeout, connect=connect_timeout)
        self._recall_budget_seconds = max(0.05, float(cfg.get("recall_budget_seconds", 0.6)))
        self._bg_budget_seconds = max(self._recall_budget_seconds, float(cfg.get("recall_bg_budget_seconds", 0.8)))

        has_explicit_swr = "stale_while_revalidate" in cfg
        has_explicit_warm_budget = "recall_warm_budget_seconds" in cfg
        self._stale_while_revalidate = bool(cfg.get("stale_while_revalidate", False))
        if has_explicit_warm_budget:
            self._warm_recall_budget_seconds = max(0.0, float(cfg["recall_warm_budget_seconds"]))
        elif has_explicit_swr and self._stale_while_revalidate:
            # Explicit legacy SWR=True without warm budget preserves zero-wait SWR compatibility
            self._warm_recall_budget_seconds = 0.0
        else:
            # C-6 default: attempt synchronous recall within 300ms even when cache hits,
            # falling back to the cached value on timeout (avoids "always 1 turn late")
            self._warm_recall_budget_seconds = min(self._recall_budget_seconds, 0.3)

        self._cache_ttl_seconds = max(1.0, float(cfg.get("recall_cache_ttl_seconds", 600.0)))
        self._max_cached_devices = max(8, int(cfg.get("recall_max_cached_devices", 64)))

        # Circuit breaker parameters
        self._cb_failure_threshold = max(1, int(cfg.get("recall_cb_threshold", 3)))
        self._cb_cooldown_seconds = max(0.1, float(cfg.get("recall_cb_cooldown_seconds", 30.0)))
        self._cb_consecutive_failures = 0
        self._cb_open_until = 0.0

        # A delayed refresh can only publish to its original device and archive.
        self._recall_cache: dict[MemoryTarget, tuple[str, float]] = {}
        self._recall_gen: dict[MemoryTarget, int] = {}
        self._bg_refresh_tasks: dict[MemoryTarget, asyncio.Task] = {}
        self._pending_refresh_queries: dict[MemoryTarget, tuple[str, int] | None] = {}

        self._recall_stats: dict[str, Any] = {
            "sync_calls": 0,
            "warm_sync_hits": 0,
            "warm_fallback_hits": 0,
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
            f"(角色: {_GLOBAL_CONFIG['character_id']}, 冷预算: {self._recall_budget_seconds:.2f}s, "
            f"温预算: {self._warm_recall_budget_seconds:.2f}s, "
            f"SWR={'on' if self._stale_while_revalidate else 'off'})"
        )

    def _resolve_device_key(self, device_id: str | None = None) -> str:
        raw = device_id
        if raw is None:
            ctx_dev = _CURRENT_DEVICE_ID_VAR.get()
            raw = ctx_dev if ctx_dev else getattr(self, "role_id", None)
        cleaned = str(raw or "").strip().lower()
        return cleaned or "__default__"

    @property
    def _cached_context(self) -> str | None:
        entry = self._recall_cache.get(self.memory_target())
        return entry[0] if entry is not None else None

    @property
    def _bg_refresh_task(self) -> asyncio.Task | None:
        return self._bg_refresh_tasks.get(self.memory_target())

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

    def _record_recall_success(
        self,
        target: MemoryTarget,
        ctx: str,
        elapsed_ms: float,
        *,
        gen: int | None = None,
    ) -> None:
        self._cb_consecutive_failures = 0
        self._cb_open_until = 0.0
        self._recall_stats["last_latency_ms"] = round(elapsed_ms, 2)
        if gen is None or gen >= self._recall_gen.get(target, 0):
            self._recall_cache[target] = (ctx, time.monotonic())
            if len(self._recall_cache) > self._max_cached_devices:
                oldest_dev = min(self._recall_cache.items(), key=lambda kv: kv[1][1])[0]
                if oldest_dev != target:
                    self._recall_cache.pop(oldest_dev, None)

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

    def get_recall_stats(self, device_id: str | None = None, *, target: MemoryTarget | None = None) -> dict[str, Any]:
        target = target or self.memory_target(device_id)
        return {
            **self._recall_stats,
            "circuit_open": self._is_circuit_open(),
            "consecutive_failures": self._cb_consecutive_failures,
            "cache_warm": target in self._recall_cache,
            "cached_devices": len({key.device_key for key in self._recall_cache}),
            "budget_seconds": self._recall_budget_seconds,
            "warm_budget_seconds": self._warm_recall_budget_seconds,
            "stale_while_revalidate": self._stale_while_revalidate,
        }

    def _resolve_character_id(self, dev_key: str) -> str:
        return resolve_character_id_for_device(
            None if dev_key == "__default__" else dev_key,
            base_character_id=self._base_character_id,
            device_map=self._device_character_map,
            isolate_by_device=self._isolate_character_by_device,
        )

    def memory_target(self, device_id: str | None = None) -> MemoryTarget:
        device_key = self._resolve_device_key(device_id)
        return connection_memory_target(device_key, self._resolve_character_id(device_key))

    async def _fetch_recall_once(
        self,
        query: str,
        token: str,
        target: MemoryTarget,
        *,
        gen: int | None = None,
    ) -> str:
        url = _GLOBAL_CONFIG["service_url"] + "/xiaozhi/recall"
        client = self._get_async_client()
        t0 = time.monotonic()
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "character_id": target.character_id,
                "query": query or "",
            },
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        data = resp.json()
        if target.archive == "tavern" and data.get("character_id") != target.character_id:
            raise ValueError("Anima 召回返回了不匹配的酒馆存档")
        mood_str = str(data.get("mood") or "")
        ctx = (data.get("context_text") or "").strip()
        # 剥离重复的【角色身份设定】段落，以小智主配置 prompt 为唯一人设权威，避免双倍 Token 与人设冲突
        ctx = re.sub(
            r"^【角色身份设定（来自 Companion Studio）】.*?(?=\n\n【|\Z)",
            "",
            ctx,
            flags=re.DOTALL,
        ).strip()
        # 召回层敏感实体自动拦截（防语音外放泄露密钥、手机号、银行卡、证件号、验证码）
        redacted_cnt = 0
        try:
            import recall_guard

            ctx, rg_meta = recall_guard.sanitize_recall_context(ctx)
            redacted_cnt = int(rg_meta.get("redacted_count") or 0)
        except Exception:
            pass
        # 4.1 #4: Check generation freshness BEFORE mutating device Anima mood so stale recalls never overwrite newer mood
        is_current_gen = gen is None or gen >= self._recall_gen.get(target, 0)
        if is_current_gen:
            mei_emotion_router.set_anima_mood(mood_str, device_id=target.mood_key)
        self._record_recall_success(target, ctx, elapsed_ms, gen=gen)
        redact_note = f", 已拦截敏感实体={redacted_cnt}" if redacted_cnt else ""
        logger.bind(tag=TAG).info(
            f"Anima Brain 极速召回成功 ({elapsed_ms:.0f}ms): 事实={data.get('facts_count', 0)}, "
            f"待跟进={data.get('open_loops_count', 0)}, 状态={data.get('mood', '')}{redact_note}"
        )
        return ctx

    async def _bg_refresh_loop(self, target: MemoryTarget, initial_query: str, initial_gen: int) -> None:
        next_item: tuple[str, int] | None = (initial_query, initial_gen)
        try:
            while next_item is not None:
                q, req_gen = next_item
                next_item = None
                if self._is_circuit_open():
                    self._pending_refresh_queries.pop(target, None)
                    self._recall_stats["cb_short_circuits"] += 1
                    return
                token = _read_token()
                if not token:
                    self._pending_refresh_queries.pop(target, None)
                    return
                self._recall_stats["bg_refreshes"] += 1
                try:
                    await asyncio.wait_for(
                        self._fetch_recall_once(q, token, target, gen=req_gen),
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

                queued_next = self._pending_refresh_queries.pop(target, None)
                if queued_next is not None:
                    next_item = queued_next
        finally:
            cur_task = asyncio.current_task()
            if self._bg_refresh_tasks.get(target) is cur_task:
                self._bg_refresh_tasks.pop(target, None)

    def _schedule_bg_refresh(self, query: str, target: MemoryTarget, *, gen: int | None = None) -> None:
        if self._is_circuit_open():
            self._recall_stats["cb_short_circuits"] += 1
            return
        req_gen = self._recall_gen.get(target, 0) if gen is None else gen
        existing = self._bg_refresh_tasks.get(target)
        if existing is not None and not existing.done():
            self._pending_refresh_queries[target] = (query, req_gen)
            return
        try:
            task = asyncio.create_task(self._bg_refresh_loop(target, query, req_gen))
            self._bg_refresh_tasks[target] = task

            def _consume_exc(t: asyncio.Task) -> None:
                try:
                    t.exception()
                except (asyncio.CancelledError, Exception):
                    pass

            task.add_done_callback(_consume_exc)
        except RuntimeError:
            pass

    async def query_memory(self, query: str, *, device_id: str | None = None, target: MemoryTarget | None = None) -> str:
        token = _read_token()
        if not token:
            return ""

        now = time.monotonic()
        target = target or self.memory_target(device_id)
        if target.handoff:
            # Keep private handoff text out of the shared, longer-lived cache keys.
            target = replace(target, handoff=())
        cache_entry = self._recall_cache.get(target)
        has_valid_cache = (
            cache_entry is not None and (now - cache_entry[1]) <= self._cache_ttl_seconds
        )
        cached_ctx = cache_entry[0] if has_valid_cache else None

        # Legacy explicit zero-wait SWR mode (only when stale_while_revalidate=True and warm_budget == 0)
        if (
            self._stale_while_revalidate
            and self._warm_recall_budget_seconds <= 0.0
            and cached_ctx is not None
        ):
            self._recall_stats["cache_hits"] += 1
            gen = self._recall_gen.get(target, 0) + 1
            self._recall_gen[target] = gen
            self._schedule_bg_refresh(query or "", target, gen=gen)
            return cached_ctx

        # Circuit breaker open: degrade immediately to this device's cached context (never cross-device)
        if self._is_circuit_open():
            self._recall_stats["cb_short_circuits"] += 1
            if cached_ctx is not None:
                self._recall_stats["cache_hits"] += 1
            return cached_ctx or ""

        gen = self._recall_gen.get(target, 0) + 1
        self._recall_gen[target] = gen

        # C-6: Even when cache hits, attempt a short-budget synchronous recall (default 300ms)
        # and fall back to this device's cached value on timeout/error, avoiding "always 1 turn late".
        if cached_ctx is not None:
            warm_budget = min(self._recall_budget_seconds, self._warm_recall_budget_seconds)
            self._recall_stats["sync_calls"] += 1
            try:
                fresh = await asyncio.wait_for(
                    self._fetch_recall_once(query or "", token, target, gen=gen),
                    timeout=warm_budget,
                )
                self._recall_stats["warm_sync_hits"] += 1
                return fresh
            except asyncio.TimeoutError:
                # 4.1 #4: Short 300ms warm-budget timeout is latency jitter, NOT service unavailability;
                # do NOT trip the global 30s circuit breaker here (let full-budget background refresh judge health).
                self._recall_stats["timeouts"] += 1
                self._recall_stats["cache_hits"] += 1
                self._recall_stats["warm_fallback_hits"] += 1
                logger.bind(tag=TAG).warning(
                    f"Anima Brain 温缓存短预算召回超时 (>{warm_budget:.2f}s)，回退缓存并后台刷新"
                )
                if not self._is_circuit_open():
                    self._schedule_bg_refresh(query or "", target, gen=gen)
                return cached_ctx
            except Exception as e:
                self._record_recall_failure(is_timeout=False)
                self._recall_stats["cache_hits"] += 1
                self._recall_stats["warm_fallback_hits"] += 1
                logger.bind(tag=TAG).warning(f"Anima Brain 温缓存召回异常，回退缓存: {e}")
                return cached_ctx

        # Cold cache miss for this device: wait up to full recall budget
        self._recall_stats["sync_calls"] += 1
        try:
            return await asyncio.wait_for(
                self._fetch_recall_once(query or "", token, target, gen=gen),
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
        return ""

    async def save_memory(self, msgs, session_id=None, *, device_id: str | None = None, target: MemoryTarget | None = None):
        if device_id is not None:
            dev_id = str(device_id or "").strip()
        else:
            ctx_dev = _CURRENT_DEVICE_ID_VAR.get()
            dev_id = str(ctx_dev or getattr(self, "role_id", "") or "").strip()
        target = target or self.memory_target(dev_id)
        pairs = extract_clean_pairs(msgs, device_id=dev_id, target=target)
        if pairs and push_turns_async(pairs):
            target.acknowledge_handoff()
        return "ok"


class _DeviceBoundMemoryProxy:
    """Lightweight per-connection proxy binding an immutable device_id over a shared MemoryProvider singleton (P0-5)."""

    def __init__(self, base: MemoryProvider, device_id: str | None = None, *, target: MemoryTarget):
        self._base = base
        self._device_id = str(device_id or "").strip()
        self.target = target

    @property
    def role_id(self) -> str:
        return self._device_id or str(getattr(self._base, "role_id", "") or "")

    @role_id.setter
    def role_id(self, value: str) -> None:
        self._device_id = str(value or "").strip()

    @property
    def _cached_context(self) -> str | None:
        entry = self._base._recall_cache.get(self.target)
        return entry[0] if entry is not None else None

    @property
    def _bg_refresh_task(self) -> asyncio.Task | None:
        return self._base._bg_refresh_tasks.get(self.target)

    def init_memory(self, role_id=None, *args, **kwargs):
        if role_id:
            self._device_id = str(role_id).strip()
        orig_init = getattr(self._base, "init_memory", None)
        if callable(orig_init):
            if args:
                return orig_init(self._device_id, *args, **kwargs)
            return orig_init(role_id=self._device_id, **kwargs)
        return None

    async def query_memory(self, query: str, *, device_id: str | None = None) -> str:
        eff_dev = device_id if device_id is not None else (self._device_id or None)
        return await self._base.query_memory(query, device_id=eff_dev, target=self.target)

    async def save_memory(self, msgs, session_id=None, *, device_id: str | None = None):
        eff_dev = device_id if device_id is not None else (self._device_id or None)
        return await self._base.save_memory(msgs, session_id=session_id, device_id=eff_dev, target=self.target)

    def get_recall_stats(self, device_id: str | None = None) -> dict[str, Any]:
        eff_dev = device_id if device_id is not None else (self._device_id or None)
        return self._base.get_recall_stats(device_id=eff_dev, target=self.target)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def bind_memory_provider(memory_obj: Any, device_id: str | None, *, target: MemoryTarget | None = None) -> Any:
    """Wrap a shared MemoryProvider singleton in a per-connection _DeviceBoundMemoryProxy (P0-5)."""
    dev = str(device_id or "").strip()
    if isinstance(memory_obj, _DeviceBoundMemoryProxy):
        if (not dev or memory_obj._device_id == dev) and (target is None or target is memory_obj.target):
            return memory_obj
        memory_obj = memory_obj._base
    if isinstance(memory_obj, MemoryProvider):
        return _DeviceBoundMemoryProxy(memory_obj, dev, target=target or memory_obj.memory_target(dev))
    return memory_obj

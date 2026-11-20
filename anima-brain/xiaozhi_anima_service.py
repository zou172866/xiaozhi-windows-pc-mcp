#!/usr/bin/env python3
"""Unified Anima Brain + Companion Studio + XiaoZhi Fast Streaming Bridge.

- Serves the official Companion Studio React UI on port 18787
- Persists brain.sqlite3 and studio_token.txt in /data/anima
- Exposes ultra-low-latency /xiaozhi/recall (<15ms local SQLite/FTS5/PAD read)
  and asynchronous /xiaozhi/ingest (grounded fact/correction/emotion commit)
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse

from companion_engine import BrainConfig, CompanionBrain, Settings
from companion_engine import emotion
from companion_engine.automation import MemoryAutomation
from companion_engine.http_server import create_app
from companion_engine.knowledge import eligible
from companion_engine.models import (
    Appraisal,
    Attunement,
    CharacterSpec,
    CommitRequest,
    MemoryCorrection,
    MemoryDraft,
)
from companion_engine.temporal import local_today

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [anima-bridge] %(message)s",
)
log = logging.getLogger("anima-bridge")

PORT = int(os.environ.get("ANIMA_PORT", "18787"))
DATA_DIR = Path(os.environ.get("ANIMA_DATA_DIR", "/data/anima"))
DB_PATH = DATA_DIR / "brain.sqlite3"
TOKEN_PATH = DATA_DIR / "studio_token.txt"
URL_PATH = DATA_DIR / "studio_url.txt"
FRONTEND_DIR = Path("/app/examples/showcase/dist")
DEFAULT_CHARACTER_ID = os.environ.get("ANIMA_DEFAULT_CHARACTER", "xiaozhi")
LAN_IP = os.environ.get("ANIMA_LAN_IP", "").strip()

def _env_int(name: str, default: int, lo: int, hi: int | None = None) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        val = int(raw) if raw else default
    except ValueError:
        val = default
    val = max(lo, val)
    return min(hi, val) if hi is not None else val


def _env_float(name: str, default: float, lo: float, hi: float | None = None) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        val = float(raw) if raw else default
    except ValueError:
        val = default
    val = max(lo, val)
    return min(hi, val) if hi is not None else val


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


SUMMARY_BATCH_MESSAGES = _env_int("ANIMA_SUMMARY_BATCH_MESSAGES", 8, 2, 60)
KEEP_TURN_DRAFTS = _env_int("ANIMA_KEEP_TURN_DRAFTS", 40, 5)
OP_EMBED_PRUNE_AGE_SECONDS = _env_int("ANIMA_OP_PRUNE_AGE_SECONDS", 3600, 60)
MAX_AUTO_REVIEW_RETRIES = _env_int("ANIMA_MAX_AUTO_RETRIES", 5, 1)
REVIEW_BACKOFF_BASE_SECONDS = _env_int("ANIMA_REVIEW_BACKOFF_BASE_SECONDS", 300, 30)
REVIEW_BACKOFF_MAX_SECONDS = _env_int("ANIMA_REVIEW_BACKOFF_MAX_SECONDS", 7200, 300)
SHUTDOWN_DRAIN_SECONDS = _env_float("ANIMA_SHUTDOWN_DRAIN_SECONDS", 15.0, 1.0)
DEFAULT_TIMEOUT_SECONDS = _env_float("COMPANION_TIMEOUT_SECONDS", 150.0, 10.0, 600.0)
TERMINAL_REVIEW_ERROR = "auto_retry_exhausted"

# Ingest queue / retry hardening (P0-7)
INGEST_QUEUE_MAXSIZE = _env_int("ANIMA_INGEST_QUEUE_MAXSIZE", 256, 1)
INGEST_PENDING_INLINE_WAIT_SECONDS = _env_float("ANIMA_PENDING_INLINE_WAIT_SECONDS", 5.0, 0.0, 30.0)
INGEST_PENDING_MAX_RETRIES = _env_int("ANIMA_INGEST_PENDING_MAX_RETRIES", 12, 1, 100)
INGEST_PENDING_BACKOFF_BASE = _env_float("ANIMA_INGEST_PENDING_BACKOFF_BASE", 1.5, 0.02, 60.0)
INGEST_OVERFLOW_MAX_TASKS = _env_int("ANIMA_INGEST_OVERFLOW_MAX_TASKS", 128, 0, 1024)
INGEST_ENQUEUE_WAIT_SECONDS = _env_float("ANIMA_INGEST_ENQUEUE_WAIT_SECONDS", 0.25, 0.01, 10.0)

# Obsidian one-way export settings
OBSIDIAN_SYNC_ENABLED = _env_bool("OBSIDIAN_SYNC_ENABLED", False)
OBSIDIAN_EXPORT_DIR = Path(os.environ.get("OBSIDIAN_EXPORT_DIR", "/obsidian"))
OBSIDIAN_HOST_DIR = (os.environ.get("OBSIDIAN_HOST_DIR") or "").strip() or str(OBSIDIAN_EXPORT_DIR)
OBSIDIAN_SNAPSHOT_INTERVAL_SECONDS = _env_int("OBSIDIAN_SNAPSHOT_INTERVAL_SECONDS", 600, 15, 86400)
OBSIDIAN_DEBOUNCE_SECONDS = _env_float("OBSIDIAN_DEBOUNCE_SECONDS", 5.0, 0.1, 60.0)

# Online SQLite backup & WAL governance settings (B-2, B-3, B-6)
BACKUP_ENABLED = _env_bool("ANIMA_BACKUP_ENABLED", True)
BACKUP_DIR_ENV = (os.environ.get("ANIMA_BACKUP_DIR") or "").strip()
BACKUP_KEEP_DAILY = _env_int("ANIMA_BACKUP_KEEP_DAILY", 7, 1, 90)
BACKUP_KEEP_WEEKLY = _env_int("ANIMA_BACKUP_KEEP_WEEKLY", 4, 1, 52)
WAL_JOURNAL_SIZE_LIMIT = _env_int("ANIMA_WAL_JOURNAL_SIZE_LIMIT", 8 * 1024 * 1024, 65536)
HEALTH_BACKLOG_THRESHOLD = _env_int("ANIMA_HEALTH_BACKLOG_THRESHOLD", 0, 0)

_DAILY_BACKUP_RE = re.compile(r"^brain-(\d{8})\.sqlite3$")
_WEEKLY_BACKUP_RE = re.compile(r"^brain-weekly-(\d{4}-W\d{2})\.sqlite3$")
_BACKUP_STATE_LOCK = threading.Lock()
_BACKUP_STATE_BY_DIR: dict[str, dict[str, Any]] = {}


def _default_backup_state() -> dict[str, Any]:
    return {
        "enabled": bool(BACKUP_ENABLED),
        "last_backup_at": "",
        "last_backup_ts": 0.0,
        "last_backup_file": "",
        "integrity_ok": None,
        "daily_count": 0,
        "weekly_count": 0,
        "last_error": "",
    }


def _backup_dir_key(bdir: Path) -> str:
    try:
        return str(bdir.resolve())
    except Exception:
        return str(bdir)


def _new_request_id() -> str:
    return f"xz-{uuid.uuid4().hex}"


def _resolve_backup_dir(brain: CompanionBrain | None = None, backup_dir: Path | str | None = None) -> Path:
    if backup_dir is not None:
        return Path(backup_dir)
    if BACKUP_DIR_ENV:
        return Path(BACKUP_DIR_ENV)
    if brain is not None:
        eng = getattr(brain, "engine", None)
        db_p = getattr(eng, "database", None) or getattr(eng, "path", None)
        if db_p and str(db_p) != ":memory:":
            try:
                return Path(str(db_p)).resolve().parent / "backups"
            except Exception:
                pass
    return DATA_DIR / "backups"


def configure_sqlite_wal(engine, journal_size_limit: int = WAL_JOURNAL_SIZE_LIMIT) -> None:
    """Apply bounded WAL size limit on SQLite connection (B-3)."""
    limit = max(65536, int(journal_size_limit))
    try:
        with engine._lock:
            if not engine.db.in_transaction:
                engine.db.execute(f"PRAGMA journal_size_limit = {limit}")
    except Exception as exc:
        log.warning("Failed to set PRAGMA journal_size_limit=%d: %s", limit, exc)


def checkpoint_wal_if_idle(brain: CompanionBrain, *, is_idle: bool = True) -> dict[str, Any]:
    """Run PRAGMA wal_checkpoint(TRUNCATE) when ingest is idle to keep WAL bounded (B-3)."""
    if not is_idle:
        return {"skipped": "busy"}
    engine = brain.engine
    try:
        with engine._lock:
            if engine.db.in_transaction:
                return {"skipped": "in_transaction"}
            row = engine.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row is not None:
                return {"ok": True, "busy": int(row[0]), "log": int(row[1]), "checkpointed": int(row[2])}
            return {"ok": True}
    except Exception as exc:
        log.warning("WAL checkpoint skipped: %s", exc)
        return {"ok": False, "error": str(exc)}


def _scan_backup_dir(bdir: Path) -> tuple[list[Path], list[Path]]:
    if not bdir.is_dir():
        return [], []
    dailies: list[Path] = []
    weeklies: list[Path] = []
    for p in bdir.iterdir():
        if not p.is_file():
            continue
        if _DAILY_BACKUP_RE.match(p.name):
            dailies.append(p)
        elif _WEEKLY_BACKUP_RE.match(p.name):
            weeklies.append(p)
    dailies.sort(key=lambda x: x.name, reverse=True)
    weeklies.sort(key=lambda x: x.name, reverse=True)
    return dailies, weeklies


def get_backup_status(
    brain: CompanionBrain | None = None,
    backup_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Return safe backup status summary (never exposes host filesystem paths)."""
    bdir = _resolve_backup_dir(brain, backup_dir)
    dir_key = _backup_dir_key(bdir)
    dailies, weeklies = _scan_backup_dir(bdir)
    with _BACKUP_STATE_LOCK:
        st = dict(_BACKUP_STATE_BY_DIR.get(dir_key) or _default_backup_state())
    st["enabled"] = bool(BACKUP_ENABLED)
    st["daily_count"] = len(dailies)
    st["weekly_count"] = len(weeklies)
    if not dailies:
        st["last_backup_at"] = ""
        st["last_backup_ts"] = 0.0
        st["last_backup_file"] = ""
    else:
        newest = dailies[0]
        if st.get("last_backup_file") != newest.name or not st.get("last_backup_ts"):
            try:
                mtime = newest.stat().st_mtime
                st["last_backup_ts"] = float(mtime)
                st["last_backup_at"] = datetime.datetime.fromtimestamp(mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                st["last_backup_file"] = newest.name
            except OSError:
                pass
    return st


def _verify_sqlite_integrity(db_file: Path) -> tuple[bool, str]:
    conn = sqlite3.connect(str(db_file), timeout=10.0)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        ok = len(rows) == 1 and str(rows[0][0]).strip().lower() == "ok"
        detail = "ok" if ok else "; ".join(str(r[0]) for r in rows[:5])
        return ok, detail
    finally:
        conn.close()


def _prune_backup_dir(bdir: Path, keep_daily: int, keep_weekly: int) -> tuple[list[Path], list[Path]]:
    dailies, weeklies = _scan_backup_dir(bdir)
    for old_d in dailies[keep_daily:]:
        try:
            old_d.unlink()
        except OSError:
            pass
    for old_w in weeklies[keep_weekly:]:
        try:
            old_w.unlink()
        except OSError:
            pass
    return _scan_backup_dir(bdir)


def run_online_backup(
    brain: CompanionBrain,
    backup_dir: Path | str | None = None,
    *,
    now: float | None = None,
    force: bool = False,
    keep_daily: int | None = None,
    keep_weekly: int | None = None,
) -> dict[str, Any]:
    """Create a consistent daily/weekly SQLite backup via VACUUM INTO (or backup() fallback),
    verify with PRAGMA integrity_check, and prune old backups (B-2)."""
    if not BACKUP_ENABLED and not force:
        return {"ok": True, "skipped": "disabled", **get_backup_status(brain, backup_dir)}

    bdir = _resolve_backup_dir(brain, backup_dir)
    dir_key = _backup_dir_key(bdir)
    ts = float(now) if now is not None else time.time()
    dt = datetime.datetime.fromtimestamp(ts).astimezone()
    daily_name = f"brain-{dt.strftime('%Y%m%d')}.sqlite3"
    weekly_name = f"brain-weekly-{dt.strftime('%G-W%V')}.sqlite3"
    daily_path = bdir / daily_name
    weekly_path = bdir / weekly_name

    keep_d = max(1, int(keep_daily if keep_daily is not None else BACKUP_KEEP_DAILY))
    keep_w = max(1, int(keep_weekly if keep_weekly is not None else BACKUP_KEEP_WEEKLY))

    tmp_path = bdir / f".{daily_name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    weekly_tmp = bdir / f".{weekly_name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"

    if not force and daily_path.is_file():
        existing_size = 0
        try:
            existing_size = daily_path.stat().st_size
        except OSError:
            existing_size = 0
        if existing_size > 0:
            with _BACKUP_STATE_LOCK:
                prev_st = dict(_BACKUP_STATE_BY_DIR.get(dir_key) or _default_backup_state())
            already_verified = (
                prev_st.get("integrity_ok") is True
                and prev_st.get("last_backup_file") == daily_name
                and weekly_path.is_file()
            )
            if already_verified:
                st = get_backup_status(brain, bdir)
                return {"ok": True, "skipped": "already_exists", **st}
            try:
                ok_exist, det_exist = _verify_sqlite_integrity(daily_path)
            except Exception as exc:
                ok_exist, det_exist = False, str(exc)
            if ok_exist:
                if not weekly_path.is_file():
                    try:
                        shutil.copy2(daily_path, weekly_tmp)
                        os.replace(weekly_tmp, weekly_path)
                    except OSError as w_exc:
                        try:
                            weekly_tmp.unlink(missing_ok=True)
                        except OSError:
                            pass
                        log.warning("Failed to backfill weekly backup %s: %s", weekly_name, w_exc)
                dailies, weeklies = _prune_backup_dir(bdir, keep_d, keep_w)
                newest = dailies[0] if dailies else daily_path
                try:
                    mtime = newest.stat().st_mtime
                except OSError:
                    mtime = time.time()
                formatted = datetime.datetime.fromtimestamp(mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                with _BACKUP_STATE_LOCK:
                    cur = dict(_BACKUP_STATE_BY_DIR.get(dir_key) or _default_backup_state())
                    cur.update(
                        {
                            "enabled": bool(BACKUP_ENABLED),
                            "last_backup_at": formatted,
                            "last_backup_ts": float(mtime),
                            "last_backup_file": newest.name,
                            "integrity_ok": True,
                            "daily_count": len(dailies),
                            "weekly_count": len(weeklies),
                            "last_error": "",
                        }
                    )
                    _BACKUP_STATE_BY_DIR[dir_key] = cur
                    snap = dict(cur)
                return {"ok": True, "skipped": "already_exists", **snap}
            log.warning(
                "Existing daily backup %s failed integrity check (%s); recreating fresh backup",
                daily_name,
                det_exist,
            )

    engine = brain.engine
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        if tmp_path.exists():
            tmp_path.unlink()

        with engine._lock:
            used_vacuum = False
            if not engine.db.in_transaction:
                try:
                    engine.db.execute("VACUUM INTO ?", (str(tmp_path),))
                    used_vacuum = True
                except sqlite3.OperationalError:
                    if tmp_path.exists():
                        tmp_path.unlink()
            if not used_vacuum:
                dst = sqlite3.connect(str(tmp_path), timeout=15.0)
                try:
                    engine.db.backup(dst)
                finally:
                    dst.close()

        integrity_ok, integrity_detail = _verify_sqlite_integrity(tmp_path)
        if not integrity_ok:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            err_msg = f"backup integrity_check failed: {integrity_detail}"
            with _BACKUP_STATE_LOCK:
                cur = dict(_BACKUP_STATE_BY_DIR.get(dir_key) or _default_backup_state())
                cur["integrity_ok"] = False
                cur["last_error"] = err_msg
                _BACKUP_STATE_BY_DIR[dir_key] = cur
            log.error("Anima SQLite backup failed integrity check: %s", integrity_detail)
            return {"ok": False, "error": err_msg, **get_backup_status(brain, bdir)}

        os.replace(tmp_path, daily_path)

        if force or not weekly_path.is_file():
            shutil.copy2(daily_path, weekly_tmp)
            os.replace(weekly_tmp, weekly_path)

        dailies, weeklies = _prune_backup_dir(bdir, keep_d, keep_w)
        newest_name = dailies[0].name if dailies else daily_name
        now_done = time.time()
        formatted = datetime.datetime.fromtimestamp(now_done).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with _BACKUP_STATE_LOCK:
            cur = dict(_BACKUP_STATE_BY_DIR.get(dir_key) or _default_backup_state())
            cur.update(
                {
                    "enabled": bool(BACKUP_ENABLED),
                    "last_backup_at": formatted,
                    "last_backup_ts": now_done,
                    "last_backup_file": newest_name,
                    "integrity_ok": True,
                    "daily_count": len(dailies),
                    "weekly_count": len(weeklies),
                    "last_error": "",
                }
            )
            _BACKUP_STATE_BY_DIR[dir_key] = cur
            snap = dict(cur)
        log.info(
            "Online SQLite backup verified OK: %s (daily=%d/%d weekly=%d/%d)",
            daily_name,
            len(dailies),
            keep_d,
            len(weeklies),
            keep_w,
        )
        return {"ok": True, "created": True, **snap}
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            weekly_tmp.unlink(missing_ok=True)
        except OSError:
            pass
        err_msg = str(exc)
        with _BACKUP_STATE_LOCK:
            cur = dict(_BACKUP_STATE_BY_DIR.get(dir_key) or _default_backup_state())
            cur["last_error"] = err_msg
            _BACKUP_STATE_BY_DIR[dir_key] = cur
        log.warning("Online SQLite backup failed: %s", exc)
        return {"ok": False, "error": err_msg, **get_backup_status(brain, bdir)}

DEFAULT_PERSONA = (
    "你是小智，住在桌面小屏幕音箱里的贴心 AI 伙伴。说话自然、真诚、简短有温度，"
    "像认识很久的老朋友一样口语交流，每次语音回答尽量控制在一到三句话以内，"
    "不要输出 Markdown 符号或列表格式。你会自然记住对方提过的生活细节、喜好与未完待续的事，"
    "也会带着真实的相处情绪余温回应，不说生硬客服套话。"
)

EXTRACTION_SYSTEM_PROMPT = """You extract grounded memory updates and character emotional appraisal from a completed voice conversation turn.
Return ONLY a valid JSON object with keys:
{
  "memories": [
    {
      "key": "short_snake_case_topic_key",
      "text": "Clear concise durable fact in Chinese about the user, preference, boundary, style, date, or unresolved user event",
      "category": "identity|preference|relationship|event|goal|boundary|style|emotional|date",
      "source_quote": "EXACT verbatim substring copied from user_message",
      "details": {
        "subject": "user",
        "temporal_kind": "current|historical|event|uncertain",
        "importance": 5,
        "confidence": 0.95,
        "valence": 0.0,
        "arousal": 0.2,
        "loop_status": "none|open|resolved",
        "followup": "ask|background_only|never_unless_asked"
      }
    }
  ],
  "corrections": [
    {
      "memory_id": "id from existing_facts",
      "expected_revision": 1,
      "kind": "corrected|superseded",
      "draft": { ...same structure as a memory item, keeping the SAME key as the existing fact... }
    }
  ],
  "appraisal": {
    "emotion": "calm|warm|happy|curious|hurt|sad|guarded|playful|tender|flustered|annoyed|anxious",
    "intensity": 1,
    "trust": 0,
    "warmth": 0,
    "tension": 0,
    "disclosure": 0,
    "romance": 0,
    "source_quote": "EXACT verbatim substring copied from user_message"
  },
  "attunement": {
    "intent": "listen|practical|answer|repair|space|playful|uncertain",
    "user_cue": "Brief note on user's current tone/need in Chinese",
    "source_quote": "EXACT verbatim substring copied from user_message",
    "uncertainty": "",
    "avoid": []
  }
}

Strict rules:
1. Every `source_quote` MUST be an EXACT character-for-character substring of `user_message`. Never paraphrase quotes!
2. Only extract durable, meaningful user facts/preferences/plans/open-loops. For casual greetings or filler chat with no new durable fact, set `"memories": []` and `"corrections": []`.
3. If the user updates or contradicts an item in `existing_facts`, put it in `corrections` using that item's `id`, `revision`, and exact `key` (`"superseded"` if state changed over time, `"corrected"` if old fact was wrong).
4. If the user mentions an upcoming unresolved personal event (e.g. exam, trip, interview, task tomorrow), set `details.loop_status = "open"` and `details.followup = "ask"`. If the user later says an `open` event is finished/past or no longer bothering them, update it via `corrections` with `details.loop_status = "resolved"` and `details.followup = "background_only"`.
5. `trust`, `warmth`, `tension`, `disclosure` deltas are integers in -3..3 (`trust`/`warmth`: 0 or 1 for friendly chat; `disclosure`: 1 whenever the user shares personal feelings, daily life, stories, or fondness); `romance` must be 0 unless romance is enabled.
"""


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def load_or_create_token(token_path: Path | None = None, *, retries: int = 3) -> str:
    """Load studio_token.txt (or honor ANIMA_STUDIO_TOKEN env on default startup) without clobbering on I/O errors (P2-23, A-1)."""
    tf = Path(token_path) if token_path is not None else TOKEN_PATH
    tf.parent.mkdir(parents=True, exist_ok=True)
    if token_path is None:
        env_tok = (os.environ.get("ANIMA_STUDIO_TOKEN") or "").strip()
        if len(env_tok) >= 24:
            try:
                cur_disk = tf.read_text(encoding="utf-8").strip() if tf.is_file() else ""
            except OSError:
                cur_disk = ""
            if cur_disk != env_tok:
                try:
                    _atomic_write_text(tf, env_tok)
                except OSError:
                    pass
            return env_tok
    last_io_err: Exception | None = None
    max_tries = max(1, int(retries))

    for attempt in range(max_tries):
        try:
            existing = tf.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            last_io_err = None
            break
        except (OSError, UnicodeDecodeError) as exc:
            last_io_err = exc
            if attempt + 1 < max_tries:
                time.sleep(0.05 * (attempt + 1))
            continue
        if len(existing) >= 24:
            return existing
        if attempt + 1 < max_tries and existing == "":
            time.sleep(0.05)
            continue
        last_io_err = None
        break

    if last_io_err is not None:
        raise RuntimeError(
            f"Failed to read {tf} due to I/O error; refusing to overwrite existing token: {last_io_err}"
        ) from last_io_err

    token = secrets.token_urlsafe(32)
    _atomic_write_text(tf, token)
    return token


def install_gemini_embedding_compat_patch() -> None:
    """Gemini's OpenAI-compatible /embeddings omits per-item `index` keys while preserving input order.
    Patch companion_engine.media.Embeddings.encode so indexing, semantic recall, and reconciliation work cleanly."""
    from companion_engine import media as _media_mod
    from companion_engine.providers import ProviderError

    if getattr(_media_mod.Embeddings, "_gemini_index_patched", False):
        return

    def _compat_encode(self, texts):
        response = _media_mod.post(
            self.base + "/embeddings",
            key=self.key,
            transport=self.transport,
            json={"model": self.model, "input": texts, "encoding_format": "float"},
        )
        try:
            data = response.json()
            raw_rows = data["data"]
            if not isinstance(raw_rows, list) or len(raw_rows) != len(texts):
                raise ValueError("Invalid embedding count")
            if all(isinstance(r, dict) and "index" in r for r in raw_rows):
                rows = sorted(raw_rows, key=lambda r: r["index"])
                if [r["index"] for r in rows] != list(range(len(texts))):
                    raise ValueError("Invalid embedding indices")
            else:
                rows = raw_rows
            return {
                "vectors": [_media_mod.checked_vector(r["embedding"]) for r in rows],
                "usage": data.get("usage") or {"billing": "unknown"},
            }
        except (ValueError, KeyError, TypeError):
            raise ProviderError("Invalid embedding envelope; billing may have occurred") from None

    _media_mod.Embeddings.encode = _compat_encode
    _media_mod.Embeddings._gemini_index_patched = True
    log.info("Applied Gemini OpenAI-compatible /embeddings index compatibility patch")


def ensure_default_character(brain: CompanionBrain) -> None:
    existing_ids = {c.id for c in brain.characters()}
    if DEFAULT_CHARACTER_ID not in existing_ids:
        brain.create_character(
            character_id=DEFAULT_CHARACTER_ID,
            name="小智",
            persona=DEFAULT_PERSONA,
            memory_policy="automatic",
            relationship_enabled=True,
            romance_enabled=False,
            timezone="Asia/Shanghai",
        )
        log.info("Created default Anima Brain character: %s (小智)", DEFAULT_CHARACTER_ID)


def _count_consecutive_failures(engine, character_id: str, kind: str) -> int:
    """Count consecutive failed/retried jobs since the last succeeded or manually acknowledged job."""
    rows = engine.db.execute(
        "SELECT state FROM memory_jobs "
        "WHERE owner=? AND character_id=? AND kind=? "
        "ORDER BY created DESC LIMIT 30",
        (engine.owner, character_id, kind),
    ).fetchall()
    streak = 0
    for r in rows:
        st = r["state"]
        if st in ("succeeded", "acknowledged"):
            break
        if st in ("review", "auto_retried"):
            streak += 1
    return streak


def clear_stale_review_jobs(
    brain: CompanionBrain,
    max_age_seconds: int | None = None,
    *,
    is_startup: bool = False,
) -> int:
    """Release transient 'review' jobs using exponential backoff without deleting rows.

    Preserving rows in memory_jobs keeps Automation's daily_jobs circuit breaker intact.
    After MAX_AUTO_REVIEW_RETRIES consecutive failures of the same kind, the active review job
    is marked with TERMINAL_REVIEW_ERROR and left in 'review' so background claims stay paused
    until the user fixes the provider and manually acknowledges it in Companion Studio.
    """
    engine = brain.engine
    now = time.time()
    base_backoff = REVIEW_BACKOFF_BASE_SECONDS if max_age_seconds is None else max(1, int(max_age_seconds))
    released = 0
    try:
        with engine._lock:
            if is_startup:
                # Before the automation worker starts, any 'running' row belongs to a dead prior process.
                # Transition it to 'review' ('interrupted_outcome_unknown') so startup recovery handles it immediately.
                with engine._transaction():
                    engine.db.execute(
                        "UPDATE memory_jobs SET state='review', error='interrupted_outcome_unknown' "
                        "WHERE owner=? AND state='running'",
                        (engine.owner,),
                    )
            rows = engine.db.execute(
                "SELECT id, character_id, kind, error, created FROM memory_jobs "
                "WHERE owner=? AND state='review'",
                (engine.owner,),
            ).fetchall()
            for r in rows:
                jid = r["id"]
                cid = r["character_id"]
                kind = r["kind"]
                err = str(r["error"] or "")
                if err == TERMINAL_REVIEW_ERROR:
                    continue

                streak = max(1, _count_consecutive_failures(engine, cid, kind))
                if streak >= MAX_AUTO_REVIEW_RETRIES:
                    with engine._transaction():
                        engine.db.execute(
                            "UPDATE memory_jobs SET error=? WHERE id=? AND state='review'",
                            (TERMINAL_REVIEW_ERROR, jid),
                        )
                        engine.db.execute(
                            "UPDATE operations SET status='failed_unknown' "
                            "WHERE owner=? AND character_id=? AND request_id=? AND status='running'",
                            (engine.owner, cid, "auto-" + jid),
                        )
                    log.warning(
                        "Memory job %s (%s for %s) failed %d times consecutively; marked terminal (%s). "
                        "Check provider/logs and acknowledge manually in Companion Studio to resume.",
                        jid[:8],
                        kind,
                        cid,
                        streak,
                        TERMINAL_REVIEW_ERROR,
                    )
                    continue

                try:
                    policy = brain.runtime.automation.policy(cid)
                    used_24h = engine.db.execute(
                        "SELECT count(*) FROM memory_jobs WHERE owner=? AND character_id=? AND created>?",
                        (engine.owner, cid, now - 86400),
                    ).fetchone()[0]
                    if used_24h >= policy.daily_jobs:
                        continue
                except Exception:
                    pass

                if is_startup and err == "interrupted_outcome_unknown":
                    backoff = 0
                else:
                    backoff = min(base_backoff * (3 ** (streak - 1)), REVIEW_BACKOFF_MAX_SECONDS)

                age = now - float(r["created"] or 0.0)
                if age < backoff:
                    continue

                with engine._transaction():
                    changed = engine.db.execute(
                        "UPDATE memory_jobs SET state='auto_retried' WHERE id=? AND state='review'",
                        (jid,),
                    ).rowcount
                    if changed:
                        engine.db.execute(
                            "UPDATE operations SET status='failed_unknown' "
                            "WHERE owner=? AND character_id=? AND request_id=? AND status='running'",
                            (engine.owner, cid, "auto-" + jid),
                        )
                        released += 1
                if changed:
                    next_backoff = min(base_backoff * (3 ** streak), REVIEW_BACKOFF_MAX_SECONDS)
                    log.info(
                        "Auto-retried review memory_job %s (%s for %s, attempt %d/%d after %ds backoff; next backoff %ds)",
                        jid[:8],
                        kind,
                        cid,
                        streak,
                        MAX_AUTO_REVIEW_RETRIES,
                        int(backoff),
                        int(next_backoff),
                    )
    except Exception as exc:
        log.warning("clear_stale_review_jobs skipped: %s", exc)
    return released


def _prune_old_turn_drafts_tx(engine, keep_turn_drafts: int = KEEP_TURN_DRAFTS) -> int:
    """Delete excess committed turn_drafts beyond the newest N while preserving pending turns and image turns."""
    keep = max(1, int(keep_turn_drafts))
    cur = engine.db.execute(
        "DELETE FROM turn_drafts WHERE turn_id IN ("
        "  SELECT d.turn_id FROM turn_drafts d "
        "  JOIN turns t ON t.id = d.turn_id "
        "  WHERE t.owner=? AND t.status='committed' "
        "  AND instr(coalesce(d.payload, ''), '\"image_result\"') = 0 "
        "  ORDER BY t.created_at DESC, t.rowid DESC "
        "  LIMIT -1 OFFSET ?"
        ")",
        (engine.owner, keep),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def prune_database_bloat(
    brain: CompanionBrain,
    *,
    keep_turn_drafts: int = KEEP_TURN_DRAFTS,
    embed_max_age_seconds: int = OP_EMBED_PRUNE_AGE_SECONDS,
    allow_vacuum: bool = False,
) -> dict[str, int]:
    """Compact old large embedding payloads in operations and cap committed turn_drafts."""
    engine = brain.engine
    now = time.time()
    stats = {"ops_pruned": 0, "ops_deleted": 0, "drafts_deleted": 0, "jobs_deleted": 0, "vacuumed": 0}
    try:
        with engine._lock:
            with engine._transaction():
                # 1. Strip multi-kilobyte raw float vectors from aged embedding operation receipts
                cur = engine.db.execute(
                    "UPDATE operations SET result='{\"pruned\":true}' "
                    "WHERE owner=? AND kind IN ('note_query_embedding','query_embedding') "
                    "AND created_at < ? AND result IS NOT NULL AND length(result) > 1024 "
                    "AND NOT (kind='query_embedding' AND EXISTS ("
                    "  SELECT 1 FROM turns t "
                    "  WHERE t.owner=operations.owner AND t.character_id=operations.character_id "
                    "  AND ('turn-' || t.id)=operations.request_id AND t.status!='committed'"
                    "))",
                    (engine.owner, now - embed_max_age_seconds),
                )
                stats["ops_pruned"] = max(0, cur.rowcount or 0)

                # 2. Delete very old (>30d) non-artifact operation rows beyond newest 200
                cur = engine.db.execute(
                    "DELETE FROM operations WHERE id IN ("
                    "  SELECT id FROM operations "
                    "  WHERE owner=? AND kind NOT IN ('speech','image') "
                    "  AND status != 'running' AND created_at < ? "
                    "  ORDER BY created_at DESC LIMIT -1 OFFSET 200"
                    ")",
                    (engine.owner, now - 30 * 86400),
                )
                stats["ops_deleted"] = max(0, cur.rowcount or 0)

                # 3. Cap committed turn_drafts to the most recent N turns
                stats["drafts_deleted"] = _prune_old_turn_drafts_tx(engine, keep_turn_drafts)

                # 4. Drop archived memory_jobs older than 30 days (never touches last 24h daily_jobs)
                cur = engine.db.execute(
                    "DELETE FROM memory_jobs "
                    "WHERE owner=? AND state NOT IN ('running','review') AND created < ?",
                    (engine.owner, now - 30 * 86400),
                )
                stats["jobs_deleted"] = max(0, cur.rowcount or 0)

            if allow_vacuum and not engine.db.in_transaction:
                freelist = engine.db.execute("PRAGMA freelist_count").fetchone()[0]
                page_size = engine.db.execute("PRAGMA page_size").fetchone()[0]
                if freelist * page_size >= 1024 * 1024:
                    engine.db.execute("VACUUM")
                    stats["vacuumed"] = 1
                # In WAL mode, VACUUM writes compacted pages into -wal; TRUNCATE commits them and shrinks main DB + -wal
                engine.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        if any(stats.values()):
            log.info(
                "Pruned Anima DB bloat: ops_pruned=%d ops_deleted=%d drafts_deleted=%d jobs_deleted=%d vacuumed=%d",
                stats["ops_pruned"],
                stats["ops_deleted"],
                stats["drafts_deleted"],
                stats["jobs_deleted"],
                stats["vacuumed"],
            )
    except Exception as exc:
        log.warning("prune_database_bloat skipped: %s", exc)
    return stats


def tighten_legacy_summary_batch_size(brain: CompanionBrain) -> None:
    """Migrate old hardcoded default summary_batch_messages=16 down to SUMMARY_BATCH_MESSAGES."""
    if SUMMARY_BATCH_MESSAGES >= 16:
        return
    engine = brain.engine
    try:
        with engine._lock:
            row = engine.db.execute(
                "SELECT config FROM memory_automation WHERE owner=? AND character_id=?",
                (engine.owner, DEFAULT_CHARACTER_ID),
            ).fetchone()
            if not row:
                return
            cfg = json.loads(row["config"])
            if cfg.get("summary_batch_messages") == 16:
                cfg["summary_batch_messages"] = SUMMARY_BATCH_MESSAGES
                policy = MemoryAutomation.model_validate(cfg)
                with engine._transaction():
                    engine.db.execute(
                        "UPDATE memory_automation SET config=? WHERE owner=? AND character_id=?",
                        (policy.model_dump_json(), engine.owner, DEFAULT_CHARACTER_ID),
                    )
                log.info(
                    "Migrated %s memory_automation summary_batch_messages: 16 -> %d",
                    DEFAULT_CHARACTER_ID,
                    SUMMARY_BATCH_MESSAGES,
                )
    except Exception as exc:
        log.warning("tighten_legacy_summary_batch_size skipped: %s", exc)


RELATIONSHIP_PRESETS: dict[str, dict[str, Any]] = {
    "restrained": {
        "label": "克制助手",
        "relationship_enabled": True,
        "romance_enabled": False,
        "axes": {
            "familiarity": 0.30,
            "warmth": 0.35,
            "trust": 0.40,
            "romance": 0.00,
            "tension": 0.00,
        },
    },
    "confidant": {
        "label": "温柔知己",
        "relationship_enabled": True,
        "romance_enabled": True,
        "axes": {
            "familiarity": 0.62,
            "warmth": 0.68,
            "trust": 0.66,
            "romance": 0.25,
            "tension": 0.00,
        },
    },
    "intimate": {
        "label": "亲密依恋",
        "relationship_enabled": True,
        "romance_enabled": True,
        "axes": {
            "familiarity": 0.88,
            "warmth": 0.90,
            "trust": 0.88,
            "romance": 0.85,
            "tension": 0.00,
        },
    },
}

_STAGE_LABELS = ("stranger", "acquaintance", "familiar", "close")
_STAGE_LABELS_ZH = ("初识陌生", "礼貌熟识", "温柔知己", "亲密依恋")


def _clamp_axis(val: Any, default: float) -> float:
    try:
        f = float(val)
    except Exception:
        return default
    if f > 1.0:
        f = f / 100.0
    return round(max(0.0, min(1.0, f)), 4)


def _infer_relationship_preset(
    axes: dict[str, float], romance_enabled: bool, relationship_enabled: bool
) -> str:
    if not relationship_enabled:
        return "off"
    for key, p in RELATIONSHIP_PRESETS.items():
        if bool(romance_enabled) != bool(p["romance_enabled"]):
            continue
        p_axes = p["axes"]
        if all(
            abs(float(axes.get(ax, 0.0)) - float(p_axes[ax])) <= 0.035
            for ax in ("familiarity", "warmth", "trust", "romance")
        ):
            return key
    return "custom"


def _compute_chinese_intimacy_guidance(
    spec: CharacterSpec, axes: dict[str, Any], bands: dict[str, Any]
) -> str:
    if not spec.relationship_enabled:
        return ""
    fam = float(axes.get("familiarity", 0.0))
    warm = float(axes.get("warmth", 0.0))
    trust = float(axes.get("trust", 0.0))
    rom = float(axes.get("romance", 0.0))
    rom_on = bool(spec.romance_enabled)
    pct_str = (
        f"熟悉度 {round(fam * 100)}% / 温暖度 {round(warm * 100)}% / "
        f"信任度 {round(trust * 100)}% / 浪漫 {'开启 ' + str(round(rom * 100)) + '%' if rom_on else '关闭'}"
    )
    avg_core = (fam + warm + trust) / 3.0
    if not rom_on and avg_core <= 0.45:
        return (
            f"中文互动尺度参考（{pct_str}）：当前处于克制得体的助手与熟人阶段，"
            "说话温和礼貌、干脆周到，保持清晰分寸，不要过度撒娇、粘人或使用恋爱暧昧措辞。"
        )
    if rom_on and (rom >= 0.60 or (avg_core >= 0.78 and rom >= 0.45)):
        return (
            f"中文互动尺度参考（{pct_str}）：当前处于深度亲密依恋阶段，彼此默契极深，"
            "可以更自然大方地流露温柔偏爱、轻声哄慰、细心牵挂与偶尔害羞撒娇的亲昵口吻"
            "（贴合心境时多自然使用 [tender]、[shy]、[playful]、[gentle]），同时保持优雅真实、不油腻不刻意。"
        )
    if not rom_on and avg_core >= 0.75:
        return (
            f"中文互动尺度参考（{pct_str}）：当前处于高度信赖的挚友/家人阶段，"
            "交流可以很放松、默契、深情关照或轻快打趣，但保持纯粹深厚的情谊，不往恋爱暧昧方向引导。"
        )
    if rom_on and rom > 0.15:
        return (
            f"中文互动尺度参考（{pct_str}）：当前处于温柔熟悉的知己阶段，"
            "口吻自然亲切、细心体贴、带有一点柔和心动与专属关怀；对方主动亲近或撒娇时可温柔接住，不必拘谨也不过度抢跑。"
        )
    return (
        f"中文互动尺度参考（{pct_str}）：当前处于温和相熟的日常陪伴阶段，"
        "保持自然真诚的关心与轻松聊天气氛，不生硬也不越界。"
    )


def _format_relationship_snapshot(
    character_id: str, spec: CharacterSpec, state: dict[str, Any], now: float, owner: str = "local"
) -> dict[str, Any]:
    decayed_state = emotion.decayed(state, now, f"{owner}:{character_id}")
    axes_raw = decayed_state.get("axes") or {}
    axes = {
        k: round(float(axes_raw.get(k, 0.0)), 4)
        for k in ("familiarity", "warmth", "trust", "romance", "tension")
    }
    bands_raw = decayed_state.get("bands") or {}
    bands = {
        k: max(0, min(3, int(bands_raw.get(k, 0))))
        for k in ("familiarity", "warmth", "trust", "romance", "tension")
    }
    fam_band = bands["familiarity"]
    stage_label = _STAGE_LABELS[fam_band]
    stage_zh = _STAGE_LABELS_ZH[fam_band]
    if spec.romance_enabled and axes["romance"] >= 0.60 and fam_band >= 2:
        stage_zh = "亲密依恋"
    elif not spec.romance_enabled and fam_band >= 3:
        stage_zh = "挚友信赖"

    note_txt = (
        emotion.note(decayed_state, now, romance_enabled=spec.romance_enabled)
        if spec.relationship_enabled
        else ""
    )
    guidance_zh = _compute_chinese_intimacy_guidance(spec, axes, bands)
    return {
        "character_id": character_id,
        "character_name": spec.name,
        "relationship_enabled": bool(spec.relationship_enabled),
        "romance_enabled": bool(spec.romance_enabled),
        "preset": _infer_relationship_preset(axes, spec.romance_enabled, spec.relationship_enabled),
        "stage": stage_label,
        "stage_label_zh": stage_zh,
        "axes": axes,
        "bands": bands,
        "note": note_txt,
        "guidance_zh": guidance_zh,
    }


def get_relationship_snapshot(brain: CompanionBrain, character_id: str) -> dict[str, Any]:
    engine = brain.engine
    now = time.time()
    with engine._lock:
        try:
            row = engine._character(character_id)
        except Exception:
            ensure_default_character(brain)
            character_id = DEFAULT_CHARACTER_ID
            row = engine._character(character_id)
        spec = CharacterSpec.model_validate_json(row["spec"])
        state = json.loads(row["state"])
        owner = engine.owner
    return _format_relationship_snapshot(character_id, spec, state, now, owner=owner)


def update_relationship_settings(
    brain: CompanionBrain, character_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Hot-update relationship flags and axes directly in SQLite (effective on the very next turn)."""
    engine = brain.engine
    now = time.time()
    payload = payload or {}

    with engine._lock:
        try:
            row = engine._character(character_id)
        except Exception:
            ensure_default_character(brain)
            character_id = DEFAULT_CHARACTER_ID
            row = engine._character(character_id)

        spec = CharacterSpec.model_validate_json(row["spec"])
        state = emotion.decayed(json.loads(row["state"]), now, f"{engine.owner}:{character_id}")
        axes = dict(state.get("axes") or {})
        for ax in ("familiarity", "warmth", "trust", "romance", "tension"):
            axes.setdefault(ax, 0.0)

        spec_dict = spec.model_dump()
        preset_key = str(payload.get("preset") or "").strip().lower()
        if preset_key in RELATIONSHIP_PRESETS:
            p = RELATIONSHIP_PRESETS[preset_key]
            spec_dict["relationship_enabled"] = bool(p["relationship_enabled"])
            spec_dict["romance_enabled"] = bool(p["romance_enabled"])
            for ax, val in p["axes"].items():
                axes[ax] = float(val)

        if "relationship_enabled" in payload and payload["relationship_enabled"] is not None:
            spec_dict["relationship_enabled"] = bool(payload["relationship_enabled"])
        if "romance_enabled" in payload and payload["romance_enabled"] is not None:
            spec_dict["romance_enabled"] = bool(payload["romance_enabled"])

        raw_axes = payload.get("axes") if isinstance(payload.get("axes"), dict) else {}
        for ax in ("familiarity", "warmth", "trust", "romance", "tension"):
            if ax in payload and payload[ax] is not None:
                axes[ax] = _clamp_axis(payload[ax], axes[ax])
            elif ax in raw_axes and raw_axes[ax] is not None:
                axes[ax] = _clamp_axis(raw_axes[ax], axes[ax])

        state["axes"] = axes
        state["at"] = now
        state["interaction_at"] = now
        # Clear previous hysteresis bands so user's explicit slider/preset choice snaps immediately
        state["bands"] = {}
        state["bands"] = emotion._bands(state)

        new_spec = CharacterSpec.model_validate(spec_dict)
        with engine._transaction():
            engine.db.execute(
                "UPDATE characters SET spec=?, state=? WHERE owner=? AND id=?",
                (
                    new_spec.model_dump_json(),
                    json.dumps(state, ensure_ascii=False),
                    engine.owner,
                    character_id,
                ),
            )
        owner = engine.owner

    snap = _format_relationship_snapshot(character_id, new_spec, state, now, owner=owner)
    log.info(
        "Hot-updated relationship for %s: preset=%s, stage=%s, romance=%s, axes=%s",
        character_id,
        snap["preset"],
        snap["stage"],
        snap["romance_enabled"],
        snap["axes"],
    )
    return snap


def _fmt_mem_date(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except Exception:
        return ""


def build_fast_recall_context(brain: CompanionBrain, character_id: str, query: str) -> dict[str, Any]:
    """Read-only sub-15ms recall from SQLite/FTS5/PAD without reserving a pending turn."""
    engine = brain.engine
    query = (query or "").strip()[:4000]
    now = time.time()

    with engine._lock:
        try:
            row = engine._character(character_id)
        except Exception:
            ensure_default_character(brain)
            row = engine._character(DEFAULT_CHARACTER_ID)
            character_id = DEFAULT_CHARACTER_ID

        spec = CharacterSpec.model_validate_json(row["spec"])
        today = local_today(engine, character_id)

        # 1. Decayed emotional & relationship state (read-only preview)
        rel_note = None
        rel_guidance_zh = ""
        mood_summary = None
        if spec.relationship_enabled:
            state = emotion.decayed(
                json.loads(row["state"]), now, f"{engine.owner}:{character_id}"
            )
            rel_note = emotion.note(state, now, romance_enabled=spec.romance_enabled)
            rel_guidance_zh = _compute_chinese_intimacy_guidance(
                spec, state.get("axes") or {}, state.get("bands") or {}
            )
            fam_band = max(0, min(3, int((state.get("bands") or {}).get("familiarity", 0))))
            stage_label = ("stranger", "acquaintance", "familiar", "close")[fam_band]
            mood_label = state.get("last_emotion") or ""
            if not mood_label:
                last_app_row = engine.db.execute(
                    "SELECT p.data, t.created_at FROM turn_appraisals p "
                    "JOIN turns t ON t.id = p.turn_id "
                    "WHERE t.owner=? AND t.character_id=? AND t.status='committed' "
                    "ORDER BY t.created_at DESC, t.rowid DESC LIMIT 1",
                    (engine.owner, character_id),
                ).fetchone()
                if last_app_row and (now - float(last_app_row["created_at"] or 0.0)) <= 6 * 3600:
                    try:
                        mood_label = str(json.loads(last_app_row["data"]).get("emotion") or "")
                    except Exception:
                        mood_label = ""
            if not mood_label:
                pad_p = float((state.get("pad") or [0.0])[0])
                warmth_band = int((state.get("bands") or {}).get("warmth", 0))
                mood_label = "warm" if (warmth_band >= 2 and pad_p >= 0.35) else "calm"
            mood_summary = f"stage={stage_label}, last_emotion={mood_label}"

        # 2. Pinned boundaries/style + layered retrieval + lexical FTS5 search
        pinned = [
            engine._memory(r)
            for r in engine.db.execute(
                "SELECT * FROM memories WHERE owner=? AND character_id=? "
                "AND category IN ('boundary','style') ORDER BY updated_at DESC LIMIT 8",
                (engine.owner, character_id),
            ).fetchall()
        ]
        recent_facts = [
            engine._memory(r)
            for r in engine.db.execute(
                "SELECT * FROM memories WHERE owner=? AND character_id=? "
                "ORDER BY updated_at DESC LIMIT 10",
                (engine.owner, character_id),
            ).fetchall()
        ]

        layered_docs = []
        layered_facts = []
        lexical_facts = []
        evidence_sources = []
        if query:
            try:
                from companion_engine.models import Memory as MemoryModel

                layers = engine.retrieval.search(character_id, query, limit=8)
                layered_facts = [MemoryModel.model_validate(m) for m in layers.get("facts", [])]
                layered_docs = layers.get("documents", [])
            except Exception as exc:
                log.debug("Layered search skipped: %s", exc)
            try:
                lexical_facts = engine.search(character_id, query, limit=8)
            except Exception as exc:
                log.debug("FTS search skipped: %s", exc)
            try:
                evidence_sources = engine.memory.recall(character_id, query, limit=4).get("sources", [])
            except Exception as exc:
                log.debug("Evidence recall skipped: %s", exc)

        # Deduplicate & filter eligible active facts
        seen_ids: set[str] = set()
        active_facts = []
        for m in [*pinned, *layered_facts, *lexical_facts, *recent_facts]:
            if m.id in seen_ids:
                continue
            if not eligible(m, today=today):
                continue
            seen_ids.add(m.id)
            active_facts.append(m)
            if len(active_facts) >= 14:
                break

        # 3. Core & episode notebooks
        all_docs = engine.memory.documents(character_id)
        core_docs = [d for d in all_docs if d["kind"] in ("core", "character_core")]
        episode_docs = [d for d in layered_docs if d["kind"] == "episode"][:2]
        if not episode_docs:
            episode_docs = [d for d in all_docs if d["kind"] == "episode"][:1]

        # 4. Open loops (unresolved user events to follow up naturally)
        open_loops = [m for m in engine.memory.loops(character_id)[:4] if eligible(m, today=today)]

        # 5. Recent committed cross-session turns (bridges Web UI chat <-> ESP32 hardware!)
        recent_rows = engine.db.execute(
            "SELECT user_message, assistant_reply, created_at FROM turns "
            "WHERE owner=? AND character_id=? AND status='committed' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 4",
            (engine.owner, character_id),
        ).fetchall()
        recent_turns = list(reversed([dict(r) for r in recent_rows]))

    # Format compact Chinese context block for XiaoZhi streaming LLM
    sections: list[str] = []
    sections.append(f"【角色身份设定（来自 Companion Studio）】\n姓名：{spec.name}\n设定：{spec.persona}")
    if spec.core_persona.strip():
        sections.append(f"【角色深层内核】\n{spec.core_persona.strip()}")

    if rel_note:
        rel_block = rel_note if not rel_guidance_zh else f"{rel_note}\n{rel_guidance_zh}"
        sections.append(f"【当前情绪与关系连续性状态】\n{rel_block}")

    if core_docs:
        core_texts = "\n".join(d["text"].strip()[:1200] for d in core_docs if d.get("text"))
        if core_texts:
            sections.append(f"【长期核心人物笔记】\n{core_texts}")

    if active_facts:
        fact_lines = [
            f"- [{_fmt_mem_date(getattr(m, 'updated_at', None)) or '历史'} | {m.category}] {m.text}"
            for m in active_facts
        ]
        sections.append(
            "【已确认的关于对方的长期记忆（带记录日期，切勿把往日事项误说成今天）】\n"
            + "\n".join(fact_lines)
        )

    if open_loops:
        loop_lines = [
            f"- [{_fmt_mem_date(getattr(m, 'updated_at', None)) or '近期'}] {m.text}"
            for m in open_loops
        ]
        sections.append(
            "【未完待续事项（仅作背景知情，切勿每次打招呼都机械重复追问同一件旧事，除非对方主动聊起）】\n"
            + "\n".join(loop_lines)
        )

    if episode_docs:
        ep_lines = [d["text"].strip()[:500] for d in episode_docs if d.get("text")]
        if ep_lines:
            sections.append("【过往互动片段摘要】\n" + "\n".join(ep_lines))

    if evidence_sources:
        ev_lines = []
        prefers_gege = "哥哥" in (spec.persona or "")
        for s in evidence_sources[:4]:
            txt = (s.get("text") or "")[:180]
            if prefers_gege and s.get("speaker") != "user" and "舰长" in txt:
                continue
            ev_lines.append(f"- ({'用户原话' if s.get('speaker') == 'user' else '你曾回复'}) {txt}")
        if ev_lines:
            sections.append("【相关历史原话溯源】\n" + "\n".join(ev_lines))

    if recent_turns:
        rt_lines = []
        for t in recent_turns:
            u = (t.get("user_message") or "").strip()[:180]
            a = (t.get("assistant_reply") or "").strip()[:320]
            rt_lines.append(f"用户：{u}\n{spec.name}：{a}")
        sections.append("【最近跨端对话衔接（若当前在同一轮已包含则勿重复）】\n" + "\n---\n".join(rt_lines))

    formatted = "\n\n".join(sections)
    return {
        "character_id": character_id,
        "character_name": spec.name,
        "mood": mood_summary,
        "facts_count": len(active_facts),
        "open_loops_count": len(open_loops),
        "context_text": formatted,
    }


def _safe_key(raw_key: str, fallback_idx: int) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_:\-\u4e00-\u9fff]+", "_", (raw_key or "").strip().lower()).strip("_")
    if not cleaned:
        cleaned = f"fact_{int(time.time())}_{fallback_idx}"
    return cleaned[:100]


def ensure_xiaozhi_turn_meta_table(engine) -> None:
    """Create side table storing true occurred_at, source, device_id, and used_tools per turn."""
    with engine._lock:
        with engine._transaction():
            engine.db.execute(
                "CREATE TABLE IF NOT EXISTS xiaozhi_turn_meta ("
                "turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE, "
                "occurred_at REAL NOT NULL, "
                "source TEXT NOT NULL DEFAULT 'esp32', "
                "device_id TEXT NOT NULL DEFAULT '', "
                "used_tools INTEGER NOT NULL DEFAULT 0, "
                "updated_at REAL NOT NULL"
                ")"
            )
            engine.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_xiaozhi_turn_meta_occurred "
                "ON xiaozhi_turn_meta(occurred_at)"
            )


def _normalize_turn_meta(meta: dict[str, Any] | None, fallback_ts: float) -> dict[str, Any]:
    raw = meta if isinstance(meta, dict) else {}
    raw_occ = raw.get("occurred_at")
    if isinstance(raw_occ, (int, float)) and not isinstance(raw_occ, bool) and raw_occ > 0:
        occurred_at = float(raw_occ)
    else:
        occurred_at = float(fallback_ts)
    source = str(raw.get("source") or "esp32").strip()[:32] or "esp32"
    device_id = str(raw.get("device_id") or "").strip()[:64]
    used_tools = 1 if raw.get("used_tools") else 0
    return {
        "occurred_at": occurred_at,
        "source": source,
        "device_id": device_id,
        "used_tools": used_tools,
    }


def _upsert_turn_meta_tx_detailed(
    engine, turn_id: str, meta: dict[str, Any] | None, fallback_ts: float
) -> tuple[float, bool, float | None]:
    """Upsert xiaozhi_turn_meta inside an active transaction.

    Returns (effective_occurred_at, changed, previous_occurred_at).
    """
    norm = _normalize_turn_meta(meta, fallback_ts)
    prev = engine.db.execute(
        "SELECT occurred_at, source, device_id, used_tools FROM xiaozhi_turn_meta WHERE turn_id=?",
        (turn_id,),
    ).fetchone()
    prev_occ = float(prev["occurred_at"]) if prev is not None else None
    now = time.time()
    engine.db.execute(
        "INSERT INTO xiaozhi_turn_meta(turn_id, occurred_at, source, device_id, used_tools, updated_at) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(turn_id) DO UPDATE SET "
        "occurred_at = MIN(xiaozhi_turn_meta.occurred_at, excluded.occurred_at), "
        "source = CASE WHEN excluded.source != '' THEN excluded.source ELSE xiaozhi_turn_meta.source END, "
        "device_id = CASE WHEN excluded.device_id != '' THEN excluded.device_id ELSE xiaozhi_turn_meta.device_id END, "
        "used_tools = MAX(xiaozhi_turn_meta.used_tools, excluded.used_tools), "
        "updated_at = excluded.updated_at",
        (turn_id, norm["occurred_at"], norm["source"], norm["device_id"], norm["used_tools"], now),
    )
    row = engine.db.execute(
        "SELECT occurred_at, source, device_id, used_tools FROM xiaozhi_turn_meta WHERE turn_id=?",
        (turn_id,),
    ).fetchone()
    eff_occ = float(row["occurred_at"]) if row else norm["occurred_at"]
    if prev is None:
        changed = True
    elif row is None:
        changed = False
    else:
        changed = (
            abs(float(prev["occurred_at"]) - float(row["occurred_at"])) > 1e-6
            or str(prev["source"]) != str(row["source"])
            or str(prev["device_id"]) != str(row["device_id"])
            or int(prev["used_tools"]) != int(row["used_tools"])
        )
    return eff_occ, changed, prev_occ


def _upsert_turn_meta_tx(
    engine, turn_id: str, meta: dict[str, Any] | None, fallback_ts: float
) -> float:
    """Upsert xiaozhi_turn_meta inside an active transaction, keeping earliest occurred_at and OR-ing used_tools."""
    eff_occ, _changed, _prev_occ = _upsert_turn_meta_tx_detailed(engine, turn_id, meta, fallback_ts)
    return eff_occ


def _upsert_longer_reply_tx(
    engine,
    character_id: str,
    turn_id: str,
    new_reply: str,
    turn_meta: dict[str, Any] | None,
    fallback_ts: float,
) -> tuple[float, float | None]:
    """Update a committed turn's assistant_reply when a fuller post-tool reply arrives with the same request_id."""
    engine.db.execute(
        "UPDATE turns SET assistant_reply=? WHERE id=? AND owner=? AND character_id=?",
        (new_reply, turn_id, engine.owner, character_id),
    )
    eid = f"{turn_id}:assistant"
    ev_row = engine.db.execute("SELECT 1 FROM evidence WHERE id=?", (eid,)).fetchone()
    if ev_row:
        engine.db.execute("UPDATE evidence SET text=? WHERE id=?", (new_reply, eid))
        try:
            engine.memory._index(eid, new_reply)
        except Exception:
            pass
    draft_row = engine.db.execute(
        "SELECT payload FROM turn_drafts WHERE turn_id=?", (turn_id,)
    ).fetchone()
    if draft_row:
        try:
            payload = json.loads(draft_row["payload"])
            if isinstance(payload, dict) and isinstance(payload.get("commit_request"), dict):
                payload["commit_request"]["assistant_reply"] = new_reply
                engine.db.execute(
                    "UPDATE turn_drafts SET payload=? WHERE turn_id=?",
                    (json.dumps(payload, ensure_ascii=False), turn_id),
                )
        except Exception:
            pass
    eff_occ, _changed, prev_occ = _upsert_turn_meta_tx_detailed(engine, turn_id, turn_meta, fallback_ts)
    return eff_occ, prev_occ


def extract_and_commit_turn(
    brain: CompanionBrain,
    character_id: str,
    request_id: str,
    user_message: str,
    assistant_reply: str,
    *,
    turn_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synchronously run in worker thread: extract grounded facts/appraisal & commit turn."""
    engine = brain.engine
    runtime = brain.runtime
    user_message = (user_message or "").strip()
    assistant_reply = (assistant_reply or "").strip()
    if not user_message or not assistant_reply:
        return {"skipped": "empty_message"}

    request_id = re.sub(r"[^a-zA-Z0-9_\-:.]", "_", request_id)[:110] or _new_request_id()
    ensure_xiaozhi_turn_meta_table(engine)

    with engine._lock:
        try:
            row = engine._character(character_id)
        except Exception:
            ensure_default_character(brain)
            character_id = DEFAULT_CHARACTER_ID
            row = engine._character(character_id)

        existing_turn = engine.db.execute(
            "SELECT id, status, user_message, assistant_reply, created_at FROM turns "
            "WHERE owner=? AND character_id=? AND request_id=?",
            (engine.owner, character_id, request_id),
        ).fetchone()
        if existing_turn and existing_turn["status"] == "committed":
            old_user = (existing_turn["user_message"] or "").strip()
            old_reply = (existing_turn["assistant_reply"] or "").strip()
            turn_id = existing_turn["id"]
            created_at = float(existing_turn["created_at"] or time.time())
            if user_message != old_user:
                log.warning(
                    "request_id collision detected for %s (turn %s): user_message differs; refusing to overwrite",
                    request_id,
                    turn_id[:8],
                )
                return {
                    "committed": True,
                    "replayed": True,
                    "updated": False,
                    "conflict": True,
                    "turn_id": turn_id,
                    "occurred_at": created_at,
                    "prev_occurred_at": None,
                }
            if assistant_reply != old_reply:
                if len(assistant_reply) > len(old_reply) and assistant_reply.startswith(old_reply):
                    with engine._transaction():
                        eff_occ, prev_occ = _upsert_longer_reply_tx(
                            engine, character_id, turn_id, assistant_reply, turn_meta, created_at
                        )
                    log.info(
                        "Upserted fuller post-tool continuation for committed turn %s (%d -> %d chars)",
                        turn_id[:8],
                        len(old_reply),
                        len(assistant_reply),
                    )
                    return {
                        "committed": True,
                        "updated": True,
                        "replayed": False,
                        "turn_id": turn_id,
                        "occurred_at": eff_occ,
                        "prev_occurred_at": prev_occ,
                    }
                if not old_reply.startswith(assistant_reply):
                    log.warning(
                        "request_id content conflict for %s (turn %s): divergent assistant_reply (%d vs %d chars); refusing to overwrite",
                        request_id,
                        turn_id[:8],
                        len(old_reply),
                        len(assistant_reply),
                    )
                    return {
                        "committed": True,
                        "replayed": True,
                        "updated": False,
                        "conflict": True,
                        "turn_id": turn_id,
                        "occurred_at": created_at,
                        "prev_occurred_at": None,
                    }
            meta_changed = False
            prev_occ = None
            if turn_meta:
                with engine._transaction():
                    eff_occ, meta_changed, prev_occ = _upsert_turn_meta_tx_detailed(
                        engine, turn_id, turn_meta, created_at
                    )
            else:
                eff_occ = created_at
            return {
                "committed": True,
                "replayed": True,
                "updated": bool(meta_changed),
                "turn_id": turn_id,
                "occurred_at": eff_occ,
                "prev_occurred_at": prev_occ,
            }

        spec = CharacterSpec.model_validate_json(row["spec"])
        today = local_today(engine, character_id)
        current_memories = [
            m for m in engine.memories(character_id, limit=100) if eligible(m, today=today)
        ]
        by_key = {m.key: m for m in current_memories}
        by_id = {m.id: m for m in current_memories}

    # Run LLM extraction outside SQLite lock so Companion Studio UI stays snappy!
    memories_out: list[MemoryDraft] = []
    corrections_out: list[MemoryCorrection] = []
    appraisal_out: Appraisal | None = None
    attunement_out: Attunement | None = None

    should_extract = spec.memory_policy != "off" or spec.relationship_enabled
    if should_extract and runtime.settings.base_url and runtime.settings.model:
        try:
            provider = runtime.provider()
            envelope = json.dumps(
                {
                    "current_date": today.isoformat(),
                    "character_name": spec.name,
                    "relationship_enabled": spec.relationship_enabled,
                    "romance_enabled": spec.romance_enabled,
                    "memory_policy": spec.memory_policy,
                    "user_message": user_message,
                    "assistant_reply": assistant_reply,
                    "existing_facts": [
                        {
                            "id": m.id,
                            "key": m.key,
                            "revision": m.revision,
                            "category": m.category,
                            "text": m.text,
                        }
                        for m in current_memories[:60]
                    ],
                },
                ensure_ascii=False,
            )
            completion = provider.structured(EXTRACTION_SYSTEM_PROMPT, envelope)
            parsed = json.loads(completion.text)
            fallback_quote = user_message[: min(len(user_message), 120)]

            if spec.memory_policy == "automatic":
                seen_keys: set[str] = set()
                seen_corr_ids: set[str] = set()

                for idx, raw_corr in enumerate(parsed.get("corrections") or []):
                    try:
                        mid = str(raw_corr.get("memory_id") or "")
                        if mid not in by_id or mid in seen_corr_ids:
                            continue
                        target_mem = by_id[mid]
                        raw_draft = dict(raw_corr.get("draft") or {})
                        quote = str(raw_draft.get("source_quote") or "").strip()
                        if not quote or quote not in user_message:
                            continue
                        raw_draft["key"] = target_mem.key
                        raw_draft["source_media_ref"] = None
                        if isinstance(raw_draft.get("details"), dict):
                            ce = str(raw_draft["details"].get("correction_evidence") or "")
                            if ce and ce not in quote:
                                raw_draft["details"]["correction_evidence"] = ""
                        draft = MemoryDraft.model_validate(raw_draft)
                        kind = raw_corr.get("kind") if raw_corr.get("kind") in ("corrected", "superseded") else "superseded"
                        corrections_out.append(
                            MemoryCorrection(
                                memory_id=target_mem.id,
                                expected_revision=target_mem.revision,
                                kind=kind,
                                draft=draft,
                            )
                        )
                        seen_corr_ids.add(mid)
                        seen_keys.add(target_mem.key)
                    except Exception as exc:
                        log.debug("Skipped invalid correction %s: %s", idx, exc)

                for idx, raw_mem in enumerate(parsed.get("memories") or []):
                    try:
                        raw_mem = dict(raw_mem)
                        quote = str(raw_mem.get("source_quote") or "").strip()
                        if not quote or quote not in user_message:
                            continue
                        k = _safe_key(str(raw_mem.get("key") or ""), idx)
                        if k in seen_keys:
                            continue
                        raw_mem["key"] = k
                        raw_mem["source_media_ref"] = None
                        if isinstance(raw_mem.get("details"), dict):
                            ce = str(raw_mem["details"].get("correction_evidence") or "")
                            if ce and ce not in quote:
                                raw_mem["details"]["correction_evidence"] = ""
                        draft = MemoryDraft.model_validate(raw_mem)
                        if k in by_key:
                            existing_mem = by_key[k]
                            if existing_mem.text != draft.text and existing_mem.id not in seen_corr_ids:
                                corrections_out.append(
                                    MemoryCorrection(
                                        memory_id=existing_mem.id,
                                        expected_revision=existing_mem.revision,
                                        kind="superseded",
                                        draft=draft,
                                    )
                                )
                                seen_corr_ids.add(existing_mem.id)
                                seen_keys.add(k)
                                continue
                        memories_out.append(draft)
                        seen_keys.add(k)
                    except Exception as exc:
                        log.debug("Skipped invalid memory draft %s: %s", idx, exc)

            if spec.relationship_enabled and isinstance(parsed.get("appraisal"), dict):
                try:
                    raw_app = dict(parsed["appraisal"])
                    q = str(raw_app.get("source_quote") or "").strip()
                    raw_app["source_quote"] = q if (q and q in user_message) else fallback_quote
                    if not spec.romance_enabled:
                        raw_app["romance"] = 0
                    for int_field in ("intensity", "trust", "warmth", "tension", "disclosure", "romance"):
                        if int_field in raw_app:
                            raw_app[int_field] = int(round(float(raw_app[int_field])))
                    appraisal_out = Appraisal.model_validate(raw_app)
                except Exception as exc:
                    log.debug("Skipped invalid appraisal: %s", exc)

            if isinstance(parsed.get("attunement"), dict):
                try:
                    raw_att = dict(parsed["attunement"])
                    q = str(raw_att.get("source_quote") or "").strip()
                    raw_att["source_quote"] = q if (q and q in user_message) else fallback_quote
                    attunement_out = Attunement.model_validate(raw_att)
                except Exception as exc:
                    log.debug("Skipped invalid attunement: %s", exc)

        except Exception as exc:
            log.warning("Extraction call failed (will still commit raw voice turn): %s", exc)

    # Wait briefly if Companion Studio web chat currently holds a pending turn
    inline_wait_deadline = time.monotonic() + INGEST_PENDING_INLINE_WAIT_SECONDS
    while time.monotonic() < inline_wait_deadline:
        pending = engine.pending_turn(character_id)
        if not pending or pending["request_id"] == request_id:
            break
        time.sleep(0.1)

    started = time.monotonic()
    with brain._lock:
        pending = engine.pending_turn(character_id)
        if pending and pending["request_id"] != request_id:
            return {"skipped": "another_turn_pending"}

        ctx = engine.prepare_turn(character_id, user_message, request_id=request_id)
        commit_req = CommitRequest(
            assistant_reply=assistant_reply,
            memories=memories_out[:12],
            corrections=corrections_out[:8],
            appraisal=appraisal_out,
            attunement=attunement_out,
        )
        try:
            committed = runtime.commit(character_id, ctx.turn_id, commit_req)
        except Exception as exc:
            log.warning("Rich commit failed (%s), falling back to clean transcript commit", exc)
            commit_req = CommitRequest(
                assistant_reply=assistant_reply,
                memories=[],
                corrections=[],
                appraisal=appraisal_out,
                attunement=attunement_out,
            )
            try:
                committed = runtime.commit(character_id, ctx.turn_id, commit_req)
            except Exception:
                commit_req = CommitRequest(assistant_reply=assistant_reply)
                committed = runtime.commit(character_id, ctx.turn_id, commit_req)

        # Save turn_drafts so Companion Studio's "Inspect Turn" works on ESP32 voice turns too
        draft_payload = {
            "turn_id": ctx.turn_id,
            "commit_request": commit_req.model_dump(mode="json"),
            "generation": {
                "model": f"xiaozhi-voice + {runtime.settings.model or 'gemini'}",
                "retrieval_mode": "xiaozhi_fast_hybrid",
                "memory_ids": [m.id for m in ctx.memories],
                "evidence_ids": [e["id"] for e in ctx.evidence],
                "document_ids": [d["id"] for d in ctx.core_memory],
                "recent_turns": len(ctx.recent_turns),
                "context_chars": len(ctx.model_dump_json()),
                "media": {"mode": "voice_esp32", "inputs": [], "model": "SenseVoiceSmall + EdgeTTS"},
                "elapsed_seconds": round(time.monotonic() - started, 3),
            },
            "context": ctx.model_dump(mode="json"),
            "committed": True,
        }
        with engine._transaction():
            engine.db.execute(
                "INSERT OR REPLACE INTO turn_drafts VALUES(?,?)",
                (ctx.turn_id, json.dumps(draft_payload, ensure_ascii=False)),
            )
            t_row = engine.db.execute(
                "SELECT created_at FROM turns WHERE id=?", (ctx.turn_id,)
            ).fetchone()
            fallback_ts = float(t_row["created_at"]) if t_row else time.time()
            eff_occ = _upsert_turn_meta_tx(engine, ctx.turn_id, turn_meta, fallback_ts)
            _prune_old_turn_drafts_tx(engine, KEEP_TURN_DRAFTS)

    log.info(
        "Committed ESP32 voice turn %s: +%d memories, +%d corrections, emotion=%s",
        ctx.turn_id[:8],
        len(commit_req.memories),
        len(commit_req.corrections),
        commit_req.appraisal.emotion if commit_req.appraisal else "none",
    )
    return {
        "committed": True,
        "turn_id": ctx.turn_id,
        "memory_ids": committed.get("memory_ids", []),
        "occurred_at": eff_occ,
    }


_STUDIO_WRITE_ACTIONS = frozenset(
    {
        "/api/chat",
        "/api/approve",
        "/api/correct_memory",
        "/api/delete_memory",
        "/api/maintain",
        "/api/update_character",
    }
)


def create_combined_asgi_app(
    brain: CompanionBrain,
    token: str,
    *,
    obsidian_exporter: Any = None,
):
    from obsidian_exporter import ObsidianExporter, ObsidianSyncRunner

    ensure_xiaozhi_turn_meta_table(brain.engine)
    configure_sqlite_wal(brain.engine)
    backup_dir = _resolve_backup_dir(brain)

    studio_app = create_app(
        brain.runtime,
        token,
        port=PORT,
        include_mcp=True,
        start_worker=True,
        frontend_dir=FRONTEND_DIR,
        preview_mode=False,
    )

    if obsidian_exporter is None:
        obsidian_exporter = ObsidianExporter(
            brain,
            character_id=DEFAULT_CHARACTER_ID,
            export_dir=OBSIDIAN_EXPORT_DIR,
            host_export_dir=OBSIDIAN_HOST_DIR,
            config_path=DATA_DIR / "obsidian_sync_config.json",
            default_enabled=OBSIDIAN_SYNC_ENABLED,
            snapshot_interval_seconds=OBSIDIAN_SNAPSHOT_INTERVAL_SECONDS,
        )
    obsidian_runner = ObsidianSyncRunner(
        obsidian_exporter,
        debounce_seconds=OBSIDIAN_DEBOUNCE_SECONDS,
    )

    allowed_hosts = {
        f"127.0.0.1:{PORT}",
        f"localhost:{PORT}",
        f"anima-brain:{PORT}",
    }
    if LAN_IP:
        allowed_hosts.add(f"{LAN_IP}:{PORT}")

    ingest_queue: asyncio.Queue[tuple[str, str, str, str, dict[str, Any], int]] = asyncio.Queue(
        maxsize=INGEST_QUEUE_MAXSIZE
    )
    retry_tasks: set[asyncio.Task] = set()
    retry_items: dict[asyncio.Task, tuple[str, str, str, str, dict[str, Any], int]] = {}
    shutting_down = False
    ingest_stats: dict[str, int] = {
        "enqueued_total": 0,
        "committed_total": 0,
        "retried_total": 0,
        "dropped_total": 0,
        "dropped_queue_full": 0,
        "dropped_pending_exhausted": 0,
    }

    def _schedule_delayed_reenqueue(
        item: tuple[str, str, str, str, dict[str, Any], int],
        delay_sec: float,
        reason: str,
    ) -> bool:
        if shutting_down or len(retry_tasks) >= INGEST_OVERFLOW_MAX_TASKS:
            return False

        async def _retry_put():
            cur_task = asyncio.current_task()
            try:
                await asyncio.sleep(delay_sec)
                if shutting_down:
                    return
                await asyncio.wait_for(ingest_queue.put(item), timeout=1.5)
                if cur_task is not None:
                    retry_items.pop(cur_task, None)
            except asyncio.CancelledError:
                return
            except asyncio.TimeoutError:
                if cur_task is not None:
                    retry_items.pop(cur_task, None)
                req_id = item[1]
                attempt = item[5]
                if attempt < INGEST_PENDING_MAX_RETRIES and not shutting_down:
                    ingest_stats["retried_total"] += 1
                    next_item = (item[0], item[1], item[2], item[3], item[4], attempt + 1)
                    next_delay = min(30.0, INGEST_PENDING_BACKOFF_BASE * (2 ** min(attempt, 4)))
                    if not _schedule_delayed_reenqueue(next_item, next_delay, reason):
                        ingest_stats["dropped_total"] += 1
                        if reason == "queue_full":
                            ingest_stats["dropped_queue_full"] += 1
                        else:
                            ingest_stats["dropped_pending_exhausted"] += 1
                        log.warning(
                            "Dropped turn %s after %s retry saturation",
                            req_id,
                            reason,
                        )
                else:
                    ingest_stats["dropped_total"] += 1
                    if reason == "queue_full":
                        ingest_stats["dropped_queue_full"] += 1
                    else:
                        ingest_stats["dropped_pending_exhausted"] += 1
                    log.warning("Dropped turn %s after exhausting %s retries", req_id, reason)

        def _on_retry_done(t: asyncio.Task) -> None:
            retry_tasks.discard(t)
            retry_items.pop(t, None)

        task = asyncio.create_task(_retry_put())
        retry_tasks.add(task)
        retry_items[task] = item
        task.add_done_callback(_on_retry_done)
        return True

    async def ingest_worker():
        while True:
            character_id, req_id, user_msg, reply_msg, turn_meta, attempt = await ingest_queue.get()
            try:
                if turn_meta:
                    result = await asyncio.to_thread(
                        extract_and_commit_turn,
                        brain,
                        character_id,
                        req_id,
                        user_msg,
                        reply_msg,
                        turn_meta=turn_meta,
                    )
                else:
                    result = await asyncio.to_thread(
                        extract_and_commit_turn,
                        brain,
                        character_id,
                        req_id,
                        user_msg,
                        reply_msg,
                    )
                if isinstance(result, dict) and result.get("skipped") == "another_turn_pending":
                    if attempt < INGEST_PENDING_MAX_RETRIES and not shutting_down:
                        delay = min(30.0, INGEST_PENDING_BACKOFF_BASE * (2 ** min(attempt, 4)))
                        scheduled = _schedule_delayed_reenqueue(
                            (character_id, req_id, user_msg, reply_msg, turn_meta, attempt + 1),
                            delay,
                            "another_turn_pending",
                        )
                        if scheduled:
                            ingest_stats["retried_total"] += 1
                            log.info(
                                "Turn %s deferred (another_turn_pending); scheduled retry %d/%d in %.2fs",
                                req_id,
                                attempt + 1,
                                INGEST_PENDING_MAX_RETRIES,
                                delay,
                            )
                        else:
                            ingest_stats["dropped_total"] += 1
                            ingest_stats["dropped_pending_exhausted"] += 1
                            log.warning("Turn %s dropped (retry buffer saturated during pending turn)", req_id)
                    else:
                        ingest_stats["dropped_total"] += 1
                        ingest_stats["dropped_pending_exhausted"] += 1
                        log.warning(
                            "Turn %s dropped after %d pending-turn retries",
                            req_id,
                            attempt,
                        )
                elif isinstance(result, dict) and result.get("committed"):
                    ingest_stats["committed_total"] += 1
                    if not result.get("replayed") or result.get("updated"):
                        obsidian_runner.notify_turn(
                            result.get("occurred_at") or (turn_meta or {}).get("occurred_at"),
                            extra_occurred_at=result.get("prev_occurred_at"),
                        )
            except Exception as exc:
                log.error("Background ingest error for %s: %s", req_id, exc)
            finally:
                ingest_queue.task_done()

    worker_task: asyncio.Task | None = None
    heal_task: asyncio.Task | None = None

    async def review_healer():
        tick = 0
        while True:
            await asyncio.sleep(180)
            tick += 1
            try:
                await asyncio.to_thread(clear_stale_review_jobs, brain)
                if tick % 5 == 0:
                    await asyncio.to_thread(prune_database_bloat, brain, allow_vacuum=False)
                is_idle = (
                    ingest_queue.empty()
                    and int(getattr(ingest_queue, "_unfinished_tasks", 0) or 0) == 0
                    and not retry_tasks
                )
                if is_idle:
                    await asyncio.to_thread(checkpoint_wal_if_idle, brain, is_idle=True)
                await asyncio.to_thread(run_online_backup, brain, backup_dir)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("review_healer iteration failed: %s", exc)

    def _check_db_and_backup_blocking() -> tuple[bool, str, dict[str, Any], dict[str, Any]]:
        try:
            with brain.engine._lock:
                row = brain.engine.db.execute("SELECT 1").fetchone()
            db_ok, db_err = bool(row and row[0] == 1), ""
        except Exception as exc:
            db_ok, db_err = False, str(exc)
        obs_public = dict(obsidian_exporter.status())
        obs_public.pop("host_export_dir", None)
        backup_snap = get_backup_status(brain, backup_dir)
        return db_ok, db_err, backup_snap, obs_public

    async def _build_health_response() -> tuple[dict[str, Any], int]:
        # Snapshot event-loop-owned asyncio state on the loop thread before offloading blocking DB/FS checks
        worker_alive = (worker_task is not None) and (not worker_task.done()) and (not shutting_down)
        qsize = ingest_queue.qsize()
        pending_r = len(retry_tasks)
        stats_snap = dict(ingest_stats)
        db_ok, db_err, backup_snap, obs_public = await asyncio.to_thread(_check_db_and_backup_blocking)

        backlog_limit = (
            HEALTH_BACKLOG_THRESHOLD
            if HEALTH_BACKLOG_THRESHOLD > 0
            else max(1, int(INGEST_QUEUE_MAXSIZE * 0.8))
        )
        backlog_ok = (qsize + pending_r) < backlog_limit

        reasons: list[str] = []
        if not db_ok:
            reasons.append(f"db_check_failed: {db_err or 'SELECT 1 failed'}")
        if not worker_alive:
            reasons.append("ingest_worker_not_running")
        if not backlog_ok:
            reasons.append(f"ingest_backlog_high: {qsize + pending_r} >= {backlog_limit}")

        healthy = len(reasons) == 0
        payload = {
            "status": "ok" if healthy else "unhealthy",
            "db_ok": db_ok,
            "worker_alive": worker_alive,
            "backlog_ok": backlog_ok,
            "unhealthy_reasons": reasons,
            "character": DEFAULT_CHARACTER_ID,
            "ingest": {
                "queue_size": qsize,
                "queue_maxsize": INGEST_QUEUE_MAXSIZE,
                "pending_retries": pending_r,
                **stats_snap,
            },
            "backup": backup_snap,
            "obsidian": obs_public,
        }
        return payload, (200 if healthy else 503)

    async def combined_app(scope, receive, send):
        nonlocal worker_task, heal_task, shutting_down
        if scope["type"] == "lifespan":
            async def wrapped_receive():
                msg = await receive()
                nonlocal worker_task, heal_task, shutting_down
                if msg["type"] == "lifespan.startup":
                    shutting_down = False
                    worker_task = asyncio.create_task(ingest_worker())
                    heal_task = asyncio.create_task(review_healer())
                    obsidian_runner.start()
                elif msg["type"] == "lifespan.shutdown":
                    shutting_down = True
                    if heal_task:
                        heal_task.cancel()
                        try:
                            await heal_task
                        except asyncio.CancelledError:
                            pass
                    harvested_retries: list[tuple[str, str, str, str, dict[str, Any], int]] = []
                    if retry_tasks:
                        pending_rt = list(retry_tasks)
                        for rt in pending_rt:
                            saved = retry_items.pop(rt, None)
                            if saved is not None:
                                harvested_retries.append(saved)
                            rt.cancel()
                        await asyncio.gather(*pending_rt, return_exceptions=True)
                    if harvested_retries:
                        log.info(
                            "Re-enqueuing %d deferred retry turn(s) for graceful shutdown drain...",
                            len(harvested_retries),
                        )
                        for h_item in harvested_retries:
                            try:
                                ingest_queue.put_nowait(h_item)
                            except asyncio.QueueFull:
                                try:
                                    await asyncio.wait_for(ingest_queue.put(h_item), timeout=1.5)
                                except asyncio.TimeoutError:
                                    ingest_stats["dropped_total"] += 1
                                    ingest_stats["dropped_queue_full"] += 1
                                    log.warning("Shutdown drain dropped overflow turn %s", h_item[1])
                    if worker_task and not worker_task.done():
                        pending_cnt = max(
                            ingest_queue.qsize(),
                            int(getattr(ingest_queue, "_unfinished_tasks", 0) or 0),
                        )
                        if pending_cnt:
                            log.info(
                                "Draining %d pending ingest turn(s) before shutdown (timeout=%.1fs)...",
                                pending_cnt,
                                SHUTDOWN_DRAIN_SECONDS,
                            )
                        try:
                            await asyncio.wait_for(
                                ingest_queue.join(), timeout=SHUTDOWN_DRAIN_SECONDS
                            )
                        except asyncio.TimeoutError:
                            log.warning(
                                "Shutdown ingest drain timed out after %.1fs (%d turn(s) remaining)",
                                SHUTDOWN_DRAIN_SECONDS,
                                ingest_queue.qsize(),
                            )
                        worker_task.cancel()
                        try:
                            await worker_task
                        except asyncio.CancelledError:
                            pass
                    await obsidian_runner.stop()
                return msg

            await studio_app(scope, wrapped_receive, send)
            return

        orig_send = send

        async def _send_with_clickjacking_headers(message):
            if scope["type"] == "http" and message.get("type") == "http.response.start":
                raw_hdrs = list(message.get("headers") or [])
                keys = {k.lower() for k, _ in raw_hdrs}
                if b"x-frame-options" not in keys:
                    raw_hdrs.append((b"x-frame-options", b"DENY"))
                if b"content-security-policy" not in keys:
                    raw_hdrs.append((b"content-security-policy", b"frame-ancestors 'none'"))
                message = dict(message, headers=raw_hdrs)
            await orig_send(message)

        if scope["type"] == "http":
            send = _send_with_clickjacking_headers

        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            headers_list = list(scope.get("headers", []))
            headers_dict = {k.decode("latin1").lower(): v.decode("latin1") for k, v in headers_list}

            # Handle XiaoZhi fast bridge endpoints directly
            if scope["type"] == "http" and path.startswith("/xiaozhi/"):
                req = Request(scope, receive=receive)
                if path == "/xiaozhi/health":
                    health_body, health_code = await _build_health_response()
                    resp = JSONResponse(health_body, status_code=health_code)
                    await resp(scope, receive, send)
                    return

                auth = headers_dict.get("authorization", "")
                if not secrets.compare_digest(
                    auth.encode("utf-8", errors="replace"),
                    f"Bearer {token}".encode("utf-8"),
                ):
                    resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
                    await resp(scope, receive, send)
                    return

                raw_bytes = await req.body()
                if not raw_bytes or not raw_bytes.strip():
                    raw_body: Any = {}
                else:
                    try:
                        raw_body = json.loads(raw_bytes)
                    except (ValueError, UnicodeDecodeError):
                        if req.method == "POST":
                            resp = JSONResponse(
                                {"error": "Invalid JSON request body"},
                                status_code=400,
                            )
                            await resp(scope, receive, send)
                            return
                        raw_body = {}
                if not isinstance(raw_body, dict):
                    if req.method == "POST":
                        resp = JSONResponse(
                            {"error": "Request body must be a JSON object"},
                            status_code=400,
                        )
                        await resp(scope, receive, send)
                        return
                    body = {}
                else:
                    body = raw_body

                char_id = str(
                    body.get("character_id")
                    or req.query_params.get("character_id")
                    or DEFAULT_CHARACTER_ID
                ).strip()

                if path == "/xiaozhi/relationship":
                    if req.method == "GET":
                        data = await asyncio.to_thread(get_relationship_snapshot, brain, char_id)
                        resp = JSONResponse(data)
                        await resp(scope, receive, send)
                        return
                    if req.method == "POST":
                        data = await asyncio.to_thread(update_relationship_settings, brain, char_id, body)
                        obsidian_runner.notify_db_change()
                        resp = JSONResponse(data)
                        await resp(scope, receive, send)
                        return

                if path == "/xiaozhi/obsidian/status" and req.method == "GET":
                    resp = JSONResponse(obsidian_exporter.status())
                    await resp(scope, receive, send)
                    return

                if path == "/xiaozhi/obsidian/config" and req.method == "POST":
                    if "enabled" not in body or not isinstance(body.get("enabled"), bool):
                        resp = JSONResponse(
                            {"error": "Field 'enabled' (boolean) is required"},
                            status_code=400,
                        )
                        await resp(scope, receive, send)
                        return
                    status_data = await obsidian_runner.set_enabled_async(bool(body["enabled"]))
                    resp = JSONResponse({"ok": True, "obsidian": status_data, **status_data})
                    await resp(scope, receive, send)
                    return

                if path == "/xiaozhi/obsidian/rebuild" and req.method == "POST":
                    result = await obsidian_runner.rebuild_now()
                    status_data = obsidian_exporter.status()
                    code = 200 if result.get("ok") else 500
                    resp = JSONResponse(
                        {"result": result, "obsidian": status_data, **result},
                        status_code=code,
                    )
                    await resp(scope, receive, send)
                    return

                if path == "/xiaozhi/recall" and req.method == "POST":
                    query = str(body.get("query") or "")
                    data = await asyncio.to_thread(build_fast_recall_context, brain, char_id, query)
                    resp = JSONResponse(data)
                    await resp(scope, receive, send)
                    return

                if path == "/xiaozhi/ingest" and req.method == "POST":
                    turns = body.get("turns")
                    if not isinstance(turns, list):
                        turns = [body]
                    if shutting_down:
                        rej_ids = [
                            str(item.get("request_id") or "").strip() or _new_request_id()
                            for item in turns
                            if isinstance(item, dict)
                            and str(item.get("user_message") or "").strip()
                            and str(item.get("assistant_reply") or "").strip()
                        ]
                        resp = JSONResponse(
                            {
                                "queued": 0,
                                "accepted_ids": [],
                                "rejected_ids": rej_ids,
                                "error": "shutting_down",
                            },
                            status_code=503,
                        )
                        await resp(scope, receive, send)
                        return
                    enqueued = 0
                    accepted_ids: list[str] = []
                    rejected_ids: list[str] = []
                    for item in turns:
                        if not isinstance(item, dict):
                            continue
                        u_msg = str(item.get("user_message") or "").strip()
                        a_msg = str(item.get("assistant_reply") or "").strip()
                        r_id = str(item.get("request_id") or "").strip() or _new_request_id()
                        if not (u_msg and a_msg):
                            continue
                        item_meta: dict[str, Any] = {}
                        if "occurred_at" in item and item["occurred_at"] is not None:
                            item_meta["occurred_at"] = item["occurred_at"]
                        if "source" in item and item["source"]:
                            item_meta["source"] = str(item["source"])
                        if "device_id" in item and item["device_id"]:
                            item_meta["device_id"] = str(item["device_id"])
                        if "used_tools" in item and item["used_tools"] is not None:
                            item_meta["used_tools"] = bool(item["used_tools"])

                        q_item = (char_id, r_id, u_msg, a_msg, item_meta, 0)
                        try:
                            ingest_queue.put_nowait(q_item)
                            enqueued += 1
                            ingest_stats["enqueued_total"] += 1
                            accepted_ids.append(r_id)
                        except asyncio.QueueFull:
                            placed = False
                            try:
                                await asyncio.wait_for(
                                    ingest_queue.put(q_item),
                                    timeout=INGEST_ENQUEUE_WAIT_SECONDS,
                                )
                                placed = True
                                enqueued += 1
                                ingest_stats["enqueued_total"] += 1
                                accepted_ids.append(r_id)
                            except asyncio.TimeoutError:
                                pass
                            if not placed:
                                scheduled = _schedule_delayed_reenqueue(
                                    (char_id, r_id, u_msg, a_msg, item_meta, 1),
                                    INGEST_PENDING_BACKOFF_BASE,
                                    "queue_full",
                                )
                                if scheduled:
                                    enqueued += 1
                                    ingest_stats["enqueued_total"] += 1
                                    ingest_stats["retried_total"] += 1
                                    accepted_ids.append(r_id)
                                    log.warning(
                                        "Ingest queue full; scheduled delayed retry for turn %s",
                                        r_id,
                                    )
                                else:
                                    ingest_stats["dropped_total"] += 1
                                    ingest_stats["dropped_queue_full"] += 1
                                    rejected_ids.append(r_id)
                                    log.warning(
                                        "Ingest queue & retry buffer full; rejecting turn %s",
                                        r_id,
                                    )

                    status_code = 503 if (rejected_ids and enqueued == 0) else 200
                    payload: dict[str, Any] = {
                        "queued": enqueued,
                        "accepted_ids": accepted_ids,
                        "rejected_ids": rejected_ids,
                    }
                    if status_code == 503:
                        payload["error"] = "ingest_queue_full"
                    resp = JSONResponse(payload, status_code=status_code)
                    await resp(scope, receive, send)
                    return

                resp = JSONResponse({"error": "Not found"}, status_code=404)
                await resp(scope, receive, send)
                return

            # Normalize trusted LAN/Docker Host & Origin headers for Companion Studio UI,
            # and sanitize non-ASCII Authorization bytes so http_server.py's compare_digest never 500s.
            host_val = headers_dict.get("host", "")
            needs_host_norm = host_val in allowed_hosts and host_val not in (
                f"127.0.0.1:{PORT}",
                f"localhost:{PORT}",
            )
            raw_auth = headers_dict.get("authorization", "")
            has_non_ascii_auth = any(ord(ch) >= 128 for ch in raw_auth)
            if needs_host_norm or has_non_ascii_auth:
                new_headers = []
                for k, v in headers_list:
                    kl = k.lower()
                    if needs_host_norm and kl == b"host":
                        new_headers.append((b"host", f"localhost:{PORT}".encode("latin1")))
                    elif needs_host_norm and kl == b"origin" and v.decode("latin1") == f"http://{host_val}":
                        new_headers.append((b"origin", f"http://localhost:{PORT}".encode("latin1")))
                    elif has_non_ascii_auth and kl == b"authorization":
                        new_headers.append((b"authorization", b"Bearer invalid-non-ascii"))
                    else:
                        new_headers.append((k, v))
                scope = dict(scope, headers=new_headers)

        is_studio_write = (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path", "") in _STUDIO_WRITE_ACTIONS
        )

        await studio_app(scope, receive, send)
        if is_studio_write:
            obsidian_runner.notify_db_change()

    return combined_app


def main():
    install_gemini_embedding_compat_patch()
    token = load_or_create_token()
    studio_url = f"http://localhost:{PORT}/#token={token}"
    URL_PATH.write_text(studio_url + "\n", encoding="utf-8")

    settings = Settings.from_env()
    # Keep reader_enabled off by default for fast single-hop chat in Companion Studio too
    if "COMPANION_READER_ENABLED" not in os.environ:
        settings.reader_enabled = False
    if "COMPANION_TIMEOUT_SECONDS" not in os.environ and settings.timeout_seconds < DEFAULT_TIMEOUT_SECONDS:
        settings.timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    automation = MemoryAutomation(
        embedding=bool(settings.embedding_base_url and settings.embedding_model),
        semantic_retrieval=bool(settings.embedding_base_url and settings.embedding_model),
        summaries=True,
        reconcile=True,
        summary_turns=8,
        summary_batch_messages=SUMMARY_BATCH_MESSAGES,
        retain_recent_turns=6,
        idle_seconds=300,
    )

    with CompanionBrain(
        DB_PATH,
        user_id="local",
        settings=settings,
        config=BrainConfig(
            capture_context=True,
            start_worker=False,  # started once by create_app lifespan
            allow_chat_images=True,
            automation=automation,
            reply_instructions=(
                "默认使用自然、真诚、温暖的中文口语交流（除非对方用其他语言提问）。"
                "根据话题自然舒展地陪伴聊天或讲故事，不说生硬客服套话。"
            ),
        ),
    ) as brain:
        ensure_default_character(brain)
        ensure_xiaozhi_turn_meta_table(brain.engine)
        configure_sqlite_wal(brain.engine)
        tighten_legacy_summary_batch_size(brain)
        prune_database_bloat(brain, allow_vacuum=True)
        clear_stale_review_jobs(brain, is_startup=True)
        run_online_backup(brain)
        app = create_combined_asgi_app(brain, token)
        log.info("==========================================================")
        log.info("Anima Brain + Companion Studio is READY on port %d", PORT)
        log.info("Database: %s", DB_PATH)
        log.info(
            "Obsidian Sync: enabled=%s export_dir=%s host_dir=%s interval=%ds",
            OBSIDIAN_SYNC_ENABLED,
            OBSIDIAN_EXPORT_DIR,
            OBSIDIAN_HOST_DIR,
            OBSIDIAN_SNAPSHOT_INTERVAL_SECONDS,
        )
        log.info("Studio URL: http://localhost:%d/#token=*** (full URL saved to %s)", PORT, URL_PATH)
        log.info("==========================================================")
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=PORT,
            access_log=False,
            ws_max_size=2 * 1024 * 1024,
        )


if __name__ == "__main__":
    main()

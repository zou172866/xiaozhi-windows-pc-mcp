"""Bounded 256KB log polling, cursor write throttling, boot replay, and watcher loop (H-1, D-4)."""

from __future__ import annotations

import asyncio
import atexit
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
import re
import secrets
import socket
import struct
import threading
import time
from urllib.parse import parse_qs, quote, urlparse
import wave

import httpx
import yaml

import asr_corrections
import mei_emotion_router
import patch_registry
import pc_audio_router
import reply_length_router
import runtime_events
import runtime_settings
import turn_context_router
import xz_common
from console import shared

_LOG_MAX_READ_BYTES = 256 * 1024
_LOG_CURSOR_WRITE_INTERVAL = 5.0
_active_watcher_state_lock = threading.Lock()
_active_watcher_state: dict | None = None


def _load_log_cursor() -> tuple[int | None, int]:
    try:
        if os.path.isfile(shared.LOG_CURSOR_PATH):
            with open(shared.LOG_CURSOR_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                ino = data.get("inode")
                off = int(data.get("offset", 0))
                if ino is not None and off >= 0:
                    return int(ino), off
    except Exception as exc:
        shared._log_rate_limited("load_log_cursor", exc)
    return None, 0


def _save_log_cursor(inode: int, offset: int) -> None:
    try:
        shared._atomic_write_text(
            shared.LOG_CURSOR_PATH,
            json.dumps({"inode": int(inode), "offset": int(offset)}),
        )
    except Exception as exc:
        shared._log_rate_limited("save_log_cursor", exc)


def flush_log_cursor_if_dirty(watcher_state: dict | None = None) -> bool:
    """Persist pending throttled log cursor if dirty (used on clean shutdown or explicit flush)."""
    st = watcher_state
    if st is None:
        with _active_watcher_state_lock:
            st = _active_watcher_state
    if not isinstance(st, dict) or not st.get("cursor_dirty"):
        return False
    ino = st.get("cursor_inode") or st.get("last_inode")
    if ino is None:
        return False
    _save_log_cursor(int(ino), int(shared._log_offset))
    st["last_cursor_save_ts"] = time.monotonic()
    st["cursor_dirty"] = False
    return True


def _process_log_line(line: str, *, status_only: bool = False):
    s = line.strip()
    if not s:
        return

    with shared._state_lock:
        shared._append_raw_log_locked(s)

        m_time = shared._RE_TIME.match(s)
        ts_short = m_time.group(1)[11:19] if m_time else time.strftime("%H:%M:%S")

        if "ASR模块初始化完成" in s or "Websocket地址是" in s:
            shared._device_info["server_ready"] = True

        m_dev = shared._RE_OTA_DEV.search(s)
        if m_dev:
            shared._device_info["device_id"] = m_dev.group(1)
            shared._device_info["last_seen"] = ts_short

        if status_only:
            return

        if shared._RE_LISTEN_START.search(s):
            shared._finalize_open_ai_turn()
            shared._device_info["activity"] = "listening"
            shared._device_info["last_seen"] = ts_short
            return

        m_user = shared._RE_USER_MSG.search(s)
        if m_user:
            shared._finalize_open_ai_turn()
            text = m_user.group(1).strip()
            shared._device_info["activity"] = "speaking"
            shared._device_info["last_seen"] = ts_short
            if not shared._is_duplicate_chat_message("user", ts_short, text):
                shared._chat_messages.append({"role": "user", "text": text, "time": ts_short})
                shared._save_history()
            return

        m_ai = shared._RE_AI_SENTENCE.search(s)
        if m_ai:
            stype, raw_content = m_ai.group(1), m_ai.group(2).strip()
            content = (
                "None"
                if raw_content == "None"
                else mei_emotion_router.strip_emotion_tags(raw_content).strip()
            )
            shared._device_info["last_seen"] = ts_short
            if stype == "LAST" or content == "None":
                shared._finalize_open_ai_turn()
                shared._device_info["activity"] = "idle"
                shared._save_history()
            elif content:
                shared._device_info["activity"] = "speaking"
                if shared._current_ai_turn_open and shared._chat_messages and shared._chat_messages[-1]["role"] == "assistant":
                    shared._chat_messages[-1]["text"] = shared._join_cn_clauses(shared._chat_messages[-1]["text"], content)
                else:
                    shared._chat_messages.append({"role": "assistant", "text": content, "time": ts_short})
                    shared._current_ai_turn_open = True
            return


def _stream_status_prefix(log_path: str, limit_bytes: int) -> None:
    """Replay [0..limit_bytes] in streaming fashion updating only status flags & raw logs."""
    remaining = max(0, int(limit_bytes))
    if not remaining:
        return
    with open(log_path, "rb") as f:
        buf = b""
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                ln = buf[:nl].decode("utf-8", errors="replace")
                buf = buf[nl + 1 :]
                _process_log_line(ln, status_only=True)
        if buf:
            _process_log_line(buf.decode("utf-8", errors="replace"), status_only=True)


def _replay_boot_lines_merged(lines: list[str]) -> None:
    """Replay a full log on cursor-less boot without duplicating or scrambling existing history."""
    replayed_msgs: list[dict] = []
    open_ai = False
    last_activity: str | None = None
    last_seen: str | None = None

    for line in lines:
        s = line.strip()
        if not s:
            continue
        _process_log_line(s, status_only=True)

        m_time = shared._RE_TIME.match(s)
        ts_short = m_time.group(1)[11:19] if m_time else time.strftime("%H:%M:%S")

        if shared._RE_LISTEN_START.search(s):
            open_ai = False
            last_activity = "listening"
            last_seen = ts_short
            continue

        m_user = shared._RE_USER_MSG.search(s)
        if m_user:
            open_ai = False
            text = m_user.group(1).strip()
            last_activity = "speaking"
            last_seen = ts_short
            replayed_msgs.append({"role": "user", "text": text, "time": ts_short})
            continue

        m_ai = shared._RE_AI_SENTENCE.search(s)
        if m_ai:
            stype, raw_content = m_ai.group(1), m_ai.group(2).strip()
            content = (
                "None"
                if raw_content == "None"
                else mei_emotion_router.strip_emotion_tags(raw_content).strip()
            )
            last_seen = ts_short
            if stype == "LAST" or content == "None":
                open_ai = False
                last_activity = "idle"
            elif content:
                last_activity = "speaking"
                if open_ai and replayed_msgs and replayed_msgs[-1]["role"] == "assistant":
                    replayed_msgs[-1]["text"] = shared._join_cn_clauses(replayed_msgs[-1]["text"], content)
                else:
                    replayed_msgs.append({"role": "assistant", "text": content, "time": ts_short})
                    open_ai = True

    with shared._state_lock:
        if last_activity is not None:
            shared._device_info["activity"] = last_activity
        if last_seen is not None:
            shared._device_info["last_seen"] = last_seen

        # Cap replayed window to newest 100 turns so aged log lines already rotated out of
        # chat_history.json are never appended out of order behind newer persisted turns.
        recent_replayed = replayed_msgs[-100:]
        existing_keys = {
            (str(m.get("role") or ""), str(m.get("time") or ""), str(m.get("text") or ""))
            for m in shared._chat_messages
        }
        changed = False
        last_added = False
        for idx, item in enumerate(recent_replayed):
            k = (item["role"], item["time"], item["text"])
            if k in existing_keys:
                last_added = False
                continue
            existing_keys.add(k)
            shared._chat_messages.append(item)
            changed = True
            last_added = idx == len(recent_replayed) - 1
        shared._current_ai_turn_open = bool(open_ai and last_added)
        if changed:
            shared._trim_chat_messages()
            shared._save_history()


def _poll_log_once(watcher_state: dict, *, raw_logs_only: bool = False) -> None:
    if not os.path.exists(shared.LOG_PATH):
        return
    st = os.stat(shared.LOG_PATH)
    is_boot_replay = False
    if not watcher_state.get("initialized"):
        watcher_state["initialized"] = True
        saved_ino = watcher_state.get("saved_inode")
        saved_off = int(watcher_state.get("saved_offset") or 0)
        if raw_logs_only:
            # Structured runtime event bus owns chat & device status; tail recent log lines only for log drawer
            tail_bytes = 65536
            shared._log_offset = max(0, st.st_size - tail_bytes)
            if shared._log_offset > 0:
                try:
                    with open(shared.LOG_PATH, "rb") as _rf:
                        _rf.seek(shared._log_offset)
                        _first_chunk = _rf.read(4096)
                    _nl = _first_chunk.find(b"\n")
                    if _nl >= 0:
                        shared._log_offset += _nl + 1
                except Exception as exc:
                    shared._log_rate_limited("poll_log_tail_align", exc)
            watcher_state["last_inode"] = st.st_ino
        elif saved_ino == st.st_ino and 0 < saved_off <= st.st_size:
            _stream_status_prefix(shared.LOG_PATH, saved_off)
            shared._log_offset = saved_off
            watcher_state["last_inode"] = st.st_ino
        else:
            shared._log_offset = 0
            watcher_state["last_inode"] = st.st_ino
            is_boot_replay = True
    elif watcher_state.get("last_inode") != st.st_ino or st.st_size < shared._log_offset:
        shared._log_offset = 0
        watcher_state["last_inode"] = st.st_ino

    consumed_any = False
    if st.st_size > shared._log_offset:
        with open(shared.LOG_PATH, "rb") as f:
            f.seek(shared._log_offset)
            # D-4: Cap log read to at most 256 KB per tick to bound memory/CPU spikes
            raw = f.read(_LOG_MAX_READ_BYTES)
        last_nl = raw.rfind(b"\n")
        if last_nl >= 0:
            to_consume = raw[: last_nl + 1]
        elif len(raw) >= _LOG_MAX_READ_BYTES:
            # Giant >256 KB single line without newline: consume chunk so watcher never stalls
            to_consume = raw
        else:
            to_consume = b""

        if to_consume:
            shared._log_offset += len(to_consume)
            consumed_any = True
            lines = to_consume.decode("utf-8", errors="replace").splitlines()
            if raw_logs_only:
                with shared._state_lock:
                    for ln in lines:
                        s = ln.strip()
                        if s:
                            shared._append_raw_log_locked(s)
            elif is_boot_replay and shared._chat_messages:
                _replay_boot_lines_merged(lines)
            else:
                for ln in lines:
                    _process_log_line(ln, status_only=False)
                with shared._state_lock:
                    shared._trim_chat_messages()
            watcher_state["cursor_dirty"] = True
            watcher_state["cursor_inode"] = st.st_ino

    # D-4: Write log cursor to disk at most once every 5s (first write on a fresh watcher flushes immediately)
    if watcher_state.get("cursor_dirty"):
        now_mono = time.monotonic()
        last_save = float(watcher_state.get("last_cursor_save_ts") or 0.0)
        if last_save <= 0.0 or (now_mono - last_save) >= _LOG_CURSOR_WRITE_INTERVAL:
            _save_log_cursor(int(watcher_state.get("cursor_inode") or st.st_ino), shared._log_offset)
            watcher_state["last_cursor_save_ts"] = now_mono
            watcher_state["cursor_dirty"] = False

    # Legacy non-hooks log-replay path (runs in background watcher thread, never in sentence_start):
    # coalesce the full batch into at most one disk flush after processing lines.
    if consumed_any and not raw_logs_only:
        shared.flush_history_if_dirty()


def _log_watcher_loop():
    global _active_watcher_state
    shared._load_history()
    shared._sync_runtime_events()
    saved_ino, saved_off = _load_log_cursor()
    watcher_state = {
        "initialized": False,
        "last_inode": None,
        "saved_inode": saved_ino,
        "saved_offset": saved_off,
        "cursor_dirty": False,
        "cursor_inode": saved_ino,
        "last_cursor_save_ts": 0.0,
    }
    with _active_watcher_state_lock:
        _active_watcher_state = watcher_state
    while True:
        try:
            shared._sync_runtime_events()
            _poll_log_once(watcher_state, raw_logs_only=runtime_events.is_hooks_active())
        except Exception as exc:
            shared._log_rate_limited("log_watcher_loop", exc)
        time.sleep(0.4)


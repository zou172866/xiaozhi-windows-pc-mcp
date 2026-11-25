"""Shared console configuration, security validation, secret redaction, YAML cache, and chat history state (H-1)."""

from __future__ import annotations

import asyncio
import atexit
from collections import deque
import copy
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

ROOT_DIR = xz_common.ROOT_DIR
APP_DIR = xz_common.APP_DIR
CONFIG_DIR = xz_common.CONFIG_DIR
STATE_DIR = xz_common.STATE_DIR
VOICES_DIR = xz_common.VOICES_DIR
DATA_DIR = CONFIG_DIR
CONFIG_PATH = os.path.join(CONFIG_DIR, ".config.yaml")
STATIC_DIR = os.path.join(APP_DIR, "console", "static")
HTML_PATH = os.path.join(STATIC_DIR, "index.html")
CSS_PATH = os.path.join(STATIC_DIR, "app.css")
JS_PATH = os.path.join(STATIC_DIR, "app.js")
HISTORY_PATH = os.path.join(STATE_DIR, "chat_history.json")
LOG_CURSOR_PATH = os.path.join(STATE_DIR, ".console_log_cursor.json")
CONSOLE_TOKEN_PATH = os.path.join(STATE_DIR, ".console_token")
_DEFAULT_MODE_PATH = os.path.join(STATE_DIR, "pc_control_mode.json")
MODE_PATH = _DEFAULT_MODE_PATH
LOG_PATH = os.path.join(ROOT_DIR, "tmp", "server.log")

CONSOLE_PORT = int(os.environ.get("CONSOLE_PORT", "18080"))
GSV_Host = "http://host.docker.internal:19880"
PC_MCP_HOST = os.environ.get("PC_AUDIO_BRIDGE_URL", "http://host.docker.internal:18900").rstrip("/")
GSV_INFER_YAML = os.path.join(CONFIG_DIR, "tts_infer_xiaozhi.yaml")
ALLOWED_L3_UPDATE_KEYS = frozenset({"backend", "timeout_seconds", "timeout"})
VALID_L3_BACKENDS = ("antigravity", "dsh", "workbuddy", "none")
_HOST_DIRS = xz_common.resolve_host_dirs()
HOST_DATA_DIR = _HOST_DIRS["HOST_DATA_DIR"]
HOST_PROJECT_DIR = _HOST_DIRS["HOST_PROJECT_DIR"]
HOST_VOICES_DIR = _HOST_DIRS["HOST_VOICES_DIR"]
HOST_GSV_WEIGHTS_DIR = _HOST_DIRS["HOST_GSV_WEIGHTS_DIR"]
HOST_LAN_IP = (os.environ.get("HOST_LAN_IP") or "").strip() or "127.0.0.1"

# A-4: Per-boot random write token injected into console/static/index.html and required via X-Console-Token on ALL POSTs
_CONSOLE_WRITE_TOKEN = secrets.token_urlsafe(32)
_CONSOLE_BOOT_ID = secrets.token_hex(8)
_CONSOLE_TOKEN_PLACEHOLDER = "__XIAOZHI_CONSOLE_TOKEN__"
_INTERNAL_READONLY_PATHS = frozenset({"/api/health", "/api/state"})


def get_console_token() -> str:
    return _CONSOLE_WRITE_TOKEN


def persist_console_token(path: str | None = None) -> str:
    """Persist per-boot `_CONSOLE_WRITE_TOKEN` to host-mounted `state/.console_token` (0600) for local CLI/script automation (R1 #5).

    Must ONLY be called from `server.start_console_background()` (never at import time, H-1).
    """
    target = path if path is not None else CONSOLE_TOKEN_PATH
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
    payload = (_CONSOLE_WRITE_TOKEN + "\n").encode("utf-8")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, target)
    except Exception as exc:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        _log_rate_limited("persist_console_token", exc)
    return target


def get_console_boot_id() -> str:
    return _CONSOLE_BOOT_ID


_SECRET_PATTERNS = (
    (re.compile(r"(Bearer[\s;]+)[A-Za-z0-9._\-~+/=]{6,}", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Basic\s+)[A-Za-z0-9+/=]{8,}", re.IGNORECASE), r"\1***"),
    (re.compile(r"((?<![A-Za-z0-9_])Token\s+)[A-Za-z0-9._\-~+/=]{6,}", re.IGNORECASE), r"\1***"),
    (
        re.compile(
            r"((?:https?|wss?|ftp)://)[^/\s:@\"']+:[^/\s@\"']+@",
            re.IGNORECASE,
        ),
        r"\1***:***@",
    ),
    (
        re.compile(
            r"((?:https?|wss?|ftp)://)[^/\s:@\"']+@",
            re.IGNORECASE,
        ),
        r"\1***@",
    ),
    (
        re.compile(
            r"([?#&](?:access_token|refresh_token|auth_token|token|api_key|api-key|apikey|auth_key|key|secret|secret_key|client_secret|password|passwd|pwd|groupid)=)[^\s&#\"',;)}\]]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (
        re.compile(
            r"((?<![A-Za-z0-9_])(?:x-goog-api-key|x-api-key|x-api-access-key|x-api-app-key|x-console-token|api_key|api-key|apikey|access_token|refresh_token|auth_token|auth_key|token|secret|secret_key|client_secret|password|passwd|pwd|appid|group_id|pc_mcp_token|anima_studio_token|esp32_auth_key|deepseek_api_key|glm_api_key|gemini_api_key|siliconflow_api_key|openai_audio_api_key|dashscope_api_key|doubao_appid|doubao_asr_appid|doubao_tts_appid|doubao_access_token|doubao_asr_access_token|doubao_tts_access_token|fishspeech_api_key|minimax_api_key|minimax_group_id)(?![A-Za-z0-9_])\s*[\"']?\s*[:=]\s*[\"']?)(?!None\b|null\b|true\b|false\b)[^\s&\"',;)}\]]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (
        re.compile(
            r"(authorization\s*[\"']?\s*[:=]\s*[\"']?(?:(?:Bearer|Basic|Token)[\s;]+)?)[^\s&\"',;)}\]]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_\-]{12,}(?![A-Za-z0-9_\-])"), "sk-***"),
    (re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_\-]{16,}(?![A-Za-z0-9_\-])"), "AIza***"),
    (re.compile(r"(?<![A-Za-z0-9_])ghp_[A-Za-z0-9]{16,}(?![A-Za-z0-9_])"), "ghp_***"),
    (re.compile(r"(?<![A-Za-z0-9_])xox[baprs]-[A-Za-z0-9\-]{10,}(?![A-Za-z0-9\-])"), "xox-***"),
)

_ERROR_PAYLOAD_KEYS = frozenset(
    {
        "error",
        "detail",
        "message",
        "rollback_warning",
        "voice_dirty_error",
        "last_error",
        "degraded_reasons",
        "security_failures",
        "unhealthy_reasons",
        "warning",
    }
)


def _redact_secrets(text: str) -> str:
    s = str(text or "")
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s


def _redact_json_payload(obj, *, redact_all_strings: bool = False):
    """Recursively redact secrets in error responses and error-bearing fields (D-5)."""
    if isinstance(obj, str):
        return _redact_secrets(obj) if redact_all_strings else obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            force_child = redact_all_strings or (str(k).lower() in _ERROR_PAYLOAD_KEYS)
            out[k] = _redact_json_payload(v, redact_all_strings=force_child)
        return out
    if isinstance(obj, list):
        return [_redact_json_payload(item, redact_all_strings=redact_all_strings) for item in obj]
    if isinstance(obj, tuple):
        return [_redact_json_payload(item, redact_all_strings=redact_all_strings) for item in obj]
    return obj


_RATE_LOG_INTERVAL_SECONDS = 60.0
_rate_log_lock = threading.Lock()
_rate_log_last_ts: dict[str, float] = {}
_rate_log_suppressed: dict[str, int] = {}


def _reset_rate_limited_logs() -> None:
    with _rate_log_lock:
        _rate_log_last_ts.clear()
        _rate_log_suppressed.clear()


def _log_rate_limited(
    site: str,
    exc: BaseException | str,
    *,
    interval: float | None = None,
    context: str = "",
) -> bool:
    """Emit a redacted warning at most once per `interval` seconds (default 60s) per `site` key (D-6)."""
    window = _RATE_LOG_INTERVAL_SECONDS if interval is None else max(0.0, float(interval))
    now = time.monotonic()
    key = str(site or "unknown")
    with _rate_log_lock:
        last_ts = _rate_log_last_ts.get(key)
        if last_ts is not None and (now - last_ts) < window:
            _rate_log_suppressed[key] = _rate_log_suppressed.get(key, 0) + 1
            return False
        suppressed = _rate_log_suppressed.pop(key, 0)
        _rate_log_last_ts[key] = now

    detail = _redact_secrets(f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc))
    ctx_part = f" ({_redact_secrets(context)})" if context else ""
    sup_part = f" [suppressed {suppressed} similar in last {int(window)}s]" if suppressed else ""
    print(f"[WebConsole] WARN [{key}]{ctx_part}: {detail}{sup_part}", flush=True)
    return True


def _atomic_write_text(path: str, content: str) -> None:
    xz_common.atomic_write_text(path, content)


def get_allowed_console_origins(port: int | None = None) -> frozenset[str]:
    """Return exact allowed http-only loopback origins for the console port (A-4, P1-1)."""
    p = CONSOLE_PORT if port is None else int(port)
    return frozenset(
        {
            f"http://localhost:{p}",
            f"http://127.0.0.1:{p}",
            f"http://[::1]:{p}",
        }
    )


_SINGLETON_SECURITY_HEADERS = frozenset(
    {
        "host",
        "origin",
        "referer",
        "sec-fetch-site",
        "content-type",
        "content-length",
        "transfer-encoding",
        "x-console-token",
        "access-control-request-method",
    }
)
_BAD_REQUEST_DUPLICATE_HEADERS = frozenset({"host", "content-length", "transfer-encoding"})


def validate_console_request(
    method: str,
    headers: dict,
    port: int | None = None,
    path: str = "",
    expected_token: str | None = None,
) -> tuple[bool, int, str]:
    """Validate Host, internal-host read-only scope, browser CSRF guards, and per-boot X-Console-Token (A-4)."""
    if port is None:
        port = CONSOLE_PORT
    norm_headers: dict[str, str] = {}
    dup_bad_request: str | None = None
    dup_forbidden: str | None = None
    for raw_k, raw_v in (headers or {}).items():
        k_low = str(raw_k).lower()
        v_clean = str(raw_v).strip()
        if k_low in norm_headers:
            if k_low in _BAD_REQUEST_DUPLICATE_HEADERS:
                dup_bad_request = dup_bad_request or k_low
                continue
            if k_low in _SINGLETON_SECURITY_HEADERS:
                dup_forbidden = dup_forbidden or k_low
                continue
            if k_low == "access-control-request-headers" and v_clean:
                prev = norm_headers[k_low]
                norm_headers[k_low] = f"{prev}, {v_clean}" if prev else v_clean
                continue
        norm_headers[k_low] = v_clean

    if dup_bad_request == "host":
        return False, int(HTTPStatus.BAD_REQUEST), "Duplicate Host header"

    local_hosts = {
        f"localhost:{port}",
        f"127.0.0.1:{port}",
        f"[::1]:{port}",
        "localhost",
        "127.0.0.1",
        "[::1]",
    }
    internal_hosts = {
        f"xiaozhi-esp32-server:{port}",
        "xiaozhi-esp32-server",
    }
    allowed_origins = get_allowed_console_origins(port)

    host = norm_headers.get("host", "").lower()
    is_internal = host in internal_hosts
    if not host or (host not in local_hosts and not is_internal):
        return False, int(HTTPStatus.MISDIRECTED_REQUEST), "Invalid Host header"

    if dup_bad_request:
        return False, int(HTTPStatus.BAD_REQUEST), f"Duplicate {dup_bad_request} header"
    if dup_forbidden:
        return False, int(HTTPStatus.FORBIDDEN), f"Duplicate {dup_forbidden} header rejected"

    meth = str(method).upper()
    req_path = urlparse(str(path or "")).path

    # A-4: Internal container hostname `xiaozhi-esp32-server` may ONLY access necessary read-only APIs
    # (never GET / HTML containing write token, and never any POST write endpoint).
    if is_internal:
        if meth != "GET" or req_path not in _INTERNAL_READONLY_PATHS:
            return (
                False,
                int(HTTPStatus.FORBIDDEN),
                "Host xiaozhi-esp32-server is restricted to read-only health/state endpoints",
            )
        return True, int(HTTPStatus.OK), ""

    if meth == "OPTIONS":
        has_preflight_hdr = (
            "access-control-request-method" in norm_headers
            or "access-control-request-headers" in norm_headers
        )
        if "origin" in norm_headers or has_preflight_hdr:
            origin = norm_headers.get("origin", "").lower()
            if not origin or origin not in allowed_origins:
                return False, int(HTTPStatus.FORBIDDEN), "Cross-origin OPTIONS rejected"
        else:
            referer = norm_headers.get("referer", "").lower()
            if referer and not any(referer == o or referer.startswith(o + "/") for o in allowed_origins):
                return False, int(HTTPStatus.FORBIDDEN), "Cross-origin Referer rejected"

        sec_fetch_site = norm_headers.get("sec-fetch-site", "").lower()
        if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
            return False, int(HTTPStatus.FORBIDDEN), "Cross-site OPTIONS rejected"

        if "access-control-request-method" in norm_headers:
            req_meth = norm_headers.get("access-control-request-method", "").upper()
            if req_meth not in ("GET", "POST", "OPTIONS"):
                return (
                    False,
                    int(HTTPStatus.METHOD_NOT_ALLOWED),
                    "Unsupported Access-Control-Request-Method",
                )

        return True, int(HTTPStatus.NO_CONTENT), ""

    if meth == "POST":
        if "transfer-encoding" in norm_headers:
            return False, int(HTTPStatus.BAD_REQUEST), "Transfer-Encoding is not supported"
        origin = norm_headers.get("origin", "").lower()
        if origin:
            if origin not in allowed_origins:
                return False, int(HTTPStatus.FORBIDDEN), "Cross-origin POST rejected"
        else:
            referer = norm_headers.get("referer", "").lower()
            if referer and not any(referer == o or referer.startswith(o + "/") for o in allowed_origins):
                return False, int(HTTPStatus.FORBIDDEN), "Cross-origin Referer rejected"

        sec_fetch_site = norm_headers.get("sec-fetch-site", "").lower()
        if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
            return False, int(HTTPStatus.FORBIDDEN), "Cross-site POST rejected"

        content_type = norm_headers.get("content-type", "").lower()
        media_type = content_type.split(";", 1)[0].strip()
        if media_type != "application/json":
            return False, int(HTTPStatus.UNSUPPORTED_MEDIA_TYPE), "POST Content-Type must be application/json"

        # A-4: Require per-boot X-Console-Token on ALL POST endpoints
        tok = _CONSOLE_WRITE_TOKEN if expected_token is None else str(expected_token)
        supplied = norm_headers.get("x-console-token", "")
        if not tok or not supplied or not secrets.compare_digest(
            supplied.encode("utf-8", errors="replace"),
            tok.encode("utf-8"),
        ):
            return False, int(HTTPStatus.UNAUTHORIZED), "Missing or invalid X-Console-Token"

    return True, int(HTTPStatus.OK), ""


_console_http_lock = threading.Lock()
_console_http_client: httpx.Client | None = None


def _get_console_http_client() -> httpx.Client:
    """Shared persistent keep-alive httpx.Client (trust_env=False) for local 18900/18787/19880 calls."""
    global _console_http_client
    with _console_http_lock:
        if _console_http_client is None or _console_http_client.is_closed:
            _console_http_client = httpx.Client(
                trust_env=False,
                limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
            )
        return _console_http_client


class _BadRequestError(ValueError):
    """Raised when a POST request body is malformed, oversized, or not a JSON object."""


_state_lock = threading.Lock()
_chat_messages = []
_raw_logs: deque[tuple[int, str]] = deque(maxlen=150)
_raw_log_seq = 0
_device_info = {
    "device_id": "",
    "last_seen": "",
    "activity": "idle",
    "server_ready": False,
}
_current_ai_turn_open = False
_turn_has_tts = False
_turn_dialogue_bubble_open = False
_last_event_seq = 0
_log_offset = 0


def _append_raw_log_locked(line: str) -> int:
    global _raw_log_seq
    _raw_log_seq += 1
    _raw_logs.append((_raw_log_seq, _redact_secrets(line)))
    return _raw_log_seq


def _get_logs_for_state(cursor: int | None) -> tuple[list[str], int, bool, bool]:
    """Return (lines, latest_cursor, incremental, reset) for /api/state (P2-22).

    Without `cursor`, returns full tail (up to 100 lines) for full backward compatibility.
    With `cursor`, returns only lines with seq > cursor unless the ring wrapped or restarted.
    """
    with _state_lock:
        latest_seq = _raw_log_seq
        snapshot = list(_raw_logs)

    if cursor is None:
        return [txt for _, txt in snapshot[-100:]], latest_seq, False, False

    if not snapshot:
        reset = cursor > latest_seq
        return [], latest_seq, not reset, reset

    earliest_seq = snapshot[0][0]
    if cursor < 0 or cursor > latest_seq or cursor < (earliest_seq - 1):
        return [txt for _, txt in snapshot[-100:]], latest_seq, False, True

    delta = [txt for seq, txt in snapshot if seq > cursor]
    return delta, latest_seq, True, False


def _is_device_busy() -> bool:
    """Return True if ESP32 is actively speaking or a live GPT-SoVITS GPU synthesis is running (P1-14)."""
    if mei_emotion_router.is_live_gsv_busy():
        return True
    if runtime_events.is_any_device_speaking():
        return True
    with _state_lock:
        return _device_info.get("activity") == "speaking"


def _norm_spoken_key(text: str) -> str:
    """Normalize text to alphanumeric/CJK characters for Markdown-tolerant dedup."""
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", str(text or ""))).lower()


def _is_segment_already_covered(existing_text: str, seg_text: str) -> bool:
    if not seg_text:
        return True
    if seg_text in existing_text:
        return True
    norm_seg = _norm_spoken_key(seg_text)
    if norm_seg and norm_seg in _norm_spoken_key(existing_text):
        return True
    return False


def _apply_runtime_event(evt: dict) -> None:
    """Consume a structured in-process runtime event into console chat & device state (P1-11)."""
    global _current_ai_turn_open, _turn_has_tts, _turn_dialogue_bubble_open, _last_event_seq
    if not isinstance(evt, dict):
        return
    seq = int(evt.get("seq") or 0)
    etype = str(evt.get("type") or "")
    ts_short = str(evt.get("time") or time.strftime("%H:%M:%S"))
    dev_id = str(evt.get("device_id") or "").strip()

    with _state_lock:
        if seq > 0:
            if seq <= _last_event_seq:
                return
            _last_event_seq = seq

        if dev_id:
            _device_info["device_id"] = dev_id
            _device_info["last_seen"] = ts_short

        if etype == "server_ready":
            _device_info["server_ready"] = True
            return

        if etype == "device_seen":
            _device_info["last_seen"] = ts_short
            return

        if etype == "listen_start":
            _finalize_open_ai_turn()
            _turn_has_tts = False
            _turn_dialogue_bubble_open = False
            _device_info["activity"] = "listening"
            _device_info["last_seen"] = ts_short
            return

        if etype == "user_message":
            _finalize_open_ai_turn()
            _turn_has_tts = False
            _turn_dialogue_bubble_open = False
            text = str(evt.get("text") or "").strip()
            _device_info["activity"] = "speaking"
            _device_info["last_seen"] = ts_short
            if text and not _is_duplicate_chat_message("user", ts_short, text):
                _chat_messages.append({"role": "user", "text": text, "time": ts_short})
                _trim_chat_messages()
                _save_history()
            return

        if etype == "connection_closed":
            _finalize_open_ai_turn()
            _turn_has_tts = False
            _turn_dialogue_bubble_open = False
            if not runtime_events.is_any_device_speaking():
                _device_info["activity"] = "idle"
            _save_history()
            return

        if etype == "tts_state":
            state = str(evt.get("state") or "")
            raw_txt = evt.get("text")
            _device_info["last_seen"] = ts_short
            if state == "start":
                _device_info["activity"] = "speaking"
            elif state == "stop":
                _finalize_open_ai_turn()
                _turn_has_tts = False
                _turn_dialogue_bubble_open = False
                _device_info["activity"] = "idle"
                _save_history()
            elif state == "sentence_start" and raw_txt:
                content = mei_emotion_router.strip_emotion_tags(str(raw_txt)).strip()
                if content:
                    _device_info["activity"] = "speaking"
                    if _turn_dialogue_bubble_open and _chat_messages and _chat_messages[-1]["role"] == "assistant":
                        if not _is_segment_already_covered(_chat_messages[-1]["text"], content):
                            _chat_messages[-1]["text"] = _join_cn_clauses(_chat_messages[-1]["text"], content)
                        _current_ai_turn_open = True
                    elif _current_ai_turn_open and _chat_messages and _chat_messages[-1]["role"] == "assistant":
                        _chat_messages[-1]["text"] = _join_cn_clauses(_chat_messages[-1]["text"], content)
                    else:
                        _chat_messages.append({"role": "assistant", "text": content, "time": ts_short})
                        _current_ai_turn_open = True
                    _turn_has_tts = True
                    _trim_chat_messages()
                    _save_history()
            return

        if etype == "assistant_message":
            content = mei_emotion_router.strip_emotion_tags(str(evt.get("text") or "")).strip()
            if not content:
                return
            _device_info["last_seen"] = ts_short
            if _turn_has_tts:
                # Spoken segments already streamed via send_tts_message("sentence_start"); avoid duplicate bubble
                return
            if _turn_dialogue_bubble_open and _chat_messages and _chat_messages[-1]["role"] == "assistant":
                if not _is_segment_already_covered(_chat_messages[-1]["text"], content):
                    _chat_messages[-1]["text"] = _join_cn_clauses(_chat_messages[-1]["text"], content)
                    _save_history()
            elif not _is_duplicate_chat_message("assistant", ts_short, content):
                _chat_messages.append({"role": "assistant", "text": content, "time": ts_short})
                _current_ai_turn_open = True
                _turn_dialogue_bubble_open = True
                _trim_chat_messages()
                _save_history()


def _sync_runtime_events() -> None:
    """Drain any unconsumed events from the in-process runtime ring buffer."""
    with _state_lock:
        since = _last_event_seq
    pending = runtime_events.get_events_since(since)
    for evt in pending:
        _apply_runtime_event(evt)


def _dedup_chat_items(items: list) -> list:
    deduped = []
    seen_recent = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("role") or ""),
            str(item.get("time") or ""),
            str(item.get("text") or ""),
        )
        if key in seen_recent:
            continue
        seen_recent.add(key)
        deduped.append(item)
    return deduped


_HISTORY_FLUSH_INTERVAL = 2.0
_history_dirty_lock = threading.Lock()
_history_flush_lock = threading.Lock()
_history_dirty = False
_history_dirty_gen = 0
_history_flushed_gen = 0
_history_stop_event = threading.Event()
_history_flusher_thread: threading.Thread | None = None


def _load_history():
    global _chat_messages
    loaded = None
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                items = json.load(f)
                if isinstance(items, list):
                    loaded = _dedup_chat_items(items)[-100:]
        except Exception as exc:
            _log_rate_limited("load_history", exc)
            loaded = []
    if loaded is not None:
        with _state_lock:
            if not _chat_messages:
                _chat_messages = loaded
            else:
                _chat_messages = _dedup_chat_items(loaded + _chat_messages)[-100:]


def _save_history() -> None:
    """D-1: Mark chat history dirty for coalesced background disk flush (~2s); ZERO synchronous disk I/O."""
    global _history_dirty, _history_dirty_gen
    with _history_dirty_lock:
        _history_dirty = True
        _history_dirty_gen += 1


def is_history_dirty() -> bool:
    with _history_dirty_lock:
        return bool(_history_dirty)


def flush_history_if_dirty(*, force: bool = False) -> bool:
    """Coalesce and atomically persist chat history outside _state_lock (D-1). Returns True if written.

    Lock order: _history_flush_lock -> _state_lock -> _history_dirty_lock.
    Acquiring _history_flush_lock before snapshotting guarantees concurrent flushers
    (e.g. background 2s timer vs shutdown flush) can never overwrite a newer generation with an older snapshot.
    """
    global _history_dirty, _history_flushed_gen
    with _history_dirty_lock:
        if not force and not _history_dirty:
            return False

    with _history_flush_lock:
        with _state_lock:
            with _history_dirty_lock:
                if not force and not _history_dirty:
                    return False
                snap_gen = _history_dirty_gen
            snapshot = [dict(m) if isinstance(m, dict) else m for m in _chat_messages[-100:]]

        serialized = json.dumps(snapshot, ensure_ascii=False, indent=2)
        try:
            _atomic_write_text(HISTORY_PATH, serialized)
        except Exception as exc:
            _log_rate_limited("flush_history", exc)
            return False

        with _history_dirty_lock:
            if _history_dirty_gen == snap_gen:
                _history_dirty = False
            _history_flushed_gen = max(_history_flushed_gen, snap_gen)
        return True


def _history_flusher_loop() -> None:
    while not _history_stop_event.wait(timeout=_HISTORY_FLUSH_INTERVAL):
        try:
            flush_history_if_dirty()
        except Exception as exc:
            _log_rate_limited("history_flusher_loop", exc)
    try:
        flush_history_if_dirty()
    except Exception as exc:
        _log_rate_limited("history_flusher_shutdown", exc)


def start_history_flusher() -> threading.Thread:
    """Idempotently start the background coalesced history flusher thread (~2s interval)."""
    global _history_flusher_thread
    with _history_dirty_lock:
        if _history_flusher_thread is not None and _history_flusher_thread.is_alive():
            return _history_flusher_thread
        _history_stop_event.clear()
        t = threading.Thread(target=_history_flusher_loop, name="console-history-flusher", daemon=True)
        t.start()
        _history_flusher_thread = t
        return t


def stop_history_flusher(timeout: float = 2.0) -> None:
    """Stop background history flusher cleanly and flush any pending dirty history."""
    global _history_flusher_thread
    _history_stop_event.set()
    t = _history_flusher_thread
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=timeout)
    flush_history_if_dirty()
    with _history_dirty_lock:
        if _history_flusher_thread is t:
            _history_flusher_thread = None


_RE_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
_RE_OTA_DEV = re.compile(r"OTA请求设备ID:\s*([0-9a-fA-F:]+)")
_RE_LISTEN_START = re.compile(r'收到listen消息：.*"state"\s*:\s*"start"')
_RE_USER_MSG = re.compile(r"大模型收到用户消息:\s*(.+)$")
_RE_AI_SENTENCE = re.compile(r"发送音频消息:\s*SentenceType\.(\w+),\s*(.+)$")


def _join_cn_clauses(prev: str, nxt: str) -> str:
    if not prev:
        return nxt
    if prev[-1] in "，。！？；：,.!?;:~～…":
        return prev + nxt
    return prev + "，" + nxt


def _is_duplicate_chat_message(
    role: str, ts_short: str, text: str, *, exclude_last: bool = False
) -> bool:
    pool = _chat_messages[:-1] if exclude_last else _chat_messages
    for m in pool:
        if (
            m.get("role") == role
            and m.get("time") == ts_short
            and m.get("text") == text
        ):
            return True
    return False


def _finalize_open_ai_turn() -> None:
    global _current_ai_turn_open
    if _current_ai_turn_open:
        if _chat_messages and _chat_messages[-1].get("role") == "assistant":
            last = _chat_messages[-1]
            if _is_duplicate_chat_message(
                "assistant",
                str(last.get("time") or ""),
                str(last.get("text") or ""),
                exclude_last=True,
            ):
                _chat_messages.pop()
        _current_ai_turn_open = False


def _trim_chat_messages() -> None:
    if len(_chat_messages) > 100:
        del _chat_messages[:-100]


_config_yaml_caches_lock = threading.Lock()
_config_yaml_caches: dict[str, xz_common.MtimeFileCache[dict]] = {}


def _parse_yaml_dict(raw: str) -> dict:
    parsed = yaml.safe_load(raw)
    return parsed if isinstance(parsed, dict) else {}


def _get_config_yaml_cache(path: str) -> xz_common.MtimeFileCache[dict]:
    norm = os.path.abspath(str(path))
    with _config_yaml_caches_lock:
        cache = _config_yaml_caches.get(norm)
        if cache is None:
            cache = xz_common.MtimeFileCache(
                parser=_parse_yaml_dict,
                default_factory=dict,
                retries=2,
            )
            _config_yaml_caches[norm] = cache
        return cache


def invalidate_config_yaml_cache(path: str | None = None) -> None:
    with _config_yaml_caches_lock:
        if path is None:
            for c in _config_yaml_caches.values():
                c.invalidate()
        else:
            c = _config_yaml_caches.get(os.path.abspath(str(path)))
            if c is not None:
                c.invalidate()


def _read_raw_yaml() -> dict:
    """Read .config.yaml via xz_common.MtimeFileCache so /api/state avoids reparsing unchanged YAML (D-4)."""
    return _get_config_yaml_cache(CONFIG_PATH).get(CONFIG_PATH, clone=copy.deepcopy)


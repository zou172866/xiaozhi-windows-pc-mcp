import asyncio
import atexit
import copy
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
import wave
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import edge_tts
import httpx
import yaml

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
HTML_PATH = os.path.join(APP_DIR, "console.html")
HISTORY_PATH = os.path.join(STATE_DIR, "chat_history.json")
LOG_CURSOR_PATH = os.path.join(STATE_DIR, ".console_log_cursor.json")
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

# A-4: Per-boot random write token injected into console.html and required via X-Console-Token on ALL POSTs
_CONSOLE_WRITE_TOKEN = secrets.token_urlsafe(32)
_CONSOLE_TOKEN_PLACEHOLDER = "__XIAOZHI_CONSOLE_TOKEN__"
_INTERNAL_READONLY_PATHS = frozenset({"/api/health", "/api/state"})


def get_console_token() -> str:
    return _CONSOLE_WRITE_TOKEN


GSV_PROFILES = {
    "__gsv_raiden_mei__": {
        "version": "v4",
        "gpt_weights": f"{HOST_GSV_WEIGHTS_DIR}/raiden_mei/v4/芽衣/芽衣-e10.ckpt",
        "sovits_weights": f"{HOST_GSV_WEIGHTS_DIR}/raiden_mei/v4/芽衣/芽衣_e10_s430_l32.pth",
        "block": {
            "type": "gpt_sovits_v2",
            "voice_id": "__gsv_raiden_mei__",
            "url": f"{GSV_Host}/tts",
            "output_dir": "tmp/",
            "text_lang": "zh",
            "ref_audio_path": "${HOST_VOICES_DIR}/raiden_mei/mei_default_ref.wav",
            "aux_ref_audio_paths": [],
            "prompt_text": "今夜月色很美，呵...适合与朋友小聚一番。",
            "prompt_lang": "zh",
            "top_k": 12,
            "top_p": 1,
            "temperature": 0.98,
            "text_split_method": "cut0",
            "batch_size": 1,
            "return_fragment": False,
            "speed_factor": 1.0,
            "streaming_mode": False,
            "seed": -1,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
            "sample_steps": 16,
        },
    },
    "__gsv_xiaozhi_tw__": {
        "version": "v2ProPlus",
        "gpt_weights": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
        "sovits_weights": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth",
        "block": {
            "type": "gpt_sovits_v2",
            "voice_id": "__gsv_xiaozhi_tw__",
            "url": f"{GSV_Host}/tts",
            "output_dir": "tmp/",
            "text_lang": "zh",
            "ref_audio_path": "${HOST_VOICES_DIR}/xiaozhi_taiwan_ref.wav",
            "aux_ref_audio_paths": ["${HOST_VOICES_DIR}/xiaozhi_taiwan_aux.wav"],
            "prompt_text": "哈啰啊，我是小智啦，声音好听的台湾女孩一枚，超开心认识你耶，最近在忙啥？",
            "prompt_lang": "zh",
            "top_k": 12,
            "top_p": 1,
            "temperature": 0.98,
            "text_split_method": "cut0",
            "batch_size": 1,
            "return_fragment": False,
            "speed_factor": 1.04,
            "streaming_mode": False,
            "seed": -1,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
        },
    },
}

ALLOWED_GSV_VOICES = frozenset(GSV_PROFILES.keys())
ALLOWED_EDGE_VOICES = frozenset(
    {
        "zh-TW-HsiaoChenNeural",
        "zh-TW-YunJheNeural",
        "zh-CN-XiaoxiaoNeural",
        "zh-CN-XiaoyiNeural",
        "zh-CN-YunxiNeural",
        "zh-CN-YunjianNeural",
        "zh-CN-YunxiaNeural",
        "zh-HK-HiuGaaiNeural",
        "zh-CN-liaoning-XiaobeiNeural",
        "zh-CN-shaanxi-XiaoniNeural",
    }
)
ALLOWED_VOICES = ALLOWED_GSV_VOICES | ALLOWED_EDGE_VOICES
# Share the exact same RLock with live ESP32 GPT-SoVITS synthesis (P1-14)
_gsv_weights_lock = mei_emotion_router.GSV_GPU_LOCK
_active_gsv_loaded = None
_gsv_op_deadline_tls = threading.local()
_GSV_PREVIEW_LOCK_MAX_SECONDS = 45.0
EDGE_PREVIEW_TIMEOUT_SECONDS = 15.0
_voice_dirty_lock = threading.Lock()
_voice_dirty_target: str | None = None
_voice_dirty_error: str = ""


def _set_voice_dirty(target_voice: str | None, error_msg: str = "") -> None:
    global _voice_dirty_target, _voice_dirty_error
    with _voice_dirty_lock:
        _voice_dirty_target = target_voice
        _voice_dirty_error = _redact_secrets(error_msg) if error_msg else ""


def _get_voice_dirty_state() -> tuple[str | None, str]:
    with _voice_dirty_lock:
        return _voice_dirty_target, _voice_dirty_error


def _gsv_timeout_within_budget(default_timeout: float) -> float:
    deadline = getattr(_gsv_op_deadline_tls, "deadline", None)
    if deadline is None:
        return float(default_timeout)
    rem = float(deadline) - time.monotonic()
    if rem <= 0:
        raise TimeoutError("GPU 锁持有时间已达到上限，中止试听以保护线上语音播报")
    return min(float(default_timeout), rem)


def _detect_boot_gsv_voice() -> str | None:
    if os.path.exists(GSV_INFER_YAML):
        try:
            with open(GSV_INFER_YAML, "r", encoding="utf-8") as rf:
                boot_cfg = yaml.safe_load(rf) or {}
            t2s = str((boot_cfg.get("custom") or {}).get("t2s_weights_path") or "")
            for vid, prof in GSV_PROFILES.items():
                if prof["gpt_weights"] == t2s:
                    return vid
        except Exception as exc:
            _log_rate_limited("detect_boot_gsv_voice", exc)
    return None


def _resolve_gsv_voice_id(tts_cfg: dict | None) -> str:
    """Resolve GSV voice ID from explicit persistent `voice_id` first, falling back to legacy ref_audio_path (D-7)."""
    cfg_d = tts_cfg if isinstance(tts_cfg, dict) else {}
    explicit = str(cfg_d.get("voice_id") or "").strip()
    if explicit in GSV_PROFILES:
        return explicit
    # Legacy fallback ONLY for older configs lacking explicit voice_id
    ref_p = str(cfg_d.get("ref_audio_path") or "")
    return "__gsv_raiden_mei__" if "raiden_mei" in ref_p else "__gsv_xiaozhi_tw__"


def _get_configured_gsv_voice() -> str | None:
    try:
        cfg = _read_raw_yaml()
        selected = cfg.get("selected_module") or {}
        if selected.get("TTS") != "GPT_SOVITS_V2":
            return None
        return _get_current_voice_id(cfg)
    except Exception as exc:
        _log_rate_limited("get_configured_gsv_voice", exc)
        return None


_SECRET_PATTERNS = (
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/=]{6,}", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Basic\s+)[A-Za-z0-9+/=]{8,}", re.IGNORECASE), r"\1***"),
    (
        re.compile(
            r"([?#&](?:access_token|refresh_token|auth_token|token|api_key|apikey|key)=)[^\s&#\"',;)}\]]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (
        re.compile(
            r"((?:x-goog-api-key|x-api-key|x-console-token|api_key|apikey|access_token|refresh_token|auth_token)\s*[\"']?\s*[:=]\s*[\"']?)[^\s&\"',;)}\]]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (
        re.compile(
            r"(authorization\s*[\"']?\s*[:=]\s*[\"']?(?:(?:Bearer|Basic)\s+)?)[^\s&\"',;)}\]]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "sk-***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{16,}\b"), "AIza***"),
)

_ERROR_PAYLOAD_KEYS = frozenset(
    {
        "error",
        "detail",
        "message",
        "rollback_warning",
        "voice_dirty_error",
        "last_error",
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


def validate_console_request(
    method: str,
    headers: dict,
    port: int = CONSOLE_PORT,
    path: str = "",
    expected_token: str | None = None,
) -> tuple[bool, int, str]:
    """Validate Host, internal-host read-only scope, browser CSRF guards, and per-boot X-Console-Token (A-4)."""
    norm_headers = {str(k).lower(): str(v).strip() for k, v in (headers or {}).items()}
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
    allowed_origins = {
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
        f"http://[::1]:{port}",
    }

    host = norm_headers.get("host", "").lower()
    is_internal = host in internal_hosts
    if not host or (host not in local_hosts and not is_internal):
        return False, int(HTTPStatus.MISDIRECTED_REQUEST), "Invalid Host header"

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

    if meth == "POST":
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


def _ensure_gsv_weights(voice_id: str, persist_boot_yaml: bool = False):
    global _active_gsv_loaded
    prof = GSV_PROFILES.get(voice_id)
    if not prof:
        return
    with _gsv_weights_lock:
        if _active_gsv_loaded is None:
            _active_gsv_loaded = _detect_boot_gsv_voice()
        if _active_gsv_loaded != voice_id:
            client = _get_console_http_client()
            t_gpt = _gsv_timeout_within_budget(25.0)
            r1 = client.get(f"{GSV_Host}/set_gpt_weights", params={"weights_path": prof["gpt_weights"]}, timeout=t_gpt)
            if r1.status_code != 200:
                raise RuntimeError(f"切换 GPT 权重失败 (HTTP {r1.status_code}): {r1.text[:160]}")
            # GPT weights are now mutated on GPU: mark state dirty (distinct from None) so a SoVITS
            # failure forces restoration instead of falling back to _detect_boot_gsv_voice().
            _active_gsv_loaded = "__dirty__"
            t_sov = _gsv_timeout_within_budget(30.0)
            r2 = client.get(f"{GSV_Host}/set_sovits_weights", params={"weights_path": prof["sovits_weights"]}, timeout=t_sov)
            if r2.status_code != 200:
                raise RuntimeError(f"切换 SoVITS 权重失败 (HTTP {r2.status_code}): {r2.text[:160]}")
            _active_gsv_loaded = voice_id
        if persist_boot_yaml:
            boot_cfg = {}
            if os.path.exists(GSV_INFER_YAML):
                try:
                    with open(GSV_INFER_YAML, "r", encoding="utf-8") as rf:
                        boot_cfg = yaml.safe_load(rf) or {}
                except Exception as exc:
                    _log_rate_limited("read_gsv_infer_yaml", exc)
                    boot_cfg = {}
            boot_cfg["custom"] = {
                "bert_base_path": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
                "cnhuhbert_base_path": "GPT_SoVITS/pretrained_models/chinese-hubert-base",
                "device": "cuda",
                "is_half": True,
                "t2s_weights_path": prof["gpt_weights"],
                "version": prof["version"],
                "vits_weights_path": prof["sovits_weights"],
            }
            _atomic_write_text(GSV_INFER_YAML, yaml.safe_dump(boot_cfg, allow_unicode=True, sort_keys=False))


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


_pc_mode_lock = threading.Lock()
_pc_mode_cache: str = "safe"
_pc_mode_online_cache: bool = False


def _set_cached_pc_mode(mode: str, online: bool = True) -> None:
    global _pc_mode_cache, _pc_mode_online_cache
    m = str(mode or "safe").strip().lower()
    if m not in ("off", "safe", "full"):
        m = "safe"
    with _pc_mode_lock:
        _pc_mode_cache = m
        _pc_mode_online_cache = bool(online)


def _get_cached_pc_mode_online() -> bool:
    with _pc_mode_lock:
        return bool(_pc_mode_online_cache)


def _read_pc_mode() -> str:
    """Read PC control mode: cached host-18900 authority by default (A-2), or explicit test MODE_PATH."""
    if MODE_PATH == _DEFAULT_MODE_PATH:
        with _pc_mode_lock:
            return _pc_mode_cache
    try:
        if os.path.exists(MODE_PATH):
            with open(MODE_PATH, "r", encoding="utf-8") as f:
                m = json.load(f).get("mode", "safe")
                if m in ("off", "safe", "full"):
                    return m
    except Exception as exc:
        _log_rate_limited("read_pc_mode", exc)
    return "safe"


def _write_pc_mode(mode: str) -> dict:
    """Switch PC control mode via authenticated host 18900 POST /mode (A-2; fail-closed if 18900 unreachable)."""
    m = str(mode or "safe").strip().lower()
    if m not in ("off", "safe", "full"):
        raise ValueError("无效的权限挡位")
    if MODE_PATH != _DEFAULT_MODE_PATH:
        xz_common.atomic_write_json(MODE_PATH, {"mode": m})
        return {"ok": True, "mode": m, "source": "local_test"}

    client = _get_console_http_client()
    resp = client.post(
        f"{PC_MCP_HOST}/mode",
        headers=pc_audio_router.get_pc_mcp_auth_headers(),
        json={"mode": m},
        timeout=3.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"宿主机 18900 MCP 拒绝切换挡位 (HTTP {resp.status_code}): {_redact_secrets(resp.text[:160])}"
        )
    data = resp.json() if resp.content else {}
    confirmed = str(data.get("mode") or m).strip().lower()
    if confirmed not in ("off", "safe", "full"):
        confirmed = m
    _set_cached_pc_mode(confirmed, online=True)
    return {"ok": True, "mode": confirmed, "source": "host_18900"}


def _update_idle_farewell_config(payload: dict) -> tuple[bool, dict | str]:
    """Validate, persist, and live-sync idle timeout farewell settings across active connections."""
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象"

    enabled_arg: bool | None = None
    if "enabled" in payload:
        raw_en = payload.get("enabled")
        if not isinstance(raw_en, bool):
            return False, "字段 enabled 必须为布尔值"
        enabled_arg = raw_en

    mode_arg: str | None = None
    if "mode" in payload:
        raw_mode = payload.get("mode")
        if not isinstance(raw_mode, str) or raw_mode.strip().lower() not in runtime_settings.VALID_IDLE_FAREWELL_MODES:
            return False, "字段 mode 必须为 fixed 或 llm_summary"
        mode_arg = raw_mode.strip().lower()

    timeout_arg: int | None = None
    if "timeout_seconds" in payload:
        raw_to = payload.get("timeout_seconds")
        if isinstance(raw_to, bool):
            return False, "字段 timeout_seconds 必须为整数（30～1800）"
        try:
            timeout_arg = int(raw_to)
        except (TypeError, ValueError):
            return False, "字段 timeout_seconds 必须为整数（30～1800）"
        if timeout_arg < 30 or timeout_arg > 1800:
            return False, "无声超时秒数必须在 30～1800 秒之间"

    summary_prompt_arg: str | None = None
    if "summary_prompt" in payload:
        raw_sp = payload.get("summary_prompt")
        if not isinstance(raw_sp, str):
            return False, "字段 summary_prompt 必须为字符串"
        summary_prompt_arg = raw_sp

    messages_arg = None
    if "messages" in payload:
        raw_msgs = payload.get("messages")
        if not isinstance(raw_msgs, (list, tuple, str)):
            return False, "字段 messages 必须为字符串数组或换行分隔文本"
        messages_arg = raw_msgs

    if (
        enabled_arg is None
        and mode_arg is None
        and timeout_arg is None
        and summary_prompt_arg is None
        and messages_arg is None
    ):
        return False, "请至少提供 enabled、mode、timeout_seconds、summary_prompt 或 messages"

    saved = runtime_settings.set_idle_farewell(
        enabled=enabled_arg,
        mode=mode_arg,
        timeout_seconds=timeout_arg,
        summary_prompt=summary_prompt_arg,
        messages=messages_arg,
    )
    eff_timeout = int(saved["timeout_seconds"])
    eff_hard = max(60, eff_timeout + 60)
    try:
        for srv in runtime_events.get_ws_servers():
            if isinstance(getattr(srv, "config", None), dict):
                srv.config["close_connection_no_voice_time"] = eff_timeout
        for conn in runtime_events.get_active_connections():
            if hasattr(conn, "timeout_seconds"):
                conn.timeout_seconds = eff_hard
            if isinstance(getattr(conn, "config", None), dict):
                conn.config["close_connection_no_voice_time"] = eff_timeout
    except Exception as exc:
        _log_rate_limited("idle_farewell_live_sync", exc)
    return True, saved


def _check_agy_bridge_online(l3_status: dict | None = None) -> bool:
    """Derive Antigravity sidecar heartbeat solely from host-private 18900 /l3/status (never ./data)."""
    src = l3_status
    if src is None:
        with _l3_cache_lock:
            src = dict(_l3_cache) if _l3_cache is not None else None
    if not isinstance(src, dict):
        return False
    detected = src.get("detected")
    if isinstance(detected, dict):
        agy = detected.get("antigravity")
        if isinstance(agy, dict):
            return bool(agy.get("ready"))
    return False


_l3_cache_lock = threading.Lock()
_l3_cache: dict | None = None
_l3_cache_ts = 0.0
_L3_CACHE_TTL = 3.0


def _read_local_l3_fallback() -> dict:
    """Read-only offline fallback when 18900 is unreachable (never reads or writes ./data L3 files)."""
    b = (os.environ.get("L3_BACKEND") or "dsh").strip().lower()
    if b not in VALID_L3_BACKENDS:
        b = "dsh"
    labels = {
        "antigravity": "Google Antigravity（反重力）",
        "dsh": "DeepSeek DSH（DeepSeek Harness）",
        "workbuddy": "腾讯 WorkBuddy / CodeBuddy",
        "none": "关闭 L3 后台派单",
    }
    return {
        "ok": False,
        "mcp_online": False,
        "backend": b,
        "label_zh": labels.get(b, b),
        "ready": b == "none",
        "detail": (
            "宿主机 18900 MCP 离线（L3 配置仅存宿主机私有目录 pc-mcp/state/）"
            if b != "none"
            else "已关闭 L3 后台 Agent（仅保留 L1/L2）"
        ),
        "workdir": "",
        "effective_workdir": "",
        "dsh_cmd": "",
        "workbuddy_cmd": "",
        "workbuddy_auto_approve": False,
        "timeout_seconds": 300,
        "writable_fields": ["backend", "timeout_seconds"],
        "host_env_only_fields": ["workdir", "dsh_cmd", "workbuddy_cmd", "workbuddy_auto_approve"],
        "detected": {
            "antigravity": {"ready": False, "detail": "待宿主机 18900 MCP 在线探测"},
            "dsh": {"ready": False, "detail": "待宿主机 18900 MCP 在线探测"},
            "workbuddy": {"ready": False, "detail": "待宿主机 18900 MCP 在线探测"},
            "none": {"ready": True, "detail": "已关闭 L3 后台派单"},
        },
    }


def _fetch_l3_backend_status(force: bool = False) -> dict:
    global _l3_cache, _l3_cache_ts
    now = time.monotonic()
    with _l3_cache_lock:
        if not force and _l3_cache is not None and (now - _l3_cache_ts) < _L3_CACHE_TTL:
            return dict(_l3_cache)
    try:
        client = _get_console_http_client()
        resp = client.get(
            f"{PC_MCP_HOST}/l3/status",
            headers=pc_audio_router.get_pc_mcp_auth_headers(),
            timeout=1.6,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["mcp_online"] = True
            with _l3_cache_lock:
                _l3_cache = dict(data)
                _l3_cache_ts = time.monotonic()
            return data
    except Exception as exc:
        _log_rate_limited("fetch_l3_backend_status", exc)
    fb = _read_local_l3_fallback()
    with _l3_cache_lock:
        _l3_cache = dict(fb)
        _l3_cache_ts = time.monotonic()
    return fb


def _sanitize_l3_update_payload(payload: dict) -> tuple[bool, dict | str]:
    """Allow ONLY backend and timeout_seconds (or alias timeout); strictly reject host-env-only fields."""
    if not isinstance(payload, dict):
        return False, "请求体必须是 JSON 对象"
    forbidden = sorted(str(k) for k in payload.keys() if k not in ALLOWED_L3_UPDATE_KEYS)
    if forbidden:
        return (
            False,
            f"仅允许修改 backend / timeout_seconds，宿主机命令与目录仅可从宿主机 .env 配置，已拒绝字段: {', '.join(forbidden)}",
        )

    out: dict = {}
    if "backend" in payload:
        b = str(payload.get("backend") or "").strip().lower()
        if b not in VALID_L3_BACKENDS:
            return False, "无效的 L3 后端名称（可选: antigravity / dsh / workbuddy / none）"
        out["backend"] = b

    has_ts = "timeout_seconds" in payload
    has_t = "timeout" in payload
    if has_ts or has_t:
        parsed_vals = []
        for k in ("timeout_seconds", "timeout"):
            if k in payload:
                raw_v = payload[k]
                if isinstance(raw_v, bool):
                    return False, f"字段 {k} 必须为整数（30～1800）"
                try:
                    iv = int(raw_v)
                except (TypeError, ValueError):
                    return False, f"字段 {k} 必须为整数（30～1800）"
                parsed_vals.append(max(30, min(1800, iv)))
        if len(parsed_vals) == 2 and parsed_vals[0] != parsed_vals[1]:
            return False, "timeout_seconds 与 timeout 参数冲突"
        out["timeout_seconds"] = parsed_vals[0]

    if not out:
        return False, "请至少提供 backend 或 timeout_seconds"
    return True, out


def _update_l3_backend_config(payload: dict) -> tuple[bool, dict | str]:
    global _l3_cache, _l3_cache_ts
    ok_clean, clean_or_err = _sanitize_l3_update_payload(payload)
    if not ok_clean:
        return False, str(clean_or_err)
    forward_payload = clean_or_err
    try:
        client = _get_console_http_client()
        resp = client.post(
            f"{PC_MCP_HOST}/l3/config",
            headers=pc_audio_router.get_pc_mcp_auth_headers(),
            json=forward_payload,
            timeout=3.5,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["mcp_online"] = True
            with _l3_cache_lock:
                _l3_cache = dict(data)
                _l3_cache_ts = time.monotonic()
            return True, data
        return False, f"宿主机 MCP 返回 HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as exc:
        return False, f"宿主机 18900 MCP 离线，无法更新 L3 配置（L3 配置仅存宿主机私有目录 pc-mcp/state/）: {exc}"


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


_LOG_MAX_READ_BYTES = 256 * 1024
_LOG_CURSOR_WRITE_INTERVAL = 5.0
_active_watcher_state_lock = threading.Lock()
_active_watcher_state: dict | None = None


def _load_log_cursor() -> tuple[int | None, int]:
    try:
        if os.path.isfile(LOG_CURSOR_PATH):
            with open(LOG_CURSOR_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                ino = data.get("inode")
                off = int(data.get("offset", 0))
                if ino is not None and off >= 0:
                    return int(ino), off
    except Exception as exc:
        _log_rate_limited("load_log_cursor", exc)
    return None, 0


def _save_log_cursor(inode: int, offset: int) -> None:
    try:
        _atomic_write_text(
            LOG_CURSOR_PATH,
            json.dumps({"inode": int(inode), "offset": int(offset)}),
        )
    except Exception as exc:
        _log_rate_limited("save_log_cursor", exc)


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
    _save_log_cursor(int(ino), int(_log_offset))
    st["last_cursor_save_ts"] = time.monotonic()
    st["cursor_dirty"] = False
    return True


def _process_log_line(line: str, *, status_only: bool = False):
    global _current_ai_turn_open
    s = line.strip()
    if not s:
        return

    with _state_lock:
        _append_raw_log_locked(s)

        m_time = _RE_TIME.match(s)
        ts_short = m_time.group(1)[11:19] if m_time else time.strftime("%H:%M:%S")

        if "ASR模块初始化完成" in s or "Websocket地址是" in s:
            _device_info["server_ready"] = True

        m_dev = _RE_OTA_DEV.search(s)
        if m_dev:
            _device_info["device_id"] = m_dev.group(1)
            _device_info["last_seen"] = ts_short

        if status_only:
            return

        if _RE_LISTEN_START.search(s):
            _finalize_open_ai_turn()
            _device_info["activity"] = "listening"
            _device_info["last_seen"] = ts_short
            return

        m_user = _RE_USER_MSG.search(s)
        if m_user:
            _finalize_open_ai_turn()
            text = m_user.group(1).strip()
            _device_info["activity"] = "speaking"
            _device_info["last_seen"] = ts_short
            if not _is_duplicate_chat_message("user", ts_short, text):
                _chat_messages.append({"role": "user", "text": text, "time": ts_short})
                _save_history()
            return

        m_ai = _RE_AI_SENTENCE.search(s)
        if m_ai:
            stype, raw_content = m_ai.group(1), m_ai.group(2).strip()
            content = (
                "None"
                if raw_content == "None"
                else mei_emotion_router.strip_emotion_tags(raw_content).strip()
            )
            _device_info["last_seen"] = ts_short
            if stype == "LAST" or content == "None":
                _finalize_open_ai_turn()
                _device_info["activity"] = "idle"
                _save_history()
            elif content:
                _device_info["activity"] = "speaking"
                if _current_ai_turn_open and _chat_messages and _chat_messages[-1]["role"] == "assistant":
                    _chat_messages[-1]["text"] = _join_cn_clauses(_chat_messages[-1]["text"], content)
                else:
                    _chat_messages.append({"role": "assistant", "text": content, "time": ts_short})
                    _current_ai_turn_open = True
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
    global _current_ai_turn_open
    replayed_msgs: list[dict] = []
    open_ai = False
    last_activity: str | None = None
    last_seen: str | None = None

    for line in lines:
        s = line.strip()
        if not s:
            continue
        _process_log_line(s, status_only=True)

        m_time = _RE_TIME.match(s)
        ts_short = m_time.group(1)[11:19] if m_time else time.strftime("%H:%M:%S")

        if _RE_LISTEN_START.search(s):
            open_ai = False
            last_activity = "listening"
            last_seen = ts_short
            continue

        m_user = _RE_USER_MSG.search(s)
        if m_user:
            open_ai = False
            text = m_user.group(1).strip()
            last_activity = "speaking"
            last_seen = ts_short
            replayed_msgs.append({"role": "user", "text": text, "time": ts_short})
            continue

        m_ai = _RE_AI_SENTENCE.search(s)
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
                    replayed_msgs[-1]["text"] = _join_cn_clauses(replayed_msgs[-1]["text"], content)
                else:
                    replayed_msgs.append({"role": "assistant", "text": content, "time": ts_short})
                    open_ai = True

    with _state_lock:
        if last_activity is not None:
            _device_info["activity"] = last_activity
        if last_seen is not None:
            _device_info["last_seen"] = last_seen

        # Cap replayed window to newest 100 turns so aged log lines already rotated out of
        # chat_history.json are never appended out of order behind newer persisted turns.
        recent_replayed = replayed_msgs[-100:]
        existing_keys = {
            (str(m.get("role") or ""), str(m.get("time") or ""), str(m.get("text") or ""))
            for m in _chat_messages
        }
        changed = False
        last_added = False
        for idx, item in enumerate(recent_replayed):
            k = (item["role"], item["time"], item["text"])
            if k in existing_keys:
                last_added = False
                continue
            existing_keys.add(k)
            _chat_messages.append(item)
            changed = True
            last_added = idx == len(recent_replayed) - 1
        _current_ai_turn_open = bool(open_ai and last_added)
        if changed:
            _trim_chat_messages()
            _save_history()


def _poll_log_once(watcher_state: dict, *, raw_logs_only: bool = False) -> None:
    global _log_offset
    if not os.path.exists(LOG_PATH):
        return
    st = os.stat(LOG_PATH)
    is_boot_replay = False
    if not watcher_state.get("initialized"):
        watcher_state["initialized"] = True
        saved_ino = watcher_state.get("saved_inode")
        saved_off = int(watcher_state.get("saved_offset") or 0)
        if raw_logs_only:
            # Structured runtime event bus owns chat & device status; tail recent log lines only for log drawer
            tail_bytes = 65536
            _log_offset = max(0, st.st_size - tail_bytes)
            if _log_offset > 0:
                try:
                    with open(LOG_PATH, "rb") as _rf:
                        _rf.seek(_log_offset)
                        _first_chunk = _rf.read(4096)
                    _nl = _first_chunk.find(b"\n")
                    if _nl >= 0:
                        _log_offset += _nl + 1
                except Exception as exc:
                    _log_rate_limited("poll_log_tail_align", exc)
            watcher_state["last_inode"] = st.st_ino
        elif saved_ino == st.st_ino and 0 < saved_off <= st.st_size:
            _stream_status_prefix(LOG_PATH, saved_off)
            _log_offset = saved_off
            watcher_state["last_inode"] = st.st_ino
        else:
            _log_offset = 0
            watcher_state["last_inode"] = st.st_ino
            is_boot_replay = True
    elif watcher_state.get("last_inode") != st.st_ino or st.st_size < _log_offset:
        _log_offset = 0
        watcher_state["last_inode"] = st.st_ino

    consumed_any = False
    if st.st_size > _log_offset:
        with open(LOG_PATH, "rb") as f:
            f.seek(_log_offset)
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
            _log_offset += len(to_consume)
            consumed_any = True
            lines = to_consume.decode("utf-8", errors="replace").splitlines()
            if raw_logs_only:
                with _state_lock:
                    for ln in lines:
                        s = ln.strip()
                        if s:
                            _append_raw_log_locked(s)
            elif is_boot_replay and _chat_messages:
                _replay_boot_lines_merged(lines)
            else:
                for ln in lines:
                    _process_log_line(ln, status_only=False)
                with _state_lock:
                    _trim_chat_messages()
            watcher_state["cursor_dirty"] = True
            watcher_state["cursor_inode"] = st.st_ino

    # D-4: Write log cursor to disk at most once every 5s (first write on a fresh watcher flushes immediately)
    if watcher_state.get("cursor_dirty"):
        now_mono = time.monotonic()
        last_save = float(watcher_state.get("last_cursor_save_ts") or 0.0)
        if last_save <= 0.0 or (now_mono - last_save) >= _LOG_CURSOR_WRITE_INTERVAL:
            _save_log_cursor(int(watcher_state.get("cursor_inode") or st.st_ino), _log_offset)
            watcher_state["last_cursor_save_ts"] = now_mono
            watcher_state["cursor_dirty"] = False

    # Legacy non-hooks log-replay path (runs in background watcher thread, never in sentence_start):
    # coalesce the full batch into at most one disk flush after processing lines.
    if consumed_any and not raw_logs_only:
        flush_history_if_dirty()


def _log_watcher_loop():
    global _active_watcher_state
    _load_history()
    _sync_runtime_events()
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
            _sync_runtime_events()
            _poll_log_once(watcher_state, raw_logs_only=runtime_events.is_hooks_active())
        except Exception as exc:
            _log_rate_limited("log_watcher_loop", exc)
        time.sleep(0.4)


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


ANIMA_HOST = "http://anima-brain:18787"
ANIMA_TOKEN_PATH = xz_common.ANIMA_TOKEN_PATH
_anima_rel_lock = threading.Lock()
_anima_rel_cache: dict | None = None
_anima_rel_ts = 0.0
_ANIMA_REL_TTL = 3.0


def _read_anima_token() -> str:
    return xz_common.get_anima_token(ANIMA_TOKEN_PATH)


def _get_anima_character_id(cfg: dict | None = None) -> str:
    try:
        c = cfg if cfg is not None else _read_raw_yaml()
        cid = str(((c.get("Memory") or {}).get("AnimaBrain") or {}).get("character_id") or "").strip()
        if cid:
            return cid
    except Exception as exc:
        _log_rate_limited("get_anima_character_id", exc)
    return "xiaozhi"


def _default_offline_relationship(char_id: str = "xiaozhi") -> dict:
    return {
        "online": False,
        "character_id": char_id,
        "character_name": "小智",
        "relationship_enabled": True,
        "romance_enabled": True,
        "preset": "custom",
        "stage": "familiar",
        "stage_label_zh": "等待连接",
        "axes": {
            "familiarity": 0.62,
            "warmth": 0.66,
            "trust": 0.62,
            "romance": 0.20,
            "tension": 0.0,
        },
        "bands": {"familiarity": 2, "warmth": 2, "trust": 2, "romance": 0, "tension": 0},
        "guidance_zh": "",
    }


def _fetch_anima_relationship(force: bool = False, cfg: dict | None = None) -> dict:
    global _anima_rel_cache, _anima_rel_ts
    now = time.monotonic()
    with _anima_rel_lock:
        if not force and _anima_rel_cache is not None and (now - _anima_rel_ts) < _ANIMA_REL_TTL:
            return dict(_anima_rel_cache)

    char_id = _get_anima_character_id(cfg)
    token = _read_anima_token()
    if not token:
        return _default_offline_relationship(char_id)
    try:
        client = _get_console_http_client()
        resp = client.get(
            f"{ANIMA_HOST}/xiaozhi/relationship",
            params={"character_id": char_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=1.8,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["online"] = True
            with _anima_rel_lock:
                _anima_rel_cache = dict(data)
                _anima_rel_ts = time.monotonic()
            return data
    except Exception as exc:
        _log_rate_limited("fetch_anima_relationship", exc)
    with _anima_rel_lock:
        if _anima_rel_cache is not None:
            stale = dict(_anima_rel_cache)
            stale["online"] = False
            _anima_rel_ts = time.monotonic()
            return stale
    return _default_offline_relationship(char_id)


def _update_anima_relationship(payload: dict) -> tuple[bool, dict | str]:
    global _anima_rel_cache, _anima_rel_ts
    char_id = _get_anima_character_id()
    token = _read_anima_token()
    if not token:
        return False, "未找到 Anima Brain 访问令牌 (studio_token.txt)"
    req_body = dict(payload or {})
    req_body.setdefault("character_id", char_id)
    try:
        client = _get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/relationship",
            headers={"Authorization": f"Bearer {token}"},
            json=req_body,
            timeout=4.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["online"] = True
            with _anima_rel_lock:
                _anima_rel_cache = dict(data)
                _anima_rel_ts = time.monotonic()
            return True, data
        return False, f"Anima Brain 返回 HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as exc:
        return False, f"无法连接 Anima Brain (18787): {exc}"


_obsidian_lock = threading.Lock()
_obsidian_cache: dict | None = None
_obsidian_ts = 0.0
_OBSIDIAN_TTL = 3.0


def _default_offline_obsidian() -> dict:
    return {
        "online": False,
        "enabled": False,
        "writable": False,
        "export_dir": "/obsidian",
        "host_export_dir": "",
        "snapshot_interval_seconds": 600,
        "last_export_at": "",
        "last_export_ts": 0.0,
        "last_export_reason": "",
        "last_error": "无法连接 Anima Brain (18787)",
        "stats": {
            "turns": 0,
            "days": 0,
            "active_facts": 0,
            "historical_revisions": 0,
            "open_loops": 0,
            "episodes": 0,
            "files_written": 0,
            "files_skipped": 0,
        },
    }


def _mark_obsidian_cache_offline(err_msg: str = "") -> dict:
    global _obsidian_cache, _obsidian_ts
    with _obsidian_lock:
        if _obsidian_cache is not None:
            offline = dict(_obsidian_cache)
            offline["online"] = False
            if err_msg:
                offline["last_error"] = err_msg
        else:
            offline = _default_offline_obsidian()
            if err_msg:
                offline["last_error"] = err_msg
        _obsidian_cache = dict(offline)
        _obsidian_ts = time.monotonic()
        return offline


def _get_cached_obsidian_snapshot() -> dict:
    with _obsidian_lock:
        if _obsidian_cache is not None:
            return dict(_obsidian_cache)
    return _default_offline_obsidian()


def _fetch_obsidian_status(force: bool = False) -> dict:
    global _obsidian_cache, _obsidian_ts
    now = time.monotonic()
    with _obsidian_lock:
        if not force and _obsidian_cache is not None and (now - _obsidian_ts) < _OBSIDIAN_TTL:
            return dict(_obsidian_cache)

    token = _read_anima_token()
    if not token:
        return _mark_obsidian_cache_offline("未找到 Anima Brain 访问令牌 (studio_token.txt)")
    try:
        client = _get_console_http_client()
        resp = client.get(
            f"{ANIMA_HOST}/xiaozhi/obsidian/status",
            headers={"Authorization": f"Bearer {token}"},
            timeout=1.8,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["online"] = True
            with _obsidian_lock:
                _obsidian_cache = dict(data)
                _obsidian_ts = time.monotonic()
            return data
    except Exception as exc:
        _log_rate_limited("fetch_obsidian_status", exc)
    return _mark_obsidian_cache_offline()


def _update_obsidian_config(payload: dict) -> tuple[bool, dict | str]:
    global _obsidian_cache, _obsidian_ts
    token = _read_anima_token()
    if not token:
        msg = "未找到 Anima Brain 访问令牌 (studio_token.txt)"
        _mark_obsidian_cache_offline(msg)
        return False, msg
    if "enabled" not in (payload or {}) or not isinstance(payload.get("enabled"), bool):
        return False, "缺少布尔字段 enabled"
    try:
        client = _get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/obsidian/config",
            headers={"Authorization": f"Bearer {token}"},
            json={"enabled": bool(payload["enabled"])},
            timeout=15.0,
        )
        if resp.status_code == 200:
            raw = resp.json()
            data = dict(raw.get("obsidian") or raw)
            data["online"] = True
            with _obsidian_lock:
                _obsidian_cache = dict(data)
                _obsidian_ts = time.monotonic()
            return True, data
        return False, f"Anima Brain 返回 HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as exc:
        msg = f"无法连接 Anima Brain (18787): {exc}"
        _mark_obsidian_cache_offline(msg)
        return False, msg


def _trigger_obsidian_rebuild() -> tuple[bool, dict | str]:
    global _obsidian_cache, _obsidian_ts
    token = _read_anima_token()
    if not token:
        msg = "未找到 Anima Brain 访问令牌 (studio_token.txt)"
        _mark_obsidian_cache_offline(msg)
        return False, msg
    try:
        client = _get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/obsidian/rebuild",
            headers={"Authorization": f"Bearer {token}"},
            json={},
            timeout=30.0,
        )
        raw = {}
        try:
            raw = resp.json()
        except Exception as exc:
            _log_rate_limited("obsidian_rebuild_json", exc)
            raw = {}
        if resp.status_code == 200 and raw.get("ok"):
            status_data = dict(raw.get("obsidian") or {})
            status_data["online"] = True
            with _obsidian_lock:
                _obsidian_cache = dict(status_data)
                _obsidian_ts = time.monotonic()
            return True, {"obsidian": status_data, "result": raw.get("result") or raw}
        err = raw.get("error") or f"Anima Brain 返回 HTTP {resp.status_code}: {resp.text[:160]}"
        if isinstance(raw.get("obsidian"), dict):
            status_data = dict(raw["obsidian"])
            status_data["online"] = True
            with _obsidian_lock:
                _obsidian_cache = dict(status_data)
                _obsidian_ts = time.monotonic()
        return False, str(err)
    except Exception as exc:
        msg = f"无法连接 Anima Brain (18787): {exc}"
        _mark_obsidian_cache_offline(msg)
        return False, msg


_probe_lock = threading.Lock()
_pc_audio_online_cache: bool = False
_gsv_online_cache: bool = False
_anima_backup_lock = threading.Lock()
_anima_backup_cache: dict | None = None
_probe_fail_counts: dict[str, int] = {
    "pc_audio": 0,
    "l3": 0,
    "anima_rel": 0,
    "obsidian": 0,
    "gsv": 0,
}


def _get_cached_anima_backup_snapshot() -> dict:
    with _anima_backup_lock:
        if _anima_backup_cache is not None:
            snap = dict(_anima_backup_cache)
            snap["probed"] = True
            return snap
    return {
        "probed": False,
        "enabled": True,
        "last_backup_at": "",
        "last_backup_ts": 0.0,
        "last_backup_file": "",
        "integrity_ok": None,
        "daily_count": 0,
        "weekly_count": 0,
        "last_error": "",
    }
_PROBE_OFFLINE_THRESHOLD = 2
_PROBE_INTERVAL_SECONDS = 3.0


def _get_cached_pc_audio_online() -> bool:
    with _probe_lock:
        return bool(_pc_audio_online_cache)


def _set_cached_pc_audio_online(val: bool) -> None:
    with _probe_lock:
        global _pc_audio_online_cache
        _pc_audio_online_cache = bool(val)
        _probe_fail_counts["pc_audio"] = 0 if val else _PROBE_OFFLINE_THRESHOLD


def _get_cached_gsv_online() -> bool:
    with _probe_lock:
        return bool(_gsv_online_cache)


def _get_cached_l3_backend_snapshot() -> dict:
    with _l3_cache_lock:
        if _l3_cache is not None:
            return dict(_l3_cache)
    return _read_local_l3_fallback()


def _get_cached_anima_relationship_snapshot(cfg: dict | None = None) -> dict:
    with _anima_rel_lock:
        if _anima_rel_cache is not None:
            return dict(_anima_rel_cache)
    return _default_offline_relationship(_get_anima_character_id(cfg))


def _probe_gsv_tcp_once() -> bool:
    """Lightweight TCP connect check for 19880 without touching GPU weights or locks."""
    try:
        u = urlparse(GSV_Host)
        host = u.hostname or "host.docker.internal"
        port = int(u.port or 19880)
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except Exception as exc:
        _log_rate_limited("probe_gsv_tcp", exc)
        return False


def _probe_background_services_once() -> dict[str, bool]:
    """Probe 18900 / 19880 / 18787 off the /api/state request path with anti-flap hysteresis (P2-22)."""
    global _pc_audio_online_cache, _gsv_online_cache, _l3_cache, _l3_cache_ts
    global _anima_rel_cache, _anima_rel_ts, _obsidian_cache, _obsidian_ts

    # 1. 18900 PC Audio Bridge
    audio_ok = False
    try:
        audio_ok = bool(pc_audio_router.is_pc_audio_bridge_online(force=True))
    except Exception as exc:
        _log_rate_limited("probe_pc_audio", exc)
        audio_ok = False
    with _probe_lock:
        if audio_ok:
            _probe_fail_counts["pc_audio"] = 0
            _pc_audio_online_cache = True
        else:
            _probe_fail_counts["pc_audio"] += 1
            if _probe_fail_counts["pc_audio"] >= _PROBE_OFFLINE_THRESHOLD:
                _pc_audio_online_cache = False

    # 2. 18900 L3 Backend Status
    l3_ok = False
    try:
        client = _get_console_http_client()
        resp = client.get(
            f"{PC_MCP_HOST}/l3/status",
            headers=pc_audio_router.get_pc_mcp_auth_headers(),
            timeout=1.6,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["mcp_online"] = True
            host_mode = str(data.get("pc_mode") or "").strip().lower()
            if host_mode in ("off", "safe", "full"):
                _set_cached_pc_mode(host_mode, online=True)
            with _l3_cache_lock:
                _l3_cache = dict(data)
                _l3_cache_ts = time.monotonic()
            l3_ok = True
    except Exception as exc:
        _log_rate_limited("probe_l3_status", exc)
        l3_ok = False
    with _probe_lock:
        if l3_ok:
            _probe_fail_counts["l3"] = 0
        else:
            _probe_fail_counts["l3"] += 1
            should_flip_l3 = _probe_fail_counts["l3"] >= _PROBE_OFFLINE_THRESHOLD
    if not l3_ok:
        with _l3_cache_lock:
            was_never_set = _l3_cache is None
        if was_never_set or should_flip_l3:
            _set_cached_pc_mode("safe", online=False)
            fb = _read_local_l3_fallback()
            with _l3_cache_lock:
                _l3_cache = dict(fb)
                _l3_cache_ts = time.monotonic()

    # 3. 18787 Anima Relationship
    rel_ok = False
    cfg = None
    try:
        cfg = _read_raw_yaml()
    except Exception as exc:
        _log_rate_limited("probe_read_yaml", exc)
        cfg = None
    char_id = _get_anima_character_id(cfg)
    anima_tok = _read_anima_token()
    if anima_tok:
        try:
            client = _get_console_http_client()
            resp = client.get(
                f"{ANIMA_HOST}/xiaozhi/relationship",
                params={"character_id": char_id},
                headers={"Authorization": f"Bearer {anima_tok}"},
                timeout=1.8,
            )
            if resp.status_code == 200:
                data = resp.json()
                data["online"] = True
                with _anima_rel_lock:
                    _anima_rel_cache = dict(data)
                    _anima_rel_ts = time.monotonic()
                rel_ok = True
        except Exception as exc:
            _log_rate_limited("probe_anima_rel", exc)
            rel_ok = False
    with _probe_lock:
        if rel_ok:
            _probe_fail_counts["anima_rel"] = 0
        else:
            _probe_fail_counts["anima_rel"] += 1
            should_flip_rel = _probe_fail_counts["anima_rel"] >= _PROBE_OFFLINE_THRESHOLD
    if not rel_ok:
        with _anima_rel_lock:
            if _anima_rel_cache is None:
                _anima_rel_cache = _default_offline_relationship(char_id)
                _anima_rel_ts = time.monotonic()
            elif should_flip_rel:
                stale = dict(_anima_rel_cache)
                stale["online"] = False
                _anima_rel_cache = stale
                _anima_rel_ts = time.monotonic()

    # 4. 18787 Obsidian Sync Status
    obs_ok = False
    if anima_tok:
        try:
            client = _get_console_http_client()
            resp = client.get(
                f"{ANIMA_HOST}/xiaozhi/obsidian/status",
                headers={"Authorization": f"Bearer {anima_tok}"},
                timeout=1.8,
            )
            if resp.status_code == 200:
                data = resp.json()
                data["online"] = True
                with _obsidian_lock:
                    _obsidian_cache = dict(data)
                    _obsidian_ts = time.monotonic()
                obs_ok = True
        except Exception as exc:
            _log_rate_limited("probe_obsidian", exc)
            obs_ok = False
    with _probe_lock:
        if obs_ok:
            _probe_fail_counts["obsidian"] = 0
        else:
            _probe_fail_counts["obsidian"] += 1
            should_flip_obs = _probe_fail_counts["obsidian"] >= _PROBE_OFFLINE_THRESHOLD
    if not obs_ok:
        with _obsidian_lock:
            was_never_set_obs = _obsidian_cache is None
        if was_never_set_obs or should_flip_obs:
            err_msg = "" if anima_tok else "未找到 Anima Brain 访问令牌 (studio_token.txt)"
            _mark_obsidian_cache_offline(err_msg)

    # 5. 19880 GPT-SoVITS TCP Liveness
    gsv_ok = _probe_gsv_tcp_once()
    with _probe_lock:
        if gsv_ok:
            _probe_fail_counts["gsv"] = 0
            _gsv_online_cache = True
        else:
            _probe_fail_counts["gsv"] += 1
            if _probe_fail_counts["gsv"] >= _PROBE_OFFLINE_THRESHOLD:
                _gsv_online_cache = False

    # 6. 18787 Anima Health & Online Backup Status (B-2 / B-1 inbox counters)
    try:
        client = _get_console_http_client()
        resp = client.get(f"{ANIMA_HOST}/xiaozhi/health", timeout=1.5)
        if resp.status_code in (200, 503):
            h_data = resp.json()
            if isinstance(h_data, dict):
                if isinstance(h_data.get("backup"), dict):
                    snap_bk = dict(h_data["backup"])
                    if "inbox_pending" in h_data:
                        snap_bk["inbox_pending"] = int(h_data.get("inbox_pending") or 0)
                    if "inbox_dead" in h_data:
                        snap_bk["inbox_dead"] = int(h_data.get("inbox_dead") or 0)
                    with _anima_backup_lock:
                        global _anima_backup_cache
                        _anima_backup_cache = snap_bk
    except Exception as exc:
        _log_rate_limited("probe_anima_health", exc)

    # 7. Auto-retry dirty GSV voice weight rollback when GSV is online and ESP32 is idle (D-3)
    dirty_target, _dirty_err = _get_voice_dirty_state()
    if dirty_target:
        # Always reconcile against current saved active GSV voice so stale dirty state never reverts a newly saved voice
        current_cfg_gsv = _get_configured_gsv_voice()
        if current_cfg_gsv is None:
            _set_voice_dirty(None, "")
            dirty_target = None
        elif current_cfg_gsv != dirty_target:
            dirty_target = current_cfg_gsv
    if gsv_ok and dirty_target and not _is_device_busy():
        acquired = _gsv_weights_lock.acquire(timeout=0.1)
        if acquired:
            try:
                # Re-check dirty state, configured voice, and device busy status AFTER acquiring
                # _gsv_weights_lock so concurrent POST /api/config voice switches or recoveries are never reverted.
                cur_dirty, _ = _get_voice_dirty_state()
                if cur_dirty and not _is_device_busy():
                    cur_cfg_gsv = _get_configured_gsv_voice()
                    if cur_cfg_gsv is None:
                        _set_voice_dirty(None, "")
                    else:
                        target_voice = cur_cfg_gsv
                        global _active_gsv_loaded
                        _active_gsv_loaded = "__dirty__"
                        _gsv_op_deadline_tls.deadline = time.monotonic() + 20.0
                        _ensure_gsv_weights(target_voice, persist_boot_yaml=False)
                        if _active_gsv_loaded == "__dirty__":
                            _active_gsv_loaded = target_voice
                        _set_voice_dirty(None, "")
                        msg = f"[WebConsole] 后台探测已自动恢复线上 GPT-SoVITS 音色权重: {target_voice}"
                        print(msg, flush=True)
                        with _state_lock:
                            _append_raw_log_locked(msg)
            except Exception as rb_exc:
                _active_gsv_loaded = "__dirty__"
                cur_cfg_on_err = _get_configured_gsv_voice()
                if cur_cfg_on_err is not None:
                    _set_voice_dirty(cur_cfg_on_err, str(rb_exc))
                else:
                    _set_voice_dirty(None, "")
                print(
                    f"[WebConsole] WARNING: 后台重试恢复线上音色权重 ({cur_cfg_on_err or dirty_target}) 仍失败: {rb_exc}",
                    flush=True,
                )
            finally:
                _gsv_op_deadline_tls.deadline = None
                _gsv_weights_lock.release()

    return {
        "pc_audio": _get_cached_pc_audio_online(),
        "l3": l3_ok,
        "anima_rel": rel_ok,
        "obsidian": obs_ok,
        "gsv": _get_cached_gsv_online(),
    }


def _service_probe_loop() -> None:
    while True:
        try:
            _probe_background_services_once()
        except Exception as exc:
            _log_rate_limited("service_probe_loop", exc)
        time.sleep(_PROBE_INTERVAL_SECONDS)


def _infer_character_name(prompt: str) -> str:
    p = (prompt or "").strip()
    if "雷电芽衣" in p:
        return "雷电芽衣"
    if "芽衣" in p:
        return "芽衣"
    m = re.search(r"你是(?:《[^》]+》中的)?([\u4e00-\u9fa5A-Za-z0-9]{2,8}?)(?:[，,。！!、\s]|$)", p)
    if m:
        cand = m.group(1).strip()
        if cand and not cand.startswith(("一个", "一名", "一位", "住在")):
            return cand
    return "小智"


def _sync_persona_to_anima(prompt: str):
    """将 18080 控制台的角色姓名与提示词自动同步至 18787 Anima Brain 数据库，确保双端一致。"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return
    try:
        token = _read_anima_token()
        if not token:
            return
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            boot = client.get(f"{ANIMA_HOST}/api/bootstrap", headers=headers)
            if boot.status_code != 200:
                return
            chars = boot.json().get("characters") or []
            target_spec = next((c for c in chars if c.get("id") == "xiaozhi"), None)
            if not target_spec:
                return
            new_name = _infer_character_name(clean_prompt)
            anima_persona = mei_emotion_router.strip_voice_tag_instruction_for_anima(clean_prompt)
            if target_spec.get("name") == new_name and (target_spec.get("persona") or "").strip() == anima_persona:
                return
            updated = dict(target_spec)
            updated["name"] = new_name
            updated["persona"] = anima_persona
            r = client.post(f"{ANIMA_HOST}/api/update_character", headers=headers, json={"spec": updated})
            if r.status_code == 200:
                print(f"[WebConsole] 已自动同步角色设定至 Anima Brain: name={new_name}", flush=True)
    except Exception as exc:
        print(f"[WebConsole] 同步 Anima Brain 设定跳过: {exc}", flush=True)


def _deep_merge_dict(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` onto `base` without discarding unrelated sibling keys (P1-13)."""
    out = dict(base) if isinstance(base, dict) else {}
    if not isinstance(overlay, dict):
        return out
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _get_current_voice_id(cfg: dict) -> str:
    selected = (cfg or {}).get("selected_module") or {}
    tts_key = selected.get("TTS", "GPT_SOVITS_V2")
    tts_cfg = ((cfg or {}).get("TTS") or {}).get(tts_key) or {}
    if tts_key == "GPT_SOVITS_V2":
        return _resolve_gsv_voice_id(tts_cfg)
    return str(tts_cfg.get("voice_id") or tts_cfg.get("voice") or "zh-CN-XiaoxiaoNeural")


_GSV_VOICE_IDENTITY_KEYS = (
    "voice_id",
    "ref_audio_path",
    "aux_ref_audio_paths",
    "prompt_text",
    "prompt_lang",
    "speed_factor",
    "top_k",
    "top_p",
    "temperature",
    "text_split_method",
)

_LLM_PROVIDER_DEFAULTS = {
    "DeepSeekLLM": {
        "type": "openai",
        "base_url": "https://api.deepseek.com",
        "model_name": "deepseek-flash",
        "api_key": "${DEEPSEEK_API_KEY}",
    },
    "GeminiOpenAI": {
        "type": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model_name": "gemini-3.5-flash-lite",
        "api_key": "${GEMINI_API_KEY}",
    },
}

ALLOWED_LLM_PROVIDERS = frozenset(_LLM_PROVIDER_DEFAULTS.keys())
ALLOWED_LLM_MODELS: dict[str, frozenset[str]] = {
    "DeepSeekLLM": frozenset(
        {
            "deepseek-flash",
            "deepseek-chat",
            "deepseek-reasoner",
            "deepseek-v4-pro",
            "deepseek-v3",
        }
    ),
    "GeminiOpenAI": frozenset(
        {
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        }
    ),
}

# D-2: Module-level lock serializing all .config.yaml read-modify-write operations
_config_write_lock = threading.RLock()


def _validate_config_fields(
    *,
    llm_provider: str | None,
    model_name: str | None,
    voice: str | None,
    cur_llm_provider: str,
) -> tuple[str, str | None, str | None]:
    """D-2: Strictly validate llm_provider, model_name, and voice against explicit allowlists (raise ValueError -> HTTP 400)."""
    target_provider = cur_llm_provider
    if llm_provider is not None:
        cand_prov = str(llm_provider).strip()
        if not cand_prov or cand_prov not in ALLOWED_LLM_PROVIDERS:
            raise ValueError(
                f"无效的 LLM 服务商: {cand_prov or '(空)'}（仅允许: {', '.join(sorted(ALLOWED_LLM_PROVIDERS))}）"
            )
        target_provider = cand_prov

    validated_model: str | None = None
    if model_name is not None:
        cand_model = str(model_name).strip()
        allowed_for_prov = ALLOWED_LLM_MODELS.get(target_provider, frozenset())
        if not cand_model or cand_model not in allowed_for_prov:
            raise ValueError(
                f"服务商 {target_provider} 不支持模型 {cand_model or '(空)'}（允许列表: {', '.join(sorted(allowed_for_prov))}）"
            )
        validated_model = cand_model

    validated_voice: str | None = None
    if voice is not None:
        cand_voice = str(voice).strip()
        if not cand_voice or cand_voice not in ALLOWED_VOICES:
            raise ValueError(f"无效的音色 ID: {cand_voice or '(空)'}（不在允许的本地 GSV / EdgeTTS 音色列表中）")
        validated_voice = cand_voice

    return target_provider, validated_model, validated_voice


def _write_updated_yaml(
    prompt: str | None = None,
    llm_provider: str | None = None,
    model_name: str | None = None,
    voice: str | None = None,
) -> dict:
    """Deep-merge ONLY user-modified keys into data/.config.yaml under _config_write_lock with strict allowlists (P1-13 / D-2 / D-7)."""
    global _active_gsv_loaded
    with _config_write_lock:
        cfg = _read_raw_yaml()
        selected = cfg.get("selected_module") or {}
        cur_prompt = str(cfg.get("prompt") or "").strip()
        cur_llm_provider = str(selected.get("LLM") or "DeepSeekLLM").strip()
        cur_llm_cfg = ((cfg.get("LLM") or {}).get(cur_llm_provider)) or {}
        cur_model = str(cur_llm_cfg.get("model_name") or "").strip()
        cur_voice = _get_current_voice_id(cfg)

        # D-2: Validate all supplied fields strictly before any GPU weight swap or file write
        target_provider, validated_model, validated_voice = _validate_config_fields(
            llm_provider=llm_provider,
            model_name=model_name,
            voice=voice,
            cur_llm_provider=cur_llm_provider,
        )

        patch: dict = {}
        changed_fields: list[str] = []

        if prompt is not None:
            new_prompt = prompt.strip()
            if new_prompt and new_prompt != cur_prompt:
                patch["prompt"] = new_prompt + "\n"
                changed_fields.append("prompt")

        provider_changed = target_provider != cur_llm_provider
        if provider_changed:
            changed_fields.append("llm_provider")
            patch.setdefault("selected_module", {})["LLM"] = target_provider
            existing_prov_blk = ((cfg.get("LLM") or {}).get(target_provider)) or {}
            if not existing_prov_blk and target_provider in _LLM_PROVIDER_DEFAULTS:
                patch.setdefault("LLM", {})[target_provider] = dict(_LLM_PROVIDER_DEFAULTS[target_provider])

        existing_target_model = str(
            (((cfg.get("LLM") or {}).get(target_provider)) or {}).get("model_name") or ""
        ).strip()
        if validated_model and validated_model != (cur_model if not provider_changed else existing_target_model):
            changed_fields.append("model_name")
            prov_patch = patch.setdefault("LLM", {}).setdefault(target_provider, {})
            if not ((cfg.get("LLM") or {}).get(target_provider)) and target_provider in _LLM_PROVIDER_DEFAULTS:
                for dk, dv in _LLM_PROVIDER_DEFAULTS[target_provider].items():
                    prov_patch.setdefault(dk, dv)
            prov_patch["model_name"] = validated_model

        voice_recovered = False
        if validated_voice is not None:
            v_id = validated_voice
            if v_id != cur_voice:
                changed_fields.append("voice")
                if v_id in GSV_PROFILES:
                    with _gsv_weights_lock:
                        dirty_before, _ = _get_voice_dirty_state()
                        if dirty_before is not None:
                            _active_gsv_loaded = "__dirty__"
                        try:
                            _ensure_gsv_weights(v_id, persist_boot_yaml=True)
                        except Exception as exc:
                            if (
                                _active_gsv_loaded == "__dirty__" or dirty_before is not None
                            ) and cur_voice in GSV_PROFILES:
                                _set_voice_dirty(cur_voice, str(exc))
                            raise
                        if _active_gsv_loaded == "__dirty__":
                            _active_gsv_loaded = v_id
                        _set_voice_dirty(None, "")
                    patch.setdefault("selected_module", {})["TTS"] = "GPT_SOVITS_V2"
                    existing_gsv = ((cfg.get("TTS") or {}).get("GPT_SOVITS_V2")) or {}
                    prof_blk = GSV_PROFILES[v_id]["block"]
                    gsv_patch: dict = {}
                    # Populate missing defaults only once, never overwriting user-customized tuning keys
                    for dk, dv in prof_blk.items():
                        if dk not in existing_gsv:
                            gsv_patch[dk] = dv
                    # Update only voice-identity fields (incl. explicit voice_id) for the newly selected GSV profile
                    for ik in _GSV_VOICE_IDENTITY_KEYS:
                        if ik in prof_blk:
                            gsv_patch[ik] = prof_blk[ik]
                    gsv_patch["voice_id"] = v_id
                    if "sample_steps" in prof_blk and "sample_steps" not in existing_gsv:
                        gsv_patch["sample_steps"] = prof_blk["sample_steps"]
                    patch.setdefault("TTS", {})["GPT_SOVITS_V2"] = gsv_patch
                else:
                    with _gsv_weights_lock:
                        _set_voice_dirty(None, "")
                    patch.setdefault("selected_module", {})["TTS"] = "EdgeTTS"
                    existing_edge = ((cfg.get("TTS") or {}).get("EdgeTTS")) or {}
                    edge_patch = {"voice": v_id, "voice_id": v_id}
                    if "type" not in existing_edge:
                        edge_patch["type"] = "edge"
                    if "output_dir" not in existing_edge:
                        edge_patch["output_dir"] = "tmp/"
                    patch.setdefault("TTS", {})["EdgeTTS"] = edge_patch
            else:
                if v_id in GSV_PROFILES:
                    with _gsv_weights_lock:
                        dirty_target, _ = _get_voice_dirty_state()
                        is_dirty = (dirty_target is not None) or (_active_gsv_loaded == "__dirty__")
                        if is_dirty:
                            _active_gsv_loaded = "__dirty__"
                            try:
                                _ensure_gsv_weights(v_id, persist_boot_yaml=True)
                            except Exception as exc:
                                _set_voice_dirty(v_id, str(exc))
                                raise
                            if _active_gsv_loaded == "__dirty__":
                                _active_gsv_loaded = v_id
                            _set_voice_dirty(None, "")
                            voice_recovered = True
                else:
                    dirty_target, _ = _get_voice_dirty_state()
                    if dirty_target is not None:
                        _set_voice_dirty(None, "")

        if not changed_fields:
            return {"changed_fields": [], "written": False, "voice_recovered": voice_recovered}

        merged_cfg = _deep_merge_dict(cfg, patch)
        header = (
            "# =====================================================================\n"
            "# 小智 ESP32 本地服务端精简配置（由本地 Web 控制台管理）\n"
            "# 自动读取 DEEPSEEK_API_KEY 与 GEMINI_API_KEY 环境变量，不落盘明文密钥\n"
            "# =====================================================================\n\n"
        )
        body = yaml.safe_dump(merged_cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
        _atomic_write_text(CONFIG_PATH, header + body)
        _get_config_yaml_cache(CONFIG_PATH).set(CONFIG_PATH, copy.deepcopy(merged_cfg))

    if "prompt" in changed_fields and prompt is not None:
        threading.Thread(target=_sync_persona_to_anima, args=(prompt,), daemon=True).start()
    return {"changed_fields": changed_fields, "written": True, "voice_recovered": voice_recovered}


def _apply_tts_attrs_in_place(tts_obj, tts_module_name: str, tts_cfg: dict) -> bool:
    """Update live TTS provider parameters in-place when engine class stays unchanged."""
    if tts_obj is None or not isinstance(tts_cfg, dict):
        return False
    cls_mod = str(getattr(type(tts_obj), "__module__", "") or "")
    if tts_module_name == "GPT_SOVITS_V2" and "gpt_sovits" in cls_mod:
        for attr in ("ref_audio_path", "prompt_text", "prompt_lang", "text_split_method", "url"):
            if attr in tts_cfg and tts_cfg[attr] is not None:
                setattr(tts_obj, attr, tts_cfg[attr])
        if "aux_ref_audio_paths" in tts_cfg:
            raw_aux = tts_cfg["aux_ref_audio_paths"]
            tts_obj.aux_ref_audio_paths = list(raw_aux) if isinstance(raw_aux, (list, tuple)) else []
        for f_attr in ("speed_factor", "temperature", "top_p"):
            if f_attr in tts_cfg and tts_cfg[f_attr] is not None:
                try:
                    setattr(tts_obj, f_attr, float(tts_cfg[f_attr]))
                except Exception as exc:
                    _log_rate_limited(f"tts_attr_{f_attr}", exc)
        for i_attr in ("top_k", "sample_steps"):
            if i_attr in tts_cfg and tts_cfg[i_attr] is not None:
                try:
                    setattr(tts_obj, i_attr, int(tts_cfg[i_attr]))
                except Exception as exc:
                    _log_rate_limited(f"tts_attr_{i_attr}", exc)
        if isinstance(getattr(tts_obj, "config", None), dict):
            tts_obj.config.update(tts_cfg)
        return True
    if tts_module_name == "EdgeTTS" and "edge" in cls_mod:
        if tts_cfg.get("voice"):
            tts_obj.voice = tts_cfg["voice"]
        if isinstance(getattr(tts_obj, "config", None), dict):
            tts_obj.config.update(tts_cfg)
        return True
    return False


def _hot_reload_runtime_config(changed_fields: list[str]) -> tuple[bool, str]:
    """Apply saved prompt / LLM / voice config changes live in-process without killing the server (P1-13)."""
    if not changed_fields:
        return True, "no_changes"
    try:
        import config.config_loader as config_loader
        from core.utils.cache.manager import CacheType, cache_manager
        from core.utils.modules_initialize import initialize_modules

        default_cfg_path = config_loader.get_project_dir() + "config.yaml"
        custom_cfg_path = CONFIG_PATH
        default_cfg = config_loader.read_config(default_cfg_path)
        custom_cfg = config_loader.read_config(custom_cfg_path)
        new_cfg = config_loader.merge_configs(default_cfg, custom_cfg)
        config_loader.ensure_directories(new_cfg)

        # Preserve runtime-generated auth_key if live server holds one
        servers = runtime_events.get_ws_servers()
        if servers:
            old_auth_key = ((getattr(servers[0], "config", None) or {}).get("server") or {}).get("auth_key")
            if old_auth_key and not (new_cfg.get("server") or {}).get("auth_key"):
                new_cfg.setdefault("server", {})["auth_key"] = old_auth_key

        try:
            cache_manager.set(CacheType.CONFIG, "main_config", new_cfg)
        except Exception as exc:
            _log_rate_limited("hot_reload_cache_set", exc)

        llm_changed = "llm_provider" in changed_fields or "model_name" in changed_fields
        prompt_changed = "prompt" in changed_fields
        voice_changed = "voice" in changed_fields

        new_llm = None
        for srv in servers:
            if isinstance(getattr(srv, "config", None), dict):
                # Atomic in-place update without clearing to {} (avoids empty-config race window)
                srv.config.update(new_cfg)
                for stale_k in list(srv.config.keys()):
                    if stale_k not in new_cfg:
                        srv.config.pop(stale_k, None)
            else:
                srv.config = new_cfg
            if llm_changed:
                mods = initialize_modules(srv.logger, srv.config, init_llm=True)
                if "llm" in mods:
                    srv._llm = mods["llm"]
                    new_llm = mods["llm"]

        if llm_changed and new_llm is None and not servers:
            # Offline/test environment without WebSocketServer: still validate LLM config instantiability
            pass

        tts_mod_name = str((new_cfg.get("selected_module") or {}).get("TTS") or "")
        tts_cfg = ((new_cfg.get("TTS") or {}).get(tts_mod_name)) or {}
        tts_class_mismatch = False

        for conn in runtime_events.get_active_connections():
            try:
                if isinstance(getattr(conn, "common_config", None), dict):
                    conn.common_config = new_cfg
                if isinstance(getattr(conn, "config", None), dict):
                    if prompt_changed:
                        conn.config["prompt"] = new_cfg.get("prompt", "")
                    if llm_changed:
                        conn.config.setdefault("selected_module", {})["LLM"] = (
                            new_cfg.get("selected_module") or {}
                        ).get("LLM")
                        conn.config["LLM"] = new_cfg.get("LLM", {})
                    if voice_changed:
                        conn.config.setdefault("selected_module", {})["TTS"] = tts_mod_name
                        conn.config["TTS"] = new_cfg.get("TTS", {})
                if prompt_changed and hasattr(conn, "prompt_manager") and hasattr(conn, "change_system_prompt"):
                    enhanced = conn.prompt_manager.build_enhanced_prompt(
                        new_cfg.get("prompt", ""),
                        getattr(conn, "device_id", "") or "",
                        getattr(conn, "client_ip", None),
                        emoji_enabled=(getattr(conn, "features", None) or {}).get("emoji", True),
                    )
                    if enhanced:
                        conn.change_system_prompt(enhanced)
                if llm_changed and new_llm is not None:
                    conn.llm = new_llm
                    intent_obj = getattr(conn, "intent", None)
                    if intent_obj is not None and hasattr(intent_obj, "set_llm"):
                        intent_sel = (new_cfg.get("selected_module") or {}).get("Intent")
                        intent_llm_override = ((new_cfg.get("Intent") or {}).get(intent_sel) or {}).get("llm")
                        if not intent_llm_override:
                            try:
                                intent_obj.set_llm(new_llm)
                            except Exception as exc:
                                _log_rate_limited("hot_reload_intent_set_llm", exc)
                    mem_obj = getattr(conn, "memory", None)
                    if mem_obj is not None and hasattr(mem_obj, "set_llm"):
                        try:
                            mem_obj.set_llm(new_llm)
                        except Exception as exc:
                            _log_rate_limited("hot_reload_memory_set_llm", exc)
                if voice_changed and getattr(conn, "tts", None) is not None:
                    if not _apply_tts_attrs_in_place(conn.tts, tts_mod_name, tts_cfg):
                        tts_class_mismatch = True
            except Exception as conn_exc:
                print(f"[WebConsole] 单连接热更新跳过: {conn_exc}", flush=True)

        if tts_class_mismatch:
            print(
                f"[WebConsole] 检测到活跃连接切换了 TTS 引擎类型 ({tts_mod_name})，需平滑重启生效",
                flush=True,
            )
            return False, "tts_engine_class_changed_on_active_conn"

        print(f"[WebConsole] 配置已热更新生效（免重启）: {', '.join(changed_fields)}", flush=True)
        return True, "hot_reloaded"
    except Exception as exc:
        print(f"[WebConsole] 配置热更新失败，回退重启动作: {exc}", flush=True)
        return False, str(exc)


def _generate_test_chime_wav(duration_sec: float = 0.4, sample_rate: int = 48000) -> bytes:
    """Generate a clean 0.4s 48kHz mono 16-bit sine chime WAV in memory as audio-test fallback."""
    n_samples = max(1, int(sample_rate * duration_sec))
    fade_in = max(1, int(sample_rate * 0.02))
    fade_out = max(1, int(sample_rate * 0.06))
    half = n_samples // 2
    amp = 0.28 * 32767.0
    frames = bytearray()
    phase = 0.0
    two_pi_over_sr = 2.0 * math.pi / float(sample_rate)
    for i in range(n_samples):
        freq = 587.33 if i < half else 880.0
        env = 1.0
        if i < fade_in:
            env = 0.5 * (1.0 - math.cos(math.pi * i / fade_in))
        elif i >= n_samples - fade_out:
            rem = n_samples - 1 - i
            env = 0.5 * (1.0 - math.cos(math.pi * rem / fade_out))
        sample = int(max(-32767, min(32767, round(amp * env * math.sin(phase)))))
        phase += two_pi_over_sr * freq
        frames.extend(struct.pack("<h", sample))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


async def _synthesize_mp3(text: str, voice: str) -> bytes:
    comm = edge_tts.Communicate(text=text, voice=voice)
    chunks = bytearray()
    async for item in comm.stream():
        if item.get("type") == "audio":
            chunks.extend(item["data"])
    return bytes(chunks)


async def _synthesize_mp3_with_timeout(
    text: str, voice: str, timeout: float = EDGE_PREVIEW_TIMEOUT_SECONDS
) -> bytes:
    return await asyncio.wait_for(_synthesize_mp3(text=text, voice=voice), timeout=float(timeout))


# --- B-5 / F-1 / G-4 Console Integration Helpers ---
_DEFAULT_VAD_SPECULATIVE = {
    "enabled": True,
    "speculative_ms": 550,
    "post_wakeup_ms": 800,
    "min_chars": 4,
    "complete_suffixes": [],
    "hesitation_suffixes": [],
}
_ALLOWED_VAD_SPEC_KEYS = frozenset(
    {"enabled", "speculative_ms", "post_wakeup_ms", "min_chars", "complete_suffixes", "hesitation_suffixes"}
)
_ALLOWED_ASR_CORR_KEYS = frozenset({"enabled", "corrections"})


def _sanitize_outbox_stats(raw: dict | None) -> dict:
    d = raw if isinstance(raw, dict) else {}
    return {
        "pending": int(d.get("pending") or 0),
        "dead": int(d.get("dead") or 0),
        "corrupt": int(d.get("corrupt") or 0),
        "sent": int(d.get("sent") or 0),
        "max_retries": int(d.get("max_retries") or 8),
        "dead_ttl_days": float(d.get("dead_ttl_days") or 14.0),
        "last_error": _redact_secrets(str(d.get("last_error") or "")),
        "worker_alive": bool(d.get("worker_alive", False)),
    }


def _get_anima_outbox_snapshot() -> dict:
    try:
        import anima_memory_provider

        return _sanitize_outbox_stats(anima_memory_provider.get_outbox_stats())
    except Exception as exc:
        _log_rate_limited("get_anima_outbox_stats", exc)
        return _sanitize_outbox_stats(None)


def _replay_anima_outbox_dead(payload: dict) -> tuple[bool, dict | str, int]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象", int(HTTPStatus.BAD_REQUEST)
    unknown = sorted(str(k) for k in payload.keys() if k not in ("limit", "request_ids"))
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}", int(HTTPStatus.BAD_REQUEST)

    limit_arg: int | None = None
    if "limit" in payload and payload.get("limit") is not None:
        raw_lim = payload.get("limit")
        if isinstance(raw_lim, bool):
            return False, "字段 limit 必须为正整数", int(HTTPStatus.BAD_REQUEST)
        try:
            limit_arg = int(raw_lim)
        except (TypeError, ValueError):
            return False, "字段 limit 必须为正整数", int(HTTPStatus.BAD_REQUEST)
        if limit_arg <= 0 or limit_arg > 1000:
            return False, "字段 limit 必须在 1～1000 之间", int(HTTPStatus.BAD_REQUEST)

    req_ids_arg: list[str] | None = None
    if "request_ids" in payload and payload.get("request_ids") is not None:
        raw_ids = payload.get("request_ids")
        if not isinstance(raw_ids, list) or not all(isinstance(x, str) and x.strip() for x in raw_ids):
            return False, "字段 request_ids 必须为非空字符串数组", int(HTTPStatus.BAD_REQUEST)
        req_ids_arg = [x.strip() for x in raw_ids]

    try:
        import anima_memory_provider

        fn = getattr(anima_memory_provider, "replay_dead_outbox_turns", None)
        if not callable(fn):
            return False, "当前 anima_memory_provider 版本不支持重放 dead 队列", int(HTTPStatus.NOT_IMPLEMENTED)
        res = fn(limit=limit_arg, request_ids=req_ids_arg)
        if not isinstance(res, dict) or not res.get("ok"):
            err = (res.get("error") if isinstance(res, dict) else None) or "重放 dead 队列失败"
            return False, str(err), int(HTTPStatus.INTERNAL_SERVER_ERROR)
        return (
            True,
            {
                "ok": True,
                "replayed": int(res.get("replayed") or 0),
                "outbox": _sanitize_outbox_stats(res.get("stats")),
            },
            int(HTTPStatus.OK),
        )
    except Exception as exc:
        _log_rate_limited("replay_anima_outbox_dead", exc)
        return False, f"重放 Anima dead 队列失败: {exc}", int(HTTPStatus.INTERNAL_SERVER_ERROR)


def _get_vad_speculative_snapshot() -> dict:
    try:
        fn = getattr(runtime_settings, "get_vad_speculative", None)
        if callable(fn):
            return dict(fn())
    except Exception as exc:
        _log_rate_limited("get_vad_speculative", exc)
    return copy.deepcopy(_DEFAULT_VAD_SPECULATIVE)


def _get_vad_speculative_stats_snapshot() -> dict:
    try:
        import vad_speculative_router

        fn = getattr(vad_speculative_router, "get_vad_speculative_stats", None)
        if callable(fn):
            return dict(fn())
    except ImportError:
        pass
    except Exception as exc:
        _log_rate_limited("get_vad_speculative_stats", exc)
    return {
        "speculative_triggers": 0,
        "early_commits": 0,
        "resumed_discards": 0,
        "held_hesitation": 0,
        "held_incomplete": 0,
        "story_mode_skips": 0,
        "avg_saved_ms": 0.0,
    }


def _update_vad_speculative_config(payload: dict) -> tuple[bool, dict | str]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象"
    unknown = sorted(str(k) for k in payload.keys() if k not in _ALLOWED_VAD_SPEC_KEYS)
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}"

    kwargs: dict = {}
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            return False, "字段 enabled 必须为布尔值"
        kwargs["enabled"] = payload["enabled"]

    for int_key, lo, hi, label in (
        ("speculative_ms", 300, 950, "早判静默阈值 speculative_ms"),
        ("post_wakeup_ms", 450, 1050, "唤醒首句静默阈值 post_wakeup_ms"),
        ("min_chars", 2, 20, "最小字数 min_chars"),
    ):
        if int_key in payload:
            raw_v = payload[int_key]
            if isinstance(raw_v, bool):
                return False, f"{label} 必须为整数（{lo}～{hi}）"
            try:
                iv = int(raw_v)
            except (TypeError, ValueError):
                return False, f"{label} 必须为整数（{lo}～{hi}）"
            if iv < lo or iv > hi:
                return False, f"{label} 必须在 {lo}～{hi} 之间"
            kwargs[int_key] = iv

    for list_key in ("complete_suffixes", "hesitation_suffixes"):
        if list_key in payload:
            raw_l = payload[list_key]
            if isinstance(raw_l, str):
                kwargs[list_key] = raw_l
            elif isinstance(raw_l, list) and all(isinstance(x, str) for x in raw_l):
                kwargs[list_key] = raw_l
            else:
                return False, f"字段 {list_key} 必须为字符串数组或逗号/换行分隔文本"

    if not kwargs:
        return False, "请至少提供一个要更新的投机 VAD 字段"

    fn = getattr(runtime_settings, "set_vad_speculative", None)
    if not callable(fn):
        return False, "当前 runtime_settings 版本不支持 set_vad_speculative"
    try:
        saved = fn(**kwargs)
        return True, dict(saved)
    except ValueError as exc:
        return False, str(exc)


def _get_asr_corrections_snapshot() -> dict:
    try:
        import asr_corrections

        return dict(asr_corrections.get_asr_corrections())
    except ImportError:
        pass
    except Exception as exc:
        _log_rate_limited("get_asr_corrections", exc)
    return {"schema_version": 1, "enabled": True, "corrections": {}, "count": 0}


def _update_asr_corrections_config(payload: dict) -> tuple[bool, dict | str]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象"
    unknown = sorted(str(k) for k in payload.keys() if k not in _ALLOWED_ASR_CORR_KEYS)
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}"

    has_en = "enabled" in payload
    has_corr = "corrections" in payload
    if not has_en and not has_corr:
        return False, "请至少提供 enabled 或 corrections"

    en_arg: bool | None = None
    if has_en:
        if not isinstance(payload["enabled"], bool):
            return False, "字段 enabled 必须为布尔值"
        en_arg = payload["enabled"]

    corr_arg = None
    if has_corr:
        raw_c = payload["corrections"]
        if not isinstance(raw_c, (dict, list, str)):
            return False, "字段 corrections 必须为映射对象、规则数组或 '错词->正词' 多行文本"
        corr_arg = raw_c

    try:
        import asr_corrections

        saved = asr_corrections.save_asr_corrections(corrections=corr_arg, enabled=en_arg)
        return True, dict(saved)
    except ValueError as exc:
        return False, str(exc)
    except ImportError:
        return False, "当前环境未加载 asr_corrections 模块"


class ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def end_headers(self):
        # A-5: Guarantee clickjacking & MIME-sniffing protection on EVERY response (including HTML error pages)
        existing = set()
        for raw in getattr(self, "_headers_buffer", ()):
            if isinstance(raw, (bytes, bytearray)) and b":" in raw:
                existing.add(raw.split(b":", 1)[0].strip().lower())
        if b"x-frame-options" not in existing:
            self.send_header("X-Frame-Options", "DENY")
        if b"content-security-policy" not in existing:
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        if b"x-content-type-options" not in existing:
            self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _send_json(self, payload: dict, status=HTTPStatus.OK):
        # D-5: Automatically redact secrets in all error responses and error-bearing fields
        is_err = int(status) >= 400 or (isinstance(payload, dict) and payload.get("ok") is False)
        safe_payload = _redact_json_payload(payload, redact_all_strings=is_err)
        raw = json.dumps(safe_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise _BadRequestError("Invalid Content-Length header") from exc
        if length < 0 or length > 2 * 1024 * 1024:
            raise _BadRequestError("Request body size out of range")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise _BadRequestError(f"Invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise _BadRequestError("JSON body must be an object")
        return data

    def _enforce_security(self, method: str) -> bool:
        hdrs = {k: v for k, v in self.headers.items()}
        ok, status_code, err_msg = validate_console_request(
            method, hdrs, port=CONSOLE_PORT, path=self.path
        )
        if not ok:
            self._send_json({"ok": False, "error": err_msg}, HTTPStatus(status_code))
            return False
        return True

    def do_GET(self):
        if not self._enforce_security("GET"):
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            _sync_runtime_events()
            with _state_lock:
                ready = bool(_device_info.get("server_ready"))
            health = patch_registry.get_health_summary()
            degraded_list = list(health.get("degraded") or [])
            security_ok = bool(health.get("security_ok", True))
            http_status = HTTPStatus.OK if security_ok else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(
                {
                    "ok": security_ok and not degraded_list,
                    "status": health.get("status", "ok"),
                    "degraded": degraded_list,
                    "security_ok": security_ok,
                    "security_failures": list(health.get("security_failures") or []),
                    "insecure_boot": bool(health.get("insecure_boot", False)),
                    "insecure_boot_env": bool(health.get("insecure_boot_env", False)),
                    "server_ready": ready,
                    "server_version": health.get("server_version", ""),
                    "expected_server_version": health.get("expected_server_version", ""),
                    "version_match": bool(health.get("version_match", True)),
                    "patch_counts": health.get("counts", {}),
                    "patches": health.get("patches", {}),
                    "degraded_reasons": health.get("degraded_reasons", []),
                },
                status=http_status,
            )
            return
        if parsed.path in ("/", "/index.html"):
            if not os.path.exists(HTML_PATH):
                self._send_json({"error": "console.html not found"}, HTTPStatus.NOT_FOUND)
                return
            with open(HTML_PATH, "r", encoding="utf-8") as f:
                html_text = f.read()
            html_text = html_text.replace(_CONSOLE_TOKEN_PLACEHOLDER, _CONSOLE_WRITE_TOKEN)
            html_bytes = html_text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        if parsed.path == "/api/state":
            cfg = _read_raw_yaml()
            selected = cfg.get("selected_module", {})
            llm_key = selected.get("LLM", "DeepSeekLLM")
            tts_key = selected.get("TTS", "GPT_SOVITS_V2")
            llm_cfg = cfg.get("LLM", {}).get(llm_key, {})
            tts_cfg = cfg.get("TTS", {}).get(tts_key, {})
            srv_cfg = cfg.get("server", {})
            raw_ws = str(srv_cfg.get("websocket") or f"ws://{HOST_LAN_IP}:18000/xiaozhi/v1/")
            ws_url = raw_ws.replace("${HOST_LAN_IP}", HOST_LAN_IP)
            ota_url = ws_url.replace("ws://", "http://").replace(":18000/xiaozhi/v1/", ":18003/xiaozhi/ota/")

            ds_env = os.environ.get("DEEPSEEK_API_KEY", "")
            gem_env = os.environ.get("GEMINI_API_KEY", "")
            ds_ok = bool(ds_env and len(ds_env) > 10 and "请在这里" not in ds_env)
            gem_ok = bool(gem_env and len(gem_env) > 10 and "请在这里" not in gem_env)

            active_voice = _get_current_voice_id(cfg)

            qs = parse_qs(parsed.query)
            req_cursor: int | None = None
            raw_cursor_vals = qs.get("log_cursor")
            if raw_cursor_vals:
                try:
                    req_cursor = int(raw_cursor_vals[0])
                except (TypeError, ValueError):
                    req_cursor = None

            _sync_runtime_events()
            with _state_lock:
                dev = dict(_device_info)
                msgs = list(_chat_messages[-60:])
            logs, latest_log_cursor, logs_inc, logs_reset = _get_logs_for_state(req_cursor)
            health_summary = patch_registry.get_health_summary()

            audio_cfg = pc_audio_router.read_audio_output_config()
            reply_len_cfg = reply_length_router.read_reply_length_config()
            idle_farewell_cfg = runtime_settings.get_idle_farewell()
            # P2-22: Pure in-memory cached snapshots only (zero synchronous HTTP probes on /api/state)
            rel_cfg = _get_cached_anima_relationship_snapshot(cfg=cfg)
            l3_cfg = _get_cached_l3_backend_snapshot()
            obs_cfg = _get_cached_obsidian_snapshot()
            backup_cfg = _get_cached_anima_backup_snapshot()
            outbox_cfg = _get_anima_outbox_snapshot()
            vad_spec_cfg = _get_vad_speculative_snapshot()
            vad_spec_stats = _get_vad_speculative_stats_snapshot()
            asr_corr_cfg = _get_asr_corrections_snapshot()
            dirty_voice, dirty_voice_err = _get_voice_dirty_state()

            self._send_json(
                {
                    "config": {
                        "prompt": (cfg.get("prompt") or "").strip(),
                        "llm_provider": llm_key,
                        "model_name": llm_cfg.get(
                            "model_name",
                            "deepseek-flash" if llm_key == "DeepSeekLLM" else "gemini-3.5-flash-lite",
                        ),
                        "voice": active_voice,
                        "voice_dirty": bool(dirty_voice),
                        "voice_dirty_target": dirty_voice or "",
                        "voice_dirty_error": dirty_voice_err,
                        "websocket": ws_url,
                        "ota_url": ota_url,
                        "deepseek_key_ok": ds_ok,
                        "gemini_key_ok": gem_ok,
                        "llm_cache": turn_context_router.get_llm_cache_stats(),
                        "pc_mode": _read_pc_mode(),
                        "pc_mode_online": _get_cached_pc_mode_online(),
                        "audio_output": audio_cfg["target"],
                        "pc_audio_volume": audio_cfg["volume"],
                        "pc_audio_online": _get_cached_pc_audio_online(),
                        "gsv_online": _get_cached_gsv_online(),
                        "reply_length": reply_len_cfg,
                        "idle_farewell": idle_farewell_cfg,
                        "relationship": rel_cfg,
                        "anima_backup": backup_cfg,
                        "anima_outbox": outbox_cfg,
                        "vad_speculative": vad_spec_cfg,
                        "vad_speculative_stats": vad_spec_stats,
                        "asr_corrections": asr_corr_cfg,
                        "obsidian": obs_cfg,
                        "l3_backend": l3_cfg,
                        "agy_bridge_online": _check_agy_bridge_online(l3_cfg),
                        "mei_emotions": [
                            {"id": k, "label": v.get("label_zh", k)}
                            for k, v in mei_emotion_router.load_mei_emotions().items()
                        ],
                    },
                    "device": dev,
                    "health": {
                        "status": health_summary.get("status", "ok"),
                        "degraded": bool(health_summary.get("degraded")),
                        "degraded_list": list(health_summary.get("degraded") or []),
                        "security_ok": bool(health_summary.get("security_ok", True)),
                        "security_failures": list(health_summary.get("security_failures") or []),
                        "insecure_boot": bool(health_summary.get("insecure_boot", False)),
                        "insecure_boot_env": bool(health_summary.get("insecure_boot_env", False)),
                        "server_version": health_summary.get("server_version", ""),
                        "degraded_reasons": health_summary.get("degraded_reasons", []),
                    },
                    "messages": msgs,
                    "logs": logs,
                    "log_cursor": latest_log_cursor,
                    "logs_incremental": logs_inc,
                    "logs_reset": logs_reset,
                }
            )
            return

        if parsed.path == "/api/relationship":
            rel_cfg = _fetch_anima_relationship(force=True)
            self._send_json({"ok": bool(rel_cfg.get("online")), "relationship": rel_cfg})
            return

        if parsed.path == "/api/obsidian":
            obs_cfg = _fetch_obsidian_status(force=True)
            self._send_json({"ok": bool(obs_cfg.get("online")), "obsidian": obs_cfg})
            return

        if parsed.path == "/api/l3-backend":
            l3_cfg = _fetch_l3_backend_status(force=True)
            self._send_json({"ok": True, "l3_backend": l3_cfg})
            return

        if parsed.path == "/api/idle-farewell":
            self._send_json({"ok": True, "idle_farewell": runtime_settings.get_idle_farewell()})
            return

        if parsed.path == "/api/anima-outbox":
            self._send_json({"ok": True, "outbox": _get_anima_outbox_snapshot()})
            return

        if parsed.path == "/api/vad-speculative":
            self._send_json(
                {
                    "ok": True,
                    "vad_speculative": _get_vad_speculative_snapshot(),
                    "stats": _get_vad_speculative_stats_snapshot(),
                }
            )
            return

        if parsed.path == "/api/asr-corrections":
            self._send_json({"ok": True, "asr_corrections": _get_asr_corrections_snapshot()})
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self._enforce_security("POST"):
            return
        parsed = urlparse(self.path)
        try:
            self._handle_post(parsed)
        except _BadRequestError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _handle_post(self, parsed):

        if parsed.path == "/api/pc-mode":
            try:
                body = self._read_body_json()
                mode = str(body.get("mode") or "safe").strip().lower()
                if mode not in ("off", "safe", "full"):
                    self._send_json({"ok": False, "error": "无效的权限挡位"}, HTTPStatus.BAD_REQUEST)
                    return
                res = _write_pc_mode(mode)
                self._send_json(res)
            except _BadRequestError:
                raise
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:
                self._send_json(
                    {"ok": False, "error": f"宿主机 18900 MCP 不可用，未能切换挡位: {_redact_secrets(str(e))}"},
                    HTTPStatus.BAD_GATEWAY,
                )
            return

        if parsed.path == "/api/audio-output":
            try:
                body = self._read_body_json()
                target = str(body.get("target") or "").strip().lower()
                volume = body.get("volume", None)
                if not target:
                    target = pc_audio_router.read_audio_output_config()["target"]
                if target not in ("esp32", "pc"):
                    self._send_json({"ok": False, "error": "无效的音频输出目标"}, HTTPStatus.BAD_REQUEST)
                    return
                saved = pc_audio_router.write_audio_output_config(target, volume)
                pc_online = pc_audio_router.is_pc_audio_bridge_online(force=True)
                _set_cached_pc_audio_online(pc_online)
                self._send_json(
                    {
                        "ok": True,
                        "target": saved["target"],
                        "volume": saved["volume"],
                        "pc_audio_online": pc_online,
                    }
                )
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/reply-length":
            try:
                body = self._read_body_json()
                saved = reply_length_router.write_reply_length_config(
                    chat_chars=body.get("chat_chars", None),
                    story_chars=body.get("story_chars", None),
                    preset=body.get("preset", None),
                )
                self._send_json({"ok": True, "reply_length": saved})
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/idle-farewell":
            try:
                body = self._read_body_json()
                ok_if, res_or_err = _update_idle_farewell_config(body)
                if ok_if:
                    self._send_json({"ok": True, "idle_farewell": res_or_err})
                else:
                    self._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus.BAD_REQUEST)
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/anima-outbox/replay-dead":
            try:
                body = self._read_body_json()
                ok_rep, res_or_err, status_code = _replay_anima_outbox_dead(body)
                if ok_rep and isinstance(res_or_err, dict):
                    self._send_json(res_or_err, HTTPStatus(status_code))
                else:
                    self._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus(status_code))
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/vad-speculative":
            try:
                body = self._read_body_json()
                ok_vs, res_or_err = _update_vad_speculative_config(body)
                if ok_vs:
                    self._send_json(
                        {
                            "ok": True,
                            "vad_speculative": res_or_err,
                            "stats": _get_vad_speculative_stats_snapshot(),
                        }
                    )
                else:
                    self._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus.BAD_REQUEST)
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/asr-corrections":
            try:
                body = self._read_body_json()
                ok_ac, res_or_err = _update_asr_corrections_config(body)
                if ok_ac:
                    self._send_json({"ok": True, "asr_corrections": res_or_err})
                else:
                    self._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus.BAD_REQUEST)
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/relationship":
            try:
                body = self._read_body_json()
                ok, result = _update_anima_relationship(body)
                if ok:
                    self._send_json({"ok": True, "relationship": result})
                else:
                    self._send_json({"ok": False, "error": str(result)}, HTTPStatus.BAD_GATEWAY)
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/obsidian/config":
            try:
                body = self._read_body_json()
                ok, result = _update_obsidian_config(body)
                if ok:
                    self._send_json({"ok": True, "obsidian": result})
                else:
                    status = HTTPStatus.BAD_REQUEST if "enabled" in str(result) else HTTPStatus.BAD_GATEWAY
                    err_payload = {"ok": False, "error": str(result)}
                    if status == HTTPStatus.BAD_GATEWAY:
                        err_payload["obsidian"] = _get_cached_obsidian_snapshot()
                    self._send_json(err_payload, status)
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/obsidian/rebuild":
            try:
                self._read_body_json()
                ok, result = _trigger_obsidian_rebuild()
                if ok and isinstance(result, dict):
                    self._send_json(
                        {
                            "ok": True,
                            "obsidian": result.get("obsidian"),
                            "result": result.get("result"),
                        }
                    )
                else:
                    self._send_json(
                        {"ok": False, "error": str(result), "obsidian": _get_cached_obsidian_snapshot()},
                        HTTPStatus.BAD_GATEWAY,
                    )
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/l3-backend":
            try:
                body = self._read_body_json()
                ok, result = _update_l3_backend_config(body)
                if ok:
                    self._send_json({"ok": True, "l3_backend": result})
                else:
                    self._send_json({"ok": False, "error": str(result)}, HTTPStatus.BAD_REQUEST)
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/audio-test":
            try:
                body = self._read_body_json()
                vol = body.get("volume", None)
                ref_wav = os.path.join(VOICES_DIR, "raiden_mei", "mei_gentle_ref.wav")
                if not os.path.isfile(ref_wav):
                    ref_wav = os.path.join(VOICES_DIR, "wakeup_words_short.wav")
                if not os.path.isfile(ref_wav):
                    ref_wav = os.path.join(DATA_DIR, "wakeup_words_short.wav")
                if os.path.isfile(ref_wav):
                    with open(ref_wav, "rb") as f:
                        wav_bytes = f.read()
                else:
                    wav_bytes = _generate_test_chime_wav(0.4)
                ok, detail = pc_audio_router.play_wav_on_pc_sync(wav_bytes, volume=vol)
                if ok:
                    self._send_json({"ok": True})
                else:
                    self._send_json({"ok": False, "error": f"电脑音响桥接不可用: {detail}"}, HTTPStatus.BAD_GATEWAY)
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/config":
            try:
                body = self._read_body_json()
                has_prompt = "prompt" in body
                raw_prompt = body.get("prompt")
                if has_prompt and not isinstance(raw_prompt, str):
                    self._send_json({"ok": False, "error": "字段 prompt 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                    return
                prompt = raw_prompt.strip() if has_prompt else None

                llm_provider: str | None = None
                if "llm_provider" in body and body.get("llm_provider") is not None:
                    raw_prov = body.get("llm_provider")
                    if not isinstance(raw_prov, str):
                        self._send_json({"ok": False, "error": "字段 llm_provider 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                        return
                    llm_provider = raw_prov.strip()

                model_name: str | None = None
                if "model_name" in body and body.get("model_name") is not None:
                    raw_model = body.get("model_name")
                    if not isinstance(raw_model, str):
                        self._send_json({"ok": False, "error": "字段 model_name 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                        return
                    model_name = raw_model.strip()

                voice: str | None = None
                if "voice" in body and body.get("voice") is not None:
                    raw_voice = body.get("voice")
                    if not isinstance(raw_voice, str):
                        self._send_json({"ok": False, "error": "字段 voice 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                        return
                    voice = raw_voice.strip()

                force_restart = bool(body.get("force_restart", False))
                allow_restart = bool(body.get("restart", True))

                if has_prompt and not prompt:
                    self._send_json({"ok": False, "error": "角色提示词不能为空"}, HTTPStatus.BAD_REQUEST)
                    return

                write_res = _write_updated_yaml(
                    prompt=prompt,
                    llm_provider=llm_provider,
                    model_name=model_name,
                    voice=voice,
                )
                changed_fields = list(write_res.get("changed_fields") or [])
                voice_recovered = bool(write_res.get("voice_recovered", False))
                hot_ok, hot_detail = _hot_reload_runtime_config(changed_fields)
                if voice_recovered and hot_detail == "no_changes":
                    hot_detail = "voice_weights_recovered"
                will_restart = bool(force_restart or (allow_restart and not hot_ok))

                dirty_after, _ = _get_voice_dirty_state()
                self._send_json(
                    {
                        "ok": True,
                        "restarting": will_restart,
                        "hot_reloaded": bool(hot_ok and not will_restart),
                        "changed_fields": changed_fields,
                        "voice_recovered": voice_recovered,
                        "voice_dirty": bool(dirty_after),
                        "detail": hot_detail,
                    }
                )

                if will_restart:
                    def _delayed_restart():
                        try:
                            _flush_console_state_on_shutdown()
                        except Exception as exc:
                            _log_rate_limited("delayed_restart_flush_console", exc)
                        try:
                            import anima_memory_provider

                            anima_memory_provider.flush_outbox(timeout=3.5)
                        except Exception as exc:
                            _log_rate_limited("delayed_restart_flush_outbox", exc)
                        time.sleep(0.5)
                        os._exit(0)

                    threading.Thread(target=_delayed_restart, daemon=True).start()
            except _BadRequestError:
                raise
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/clear-history":
            self._read_body_json()
            with _state_lock:
                _chat_messages.clear()
                _save_history()
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/tts-preview":
            try:
                body = self._read_body_json()
                raw_voice = body.get("voice", "__gsv_raiden_mei__")
                if raw_voice is not None and not isinstance(raw_voice, str):
                    self._send_json({"ok": False, "error": "字段 voice 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                    return
                voice = (raw_voice or "__gsv_raiden_mei__").strip()
                if voice not in ALLOWED_VOICES:
                    self._send_json(
                        {"ok": False, "error": f"无效的试听音色 ID: {voice or '(空)'}"},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                raw_text = str(body.get("text") or "嗯，哥哥欢迎回来，今天的工作辛苦了，我已经为你泡好了热茶。").strip()
                req_emotion = str(body.get("emotion") or "auto").strip().lower()
                tag_emo, stripped_text = mei_emotion_router.extract_and_strip_emotion(
                    raw_text, flush_incomplete=True
                )
                clean_text = mei_emotion_router.clean_segment_for_tts(stripped_text) or "欢迎回来，今天辛苦了。"
                if voice in GSV_PROFILES:
                    configured_gsv = _get_configured_gsv_voice()
                    needs_weight_swap = bool(configured_gsv and configured_gsv != voice)
                    # P1-14: Refuse swapping GPU weights to a non-active voice while ESP32 is busy
                    if needs_weight_swap and _is_device_busy():
                        self._send_json(
                            {
                                "ok": False,
                                "busy": True,
                                "error": "ESP32 设备正在语音播报或合成中，为避免抢占 GPU 显存权重，已拒绝试听非当前音色，请稍后再试。",
                            },
                            HTTPStatus.CONFLICT,
                        )
                        return

                    lock_timeout = 0.25 if needs_weight_swap else 10.0
                    acquired = _gsv_weights_lock.acquire(timeout=lock_timeout)
                    if not acquired:
                        self._send_json(
                            {
                                "ok": False,
                                "busy": True,
                                "error": "本地 GPU 语音合成引擎正忙，请等待当前播报结束后再试听。",
                            },
                            HTTPStatus.CONFLICT,
                        )
                        return
                    rollback_warning = ""
                    total_cap = max(0.01, float(_GSV_PREVIEW_LOCK_MAX_SECONDS))
                    lock_deadline = time.monotonic() + total_cap
                    # Reserve rollback budget INSIDE total_cap when swapping voices so GPU lock hold time NEVER exceeds total_cap (D-3)
                    rollback_reserve = min(12.0, total_cap * 0.30) if needs_weight_swap else 0.0
                    _gsv_op_deadline_tls.deadline = lock_deadline - rollback_reserve
                    try:
                        # Re-check busy status after acquiring shared GPU lock
                        if needs_weight_swap and _is_device_busy():
                            self._send_json(
                                {
                                    "ok": False,
                                    "busy": True,
                                    "error": "ESP32 设备正在语音播报或合成中，已取消非当前音色试听。",
                                },
                                HTTPStatus.CONFLICT,
                            )
                            return
                        try:
                            _ensure_gsv_weights(voice, persist_boot_yaml=False)
                            req_payload = dict(GSV_PROFILES[voice]["block"])
                            if isinstance(req_payload.get("ref_audio_path"), str):
                                req_payload["ref_audio_path"] = req_payload["ref_audio_path"].replace(
                                    "${HOST_VOICES_DIR}", HOST_VOICES_DIR
                                ).replace("${HOST_DATA_DIR}", HOST_DATA_DIR)
                            if isinstance(req_payload.get("aux_ref_audio_paths"), list):
                                req_payload["aux_ref_audio_paths"] = [
                                    str(p).replace("${HOST_VOICES_DIR}", HOST_VOICES_DIR).replace(
                                        "${HOST_DATA_DIR}", HOST_DATA_DIR
                                    )
                                    for p in req_payload["aux_ref_audio_paths"]
                                ]
                            req_payload["text"] = clean_text[:120]
                            req_payload["text_split_method"] = "cut0"
                            if voice == "__gsv_raiden_mei__":
                                explicit_emo = None
                                if req_emotion and req_emotion != "auto":
                                    norm_emo, _ = mei_emotion_router.normalize_emotion_name(req_emotion)
                                    explicit_emo = norm_emo or req_emotion
                                elif tag_emo:
                                    explicit_emo = tag_emo
                                emo_key = mei_emotion_router.resolve_mei_emotion(
                                    clean_text,
                                    explicit_segment_emotion=explicit_emo,
                                )
                                emo_prof = mei_emotion_router.get_mei_emotion_profile(emo_key)
                                req_payload["ref_audio_path"] = emo_prof["ref_audio_path"]
                                req_payload["aux_ref_audio_paths"] = []
                                req_payload["prompt_text"] = emo_prof["prompt_text"]
                                req_payload["prompt_lang"] = emo_prof.get("prompt_lang", "zh")
                                req_payload["speed_factor"] = emo_prof["speed_factor"]
                                req_payload["temperature"] = emo_prof["temperature"]
                                req_payload["top_k"] = emo_prof["top_k"]
                                req_payload["top_p"] = emo_prof["top_p"]
                            for drop_k in ("type", "url", "output_dir", "voice_id"):
                                req_payload.pop(drop_k, None)
                            tts_timeout = _gsv_timeout_within_budget(25.0)
                            client = _get_console_http_client()
                            r = client.post(f"{GSV_Host}/tts", json=req_payload, timeout=tts_timeout)
                            if r.status_code != 200:
                                raise RuntimeError(f"本地 GPT-SoVITS 未就绪或返回异常 ({r.status_code})")
                            wav_bytes = r.content
                        finally:
                            # Restore live configured ESP32 GSV weights if another voice was previewed OR weights became dirty (D-3)
                            global _active_gsv_loaded
                            should_restore = bool(
                                configured_gsv
                                and (configured_gsv != voice or _active_gsv_loaded == "__dirty__")
                            )
                            if should_restore:
                                # Strictly respect original lock_deadline — NEVER extend beyond _GSV_PREVIEW_LOCK_MAX_SECONDS
                                _gsv_op_deadline_tls.deadline = lock_deadline
                                try:
                                    if time.monotonic() >= lock_deadline:
                                        raise TimeoutError("GPU 锁持有时间已达上限，跳过同步回滚，由后台自动恢复")
                                    _ensure_gsv_weights(configured_gsv, persist_boot_yaml=False)
                                    cur_dirty, _ = _get_voice_dirty_state()
                                    if cur_dirty == configured_gsv:
                                        _set_voice_dirty(None, "")
                                except Exception as rb_exc:
                                    _active_gsv_loaded = "__dirty__"
                                    _set_voice_dirty(configured_gsv, str(rb_exc))
                                    redacted_rb = _redact_secrets(str(rb_exc))
                                    rollback_warning = (
                                        f"试听后恢复线上音色权重 ({configured_gsv}) 失败，后台将自动重试：{redacted_rb}"
                                    )
                                    warn_log = f"[WebConsole] WARNING: {rollback_warning}"
                                    print(warn_log, flush=True)
                                    with _state_lock:
                                        _append_raw_log_locked(warn_log)
                    finally:
                        _gsv_op_deadline_tls.deadline = None
                        _gsv_weights_lock.release()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Cache-Control", "no-store")
                    if rollback_warning:
                        self.send_header("X-Voice-Dirty", "1")
                        self.send_header("X-Preview-Warning", quote(rollback_warning, safe=""))
                    self.send_header("Content-Length", str(len(wav_bytes)))
                    self.end_headers()
                    self.wfile.write(wav_bytes)
                else:
                    try:
                        mp3_bytes = asyncio.run(
                            _synthesize_mp3_with_timeout(
                                text=clean_text[:120],
                                voice=voice,
                                timeout=EDGE_PREVIEW_TIMEOUT_SECONDS,
                            )
                        )
                    except asyncio.TimeoutError:
                        self._send_json(
                            {
                                "ok": False,
                                "error": f"Edge 语音试听超时（超过 {int(EDGE_PREVIEW_TIMEOUT_SECONDS)} 秒），请稍后重试。",
                            },
                            HTTPStatus.GATEWAY_TIMEOUT,
                        )
                        return
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(mp3_bytes)))
                    self.end_headers()
                    self.wfile.write(mp3_bytes)
            except _BadRequestError:
                raise
            except TimeoutError as e:
                err_payload = {"ok": False, "error": _redact_secrets(str(e))}
                dirty_v, dirty_e = _get_voice_dirty_state()
                if dirty_v:
                    err_payload["voice_dirty"] = True
                    err_payload["rollback_warning"] = f"线上音色恢复失败，后台将自动重试：{dirty_e}"
                self._send_json(err_payload, HTTPStatus.GATEWAY_TIMEOUT)
            except Exception as e:
                err_payload = {"ok": False, "error": _redact_secrets(f"音色试听合成失败: {e}")}
                dirty_v, dirty_e = _get_voice_dirty_state()
                if dirty_v:
                    err_payload["voice_dirty"] = True
                    err_payload["rollback_warning"] = f"线上音色恢复失败，后台将自动重试：{dirty_e}"
                self._send_json(err_payload, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


def _flush_console_state_on_shutdown() -> None:
    """Flush coalesced dirty history and throttled log cursor on clean process shutdown (D-1 / D-4)."""
    try:
        flush_history_if_dirty()
    except Exception as exc:
        _log_rate_limited("shutdown_flush_history", exc)
    try:
        flush_log_cursor_if_dirty()
    except Exception as exc:
        _log_rate_limited("shutdown_flush_cursor", exc)


atexit.register(_flush_console_state_on_shutdown)


def start_console_background():
    _load_history()
    runtime_events.subscribe(_apply_runtime_event)
    _sync_runtime_events()
    start_history_flusher()
    watcher = threading.Thread(target=_log_watcher_loop, name="console-log-watcher", daemon=True)
    watcher.start()
    prober = threading.Thread(target=_service_probe_loop, name="console-service-prober", daemon=True)
    prober.start()

    def _boot_sync():
        time.sleep(2.0)
        cfg = _read_raw_yaml()
        if cfg.get("prompt"):
            _sync_persona_to_anima(str(cfg["prompt"]))

    threading.Thread(target=_boot_sync, name="anima-persona-sync", daemon=True).start()

    def _serve():
        httpd = ThreadingHTTPServer(("0.0.0.0", CONSOLE_PORT), ConsoleHandler)
        print(f"[WebConsole] 本地可视化控制台已启动: http://localhost:{CONSOLE_PORT}", flush=True)
        httpd.serve_forever()

    srv_thread = threading.Thread(target=_serve, name="console-http-server", daemon=True)
    srv_thread.start()

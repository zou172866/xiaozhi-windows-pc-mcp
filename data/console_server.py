import asyncio
import io
import json
import math
import os
import re
import struct
import threading
import time
import wave
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import edge_tts
import httpx
import yaml

import mei_emotion_router
import pc_audio_router
import reply_length_router

ROOT_DIR = "/opt/xiaozhi-esp32-server"
DATA_DIR = os.path.join(ROOT_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, ".config.yaml")
HTML_PATH = os.path.join(DATA_DIR, "console.html")
HISTORY_PATH = os.path.join(DATA_DIR, "chat_history.json")
LOG_CURSOR_PATH = os.path.join(DATA_DIR, ".console_log_cursor.json")
MODE_PATH = os.path.join(DATA_DIR, "pc_control_mode.json")
AGY_HB_PATH = os.path.join(DATA_DIR, "agy_tasks", "worker_heartbeat.json")
LOG_PATH = os.path.join(ROOT_DIR, "tmp", "server.log")

CONSOLE_PORT = int(os.environ.get("CONSOLE_PORT", "18080"))
GSV_Host = "http://host.docker.internal:19880"
PC_MCP_HOST = os.environ.get("PC_AUDIO_BRIDGE_URL", "http://host.docker.internal:18900").rstrip("/")
GSV_INFER_YAML = os.path.join(DATA_DIR, "tts_infer_xiaozhi.yaml")
L3_CONFIG_PATH = os.path.join(DATA_DIR, "l3_backend_config.json")
HOST_DATA_DIR = (
    (os.environ.get("HOST_DATA_DIR") or "").replace("\\", "/").rstrip("/")
    or DATA_DIR
)
HOST_LAN_IP = (os.environ.get("HOST_LAN_IP") or "").strip() or "127.0.0.1"

GSV_PROFILES = {
    "__gsv_raiden_mei__": {
        "version": "v4",
        "gpt_weights": f"{HOST_DATA_DIR}/voices/raiden_mei/v4/芽衣/芽衣-e10.ckpt",
        "sovits_weights": f"{HOST_DATA_DIR}/voices/raiden_mei/v4/芽衣/芽衣_e10_s430_l32.pth",
        "block": {
            "type": "gpt_sovits_v2",
            "url": f"{GSV_Host}/tts",
            "output_dir": "tmp/",
            "text_lang": "zh",
            "ref_audio_path": "${HOST_DATA_DIR}/voices/raiden_mei/mei_default_ref.wav",
            "aux_ref_audio_paths": [],
            "prompt_text": "今夜月色很美，呵...适合与朋友小聚一番。",
            "prompt_lang": "zh",
            "top_k": 12,
            "top_p": 1,
            "temperature": 0.98,
            "text_split_method": "cut0",
            "batch_size": 1,
            "batch_threshold": 0.75,
            "split_bucket": True,
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
            "url": f"{GSV_Host}/tts",
            "output_dir": "tmp/",
            "text_lang": "zh",
            "ref_audio_path": "${HOST_DATA_DIR}/xiaozhi_taiwan_ref.wav",
            "aux_ref_audio_paths": ["${HOST_DATA_DIR}/xiaozhi_taiwan_aux.wav"],
            "prompt_text": "哈啰啊，我是小智啦，声音好听的台湾女孩一枚，超开心认识你耶，最近在忙啥？",
            "prompt_lang": "zh",
            "top_k": 12,
            "top_p": 1,
            "temperature": 0.98,
            "text_split_method": "cut0",
            "batch_size": 1,
            "batch_threshold": 0.75,
            "split_bucket": True,
            "return_fragment": False,
            "speed_factor": 1.04,
            "streaming_mode": False,
            "seed": -1,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
        },
    },
}
_gsv_weights_lock = threading.RLock()
_active_gsv_loaded = None


def _detect_boot_gsv_voice() -> str | None:
    if os.path.exists(GSV_INFER_YAML):
        try:
            with open(GSV_INFER_YAML, "r", encoding="utf-8") as rf:
                boot_cfg = yaml.safe_load(rf) or {}
            t2s = str((boot_cfg.get("custom") or {}).get("t2s_weights_path") or "")
            for vid, prof in GSV_PROFILES.items():
                if prof["gpt_weights"] == t2s:
                    return vid
        except Exception:
            pass
    return None


def _get_configured_gsv_voice() -> str | None:
    try:
        cfg = _read_raw_yaml()
        selected = cfg.get("selected_module", {})
        if selected.get("TTS") != "GPT_SOVITS_V2":
            return None
        ref_p = str((cfg.get("TTS", {}).get("GPT_SOVITS_V2") or {}).get("ref_audio_path") or "")
        return "__gsv_raiden_mei__" if "raiden_mei" in ref_p else "__gsv_xiaozhi_tw__"
    except Exception:
        return None


_SECRET_PATTERNS = (
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/=]{8,}", re.IGNORECASE), r"\1***"),
    (re.compile(r"(#token=|api_key=|authorization\s*[:=]\s*)[^\s&\"',;]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "sk-***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"), "AIza***"),
)


def _redact_secrets(text: str) -> str:
    s = str(text or "")
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s


def _atomic_write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
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
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def validate_console_request(method: str, headers: dict, port: int = CONSOLE_PORT) -> tuple[bool, int, str]:
    """Validate Host and browser CSRF guards (Origin / Referer / Sec-Fetch-Site / Content-Type) on 18080."""
    norm_headers = {str(k).lower(): str(v).strip() for k, v in (headers or {}).items()}
    allowed_hosts = {
        f"localhost:{port}",
        f"127.0.0.1:{port}",
        f"[::1]:{port}",
        f"xiaozhi-esp32-server:{port}",
        "localhost",
        "127.0.0.1",
        "[::1]",
        "xiaozhi-esp32-server",
    }
    allowed_origins = {
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
        f"http://[::1]:{port}",
    }

    host = norm_headers.get("host", "").lower()
    if not host or host not in allowed_hosts:
        return False, int(HTTPStatus.MISDIRECTED_REQUEST), "Invalid Host header"

    if str(method).upper() == "POST":
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
            r1 = client.get(f"{GSV_Host}/set_gpt_weights", params={"weights_path": prof["gpt_weights"]}, timeout=25.0)
            if r1.status_code != 200:
                raise RuntimeError(f"切换 GPT 权重失败 (HTTP {r1.status_code}): {r1.text[:160]}")
            r2 = client.get(f"{GSV_Host}/set_sovits_weights", params={"weights_path": prof["sovits_weights"]}, timeout=30.0)
            if r2.status_code != 200:
                raise RuntimeError(f"切换 SoVITS 权重失败 (HTTP {r2.status_code}): {r2.text[:160]}")
            _active_gsv_loaded = voice_id
        if persist_boot_yaml:
            boot_cfg = {}
            if os.path.exists(GSV_INFER_YAML):
                try:
                    with open(GSV_INFER_YAML, "r", encoding="utf-8") as rf:
                        boot_cfg = yaml.safe_load(rf) or {}
                except Exception:
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
_raw_logs = deque(maxlen=150)
_device_info = {
    "device_id": "",
    "last_seen": "",
    "activity": "idle",
    "server_ready": False,
}
_current_ai_turn_open = False
_log_offset = 0


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


def _load_history():
    global _chat_messages
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                items = json.load(f)
                if isinstance(items, list):
                    _chat_messages = _dedup_chat_items(items)[-100:]
        except Exception:
            _chat_messages = []


def _save_history():
    try:
        _atomic_write_text(
            HISTORY_PATH,
            json.dumps(_chat_messages[-100:], ensure_ascii=False, indent=2),
        )
    except Exception:
        pass


def _read_pc_mode() -> str:
    try:
        if os.path.exists(MODE_PATH):
            with open(MODE_PATH, "r", encoding="utf-8") as f:
                m = json.load(f).get("mode", "safe")
                if m in ("off", "safe", "full"):
                    return m
    except Exception:
        pass
    return "safe"


def _write_pc_mode(mode: str):
    _atomic_write_text(MODE_PATH, json.dumps({"mode": mode}, ensure_ascii=False, indent=2))


def _check_agy_bridge_online() -> bool:
    try:
        if os.path.exists(AGY_HB_PATH):
            with open(AGY_HB_PATH, "r", encoding="utf-8") as f:
                hb = json.load(f)
            return (time.time() - float(hb.get("ts", 0))) < 15.0
    except Exception:
        pass
    return False


_l3_cache_lock = threading.Lock()
_l3_cache: dict | None = None
_l3_cache_ts = 0.0
_L3_CACHE_TTL = 3.0


def _read_local_l3_fallback() -> dict:
    cfg = {
        "backend": (os.environ.get("L3_BACKEND") or "antigravity").strip().lower(),
        "workdir": "",
        "dsh_cmd": "",
        "workbuddy_cmd": "",
        "workbuddy_auto_approve": True,
        "timeout_seconds": 300,
    }
    if os.path.isfile(L3_CONFIG_PATH):
        try:
            raw = json.loads(open(L3_CONFIG_PATH, "r", encoding="utf-8").read())
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception:
            pass
    agy_on = _check_agy_bridge_online()
    b = cfg["backend"]
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
        "ready": agy_on if b == "antigravity" else (b == "none"),
        "detail": (
            "宿主机 18900 MCP 离线（已读取本地配置）"
            if b != "none"
            else "已关闭 L3 后台 Agent（仅保留 L1/L2）"
        ),
        "workdir": cfg.get("workdir", ""),
        "effective_workdir": cfg.get("workdir", ""),
        "dsh_cmd": cfg.get("dsh_cmd", ""),
        "workbuddy_cmd": cfg.get("workbuddy_cmd", ""),
        "workbuddy_auto_approve": bool(cfg.get("workbuddy_auto_approve", True)),
        "timeout_seconds": int(cfg.get("timeout_seconds", 300)),
        "detected": {
            "antigravity": {"ready": agy_on, "detail": "伴生桥接在线" if agy_on else "待开启 Antigravity 伴生桥接"},
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
    except Exception:
        pass
    fb = _read_local_l3_fallback()
    with _l3_cache_lock:
        _l3_cache = dict(fb)
        _l3_cache_ts = time.monotonic()
    return fb


def _update_l3_backend_config(payload: dict) -> tuple[bool, dict | str]:
    global _l3_cache, _l3_cache_ts
    try:
        client = _get_console_http_client()
        resp = client.post(
            f"{PC_MCP_HOST}/l3/config",
            headers=pc_audio_router.get_pc_mcp_auth_headers(),
            json=payload or {},
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
    except Exception:
        # Offline fallback: persist directly to shared data/l3_backend_config.json so next 18900 start picks it up
        try:
            cur = _read_local_l3_fallback()
            new_cfg = {
                "backend": str(payload.get("backend") or cur["backend"]).strip().lower(),
                "workdir": str(payload.get("workdir", cur["workdir"]) or "").strip(),
                "dsh_cmd": str(payload.get("dsh_cmd", cur["dsh_cmd"]) or "").strip(),
                "workbuddy_cmd": str(payload.get("workbuddy_cmd", cur["workbuddy_cmd"]) or "").strip(),
                "workbuddy_auto_approve": bool(
                    payload.get("workbuddy_auto_approve", cur["workbuddy_auto_approve"])
                ),
                "timeout_seconds": int(payload.get("timeout_seconds", cur["timeout_seconds"])),
            }
            if new_cfg["backend"] not in ("antigravity", "dsh", "workbuddy", "none"):
                return False, "无效的 L3 后端名称"
            _atomic_write_text(L3_CONFIG_PATH, json.dumps(new_cfg, ensure_ascii=False, indent=2))
            fb = _read_local_l3_fallback()
            with _l3_cache_lock:
                _l3_cache = dict(fb)
                _l3_cache_ts = time.monotonic()
            return True, fb
        except Exception as exc:
            return False, str(exc)


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
    except Exception:
        pass
    return None, 0


def _save_log_cursor(inode: int, offset: int) -> None:
    try:
        _atomic_write_text(
            LOG_CURSOR_PATH,
            json.dumps({"inode": int(inode), "offset": int(offset)}),
        )
    except Exception:
        pass


def _process_log_line(line: str, *, status_only: bool = False):
    global _current_ai_turn_open
    s = line.strip()
    if not s:
        return

    with _state_lock:
        _raw_logs.append(_redact_secrets(s))

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


def _poll_log_once(watcher_state: dict) -> None:
    global _log_offset
    if not os.path.exists(LOG_PATH):
        return
    st = os.stat(LOG_PATH)
    is_boot_replay = False
    if not watcher_state.get("initialized"):
        watcher_state["initialized"] = True
        saved_ino = watcher_state.get("saved_inode")
        saved_off = int(watcher_state.get("saved_offset") or 0)
        if saved_ino == st.st_ino and 0 < saved_off <= st.st_size:
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

    if st.st_size > _log_offset:
        with open(LOG_PATH, "rb") as f:
            f.seek(_log_offset)
            raw = f.read()
        last_nl = raw.rfind(b"\n")
        if last_nl >= 0:
            to_consume = raw[: last_nl + 1]
            _log_offset += len(to_consume)
            lines = to_consume.decode("utf-8", errors="replace").splitlines()
            if is_boot_replay and _chat_messages:
                _replay_boot_lines_merged(lines)
            else:
                for ln in lines:
                    _process_log_line(ln, status_only=False)
                with _state_lock:
                    _trim_chat_messages()
            _save_log_cursor(st.st_ino, _log_offset)


def _log_watcher_loop():
    _load_history()
    saved_ino, saved_off = _load_log_cursor()
    watcher_state = {
        "initialized": False,
        "last_inode": None,
        "saved_inode": saved_ino,
        "saved_offset": saved_off,
    }
    while True:
        try:
            _poll_log_once(watcher_state)
        except Exception:
            pass
        time.sleep(0.4)


def _read_raw_yaml() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


ANIMA_HOST = "http://anima-brain:18787"
ANIMA_TOKEN_PATH = os.path.join(DATA_DIR, "anima", "studio_token.txt")
_anima_rel_lock = threading.Lock()
_anima_rel_cache: dict | None = None
_anima_rel_ts = 0.0
_ANIMA_REL_TTL = 3.0


def _read_anima_token() -> str:
    if not os.path.isfile(ANIMA_TOKEN_PATH):
        return ""
    try:
        with open(ANIMA_TOKEN_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _get_anima_character_id(cfg: dict | None = None) -> str:
    try:
        c = cfg if cfg is not None else _read_raw_yaml()
        cid = str(((c.get("Memory") or {}).get("AnimaBrain") or {}).get("character_id") or "").strip()
        if cid:
            return cid
    except Exception:
        pass
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
    except Exception:
        pass
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
    if not clean_prompt or not os.path.isfile(ANIMA_TOKEN_PATH):
        return
    try:
        with open(ANIMA_TOKEN_PATH, "r", encoding="utf-8") as f:
            token = f.read().strip()
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


def _write_updated_yaml(prompt: str, llm_provider: str, model_name: str, voice: str):
    cfg = _read_raw_yaml()
    cfg["prompt"] = prompt.strip() + "\n"

    selected = cfg.setdefault("selected_module", {})
    llm_section = cfg.setdefault("LLM", {})

    # Ensure both DeepSeekLLM and GeminiOpenAI blocks always remain intact
    ds_cfg = llm_section.setdefault("DeepSeekLLM", {})
    ds_cfg["type"] = "openai"
    ds_cfg["base_url"] = "https://api.deepseek.com"
    ds_cfg.setdefault("model_name", "deepseek-flash")
    ds_cfg["api_key"] = "${DEEPSEEK_API_KEY}"

    gem_cfg = llm_section.setdefault("GeminiOpenAI", {})
    gem_cfg["type"] = "openai"
    gem_cfg["base_url"] = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gem_cfg.setdefault("model_name", "gemini-3.5-flash-lite")
    gem_cfg["api_key"] = "${GEMINI_API_KEY}"

    if llm_provider == "GeminiOpenAI":
        selected["LLM"] = "GeminiOpenAI"
        gem_cfg["model_name"] = model_name.strip() or "gemini-3.5-flash-lite"
    else:
        selected["LLM"] = "DeepSeekLLM"
        ds_cfg["model_name"] = model_name.strip() or "deepseek-flash"

    tts_section = cfg.setdefault("TTS", {})
    v_id = voice.strip()
    if v_id in GSV_PROFILES:
        _ensure_gsv_weights(v_id, persist_boot_yaml=True)
        selected["TTS"] = "GPT_SOVITS_V2"
        tts_section["GPT_SOVITS_V2"] = dict(GSV_PROFILES[v_id]["block"])
    else:
        selected["TTS"] = "EdgeTTS"
        tts_cfg = tts_section.setdefault("EdgeTTS", {})
        tts_cfg.setdefault("type", "edge")
        tts_cfg["voice"] = v_id
        tts_cfg.setdefault("output_dir", "tmp/")

    header = (
        "# =====================================================================\n"
        "# 小智 ESP32 本地服务端精简配置（由本地 Web 控制台管理）\n"
        "# 自动读取 DEEPSEEK_API_KEY 与 GEMINI_API_KEY 环境变量，不落盘明文密钥\n"
        "# =====================================================================\n\n"
    )
    body = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
    _atomic_write_text(CONFIG_PATH, header + body)
    threading.Thread(target=_sync_persona_to_anima, args=(prompt,), daemon=True).start()


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


class ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, payload: dict, status=HTTPStatus.OK):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
        ok, status_code, err_msg = validate_console_request(method, hdrs, port=CONSOLE_PORT)
        if not ok:
            self._send_json({"ok": False, "error": err_msg}, HTTPStatus(status_code))
            return False
        return True

    def do_GET(self):
        if not self._enforce_security("GET"):
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            with _state_lock:
                ready = bool(_device_info.get("server_ready"))
            self._send_json({"ok": True, "server_ready": ready})
            return
        if parsed.path in ("/", "/index.html"):
            if not os.path.exists(HTML_PATH):
                self._send_json({"error": "console.html not found"}, HTTPStatus.NOT_FOUND)
                return
            with open(HTML_PATH, "rb") as f:
                html_bytes = f.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
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

            if tts_key == "GPT_SOVITS_V2":
                ref_p = str(tts_cfg.get("ref_audio_path", ""))
                active_voice = "__gsv_raiden_mei__" if "raiden_mei" in ref_p else "__gsv_xiaozhi_tw__"
            else:
                active_voice = tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural")

            with _state_lock:
                dev = dict(_device_info)
                msgs = list(_chat_messages[-60:])
                logs = list(_raw_logs)[-100:]

            audio_cfg = pc_audio_router.read_audio_output_config()
            reply_len_cfg = reply_length_router.read_reply_length_config()
            rel_cfg = _fetch_anima_relationship(force=False, cfg=cfg)
            l3_cfg = _fetch_l3_backend_status(force=False)

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
                        "websocket": ws_url,
                        "ota_url": ota_url,
                        "deepseek_key_ok": ds_ok,
                        "gemini_key_ok": gem_ok,
                        "pc_mode": _read_pc_mode(),
                        "audio_output": audio_cfg["target"],
                        "pc_audio_volume": audio_cfg["volume"],
                        "pc_audio_online": pc_audio_router.is_pc_audio_bridge_online(),
                        "reply_length": reply_len_cfg,
                        "relationship": rel_cfg,
                        "l3_backend": l3_cfg,
                        "agy_bridge_online": _check_agy_bridge_online(),
                        "mei_emotions": [
                            {"id": k, "label": v.get("label_zh", k)}
                            for k, v in mei_emotion_router.load_mei_emotions().items()
                        ],
                    },
                    "device": dev,
                    "messages": msgs,
                    "logs": logs,
                }
            )
            return

        if parsed.path == "/api/relationship":
            rel_cfg = _fetch_anima_relationship(force=True)
            self._send_json({"ok": bool(rel_cfg.get("online")), "relationship": rel_cfg})
            return

        if parsed.path == "/api/l3-backend":
            l3_cfg = _fetch_l3_backend_status(force=True)
            self._send_json({"ok": True, "l3_backend": l3_cfg})
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
                mode = str(body.get("mode") or "safe").strip()
                if mode not in ("off", "safe", "full"):
                    self._send_json({"ok": False, "error": "无效的权限挡位"}, HTTPStatus.BAD_REQUEST)
                    return
                _write_pc_mode(mode)
                self._send_json({"ok": True, "mode": mode})
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
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
                self._send_json(
                    {
                        "ok": True,
                        "target": saved["target"],
                        "volume": saved["volume"],
                        "pc_audio_online": pc_audio_router.is_pc_audio_bridge_online(force=True),
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
                ref_wav = os.path.join(DATA_DIR, "voices", "raiden_mei", "mei_gentle_ref.wav")
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
                prompt = str(body.get("prompt") or "").strip()
                llm_provider = str(body.get("llm_provider") or "DeepSeekLLM").strip()
                model_name = str(body.get("model_name") or "").strip()
                voice = str(body.get("voice") or "__gsv_raiden_mei__").strip()
                restart_now = bool(body.get("restart", True))

                if not prompt:
                    self._send_json({"ok": False, "error": "角色提示词不能为空"}, HTTPStatus.BAD_REQUEST)
                    return

                _write_updated_yaml(
                    prompt=prompt,
                    llm_provider=llm_provider,
                    model_name=model_name,
                    voice=voice,
                )
                self._send_json({"ok": True, "restarting": restart_now})

                if restart_now:
                    def _delayed_restart():
                        time.sleep(0.5)
                        os._exit(0)

                    threading.Thread(target=_delayed_restart, daemon=True).start()
            except _BadRequestError:
                raise
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
                voice = str(body.get("voice") or "__gsv_raiden_mei__").strip()
                raw_text = str(body.get("text") or "嗯，哥哥欢迎回来，今天的工作辛苦了，我已经为你泡好了热茶。").strip()
                req_emotion = str(body.get("emotion") or "auto").strip().lower()
                tag_emo, stripped_text = mei_emotion_router.extract_and_strip_emotion(
                    raw_text, flush_incomplete=True
                )
                clean_text = mei_emotion_router.clean_segment_for_tts(stripped_text) or "欢迎回来，今天辛苦了。"
                if voice in GSV_PROFILES:
                    configured_gsv = _get_configured_gsv_voice()
                    with _gsv_weights_lock:
                        _ensure_gsv_weights(voice, persist_boot_yaml=False)
                        try:
                            req_payload = dict(GSV_PROFILES[voice]["block"])
                            if isinstance(req_payload.get("ref_audio_path"), str):
                                req_payload["ref_audio_path"] = req_payload["ref_audio_path"].replace(
                                    "${HOST_DATA_DIR}", HOST_DATA_DIR
                                )
                            if isinstance(req_payload.get("aux_ref_audio_paths"), list):
                                req_payload["aux_ref_audio_paths"] = [
                                    str(p).replace("${HOST_DATA_DIR}", HOST_DATA_DIR)
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
                            for drop_k in ("type", "url", "output_dir"):
                                req_payload.pop(drop_k, None)
                            client = _get_console_http_client()
                            r = client.post(f"{GSV_Host}/tts", json=req_payload, timeout=25.0)
                            if r.status_code != 200:
                                raise RuntimeError(f"本地 GPT-SoVITS 未就绪或返回异常 ({r.status_code})")
                            wav_bytes = r.content
                        finally:
                            # Restore live configured ESP32 GSV weights if user only previewed another GSV voice
                            if configured_gsv and configured_gsv != voice:
                                try:
                                    _ensure_gsv_weights(configured_gsv, persist_boot_yaml=False)
                                except Exception:
                                    pass
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(wav_bytes)))
                    self.end_headers()
                    self.wfile.write(wav_bytes)
                else:
                    mp3_bytes = asyncio.run(_synthesize_mp3(text=clean_text[:120], voice=voice))
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(mp3_bytes)))
                    self.end_headers()
                    self.wfile.write(mp3_bytes)
            except _BadRequestError:
                raise
            except Exception as e:
                self._send_json({"ok": False, "error": f"音色试听合成失败: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


def start_console_background():
    watcher = threading.Thread(target=_log_watcher_loop, name="console-log-watcher", daemon=True)
    watcher.start()

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

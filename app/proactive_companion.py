"""Two-stage flow-aware proactive companion heartbeat engine (Phase 1 ②).

Zero import-time side effects: no background threads, no file I/O, no subscriptions until
`start_proactive_companion()` is explicitly called by `console.server.start_console_background()`.

Architecture:
- Stage 1 (Hard local gate, zero LLM tokens):
  1. Master toggle (`proactive.enabled`)
  2. Quiet hours (correctly handles cross-midnight windows like 23:00-08:00)
  3. Active voice/TTS/GSV busy check & `min_since_last_chat_minutes`
  4. Daily cap (`max_daily_nudges`) & exponential ignored-nudge cooldown backoff
     (`base * 2^min(consecutive_ignored, 3)`, capped at 12h; auto-reset when user speaks!)
  5. Host `18900 GET /desktop/rhythm` (2s timeout, fail-soft): blocks fullscreen,
     `game`/`meeting`, active PC audio playback, away (`idle > 1200s`), and active typing flow
     (`idle < 45s` unless `just_paused`, `returned_from_away`, or fresh L3 task completion).
- Stage 2 (Soft flow evaluation, compact LLM call with `suppress_llm_telemetry()`):
  - Fetches Anima Brain `POST /xiaozhi/proactive/context` + reads local weather cache (zero web calls)
  - Requests strict JSON (`HEARTBEAT_OK` or `SPEAK` with `<=45` chars + emotion tag)
- Stage 3 (Safe delivery):
  - `dry_run` (DEFAULT): persists to bounded `state/proactive_history.json` (max 50) & emits
    `runtime_events` without playing audio or consuming Anima `open_loop` follow-up quota.
  - `pc_speaker`: synthesizes with active voice (GSV short-timeout GPU lock yielding to live talk,
    cloud TTS, or EdgeTTS fallback), plays via `18900 /audio/play`, and acks Anima `open_loop_id`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import math
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.parse
import uuid

import httpx

import mei_emotion_router
import pc_audio_router
import recall_guard
import runtime_events
import runtime_settings
import turn_context_router
import vocal_cue_router
import xz_common

STATE_DIR = xz_common.STATE_DIR
HISTORY_PATH = os.path.join(STATE_DIR, "proactive_history.json")
MAX_HISTORY_ENTRIES = 50
MAX_BACKOFF_MINUTES = 720  # 12 hours cap
AWAY_IDLE_SECONDS = 1200.0
ACTIVE_TYPING_IDLE_SECONDS = 45.0
MAX_PROACTIVE_TEXT_CHARS = 45

ANIMA_HOST = os.environ.get("ANIMA_BRAIN_URL", "http://anima-brain:18787").rstrip("/")
PC_MCP_HOST = os.environ.get("PC_AUDIO_BRIDGE_URL", "http://host.docker.internal:18900").rstrip("/")
GSV_HOST = "http://host.docker.internal:19880"

_VALID_EMOTIONS = frozenset({"calm", "gentle", "happy", "playful", "shy", "serious", "tender"})
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK_RE = re.compile(r"<think>[\s\S]*$", re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_MD_DECOR_RE = re.compile(r"[*`#>\[\]]+")
_PARENS_ACTION_RE = re.compile(r"[（(][^（）()]{1,24}[）)]")
_HEARTBEAT_OK_MAX_COOLDOWN_MINUTES = 45
_LOCAL_LLM_HOSTS = frozenset({"host.docker.internal", "127.0.0.1", "localhost", "::1"})
_FALLBACK_SECRET_PATTERNS = (
    (re.compile(r"(Bearer[\s;]+)[A-Za-z0-9._\-~+/=]{6,}", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Basic\s+)[A-Za-z0-9+/=]{8,}", re.IGNORECASE), r"\1***"),
    (re.compile(r"((?<![A-Za-z0-9_])Token\s+)[A-Za-z0-9._\-~+/=]{6,}", re.IGNORECASE), r"\1***"),
    (
        re.compile(r"((?:https?|wss?|ftp)://)[^/\s:@\"']+:[^/\s@\"']+@", re.IGNORECASE),
        r"\1***:***@",
    ),
    (
        re.compile(r"((?:https?|wss?|ftp)://)[^/\s:@\"']+@", re.IGNORECASE),
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
            r"((?<![A-Za-z0-9_])(?:x-api-key|x-goog-api-key|x-api-access-key|x-api-app-key|x-console-token|api_key|api-key|apikey|access_token|refresh_token|auth_token|auth_key|token|secret|secret_key|client_secret|password|passwd|pwd|appid|group_id|pc_mcp_token|anima_studio_token|esp32_auth_key|deepseek_api_key|glm_api_key|gemini_api_key|siliconflow_api_key|openai_audio_api_key|dashscope_api_key|doubao_appid|doubao_asr_appid|doubao_tts_appid|doubao_access_token|doubao_asr_access_token|doubao_tts_access_token|fishspeech_api_key|minimax_api_key|minimax_group_id|authorization)(?![A-Za-z0-9_])\s*[\"']?\s*[:=]\s*[\"']?(?:(?:Bearer|Basic|Token)[\s;]+)?)(?!None\b|null\b|true\b|false\b)[^\s&\"',;)}\]]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_\-]{12,}(?![A-Za-z0-9_\-])"), "sk-***"),
    (re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_\-]{16,}(?![A-Za-z0-9_\-])"), "AIza***"),
    (re.compile(r"(?<![A-Za-z0-9_])ghp_[A-Za-z0-9]{16,}(?![A-Za-z0-9_])"), "ghp_***"),
    (re.compile(r"(?<![A-Za-z0-9_])xox[baprs]-[A-Za-z0-9\-]{10,}(?![A-Za-z0-9\-])"), "xox-***"),
)


def _is_local_endpoint(endpoint: str) -> bool:
    """Return True only when endpoint's parsed hostname matches an explicit local/loopback host."""
    try:
        parsed = urllib.parse.urlparse(str(endpoint or "").strip())
        if parsed.scheme.lower() not in ("http", "https"):
            return False
        host = (parsed.hostname or "").strip().lower()
    except ValueError:
        return False
    return host in _LOCAL_LLM_HOSTS


def _redact_error_text(raw: Any) -> str:
    """Redact API keys, Bearer tokens, and URL credentials from exception/error strings."""
    s = str(raw or "")
    if not s:
        return ""
    try:
        from console import shared

        return shared._redact_secrets(s)
    except Exception:
        for pat, repl in _FALLBACK_SECRET_PATTERNS:
            s = pat.sub(repl, s)
        return s


def _parse_hhmm_minutes(hhmm: str, default_minutes: int) -> int:
    s = str(hhmm or "").strip()
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", s)
    if not m:
        return default_minutes
    return int(m.group(1)) * 60 + int(m.group(2))


def is_in_quiet_hours(now_dt: datetime, quiet_start: str, quiet_end: str) -> bool:
    """Return True if `now_dt` falls inside `[quiet_start, quiet_end)` (supports cross-midnight)."""
    start_m = _parse_hhmm_minutes(quiet_start, 23 * 60)
    end_m = _parse_hhmm_minutes(quiet_end, 8 * 60)
    if start_m == end_m:
        return False
    cur_m = now_dt.hour * 60 + now_dt.minute
    if start_m < end_m:
        return start_m <= cur_m < end_m
    # Cross-midnight (e.g. 23:00 -> 08:00)
    return cur_m >= start_m or cur_m < end_m


def compute_effective_cooldown_minutes(min_cooldown_minutes: int, consecutive_ignored: int) -> int:
    """Exponential ignored-nudge backoff: `base * 2^min(ignored, 3)`, capped at 12 hours (720 min)."""
    base = max(15, int(min_cooldown_minutes or 90))
    ignored = max(0, int(consecutive_ignored or 0))
    return min(MAX_BACKOFF_MINUTES, base * (2 ** min(ignored, 3)))


def _expand_env_str(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    return _ENV_PLACEHOLDER_RE.sub(lambda m: os.environ.get(m.group(1), ""), s).strip()


def _strip_think_blocks(raw: str) -> str:
    s = _THINK_BLOCK_RE.sub("", str(raw or ""))
    return _UNCLOSED_THINK_RE.sub("", s)


def _run_coro_sync(coro):
    """Run an async coroutine safely whether or not the current thread already has a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result_box: Dict[str, Any] = {}
    err_box: Dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result_box["val"] = asyncio.run(coro)
        except BaseException as exc:
            err_box["exc"] = exc

    t = threading.Thread(target=_runner, name="proactive-async-bridge", daemon=True)
    t.start()
    t.join()
    if "exc" in err_box:
        raise err_box["exc"]
    return result_box.get("val")


def _trim_proactive_spoken_text(raw: str, max_chars: int = MAX_PROACTIVE_TEXT_CHARS) -> str:
    txt = _strip_think_blocks(str(raw or ""))
    txt = mei_emotion_router.strip_emotion_tags(txt)
    txt = _PARENS_ACTION_RE.sub("", txt)
    txt = _MD_DECOR_RE.sub("", txt)
    txt = " ".join(ln.strip() for ln in txt.splitlines() if ln.strip()).strip().strip("\"'「」『』")
    if not txt:
        return ""
    try:
        txt, _ = recall_guard.sanitize_recall_context(txt, record_stats=False)
    except Exception:
        pass
    if len(txt) > max_chars:
        cut = max(
            txt.rfind("。", 0, max_chars),
            txt.rfind("！", 0, max_chars),
            txt.rfind("？", 0, max_chars),
            txt.rfind("~", 0, max_chars),
        )
        if cut >= 10:
            txt = txt[: cut + 1]
        else:
            stem = txt[: max(1, max_chars - 1)].rstrip("，,、；;。！？!?. ")
            txt = (stem + "。") if stem else txt[:max_chars]
    return txt.strip()


def parse_proactive_llm_decision(
    raw_content: str,
    *,
    allowed_open_loop_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Defensively parse LLM JSON output into `{action, reason, emotion, text, open_loop_id}`."""
    raw = _strip_think_blocks(str(raw_content or "")).strip()
    if not raw or ("HEARTBEAT_OK" in raw.upper() and "{" not in raw):
        return {
            "action": "HEARTBEAT_OK",
            "reason": "llm_heartbeat_ok",
            "emotion": "calm",
            "text": "",
            "open_loop_id": "",
        }

    fenced = _JSON_FENCE_RE.search(raw)
    cand = fenced.group(1).strip() if fenced else raw
    parsed: Optional[dict] = None
    try:
        obj = json.loads(cand)
        if isinstance(obj, dict):
            parsed = obj
    except Exception:
        decoder = json.JSONDecoder()
        search_pos = 0
        while True:
            s_idx = cand.find("{", search_pos)
            if s_idx == -1:
                break
            try:
                obj, _ = decoder.raw_decode(cand, s_idx)
                if isinstance(obj, dict):
                    parsed = obj
                    break
            except Exception:
                pass
            search_pos = s_idx + 1

    if not parsed:
        return {
            "action": "HEARTBEAT_OK",
            "reason": "llm_unparseable_output",
            "emotion": "calm",
            "text": "",
            "open_loop_id": "",
        }

    act = str(parsed.get("action") or "HEARTBEAT_OK").strip().upper()
    reason = str(parsed.get("reason") or "").strip()[:120]
    if act != "SPEAK":
        return {
            "action": "HEARTBEAT_OK",
            "reason": reason or "llm_chose_silence",
            "emotion": "calm",
            "text": "",
            "open_loop_id": "",
        }

    raw_text = str(parsed.get("text") or "").strip()
    tag_emo, stripped = mei_emotion_router.extract_and_strip_emotion(raw_text, flush_incomplete=True)
    clean_text = _trim_proactive_spoken_text(stripped)
    if not clean_text or len(clean_text) < 2:
        return {
            "action": "HEARTBEAT_OK",
            "reason": "llm_empty_text",
            "emotion": "calm",
            "text": "",
            "open_loop_id": "",
        }

    emo_cand = str(parsed.get("emotion") or tag_emo or "gentle").strip().lower()
    norm_emo, _ = mei_emotion_router.normalize_emotion_name(emo_cand)
    emo = norm_emo if norm_emo in _VALID_EMOTIONS else (emo_cand if emo_cand in _VALID_EMOTIONS else "gentle")

    req_loop_id = str(parsed.get("open_loop_id") or "").strip()
    if allowed_open_loop_ids is not None and req_loop_id not in allowed_open_loop_ids:
        req_loop_id = ""

    return {
        "action": "SPEAK",
        "reason": reason or "proactive_care",
        "emotion": emo,
        "text": clean_text,
        "open_loop_id": req_loop_id,
    }


def _read_cached_weather_hint(cfg: Optional[dict] = None) -> str:
    """Read local in-process weather cache ONLY (never performs outbound internet requests)."""
    try:
        from core.utils.cache.manager import CacheType, cache_manager

        default_loc = "长沙"
        if isinstance(cfg, dict):
            default_loc = (
                str(((cfg.get("plugins") or {}).get("get_weather") or {}).get("default_location") or "").strip()
                or default_loc
            )
        for key in (f"nmc_weather_{default_loc}_zh_CN", f"nmc_weather_{default_loc}_zh"):
            val = cache_manager.get(CacheType.WEATHER, key)
            if isinstance(val, str) and val.strip():
                lines = [ln.strip() for ln in val.splitlines() if ln.strip()]
                return "；".join(lines[:2])[:100]
    except Exception:
        pass
    return ""


def _read_server_yaml() -> dict:
    try:
        from console import shared

        return shared._read_raw_yaml()
    except Exception:
        return {}


def _resolve_character_id(cfg: Optional[dict] = None) -> str:
    try:
        c = cfg if isinstance(cfg, dict) else _read_server_yaml()
        cid = str(((c.get("Memory") or {}).get("AnimaBrain") or {}).get("character_id") or "").strip()
        if cid:
            return cid
    except Exception:
        pass
    return "xiaozhi"


def _is_voice_pipeline_busy() -> bool:
    try:
        if runtime_events.is_any_device_speaking():
            return True
        snap = runtime_events.get_latest_runtime_snapshot()
        if snap.get("activity") in ("speaking", "listening"):
            return True
        for conn in runtime_events.get_active_connections():
            if getattr(conn, "client_have_voice", False) or getattr(conn, "client_is_speaking", False):
                return True
            if getattr(conn, "_pc_audio_active_sid", None) is not None:
                return True
    except Exception:
        pass
    try:
        if mei_emotion_router.is_live_gsv_busy():
            return True
    except Exception:
        pass
    return False


def _default_llm_call(system_prompt: str, user_prompt: str, cfg: dict) -> str:
    """Invoke the currently configured OpenAI-compatible LLM with telemetry suppressed."""
    selected = cfg.get("selected_module") or {}
    llm_key = str(selected.get("LLM") or "DeepSeekLLM").strip() or "DeepSeekLLM"
    llm_blocks = cfg.get("LLM") or {}
    blk = dict(llm_blocks.get(llm_key) or {})

    try:
        from console.routes import config as cfg_routes

        defaults = dict(cfg_routes._LLM_PROVIDER_DEFAULTS.get(llm_key) or {})
        for k, v in defaults.items():
            blk.setdefault(k, v)
    except Exception:
        pass

    base_url = _expand_env_str(blk.get("base_url") or blk.get("url") or "")
    model_name = _expand_env_str(blk.get("model_name") or "")
    api_key = _expand_env_str(blk.get("api_key") or "")
    endpoint = base_url.rstrip("/")
    if endpoint and not endpoint.endswith("/chat/completions"):
        endpoint = endpoint + "/chat/completions"
    is_local = _is_local_endpoint(endpoint)
    if not endpoint or not model_name or (not api_key and not is_local):
        raise RuntimeError(f"LLM ({llm_key}) 未配置完整的 base_url / model_name / api_key")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req_body: Dict[str, Any] = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "max_tokens": 300,
        "temperature": 0.7,
    }
    if llm_key == "GLMLLM":
        req_body["thinking"] = {"type": "disabled"}

    with turn_context_router.suppress_llm_telemetry():
        try:
            with httpx.Client(timeout=12.0, trust_env=not is_local, follow_redirects=False) as client:
                resp = client.post(endpoint, headers=headers, json=req_body)
        except ImportError:
            with httpx.Client(timeout=12.0, trust_env=False, follow_redirects=False) as client:
                resp = client.post(endpoint, headers=headers, json=req_body)
    if resp.status_code != 200:
        raise RuntimeError(f"LLM HTTP {resp.status_code}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    return str(msg.get("content") or "")


def _default_synthesize_wav(text: str, emotion: str, cfg: dict) -> Tuple[Optional[bytes], str]:
    """Synthesize proactive WAV using active TTS voice, yielding immediately if local GPU is busy."""
    from console import shared
    from console.routes import audio_api, gsv

    selected = cfg.get("selected_module") or {}
    tts_provider = str(selected.get("TTS") or "EdgeTTS").strip() or "EdgeTTS"
    clean_text = mei_emotion_router.clean_segment_for_tts(text)
    if not clean_text:
        return None, "empty_tts_text"

    # 1. Local GPU GPT-SoVITS V2: acquire GSV_GPU_LOCK with SHORT timeout (yield to real dialogue!)
    if tts_provider == "GPT_SOVITS_V2":
        existing_gsv_cfg = (cfg.get("TTS") or {}).get("GPT_SOVITS_V2") or {}
        voice_id = (
            (gsv._resolve_gsv_voice_id(existing_gsv_cfg) if existing_gsv_cfg else None)
            or gsv._get_configured_gsv_voice()
            or "__gsv_raiden_mei__"
        )
        if mei_emotion_router.is_live_gsv_busy() or runtime_events.is_any_device_speaking():
            return None, "gsv_gpu_busy"
        acquired = mei_emotion_router.GSV_GPU_LOCK.acquire(timeout=0.3)
        if not acquired:
            return None, "gsv_gpu_busy"
        try:
            if mei_emotion_router.is_live_gsv_busy() or runtime_events.is_any_device_speaking():
                return None, "gsv_gpu_busy"
            prof_block = dict((gsv.GSV_PROFILES.get(voice_id) or gsv.GSV_PROFILES["__gsv_raiden_mei__"])["block"])
            prof_block.update(gsv._voice_sampling_settings(existing_gsv_cfg, voice_id))
            if isinstance(prof_block.get("ref_audio_path"), str):
                prof_block["ref_audio_path"] = (
                    prof_block["ref_audio_path"]
                    .replace("${HOST_VOICES_DIR}", shared.HOST_VOICES_DIR)
                    .replace("${HOST_DATA_DIR}", shared.HOST_DATA_DIR)
                )
            if isinstance(prof_block.get("aux_ref_audio_paths"), list):
                prof_block["aux_ref_audio_paths"] = [
                    str(p)
                    .replace("${HOST_VOICES_DIR}", shared.HOST_VOICES_DIR)
                    .replace("${HOST_DATA_DIR}", shared.HOST_DATA_DIR)
                    for p in prof_block["aux_ref_audio_paths"]
                ]
            prof_block["text"] = clean_text[:80]
            prof_block["text_split_method"] = "cut0"
            emo_char = mei_emotion_router.resolve_emotion_character_key(voice_id=voice_id)
            if emo_char is not None:
                emo_key = mei_emotion_router.resolve_mei_emotion(
                    clean_text,
                    explicit_segment_emotion=emotion,
                    character_key=emo_char,
                )
                emo_prof = mei_emotion_router.get_character_emotion_profile(emo_char, emo_key)
                prof_block["ref_audio_path"] = emo_prof["ref_audio_path"]
                prof_block["aux_ref_audio_paths"] = []
                prof_block["prompt_text"] = emo_prof["prompt_text"]
                prof_block["prompt_lang"] = emo_prof.get("prompt_lang", "zh")
                prof_block["speed_factor"] = emo_prof["speed_factor"]
                prof_block["temperature"] = emo_prof["temperature"]
                prof_block["top_k"] = emo_prof["top_k"]
                prof_block["top_p"] = emo_prof["top_p"]
            for drop_k in ("type", "url", "output_dir", "voice_id", "first_segment_sample_steps"):
                prof_block.pop(drop_k, None)
            if prof_block.get("sample_steps") is None:
                prof_block.pop("sample_steps", None)
            client = shared._get_console_http_client()
            resp = client.post(f"{GSV_HOST}/tts", json=prof_block, timeout=12.0)
            if (
                resp.status_code == 200
                and mei_emotion_router.is_gsv_placeholder_zero_wav(resp.content)
                and prof_block.get("use_cuda_graph") is not False
            ):
                retry_block = dict(prof_block)
                retry_block["use_cuda_graph"] = False
                resp = client.post(f"{GSV_HOST}/tts", json=retry_block, timeout=12.0)
            if resp.status_code == 200 and resp.content and not mei_emotion_router.is_gsv_placeholder_zero_wav(resp.content):
                wav_bytes, _ = pc_audio_router.ensure_pcm_wav(resp.content, "wav")
                return wav_bytes, "gsv"
        except Exception:
            pass
        finally:
            mei_emotion_router.GSV_GPU_LOCK.release()
        # Fall back to EdgeTTS if local GPU 19880 is offline / failed

    # 2. Cloud TTS provider
    if tts_provider in audio_api.CLOUD_TTS_PROVIDERS:
        try:
            audio_bytes, ctype = audio_api.synthesize_cloud_tts_preview(
                tts_provider,
                clean_text[:80],
                raw_cfg=cfg,
            )
            ctype_low = str(ctype or "").lower()
            if "wav" in ctype_low:
                fmt = "wav"
            elif "ogg" in ctype_low or "opus" in ctype_low:
                fmt = "ogg"
            else:
                fmt = "mp3"
            wav_bytes, _ = pc_audio_router.ensure_pcm_wav(audio_bytes, fmt)
            return wav_bytes, f"cloud:{tts_provider}"
        except Exception:
            pass

    # 3. EdgeTTS (default or fallback)
    edge_cfg = (cfg.get("TTS") or {}).get("EdgeTTS") or {}
    edge_voice = str(edge_cfg.get("voice_id") or edge_cfg.get("voice") or "").strip()
    if edge_voice not in gsv.ALLOWED_EDGE_VOICES:
        edge_voice = "zh-CN-XiaoxiaoNeural"
    try:
        mp3_bytes = _run_coro_sync(
            gsv._synthesize_mp3_with_timeout(
                text=clean_text[:80],
                voice=edge_voice,
                timeout=10.0,
            )
        )
        wav_bytes, _ = pc_audio_router.ensure_pcm_wav(mp3_bytes, "mp3")
        return wav_bytes, f"edge:{edge_voice}"
    except Exception as exc:
        return None, f"tts_failed:{type(exc).__name__}"


class ProactiveCompanionEngine:
    """Two-stage proactive companion heartbeat manager with strict flow & quiet-hour gates."""

    def __init__(
        self,
        *,
        history_path: Optional[str] = None,
        settings_path: Optional[str] = None,
        now_fn: Optional[Callable[[], float]] = None,
        local_dt_fn: Optional[Callable[[float], datetime]] = None,
        rhythm_fetcher: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        anima_context_fetcher: Optional[Callable[[str, str], Optional[Dict[str, Any]]]] = None,
        anima_ack_sender: Optional[Callable[[str, str, str, str], bool]] = None,
        anima_inner_life_refresher: Optional[Callable[[str, str, bool], Optional[Dict[str, Any]]]] = None,
        llm_caller: Optional[Callable[[str, str, dict], str]] = None,
        tts_synthesizer: Optional[Callable[[str, str, dict], Tuple[Optional[bytes], str]]] = None,
        speaker_player: Optional[Callable[[bytes, float], Tuple[bool, str]]] = None,
        busy_checker: Optional[Callable[[], bool]] = None,
        config_reader: Optional[Callable[[], dict]] = None,
        weather_reader: Optional[Callable[[dict], str]] = None,
        mobile_sender: Optional[Callable[[str, str, str], Dict[str, Any]]] = None,
    ) -> None:
        self.history_path = os.path.abspath(history_path or HISTORY_PATH)
        self.settings_path = settings_path
        self._now_fn = now_fn or time.time
        self._local_dt_fn = local_dt_fn or (lambda ts: datetime.fromtimestamp(ts).astimezone())
        self._rhythm_fetcher = rhythm_fetcher
        self._anima_context_fetcher = anima_context_fetcher
        self._anima_ack_sender = anima_ack_sender
        self._anima_inner_life_refresher = anima_inner_life_refresher
        self._llm_caller = llm_caller or _default_llm_call
        self._tts_synthesizer = tts_synthesizer or _default_synthesize_wav
        self._speaker_player = speaker_player or (
            lambda wav, vol: pc_audio_router.play_wav_on_pc_sync(wav, volume=vol)
        )
        self._busy_checker = busy_checker or _is_voice_pipeline_busy
        self._config_reader = config_reader or _read_server_yaml
        self._weather_reader = weather_reader or _read_cached_weather_hint
        self._mobile_sender = mobile_sender

        self._lock = threading.RLock()
        self._eval_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._subscribed = False
        self._loaded_from_disk = False

        # Persisted runtime state
        self._daily_date: str = ""
        self._daily_spoke_count: int = 0
        self._last_spoke_ts: float = 0.0
        self._last_dry_run_ts: float = 0.0
        self._last_heartbeat_ok_ts: float = 0.0
        self._consecutive_ignored: int = 0
        self._last_user_chat_ts: float = 0.0
        self._last_l3_notified_id: str = ""
        self._last_check_ts: float = 0.0
        self._last_skip_reason: str = ""
        self._last_rhythm: Dict[str, Any] = {"online": False}
        self._last_inner_life: Dict[str, Any] = {}
        self._history: List[Dict[str, Any]] = []

    def _now(self) -> float:
        return float(self._now_fn())

    def _local_dt(self, ts: Optional[float] = None) -> datetime:
        return self._local_dt_fn(self._now() if ts is None else float(ts))

    def _get_proactive_cfg(self) -> Dict[str, Any]:
        return runtime_settings.get_proactive(self.settings_path)

    def _ensure_loaded_locked(self) -> None:
        if self._loaded_from_disk:
            return
        self._loaded_from_disk = True
        if not os.path.isfile(self.history_path):
            return
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            def _safe_ts(val: Any) -> float:
                if isinstance(val, bool):
                    return 0.0
                try:
                    fv = float(val or 0.0)
                except (TypeError, ValueError):
                    return 0.0
                return fv if math.isfinite(fv) and fv > 0.0 else 0.0

            self._daily_date = str(data.get("daily_date") or "")
            self._daily_spoke_count = max(0, int(data.get("daily_spoke_count") or 0))
            self._last_spoke_ts = _safe_ts(data.get("last_spoke_ts"))
            self._last_dry_run_ts = _safe_ts(data.get("last_dry_run_ts"))
            self._last_heartbeat_ok_ts = _safe_ts(data.get("last_heartbeat_ok_ts"))
            self._consecutive_ignored = max(0, int(data.get("consecutive_ignored") or 0))
            self._last_user_chat_ts = max(
                self._last_user_chat_ts, _safe_ts(data.get("last_user_chat_ts"))
            )
            self._last_l3_notified_id = str(data.get("last_l3_notified_id") or "")
            raw_hist = data.get("history")
            if isinstance(raw_hist, list):
                self._history = [dict(x) for x in raw_hist if isinstance(x, dict)][-MAX_HISTORY_ENTRIES:]
        except Exception:
            pass

    def _persist_locked(self) -> None:
        payload = {
            "daily_date": self._daily_date,
            "daily_spoke_count": self._daily_spoke_count,
            "last_spoke_ts": self._last_spoke_ts,
            "last_dry_run_ts": self._last_dry_run_ts,
            "last_heartbeat_ok_ts": self._last_heartbeat_ok_ts,
            "consecutive_ignored": self._consecutive_ignored,
            "last_user_chat_ts": self._last_user_chat_ts,
            "last_l3_notified_id": self._last_l3_notified_id,
            "history": self._history[-MAX_HISTORY_ENTRIES:],
        }
        try:
            xz_common.atomic_write_json(self.history_path, payload)
        except Exception:
            pass

    def _rollover_day_locked(self, today_str: str) -> None:
        if self._daily_date != today_str:
            self._daily_date = today_str
            self._daily_spoke_count = 0

    def _sync_anima_last_turn_locked(self, anima_ctx: Optional[Dict[str, Any]]) -> bool:
        """Sync Anima's `last_turn_at` to clear ignored-nudge backoff after cross-client chats.

        Returns True when `_consecutive_ignored` was reset from >0 to 0.
        """
        if not isinstance(anima_ctx, dict):
            return False
        anima_last_turn = anima_ctx.get("last_turn_at")
        if isinstance(anima_last_turn, bool) or not isinstance(anima_last_turn, (int, float)):
            return False
        turn_ts = float(anima_last_turn)
        if not math.isfinite(turn_ts) or turn_ts <= 0.0:
            return False

        self._ensure_loaded_locked()
        reset_backoff = False
        changed = False
        if turn_ts > self._last_user_chat_ts:
            self._last_user_chat_ts = turn_ts
            changed = True
        if self._consecutive_ignored > 0 and self._last_spoke_ts > 0.0 and turn_ts > self._last_spoke_ts:
            self._consecutive_ignored = 0
            reset_backoff = True
            changed = True
        if changed:
            self._persist_locked()
        return reset_backoff

    def on_runtime_event(self, evt: Dict[str, Any]) -> None:
        """Observe user speech events to track recency and reset ignored-nudge backoff."""
        if not isinstance(evt, dict):
            return
        etype = str(evt.get("type") or "")
        if etype in ("user_message", "listen_start"):
            now = self._now()
            with self._lock:
                self._ensure_loaded_locked()
                changed = self._consecutive_ignored > 0
                self._last_user_chat_ts = max(self._last_user_chat_ts, now)
                self._consecutive_ignored = 0
                if changed:
                    self._persist_locked()

    def wake(self) -> None:
        self._wake_event.set()

    def start(self) -> None:
        with self._lock:
            self._ensure_loaded_locked()
            if not self._subscribed:
                runtime_events.subscribe(self.on_runtime_event)
                self._subscribed = True
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
            t = threading.Thread(
                target=self._heartbeat_loop,
                name="proactive-companion-heartbeat",
                daemon=True,
            )
            t.start()
            self._thread = t

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            if self._subscribed:
                runtime_events.unsubscribe(self.on_runtime_event)
                self._subscribed = False
            t = self._thread
            self._thread = None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)

    def _heartbeat_loop(self) -> None:
        # Initial grace period after boot so server & Anima Brain finish initializing
        self._wake_event.wait(timeout=45.0)
        self._wake_event.clear()
        while not self._stop_event.is_set():
            try:
                cfg = self._get_proactive_cfg()
                interval_sec = max(60.0, float(cfg.get("check_interval_minutes", 15)) * 60.0)
                if cfg.get("enabled", False):
                    self.run_once(manual=False)
            except Exception:
                interval_sec = 900.0
            self._wake_event.wait(timeout=interval_sec)
            self._wake_event.clear()

    def _fetch_desktop_rhythm(self) -> Dict[str, Any]:
        if callable(self._rhythm_fetcher):
            res = self._rhythm_fetcher()
            return dict(res) if isinstance(res, dict) else {"online": False}
        try:
            with httpx.Client(trust_env=False, timeout=2.0) as client:
                resp = client.get(
                    f"{PC_MCP_HOST}/desktop/rhythm",
                    headers=pc_audio_router.get_pc_mcp_auth_headers(),
                )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    data["online"] = True
                    return data
        except Exception:
            pass
        return {"online": False}

    def _fetch_anima_context(
        self,
        character_id: str,
        weather_hint: str,
        *,
        ensure_inner_life: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if callable(self._anima_context_fetcher):
            res = self._anima_context_fetcher(character_id, weather_hint)
            return dict(res) if isinstance(res, dict) else None
        token = xz_common.get_anima_token()
        if not token:
            return None
        try:
            with httpx.Client(trust_env=False, timeout=3.0) as client:
                resp = client.post(
                    f"{ANIMA_HOST}/xiaozhi/proactive/context",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "character_id": character_id,
                        "weather_hint": weather_hint,
                        "ensure_inner_life": bool(ensure_inner_life),
                    },
                )
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, dict) else None
        except Exception:
            pass
        return None

    def _send_anima_ack(
        self,
        character_id: str,
        open_loop_id: str,
        action: str,
        event_id: str,
    ) -> bool:
        if callable(self._anima_ack_sender):
            return bool(self._anima_ack_sender(character_id, open_loop_id, action, event_id))
        if not open_loop_id:
            return True
        token = xz_common.get_anima_token()
        if not token:
            return False
        try:
            with httpx.Client(trust_env=False, timeout=3.0) as client:
                resp = client.post(
                    f"{ANIMA_HOST}/xiaozhi/proactive/ack",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "character_id": character_id,
                        "open_loop_id": open_loop_id,
                        "action": action,
                        "event_id": event_id,
                    },
                )
            return resp.status_code == 200
        except Exception:
            return False

    def refresh_inner_life_now(self, *, use_llm: bool = False) -> Dict[str, Any]:
        """Trigger Anima Brain to refresh today's character Inner Life micro-state."""
        server_cfg = self._config_reader()
        char_id = _resolve_character_id(server_cfg)
        weather_hint = self._weather_reader(server_cfg)

        if callable(self._anima_inner_life_refresher):
            try:
                res = self._anima_inner_life_refresher(char_id, weather_hint, bool(use_llm))
            except Exception as exc:
                return {"ok": False, "error": f"无法刷新角色日常: {_redact_error_text(exc)}"}
            if isinstance(res, dict):
                if res.get("ok") is False:
                    err_txt = _redact_error_text(res.get("error") or "")
                    return {"ok": False, "error": err_txt or "无法刷新角色日常"}
                il = dict(res.get("inner_life") or res)
                with self._lock:
                    self._last_inner_life = il
                return {"ok": True, "inner_life": il}
            return {"ok": False, "error": "无法刷新角色日常"}

        token = xz_common.get_anima_token()
        if not token:
            return {"ok": False, "error": "未找到 Anima Brain 访问令牌"}
        try:
            with httpx.Client(trust_env=False, timeout=8.0) as client:
                resp = client.post(
                    f"{ANIMA_HOST}/xiaozhi/inner-life/refresh",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "character_id": char_id,
                        "weather_hint": weather_hint,
                        "use_llm": bool(use_llm),
                    },
                )
            if resp.status_code == 200:
                data = resp.json()
                il = dict(data.get("inner_life") or data) if isinstance(data, dict) else {}
                with self._lock:
                    self._last_inner_life = il
                return {"ok": True, "inner_life": il}
            return {"ok": False, "error": f"Anima Brain 返回 HTTP {resp.status_code}"}
        except Exception as exc:
            return {"ok": False, "error": f"无法连接 Anima Brain (18787): {_redact_error_text(exc)}"}

    def evaluate_hard_gates(
        self,
        *,
        cfg: Dict[str, Any],
        now: float,
        now_dt: datetime,
        rhythm: Optional[Dict[str, Any]] = None,
        effective_mode: Optional[str] = None,
        ignore_backoff_multiplier: bool = False,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Stage 1 local hard gate (zero LLM calls). Returns `(allowed, reason, meta)`."""
        mode = str(effective_mode or cfg.get("delivery_mode") or "dry_run").strip().lower()
        today_str = now_dt.strftime("%Y-%m-%d")

        with self._lock:
            self._ensure_loaded_locked()
            self._rollover_day_locked(today_str)
            daily_count = self._daily_spoke_count
            last_spoke = self._last_spoke_ts
            last_dry = self._last_dry_run_ts
            last_hb_ok = self._last_heartbeat_ok_ts
            ignored = self._consecutive_ignored
            last_chat = self._last_user_chat_ts
            last_l3_id = self._last_l3_notified_id

        eff_cd_min = compute_effective_cooldown_minutes(
            int(cfg.get("min_cooldown_minutes", 90)),
            0 if ignore_backoff_multiplier else ignored,
        )
        meta: Dict[str, Any] = {
            "effective_cooldown_minutes": eff_cd_min,
            "daily_spoke_count": daily_count,
            "consecutive_ignored": ignored,
            "l3_finished": None,
        }

        # Gate 1: master switch
        if not cfg.get("enabled", False):
            return False, "disabled", meta

        # Gate 2: quiet hours
        if is_in_quiet_hours(now_dt, str(cfg.get("quiet_start", "23:00")), str(cfg.get("quiet_end", "08:00"))):
            return False, "quiet_hours", meta

        # Gate 3: active voice / TTS / GSV busy & min_since_last_chat
        if self._busy_checker():
            return False, "voice_busy", meta

        min_chat_sec = max(5, int(cfg.get("min_since_last_chat_minutes", 45))) * 60.0
        if last_chat > 0.0 and (now - last_chat) < min_chat_sec:
            return False, "recent_chat_active", meta

        # Gate 4: daily limit & ignored-nudge exponential backoff
        max_daily = max(1, int(cfg.get("max_daily_nudges", 4)))
        if mode == "pc_speaker":
            if daily_count >= max_daily:
                return False, "daily_limit_reached", meta
            if last_spoke > 0.0 and (now - last_spoke) < (eff_cd_min * 60.0):
                return False, "cooldown_backoff", meta
        else:
            # In dry_run background ticks, still space evaluations by base cooldown so logs stay calm
            base_cd_sec = max(15, int(cfg.get("min_cooldown_minutes", 90))) * 60.0
            if last_dry > 0.0 and (now - last_dry) < base_cd_sec:
                return False, "dry_run_cooldown", meta

        # Gate 5: desktop rhythm flow-state check
        rhy = rhythm if isinstance(rhythm, dict) else {"online": False}
        if not rhy.get("online", False):
            if mode == "pc_speaker":
                return False, "pc_bridge_offline", meta
            return True, "ok_rhythm_offline_dry_run", meta

        if rhy.get("is_fullscreen"):
            return False, "desktop_fullscreen", meta

        cat = str(rhy.get("activity_category") or "other").strip().lower()
        if cat in ("game", "meeting"):
            return False, f"desktop_focus_{cat}", meta

        if rhy.get("pc_audio_busy"):
            return False, "pc_audio_busy", meta

        idle_sec = float(rhy.get("idle_seconds", 0.0) or 0.0)
        if idle_sec > AWAY_IDLE_SECONDS:
            return False, "user_away_from_pc", meta

        l3_fin = None
        l3_st = rhy.get("l3_status")
        if isinstance(l3_st, dict) and cfg.get("allow_l3_finish_notify", True):
            cand_fin = l3_st.get("recently_finished")
            if isinstance(cand_fin, dict) and cand_fin.get("id"):
                fin_id = str(cand_fin.get("id"))
                age_s = float(cand_fin.get("age_seconds", 9999.0) or 9999.0)
                if fin_id != last_l3_id and age_s <= 900.0:
                    l3_fin = dict(cand_fin)
                    meta["l3_finished"] = l3_fin

        just_paused = bool(rhy.get("just_paused", False))
        returned_from_away = bool(rhy.get("returned_from_away", False))
        if (
            idle_sec < ACTIVE_TYPING_IDLE_SECONDS
            and not just_paused
            and not returned_from_away
            and l3_fin is None
        ):
            return False, "desktop_typing_flow", meta

        # Prevent repeated LLM token burn when LLM recently chose HEARTBEAT_OK (unless fresh L3 finished)
        if mode == "pc_speaker" and l3_fin is None and last_hb_ok > 0.0:
            hb_cd_min = min(
                _HEARTBEAT_OK_MAX_COOLDOWN_MINUTES,
                max(15, int(cfg.get("min_cooldown_minutes", 90))),
            )
            if (now - last_hb_ok) < (hb_cd_min * 60.0):
                return False, "heartbeat_ok_cooldown", meta

        return True, "ok", meta

    def _build_llm_prompts(
        self,
        *,
        server_cfg: dict,
        anima_ctx: Dict[str, Any],
        rhythm: Dict[str, Any],
        weather_hint: str,
        due_loops: List[Dict[str, Any]],
        l3_finished: Optional[Dict[str, Any]],
        now_dt: datetime,
    ) -> Tuple[str, str]:
        persona = str(server_cfg.get("prompt") or "").strip()
        if len(persona) > 700:
            persona = persona[:700]
        char_name = str(anima_ctx.get("character_name") or "小智")

        sys_prompt = (
            f"{persona}\n\n"
            f"【当前任务：低打扰主动陪伴决策】你是{char_name}。"
            "现在系统探测到一个可能适合轻声打招呼的空档。请保持极高克制：如果不适合开口或没必要打扰，"
            '务必输出 {"action":"HEARTBEAT_OK","reason":"..."} 保持安静；'
            "只有在确实自然、温暖、贴切时才输出 SPEAK。\n"
            "严格只输出一个 JSON 对象，不要 Markdown 代码块，不要多余解释：\n"
            '{"action":"HEARTBEAT_OK"|"SPEAK","reason":"简短原因",'
            '"emotion":"gentle|calm|happy|playful|shy|serious|tender",'
            '"text":"<=45字自然口语短句（无括号动作/无表情符号）","open_loop_id":"如跟进待办则填其memory_id否则留空"}'
        )

        mood = anima_ctx.get("mood") or {}
        inner_life = anima_ctx.get("inner_life") or {}
        lines = [
            f"当前本地时间：{now_dt.strftime('%Y-%m-%d %H:%M')}",
            f"情绪与关系：stage={mood.get('stage', 'familiar')}, mood={mood.get('last_emotion', 'calm')}",
        ]
        hrs = anima_ctx.get("hours_since_last_chat")
        if hrs is not None:
            lines.append(f"距上次对话：约 {hrs} 小时")
        if weather_hint:
            lines.append(f"本地缓存天气：{weather_hint}")
        if inner_life.get("text"):
            lines.append(f"你当下的微日常：{inner_life['text']}")

        if rhythm.get("online"):
            lines.append(
                "桌面节律快照："
                f"类别={rhythm.get('activity_category', 'other')}，"
                f"窗口={rhythm.get('safe_title', '常规窗口')}，"
                f"静默={int(float(rhythm.get('idle_seconds', 0)))}秒，"
                f"连续活跃={int(float(rhythm.get('active_streak_seconds', 0)) // 60)}分钟，"
                f"刚停顿={bool(rhythm.get('just_paused'))}，"
                f"离开后刚回桌={bool(rhythm.get('returned_from_away'))}"
            )
        else:
            lines.append("桌面节律快照：宿主机节律探针未在线（仅做试运行参考）")

        if l3_finished:
            lines.append(
                f"后台 L3 任务刚结束：id={l3_finished.get('id')} status={l3_finished.get('status')} "
                f"backend={l3_finished.get('backend')}"
            )

        if due_loops:
            lines.append("到期可顺带轻声关心的未完小事（最多选 1 条，切勿盘问）：")
            for lp in due_loops[:2]:
                ed = f" (日期:{lp['event_date']})" if lp.get("event_date") else ""
                lines.append(f"- memory_id={lp['memory_id']}: {lp['text']}{ed}")
        else:
            lines.append("到期未完小事：无")

        lines.append("请决定保持安静 (HEARTBEAT_OK) 还是说一句不超过 45 字的温柔短句 (SPEAK)。")
        return sys_prompt, "\n".join(lines)

    def _append_history_locked(self, entry: Dict[str, Any]) -> None:
        self._history.append(entry)
        if len(self._history) > MAX_HISTORY_ENTRIES:
            self._history = self._history[-MAX_HISTORY_ENTRIES:]
        self._persist_locked()

    def run_once(
        self,
        *,
        manual: bool = False,
        force_dry_run: bool = False,
        bypass_timing_gates: bool = False,
    ) -> Dict[str, Any]:
        """Run one full two-stage proactive companion evaluation cycle."""
        if not self._eval_lock.acquire(blocking=manual, timeout=5.0 if manual else -1):
            return {"ok": True, "outcome": "skipped", "reason": "evaluation_in_progress"}

        try:
            now = self._now()
            now_dt = self._local_dt(now)
            today_str = now_dt.strftime("%Y-%m-%d")
            cfg = self._get_proactive_cfg()
            effective_mode = "dry_run" if force_dry_run else str(cfg.get("delivery_mode") or "dry_run")
            if effective_mode not in runtime_settings.VALID_PROACTIVE_DELIVERY_MODES:
                effective_mode = "dry_run"

            # Safety invariant: timing gate bypass is ONLY permitted for explicit manual dry_run probes
            allow_bypass = bool(manual and force_dry_run and bypass_timing_gates and effective_mode == "dry_run")

            # Avoid unnecessary HTTP probes to host 18900 on background ticks when disabled/quiet/busy
            if not manual:
                pre_block = ""
                if not cfg.get("enabled", False):
                    pre_block = "disabled"
                elif is_in_quiet_hours(
                    now_dt,
                    str(cfg.get("quiet_start", "23:00")),
                    str(cfg.get("quiet_end", "08:00")),
                ):
                    pre_block = "quiet_hours"
                elif self._busy_checker():
                    pre_block = "voice_busy"
                if pre_block:
                    with self._lock:
                        self._ensure_loaded_locked()
                        self._rollover_day_locked(today_str)
                        self._last_check_ts = now
                        self._last_skip_reason = pre_block
                        cached_rhy = dict(self._last_rhythm)
                    return {
                        "ok": True,
                        "outcome": "skipped",
                        "stage": "hard_gate",
                        "reason": pre_block,
                        "delivery_mode": effective_mode,
                        "manual": manual,
                        "rhythm": cached_rhy,
                    }

            rhythm = self._fetch_desktop_rhythm()
            with self._lock:
                self._ensure_loaded_locked()
                self._rollover_day_locked(today_str)
                self._last_check_ts = now
                self._last_rhythm = dict(rhythm)

            # Stage 1: Hard local gate (zero LLM calls)
            allowed, gate_reason, gate_meta = self.evaluate_hard_gates(
                cfg=cfg,
                now=now,
                now_dt=now_dt,
                rhythm=rhythm,
                effective_mode=effective_mode,
            )

            # Cross-client / post-restart backoff check:
            # Only consult Anima's `last_turn_at` when the tick is past base cooldown AND
            # would otherwise pass all remaining local gates (Gate 5 desktop rhythm / heartbeat_ok)
            # — i.e., blocked SOLELY by `ignored > 0` backoff. Reused in Stage 2 if unblocked.
            anima_fetched = False
            server_cfg: dict = {}
            char_id = "xiaozhi"
            weather_hint = ""
            prefetched_anima_ctx: Optional[Dict[str, Any]] = None
            if (
                not allowed
                and gate_reason == "cooldown_backoff"
                and int(gate_meta.get("consecutive_ignored", 0)) > 0
            ):
                would_allow_base, _, _ = self.evaluate_hard_gates(
                    cfg=cfg,
                    now=now,
                    now_dt=now_dt,
                    rhythm=rhythm,
                    effective_mode=effective_mode,
                    ignore_backoff_multiplier=True,
                )
                if would_allow_base:
                    server_cfg = self._config_reader()
                    char_id = _resolve_character_id(server_cfg)
                    weather_hint = self._weather_reader(server_cfg)
                    prefetched_anima_ctx = self._fetch_anima_context(
                        char_id,
                        weather_hint,
                        ensure_inner_life=bool(cfg.get("inner_life_enabled", False)),
                    )
                    anima_fetched = True
                    if isinstance(prefetched_anima_ctx, dict):
                        with self._lock:
                            if (
                                isinstance(prefetched_anima_ctx.get("inner_life"), dict)
                                and prefetched_anima_ctx["inner_life"]
                            ):
                                self._last_inner_life = dict(prefetched_anima_ctx["inner_life"])
                            backoff_reset = self._sync_anima_last_turn_locked(prefetched_anima_ctx)
                        if backoff_reset:
                            allowed, gate_reason, gate_meta = self.evaluate_hard_gates(
                                cfg=cfg,
                                now=now,
                                now_dt=now_dt,
                                rhythm=rhythm,
                                effective_mode=effective_mode,
                            )

            would_block_reason = "" if allowed else gate_reason

            if not allowed and not allow_bypass:
                with self._lock:
                    self._last_skip_reason = gate_reason
                return {
                    "ok": True,
                    "outcome": "skipped",
                    "stage": "hard_gate",
                    "reason": gate_reason,
                    "delivery_mode": effective_mode,
                    "manual": manual,
                    "rhythm": rhythm,
                    **gate_meta,
                }

            # Stage 2: Anima context + local weather cache + compact LLM flow decision
            if not anima_fetched:
                server_cfg = self._config_reader()
                char_id = _resolve_character_id(server_cfg)
                weather_hint = self._weather_reader(server_cfg)
                prefetched_anima_ctx = self._fetch_anima_context(
                    char_id,
                    weather_hint,
                    ensure_inner_life=bool(cfg.get("inner_life_enabled", False)),
                )
            anima_ctx = prefetched_anima_ctx or {
                "character_id": char_id,
                "character_name": "小智",
                "hours_since_last_chat": None,
                "mood": {"stage": "familiar", "last_emotion": "calm"},
                "due_open_loops": [],
                "inner_life": {},
            }
            if isinstance(anima_ctx.get("inner_life"), dict) and anima_ctx["inner_life"]:
                with self._lock:
                    self._last_inner_life = dict(anima_ctx["inner_life"])

            # Cross-client / post-restart ignored-nudge backoff & recency sync
            with self._lock:
                self._sync_anima_last_turn_locked(anima_ctx)
                synced_last_chat = self._last_user_chat_ts

            # Post-restart chat recency safeguard using Anima's hours_since_last_chat & last_turn_at
            min_chat_min = max(5, int(cfg.get("min_since_last_chat_minutes", 45)))
            anima_hrs = anima_ctx.get("hours_since_last_chat")
            recent_by_hrs = (
                not isinstance(anima_hrs, bool)
                and isinstance(anima_hrs, (int, float))
                and math.isfinite(float(anima_hrs))
                and (float(anima_hrs) * 60.0) < float(min_chat_min)
            )
            recent_by_ts = (
                synced_last_chat > 0.0
                and (now - synced_last_chat) < (float(min_chat_min) * 60.0)
            )
            if not allow_bypass and (recent_by_hrs or recent_by_ts):
                with self._lock:
                    self._last_skip_reason = "recent_chat_anima"
                return {
                    "ok": True,
                    "outcome": "skipped",
                    "stage": "anima_recency_gate",
                    "reason": "recent_chat_anima",
                    "delivery_mode": effective_mode,
                    "manual": manual,
                    "rhythm": rhythm,
                }

            due_loops = (
                list(anima_ctx.get("due_open_loops") or [])[:2]
                if cfg.get("allow_open_loop_followup", True)
                else []
            )
            l3_finished = gate_meta.get("l3_finished")

            if not due_loops and not l3_finished and not cfg.get("allow_idle_chitchat", True):
                with self._lock:
                    self._last_skip_reason = "no_trigger_signal"
                return {
                    "ok": True,
                    "outcome": "skipped",
                    "stage": "trigger_filter",
                    "reason": "no_trigger_signal",
                    "delivery_mode": effective_mode,
                    "manual": manual,
                    "rhythm": rhythm,
                }

            sys_prompt, user_prompt = self._build_llm_prompts(
                server_cfg=server_cfg,
                anima_ctx=anima_ctx,
                rhythm=rhythm,
                weather_hint=weather_hint,
                due_loops=due_loops,
                l3_finished=l3_finished,
                now_dt=now_dt,
            )

            try:
                raw_llm = self._llm_caller(sys_prompt, user_prompt, server_cfg)
            except Exception as exc:
                err_reason = f"llm_error:{type(exc).__name__}"
                safe_err = _redact_error_text(exc)
                with self._lock:
                    self._last_skip_reason = err_reason
                return {
                    "ok": False,
                    "outcome": "error",
                    "stage": "llm_eval",
                    "reason": err_reason,
                    "error": safe_err,
                    "delivery_mode": effective_mode,
                    "manual": manual,
                }

            allowed_loop_ids = {str(lp.get("memory_id") or "") for lp in due_loops if lp.get("memory_id")}
            decision = parse_proactive_llm_decision(raw_llm, allowed_open_loop_ids=allowed_loop_ids)

            entry_id = f"pro-{uuid.uuid4().hex[:12]}"
            ts_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

            if decision["action"] != "SPEAK":
                entry = {
                    "id": entry_id,
                    "ts": now,
                    "time": ts_str,
                    "mode": effective_mode,
                    "action": "HEARTBEAT_OK",
                    "delivered": False,
                    "manual": bool(manual),
                    "would_block_reason": would_block_reason,
                    "reason": decision["reason"],
                    "emotion": "",
                    "text": "",
                    "open_loop_id": "",
                    "rhythm_category": str(rhythm.get("activity_category") or ""),
                    "rhythm_online": bool(rhythm.get("online", False)),
                }
                with self._lock:
                    self._last_skip_reason = decision["reason"]
                    if not manual:
                        if effective_mode == "dry_run":
                            self._last_dry_run_ts = now
                        else:
                            self._last_heartbeat_ok_ts = now
                    self._append_history_locked(entry)
                return {
                    "ok": True,
                    "outcome": "heartbeat_ok",
                    "entry": entry,
                    "would_block_reason": would_block_reason,
                    "rhythm": rhythm,
                    "inner_life": anima_ctx.get("inner_life") or {},
                }

            # Stage 3: SPEAK delivery
            if effective_mode == "dry_run":
                entry = {
                    "id": entry_id,
                    "ts": now,
                    "time": ts_str,
                    "mode": "dry_run",
                    "action": "SPEAK",
                    "delivered": False,
                    "manual": bool(manual),
                    "would_block_reason": would_block_reason,
                    "reason": decision["reason"],
                    "emotion": decision["emotion"],
                    "text": decision["text"],
                    "open_loop_id": decision["open_loop_id"],
                    "rhythm_category": str(rhythm.get("activity_category") or ""),
                    "rhythm_online": bool(rhythm.get("online", False)),
                }
                with self._lock:
                    self._last_skip_reason = ""
                    if not manual:
                        self._last_dry_run_ts = now
                    self._append_history_locked(entry)
                try:
                    runtime_events.publish_event(
                        "proactive_nudge",
                        id=entry_id,
                        mode="dry_run",
                        action="SPEAK",
                        delivered=False,
                        emotion=decision["emotion"],
                        text=decision["text"],
                        open_loop_id=decision["open_loop_id"],
                        reason=decision["reason"],
                    )
                except Exception:
                    pass
                return {
                    "ok": True,
                    "outcome": "dry_run_logged",
                    "entry": entry,
                    "would_block_reason": would_block_reason,
                    "rhythm": rhythm,
                    "inner_life": anima_ctx.get("inner_life") or {},
                }

            def _dispatch_mobile_nudge() -> Dict[str, Any]:
                emo_zh_map = {
                    "calm": "平静",
                    "gentle": "温柔",
                    "happy": "开心",
                    "playful": "俏皮",
                    "shy": "害羞",
                    "serious": "认真",
                    "tender": "柔情",
                }
                emo_label = emo_zh_map.get(str(decision["emotion"] or "gentle").lower(), "温柔")
                prompt_s = str(server_cfg.get("prompt") or "")
                cname = "雷电芽衣" if ("雷电芽衣" in prompt_s or "芽衣" in prompt_s) else ("罗小黑" if "罗小黑" in prompt_s else "小智")
                title = f"{cname} · {emo_label}"
                if callable(self._mobile_sender):
                    return dict(self._mobile_sender(title, decision["text"], "proactive") or {})
                import mobile_push

                return mobile_push.send_mobile_notification(title, decision["text"], category="proactive")

            # mobile_push-only delivery mode (zero audio/TTS overhead; sends directly to phone)
            if effective_mode == "mobile_push":
                push_res = _dispatch_mobile_nudge()
                if not push_res.get("ok"):
                    safe_err = _redact_error_text(push_res.get("error") or push_res.get("reason") or "mobile_push_failed")
                    with self._lock:
                        self._last_skip_reason = f"mobile_push:{safe_err}"
                    return {
                        "ok": False,
                        "outcome": "deferred" if push_res.get("skipped") else "error",
                        "stage": "mobile_push",
                        "reason": str(push_res.get("reason") or "mobile_push_failed"),
                        "error": safe_err,
                        "delivery_mode": "mobile_push",
                    }
                ack_ok = True
                if decision["open_loop_id"]:
                    ack_ok = self._send_anima_ack(char_id, decision["open_loop_id"], "nudged", entry_id)
                entry = {
                    "id": entry_id,
                    "ts": now,
                    "time": ts_str,
                    "mode": "mobile_push",
                    "action": "SPEAK",
                    "delivered": True,
                    "mobile_pushed": True,
                    "mobile_provider": str(push_res.get("provider") or ""),
                    "manual": bool(manual),
                    "would_block_reason": "",
                    "reason": decision["reason"],
                    "emotion": decision["emotion"],
                    "text": decision["text"],
                    "open_loop_id": decision["open_loop_id"],
                    "ack_ok": ack_ok,
                    "rhythm_category": str(rhythm.get("activity_category") or ""),
                    "rhythm_online": bool(rhythm.get("online", False)),
                }
                with self._lock:
                    self._rollover_day_locked(today_str)
                    self._daily_spoke_count += 1
                    self._consecutive_ignored += 1
                    self._last_spoke_ts = now
                    self._last_skip_reason = ""
                    if l3_finished and l3_finished.get("id"):
                        self._last_l3_notified_id = str(l3_finished["id"])
                    self._append_history_locked(entry)
                try:
                    runtime_events.publish_event(
                        "proactive_nudge",
                        id=entry_id,
                        mode="mobile_push",
                        action="SPEAK",
                        delivered=True,
                        emotion=decision["emotion"],
                        text=decision["text"],
                        open_loop_id=decision["open_loop_id"],
                        reason=decision["reason"],
                    )
                except Exception:
                    pass
                return {
                    "ok": True,
                    "outcome": "pushed_mobile",
                    "entry": entry,
                    "rhythm": rhythm,
                    "inner_life": anima_ctx.get("inner_life") or {},
                }

            # pc_speaker / pc_and_mobile delivery mode
            if self._busy_checker():
                with self._lock:
                    self._last_skip_reason = "voice_busy_pre_tts"
                return {
                    "ok": True,
                    "outcome": "skipped",
                    "stage": "pre_tts_guard",
                    "reason": "voice_busy_pre_tts",
                    "delivery_mode": effective_mode,
                }

            try:
                wav_bytes, tts_src = self._tts_synthesizer(decision["text"], decision["emotion"], server_cfg)
            except Exception as exc:
                safe_tts_err = _redact_error_text(exc)
                err_reason = f"tts_error:{type(exc).__name__}"
                with self._lock:
                    self._last_skip_reason = err_reason
                return {
                    "ok": False,
                    "outcome": "deferred",
                    "stage": "tts_synth",
                    "reason": err_reason,
                    "error": safe_tts_err,
                    "delivery_mode": effective_mode,
                }
            if not wav_bytes:
                safe_tts_reason = _redact_error_text(tts_src) or "tts_unavailable"
                with self._lock:
                    self._last_skip_reason = safe_tts_reason
                return {
                    "ok": False,
                    "outcome": "deferred",
                    "stage": "tts_synth",
                    "reason": safe_tts_reason,
                    "delivery_mode": effective_mode,
                }

            # Final pre-playback busy check
            if self._busy_checker():
                with self._lock:
                    self._last_skip_reason = "voice_busy_pre_play"
                return {
                    "ok": True,
                    "outcome": "skipped",
                    "stage": "pre_play_guard",
                    "reason": "voice_busy_pre_play",
                    "delivery_mode": effective_mode,
                }

            vol = float(cfg.get("pc_speaker_volume", 0.75))
            try:
                play_ok, play_detail = self._speaker_player(wav_bytes, vol)
            except Exception as exc:
                play_ok, play_detail = False, f"{type(exc).__name__}: {exc}"
            if not play_ok:
                safe_detail = _redact_error_text(play_detail)
                with self._lock:
                    self._last_skip_reason = f"pc_play_failed:{safe_detail}"
                return {
                    "ok": False,
                    "outcome": "error",
                    "stage": "pc_speaker_play",
                    "reason": "pc_play_failed",
                    "error": safe_detail,
                    "delivery_mode": effective_mode,
                }

            # Optional best-effort mobile push copy when pc_and_mobile or forward_proactive is enabled
            mobile_pushed = False
            mobile_err = ""
            mp_cfg = runtime_settings.get_mobile_push(self.settings_path)
            should_push_copy = (
                effective_mode == "pc_and_mobile"
                or (bool(mp_cfg.get("enabled", False)) and bool(mp_cfg.get("forward_proactive", True)))
            )
            if should_push_copy and (bool(mp_cfg.get("enabled", False)) or callable(self._mobile_sender)):
                try:
                    m_res = _dispatch_mobile_nudge()
                    mobile_pushed = bool(m_res.get("ok"))
                    if not mobile_pushed and not m_res.get("skipped"):
                        mobile_err = _redact_error_text(m_res.get("error") or m_res.get("reason") or "")
                except Exception as exc:
                    mobile_err = _redact_error_text(exc)

            ack_ok = True
            if decision["open_loop_id"]:
                ack_ok = self._send_anima_ack(char_id, decision["open_loop_id"], "nudged", entry_id)

            entry = {
                "id": entry_id,
                "ts": now,
                "time": ts_str,
                "mode": effective_mode,
                "action": "SPEAK",
                "delivered": True,
                "mobile_pushed": mobile_pushed,
                "mobile_error": mobile_err,
                "manual": bool(manual),
                "would_block_reason": "",
                "reason": decision["reason"],
                "emotion": decision["emotion"],
                "text": decision["text"],
                "open_loop_id": decision["open_loop_id"],
                "ack_ok": ack_ok,
                "tts_source": tts_src,
                "volume": vol,
                "rhythm_category": str(rhythm.get("activity_category") or ""),
                "rhythm_online": bool(rhythm.get("online", False)),
            }
            with self._lock:
                self._rollover_day_locked(today_str)
                self._daily_spoke_count += 1
                self._consecutive_ignored += 1
                self._last_spoke_ts = now
                self._last_skip_reason = ""
                if l3_finished and l3_finished.get("id"):
                    self._last_l3_notified_id = str(l3_finished["id"])
                self._append_history_locked(entry)

            try:
                runtime_events.publish_event(
                    "proactive_nudge",
                    id=entry_id,
                    mode=effective_mode,
                    action="SPEAK",
                    delivered=True,
                    emotion=decision["emotion"],
                    text=decision["text"],
                    open_loop_id=decision["open_loop_id"],
                    reason=decision["reason"],
                )
            except Exception:
                pass

            return {
                "ok": True,
                "outcome": "spoken",
                "entry": entry,
                "rhythm": rhythm,
                "inner_life": anima_ctx.get("inner_life") or {},
            }
        finally:
            self._eval_lock.release()

    def trigger_manual_check(
        self,
        *,
        force_dry_run: bool = True,
        bypass_timing_gates: bool = True,
    ) -> Dict[str, Any]:
        """Manual trigger for 18080 Console '立即试跑一次' button (defaults to safe dry_run)."""
        return self.run_once(
            manual=True,
            force_dry_run=bool(force_dry_run),
            bypass_timing_gates=bool(bypass_timing_gates) if force_dry_run else False,
        )

    def get_snapshot(
        self,
        *,
        refresh_live: bool = False,
        history_limit: int = 20,
    ) -> Dict[str, Any]:
        """Return structured status for 18080 Console `/api/proactive` and `/api/state`."""
        cfg = self._get_proactive_cfg()
        now = self._now()
        now_dt = self._local_dt(now)
        today_str = now_dt.strftime("%Y-%m-%d")

        if refresh_live:
            if cfg.get("enabled", False) or cfg.get("inner_life_enabled", False):
                if cfg.get("enabled", False):
                    rhy = self._fetch_desktop_rhythm()
                else:
                    rhy = {"online": False}
                server_cfg = self._config_reader()
                char_id = _resolve_character_id(server_cfg)
                weather_hint = self._weather_reader(server_cfg)
                anima_ctx = self._fetch_anima_context(
                    char_id,
                    weather_hint,
                    ensure_inner_life=bool(cfg.get("inner_life_enabled", False)),
                )
                with self._lock:
                    self._ensure_loaded_locked()
                    if cfg.get("enabled", False):
                        self._last_rhythm = dict(rhy)
                    if isinstance(anima_ctx, dict):
                        if isinstance(anima_ctx.get("inner_life"), dict) and anima_ctx["inner_life"]:
                            self._last_inner_life = dict(anima_ctx["inner_life"])
                        self._sync_anima_last_turn_locked(anima_ctx)

        with self._lock:
            self._ensure_loaded_locked()
            self._rollover_day_locked(today_str)
            eff_cd = compute_effective_cooldown_minutes(
                int(cfg.get("min_cooldown_minutes", 90)),
                self._consecutive_ignored,
            )
            next_eligible_sec = 0
            if self._last_spoke_ts > 0.0:
                next_eligible_sec = max(0, int((self._last_spoke_ts + eff_cd * 60.0) - now))
            runtime_info = {
                "running": bool(self._thread is not None and self._thread.is_alive()),
                "in_quiet_hours": is_in_quiet_hours(
                    now_dt,
                    str(cfg.get("quiet_start", "23:00")),
                    str(cfg.get("quiet_end", "08:00")),
                ),
                "daily_date": self._daily_date,
                "daily_spoke_count": self._daily_spoke_count,
                "max_daily_nudges": int(cfg.get("max_daily_nudges", 4)),
                "consecutive_ignored": self._consecutive_ignored,
                "effective_cooldown_minutes": eff_cd,
                "cooldown_remaining_seconds": next_eligible_sec,
                "last_spoke_ts": self._last_spoke_ts,
                "last_check_ts": self._last_check_ts,
                "last_skip_reason": self._last_skip_reason,
                "last_user_chat_ts": self._last_user_chat_ts,
            }
            rhythm_copy = dict(self._last_rhythm)
            inner_life_copy = dict(self._last_inner_life)
            lim = max(1, min(MAX_HISTORY_ENTRIES, int(history_limit)))
            hist_copy = [dict(h) for h in reversed(self._history[-lim:])]

        return {
            "ok": True,
            "config": cfg,
            "vocal_cue_enabled": vocal_cue_router.is_vocal_cue_enabled(self.settings_path),
            "screen_peek_enabled": bool(runtime_settings.get_screen_peek(self.settings_path).get("enabled", False)),
            "runtime": runtime_info,
            "rhythm": rhythm_copy,
            "inner_life": inner_life_copy,
            "history": hist_copy,
            "latest_vocal_cue": vocal_cue_router.get_latest_vocal_cue(),
            "recent_vocal_cues": vocal_cue_router.get_recent_vocal_cues(limit=8),
        }


_engine_lock = threading.Lock()
_default_engine: Optional[ProactiveCompanionEngine] = None


def get_proactive_engine() -> ProactiveCompanionEngine:
    global _default_engine
    with _engine_lock:
        if _default_engine is None:
            _default_engine = ProactiveCompanionEngine()
        return _default_engine


def sync_proactive_lifecycle(settings_path: Optional[str] = None) -> bool:
    """Lazily start background heartbeat only when `proactive.enabled` is True; stop and release caches when False."""
    enabled = bool(runtime_settings.get_proactive(settings_path).get("enabled", False))
    if enabled:
        eng = get_proactive_engine()
        eng.start()
        return True
    with _engine_lock:
        eng = _default_engine
    if eng is not None:
        eng.stop(timeout=1.5)
        with eng._lock:
            eng._last_rhythm = {"online": False}
    return False


def start_proactive_companion() -> Optional[ProactiveCompanionEngine]:
    """Start proactive heartbeat ONLY if enabled in runtime settings (default False -> zero idle thread/memory)."""
    if not bool(runtime_settings.get_proactive().get("enabled", False)):
        return None
    eng = get_proactive_engine()
    eng.start()
    return eng


def stop_proactive_companion(timeout: float = 2.0) -> None:
    global _default_engine
    with _engine_lock:
        eng = _default_engine
    if eng is not None:
        eng.stop(timeout=timeout)


def trigger_manual_check(*, force_dry_run: bool = True) -> Dict[str, Any]:
    return get_proactive_engine().trigger_manual_check(force_dry_run=force_dry_run)


def get_proactive_snapshot(*, refresh_live: bool = False, history_limit: int = 20) -> Dict[str, Any]:
    return get_proactive_engine().get_snapshot(refresh_live=refresh_live, history_limit=history_limit)


def run_on_demand_screen_peek(
    *,
    question: str = "",
    speak: bool = False,
    settings_path: Optional[str] = None,
    peek_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
    llm_caller: Optional[Callable[[str, str, dict], str]] = None,
    tts_synthesizer: Optional[Callable[[str, str, dict], Tuple[Optional[bytes], str]]] = None,
    speaker_player: Optional[Callable[[bytes, float], Tuple[bool, str]]] = None,
    config_reader: Optional[Callable[[], dict]] = None,
    busy_checker: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Privacy-guarded on-demand active-window screen peek for 18080 Console & desktop widget (Phase 2 ⑤)."""
    if not bool(runtime_settings.get_screen_peek(settings_path).get("enabled", False)):
        return {
            "ok": False,
            "blocked": True,
            "reason": "screen_peek_disabled",
            "error": "按需浅看屏功能当前处于关闭状态，请在 18080 控制台开启后再使用。",
        }

    q_clean = str(question or "").strip()[:200]
    if callable(peek_fetcher):
        peek_res = dict(peek_fetcher(q_clean) or {})
    else:
        try:
            # Host 18900 VLM fallback tries up to 2 providers * 16s = 32s; give 36s budget
            with httpx.Client(trust_env=False, timeout=36.0) as client:
                resp = client.post(
                    f"{PC_MCP_HOST}/desktop/screen-peek",
                    headers=pc_audio_router.get_pc_mcp_auth_headers(),
                    json={"question": q_clean},
                )
            try:
                peek_res = resp.json() if resp.content else {}
            except Exception:
                peek_res = {}
            if not isinstance(peek_res, dict):
                peek_res = {}
            if resp.status_code == 403:
                peek_res.setdefault("ok", False)
                peek_res.setdefault("blocked", True)
                peek_res.setdefault("summary", "隐私护栏或当前电脑操控挡位不允许截屏")
            elif resp.status_code != 200:
                return {
                    "ok": False,
                    "blocked": False,
                    "reason": f"host_http_{resp.status_code}",
                    "error": _redact_error_text(peek_res.get("summary") or peek_res.get("error") or f"宿主机返回 HTTP {resp.status_code}"),
                }
        except Exception as exc:
            return {
                "ok": False,
                "blocked": False,
                "reason": "host_unreachable",
                "error": f"无法连接宿主机 18900 浅看屏接口: {_redact_error_text(exc)}",
            }

    if peek_res.get("blocked") or not peek_res.get("ok"):
        return {
            "ok": False,
            "blocked": bool(peek_res.get("blocked")),
            "reason": str(peek_res.get("blocked_reason") or "peek_failed"),
            "error": _redact_error_text(peek_res.get("summary") or "浅看屏未能获取当前窗口内容"),
            "safe_title": str(peek_res.get("safe_title") or ""),
            "activity_category": str(peek_res.get("activity_category") or ""),
        }

    raw_summary = str(peek_res.get("summary") or "").strip()
    try:
        raw_summary, _ = recall_guard.sanitize_recall_context(raw_summary, record_stats=False)
    except Exception:
        pass
    safe_title = str(peek_res.get("safe_title") or "")
    category = str(peek_res.get("activity_category") or "other")

    cfg_fn = config_reader or _read_server_yaml
    llm_fn = llm_caller or _default_llm_call
    server_cfg = cfg_fn()
    persona = str(server_cfg.get("prompt") or "").strip()[:600]
    sys_prompt = (
        f"{persona}\n\n"
        "【当前任务：按需陪看屏幕的一句自然回应】请结合下方屏幕浅摘要，用角色自然温柔的口吻回应 1 句话"
        "（严格 55 字以内，无 Markdown/无括号动作/无表情符号）。\n"
        '严格只输出 JSON：{"emotion":"gentle|calm|happy|playful|shy|serious|tender","text":"一句回应"}'
    )
    user_prompt = (
        f"当前活动窗口类别：{category}（{safe_title}）\n"
        f"用户提问：{q_clean or '帮我看看我现在在看什么'}\n"
        f"屏幕观察摘要：{raw_summary}"
    )

    emotion = "gentle"
    reply_text = _trim_proactive_spoken_text(raw_summary, max_chars=55)
    try:
        raw_llm = llm_fn(sys_prompt, user_prompt, server_cfg)
        dec = parse_proactive_llm_decision(
            raw_llm if '"action"' in str(raw_llm or "") else str(raw_llm or "").replace("{", '{"action":"SPEAK",', 1),
        )
        if dec.get("action") == "SPEAK" and dec.get("text"):
            emotion = str(dec.get("emotion") or "gentle")
            reply_text = _trim_proactive_spoken_text(dec["text"], max_chars=55)
    except Exception:
        pass

    spoken = False
    speak_status = "text_only"
    if speak and reply_text:
        busy_fn = busy_checker or _is_voice_pipeline_busy
        if busy_fn():
            speak_status = "voice_busy"
        else:
            synth_fn = tts_synthesizer or _default_synthesize_wav
            play_fn = speaker_player or (lambda w, v: pc_audio_router.play_wav_on_pc_sync(w, volume=v))
            try:
                wav_bytes, tts_src = synth_fn(reply_text, emotion, server_cfg)
                if wav_bytes:
                    vol = float(runtime_settings.get_proactive(settings_path).get("pc_speaker_volume", 0.75))
                    ok_p, p_det = play_fn(wav_bytes, vol)
                    spoken = bool(ok_p)
                    speak_status = f"spoken:{tts_src}" if ok_p else f"play_failed:{_redact_error_text(p_det)}"
                else:
                    speak_status = _redact_error_text(tts_src) or "tts_unavailable"
            except Exception as exc:
                speak_status = f"tts_error:{_redact_error_text(exc)}"

    try:
        runtime_events.publish_event(
            "screen_peek",
            emotion=emotion,
            text=reply_text,
            summary=raw_summary[:160],
            safe_title=safe_title,
            activity_category=category,
            spoken=spoken,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "blocked": False,
        "emotion": emotion,
        "text": reply_text,
        "summary": raw_summary,
        "safe_title": safe_title,
        "activity_category": category,
        "spoken": spoken,
        "speak_status": speak_status,
    }

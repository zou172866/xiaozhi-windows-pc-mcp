"""Cloud Audio API runtime hardening & streaming TTS 48kHz PC audio passthrough (21st patch: cloud_audio_api).

Zero import-time side effects: call `apply_cloud_audio_patches()` explicitly from `boot.install_all()`.

Fixes upstream cloud ASR/TTS bottlenecks for low-spec / GPU-free Windows PCs:
1. OpenaiASR (`core.providers.asr.openai`): offload blocking `requests.post` to worker thread,
   reuse pooled `requests.Session`, enforce timeout (`CLOUD_ASR_TIMEOUT_SECONDS`, default 20s),
   fail fast on unexpanded `${...}` placeholders, and redact secrets in logs/errors.
2. Qwen3ASRFlash (`core.providers.asr.qwen3_asr_flash`): offload blocking `dashscope.MultiModalConversation.call`
   to worker thread with timeout protection and placeholder guard.
3. Non-streaming Cloud TTS (`OpenAITTS`, `CosyVoiceSiliconflow`, `DoubaoTTS`, `FishSpeech`):
   - offload blocking HTTP calls to worker thread with connection pooling;
   - forward `speed` (and optional `gain`) in `CosyVoiceSiliconflow`;
   - make `DoubaoTTS.__init__` safe against unexpanded `${DOUBAO_TTS_APPID}` / non-numeric appid;
   - strip upstream's default `wakeup_words.wav` in `FishSpeech` when cloud `reference_id` is set.
4. Streaming Cloud TTS (`AliBLTTS`, `HuoshanDoubleStreamTTS`, `MinimaxTTSHTTPStream`):
   - cross-chunk emotion-tag firewall (strips `[gentle]`, `[happy]`, etc. before synthesis, even across token splits);
   - Windows PC 48kHz / 44.1kHz lossless speaker passthrough via `patches.pc_audio` with hot ESP32<->PC switch.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import ipaddress
import json
import os
import re
import threading
import time
from urllib.parse import urlparse
import uuid
from typing import Any, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

import asr_corrections
import patch_registry
from patches import pc_audio as pc_audio_patch
from patches.emotion import splitter as emotion_splitter
from patches.emotion import tags as emotion_tags

DEFAULT_CLOUD_ASR_TIMEOUT = 20.0
_SENSEVOICE_TAG_RE = re.compile(r"<\|[^|]+\|>")

_http_session_lock = threading.Lock()
_http_session: Optional[requests.Session] = None
_dashscope_lock = threading.Lock()

_REDACT_PATTERNS = (
    (re.compile(r"(Bearer[\s;]+)[A-Za-z0-9._\-~+/=]{6,}", re.IGNORECASE), r"\1***"),
    (re.compile(r"(GroupId\s*=\s*)[^\s&\"',;)}\]]+", re.IGNORECASE), r"\1***"),
    (
        re.compile(
            r"(\b(?:api_key|access_token|x-api-access-key|x-api-app-key|authorization|group_id|appid|token|"
            r"SILICONFLOW_API_KEY|OPENAI_AUDIO_API_KEY|DASHSCOPE_API_KEY|"
            r"DOUBAO_(?:ASR_|TTS_)?(?:APPID|ACCESS_TOKEN)|FISHSPEECH_API_KEY|"
            r"MINIMAX_(?:API_KEY|GROUP_ID))\b\s*[\"']?\s*[:=]\s*[\"']?)(?!None\b|null\b|true\b|false\b)[^\s&\"',;)}\]]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "sk-***"),
)

_PLACEHOLDER_MARKERS = (
    "你的",
    "请在这里",
    "your_api_key",
    "your-api-key",
)


def is_unexpanded_secret(value: Any) -> bool:
    """Return True when a secret/credential is empty, unexpanded `${ENV}`, or a template placeholder."""
    s = str(value or "").strip()
    if not s:
        return True
    if "${" in s and "}" in s:
        return True
    low = s.lower()
    return any(m in low for m in _PLACEHOLDER_MARKERS)


def redact_cloud_error(text: Any) -> str:
    s = str(text or "")
    for pat, repl in _REDACT_PATTERNS:
        s = pat.sub(repl, s)
    return s


def get_cloud_asr_timeout() -> float:
    raw = (os.environ.get("CLOUD_ASR_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            v = float(raw)
            if v == v:  # finite check
                return max(3.0, min(120.0, v))
        except (TypeError, ValueError):
            pass
    return DEFAULT_CLOUD_ASR_TIMEOUT


class _NoRedirectSession(requests.Session):
    """Pooled requests.Session that disables HTTP redirects by default (SSRF defense, P0-2)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.max_redirects = 0

    def request(self, method, url, **kwargs):
        kwargs.setdefault("allow_redirects", False)
        return super().request(method, url, **kwargs)


class _RedactingLoggerProxy:
    """Wraps upstream provider logger so URLs/exceptions with `?GroupId=` or `Bearer` never hit disk raw."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def bind(self, **kwargs):
        bound = self._inner.bind(**kwargs) if hasattr(self._inner, "bind") else self._inner
        return _RedactingLoggerProxy(bound)

    def _call(self, level_fn_name: str, msg: Any, *args, **kwargs):
        fn = getattr(self._inner, level_fn_name)
        if args:
            red_args = tuple(redact_cloud_error(a) if isinstance(a, (str, BaseException)) else a for a in args)
            return fn(redact_cloud_error(msg), *red_args, **kwargs)
        return fn(redact_cloud_error(msg), **kwargs)

    def debug(self, msg: Any, *args, **kwargs):
        return self._call("debug", msg, *args, **kwargs)

    def info(self, msg: Any, *args, **kwargs):
        return self._call("info", msg, *args, **kwargs)

    def error(self, msg: Any, *args, **kwargs):
        return self._call("error", msg, *args, **kwargs)

    def warning(self, msg: Any, *args, **kwargs):
        return self._call("warning", msg, *args, **kwargs)

    def critical(self, msg: Any, *args, **kwargs):
        return self._call("critical", msg, *args, **kwargs)

    def exception(self, msg: Any, *args, **kwargs):
        return self._call("exception", msg, *args, **kwargs)

    def log(self, level: Any, msg: Any, *args, **kwargs):
        fn = getattr(self._inner, "log")
        red_args = tuple(redact_cloud_error(a) if isinstance(a, (str, BaseException)) else a for a in args)
        return fn(level, redact_cloud_error(msg), *red_args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _is_official_https_domain(url: str, suffixes: Tuple[str, ...]) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in ("https", "wss"):
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    return any(host == suf or host.endswith("." + suf) for suf in suffixes)


def _get_cloud_http_session() -> requests.Session:
    global _http_session
    with _http_session_lock:
        if _http_session is None:
            sess = _NoRedirectSession()
            adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16)
            sess.mount("http://", adapter)
            sess.mount("https://", adapter)
            _http_session = sess
        return _http_session


def _split_trailing_unclosed_tag(text: str) -> Tuple[str, str]:
    """Split `text` into `(safe_prefix, trailing_pending)` if it ends with an unclosed potential emotion tag."""
    if not text:
        return "", ""
    last_open = max(text.rfind("["), text.rfind("<"), text.rfind("【"))
    if last_open < 0:
        return text, ""
    last_close = max(text.rfind("]"), text.rfind(">"), text.rfind("】"))
    if last_close > last_open:
        return text, ""
    tail = text[last_open:]
    if emotion_tags.has_unclosed_leading_tag(tail):
        return text[:last_open], tail
    return text, ""


def _push_streaming_emotions_from_text(tts_obj: Any, segment: str) -> Optional[str]:
    """Scan a streaming chunk for `[emotion]` tags, push to ESP32 OLED immediately, and return last emotion."""
    if not segment:
        return None
    found_emo: Optional[str] = None
    for m in emotion_tags._BRACKET_CANDIDATE_RE.finditer(segment):
        raw_inner = next((g for g in m.groups() if g is not None), "")
        emo_cand, should_strip = emotion_tags.normalize_emotion_name(raw_inner)
        if should_strip and emo_cand:
            found_emo = emo_cand
            tts_obj._turn_explicit_emotion = emo_cand
            conn = getattr(tts_obj, "conn", None)
            if conn is not None:
                prof = emotion_tags.get_mei_emotion_profile(emo_cand, require_ref_audio=False)
                emotion_splitter._push_esp32_emotion_async(
                    conn, prof["esp32_emoji"], prof["esp32_emotion"]
                )
    return found_emo


def strip_streaming_emotion_chunk(tts_obj: Any, text: str, *, flush: bool = False) -> str:
    """Stateful cross-chunk emotion-tag firewall + ESP32 emotion sync for streaming TTS (`[gen` + `tle] ` -> '')."""
    pending = str(getattr(tts_obj, "_cloud_tag_pending", "") or "")
    combined = pending + str(text or "")
    if not combined:
        tts_obj._cloud_tag_pending = ""
        return ""
    if flush:
        tts_obj._cloud_tag_pending = ""
        _push_streaming_emotions_from_text(tts_obj, combined)
        _, stripped = emotion_tags.extract_and_strip_emotion(combined, flush_incomplete=True)
        return emotion_tags.strip_emotion_tags(stripped)

    safe_prefix, next_pending = _split_trailing_unclosed_tag(combined)
    tts_obj._cloud_tag_pending = next_pending
    if not safe_prefix:
        return ""
    _push_streaming_emotions_from_text(tts_obj, safe_prefix)
    return emotion_tags.strip_emotion_tags(safe_prefix)


def _reset_streaming_tag_state(tts_obj: Any) -> None:
    tts_obj._cloud_tag_pending = ""


def _is_default_fish_ref_audio(ref_list: Any) -> bool:
    if not ref_list:
        return True
    if not isinstance(ref_list, (list, tuple)):
        return False
    for item in ref_list:
        s = str(item or "").strip().replace("\\", "/")
        if not s:
            continue
        if s.endswith("config/assets/wakeup_words.wav") or s == "wakeup_words.wav":
            continue
        return False
    return True


_LOCAL_GATEWAY_HOSTNAMES = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "host.docker.internal", "gateway.docker.internal"}
)
_DOCKER_BRIDGE_NET = ipaddress.ip_network("172.16.0.0/12")
_BLOCKED_ADMIN_PORTS = frozenset({8000, 8003, 18000, 18003, 18080, 18787, 18900, 19880})


def _is_local_http_url(url: str) -> bool:
    """Return True strictly for parsed local loopback / RFC1918 LAN HTTP gateways on non-admin ports (never prefix-spoofable)."""
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    if parsed.scheme.lower() != "http" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    try:
        eff_port = parsed.port if parsed.port is not None else 80
    except ValueError:
        return False
    if eff_port in _BLOCKED_ADMIN_PORTS:
        return False
    if host in _LOCAL_GATEWAY_HOSTNAMES:
        return True
    try:
        ip_obj = ipaddress.ip_address(host.split("%")[0])
        if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
            ip_obj = ip_obj.ipv4_mapped
    except ValueError:
        return False
    if ip_obj.is_unspecified or ip_obj.is_link_local or ip_obj.is_multicast:
        return False
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _DOCKER_BRIDGE_NET:
        return False
    return bool(ip_obj.is_loopback or ip_obj.is_private)


def _patch_openai_asr() -> Tuple[bool, str]:
    import core.providers.asr.openai as openai_asr_mod

    cls = openai_asr_mod.ASRProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    orig_stt = cls.speech_to_text
    ok_sig, det = patch_registry.verify_signature_contract(
        orig_stt, required_params=("self", "opus_data", "session_id"), require_async=True
    )
    if not ok_sig:
        return False, f"OpenaiASR.speech_to_text: {det}"

    @functools.wraps(orig_stt)
    async def _hardened_openai_stt(self, opus_data: List[bytes], session_id: str, artifacts=None, *args, **kwargs):
        file_path = None
        try:
            if artifacts is None:
                return "", None
            file_path = artifacts.file_path
            if not file_path or not os.path.isfile(file_path):
                return "", file_path

            api_url = str(getattr(self, "api_url", "") or "").strip()
            api_key = str(getattr(self, "api_key", "") or "").strip()
            if not api_url:
                raise RuntimeError("OpenaiASR 未配置 base_url")
            if is_unexpanded_secret(api_key) and not _is_local_http_url(api_url):
                raise RuntimeError("云端 OpenAI 兼容 ASR 密钥未配置或环境变量未展开（请设置 SILICONFLOW_API_KEY / OPENAI_AUDIO_API_KEY）")

            headers = {}
            if api_key and not is_unexpanded_secret(api_key):
                headers["Authorization"] = f"Bearer {api_key}"
            data = {"model": getattr(self, "model", "") or ""}
            timeout_sec = get_cloud_asr_timeout()

            def _do_post():
                sess = _get_cloud_http_session()
                with open(file_path, "rb") as audio_file:
                    files = {"file": (os.path.basename(file_path) or "audio.wav", audio_file, "audio/wav")}
                    return sess.post(api_url, files=files, data=data, headers=headers, timeout=timeout_sec)

            t0 = time.monotonic()
            response = await asyncio.wait_for(asyncio.to_thread(_do_post), timeout=timeout_sec + 2.0)
            elapsed = time.monotonic() - t0
            if response.status_code == 200:
                payload = response.json()
                text = str(payload.get("text", "") or "").strip() if isinstance(payload, dict) else ""
                if text:
                    text = _SENSEVOICE_TAG_RE.sub("", text).strip()
                if text:
                    text = asr_corrections.apply_asr_corrections(text)
                openai_asr_mod.logger.bind(tag=openai_asr_mod.TAG).debug(
                    f"[CloudAudio] OpenaiASR 耗时: {elapsed:.3f}s | 长度: {len(text)}"
                )
                return text, file_path
            raise RuntimeError(
                redact_cloud_error(f"OpenaiASR HTTP {response.status_code}: {response.text[:240]}")
            )
        except asyncio.TimeoutError:
            openai_asr_mod.logger.bind(tag=openai_asr_mod.TAG).error(
                f"[CloudAudio] OpenaiASR 识别超时 (>{get_cloud_asr_timeout():.1f}s)"
            )
            return "", file_path
        except Exception as exc:
            openai_asr_mod.logger.bind(tag=openai_asr_mod.TAG).error(
                f"[CloudAudio] OpenaiASR 语音识别失败: {redact_cloud_error(exc)}"
            )
            return "", file_path

    cls.speech_to_text = _hardened_openai_stt
    cls._cloud_asr_self_corrected = True
    cls._cloud_audio_patched = True
    return True, "ok"


def _patch_alibl_stream_asr() -> Tuple[bool, str]:
    import core.providers.asr.aliyunbl_stream as alibl_asr_mod

    cls = alibl_asr_mod.ASRProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    orig_init = cls.__init__
    ok_sig, det = patch_registry.verify_signature_contract(
        orig_init, required_params=("self", "config", "delete_audio_file"), require_async=False
    )
    if not ok_sig:
        return False, f"AliyunBLStreamASR.__init__: {det}"

    @functools.wraps(orig_init)
    def _safe_alibl_asr_init(self, config, delete_audio_file, *args, **kwargs):
        orig_init(self, config, delete_audio_file, *args, **kwargs)
        out_dir = str(getattr(self, "output_dir", "") or "./audio_output").strip()
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

    if not isinstance(alibl_asr_mod.logger, _RedactingLoggerProxy):
        alibl_asr_mod.logger = _RedactingLoggerProxy(alibl_asr_mod.logger)
    cls.__init__ = _safe_alibl_asr_init
    cls._cloud_audio_patched = True
    return True, "ok"


def _patch_doubao_asr() -> Tuple[bool, str]:
    import core.providers.asr.doubao as doubao_asr_mod

    cls = doubao_asr_mod.ASRProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    orig_init = cls.__init__
    orig_stt = cls.speech_to_text
    ok1, det1 = patch_registry.verify_signature_contract(
        orig_init, required_params=("self", "config", "delete_audio_file"), require_async=False
    )
    ok2, det2 = patch_registry.verify_signature_contract(
        orig_stt, required_params=("self", "opus_data", "session_id"), require_async=True
    )
    if not ok1 or not ok2:
        return False, f"DoubaoASR: init={det1}; stt={det2}"

    @functools.wraps(orig_init)
    def _safe_doubao_asr_init(self, config, delete_audio_file, *args, **kwargs):
        cfg_copy = dict(config or {})
        if not str(cfg_copy.get("output_dir") or "").strip():
            cfg_copy["output_dir"] = "tmp/"
        if is_unexpanded_secret(cfg_copy.get("appid")):
            fb_id = (os.environ.get("DOUBAO_ASR_APPID") or os.environ.get("DOUBAO_APPID") or "").strip()
            if fb_id:
                cfg_copy["appid"] = fb_id
        if is_unexpanded_secret(cfg_copy.get("access_token")):
            fb_tok = (os.environ.get("DOUBAO_ASR_ACCESS_TOKEN") or os.environ.get("DOUBAO_ACCESS_TOKEN") or "").strip()
            if fb_tok:
                cfg_copy["access_token"] = fb_tok
        orig_init(self, cfg_copy, delete_audio_file, *args, **kwargs)

    @functools.wraps(orig_stt)
    async def _hardened_doubao_asr_stt(self, opus_data: List[bytes], session_id: str, *args, **kwargs):
        if is_unexpanded_secret(getattr(self, "appid", "")) or is_unexpanded_secret(getattr(self, "access_token", "")):
            doubao_asr_mod.logger.bind(tag=doubao_asr_mod.TAG).error(
                "[CloudAudio] 火山豆包 ASR 密钥未配置或未展开（请设置 DOUBAO_ASR_APPID/DOUBAO_ASR_ACCESS_TOKEN 或 DOUBAO_APPID/DOUBAO_ACCESS_TOKEN）"
            )
            return "", None
        return await orig_stt(self, opus_data, session_id, *args, **kwargs)

    if not isinstance(doubao_asr_mod.logger, _RedactingLoggerProxy):
        doubao_asr_mod.logger = _RedactingLoggerProxy(doubao_asr_mod.logger)
    cls.__init__ = _safe_doubao_asr_init
    cls.speech_to_text = _hardened_doubao_asr_stt
    cls._cloud_audio_patched = True
    return True, "ok"


def _patch_doubao_stream_asr() -> Tuple[bool, str]:
    import core.providers.asr.doubao_stream as doubao_stream_mod

    cls = doubao_stream_mod.ASRProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    orig_init = cls.__init__
    orig_recv = cls.receive_audio
    ok1, det1 = patch_registry.verify_signature_contract(
        orig_init, required_params=("self", "config", "delete_audio_file"), require_async=False
    )
    ok2, det2 = patch_registry.verify_signature_contract(
        orig_recv, required_params=("self", "conn", "pcm_frame", "audio_have_voice"), require_async=True
    )
    if not ok1 or not ok2:
        return False, f"DoubaoStreamASR: init={det1}; receive_audio={det2}"

    @functools.wraps(orig_init)
    def _safe_doubao_stream_init(self, config, delete_audio_file, *args, **kwargs):
        cfg_copy = dict(config or {})
        if not str(cfg_copy.get("output_dir") or "").strip():
            cfg_copy["output_dir"] = "tmp/"
        if is_unexpanded_secret(cfg_copy.get("appid")):
            fb_id = (os.environ.get("DOUBAO_ASR_APPID") or os.environ.get("DOUBAO_APPID") or "").strip()
            if fb_id:
                cfg_copy["appid"] = fb_id
        if is_unexpanded_secret(cfg_copy.get("access_token")):
            fb_tok = (os.environ.get("DOUBAO_ASR_ACCESS_TOKEN") or os.environ.get("DOUBAO_ACCESS_TOKEN") or "").strip()
            if fb_tok:
                cfg_copy["access_token"] = fb_tok
        orig_init(self, cfg_copy, delete_audio_file, *args, **kwargs)
        out_dir = str(getattr(self, "output_dir", "") or "tmp/").strip()
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

    @functools.wraps(orig_recv)
    async def _hardened_doubao_stream_recv(self, conn, pcm_frame, audio_have_voice, *args, **kwargs):
        if audio_have_voice and (
            is_unexpanded_secret(getattr(self, "appid", ""))
            or is_unexpanded_secret(getattr(self, "access_token", ""))
        ):
            now = time.monotonic()
            if (now - float(getattr(self, "_last_cred_err_ts", 0.0))) >= 10.0:
                self._last_cred_err_ts = now
                doubao_stream_mod.logger.bind(tag=doubao_stream_mod.TAG).error(
                    "[CloudAudio] 火山豆包流式 ASR 密钥未配置或未展开（请设置 DOUBAO_ASR_APPID/DOUBAO_ASR_ACCESS_TOKEN 或 DOUBAO_APPID/DOUBAO_ACCESS_TOKEN）"
                )
            return await super(cls, self).receive_audio(conn, pcm_frame, audio_have_voice)
        return await orig_recv(self, conn, pcm_frame, audio_have_voice, *args, **kwargs)

    if not isinstance(doubao_stream_mod.logger, _RedactingLoggerProxy):
        doubao_stream_mod.logger = _RedactingLoggerProxy(doubao_stream_mod.logger)
    cls.__init__ = _safe_doubao_stream_init
    cls.receive_audio = _hardened_doubao_stream_recv
    cls._cloud_audio_patched = True
    return True, "ok"


def _patch_qwen3_asr_flash() -> Tuple[bool, str]:
    import core.providers.asr.qwen3_asr_flash as qwen_asr_mod

    cls = qwen_asr_mod.ASRProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    orig_stt = cls.speech_to_text
    ok_sig, det = patch_registry.verify_signature_contract(
        orig_stt, required_params=("self", "opus_data", "session_id"), require_async=True
    )
    if not ok_sig:
        return False, f"Qwen3ASRFlash.speech_to_text: {det}"

    @functools.wraps(orig_stt)
    async def _hardened_qwen_stt(self, opus_data: List[bytes], session_id: str, artifacts=None, *args, **kwargs):
        file_path = None
        try:
            if artifacts is None:
                return "", None
            temp_file_path = getattr(artifacts, "temp_path", None)
            file_path = getattr(artifacts, "file_path", None)
            if not temp_file_path:
                return "", file_path

            api_key = str(getattr(self, "api_key", "") or "").strip()
            if is_unexpanded_secret(api_key):
                raise RuntimeError("阿里云百炼 Qwen3-ASR-Flash 密钥未配置（请设置 DASHSCOPE_API_KEY）")

            messages = [{"role": "user", "content": [{"audio": temp_file_path}]}]
            if getattr(self, "context", ""):
                messages.insert(0, {"role": "system", "content": [{"text": self.context}]})
            asr_options = {
                "enable_lid": getattr(self, "enable_lid", True),
                "enable_itn": getattr(self, "enable_itn", True),
            }
            if getattr(self, "language", None):
                asr_options["language"] = self.language

            timeout_sec = get_cloud_asr_timeout()

            def _do_call() -> str:
                with _dashscope_lock:
                    qwen_asr_mod.dashscope.api_key = api_key
                    response = qwen_asr_mod.dashscope.MultiModalConversation.call(
                        model=getattr(self, "model_name", "qwen3-asr-flash"),
                        messages=messages,
                        result_format="message",
                        asr_options=asr_options,
                        stream=True,
                    )
                full_text = ""
                for chunk in response:
                    try:
                        text = chunk["output"]["choices"][0]["message"].content[0]["text"]
                        if isinstance(text, str):
                            full_text = text.strip()
                    except Exception:
                        continue
                return full_text

            full_text = await asyncio.wait_for(asyncio.to_thread(_do_call), timeout=timeout_sec)
            return full_text, file_path
        except asyncio.TimeoutError:
            qwen_asr_mod.logger.bind(tag=qwen_asr_mod.tag).error(
                f"[CloudAudio] Qwen3ASRFlash 识别超时 (>{get_cloud_asr_timeout():.1f}s)"
            )
            return "", file_path
        except Exception as exc:
            qwen_asr_mod.logger.bind(tag=qwen_asr_mod.tag).error(
                f"[CloudAudio] Qwen3ASRFlash 语音识别失败: {redact_cloud_error(exc)}"
            )
            return "", file_path

    cls.speech_to_text = _hardened_qwen_stt
    cls._cloud_audio_patched = True
    return True, "ok"


def _patch_openai_tts() -> Tuple[bool, str]:
    import core.providers.tts.openai as openai_tts_mod

    cls = openai_tts_mod.TTSProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    orig_speak = cls.text_to_speak
    ok_sig, det = patch_registry.verify_signature_contract(
        orig_speak, required_params=("self", "text", "output_file"), require_async=True
    )
    if not ok_sig:
        return False, f"OpenAITTS.text_to_speak: {det}"

    def _openai_tts_sync(self, text) -> bytes:
        api_url = str(getattr(self, "api_url", "") or "").strip()
        api_key = str(getattr(self, "api_key", "") or "").strip()
        if is_unexpanded_secret(api_key) and not _is_local_http_url(api_url):
            raise RuntimeError("OpenAITTS 密钥未配置或环境变量未展开（请设置 OPENAI_AUDIO_API_KEY）")
        headers = {"Content-Type": "application/json"}
        if api_key and not is_unexpanded_secret(api_key):
            headers["Authorization"] = f"Bearer {api_key}"
        data = {
            "model": getattr(self, "model", "tts-1"),
            "input": text,
            "voice": getattr(self, "voice", "alloy"),
            "response_format": getattr(self, "audio_file_type", "wav"),
            "speed": float(getattr(self, "speed", 1.0) or 1.0),
        }
        timeout_sec = float(getattr(self, "tts_timeout", 15.0) or 15.0)
        response = _get_cloud_http_session().post(api_url, json=data, headers=headers, timeout=timeout_sec)
        if response.status_code == 200:
            return response.content
        raise RuntimeError(
            redact_cloud_error(f"OpenAITTS 请求失败: HTTP {response.status_code} - {response.text[:240]}")
        )

    @functools.wraps(orig_speak)
    async def _hardened_openai_tts(self, text, output_file, *args, **kwargs):
        audio_bytes = await asyncio.to_thread(_openai_tts_sync, self, text)
        if output_file:
            with open(output_file, "wb") as audio_file:
                audio_file.write(audio_bytes)
            return None
        return audio_bytes

    cls._text_to_speak_sync = _openai_tts_sync
    cls.text_to_speak = _hardened_openai_tts
    cls._patched_tts_speak_ref = _hardened_openai_tts
    cls._cloud_audio_patched = True
    return True, "ok"


def _patch_siliconflow_tts() -> Tuple[bool, str]:
    import core.providers.tts.siliconflow as sf_tts_mod

    cls = sf_tts_mod.TTSProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    orig_speak = cls.text_to_speak
    ok_sig, det = patch_registry.verify_signature_contract(
        orig_speak, required_params=("self", "text", "output_file"), require_async=True
    )
    if not ok_sig:
        return False, f"CosyVoiceSiliconflow.text_to_speak: {det}"

    def _sf_tts_sync(self, text) -> bytes:
        access_token = str(getattr(self, "access_token", "") or "").strip()
        if is_unexpanded_secret(access_token):
            raise RuntimeError("硅基流动 TTS 密钥未配置或环境变量未展开（请设置 SILICONFLOW_API_KEY）")
        api_url = str(getattr(self, "api_url", "https://api.siliconflow.cn/v1/audio/speech") or "").strip()
        if not _is_official_https_domain(api_url, ("siliconflow.cn", "siliconflow.com")):
            raise RuntimeError("安全限制：SILICONFLOW_API_KEY 仅允许发往官方 siliconflow.cn / siliconflow.com 域名")
        request_json = {
            "model": getattr(self, "model", "FunAudioLLM/CosyVoice2-0.5B"),
            "input": text,
            "voice": getattr(self, "voice", ""),
            "response_format": getattr(self, "audio_file_type", "wav"),
            "speed": float(getattr(self, "speed", 1.0) or 1.0),
        }
        gain = getattr(self, "gain", None)
        if gain is not None and gain != "":
            try:
                request_json["gain"] = float(gain)
            except (TypeError, ValueError):
                pass
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        timeout_sec = float(getattr(self, "tts_timeout", 15.0) or 15.0)
        response = _get_cloud_http_session().post(api_url, json=request_json, headers=headers, timeout=timeout_sec)
        if response.status_code != 200:
            raise RuntimeError(
                redact_cloud_error(f"CosyVoiceSiliconflow HTTP {response.status_code}: {response.text[:240]}")
            )
        return response.content

    @functools.wraps(orig_speak)
    async def _hardened_sf_tts(self, text, output_file, *args, **kwargs):
        data = await asyncio.to_thread(_sf_tts_sync, self, text)
        if output_file:
            with open(output_file, "wb") as file_to_save:
                file_to_save.write(data)
            return None
        return data

    cls._text_to_speak_sync = _sf_tts_sync
    cls.text_to_speak = _hardened_sf_tts
    cls._patched_tts_speak_ref = _hardened_sf_tts
    cls._cloud_audio_patched = True
    return True, "ok"


def _patch_doubao_tts() -> Tuple[bool, str]:
    import core.providers.tts.doubao as doubao_tts_mod

    cls = doubao_tts_mod.TTSProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    orig_init = cls.__init__
    orig_speak = cls.text_to_speak
    ok1, det1 = patch_registry.verify_signature_contract(
        orig_init, required_params=("self", "config", "delete_audio_file"), require_async=False
    )
    ok2, det2 = patch_registry.verify_signature_contract(
        orig_speak, required_params=("self", "text", "output_file"), require_async=True
    )
    if not ok1 or not ok2:
        return False, f"DoubaoTTS: init={det1}; speak={det2}"

    @functools.wraps(orig_init)
    def _safe_doubao_init(self, config, delete_audio_file, *args, **kwargs):
        cfg_copy = dict(config or {})
        raw_appid = str(cfg_copy.get("appid") or "").strip()
        if is_unexpanded_secret(raw_appid):
            fb_appid = (os.environ.get("DOUBAO_TTS_APPID") or os.environ.get("DOUBAO_APPID") or "").strip()
            if fb_appid:
                raw_appid = fb_appid
        raw_token = str(cfg_copy.get("access_token") or "").strip()
        if is_unexpanded_secret(raw_token):
            fb_tok = (os.environ.get("DOUBAO_TTS_ACCESS_TOKEN") or os.environ.get("DOUBAO_ACCESS_TOKEN") or "").strip()
            if fb_tok:
                cfg_copy["access_token"] = fb_tok

        non_numeric_appid = bool(raw_appid and not raw_appid.isdigit())
        if non_numeric_appid:
            cfg_copy["appid"] = ""
        elif raw_appid:
            cfg_copy["appid"] = raw_appid

        orig_init(self, cfg_copy, delete_audio_file, *args, **kwargs)
        if non_numeric_appid:
            self.appid = raw_appid

    def _doubao_tts_sync(self, text) -> bytes:
        appid_str = str(getattr(self, "appid", "") or "").strip()
        access_token = str(getattr(self, "access_token", "") or "").strip()
        if is_unexpanded_secret(appid_str) or is_unexpanded_secret(access_token):
            raise RuntimeError("火山豆包 TTS 密钥未配置（请设置 DOUBAO_TTS_APPID/DOUBAO_TTS_ACCESS_TOKEN 或 DOUBAO_APPID/DOUBAO_ACCESS_TOKEN）")

        request_json = {
            "app": {
                "appid": appid_str,
                "token": access_token,
                "cluster": getattr(self, "cluster", "volcano_tts"),
            },
            "user": {"uid": "1"},
            "audio": {
                "voice_type": getattr(self, "voice", "BV001_streaming"),
                "encoding": getattr(self, "audio_file_type", "wav"),
                "speed_ratio": float(getattr(self, "speed_ratio", 1.0) or 1.0),
                "volume_ratio": float(getattr(self, "volume_ratio", 1.0) or 1.0),
                "pitch_ratio": float(getattr(self, "pitch_ratio", 1.0) or 1.0),
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "text_type": "plain",
                "operation": "query",
                "with_frontend": 1,
                "frontend_type": "unitTson",
            },
        }
        api_url = str(getattr(self, "api_url", "https://openspeech.bytedance.com/api/v1/tts") or "").strip()
        auth_prefix = str(getattr(self, "authorization", "Bearer;") or "Bearer;")
        headers = {"Authorization": f"{auth_prefix}{access_token}", "Content-Type": "application/json"}
        timeout_sec = float(getattr(self, "tts_timeout", 15.0) or 15.0)
        resp = _get_cloud_http_session().post(
            api_url, data=json.dumps(request_json), headers=headers, timeout=timeout_sec
        )
        try:
            body = resp.json()
        except Exception:
            body = {}
        if isinstance(body, dict) and body.get("data"):
            return base64.b64decode(body["data"])
        raise RuntimeError(
            redact_cloud_error(f"DoubaoTTS 请求失败: HTTP {resp.status_code} {resp.text[:240]}")
        )

    @functools.wraps(orig_speak)
    async def _hardened_doubao_tts(self, text, output_file, *args, **kwargs):
        audio_bytes = await asyncio.to_thread(_doubao_tts_sync, self, text)
        if output_file:
            with open(output_file, "wb") as f:
                f.write(audio_bytes)
            return None
        return audio_bytes

    cls.__init__ = _safe_doubao_init
    cls._text_to_speak_sync = _doubao_tts_sync
    cls.text_to_speak = _hardened_doubao_tts
    cls._patched_tts_speak_ref = _hardened_doubao_tts
    cls._cloud_audio_patched = True
    return True, "ok"


def _patch_fishspeech_tts() -> Tuple[bool, str]:
    import core.providers.tts.fishspeech as fish_mod

    cls = fish_mod.TTSProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    orig_speak = cls.text_to_speak
    ok_sig, det = patch_registry.verify_signature_contract(
        orig_speak, required_params=("self", "text", "output_file"), require_async=True
    )
    if not ok_sig:
        return False, f"FishSpeech.text_to_speak: {det}"

    def _fish_tts_sync(self, text) -> bytes:
        api_url = str(getattr(self, "api_url", "https://api.fish.audio/v1/tts") or "https://api.fish.audio/v1/tts").strip()
        api_key = str(getattr(self, "api_key", "") or "").strip()
        is_official_fish = _is_official_https_domain(api_url, ("fish.audio",))
        if is_unexpanded_secret(api_key) and not _is_local_http_url(api_url):
            raise RuntimeError("Fish Audio 密钥未配置或环境变量未展开（请设置 FISHSPEECH_API_KEY）")
        if api_key and not is_unexpanded_secret(api_key) and not is_official_fish and not _is_local_http_url(api_url):
            raise RuntimeError("安全限制：FISHSPEECH_API_KEY 仅允许发往官方 fish.audio 域名")

        ref_id = getattr(self, "reference_id", None)
        ref_id_str = str(ref_id).strip() if ref_id else None
        raw_ref_audios = getattr(self, "reference_audio", None) or []
        raw_ref_texts = getattr(self, "reference_text", None) or []

        references = []
        if not (ref_id_str and _is_default_fish_ref_audio(raw_ref_audios)):
            byte_audios = [fish_mod.audio_to_bytes(ra) for ra in raw_ref_audios]
            ref_texts = [fish_mod.read_ref_text(rt) for rt in raw_ref_texts]
            for rt, aud in zip(ref_texts, byte_audios):
                if aud:
                    references.append(fish_mod.ServeReferenceAudio(audio=aud, text=rt))

        data = {
            "text": text,
            "references": references,
            "reference_id": ref_id_str,
            "normalize": bool(getattr(self, "normalize", True)),
            "format": getattr(self, "audio_file_type", "wav"),
            "max_new_tokens": int(getattr(self, "max_new_tokens", 1024) or 1024),
            "chunk_length": int(getattr(self, "chunk_length", 200) or 200),
            "top_p": float(getattr(self, "top_p", 0.7) or 0.7),
            "repetition_penalty": float(getattr(self, "repetition_penalty", 1.2) or 1.2),
            "temperature": float(getattr(self, "temperature", 0.7) or 0.7),
            "streaming": bool(getattr(self, "streaming", False)),
            "use_memory_cache": getattr(self, "use_memory_cache", "on"),
            "seed": getattr(self, "seed", None),
        }
        pydantic_data = fish_mod.ServeTTSRequest(**data)
        packed = fish_mod.ormsgpack.packb(pydantic_data, option=fish_mod.ormsgpack.OPT_SERIALIZE_PYDANTIC)
        headers = {"Content-Type": "application/msgpack"}
        if api_key and not is_unexpanded_secret(api_key) and is_official_fish:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout_sec = float(getattr(self, "tts_timeout", 15.0) or 15.0)
        response = _get_cloud_http_session().post(api_url, data=packed, headers=headers, timeout=timeout_sec)
        if response.status_code == 200:
            return response.content
        raise RuntimeError(
            redact_cloud_error(f"FishSpeech 请求失败: HTTP {response.status_code} - {response.text[:240]}")
        )

    @functools.wraps(orig_speak)
    async def _hardened_fish_tts(self, text, output_file, *args, **kwargs):
        audio_bytes = await asyncio.to_thread(_fish_tts_sync, self, text)
        if output_file:
            with open(output_file, "wb") as audio_file:
                audio_file.write(audio_bytes)
            return None
        return audio_bytes

    cls._text_to_speak_sync = _fish_tts_sync
    cls.text_to_speak = _hardened_fish_tts
    cls._patched_tts_speak_ref = _hardened_fish_tts
    cls._cloud_audio_patched = True
    return True, "ok"


def _patch_alibl_stream_tts() -> Tuple[bool, str]:
    import core.providers.tts.alibl_stream as alibl_mod

    cls = alibl_mod.TTSProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    checks = (
        ("text_to_speak", ("self", "text"), True),
        ("start_session", ("self", "session_id"), True),
        ("finish_session", ("self", "session_id"), True),
        ("open_audio_channels", ("self", "conn"), True),
    )
    for mname, req_p, req_a in checks:
        fn = getattr(cls, mname, None)
        ok, det = patch_registry.verify_signature_contract(fn, required_params=req_p, require_async=req_a)
        if not ok:
            return False, f"AliBLTTS.{mname}: {det}"

    # 1. Attach PC 48kHz lossless streaming passthrough first
    pc_audio_patch.attach_streaming_pc_audio_hooks(cls, provider_kind="alibl_stream")

    # 2. Wrap emotion-tag firewall (handles split tokens across WS duplex chunks)
    orig_speak = cls.text_to_speak
    orig_start = cls.start_session
    orig_finish = cls.finish_session
    orig_reset = cls.reset_stream_state

    @functools.wraps(orig_reset)
    def _alibl_reset_stream_state(self, *args, **kwargs):
        _reset_streaming_tag_state(self)
        return orig_reset(self, *args, **kwargs)

    @functools.wraps(orig_start)
    async def _alibl_start_session(self, session_id, *args, **kwargs):
        _reset_streaming_tag_state(self)
        if is_unexpanded_secret(getattr(self, "api_key", "")):
            raise RuntimeError("阿里云百炼 CosyVoice 密钥未配置或环境变量未展开（请设置 DASHSCOPE_API_KEY）")
        return await orig_start(self, session_id, *args, **kwargs)

    @functools.wraps(orig_speak)
    async def _alibl_text_to_speak(self, text, *args, **kwargs):
        cleaned = strip_streaming_emotion_chunk(self, text or "", flush=False)
        if not cleaned:
            return None
        return await orig_speak(self, cleaned, *args, **kwargs)

    @functools.wraps(orig_finish)
    async def _alibl_finish_session(self, session_id, *args, **kwargs):
        rem = strip_streaming_emotion_chunk(self, "", flush=True)
        if rem and getattr(self, "ws", None) is not None:
            try:
                await orig_speak(self, rem, None)
            except Exception:
                pass
        return await orig_finish(self, session_id, *args, **kwargs)

    if not isinstance(alibl_mod.logger, _RedactingLoggerProxy):
        alibl_mod.logger = _RedactingLoggerProxy(alibl_mod.logger)
    cls.reset_stream_state = _alibl_reset_stream_state
    cls.start_session = _alibl_start_session
    cls.text_to_speak = _alibl_text_to_speak
    cls.finish_session = _alibl_finish_session
    cls._cloud_audio_patched = True
    return True, "ok"


def _patch_huoshan_double_stream_tts() -> Tuple[bool, str]:
    import core.providers.tts.huoshan_double_stream as hs_mod

    cls = hs_mod.TTSProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    checks = (
        ("text_to_speak", ("self", "text"), True),
        ("start_session", ("self", "session_id"), True),
        ("finish_session", ("self", "session_id"), True),
        ("open_audio_channels", ("self", "conn"), True),
    )
    for mname, req_p, req_a in checks:
        fn = getattr(cls, mname, None)
        ok, det = patch_registry.verify_signature_contract(fn, required_params=req_p, require_async=req_a)
        if not ok:
            return False, f"HuoshanDoubleStreamTTS.{mname}: {det}"

    # 1. Attach PC 48kHz lossless streaming passthrough first
    pc_audio_patch.attach_streaming_pc_audio_hooks(cls, provider_kind="huoshan_double_stream")

    # 2. Wrap emotion-tag firewall + DOUBAO_* fallback safety
    orig_speak = cls.text_to_speak
    orig_start = cls.start_session
    orig_finish = cls.finish_session
    orig_reset = cls.reset_stream_state

    @functools.wraps(orig_reset)
    def _hs_reset_stream_state(self, *args, **kwargs):
        _reset_streaming_tag_state(self)
        return orig_reset(self, *args, **kwargs)

    @functools.wraps(orig_start)
    async def _hs_start_session(self, session_id, *args, **kwargs):
        _reset_streaming_tag_state(self)
        if is_unexpanded_secret(getattr(self, "appId", "")):
            fb_id = (os.environ.get("DOUBAO_TTS_APPID") or os.environ.get("DOUBAO_APPID") or "").strip()
            if fb_id:
                self.appId = fb_id
        if is_unexpanded_secret(getattr(self, "access_token", "")):
            fb_tok = (os.environ.get("DOUBAO_TTS_ACCESS_TOKEN") or os.environ.get("DOUBAO_ACCESS_TOKEN") or "").strip()
            if fb_tok:
                self.access_token = fb_tok
        if is_unexpanded_secret(getattr(self, "appId", "")) or is_unexpanded_secret(getattr(self, "access_token", "")):
            raise RuntimeError("火山豆包双流式 TTS 密钥未配置（请设置 DOUBAO_TTS_APPID/DOUBAO_TTS_ACCESS_TOKEN 或 DOUBAO_APPID/DOUBAO_ACCESS_TOKEN）")
        return await orig_start(self, session_id, *args, **kwargs)

    @functools.wraps(orig_speak)
    async def _hs_text_to_speak(self, text, *args, **kwargs):
        cleaned = strip_streaming_emotion_chunk(self, text or "", flush=False)
        if not cleaned:
            return None
        return await orig_speak(self, cleaned, *args, **kwargs)

    @functools.wraps(orig_finish)
    async def _hs_finish_session(self, session_id, *args, **kwargs):
        rem = strip_streaming_emotion_chunk(self, "", flush=True)
        if rem and getattr(self, "ws", None) is not None:
            try:
                await orig_speak(self, rem, None)
            except Exception:
                pass
        return await orig_finish(self, session_id, *args, **kwargs)

    if not isinstance(hs_mod.logger, _RedactingLoggerProxy):
        hs_mod.logger = _RedactingLoggerProxy(hs_mod.logger)
    cls.reset_stream_state = _hs_reset_stream_state
    cls.start_session = _hs_start_session
    cls.text_to_speak = _hs_text_to_speak
    cls.finish_session = _hs_finish_session
    cls._cloud_audio_patched = True
    return True, "ok"


_MINIMAX_EMOTION_MAP = {
    "happy": "happy",
    "playful": "happy",
    "sad": "sad",
    "angry": "angry",
    "fearful": "fearful",
    "surprised": "surprised",
    "calm": "neutral",
    "gentle": "neutral",
    "serious": "neutral",
}
_MINIMAX_RAW_TAG_MAP = {
    "sad": "sad",
    "sorrow": "sad",
    "难过": "sad",
    "悲伤": "sad",
    "angry": "angry",
    "生气": "angry",
    "愤怒": "angry",
    "fearful": "fearful",
    "害怕": "fearful",
    "surprised": "surprised",
    "惊讶": "surprised",
}


def _patch_minimax_httpstream_tts() -> Tuple[bool, str]:
    import core.providers.tts.minimax_httpstream as mm_mod
    from core.providers.tts.dto.dto import SentenceType

    cls = mm_mod.TTSProvider
    if getattr(cls, "_cloud_audio_patched", False):
        return True, "already applied"

    checks = (
        ("__init__", ("self", "config", "delete_audio_file"), False),
        ("text_to_speak", ("self", "text", "is_last"), True),
        ("open_audio_channels", ("self", "conn"), True),
    )
    for mname, req_p, req_a in checks:
        fn = getattr(cls, mname, None)
        ok, det = patch_registry.verify_signature_contract(fn, required_params=req_p, require_async=req_a)
        if not ok:
            return False, f"MinimaxTTSHTTPStream.{mname}: {det}"

    # 1. Attach PC 44.1kHz lossless streaming passthrough first
    pc_audio_patch.attach_streaming_pc_audio_hooks(cls, provider_kind="minimax_httpstream")

    # 2. Wrap __init__, _process_remaining_text_stream & text_to_speak to clear forced `"happy"` default and preserve [emotion] tags
    orig_init = cls.__init__
    orig_speak = cls.text_to_speak
    orig_proc_rem = getattr(cls, "_process_remaining_text_stream", None)

    @functools.wraps(orig_init)
    def _mm_safe_init(self, config, delete_audio_file, *args, **kwargs):
        orig_init(self, config, delete_audio_file, *args, **kwargs)
        explicit_vs = (config or {}).get("voice_setting")
        has_explicit_emo = isinstance(explicit_vs, dict) and bool(explicit_vs.get("emotion"))
        self._mm_has_explicit_emotion = has_explicit_emo
        if not has_explicit_emo and isinstance(getattr(self, "voice_setting", None), dict):
            self.voice_setting.pop("emotion", None)

    if callable(orig_proc_rem):
        @functools.wraps(orig_proc_rem)
        def _mm_process_remaining_text_stream(self, is_last=False, *args, **kwargs):
            # Drain complete prosodic segments first without stripping `[emotion]` brackets prematurely
            while True:
                seg = self._get_segment_text()
                if not seg:
                    break
                self.to_tts_single_stream(seg, False)

            full_text = "".join(self.tts_text_buff)
            remaining_text = full_text[self.processed_chars :]
            self.processed_chars = len(full_text)
            if not remaining_text:
                if is_last:
                    self._process_before_stop_play_files()
                return False

            conn = getattr(self, "conn", None)
            cur_sid = getattr(self, "current_sentence_id", None) or getattr(conn, "sentence_id", None)
            if getattr(self, "_emotion_turn_sid", None) != cur_sid:
                self._emotion_turn_sid = cur_sid
                self._turn_explicit_emotion = None
                self._pending_segment_emotion = None
                self._segment_emotion_queue = []
                self._first_segment_done_sid = None
                self.is_first_sentence = True
                self.tts_audio_first_sentence = True

            raw_mm_rem = None
            m_rem_tag = emotion_tags._BRACKET_CANDIDATE_RE.search(remaining_text)
            if m_rem_tag:
                raw_tok_r = next((g for g in m_rem_tag.groups() if g is not None), "").strip().lower()
                raw_mm_rem = _MINIMAX_RAW_TAG_MAP.get(raw_tok_r)
            seg_emo, stripped = emotion_tags.extract_and_strip_emotion(remaining_text, flush_incomplete=True)
            if seg_emo:
                self._turn_explicit_emotion = seg_emo
                self._pending_segment_emotion = seg_emo
                self._pending_mm_raw_emotion = raw_mm_rem
            segment_text = emotion_tags.clean_segment_for_tts(stripped)
            if segment_text:
                self.to_tts_single_stream(segment_text, is_last)
                return True
            if is_last:
                self._process_before_stop_play_files()
            return False

    @functools.wraps(orig_speak)
    async def _mm_text_to_speak(self, text, is_last, *args, **kwargs):
        if is_unexpanded_secret(getattr(self, "api_key", "")) or is_unexpanded_secret(getattr(self, "group_id", "")):
            mm_mod.logger.bind(tag=mm_mod.TAG).error(
                "[CloudAudio] MiniMax TTS 密钥未配置或未展开（请设置 MINIMAX_API_KEY 与 MINIMAX_GROUP_ID）"
            )
            if is_last:
                self._process_before_stop_play_files()
            else:
                self.tts_audio_queue.put((SentenceType.LAST, [], None))
            return None

        raw_text_str = str(text or "")
        raw_mm_override = None
        m_raw_tag = emotion_tags._BRACKET_CANDIDATE_RE.search(raw_text_str)
        if m_raw_tag:
            raw_tok = next((g for g in m_raw_tag.groups() if g is not None), "").strip().lower()
            raw_mm_override = _MINIMAX_RAW_TAG_MAP.get(raw_tok)
        seg_emo, stripped = emotion_tags.extract_and_strip_emotion(raw_text_str, flush_incomplete=True)
        cleaned = emotion_tags.strip_emotion_tags(stripped).strip()
        if not cleaned:
            if is_last:
                self._process_before_stop_play_files()
            return None

        eff_emo = seg_emo or getattr(self, "_pending_segment_emotion", None) or getattr(self, "_turn_explicit_emotion", None)
        self._pending_segment_emotion = None
        if seg_emo:
            self._turn_explicit_emotion = seg_emo
        conn = getattr(self, "conn", None)
        if eff_emo:
            prof = emotion_tags.get_mei_emotion_profile(eff_emo, require_ref_audio=False)
            cur_sid = getattr(self, "current_sentence_id", None) or getattr(conn, "sentence_id", None)
            q = getattr(self, "_segment_emotion_queue", None)
            if q is None:
                q = []
                self._segment_emotion_queue = q
            q.append((cur_sid, cleaned, prof["esp32_emoji"], prof["esp32_emotion"]))
            if len(q) > 64:
                del q[:-64]
            if conn is not None and getattr(self, "tts_audio_first_sentence", True) and getattr(
                conn, "_last_pushed_mei_sid", None
            ) != cur_sid:
                emotion_splitter._push_esp32_emotion_async(conn, prof["esp32_emoji"], prof["esp32_emotion"])

        prev_vs = dict(self.voice_setting) if isinstance(getattr(self, "voice_setting", None), dict) else None
        if prev_vs is not None and not getattr(self, "_mm_has_explicit_emotion", False):
            mapped_mm_emo = (
                raw_mm_override
                or getattr(self, "_pending_mm_raw_emotion", None)
                or _MINIMAX_EMOTION_MAP.get(str(eff_emo or ""))
            )
            self._pending_mm_raw_emotion = None
            call_vs = dict(prev_vs)
            if mapped_mm_emo:
                call_vs["emotion"] = mapped_mm_emo
            else:
                call_vs.pop("emotion", None)
            self.voice_setting = call_vs
        try:
            return await orig_speak(self, cleaned, is_last, *args, **kwargs)
        finally:
            if prev_vs is not None:
                self.voice_setting = prev_vs

    if not isinstance(mm_mod.logger, _RedactingLoggerProxy):
        mm_mod.logger = _RedactingLoggerProxy(mm_mod.logger)
    cls.__init__ = _mm_safe_init
    if callable(orig_proc_rem):
        cls._process_remaining_text_stream = _mm_process_remaining_text_stream
    cls.text_to_speak = _mm_text_to_speak
    cls._cloud_audio_patched = True
    return True, "ok"


def apply_cloud_audio_patches() -> None:
    """Install all cloud audio API async/timeout/tag-firewall/PC-48kHz-streaming patches (fail-soft)."""
    sub_installers = (
        ("openai_asr", _patch_openai_asr),
        ("qwen3_asr_flash", _patch_qwen3_asr_flash),
        ("aliyunbl_stream_asr", _patch_alibl_stream_asr),
        ("doubao_asr", _patch_doubao_asr),
        ("doubao_stream_asr", _patch_doubao_stream_asr),
        ("openai_tts", _patch_openai_tts),
        ("siliconflow_tts", _patch_siliconflow_tts),
        ("doubao_tts", _patch_doubao_tts),
        ("fishspeech_tts", _patch_fishspeech_tts),
        ("alibl_stream", _patch_alibl_stream_tts),
        ("huoshan_double_stream", _patch_huoshan_double_stream_tts),
        ("minimax_httpstream", _patch_minimax_httpstream_tts),
    )
    applied: List[str] = []
    failures: List[str] = []
    for sub_name, fn in sub_installers:
        try:
            ok, det = fn()
            if ok:
                applied.append(sub_name)
            else:
                failures.append(f"{sub_name} ({det})")
        except Exception as exc:
            failures.append(f"{sub_name} ({exc})")

    total_targets = len(sub_installers)
    if not failures:
        patch_registry.record_patch(
            "cloud_audio_api",
            "ok",
            target=f"Cloud ASR/TTS providers ({total_targets} modules + 3 streaming PC audio)",
            detail=f"{len(applied)}/{total_targets} cloud audio targets patched",
            contract_ok=True,
            category="experience",
        )
        print(
            "[CloudAudio] 已挂载云端音频 API 加固补丁（异步非阻塞/超时/标签过滤 + 3 大流式 TTS 电脑高保真音响直通）",
            flush=True,
        )
    elif applied:
        patch_registry.record_patch(
            "cloud_audio_api",
            "partial",
            target=f"Cloud ASR/TTS providers ({total_targets} modules + 3 streaming PC audio)",
            detail=f"applied={len(applied)}/{total_targets}; skipped={'; '.join(failures)}",
            contract_ok=False,
            category="experience",
        )
        print(f"[CloudAudio] 部分云端音频补丁跳过: {'; '.join(failures)}", flush=True)
    else:
        patch_registry.record_patch(
            "cloud_audio_api",
            "skipped_contract",
            target=f"Cloud ASR/TTS providers ({total_targets} modules + 3 streaming PC audio)",
            detail="; ".join(failures),
            contract_ok=False,
            category="experience",
        )
        print(f"[CloudAudio] 跳过挂载云端音频补丁: {'; '.join(failures)}", flush=True)

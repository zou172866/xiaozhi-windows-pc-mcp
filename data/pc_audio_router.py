"""Route Xiaozhi TTS & wakeup audio directly to Windows PC speakers (48kHz lossless WAV)
while keeping ESP32-S3 OLED subtitles, emotions, speaking state, and abort in exact lockstep.

Hot-switched via data/audio_output_mode.json ("esp32" | "pc") with zero container restart.
Falls back seamlessly to ESP32 built-in speaker if host 18900 audio bridge is unreachable.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import threading
import time
import wave
import weakref
from typing import Dict, Optional, Tuple

import httpx

ROOT_DIR = "/opt/xiaozhi-esp32-server"
DATA_DIR = os.path.join(ROOT_DIR, "data")
AUDIO_MODE_PATH = os.path.join(DATA_DIR, "audio_output_mode.json")
PC_MCP_TOKEN_PATH = os.path.join(DATA_DIR, "pc_mcp_token.txt")
PC_AUDIO_HOST = os.environ.get("PC_AUDIO_BRIDGE_URL", "http://host.docker.internal:18900").rstrip("/")

_cfg_lock = threading.Lock()
_cfg_mtime = -1.0
_cfg_cache = {"target": "esp32", "volume": 1.0}

_tok_lock = threading.Lock()
_tok_mtime = -1.0
_tok_cache = ""

_health_lock = threading.Lock()
_health_ts = 0.0
_health_ok = False

_silent_opus_cache: Dict[int, bytes] = {}
_silent_opus_lock = threading.Lock()

_sync_client_lock = threading.Lock()
_sync_client: Optional[httpx.Client] = None
_async_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)


def read_pc_mcp_token() -> str:
    """Read shared local 18900 Bearer token with mtime cache."""
    global _tok_mtime, _tok_cache
    try:
        if not os.path.isfile(PC_MCP_TOKEN_PATH):
            return ""
        st = os.stat(PC_MCP_TOKEN_PATH)
        with _tok_lock:
            if st.st_mtime != _tok_mtime:
                with open(PC_MCP_TOKEN_PATH, "r", encoding="utf-8") as f:
                    _tok_cache = f.read().strip()
                _tok_mtime = st.st_mtime
            return _tok_cache
    except Exception:
        return _tok_cache


def get_pc_mcp_auth_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = dict(extra) if extra else {}
    tok = read_pc_mcp_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def _get_sync_client() -> httpx.Client:
    global _sync_client
    with _sync_client_lock:
        if _sync_client is None or _sync_client.is_closed:
            _sync_client = httpx.Client(
                trust_env=False,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return _sync_client


def _get_async_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _async_clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            trust_env=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=6),
        )
        _async_clients[loop] = client
    return client


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
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


def read_audio_output_config() -> dict:
    """Read audio_output_mode.json with mtime caching (sub-microsecond hot path)."""
    global _cfg_mtime, _cfg_cache
    try:
        if not os.path.exists(AUDIO_MODE_PATH):
            return dict(_cfg_cache)
        st = os.stat(AUDIO_MODE_PATH)
        with _cfg_lock:
            if st.st_mtime != _cfg_mtime:
                with open(AUDIO_MODE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                target = str(data.get("target") or "esp32").strip().lower()
                if target not in ("esp32", "pc"):
                    target = "esp32"
                try:
                    vol = float(data.get("volume", 1.0))
                except Exception:
                    vol = 1.0
                vol = round(max(0.2, min(1.5, vol)), 2)
                _cfg_cache = {"target": target, "volume": vol}
                _cfg_mtime = st.st_mtime
            return dict(_cfg_cache)
    except Exception:
        return dict(_cfg_cache)


def write_audio_output_config(target: str, volume: Optional[float] = None) -> dict:
    global _cfg_mtime, _cfg_cache
    cur = read_audio_output_config()
    t = str(target or cur["target"]).strip().lower()
    if t not in ("esp32", "pc"):
        raise ValueError(f"Unsupported audio output target: {target}")
    vol = cur["volume"] if volume is None else round(max(0.2, min(1.5, float(volume))), 2)
    payload = {"target": t, "volume": vol}
    _atomic_write_json(AUDIO_MODE_PATH, payload)
    with _cfg_lock:
        _cfg_cache = dict(payload)
        try:
            _cfg_mtime = os.stat(AUDIO_MODE_PATH).st_mtime
        except Exception:
            _cfg_mtime = -1.0
    return payload


def is_pc_audio_bridge_online(force: bool = False) -> bool:
    global _health_ts, _health_ok
    now = time.monotonic()
    with _health_lock:
        if not force and (now - _health_ts) < 4.0:
            return _health_ok
    ok = False
    try:
        client = _get_sync_client()
        r = client.get(
            f"{PC_AUDIO_HOST}/audio/status",
            headers=get_pc_mcp_auth_headers(),
            timeout=0.8,
        )
        ok = r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        ok = False
    with _health_lock:
        _health_ts = time.monotonic()
        _health_ok = ok
    return ok


async def is_pc_audio_bridge_online_async(force: bool = False) -> bool:
    global _health_ts, _health_ok
    now = time.monotonic()
    with _health_lock:
        if not force and (now - _health_ts) < 4.0:
            return _health_ok
    ok = False
    try:
        client = _get_async_client()
        r = await client.get(
            f"{PC_AUDIO_HOST}/audio/status",
            headers=get_pc_mcp_auth_headers(),
            timeout=0.8,
        )
        ok = r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        ok = False
    with _health_lock:
        _health_ts = time.monotonic()
        _health_ok = ok
    return ok


def _get_silent_opus_frame(sample_rate: int = 16000) -> bytes:
    sr = int(sample_rate or 16000)
    with _silent_opus_lock:
        cached = _silent_opus_cache.get(sr)
        if cached is not None:
            return cached
        import opuslib_next

        enc = opuslib_next.Encoder(sr, 1, opuslib_next.APPLICATION_AUDIO)
        frame_samples = int(sr * 60 / 1000)
        pcm_zero = bytes(frame_samples * 2)
        pkt = bytes(enc.encode(pcm_zero, frame_samples))
        _silent_opus_cache[sr] = pkt
        return pkt


def ensure_pcm_wav(audio_bytes: bytes, file_type: str = "wav") -> Tuple[bytes, int]:
    """Return (16-bit PCM WAV bytes, duration_ms). Lossless zero-copy passthrough for WAV."""
    if len(audio_bytes) >= 44 and audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                if wf.getsampwidth() == 2 and wf.getframerate() > 0:
                    dur_ms = int(round((wf.getnframes() / float(wf.getframerate())) * 1000.0))
                    return audio_bytes, dur_ms
        except Exception:
            pass
    # Decode MP3/OGG/etc. (e.g. EdgeTTS) into standard 16-bit PCM WAV
    from pydub import AudioSegment

    seg = AudioSegment.from_file(io.BytesIO(audio_bytes), format=(file_type or "mp3"))
    seg = seg.set_sample_width(2)
    out = io.BytesIO()
    seg.export(out, format="wav")
    return out.getvalue(), int(len(seg))


def _ensure_conn_pc_state(conn):
    if not hasattr(conn, "_pc_audio_queue_lock"):
        conn._pc_audio_queue_lock = threading.Lock()
    if not hasattr(conn, "_pc_audio_clip_queue"):
        conn._pc_audio_clip_queue = []
    if not hasattr(conn, "_pc_audio_send_lock"):
        conn._pc_audio_send_lock = asyncio.Lock()
    if not hasattr(conn, "_pc_audio_active_sid"):
        conn._pc_audio_active_sid = None
    if not hasattr(conn, "_pc_audio_queued_ms"):
        conn._pc_audio_queued_ms = 0
    if not hasattr(conn, "_pc_audio_skip_wait_once"):
        conn._pc_audio_skip_wait_once = None


def _enqueue_pc_clip(conn, sid, text: str, wav_bytes: bytes, dur_ms: int, volume: float) -> None:
    _ensure_conn_pc_state(conn)
    with conn._pc_audio_queue_lock:
        conn._pc_audio_clip_queue.append((sid, text, wav_bytes, dur_ms, volume))
        if len(conn._pc_audio_clip_queue) > 64:
            del conn._pc_audio_clip_queue[:-64]


def _pop_pc_clip_for_sentence(conn, cur_sid, text: Optional[str]):
    _ensure_conn_pc_state(conn)
    with conn._pc_audio_queue_lock:
        if not conn._pc_audio_clip_queue:
            return None
        conn._pc_audio_clip_queue[:] = [
            item for item in conn._pc_audio_clip_queue if item[0] == cur_sid
        ]
        if not conn._pc_audio_clip_queue:
            return None
        clean_txt = (text or "").strip()
        idx = next(
            (i for i, it in enumerate(conn._pc_audio_clip_queue) if (it[1] or "").strip() == clean_txt),
            0,
        )
        return conn._pc_audio_clip_queue.pop(idx)


def _clear_pc_clips(conn) -> None:
    _ensure_conn_pc_state(conn)
    with conn._pc_audio_queue_lock:
        conn._pc_audio_clip_queue.clear()


async def _post_play_to_pc(conn, turn_id: str, wav_bytes: bytes, volume: float) -> bool:
    _ensure_conn_pc_state(conn)
    async with conn._pc_audio_send_lock:
        if getattr(conn, "client_abort", False):
            return False
        if turn_id and getattr(conn, "sentence_id", None) != turn_id:
            return False
        try:
            client = _get_async_client()
            r = await client.post(
                f"{PC_AUDIO_HOST}/audio/play",
                content=wav_bytes,
                headers=get_pc_mcp_auth_headers(
                    {
                        "Content-Type": "audio/wav",
                        "X-Turn-Id": str(turn_id or ""),
                        "X-Volume": str(volume),
                    }
                ),
                timeout=8.0,
            )
            return r.status_code == 200
        except Exception as exc:
            print(f"[PCAudioRouter] 发送音频至电脑音响失败: {exc}", flush=True)
            return False


async def _post_stop_to_pc() -> None:
    try:
        client = _get_async_client()
        await client.post(
            f"{PC_AUDIO_HOST}/audio/stop",
            headers=get_pc_mcp_auth_headers(),
            timeout=1.5,
        )
    except Exception:
        pass


async def _wait_pc_audio_done(
    conn=None,
    cur_sid: Optional[str] = None,
    max_total_wait: float = 360.0,
    slice_timeout: float = 6.0,
) -> bool:
    """Dynamically poll /audio/wait in short slices until Windows WinMM finishes the entire reply
    (prevents 20s hard-cap truncation self-echo on long stories while reacting rapidly to aborts)."""
    deadline = time.monotonic() + max(1.0, float(max_total_wait))
    slice_sec = max(0.5, min(15.0, float(slice_timeout)))
    client = _get_async_client()
    while True:
        if conn is not None:
            if getattr(conn, "client_abort", False):
                return False
            if cur_sid is not None and getattr(conn, "sentence_id", None) != cur_sid:
                return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        req_wait = min(slice_sec, remaining)
        try:
            r = await client.get(
                f"{PC_AUDIO_HOST}/audio/wait",
                params={"timeout": f"{req_wait:.2f}"},
                headers=get_pc_mcp_auth_headers(),
                timeout=req_wait + 3.0,
            )
            if r.status_code != 200:
                return False
            data = r.json() if r.content else {}
            if data.get("done"):
                return True
            if data.get("playing") is False and int(data.get("queued") or 0) == 0:
                return True
        except Exception:
            return False


def play_wav_on_pc_sync(wav_bytes: bytes, volume: Optional[float] = None) -> Tuple[bool, str]:
    """Direct sync playback helper used by 18080 Web Console '测试电脑音响' button."""
    cfg = read_audio_output_config()
    vol = cfg["volume"] if volume is None else float(volume)
    try:
        client = _get_sync_client()
        r = client.post(
            f"{PC_AUDIO_HOST}/audio/play",
            content=wav_bytes,
            headers=get_pc_mcp_auth_headers(
                {
                    "Content-Type": "audio/wav",
                    "X-Turn-Id": "console-test",
                    "X-Volume": str(vol),
                }
            ),
            timeout=8.0,
        )
        if r.status_code == 200:
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as exc:
        return False, str(exc)


def apply_pc_audio_to_tts_patch() -> None:
    """Layer 1 (call BEFORE mei_emotion_router): intercept TTSProviderBase.to_tts_stream
    to capture lossless WAV for Windows PC speakers and emit silent Opus pacing frames to ESP32."""
    from core.providers.tts import base as tts_base_mod
    from core.providers.tts.base import TTSProviderBase, logger as base_logger, TAG as BASE_TAG
    from core.providers.tts.dto.dto import SentenceType

    MarkdownCleaner = getattr(tts_base_mod, "MarkdownCleaner", None)

    if getattr(TTSProviderBase, "_pc_audio_tts_patched", False):
        return

    _orig_to_tts_stream = TTSProviderBase.to_tts_stream

    def _pc_aware_to_tts_stream(self, text, opus_handler=None):
        cfg = read_audio_output_config()
        conn = getattr(self, "conn", None)
        if (
            cfg["target"] != "pc"
            or not getattr(self, "delete_audio_file", True)
            or conn is None
            or opus_handler is None
            or not is_pc_audio_bridge_online()
        ):
            return _orig_to_tts_stream(self, text, opus_handler=opus_handler)

        original_text = text
        clean_text = MarkdownCleaner.clean_markdown(text) if MarkdownCleaner is not None else text
        if getattr(self, "_correct_words_pattern", None):
            clean_text = self._correct_words_pattern.sub(
                lambda m: self.correct_words[m.group(0)], clean_text
            )

        max_repeat_time = 5
        audio_bytes = None
        while max_repeat_time > 0:
            try:
                audio_bytes = asyncio.run(self.text_to_speak(clean_text, None))
                if audio_bytes:
                    break
                max_repeat_time -= 1
            except Exception as exc:
                base_logger.bind(tag=BASE_TAG).warning(
                    f"语音生成失败{5 - max_repeat_time + 1}次: {original_text}，错误: {exc}"
                )
                max_repeat_time -= 1

        if not audio_bytes:
            base_logger.bind(tag=BASE_TAG).error(
                f"语音生成失败: {original_text}，请检查网络或服务是否正常"
            )
            return None

        try:
            wav_bytes, dur_ms = ensure_pcm_wav(audio_bytes, getattr(self, "audio_file_type", "wav"))
        except Exception as exc:
            base_logger.bind(tag=BASE_TAG).warning(
                f"[PCAudioRouter] WAV 转换失败，自动回退 ESP32 喇叭: {exc}"
            )
            return _orig_to_tts_stream(self, text, opus_handler=opus_handler)

        cur_sid = getattr(self, "current_sentence_id", None) or getattr(conn, "sentence_id", None)
        _enqueue_pc_clip(conn, cur_sid, original_text, wav_bytes, dur_ms, cfg["volume"])

        # Push FIRST (triggers sentence_start -> plays WAV on PC + syncs ESP32 OLED subtitle/emotion)
        self.tts_audio_queue.put((SentenceType.FIRST, None, original_text, cur_sid))

        # Emit exact duration of silent 60ms Opus frames so ESP32 stays smoothly in SPEAKING state
        # without playing double audio on its built-in speaker.
        n_frames = max(1, int(math.ceil(dur_ms / 60.0)))
        silent_pkt = _get_silent_opus_frame(getattr(conn, "sample_rate", 16000))
        for _ in range(n_frames):
            opus_handler(silent_pkt)

        base_logger.bind(tag=BASE_TAG).info(
            f"[PCAudioRouter] 已路由至电脑音响 ({dur_ms}ms / {n_frames} 静音占位帧): {original_text}"
        )
        return None

    TTSProviderBase.to_tts_stream = _pc_aware_to_tts_stream
    TTSProviderBase._pc_audio_tts_patched = True


def apply_pc_audio_playback_patches() -> None:
    """Layer 2 (call AFTER mei_emotion_router): trigger PC playback on sentence_start,
    wait for PC finish before tts stop, intercept wakeup replies, and stop PC audio on abort."""
    import core.handle.sendAudioHandle as send_audio_mod
    import core.handle.abortHandle as abort_mod
    from core.providers.tts.dto.dto import SentenceType

    if getattr(send_audio_mod, "_pc_audio_playback_patched", False):
        return

    # 1. Wrap _wait_for_audio_completion so PC mode doesn't pay the 240ms+ ESP32 drain sleep TWICE
    _orig_wait_completion = send_audio_mod._wait_for_audio_completion

    async def _pc_aware_wait_completion(conn):
        if conn is not None:
            skip_sid = getattr(conn, "_pc_audio_skip_wait_once", None)
            if skip_sid is not None and skip_sid == getattr(conn, "sentence_id", None):
                conn._pc_audio_skip_wait_once = None
                if not (getattr(conn, "config", None) or {}).get("enable_stop_tts_notify", False):
                    return
        return await _orig_wait_completion(conn)

    send_audio_mod._wait_for_audio_completion = _pc_aware_wait_completion

    # 2. Patch send_tts_message FIRST (before importing helloHandle) so every module binds to it
    _orig_send_tts_message = send_audio_mod.send_tts_message

    async def _pc_aware_send_tts_message(conn, state, text=None):
        if conn is not None:
            _ensure_conn_pc_state(conn)
            cur_sid = getattr(conn, "sentence_id", None)

            if state == "sentence_start":
                clip = _pop_pc_clip_for_sentence(conn, cur_sid, text)
                if clip is not None:
                    clip_sid, _clip_txt, wav_bytes, dur_ms, vol = clip
                    if conn._pc_audio_active_sid != clip_sid:
                        conn._pc_audio_queued_ms = 0
                    conn._pc_audio_active_sid = clip_sid
                    conn._pc_audio_queued_ms += max(0, int(dur_ms or 0))
                    # Enqueue on host 18900 non-blockingly (~2-5ms) right as OLED subtitle/emotion updates
                    await _post_play_to_pc(conn, clip_sid, wav_bytes, vol)

            elif state == "stop" and conn._pc_audio_active_sid == cur_sid:
                # Drain AudioRateController queue ONLY (no extra 240ms ESP32 speaker sleep!) so every
                # queued sentence_start callback submits its WAV clip to 18900, then dynamically poll
                # /audio/wait until Windows speakers finish the exact last millisecond of audio.
                rc = getattr(conn, "audio_rate_controller", None)
                if rc is not None and hasattr(rc, "queue_empty_event"):
                    await rc.queue_empty_event.wait()
                if not getattr(conn, "client_abort", False) and getattr(conn, "sentence_id", None) == cur_sid:
                    async with conn._pc_audio_send_lock:
                        pass
                    est_sec = max(30.0, min(600.0, (conn._pc_audio_queued_ms / 1000.0) + 30.0))
                    await _wait_pc_audio_done(conn=conn, cur_sid=cur_sid, max_total_wait=est_sec)
                # Abort or a newer turn may have taken over while we awaited completion;
                # NEVER wipe the newer turn's PC audio state or emit a stale `stop` that kills it!
                if getattr(conn, "sentence_id", None) != cur_sid or getattr(conn, "client_abort", False):
                    if conn._pc_audio_active_sid == cur_sid:
                        conn._pc_audio_active_sid = None
                        conn._pc_audio_queued_ms = 0
                    return
                conn._pc_audio_active_sid = None
                conn._pc_audio_queued_ms = 0
                conn._pc_audio_skip_wait_once = cur_sid
                try:
                    return await _orig_send_tts_message(conn, state, text)
                finally:
                    if getattr(conn, "_pc_audio_skip_wait_once", None) == cur_sid:
                        conn._pc_audio_skip_wait_once = None

        return await _orig_send_tts_message(conn, state, text)

    send_audio_mod.send_tts_message = _pc_aware_send_tts_message
    send_audio_mod._pc_audio_playback_patched = True

    # 3. Patch helloHandle.sendAudioMessage so wakeup replies ("我在这里哦！") also play on PC speakers
    import core.handle.helloHandle as hello_mod

    hello_mod.send_tts_message = _pc_aware_send_tts_message
    _orig_hello_send_audio = hello_mod.sendAudioMessage

    async def _pc_aware_hello_send_audio(conn, sentenceType, audios, text, sentence_id=None):
        cfg = read_audio_output_config()
        if (
            cfg["target"] == "pc"
            and sentenceType == SentenceType.FIRST
            and isinstance(audios, list)
            and len(audios) > 0
            and await is_pc_audio_bridge_online_async()
        ):
            try:
                voice = getattr(getattr(conn, "tts", None), "voice", "default") or "default"
                resp = hello_mod.wakeup_words_config.get_wakeup_response(voice)
                wav_path = (resp or {}).get("file_path") or "config/assets/wakeup_words_short.wav"
                if os.path.isfile(wav_path):
                    with open(wav_path, "rb") as f:
                        raw_bytes = f.read()
                    ext = os.path.splitext(wav_path)[1].lstrip(".").lower() or "wav"
                    wav_bytes, dur_ms = ensure_pcm_wav(raw_bytes, ext)
                    cur_sid = getattr(conn, "sentence_id", None)
                    _enqueue_pc_clip(conn, cur_sid, text or "", wav_bytes, dur_ms, cfg["volume"])
                    silent_pkt = _get_silent_opus_frame(getattr(conn, "sample_rate", 16000))
                    audios = [silent_pkt] * max(1, int(math.ceil(dur_ms / 60.0)))
            except Exception as exc:
                print(f"[PCAudioRouter] 唤醒回复转电脑音响跳过，回退 ESP32: {exc}", flush=True)
        return await _orig_hello_send_audio(conn, sentenceType, audios, text, sentence_id)

    hello_mod.sendAudioMessage = _pc_aware_hello_send_audio

    # 4. Patch abortHandle.handleAbortMessage (and any importers) to cut off PC speakers in <5ms
    _orig_abort = abort_mod.handleAbortMessage

    async def _pc_aware_abort(conn):
        if conn is not None:
            _ensure_conn_pc_state(conn)
            _clear_pc_clips(conn)
            was_pc = conn._pc_audio_active_sid is not None or read_audio_output_config()["target"] == "pc"
            conn._pc_audio_active_sid = None
            conn._pc_audio_queued_ms = 0
            if was_pc:
                await _post_stop_to_pc()
        return await _orig_abort(conn)

    abort_mod.handleAbortMessage = _pc_aware_abort
    # Update any module that may already have imported handleAbortMessage
    for mod_name in ("core.handle.receiveAudioHandle", "core.handle.textHandler.abortMessageHandler"):
        mod = __import__("sys").modules.get(mod_name)
        if mod is not None and hasattr(mod, "handleAbortMessage"):
            setattr(mod, "handleAbortMessage", _pc_aware_abort)

    print("[PCAudioRouter] 已挂载电脑音响直出桥接 (48kHz 无损 + 18080 热切换 + 毫秒级打断停止)", flush=True)

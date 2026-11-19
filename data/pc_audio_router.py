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
from typing import Dict, Optional, Tuple

import httpx

ROOT_DIR = "/opt/xiaozhi-esp32-server"
DATA_DIR = os.path.join(ROOT_DIR, "data")
AUDIO_MODE_PATH = os.path.join(DATA_DIR, "audio_output_mode.json")
PC_AUDIO_HOST = os.environ.get("PC_AUDIO_BRIDGE_URL", "http://host.docker.internal:18900").rstrip("/")

_cfg_lock = threading.Lock()
_cfg_mtime = -1.0
_cfg_cache = {"target": "esp32", "volume": 1.0}

_health_lock = threading.Lock()
_health_ts = 0.0
_health_ok = False

_silent_opus_cache: Dict[int, bytes] = {}
_silent_opus_lock = threading.Lock()


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
    os.makedirs(os.path.dirname(AUDIO_MODE_PATH), exist_ok=True)
    with open(AUDIO_MODE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
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
        with httpx.Client(timeout=0.8, trust_env=False) as client:
            r = client.get(f"{PC_AUDIO_HOST}/audio/status")
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
    if not hasattr(conn, "_pc_audio_clip_queue"):
        conn._pc_audio_clip_queue = []
    if not hasattr(conn, "_pc_audio_send_lock"):
        conn._pc_audio_send_lock = asyncio.Lock()
    if not hasattr(conn, "_pc_audio_active_sid"):
        conn._pc_audio_active_sid = None


async def _post_play_to_pc(conn, turn_id: str, wav_bytes: bytes, volume: float) -> bool:
    _ensure_conn_pc_state(conn)
    async with conn._pc_audio_send_lock:
        if getattr(conn, "client_abort", False):
            return False
        if turn_id and getattr(conn, "sentence_id", None) != turn_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                r = await client.post(
                    f"{PC_AUDIO_HOST}/audio/play",
                    content=wav_bytes,
                    headers={
                        "Content-Type": "audio/wav",
                        "X-Turn-Id": str(turn_id or ""),
                        "X-Volume": str(volume),
                    },
                )
                return r.status_code == 200
        except Exception as exc:
            print(f"[PCAudioRouter] 发送音频至电脑音响失败: {exc}", flush=True)
            return False


async def _post_stop_to_pc() -> None:
    try:
        async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
            await client.post(f"{PC_AUDIO_HOST}/audio/stop")
    except Exception:
        pass


async def _wait_pc_audio_done(timeout: float = 10.0) -> None:
    try:
        async with httpx.AsyncClient(timeout=timeout + 2.0, trust_env=False) as client:
            await client.get(f"{PC_AUDIO_HOST}/audio/wait", params={"timeout": str(timeout)})
    except Exception:
        pass


def play_wav_on_pc_sync(wav_bytes: bytes, volume: Optional[float] = None) -> Tuple[bool, str]:
    """Direct sync playback helper used by 18080 Web Console '测试电脑音响' button."""
    cfg = read_audio_output_config()
    vol = cfg["volume"] if volume is None else float(volume)
    try:
        with httpx.Client(timeout=8.0, trust_env=False) as client:
            r = client.post(
                f"{PC_AUDIO_HOST}/audio/play",
                content=wav_bytes,
                headers={"Content-Type": "audio/wav", "X-Turn-Id": "console-test", "X-Volume": str(vol)},
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
        _ensure_conn_pc_state(conn)
        conn._pc_audio_clip_queue.append((cur_sid, original_text, wav_bytes, dur_ms, cfg["volume"]))
        if len(conn._pc_audio_clip_queue) > 24:
            del conn._pc_audio_clip_queue[:-24]

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

    # 1. Patch send_tts_message FIRST (before importing helloHandle) so every module binds to it
    _orig_send_tts_message = send_audio_mod.send_tts_message

    async def _pc_aware_send_tts_message(conn, state, text=None):
        if conn is not None:
            _ensure_conn_pc_state(conn)
            cur_sid = getattr(conn, "sentence_id", None)

            if state == "sentence_start" and conn._pc_audio_clip_queue:
                # Prune stale turns
                conn._pc_audio_clip_queue[:] = [
                    item for item in conn._pc_audio_clip_queue if item[0] == cur_sid
                ]
                if conn._pc_audio_clip_queue:
                    clean_txt = (text or "").strip()
                    idx = next(
                        (i for i, it in enumerate(conn._pc_audio_clip_queue) if (it[1] or "").strip() == clean_txt),
                        0,
                    )
                    clip_sid, _clip_txt, wav_bytes, _dur_ms, vol = conn._pc_audio_clip_queue.pop(idx)
                    conn._pc_audio_active_sid = clip_sid
                    # Enqueue on host 18900 non-blockingly (~2-5ms) right as OLED subtitle/emotion updates
                    await _post_play_to_pc(conn, clip_sid, wav_bytes, vol)

            elif state == "stop" and conn._pc_audio_active_sid == cur_sid:
                # Wait for all silent pacing frames to drain (which dispatches all sentence_starts),
                # then wait for host Windows speakers to actually finish the last millisecond of audio
                # BEFORE sending tts stop to ESP32 (prevents speaker tail from triggering mic!).
                await send_audio_mod._wait_for_audio_completion(conn)
                if not getattr(conn, "client_abort", False) and getattr(conn, "sentence_id", None) == cur_sid:
                    async with conn._pc_audio_send_lock:
                        pass
                    await _wait_pc_audio_done(timeout=10.0)
                conn._pc_audio_active_sid = None

        return await _orig_send_tts_message(conn, state, text)

    send_audio_mod.send_tts_message = _pc_aware_send_tts_message
    send_audio_mod._pc_audio_playback_patched = True

    # 2. Patch helloHandle.sendAudioMessage so wakeup replies ("我在这里哦！") also play on PC speakers
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
            and is_pc_audio_bridge_online()
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
                    _ensure_conn_pc_state(conn)
                    conn._pc_audio_clip_queue.append((cur_sid, text or "", wav_bytes, dur_ms, cfg["volume"]))
                    silent_pkt = _get_silent_opus_frame(getattr(conn, "sample_rate", 16000))
                    audios = [silent_pkt] * max(1, int(math.ceil(dur_ms / 60.0)))
            except Exception as exc:
                print(f"[PCAudioRouter] 唤醒回复转电脑音响跳过，回退 ESP32: {exc}", flush=True)
        return await _orig_hello_send_audio(conn, sentenceType, audios, text, sentence_id)

    hello_mod.sendAudioMessage = _pc_aware_hello_send_audio

    # 3. Patch abortHandle.handleAbortMessage (and any importers) to cut off PC speakers in <5ms
    _orig_abort = abort_mod.handleAbortMessage

    async def _pc_aware_abort(conn):
        if conn is not None:
            _ensure_conn_pc_state(conn)
            conn._pc_audio_clip_queue.clear()
            was_pc = conn._pc_audio_active_sid is not None or read_audio_output_config()["target"] == "pc"
            conn._pc_audio_active_sid = None
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

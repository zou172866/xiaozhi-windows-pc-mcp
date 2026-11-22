"""Route Xiaozhi TTS & wakeup audio directly to Windows PC speakers (48kHz lossless WAV)
while keeping ESP32-S3 OLED subtitles, emotions, speaking state, and abort in exact lockstep.

Hot-switched via data/audio_output_mode.json ("esp32" | "pc") with zero container restart.
Falls back seamlessly to ESP32 built-in speaker if host 18900 audio bridge is unreachable.
"""

from __future__ import annotations

import asyncio
import collections
import functools
import io
import json
import math
import os
import queue
import struct
import threading
import time
import wave
import weakref
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

import patch_registry
import runtime_events
import runtime_settings
import xz_common

ROOT_DIR = xz_common.ROOT_DIR
DATA_DIR = xz_common.STATE_DIR
_DEFAULT_AUDIO_MODE_PATH = os.path.join(xz_common.STATE_DIR, "audio_output_mode.json")
AUDIO_MODE_PATH = _DEFAULT_AUDIO_MODE_PATH
PC_MCP_TOKEN_PATH = xz_common.PC_MCP_TOKEN_PATH
PC_AUDIO_HOST = os.environ.get("PC_AUDIO_BRIDGE_URL", "http://host.docker.internal:18900").rstrip("/")

_cfg_lock = threading.Lock()
_cfg_mtime = -1.0
_cfg_cache = {"target": "esp32", "volume": 1.0}
_cfg_file_cache = xz_common.MtimeJsonCache(
    normalizer=runtime_settings.normalize_audio_output,
    default_factory=lambda: dict(_cfg_cache),
)
_cfg_file_cache_path = ""

_health_lock = threading.Lock()
_health_ts = 0.0
_health_ok = False

_silent_opus_cache: Dict[int, bytes] = {}
_silent_opus_lock = threading.Lock()

# C-5 / §8 #2: Bound wait on AudioRateController.queue_empty_event (upstream _send_loop exits
# without setting queue_empty_event if send_audio_callback raises or pending_send_task cancels).
QUEUE_EMPTY_WAIT_TIMEOUT = 10.0
QUEUE_EMPTY_POLL_SLICE = 1.0

_sync_client_lock = threading.Lock()
_sync_client: Optional[httpx.Client] = None
_async_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)


def read_pc_mcp_token() -> str:
    """Read shared local 18900 Bearer token via env-first xz_common (A-1)."""
    return xz_common.get_pc_mcp_token(PC_MCP_TOKEN_PATH)


def get_pc_mcp_auth_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    return xz_common.get_pc_mcp_auth_headers(extra, token_path=PC_MCP_TOKEN_PATH)


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
    xz_common.atomic_write_json(path, payload)


def read_audio_output_config() -> dict:
    """Read audio_output settings from unified runtime_settings.json (or custom monkeypatched path via xz_common)."""
    global _cfg_mtime, _cfg_cache, _cfg_file_cache_path
    if AUDIO_MODE_PATH == _DEFAULT_AUDIO_MODE_PATH:
        return runtime_settings.get_audio_output()
    with _cfg_lock:
        if _cfg_mtime < 0 or _cfg_file_cache_path != AUDIO_MODE_PATH:
            _cfg_file_cache.invalidate()
            _cfg_file_cache_path = AUDIO_MODE_PATH
        res = _cfg_file_cache.get(AUDIO_MODE_PATH)
        _cfg_cache = dict(res)
        sig = _cfg_file_cache._inner._sig
        _cfg_mtime = (sig[0] / 1_000_000_000.0) if sig else -1.0
        return dict(res)


def write_audio_output_config(target: str, volume: Optional[float] = None) -> dict:
    global _cfg_mtime, _cfg_cache, _cfg_file_cache_path
    if AUDIO_MODE_PATH == _DEFAULT_AUDIO_MODE_PATH:
        return runtime_settings.set_audio_output(target=target, volume=volume)
    cur = read_audio_output_config()
    t = str(target or cur["target"]).strip().lower()
    if t not in ("esp32", "pc"):
        raise ValueError(f"Unsupported audio output target: {target}")
    vol = cur["volume"] if volume is None else round(max(0.2, min(1.5, float(volume))), 2)
    payload = {"target": t, "volume": vol}
    _atomic_write_json(AUDIO_MODE_PATH, payload)
    with _cfg_lock:
        _cfg_file_cache_path = AUDIO_MODE_PATH
        _cfg_file_cache.set(AUDIO_MODE_PATH, payload)
        _cfg_cache = dict(payload)
        sig = _cfg_file_cache._inner._sig
        _cfg_mtime = (sig[0] / 1_000_000_000.0) if sig else -1.0
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


def pcm16_to_wav(pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> Tuple[bytes, int]:
    """Wrap raw 16-bit little-endian PCM bytes with a 44-byte RIFF/WAVE header (zero transcoding)."""
    sr = max(8000, int(sample_rate or 16000))
    ch = max(1, int(channels or 1))
    data = bytes(pcm_bytes or b"")
    frame_bytes = ch * 2
    rem = len(data) % frame_bytes
    if rem:
        data = data + (b"\x00" * (frame_bytes - rem))
    n_frames = len(data) // frame_bytes
    dur_ms = int(round((n_frames / float(sr)) * 1000.0)) if n_frames > 0 else 0
    if n_frames > 0 and dur_ms <= 0:
        dur_ms = 1
    data_len = len(data)
    byte_rate = sr * frame_bytes
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_len,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        ch,
        sr,
        byte_rate,
        frame_bytes,
        16,
        b"data",
        data_len,
    )
    return header + data, dur_ms


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
    if not hasattr(conn, "_pc_stream_pending_chunks"):
        conn._pc_stream_pending_chunks = collections.deque()
    if not hasattr(conn, "_pc_stream_drain_task"):
        conn._pc_stream_drain_task = None
    if not hasattr(conn, "_pc_audio_abort_gen"):
        conn._pc_audio_abort_gen = 0
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


def _clear_pc_stream_chunks(conn):
    _ensure_conn_pc_state(conn)
    with conn._pc_audio_queue_lock:
        conn._pc_audio_abort_gen = int(getattr(conn, "_pc_audio_abort_gen", 0) or 0) + 1
        conn._pc_stream_pending_chunks.clear()
        drain_task = getattr(conn, "_pc_stream_drain_task", None)
    if drain_task is not None and not drain_task.done():
        try:
            drain_task.cancel()
        except Exception:
            pass
    return drain_task


async def _drain_pc_stream_chunks(conn) -> None:
    _ensure_conn_pc_state(conn)
    cur_task = asyncio.current_task()
    try:
        while True:
            with conn._pc_audio_queue_lock:
                if not conn._pc_stream_pending_chunks:
                    break
                chunk_sid, chunk_gen, wav_bytes, vol = conn._pc_stream_pending_chunks.popleft()
            if getattr(conn, "client_abort", False):
                continue
            if chunk_sid and getattr(conn, "sentence_id", None) != chunk_sid:
                continue
            if int(getattr(conn, "_pc_audio_abort_gen", 0) or 0) != chunk_gen:
                continue
            await _post_play_to_pc(conn, chunk_sid, wav_bytes, vol)
    except asyncio.CancelledError:
        raise
    finally:
        reschedule = False
        with conn._pc_audio_queue_lock:
            if getattr(conn, "_pc_stream_drain_task", None) is cur_task:
                conn._pc_stream_drain_task = None
            if conn._pc_stream_pending_chunks and not getattr(conn, "client_abort", False):
                reschedule = True
        if reschedule:
            _schedule_pc_stream_drain(conn)


def _schedule_pc_stream_drain(conn) -> bool:
    loop = getattr(conn, "loop", None)
    if loop is None or loop.is_closed():
        return False

    def _ensure_task():
        with conn._pc_audio_queue_lock:
            existing = getattr(conn, "_pc_stream_drain_task", None)
            if existing is not None and not existing.done():
                return
            if not conn._pc_stream_pending_chunks:
                return
            conn._pc_stream_drain_task = loop.create_task(_drain_pc_stream_chunks(conn))

    try:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _ensure_task()
        else:
            loop.call_soon_threadsafe(_ensure_task)
        return True
    except Exception:
        return False


def _enqueue_pc_stream_wav(conn, cur_sid, wav_bytes: bytes, dur_ms: int, volume: float) -> None:
    """Queue a lossless streaming PCM WAV chunk to Windows PC speakers in strict FIFO order."""
    if conn is None or not wav_bytes:
        return
    _ensure_conn_pc_state(conn)
    with conn._pc_audio_queue_lock:
        if conn._pc_audio_active_sid != cur_sid:
            conn._pc_audio_queued_ms = 0
        conn._pc_audio_active_sid = cur_sid
        conn._pc_audio_queued_ms += max(0, int(dur_ms or 0))
        abort_gen = int(getattr(conn, "_pc_audio_abort_gen", 0) or 0)
        conn._pc_stream_pending_chunks.append((cur_sid, abort_gen, wav_bytes, float(volume)))
        while len(conn._pc_stream_pending_chunks) > 256:
            conn._pc_stream_pending_chunks.popleft()
    if not _schedule_pc_stream_drain(conn):
        # Fallback for sync/unit-test environments without an active conn.loop
        while True:
            with conn._pc_audio_queue_lock:
                if not conn._pc_stream_pending_chunks:
                    break
                c_sid, c_gen, w_bytes, c_vol = conn._pc_stream_pending_chunks.popleft()
            if getattr(conn, "client_abort", False):
                continue
            if c_sid and getattr(conn, "sentence_id", None) != c_sid:
                continue
            if int(getattr(conn, "_pc_audio_abort_gen", 0) or 0) != c_gen:
                continue
            play_wav_on_pc_sync(w_bytes, volume=c_vol)


async def _wait_pc_stream_drain(conn, cur_sid: Optional[str], timeout: float = 15.0) -> None:
    """Wait until all queued streaming PCM WAV chunks for `cur_sid` have been submitted to 18900."""
    if conn is None:
        return
    _ensure_conn_pc_state(conn)
    deadline = time.monotonic() + max(0.5, float(timeout))
    while time.monotonic() < deadline:
        if getattr(conn, "client_abort", False):
            return
        if cur_sid is not None and getattr(conn, "sentence_id", None) != cur_sid:
            return
        with conn._pc_audio_queue_lock:
            task = getattr(conn, "_pc_stream_drain_task", None)
            has_pending = bool(conn._pc_stream_pending_chunks)
        if task is not None and not task.done():
            rem = max(0.05, deadline - time.monotonic())
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=min(1.0, rem))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                return
            continue
        if has_pending:
            await _drain_pc_stream_chunks(conn)
            continue
        break


STREAMING_PC_SUPPORTED_RATES: Dict[str, Tuple[int, ...]] = {
    "alibl_stream": (8000, 16000, 22050, 24000, 44100, 48000),
    "huoshan_double_stream": (8000, 16000, 22050, 24000, 32000, 44100, 48000),
    "minimax_httpstream": (8000, 16000, 22050, 24000, 32000, 44100),
}
STREAMING_PC_DEFAULT_RATES: Dict[str, int] = {
    "alibl_stream": 48000,
    "huoshan_double_stream": 48000,
    "minimax_httpstream": 44100,
}


def resolve_streaming_pc_sample_rate(provider_kind: str) -> int:
    """Return the highest safe lossless PCM sample rate for a streaming TTS provider in PC speaker mode."""
    supported = STREAMING_PC_SUPPORTED_RATES.get(provider_kind, (16000, 24000, 48000))
    default_sr = STREAMING_PC_DEFAULT_RATES.get(provider_kind, max(supported))
    raw_env = (os.environ.get("PC_AUDIO_STREAM_SAMPLE_RATE") or "").strip()
    if raw_env:
        try:
            req = int(raw_env)
            if req in supported:
                return req
            # Clamp to highest supported rate <= req (or max supported if req > max)
            le_rates = [r for r in supported if r <= req]
            return max(le_rates) if le_rates else min(supported)
        except (TypeError, ValueError):
            pass
    return default_sr


def _get_or_init_pc_stream_ctx(tts_obj) -> dict:
    ctx = getattr(tts_obj, "_pc_stream_ctx", None)
    if not isinstance(ctx, dict):
        ctx = {
            "lock": threading.Lock(),
            "active": False,
            "sid": None,
            "sample_rate": 48000,
            "volume": 1.0,
            "first_chunk_ms": 200,
            "chunk_ms": 320,
            "pcm_buf": bytearray(),
            "sent_any": False,
            "total_samples": 0,
            "emitted_frames": 0,
        }
        tts_obj._pc_stream_ctx = ctx
    return ctx


def _reset_pc_stream_ctx(
    tts_obj,
    *,
    active: bool,
    sid: Optional[str] = None,
    sample_rate: int = 48000,
    volume: float = 1.0,
) -> dict:
    ctx = _get_or_init_pc_stream_ctx(tts_obj)
    with ctx["lock"]:
        ctx["active"] = bool(active)
        ctx["sid"] = sid
        ctx["sample_rate"] = int(sample_rate or 48000)
        ctx["volume"] = float(volume if volume is not None else 1.0)
        ctx["pcm_buf"].clear()
        ctx["sent_any"] = False
        ctx["total_samples"] = 0
        ctx["emitted_frames"] = 0
    return ctx


def _is_live_pc_stream_callback(tts_obj, callback) -> bool:
    ctx = getattr(tts_obj, "_pc_stream_ctx", None)
    if not isinstance(ctx, dict) or not ctx.get("active"):
        return False
    handle_opus = getattr(tts_obj, "handle_opus", None)
    if handle_opus is None or callback is None:
        return False
    return callback == handle_opus


def _flush_pc_stream_buffer(
    tts_obj,
    *,
    end_of_stream: bool,
    callback: Optional[Callable[[bytes], Any]] = None,
) -> int:
    """Flush buffered streaming PCM to Windows PC speakers and emit matched 60ms silent Opus frames."""
    ctx = getattr(tts_obj, "_pc_stream_ctx", None)
    conn = getattr(tts_obj, "conn", None)
    if not isinstance(ctx, dict) or not ctx.get("active") or conn is None:
        return 0
    cb = callback or getattr(tts_obj, "handle_opus", None)
    if cb is None:
        return 0

    wav_bytes: Optional[bytes] = None
    dur_ms = 0
    emit_n = 0
    cur_sid = None
    vol = 1.0
    sr_esp32 = int(getattr(conn, "sample_rate", 16000) or 16000)

    with ctx["lock"]:
        if not ctx.get("active"):
            return 0
        cur_sid = ctx.get("sid") or getattr(conn, "sentence_id", None) or getattr(tts_obj, "current_sentence_id", None)
        if getattr(conn, "client_abort", False) or (
            cur_sid and getattr(conn, "sentence_id", None) and conn.sentence_id != cur_sid
        ):
            ctx["pcm_buf"].clear()
            return 0

        buf: bytearray = ctx["pcm_buf"]
        sr = max(8000, int(ctx.get("sample_rate", 48000) or 48000))
        vol = float(ctx.get("volume", 1.0))

        min_ms = ctx["first_chunk_ms"] if not ctx["sent_any"] else ctx["chunk_ms"]
        min_bytes = int(sr * 2 * min_ms / 1000.0)
        if min_bytes % 2 == 1:
            min_bytes += 1

        if end_of_stream or len(buf) >= min_bytes:
            if end_of_stream and (len(buf) % 2 == 1):
                buf.append(0)
            n_bytes = len(buf) - (len(buf) % 2)
            if n_bytes > 0:
                chunk = bytes(buf[:n_bytes])
                del buf[:n_bytes]
                wav_bytes, dur_ms = pcm16_to_wav(chunk, sr, channels=1)
                ctx["sent_any"] = True
                ctx["total_samples"] += n_bytes // 2

            total_samples = ctx["total_samples"]
            if total_samples > 0:
                total_ms = (total_samples / float(sr)) * 1000.0
                if end_of_stream:
                    target_frames = max(1, int(math.ceil(total_ms / 60.0)))
                else:
                    target_frames = int(math.floor(total_ms / 60.0))
                emit_n = max(0, target_frames - ctx["emitted_frames"])
                ctx["emitted_frames"] += emit_n

    if wav_bytes:
        _enqueue_pc_stream_wav(conn, cur_sid, wav_bytes, dur_ms, vol)
    if emit_n > 0:
        silent_pkt = _get_silent_opus_frame(sr_esp32)
        for _ in range(emit_n):
            cb(silent_pkt)
    return emit_n


def _feed_pc_stream_pcm(
    tts_obj,
    pcm_data: bytes,
    *,
    end_of_stream: bool,
    callback: Callable[[bytes], Any],
) -> None:
    ctx = getattr(tts_obj, "_pc_stream_ctx", None)
    if not isinstance(ctx, dict) or not ctx.get("active"):
        return
    if pcm_data:
        with ctx["lock"]:
            if ctx.get("active"):
                ctx["pcm_buf"].extend(pcm_data)
    _flush_pc_stream_buffer(tts_obj, end_of_stream=bool(end_of_stream), callback=callback)


class _PCAwareAudioQueue(queue.Queue):
    """queue.Queue subclass that flushes buffered streaming PCM before SentenceType.FIRST/LAST boundaries."""

    def __init__(self, tts_obj):
        super().__init__()
        self._tts_ref = weakref.ref(tts_obj)

    def put(self, item, block=True, timeout=None):
        tts_obj = self._tts_ref()
        if tts_obj is not None and isinstance(item, tuple) and item:
            from core.providers.tts.dto.dto import SentenceType

            stype = item[0]
            if stype in (SentenceType.FIRST, SentenceType.LAST):
                ctx = getattr(tts_obj, "_pc_stream_ctx", None)
                if isinstance(ctx, dict) and ctx.get("active"):
                    with ctx["lock"]:
                        has_buf = bool(ctx["pcm_buf"]) or (
                            stype == SentenceType.LAST and ctx["total_samples"] > 0
                        )
                    if has_buf:
                        _flush_pc_stream_buffer(tts_obj, end_of_stream=True)
        return super().put(item, block=block, timeout=timeout)


class _AliBLWSProxy:
    """Transparent WebSocket proxy that injects PC-mode 48kHz PCM sample_rate into AliBL run-task."""

    def __init__(self, ws, tts_obj):
        self._ws = ws
        self._tts_ref = weakref.ref(tts_obj)

    async def send(self, data, *args, **kwargs):
        tts_obj = self._tts_ref()
        if tts_obj is not None and isinstance(data, str) and "run-task" in data:
            ctx = getattr(tts_obj, "_pc_stream_ctx", None)
            if isinstance(ctx, dict) and ctx.get("active"):
                try:
                    msg = json.loads(data)
                    if (msg.get("header") or {}).get("action") == "run-task":
                        params = (msg.get("payload") or {}).get("parameters")
                        if isinstance(params, dict) and str(params.get("format", "pcm")).lower() == "pcm":
                            params["sample_rate"] = int(ctx["sample_rate"])
                            data = json.dumps(msg, ensure_ascii=False)
                except Exception:
                    pass
        return await self._ws.send(data, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._ws, name)


def _ensure_pc_stream_hooks(tts_obj) -> None:
    """Idempotently attach _PCAwareAudioQueue and PC-aware opus_encoder interceptor onto a streaming TTS instance."""
    cur_q = getattr(tts_obj, "tts_audio_queue", None)
    if cur_q is not None and not isinstance(cur_q, _PCAwareAudioQueue):
        new_q = _PCAwareAudioQueue(tts_obj)
        while True:
            try:
                new_q.put_nowait(cur_q.get_nowait())
            except queue.Empty:
                break
        tts_obj.tts_audio_queue = new_q

    enc = getattr(tts_obj, "opus_encoder", None)
    if enc is not None and not getattr(enc, "_pc_stream_wrapped", False):
        orig_encode = enc.encode_pcm_to_opus_stream
        tts_ref = weakref.ref(tts_obj)

        @functools.wraps(orig_encode)
        def _pc_aware_encode(pcm_data: bytes, end_of_stream: bool = False, callback=None, *args, **kwargs):
            owner = tts_ref()
            if owner is not None and _is_live_pc_stream_callback(owner, callback):
                _feed_pc_stream_pcm(owner, pcm_data, end_of_stream=bool(end_of_stream), callback=callback)
                return None
            return orig_encode(pcm_data, end_of_stream, callback, *args, **kwargs)

        enc.encode_pcm_to_opus_stream = _pc_aware_encode
        enc._pc_stream_wrapped = True


def attach_streaming_pc_audio_hooks(tts_cls, *, provider_kind: str) -> None:
    """Attach PC 48kHz/44.1kHz lossless speaker passthrough to AliBLTTS / HuoshanDoubleStreamTTS / MinimaxTTSHTTPStream."""
    if getattr(tts_cls, "_pc_audio_stream_patched", False):
        return

    _orig_open = tts_cls.open_audio_channels

    @functools.wraps(_orig_open)
    async def _pc_stream_open_audio_channels(self, conn, *args, **kwargs):
        _ensure_pc_stream_hooks(self)
        res = await _orig_open(self, conn, *args, **kwargs)
        _ensure_pc_stream_hooks(self)
        return res

    tts_cls.open_audio_channels = _pc_stream_open_audio_channels

    if provider_kind == "alibl_stream":
        _orig_ensure_conn = tts_cls._ensure_connection
        _orig_start_session = tts_cls.start_session

        @functools.wraps(_orig_ensure_conn)
        async def _pc_alibl_ensure_connection(self, *args, **kwargs):
            ws = await _orig_ensure_conn(self, *args, **kwargs)
            if ws is not None and not isinstance(ws, _AliBLWSProxy):
                proxy = _AliBLWSProxy(ws, self)
                self.ws = proxy
                return proxy
            return ws

        @functools.wraps(_orig_start_session)
        async def _pc_alibl_start_session(self, session_id, *args, **kwargs):
            _ensure_pc_stream_hooks(self)
            cfg = read_audio_output_config()
            fmt_ok = str(getattr(self, "format", "pcm") or "pcm").strip().lower() == "pcm"
            is_pc = bool(
                cfg["target"] == "pc"
                and fmt_ok
                and getattr(self, "conn", None) is not None
                and await is_pc_audio_bridge_online_async()
            )
            sr = resolve_streaming_pc_sample_rate("alibl_stream")
            _reset_pc_stream_ctx(
                self,
                active=is_pc,
                sid=session_id or getattr(getattr(self, "conn", None), "sentence_id", None),
                sample_rate=sr,
                volume=cfg["volume"],
            )
            return await _orig_start_session(self, session_id, *args, **kwargs)

        tts_cls._ensure_connection = _pc_alibl_ensure_connection
        tts_cls.start_session = _pc_alibl_start_session

    elif provider_kind == "huoshan_double_stream":
        _orig_start_session = tts_cls.start_session
        _orig_to_tts = getattr(tts_cls, "to_tts", None)

        @functools.wraps(_orig_start_session)
        async def _pc_huoshan_start_session(self, session_id, *args, **kwargs):
            _ensure_pc_stream_hooks(self)
            conn = getattr(self, "conn", None)
            cfg = read_audio_output_config()
            is_pc = bool(
                cfg["target"] == "pc"
                and conn is not None
                and await is_pc_audio_bridge_online_async()
            )
            base_sr = int(getattr(conn, "sample_rate", 16000) or 16000)
            pc_sr = resolve_streaming_pc_sample_rate("huoshan_double_stream")
            if isinstance(getattr(self, "audio_params", None), dict):
                self.audio_params["sample_rate"] = pc_sr if is_pc else base_sr
            _reset_pc_stream_ctx(
                self,
                active=is_pc,
                sid=session_id or getattr(conn, "sentence_id", None),
                sample_rate=pc_sr,
                volume=cfg["volume"],
            )
            return await _orig_start_session(self, session_id, *args, **kwargs)

        tts_cls.start_session = _pc_huoshan_start_session

        if callable(_orig_to_tts):
            @functools.wraps(_orig_to_tts)
            def _pc_huoshan_to_tts(self, text, *args, **kwargs):
                params = getattr(self, "audio_params", None)
                prev_sr = params.get("sample_rate") if isinstance(params, dict) else None
                enc_sr = getattr(getattr(self, "opus_encoder", None), "sample_rate", None)
                conn_sr = getattr(getattr(self, "conn", None), "sample_rate", None)
                target_sr = enc_sr or conn_sr
                if isinstance(params, dict) and target_sr:
                    params["sample_rate"] = int(target_sr)
                try:
                    return _orig_to_tts(self, text, *args, **kwargs)
                finally:
                    if isinstance(params, dict) and prev_sr is not None:
                        params["sample_rate"] = prev_sr

            tts_cls.to_tts = _pc_huoshan_to_tts

    elif provider_kind == "minimax_httpstream":
        _orig_text_to_speak = tts_cls.text_to_speak
        _orig_to_tts = getattr(tts_cls, "to_tts", None)

        @functools.wraps(_orig_text_to_speak)
        async def _pc_minimax_text_to_speak(self, text, is_last, *args, **kwargs):
            _ensure_pc_stream_hooks(self)
            conn = getattr(self, "conn", None)
            cur_sid = getattr(conn, "sentence_id", None) or getattr(self, "current_sentence_id", None)
            ctx = _get_or_init_pc_stream_ctx(self)
            audio_setting = getattr(self, "audio_setting", None)
            fmt_ok = str((audio_setting or {}).get("format", "pcm") if isinstance(audio_setting, dict) else "pcm").strip().lower() == "pcm"
            base_sr = int(getattr(conn, "sample_rate", 16000) or 16000)
            pc_sr = resolve_streaming_pc_sample_rate("minimax_httpstream")

            # Lock PC mode decision per turn (sentence_id) so multi-sentence turns stay format-consistent
            need_eval = (ctx.get("sid") != cur_sid) or (cur_sid is None)
            if need_eval:
                cfg = read_audio_output_config()
                is_pc = bool(
                    cfg["target"] == "pc"
                    and fmt_ok
                    and conn is not None
                    and await is_pc_audio_bridge_online_async()
                )
                _reset_pc_stream_ctx(
                    self,
                    active=is_pc,
                    sid=cur_sid,
                    sample_rate=pc_sr,
                    volume=cfg["volume"],
                )
            else:
                is_pc = bool(ctx.get("active"))

            if isinstance(audio_setting, dict):
                audio_setting["sample_rate"] = pc_sr if is_pc else base_sr
            return await _orig_text_to_speak(self, text, is_last, *args, **kwargs)

        tts_cls.text_to_speak = _pc_minimax_text_to_speak

        if callable(_orig_to_tts):
            @functools.wraps(_orig_to_tts)
            def _pc_minimax_to_tts(self, text, *args, **kwargs):
                aset = getattr(self, "audio_setting", None)
                prev_sr = aset.get("sample_rate") if isinstance(aset, dict) else None
                enc_sr = getattr(getattr(self, "opus_encoder", None), "sample_rate", None)
                conn_sr = getattr(getattr(self, "conn", None), "sample_rate", None)
                target_sr = enc_sr or conn_sr
                if isinstance(aset, dict) and target_sr:
                    aset["sample_rate"] = int(target_sr)
                try:
                    return _orig_to_tts(self, text, *args, **kwargs)
                finally:
                    if isinstance(aset, dict) and prev_sr is not None:
                        aset["sample_rate"] = prev_sr

            tts_cls.to_tts = _pc_minimax_to_tts

    tts_cls._pc_audio_stream_patched = True


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


def _load_wav_for_pc_sync(wav_path: str) -> Tuple[bytes, int]:
    with open(wav_path, "rb") as f:
        raw_bytes = f.read()
    ext = os.path.splitext(wav_path)[1].lstrip(".").lower() or "wav"
    return ensure_pcm_wav(raw_bytes, ext)


async def _wait_rate_controller_empty(
    conn,
    rc,
    cur_sid: Optional[str],
    *,
    stall_timeout: Optional[float] = None,
) -> bool:
    """Bounded wait for AudioRateController.queue_empty_event (C-5 / §8 #2).

    Upstream `AudioRateController._send_loop()` exits on send_audio_callback exception or
    cancellation WITHOUT calling `queue_empty_event.set()`. At the same time, long stories
    legitimately drain 60ms pacing frames over 30-120s. Therefore:
    - Return immediately if `queue_empty_event` is already set.
    - Abort immediately if `conn.client_abort`, turn changed, or `pending_send_task` died.
    - Await `asyncio.wait_for(rc.queue_empty_event.wait(), timeout=step)` in bounded slices,
      tracking `len(rc.queue)` progress and failing soft after `stall_timeout` (default 10.0s)
      without progress or overall turn ceiling.
    """
    ev = getattr(rc, "queue_empty_event", None)
    if ev is None or ev.is_set():
        return True

    eff_stall = max(0.05, float(QUEUE_EMPTY_WAIT_TIMEOUT if stall_timeout is None else stall_timeout))
    queued_ms = max(0, int(getattr(conn, "_pc_audio_queued_ms", 0) or 0))
    hard_cap = max(eff_stall, min(600.0, (queued_ms / 1000.0) + max(15.0, eff_stall)))

    now = time.monotonic()
    hard_deadline = now + hard_cap
    last_progress = now
    q_obj = getattr(rc, "queue", None)
    prev_qlen = len(q_obj) if q_obj is not None else -1

    while not ev.is_set():
        if getattr(conn, "client_abort", False) or getattr(conn, "sentence_id", None) != cur_sid:
            return False
        sender_task = getattr(rc, "pending_send_task", None)
        if sender_task is not None and callable(getattr(sender_task, "done", None)) and sender_task.done():
            print(
                "[PCAudioRouter] AudioRateController 发送任务已退出但 queue_empty_event 未置位，提前结束等待 (C-5)",
                flush=True,
            )
            return False

        now = time.monotonic()
        rem_stall = eff_stall - (now - last_progress)
        rem_hard = hard_deadline - now
        if rem_stall <= 0 or rem_hard <= 0:
            print(
                f"[PCAudioRouter] 等待 AudioRateController.queue_empty_event 超时 ({eff_stall:.1f}s 无进展)，强制继续收尾 (C-5)",
                flush=True,
            )
            return False

        step = min(max(0.02, float(QUEUE_EMPTY_POLL_SLICE)), rem_stall, rem_hard)
        try:
            await asyncio.wait_for(ev.wait(), timeout=step)
            return True
        except asyncio.TimeoutError:
            if q_obj is not None:
                cur_qlen = len(q_obj)
                if cur_qlen < prev_qlen:
                    prev_qlen = cur_qlen
                    last_progress = time.monotonic()
    return True


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
        if patch_registry.get_patch_status("pc_audio_tts") is None:
            patch_registry.record_patch("pc_audio_tts", "ok", target="TTSProviderBase.to_tts_stream", detail="already applied")
        return

    _orig_to_tts_stream = TTSProviderBase.to_tts_stream
    ok_sig, sig_det = patch_registry.verify_signature_contract(
        _orig_to_tts_stream, required_params=("self", "text"), require_async=False
    )
    if not ok_sig:
        patch_registry.record_patch(
            "pc_audio_tts",
            "skipped_contract",
            target="TTSProviderBase.to_tts_stream",
            detail=sig_det,
            contract_ok=False,
            category="experience",
        )
        print(f"[PCAudioRouter] 跳过挂载 TTS 拦截层（签名契约不匹配）: {sig_det}", flush=True)
        return

    _pc_tts_tls = threading.local()

    def _run_tts_speak_for_pc(tts_obj, clean_text: str):
        sync_fn = getattr(tts_obj, "_text_to_speak_sync", None)
        has_instance_override = "text_to_speak" in getattr(tts_obj, "__dict__", {})
        cls_speak = getattr(type(tts_obj), "text_to_speak", None)
        expected_ref = getattr(type(tts_obj), "_patched_tts_speak_ref", None)
        if callable(sync_fn) and not has_instance_override and (expected_ref is None or cls_speak is expected_ref):
            return sync_fn(clean_text)

        loop = getattr(_pc_tts_tls, "loop", None)
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            _pc_tts_tls.loop = loop
        return loop.run_until_complete(tts_obj.text_to_speak(clean_text, None))

    @functools.wraps(_orig_to_tts_stream)
    def _pc_aware_to_tts_stream(self, text, *args, **kwargs):
        opus_handler = kwargs.get("opus_handler", args[0] if args else None)
        cfg = read_audio_output_config()
        conn = getattr(self, "conn", None)
        if (
            cfg["target"] != "pc"
            or not getattr(self, "delete_audio_file", True)
            or conn is None
            or opus_handler is None
            or not is_pc_audio_bridge_online()
        ):
            return _orig_to_tts_stream(self, text, *args, **kwargs)

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
                audio_bytes = _run_tts_speak_for_pc(self, clean_text)
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
            return _orig_to_tts_stream(self, text, *args, **kwargs)

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
    patch_registry.record_patch(
        "pc_audio_tts",
        "ok",
        target="TTSProviderBase.to_tts_stream",
        detail=sig_det,
        contract_ok=True,
        category="experience",
    )


def apply_pc_audio_playback_patches() -> None:
    """Layer 2 (call AFTER mei_emotion_router): trigger PC playback on sentence_start,
    wait for PC finish before tts stop, intercept wakeup replies, and stop PC audio on abort."""
    import core.handle.sendAudioHandle as send_audio_mod
    import core.handle.abortHandle as abort_mod
    from core.providers.tts.dto.dto import SentenceType

    if getattr(send_audio_mod, "_pc_audio_playback_patched", False):
        if patch_registry.get_patch_status("pc_audio_playback") is None:
            patch_registry.record_patch("pc_audio_playback", "ok", target="sendAudioHandle/helloHandle/abortHandle", detail="already applied")
        return

    # 1. Wrap _wait_for_audio_completion so PC mode doesn't pay the 240ms+ ESP32 drain sleep TWICE
    _orig_wait_completion = send_audio_mod._wait_for_audio_completion
    _orig_send_tts_message = send_audio_mod.send_tts_message
    import core.handle.helloHandle as hello_mod
    _orig_hello_send_audio = hello_mod.sendAudioMessage
    _orig_abort = abort_mod.handleAbortMessage

    pb_checks = [
        ("sendAudioHandle._wait_for_audio_completion", _orig_wait_completion, ("conn",), True),
        ("sendAudioHandle.send_tts_message", _orig_send_tts_message, ("conn", "state"), True),
        ("helloHandle.sendAudioMessage", _orig_hello_send_audio, ("conn", "sentenceType", "audios", "text"), True),
        ("abortHandle.handleAbortMessage", _orig_abort, ("conn",), True),
    ]
    pb_failures = []
    for cname, cfn, req_p, req_a in pb_checks:
        ok_s, det = patch_registry.verify_signature_contract(cfn, required_params=req_p, require_async=req_a)
        if not ok_s:
            pb_failures.append(f"{cname}: {det}")
    if pb_failures:
        patch_registry.record_patch(
            "pc_audio_playback",
            "skipped_contract",
            target="sendAudioHandle/helloHandle/abortHandle",
            detail="; ".join(pb_failures),
            contract_ok=False,
            category="experience",
        )
        print(
            f"[PCAudioRouter] 跳过挂载播放同步层（签名契约不匹配）: {'; '.join(pb_failures)}",
            flush=True,
        )
        return

    @functools.wraps(_orig_wait_completion)
    async def _pc_aware_wait_completion(conn, *args, **kwargs):
        if conn is not None:
            skip_sid = getattr(conn, "_pc_audio_skip_wait_once", None)
            if skip_sid is not None and skip_sid == getattr(conn, "sentence_id", None):
                conn._pc_audio_skip_wait_once = None
                if not (getattr(conn, "config", None) or {}).get("enable_stop_tts_notify", False):
                    return
        return await _orig_wait_completion(conn, *args, **kwargs)

    send_audio_mod._wait_for_audio_completion = _pc_aware_wait_completion
    rebound_all = patch_registry.rebind(_orig_wait_completion, _pc_aware_wait_completion)

    # 2. Patch send_tts_message and rebind across ALL sys.modules (including listenMessageHandler & helloHandle!)
    @functools.wraps(_orig_send_tts_message)
    async def _pc_aware_send_tts_message(conn, state, text=None, *args, **kwargs):
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
                # Ensure any queued streaming PCM WAV chunks finished submitting to 18900 before draining.
                await _wait_pc_stream_drain(conn, cur_sid)
                # Drain AudioRateController queue ONLY (no extra 240ms ESP32 speaker sleep!) so every
                # queued sentence_start callback submits its WAV clip to 18900, then dynamically poll
                # /audio/wait until Windows speakers finish the exact last millisecond of audio.
                rc = getattr(conn, "audio_rate_controller", None)
                if rc is not None and hasattr(rc, "queue_empty_event"):
                    await _wait_rate_controller_empty(conn, rc, cur_sid)
                await _wait_pc_stream_drain(conn, cur_sid)
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
                    return await _orig_send_tts_message(conn, state, text, *args, **kwargs)
                finally:
                    if getattr(conn, "_pc_audio_skip_wait_once", None) == cur_sid:
                        conn._pc_audio_skip_wait_once = None

        return await _orig_send_tts_message(conn, state, text, *args, **kwargs)

    send_audio_mod.send_tts_message = _pc_aware_send_tts_message
    send_audio_mod._pc_audio_playback_patched = True
    # P1-12: Rebind EVERY imported reference (including early-bound listenMessageHandler.send_tts_message!)
    rebound_all += patch_registry.rebind(_orig_send_tts_message, _pc_aware_send_tts_message)
    hello_mod.send_tts_message = _pc_aware_send_tts_message

    # 3. Patch helloHandle.sendAudioMessage so wakeup replies ("我在这里哦！") also play on PC speakers
    @functools.wraps(_orig_hello_send_audio)
    async def _pc_aware_hello_send_audio(conn, sentenceType, audios, text, *args, **kwargs):
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
                    wav_bytes, dur_ms = await asyncio.to_thread(_load_wav_for_pc_sync, wav_path)
                    cur_sid = getattr(conn, "sentence_id", None)
                    _enqueue_pc_clip(conn, cur_sid, text or "", wav_bytes, dur_ms, cfg["volume"])
                    silent_pkt = _get_silent_opus_frame(getattr(conn, "sample_rate", 16000))
                    audios = [silent_pkt] * max(1, int(math.ceil(dur_ms / 60.0)))
            except Exception as exc:
                print(f"[PCAudioRouter] 唤醒回复转电脑音响跳过，回退 ESP32: {exc}", flush=True)
        return await _orig_hello_send_audio(conn, sentenceType, audios, text, *args, **kwargs)

    hello_mod.sendAudioMessage = _pc_aware_hello_send_audio
    # Only rebind hello_mod's own sendAudioMessage references (not sendAudioHandle.sendAudioMessage!)
    rebound_all += patch_registry.rebind(_orig_hello_send_audio, _pc_aware_hello_send_audio, module_prefixes=("core.handle.helloHandle",))

    # 4. Patch abortHandle.handleAbortMessage (and all importers via universal rebind) to cut off PC speakers in <5ms
    #    and publish tts_state("stop") to runtime_events because upstream handleAbortMessage sends WS JSON directly.
    @functools.wraps(_orig_abort)
    async def _pc_aware_abort(conn, *args, **kwargs):
        if conn is not None:
            _ensure_conn_pc_state(conn)
            _clear_pc_clips(conn)
            _clear_pc_stream_chunks(conn)
            was_pc = conn._pc_audio_active_sid is not None or read_audio_output_config()["target"] == "pc"
            conn._pc_audio_active_sid = None
            conn._pc_audio_queued_ms = 0
            if was_pc:
                await _post_stop_to_pc()
        try:
            return await _orig_abort(conn, *args, **kwargs)
        finally:
            try:
                runtime_events.publish_event(
                    "tts_state",
                    state="stop",
                    text=None,
                    device_id=str(getattr(conn, "device_id", "") or ""),
                    sentence_id=str(getattr(conn, "sentence_id", "") or ""),
                    reason="abort",
                )
            except Exception:
                pass

    abort_mod.handleAbortMessage = _pc_aware_abort
    rebound_all += patch_registry.rebind(_orig_abort, _pc_aware_abort)

    patch_registry.record_patch(
        "pc_audio_playback",
        "ok",
        target="sendAudioHandle/helloHandle/abortHandle",
        detail="ok",
        contract_ok=True,
        category="experience",
        rebound=rebound_all,
    )
    print("[PCAudioRouter] 已挂载电脑音响直出桥接 (48kHz 无损 + 18080 热切换 + 毫秒级打断停止)", flush=True)

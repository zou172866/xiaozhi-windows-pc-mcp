"""Windows native WinMM 48kHz lossless PCM audio player & /audio/* + /l3/* Starlette routes (P2-20)."""

from __future__ import annotations

import array
import asyncio
import ctypes
import ctypes.wintypes as wt
import io
import logging
import math
import queue
import threading
import time
from typing import Optional
import wave

import l3_backends
from starlette.requests import Request as _StarletteRequest
from starlette.responses import JSONResponse as _StarletteJSONResponse

from . import facade_attr
from .security import _get_mode, is_emergency_stop_active, set_host_mode
from .winapi import _kernel32


class _WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wt.WORD),
        ("nChannels", wt.WORD),
        ("nSamplesPerSec", wt.DWORD),
        ("nAvgBytesPerSec", wt.DWORD),
        ("nBlockAlign", wt.WORD),
        ("wBitsPerSample", wt.WORD),
        ("cbSize", wt.WORD),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_char_p),
        ("dwBufferLength", wt.DWORD),
        ("dwBytesRecorded", wt.DWORD),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", wt.DWORD),
        ("dwLoops", wt.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


_winmm = ctypes.windll.winmm
_winmm.waveOutGetNumDevs.argtypes = []
_winmm.waveOutGetNumDevs.restype = wt.UINT
_winmm.waveOutOpen.argtypes = [
    ctypes.POINTER(ctypes.c_void_p),
    wt.UINT,
    ctypes.POINTER(_WAVEFORMATEX),
    ctypes.c_size_t,
    ctypes.c_size_t,
    wt.DWORD,
]
_winmm.waveOutOpen.restype = wt.UINT
_winmm.waveOutPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), wt.UINT]
_winmm.waveOutPrepareHeader.restype = wt.UINT
_winmm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), wt.UINT]
_winmm.waveOutWrite.restype = wt.UINT
_winmm.waveOutUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), wt.UINT]
_winmm.waveOutUnprepareHeader.restype = wt.UINT
_winmm.waveOutReset.argtypes = [ctypes.c_void_p]
_winmm.waveOutReset.restype = wt.UINT
_winmm.waveOutClose.argtypes = [ctypes.c_void_p]
_winmm.waveOutClose.restype = wt.UINT

_WAVE_MAPPER = 0xFFFFFFFF
_WAVE_FORMAT_PCM = 1
_WHDR_DONE = 0x00000001
_CALLBACK_EVENT = 0x00050000
_SOFT_LIMIT_KNEE = 0.85

_soft_lut_lock = threading.Lock()
_soft_lut_cache: dict[float, list[int]] = {}


def _get_soft_limiter_lut(vol_key: float) -> list[int]:
    """Build & cache a 65536-entry signed-int16 LUT: linear when vol <= 1.0, tanh soft-knee above 0.85 FS when vol > 1.0."""
    with _soft_lut_lock:
        cached = _soft_lut_cache.get(vol_key)
        if cached is not None:
            return cached
    if vol_key <= 1.0:
        def _map_sample(s: int) -> int:
            v = int(round(s * vol_key))
            return 32767 if v > 32767 else (-32768 if v < -32768 else v)
    else:
        knee = _SOFT_LIMIT_KNEE
        headroom = 1.0 - knee
        inv_headroom = 1.0 / headroom

        def _map_sample(s: int) -> int:
            x = (s / 32768.0) * vol_key
            ax = abs(x)
            if ax <= knee:
                y = x
            else:
                y = math.copysign(knee + headroom * math.tanh((ax - knee) * inv_headroom), x)
            v = int(round(y * 32767.0))
            return 32767 if v > 32767 else (-32768 if v < -32768 else v)

    lut = [_map_sample(i) for i in range(0, 32768)] + [_map_sample(i) for i in range(-32768, 0)]
    with _soft_lut_lock:
        if len(_soft_lut_cache) >= 16:
            _soft_lut_cache.pop(next(iter(_soft_lut_cache)))
        _soft_lut_cache[vol_key] = lut
    return lut


def _scale_pcm16(pcm_bytes: bytes, volume: float) -> bytes:
    """Scale 16-bit signed PCM linearly for vol <= 1.0 and with smooth tanh soft limiter for vol > 1.0."""
    if abs(volume - 1.0) <= 0.01 or not pcm_bytes:
        return pcm_bytes
    vol = round(max(0.05, min(1.8, float(volume))), 2)
    lut = _get_soft_limiter_lut(vol)
    usable_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
    arr = array.array("h")
    arr.frombytes(pcm_bytes[:usable_len])
    out = array.array("h", map(lut.__getitem__, arr))
    return out.tobytes()


class _WinMMPlayer:
    """Double-buffered WinMM CALLBACK_EVENT lossless PCM WAV playback queue for Windows audio device."""

    def __init__(self, max_inflight: int = 2):
        # Lock hierarchy: ALWAYS acquire _device_lock before _lock if both are ever held (never reverse).
        self._lock = threading.Lock()
        self._device_lock = threading.Lock()
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._gen = 0
        self._active_hwo: Optional[ctypes.c_void_p] = None
        self._active_fmt: Optional[tuple] = None
        self._playing = False
        self._inflight_count = 0
        self._has_pending_next = False
        self._max_inflight = max(1, min(4, int(max_inflight)))
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._closed = False

        self._wave_event = _kernel32.CreateEventW(None, False, False, None)
        self._wake_event = _kernel32.CreateEventW(None, False, False, None)
        self._wait_handles = (wt.HANDLE * 2)(self._wave_event, self._wake_event)

        self._worker = threading.Thread(target=self._run_loop, name="winmm-audio-player", daemon=True)
        self._worker.start()

    def _signal_wake(self) -> None:
        if self._wake_event:
            try:
                _kernel32.SetEvent(self._wake_event)
            except Exception:
                pass

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "playing": self._playing,
                "queued": (
                    self._queue.qsize()
                    + (1 if self._has_pending_next else 0)
                    + max(0, self._inflight_count - (1 if self._playing else 0))
                ),
                "inflight": self._inflight_count,
                "devices": int(_winmm.waveOutGetNumDevs()),
            }

    def enqueue_wav(self, wav_bytes: bytes, turn_id: str = "", volume: float = 1.0) -> int:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            pcm = wf.readframes(nframes)
        if sampwidth != 2:
            raise ValueError(f"Only 16-bit PCM WAV is supported, got sampwidth={sampwidth}")
        if not pcm:
            return 0
        if abs(volume - 1.0) > 0.01:
            pcm = _scale_pcm16(pcm, volume)
        dur_ms = int(round((nframes / float(framerate)) * 1000.0)) if framerate > 0 else 0
        with self._lock:
            gen = self._gen
            self._idle_event.clear()
            self._queue.put((gen, turn_id, pcm, channels, framerate, sampwidth * 8))
        self._signal_wake()
        return dur_ms

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
        with self._device_lock:
            hwo = self._active_hwo
            if hwo is not None:
                try:
                    _winmm.waveOutReset(hwo)
                except Exception:
                    pass
        self._signal_wake()

    def wait_idle(self, timeout: float = 8.0) -> bool:
        return self._idle_event.wait(timeout=max(0.1, float(timeout)))

    def close(self) -> None:
        self._closed = True
        self.stop()
        self._worker.join(timeout=2.0)
        self._close_device()
        for attr in ("_wave_event", "_wake_event"):
            h = getattr(self, attr, None)
            if h:
                setattr(self, attr, None)
                try:
                    _kernel32.CloseHandle(h)
                except Exception:
                    pass

    def _close_device_locked(self):
        hwo = self._active_hwo
        self._active_hwo = None
        self._active_fmt = None
        if hwo is not None:
            try:
                _winmm.waveOutReset(hwo)
                _winmm.waveOutClose(hwo)
            except Exception:
                pass

    def _close_device(self):
        with self._device_lock:
            self._close_device_locked()

    def _ensure_device_locked(self, channels: int, sample_rate: int, bits: int) -> ctypes.c_void_p:
        fmt_key = (channels, sample_rate, bits)
        if self._active_hwo is not None and self._active_fmt == fmt_key:
            return self._active_hwo
        self._close_device_locked()
        wfx = _WAVEFORMATEX()
        wfx.wFormatTag = _WAVE_FORMAT_PCM
        wfx.nChannels = channels
        wfx.nSamplesPerSec = sample_rate
        wfx.wBitsPerSample = bits
        wfx.nBlockAlign = channels * (bits // 8)
        wfx.nAvgBytesPerSec = sample_rate * wfx.nBlockAlign
        wfx.cbSize = 0
        hwo = ctypes.c_void_p()
        cb_arg = int(self._wave_event) if self._wave_event else 0
        flags = _CALLBACK_EVENT if cb_arg else 0
        res = _winmm.waveOutOpen(ctypes.byref(hwo), _WAVE_MAPPER, ctypes.byref(wfx), cb_arg, 0, flags)
        if res != 0:
            raise RuntimeError(f"waveOutOpen failed with code {res}")
        self._active_hwo = hwo
        self._active_fmt = fmt_key
        return hwo

    def _reap_done_headers(self, inflight: list) -> None:
        remaining = []
        for buf, hdr, gen in inflight:
            if hdr.dwFlags & _WHDR_DONE:
                with self._device_lock:
                    hwo = self._active_hwo
                    if hwo is not None:
                        try:
                            _winmm.waveOutUnprepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr))
                        except Exception:
                            pass
                self._queue.task_done()
            else:
                remaining.append((buf, hdr, gen))
        inflight[:] = remaining

    def _abort_all_inflight(self, inflight: list) -> None:
        if not inflight:
            return
        with self._device_lock:
            hwo = self._active_hwo
            if hwo is not None:
                try:
                    _winmm.waveOutReset(hwo)
                except Exception:
                    pass
        if hwo is not None:
            for _ in range(40):
                if all(hdr.dwFlags & _WHDR_DONE for _, hdr, _ in inflight):
                    break
                if self._wave_event:
                    _kernel32.WaitForMultipleObjects(1, self._wait_handles, False, 10)
                else:
                    time.sleep(0.005)
            with self._device_lock:
                cur_hwo = self._active_hwo
                if cur_hwo is not None:
                    for _, hdr, _ in inflight:
                        try:
                            _winmm.waveOutUnprepareHeader(cur_hwo, ctypes.byref(hdr), ctypes.sizeof(hdr))
                        except Exception:
                            pass
        for _ in inflight:
            self._queue.task_done()
        inflight.clear()

    def _run_loop(self):
        inflight: list = []
        pending_next: Optional[tuple] = None
        last_idle_ts = time.monotonic()

        while not self._closed:
            if inflight:
                self._reap_done_headers(inflight)
                with self._lock:
                    cur_gen = self._gen
                if any(g != cur_gen for _, _, g in inflight):
                    self._abort_all_inflight(inflight)
                    if pending_next is not None:
                        self._queue.task_done()
                        pending_next = None
                        with self._lock:
                            self._has_pending_next = False

            while len(inflight) < self._max_inflight and not self._closed:
                if pending_next is not None:
                    item = pending_next
                    pending_next = None
                    with self._lock:
                        self._has_pending_next = False
                else:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break

                gen, _turn_id, pcm, channels, sample_rate, bits = item
                with self._lock:
                    if gen != self._gen:
                        self._queue.task_done()
                        continue

                fmt_key = (channels, sample_rate, bits)
                with self._device_lock:
                    active_fmt = self._active_fmt
                if inflight and active_fmt is not None and active_fmt != fmt_key:
                    pending_next = item
                    with self._lock:
                        self._has_pending_next = True
                    break

                try:
                    buf = ctypes.create_string_buffer(pcm, len(pcm))
                    hdr = _WAVEHDR()
                    hdr.lpData = ctypes.cast(buf, ctypes.c_char_p)
                    hdr.dwBufferLength = len(pcm)
                    hdr.dwFlags = 0

                    submitted = False
                    stale = False
                    with self._device_lock:
                        with self._lock:
                            if gen != self._gen or self._closed:
                                stale = True
                        if not stale:
                            hwo = self._ensure_device_locked(channels, sample_rate, bits)
                            if _winmm.waveOutPrepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr)) != 0:
                                if not inflight:
                                    self._close_device_locked()
                            elif _winmm.waveOutWrite(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr)) != 0:
                                _winmm.waveOutUnprepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr))
                                if not inflight:
                                    self._close_device_locked()
                            else:
                                submitted = True

                    if stale:
                        self._queue.task_done()
                        continue
                    if not submitted:
                        self._queue.task_done()
                        break

                    inflight.append((buf, hdr, gen))
                    with self._lock:
                        self._playing = True
                        self._inflight_count = len(inflight)
                except Exception as exc:
                    logging.getLogger("xiaozhi.pcaudio").warning("WinMM playback error: %s", exc)
                    self._queue.task_done()
                    self._abort_all_inflight(inflight)
                    self._close_device()
                    break

            with self._lock:
                self._inflight_count = len(inflight)
                self._has_pending_next = pending_next is not None
                if not inflight:
                    self._playing = False
                    if self._queue.empty() and pending_next is None:
                        self._idle_event.set()

            if inflight:
                _kernel32.WaitForMultipleObjects(2, self._wait_handles, False, 50)
                last_idle_ts = time.monotonic()
            elif pending_next is not None:
                continue
            else:
                res = _kernel32.WaitForMultipleObjects(2, self._wait_handles, False, 200)
                if res == 0x00000102 and (time.monotonic() - last_idle_ts) >= 0.8:
                    self._close_device()
                    last_idle_ts = time.monotonic()


_ALLOWED_L3_CONFIG_KEYS = frozenset({"backend", "timeout_seconds", "timeout"})


def _validate_l3_config_payload(body: dict) -> dict:
    """Validate POST /l3/config body: strictly allow only `backend` and `timeout_seconds`/`timeout`."""
    l3_mod = facade_attr("l3_backends", l3_backends)
    if not isinstance(body, dict):
        raise ValueError("invalid json body")
    forbidden = sorted(str(k) for k in body.keys() if k not in _ALLOWED_L3_CONFIG_KEYS)
    if forbidden:
        raise ValueError(
            f"禁止通过 /l3/config 修改受限或未知字段: {', '.join(forbidden)}（仅允许修改 backend 与 timeout_seconds）"
        )
    cleaned: dict = {}
    if "backend" in body and body["backend"] is not None:
        if not isinstance(body["backend"], str):
            raise ValueError("backend 必须为字符串")
        b = body["backend"].strip().lower()
        if b not in l3_mod.VALID_BACKENDS:
            raise ValueError(f"不支持的 L3 后端: {body['backend']}")
        cleaned["backend"] = b

    has_ts = "timeout_seconds" in body and body["timeout_seconds"] is not None
    has_to = "timeout" in body and body["timeout"] is not None
    if has_ts and has_to and body["timeout_seconds"] != body["timeout"]:
        raise ValueError("timeout 与 timeout_seconds 数值不一致")

    if has_ts or has_to:
        raw_timeout = body["timeout_seconds"] if has_ts else body["timeout"]
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
            raise ValueError("timeout_seconds 必须为整数")
        cleaned["timeout_seconds"] = max(30, min(1800, raw_timeout))

    return cleaned


def register_http_routes(mcp, player: _WinMMPlayer) -> None:
    """Mount /audio/* and /l3/* Starlette routes onto the MCPServer HTTP app."""

    @mcp.custom_route("/audio/status", methods=["GET"])
    async def _audio_status_route(request: _StarletteRequest):
        return _StarletteJSONResponse(player.status())

    @mcp.custom_route("/audio/play", methods=["POST"])
    async def _audio_play_route(request: _StarletteRequest):
        try:
            wav_bytes = await request.body()
            if not wav_bytes:
                return _StarletteJSONResponse({"ok": False, "error": "empty body"}, status_code=400)
            turn_id = request.headers.get("X-Turn-Id", "")
            try:
                volume = float(request.headers.get("X-Volume", "1.0"))
            except Exception:
                volume = 1.0
            dur_ms = await asyncio.to_thread(player.enqueue_wav, wav_bytes, turn_id, volume)
            return _StarletteJSONResponse({"ok": True, "duration_ms": dur_ms})
        except Exception as exc:
            return _StarletteJSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @mcp.custom_route("/audio/stop", methods=["POST"])
    async def _audio_stop_route(request: _StarletteRequest):
        await asyncio.to_thread(player.stop)
        return _StarletteJSONResponse({"ok": True})

    @mcp.custom_route("/audio/wait", methods=["GET"])
    async def _audio_wait_route(request: _StarletteRequest):
        try:
            timeout = float(request.query_params.get("timeout", "8.0"))
        except Exception:
            timeout = 8.0
        timeout = max(0.2, min(20.0, timeout))
        done = await asyncio.to_thread(player.wait_idle, timeout)
        st = player.status()
        return _StarletteJSONResponse(
            {"ok": True, "done": bool(done), "playing": bool(st["playing"]), "queued": int(st["queued"])}
        )

    @mcp.custom_route("/l3/status", methods=["GET"])
    async def _l3_status_route(request: _StarletteRequest):
        l3_mod = facade_attr("l3_backends", l3_backends)
        get_mode_fn = facade_attr("_get_mode", _get_mode)
        data = await asyncio.to_thread(l3_mod.get_status_overview)
        mode = get_mode_fn()
        data["pc_mode"] = mode
        data["l3_mode_allowed"] = mode == "full"
        return _StarletteJSONResponse(data)

    @mcp.custom_route("/mode", methods=["GET"])
    async def _mode_get_route(request: _StarletteRequest):
        get_mode_fn = facade_attr("_get_mode", _get_mode)
        estop_fn = facade_attr("is_emergency_stop_active", is_emergency_stop_active)
        return _StarletteJSONResponse(
            {
                "ok": True,
                "mode": get_mode_fn(),
                "source": "host_state",
                "emergency_stopped": bool(estop_fn()),
            }
        )

    @mcp.custom_route("/mode", methods=["POST"])
    async def _mode_post_route(request: _StarletteRequest):
        try:
            body = await request.json()
            if not isinstance(body, dict) or "mode" not in body:
                return _StarletteJSONResponse(
                    {"ok": False, "error": "请求体必须包含 mode 字段 (off / safe / full)"},
                    status_code=400,
                )
            extra = [k for k in body.keys() if k not in ("mode", "reason")]
            if extra:
                return _StarletteJSONResponse(
                    {"ok": False, "error": f"禁止提交未知字段: {', '.join(sorted(str(k) for k in extra))}"},
                    status_code=400,
                )
            set_mode_fn = facade_attr("set_host_mode", set_host_mode)
            reason = str(body.get("reason") or "").strip()[:80]
            res = await asyncio.to_thread(set_mode_fn, body["mode"], source="http_api", reason=reason)
            return _StarletteJSONResponse(res)
        except ValueError as exc:
            return _StarletteJSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            return _StarletteJSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @mcp.custom_route("/l3/config", methods=["POST"])
    async def _l3_config_route(request: _StarletteRequest):
        try:
            l3_mod = facade_attr("l3_backends", l3_backends)
            get_mode_fn = facade_attr("_get_mode", _get_mode)
            body = await request.json()
            cleaned = _validate_l3_config_payload(body)
            await asyncio.to_thread(l3_mod.write_l3_config, cleaned)
            overview = await asyncio.to_thread(l3_mod.get_status_overview)
            mode = get_mode_fn()
            overview["pc_mode"] = mode
            overview["l3_mode_allowed"] = mode == "full"
            return _StarletteJSONResponse(overview)
        except Exception as exc:
            return _StarletteJSONResponse({"ok": False, "error": str(exc)}, status_code=400)

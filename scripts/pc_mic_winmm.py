"""Zero-dependency Windows native WinMM (`winmm.dll` waveIn*) 16kHz PCM microphone capturer.

Design invariants:
- Uses ONLY Python standard library (`ctypes`, `array`, `threading`) — zero `pip install`.
- Mirrors `pc-mcp/pc_mcp_modules/audio_bridge.py` (`_WinMMPlayer`): Win64-safe `ctypes`
  struct layout, `CALLBACK_EVENT` wakeup (no fragile cross-thread ctypes callbacks),
  4-slot pinned ring buffer so GC never reclaims DMA buffers while recording.
- Dependency-injectable `winmm` / `kernel32` arguments allow 100% deterministic unit tests
  without requiring physical audio hardware.
"""

from __future__ import annotations

import array
import ctypes
import ctypes.wintypes as wt
import math
import os
import threading
from typing import Any, Callable, Optional

WAVE_MAPPER = 0xFFFFFFFF
_WAVE_FORMAT_PCM = 1
_WHDR_DONE = 0x00000001
_WHDR_PREPARED = 0x00000002
_CALLBACK_EVENT = 0x00050000
_WAIT_OBJECT_0 = 0x00000000


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
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wt.DWORD),
        ("dwBytesRecorded", wt.DWORD),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", wt.DWORD),
        ("dwLoops", wt.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


class _WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wt.WORD),
        ("wPid", wt.WORD),
        ("vDriverVersion", wt.UINT),
        ("szPname", ctypes.c_wchar * 32),
        ("dwFormats", wt.DWORD),
        ("wChannels", wt.WORD),
        ("wReserved1", wt.WORD),
    ]


_winmm_singleton = None
_kernel32_singleton = None


def _get_default_winmm():
    global _winmm_singleton
    if _winmm_singleton is not None:
        return _winmm_singleton
    if os.name != "nt":
        raise RuntimeError("WinMM 麦克风采集仅支持 Windows 宿主机")
    lib = ctypes.WinDLL("winmm", use_last_error=True)
    lib.waveInGetNumDevs.argtypes = []
    lib.waveInGetNumDevs.restype = wt.UINT
    lib.waveInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(_WAVEINCAPSW), wt.UINT]
    lib.waveInGetDevCapsW.restype = wt.UINT
    lib.waveInOpen.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        wt.UINT,
        ctypes.POINTER(_WAVEFORMATEX),
        ctypes.c_size_t,
        ctypes.c_size_t,
        wt.DWORD,
    ]
    lib.waveInOpen.restype = wt.UINT
    lib.waveInPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), wt.UINT]
    lib.waveInPrepareHeader.restype = wt.UINT
    lib.waveInUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), wt.UINT]
    lib.waveInUnprepareHeader.restype = wt.UINT
    lib.waveInAddBuffer.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), wt.UINT]
    lib.waveInAddBuffer.restype = wt.UINT
    lib.waveInStart.argtypes = [ctypes.c_void_p]
    lib.waveInStart.restype = wt.UINT
    lib.waveInStop.argtypes = [ctypes.c_void_p]
    lib.waveInStop.restype = wt.UINT
    lib.waveInReset.argtypes = [ctypes.c_void_p]
    lib.waveInReset.restype = wt.UINT
    lib.waveInClose.argtypes = [ctypes.c_void_p]
    lib.waveInClose.restype = wt.UINT
    _winmm_singleton = lib
    return lib


def _get_default_kernel32():
    global _kernel32_singleton
    if _kernel32_singleton is not None:
        return _kernel32_singleton
    if os.name != "nt":
        raise RuntimeError("Win32 Kernel32 事件句柄仅支持 Windows 宿主机")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    k32.CreateEventW.restype = wt.HANDLE
    k32.SetEvent.argtypes = [wt.HANDLE]
    k32.SetEvent.restype = wt.BOOL
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.WaitForSingleObject.restype = wt.DWORD
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.CloseHandle.restype = wt.BOOL
    _kernel32_singleton = k32
    return k32


def calc_pcm16_rms(pcm_bytes: bytes) -> float:
    """Compute normalized RMS amplitude (`0.0..1.0`) for little-endian signed 16-bit PCM."""
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm_bytes[:usable])
    if not samples:
        return 0.0
    acc = sum(s * s for s in samples)
    return min(1.0, math.sqrt(acc / len(samples)) / 32768.0)


def list_input_devices(winmm: Any = None) -> list[dict[str, Any]]:
    """Enumerate Windows audio recording devices (`waveInGetNumDevs` + `waveInGetDevCapsW`)."""
    lib = winmm if winmm is not None else _get_default_winmm()
    count = int(lib.waveInGetNumDevs())
    devices: list[dict[str, Any]] = []
    for idx in range(max(0, count)):
        caps = _WAVEINCAPSW()
        res = int(lib.waveInGetDevCapsW(idx, ctypes.byref(caps), ctypes.sizeof(caps)))
        name = str(caps.szPname or "").strip() if res == 0 else f"录音设备 #{idx}"
        devices.append({"id": idx, "name": name or f"录音设备 #{idx}", "channels": int(caps.wChannels or 1)})
    return devices


class WinMMMicRecorder:
    """Multi-buffered 16kHz 16-bit mono PCM microphone capturer using Windows `winmm.dll`."""

    def __init__(
        self,
        on_frame: Callable[[bytes], None],
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_ms: int = 60,
        device_id: int = WAVE_MAPPER,
        num_buffers: int = 4,
        on_error: Optional[Callable[[Exception], None]] = None,
        winmm: Any = None,
        kernel32: Any = None,
    ) -> None:
        self._on_frame = on_frame
        self._on_error = on_error
        self.sample_rate = max(8000, int(sample_rate))
        self.channels = 1 if int(channels) <= 1 else 2
        self.frame_ms = max(20, int(frame_ms))
        self.frame_bytes = int(self.sample_rate * self.channels * 2 * self.frame_ms // 1000)
        if self.frame_bytes % 2 != 0:
            self.frame_bytes += 1
        self.device_id = WAVE_MAPPER if int(device_id) < 0 else int(device_id)
        self.num_buffers = max(2, min(16, int(num_buffers)))
        self._winmm = winmm
        self._kernel32 = kernel32

        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._running = False
        self._hwi: Optional[ctypes.c_void_p] = None
        self._win_event: Any = None
        self._buffers: list[Any] = []
        self._headers: list[_WAVEHDR] = []
        self._worker: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            lib = self._winmm if self._winmm is not None else _get_default_winmm()
            k32 = self._kernel32 if self._kernel32 is not None else _get_default_kernel32()
            self._winmm = lib
            self._kernel32 = k32

            num_devs = int(lib.waveInGetNumDevs())
            if num_devs <= 0:
                raise RuntimeError("未检测到可用的 Windows 麦克风录音设备（请检查系统声音输入设置）")

            win_evt = k32.CreateEventW(None, False, False, None)
            if not win_evt:
                raise RuntimeError("CreateEventW 创建录音唤醒事件失败")

            wfx = _WAVEFORMATEX()
            wfx.wFormatTag = _WAVE_FORMAT_PCM
            wfx.nChannels = self.channels
            wfx.nSamplesPerSec = self.sample_rate
            wfx.wBitsPerSample = 16
            wfx.nBlockAlign = self.channels * 2
            wfx.nAvgBytesPerSec = self.sample_rate * wfx.nBlockAlign
            wfx.cbSize = 0

            hwi = ctypes.c_void_p()
            evt_val = int(win_evt) if not isinstance(win_evt, int) else win_evt
            res = int(
                lib.waveInOpen(
                    ctypes.byref(hwi),
                    wt.UINT(self.device_id),
                    ctypes.byref(wfx),
                    ctypes.c_size_t(evt_val),
                    ctypes.c_size_t(0),
                    wt.DWORD(_CALLBACK_EVENT),
                )
            )
            if res != 0:
                try:
                    k32.CloseHandle(win_evt)
                except Exception:
                    pass
                raise RuntimeError(f"waveInOpen 打开电脑麦克风失败 (code={res})")

            buffers: list[Any] = []
            headers: list[_WAVEHDR] = []
            try:
                for _ in range(self.num_buffers):
                    buf = ctypes.create_string_buffer(self.frame_bytes)
                    hdr = _WAVEHDR()
                    hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
                    hdr.dwBufferLength = self.frame_bytes
                    hdr.dwBytesRecorded = 0
                    hdr.dwFlags = 0
                    r_prep = int(lib.waveInPrepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr)))
                    if r_prep != 0:
                        raise RuntimeError(f"waveInPrepareHeader 失败 (code={r_prep})")
                    buffers.append(buf)
                    headers.append(hdr)
                    r_add = int(lib.waveInAddBuffer(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr)))
                    if r_add != 0:
                        raise RuntimeError(f"waveInAddBuffer 失败 (code={r_add})")

                r_start = int(lib.waveInStart(hwi))
                if r_start != 0:
                    raise RuntimeError(f"waveInStart 启动录音失败 (code={r_start})")
            except Exception:
                self._cleanup_handles(lib, k32, hwi, win_evt, headers)
                raise

            self._stop_evt.clear()
            self._hwi = hwi
            self._win_event = win_evt
            self._buffers = buffers
            self._headers = headers
            self._running = True
            self._worker = threading.Thread(target=self._capture_loop, name="winmm-mic-recorder", daemon=True)
            self._worker.start()

    def _cleanup_handles(self, lib: Any, k32: Any, hwi: Any, win_evt: Any, headers: list[_WAVEHDR]) -> None:
        if hwi:
            try:
                lib.waveInStop(hwi)
            except Exception:
                pass
            try:
                lib.waveInReset(hwi)
            except Exception:
                pass
            for hdr in headers:
                try:
                    lib.waveInUnprepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
                except Exception:
                    pass
            try:
                lib.waveInClose(hwi)
            except Exception:
                pass
        if win_evt:
            try:
                k32.CloseHandle(win_evt)
            except Exception:
                pass

    def _capture_loop(self) -> None:
        lib = self._winmm
        k32 = self._kernel32
        hwi = self._hwi
        win_evt = self._win_event
        try:
            while not self._stop_evt.is_set():
                k32.WaitForSingleObject(win_evt, 100)
                if self._stop_evt.is_set():
                    break
                for buf, hdr in zip(self._buffers, self._headers):
                    if self._stop_evt.is_set():
                        break
                    if not (int(hdr.dwFlags) & _WHDR_DONE):
                        continue
                    recorded = min(int(hdr.dwBytesRecorded or 0), self.frame_bytes)
                    recorded -= recorded % 2
                    chunk = bytes(buf.raw[:recorded]) if recorded >= 2 else b""
                    hdr.dwFlags = int(hdr.dwFlags) & ~_WHDR_DONE
                    hdr.dwBytesRecorded = 0
                    if chunk:
                        try:
                            self._on_frame(chunk)
                        except Exception as cb_exc:
                            if self._on_error is not None:
                                self._on_error(cb_exc)
                    if not self._stop_evt.is_set():
                        r_re = int(lib.waveInAddBuffer(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr)))
                        if r_re != 0 and not self._stop_evt.is_set():
                            raise RuntimeError(f"waveInAddBuffer 重新入队失败 (code={r_re})")
        except Exception as exc:
            if not self._stop_evt.is_set() and self._on_error is not None:
                self._on_error(exc)
        finally:
            with self._lock:
                self._running = False

    def stop(self) -> None:
        with self._lock:
            worker = self._worker
            lib = self._winmm
            k32 = self._kernel32
            hwi = self._hwi
            win_evt = self._win_event
            headers = list(self._headers)
            self._stop_evt.set()
            self._running = False
            self._worker = None
            self._hwi = None
            self._win_event = None
            self._headers = []

        if k32 is not None and win_evt:
            try:
                k32.SetEvent(win_evt)
            except Exception:
                pass
        if lib is not None and hwi:
            try:
                lib.waveInStop(hwi)
            except Exception:
                pass
            try:
                lib.waveInReset(hwi)
            except Exception:
                pass
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        if lib is not None and hwi:
            for hdr in headers:
                try:
                    lib.waveInUnprepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
                except Exception:
                    pass
            try:
                lib.waveInClose(hwi)
            except Exception:
                pass
        if k32 is not None and win_evt:
            try:
                k32.CloseHandle(win_evt)
            except Exception:
                pass
        with self._lock:
            self._buffers = []

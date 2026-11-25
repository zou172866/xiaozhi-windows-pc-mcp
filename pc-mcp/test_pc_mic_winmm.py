"""Host offline unit tests for Windows WinMM native mic capture, PcMicBridge, and WidgetMicController."""

from __future__ import annotations

import ctypes
import importlib.util
from pathlib import Path
import struct
import threading
import time
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeKernel32:
    def __init__(self) -> None:
        self.events: dict[int, threading.Event] = {}
        self._next_handle = 100
        self.closed: list[int] = []

    def CreateEventW(self, _sa, _manual_reset, _init_state, _name) -> int:
        h = self._next_handle
        self._next_handle += 1
        self.events[h] = threading.Event()
        return h

    def SetEvent(self, handle: int) -> bool:
        ev = self.events.get(int(handle))
        if ev is not None:
            ev.set()
        return True

    def WaitForSingleObject(self, handle: int, timeout_ms: int) -> int:
        ev = self.events.get(int(handle))
        if ev is None:
            return 0xFFFFFFFF
        ok = ev.wait(timeout=max(0.005, float(timeout_ms) / 1000.0))
        if ok:
            ev.clear()
            return 0
        return 0x00000102

    def CloseHandle(self, handle: int) -> bool:
        self.closed.append(int(handle))
        self.events.pop(int(handle), None)
        return True


class _FakeWinMM:
    def __init__(self, k32: _FakeKernel32, *, num_devs: int = 1) -> None:
        self.k32 = k32
        self.num_devs = num_devs
        self.opened_wfx = None
        self.event_handle = None
        self.prepared_count = 0
        self.unprepared_count = 0
        self.queued_hdrs: list = []
        self.started = False
        self.stopped = False
        self.reset_called = False
        self.closed = False

    def waveInGetNumDevs(self) -> int:
        return self.num_devs

    def waveInGetDevCapsW(self, idx: int, caps_ref, _size: int) -> int:
        caps = caps_ref._obj
        caps.szPname = f"Fake Mic #{idx}"
        caps.wChannels = 1
        return 0

    def waveInOpen(self, hwi_ref, _dev_id, wfx_ref, cb_event, _inst, _flags) -> int:
        hwi_ref._obj.value = 0x1234
        wfx = wfx_ref._obj
        self.opened_wfx = (wfx.nSamplesPerSec, wfx.nChannels, wfx.wBitsPerSample)
        self.event_handle = int(cb_event.value if hasattr(cb_event, "value") else cb_event)
        return 0

    def waveInPrepareHeader(self, _hwi, hdr_ref, _size: int) -> int:
        hdr_ref._obj.dwFlags |= 0x00000002
        self.prepared_count += 1
        return 0

    def waveInAddBuffer(self, _hwi, hdr_ref, _size: int) -> int:
        self.queued_hdrs.append(hdr_ref._obj)
        return 0

    def waveInStart(self, _hwi) -> int:
        self.started = True
        return 0

    def waveInStop(self, _hwi) -> int:
        self.stopped = True
        return 0

    def waveInReset(self, _hwi) -> int:
        self.reset_called = True
        return 0

    def waveInUnprepareHeader(self, _hwi, _hdr_ref, _size: int) -> int:
        self.unprepared_count += 1
        return 0

    def waveInClose(self, _hwi) -> int:
        self.closed = True
        return 0

    def inject_pcm(self, pcm_bytes: bytes) -> None:
        assert self.queued_hdrs, "No queued waveIn headers to fill"
        hdr = self.queued_hdrs.pop(0)
        n = min(len(pcm_bytes), int(hdr.dwBufferLength))
        ctypes.memmove(int(hdr.lpData), pcm_bytes, n)
        hdr.dwBytesRecorded = n
        hdr.dwFlags |= 0x00000001  # WHDR_DONE
        if self.event_handle:
            self.k32.SetEvent(self.event_handle)


class TestPcMicWinMMAndDesktopClients(unittest.TestCase):
    def test_01_winmm_mic_recorder_capture_rms_and_clean_release(self):
        mic_mod = _load_script("pc_mic_winmm_test", SCRIPTS_DIR / "pc_mic_winmm.py")
        k32 = _FakeKernel32()
        winmm = _FakeWinMM(k32, num_devs=2)

        devs = mic_mod.list_input_devices(winmm=winmm)
        self.assertEqual(len(devs), 2)
        self.assertEqual(devs[0]["name"], "Fake Mic #0")

        received: list[bytes] = []
        got_evt = threading.Event()

        def _on_frame(chunk: bytes) -> None:
            received.append(chunk)
            if len(received) >= 2:
                got_evt.set()

        rec = mic_mod.WinMMMicRecorder(
            _on_frame,
            sample_rate=16000,
            channels=1,
            frame_ms=60,
            num_buffers=4,
            winmm=winmm,
            kernel32=k32,
        )
        rec.start()
        try:
            self.assertTrue(rec.is_running)
            self.assertEqual(winmm.opened_wfx, (16000, 1, 16))
            self.assertEqual(winmm.prepared_count, 4)
            self.assertEqual(rec.frame_bytes, 1920)

            frame1 = struct.pack("<960h", *([4000] * 960))
            frame2 = struct.pack("<960h", *([-8000] * 960))
            winmm.inject_pcm(frame1)
            winmm.inject_pcm(frame2)
            self.assertTrue(got_evt.wait(timeout=2.0), "Timed out waiting for fake WinMM frames")
            self.assertEqual(received[0], frame1)
            self.assertEqual(received[1], frame2)
            self.assertGreater(mic_mod.calc_pcm16_rms(frame2), mic_mod.calc_pcm16_rms(frame1))
            self.assertEqual(mic_mod.calc_pcm16_rms(b"\x00\x00" * 100), 0.0)
        finally:
            rec.stop()

        self.assertFalse(rec.is_running)
        self.assertTrue(winmm.stopped and winmm.reset_called and winmm.closed)
        self.assertEqual(winmm.unprepared_count, 4)
        self.assertEqual(len(k32.closed), 1)

        # Zero devices fails with clear error
        empty_winmm = _FakeWinMM(_FakeKernel32(), num_devs=0)
        rec_empty = mic_mod.WinMMMicRecorder(lambda _: None, winmm=empty_winmm, kernel32=_FakeKernel32())
        with self.assertRaises(RuntimeError):
            rec_empty.start()

    def test_02_pc_mic_bridge_anti_echo_gating_full_duplex_and_interrupt(self):
        pc_talk = _load_script("pc_talk_mic_test", SCRIPTS_DIR / "pc_talk.py")

        class _FakeWs:
            def __init__(self) -> None:
                self.json_msgs: list[dict] = []
                self.bin_frames: list[bytes] = []

            def send_json(self, obj: dict) -> None:
                self.json_msgs.append(obj)

            def send_frame(self, opcode: int, payload: bytes = b"") -> None:
                if opcode == 0x2:
                    self.bin_frames.append(payload)

        class _FakeRecorder:
            def __init__(self, on_frame, _on_error) -> None:
                self.on_frame = on_frame
                self.is_running = False

            def start(self) -> None:
                self.is_running = True

            def stop(self) -> None:
                self.is_running = False

        ws = _FakeWs()
        rec_holder: list[_FakeRecorder] = []

        def _factory(on_frame, on_err):
            r = _FakeRecorder(on_frame, on_err)
            rec_holder.append(r)
            return r

        bridge = pc_talk.PcMicBridge(
            ws,
            full_duplex=False,
            tail_guard_sec=0.12,
            recorder_factory=_factory,
        )
        bridge.start()
        self.assertTrue(bridge.is_running)
        self.assertEqual(ws.json_msgs[-1], {"type": "listen", "state": "start", "mode": "auto"})

        pcm = b"\x01\x00" * 960
        # 1. Idle -> frames uploaded
        rec_holder[0].on_frame(pcm)
        self.assertEqual(len(ws.bin_frames), 1)

        # 2. TTS starts -> anti-echo drops frames
        bridge.notify_tts_state("start")
        rec_holder[0].on_frame(pcm)
        self.assertEqual(len(ws.bin_frames), 1)

        # 3. TTS stops -> tail guard briefly holds, then resumes
        bridge.notify_tts_state("stop")
        rec_holder[0].on_frame(pcm)
        self.assertEqual(len(ws.bin_frames), 1)
        time.sleep(0.16)
        rec_holder[0].on_frame(pcm)
        self.assertEqual(len(ws.bin_frames), 2)

        # 4. Interrupt clears speaking gate immediately and sends abort + listen/start
        bridge.notify_tts_state("start")
        bridge.interrupt()
        self.assertEqual(ws.json_msgs[-2]["type"], "abort")
        self.assertEqual(ws.json_msgs[-1], {"type": "listen", "state": "start", "mode": "auto"})
        rec_holder[0].on_frame(pcm)
        self.assertEqual(len(ws.bin_frames), 3)

        # 5. Full-duplex headphone mode allows frames during TTS speaking
        bridge.set_full_duplex(True)
        bridge.notify_tts_state("start")
        rec_holder[0].on_frame(pcm)
        self.assertEqual(len(ws.bin_frames), 4)

        bridge.stop()
        self.assertFalse(bridge.is_running)
        self.assertEqual(
            ws.json_msgs[-1],
            {"type": "listen", "state": "stop", "pc_mic_state": "off"},
        )
        # Idempotent stop does not emit duplicate release signals
        msg_count = len(ws.json_msgs)
        bridge.stop()
        self.assertEqual(len(ws.json_msgs), msg_count)

    def test_03_widget_mic_controller_default_off_and_bat_encoding_contract(self):
        widget = _load_script("desktop_companion_mic_test", SCRIPTS_DIR / "desktop_companion.py")
        ctrl = widget.WidgetMicController()
        self.assertFalse(ctrl.is_active)
        self.assertFalse(ctrl.full_duplex)
        self.assertTrue(ctrl.set_full_duplex(True))
        self.assertFalse(ctrl.set_full_duplex(False))
        ctrl.stop()  # idempotent when inactive

        bat_path = REPO_ROOT / "电脑语音对话.bat"
        self.assertTrue(bat_path.is_file(), "Missing 电脑语音对话.bat launcher")
        raw = bat_path.read_bytes()
        self.assertTrue(all(b < 128 for b in raw), "电脑语音对话.bat must be pure ASCII")
        self.assertIn(b"\r\n", raw, "电脑语音对话.bat must use CRLF")
        self.assertEqual(raw.replace(b"\r\n", b"").find(b"\n"), -1, "电脑语音对话.bat must not contain bare LF")
        self.assertIn(b"--mic", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Unit tests for Batch 2 & Batch 3 host 18900 hardening:
- Bearer Token + Host/Origin ASGI middleware & DNS rebinding protection
- Access log polling noise filter & secret redaction filter
- WinMM soft limiter (_scale_pcm16) & CALLBACK_EVENT double-buffered player
- _shell_open without shell=True, unified MTA COM scope, and type_text clipboard protection
"""

from __future__ import annotations

import array
import io
import logging
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import pc_control_mcp as pcm


def _make_silence_wav(duration_sec: float = 0.08, sample_rate: int = 48000) -> bytes:
    n = max(1, int(sample_rate * duration_sec))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(n * 2))
    return buf.getvalue()


class TestPcControlMcpHardening(unittest.TestCase):
    def test_no_shell_true_in_pc_control_mcp_source(self):
        src = (MCP_DIR / "pc_control_mcp.py").read_text(encoding="utf-8")
        self.assertNotIn("shell=True", src, "shell=True must never be used in pc_control_mcp.py")

    def test_load_or_create_token_is_persistent_and_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            tf = Path(td) / "pc_mcp_token.txt"
            tok1 = pcm.load_or_create_pc_mcp_token(tf)
            self.assertGreaterEqual(len(tok1), 32)
            tok2 = pcm.load_or_create_pc_mcp_token(tf)
            self.assertEqual(tok1, tok2)
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_soft_limiter_prevents_hard_clipping_and_preserves_monotonicity(self):
        # 1. Identity around 1.0
        samples = array.array("h", [-32000, -20000, -1000, 0, 1000, 20000, 32000])
        raw = samples.tobytes()
        self.assertEqual(pcm._scale_pcm16(raw, 1.0), raw)

        # 2. Gain 1.5x applies smooth soft knee above 0.85 FS without wraparound or hard flat plateau
        scaled = array.array("h")
        scaled.frombytes(pcm._scale_pcm16(raw, 1.5))
        self.assertEqual(len(scaled), len(samples))
        self.assertEqual(scaled[3], 0)
        # Monotonicity preserved across positive & negative signals
        for a, b in zip(scaled, scaled[1:]):
            self.assertLessEqual(a, b)
        # Small signal scales linearly (~1500)
        self.assertAlmostEqual(scaled[4], 1500, delta=3)
        # Peak input (32000 * 1.5 -> ~1.46 FS) soft-saturates cleanly below 32767 (strictly > 20000*1.5 soft output)
        self.assertGreater(scaled[6], scaled[5])
        self.assertLessEqual(scaled[6], 32767)
        self.assertGreaterEqual(scaled[0], -32768)

        # 3. Attenuation (0.5x and 0.95x) scales PURELY linearly (no 0.85 FS soft-knee distortion, zero audioop warning)
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            quiet = array.array("h")
            quiet.frombytes(pcm._scale_pcm16(raw, 0.5))
            self.assertAlmostEqual(quiet[5], 10000, delta=2)

            near_unity = array.array("h")
            near_unity.frombytes(pcm._scale_pcm16(raw, 0.95))
            self.assertAlmostEqual(near_unity[6], int(round(32000 * 0.95)), delta=2)
            self.assertEqual(
                [w for w in caught if "audioop" in str(w.message)],
                [],
                "Deprecated audioop must not be imported by _scale_pcm16",
            )

    def test_access_log_noise_filter_and_secret_redaction_filter(self):
        nf = pcm._AccessLogNoiseFilter()
        rec_l3_ok = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1234", "GET", "/l3/status", "1.1", 200), None,
        )
        rec_audio_ok = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1234", "GET", "/audio/status?foo=1", "1.1", 200), None,
        )
        rec_l3_err = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1234", "GET", "/l3/status", "1.1", 401), None,
        )
        rec_mcp_ok = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1234", "POST", "/mcp", "1.1", 200), None,
        )
        self.assertFalse(nf.filter(rec_l3_ok))
        self.assertFalse(nf.filter(rec_audio_ok))
        self.assertTrue(nf.filter(rec_l3_err))
        self.assertTrue(nf.filter(rec_mcp_ok))

        rf = pcm._SecretRedactingFilter(("super-secret-runtime-token-123456",))
        rec_sec = logging.LogRecord(
            "test", logging.INFO, "", 0,
            "Header Bearer mySecretToken999999 and sk-abc1234567890123 and AIzaSyA12345678901234567890 and %s",
            ("super-secret-runtime-token-123456",), None,
        )
        self.assertTrue(rf.filter(rec_sec))
        rendered = rec_sec.getMessage()
        self.assertNotIn("mySecretToken999999", rendered)
        self.assertNotIn("sk-abc1234567890123", rendered)
        self.assertNotIn("AIzaSyA12345678901234567890", rendered)
        self.assertNotIn("super-secret-runtime-token-123456", rendered)
        self.assertIn("Bearer ***", rendered)

    def test_asgi_bearer_host_and_origin_enforcement(self):
        import asyncio

        tok = "test-token-abcdefghijklmnopqrstuvwxyz-123456"
        app = pcm.create_secured_asgi_app(tok, port=18900, bind_host="127.0.0.1")

        async def _exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18900") as client:
                # 1. Missing Authorization -> 401
                r1 = await client.get("/audio/status")
                self.assertEqual(r1.status_code, 401)
                # 2. Wrong Bearer token -> 401
                r2 = await client.get("/audio/status", headers={"Authorization": "Bearer wrong"})
                self.assertEqual(r2.status_code, 401)
                # 3. Untrusted Host -> 421
                r3 = await client.get(
                    "/audio/status",
                    headers={"Host": "attacker.example:18900", "Authorization": f"Bearer {tok}"},
                )
                self.assertEqual(r3.status_code, 421)
                # 4. Untrusted Origin -> 403
                r4 = await client.get(
                    "/audio/status",
                    headers={"Origin": "https://evil.example", "Authorization": f"Bearer {tok}"},
                )
                self.assertEqual(r4.status_code, 403)
                # 5. Trusted host.docker.internal + valid Bearer (including case-insensitive scheme) -> 200
                r5 = await client.get(
                    "/audio/status",
                    headers={"Host": "host.docker.internal:18900", "Authorization": f"bearer {tok}"},
                )
                self.assertEqual(r5.status_code, 200)
                self.assertTrue(r5.json().get("ok"))
                # 6. /audio/wait returns done + playing + queued
                r6 = await client.get(
                    "/audio/wait",
                    params={"timeout": "0.3"},
                    headers={"Host": "host.docker.internal:18900", "Authorization": f"Bearer {tok}"},
                )
                self.assertEqual(r6.status_code, 200)
                j6 = r6.json()
                self.assertIn("done", j6)
                self.assertIn("playing", j6)
                self.assertIn("queued", j6)

        asyncio.run(_exercise())

    def test_winmm_double_buffered_playback_and_stop(self):
        import threading
        import time

        # Exercises real CALLBACK_EVENT WinMM player with ultra-short silent WAV clips
        player = pcm._WinMMPlayer(max_inflight=2)
        try:
            silent = _make_silence_wav(0.06, 48000)
            dur1 = player.enqueue_wav(silent, turn_id="t1", volume=1.0)
            dur2 = player.enqueue_wav(silent, turn_id="t1", volume=1.2)
            self.assertEqual(dur1, 60)
            self.assertEqual(dur2, 60)
            self.assertTrue(player.wait_idle(timeout=3.0), "Player did not finish double-buffered silent clips")

            # Format switch 48kHz -> 24kHz: status()["queued"] must reflect pending_next while 48kHz plays
            w48 = _make_silence_wav(0.45, 48000)
            w24 = _make_silence_wav(0.10, 24000)
            player.enqueue_wav(w48, turn_id="fmt", volume=1.0)
            player.enqueue_wav(w24, turn_id="fmt", volume=1.0)
            time.sleep(0.15)
            st_mid = player.status()
            self.assertTrue(st_mid["playing"] or st_mid["queued"] >= 1)
            if st_mid["playing"]:
                self.assertGreaterEqual(st_mid["queued"], 1, f"pending_next dropped from status().queued: {st_mid}")
            self.assertTrue(player.wait_idle(timeout=3.0), "Player did not finish 48k->24k format switch")

            # Enqueue a longer silent clip and immediately stop() -> must become idle promptly
            long_silent = _make_silence_wav(1.2, 48000)
            player.enqueue_wav(long_silent, turn_id="t2", volume=1.0)
            player.stop()
            self.assertTrue(player.wait_idle(timeout=1.5), "Player did not abort promptly on stop()")

            # Concurrent enqueue_wav + stop stress test across threads (must never deadlock or leak)
            errors = []

            def _spam_enqueue():
                try:
                    for i in range(12):
                        player.enqueue_wav(silent, turn_id=f"r{i}", volume=1.0)
                        time.sleep(0.005)
                except Exception as exc:
                    errors.append(exc)

            def _spam_stop():
                try:
                    for _ in range(12):
                        player.stop()
                        time.sleep(0.006)
                except Exception as exc:
                    errors.append(exc)

            t_enq = threading.Thread(target=_spam_enqueue)
            t_stp = threading.Thread(target=_spam_stop)
            t_enq.start()
            t_stp.start()
            t_enq.join(timeout=5.0)
            t_stp.join(timeout=5.0)
            player.stop()
            self.assertEqual(errors, [])
            self.assertTrue(player.wait_idle(timeout=2.0), "Player stuck non-idle after enqueue/stop race")
            st = player.status()
            self.assertFalse(st["playing"])
            self.assertEqual(st["queued"], 0)
        finally:
            player.close()

    def test_type_text_preserves_non_text_clipboard_and_restores_text_clipboard(self):
        copied_calls = []

        def _fake_copy(val):
            copied_calls.append(val)

        benign_fg = {
            "hwnd": 1001,
            "pid": 4321,
            "process": "notepad.exe",
            "class": "Notepad",
            "title": "无标题 - 记事本",
        }

        with tempfile.TemporaryDirectory() as td:
            audit_file = Path(td) / "l2_audit.jsonl"
            with (
                patch.object(pcm, "_check_permission", return_value=None),
                patch.object(pcm, "_get_foreground_window_info", return_value=benign_fg),
                patch.object(pcm, "L2_AUDIT_LOG_FILE", audit_file),
                patch.object(pcm.pyautogui, "hotkey"),
                patch.object(pcm.pyautogui, "press"),
                patch.object(pcm.time, "sleep"),
                patch.object(pcm.pyperclip, "copy", side_effect=_fake_copy),
                patch.object(pcm.pyperclip, "paste", return_value="hello"),
            ):
                # Case A: clipboard holds non-text (e.g. screenshot image) -> IsClipboardFormatAvailable(13) == 0 -> must NOT overwrite with ""
                with patch.object(pcm._user32, "IsClipboardFormatAvailable", return_value=0):
                    copied_calls.clear()
                    res = pcm.pc_ui_action(action="type_text", text="hello")
                    self.assertIn("已输入文本", res)
                    self.assertEqual(copied_calls, ["hello"])

                # Case B: clipboard holds previous text -> restores previous text after paste
                with (
                    patch.object(pcm._user32, "IsClipboardFormatAvailable", return_value=1),
                    patch.object(pcm.pyperclip, "paste", side_effect=["previous-clip", "hello"]),
                ):
                    copied_calls.clear()
                    pcm.pc_ui_action(action="type_text", text="hello")
                    self.assertEqual(copied_calls, ["hello", "previous-clip"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

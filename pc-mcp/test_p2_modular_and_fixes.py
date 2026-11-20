"""Unit tests for P2 #20 (modular split), #21 (runtime_settings & L3 default), and #23 (SendMessageTimeoutW + token I/O guard)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MCP_DIR = Path(__file__).resolve().parent
SERVER_DIR = MCP_DIR.parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import l3_backends
import pc_control_mcp as pcm
from pc_mcp_modules import audio_bridge, launcher, security, ui_tools, vision, winapi


class TestP2ModularSplitAndFixes(unittest.TestCase):
    def test_p220_modular_split_no_shell_true_and_thin_facade(self):
        # 1. Facade stays compact and re-exports key symbols from all 6 modules
        facade_lines = (MCP_DIR / "pc_control_mcp.py").read_text(encoding="utf-8").splitlines()
        self.assertLess(len(facade_lines), 450, "pc_control_mcp.py should remain a thin facade")

        mod_dir = MCP_DIR / "pc_mcp_modules"
        expected_files = {
            "__init__.py",
            "winapi.py",
            "security.py",
            "launcher.py",
            "vision.py",
            "ui_tools.py",
            "audio_bridge.py",
        }
        actual_files = {p.name for p in mod_dir.glob("*.py")}
        self.assertTrue(expected_files.issubset(actual_files))

        # 2. Zero shell=True anywhere across facade and split modules
        for py_file in [MCP_DIR / "pc_control_mcp.py", *sorted(mod_dir.glob("*.py"))]:
            src = py_file.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", src, f"shell=True forbidden in {py_file.name}")

        # 3. Verify key facade exports match underlying module implementations
        self.assertIs(pcm._WinMMPlayer, audio_bridge._WinMMPlayer)
        self.assertIs(pcm._scale_pcm16, audio_bridge._scale_pcm16)
        self.assertIs(pcm._broadcast_screen_off, winapi._broadcast_screen_off)
        self.assertIs(pcm._check_foreground_input_guard, security._check_foreground_input_guard)
        self.assertIs(pcm._shell_open, launcher._shell_open)
        self.assertIs(pcm._smart_locate_target, vision._smart_locate_target)
        self.assertIs(pcm._pc_ui_action_locked, ui_tools._pc_ui_action_locked)

    def test_p223_screen_off_uses_send_message_timeout_no_blocking_sendmessagew(self):
        # Source must not use blocking SendMessageW
        for py_file in [MCP_DIR / "pc_control_mcp.py", *(MCP_DIR / "pc_mcp_modules").glob("*.py")]:
            src = py_file.read_text(encoding="utf-8")
            self.assertNotIn("SendMessageW(", src, f"Blocking SendMessageW found in {py_file.name}")

        fake_u32 = MagicMock()
        fake_u32.SendMessageTimeoutW.return_value = 1
        with (
            patch.object(pcm, "_get_mode", return_value="safe"),
            patch.object(pcm, "_user32", fake_u32),
        ):
            res = pcm.pc_system_control("screen_off")
        self.assertIn("息屏", res)
        fake_u32.SendMessageTimeoutW.assert_called_once()
        args = fake_u32.SendMessageTimeoutW.call_args[0]
        self.assertEqual(args[0], 0xFFFF)
        self.assertEqual(args[1], 0x0112)
        self.assertEqual(args[2], 0xF170)
        self.assertEqual(args[3], 2)
        self.assertEqual(args[4], winapi.SMTO_ABORTIFHUNG)
        self.assertGreaterEqual(args[5], 1000)

    def test_p223_token_loader_retries_transient_io_and_refuses_silent_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            tf = Path(td) / "pc_mcp_token.txt"
            original = "A" * 40
            tf.write_text(original + "\n", encoding="utf-8")

            # 1. Transient PermissionError on first read recovers on second read without overwriting
            orig_read = Path.read_text
            attempts = {"n": 0}

            def _flaky_read(self_path, *args, **kwargs):
                if self_path == tf:
                    attempts["n"] += 1
                    if attempts["n"] == 1:
                        raise PermissionError("temporary AV lock")
                return orig_read(self_path, *args, **kwargs)

            with patch.object(Path, "read_text", _flaky_read):
                tok = pcm.load_or_create_pc_mcp_token(tf, retries=3)
            self.assertEqual(tok, original)
            self.assertEqual(attempts["n"], 2)
            self.assertEqual(tf.read_text(encoding="utf-8").strip(), original)

            # 2. Persistent OSError MUST raise instead of silently generating and overwriting token
            def _always_fail(self_path, *args, **kwargs):
                if self_path == tf:
                    raise PermissionError("locked permanently")
                return orig_read(self_path, *args, **kwargs)

            with patch.object(Path, "read_text", _always_fail):
                with self.assertRaises(RuntimeError):
                    pcm.load_or_create_pc_mcp_token(tf, retries=2)
            # Token on disk MUST remain completely untouched
            self.assertEqual(tf.read_text(encoding="utf-8").strip(), original)

    def test_p221_host_reads_runtime_settings_with_legacy_fallback_and_corrupt_failsafe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rs_file = root / "runtime_settings.json"
            legacy_mode = root / "pc_control_mode.json"

            # 1. Primary runtime_settings.json wins over legacy file
            rs_file.write_text(
                json.dumps({"schema_version": 1, "pc_control": {"mode": "full"}}),
                encoding="utf-8",
            )
            legacy_mode.write_text(json.dumps({"mode": "off"}), encoding="utf-8")
            with (
                patch.object(pcm, "RUNTIME_SETTINGS_FILE", rs_file),
                patch.object(pcm, "MODE_FILE", legacy_mode),
            ):
                self.assertEqual(pcm._get_mode(), "full")

                # 2. Corrupt runtime_settings.json fails safe to 'safe' (does not read stale legacy file)
                rs_file.write_text("{corrupt json", encoding="utf-8")
                self.assertEqual(pcm._get_mode(), "safe")

                # 3. Missing runtime_settings.json falls back cleanly to legacy pc_control_mode.json
                rs_file.unlink()
                self.assertEqual(pcm._get_mode(), "off")

    def test_p221_unified_l3_backend_default_is_dsh_everywhere(self):
        with tempfile.TemporaryDirectory() as td:
            empty_dotenv = Path(td) / ".env"
            empty_dotenv.write_text("", encoding="utf-8")
            clean_env = {k: v for k, v in os.environ.items() if k != "L3_BACKEND"}
            with (
                patch.dict(os.environ, clean_env, clear=True),
                patch.object(l3_backends, "DOTENV_FILE", empty_dotenv),
            ):
                self.assertEqual(l3_backends._default_backend(), "dsh")
                self.assertEqual(l3_backends.default_l3_config()["backend"], "dsh")

        compose_txt = (SERVER_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("L3_BACKEND=${L3_BACKEND:-dsh}", compose_txt)
        env_ex_txt = (SERVER_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertIn("L3_BACKEND=dsh", env_ex_txt)


if __name__ == "__main__":
    unittest.main()

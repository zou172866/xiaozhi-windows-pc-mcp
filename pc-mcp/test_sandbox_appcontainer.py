"""Host tests for Phase 4 Windows native AppContainer workspace sandbox & handle anti-escape guard."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PC_MCP_DIR = Path(__file__).resolve().parent
if str(PC_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(PC_MCP_DIR))

import pc_control_mcp
from pc_mcp_modules import sandbox


@unittest.skipUnless(sys.platform == "win32", "Windows AppContainer tests require win32")
class TestPhase4WindowsAppContainerSandbox(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.ws = self.base / "workspace"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.outside = self.base / "outside_secret"
        self.outside.mkdir(parents=True, exist_ok=True)
        self.secret_file = self.outside / "secret.txt"
        self.secret_file.write_text("TOP_SECRET_CANARY_998877", encoding="utf-8")

    def tearDown(self):
        # Remove any junction safely before TemporaryDirectory cleanup
        junc = self.ws / "evil_junction"
        if junc.exists() or junc.is_symlink():
            try:
                os.rmdir(junc)
            except OSError:
                pass
        self._td.cleanup()

    def test_handle_guard_blocks_traversal_reserved_names_junction_and_hardlink(self):
        # Basic write / read / list / delete inside workspace
        msg = sandbox.sandbox_file_op("write", "sub/hello.txt", "hello world", root=self.ws)
        self.assertIn("已写入", msg)
        self.assertEqual(sandbox.sandbox_file_op("read", "sub/hello.txt", root=self.ws), "hello world")
        self.assertIn("sub", sandbox.sandbox_file_op("list", "", root=self.ws))

        # Traversal, absolute paths, drive letters, reserved names must be blocked
        for bad in ("../outside_secret/secret.txt", "C:\\Windows\\win.ini", "\\\\server\\share", "CON", "sub/NUL.txt"):
            with self.assertRaises(sandbox.SandboxSecurityError, msg=f"Should block {bad}"):
                sandbox.safe_resolve_workspace_path(bad, root=self.ws)

        # Hardlink escape check (NTFS link pointing to outside secret file)
        hardlink_path = self.ws / "linked_secret.txt"
        os.link(self.secret_file, hardlink_path)
        with self.assertRaises(sandbox.SandboxSecurityError):
            sandbox.sandbox_file_op("read", "linked_secret.txt", root=self.ws)

        # Junction (Reparse Point) escape check
        junc_path = self.ws / "evil_junction"
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junc_path), str(self.outside)],
            capture_output=True,
            creationflags=sandbox.CREATE_NO_WINDOW,
        )
        if proc.returncode == 0:
            with self.assertRaises(sandbox.SandboxSecurityError):
                sandbox.sandbox_file_op("read", "evil_junction/secret.txt", root=self.ws)

    def test_real_windows_appcontainer_execution_isolation_and_timeout_kill(self):
        # 1. Workspace file I/O succeeds inside AppContainer; host secrets & env stripped
        code = (
            "import os, pathlib\n"
            "p = pathlib.Path('created_in_sandbox.txt')\n"
            "p.write_text('ok-inside', encoding='utf-8')\n"
            "print('SANDBOX=' + os.environ.get('XIAOZHI_SANDBOX', ''))\n"
            "print('TOKEN_LEAK=' + os.environ.get('PC_MCP_TOKEN', 'NONE'))\n"
            "print('READBACK=' + p.read_text(encoding='utf-8'))\n"
        )
        res = sandbox.run_python_in_appcontainer(inline_python=code, timeout_sec=10, root=self.ws)
        self.assertTrue(res["ok"], f"AppContainer run failed: {res}")
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("SANDBOX=1", res["stdout"])
        self.assertIn("TOKEN_LEAK=NONE", res["stdout"])
        self.assertIn("READBACK=ok-inside", res["stdout"])
        self.assertTrue((self.ws / "created_in_sandbox.txt").is_file())

        # 2. Reading outside host secret file MUST fail with PermissionError inside AppContainer
        probe_escape = (
            f"target = r'{self.secret_file}'\n"
            "try:\n"
            "    open(target, 'r', encoding='utf-8').read()\n"
            "    print('ESCAPED_BAD')\n"
            "except PermissionError:\n"
            "    print('BLOCKED_BY_KERNEL')\n"
            "except OSError as e:\n"
            "    print('BLOCKED_OS_' + str(e.errno))\n"
        )
        res_esc = sandbox.run_python_in_appcontainer(inline_python=probe_escape, timeout_sec=10, root=self.ws)
        self.assertTrue(res_esc["ok"], f"Escape probe failed unexpectedly: {res_esc}")
        self.assertNotIn("ESCAPED_BAD", res_esc["stdout"])
        self.assertIn("BLOCKED_", res_esc["stdout"])

        # 3. Zero-network AppContainer blocks outbound sockets at kernel level
        probe_net = (
            "import socket\n"
            "try:\n"
            "    s = socket.create_connection(('1.1.1.1', 80), timeout=2)\n"
            "    s.close()\n"
            "    print('NET_ALLOWED_BAD')\n"
            "except OSError as e:\n"
            "    print('NET_BLOCKED_' + type(e).__name__)\n"
        )
        res_net = sandbox.run_python_in_appcontainer(inline_python=probe_net, timeout_sec=10, root=self.ws)
        self.assertTrue(res_net["ok"], f"Net probe failed unexpectedly: {res_net}")
        self.assertNotIn("NET_ALLOWED_BAD", res_net["stdout"])
        self.assertIn("NET_BLOCKED_", res_net["stdout"])

        # 4. Timeout triggers Win32 Job Object hard kill
        res_to = sandbox.run_python_in_appcontainer(
            inline_python="import time; time.sleep(30)",
            timeout_sec=1,
            root=self.ws,
        )
        self.assertFalse(res_to["ok"])
        self.assertTrue(res_to["timed_out"])
        self.assertEqual(res_to["exit_code"], 124)

    def test_mcp_tools_require_full_mode_and_respect_emergency_stop(self):
        with patch.object(pc_control_mcp, "_check_permission", return_value="当前模式不允许"), \
             patch.object(pc_control_mcp, "_write_l2_audit_log") as audit_mock:
            out1 = pc_control_mcp.pc_sandbox_file("list")
            out2 = pc_control_mcp.pc_sandbox_run(inline_python="print(1)")
        self.assertIn("不允许", out1)
        self.assertIn("不允许", out2)
        self.assertEqual(audit_mock.call_count, 2)

        with patch.object(pc_control_mcp, "_check_permission", return_value=""), \
             patch.object(pc_control_mcp, "is_emergency_stop_active", return_value=True), \
             patch.object(pc_control_mcp, "_write_l2_audit_log") as audit_mock2:
            out3 = pc_control_mcp.pc_sandbox_run(inline_python="print(1)")
        self.assertIn("急停", out3)
        self.assertEqual(audit_mock2.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

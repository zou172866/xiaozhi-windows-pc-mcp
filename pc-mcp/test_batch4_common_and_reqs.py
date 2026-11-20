"""Batch 4 unit tests for H-5 (pc-mcp/common deduplication) and H-9 (requirements.in provenance)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import agy_sidecar_worker
import common as pc_common
import l3_backends


class TestBatch4CommonAndReqs(unittest.TestCase):
    def test_h5_sidecar_gains_node_shim_unwrap_branch(self):
        """H-5: agy_sidecar_worker._prepare_cmd now unwraps npm/Node .cmd shims to node.exe + js entry."""
        if os.name != "nt":
            self.skipTest("Windows .cmd shim unwrapping only applies on nt")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_node = root / "node.exe"
            fake_node.write_bytes(b"MZ")
            entry_js = root / "node_modules" / "demo-cli" / "bin" / "cli.js"
            entry_js.parent.mkdir(parents=True, exist_ok=True)
            entry_js.write_text("console.log('ok');\n", encoding="utf-8")
            shim = root / "demo-cli.cmd"
            shim.write_text(
                '@ECHO off\r\n"%~dp0\\node.exe" "%~dp0\\node_modules\\demo-cli\\bin\\cli.js" %*\r\n',
                encoding="utf-8",
            )
            prompt = "line1\nline2 & echo pwned"
            prep_sidecar = agy_sidecar_worker._prepare_cmd([str(shim), "run", prompt])
            prep_l3 = l3_backends.prepare_subprocess_command([str(shim), "run", prompt])
            self.assertEqual(prep_sidecar, prep_l3)
            self.assertEqual(Path(prep_sidecar[0]).resolve(), fake_node.resolve())
            self.assertEqual(Path(prep_sidecar[1]).resolve(), entry_js.resolve())
            self.assertEqual(prep_sidecar[3], prompt)

    def test_h5_sidecar_kill_process_tree_enforces_create_time(self):
        """H-5: agy_sidecar_worker._kill_process_tree refuses to kill a reused PID with mismatched create_time."""
        fake_proc = MagicMock()
        fake_proc.create_time.return_value = 2000.0
        fake_proc.is_running.return_value = True
        fake_proc.status.return_value = "running"

        with patch.object(pc_common.process.psutil, "Process", return_value=fake_proc), patch.object(
            pc_common.process.subprocess, "run"
        ) as mock_run:
            killed = agy_sidecar_worker._kill_process_tree(99999, expected_create_time=1000.0)
            self.assertFalse(killed)
            fake_proc.terminate.assert_not_called()
            fake_proc.kill.assert_not_called()
            mock_run.assert_not_called()

    def test_h5_sidecar_timeout_passes_captured_create_time(self):
        """H-5: _process_task captures proc create_time and passes expected_create_time on timeout."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pending = root / "pending"
            active = root / "active"
            pending.mkdir()
            active.mkdir()
            tf = pending / "t_timeout_ct.json"
            tf.write_text('{"id": "t_timeout_ct", "prompt": "hello"}', encoding="utf-8")

            fake_popen = MagicMock()
            fake_popen.pid = 424242
            fake_popen.communicate.side_effect = [
                subprocess.TimeoutExpired(cmd=["agentapi.exe"], timeout=30),
                ("", ""),
            ]
            with (
                patch.object(agy_sidecar_worker, "PENDING_DIR", pending),
                patch.object(agy_sidecar_worker, "ACTIVE_DIR", active),
                patch.object(agy_sidecar_worker, "_build_agentapi_prefix", return_value=["agentapi.exe"]),
                patch.object(agy_sidecar_worker.subprocess, "Popen", return_value=fake_popen),
                patch.object(pc_common, "get_process_create_time", return_value=1700000123.5),
                patch.object(agy_sidecar_worker, "_kill_process_tree") as mock_kill,
            ):
                ok = agy_sidecar_worker._process_task(tf)
                self.assertTrue(ok)
                mock_kill.assert_called_once_with(424242, expected_create_time=1700000123.5)

    def test_h9_requirements_in_is_source_of_hash_locked_requirements_txt(self):
        """H-9: pc-mcp/requirements.in declares direct deps and matches hash-locked requirements.txt."""
        req_in = MCP_DIR / "requirements.in"
        req_txt = MCP_DIR / "requirements.txt"
        self.assertTrue(req_in.is_file(), "pc-mcp/requirements.in must exist")
        self.assertTrue(req_txt.is_file(), "pc-mcp/requirements.txt must exist")

        direct_names: set[str] = set()
        for raw_ln in req_in.read_text(encoding="utf-8").splitlines():
            ln = raw_ln.strip()
            if not ln or ln.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z0-9_.-]+)", ln)
            self.assertIsNotNone(m, f"Unparseable requirements.in line: {ln}")
            direct_names.add(m.group(1).lower().replace("_", "-"))

        # requirements.in should stay concise (only direct deps, not 45 transitive packages)
        self.assertGreaterEqual(len(direct_names), 10)
        self.assertLessEqual(len(direct_names), 20)

        txt = req_txt.read_text(encoding="utf-8")
        self.assertIn("requirements.in", txt, "requirements.txt should record requirements.in provenance")
        locked_names = {
            name.lower().replace("_", "-")
            for name in re.findall(r"^([A-Za-z0-9_.-]+)==", txt, re.M)
        }
        missing = sorted(direct_names - locked_names)
        self.assertEqual(missing, [], f"Direct deps missing from requirements.txt: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

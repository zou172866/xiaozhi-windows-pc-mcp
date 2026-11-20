"""Security unit tests for P0 #2 (container -> host RCE cutoff) & P0 #3 (workbuddy_auto_approve default False)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import agy_sidecar_worker
import l3_backends
from l3_backends.antigravity import AntigravityBackend, _summarize_transcript
from l3_backends.dsh import DshBackend
from l3_backends.workbuddy import WorkBuddyBackend


class TestL3SecurityHardening(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.tasks_root = self.state_dir / "agy_tasks"
        self.pending = self.tasks_root / "pending"
        self.active = self.tasks_root / "active"
        self.pending.mkdir(parents=True, exist_ok=True)
        self.active.mkdir(parents=True, exist_ok=True)
        self.hb = self.tasks_root / "worker_heartbeat.json"
        self.cfg_file = self.state_dir / "l3_backend_config.json"
        self.dotenv_file = self.tmp_root / ".env"

        self._initial_threads = set(threading.enumerate())
        self._patches = [
            patch.object(l3_backends, "STATE_DIR", self.state_dir),
            patch.object(l3_backends, "TASKS_ROOT", self.tasks_root),
            patch.object(l3_backends, "PENDING_DIR", self.pending),
            patch.object(l3_backends, "ACTIVE_DIR", self.active),
            patch.object(l3_backends, "HEARTBEAT_FILE", self.hb),
            patch.object(l3_backends, "L3_CONFIG_FILE", self.cfg_file),
            patch.object(l3_backends, "DOTENV_FILE", self.dotenv_file),
            patch("l3_backends.antigravity.PENDING_DIR", self.pending),
            patch("l3_backends.antigravity.ACTIVE_DIR", self.active),
            patch("l3_backends.antigravity.HEARTBEAT_FILE", self.hb),
            patch.object(agy_sidecar_worker, "SERVER_DIR", self.tmp_root),
            patch.object(agy_sidecar_worker, "STATE_DIR", self.state_dir),
            patch.object(agy_sidecar_worker, "TASKS_ROOT", self.tasks_root),
            patch.object(agy_sidecar_worker, "PENDING_DIR", self.pending),
            patch.object(agy_sidecar_worker, "ACTIVE_DIR", self.active),
            patch.object(agy_sidecar_worker, "HEARTBEAT_FILE", self.hb),
        ]
        for p in self._patches:
            p.start()
        with l3_backends._dotenv_lock:
            l3_backends._dotenv_cache_key = None
            l3_backends._dotenv_cache_map = {}

    def tearDown(self):
        try:
            l3_backends.wait_for_worker_threads(timeout=5.0)
            cur = threading.current_thread()
            for t in list(threading.enumerate()):
                if t is cur or t in self._initial_threads:
                    continue
                t.join(timeout=2.0)
        finally:
            for p in reversed(self._patches):
                p.stop()
            with l3_backends._dotenv_lock:
                l3_backends._dotenv_cache_key = None
                l3_backends._dotenv_cache_map = {}
            self._tmp.cleanup()

    def test_state_paths_live_under_pc_mcp_state_not_data(self):
        # Inspect unpatched module-level defaults via the original descriptors
        for p in reversed(self._patches):
            p.stop()
        try:
            expected_state = (MCP_DIR / "state").resolve()
            data_dir = (MCP_DIR.parent / "data").resolve()

            for path_obj in (
                l3_backends.STATE_DIR,
                l3_backends.L3_CONFIG_FILE,
                l3_backends.TASKS_ROOT,
                l3_backends.PENDING_DIR,
                l3_backends.ACTIVE_DIR,
                l3_backends.HEARTBEAT_FILE,
                agy_sidecar_worker.STATE_DIR,
                agy_sidecar_worker.TASKS_ROOT,
                agy_sidecar_worker.PENDING_DIR,
                agy_sidecar_worker.ACTIVE_DIR,
                agy_sidecar_worker.HEARTBEAT_FILE,
            ):
                resolved = path_obj.resolve()
                self.assertTrue(
                    resolved.is_relative_to(expected_state),
                    f"{resolved} must live under host-private {expected_state}",
                )
                self.assertFalse(
                    resolved.is_relative_to(data_dir),
                    f"{resolved} must NOT live under container-mounted {data_dir}",
                )

            self.assertEqual(l3_backends.L3_CONFIG_FILE.resolve(), (expected_state / "l3_backend_config.json").resolve())
            self.assertEqual(l3_backends.TASKS_ROOT.resolve(), (expected_state / "agy_tasks").resolve())
            self.assertEqual(agy_sidecar_worker.TASKS_ROOT.resolve(), (expected_state / "agy_tasks").resolve())
        finally:
            for p in self._patches:
                p.start()

    def test_write_l3_config_rejects_host_command_and_workdir_injection(self):
        with patch.dict(os.environ, {}, clear=True), patch("winreg.OpenKey", side_effect=FileNotFoundError):
            baseline = l3_backends.write_l3_config({"backend": "dsh", "timeout_seconds": 240})
            self.assertEqual(baseline["backend"], "dsh")
            self.assertEqual(baseline["timeout_seconds"], 240)

            forbidden_payloads = [
                {"dsh_cmd": "calc.exe"},
                {"backend": "dsh", "dsh_cmd": "powershell -c evil"},
                {"workbuddy_cmd": "cmd.exe /c evil"},
                {"workdir": r"C:\Windows\System32"},
                {"workbuddy_auto_approve": True},
                {"workbuddy_auto_approve": False},
                {"dsh_cmd": ""},
                {"dsh_cmd": None},
                {"unknown_field": "abc"},
                {"backend": "bash"},
                {"backend": 123},
                {"timeout_seconds": True},
                {"timeout_seconds": "not-an-int"},
                {"timeout_seconds": 120.5},
                {"timeout": 120, "timeout_seconds": 300},
            ]
            for payload in forbidden_payloads:
                with self.subTest(payload=payload):
                    with self.assertRaises(ValueError):
                        l3_backends.write_l3_config(payload)

            # Non-dict payload rejected
            with self.assertRaises(ValueError):
                l3_backends.write_l3_config(["dsh"])  # type: ignore[arg-type]

            # Alias `timeout` is accepted and persisted as `timeout_seconds`
            updated = l3_backends.write_l3_config({"timeout": 180})
            self.assertEqual(updated["backend"], "dsh")
            self.assertEqual(updated["timeout_seconds"], 180)

            # Disk JSON must contain ONLY backend and timeout_seconds
            disk_obj = json.loads(self.cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(disk_obj, {"backend": "dsh", "timeout_seconds": 180})

    def test_tampered_config_json_cannot_override_host_dotenv_values(self):
        safe_workdir = self.tmp_root / "safe_wd"
        evil_workdir = self.tmp_root / "evil_wd"
        safe_workdir.mkdir()
        evil_workdir.mkdir()

        self.dotenv_file.write_text(
            f'XIAOZHI_L3_WORKDIR="{safe_workdir}"\n'
            "DSH_CMD=host-dsh\n"
            "WORKBUDDY_CMD=host-codebuddy\n",
            encoding="utf-8",
        )
        # Simulate attacker tampering directly with l3_backend_config.json on disk
        self.cfg_file.write_text(
            json.dumps(
                {
                    "backend": "workbuddy",
                    "timeout_seconds": 420,
                    "dsh_cmd": "evil-dsh.exe",
                    "workbuddy_cmd": "evil-wb.exe",
                    "workdir": str(evil_workdir),
                    "workbuddy_auto_approve": True,
                }
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {}, clear=True), patch("winreg.OpenKey", side_effect=FileNotFoundError):
            cfg = l3_backends.read_l3_config()
            self.assertEqual(cfg["backend"], "workbuddy")
            self.assertEqual(cfg["timeout_seconds"], 420)
            self.assertEqual(cfg["dsh_cmd"], "host-dsh")
            self.assertEqual(cfg["workbuddy_cmd"], "host-codebuddy")
            self.assertEqual(cfg["workdir"], str(safe_workdir))
            self.assertFalse(cfg["workbuddy_auto_approve"])

            # Sidecar workdir resolution must also honor host .env and NEVER trust JSON
            self.assertEqual(agy_sidecar_worker._resolve_default_workdir(), str(safe_workdir))

    def test_workbuddy_auto_approve_defaults_to_false_and_omits_y_flag(self):
        with patch.dict(os.environ, {}, clear=True), patch("winreg.OpenKey", side_effect=FileNotFoundError):
            self.assertFalse(l3_backends.default_l3_config()["workbuddy_auto_approve"])
            self.assertFalse(l3_backends.read_l3_config()["workbuddy_auto_approve"])

            self.dotenv_file.write_text(
                f'XIAOZHI_L3_WORKDIR="{self.tmp_root}"\n'
                "WORKBUDDY_CMD=codebuddy\n",
                encoding="utf-8",
            )
            cfg = l3_backends.read_l3_config()
            self.assertFalse(cfg["workbuddy_auto_approve"])

            captured: list[list[str]] = []

            def _fake_run(cmd, **kwargs):
                captured.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

            backend = WorkBuddyBackend()
            with patch("l3_backends.workbuddy.run_subprocess_with_tree_kill", side_effect=_fake_run):
                backend.dispatch("t_wb_default_safe", "检查仓库状态", False, cfg)
                self.assertEqual(l3_backends.wait_for_worker_threads(("t_wb_default_safe",), timeout=5.0), [])

            self.assertEqual(len(captured), 1)
            self.assertNotIn("-y", captured[0])
            self.assertEqual(captured[0], ["codebuddy", "-p", "检查仓库状态", "--output-format", "text"])

            # Even when workbuddy_auto_approve key is omitted from cfg, dispatch still defaults to False
            captured.clear()
            with patch("l3_backends.workbuddy.run_subprocess_with_tree_kill", side_effect=_fake_run):
                backend.dispatch("t_wb_omitted_key", "检查状态", False, {"workbuddy_cmd": "codebuddy", "workdir": str(self.tmp_root)})
                self.assertEqual(l3_backends.wait_for_worker_threads(("t_wb_omitted_key",), timeout=5.0), [])
            self.assertNotIn("-y", captured[0])

            # Explicit host .env override enables workbuddy_auto_approve
            time.sleep(0.02)
            self.dotenv_file.write_text(
                f'XIAOZHI_L3_WORKDIR="{self.tmp_root}"\n'
                "WORKBUDDY_CMD=codebuddy\n"
                "WORKBUDDY_AUTO_APPROVE=true\n",
                encoding="utf-8",
            )
            cfg_enabled = l3_backends.read_l3_config()
            self.assertTrue(cfg_enabled["workbuddy_auto_approve"])

    def test_task_id_validation_rejects_traversal_and_malformed_ids(self):
        bad_ids = [
            "../evil",
            "..\\evil",
            "../../Windows/win.ini",
            "a/b",
            "a\\b",
            "%2e%2e",
            "foo\x00bar",
            "",
            "   ",
            ".",
            "..",
            "a" * 65,
            "valid_id\n",
            "has space",
            "id;whoami",
            "CON",
            "nul",
            "COM1",
            "lpt9",
            None,
            123,
            ["t1"],
        ]
        for bad in bad_ids:
            with self.subTest(bad_id=bad):
                with self.assertRaises(ValueError):
                    agy_sidecar_worker.validate_task_id(bad)
                with self.assertRaises(ValueError):
                    agy_sidecar_worker._resolve_task_file(self.active, bad)
                with self.assertRaises(ValueError):
                    l3_backends.validate_task_id(bad)
                with self.assertRaises(ValueError):
                    l3_backends.resolve_task_file(self.active, bad)
                with self.assertRaises(ValueError):
                    l3_backends.save_active_task(bad, {"id": bad})  # type: ignore[arg-type]

        for good in ("20261118_110447_cfee83", "t_dsh-1", "A" * 64, "a"):
            with self.subTest(good_id=good):
                p1 = agy_sidecar_worker._resolve_task_file(self.active, good)
                p2 = l3_backends.resolve_task_file(self.active, good)
                self.assertEqual(p1, p2)
                self.assertTrue(p1.is_relative_to(self.active.resolve()))
                self.assertEqual(p1.parent, self.active.resolve())

    def test_resolve_task_file_defense_in_depth_containment_without_regex(self):
        # Even if TASK_ID_RE were somehow bypassed, .resolve().is_relative_to() must still block escape
        permissive = re.compile(r".+", re.DOTALL)
        with patch.object(agy_sidecar_worker, "TASK_ID_RE", permissive):
            with self.assertRaises(ValueError):
                agy_sidecar_worker._resolve_task_file(self.active, "../escaped")
            with self.assertRaises(ValueError):
                agy_sidecar_worker._resolve_task_file(self.active, "sub/nested")
        with patch.object(l3_backends, "TASK_ID_RE", permissive):
            with self.assertRaises(ValueError):
                l3_backends.resolve_task_file(self.active, "../escaped")
            with self.assertRaises(ValueError):
                l3_backends.resolve_task_file(self.active, "sub/nested")

    def test_sidecar_process_task_blocks_traversal_ids_and_invalid_conversation_id(self):
        outside_marker = self.tasks_root / "pwned.json"
        for idx, malicious_payload in enumerate(
            (
                {"id": "../pwned", "prompt": "pwn"},
                {"id": "..\\pwned", "prompt": "pwn"},
                {"id": "nested/pwned", "prompt": "pwn"},
                {"id": "", "prompt": "pwn"},
                {"id": "a" * 80, "prompt": "pwn"},
                {"id": "foo\x00bar", "prompt": "pwn"},
            )
        ):
            tf = self.pending / f"poison_{idx}.json"
            tf.write_text(json.dumps(malicious_payload), encoding="utf-8")
            with patch.object(agy_sidecar_worker.subprocess, "Popen") as mock_popen:
                ok = agy_sidecar_worker._process_task(tf)
                self.assertFalse(ok)
                mock_popen.assert_not_called()
            self.assertFalse(tf.exists(), "Poison task file must be removed to prevent infinite loop")
            self.assertFalse(outside_marker.exists(), "Traversal must never write outside ACTIVE_DIR")
            self.assertEqual(list(self.active.glob("*.json")), [])

        # Invalid conversation_id (path traversal or CLI flag injection) must NOT spawn agentapi
        for bad_conv in ("../../etc/passwd", "--malicious-flag", "not-a-uuid"):
            tf = self.pending / "bad_conv_task.json"
            tf.write_text(
                json.dumps({"id": "valid_task_1", "prompt": "hi", "conversation_id": bad_conv}),
                encoding="utf-8",
            )
            with patch.object(agy_sidecar_worker.subprocess, "Popen") as mock_popen:
                ok = agy_sidecar_worker._process_task(tf)
                self.assertFalse(ok)
                mock_popen.assert_not_called()
            self.assertFalse(tf.exists())
            rec = json.loads((self.active / "valid_task_1.json").read_text(encoding="utf-8"))
            self.assertEqual(rec["status"], "error")
            (self.active / "valid_task_1.json").unlink()

    def test_sidecar_process_task_accepts_valid_id_and_uses_host_dotenv_workdir(self):
        host_wd = self.tmp_root / "host_workspace"
        host_wd.mkdir()
        self.dotenv_file.write_text(f'XIAOZHI_L3_WORKDIR="{host_wd}"\n', encoding="utf-8")

        tf = self.pending / "20261120_120000_abcdef.json"
        tf.write_text(
            json.dumps({"id": "20261120_120000_abcdef", "prompt": "检查测试结果"}),
            encoding="utf-8",
        )

        fake_proc = MagicMock()
        fake_proc.pid = 424242
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = (
            '{"conversationId": "11089b72-95cd-465c-8ba2-e6dcd5d03bfe"}',
            "",
        )

        captured_cmds: list[list[str]] = []

        def _fake_popen(cmd, **kwargs):
            captured_cmds.append(cmd)
            return fake_proc

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("winreg.OpenKey", side_effect=FileNotFoundError),
            patch.object(agy_sidecar_worker, "_build_agentapi_prefix", return_value=["agentapi.exe"]),
            patch.object(agy_sidecar_worker.subprocess, "Popen", side_effect=_fake_popen),
        ):
            ok = agy_sidecar_worker._process_task(tf)

        self.assertTrue(ok)
        self.assertFalse(tf.exists())
        out_file = self.active / "20261120_120000_abcdef.json"
        self.assertTrue(out_file.is_file())
        rec = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertEqual(rec["status"], "dispatched")
        self.assertEqual(rec["conversation_id"], "11089b72-95cd-465c-8ba2-e6dcd5d03bfe")
        self.assertEqual(len(captured_cmds), 1)
        self.assertIn(str(host_wd), captured_cmds[0][-1])

    def test_antigravity_transcript_and_backends_reject_traversal(self):
        # _summarize_transcript refuses traversal conv_id
        msg = _summarize_transcript("../../evil")
        self.assertIn("无效", msg)

        # Backends refuse traversal task_id upfront
        with self.assertRaises(ValueError):
            AntigravityBackend().dispatch("../evil", "test", False, {})
        with self.assertRaises(ValueError):
            DshBackend().dispatch("../evil", "test", False, {})
        with self.assertRaises(ValueError):
            WorkBuddyBackend().dispatch("../evil", "test", False, {})

    def test_migrate_legacy_l3_state_ignores_untrusted_commands_and_pending(self):
        legacy_data = self.tmp_root / "legacy_data"
        (legacy_data / "agy_tasks" / "active").mkdir(parents=True)
        (legacy_data / "agy_tasks" / "pending").mkdir(parents=True)

        # Legacy config contains poisoned host commands + workbuddy_auto_approve=True
        (legacy_data / "l3_backend_config.json").write_text(
            json.dumps(
                {
                    "backend": "dsh",
                    "timeout_seconds": 600,
                    "dsh_cmd": "calc.exe",
                    "workbuddy_cmd": "cmd.exe",
                    "workdir": r"C:\Windows",
                    "workbuddy_auto_approve": True,
                }
            ),
            encoding="utf-8",
        )
        # Valid legacy active task + invalid traversal active task + untrusted pending task
        (legacy_data / "agy_tasks" / "active" / "20261118_good.json").write_text(
            json.dumps({"id": "20261118_good", "status": "dispatched"}), encoding="utf-8"
        )
        (legacy_data / "agy_tasks" / "active" / "bad_payload.json").write_text(
            json.dumps({"id": "../evil", "status": "dispatched"}), encoding="utf-8"
        )
        (legacy_data / "agy_tasks" / "pending" / "untrusted_pending.json").write_text(
            json.dumps({"id": "untrusted_pending", "prompt": "evil"}), encoding="utf-8"
        )

        with patch.dict(os.environ, {}, clear=True), patch("winreg.OpenKey", side_effect=FileNotFoundError):
            res = l3_backends.migrate_legacy_l3_state(legacy_data)
            self.assertTrue(res["migrated_config"])
            self.assertEqual(res["migrated_active_tasks"], 1)

            disk_cfg = json.loads(self.cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(disk_cfg, {"backend": "dsh", "timeout_seconds": 600})
            cfg = l3_backends.read_l3_config()
            self.assertEqual(cfg["dsh_cmd"], "")
            self.assertEqual(cfg["workbuddy_cmd"], "")
            self.assertEqual(cfg["workdir"], "")
            self.assertFalse(cfg["workbuddy_auto_approve"])

            self.assertTrue((self.active / "20261118_good.json").is_file())
            self.assertFalse((self.active / "bad_payload.json").exists())
            self.assertEqual(list(self.pending.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

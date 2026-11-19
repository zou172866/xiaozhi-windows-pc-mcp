"""Unit tests for pluggable L3 backends (Antigravity / DeepSeek DSH / Tencent WorkBuddy / None)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import l3_backends
from l3_backends.antigravity import AntigravityBackend
from l3_backends.dsh import DshBackend, resolve_dsh_prefix
from l3_backends.none_backend import NoneBackend
from l3_backends.workbuddy import WorkBuddyBackend, resolve_workbuddy_prefix


class TestPluggableL3Backends(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.pending = self.tmp_root / "pending"
        self.active = self.tmp_root / "active"
        self.pending.mkdir(parents=True, exist_ok=True)
        self.active.mkdir(parents=True, exist_ok=True)
        self.hb = self.tmp_root / "worker_heartbeat.json"
        self.cfg_file = self.tmp_root / "l3_backend_config.json"

        self._patches = [
            patch.object(l3_backends, "PENDING_DIR", self.pending),
            patch.object(l3_backends, "ACTIVE_DIR", self.active),
            patch.object(l3_backends, "HEARTBEAT_FILE", self.hb),
            patch.object(l3_backends, "L3_CONFIG_FILE", self.cfg_file),
            patch("l3_backends.antigravity.PENDING_DIR", self.pending),
            patch("l3_backends.antigravity.ACTIVE_DIR", self.active),
            patch("l3_backends.antigravity.HEARTBEAT_FILE", self.hb),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def test_dynamic_paths_have_no_hardcoded_username_or_drive(self):
        for fname in ("pc_control_mcp.py", "agy_sidecar_worker.py"):
            src = (MCP_DIR / fname).read_text(encoding="utf-8")
            self.assertNotIn("C:\\Users\\Administrator", src)
            self.assertNotIn("e:\\故事\\1", src)
            self.assertNotIn("e:/故事/1", src)

    def test_config_roundtrip_and_normalization(self):
        saved = l3_backends.write_l3_config(
            {
                "backend": "dsh",
                "workdir": str(self.tmp_root),
                "dsh_cmd": "dsh",
                "workbuddy_cmd": "codebuddy",
                "workbuddy_auto_approve": False,
                "timeout_seconds": 99999,
            }
        )
        self.assertEqual(saved["backend"], "dsh")
        self.assertEqual(saved["timeout_seconds"], 1800)
        self.assertFalse(saved["workbuddy_auto_approve"])

        loaded = l3_backends.read_l3_config()
        self.assertEqual(loaded["backend"], "dsh")
        self.assertEqual(loaded["workdir"], str(self.tmp_root))

    def test_dsh_dispatch_headless_and_followup_context(self):
        backend = DshBackend()
        cfg = l3_backends.write_l3_config(
            {"backend": "dsh", "workdir": str(self.tmp_root), "dsh_cmd": "dsh"}
        )

        captured_cmds = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append((cmd, kwargs.get("cwd")))
            return subprocess.CompletedProcess(cmd, 0, stdout="已整理完 12 份文档并生成汇总报告。", stderr="")

        with patch("l3_backends.dsh.subprocess.run", side_effect=_fake_run):
            msg = backend.dispatch("t_dsh_1", "帮我整理桌面文档", False, cfg)
            self.assertIn("DeepSeek DSH", msg)
            for _ in range(40):
                rec = l3_backends.load_last_active_task(backend="dsh")
                if rec and rec.get("status") == "completed":
                    break
                time.sleep(0.05)

            status_txt = backend.check_status(cfg)
            self.assertIn("已完成任务", status_txt)
            self.assertIn("12 份文档", status_txt)
            self.assertEqual(captured_cmds[0][0][:3], ["dsh", "--profile", "headless"])
            self.assertEqual(captured_cmds[0][1], str(self.tmp_root))

            # Follow-up must inject previous prompt + summary context
            backend.dispatch("t_dsh_2", "顺便发成 Markdown", True, cfg)
            for _ in range(40):
                if len(captured_cmds) >= 2:
                    break
                time.sleep(0.05)
            self.assertIn("[接续上一轮任务]", captured_cmds[1][0][3])
            self.assertIn("12 份文档", captured_cmds[1][0][3])

    def test_dsh_npx_fallback_detection(self):
        cfg = {"dsh_cmd": ""}
        with patch("l3_backends.dsh.shutil.which", side_effect=lambda x: r"C:\node\npx.cmd" if x == "npx.cmd" else None):
            prefix, desc = resolve_dsh_prefix(cfg)
            self.assertEqual(prefix, [r"C:\node\npx.cmd", "-y", "@deepseek-ai/dsh"])
            self.assertIn("npx", desc)

    def test_workbuddy_dispatch_flags_and_continue(self):
        backend = WorkBuddyBackend()
        cfg = l3_backends.write_l3_config(
            {
                "backend": "workbuddy",
                "workdir": str(self.tmp_root),
                "workbuddy_cmd": "codebuddy",
                "workbuddy_auto_approve": True,
            }
        )

        captured_cmds = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="WorkBuddy 已输出本周销售周报。", stderr="")

        with patch("l3_backends.workbuddy.subprocess.run", side_effect=_fake_run):
            msg = backend.dispatch("t_wb_1", "写一份本周周报", False, cfg)
            self.assertIn("腾讯 WorkBuddy", msg)
            for _ in range(40):
                rec = l3_backends.load_last_active_task(backend="workbuddy")
                if rec and rec.get("status") == "completed":
                    break
                time.sleep(0.05)

            self.assertEqual(
                captured_cmds[0],
                ["codebuddy", "-p", "写一份本周周报", "--output-format", "text", "-y"],
            )
            self.assertIn("销售周报", backend.check_status(cfg))

            # Follow-up should attach -c
            backend.dispatch("t_wb_2", "精简成三条重点", True, cfg)
            for _ in range(40):
                if len(captured_cmds) >= 2:
                    break
                time.sleep(0.05)
            self.assertEqual(
                captured_cmds[1],
                ["codebuddy", "-c", "-p", "精简成三条重点", "--output-format", "text", "-y"],
            )

    def test_workbuddy_auto_detect_priority(self):
        with patch("l3_backends.workbuddy.shutil.which", side_effect=lambda x: r"C:\bin\cbc.cmd" if x == "cbc.cmd" else None):
            prefix, desc = resolve_workbuddy_prefix({"workbuddy_cmd": ""})
            self.assertEqual(prefix, [r"C:\bin\cbc.cmd"])
            self.assertIn("cbc.cmd", desc)

    def test_none_backend_and_antigravity_offline_queue(self):
        nb = NoneBackend()
        self.assertTrue(nb.detect({})["ready"])
        self.assertIn("关闭", nb.dispatch("t_none", "测试", False, {}))

        ab = AntigravityBackend()
        reply = ab.dispatch("t_agy_1", "分析日志", False, {})
        self.assertIn("伴生服务还没开启", reply)
        self.assertTrue((self.pending / "t_agy_1.json").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Unit tests for pluggable L3 backends (Antigravity / DeepSeek DSH / Tencent WorkBuddy / None)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
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
        self.dotenv_file = self.tmp_root / ".env"

        self._initial_threads = set(threading.enumerate())
        self._prod_active_dir = MCP_DIR / "state" / "agy_tasks" / "active"
        self._legacy_prod_active_dir = MCP_DIR.parent / "data" / "agy_tasks" / "active"
        self._patches = [
            patch.object(l3_backends, "PENDING_DIR", self.pending),
            patch.object(l3_backends, "ACTIVE_DIR", self.active),
            patch.object(l3_backends, "HEARTBEAT_FILE", self.hb),
            patch.object(l3_backends, "L3_CONFIG_FILE", self.cfg_file),
            patch.object(l3_backends, "DOTENV_FILE", self.dotenv_file),
            patch("l3_backends.antigravity.PENDING_DIR", self.pending),
            patch("l3_backends.antigravity.ACTIVE_DIR", self.active),
            patch("l3_backends.antigravity.HEARTBEAT_FILE", self.hb),
        ]
        for p in self._patches:
            p.start()
        with l3_backends._dotenv_lock:
            l3_backends._dotenv_cache_key = None
            l3_backends._dotenv_cache_map = {}

    def _join_test_threads(
        self,
        task_ids: list[str] | tuple[str, ...] | None = None,
        timeout: float = 5.0,
    ) -> list[str]:
        """Join tracked L3 workers (and, when task_ids is None, any new thread spawned during the test)."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        alive = list(l3_backends.wait_for_worker_threads(task_ids=task_ids, timeout=timeout))
        if task_ids is None:
            cur = threading.current_thread()
            for t in list(threading.enumerate()):
                if t is cur or t in self._initial_threads:
                    continue
                remaining = max(0.0, deadline - time.monotonic())
                t.join(timeout=remaining)
                if t.is_alive() and t.name not in alive:
                    alive.append(t.name)
        return alive

    def _wait_for_worker_threads(
        self,
        task_ids: list[str] | tuple[str, ...] | None = None,
        timeout: float = 5.0,
    ) -> None:
        alive = self._join_test_threads(task_ids=task_ids, timeout=timeout)
        self.assertEqual(
            alive,
            [],
            f"Background L3 worker thread(s) did not finish within {timeout}s: {alive}",
        )

    def tearDown(self):
        alive_threads: list[str] = []
        try:
            alive_threads = self._join_test_threads(task_ids=None, timeout=5.0)
        finally:
            for p in reversed(self._patches):
                p.stop()
            with l3_backends._dotenv_lock:
                l3_backends._dotenv_cache_key = None
                l3_backends._dotenv_cache_map = {}
            try:
                self._tmp.cleanup()
            except OSError:
                if not alive_threads:
                    raise
        if alive_threads:
            self.fail(f"Background L3 worker threads still alive during tearDown: {alive_threads}")

    def test_dynamic_paths_have_no_hardcoded_username_or_drive(self):
        for fname in ("pc_control_mcp.py", "agy_sidecar_worker.py"):
            src = (MCP_DIR / fname).read_text(encoding="utf-8")
            self.assertNotIn("C:\\Users\\Administrator", src)
            self.assertNotIn("e:\\故事\\1", src)
            self.assertNotIn("e:/故事/1", src)
        server_dir = MCP_DIR.parent
        personal_vault = "E:" + "\\love"
        for rel in (".env.example", "data/init_setup.ps1", "anima-brain/obsidian_exporter.py"):
            src = (server_dir / rel).read_text(encoding="utf-8-sig")
            self.assertNotIn(personal_vault, src, f"Hardcoded personal vault path found in {rel}")
        ps1_src = (server_dir / "data/init_setup.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('Join-Path $serverDir "data\\obsidian_export"', ps1_src)

    def test_config_roundtrip_and_normalization(self):
        self.dotenv_file.write_text(
            f'XIAOZHI_L3_WORKDIR="{self.tmp_root}"\n'
            "DSH_CMD=dsh\n"
            "WORKBUDDY_CMD=codebuddy\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True), patch("winreg.OpenKey", side_effect=FileNotFoundError):
            saved = l3_backends.write_l3_config(
                {
                    "backend": "dsh",
                    "timeout_seconds": 99999,
                }
            )
            self.assertEqual(saved["backend"], "dsh")
            self.assertEqual(saved["timeout_seconds"], 1800)
            self.assertFalse(saved["workbuddy_auto_approve"])
            self.assertEqual(saved["workdir"], str(self.tmp_root))
            self.assertEqual(saved["dsh_cmd"], "dsh")
            self.assertEqual(saved["workbuddy_cmd"], "codebuddy")

            raw_disk = json.loads(self.cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(raw_disk, {"backend": "dsh", "timeout_seconds": 1800})

            loaded = l3_backends.read_l3_config()
            self.assertEqual(loaded["backend"], "dsh")
            self.assertEqual(loaded["workdir"], str(self.tmp_root))
            self.assertFalse(loaded["workbuddy_auto_approve"])

    def test_dsh_dispatch_headless_and_followup_context(self):
        backend = DshBackend()
        with patch.dict(
            os.environ,
            {"XIAOZHI_L3_WORKDIR": str(self.tmp_root), "DSH_CMD": "dsh"},
        ):
            cfg = l3_backends.write_l3_config({"backend": "dsh"})

        captured_cmds = []
        fake_outputs = [
            "已整理完 12 份文档并生成汇总报告。",
            "已将汇总报告导出为 Markdown 文件。",
        ]

        def _fake_run(cmd, **kwargs):
            idx = len(captured_cmds)
            captured_cmds.append((cmd, kwargs.get("cwd")))
            out = fake_outputs[min(idx, len(fake_outputs) - 1)]
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        with patch("l3_backends.dsh.run_subprocess_with_tree_kill", side_effect=_fake_run):
            try:
                msg = backend.dispatch("t_dsh_1", "帮我整理桌面文档", False, cfg)
                self.assertIn("DeepSeek DSH", msg)
                self._wait_for_worker_threads(("t_dsh_1",))
                rec1 = json.loads((self.active / "t_dsh_1.json").read_text(encoding="utf-8"))
                self.assertEqual(rec1.get("status"), "completed")

                status_txt = backend.check_status(cfg)
                self.assertIn("已完成任务", status_txt)
                self.assertIn("12 份文档", status_txt)
                self.assertEqual(captured_cmds[0][0][:3], ["dsh", "--profile", "headless"])
                self.assertEqual(captured_cmds[0][1], str(self.tmp_root))

                # Follow-up must inject previous prompt + summary context
                backend.dispatch("t_dsh_2", "顺便发成 Markdown", True, cfg)
                self._wait_for_worker_threads(("t_dsh_2",))
                rec2 = json.loads((self.active / "t_dsh_2.json").read_text(encoding="utf-8"))
                self.assertEqual(rec2.get("status"), "completed")
                self.assertEqual(len(captured_cmds), 2)
                self.assertIn("[接续上一轮任务]", captured_cmds[1][0][3])
                self.assertIn("12 份文档", captured_cmds[1][0][3])
                self.assertIn("Markdown 文件", backend.check_status(cfg))
                self.assertFalse((self._prod_active_dir / "t_dsh_1.json").exists())
                self.assertFalse((self._prod_active_dir / "t_dsh_2.json").exists())
                self.assertFalse((self._legacy_prod_active_dir / "t_dsh_1.json").exists())
                self.assertFalse((self._legacy_prod_active_dir / "t_dsh_2.json").exists())
            finally:
                # Drain without asserting so any original test failure is never masked
                self._join_test_threads(("t_dsh_1", "t_dsh_2"), timeout=5.0)

    def test_dsh_npx_fallback_detection(self):
        from l3_backends.dsh import DSH_NPX_PACKAGE

        cfg = {"dsh_cmd": ""}
        with patch("l3_backends.dsh.shutil.which", side_effect=lambda x: r"C:\node\npx.cmd" if x == "npx.cmd" else None):
            prefix, desc = resolve_dsh_prefix(cfg)
            self.assertEqual(prefix, [r"C:\node\npx.cmd", "-y", DSH_NPX_PACKAGE])
            self.assertRegex(prefix[2], r"^@deepseek-ai/dsh@\d+\.\d+\.\d+")
            self.assertIn("npx", desc)

    def test_workbuddy_dispatch_flags_and_continue(self):
        backend = WorkBuddyBackend()
        with patch.dict(
            os.environ,
            {
                "XIAOZHI_L3_WORKDIR": str(self.tmp_root),
                "WORKBUDDY_CMD": "codebuddy",
                "WORKBUDDY_AUTO_APPROVE": "true",
            },
        ):
            cfg = l3_backends.write_l3_config({"backend": "workbuddy"})
        self.assertTrue(cfg["workbuddy_auto_approve"])

        captured_cmds = []
        fake_outputs = [
            "WorkBuddy 已输出本周销售周报。",
            "WorkBuddy 已精简为三条核心重点。",
        ]

        def _fake_run(cmd, **kwargs):
            idx = len(captured_cmds)
            captured_cmds.append(cmd)
            out = fake_outputs[min(idx, len(fake_outputs) - 1)]
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        with patch("l3_backends.workbuddy.run_subprocess_with_tree_kill", side_effect=_fake_run):
            try:
                msg = backend.dispatch("t_wb_1", "写一份本周周报", False, cfg)
                self.assertIn("腾讯 WorkBuddy", msg)
                self._wait_for_worker_threads(("t_wb_1",))
                rec1 = json.loads((self.active / "t_wb_1.json").read_text(encoding="utf-8"))
                self.assertEqual(rec1.get("status"), "completed")

                self.assertEqual(
                    captured_cmds[0],
                    ["codebuddy", "-p", "写一份本周周报", "--output-format", "text", "-y"],
                )
                self.assertIn("销售周报", backend.check_status(cfg))

                # Follow-up should attach -c
                backend.dispatch("t_wb_2", "精简成三条重点", True, cfg)
                self._wait_for_worker_threads(("t_wb_2",))
                rec2 = json.loads((self.active / "t_wb_2.json").read_text(encoding="utf-8"))
                self.assertEqual(rec2.get("status"), "completed")
                self.assertEqual(len(captured_cmds), 2)
                self.assertEqual(
                    captured_cmds[1],
                    ["codebuddy", "-c", "-p", "精简成三条重点", "--output-format", "text", "-y"],
                )
                self.assertIn("三条核心重点", backend.check_status(cfg))
                self.assertFalse((self._prod_active_dir / "t_wb_1.json").exists())
                self.assertFalse((self._prod_active_dir / "t_wb_2.json").exists())
                self.assertFalse((self._legacy_prod_active_dir / "t_wb_1.json").exists())
                self.assertFalse((self._legacy_prod_active_dir / "t_wb_2.json").exists())
            finally:
                # Drain without asserting so any original test failure is never masked
                self._join_test_threads(("t_wb_1", "t_wb_2"), timeout=5.0)

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

    def test_dotenv_fallback_for_wmi_env_isolation(self):
        import agy_sidecar_worker
        from l3_backends.antigravity import _get_brain_dir

        self.dotenv_file.write_bytes(
            b"\xef\xbb\xbf"
            + (
                "# comment\n"
                "L3_BACKEND=antigravity # overridden below\n"
                "L3_BACKEND=workbuddy # inline comment must be stripped\n"
                'XIAOZHI_L3_WORKDIR="' + str(self.tmp_root) + '" # trailing comment\n'
                "export DSH_CMD='dsh --custom # kept inside quotes'\n"
                "WORKBUDDY_CMD=codebuddy\n"
                "ANTIGRAVITY_BRAIN_DIR=" + str(self.tmp_root / "brain") + "\n"
            ).encode("utf-8")
        )
        # Clear process env and bypass Windows registry so .env fallback is exercised directly
        with patch.dict(os.environ, {}, clear=True), patch("winreg.OpenKey", side_effect=FileNotFoundError):
            self.assertEqual(l3_backends.read_env_key("L3_BACKEND"), "workbuddy")
            self.assertEqual(l3_backends.read_env_key("DSH_CMD"), "dsh --custom # kept inside quotes")
            cfg = l3_backends.default_l3_config()
            self.assertEqual(cfg["backend"], "workbuddy")
            self.assertEqual(cfg["workdir"], str(self.tmp_root))
            self.assertEqual(cfg["workbuddy_cmd"], "codebuddy")
            self.assertEqual(_get_brain_dir(), self.tmp_root / "brain")
            with patch.object(agy_sidecar_worker, "SERVER_DIR", self.tmp_root):
                self.assertEqual(agy_sidecar_worker._read_dotenv_var("L3_BACKEND"), "workbuddy")
                self.assertEqual(agy_sidecar_worker._read_dotenv_var("XIAOZHI_L3_WORKDIR"), str(self.tmp_root))

            # Process env must override .env when explicitly set
            os.environ["L3_BACKEND"] = "dsh"
            self.assertEqual(l3_backends.read_env_key("L3_BACKEND"), "dsh")

        # Cache invalidates when .env is updated (even same-length or rapid rewrite via mtime_ns+size)
        time.sleep(0.02)
        self.dotenv_file.write_text("L3_BACKEND=none\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), patch("winreg.OpenKey", side_effect=FileNotFoundError):
            self.assertEqual(l3_backends.read_env_key("L3_BACKEND"), "none")

    def test_bat_ascii_crlf_and_ps1_bom_crlf(self):
        server_dir = MCP_DIR.parent
        bat_files = sorted(server_dir.glob("*.bat"))
        self.assertTrue(bat_files, "Expected launcher .bat files in repo root")
        for bp in bat_files:
            raw = bp.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"BOM forbidden in {bp.name}")
            self.assertFalse(any(x > 127 for x in raw), f"Non-ASCII byte in {bp.name}")
            self.assertEqual(raw.count(b"\n") - raw.count(b"\r\n"), 0, f"LF-only line in {bp.name}")

        ps1_files = sorted((server_dir / "data").glob("*.ps1"))
        self.assertTrue(ps1_files, "Expected PowerShell scripts in data/")
        for pp in ps1_files:
            raw = pp.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), f"Missing UTF-8 BOM in {pp.name}")
            self.assertEqual(raw.count(b"\n") - raw.count(b"\r\n"), 0, f"LF-only line in {pp.name}")

    def test_windows_cmd_shim_unwrapping_and_batch_fallback_sanitization(self):
        # 1. Global npm .cmd shim unwraps cleanly to [node.exe, cli.js, ...args] preserving newlines & metachars
        fake_node = self.tmp_root / "node.exe"
        fake_node.write_bytes(b"MZ")
        cli_js = self.tmp_root / "node_modules" / "@deepseek-ai" / "dsh" / "dist" / "cli.js"
        cli_js.parent.mkdir(parents=True, exist_ok=True)
        cli_js.write_text("// stub", encoding="utf-8")
        shim = self.tmp_root / "dsh.cmd"
        shim.write_text(
            '@ECHO off\r\n"%_prog%"  "%dp0%\\node_modules\\@deepseek-ai\\dsh\\dist\\cli.js" %*\r\n',
            encoding="utf-8",
        )
        multiline_prompt = "第一行 & %PATH%\n第二行 | echo hi > out.txt"
        prepared = l3_backends.prepare_subprocess_command([str(shim), "--profile", "headless", multiline_prompt])
        self.assertEqual(prepared[0], str(fake_node))
        self.assertEqual(Path(prepared[1]).resolve(), cli_js.resolve())
        self.assertEqual(prepared[2:], ["--profile", "headless", multiline_prompt])

        # 1b. %~dp0\... shim (like corepack.cmd) AND extensionless npm bin scripts must unwrap cleanly with zero literal %~dp0
        extless_bin = self.tmp_root / "node_modules" / "pkg" / "bin" / "mycli"
        extless_bin.parent.mkdir(parents=True, exist_ok=True)
        extless_bin.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        tilde_shim = self.tmp_root / "corepack_like.cmd"
        tilde_shim.write_text(
            '@IF EXIST "%~dp0\\node.exe" (\r\n  "%~dp0\\node.exe" "%~dp0\\node_modules\\pkg\\bin\\mycli" %*\r\n)\r\n',
            encoding="utf-8",
        )
        tilde_prep = l3_backends.prepare_subprocess_command([str(tilde_shim), multiline_prompt])
        self.assertEqual(tilde_prep[0], str(fake_node))
        self.assertEqual(Path(tilde_prep[1]).resolve(), extless_bin.resolve())
        self.assertNotIn("%~dp0", tilde_prep[1])
        self.assertEqual(tilde_prep[2], multiline_prompt)

        # 2. Un-unwrappable custom .bat falls back to newline flattening, --key=val sanitization, and blocks quote-breakout OS injection
        custom_bat = self.tmp_root / "custom_tool.bat"
        custom_bat.write_text("@echo off\r\necho ARGS: %*\r\n", encoding="utf-8")
        marker = self.tmp_root / "injected_marker.txt"
        title_attack = f"--title=hi\n& echo PWNED1 > {marker}"
        quote_attack = f'hello " & echo PWNED2 > {marker} & rem '
        fb = l3_backends.prepare_subprocess_command(
            [str(custom_bat), "-p", title_attack, quote_attack, "line1\nline2 %PATH% !VAR!", "a&b|c>d"]
        )
        self.assertEqual(fb[0], str(custom_bat))
        self.assertEqual(fb[1], "-p")
        self.assertTrue(fb[2].startswith("--title="))
        self.assertNotIn("\n", fb[2])
        self.assertNotIn("&", fb[2])
        self.assertNotIn('"', fb[3])
        self.assertNotIn("&", fb[3])
        self.assertNotIn("\n", fb[4])
        self.assertNotIn("%", fb[4])
        self.assertNotIn("!", fb[4])
        self.assertNotIn("&", fb[5])
        self.assertNotIn("|", fb[5])
        self.assertNotIn(">", fb[5])
        # Actually execute custom_tool.bat with sanitized args to confirm ZERO OS command injection and ZERO truncation!
        if os.name == "nt":
            res = subprocess.run(fb, capture_output=True, text=True, timeout=10)
            self.assertEqual(res.returncode, 0)
            self.assertFalse(marker.exists(), "Batch fallback allowed OS command injection!")
            self.assertIn("line2", res.stdout)

        # 3. agy_sidecar_worker.py also unwraps .bat pointing to .exe and sanitizes --title=... / quote breakout on fallback
        import agy_sidecar_worker

        fake_ls = self.tmp_root / "language_server.exe"
        fake_ls.write_bytes(b"MZ")
        ag_bat = self.tmp_root / "agentapi.bat"
        ag_bat.write_text(f'@echo off\r\n"{fake_ls}" agentapi %*\r\n', encoding="utf-8")
        ag_prep = agy_sidecar_worker._prepare_cmd([str(ag_bat), "new-conversation", "第一行\n第二行"])
        self.assertEqual(ag_prep, [str(fake_ls), "agentapi", "new-conversation", "第一行\n第二行"])
        ag_fb = agy_sidecar_worker._prepare_cmd([str(custom_bat), title_attack, quote_attack])
        self.assertNotIn("\n", ag_fb[1])
        self.assertNotIn("&", ag_fb[1])
        self.assertNotIn('"', ag_fb[2])
        self.assertNotIn("&", ag_fb[2])

    def test_timeout_kills_full_child_process_tree(self):
        import psutil

        pid_file = self.tmp_root / "grandchild.pid"
        # Parent spawns a sleeping child process and writes grandchild PID to pid_file, then sleeps forever
        parent_code = (
            "import subprocess, sys, time; "
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
            "time.sleep(60)"
        )
        spawned_pids = []
        with self.assertRaises(subprocess.TimeoutExpired):
            l3_backends.run_subprocess_with_tree_kill(
                [sys.executable, "-c", parent_code],
                timeout=1.5,
                on_spawn=lambda pid: spawned_pids.append(pid),
            )
        self.assertEqual(len(spawned_pids), 1)
        parent_pid = spawned_pids[0]
        # Give OS a short moment to finalize process table cleanup
        time.sleep(0.3)
        self.assertFalse(psutil.pid_exists(parent_pid), f"Parent PID {parent_pid} leaked after timeout")
        if pid_file.is_file():
            child_pid = int(pid_file.read_text(encoding="utf-8").strip())
            self.assertFalse(psutil.pid_exists(child_pid), f"Grandchild PID {child_pid} leaked after timeout")

    def test_zombie_running_task_auto_reap_and_atomic_write(self):
        # 1. Dead owner_pid running task is reaped immediately
        l3_backends.save_active_task(
            "zombie_dead_owner",
            {
                "id": "zombie_dead_owner",
                "backend": "dsh",
                "prompt": "old task",
                "status": "running",
                "owner_pid": 999999,
                "pid": 999998,
                "timeout_seconds": 300,
                "started_ts": time.time() - 20,
            },
        )
        # 2. Expired timeout running task is reaped
        l3_backends.save_active_task(
            "zombie_expired",
            {
                "id": "zombie_expired",
                "backend": "workbuddy",
                "prompt": "hung task",
                "status": "running",
                "owner_pid": os.getpid(),
                "pid": None,
                "timeout_seconds": 30,
                "started_ts": time.time() - 120,
            },
        )
        reaped = l3_backends.reap_stale_running_tasks()
        self.assertEqual(reaped, 2)
        for tid in ("zombie_dead_owner", "zombie_expired"):
            rec = json.loads((self.active / f"{tid}.json").read_text(encoding="utf-8"))
            self.assertEqual(rec["status"], "error")
            self.assertTrue(rec.get("reaped_zombie"))
            self.assertTrue(rec.get("finished_at"))
        # No leftover .tmp files in active dir
        self.assertEqual(list(self.active.glob("*.tmp")), [])

    def test_zombie_reap_preserves_latest_task_ordering_and_skips_live_inprocess_worker(self):
        now = time.time()
        # 1. Reaping an older zombie must NEVER hijack load_last_active_task ahead of a newer completed task
        p_old = l3_backends.save_active_task(
            "old_zombie_dsh",
            {
                "id": "old_zombie_dsh",
                "backend": "dsh",
                "prompt": "yesterday dead task",
                "status": "running",
                "owner_pid": 999999,
                "pid": 999998,
                "timeout_seconds": 300,
                "started_ts": now - 3600,
            },
        )
        os.utime(p_old, (now - 3600, now - 3600))
        p_new = l3_backends.save_active_task(
            "newer_ok_dsh",
            {
                "id": "newer_ok_dsh",
                "backend": "dsh",
                "prompt": "today completed task",
                "status": "completed",
                "summary": "今天的报告已生成",
                "owner_pid": os.getpid(),
                "timeout_seconds": 300,
                "started_ts": now - 10,
            },
        )
        os.utime(p_new, (now - 10, now - 10))

        latest = l3_backends.load_last_active_task(backend="dsh")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], "newer_ok_dsh")
        self.assertEqual(latest["status"], "completed")
        old_rec = json.loads(p_old.read_text(encoding="utf-8"))
        self.assertEqual(old_rec["status"], "error")
        self.assertTrue(old_rec.get("reaped_zombie"))
        self.assertLess(p_old.stat().st_mtime, p_new.stat().st_mtime)

        # 2. A running task whose tracked worker thread is still alive in THIS process must NOT be reaped prematurely
        p_live = l3_backends.save_active_task(
            "finishing_inproc",
            {
                "id": "finishing_inproc",
                "backend": "dsh",
                "prompt": "post-processing after subprocess exit",
                "status": "running",
                "owner_pid": os.getpid(),
                "pid": 999997,
                "timeout_seconds": 300,
                "started_ts": now - 8,
            },
        )
        gate = threading.Event()
        l3_backends.start_worker_thread("finishing_inproc", "l3-custom-finishing_inproc", lambda: gate.wait(2.0))
        try:
            self.assertEqual(l3_backends.reap_stale_running_tasks(), 0)
            live_rec = json.loads(p_live.read_text(encoding="utf-8"))
            self.assertEqual(live_rec["status"], "running")
        finally:
            gate.set()
            self._wait_for_worker_threads(("finishing_inproc",))
        # Once worker thread has exited without finalizing, dead child_pid IS reaped
        self.assertEqual(l3_backends.reap_stale_running_tasks(), 1)
        reaped_rec = json.loads(p_live.read_text(encoding="utf-8"))
        self.assertEqual(reaped_rec["status"], "error")
        self.assertTrue(reaped_rec.get("reaped_zombie"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Comprehensive unit & stress tests for Batch 3 host pc-mcp (18900) hardening:
- E-1: Watchdog 3x5s probe threshold, L3 survival on restart, 3-in-10min circuit breaker, append/rotate boot.log
- E-2: L3 per-workspace single-task mutex, busy message, explicit queue mode, and cancel
- E-3: Vision _gui_lock release during VLM, pre-click target-region hash staleness re-locate, persistent httpx.Client, fault-tolerant mark_id
- E-4: Sidecar dedicated heartbeat daemon thread & leading-dash prompt safety (without '--')
- H-6: l2_audit.jsonl 5MBx3 rotation, agy_tasks/active 50-record archive pruning, 64KB transcript tail read
- H-7: L1 Start Menu shortcut blacklist & directory-mtime cached index
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import agy_sidecar_worker
import l3_backends
from l3_backends.antigravity import (
    TRANSCRIPT_TAIL_BYTES,
    AntigravityBackend,
    _read_tail_lines,
    _summarize_transcript,
)
from l3_backends.dsh import DshBackend
from l3_backends.workbuddy import WorkBuddyBackend
import pc_control_mcp as pcm
import pc_mcp_watchdog as wd
from pc_mcp_modules import launcher, security, vision


_BENIGN_FG = {
    "hwnd": 7001,
    "pid": 8888,
    "process": "notepad.exe",
    "class": "Notepad",
    "title": "test.txt - 记事本",
}


class TestBatch3PcMcpHardening(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.tasks_root = self.state_dir / "agy_tasks"
        self.pending = self.tasks_root / "pending"
        self.active = self.tasks_root / "active"
        self.archive = self.tasks_root / "archive"
        self.pending.mkdir(parents=True, exist_ok=True)
        self.active.mkdir(parents=True, exist_ok=True)
        self.hb = self.tasks_root / "worker_heartbeat.json"
        self.cfg_file = self.state_dir / "l3_backend_config.json"
        self.dotenv_file = self.tmp_root / ".env"
        self.mode_file = self.state_dir / "pc_control_mode.json"
        self.audit_file = self.state_dir / "l2_audit.jsonl"

        self._initial_threads = set(threading.enumerate())
        self._patches = [
            patch.object(l3_backends, "STATE_DIR", self.state_dir),
            patch.object(l3_backends, "TASKS_ROOT", self.tasks_root),
            patch.object(l3_backends, "PENDING_DIR", self.pending),
            patch.object(l3_backends, "ACTIVE_DIR", self.active),
            patch.object(l3_backends, "ARCHIVE_DIR", self.archive),
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
            patch.object(pcm, "MODE_FILE", self.mode_file),
            patch.object(pcm, "L2_AUDIT_LOG_FILE", self.audit_file),
            patch.object(pcm, "LAST_GROUNDING_IMG", self.state_dir / "last_som_grounding.jpg"),
        ]
        for p in self._patches:
            p.start()
        pcm.clear_emergency_stop()
        launcher._clear_start_menu_cache()
        with l3_backends._dotenv_lock:
            l3_backends._dotenv_cache_key = None
            l3_backends._dotenv_cache_map = {}
        with l3_backends._workspace_guard_lock:
            l3_backends._workspace_active_tasks.clear()
            l3_backends._workspace_queues.clear()
            l3_backends._cancelled_task_ids.clear()

    def tearDown(self):
        try:
            l3_backends.wait_for_worker_threads(timeout=5.0)
            cur = threading.current_thread()
            for t in list(threading.enumerate()):
                if t is cur or t in self._initial_threads or not t.daemon:
                    continue
                if t.name.startswith(("l3-", "agy-sidecar-heartbeat")):
                    t.join(timeout=2.0)
        finally:
            pcm.clear_emergency_stop()
            launcher._clear_start_menu_cache()
            vision.close_vision_http_client()
            with l3_backends._workspace_guard_lock:
                l3_backends._workspace_active_tasks.clear()
                l3_backends._workspace_queues.clear()
                l3_backends._cancelled_task_ids.clear()
            for p in reversed(self._patches):
                p.stop()
            with l3_backends._dotenv_lock:
                l3_backends._dotenv_cache_key = None
                l3_backends._dotenv_cache_map = {}
            self._tmp.cleanup()

    # ==================================================================
    # E-1: Watchdog resilience
    # ==================================================================
    def test_e1_watchdog_defaults_3x5s_and_circuit_breaker_trips_and_recovers(self):
        # 1. Defaults: 5s probe timeout, 3 consecutive failures, 3 restarts in 10 minutes (600s)
        sig = inspect.signature(wd.probe_pc_mcp_health)
        self.assertEqual(sig.parameters["timeout"].default, 5.0)
        self.assertEqual(wd.DEFAULT_FAILURE_THRESHOLD, 3)
        self.assertEqual(wd.DEFAULT_CIRCUIT_BREAKER_MAX_RESTARTS, 3)
        self.assertEqual(wd.DEFAULT_CIRCUIT_BREAKER_WINDOW_SECONDS, 600.0)

        now_t = {"t": 1000.0}
        restarts = {"n": 0}
        alerts: list[str] = []
        probe_ok = {"v": False}
        state_file = self.state_dir / "wd_state.json"

        sup = wd.WatchdogSupervisor(
            probe_fn=lambda: (probe_ok["v"], "ok" if probe_ok["v"] else "connect_timeout"),
            restart_fn=lambda: (restarts.__setitem__("n", restarts["n"] + 1) or True),
            stop_check_fn=lambda: False,
            sleep_fn=lambda _s: None,
            clock_fn=lambda: now_t["t"],
            state_file=state_file,
            alert_fn=alerts.append,
        )
        self.assertEqual(sup.failure_threshold, 3)

        # Cycle 1: requires 3 consecutive failures before restarting
        self.assertEqual(sup.tick(), "degraded")  # 1/3
        self.assertEqual(sup.tick(), "degraded")  # 2/3
        self.assertEqual(restarts["n"], 0)
        self.assertEqual(sup.tick(), "restarted")  # 3/3 -> restart #1
        self.assertEqual(restarts["n"], 1)

        # Cycle 2 (t + 120s): 3 failures -> restart #2
        now_t["t"] += 120.0
        self.assertEqual(sup.tick(), "degraded")
        self.assertEqual(sup.tick(), "degraded")
        self.assertEqual(sup.tick(), "restarted")
        self.assertEqual(restarts["n"], 2)

        # Cycle 3 (t + 120s -> 240s elapsed < 600s): 3 failures -> restart #3
        now_t["t"] += 120.0
        self.assertEqual(sup.tick(), "degraded")
        self.assertEqual(sup.tick(), "degraded")
        self.assertEqual(sup.tick(), "restarted")
        self.assertEqual(restarts["n"], 3)
        self.assertFalse(sup.circuit_open)
        self.assertEqual(alerts, [])

        # Cycle 4 (still within 10 minutes): 3 failures -> circuit breaker TRIPS instead of 4th restart!
        now_t["t"] += 60.0
        self.assertEqual(sup.tick(), "degraded")
        self.assertEqual(sup.tick(), "degraded")
        self.assertEqual(sup.tick(), "circuit_open")
        self.assertTrue(sup.circuit_open)
        self.assertEqual(restarts["n"], 3, "Circuit breaker must block 4th restart within 10 min")
        self.assertEqual(len(alerts), 1)
        self.assertIn("熔断", alerts[0])

        # Alert & state persisted on disk
        st_disk = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(st_disk["status"], "circuit_open")
        self.assertTrue(st_disk["circuit_open"])
        alert_file = self.state_dir / "watchdog_alert.json"
        self.assertTrue(alert_file.is_file())
        self.assertIn("熔断", json.loads(alert_file.read_text(encoding="utf-8"))["message"])

        # Repeated subsequent failures stay circuit_open with ZERO extra restarts and ZERO alert spam
        for _ in range(10):
            now_t["t"] += 10.0
            self.assertEqual(sup.tick(), "circuit_open")
        self.assertEqual(restarts["n"], 3)
        self.assertEqual(len(alerts), 1)

        # Manual recovery resets circuit breaker cleanly
        probe_ok["v"] = True
        self.assertEqual(sup.tick(), "healthy")
        self.assertFalse(sup.circuit_open)

    def test_e1_circuit_breaker_sliding_window_allows_spaced_restarts(self):
        now_t = {"t": 0.0}
        restarts = {"n": 0}
        sup = wd.WatchdogSupervisor(
            probe_fn=lambda: (False, "down"),
            restart_fn=lambda: (restarts.__setitem__("n", restarts["n"] + 1) or True),
            stop_check_fn=lambda: False,
            sleep_fn=lambda _s: None,
            clock_fn=lambda: now_t["t"],
            failure_threshold=1,
            state_file=None,
        )
        # Restarts at t=0, t=350, t=700 (>600s window from t=0) -> never exceeds 3 within 10 min
        for step in range(5):
            now_t["t"] = step * 350.0
            self.assertEqual(sup.tick(), "restarted")
        self.assertEqual(restarts["n"], 5)
        self.assertFalse(sup.circuit_open)

    def test_e1_boot_log_appends_and_rotates_preserving_first_crash(self):
        boot_log = self.tmp_root / "pc_mcp_18900_boot.log"
        boot_log.write_text("FIRST_CRASH_TRACEBACK_LINE\n", encoding="utf-8")

        fake_proc = MagicMock()
        with patch.object(wd.subprocess, "Popen", return_value=fake_proc):
            wd.spawn_pc_mcp_server(boot_log_path=boot_log, boot_log_max_bytes=1024 * 1024)
            wd.spawn_pc_mcp_server(boot_log_path=boot_log, boot_log_max_bytes=1024 * 1024)

        content = boot_log.read_text(encoding="utf-8")
        self.assertIn("FIRST_CRASH_TRACEBACK_LINE", content, "boot.log must append, never overwrite first crash")
        self.assertGreaterEqual(content.count("watchdog spawn"), 2)

        # Oversized boot.log rotates to .1 while preserving previous content in backup
        boot_log.write_bytes(b"X" * 2048)
        with patch.object(wd.subprocess, "Popen", return_value=fake_proc):
            wd.spawn_pc_mcp_server(boot_log_path=boot_log, boot_log_max_bytes=1024, boot_log_backup_count=2)
        bak1 = boot_log.with_name(f"{boot_log.name}.1")
        self.assertTrue(bak1.is_file())
        self.assertEqual(bak1.stat().st_size, 2048)

    def test_e1_watchdog_restart_stress_never_kills_running_l3_task(self):
        """Stress test: real running L3 child process MUST survive multiple watchdog restarts & MCP startup reaps."""
        l3_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        try:
            l3_ct = l3_backends.get_process_create_time(l3_proc.pid)
            self.assertIsNotNone(l3_ct)
            l3_backends.save_active_task(
                "live_l3_survivor",
                {
                    "id": "live_l3_survivor",
                    "backend": "dsh",
                    "prompt": "long engineering task",
                    "workdir": str(self.tmp_root),
                    "status": "running",
                    "owner_pid": 999999,  # old dead 18900 MCP PID
                    "owner_create_time": 1000.0,
                    "pid": l3_proc.pid,
                    "pid_create_time": l3_ct,
                    "timeout_seconds": 300,
                    "started_ts": time.time(),
                },
            )

            fake_respawned = MagicMock()
            fake_respawned.poll.return_value = None
            with (
                patch.object(wd, "find_pc_mcp_pids", return_value=[]),
                patch.object(wd, "spawn_pc_mcp_server", return_value=fake_respawned),
                patch.object(wd, "probe_pc_mcp_health", return_value=(True, "ok")),
                patch.object(l3_backends, "cleanup_running_task_processes") as m_cleanup,
            ):
                for _ in range(3):
                    ok = wd.restart_pc_mcp_server(port=18900, health_wait_steps=2)
                    self.assertTrue(ok)
                    # Simulate newly respawned 18900 running startup reap_stale_running_tasks()
                    reaped = l3_backends.reap_stale_running_tasks()
                    self.assertEqual(reaped, 0)
                    self.assertIsNone(l3_proc.poll(), "Running L3 task was killed during watchdog restart!")
                m_cleanup.assert_not_called()

            rec = json.loads((self.active / "live_l3_survivor.json").read_text(encoding="utf-8"))
            self.assertEqual(rec["status"], "running")
        finally:
            if l3_proc.poll() is None:
                l3_proc.kill()
                l3_proc.wait(timeout=3.0)

    # ==================================================================
    # E-2: L3 per-workspace concurrency guard, queue mode & cancel
    # ==================================================================
    def test_e2_workspace_mutex_blocks_concurrent_same_ws_and_allows_different_ws(self):
        ws_a = self.tmp_root / "workspace_a"
        ws_b = self.tmp_root / "workspace_b"
        ws_a.mkdir()
        ws_b.mkdir()

        gate_a = threading.Event()
        entered_a = threading.Event()
        active_in_a = {"cur": 0, "max": 0}
        cnt_lock = threading.Lock()

        def _fake_run(cmd, **kwargs):
            cwd = Path(kwargs["cwd"]).resolve()
            if cwd == ws_a.resolve():
                with cnt_lock:
                    active_in_a["cur"] += 1
                    active_in_a["max"] = max(active_in_a["max"], active_in_a["cur"])
                entered_a.set()
                gate_a.wait(timeout=5.0)
                with cnt_lock:
                    active_in_a["cur"] -= 1
            return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

        dsh = DshBackend()
        wb = WorkBuddyBackend()
        agy = AntigravityBackend()

        cfg_a = {"backend": "dsh", "workdir": str(ws_a), "dsh_cmd": "dsh", "timeout_seconds": 60, "queue_mode": False}
        # Different casing / trailing slash referencing the SAME workspace A
        cfg_a_alias = {
            "backend": "workbuddy",
            "workdir": str(ws_a).replace("\\", "/") + "/",
            "workbuddy_cmd": "codebuddy",
            "timeout_seconds": 60,
            "queue_mode": False,
        }
        cfg_b = {"backend": "dsh", "workdir": str(ws_b), "dsh_cmd": "dsh", "timeout_seconds": 60, "queue_mode": False}

        with (
            patch("l3_backends.dsh.run_subprocess_with_tree_kill", side_effect=_fake_run),
            patch("l3_backends.workbuddy.run_subprocess_with_tree_kill", side_effect=_fake_run),
        ):
            try:
                res1 = dsh.dispatch("ws_a_1", "重构模块 A", False, cfg_a)
                self.assertIn("已成功派单", res1)
                self.assertTrue(entered_a.wait(timeout=3.0))

                # Concurrent dispatch on SAME workspace (DSH, WorkBuddy alias path, Antigravity) must all be rejected
                res_dup_dsh = dsh.dispatch("ws_a_2", "并发任务 2", False, cfg_a)
                self.assertIn("已有任务进行中，可追加指令或取消", res_dup_dsh)

                res_dup_wb = wb.dispatch("ws_a_3", "并发任务 3", False, cfg_a_alias)
                self.assertIn("已有任务进行中，可追加指令或取消", res_dup_wb)

                res_dup_agy = agy.dispatch("ws_a_4", "并发任务 4", False, cfg_a)
                self.assertIn("已有任务进行中，可追加指令或取消", res_dup_agy)

                # Different workspace B runs concurrently without blocking!
                res_b = dsh.dispatch("ws_b_1", "处理工作区 B", False, cfg_b)
                self.assertIn("已成功派单", res_b)
                self.assertEqual(l3_backends.wait_for_worker_threads(("ws_b_1",), timeout=3.0), [])
                self.assertEqual(
                    json.loads((self.active / "ws_b_1.json").read_text(encoding="utf-8"))["status"],
                    "completed",
                )
            finally:
                gate_a.set()
                l3_backends.wait_for_worker_threads(timeout=5.0)

        self.assertEqual(active_in_a["max"], 1, "Same workspace ran >1 concurrent L3 tasks!")

    def test_e2_workspace_queue_mode_serializes_fifo_and_cancel_unblocks(self):
        ws = self.tmp_root / "ws_queue"
        ws.mkdir()

        order: list[str] = []
        conc = {"cur": 0, "max": 0}
        mu = threading.Lock()
        gate1 = threading.Event()
        started1 = threading.Event()

        def _fake_run(cmd, **kwargs):
            prompt = cmd[-1]
            with mu:
                conc["cur"] += 1
                conc["max"] = max(conc["max"], conc["cur"])
                order.append(f"start:{prompt}")
            if prompt == "job-1":
                started1.set()
                gate1.wait(timeout=5.0)
            else:
                time.sleep(0.03)
            with mu:
                order.append(f"end:{prompt}")
                conc["cur"] -= 1
            return subprocess.CompletedProcess(cmd, 0, stdout=f"ok:{prompt}", stderr="")

        dsh = DshBackend()
        cfg_q = {"backend": "dsh", "workdir": str(ws), "dsh_cmd": "dsh", "timeout_seconds": 60, "queue_mode": True}

        with patch("l3_backends.dsh.run_subprocess_with_tree_kill", side_effect=_fake_run):
            try:
                r1 = dsh.dispatch("q_job_1", "job-1", False, cfg_q)
                self.assertIn("已成功派单", r1)
                self.assertTrue(started1.wait(timeout=3.0))

                r2 = dsh.dispatch("q_job_2", "job-2", False, cfg_q)
                r3 = dsh.dispatch("q_job_3", "job-3", False, cfg_q)
                self.assertIn("排队队列", r2)
                self.assertIn("第 1 位", r2)
                self.assertIn("第 2 位", r3)
                self.assertEqual(json.loads((self.active / "q_job_2.json").read_text(encoding="utf-8"))["status"], "queued")
            finally:
                gate1.set()

            # Wait for all queued jobs to drain
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                l3_backends.wait_for_worker_threads(timeout=1.0)
                snap = l3_backends.get_workspace_queue_snapshot(ws)
                if snap["active_task_id"] is None and not snap["queued_task_ids"]:
                    break
                time.sleep(0.05)

        self.assertEqual(conc["max"], 1, f"Queue mode overlapped tasks! order={order}")
        self.assertEqual(order, ["start:job-1", "end:job-1", "start:job-2", "end:job-2", "start:job-3", "end:job-3"])

        # Test cancel via pc_agent_task(action="cancel") kills real subprocess and frees workspace
        self.mode_file.write_text(json.dumps({"mode": "full"}), encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        try:
            ct = l3_backends.get_process_create_time(proc.pid)
            l3_backends.save_active_task(
                "cancel_target",
                {
                    "id": "cancel_target",
                    "backend": "dsh",
                    "prompt": "hang task",
                    "workdir": str(ws),
                    "status": "running",
                    "owner_pid": os.getpid(),
                    "pid": proc.pid,
                    "pid_create_time": ct,
                    "timeout_seconds": 300,
                    "started_ts": time.time(),
                },
            )
            with patch.dict(os.environ, {"XIAOZHI_L3_WORKDIR": str(ws)}):
                msg = pcm.pc_agent_task(action="cancel")
            self.assertIn("已取消", msg)
            proc.wait(timeout=4.0)
            self.assertIsNotNone(proc.poll())
            self.assertIsNone(l3_backends.find_busy_workspace_task(ws))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3.0)

    # ==================================================================
    # E-3: Vision lock release, staleness hash check, httpx reuse, mark_id
    # ==================================================================
    def test_e3_gui_lock_released_during_vlm_inference(self):
        self.mode_file.write_text(json.dumps({"mode": "full"}), encoding="utf-8")
        vlm_entered = threading.Event()
        vlm_release = threading.Event()
        hotkey_done = threading.Event()

        static_img = Image.new("RGB", (400, 300), color=(40, 80, 120))

        def _slow_vlm(prompt, img, json_mode=False):
            if json_mode:
                vlm_entered.set()
                vlm_release.wait(timeout=5.0)
                return json.dumps(
                    {"found": True, "mark_id": 0, "label": "确定", "x_min": 200, "y_min": 200, "x_max": 600, "y_max": 600}
                )
            return "屏幕上是一个蓝色测试窗口"

        with (
            patch.object(pcm, "_collect_uia_elements", return_value=[]),
            patch.object(pcm.ImageGrab, "grab", return_value=static_img),
            patch.object(pcm, "_call_vision_llm", side_effect=_slow_vlm),
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm, "_get_window_info_at_point", return_value=_BENIGN_FG),
            patch.object(pcm.pyautogui, "moveTo"),
            patch.object(pcm.pyautogui, "click"),
            patch.object(pcm.pyautogui, "hotkey"),
            patch.object(pcm.time, "sleep"),
        ):
            click_res: list[str] = []
            t_click = threading.Thread(
                target=lambda: click_res.append(pcm.pc_ui_action(action="click", element_name="确定按钮"))
            )
            t_click.start()
            self.assertTrue(vlm_entered.wait(timeout=3.0), "VLM call did not start")

            # While click's VLM call is blocked, BOTH hotkey and pc_look_at_screen MUST acquire _gui_lock immediately!
            other_res: list[str] = []

            def _run_concurrent_gui():
                other_res.append(pcm.pc_ui_action(action="hotkey", hotkey="ctrl+c"))
                other_res.append(pcm.pc_look_at_screen("看看屏幕"))
                hotkey_done.set()

            t_other = threading.Thread(target=_run_concurrent_gui)
            t_other.start()
            acquired_while_vlm_blocked = hotkey_done.wait(timeout=2.0)
            vlm_release.set()
            t_click.join(timeout=4.0)
            t_other.join(timeout=4.0)

        self.assertTrue(acquired_while_vlm_blocked, "_gui_lock remained held during VLM inference!")
        self.assertEqual(len(other_res), 2)
        self.assertIn("已按下快捷键", other_res[0])
        self.assertIn("屏幕画面分析结果", other_res[1])
        self.assertIn("已精准点击", click_res[0])

    def test_e3_preclick_region_hash_detects_staleness_and_relocates_or_aborts(self):
        self.mode_file.write_text(json.dumps({"mode": "full"}), encoding="utf-8")

        def _make_screen(btn_box, btn_color, bg_color=(20, 20, 20)):
            img = Image.new("RGB", (400, 300), color=bg_color)
            d = ImageDraw.Draw(img)
            d.rectangle(btn_box, fill=btn_color)
            return img

        # Case 1: Screen changes between locate #1 and pre-click grab -> re-locates -> stabilizes -> clicks NEW coords!
        img_before = _make_screen((60, 60, 140, 120), (0, 200, 80))
        img_changed = _make_screen((240, 160, 340, 240), (220, 40, 40), bg_color=(240, 240, 240))
        grabs = iter([img_before, img_changed, img_changed, img_changed])
        locate_attempts = {"n": 0}
        orig_locate = vision._smart_locate_target

        def _spy_locate(desc):
            locate_attempts["n"] += 1
            return orig_locate(desc)

        def _vlm_two_stage(prompt, img, json_mode=False):
            if locate_attempts["n"] == 1:
                # Locate #1: points to (100, 90) on img_before
                return json.dumps(
                    {"found": True, "mark_id": 0, "label": "提交", "x_min": 150, "y_min": 200, "x_max": 350, "y_max": 400}
                )
            # Locate #2: points to (290, 200) on img_changed
            return json.dumps(
                {"found": True, "mark_id": 0, "label": "提交", "x_min": 600, "y_min": 533, "x_max": 850, "y_max": 800}
            )

        clicked_coords: list[tuple[int, int]] = []
        with (
            patch.object(pcm, "_smart_locate_target", side_effect=_spy_locate),
            patch.object(pcm, "_collect_uia_elements", return_value=[]),
            patch.object(pcm.ImageGrab, "grab", side_effect=lambda: next(grabs)),
            patch.object(pcm, "_call_vision_llm", side_effect=_vlm_two_stage),
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm, "_get_window_info_at_point", return_value=_BENIGN_FG),
            patch.object(pcm.pyautogui, "moveTo"),
            patch.object(pcm.pyautogui, "click", side_effect=lambda cx, cy: clicked_coords.append((cx, cy))),
            patch.object(pcm.time, "sleep"),
        ):
            res = pcm.pc_ui_action(action="click", element_name="提交按钮")

        self.assertIn("已精准点击", res)
        self.assertEqual(locate_attempts["n"], 2, "Must re-locate when target region hash changes before click")
        self.assertEqual(len(clicked_coords), 1)
        self.assertGreater(clicked_coords[0][0], 220, f"Clicked stale coordinates instead of re-located: {clicked_coords}")

        # Case 2: Screen changes on EVERY pre-click check -> aborts safely with ZERO clicks & audit log
        tick_color = {"i": 0}

        def _always_changing_grab():
            tick_color["i"] += 1
            c = (tick_color["i"] * 45) % 250
            return Image.new("RGB", (400, 300), color=(c, (c + 80) % 250, (c + 160) % 250))

        with (
            patch.object(pcm, "_collect_uia_elements", return_value=[]),
            patch.object(pcm.ImageGrab, "grab", side_effect=_always_changing_grab),
            patch.object(
                pcm,
                "_call_vision_llm",
                return_value=json.dumps(
                    {"found": True, "mark_id": 0, "label": "按钮", "x_min": 200, "y_min": 200, "x_max": 600, "y_max": 600}
                ),
            ),
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm, "_get_window_info_at_point", return_value=_BENIGN_FG),
            patch.object(pcm.pyautogui, "click") as m_click,
            patch.object(pcm.time, "sleep"),
        ):
            abort_res = pcm.pc_ui_action(action="click", element_name="不断变化的按钮")
            self.assertIn("中止点击", abort_res)
            m_click.assert_not_called()

        audit_lines = [json.loads(ln) for ln in self.audit_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertTrue(any(r.get("reason") == "target_region_changed" for r in audit_lines))

    def test_e3_persistent_httpx_client_and_fault_tolerant_mark_id(self):
        # 1. Persistent httpx.Client reused across multiple _call_vision_llm invocations
        vision.close_vision_http_client()
        c1 = vision._get_vision_http_client()
        c2 = vision._get_vision_http_client()
        self.assertIs(c1, c2)

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        img = Image.new("RGB", (64, 64), color=(10, 20, 30))

        with (
            patch.object(vision, "_get_env_key", side_effect=lambda k: "sk-test-key" if k == "DEEPSEEK_API_KEY" else ""),
            patch.object(vision, "_prefer_gemini_vision", return_value=False),
            patch.object(c1, "post", return_value=fake_resp) as m_post,
            patch.object(vision.httpx, "post", side_effect=AssertionError("Must use persistent httpx.Client, not httpx.post")),
        ):
            self.assertEqual(vision._call_vision_llm("hi", img), "ok")
            self.assertEqual(vision._call_vision_llm("hi2", img), "ok")
            self.assertEqual(m_post.call_count, 2)

        # 2. Fault-tolerant mark_id parsing across malformed & edge-case inputs
        cases = [
            (3, 10, 3),
            (" 3 ", 10, 3),
            ("[3]", 10, 3),
            ("【3】", 10, 3),
            ("(3)", 10, 3),
            ("#3", 10, 3),
            ("SoM#3", 10, 3),
            ("mark_id: 3", 10, 3),
            ("编号3", 10, 3),
            ("3.0", 10, 3),
            (3.0, 10, 3),
            (3.7, 10, 0),
            (float("nan"), 10, 0),
            (True, 10, 0),
            (False, 10, 0),
            (None, 10, 0),
            ("", 10, 0),
            ("none", 10, 0),
            ("N/A", 10, 0),
            (-1, 10, 0),
            ("-2", 10, 0),
            (0, 10, 0),
            (999, 10, 0),
            ("[999]", 10, 0),
            ([3], 10, 0),
            ({"id": 3}, 10, 0),
        ]
        for raw, max_id, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(vision._parse_mark_id(raw, max_id=max_id), expected)

        # 3. End-to-end _smart_locate_target with bracketed mark_id AND out-of-range mark_id fallback to bbox
        fake_elems = [
            {"id": 1, "type": "Button", "name": "取消", "left": 10, "top": 10, "right": 90, "bottom": 50, "cx": 50, "cy": 30},
            {"id": 2, "type": "Button", "name": "保存", "left": 120, "top": 10, "right": 200, "bottom": 50, "cx": 160, "cy": 30},
        ]
        base_img = Image.new("RGB", (400, 300), color=(30, 30, 30))
        with (
            patch.object(pcm, "_collect_uia_elements", return_value=fake_elems),
            patch.object(pcm.ImageGrab, "grab", return_value=base_img),
            patch.object(
                pcm,
                "_call_vision_llm",
                return_value=json.dumps({"found": True, "mark_id": " [2] ", "label": "保存"}),
            ),
        ):
            loc = vision._smart_locate_target("右上角按钮")
            self.assertIsNotNone(loc)
            self.assertEqual((loc[0], loc[1]), (160, 30))
            self.assertTrue(loc.region_hash)

        with (
            patch.object(pcm, "_collect_uia_elements", return_value=fake_elems),
            patch.object(pcm.ImageGrab, "grab", return_value=base_img),
            patch.object(
                pcm,
                "_call_vision_llm",
                return_value=json.dumps(
                    {"found": True, "mark_id": "[999]", "label": "封面", "x_min": 200, "y_min": 200, "x_max": 800, "y_max": 800}
                ),
            ),
        ):
            loc_fb = vision._smart_locate_target("第一个封面")
            self.assertIsNotNone(loc_fb)
            self.assertEqual((loc_fb[0], loc_fb[1]), (200, 150))

    # ==================================================================
    # E-4: Sidecar heartbeat thread & leading-dash prompt safety
    # ==================================================================
    def test_e4_sidecar_heartbeat_thread_keeps_ticking_during_blocking_dispatch(self):
        stop_ev = threading.Event()
        takeover_ev = threading.Event()
        th = agy_sidecar_worker.start_heartbeat_thread(stop_ev, takeover_ev, interval=0.06)
        try:
            self.assertTrue(th.daemon)
            ts1 = json.loads(self.hb.read_text(encoding="utf-8"))["ts"]

            # Simulate a blocking _process_task (e.g. 0.25s inside proc.communicate)
            tf = self.pending / "slow_task.json"
            tf.write_text(json.dumps({"id": "slow_task", "prompt": "慢任务"}), encoding="utf-8")
            fake_proc = MagicMock()
            fake_proc.returncode = 0

            def _slow_comm(timeout=30):
                time.sleep(0.25)
                return ('{"conversationId": "11089b72-95cd-465c-8ba2-e6dcd5d03bfe"}', "")

            fake_proc.communicate.side_effect = _slow_comm
            with (
                patch.object(agy_sidecar_worker, "_build_agentapi_prefix", return_value=["agentapi.exe"]),
                patch.object(agy_sidecar_worker.subprocess, "Popen", return_value=fake_proc),
            ):
                self.assertTrue(agy_sidecar_worker._process_task(tf))

            ts2 = json.loads(self.hb.read_text(encoding="utf-8"))["ts"]
            self.assertGreater(ts2, ts1, "Heartbeat thread stalled during blocking dispatch!")
            self.assertFalse(takeover_ev.is_set())

            # Simulate newer worker takeover -> heartbeat thread sets takeover_event and stops
            agy_sidecar_worker._atomic_write_json(
                self.hb,
                {"ts": time.time() + 10.0, "pid": os.getpid() + 99999, "version": 3},
                indent=None,
            )
            self.assertTrue(takeover_ev.wait(timeout=1.5))
        finally:
            stop_ev.set()
            th.join(timeout=2.0)

    def test_e4_leading_dash_prompt_safety_without_double_dash(self):
        captured_cmds: list[list[str]] = []
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = ('{"conversationId": "11089b72-95cd-465c-8ba2-e6dcd5d03bfe"}', "")

        def _fake_popen(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            return fake_proc

        # 1. send-message with leading-dash prompts (-h, --unknown, - 列表)
        conv_uuid = "11089b72-95cd-465c-8ba2-e6dcd5d03bfe"
        for idx, dash_prompt in enumerate(["--unknown-flag", "-h", "- 第一条待办"]):
            tf = self.pending / f"dash_send_{idx}.json"
            tf.write_text(
                json.dumps({"id": f"dash_send_{idx}", "prompt": dash_prompt, "conversation_id": conv_uuid}),
                encoding="utf-8",
            )
            with (
                patch.object(agy_sidecar_worker, "_build_agentapi_prefix", return_value=["language_server.exe", "agentapi"]),
                patch.object(agy_sidecar_worker.subprocess, "Popen", side_effect=_fake_popen),
            ):
                self.assertTrue(agy_sidecar_worker._process_task(tf))
            cmd = captured_cmds[-1]
            self.assertNotIn("--", cmd, "AGENTS.md §2 forbids passing '--' before <prompt>")
            self.assertEqual(cmd[:3], ["language_server.exe", "agentapi", "send-message"])
            self.assertFalse(cmd[-1].startswith("-"), f"Leading '-' reached CLI arg unguarded: {cmd[-1]!r}")
            self.assertIn(dash_prompt, cmd[-1])

        # 2. new-conversation with leading-dash prompt
        tf_new = self.pending / "dash_new.json"
        tf_new.write_text(json.dumps({"id": "dash_new", "prompt": "--model=evil -h"}), encoding="utf-8")
        with (
            patch.object(agy_sidecar_worker, "_build_agentapi_prefix", return_value=["language_server.exe", "agentapi"]),
            patch.object(agy_sidecar_worker.subprocess, "Popen", side_effect=_fake_popen),
        ):
            self.assertTrue(agy_sidecar_worker._process_task(tf_new))
        cmd_new = captured_cmds[-1]
        self.assertNotIn("--", cmd_new)
        self.assertEqual(cmd_new[:3], ["language_server.exe", "agentapi", "new-conversation"])
        self.assertFalse(cmd_new[-1].startswith("-"))
        self.assertIn("--model=evil -h", cmd_new[-1])

        # 3. AntigravityBackend.dispatch also guards leading-dash prompts idempotently
        ab = AntigravityBackend()
        ab.dispatch("agy_dash_1", "--help 帮我检查", False, {"workdir": str(self.tmp_root)})
        queued_obj = json.loads((self.pending / "agy_dash_1.json").read_text(encoding="utf-8"))
        self.assertFalse(queued_obj["prompt"].startswith("-"))
        self.assertIn("--help 帮我检查", queued_obj["prompt"])

        # 4. Live CLI flag-parser sanity check against real language_server.exe (with unreachable dummy LS env)
        ls_exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "antigravity" / "resources" / "bin" / "language_server.exe"
        if ls_exe.is_file():
            clean_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("ANTIGRAVITY")}
            clean_env["ANTIGRAVITY_LS_ADDRESS"] = "127.0.0.1:1"
            clean_env["ANTIGRAVITY_LS_CSRF_TOKEN"] = "unit-test-dummy"
            safe_p = agy_sidecar_worker._guard_leading_dash_prompt("--unknown-flag")
            r_new = subprocess.run(
                [str(ls_exe), "agentapi", "new-conversation", "--title=t", safe_p],
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotIn("flag provided but not defined", r_new.stdout + r_new.stderr)
            r_send = subprocess.run(
                [str(ls_exe), "agentapi", "send-message", "--title=t", conv_uuid, safe_p],
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotIn("flag provided but not defined", r_send.stdout + r_send.stderr)

    # ==================================================================
    # H-6: l2_audit.jsonl 5MBx3 rotation, active 50-record archive, 64KB tail
    # ==================================================================
    def test_h6_l2_audit_log_rotates_5mb_x3_backups(self):
        self.assertEqual(security.L2_AUDIT_MAX_BYTES, 5 * 1024 * 1024)
        self.assertEqual(security.L2_AUDIT_BACKUP_COUNT, 3)

        with (
            patch.object(security, "L2_AUDIT_MAX_BYTES", 600),
            patch.object(security, "L2_AUDIT_BACKUP_COUNT", 3),
        ):
            for i in range(25):
                security._write_l2_audit_log(
                    tool="pc_ui_action",
                    action="click",
                    outcome="allowed",
                    reason="ok",
                    mode="full",
                    fg_info=_BENIGN_FG,
                    target=f"target-{i:03d}-" + ("x" * 40),
                    detail=f"detail-{i:03d}",
                )

        self.assertTrue(self.audit_file.is_file())
        self.assertTrue(self.audit_file.with_name(f"{self.audit_file.name}.1").is_file())
        self.assertTrue(self.audit_file.with_name(f"{self.audit_file.name}.2").is_file())
        self.assertTrue(self.audit_file.with_name(f"{self.audit_file.name}.3").is_file())
        self.assertFalse(
            self.audit_file.with_name(f"{self.audit_file.name}.4").exists(),
            "Audit rotation must keep at most 3 backups",
        )
        # Every line across active + rotated backups must be valid JSON
        for cand in [self.audit_file, *(self.audit_file.with_name(f"{self.audit_file.name}.{k}") for k in (1, 2, 3))]:
            lines = [ln for ln in cand.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertTrue(lines)
            for ln in lines:
                self.assertIn("tool", json.loads(ln))

    def test_h6_active_tasks_keep_latest_50_and_archive_older_preserving_running(self):
        self.assertEqual(l3_backends.MAX_ACTIVE_TASK_RECORDS, 50)
        self.assertEqual(agy_sidecar_worker.MAX_ACTIVE_TASK_RECORDS, 50)

        base_ts = 1_700_000_000.0
        # Write 1 active running task (oldest timestamp!) + 60 completed tasks directly
        (self.active / "task_running_keep.json").write_text(
            json.dumps({"id": "task_running_keep", "status": "running", "started_ts": base_ts - 1000}),
            encoding="utf-8",
        )
        for i in range(60):
            fp = self.active / f"task_{i:03d}.json"
            fp.write_text(
                json.dumps({"id": f"task_{i:03d}", "status": "completed", "started_ts": base_ts + i}),
                encoding="utf-8",
            )

        archived_n = l3_backends.prune_active_tasks()
        self.assertEqual(archived_n, 11)
        remaining = {fp.stem for fp in self.active.glob("*.json")}
        archived = {fp.stem for fp in self.archive.glob("*.json")}
        self.assertEqual(len(remaining), 50)
        self.assertIn("task_running_keep", remaining, "Running tasks must never be archived")
        self.assertIn("task_059", remaining, "Newest tasks must stay in active/")
        self.assertIn("task_000", archived, "Oldest completed tasks must move to archive/")
        self.assertEqual(len(archived), 11)

        # Also verify agy_sidecar_worker._prune_active_tasks keeps <=50
        for j in range(60, 68):
            (self.active / f"task_{j:03d}.json").write_text(
                json.dumps({"id": f"task_{j:03d}", "status": "dispatched", "started_ts": base_ts + j}),
                encoding="utf-8",
            )
        archived_n2 = agy_sidecar_worker._prune_active_tasks()
        self.assertEqual(archived_n2, 8)
        self.assertEqual(len(list(self.active.glob("*.json"))), 50)

    def test_h6_antigravity_transcript_reads_only_last_64kb(self):
        self.assertEqual(TRANSCRIPT_TAIL_BYTES, 64 * 1024)
        brain_dir = self.tmp_root / "brain"
        conv_id = "11089b72-95cd-465c-8ba2-e6dcd5d03bfe"
        t_path = brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        t_path.parent.mkdir(parents=True, exist_ok=True)

        # Case 1: 250 KB transcript with old reply at start and latest reply near the end
        old_reply = json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "OLD_REPLY_SHOULD_NOT_BE_READ"})
        filler = json.dumps({"type": "TOOL_CALL", "status": "DONE", "content": "中" * 400})
        new_reply = json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "LATEST_64KB_TAIL_REPLY"})
        t_path.write_text(
            "\n".join([old_reply, *(filler for _ in range(240)), new_reply]) + "\n",
            encoding="utf-8",
        )
        self.assertGreater(t_path.stat().st_size, 200 * 1024)

        lines, truncated = _read_tail_lines(t_path, max_bytes=TRANSCRIPT_TAIL_BYTES)
        self.assertTrue(truncated)
        self.assertNotIn("OLD_REPLY_SHOULD_NOT_BE_READ", "\n".join(lines))
        self.assertIn("LATEST_64KB_TAIL_REPLY", "\n".join(lines))

        with patch("l3_backends.antigravity._get_brain_dir", return_value=brain_dir):
            summary = _summarize_transcript(conv_id)
        self.assertIn("LATEST_64KB_TAIL_REPLY", summary)

        # Case 2: Old reply is BEFORE the last 64 KB; last 64 KB has ONLY tool steps -> must NOT scan past 64 KB
        t_path.write_text(
            "\n".join([old_reply, *(filler for _ in range(240))]) + "\n",
            encoding="utf-8",
        )
        with patch("l3_backends.antigravity._get_brain_dir", return_value=brain_dir):
            summary2 = _summarize_transcript(conv_id)
        self.assertNotIn("OLD_REPLY_SHOULD_NOT_BE_READ", summary2)
        self.assertIn("TOOL_CALL", summary2)

    # ==================================================================
    # H-7: L1 Start Menu shortcut blacklist & mtime-invalidated cache
    # ==================================================================
    def test_h7_start_menu_blacklist_blocks_sensitive_tools_and_allows_benign(self):
        sm_root = self.tmp_root / "StartMenu" / "Programs"
        files_to_create = [
            "微信.lnk",
            "腾讯软件/微信/卸载微信.lnk",
            "腾讯软件/微信/卸载清除.lnk",
            "网易云音乐/卸载网易云音乐.lnk",
            "网易云音乐/网易云音乐.lnk",
            "PotPlayer/Uninstall PotPlayer-64 bit.lnk",
            "PotPlayer/PotPlayer 64 bit.lnk",
            "PowerPoint.lnk",
            "Obsidian.lnk",
            "Accessories/Notepad.lnk",
            "Windows PowerShell/Windows PowerShell.lnk",
            "Windows PowerShell/Windows PowerShell ISE.lnk",
            "System Tools/Command Prompt.lnk",
            "System Tools/Run.lnk",
            "System Tools/Task Manager.lnk",
            "Administrative Tools/Registry Editor.lnk",
            "Administrative Tools/services.lnk",
            "Administrative Tools/System Configuration.lnk",
            "regedit.lnk",
            "cmd.lnk",
            "Windows Terminal.lnk",
            "WezTerm.lnk",
            "Git/Git Bash.lnk",
            "Git/Git CMD.lnk",
            "WSL.lnk",
        ]
        for rel in files_to_create:
            p = sm_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"LNK")

        blocked_queries = [
            "regedit",
            "Registry Editor",
            "PowerShell",
            "Windows PowerShell",
            "cmd",
            "Command Prompt",
            "Windows Terminal",
            "WezTerm",
            "Git Bash",
            "Git CMD",
            "WSL",
            "Uninstall",
            "卸载",
            "卸载微信",
            "卸载网易云音乐",
            "services",
            "Run",
            "Task Manager",
        ]
        for q in blocked_queries:
            with self.subTest(blocked_query=q):
                found = launcher._find_start_menu_shortcut(q, start_dirs=[sm_root])
                self.assertIsNone(found, f"Sensitive/uninstaller shortcut should be blocked for {q!r}, got {found}")

        # Benign apps must still resolve accurately (and never accidentally match their uninstaller!)
        self.assertEqual(launcher._find_start_menu_shortcut("微信", start_dirs=[sm_root]).stem, "微信")
        self.assertEqual(launcher._find_start_menu_shortcut("网易云音乐", start_dirs=[sm_root]).stem, "网易云音乐")
        self.assertEqual(launcher._find_start_menu_shortcut("PotPlayer", start_dirs=[sm_root]).stem, "PotPlayer 64 bit")
        self.assertEqual(launcher._find_start_menu_shortcut("PowerPoint", start_dirs=[sm_root]).stem, "PowerPoint")
        self.assertEqual(launcher._find_start_menu_shortcut("Obsidian", start_dirs=[sm_root]).stem, "Obsidian")

    def test_h7_start_menu_cache_hits_and_invalidates_on_dir_mtime_change(self):
        sm_root = self.tmp_root / "StartMenuCache"
        sub_dir = sm_root / "MySuite"
        sub_dir.mkdir(parents=True)
        (sm_root / "AppOne.lnk").write_bytes(b"LNK")
        (sub_dir / "SubAppOne.lnk").write_bytes(b"LNK")

        orig_walk = os.walk
        walk_calls = {"n": 0}

        def _counting_walk(*args, **kwargs):
            walk_calls["n"] += 1
            return orig_walk(*args, **kwargs)

        with patch.object(launcher.os, "walk", side_effect=_counting_walk):
            # 1. First query populates cache (1 walk)
            self.assertEqual(launcher._find_start_menu_shortcut("AppOne", start_dirs=[sm_root]).stem, "AppOne")
            self.assertEqual(walk_calls["n"], 1)

            # 2. Repeated queries hit cache (ZERO additional walks!)
            self.assertEqual(launcher._find_start_menu_shortcut("SubAppOne", start_dirs=[sm_root]).stem, "SubAppOne")
            self.assertIsNone(launcher._find_start_menu_shortcut("NonExistent", start_dirs=[sm_root]))
            self.assertEqual(walk_calls["n"], 1, "Expected Start Menu shortcut index cache hit!")

            # 3. Adding a new shortcut in root dir updates root mtime -> cache invalidates!
            time.sleep(0.02)
            (sm_root / "AppTwo.lnk").write_bytes(b"LNK")
            # Bump root mtime explicitly in case filesystem timer resolution is coarse
            st_root = sm_root.stat()
            os.utime(sm_root, ns=(st_root.st_atime_ns, st_root.st_mtime_ns + 1_000_000_000))
            self.assertEqual(launcher._find_start_menu_shortcut("AppTwo", start_dirs=[sm_root]).stem, "AppTwo")
            self.assertEqual(walk_calls["n"], 2)

            # 4. Adding a new shortcut inside a subdirectory updates subdir mtime -> cache invalidates!
            (sub_dir / "SubAppTwo.lnk").write_bytes(b"LNK")
            st_sub = sub_dir.stat()
            os.utime(sub_dir, ns=(st_sub.st_atime_ns, st_sub.st_mtime_ns + 1_000_000_000))
            self.assertEqual(launcher._find_start_menu_shortcut("SubAppTwo", start_dirs=[sm_root]).stem, "SubAppTwo")
            self.assertEqual(walk_calls["n"], 3)

    def test_p0_3_and_p1_4_builtin_apps_removed_lnk_target_and_unicode_boundary(self):
        """P0-3 & P1-4: _BUILTIN_APPS excludes cmd/wt/taskmgr; Unicode word boundary blocks 中文+cmd/underscore; IShellLinkW blocks renamed .lnk targets."""
        import ctypes

        # 1. P0-3: _BUILTIN_APPS must NOT contain cmd.exe, wt.exe, taskmgr.exe or their Chinese aliases
        for bad_key in ("命令行", "终端", "任务管理器"):
            self.assertNotIn(bad_key, launcher._BUILTIN_APPS)
        builtin_vals = {str(v).lower() for v in launcher._BUILTIN_APPS.values()}
        for bad_exe in ("cmd.exe", "wt.exe", "taskmgr.exe"):
            self.assertNotIn(bad_exe, builtin_vals)

        # Calling pc_open_app_or_web in safe mode with 命令行 / 终端 / 任务管理器 MUST NOT invoke _shell_open
        self.mode_file.write_text(json.dumps({"mode": "safe"}), encoding="utf-8")
        with (
            patch.object(pcm, "_shell_open") as m_open,
            patch.object(pcm, "_find_start_menu_shortcut", return_value=None),
        ):
            for q in ("命令行", "终端", "任务管理器"):
                res = pcm.pc_open_app_or_web(q)
                self.assertIn("未在开始菜单", res)
            m_open.assert_not_called()

        # 2. P1-4: Unicode/underscore boundary bypasses MUST be blocked while benign words containing 'sh' stay allowed
        sm_unicode = self.tmp_root / "StartMenuUnicode"
        sm_unicode.mkdir(parents=True)
        for fn in (
            "打开cmd.lnk",
            "启动wsl.lnk",
            "my_mshta.lnk",
            "运行_powershell.lnk",
            "工具_bash.lnk",
            "Photoshop.lnk",
            "Clash.lnk",
            "PowerPoint.lnk",
        ):
            (sm_unicode / fn).write_bytes(b"LNK")

        for bad_stem in ("打开cmd", "启动wsl", "my_mshta", "运行_powershell", "工具_bash"):
            self.assertIsNone(
                launcher._find_start_menu_shortcut(bad_stem, start_dirs=[sm_unicode]),
                f"Unicode/underscore bypass should be blocked: {bad_stem}",
            )
        for good_stem in ("Photoshop", "Clash", "PowerPoint"):
            found = launcher._find_start_menu_shortcut(good_stem, start_dirs=[sm_unicode])
            self.assertIsNotNone(found, f"Benign app should remain allowed: {good_stem}")
            self.assertEqual(found.stem, good_stem)

        # 3. P1-4: Real Windows .lnk target & arguments inspection via IShellLinkW
        if os.name == "nt":
            def _create_real_lnk(lnk_path: Path, target_path: str, arguments: str = "") -> None:
                ole32 = ctypes.windll.ole32
                clsid_sl, iid_sl, iid_pf = launcher._get_shell_link_guids()
                psl = ctypes.c_void_p()
                ppf = ctypes.c_void_p()
                with launcher._com_mta_scope():
                    hr = ole32.CoCreateInstance(
                        ctypes.byref(clsid_sl), None, 1, ctypes.byref(iid_sl), ctypes.byref(psl)
                    )
                    self.assertEqual(hr, 0)
                    try:
                        vtbl_sl = ctypes.cast(psl, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                        set_path = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl_sl[20])
                        set_args = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl_sl[11])
                        self.assertEqual(set_path(psl, target_path), 0)
                        if arguments:
                            self.assertEqual(set_args(psl, arguments), 0)
                        qi = ctypes.WINFUNCTYPE(
                            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(launcher._GUID), ctypes.POINTER(ctypes.c_void_p)
                        )(vtbl_sl[0])
                        self.assertEqual(qi(psl, ctypes.byref(iid_pf), ctypes.byref(ppf)), 0)
                        vtbl_pf = ctypes.cast(ppf, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                        pf_save = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)(
                            vtbl_pf[6]
                        )
                        self.assertEqual(pf_save(ppf, str(lnk_path), 1), 0)
                    finally:
                        if ppf.value:
                            vtbl_pf = ctypes.cast(ppf, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl_pf[2])(ppf)
                        if psl.value:
                            vtbl_sl = ctypes.cast(psl, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl_sl[2])(psl)

            sm_real = self.tmp_root / "StartMenuRealLnk"
            sm_real.mkdir(parents=True)
            sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
            _create_real_lnk(sm_real / "正常笔记.lnk", str(sys32 / "cmd.exe"))
            _create_real_lnk(sm_real / "日常助手.lnk", "%ComSpec%", "/k echo hi")
            _create_real_lnk(sm_real / "快捷面板.lnk", str(sys32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"))
            _create_real_lnk(sm_real / "文档预览.lnk", str(sys32 / "notepad.exe"), "payload.ps1")
            _create_real_lnk(sm_real / "纯良记事本.lnk", str(sys32 / "notepad.exe"))

            for disguised in ("正常笔记", "日常助手", "快捷面板", "文档预览"):
                self.assertIsNone(
                    launcher._find_start_menu_shortcut(disguised, start_dirs=[sm_real]),
                    f"Disguised .lnk targeting sensitive binary/script must be blocked: {disguised}",
                )
            benign = launcher._find_start_menu_shortcut("纯良记事本", start_dirs=[sm_real])
            self.assertIsNotNone(benign)
            self.assertEqual(benign.stem, "纯良记事本")

            # Verify COM IShellLinkW resolution works cleanly on a fresh worker thread (Release BEFORE CoUninitialize)
            worker_res: list = []
            worker_err: list = []

            def _resolve_on_fresh_thread():
                try:
                    worker_res.append(launcher._resolve_lnk_targets(sm_real / "纯良记事本.lnk"))
                except Exception as exc:
                    worker_err.append(exc)

            th = threading.Thread(target=_resolve_on_fresh_thread)
            th.start()
            th.join(timeout=5.0)
            self.assertEqual(worker_err, [])
            self.assertTrue(worker_res and worker_res[0] is not None)

        # 4. P1-4 red-team regressions: blocked exes/scripts/%ComSpec% in Arguments, extensionless/trailing-dot targets,
        #    Shell-CLSID Control Panel, & 8.3 short paths MUST be blocked
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\explorer.exe", "windowsterminal.exe"))
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\explorer.exe", "msiexec.exe /i foo.msi"))
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\explorer.exe", "regsvr32 /s evil.dll"))
        self.assertTrue(
            launcher._is_blocked_shortcut_target(r"C:\Windows\explorer.exe", r"forfiles /p c:\windows\system32 /c calc")
        )
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\System32\conhost", ""))
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\System32\msiexec", "/x {GUID}"))
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\System32\regsvr32", ""))
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\System32\windowsterminal.exe.", ""))
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\explorer.exe", "evil.wsf"))
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\explorer.exe", "evil.jse"))
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\explorer.exe", "evil.cpl"))
        self.assertTrue(launcher._is_blocked_shortcut_target(r"C:\Windows\explorer.exe", "%ComSpec%"))
        self.assertTrue(
            launcher._is_blocked_shortcut_target(r"C:\Windows\System32\WindowsPowerShell\v1.0\POWERS~1.EXE", "")
        )
        self.assertFalse(launcher._is_blocked_shortcut_target(r"C:\Windows\System32\notepad.exe", "notes.txt"))
        sm_cp = self.tmp_root / "StartMenuControlPanel"
        sm_cp.mkdir(parents=True)
        (sm_cp / "Control Panel.lnk").write_bytes(b"LNK")
        (sm_cp / "控制面板.lnk").write_bytes(b"LNK")
        (sm_cp / "Virtual IO Control Panel.lnk").write_bytes(b"LNK")
        (sm_cp / "File Explorer.lnk").write_bytes(b"LNK")
        self.assertTrue(launcher._is_blocked_shortcut(sm_cp / "Control Panel.lnk", root_dir=sm_cp))
        self.assertTrue(launcher._is_blocked_shortcut(sm_cp / "控制面板.lnk", root_dir=sm_cp))
        self.assertFalse(launcher._is_blocked_shortcut(sm_cp / "Virtual IO Control Panel.lnk", root_dir=sm_cp))
        self.assertFalse(launcher._is_blocked_shortcut(sm_cp / "File Explorer.lnk", root_dir=sm_cp))

    def test_p1_3_dsh_and_workbuddy_guard_leading_dash_prompt(self):
        """P1-3: Shared common.guard_leading_dash_prompt protects DSH, WorkBuddy, Antigravity, and Sidecar against leading '-' prompts."""
        import common as pc_common
        from l3_backends.workbuddy import WorkBuddyBackend

        # 1. Shared helper in pc-mcp/common (including BOM-prefixed dashes)
        self.assertEqual(pc_common.guard_leading_dash_prompt("-y --danger"), "[指令] -y --danger")
        self.assertEqual(pc_common.guard_leading_dash_prompt("\ufeff-y"), "[指令] -y")
        self.assertEqual(pc_common.guard_leading_dash_prompt("  --model=evil", prefix_tag="[追问]"), "[追问] --model=evil")
        self.assertEqual(pc_common.guard_leading_dash_prompt("正常任务"), "正常任务")

        # 2. DshBackend._build_command guards leading '-' prompts
        dsh_cmd = DshBackend()._build_command(["dsh"], "--dangerously-allow-all -y")
        self.assertEqual(dsh_cmd[:3], ["dsh", "--profile", "headless"])
        self.assertFalse(dsh_cmd[-1].startswith("-"))
        self.assertIn("--dangerously-allow-all -y", dsh_cmd[-1])

        # 3. WorkBuddyBackend._build_command guards leading '-' prompts for both fresh and follow-up
        wb_cmd1 = WorkBuddyBackend()._build_command(["workbuddy"], "-y --model=evil", False, False)
        p_idx1 = wb_cmd1.index("-p")
        self.assertFalse(wb_cmd1[p_idx1 + 1].startswith("-"))
        self.assertIn("-y --model=evil", wb_cmd1[p_idx1 + 1])

        wb_cmd2 = WorkBuddyBackend()._build_command(["workbuddy"], "--help", True, True)
        p_idx2 = wb_cmd2.index("-p")
        self.assertTrue(wb_cmd2[p_idx2 + 1].startswith("[追问] "))
        self.assertIn("--help", wb_cmd2[p_idx2 + 1])

    def test_p1_11_l3_file_backed_logs_queued_reap_antigravity_lock_and_spawn_cancel_race(self):
        """P1-11: File-backed child logs (no broken pipe), dead-parent queued reap, Antigravity lock, _on_spawn cancel race."""
        from l3_backends.antigravity import AntigravityBackend

        # 1. run_subprocess_with_tree_kill with log file paths captures stdout/stderr without PIPE & cleans up logs
        out_log = self.tmp_root / "task_p111.stdout.log"
        err_log = self.tmp_root / "task_p111.stderr.log"
        res = l3_backends.run_subprocess_with_tree_kill(
            [sys.executable, "-c", "import sys; print('hello_file_log'); print('err_line', file=sys.stderr)"],
            stdout_log_path=out_log,
            stderr_log_path=err_log,
            timeout=15.0,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("hello_file_log", res.stdout)
        self.assertIn("err_line", res.stderr)
        self.assertFalse(out_log.exists(), "Temp stdout log should be cleaned up after completion")
        self.assertFalse(err_log.exists(), "Temp stderr log should be cleaned up after completion")

        # 2. Abandoned `status == 'queued'` record from dead owner_pid is reaped cleanly (not permanent zombie)
        zombie_q = self.active / "z_queued_dead_owner.json"
        zombie_q.write_text(
            json.dumps(
                {
                    "id": "z_queued_dead_owner",
                    "backend": "dsh",
                    "workdir": str(self.tmp_root),
                    "status": "queued",
                    "owner_pid": 99999991,
                    "owner_create_time": 12345.0,
                    "started_ts": time.time() - 60.0,
                    "timeout_seconds": 300,
                }
            ),
            encoding="utf-8",
        )
        reaped = l3_backends.reap_stale_running_tasks()
        self.assertGreaterEqual(reaped, 1)
        z_after = json.loads(zombie_q.read_text(encoding="utf-8"))
        self.assertEqual(z_after["status"], "error")
        self.assertTrue(z_after.get("reaped_zombie"))

        # 3. register_spawned_task_pid immediately kills child and does NOT overwrite 'cancelled' back to 'running'
        rec_cancel = {
            "id": "t_cancel_race",
            "backend": "dsh",
            "workdir": str(self.tmp_root),
            "status": "running",
            "pid": None,
        }
        cancel_file = self.active / "t_cancel_race.json"
        cancel_file.write_text(json.dumps({"id": "t_cancel_race", "status": "cancelled"}), encoding="utf-8")
        l3_backends._mark_task_cancelled_id("t_cancel_race")
        killed_pids: list[int] = []
        with patch.object(l3_backends, "kill_process_tree", side_effect=lambda pid, **kw: killed_pids.append(pid) or True):
            allowed = l3_backends.register_spawned_task_pid("t_cancel_race", rec_cancel, 54321, child_ct=100.0)
        self.assertFalse(allowed)
        self.assertEqual(killed_pids, [54321])
        self.assertEqual(json.loads(cancel_file.read_text(encoding="utf-8"))["status"], "cancelled")

        # 4. Concurrent AntigravityBackend.dispatch on same workspace under _workspace_guard_lock allows ONLY 1 pending task
        ag_backend = AntigravityBackend()
        cfg_ag = {"backend": "antigravity", "workdir": str(self.tmp_root), "queue_mode": False}
        barrier = threading.Barrier(2)
        msgs: list[str] = []

        def _dispatch_one(tid: str) -> None:
            barrier.wait(timeout=3.0)
            with patch("l3_backends.antigravity._is_worker_alive", return_value=False):
                msgs.append(ag_backend.dispatch(tid, f"并发任务 {tid}", False, cfg_ag))

        t1 = threading.Thread(target=_dispatch_one, args=("ag_race_1",))
        t2 = threading.Thread(target=_dispatch_one, args=("ag_race_2",))
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        self.assertEqual(len(msgs), 2)
        pending_files = list(self.pending.glob("*.json"))
        self.assertEqual(len(pending_files), 1, f"Expected exactly 1 pending task under lock, got {pending_files}")
        self.assertTrue(any("已有任务进行中" in m for m in msgs), f"Second dispatch should be rejected as busy: {msgs}")
        for pf in pending_files:
            pf.unlink(missing_ok=True)

        # 5. Orphaned .claimed.<dead_pid>.<ms>.tmp from crashed sidecar worker is reaped and does NOT deadlock workspace;
        #    while a live-PID fresh .claimed.<live_pid>.<now_ms>.tmp STILL blocks concurrent dispatch.
        orphan_claim = self.pending / ".dead_task.claimed.99999991.1000.tmp"
        orphan_claim.write_text(
            json.dumps({"id": "dead_task", "workdir": str(self.tmp_root), "prompt": "crashed"}),
            encoding="utf-8",
        )
        with patch("l3_backends.antigravity._is_worker_alive", return_value=False):
            msg_after_orphan = ag_backend.dispatch("ag_after_orphan", "恢复任务", False, cfg_ag)
        self.assertNotIn("已有任务进行中", msg_after_orphan)
        self.assertFalse(orphan_claim.exists(), "Orphaned dead-PID claim file must be reaped automatically")
        for pf in self.pending.glob("*.json"):
            pf.unlink(missing_ok=True)

        live_claim = self.pending / f".live_task.claimed.{os.getpid()}.{int(time.time() * 1000)}.tmp"
        live_claim.write_text(
            json.dumps({"id": "live_task", "workdir": str(self.tmp_root), "prompt": "in-flight"}),
            encoding="utf-8",
        )
        try:
            with patch("l3_backends.antigravity._is_worker_alive", return_value=False):
                msg_live_busy = ag_backend.dispatch("ag_vs_live_claim", "冲突任务", False, cfg_ag)
            self.assertIn("已有任务进行中", msg_live_busy)
            self.assertTrue(live_claim.exists(), "Fresh live-PID claim file must NOT be reaped")
        finally:
            live_claim.unlink(missing_ok=True)

        # 6. _salvage_and_clean_task_logs refuses to read or delete files outside LOGS_DIR
        outside_file = self.tmp_root / "do_not_delete.txt"
        outside_file.write_text("SECRET_OUTSIDE_CONTENT", encoding="utf-8")
        tampered_obj = {"stdout_log": str(outside_file), "stderr_log": str(outside_file)}
        l3_backends._salvage_and_clean_task_logs(tampered_obj)
        self.assertTrue(outside_file.exists(), "_salvage_and_clean_task_logs must NEVER unlink files outside LOGS_DIR")
        self.assertNotIn("stdout", tampered_obj)
        self.assertNotIn("stderr", tampered_obj)

    def test_p2_watchdog_identity_hung_cleanup_sidecar_atomic_claim_and_vision_phash(self):
        """4.1 #3: Watchdog port-owner verify + hung spawn kill, sidecar atomic claim, vision UIA hash & cursor-tolerant phash."""
        import agy_sidecar_worker
        import pc_mcp_watchdog as wd

        # 1. find_pc_mcp_pids ignores unrelated process listening on 18900
        fake_conn = MagicMock(status=wd.psutil.CONN_LISTEN, laddr=MagicMock(port=18900), pid=77777)
        fake_proc_other = MagicMock()
        fake_proc_other.name.return_value = "nginx.exe"
        fake_proc_other.cmdline.return_value = ["nginx.exe", "-g", "daemon off;"]
        with (
            patch.object(wd.psutil, "net_connections", return_value=[fake_conn]),
            patch.object(wd.psutil, "Process", return_value=fake_proc_other),
            patch.object(wd.psutil, "process_iter", return_value=[]),
        ):
            self.assertEqual(wd.find_pc_mcp_pids(port=18900), [])

        # 2. restart_pc_mcp_server terminates newly spawned hung process if health check times out
        fake_spawned = MagicMock()
        fake_spawned.pid = 88888
        fake_spawned.poll.return_value = None
        with (
            patch.object(wd, "stop_pc_mcp_only"),
            patch.object(wd, "spawn_pc_mcp_server", return_value=fake_spawned),
            patch.object(wd.l3_backends, "get_process_create_time", return_value=None),
            patch.object(wd, "probe_pc_mcp_health", return_value=(False, "timeout")),
            patch.object(wd.time, "sleep"),
        ):
            ok_r = wd.restart_pc_mcp_server(port=18900, health_wait_steps=2)
        self.assertFalse(ok_r)
        fake_spawned.terminate.assert_called_once()

        # 3. Sidecar _process_task atomically claims pending file BEFORE running agentapi (no double dispatch)
        tf = self.pending / "sidecar_atomic_1.json"
        tf.write_text(json.dumps({"id": "sidecar_atomic_1", "prompt": "测试原子认领"}), encoding="utf-8")
        seen_pending_during_popen: list[bool] = []

        class _FakeProc:
            pid = 12345
            returncode = 0

            def communicate(self, timeout=None):
                seen_pending_during_popen.append(tf.exists())
                return ("11111111-2222-3333-4444-555555555555", "")

        with (
            patch.object(agy_sidecar_worker, "PENDING_DIR", self.pending),
            patch.object(agy_sidecar_worker, "ACTIVE_DIR", self.active),
            patch.object(agy_sidecar_worker, "_build_agentapi_prefix", return_value=["agentapi"]),
            patch.object(agy_sidecar_worker, "_prepare_cmd", side_effect=lambda c: c),
            patch.object(agy_sidecar_worker.subprocess, "Popen", return_value=_FakeProc()),
        ):
            self.assertTrue(agy_sidecar_worker._process_task(tf))
            # Second concurrent attempt on same path immediately returns False
            self.assertFalse(agy_sidecar_worker._process_task(tf))
        self.assertEqual(seen_pending_during_popen, [False], "Pending .json must be claimed/renamed before agentapi runs")

        # 3b. Sidecar _reap_orphaned_claim_files removes dead-PID claims while preserving live-PID fresh claims
        sc_dead_claim = self.pending / ".sc_dead.claimed.99999991.1000.tmp"
        sc_live_claim = self.pending / f".sc_live.claimed.{os.getpid()}.{int(time.time() * 1000)}.tmp"
        sc_dead_claim.write_text("{}", encoding="utf-8")
        sc_live_claim.write_text("{}", encoding="utf-8")
        try:
            reaped_claims = agy_sidecar_worker._reap_orphaned_claim_files(self.pending)
            self.assertEqual(reaped_claims, 1)
            self.assertFalse(sc_dead_claim.exists())
            self.assertTrue(sc_live_claim.exists())
        finally:
            sc_live_claim.unlink(missing_ok=True)

        # 4. Vision: Stage-1 UIA hit populates region_hash; perceptual hash tolerates 1-2px blinking cursor
        base_img = Image.new("RGB", (200, 120), color=(240, 240, 240))
        d1 = ImageDraw.Draw(base_img)
        d1.rectangle((40, 30, 160, 90), fill=(255, 255, 255), outline=(60, 60, 60))
        d1.rectangle((55, 50, 90, 65), fill=(30, 30, 30))

        cursor_img = base_img.copy()
        d2 = ImageDraw.Draw(cursor_img)
        # Draw a thin 1px blinking text caret at x=100
        d2.line((100, 48, 100, 68), fill=(0, 0, 0), width=1)

        popup_img = Image.new("RGB", (200, 120), color=(20, 20, 40))

        h_base, box_b = vision._compute_region_hash(base_img, 100, 60)
        h_cursor, _ = vision._compute_region_hash(cursor_img, 100, 60, box=box_b)
        h_popup, _ = vision._compute_region_hash(popup_img, 100, 60, box=box_b)
        self.assertTrue(h_base.startswith("phash:"))
        self.assertTrue(vision._region_hashes_match(h_base, h_cursor), "Perceptual hash must tolerate 1px blinking cursor")
        self.assertFalse(vision._region_hashes_match(h_base, h_popup), "Perceptual hash must reject popup/window switch")

        fake_uia = [{"id": 1, "type": "Button", "name": "发送", "left": 80, "top": 40, "right": 120, "bottom": 80, "cx": 100, "cy": 60}]
        with (
            patch.object(pcm, "_collect_uia_elements", return_value=fake_uia),
            patch.object(pcm.ImageGrab, "grab", return_value=base_img),
        ):
            uia_loc = vision._smart_locate_target("发送")
        self.assertIsNotNone(uia_loc)
        self.assertTrue(bool(uia_loc.region_hash), "Stage-1 UIA hit must also populate region_hash")

    def test_p01_socks5_proxy_env_split_and_fallback(self):
        """P0-1: DeepSeek uses direct trust_env=False client; Gemini uses proxy-aware client with SOCKS5 & ImportError fallback."""
        vision.close_vision_http_client()
        try:
            with patch.dict(os.environ, {"ALL_PROXY": "socks5://127.0.0.1:10808"}, clear=False):
                c_direct = vision._get_vision_http_client(False)
                c_proxy = vision._get_vision_http_client(True)
                self.assertIsNot(c_direct, c_proxy)
                self.assertFalse(c_direct._trust_env)
                self.assertTrue(c_proxy._trust_env)

            # Simulate missing socksio -> ImportError falls back cleanly to trust_env=False
            vision.close_vision_http_client()
            real_client_cls = vision.httpx.Client
            calls = []

            def _fake_httpx_client(*args, **kwargs):
                calls.append(dict(kwargs))
                if kwargs.get("trust_env", True):
                    raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed")
                return real_client_cls(*args, **kwargs)

            with patch.object(vision.httpx, "Client", side_effect=_fake_httpx_client):
                c_fb = vision._get_vision_http_client(True)
                self.assertFalse(c_fb._trust_env)
                self.assertEqual([c.get("trust_env") for c in calls], [True, False])
        finally:
            vision.close_vision_http_client()

    def test_p21_antigravity_dispatched_transcript_mutex_and_followup_bypass(self):
        """P2-1: Dispatched Antigravity session with real step-level DONE transcript blocks new task on same workdir, allows follow-up."""
        from l3_backends import claim_workspace_or_enqueue

        ag_backend = AntigravityBackend()
        brain_dir = self.tmp_root / "fake_brain"
        conv_id = "conv_active_001"
        logs_dir = brain_dir / conv_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_fp = logs_dir / "transcript.jsonl"
        # Real Antigravity transcript schema: intermediate USER_INPUT, PLANNER_RESPONSE(tool_calls), and GENERIC tool results ALL carry status="DONE"!
        for active_step in (
            {"type": "USER_INPUT", "status": "DONE", "content": "重构数据库层"},
            {"type": "PLANNER_RESPONSE", "status": "DONE", "thinking": "Reading files...", "tool_calls": [{"name": "read_file"}]},
            {"type": "GENERIC", "status": "DONE", "content": "Created At: ...\nCompleted At: ..."},
        ):
            transcript_fp.write_text(json.dumps(active_step) + "\n", encoding="utf-8")
            disp_fp = self.active / "ag_disp_1.json"
            disp_fp.write_text(
                json.dumps(
                    {
                        "id": "ag_disp_1",
                        "backend": "antigravity",
                        "status": "dispatched",
                        "workdir": str(self.tmp_root),
                        "conversation_id": conv_id,
                        "prompt": "重构数据库层",
                    }
                ),
                encoding="utf-8",
            )
            cfg_ag = {"backend": "antigravity", "workdir": str(self.tmp_root), "queue_mode": False}
            try:
                with (
                    patch("l3_backends.antigravity._get_brain_dir", return_value=brain_dir),
                    patch("l3_backends.antigravity._is_worker_alive", return_value=False),
                ):
                    # 1. Second independent task on same workdir MUST be blocked during intermediate DONE steps!
                    msg_blocked = ag_backend.dispatch("ag_new_2", "再改一个文件", False, cfg_ag)
                    self.assertIn("已有任务进行中", msg_blocked, f"Failed to block on active step: {active_step}")

                    # 1b. CLI claim_workspace_or_enqueue with queue_if_busy=True must reject ('busy') rather than stranding in _workspace_queues
                    status_cli, msg_cli = claim_workspace_or_enqueue(
                        workdir=self.tmp_root,
                        task_id="cli_behind_ag",
                        backend="dsh",
                        prompt="测试冲突",
                        start_fn=lambda: None,
                        queue_if_busy=True,
                    )
                    self.assertEqual(status_cli, "busy")
                    self.assertIn("已有任务进行中", msg_cli or "")

                    # 2. Follow-up on the active conversation IS allowed through
                    with patch("l3_backends.antigravity.load_last_active_task", return_value={"conversation_id": conv_id}):
                        msg_fu = ag_backend.dispatch("ag_fu_3", "顺便补个单测", True, cfg_ag)
                    self.assertNotIn("已有任务进行中", msg_fu)
                    for pf in self.pending.glob("*.json"):
                        pf.unlink(missing_ok=True)
            finally:
                disp_fp.unlink(missing_ok=True)

        # 3. Once final PLANNER_RESPONSE has NO tool_calls (status="DONE"), turn is complete and new independent task is allowed
        disp_fp = self.active / "ag_disp_1.json"
        disp_fp.write_text(
            json.dumps(
                {
                    "id": "ag_disp_1",
                    "backend": "antigravity",
                    "status": "dispatched",
                    "workdir": str(self.tmp_root),
                    "conversation_id": conv_id,
                    "prompt": "重构数据库层",
                }
            ),
            encoding="utf-8",
        )
        try:
            with (
                patch("l3_backends.antigravity._get_brain_dir", return_value=brain_dir),
                patch("l3_backends.antigravity._is_worker_alive", return_value=False),
            ):
                transcript_fp.write_text(
                    json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "已完成"}) + "\n",
                    encoding="utf-8",
                )
                msg_ok = ag_backend.dispatch("ag_new_4", "开启新任务", False, {"backend": "antigravity", "workdir": str(self.tmp_root)})
                self.assertNotIn("已有任务进行中", msg_ok)
                for pf in self.pending.glob("*.json"):
                    pf.unlink(missing_ok=True)
        finally:
            disp_fp.unlink(missing_ok=True)

    def test_p22_negative_coord_secondary_monitor_and_p23_winmm_defaults(self):
        """P2-2 & P2-3: Secondary negative-coordinate monitor grounding + WinMM max_inflight=3."""
        sec_mon = (-1920, 0, 0, 1080)
        sec_img = Image.new("RGB", (1920, 1080), color=(30, 40, 50))
        d = ImageDraw.Draw(sec_img)
        # Button at local (960, 540) -> desktop (-960, 540)
        d.rectangle((910, 510, 1010, 570), fill=(220, 80, 40), outline=(255, 255, 255))

        fake_uia = [
            {
                "id": 1,
                "type": "Button",
                "name": "副屏确认",
                "left": -1010,
                "top": 510,
                "right": -910,
                "bottom": 570,
                "cx": -960,
                "cy": 540,
            }
        ]
        grab_kwargs = []

        def _fake_grab(*args, **kwargs):
            grab_kwargs.append(dict(kwargs))
            return sec_img.copy()

        vlm_json = json.dumps({"found": True, "mark_id": 1, "label": "副屏确认", "x_min": 470, "y_min": 470, "x_max": 530, "y_max": 530})
        with (
            patch.object(vision, "_get_target_monitor_rect", return_value=sec_mon),
            patch.object(pcm, "_collect_uia_elements", return_value=fake_uia),
            patch.object(pcm.ImageGrab, "grab", side_effect=_fake_grab),
            patch.object(pcm, "_call_vision_llm", return_value=vlm_json),
        ):
            # Force Stage-2 SoM path using positional hint ("左边")
            loc = vision._smart_locate_target("左边的副屏确认")
        self.assertIsNotNone(loc)
        self.assertEqual((loc[0], loc[1]), (-960, 540))
        self.assertEqual(loc.monitor_rect, sec_mon)
        self.assertTrue(any(k.get("all_screens") is True and k.get("bbox") == sec_mon for k in grab_kwargs))

        # P2-3: _WinMMPlayer default max_inflight == 3
        import inspect
        from pc_mcp_modules import audio_bridge

        sig = inspect.signature(audio_bridge._WinMMPlayer.__init__)
        self.assertEqual(sig.parameters["max_inflight"].default, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Unit and adversarial tests for host-side P1 stability & reliability items (#15-#18)."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import uvicorn

MCP_DIR = Path(__file__).resolve().parent
SERVER_DIR = MCP_DIR.parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import l3_backends
import pc_control_mcp as pcm
import pc_mcp_watchdog as wd


class TestP1HostStability(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.active_dir = self.tmp_root / "agy_tasks" / "active"
        self.pending_dir = self.tmp_root / "agy_tasks" / "pending"
        self.active_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # P1-15: Watchdog health probe, backoff restart, boot.log tail, ps1 scripts
    # ------------------------------------------------------------------
    def test_p115_watchdog_health_probe_backoff_restart_and_ps1_hardening(self):
        # 1. Real authenticated /audio/status probe against local secured ASGI app
        token = "p115-watchdog-test-token-1234567890"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        app = pcm.create_secured_asgi_app(token, port=port, bind_host="127.0.0.1")
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
        server = uvicorn.Server(config)
        srv_thread = threading.Thread(target=server.run, daemon=True)
        srv_thread.start()
        try:
            ready = False
            for _ in range(40):
                if server.started:
                    ready = True
                    break
                time.sleep(0.05)
            self.assertTrue(ready, "Local test ASGI server did not start")

            tok_file = self.tmp_root / "pc_mcp_token.txt"
            tok_file.write_text(token + "\n", encoding="utf-8")

            ok_good, det_good = wd.probe_pc_mcp_health(port=port, token_file=tok_file)
            self.assertTrue(ok_good, det_good)

            ok_bad_tok, det_bad_tok = wd.probe_pc_mcp_health(port=port, token="wrong-token")
            self.assertFalse(ok_bad_tok)
            self.assertIn("401", det_bad_tok)

            ok_missing, _ = wd.probe_pc_mcp_health(port=port, token_file=self.tmp_root / "missing.txt")
            self.assertFalse(ok_missing)
        finally:
            server.should_exit = True
            srv_thread.join(timeout=3.0)

        # 2. WatchdogSupervisor state machine: consecutive failure threshold, exponential backoff, healthy reset, stop flag
        probe_results = [
            (True, "ok"),
            (False, "timeout-1"),
            (False, "timeout-2"),
            (False, "timeout-3"),
            (False, "timeout-4"),
            (True, "ok"),
            (False, "timeout-5"),
            (False, "timeout-6"),
        ]
        probe_idx = {"i": 0}
        sleeps: list[float] = []
        restarts = {"n": 0}
        now_t = {"t": 100.0}
        stop_flag = {"v": False}

        def _probe():
            i = probe_idx["i"]
            probe_idx["i"] += 1
            return probe_results[min(i, len(probe_results) - 1)]

        def _restart():
            restarts["n"] += 1
            return True

        sup = wd.WatchdogSupervisor(
            probe_fn=_probe,
            restart_fn=_restart,
            stop_check_fn=lambda: stop_flag["v"],
            sleep_fn=lambda s: sleeps.append(s),
            clock_fn=lambda: now_t["t"],
            failure_threshold=2,
            backoff_schedule=(1.0, 2.0, 5.0),
            healthy_reset_seconds=60.0,
            state_file=self.tmp_root / "wd_state.json",
        )
        self.assertEqual(sup.tick(), "healthy")
        self.assertEqual(sup.tick(), "degraded")  # 1/2 failure -> no restart yet
        self.assertEqual(restarts["n"], 0)
        self.assertEqual(sup.tick(), "restarted")  # 2/2 failure -> backoff 1.0s + restart #1
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(restarts["n"], 1)

        # Fail twice more immediately -> escalates backoff to 2.0s
        now_t["t"] += 10.0
        self.assertEqual(sup.tick(), "degraded")
        self.assertEqual(sup.tick(), "restarted")
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(restarts["n"], 2)

        # Healthy for >=60s -> resets backoff index back to 1.0s
        now_t["t"] += 70.0
        self.assertEqual(sup.tick(), "healthy")
        self.assertEqual(sup.tick(), "degraded")
        self.assertEqual(sup.tick(), "restarted")
        self.assertEqual(sleeps, [1.0, 2.0, 1.0])

        # Stop flag -> stops immediately without probing/restarting
        stop_flag["v"] = True
        self.assertEqual(sup.tick(), "stopped")
        self.assertEqual(restarts["n"], 3)

        # 3. boot.log tail helper
        boot_log = self.tmp_root / "boot.log"
        boot_log.write_text("\n".join(f"line-{i}" for i in range(60)), encoding="utf-8")
        tail = wd.read_log_tail(boot_log, max_lines=10)
        self.assertEqual(tail.splitlines()[0], "line-50")
        self.assertEqual(tail.splitlines()[-1], "line-59")

        # 3b. Process matching must ONLY match Python directly executing pc_control_mcp.py (never user shells/editors!)
        self.assertTrue(wd._is_pc_mcp_python_cmdline("python.exe", ["python.exe", "-X", "utf8", str(wd.MCP_SCRIPT)]))
        self.assertTrue(wd._is_pc_mcp_python_cmdline("pythonw.exe", ["pythonw.exe", str(wd.MCP_SCRIPT)]))
        self.assertFalse(wd._is_pc_mcp_python_cmdline("powershell.exe", ["powershell.exe", "-Command", "Get-Process pc_control_mcp.py"]))
        self.assertFalse(wd._is_pc_mcp_python_cmdline("Code.exe", ["Code.exe", str(wd.MCP_SCRIPT)]))
        self.assertFalse(wd._is_pc_mcp_python_cmdline("notepad.exe", ["notepad.exe", str(wd.MCP_SCRIPT)]))
        self.assertFalse(wd._is_pc_mcp_python_cmdline("python.exe", ["python.exe", "-c", "print('pc_control_mcp.py')"]))
        self.assertFalse(wd._is_pc_mcp_python_cmdline("python.exe", ["python.exe", "-m", "unittest", "pc_control_mcp.py"]))

        # 3c. spawn_pc_mcp_server falls back cleanly to DEVNULL when boot.log is locked (PermissionError)
        fake_popen = MagicMock()
        with (
            patch("builtins.open", side_effect=PermissionError("locked by another process")),
            patch.object(wd.subprocess, "Popen", return_value=fake_popen) as mock_popen,
        ):
            p = wd.spawn_pc_mcp_server(boot_log_path=self.tmp_root / "locked_boot.log")
            self.assertIs(p, fake_popen)
            self.assertEqual(mock_popen.call_args.kwargs.get("stdout"), subprocess.DEVNULL)

        # 4. start_all.ps1 and stop_all.ps1 contract checks (UTF-8 BOM + CRLF + token auth + watchdog + L3 cleanup)
        start_ps1 = (SERVER_DIR / "data" / "start_all.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("/audio/status", start_ps1)
        self.assertIn("Bearer", start_ps1)
        self.assertIn("pc_mcp_18900_boot.log", start_ps1)
        self.assertIn("pc_mcp_watchdog.py", start_ps1)
        self.assertIn("--cleanup-l3", start_ps1)
        self.assertNotIn("taskkill", start_ps1.lower(), "start_all.ps1 must never taskkill /T 18900 (browser protection)")

        stop_ps1 = (SERVER_DIR / "data" / "stop_all.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("pc_mcp_watchdog.py", stop_ps1)
        self.assertIn("--cleanup-l3", stop_ps1)
        self.assertIn("Stop-ProjectPythonProcess $proc $mcpScript", stop_ps1)
        process_guard = (SERVER_DIR / "data" / "host_process.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Stop-Process -Id $current.ProcessId -Force", process_guard)
        other_copy = self.tmp_root / wd.MCP_SCRIPT.name
        self.assertFalse(wd._is_pc_mcp_python_cmdline("python.exe", ["python.exe", str(other_copy)]))
        self.assertIn(r"pc-mcp\state\agy_tasks\worker_heartbeat.json", stop_ps1)

    # ------------------------------------------------------------------
    # P1-16: Global GUI mutex prevents concurrent mouse/keyboard/window interleaving
    # ------------------------------------------------------------------
    def test_p116_global_gui_mutex_serializes_concurrent_ui_and_window_ops(self):
        from PIL import Image

        events: list[tuple[str, str]] = []
        ev_lock = threading.Lock()
        conc = {"active": 0, "max_seen": 0}

        def _enter(tag: str):
            with ev_lock:
                conc["active"] += 1
                if conc["active"] > conc["max_seen"]:
                    conc["max_seen"] = conc["active"]
                events.append((tag, "enter"))

        def _exit(tag: str):
            with ev_lock:
                events.append((tag, "exit"))
                conc["active"] -= 1

        def _fake_locate(desc: str):
            # E-3: VLM grounding runs OUTSIDE _gui_lock; only screenshot/UIA and mouse click hold _gui_lock.
            time.sleep(0.06)
            return 100, 200, f"fake:{desc}"

        def _fake_move(x, y, duration=0):
            _enter("mouse_click")
            time.sleep(0.04)

        def _fake_click(x, y):
            _exit("mouse_click")

        def _fake_shell_open(url, params=None):
            _enter("open_web")
            time.sleep(0.07)
            _exit("open_web")

        def _fake_grab():
            _enter("grab_screen")
            time.sleep(0.05)
            _exit("grab_screen")
            return Image.new("RGB", (64, 64), color=(10, 20, 30))

        def _fake_walk(auto, root, max_items, sw, sh):
            _enter("inspect_ui")
            time.sleep(0.06)
            _exit("inspect_ui")
            return []

        def _fake_hotkey(*k):
            _enter("hotkey")
            time.sleep(0.04)
            _exit("hotkey")

        open_web_calls = {"n": 0}

        def _fake_list_windows():
            open_web_calls["n"] += 1
            if open_web_calls["n"] == 1:
                return []
            return [{"hwnd": 12345, "pid": 1, "proc": "msedge.exe", "class": "Chrome_WidgetWin_1", "title": "Example"}]

        audit_file = self.tmp_root / "l2_audit.jsonl"
        with (
            patch.object(pcm, "_get_mode", return_value="full"),
            patch.object(pcm, "L2_AUDIT_LOG_FILE", audit_file),
            patch.object(pcm, "_smart_locate_target", side_effect=_fake_locate),
            patch.object(pcm.pyautogui, "moveTo", side_effect=_fake_move),
            patch.object(pcm.pyautogui, "click", side_effect=_fake_click),
            patch.object(pcm, "_shell_open", side_effect=_fake_shell_open),
            patch.object(pcm, "_list_top_windows", side_effect=_fake_list_windows),
            patch.object(pcm, "_force_foreground_window", return_value=True),
            patch.object(pcm, "_get_window_title", return_value="Example"),
            patch.object(pcm, "_get_default_browser_proc", return_value="msedge.exe"),
            patch.object(pcm, "_get_foreground_window_info", return_value={"process": "notepad.exe", "class": "Notepad", "title": "test"}),
            patch.object(pcm.pyautogui, "hotkey", side_effect=_fake_hotkey),
            patch.object(pcm.ImageGrab, "grab", side_effect=_fake_grab),
            patch.object(pcm, "_call_vision_llm", return_value="ok"),
            patch.object(pcm, "_walk_uia_controls", side_effect=_fake_walk),
        ):
            results: list[str] = []
            threads = [
                threading.Thread(target=lambda: results.append(pcm.pc_ui_action(action="click", element_name="btn_A"))),
                threading.Thread(target=lambda: results.append(pcm.pc_ui_action(action="click", element_name="btn_B"))),
                threading.Thread(target=lambda: results.append(pcm.pc_ui_action(action="hotkey", hotkey="ctrl+c"))),
                threading.Thread(target=lambda: results.append(pcm.pc_open_app_or_web("https://example.com"))),
                threading.Thread(target=lambda: results.append(pcm.pc_look_at_screen("test"))),
                threading.Thread(target=lambda: results.append(pcm.pc_inspect_window_ui(""))),
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=8.0)

            self.assertEqual(len(results), 6)
            self.assertEqual(
                conc["max_seen"],
                1,
                f"Concurrent GUI operations overlapped! max_seen={conc['max_seen']}, events={events}",
            )

    # ------------------------------------------------------------------
    # P1-17: Corrupt task files isolated + create_time() blocks PID-reuse kills
    # ------------------------------------------------------------------
    def test_p117_corrupt_task_files_isolated_and_create_time_guards_pid_reuse(self):
        now = time.time()
        with patch.object(l3_backends, "ACTIVE_DIR", self.active_dir):
            # 1. Poisoned files that previously crashed reap_stale_running_tasks() and broke 18900 boot
            (self.active_dir / "bad_json.json").write_text("{corrupt json", encoding="utf-8")
            (self.active_dir / "not_dict.json").write_text("[1, 2, 3]", encoding="utf-8")
            (self.active_dir / "bad_types.json").write_text(
                json.dumps(
                    {
                        "id": "bad_types",
                        "status": "running",
                        "started_ts": "not-a-float",
                        "timeout_seconds": {"bad": 1},
                        "owner_pid": "xyz",
                        "pid": ["not-an-int"],
                    }
                ),
                encoding="utf-8",
            )
            (self.active_dir / "bool_types.json").write_text(
                json.dumps(
                    {
                        "id": "bool_types",
                        "status": "running",
                        "started_ts": True,
                        "timeout_seconds": False,
                        "owner_pid": True,
                        "pid": True,
                    }
                ),
                encoding="utf-8",
            )
            # Valid expired zombie task alongside poisoned files
            valid_zombie = self.active_dir / "valid_zombie.json"
            valid_zombie.write_text(
                json.dumps(
                    {
                        "id": "valid_zombie",
                        "status": "running",
                        "started_ts": now - 900,
                        "timeout_seconds": 60,
                        "owner_pid": 999999,
                        "pid": 999998,
                    }
                ),
                encoding="utf-8",
            )
            # PID reuse trap: points to our OWN live PID (os.getpid()) with a mismatched create_time!
            pid_reuse_trap = self.active_dir / "pid_reuse_trap.json"
            pid_reuse_trap.write_text(
                json.dumps(
                    {
                        "id": "pid_reuse_trap",
                        "status": "running",
                        "started_ts": now - 900,
                        "timeout_seconds": 60,
                        "owner_pid": 999999,
                        "owner_create_time": 1000.0,
                        "pid": os.getpid(),
                        "pid_create_time": 1000.0,
                    }
                ),
                encoding="utf-8",
            )

            # Legacy PID reuse trap: owner_pid == os.getpid() without owner_create_time, started BEFORE our process existed
            my_ct = l3_backends.get_process_create_time(os.getpid()) or now
            legacy_owner_reuse = self.active_dir / "legacy_owner_reuse.json"
            legacy_owner_reuse.write_text(
                json.dumps(
                    {
                        "id": "legacy_owner_reuse",
                        "status": "running",
                        "started_ts": my_ct - 120.0,
                        "timeout_seconds": 600,
                        "owner_pid": os.getpid(),
                        "pid": None,
                    }
                ),
                encoding="utf-8",
            )

            killed_pids: list[int] = []
            orig_kill = l3_backends.kill_process_tree

            def _spy_kill(pid, *args, **kwargs):
                killed_pids.append(int(pid))
                return orig_kill(pid, *args, **kwargs)

            with patch.object(l3_backends, "kill_process_tree", side_effect=_spy_kill):
                reaped = l3_backends.reap_stale_running_tasks(now=now)

            # Must not crash, must reap valid_zombie + pid_reuse_trap + legacy_owner_reuse cleanly,
            # and MUST NEVER attempt to kill os.getpid() despite pid_reuse_trap referencing it!
            self.assertGreaterEqual(reaped, 3)
            self.assertNotIn(os.getpid(), killed_pids, "PID reuse trap attempted to kill current process!")
            self.assertEqual(json.loads(valid_zombie.read_text(encoding="utf-8"))["status"], "error")
            self.assertEqual(json.loads(pid_reuse_trap.read_text(encoding="utf-8"))["status"], "error")
            self.assertEqual(json.loads(legacy_owner_reuse.read_text(encoding="utf-8"))["status"], "error")

            # 2. Direct kill_process_tree create_time() verification on a REAL subprocess
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            try:
                real_ct = l3_backends.get_process_create_time(proc.pid)
                self.assertIsNotNone(real_ct)
                # Wrong expected_create_time MUST refuse to kill!
                killed_wrong = l3_backends.kill_process_tree(proc.pid, expected_create_time=real_ct - 600.0)
                self.assertFalse(killed_wrong)
                self.assertIsNone(proc.poll(), "kill_process_tree killed a process with mismatched create_time!")

                # cleanup_running_task_processes with matching pid_create_time MUST kill it cleanly
                live_rec = self.active_dir / "real_l3_child.json"
                live_rec.write_text(
                    json.dumps(
                        {
                            "id": "real_l3_child",
                            "status": "running",
                            "started_ts": now,
                            "timeout_seconds": 300,
                            "owner_pid": os.getpid(),
                            "pid": proc.pid,
                            "pid_create_time": real_ct,
                        }
                    ),
                    encoding="utf-8",
                )
                cleaned = l3_backends.cleanup_running_task_processes()
                self.assertEqual(cleaned, 1)
                proc.wait(timeout=4.0)
                self.assertIsNotNone(proc.poll())
                self.assertEqual(json.loads(live_rec.read_text(encoding="utf-8"))["status"], "error")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3.0)

    # ------------------------------------------------------------------
    # P1-18: requirements.txt pinned with == + sha256 hashes (incl. uvicorn & starlette) + pinned dsh
    # ------------------------------------------------------------------
    def test_p118_requirements_pinned_with_hashes_and_dsh_npx_pinned(self):
        from l3_backends.dsh import DSH_NPX_PACKAGE, DSH_PINNED_VERSION

        req_path = MCP_DIR / "requirements.txt"
        raw = req_path.read_text(encoding="utf-8")
        # Join backslash-continued lines into logical requirement entries
        logical_entries: list[str] = []
        buf = ""
        for raw_ln in raw.splitlines():
            ln = raw_ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if ln.endswith("\\"):
                buf += ln[:-1].strip() + " "
            else:
                buf += ln
                logical_entries.append(buf.strip())
                buf = ""
        if buf.strip():
            logical_entries.append(buf.strip())

        self.assertGreaterEqual(len(logical_entries), 11)
        seen_pkgs: set[str] = set()
        hash_re = re.compile(r"--hash=sha256:[0-9a-f]{64}")
        pin_re = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)\s+")
        for entry in logical_entries:
            self.assertNotIn(">=", entry, f"Unpinned >= specifier found: {entry[:80]}")
            self.assertNotIn("~=", entry, f"Unpinned ~= specifier found: {entry[:80]}")
            m = pin_re.match(entry)
            self.assertIsNotNone(m, f"Package not pinned with == : {entry[:80]}")
            seen_pkgs.add(m.group(1).lower())
            self.assertTrue(hash_re.search(entry), f"Missing --hash=sha256:... for {m.group(1)}")

        for required_pkg in (
            "mcp",
            "httpx",
            "pyautogui",
            "pyperclip",
            "pillow",
            "psutil",
            "pycaw",
            "comtypes",
            "uiautomation",
            "uvicorn",
            "starlette",
        ):
            self.assertIn(required_pkg, seen_pkgs, f"Missing required dependency in requirements.txt: {required_pkg}")

        # Verify init_setup.ps1 uses --require-hashes
        init_ps1 = (SERVER_DIR / "data" / "init_setup.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("--require-hashes", init_ps1)

        # Verify @deepseek-ai/dsh npx fallback is pinned to an explicit version
        self.assertTrue(DSH_PINNED_VERSION)
        self.assertNotIn("latest", DSH_NPX_PACKAGE.lower())
        self.assertRegex(DSH_NPX_PACKAGE, r"^@deepseek-ai/dsh@\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main(verbosity=2)

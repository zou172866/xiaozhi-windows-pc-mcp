"""Host pc-mcp unit tests for dsh-world borrowed Batch 1 & 2 enhancements:
- L2 click red-ring feedback marker + 16x16 grayscale frame change detection.
- Background window PrintWindow + PostMessage guardrails (terminal/Explorer/ELEVATED fail-closed)
  and non-intrusive action dispatch.
- Reusable L2 UI procedures: strict schema validation, path traversal prevention, versioned backup
  & rollback, outward confirmation gate, proportional window scaling, and deterministic replay.
- Taskbar inbox monitor: 10s quiet debounce aggregation, default system/self ignore filter,
  secret redaction, and HTTP /desktop/inbox routes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
from starlette.testclient import TestClient

_MCP_DIR = Path(__file__).resolve().parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import pc_control_mcp as pcm
from pc_mcp_modules import bg_desktop, procedures, taskbar_inbox


class TestClickFeedbackAndFrameDiff(unittest.TestCase):
    def test_draw_marker_and_frame_change_detection(self):
        img_a = Image.new("RGB", (320, 200), (240, 240, 240))
        img_b = Image.new("RGB", (320, 200), (240, 240, 240))
        img_c = Image.new("RGB", (320, 200), (40, 80, 160))

        fp_a = pcm._compute_frame_fingerprint(img_a)
        fp_b = pcm._compute_frame_fingerprint(img_b)
        fp_c = pcm._compute_frame_fingerprint(img_c)
        self.assertFalse(pcm._frames_changed(fp_a, fp_b))
        self.assertTrue(pcm._frames_changed(fp_a, fp_c))

        marked = pcm._draw_click_feedback_marker(img_a, 100, 80)
        # Pixel on the red ring (radius 16) should turn red-dominant
        r, g, b = marked.getpixel((116, 80))
        self.assertGreater(r, 180)
        self.assertLess(g, 120)

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "feedback.jpg"
            fake_ig = MagicMock()
            fake_ig.grab.return_value = img_c
            res = pcm._capture_click_feedback(
                fake_ig, 100, 80, monitor_rect=(0, 0, 320, 200), fp_before=fp_a, dest_path=dest
            )
            self.assertTrue(res["ok"])
            self.assertTrue(res["changed"])
            self.assertTrue(dest.is_file())


class TestBgDesktopGuardrailsAndDispatch(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._td.name)
        self.audit_log = self.state_dir / "l2_audit.jsonl"
        self.mode_file = self.state_dir / "pc_control_mode.json"
        self.mode_file.write_text(json.dumps({"mode": "full"}), encoding="utf-8")

    def tearDown(self):
        pcm.clear_emergency_stop()
        self._td.cleanup()

    def test_blocks_terminal_explorer_and_elevated_windows_fail_closed(self):
        fake_u32 = MagicMock()
        fake_u32.IsWindow.return_value = 1

        with (
            patch.object(pcm, "MODE_FILE", self.mode_file),
            patch.object(pcm, "L2_AUDIT_LOG_FILE", self.audit_log),
            patch.object(pcm, "_user32", fake_u32),
            patch.object(
                pcm,
                "_list_top_windows",
                return_value=[
                    {"hwnd": 101, "title": "PowerShell", "proc": "powershell.exe"},
                    {"hwnd": 102, "title": "下载 - 文件资源管理器", "proc": "explorer.exe"},
                    {"hwnd": 103, "title": "星穹铁道", "proc": "StarRail.exe"},
                ],
            ),
        ):
            # 1. Terminal blocked
            with patch.object(
                pcm,
                "_get_window_info_by_hwnd",
                return_value={"hwnd": 101, "pid": 500, "process": "powershell.exe", "class": "ConsoleWindowClass", "title": "PowerShell"},
            ):
                res_term = pcm.pc_bg_window_action(action="click", window_keyword="PowerShell", x=10, y=10)
                self.assertIn("安全护栏已拦截", res_term)

            # 2. Explorer CabinetWClass blocked in background mode
            with patch.object(
                pcm,
                "_get_window_info_by_hwnd",
                return_value={"hwnd": 102, "pid": 501, "process": "explorer.exe", "class": "CabinetWClass", "title": "下载"},
            ):
                res_exp = pcm.pc_bg_window_action(action="click", window_keyword="下载", x=10, y=10)
                self.assertIn("安全护栏已拦截", res_exp)

            # 3. Elevated admin process rejected with clear foreground guidance
            with (
                patch.object(
                    pcm,
                    "_get_window_info_by_hwnd",
                    return_value={"hwnd": 103, "pid": 502, "process": "StarRail.exe", "class": "UnityWndClass", "title": "星穹铁道"},
                ),
                patch.object(bg_desktop, "is_process_elevated", return_value=True),
            ):
                res_elev = pcm.pc_bg_window_action(action="click", window_keyword="星穹铁道", x=50, y=50)
                self.assertIn("ELEVATED", res_elev)
                self.assertIn("pc_ui_action", res_elev)

    def test_bg_click_detects_unchanged_window_and_posts_without_cursor_move(self):
        fake_u32 = MagicMock()
        fake_u32.IsWindow.return_value = 1
        fake_u32.ChildWindowFromPointEx.return_value = 0
        static_img = Image.new("RGB", (640, 400), (220, 220, 220))
        fb_img = self.state_dir / "fb.jpg"

        with (
            patch.object(pcm, "MODE_FILE", self.mode_file),
            patch.object(pcm, "L2_AUDIT_LOG_FILE", self.audit_log),
            patch.object(pcm, "LAST_ACTION_FEEDBACK_IMG", fb_img),
            patch.object(pcm, "_user32", fake_u32),
            patch.object(
                pcm,
                "_list_top_windows",
                return_value=[{"hwnd": 201, "title": "无标题", "proc": "notepad.exe"}],
            ),
            patch.object(
                pcm,
                "_get_window_info_by_hwnd",
                return_value={"hwnd": 201, "pid": 600, "process": "notepad.exe", "class": "Notepad", "title": "无标题"},
            ),
            patch.object(bg_desktop, "is_process_elevated", return_value=False),
            patch.object(bg_desktop, "capture_window_client", return_value=(static_img, (640, 400))),
            patch.object(pcm, "time") as m_time,
        ):
            # Also verify matching by real winapi `_list_top_windows` key "proc" & list_windows formatting
            listed = pcm.pc_bg_window_action(action="list_windows")
            self.assertIn("notepad.exe", listed)
            res = pcm.pc_bg_window_action(action="click", window_keyword="notepad", x=120, y=90)
            self.assertIn("后台静默点击", res)
            self.assertIn("画面无变化", res)
            self.assertTrue(fb_img.is_file())
            self.assertGreaterEqual(fake_u32.PostMessageW.call_count, 3)
            m_time.sleep.assert_called()


class TestProceduresEngine(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._td.name)
        self.proc_dir = self.state_dir / "procedures"
        self.audit_log = self.state_dir / "l2_audit.jsonl"
        self.mode_file = self.state_dir / "pc_control_mode.json"
        self.mode_file.write_text(json.dumps({"mode": "full"}), encoding="utf-8")

    def tearDown(self):
        pcm.clear_emergency_stop()
        self._td.cleanup()

    def test_schema_validation_blocks_unknown_keys_traversal_and_blacklisted_hotkeys(self):
        # Unknown key rejected
        with self.assertRaises(ValueError):
            procedures.validate_procedure_spec(
                {
                    "category": "demo",
                    "name": "bad_typo",
                    "where": "screen",
                    "typo_field": 123,
                    "steps": [{"op": "click", "x": 10, "y": 20}],
                }
            )
        # Path traversal rejected
        with self.assertRaises(ValueError):
            procedures.validate_procedure_spec(
                {
                    "category": "../escape",
                    "name": "ok",
                    "where": "screen",
                    "steps": [{"op": "click", "x": 10, "y": 20}],
                }
            )
        # Blacklisted hotkey win+r rejected at save time
        with self.assertRaises(ValueError):
            procedures.validate_procedure_spec(
                {
                    "category": "demo",
                    "name": "evil_hk",
                    "where": "screen",
                    "steps": [{"op": "hotkey", "hotkey": "win+r"}],
                }
            )
        # Windows reserved device name rejected
        with self.assertRaises(ValueError):
            procedures.validate_procedure_spec(
                {
                    "category": "demo",
                    "name": "CON",
                    "where": "screen",
                    "steps": [{"op": "click", "x": 10, "y": 20}],
                }
            )

    def test_save_version_backup_rollback_outward_gate_and_scaled_replay(self):
        v1 = {
            "category": "音乐",
            "name": "搜索播放",
            "description": "在音乐窗口搜索并播放",
            "where": "bg_window",
            "window_keyword": "云音乐",
            "ref_size": [1000, 500],
            "params": {"song": {"type": "string", "required": True, "description": "歌名"}},
            "steps": [
                {"op": "click", "x": 200, "y": 100},
                {"op": "type_text", "text": "{{song}}", "press_enter": True, "outward": True},
            ],
        }
        v2 = dict(v1, description="版本2")
        bg_calls = []

        def _fake_bg_exec(**kwargs):
            bg_calls.append(kwargs)
            return True, f"已在后台静默输入：「{kwargs.get('text') or ''}」成功。"

        with (
            patch.object(pcm, "MODE_FILE", self.mode_file),
            patch.object(pcm, "PROCEDURES_DIR", self.proc_dir),
            patch.object(pcm, "L2_AUDIT_LOG_FILE", self.audit_log),
        ):
            # Empty save returns schema guide
            guide = pcm.pc_ui_procedure(action="save")
            self.assertIn("操作剧本 JSON 格式说明", guide)

            r1 = pcm.pc_ui_procedure(action="save", procedure_json=json.dumps(v1, ensure_ascii=False))
            self.assertIn("音乐/搜索播放", r1)

            r2 = pcm.pc_ui_procedure(action="save", procedure_json=json.dumps(v2, ensure_ascii=False))
            self.assertIn("v1.json", r2)
            self.assertEqual(procedures.get_procedure("音乐", "搜索播放")["description"], "版本2")

            rb = pcm.pc_ui_procedure(action="rollback", category="音乐", name="搜索播放")
            self.assertIn("v1", rb)
            self.assertEqual(procedures.get_procedure("音乐", "搜索播放")["description"], "在音乐窗口搜索并播放")

            # Replay with resized window (2000x1000 -> 2x coords) and locked hwnd
            with (
                patch.object(bg_desktop, "find_bg_window", return_value={"hwnd": 301, "title": "网易云音乐", "proc": "cloudmusic.exe"}),
                patch.object(bg_desktop, "check_bg_window_guard", return_value=({"hwnd": 301}, None)),
                patch.object(bg_desktop, "capture_window_client", return_value=(Image.new("RGB", (2000, 1000)), (2000, 1000))),
                patch.object(bg_desktop, "_execute_bg_window_action_result", side_effect=_fake_bg_exec),
            ):
                # Default allow_outward=False pauses before step 2
                paused = pcm.pc_ui_procedure(
                    action="run",
                    category="音乐",
                    name="搜索播放",
                    args_json=json.dumps({"song": "夜曲"}, ensure_ascii=False),
                )
                self.assertIn("outward", paused)
                self.assertEqual(len(bg_calls), 1)
                self.assertEqual((bg_calls[0]["x"], bg_calls[0]["y"]), (400, 200))
                self.assertEqual(bg_calls[0]["target_hwnd"], 301)

                # Explicit allow_outward=True completes even when text contains "失败" (no false substring abort!)
                bg_calls.clear()
                done = pcm.pc_ui_procedure(
                    action="run",
                    category="音乐",
                    name="搜索播放",
                    args_json=json.dumps({"song": "编译失败怎么办"}, ensure_ascii=False),
                    allow_outward=True,
                )
                self.assertIn("已完成剧本 音乐/搜索播放（共 2/2 步", done)
                self.assertEqual(bg_calls[1]["text"], "编译失败怎么办")

            # Delete also cleans backups so rollback cannot resurrect deleted procedure
            pcm.pc_ui_procedure(action="save", procedure_json=json.dumps(v2, ensure_ascii=False))
            del_msg = pcm.pc_ui_procedure(action="delete", category="音乐", name="搜索播放")
            self.assertIn("已删除", del_msg)
            rb_after_del = pcm.pc_ui_procedure(action="rollback", category="音乐", name="搜索播放")
            self.assertIn("没有可回退", rb_after_del)


class TestTaskbarInboxAndRoutes(unittest.TestCase):
    def test_debounce_aggregation_ignore_noise_and_http_routes(self):
        t_wall = {"v": 1000.0}
        t_mono = {"v": 100.0}
        mon = taskbar_inbox.TaskbarInboxMonitor(
            clock_fn=lambda: t_wall["v"],
            mono_fn=lambda: t_mono["v"],
            enable_win32_hook=False,
        )
        # Disabled by default -> ignores events
        self.assertFalse(mon.record_event(app="WeChat", exe="wechat.exe", flashed=True))

        mon.configure(enabled=True, quiet_seconds=10.0)
        # System noise ignored
        self.assertFalse(mon.record_event(app="conhost", exe="conhost.exe", flashed=True))
        self.assertFalse(mon.record_event(app="Windows Explorer", exe="explorer.exe", flashed=True))
        self.assertFalse(mon.record_event(app="Microsoft.Windows.Shell", flashed=True))

        # Valid app burst + secret redaction in preview
        self.assertTrue(mon.record_event(app="WeChat", exe="wechat.exe", flashed=True))
        t_mono["v"] += 3.0
        t_wall["v"] += 3.0
        self.assertTrue(
            mon.record_event(
                app="WeChat",
                exe="wechat.exe",
                notification_text="密钥 sk-1234567890abcdefghijklmnop 刚发了消息",
            )
        )
        # Still inside 10s quiet window -> staging only, not settled yet
        s_mid = mon.get_summary()
        self.assertEqual(s_mid["staging_count"], 1)
        self.assertEqual(s_mid["items"], [])

        # Advance 11s quiet -> settled into digest
        t_mono["v"] += 11.0
        t_wall["v"] += 11.0
        s_ready = mon.get_summary()
        self.assertTrue(s_ready["digest_id"].startswith("inbox-1-"))
        self.assertEqual(len(s_ready["items"]), 1)
        self.assertEqual(s_ready["items"][0]["app"], "WeChat")
        self.assertTrue(s_ready["items"][0]["flashed"])
        self.assertEqual(s_ready["items"][0]["notes"], 1)
        self.assertNotIn("sk-1234567890abcdefghijklmnop", s_ready["items"][0]["preview"])

        # HTTP routes require Bearer token and reject unknown keys
        tok = "test-inbox-bearer-token-1234567890"
        with patch.object(taskbar_inbox, "_inbox_singleton", mon):
            app = pcm.create_secured_asgi_app(tok, port=18900)
            client = TestClient(app, base_url="http://127.0.0.1:18900")
            headers = {"Authorization": f"Bearer {tok}"}

            r_unauth = client.get("/desktop/inbox")
            self.assertEqual(r_unauth.status_code, 401)

            r_get = client.get("/desktop/inbox", headers=headers)
            self.assertEqual(r_get.status_code, 200)
            self.assertEqual(len(r_get.json()["items"]), 1)

            r_bad = client.post("/desktop/inbox/config", headers=headers, json={"bad_key": 1})
            self.assertEqual(r_bad.status_code, 400)

            r_null = client.post("/desktop/inbox/config", headers=headers, json={"enabled": None})
            self.assertEqual(r_null.status_code, 400)

            r_off = client.post("/desktop/inbox/config", headers=headers, json={"enabled": False})
            self.assertEqual(r_off.status_code, 200)
            self.assertFalse(r_off.json()["enabled"])

    def test_win32_shellhook_64bit_start_and_clean_stop(self):
        if os.name != "nt":
            self.skipTest("Windows-only Win32 SHELLHOOK test")
        mon = taskbar_inbox.TaskbarInboxMonitor(enable_win32_hook=True)
        try:
            snap = mon.configure(enabled=True)
            self.assertTrue(snap["enabled"])
            self.assertEqual(snap["error"], "")
            self.assertGreater(mon._hook_hwnd, 0)
            self.assertIsNotNone(mon._thread)
            self.assertTrue(mon._thread.is_alive())
        finally:
            snap_off = mon.configure(enabled=False)
        self.assertFalse(snap_off["enabled"])
        self.assertEqual(mon._hook_hwnd, 0)
        self.assertIsNone(mon._thread)


if __name__ == "__main__":
    unittest.main()

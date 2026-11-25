"""Unit & ASGI route tests for Phase 1 ② Host 18900 Desktop Rhythm Probe (zero screenshots, privacy-safe)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import pc_control_mcp as pcm
from pc_mcp_modules import audio_bridge, security, winapi


class _FakeUser32ForRhythm:
    def __init__(self, *, last_input_ms: int = 0, ok_input: bool = True):
        self.last_input_ms = last_input_ms
        self.ok_input = ok_input
        self.iconic = False
        self.zoomed = False
        self.style = 0
        self.pid = 0
        self.fg_hwnd = 0
        self.win_rect = (0, 0, 1920, 1080)
        self.mon_rect = (0, 0, 1920, 1080)
        self.work_rect = None
        self.hmon = 101

    def GetLastInputInfo(self, ptr):
        if not self.ok_input:
            return 0
        ptr._obj.dwTime = self.last_input_ms & 0xFFFFFFFF
        return 1

    def IsIconic(self, _hwnd):
        return 1 if self.iconic else 0

    def IsZoomed(self, _hwnd):
        return 1 if self.zoomed else 0

    def GetWindowLongW(self, _hwnd, _idx):
        return self.style

    def GetWindowThreadProcessId(self, _hwnd, pid_ptr):
        if pid_ptr is not None:
            pid_ptr._obj.value = self.pid
        return 1

    def GetForegroundWindow(self):
        return self.fg_hwnd

    def GetWindowRect(self, _hwnd, ptr):
        l, t, r, b = self.win_rect
        ptr._obj.left = l
        ptr._obj.top = t
        ptr._obj.right = r
        ptr._obj.bottom = b
        return 1

    def MonitorFromWindow(self, _hwnd, _flags):
        return self.hmon

    def GetMonitorInfoW(self, _hmon, ptr):
        l, t, r, b = self.mon_rect
        mi = ptr._obj
        mi.rcMonitor.left = l
        mi.rcMonitor.top = t
        mi.rcMonitor.right = r
        mi.rcMonitor.bottom = b
        if self.work_rect is not None:
            wl, wt, wr, wb = self.work_rect
            mi.rcWork.left = wl
            mi.rcWork.top = wt
            mi.rcWork.right = wr
            mi.rcWork.bottom = wb
        return 1


class _FakeKernel32ForRhythm:
    def __init__(self, tick64: int):
        self.tick64 = tick64

    def GetTickCount64(self):
        return self.tick64


class TestDesktopRhythmProbe(unittest.TestCase):
    def test_idle_seconds_overflow_safe_across_32bit_wrap(self):
        # 32-bit tick counter wrapped: tick64 = 0x1_0000_0000 + 5000, last input was 0xFFFF_EC78 (-5000ms before wrap)
        u32 = _FakeUser32ForRhythm(last_input_ms=0xFFFF_EC78)
        k32 = _FakeKernel32ForRhythm(tick64=0x1_0000_0000 + 5000)
        with patch.object(pcm, "_user32", u32), patch.object(pcm, "_kernel32", k32):
            idle = winapi._get_user_idle_seconds()
        self.assertAlmostEqual(idle, 10.0, places=2)

        # Normal non-wrapped tick
        u32_n = _FakeUser32ForRhythm(last_input_ms=100_000)
        k32_n = _FakeKernel32ForRhythm(tick64=165_500)
        with patch.object(pcm, "_user32", u32_n), patch.object(pcm, "_kernel32", k32_n):
            self.assertAlmostEqual(winapi._get_user_idle_seconds(), 65.5, places=2)

        # Timer granularity skew: dwTime slightly ahead of GetTickCount64 by 16ms MUST return 0.0 (not 49.7 days!)
        u32_skew = _FakeUser32ForRhythm(last_input_ms=100_016)
        k32_skew = _FakeKernel32ForRhythm(tick64=100_000)
        with patch.object(pcm, "_user32", u32_skew), patch.object(pcm, "_kernel32", k32_skew):
            self.assertEqual(winapi._get_user_idle_seconds(), 0.0)

        # API error fails soft to 0.0 (conservative: never claims user is idle on error)
        u32_err = _FakeUser32ForRhythm(ok_input=False)
        with patch.object(pcm, "_user32", u32_err), patch.object(pcm, "_kernel32", k32_n):
            self.assertEqual(winapi._get_user_idle_seconds(), 0.0)

    def test_fullscreen_detection_excludes_shell_and_minimized(self):
        u32 = _FakeUser32ForRhythm()
        with patch.object(pcm, "_user32", u32), patch.object(
            winapi, "_get_window_class_name", return_value="Chrome_WidgetWin_1"
        ), patch.object(winapi, "_is_window_cloaked", return_value=False):
            # Fullscreen 1920x1080 on 1920x1080 monitor
            self.assertTrue(winapi._is_window_fullscreen(1234))

            # Non-fullscreen window
            u32.win_rect = (100, 100, 1400, 900)
            self.assertFalse(winapi._is_window_fullscreen(1234))

            # Minimized window is never fullscreen
            u32.win_rect = (0, 0, 1920, 1080)
            u32.iconic = True
            self.assertFalse(winapi._is_window_fullscreen(1234))
            u32.iconic = False

        # Cloaked background UWP window is never fullscreen
        with patch.object(pcm, "_user32", u32), patch.object(
            winapi, "_get_window_class_name", return_value="ApplicationFrameWindow"
        ), patch.object(winapi, "_is_window_cloaked", return_value=True):
            self.assertFalse(winapi._is_window_fullscreen(1234))

        # Desktop shell windows (Progman / WorkerW / Shell_TrayWnd) are excluded
        for shell_cls in ("Progman", "WorkerW", "Shell_TrayWnd"):
            with patch.object(pcm, "_user32", u32), patch.object(
                winapi, "_get_window_class_name", return_value=shell_cls
            ), patch.object(winapi, "_is_window_cloaked", return_value=False):
                self.assertFalse(winapi._is_window_fullscreen(1234), f"{shell_cls} must not be fullscreen")

    def test_fullscreen_detection_distinguishes_maximized_desktop_apps_from_true_fullscreen(self):
        # Standard maximized desktop window style: WS_VISIBLE | WS_MAXIMIZE | WS_CAPTION | WS_SYSMENU | WS_THICKFRAME
        ws_maximized_caption = 0x17CF0000
        # Borderless fullscreen popup style (no WS_CAPTION)
        ws_borderless_popup = 0x96000000
        # Partial WS_BORDER (0x00800000) WITHOUT WS_DLGFRAME (0x00400000) is NOT WS_CAPTION (0x00C00000)
        ws_border_only = 0x11800000

        u32 = _FakeUser32ForRhythm()
        u32.win_rect = (-8, -8, 1928, 1088)
        u32.mon_rect = (0, 0, 1920, 1080)
        u32.zoomed = True
        u32.style = ws_maximized_caption
        u32.pid = 4242

        with patch.object(pcm, "_user32", u32), patch.object(
            winapi, "_get_window_class_name", return_value="Chrome_WidgetWin_1"
        ), patch.object(winapi, "_is_window_cloaked", return_value=False), patch.object(
            winapi, "_get_process_name_by_pid", return_value="code.exe"
        ):
            # 1. Maximized VS Code on auto-hide taskbar monitor -> NOT fullscreen
            self.assertFalse(winapi._is_window_fullscreen(1234, process_name="code.exe"))
            # Also works via PID lookup when process_name is omitted
            self.assertFalse(winapi._is_window_fullscreen(1234))

            # 2. Maximized Chrome on taskbar-less negative-coordinate secondary monitor -> NOT fullscreen
            u32.win_rect = (-1928, -8, 8, 1088)
            u32.mon_rect = (-1920, 0, 0, 1080)
            self.assertFalse(winapi._is_window_fullscreen(1234, process_name="chrome.exe"))

            # 3. F11 browser / fullscreen video player drops WS_CAPTION and clears IsZoomed -> TRUE fullscreen
            u32.win_rect = (0, 0, 1920, 1080)
            u32.mon_rect = (0, 0, 1920, 1080)
            u32.zoomed = False
            u32.style = ws_borderless_popup
            self.assertTrue(winapi._is_window_fullscreen(1234, process_name="chrome.exe"))
            self.assertTrue(winapi._is_window_fullscreen(1234, process_name="PotPlayerMini64.exe"))

            # 4. Borderless fullscreen app keeping WS_BORDER only (missing WS_DLGFRAME) even if zoomed -> TRUE fullscreen
            u32.zoomed = True
            u32.style = ws_border_only
            self.assertTrue(winapi._is_window_fullscreen(1234, process_name="chrome.exe"))

            # 5. Borderless/exclusive fullscreen game -> TRUE fullscreen
            u32.zoomed = False
            u32.style = ws_borderless_popup
            self.assertTrue(winapi._is_window_fullscreen(1234, process_name="GenshinImpact.exe"))

            # 6. Game process covering monitor even with WS_CAPTION + IsZoomed -> STILL TRUE fullscreen
            u32.zoomed = True
            u32.style = ws_maximized_caption
            self.assertTrue(winapi._is_window_fullscreen(1234, process_name="GenshinImpact.exe"))

            # 6b. Signed negative 32-bit LONG returned by real Win32 GetWindowLongW (WS_POPUP bit 31 set)
            # Borderless popup (-1778384896 == 0x96000000) -> TRUE fullscreen
            u32.zoomed = False
            u32.style = -1778384896
            self.assertTrue(winapi._is_window_fullscreen(1234, process_name="chrome.exe"))

            # 6c. Auto-hide taskbar (rcWork inset 2px) vs visible taskbar (rcWork inset 48px):
            # Auto-hide taskbar with normal maximized VS Code -> NOT fullscreen
            u32.zoomed = True
            u32.style = ws_maximized_caption
            u32.win_rect = (-8, -8, 1928, 1086)
            u32.mon_rect = (0, 0, 1920, 1080)
            u32.work_rect = (0, 0, 1920, 1078)
            self.assertFalse(winapi._is_window_fullscreen(1234, process_name="code.exe"))

            # Visible taskbar (48px) covered end-to-end by an unlisted fullscreen app/UWP window -> TRUE fullscreen
            u32.win_rect = (0, 0, 1920, 1080)
            u32.work_rect = (0, 0, 1920, 1032)
            self.assertTrue(winapi._is_window_fullscreen(1234, process_name="ApplicationFrameHost.exe"))
            u32.work_rect = None

        # PID lookup for maximized game process when process_name is omitted -> TRUE fullscreen
        with patch.object(pcm, "_user32", u32), patch.object(
            winapi, "_get_window_class_name", return_value="UnityWndClass"
        ), patch.object(winapi, "_is_window_cloaked", return_value=False), patch.object(
            winapi, "_get_process_name_by_pid", return_value="GenshinImpact.exe"
        ):
            self.assertTrue(winapi._is_window_fullscreen(1234))

        # 7. End-to-end snapshot check: maximized VS Code reports is_fullscreen=False, maximized game reports True
        u32.fg_hwnd = 777
        u32.zoomed = True
        u32.style = ws_maximized_caption
        u32.win_rect = (0, 0, 1920, 1080)
        u32.mon_rect = (0, 0, 1920, 1080)
        trk = winapi.DesktopRhythmTracker()
        with patch.object(pcm, "_user32", u32), patch.object(
            pcm, "_get_user_idle_seconds", return_value=150.0
        ), patch.object(winapi, "_is_window_cloaked", return_value=False), patch.object(
            winapi,
            "_get_window_info_by_hwnd",
            return_value={
                "hwnd": 777,
                "pid": 4242,
                "process": "code.exe",
                "class": "Chrome_WidgetWin_1",
                "title": "main.py - VS Code",
            },
        ), patch.object(winapi, "_get_window_class_name", return_value="Chrome_WidgetWin_1"):
            snap_code = winapi.get_desktop_rhythm_snapshot(tracker=trk)
        self.assertFalse(snap_code["is_fullscreen"])
        self.assertEqual(snap_code["activity_category"], "coding")

        with patch.object(pcm, "_user32", u32), patch.object(
            pcm, "_get_user_idle_seconds", return_value=150.0
        ), patch.object(winapi, "_is_window_cloaked", return_value=False), patch.object(
            winapi,
            "_get_window_info_by_hwnd",
            return_value={
                "hwnd": 777,
                "pid": 4242,
                "process": "GenshinImpact.exe",
                "class": "UnityWndClass",
                "title": "原神",
            },
        ), patch.object(winapi, "_get_window_class_name", return_value="UnityWndClass"):
            snap_game = winapi.get_desktop_rhythm_snapshot(tracker=trk)
        self.assertTrue(snap_game["is_fullscreen"])
        self.assertEqual(snap_game["activity_category"], "game")

    def test_activity_classification_and_strict_title_sanitization(self):
        self.assertEqual(winapi.classify_desktop_activity("Code.exe", "", "main.py - VS Code"), "coding")
        self.assertEqual(winapi.classify_desktop_activity("chrome.exe", "", "GitHub - Chrome"), "browser")
        self.assertEqual(winapi.classify_desktop_activity("chrome.exe", "", "腾讯会议 - Chrome"), "meeting")
        self.assertEqual(winapi.classify_desktop_activity("Zoom.exe", "", "Zoom Meeting"), "meeting")
        self.assertEqual(winapi.classify_desktop_activity("GenshinImpact.exe", "", "原神"), "game")
        self.assertEqual(winapi.classify_desktop_activity("PotPlayerMini64.exe", "", "movie.mkv"), "media")
        self.assertEqual(winapi.classify_desktop_activity("WeChat.exe", "", "微信"), "chat")
        self.assertEqual(winapi.classify_desktop_activity("explorer.exe", "Progman", ""), "desktop")

        # Sensitive titles MUST be replaced with safe category labels (zero leakage)
        self.assertEqual(winapi.sanitize_window_title("powershell.exe", "ssh root@prod - sk-secret1234567890", "coding"), "终端窗口")
        self.assertEqual(winapi.sanitize_window_title("wt.exe", "Administrator: .env", "coding"), "终端窗口")
        self.assertEqual(winapi.sanitize_window_title("KeePassXC.exe", "MyPasswords.kdbx", "other"), "密码管理工具")
        self.assertEqual(winapi.sanitize_window_title("chrome.exe", "隐私窗口 - InPrivate - Edge", "browser"), "浏览器窗口")
        self.assertEqual(winapi.sanitize_window_title("chrome.exe", "招商银行个人网银转账 - Chrome", "browser"), "浏览器窗口")
        self.assertEqual(winapi.sanitize_window_title("chrome.exe", "MetaMask Wallet Login - Chrome", "browser"), "浏览器窗口")
        self.assertEqual(winapi.sanitize_window_title("code.exe", ".env - xiaozhi-server - VS Code", "coding"), "代码与开发窗口")
        self.assertEqual(winapi.sanitize_window_title("WeChat.exe", "张三 - 微信", "chat"), "聊天窗口")
        self.assertEqual(winapi.sanitize_window_title("chrome.exe", "user@company.com - Mail", "browser"), "浏览器窗口")
        self.assertEqual(winapi.sanitize_window_title("notepad.exe", r"C:\Users\Alice\notes.txt", "other"), "常规窗口")
        self.assertEqual(winapi.sanitize_window_title("chrome.exe", "A" * 130, "browser"), "浏览器窗口")

        # Safe normal title passes cleanly
        safe = winapi.sanitize_window_title("Code.exe", "README.md - xiaozhi-server - Visual Studio Code", "coding")
        self.assertIn("README.md", safe)
        self.assertLessEqual(len(safe), 60)

    def test_desktop_rhythm_tracker_dense_and_sparse_transitions(self):
        # 1. Sparse 15-minute polling: 27m active work -> 3m pause => just_paused=True
        trk = winapi.DesktopRhythmTracker()
        s0 = trk.sample(2.0, now_mono=1000.0)
        self.assertFalse(s0["just_paused"])
        self.assertFalse(s0["returned_from_away"])

        s1 = trk.sample(5.0, now_mono=1900.0)  # +15 min active
        self.assertGreaterEqual(s1["active_streak_seconds"], 890.0)
        self.assertFalse(s1["just_paused"])

        s2 = trk.sample(180.0, now_mono=2800.0)  # +12 min active then 3 min pause
        self.assertGreaterEqual(s2["active_streak_seconds"], 1500.0)
        self.assertTrue(s2["just_paused"])

        # Idle exceeds 8 min -> streak breaks and just_paused turns False
        s3 = trk.sample(600.0, now_mono=3220.0)
        self.assertFalse(s3["just_paused"])
        self.assertEqual(s3["active_streak_seconds"], 0.0)

        # 2. Away >= 45 min -> return => returned_from_away=True for 5 min window
        s4 = trk.sample(3000.0, now_mono=6220.0)
        self.assertFalse(s4["returned_from_away"])
        s5 = trk.sample(3.0, now_mono=6250.0)
        self.assertTrue(s5["returned_from_away"])
        # Still True within 5-min hold window
        s6 = trk.sample(10.0, now_mono=6400.0)
        self.assertTrue(s6["returned_from_away"])
        # Expires after 5-min hold window
        s7 = trk.sample(10.0, now_mono=6600.0)
        self.assertFalse(s7["returned_from_away"])

        # 3. 90s..120s transition dead-zone must NOT drop pre-pause work streak!
        trk2 = winapi.DesktopRhythmTracker()
        trk2.sample(2.0, now_mono=100.0)
        trk2.sample(5.0, now_mono=1000.0)    # +15 min active (~897s)
        s_mid = trk2.sample(100.0, now_mono=1900.0)  # +13.3 min active then 100s idle (90..120s gap)
        self.assertFalse(s_mid["just_paused"])
        self.assertGreaterEqual(s_mid["active_streak_seconds"], 1500.0)
        s_pause = trk2.sample(130.0, now_mono=1930.0)  # 30s later -> now in 2..8m pause window!
        self.assertTrue(s_pause["just_paused"])

    def test_desktop_rhythm_http_route_auth_and_payload(self):
        class _MiniMCP:
            def __init__(self):
                self.routes = []

            def custom_route(self, path, methods=None):
                def _dec(fn):
                    self.routes.append(Route(path, fn, methods=methods or ["GET"]))
                    return fn
                return _dec

        fake_player = SimpleNamespace(status=lambda: {"ok": True, "playing": False, "queued": 0})
        mini = _MiniMCP()
        audio_bridge.register_http_routes(mini, fake_player)
        base_app = Starlette(routes=mini.routes)
        token = "test-desktop-rhythm-token-1234567890"
        authed_app = security._LocalBearerAndHostMiddleware(
            base_app,
            token=token,
            allowed_hosts=["127.0.0.1:18900", "localhost:18900", "testserver"],
            allowed_origins=["http://127.0.0.1:18900"],
        )

        with tempfile.TemporaryDirectory() as td:
            active_dir = Path(td)
            # Real L3 dsh/workbuddy schema uses formatted `finished_at` (not `finished_ts`)
            (active_dir / "task_fin_001.json").write_text(
                json.dumps(
                    {
                        "id": "task_fin_001",
                        "status": "completed",
                        "backend": "dsh",
                        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 30.0)),
                    }
                ),
                encoding="utf-8",
            )
            # Startup-reaped zombie must NOT be reported as a fresh recently_finished completion
            (active_dir / "task_zombie.json").write_text(
                json.dumps(
                    {
                        "id": "task_zombie",
                        "status": "error",
                        "backend": "dsh",
                        "reaped_zombie": True,
                        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 5.0)),
                    }
                ),
                encoding="utf-8",
            )
            # Idle historical Antigravity dispatched record must NOT permanently inflate running_count
            (active_dir / "task_agy_old.json").write_text(
                json.dumps(
                    {
                        "id": "task_agy_old",
                        "status": "dispatched",
                        "backend": "antigravity",
                        "conversation_id": "conv-old-done",
                    }
                ),
                encoding="utf-8",
            )
            fake_agy = SimpleNamespace(_is_dispatched_conversation_busy=lambda _cid, _fp: False)
            fake_l3 = SimpleNamespace(ACTIVE_DIR=active_dir, antigravity=fake_agy)
            fake_snap = {
                "ok": True,
                "idle_seconds": 150.0,
                "is_fullscreen": False,
                "activity_category": "coding",
                "process_name": "code",
                "safe_title": "代码与开发窗口",
                "active_streak_seconds": 1620.0,
                "just_paused": True,
                "returned_from_away": False,
            }

            with patch.object(pcm, "get_desktop_rhythm_snapshot", return_value=fake_snap), patch.object(
                pcm, "l3_backends", fake_l3
            ):
                client = TestClient(authed_app)
                # 1. Unauthenticated request MUST be rejected with 401
                r_unauth = client.get("/desktop/rhythm")
                self.assertEqual(r_unauth.status_code, 401)

                # 2. Authenticated request returns structured read-only rhythm + L3 summary
                r_ok = client.get(
                    "/desktop/rhythm",
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(r_ok.status_code, 200)
                body = r_ok.json()
                self.assertTrue(body["ok"])
                self.assertEqual(body["activity_category"], "coding")
                self.assertTrue(body["just_paused"])
                self.assertFalse(body["is_fullscreen"])
                self.assertFalse(body["pc_audio_busy"])
                self.assertEqual(body["l3_status"]["running_count"], 0)
                self.assertEqual(body["l3_status"]["recently_finished"]["id"], "task_fin_001")


if __name__ == "__main__":
    unittest.main(verbosity=2)

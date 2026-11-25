"""Host unit tests for Phase 2 privacy-guarded screen peek, desktop companion widget,
speaker model downloader allowlist, and script encoding invariants."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

PC_MCP_DIR = Path(__file__).resolve().parent
SERVER_DIR = PC_MCP_DIR.parent
if str(PC_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(PC_MCP_DIR))

import pc_control_mcp as mcp_mod  # noqa: E402
from pc_mcp_modules import audio_bridge, security, vision, winapi  # noqa: E402


def _load_script_module(rel_path: str, name: str):
    p = SERVER_DIR / rel_path
    spec = importlib.util.spec_from_file_location(name, str(p))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class TestPhase2ScreenPeekAndWidget(unittest.TestCase):
    def test_screen_peek_privacy_blocks_sensitive_apps_and_titles(self):
        blocked_cases = [
            {"process": "KeePassXC.exe", "class": "Qt5QWindow", "title": "Main.kdbx"},
            {"process": "pwsh.exe", "class": "CASCADIA_HOSTING_WINDOW_CLASS", "title": "PowerShell"},
            {"process": "mstsc.exe", "class": "TscShellContainerClass", "title": "prod-server"},
            {"process": "WeChat.exe", "class": "WeChatMainWndForPC", "title": "微信"},
            {"process": "chrome.exe", "class": "Chrome_WidgetWin_1", "title": "招商银行网银登录 - Chrome"},
            {"process": "msedge.exe", "class": "Chrome_WidgetWin_1", "title": "InPrivate - Edge"},
            {"process": "Code.exe", "class": "Chrome_WidgetWin_1", "title": ".env - xiaozhi-server"},
            {},
            {"process": "", "class": "", "title": ""},
            {"process": "LockApp.exe", "class": "Windows.UI.Core.CoreWindow", "title": "Windows 默认锁屏界面"},
            {"process": "explorer.exe", "class": "Progman", "title": "Program Manager"},
            {"process": "Feishu.exe", "class": "Chrome_WidgetWin_1", "title": "飞书"},
        ]
        for fg in blocked_cases:
            allowed, reason = winapi.check_screen_peek_privacy(fg)
            self.assertFalse(allowed, f"Expected privacy block for {fg}")
            self.assertTrue(reason)

        allowed_ok, reason_ok = winapi.check_screen_peek_privacy(
            {"process": "Code.exe", "class": "Chrome_WidgetWin_1", "title": "app.py - xiaozhi-server"}
        )
        self.assertTrue(allowed_ok)
        self.assertEqual(reason_ok, "")

    def test_peek_active_window_never_captures_when_privacy_blocked_and_downscales(self):
        grab_calls = []

        class _FakeGrab:
            @staticmethod
            def grab(bbox=None, all_screens=True):
                grab_calls.append((bbox, all_screens))
                return Image.new("RGB", (2000, 1200), (40, 40, 60))

        # 1. Privacy blocked -> ZERO ImageGrab calls!
        with patch.object(mcp_mod, "_get_foreground_window_info", return_value={"hwnd": 100, "process": "pwsh.exe", "class": "C", "title": "Terminal"}), \
             patch.object(mcp_mod, "ImageGrab", _FakeGrab), \
             patch.object(mcp_mod, "_call_vision_llm", return_value="should not run"):
            res_blk = vision.peek_active_window("看看终端")
            self.assertFalse(res_blk["ok"])
            self.assertTrue(res_blk["blocked"])
            self.assertEqual(grab_calls, [])

        # 2. Allowed window -> captures clipped window bbox and scales long edge <= 1024px
        seen_sizes = []

        def _fake_vlm(_prompt, pil_img, json_mode=False):
            seen_sizes.append(pil_img.size)
            return "你正在 VS Code 里调试单元测试，看到 sk-secret1234567890abcdef1234567890。"

        with patch.object(mcp_mod, "_get_foreground_window_info", return_value={"hwnd": 200, "process": "Code.exe", "class": "Chrome_WidgetWin_1", "title": "voiceprint_router.py - VS Code"}), \
             patch.object(mcp_mod, "_get_active_window_clipped_rect", return_value=(-1920, 0, -400, 900)), \
             patch.object(mcp_mod, "ImageGrab", _FakeGrab), \
             patch.object(mcp_mod, "_call_vision_llm", side_effect=_fake_vlm):
            res_ok = vision.peek_active_window("看看这段代码")
            self.assertTrue(res_ok["ok"])
            self.assertFalse(res_ok["blocked"])
            self.assertEqual(grab_calls, [((-1920, 0, -400, 900), True)])
            self.assertEqual(len(seen_sizes), 1)
            self.assertLessEqual(max(seen_sizes[0]), 1024)
            self.assertIn("VS Code", res_ok["summary"])
            self.assertNotIn("sk-secret1234567890abcdef1234567890", res_ok["summary"])

        # 3. Missing window rect -> fail-closed (NEVER fall back to full-monitor capture!)
        grab_calls.clear()
        with patch.object(mcp_mod, "_get_foreground_window_info", return_value={"hwnd": 201, "process": "Code.exe", "class": "Chrome_WidgetWin_1", "title": "app.py"}), \
             patch.object(mcp_mod, "_get_active_window_clipped_rect", return_value=None), \
             patch.object(mcp_mod, "ImageGrab", _FakeGrab), \
             patch.object(mcp_mod, "_call_vision_llm", return_value="should not run"):
            res_norect = vision.peek_active_window("看看")
            self.assertFalse(res_norect["ok"])
            self.assertTrue(res_norect["blocked"])
            self.assertEqual(grab_calls, [])

    def test_host_screen_peek_route_mode_and_validation(self):
        class _FakeMCP:
            def __init__(self):
                self.routes = {}

            def custom_route(self, path, methods=None):
                def _dec(fn):
                    self.routes[(path, tuple(methods or []))] = fn
                    return fn
                return _dec

        class _FakeReq:
            def __init__(self, body_bytes: bytes):
                self._raw = body_bytes

            async def body(self):
                return self._raw

        fake_mcp = _FakeMCP()
        audio_bridge.register_http_routes(fake_mcp, audio_bridge._WinMMPlayer())
        route = fake_mcp.routes[("/desktop/screen-peek", ("POST",))]

        # 1. Blocked in `off` mode -> 403
        with patch.object(mcp_mod, "_check_permission", return_value="当前处于只聊挡，已拒绝"):
            resp_off = asyncio.run(route(_FakeReq(b"{}")))
            self.assertEqual(resp_off.status_code, 403)

        # 2. Unknown JSON key -> 400
        with patch.object(mcp_mod, "_check_permission", return_value=None):
            resp_bad = asyncio.run(route(_FakeReq(b'{"evil": 1}')))
            self.assertEqual(resp_bad.status_code, 400)

        # 3. Privacy-blocked window -> 403
        with patch.object(mcp_mod, "_check_permission", return_value=None), \
             patch.object(mcp_mod, "peek_active_window", return_value={"ok": False, "blocked": True, "summary": "隐私拦截"}):
            resp_priv = asyncio.run(route(_FakeReq('{"question":"看看"}'.encode("utf-8"))))
            self.assertEqual(resp_priv.status_code, 403)

        # 4. Success -> 200
        with patch.object(mcp_mod, "_check_permission", return_value=None), \
             patch.object(mcp_mod, "peek_active_window", return_value={"ok": True, "blocked": False, "summary": "代码窗口"}):
            resp_ok = asyncio.run(route(_FakeReq('{"question":"看看"}'.encode("utf-8"))))
            self.assertEqual(resp_ok.status_code, 200)

    def test_desktop_companion_widget_helpers_rendering_and_loopback_guard(self):
        widget = _load_script_module("scripts/desktop_companion.py", "_test_desktop_companion")
        with tempfile.TemporaryDirectory() as td:
            vdir = Path(td) / "voices"
            pdir = vdir / "raiden_mei" / "portraits"
            pdir.mkdir(parents=True)
            img_happy = pdir / "happy.png"
            img_default = pdir / "default.png"
            Image.new("RGBA", (128, 128), (120, 80, 220, 255)).save(img_happy)
            Image.new("RGBA", (128, 128), (80, 80, 140, 255)).save(img_default)

            # Exact emotion match, default fallback, and traversal safety
            self.assertEqual(widget.resolve_portrait_path(vdir, "raiden_mei", "happy"), img_happy.resolve())
            self.assertEqual(widget.resolve_portrait_path(vdir, "raiden_mei", "shy"), img_default.resolve())
            self.assertIsNone(widget.resolve_portrait_path(vdir, "../../etc", "calm"))

            # Portrait & all 7 vector emotion avatars render cleanly
            im_p = widget.render_avatar_image(64, "happy", portrait_path=img_happy, speaking=True)
            self.assertEqual(im_p.size, (64, 64))
            self.assertEqual(im_p.mode, "RGBA")
            for emo in widget.VALID_EMOTIONS:
                im_v = widget.render_avatar_image(64, emo, portrait_path=None, speaking=(emo == "gentle"))
                self.assertEqual(im_v.size, (64, 64))

            # Bubble priority picks newest timestamp
            tag, txt = widget.choose_display_bubble(
                {
                    "subtitle": "旧字幕",
                    "subtitle_ts": 100.0,
                    "screen_peek_bubble": {"text": "新看屏回应", "ts": 200.0},
                }
            )
            self.assertEqual(tag, "浅看屏")
            self.assertEqual(txt, "新看屏回应")

            # Position persistence & single-instance PID lock
            pos_file = Path(td) / "widget_pos.json"
            widget.save_widget_position(pos_file, 1200, 820)
            self.assertEqual(widget.load_widget_position(pos_file), (1200, 820))

            pid_file = Path(td) / "widget.pid"
            self.assertTrue(widget.acquire_single_instance_pid(pid_file))
            widget.release_single_instance_pid(pid_file)
            self.assertFalse(pid_file.exists())

            # Loopback-only guard rejects non-localhost console URLs
            with self.assertRaises(ValueError):
                widget.WidgetApiClient(base_url="http://192.168.1.9:18080")
            cli = widget.WidgetApiClient(base_url="http://127.0.0.1:18080")
            cli.close()

    def test_speaker_model_downloader_url_allowlist_and_script_encodings(self):
        dl = _load_script_module("scripts/download_speaker_model.py", "_test_download_speaker_model")
        self.assertTrue(dl.is_allowed_download_url(dl.DEFAULT_URLS[0]))
        self.assertTrue(dl.is_allowed_download_url(dl.DEFAULT_URLS[1]))
        self.assertFalse(dl.is_allowed_download_url("http://github.com/file.onnx"))
        self.assertFalse(dl.is_allowed_download_url("https://evil.example.com/file.onnx"))
        self.assertFalse(dl.is_allowed_download_url("https://github.com.evil.com/file.onnx"))

        # Encoding iron laws: .bat = pure ASCII + CRLF; .ps1 = UTF-8 BOM + CRLF
        bat_bytes = (SERVER_DIR / "桌角陪伴小窗.bat").read_bytes()
        self.assertTrue(all(b < 128 for b in bat_bytes), "桌角陪伴小窗.bat must be pure ASCII")
        self.assertIn(b"\r\n", bat_bytes)
        self.assertNotIn(b"\n", bat_bytes.replace(b"\r\n", b""))

        for ps1_rel in ("data/start_companion_widget.ps1", "data/stop_all.ps1"):
            ps_bytes = (SERVER_DIR / ps1_rel).read_bytes()
            self.assertTrue(ps_bytes.startswith(b"\xef\xbb\xbf"), f"{ps1_rel} must have UTF-8 BOM")
            self.assertIn(b"\r\n", ps_bytes)
            self.assertNotIn(b"\n", ps_bytes.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()

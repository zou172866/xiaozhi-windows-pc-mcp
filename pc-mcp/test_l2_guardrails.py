"""Unit tests for P0 #3 (L3 full-mode gate & safe default) and P0 #4 (L2 guardrails & sanitized audit log)
plus /l3/config restricted-field HTTP enforcement in pc_control_mcp.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import l3_backends
import pc_control_mcp as pcm

_ORIG_L2_AUDIT_LOG_FILE = pcm.L2_AUDIT_LOG_FILE


_BENIGN_FG = {
    "hwnd": 2001,
    "pid": 5432,
    "process": "notepad.exe",
    "class": "Notepad",
    "title": "无标题 - 记事本",
}


class TestL2GuardrailsAndL3Gate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.mode_file = self.tmp_root / "pc_control_mode.json"
        self.audit_file = self.tmp_root / "l2_audit.jsonl"
        self.l3_cfg_file = self.tmp_root / "l3_backend_config.json"

        self._patches = [
            patch.object(pcm, "MODE_FILE", self.mode_file),
            patch.object(pcm, "L2_AUDIT_LOG_FILE", self.audit_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _set_mode(self, mode_val) -> None:
        self.mode_file.write_text(json.dumps({"mode": mode_val}), encoding="utf-8")

    def _read_audit_records(self) -> list[dict]:
        if not self.audit_file.is_file():
            return []
        lines = [ln for ln in self.audit_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [json.loads(ln) for ln in lines]

    def test_host_private_state_paths_live_under_pc_mcp_state(self):
        expected_state = (MCP_DIR / "state").resolve()
        data_dir = (MCP_DIR.parent / "data").resolve()
        self.assertEqual(pcm.STATE_DIR.resolve(), expected_state)
        self.assertEqual(_ORIG_L2_AUDIT_LOG_FILE.resolve(), expected_state / "l2_audit.jsonl")
        self.assertFalse(_ORIG_L2_AUDIT_LOG_FILE.resolve().is_relative_to(data_dir))
        for attr in ("TASKS_ROOT", "PENDING_DIR", "ACTIVE_DIR", "HEARTBEAT_FILE", "LAST_GROUNDING_IMG"):
            p = getattr(pcm, attr).resolve()
            self.assertTrue(p.is_relative_to(expected_state), f"{attr}={p} must live under {expected_state}")
            self.assertFalse(p.is_relative_to(data_dir), f"{attr}={p} must NOT live under container-mounted data/")

    def test_default_mode_fallback_when_missing_or_corrupt(self):
        # 1. Missing mode file -> safe
        self.assertFalse(self.mode_file.exists())
        self.assertEqual(pcm._get_mode(), "safe")

        # 2. Corrupt JSON -> safe
        self.mode_file.write_text("{broken json", encoding="utf-8")
        self.assertEqual(pcm._get_mode(), "safe")

        # 3. Non-dict JSON -> safe
        for raw in ("[]", '"full"', "null", "123"):
            self.mode_file.write_text(raw, encoding="utf-8")
            self.assertEqual(pcm._get_mode(), "safe", f"Expected safe fallback for {raw}")

        # 4. Unrecognized/unsupported mode values -> safe
        for bad_mode in ("standard", "admin", "debug", "", None, 1, True):
            self._set_mode(bad_mode)
            self.assertEqual(pcm._get_mode(), "safe", f"Expected safe fallback for mode={bad_mode!r}")

        # 5. Valid modes normalize cleanly
        for raw_mode, expected in (("off", "off"), ("safe", "safe"), ("full", "full"), (" FULL ", "full")):
            self._set_mode(raw_mode)
            self.assertEqual(pcm._get_mode(), expected)

    def test_permission_matrix_and_l3_full_mode_gate(self):
        # Matrix: mode -> (L1_allowed, L2_allowed, L3_allowed)
        matrix = [
            ("off", False, False, False),
            ("safe", True, False, False),
            ("standard", True, False, False),
            ("full", True, True, True),
        ]
        for mode_name, exp_l1, exp_l2, exp_l3 in matrix:
            with patch.object(pcm, "_get_mode", return_value=mode_name):
                self.assertEqual(pcm._check_permission("L1") is None, exp_l1, f"L1 failed for mode={mode_name}")
                self.assertEqual(pcm._check_permission("L2") is None, exp_l2, f"L2 failed for mode={mode_name}")
                self.assertEqual(pcm._check_permission("L3") is None, exp_l3, f"L3 failed for mode={mode_name}")

        # End-to-end pc_agent_task blocked in off/safe/standard, allowed only in full
        for blocked_mode in ("off", "safe", "standard"):
            with (
                patch.object(pcm, "_get_mode", return_value=blocked_mode),
                patch.object(pcm.l3_backends, "dispatch_task") as m_disp,
                patch.object(pcm.l3_backends, "check_task_status") as m_chk,
            ):
                res_disp = pcm.pc_agent_task(action="dispatch", task_instruction="整理项目文件")
                res_chk = pcm.pc_agent_task(action="check_status")
                m_disp.assert_not_called()
                m_chk.assert_not_called()
                self.assertIn("18080", res_disp)
                self.assertIn("18080", res_chk)
                if blocked_mode != "off":
                    self.assertIn("全权操控挡", res_disp)
                    self.assertIn("full", res_disp)

        with (
            patch.object(pcm, "_get_mode", return_value="full"),
            patch.object(pcm.l3_backends, "dispatch_task", return_value="已派发任务") as m_disp,
            patch.object(pcm.l3_backends, "check_task_status", return_value="任务进行中") as m_chk,
        ):
            self.assertEqual(pcm.pc_agent_task(action="dispatch", task_instruction="整理项目文件"), "已派发任务")
            self.assertEqual(pcm.pc_agent_task(action="check_status"), "任务进行中")
            m_disp.assert_called_once_with("整理项目文件", follow_up_last=False)
            m_chk.assert_called_once()

        # L2 blocked in safe mode also records audit entries for both pc_ui_action & pc_inspect_window_ui
        self._set_mode("safe")
        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm.pyautogui, "hotkey") as m_hk,
        ):
            res = pcm.pc_ui_action(action="hotkey", hotkey="ctrl+c")
            self.assertIn("安全挡", res)
            m_hk.assert_not_called()
            res_insp = pcm.pc_inspect_window_ui(window_keyword="记事本")
            self.assertIn("安全挡", res_insp)
        recs = self._read_audit_records()
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["tool"], "pc_ui_action")
        self.assertEqual(recs[0]["outcome"], "blocked")
        self.assertEqual(recs[0]["reason"], "mode_blocked")
        self.assertEqual(recs[0]["mode"], "safe")
        self.assertEqual(recs[1]["tool"], "pc_inspect_window_ui")
        self.assertEqual(recs[1]["outcome"], "blocked")
        self.assertEqual(recs[1]["reason"], "mode_blocked")

    def test_type_text_missing_target_fails_closed_without_typing(self):
        self._set_mode("full")
        with (
            patch.object(pcm, "_smart_locate_target", return_value=None) as m_loc,
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm.pyautogui, "moveTo") as m_move,
            patch.object(pcm.pyautogui, "click") as m_click,
            patch.object(pcm.pyautogui, "hotkey") as m_hk,
            patch.object(pcm.pyautogui, "press") as m_press,
            patch.object(pcm.pyperclip, "copy") as m_copy,
        ):
            res = pcm.pc_ui_action(
                action="type_text",
                element_name="不存在的搜索框",
                text="rm -rf / # secret-never-type",
                press_enter=True,
            )
            self.assertIn("未能", res)
            self.assertIn("中止", res)
            m_loc.assert_called_once_with("不存在的搜索框")
            m_move.assert_not_called()
            m_click.assert_not_called()
            m_hk.assert_not_called()
            m_press.assert_not_called()
            m_copy.assert_not_called()

        raw_log = self.audit_file.read_text(encoding="utf-8")
        self.assertNotIn("secret-never-type", raw_log)
        recs = self._read_audit_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["action"], "type_text")
        self.assertEqual(recs[0]["outcome"], "blocked")
        self.assertEqual(recs[0]["reason"], "target_not_found")
        self.assertEqual(recs[0]["text_len"], len("rm -rf / # secret-never-type"))

    def test_hotkey_blacklist_blocks_dangerous_aliases_and_allows_benign(self):
        self._set_mode("full")
        dangerous_combos = [
            "win+r",
            "Win+R",
            "RWin+R",
            "LWin+R",
            "Windows+R",
            "WinLeft + R",
            "win_right+r",
            "win-left+r",
            "super+r",
            "meta+r",
            "cmd+r",
            "r+win",
            "win-r",
            "win＋r",
            "lwin+rwin+r",
            "win+x",
            "Windows + X",
            "meta+x",
            "x+rwin",
            "ctrl+shift+esc",
            "Shift+Ctrl+Escape",
            "Escape+Shift+Control",
            "lctrl+rshift+esc",
            "ctrl-shift-esc",
            "ctrl shift esc",
            "ctrl+alt+del",
            "Ctrl+Alt+Delete",
            "Alt+Control+Del",
            "win+l",
            "LWin+L",
            "win+s",
            "win+q",
            "win",
            "LWin",
            "ctrl+esc",
            "Control+Escape",
            # Extra/trailing keys and bogus keys appended to blacklisted combos (subset defense)
            "win+r+noop",
            "win+r+a",
            "win+shift+r",
            "ctrl+shift+esc+foo",
            "ctrl+shift+esc+a",
            "win+x+123",
            "ctrl+alt+del+a",
        ]
        unknown_key_combos = [
            "win+noop",
            "ctrl+boguskey",
            "foo+bar",
            "ctrl+123",
        ]
        benign_combos = [
            ("ctrl+c", ("ctrl", "c")),
            ("Ctrl+V", ("ctrl", "v")),
            ("ctrl+s", ("ctrl", "s")),
            ("ctrl+a", ("ctrl", "a")),
            ("ctrl+w", ("ctrl", "w")),
            ("alt+tab", ("alt", "tab")),
            ("win+d", ("win", "d")),
            ("space", ("space",)),
            ("enter", ("enter",)),
            ("esc", ("esc",)),
            ("ctrl+-", ("ctrl", "-")),
        ]

        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm.pyautogui, "hotkey") as m_hk,
        ):
            for combo in dangerous_combos:
                m_hk.reset_mock()
                res = pcm.pc_ui_action(action="hotkey", hotkey=combo)
                self.assertIn("拦截", res, f"Expected blacklist block for {combo!r}, got: {res}")
                m_hk.assert_not_called()

            for combo in unknown_key_combos:
                m_hk.reset_mock()
                res = pcm.pc_ui_action(action="hotkey", hotkey=combo)
                self.assertIn("无效", res, f"Expected invalid-key block for {combo!r}, got: {res}")
                m_hk.assert_not_called()

            for combo, expected_args in benign_combos:
                m_hk.reset_mock()
                res = pcm.pc_ui_action(action="hotkey", hotkey=combo)
                self.assertIn("已按下快捷键", res, f"Benign hotkey {combo!r} was unexpectedly blocked: {res}")
                m_hk.assert_called_once_with(*expected_args)

        recs = self._read_audit_records()
        self.assertEqual(len(recs), len(dangerous_combos) + len(unknown_key_combos) + len(benign_combos))
        bl_recs = [r for r in recs if r["reason"] == "hotkey_blacklisted"]
        inv_recs = [r for r in recs if r["reason"] == "invalid_hotkey_key"]
        allowed_recs = [r for r in recs if r["outcome"] == "allowed"]
        self.assertEqual(len(bl_recs), len(dangerous_combos))
        self.assertTrue(all(r["outcome"] == "blocked" for r in bl_recs))
        self.assertEqual(len(inv_recs), len(unknown_key_combos))
        self.assertTrue(all(r["outcome"] == "blocked" for r in inv_recs))
        self.assertEqual(len(allowed_recs), len(benign_combos))

    def test_foreground_terminal_and_run_dialog_refuse_keyboard_input(self):
        self._set_mode("full")
        blocked_windows = [
            {"hwnd": 1, "pid": 10, "process": "powershell.exe", "class": "ConsoleWindowClass", "title": "Windows PowerShell"},
            {"hwnd": 2, "pid": 11, "process": "pwsh.exe", "class": "ConsoleWindowClass", "title": "PowerShell 7"},
            {"hwnd": 3, "pid": 12, "process": "cmd.exe", "class": "ConsoleWindowClass", "title": "命令提示符"},
            {"hwnd": 4, "pid": 13, "process": "WindowsTerminal.exe", "class": "CASCADIA_HOSTING_WINDOW_CLASS", "title": "Terminal"},
            {"hwnd": 5, "pid": 14, "process": "wt.exe", "class": "CASCADIA_HOSTING_WINDOW_CLASS", "title": "Windows Terminal"},
            {"hwnd": 6, "pid": 15, "process": "conhost.exe", "class": "ConsoleWindowClass", "title": "cmd"},
            {"hwnd": 7, "pid": 16, "process": "OpenConsole.exe", "class": "PseudoConsoleWindow", "title": "Console"},
            {"hwnd": 8, "pid": 17, "process": "mintty.exe", "class": "mintty", "title": "MINGW64"},
            {"hwnd": 9, "pid": 18, "process": "wsl.exe", "class": "ConsoleWindowClass", "title": "Ubuntu"},
            {"hwnd": 10, "pid": 19, "process": "git-bash.exe", "class": "mintty", "title": "Git Bash"},
            # Elevated terminal where process name cannot be queried, caught by Win32 class name
            {"hwnd": 11, "pid": 20, "process": "", "class": "ConsoleWindowClass", "title": "管理员: 命令提示符"},
            {"hwnd": 12, "pid": 21, "process": "", "class": "CASCADIA_HOSTING_WINDOW_CLASS", "title": "管理员: Windows Terminal"},
            # Windows Run dialog (Chinese & English, with and without class)
            {"hwnd": 13, "pid": 22, "process": "explorer.exe", "class": "#32770", "title": "运行"},
            {"hwnd": 14, "pid": 23, "process": "explorer.exe", "class": "#32770", "title": "Run"},
            {"hwnd": 15, "pid": 24, "process": "explorer.exe", "class": "", "title": "运行"},
            {"hwnd": 16, "pid": 25, "process": "taskmgr.exe", "class": "TaskManagerWindow", "title": "任务管理器"},
        ]

        with (
            patch.object(pcm.pyautogui, "hotkey") as m_hk,
            patch.object(pcm.pyautogui, "press") as m_press,
            patch.object(pcm.pyperclip, "copy") as m_copy,
            patch.object(pcm.time, "sleep"),
        ):
            for fg in blocked_windows:
                with patch.object(pcm, "_get_foreground_window_info", return_value=fg):
                    m_hk.reset_mock()
                    m_press.reset_mock()
                    m_copy.reset_mock()

                    res_type = pcm.pc_ui_action(action="type_text", text="calc.exe", press_enter=True)
                    self.assertIn("安全护栏已拦截", res_type, f"type_text was not blocked for {fg}")
                    m_copy.assert_not_called()
                    m_hk.assert_not_called()
                    m_press.assert_not_called()

                    res_hk = pcm.pc_ui_action(action="hotkey", hotkey="ctrl+v")
                    self.assertIn("安全护栏已拦截", res_hk, f"hotkey was not blocked for {fg}")
                    m_hk.assert_not_called()

        # Also verify type_text with located element still checks foreground AFTER clicking and refuses typing into terminal
        ps_fg = {"hwnd": 99, "pid": 99, "process": "powershell.exe", "class": "ConsoleWindowClass", "title": "PowerShell"}
        with (
            patch.object(pcm, "_smart_locate_target", return_value=(400, 400, "视觉定位「终端」")),
            patch.object(pcm, "_get_foreground_window_info", return_value=ps_fg),
            patch.object(pcm.pyautogui, "moveTo"),
            patch.object(pcm.pyautogui, "click"),
            patch.object(pcm.pyautogui, "hotkey") as m_hk,
            patch.object(pcm.pyautogui, "press") as m_press,
            patch.object(pcm.pyperclip, "copy") as m_copy,
            patch.object(pcm.time, "sleep"),
        ):
            res = pcm.pc_ui_action(action="type_text", element_name="终端输入框", text="whoami", press_enter=True)
            self.assertIn("安全护栏已拦截", res)
            m_copy.assert_not_called()
            m_hk.assert_not_called()
            m_press.assert_not_called()

        # Benign windows (browser reading PowerShell docs, Explorer folder named "Run") must NOT be blocked
        benign_windows = [
            {"hwnd": 101, "pid": 500, "process": "msedge.exe", "class": "Chrome_WidgetWin_1", "title": "PowerShell 文档 - Edge"},
            {"hwnd": 102, "pid": 501, "process": "explorer.exe", "class": "CabinetWClass", "title": "Run"},
            _BENIGN_FG,
        ]
        for fg in benign_windows:
            self.assertIsNone(pcm._check_foreground_input_guard(fg), f"False positive on benign window {fg}")

    def test_l2_audit_log_redacts_typed_secrets_and_window_title_tokens(self):
        self._set_mode("full")
        runtime_secret = "runtime-deepseek-secret-998877665544"
        typed_password = "MySuperSecretPassword!2026"
        typed_api_key = "sk-1234567890abcdefghijklmnop"
        noisy_fg = {
            "hwnd": 3001,
            "pid": 6000,
            "process": "msedge.exe",
            "class": "Chrome_WidgetWin_1",
            "title": (
                "Dashboard #token=urlSecretToken123456 "
                "Bearer headerSecretToken789012 "
                "AIzaSyA123456789012345678901234 "
                f"{runtime_secret}"
            ),
        }

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": runtime_secret}),
            patch.object(pcm, "_get_foreground_window_info", return_value=noisy_fg),
            patch.object(pcm._user32, "IsClipboardFormatAvailable", return_value=0),
            patch.object(pcm.pyperclip, "copy"),
            patch.object(pcm.pyperclip, "paste", return_value=""),
            patch.object(pcm.pyautogui, "hotkey"),
            patch.object(pcm.pyautogui, "press"),
            patch.object(pcm.time, "sleep"),
        ):
            pcm.pc_ui_action(action="type_text", text=typed_password)
            res_key = pcm.pc_ui_action(action="type_text", text=typed_api_key)
            self.assertNotIn(typed_api_key, res_key)

        raw = self.audit_file.read_text(encoding="utf-8")
        for secret in (
            typed_password,
            typed_api_key,
            runtime_secret,
            "urlSecretToken123456",
            "headerSecretToken789012",
            "AIzaSyA123456789012345678901234",
        ):
            self.assertNotIn(secret, raw, f"Secret leaked into l2_audit.jsonl: {secret}")

        recs = self._read_audit_records()
        self.assertEqual(len(recs), 2)
        for rec, orig in zip(recs, (typed_password, typed_api_key)):
            for field in (
                "ts", "mode", "tool", "action", "outcome", "reason",
                "fg_process", "fg_class", "fg_title", "target", "hotkey",
                "text_len", "text_summary", "press_enter", "detail",
            ):
                self.assertIn(field, rec)
            self.assertEqual(rec["outcome"], "allowed")
            self.assertEqual(rec["text_len"], len(orig))
            self.assertEqual(rec["text_summary"], f"[REDACTED len={len(orig)}]")
            self.assertEqual(rec["fg_process"], "msedge.exe")

    def test_http_l3_config_enforces_restricted_fields_and_status_reports_mode(self):
        tok = "test-l2-guard-token-abcdefghijklmnopqrstuvwxyz"
        app = pcm.create_secured_asgi_app(tok, port=18900, bind_host="127.0.0.1")
        headers = {
            "Host": "127.0.0.1:18900",
            "Authorization": f"Bearer {tok}",
        }

        self._set_mode("safe")

        async def _exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18900") as client:
                with (
                    patch.object(l3_backends, "L3_CONFIG_FILE", self.l3_cfg_file),
                    patch.object(l3_backends, "DOTENV_FILE", self.tmp_root / ".env"),
                ):
                    # 1. GET /l3/status reports pc_mode=safe & l3_mode_allowed=False
                    r_st = await client.get("/l3/status", headers=headers)
                    self.assertEqual(r_st.status_code, 200)
                    j_st = r_st.json()
                    self.assertEqual(j_st["pc_mode"], "safe")
                    self.assertFalse(j_st["l3_mode_allowed"])

                    # 2. Forbidden / host-env-only fields must return HTTP 400 and NEVER invoke write_l3_config
                    forbidden_payloads = [
                        {"backend": "dsh", "dsh_cmd": "calc.exe"},
                        {"workbuddy_cmd": "powershell.exe -c evil"},
                        {"workdir": "C:\\Windows\\System32"},
                        {"workbuddy_auto_approve": True},
                        {"backend": "dsh", "extra_injected": "x"},
                        {"backend": "evil_backend"},
                        {"timeout_seconds": True},
                        {"timeout": "300"},
                        {"timeout": 120, "timeout_seconds": 300},
                    ]
                    with patch.object(pcm.l3_backends, "write_l3_config") as m_write:
                        for payload in forbidden_payloads:
                            r_bad = await client.post("/l3/config", headers=headers, json=payload)
                            self.assertEqual(
                                r_bad.status_code,
                                400,
                                f"Expected HTTP 400 for payload={payload!r}, got {r_bad.status_code}: {r_bad.text}",
                            )
                            self.assertFalse(r_bad.json().get("ok"))
                        m_write.assert_not_called()

                    # 3. Allowed fields: backend + timeout alias
                    r_ok1 = await client.post(
                        "/l3/config",
                        headers=headers,
                        json={"backend": "dsh", "timeout": 180},
                    )
                    self.assertEqual(r_ok1.status_code, 200, r_ok1.text)
                    j_ok1 = r_ok1.json()
                    self.assertEqual(j_ok1["backend"], "dsh")
                    self.assertEqual(j_ok1["timeout_seconds"], 180)
                    self.assertEqual(j_ok1["pc_mode"], "safe")
                    self.assertFalse(j_ok1["l3_mode_allowed"])

                    # 4. Allowed fields: backend + timeout_seconds, with mode switched to full
                    self._set_mode("full")
                    r_ok2 = await client.post(
                        "/l3/config",
                        headers=headers,
                        json={"backend": "none", "timeout_seconds": 450},
                    )
                    self.assertEqual(r_ok2.status_code, 200, r_ok2.text)
                    j_ok2 = r_ok2.json()
                    self.assertEqual(j_ok2["backend"], "none")
                    self.assertEqual(j_ok2["timeout_seconds"], 450)
                    self.assertEqual(j_ok2["pc_mode"], "full")
                    self.assertTrue(j_ok2["l3_mode_allowed"])

        asyncio.run(_exercise())


if __name__ == "__main__":
    unittest.main(verbosity=2)

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
_ORIG_MODE_FILE = pcm.MODE_FILE
_ORIG_TOKEN_FILE = pcm.TOKEN_FILE


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
        pcm.clear_emergency_stop()

    def tearDown(self):
        pcm.clear_emergency_stop()
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
        # A-2 / A-1: authoritative MODE_FILE under pc-mcp/state/, TOKEN_FILE under secrets/, FAILSAFE enabled
        self.assertEqual(_ORIG_MODE_FILE.resolve(), expected_state / "pc_control_mode.json")
        self.assertFalse(_ORIG_MODE_FILE.resolve().is_relative_to(data_dir))
        self.assertTrue(_ORIG_TOKEN_FILE.resolve().is_relative_to((MCP_DIR.parent / "secrets").resolve()))
        self.assertFalse(_ORIG_TOKEN_FILE.resolve().is_relative_to(data_dir))
        self.assertTrue(pcm.pyautogui.FAILSAFE, "PyAutoGUI FAILSAFE must remain enabled by default (A-3)")

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

    def test_a3_explorer_address_bar_start_search_and_taskmgr_new_task_blocked(self):
        """A-3: Block Explorer CabinetWClass address bar, SearchHost, StartMenuExperienceHost & TaskMgr Run New Task."""
        self._set_mode("full")
        bypass_windows = [
            # Explorer CabinetWClass address bar (Win10 Edit + Win11 AutoSuggestBox EditControl)
            {
                "hwnd": 201,
                "pid": 601,
                "process": "explorer.exe",
                "class": "CabinetWClass",
                "title": "本地磁盘 (C:)",
                "focused_control": {
                    "control_type": "EditControl",
                    "class": "Edit",
                    "automation_id": "41477",
                    "name": "地址",
                    "ancestor_names": "Address Band Root | ToolbarWindow32",
                },
            },
            {
                "hwnd": 202,
                "pid": 601,
                "process": "explorer.exe",
                "class": "CabinetWClass",
                "title": "文档",
                "focused_control": {
                    "control_type": "Edit",
                    "class": "RichEditD2DPT",
                    "automation_id": "1001",
                    "name": "Address: C:\\Users",
                },
            },
            {
                "hwnd": 203,
                "pid": 601,
                "process": "explorer.exe",
                "class": "CabinetWClass",
                "title": "下载",
                "focused_control": {
                    "control_type": "EditControl",
                    "name": "搜索 下载",
                    "automation_id": "SearchEditBox",
                },
            },
            # Start menu & Windows Search
            {
                "hwnd": 204,
                "pid": 602,
                "process": "SearchHost.exe",
                "class": "Windows.UI.Core.CoreWindow",
                "title": "搜索",
            },
            {
                "hwnd": 205,
                "pid": 603,
                "process": "StartMenuExperienceHost.exe",
                "class": "Windows.UI.Core.CoreWindow",
                "title": "开始",
            },
            # Task Manager "Run new task" / "创建新任务" (including elevated empty process name)
            {
                "hwnd": 206,
                "pid": 604,
                "process": "",
                "class": "#32770",
                "title": "创建新任务",
            },
            {
                "hwnd": 207,
                "pid": 605,
                "process": "",
                "class": "#32770",
                "title": "Create new task",
            },
            {
                "hwnd": 208,
                "pid": 606,
                "process": "taskmgr.exe",
                "class": "#32770",
                "title": "运行新任务",
            },
        ]

        with (
            patch.object(pcm.pyautogui, "hotkey") as m_hk,
            patch.object(pcm.pyautogui, "press") as m_press,
            patch.object(pcm.pyperclip, "copy") as m_copy,
            patch.object(pcm.time, "sleep"),
        ):
            for fg in bypass_windows:
                with patch.object(pcm, "_get_foreground_window_info", return_value=fg):
                    m_hk.reset_mock()
                    m_press.reset_mock()
                    m_copy.reset_mock()
                    res = pcm.pc_ui_action(
                        action="type_text",
                        text="powershell -c calc.exe",
                        press_enter=True,
                    )
                    self.assertIn("安全护栏已拦截", res, f"Expected bypass block for {fg}, got: {res}")
                    m_copy.assert_not_called()
                    m_hk.assert_not_called()
                    m_press.assert_not_called()

        # Also block Explorer address-bar focus hotkeys (Alt+D, Ctrl+L, F4) when CabinetWClass is foreground
        explorer_fg = {
            "hwnd": 209,
            "pid": 601,
            "process": "explorer.exe",
            "class": "CabinetWClass",
            "title": "文件资源管理器",
        }
        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=explorer_fg),
            patch.object(pcm.pyautogui, "hotkey") as m_hk,
        ):
            for combo in ("alt+d", "ctrl+l", "f4"):
                m_hk.reset_mock()
                res = pcm.pc_ui_action(action="hotkey", hotkey=combo)
                self.assertIn("安全护栏已拦截", res, f"Expected Explorer hotkey {combo} blocked, got: {res}")
                m_hk.assert_not_called()

    def test_a3_click_and_right_click_foreground_and_target_terminal_guard(self):
        """A-3: Refuse right_click (and terminal-targeted clicks) on conhost/WindowsTerminal/Run/AddressBar."""
        self._set_mode("full")
        wt_fg = {
            "hwnd": 301,
            "pid": 701,
            "process": "WindowsTerminal.exe",
            "class": "CASCADIA_HOSTING_WINDOW_CLASS",
            "title": "Windows PowerShell",
        }
        conhost_fg = {
            "hwnd": 302,
            "pid": 702,
            "process": "conhost.exe",
            "class": "ConsoleWindowClass",
            "title": "命令提示符",
        }
        explorer_addr_pt = {
            "hwnd": 303,
            "pid": 703,
            "process": "explorer.exe",
            "class": "CabinetWClass",
            "title": "此电脑",
            "point_control": {"hwnd": 304, "class": "Edit", "name": "地址"},
        }

        with (
            patch.object(pcm.pyautogui, "moveTo") as m_move,
            patch.object(pcm.pyautogui, "click") as m_click,
            patch.object(pcm.pyautogui, "rightClick") as m_rclick,
            patch.object(pcm.time, "sleep"),
        ):
            # 1. Foreground is Windows Terminal or conhost -> right_click MUST be rejected even with coords
            for term_fg in (wt_fg, conhost_fg):
                with (
                    patch.object(pcm, "_get_foreground_window_info", return_value=term_fg),
                    patch.object(pcm, "_get_window_info_at_point", return_value=term_fg),
                ):
                    res = pcm.pc_ui_action(action="right_click", x=500, y=500)
                    self.assertIn("安全护栏已拦截", res)
                    m_rclick.assert_not_called()
                    m_move.assert_not_called()

            # 2. Foreground is benign Notepad, but right_click target point is over Windows Terminal -> rejected!
            with (
                patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
                patch.object(pcm, "_get_window_info_at_point", return_value=wt_fg),
            ):
                res = pcm.pc_ui_action(action="right_click", x=900, y=500)
                self.assertIn("安全护栏已拦截", res)
                m_rclick.assert_not_called()

            # 3. Left click directly targeting Explorer address bar Edit control -> rejected!
            with (
                patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
                patch.object(pcm, "_get_window_info_at_point", return_value=explorer_addr_pt),
            ):
                res = pcm.pc_ui_action(action="click", x=400, y=60)
                self.assertIn("安全护栏已拦截", res)
                m_click.assert_not_called()

            # 4. Right click on benign window -> allowed
            with (
                patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
                patch.object(pcm, "_get_window_info_at_point", return_value=_BENIGN_FG),
            ):
                res = pcm.pc_ui_action(action="right_click", x=300, y=300)
                self.assertIn("右键点击", res)
                m_rclick.assert_called_once_with(300, 300)

    def test_a3_clipboard_always_cleared_when_empty_and_restored_when_nonempty(self):
        """A-3: Clipboard must be emptied if originally empty, and restored if originally containing text."""
        self._set_mode("full")

        # Case 1: Originally empty clipboard -> typed text MUST be cleared (never left behind in clipboard)
        clip_state = {"val": ""}

        def _fake_copy(val):
            clip_state["val"] = val

        def _fake_paste():
            return clip_state["val"]

        fake_clip = MagicMock()
        fake_clip.copy.side_effect = _fake_copy
        fake_clip.paste.side_effect = _fake_paste

        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm._user32, "IsClipboardFormatAvailable", return_value=0),
            patch.object(pcm, "pyperclip", fake_clip),
            patch.object(pcm.pyautogui, "hotkey"),
            patch.object(pcm.time, "sleep"),
        ):
            res = pcm.pc_ui_action(action="type_text", text="secret-command-never-linger")
            self.assertIn("已输入文本", res)
        self.assertEqual(clip_state["val"], "", "Typed text leaked in clipboard when original clipboard was empty")

        # Case 2: Originally non-empty clipboard -> old text MUST be restored even if paste raises
        clip_state["val"] = "user-original-clip"
        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm._user32, "IsClipboardFormatAvailable", return_value=1),
            patch.object(pcm, "pyperclip", fake_clip),
            patch.object(pcm.pyautogui, "hotkey", side_effect=RuntimeError("simulated paste fail")),
            patch.object(pcm.time, "sleep"),
        ):
            with self.assertRaises(RuntimeError):
                pcm.pc_ui_action(action="type_text", text="temporary-input")
        self.assertEqual(clip_state["val"], "user-original-clip")

        # Case 3: Direct Win32 _clear_clipboard uses OpenClipboard + EmptyClipboard + CloseClipboard
        fake_u32 = MagicMock()
        fake_u32.OpenClipboard.return_value = 1
        real_like_clip = type("ClipObj", (), {"copy": staticmethod(lambda s: None)})()
        pcm._clear_clipboard(fake_u32, real_like_clip)
        fake_u32.OpenClipboard.assert_called_once_with(0)
        fake_u32.EmptyClipboard.assert_called_once()
        fake_u32.CloseClipboard.assert_called_once()

    def test_a3_emergency_stop_and_failsafe_downgrade_to_safe(self):
        """A-3: Global emergency stop (Ctrl+Alt+Pause) and PyAutoGUI FailSafe immediately abort L2 and downgrade to safe."""
        self._set_mode("full")
        self.assertEqual(pcm._get_mode(), "full")

        # 1. Trigger emergency stop -> downgrades host MODE_FILE to safe & sets stop flag
        res = pcm.trigger_emergency_stop("hotkey:ctrl+alt+pause")
        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "safe")
        self.assertEqual(pcm._get_mode(), "safe")
        self.assertTrue(pcm.is_emergency_stop_active())

        # Subsequent L2 call is blocked by mode=safe (and if mode forced full without clearing estop, blocked by estop)
        self._set_mode("full")
        with patch.object(pcm.pyautogui, "click") as m_clk:
            blocked_res = pcm.pc_ui_action(action="click", x=100, y=100)
            self.assertIn("急停", blocked_res)
            m_clk.assert_not_called()

        # 2. P0-4: HTTP API set_host_mode MUST NOT clear physical emergency stop or escalate to full!
        with self.assertRaises(pcm.EmergencyStopLockedError):
            pcm.set_host_mode("full", source="http_api")
        self.assertTrue(pcm.is_emergency_stop_active())

        # Host-side explicit reset clears emergency stop cleanly and allows full mode again
        pcm.set_host_mode("full", source="host_admin", allow_clear_emergency_stop=True)
        self.assertFalse(pcm.is_emergency_stop_active())

        # 3. PyAutoGUI FailSafeException during action triggers emergency stop & downgrades to safe
        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm, "_get_window_info_at_point", return_value=_BENIGN_FG),
            patch.object(pcm.pyautogui, "moveTo", side_effect=pcm.pyautogui.FailSafeException("corner")),
        ):
            fs_res = pcm.pc_ui_action(action="click", x=10, y=10)
            self.assertIn("FAILSAFE", fs_res)
        self.assertEqual(pcm._get_mode(), "safe")
        self.assertTrue(pcm.is_emergency_stop_active())

    def test_a2_http_mode_endpoint_auth_persistence_and_full_audit(self):
        """A-2: GET/POST /mode enforces Bearer auth, writes host-private MODE_FILE, and audits switching to full."""
        tok = "test-a2-mode-token-abcdefghijklmnopqrstuvwxyz"
        app = pcm.create_secured_asgi_app(tok, port=18900, bind_host="127.0.0.1")
        headers = {
            "Host": "127.0.0.1:18900",
            "Authorization": f"Bearer {tok}",
        }

        async def _exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18900") as client:
                # 1. Missing Bearer token -> 401
                r_unauth = await client.post("/mode", headers={"Host": "127.0.0.1:18900"}, json={"mode": "full"})
                self.assertEqual(r_unauth.status_code, 401)

                # 2. Initial GET /mode defaults to safe
                r_get0 = await client.get("/mode", headers=headers)
                self.assertEqual(r_get0.status_code, 200)
                self.assertEqual(r_get0.json()["mode"], "safe")

                # 3. Invalid mode or extra field -> 400
                r_bad1 = await client.post("/mode", headers=headers, json={"mode": "root"})
                self.assertEqual(r_bad1.status_code, 400)
                r_bad2 = await client.post("/mode", headers=headers, json={"mode": "full", "extra": 1})
                self.assertEqual(r_bad2.status_code, 400)

                # 4. Valid switch to full -> 200, persists host MODE_FILE, and logs audit record
                r_full = await client.post("/mode", headers=headers, json={"mode": "full"})
                self.assertEqual(r_full.status_code, 200, r_full.text)
                self.assertEqual(r_full.json()["mode"], "full")
                self.assertEqual(pcm._get_mode(), "full")

                # 5. Switch back to off -> 200
                r_off = await client.post("/mode", headers=headers, json={"mode": "off"})
                self.assertEqual(r_off.status_code, 200)
                self.assertEqual(pcm._get_mode(), "off")

        asyncio.run(_exercise())

        recs = self._read_audit_records()
        full_audits = [r for r in recs if r["tool"] == "pc_mode" and r["reason"] == "mode_escalated_to_full"]
        self.assertEqual(len(full_audits), 1)
        self.assertEqual(full_audits[0]["mode"], "full")
        self.assertIn("safe->full", full_audits[0]["detail"])

    def test_a3_emergency_stop_hotkey_registers_vk_cancel_and_vk_pause_and_dispatches(self):
        """A-3: Hotkey listener registers BOTH VK_CANCEL (0x03, physical Ctrl+Alt+Pause) and VK_PAUSE (0x13)."""
        import ctypes
        import pc_mcp_modules.security as sec_mod

        self._set_mode("full")
        registered_calls: list[tuple[int, int, int]] = []
        unregistered_ids: list[int] = []
        msg_step = {"n": 0}

        def _fake_register(hwnd, hk_id, mods, vk):
            registered_calls.append((hk_id, mods, vk))
            return 1

        def _fake_get_msg(msg_ptr, hwnd, fmin, fmax):
            msg_step["n"] += 1
            if msg_step["n"] == 1:
                m = ctypes.cast(msg_ptr, ctypes.POINTER(ctypes.wintypes.MSG)).contents
                m.message = sec_mod._WM_HOTKEY
                m.wParam = sec_mod._EMERGENCY_HOTKEY_CANCEL_ID
                return 1
            return 0  # WM_QUIT

        def _fake_unregister(hwnd, hk_id):
            unregistered_ids.append(hk_id)
            return 1

        fake_u32 = MagicMock()
        fake_u32.RegisterHotKey.side_effect = _fake_register
        fake_u32.GetMessageW.side_effect = _fake_get_msg
        fake_u32.UnregisterHotKey.side_effect = _fake_unregister
        fake_u32.PostThreadMessageW.return_value = 1

        fake_k32 = MagicMock()
        fake_k32.GetCurrentThreadId.return_value = 424242

        sec_mod.stop_emergency_stop_hotkey_listener()
        try:
            with (
                patch.object(pcm, "_user32", fake_u32),
                patch.object(pcm, "_kernel32", fake_k32),
            ):
                t = sec_mod.start_emergency_stop_hotkey_listener()
                self.assertIsNotNone(t)
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive())
        finally:
            sec_mod.stop_emergency_stop_hotkey_listener()

        vks = {vk for _, _, vk in registered_calls}
        self.assertIn(sec_mod._VK_CANCEL, vks, "Must register VK_CANCEL (0x03) for physical Ctrl+Alt+Pause/Break")
        self.assertIn(sec_mod._VK_PAUSE, vks, "Must also register VK_PAUSE (0x13) for virtual keyboards")
        self.assertEqual(set(unregistered_ids), {sec_mod._EMERGENCY_HOTKEY_CANCEL_ID, sec_mod._EMERGENCY_HOTKEY_ID})
        self.assertTrue(pcm.is_emergency_stop_active())
        self.assertEqual(pcm._get_mode(), "safe")

    def test_a3_explorer_xaml_f3_type_text_click_guard_and_clipboard_edge_cases(self):
        """A-3: Win10/11 XAML Explorer address bar click, F3 hotkey, type_text click guard, and clipboard fail-safety."""
        self._set_mode("full")

        # 1. Win11 XAML Islands address bar click (single DesktopChildSiteBridge HWND enriched via UIA)
        xaml_addr_pt = {
            "hwnd": 401,
            "pid": 801,
            "process": "explorer.exe",
            "class": "CabinetWClass",
            "title": "文件资源管理器",
            "point_control": {
                "hwnd": 402,
                "class": "Microsoft.UI.Content.DesktopChildSiteBridge",
                "uia_class": "AutoSuggestBox",
                "control_type": "EditControl",
                "name": "地址栏",
            },
        }
        # 2. Win11 XAML bridge when UIA fails -> fail-closed
        xaml_uia_err_pt = {
            "hwnd": 401,
            "pid": 801,
            "process": "explorer.exe",
            "class": "CabinetWClass",
            "title": "文件资源管理器",
            "point_control": {
                "hwnd": 402,
                "class": "Microsoft.UI.Content.DesktopChildSiteBridge",
                "uia_error": True,
            },
        }
        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm.pyautogui, "click") as m_clk,
            patch.object(pcm.pyautogui, "moveTo") as m_mv,
        ):
            for pt in (xaml_addr_pt, xaml_uia_err_pt):
                with patch.object(pcm, "_get_window_info_at_point", return_value=pt):
                    res = pcm.pc_ui_action(action="click", x=300, y=55)
                    self.assertIn("安全护栏已拦截", res)
                    m_clk.assert_not_called()
                    m_mv.assert_not_called()

        # 3. Explorer F3 hotkey blocked
        explorer_fg = {"hwnd": 401, "pid": 801, "process": "explorer.exe", "class": "CabinetWClass", "title": "此电脑"}
        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=explorer_fg),
            patch.object(pcm.pyautogui, "hotkey") as m_hk,
        ):
            res_f3 = pcm.pc_ui_action(action="hotkey", hotkey="f3")
            self.assertIn("安全护栏已拦截", res_f3)
            m_hk.assert_not_called()

        # 4. type_text with element_name when foreground is terminal and WindowFromPoint returns None -> NO blind click!
        wt_fg = {
            "hwnd": 405,
            "pid": 805,
            "process": "WindowsTerminal.exe",
            "class": "CASCADIA_HOSTING_WINDOW_CLASS",
            "title": "PowerShell",
        }
        with (
            patch.object(pcm, "_smart_locate_target", return_value=(500, 500, "输入框")),
            patch.object(pcm, "_get_foreground_window_info", return_value=wt_fg),
            patch.object(pcm, "_get_window_info_at_point", return_value=None),
            patch.object(pcm.pyautogui, "moveTo") as m_mv,
            patch.object(pcm.pyautogui, "click") as m_clk,
        ):
            res_tt = pcm.pc_ui_action(action="type_text", element_name="输入框", text="whoami")
            self.assertIn("安全护栏已拦截", res_tt)
            m_mv.assert_not_called()
            m_clk.assert_not_called()

        # 5. Clipboard: if restore clip.copy(old_clip) fails in finally, fallback MUST call _clear_clipboard
        copy_calls: list[str] = []

        def _flaky_copy(val):
            copy_calls.append(val)
            if val == "orig-clip":
                raise RuntimeError("clipboard locked during restore")

        fake_clip = MagicMock()
        fake_clip.copy.side_effect = _flaky_copy
        fake_clip.paste.return_value = "orig-clip"
        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm._user32, "IsClipboardFormatAvailable", return_value=1),
            patch.object(pcm, "pyperclip", fake_clip),
            patch.object(pcm, "_clear_clipboard") as m_clear,
            patch.object(pcm.pyautogui, "hotkey"),
            patch.object(pcm.time, "sleep"),
        ):
            res_cb = pcm.pc_ui_action(action="type_text", text="secret-typed-text")
            self.assertIn("已输入文本", res_cb)
            m_clear.assert_called_once()

        # 6. Clipboard: if estop fires right before clip.copy(text), non-text clipboard MUST NOT be wiped
        estop_calls = {"n": 0}

        def _estop_before_copy():
            estop_calls["n"] += 1
            return estop_calls["n"] >= 3  # allow entry + post-target check, trigger right before clip.copy

        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=_BENIGN_FG),
            patch.object(pcm._user32, "IsClipboardFormatAvailable", return_value=0),
            patch.object(pcm, "is_emergency_stop_active", side_effect=_estop_before_copy),
            patch.object(pcm, "_clear_clipboard") as m_clear2,
            patch.object(pcm.pyperclip, "copy") as m_copy2,
            patch.object(pcm.time, "sleep"),
        ):
            res_es = pcm.pc_ui_action(action="type_text", text="should-abort")
            self.assertIn("急停", res_es)
            m_copy2.assert_not_called()
            m_clear2.assert_not_called()

    def test_p0_4_http_mode_cannot_clear_physical_emergency_stop_and_blocks_l3_and_inspect(self):
        """P0-4: POST /mode cannot clear physical emergency stop (423 on full, keeps lock on off/safe); L3 & inspect also blocked."""
        tok = "test-p04-estop-token-abcdefghijklmnopqrstuvwxyz"
        app = pcm.create_secured_asgi_app(tok, port=18900, bind_host="127.0.0.1")
        headers = {
            "Host": "127.0.0.1:18900",
            "Authorization": f"Bearer {tok}",
        }
        self._set_mode("full")
        pcm.trigger_emergency_stop("hotkey:ctrl+alt+pause")
        self.assertTrue(pcm.is_emergency_stop_active())
        self.assertEqual(pcm._get_mode(), "safe")

        async def _exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18900") as client:
                # 1. Container/HTTP POST /mode {"mode": "full"} MUST fail closed with 423 Locked
                r_esc = await client.post("/mode", headers=headers, json={"mode": "full"})
                self.assertEqual(r_esc.status_code, 423, r_esc.text)
                self.assertFalse(r_esc.json().get("ok"))
                self.assertTrue(r_esc.json().get("emergency_stopped"))
                self.assertEqual(pcm._get_mode(), "safe")
                self.assertTrue(pcm.is_emergency_stop_active())

                # 2. Downgrading to off/safe via HTTP succeeds WITHOUT clearing physical emergency_stop!
                r_off = await client.post("/mode", headers=headers, json={"mode": "off"})
                self.assertEqual(r_off.status_code, 200, r_off.text)
                self.assertEqual(r_off.json().get("mode"), "off")
                self.assertTrue(r_off.json().get("emergency_stopped"))
                self.assertTrue(pcm.is_emergency_stop_active())

                # 3. GET /mode still reports emergency_stopped=True
                r_get = await client.get("/mode", headers=headers)
                self.assertEqual(r_get.status_code, 200)
                self.assertTrue(r_get.json().get("emergency_stopped"))

                # 4. Even if MODE_FILE is forced to 'full', L2, L3, /l3/status, and pc_inspect_window_ui remain blocked!
                self._set_mode("full")
                l2_err = pcm._check_permission("L2")
                self.assertIsNotNone(l2_err)
                self.assertIn("急停", l2_err)
                l3_err = pcm._check_permission("L3")
                self.assertIsNotNone(l3_err)
                self.assertIn("急停", l3_err)
                insp_res = pcm.pc_inspect_window_ui("")
                self.assertIn("急停", insp_res)
                r_l3st = await client.get("/l3/status", headers=headers)
                self.assertEqual(r_l3st.status_code, 200)
                self.assertTrue(r_l3st.json().get("emergency_stopped"))
                self.assertFalse(r_l3st.json().get("l3_mode_allowed"))

                # 5. Host-side explicit reset unlocks emergency stop, after which HTTP POST /mode full works
                pcm.set_host_mode("safe", source="host_admin", allow_clear_emergency_stop=True)
                self.assertFalse(pcm.is_emergency_stop_active())
                r_full_ok = await client.post("/mode", headers=headers, json={"mode": "full"})
                self.assertEqual(r_full_ok.status_code, 200, r_full_ok.text)
                self.assertEqual(r_full_ok.json().get("mode"), "full")
                self.assertFalse(r_full_ok.json().get("emergency_stopped"))

        asyncio.run(_exercise())

        recs = self._read_audit_records()
        locked_audits = [r for r in recs if r["tool"] == "pc_mode" and r["reason"] == "emergency_stop_locked"]
        self.assertGreaterEqual(len(locked_audits), 1)

    def test_p0_2_and_p1_2_dialog_32770_xaml_bridge_and_rename_hardening(self):
        """P0-2 & P1-2: Guard #32770 common file dialog address/edit/hotkeys, Win11 XAML bridge fail-closed, deep ancestors, and strict rename."""
        import pc_mcp_modules.winapi as winapi_mod

        self._set_mode("full")

        # 1. P0-2: #32770 Open/Save dialog address bar, Breadcrumb Parent, filename Edit (1148), SearchEditBox blocked
        dialog_bypass_cases = [
            {
                "hwnd": 501,
                "pid": 901,
                "process": "notepad.exe",
                "class": "#32770",
                "title": "打开",
                "focused_control": {
                    "class": "Breadcrumb Parent",
                    "name": "地址: C:\\Users",
                    "control_type": "ToolBarControl",
                },
            },
            {
                "hwnd": 502,
                "pid": 901,
                "process": "notepad.exe",
                "class": "#32770",
                "title": "Open",
                "focused_control": {
                    "class": "Edit",
                    "automation_id": "1148",
                    "control_type": "EditControl",
                    "name": "文件名(N):",
                },
            },
            {
                "hwnd": 503,
                "pid": 901,
                "process": "notepad.exe",
                "class": "#32770",
                "title": "另存为",
                "focused_control": {
                    "class": "ComboBoxEx32",
                    "control_type": "ComboBoxControl",
                    "name": "文件名",
                },
            },
            {
                "hwnd": 504,
                "pid": 901,
                "process": "notepad.exe",
                "class": "#32770",
                "title": "打开",
                "focused_control": {
                    "class": "Microsoft.UI.Content.DesktopChildSiteBridge",
                    "uia_error": True,
                },
            },
        ]
        for fg in dialog_bypass_cases:
            err = pcm._check_foreground_input_guard(fg)
            self.assertIsNotNone(err, f"Expected #32770 dialog control to be blocked: {fg}")
            self.assertIn("安全护栏已拦截", err)

        # 2. P0-2: #32770 hotkeys (alt+d, ctrl+l, f4, f6, alt+n) and CabinetWClass f6 blocked
        dlg_fg = {"hwnd": 501, "pid": 901, "process": "notepad.exe", "class": "#32770", "title": "打开"}
        for combo in ("alt+d", "ctrl+l", "f4", "f6", "alt+n"):
            with (
                patch.object(pcm, "_get_foreground_window_info", return_value=dlg_fg),
                patch.object(pcm.pyautogui, "hotkey") as m_hk,
            ):
                res = pcm.pc_ui_action(action="hotkey", hotkey=combo)
                self.assertIn("安全护栏已拦截", res, f"Expected #32770 hotkey {combo} blocked, got: {res}")
                m_hk.assert_not_called()

        exp_fg = {"hwnd": 505, "pid": 902, "process": "explorer.exe", "class": "CabinetWClass", "title": "此电脑"}
        with (
            patch.object(pcm, "_get_foreground_window_info", return_value=exp_fg),
            patch.object(pcm.pyautogui, "hotkey") as m_hk,
        ):
            res_f6 = pcm.pc_ui_action(action="hotkey", hotkey="f6")
            self.assertIn("安全护栏已拦截", res_f6)
            m_hk.assert_not_called()

        # 3. P1-2: Win11 XAML bridge returning outer DesktopChildSiteBridge (uia_error NOT set!) MUST still fail closed!
        xaml_outer_bridge = {
            "hwnd": 506,
            "pid": 902,
            "process": "explorer.exe",
            "class": "CabinetWClass",
            "title": "文件资源管理器",
            "point_control": {
                "hwnd": 507,
                "class": "Microsoft.UI.Content.DesktopChildSiteBridge",
                "uia_class": "Microsoft.UI.Content.DesktopChildSiteBridge",
                "control_type": "PaneControl",
                "name": "DesktopWindowXamlSource",
            },
        }
        self.assertTrue(pcm._is_explorer_address_or_search_bar(xaml_outer_bridge))
        self.assertTrue(
            winapi_mod._is_unresolved_xaml_bridge(
                {"class": "Microsoft.UI.Content.DesktopChildSiteBridge"},
                {"class": "Microsoft.UI.Content.DesktopChildSiteBridge", "control_type": "PaneControl"},
                True,
            )
        )

        # 4. P1-2: Folder named "rename" / "重命名" in address bar MUST NOT bypass guard; genuine ListItemControl rename IS allowed
        fake_rename_bypass = {
            "hwnd": 508,
            "pid": 902,
            "process": "explorer.exe",
            "class": "CabinetWClass",
            "title": "rename",
            "focused_control": {
                "control_type": "EditControl",
                "class": "Edit",
                "name": "地址: C:\\Users\\rename",
                "automation_id": "",
                "ancestor_names": "Address Band Root | Breadcrumb Parent",
                "ancestor_types": "PaneControl | ToolBarControl",
            },
        }
        self.assertTrue(
            pcm._is_explorer_address_or_search_bar(fake_rename_bypass),
            "Folder named 'rename' in address bar must NOT bypass guard!",
        )
        fake_rename_no_listitem = {
            "hwnd": 509,
            "pid": 902,
            "process": "explorer.exe",
            "class": "CabinetWClass",
            "title": "重命名",
            "focused_control": {
                "control_type": "EditControl",
                "class": "RichEditD2DPT",
                "name": "重命名",
                "automation_id": "",
                "ancestor_names": "NavigationBar",
                "ancestor_types": "PaneControl | GroupControl",
            },
        }
        self.assertTrue(
            pcm._is_explorer_address_or_search_bar(fake_rename_no_listitem),
            "Edit without ListItemControl ancestor must NOT get rename exemption!",
        )
        legit_file_rename = {
            "hwnd": 510,
            "pid": 902,
            "process": "explorer.exe",
            "class": "CabinetWClass",
            "title": "文档",
            "focused_control": {
                "control_type": "EditControl",
                "class": "DirectUIHWND",
                "name": "重命名",
                "automation_id": "System.ItemNameDisplay",
                "ancestor_names": "report.txt | 项目视图",
                "ancestor_types": "ListItemControl | ListControl",
            },
        }
        self.assertFalse(
            pcm._is_explorer_address_or_search_bar(legit_file_rename),
            "Genuine Explorer ListItemControl inline file rename should remain allowed",
        )

        # 5. P1-2: _extract_uia_control_fields walks >= 8 parent levels and populates ancestor_types
        class _MockNode:
            def __init__(self, idx: int, parent=None):
                self.ControlTypeName = "ListItemControl" if idx == 8 else "PaneControl"
                self.Name = f"Node{idx}"
                self.AutomationId = f"aid{idx}"
                self.ClassName = f"Cls{idx}"
                self._parent = parent

            def GetParentControl(self):
                return self._parent

        chain = None
        for idx in range(9, -1, -1):
            chain = _MockNode(idx, parent=chain)
        fields = winapi_mod._extract_uia_control_fields(chain)
        self.assertIn("Node8", fields.get("ancestor_names", ""))
        self.assertIn("ListItemControl", fields.get("ancestor_types", ""))

        # 6. Red-team hardening regressions:
        # 6a. User-controlled folder literally named "ListItemControl" in ancestor_names MUST NOT spoof has_list_item_ancestor
        spoofed_folder_rename = {
            "hwnd": 511,
            "pid": 902,
            "process": "explorer.exe",
            "class": "CabinetWClass",
            "title": "rename",
            "focused_control": {
                "control_type": "EditControl",
                "class": "Edit",
                "name": "rename",
                "automation_id": "",
                "ancestor_names": "C:\\Temp\\ListItemControl",
                "ancestor_types": "PaneControl | ToolBarControl",
            },
        }
        self.assertTrue(
            pcm._is_explorer_address_or_search_bar(spoofed_folder_rename),
            "Folder named 'ListItemControl' in ancestor_names must NOT spoof rename exemption!",
        )

        # 6b. Win11 XAML AutoSuggestBox ListItemControl dropdown inside DesktopChildSiteBridge MUST be blocked
        xaml_autosuggest_item = {
            "hwnd": 512,
            "pid": 902,
            "process": "explorer.exe",
            "class": "CabinetWClass",
            "title": "文件资源管理器",
            "focused_control": {
                "class": "Microsoft.UI.Content.DesktopChildSiteBridge",
                "uia_class": "ListViewItem",
                "control_type": "ListItemControl",
                "name": "cmd.exe",
                "ancestor_names": "AutoSuggestBox | Address Band Root",
                "ancestor_types": "ListControl",
            },
        }
        self.assertTrue(
            pcm._is_explorer_address_or_search_bar(xaml_autosuggest_item),
            "XAML AutoSuggestBox ListItemControl dropdown must be blocked!",
        )

        # 6c. Win11 22H2/23H2/24H2 XAML InputSite HWND MUST fail closed when UIA is unresolved
        self.assertTrue(
            winapi_mod._is_unresolved_xaml_bridge(
                {"class": "Windows.UI.Input.InputSite.WindowClass"}, {}, False
            )
        )
        self.assertTrue(
            pcm._is_explorer_address_or_search_bar(
                {
                    "hwnd": 513,
                    "pid": 902,
                    "process": "explorer.exe",
                    "class": "CabinetWClass",
                    "title": "此电脑",
                    "focused_control": {
                        "class": "Windows.UI.Input.InputSite.WindowClass",
                        "uia_error": True,
                    },
                }
            )
        )

        # 6d. #32770 Breadcrumb ToolbarWindow32 click with neutral name ("Desktop") MUST be blocked
        self.assertTrue(
            pcm._is_explorer_address_or_search_bar(
                {
                    "hwnd": 514,
                    "pid": 901,
                    "process": "notepad.exe",
                    "class": "#32770",
                    "title": "打开",
                    "point_control": {
                        "class": "ToolbarWindow32",
                        "name": "Desktop",
                        "ancestor_names": "Breadcrumb Parent | ReBarWindow32",
                    },
                }
            )
        )

        # 6e. #32770 sets uia_error=True fail-closed even for classic Win32 child controls when UIA raises,
        #     and CabinetWClass also sets uia_error=True fail-closed when both focus HWND & UIA are unavailable.
        import sys as _sys
        broken_uia = type("_BrokenUia", (), {"GetFocusedControl": staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("uia fail")))})()
        with patch.dict(_sys.modules, {"uiautomation": broken_uia}):
            fc_32770 = winapi_mod._get_focused_control_info(0, parent_class="#32770")
            fc_cab_uninspectable = winapi_mod._get_focused_control_info(0, parent_class="CabinetWClass")
        self.assertTrue(fc_32770.get("uia_error"))
        self.assertTrue(fc_cab_uninspectable.get("uia_error"))
        self.assertTrue(
            pcm._is_explorer_address_or_search_bar(
                {
                    "hwnd": 515,
                    "pid": 902,
                    "process": "explorer.exe",
                    "class": "CabinetWClass",
                    "title": "此电脑",
                    "focused_control": fc_cab_uninspectable,
                }
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

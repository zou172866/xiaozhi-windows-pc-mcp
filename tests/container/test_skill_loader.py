"""Unit tests for Phase 4 progressive `SKILL.md` loader & prefix-cache protection."""

from __future__ import annotations

from http import HTTPStatus
import os
import sys
import tempfile
import types
import unittest

ROOT_DIR = "/opt/xiaozhi-esp32-server"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DIR = os.environ.get("XIAOZHI_APP_DIR") or (
    os.path.join(ROOT_DIR, "app")
    if os.path.isdir(os.path.join(ROOT_DIR, "app"))
    else os.path.join(_REPO_ROOT, "app")
)
for _p in (ROOT_DIR, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_settings
import skill_loader
import turn_context_router
from console.routes import skills as skills_routes
from patches import skill_loader_patch


class _FakeHandler:
    def __init__(self, body=None):
        self._body = body or {}
        self.status = None
        self.payload = None

    def _read_body_json(self):
        return self._body

    def _send_json(self, data, status=HTTPStatus.OK):
        self.status = int(status)
        self.payload = data


class TestPhase4ProgressiveSkillLoader(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.settings_path = os.path.join(self._td.name, "runtime_settings.json")
        self.builtin_dir = os.path.join(self._td.name, "builtin_skills")
        self.user_dir = os.path.join(self._td.name, "user_skills")
        os.makedirs(os.path.join(self.builtin_dir, "demo-ops"), exist_ok=True)
        with open(os.path.join(self.builtin_dir, "demo-ops", "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(
                "---\n"
                "name: demo-ops\n"
                "description: 演示自动化排障手册，处理桌面与日志问题。\n"
                "---\n\n"
                "# Demo Ops\n"
                "正文步骤：1) 检查日志；密钥 sk-1234567890abcdefghij 必须脱敏。\n"
            )
        self.reg = skill_loader.SkillRegistry(
            roots=[self.builtin_dir, self.user_dir],
            settings_path=self.settings_path,
        )
        skill_loader.reset_skill_registry(self.reg)

    def tearDown(self):
        skill_loader.reset_skill_registry(None)
        self._td.cleanup()

    def test_metadata_index_only_and_on_demand_load_with_redaction(self):
        skills = self.reg.scan()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "demo-ops")
        self.assertIn("演示自动化排障手册", skills[0]["description"])

        idx_block = self.reg.build_turn_context_index()
        self.assertIn("<available_skills>", idx_block)
        self.assertIn("demo-ops", idx_block)
        # Full body must NOT leak into metadata index
        self.assertNotIn("正文步骤", idx_block)

        # On-demand load returns body with secrets redacted
        ok, body = self.reg.load_skill_body("demo-ops")
        self.assertTrue(ok)
        self.assertIn("正文步骤", body)
        self.assertNotIn("sk-1234567890abcdefghij", body)

        # Traversal / invalid names rejected
        ok_bad, msg_bad = self.reg.load_skill_body("../etc/passwd")
        self.assertFalse(ok_bad)
        self.assertIn("非法技能名称", msg_bad)

    def test_folded_and_literal_yaml_block_scalar_frontmatter(self):
        folded_raw = (
            "---\n"
            "name: win-gui-ops\n"
            "description: >-\n"
            "  Windows 高分屏桌面操控指南。\n"
            "  支持说明: 精确定位与后台无黑框守护。\n"
            "---\n\n"
            "# Body\n"
        )
        name1, desc1 = skill_loader.parse_skill_frontmatter(folded_raw, fallback_name="fallback")
        self.assertEqual(name1, "win-gui-ops")
        self.assertNotEqual(desc1, ">-")
        self.assertIn("Windows 高分屏桌面操控指南", desc1)
        self.assertIn("精确定位与后台无黑框守护", desc1)

        literal_raw = (
            "---\n"
            "name: \"quoted-skill\"\n"
            "description: |\n"
            "  第一行摘要\n"
            "\n"
            "  第二行摘要\n"
            "---\n"
        )
        name2, desc2 = skill_loader.parse_skill_frontmatter(literal_raw, fallback_name="fallback")
        self.assertEqual(name2, "quoted-skill")
        self.assertEqual(desc2, "第一行摘要 第二行摘要")

    def test_turn_context_injection_keeps_static_system_prompt_byte_identical(self):
        sys_prompt = "你是雷电芽衣，温柔体贴的语音助手。"
        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "帮我看看电脑怎么排障"},
        ]
        out1 = turn_context_router.apply_turn_context_to_messages(msgs, mode="split", current_time_str="10:00")
        # Static system prompt remains 100% byte-identical (zero prefix-cache busting!)
        self.assertEqual(out1[0]["content"], sys_prompt)
        # Compact skill index lives inside ephemeral <turn_context> on latest user message
        self.assertIn("<turn_context>", out1[1]["content"])
        self.assertIn("<available_skills>", out1[1]["content"])
        self.assertIn("demo-ops", out1[1]["content"])
        self.assertNotIn("正文步骤", out1[1]["content"])

        # Disabling skill loader removes <available_skills> while keeping system prompt identical
        runtime_settings.set_skill_loader(enabled=False, settings_path=self.settings_path)
        out2 = turn_context_router.apply_turn_context_to_messages(msgs, mode="split", current_time_str="10:00")
        self.assertEqual(out2[0]["content"], sys_prompt)
        self.assertNotIn("<available_skills>", out2[1]["content"])

    def test_patch_registration_and_console_routes(self):
        reg_mod = types.ModuleType("plugins_func.register")

        class _ToolType:
            SYSTEM_CTL = "SYSTEM_CTL"

        class _Action:
            REQLLM = "REQLLM"

        class _ActionResponse:
            def __init__(self, action, result, response):
                self.action = action
                self.result = result
                self.response = response

        class _FunctionItem:
            def __init__(self, name, desc, func, tool_type):
                self.name = name
                self.description = desc
                self.func = func
                self.type = tool_type

        reg_mod.ToolType = _ToolType
        reg_mod.Action = _Action
        reg_mod.ActionResponse = _ActionResponse
        reg_mod.FunctionItem = _FunctionItem
        reg_mod.all_function_registry = {}
        pkg = types.ModuleType("plugins_func")
        pkg.register = reg_mod
        saved = {k: sys.modules.get(k) for k in ("plugins_func", "plugins_func.register")}
        try:
            sys.modules["plugins_func"] = pkg
            sys.modules["plugins_func.register"] = reg_mod
            skill_loader_patch.install_skill_loader_patch()
            self.assertIn("load_skill", reg_mod.all_function_registry)
            resp = reg_mod.all_function_registry["load_skill"].func(None, skill_name="demo-ops")
            self.assertEqual(resp.action, "REQLLM")
            self.assertIn("<loaded_skill name=\"demo-ops\">", resp.result)
            self.assertIn("正文步骤", resp.result)
        finally:
            for k, old in saved.items():
                if old is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = old

        orig_path = runtime_settings.RUNTIME_SETTINGS_PATH
        runtime_settings.RUNTIME_SETTINGS_PATH = self.settings_path
        try:
            h_get = _FakeHandler()
            skills_routes.handle_get_skills(h_get)
            self.assertEqual(h_get.status, 200)
            self.assertEqual(h_get.payload["count"], 1)

            h_post = _FakeHandler({"enabled": False, "refresh": True})
            skills_routes.handle_post_skills(h_post)
            self.assertEqual(h_post.status, 200)
            self.assertFalse(h_post.payload["enabled"])
        finally:
            runtime_settings.RUNTIME_SETTINGS_PATH = orig_path


if __name__ == "__main__":
    unittest.main(verbosity=2)

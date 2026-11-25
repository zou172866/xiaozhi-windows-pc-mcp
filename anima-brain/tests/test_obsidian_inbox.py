"""Unit & HTTP API tests for Phase 3 Obsidian two-way memo inbox (anima-brain/obsidian_inbox.py)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

ANIMA_ROOT = Path(__file__).resolve().parent.parent
if str(ANIMA_ROOT) not in sys.path:
    sys.path.insert(0, str(ANIMA_ROOT))
SRC_DIR = ANIMA_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from companion_engine import BrainConfig, CompanionBrain, Settings
from companion_engine.automation import MemoryAutomation
import obsidian_exporter as obs_mod
import obsidian_inbox as inbox_mod
import xiaozhi_anima_service as svc


class TestObsidianInbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.db_path = self.tmp_dir / "brain.sqlite3"
        self.vault_dir = self.tmp_dir / "obsidian_vault"
        self.cfg_path = self.tmp_dir / "obsidian_sync_config.json"
        self.brain = CompanionBrain(
            self.db_path,
            user_id="local",
            settings=Settings(),
            config=BrainConfig(
                start_worker=False,
                automation=MemoryAutomation(embedding=False, summaries=False, reconcile=False),
            ),
        )
        self.brain.create_character(
            character_id=svc.DEFAULT_CHARACTER_ID,
            name="雷电芽衣",
            persona="你是雷电芽衣。",
            memory_policy="automatic",
            relationship_enabled=True,
            romance_enabled=True,
            timezone="Asia/Shanghai",
        )
        svc.ensure_xiaozhi_turn_meta_table(self.brain.engine)

    def tearDown(self):
        self.brain.close()
        self._tmp.cleanup()

    def test_default_disabled_zero_io_and_no_receipt_or_table(self):
        exp = obs_mod.ObsidianExporter(
            self.brain,
            character_id=svc.DEFAULT_CHARACTER_ID,
            export_dir=self.vault_dir,
            config_path=self.cfg_path,
        )
        self.assertFalse(exp.is_inbox_enabled())
        st = exp.status()
        self.assertFalse(st["inbox_enabled"])
        self.assertFalse(st["inbox"]["enabled"])

        # Disabled scan is a strict no-op: no directory created, no receipt written, no SQLite ledger table created
        res_bg = exp.scan_inbox(force=False)
        self.assertTrue(res_bg["ok"])
        self.assertTrue(res_bg["skipped_disabled"])
        self.assertFalse((self.vault_dir / inbox_mod.INBOX_DIRNAME).exists())

        res_manual = exp.scan_inbox(force=True)
        self.assertFalse(res_manual["ok"])
        self.assertTrue(res_manual["skipped_disabled"])
        self.assertFalse((self.vault_dir / inbox_mod.INBOX_DIRNAME).exists())

        tbl = self.brain.engine.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xiaozhi_obsidian_inbox_items'"
        ).fetchone()
        self.assertIsNone(tbl)

    def test_markdown_parser_todos_tags_dates_comments_code_and_redaction(self):
        md = """---
title: 我的备忘
---
# 标题应忽略

<!-- HTML 注释内容不应入库 -->
%% Obsidian 注释不应入库 %%

```python
# 代码块里的注释与列表不应入库
- [ ] fake code task
```

> 引用提示不应入库

- [ ] 提醒我提交季度总结 @2026-12-04
- [x] 已完成买牛奶
- 我最近在戒糖，晚饭少吃碳水 #偏好
- 妈妈下周过生日 2026-12-10 #关系
- API 密钥 sk-secret1234567890abcdef1234567890
- 我的手机是 13812345678，最近想学吉他 #目标
"""
        items, red_skip = inbox_mod.parse_inbox_markdown(
            md,
            source_name="memo.md",
            redact_fn=obs_mod.redact_credentials,
        )
        kinds = [(x["kind"], x["checked"], x["category"]) for x in items]
        self.assertIn(("todo", False, "date"), kinds)
        self.assertIn(("todo", True, "goal"), kinds)
        self.assertIn(("fact", False, "preference"), kinds)
        self.assertIn(("fact", False, "relationship"), kinds)
        self.assertIn(("fact", False, "goal"), kinds)

        # Ensure comments/code/frontmatter were skipped
        all_texts = "\n".join(x["text"] for x in items)
        self.assertNotIn("HTML 注释", all_texts)
        self.assertNotIn("Obsidian 注释", all_texts)
        self.assertNotIn("fake code task", all_texts)
        # Ensure API key and phone number are redacted
        self.assertNotIn("sk-secret1234567890abcdef1234567890", all_texts)
        self.assertNotIn("13812345678", all_texts)

        # Unchecked vs checked identical task produces identical canonical item_hash
        it_open, _ = inbox_mod.parse_inbox_markdown(
            "- [ ] 提醒我提交季度总结 @2026-12-04",
            source_name="a.md",
            redact_fn=obs_mod.redact_credentials,
        )
        it_done, _ = inbox_mod.parse_inbox_markdown(
            "- [x] 提醒我提交季度总结 @2026-12-04",
            source_name="a.md",
            redact_fn=obs_mod.redact_credentials,
        )
        self.assertEqual(it_open[0]["item_hash"], it_done[0]["item_hash"])
        self.assertEqual(str(it_open[0]["event_date"]), "2026-12-04")
        self.assertGreaterEqual(red_skip, 0)

    def test_end_to_end_scan_idempotency_resolve_reopen_and_original_untouched(self):
        exp = obs_mod.ObsidianExporter(
            self.brain,
            character_id=svc.DEFAULT_CHARACTER_ID,
            export_dir=self.vault_dir,
            config_path=self.cfg_path,
        )
        inbox_dir = self.vault_dir / inbox_mod.INBOX_DIRNAME
        inbox_dir.mkdir(parents=True, exist_ok=True)
        note_path = inbox_dir / "给芽衣的备忘.md"
        original_content = (
            "# 给芽衣的备忘\n\n"
            "- [ ] 提醒我周五去体检 @2026-12-04\n"
            "- 平时喝美式咖啡不加糖 #偏好\n"
        )
        note_path.write_text(original_content, encoding="utf-8")

        exp.set_inbox_enabled(True)
        res1 = exp.scan_inbox(force=True)
        self.assertTrue(res1["ok"])
        self.assertTrue(res1["changed"])
        self.assertEqual(res1["stats"]["added_facts"], 1)
        self.assertEqual(res1["stats"]["added_loops"], 1)
        self.assertEqual(res1["stats"]["resolved_loops"], 0)

        # User's original file MUST remain byte-for-byte untouched; read-only receipt exists
        self.assertEqual(note_path.read_text(encoding="utf-8"), original_content)
        receipt_path = inbox_dir / inbox_mod.STATUS_FILENAME
        self.assertTrue(receipt_path.is_file())
        self.assertIn("提醒我周五去体检", receipt_path.read_text(encoding="utf-8"))

        # Verify open loop is visible in Anima Brain memory.loops()
        loops = self.brain.engine.memory.loops(svc.DEFAULT_CHARACTER_ID)
        self.assertTrue(any("周五去体检" in m.text for m in loops))

        # Re-scanning unchanged note short-circuits via fingerprint (zero duplicates)
        res2 = exp.scan_inbox(force=False)
        self.assertTrue(res2["ok"])
        self.assertTrue(res2["skipped_unchanged"])
        self.assertFalse(res2["changed"])

        # User checks [x] -> Anima Brain marks open loop resolved & dismisses proactive nudge
        checked_content = original_content.replace("- [ ] 提醒我周五去体检", "- [x] 提醒我周五去体检")
        note_path.write_text(checked_content, encoding="utf-8")
        res3 = exp.scan_inbox(force=True)
        self.assertTrue(res3["ok"])
        self.assertTrue(res3["changed"])
        self.assertEqual(res3["stats"]["resolved_loops"], 1)
        self.assertEqual(res3["stats"]["total_open_todos"], 0)
        self.assertEqual(res3["stats"]["total_resolved_todos"], 1)
        loops_after = self.brain.engine.memory.loops(svc.DEFAULT_CHARACTER_ID)
        self.assertFalse(any("周五去体检" in m.text for m in loops_after))

        # User unchecks [ ] -> cleanly reopens loop
        note_path.write_text(original_content, encoding="utf-8")
        res4 = exp.scan_inbox(force=True)
        self.assertTrue(res4["ok"])
        self.assertTrue(res4["changed"])
        self.assertEqual(res4["stats"]["added_loops"], 1)
        loops_reopened = self.brain.engine.memory.loops(svc.DEFAULT_CHARACTER_ID)
        self.assertTrue(any("周五去体检" in m.text for m in loops_reopened))

    def test_symlink_outside_inbox_is_blocked(self):
        outside_secret = self.tmp_dir / "outside_secret.md"
        outside_secret.write_text("- 绝密外部内容不应被导入\n", encoding="utf-8")
        inbox_dir = self.vault_dir / inbox_mod.INBOX_DIRNAME
        inbox_dir.mkdir(parents=True, exist_ok=True)
        link_path = inbox_dir / "escape.md"
        try:
            os.symlink(outside_secret, link_path)
        except OSError:
            self.skipTest("Symlink creation not permitted in this environment")

        files, warnings = inbox_mod.list_safe_inbox_files(inbox_dir)
        self.assertEqual(files, [])
        self.assertTrue(warnings)

    def test_http_api_obsidian_inbox_config_scan_and_briefing_summary(self):
        exp = obs_mod.ObsidianExporter(
            self.brain,
            character_id=svc.DEFAULT_CHARACTER_ID,
            export_dir=self.vault_dir,
            config_path=self.cfg_path,
            default_enabled=False,
            default_inbox_enabled=False,
        )
        token = "test-phase3-token-" + ("a" * 16)
        headers = {"Authorization": f"Bearer {token}"}
        app = svc.create_combined_asgi_app(self.brain, token, obsidian_exporter=exp)

        inbox_dir = self.vault_dir / inbox_mod.INBOX_DIRNAME
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / "todo.md").write_text("- [ ] 记得给绿植浇水\n", encoding="utf-8")

        with TestClient(app, base_url="http://127.0.0.1:18787") as client:
            # 1. Scan while disabled -> HTTP 400
            r_dis = client.post("/xiaozhi/obsidian/inbox/scan", headers=headers, json={})
            self.assertEqual(r_dis.status_code, 400)
            self.assertFalse(r_dis.json()["ok"])

            # 2. Enable inbox via /xiaozhi/obsidian/config -> HTTP 200 and ingests immediately
            r_en = client.post("/xiaozhi/obsidian/config", headers=headers, json={"inbox_enabled": True})
            self.assertEqual(r_en.status_code, 200)
            body_en = r_en.json()
            self.assertTrue(body_en["ok"])
            self.assertTrue(body_en["obsidian"]["inbox_enabled"])
            self.assertEqual(body_en["obsidian"]["inbox"]["stats"]["total_open_todos"], 1)

            # 3. Manual scan -> HTTP 200
            r_scan = client.post("/xiaozhi/obsidian/inbox/scan", headers=headers, json={})
            self.assertEqual(r_scan.status_code, 200)
            self.assertTrue(r_scan.json()["ok"])

            # 4. Briefing endpoint returns concise redacted briefing_summary including open loop
            r_br = client.post("/xiaozhi/obsidian/briefing", headers=headers, json={})
            self.assertEqual(r_br.status_code, 200)
            br_json = r_br.json()
            self.assertTrue(br_json["ok"])
            summary = br_json["result"]["briefing_summary"]
            self.assertIn("给绿植浇水", summary)


if __name__ == "__main__":
    unittest.main()

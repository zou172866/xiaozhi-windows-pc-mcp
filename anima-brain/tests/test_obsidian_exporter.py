"""Unit & integration tests for Obsidian one-way exporter (layout, frozen days, block links, revisions, redaction, degradation)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from starlette.testclient import TestClient

ANIMA_ROOT = Path(__file__).resolve().parent.parent
if str(ANIMA_ROOT) not in sys.path:
    sys.path.insert(0, str(ANIMA_ROOT))
SRC_DIR = ANIMA_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from companion_engine import BrainConfig, CompanionBrain, CommitRequest, Settings
from companion_engine.automation import MemoryAutomation
from companion_engine.models import Appraisal, MemoryDetails, MemoryDraft
import obsidian_exporter as obs_mod
import xiaozhi_anima_service as svc


class TestObsidianExporter(unittest.TestCase):
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
        self.tz = ZoneInfo("Asia/Shanghai")

    def tearDown(self):
        self.brain.close()
        self._tmp.cleanup()

    def _commit_turn(
        self,
        req_id: str,
        user_msg: str,
        reply: str,
        *,
        occurred_at: float | None = None,
        created_at: float | None = None,
        source: str = "esp32",
        used_tools: bool = False,
        emotion_label: str | None = None,
        memories: list[MemoryDraft] | None = None,
    ) -> str:
        eng = self.brain.engine
        ctx = eng.prepare_turn(svc.DEFAULT_CHARACTER_ID, user_msg, request_id=req_id)
        appraisal = (
            Appraisal(emotion=emotion_label, intensity=2, source_quote=user_msg[: min(len(user_msg), 40)])
            if emotion_label
            else None
        )
        eng.commit_turn(
            svc.DEFAULT_CHARACTER_ID,
            ctx.turn_id,
            CommitRequest(
                assistant_reply=reply,
                memories=memories or [],
                appraisal=appraisal,
            ),
        )
        if created_at is not None:
            with eng._transaction():
                eng.db.execute("UPDATE turns SET created_at=? WHERE id=?", (float(created_at), ctx.turn_id))
        if occurred_at is not None or source != "esp32" or used_tools:
            with eng._transaction():
                svc._upsert_turn_meta_tx(
                    eng,
                    ctx.turn_id,
                    {
                        "occurred_at": occurred_at,
                        "source": source,
                        "device_id": "aa:bb:cc:dd:ee:ff",
                        "used_tools": used_tools,
                    },
                    created_at or time.time(),
                )
        return ctx.turn_id

    def _make_exporter(self, *, default_enabled: bool = True, export_dir: Path | None = None):
        return obs_mod.ObsidianExporter(
            self.brain,
            character_id=svc.DEFAULT_CHARACTER_ID,
            export_dir=export_dir or self.vault_dir,
            host_export_dir=r"D:\Obsidian\XiaozhiMemory",
            config_path=self.cfg_path,
            default_enabled=default_enabled,
            snapshot_interval_seconds=600,
        )

    def test_vault_layout_backfill_and_idempotent_hash_dedup(self):
        ts_day1 = datetime(2026, 11, 18, 21, 30, 0, tzinfo=self.tz).timestamp()
        ts_day2 = datetime(2026, 11, 19, 9, 15, 0, tzinfo=self.tz).timestamp()

        t1 = self._commit_turn(
            "xz-t1",
            "我平时喜欢喝无糖乌龙茶，明天要去面试有点紧张",
            "哥哥别紧张，无糖乌龙茶提神又清爽，明天一定顺利。",
            occurred_at=ts_day1,
            created_at=ts_day1,
            source="esp32",
            emotion_label="warm",
            memories=[
                MemoryDraft(
                    key="pref_tea",
                    text="喜欢喝无糖乌龙茶",
                    category="preference",
                    source_quote="喜欢喝无糖乌龙茶",
                ),
                MemoryDraft(
                    key="loop_interview",
                    text="明天去参加面试",
                    category="event",
                    source_quote="明天要去面试",
                    details=MemoryDetails(
                        subject="user",
                        temporal_kind="event",
                        importance=8,
                        loop_status="open",
                        followup="ask",
                    ),
                ),
            ],
        )
        t2 = self._commit_turn(
            "studio-t2",
            "在网页里跟你打个招呼",
            "欢迎回来哥哥，网页这边我也在哦。",
            occurred_at=ts_day2,
            created_at=ts_day2,
            source="studio",
            emotion_label="happy",
        )

        # Insert a core document and an episode document directly
        eng = self.brain.engine
        with eng._transaction():
            eng.db.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?)",
                (
                    "core1111-2222-3333-4444-555555555555",
                    eng.owner,
                    svc.DEFAULT_CHARACTER_ID,
                    "core",
                    "哥哥喜欢无糖乌龙茶，最近在准备面试。",
                    json.dumps([f"{t1}:user"]),
                    ts_day2,
                ),
            )
            eng.db.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?)",
                (
                    "ep999999-2222-3333-4444-555555555555",
                    eng.owner,
                    svc.DEFAULT_CHARACTER_ID,
                    "episode",
                    "聊了喝茶习惯与面试准备。",
                    json.dumps([f"{t1}:user", f"{t2}:user"]),
                    ts_day2,
                ),
            )
            eng.db.execute(
                "INSERT INTO document_metadata VALUES(?,?)",
                (
                    "ep999999-2222-3333-4444-555555555555",
                    json.dumps({"importance": 7, "covers_from": ts_day1, "covers_to": ts_day2}),
                ),
            )

        exporter = self._make_exporter()
        res1 = exporter.sync_startup_backfill()
        self.assertTrue(res1["ok"])
        self.assertGreater(res1["written"], 0)

        # Check dedicated vault layout under vault root
        expected_paths = [
            self.vault_dir / "总览.md",
            self.vault_dir / "对话" / "2026" / "11" / "2026-11-18.md",
            self.vault_dir / "对话" / "2026" / "11" / "2026-11-19.md",
            self.vault_dir / "记忆" / "芽衣记得的事.md",
            self.vault_dir / "记忆" / "未完待续.md",
            self.vault_dir / "记忆" / "关系与情绪状态.md",
            self.vault_dir / "记忆" / "核心人物笔记.md",
            self.vault_dir / "记忆" / "互动片段" / "2026-11-18-片段-ep999999.md",
        ]
        for p in expected_paths:
            self.assertTrue(p.is_file(), f"Missing exported file: {p}")

        # Verify Companion Studio label & ESP32 label
        day1_txt = (self.vault_dir / "对话/2026/11/2026-11-18.md").read_text(encoding="utf-8")
        day2_txt = (self.vault_dir / "对话/2026/11/2026-11-19.md").read_text(encoding="utf-8")
        self.assertIn("ESP32 语音", day1_txt)
        self.assertIn(f"^t-{t1.replace('-', '')[:8]}", day1_txt)
        self.assertIn("Companion Studio", day2_txt)

        # Open loops file contains the open item with block link
        loops_txt = (self.vault_dir / "记忆/未完待续.md").read_text(encoding="utf-8")
        self.assertIn("- [ ] 明天去参加面试", loops_txt)
        self.assertIn(f"[[对话/2026/11/2026-11-18#^t-{t1.replace('-', '')[:8]}|原话]]", loops_txt)

        # Core & episode notes exported
        core_txt = (self.vault_dir / "记忆/核心人物笔记.md").read_text(encoding="utf-8")
        self.assertIn("哥哥喜欢无糖乌龙茶", core_txt)
        ep_txt = (self.vault_dir / "记忆/互动片段/2026-11-18-片段-ep999999.md").read_text(encoding="utf-8")
        self.assertIn("聊了喝茶习惯与面试准备", ep_txt)

        # Second full rebuild must hash-skip EVERY single file (idempotency!)
        res2 = exporter.rebuild_all()
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["written"], 0)
        self.assertEqual(res2["skipped"], len(expected_paths))

    def test_frozen_historical_day_policy_delayed_turn_and_manual_rebuild(self):
        past_ts = datetime(2025, 10, 1, 20, 0, 0, tzinfo=self.tz).timestamp()
        today_ts = time.time()
        today_str = datetime.fromtimestamp(today_ts, self.tz).strftime("%Y-%m-%d")

        self._commit_turn("xz-past-1", "十月一号打卡", "国庆快乐哥哥！", occurred_at=past_ts, created_at=past_ts)
        self._commit_turn("xz-today-1", "今天聊聊天", "好呀，随时陪着哥哥。", occurred_at=today_ts, created_at=today_ts)

        exporter = self._make_exporter()
        r1 = exporter.sync_startup_backfill()
        self.assertTrue(r1["ok"])

        past_file = self.vault_dir / "对话/2025/10/2025-10-01.md"
        sentinel = "\n<!-- FROZEN_SENTINEL -->\n"
        past_file.write_text(past_file.read_text(encoding="utf-8") + sentinel, encoding="utf-8")

        # 1. Another startup backfill MUST keep existing past day frozen
        exporter.sync_startup_backfill()
        self.assertIn("FROZEN_SENTINEL", past_file.read_text(encoding="utf-8"))

        # 2. Normal incremental sync for today MUST keep past day frozen
        self._commit_turn("xz-today-2", "今天第二句", "嗯，我在听呢。", occurred_at=today_ts + 5, created_at=today_ts + 5)
        exporter.sync_incremental({today_str})
        self.assertIn("FROZEN_SENTINEL", past_file.read_text(encoding="utf-8"))

        # 3. A delayed turn whose occurred_at lands on 2025-10-01 updates ONLY that past day
        self._commit_turn(
            "xz-past-delayed",
            "十月一号补单消息",
            "这条迟到的消息也好好收下啦。",
            occurred_at=past_ts + 120,
            created_at=today_ts + 10,
        )
        exporter.sync_incremental({"2025-10-01"})
        updated_past = past_file.read_text(encoding="utf-8")
        self.assertNotIn("FROZEN_SENTINEL", updated_past)
        self.assertIn("十月一号补单消息", updated_past)

        # 4. Manual full rebuild refreshes past days even without new turns
        past_file.write_text(updated_past + sentinel, encoding="utf-8")
        r_full = exporter.rebuild_all()
        self.assertTrue(r_full["ok"])
        self.assertNotIn("FROZEN_SENTINEL", past_file.read_text(encoding="utf-8"))

    def test_cross_midnight_archiving_uses_occurred_at_and_block_id_links(self):
        # Spoke at 23:58:30 on Nov 18, committed after midnight at 00:05:10 on Nov 19!
        occ_ts = datetime(2026, 11, 18, 23, 58, 30, tzinfo=self.tz).timestamp()
        commit_ts = datetime(2026, 11, 19, 0, 5, 10, tzinfo=self.tz).timestamp()

        tid = self._commit_turn(
            "xz-midnight-1",
            "帮我把音量调到三十，对了我不爱吃香菜",
            "好啦，已经调到 30 了，以后做菜也不放香菜。",
            occurred_at=occ_ts,
            created_at=commit_ts,
            used_tools=True,
            emotion_label="tender",
            memories=[
                MemoryDraft(
                    key="dislike_cilantro",
                    text="不爱吃香菜",
                    category="preference",
                    source_quote="不爱吃香菜",
                )
            ],
        )

        exporter = self._make_exporter()
        res = exporter.rebuild_all()
        self.assertTrue(res["ok"])

        nov18 = self.vault_dir / "对话/2026/11/2026-11-18.md"
        nov19 = self.vault_dir / "对话/2026/11/2026-11-19.md"
        self.assertTrue(nov18.is_file())
        self.assertFalse(nov19.exists())

        txt18 = nov18.read_text(encoding="utf-8")
        blk_id = f"t-{tid.replace('-', '')[:8]}"
        self.assertIn("## 23:58:30 · ESP32 语音 · 🔧 调用工具", txt18)
        self.assertIn(f"^{blk_id}", txt18)

        mem_txt = (self.vault_dir / "记忆/芽衣记得的事.md").read_text(encoding="utf-8")
        self.assertIn(f"[[对话/2026/11/2026-11-18#^{blk_id}|原话]]", mem_txt)

    def test_revisions_and_superseded_memories_collapsed_at_bottom(self):
        ts1 = datetime(2026, 11, 10, 14, 0, 0, tzinfo=self.tz).timestamp()
        ts2 = datetime(2026, 11, 15, 15, 0, 0, tzinfo=self.tz).timestamp()

        t1 = self._commit_turn(
            "xz-rev-1",
            "我以前喜欢喝甜咖啡，目前住在上海",
            "记下啦哥哥。",
            occurred_at=ts1,
            created_at=ts1,
            memories=[
                MemoryDraft(key="coffee_pref", text="喜欢喝甜咖啡", category="preference", source_quote="喜欢喝甜咖啡"),
                MemoryDraft(key="city_old", text="目前住在上海", category="identity", source_quote="目前住在上海"),
            ],
        )
        t2 = self._commit_turn(
            "xz-rev-2",
            "现在改喝无糖乌龙茶了，而且已经搬到长沙",
            "那我把记录都更新过来哦。",
            occurred_at=ts2,
            created_at=ts2,
            memories=[
                MemoryDraft(key="city_new", text="已经搬到长沙", category="identity", source_quote="已经搬到长沙"),
            ],
        )

        eng = self.brain.engine
        mems = {m.key: m for m in eng.memories(svc.DEFAULT_CHARACTER_ID)}
        # 1. In-place revision (populates `revisions` table)
        eng.correct_memory(
            svc.DEFAULT_CHARACTER_ID,
            mems["coffee_pref"].id,
            MemoryDraft(key="coffee_pref", text="改喝无糖乌龙茶", category="preference", source_quote="改喝无糖乌龙茶"),
            expected_revision=1,
        )
        # 2. Cross-key lifecycle retirement (populates `fact_lifecycle.invalidation`)
        old_city = mems["city_old"]
        new_city = mems["city_new"]
        retired_life = old_city.lifecycle.model_copy(
            update={
                "invalidation": "superseded",
                "invalidated_at": ts2,
                "superseded_by": new_city.id,
            }
        )
        with eng._transaction():
            eng.db.execute(
                "UPDATE fact_lifecycle SET data=? WHERE memory_id=?",
                (retired_life.model_dump_json(), old_city.id),
            )

        exporter = self._make_exporter()
        exporter.rebuild_all()

        mem_md = (self.vault_dir / "记忆/芽衣记得的事.md").read_text(encoding="utf-8")
        marker = "> [!abstract]- 历史修订与已更正记忆"
        self.assertIn(marker, mem_md)
        main_part, collapsed_part = mem_md.split(marker, 1)

        # Main section must ONLY have active facts
        self.assertIn("改喝无糖乌龙茶", main_part)
        self.assertIn("已经搬到长沙", main_part)
        self.assertNotIn("喜欢喝甜咖啡", main_part)
        self.assertNotIn("目前住在上海", main_part)

        # Collapsed section must have BOTH revised and superseded facts with strikethrough & original block links
        blk1 = f"t-{t1.replace('-', '')[:8]}"
        self.assertIn("~~喜欢喝甜咖啡~~", collapsed_part)
        self.assertIn("现为：改喝无糖乌龙茶", collapsed_part)
        self.assertIn("~~目前住在上海~~", collapsed_part)
        self.assertIn("现为：已经搬到长沙", collapsed_part)
        self.assertIn(f"[[对话/2026/11/2026-11-10#^{blk1}|原话]]", collapsed_part)
        self.assertTrue(t2)

    def test_credential_redaction_preserves_normal_chinese_chat(self):
        ts = datetime(2026, 11, 19, 18, 0, 0, tzinfo=self.tz).timestamp()
        self._commit_turn(
            "xz-sec-1",
            "今天在长沙喝了无糖乌龙茶，顺便聊了 token 计费和 [安全挡]。测试秘钥 Bearer sk-testsecret1234567890 和 api_key=AIzaSySecretKey1234567890 以及 password=mySecretPass99 和 https://admin:pw123@api.test/v1",
            "[gentle] 好的哥哥，我帮你在 [安全挡] 里处理，不泄露 sk-anothersecret9876543210。",
            occurred_at=ts,
            created_at=ts,
            memories=[
                MemoryDraft(
                    key="tea_chat",
                    text="在长沙喝无糖乌龙茶，不要记录 password=mySecretPass99",
                    category="preference",
                    source_quote="password=mySecretPass99",
                )
            ],
        )

        exporter = self._make_exporter()
        res = exporter.rebuild_all()
        self.assertTrue(res["ok"])

        all_md = ""
        for md_file in self.vault_dir.rglob("*.md"):
            all_md += md_file.read_text(encoding="utf-8") + "\n"

        for secret in (
            "sk-testsecret1234567890",
            "AIzaSySecretKey1234567890",
            "mySecretPass99",
            "admin:pw123",
            "sk-anothersecret9876543210",
        ):
            self.assertNotIn(secret, all_md, f"Secret leaked into Obsidian Markdown: {secret}")

        self.assertIn("[REDACTED]", all_md)
        # Normal Chinese text & legitimate brackets untouched, while [gentle] emotion tag stripped
        day_md = (self.vault_dir / "对话/2026/11/2026-11-19.md").read_text(encoding="utf-8")
        self.assertIn("今天在长沙喝了无糖乌龙茶，顺便聊了 token 计费和 [安全挡]", day_md)
        self.assertNotIn("[gentle]", day_md)
        self.assertIn("在 [安全挡] 里处理", day_md)

        # Normal English "basic <word>" must NOT be falsely redacted, while real Basic auth creds ARE redacted
        self.assertEqual(
            obs_mod.redact_credentials("I am doing basic training and basic English"),
            "I am doing basic training and basic English",
        )
        self.assertEqual(
            obs_mod.redact_credentials("Authorization: Basic dXNlcjpwYXNz"),
            "Authorization: Basic [REDACTED]",
        )
        self.assertEqual(
            obs_mod.redact_credentials("basic dXNlcjpwYXNzMTIzNA=="),
            "basic [REDACTED]",
        )

    def test_unwritable_vault_graceful_degradation(self):
        # 1. File blocking directory creation fails even under root in Docker
        blocker = self.tmp_dir / "not_a_directory"
        blocker.write_text("occupied", encoding="utf-8")
        exporter = self._make_exporter(export_dir=blocker)
        res = exporter.rebuild_all()
        self.assertFalse(res["ok"])
        self.assertFalse(res["writable"])
        self.assertTrue(exporter.status()["last_error"])

        # 2. Mid-write PermissionError degrades cleanly without raising
        exporter2 = self._make_exporter()
        with patch.object(obs_mod.os, "replace", side_effect=PermissionError("Read-only filesystem")):
            res2 = exporter2.rebuild_all()
        self.assertFalse(res2["ok"])
        self.assertFalse(res2["writable"])
        self.assertIn("Read-only filesystem", res2["error"])

    def test_obsidian_http_endpoints_status_toggle_rebuild(self):
        token = "obs-test-token-" + ("c" * 24)
        ts = datetime(2026, 11, 19, 10, 0, 0, tzinfo=self.tz).timestamp()
        self._commit_turn("xz-http-1", "早安芽衣", "早安哥哥！", occurred_at=ts, created_at=ts)

        exporter = self._make_exporter(default_enabled=False)
        frontend_override = svc.FRONTEND_DIR if (svc.FRONTEND_DIR / "index.html").is_file() else None
        with patch.object(svc, "FRONTEND_DIR", frontend_override):
            app = svc.create_combined_asgi_app(self.brain, token, obsidian_exporter=exporter)
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                headers = {"Authorization": f"Bearer {token}"}

                # Unauthorized rejected
                self.assertEqual(client.get("/xiaozhi/obsidian/status").status_code, 401)

                # Initial status disabled, vault not written yet
                st0 = client.get("/xiaozhi/obsidian/status", headers=headers)
                self.assertEqual(st0.status_code, 200)
                self.assertFalse(st0.json()["enabled"])
                self.assertEqual(st0.json()["host_export_dir"], r"D:\Obsidian\XiaozhiMemory")
                self.assertFalse((self.vault_dir / "总览.md").exists())

                # Toggle ON -> persists config and backfills immediately
                r_on = client.post("/xiaozhi/obsidian/config", headers=headers, json={"enabled": True})
                self.assertEqual(r_on.status_code, 200)
                self.assertTrue(r_on.json()["enabled"])
                self.assertTrue((self.vault_dir / "总览.md").is_file())
                self.assertTrue(self.cfg_path.is_file())
                self.assertTrue(json.loads(self.cfg_path.read_text(encoding="utf-8"))["enabled"])

                # Manual rebuild -> 0 written because hashes already match
                r_reb = client.post("/xiaozhi/obsidian/rebuild", headers=headers, json={})
                self.assertEqual(r_reb.status_code, 200)
                self.assertTrue(r_reb.json()["ok"])
                self.assertEqual(r_reb.json()["written"], 0)
                self.assertGreater(r_reb.json()["skipped"], 0)

                # Health endpoint includes ingest + obsidian summaries
                h = client.get("/xiaozhi/health").json()
                self.assertEqual(h["status"], "ok")
                self.assertIn("ingest", h)
                self.assertIn("dropped_total", h["ingest"])
                self.assertIn("obsidian", h)
                self.assertTrue(h["obsidian"]["enabled"])

    def test_regression_frozen_day_on_cold_poll_studio_shutdown_multiline_callout_and_failed_backfill(self):
        import asyncio

        past_ts = datetime(2025, 10, 1, 20, 0, 0, tzinfo=self.tz).timestamp()
        today_ts = time.time()
        today_str = datetime.fromtimestamp(today_ts, self.tz).strftime("%Y-%m-%d")

        self._commit_turn(
            "xz-reg-past",
            "国庆打卡",
            "国庆快乐哥哥！",
            occurred_at=past_ts,
            created_at=past_ts,
        )
        exporter = self._make_exporter()
        self.assertTrue(exporter.sync_startup_backfill()["ok"])

        # 1. Cold exporter (empty _known_turn_sigs) running sync_if_db_changed must NOT overwrite frozen past day
        past_file = self.vault_dir / "对话/2025/10/2025-10-01.md"
        past_file.write_text(past_file.read_text(encoding="utf-8") + "\n<!-- COLD_FROZEN -->\n", encoding="utf-8")
        cold_exporter = self._make_exporter()
        cold_exporter.sync_if_db_changed(reason="toggle_enabled")
        self.assertIn("COLD_FROZEN", past_file.read_text(encoding="utf-8"))

        # 2. Companion Studio write right before shutdown (notify_db_change only) must still flush today's dialogue log
        self._commit_turn(
            "studio-shutdown-1",
            "网页端关停前最后一句",
            "网页端关停前回复收到！",
            occurred_at=today_ts,
            created_at=today_ts,
            source="studio",
        )

        async def _run_studio_shutdown():
            runner = obs_mod.ObsidianSyncRunner(cold_exporter, debounce_seconds=30.0)
            runner._startup_done = True
            runner._wake_event = asyncio.Event()
            runner._stop_event = asyncio.Event()
            runner.notify_db_change()
            await runner.stop()

        asyncio.run(_run_studio_shutdown())
        today_file = self.vault_dir / (obs_mod._dialogue_rel_path(today_str) + ".md")
        self.assertTrue(today_file.is_file())
        self.assertIn("网页端关停前最后一句", today_file.read_text(encoding="utf-8"))

        # 3. Failed startup backfill / rebuild must NOT mark _startup_done=True or discard _dirty_dates
        blocker = self.tmp_dir / "blocked_vault_file"
        blocker.write_text("occupied", encoding="utf-8")
        bad_exporter = self._make_exporter(default_enabled=False, export_dir=blocker)

        async def _run_failed_backfill():
            runner = obs_mod.ObsidianSyncRunner(bad_exporter, debounce_seconds=0.1)
            runner._dirty_dates.add(today_str)
            await runner.set_enabled_async(True)
            done_after_toggle = runner._startup_done
            reb_res = await runner.rebuild_now()
            return done_after_toggle, runner._startup_done, reb_res["ok"], set(runner._dirty_dates)

        done1, done2, reb_ok, remaining_dirty = asyncio.run(_run_failed_backfill())
        self.assertFalse(done1)
        self.assertFalse(done2)
        self.assertFalse(reb_ok)
        self.assertIn(today_str, remaining_dirty)

        # 4. Multi-line memory/revision text must stay inside > [!abstract]- callout, and double-retired follows superseded_by
        self._commit_turn(
            "xz-reg-ml",
            "喜欢多行测试",
            "好的",
            occurred_at=today_ts + 2,
            created_at=today_ts + 2,
            memories=[
                MemoryDraft(key="ml_old", text="旧第一行\n旧第二行", category="preference", source_quote="多行"),
                MemoryDraft(key="ml_final", text="最终有效结论", category="preference", source_quote="多行"),
            ],
        )
        eng = self.brain.engine
        mems = {m.key: m for m in eng.memories(svc.DEFAULT_CHARACTER_ID)}
        eng.correct_memory(
            svc.DEFAULT_CHARACTER_ID,
            mems["ml_old"].id,
            MemoryDraft(key="ml_old", text="中间过渡第一行\n中间过渡第二行", category="preference", source_quote="多行"),
            expected_revision=1,
        )
        # Now retire ml_old in favor of ml_final
        mems2 = {m.key: m for m in eng.memories(svc.DEFAULT_CHARACTER_ID)}
        retired = mems2["ml_old"].lifecycle.model_copy(
            update={"invalidation": "superseded", "invalidated_at": today_ts + 5, "superseded_by": mems2["ml_final"].id}
        )
        with eng._transaction():
            eng.db.execute("UPDATE fact_lifecycle SET data=? WHERE memory_id=?", (retired.model_dump_json(), mems2["ml_old"].id))

        self.assertTrue(exporter.rebuild_all()["ok"])
        mem_md = (self.vault_dir / "记忆/芽衣记得的事.md").read_text(encoding="utf-8")
        after_callout = mem_md.split("> [!abstract]-", 1)[1]
        escaped_lines = [ln for ln in after_callout.splitlines()[1:] if ln.strip() and not ln.startswith(">")]
        self.assertEqual(escaped_lines, [], f"Multi-line revision broke Obsidian callout: {escaped_lines}")
        self.assertIn("~~旧第一行 旧第二行~~", after_callout)
        self.assertIn("现为：最终有效结论", after_callout)
        self.assertNotIn("现为：中间过渡第一行", after_callout)

    def test_multiline_turns_neutralize_markdown_block_breakers_for_block_id(self):
        import re

        ts1 = datetime(2026, 11, 19, 20, 0, 0, tzinfo=self.tz).timestamp()
        ts2 = datetime(2026, 11, 19, 20, 1, 0, tzinfo=self.tz).timestamp()
        ts3 = datetime(2026, 11, 19, 20, 2, 0, tzinfo=self.tz).timestamp()

        user_nasty = (
            "帮我看一下这些 Markdown 片段：\n"
            "\n"
            "---\n"
            "===\n"
            "***\n"
            "___\n"
            "# 一级标题\n"
            "  ## 缩进二级标题\n"
            "- 无序项A\n"
            "* 无序项B\n"
            "+ 无序项C\n"
            "1. 有序项一\n"
            "2) 有序项二\n"
            "> 普通引用\n"
            "> [!warning] 警告卡片\n"
            "```python\n"
            "# 代码内注释\n"
            "- 代码内横线\n"
            "print('ok')\n"
            "```\n"
            "A | B\n"
            "--- | ---\n"
            "| 1 | 2 |\n"
            "<div>html block</div>\n"
            "<!-- html comment -->\n"
            "$$\n"
            "E = mc^2\n"
            "$$\n"
            "[^1]: 脚注\n"
            "^fake-id\n"
            "%% 隐藏注释\n"
            "正常段落\u2028---"
        )
        reply_nasty = (
            "收到啦哥哥！下面是我整理的要点：\n"
            "---\n"
            "## 回复小节\n"
            "- 第一条\n"
            "1. 步骤一\n"
            "```js\n"
            "const unclosed = 1;\n"
            "$$\n"
            "x + y\n"
            "*悄悄靠近*\n"
            "#日常标签"
        )
        t1 = self._commit_turn(
            "studio-md-1",
            user_nasty,
            reply_nasty,
            occurred_at=ts1,
            created_at=ts1,
            source="studio",
            emotion_label="warm",
            memories=[
                MemoryDraft(
                    key="md_breaker_fact",
                    text="喜欢测试边界用例",
                    category="preference",
                    source_quote="Markdown 片段",
                )
            ],
        )
        # Turn starting directly with fenced code / math on line 0
        t2 = self._commit_turn(
            "studio-md-2",
            "```python\nprint(42)\n```",
            "$$\na^2 + b^2 = c^2\n$$",
            occurred_at=ts2,
            created_at=ts2,
            source="studio",
            emotion_label="calm",
        )
        # Follow-up normal turn after unclosed fences/math MUST remain cleanly intact
        t3 = self._commit_turn(
            "studio-md-3",
            "晚安芽衣",
            "晚安哥哥，好梦。",
            occurred_at=ts3,
            created_at=ts3,
            source="studio",
            emotion_label="tender",
        )

        exporter = self._make_exporter()
        res = exporter.rebuild_all()
        self.assertTrue(res["ok"])

        day_md = (self.vault_dir / "对话/2026/11/2026-11-19.md").read_text(encoding="utf-8")
        # Extract each turn section starting from `## `
        sections = re.split(r"(?m)^(?=## )", day_md)
        turn_sections = [s.strip() for s in sections if s.startswith("## ")]
        self.assertEqual(len(turn_sections), 3)

        expected_blks = [f"t-{tid.replace('-', '')[:8]}" for tid in (t1, t2, t3)]
        block_breaker_re = re.compile(
            r"^[ \t]*(?:"
            r"`{3,}|~{3,}|\$\$|#{1,6}(?:[ \t]|$)|>|[-*+](?:[ \t]|$)|"
            r"(?:-[ \t]*){2,}$|(?:\*[ \t]*){3,}$|(?:_[ \t]*){3,}$|=+[ \t]*$|"
            r"\||[:|-][\s:|-]*$|\d{1,9}[.)](?:[ \t]|$)|<|\[\^[^\]]+\]:|\^|%%"
            r")"
        )
        for sec, blk in zip(turn_sections, expected_blks):
            sec_lines = sec.splitlines()
            self.assertTrue(sec_lines[0].startswith("## "))
            body_lines = sec_lines[1:]
            # No blank line allowed between **我**, **芽衣**, and ^t-xxxxxxxx
            self.assertTrue(all(ln.strip() for ln in body_lines), f"Blank line split turn block: {body_lines}")
            self.assertTrue(body_lines[0].startswith("**我**："))
            self.assertEqual(body_lines[-1], f"^{blk}")
            # No unescaped Markdown block-level starter on any continuation line before ^t-xxxxxxxx
            for ln in body_lines[1:-1]:
                if ln.startswith("**芽衣**"):
                    continue
                self.assertIsNone(
                    block_breaker_re.match(ln),
                    f"Unescaped Markdown block-level breaker could detach ^{blk}: {ln!r}",
                )

        # Normal inline emphasis & Obsidian #tags on continuation lines stay unescaped
        self.assertIn("\n*悄悄靠近*\n", turn_sections[0])
        self.assertIn("\n#日常标签\n", turn_sections[0])
        # Line-0 fences and math delimiters are escaped to stay balanced
        self.assertIn(r"**我**：\`\`\`python", turn_sections[1])
        self.assertIn(r"**芽衣** · calm：\$\$", turn_sections[1])
        # Follow-up turn is completely unaffected
        self.assertIn("**我**：晚安芽衣", turn_sections[2])
        self.assertIn("**芽衣** · tender：晚安哥哥，好梦。", turn_sections[2])
        # Single-line snippet in relationship note stays readable without continuation escapes
        rel_md = (self.vault_dir / "记忆/关系与情绪状态.md").read_text(encoding="utf-8")
        self.assertIn("帮我看一下这些 Markdown 片段", rel_md)

    def test_inline_span_swallowers_and_trailing_backslash_do_not_corrupt_turn_or_block_id(self):
        # 1. Line-0 & mid-line <!-- HTML comments, raw-text HTML tags, and %% Obsidian comments
        self.assertEqual(obs_mod.clean_dialogue_text("<!-- unclosed"), r"\<!-- unclosed")
        self.assertEqual(obs_mod.clean_dialogue_text("看看 <!-- 忘记闭合"), r"看看 \<!-- 忘记闭合")
        self.assertEqual(obs_mod.clean_dialogue_text("<style>body{display:none}"), r"\<style>body{display:none}")
        self.assertEqual(obs_mod.clean_dialogue_text("测试 <script>alert(1)"), r"测试 \<script>alert(1)")
        self.assertEqual(obs_mod.clean_dialogue_text("%% 隐藏了"), r"\%\% 隐藏了")
        self.assertEqual(obs_mod.clean_dialogue_text("折扣 50%% 真香"), r"折扣 50\%\% 真香")
        self.assertEqual(obs_mod.clean_dialogue_text(r"路径\\%%注释"), r"路径\\\%\%注释")

        # 2. Unmatched / cross-line $$ escaped, while balanced single-line $$E=mc^2$$ stays intact
        self.assertEqual(obs_mod.clean_dialogue_text("公式开始 $$\nE=mc^2"), "公式开始 \\$\\$\nE=mc^2")
        self.assertEqual(obs_mod.clean_dialogue_text("公式开始 $$\nE=mc^2 $$"), "公式开始 \\$\\$\nE=mc^2 \\$\\$")
        self.assertEqual(obs_mod.clean_dialogue_text("公式 $$E=mc^2$$ 很美"), "公式 $$E=mc^2$$ 很美")

        # 3. Unclosed mid-line backticks escaped so they cannot pair across `\n**芽衣**`,
        #    while balanced inline `code` spans (including %% / <!-- inside backticks) stay untouched
        self.assertEqual(obs_mod.clean_dialogue_text("看 ```python\nprint(1)"), "看 \\`\\`\\`python\nprint(1)")
        self.assertEqual(obs_mod.clean_dialogue_text("路径 `foo"), r"路径 \`foo")
        self.assertEqual(obs_mod.clean_dialogue_text("用 `a %% b` 和 `<!-- x` 就行"), "用 `a %% b` 和 `<!-- x` 就行")

        # 4. Trailing odd backslashes doubled so they render literally and never escape `\n^t-xxxxxxxx`
        self.assertEqual(obs_mod.clean_dialogue_text(r"在 E:\test" + "\\"), r"在 E:\test\\")
        self.assertEqual(obs_mod.clean_dialogue_text(r"在 E:\test\\\ ".rstrip()), r"在 E:\test\\\\")
        self.assertEqual(obs_mod.clean_dialogue_text(r"已有双斜杠\\"), r"已有双斜杠\\")

        # 5. End-to-end daily note export keeps speaker labels, ^t-xxxxxxxx, and next turn intact
        ts1 = datetime(2026, 11, 20, 14, 0, 0, tzinfo=self.tz).timestamp()
        ts2 = datetime(2026, 11, 20, 14, 1, 0, tzinfo=self.tz).timestamp()
        t1 = self._commit_turn(
            "studio-span-1",
            "看 <!-- 未闭合 和 50%% 还有 ```python\n公式 $$\nE=mc^2",
            "还有 `unclosed\n路径在 E:\\test\\",
            occurred_at=ts1,
            created_at=ts1,
            source="studio",
            emotion_label="calm",
        )
        t2 = self._commit_turn(
            "studio-span-2",
            "下一轮正常对话",
            "收到啦哥哥。",
            occurred_at=ts2,
            created_at=ts2,
            source="studio",
            emotion_label="warm",
        )
        exporter = self._make_exporter()
        self.assertTrue(exporter.rebuild_all()["ok"])
        day_md = (self.vault_dir / "对话/2026/11/2026-11-20.md").read_text(encoding="utf-8")
        blk1 = f"t-{t1.replace('-', '')[:8]}"
        blk2 = f"t-{t2.replace('-', '')[:8]}"
        self.assertIn(r"\<!-- 未闭合", day_md)
        self.assertIn(r"50\%\%", day_md)
        self.assertIn(r"\`\`\`python", day_md)
        self.assertIn(r"\$\$", day_md)
        self.assertIn(r"E:\test\\", day_md)
        self.assertIn(r"\`unclosed", day_md)
        self.assertIn(f"\n^{blk1}\n", day_md)
        self.assertIn(f"\n^{blk2}\n", day_md)

    def test_empty_core_note_and_empty_vault_frontmatter_clean_yaml(self):
        # No turns, no memories, no core documents
        exporter = self._make_exporter()
        res = exporter.rebuild_all()
        self.assertTrue(res["ok"])

        core_md = (self.vault_dir / "记忆/核心人物笔记.md").read_text(encoding="utf-8")
        self.assertIn("updated: null\n", core_md)
        self.assertNotIn("updated: --", core_md)
        self.assertIn("documents: 0\n", core_md)

        # Snapshot files with 0 records must use clean YAML `updated: null` (never bare `--`)
        for rel in (
            "记忆/芽衣记得的事.md",
            "记忆/未完待续.md",
            "记忆/核心人物笔记.md",
        ):
            txt = (self.vault_dir / rel).read_text(encoding="utf-8")
            self.assertTrue(txt.startswith("---\n"), f"Missing frontmatter in {rel}")
            fm = txt.split("---\n", 2)[1]
            self.assertNotIn("updated: --", fm, f"Bare `--` leaked into frontmatter of {rel}")
            self.assertIn("updated: null", fm, f"Expected `updated: null` in empty {rel}")

        # Overview & relationship notes have a valid character state timestamp (never bare `--`)
        for rel in ("总览.md", "记忆/关系与情绪状态.md"):
            txt = (self.vault_dir / rel).read_text(encoding="utf-8")
            fm = txt.split("---\n", 2)[1]
            self.assertNotIn("updated: --", fm, f"Bare `--` leaked into frontmatter of {rel}")
            self.assertRegex(fm, r'(?m)^updated: (?:"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"|null)$')

        # _yaml_scalar safely quotes YAML indicator prefixes, reserved literals, control chars, and numeric/date literals
        self.assertEqual(obs_mod._yaml_scalar("--"), '"--"')
        self.assertEqual(obs_mod._yaml_scalar("..."), '"..."')
        self.assertEqual(obs_mod._yaml_scalar("- item"), '"- item"')
        self.assertEqual(obs_mod._yaml_scalar("true"), '"true"')
        self.assertEqual(obs_mod._yaml_scalar("null"), '"null"')
        self.assertEqual(obs_mod._yaml_scalar("  padded  "), '"  padded  "')
        self.assertEqual(obs_mod._yaml_scalar("a\tb"), '"a\\tb"')
        self.assertEqual(obs_mod._yaml_scalar("a\x00b"), '"a\\u0000b"')
        self.assertEqual(obs_mod._yaml_scalar("123"), '"123"')
        self.assertEqual(obs_mod._yaml_scalar(".inf"), '".inf"')
        self.assertEqual(obs_mod._yaml_scalar("2026-11-19"), '"2026-11-19"')
        self.assertEqual(obs_mod._yaml_scalar("雷电芽衣"), "雷电芽衣")


if __name__ == "__main__":
    unittest.main(verbosity=2)

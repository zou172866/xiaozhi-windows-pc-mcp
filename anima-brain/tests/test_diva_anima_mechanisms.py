"""Comprehensive unit & HTTP integration tests for Anima Brain mechanisms:
- Mechanism 1: AutoDream + strict Read-Only SQLite Dream Executor (first-hand evidence only, zero auto-publish, CAS baseline)
- Mechanism 2: Anima Brain recall-layer sensitive entity auto-filter
- Mechanism 3: Multi-Space Embedding (model@dim coexistence, write-time dim validation, 401/403 auth breaker, explicit FTS5 degradation)
- Mechanism 5: Offline layered daily & weekly follow-up briefings in Obsidian (low-cost, zero voice push, hash-idempotent)
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr
from starlette.testclient import TestClient

ANIMA_ROOT = Path(__file__).resolve().parent.parent
if str(ANIMA_ROOT) not in sys.path:
    sys.path.insert(0, str(ANIMA_ROOT))
SRC_DIR = ANIMA_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from companion_engine import BrainConfig, CompanionBrain, CommitRequest, Settings
from companion_engine.automation import MemoryAutomation
from companion_engine.media import Embeddings, embedding_breaker
from companion_engine.models import Appraisal, MemoryDetails, MemoryDraft
from companion_engine.providers import ProviderError
import dream_executor
import obsidian_exporter as obs_mod
import recall_sanitizer
import xiaozhi_anima_service as svc


class TestDivaAnimaMechanisms(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.db_path = self.tmp_dir / "brain.sqlite3"
        self.vault_dir = self.tmp_dir / "obsidian_vault"
        self.cfg_path = self.tmp_dir / "obsidian_sync_config.json"
        self.tz = ZoneInfo("Asia/Shanghai")
        embedding_breaker.reset()
        svc._query_embed_lru.clear()
        svc._query_embed_fail_streak = 0
        svc._query_embed_cb_until = 0.0
        svc._query_embed_last_degraded_reason = ""
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
            persona="你是雷电芽衣，性格温柔沉稳。",
            memory_policy="automatic",
            relationship_enabled=True,
            romance_enabled=True,
            timezone="Asia/Shanghai",
        )
        svc.ensure_xiaozhi_turn_meta_table(self.brain.engine)
        svc.ensure_xiaozhi_inbox_table(self.brain.engine)

    def tearDown(self):
        embedding_breaker.reset()
        self.brain.close()
        self._tmp.cleanup()

    def _commit(
        self,
        req_id: str,
        user_msg: str,
        assistant_reply: str,
        *,
        memories: list[MemoryDraft] | None = None,
        occurred_at: float | None = None,
        emotion: str | None = None,
    ) -> str:
        eng = self.brain.engine
        ctx = eng.prepare_turn(svc.DEFAULT_CHARACTER_ID, user_msg, request_id=req_id)
        app = (
            Appraisal(emotion=emotion, intensity=2, source_quote=user_msg[: min(len(user_msg), 40)])
            if emotion
            else None
        )
        eng.commit_turn(
            svc.DEFAULT_CHARACTER_ID,
            ctx.turn_id,
            CommitRequest(assistant_reply=assistant_reply, memories=memories or [], appraisal=app),
        )
        if occurred_at is not None:
            with eng._transaction():
                eng.db.execute("UPDATE turns SET created_at=? WHERE id=?", (float(occurred_at), ctx.turn_id))
                eng.db.execute("UPDATE evidence SET observed_at=? WHERE turn_id=?", (float(occurred_at), ctx.turn_id))
                svc._upsert_turn_meta_tx(
                    eng,
                    ctx.turn_id,
                    {"occurred_at": occurred_at, "source": "esp32", "device_id": "aa:bb:cc:dd:ee:ff"},
                    occurred_at,
                )
        return ctx.turn_id

    def test_m1_readonly_dream_executor_firsthand_only_and_cas_approval(self):
        """Mechanism 1: Genuine read-only SQLite boundary, excludes summaries/self-output, zero auto-publish, CAS approval."""
        # 1. Verify SQLite read-only connection strictly forbids any write at engine level
        analyzer = dream_executor.ReadOnlyDreamAnalyzer(self.db_path, self.brain.engine.owner)
        ro_conn = analyzer._connect_ro()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro_conn.execute("CREATE TABLE should_fail (x INT)")
        finally:
            ro_conn.close()

        # 2. Seed first-hand user evidence + conflicting memories + recursive-bait assistant reply & derived document
        t1 = self._commit(
            "dream-t1",
            "我说的是芽衣，不是牙医，以后说话简短一点，顺便我手机号是 13812345678",
            "好的，不是猫狗是神仙，说话简短一点，以后叫我船长。",
            memories=[
                MemoryDraft(
                    key="home_city_old",
                    text="住在上海浦东",
                    category="identity",
                    source_quote="芽衣",
                    details=MemoryDetails(subject="user", temporal_kind="current", state_key="user:city"),
                )
            ],
        )
        time.sleep(0.02)
        t2 = self._commit(
            "dream-t2",
            "我现在搬去杭州西湖了，另外平时回复简短一点就行",
            "收到，杭州西湖很舒服。",
            memories=[
                MemoryDraft(
                    key="home_city_new",
                    text="现在住在杭州西湖",
                    category="identity",
                    source_quote="杭州西湖",
                    details=MemoryDetails(subject="user", temporal_kind="current", state_key="user:city"),
                )
            ],
        )
        # Insert derived document containing fake correction bait — MUST be ignored by Dream analyzer!
        self.brain.engine.memory.consolidate(
            svc.DEFAULT_CHARACTER_ID,
            "episode",
            "摘要误导：我说的是月亮，不是越亮，平时回复详细一点多讲一点。",
            [f"{t1}:user", f"{t2}:user"],
        )

        mgr = dream_executor.DreamManager(self.brain)
        mems_before = len(self.brain.engine.memories(svc.DEFAULT_CHARACTER_ID))
        run_res = mgr.run_dream_cycle(
            svc.DEFAULT_CHARACTER_ID,
            reason="manual",
            existing_asr_corrections={},
            known_terms=["雷电芽衣"],
        )
        self.assertTrue(run_res["ok"])
        self.assertGreaterEqual(run_res["created"], 3)
        # Zero auto-publish: memories count MUST remain identical!
        self.assertEqual(len(self.brain.engine.memories(svc.DEFAULT_CHARACTER_ID)), mems_before)

        props = run_res["proposals"]
        kinds = {p["kind"] for p in props}
        self.assertEqual(kinds, {"asr_correction", "memory_merge", "style_adjustment"})

        # Anti-recursive contamination: neither assistant bait ("猫狗->神仙") nor derived summary bait ("越亮->月亮") appears!
        all_titles = " ".join(p["title"] for p in props)
        self.assertNotIn("猫狗", all_titles)
        self.assertNotIn("越亮", all_titles)
        self.assertNotIn("船长", all_titles)

        # Sensitive entity in user evidence excerpt MUST be redacted!
        all_ev_txt = json.dumps([p["evidence"] for p in props], ensure_ascii=False)
        self.assertNotIn("13812345678", all_ev_txt)
        self.assertIn("[已拦截隐私信息:手机号]", all_ev_txt)

        # Approve memory_merge -> old conflicting memory superseded cleanly with valid MemoryLifecycle
        merge_p = next(p for p in props if p["kind"] == "memory_merge")
        ap_merge = mgr.approve_proposal(
            svc.DEFAULT_CHARACTER_ID,
            merge_p["id"],
            expected_baseline_hash=merge_p["baseline_hash"],
        )
        self.assertTrue(ap_merge["ok"])
        self.assertEqual(len(ap_merge["applied_result"]["retired_memory_ids"]), 1)
        retired_id = ap_merge["applied_result"]["retired_memory_ids"][0]
        # Verify engine._memory validates cleanly and retired memory is no longer eligible
        all_mems_after = self.brain.engine.memories(svc.DEFAULT_CHARACTER_ID)
        retired_mem = next(m for m in all_mems_after if m.id == retired_id)
        self.assertEqual(retired_mem.lifecycle.invalidation, "superseded")
        self.assertFalse(svc.eligible(retired_mem))

        # Approve style_adjustment -> creates style memory
        style_p = next(p for p in props if p["kind"] == "style_adjustment")
        # First test CAS mismatch -> DreamConflictError
        with self.assertRaises(dream_executor.DreamConflictError):
            mgr.approve_proposal(
                svc.DEFAULT_CHARACTER_ID,
                style_p["id"],
                expected_baseline_hash="0000000000000000",
            )
        ap_style = mgr.approve_proposal(
            svc.DEFAULT_CHARACTER_ID,
            style_p["id"],
            expected_baseline_hash=style_p["baseline_hash"],
        )
        self.assertTrue(ap_style["ok"])
        style_mems = [m for m in self.brain.engine.memories(svc.DEFAULT_CHARACTER_ID) if m.category == "style"]
        self.assertEqual(len(style_mems), 1)

        # Reject asr_correction -> marked rejected, and re-running dream does NOT recreate it
        asr_p = next(p for p in props if p["kind"] == "asr_correction")
        rej = mgr.reject_proposal(svc.DEFAULT_CHARACTER_ID, asr_p["id"], note="暂不需要")
        self.assertEqual(rej["proposal"]["status"], "rejected")
        rerun = mgr.run_dream_cycle(svc.DEFAULT_CHARACTER_ID, reason="manual", existing_asr_corrections={})
        self.assertEqual(rerun["pending_count"], 0)

        # Baseline drift test on a brand new memory_merge proposal
        self._commit(
            "dream-t3",
            "我最喜欢的饮料是绿茶",
            "记下啦。",
            memories=[
                MemoryDraft(
                    key="drink_1",
                    text="最喜欢喝热绿茶",
                    category="preference",
                    source_quote="绿茶",
                    details=MemoryDetails(state_key="user:drink"),
                )
            ],
        )
        time.sleep(0.02)
        self._commit(
            "dream-t4",
            "我现在改喝乌龙茶了",
            "乌龙茶也不错。",
            memories=[
                MemoryDraft(
                    key="drink_2",
                    text="最喜欢喝无糖乌龙茶",
                    category="preference",
                    source_quote="乌龙茶",
                    details=MemoryDetails(state_key="user:drink"),
                )
            ],
        )
        run2 = mgr.run_dream_cycle(svc.DEFAULT_CHARACTER_ID, reason="manual")
        merge_p2 = next(p for p in run2["proposals"] if p["status"] == "pending" and p["kind"] == "memory_merge")
        # Mutate involved memory externally before approval
        keep_id = merge_p2["payload"]["keep_memory_id"]
        self.brain.engine.correct_memory(
            svc.DEFAULT_CHARACTER_ID,
            keep_id,
            MemoryDraft(key="drink_2", text="其实最喜欢喝冷泡乌龙茶", category="preference", source_quote="乌龙茶"),
            expected_revision=merge_p2["payload"]["keep_revision"],
        )
        with self.assertRaises(dream_executor.DreamConflictError):
            mgr.approve_proposal(
                svc.DEFAULT_CHARACTER_ID,
                merge_p2["id"],
                expected_baseline_hash=merge_p2["baseline_hash"],
            )
        stale_row = mgr.get_proposal(svc.DEFAULT_CHARACTER_ID, merge_p2["id"])
        self.assertEqual(stale_row["status"], "stale")

    def test_m2_anima_fast_recall_sensitive_filter(self):
        """Mechanism 2 (Anima side): build_fast_recall_context auto-redacts secrets/phones/cards/IDs/OTPs."""
        self._commit(
            "pii-t1",
            "记住我的密钥 sk-live-secret9876543210xyz，手机号 13912345678，身份证 11010519491231002X，验证码是 739104，但我喜欢吃草莓蛋糕",
            "好的，草莓蛋糕很甜。",
            memories=[
                MemoryDraft(
                    key="sensitive_mix",
                    text="密钥 sk-live-secret9876543210xyz，手机 13912345678，喜欢吃草莓蛋糕",
                    category="identity",
                    source_quote="喜欢吃草莓蛋糕",
                )
            ],
        )
        rec = svc.build_fast_recall_context(self.brain, svc.DEFAULT_CHARACTER_ID, "草莓蛋糕")
        ctx = rec["context_text"]
        self.assertGreater(rec["sensitive_redacted_count"], 0)
        for leak in ("sk-live-secret9876543210xyz", "13912345678", "11010519491231002X", "739104"):
            self.assertNotIn(leak, ctx)
        self.assertIn("草莓蛋糕", ctx)

    def test_m3_multispace_embedding_dim_validation_auth_breaker_and_health_degradation(self):
        """Mechanism 3: model@dim coexistence, zero rebuild on switch-back, write-time dim check, 401/403 breaker & explicit FTS5 health."""
        self._commit(
            "emb-t1",
            "我养了一只叫团子的橘猫",
            "团子这个名字真可爱。",
            memories=[
                MemoryDraft(key="pet_cat", text="养了一只叫团子的橘猫", category="identity", source_quote="团子的橘猫")
            ],
        )

        # 1. Write-time dimension validation rejects mismatched vector length against @dim space
        with self.assertRaises(ValueError):
            self.brain.engine.retrieval.store(
                svc.DEFAULT_CHARACTER_ID,
                f"fact:{self.brain.engine.memories(svc.DEFAULT_CHARACTER_ID)[0].id}",
                "abc12345:test-embed@4",
                [1.0, 0.0],  # wrong dim (2 != 4)
                "pet_cat 养了一只叫团子的橘猫",
            )

        # 2. Index with model-A (dim=2) -> switch to dim=4 (embedding_dimensions=4) -> both spaces coexist!
        calls_log: list[dict] = []

        def _make_transport(dim: int, *, status_code: int = 200):
            def _handler(request):
                body = json.loads(request.content)
                calls_log.append(body)
                if status_code != 200:
                    return httpx.Response(status_code, json={"error": "auth_denied"})
                n = len(body["input"])
                vec = [0.0] * dim
                vec[0] = 1.0
                return httpx.Response(
                    200,
                    json={"data": [{"index": i, "embedding": vec} for i in range(n)], "usage": {"total_tokens": n}},
                )

            return httpx.MockTransport(_handler)

        rt = self.brain.runtime
        rt.settings.embedding_base_url = "https://embed.example/v1"
        rt.settings.embedding_model = "embed-multi"
        rt.settings.embedding_key = SecretStr("sk-embed-good-key-123456")
        rt.settings.embedding_dimensions = 2
        rt.transport = _make_transport(2)

        idx1 = rt.index(svc.DEFAULT_CHARACTER_ID, "idx-dim2")
        self.assertGreater(idx1["indexed"], 0)
        self.assertTrue(idx1["space"].endswith(":embed-multi@2"))
        self.assertEqual(idx1["dim"], 2)

        # Switch to dim=4 on the SAME model! Old @2 vectors MUST stay preserved in SQLite!
        rt.settings.embedding_dimensions = 4
        rt.transport = _make_transport(4)
        idx2 = rt.index(svc.DEFAULT_CHARACTER_ID, "idx-dim4")
        self.assertGreater(idx2["indexed"], 0)
        self.assertTrue(idx2["space"].endswith(":embed-multi@4"))

        spaces = self.brain.engine.retrieval.vector_spaces(svc.DEFAULT_CHARACTER_ID)
        labels = {s["label"] for s in spaces}
        self.assertIn("embed-multi@2", labels)
        self.assertIn("embed-multi@4", labels)

        # Switch BACK to dim=2 -> ZERO rebuild needed (0 items indexed, 0 network calls!)
        calls_before = len(calls_log)
        rt.settings.embedding_dimensions = 2
        rt.transport = _make_transport(2)
        idx3 = rt.index(svc.DEFAULT_CHARACTER_ID, "idx-dim2-again")
        self.assertEqual(idx3["indexed"], 0)
        self.assertEqual(len(calls_log), calls_before, "Switching back to preserved vector space must NOT rebuild!")

        # 3. 401 Auth Failure immediately trips circuit breaker (zero retry hammering!) & degrades /health explicitly
        calls_401 = len(calls_log)
        rt.transport = _make_transport(2, status_code=401)
        emb = Embeddings(rt.settings, rt.transport)
        with self.assertRaises(ProviderError) as pe:
            emb.encode(["测试鉴权失败"])
        self.assertEqual(pe.exception.status_code, 401)
        self.assertEqual(len(calls_log), calls_401 + 1)

        # Second call MUST short-circuit locally without hitting transport!
        with self.assertRaises(ProviderError):
            emb.encode(["第二次应直接熔断"])
        self.assertEqual(len(calls_log), calls_401 + 1, "Auth circuit breaker must short-circuit without HTTP call!")

        # resolve_recall_query_embedding also short-circuits cleanly to (None, None)
        qv, qm = asyncio.run(svc.resolve_recall_query_embedding(self.brain, "团子"))
        self.assertIsNone(qv)
        self.assertIsNone(qm)
        self.assertEqual(len(calls_log), calls_401 + 1)

        # Health endpoint explicitly reports degraded semantic retrieval -> pure FTS5 (while still HTTP 200!)
        token = "test-studio-token-1234567890abcdef"
        app = svc.create_combined_asgi_app(self.brain, token)
        with TestClient(app) as client:
            r_h = client.get("/xiaozhi/health")
            self.assertEqual(r_h.status_code, 200)
            hb = r_h.json()
            self.assertEqual(hb["status"], "degraded")
            self.assertTrue(hb["degraded"])
            self.assertTrue(any("semantic_retrieval_degraded" in x for x in hb["degraded_reasons"]))
            sem = hb["semantic_retrieval"]
            self.assertTrue(sem["degraded"])
            self.assertTrue(sem["auth_breaker_open"])
            self.assertEqual(sem["retrieval_mode"], "lexical_fts5")
            self.assertEqual(sem["degraded_reason"], "auth_failed_401")
            self.assertEqual(sem["spaces_count"], 2)

            # Manual breaker reset via authenticated POST /xiaozhi/embedding/reset-breaker clears breaker
            rt.transport = _make_transport(2, status_code=200)
            r_rst = client.post(
                "/xiaozhi/embedding/reset-breaker",
                headers={"Authorization": f"Bearer {token}"},
                json={},
            )
            self.assertEqual(r_rst.status_code, 200)
            self.assertFalse(r_rst.json()["semantic_retrieval"]["degraded"])
            self.assertEqual(r_rst.json()["semantic_retrieval"]["retrieval_mode"], "hybrid_rrf")

            r_h2 = client.get("/xiaozhi/health")
            self.assertEqual(r_h2.json()["status"], "ok")

    def test_m5_offline_layered_daily_and_weekly_briefings_in_obsidian(self):
        """Mechanism 5: Low-cost offline daily/weekly Obsidian briefings with urgency tiers, redaction, idempotency, zero voice push."""
        day_dt = datetime(2026, 11, 20, 15, 0, 0, tzinfo=self.tz)
        prev_week_dt = datetime(2026, 11, 10, 10, 0, 0, tzinfo=self.tz)

        self._commit(
            "br-t0",
            "上周安排的体检还没去预约",
            "记得尽快约一下哦。",
            occurred_at=prev_week_dt.timestamp(),
            emotion="calm",
            memories=[
                MemoryDraft(
                    key="loop_checkup",
                    text="预约年度体检",
                    category="event",
                    source_quote="体检还没去预约",
                    details=MemoryDetails(importance=6, loop_status="open", followup="ask"),
                )
            ],
        )
        self._commit(
            "br-t1",
            "明天 11 月 21 日要参加产品评审会，另外别泄露我的手机号 13800138000",
            "评审会加油！",
            occurred_at=day_dt.timestamp(),
            emotion="warm",
            memories=[
                MemoryDraft(
                    key="loop_review",
                    text="参加产品评审会",
                    category="event",
                    source_quote="参加产品评审会",
                    details=MemoryDetails(
                        importance=9,
                        event_date=date(2026, 11, 21),
                        loop_status="open",
                        followup="ask",
                    ),
                )
            ],
        )

        exporter = obs_mod.ObsidianExporter(
            self.brain,
            character_id=svc.DEFAULT_CHARACTER_ID,
            export_dir=self.vault_dir,
            config_path=self.cfg_path,
            default_enabled=True,
            default_briefing_enabled=True,
        )
        res1 = exporter.export_briefings(reason="test_run", target_date="2026-11-20")
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["written"], 3)
        self.assertEqual(res1["skipped"], 0)
        self.assertEqual(res1["daily_date"], "2026-11-20")
        self.assertEqual(res1["week_key"], "2026-W47")

        daily_fp = self.vault_dir / "简报" / "每日" / "2026-11-20.md"
        weekly_fp = self.vault_dir / "简报" / "每周" / "2026-W47.md"
        index_fp = self.vault_dir / "简报" / "最新简报.md"
        self.assertTrue(daily_fp.is_file())
        self.assertTrue(weekly_fp.is_file())
        self.assertTrue(index_fp.is_file())

        daily_txt = daily_fp.read_text(encoding="utf-8")
        self.assertIn("优先 / 临近待跟进", daily_txt)
        self.assertIn("参加产品评审会", daily_txt)
        self.assertIn("距目标日 1 天", daily_txt)
        self.assertNotIn("13800138000", daily_txt)
        self.assertIn("[已拦截隐私信息:手机号]", daily_txt)

        weekly_txt = weekly_fp.read_text(encoding="utf-8")
        self.assertIn("2026-W47", weekly_txt)
        self.assertIn("跨周未闭环", weekly_txt)
        self.assertIn("预约年度体检", weekly_txt)

        # Second identical call is 100% hash-skipped (low-cost idempotency)
        res2 = exporter.export_briefings(reason="test_idempotent", target_date="2026-11-20")
        self.assertEqual(res2["written"], 0)
        self.assertEqual(res2["skipped"], 3)

        # Status exposes briefing metadata & voice_push=False
        st = exporter.status()
        self.assertTrue(st["briefing_enabled"])
        self.assertFalse(st["briefing"]["voice_push"])
        self.assertEqual(st["briefing"]["daily_date"], "2026-11-20")

        # Historical target_date export MUST NOT roll 最新简报.md backward from 2026-11-20
        res_hist = exporter.export_briefings(reason="historical_backfill", target_date="2026-11-10")
        self.assertTrue(res_hist["ok"])
        self.assertTrue((self.vault_dir / "简报" / "每日" / "2026-11-10.md").is_file())
        self.assertIn("latest_daily: 2026-11-20", index_fp.read_text(encoding="utf-8"))

        # HTTP endpoints: /xiaozhi/obsidian/config (briefing_enabled) & /xiaozhi/obsidian/briefing
        token = "test-studio-token-briefing-123456"
        app = svc.create_combined_asgi_app(self.brain, token, obsidian_exporter=exporter)
        with TestClient(app) as client:
            r_tog = client.post(
                "/xiaozhi/obsidian/config",
                headers={"Authorization": f"Bearer {token}"},
                json={"briefing_enabled": False},
            )
            self.assertEqual(r_tog.status_code, 200)
            self.assertFalse(r_tog.json()["obsidian"]["briefing_enabled"])

            r_br = client.post(
                "/xiaozhi/obsidian/briefing",
                headers={"Authorization": f"Bearer {token}"},
                json={"target_date": "2026-11-20"},
            )
            self.assertEqual(r_br.status_code, 200)
            self.assertTrue(r_br.json()["ok"])

            # Invalid target_date formats MUST return HTTP 400
            for bad_d in ("not-a-date", "2026-02-30", "2026/11/20"):
                r_bad = client.post(
                    "/xiaozhi/obsidian/briefing",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"target_date": bad_d},
                )
                self.assertEqual(r_bad.status_code, 400, f"Invalid target_date {bad_d!r} must return 400")

    def test_m1_sibling_style_proposals_pii_scrub_addressing_particle_and_known_asr_persistence(self):
        """Regression: sibling style proposals must not self-invalidate; source_quote must be PII-scrubbed; greedy particle bug fixed."""
        self._commit(
            "sty-1",
            "以后说话简短一点，别太正式，以后叫我阿明，我说的是芽衣，不是牙医",
            "好呀阿明。",
        )
        self._commit(
            "sty-2",
            "平时回复简短一点，不用那么客气，以后就叫我阿明吧，顺便我手机号是 13812345678",
            "收到。",
        )
        mgr = dream_executor.DreamManager(self.brain)
        res = mgr.run_dream_cycle(
            svc.DEFAULT_CHARACTER_ID,
            reason="manual",
            existing_asr_corrections={"牙医": "芽衣"},
        )
        self.assertTrue(res["ok"])
        # Already-active ASR rule from existing_asr_corrections MUST be skipped
        asr_props = [p for p in res["proposals"] if p["kind"] == "asr_correction"]
        self.assertEqual(len(asr_props), 0)

        # Scheduled run (existing_asr_corrections=None) MUST remember cached known_asr_json and still skip
        res_sched = mgr.run_dream_cycle(
            svc.DEFAULT_CHARACTER_ID,
            reason="scheduled",
            existing_asr_corrections=None,
            skip_if_no_new_evidence=False,
        )
        self.assertEqual(len([p for p in res_sched["proposals"] if p["kind"] == "asr_correction"]), 0)

        style_props = [p for p in res["proposals"] if p["kind"] == "style_adjustment"]
        self.assertEqual(len(style_props), 3, f"Expected concise + casual_warm + addressing(阿明), got {[p['title'] for p in style_props]}")
        addr_prop = next(p for p in style_props if p["payload"].get("habit_id") == "addressing_preference")
        self.assertIn("阿明", addr_prop["payload"]["text"])
        self.assertNotIn("阿明吧", addr_prop["payload"]["text"])

        # source_quote in payload MUST NOT leak raw phone number
        for sp in style_props:
            self.assertNotIn("13812345678", sp["payload"]["source_quote"])

        # Approving all 3 sibling style proposals sequentially with their original baseline_hash MUST succeed
        for sp in style_props:
            ap = mgr.approve_proposal(
                svc.DEFAULT_CHARACTER_ID,
                sp["id"],
                expected_baseline_hash=sp["baseline_hash"],
            )
            self.assertTrue(ap["ok"], f"Sibling style approval failed for {sp['payload']['key']}")

        # Direct ?proposal_id= lookup works via HTTP endpoint
        token = "test-studio-token-dream-lookup-123"
        app = svc.create_combined_asgi_app(self.brain, token)
        with TestClient(app) as client:
            r_one = client.get(
                f"/xiaozhi/dream/status?proposal_id={style_props[0]['id']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(r_one.status_code, 200)
            self.assertEqual(r_one.json()["proposal"]["id"], style_props[0]["id"])

    def test_m2_recall_sanitizer_edge_cases(self):
        """Regression: OTP no false positive, CJK-adjacent OTP, Chinese quotes, plain token, standalone JWT, 15-digit Amex."""
        fp_txt = "验证码发的是abcdef，明天3点见"
        fp_out, fp_meta = recall_sanitizer.redact_sensitive_entities(fp_txt)
        self.assertEqual(fp_out, fp_txt)
        self.assertEqual(fp_meta["redacted_count"], 0)

        sample = (
            "短信验证码是AB12CD请勿泄露；验证码为「482910」；"
            "我的token是abc12345xyz；独立卡 378282246310005；"
            "JWT eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        out, meta = recall_sanitizer.redact_sensitive_entities(sample)
        self.assertGreaterEqual(meta["redacted_count"], 5)
        for leak in ("AB12CD", "482910", "abc12345xyz", "378282246310005", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"):
            self.assertNotIn(leak, out, f"Leaked: {leak}")

    def test_m3_multispace_dim_unset_zero_infinite_loop_and_short_query_not_degraded(self):
        """Regression: unsetting embedding_dimensions after @2 must switch cleanly to @4 without infinite paid API loop; 1-char query not degraded."""
        self._commit("loop-1", "喜欢喝乌龙茶", "好喝。")
        self._commit("loop-2", "周末想去西湖散步", "很惬意。")
        self._commit("loop-3", "最近在学摄影", "拍得一定好看。")

        calls: list[tuple[int, int]] = []

        def _mk_transport(dim: int):
            def _h(request):
                body = json.loads(request.content)
                n = len(body["input"])
                calls.append((dim, n))
                vec = [0.0] * dim
                vec[0] = 1.0
                return httpx.Response(
                    200,
                    json={"data": [{"index": i, "embedding": vec} for i in range(n)], "usage": {"total_tokens": n}},
                )

            return httpx.MockTransport(_h)

        rt = self.brain.runtime
        rt.automation.configure(
            svc.DEFAULT_CHARACTER_ID,
            MemoryAutomation(embedding=True, semantic_retrieval=True, summaries=False, reconcile=False),
        )
        rt.settings.embedding_base_url = "https://embed.example/v1"
        rt.settings.embedding_model = "embed-loop"
        rt.settings.embedding_key = SecretStr("sk-embed-loop-key-123456")
        rt.settings.embedding_dimensions = 2
        rt.transport = _mk_transport(2)

        idx_init = rt.index(svc.DEFAULT_CHARACTER_ID, "idx-init-2")
        self.assertGreater(idx_init["indexed"], 0)

        # Unset embedding_dimensions (provider now returns native dim=4) and add 1 new turn
        rt.settings.embedding_dimensions = None
        rt.transport = _mk_transport(4)
        self._commit("loop-4", "今天阳光真好", "是呀。")
        calls.clear()

        worked_steps = 0
        for _ in range(6):
            if rt.automation.run_once():
                worked_steps += 1
            else:
                break
        # Must converge in <= 2 steps (first indexes new turn & records observed_default_dim=4; second indexes remaining 3 turns; third is idle)
        self.assertLessEqual(worked_steps, 2, f"Infinite re-indexing loop detected! calls={calls}")
        self.assertFalse(rt.automation.run_once(), "Automation must become idle once all turns are indexed in @4")

        spaces = {s["label"]: s["evidence_vectors"] for s in self.brain.engine.retrieval.vector_spaces(svc.DEFAULT_CHARACTER_ID)}
        self.assertEqual(spaces.get("embed-loop@2"), 6)
        self.assertEqual(spaces.get("embed-loop@4"), 8)

        # 1-character utterance ("嗯") MUST NOT falsely flag semantic_degraded=True
        rec_short = svc.build_fast_recall_context(
            self.brain, svc.DEFAULT_CHARACTER_ID, "嗯", query_vector=None, embed_model=None
        )
        self.assertFalse(rec_short["semantic_degraded"])

    def test_r2_embedding_key_missing_reported_as_degraded_and_loopback_allowed(self):
        """Regression #1: configured remote embedding with missing/unexpanded key MUST report degraded=True (not unconfigured)."""
        token = "test-studio-token-embed-key-missing-123"
        rt = self.brain.runtime
        app = svc.create_combined_asgi_app(self.brain, token)

        with TestClient(app) as client:
            # 1. Truly unconfigured (no base_url / no model) -> configured=False, degraded=False, /health status=ok
            rt.settings.embedding_base_url = ""
            rt.settings.embedding_model = ""
            rt.settings.embedding_key = SecretStr("")
            h_unc = client.get("/xiaozhi/health").json()
            self.assertEqual(h_unc["status"], "ok")
            self.assertFalse(h_unc["degraded"])
            self.assertFalse(h_unc["semantic_retrieval"]["configured"])
            self.assertFalse(h_unc["semantic_retrieval"]["degraded"])
            self.assertEqual(h_unc["semantic_retrieval"]["degraded_reason"], "unconfigured")

            # 2. Configured remote endpoint + empty key -> configured=True, degraded=True, embedding_key_missing
            rt.settings.embedding_base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
            rt.settings.embedding_model = "gemini-embedding-001"
            rt.settings.embedding_key = SecretStr("")
            for path in ("/health", "/xiaozhi/health"):
                r = client.get(path)
                self.assertEqual(r.status_code, 200)
                hb = r.json()
                self.assertEqual(hb["status"], "degraded")
                self.assertTrue(hb["degraded"])
                self.assertIn("semantic_retrieval_degraded: embedding_key_missing", hb["degraded_reasons"])
                sem = hb["semantic_retrieval"]
                self.assertTrue(sem["configured"])
                self.assertFalse(sem["semantic_available"])
                self.assertEqual(sem["retrieval_mode"], "lexical_fts5")
                self.assertTrue(sem["degraded"])
                self.assertEqual(sem["degraded_reason"], "embedding_key_missing")
                self.assertFalse(sem["auth_breaker_open"])
                self.assertFalse(sem["circuit_open"])
                self.assertEqual(sem["active_model"], "gemini-embedding-001")

            # 3. Configured remote endpoint + unexpanded ${...} placeholder -> ALSO degraded=True
            rt.settings.embedding_key = SecretStr("${GEMINI_API_KEY}")
            hb_ph = client.get("/xiaozhi/health").json()
            self.assertEqual(hb_ph["status"], "degraded")
            self.assertTrue(hb_ph["degraded"])
            self.assertTrue(hb_ph["semantic_retrieval"]["configured"])
            self.assertTrue(hb_ph["semantic_retrieval"]["degraded"])
            self.assertEqual(hb_ph["semantic_retrieval"]["degraded_reason"], "embedding_key_missing")

            # 4. Local loopback embedding servers (IPv4 / uppercase localhost / IPv6) without key -> NOT degraded
            for loop_url in (
                "http://127.0.0.1:11434/v1",
                "http://LOCALHOST:11434/v1",
                "http://[::1]:11434/v1",
            ):
                rt.settings.embedding_base_url = loop_url
                rt.settings.embedding_model = "bge-m3"
                rt.settings.embedding_key = SecretStr("")
                hb_loc = client.get("/xiaozhi/health").json()
                self.assertEqual(hb_loc["status"], "ok", f"Loopback {loop_url} must be allowed without key")
                self.assertFalse(hb_loc["degraded"])
                self.assertTrue(hb_loc["semantic_retrieval"]["configured"])
                self.assertFalse(hb_loc["semantic_retrieval"]["degraded"])
                self.assertEqual(hb_loc["semantic_retrieval"]["retrieval_mode"], "hybrid_rrf")

            # 5. Spoofed non-loopback URLs containing '//localhost' substring -> MUST STILL report embedding_key_missing!
            for spoof_url in (
                "https://localhost.evil.example/v1",
                "https://api.example.com/proxy//localhost",
            ):
                rt.settings.embedding_base_url = spoof_url
                rt.settings.embedding_model = "gemini-embedding-001"
                rt.settings.embedding_key = SecretStr("")
                hb_sp = client.get("/xiaozhi/health").json()
                self.assertEqual(hb_sp["status"], "degraded", f"Spoofed {spoof_url} must NOT bypass key check")
                self.assertTrue(hb_sp["degraded"])
                self.assertEqual(hb_sp["semantic_retrieval"]["degraded_reason"], "embedding_key_missing")

    def test_r2_transient_embedding_breaker_blocks_automation_claim_and_maintain(self):
        """Regression #2: open transient breaker must block automation index claims and runtime.maintain note embeddings."""
        from types import SimpleNamespace

        self._commit("tb-1", "周末想去西湖边散步喝龙井茶", "西湖边很舒服。")
        rt = self.brain.runtime
        rt.settings.embedding_base_url = "https://embed.transient.test/v1"
        rt.settings.embedding_model = "embed-transient"
        rt.settings.embedding_key = SecretStr("sk-embed-transient-key-12345")
        rt.settings.embedding_dimensions = 4

        embed_http_calls = 0

        def _fail_503(request):
            nonlocal embed_http_calls
            embed_http_calls += 1
            return httpx.Response(503, json={"error": "upstream_unavailable"})

        rt.transport = httpx.MockTransport(_fail_503)
        emb = Embeddings(rt.settings, rt.transport)
        for _ in range(3):
            with self.assertRaises(ProviderError):
                emb.encode(["trip transient breaker"])
        self.assertEqual(embed_http_calls, 3)

        # 1. automation.availability() reports embedding_circuit_open (NOT embedding_auth_failed)
        idx_policy = MemoryAutomation(
            embedding=True, semantic_retrieval=True, summaries=False, reconcile=False, daily_jobs=10
        )
        rt.automation.configure(svc.DEFAULT_CHARACTER_ID, idx_policy)
        issues = rt.automation.availability(idx_policy)
        self.assertIn("embedding_circuit_open", issues)
        self.assertNotIn("embedding_auth_failed", issues)

        # 2. automation.claim() / run_once() MUST NOT claim index jobs, burn daily_jobs, or park review jobs
        self.assertIsNone(rt.automation.claim())
        self.assertFalse(rt.automation.run_once())
        jobs_cnt = self.brain.engine.db.execute("SELECT count(*) FROM memory_jobs").fetchone()[0]
        self.assertEqual(jobs_cnt, 0)
        self.assertEqual(embed_http_calls, 3)

        # 2b. runtime.recall() and runtime.prepare() also short-circuit without writing failed SQLite operations rows
        ops_before = self.brain.engine.db.execute("SELECT count(*) FROM operations").fetchone()[0]
        rec_cb = rt.recall(svc.DEFAULT_CHARACTER_ID, "西湖散步")
        self.assertTrue(rec_cb.get("semantic_degraded"))
        prep_cb = rt.prepare(svc.DEFAULT_CHARACTER_ID, "西湖散步", "req-cb-open-shortcircuit")
        self.assertEqual(prep_cb.retrieval_mode, "lexical_semantic_unavailable")
        ops_after = self.brain.engine.db.execute("SELECT count(*) FROM operations").fetchone()[0]
        self.assertEqual(ops_after, ops_before, "Open circuit must NOT insert failed rows into operations table")
        self.assertEqual(embed_http_calls, 3)

        # 3. runtime.maintain() skips note query embedding cleanly when transient breaker is open (0 embed HTTP calls)
        class _FakeNoteProvider:
            def structured(self, system, user):
                data = json.loads(user)
                if "new_source" in data:
                    out = {"description": "想去西湖散步喝茶", "keywords": ["西湖", "龙井茶"], "links": []}
                else:
                    src_ids = [s["id"] for s in data["sources"]]
                    out = {
                        "episode": "聊到想去西湖喝龙井茶。",
                        "core": "喜欢西湖和龙井茶。",
                        "episode_source_ids": src_ids,
                        "core_source_ids": src_ids,
                    }
                return SimpleNamespace(
                    text=json.dumps(out, ensure_ascii=False),
                    model="fake-summary",
                    usage=None,
                )

        rt._provider = _FakeNoteProvider()
        rt.automation.configure(
            svc.DEFAULT_CHARACTER_ID,
            MemoryAutomation(
                embedding=True,
                semantic_retrieval=True,
                summaries=True,
                summary_turns=1,
                retain_recent_turns=0,
                reconcile=False,
                daily_jobs=10,
            ),
        )
        rt.maintain(svc.DEFAULT_CHARACTER_ID, "req-maintain-cb-open", flush=True)
        self.assertEqual(embed_http_calls, 3, "maintain() must NOT call embedding endpoint while transient breaker is open")

        # 4. dimension_mismatch also trips transient breaker and blocks index claims
        embedding_breaker.reset(rt.settings)
        dim_calls = 0

        def _bad_dim(request):
            nonlocal dim_calls
            dim_calls += 1
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

        rt.transport = httpx.MockTransport(_bad_dim)
        emb_dim = Embeddings(rt.settings, rt.transport)
        for _ in range(3):
            with self.assertRaises(ProviderError):
                emb_dim.encode(["wrong dim"])
        self.assertEqual(dim_calls, 3)
        self.assertIn("embedding_circuit_open", rt.automation.availability(idx_policy))
        rt.automation.configure(svc.DEFAULT_CHARACTER_ID, idx_policy)
        self.assertIsNone(rt.automation.claim())

        # 5. Auth failure (401) emits embedding_auth_failed and also blocks index claims
        embedding_breaker.reset(rt.settings)
        rt.transport = httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "bad_key"}))
        with self.assertRaises(ProviderError):
            Embeddings(rt.settings, rt.transport).encode(["auth fail"])
        auth_issues = rt.automation.availability(idx_policy)
        self.assertIn("embedding_auth_failed", auth_issues)
        self.assertNotIn("embedding_circuit_open", auth_issues)
        self.assertIsNone(rt.automation.claim())

    def test_r2_dream_config_syncs_known_asr_across_characters_and_marks_drifted_stale(self):
        """Regression #4 (Anima side): POST /xiaozhi/dream/config persists known_asr_json and marks drifted ASR proposals stale."""
        second_cid = "kid_room"
        self.brain.create_character(
            character_id=second_cid,
            name="书房小智",
            persona="你是书房小智。",
            memory_policy="automatic",
            timezone="Asia/Shanghai",
        )
        # Seed first-hand ASR correction evidence on both characters
        self._commit("asr-sync-1", "我说的是芽衣，不是牙医", "好的芽衣。")
        eng = self.brain.engine
        ctx_k = eng.prepare_turn(second_cid, "我说的是芽衣，不是牙医", request_id="asr-sync-kid-1")
        eng.commit_turn(second_cid, ctx_k.turn_id, CommitRequest(assistant_reply="记住啦。"))

        token = "test-studio-token-dream-cfg-sync-123"
        app = svc.create_combined_asgi_app(self.brain, token)
        headers = {"Authorization": f"Bearer {token}"}

        with TestClient(app) as client:
            # Generate pending asr_correction proposals on both characters with empty existing_asr_corrections
            for cid in (svc.DEFAULT_CHARACTER_ID, second_cid):
                r_run = client.post(
                    "/xiaozhi/dream/run",
                    headers=headers,
                    json={"character_id": cid, "existing_asr_corrections": {}},
                )
                self.assertEqual(r_run.status_code, 200)
                asr_ps = [p for p in r_run.json()["proposals"] if p["kind"] == "asr_correction" and p["status"] == "pending"]
                self.assertEqual(len(asr_ps), 1, f"Expected 1 pending ASR proposal for {cid}")

            # Invalid payloads -> 400
            self.assertEqual(client.post("/xiaozhi/dream/config", headers=headers, json={}).status_code, 400)
            self.assertEqual(
                client.post(
                    "/xiaozhi/dream/config",
                    headers=headers,
                    json={"existing_asr_corrections": ["bad"]},
                ).status_code,
                400,
            )
            self.assertEqual(
                client.post(
                    "/xiaozhi/dream/config",
                    headers=headers,
                    json={"existing_asr_corrections": {}, "all_characters": "yes"},
                ).status_code,
                400,
            )

            # 1. Targeted single-character sync only marks kid_room proposal stale; default character remains pending
            r_one = client.post(
                "/xiaozhi/dream/config",
                headers=headers,
                json={"character_id": second_cid, "existing_asr_corrections": {"牙医": "芽衣"}},
            )
            self.assertEqual(r_one.status_code, 200)
            self.assertEqual(r_one.json()["config"]["stale_marked"], 1)
            self.assertEqual(r_one.json()["config"]["known_asr_count"], 1)

            st_kid = client.get(f"/xiaozhi/dream/status?character_id={second_cid}", headers=headers).json()
            st_def = client.get(f"/xiaozhi/dream/status?character_id={svc.DEFAULT_CHARACTER_ID}", headers=headers).json()
            self.assertEqual(st_kid["pending_count"], 0)
            self.assertEqual(st_kid["stale_count"], 1)
            self.assertEqual(st_def["pending_count"], 1)

            # 2. Global sync (all_characters=True) persists across all characters & marks default character proposal stale
            r_all = client.post(
                "/xiaozhi/dream/config",
                headers=headers,
                json={"existing_asr_corrections": {"牙医": "芽衣"}, "all_characters": True},
            )
            self.assertEqual(r_all.status_code, 200)
            cfg_all = r_all.json()["config"]
            self.assertEqual(cfg_all["stale_marked"], 1)
            self.assertIn(svc.DEFAULT_CHARACTER_ID, cfg_all["synced_characters"])
            self.assertIn(second_cid, cfg_all["synced_characters"])

            st_def_after = client.get(f"/xiaozhi/dream/status?character_id={svc.DEFAULT_CHARACTER_ID}", headers=headers).json()
            self.assertEqual(st_def_after["pending_count"], 0)
            self.assertEqual(st_def_after["stale_count"], 1)

            # 3. Scheduled Dream run (no existing_asr_corrections passed) skips already-synced rule!
            r_sched = client.post(
                "/xiaozhi/dream/run",
                headers=headers,
                json={"character_id": svc.DEFAULT_CHARACTER_ID},
            )
            self.assertEqual(r_sched.status_code, 200)
            self.assertEqual(
                len([p for p in r_sched.json()["proposals"] if p["kind"] == "asr_correction" and p["status"] == "pending"]),
                0,
            )

            # 4. Fresh character created AFTER global sync inherits synced known_asr_json
            fresh_cid = "guest_room"
            self.brain.create_character(
                character_id=fresh_cid,
                name="客房小智",
                persona="你是客房小智。",
                memory_policy="automatic",
                timezone="Asia/Shanghai",
            )
            ctx_g = eng.prepare_turn(fresh_cid, "我说的是芽衣，不是牙医", request_id="asr-sync-guest-1")
            eng.commit_turn(fresh_cid, ctx_g.turn_id, CommitRequest(assistant_reply="好的。"))
            r_fresh = client.post(
                "/xiaozhi/dream/run",
                headers=headers,
                json={"character_id": fresh_cid},
            )
            self.assertEqual(r_fresh.status_code, 200)
            self.assertEqual(
                len([p for p in r_fresh.json()["proposals"] if p["kind"] == "asr_correction" and p["status"] == "pending"]),
                0,
                "Newly provisioned character must inherit globally synced known_asr_json",
            )

            # 4b. Simulated anima-brain restart (fresh DreamManager on same SQLite DB) preserves global known_asr_json!
            dm_restarted = dream_executor.DreamManager(self.brain)
            post_restart_cid = "attic_room"
            self.brain.create_character(
                character_id=post_restart_cid,
                name="阁楼小智",
                persona="你是阁楼小智。",
                memory_policy="automatic",
                timezone="Asia/Shanghai",
            )
            ctx_a = eng.prepare_turn(post_restart_cid, "我说的是芽衣，不是牙医", request_id="asr-sync-attic-1")
            eng.commit_turn(post_restart_cid, ctx_a.turn_id, CommitRequest(assistant_reply="好的。"))
            self.assertEqual(dm_restarted.get_config(post_restart_cid)["known_asr_count"], 1)
            r_post_restart = dm_restarted.run_dream_cycle(post_restart_cid, reason="scheduled")
            self.assertEqual(
                len([p for p in r_post_restart["proposals"] if p["kind"] == "asr_correction" and p["status"] == "pending"]),
                0,
                "New character created after anima-brain restart must still inherit SQLite-persisted known_asr_json",
            )

            # 5. Empty dict sync is valid and preserves enabled / interval_seconds
            r_en = client.post(
                "/xiaozhi/dream/config",
                headers=headers,
                json={"character_id": svc.DEFAULT_CHARACTER_ID, "enabled": False, "interval_seconds": 7200},
            )
            self.assertEqual(r_en.status_code, 200)
            r_empty = client.post(
                "/xiaozhi/dream/config",
                headers=headers,
                json={"existing_asr_corrections": {}, "all_characters": True},
            )
            self.assertEqual(r_empty.status_code, 200)
            cfg_empty = r_empty.json()["config"]
            self.assertFalse(cfg_empty["enabled"])
            self.assertEqual(cfg_empty["interval_seconds"], 7200)
            self.assertEqual(cfg_empty["known_asr_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Unit tests for XiaoZhi Anima Brain bridge hardening (backoff, DB pruning, shutdown drain, validation)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

ANIMA_ROOT = Path(__file__).resolve().parent.parent
if str(ANIMA_ROOT) not in sys.path:
    sys.path.insert(0, str(ANIMA_ROOT))
SRC_DIR = ANIMA_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from companion_engine import BrainConfig, CompanionBrain, CommitRequest, Settings
from companion_engine.automation import MemoryAutomation, _sanitize_error_for_log
import xiaozhi_anima_service as svc


class _ListLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


class TestXiaozhiAnimaServiceHardening(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "brain.sqlite3"
        self.settings = Settings(
            embedding_base_url="https://example.test/v1",
            embedding_model="embed-test",
            timeout_seconds=150,
        )
        self.brain = CompanionBrain(
            self.db_path,
            user_id="local",
            settings=self.settings,
            config=BrainConfig(
                start_worker=False,
                automation=MemoryAutomation(
                    embedding=True,
                    summaries=True,
                    reconcile=True,
                    summary_batch_messages=8,
                    daily_jobs=100,
                ),
            ),
        )
        svc.ensure_default_character(self.brain)

    def tearDown(self):
        self.brain.close()
        self._tmp.cleanup()

    def _insert_job(self, jid: str, state: str, created: float, error: str | None = None, kind: str = "maintenance"):
        eng = self.brain.engine
        with eng._transaction():
            eng.db.execute(
                "INSERT INTO memory_jobs VALUES(?,?,?,?,?,?,?,?)",
                (jid, eng.owner, svc.DEFAULT_CHARACTER_ID, kind, state, created, created + 120, error),
            )

    def test_automation_sanitized_logging_redacts_secrets_and_logs_failures(self):
        secret_cases = (
            ("https://user:pw@api.test with Bearer sk-testsecret1234567890 and api_key=AIzaSySecretKey1234567890",
             ("sk-testsecret1234567890", "AIzaSySecretKey1234567890", "user:pw")),
            ('{"api_key": "my_custom_secret_987654321"}', ("my_custom_secret_987654321",)),
            ("{'token': 'my_token_value_987654321'}", ("my_token_value_987654321",)),
            ('x-api-key: "header_secret_987654321"', ("header_secret_987654321",)),
            ("POST https://generativelanguage.googleapis.com/v1beta/models?key=custom_query_secret_999",
             ("custom_query_secret_999",)),
            ("Auth failed with AQ.Ab8RN6abcdef123456.extraSecretSegment999",
             ("Ab8RN6abcdef123456", "extraSecretSegment999")),
            ('Authorization: "Basic dXNlcjpwYXNz"', ("dXNlcjpwYXNz",)),
        )
        for raw_msg, forbidden in secret_cases:
            cleaned = _sanitize_error_for_log(RuntimeError(raw_msg))
            for secret in forbidden:
                self.assertNotIn(secret, cleaned, f"Leaked {secret!r} in {cleaned!r}")
            self.assertIn("[REDACTED]", cleaned)

        # Trigger a real failing embedding job via automation.run_once()
        eng = self.brain.engine
        ctx = eng.prepare_turn(svc.DEFAULT_CHARACTER_ID, "hello", request_id="t-log-1")
        eng.commit_turn(svc.DEFAULT_CHARACTER_ID, ctx.turn_id, CommitRequest(assistant_reply="hi"))
        self.brain.runtime.automation.configure(svc.DEFAULT_CHARACTER_ID, {"embedding": True})

        def _fail_handler(request):
            return httpx.Response(503, json={"error": "secret-body-must-not-appear"})

        self.brain.runtime.transport = httpx.MockTransport(_fail_handler)
        auto_logger = logging.getLogger("companion_engine.automation")
        handler = _ListLogHandler()
        auto_logger.addHandler(handler)
        try:
            ran = self.brain.runtime.automation.run_once()
            self.assertTrue(ran)
            st = self.brain.runtime.automation.status(svc.DEFAULT_CHARACTER_ID)
            self.assertEqual(st["jobs"][0]["state"], "review")
            self.assertNotIn("secret-body-must-not-appear", json.dumps(st))
            joined = "\n".join(handler.records)
            self.assertIn("ProviderError", joined)
            self.assertIn("503", joined)
            self.assertNotIn("secret-body-must-not-appear", joined)
        finally:
            auto_logger.removeHandler(handler)

    def test_review_healer_exponential_backoff_preserves_daily_jobs_and_marks_terminal(self):
        eng = self.brain.engine
        now = time.time()

        # Seed a dangling running operation matching job-1
        with eng._transaction():
            eng.db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("op-1", eng.owner, svc.DEFAULT_CHARACTER_ID, "maintenance", "auto-job-1", "d1", "running", None, None, now - 400),
            )
        # Failure #1: too fresh (100s < 300s base backoff) -> must NOT release yet
        self._insert_job("job-1", "review", now - 100, "provider_or_validation_failed_outcome_unknown")
        self.assertEqual(svc.clear_stale_review_jobs(self.brain, max_age_seconds=300), 0)

        # Age job-1 past 300s -> released as 'auto_retried' (NOT deleted!), dangling op marked failed_unknown
        with eng._transaction():
            eng.db.execute("UPDATE memory_jobs SET created=? WHERE id='job-1'", (now - 350,))
        self.assertEqual(svc.clear_stale_review_jobs(self.brain, max_age_seconds=300), 1)
        row1 = eng.db.execute("SELECT state FROM memory_jobs WHERE id='job-1'").fetchone()
        self.assertIsNotNone(row1)
        self.assertEqual(row1["state"], "auto_retried")
        op1 = eng.db.execute("SELECT status FROM operations WHERE id='op-1'").fetchone()
        self.assertEqual(op1["status"], "failed_unknown")

        # Failure #2: streak=2 requires 3 * 300 = 900s; 400s must NOT release yet
        self._insert_job("job-2", "review", now - 400, "provider_or_validation_failed_outcome_unknown")
        self.assertEqual(svc.clear_stale_review_jobs(self.brain, max_age_seconds=300), 0)
        with eng._transaction():
            eng.db.execute("UPDATE memory_jobs SET created=? WHERE id='job-2'", (now - 950,))
        self.assertEqual(svc.clear_stale_review_jobs(self.brain, max_age_seconds=300), 1)

        # Failures #3, #4 already auto_retried, Failure #5 arrives -> hits MAX_AUTO_REVIEW_RETRIES (5) -> terminal!
        self._insert_job("job-3", "auto_retried", now - 300, "provider_or_validation_failed_outcome_unknown")
        self._insert_job("job-4", "auto_retried", now - 200, "provider_or_validation_failed_outcome_unknown")
        self._insert_job("job-5", "review", now - 100, "provider_or_validation_failed_outcome_unknown")
        self.assertEqual(svc.clear_stale_review_jobs(self.brain, max_age_seconds=300), 0)

        row5 = eng.db.execute("SELECT state, error FROM memory_jobs WHERE id='job-5'").fetchone()
        self.assertEqual(row5["state"], "review")
        self.assertEqual(row5["error"], svc.TERMINAL_REVIEW_ERROR)

        # All 5 rows remain in memory_jobs so daily_jobs circuit breaker counts every attempt
        total_24h = eng.db.execute(
            "SELECT count(*) FROM memory_jobs WHERE owner=? AND character_id=? AND created>?",
            (eng.owner, svc.DEFAULT_CHARACTER_ID, now - 86400),
        ).fetchone()[0]
        self.assertEqual(total_24h, 5)

        # Even if aged far past max backoff, terminal job is NEVER auto-released again
        with eng._transaction():
            eng.db.execute("UPDATE memory_jobs SET created=? WHERE id='job-5'", (now - 99999,))
        self.assertEqual(svc.clear_stale_review_jobs(self.brain, max_age_seconds=300), 0)
        self.assertIsNone(self.brain.runtime.automation.claim())

        # Manual acknowledge clears terminal review so work can resume
        self.brain.runtime.automation.acknowledge(svc.DEFAULT_CHARACTER_ID, "job-5")
        self.assertEqual(eng.db.execute("SELECT state FROM memory_jobs WHERE id='job-5'").fetchone()["state"], "acknowledged")

    def test_startup_recovers_interrupted_and_orphaned_running_jobs_immediately(self):
        eng = self.brain.engine
        now = time.time()
        # Case 1: already transitioned to review(interrupted_outcome_unknown)
        self._insert_job("crash-1", "review", now - 5, "interrupted_outcome_unknown")
        self.assertEqual(svc.clear_stale_review_jobs(self.brain, is_startup=True), 1)
        row = eng.db.execute("SELECT state FROM memory_jobs WHERE id='crash-1'").fetchone()
        self.assertEqual(row["state"], "auto_retried")

        # Case 2: dead prior process left job in state='running' with unexpired lease + running operation
        with eng._transaction():
            eng.db.execute(
                "INSERT INTO memory_jobs VALUES(?,?,?,?,?,?,?,NULL)",
                ("dead-run-1", eng.owner, svc.DEFAULT_CHARACTER_ID, "maintenance", "running", now - 10, now + 110),
            )
            eng.db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("op-dead-1", eng.owner, svc.DEFAULT_CHARACTER_ID, "maintenance", "auto-dead-run-1", "d1", "running", None, None, now - 10),
            )
        self.assertEqual(svc.clear_stale_review_jobs(self.brain, is_startup=True), 1)
        row2 = eng.db.execute("SELECT state, error FROM memory_jobs WHERE id='dead-run-1'").fetchone()
        op2 = eng.db.execute("SELECT status FROM operations WHERE id='op-dead-1'").fetchone()
        self.assertEqual(row2["state"], "auto_retried")
        self.assertEqual(row2["error"], "interrupted_outcome_unknown")
        self.assertEqual(op2["status"], "failed_unknown")

    def test_env_helpers_tolerate_empty_and_invalid_values(self):
        import os

        with patch.dict(os.environ, {"TEST_EMPTY_INT": "", "TEST_BAD_FLOAT": "not-a-num"}):
            self.assertEqual(svc._env_int("TEST_EMPTY_INT", 8, 2, 60), 8)
            self.assertEqual(svc._env_float("TEST_BAD_FLOAT", 150.0, 10.0, 600.0), 150.0)

    def test_prune_database_bloat_compacts_embeddings_and_caps_turn_drafts(self):
        eng = self.brain.engine
        now = time.time()
        big_vec = json.dumps({"vectors": [[0.123456] * 500], "usage": {"total_tokens": 10}})

        # Commit 8 turns + drafts (oldest one includes image_result and must be preserved), plus 1 pending turn
        img_turn_id = ""
        for i in range(8):
            ctx = eng.prepare_turn(svc.DEFAULT_CHARACTER_ID, f"msg {i}", request_id=f"req-{i}")
            eng.commit_turn(svc.DEFAULT_CHARACTER_ID, ctx.turn_id, CommitRequest(assistant_reply=f"reply {i}"))
            payload = {"turn_id": ctx.turn_id, "pad": "x" * 2048}
            if i == 0:
                img_turn_id = ctx.turn_id
                payload["image_result"] = {"status": "succeeded", "artifact": "img.png"}
            with eng._transaction():
                eng.db.execute(
                    "INSERT OR REPLACE INTO turn_drafts VALUES(?,?)",
                    (ctx.turn_id, json.dumps(payload)),
                )
        pending_ctx = eng.prepare_turn(svc.DEFAULT_CHARACTER_ID, "pending msg", request_id="req-pending")
        with eng._transaction():
            eng.db.execute(
                "INSERT OR REPLACE INTO turn_drafts VALUES(?,?)",
                (pending_ctx.turn_id, json.dumps({"turn_id": pending_ctx.turn_id, "pending": True})),
            )
            # Old note_query_embedding (should prune), pending turn query_embedding (must KEEP), speech artifact (must KEEP)
            eng.db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("op-note-old", eng.owner, svc.DEFAULT_CHARACTER_ID, "note_query_embedding", "u1", "d1", "succeeded", big_vec, "{}", now - 7200),
            )
            eng.db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("op-q-pending", eng.owner, svc.DEFAULT_CHARACTER_ID, "query_embedding", "turn-" + pending_ctx.turn_id, "d2", "succeeded", big_vec, "{}", now - 7200),
            )
            eng.db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("op-speech", eng.owner, svc.DEFAULT_CHARACTER_ID, "speech", "sp-1", "d3", "succeeded", json.dumps({"path": "a.wav", "pad": "y" * 2048}), "{}", now - 40 * 86400),
            )

        # Seed enough bloat (>1.2MB) to exercise VACUUM + WAL TRUNCATE physical shrinking too
        big_pad = json.dumps({"vectors": [[0.123456] * 50000], "usage": {"total_tokens": 10}})
        with eng._transaction():
            for idx in range(3):
                eng.db.execute(
                    "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"op-bloat-{idx}", eng.owner, svc.DEFAULT_CHARACTER_ID, "note_query_embedding", f"bloat-{idx}", f"db-{idx}", "succeeded", big_pad, "{}", now - 7200),
                )
        eng.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size_before = self.db_path.stat().st_size
        self.assertGreater(size_before, 1024 * 1024)

        stats = svc.prune_database_bloat(
            self.brain, keep_turn_drafts=3, embed_max_age_seconds=3600, allow_vacuum=True
        )
        self.assertEqual(stats["ops_pruned"], 4)
        self.assertEqual(stats["drafts_deleted"], 4)
        self.assertEqual(stats["vacuumed"], 1)
        size_after = self.db_path.stat().st_size
        self.assertLess(size_after, size_before // 2)

        self.assertEqual(
            eng.db.execute("SELECT result FROM operations WHERE id='op-note-old'").fetchone()["result"],
            '{"pruned":true}',
        )
        self.assertGreater(
            len(eng.db.execute("SELECT result FROM operations WHERE id='op-q-pending'").fetchone()["result"]),
            1024,
        )
        self.assertIsNotNone(eng.db.execute("SELECT 1 FROM operations WHERE id='op-speech'").fetchone())
        # Pending turn draft + image_result draft + 3 newest non-image committed drafts kept = 5 total
        self.assertEqual(eng.db.execute("SELECT count(*) FROM turn_drafts").fetchone()[0], 5)
        self.assertIsNotNone(
            eng.db.execute("SELECT 1 FROM turn_drafts WHERE turn_id=?", (pending_ctx.turn_id,)).fetchone()
        )
        self.assertIsNotNone(
            eng.db.execute("SELECT 1 FROM turn_drafts WHERE turn_id=?", (img_turn_id,)).fetchone()
        )

    def test_http_body_validation_and_graceful_shutdown_drain(self):
        token = "test-token-" + ("a" * 24)
        drained = []

        def _slow_extract(brain, char_id, req_id, user_msg, reply_msg):
            time.sleep(0.15)
            drained.append((char_id, req_id, user_msg, reply_msg))
            return {"committed": True, "turn_id": "t-1"}

        frontend_override = svc.FRONTEND_DIR if (svc.FRONTEND_DIR / "index.html").is_file() else None
        with (
            patch.object(svc, "extract_and_commit_turn", side_effect=_slow_extract),
            patch.object(svc, "FRONTEND_DIR", frontend_override),
        ):
            app = svc.create_combined_asgi_app(self.brain, token)
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                headers = {"Authorization": f"Bearer {token}"}
                # Non-dict and malformed JSON payloads must return 400 cleanly instead of 200/500
                for bad_json in ("[]", "null", '"hello"', "123", "{bad json"):
                    r = client.post(
                        "/xiaozhi/recall",
                        headers={**headers, "Content-Type": "application/json"},
                        content=bad_json,
                    )
                    self.assertEqual(r.status_code, 400, f"Expected 400 for body={bad_json}, got {r.status_code}")

                # Valid ingest right before lifespan shutdown must finish draining
                r_ing = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={
                        "character_id": svc.DEFAULT_CHARACTER_ID,
                        "request_id": "drain-check-1",
                        "user_message": "晚安",
                        "assistant_reply": "晚安好梦",
                    },
                )
                self.assertEqual(r_ing.status_code, 200)
                self.assertEqual(r_ing.json()["queued"], 1)
            # Exiting TestClient triggers lifespan.shutdown -> must have drained!
            self.assertEqual(len(drained), 1)
            self.assertEqual(drained[0][1], "drain-check-1")

    def test_offline_relationship_hot_control_roundtrip_and_log_token_redaction(self):
        # Exercises full relationship preset + slider + recall roundtrip against isolated temp SQLite DB
        # (never mutates production data/anima/brain.sqlite3)
        token = "test-rel-token-" + ("b" * 24)
        frontend_override = svc.FRONTEND_DIR if (svc.FRONTEND_DIR / "index.html").is_file() else None
        with patch.object(svc, "FRONTEND_DIR", frontend_override):
            app = svc.create_combined_asgi_app(self.brain, token)
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                headers = {"Authorization": f"Bearer {token}"}

                r1 = client.post(
                    "/xiaozhi/relationship",
                    headers=headers,
                    json={"character_id": svc.DEFAULT_CHARACTER_ID, "preset": "restrained"},
                )
                self.assertEqual(r1.status_code, 200)
                rel1 = r1.json()
                self.assertEqual(rel1["preset"], "restrained")
                self.assertFalse(rel1["romance_enabled"])
                self.assertAlmostEqual(rel1["axes"]["familiarity"], 0.30, places=2)

                rec1 = client.post(
                    "/xiaozhi/recall",
                    headers=headers,
                    json={"character_id": svc.DEFAULT_CHARACTER_ID, "query": "你好"},
                ).json()
                self.assertIn("克制得体", rec1.get("context_text", ""))

                r2 = client.post(
                    "/xiaozhi/relationship",
                    headers=headers,
                    json={"character_id": svc.DEFAULT_CHARACTER_ID, "preset": "intimate"},
                )
                self.assertEqual(r2.status_code, 200)
                rel2 = r2.json()
                self.assertEqual(rel2["preset"], "intimate")
                self.assertTrue(rel2["romance_enabled"])
                self.assertEqual(rel2["stage"], "close")
                self.assertEqual(rel2["bands"]["familiarity"], 3)
                self.assertAlmostEqual(rel2["axes"]["romance"], 0.85, places=2)

                rec2 = client.post(
                    "/xiaozhi/recall",
                    headers=headers,
                    json={"character_id": svc.DEFAULT_CHARACTER_ID, "query": "晚安"},
                ).json()
                self.assertIn("深度亲密依恋", rec2.get("context_text", ""))
                self.assertIn("stage=close", rec2.get("mood", ""))

                r3 = client.post(
                    "/xiaozhi/relationship",
                    headers=headers,
                    json={
                        "character_id": svc.DEFAULT_CHARACTER_ID,
                        "romance_enabled": False,
                        "familiarity": 0.76,
                        "warmth": 0.76,
                        "trust": 0.76,
                        "romance": 0.40,
                    },
                )
                self.assertEqual(r3.status_code, 200)
                rel3 = r3.json()
                self.assertEqual(rel3["preset"], "custom")
                self.assertFalse(rel3["romance_enabled"])
                self.assertEqual(rel3["bands"]["familiarity"], 3)
                self.assertAlmostEqual(rel3["axes"]["romance"], 0.40, places=2)

        # Verify startup log line redacts studio token instead of printing raw studio_url
        svc_src = Path(svc.__file__).read_text(encoding="utf-8")
        self.assertNotIn('log.info("Studio URL: %s", studio_url)', svc_src)
        self.assertIn("#token=***", svc_src)

    def test_longer_reply_upsert_and_turn_meta_preserves_earliest_occurred_at(self):
        t0 = 1795000000.0
        r1 = svc.extract_and_commit_turn(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            "xz-tool-round-1",
            "帮我查下明天长沙天气",
            "我帮你查一下",
            turn_meta={"occurred_at": t0, "source": "esp32", "device_id": "aa:bb", "used_tools": False},
        )
        self.assertTrue(r1["committed"])
        self.assertFalse(r1.get("replayed", False))
        turn_id = r1["turn_id"]

        # Fuller post-tool reply arrives with SAME request_id -> must upsert turns, evidence & turn_meta
        r2 = svc.extract_and_commit_turn(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            "xz-tool-round-1",
            "帮我查下明天长沙天气",
            "我帮你查一下 明天长沙多云转晴，16到24度。",
            turn_meta={"occurred_at": t0 + 25.0, "source": "esp32", "device_id": "aa:bb", "used_tools": True},
        )
        self.assertTrue(r2["committed"])
        self.assertTrue(r2.get("updated"))
        self.assertFalse(r2.get("replayed"))

        eng = self.brain.engine
        t_row = eng.db.execute("SELECT assistant_reply FROM turns WHERE id=?", (turn_id,)).fetchone()
        self.assertEqual(t_row["assistant_reply"], "我帮你查一下 明天长沙多云转晴，16到24度。")
        ev_row = eng.db.execute("SELECT text FROM evidence WHERE id=?", (f"{turn_id}:assistant",)).fetchone()
        self.assertEqual(ev_row["text"], "我帮你查一下 明天长沙多云转晴，16到24度。")
        meta_row = eng.db.execute("SELECT * FROM xiaozhi_turn_meta WHERE turn_id=?", (turn_id,)).fetchone()
        self.assertAlmostEqual(meta_row["occurred_at"], t0, places=2)
        self.assertEqual(meta_row["used_tools"], 1)
        self.assertEqual(meta_row["device_id"], "aa:bb")

        # Shorter stale replay must NOT downgrade the fuller reply
        r3 = svc.extract_and_commit_turn(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            "xz-tool-round-1",
            "帮我查下明天长沙天气",
            "我帮你查一下",
            turn_meta={"occurred_at": t0 + 40.0, "used_tools": False},
        )
        self.assertTrue(r3["committed"])
        self.assertTrue(r3["replayed"])
        self.assertFalse(r3["updated"])
        t_row2 = eng.db.execute("SELECT assistant_reply FROM turns WHERE id=?", (turn_id,)).fetchone()
        self.assertEqual(t_row2["assistant_reply"], "我帮你查一下 明天长沙多云转晴，16到24度。")

        # Metadata-only replay shifting occurred_at earlier must mark updated=True and expose prev_occurred_at
        r4 = svc.extract_and_commit_turn(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            "xz-tool-round-1",
            "帮我查下明天长沙天气",
            "我帮你查一下 明天长沙多云转晴，16到24度。",
            turn_meta={"occurred_at": t0 - 3600.0, "used_tools": True},
        )
        self.assertTrue(r4["committed"])
        self.assertTrue(r4["updated"])
        self.assertAlmostEqual(r4["occurred_at"], t0 - 3600.0, places=2)
        self.assertAlmostEqual(r4["prev_occurred_at"], t0, places=2)

    def test_ingest_retries_on_another_turn_pending_instead_of_dropping(self):
        token = "retry-pending-token-" + ("d" * 24)
        # Hold a pending Studio turn so initial ingest attempt hits another_turn_pending
        pending_ctx = self.brain.engine.prepare_turn(
            svc.DEFAULT_CHARACTER_ID, "网页正在聊天", request_id="studio-pending-1"
        )

        frontend_override = svc.FRONTEND_DIR if (svc.FRONTEND_DIR / "index.html").is_file() else None
        with (
            patch.object(svc, "FRONTEND_DIR", frontend_override),
            patch.object(svc, "INGEST_PENDING_INLINE_WAIT_SECONDS", 0.05),
            patch.object(svc, "INGEST_PENDING_BACKOFF_BASE", 0.1),
            patch.object(svc, "INGEST_PENDING_MAX_RETRIES", 8),
        ):
            app = svc.create_combined_asgi_app(self.brain, token)
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                headers = {"Authorization": f"Bearer {token}"}
                r = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={
                        "character_id": svc.DEFAULT_CHARACTER_ID,
                        "request_id": "xz-delayed-by-pending",
                        "user_message": "哥哥刚刚回来",
                        "assistant_reply": "欢迎回来哥哥。",
                        "occurred_at": time.time() - 10,
                    },
                )
                self.assertEqual(r.status_code, 200)
                time.sleep(0.15)
                h_mid = client.get("/xiaozhi/health").json()
                self.assertGreaterEqual(h_mid["ingest"]["retried_total"], 1)
                self.assertEqual(h_mid["ingest"]["dropped_total"], 0)

                # Clear the pending turn -> delayed retry MUST commit the voice turn!
                self.brain.engine.commit_turn(
                    svc.DEFAULT_CHARACTER_ID,
                    pending_ctx.turn_id,
                    CommitRequest(assistant_reply="网页回复完毕"),
                )
                deadline = time.monotonic() + 3.0
                found = None
                while time.monotonic() < deadline:
                    found = self.brain.engine.db.execute(
                        "SELECT status, assistant_reply FROM turns WHERE request_id='xz-delayed-by-pending'"
                    ).fetchone()
                    if found and found["status"] == "committed":
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(found)
                self.assertEqual(found["status"], "committed")
                self.assertEqual(found["assistant_reply"], "欢迎回来哥哥。")

    def test_ingest_queue_full_backpressure_and_health_dropped_metrics(self):
        import threading

        token = "queue-full-token-" + ("e" * 24)
        gate = threading.Event()
        started = threading.Event()

        def _blocked_extract(brain, char_id, req_id, user_msg, reply_msg, **kwargs):
            started.set()
            gate.wait(timeout=3.0)
            return {"committed": True, "turn_id": f"tid-{req_id}"}

        frontend_override = svc.FRONTEND_DIR if (svc.FRONTEND_DIR / "index.html").is_file() else None
        with (
            patch.object(svc, "FRONTEND_DIR", frontend_override),
            patch.object(svc, "extract_and_commit_turn", side_effect=_blocked_extract),
            patch.object(svc, "INGEST_QUEUE_MAXSIZE", 1),
            patch.object(svc, "INGEST_OVERFLOW_MAX_TASKS", 0),
            patch.object(svc, "INGEST_ENQUEUE_WAIT_SECONDS", 0.03),
            patch.object(svc, "SHUTDOWN_DRAIN_SECONDS", 1.5),
        ):
            app = svc.create_combined_asgi_app(self.brain, token)
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                headers = {"Authorization": f"Bearer {token}"}
                # 1st item picked up by worker (blocked on gate)
                r1 = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={"request_id": "qf-1", "user_message": "u1", "assistant_reply": "a1"},
                )
                self.assertEqual(r1.status_code, 200)
                self.assertTrue(started.wait(timeout=2.0))
                # 2nd item fills queue (maxsize=1)
                r2 = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={"request_id": "qf-2", "user_message": "u2", "assistant_reply": "a2"},
                )
                self.assertEqual(r2.status_code, 200)
                # 3rd item overflows queue & overflow=0 -> 503 with rejected_ids and health dropped counter
                r3 = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={"request_id": "qf-3", "user_message": "u3", "assistant_reply": "a3"},
                )
                self.assertEqual(r3.status_code, 503)
                body3 = r3.json()
                self.assertEqual(body3["queued"], 0)
                self.assertEqual(body3["rejected_ids"], ["qf-3"])

                h = client.get("/xiaozhi/health").json()
                self.assertEqual(h["ingest"]["dropped_total"], 1)
                self.assertEqual(h["ingest"]["dropped_queue_full"], 1)
                gate.set()

    def test_graceful_shutdown_harvests_sleeping_retry_tasks_instead_of_dropping(self):
        token = "shutdown-retry-token-" + ("f" * 24)
        pending_ctx = self.brain.engine.prepare_turn(
            svc.DEFAULT_CHARACTER_ID, "网页端占用中", request_id="studio-lock-shutdown"
        )
        frontend_override = svc.FRONTEND_DIR if (svc.FRONTEND_DIR / "index.html").is_file() else None
        with (
            patch.object(svc, "FRONTEND_DIR", frontend_override),
            patch.object(svc, "INGEST_PENDING_INLINE_WAIT_SECONDS", 0.02),
            patch.object(svc, "INGEST_PENDING_BACKOFF_BASE", 10.0),
            patch.object(svc, "SHUTDOWN_DRAIN_SECONDS", 5.0),
        ):
            app = svc.create_combined_asgi_app(self.brain, token)
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                headers = {"Authorization": f"Bearer {token}"}
                r = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={
                        "character_id": svc.DEFAULT_CHARACTER_ID,
                        "request_id": "xz-harvest-on-shutdown",
                        "user_message": "关停前别丢我",
                        "assistant_reply": "放心，关停前会稳稳落库。",
                    },
                )
                self.assertEqual(r.status_code, 200)
                # Wait until turn enters the 10s retry_tasks sleep
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if client.get("/xiaozhi/health").json()["ingest"]["pending_retries"] >= 1:
                        break
                    time.sleep(0.05)
                self.assertEqual(client.get("/xiaozhi/health").json()["ingest"]["pending_retries"], 1)
                # Release pending lock right before lifespan.shutdown fires
                self.brain.engine.commit_turn(
                    svc.DEFAULT_CHARACTER_ID,
                    pending_ctx.turn_id,
                    CommitRequest(assistant_reply="网页端结束"),
                )
            # After TestClient exits (lifespan.shutdown complete), the sleeping retry turn MUST be committed!
            row = self.brain.engine.db.execute(
                "SELECT status, assistant_reply FROM turns WHERE request_id='xz-harvest-on-shutdown'"
            ).fetchone()
            self.assertIsNotNone(row, "Deferred retry turn was silently dropped on graceful shutdown")
            self.assertEqual(row["status"], "committed")
            self.assertEqual(row["assistant_reply"], "放心，关停前会稳稳落库。")

    def test_p223_load_or_create_token_retries_io_and_refuses_silent_overwrite(self):
        tf = Path(self._tmp.name) / "studio_token.txt"
        tok1 = svc.load_or_create_token(tf)
        self.assertGreaterEqual(len(tok1), 32)
        self.assertEqual(list(Path(self._tmp.name).glob("*.tmp")), [])
        self.assertEqual(svc.load_or_create_token(tf), tok1)

        # Transient OSError recovers without regenerating
        orig_read = Path.read_text
        state = {"n": 0}

        def _flaky_read(self_path, *args, **kwargs):
            if self_path == tf:
                state["n"] += 1
                if state["n"] == 1:
                    raise PermissionError("transient lock")
            return orig_read(self_path, *args, **kwargs)

        with patch.object(Path, "read_text", _flaky_read):
            self.assertEqual(svc.load_or_create_token(tf, retries=3), tok1)
        self.assertEqual(state["n"], 2)

        # Persistent OSError raises and leaves existing token untouched
        def _always_fail(self_path, *args, **kwargs):
            if self_path == tf:
                raise OSError("persistent io error")
            return orig_read(self_path, *args, **kwargs)

        with patch.object(Path, "read_text", _always_fail):
            with self.assertRaises(RuntimeError):
                svc.load_or_create_token(tf, retries=2)
        self.assertEqual(tf.read_text(encoding="utf-8").strip(), tok1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

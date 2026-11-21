"""Unit tests for XiaoZhi Anima Brain bridge hardening (backoff, DB pruning, shutdown drain, validation)."""

from __future__ import annotations

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

    def test_batch1_b2_online_backup_integrity_and_retention(self):
        import sqlite3
        import datetime

        # Commit a real turn so backup DB contains verifiable data
        svc.extract_and_commit_turn(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            "b2-backup-turn-1",
            "帮我记住今天喝了拿铁",
            "好呀，记住今天喝了拿铁。",
        )
        bdir = Path(self._tmp.name) / "backups_test"
        res = svc.run_online_backup(self.brain, bdir, force=True, keep_daily=7, keep_weekly=4)
        self.assertTrue(res["ok"], f"Backup failed: {res}")
        self.assertTrue(res.get("integrity_ok"))
        self.assertEqual(res["daily_count"], 1)
        self.assertEqual(res["weekly_count"], 1)

        bk_file = bdir / res["last_backup_file"]
        self.assertTrue(bk_file.is_file())
        conn = sqlite3.connect(str(bk_file))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            cnt = conn.execute("SELECT count(*) FROM turns WHERE request_id='b2-backup-turn-1'").fetchone()[0]
            self.assertEqual(cnt, 1)
        finally:
            conn.close()

        # Retention pruning: 10 days + 6 weeks -> keeps newest 7 daily and 4 weekly
        base_dt = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
        for d_off in range(10):
            ts = (base_dt - datetime.timedelta(days=d_off)).timestamp()
            svc.run_online_backup(self.brain, bdir, now=ts, force=True, keep_daily=7, keep_weekly=4)
        for w_off in range(6):
            ts = (base_dt - datetime.timedelta(weeks=w_off)).timestamp()
            svc.run_online_backup(self.brain, bdir, now=ts, force=True, keep_daily=7, keep_weekly=4)
        dailies, weeklies = svc._scan_backup_dir(bdir)
        self.assertLessEqual(len(dailies), 7)
        self.assertLessEqual(len(weeklies), 4)

        # Integrity check failure must NOT leave corrupted daily file or overwrite good one
        bdir_bad = Path(self._tmp.name) / "backups_bad"
        with patch.object(svc, "_verify_sqlite_integrity", return_value=(False, "corrupt page")):
            bad_res = svc.run_online_backup(self.brain, bdir_bad, force=True)
        self.assertFalse(bad_res["ok"])
        self.assertFalse(bad_res.get("integrity_ok"))
        self.assertEqual(list(bdir_bad.glob("brain-*.sqlite3")), [])

        # _resolve_backup_dir must resolve beside brain.engine.database (NOT fallback to DATA_DIR!)
        with patch.object(svc, "BACKUP_DIR_ENV", ""):
            resolved_dir = svc._resolve_backup_dir(self.brain)
            self.assertEqual(resolved_dir, Path(self.brain.engine.database).resolve().parent / "backups")

        # Same-day restart with fresh state & missing weekly backup: verifies integrity and backfills weekly!
        bdir_restart = Path(self._tmp.name) / "backups_restart"
        r_init = svc.run_online_backup(self.brain, bdir_restart, force=True)
        self.assertTrue(r_init["ok"])
        for wf in bdir_restart.glob("brain-weekly-*.sqlite3"):
            wf.unlink()
        with svc._BACKUP_STATE_LOCK:
            svc._BACKUP_STATE_BY_DIR.pop(svc._backup_dir_key(bdir_restart), None)
        r_same_day = svc.run_online_backup(self.brain, bdir_restart, force=False)
        self.assertTrue(r_same_day["ok"])
        self.assertEqual(r_same_day.get("skipped"), "already_exists")
        self.assertTrue(r_same_day.get("integrity_ok"))
        self.assertEqual(r_same_day["weekly_count"], 1)

        # Corrupted existing daily backup on same-day check must be recreated cleanly
        daily_f = next(bdir_restart.glob("brain-20*.sqlite3"))
        daily_f.write_bytes(b"not-a-valid-sqlite-file")
        with svc._BACKUP_STATE_LOCK:
            svc._BACKUP_STATE_BY_DIR.pop(svc._backup_dir_key(bdir_restart), None)
        r_healed = svc.run_online_backup(self.brain, bdir_restart, force=False)
        self.assertTrue(r_healed["ok"])
        self.assertTrue(r_healed.get("created"))
        self.assertTrue(r_healed.get("integrity_ok"))

        # Empty backup dir status must NOT inherit stale state from another directory
        bdir_empty = Path(self._tmp.name) / "backups_empty"
        st_empty = svc.get_backup_status(self.brain, bdir_empty)
        self.assertEqual(st_empty["daily_count"], 0)
        self.assertEqual(st_empty["last_backup_at"], "")
        self.assertEqual(st_empty["last_backup_file"], "")

    def test_batch1_b3_wal_governance_and_b4_uuid_and_collision_guard(self):
        # B-3: PRAGMA journal_size_limit=8388608 & idle wal_checkpoint(TRUNCATE)
        svc.configure_sqlite_wal(self.brain.engine, 8 * 1024 * 1024)
        lim = self.brain.engine.db.execute("PRAGMA journal_size_limit").fetchone()[0]
        self.assertEqual(lim, 8 * 1024 * 1024)
        skip_busy = svc.checkpoint_wal_if_idle(self.brain, is_idle=False)
        self.assertEqual(skip_busy.get("skipped"), "busy")
        cp_ok = svc.checkpoint_wal_if_idle(self.brain, is_idle=True)
        self.assertTrue(cp_ok.get("ok"))

        # B-4: _new_request_id uses uuid4 hex (unique across rapid calls)
        ids = {svc._new_request_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for rid in ids:
            self.assertRegex(rid, r"^xz-[0-9a-f]{32}$")

        # B-4: Same request_id with DIFFERENT user_message or divergent LONGER reply must NOT overwrite!
        r1 = svc.extract_and_commit_turn(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            "b4-collision-id",
            "第一轮问题",
            "第一轮原始回复。",
        )
        self.assertTrue(r1["committed"])
        tid = r1["turn_id"]

        with self.assertLogs("anima-bridge", level="WARNING") as cm_user:
            r_diff_u = svc.extract_and_commit_turn(
                self.brain,
                svc.DEFAULT_CHARACTER_ID,
                "b4-collision-id",
                "完全不同的另一个用户问题",
                "这是一个长得多得多的完全不同的回复，绝不能覆盖第一轮！",
            )
        self.assertTrue(r_diff_u.get("conflict"))
        self.assertFalse(r_diff_u.get("updated"))
        self.assertTrue(any("collision" in m for m in cm_user.output))

        with self.assertLogs("anima-bridge", level="WARNING") as cm_rep:
            r_diff_a = svc.extract_and_commit_turn(
                self.brain,
                svc.DEFAULT_CHARACTER_ID,
                "b4-collision-id",
                "第一轮问题",
                "这是一条与原始回复完全不相关但更长的回复内容，也不应覆盖。",
            )
        self.assertTrue(r_diff_a.get("conflict"))
        self.assertFalse(r_diff_a.get("updated"))
        self.assertTrue(any("conflict" in m for m in cm_rep.output))

        stored = self.brain.engine.db.execute(
            "SELECT user_message, assistant_reply FROM turns WHERE id=?", (tid,)
        ).fetchone()
        self.assertEqual(stored["user_message"], "第一轮问题")
        self.assertEqual(stored["assistant_reply"], "第一轮原始回复。")

    def test_batch1_b6_health_grading_and_a5_clickjacking_headers(self):
        token = "b6-a5-health-token-" + ("g" * 24)
        frontend_override = svc.FRONTEND_DIR if (svc.FRONTEND_DIR / "index.html").is_file() else None
        with patch.object(svc, "FRONTEND_DIR", frontend_override):
            app = svc.create_combined_asgi_app(self.brain, token)
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                # 1. Healthy /xiaozhi/health: 200, db_ok, worker_alive, backlog_ok, backup, NO host_export_dir!
                r_ok = client.get("/xiaozhi/health")
                self.assertEqual(r_ok.status_code, 200)
                self.assertEqual(r_ok.headers.get("x-frame-options"), "DENY")
                self.assertIn("frame-ancestors 'none'", r_ok.headers.get("content-security-policy", ""))
                body = r_ok.json()
                self.assertEqual(body["status"], "ok")
                self.assertTrue(body["db_ok"])
                self.assertTrue(body["worker_alive"])
                self.assertTrue(body["backlog_ok"])
                self.assertIn("backup", body)
                self.assertNotIn(
                    "host_export_dir",
                    body.get("obsidian") or {},
                    "Unauthenticated /xiaozhi/health must NOT expose host_export_dir",
                )

                # 2. Authenticated /xiaozhi/obsidian/status still includes host_export_dir for 18080 console
                r_obs = client.get(
                    "/xiaozhi/obsidian/status",
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(r_obs.status_code, 200)
                self.assertIn("host_export_dir", r_obs.json())

                # 3. Non-ASCII raw Authorization header bytes via ASGI -> 401 (never TypeError/500)
                import asyncio as _aio

                sent_na = []

                async def _send_na():
                    async def _recv():
                        return {"type": "http.request", "body": b"", "more_body": False}

                    async def _snd(msg):
                        sent_na.append(msg)

                    scope = {
                        "type": "http",
                        "method": "POST",
                        "path": "/xiaozhi/recall",
                        "query_string": b"",
                        "headers": [
                            (b"host", b"127.0.0.1:18787"),
                            (b"authorization", b"Bearer \xe4\xb8\xad\xe6\x96\x87"),
                        ],
                    }
                    await app(scope, _recv, _snd)

                _aio.run(_send_na())
                self.assertEqual(sent_na[0]["status"], 401)
                na_hdrs = {k.lower(): v for k, v in sent_na[0]["headers"]}
                self.assertEqual(na_hdrs.get(b"x-frame-options"), b"DENY")
                self.assertIn(b"frame-ancestors 'none'", na_hdrs.get(b"content-security-policy", b""))

                # 3b. Non-ASCII Authorization on Studio /api/bootstrap must return 401 (NEVER 500!)
                sent_studio_na = []

                async def _send_studio_na():
                    async def _recv():
                        return {"type": "http.request", "body": b"", "more_body": False}

                    async def _snd(msg):
                        sent_studio_na.append(msg)

                    scope = {
                        "type": "http",
                        "method": "GET",
                        "path": "/api/bootstrap",
                        "query_string": b"",
                        "headers": [
                            (b"host", b"127.0.0.1:18787"),
                            (b"authorization", b"Bearer \xe4\xb8\xad\xe6\x96\x87"),
                        ],
                    }
                    await app(scope, _recv, _snd)

                _aio.run(_send_studio_na())
                self.assertEqual(sent_studio_na[0]["status"], 401)

                # 4. Studio route carries clickjacking protection headers (A-5)
                r_studio = client.get("/")
                self.assertEqual(r_studio.headers.get("x-frame-options"), "DENY")
                self.assertIn("frame-ancestors 'none'", r_studio.headers.get("content-security-policy", ""))

                # 5. DB check failure -> 503 with db_ok=False
                orig_db = self.brain.engine.db

                class _BrokenDB:
                    def execute(self, *a, **kw):
                        raise RuntimeError("sqlite disk I/O error")

                self.brain.engine.db = _BrokenDB()
                try:
                    r_db_fail = client.get("/xiaozhi/health")
                    self.assertEqual(r_db_fail.status_code, 503)
                    self.assertFalse(r_db_fail.json()["db_ok"])
                    self.assertEqual(r_db_fail.json()["status"], "unhealthy")
                finally:
                    self.brain.engine.db = orig_db

            # 6. Outside lifespan (worker not running / done) -> 503 with worker_alive=False
            sent_dead = []

            async def _probe_no_worker():
                async def _recv():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def _snd(msg):
                    sent_dead.append(msg)

                await app(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/xiaozhi/health",
                        "query_string": b"",
                        "headers": [(b"host", b"127.0.0.1:18787")],
                    },
                    _recv,
                    _snd,
                )

            _aio.run(_probe_no_worker())
            self.assertEqual(sent_dead[0]["status"], 503)
            dead_body = json.loads(sent_dead[1]["body"].decode("utf-8"))
            self.assertFalse(dead_body["worker_alive"])
            self.assertEqual(dead_body["status"], "unhealthy")

            # 7. Direct companion_engine.http_server.create_app (bypassing xiaozhi_anima_service wrapper)
            #    MUST emit X-Frame-Options: DENY (A-5) and reject non-ASCII Authorization with 401 (E-5, never TypeError).
            from companion_engine.http_server import create_app as create_studio_app

            direct_app = create_studio_app(self.brain.runtime, token=token, port=18787, start_worker=False)
            with TestClient(direct_app, base_url="http://127.0.0.1:18787") as direct_client:
                r_dir_ok = direct_client.get(
                    "/api/bootstrap",
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(r_dir_ok.status_code, 200)
                self.assertEqual(r_dir_ok.headers.get("x-frame-options"), "DENY")

            sent_direct_na = []

            async def _send_direct_na():
                async def _recv():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def _snd(msg):
                    sent_direct_na.append(msg)

                await direct_app(
                    {
                        "type": "http",
                        "asgi": {"version": "3.0"},
                        "http_version": "1.1",
                        "method": "GET",
                        "scheme": "http",
                        "path": "/api/bootstrap",
                        "raw_path": b"/api/bootstrap",
                        "query_string": b"",
                        "root_path": "",
                        "client": ("127.0.0.1", 12345),
                        "server": ("127.0.0.1", 18787),
                        "headers": [
                            (b"host", b"127.0.0.1:18787"),
                            (b"authorization", b"Bearer \xe4\xb8\xad\xe6\x96\x87"),
                        ],
                    },
                    _recv,
                    _snd,
                )

            _aio.run(_send_direct_na())
            self.assertEqual(sent_direct_na[0]["status"], 401)

    def test_batch3_b1_inbox_persists_before_http_200_and_b4_conflict_semantics(self):
        import threading

        token = "b1-persist-token-" + ("h" * 24)
        # Verify exact xiaozhi_inbox schema columns
        svc.ensure_xiaozhi_inbox_table(self.brain.engine)
        cols = [
            r["name"]
            for r in self.brain.engine.db.execute("PRAGMA table_info(xiaozhi_inbox)").fetchall()
        ]
        self.assertEqual(
            cols,
            [
                "request_id",
                "character_id",
                "payload",
                "status",
                "attempts",
                "last_error",
                "created_at",
                "updated_at",
            ],
        )

        gate = threading.Event()
        started_t1 = threading.Event()
        real_extract = svc.extract_and_commit_turn
        seen_t1_calls = []

        def _gated_extract(brain, char_id, req_id, user_msg, reply_msg, **kwargs):
            if req_id == "b1-req-1":
                seen_t1_calls.append(reply_msg)
                if len(seen_t1_calls) == 1:
                    started_t1.set()
                    gate.wait(timeout=3.0)
            return real_extract(brain, char_id, req_id, user_msg, reply_msg, **kwargs)

        frontend_override = svc.FRONTEND_DIR if (svc.FRONTEND_DIR / "index.html").is_file() else None
        with (
            patch.object(svc, "FRONTEND_DIR", frontend_override),
            patch.object(svc, "extract_and_commit_turn", side_effect=_gated_extract),
        ):
            app = svc.create_combined_asgi_app(self.brain, token)
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                headers = {"Authorization": f"Bearer {token}"}
                # 1. Send b1-req-1 -> worker claims it ('processing') and blocks on gate
                r1 = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={
                        "request_id": "b1-req-1",
                        "user_message": "查一下明天天气",
                        "assistant_reply": "我帮你查一下",
                    },
                )
                self.assertEqual(r1.status_code, 200)
                self.assertTrue(started_t1.wait(timeout=2.0))

                # 2. Send b1-req-2 while worker is blocked -> MUST already be committed in xiaozhi_inbox BEFORE HTTP 200!
                r2 = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={
                        "request_id": "b1-req-2",
                        "user_message": "我喜欢拿铁",
                        "assistant_reply": "记住你喜欢拿铁啦。",
                    },
                )
                self.assertEqual(r2.status_code, 200)
                self.assertEqual(r2.json()["accepted_ids"], ["b1-req-2"])
                row2 = self.brain.engine.db.execute(
                    "SELECT status, payload FROM xiaozhi_inbox WHERE request_id='b1-req-2'"
                ).fetchone()
                self.assertIsNotNone(row2)
                self.assertEqual(row2["status"], "pending")

                # 3. B-4 duplicate idempotency & conflict guard at inbox layer
                r2_dup = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={
                        "request_id": "b1-req-2",
                        "user_message": "我喜欢拿铁",
                        "assistant_reply": "记住你喜欢拿铁啦。",
                    },
                )
                self.assertEqual(r2_dup.status_code, 200)
                with self.assertLogs("anima-bridge", level="WARNING") as cm_conf:
                    r2_conflict_u = client.post(
                        "/xiaozhi/ingest",
                        headers=headers,
                        json={
                            "request_id": "b1-req-2",
                            "user_message": "完全不同的另一句话",
                            "assistant_reply": "完全不同的更长回复不能覆盖原始记录！",
                        },
                    )
                    r2_conflict_a = client.post(
                        "/xiaozhi/ingest",
                        headers=headers,
                        json={
                            "request_id": "b1-req-2",
                            "user_message": "我喜欢拿铁",
                            "assistant_reply": "另一条完全不相关且更长的回复也不能覆盖！",
                        },
                    )
                self.assertEqual(r2_conflict_u.status_code, 200)
                self.assertEqual(r2_conflict_a.status_code, 200)
                self.assertTrue(any("conflict" in m for m in cm_conf.output))
                row2_after = self.brain.engine.db.execute(
                    "SELECT payload FROM xiaozhi_inbox WHERE request_id='b1-req-2'"
                ).fetchone()
                p2 = json.loads(row2_after["payload"])
                self.assertEqual(p2["user_message"], "我喜欢拿铁")
                self.assertEqual(p2["assistant_reply"], "记住你喜欢拿铁啦。")

                # 4. Mid-processing continuation upgrade for b1-req-1 -> worker MUST re-run and commit fuller reply!
                r1_full = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={
                        "request_id": "b1-req-1",
                        "user_message": "查一下明天天气",
                        "assistant_reply": "我帮你查一下 明天长沙晴，18到26度。",
                        "used_tools": True,
                    },
                )
                self.assertEqual(r1_full.status_code, 200)
                gate.set()

                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    st = svc.get_inbox_stats(self.brain)
                    if st["done"] == 2 and st["pending"] == 0 and st["processing"] == 0:
                        break
                    time.sleep(0.03)

                st_done = svc.get_inbox_stats(self.brain)
                self.assertEqual(st_done["done"], 2)
                self.assertEqual(st_done["pending"], 0)
                self.assertEqual(st_done["processing"], 0)
                t1 = self.brain.engine.db.execute(
                    "SELECT assistant_reply FROM turns WHERE request_id='b1-req-1'"
                ).fetchone()
                self.assertIsNotNone(t1)
                self.assertEqual(t1["assistant_reply"], "我帮你查一下 明天长沙晴，18到26度。")

    def test_batch3_b1_simulated_crash_and_restart_reconciliation_zero_lost_turns(self):
        token = "b1-crash-token-" + ("i" * 24)
        frontend_override = svc.FRONTEND_DIR if (svc.FRONTEND_DIR / "index.html").is_file() else None
        client_sent: list[tuple[str, str, str]] = [
            (f"b1-crash-{idx:02d}", f"用户消息 {idx}", f"助手回复 {idx}")
            for idx in range(1, 13)
        ]

        # Phase 1: Process turns 1-2 to completion, leave turn 3 stuck in 'processing',
        # and persist turns 4-8 as 'pending' — then simulate abrupt process kill (no shutdown drain!).
        svc.ensure_xiaozhi_inbox_table(self.brain.engine)
        for req_id, u_msg, a_msg in client_sent[:2]:
            enq = svc.enqueue_inbox_turn(self.brain, svc.DEFAULT_CHARACTER_ID, req_id, u_msg, a_msg)
            self.assertTrue(enq["accepted"])
            row, _ = svc._claim_next_inbox_row(self.brain)
            self.assertIsNotNone(row)
            res = svc.extract_and_commit_turn(
                self.brain, svc.DEFAULT_CHARACTER_ID, req_id, u_msg, a_msg
            )
            self.assertTrue(res["committed"])
            self.assertTrue(svc._complete_inbox_row(self.brain, req_id, row["payload"]))

        # Turn 3 claimed as 'processing' when process is SIGKILLed mid-flight
        r3_id, r3_u, r3_a = client_sent[2]
        svc.enqueue_inbox_turn(self.brain, svc.DEFAULT_CHARACTER_ID, r3_id, r3_u, r3_a)
        row3, _ = svc._claim_next_inbox_row(self.brain)
        self.assertIsNotNone(row3)
        self.assertEqual(row3["request_id"], r3_id)

        # Turns 4-8 durably persisted via transactional INSERT OR IGNORE as 'pending' before crash
        for req_id, u_msg, a_msg in client_sent[3:8]:
            enq = svc.enqueue_inbox_turn(self.brain, svc.DEFAULT_CHARACTER_ID, req_id, u_msg, a_msg)
            self.assertTrue(enq["accepted"])

        pre_crash_stats = svc.get_inbox_stats(self.brain)
        self.assertEqual(pre_crash_stats["done"], 2)
        self.assertEqual(pre_crash_stats["processing"], 1)
        self.assertEqual(pre_crash_stats["pending"], 5)

        # Close old brain handle without draining (simulating hard kill & fresh container boot)
        self.brain.close()

        # Phase 2: Fresh CompanionBrain + ASGI app restart on same brain.sqlite3
        self.brain = CompanionBrain(
            self.db_path,
            user_id="local",
            settings=self.settings,
            config=BrainConfig(start_worker=False),
        )
        with patch.object(svc, "FRONTEND_DIR", frontend_override):
            app2 = svc.create_combined_asgi_app(self.brain, token)
            with TestClient(app2, base_url="http://127.0.0.1:18787") as client:
                headers = {"Authorization": f"Bearer {token}"}
                # Send remaining turns 9-12 after restart
                for req_id, u_msg, a_msg in client_sent[8:]:
                    resp = client.post(
                        "/xiaozhi/ingest",
                        headers=headers,
                        json={"request_id": req_id, "user_message": u_msg, "assistant_reply": a_msg},
                    )
                    self.assertEqual(resp.status_code, 200)
                    self.assertEqual(resp.json()["accepted_ids"], [req_id])

                # Wait for startup recovery + live ingest to finish reconciling all 12 turns
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    h = client.get("/xiaozhi/health").json()
                    if h["inbox_pending"] == 0 and h["ingest"]["inbox_processing"] == 0 and h["ingest"]["inbox_done"] == 12:
                        break
                    time.sleep(0.04)

                h_final = client.get("/xiaozhi/health").json()
                self.assertEqual(h_final["status"], "ok")
                self.assertEqual(h_final["inbox_pending"], 0)
                self.assertEqual(h_final["inbox_dead"], 0)
                self.assertEqual(h_final["ingest"]["inbox_done"], 12)

        # Reconciliation proof: every single client-sent turn is committed in turns & done in xiaozhi_inbox (0 lost!)
        committed_rows = {
            r["request_id"]: (r["status"], r["user_message"], r["assistant_reply"])
            for r in self.brain.engine.db.execute(
                "SELECT request_id, status, user_message, assistant_reply FROM turns WHERE request_id LIKE 'b1-crash-%'"
            ).fetchall()
        }
        inbox_rows = {
            r["request_id"]: r["status"]
            for r in self.brain.engine.db.execute(
                "SELECT request_id, status FROM xiaozhi_inbox WHERE request_id LIKE 'b1-crash-%'"
            ).fetchall()
        }
        self.assertEqual(len(committed_rows), 12)
        for req_id, u_msg, a_msg in client_sent:
            self.assertIn(req_id, committed_rows, f"Lost turn {req_id} across crash/restart!")
            self.assertEqual(committed_rows[req_id], ("committed", u_msg, a_msg))
            self.assertEqual(inbox_rows.get(req_id), "done")

    def test_batch3_b1_retries_dead_letter_timeout_recovery_and_health_backlog(self):
        token = "b1-dead-token-" + ("j" * 24)
        frontend_override = svc.FRONTEND_DIR if (svc.FRONTEND_DIR / "index.html").is_file() else None

        # 1. Runtime recovery of timed-out 'processing' rows (and dead-lettering when attempts > max)
        svc.ensure_xiaozhi_inbox_table(self.brain.engine)
        old_ts = time.time() - 600.0
        with self.brain.engine._transaction():
            self.brain.engine.db.execute(
                "INSERT INTO xiaozhi_inbox VALUES(?,?,?,?,?,?,?,?)",
                ("b1-stuck-ok", svc.DEFAULT_CHARACTER_ID, '{"user_message":"u","assistant_reply":"a"}', "processing", 0, "", old_ts, old_ts),
            )
            self.brain.engine.db.execute(
                "INSERT INTO xiaozhi_inbox VALUES(?,?,?,?,?,?,?,?)",
                ("b1-stuck-max", svc.DEFAULT_CHARACTER_ID, '{"user_message":"u","assistant_reply":"a"}', "processing", 2, "", old_ts, old_ts),
            )
        with patch.object(svc, "INGEST_PENDING_MAX_RETRIES", 2):
            rec = svc.recover_inbox_rows(self.brain, processing_timeout_seconds=30.0, is_startup=False)
        self.assertEqual(rec["recovered_processing"], 1)
        self.assertEqual(rec["marked_dead"], 1)
        r_ok = self.brain.engine.db.execute(
            "SELECT status, attempts, last_error FROM xiaozhi_inbox WHERE request_id='b1-stuck-ok'"
        ).fetchone()
        r_max = self.brain.engine.db.execute(
            "SELECT status, attempts, last_error FROM xiaozhi_inbox WHERE request_id='b1-stuck-max'"
        ).fetchone()
        self.assertEqual((r_ok["status"], r_ok["attempts"]), ("pending", 1))
        self.assertEqual((r_max["status"], r_max["attempts"]), ("dead", 3))
        # Clean synthetic rows before testing worker dead-lettering
        with self.brain.engine._transaction():
            self.brain.engine.db.execute("DELETE FROM xiaozhi_inbox")

        # 2. Worker increments attempts on repeated failure, redacts secrets in last_error, moves to 'dead'
        def _always_boom(*args, **kwargs):
            raise RuntimeError("upstream failed with Bearer sk-topsecret1234567890abcdef")

        with (
            patch.object(svc, "FRONTEND_DIR", frontend_override),
            patch.object(svc, "extract_and_commit_turn", side_effect=_always_boom),
            patch.object(svc, "INGEST_PENDING_MAX_RETRIES", 2),
            patch.object(svc, "INGEST_PENDING_BACKOFF_BASE", 0.03),
            patch.object(svc, "HEALTH_BACKLOG_THRESHOLD", 2),
        ):
            app = svc.create_combined_asgi_app(self.brain, token)
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                headers = {"Authorization": f"Bearer {token}"}
                r_ing = client.post(
                    "/xiaozhi/ingest",
                    headers=headers,
                    json={"request_id": "b1-always-fail", "user_message": "u", "assistant_reply": "a"},
                )
                self.assertEqual(r_ing.status_code, 200)

                deadline = time.monotonic() + 3.0
                row_dead = None
                while time.monotonic() < deadline:
                    row_dead = self.brain.engine.db.execute(
                        "SELECT status, attempts, last_error FROM xiaozhi_inbox WHERE request_id='b1-always-fail'"
                    ).fetchone()
                    if row_dead and row_dead["status"] == "dead":
                        break
                    time.sleep(0.03)

                self.assertIsNotNone(row_dead)
                self.assertEqual(row_dead["status"], "dead")
                self.assertEqual(row_dead["attempts"], 3)  # > max_retries(2)
                self.assertNotIn("sk-topsecret1234567890abcdef", row_dead["last_error"])

                h_dead = client.get("/xiaozhi/health")
                self.assertEqual(h_dead.status_code, 200)
                hb = h_dead.json()
                self.assertEqual(hb["inbox_dead"], 1)
                self.assertEqual(hb["inbox_pending"], 0)
                self.assertEqual(hb["ingest"]["inbox_dead"], 1)

                # 3. Seed 2 pending rows with future backoff to hit HEALTH_BACKLOG_THRESHOLD=2 -> 503 backlog_ok=False
                fut_ts = time.time() + 120.0
                with self.brain.engine._transaction():
                    for k in range(2):
                        self.brain.engine.db.execute(
                            "INSERT INTO xiaozhi_inbox VALUES(?,?,?,?,?,?,?,?)",
                            (f"b1-backlog-{k}", svc.DEFAULT_CHARACTER_ID, '{"user_message":"u","assistant_reply":"a"}', "pending", 1, "wait", fut_ts, fut_ts),
                        )
                h_high = client.get("/xiaozhi/health")
                self.assertEqual(h_high.status_code, 503)
                hh = h_high.json()
                self.assertFalse(hh["backlog_ok"])
                self.assertEqual(hh["inbox_pending"], 2)
                self.assertEqual(hh["status"], "unhealthy")
                # Clean synthetic pending rows so shutdown drain completes immediately
                with self.brain.engine._transaction():
                    self.brain.engine.db.execute("DELETE FROM xiaozhi_inbox WHERE request_id LIKE 'b1-backlog-%'")

    def test_batch3_b1_rollback_inbox_script(self):
        import importlib.util
        import sqlite3

        script_path = ANIMA_ROOT / "scripts" / "rollback_inbox.py"
        self.assertTrue(
            script_path.is_file(),
            f"Missing rollback script at {script_path} (ensure anima-brain/scripts is mounted to /app/scripts:ro)",
        )
        spec = importlib.util.spec_from_file_location("rollback_inbox", str(script_path))
        rb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rb)

        # Seed a committed turn plus inbox rows across all 4 statuses
        svc.extract_and_commit_turn(
            self.brain, svc.DEFAULT_CHARACTER_ID, "rb-existing-turn", "已有问题", "已有回复"
        )
        svc.ensure_xiaozhi_inbox_table(self.brain.engine)
        now = time.time()
        with self.brain.engine._transaction():
            self.brain.engine.db.execute(
                "INSERT INTO xiaozhi_inbox VALUES(?,?,?,?,?,?,?,?)",
                ("rb-pending-1", svc.DEFAULT_CHARACTER_ID, json.dumps({"user_message": "待处理1", "assistant_reply": "回复1"}, ensure_ascii=False), "pending", 0, "", now - 30, now - 30),
            )
            self.brain.engine.db.execute(
                "INSERT INTO xiaozhi_inbox VALUES(?,?,?,?,?,?,?,?)",
                ("rb-proc-1", svc.DEFAULT_CHARACTER_ID, json.dumps({"user_message": "处理中1", "assistant_reply": "回复2"}, ensure_ascii=False), "processing", 1, "", now - 20, now - 20),
            )
            self.brain.engine.db.execute(
                "INSERT INTO xiaozhi_inbox VALUES(?,?,?,?,?,?,?,?)",
                ("rb-dead-1", svc.DEFAULT_CHARACTER_ID, json.dumps({"user_message": "死信1", "assistant_reply": "回复3"}, ensure_ascii=False), "dead", 13, "boom", now - 10, now - 10),
            )
            self.brain.engine.db.execute(
                "INSERT INTO xiaozhi_inbox VALUES(?,?,?,?,?,?,?,?)",
                ("rb-done-1", svc.DEFAULT_CHARACTER_ID, json.dumps({"user_message": "已完成1", "assistant_reply": "回复4"}, ensure_ascii=False), "done", 0, "", now - 5, now - 5),
            )

        # 1. Dry-run leaves table untouched and reports counts accurately
        dry = rb.rollback_inbox(self.db_path, dry_run=True)
        self.assertTrue(dry["ok"])
        self.assertTrue(dry["dry_run"])
        self.assertFalse(dry["dropped"])
        self.assertEqual(dry["counts"], {"pending": 1, "processing": 1, "dead": 1, "done": 1})
        self.assertEqual(dry["exported"], 3)

        # 2. Drain callback failing on one row refuses to drop table and still exports uncompleted rows safely
        exp_partial = Path(self._tmp.name) / "partial_export.json"

        def _partial_drain(row):
            return row["request_id"] == "rb-pending-1"

        res_partial = rb.rollback_inbox(
            self.db_path, export_path=exp_partial, drain_fn=_partial_drain, drop_table=True
        )
        self.assertFalse(res_partial["ok"])
        self.assertEqual(res_partial["drained"], 1)
        self.assertEqual(res_partial["drain_failed"], 1)
        self.assertFalse(res_partial["dropped"])
        self.assertTrue(exp_partial.is_file())
        exported_partial = json.loads(exp_partial.read_text(encoding="utf-8"))
        self.assertEqual(
            {r["request_id"] for r in exported_partial["rows"]},
            {"rb-proc-1", "rb-dead-1"},
        )

        # 3. Full rollback export + drop via CLI main()
        exp_final = Path(self._tmp.name) / "final_rollback_export.json"
        rc = rb.main(["--db", str(self.db_path), "--export", str(exp_final)])
        self.assertEqual(rc, 0)
        self.assertTrue(exp_final.is_file())
        doc = json.loads(exp_final.read_text(encoding="utf-8"))
        self.assertEqual(doc["exported_count"], 2)
        self.assertEqual({r["request_id"] for r in doc["rows"]}, {"rb-proc-1", "rb-dead-1"})

        # Verify xiaozhi_inbox is gone, turns table intact, and DB passes integrity_check
        conn = sqlite3.connect(str(self.db_path))
        try:
            self.assertIsNone(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='xiaozhi_inbox'").fetchone()
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM turns WHERE request_id='rb-existing-turn'").fetchone()[0],
                1,
            )
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        finally:
            conn.close()

        # 4. Idempotent re-run after table is already dropped succeeds cleanly
        again = rb.rollback_inbox(self.db_path)
        self.assertTrue(again["ok"])
        self.assertFalse(again["table_existed"])
        self.assertFalse(again["dropped"])

        # 5. CLI --drain mode commits pending row into turns, exports dead row, and drops xiaozhi_inbox
        drain_db = Path(self._tmp.name) / "drain_cli.sqlite3"
        drain_brain = CompanionBrain(
            drain_db,
            user_id="local",
            settings=self.settings,
            config=BrainConfig(start_worker=False),
        )
        try:
            svc.ensure_default_character(drain_brain)
            svc.ensure_xiaozhi_inbox_table(drain_brain.engine)
            with drain_brain.engine._transaction():
                drain_brain.engine.db.execute(
                    "INSERT INTO xiaozhi_inbox VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "rb-cli-drain-ok",
                        svc.DEFAULT_CHARACTER_ID,
                        json.dumps({"user_message": "CLI排空问题", "assistant_reply": "CLI排空回复"}, ensure_ascii=False),
                        "pending",
                        0,
                        "",
                        now - 10,
                        now - 10,
                    ),
                )
                drain_brain.engine.db.execute(
                    "INSERT INTO xiaozhi_inbox VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "rb-cli-drain-dead",
                        svc.DEFAULT_CHARACTER_ID,
                        json.dumps({"user_message": "死信保留", "assistant_reply": "死信回复"}, ensure_ascii=False),
                        "dead",
                        13,
                        "err",
                        now - 5,
                        now - 5,
                    ),
                )
        finally:
            drain_brain.close()

        exp_drain = Path(self._tmp.name) / "drain_cli_export.json"
        rc_drain = rb.main(["--db", str(drain_db), "--drain", "--export", str(exp_drain)])
        self.assertEqual(rc_drain, 0)
        doc_drain = json.loads(exp_drain.read_text(encoding="utf-8"))
        self.assertEqual([r["request_id"] for r in doc_drain["rows"]], ["rb-cli-drain-dead"])
        dconn = sqlite3.connect(str(drain_db))
        try:
            self.assertIsNone(
                dconn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='xiaozhi_inbox'").fetchone()
            )
            t_dr = dconn.execute(
                "SELECT status, assistant_reply FROM turns WHERE request_id='rb-cli-drain-ok'"
            ).fetchone()
            self.assertIsNotNone(t_dr)
            self.assertEqual(t_dr[0], "committed")
            self.assertEqual(t_dr[1], "CLI排空回复")
        finally:
            dconn.close()

    def test_batch4_h2_h3_h4_nonroot_build_version_and_baked_ui_assets(self):
        """H-2/H-3/H-4: non-root runtime, non-editable wheel with built UI assets, and /health build_version."""
        import os
        import companion_engine
        from starlette.testclient import TestClient

        # 1. Built UI artifacts exist inside installed companion_engine package (not editable /app/src)
        pkg_dir = Path(companion_engine.__file__).resolve().parent
        static_dir = pkg_dir / "static"
        self.assertIn("site-packages", str(pkg_dir), f"Expected non-editable install in site-packages, got {pkg_dir}")
        for asset in (
            "voice-player.html",
            "voice-player-notices.txt",
            "memory-panel.html",
            "provider-settings.html",
            "local-settings.html",
        ):
            fp = static_dir / asset
            self.assertTrue(fp.is_file(), f"Missing baked static UI asset: {fp}")
            self.assertGreater(fp.stat().st_size, 0, f"Empty static UI asset: {fp}")

        # 2. Container runs as non-root USER app (uid != 0)
        if hasattr(os, "getuid"):
            self.assertNotEqual(os.getuid(), 0, "H-2: anima-brain container must run as non-root USER app")

        # 3. Both /health and /xiaozhi/health expose build_version without auth
        orig_bv = svc.BUILD_VERSION
        svc.BUILD_VERSION = "abc1234"
        try:
            app = svc.create_combined_asgi_app(self.brain, "tok-h3-test-" + ("x" * 24))
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                r1 = client.get("/xiaozhi/health")
                self.assertEqual(r1.status_code, 200)
                self.assertEqual(r1.json().get("build_version"), "abc1234")

                r2 = client.get("/health")
                self.assertEqual(r2.status_code, 200)
                self.assertEqual(r2.json().get("build_version"), "abc1234")
                self.assertTrue(r2.json().get("db_ok"))
        finally:
            svc.BUILD_VERSION = orig_bv

    def test_batch4_h3_h4_dev_override_static_html_fallback(self):
        """H-3/H-4: Dev bind-mount (without gitignored built HTMLs in src/) falls back cleanly to ANIMA_BUILT_STATIC_DIR."""
        import os
        import tempfile
        from unittest.mock import patch
        from companion_engine import memory_ui, plugin_support, settings_ui

        with tempfile.TemporaryDirectory() as td:
            built_dir = Path(td) / "built_static"
            built_dir.mkdir()
            (built_dir / "voice-player.html").write_text("<html>voice-built</html>", encoding="utf-8")
            (built_dir / "memory-panel.html").write_text("<html>memory-built</html>", encoding="utf-8")
            (built_dir / "provider-settings.html").write_text("<html>settings-built</html>", encoding="utf-8")

            empty_pkg = Path(td) / "empty_pkg"
            (empty_pkg / "static").mkdir(parents=True)

            # 1. plugin_support.player_html()
            with (
                patch.dict(os.environ, {"ANIMA_BUILT_STATIC_DIR": str(built_dir)}),
                patch.object(plugin_support, "ROOT", empty_pkg),
            ):
                self.assertEqual(plugin_support.player_html(), "<html>voice-built</html>")

            # 2. memory_ui resource reader
            class _FakeServer:
                def __init__(self):
                    self.resources = {}
                def resource(self, uri, **_kw):
                    def _dec(fn):
                        self.resources[uri] = fn
                        return fn
                    return _dec
                def tool(self, **_kw):
                    return lambda fn: fn

            class _FakeRuntime:
                engine = None

            srv = _FakeServer()
            memory_ui.register_memory_ui(srv, _FakeRuntime())
            settings_ui.register_settings_ui(srv, _FakeRuntime())
            with (
                patch.dict(os.environ, {"ANIMA_BUILT_STATIC_DIR": str(built_dir)}),
                patch.object(memory_ui, "__file__", str(empty_pkg / "memory_ui.py")),
                patch.object(settings_ui, "__file__", str(empty_pkg / "settings_ui.py")),
            ):
                self.assertEqual(srv.resources[memory_ui.MEMORY_URI](), "<html>memory-built</html>")
                self.assertEqual(srv.resources[settings_ui.SETTINGS_URI](), "<html>settings-built</html>")

    def test_p1_7_ingest_worker_survives_claim_error_and_renews_heartbeat_lease(self):
        """P1-7: ingest_worker survives transient _claim_next_inbox_row DB errors and renews updated_at lease during slow extraction."""
        import sqlite3
        import threading
        from unittest.mock import patch

        tok = "tok-p17-test-" + ("z" * 24)
        hdrs = {"Authorization": f"Bearer {tok}", "Host": "127.0.0.1:18787"}

        # 1. Transient OperationalError in _claim_next_inbox_row must NOT kill ingest_worker
        orig_claim = svc._claim_next_inbox_row
        claim_calls = {"n": 0}

        def _flaky_claim(*args, **kwargs):
            claim_calls["n"] += 1
            if claim_calls["n"] <= 2:
                raise sqlite3.OperationalError("database is locked (simulated)")
            return orig_claim(*args, **kwargs)

        committed_ids: list[str] = []

        def _fast_extract(brain, cid, req_id, u, a, **kwargs):
            committed_ids.append(req_id)
            return {"committed": True, "request_id": req_id}

        app = svc.create_combined_asgi_app(self.brain, tok)
        with (
            patch.object(svc, "_claim_next_inbox_row", side_effect=_flaky_claim),
            patch.object(svc, "extract_and_commit_turn", side_effect=_fast_extract),
        ):
            with TestClient(app, base_url="http://127.0.0.1:18787") as client:
                r_in = client.post(
                    "/xiaozhi/ingest",
                    headers=hdrs,
                    json={
                        "character_id": "xiaozhi",
                        "turns": [{"request_id": "req-p17-claim-ok", "user_message": "u", "assistant_reply": "a"}],
                    },
                )
                self.assertEqual(r_in.status_code, 200)
                deadline = time.monotonic() + 5.0
                while "req-p17-claim-ok" not in committed_ids and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIn("req-p17-claim-ok", committed_ids)
                self.assertGreaterEqual(claim_calls["n"], 3)

        # 2. Heartbeat lease keeps slow in-flight extraction alive past INBOX_PROCESSING_TIMEOUT_SECONDS
        orig_timeout = svc.INBOX_PROCESSING_TIMEOUT_SECONDS
        svc.INBOX_PROCESSING_TIMEOUT_SECONDS = 0.25
        extraction_started = threading.Event()
        allow_finish = threading.Event()
        recovered_midway: dict = {}

        def _slow_extract(brain, cid, req_id, u, a, **kwargs):
            extraction_started.set()
            allow_finish.wait(timeout=4.0)
            return {"committed": True, "request_id": req_id}

        try:
            app2 = svc.create_combined_asgi_app(self.brain, tok)
            with patch.object(svc, "extract_and_commit_turn", side_effect=_slow_extract):
                with TestClient(app2, base_url="http://127.0.0.1:18787") as client:
                    r_in2 = client.post(
                        "/xiaozhi/ingest",
                        headers=hdrs,
                        json={
                            "character_id": "xiaozhi",
                            "turns": [{"request_id": "req-p17-hb", "user_message": "slow", "assistant_reply": "reply"}],
                        },
                    )
                    self.assertEqual(r_in2.status_code, 200)
                    self.assertTrue(extraction_started.wait(timeout=3.0))
                    # Sleep LONGER than INBOX_PROCESSING_TIMEOUT_SECONDS (0.25s) while extraction is active!
                    time.sleep(0.45)
                    # review_healer's recover_inbox_rows MUST NOT reclaim or mark dead the leased row!
                    recovered_midway.update(svc.recover_inbox_rows(self.brain, is_startup=False))
                    self.assertEqual(recovered_midway["recovered_processing"], 0)
                    self.assertEqual(recovered_midway["marked_dead"], 0)
                    allow_finish.set()
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        with self.brain.engine._lock:
                            row = self.brain.engine.db.execute(
                                "SELECT status FROM xiaozhi_inbox WHERE request_id='req-p17-hb'"
                            ).fetchone()
                        if row and row["status"] == "done":
                            break
                        time.sleep(0.03)
                    self.assertIsNotNone(row)
                    self.assertEqual(row["status"], "done")

            # 3. _complete_inbox_row marks a committed turn 'done' even if review_healer had flipped it to 'dead'
            #    while payload stayed identical, and transient DB error on _complete_inbox_row never marks committed turn dead.
            svc.enqueue_inbox_turn(
                self.brain,
                character_id="xiaozhi",
                request_id="req-p17-reclaimed",
                user_message="u",
                assistant_reply="a",
            )
            with self.brain.engine._lock:
                with self.brain.engine._transaction():
                    cur_p = self.brain.engine.db.execute(
                        "SELECT payload FROM xiaozhi_inbox WHERE request_id='req-p17-reclaimed'"
                    ).fetchone()["payload"]
                    self.brain.engine.db.execute(
                        "UPDATE xiaozhi_inbox SET status='dead', attempts=4 WHERE request_id='req-p17-reclaimed'"
                    )
            self.assertTrue(svc._complete_inbox_row(self.brain, "req-p17-reclaimed", cur_p))
            with self.brain.engine._lock:
                st_rec = self.brain.engine.db.execute(
                    "SELECT status FROM xiaozhi_inbox WHERE request_id='req-p17-reclaimed'"
                ).fetchone()["status"]
            self.assertEqual(st_rec, "done")

            # 4. If review_healer reclaims a processing row to 'pending' and an upgraded continuation arrives
            #    before the worker completes, both enqueue_inbox_turn and _complete_inbox_row must reset
            #    attempts=0 / updated_at=0.0 so the fuller continuation is immediately claimable without backoff.
            svc.enqueue_inbox_turn(
                self.brain,
                character_id="xiaozhi",
                request_id="req-p17-reclaimed-upgrade",
                user_message="你好",
                assistant_reply="前半句",
                now=100.0,
            )
            claimed_row, _ = svc._claim_next_inbox_row(self.brain, now=101.0)
            self.assertIsNotNone(claimed_row)
            svc.recover_inbox_rows(self.brain, processing_timeout_seconds=10.0, is_startup=False, now=300.0)
            svc.enqueue_inbox_turn(
                self.brain,
                character_id="xiaozhi",
                request_id="req-p17-reclaimed-upgrade",
                user_message="你好",
                assistant_reply="前半句加上完整后半句",
                now=301.0,
            )
            self.assertFalse(
                svc._complete_inbox_row(
                    self.brain, "req-p17-reclaimed-upgrade", claimed_row["payload"], now=302.0
                )
            )
            with self.brain.engine._lock:
                upg_row = self.brain.engine.db.execute(
                    "SELECT status, attempts, last_error, updated_at FROM xiaozhi_inbox "
                    "WHERE request_id='req-p17-reclaimed-upgrade'"
                ).fetchone()
            self.assertEqual(upg_row["status"], "pending")
            self.assertEqual(upg_row["attempts"], 0)
            self.assertEqual(upg_row["last_error"], "")
            self.assertEqual(upg_row["updated_at"], 0.0)
            claimed_upg, wait_upg = svc._claim_next_inbox_row(self.brain, now=302.1)
            self.assertIsNotNone(claimed_upg)
            self.assertEqual(claimed_upg["request_id"], "req-p17-reclaimed-upgrade")
            self.assertEqual(wait_upg, 0.0)
        finally:
            svc.INBOX_PROCESSING_TIMEOUT_SECONDS = orig_timeout


if __name__ == "__main__":
    unittest.main(verbosity=2)

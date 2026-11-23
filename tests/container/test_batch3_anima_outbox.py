"""Batch 3 unit tests for B-5 (AnimaOutbox dead-lettering, TTL cleanup & replay)
and C-6 (per-device isolated recall cache + 300ms short-budget sync with cached fallback).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import httpx

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

import anima_memory_provider as amp
import mei_emotion_router


class _FakeResp:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class TestBatch3AnimaOutboxAndRecall(unittest.TestCase):
    def setUp(self):
        self._orig_global_cfg = dict(amp._GLOBAL_CONFIG)

    def tearDown(self):
        amp._GLOBAL_CONFIG.clear()
        amp._GLOBAL_CONFIG.update(self._orig_global_cfg)
        with amp._outbox_lock:
            if amp._default_outbox is not None and amp._default_outbox.db_path != self._orig_global_cfg.get("outbox_db"):
                amp._default_outbox.close()
                amp._default_outbox = None

    def test_same_device_reconnect_pins_recall_mood_and_late_writes_to_the_original_archive(self):
        from patches import memory_hooks

        async def run(td):
            outbox = amp.AnimaOutbox(os.path.join(td, "outbox.sqlite"), auto_start=False)
            delayed = asyncio.Event()
            started = asyncio.Event()

            class Client:
                async def post(self, url, *, headers, json):
                    cid = json["character_id"]
                    if json["query"] == "delayed":
                        started.set()
                        await delayed.wait()
                    if json["query"] == "wrong":
                        cid = "daily-persona"
                    return _FakeResp(200, {
                        "character_id": cid, "context_text": cid,
                        "mood": "happy" if cid == "daily-persona" else "gentle",
                    })

            with (
                patch.object(amp.memory_archives.xz_common, "STATE_DIR", td),
                patch.object(amp, "get_default_outbox", return_value=outbox),
                patch.object(amp, "_read_token", return_value="token"),
            ):
                device = "test-device"
                shared = amp.MemoryProvider({"character_id": "daily-persona", "recall_budget_seconds": 10})
                await shared._client.aclose()
                shared._client = Client()

                def connect():
                    conn = SimpleNamespace(device_id=device, memory=shared, dialogue=SimpleNamespace(dialogue=[]))
                    memory_hooks._bind_connection_memory(conn)
                    return conn

                daily = connect()
                delayed_recall = asyncio.create_task(daily.memory.query_memory("delayed"))
                await started.wait()
                amp.memory_archives.select_archive(device, "tavern")
                tavern = connect()
                cid = tavern._anima_memory_target.character_id
                self.assertNotEqual(daily._anima_memory_target.character_id, cid)
                self.assertEqual(await tavern.memory.query_memory("wrong"), "")
                self.assertIsNone(tavern.memory._cached_context)
                self.assertEqual(await tavern.memory.query_memory("ok"), cid)
                delayed.set()
                self.assertEqual(await delayed_recall, "daily-persona")
                self.assertEqual(tavern.memory._cached_context, cid)
                self.assertEqual(mei_emotion_router.get_anima_mood(tavern._anima_memory_target.mood_key), "gentle")
                self.assertEqual(mei_emotion_router.get_anima_mood(device), "happy")

                messages = [SimpleNamespace(role="user", content="真实提问", uniq_id="late-close"),
                            SimpleNamespace(role="assistant", content="回答")]
                await daily.memory.save_memory(messages)
                # An old snapshot and a later extended duplicate must never be moved to the new archive.
                messages[1].content += "更多"
                await tavern.memory.save_memory(messages)
                messages[0].uniq_id = "new-story"
                tavern.dialogue.dialogue = messages
                self.assertTrue(memory_hooks._flush_dialogue_latest_pair(tavern.dialogue))
                with outbox._lock:
                    rows = outbox._ensure_db().execute("SELECT request_id, character_id FROM outbox_turns").fetchall()
                self.assertEqual(dict(rows), {"xz-late-close": "daily-persona", "xz-new-story": cid})
                outbox.close()
                mei_emotion_router.clear_anima_mood()

        with tempfile.TemporaryDirectory() as td:
            asyncio.run(run(td))

    def test_handoff_is_quoted_only_for_first_user_and_acknowledged_after_durable_write(self):
        from patches import memory_hooks as hooks

        story = (("约定使用蓝色门票" + "雪" * 800, "我记住了。号码 13800138000。"),)
        target = amp.MemoryTarget("dev", "tavern", "story", "a" * 64, story, 5)
        user = SimpleNamespace(role="user", content="继续", uniq_id="first")
        dialogue = SimpleNamespace(_anima_memory_target=target, _device_id="dev", dialogue=[user])
        original = [{"role": "system", "content": "人设"}, {"role": "user", "content": "继续"}]
        with patch.object(amp.memory_archives, "acknowledge_handoff") as ack:
            result = hooks._with_tavern_handoff(dialogue, original, user)
            self.assertEqual(len(original), 2)
            self.assertEqual(result[-1], original[-1])
            self.assertIn(story[0][0], result[-2]["content"])
            self.assertNotIn("13800138000", result[-2]["content"])
            self.assertEqual(result[-2]["role"], "assistant")
            self.assertEqual(hooks._with_tavern_handoff(dialogue, original, object()), original)
            dialogue.dialogue.append(SimpleNamespace(role="assistant", content="走吧"))
            with patch.object(amp, "push_turns_async", return_value=False):
                self.assertFalse(hooks._flush_dialogue_latest_pair(dialogue))
                ack.assert_not_called()
            with patch.object(amp, "push_turns_async", return_value=True) as send:
                self.assertTrue(hooks._flush_dialogue_latest_pair(dialogue))
                self.assertEqual(len(send.call_args.args[0]), 1)
                turn = send.call_args.args[0][0]
                self.assertEqual((turn["user_message"], turn["assistant_reply"]), ("继续", "走吧"))
            ack.assert_called_once_with("dev", "a" * 64, story, 5)
            self.assertEqual(hooks._with_tavern_handoff(dialogue, original, user), original)

    def test_damaged_selection_disables_all_writes_even_on_close(self):
        from patches import memory_hooks as hooks

        with tempfile.TemporaryDirectory() as td, patch.object(amp.memory_archives.xz_common, "STATE_DIR", td):
            with open(os.path.join(td, "memory_archives.sqlite"), "wb") as handle:
                handle.write(b"This is not a SQLite database.")
            conn = SimpleNamespace(device_id="dev", memory=None, dialogue=SimpleNamespace(dialogue=[]))
            with self.assertRaises(amp.sqlite3.DatabaseError):
                hooks._bind_connection_memory(conn)
            self.assertIsNone(conn.memory)
            self.assertTrue(conn.dialogue._anima_memory_unavailable)
            with patch.object(amp, "push_turns_async") as send:
                self.assertFalse(hooks._flush_dialogue_latest_pair(conn.dialogue))
                send.assert_not_called()

    def test_tavern_rejects_unscoped_provider_without_touching_daily_connections(self):
        from patches import memory_hooks as hooks

        with tempfile.TemporaryDirectory() as td, patch.object(amp.memory_archives.xz_common, "STATE_DIR", td):
            provider = object()
            daily = SimpleNamespace(device_id="dev", memory=provider, dialogue=SimpleNamespace(dialogue=[]))
            hooks._bind_connection_memory(daily)
            self.assertIs(daily.memory, provider)
            amp.memory_archives.select_archive("dev", "tavern")
            story = SimpleNamespace(device_id="dev", memory=provider, dialogue=SimpleNamespace(dialogue=[]))
            with self.assertRaisesRegex(hooks.UnsupportedArchiveProvider, "Anima Brain"):
                hooks._bind_connection_memory(story)
            self.assertIsNone(story.memory)
            self.assertTrue(story.dialogue._anima_memory_unavailable)
            hooks._bind_connection_memory(daily)
            self.assertIs(daily.memory, provider)

    def test_unconfirmed_success_stays_pending(self):
        for body in (b"", b"broken", b"{}", b'{"rejected_ids":[]}', b'{"queued":true}'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as td:
                transport = httpx.MockTransport(lambda req: httpx.Response(200, content=body))
                box = amp.AnimaOutbox(
                    os.path.join(td, "ack.sqlite"), token_getter=lambda: "fixture",
                    client_factory=lambda: httpx.Client(transport=transport), auto_start=False,
                )
                try:
                    box.enqueue([{"request_id": "test-ack", "user_message": "u", "assistant_reply": "a"}])
                    result = box.drain_once()
                    self.assertEqual((result["sent"], result["remaining"]), (0, 1))
                finally:
                    box.close()

    def test_b5_http_4xx_marks_dead_immediately_and_stops_retrying(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "outbox_4xx.sqlite")
            calls = []

            def _handler(request: httpx.Request) -> httpx.Response:
                calls.append(json.loads(request.content.decode("utf-8")))
                return httpx.Response(
                    400,
                    text="invalid payload Bearer sk-secret1234567890abcdef",
                )

            transport = httpx.MockTransport(_handler)
            ob = amp.AnimaOutbox(
                db_path,
                service_url_getter=lambda: "http://anima-brain:18787",
                character_id_getter=lambda: "xiaozhi",
                token_getter=lambda: "tok-123456789012345678901234",
                client_factory=lambda: httpx.Client(transport=transport, trust_env=False),
                base_backoff=0.05,
                auto_start=False,
            )
            try:
                self.assertEqual(ob.max_retries, 8)
                self.assertEqual(ob.dead_ttl_days, 14.0)

                ob.enqueue([{"request_id": "xz-4xx-1", "user_message": "u", "assistant_reply": "a"}])
                res = ob.drain_once(force_due=True)
                self.assertEqual(res["sent"], 0)
                self.assertEqual(res["failed"], 1)
                self.assertEqual(res["dead"], 1)
                self.assertEqual(res["remaining"], 0)

                st = ob.stats()
                self.assertEqual(st["pending"], 0)
                self.assertEqual(st["dead"], 1)
                self.assertEqual(st["corrupt"], 0)
                self.assertEqual(st["sent"], 0)
                self.assertNotIn("sk-secret1234567890abcdef", st["last_error"])

                # Subsequent drain MUST NOT retry dead row (0 additional HTTP calls)
                res2 = ob.drain_once(force_due=True)
                self.assertEqual(res2["sent"], 0)
                self.assertEqual(res2["failed"], 0)
                self.assertEqual(len(calls), 1)

                # Identical re-enqueue must NOT resurrect dead row automatically
                added = ob.enqueue([{"request_id": "xz-4xx-1", "user_message": "u", "assistant_reply": "a"}])
                self.assertEqual(added, 0)
                self.assertEqual(ob.stats()["dead"], 1)
                self.assertEqual(ob.stats()["pending"], 0)
            finally:
                ob.close()

    def test_b5_retries_exceeding_max_moves_to_dead_with_exponential_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "outbox_maxretry.sqlite")
            calls = {"n": 0}

            def _handler(request: httpx.Request) -> httpx.Response:
                calls["n"] += 1
                return httpx.Response(503, json={"error": "service_unavailable"})

            transport = httpx.MockTransport(_handler)
            ob = amp.AnimaOutbox(
                db_path,
                service_url_getter=lambda: "http://anima-brain:18787",
                character_id_getter=lambda: "xiaozhi",
                token_getter=lambda: "tok-123456789012345678901234",
                client_factory=lambda: httpx.Client(transport=transport, trust_env=False),
                base_backoff=0.05,
                max_backoff=0.4,
                max_retries=3,
                auto_start=False,
            )
            try:
                ob.enqueue([{"request_id": "xz-retry-dead", "user_message": "u", "assistant_reply": "a"}])
                # Attempts 1, 2, 3 stay 'pending' with growing exponential backoff
                prev_next_retry = 0.0
                for expected_attempt in (1, 2, 3):
                    r = ob.drain_once(force_due=True)
                    self.assertEqual(r["dead"], 0)
                    self.assertEqual(r["remaining"], 1)
                    row = ob._ensure_db().execute(
                        "SELECT status, attempts, next_retry_at FROM outbox_turns WHERE request_id='xz-retry-dead'"
                    ).fetchone()
                    self.assertEqual(row["status"], "pending")
                    self.assertEqual(row["attempts"], expected_attempt)
                    self.assertGreater(float(row["next_retry_at"]), prev_next_retry)
                    prev_next_retry = float(row["next_retry_at"])

                # Attempt 4 (> max_retries=3) marks row 'dead' and stops retrying
                r_dead = ob.drain_once(force_due=True)
                self.assertEqual(r_dead["dead"], 1)
                self.assertEqual(r_dead["remaining"], 0)
                st = ob.stats()
                self.assertEqual(st["dead"], 1)
                self.assertEqual(st["pending"], 0)

                # Confirm no further HTTP calls happen after becoming dead
                ob.drain_once(force_due=True)
                self.assertEqual(calls["n"], 4)
            finally:
                ob.close()

    def test_b5_ttl_cleanup_prunes_expired_dead_and_corrupt_only(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "outbox_ttl.sqlite")
            ob = amp.AnimaOutbox(db_path, dead_ttl_days=14.0, auto_start=False)
            try:
                db = ob._ensure_db()
                now = 1800000000.0
                old_ts = now - (15.0 * 86400.0)
                fresh_ts = now - (5.0 * 86400.0)
                seed = [
                    ("dead-old", "dead", old_ts),
                    ("dead-fresh", "dead", fresh_ts),
                    ("corrupt-old", "corrupt", old_ts),
                    ("corrupt-fresh", "corrupt", fresh_ts),
                    ("pending-old", "pending", old_ts),
                    ("sent-old", "sent", old_ts),
                ]
                for rid, st, ts in seed:
                    db.execute(
                        "INSERT INTO outbox_turns("
                        "request_id, character_id, payload_json, payload_hash, status, "
                        "attempts, next_retry_at, last_error, created_at, updated_at, sent_at"
                        ") VALUES(?, 'xiaozhi', '{}', ?, ?, 1, 0, 'err', ?, ?, NULL)",
                        (rid, f"h-{rid}", st, ts, ts),
                    )
                db.commit()

                before = ob.stats()
                self.assertEqual(before["dead"], 2)
                self.assertEqual(before["corrupt"], 2)
                self.assertEqual(before["pending"], 1)
                self.assertEqual(before["sent"], 1)

                deleted = ob.cleanup_expired_rows(now=now)
                self.assertEqual(deleted, 2)

                after = ob.stats()
                self.assertEqual(after["dead"], 1)
                self.assertEqual(after["corrupt"], 1)
                self.assertEqual(after["pending"], 1)
                self.assertEqual(after["sent"], 1)

                remaining_ids = {
                    r["request_id"]
                    for r in db.execute("SELECT request_id FROM outbox_turns").fetchall()
                }
                self.assertEqual(remaining_ids, {"dead-fresh", "corrupt-fresh", "pending-old", "sent-old"})
            finally:
                ob.close()

    def test_b5_replay_dead_turns_and_module_level_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "outbox_replay.sqlite")
            state = {"fail_4xx": True}

            def _handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                if state["fail_4xx"]:
                    return httpx.Response(422, text="unprocessable")
                ids = [t["request_id"] for t in body["turns"]]
                return httpx.Response(200, json={"queued": len(ids), "accepted_ids": ids, "rejected_ids": []})

            transport = httpx.MockTransport(_handler)
            ob = amp.AnimaOutbox(
                db_path,
                service_url_getter=lambda: "http://anima-brain:18787",
                character_id_getter=lambda: "xiaozhi",
                token_getter=lambda: "tok-123456789012345678901234",
                client_factory=lambda: httpx.Client(transport=transport, trust_env=False),
                auto_start=False,
            )
            try:
                ob.enqueue(
                    [
                        {"request_id": "xz-d1", "user_message": "u1", "assistant_reply": "a1"},
                        {"request_id": "xz-d2", "user_message": "u2", "assistant_reply": "a2"},
                        {"request_id": "xz-d3", "user_message": "u3", "assistant_reply": "a3"},
                    ]
                )
                r1 = ob.drain_once(force_due=True)
                self.assertEqual(r1["dead"], 3)
                self.assertEqual(ob.stats()["dead"], 3)

                # 1. Targeted replay by request_ids
                rep_one = ob.replay_dead_turns(request_ids=["xz-d2"])
                self.assertTrue(rep_one["ok"])
                self.assertEqual(rep_one["replayed"], 1)
                self.assertEqual(rep_one["stats"]["pending"], 1)
                self.assertEqual(rep_one["stats"]["dead"], 2)

                # 2. Limited replay (oldest 1 remaining dead)
                rep_lim = ob.replay_dead_turns(limit=1)
                self.assertEqual(rep_lim["replayed"], 1)
                self.assertEqual(rep_lim["stats"]["pending"], 2)
                self.assertEqual(rep_lim["stats"]["dead"], 1)

                # 3. Replay all remaining dead rows and verify delivery succeeds once upstream recovers
                rep_all = ob.replay_dead_turns()
                self.assertEqual(rep_all["replayed"], 1)
                self.assertEqual(rep_all["stats"]["pending"], 3)
                self.assertEqual(rep_all["stats"]["dead"], 0)

                state["fail_4xx"] = False
                r_ok = ob.drain_once(force_due=True)
                self.assertEqual(r_ok["sent"], 3)
                self.assertEqual(r_ok["remaining"], 0)
                self.assertEqual(ob.stats()["sent"], 3)
                self.assertEqual(ob.stats()["dead"], 0)
            finally:
                ob.close()

            # 4. Module-level get_outbox_stats() & replay_dead_outbox_turns() contract for 18080 console
            mod_db = os.path.join(td, "mod_outbox.sqlite")
            with amp._outbox_lock:
                if amp._default_outbox is not None:
                    amp._default_outbox.close()
                    amp._default_outbox = None
                amp._GLOBAL_CONFIG["outbox_db"] = mod_db
                amp._default_outbox = amp.AnimaOutbox(
                    mod_db,
                    service_url_getter=lambda: "http://anima-brain:18787",
                    character_id_getter=lambda: "xiaozhi",
                    token_getter=lambda: "tok-123456789012345678901234",
                    client_factory=lambda: httpx.Client(
                        transport=httpx.MockTransport(lambda req: httpx.Response(400, text="bad")),
                        trust_env=False,
                    ),
                    auto_start=False,
                )
            amp._default_outbox.enqueue([{"request_id": "xz-mod-dead", "user_message": "u", "assistant_reply": "a"}])
            amp._default_outbox.drain_once(force_due=True)

            st_mod = amp.get_outbox_stats()
            for k in ("pending", "dead", "corrupt", "sent", "max_retries", "dead_ttl_days", "last_error", "worker_alive"):
                self.assertIn(k, st_mod)
            self.assertEqual(st_mod["dead"], 1)
            self.assertEqual(st_mod["pending"], 0)

            # Avoid starting background HTTP worker during replay unit test by patching start()
            with patch.object(amp._default_outbox, "start", lambda: None):
                rep_mod = amp.replay_dead_outbox_turns()
            self.assertTrue(rep_mod["ok"])
            self.assertEqual(rep_mod["replayed"], 1)
            self.assertEqual(rep_mod["stats"]["dead"], 0)
            self.assertEqual(rep_mod["stats"]["pending"], 1)

    def test_c6_per_device_recall_cache_isolation_and_mood_device_id(self):
        async def _run():
            with tempfile.TemporaryDirectory() as td:
                tok_file = os.path.join(td, "tok.txt")
                outbox_db = os.path.join(td, "outbox.sqlite")
                with open(tok_file, "w", encoding="utf-8") as f:
                    f.write("test-c6-token-1234567890123456")

                prov = amp.MemoryProvider(
                    {
                        "service_url": "http://127.0.0.1:18787",
                        "token_file": tok_file,
                        "outbox_db": outbox_db,
                        "recall_budget_seconds": 0.4,
                        "recall_warm_budget_seconds": 0.15,
                        "recall_cb_threshold": 5,
                    }
                )
                mode = {"slow": False, "reply_for": {}}
                mood_calls: list[tuple[str, str | None]] = []

                class _Client:
                    async def post(self, url, **kwargs):
                        q = (kwargs.get("json") or {}).get("query", "")
                        if mode["slow"]:
                            await asyncio.sleep(0.5)
                        return _FakeResp(
                            200,
                            {
                                "context_text": mode["reply_for"].get(q, f"ctx:{q}"),
                                "mood": f"stage=close, last_emotion={'happy' if 'A' in q else 'gentle'}",
                                "facts_count": 1,
                            },
                        )

                await prov._client.aclose()
                prov._client = _Client()

                def _capture_mood(mood_summary: str, device_id=None):
                    mood_calls.append((mood_summary, device_id))

                with patch.object(mei_emotion_router, "set_anima_mood", side_effect=_capture_mood):
                    # Device A recalls and caches its own context
                    prov.role_id = "AA:BB:CC:01"
                    c_a1 = await prov.query_memory("qA1")
                    self.assertEqual(c_a1, "ctx:qA1")
                    self.assertEqual(mood_calls[-1][1], "aa:bb:cc:01")

                    # Device B hits timeout on cold cache -> MUST get "" (NEVER leak Device A's cache!)
                    mode["slow"] = True
                    prov.role_id = "AA:BB:CC:02"
                    c_b_miss = await prov.query_memory("qB_cold_timeout")
                    self.assertEqual(c_b_miss, "")

                    # Device B succeeds when healthy -> gets its own cache entry
                    mode["slow"] = False
                    c_b1 = await prov.query_memory("qB1")
                    self.assertEqual(c_b1, "ctx:qB1")
                    self.assertEqual(mood_calls[-1][1], "aa:bb:cc:02")
                    self.assertEqual(prov.get_recall_stats()["cached_devices"], 2)

                    # Now server slows down: Device A (lowercase alias) falls back to Device A cache,
                    # Device B falls back to Device B cache (zero cross-device leakage!)
                    mode["slow"] = True
                    c_a_fb = await prov.query_memory("qA2", device_id="aa:bb:cc:01")
                    c_b_fb = await prov.query_memory("qB2", device_id="AA:BB:CC:02")
                    self.assertEqual(c_a_fb, "ctx:qA1")
                    self.assertEqual(c_b_fb, "ctx:qB1")

                for t in list(prov._bg_refresh_tasks.values()):
                    t.cancel()
                await asyncio.gather(*list(prov._bg_refresh_tasks.values()), return_exceptions=True)

        asyncio.run(_run())

    def test_c6_warm_cache_short_budget_sync_avoids_one_turn_late_and_falls_back_on_timeout(self):
        async def _run():
            with tempfile.TemporaryDirectory() as td:
                tok_file = os.path.join(td, "tok.txt")
                outbox_db = os.path.join(td, "outbox.sqlite")
                with open(tok_file, "w", encoding="utf-8") as f:
                    f.write("test-c6-warm-token-1234567890123456")

                # Default MemoryProvider uses 300ms warm-cache sync budget (NOT 0ms stale-only!)
                prov_def = amp.MemoryProvider(
                    {"service_url": "http://127.0.0.1:18787", "token_file": tok_file, "outbox_db": outbox_db}
                )
                self.assertAlmostEqual(prov_def._warm_recall_budget_seconds, 0.3, places=3)
                self.assertAlmostEqual(prov_def.get_recall_stats()["warm_budget_seconds"], 0.3, places=3)
                await prov_def._client.aclose()

                prov = amp.MemoryProvider(
                    {
                        "service_url": "http://127.0.0.1:18787",
                        "token_file": tok_file,
                        "outbox_db": outbox_db,
                        "recall_budget_seconds": 0.6,
                        "recall_warm_budget_seconds": 0.2,
                        "recall_bg_budget_seconds": 0.6,
                        "recall_cb_threshold": 5,
                    }
                )
                prov.role_id = "dev-warm-1"
                call_idx = {"n": 0}

                class _DynamicClient:
                    async def post(self, url, **kwargs):
                        call_idx["n"] += 1
                        q = (kwargs.get("json") or {}).get("query", "")
                        if q == "turn-1":
                            await asyncio.sleep(0.01)
                            return _FakeResp(200, {"context_text": "mem-turn-1", "mood": "calm"})
                        if q == "turn-2-fast":
                            await asyncio.sleep(0.02)
                            return _FakeResp(200, {"context_text": "mem-turn-2-fresh", "mood": "happy"})
                        if q == "turn-3-slow":
                            await asyncio.sleep(0.45)
                            return _FakeResp(200, {"context_text": "mem-turn-3-late", "mood": "gentle"})
                        if q == "turn-4-error":
                            return _FakeResp(500, {})
                        if q == "turn-5-fresh":
                            await asyncio.sleep(0.01)
                            return _FakeResp(200, {"context_text": "mem-turn-5-newest", "mood": "playful"})
                        return _FakeResp(200, {"context_text": f"mem-{q}", "mood": "calm"})

                await prov._client.aclose()
                prov._client = _DynamicClient()

                # Turn 1 (cold): caches mem-turn-1
                r1 = await prov.query_memory("turn-1")
                self.assertEqual(r1, "mem-turn-1")

                # Turn 2 (warm, fast 20ms < 200ms budget): MUST return FRESH mem-turn-2-fresh (NOT 1 turn late!)
                t0 = time.monotonic()
                r2 = await prov.query_memory("turn-2-fast")
                self.assertEqual(r2, "mem-turn-2-fresh")
                self.assertLess(time.monotonic() - t0, 0.15)
                self.assertEqual(prov.get_recall_stats()["warm_sync_hits"], 1)

                # Turn 3 (warm, slow 450ms > 200ms budget): aborts near 0.2s and falls back to cached mem-turn-2-fresh!
                t0 = time.monotonic()
                r3 = await prov.query_memory("turn-3-slow")
                elapsed_3 = time.monotonic() - t0
                self.assertEqual(r3, "mem-turn-2-fresh")
                self.assertGreaterEqual(elapsed_3, 0.15)
                self.assertLess(elapsed_3, 0.40, f"Warm sync should timeout near 0.2s, got {elapsed_3:.3f}s")
                self.assertEqual(prov.get_recall_stats()["warm_fallback_hits"], 1)
                self.assertEqual(prov.get_recall_stats()["timeouts"], 1)

                # Turn 5 arrives quickly while Turn 3's slow bg refresh is still in flight ->
                # Turn 5 succeeds with mem-turn-5-newest, and Turn 3's stale bg refresh MUST NOT overwrite it!
                r5 = await prov.query_memory("turn-5-fresh")
                self.assertEqual(r5, "mem-turn-5-newest")
                bg_task = prov._bg_refresh_task
                if bg_task is not None:
                    await bg_task
                self.assertEqual(prov._cached_context, "mem-turn-5-newest")

                # Turn 4 (warm, HTTP 500 error): gracefully falls back to newest cached value
                r4 = await prov.query_memory("turn-4-error")
                self.assertEqual(r4, "mem-turn-5-newest")
                self.assertGreaterEqual(prov.get_recall_stats()["errors"], 1)

        asyncio.run(_run())

    def test_p0_5_shared_memory_provider_per_connection_device_isolation(self):
        """P0-5: Shared MemoryProvider singleton wrapped via ConnectionHandler._initialize_memory isolates concurrent devices."""
        import threading
        from types import SimpleNamespace
        import boot
        from core.connection import ConnectionHandler

        boot.install_all(start_background_workers=False, run_contract_self_check=False)

        async def _run():
            with tempfile.TemporaryDirectory() as td:
                tok_file = os.path.join(td, "tok.txt")
                outbox_db = os.path.join(td, "outbox.sqlite")
                with open(tok_file, "w", encoding="utf-8") as f:
                    f.write("test-p05-token-1234567890123456")

                shared_singleton = amp.MemoryProvider(
                    {
                        "service_url": "http://127.0.0.1:18787",
                        "token_file": tok_file,
                        "outbox_db": outbox_db,
                        "recall_budget_seconds": 0.4,
                        "recall_warm_budget_seconds": 0.12,
                        "recall_cb_threshold": 5,
                    }
                )
                slow_flag = {"slow": False}
                mood_calls: list[tuple[str, str | None]] = []
                pushed_batches: list[list[dict]] = []

                class _Client:
                    async def post(self, url, **kwargs):
                        q = (kwargs.get("json") or {}).get("query", "")
                        if slow_flag["slow"]:
                            await asyncio.sleep(0.4)
                        return _FakeResp(200, {"context_text": f"mem:{q}", "mood": "happy"})

                await shared_singleton._client.aclose()
                shared_singleton._client = _Client()

                def _make_conn(dev_id: str):
                    c = ConnectionHandler.__new__(ConnectionHandler)
                    c.device_id = dev_id
                    c.memory = shared_singleton
                    c.llm = SimpleNamespace()
                    c.read_config_from_api = False
                    c.config = {
                        "selected_module": {"Memory": "anima_brain"},
                        "Memory": {"anima_brain": {"type": "anima_brain"}},
                    }
                    c.logger = SimpleNamespace(bind=lambda **kw: SimpleNamespace(info=lambda *a, **k: None))
                    c._initialize_memory()
                    return c

                # Simulate real upstream: Conn A connects, then Conn B connects sharing the SAME singleton!
                conn_a = _make_conn("AA:BB:CC:01")
                conn_b = _make_conn("AA:BB:CC:02")

                # Positional init_memory(role_id, llm) MUST NOT raise TypeError (multiple values for 'role_id')
                dummy_llm = object()
                conn_a.memory.init_memory("AA:BB:CC:01", dummy_llm)
                self.assertEqual(conn_a.memory.role_id, "AA:BB:CC:01")
                self.assertIs(shared_singleton.llm, dummy_llm)

                # Even though Conn B connected AFTER Conn A, Conn A's proxy MUST keep device A!
                with (
                    patch.object(
                        mei_emotion_router,
                        "set_anima_mood",
                        side_effect=lambda m, device_id=None: mood_calls.append((m, device_id)),
                    ),
                    patch.object(amp, "push_turns_async", side_effect=lambda pairs: pushed_batches.append(list(pairs))),
                ):
                    # Upstream chat() calls query_memory(query) WITHOUT passing device_id!
                    res_a1 = await conn_a.memory.query_memory("queryA1")
                    self.assertEqual(res_a1, "mem:queryA1")
                    self.assertEqual(mood_calls[-1][1], "aa:bb:cc:01")

                    res_b1 = await conn_b.memory.query_memory("queryB1")
                    self.assertEqual(res_b1, "mem:queryB1")
                    self.assertEqual(mood_calls[-1][1], "aa:bb:cc:02")

                    # Under timeout, Conn A MUST fall back to A's cache (NEVER B's!) even with zero device_id arg
                    slow_flag["slow"] = True
                    fb_a = await conn_a.memory.query_memory("queryA2")
                    fb_b = await conn_b.memory.query_memory("queryB2")
                    self.assertEqual(fb_a, "mem:queryA1")
                    self.assertEqual(fb_b, "mem:queryB1")

                    # Simulate upstream save_memory_task running in a brand-new daemon thread (no ContextVar!)
                    msgs_a = [
                        SimpleNamespace(role="user", content="我是A设备", _occurred_at=1700000001.0),
                        SimpleNamespace(role="assistant", content="你好A", _occurred_at=1700000002.0),
                    ]
                    save_err: list[Exception] = []

                    def _save_in_new_thread():
                        loop = asyncio.new_event_loop()
                        try:
                            loop.run_until_complete(conn_a.memory.save_memory(msgs_a, "sess-a"))
                        except Exception as exc:
                            save_err.append(exc)
                        finally:
                            loop.close()

                    th = threading.Thread(target=_save_in_new_thread)
                    th.start()
                    th.join(timeout=5.0)
                    self.assertEqual(save_err, [])
                    self.assertTrue(pushed_batches)
                    self.assertEqual(pushed_batches[-1][0]["device_id"], "AA:BB:CC:01")

                    # Also verify ContextVar isolates raw singleton calls directly
                    tok = amp.set_current_device_id("AA:BB:CC:01")
                    try:
                        self.assertEqual(shared_singleton._resolve_device_key(), "aa:bb:cc:01")
                    finally:
                        amp.reset_current_device_id(tok)

                for t in list(shared_singleton._bg_refresh_tasks.values()):
                    t.cancel()
                await asyncio.gather(*list(shared_singleton._bg_refresh_tasks.values()), return_exceptions=True)

        asyncio.run(_run())

    def test_p1_10_outbox_retries_401_408_429_and_payload_hash_protects_upgraded_row(self):
        """P1-10: Outbox retries transient 401/408/429 with backoff, and payload_hash protects concurrent upgrades."""
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "outbox_p110.sqlite")
            status_seq = iter([401, 408, 429, 200])

            def _handler(request: httpx.Request) -> httpx.Response:
                code = next(status_seq)
                if code == 200:
                    body = json.loads(request.content.decode("utf-8"))
                    ids = [t["request_id"] for t in body["turns"]]
                    return httpx.Response(200, json={"accepted_ids": ids})
                return httpx.Response(code, text=f"transient {code}")

            ob = amp.AnimaOutbox(
                db_path,
                service_url_getter=lambda: "http://anima-brain:18787",
                character_id_getter=lambda: "xiaozhi",
                token_getter=lambda: "tok-123456789012345678901234",
                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_handler), trust_env=False),
                base_backoff=0.01,
                auto_start=False,
            )
            try:
                ob.enqueue([{"request_id": "xz-p110-retry", "user_message": "u", "assistant_reply": "a"}])
                for expected_code in (401, 408, 429):
                    r = ob.drain_once(force_due=True)
                    self.assertEqual(r["dead"], 0, f"HTTP {expected_code} must NOT immediately mark dead")
                    self.assertEqual(r["remaining"], 1, f"HTTP {expected_code} must stay pending for retry")
                r_ok = ob.drain_once(force_due=True)
                self.assertEqual(r_ok["sent"], 1)
                self.assertEqual(r_ok["remaining"], 0)
            finally:
                ob.close()

            # Concurrent upgrade during in-flight 400 / 503 MUST NOT mark upgraded row dead or bump attempts
            db_path2 = os.path.join(td, "outbox_p110_race.sqlite")
            race_code = {"code": 400}

            def _race_handler(request: httpx.Request) -> httpx.Response:
                # While old payload is in-flight, enqueue() upgrades the SAME request_id with a valid continuation reply!
                upgraded_n = ob2.enqueue(
                    [
                        {
                            "request_id": "xz-p110-up",
                            "user_message": "u",
                            "assistant_reply": "short — upgraded longer reply after tool",
                        }
                    ]
                )
                self.assertEqual(upgraded_n, 1)
                return httpx.Response(race_code["code"], text="bad old payload")

            ob2 = amp.AnimaOutbox(
                db_path2,
                service_url_getter=lambda: "http://anima-brain:18787",
                character_id_getter=lambda: "xiaozhi",
                token_getter=lambda: "tok-123456789012345678901234",
                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_race_handler), trust_env=False),
                base_backoff=5.0,
                auto_start=False,
            )
            try:
                ob2.enqueue([{"request_id": "xz-p110-up", "user_message": "u", "assistant_reply": "short"}])
                res_400 = ob2.drain_once(force_due=True)
                self.assertEqual(res_400["dead"], 0, "Stale 400 must NOT kill newly upgraded payload")
                st = ob2.stats()
                self.assertEqual(st["dead"], 0)
                self.assertEqual(st["pending"], 1)

                # Verify same protection for 503 backoff
                race_code["code"] = 503

                def _race_handler_503(request: httpx.Request) -> httpx.Response:
                    upgraded_n2 = ob2.enqueue(
                        [
                            {
                                "request_id": "xz-p110-up",
                                "user_message": "u",
                                "assistant_reply": "short — upgraded longer reply after tool v3",
                            }
                        ]
                    )
                    self.assertEqual(upgraded_n2, 1)
                    return httpx.Response(503, text="temp unavailable")

                ob2._client_factory = lambda: httpx.Client(
                    transport=httpx.MockTransport(_race_handler_503), trust_env=False
                )
                ob2.drain_once(force_due=True)
                with ob2._lock:
                    row = ob2._ensure_db().execute(
                        "SELECT status, attempts, next_retry_at FROM outbox_turns WHERE request_id='xz-p110-up'"
                    ).fetchone()
                self.assertEqual(row["status"], "pending")
                self.assertEqual(row["attempts"], 0, "Stale 503 must NOT bump attempts on upgraded row")
                self.assertEqual(row["next_retry_at"], 0.0, "Stale 503 must NOT delay newly upgraded row")

                # _quarantine_corrupt_rows with stale payload_hash MUST NOT quarantine an upgraded valid row
                ob2._quarantine_corrupt_rows([("xz-p110-up", "stale-corrupt-hash")])
                with ob2._lock:
                    row_after_q = ob2._ensure_db().execute(
                        "SELECT status FROM outbox_turns WHERE request_id='xz-p110-up'"
                    ).fetchone()
                self.assertEqual(row_after_q["status"], "pending")
            finally:
                ob2.close()

    def test_p2_uniq_id_none_stale_mood_guard_and_warm_timeout_no_circuit_trip(self):
        """4.1 #4b: uniq_id=None never produces 'xz-None'; stale recall gen skips mood write; 300ms warm timeout doesn't trip global CB."""
        from types import SimpleNamespace
        import anima_memory_provider as amp
        import mei_emotion_router as mer

        # 1. uniq_id=None MUST fall back to generated unique ID, NEVER literal 'xz-None'
        m_user = SimpleNamespace(role="user", content="你好呀", uniq_id=None, is_temporary=False)
        m_asst = SimpleNamespace(role="assistant", content="你好！", is_temporary=False)
        pairs = amp.extract_clean_pairs([m_user, m_asst], device_id="dev-null-uid")
        self.assertEqual(len(pairs), 1)
        self.assertNotEqual(pairs[0]["request_id"], "xz-None")
        self.assertNotIn("None", pairs[0]["request_id"])
        self.assertTrue(pairs[0]["request_id"].startswith("xz-"))

        # 2. Stale recall generation returning late must NOT overwrite newer device Anima mood
        prov = amp.MemoryProvider({"role_id": "dev-mood-gen"}, summary_memory=None)
        target = prov.memory_target("dev-mood-gen")
        prov._recall_gen[target] = 5
        mood_writes = []

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"context_text": "旧记忆", "mood": "angry_stale", "facts_count": 1, "open_loops_count": 0}

        class _FakeAsyncCli:
            async def post(self, *a, **kw):
                return _FakeResp()

        async def _run_mood_and_warm():
            with (
                patch.object(prov, "_get_async_client", return_value=_FakeAsyncCli()),
                patch.object(mer, "set_anima_mood", side_effect=lambda m, **kw: mood_writes.append((m, kw))),
            ):
                # Stale gen=2 < current gen=5 -> mood MUST NOT be updated
                await prov._fetch_recall_once("q_old", "tok", target, gen=2)
                self.assertEqual(mood_writes, [], "Stale recall gen must NOT mutate device Anima mood")
                # Fresh gen=5 >= current gen=5 -> mood IS updated
                await prov._fetch_recall_once("q_new", "tok", target, gen=5)
                self.assertEqual(len(mood_writes), 1)
                self.assertEqual(mood_writes[0][0], "angry_stale")

            # 3. Repeated 300ms warm-budget timeouts must fall back to cache WITHOUT tripping global 30s circuit breaker
            prov_w = amp.MemoryProvider(
                {
                    "role_id": "dev-warm",
                    "recall_warm_budget_seconds": 0.05,
                    "recall_budget_seconds": 1.0,
                    "recall_cb_threshold": 2,
                    "recall_cb_cooldown_seconds": 30.0,
                },
                summary_memory=None,
            )
            prov_w._recall_cache[prov_w.memory_target("dev-warm")] = ("缓存记忆内容", time.monotonic())

            async def _slow_fetch(*a, **kw):
                await asyncio.sleep(0.25)
                return "慢返回"

            with (
                patch.object(amp, "_read_token", return_value="tok-warm"),
                patch.object(prov_w, "_fetch_recall_once", side_effect=_slow_fetch),
                patch.object(prov_w, "_schedule_bg_refresh") as m_bg,
            ):
                for _ in range(4):
                    ctx = await prov_w.query_memory("hello", device_id="dev-warm")
                    self.assertEqual(ctx, "缓存记忆内容")
                st = prov_w.get_recall_stats("dev-warm")
                self.assertFalse(st["circuit_open"], "300ms warm budget timeouts must NOT trip global 30s circuit breaker")
                self.assertEqual(st["consecutive_failures"], 0)
                self.assertEqual(st["warm_fallback_hits"], 4)
                self.assertEqual(m_bg.call_count, 4)

        asyncio.run(_run_mood_and_warm())


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Unit & HTTP tests for Phase 1 ③ Anima open-loop dedup/expiry & Inner Life."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from starlette.testclient import TestClient

ANIMA_ROOT = Path(__file__).resolve().parent.parent
if str(ANIMA_ROOT) not in sys.path:
    sys.path.insert(0, str(ANIMA_ROOT))
SRC_DIR = ANIMA_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from companion_engine import BrainConfig, CommitRequest, CompanionBrain, Settings  # noqa: E402
from companion_engine.automation import MemoryAutomation  # noqa: E402
from companion_engine.models import MemoryDetails, MemoryDraft  # noqa: E402

import proactive_state  # noqa: E402
import xiaozhi_anima_service as svc  # noqa: E402


class TestProactiveStateAndInnerLife(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.db_path = self.tmp_dir / "brain.sqlite3"
        self.tz = ZoneInfo("Asia/Shanghai")
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
            persona="你是雷电芽衣，温柔体贴、成熟沉稳。",
            memory_policy="automatic",
            relationship_enabled=True,
            romance_enabled=True,
            timezone="Asia/Shanghai",
        )
        svc.ensure_xiaozhi_turn_meta_table(self.brain.engine)
        svc.ensure_xiaozhi_inbox_table(self.brain.engine)
        proactive_state.ensure_proactive_tables(self.brain.engine)

    def tearDown(self):
        self.brain.close()
        self._tmp.cleanup()

    def _add_open_loop(
        self,
        req_id: str,
        key: str,
        text: str,
        *,
        event_date: date | None = None,
        updated_at: float | None = None,
        importance: int = 7,
    ) -> str:
        eng = self.brain.engine
        ctx = eng.prepare_turn(svc.DEFAULT_CHARACTER_ID, text, request_id=req_id)
        eng.commit_turn(
            svc.DEFAULT_CHARACTER_ID,
            ctx.turn_id,
            CommitRequest(
                assistant_reply="好的，我记下来了。",
                memories=[
                    MemoryDraft(
                        key=key,
                        text=text,
                        category="event",
                        source_quote=text[: min(len(text), 40)],
                        details=MemoryDetails(
                            subject="user",
                            temporal_kind="event",
                            event_date=event_date,
                            importance=importance,
                            loop_status="open",
                            followup="ask",
                        ),
                    )
                ],
            ),
        )
        mems = [m for m in eng.memories(svc.DEFAULT_CHARACTER_ID) if m.key == key]
        self.assertTrue(mems)
        mem = mems[0]
        if updated_at is not None:
            with eng._transaction():
                eng.db.execute(
                    "UPDATE memories SET updated_at=? WHERE id=?",
                    (float(updated_at), mem.id),
                )
                eng.db.execute(
                    "UPDATE fact_lifecycle "
                    "SET data=json_set(data, '$.observed_at', ?, '$.last_reinforced_at', ?) "
                    "WHERE memory_id=?",
                    (float(updated_at), float(updated_at), mem.id),
                )
                eng.db.execute(
                    "UPDATE turns SET created_at=? WHERE id=?",
                    (float(updated_at), ctx.turn_id),
                )
        return mem.id

    def test_open_loop_expiry_dedup_snooze_and_recall_filtering(self):
        now_dt = datetime.now(self.tz).replace(hour=15, minute=0, second=0, microsecond=0)
        now_ts = now_dt.timestamp()
        today = now_dt.date()

        # 1. Expired by event_date (> 7 days past)
        exp_event_id = self._add_open_loop(
            "ol-1",
            "expired_exam",
            "准备资格考试",
            event_date=today - timedelta(days=10),
            updated_at=now_ts - 11 * 86400,
        )
        # 2. Expired dateless loop (> 14 days untouched)
        exp_stale_id = self._add_open_loop(
            "ol-2",
            "stale_errand",
            "打算去修一下阳台灯泡",
            event_date=None,
            updated_at=now_ts - 16 * 86400,
        )
        # 3. Future event (> 1 day away): not expired, not yet due
        future_id = self._add_open_loop(
            "ol-3",
            "future_trip",
            "下个月去云南旅行",
            event_date=today + timedelta(days=15),
            updated_at=now_ts - 2 * 86400,
        )
        # 4. Due event (yesterday) containing sensitive phone number that MUST be redacted
        due_id = self._add_open_loop(
            "ol-4",
            "due_interview",
            "昨天下午去参加技术面试，HR电话 13812345678",
            event_date=today - timedelta(days=1),
            updated_at=now_ts - 2 * 86400,
            importance=9,
        )
        # 5. Due dateless loop (created 10h ago)
        due_dateless_id = self._add_open_loop(
            "ol-5",
            "due_dentist",
            "今天想预约牙科复查",
            event_date=None,
            updated_at=now_ts - 10 * 3600,
            importance=6,
        )

        ctx = proactive_state.get_proactive_context(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            weather_hint="小雨，20℃",
            now=now_ts,
        )
        self.assertTrue(ctx["ok"])
        self.assertEqual(ctx["expired_open_loops_count"], 2)
        due_ids = [lp["memory_id"] for lp in ctx["due_open_loops"]]
        self.assertIn(due_id, due_ids)
        self.assertIn(due_dateless_id, due_ids)
        self.assertNotIn(exp_event_id, due_ids)
        self.assertNotIn(exp_stale_id, due_ids)
        self.assertNotIn(future_id, due_ids)

        # Verify sensitive phone number was redacted in proactive due_open_loops
        due_item = next(lp for lp in ctx["due_open_loops"] if lp["memory_id"] == due_id)
        self.assertNotIn("13812345678", due_item["text"])
        self.assertIn("[已拦截隐私信息:手机号]", due_item["text"])

        # Verify build_fast_recall_context filters out expired open loops from 【未完待续事项】
        # and defaults include_inner_life=False (zero extra inner-life prompt overhead when disabled)
        orig_time = svc.time.time
        svc.time.time = lambda: now_ts
        try:
            recall_default = svc.build_fast_recall_context(
                self.brain, svc.DEFAULT_CHARACTER_ID, "最近怎么样"
            )
            recall = svc.build_fast_recall_context(
                self.brain, svc.DEFAULT_CHARACTER_ID, "最近怎么样", include_inner_life=True
            )
        finally:
            svc.time.time = orig_time
        self.assertNotIn("角色当下生活气息背景", recall_default["context_text"])
        # 3 active non-expired open loops (2 expired excluded)
        self.assertEqual(recall["open_loops_count"], 3)
        open_loop_section = recall["context_text"].split("【未完待续事项", 1)[1].split("【", 1)[0]
        self.assertIn("技术面试", open_loop_section)
        self.assertIn("牙科复查", open_loop_section)
        self.assertNotIn("准备资格考试", open_loop_section)
        self.assertNotIn("阳台灯泡", open_loop_section)
        # Inner life slice should be gently injected when explicitly enabled
        self.assertIn("角色当下生活气息背景", recall["context_text"])

        # Ack nudged once -> idempotent on same event_id, excluded from future due queue!
        ack1 = proactive_state.ack_proactive_event(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            open_loop_id=due_id,
            action="nudged",
            event_id="evt-001",
            now=now_ts,
        )
        self.assertEqual(ack1["nudge_count"], 1)
        self.assertFalse(ack1["idempotent_replay"])

        ack1_dup = proactive_state.ack_proactive_event(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            open_loop_id=due_id,
            action="nudged",
            event_id="evt-001",
            now=now_ts,
        )
        self.assertEqual(ack1_dup["nudge_count"], 1)
        self.assertTrue(ack1_dup["idempotent_replay"])

        ctx2 = proactive_state.get_proactive_context(
            self.brain, svc.DEFAULT_CHARACTER_ID, now=now_ts + 60
        )
        self.assertNotIn(due_id, [lp["memory_id"] for lp in ctx2["due_open_loops"]])

        # Snooze due_dateless_id for 12h -> hidden at +1h, returns at +13h
        proactive_state.ack_proactive_event(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            open_loop_id=due_dateless_id,
            action="snooze",
            snooze_hours=12.0,
            now=now_ts,
        )
        ctx_snz = proactive_state.get_proactive_context(
            self.brain, svc.DEFAULT_CHARACTER_ID, now=now_ts + 3600
        )
        self.assertNotIn(due_dateless_id, [lp["memory_id"] for lp in ctx_snz["due_open_loops"]])
        ctx_after = proactive_state.get_proactive_context(
            self.brain, svc.DEFAULT_CHARACTER_ID, now=now_ts + 13 * 3600
        )
        self.assertIn(due_dateless_id, [lp["memory_id"] for lp in ctx_after["due_open_loops"]])

        # Dismiss permanently removes from BOTH due queue and passive recall
        proactive_state.ack_proactive_event(
            self.brain,
            svc.DEFAULT_CHARACTER_ID,
            open_loop_id=due_dateless_id,
            action="dismiss",
            now=now_ts + 14 * 3600,
        )
        ctx_dis = proactive_state.get_proactive_context(
            self.brain, svc.DEFAULT_CHARACTER_ID, now=now_ts + 15 * 3600
        )
        self.assertNotIn(due_dateless_id, [lp["memory_id"] for lp in ctx_dis["due_open_loops"]])

    def test_inner_life_deduplication_and_weather_adaptation(self):
        day1 = datetime(2026, 6, 15, 10, 0, tzinfo=self.tz).timestamp()
        day2 = datetime(2026, 6, 16, 10, 0, tzinfo=self.tz).timestamp()
        day3 = datetime(2026, 6, 17, 10, 0, tzinfo=self.tz).timestamp()

        s1 = proactive_state.get_or_create_inner_life(
            self.brain, svc.DEFAULT_CHARACTER_ID, weather_hint="小雨", now=day1
        )
        self.assertEqual(s1["slot"], "morning")
        self.assertIn("雨", s1["text"])

        # Calling again on same day/slot returns persisted slice without mutation
        s1_same = proactive_state.get_or_create_inner_life(
            self.brain, svc.DEFAULT_CHARACTER_ID, now=day1 + 600
        )
        self.assertEqual(s1_same["text"], s1["text"])
        self.assertFalse(s1_same["refreshed"])

        # Next day avoids repeating day 1's slice
        s2 = proactive_state.get_or_create_inner_life(
            self.brain, svc.DEFAULT_CHARACTER_ID, now=day2
        )
        self.assertNotEqual(s2["text"], s1["text"])

        # Day 3 avoids repeating day 1 & day 2
        s3 = proactive_state.get_or_create_inner_life(
            self.brain, svc.DEFAULT_CHARACTER_ID, now=day3
        )
        self.assertNotIn(s3["text"], {s1["text"], s2["text"]})

        # Force refresh always picks a distinct slice even across repeated refreshes (> pool size)
        prev_txt = s3["text"]
        for step in range(1, 9):
            ref_i = proactive_state.refresh_inner_life(
                self.brain, svc.DEFAULT_CHARACTER_ID, now=day3 + step * 10
            )
            self.assertTrue(ref_i["refreshed"])
            self.assertNotEqual(ref_i["text"], prev_txt)
            prev_txt = ref_i["text"]

    def test_http_proactive_routes_auth_and_validation(self):
        token = "test-anima-proactive-token-9876543210"
        app = svc.create_combined_asgi_app(self.brain, token)
        headers = {"Authorization": f"Bearer {token}"}

        with TestClient(app) as client:
            # 1. Unauthorized requests must be rejected with 401
            self.assertEqual(client.post("/xiaozhi/proactive/context", json={}).status_code, 401)
            self.assertEqual(client.post("/xiaozhi/proactive/ack", json={}).status_code, 401)
            self.assertEqual(client.post("/xiaozhi/inner-life/refresh", json={}).status_code, 401)

            # 2. Authorized context fetch creates today's inner_life and returns schema
            r_ctx = client.post(
                "/xiaozhi/proactive/context",
                headers=headers,
                json={"character_id": svc.DEFAULT_CHARACTER_ID, "weather_hint": "晴，24℃"},
            )
            self.assertEqual(r_ctx.status_code, 200)
            data = r_ctx.json()
            self.assertTrue(data["ok"])
            self.assertIn("inner_life", data)
            self.assertTrue(data["inner_life"].get("text"))

            # 3. Invalid ack action or non-finite/non-positive snooze_hours returns HTTP 400
            r_bad_ack = client.post(
                "/xiaozhi/proactive/ack",
                headers=headers,
                json={"open_loop_id": "m1", "action": "delete_everything"},
            )
            self.assertEqual(r_bad_ack.status_code, 400)

            for bad_snz in ("nan", "inf", -5, 0, True):
                r_bad_snz = client.post(
                    "/xiaozhi/proactive/ack",
                    headers=headers,
                    json={"open_loop_id": "m1", "action": "snooze", "snooze_hours": bad_snz},
                )
                self.assertEqual(
                    r_bad_snz.status_code,
                    400,
                    f"snooze_hours={bad_snz!r} must be rejected",
                )

            # 4. Valid ack & inner-life refresh return HTTP 200
            r_ack = client.post(
                "/xiaozhi/proactive/ack",
                headers=headers,
                json={"open_loop_id": "m1", "action": "nudged", "event_id": "e1"},
            )
            self.assertEqual(r_ack.status_code, 200)
            self.assertEqual(r_ack.json()["nudge_count"], 1)

            r_bad_slot = client.post(
                "/xiaozhi/inner-life/refresh",
                headers=headers,
                json={"slot": "midnight_party"},
            )
            self.assertEqual(r_bad_slot.status_code, 400)

            r_il = client.post(
                "/xiaozhi/inner-life/refresh",
                headers=headers,
                json={"weather_hint": "小雨"},
            )
            self.assertEqual(r_il.status_code, 200)
            self.assertTrue(r_il.json()["ok"])
            self.assertTrue(r_il.json()["inner_life"]["text"])

    def test_inner_life_llm_local_hostname_parsing(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import httpx

        self.assertTrue(proactive_state._is_local_endpoint("http://host.docker.internal:19340/v1/chat/completions"))
        self.assertTrue(proactive_state._is_local_endpoint("http://127.0.0.1:19340/v1/chat/completions"))
        self.assertTrue(proactive_state._is_local_endpoint("http://localhost:11434/v1/chat/completions"))
        self.assertTrue(proactive_state._is_local_endpoint("http://[::1]:19340/v1/chat/completions"))
        self.assertFalse(proactive_state._is_local_endpoint("https://localhost.evil.com/v1/chat/completions"))
        self.assertFalse(proactive_state._is_local_endpoint("https://evil.com/host.docker.internal/v1"))
        self.assertFalse(proactive_state._is_local_endpoint("http://localhost@evil.com/v1/chat/completions"))
        self.assertFalse(proactive_state._is_local_endpoint("file://localhost/etc/passwd"))
        self.assertFalse(proactive_state._is_local_endpoint("http://[::1"))

        captured = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "午后泡了一壶温热的乌龙茶，静静翻看手边的散文集。"}}]}

        class _FakeClient:
            def __init__(self, **kw):
                captured["client_kw"] = kw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                return _FakeResp()

        spec = self.brain.characters()[0]

        # 1. Substring-spoofed external host MUST NOT be treated as local (trust_env=True, requires api_key)
        spoof_brain_no_key = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    summary_base_url="https://localhost.evil.com/v1",
                    summary_model="m",
                    summary_key="",
                    base_url="",
                    model="",
                    api_key="",
                )
            )
        )
        self.assertIsNone(
            proactive_state._try_generate_inner_life_llm(
                spoof_brain_no_key, spec, local_date="2026-06-15", slot="afternoon", weather_hint="", recent_texts=[]
            )
        )

        spoof_brain = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    summary_base_url="https://evil.com/host.docker.internal/v1",
                    summary_model="m",
                    summary_key="sk-test1234567890",
                    base_url="",
                    model="",
                    api_key="",
                )
            )
        )
        with patch.object(httpx, "Client", _FakeClient):
            out_ext = proactive_state._try_generate_inner_life_llm(
                spoof_brain, spec, local_date="2026-06-15", slot="afternoon", weather_hint="", recent_texts=[]
            )
        self.assertIsNotNone(out_ext)
        self.assertTrue(captured["client_kw"]["trust_env"])

        # 2. Local IPv6 [::1] allowed without api_key and uses trust_env=False
        local_v6_brain = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    summary_base_url="http://[::1]:19340/v1",
                    summary_model="qwen",
                    summary_key="",
                    base_url="",
                    model="",
                    api_key="",
                )
            )
        )
        with patch.object(httpx, "Client", _FakeClient):
            out_v6 = proactive_state._try_generate_inner_life_llm(
                local_v6_brain, spec, local_date="2026-06-15", slot="afternoon", weather_hint="", recent_texts=[]
            )
        self.assertIsNotNone(out_v6)
        self.assertFalse(captured["client_kw"]["trust_env"])
        self.assertNotIn("Authorization", captured["headers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

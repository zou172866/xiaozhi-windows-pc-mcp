"""Unit tests for Phase 1 natural-language future tasks & Cron reminder engine."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from http import HTTPStatus
import io
import json
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

import future_tasks
import patch_registry
import runtime_events
import runtime_settings
from console.routes import future_tasks as ft_routes
from patches import future_task_tool


TZ_SH = timezone(timedelta(hours=8))


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


class TestPhase1FutureTasks(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._td.name, "future_tasks.sqlite3")
        self.settings_path = os.path.join(self._td.name, "runtime_settings.json")
        self.now_ts = datetime(2026, 11, 27, 10, 0, 0, tzinfo=TZ_SH).timestamp()
        self.spoken = []
        self.mobile_sent = []
        self.qq_sent = []
        self.voice_busy = False
        runtime_events.reset_runtime_events()

        self.engine = future_tasks.FutureTaskEngine(
            db_path=self.db_path,
            settings_path=self.settings_path,
            now_fn=lambda: self.now_ts,
            local_dt_fn=lambda ts: datetime.fromtimestamp(ts, tz=TZ_SH),
            tts_synthesizer=lambda text, emo, cfg: (b"RIFFWAVE", "mock-tts"),
            speaker_player=lambda wav, vol: (self.spoken.append((wav, vol)) or True, "ok"),
            busy_checker=lambda: self.voice_busy,
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            mobile_sender=lambda title, body, cat: (
                self.mobile_sent.append((title, body, cat)) or {"ok": True, "provider": "bark"}
            ),
            qq_sender=lambda title, body: (
                self.qq_sent.append((title, body)) or {"ok": True}
            ),
            weather_fetcher=lambda city, cfg: f"{city or '长沙'} 晴 18℃",
        )
        future_tasks.reset_future_task_engine(self.engine)

    def tearDown(self):
        future_tasks.reset_future_task_engine(None)
        runtime_events.reset_runtime_events()
        self._td.cleanup()

    def test_cron_parsing_next_fire_leap_year_and_min_interval_guard(self):
        base = datetime(2026, 11, 27, 10, 0, 0, tzinfo=TZ_SH)
        nxt = future_tasks.next_cron_datetime("30 8 * * *", base)
        self.assertEqual((nxt.year, nxt.month, nxt.day, nxt.hour, nxt.minute), (2026, 11, 28, 8, 30))

        # Sunday=7 equivalence (2026-11-29 is Sunday)
        nxt_sun = future_tasks.next_cron_datetime("0 9 * * 7", base)
        self.assertEqual((nxt_sun.year, nxt_sun.month, nxt_sun.day, nxt_sun.hour), (2026, 11, 29, 9))

        # Leap day jumps directly to 2028-02-29
        nxt_leap = future_tasks.next_cron_datetime("0 12 29 2 *", base)
        self.assertEqual((nxt_leap.year, nxt_leap.month, nxt_leap.day), (2028, 2, 29))

        # Every 5 minutes is allowed; every 1 or 2 minutes is rejected
        self.assertEqual(future_tasks.validate_cron_min_interval("*/5 * * * *", ref_dt=base), "*/5 * * * *")
        with self.assertRaises(ValueError):
            future_tasks.validate_cron_min_interval("* * * * *", ref_dt=base)
        with self.assertRaises(ValueError):
            future_tasks.validate_cron_min_interval("*/2 * * * *", ref_dt=base)
        with self.assertRaises(ValueError):
            future_tasks.validate_cron_min_interval("1,3 * * * *", ref_dt=base)

    def test_one_shot_task_persistence_delivery_and_secret_redaction(self):
        task = self.engine.create_task(
            title="喝水提醒",
            message="记得喝水，密钥 sk-1234567890abcdefghij 不要念",
            schedule_type="once",
            delay_minutes=10,
            delivery="pc_and_mobile",
            source="voice",
        )
        self.assertEqual(task["status"], "active")
        self.assertNotIn("sk-1234567890abcdefghij", task["message"])
        self.assertIn("[已脱敏密钥]", task["message"])

        # Simulate process restart using a new engine pointing at the same SQLite DB
        eng2 = future_tasks.FutureTaskEngine(
            db_path=self.db_path,
            settings_path=self.settings_path,
            now_fn=lambda: self.now_ts,
            local_dt_fn=lambda ts: datetime.fromtimestamp(ts, tz=TZ_SH),
            tts_synthesizer=lambda text, emo, cfg: (b"RIFFWAVE", "mock-tts"),
            speaker_player=lambda wav, vol: (self.spoken.append((wav, vol)) or True, "ok"),
            busy_checker=lambda: False,
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            mobile_sender=lambda title, body, cat: (
                self.mobile_sent.append((title, body, cat)) or {"ok": True, "provider": "bark"}
            ),
        )
        loaded = eng2.get_task(task["id"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["title"], "喝水提醒")

        # Not due yet
        self.assertEqual(eng2.tick(), [])

        # Advance 10 minutes -> fires and marks once task completed
        self.now_ts += 600.0
        fired = eng2.tick()
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["outcome"], "delivered_pc_and_mobile")
        self.assertEqual(len(self.spoken), 1)
        self.assertEqual(len(self.mobile_sent), 1)
        self.assertEqual(self.mobile_sent[0][2], "reminder")

        after = eng2.get_task(task["id"])
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["run_count"], 1)
        self.assertIsNone(after["next_run_at"])

    def test_restart_storm_guard_marks_stale_once_missed_and_advances_cron(self):
        t_once = self.engine.create_task(
            title="过时闹钟",
            delay_minutes=5,
            delivery="dry_run",
        )
        t_cron = self.engine.create_task(
            title="每日早安天气",
            schedule_type="cron",
            cron_expr="0 11 * * *",
            task_kind="weather_briefing",
            city="长沙",
            delivery="dry_run",
            respect_quiet_hours=False,
        )
        # Jump 2 hours forward (well beyond 300s STALE_GRACE_SECONDS for both tasks)
        self.now_ts += 7200.0
        res = self.engine.tick()
        outcomes = {r["task_id"]: r["outcome"] for r in res}
        self.assertEqual(outcomes[t_once["id"]], "missed_stale")
        self.assertEqual(outcomes[t_cron["id"]], "advanced_stale")

        self.assertEqual(self.engine.get_task(t_once["id"])["status"], "missed")
        cron_after = self.engine.get_task(t_cron["id"])
        self.assertEqual(cron_after["status"], "active")
        self.assertGreater(cron_after["next_run_at"], self.now_ts)

    def test_busy_voice_defers_then_falls_back_to_mobile(self):
        t = self.engine.create_task(title="吃药提醒", delay_minutes=1, delivery="pc_speaker")
        self.voice_busy = True
        self.now_ts += 60.0

        # Defers 3 times
        for _ in range(future_tasks.MAX_BUSY_DEFERS):
            res = self.engine.tick()
            self.assertEqual(res[0]["outcome"], "deferred_busy")
            self.now_ts += future_tasks.BUSY_DEFER_SECONDS + 1.0

        # 4th attempt falls back cleanly to mobile push
        res = self.engine.tick()
        self.assertEqual(res[0]["outcome"], "delivered_mobile_fallback")
        self.assertEqual(len(self.mobile_sent), 1)
        self.assertEqual(self.engine.get_task(t["id"])["status"], "completed")

    def test_weather_briefing_quiet_hours_and_qq_fallback(self):
        # Quiet hours 23:00 - 08:00
        runtime_settings.set_proactive(quiet_start="23:00", quiet_end="08:00", settings_path=self.settings_path)
        self.now_ts = datetime(2026, 11, 27, 23, 30, 0, tzinfo=TZ_SH).timestamp()
        t_q = self.engine.create_task(
            title="夜间天气",
            delay_minutes=1,
            task_kind="weather_briefing",
            city="上海",
            delivery="mobile_push",
            respect_quiet_hours=True,
        )
        self.now_ts += 65.0
        res = self.engine.tick()
        self.assertEqual(res[0]["outcome"], "skipped_quiet_hours")
        self.assertEqual(len(self.mobile_sent), 0)

        # Manual trigger bypasses quiet hours and composes weather text
        man = self.engine.trigger_now(t_q["id"], force_dry_run=True)
        self.assertTrue(man["ok"])
        self.assertIn("上海 晴 18℃", man["run"]["rendered_text"])

        # QQ delivery works when sender succeeds, and falls back to mobile_push when QQ fails
        t_qq = self.engine.create_task(title="QQ提醒", delay_minutes=1, delivery="qq_bot")
        self.now_ts += 65.0
        res_qq = self.engine.tick()
        self.assertEqual(res_qq[0]["outcome"], "delivered_qq")
        self.assertEqual(len(self.qq_sent), 1)

        self.engine._qq_sender = lambda title, body: {"ok": False, "reason": "disabled"}
        t_qq2 = self.engine.create_task(title="QQ回退提醒", delay_minutes=1, delivery="qq_bot")
        self.now_ts += 65.0
        res_qq2 = self.engine.tick()
        self.assertEqual(res_qq2[0]["outcome"], "delivered_mobile_fallback")
        self.assertEqual(len(self.mobile_sent), 1)

    def test_max_active_tasks_limit_and_llm_tool_and_console_routes(self):
        # LLM tool create / list / pause / resume / cancel
        msg_create = future_task_tool.execute_future_task_tool(
            action="create",
            title="开会提醒",
            delay_minutes=15,
            delivery="pc_and_mobile",
        )
        self.assertIn("已为你设定定时任务", msg_create)
        self.assertIn("开会提醒", future_task_tool.execute_future_task_tool(action="list"))
        self.assertIn("已暂停", future_task_tool.execute_future_task_tool(action="pause", task_id="开会"))
        self.assertIn("已恢复", future_task_tool.execute_future_task_tool(action="resume", task_id="开会"))
        self.assertIn("已取消", future_task_tool.execute_future_task_tool(action="cancel", task_id="开会"))

        # Console route create / get / trigger / delete + invalid field 400
        h_bad = _FakeHandler({"action": "create", "bogus_field": 1})
        ft_routes.handle_post_future_tasks(h_bad)
        self.assertEqual(h_bad.status, 400)

        h_create = _FakeHandler({
            "action": "create",
            "title": "面板定时提醒",
            "schedule_type": "cron",
            "cron_expr": "0 9 * * *",
            "delivery": "dry_run",
        })
        ft_routes.handle_post_future_tasks(h_create)
        self.assertEqual(h_create.status, 200)
        tid = h_create.payload["task"]["id"]

        h_get = _FakeHandler()
        ft_routes.handle_get_future_tasks(h_get)
        self.assertEqual(h_get.status, 200)
        self.assertEqual(h_get.payload["active_count"], 1)

        h_trig = _FakeHandler({"action": "trigger", "task_id": tid, "force_dry_run": True})
        ft_routes.handle_post_future_tasks(h_trig)
        self.assertEqual(h_trig.status, 200)
        self.assertEqual(h_trig.payload["run"]["outcome"], "dry_run")

        h_del = _FakeHandler({"action": "delete", "task_id": tid})
        ft_routes.handle_post_future_tasks(h_del)
        self.assertEqual(h_del.status, 200)

        # Max active tasks cap (50)
        for i in range(future_tasks.MAX_ACTIVE_TASKS):
            self.engine.create_task(title=f"任务{i}", delay_minutes=30 + i, delivery="dry_run")
        with self.assertRaises(ValueError):
            self.engine.create_task(title="超限任务", delay_minutes=99, delivery="dry_run")

    def test_patch_registration_contract_with_stub_plugins_func(self):
        # Verify patch installer cleanly registers FunctionItem + auto-intent exposure
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
            future_task_tool.install_future_task_tool_patch()
            self.assertIn("manage_future_task", reg_mod.all_function_registry)
            item = reg_mod.all_function_registry["manage_future_task"]
            resp = item.func(None, action="list")
            self.assertEqual(resp.action, "REQLLM")
            self.assertIn("没有任何", resp.result)
        finally:
            for k, old in saved.items():
                if old is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)

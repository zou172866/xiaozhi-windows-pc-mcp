"""Offline tests for Phase 3 domestic mobile push bridge, proactive delivery integration,
and 18080 Console mobile-push & Obsidian inbox routes."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT_DIR = "/opt/xiaozhi-esp32-server"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DIR = os.environ.get("XIAOZHI_APP_DIR") or (
    os.path.join(ROOT_DIR, "app")
    if os.path.isdir(os.path.join(ROOT_DIR, "app"))
    else os.path.join(_REPO_ROOT, "app")
)
for _p in (ROOT_DIR, APP_DIR):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import mobile_push
import proactive_companion
import runtime_settings
from console.routes import anima as anima_routes
from console.routes import mobile_push as mp_routes
from console.routes import proactive as proactive_routes


def _public_resolver(_host: str, _port: int):
    return ["114.114.114.114"]


class TestPhase3MobilePushAndInboxRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.settings_path = os.path.join(self.tmp_dir, "runtime_settings.json")
        self.secrets_path = os.path.join(self.tmp_dir, ".mobile_push_secrets.json")
        self.history_path = os.path.join(self.tmp_dir, "proactive_history.json")
        self._saved_env = {k: os.environ.pop(k, None) for k in mobile_push.SECRET_FIELD_ENV_MAP.values()}
        self.posted_calls = []

        def _fake_post(url, payload, headers, timeout):
            self.posted_calls.append((url, payload, headers, timeout))
            if "day.app" in url:
                return 200, {"code": 200, "message": "success"}
            if "feishu" in url:
                return 200, {"code": 0, "msg": "success"}
            if "weixin.qq.com" in url:
                return 200, {"errcode": 0, "errmsg": "ok"}
            if "pushplus.plus" in url:
                return 200, {"code": 200, "msg": "请求成功"}
            return 200, {"code": 0, "message": ""}

        self._fake_post = _fake_post
        self.now_ts = 1764043200.0  # 2025-11-25 12:00:00 local-ish
        self.now_dt = datetime(2026, 11, 25, 14, 30, 0)
        self.bridge = mobile_push.MobilePushBridge(
            settings_path=self.settings_path,
            secrets_path=self.secrets_path,
            now_fn=lambda: self.now_ts,
            now_dt_fn=lambda: self.now_dt,
            http_poster=self._fake_post,
            resolver=_public_resolver,
        )

    def tearDown(self):
        mobile_push.reset_default_bridge_for_tests(None)
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_default_off_zero_outbound_and_zero_import_side_effects(self):
        cfg = runtime_settings.get_mobile_push(self.settings_path)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["provider"], "bark")
        self.assertFalse(cfg["forward_briefing"])

        res = self.bridge.send("标题", "正文测试")
        self.assertFalse(res["ok"])
        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "disabled")
        self.assertEqual(self.posted_calls, [])

        # Zero import-time threads
        code = (
            "import sys, threading; "
            f"sys.path.insert(0, {APP_DIR!r}); "
            "before = {t.name for t in threading.enumerate()}; "
            "import mobile_push; "
            "after = {t.name for t in threading.enumerate()}; "
            "assert before == after, f'spawned: {after - before}'"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_all_five_domestic_providers_payload_and_feishu_hmac(self):
        secrets = {
            "bark_url": "https://api.day.app/MyBarkDeviceKey123",
            "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/abc12345",
            "feishu_secret": "feishu-hmac-secret",
            "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-key-123",
            "pushplus_token": "pp_token_12345678",
            "serverchan_key": "SCT123456Tabcdefg",
        }
        # 1. Bark
        u, p, _ = mobile_push.build_provider_request("bark", "芽衣", "记得喝水", secrets, resolver=_public_resolver)
        self.assertEqual(u, "https://api.day.app/MyBarkDeviceKey123")
        self.assertEqual(p["title"], "芽衣")
        self.assertEqual(p["body"], "记得喝水")

        # 2. Feishu + HMAC-SHA256 verification
        u, p, _ = mobile_push.build_provider_request(
            "feishu", "芽衣", "午后休息一下", secrets, now_ts=1700000000.0, resolver=_public_resolver
        )
        self.assertEqual(u, secrets["feishu_webhook"])
        expected_sign = base64.b64encode(
            hmac.new(b"1700000000\nfeishu-hmac-secret", b"", digestmod=hashlib.sha256).digest()
        ).decode("ascii")
        self.assertEqual(p["timestamp"], "1700000000")
        self.assertEqual(p["sign"], expected_sign)

        # 3. WeCom
        u, p, _ = mobile_push.build_provider_request("wecom", "芽衣", "任务完成了", secrets, resolver=_public_resolver)
        self.assertEqual(u, secrets["wecom_webhook"])
        self.assertEqual(p["msgtype"], "text")
        self.assertIn("任务完成了", p["text"]["content"])

        # 4. PushPlus
        u, p, _ = mobile_push.build_provider_request("pushplus", "简报", "今日3轮", secrets, resolver=_public_resolver)
        self.assertEqual(u, "https://www.pushplus.plus/send")
        self.assertEqual(p["token"], "pp_token_12345678")

        # 5. ServerChan Turbo & SC3
        u, p, _ = mobile_push.build_provider_request("serverchan", "提醒", "Turbo测试", secrets, resolver=_public_resolver)
        self.assertEqual(u, "https://sctapi.ftqq.com/SCT123456Tabcdefg.send")
        self.assertEqual(p["desp"], "Turbo测试")

        u3, _, _ = mobile_push.build_provider_request(
            "serverchan",
            "提醒",
            "SC3测试",
            {"serverchan_key": "sctp98765tAbCdEfGh"},
            resolver=_public_resolver,
        )
        self.assertEqual(u3, "https://98765.push.ft07.com/send/sctp98765tAbCdEfGh.send")

    def test_ssrf_guards_reject_http_private_ip_non443_and_wrong_hosts(self):
        with self.assertRaises(ValueError):
            mobile_push.validate_outbound_https_url("http://api.day.app/key123", resolver=_public_resolver)
        with self.assertRaises(ValueError):
            mobile_push.validate_outbound_https_url("https://127.0.0.1/key123", resolver=_public_resolver)
        with self.assertRaises(ValueError):
            mobile_push.validate_outbound_https_url("https://192.168.1.5/key123", resolver=_public_resolver)
        with self.assertRaises(ValueError):
            mobile_push.validate_outbound_https_url("https://api.day.app:8443/key123", resolver=_public_resolver)
        with self.assertRaises(ValueError):
            mobile_push.validate_outbound_https_url(
                "https://evil.example.com/key123",
                resolver=lambda _h, _p: ["10.0.0.5"],
            )
        with self.assertRaises(ValueError):
            mobile_push.build_provider_request(
                "feishu",
                "t",
                "b",
                {"feishu_webhook": "https://evil.com/open-apis/bot/v2/hook/123"},
                resolver=_public_resolver,
            )

    def test_secret_isolation_masking_sanitization_quiet_dedup_and_quota(self):
        self.bridge.update_secrets({"bark_url": "https://api.day.app/SuperSecretBarkKey999"})
        runtime_settings.set_mobile_push(
            enabled=True,
            provider="bark",
            max_daily_pushes=2,
            respect_quiet_hours=True,
            settings_path=self.settings_path,
        )

        # runtime_settings.json must NEVER contain the Bark secret URL
        with open(self.settings_path, "r", encoding="utf-8") as f_rt:
            raw_rt = f_rt.read()
        self.assertNotIn("SuperSecretBarkKey999", raw_rt)

        # Status API returns only masked value
        st = self.bridge.get_status()
        self.assertTrue(st["provider_ready"])
        self.assertNotIn("SuperSecretBarkKey999", json.dumps(st, ensure_ascii=False))
        self.assertTrue(st["secrets"]["bark_url"]["configured"])
        self.assertEqual(st["secrets"]["bark_url"]["source"], "file")

        # Body sanitization strips secrets & phone numbers before sending
        r1 = self.bridge.send("芽衣提醒", "[gentle] 别忘了，密钥 sk-secret1234567890abcdef1234567890 和 13812345678")
        self.assertTrue(r1["ok"])
        sent_body = self.posted_calls[-1][1]["body"]
        self.assertNotIn("sk-secret1234567890abcdef1234567890", sent_body)
        self.assertNotIn("13812345678", sent_body)
        self.assertNotIn("[gentle]", sent_body)

        # Same content within 10 min is deduplicated
        r_dup = self.bridge.send("芽衣提醒", "[gentle] 别忘了，密钥 sk-secret1234567890abcdef1234567890 和 13812345678")
        self.assertTrue(r_dup["skipped"])
        self.assertEqual(r_dup["reason"], "dedup_recent")

        # 2nd unique push succeeds, 3rd hits daily limit (max=2)
        r2 = self.bridge.send("芽衣提醒", "第二条不同内容")
        self.assertTrue(r2["ok"])
        r3 = self.bridge.send("芽衣提醒", "第三条超额内容")
        self.assertTrue(r3["skipped"])
        self.assertEqual(r3["reason"], "daily_limit_reached")

        # Quiet hours (23:30) blocks normal push, while bypass_limits=True still sends test push
        self.now_dt = datetime(2026, 11, 25, 23, 30, 0)
        r_q = self.bridge.send("芽衣提醒", "深夜消息")
        self.assertTrue(r_q["skipped"])
        self.assertEqual(r_q["reason"], "quiet_hours")
        r_test = self.bridge.send("测试", "手工测试", bypass_limits=True)
        self.assertTrue(r_test["ok"])

    def test_proactive_companion_mobile_push_and_pc_and_mobile_modes(self):
        runtime_settings.set_proactive(
            enabled=True,
            delivery_mode="mobile_push",
            quiet_start="23:00",
            quiet_end="08:00",
            settings_path=self.settings_path,
        )
        runtime_settings.set_mobile_push(
            enabled=True,
            provider="bark",
            forward_proactive=True,
            settings_path=self.settings_path,
        )
        mobile_calls = []
        tts_calls = []
        acks = []

        mgr = proactive_companion.ProactiveCompanionEngine(
            history_path=self.history_path,
            settings_path=self.settings_path,
            now_fn=lambda: 1764043200.0,
            local_dt_fn=lambda _ts: datetime(2026, 11, 25, 15, 0, 0),
            rhythm_fetcher=lambda: {"online": True, "activity_category": "other", "idle_seconds": 300, "fullscreen": False},
            anima_context_fetcher=lambda _c, _w: {
                "last_turn_at": 1764043200.0 - 7200.0,
                "due_open_loops": [{"memory_id": "loop-m1", "text": "提交周报"}],
                "inner_life": {},
            },
            anima_ack_sender=lambda c, lid, act, nid: (acks.append((c, lid, act)), True)[1],
            llm_caller=lambda _s, _u, _cfg: '{"action":"SPEAK","emotion":"gentle","text":"周报发完了吗？忙完记得歇会儿。","open_loop_id":"loop-m1","reason":"follow_up"}',
            tts_synthesizer=lambda t, e, _cfg: (tts_calls.append((t, e)), (b"RIFF....", "fake"))[1],
            speaker_player=lambda _w, _v: (True, "ok"),
            busy_checker=lambda: False,
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            mobile_sender=lambda title, text, cat: (mobile_calls.append((title, text, cat)), {"ok": True, "provider": "bark"})[1],
        )

        # 1. mobile_push mode -> pushes to mobile, ZERO TTS/speaker calls, acks Anima open loop
        r_m = mgr.run_once(manual=False)
        self.assertTrue(r_m["ok"])
        self.assertEqual(r_m["outcome"], "pushed_mobile")
        self.assertEqual(len(mobile_calls), 1)
        self.assertIn("雷电芽衣", mobile_calls[0][0])
        self.assertEqual(tts_calls, [])
        self.assertEqual(len(acks), 1)

        # 2. pc_and_mobile mode -> speaks on PC AND sends mobile copy
        runtime_settings.set_proactive(delivery_mode="pc_and_mobile", settings_path=self.settings_path)
        mgr._last_spoke_ts = 0.0
        mgr._consecutive_ignored = 0
        r_pm = mgr.run_once(manual=False)
        self.assertTrue(r_pm["ok"])
        self.assertEqual(r_pm["outcome"], "spoken")
        self.assertEqual(len(tts_calls), 1)
        self.assertEqual(len(mobile_calls), 2)
        self.assertTrue(r_pm["entry"]["mobile_pushed"])

    def test_console_routes_mobile_push_and_obsidian_inbox_validation(self):
        mobile_push.reset_default_bridge_for_tests(self.bridge)

        class _FakeHandler:
            def __init__(self, body=None):
                self._body = body if body is not None else {}
                self.status = 200
                self.payload = None

            def _read_body_json(self):
                return self._body

            def _send_json(self, data, status=200, **_kw):
                self.status = int(status)
                self.payload = data

        orig_get_mp = runtime_settings.get_mobile_push
        orig_set_mp = runtime_settings.set_mobile_push
        orig_trigger_brief = anima_routes._trigger_obsidian_briefing
        orig_trigger_scan = anima_routes._trigger_obsidian_inbox_scan
        runtime_settings.get_mobile_push = lambda sp=None: orig_get_mp(self.settings_path)
        runtime_settings.set_mobile_push = lambda **kw: orig_set_mp(settings_path=self.settings_path, **kw)
        try:
            # GET /api/mobile-push
            h_get = _FakeHandler()
            mp_routes.handle_get_mobile_push(h_get)
            self.assertEqual(h_get.status, 200)
            self.assertFalse(h_get.payload["config"]["enabled"])

            # POST /api/mobile-push/config rejects unknown key & bool max_daily_pushes
            h_bad = _FakeHandler({"evil": 1})
            mp_routes.handle_post_mobile_push_config(h_bad)
            self.assertEqual(h_bad.status, 400)

            h_bad2 = _FakeHandler({"max_daily_pushes": True})
            mp_routes.handle_post_mobile_push_config(h_bad2)
            self.assertEqual(h_bad2.status, 400)

            # POST /api/mobile-push/briefing blocked when disabled -> 403
            h_br_off = _FakeHandler({})
            mp_routes.handle_post_mobile_push_briefing(h_br_off)
            self.assertEqual(h_br_off.status, 403)

            # Enable & save Bark secret via config route -> masked response
            h_cfg = _FakeHandler(
                {
                    "enabled": True,
                    "provider": "bark",
                    "secrets": {"bark_url": "https://api.day.app/MyRouteSecret777"},
                }
            )
            mp_routes.handle_post_mobile_push_config(h_cfg)
            self.assertEqual(h_cfg.status, 200)
            self.assertTrue(h_cfg.payload["provider_ready"])
            self.assertNotIn("MyRouteSecret777", json.dumps(h_cfg.payload, ensure_ascii=False))

            # POST /api/mobile-push/test -> 200
            h_test = _FakeHandler({"provider": "bark"})
            mp_routes.handle_post_mobile_push_test(h_test)
            self.assertEqual(h_test.status, 200)
            self.assertTrue(h_test.payload["ok"])

            # POST /api/mobile-push/briefing -> triggers Anima briefing & pushes summary
            anima_routes._trigger_obsidian_briefing = lambda _p=None: (
                True,
                {
                    "result": {
                        "daily_date": "2026-11-25",
                        "briefing_summary": "2026-11-25 · 今日对话 4 轮 · 待跟进 1 项",
                    },
                    "obsidian": {"online": True},
                },
            )
            h_br_ok = _FakeHandler({})
            mp_routes.handle_post_mobile_push_briefing(h_br_ok)
            self.assertEqual(h_br_ok.status, 200)
            self.assertTrue(h_br_ok.payload["ok"])
            self.assertIn("今日对话 4 轮", self.posted_calls[-1][1]["body"])

            # POST /api/obsidian/inbox/scan rejects unexpected keys & forwards valid empty body
            h_scan_bad = _FakeHandler({"extra": 1})
            anima_routes.handle_post_obsidian_inbox_scan(h_scan_bad)
            self.assertEqual(h_scan_bad.status, 400)

            anima_routes._trigger_obsidian_inbox_scan = lambda: (
                True,
                {"obsidian": {"online": True, "inbox_enabled": True}, "result": {"ok": True, "stats": {"added_facts": 2}}},
                200,
            )
            h_scan_ok = _FakeHandler({})
            anima_routes.handle_post_obsidian_inbox_scan(h_scan_ok)
            self.assertEqual(h_scan_ok.status, 200)
            self.assertEqual(h_scan_ok.payload["result"]["stats"]["added_facts"], 2)

            # Proactive route validator accepts mobile_push & pc_and_mobile
            cleaned = proactive_routes._validate_proactive_update({"delivery_mode": "mobile_push"})
            self.assertEqual(cleaned["delivery_mode"], "mobile_push")
            cleaned2 = proactive_routes._validate_proactive_update({"delivery_mode": "pc_and_mobile"})
            self.assertEqual(cleaned2["delivery_mode"], "pc_and_mobile")
        finally:
            runtime_settings.get_mobile_push = orig_get_mp
            runtime_settings.set_mobile_push = orig_set_mp
            anima_routes._trigger_obsidian_briefing = orig_trigger_brief
            anima_routes._trigger_obsidian_inbox_scan = orig_trigger_scan


if __name__ == "__main__":
    unittest.main()

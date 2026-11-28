"""Unit tests for Phase 3 Official QQ Bot v2 WebSocket bidirectional bridge."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import json
import os
import sys
import tempfile
import unittest

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

import qq_bot_bridge
import runtime_settings
from console.routes import qq_bot as qq_routes


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


class _FakeWebSocket:
    def __init__(self, incoming_frames):
        self._incoming = list(incoming_frames)
        self.sent = []

    async def recv(self):
        if not self._incoming:
            raise EOFError("stream ended")
        item = self._incoming.pop(0)
        return json.dumps(item) if isinstance(item, dict) else item

    async def send(self, data):
        self.sent.append(json.loads(data))


class TestPhase3QqBotBridge(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.settings_path = os.path.join(self._td.name, "runtime_settings.json")
        self.secrets_path = os.path.join(self._td.name, ".qq_bot_secrets.json")
        self.now_ts = 2000.0
        self.http_requests = []
        self.recalled = []
        self.ingested = []
        self.l3_dispatched = []

        def _http_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            self.http_requests.append((request.method, str(request.url), dict(request.headers), body))
            url = str(request.url)
            if url == qq_bot_bridge.TOKEN_ENDPOINT:
                return httpx.Response(200, json={"access_token": "tok_qq_live_123456", "expires_in": 7200})
            if url.endswith("/gateway/bot"):
                return httpx.Response(200, json={"url": "wss://api.sgroup.qq.com/websocket"})
            if "/v2/users/" in url or "/v2/groups/" in url:
                return httpx.Response(200, json={"id": "reply_ok_1"})
            return httpx.Response(404)

        transport = httpx.MockTransport(_http_handler)
        self.bridge = qq_bot_bridge.QQBotBridge(
            settings_path=self.settings_path,
            secrets_path=self.secrets_path,
            now_fn=lambda: self.now_ts,
            http_client_factory=lambda: httpx.Client(transport=transport),
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            anima_recall_fn=lambda dev, q: (self.recalled.append((dev, q)) or "你喜欢喝热茶"),
            anima_ingest_fn=lambda dev, u, a: (self.ingested.append((dev, u, a)) or True),
            llm_reply_fn=lambda msgs, cfg: "[gentle] 收到啦，记得喝热茶！密钥 sk-1234567890abcdefghij",
            l3_status_fn=lambda: "L3 空闲",
            l3_dispatch_fn=lambda instr: (self.l3_dispatched.append(instr) or f"已派发: {instr}"),
            future_task_fn=lambda arg: f"定时任务结果: {arg}",
            weather_fn=lambda city: f"{city or '长沙'} 晴 20℃",
        )
        self.bridge.update_secrets(app_id="102030405", app_secret="secret_qq_99999999")
        qq_bot_bridge.reset_qq_bot_bridge(self.bridge)

    def tearDown(self):
        qq_bot_bridge.reset_qq_bot_bridge(None)
        self._td.cleanup()

    def test_gateway_ssrf_guard_and_token_caching(self):
        self.assertEqual(
            qq_bot_bridge.validate_qq_gateway_url("wss://api.sgroup.qq.com/websocket"),
            "wss://api.sgroup.qq.com/websocket",
        )
        for bad in (
            "ws://api.sgroup.qq.com/websocket",
            "wss://evil.example.com/websocket",
            "wss://127.0.0.1:18080/ws",
            "wss://user:pass@api.sgroup.qq.com/ws",
        ):
            with self.assertRaises(ValueError):
                qq_bot_bridge.validate_qq_gateway_url(bad)

        tok1 = self.bridge.get_access_token()
        tok2 = self.bridge.get_access_token()
        self.assertEqual(tok1, "tok_qq_live_123456")
        self.assertEqual(tok1, tok2)
        # Only 1 token HTTP call due to memory cache
        token_calls = [r for r in self.http_requests if r[1] == qq_bot_bridge.TOKEN_ENDPOINT]
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(self.bridge.fetch_gateway_url(), "wss://api.sgroup.qq.com/websocket")

    def test_fail_closed_allowlist_pairing_and_approval_flow(self):
        # Unknown sender is silently rejected before pairing
        res_unknown = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "m1", "content": "你好", "author": {"user_openid": "OPENID_STRANGER_01"}},
        )
        self.assertTrue(res_unknown["ignored"])
        self.assertEqual(res_unknown["reason"], "unauthorized_sender")
        self.assertEqual(self.ingested, [])

        # Start 6-digit pairing -> user sends `配对 <code>` -> queued for 18080 approval
        pair = self.bridge.start_pairing()
        res_pair = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "m2", "content": f"配对 {pair['code']}", "author": {"user_openid": "OPENID_OWNER_001"}},
        )
        self.assertEqual(res_pair["outcome"], "pairing_handled")
        st = self.bridge.get_status()
        self.assertEqual(len(st["pairing"]["pending"]), 1)

        # Still unauthorized until owner clicks approve in 18080
        res_before = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "m3", "content": "测试", "author": {"user_openid": "OPENID_OWNER_001"}},
        )
        self.assertTrue(res_before["ignored"])

        # Approve pairing -> now C2C chat works, emotion tags & secrets stripped, Anima shared memory used
        self.bridge.approve_pairing("OPENID_OWNER_001", set_as_owner=True)
        res_chat = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "m4", "content": "我出门啦", "author": {"user_openid": "OPENID_OWNER_001"}},
        )
        self.assertTrue(res_chat["ok"])
        self.assertNotIn("[gentle]", res_chat["reply"])
        self.assertNotIn("sk-1234567890abcdefghij", res_chat["reply"])
        self.assertEqual(len(self.recalled), 1)
        self.assertEqual(len(self.ingested), 1)

        # Duplicate msg_id is ignored
        res_dup = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "m4", "content": "我出门啦", "author": {"user_openid": "OPENID_OWNER_001"}},
        )
        self.assertTrue(res_dup["ignored"])
        self.assertEqual(res_dup["reason"], "duplicate_msg_id")

    def test_l3_dispatch_double_gate_and_group_isolation(self):
        runtime_settings.set_qq_bot(
            enabled=True,
            owner_openid="OPENID_OWNER_001",
            allowed_openids=["OPENID_OWNER_001", "OPENID_FRIEND_02"],
            allowed_group_openids=["GROUP_ALLOW_001"],
            allow_l3_dispatch=False,
            settings_path=self.settings_path,
        )

        # 1. Owner C2C blocked when allow_l3_dispatch=False
        r1 = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "g1", "content": "/派发 整理下载", "author": {"user_openid": "OPENID_OWNER_001"}},
        )
        self.assertIn("未开启", r1["reply"])
        self.assertEqual(self.l3_dispatched, [])

        # 2. Enable allow_l3_dispatch -> still blocked in group chat and for non-owner
        runtime_settings.set_qq_bot(allow_l3_dispatch=True, settings_path=self.settings_path)
        r_grp = self.bridge.handle_incoming_event(
            "GROUP_AT_MESSAGE_CREATE",
            {
                "id": "g2",
                "group_openid": "GROUP_ALLOW_001",
                "content": "<@!bot123> /派发 整理下载",
                "author": {"member_openid": "OPENID_OWNER_001"},
            },
        )
        self.assertIn("仅主人私聊", r_grp["reply"])
        self.assertEqual(self.l3_dispatched, [])

        r_friend = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "g3", "content": "/派发 整理下载", "author": {"user_openid": "OPENID_FRIEND_02"}},
        )
        self.assertIn("仅主人私聊", r_friend["reply"])
        self.assertEqual(self.l3_dispatched, [])

        # 3. Owner C2C succeeds when allow_l3_dispatch=True
        r_ok = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "g4", "content": "/派发 整理下载", "author": {"user_openid": "OPENID_OWNER_001"}},
        )
        self.assertIn("已派发: 整理下载", r_ok["reply"])
        self.assertEqual(self.l3_dispatched, ["整理下载"])

        # 4. /提醒 shortcut works
        r_ft = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "g5", "content": "/提醒 10分钟后 喝水", "author": {"user_openid": "OPENID_OWNER_001"}},
        )
        self.assertIn("定时任务结果", r_ft["reply"])

        # 5. Proactive reminder sends to owner and respects daily cap
        pro = self.bridge.send_proactive_reminder("早安", "记得带伞")
        self.assertTrue(pro["ok"])

    def test_websocket_protocol_identify_dispatch_reconnect_and_resume(self):
        runtime_settings.set_qq_bot(
            enabled=True,
            owner_openid="OPENID_OWNER_001",
            allowed_openids=["OPENID_OWNER_001"],
            settings_path=self.settings_path,
        )
        ws1 = _FakeWebSocket(
            [
                {"op": qq_bot_bridge.OP_HELLO, "d": {"heartbeat_interval": 30000}},
                {"op": qq_bot_bridge.OP_DISPATCH, "s": 1, "t": "READY", "d": {"session_id": "sess-abc"}},
                {
                    "op": qq_bot_bridge.OP_DISPATCH,
                    "s": 2,
                    "t": "C2C_MESSAGE_CREATE",
                    "d": {"id": "ws_m1", "content": "/天气 长沙", "author": {"user_openid": "OPENID_OWNER_001"}},
                },
                {"op": qq_bot_bridge.OP_HEARTBEAT_ACK},
                {"op": qq_bot_bridge.OP_RECONNECT},
            ]
        )
        reason1 = asyncio.run(self.bridge.run_gateway_session(ws1))
        self.assertEqual(reason1, "server_reconnect")
        self.assertEqual(ws1.sent[0]["op"], qq_bot_bridge.OP_IDENTIFY)
        self.assertEqual(ws1.sent[0]["d"]["intents"], qq_bot_bridge.INTENT_GROUP_AND_C2C)

        # Next session automatically sends OP=6 RESUME with session_id & seq=2
        ws2 = _FakeWebSocket(
            [
                {"op": qq_bot_bridge.OP_HELLO, "d": {"heartbeat_interval": 30000}},
                {"op": qq_bot_bridge.OP_DISPATCH, "s": 3, "t": "RESUMED", "d": {}},
                {"op": qq_bot_bridge.OP_INVALID_SESSION, "d": False},
            ]
        )
        reason2 = asyncio.run(self.bridge.run_gateway_session(ws2))
        self.assertEqual(reason2, "invalid_session")
        self.assertEqual(ws2.sent[0]["op"], qq_bot_bridge.OP_RESUME)
        self.assertEqual(ws2.sent[0]["d"]["session_id"], "sess-abc")
        self.assertEqual(ws2.sent[0]["d"]["seq"], 2)

    def test_console_routes_hide_secrets_and_validate_inputs(self):
        orig_path = runtime_settings.RUNTIME_SETTINGS_PATH
        runtime_settings.RUNTIME_SETTINGS_PATH = self.settings_path
        try:
            h_bad = _FakeHandler({"unknown_key": 1})
            qq_routes.handle_post_qq_bot(h_bad)
            self.assertEqual(h_bad.status, 400)

            h_ok = _FakeHandler({
                "enabled": False,
                "sandbox": True,
                "allowed_openids": ["OPENID_OWNER_001"],
                "secrets": {"app_id": "102030999", "app_secret": "super_secret_qq_val"},
            })
            qq_routes.handle_post_qq_bot(h_ok)
            self.assertEqual(h_ok.status, 200)
            self.assertNotIn("super_secret_qq_val", str(h_ok.payload))
            with open(self.settings_path, "r", encoding="utf-8") as f:
                self.assertNotIn("super_secret_qq_val", f.read())
        finally:
            runtime_settings.RUNTIME_SETTINGS_PATH = orig_path

    def test_default_anima_recall_ingest_contract_and_qq_error_detail(self):
        captured = []

        def _anima_and_qq_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            captured.append((str(request.url), body))
            url = str(request.url)
            if url.endswith("/xiaozhi/recall"):
                return httpx.Response(
                    200,
                    json={
                        "context_text": "【角色身份设定（来自 Companion Studio）】旧设定\n\n【近期记忆】喜欢热绿茶",
                        "mood": "stage=close, last_emotion=calm",
                    },
                )
            if url.endswith("/xiaozhi/ingest"):
                turns = body.get("turns") or []
                valid = [t for t in turns if t.get("user_message") and t.get("assistant_reply")]
                return httpx.Response(200, json={"queued": len(valid), "accepted_ids": ["r1"] if valid else []})
            if url.endswith("/gateway/bot"):
                return httpx.Response(401, json={"code": 11298, "message": "ip not in whitelist"})
            if url == qq_bot_bridge.TOKEN_ENDPOINT:
                return httpx.Response(200, json={"access_token": "tok_err_test", "expires_in": 7200})
            return httpx.Response(404)

        b = qq_bot_bridge.QQBotBridge(
            settings_path=self.settings_path,
            secrets_path=self.secrets_path,
            http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_anima_and_qq_handler)),
            config_reader=lambda: {"Memory": {"AnimaBrain": {"character_id": "xiaozhi"}}},
        )
        old_tok = os.environ.get("ANIMA_STUDIO_TOKEN")
        os.environ["ANIMA_STUDIO_TOKEN"] = "tok-anima-test-1234567890123456"
        try:
            ctx = b._default_anima_recall("qq-primary-shared", "我喜欢喝什么")
            self.assertIn("喜欢热绿茶", ctx)
            self.assertNotIn("角色身份设定（来自 Companion Studio）", ctx)
            ok = b._default_anima_ingest("qq-primary-shared", "晚上好", "晚上好呀")
            self.assertTrue(ok)
        finally:
            if old_tok is None:
                os.environ.pop("ANIMA_STUDIO_TOKEN", None)
            else:
                os.environ["ANIMA_STUDIO_TOKEN"] = old_tok

        recall_body = [bd for u, bd in captured if u.endswith("/xiaozhi/recall")][0]
        self.assertEqual(recall_body["character_id"], "xiaozhi")
        ingest_body = [bd for u, bd in captured if u.endswith("/xiaozhi/ingest")][0]
        self.assertEqual(ingest_body["character_id"], "xiaozhi")
        self.assertEqual(ingest_body["turns"][0]["user_message"], "晚上好")
        self.assertEqual(ingest_body["turns"][0]["assistant_reply"], "晚上好呀")
        self.assertEqual(ingest_body["turns"][0]["source"], "qq_bot")

        with self.assertRaises(RuntimeError) as cm:
            b.fetch_gateway_url()
        self.assertIn("code=11298", str(cm.exception))
        self.assertIn("ip not in whitelist", str(cm.exception))

    def test_bare_six_digit_pairing_and_start_clears_stale_stop_event(self):
        pair = self.bridge.start_pairing()
        # Wrong bare 6-digit code from stranger stays silently ignored (fail-closed)
        wrong_code = "000000" if pair["code"] != "000000" else "999999"
        r_wrong = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "b1", "content": wrong_code, "author": {"user_openid": "OPENID_STRANGER_BARE"}},
        )
        self.assertTrue(r_wrong["ignored"])
        self.assertEqual(r_wrong["reason"], "unauthorized_sender")

        # Matching bare 6-digit code queues pairing request without requiring `配对 ` prefix
        r_bare = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "b2", "content": pair["code"], "author": {"user_openid": "OPENID_BARE_OWNER"}},
        )
        self.assertEqual(r_bare["outcome"], "pairing_handled")
        self.assertEqual(len(self.bridge.get_status()["pairing"]["pending"]), 1)

        # Once approved, sending a 6-digit number goes to normal chat rather than pairing
        self.bridge.approve_pairing("OPENID_BARE_OWNER", set_as_owner=True)
        r_chat_num = self.bridge.handle_incoming_event(
            "C2C_MESSAGE_CREATE",
            {"id": "b3", "content": "123456", "author": {"user_openid": "OPENID_BARE_OWNER"}},
        )
        self.assertEqual(r_chat_num["outcome"], "replied")

        # start() must clear stale _stop_event even if previous worker thread is still winding down
        class _AliveStub:
            def is_alive(self):
                return True

        runtime_settings.set_qq_bot(enabled=True, settings_path=self.settings_path)
        self.bridge._stop_event.set()
        self.bridge._reconnect_event.clear()
        self.bridge._thread = _AliveStub()
        try:
            self.bridge.start()
            self.assertFalse(self.bridge._stop_event.is_set())
            self.assertTrue(self.bridge._reconnect_event.is_set())
        finally:
            self.bridge._thread = None

    def test_task_notification_and_console_notify_route(self):
        from console import server as console_server

        self.assertIn("/api/qq-bot/notify", console_server._POST_ROUTES)

        # 1. Input validation rejects unknown fields, wrong types, and empty payloads
        for bad_body in (
            {"title": "ok", "openid": "INJECTED_OPENID"},
            {"title": 123, "body": "hello"},
            {"title": "   ", "body": "   "},
            {"title": "x" * 121, "body": "ok"},
        ):
            h_bad = _FakeHandler(bad_body)
            qq_routes.handle_post_qq_bot_notify(h_bad)
            self.assertEqual(h_bad.status, 400)

        # 2. Disabled QQ bot returns 409 conflict without sending HTTP request
        runtime_settings.set_qq_bot(enabled=False, owner_openid="OPENID_OWNER_001", settings_path=self.settings_path)
        h_dis = _FakeHandler({"title": "【反重力已完成】xiaozhi-server", "body": "对话：测试"})
        qq_routes.handle_post_qq_bot_notify(h_dis)
        self.assertEqual(h_dis.status, 409)
        self.assertEqual(h_dis.payload["reason"], "disabled")

        # 3. Enabled without owner_openid returns 409 no_owner_openid
        runtime_settings.set_qq_bot(
            enabled=True, owner_openid="", allowed_openids=[], settings_path=self.settings_path
        )
        h_no_owner = _FakeHandler({"title": "【反重力已完成】xiaozhi-server", "body": "对话：测试"})
        qq_routes.handle_post_qq_bot_notify(h_no_owner)
        self.assertEqual(h_no_owner.status, 409)
        self.assertEqual(h_no_owner.payload["reason"], "no_owner_openid")

        # 4. Enabled + bound owner succeeds, strips secrets, avoids double brackets, isolates quota from proactive_today
        runtime_settings.set_qq_bot(
            enabled=True,
            owner_openid="OPENID_OWNER_001",
            allowed_openids=["OPENID_OWNER_001"],
            settings_path=self.settings_path,
        )
        before_calls = len(self.http_requests)
        h_ok = _FakeHandler(
            {
                "title": "【反重力已完成】xiaozhi-server",
                "body": "对话：小智QQ通知\n摘要：密钥 sk-1234567890abcdefghij 已处理",
                "source": "antigravity",
            }
        )
        qq_routes.handle_post_qq_bot_notify(h_ok)
        self.assertEqual(h_ok.status, 200)
        self.assertTrue(h_ok.payload["ok"])
        self.assertEqual(h_ok.payload["via"], "xiaozhi_18080")
        self.assertEqual(h_ok.payload["task_notify_today"], 1)

        sent_c2c = [r for r in self.http_requests[before_calls:] if "/v2/users/OPENID_OWNER_001/messages" in r[1]]
        self.assertEqual(len(sent_c2c), 1)
        sent_text = sent_c2c[0][3]["content"]
        self.assertTrue(sent_text.startswith("【反重力已完成】xiaozhi-server\n"))
        self.assertNotIn("【【", sent_text)
        self.assertNotIn("sk-1234567890abcdefghij", sent_text)

        st = self.bridge.get_status()
        self.assertEqual(st["runtime"]["proactive_today"], 0)
        self.assertEqual(st["runtime"]["task_notify_today"], 1)
        self.assertEqual(st["recent_messages"][0]["channel"], "notify")
        self.assertEqual(st["recent_messages"][0]["sender"], "antigravity")

        # 5. Daily cap enforced with 429
        self.bridge._task_notify_count = qq_bot_bridge.MAX_TASK_NOTIFY_DAILY
        h_lim = _FakeHandler({"title": "【反重力已完成】xiaozhi-server", "body": "超限测试"})
        qq_routes.handle_post_qq_bot_notify(h_lim)
        self.assertEqual(h_lim.status, 429)
        self.assertEqual(h_lim.payload["reason"], "daily_limit")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Unit tests for Phase 2 multi-model LLM Fallback Pool router."""

from __future__ import annotations

from http import HTTPStatus
import os
import sys
import tempfile
from types import SimpleNamespace
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

import llm_fallback_router
import runtime_settings
from console.routes import llm_fallback as fb_routes


class _FakeHttpError(RuntimeError):
    def __init__(self, msg: str, status_code: int):
        super().__init__(msg)
        self.status_code = status_code


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


class TestPhase2LlmFallbackRouter(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.settings_path = os.path.join(self._td.name, "runtime_settings.json")
        self.secrets_path = os.path.join(self._td.name, ".llm_fallback_secrets.json")
        self.now_ts = 1000.0
        self.client_calls = []

        def _make_fake_client(base_url, api_key, timeout):
            def _create(*args, **kwargs):
                self.client_calls.append((base_url, api_key, kwargs.get("model"), kwargs.get("stream")))
                if "broken-backup" in base_url:
                    raise _FakeHttpError("503 service unavailable", 503)
                if kwargs.get("stream"):
                    return iter([SimpleNamespace(chunk="backup-1"), SimpleNamespace(chunk="backup-2")])
                return SimpleNamespace(text="backup-ok", model=kwargs.get("model"))

            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

        self.pool = llm_fallback_router.LLMFallbackPool(
            settings_path=self.settings_path,
            secrets_path=self.secrets_path,
            now_fn=lambda: self.now_ts,
            client_factory=_make_fake_client,
        )
        llm_fallback_router.reset_fallback_pool(self.pool)

    def tearDown(self):
        llm_fallback_router.reset_fallback_pool(None)
        self._td.cleanup()

    def test_ssrf_url_validation_blocks_internal_ports_and_insecure_hosts(self):
        self.assertEqual(
            llm_fallback_router.validate_fallback_base_url("https://api.deepseek.com/v1/"),
            "https://api.deepseek.com/v1",
        )
        # Non-management loopback inference gateway allowed
        self.assertEqual(
            llm_fallback_router.validate_fallback_base_url("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434/v1",
        )
        for bad in (
            "http://api.deepseek.com/v1",
            "https://user:pass@api.deepseek.com/v1",
            "https://api.deepseek.com/v1?key=abc",
            "http://127.0.0.1:18080/api/state",
            "http://host.docker.internal:18900/mcp",
            "http://anima-brain:18787/health",
            "http://169.254.169.254/latest/meta-data",
            "https://172.17.0.1/v1",
        ):
            with self.assertRaises(ValueError, msg=f"Should reject {bad}"):
                llm_fallback_router.validate_fallback_base_url(bad)

    def test_streaming_failover_before_first_chunk_and_no_switch_mid_stream(self):
        runtime_settings.set_llm_fallback(
            enabled=True,
            cooldown_seconds=60,
            nodes=[
                {
                    "id": "fb-bad",
                    "label": "故障备1",
                    "base_url": "https://broken-backup.example/v1",
                    "model": "m-bad",
                    "api_key_env": "",
                    "enabled": True,
                },
                {
                    "id": "fb-good",
                    "label": "健康备2",
                    "base_url": "https://healthy-backup.example/v1",
                    "model": "m-good",
                    "api_key_env": "",
                    "enabled": True,
                },
            ],
            settings_path=self.settings_path,
        )
        self.pool.update_node_secrets({"fb-good": "sk-backup-secret-1234567890"})

        # Case 1: Primary fails on 429 before first chunk -> skips broken-backup (503) -> succeeds on fb-good
        primary_calls = []

        def _primary_429(*args, **kwargs):
            primary_calls.append(kwargs.get("model"))
            raise _FakeHttpError("rate limit sk-primary-leak-1234567890123", 429)

        stream, used_model, used_node = self.pool.execute_chat_completion(
            primary_create=_primary_429,
            primary_model="deepseek-chat",
            args=(),
            kwargs={"model": "deepseek-chat", "stream": True},
        )
        self.assertEqual(used_node, "fb-good")
        self.assertEqual(used_model, "m-good")
        chunks = [c.chunk for c in stream]
        self.assertEqual(chunks, ["backup-1", "backup-2"])

        # Secret must be redacted from status error & masked in node view
        st = self.pool.get_status()
        self.assertNotIn("sk-primary-leak-1234567890123", st["primary"]["last_error"])
        self.assertNotIn("sk-backup-secret-1234567890", str(st))
        self.assertEqual(st["stats"]["total_failovers"], 1)
        self.assertGreater(st["primary"]["cooling_seconds"], 0)

        # Case 2: While primary is in cooldown, next call goes straight to fb-good without hitting primary
        primary_calls.clear()
        res2, _, node2 = self.pool.execute_chat_completion(
            primary_create=_primary_429,
            primary_model="deepseek-chat",
            args=(),
            kwargs={"model": "deepseek-chat", "stream": False},
        )
        self.assertEqual(node2, "fb-good")
        self.assertEqual(primary_calls, [])
        self.assertEqual(res2.text, "backup-ok")

        # Case 3: After cooldown expires, primary half-open probes and recovers; mid-stream failure never switches
        self.now_ts += 65.0

        def _primary_mid_stream_fail(*args, **kwargs):
            def _gen():
                yield SimpleNamespace(chunk="p-1")
                raise _FakeHttpError("503 mid-stream", 503)

            return _gen()

        stream3, _, node3 = self.pool.execute_chat_completion(
            primary_create=_primary_mid_stream_fail,
            primary_model="deepseek-chat",
            args=(),
            kwargs={"model": "deepseek-chat", "stream": True},
        )
        self.assertEqual(node3, "primary")
        self.assertEqual(next(stream3).chunk, "p-1")
        with self.assertRaises(_FakeHttpError):
            next(stream3)

    def test_non_retryable_400_does_not_failover(self):
        runtime_settings.set_llm_fallback(
            enabled=True,
            nodes=[
                {
                    "id": "fb-good",
                    "label": "健康备",
                    "base_url": "https://healthy-backup.example/v1",
                    "model": "m-good",
                    "enabled": True,
                }
            ],
            settings_path=self.settings_path,
        )

        def _primary_400(*args, **kwargs):
            raise _FakeHttpError("400 bad request", 400)

        with self.assertRaises(_FakeHttpError):
            self.pool.execute_chat_completion(
                primary_create=_primary_400,
                primary_model="m-main",
                args=(),
                kwargs={"model": "m-main", "stream": False},
            )
        self.assertEqual(self.client_calls, [])

    def test_console_routes_validation_and_secret_isolation(self):
        h_bad = _FakeHandler({"enabled": True, "nodes": [{"id": "fb1", "base_url": "http://127.0.0.1:18080", "model": "x"}]})
        fb_routes.handle_post_llm_fallback(h_bad)
        self.assertEqual(h_bad.status, 400)

        h_ok = _FakeHandler({
            "enabled": True,
            "cooldown_seconds": 45,
            "nodes": [
                {
                    "id": "fb1",
                    "label": "硅基备用",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "model": "deepseek-ai/DeepSeek-V3",
                    "api_key_env": "SILICONFLOW_API_KEY",
                    "enabled": True,
                }
            ],
            "secrets": {"fb1": "sk-secret-sidecar-99999999"},
        })
        # Temporarily point runtime_settings default path at temp file via monkeypatch
        orig_path = runtime_settings.RUNTIME_SETTINGS_PATH
        runtime_settings.RUNTIME_SETTINGS_PATH = self.settings_path
        try:
            fb_routes.handle_post_llm_fallback(h_ok)
            self.assertEqual(h_ok.status, 200)
            self.assertNotIn("sk-secret-sidecar-99999999", str(h_ok.payload))
            with open(self.settings_path, "r", encoding="utf-8") as f:
                raw_cfg = f.read()
            self.assertNotIn("sk-secret-sidecar-99999999", raw_cfg)
        finally:
            runtime_settings.RUNTIME_SETTINGS_PATH = orig_path


if __name__ == "__main__":
    unittest.main(verbosity=2)

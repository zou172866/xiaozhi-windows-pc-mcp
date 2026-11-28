"""Unit tests for Anima Brain FallbackProvider (Phase 2)."""

from __future__ import annotations

import os
import sys
import unittest

import httpx

_ANIMA_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _ANIMA_SRC not in sys.path:
    sys.path.insert(0, _ANIMA_SRC)

from companion_engine.providers import CompatibleProvider, FallbackProvider, ProviderError


def _ok_resp(text: str = '{"ok": true}'):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


class TestAnimaFallbackProvider(unittest.TestCase):
    def test_failover_on_503_and_429_and_connect_error_then_cooldown(self):
        now = [100.0]
        calls = []

        def primary_handler(request):
            calls.append(("primary", str(request.url)))
            return httpx.Response(503)

        def backup_handler(request):
            calls.append(("backup", str(request.url)))
            return _ok_resp('{"from": "backup"}')

        p1 = CompatibleProvider(
            base_url="https://primary.example/v1",
            model="m1",
            api_key="k1",
            transport=httpx.MockTransport(primary_handler),
        )
        p2 = CompatibleProvider(
            base_url="https://backup.example/v1",
            model="m2",
            api_key="k2",
            transport=httpx.MockTransport(backup_handler),
        )
        fb = FallbackProvider([p1, p2], cooldown_seconds=60.0, now_fn=lambda: now[0])

        res1 = fb.structured("sys", "data")
        self.assertEqual(res1.model, "m2")
        self.assertEqual([c[0] for c in calls], ["primary", "backup"])

        # During cooldown, primary is bypassed
        calls.clear()
        res2 = fb.structured("sys", "data")
        self.assertEqual(res2.model, "m2")
        self.assertEqual([c[0] for c in calls], ["backup"])

        # ConnectError also fails over safely
        def connect_err_handler(request):
            raise httpx.ConnectError("refused")

        p_conn = CompatibleProvider(
            base_url="https://down.example/v1",
            model="m-down",
            api_key="k1",
            transport=httpx.MockTransport(connect_err_handler),
        )
        fb_conn = FallbackProvider([p_conn, p2])
        res3 = fb_conn.structured("sys", "data")
        self.assertEqual(res3.model, "m2")

    def test_read_timeout_never_silently_recharges_backup(self):
        backup_called = []

        def read_timeout_handler(request):
            raise httpx.ReadTimeout("timed out after request accepted")

        def backup_handler(request):
            backup_called.append(True)
            return _ok_resp()

        p1 = CompatibleProvider(
            base_url="https://slow.example/v1",
            model="m1",
            api_key="k1",
            transport=httpx.MockTransport(read_timeout_handler),
        )
        p2 = CompatibleProvider(
            base_url="https://backup.example/v1",
            model="m2",
            api_key="k2",
            transport=httpx.MockTransport(backup_handler),
        )
        fb = FallbackProvider([p1, p2])
        with self.assertRaises(ProviderError) as ctx:
            fb.structured("sys", "data")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(backup_called, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Batch 3 unit & HTTP integration tests for 18080 Web Console hardening (D-1/2/4/5/6/7, H-6)
and B-5 / F-1 / G-4 API + UI wiring.

Pure offline tests (zero production config/DB/GPU mutation).
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import httpx
import yaml

ROOT_DIR = "/opt/xiaozhi-esp32-server"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DIR = os.environ.get("XIAOZHI_APP_DIR") or (
    os.path.join(ROOT_DIR, "app")
    if os.path.isdir(os.path.join(ROOT_DIR, "app"))
    else os.path.join(_REPO_ROOT, "app")
)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
if ROOT_DIR not in sys.path and os.path.isdir(ROOT_DIR):
    sys.path.insert(0, ROOT_DIR)

import console_server as cs
import runtime_settings as rs
import xz_common

PROJECT_ROOT = (
    _REPO_ROOT
    if os.path.isfile(os.path.join(_REPO_ROOT, "docker-compose.yml"))
    else os.path.dirname(APP_DIR)
)
CONFIG_TEMPLATES_DIR = (
    os.path.join(xz_common.ROOT_DIR, "config_templates")
    if os.path.isdir(os.path.join(xz_common.ROOT_DIR, "config_templates"))
    else os.path.join(PROJECT_ROOT, "config")
)
HOST_SCRIPTS_DIR = (
    os.environ.get("XIAOZHI_HOST_SCRIPTS_DIR")
    if os.environ.get("XIAOZHI_HOST_SCRIPTS_DIR") and os.path.isdir(os.environ.get("XIAOZHI_HOST_SCRIPTS_DIR", ""))
    else os.path.join(PROJECT_ROOT, "data")
)


def _start_test_console_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), cs.ConsoleHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _post_headers(port: int, *, token: str | None = None) -> dict[str, str]:
    hdrs = {
        "Content-Type": "application/json",
        "Origin": f"http://127.0.0.1:{port}",
    }
    if token is not None:
        hdrs["X-Console-Token"] = token
    return hdrs


class TestBatch3ConsoleHardening(unittest.TestCase):
    def setUp(self):
        cs._reset_rate_limited_logs()

    def test_d1_coalesced_async_history_flush_and_zero_fsync_in_sentence_start(self):
        """D-1: _save_history only marks dirty; zero disk writes in sentence_start; background flusher coalesces & flushes on shutdown."""
        orig_msgs = list(cs._chat_messages)
        orig_dev = dict(cs._device_info)
        orig_open = cs._current_ai_turn_open
        orig_has_tts = cs._turn_has_tts
        orig_dlg_open = cs._turn_dialogue_bubble_open
        orig_seq = cs._last_event_seq
        orig_dirty = cs._history_dirty
        orig_dirty_gen = cs._history_dirty_gen
        orig_flushed_gen = cs._history_flushed_gen

        with tempfile.TemporaryDirectory() as td:
            hist_path = os.path.join(td, "chat_history.json")
            write_calls: list[str] = []
            orig_atomic = cs._atomic_write_text

            def _counting_atomic(path, content):
                res = orig_atomic(path, content)
                if os.path.abspath(path) == os.path.abspath(hist_path):
                    write_calls.append(content)
                return res

            try:
                with (
                    patch.object(cs, "HISTORY_PATH", hist_path),
                    patch.object(cs, "_atomic_write_text", side_effect=_counting_atomic),
                    patch.object(cs, "_HISTORY_FLUSH_INTERVAL", 0.08),
                ):
                    cs.stop_history_flusher()
                    with cs._state_lock:
                        cs._chat_messages = []
                        cs._current_ai_turn_open = False
                        cs._turn_has_tts = False
                        cs._turn_dialogue_bubble_open = False
                    with cs._history_dirty_lock:
                        cs._history_dirty = False

                    # 1. Rapid runtime events including sentence_start -> ZERO synchronous disk writes!
                    cs._apply_runtime_event({"type": "user_message", "text": "你好芽衣", "time": "10:00:01"})
                    cs._apply_runtime_event({"type": "tts_state", "state": "sentence_start", "text": "[gentle] 哥哥好呀。", "time": "10:00:02"})
                    cs._apply_runtime_event({"type": "tts_state", "state": "sentence_start", "text": "今天过得怎么样？", "time": "10:00:03"})
                    cs._apply_runtime_event({"type": "tts_state", "state": "stop", "time": "10:00:04"})

                    self.assertEqual(write_calls, [], "Hot callbacks (incl. sentence_start) must do ZERO disk writes/fsync")
                    self.assertTrue(cs.is_history_dirty())
                    self.assertFalse(os.path.exists(hist_path))

                    # 2. Start background flusher -> coalesces all 4 dirty marks into ONE disk write
                    cs.start_history_flusher()
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and (not write_calls or cs.is_history_dirty()):
                        time.sleep(0.02)
                    self.assertEqual(len(write_calls), 1, "Multiple rapid updates should coalesce into 1 write")
                    self.assertFalse(cs.is_history_dirty())
                    with open(hist_path, "r", encoding="utf-8") as f:
                        saved = json.load(f)
                    self.assertEqual(len(saved), 2)
                    self.assertEqual(saved[0]["text"], "你好芽衣")
                    self.assertEqual(saved[1]["text"], "哥哥好呀。今天过得怎么样？")

                    # 3. Concurrent write-during-flush keeps dirty=True so newer turn is NEVER lost
                    cs.stop_history_flusher()
                    write_calls.clear()
                    in_write = threading.Event()
                    allow_finish = threading.Event()

                    def _slow_atomic(path, content):
                        if os.path.abspath(path) == os.path.abspath(hist_path):
                            in_write.set()
                            allow_finish.wait(timeout=2.0)
                            write_calls.append(content)
                        return orig_atomic(path, content)

                    with patch.object(cs, "_atomic_write_text", side_effect=_slow_atomic):
                        cs._apply_runtime_event({"type": "user_message", "text": "第一条", "time": "10:01:00"})
                        t_fl = threading.Thread(target=cs.flush_history_if_dirty)
                        t_fl.start()
                        self.assertTrue(in_write.wait(timeout=2.0))
                        # New turn arrives WHILE first flush is writing to disk
                        cs._apply_runtime_event({"type": "user_message", "text": "写入期间到达", "time": "10:01:01"})
                        allow_finish.set()
                        t_fl.join(timeout=2.0)

                    self.assertTrue(cs.is_history_dirty(), "Dirty flag must stay True when updated mid-flush")
                    # Shutdown flush persists the missed update cleanly
                    cs._flush_console_state_on_shutdown()
                    self.assertFalse(cs.is_history_dirty())
                    with open(hist_path, "r", encoding="utf-8") as f:
                        saved_final = json.load(f)
                    self.assertEqual(saved_final[-1]["text"], "写入期间到达")

                    # 4. Concurrent flushers (background timer vs shutdown flush) MUST serialize via
                    #    _history_flush_lock before snapshotting so stale gen=1 NEVER overwrites gen=2!
                    write_order: list[int] = []
                    t1_in_write = threading.Event()
                    t1_release = threading.Event()

                    def _ordered_atomic(path, content):
                        if os.path.abspath(path) == os.path.abspath(hist_path):
                            items = json.loads(content)
                            write_order.append(len(items))
                            if len(write_order) == 1:
                                t1_in_write.set()
                                t1_release.wait(timeout=2.0)
                        return orig_atomic(path, content)

                    with patch.object(cs, "_atomic_write_text", side_effect=_ordered_atomic):
                        cs._apply_runtime_event({"type": "user_message", "text": "并发m1", "time": "10:02:01"})
                        t_bg = threading.Thread(target=cs.flush_history_if_dirty, name="bg-flush")
                        t_bg.start()
                        self.assertTrue(t1_in_write.wait(timeout=2.0))

                        # m2 arrives AND concurrent shutdown flush starts while bg-flush is mid-write
                        cs._apply_runtime_event({"type": "user_message", "text": "并发m2", "time": "10:02:02"})
                        t_sd = threading.Thread(target=cs.flush_history_if_dirty, name="shutdown-flush")
                        t_sd.start()
                        time.sleep(0.05)  # Give shutdown-flush time to contend on _history_flush_lock
                        t1_release.set()
                        t_bg.join(timeout=2.0)
                        t_sd.join(timeout=2.0)

                    self.assertEqual(len(write_order), 2)
                    self.assertLess(write_order[0], write_order[1], "Writes must be strictly monotonic (never stale overwrite)")
                    self.assertFalse(cs.is_history_dirty())
                    with open(hist_path, "r", encoding="utf-8") as f:
                        saved_conc = json.load(f)
                    self.assertEqual(saved_conc[-1]["text"], "并发m2")
            finally:
                cs.stop_history_flusher()
                with cs._state_lock:
                    cs._chat_messages = orig_msgs
                    cs._device_info = orig_dev
                    cs._current_ai_turn_open = orig_open
                    cs._turn_has_tts = orig_has_tts
                    cs._turn_dialogue_bubble_open = orig_dlg_open
                    cs._last_event_seq = orig_seq
                with cs._history_dirty_lock:
                    cs._history_dirty = orig_dirty
                    cs._history_dirty_gen = orig_dirty_gen
                    cs._history_flushed_gen = orig_flushed_gen

    def test_d2_config_write_lock_and_strict_allowlist_http_400(self):
        """D-2: _config_write_lock prevents lost updates; invalid provider/model/voice returns HTTP 400 (no DeepSeek fallback)."""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, ".config.yaml")
            initial_cfg = {
                "prompt": "初始芽衣人设。\n",
                "selected_module": {
                    "LLM": "DeepSeekLLM",
                    "TTS": "GPT_SOVITS_V2",
                },
                "LLM": {
                    "DeepSeekLLM": {
                        "type": "openai",
                        "base_url": "https://api.deepseek.com",
                        "model_name": "deepseek-flash",
                        "api_key": "${DEEPSEEK_API_KEY}",
                    },
                    "GeminiOpenAI": {
                        "type": "openai",
                        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                        "model_name": "gemini-3.5-flash-lite",
                        "api_key": "${GEMINI_API_KEY}",
                    },
                },
                "TTS": {
                    "GPT_SOVITS_V2": {
                        "type": "gpt_sovits_v2",
                        "voice_id": "__gsv_raiden_mei__",
                        "url": "http://host.docker.internal:19880/tts",
                        "ref_audio_path": "${HOST_VOICES_DIR}/raiden_mei/mei_default_ref.wav",
                    }
                },
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(initial_cfg, f, allow_unicode=True, sort_keys=False)

            httpd, port = _start_test_console_server()
            orig_port = cs.CONSOLE_PORT
            cs.CONSOLE_PORT = port
            try:
                with (
                    patch.object(cs, "CONFIG_PATH", cfg_path),
                    patch.object(cs, "_ensure_gsv_weights"),
                    patch.object(cs, "_sync_persona_to_anima"),
                    patch.object(cs, "_hot_reload_runtime_config", return_value=(True, "hot_reloaded")),
                ):
                    tok = cs.get_console_token()
                    hdrs = _post_headers(port, token=tok)
                    with httpx.Client(timeout=5.0, trust_env=False) as client:
                        # 1. Invalid llm_provider -> HTTP 400 (must NOT fall back to DeepSeekLLM)
                        for bad_body in (
                            {"llm_provider": "UnknownProvider"},
                            {"llm_provider": ""},
                            {"llm_provider": 123},
                            {"llm_provider": "DeepSeekLLM", "model_name": "evil-injected-model"},
                            {"llm_provider": "DeepSeekLLM", "model_name": "__custom__"},
                            {"llm_provider": "DeepSeekLLM", "model_name": ""},
                            # Cross-provider model mismatch -> HTTP 400
                            {"llm_provider": "DeepSeekLLM", "model_name": "gemini-3.5-flash-lite"},
                            {"llm_provider": "GeminiOpenAI", "model_name": "deepseek-flash"},
                            # Invalid voice -> HTTP 400
                            {"voice": "__gsv_nonexistent__"},
                            {"voice": "en-US-EvilNeural"},
                            {"voice": ""},
                            {"voice": ["not-a-string"]},
                        ):
                            r = client.post(
                                f"http://127.0.0.1:{port}/api/config",
                                json=bad_body,
                                headers=hdrs,
                            )
                            self.assertEqual(
                                r.status_code,
                                400,
                                f"Expected 400 for {bad_body!r}, got {r.status_code}: {r.text}",
                            )
                            self.assertFalse(r.json().get("ok"))

                        # Also reject invalid voice on /api/tts-preview with HTTP 400
                        r_prev_bad = client.post(
                            f"http://127.0.0.1:{port}/api/tts-preview",
                            json={"voice": "evil-voice-name", "text": "你好"},
                            headers=hdrs,
                        )
                        self.assertEqual(r_prev_bad.status_code, 400)

                        # Disk config remains completely unmodified after all rejected 400 requests
                        with open(cfg_path, "r", encoding="utf-8") as f:
                            unchanged = yaml.safe_load(f)
                        self.assertEqual(unchanged["selected_module"]["LLM"], "DeepSeekLLM")
                        self.assertEqual(unchanged["LLM"]["DeepSeekLLM"]["model_name"], "deepseek-flash")

                        # 2. Valid allowlisted provider/model/voice update succeeds (200)
                        r_ok = client.post(
                            f"http://127.0.0.1:{port}/api/config",
                            json={
                                "llm_provider": "GeminiOpenAI",
                                "model_name": "gemini-3.1-flash-lite",
                                "voice": "zh-TW-HsiaoChenNeural",
                                "restart": False,
                            },
                            headers=hdrs,
                        )
                        self.assertEqual(r_ok.status_code, 200, r_ok.text)
                        self.assertTrue(r_ok.json().get("ok"))

                    # 3. Concurrency test: _config_write_lock serializes concurrent read-modify-writes with zero lost updates
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(initial_cfg, f, allow_unicode=True, sort_keys=False)
                    cs.invalidate_config_yaml_cache(cfg_path)

                    errors: list[Exception] = []
                    orig_write = cs._atomic_write_text

                    def _delayed_write(p, c):
                        time.sleep(0.03)
                        return orig_write(p, c)

                    def _w_prompt():
                        try:
                            cs._write_updated_yaml(prompt="并发更新后的新提示词")
                        except Exception as exc:
                            errors.append(exc)

                    def _w_model():
                        try:
                            cs._write_updated_yaml(llm_provider="DeepSeekLLM", model_name="deepseek-reasoner")
                        except Exception as exc:
                            errors.append(exc)

                    with patch.object(cs, "_atomic_write_text", side_effect=_delayed_write):
                        t1 = threading.Thread(target=_w_prompt)
                        t2 = threading.Thread(target=_w_model)
                        t1.start()
                        t2.start()
                        t1.join(timeout=3.0)
                        t2.join(timeout=3.0)

                    self.assertEqual(errors, [])
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        final_cfg = yaml.safe_load(f)
                    self.assertEqual(final_cfg["prompt"].strip(), "并发更新后的新提示词")
                    self.assertEqual(final_cfg["LLM"]["DeepSeekLLM"]["model_name"], "deepseek-reasoner")
            finally:
                cs.CONSOLE_PORT = orig_port
                cs.invalidate_config_yaml_cache(cfg_path)
                httpd.shutdown()
                httpd.server_close()

    def test_d4_bounded_log_reads_cursor_throttle_yaml_mtime_cache_and_html_visibility(self):
        """D-4: 256 KB per-tick log cap, 5s cursor write throttle, YAML MtimeFileCache on /api/state, visibilityState polling pause."""
        orig_logs = list(cs._raw_logs)
        orig_seq = cs._raw_log_seq
        orig_off = cs._log_offset
        try:
            with tempfile.TemporaryDirectory() as td:
                log_p = os.path.join(td, "big.log")
                cur_p = os.path.join(td, ".cursor.json")
                cfg_p = os.path.join(td, ".config.yaml")

                # 1. 256 KB bounded read per tick on a 600 KB log file
                line_payload = "2026-11-20 12:00:00 INFO " + ("A" * 200) + "\n"
                with open(log_p, "w", encoding="utf-8") as f:
                    for i in range(2800):  # ~630 KB
                        f.write(f"line-{i:04d} " + line_payload)
                total_size = os.path.getsize(log_p)
                self.assertGreater(total_size, 550 * 1024)

                cursor_writes: list[tuple[int, int]] = []
                orig_save_cur = cs._save_log_cursor

                def _track_cursor(ino, off):
                    cursor_writes.append((ino, off))
                    return orig_save_cur(ino, off)

                with (
                    patch.object(cs, "LOG_PATH", log_p),
                    patch.object(cs, "LOG_CURSOR_PATH", cur_p),
                    patch.object(cs, "_save_log_cursor", side_effect=_track_cursor),
                ):
                    cs._log_offset = 0
                    st = {
                        "initialized": True,
                        "last_inode": os.stat(log_p).st_ino,
                        "saved_inode": None,
                        "saved_offset": 0,
                    }

                    # Tick 1: reads <= 256 KB and writes initial cursor once
                    cs._poll_log_once(st, raw_logs_only=True)
                    off1 = cs._log_offset
                    self.assertGreater(off1, 0)
                    self.assertLessEqual(off1, cs._LOG_MAX_READ_BYTES)
                    self.assertEqual(len(cursor_writes), 1)

                    # Tick 2 & Tick 3 immediately (within 5s): advance offset <= 256 KB each, ZERO extra cursor disk writes!
                    cs._poll_log_once(st, raw_logs_only=True)
                    off2 = cs._log_offset
                    self.assertGreater(off2, off1)
                    self.assertLessEqual(off2 - off1, cs._LOG_MAX_READ_BYTES)
                    self.assertEqual(len(cursor_writes), 1, "Cursor write must be throttled within 5s window")
                    self.assertTrue(st.get("cursor_dirty"))

                    cs._poll_log_once(st, raw_logs_only=True)
                    off3 = cs._log_offset
                    self.assertEqual(off3, total_size)
                    self.assertEqual(len(cursor_writes), 1, "Cursor write still throttled within 5s window")

                    # Simulate >=5s elapsed -> next tick flushes dirty cursor even with 0 new bytes
                    st["last_cursor_save_ts"] = time.monotonic() - (cs._LOG_CURSOR_WRITE_INTERVAL + 0.5)
                    cs._poll_log_once(st, raw_logs_only=True)
                    self.assertEqual(len(cursor_writes), 2)
                    self.assertEqual(cursor_writes[-1][1], total_size)
                    self.assertFalse(st.get("cursor_dirty"))

                    # Giant >256 KB single unbroken line without newline must NOT stall watcher
                    giant_p = os.path.join(td, "giant_nolf.log")
                    with open(giant_p, "wb") as gf:
                        gf.write(b"X" * (300 * 1024))
                    with patch.object(cs, "LOG_PATH", giant_p):
                        cs._log_offset = 0
                        st_g = {"initialized": True, "last_inode": os.stat(giant_p).st_ino}
                        cs._poll_log_once(st_g, raw_logs_only=True)
                        self.assertEqual(cs._log_offset, cs._LOG_MAX_READ_BYTES)

                # 2. YAML MtimeFileCache on /api/state avoids reparsing unchanged .config.yaml
                sample_cfg = {
                    "prompt": "缓存测试人设",
                    "selected_module": {"LLM": "DeepSeekLLM", "TTS": "GPT_SOVITS_V2"},
                    "LLM": {"DeepSeekLLM": {"model_name": "deepseek-flash"}},
                    "TTS": {"GPT_SOVITS_V2": {"voice_id": "__gsv_raiden_mei__", "ref_audio_path": "x.wav"}},
                }
                with open(cfg_p, "w", encoding="utf-8") as f:
                    yaml.safe_dump(sample_cfg, f, allow_unicode=True)
                cs.invalidate_config_yaml_cache(cfg_p)

                parse_count = {"n": 0}
                orig_parse_yaml = cs._parse_yaml_dict

                def _counting_parse(raw_str):
                    parse_count["n"] += 1
                    return orig_parse_yaml(raw_str)

                httpd, port = _start_test_console_server()
                orig_port = cs.CONSOLE_PORT
                cs.CONSOLE_PORT = port
                try:
                    with (
                        patch.object(cs, "CONFIG_PATH", cfg_p),
                        patch.object(cs, "_parse_yaml_dict", side_effect=_counting_parse),
                    ):
                        # Recreate cache bound to patched _parse_yaml_dict
                        cs._config_yaml_caches.pop(os.path.abspath(cfg_p), None)
                        with httpx.Client(timeout=4.0, trust_env=False) as client:
                            for _ in range(5):
                                r = client.get(f"http://127.0.0.1:{port}/api/state")
                                self.assertEqual(r.status_code, 200)
                                self.assertEqual(r.json()["config"]["prompt"], "缓存测试人设")
                            self.assertEqual(parse_count["n"], 1, "Unchanged .config.yaml should be parsed only once across 5 polls")

                            # Modify file (different content + size) -> cache refreshes cleanly
                            sample_cfg["prompt"] = "已更新的人设内容_长度变化"
                            with open(cfg_p, "w", encoding="utf-8") as f:
                                yaml.safe_dump(sample_cfg, f, allow_unicode=True)
                            r2 = client.get(f"http://127.0.0.1:{port}/api/state")
                            self.assertEqual(r2.status_code, 200)
                            self.assertEqual(r2.json()["config"]["prompt"], "已更新的人设内容_长度变化")
                            self.assertEqual(parse_count["n"], 2)
                finally:
                    cs.CONSOLE_PORT = orig_port
                    cs._config_yaml_caches.pop(os.path.abspath(cfg_p), None)
                    httpd.shutdown()
                    httpd.server_close()
        finally:
            with cs._state_lock:
                cs._raw_logs.clear()
                cs._raw_logs.extend(orig_logs)
                cs._raw_log_seq = orig_seq
                cs._log_offset = orig_off

        # 3. HTML/JS tab-visibility polling pause & resume check (static/index.html + static/app.js)
        with open(cs.JS_PATH, "r", encoding="utf-8") as f:
            js_src = f.read()
        self.assertIn('document.visibilityState !== "visible"', js_src)
        self.assertIn("visibilitychange", js_src)
        self.assertIn("stopPolling()", js_src)
        self.assertIn("startPolling()", js_src)
        self.assertIn("pollState(true)", js_src)

    def test_d5_expanded_secret_redaction_and_unified_send_json_redaction(self):
        """D-5: _redact_secrets covers ?token=, x-goog-api-key, Bearer, sk-, AIza; _send_json auto-redacts all error responses."""
        raw = (
            "GET https://example.com/v1?token=query_secret_123&foo=keepme "
            "and https://generativelanguage.googleapis.com/v1?key=AIzaSySecretKey1234567890123 "
            "headers={'x-goog-api-key': 'goog_hdr_secret_456', 'X-API-Key': 'xapi_secret_789', "
            "'X-Console-Token': 'console_secret_abc'} "
            "Authorization: Bearer bearer_secret_xyz999 "
            "key=sk-deepseek1234567890abcd #token=frag_secret_555"
        )
        red = cs._redact_secrets(raw)
        for leak in (
            "query_secret_123",
            "AIzaSySecretKey1234567890123",
            "goog_hdr_secret_456",
            "xapi_secret_789",
            "console_secret_abc",
            "bearer_secret_xyz999",
            "sk-deepseek1234567890abcd",
            "frag_secret_555",
        ):
            self.assertNotIn(leak, red, f"Secret leaked after redaction: {leak} in {red}")
        self.assertIn("foo=keepme", red)

        # Normal error wording without secret assignment must remain intact
        self.assertEqual(
            cs._redact_secrets("Missing or invalid X-Console-Token"),
            "Missing or invalid X-Console-Token",
        )

        # End-to-end HTTP test: unhandled error string from downstream containing secrets is auto-redacted by _send_json
        httpd, port = _start_test_console_server()
        orig_port = cs.CONSOLE_PORT
        cs.CONSOLE_PORT = port
        try:
            leaking_err = RuntimeError(
                "upstream failed url=http://x?token=leak_tok_111 x-goog-api-key: leak_goog_222 "
                "Bearer leak_bearer_333 sk-leaksecret1234567890"
            )
            with (
                patch.object(cs, "_write_updated_yaml", side_effect=leaking_err),
                httpx.Client(timeout=4.0, trust_env=False) as client,
            ):
                r = client.post(
                    f"http://127.0.0.1:{port}/api/config",
                    json={"prompt": "test"},
                    headers=_post_headers(port, token=cs.get_console_token()),
                )
                self.assertEqual(r.status_code, 500)
                txt = r.text
                for leak in ("leak_tok_111", "leak_goog_222", "leak_bearer_333", "sk-leaksecret1234567890"):
                    self.assertNotIn(leak, txt, f"_send_json leaked secret {leak}: {txt}")
                self.assertIn("***", txt)
        finally:
            cs.CONSOLE_PORT = orig_port
            httpd.shutdown()
            httpd.server_close()

    def test_d6_rate_limited_logging_and_zero_silent_except_exception_pass(self):
        """D-6: _log_rate_limited suppresses repeat bursts within window, reports suppressed count, and redacts secrets."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(cs._log_rate_limited("site_a", RuntimeError("fail Bearer supersecret999"), interval=60.0))
            self.assertFalse(cs._log_rate_limited("site_a", RuntimeError("fail 2"), interval=60.0))
            self.assertFalse(cs._log_rate_limited("site_a", RuntimeError("fail 3"), interval=60.0))
            # Independent site key is not throttled by site_a
            self.assertTrue(cs._log_rate_limited("site_b", RuntimeError("other site"), interval=60.0))

            # Advance past 60s window for site_a -> next call emits with suppressed=2 note
            with cs._rate_log_lock:
                cs._rate_log_last_ts["site_a"] -= 61.0
            self.assertTrue(cs._log_rate_limited("site_a", RuntimeError("fail 4"), interval=60.0))

        out = buf.getvalue()
        self.assertNotIn("supersecret999", out)
        self.assertIn("[site_a]", out)
        self.assertIn("[site_b]", out)
        self.assertIn("suppressed 2 similar", out)

        # Static AST check: zero silent `except Exception: pass` blocks across ALL console modules
        console_py_files = [os.path.join(APP_DIR, "console_server.py")]
        for dirpath, _, fnames in os.walk(os.path.join(APP_DIR, "console")):
            for fn in sorted(fnames):
                if fn.endswith(".py"):
                    console_py_files.append(os.path.join(dirpath, fn))
        silent_exc_lines = []
        for cs_path in console_py_files:
            with open(cs_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=cs_path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    if node.type is None or (isinstance(node.type, ast.Name) and node.type.id == "Exception"):
                        silent_exc_lines.append((os.path.relpath(cs_path, APP_DIR), node.lineno))
        self.assertEqual(silent_exc_lines, [], f"Found silent except Exception: pass at {silent_exc_lines}")

    def test_d7_explicit_persistent_voice_id_and_legacy_fallback(self):
        """D-7: voice_id persisted in TTS config, prioritized over ref_audio_path across all sites, legacy fallback kept."""
        # 1. Single unified resolver: explicit voice_id wins even if ref_audio_path has misleading substring
        cfg_explicit = {
            "selected_module": {"TTS": "GPT_SOVITS_V2"},
            "TTS": {
                "GPT_SOVITS_V2": {
                    "voice_id": "__gsv_xiaozhi_tw__",
                    "ref_audio_path": "${HOST_VOICES_DIR}/raiden_mei/custom_override.wav",
                }
            },
        }
        self.assertEqual(cs._get_current_voice_id(cfg_explicit), "__gsv_xiaozhi_tw__")

        # Legacy config lacking voice_id still falls back to ref_audio_path substring
        cfg_legacy_mei = {
            "selected_module": {"TTS": "GPT_SOVITS_V2"},
            "TTS": {"GPT_SOVITS_V2": {"ref_audio_path": "${HOST_VOICES_DIR}/raiden_mei/mei_default_ref.wav"}},
        }
        cfg_legacy_tw = {
            "selected_module": {"TTS": "GPT_SOVITS_V2"},
            "TTS": {"GPT_SOVITS_V2": {"ref_audio_path": "${HOST_VOICES_DIR}/xiaozhi_taiwan_ref.wav"}},
        }
        self.assertEqual(cs._get_current_voice_id(cfg_legacy_mei), "__gsv_raiden_mei__")
        self.assertEqual(cs._get_current_voice_id(cfg_legacy_tw), "__gsv_xiaozhi_tw__")

        # 2. Switching voice persists explicit voice_id to .config.yaml and /api/state reflects it
        with tempfile.TemporaryDirectory() as td:
            cfg_p = os.path.join(td, ".config.yaml")
            with open(cfg_p, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg_legacy_mei, f, allow_unicode=True)

            httpd, port = _start_test_console_server()
            orig_port = cs.CONSOLE_PORT
            cs.CONSOLE_PORT = port
            try:
                with (
                    patch.object(cs, "CONFIG_PATH", cfg_p),
                    patch.object(cs, "_ensure_gsv_weights"),
                    patch.object(cs, "_sync_persona_to_anima"),
                ):
                    res = cs._write_updated_yaml(voice="__gsv_xiaozhi_tw__")
                    self.assertTrue(res["written"])
                    with open(cfg_p, "r", encoding="utf-8") as f:
                        saved = yaml.safe_load(f)
                    self.assertEqual(saved["TTS"]["GPT_SOVITS_V2"]["voice_id"], "__gsv_xiaozhi_tw__")
                    self.assertEqual(cs._get_configured_gsv_voice(), "__gsv_xiaozhi_tw__")

                    # Mutate ref_audio_path to contain "raiden_mei" while keeping explicit voice_id="__gsv_xiaozhi_tw__"
                    saved["TTS"]["GPT_SOVITS_V2"]["ref_audio_path"] = "voices/raiden_mei/custom.wav"
                    with open(cfg_p, "w", encoding="utf-8") as f:
                        yaml.safe_dump(saved, f, allow_unicode=True)
                    cs.invalidate_config_yaml_cache(cfg_p)

                    with httpx.Client(timeout=4.0, trust_env=False) as client:
                        r_st = client.get(f"http://127.0.0.1:{port}/api/state")
                        self.assertEqual(r_st.status_code, 200)
                        self.assertEqual(r_st.json()["config"]["voice"], "__gsv_xiaozhi_tw__")

                        # Verify /api/tts-preview strips internal voice_id key before posting to 19880 /tts
                        posted_payloads = []

                        class _FakeResp:
                            status_code = 200
                            content = b"RIFF\x24\x00\x00\x00WAVEfmt "
                            text = "ok"

                        class _FakeCli:
                            def post(self, url, json=None, timeout=None):
                                posted_payloads.append(dict(json or {}))
                                return _FakeResp()

                        with (
                            patch.object(cs, "_get_console_http_client", return_value=_FakeCli()),
                            patch.object(cs, "_is_device_busy", return_value=False),
                        ):
                            r_pv = client.post(
                                f"http://127.0.0.1:{port}/api/tts-preview",
                                json={"voice": "__gsv_xiaozhi_tw__", "text": "你好呀"},
                                headers=_post_headers(port, token=cs.get_console_token()),
                            )
                            self.assertEqual(r_pv.status_code, 200)
                        self.assertEqual(len(posted_payloads), 1)
                        self.assertNotIn("voice_id", posted_payloads[0])
            finally:
                cs.CONSOLE_PORT = orig_port
                cs.invalidate_config_yaml_cache(cfg_p)
                httpd.shutdown()
                httpd.server_close()

        # 3. No duplicate "raiden_mei" in ref_p inference across console modules (only 1 legacy fallback)
        ref_count = 0
        for dirpath, _, fnames in os.walk(os.path.join(APP_DIR, "console")):
            for fn in sorted(fnames):
                if fn.endswith(".py"):
                    with open(os.path.join(dirpath, fn), "r", encoding="utf-8") as f:
                        ref_count += f.read().count('"raiden_mei" in ref_p')
        with open(os.path.join(APP_DIR, "console_server.py"), "r", encoding="utf-8") as f:
            ref_count += f.read().count('"raiden_mei" in ref_p')
        self.assertEqual(ref_count, 1)

        # 4. config/.config.example.yaml includes explicit voice_id
        ex_path = os.path.join(CONFIG_TEMPLATES_DIR, ".config.example.yaml")
        if os.path.isfile(ex_path):
            with open(ex_path, "r", encoding="utf-8") as f:
                ex_cfg = yaml.safe_load(f) or {}
            self.assertEqual(
                ((ex_cfg.get("TTS") or {}).get("GPT_SOVITS_V2") or {}).get("voice_id"),
                "__gsv_raiden_mei__",
            )

    def test_b5_f1_g4_endpoints_token_enforcement_validation_and_ui_wiring(self):
        """B-5 / F-1 / G-4: GET/POST endpoints, X-Console-Token 401 guard, 400 validation, and console.html DOM wiring."""
        httpd, port = _start_test_console_server()
        orig_port = cs.CONSOLE_PORT
        cs.CONSOLE_PORT = port

        # Prepare mock/isolated backends so tests pass cleanly regardless of external module state
        outbox_state = {
            "db_path": "/secret/path/outbox.sqlite",
            "pending": 1,
            "dead": 2,
            "corrupt": 0,
            "sent": 15,
            "max_retries": 8,
            "dead_ttl_days": 14.0,
            "last_error": "",
            "worker_alive": True,
        }

        def _mock_get_outbox():
            return dict(outbox_state)

        def _mock_replay_dead(*, limit=None, request_ids=None):
            n = outbox_state["dead"] if limit is None else min(outbox_state["dead"], int(limit))
            outbox_state["dead"] -= n
            outbox_state["pending"] += n
            return {"ok": True, "replayed": n, "stats": dict(outbox_state)}

        fake_amp = types.ModuleType("anima_memory_provider")
        fake_amp.get_outbox_stats = _mock_get_outbox
        fake_amp.replay_dead_outbox_turns = _mock_replay_dead

        vad_state = {
            "enabled": True,
            "speculative_ms": 550,
            "post_wakeup_ms": 800,
            "min_chars": 4,
            "complete_suffixes": ["吗", "呢", "吧"],
            "hesitation_suffixes": ["然后", "就是", "那个"],
        }

        def _mock_get_vad(settings_path=None):
            return dict(vad_state)

        def _mock_set_vad(
            enabled=None,
            speculative_ms=None,
            post_wakeup_ms=None,
            min_chars=None,
            complete_suffixes=None,
            hesitation_suffixes=None,
            settings_path=None,
        ):
            if enabled is not None:
                vad_state["enabled"] = bool(enabled)
            if speculative_ms is not None:
                vad_state["speculative_ms"] = int(speculative_ms)
            if post_wakeup_ms is not None:
                vad_state["post_wakeup_ms"] = max(vad_state["speculative_ms"], int(post_wakeup_ms))
            if min_chars is not None:
                vad_state["min_chars"] = int(min_chars)
            if complete_suffixes is not None:
                vad_state["complete_suffixes"] = (
                    [x.strip() for x in re.split(r"[\r\n,，]+", complete_suffixes) if x.strip()]
                    if isinstance(complete_suffixes, str)
                    else list(complete_suffixes)
                )
            if hesitation_suffixes is not None:
                vad_state["hesitation_suffixes"] = (
                    [x.strip() for x in re.split(r"[\r\n,，]+", hesitation_suffixes) if x.strip()]
                    if isinstance(hesitation_suffixes, str)
                    else list(hesitation_suffixes)
                )
            return dict(vad_state)

        asr_state = {
            "schema_version": 1,
            "enabled": True,
            "corrections": {"雷电牙医": "雷电芽衣", "牙医": "芽衣"},
            "count": 2,
        }

        def _mock_get_asr(path=None):
            return {
                "schema_version": 1,
                "enabled": asr_state["enabled"],
                "corrections": dict(asr_state["corrections"]),
                "count": len(asr_state["corrections"]),
            }

        def _mock_save_asr(corrections=None, *, enabled=None, path=None):
            if enabled is not None:
                asr_state["enabled"] = bool(enabled)
            if corrections is not None:
                if isinstance(corrections, str):
                    parsed = {}
                    for ln in corrections.splitlines():
                        ln = ln.strip()
                        if not ln:
                            continue
                        if "->" not in ln:
                            raise ValueError(f"无效规则格式: {ln}")
                        k, v = ln.split("->", 1)
                        k, v = k.strip(), v.strip()
                        if not k or not v or len(k) > 64 or len(v) > 64:
                            raise ValueError("词条长度超出限制")
                        parsed[k] = v
                    asr_state["corrections"] = parsed
                elif isinstance(corrections, dict):
                    asr_state["corrections"] = {str(k): str(v) for k, v in corrections.items()}
                else:
                    raise ValueError("不支持的 corrections 格式")
            asr_state["count"] = len(asr_state["corrections"])
            return _mock_get_asr()

        fake_asr = types.ModuleType("asr_corrections")
        fake_asr.get_asr_corrections = _mock_get_asr
        fake_asr.save_asr_corrections = _mock_save_asr

        try:
            with (
                patch.dict(sys.modules, {"anima_memory_provider": fake_amp, "asr_corrections": fake_asr}),
                patch.object(rs, "get_vad_speculative", _mock_get_vad, create=True),
                patch.object(rs, "set_vad_speculative", _mock_set_vad, create=True),
            ):
                tok = cs.get_console_token()
                auth_hdrs = _post_headers(port, token=tok)
                no_tok_hdrs = _post_headers(port, token=None)

                with httpx.Client(timeout=4.0, trust_env=False) as client:
                    # 1. GET /api/state exposes anima_outbox (without db_path), vad_speculative, asr_corrections
                    r_st = client.get(f"http://127.0.0.1:{port}/api/state")
                    self.assertEqual(r_st.status_code, 200)
                    cfg = r_st.json()["config"]
                    self.assertEqual(cfg["anima_outbox"]["pending"], 1)
                    self.assertEqual(cfg["anima_outbox"]["dead"], 2)
                    self.assertNotIn("db_path", cfg["anima_outbox"])
                    self.assertEqual(cfg["vad_speculative"]["speculative_ms"], 550)
                    self.assertEqual(cfg["asr_corrections"]["corrections"]["牙医"], "芽衣")

                    # 2. ALL 3 new POST endpoints MUST enforce X-Console-Token (401 without token)
                    for ep, body in (
                        ("/api/anima-outbox/replay-dead", {}),
                        ("/api/vad-speculative", {"enabled": False}),
                        ("/api/asr-corrections", {"enabled": False}),
                    ):
                        r_unauth = client.post(f"http://127.0.0.1:{port}{ep}", json=body, headers=no_tok_hdrs)
                        self.assertEqual(r_unauth.status_code, 401, f"{ep} must require X-Console-Token")

                    # 3. B-5 GET & POST /api/anima-outbox/replay-dead
                    r_ob_get = client.get(f"http://127.0.0.1:{port}/api/anima-outbox")
                    self.assertEqual(r_ob_get.status_code, 200)
                    self.assertEqual(r_ob_get.json()["outbox"]["dead"], 2)
                    self.assertNotIn("db_path", r_ob_get.json()["outbox"])

                    r_ob_bad = client.post(
                        f"http://127.0.0.1:{port}/api/anima-outbox/replay-dead",
                        json={"limit": -5},
                        headers=auth_hdrs,
                    )
                    self.assertEqual(r_ob_bad.status_code, 400)

                    r_ob_rep = client.post(
                        f"http://127.0.0.1:{port}/api/anima-outbox/replay-dead",
                        json={},
                        headers=auth_hdrs,
                    )
                    self.assertEqual(r_ob_rep.status_code, 200)
                    self.assertTrue(r_ob_rep.json()["ok"])
                    self.assertEqual(r_ob_rep.json()["replayed"], 2)
                    self.assertEqual(r_ob_rep.json()["outbox"]["dead"], 0)
                    self.assertEqual(r_ob_rep.json()["outbox"]["pending"], 3)

                    # 4. F-1 GET & POST /api/vad-speculative
                    r_vs_bad = client.post(
                        f"http://127.0.0.1:{port}/api/vad-speculative",
                        json={"speculative_ms": 100},
                        headers=auth_hdrs,
                    )
                    self.assertEqual(r_vs_bad.status_code, 400)

                    r_vs_bad_bool = client.post(
                        f"http://127.0.0.1:{port}/api/vad-speculative",
                        json={"enabled": "yes"},
                        headers=auth_hdrs,
                    )
                    self.assertEqual(r_vs_bad_bool.status_code, 400)

                    r_vs_ok = client.post(
                        f"http://127.0.0.1:{port}/api/vad-speculative",
                        json={
                            "enabled": False,
                            "speculative_ms": 600,
                            "min_chars": 5,
                            "complete_suffixes": ["吗", "吧", "了"],
                            "hesitation_suffixes": ["然后", "嗯"],
                        },
                        headers=auth_hdrs,
                    )
                    self.assertEqual(r_vs_ok.status_code, 200)
                    self.assertFalse(r_vs_ok.json()["vad_speculative"]["enabled"])
                    self.assertEqual(r_vs_ok.json()["vad_speculative"]["speculative_ms"], 600)
                    self.assertEqual(r_vs_ok.json()["vad_speculative"]["min_chars"], 5)

                    # 5. G-4 GET & POST /api/asr-corrections
                    r_ac_bad = client.post(
                        f"http://127.0.0.1:{port}/api/asr-corrections",
                        json={"corrections": "bad_line_without_arrow"},
                        headers=auth_hdrs,
                    )
                    self.assertEqual(r_ac_bad.status_code, 400)

                    r_ac_ok = client.post(
                        f"http://127.0.0.1:{port}/api/asr-corrections",
                        json={"enabled": True, "corrections": "雷电牙医->雷电芽衣\n牙医->芽衣\n小纸->小智"},
                        headers=auth_hdrs,
                    )
                    self.assertEqual(r_ac_ok.status_code, 200)
                    self.assertEqual(r_ac_ok.json()["asr_corrections"]["count"], 3)
                    self.assertEqual(r_ac_ok.json()["asr_corrections"]["corrections"]["小纸"], "小智")
        finally:
            cs.CONSOLE_PORT = orig_port
            httpd.shutdown()
            httpd.server_close()

        # 6. Verify console/static/{index.html,app.js} DOM IDs, API routes, and consolePost usage (X-Console-Token)
        with open(cs.HTML_PATH, "r", encoding="utf-8") as f:
            html_src = f.read()
        with open(cs.JS_PATH, "r", encoding="utf-8") as f:
            js_src = f.read()
        self.assertIn('href="/static/app.css"', html_src)
        self.assertIn('src="/static/app.js"', html_src)
        for el_id in (
            "animaOutboxBadge",
            "animaReplayDeadBtn",
            "animaOutboxPendingText",
            "animaOutboxDeadText",
            "vadSpecBadge",
            "vadSpecToggle",
            "vadSpecMsRange",
            "vadPostWakeupMsRange",
            "vadMinCharsRange",
            "vadCompleteSuffixesInput",
            "vadHesitationSuffixesInput",
            "vadSpecSaveBtn",
            "asrCorrBadge",
            "asrCorrToggle",
            "asrCorrInput",
            "asrCorrSaveBtn",
        ):
            self.assertIn(f'id="{el_id}"', html_src, f"Missing DOM element {el_id} in index.html")
        self.assertIn('consolePost("/api/anima-outbox/replay-dead"', js_src)
        self.assertIn('consolePost("/api/vad-speculative"', js_src)
        self.assertIn('consolePost("/api/asr-corrections"', js_src)
        self.assertIn('Object.prototype.hasOwnProperty.call(payload, "corrections")', js_src)
        self.assertIn("if (savedCorrections)", js_src)

        # Verify every getElementById("...") in static/app.js exists in static/index.html
        all_ids = set(re.findall(r'\bid="([^"]+)"', html_src))
        script_refs = re.findall(r'getElementById\("([^"]+)"\)', js_src)
        self.assertGreater(len(script_refs), 20)
        missing_refs = [r for r in script_refs if r not in all_ids]
        self.assertEqual(missing_refs, [], f"Missing DOM IDs referenced in JS: {missing_refs}")

    def test_b5_f1_g4_real_module_integration_isolated_tempfiles(self):
        """Real cross-module integration via 18080 HTTP against actual anima_memory_provider, runtime_settings, and asr_corrections."""
        import anima_memory_provider as amp
        import asr_corrections as ac

        if not (hasattr(amp, "replay_dead_outbox_turns") and hasattr(rs, "set_vad_speculative")):
            self.skipTest("Sibling Batch 3 modules not loaded")

        httpd, port = _start_test_console_server()
        orig_port = cs.CONSOLE_PORT
        cs.CONSOLE_PORT = port
        with tempfile.TemporaryDirectory() as td:
            ob_db = os.path.join(td, "real_outbox.sqlite")
            rs_path = os.path.join(td, "real_runtime_settings.json")
            asr_path = os.path.join(td, "real_asr_corrections.json")

            real_ob = amp.AnimaOutbox(
                ob_db,
                service_url_getter=lambda: "http://127.0.0.1:1",
                character_id_getter=lambda: "xiaozhi",
                token_getter=lambda: "tok-test-12345678901234567890",
                client_factory=lambda: httpx.Client(
                    transport=httpx.MockTransport(lambda req: httpx.Response(400, json={"error": "bad"})),
                    trust_env=False,
                ),
                auto_start=False,
            )
            try:
                real_ob.enqueue([{"request_id": "xz-real-dead-1", "user_message": "测试", "assistant_reply": "回复"}])
                real_ob.drain_once(force_due=True)
                self.assertEqual(real_ob.stats()["dead"], 1)

                tok = cs.get_console_token()
                auth_hdrs = _post_headers(port, token=tok)
                with (
                    patch.object(amp, "get_default_outbox", return_value=real_ob),
                    patch.object(rs, "RUNTIME_SETTINGS_PATH", rs_path),
                    patch.object(ac, "ASR_CORRECTIONS_PATH", asr_path),
                    httpx.Client(timeout=4.0, trust_env=False) as client,
                ):
                    # B-5 real outbox stats & replay dead
                    r_ob = client.get(f"http://127.0.0.1:{port}/api/anima-outbox")
                    self.assertEqual(r_ob.status_code, 200)
                    self.assertEqual(r_ob.json()["outbox"]["dead"], 1)
                    self.assertNotIn("db_path", r_ob.json()["outbox"])

                    r_rep = client.post(
                        f"http://127.0.0.1:{port}/api/anima-outbox/replay-dead",
                        json={},
                        headers=auth_hdrs,
                    )
                    self.assertEqual(r_rep.status_code, 200)
                    self.assertEqual(r_rep.json()["replayed"], 1)
                    self.assertEqual(r_rep.json()["outbox"]["dead"], 0)
                    self.assertEqual(r_rep.json()["outbox"]["pending"], 1)

                    # F-1 real runtime_settings get & post
                    r_vs = client.post(
                        f"http://127.0.0.1:{port}/api/vad-speculative",
                        json={
                            "enabled": True,
                            "speculative_ms": 625,
                            "post_wakeup_ms": 850,
                            "min_chars": 5,
                            "complete_suffixes": "吗, 呢, 吧",
                            "hesitation_suffixes": "然后, 就是",
                        },
                        headers=auth_hdrs,
                    )
                    self.assertEqual(r_vs.status_code, 200)
                    vs_saved = r_vs.json()["vad_speculative"]
                    self.assertEqual(vs_saved["speculative_ms"], 625)
                    self.assertEqual(vs_saved["post_wakeup_ms"], 850)
                    self.assertEqual(vs_saved["min_chars"], 5)
                    self.assertIn("吗", vs_saved["complete_suffixes"])

                    # G-4 real asr_corrections get & post + verify apply_asr_corrections
                    r_ac = client.post(
                        f"http://127.0.0.1:{port}/api/asr-corrections",
                        json={"enabled": True, "corrections": "雷电牙医->雷电芽衣\n牙医->芽衣"},
                        headers=auth_hdrs,
                    )
                    self.assertEqual(r_ac.status_code, 200)
                    self.assertEqual(r_ac.json()["asr_corrections"]["count"], 2)
                    self.assertEqual(ac.apply_asr_corrections("你好雷电牙医", path=asr_path), "你好雷电芽衣")

                    # Browser favicon request must return 204 (zero 404 noise in DevTools console)
                    r_fav = client.get(f"http://127.0.0.1:{port}/favicon.ico")
                    self.assertEqual(r_fav.status_code, 204)
            finally:
                real_ob.close()
                cs.CONSOLE_PORT = orig_port
                httpd.shutdown()
                httpd.server_close()

    def test_h6_gsv_log_rotation_script_hygiene(self):
        """H-6: Verify data/start_all.ps1 retains UTF-8 BOM + CRLF and configures GSV 5MB x 3 rotation."""
        ps1_path = os.path.join(HOST_SCRIPTS_DIR, "start_all.ps1")
        if not os.path.isfile(ps1_path):
            self.skipTest("Host data/start_all.ps1 not mounted inside container")
        with open(ps1_path, "rb") as bf:
            raw = bf.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "start_all.ps1 must have UTF-8 BOM")
        self.assertEqual(raw.count(b"\n") - raw.count(b"\r\n"), 0, "start_all.ps1 must use CRLF")
        txt = raw.decode("utf-8-sig")
        self.assertIn("--log_file", txt)
        self.assertIn("5242880", txt)
        self.assertIn("--log_backup_count 3", txt)
        self.assertIn("gsv_19880_boot.log", txt)
        self.assertNotIn('> `"$logOut`"', txt, "cmd.exe must not hold > redirect on rotating gsv_19880.log")

    def test_p1_1_console_cors_port_restriction_and_malformed_headers_safety(self):
        """P1-1: 18080 CORS reflects ONLY exact http loopback origin matching CONSOLE_PORT, and tolerates headers=None."""
        httpd, port = _start_test_console_server()
        orig_port = cs.CONSOLE_PORT
        cs.CONSOLE_PORT = port
        try:
            with httpx.Client(trust_env=False, timeout=5.0) as client:
                # 1. Cross-port / https / external origins MUST NOT receive Access-Control-Allow-Origin
                for bad_origin in (
                    "http://localhost:3000",
                    "http://127.0.0.1:8888",
                    f"https://localhost:{port}",
                    f"https://127.0.0.1:{port}",
                    "http://evil.example",
                    "null",
                ):
                    r_bad = client.get(
                        f"http://127.0.0.1:{port}/api/health",
                        headers={"Origin": bad_origin},
                    )
                    self.assertEqual(r_bad.status_code, 200)
                    self.assertNotIn(
                        "access-control-allow-origin",
                        {k.lower() for k in r_bad.headers.keys()},
                        f"Cross-port/scheme Origin {bad_origin!r} must NOT be reflected in ACAO",
                    )

                # 2. Exact matching http loopback origin on CONSOLE_PORT IS allowed
                good_origin = f"http://127.0.0.1:{port}"
                r_good = client.get(
                    f"http://127.0.0.1:{port}/api/health",
                    headers={"Origin": good_origin},
                )
                self.assertEqual(r_good.status_code, 200)
                self.assertEqual(r_good.headers.get("Access-Control-Allow-Origin"), good_origin)
                self.assertEqual(r_good.headers.get("Vary"), "Origin")

            # 3. Malformed HTTP request (self.headers not initialized / None) must NOT raise AttributeError
            sent_hdrs: list[tuple[str, str]] = []

            class _BrokenHandler(cs.ConsoleHandler):
                def __init__(self):
                    self.headers = None
                    self._headers_buffer = []

                def send_header(self, keyword, value):
                    sent_hdrs.append((keyword, value))

            bh = _BrokenHandler()
            with patch.object(cs.BaseHTTPRequestHandler, "end_headers", lambda self: None):
                bh.end_headers()
            hdr_names = {k.lower() for k, _ in sent_hdrs}
            self.assertIn("x-frame-options", hdr_names)
            self.assertIn("content-security-policy", hdr_names)
            self.assertIn("x-content-type-options", hdr_names)
            self.assertNotIn("access-control-allow-origin", hdr_names)
        finally:
            cs.CONSOLE_PORT = orig_port
            httpd.shutdown()
            httpd.server_close()

    def test_p1_5_gsv_preview_rereads_configured_voice_in_lock_and_finally(self):
        """P1-5: handle_post_tts_preview re-reads configured GSV voice after acquiring lock and before rollback."""
        import console.routes.gsv as gsv_mod

        orig_loaded = gsv_mod._active_gsv_loaded
        gsv_mod._set_voice_dirty(None, "")
        try:
            # Case A: Concurrent POST /api/config switches configured voice from raiden_mei -> xiaozhi_tw
            # WHILE preview is synthesizing xiaozhi_tw. Preview MUST NOT roll back GPU to old raiden_mei!
            cfg_reads = {"n": 0}
            ensure_calls: list[str] = []

            def _changing_cfg():
                cfg_reads["n"] += 1
                # Read 1 (before lock): raiden_mei; Read 2 (after lock): raiden_mei; Read 3 (in finally): xiaozhi_tw!
                return "__gsv_raiden_mei__" if cfg_reads["n"] <= 2 else "__gsv_xiaozhi_tw__"

            def _fake_ensure(v, persist_boot_yaml=False):
                ensure_calls.append(v)
                gsv_mod._active_gsv_loaded = v

            class _FakeResp:
                status_code = 200
                content = b"RIFFfakewav"

            class _FakeCli:
                def post(self, url, **kwargs):
                    return _FakeResp()

            class _FakeHandler:
                def __init__(self, body):
                    self._body = body
                    self.status = None
                    self.headers_sent = {}
                    self.wfile = io.BytesIO()

                def _read_body_json(self):
                    return self._body

                def _send_json(self, data, status=200):
                    self.status = int(status)
                    self.wfile.write(json.dumps(data).encode("utf-8"))

                def send_response(self, status):
                    self.status = int(status)

                def send_header(self, k, v):
                    self.headers_sent[k] = v

                def end_headers(self):
                    pass

            gsv_mod._active_gsv_loaded = "__gsv_raiden_mei__"
            h1 = _FakeHandler({"voice": "__gsv_xiaozhi_tw__", "text": "测试切换"})
            with (
                patch.object(gsv_mod, "_get_configured_gsv_voice", side_effect=_changing_cfg),
                patch.object(gsv_mod, "_ensure_gsv_weights", side_effect=_fake_ensure),
                patch.object(gsv_mod.shared, "_is_device_busy", return_value=False),
                patch.object(gsv_mod.shared, "_get_console_http_client", return_value=_FakeCli()),
            ):
                gsv_mod.handle_post_tts_preview(h1)

            self.assertEqual(h1.status, 200)
            self.assertEqual(
                ensure_calls,
                ["__gsv_xiaozhi_tw__"],
                f"Must NOT revert GPU back to stale __gsv_raiden_mei__ after config switched to __gsv_xiaozhi_tw__: {ensure_calls}",
            )
            self.assertEqual(gsv_mod._active_gsv_loaded, "__gsv_xiaozhi_tw__")

            # Case B: Config switches from raiden_mei -> xiaozhi_tw while previewing raiden_mei:
            # finally MUST restore NEW configured voice (__gsv_xiaozhi_tw__) instead of leaving raiden_mei loaded!
            cfg_reads["n"] = 0
            ensure_calls.clear()
            gsv_mod._active_gsv_loaded = "__gsv_raiden_mei__"
            h2 = _FakeHandler({"voice": "__gsv_raiden_mei__", "text": "试听旧音色"})
            with (
                patch.object(gsv_mod, "_get_configured_gsv_voice", side_effect=_changing_cfg),
                patch.object(gsv_mod, "_ensure_gsv_weights", side_effect=_fake_ensure),
                patch.object(gsv_mod.shared, "_is_device_busy", return_value=False),
                patch.object(gsv_mod.shared, "_get_console_http_client", return_value=_FakeCli()),
            ):
                gsv_mod.handle_post_tts_preview(h2)

            self.assertEqual(h2.status, 200)
            self.assertEqual(
                ensure_calls,
                ["__gsv_raiden_mei__", "__gsv_xiaozhi_tw__"],
                f"Expected rollback to newly configured __gsv_xiaozhi_tw__, got: {ensure_calls}",
            )
            self.assertEqual(gsv_mod._active_gsv_loaded, "__gsv_xiaozhi_tw__")

            # Case C: config._write_updated_yaml keeps gsv._gsv_weights_lock held across atomic .config.yaml write
            # so a concurrent preview thread can NEVER observe pre-write YAML after GPU weights already switched.
            import console.routes.config as cfg_mod

            orig_cfg_path = cs.CONFIG_PATH
            orig_infer_path = cs.GSV_INFER_YAML
            lock_held_at_write = {"held": False}
            with tempfile.TemporaryDirectory() as td_p15:
                tmp_cfg = os.path.join(td_p15, ".config.yaml")
                tmp_infer = os.path.join(td_p15, "gsv_infer.yaml")
                with open(tmp_cfg, "w", encoding="utf-8") as wf:
                    yaml.safe_dump(
                        {
                            "selected_module": {"TTS": "GPT_SOVITS_V2", "LLM": "DeepSeekLLM"},
                            "TTS": {"GPT_SOVITS_V2": dict(gsv_mod.GSV_PROFILES["__gsv_raiden_mei__"]["block"])},
                            "LLM": {"DeepSeekLLM": {"model_name": "deepseek-chat"}},
                        },
                        wf,
                        allow_unicode=True,
                    )
                cs.CONFIG_PATH = tmp_cfg
                cs.GSV_INFER_YAML = tmp_infer
                orig_atomic = cs.shared._atomic_write_text

                def _spy_atomic(path, content):
                    if path == tmp_cfg:
                        # From another thread, non-blocking acquire of _gsv_weights_lock MUST fail (lock is still held!)
                        other_res = []

                        def _probe():
                            got = gsv_mod._gsv_weights_lock.acquire(blocking=False)
                            if got:
                                gsv_mod._gsv_weights_lock.release()
                            other_res.append(got)

                        t_probe = threading.Thread(target=_probe)
                        t_probe.start()
                        t_probe.join(timeout=2.0)
                        lock_held_at_write["held"] = (other_res == [False])
                    return orig_atomic(path, content)

                try:
                    with (
                        patch.object(gsv_mod, "_ensure_gsv_weights", side_effect=_fake_ensure),
                        patch.object(cs.shared, "_atomic_write_text", side_effect=_spy_atomic),
                    ):
                        cfg_mod._write_updated_yaml(voice="__gsv_xiaozhi_tw__")
                    self.assertTrue(
                        lock_held_at_write["held"],
                        "gsv._gsv_weights_lock MUST stay held during atomic .config.yaml write to close TOCTOU!",
                    )
                finally:
                    cs.CONFIG_PATH = orig_cfg_path
                    cs.GSV_INFER_YAML = orig_infer_path

            # Case D: P0-4 18080 POST /api/pc-mode propagates host 18900 HTTP 423 Locked cleanly (not HTTP 502)
            import console.routes.l3 as l3_mod

            class _Fake423Resp:
                status_code = 423
                content = b'{"ok":false,"emergency_stopped":true,"error":"\xe6\x80\xa5\xe5\x81\x9c\xe9\x94\x81\xe5\xae\x9a"}'
                text = '{"ok":false,"emergency_stopped":true,"error":"急停锁定"}'

                def json(self):
                    return {"ok": False, "emergency_stopped": True, "error": "急停锁定"}

            class _Fake423Cli:
                def post(self, url, **kwargs):
                    return _Fake423Resp()

            h423 = _FakeHandler({"mode": "full"})
            orig_mode_path = l3_mod.shared.MODE_PATH
            l3_mod.shared.MODE_PATH = l3_mod.shared._DEFAULT_MODE_PATH
            try:
                with patch.object(l3_mod.shared, "_get_console_http_client", return_value=_Fake423Cli()):
                    l3_mod.handle_post_pc_mode(h423)
            finally:
                l3_mod.shared.MODE_PATH = orig_mode_path
            self.assertEqual(h423.status, 423)
            body_423 = json.loads(h423.wfile.getvalue().decode("utf-8"))
            self.assertTrue(body_423.get("emergency_stopped"))
            self.assertEqual(body_423.get("error"), "急停锁定")
        finally:
            gsv_mod._active_gsv_loaded = orig_loaded
            gsv_mod._set_voice_dirty(None, "")

    def test_p2_console_hardening_allowlists_redaction_boot_id_lock_and_facade_broadcast(self):
        """4.1 #2 & H-1: malformed headers, /api/relationship allowlist, /api/audio-test volume, boot_id/app.js refresh, redaction, facade broadcast."""
        import io
        import math
        import console.routes.anima as anima_mod
        import console.routes.config as cfg_mod
        import console.routes.gsv as gsv_mod
        import console.routes.state as state_mod
        import console.server as srv_mod
        import console.shared as shared_mod
        import console_server as cs
        import start_with_env as swe
        import boot as boot_mod
        from patches import asr as asr_patch_mod

        class _FakeH:
            def __init__(self, body_dict=None):
                self._body = body_dict or {}
                self.status = None
                self.wfile = io.BytesIO()
                self.sent_headers = []

            def _read_body_json(self):
                return self._body

            def _send_json(self, obj, status=200):
                self.status = int(status)
                raw = json.dumps(shared_mod._redact_json_payload(obj), ensure_ascii=False).encode("utf-8")
                self.wfile = io.BytesIO(raw)

            def send_header(self, k, v):
                self.sent_headers.append((k, v))

        # 1. Malformed HTTP request before self.headers exists must NOT raise AttributeError in end_headers()
        raw_handler = srv_mod.ConsoleHandler.__new__(srv_mod.ConsoleHandler)
        sent_hdrs = []
        raw_handler.send_header = lambda k, v: sent_hdrs.append((k, v))
        with patch("http.server.BaseHTTPRequestHandler.end_headers", return_value=None):
            raw_handler.end_headers()
        hdr_keys = {k.lower() for k, _ in sent_hdrs}
        self.assertIn("x-frame-options", hdr_keys)
        self.assertIn("content-security-policy", hdr_keys)
        self.assertIn("x-content-type-options", hdr_keys)

        # 2. /api/relationship rejects unknown fields, character_id override, invalid presets & out-of-range axes with 400
        h_rel_bad = _FakeH({"character_id": "hijacked_char", "preset": "confidant"})
        anima_mod.handle_post_relationship(h_rel_bad)
        self.assertEqual(h_rel_bad.status, 400)

        h_rel_nan = _FakeH({"warmth": float("nan")})
        anima_mod.handle_post_relationship(h_rel_nan)
        self.assertEqual(h_rel_nan.status, 400)

        h_rel_neg = _FakeH({"familiarity": -0.5})
        anima_mod.handle_post_relationship(h_rel_neg)
        self.assertEqual(h_rel_neg.status, 400)

        h_rel_bad_preset = _FakeH({"preset": "totally_invalid_junk_preset"})
        anima_mod.handle_post_relationship(h_rel_bad_preset)
        self.assertEqual(h_rel_bad_preset.status, 400)

        posted_rel = []

        class _FakeRelResp:
            status_code = 200

            def json(self):
                return {"character_id": "xiaozhi", "preset": "confidant"}

        class _FakeRelCli:
            def post(self, url, **kwargs):
                posted_rel.append(kwargs.get("json"))
                return _FakeRelResp()

        h_rel_ok = _FakeH({"preset": "confidant", "warmth": 0.6})
        with (
            patch.object(anima_mod, "_read_anima_token", return_value="tok-test-123456"),
            patch.object(anima_mod, "_get_anima_character_id", return_value="xiaozhi"),
            patch.object(anima_mod.shared, "_get_console_http_client", return_value=_FakeRelCli()),
        ):
            anima_mod.handle_post_relationship(h_rel_ok)
        self.assertEqual(h_rel_ok.status, 200)
        self.assertEqual(posted_rel[0]["character_id"], "xiaozhi")
        self.assertEqual(posted_rel[0]["preset"], "confidant")
        self.assertEqual(posted_rel[0]["warmth"], 0.6)

        # 3. /api/audio-test rejects NaN, Inf, bool, string, and out-of-range volume with 400
        for bad_vol in (float("nan"), float("inf"), -0.1, 1.5, True, "loud"):
            h_vol = _FakeH({"volume": bad_vol})
            gsv_mod.handle_post_audio_test(h_vol)
            self.assertEqual(h_vol.status, 400, f"volume={bad_vol!r} must be rejected with 400")

        # 4. Expanded secret redaction covers Token auth, auth_key, PC_MCP_TOKEN, ANIMA_STUDIO_TOKEN, URL creds, degraded_reasons & security_failures
        samples = [
            ("Authorization: Token mysecretToken999", "mysecretToken999"),
            ("auth_key=super_secret_esp32_key", "super_secret_esp32_key"),
            ("PC_MCP_TOKEN=pc_secret_987654", "pc_secret_987654"),
            ("ANIMA_STUDIO_TOKEN=anima_secret_654321", "anima_secret_654321"),
            ("https://alice:topsecretpass@example.com/api", "topsecretpass"),
        ]
        for raw_s, leak in samples:
            red = shared_mod._redact_secrets(raw_s)
            self.assertNotIn(leak, red, f"Secret leaked in redacted output: {red!r}")

        red_json = shared_mod._redact_json_payload(
            {
                "ok": False,
                "degraded_reasons": ["failed auth_key=leak_key_123"],
                "security_failures": ["Bearer leak_tok_456"],
            }
        )
        self.assertNotIn("leak_key_123", json.dumps(red_json))
        self.assertNotIn("leak_tok_456", json.dumps(red_json))

        # 5. boot_id exposed in /api/health, and app.js implements refreshConsoleToken + boot_id restart tracking
        h_hlth = _FakeH()
        state_mod.handle_get_health(h_hlth)
        hlth_body = json.loads(h_hlth.wfile.getvalue().decode("utf-8"))
        self.assertEqual(hlth_body.get("boot_id"), shared_mod.get_console_boot_id())
        with open(os.path.join(os.path.dirname(srv_mod.__file__), "static", "app.js"), encoding="utf-8") as _f:
            app_js = _f.read()
        self.assertIn("refreshConsoleToken", app_js)
        self.assertIn("restartTargetBootId", app_js)
        self.assertIn("data.boot_id", app_js)
        self.assertNotIn('fetch("/api/tts-preview"', app_js, "All /api/tts-preview POSTs must go through consolePost() for 401 token refresh")

        # 6. handle_post_config holds _config_write_lock during _hot_reload_runtime_config and exposes boot_id
        lock_held_in_reload = {"held": False}

        def _spy_reload(changed):
            other_res = []

            def _probe():
                got = cfg_mod._config_write_lock.acquire(blocking=False)
                if got:
                    cfg_mod._config_write_lock.release()
                other_res.append(got)

            t = threading.Thread(target=_probe)
            t.start()
            t.join(timeout=2.0)
            lock_held_in_reload["held"] = (other_res == [False])
            return True, "hot_reloaded"

        h_cfg = _FakeH({"prompt": "新角色人设测试", "restart": False})
        with (
            patch.object(cfg_mod, "_write_updated_yaml", return_value={"changed_fields": ["prompt"], "written": True, "voice_recovered": False}),
            patch.object(cfg_mod, "_hot_reload_runtime_config", side_effect=_spy_reload),
        ):
            cfg_mod.handle_post_config(h_cfg, shutdown_flush_fn=lambda: None)
        self.assertTrue(lock_held_in_reload["held"], "_config_write_lock must remain held during _hot_reload_runtime_config")
        cfg_body = json.loads(h_cfg.wfile.getvalue().decode("utf-8"))
        self.assertEqual(cfg_body.get("boot_id"), shared_mod.get_console_boot_id())

        # 7. H-1 facade broadcast setattr syncs ALL submodules holding the same attribute (e.g. start_with_env.ROOT_DIR)
        orig_root = boot_mod.ROOT_DIR
        try:
            with patch.object(swe, "ROOT_DIR", "/tmp/h1_broadcast_test"):
                self.assertEqual(boot_mod.ROOT_DIR, "/tmp/h1_broadcast_test")
                self.assertEqual(asr_patch_mod.ROOT_DIR, "/tmp/h1_broadcast_test")
        finally:
            self.assertEqual(boot_mod.ROOT_DIR, orig_root)
            self.assertEqual(asr_patch_mod.ROOT_DIR, orig_root)


if __name__ == "__main__":
    unittest.main(verbosity=2)

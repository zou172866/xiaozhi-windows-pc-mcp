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

                # 2b. P2-5: OPTIONS CORS preflight rejects non-allowlisted Origin, internal/evil Host & cross-site,
                #     and returns 204 No Content with CORS allow headers for exact loopback Origin
                for bad_origin in (
                    "http://localhost:3000",
                    "http://127.0.0.1:8888",
                    f"https://localhost:{port}",
                    f"https://127.0.0.1:{port}",
                    "http://evil.example",
                    "null",
                    "",
                ):
                    r_opt_bad = client.options(
                        f"http://127.0.0.1:{port}/api/config",
                        headers={
                            "Origin": bad_origin,
                            "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "content-type, x-console-token",
                        },
                    )
                    self.assertEqual(r_opt_bad.status_code, 403, f"Bad Origin {bad_origin!r} OPTIONS must be 403")
                    self.assertNotIn(
                        "access-control-allow-origin",
                        {k.lower() for k in r_opt_bad.headers.keys()},
                    )

                r_opt_internal = client.options(
                    f"http://127.0.0.1:{port}/api/health",
                    headers={"Host": f"xiaozhi-esp32-server:{port}", "Origin": good_origin},
                )
                self.assertEqual(r_opt_internal.status_code, 403)
                self.assertNotIn(
                    "access-control-allow-origin",
                    {k.lower() for k in r_opt_internal.headers.keys()},
                )

                r_opt_evil_host = client.options(
                    f"http://127.0.0.1:{port}/api/config",
                    headers={"Host": f"evil.example:{port}", "Origin": good_origin},
                )
                self.assertEqual(r_opt_evil_host.status_code, 421)
                self.assertNotIn(
                    "access-control-allow-origin",
                    {k.lower() for k in r_opt_evil_host.headers.keys()},
                )

                r_opt_cross_site = client.options(
                    f"http://127.0.0.1:{port}/api/config",
                    headers={"Origin": good_origin, "Sec-Fetch-Site": "cross-site"},
                )
                self.assertEqual(r_opt_cross_site.status_code, 403)

                # Subsequent valid OPTIONS on the same keep-alive client must succeed with 204 No Content
                for opt_path in ("/api/config", "/api/persona/history", "/api/dream"):
                    r_opt_ok = client.options(
                        f"http://127.0.0.1:{port}{opt_path}",
                        headers={
                            "Origin": good_origin,
                            "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "content-type, x-console-token",
                        },
                    )
                    self.assertEqual(r_opt_ok.status_code, 204)
                    self.assertEqual(r_opt_ok.content, b"")
                    self.assertEqual(r_opt_ok.headers.get("Access-Control-Allow-Origin"), good_origin)
                    self.assertEqual(r_opt_ok.headers.get("Access-Control-Allow-Methods"), "GET, POST, OPTIONS")
                    self.assertEqual(
                        r_opt_ok.headers.get("Access-Control-Allow-Headers"),
                        "Content-Type, X-Console-Token",
                    )
                    self.assertEqual(r_opt_ok.headers.get("Allow"), "GET, POST, OPTIONS")

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

    def test_p2_5_console_options_smuggling_and_fail_closed_cors_guards(self):
        """P2-5 hardening: OPTIONS *, disallowed ACRM, duplicate header smuggling, and 501 fail-closed CORS."""
        import socket as _sock

        httpd, port = _start_test_console_server()
        orig_port = cs.CONSOLE_PORT
        cs.CONSOLE_PORT = port

        def _raw_http_request(raw_req: bytes) -> tuple[int, dict[str, str], bytes]:
            with _sock.create_connection(("127.0.0.1", port), timeout=5.0) as sock_obj:
                sock_obj.sendall(raw_req)
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = sock_obj.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                head_bytes, body_rem = buf.split(b"\r\n\r\n", 1)
                lines = head_bytes.decode("iso-8859-1").split("\r\n")
                status_code = int(lines[0].split()[1])
                hdr_map: dict[str, str] = {}
                for ln in lines[1:]:
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        hdr_map[k.strip().lower()] = v.strip()
                if "content-length" in hdr_map:
                    clen = int(hdr_map["content-length"])
                    while len(body_rem) < clen:
                        chunk = sock_obj.recv(4096)
                        if not chunk:
                            break
                        body_rem += chunk
                    body_rem = body_rem[:clen]
                return status_code, hdr_map, body_rem

        try:
            good_origin = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False, timeout=5.0) as client:
                # 1. Disallowed or empty Access-Control-Request-Method on OPTIONS must be 405 without ACAO
                for bad_acrm in ("DELETE", "PUT", "PATCH", ""):
                    r_bad_m = client.options(
                        f"http://127.0.0.1:{port}/api/config",
                        headers={
                            "Origin": good_origin,
                            "Access-Control-Request-Method": bad_acrm,
                        },
                    )
                    self.assertEqual(r_bad_m.status_code, 405, f"ACRM={bad_acrm!r} must be rejected with 405")
                    self.assertNotIn("access-control-allow-origin", {k.lower() for k in r_bad_m.headers.keys()})

            # 2a. Valid GET sets _security_allowed only for that response; unvalidated 501 DELETE with evil/internal
            #     Host (both standalone and when reusing a handler instance) must NOT reflect Access-Control-Allow-Origin
            st1, h1, _ = _raw_http_request(
                f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: {good_origin}\r\n\r\n".encode()
            )
            self.assertEqual(st1, 200)
            self.assertEqual(h1.get("access-control-allow-origin"), good_origin)

            for bad_host in (f"evil.example:{port}", f"xiaozhi-esp32-server:{port}"):
                st_del, h_del, _ = _raw_http_request(
                    f"DELETE /api/config HTTP/1.1\r\nHost: {bad_host}\r\nOrigin: {good_origin}\r\n\r\n".encode()
                )
                self.assertEqual(st_del, 501)
                self.assertNotIn("access-control-allow-origin", h_del)

            # Verify ConsoleHandler.end_headers() explicitly clears _security_allowed after one response
            reused_hdrs: list[list[tuple[str, str]]] = []

            class _KeepAliveStub(cs.ConsoleHandler):
                def __init__(self):
                    self.headers = {"Origin": good_origin}
                    self._headers_buffer = []
                    self._security_allowed = True

                def send_header(self, keyword, value):
                    reused_hdrs[-1].append((keyword, value))

            ka = _KeepAliveStub()
            with patch.object(cs.BaseHTTPRequestHandler, "end_headers", lambda self: None):
                reused_hdrs.append([])
                ka.end_headers()
                reused_hdrs.append([])
                ka.end_headers()
            self.assertIn("access-control-allow-origin", {k.lower() for k, _ in reused_hdrs[0]})
            self.assertFalse(ka._security_allowed)
            self.assertNotIn("access-control-allow-origin", {k.lower() for k, _ in reused_hdrs[1]})

            # 2b. OPTIONS * asterisk-form target: 204 without Origin, 403 for internal Host
            st4, h4, b4 = _raw_http_request(
                f"OPTIONS * HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
            )
            self.assertEqual(st4, 204)
            self.assertEqual(b4, b"")
            self.assertEqual(h4.get("allow"), "GET, POST, OPTIONS")
            self.assertNotIn("access-control-allow-origin", h4)

            st5, h5, _ = _raw_http_request(
                f"OPTIONS * HTTP/1.1\r\nHost: xiaozhi-esp32-server:{port}\r\n\r\n".encode()
            )
            self.assertEqual(st5, 403)
            self.assertNotIn("access-control-allow-origin", h5)

            # 2c. Duplicate Host header smuggling MUST be rejected with 400 (never serving / HTML token)
            st6, h6, b6 = _raw_http_request(
                f"GET / HTTP/1.1\r\nHost: xiaozhi-esp32-server:{port}\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
            )
            self.assertEqual(st6, 400)
            self.assertNotIn(cs.get_console_token().encode("utf-8"), b6)
            self.assertNotIn("access-control-allow-origin", h6)

            # 2d. Duplicate Origin (both orders) and duplicate Sec-Fetch-Site MUST be rejected with 403
            for dup_req in (
                (
                    f"OPTIONS /api/config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Origin: https://evil.example\r\nOrigin: {good_origin}\r\n"
                    f"Access-Control-Request-Method: POST\r\n\r\n"
                ),
                (
                    f"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Origin: {good_origin}\r\nOrigin: https://evil.example\r\n\r\n"
                ),
                (
                    f"OPTIONS /api/config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Origin: {good_origin}\r\nSec-Fetch-Site: cross-site\r\nSec-Fetch-Site: same-origin\r\n\r\n"
                ),
            ):
                st_dup, h_dup, _ = _raw_http_request(dup_req.encode())
                self.assertEqual(st_dup, 403)
                self.assertNotIn("access-control-allow-origin", h_dup)
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
        import console.routes.anima as anima_mod
        import console.routes.config as cfg_mod
        import console.routes.gsv as gsv_mod
        import console.routes.state as state_mod
        import console.server as srv_mod
        import console.shared as shared_mod
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

    def test_oobe_esp32_allowlist_api_live_sync_and_generic_defaults(self):
        """OOBE: 18080 ESP32 rejected-device one-click allowlist API + live sync + generic UI/GSV defaults."""
        import runtime_events
        from console.routes import gsv as gsv_mod
        from patches import auth as auth_patch

        with tempfile.TemporaryDirectory() as td:
            cfg_p = os.path.join(td, ".config.yaml")
            with open(cfg_p, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    {
                        "server": {
                            "auth_key": "${ESP32_AUTH_KEY}",
                            "auth": {"enabled": True, "allowed_devices": ["${ESP32_ALLOWED_DEVICES}"]},
                        },
                        "selected_module": {"LLM": "DeepSeekLLM", "TTS": "EdgeTTS"},
                    },
                    f,
                    allow_unicode=True,
                )

            httpd, port = _start_test_console_server()
            orig_port = cs.CONSOLE_PORT
            cs.CONSOLE_PORT = port
            runtime_events.reset_runtime_events()

            class _FakeLiveSrv:
                def __init__(self, cfg, allowed=None):
                    self.config = cfg
                    if allowed is not None:
                        self.allowed_devices = allowed

            live_cfg = {"server": {"auth": {"enabled": True, "allowed_devices": []}}}
            live_ws = _FakeLiveSrv(live_cfg, allowed=set())
            # Distinct config dict (tests handler-level sync even when OTAHandler does not share ws_server.config)
            live_ota = _FakeLiveSrv({"server": {"auth": {"enabled": True, "allowed_devices": []}}}, allowed=set())
            isolated_ota = _FakeLiveSrv({"server": {"auth": {"enabled": True, "allowed_devices": []}}})
            runtime_events.register_ws_server(live_ws)
            runtime_events.register_auth_handler(live_ota)

            try:
                with (
                    patch.object(cs, "CONFIG_PATH", cfg_p),
                    patch.dict(os.environ, {"ESP32_ALLOWED_DEVICES": "11:22:33:44:55:aa"}, clear=False),
                ):
                    # Record a rejected ESP32 device (and duplicate hit increments count)
                    runtime_events.publish_event(
                        "device_rejected",
                        device_id="AA:BB:CC:DD:EE:01",
                        source="ota",
                        client_id="cli-new",
                        reason="not_in_allowlist",
                    )
                    runtime_events.publish_event(
                        "device_rejected",
                        device_id="aa:bb:cc:dd:ee:01",
                        source="ota",
                        client_id="cli-new",
                        reason="not_in_allowlist",
                    )

                    tok = cs.get_console_token()
                    auth_hdrs = _post_headers(port, token=tok)
                    no_tok_hdrs = _post_headers(port, token=None)

                    with httpx.Client(timeout=5.0, trust_env=False) as client:
                        # 1. GET /api/esp32-devices and /api/state expose env + pending without leaking auth_key
                        r_get = client.get(f"http://127.0.0.1:{port}/api/esp32-devices")
                        self.assertEqual(r_get.status_code, 200)
                        ea = r_get.json()["esp32_auth"]
                        self.assertTrue(ea["auth_enabled"])
                        self.assertEqual(ea["env_allowed_devices"], ["11:22:33:44:55:aa"])
                        self.assertEqual(ea["config_allowed_devices"], [])
                        self.assertEqual(len(ea["pending_devices"]), 1)
                        self.assertEqual(ea["pending_devices"][0]["device_id"], "aa:bb:cc:dd:ee:01")
                        self.assertEqual(ea["pending_devices"][0]["count"], 2)
                        self.assertNotIn("auth_key", ea)

                        # 2. POST /api/esp32-devices MUST require X-Console-Token (401) & block internal Host (403)
                        r_unauth = client.post(
                            f"http://127.0.0.1:{port}/api/esp32-devices",
                            json={"action": "add", "device_id": "aa:bb:cc:dd:ee:01"},
                            headers=no_tok_hdrs,
                        )
                        self.assertEqual(r_unauth.status_code, 401)

                        r_internal = client.post(
                            f"http://127.0.0.1:{port}/api/esp32-devices",
                            json={"action": "add", "device_id": "aa:bb:cc:dd:ee:01"},
                            headers={**auth_hdrs, "Host": f"xiaozhi-esp32-server:{port}"},
                        )
                        self.assertEqual(r_internal.status_code, 403)

                        # 3. Invalid inputs return HTTP 400
                        for bad_body in (
                            {"action": "add", "device_id": "${ESP32_ALLOWED_DEVICES}"},
                            {"action": "add", "device_id": "bad mac with space"},
                            {"action": "drop", "device_id": "aa:bb:cc:dd:ee:01"},
                            {"action": "add", "device_id": "aa:bb:cc:dd:ee:01", "extra": 1},
                        ):
                            r_bad = client.post(
                                f"http://127.0.0.1:{port}/api/esp32-devices",
                                json=bad_body,
                                headers=auth_hdrs,
                            )
                            self.assertEqual(r_bad.status_code, 400, f"Expected 400 for {bad_body}")

                        # 4. One-click allowlist normalizes dash MAC, persists to YAML (keeping ${ESP32_ALLOWED_DEVICES}),
                        #    clears pending, and hot-syncs ONLY live server/handlers (not isolated mocks)
                        r_add = client.post(
                            f"http://127.0.0.1:{port}/api/esp32-devices",
                            json={"action": "add", "device_id": "AA-BB-CC-DD-EE-01"},
                            headers=auth_hdrs,
                        )
                        self.assertEqual(r_add.status_code, 200)
                        add_j = r_add.json()
                        self.assertEqual(add_j["device_id"], "aa:bb:cc:dd:ee:01")
                        self.assertEqual(add_j["esp32_auth"]["pending_devices"], [])
                        self.assertIn("aa:bb:cc:dd:ee:01", add_j["esp32_auth"]["allowed_devices"])
                        self.assertIn("11:22:33:44:55:aa", add_j["esp32_auth"]["allowed_devices"])
                        self.assertIn("aa:bb:cc:dd:ee:01", auth_patch._extract_handler_allowed_devices(live_ws))
                        self.assertIn("aa:bb:cc:dd:ee:01", auth_patch._extract_handler_allowed_devices(live_ota))
                        self.assertEqual(auth_patch._extract_handler_allowed_devices(isolated_ota), set())

                        with open(cfg_p, "r", encoding="utf-8") as rf:
                            persisted = yaml.safe_load(rf)
                        self.assertIn("${ESP32_ALLOWED_DEVICES}", persisted["server"]["auth"]["allowed_devices"])
                        self.assertIn("aa:bb:cc:dd:ee:01", persisted["server"]["auth"]["allowed_devices"])

                        # 5. Removing env-only device returns 400; removing console-added device succeeds
                        r_rm_env = client.post(
                            f"http://127.0.0.1:{port}/api/esp32-devices",
                            json={"action": "remove", "device_id": "11:22:33:44:55:aa"},
                            headers=auth_hdrs,
                        )
                        self.assertEqual(r_rm_env.status_code, 400)

                        r_rm_ok = client.post(
                            f"http://127.0.0.1:{port}/api/esp32-devices",
                            json={"action": "remove", "device_id": "aa:bb:cc:dd:ee:01"},
                            headers=auth_hdrs,
                        )
                        self.assertEqual(r_rm_ok.status_code, 200)
                        self.assertNotIn("aa:bb:cc:dd:ee:01", auth_patch._extract_handler_allowed_devices(live_ws))

                        # 6. Dismiss clears pending without allowlisting (including malformed/short Device-Ids)
                        runtime_events.publish_event(
                            "device_rejected",
                            device_id="de:ad:be:ef:00:09",
                            source="ws",
                            reason="not_in_allowlist",
                        )
                        runtime_events.publish_event(
                            "device_rejected",
                            device_id="bad/id",
                            source="ota",
                            reason="not_in_allowlist",
                        )
                        r_dis = client.post(
                            f"http://127.0.0.1:{port}/api/esp32-devices",
                            json={"action": "dismiss", "device_id": "de:ad:be:ef:00:09"},
                            headers=auth_hdrs,
                        )
                        self.assertEqual(r_dis.status_code, 200)
                        r_dis_bad = client.post(
                            f"http://127.0.0.1:{port}/api/esp32-devices",
                            json={"action": "dismiss", "device_id": "bad/id"},
                            headers=auth_hdrs,
                        )
                        self.assertEqual(r_dis_bad.status_code, 200)
                        self.assertEqual(r_dis_bad.json()["esp32_auth"]["pending_devices"], [])
                        self.assertNotIn("de:ad:be:ef:00:09", r_dis_bad.json()["esp32_auth"]["allowed_devices"])
            finally:
                runtime_events.reset_runtime_events()
                cs.CONSOLE_PORT = orig_port
                httpd.shutdown()
                httpd.server_close()

        # 7. Verify generic defaults in gsv.py and static UI (no personal hardcoded weights or default wording)
        self.assertNotIn("芽衣-e10.ckpt", gsv_mod.GSV_PROFILES["__gsv_raiden_mei__"]["gpt_weights"])
        self.assertNotIn("芽衣_e10_s430_l32.pth", gsv_mod.GSV_PROFILES["__gsv_raiden_mei__"]["sovits_weights"])
        with open(cs.HTML_PATH, "r", encoding="utf-8") as f:
            html_src = f.read()
        with open(cs.JS_PATH, "r", encoding="utf-8") as f:
            js_src = f.read()
        for el_id in ("esp32AuthBadge", "esp32PendingWrap", "esp32PendingList", "esp32AllowedList", "esp32MacInput", "esp32AddMacBtn", "esp32AuthHint"):
            self.assertIn(f'id="{el_id}"', html_src)
        self.assertIn('consolePost("/api/esp32-devices"', js_src)
        self.assertNotIn("哥哥欢迎回来", html_src)
        self.assertNotIn("播放雷电芽衣原声测试片段", js_src)
        self.assertNotIn("太棒啦哥哥", js_src)
        self.assertNotIn("别难过了哥哥", js_src)

    def test_oobe_preboot_esp32_allowlist_sync_and_gsv_custom_weight_preservation(self):
        """OOBE Round 2: pre-boot ESP32 allowlist applies to late-registering handlers & custom GSV weights are preserved."""
        import os
        import tempfile
        from unittest.mock import patch
        import yaml
        import console_server as cs
        import console.routes.gsv as gsv_mod
        import runtime_events
        from patches import auth as auth_patch

        # Part A: Pre-boot allowlist race — user adds MAC in 18080 BEFORE OTA/Vision/WS handlers finish __init__
        class _FakeLateHandler:
            def __init__(self, cfg, allowed=None):
                self.config = cfg
                if allowed is not None:
                    self.allowed_devices = allowed

        runtime_events.reset_runtime_events()
        try:
            runtime_events.set_live_allowed_devices(["AA:BB:CC:DD:EE:99", "11:22:33:44:55:66"])
            late_ws = _FakeLateHandler({"server": {"auth": {"enabled": True, "allowed_devices": []}}}, allowed=set())
            late_ota = _FakeLateHandler({"server": {"auth": {"enabled": True, "allowed_devices": []}}}, allowed=set())
            late_vis = _FakeLateHandler({"server": {"auth": {"enabled": True, "allowed_devices": []}}})
            unregistered = _FakeLateHandler({"server": {"auth": {"enabled": True, "allowed_devices": []}}}, allowed=set())

            runtime_events.register_ws_server(late_ws)
            runtime_events.register_auth_handler(late_ota)
            runtime_events.register_auth_handler(late_vis)

            expected = {"aa:bb:cc:dd:ee:99", "11:22:33:44:55:66"}
            self.assertEqual(auth_patch._extract_handler_allowed_devices(late_ws), expected)
            self.assertEqual(auth_patch._extract_handler_allowed_devices(late_ota), expected)
            self.assertEqual(auth_patch._extract_handler_allowed_devices(late_vis), expected)
            self.assertEqual(late_ws.allowed_devices, expected)
            self.assertEqual(late_ota.allowed_devices, expected)
            self.assertEqual(late_vis.config["server"]["auth"]["allowed_devices"], sorted(expected))
            self.assertEqual(auth_patch._extract_handler_allowed_devices(unregistered), set())

            # Subsequent update propagates to all late-registered handlers
            runtime_events.set_live_allowed_devices(["11:22:33:44:55:66"])
            self.assertEqual(auth_patch._extract_handler_allowed_devices(late_ws), {"11:22:33:44:55:66"})
            self.assertEqual(auth_patch._extract_handler_allowed_devices(late_ota), {"11:22:33:44:55:66"})
            self.assertEqual(auth_patch._extract_handler_allowed_devices(late_vis), {"11:22:33:44:55:66"})

            # Dash-MAC normalization across auth.py & runtime_events, plus None-section & GC safety
            self.assertEqual(
                auth_patch.normalize_allowed_devices(["AA-BB-CC-DD-EE-07"], env_val="11-22-33-44-55-66"),
                ["aa:bb:cc:dd:ee:07", "11:22:33:44:55:66"],
            )
            self.assertEqual(auth_patch._normalize_device_id("DE-AD-BE-EF-00-01"), "de:ad:be:ef:00:01")
            runtime_events.set_live_allowed_devices(["AA-BB-CC-DD-EE-77"])
            null_sec_handler = _FakeLateHandler({"server": None}, allowed=set())
            null_auth_handler = _FakeLateHandler({"server": {"auth": None}}, allowed=set())
            runtime_events.register_auth_handler(null_sec_handler)
            runtime_events.register_auth_handler(null_auth_handler)
            self.assertEqual(auth_patch._extract_handler_allowed_devices(null_sec_handler), {"aa:bb:cc:dd:ee:77"})
            self.assertEqual(auth_patch._extract_handler_allowed_devices(null_auth_handler), {"aa:bb:cc:dd:ee:77"})
            # Repeated register calls on an already-synced handler must stay idempotent
            runtime_events.register_auth_handler(null_sec_handler)
            self.assertEqual(auth_patch._extract_handler_allowed_devices(null_sec_handler), {"aa:bb:cc:dd:ee:77"})
            # Dropping the only strong ref to a registered handler must not leave dangling config tracking
            import gc as _gc

            ephemeral_cfg = {"server": {"auth": {"enabled": True, "allowed_devices": []}}}
            ephemeral_h = _FakeLateHandler(ephemeral_cfg, allowed=set())
            runtime_events.register_auth_handler(ephemeral_h)
            self.assertEqual(runtime_events.get_live_allowed_devices_for_config(ephemeral_cfg), {"aa:bb:cc:dd:ee:77"})
            del ephemeral_h
            _gc.collect()
            self.assertIsNone(runtime_events.get_live_allowed_devices_for_config(ephemeral_cfg))

            # reset_runtime_events() clears remembered allowlist cleanly for isolated unit tests
            runtime_events.reset_runtime_events()
            after_reset = _FakeLateHandler(
                {"server": {"auth": {"enabled": True, "allowed_devices": ["fe:ed:fa:ce:00:01"]}}},
                allowed={"fe:ed:fa:ce:00:01"},
            )
            runtime_events.register_auth_handler(after_reset)
            self.assertEqual(auth_patch._extract_handler_allowed_devices(after_reset), {"fe:ed:fa:ce:00:01"})
        finally:
            runtime_events.reset_runtime_events()

        # Part B: Existing machine custom raiden_mei weights in tts_infer_xiaozhi.yaml MUST be detected and preserved
        with tempfile.TemporaryDirectory() as td:
            infer_p = os.path.join(td, "tts_infer_xiaozhi.yaml")
            custom_gpt = "/models/gsv_weights/raiden_mei/v4/custom/mei_finetuned.ckpt"
            custom_sov = "/models/gsv_weights/raiden_mei/v4/custom/mei_finetuned.pth"
            with open(infer_p, "w", encoding="utf-8") as wf:
                yaml.safe_dump(
                    {
                        "custom": {
                            "version": "v4",
                            "device": "cpu",
                            "is_half": False,
                            "bert_base_path": "custom/bert",
                            "t2s_weights_path": custom_gpt,
                            "vits_weights_path": custom_sov,
                        }
                    },
                    wf,
                    allow_unicode=True,
                )

            http_calls: list[tuple[str, str]] = []

            class _Resp200:
                status_code = 200
                text = "ok"

            class _FakeClient:
                def get(self, url, params=None, timeout=None):
                    http_calls.append((url.rsplit("/", 1)[-1], (params or {}).get("weights_path", "")))
                    return _Resp200()

            orig_loaded = gsv_mod._active_gsv_loaded
            gsv_mod._active_gsv_loaded = None
            gsv_mod._preserved_boot_mei_weights.pop(infer_p, None)
            try:
                with (
                    patch.object(cs, "GSV_INFER_YAML", infer_p),
                    patch.object(cs, "_get_console_http_client", return_value=_FakeClient()),
                    patch.dict(
                        os.environ,
                        {"GSV_MEI_GPT_WEIGHTS": "", "GSV_MEI_SOVITS_WEIGHTS": "", "GSV_MEI_VERSION": ""},
                        clear=False,
                    ),
                ):
                    # 1. Boot detection matches custom raiden_mei weights without clobbering
                    self.assertEqual(gsv_mod._detect_boot_gsv_voice(), "__gsv_raiden_mei__")
                    gsv_mod._ensure_gsv_weights("__gsv_raiden_mei__", persist_boot_yaml=True)
                    self.assertEqual(http_calls, [], "Already-loaded custom boot weights must not re-swap")
                    with open(infer_p, "r", encoding="utf-8") as rf:
                        after_same = yaml.safe_load(rf)
                    self.assertEqual(after_same["custom"]["t2s_weights_path"], custom_gpt)
                    self.assertEqual(after_same["custom"]["vits_weights_path"], custom_sov)
                    self.assertEqual(after_same["custom"]["device"], "cpu")
                    self.assertIs(after_same["custom"]["is_half"], False)
                    self.assertEqual(after_same["custom"]["bert_base_path"], "custom/bert")

                    # 2. Switch to __gsv_xiaozhi_tw__ (persisting YAML), simulate container restart
                    #    (clear in-memory cache), then switch back to __gsv_raiden_mei__:
                    #    MUST restore preserved custom weights from sidecar backup, not defaults!
                    gsv_mod._ensure_gsv_weights("__gsv_xiaozhi_tw__", persist_boot_yaml=True)
                    gsv_mod._preserved_boot_mei_weights.clear()
                    gsv_mod._active_gsv_loaded = None
                    http_calls.clear()
                    gsv_mod._ensure_gsv_weights("__gsv_raiden_mei__", persist_boot_yaml=True)
                    self.assertEqual(
                        http_calls,
                        [("set_gpt_weights", custom_gpt), ("set_sovits_weights", custom_sov)],
                    )
                    with open(infer_p, "r", encoding="utf-8") as rf:
                        after_back = yaml.safe_load(rf)
                    self.assertEqual(after_back["custom"]["t2s_weights_path"], custom_gpt)
                    self.assertEqual(after_back["custom"]["vits_weights_path"], custom_sov)

                # 2b. Half-pretrained custom fine-tune (pretrained s1v3.ckpt + custom SoVITS without "raiden_mei"
                #     in path) AND Windows backslash path separator matching must be preserved!
                with tempfile.TemporaryDirectory() as td_half:
                    half_p = os.path.join(td_half, "tts_infer_xiaozhi.yaml")
                    half_gpt = "GPT_SoVITS/pretrained_models/s1v3.ckpt"
                    half_sov_win = r"D:\my_models\speaker_e12.pth"
                    with open(half_p, "w", encoding="utf-8") as wf:
                        yaml.safe_dump(
                            {
                                "custom": {
                                    "version": "v4",
                                    "t2s_weights_path": half_gpt,
                                    "vits_weights_path": half_sov_win,
                                }
                            },
                            wf,
                            allow_unicode=True,
                        )
                    gsv_mod._active_gsv_loaded = None
                    with (
                        patch.object(cs, "GSV_INFER_YAML", half_p),
                        patch.dict(
                            os.environ,
                            {"GSV_MEI_GPT_WEIGHTS": "", "GSV_MEI_SOVITS_WEIGHTS": "", "GSV_MEI_VERSION": ""},
                            clear=False,
                        ),
                    ):
                        self.assertTrue(gsv_mod._is_custom_mei_weights(half_gpt, half_sov_win))
                        self.assertEqual(gsv_mod._detect_boot_gsv_voice(), "__gsv_raiden_mei__")
                    with (
                        patch.object(cs, "GSV_INFER_YAML", half_p),
                        patch.dict(
                            os.environ,
                            {
                                "GSV_MEI_GPT_WEIGHTS": half_gpt,
                                "GSV_MEI_SOVITS_WEIGHTS": "D:/my_models/speaker_e12.pth",
                                "GSV_MEI_VERSION": "v4",
                            },
                            clear=False,
                        ),
                    ):
                        self.assertEqual(gsv_mod._detect_boot_gsv_voice(), "__gsv_raiden_mei__")
                    gsv_mod._preserved_boot_mei_weights.pop(half_p, None)

                # 3. Explicit GSV_MEI_* env override takes priority over preserved boot YAML
                env_gpt = "/env/raiden_mei/override.ckpt"
                env_sov = "/env/raiden_mei/override.pth"
                gsv_mod._active_gsv_loaded = None
                http_calls.clear()
                with (
                    patch.object(cs, "GSV_INFER_YAML", infer_p),
                    patch.object(cs, "_get_console_http_client", return_value=_FakeClient()),
                    patch.dict(
                        os.environ,
                        {"GSV_MEI_GPT_WEIGHTS": env_gpt, "GSV_MEI_SOVITS_WEIGHTS": env_sov, "GSV_MEI_VERSION": "v4"},
                        clear=False,
                    ),
                ):
                    self.assertIsNone(gsv_mod._detect_boot_gsv_voice())
                    gsv_mod._ensure_gsv_weights("__gsv_raiden_mei__", persist_boot_yaml=True)
                    self.assertEqual(
                        http_calls,
                        [("set_gpt_weights", env_gpt), ("set_sovits_weights", env_sov)],
                    )

                # 4. Shared s1v3.ckpt between two profiles does NOT misidentify __gsv_xiaozhi_tw__ as __gsv_raiden_mei__
                with open(infer_p, "w", encoding="utf-8") as wf:
                    yaml.safe_dump(
                        {
                            "custom": {
                                "version": "v2ProPlus",
                                "t2s_weights_path": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
                                "vits_weights_path": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth",
                            }
                        },
                        wf,
                        allow_unicode=True,
                    )
                with (
                    patch.object(cs, "GSV_INFER_YAML", infer_p),
                    patch.dict(
                        os.environ,
                        {
                            "GSV_MEI_GPT_WEIGHTS": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
                            "GSV_MEI_SOVITS_WEIGHTS": "GPT_SoVITS/pretrained_models/gsv-v4-pretrained/s2Gv4.pth",
                        },
                        clear=False,
                    ),
                ):
                    self.assertEqual(gsv_mod._detect_boot_gsv_voice(), "__gsv_xiaozhi_tw__")
            finally:
                gsv_mod._active_gsv_loaded = orig_loaded
                gsv_mod._preserved_boot_mei_weights.pop(infer_p, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Batch 3 unit, regression, and benchmark tests for voice pipeline, speculative VAD (F-1),
memory/ASR optimizations (G-1/G-2/G-4), and router hardening (C-4/C-5/C-6/C-7/C-8).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT_DIR = "/opt/xiaozhi-esp32-server"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DIR = os.environ.get("XIAOZHI_APP_DIR") or (
    os.path.join(ROOT_DIR, "app")
    if os.path.isdir(os.path.join(ROOT_DIR, "app"))
    else os.path.join(_REPO_ROOT, "app")
)
for _p in (ROOT_DIR, APP_DIR):
    if _p not in sys.path and os.path.isdir(_p):
        sys.path.insert(0, _p)

import asr_corrections
import mei_emotion_router as mer
import patch_registry
import pc_audio_router as par
import runtime_settings as rs
import vad_speculative_router as vsr
import xz_common

try:
    import boot
    import start_with_env  # noqa: F401
    import nmc_weather_patch as nmc
    from core.providers.asr.base import ASRProviderBase
    from core.providers.asr.dto.dto import InterfaceType
    from core.providers.tts.gpt_sovits_v2 import TTSProvider as GsvTTSProvider
    from core.utils.audioRateController import AudioRateController
    from core.utils.dialogue import Dialogue, Message

    boot.install_all(start_background_workers=False, run_contract_self_check=True)
    _HAS_XIAOZHI_CORE = True
except Exception:
    _HAS_XIAOZHI_CORE = False


def _make_silence_wav_bytes(duration_sec: float = 0.6, sr: int = 16000) -> bytes:
    import io

    n = int(sr * duration_sec)
    samples = [int(1500 * ((i % 40) < 20)) for i in range(n)]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(struct.pack(f"<{n}h", *samples))
    return buf.getvalue()


class TestBatch3StandaloneUnits(unittest.TestCase):
    """Tests that run cleanly both on host and inside container."""

    def test_c6_per_device_mood_isolation_and_backward_compat(self):
        mer.clear_anima_mood()
        try:
            # Default starts calm
            self.assertEqual(mer.get_anima_mood(), "calm")
            self.assertEqual(mer.get_anima_mood("AA:BB:CC:00:00:01"), "calm")

            # Device-scoped moods never leak across devices or overwrite default
            mer.set_anima_mood("stage=close, last_emotion=happy", device_id="AA:BB:CC:00:00:01")
            mer.set_anima_mood("stage=familiar, last_emotion=sad", device_id="AA:BB:CC:00:00:02")

            self.assertEqual(mer.get_anima_mood("AA:BB:CC:00:00:01"), "happy")
            self.assertEqual(mer.get_anima_mood("aa:bb:cc:00:00:01"), "happy")
            self.assertEqual(mer.get_anima_mood("AA:BB:CC:00:00:02"), "tender")
            self.assertEqual(mer.get_anima_mood("AA:BB:CC:00:00:03"), "calm")
            self.assertEqual(mer.get_anima_mood(), "calm")

            # Neutral text resolves per device_id
            with patch.object(mer, "ref_audio_exists", return_value=True):
                self.assertEqual(
                    mer.resolve_mei_emotion("文件已经整理到桌面目录了。", device_id="AA:BB:CC:00:00:01"),
                    "happy",
                )
                self.assertEqual(
                    mer.resolve_mei_emotion("文件已经整理到桌面目录了。", device_id="AA:BB:CC:00:00:02"),
                    "tender",
                )

            # Legacy no-device_id call updates default without clobbering device-specific slots
            mer.set_anima_mood("stage=close, last_emotion=warm")
            self.assertEqual(mer.get_anima_mood(), "gentle")
            self.assertEqual(mer.get_anima_mood("AA:BB:CC:00:00:01"), "happy")
            self.assertEqual(mer.get_anima_mood("AA:BB:CC:00:00:02"), "tender")
            self.assertEqual(mer.get_anima_mood("AA:BB:CC:00:00:99"), "gentle")
        finally:
            mer.clear_anima_mood()

    def test_g4_asr_corrections_hot_reload_longest_match_and_validation(self):
        # Verify tracked example template exists and includes required seed pairs
        proj_root = os.path.dirname(APP_DIR)
        ex_candidates = [
            os.path.join(proj_root, "config", "asr_corrections.example.json"),
            os.path.join(ROOT_DIR, "config_templates", "asr_corrections.example.json"),
        ]
        ex_path = next((p for p in ex_candidates if os.path.isfile(p)), None)
        self.assertIsNotNone(ex_path, "Missing config/asr_corrections.example.json")
        with open(ex_path, "r", encoding="utf-8") as f:
            ex_data = json.load(f)
        self.assertEqual(ex_data["corrections"]["牙医"], "芽衣")
        self.assertEqual(ex_data["corrections"]["雷电牙医"], "雷电芽衣")

        with tempfile.TemporaryDirectory() as td:
            corr_path = os.path.join(td, "asr_corrections.json")
            # Default seed works even before file exists
            d0 = asr_corrections.get_asr_corrections(corr_path)
            self.assertTrue(d0["enabled"])
            self.assertEqual(d0["corrections"]["牙医"], "芽衣")
            self.assertEqual(
                asr_corrections.apply_asr_corrections("雷电牙医你好呀，牙医帮我查天气", path=corr_path),
                "雷电芽衣你好呀，芽衣帮我查天气",
            )

            # Save custom rules (including longest-match & single-pass non-cascading check)
            saved = asr_corrections.save_asr_corrections(
                {"牙医": "芽衣", "雷电牙医": "雷电芽衣", "甲": "乙", "乙": "丙"},
                enabled=True,
                path=corr_path,
            )
            self.assertTrue(os.path.isfile(corr_path))
            self.assertEqual(saved["count"], 4)
            self.assertEqual(asr_corrections.apply_asr_corrections("甲和雷电牙医", path=corr_path), "乙和雷电芽衣")

            # Disable preserves stored rules while skipping replacement
            asr_corrections.save_asr_corrections(enabled=False, path=corr_path)
            self.assertEqual(asr_corrections.apply_asr_corrections("雷电牙医", path=corr_path), "雷电牙医")
            self.assertEqual(asr_corrections.get_asr_corrections(corr_path)["count"], 4)

            # Hot-reload via direct file edit (mtime + size change) takes effect immediately
            time.sleep(0.02)
            with open(corr_path, "w", encoding="utf-8") as f:
                json.dump({"schema_version": 1, "enabled": True, "corrections": {"测试词": "目标词"}}, f, ensure_ascii=False)
            self.assertEqual(asr_corrections.apply_asr_corrections("这是一个测试词", path=corr_path), "这是一个目标词")

            # Supports newline text format and list-of-dicts format
            res_txt = asr_corrections.save_asr_corrections("你好牙医->你好芽衣\n小志=小智\n", path=corr_path)
            self.assertEqual(res_txt["corrections"], {"你好牙医": "你好芽衣", "小志": "小智"})

            # Validation rejects invalid payloads cleanly
            with self.assertRaises(ValueError):
                asr_corrections.save_asr_corrections({"": "芽衣"}, path=corr_path)
            with self.assertRaises(ValueError):
                asr_corrections.save_asr_corrections({"x" * 65: "y"}, path=corr_path)
            with self.assertRaises(ValueError):
                asr_corrections.save_asr_corrections(enabled="yes", path=corr_path)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                asr_corrections.save_asr_corrections({f"k{i}": f"v{i}" for i in range(505)}, path=corr_path)

    def test_f1_runtime_settings_hot_config_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            rs_path = os.path.join(td, "runtime_settings.json")
            cfg0 = rs.get_vad_speculative(settings_path=rs_path)
            self.assertTrue(cfg0["enabled"])
            self.assertEqual(cfg0["speculative_ms"], 550)
            self.assertEqual(cfg0["post_wakeup_ms"], 800)
            self.assertEqual(cfg0["min_chars"], 4)
            self.assertIn("吗", cfg0["complete_suffixes"])
            self.assertIn("然后", cfg0["hesitation_suffixes"])

            # Hot update & clamping (strictly below 1100ms upper bound)
            cfg1 = rs.set_vad_speculative(
                enabled=False,
                speculative_ms=600,
                post_wakeup_ms=900,
                min_chars=5,
                complete_suffixes="吗, 呢\n吧",
                hesitation_suffixes=["然后", "就是", "然后"],
                settings_path=rs_path,
            )
            self.assertFalse(cfg1["enabled"])
            self.assertEqual(cfg1["speculative_ms"], 600)
            self.assertEqual(cfg1["post_wakeup_ms"], 900)
            self.assertEqual(cfg1["min_chars"], 5)
            self.assertEqual(cfg1["complete_suffixes"], ["吗", "呢", "吧"])
            self.assertEqual(cfg1["hesitation_suffixes"], ["然后", "就是"])

            # Clamping bounds + post_wakeup_ms >= speculative_ms invariant
            cfg2 = rs.set_vad_speculative(speculative_ms=9999, post_wakeup_ms=400, min_chars=99, settings_path=rs_path)
            self.assertEqual(cfg2["speculative_ms"], 950)
            self.assertGreaterEqual(cfg2["post_wakeup_ms"], cfg2["speculative_ms"])
            self.assertLessEqual(cfg2["post_wakeup_ms"], 1050)
            self.assertEqual(cfg2["min_chars"], 20)

            # Invalid types raise ValueError
            with self.assertRaises(ValueError):
                rs.set_vad_speculative(enabled="true", settings_path=rs_path)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                rs.set_vad_speculative(speculative_ms=True, settings_path=rs_path)  # type: ignore[arg-type]

    def test_g1_docker_compose_glibc_malloc_env_and_1100ms_vad_preserved(self):
        import re

        proj_root = os.path.dirname(APP_DIR)
        compose_path = os.path.join(proj_root, "docker-compose.yml")
        if os.path.isfile(compose_path):
            with open(compose_path, "r", encoding="utf-8") as f:
                raw_dc = f.read()
            # Skip upstream image's bundled /opt/xiaozhi-esp32-server/docker-compose.yml inside container
            if "anima-brain:" in raw_dc and "xiaozhi-esp32-server:" in raw_dc:
                anima_sec, xz_sec = raw_dc.split("xiaozhi-esp32-server:", 1)
                self.assertIn("MALLOC_ARENA_MAX=2", xz_sec)
                self.assertIn("OMP_NUM_THREADS=2", xz_sec)
                self.assertIn("MALLOC_TRIM_THRESHOLD_=131072", xz_sec)
                self.assertIn("MALLOC_MMAP_THRESHOLD_=131072", xz_sec)
                # Ensure we did NOT pollute anima-brain with those two env vars
                self.assertNotIn("MALLOC_TRIM_THRESHOLD_=131072", anima_sec)
                self.assertNotIn("MALLOC_MMAP_THRESHOLD_=131072", anima_sec)

        cfg_paths = [
            os.path.join(proj_root, "config", ".config.example.yaml"),
            os.path.join(ROOT_DIR, "config_templates", ".config.example.yaml"),
            os.path.join(ROOT_DIR, "data", ".config.yaml"),
        ]
        checked_any = False
        for cp in cfg_paths:
            if os.path.isfile(cp):
                with open(cp, "r", encoding="utf-8") as f:
                    raw_cfg = f.read()
                m_vad = re.search(r"min_silence_duration_ms\s*:\s*(\d+)", raw_cfg)
                if m_vad:
                    self.assertEqual(int(m_vad.group(1)), 1100, f"VAD upper bound regressed in {cp}")
                    checked_any = True
        self.assertTrue(checked_any, "Expected at least one config YAML with SileroVAD.min_silence_duration_ms=1100")


@unittest.skipUnless(_HAS_XIAOZHI_CORE, "Requires xiaozhi-esp32-server container environment")
class TestBatch3ContainerPipeline(unittest.TestCase):
    """Full container-side tests for C-4, C-5, C-7, C-8, G-1, G-2, G-4, and F-1."""

    def test_c4_wakeup_cache_snapshot_no_tts_mutation_idle_guard_and_context_lock(self):
        import core.handle.helloHandle as hello_mod

        mer.apply_xiaozhi_emotion_and_prosody_patches()
        mer.apply_mei_wakeup_responses_patch()

        cfg = {
            "url": "http://host.docker.internal:19880/tts",
            "output_dir": "/tmp/",
            "text_lang": "zh",
            "ref_audio_path": "voices/raiden_mei/mei_default_ref.wav",
            "aux_ref_audio_paths": [],
            "prompt_text": "今夜月色很美，呵...适合与朋友小聚一番。",
            "prompt_lang": "zh",
            "top_k": 12,
            "top_p": 1.0,
            "temperature": 0.98,
            "text_split_method": "cut0",
            "speed_factor": 1.0,
            "sample_steps": 16,
        }
        tts = GsvTTSProvider(cfg, delete_audio_file=True)
        tts._turn_explicit_emotion = "happy"
        tts._pending_segment_emotion = "playful"
        tts._suppress_first_seg_fast_steps = False
        tts._first_segment_done_sid = "live-sid-99"
        tts._segment_emotion_queue = [("live-sid-99", "保持不变", "😆", "happy")]

        # On clean CI runners without gitignored private voices/raiden_mei WAVs, stub ref_audio_exists
        # so pure snapshot/lock logic still executes in full; on local hosts, real FS check runs unchanged.
        _ref_ctx = (
            contextlib.nullcontext()
            if os.path.isdir(os.path.join(xz_common.VOICES_DIR, "raiden_mei"))
            else patch.object(mer, "ref_audio_exists", return_value=True)
        )
        with _ref_ctx:
            # 1. Snapshot builds gentle + full sample_steps=16 WITHOUT mutating tts
            url, snap_json, _prof, emo_key = mer.build_gsv_request_snapshot(
                tts,
                "我在呢，哥哥请说吧。",
                explicit_segment_emotion="gentle",
                turn_explicit_emotion="gentle",
                is_first_segment=False,
                suppress_first_seg_fast_steps=True,
            )
            self.assertEqual(url, "http://host.docker.internal:19880/tts")
            self.assertEqual(emo_key, "gentle")
            self.assertTrue(snap_json["ref_audio_path"].endswith("mei_gentle_ref.wav"))
            self.assertEqual(snap_json["sample_steps"], 16)
            self.assertEqual(snap_json["text_split_method"], "cut0")
            self.assertEqual(tts._turn_explicit_emotion, "happy")
            self.assertEqual(tts._pending_segment_emotion, "playful")
            self.assertFalse(tts._suppress_first_seg_fast_steps)

            # 2. Full wakeupWordsResponse runs via snapshot, leaves shared tts untouched, releases async lock cleanly
            wav_sample = _make_silence_wav_bytes(0.6, 16000)
            posted_payloads = []

            class _FakeResp:
                status_code = 200
                content = wav_sample
                text = "ok"

            def _fake_post(u, json=None, timeout=None):
                posted_payloads.append(dict(json or {}))
                return _FakeResp()

            async def _run():
                with tempfile.TemporaryDirectory() as td:
                    out_wav = os.path.join(td, "raiden_mei.wav")
                    conn = SimpleNamespace(tts=tts, sample_rate=16000)
                    with (
                        patch.object(mer._get_gsv_http_session(), "post", side_effect=_fake_post),
                        patch.object(hello_mod.wakeup_words_config, "generate_file_path", return_value=out_wav),
                        patch.object(hello_mod.wakeup_words_config, "update_wakeup_response") as m_upd,
                        patch.object(hello_mod, "audio_to_data", new=AsyncMock(return_value=[b"opus"])),
                    ):
                        # 2a. When GSV is busy -> skips completely (0 HTTP calls)
                        with patch.object(mer, "is_live_gsv_busy", return_value=True):
                            await hello_mod.wakeupWordsResponse(conn)
                        self.assertEqual(len(posted_payloads), 0)
                        m_upd.assert_not_called()

                        # 2b. When GSV_GPU_LOCK is held by another thread -> skips without blocking!
                        held = threading.Event()
                        release = threading.Event()

                        def _hold():
                            with mer.GSV_GPU_LOCK:
                                held.set()
                                release.wait(3.0)

                        th = threading.Thread(target=_hold, daemon=True)
                        th.start()
                        self.assertTrue(held.wait(2.0))
                        try:
                            t0 = time.monotonic()
                            await hello_mod.wakeupWordsResponse(conn)
                            self.assertLess(time.monotonic() - t0, 0.5)
                            self.assertEqual(len(posted_payloads), 0)
                        finally:
                            release.set()
                            th.join(timeout=2.0)

                        # 2c. When GSV is idle -> synthesizes gentle snapshot with sample_steps=16, ZERO tts mutation
                        await hello_mod.wakeupWordsResponse(conn)
                        self.assertEqual(len(posted_payloads), 1)
                        self.assertTrue(posted_payloads[0]["ref_audio_path"].endswith("mei_gentle_ref.wav"))
                        self.assertEqual(posted_payloads[0]["sample_steps"], 16)
                        self.assertTrue(os.path.isfile(out_wav))
                        m_upd.assert_called_once()

                        # Shared TTS state remained 100% intact!
                        self.assertEqual(tts._turn_explicit_emotion, "happy")
                        self.assertEqual(tts._pending_segment_emotion, "playful")
                        self.assertFalse(tts._suppress_first_seg_fast_steps)
                        self.assertEqual(tts._first_segment_done_sid, "live-sid-99")
                        self.assertEqual(len(tts._segment_emotion_queue), 1)
                        self.assertFalse(hello_mod._wakeup_response_lock.locked())

                        # 2d. Exception inside synthesis still releases async with lock cleanly
                        with patch.object(mer._get_gsv_http_session(), "post", side_effect=RuntimeError("boom")):
                            with self.assertRaises(RuntimeError):
                                await hello_mod.wakeupWordsResponse(conn)
                        self.assertFalse(hello_mod._wakeup_response_lock.locked())

            asyncio.run(_run())

    def test_c5_async_gsv_nonblocking_and_queue_empty_event_bounded_wait(self):
        import core.handle.sendAudioHandle as send_audio_mod

        mer.apply_xiaozhi_emotion_and_prosody_patches()
        par.apply_pc_audio_playback_patches()

        # 1. Verify _patched_gsv_text_to_speak does NOT block the asyncio event loop during HTTP + lock wait
        cfg = {
            "url": "http://host.docker.internal:19880/tts",
            "output_dir": "/tmp/",
            "text_lang": "zh",
            "ref_audio_path": "voices/raiden_mei/mei_default_ref.wav",
            "prompt_text": "今夜月色很美",
            "prompt_lang": "zh",
            "text_split_method": "cut0",
        }
        provider = GsvTTSProvider(cfg, delete_audio_file=True)

        class _FakeResp:
            status_code = 200
            content = b"RIFF\x24\x00\x00\x00WAVEfmt "
            text = "ok"

        def _slow_post(url, json=None, timeout=None):
            time.sleep(0.14)
            return _FakeResp()

        async def _check_nonblocking():
            ticks = 0

            async def _ticker():
                nonlocal ticks
                for _ in range(10):
                    await asyncio.sleep(0.015)
                    ticks += 1

            with patch.object(mer._get_gsv_http_session(), "post", side_effect=_slow_post):
                tts_task = asyncio.create_task(provider.text_to_speak("[gentle] 异步非阻塞测试。", None))
                tick_task = asyncio.create_task(_ticker())
                wav_out = await tts_task
                await tick_task
            self.assertTrue(wav_out.startswith(b"RIFF"))
            self.assertGreaterEqual(ticks, 4, f"Event loop appeared blocked during GSV POST (ticks={ticks})")

        asyncio.run(_check_nonblocking())

        # 2. Verify pc_audio_router handles upstream AudioRateController dead-task & stalled queue_empty_event
        async def _check_queue_empty_bounds():
            class _Ws:
                def __init__(self):
                    self.sent = []

                async def send(self, msg):
                    self.sent.append(msg)

            # 2a. Real AudioRateController whose _send_loop crashes on WS send -> queue_empty_event NEVER set upstream!
            rc_dead = AudioRateController(frame_duration=60)
            rc_dead.add_audio(b"\x00" * 16)
            self.assertFalse(rc_dead.queue_empty_event.is_set())

            async def _failing_send(_pkt):
                raise RuntimeError("ws closed mid-playback")

            rc_dead.start_sending(_failing_send)
            await asyncio.sleep(0.05)
            self.assertTrue(rc_dead.pending_send_task.done())
            self.assertFalse(rc_dead.queue_empty_event.is_set(), "Confirms upstream §8 #2 bug: event left unset on crash")

            conn_dead = SimpleNamespace(
                session_id="s-dead",
                sentence_id="sid-dead",
                client_abort=False,
                config={"enable_stop_tts_notify": False},
                websocket=_Ws(),
                audio_rate_controller=rc_dead,
                _pc_audio_active_sid="sid-dead",
                _pc_audio_queued_ms=2000,
                clearSpeakStatus=lambda: None,
            )
            with patch.object(par, "_wait_pc_audio_done", new=AsyncMock(return_value=True)):
                t0 = time.monotonic()
                await send_audio_mod.send_tts_message(conn_dead, "stop", None)
                elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5, f"Dead AudioRateController task should abort wait fast, took {elapsed:.2f}s")
            self.assertIsNone(conn_dead._pc_audio_active_sid)

            # 2b. Stalled queue_empty_event (no progress) -> times out cleanly at QUEUE_EMPTY_WAIT_TIMEOUT
            rc_stalled = SimpleNamespace(
                queue=deque([("audio", b"1"), ("audio", b"2")]),
                queue_empty_event=asyncio.Event(),
                pending_send_task=None,
                stop_sending=lambda: None,
            )
            conn_stalled = SimpleNamespace(
                session_id="s-stall",
                sentence_id="sid-stall",
                client_abort=False,
                config={"enable_stop_tts_notify": False},
                websocket=_Ws(),
                audio_rate_controller=rc_stalled,
                _pc_audio_active_sid="sid-stall",
                _pc_audio_queued_ms=2000,
                clearSpeakStatus=lambda: None,
            )
            with (
                patch.object(par, "QUEUE_EMPTY_WAIT_TIMEOUT", 0.14),
                patch.object(par, "QUEUE_EMPTY_POLL_SLICE", 0.04),
                patch.object(par, "_wait_pc_audio_done", new=AsyncMock(return_value=True)),
            ):
                t0 = time.monotonic()
                await send_audio_mod.send_tts_message(conn_stalled, "stop", None)
                elapsed_s = time.monotonic() - t0
            self.assertGreaterEqual(elapsed_s, 0.12)
            self.assertLess(elapsed_s, 0.6, f"Stalled wait exceeded bound: {elapsed_s:.2f}s")
            self.assertIsNone(conn_stalled._pc_audio_active_sid)

            # 2c. Long story actively draining past stall_timeout -> waits cleanly until queue empties!
            q_prog = deque([("audio", b"x")] * 5)
            ev_prog = asyncio.Event()
            rc_prog = SimpleNamespace(queue=q_prog, queue_empty_event=ev_prog, pending_send_task=None)
            conn_prog = SimpleNamespace(sentence_id="sid-prog", client_abort=False, _pc_audio_queued_ms=5000)

            async def _drainer():
                for _ in range(5):
                    await asyncio.sleep(0.035)
                    q_prog.popleft()
                ev_prog.set()

            with (
                patch.object(par, "QUEUE_EMPTY_WAIT_TIMEOUT", 0.10),
                patch.object(par, "QUEUE_EMPTY_POLL_SLICE", 0.03),
            ):
                dr_task = asyncio.create_task(_drainer())
                ok_prog = await par._wait_rate_controller_empty(conn_prog, rc_prog, "sid-prog")
                await dr_task
            self.assertTrue(ok_prog, "Actively draining long story should not time out prematurely")

        asyncio.run(_check_queue_empty_bounds())

    def test_c7_nmc_weather_exponential_backoff_client_reuse_and_local_cache_preference(self):
        import httpx

        orig_stations = list(nmc._stations_list)
        orig_fail = nmc._fetch_fail_count
        orig_retry = nmc._next_fetch_retry_at

        async def _run():
            with tempfile.TemporaryDirectory() as td:
                missing_cache = os.path.join(td, "missing_stations.json")
                valid_cache = os.path.join(td, "valid_stations.json")
                valid_600 = [{"code": f"S{i}", "province": "湖南", "city": f"城{i}"} for i in range(600)]
                with open(valid_cache, "w", encoding="utf-8") as f:
                    json.dump(valid_600, f)

                http_calls = []
                created_clients = []

                def _fail_transport(request: httpx.Request) -> httpx.Response:
                    http_calls.append(str(request.url))
                    return httpx.Response(503)

                orig_cls = httpx.AsyncClient

                def _counting_client(*args, **kwargs):
                    kwargs["transport"] = httpx.MockTransport(_fail_transport)
                    c = orig_cls(*args, **kwargs)
                    created_clients.append(c)
                    return c

                # 1. Exponential backoff 5m -> 10m -> 20m -> 40m -> 1h -> 1h cap + single reused AsyncClient
                nmc.reset_station_fetch_state()
                await nmc.close_async_client()
                expected_delays = [300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0]
                with (
                    patch.object(nmc, "STATIONS_CACHE_PATH", missing_cache),
                    patch.object(nmc.httpx, "AsyncClient", side_effect=_counting_client),
                ):
                    for idx, exp_d in enumerate(expected_delays, start=1):
                        # Expire previous backoff to test next step
                        nmc._next_fetch_retry_at = 0.0
                        res = await nmc._ensure_stations_loaded()
                        self.assertEqual(res, [])
                        st = nmc.get_station_fetch_backoff_state()
                        self.assertEqual(st["fail_count"], idx)
                        self.assertGreater(st["backoff_remaining_seconds"], exp_d - 5.0)
                        self.assertLessEqual(st["backoff_remaining_seconds"], exp_d + 1.0)

                    # Calling again while inside backoff makes ZERO network calls!
                    calls_before = len(http_calls)
                    res_skip = await nmc._ensure_stations_loaded()
                    self.assertEqual(res_skip, [])
                    self.assertEqual(len(http_calls), calls_before)

                    # Module-level AsyncClient was created ONCE and reused across all 6 attempts + wttr call
                    await nmc._fetch_overseas_wttr("Tokyo")
                    self.assertEqual(len(created_clients), 1)

                # 2. Valid local nmc_stations_cache.json ALWAYS preferred (0 network calls, resets backoff!)
                nmc._stations_list = []
                calls_before_local = len(http_calls)
                with (
                    patch.object(nmc, "STATIONS_CACHE_PATH", valid_cache),
                    patch.object(nmc.httpx, "AsyncClient", side_effect=_counting_client),
                ):
                    loaded = await nmc._ensure_stations_loaded()
                self.assertEqual(len(loaded), 600)
                self.assertEqual(len(http_calls), calls_before_local)
                self.assertEqual(nmc.get_station_fetch_backoff_state()["fail_count"], 0)
                await nmc.close_async_client()

        try:
            asyncio.run(_run())
        finally:
            nmc._stations_list = orig_stations
            nmc._fetch_fail_count = orig_fail
            nmc._next_fetch_retry_at = orig_retry

    def test_c8_event_loop_call_later_no_threading_timer_and_snapshot_isolation(self):
        import ast
        import anima_memory_provider

        # 1. Static AST guarantee: zero threading.Timer calls/references across boot/start_with_env/patches
        scan_files = [
            os.path.join(APP_DIR, "boot.py"),
            os.path.join(APP_DIR, "start_with_env.py"),
            os.path.join(APP_DIR, "patches", "memory_hooks.py"),
            os.path.join(APP_DIR, "patches", "auth.py"),
            os.path.join(APP_DIR, "patches", "asr.py"),
        ]
        timer_nodes = []
        for sf in scan_files:
            with open(sf, "r", encoding="utf-8") as f:
                swe_tree = ast.parse(f.read(), filename=sf)
            for n in ast.walk(swe_tree):
                if (
                    isinstance(n, ast.Attribute)
                    and n.attr == "Timer"
                    and isinstance(n.value, ast.Name)
                    and n.value.id == "threading"
                ):
                    timer_nodes.append((sf, n.lineno))
        self.assertEqual(timer_nodes, [], f"C-8: must not instantiate threading.Timer: {timer_nodes}")

        # 2. Inside running asyncio event loop: uses loop.call_later + TimerHandle + snapshot copy
        pushed = []

        async def _run_async_c8():
            with (
                patch.object(anima_memory_provider, "push_turns_async", side_effect=lambda p: pushed.extend(p)),
                patch.object(start_with_env, "_FALLBACK_DEBOUNCE_SECONDS", 0.05),
            ):
                dlg = Dialogue()
                dlg._device_id = "dev-c8"
                dlg.put(Message(role="user", content="快照测试问题", uniq_id="c8-u1"))
                dlg.put(Message(role="assistant", content="[gentle] 原始完整回答。"))

                with start_with_env._timers_lock:
                    ent = start_with_env._debounce_timers.get(id(dlg))
                self.assertIsNotNone(ent)
                self.assertEqual(ent["kind"], "loop")
                self.assertIsInstance(ent["handle"], asyncio.TimerHandle)

                # Concurrently mutate dlg.dialogue list directly BEFORE call_later fires!
                dlg.dialogue.clear()
                await asyncio.sleep(0.10)

                self.assertEqual(len(pushed), 1, "Snapshot fallback flush must still emit original turn safely")
                self.assertEqual(pushed[0]["request_id"], "xz-c8-u1")
                self.assertEqual(pushed[0]["assistant_reply"], "原始完整回答。")

                # Cancellation test: tool message cancels TimerHandle before fire
                pushed.clear()
                dlg2 = Dialogue()
                dlg2.put(Message(role="user", content="工具取消测试", uniq_id="c8-u2"))
                dlg2.put(Message(role="assistant", content="正在查询"))
                with start_with_env._timers_lock:
                    h2 = start_with_env._debounce_timers[id(dlg2)]["handle"]
                dlg2.put(Message(role="tool", tool_call_id="t1", content="ok"))
                self.assertTrue(h2.cancelled())
                await asyncio.sleep(0.10)
                self.assertEqual(len(pushed), 0)

        asyncio.run(_run_async_c8())

    def test_g1_idle_triggered_gc_and_malloc_trim(self):
        self.assertGreaterEqual(start_with_env.IDLE_TRIM_DELAY_SECONDS, 20.0)
        self.assertLessEqual(start_with_env.IDLE_TRIM_DELAY_SECONDS, 30.0)

        gc_calls = []
        trim_calls = []
        busy_flag = {"busy": False}

        trimmer = start_with_env.IdleMemoryTrimmer(
            delay_seconds=0.08,
            busy_checker=lambda: busy_flag["busy"],
            trim_fn=lambda pad: trim_calls.append(pad),
            gc_fn=lambda: gc_calls.append(time.monotonic()),
        )
        trimmer.start()
        try:
            # 1. Idle with NO completed turn -> does NOT trim
            time.sleep(0.14)
            self.assertEqual(len(trim_calls), 0)

            # 2. Turn completes while busy -> suppressed (never collides with active speech!)
            busy_flag["busy"] = True
            trimmer.on_runtime_event({"type": "tts_state", "state": "stop"})
            time.sleep(0.14)
            self.assertEqual(len(trim_calls), 0)
            self.assertGreaterEqual(trimmer.stats()["suppressed_busy_count"], 1)

            # 3. Speech starts mid-countdown -> cancels pending idle trim
            busy_flag["busy"] = False
            trimmer.on_runtime_event({"type": "tts_state", "state": "stop"})
            time.sleep(0.03)
            trimmer.on_runtime_event({"type": "listen_start"})
            time.sleep(0.12)
            self.assertEqual(len(trim_calls), 0)

            # 4. Turn completes and stays idle for >= delay_seconds -> triggers gc.collect + malloc_trim(0) ONCE
            trimmer.on_runtime_event({"type": "tts_state", "state": "stop"})
            time.sleep(0.18)
            self.assertEqual(len(gc_calls), 1)
            self.assertEqual(trim_calls, [0])

            # 5. Continues idle without a new turn -> does NOT repeatedly re-trigger
            time.sleep(0.16)
            self.assertEqual(len(trim_calls), 1)
        finally:
            trimmer.stop()

    def test_g2_and_g4_dedicated_asr_executor_and_sherpa_output_corrections(self):
        import core.providers.asr.sherpa_onnx_local as sherpa_mod

        ex = start_with_env.get_asr_executor()
        self.assertIsInstance(ex, ThreadPoolExecutor)
        self.assertEqual(ex._max_workers, 1)
        self.assertEqual(ex._thread_name_prefix, "asr")

        seen_threads = []

        class _FakeStream:
            def __init__(self):
                self.result = SimpleNamespace(text="<|zh|><|NEUTRAL|>你好雷电牙医，牙医帮我查一下天气")

            def accept_waveform(self, sr, samples):
                pass

        class _FakeModel:
            def create_stream(self):
                return _FakeStream()

            def decode_stream(self, s):
                seen_threads.append(threading.current_thread().name)

        fake_asr = object.__new__(sherpa_mod.ASRProvider)
        fake_asr.model = _FakeModel()
        fake_asr.read_wave = lambda fp: ([0.0] * 160, 16000)

        with tempfile.TemporaryDirectory() as td:
            corr_file = os.path.join(td, "asr_corrections.json")
            asr_corrections.save_asr_corrections(
                {"雷电牙医": "雷电芽衣", "牙医": "芽衣"},
                enabled=True,
                path=corr_file,
            )
            artifacts = SimpleNamespace(file_path="/tmp/fake.wav")
            with patch.object(asr_corrections, "ASR_CORRECTIONS_PATH", corr_file):
                text, fp = asyncio.run(fake_asr.speech_to_text([b"\x00"], "sess-g2", artifacts=artifacts))

        self.assertEqual(fp, "/tmp/fake.wav")
        self.assertEqual(text, "你好雷电芽衣，芽衣帮我查一下天气")
        self.assertEqual(len(seen_threads), 1)
        self.assertTrue(
            seen_threads[0].startswith("asr"),
            f"Expected dedicated 'asr' executor thread, got {seen_threads[0]!r}",
        )

    def test_f1_patch_registration_hot_toggle_resumption_discard_story_mode_and_post_wakeup(self):
        st = patch_registry.get_patch_status("vad_speculative")
        self.assertIsNotNone(st)
        self.assertEqual(st["status"], "ok")
        self.assertTrue(st["contract_ok"])
        self.assertEqual(st["category"], "experience")

        class _SimAsr(ASRProviderBase):
            def __init__(self, decode_fn, *, delete_audio_file=True):
                super().__init__()
                self.interface_type = InterfaceType.LOCAL
                self.output_dir = "/tmp"
                self.delete_audio_file = delete_audio_file
                self._speculative_decode_pcm = decode_fn
                self.full_stt_calls = 0
                self.committed_texts = []

            async def speech_to_text(self, opus_data, session_id, artifacts=None):
                self.full_stt_calls += 1
                pcm = b"".join(opus_data)
                return self._speculative_decode_pcm(pcm), None

        def _make_sim_conn(asr, *, post_wakeup=False, dictation=False, story=False, dialogue_msgs=None):
            c = SimpleNamespace(
                session_id=f"sess-{time.monotonic_ns()}",
                asr=asr,
                vad=SimpleNamespace(silence_threshold_ms=1100),
                client_listen_mode="auto",
                client_have_voice=True,
                client_voice_stop=False,
                vad_last_voice_time=0.0,
                asr_audio=[b"\x01\x00" * 960 for _ in range(18)],  # > 15 frames (1.08s speech)
                voiceprint_provider=None,
                logger=MagicMock(),
                just_woken_up=False,
                _vad_post_wakeup_pending=post_wakeup,
                dictation_mode=dictation,
                story_mode=story,
                dialogue=SimpleNamespace(dialogue=list(dialogue_msgs or [])),
                config={},
            )

            def _reset():
                st_obj = getattr(c, "_vad_spec_state", None)
                if st_obj is not None:
                    st_obj.invalidate(count_discard=False)
                c.client_have_voice = False
                c.client_voice_stop = False
                c.asr_audio.clear()

            c.reset_audio_states = _reset
            return c

        async def _run_f1_behaviors():
            import core.providers.asr.base as asr_base_mod

            frame = b"\x00\x00" * 960  # 60ms silence frame

            # 1. Hot toggle OFF disables speculative trigger immediately (waits for 1100ms)
            vsr.reset_vad_speculative_stats()
            spec_calls = {"n": 0}

            def _dec_complete(_pcm):
                spec_calls["n"] += 1
                return "今天天气怎么样？"

            asr_off = _SimAsr(_dec_complete)
            conn_off = _make_sim_conn(asr_off)
            conn_off.vad_last_voice_time = (time.time() - 0.70) * 1000.0
            with (
                patch.object(rs, "get_vad_speculative", return_value={**rs.DEFAULT_SETTINGS["vad_speculative"], "enabled": False}),
                patch.object(asr_base_mod, "startToChat", new=AsyncMock()),
                patch.object(asr_base_mod, "enqueue_asr_report"),
            ):
                await asr_off.receive_audio(conn_off, frame, False)
                await asyncio.sleep(0.03)
            self.assertEqual(spec_calls["n"], 0)
            self.assertFalse(conn_off.client_voice_stop)

            # 2. Speech resumes while speculative decode is in-flight -> result discarded cleanly!
            vsr.reset_vad_speculative_stats()
            dec_started = threading.Event()
            dec_release = threading.Event()

            def _slow_dec(_pcm):
                dec_started.set()
                dec_release.wait(2.0)
                return "今天天气怎么样？"

            asr_res = _SimAsr(_slow_dec)
            conn_res = _make_sim_conn(asr_res)
            conn_res.vad_last_voice_time = (time.time() - 0.60) * 1000.0
            with (
                patch.object(rs, "get_vad_speculative", return_value=dict(rs.DEFAULT_SETTINGS["vad_speculative"])),
                patch.object(asr_base_mod, "startToChat", new=AsyncMock()),
                patch.object(asr_base_mod, "enqueue_asr_report"),
            ):
                await asr_res.receive_audio(conn_res, frame, False)
                await asyncio.sleep(0.02)
                self.assertTrue(dec_started.wait(1.0))
                # User resumes speaking mid-decode!
                await asr_res.receive_audio(conn_res, frame, True)
                dec_release.set()
                await asyncio.sleep(0.05)
                # Next brief silence frame MUST NOT commit the discarded speculative result!
                conn_res.vad_last_voice_time = (time.time() - 0.10) * 1000.0
                await asr_res.receive_audio(conn_res, frame, False)
            self.assertFalse(conn_res.client_voice_stop)
            self.assertGreaterEqual(vsr.get_vad_speculative_stats()["resumed_discards"], 1)

            # 3. Story / dictation mode auto-disables speculative commit (explicit flag + dialogue cue + >8s monologue)
            vsr.reset_vad_speculative_stats()
            spec_calls["n"] = 0
            asr_story = _SimAsr(_dec_complete)
            for c_story in (
                _make_sim_conn(asr_story, dictation=True),
                _make_sim_conn(asr_story, story=True),
                _make_sim_conn(asr_story, dialogue_msgs=[SimpleNamespace(role="user", content="开始听写模式", is_temporary=False)]),
            ):
                c_story.vad_last_voice_time = (time.time() - 0.70) * 1000.0
                with patch.object(rs, "get_vad_speculative", return_value=dict(rs.DEFAULT_SETTINGS["vad_speculative"])):
                    await asr_story.receive_audio(c_story, frame, False)
                    await asyncio.sleep(0.02)
                self.assertFalse(c_story.client_voice_stop)
            # Long monologue >= 8s PCM also skips speculative commit
            c_long = _make_sim_conn(asr_story)
            c_long.asr_audio = [b"\x01\x00" * 960 for _ in range(140)]  # 8.4s
            c_long.vad_last_voice_time = (time.time() - 0.70) * 1000.0
            with patch.object(rs, "get_vad_speculative", return_value=dict(rs.DEFAULT_SETTINGS["vad_speculative"])):
                await asr_story.receive_audio(c_long, frame, False)
                await asyncio.sleep(0.02)
            self.assertEqual(spec_calls["n"], 0)
            self.assertEqual(vsr.get_vad_speculative_stats()["story_mode_skips"], 4)

            # 4. First utterance right after wakeup uses conservative post_wakeup_ms (800ms), then 2nd turn uses 550ms
            #    AND early-commit reuses speculative text without double-decoding Sherpa!
            vsr.reset_vad_speculative_stats()
            spec_calls["n"] = 0
            asr_wk = _SimAsr(_dec_complete)
            conn_wk = _make_sim_conn(asr_wk, post_wakeup=True)
            chat_calls = []

            async def _cap_chat(c, txt):
                chat_calls.append(txt)

            with (
                patch.object(rs, "get_vad_speculative", return_value=dict(rs.DEFAULT_SETTINGS["vad_speculative"])),
                patch.object(asr_base_mod, "startToChat", side_effect=_cap_chat),
                patch.object(asr_base_mod, "enqueue_asr_report"),
            ):
                # At 600ms (< 800ms post_wakeup_ms): MUST NOT trigger yet!
                conn_wk.vad_last_voice_time = (time.time() - 0.60) * 1000.0
                await asr_wk.receive_audio(conn_wk, frame, False)
                await asyncio.sleep(0.03)
                self.assertEqual(spec_calls["n"], 0)
                self.assertFalse(conn_wk.client_voice_stop)

                # At 820ms (>= 800ms post_wakeup_ms): triggers speculative decode, next frame early-commits!
                conn_wk.vad_last_voice_time = (time.time() - 0.82) * 1000.0
                await asr_wk.receive_audio(conn_wk, frame, False)
                await asyncio.sleep(0.05)
                conn_wk.vad_last_voice_time = (time.time() - 0.88) * 1000.0
                await asr_wk.receive_audio(conn_wk, frame, False)

                self.assertEqual(chat_calls, ["今天天气怎么样？"])
                self.assertEqual(asr_wk.full_stt_calls, 0, "Early commit must reuse speculative text (0 double-decode)")
                self.assertFalse(conn_wk._vad_post_wakeup_pending, "Post-wakeup flag must clear after 1st turn")

                # 2nd turn now triggers at 560ms (standard speculative_ms=550ms)!
                conn_wk.client_have_voice = True
                conn_wk.client_voice_stop = False
                conn_wk.asr_audio = [b"\x01\x00" * 960 for _ in range(18)]
                conn_wk.vad_last_voice_time = (time.time() - 0.56) * 1000.0
                await asr_wk.receive_audio(conn_wk, frame, False)
                await asyncio.sleep(0.05)
                conn_wk.vad_last_voice_time = (time.time() - 0.62) * 1000.0
                await asr_wk.receive_audio(conn_wk, frame, False)
                self.assertEqual(len(chat_calls), 2)
                self.assertEqual(asr_wk.full_stt_calls, 0)
                self.assertGreaterEqual(vsr.get_vad_speculative_stats()["cache_reuses"], 2)

        asyncio.run(_run_f1_behaviors())

    def test_f1_34_case_breathing_pause_regression_and_tail_wait_benchmark(self):
        """34-case mid-sentence breathing-pause regression suite (target: 0% false cut <= baseline)
        + 20-case complete utterance benchmark (target: average tail wait reduction >= 0.30s).
        """
        import core.providers.asr.base as asr_base_mod

        # 34 real-world Mandarin mid-sentence breathing-pause cases (pauses 660ms..840ms between 550ms and 1100ms)
        mid_sentence_breathing_cases = [
            # (partial_before_breath, continuation_after_breath, pause_ms)
            ("我昨天晚上本来想早点睡的，然后", "突然又收到老板的消息了。", 720),
            ("我觉得这件事的核心问题就是", "大家没有把接口提前对齐。", 780),
            ("帮我找一下桌面上那个", "蓝色图标的文档好吗？", 660),
            ("我今天没出门，因为", "外面一直在下大雨。", 720),
            ("今天加班太晚了，所以", "我准备明天上午再过去。", 660),
            ("这个方案听起来不错，但是", "落地的成本可能会有点高。", 780),
            ("这个耳机音质挺好的，不过", "戴久了耳朵会有一点疼。", 720),
            ("这个模型不仅速度快，而且", "中文断句也比之前自然很多。", 780),
            ("明天中午我们吃面条或者", "去楼下那家湘菜馆吧。", 720),
            ("你是想先听个故事还是", "先查一下明天的天气呢？", 660),
            ("要是明天下午不下雨的话", "我们就去橘子洲散步吧。", 840),
            ("如果一会儿有空", "帮我把今天的会议纪要整理一下吧。", 720),
            ("虽然我刚才重启了一次", "但是网络还是连不上。", 780),
            ("因为今天工作太忙了", "所以我连晚饭都还没来得及吃呢。", 780),
            ("不但语音变清晰了", "而且响应速度也快了不少。", 720),
            ("比如说", "帮我把每天早上八点的闹钟打开。", 660),
            ("除了今天要交的报告，还有", "上周留下的两个表格要核对。", 720),
            ("顺便", "帮我查一下后天广州的天气吧。", 660),
            ("我想让你帮我", "把刚刚那段文字翻译成英文。", 720),
            ("你能不能", "提醒我半小时后去拿快递？", 660),
            ("请你帮我打开", "桌面上那个工作文件夹吧。", 780),
            ("你帮我查一下", "明天从长沙去北京的高铁。", 720),
            ("你帮我看看", "屏幕上这段报错是什么意思？", 720),
            ("我刚才准备", "出门买点水果顺便取快递。", 660),
            ("我在京东上买的那台新的", "机械键盘明天应该就到了。", 780),
            ("明天中午吃完饭以后", "记得提醒我吃感冒药。", 780),
            ("刚才我打开电脑的时候", "突然弹出来一个更新提示。", 780),
            ("我昨天买了", "一本关于人工智能的新书。", 720),
            ("我刚刚去了", "楼下便利店买了瓶热牛奶。", 720),
            ("他刚才跟我说了", "下周三要提前开项目评审会。", 720),
            ("我今天想去超市买点苹果和", "香蕉顺便再带两盒酸奶。", 660),
            ("嗯", "我想问一下明天会不会降温？", 720),
            ("今天下午两点，", "我有一个线上会议要参加。", 780),
            ("我今天去超市买了苹果", "还有香蕉和牛奶。", 780),
        ]
        self.assertGreaterEqual(len(mid_sentence_breathing_cases), 30)

        # 20 complete standalone utterances that SHOULD early-commit at ~550-610ms
        complete_utterances = [
            "今天长沙天气怎么样？",
            "明天会下雨吗？",
            "现在几点了？",
            "今天星期几呢？",
            "牙医今天开心吗？",
            "哥哥我先去休息啦",
            "早点睡觉吧",
            "这个主意太棒了",
            "今天工作已经做完了",
            "外面好像下雨了",
            "帮我打开微信",
            "帮我把浏览器打开",
            "把电脑音量调大一点",
            "帮我查一下明天上海天气",
            "帮我看看屏幕",
            "播放一首轻音乐",
            "暂停音乐",
            "帮我定个明早八点的闹钟",
            "谢谢你呀芽衣",
            "好啊没问题",
        ]

        class _ReplayAsr(ASRProviderBase):
            def __init__(self, text_by_phase):
                super().__init__()
                self.interface_type = InterfaceType.LOCAL
                self.output_dir = "/tmp"
                self.delete_audio_file = True
                self._text_by_phase = text_by_phase
                self._phase = "partial"
                self._speculative_decode_pcm = lambda _pcm: self._text_by_phase[self._phase]

            async def speech_to_text(self, opus_data, session_id, artifacts=None):
                return self._text_by_phase[self._phase], None

        async def _simulate_case(partial_txt: str, full_txt: str | None, pause_ms: int, *, speculative_enabled: bool):
            """Frame-by-frame 60ms simulation against real patched ASRProviderBase.receive_audio."""
            frame = b"\x00\x00" * 960
            asr = _ReplayAsr({"partial": partial_txt, "full": full_txt or partial_txt})
            committed = []

            async def _cap_chat(c, txt):
                committed.append(txt)

            conn = SimpleNamespace(
                session_id="sim-bench",
                asr=asr,
                vad=SimpleNamespace(silence_threshold_ms=1100),
                client_listen_mode="auto",
                client_have_voice=True,
                client_voice_stop=False,
                vad_last_voice_time=0.0,
                asr_audio=[b"\x01\x00" * 960 for _ in range(18)],
                voiceprint_provider=None,
                logger=MagicMock(),
                just_woken_up=False,
                _vad_post_wakeup_pending=False,
                dictation_mode=False,
                story_mode=False,
                dialogue=SimpleNamespace(dialogue=[]),
                config={},
            )

            def _reset():
                st_obj = getattr(conn, "_vad_spec_state", None)
                if st_obj is not None:
                    st_obj.invalidate(count_discard=False)
                conn.client_have_voice = False
                conn.client_voice_stop = False
                conn.asr_audio.clear()

            conn.reset_audio_states = _reset

            cfg = dict(rs.DEFAULT_SETTINGS["vad_speculative"])
            cfg["enabled"] = speculative_enabled

            false_cut = False
            commit_silence_ms = 1100

            with (
                patch.object(rs, "get_vad_speculative", return_value=cfg),
                patch.object(asr_base_mod, "startToChat", side_effect=_cap_chat),
                patch.object(asr_base_mod, "enqueue_asr_report"),
            ):
                if full_txt is not None:
                    # Mid-sentence breathing pause: advance silence in 60ms steps up to pause_ms, then resume!
                    for s_ms in range(60, pause_ms + 1, 60):
                        conn.vad_last_voice_time = (time.time() * 1000.0) - s_ms
                        await asr.receive_audio(conn, frame, False)
                        # Let any speculative decode task complete within the simulated 60ms frame window
                        st_obj = getattr(conn, "_vad_spec_state", None)
                        if st_obj is not None and st_obj.task is not None and not st_obj.task.done():
                            await st_obj.task
                        if committed:
                            false_cut = True
                            commit_silence_ms = s_ms
                            break
                    if not false_cut:
                        # User resumes speaking!
                        asr._phase = "full"
                        await asr.receive_audio(conn, frame, True)
                        # Then finishes sentence and reaches 1100ms (or early commit)
                        for s_ms in range(60, 1200, 60):
                            conn.vad_last_voice_time = (time.time() * 1000.0) - s_ms
                            if s_ms >= 1100:
                                conn.client_voice_stop = True
                            await asr.receive_audio(conn, frame, False)
                            st_obj = getattr(conn, "_vad_spec_state", None)
                            if st_obj is not None and st_obj.task is not None and not st_obj.task.done():
                                await st_obj.task
                            if committed:
                                commit_silence_ms = s_ms
                                break
                else:
                    # Complete utterance: advance silence in 60ms steps up to 1140ms
                    for s_ms in range(60, 1200, 60):
                        conn.vad_last_voice_time = (time.time() * 1000.0) - s_ms
                        if s_ms >= 1100:
                            conn.client_voice_stop = True
                        await asr.receive_audio(conn, frame, False)
                        st_obj = getattr(conn, "_vad_spec_state", None)
                        if st_obj is not None and st_obj.task is not None and not st_obj.task.done():
                            await st_obj.task
                        if committed:
                            commit_silence_ms = s_ms
                            break

            return false_cut, commit_silence_ms, committed

        async def _run_suite():
            vsr.reset_vad_speculative_stats()
            baseline_false_cuts = 0
            spec_false_cuts = 0
            false_cut_details = []

            for partial_txt, cont_txt, pause_ms in mid_sentence_breathing_cases:
                full_txt = partial_txt + cont_txt
                b_fc, _, _ = await _simulate_case(partial_txt, full_txt, pause_ms, speculative_enabled=False)
                s_fc, s_ms, s_comm = await _simulate_case(partial_txt, full_txt, pause_ms, speculative_enabled=True)
                if b_fc:
                    baseline_false_cuts += 1
                if s_fc:
                    spec_false_cuts += 1
                    false_cut_details.append((partial_txt, s_ms, s_comm))

            baseline_waits = []
            spec_waits = []
            missed_early = []
            for utt in complete_utterances:
                _, b_ms, b_comm = await _simulate_case(utt, None, 0, speculative_enabled=False)
                _, s_ms, s_comm = await _simulate_case(utt, None, 0, speculative_enabled=True)
                self.assertEqual(len(b_comm), 1)
                self.assertEqual(len(s_comm), 1)
                baseline_waits.append(b_ms)
                spec_waits.append(s_ms)
                if s_ms >= 1000:
                    missed_early.append((utt, s_ms))

            return baseline_false_cuts, spec_false_cuts, false_cut_details, baseline_waits, spec_waits, missed_early

        b_fc, s_fc, fc_details, b_waits, s_waits, missed = asyncio.run(_run_suite())
        n_breath = len(mid_sentence_breathing_cases)
        n_comp = len(complete_utterances)
        b_rate = b_fc / float(n_breath)
        s_rate = s_fc / float(n_breath)
        avg_b = sum(b_waits) / float(n_comp)
        avg_s = sum(s_waits) / float(n_comp)
        avg_saved_ms = avg_b - avg_s

        print(
            f"\n[F-1 Benchmark] breathing_cases={n_breath} | false_cut: spec={s_fc}/{n_breath} ({s_rate*100:.1f}%) "
            f"vs baseline={b_fc}/{n_breath} ({b_rate*100:.1f}%) | "
            f"complete_cases={n_comp} | avg_wait: baseline={avg_b:.0f}ms -> spec={avg_s:.0f}ms "
            f"(avg_saved={avg_saved_ms:.0f}ms / {avg_saved_ms/1000.0:.3f}s)",
            flush=True,
        )

        self.assertEqual(s_fc, 0, f"Unexpected false cuts on mid-sentence breathing pauses: {fc_details}")
        self.assertLessEqual(s_rate, b_rate)
        self.assertEqual(missed, [], f"Complete utterances that failed to early-commit: {missed}")
        self.assertGreaterEqual(
            avg_saved_ms,
            300.0,
            f"Expected >=300ms average tail wait reduction, got {avg_saved_ms:.1f}ms",
        )

    def test_p1_6_tool_continuation_depth_preserves_turn_context_and_prefix_cache(self):
        """P1-6: Tool-call continuation (depth > 0, memory_str=None) keeps depth=0 <turn_context> & <memory> byte-identical."""
        from core.utils.dialogue import Dialogue, Message
        import boot
        import turn_context_router as tcr

        boot.install_all(start_background_workers=False, run_contract_self_check=False)

        dlg = Dialogue()
        dlg.put(Message(role="system", content="你是雷电芽衣。\n<context>当前时间 {{current_time}}</context>"))
        dlg.put(Message(role="user", content="帮我查一下明天天气"))

        # depth=0 call: real memory_str provided
        dlg._in_chat_depth = 1
        with patch.object(tcr, "get_turn_context_mode", return_value="split"):
            msgs_d0 = dlg.get_llm_dialogue_with_memory("用户住在长沙，喜欢晴天", {}, None)
            self.assertEqual(msgs_d0[0]["role"], "system")
            self.assertIn("<memory>", msgs_d0[1]["content"])
            self.assertIn("用户住在长沙，喜欢晴天", msgs_d0[1]["content"])

            # Upstream appends assistant(tool_calls) + tool(result), then calls chat(None, depth=1) -> memory_str=None!
            dlg.put(
                Message(
                    role="assistant",
                    tool_calls=[
                        {"id": "tc-1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
                    ],
                )
            )
            dlg.put(Message(role="tool", tool_call_id="tc-1", content="长沙明天晴 24°C"))
            dlg._in_chat_depth = 2

            msgs_d1 = dlg.get_llm_dialogue_with_memory(None, {}, None)
            # Prefix up to user[n] MUST be 100% byte-identical to depth=0 (preserving <memory> and KV prefix cache!)
            self.assertEqual(msgs_d1[0], msgs_d0[0])
            self.assertEqual(msgs_d1[1], msgs_d0[1])
            self.assertIn("用户住在长沙，喜欢晴天", msgs_d1[1]["content"])

            # Even at MAX_DEPTH when upstream appends synthetic "[系统提示]" user message,
            # the real user[n] message STILL retains the exact depth=0 <turn_context>!
            dlg.put(Message(role="user", content="[系统提示] 已达到最大工具调用次数限制，请直接回答。"))
            msgs_d5 = dlg.get_llm_dialogue_with_memory(None, {}, None)
            self.assertEqual(msgs_d5[1], msgs_d0[1])
            self.assertNotIn("<turn_context>", msgs_d5[-1]["content"])

            # A brand new real user turn MUST generate a fresh <turn_context> (not reuse previous turn's)
            dlg._in_chat_depth = 1
            dlg.put(Message(role="user", content="谢谢芽衣"))
            msgs_next = dlg.get_llm_dialogue_with_memory("新记忆片段", {}, None)
            self.assertIn("新记忆片段", msgs_next[-1]["content"])
            self.assertNotIn("用户住在长沙，喜欢晴天", msgs_next[-1]["content"])

    def test_p1_12_vad_hard_cap_handoff_cancel_last_is_voice_and_persistent_dictation(self):
        """P1-12: 1100ms hard cap reuses completed or cancels in-flight speculative decode; last_is_voice guards opening frames; dictation persists >4 turns + storyteller preset."""
        import core.providers.asr.base as asr_base_mod

        class _TestAsr(ASRProviderBase):
            def __init__(self, decode_fn):
                super().__init__()
                self.interface_type = InterfaceType.LOCAL
                self.output_dir = "/tmp"
                self.delete_audio_file = True
                self._speculative_decode_pcm = decode_fn
                self.full_stt_calls = 0

            async def speech_to_text(self, opus_data, session_id, artifacts=None):
                self.full_stt_calls += 1
                return "full-stt-result", None

        def _make_conn(asr, *, dialogue_msgs=None):
            c = SimpleNamespace(
                session_id=f"p112-{time.monotonic_ns()}",
                asr=asr,
                vad=SimpleNamespace(silence_threshold_ms=1100),
                client_listen_mode="auto",
                client_have_voice=True,
                client_voice_stop=False,
                last_is_voice=False,
                vad_last_voice_time=0.0,
                asr_audio=[b"\x01\x00" * 960 for _ in range(18)],
                voiceprint_provider=None,
                logger=MagicMock(),
                just_woken_up=False,
                _vad_post_wakeup_pending=False,
                dictation_mode=False,
                story_mode=False,
                dialogue=SimpleNamespace(dialogue=list(dialogue_msgs or [])),
                config={},
            )

            def _reset():
                st_obj = getattr(c, "_vad_spec_state", None)
                if st_obj is not None:
                    st_obj.invalidate(count_discard=False)
                c.client_have_voice = False
                c.client_voice_stop = False
                c.asr_audio.clear()

            c.reset_audio_states = _reset
            return c

        async def _run():
            frame = b"\x00\x00" * 960
            cfg = dict(rs.DEFAULT_SETTINGS["vad_speculative"])

            # 1. Completed speculative decode (held because incomplete) is handed off at 1100ms hard cap -> 0 double decode!
            asr1 = _TestAsr(lambda _pcm: "帮我打开")  # incomplete bare transitive -> held until 1100ms
            conn1 = _make_conn(asr1)
            chats1 = []

            async def _cap1(c, txt):
                chats1.append(txt)

            with (
                patch.object(rs, "get_vad_speculative", return_value=cfg),
                patch.object(rs, "get_reply_length", return_value={"preset": "balanced", "chat_chars": 150, "story_chars": 450}),
                patch.object(asr_base_mod, "startToChat", side_effect=_cap1),
                patch.object(asr_base_mod, "enqueue_asr_report"),
            ):
                conn1.vad_last_voice_time = (time.time() - 0.60) * 1000.0
                await asr1.receive_audio(conn1, frame, False)
                st1 = getattr(conn1, "_vad_spec_state")
                self.assertIsNotNone(st1.task)
                await st1.task
                self.assertFalse(st1.should_commit)
                # Append ~500ms more silence frames up to 1100ms hard cap
                for _ in range(8):
                    conn1.asr_audio.append(frame)
                conn1.client_voice_stop = True
                await asr1.receive_audio(conn1, frame, False)
                self.assertEqual(chats1, ["帮我打开"])
                self.assertEqual(asr1.full_stt_calls, 0, "1100ms hard cap must reuse completed speculative result")

            # 2. In-flight speculative task is cancelled immediately when 1100ms hard cap arrives
            started_ev = threading.Event()
            release_ev = threading.Event()

            def _hang_dec(_pcm):
                started_ev.set()
                release_ev.wait(2.0)
                return "迟到结果"

            asr2 = _TestAsr(_hang_dec)
            conn2 = _make_conn(asr2)
            with (
                patch.object(rs, "get_vad_speculative", return_value=cfg),
                patch.object(rs, "get_reply_length", return_value={"preset": "balanced", "chat_chars": 150, "story_chars": 450}),
                patch.object(asr_base_mod, "startToChat", new=AsyncMock()),
                patch.object(asr_base_mod, "enqueue_asr_report"),
            ):
                conn2.vad_last_voice_time = (time.time() - 0.60) * 1000.0
                await asr2.receive_audio(conn2, frame, False)
                await asyncio.sleep(0.02)
                self.assertTrue(started_ev.wait(1.0))
                st2 = getattr(conn2, "_vad_spec_state")
                in_flight_task = st2.task
                gen_before = st2.generation
                conn2.client_voice_stop = True
                await asr2.receive_audio(conn2, frame, False)
                self.assertGreater(st2.generation, gen_before)
                self.assertIsNone(st2.task)
                release_ev.set()
                await asyncio.sleep(0.02)
                self.assertTrue(in_flight_task.cancelled() or in_flight_task.done())

            # 3. conn.last_is_voice=True prevents early-commit from cutting off resumed speech's opening frame
            asr3 = _TestAsr(lambda _pcm: "今天天气怎么样？")
            conn3 = _make_conn(asr3)
            with (
                patch.object(rs, "get_vad_speculative", return_value=cfg),
                patch.object(rs, "get_reply_length", return_value={"preset": "balanced", "chat_chars": 150, "story_chars": 450}),
                patch.object(asr_base_mod, "startToChat", new=AsyncMock()),
                patch.object(asr_base_mod, "enqueue_asr_report"),
            ):
                conn3.vad_last_voice_time = (time.time() - 0.60) * 1000.0
                await asr3.receive_audio(conn3, frame, False)
                await getattr(conn3, "_vad_spec_state").task
                # First frame of user's continuation: Silero sets last_is_voice=True while window count < threshold (audio_have_voice=False)
                conn3.last_is_voice = True
                conn3.vad_last_voice_time = (time.time() - 0.66) * 1000.0
                await asr3.receive_audio(conn3, frame, False)
                self.assertFalse(conn3.client_voice_stop, "Must NOT early-commit while Silero last_is_voice=True!")

            # 3b. Short 1-2 frame speech burst (last_is_voice=True, audio_have_voice=False) between 550ms and 1100ms
            # MUST invalidate stale speculative text so full 1100ms ASR runs instead of dropping the short syllable!
            asr4 = _TestAsr(lambda _pcm: "今天天气")  # incomplete -> held until 1100ms
            conn4 = _make_conn(asr4)
            chats4 = []

            async def _cap4(c, txt):
                chats4.append(txt)

            with (
                patch.object(rs, "get_vad_speculative", return_value=cfg),
                patch.object(rs, "get_reply_length", return_value={"preset": "balanced", "chat_chars": 150, "story_chars": 450}),
                patch.object(asr_base_mod, "startToChat", side_effect=_cap4),
                patch.object(asr_base_mod, "enqueue_asr_report"),
            ):
                conn4.vad_last_voice_time = (time.time() - 0.60) * 1000.0
                await asr4.receive_audio(conn4, frame, False)
                await getattr(conn4, "_vad_spec_state").task
                # User utters a short 1-frame syllable ("吗"): last_is_voice=True while audio_have_voice stays False
                conn4.last_is_voice = True
                await asr4.receive_audio(conn4, frame, False)
                # User stops again and hits 1100ms cutoff
                conn4.last_is_voice = False
                conn4.client_voice_stop = True
                await asr4.receive_audio(conn4, frame, False)
                self.assertEqual(asr4.full_stt_calls, 1, "Short last_is_voice burst must invalidate stale speculative decode")
                self.assertEqual(chats4, ["full-stt-result"])

        asyncio.run(_run())

        # 4. Dictation cue persists past 5+ turns (and even if cue scrolls out of dialogue) until exit cue; storyteller preset also disables speculative VAD
        dlg_list = [SimpleNamespace(role="user", content="开始听写模式，帮我记一段笔记", is_temporary=False)]
        conn_d = SimpleNamespace(
            client_listen_mode="auto",
            dictation_mode=False,
            story_mode=False,
            vad_speculative_disabled=False,
            config={},
            dialogue=SimpleNamespace(dialogue=dlg_list),
        )
        with patch.object(rs, "get_reply_length", return_value={"preset": "balanced", "chat_chars": 150, "story_chars": 450}):
            self.assertTrue(vsr.is_story_or_dictation_mode(conn_d))
            for i in range(6):
                dlg_list.append(SimpleNamespace(role="user", content=f"第{i+1}条待办事项内容", is_temporary=False))
                self.assertTrue(vsr.is_story_or_dictation_mode(conn_d), f"Dictation mode must persist on turn {i+2}")
            # Even if the original enter-cue message is evicted from sliding dialogue history, state stays active
            del dlg_list[0]
            self.assertTrue(vsr.is_story_or_dictation_mode(conn_d))
            # Explicit exit cue turns dictation mode back off
            dlg_list.append(SimpleNamespace(role="user", content="好了说完了，退出听写", is_temporary=False))
            self.assertFalse(vsr.is_story_or_dictation_mode(conn_d))

        # storyteller reply-length preset disables speculative early-commit automatically
        conn_st = SimpleNamespace(
            client_listen_mode="auto",
            dictation_mode=False,
            story_mode=False,
            vad_speculative_disabled=False,
            config={},
            dialogue=SimpleNamespace(dialogue=[]),
        )
        with patch.object(rs, "get_reply_length", return_value={"preset": "storyteller", "chat_chars": 200, "story_chars": 1000}):
            self.assertTrue(vsr.is_story_or_dictation_mode(conn_st))
            # Enter dictation cue while storyteller preset is ON must still be synchronized when storyteller turns OFF
            conn_st.dialogue.dialogue.append(SimpleNamespace(role="user", content="开始听写模式", is_temporary=False))
            self.assertTrue(vsr.is_story_or_dictation_mode(conn_st))
        with patch.object(rs, "get_reply_length", return_value={"preset": "balanced", "chat_chars": 150, "story_chars": 450}):
            self.assertTrue(vsr.is_story_or_dictation_mode(conn_st))
            # Clearing or replacing conn.dialogue must cleanly reset persisted dictation state
            conn_st.dialogue.dialogue.clear()
            self.assertFalse(vsr.is_story_or_dictation_mode(conn_st))
            conn_st.dialogue.dialogue.append(SimpleNamespace(role="user", content="开始听写模式", is_temporary=False))
            self.assertTrue(vsr.is_story_or_dictation_mode(conn_st))
            conn_st.dialogue = SimpleNamespace(
                dialogue=[SimpleNamespace(role="user", content="今天天气怎么样", is_temporary=False)]
            )
            self.assertFalse(vsr.is_story_or_dictation_mode(conn_st))

    def test_p2_turn_context_anchors_stream_options_and_parity_d7_f4_g4(self):
        """4.1 #4c & 4.2 D-7/F-4/G-4: line-anchored tags, generic 400 stream_options retry, voice_id priority, F-4 dead keys removed, G-4 FunASR corrections."""
        import asr_corrections
        import turn_context_router as tcr
        from patches import asr as asr_patch
        from patches.emotion import tags as emo_tags
        from patches.emotion import tts_route

        # 1. Line-anchored <turn_context> and <memory> preserve inline prose references
        inline_tc = "用户可以引用 <turn_context>示例内容</turn_context> 而不被误删。"
        self.assertEqual(tcr.strip_turn_context_from_text(inline_tc), inline_tc)

        wrapped_tc = "<turn_context>\n<memory>\n旧记忆\n</memory>\n</turn_context>\n\n真正用户问题"
        self.assertEqual(tcr.strip_turn_context_from_text(wrapped_tc), "真正用户问题")

        inline_mem_sys = "你的人设：请参考 <memory>标签说明</memory> 中的格式来回答。"
        out_msgs = tcr.apply_turn_context_to_messages(
            [{"role": "system", "content": inline_mem_sys}, {"role": "user", "content": "你好"}],
            memory_str="长期事实A",
            mode="split",
        )
        self.assertIn("<memory>标签说明</memory>", out_msgs[0]["content"])

        # 2. Generic HTTP 400/422 unknown-field rejection triggers stream_options fallback even without literal 'stream_options'
        calls = []

        class _Err400(RuntimeError):
            status_code = 400

        class _FakeCompletions:
            def create(self, *args, **kwargs):
                calls.append(dict(kwargs))
                if "stream_options" in kwargs:
                    raise _Err400("400 Bad Request: unknown field in request payload")
                return iter([])

        fake_llm = SimpleNamespace(
            model_name="proxy-model",
            client=SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions())),
        )
        self.assertTrue(tcr.wrap_llm_client_for_usage(fake_llm))
        list(fake_llm.client.chat.completions.create(model="proxy-model", messages=[], stream=True))
        self.assertEqual(len(calls), 2)
        self.assertIn("stream_options", calls[0])
        self.assertNotIn("stream_options", calls[1])

        # 3. D-7: is_raiden_mei_voice prioritizes explicit voice_id over ref_audio_path substring
        tts_non_mei = SimpleNamespace(
            voice_id="__gsv_xiaozhi_tw__",
            ref_audio_path="/voices/raiden_mei/copied_ref.wav",
            config={"voice_id": "__gsv_xiaozhi_tw__"},
        )
        self.assertFalse(emo_tags.is_raiden_mei_voice(tts_non_mei.ref_audio_path, tts=tts_non_mei))

        tts_explicit_mei = SimpleNamespace(
            voice_id="__gsv_raiden_mei__",
            ref_audio_path="/voices/custom_renamed/ref.wav",
            config={"voice_id": "__gsv_raiden_mei__"},
        )
        self.assertTrue(emo_tags.is_raiden_mei_voice(tts_explicit_mei.ref_audio_path, tts=tts_explicit_mei))
        # Legacy fallback without voice_id still inspects ref_audio_path
        tts_legacy = SimpleNamespace(ref_audio_path="/voices/raiden_mei/mei_calm_ref.wav", config={})
        self.assertTrue(emo_tags.is_raiden_mei_voice(tts_legacy.ref_audio_path, tts=tts_legacy))

        # 4. F-4: build_gsv_request_snapshot omits dead keys split_bucket & batch_threshold
        fake_provider = SimpleNamespace(
            url="http://host.docker.internal:19880/tts",
            ref_audio_path="/voices/custom/ref.wav",
            prompt_text="你好",
            prompt_lang="zh",
            text_lang="zh",
            top_k=12,
            top_p=1.0,
            temperature=0.98,
            speed_factor=1.0,
            text_split_method="cut0",
            batch_size=1,
            batch_threshold=0.75,
            split_bucket=True,
            return_fragment=False,
            streaming_mode=False,
            seed=-1,
            parallel_infer=True,
            repetition_penalty=1.35,
            config={"voice_id": "__gsv_xiaozhi_tw__"},
        )
        _, req_json, _, _ = tts_route.build_gsv_request_snapshot(fake_provider, "测试语音")
        self.assertNotIn("split_bucket", req_json)
        self.assertNotIn("batch_threshold", req_json)

        # 5. G-4: ASRProviderBase.speech_to_text_wrapper applies homophone corrections to FunASR dict & non-Sherpa ASR outputs
        asr_patch.install_memory_optimizations(start_trimmer=False)
        from core.providers.asr.base import ASRProviderBase

        class _FakeFunASR(ASRProviderBase):
            def __init__(self):
                self.output_dir = "/tmp"
                self.delete_audio_file = True

            def requires_file(self):
                return False

            async def speech_to_text(self, opus_data, session_id, artifacts=None):
                return {"content": "你好雷电牙医", "language": "zh", "emotion": "NEUTRAL"}, None

        class _FakeGenericASR(ASRProviderBase):
            def __init__(self):
                self.output_dir = "/tmp"
                self.delete_audio_file = True

            def requires_file(self):
                return False

            async def speech_to_text(self, opus_data, session_id, artifacts=None):
                return "牙医帮我查天气", None

        async def _run_g4():
            with patch.object(
                asr_corrections,
                "apply_asr_corrections",
                side_effect=lambda t: t.replace("雷电牙医", "雷电芽衣").replace("牙医", "芽衣"),
            ):
                fun_res, _ = await _FakeFunASR().speech_to_text_wrapper([b"\x00\x00" * 160], "sess-fun")
                gen_res, _ = await _FakeGenericASR().speech_to_text_wrapper([b"\x00\x00" * 160], "sess-gen")
            self.assertEqual(fun_res["content"], "你好雷电芽衣")
            self.assertEqual(fun_res["emotion"], "NEUTRAL")
            self.assertEqual(gen_res, "芽衣帮我查天气")

        asyncio.run(_run_g4())


if __name__ == "__main__":
    unittest.main(verbosity=2)

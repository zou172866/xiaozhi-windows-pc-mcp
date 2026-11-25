"""Offline tests for Phase 2 voiceprint speaker guard, default-off zero-memory invariants,
privacy-guarded screen peek orchestration, and 18080 Console widget/voiceprint routes."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
import wave

import numpy as np

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

import anima_memory_provider
import proactive_companion
import runtime_events
import runtime_settings
import turn_context_router
import vocal_cue_router
import voiceprint_router
from console.routes import voiceprint as vp_routes


def _make_pcm16_tone(freq_hz: float, duration_sec: float = 1.2, sr: int = 16000) -> bytes:
    t = np.linspace(0.0, duration_sec, int(sr * duration_sec), endpoint=False, dtype=np.float32)
    wave_f = 0.5 * np.sin(2.0 * np.pi * freq_hz * t)
    return np.clip(np.round(wave_f * 32767.0), -32768, 32767).astype(np.int16).tobytes()


def _make_wav_bytes(freq_hz: float, *, sr: int = 48000, channels: int = 2, duration_sec: float = 1.2) -> bytes:
    t = np.linspace(0.0, duration_sec, int(sr * duration_sec), endpoint=False, dtype=np.float32)
    mono = np.clip(np.round(0.5 * np.sin(2.0 * np.pi * freq_hz * t) * 32767.0), -32768, 32767).astype(np.int16)
    samples = np.repeat(mono[:, None], channels, axis=1).reshape(-1) if channels == 2 else mono
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


class _DeterministicToneExtractor:
    """Offline stand-in for Sherpa SpeakerEmbeddingExtractor mapping dominant tone frequency -> unit vector."""

    def __init__(self, _model_path: str) -> None:
        self.dim = 4
        self.calls = 0

    def compute(self, samples_f32: np.ndarray, sample_rate: int = 16000) -> list[float]:
        self.calls += 1
        n = min(samples_f32.size, sample_rate)
        spec = np.abs(np.fft.rfft(samples_f32[:n]))
        freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
        peak_hz = float(freqs[int(np.argmax(spec))]) if spec.size else 0.0
        # Map 3 distinct speaker bands cleanly to orthogonal embeddings
        if peak_hz < 320.0:
            return [1.0, 0.0, 0.0, 0.0]  # owner (~220Hz)
        if peak_hz < 560.0:
            return [0.0, 1.0, 0.0, 0.0]  # family (~440Hz)
        return [0.0, 0.0, 1.0, 0.0]      # guest (~880Hz)


class TestPhase2DefaultOffZeroMemoryInvariants(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings_path = os.path.join(self._tmp.name, "runtime_settings.json")
        self.store_path = os.path.join(self._tmp.name, "voiceprints.json")
        self.model_dir = os.path.join(self._tmp.name, "speaker_models")
        os.makedirs(self.model_dir, exist_ok=True)
        runtime_events.reset_runtime_events()
        vocal_cue_router.reset_vocal_cues()

    def tearDown(self):
        voiceprint_router.reset_default_router_for_tests(None)
        runtime_events.reset_runtime_events()
        vocal_cue_router.reset_vocal_cues()
        self._tmp.cleanup()

    def test_zero_import_side_effects_for_phase2_modules(self):
        probe = (
            "import json, sys, threading; "
            f"sys.path.insert(0, {APP_DIR!r}); "
            "th0 = {t.name for t in threading.enumerate()}; "
            "import voiceprint_router; "
            "from patches import voiceprint as vp_patch; "
            "from console.routes import voiceprint as vp_route; "
            "th1 = {t.name for t in threading.enumerate()}; "
            "print(json.dumps({'new_threads': sorted(th1 - th0), 'loaded': voiceprint_router.get_voiceprint_router().is_model_loaded()}))"
        )
        proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(data["new_threads"], [])
        self.assertFalse(data["loaded"])

    def test_all_new_switches_default_off_with_zero_threads_and_zero_model_load(self):
        self.assertFalse(runtime_settings.get_proactive(self.settings_path)["enabled"])
        self.assertFalse(runtime_settings.get_proactive(self.settings_path)["inner_life_enabled"])
        self.assertFalse(runtime_settings.get_vocal_cue(self.settings_path)["enabled"])
        self.assertFalse(runtime_settings.get_voiceprint(self.settings_path)["enabled"])
        self.assertEqual(runtime_settings.get_voiceprint(self.settings_path)["policy"], "full_guard")
        self.assertFalse(runtime_settings.get_screen_peek(self.settings_path)["enabled"])

        # 1. Proactive companion MUST NOT spawn heartbeat thread when enabled=False
        orig_get_pro = runtime_settings.get_proactive
        runtime_settings.get_proactive = lambda sp=None: orig_get_pro(self.settings_path)
        try:
            th_before = {t.name for t in threading.enumerate()}
            eng_opt = proactive_companion.start_proactive_companion()
            self.assertIsNone(eng_opt)
            self.assertEqual({t.name for t in threading.enumerate()} - th_before, set())
        finally:
            runtime_settings.get_proactive = orig_get_pro

        # 2. Vocal cue router MUST drop/no-op when vocal_cue.enabled=False
        orig_get_vc = runtime_settings.get_vocal_cue
        runtime_settings.get_vocal_cue = lambda sp=None: orig_get_vc(self.settings_path)
        try:
            cue = vocal_cue_router.extract_vocal_cue("<|SAD|><|Sigh|>累了")
            self.assertIsNotNone(cue)
            self.assertIsNone(vocal_cue_router.record_vocal_cue("s-off", cue))
            self.assertEqual(vocal_cue_router.consume_vocal_cue("s-off"), "")
        finally:
            runtime_settings.get_vocal_cue = orig_get_vc

        # 3. Voiceprint router MUST NOT load extractor or buffer PCM when voiceprint.enabled=False
        factory_calls = []

        def _counting_factory(path):
            factory_calls.append(path)
            return _DeterministicToneExtractor(path)

        vp = voiceprint_router.VoiceprintRouter(
            store_path=self.store_path,
            model_dir=self.model_dir,
            settings_path=self.settings_path,
            extractor_factory=_counting_factory,
        )
        pcm_owner = _make_pcm16_tone(220.0)
        self.assertIsNone(vp.identify_turn_pcm("s-1", pcm_owner, transcript="你好"))
        self.assertEqual(factory_calls, [])
        self.assertFalse(vp.is_model_loaded())
        self.assertIsNone(vp.get_recent_pcm_info())

        # 4. Screen peek MUST reject cleanly when screen_peek.enabled=False
        peek_res = proactive_companion.run_on_demand_screen_peek(
            question="看屏幕",
            settings_path=self.settings_path,
            peek_fetcher=lambda _q: (_ for _ in ()).throw(AssertionError("should not fetch when disabled")),
        )
        self.assertFalse(peek_res["ok"])
        self.assertEqual(peek_res["reason"], "screen_peek_disabled")


class TestPhase2VoiceprintLifecycleAndGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings_path = os.path.join(self._tmp.name, "runtime_settings.json")
        self.store_path = os.path.join(self._tmp.name, "voiceprints.json")
        self.model_dir = os.path.join(self._tmp.name, "speaker_models")
        os.makedirs(self.model_dir, exist_ok=True)
        runtime_events.reset_runtime_events()
        self.clock = {"ts": 1_780_000_000.0}
        self.vp = voiceprint_router.VoiceprintRouter(
            store_path=self.store_path,
            model_dir=self.model_dir,
            settings_path=self.settings_path,
            extractor_factory=lambda p: _DeterministicToneExtractor(p),
            now_fn=lambda: self.clock["ts"],
        )

    def tearDown(self):
        voiceprint_router.reset_default_router_for_tests(None)
        runtime_events.reset_runtime_events()
        self._tmp.cleanup()

    def test_zero_lockout_enroll_identify_debounce_guard_and_unload(self):
        runtime_settings.set_voiceprint(enabled=True, policy="full_guard", threshold=0.58, settings_path=self.settings_path)
        pcm_owner = _make_pcm16_tone(220.0)
        pcm_family = _make_pcm16_tone(440.0)
        pcm_guest = _make_pcm16_tone(880.0)
        pcm_short = _make_pcm16_tone(880.0, duration_sec=0.3)

        # 1. Before owner is enrolled -> zero lockout (passthrough), yet recent PCM is cached for one-click enroll!
        r_pre = self.vp.identify_turn_pcm("s-main", pcm_owner, transcript="你好芽衣我是主人")
        self.assertFalse(r_pre["guard_active"])
        self.assertEqual(r_pre["role"], "owner_passthrough")
        ok_pre, _ = self.vp.check_mcp_tool_permission("s-main", "pc_ui_action")
        self.assertTrue(ok_pre)
        self.assertTrue(self.vp.get_recent_pcm_info()["available"])

        # 2. One-click enroll owner from last utterance -> guard immediately becomes active!
        owner_prof = self.vp.enroll_last_utterance(display_name="主人", role="owner")
        self.assertEqual(owner_prof["role"], "owner")
        self.assertEqual(owner_prof["character_id"], "")
        self.assertTrue(self.vp.is_guard_active())
        self.assertTrue(self.vp.is_model_loaded())

        # 3. Enroll family member via 48kHz stereo WAV (verifies resampling + mono downmix + isolated character_id)
        fam_wav = _make_wav_bytes(440.0, sr=48000, channels=2, duration_sec=1.2)
        fam_prof = self.vp.enroll_wav_bytes(fam_wav, display_name="妹妹", role="family", base_character_id="xiaozhi")
        self.assertEqual(fam_prof["role"], "family")
        self.assertTrue(fam_prof["character_id"].startswith("xiaozhi_spk_"))

        # 4. Identify Owner -> main character (no override), MCP allowed, <speakers_info> says owner
        r_own = self.vp.identify_turn_pcm("s-main", pcm_owner, transcript="帮我点开浏览器")
        self.assertTrue(r_own["guard_active"])
        self.assertEqual(r_own["role"], "owner")
        self.assertIsNone(self.vp.get_speaker_character_override("s-main"))
        ok_own, _ = self.vp.check_mcp_tool_permission("s-main", "pc_ui_action")
        self.assertTrue(ok_own)
        self.assertIn("主人", self.vp.format_speakers_info_block("s-main"))

        # 5. Short utterance (<0.8s) within 60s reuses owner identity (no false flip to guest)
        self.clock["ts"] += 20.0
        r_short = self.vp.identify_turn_pcm("s-main", pcm_short, transcript="好的")
        self.assertEqual(r_short["role"], "owner")
        self.assertTrue(r_short["reused_short"])

        # 6. Identify Family -> isolated character_id, privileged MCP blocked under full_guard, non-host tool allowed
        self.clock["ts"] += 5.0
        r_fam = self.vp.identify_turn_pcm("s-main", pcm_family, transcript="帮我操作电脑")
        self.assertEqual(r_fam["role"], "family")
        self.assertEqual(self.vp.get_speaker_character_override("s-main"), fam_prof["character_id"])
        ok_fam_pc, msg_fam = self.vp.check_mcp_tool_permission("s-main", "pc_agent_task")
        self.assertFalse(ok_fam_pc)
        self.assertIn("声纹安全拦截", msg_fam)
        ok_fam_weather, _ = self.vp.check_mcp_tool_permission("s-main", "get_weather")
        self.assertTrue(ok_fam_weather)

        # 7. Identify Guest -> xiaozhi_guest (and custom base_character_id) isolation & MCP blocked (incl. pc_check_system_status)
        r_guest = self.vp.identify_turn_pcm("s-main", pcm_guest, transcript="打开终端")
        self.assertEqual(r_guest["role"], "guest")
        self.assertEqual(self.vp.get_speaker_character_override("s-main"), "xiaozhi_guest")
        self.assertEqual(
            self.vp.get_speaker_character_override("s-main", base_character_id="raiden_mei"),
            "raiden_mei_guest",
        )
        ok_guest_pc, _ = self.vp.check_mcp_tool_permission("s-main", "pc_system_control")
        self.assertFalse(ok_guest_pc)
        ok_guest_sys, _ = self.vp.check_mcp_tool_permission("s-main", "pc_check_system_status")
        self.assertFalse(ok_guest_sys)

        # 8. Switch policy to isolate_memory -> guest memory stays isolated, MCP tools unblocked
        runtime_settings.set_voiceprint(policy="isolate_memory", settings_path=self.settings_path)
        self.assertEqual(self.vp.get_speaker_character_override("s-main"), "xiaozhi_guest")
        ok_iso, _ = self.vp.check_mcp_tool_permission("s-main", "pc_system_control")
        self.assertTrue(ok_iso)

        # 9. Verify cross-thread _DeviceBoundMemoryProxy speaker isolation (owner tavern/handoff NEVER leaks to guest!)
        seen_targets = []

        class _StubBaseProvider:
            _recall_cache = {}
            _bg_refresh_tasks = {}

            async def query_memory(self, query, *, device_id=None, target=None):
                seen_targets.append(("query", target))
                return ""

            async def save_memory(self, msgs, session_id=None, *, device_id=None, target=None):
                seen_targets.append(("save", target))
                return "ok"

        owner_target = anima_memory_provider.MemoryTarget(
            device_key="dev-1",
            archive="tavern",
            character_id="raiden_mei",
            chat_key="c1",
            handoff=(("owner secret", "owner reply"),),
        )
        proxy = anima_memory_provider._DeviceBoundMemoryProxy(_StubBaseProvider(), "dev-1", target=owner_target)
        prev_ov = proxy.set_speaker_character_override("raiden_mei_guest")
        self.assertIsNone(prev_ov)
        # Execute query_memory on a SEPARATE worker thread/event loop (simulating ConnectionHandler.chat -> self.loop)
        err_holder = []

        def _other_loop_thread():
            try:
                asyncio.run(proxy.query_memory("问个问题"))
                asyncio.run(proxy.save_memory([], session_id="s-main"))
            except Exception as exc:
                err_holder.append(exc)

        t = threading.Thread(target=_other_loop_thread)
        t.start()
        t.join(timeout=5.0)
        self.assertEqual(err_holder, [])
        self.assertEqual(len(seen_targets), 2)
        for kind, tgt in seen_targets:
            self.assertEqual(tgt.character_id, "raiden_mei_guest", f"{kind} leaked owner character_id!")
            self.assertEqual(tgt.archive, "daily")
            self.assertEqual(tgt.handoff, ())
        proxy.set_speaker_character_override(prev_ov)
        self.assertEqual(proxy.effective_target(), owner_target)

        tok = anima_memory_provider.set_current_speaker_character_id("xiaozhi_guest")
        try:
            self.assertEqual(anima_memory_provider.get_current_speaker_character_id(), "xiaozhi_guest")
        finally:
            anima_memory_provider.reset_current_speaker_character_id(tok)
        self.assertIsNone(anima_memory_provider.get_current_speaker_character_id())

        msgs = [
            {"role": "system", "content": "你是雷电芽衣。"},
            {"role": "user", "content": "你好呀"},
        ]
        out = turn_context_router.apply_turn_context_to_messages(
            msgs,
            mode="split",
            speakers_block=self.vp.format_speakers_info_block("s-main"),
        )
        self.assertEqual(out[0]["content"], "你是雷电芽衣。")
        self.assertIn("<speakers_info>", out[1]["content"])
        self.assertIn("未知访客", out[1]["content"])

        # 10. Unload when disabling -> releases model & volatile PCM & latest_identification immediately
        runtime_settings.set_voiceprint(enabled=False, settings_path=self.settings_path)
        self.assertTrue(self.vp.unload_model(run_gc=True))
        self.assertFalse(self.vp.is_model_loaded())
        self.assertIsNone(self.vp.get_recent_pcm_info())
        self.assertIsNone(self.vp.get_latest_identification())

        # 11. Enrolling WAV while disabled must NOT leave ONNX extractor loaded in RAM
        wav_off = _make_wav_bytes(220.0, sr=16000, channels=1)
        self.vp.enroll_wav_bytes(wav_off, display_name="主人", role="owner")
        self.assertFalse(self.vp.is_model_loaded())

    def test_voiceprint_console_routes_and_screen_peek_orchestration(self):
        voiceprint_router.reset_default_router_for_tests(self.vp)

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

        orig_get_vp = runtime_settings.get_voiceprint
        orig_set_vp = runtime_settings.set_voiceprint
        orig_get_sp = runtime_settings.get_screen_peek
        orig_set_sp = runtime_settings.set_screen_peek
        orig_get_pro = runtime_settings.get_proactive
        runtime_settings.get_voiceprint = lambda sp=None: orig_get_vp(self.settings_path)
        runtime_settings.set_voiceprint = lambda **kw: orig_set_vp(settings_path=self.settings_path, **kw)
        runtime_settings.get_screen_peek = lambda sp=None: orig_get_sp(self.settings_path)
        runtime_settings.set_screen_peek = lambda **kw: orig_set_sp(settings_path=self.settings_path, **kw)
        runtime_settings.get_proactive = lambda sp=None: orig_get_pro(self.settings_path)
        try:
            # GET /api/voiceprint
            h_get = _FakeHandler()
            vp_routes.handle_get_voiceprint(h_get)
            self.assertEqual(h_get.status, 200)
            self.assertFalse(h_get.payload["config"]["enabled"])
            self.assertFalse(h_get.payload["screen_peek_enabled"])

            # POST /api/voiceprint/config rejects unknown fields & bool threshold
            h_bad = _FakeHandler({"unknown_key": 1})
            vp_routes.handle_post_voiceprint_config(h_bad)
            self.assertEqual(h_bad.status, 400)

            h_bad2 = _FakeHandler({"threshold": True})
            vp_routes.handle_post_voiceprint_config(h_bad2)
            self.assertEqual(h_bad2.status, 400)

            # POST /api/voiceprint/config enables voiceprint + screen_peek
            h_en = _FakeHandler({"enabled": True, "screen_peek_enabled": True, "threshold": 0.62})
            vp_routes.handle_post_voiceprint_config(h_en)
            self.assertEqual(h_en.status, 200)
            self.assertTrue(h_en.payload["config"]["enabled"])
            self.assertTrue(h_en.payload["screen_peek_enabled"])
            self.assertAlmostEqual(h_en.payload["config"]["threshold"], 0.62)

            # POST /api/voiceprint/enroll-wav + delete
            wav_b64 = base64.b64encode(_make_wav_bytes(220.0, sr=16000, channels=1)).decode("ascii")
            h_wav = _FakeHandler({"display_name": "主人", "role": "owner", "wav_base64": wav_b64})
            vp_routes.handle_post_voiceprint_enroll_wav(h_wav)
            self.assertEqual(h_wav.status, 200)
            sid = h_wav.payload["enrolled"]["speaker_id"]
            self.assertTrue(h_wav.payload["guard_active"])

            # Disabling voiceprint via config route automatically unloads the model!
            h_off = _FakeHandler({"enabled": False})
            vp_routes.handle_post_voiceprint_config(h_off)
            self.assertEqual(h_off.status, 200)
            self.assertFalse(self.vp.is_model_loaded())

            h_del = _FakeHandler({"speaker_id": sid})
            vp_routes.handle_post_voiceprint_delete(h_del)
            self.assertEqual(h_del.status, 200)
            self.assertEqual(h_del.payload["speakers"], [])

            # Screen peek orchestration (enabled=True) + privacy block pass-through + widget snapshot
            peek_ok = proactive_companion.run_on_demand_screen_peek(
                question="这个报错怎么回事",
                speak=False,
                settings_path=self.settings_path,
                peek_fetcher=lambda _q: {
                    "ok": True,
                    "blocked": False,
                    "summary": "你在 VS Code 查看 pytest 报错，提示缺少 fixture。",
                    "safe_title": "代码与开发窗口",
                    "activity_category": "coding",
                },
                llm_caller=lambda _s, _u, _c: '{"emotion":"gentle","text":"看起来是少了 pytest 的 fixture 定义，先补上再跑一次吧。"}',
                config_reader=lambda: {"prompt": "你是雷电芽衣"},
            )
            self.assertTrue(peek_ok["ok"])
            self.assertEqual(peek_ok["emotion"], "gentle")
            self.assertIn("fixture", peek_ok["text"])

            # POST /api/voiceprint/download-model drains body & rejects unexpected keys with 400
            h_dl_bad = _FakeHandler({"evil": True})
            vp_routes.handle_post_voiceprint_download_model(h_dl_bad)
            self.assertEqual(h_dl_bad.status, 400)

            # GET /api/companion-widget reflects latest screen_peek bubble
            h_w = _FakeHandler()
            vp_routes.handle_get_companion_widget(h_w)
            self.assertEqual(h_w.status, 200)
            self.assertTrue(h_w.payload["ok"])
            self.assertEqual(h_w.payload["screen_peek_bubble"]["text"], peek_ok["text"])
        finally:
            runtime_settings.get_voiceprint = orig_get_vp
            runtime_settings.set_voiceprint = orig_set_vp
            runtime_settings.get_screen_peek = orig_get_sp
            runtime_settings.set_screen_peek = orig_set_sp
            runtime_settings.get_proactive = orig_get_pro


if __name__ == "__main__":
    unittest.main()

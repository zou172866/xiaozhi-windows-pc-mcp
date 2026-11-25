"""Comprehensive unit & integration tests for Phase 1 companion upgrades:
1. ① Sherpa SenseVoice vocal cue extraction, TTL cache, speculative VAD passthrough,
   dynamic <turn_context> injection (prefix-cache safe) & tool continuation pinning.
2. ② Runtime settings `proactive` validation, two-stage proactive companion heartbeat
   (quiet hours cross-midnight, all 5 hard gates zero-LLM, flow exemptions, dry_run default
   vs pc_speaker delivery, ignored-nudge exponential backoff + user-speech auto-reset,
   GSV busy yield, LLM JSON parsing/trimming/redaction, and 18080 `/api/proactive*` routes).
"""

from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
import json
import os
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

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

import proactive_companion
import runtime_events
import runtime_settings
import turn_context_router
import vad_speculative_router
import vocal_cue_router
from console.routes import proactive as proactive_routes


class TestPhase1VocalCueAwareness(unittest.TestCase):
    def setUp(self):
        self._orig_vc_enabled = vocal_cue_router.is_vocal_cue_enabled
        vocal_cue_router.is_vocal_cue_enabled = lambda settings_path=None: True
        vocal_cue_router.reset_vocal_cues()
        runtime_events.reset_runtime_events()

    def tearDown(self):
        vocal_cue_router.is_vocal_cue_enabled = self._orig_vc_enabled
        vocal_cue_router.reset_vocal_cues()
        runtime_events.reset_runtime_events()

    def test_extract_filters_neutral_and_parses_attrs_and_tags(self):
        # Neutral + Speech + BGM + OTHER + Breath + Speech_Noise + unknown must return None
        res_neutral = SimpleNamespace(text="<|zh|><|NEUTRAL|><|Speech|><|BGM|>你好啊", emotion="NEUTRAL", event="Speech", lang="zh")
        self.assertIsNone(vocal_cue_router.extract_vocal_cue(res_neutral.text, result=res_neutral))

        res_unk = SimpleNamespace(
            text="<|OTHER|><|EMO_UNKNOWN|><|Event_UNK|><|Breath|><|Speech_Noise|><|GBG|>测试",
            emotion="OTHER",
            event="Breath",
            lang="",
        )
        self.assertIsNone(vocal_cue_router.extract_vocal_cue(res_unk.text, result=res_unk))

        # Real SenseVoice tokens.txt emits `<|Cry|>` (not `<|Crying|>`)!
        res_real_cry = SimpleNamespace(text="我没事", emotion="<|NEUTRAL|>", event="<|Cry|>", lang="<|zh|>")
        cue_cry = vocal_cue_router.extract_vocal_cue(res_real_cry.text, result=res_real_cry)
        self.assertIsNotNone(cue_cry)
        self.assertEqual(cue_cry["event"], "crying")
        self.assertIn("哭腔", cue_cry["prompt_block"])

        # Inline `<|Cry|>` (e.g. SiliconFlow cloud SenseVoiceSmall) also works
        cue_cry_inline = vocal_cue_router.extract_vocal_cue("<|zh|><|SAD|><|Cry|>好委屈")
        self.assertIsNotNone(cue_cry_inline)
        self.assertEqual(cue_cry_inline["emotion"], "sad")
        self.assertEqual(cue_cry_inline["event"], "crying")

        # Result attributes with <|...|> wrappers
        res_sad_sigh = SimpleNamespace(text="今天加班好累", emotion="<|SAD|>", event="<|Sigh|>", lang="<|zh|>")
        cue = vocal_cue_router.extract_vocal_cue(res_sad_sigh.text, result=res_sad_sigh)
        self.assertIsNotNone(cue)
        self.assertEqual(cue["emotion"], "sad")
        self.assertEqual(cue["event"], "sigh")
        self.assertEqual(cue["lang"], "zh")
        self.assertIn("<user_vocal_cue>", cue["prompt_block"])
        self.assertIn("低落", cue["prompt_block"])
        self.assertIn("叹气", cue["prompt_block"])

        # Inline tags only (no result attrs)
        cue_inline = vocal_cue_router.extract_vocal_cue("<|zh|><|HAPPY|><|Laughter|>太开心了")
        self.assertIsNotNone(cue_inline)
        self.assertEqual(cue_inline["emotion"], "happy")
        self.assertEqual(cue_inline["event"], "laughter")
        self.assertIn("笑声", cue_inline["summary_zh"])

    def test_session_cache_oneshot_ttl_bounds_and_neutral_clear(self):
        cue = vocal_cue_router.extract_vocal_cue("<|SAD|><|Crying|>难受")
        vocal_cue_router.record_vocal_cue("sess-1", cue, ttl_seconds=20.0)
        # Runtime event emitted
        evts = [e for e in runtime_events.get_recent_events(10) if e["type"] == "user_vocal_cue"]
        self.assertEqual(len(evts), 1)
        self.assertEqual(evts[0]["emotion"], "sad")

        # One-shot consume returns block once, then empty
        blk1 = vocal_cue_router.consume_vocal_cue("sess-1")
        self.assertIn("<user_vocal_cue>", blk1)
        self.assertEqual(vocal_cue_router.consume_vocal_cue("sess-1"), "")

        # Neutral utterance clears any stale previous cue for the same session
        vocal_cue_router.record_vocal_cue("sess-2", cue, ttl_seconds=20.0)
        vocal_cue_router.record_vocal_cue("sess-2", None)
        self.assertEqual(vocal_cue_router.consume_vocal_cue("sess-2"), "")

        # Bounded max 64 sessions
        for i in range(80):
            vocal_cue_router.record_vocal_cue(f"sid-{i}", cue, publish=False)
        self.assertIsNone(vocal_cue_router.peek_vocal_cue("sid-0"))
        self.assertIsNotNone(vocal_cue_router.peek_vocal_cue("sid-79"))

        # TTL expiration
        vocal_cue_router.record_vocal_cue("sess-exp", cue, ttl_seconds=1.0, publish=False)
        with vocal_cue_router._lock:
            vocal_cue_router._session_cues["sess-exp"]["expires_at"] = time.monotonic() - 0.1
        self.assertEqual(vocal_cue_router.consume_vocal_cue("sess-exp"), "")

    def test_speculative_vad_decode_and_stash_preserves_vocal_cue_and_clean_text(self):
        class _FakeStream:
            def __init__(self):
                self.result = SimpleNamespace(
                    text="<|zh|><|SURPRISED|><|Laughter|>真的吗太好了",
                    emotion="<|SURPRISED|>",
                    event="<|Laughter|>",
                    lang="zh",
                )

            def accept_waveform(self, _sr, _samples):
                pass

        class _FakeModel:
            def create_stream(self):
                return _FakeStream()

            def decode_stream(self, _s):
                pass

        fake_asr = SimpleNamespace(model=_FakeModel())
        # Exercise the real `asr.model.create_stream()` -> `decode_stream()` -> `stream.result` path!
        cleaned, cue = vad_speculative_router._decode_pcm_with_sherpa_with_cue_sync(fake_asr, b"\x00\x00" * 320)
        self.assertEqual(cleaned, "真的吗太好了")
        self.assertNotIn("<|", cleaned)
        self.assertIsNotNone(cue)
        self.assertEqual(cue["emotion"], "surprised")
        self.assertEqual(cue["event"], "laughter")
        # Speculative decode alone must NOT pollute live session vocal cues until confirmed commit
        self.assertEqual(vocal_cue_router.consume_vocal_cue("s-vad"), "")

        # Stash & pop preserves cue across speculative cache hit
        vad_speculative_router._stash_asr_speculative_hit(fake_asr, "s-vad", 6400, cleaned, vocal_cue=cue)
        popped_txt, popped_cue = vad_speculative_router._pop_asr_speculative_hit_with_cue(fake_asr, "s-vad", 6400)
        self.assertEqual(popped_txt, "真的吗太好了")
        self.assertEqual(popped_cue["emotion"], "surprised")

    def test_turn_context_injects_vocal_cue_without_breaking_prefix_cache(self):
        base_sys = "你是雷电芽衣，性格温柔沉稳。\n\n<context>\n当前时间：{{current_time}}\n</context>"
        cue = vocal_cue_router.extract_vocal_cue("<|SAD|><|Sigh|>今天好累")

        msgs_t1 = [
            {"role": "system", "content": base_sys},
            {"role": "user", "content": "今天好累啊"},
        ]
        meta_t1 = {}
        out_t1 = turn_context_router.apply_turn_context_to_messages(
            msgs_t1,
            memory_str="用户喜欢热茶",
            current_time_str="21:00",
            mode="split",
            vocal_cue_block=cue["prompt_block"],
            out_meta=meta_t1,
        )
        # System prompt stays clean & identical (prefix cache safe!)
        self.assertEqual(out_t1[0]["content"], "你是雷电芽衣，性格温柔沉稳。")
        # Vocal cue is inside <turn_context> on the latest user turn
        self.assertIn("<user_vocal_cue>", out_t1[1]["content"])
        self.assertIn("叹气", out_t1[1]["content"])
        # Original caller list is NEVER mutated
        self.assertEqual(msgs_t1[1]["content"], "今天好累啊")

        # Tool continuation (depth > 0) with pinned_turn_context keeps Turn 1 byte-identical
        msgs_tool = [
            {"role": "system", "content": base_sys},
            {"role": "user", "content": "今天好累啊"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "ok"},
        ]
        out_tool = turn_context_router.apply_turn_context_to_messages(
            msgs_tool,
            current_time_str="21:01",
            mode="split",
            pinned_turn_context=meta_t1["turn_context"],
        )
        self.assertEqual(out_tool[0]["content"], out_t1[0]["content"])
        self.assertEqual(out_tool[1]["content"], out_t1[1]["content"])

        # Next neutral turn strips old <turn_context> and keeps system + history prefix identical
        msgs_t2 = [
            {"role": "system", "content": base_sys},
            {"role": "user", "content": "今天好累啊"},
            {"role": "assistant", "content": "辛苦啦，先歇一歇。"},
            {"role": "user", "content": "嗯，好多了"},
        ]
        out_t2 = turn_context_router.apply_turn_context_to_messages(
            msgs_t2,
            memory_str="用户喜欢热茶",
            current_time_str="21:05",
            mode="split",
            vocal_cue_block="",
        )
        self.assertEqual(out_t2[0]["content"], out_t1[0]["content"])
        self.assertNotIn("<user_vocal_cue>", out_t2[1]["content"])
        self.assertNotIn("<user_vocal_cue>", out_t2[3]["content"])


class TestPhase1ProactiveCompanion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.settings_path = os.path.join(self.tmp_dir, "runtime_settings.json")
        self.history_path = os.path.join(self.tmp_dir, "proactive_history.json")
        runtime_events.reset_runtime_events()

    def tearDown(self):
        runtime_events.reset_runtime_events()
        self._tmp.cleanup()

    def test_zero_import_side_effects_for_new_modules(self):
        probe = (
            "import json, os, sys, threading; "
            f"sys.path.insert(0, {APP_DIR!r}); "
            "th0 = {t.name for t in threading.enumerate()}; "
            "import vocal_cue_router, proactive_companion; "
            "from console.routes import proactive; "
            "th1 = {t.name for t in threading.enumerate()}; "
            "print(json.dumps({'new_threads': sorted(th1 - th0)}))"
        )
        proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(data["new_threads"], [])

    def test_runtime_settings_proactive_defaults_and_validation(self):
        cfg = runtime_settings.get_proactive(self.settings_path)
        self.assertFalse(cfg["enabled"])
        self.assertFalse(cfg["inner_life_enabled"])
        self.assertEqual(cfg["delivery_mode"], "dry_run")
        self.assertFalse(runtime_settings.get_vocal_cue(self.settings_path)["enabled"])
        self.assertFalse(runtime_settings.get_voiceprint(self.settings_path)["enabled"])
        self.assertEqual(runtime_settings.get_voiceprint(self.settings_path)["policy"], "full_guard")
        self.assertFalse(runtime_settings.get_screen_peek(self.settings_path)["enabled"])
        self.assertEqual(cfg["quiet_start"], "23:00")
        self.assertEqual(cfg["quiet_end"], "08:00")
        self.assertEqual(cfg["check_interval_minutes"], 15)
        self.assertEqual(cfg["min_since_last_chat_minutes"], 45)
        self.assertEqual(cfg["min_cooldown_minutes"], 90)
        self.assertEqual(cfg["max_daily_nudges"], 4)
        self.assertAlmostEqual(cfg["pc_speaker_volume"], 0.75)

        saved = runtime_settings.set_proactive(
            delivery_mode="pc_speaker",
            quiet_start="22:30",
            quiet_end="07:30",
            pc_speaker_volume=0.65,
            settings_path=self.settings_path,
        )
        self.assertEqual(saved["delivery_mode"], "pc_speaker")
        self.assertEqual(saved["quiet_start"], "22:30")
        self.assertAlmostEqual(saved["pc_speaker_volume"], 0.65)

        with self.assertRaises(ValueError):
            runtime_settings.set_proactive(delivery_mode="esp32_blast", settings_path=self.settings_path)
        with self.assertRaises(ValueError):
            runtime_settings.set_proactive(quiet_start="25:99", settings_path=self.settings_path)
        with self.assertRaises(ValueError):
            runtime_settings.set_proactive(enabled="yes", settings_path=self.settings_path)

    def test_quiet_hours_and_exponential_backoff_math(self):
        # Cross-midnight 23:00 - 08:00
        self.assertTrue(proactive_companion.is_in_quiet_hours(datetime(2026, 6, 15, 23, 0), "23:00", "08:00"))
        self.assertTrue(proactive_companion.is_in_quiet_hours(datetime(2026, 6, 16, 3, 30), "23:00", "08:00"))
        self.assertFalse(proactive_companion.is_in_quiet_hours(datetime(2026, 6, 16, 8, 0), "23:00", "08:00"))
        self.assertFalse(proactive_companion.is_in_quiet_hours(datetime(2026, 6, 15, 22, 59), "23:00", "08:00"))

        # Same-day 13:00 - 15:00
        self.assertTrue(proactive_companion.is_in_quiet_hours(datetime(2026, 6, 15, 14, 0), "13:00", "15:00"))
        self.assertFalse(proactive_companion.is_in_quiet_hours(datetime(2026, 6, 15, 15, 0), "13:00", "15:00"))

        # Equal start == end => disabled
        self.assertFalse(proactive_companion.is_in_quiet_hours(datetime(2026, 6, 15, 23, 0), "00:00", "00:00"))

        # Exponential backoff capped at 12h (720 min)
        self.assertEqual(proactive_companion.compute_effective_cooldown_minutes(90, 0), 90)
        self.assertEqual(proactive_companion.compute_effective_cooldown_minutes(90, 1), 180)
        self.assertEqual(proactive_companion.compute_effective_cooldown_minutes(90, 2), 360)
        self.assertEqual(proactive_companion.compute_effective_cooldown_minutes(90, 3), 720)
        self.assertEqual(proactive_companion.compute_effective_cooldown_minutes(90, 9), 720)

    def test_stage1_hard_gates_block_without_llm_calls(self):
        runtime_settings.set_proactive(enabled=True, settings_path=self.settings_path)
        llm_calls = []
        clock = {"ts": datetime(2026, 6, 15, 15, 0).timestamp()}
        rhythm_box = {
            "val": {
                "online": True,
                "idle_seconds": 180.0,
                "is_fullscreen": False,
                "activity_category": "coding",
                "safe_title": "代码与开发窗口",
                "active_streak_seconds": 1800.0,
                "just_paused": True,
                "returned_from_away": False,
                "pc_audio_busy": False,
                "l3_status": {"running_count": 0, "queued_count": 0, "recently_finished": None},
            }
        }
        busy_box = {"val": False}

        eng = proactive_companion.ProactiveCompanionEngine(
            history_path=self.history_path,
            settings_path=self.settings_path,
            now_fn=lambda: clock["ts"],
            local_dt_fn=lambda ts: datetime.fromtimestamp(ts),
            rhythm_fetcher=lambda: dict(rhythm_box["val"]),
            anima_context_fetcher=lambda _c, _w: {"character_name": "雷电芽衣", "due_open_loops": [], "inner_life": {}},
            llm_caller=lambda _s, _u, _c: (llm_calls.append(1) or '{"action":"HEARTBEAT_OK"}'),
            busy_checker=lambda: busy_box["val"],
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            weather_reader=lambda _c: "",
        )

        # 1. Fullscreen -> blocked
        rhythm_box["val"]["is_fullscreen"] = True
        r1 = eng.run_once(manual=False)
        self.assertEqual(r1["reason"], "desktop_fullscreen")
        rhythm_box["val"]["is_fullscreen"] = False

        # 2. Game / meeting -> blocked
        for cat in ("game", "meeting"):
            rhythm_box["val"]["activity_category"] = cat
            rc = eng.run_once(manual=False)
            self.assertEqual(rc["reason"], f"desktop_focus_{cat}")
        rhythm_box["val"]["activity_category"] = "coding"

        # 3. Away from PC (> 1200s) -> blocked
        rhythm_box["val"]["idle_seconds"] = 1500.0
        r_away = eng.run_once(manual=False)
        self.assertEqual(r_away["reason"], "user_away_from_pc")

        # 4. Active typing flow (idle < 45s and not just_paused / not returned / no L3 finish) -> blocked
        rhythm_box["val"]["idle_seconds"] = 10.0
        rhythm_box["val"]["just_paused"] = False
        r_type = eng.run_once(manual=False)
        self.assertEqual(r_type["reason"], "desktop_typing_flow")

        # 5. Quiet hours -> blocked
        rhythm_box["val"]["idle_seconds"] = 180.0
        rhythm_box["val"]["just_paused"] = True
        clock["ts"] = datetime(2026, 6, 15, 23, 30).timestamp()
        r_quiet = eng.run_once(manual=False)
        self.assertEqual(r_quiet["reason"], "quiet_hours")
        clock["ts"] = datetime(2026, 6, 15, 15, 0).timestamp()

        # 6. Voice pipeline busy -> blocked
        busy_box["val"] = True
        r_busy = eng.run_once(manual=False)
        self.assertEqual(r_busy["reason"], "voice_busy")
        busy_box["val"] = False

        # ZERO LLM calls must have occurred across all 6 Stage-1 rejections!
        self.assertEqual(llm_calls, [])

    def test_dry_run_default_vs_pc_speaker_backoff_and_user_reset(self):
        runtime_settings.set_proactive(enabled=True, settings_path=self.settings_path)
        clock = {"ts": datetime(2026, 6, 15, 15, 0).timestamp()}
        acks = []
        spoken_wavs = []

        def _fake_llm(_sys, _usr, _cfg):
            return json.dumps(
                {
                    "action": "SPEAK",
                    "reason": "专注刚停顿，关心一下面试",
                    "emotion": "gentle",
                    "text": "忙完一阵啦？昨天那场面试感觉还顺利吗，先喝口水歇歇吧。",
                    "open_loop_id": "loop-interview-1",
                },
                ensure_ascii=False,
            )

        eng = proactive_companion.ProactiveCompanionEngine(
            history_path=self.history_path,
            settings_path=self.settings_path,
            now_fn=lambda: clock["ts"],
            local_dt_fn=lambda ts: datetime.fromtimestamp(ts),
            rhythm_fetcher=lambda: {
                "online": True,
                "idle_seconds": 180.0,
                "is_fullscreen": False,
                "activity_category": "coding",
                "safe_title": "代码与开发窗口",
                "active_streak_seconds": 1800.0,
                "just_paused": True,
                "returned_from_away": False,
                "pc_audio_busy": False,
                "l3_status": {"running_count": 0, "queued_count": 0, "recently_finished": None},
            },
            anima_context_fetcher=lambda _c, _w: {
                "character_name": "雷电芽衣",
                "hours_since_last_chat": 3.0,
                "mood": {"stage": "close", "last_emotion": "warm"},
                "due_open_loops": [{"memory_id": "loop-interview-1", "text": "昨天参加面试"}],
                "inner_life": {"local_date": "2026-06-15", "slot": "afternoon", "text": "下午泡了壶红茶。"},
            },
            anima_ack_sender=lambda cid, lid, act, eid: (acks.append((cid, lid, act, eid)) or True),
            llm_caller=_fake_llm,
            tts_synthesizer=lambda txt, emo, _cfg: (b"RIFF....WAVEfake", f"mock:{emo}"),
            speaker_player=lambda wav, vol: (spoken_wavs.append((wav, vol)) or (True, "ok")),
            busy_checker=lambda: False,
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            weather_reader=lambda _c: "长沙：晴，25℃",
        )

        # 1. Default mode is dry_run: logs history + emits event, NO speaker playback, NO Anima ack, NO daily count!
        res_dry = eng.run_once(manual=False)
        self.assertTrue(res_dry["ok"])
        self.assertEqual(res_dry["outcome"], "dry_run_logged")
        self.assertEqual(len(spoken_wavs), 0)
        self.assertEqual(len(acks), 0)
        snap = eng.get_snapshot()
        self.assertEqual(snap["runtime"]["daily_spoke_count"], 0)
        self.assertEqual(snap["runtime"]["consecutive_ignored"], 0)
        self.assertEqual(len(snap["history"]), 1)
        self.assertTrue(os.path.isfile(self.history_path))

        # 2. Switch to pc_speaker mode -> plays softly, acks open_loop, bumps daily_spoke_count & consecutive_ignored
        runtime_settings.set_proactive(delivery_mode="pc_speaker", pc_speaker_volume=0.65, settings_path=self.settings_path)
        res_spk = eng.run_once(manual=False)
        self.assertTrue(res_spk["ok"])
        self.assertEqual(res_spk["outcome"], "spoken")
        self.assertEqual(len(spoken_wavs), 1)
        self.assertAlmostEqual(spoken_wavs[0][1], 0.65)
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0][1], "loop-interview-1")
        self.assertEqual(acks[0][2], "nudged")

        # 3. Ignored-nudge backoff: 1 ignored -> effective cooldown doubles from 90m to 180m
        clock["ts"] += 100 * 60  # 100 min later (< 180 min!) -> still blocked!
        res_cd = eng.run_once(manual=False)
        self.assertEqual(res_cd["reason"], "cooldown_backoff")

        # 4. User speaks -> resets consecutive_ignored to 0 immediately!
        eng.on_runtime_event({"type": "user_message", "text": "刚忙完，谢谢关心"})
        self.assertEqual(eng.get_snapshot()["runtime"]["consecutive_ignored"], 0)

        # 5. Right after user speaks, min_since_last_chat_minutes (45m) blocks premature nudge
        clock["ts"] += 10 * 60
        res_rec = eng.run_once(manual=False)
        self.assertEqual(res_rec["reason"], "recent_chat_active")

        # 6. Manual dry_run probe works even during cooldown/recent chat WITHOUT speaking or acking
        res_manual = eng.trigger_manual_check(force_dry_run=True, bypass_timing_gates=True)
        self.assertTrue(res_manual["ok"])
        self.assertEqual(res_manual["outcome"], "dry_run_logged")
        self.assertEqual(res_manual["would_block_reason"], "recent_chat_active")
        self.assertEqual(len(spoken_wavs), 1)  # unchanged!
        self.assertEqual(len(acks), 1)         # unchanged!

    def test_llm_parser_trims_redacts_and_guards_open_loop_id(self):
        raw = '```json\n{"action":"SPEAK","emotion":"happy","text":"你的手机号 13812345678 记好了吗？' + ("很长很长" * 20) + '","open_loop_id":"fake-id"}\n```'
        dec = proactive_companion.parse_proactive_llm_decision(raw, allowed_open_loop_ids={"real-id-1"})
        self.assertEqual(dec["action"], "SPEAK")
        self.assertEqual(dec["emotion"], "happy")
        self.assertEqual(dec["open_loop_id"], "")  # fabricated id rejected!
        self.assertNotIn("13812345678", dec["text"])
        self.assertLessEqual(len(dec["text"]), proactive_companion.MAX_PROACTIVE_TEXT_CHARS)

        # Punctuation-free long text must STILL be strictly <= MAX_PROACTIVE_TEXT_CHARS (no +1 off-by-one!)
        no_punct = json.dumps({"action": "SPEAK", "emotion": "gentle", "text": "忙" * 80}, ensure_ascii=False)
        dec_np = proactive_companion.parse_proactive_llm_decision(no_punct)
        self.assertEqual(dec_np["action"], "SPEAK")
        self.assertLessEqual(len(dec_np["text"]), proactive_companion.MAX_PROACTIVE_TEXT_CHARS)

        # Trailing commentary containing braces must not break JSON parsing
        trailing = '{"action":"SPEAK","emotion":"calm","text":"喝口水歇歇吧。"} (注：满足{条件})'
        dec_tr = proactive_companion.parse_proactive_llm_decision(trailing)
        self.assertEqual(dec_tr["action"], "SPEAK")
        self.assertEqual(dec_tr["text"], "喝口水歇歇吧。")

        # Unclosed <think> truncation degrades safely to HEARTBEAT_OK
        trunc_think = "<think>正在思考用户是否在写代码..."
        self.assertEqual(proactive_companion.parse_proactive_llm_decision(trunc_think)["action"], "HEARTBEAT_OK")

        hb = proactive_companion.parse_proactive_llm_decision("HEARTBEAT_OK")
        self.assertEqual(hb["action"], "HEARTBEAT_OK")

    def test_pc_speaker_heartbeat_ok_cooldown_pre_rhythm_skip_and_tts_helpers(self):
        import asyncio

        rhythm_calls = []
        llm_calls = []
        clock = {"ts": datetime(2026, 6, 15, 15, 0).timestamp()}
        rhythm_box = {
            "val": {
                "online": True,
                "idle_seconds": 180.0,
                "is_fullscreen": False,
                "activity_category": "coding",
                "safe_title": "代码与开发窗口",
                "active_streak_seconds": 1800.0,
                "just_paused": True,
                "returned_from_away": False,
                "pc_audio_busy": False,
                "l3_status": {"running_count": 0, "queued_count": 0, "recently_finished": None},
            }
        }

        def _fetch_rhy():
            rhythm_calls.append(1)
            return dict(rhythm_box["val"])

        runtime_settings.set_proactive(enabled=True, delivery_mode="pc_speaker", settings_path=self.settings_path)
        eng = proactive_companion.ProactiveCompanionEngine(
            history_path=self.history_path,
            settings_path=self.settings_path,
            now_fn=lambda: clock["ts"],
            local_dt_fn=lambda ts: datetime.fromtimestamp(ts),
            rhythm_fetcher=_fetch_rhy,
            anima_context_fetcher=lambda _c, _w: {"character_name": "雷电芽衣", "due_open_loops": [], "inner_life": {}},
            llm_caller=lambda _s, _u, _c: (llm_calls.append(1) or '{"action":"HEARTBEAT_OK","reason":"保持安静"}'),
            busy_checker=lambda: False,
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            weather_reader=lambda _c: "",
        )

        # 1. Quiet hours background tick must NOT even probe host 18900 /desktop/rhythm
        clock["ts"] = datetime(2026, 6, 15, 23, 30).timestamp()
        r_q = eng.run_once(manual=False)
        self.assertEqual(r_q["reason"], "quiet_hours")
        self.assertEqual(len(rhythm_calls), 0)

        # 2. Daytime pc_speaker HEARTBEAT_OK enters quiet-eval cooldown (no 15-min LLM token burn!)
        clock["ts"] = datetime(2026, 6, 15, 15, 0).timestamp()
        r_hb1 = eng.run_once(manual=False)
        self.assertEqual(r_hb1["outcome"], "heartbeat_ok")
        self.assertEqual(len(llm_calls), 1)

        clock["ts"] += 15 * 60
        r_hb2 = eng.run_once(manual=False)
        self.assertEqual(r_hb2["outcome"], "skipped")
        self.assertEqual(r_hb2["reason"], "heartbeat_ok_cooldown")
        self.assertEqual(len(llm_calls), 1)

        # 3. Fresh L3 completion bypasses heartbeat_ok_cooldown immediately
        rhythm_box["val"]["l3_status"] = {
            "running_count": 0,
            "queued_count": 0,
            "recently_finished": {"id": "dsh_99", "status": "completed", "backend": "dsh", "age_seconds": 12.0},
        }
        r_l3 = eng.run_once(manual=False)
        self.assertEqual(r_l3["outcome"], "heartbeat_ok")
        self.assertEqual(len(llm_calls), 2)

        # 4. _run_coro_sync safely runs coroutines even when an event loop is ALREADY running!
        async def _sample_coro():
            return "ok_nested"

        async def _caller_in_loop():
            return proactive_companion._run_coro_sync(_sample_coro())

        self.assertEqual(asyncio.run(_caller_in_loop()), "ok_nested")

    def test_default_llm_and_synthesize_wav_edge_cases(self):
        from console.routes import audio_api, gsv

        # 1. _default_llm_call allows local endpoint without api_key & uses max_tokens >= 300
        captured_req = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": '{"action":"HEARTBEAT_OK"}'}}]}

        class _FakeClient:
            def __init__(self, **kw):
                captured_req["client_kw"] = kw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                captured_req["url"] = url
                captured_req["headers"] = headers
                captured_req["json"] = json
                return _FakeResp()

        local_cfg = {
            "selected_module": {"LLM": "HuihuiLocal"},
            "LLM": {
                "HuihuiLocal": {
                    "base_url": "http://host.docker.internal:19340/v1",
                    "model_name": "huihui-qwen3-14b-companion",
                    "api_key": "",
                }
            },
        }
        with patch.object(proactive_companion.httpx, "Client", _FakeClient):
            out = proactive_companion._default_llm_call("sys", "usr", local_cfg)
        self.assertIn("HEARTBEAT_OK", out)
        self.assertNotIn("Authorization", captured_req["headers"])
        self.assertGreaterEqual(captured_req["json"]["max_tokens"], 300)
        self.assertFalse(captured_req["client_kw"].get("follow_redirects", True))

        # 2. _default_synthesize_wav forwards raw_cfg to Cloud TTS and maps audio/ogg -> ogg
        cloud_calls = []

        def _fake_cloud_tts(prov, txt, **kw):
            cloud_calls.append((prov, txt, kw))
            return b"OggSfake", "audio/ogg"

        pcm_calls = []

        def _fake_ensure_wav(raw, fmt):
            pcm_calls.append((raw, fmt))
            return b"RIFF....WAVEok", 500

        cloud_cfg = {"selected_module": {"TTS": "CosyVoiceSiliconflow"}, "TTS": {"CosyVoiceSiliconflow": {"voice": "anna"}}}
        with patch.object(audio_api, "synthesize_cloud_tts_preview", side_effect=_fake_cloud_tts), patch.object(
            proactive_companion.pc_audio_router, "ensure_pcm_wav", side_effect=_fake_ensure_wav
        ):
            wav_b, src = proactive_companion._default_synthesize_wav("休息一下吧", "gentle", cloud_cfg)
        self.assertEqual(wav_b, b"RIFF....WAVEok")
        self.assertEqual(src, "cloud:CosyVoiceSiliconflow")
        self.assertEqual(cloud_calls[0][2].get("raw_cfg"), cloud_cfg)
        self.assertEqual(pcm_calls[0][1], "ogg")

        # 3. _default_synthesize_wav resolves EdgeTTS voice_id (not only voice)
        edge_calls = []

        async def _fake_edge(text, voice, timeout=10.0):
            edge_calls.append((text, voice))
            return b"ID3fakemp3"

        edge_cfg = {"selected_module": {"TTS": "EdgeTTS"}, "TTS": {"EdgeTTS": {"voice_id": "zh-TW-HsiaoChenNeural"}}}
        with patch.object(gsv, "_synthesize_mp3_with_timeout", side_effect=_fake_edge), patch.object(
            proactive_companion.pc_audio_router, "ensure_pcm_wav", side_effect=_fake_ensure_wav
        ):
            wav_e, src_e = proactive_companion._default_synthesize_wav("喝口水吧", "gentle", edge_cfg)
        self.assertEqual(wav_e, b"RIFF....WAVEok")
        self.assertEqual(src_e, "edge:zh-TW-HsiaoChenNeural")
        self.assertEqual(edge_calls[0][1], "zh-TW-HsiaoChenNeural")

    def test_console_proactive_routes_validation_and_actions(self):
        fake_eng = proactive_companion.ProactiveCompanionEngine(
            history_path=self.history_path,
            settings_path=self.settings_path,
            rhythm_fetcher=lambda: {"online": False},
            anima_context_fetcher=lambda _c, _w: {"character_name": "雷电芽衣", "due_open_loops": [], "inner_life": {"text": "泡茶中", "slot": "afternoon", "local_date": "2026-06-15"}},
            anima_inner_life_refresher=lambda _c, _w, _u: {"inner_life": {"text": "翻看料理笔记", "slot": "afternoon", "local_date": "2026-06-15"}},
            llm_caller=lambda _s, _u, _c: '{"action":"SPEAK","emotion":"gentle","text":"休息一下喝口水吧。"}',
            busy_checker=lambda: False,
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            weather_reader=lambda _c: "",
        )

        def _make_handler(body_dict):
            h = MagicMock()
            h._read_body_json.return_value = body_dict
            return h

        with patch.object(proactive_companion, "get_proactive_engine", return_value=fake_eng), patch.object(
            proactive_companion, "get_proactive_snapshot", side_effect=fake_eng.get_snapshot
        ), patch.object(runtime_settings, "RUNTIME_SETTINGS_PATH", self.settings_path):
            # 1. GET /api/proactive
            h_get = _make_handler({})
            proactive_routes.handle_get_proactive(h_get)
            payload_get = h_get._send_json.call_args[0][0]
            self.assertTrue(payload_get["ok"])
            self.assertEqual(payload_get["config"]["delivery_mode"], "dry_run")

            # 2. POST /api/proactive rejects unknown keys and float ints with 400
            h_bad = _make_handler({"delivery_mode": "pc_speaker", "evil_key": 1})
            proactive_routes.handle_post_proactive(h_bad)
            self.assertEqual(h_bad._send_json.call_args[0][1], HTTPStatus.BAD_REQUEST)

            h_float = _make_handler({"check_interval_minutes": 15.7})
            proactive_routes.handle_post_proactive(h_float)
            self.assertEqual(h_float._send_json.call_args[0][1], HTTPStatus.BAD_REQUEST)

            # 3. POST /api/proactive valid update
            h_ok = _make_handler({"delivery_mode": "pc_speaker", "quiet_start": "22:30"})
            proactive_routes.handle_post_proactive(h_ok)
            self.assertEqual(h_ok._send_json.call_args[0][0]["config"]["delivery_mode"], "pc_speaker")

            # 4. POST /api/proactive/run defaults to safe dry_run
            h_run = _make_handler({})
            proactive_routes.handle_post_proactive_run(h_run)
            run_body = h_run._send_json.call_args[0][0]
            self.assertTrue(run_body["ok"])
            self.assertEqual(run_body["outcome"], "dry_run_logged")

            # 5. POST /api/proactive/inner-life
            h_il = _make_handler({"use_llm": False})
            proactive_routes.handle_post_proactive_inner_life(h_il)
            il_body = h_il._send_json.call_args[0][0]
            self.assertTrue(il_body["ok"])
            self.assertEqual(il_body["inner_life"]["text"], "翻看料理笔记")

    def test_cross_client_and_post_restart_backoff_reset_via_anima_last_turn(self):
        t0 = datetime(2026, 6, 15, 13, 0).timestamp()
        clock = {"ts": t0}
        anima_calls = []
        llm_calls = []
        spoken = []
        anima_state = {"last_turn_at": t0 - 7200.0, "hours_since_last_chat": 2.0}

        rhythm_ok = {
            "online": True,
            "idle_seconds": 180.0,
            "is_fullscreen": False,
            "activity_category": "coding",
            "safe_title": "代码与开发窗口",
            "active_streak_seconds": 1800.0,
            "just_paused": True,
            "returned_from_away": False,
            "pc_audio_busy": False,
            "l3_status": {"running_count": 0, "queued_count": 0, "recently_finished": None},
        }

        def _fetch_anima(_cid, _wh):
            anima_calls.append(clock["ts"])
            return {
                "character_name": "雷电芽衣",
                "last_turn_at": anima_state["last_turn_at"],
                "hours_since_last_chat": anima_state["hours_since_last_chat"],
                "mood": {"stage": "close", "last_emotion": "warm"},
                "due_open_loops": [],
                "inner_life": {"local_date": "2026-06-15", "slot": "afternoon", "text": "泡了红茶。"},
            }

        def _make_engine():
            return proactive_companion.ProactiveCompanionEngine(
                history_path=self.history_path,
                settings_path=self.settings_path,
                now_fn=lambda: clock["ts"],
                local_dt_fn=lambda ts: datetime.fromtimestamp(ts),
                rhythm_fetcher=lambda: dict(rhythm_ok),
                anima_context_fetcher=_fetch_anima,
                llm_caller=lambda _s, _u, _c: (
                    llm_calls.append(clock["ts"])
                    or '{"action":"SPEAK","emotion":"gentle","text":"忙了一阵啦，喝口水歇歇吧。"}'
                ),
                tts_synthesizer=lambda _t, _e, _c: (b"RIFF....WAVEfake", "mock"),
                speaker_player=lambda wav, vol: (spoken.append((clock["ts"], vol)) or (True, "ok")),
                busy_checker=lambda: False,
                config_reader=lambda: {"prompt": "你是雷电芽衣"},
                weather_reader=lambda _c: "",
            )

        runtime_settings.set_proactive(
            enabled=True,
            delivery_mode="pc_speaker",
            min_cooldown_minutes=90,
            min_since_last_chat_minutes=45,
            settings_path=self.settings_path,
        )
        eng1 = _make_engine()

        # 1. Speak once at T0 -> consecutive_ignored becomes 1 (backed-off cooldown = 180m)
        r0 = eng1.run_once(manual=False)
        self.assertEqual(r0["outcome"], "spoken")
        self.assertEqual(eng1.get_snapshot()["runtime"]["consecutive_ignored"], 1)
        self.assertEqual(len(anima_calls), 1)

        # 2. At T0 + 60m (< 90m base cooldown): blocked WITHOUT querying Anima even if user chatted
        anima_state["last_turn_at"] = t0 + 30 * 60
        anima_state["hours_since_last_chat"] = 0.5
        clock["ts"] = t0 + 60 * 60
        r_pre = eng1.run_once(manual=False)
        self.assertEqual(r_pre["outcome"], "skipped")
        self.assertEqual(r_pre["reason"], "cooldown_backoff")
        self.assertEqual(len(anima_calls), 1)  # no premature Anima fetch

        # 3. At T0 + 100m (>= 90m base cooldown, < 180m backed-off cooldown):
        # User chatted on Companion Studio (18787) at T0 + 30m (70m ago >= 45m).
        # Engine consults Anima ONCE, resets consecutive_ignored -> 0, unblocks and speaks!
        anima_state["last_turn_at"] = t0 + 30 * 60
        anima_state["hours_since_last_chat"] = round(70 / 60.0, 2)
        clock["ts"] = t0 + 100 * 60
        r_unblock = eng1.run_once(manual=False)
        self.assertEqual(r_unblock["outcome"], "spoken")
        self.assertEqual(len(anima_calls), 2)  # fetched ONCE and reused in Stage 2!
        self.assertEqual(len(spoken), 2)

        # 4. Simulate container restart with persisted ignored=2 (eff_cd=360m) and recent cross-client chat (15m ago)
        t_last = clock["ts"]
        with open(self.history_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        st["consecutive_ignored"] = 2
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(st, f)

        eng2 = _make_engine()
        clock["ts"] = t_last + 100 * 60
        anima_state["last_turn_at"] = clock["ts"] - 15 * 60  # 15m ago (< 45m min_since_last_chat)
        anima_state["hours_since_last_chat"] = 0.25
        llm_before = len(llm_calls)
        r_recent = eng2.run_once(manual=False)
        # Backoff was cleared (no longer stuck on cooldown_backoff), and recent_chat_active cleanly blocks!
        self.assertEqual(r_recent["outcome"], "skipped")
        self.assertEqual(r_recent["reason"], "recent_chat_active")
        self.assertEqual(len(llm_calls), llm_before)
        self.assertEqual(eng2.get_snapshot()["runtime"]["consecutive_ignored"], 0)

        # 5. Simulate restart where user opens 18080 Console (`get_snapshot(refresh_live=True)`):
        # Must sync `last_turn_at` from Anima and immediately clear `consecutive_ignored` & cooldown!
        with open(self.history_path, "r", encoding="utf-8") as f:
            st2 = json.load(f)
        st2["consecutive_ignored"] = 3
        st2["last_user_chat_ts"] = 0.0
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(st2, f)

        eng3 = _make_engine()
        anima_state["last_turn_at"] = t_last + 20 * 60  # after t_last
        snap_live = eng3.get_snapshot(refresh_live=True)
        self.assertEqual(snap_live["runtime"]["consecutive_ignored"], 0)
        self.assertEqual(snap_live["runtime"]["effective_cooldown_minutes"], 90)
        self.assertEqual(snap_live["runtime"]["cooldown_remaining_seconds"], 0)
        self.assertEqual(snap_live["runtime"]["last_user_chat_ts"], t_last + 20 * 60)

        # Persisted on disk so a subsequent engine instance also sees cleared backoff
        eng4 = _make_engine()
        self.assertEqual(eng4.get_snapshot(refresh_live=False)["runtime"]["consecutive_ignored"], 0)

        # 6. When Gate 5 (desktop rhythm) ALSO blocks (e.g. fullscreen / away / offline) during
        # cooldown_backoff past base cooldown, tick is NOT solely blocked by backoff -> ZERO Anima calls!
        with open(self.history_path, "r", encoding="utf-8") as f:
            st3 = json.load(f)
        st3["consecutive_ignored"] = 2
        st3["last_user_chat_ts"] = 0.0
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(st3, f)

        rhythm_gate5 = dict(rhythm_ok)

        def _make_engine_with_rhythm():
            return proactive_companion.ProactiveCompanionEngine(
                history_path=self.history_path,
                settings_path=self.settings_path,
                now_fn=lambda: clock["ts"],
                local_dt_fn=lambda ts: datetime.fromtimestamp(ts),
                rhythm_fetcher=lambda: dict(rhythm_gate5),
                anima_context_fetcher=_fetch_anima,
                llm_caller=lambda _s, _u, _c: (
                    llm_calls.append(clock["ts"])
                    or '{"action":"SPEAK","emotion":"gentle","text":"喝口水歇歇吧。"}'
                ),
                tts_synthesizer=lambda _t, _e, _c: (b"RIFF....WAVEfake", "mock"),
                speaker_player=lambda wav, vol: (spoken.append((clock["ts"], vol)) or (True, "ok")),
                busy_checker=lambda: False,
                config_reader=lambda: {"prompt": "你是雷电芽衣"},
                weather_reader=lambda _c: "",
            )

        eng5 = _make_engine_with_rhythm()
        clock["ts"] = t_last + 100 * 60
        for bad_patch in (
            {"is_fullscreen": True},
            {"idle_seconds": 2500.0},
            {"online": False},
        ):
            rhythm_gate5.clear()
            rhythm_gate5.update(rhythm_ok)
            rhythm_gate5.update(bad_patch)
            before_calls = len(anima_calls)
            res_g5 = eng5.run_once(manual=False)
            self.assertEqual(res_g5["reason"], "cooldown_backoff")
            self.assertEqual(
                len(anima_calls),
                before_calls,
                f"Gate 5 blocker {bad_patch} must NOT waste Anima HTTP calls during backoff",
            )

        # 7. Non-finite last_turn_at (inf / nan) is safely rejected without poisoning state or disk
        with eng5._lock:
            self.assertFalse(eng5._sync_anima_last_turn_locked({"last_turn_at": float("inf")}))
            self.assertFalse(eng5._sync_anima_last_turn_locked({"last_turn_at": float("nan")}))
            self.assertEqual(eng5._last_user_chat_ts, 0.0)
            self.assertEqual(eng5._consecutive_ignored, 2)

        # 8. Post-restart Stage 2 recency check uses synced last_turn_at even if hours_since_last_chat is None,
        #    and ignores boolean hours_since_last_chat=False!
        with open(self.history_path, "r", encoding="utf-8") as f:
            st4 = json.load(f)
        st4["consecutive_ignored"] = 0
        st4["last_spoke_ts"] = 0.0
        st4["last_user_chat_ts"] = 0.0
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(st4, f)

        rhythm_gate5.clear()
        rhythm_gate5.update(rhythm_ok)
        eng6 = _make_engine_with_rhythm()
        anima_state["last_turn_at"] = clock["ts"] - 10 * 60  # 10m ago (< 45m)
        anima_state["hours_since_last_chat"] = None
        llm_before6 = len(llm_calls)
        r_s2 = eng6.run_once(manual=False)
        self.assertEqual(r_s2["reason"], "recent_chat_anima")
        self.assertEqual(len(llm_calls), llm_before6)

        # Boolean hours_since_last_chat=False with old last_turn_at must NOT falsely block
        with open(self.history_path, "r", encoding="utf-8") as f:
            st5 = json.load(f)
        st5["last_user_chat_ts"] = 0.0
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(st5, f)
        eng7 = _make_engine_with_rhythm()
        anima_state["last_turn_at"] = clock["ts"] - 120 * 60
        anima_state["hours_since_last_chat"] = False
        r_bool = eng7.run_once(manual=False)
        self.assertEqual(r_bool["outcome"], "spoken")

    def test_llm_local_hostname_parsing_and_error_secret_redaction(self):
        # 1. Exact hostname parsing: allow IPv6 ::1 / loopback, reject substring spoofing!
        self.assertTrue(proactive_companion._is_local_endpoint("http://host.docker.internal:19340/v1/chat/completions"))
        self.assertTrue(proactive_companion._is_local_endpoint("http://127.0.0.1:19340/v1/chat/completions"))
        self.assertTrue(proactive_companion._is_local_endpoint("http://localhost:11434/v1/chat/completions"))
        self.assertTrue(proactive_companion._is_local_endpoint("http://[::1]:19340/v1/chat/completions"))
        self.assertFalse(proactive_companion._is_local_endpoint("https://localhost.evil.com/v1/chat/completions"))
        self.assertFalse(proactive_companion._is_local_endpoint("https://evil.com/host.docker.internal/v1"))
        self.assertFalse(proactive_companion._is_local_endpoint("http://localhost@evil.com/v1/chat/completions"))
        self.assertFalse(proactive_companion._is_local_endpoint("https://api.deepseek.com/v1?ref=127.0.0.1"))
        self.assertFalse(proactive_companion._is_local_endpoint("file://localhost/etc/passwd"))
        self.assertFalse(proactive_companion._is_local_endpoint("http://[::1"))

        captured = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": '{"action":"HEARTBEAT_OK"}'}}]}

        class _FakeClient:
            def __init__(self, **kw):
                captured["client_kw"] = kw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                captured["url"] = url
                return _FakeResp()

        # IPv6 loopback allowed without api_key & trust_env=False
        ipv6_cfg = {
            "selected_module": {"LLM": "LocalV6"},
            "LLM": {"LocalV6": {"base_url": "http://[::1]:19340/v1", "model_name": "qwen", "api_key": ""}},
        }
        with patch.object(proactive_companion.httpx, "Client", _FakeClient):
            out = proactive_companion._default_llm_call("sys", "usr", ipv6_cfg)
        self.assertIn("HEARTBEAT_OK", out)
        self.assertFalse(captured["client_kw"]["trust_env"])

        # Substring-spoofed external host MUST NOT be treated as local (requires api_key; uses trust_env=True)
        spoof_no_key = {
            "selected_module": {"LLM": "SpoofLLM"},
            "LLM": {"SpoofLLM": {"base_url": "https://localhost.evil.com/v1", "model_name": "m", "api_key": ""}},
        }
        with self.assertRaises(RuntimeError):
            proactive_companion._default_llm_call("sys", "usr", spoof_no_key)

        spoof_with_key = {
            "selected_module": {"LLM": "SpoofLLM"},
            "LLM": {
                "SpoofLLM": {
                    "base_url": "https://evil.com/host.docker.internal/v1",
                    "model_name": "m",
                    "api_key": "sk-testkey1234567890",
                }
            },
        }
        with patch.object(proactive_companion.httpx, "Client", _FakeClient):
            proactive_companion._default_llm_call("sys", "usr", spoof_with_key)
        self.assertTrue(captured["client_kw"]["trust_env"])

        # 2. Error secret redaction in run_once() (llm_eval & pc_speaker_play) and refresh_inner_life_now()
        secret_blob = (
            "connect failed https://alice:MyPass999@api.example.com/v1?api_key=sk-secret999999999999 "
            "Authorization: Bearer tok_super_secret_888 key=AIzaSyD1234567890abcdef"
        )
        raw_secrets = ("MyPass999", "sk-secret999999999999", "tok_super_secret_888", "AIzaSyD1234567890abcdef")

        # 2a. llm_eval error redaction
        eng_llm_err = proactive_companion.ProactiveCompanionEngine(
            history_path=self.history_path,
            settings_path=self.settings_path,
            rhythm_fetcher=lambda: {"online": False},
            anima_context_fetcher=lambda _c, _w: {"character_name": "雷电芽衣", "due_open_loops": [], "inner_life": {}},
            llm_caller=lambda _s, _u, _c: (_ for _ in ()).throw(RuntimeError(secret_blob)),
            busy_checker=lambda: False,
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            weather_reader=lambda _c: "",
        )
        res_llm = eng_llm_err.trigger_manual_check(force_dry_run=True, bypass_timing_gates=True)
        self.assertFalse(res_llm["ok"])
        self.assertEqual(res_llm["stage"], "llm_eval")
        for s in raw_secrets:
            self.assertNotIn(s, res_llm["error"])

        # 2b. pc_speaker_play error redaction (both returned error and persisted last_skip_reason)
        runtime_settings.set_proactive(enabled=True, delivery_mode="pc_speaker", settings_path=self.settings_path)
        eng_play_err = proactive_companion.ProactiveCompanionEngine(
            history_path=self.history_path,
            settings_path=self.settings_path,
            now_fn=lambda: datetime(2026, 6, 15, 15, 0).timestamp(),
            local_dt_fn=lambda ts: datetime.fromtimestamp(ts),
            rhythm_fetcher=lambda: {
                "online": True,
                "idle_seconds": 180.0,
                "is_fullscreen": False,
                "activity_category": "coding",
                "just_paused": True,
                "returned_from_away": False,
                "pc_audio_busy": False,
            },
            anima_context_fetcher=lambda _c, _w: {"character_name": "雷电芽衣", "due_open_loops": [], "inner_life": {}},
            llm_caller=lambda _s, _u, _c: '{"action":"SPEAK","emotion":"gentle","text":"喝口水歇歇吧。"}',
            tts_synthesizer=lambda _t, _e, _c: (b"RIFF....WAVEfake", "mock"),
            speaker_player=lambda _w, _v: (False, secret_blob),
            busy_checker=lambda: False,
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            weather_reader=lambda _c: "",
        )
        res_play = eng_play_err.run_once(manual=False)
        self.assertFalse(res_play["ok"])
        self.assertEqual(res_play["stage"], "pc_speaker_play")
        snap_play = eng_play_err.get_snapshot()
        for s in raw_secrets:
            self.assertNotIn(s, res_play["error"])
            self.assertNotIn(s, snap_play["runtime"]["last_skip_reason"])

        # 2c. refresh_inner_life_now() exception redaction
        class _FailClient:
            def __init__(self, **_kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *_a, **_kw):
                raise RuntimeError(secret_blob)

        eng_il = proactive_companion.ProactiveCompanionEngine(
            history_path=self.history_path,
            settings_path=self.settings_path,
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            weather_reader=lambda _c: "",
        )
        with patch.object(proactive_companion.xz_common, "get_anima_token", return_value="anima-tok"), patch.object(
            proactive_companion.httpx, "Client", _FailClient
        ):
            res_il = eng_il.refresh_inner_life_now(use_llm=False)
        self.assertFalse(res_il["ok"])
        for s in raw_secrets:
            self.assertNotIn(s, res_il["error"])

        # 2d. Fallback redactor works even if console.shared is unavailable, AND both primary/fallback
        #     redact Chinese-adjacent keys, single-token URL userinfo, secret/password params, and project tokens
        extended_secret_blob = (
            "连接失败sk-zhsecret1234567890超时 密钥AIzaSyZH1234567890abcdef无效 "
            "https://solo_url_tok_999@api.example.com/v1?secret=QrySecret88&password=QryPwd77 "
            "pc_mcp_token=pc_tok_555 anima_studio_token=anima_tok_444 esp32_auth_key=esp_tok_333"
        )
        extended_leaks = (
            "sk-zhsecret1234567890",
            "AIzaSyZH1234567890abcdef",
            "solo_url_tok_999",
            "QrySecret88",
            "QryPwd77",
            "pc_tok_555",
            "anima_tok_444",
            "esp_tok_333",
        )
        redacted_primary = proactive_companion._redact_error_text(extended_secret_blob)
        for s in extended_leaks:
            self.assertNotIn(s, redacted_primary)

        with patch.dict(sys.modules, {"console.shared": None}):
            redacted_fb = proactive_companion._redact_error_text(secret_blob)
            redacted_fb_ext = proactive_companion._redact_error_text(extended_secret_blob)
        for s in raw_secrets:
            self.assertNotIn(s, redacted_fb)
        for s in extended_leaks:
            self.assertNotIn(s, redacted_fb_ext)

        # 2e. Uncaught exceptions in _tts_synthesizer, _speaker_player, and custom _anima_inner_life_refresher
        #     must be caught and redacted cleanly (never leaking raw secrets)
        eng_exc = proactive_companion.ProactiveCompanionEngine(
            history_path=self.history_path,
            settings_path=self.settings_path,
            now_fn=lambda: datetime(2026, 6, 15, 16, 0).timestamp(),
            local_dt_fn=lambda ts: datetime.fromtimestamp(ts),
            rhythm_fetcher=lambda: {
                "online": True,
                "idle_seconds": 180.0,
                "is_fullscreen": False,
                "activity_category": "coding",
                "just_paused": True,
                "returned_from_away": False,
                "pc_audio_busy": False,
            },
            anima_context_fetcher=lambda _c, _w: {"character_name": "雷电芽衣", "due_open_loops": [], "inner_life": {}},
            anima_inner_life_refresher=lambda _c, _w, _u: (_ for _ in ()).throw(RuntimeError(secret_blob)),
            llm_caller=lambda _s, _u, _c: '{"action":"SPEAK","emotion":"gentle","text":"喝口水歇歇吧。"}',
            tts_synthesizer=lambda _t, _e, _c: (_ for _ in ()).throw(RuntimeError(secret_blob)),
            speaker_player=lambda _w, _v: (_ for _ in ()).throw(RuntimeError(secret_blob)),
            busy_checker=lambda: False,
            config_reader=lambda: {"prompt": "你是雷电芽衣"},
            weather_reader=lambda _c: "",
        )
        res_tts_exc = eng_exc.run_once(manual=False)
        self.assertFalse(res_tts_exc["ok"])
        self.assertEqual(res_tts_exc["stage"], "tts_synth")
        for s in raw_secrets:
            self.assertNotIn(s, res_tts_exc["error"])

        eng_exc._tts_synthesizer = lambda _t, _e, _c: (b"RIFF....WAVEfake", "mock")
        res_play_exc = eng_exc.run_once(manual=False)
        self.assertFalse(res_play_exc["ok"])
        self.assertEqual(res_play_exc["stage"], "pc_speaker_play")
        for s in raw_secrets:
            self.assertNotIn(s, res_play_exc["error"])
            self.assertNotIn(s, eng_exc.get_snapshot()["runtime"]["last_skip_reason"])

        res_il_exc = eng_exc.refresh_inner_life_now(use_llm=False)
        self.assertFalse(res_il_exc["ok"])
        for s in raw_secrets:
            self.assertNotIn(s, res_il_exc["error"])

        eng_exc._anima_inner_life_refresher = lambda _c, _w, _u: {"ok": False, "error": secret_blob}
        res_il_err = eng_exc.refresh_inner_life_now(use_llm=False)
        self.assertFalse(res_il_err["ok"])
        for s in raw_secrets:
            self.assertNotIn(s, res_il_err["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

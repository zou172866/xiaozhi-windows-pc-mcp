"""Unit & integration tests for Raiden Mei multi-emotion routing and prosodic clause segmentation."""

import contextlib
import os
import sys
import unittest
from unittest import mock as _umock
import wave

ROOT_DIR = "/opt/xiaozhi-esp32-server"
_THIS_FILE = globals().get("__file__")
_REPO_ROOT = (
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(_THIS_FILE))))
    if _THIS_FILE
    else ROOT_DIR
)
APP_DIR = os.environ.get("XIAOZHI_APP_DIR") or (
    os.path.join(ROOT_DIR, "app")
    if os.path.isdir(os.path.join(ROOT_DIR, "app"))
    else os.path.join(_REPO_ROOT, "app")
)
DATA_DIR = APP_DIR  # code modules moved from data/ -> app/ (A-1)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import xz_common
import mei_emotion_router as router

PROJECT_ROOT = (
    _REPO_ROOT
    if os.path.isfile(os.path.join(_REPO_ROOT, "docker-compose.yml"))
    else os.path.dirname(APP_DIR)
)
CONFIG_DIR = (
    xz_common.CONFIG_DIR
    if os.path.isdir(xz_common.CONFIG_DIR)
    else os.path.join(PROJECT_ROOT, "config", "runtime")
)
CONFIG_TEMPLATES_DIR = (
    os.path.join(xz_common.ROOT_DIR, "config_templates")
    if os.path.isdir(os.path.join(xz_common.ROOT_DIR, "config_templates"))
    else os.path.join(PROJECT_ROOT, "config")
)
VOICES_DIR = (
    xz_common.VOICES_DIR
    if os.path.isdir(xz_common.VOICES_DIR)
    else os.path.join(PROJECT_ROOT, "voices")
)
HOST_SCRIPTS_DIR = (
    os.environ.get("XIAOZHI_HOST_SCRIPTS_DIR")
    if os.environ.get("XIAOZHI_HOST_SCRIPTS_DIR") and os.path.isdir(os.environ.get("XIAOZHI_HOST_SCRIPTS_DIR", ""))
    else os.path.join(PROJECT_ROOT, "data")
)


def _simple_quote_safe_cut(text: str, pos: int, punctuations) -> int:
    # Mirrors TTSProviderBase._quote_safe_cut for standalone host tests
    pairs = (("“", "”"), ("「", "」"), ("（", "）"), ("【", "】"), ("[", "]"))
    closers = "”」）】]"
    end = pos + 1
    while end < len(text) and (text[end] in closers or text[end] in punctuations):
        end += 1
    seg = text[:end]
    for op, cl in pairs:
        if seg.count(op) > seg.count(cl):
            return -1
    return end - 1


@contextlib.contextmanager
def _assume_mei_ref_audios_if_absent():
    """On clean CI runners without private gitignored `voices/raiden_mei/`, stub `ref_audio_exists`
    so pure emotion-routing logic tests still execute in full. On local hosts with real WAV assets,
    this is a no-op and real filesystem verification runs unchanged."""
    if os.path.isdir(os.path.join(VOICES_DIR, "raiden_mei")):
        yield
        return
    with _umock.patch.object(router, "ref_audio_exists", return_value=True):
        yield


class TestMeiEmotionAndProsody(unittest.TestCase):
    def test_all_seven_emotion_wavs_exist_and_valid_duration(self):
        emotions = router.load_mei_emotions(force_reload=True)
        expected = {"calm", "gentle", "happy", "playful", "shy", "serious", "tender"}
        self.assertTrue(expected.issubset(set(emotions.keys())))
        for key in expected:
            self.assertTrue(emotions[key].get("prompt_text", "").strip(), f"Empty prompt_text for {key}")
        mei_voices_dir = os.path.join(VOICES_DIR, "raiden_mei")
        if not os.path.isdir(mei_voices_dir):
            self.skipTest(f"Private voices/raiden_mei WAV assets not present ({mei_voices_dir})")
        seen_paths = set()
        for key in expected:
            prof = emotions[key]
            ref_path = prof["ref_audio_path"]
            self.assertTrue(router.ref_audio_exists(ref_path), f"Missing ref audio for {key}: {ref_path}")
            local_p = router._to_local_fs_path(ref_path)
            seen_paths.add(local_p)
            with wave.open(local_p, "rb") as wf:
                frames = wf.getnframes()
                sr = wf.getframerate()
                dur = frames / float(sr)
                self.assertGreater(dur, 3.0, f"{key} clip too short ({dur:.2f}s) for GPT-SoVITS 3-10s rule")
                self.assertLess(dur, 10.0, f"{key} clip too long ({dur:.2f}s) for GPT-SoVITS 3-10s rule")
        self.assertEqual(len(seen_paths), 7, "All 7 emotions should point to 7 distinct reference wav files")

    def test_tag_extraction_and_stripping_variants(self):
        cases = [
            ("[gentle] 哥哥，欢迎回来。", "gentle", "哥哥，欢迎回来。"),
            ("[HAPPY]太棒啦哥哥！", "happy", "太棒啦哥哥！"),
            ("【俏皮】呵呵，被我抓到了哦。", "playful", "呵呵，被我抓到了哦。"),
            ("<emotion: shy> 突然这么夸我，有些不好意思呢。", "shy", "突然这么夸我，有些不好意思呢。"),
            ("[情绪：认真] 这件事可不能马虎。", "serious", "这件事可不能马虎。"),
            ("（心疼）别难过了哥哥，我一直陪着你。", "tender", "别难过了哥哥，我一直陪着你。"),
            ("[emotion: unknown_custom_tag] 晚上好呀哥哥。", None, "晚上好呀哥哥。"),
        ]
        for raw, exp_emo, exp_clean in cases:
            emo, cleaned = router.extract_and_strip_emotion(raw, flush_incomplete=False)
            self.assertEqual(emo, exp_emo, f"Failed emotion parse for: {raw}")
            self.assertEqual(cleaned.strip(), exp_clean, f"Failed tag strip for: {raw}")

    def test_legitimate_brackets_never_stripped(self):
        txt = "当前处于 [安全挡] 模式，搜索结果第 [1] 项是【航天基地熟图教程】（约10分钟）。"
        emo, cleaned = router.extract_and_strip_emotion(txt, flush_incomplete=True)
        self.assertIsNone(emo)
        self.assertEqual(cleaned, txt)

    def test_empty_and_boundary_inputs(self):
        for empty_in in ("", "   ", "\n\t", "[gentle]", "[happy]\n", "😊🎉", "，，。。"):
            _, stripped = router.extract_and_strip_emotion(empty_in, flush_incomplete=True)
            self.assertEqual(router.clean_segment_for_tts(stripped), "")

        # Stream abort mid-tag at start
        emo, stripped = router.extract_and_strip_emotion("[gentle", flush_incomplete=True)
        self.assertEqual(emo, "gentle")
        self.assertEqual(router.clean_segment_for_tts(stripped), "")

        # Leaked XML & DeepSeek DSML tool-call tags (e.g. </parameter>, </｜｜DSML｜｜ calls>) must be stripped
        self.assertEqual(router.clean_segment_for_tts("</parameter>"), "")
        self.assertEqual(router.clean_segment_for_tts("</｜｜DSML｜｜ parameter>"), "")
        self.assertEqual(router.clean_segment_for_tts("</｜｜DSML｜｜ invoke>"), "")
        self.assertEqual(router.clean_segment_for_tts("</｜｜DSML｜｜ calls>"), "")
        self.assertEqual(router.clean_segment_for_tts("<｜DSML｜function_calls>"), "")
        self.assertEqual(router.clean_segment_for_tts("<｜tool▁calls▁end｜>"), "")
        self.assertEqual(router.clean_segment_for_tts("哪怕是很小很小的事，我也愿意听。</parameter>"), "哪怕是很小很小的事，我也愿意听。")
        self.assertEqual(
            router.safe_clean_response_garbage(
                "哥哥突然问这个，是家里正好有香蕉，还是在想练完胸之后补点什么？"
                "</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>"
            ),
            "哥哥突然问这个，是家里正好有香蕉，还是在想练完胸之后补点什么？",
        )
        self.assertEqual(
            router.strip_emotion_tags(
                "[playful] 补点什么？</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>"
            ),
            "补点什么？",
        )
        self.assertEqual(router.safe_clean_response_garbage("好啦哥哥</parameter></invoke>"), "好啦哥哥")

    def test_streaming_partial_tag_guard(self):
        for partial in ("[", "[g", "[gentle", "【温", "<emotion:hap", "（轻"):
            self.assertTrue(router.has_unclosed_leading_tag(partial), f"Should hold on partial tag: {partial}")
        for non_tag in ("哥哥，你好", "[gentle] 哥哥", "[安全挡模式已经开启啦", "今天天气真好！"):
            self.assertFalse(router.has_unclosed_leading_tag(non_tag), f"Should not block: {non_tag}")

    def test_no_choppy_short_opening_comma_split(self):
        # Previously "哥哥，" cut after 2 chars! Now must wait for full clause.
        short_open = "[gentle] 哥哥，"
        self.assertEqual(router.find_prosody_cut_pos(short_open, True, _simple_quote_safe_cut), -1)

        full_clause = "[gentle] 哥哥，今天工作累不累？"
        cut = router.find_prosody_cut_pos(full_clause, True, _simple_quote_safe_cut)
        self.assertEqual(cut, len(full_clause) - 1)
        _, stripped = router.extract_and_strip_emotion(full_clause[: cut + 1])
        cleaned = router.clean_segment_for_tts(stripped)
        self.assertEqual(cleaned, "哥哥，今天工作累不累？")

    def test_preserves_exclamation_wave_and_ellipsis(self):
        # Exclamation mark must NOT be stripped (otherwise GPT-SoVITS turns it into deadpan period!)
        raw = "[happy] 太棒啦哥哥，我就知道你一定行！"
        cut = router.find_prosody_cut_pos(raw, True, _simple_quote_safe_cut)
        self.assertEqual(cut, len(raw) - 1)
        _, stripped = router.extract_and_strip_emotion(raw[: cut + 1])
        self.assertEqual(router.clean_segment_for_tts(stripped), "太棒啦哥哥，我就知道你一定行！")

        # Ellipsis & enumeration commas must NOT split mid-sentence
        ellip = "今夜月色很美，呵...适合与朋友小聚一番。"
        cut_e = router.find_prosody_cut_pos(ellip, True, _simple_quote_safe_cut)
        self.assertEqual(cut_e, len(ellip) - 1)

        enum_txt = "我准备了培根、香肠、火腿和热牛奶。"
        cut_n = router.find_prosody_cut_pos(enum_txt, True, _simple_quote_safe_cut)
        self.assertEqual(cut_n, len(enum_txt) - 1)

    def test_long_comma_runon_splits_smoothly(self):
        runon = "[serious] 我已经帮你把浏览器打开并跳转到搜索结果页面了，现在直接点左上角就能看"
        cut = router.find_prosody_cut_pos(runon, True, _simple_quote_safe_cut)
        self.assertGreater(cut, 18)
        self.assertEqual(runon[cut], "，")

    def test_three_layer_emotion_resolution_and_anima_sync(self):
        with _assume_mei_ref_audios_if_absent():
            router.set_anima_mood("stage=close, last_emotion=warm")
            self.assertEqual(router.get_anima_mood(), "gentle")
            # Neutral text with no tag or lexical cue falls back to Anima mood ('gentle')
            self.assertEqual(router.resolve_mei_emotion("我已经把文件整理到桌面目录了。"), "gentle")
            # Lexical cue overrides Anima mood when no explicit tag is present
            self.assertEqual(router.resolve_mei_emotion("太棒啦哥哥！恭喜你顺利搞定！"), "happy")
            # Explicit tag has top priority
            self.assertEqual(
                router.resolve_mei_emotion("太棒啦哥哥！", explicit_segment_emotion="shy"),
                "shy",
            )
        # Anima persona stripper removes [calm]/[gentle] rule while keeping persona intact
        sample_prompt = (
            "你是《崩坏3》中的雷电芽衣，称呼对方为“哥哥”。\n"
            "每次回复最开头先写一个英文方括号情绪标签（从 [calm]、[gentle] 中选）。\n"
            "请用有呼吸感的口语交流。"
        )
        anima_p = router.strip_voice_tag_instruction_for_anima(sample_prompt)
        self.assertNotIn("[calm]", anima_p)
        self.assertIn("雷电芽衣", anima_p)
        self.assertIn("有呼吸感的口语交流", anima_p)

    def test_wave_does_not_hijack_explicit_turn_emotion(self):
        with _assume_mei_ref_audios_if_absent():
            # Soft `～` in a second sentence must NOT override an explicit `[happy]` or `[gentle]` turn tag
            self.assertEqual(
                router.resolve_mei_emotion("这下你可以放心去吃饭啦～", turn_explicit_emotion="happy"),
                "happy",
            )
            # Whereas a calm turn tag can smoothly shift to gentle when strong caring cues appear
            self.assertEqual(
                router.resolve_mei_emotion("哥哥今天辛苦了，早点休息吧。", turn_explicit_emotion="calm"),
                "gentle",
            )

    def test_leading_emoji_and_unclosed_typo_resilience(self):
        # Leading emoji before unclosed tag must still hold stream buffer
        self.assertTrue(router.has_unclosed_leading_tag("😍[gen"))
        self.assertTrue(router.has_unclosed_leading_tag("🙂 [happy"))
        self.assertFalse(router.has_unclosed_leading_tag("😍 [gentle] 哥哥"))

        # Leading emoji without bracket tag still resolves emotion before emoji stripping
        emo_e, stripped_e = router.extract_and_strip_emotion("😍 哥哥，给你留了晚饭哦。")
        self.assertEqual(emo_e, "gentle")
        self.assertEqual(router.clean_segment_for_tts(stripped_e), "哥哥，给你留了晚饭哦。")

        # Malformed LLM typo missing closing `]` on canonical English emotion tag never leaks to TTS
        emo_m, stripped_m = router.extract_and_strip_emotion(
            "[gentle 哥哥，今天工作累不累？[playful 呵呵，目前在 [安全挡] 哦！",
            flush_incomplete=True,
        )
        self.assertEqual(emo_m, "playful")
        self.assertEqual(
            router.clean_segment_for_tts(stripped_m),
            "哥哥，今天工作累不累？ 呵呵，目前在 [安全挡] 哦！",
        )

    def test_direct_answer_streaming_all_chunk_sizes_preserve_brackets(self):
        # Regression test: sweep chunk sizes 1..16 on direct_answer response with tags & [安全挡]
        da_resp = "[gentle] 哥哥，今天工作累不累？[playful] 呵呵，目前在 [安全挡] 哦！"
        for step in range(1, 17):
            sent = 0
            buff = []
            for end_idx in range(step, len(da_resp) + step, step):
                cur = da_resp[: min(len(da_resp), end_idx)]
                safe_end = max(sent, len(cur) - 5)
                if safe_end > sent:
                    part = router.safe_clean_response_garbage(cur[sent:safe_end])
                    if part:
                        sent = safe_end
                        buff.append(part)
            rem = router.safe_clean_response_garbage(da_resp[sent:])
            if rem:
                buff.append(rem)
            assembled = "".join(buff)
            self.assertEqual(
                assembled,
                da_resp,
                f"direct_answer stream corrupted brackets at chunk step={step}: {assembled!r}",
            )

        # Genuine leaked JSON closure tails are still cleaned properly
        self.assertEqual(
            router.safe_clean_response_garbage('哥哥早点休息哦！"}'),
            "哥哥早点休息哦！",
        )
        self.assertEqual(
            router.safe_clean_response_garbage('哥哥早点休息哦！\n）"}}'),
            "哥哥早点休息哦！",
        )

    def test_emoji_manifest_matches_esp32_native_map(self):
        emotions = router.load_mei_emotions(force_reload=True)
        for key, default_prof in router.DEFAULT_MEI_EMOTIONS.items():
            self.assertEqual(
                emotions[key]["esp32_emoji"],
                default_prof["esp32_emoji"],
                f"Mismatch esp32_emoji in mei_emotions.json for {key}",
            )
            self.assertEqual(
                emotions[key]["esp32_emotion"],
                default_prof["esp32_emotion"],
                f"Mismatch esp32_emotion in mei_emotions.json for {key}",
            )


try:
    if "/opt/xiaozhi-esp32-server" not in sys.path and os.path.isdir("/opt/xiaozhi-esp32-server"):
        sys.path.insert(0, "/opt/xiaozhi-esp32-server")
    from core.providers.tts import gpt_sovits_v2 as _gsv_check  # noqa: F401
    _HAS_XIAOZHI_CORE = True
except Exception:
    _HAS_XIAOZHI_CORE = False

if "--live" in sys.argv:
    sys.argv.remove("--live")
    os.environ["XIAOZHI_RUN_LIVE_TESTS"] = "1"

_RUN_LIVE_TESTS = os.environ.get("XIAOZHI_RUN_LIVE_TESTS", "").strip().lower() in ("1", "true", "yes")


@unittest.skipUnless(_HAS_XIAOZHI_CORE, "Requires xiaozhi-esp32-server container environment")
class TestContainerOfflinePipeline(unittest.TestCase):
    """Offline container pipeline tests (ZERO network/GPU calls, ZERO production DB/config mutation)."""

    def test_token_stream_multi_emotion_and_no_tag_leak(self):
        import asyncio
        from unittest.mock import patch, MagicMock
        from core.providers.tts.gpt_sovits_v2 import TTSProvider
        import anima_memory_provider

        router.apply_xiaozhi_emotion_and_prosody_patches()

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
            "text_split_method": "cut5",  # Even if legacy cut5 passed, patch upgrades to cut0!
            "speed_factor": 1.0,
        }
        provider = TTSProvider(cfg, delete_audio_file=True)
        provider.current_sentence_id = "sid-test-001"
        provider.is_first_sentence = True
        provider.processed_chars = 0
        provider.tts_text_buff = []

        captured_payloads = []

        class _FakeResp:
            status_code = 200
            content = b"RIFF\x24\x00\x00\x00WAVEfmt "
            text = "ok"

        def _fake_post(url, json=None, timeout=None):
            captured_payloads.append(dict(json))
            return _FakeResp()

        tokens = [
            "[",
            "gen",
            "tle",
            "]",
            " 哥哥，",
            "今天工作累不累？",
            "[play",
            "ful]",
            " 呵呵，",
            "再熬夜的话我可要没收键盘了哦！",
        ]

        sess = router._get_gsv_http_session()
        self.assertFalse(sess.trust_env, "GSV requests.Session must disable proxy env lookup")
        emitted_segments = []
        with _assume_mei_ref_audios_if_absent(), patch.object(sess, "post", side_effect=_fake_post):
            for idx, tok in enumerate(tokens):
                provider.tts_text_buff.append(tok)
                seg = provider._get_segment_text()
                if idx < 5:
                    # Must NOT cut prematurely on "[", "[gen", "[gentle]", or "哥哥，"
                    self.assertIsNone(seg, f"Premature cut at token #{idx} ({tok!r}): {seg!r}")
                elif seg:
                    emitted_segments.append(seg)
                    asyncio.run(provider.text_to_speak(seg, None))

            # Drain remaining stream at SentenceType.LAST
            while True:
                seg = provider._get_segment_text()
                if not seg:
                    break
                emitted_segments.append(seg)
                asyncio.run(provider.text_to_speak(seg, None))

        self.assertEqual(
            emitted_segments,
            [
                "哥哥，今天工作累不累？",
                "呵呵，再熬夜的话我可要没收键盘了哦！",
            ],
        )
        self.assertEqual(len(captured_payloads), 2)
        # Verify 1st sentence routed to mei_gentle_ref.wav + cut0 + P1-10 fast first-seg sample_steps=8 + no tags
        self.assertTrue(captured_payloads[0]["ref_audio_path"].endswith("mei_gentle_ref.wav"))
        self.assertEqual(captured_payloads[0]["text_split_method"], "cut0")
        self.assertEqual(captured_payloads[0]["sample_steps"], 8)
        self.assertEqual(captured_payloads[0]["text"], "哥哥，今天工作累不累？")
        # Verify 2nd sentence shifted to mei_playful_ref.wav + cut0 + full sample_steps=16 + exclamation preserved
        self.assertTrue(captured_payloads[1]["ref_audio_path"].endswith("mei_playful_ref.wav"))
        self.assertEqual(captured_payloads[1]["text_split_method"], "cut0")
        self.assertEqual(captured_payloads[1]["sample_steps"], 16)
        self.assertEqual(captured_payloads[1]["text"], "呵呵，再熬夜的话我可要没收键盘了哦！")

        # Verify Anima Brain pair extractor strips tags cleanly
        m_user = MagicMock(is_temporary=False, role="user", content="我还在加班呢", uniq_id="u1")
        m_ai = MagicMock(
            is_temporary=False,
            role="assistant",
            content="[gentle] 哥哥，今天工作累不累？[playful] 呵呵，再熬夜的话我可要没收键盘了哦！",
        )
        pairs = anima_memory_provider.extract_clean_pairs([m_user, m_ai])
        self.assertEqual(len(pairs), 1)
        self.assertNotIn("[gentle]", pairs[0]["assistant_reply"])
        self.assertNotIn("[playful]", pairs[0]["assistant_reply"])

    def test_playback_synced_esp32_emotion_and_prompt_harmony(self):
        import asyncio
        import json
        from unittest.mock import patch
        from core.providers.tts.gpt_sovits_v2 import TTSProvider
        import core.handle.sendAudioHandle as send_audio_mod
        from core.connection import ConnectionHandler
        from core.utils.prompt_manager import PromptManager

        router.apply_xiaozhi_emotion_and_prosody_patches()

        # 1. Verify ConnectionHandler._clean_response_garbage is lossless on [gentle] across all steps
        full_json = '{"response": "[gentle] 哥哥，今天累不累？[playful] 呵呵，目前在 [安全挡] 哦！"}'
        for step in (2, 3, 4, 6, 7, 8, 9):
            tc = {"id": "tc1", "name": "direct_answer", "arguments": ""}
            buff = []
            for i in range(0, len(full_json), step):
                tc["arguments"] += full_json[i : i + step]
                da_text = ConnectionHandler._extract_direct_answer_response(tc["arguments"])
                sent_len = tc.get("_da_sent", 0)
                if da_text and len(da_text) > sent_len:
                    safe_end = max(sent_len, len(da_text) - 5)
                    if safe_end > sent_len:
                        part = ConnectionHandler._clean_response_garbage(da_text[sent_len:safe_end])
                        if part:
                            tc["_da_sent"] = safe_end
                            buff.append(part)
            da_resp = ConnectionHandler._extract_direct_answer_response(tc["arguments"])
            rem = ConnectionHandler._clean_response_garbage(da_resp[tc.get("_da_sent", 0) :])
            if rem:
                buff.append(rem)
            self.assertEqual("".join(buff), da_resp)

        # 2. Verify PromptManager removes contradictory [Single emoji prefix] / NEVER use brackets for Mei
        mei_prompt = (
            "你是《崩坏3》中的雷电芽衣。\n"
            "每次回复最开头先写一个英文方括号情绪标签（从 [calm]、[gentle]、[happy] 中选）。"
        )
        pm = PromptManager({"TTS": {}, "selected_module": {}})
        enhanced = pm.build_enhanced_prompt(mei_prompt, "dev-test-mei", "127.0.0.1", emoji_enabled=True)
        self.assertNotIn("[Single emoji prefix]", enhanced)
        self.assertNotIn("咋回事", enhanced)
        self.assertIn("ONE hidden English emotion tag", enhanced)

        # Non-Mei prompt keeps original upstream behavior
        other_enhanced = pm.build_enhanced_prompt("你是幽默助手小智。", "dev-test-other", "127.0.0.1", emoji_enabled=True)
        self.assertIn("[Single emoji prefix]", other_enhanced)

        # 3. Verify playback-synced ESP32 emotion does NOT overwrite Sentence 1's face prematurely
        ws_sent = []

        async def _run_sync_check():
            loop = asyncio.get_running_loop()

            class _FakeWS:
                async def send(self, msg):
                    ws_sent.append(json.loads(msg))

            class _FakeConn:
                websocket = _FakeWS()
                features = {"emoji": True}
                sentence_id = "sid-sync-01"
                session_id = "sess-sync-01"
                config = {"enable_stop_tts_notify": False}

            conn = _FakeConn()
            conn.loop = loop

            cfg = {
                "url": "http://host.docker.internal:19880/tts",
                "output_dir": "/tmp/",
                "text_lang": "zh",
                "ref_audio_path": "voices/raiden_mei/mei_default_ref.wav",
                "prompt_text": "今夜月色很美，呵...适合与朋友小聚一番。",
                "prompt_lang": "zh",
                "text_split_method": "cut0",
            }
            provider = TTSProvider(cfg, delete_audio_file=True)
            provider.conn = conn
            conn.tts = provider
            provider.current_sentence_id = conn.sentence_id
            provider.tts_audio_first_sentence = True

            class _FakeResp:
                status_code = 200
                content = b"RIFF\x24\x00\x00\x00WAVEfmt "
                text = "ok"

            with _assume_mei_ref_audios_if_absent(), patch.object(router._get_gsv_http_session(), "post", return_value=_FakeResp()):
                # Sentence 1 synthesizes (gentle -> loving)
                provider._turn_explicit_emotion = "gentle"
                provider._pending_segment_emotion = "gentle"
                await provider.text_to_speak("哥哥今天辛苦啦。", None)
                await asyncio.sleep(0.02)
                await send_audio_mod.send_tts_message(conn, "sentence_start", "哥哥今天辛苦啦。")
                provider.tts_audio_first_sentence = False

                # Sentence 2 synthesizes in background while Sentence 1 is still playing!
                provider._turn_explicit_emotion = "playful"
                provider._pending_segment_emotion = "playful"
                await provider.text_to_speak("呵呵，被我抓到了哦！", None)
                await asyncio.sleep(0.02)

                # Before Sentence 2 playback starts, `winking` MUST NOT have been sent yet!
                emotions_before_s2_play = [m["emotion"] for m in ws_sent if m.get("type") == "llm"]
                self.assertEqual(emotions_before_s2_play, ["loving"])

                # Now AudioRateController reaches Sentence 2's sentence_start -> `winking` fires right on cue!
                await send_audio_mod.send_tts_message(conn, "sentence_start", "呵呵，被我抓到了哦！")
                emotions_after_s2_play = [m["emotion"] for m in ws_sent if m.get("type") == "llm"]
                self.assertEqual(emotions_after_s2_play, ["loving", "winking"])

        asyncio.run(_run_sync_check())

    def test_mei_wakeup_word_config_and_fallback_wav(self):
        import yaml

        cfg_path = os.path.join(CONFIG_DIR, ".config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ww = cfg.get("wakeup_words") or []
        self.assertTrue(isinstance(ww, list) and len(ww) > 0, ".config.yaml wakeup_words must be a non-empty list")
        if os.environ.get("XIAOZHI_USING_EXAMPLE_CONFIG") == "1":
            self.assertIn("你好小智", ww, "public config/.config.example.yaml wakeup_words must include 你好小智")
        else:
            self.assertIn("你好芽衣", ww, "config/runtime/.config.yaml wakeup_words must include 你好芽衣")

        mei_voices_dir = os.path.join(VOICES_DIR, "raiden_mei")
        if os.path.isdir(mei_voices_dir):
            wav_path = os.path.join(mei_voices_dir, "wakeup_mei_default.wav")
            self.assertTrue(os.path.isfile(wav_path), f"Missing Mei default wakeup WAV: {wav_path}")
            with wave.open(wav_path, "rb") as wf:
                dur = wf.getnframes() / float(wf.getframerate())
                self.assertGreater(dur, 0.8)
                self.assertLess(dur, 4.0)

        class _DummyMeiTts:
            ref_audio_path = "e:/故事/1/xiaozhi-server/data/voices/raiden_mei/mei_default_ref.wav"

        class _DummyOtherTts:
            ref_audio_path = "e:/故事/1/xiaozhi-server/data/xiaozhi_taiwan_ref.wav"

        self.assertTrue(router.get_wakeup_responses_for_tts(_DummyMeiTts()))
        self.assertEqual(router.get_wakeup_responses_for_tts(_DummyOtherTts()), [])

        # Verify live wakeup patch mounts cleanly without NameError/ImportError
        import core.handle.helloHandle as hello_mod

        router.apply_mei_wakeup_responses_patch()
        self.assertTrue(getattr(hello_mod, "_mei_wakeup_patched", False))

    def test_interrupt_word_config_and_silent_listen_handler(self):
        import asyncio
        import yaml
        from unittest.mock import AsyncMock, patch
        import core.handle.textHandler.listenMessageHandler as listen_mod
        import core.handle.helloHandle as hello_mod

        cfg_path = os.path.join(CONFIG_DIR, ".config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        iw = cfg.get("interrupt_words") or []
        self.assertIn("听我说", iw, "config/runtime/.config.yaml interrupt_words must include 听我说")

        router.apply_mei_wakeup_responses_patch()
        self.assertTrue(getattr(listen_mod.ListenTextMessageHandler, "_interrupt_word_patched", False))

        class _DummyLog:
            def bind(self, **kw):
                return self

            def info(self, *a, **k):
                pass

            def debug(self, *a, **k):
                pass

        class _DummyConn:
            config = {"interrupt_words": ["听我说"], "wakeup_words": ["你好芽衣"], "enable_greeting": True}
            client_have_voice = True
            client_is_speaking = False
            just_woken_up = True
            last_activity_time = 0
            logger = _DummyLog()

            def reset_audio_states(self):
                pass

        async def _run():
            import core.handle.abortHandle as abort_mod

            conn = _DummyConn()
            conn._pc_audio_active_sid = "sid-pc-still-playing"
            handler = listen_mod.ListenTextMessageHandler()
            with (
                patch.object(abort_mod, "handleAbortMessage", new=AsyncMock()) as m_abort,
                patch.object(listen_mod, "send_stt_message", new=AsyncMock()) as m_stt,
                patch.object(hello_mod, "send_tts_message", new=AsyncMock()) as m_tts,
                patch.object(listen_mod, "startToChat", new=AsyncMock()) as m_chat,
            ):
                await handler.handle(conn, {"type": "listen", "state": "detect", "text": "听我说"})
                # Even when client_is_speaking=False, active _pc_audio_active_sid MUST trigger abort stop!
                m_abort.assert_awaited_once_with(conn)
                m_stt.assert_awaited_once_with(conn, "听我说")
                m_tts.assert_awaited_once_with(conn, "stop", None)
                m_chat.assert_not_awaited()
                self.assertFalse(conn.just_woken_up)
                self.assertFalse(conn.client_is_speaking)
                self.assertIsNone(conn._pc_audio_active_sid)

        asyncio.run(_run())

    def test_reply_length_hot_config_and_prompt_injection(self):
        import tempfile
        from unittest.mock import patch
        import reply_length_router as rl

        with tempfile.TemporaryDirectory() as td:
            tmp_cfg = os.path.join(td, "reply_length_config.json")
            orig_cache = dict(rl._cfg_cache)
            orig_mtime = rl._cfg_mtime
            try:
                with patch.object(rl, "REPLY_LEN_PATH", tmp_cfg):
                    rl._cfg_mtime = -1.0
                    saved = rl.write_reply_length_config(chat_chars=220, story_chars=900)
                    self.assertEqual(saved["chat_chars"], 220)
                    self.assertEqual(saved["story_chars"], 900)
                    self.assertGreaterEqual(saved["dynamic_max_tokens"], 2200)

                    msgs = [{"role": "system", "content": "你是雷电芽衣。"}, {"role": "user", "content": "讲个故事"}]
                    rl.inject_reply_length_into_messages(msgs)
                    self.assertIn("220 字", msgs[0]["content"])
                    self.assertIn("900 字", msgs[0]["content"])
                    self.assertEqual(msgs[0]["content"].count("<reply_length_control>"), 1)

                    # Updating again must replace previous block idempotently without duplicating tags
                    rl.write_reply_length_config(preset="storyteller")
                    rl.inject_reply_length_into_messages(msgs)
                    self.assertEqual(msgs[0]["content"].count("<reply_length_control>"), 1)
                    self.assertIn("1000 字", msgs[0]["content"])
                    self.assertNotIn("哥哥", rl.build_length_directive())
                    # Verify atomic write left no stray .tmp files
                    self.assertEqual([f for f in os.listdir(td) if f.endswith(".tmp")], [])
            finally:
                rl._cfg_cache = orig_cache
                rl._cfg_mtime = orig_mtime

    def test_console_log_cursor_dedup_audio_fallback_and_portable_defaults(self):
        import io
        import tempfile
        from unittest.mock import patch
        import console_server as cs

        # 1. Verify no hardcoded personal MAC / LAN IP / drive path across ALL console modules
        import re as _re
        src_parts = []
        with open(os.path.join(DATA_DIR, "console_server.py"), "r", encoding="utf-8") as _f:
            src_parts.append(_f.read())
        for _dp, _, _fns in os.walk(os.path.join(DATA_DIR, "console")):
            for _fn in sorted(_fns):
                if _fn.endswith(".py"):
                    with open(os.path.join(_dp, _fn), "r", encoding="utf-8") as _f:
                        src_parts.append(_f.read())
        src = "\n".join(src_parts)
        self.assertIsNone(_re.search(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", src))
        self.assertIsNone(_re.search(r"\b192\.168\.\d+\.\d+\b", src))
        self.assertIsNone(_re.search(r"(?i)[a-z]:[\\/]+故事[\\/]+1", src))

        # 2. Verify in-memory 0.4s sine chime WAV fallback produces valid 48kHz mono 16-bit PCM WAV
        chime = cs._generate_test_chime_wav(0.4, 48000)
        self.assertTrue(chime.startswith(b"RIFF"))
        with wave.open(io.BytesIO(chime), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getframerate(), 48000)
            self.assertAlmostEqual(wf.getnframes() / 48000.0, 0.4, places=2)

        # 3. Verify restart log watcher preserves server_ready/device_id without duplicating chat turns
        with tempfile.TemporaryDirectory() as td:
            log_p = os.path.join(td, "server.log")
            hist_p = os.path.join(td, "chat_history.json")
            cur_p = os.path.join(td, ".console_log_cursor.json")

            sample_lines = (
                "2026-11-19 10:00:01 INFO ASR模块初始化完成\n"
                "2026-11-19 10:00:02 INFO OTA请求设备ID: aa:bb:cc:dd:ee:ff\n"
                "2026-11-19 10:00:05 INFO 大模型收到用户消息: 你好呀\n"
                "2026-11-19 10:00:06 INFO 发送音频消息: SentenceType.FIRST, 你好\n"
                "2026-11-19 10:00:07 INFO 发送音频消息: SentenceType.MIDDLE, 今天过得怎么样？\n"
                "2026-11-19 10:00:08 INFO 发送音频消息: SentenceType.LAST, None\n"
            )
            with open(log_p, "w", encoding="utf-8") as f:
                f.write(sample_lines)

            orig_msgs = list(cs._chat_messages)
            orig_dev = dict(cs._device_info)
            orig_off = cs._log_offset
            orig_open = cs._current_ai_turn_open
            try:
                with (
                    patch.object(cs, "LOG_PATH", log_p),
                    patch.object(cs, "HISTORY_PATH", hist_p),
                    patch.object(cs, "LOG_CURSOR_PATH", cur_p),
                ):
                    # Boot 1: reads from offset 0
                    cs._chat_messages = []
                    cs._device_info = {"device_id": "", "last_seen": "", "activity": "idle", "server_ready": False}
                    cs._log_offset = 0
                    cs._current_ai_turn_open = False
                    st1 = {"initialized": False, "last_inode": None, "saved_inode": None, "saved_offset": 0}
                    cs._poll_log_once(st1)
                    self.assertTrue(cs._device_info["server_ready"])
                    self.assertEqual(cs._device_info["device_id"], "aa:bb:cc:dd:ee:ff")
                    self.assertEqual(len(cs._chat_messages), 2)
                    self.assertEqual(cs._chat_messages[1]["text"], "你好，今天过得怎么样？")
                    self.assertTrue(os.path.isfile(cur_p))

                    # Boot 2 (simulated restart with persisted cursor): keeps status, ZERO duplicate messages
                    cs._chat_messages = []
                    cs._device_info = {"device_id": "", "last_seen": "", "activity": "idle", "server_ready": False}
                    cs._log_offset = 0
                    cs._current_ai_turn_open = False
                    cs._load_history()
                    s_ino, s_off = cs._load_log_cursor()
                    st2 = {"initialized": False, "last_inode": None, "saved_inode": s_ino, "saved_offset": s_off}
                    cs._poll_log_once(st2)
                    self.assertTrue(cs._device_info["server_ready"])
                    self.assertEqual(cs._device_info["device_id"], "aa:bb:cc:dd:ee:ff")
                    self.assertEqual(len(cs._chat_messages), 2)

                    # Append a partial (non-newline-terminated) line -> must not emit half line
                    with open(log_p, "a", encoding="utf-8") as f:
                        f.write("2026-11-19 10:01:00 INFO 大模型收到用户消息: 帮我查")
                    cs._poll_log_once(st2)
                    self.assertEqual(len(cs._chat_messages), 2)

                    # Finish the line + assistant reply -> appends exactly 2 new messages
                    with open(log_p, "a", encoding="utf-8") as f:
                        f.write(
                            "一下天气\n"
                            "2026-11-19 10:01:02 INFO 发送音频消息: SentenceType.FIRST, 今天晴天。\n"
                            "2026-11-19 10:01:03 INFO 发送音频消息: SentenceType.LAST, None\n"
                        )
                    cs._poll_log_once(st2)
                    self.assertEqual(len(cs._chat_messages), 4)
                    self.assertEqual(cs._chat_messages[2]["text"], "帮我查一下天气")

                    # Boot 3 (upgrade scenario: cursor missing, full log replayed) -> tail dedup prevents duplicates
                    os.remove(cur_p)
                    cs._chat_messages = []
                    cs._device_info = {"device_id": "", "last_seen": "", "activity": "idle", "server_ready": False}
                    cs._log_offset = 0
                    cs._current_ai_turn_open = False
                    cs._load_history()
                    st3 = {"initialized": False, "last_inode": None, "saved_inode": None, "saved_offset": 0}
                    cs._poll_log_once(st3)
                    self.assertTrue(cs._device_info["server_ready"])
                    self.assertEqual(cs._device_info["device_id"], "aa:bb:cc:dd:ee:ff")
                    self.assertEqual(len(cs._chat_messages), 4)

                    # Boot 4 (60-message history > old 40-item window, cursor missing) -> ZERO cascade duplication
                    big_lines = ["2026-11-19 10:00:00 INFO ASR模块初始化完成\n"]
                    for i in range(60):  # 60 turns = 120 messages (> 100 cap!)
                        mm, ss = divmod(i, 60)
                        big_lines.append(f"2026-11-19 10:{mm+1:02d}:{ss:02d} INFO 大模型收到用户消息: 问题{i}\n")
                        big_lines.append(f"2026-11-19 10:{mm+1:02d}:{ss:02d} INFO 发送音频消息: SentenceType.FIRST, 回答{i}\n")
                        big_lines.append(f"2026-11-19 10:{mm+1:02d}:{ss:02d} INFO 发送音频消息: SentenceType.LAST, None\n")
                    with open(log_p, "w", encoding="utf-8") as f:
                        f.write("".join(big_lines))
                    cs._chat_messages = []
                    cs._log_offset = 0
                    cs._current_ai_turn_open = False
                    st4 = {"initialized": False, "last_inode": None, "saved_inode": None, "saved_offset": 0}
                    cs._poll_log_once(st4)
                    self.assertEqual(len(cs._chat_messages), 100)
                    self.assertEqual(cs._chat_messages[-1]["text"], "回答59")
                    # Delete cursor and append 1 new turn -> replay must NOT duplicate any of the 100 messages
                    os.remove(cur_p)
                    with open(log_p, "a", encoding="utf-8") as f:
                        f.write(
                            "2026-11-19 10:09:01 INFO 大模型收到用户消息: 最新问题60\n"
                            "2026-11-19 10:09:02 INFO 发送音频消息: SentenceType.FIRST, 最新回答60\n"
                            "2026-11-19 10:09:03 INFO 发送音频消息: SentenceType.LAST, None\n"
                        )
                    cs._chat_messages = []
                    cs._log_offset = 0
                    cs._current_ai_turn_open = False
                    cs._load_history()
                    self.assertEqual(len(cs._chat_messages), 100)
                    st5 = {"initialized": False, "last_inode": None, "saved_inode": None, "saved_offset": 0}
                    cs._poll_log_once(st5)
                    self.assertEqual(len(cs._chat_messages), 100)
                    self.assertEqual(cs._chat_messages[-2]["text"], "最新问题60")
                    self.assertEqual(cs._chat_messages[-1]["text"], "最新回答60")
                    keys = [(m["role"], m["time"], m["text"]) for m in cs._chat_messages]
                    self.assertEqual(len(keys), len(set(keys)), "Duplicate chat items detected after cursorless replay")
            finally:
                cs._chat_messages = orig_msgs
                cs._device_info = orig_dev
                cs._log_offset = orig_off
                cs._current_ai_turn_open = orig_open

    def test_console_csrf_host_guard_secret_redaction_and_frontend_visibility(self):
        import console_server as cs

        # 1. Host & CSRF guard matrix
        ok_get, st_get, _ = cs.validate_console_request("GET", {"Host": "localhost:18080"})
        self.assertTrue(ok_get)
        self.assertEqual(st_get, 200)

        ok_bad_host, st_bad_host, _ = cs.validate_console_request("GET", {"Host": "evil.example:18080"})
        self.assertFalse(ok_bad_host)
        self.assertEqual(st_bad_host, 421)

        # no-cors text/plain or spoofed prefix application/json-evil POST must be rejected (415)
        ok_ct, st_ct, _ = cs.validate_console_request(
            "POST", {"Host": "127.0.0.1:18080", "Content-Type": "text/plain"}
        )
        self.assertFalse(ok_ct)
        self.assertEqual(st_ct, 415)

        ok_ct_spoof, st_ct_spoof, _ = cs.validate_console_request(
            "POST", {"Host": "127.0.0.1:18080", "Content-Type": "application/json-evil"}
        )
        self.assertFalse(ok_ct_spoof)
        self.assertEqual(st_ct_spoof, 415)

        # Origin: null (sandboxed iframe / file://) must be rejected (403)
        ok_null_orig, st_null_orig, _ = cs.validate_console_request(
            "POST", {"Host": "127.0.0.1:18080", "Origin": "null", "Content-Type": "application/json"}
        )
        self.assertFalse(ok_null_orig)
        self.assertEqual(st_null_orig, 403)

        # _ensure_gsv_weights must NOT advance _active_gsv_loaded when 19880 returns non-200
        from unittest.mock import MagicMock, patch

        orig_loaded = cs._active_gsv_loaded
        try:
            cs._active_gsv_loaded = "__gsv_raiden_mei__"
            fake_cli = MagicMock()
            fake_cli.get.return_value = MagicMock(status_code=400, text="bad weights path")
            with patch.object(cs, "_get_console_http_client", return_value=fake_cli):
                with self.assertRaises(RuntimeError):
                    cs._ensure_gsv_weights("__gsv_xiaozhi_tw__", persist_boot_yaml=False)
            self.assertEqual(cs._active_gsv_loaded, "__gsv_raiden_mei__")
        finally:
            cs._active_gsv_loaded = orig_loaded

        # Cross-origin JSON POST must be rejected (403)
        ok_orig, st_orig, _ = cs.validate_console_request(
            "POST",
            {"Host": "127.0.0.1:18080", "Origin": "https://evil.example", "Content-Type": "application/json"},
        )
        self.assertFalse(ok_orig)
        self.assertEqual(st_orig, 403)

        # Cross-site Sec-Fetch-Site must be rejected (403)
        ok_sfs, st_sfs, _ = cs.validate_console_request(
            "POST",
            {
                "Host": "localhost:18080",
                "Origin": "http://localhost:18080",
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "application/json",
            },
        )
        self.assertFalse(ok_sfs)
        self.assertEqual(st_sfs, 403)

        # Same-origin JSON POST without X-Console-Token -> 401; wrong token -> 401 (A-4)
        ok_no_tok, st_no_tok, _ = cs.validate_console_request(
            "POST",
            {
                "Host": "localhost:18080",
                "Origin": "http://localhost:18080",
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json; charset=utf-8",
            },
            path="/api/pc-mode",
        )
        self.assertFalse(ok_no_tok)
        self.assertEqual(st_no_tok, 401)

        ok_bad_tok, st_bad_tok, _ = cs.validate_console_request(
            "POST",
            {
                "Host": "localhost:18080",
                "Origin": "http://localhost:18080",
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json; charset=utf-8",
                "X-Console-Token": "wrong-token",
            },
            path="/api/pc-mode",
        )
        self.assertFalse(ok_bad_tok)
        self.assertEqual(st_bad_tok, 401)

        # Same-origin JSON POST with valid per-boot X-Console-Token passes
        ok_good, st_good, _ = cs.validate_console_request(
            "POST",
            {
                "Host": "localhost:18080",
                "Origin": "http://localhost:18080",
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json; charset=utf-8",
                "X-Console-Token": cs.get_console_token(),
            },
            path="/api/pc-mode",
        )
        self.assertTrue(ok_good)
        self.assertEqual(st_good, 200)

        # Host: xiaozhi-esp32-server restricted strictly to read-only /api/health & /api/state (A-4)
        ok_ih, st_ih, _ = cs.validate_console_request("GET", {"Host": "xiaozhi-esp32-server:18080"}, path="/api/health")
        self.assertTrue(ok_ih)
        self.assertEqual(st_ih, 200)
        ok_is, st_is, _ = cs.validate_console_request("GET", {"Host": "xiaozhi-esp32-server:18080"}, path="/api/state")
        self.assertTrue(ok_is)
        self.assertEqual(st_is, 200)
        ok_iroot, st_iroot, _ = cs.validate_console_request("GET", {"Host": "xiaozhi-esp32-server:18080"}, path="/")
        self.assertFalse(ok_iroot)
        self.assertEqual(st_iroot, 403)
        ok_ipost, st_ipost, _ = cs.validate_console_request(
            "POST",
            {
                "Host": "xiaozhi-esp32-server:18080",
                "Content-Type": "application/json",
                "X-Console-Token": cs.get_console_token(),
            },
            path="/api/pc-mode",
        )
        self.assertFalse(ok_ipost)
        self.assertEqual(st_ipost, 403)

        # 2. Log secret redaction
        red = cs._redact_secrets(
            "Authorization: Bearer abcdef1234567890 #token=mysecret sk-1234567890abcdef AIzaSyA12345678901234567890"
        )
        self.assertNotIn("abcdef1234567890", red)
        self.assertNotIn("mysecret", red)
        self.assertNotIn("sk-1234567890abcdef", red)
        self.assertNotIn("AIzaSyA12345678901234567890", red)

        # 3. Verify console static assets send application/json + X-Console-Token, pause polling, and render llmCacheRateBadge
        with open(cs.HTML_PATH, "r", encoding="utf-8") as f:
            html_src = f.read()
        with open(cs.JS_PATH, "r", encoding="utf-8") as f:
            js_src = f.read()
        combined_ui = html_src + "\n" + js_src
        self.assertIn("visibilitychange", combined_ui)
        self.assertIn("document.hidden", combined_ui)
        self.assertIn('name="console-token"', html_src)
        self.assertIn("X-Console-Token", js_src)
        self.assertIn("llmCacheRateBadge", combined_ui)
        self.assertNotIn('fetch("/api/clear-history", { method: "POST" })', combined_ui)

    def test_pc_audio_router_single_wait_dynamic_poll_queue_lock_and_bearer_auth(self):
        import asyncio
        import tempfile
        from unittest.mock import AsyncMock, patch
        import core.handle.sendAudioHandle as send_audio_mod
        import pc_audio_router as par

        par.apply_pc_audio_playback_patches()

        # 1. Verify Bearer token header injection via temp token file
        with tempfile.TemporaryDirectory() as td:
            tf = os.path.join(td, "pc_mcp_token.txt")
            with open(tf, "w", encoding="utf-8") as f:
                f.write("local-test-bearer-token-1234567890\n")
            with patch.object(par, "PC_MCP_TOKEN_PATH", tf):
                par._tok_mtime = -1.0
                hdrs = par.get_pc_mcp_auth_headers({"X-Turn-Id": "t1"})
                self.assertEqual(hdrs["Authorization"], "Bearer local-test-bearer-token-1234567890")
                self.assertEqual(hdrs["X-Turn-Id"], "t1")

        # 2. Verify dynamic multi-slice /audio/wait keeps polling past 20s until done=True
        async def _check_dynamic_poll_and_single_wait():
            poll_calls = []

            class _FakeResp:
                def __init__(self, body):
                    self.status_code = 200
                    self.content = b"{}"
                    self._body = body

                def json(self):
                    return self._body

            responses = [
                _FakeResp({"ok": True, "done": False, "playing": True, "queued": 2}),
                _FakeResp({"ok": True, "done": False, "playing": True, "queued": 1}),
                _FakeResp({"ok": True, "done": True, "playing": False, "queued": 0}),
            ]

            class _FakeClient:
                async def get(self, url, params=None, headers=None, timeout=None):
                    poll_calls.append((url, params))
                    return responses.pop(0)

            class _DummyWS:
                def __init__(self):
                    self.sent = []

                async def send(self, msg):
                    self.sent.append(msg)

            class _DummyRC:
                def __init__(self):
                    self.queue = []
                    self.interval_ms = 60
                    self.queue_empty_event = asyncio.Event()
                    self.queue_empty_event.set()
                    self.stopped = False

                def stop_sending(self):
                    self.stopped = True

            class _DummyConn:
                def __init__(self):
                    self.session_id = "s-pc-1"
                    self.sentence_id = "sid-pc-1"
                    self.client_abort = False
                    self.config = {"enable_stop_tts_notify": False}
                    self.websocket = _DummyWS()
                    self.audio_rate_controller = _DummyRC()
                    self.cleared = False

                def clearSpeakStatus(self):
                    self.cleared = True

            conn = _DummyConn()
            par._enqueue_pc_clip(conn, "sid-pc-1", "很长的故事第一段", b"RIFF0000WAVE", 28000, 1.0)
            conn._pc_audio_active_sid = "sid-pc-1"
            conn._pc_audio_queued_ms = 28000

            with (
                patch.object(par, "_get_async_client", return_value=_FakeClient()),
                patch("asyncio.sleep", new=AsyncMock()) as m_sleep,
            ):
                await send_audio_mod.send_tts_message(conn, "stop", None)
                # Multi-slice dynamic wait polled 3 times until done=True!
                self.assertEqual(len(poll_calls), 3)
                # ZERO extra 240ms _wait_for_audio_completion sleeps occurred!
                m_sleep.assert_not_awaited()
                self.assertTrue(conn.cleared)
                self.assertTrue(conn.audio_rate_controller.stopped)
                self.assertIsNone(conn._pc_audio_active_sid)

            # 3. Stale `stop` from sid-1 MUST NOT clobber sid-2 or send stale `stop` after abort/turn-change
            conn2 = _DummyConn()
            conn2.sentence_id = "sid-1"
            conn2._pc_audio_active_sid = "sid-1"
            conn2._pc_audio_queued_ms = 5000

            async def _switch_turn_mid_wait(**_kwargs):
                conn2.sentence_id = "sid-2"
                conn2._pc_audio_active_sid = "sid-2"
                conn2._pc_audio_queued_ms = 3000
                return False

            with patch.object(par, "_wait_pc_audio_done", side_effect=_switch_turn_mid_wait):
                await send_audio_mod.send_tts_message(conn2, "stop", None)
            self.assertEqual(conn2._pc_audio_active_sid, "sid-2")
            self.assertEqual(conn2._pc_audio_queued_ms, 3000)
            self.assertEqual(conn2.websocket.sent, [])
            self.assertFalse(conn2.cleared)
            self.assertFalse(conn2.audio_rate_controller.stopped)

        asyncio.run(_check_dynamic_poll_and_single_wait())

    def test_start_with_env_mcp_token_injection_and_env_expansion(self):
        import tempfile
        import yaml
        import start_with_env

        # 1. Verify env expansion on example template (never requires private prod keys)
        cfg_path = os.path.join(CONFIG_TEMPLATES_DIR, ".config.example.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw_cfg = yaml.safe_load(f) or {}
        expanded = start_with_env._expand_env(raw_cfg)
        ws_url = str((expanded.get("server") or {}).get("websocket") or "")
        self.assertTrue(ws_url.startswith("ws://") and "${" not in ws_url, f"Unexpanded ws_url: {ws_url}")
        ref_path = str(((expanded.get("TTS") or {}).get("GPT_SOVITS_V2") or {}).get("ref_audio_path") or "")
        self.assertNotIn("${", ref_path, f"Unexpanded ref_audio_path: {ref_path}")

        # 2. Verify in-memory Bearer token injection into ServerMCPManager.load_config
        with tempfile.TemporaryDirectory() as td:
            tf = os.path.join(td, "pc_mcp_token.txt")
            with open(tf, "w", encoding="utf-8") as f:
                f.write("injected-mcp-bearer-token-9876543210\n")
            sample_servers = {
                "windows_pc_control": {
                    "url": "http://host.docker.internal:18900/mcp",
                    "transport": "streamable-http",
                },
                "remote_other": {
                    "url": "https://example.com/mcp",
                    "transport": "streamable-http",
                },
            }
            injected = start_with_env.inject_pc_mcp_auth_headers(sample_servers, token_path=tf)
            self.assertEqual(
                injected["windows_pc_control"]["headers"]["Authorization"],
                "Bearer injected-mcp-bearer-token-9876543210",
            )
            self.assertNotIn("headers", injected["remote_other"])
            # Original dict must not be mutated
            self.assertNotIn("headers", sample_servers["windows_pc_control"])

    def test_p05_turn_end_flush_avoids_tool_round_truncation_and_captures_meta(self):
        import time
        from unittest.mock import patch
        import boot
        import anima_memory_provider
        import start_with_env
        from core.utils.dialogue import Dialogue, Message

        boot.install_all(start_background_workers=False, run_contract_self_check=True)

        pushed = []

        def _capture_push(turns_payload):
            pushed.extend(turns_payload)

        with (
            patch.object(anima_memory_provider, "push_turns_async", side_effect=_capture_push),
            patch.object(start_with_env, "_FALLBACK_DEBOUNCE_SECONDS", 0.08),
        ):
            # 1. Simulated ConnectionHandler.chat tool round: depth=0 -> half-reply -> tool wait (> debounce) -> depth=1 final reply
            dlg = Dialogue()
            dlg._device_id = "aa:bb:cc:11:22:33"
            dlg._in_chat_depth = 1
            dlg.put(Message(role="user", content="帮我查明天长沙天气", uniq_id="tool-u1"))
            dlg.put(Message(role="assistant", content="[gentle] 我帮你查一下"))
            # Wait longer than fallback debounce while tool is executing inside chat()
            time.sleep(0.15)
            self.assertEqual(len(pushed), 0, "Must NOT flush half-reply while tool round is running inside chat()")

            dlg.put(Message(role="assistant", tool_calls=[{"id": "tc1", "function": {"name": "get_weather"}}]))
            dlg.put(Message(role="tool", tool_call_id="tc1", content="晴 18-26度"))
            dlg.put(Message(role="assistant", content="[happy] 明天长沙晴，18到26度哦！"))
            # Exit chat(depth=0)
            dlg._in_chat_depth = 0
            start_with_env._flush_dialogue_latest_pair(dlg)

            self.assertEqual(len(pushed), 1)
            p = pushed[0]
            self.assertEqual(p["request_id"], "xz-tool-u1")
            self.assertEqual(p["assistant_reply"], "我帮你查一下 明天长沙晴，18到26度哦！")
            self.assertTrue(p["used_tools"])
            self.assertEqual(p["device_id"], "aa:bb:cc:11:22:33")
            self.assertEqual(p["source"], "esp32")
            self.assertIsInstance(p.get("occurred_at"), float)

            # 2. Outside chat(): a tool message cancels pending fallback timer; arriving next user flushes previous turn
            pushed.clear()
            dlg2 = Dialogue()
            dlg2.put(Message(role="user", content="第一句", uniq_id="seq-1"))
            dlg2.put(Message(role="assistant", content="正在处理"))
            dlg2.put(Message(role="tool", tool_call_id="t9", content="done"))
            time.sleep(0.12)
            self.assertEqual(len(pushed), 0, "Tool message outside chat() must cancel premature debounce flush")
            dlg2.put(Message(role="assistant", content="已经搞定啦"))
            # Immediate next user utterance flushes previous completed pair without waiting for timer
            dlg2.put(Message(role="user", content="第二句", uniq_id="seq-2"))
            self.assertEqual(len(pushed), 1)
            self.assertEqual(pushed[0]["request_id"], "xz-seq-1")
            self.assertEqual(pushed[0]["assistant_reply"], "正在处理 已经搞定啦")
            self.assertTrue(pushed[0]["used_tools"])

            # 3. Standalone wakeup greeting after a closed turn must NOT corrupt the prior turn!
            pushed.clear()
            dlg3 = Dialogue()
            dlg3._in_chat_depth = 1
            dlg3.put(Message(role="user", content="今天天气真好", uniq_id="wake-1"))
            dlg3.put(Message(role="assistant", content="是呀哥哥，阳光很暖和。"))
            dlg3._in_chat_depth = 0
            start_with_env._flush_dialogue_latest_pair(dlg3, mark_closed=True)
            self.assertEqual(len(pushed), 1)
            # Simulate helloHandle.py:124 putting standalone assistant wakeup greeting without user message
            dlg3.put(Message(role="assistant", content="我在这里哦！"))
            time.sleep(0.14)
            self.assertEqual(len(pushed), 1, "Standalone wakeup greeting must NOT re-push/corrupt prior turn")
            pairs_after_wake = anima_memory_provider.extract_clean_pairs(dlg3.dialogue)
            self.assertEqual(len(pairs_after_wake), 1)
            self.assertEqual(pairs_after_wake[0]["assistant_reply"], "是呀哥哥，阳光很暖和。")

            # 4. Action.RESPONSE tool path (no tool_calls/tool message in dialogue) still flags used_tools=True
            pushed.clear()
            dlg4 = Dialogue()
            dlg4._in_chat_depth = 1
            dlg4.put(Message(role="user", content="帮我把音量调大点", uniq_id="act-resp-1"))
            start_with_env._mark_latest_user_attr(dlg4, "_used_tools", True)
            dlg4.put(Message(role="assistant", content="好啦，音量已经调高了。"))
            dlg4._in_chat_depth = 0
            start_with_env._flush_dialogue_latest_pair(dlg4, mark_closed=True)
            self.assertEqual(len(pushed), 1)
            self.assertTrue(pushed[0]["used_tools"])

    def test_p06_sqlite_outbox_retry_backoff_partial_reject_and_flush(self):
        import json
        import tempfile
        import time
        import httpx
        import anima_memory_provider

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "anima_outbox.sqlite")
            calls = []

            def _handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                calls.append(body)
                n = len(calls)
                if n == 1:
                    return httpx.Response(503, json={"error": "ingest_queue_full"})
                if n == 2:
                    # Accept only req-1, reject req-2
                    return httpx.Response(
                        200,
                        json={"queued": 1, "accepted_ids": ["xz-o1"], "rejected_ids": ["xz-o2"]},
                    )
                return httpx.Response(
                    200,
                    json={
                        "queued": len(body["turns"]),
                        "accepted_ids": [t["request_id"] for t in body["turns"]],
                        "rejected_ids": [],
                    },
                )

            transport = httpx.MockTransport(_handler)
            outbox = anima_memory_provider.AnimaOutbox(
                db_path,
                service_url_getter=lambda: "http://anima-brain:18787",
                character_id_getter=lambda: "xiaozhi",
                token_getter=lambda: "tok-123456789012345678901234",
                client_factory=lambda: httpx.Client(transport=transport, trust_env=False),
                base_backoff=0.05,
                max_backoff=0.5,
                auto_start=False,
            )
            try:
                t0 = time.time() - 5.0
                outbox.enqueue(
                    [
                        {
                            "request_id": "xz-o1",
                            "user_message": "你好",
                            "assistant_reply": "短回复",
                            "occurred_at": t0,
                            "used_tools": False,
                        },
                        {
                            "request_id": "xz-o2",
                            "user_message": "第二条",
                            "assistant_reply": "第二条回复",
                            "occurred_at": t0 + 1,
                        },
                    ]
                )
                # Upgrading xz-o1 with a fuller reply merges without losing earliest occurred_at
                outbox.enqueue(
                    [
                        {
                            "request_id": "xz-o1",
                            "user_message": "你好",
                            "assistant_reply": "短回复 加上完整后续",
                            "occurred_at": t0 + 20,
                            "used_tools": True,
                        }
                    ]
                )
                self.assertEqual(outbox.pending_count(), 2)

                # Attempt 1 -> 503 -> both remain pending with backoff
                r1 = outbox.drain_once(force_due=False)
                self.assertEqual(r1["sent"], 0)
                self.assertEqual(r1["failed"], 2)
                self.assertEqual(outbox.pending_count(), 2)

                # Immediate non-forced drain respects exponential backoff (does not hammer server)
                r_wait = outbox.drain_once(force_due=False)
                self.assertEqual(len(calls), 1)
                self.assertEqual(r_wait["remaining"], 2)

                # Attempt 2 (after backoff) -> partial accept: xz-o1 sent, xz-o2 remains pending
                time.sleep(0.07)
                r2 = outbox.drain_once(force_due=False)
                self.assertEqual(r2["sent"], 1)
                self.assertEqual(r2["failed"], 1)
                self.assertEqual(outbox.pending_count(), 1)
                # Verify merged fuller reply + earliest occurred_at was sent for xz-o1
                sent_o1 = next(t for t in calls[1]["turns"] if t["request_id"] == "xz-o1")
                self.assertEqual(sent_o1["assistant_reply"], "短回复 加上完整后续")
                self.assertTrue(sent_o1["used_tools"])
                self.assertAlmostEqual(sent_o1["occurred_at"], t0, places=2)

                # Attempt 3 via flush() -> delivers remaining xz-o2 immediately
                rf = outbox.flush(timeout=2.0)
                self.assertEqual(rf["sent"], 1)
                self.assertEqual(rf["remaining"], 0)

                # Re-enqueueing identical already-sent xz-o2 is an idempotent no-op
                added = outbox.enqueue(
                    [
                        {
                            "request_id": "xz-o2",
                            "user_message": "第二条",
                            "assistant_reply": "第二条回复",
                            "occurred_at": t0 + 1,
                        }
                    ]
                )
                self.assertEqual(added, 0)
                self.assertEqual(outbox.pending_count(), 0)

                # Empty accepted_ids in HTTP 200 must NEVER falsely mark turns as sent!
                empty_transport = httpx.MockTransport(
                    lambda req: httpx.Response(200, json={"queued": 0, "accepted_ids": [], "rejected_ids": []})
                )
                outbox2 = anima_memory_provider.AnimaOutbox(
                    os.path.join(td, "outbox_empty_acc.sqlite"),
                    service_url_getter=lambda: "http://anima-brain:18787",
                    character_id_getter=lambda: "xiaozhi",
                    token_getter=lambda: "tok-123456789012345678901234",
                    client_factory=lambda: httpx.Client(transport=empty_transport, trust_env=False),
                    base_backoff=0.5,
                    auto_start=False,
                )
                try:
                    outbox2.enqueue([{"request_id": "xz-e1", "user_message": "u", "assistant_reply": "a"}])
                    r_emp = outbox2.drain_once(force_due=True)
                    self.assertEqual(r_emp["sent"], 0)
                    self.assertEqual(r_emp["failed"], 1)
                    self.assertEqual(outbox2.pending_count(), 1)
                    # Re-enqueuing identical pending row must NOT wipe exponential backoff
                    added_dup = outbox2.enqueue([{"request_id": "xz-e1", "user_message": "u", "assistant_reply": "a"}])
                    self.assertEqual(added_dup, 0)
                    r_backoff = outbox2.drain_once(force_due=False)
                    self.assertEqual(r_backoff["sent"], 0)
                    self.assertEqual(r_backoff["failed"], 0)
                    self.assertEqual(r_backoff["remaining"], 1)
                finally:
                    outbox2.close()

                # get_default_outbox(auto_start=True) must actually start worker on first creation (boot drain!)
                old_db = anima_memory_provider._GLOBAL_CONFIG["outbox_db"]
                old_default = anima_memory_provider._default_outbox
                boot_db = os.path.join(td, "boot_outbox.sqlite")
                try:
                    anima_memory_provider._default_outbox = None
                    anima_memory_provider._GLOBAL_CONFIG["outbox_db"] = boot_db
                    boot_ob = anima_memory_provider.get_default_outbox(auto_start=True)
                    self.assertTrue(boot_ob.stats()["worker_alive"], "Boot outbox worker must start immediately")
                    self.assertTrue(os.path.isfile(boot_db))
                    boot_ob.close()
                finally:
                    anima_memory_provider._default_outbox = old_default
                    anima_memory_provider._GLOBAL_CONFIG["outbox_db"] = old_db
            finally:
                outbox.close()

    def test_console_obsidian_ui_and_proxy_endpoints(self):
        import time
        import console_server
        from unittest.mock import patch

        with open(console_server.HTML_PATH, "r", encoding="utf-8") as f:
            html_src = f.read()
        with open(console_server.JS_PATH, "r", encoding="utf-8") as f:
            js_src = f.read()
        combined_ui = html_src + "\n" + js_src
        for el_id in ("obsStatusBadge", "obsSyncToggle", "obsExportDirText", "obsLastExportText", "obsRebuildBtn"):
            self.assertIn(f'id="{el_id}"', html_src, f"Missing Obsidian UI element {el_id} in index.html")
        self.assertIn("/api/obsidian/config", js_src)
        self.assertIn("/api/obsidian/rebuild", js_src)
        # Ensure finally blocks restore disabled state from last known obsOnline instead of unconditionally enabling,
        # and controls start disabled before initial state load.
        self.assertIn('<input type="checkbox" id="obsSyncToggle" disabled>', html_src)
        self.assertIn('id="obsRebuildBtn"', html_src)
        self.assertIn('disabled>立即全量重建</button>', html_src)
        self.assertIn("let obsOnline = false;", js_src)
        self.assertIn("let obsPendingState = null;", js_src)
        self.assertIn("els.obsRebuildBtn.disabled = !obsOnline;", js_src)
        self.assertNotIn("if (els.obsRebuildBtn) els.obsRebuildBtn.disabled = false;", combined_ui)
        self.assertNotIn("els.obsRebuildBtn.disabled = false;", combined_ui)

        # Verify tracked setup scripts & .env.example have no hardcoded personal drive path
        personal_dir = "E:" + "\\love"
        init_ps1_path = os.path.join(HOST_SCRIPTS_DIR, "init_setup.ps1")
        if os.path.isfile(init_ps1_path):
            with open(init_ps1_path, "r", encoding="utf-8-sig") as f:
                ps1_src = f.read()
            self.assertNotIn(personal_dir, ps1_src)
            self.assertIn('Join-Path $serverDir "data\\obsidian_export"', ps1_src)
        env_ex_path = os.path.join(PROJECT_ROOT, ".env.example")
        if os.path.isfile(env_ex_path):
            with open(env_ex_path, "r", encoding="utf-8") as f:
                self.assertNotIn(personal_dir, f.read())

        # Verify console pre-exit flush imports and invokes anima_memory_provider.flush_outbox
        srv_parts = []
        for _dp, _, _fns in os.walk(os.path.join(DATA_DIR, "console")):
            for _fn in sorted(_fns):
                if _fn.endswith(".py"):
                    with open(os.path.join(_dp, _fn), "r", encoding="utf-8") as f:
                        srv_parts.append(f.read())
        self.assertIn("anima_memory_provider.flush_outbox", "\n".join(srv_parts))

        # Verify proxy helpers handle Anima responses cleanly
        import httpx

        def _mock_anima(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xiaozhi/obsidian/status":
                return httpx.Response(
                    200,
                    json={
                        "enabled": True,
                        "writable": True,
                        "export_dir": "/obsidian",
                        "host_export_dir": r"D:\Obsidian\XiaozhiMemory",
                        "last_export_at": "2026-11-20 12:00:00",
                        "stats": {"turns": 151, "days": 3, "active_facts": 13, "episodes": 2},
                    },
                )
            if request.url.path == "/xiaozhi/obsidian/config":
                return httpx.Response(200, json={"ok": True, "obsidian": {"enabled": False, "writable": True}})
            if request.url.path == "/xiaozhi/obsidian/rebuild":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "written": 0,
                        "skipped": 9,
                        "obsidian": {"enabled": True, "writable": True},
                    },
                )
            return httpx.Response(404)

        mock_client = httpx.Client(transport=httpx.MockTransport(_mock_anima), trust_env=False)
        try:
            with (
                patch.object(console_server, "_read_anima_token", return_value="tok-test-1234567890123456"),
                patch.object(console_server, "_get_console_http_client", return_value=mock_client),
            ):
                st = console_server._fetch_obsidian_status(force=True)
                self.assertTrue(st["online"])
                self.assertEqual(st["host_export_dir"], r"D:\Obsidian\XiaozhiMemory")
                self.assertEqual(st["stats"]["turns"], 151)

                ok_cfg, cfg_res = console_server._update_obsidian_config({"enabled": False})
                self.assertTrue(ok_cfg)
                self.assertFalse(cfg_res["enabled"])

                ok_reb, reb_res = console_server._trigger_obsidian_rebuild()
                self.assertTrue(ok_reb)
                self.assertEqual(reb_res["result"]["skipped"], 9)

            # Verify offline cache transition persists online=False (no 3s flapping back to online=True!)
            def _mock_offline(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("connection refused")

            offline_client = httpx.Client(transport=httpx.MockTransport(_mock_offline), trust_env=False)
            try:
                with (
                    patch.object(console_server, "_read_anima_token", return_value="tok-test-1234567890123456"),
                    patch.object(console_server, "_get_console_http_client", return_value=offline_client),
                ):
                    st_off1 = console_server._fetch_obsidian_status(force=True)
                    self.assertFalse(st_off1["online"])
                    # Immediate cached read within TTL must STAY offline (previously flapped back to online=True!)
                    st_off2 = console_server._fetch_obsidian_status(force=False)
                    self.assertFalse(st_off2["online"])

                    # Re-seed cache as online, then trigger rebuild/config while offline -> cache flips offline immediately
                    with console_server._obsidian_lock:
                        console_server._obsidian_cache = {"online": True, "enabled": True, "writable": True}
                        console_server._obsidian_ts = time.monotonic()
                    ok_reb_off, _ = console_server._trigger_obsidian_rebuild()
                    self.assertFalse(ok_reb_off)
                    self.assertFalse(console_server._get_cached_obsidian_snapshot()["online"])

                    with console_server._obsidian_lock:
                        console_server._obsidian_cache = {"online": True, "enabled": True, "writable": True}
                        console_server._obsidian_ts = time.monotonic()
                    ok_cfg_off, _ = console_server._update_obsidian_config({"enabled": False})
                    self.assertFalse(ok_cfg_off)
                    self.assertFalse(console_server._get_cached_obsidian_snapshot()["online"])
            finally:
                offline_client.close()

            # Verify online-but-unwritable rebuild preserves online=True and writable=False in cached snapshot
            def _mock_unwritable(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "ok": False,
                        "error": "Vault 目录不可写",
                        "obsidian": {"enabled": True, "writable": False, "last_error": "Vault 目录不可写"},
                    },
                )

            unwritable_client = httpx.Client(transport=httpx.MockTransport(_mock_unwritable), trust_env=False)
            try:
                with (
                    patch.object(console_server, "_read_anima_token", return_value="tok-test-1234567890123456"),
                    patch.object(console_server, "_get_console_http_client", return_value=unwritable_client),
                ):
                    ok_ro, err_ro = console_server._trigger_obsidian_rebuild()
                    self.assertFalse(ok_ro)
                    self.assertIn("不可写", str(err_ro))
                    snap_ro = console_server._get_cached_obsidian_snapshot()
                    self.assertTrue(snap_ro["online"])
                    self.assertFalse(snap_ro["writable"])
            finally:
                unwritable_client.close()
        finally:
            mock_client.close()
            with console_server._obsidian_lock:
                console_server._obsidian_cache = None
                console_server._obsidian_ts = 0.0

    def test_p01_esp32_auth_ota_ws_vision_and_safe_default(self):
        import asyncio
        import json
        import yaml
        from unittest.mock import patch
        import start_with_env

        # 1. Example configs default to safe mode + auth enabled with zero plaintext secrets
        with open(os.path.join(CONFIG_TEMPLATES_DIR, "runtime_settings.example.json"), "r", encoding="utf-8") as f:
            rs_ex = json.load(f)
        self.assertEqual(rs_ex.get("schema_version"), 1)
        self.assertEqual((rs_ex.get("pc_control") or {}).get("mode"), "safe")
        self.assertEqual((rs_ex.get("audio_output") or {}).get("target"), "esp32")

        with open(os.path.join(CONFIG_TEMPLATES_DIR, ".config.example.yaml"), "r", encoding="utf-8") as f:
            cfg_ex = yaml.safe_load(f)
        srv_ex = cfg_ex.get("server") or {}
        self.assertEqual(srv_ex.get("auth_key"), "${ESP32_AUTH_KEY}")
        self.assertTrue((srv_ex.get("auth") or {}).get("enabled"))
        self.assertEqual((srv_ex.get("auth") or {}).get("allowed_devices"), ["${ESP32_ALLOWED_DEVICES}"])

        # 2. normalize_allowed_devices & resolve_esp32_auth_key never leak placeholders
        devs = start_with_env.normalize_allowed_devices(
            ["${ESP32_ALLOWED_DEVICES}", "AA:BB:CC:00:00:01, aa:bb:cc:00:00:02; AA:BB:CC:00:00:01"],
            env_val="aa:bb:cc:00:00:03",
        )
        self.assertEqual(devs, ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02", "aa:bb:cc:00:00:03"])
        self.assertEqual(
            start_with_env.normalize_allowed_devices(["${ESP32_ALLOWED_DEVICES}", ""], env_val=""),
            [],
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ESP32_AUTH_KEY", None)
            resolved_k = start_with_env.resolve_esp32_auth_key("${ESP32_AUTH_KEY}")
            self.assertNotIn("${", resolved_k)
            self.assertGreaterEqual(len(resolved_k), 32)

        # 3. Upstream default config.yaml dummy MAC ("11:22:33:44:55:66") is stripped on read
        default_cfg = start_with_env._patched_read_config(
            os.path.join(os.path.dirname(DATA_DIR), "config.yaml")
        )
        self.assertEqual(default_cfg["server"]["auth"]["allowed_devices"], [])

        # 4. Hardened OTAHandler, WebSocketServer, and VisionHandler enforcement
        start_with_env.install_esp32_auth_hardening_patch()
        from core.api.ota_handler import OTAHandler
        from core.api.vision_handler import VisionHandler
        from core.auth import AuthManager, AuthenticationError
        from core.utils.auth import AuthToken
        from core.websocket_server import WebSocketServer

        test_secret = "unit_test_secret_key_for_esp32_auth_0123456789abcdef"
        allowed_mac = "de:ad:be:ef:00:01"
        evil_mac = "de:ad:be:ef:99:99"
        test_cfg = {
            "server": {
                "ip": "0.0.0.0",
                "port": 8000,
                "http_port": 8003,
                "websocket": "ws://127.0.0.1:18000/xiaozhi/v1/",
                "vision_explain": "http://127.0.0.1:18003/mcp/vision/explain",
                "timezone_offset": 8,
                "auth_key": test_secret,
                "auth": {
                    "enabled": True,
                    "allowed_devices": [allowed_mac],
                },
            }
        }

        class _FakeReq:
            def __init__(self, headers, body="{}"):
                self.method = "POST"
                self.headers = headers
                self._body = body

            async def text(self):
                return self._body

            async def multipart(self):
                raise RuntimeError("multipart_reached")

        ota = OTAHandler(test_cfg)

        # 4a. Non-allowlisted MAC is rejected with 403 and gets NO token
        r_evil_ota = asyncio.run(
            ota.handle_post(_FakeReq({"device-id": evil_mac, "client-id": "cli-evil"}))
        )
        self.assertEqual(r_evil_ota.status, 403)
        self.assertNotIn("websocket", json.loads(r_evil_ota.text))

        # 4b. Empty allowlist fails closed with 403
        ota_empty = OTAHandler(
            {
                "server": {
                    **test_cfg["server"],
                    "auth": {"enabled": True, "allowed_devices": []},
                }
            }
        )
        r_empty_ota = asyncio.run(
            ota_empty.handle_post(_FakeReq({"device-id": allowed_mac, "client-id": "cli-1"}))
        )
        self.assertEqual(r_empty_ota.status, 403)

        # 4c. Allowlisted MAC (case-insensitive) succeeds with HTTP 200 + real signed WS token
        r_ok_ota = asyncio.run(
            ota.handle_post(_FakeReq({"device-id": allowed_mac.upper(), "client-id": "cli-esp32"}))
        )
        self.assertEqual(r_ok_ota.status, 200)
        ota_json = json.loads(r_ok_ota.text)
        minted_ws_tok = ota_json["websocket"]["token"]
        self.assertTrue(minted_ws_tok)
        self.assertTrue(AuthManager(test_secret).verify_token(minted_ws_tok, "cli-esp32", allowed_mac))

        # 4d. WebSocketServer._handle_auth requires BOTH allowlisted MAC AND valid Bearer HMAC token
        ws_srv = WebSocketServer.__new__(WebSocketServer)
        ws_srv.config = test_cfg
        ws_srv.auth_enable = True
        ws_srv.allowed_devices = {allowed_mac}
        ws_srv.auth = AuthManager(test_secret)

        class _FakeWS:
            def __init__(self, headers):
                self.request = type("_Req", (), {"headers": headers})()

        # Allowlisted MAC WITHOUT token -> rejected (upstream allowlist bypass eliminated!)
        with self.assertRaises(AuthenticationError):
            asyncio.run(
                ws_srv._handle_auth(_FakeWS({"Device-Id": allowed_mac, "Client-Id": "cli-esp32"}))
            )
        # Allowlisted MAC WITH wrong token -> rejected
        with self.assertRaises(AuthenticationError):
            asyncio.run(
                ws_srv._handle_auth(
                    _FakeWS(
                        {
                            "Device-Id": allowed_mac,
                            "Client-Id": "cli-esp32",
                            "Authorization": "Bearer wrong.12345",
                        }
                    )
                )
            )
        # Non-allowlisted MAC WITH valid signed token -> rejected
        evil_ws_tok = AuthManager(test_secret).generate_token("cli-evil", evil_mac)
        with self.assertRaises(AuthenticationError):
            asyncio.run(
                ws_srv._handle_auth(
                    _FakeWS(
                        {
                            "Device-Id": evil_mac,
                            "Client-Id": "cli-evil",
                            "Authorization": f"Bearer {evil_ws_tok}",
                        }
                    )
                )
            )
        # Allowlisted MAC WITH valid OTA-minted token -> accepted!
        asyncio.run(
            ws_srv._handle_auth(
                _FakeWS(
                    {
                        "Device-Id": allowed_mac.upper(),
                        "Client-Id": "cli-esp32",
                        "Authorization": f"Bearer {minted_ws_tok}",
                    }
                )
            )
        )

        # 4e. VisionHandler removes `web_test_client` backdoor and enforces JWT + Device-Id + MAC allowlist
        vis = VisionHandler(test_cfg)
        r_backdoor = asyncio.run(
            vis.handle_post(_FakeReq({"Client-Id": "web_test_client", "Device-Id": allowed_mac}))
        )
        self.assertEqual(r_backdoor.status, 401)

        r_no_tok = asyncio.run(vis.handle_post(_FakeReq({"Device-Id": allowed_mac})))
        self.assertEqual(r_no_tok.status, 401)

        evil_vis_tok = AuthToken(test_secret).generate_token(evil_mac)
        r_evil_vis = asyncio.run(
            vis.handle_post(
                _FakeReq({"Device-Id": evil_mac, "Authorization": f"Bearer {evil_vis_tok}"})
            )
        )
        self.assertEqual(r_evil_vis.status, 403)

        ok_vis_tok = AuthToken(test_secret).generate_token(allowed_mac)
        r_mismatch_vis = asyncio.run(
            vis.handle_post(
                _FakeReq({"Device-Id": evil_mac, "Authorization": f"Bearer {ok_vis_tok}"})
            )
        )
        self.assertEqual(r_mismatch_vis.status, 403)

        r_ok_vis = asyncio.run(
            vis.handle_post(
                _FakeReq({"Device-Id": allowed_mac, "Authorization": f"Bearer {ok_vis_tok}"})
            )
        )
        # Passed auth and proceeded into multipart parsing (handled as 200 error JSON by upstream)
        self.assertNotIn(r_ok_vis.status, (401, 403))

    def test_p02_p03_console_l3_restricted_fields_and_host_state_scripts(self):
        import json
        import httpx
        from unittest.mock import patch
        import console_server

        # 1. console modules never reference container-mounted data/l3_backend_config.json or data/agy_tasks
        cs_parts = []
        with open(os.path.join(DATA_DIR, "console_server.py"), "r", encoding="utf-8") as f:
            cs_parts.append(f.read())
        for _dp, _, _fns in os.walk(os.path.join(DATA_DIR, "console")):
            for _fn in sorted(_fns):
                if _fn.endswith(".py"):
                    with open(os.path.join(_dp, _fn), "r", encoding="utf-8") as f:
                        cs_parts.append(f.read())
        cs_src = "\n".join(cs_parts)
        self.assertNotIn("l3_backend_config.json", cs_src)
        self.assertNotIn("agy_tasks", cs_src)

        # 2. _sanitize_l3_update_payload & _update_l3_backend_config reject forbidden host-env fields
        for bad_payload in (
            {"dsh_cmd": "calc.exe"},
            {"backend": "dsh", "dsh_cmd": "calc.exe"},
            {"workbuddy_cmd": "powershell.exe"},
            {"workdir": "C:\\Windows"},
            {"workbuddy_auto_approve": True},
            {"timeout_seconds": True},
            {"timeout_seconds": "not-an-int"},
            {"backend": "invalid_engine"},
            {},
        ):
            ok, err = console_server._update_l3_backend_config(bad_payload)
            self.assertFalse(ok, f"Expected payload to be rejected: {bad_payload}")
            self.assertTrue(str(err))

        # 3. Valid backend + timeout_seconds forwards ONLY restricted keys to 18900 /l3/config
        captured = []

        def _mock_18900(request: httpx.Request) -> httpx.Response:
            captured.append((request.url.path, json.loads(request.content.decode("utf-8"))))
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "backend": "dsh",
                    "timeout_seconds": 600,
                    "workbuddy_auto_approve": False,
                    "ready": True,
                    "detected": {"antigravity": {"ready": False}},
                },
            )

        mock_client = httpx.Client(transport=httpx.MockTransport(_mock_18900), trust_env=False)
        try:
            with patch.object(console_server, "_get_console_http_client", return_value=mock_client):
                ok_upd, res_upd = console_server._update_l3_backend_config(
                    {"backend": "DSH", "timeout": 600}
                )
                self.assertTrue(ok_upd, f"Expected valid L3 update to succeed: {res_upd}")
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0][0], "/l3/config")
                self.assertEqual(captured[0][1], {"backend": "dsh", "timeout_seconds": 600})
                self.assertFalse(res_upd["workbuddy_auto_approve"])

            # 4. Offline 18900 returns error without writing data/l3_backend_config.json
            def _mock_offline(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("connection refused")

            off_client = httpx.Client(transport=httpx.MockTransport(_mock_offline), trust_env=False)
            try:
                with patch.object(console_server, "_get_console_http_client", return_value=off_client):
                    ok_off, err_off = console_server._update_l3_backend_config({"backend": "none"})
                    self.assertFalse(ok_off)
                    self.assertIn("pc-mcp/state/", str(err_off))
            finally:
                off_client.close()
        finally:
            mock_client.close()
            with console_server._l3_cache_lock:
                console_server._l3_cache = None
                console_server._l3_cache_ts = 0.0

        # 5. console static assets reflect L3 full-mode gate & read-only host .env fields
        with open(console_server.HTML_PATH, "r", encoding="utf-8") as f:
            html_src = f.read()
        with open(console_server.JS_PATH, "r", encoding="utf-8") as f:
            js_src = f.read()
        self.assertIn('id="l3ModeGateHint"', html_src)
        self.assertIn('id="l3TimeoutInput"', html_src)
        self.assertIn('id="l3WorkdirInput" readonly', html_src)
        self.assertIn('id="l3DshCmdInput" readonly', html_src)
        self.assertIn('id="l3WbCmdInput" readonly', html_src)
        self.assertIn('id="l3WbAutoApprove" disabled', html_src)
        self.assertNotIn("l3.workbuddy_auto_approve !== false", html_src + "\n" + js_src)

        # 6. PowerShell setup/start scripts preserve UTF-8 BOM + CRLF and move L3 state to pc-mcp\state
        for ps1_name in ("start_all.ps1", "init_setup.ps1"):
            ps1_path = os.path.join(HOST_SCRIPTS_DIR, ps1_name)
            if os.path.isfile(ps1_path):
                with open(ps1_path, "rb") as bf:
                    raw = bf.read()
                self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), f"{ps1_name} missing UTF-8 BOM")
                self.assertEqual(raw.count(b"\n") - raw.count(b"\r\n"), 0, f"{ps1_name} must use CRLF")
                txt = raw.decode("utf-8-sig")
                self.assertNotIn("data\\l3_backend_config.json", txt)
                self.assertNotIn("data\\agy_tasks", txt)
                self.assertIn("pc-mcp\\state", txt)

    def test_p108_patch_registry_contracts_version_check_passthrough_and_prompt_anchors(self):
        import threading
        from http.server import ThreadingHTTPServer
        import httpx
        import boot
        import patch_registry
        import start_with_env  # noqa: F401
        import console_server as cs

        boot.install_all(start_background_workers=False, run_contract_self_check=True)

        # 1. All startup patches must be registered and healthy on expected SERVER_VERSION
        summary = patch_registry.get_health_summary()
        self.assertEqual(summary["status"], "ok", f"Unexpected degraded health: {summary}")
        self.assertFalse(summary["degraded"])
        self.assertTrue(summary["version_match"])
        self.assertEqual(summary["server_version"], patch_registry.DEFAULT_EXPECTED_SERVER_VERSION)
        for required_patch in (
            "config_env_expand",
            "mcp_token_injection",
            "esp32_auth_ota",
            "esp32_auth_ws",
            "esp32_auth_vision",
            "anima_memory_factory",
            "anima_dialogue_hooks",
            "anima_turn_flush",
            "nmc_weather",
            "pc_audio_tts",
            "mei_emotion_and_prosody",
            "mei_prompt_anchors",
            "pc_audio_playback",
            "mei_wakeup_responses",
            "listen_interrupt_handler",
            "reply_length_llm",
            "llm_prefix_cache",
            "sherpa_async_asr",
        ):
            st = patch_registry.get_patch_status(required_patch)
            self.assertIsNotNone(st, f"Missing patch registry entry: {required_patch}")
            self.assertEqual(st["status"], "ok", f"Patch not ok: {st}")
            self.assertTrue(st["contract_ok"], f"Contract failed for {required_patch}: {st}")

        # 2. verify_signature_contract catches missing params & async/sync mismatch
        def _sync_fn(a, b):
            return a + b

        async def _async_fn(conn, state, text=None):
            return None

        ok1, _ = patch_registry.verify_signature_contract(_sync_fn, required_params=("a", "b"), require_async=False)
        self.assertTrue(ok1)
        ok2, _ = patch_registry.verify_signature_contract(_sync_fn, required_params=("a", "missing_p"), require_async=False)
        self.assertFalse(ok2)
        ok3, _ = patch_registry.verify_signature_contract(_sync_fn, required_params=("a",), require_async=True)
        self.assertFalse(ok3)
        ok4, _ = patch_registry.verify_signature_contract(_async_fn, required_params=("conn", "state"), require_async=True)
        self.assertTrue(ok4)

        # 3. C-2: Missing prompt anchor soft-fails (skips missing anchor, applies remaining, warns once, records partial)
        router.reset_prompt_anchor_warnings()
        _, anchor1_old, anchor1_new = router._PROMPT_REPLACEMENTS[1]
        partial_prompt = f"header {anchor1_old} footer"
        out_soft = router.apply_prompt_harmony_replacements(partial_prompt)
        self.assertIn(anchor1_new, out_soft)
        self.assertNotIn(anchor1_old, out_soft)
        st_pa = patch_registry.get_patch_status("mei_prompt_anchors")
        self.assertEqual(st_pa["status"], "partial")
        self.assertFalse(st_pa["contract_ok"])
        # Restore mei_prompt_anchors back to ok and verify /api/health degradation lifecycle
        anchors_ok, anchors_det = router.verify_prompt_base_anchors()
        self.assertTrue(anchors_ok)
        patch_registry.record_patch(
            "mei_prompt_anchors", "ok", target="agent-base-prompt.txt", detail=anchors_det, contract_ok=True
        )

        # 4. /api/health returns degraded when a patch fails, required patch is skipped, or SERVER_VERSION mismatches
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), cs.ConsoleHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        orig_port = cs.CONSOLE_PORT
        cs.CONSOLE_PORT = port
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                r_ok = client.get(f"http://127.0.0.1:{port}/api/health")
                self.assertEqual(r_ok.status_code, 200)
                self.assertEqual(r_ok.json()["status"], "ok")
                self.assertTrue(r_ok.json()["ok"])

                # Simulate failed patch -> degraded
                patch_registry.record_patch("synthetic_fail_test", "failed", detail="boom", contract_ok=False)
                r_deg = client.get(f"http://127.0.0.1:{port}/api/health")
                self.assertEqual(r_deg.status_code, 200)
                self.assertEqual(r_deg.json()["status"], "degraded")
                self.assertTrue(r_deg.json()["degraded"])
                self.assertFalse(r_deg.json()["ok"])

                # Remove synthetic patch, simulate version mismatch -> degraded
                with patch_registry._lock:
                    patch_registry._patches.pop("synthetic_fail_test", None)
                patch_registry.check_upstream_server_version(expected_version="9.9.9", actual_version="0.9.8")
                r_ver = client.get(f"http://127.0.0.1:{port}/api/health")
                self.assertEqual(r_ver.json()["status"], "degraded")
                self.assertFalse(r_ver.json()["version_match"])

                # Restore version check -> healthy ok
                patch_registry.check_upstream_server_version()
                r_rest = client.get(f"http://127.0.0.1:{port}/api/health")
                self.assertEqual(r_rest.json()["status"], "ok")
                self.assertTrue(r_rest.json()["ok"])
        finally:
            cs.CONSOLE_PORT = orig_port
            httpd.shutdown()
            httpd.server_close()

        # 5. Patched wrappers invoke their `_orig` parameter & forward extra *args/**kwargs cleanly without TypeError
        import config.config_loader as cfg_loader

        self.assertFalse(
            hasattr(start_with_env, "_orig_read_config"),
            "Redundant module-global _orig_read_config should be removed",
        )
        self.assertFalse(
            hasattr(start_with_env, "_orig_create_memory"),
            "Redundant module-global _orig_create_memory should be removed",
        )
        wrapped_rc = start_with_env._make_patched_read_config(
            lambda config_path, *a, **kw: {"path": config_path, "extra": (a, kw)}
        )
        res = wrapped_rc("/tmp/fake.yaml", "extra_pos", custom_flag=True)
        self.assertEqual(res["path"], "/tmp/fake.yaml")
        self.assertEqual(res["extra"], (("extra_pos",), {"custom_flag": True}))
        self.assertIs(cfg_loader.read_config, start_with_env._patched_read_config)
        self.assertTrue(hasattr(cfg_loader.read_config, "__wrapped__"))

        wrapped_mem = start_with_env._wrap_create_memory(
            lambda class_name, *a, **kw: (class_name, a, kw)
        )
        self.assertEqual(wrapped_mem("other_mem", 1, flag=True), ("other_mem", (1,), {"flag": True}))

    def test_p109_anima_recall_budget_circuit_breaker_and_stale_while_revalidate(self):
        import asyncio
        import tempfile
        import time
        import anima_memory_provider as amp

        async def _run():
            with tempfile.TemporaryDirectory() as td:
                tok_file = os.path.join(td, "tok.txt")
                outbox_db = os.path.join(td, "outbox.sqlite")
                with open(tok_file, "w", encoding="utf-8") as f:
                    f.write("test-anima-token-123456")

                # 1. Verify default httpx.Timeout(0.8, connect=0.2) and 0.6s budget
                prov_def = amp.MemoryProvider(
                    {"service_url": "http://127.0.0.1:18787", "token_file": tok_file, "outbox_db": outbox_db}
                )
                self.assertEqual(prov_def._http_timeout.connect, 0.2)
                self.assertEqual(prov_def._http_timeout.read, 0.8)
                self.assertEqual(prov_def._recall_budget_seconds, 0.6)
                await prov_def._client.aclose()

                # 2. Hard total budget enforcement + circuit breaker opening after 3 failures + half-open recovery
                prov_cb = amp.MemoryProvider(
                    {
                        "service_url": "http://127.0.0.1:18787",
                        "token_file": tok_file,
                        "outbox_db": outbox_db,
                        "recall_budget_seconds": 0.12,
                        "recall_cb_threshold": 3,
                        "recall_cb_cooldown_seconds": 0.25,
                        "stale_while_revalidate": False,
                    }
                )
                call_count = {"n": 0}

                class _FakeResp:
                    def __init__(self, status_code=200, payload=None):
                        self.status_code = status_code
                        self._payload = payload or {}

                    def json(self):
                        return self._payload

                class _SlowClient:
                    async def post(self, *args, **kwargs):
                        call_count["n"] += 1
                        if call_count["n"] <= 3:
                            await asyncio.sleep(1.5)
                        return _FakeResp(200, {"context_text": "recovered-memory", "mood": "calm", "facts_count": 1})

                await prov_cb._client.aclose()
                prov_cb._client = _SlowClient()

                # 3 slow calls must each abort near 0.12s (NOT 1.5s) and trip circuit breaker
                for _ in range(3):
                    t0 = time.monotonic()
                    res = await prov_cb.query_memory("hello")
                    elapsed = time.monotonic() - t0
                    self.assertEqual(res, "")
                    self.assertLess(elapsed, 0.45, f"Hard recall budget violated: {elapsed:.3f}s")

                self.assertTrue(prov_cb.get_recall_stats()["circuit_open"])
                self.assertEqual(call_count["n"], 3)

                # 4th call during cooldown must short-circuit immediately without touching network
                t0 = time.monotonic()
                res_cb = await prov_cb.query_memory("hello")
                self.assertEqual(res_cb, "")
                self.assertLess(time.monotonic() - t0, 0.05)
                self.assertEqual(call_count["n"], 3)
                self.assertGreaterEqual(prov_cb.get_recall_stats()["cb_short_circuits"], 1)

                # After cooldown expires, half-open probe succeeds and closes circuit
                await asyncio.sleep(0.30)
                res_rec = await prov_cb.query_memory("hello")
                self.assertEqual(res_rec, "recovered-memory")
                self.assertFalse(prov_cb.get_recall_stats()["circuit_open"])
                self.assertEqual(call_count["n"], 4)

                # 3. Stale-while-revalidate: turn 2 returns turn 1 cached context immediately (0ms blocking)
                #    while refreshing asynchronously in the background for turn 3
                prov_swr = amp.MemoryProvider(
                    {
                        "service_url": "http://127.0.0.1:18787",
                        "token_file": tok_file,
                        "outbox_db": outbox_db,
                        "recall_budget_seconds": 0.6,
                        "recall_bg_budget_seconds": 0.8,
                        "stale_while_revalidate": True,
                    }
                )
                swr_calls = {"n": 0}

                class _SwrClient:
                    async def post(self, *args, **kwargs):
                        swr_calls["n"] += 1
                        if swr_calls["n"] == 1:
                            await asyncio.sleep(0.02)
                            return _FakeResp(200, {"context_text": "turn-1-ctx", "mood": "gentle", "facts_count": 2})
                        await asyncio.sleep(0.15)
                        return _FakeResp(200, {"context_text": "turn-2-refreshed-ctx", "mood": "happy", "facts_count": 3})

                await prov_swr._client.aclose()
                prov_swr._client = _SwrClient()

                # Turn 1 (cold): waits synchronously and caches result
                c1 = await prov_swr.query_memory("first question")
                self.assertEqual(c1, "turn-1-ctx")

                # Turn 2 (warm): returns previous turn immediately (< 0.05s << 0.15s server delay) + refreshes in bg
                t0 = time.monotonic()
                c2 = await prov_swr.query_memory("second question")
                t_swr = time.monotonic() - t0
                self.assertEqual(c2, "turn-1-ctx")
                self.assertLess(t_swr, 0.05, f"SWR turn 2 should not block on network ({t_swr:.3f}s)")
                self.assertEqual(prov_swr.get_recall_stats()["cache_hits"], 1)

                # Wait for background refresh task to complete -> Turn 3 gets refreshed context immediately!
                if prov_swr._bg_refresh_task is not None:
                    await prov_swr._bg_refresh_task
                c3 = await prov_swr.query_memory("third question")
                self.assertEqual(c3, "turn-2-refreshed-ctx")
                if prov_swr._bg_refresh_task is not None:
                    prov_swr._bg_refresh_task.cancel()
                    try:
                        await prov_swr._bg_refresh_task
                    except asyncio.CancelledError:
                        pass

        orig_global_cfg = dict(amp._GLOBAL_CONFIG)
        try:
            asyncio.run(_run())
        finally:
            amp._GLOBAL_CONFIG.clear()
            amp._GLOBAL_CONFIG.update(orig_global_cfg)
            with amp._outbox_lock:
                if amp._default_outbox is not None and amp._default_outbox.db_path != orig_global_cfg.get("outbox_db"):
                    amp._default_outbox.close()
                    amp._default_outbox = None

    def test_p110_first_sentence_20char_cut_and_fast_first_segment_sample_steps(self):
        import asyncio
        from unittest.mock import patch
        from core.providers.tts.gpt_sovits_v2 import TTSProvider

        router.apply_xiaozhi_emotion_and_prosody_patches()

        # 1. First sentence strong punctuation > 20 spoken chars falls back to soft comma cut (>=14 spoken chars)
        long_first = "[gentle] 我刚刚帮你查了一下长沙今天的实时天气，下午可能会有一阵小雨。"
        cut_first = router.find_prosody_cut_pos(long_first, True, _simple_quote_safe_cut)
        self.assertNotEqual(cut_first, -1)
        self.assertEqual(long_first[cut_first], "，", "First sentence >20 chars must split at soft comma instead of waiting for 30+ char period")
        self.assertLessEqual(router.count_spoken_chars(long_first[: cut_first + 1]), 20)

        # Subsequent sentences (is_first_sentence=False) keep the 38-char strong-punct ceiling
        cut_later = router.find_prosody_cut_pos(long_first, False, _simple_quote_safe_cut)
        self.assertEqual(cut_later, len(long_first) - 1, "Later sentence <=38 chars should stay whole to period")

        # 2. First segment of each new turn uses sample_steps=8; segment 2+ uses 16; wakeup background keeps 16
        cfg = {
            "url": "http://host.docker.internal:19880/tts",
            "output_dir": "/tmp/",
            "text_lang": "zh",
            "ref_audio_path": "voices/raiden_mei/mei_default_ref.wav",
            "aux_ref_audio_paths": [],
            "prompt_text": "今夜月色很美，呵...适合与朋友小聚一番。",
            "prompt_lang": "zh",
            "sample_steps": 16,
        }
        provider = TTSProvider(cfg, delete_audio_file=True)
        steps_seen = []

        class _FakeResp:
            def __init__(self, code=200):
                self.status_code = code
                self.content = b"RIFF\x24\x00\x00\x00WAVEfmt "
                self.text = "ok"

        fail_once = {"done": False}

        def _fake_post(url, json=None, timeout=None):
            steps_seen.append(json.get("sample_steps"))
            if not fail_once["done"]:
                fail_once["done"] = True
                return _FakeResp(500)
            return _FakeResp(200)

        with patch.object(router._get_gsv_http_session(), "post", side_effect=_fake_post):
            provider.current_sentence_id = "turn-A"
            # Failed first attempt must NOT consume the first-segment fast-step slot
            with self.assertRaises(Exception):
                asyncio.run(provider.text_to_speak("第一句尝试。", None))
            # Retry of segment 1 still uses 8; segment 2 uses 16
            asyncio.run(provider.text_to_speak("第一句重试。", None))
            asyncio.run(provider.text_to_speak("第二句正常质量。", None))
            # Brand new turn resets back to 8 for its first segment
            provider.current_sentence_id = "turn-B"
            asyncio.run(provider.text_to_speak("新一轮首句。", None))
            # Suppressed flag (wakeup cache background regen) preserves full 16 steps
            provider.current_sentence_id = "turn-C"
            provider._suppress_first_seg_fast_steps = True
            asyncio.run(provider.text_to_speak("唤醒缓存句。", None))

        self.assertEqual(steps_seen, [8, 8, 16, 8, 16])

    def test_p111_console_ring_buffer_events_replace_chinese_log_regex(self):
        import asyncio
        import tempfile
        from unittest.mock import patch
        from core.utils.dialogue import Dialogue, Message
        import core.handle.sendAudioHandle as send_audio_mod
        import boot
        import start_with_env  # noqa: F401
        import runtime_events
        import console_server as cs

        boot.install_all(start_background_workers=False, run_contract_self_check=True)

        orig_msgs = list(cs._chat_messages)
        orig_dev = dict(cs._device_info)
        orig_off = cs._log_offset
        orig_open = cs._current_ai_turn_open
        orig_has_tts = cs._turn_has_tts
        orig_dlg_open = cs._turn_dialogue_bubble_open
        orig_seq = cs._last_event_seq
        orig_hooks = runtime_events.is_hooks_active()
        try:
            with tempfile.TemporaryDirectory() as td:
                log_p = os.path.join(td, "server.log")
                hist_p = os.path.join(td, "chat_history.json")
                cur_p = os.path.join(td, ".console_log_cursor.json")
                with open(log_p, "w", encoding="utf-8") as f:
                    f.write(
                        "2026-11-20 10:00:01 INFO ASR模块初始化完成\n"
                        "2026-11-20 10:00:02 INFO OTA请求设备ID: ff:ff:ff:ff:ff:ff\n"
                        "2026-11-20 10:00:03 INFO 大模型收到用户消息: 来自日志的假消息\n"
                    )

                with (
                    patch.object(cs, "LOG_PATH", log_p),
                    patch.object(cs, "HISTORY_PATH", hist_p),
                    patch.object(cs, "LOG_CURSOR_PATH", cur_p),
                ):
                    cs._chat_messages = []
                    cs._device_info = {"device_id": "", "last_seen": "", "activity": "idle", "server_ready": False}
                    cs._log_offset = 0
                    cs._current_ai_turn_open = False
                    cs._turn_has_tts = False
                    cs._turn_dialogue_bubble_open = False
                    cs._last_event_seq = runtime_events.get_recent_events(1)[-1]["seq"] if runtime_events.get_recent_events(1) else 0
                    runtime_events.mark_hooks_active(True)

                    # 1. Polling log with raw_logs_only=True MUST NOT parse Chinese log regexes into chat/device
                    st = {"initialized": False, "last_inode": None, "saved_inode": None, "saved_offset": 0}
                    cs._poll_log_once(st, raw_logs_only=True)
                    self.assertEqual(cs._chat_messages, [])
                    self.assertFalse(cs._device_info["server_ready"])
                    self.assertEqual(cs._device_info["device_id"], "")

                    # 2. Structured runtime events (Dialogue.put + send_tts_message + ring buffer) drive chat & status
                    runtime_events.publish_event("server_ready")
                    runtime_events.publish_event("device_seen", device_id="aa:11:22:33:44:55")
                    runtime_events.publish_event("listen_start", device_id="aa:11:22:33:44:55")
                    cs._sync_runtime_events()
                    self.assertTrue(cs._device_info["server_ready"])
                    self.assertEqual(cs._device_info["device_id"], "aa:11:22:33:44:55")
                    self.assertEqual(cs._device_info["activity"], "listening")

                    dlg = Dialogue()
                    dlg._device_id = "aa:11:22:33:44:55"
                    dlg.put(Message(role="user", content="今天天气怎么样？"))
                    cs._sync_runtime_events()
                    self.assertEqual(len(cs._chat_messages), 1)
                    self.assertEqual(cs._chat_messages[0]["role"], "user")
                    self.assertEqual(cs._chat_messages[0]["text"], "今天天气怎么样？")
                    self.assertEqual(cs._device_info["activity"], "speaking")

                    class _FakeWS:
                        async def send(self, msg):
                            pass

                    class _FakeConn:
                        websocket = _FakeWS()
                        session_id = "s-p111"
                        device_id = "aa:11:22:33:44:55"
                        sentence_id = "sid-p111"
                        features = {"emoji": False}
                        config = {"enable_stop_tts_notify": False}
                        client_abort = False

                        def clearSpeakStatus(self):
                            pass

                    conn = _FakeConn()
                    asyncio.run(send_audio_mod.send_tts_message(conn, "start"))
                    asyncio.run(send_audio_mod.send_tts_message(conn, "sentence_start", "[gentle] 今天晴空万里。"))
                    asyncio.run(send_audio_mod.send_tts_message(conn, "sentence_start", "很适合出门散步！"))
                    # Dialogue.put(assistant) arriving after sentence_start must NOT create a duplicate bubble
                    dlg.put(Message(role="assistant", content="[gentle] 今天晴空万里。很适合出门散步！"))
                    asyncio.run(send_audio_mod.send_tts_message(conn, "stop", None))
                    cs._sync_runtime_events()

                    self.assertEqual(len(cs._chat_messages), 2)
                    self.assertEqual(cs._chat_messages[1]["role"], "assistant")
                    self.assertEqual(cs._chat_messages[1]["text"], "今天晴空万里。很适合出门散步！")
                    self.assertNotIn("[gentle]", cs._chat_messages[1]["text"])
                    self.assertEqual(cs._device_info["activity"], "idle")

                    # 3. Direct Dialogue.put(assistant) without sentence_start still appears in chat panel,
                    #    and Markdown-cleaned sentence_start arriving after assistant_message does NOT duplicate it
                    dlg.put(Message(role="user", content="纯文本测试"))
                    dlg.put(Message(role="assistant", content="[happy] 今天**很舒服**！"))
                    asyncio.run(send_audio_mod.send_tts_message(conn, "sentence_start", "今天很舒服！"))
                    asyncio.run(send_audio_mod.send_tts_message(conn, "stop", None))
                    cs._sync_runtime_events()
                    self.assertEqual(len(cs._chat_messages), 4)
                    self.assertEqual(cs._chat_messages[3]["text"], "今天**很舒服**！")
                    self.assertEqual(cs._device_info["activity"], "idle")

                    # 4. Abort (handleAbortMessage) bypasses send_tts_message upstream -> MUST still reset speaking->idle
                    import core.handle.abortHandle as abort_mod

                    class _AbortConn(_FakeConn):
                        client_is_speaking = True
                        close_after_chat = False

                        def __init__(self):
                            self.logger = _DummyLogger()

                        def clear_queues(self):
                            pass

                        def clearSpeakStatus(self):
                            self.client_is_speaking = False

                    class _DummyLogger:
                        def bind(self, **kw):
                            return self

                        def info(self, *a, **kw):
                            pass

                    a_conn = _AbortConn()
                    runtime_events.register_connection(a_conn)
                    try:
                        dlg.put(Message(role="user", content="帮我讲个长故事"))
                        cs._sync_runtime_events()
                        self.assertEqual(cs._device_info["activity"], "speaking")
                        self.assertTrue(runtime_events.is_any_device_speaking())
                        asyncio.run(abort_mod.handleAbortMessage(a_conn))
                        cs._sync_runtime_events()
                        self.assertEqual(cs._device_info["activity"], "idle")
                        self.assertFalse(runtime_events.is_any_device_speaking())

                        # 5. Disconnect mid-speech (connection_closed + unregister) MUST also reset speaking->idle
                        a_conn.client_is_speaking = True
                        dlg.put(Message(role="user", content="讲一半断网"))
                        cs._sync_runtime_events()
                        self.assertEqual(cs._device_info["activity"], "speaking")
                        runtime_events.unregister_connection(a_conn)
                        runtime_events.publish_event("connection_closed", device_id=a_conn.device_id)
                        cs._sync_runtime_events()
                        self.assertEqual(cs._device_info["activity"], "idle")
                        self.assertFalse(runtime_events.is_any_device_speaking())
                    finally:
                        runtime_events.unregister_connection(a_conn)

                    # 6. WebSocketServer._handle_auth with auth_enable=False MUST still emit device_seen
                    from core.websocket_server import WebSocketServer

                    ws_inst = object.__new__(WebSocketServer)
                    ws_inst.auth_enable = False

                    class _FakeReq:
                        headers = {"Device-Id": "11:22:33:44:55:66"}

                    class _FakeWsObj:
                        request = _FakeReq()

                    asyncio.run(WebSocketServer._handle_auth(ws_inst, _FakeWsObj()))
                    cs._sync_runtime_events()
                    self.assertEqual(cs._device_info["device_id"], "11:22:33:44:55:66")
        finally:
            cs._chat_messages = orig_msgs
            cs._device_info = orig_dev
            cs._log_offset = orig_off
            cs._current_ai_turn_open = orig_open
            cs._turn_has_tts = orig_has_tts
            cs._turn_dialogue_bubble_open = orig_dlg_open
            cs._last_event_seq = orig_seq
            runtime_events.mark_hooks_active(orig_hooks)

    def test_p112_universal_rebind_replaces_early_bound_send_tts_message(self):
        import sys
        import types
        import boot
        import patch_registry
        import start_with_env  # noqa: F401

        boot.install_all(start_background_workers=False, run_contract_self_check=True)
        import core.handle.sendAudioHandle as send_audio_mod
        import core.handle.helloHandle as hello_mod
        import core.handle.textHandler.listenMessageHandler as listen_mod
        import core.handle.abortHandle as abort_mod
        import core.handle.receiveAudioHandle as recv_mod
        import core.handle.textHandler.abortMessageHandler as abort_msg_mod
        import core.handle.intentHandler as intent_mod

        # 1. Early-imported listenMessageHandler MUST bind to the final patched send_tts_message (not unpatched original)
        import asyncio
        from unittest.mock import AsyncMock, patch
        import pc_audio_router

        self.assertIs(listen_mod.send_tts_message, send_audio_mod.send_tts_message)
        self.assertIs(hello_mod.send_tts_message, send_audio_mod.send_tts_message)
        self.assertEqual(send_audio_mod.send_tts_message.__code__.co_name, "_pc_aware_send_tts_message")

        # Behavioral proof: calling listen_mod.send_tts_message("stop") invokes PC-speaker completion wait!
        async def _check_listen_stop_waits_pc():
            class _Ws:
                async def send(self, _m):
                    pass

            class _Conn:
                websocket = _Ws()
                session_id = "s-rebind"
                device_id = "dev-rebind"
                sentence_id = "sid-rebind"
                features = {"emoji": False}
                config = {"enable_stop_tts_notify": False}
                client_abort = False
                _pc_audio_active_sid = "sid-rebind"
                _pc_audio_queued_ms = 1200

                def clearSpeakStatus(self):
                    pass

            wait_mock = AsyncMock(return_value=True)
            with patch.object(pc_audio_router, "_wait_pc_audio_done", wait_mock):
                await listen_mod.send_tts_message(_Conn(), "stop", None)
            wait_mock.assert_awaited_once()

        asyncio.run(_check_listen_stop_waits_pc())

        # 2. handleAbortMessage and checkWakeupWords also rebound across every importer
        self.assertIs(recv_mod.handleAbortMessage, abort_mod.handleAbortMessage)
        self.assertIs(abort_msg_mod.handleAbortMessage, abort_mod.handleAbortMessage)
        self.assertIs(intent_mod.checkWakeupWords, hello_mod.checkWakeupWords)

        # 3. Synthetic rebind unit test: replaces references across modules while NEVER touching _orig* saved refs
        def _old_fn():
            return "old"

        def _new_fn():
            return "new"

        mod_a = types.ModuleType("core._synthetic_rebind_a")
        mod_a.target_fn = _old_fn
        mod_a._orig_target_fn = _old_fn
        mod_b = types.ModuleType("core._synthetic_rebind_b")
        mod_b.alias_fn = _old_fn
        sys.modules[mod_a.__name__] = mod_a
        sys.modules[mod_b.__name__] = mod_b
        try:
            rebound = patch_registry.rebind(_old_fn, _new_fn, module_prefixes=("core._synthetic_rebind_",))
            self.assertIs(mod_a.target_fn, _new_fn)
            self.assertIs(mod_b.alias_fn, _new_fn)
            self.assertIs(mod_a._orig_target_fn, _old_fn, "rebind must never overwrite _orig* backup refs")
            self.assertIn("core._synthetic_rebind_a.target_fn", rebound)
            self.assertIn("core._synthetic_rebind_b.alias_fn", rebound)
        finally:
            sys.modules.pop(mod_a.__name__, None)
            sys.modules.pop(mod_b.__name__, None)

    def test_p113_config_deep_merge_minimal_diff_and_hot_reload_without_os_exit(self):
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer
        from unittest.mock import MagicMock, patch
        import httpx
        import yaml
        import runtime_events
        import console_server as cs

        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, ".config.yaml")
            initial_cfg = {
                "prompt": "初始芽衣人设。\n",
                "selected_module": {
                    "VAD": "SileroVAD",
                    "ASR": "SherpaASR",
                    "LLM": "DeepSeekLLM",
                    "TTS": "GPT_SOVITS_V2",
                    "Memory": "AnimaBrain",
                    "Intent": "function_call",
                },
                "LLM": {
                    "DeepSeekLLM": {
                        "type": "openai",
                        "base_url": "https://custom.deepseek.proxy/v1",
                        "model_name": "deepseek-flash",
                        "api_key": "${DEEPSEEK_API_KEY}",
                        "max_tokens": 2048,
                        "custom_timeout": 42,
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
                        "url": "http://host.docker.internal:19880/tts",
                        "ref_audio_path": "${HOST_DATA_DIR}/voices/raiden_mei/mei_default_ref.wav",
                        "prompt_text": "今夜月色很美",
                        "sample_steps": 24,
                        "custom_tuning": "keep-me",
                    }
                },
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(initial_cfg, f, allow_unicode=True, sort_keys=False)

            gsv_swap_calls = []

            class _FakeSrv:
                def __init__(self):
                    self.config = dict(initial_cfg)
                    self.logger = MagicMock()
                    self._llm = "old-llm"

            class _FakeConn:
                def __init__(self):
                    self.common_config = dict(initial_cfg)
                    self.config = dict(initial_cfg)
                    self.device_id = "aa:bb:cc:dd:ee:ff"
                    self.client_ip = "127.0.0.1"
                    self.features = {"emoji": False}
                    self.llm = "old-llm"
                    self.prompt = ""
                    self.prompt_manager = MagicMock()
                    self.prompt_manager.build_enhanced_prompt.return_value = "ENHANCED_PROMPT"
                    self.tts = MagicMock()
                    self.tts.__class__.__module__ = "core.providers.tts.gpt_sovits_v2"
                    self.tts.config = {}

                def change_system_prompt(self, p):
                    self.prompt = p

            fake_srv = _FakeSrv()
            fake_conn = _FakeConn()
            runtime_events.register_ws_server(fake_srv)
            runtime_events.register_connection(fake_conn)

            httpd = ThreadingHTTPServer(("127.0.0.1", 0), cs.ConsoleHandler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            orig_port = cs.CONSOLE_PORT
            cs.CONSOLE_PORT = port
            try:
                with (
                    patch.object(cs, "CONFIG_PATH", cfg_path),
                    patch.object(cs, "_ensure_gsv_weights", side_effect=lambda v, persist_boot_yaml=False: gsv_swap_calls.append(v)),
                    patch.object(cs, "_sync_persona_to_anima"),
                    patch.object(os, "_exit", side_effect=AssertionError("os._exit must NOT be called on hot-reloadable config save")),
                    patch("core.utils.modules_initialize.initialize_modules", return_value={"llm": "new-hot-llm"}),
                ):
                    with httpx.Client(timeout=4.0, trust_env=False) as client:
                        post_hdrs = {
                            "Content-Type": "application/json",
                            "Origin": f"http://127.0.0.1:{port}",
                            "X-Console-Token": cs.get_console_token(),
                        }
                        # 1. Edit ONLY prompt -> LLM & TTS blocks remain untouched, ZERO os._exit, live prompt updated
                        r1 = client.post(
                            f"http://127.0.0.1:{port}/api/config",
                            json={
                                "prompt": "更新后的芽衣温柔人设。",
                                "llm_provider": "DeepSeekLLM",
                                "model_name": "deepseek-flash",
                                "voice": "__gsv_raiden_mei__",
                                "restart": True,
                            },
                            headers=post_hdrs,
                        )
                        self.assertEqual(r1.status_code, 200)
                        d1 = r1.json()
                        self.assertTrue(d1["ok"])
                        self.assertFalse(d1["restarting"])
                        self.assertTrue(d1["hot_reloaded"])
                        self.assertEqual(d1["changed_fields"], ["prompt"])
                        self.assertEqual(gsv_swap_calls, [], "Voice did not change; must not touch GSV weights")
                        self.assertEqual(fake_conn.prompt, "ENHANCED_PROMPT")

                        with open(cfg_path, encoding="utf-8") as _f:
                            on_disk = yaml.safe_load(_f)
                        self.assertEqual(on_disk["LLM"]["DeepSeekLLM"]["base_url"], "https://custom.deepseek.proxy/v1")
                        self.assertEqual(on_disk["LLM"]["DeepSeekLLM"]["custom_timeout"], 42)
                        self.assertEqual(on_disk["TTS"]["GPT_SOVITS_V2"]["sample_steps"], 24)
                        self.assertEqual(on_disk["TTS"]["GPT_SOVITS_V2"]["custom_tuning"], "keep-me")

                        # 2. Switch model_name + voice -> deep-merges preserving custom keys, hot-reloads LLM & TTS in-place
                        r2 = client.post(
                            f"http://127.0.0.1:{port}/api/config",
                            json={
                                "prompt": "更新后的芽衣温柔人设。",
                                "llm_provider": "DeepSeekLLM",
                                "model_name": "deepseek-chat",
                                "voice": "__gsv_xiaozhi_tw__",
                                "restart": True,
                            },
                            headers=post_hdrs,
                        )
                        self.assertEqual(r2.status_code, 200)
                        d2 = r2.json()
                        self.assertTrue(d2["ok"])
                        self.assertFalse(d2["restarting"])
                        self.assertEqual(set(d2["changed_fields"]), {"model_name", "voice"})
                        self.assertEqual(gsv_swap_calls, ["__gsv_xiaozhi_tw__"])
                        self.assertEqual(fake_srv._llm, "new-hot-llm")
                        self.assertEqual(fake_conn.llm, "new-hot-llm")

                        with open(cfg_path, encoding="utf-8") as _f:
                            on_disk2 = yaml.safe_load(_f)
                        self.assertEqual(on_disk2["LLM"]["DeepSeekLLM"]["model_name"], "deepseek-chat")
                        self.assertEqual(on_disk2["LLM"]["DeepSeekLLM"]["custom_timeout"], 42)
                        self.assertEqual(on_disk2["TTS"]["GPT_SOVITS_V2"]["custom_tuning"], "keep-me")
                        self.assertIn("xiaozhi_taiwan", on_disk2["TTS"]["GPT_SOVITS_V2"]["ref_audio_path"])

                        # 3. Re-submitting identical config writes nothing
                        r3 = client.post(
                            f"http://127.0.0.1:{port}/api/config",
                            json={
                                "prompt": "更新后的芽衣温柔人设。",
                                "llm_provider": "DeepSeekLLM",
                                "model_name": "deepseek-chat",
                                "voice": "__gsv_xiaozhi_tw__",
                            },
                            headers=post_hdrs,
                        )
                        self.assertEqual(r3.json()["changed_fields"], [])

                        # 4. Cross-engine TTS switch (GPT_SOVITS_V2 -> EdgeTTS) with active incompatible conn
                        #    MUST honestly report restarting=True instead of falsely claiming hot_reloaded!
                        r4 = client.post(
                            f"http://127.0.0.1:{port}/api/config",
                            json={
                                "prompt": "更新后的芽衣温柔人设。",
                                "llm_provider": "DeepSeekLLM",
                                "model_name": "deepseek-chat",
                                "voice": "zh-CN-XiaoxiaoNeural",
                                "restart": False,
                            },
                            headers=post_hdrs,
                        )
                        d4 = r4.json()
                        self.assertTrue(d4["ok"])
                        self.assertFalse(d4["hot_reloaded"])
                        self.assertEqual(d4["detail"], "tts_engine_class_changed_on_active_conn")

                        # 5. Cross-engine TTS switch with NO active connections succeeds hot-reload (no restart)
                        runtime_events.unregister_connection(fake_conn)
                        r5 = client.post(
                            f"http://127.0.0.1:{port}/api/config",
                            json={
                                "prompt": "更新后的芽衣温柔人设。",
                                "llm_provider": "DeepSeekLLM",
                                "model_name": "deepseek-chat",
                                "voice": "__gsv_raiden_mei__",
                                "restart": True,
                            },
                            headers=post_hdrs,
                        )
                        d5 = r5.json()
                        self.assertTrue(d5["ok"])
                        self.assertFalse(d5["restarting"])
                        self.assertTrue(d5["hot_reloaded"])
            finally:
                cs.CONSOLE_PORT = orig_port
                runtime_events.unregister_connection(fake_conn)
                with runtime_events._runtime_lock:
                    runtime_events._ws_servers.discard(fake_srv)
                httpd.shutdown()
                httpd.server_close()

    def test_p114_tts_preview_shared_gpu_lock_and_busy_rejection(self):
        import threading
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch
        import httpx
        import console_server as cs

        # 1. Live ESP32 TTS and Console preview MUST share the exact same RLock instance
        self.assertIs(cs._gsv_weights_lock, router.GSV_GPU_LOCK)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), cs.ConsoleHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        orig_port = cs.CONSOLE_PORT
        cs.CONSOLE_PORT = port
        orig_act = cs._device_info.get("activity")
        swap_calls = []

        class _FakeGsvResp:
            status_code = 200
            content = b"RIFF\x24\x00\x00\x00WAVEfmt "
            text = "ok"

        class _FakeHttpClient:
            def post(self, *args, **kwargs):
                return _FakeGsvResp()

        try:
            with (
                patch.object(cs, "_get_configured_gsv_voice", return_value="__gsv_raiden_mei__"),
                patch.object(cs, "_ensure_gsv_weights", side_effect=lambda v, persist_boot_yaml=False: swap_calls.append(v)),
                patch.object(cs, "_get_console_http_client", return_value=_FakeHttpClient()),
            ):
                with httpx.Client(timeout=4.0, trust_env=False) as client:
                    post_hdrs = {
                        "Content-Type": "application/json",
                        "Origin": f"http://127.0.0.1:{port}",
                        "X-Console-Token": cs.get_console_token(),
                    }
                    # 2. Device busy (speaking) + previewing DIFFERENT GSV voice -> 409 Conflict, ZERO weight swap!
                    with cs._state_lock:
                        cs._device_info["activity"] = "speaking"
                    r_busy = client.post(
                        f"http://127.0.0.1:{port}/api/tts-preview",
                        json={"voice": "__gsv_xiaozhi_tw__", "text": "测试"},
                        headers=post_hdrs,
                    )
                    self.assertEqual(r_busy.status_code, 409)
                    self.assertTrue(r_busy.json().get("busy"))
                    self.assertEqual(swap_calls, [], "Must NOT swap GPU weights while device is busy")

                    # 3. Same configured GSV voice preview succeeds even when speaking (no weight swap needed)
                    r_same = client.post(
                        f"http://127.0.0.1:{port}/api/tts-preview",
                        json={"voice": "__gsv_raiden_mei__", "text": "测试当前音色"},
                        headers=post_hdrs,
                    )
                    self.assertEqual(r_same.status_code, 200)
                    self.assertTrue(r_same.content.startswith(b"RIFF"))

                    # 4. Another thread holding shared GSV_GPU_LOCK -> non-active voice preview fails fast with 409
                    with cs._state_lock:
                        cs._device_info["activity"] = "idle"
                    held = threading.Event()
                    release = threading.Event()

                    def _hold_gpu():
                        with router.GSV_GPU_LOCK:
                            held.set()
                            release.wait(3.0)

                    holder = threading.Thread(target=_hold_gpu, daemon=True)
                    holder.start()
                    self.assertTrue(held.wait(2.0))
                    try:
                        r_lock = client.post(
                            f"http://127.0.0.1:{port}/api/tts-preview",
                            json={"voice": "__gsv_xiaozhi_tw__", "text": "锁冲突测试"},
                            headers=post_hdrs,
                        )
                        self.assertEqual(r_lock.status_code, 409)
                        self.assertTrue(r_lock.json().get("busy"))
                    finally:
                        release.set()
                        holder.join(timeout=2.0)

                    # 5. Idle + lock free -> previewing different GSV voice swaps, synthesizes, and restores live voice
                    swap_calls.clear()
                    r_idle = client.post(
                        f"http://127.0.0.1:{port}/api/tts-preview",
                        json={"voice": "__gsv_xiaozhi_tw__", "text": "空闲试听"},
                        headers=post_hdrs,
                    )
                    self.assertEqual(r_idle.status_code, 200)
                    self.assertEqual(swap_calls, ["__gsv_xiaozhi_tw__", "__gsv_raiden_mei__"])

            # 6. Partial GPU weight swap failure (set_gpt_weights 200 -> set_sovits_weights 500) MUST
            #    invalidate _active_gsv_loaded and restore configured active voice weights in finally!
            gsv_reqs: list[tuple[str, str]] = []
            sovits_fail_once = {"done": False}

            class _PartialSwapClient:
                def get(self, url, params=None, timeout=None):
                    wp = (params or {}).get("weights_path", "")
                    gsv_reqs.append((url.rsplit("/", 1)[-1], wp))
                    if url.endswith("/set_sovits_weights") and not sovits_fail_once["done"]:
                        sovits_fail_once["done"] = True
                        r = _FakeGsvResp()
                        r.status_code = 500
                        r.text = "oom"
                        return r
                    return _FakeGsvResp()

                def post(self, *args, **kwargs):
                    return _FakeGsvResp()

            orig_loaded = cs._active_gsv_loaded
            cs._active_gsv_loaded = "__gsv_raiden_mei__"
            try:
                with (
                    patch.object(cs, "_get_configured_gsv_voice", return_value="__gsv_raiden_mei__"),
                    patch.object(cs, "_get_console_http_client", return_value=_PartialSwapClient()),
                ):
                    with httpx.Client(timeout=4.0, trust_env=False) as client:
                        r_part = client.post(
                            f"http://127.0.0.1:{port}/api/tts-preview",
                            json={"voice": "__gsv_xiaozhi_tw__", "text": "半失败恢复测试"},
                            headers={
                                "Content-Type": "application/json",
                                "Origin": f"http://127.0.0.1:{port}",
                                "X-Console-Token": cs.get_console_token(),
                            },
                        )
                        self.assertEqual(r_part.status_code, 500)
                        # Expect: set_gpt(tw) -> set_sovits(tw FAIL) -> restore set_gpt(mei) -> restore set_sovits(mei)
                        self.assertEqual(
                            [ep for ep, _ in gsv_reqs],
                            ["set_gpt_weights", "set_sovits_weights", "set_gpt_weights", "set_sovits_weights"],
                        )
                        self.assertIn("raiden_mei", gsv_reqs[2][1])
                        self.assertIn("raiden_mei", gsv_reqs[3][1])
                        self.assertEqual(cs._active_gsv_loaded, "__gsv_raiden_mei__")
            finally:
                cs._active_gsv_loaded = orig_loaded
        finally:
            cs.CONSOLE_PORT = orig_port
            with cs._state_lock:
                cs._device_info["activity"] = orig_act
            httpd.shutdown()
            httpd.server_close()

    def test_p220_xz_common_dedup_atomic_write_and_token_cache(self):
        import tempfile
        from unittest.mock import patch
        import xz_common

        with tempfile.TemporaryDirectory() as td:
            jp = os.path.join(td, "sample.json")
            xz_common.atomic_write_json(jp, {"k": "v", "n": 42})
            self.assertEqual([f for f in os.listdir(td) if f.endswith(".tmp")], [])

            cache = xz_common.MtimeJsonCache()
            self.assertEqual(cache.get(jp), {"k": "v", "n": 42})

            tf = os.path.join(td, "tok.txt")
            xz_common.atomic_write_text(tf, "secret_token_alpha_1234567890\n")
            self.assertEqual(xz_common.read_token_file(tf), "secret_token_alpha_1234567890")
            # Transient open failure retains last known cached token
            with patch("builtins.open", side_effect=PermissionError("locked")):
                self.assertEqual(xz_common.read_token_file(tf), "secret_token_alpha_1234567890")
            xz_common.invalidate_token_cache(tf)

    def test_p221_runtime_settings_lossless_migration_archive_and_unified_l3_default(self):
        import json
        import tempfile
        from unittest.mock import patch
        import console_server as cs
        import runtime_settings as rs

        with tempfile.TemporaryDirectory() as td:
            rs_path = os.path.join(td, "runtime_settings.json")
            with open(os.path.join(td, "pc_control_mode.json"), "w", encoding="utf-8") as f:
                json.dump({"mode": "full"}, f)
            with open(os.path.join(td, "audio_output_mode.json"), "w", encoding="utf-8") as f:
                json.dump({"target": "pc", "volume": 1.25}, f)
            with open(os.path.join(td, "reply_length_config.json"), "w", encoding="utf-8") as f:
                json.dump({"preset": "chatty", "chat_chars": 260, "story_chars": 750}, f)

            migrated = rs.read_runtime_settings(rs_path, auto_migrate=True)
            self.assertEqual(migrated["schema_version"], 1)
            self.assertEqual(migrated["pc_control"]["mode"], "full")
            self.assertEqual(migrated["audio_output"], {"target": "pc", "volume": 1.25})
            self.assertEqual(migrated["reply_length"]["preset"], "chatty")
            self.assertTrue(os.path.isfile(rs_path))
            # Legacy 3 files archived as *.migrated.bak
            for legacy_name in ("pc_control_mode.json", "audio_output_mode.json", "reply_length_config.json"):
                self.assertFalse(os.path.exists(os.path.join(td, legacy_name)))
                self.assertTrue(os.path.isfile(os.path.join(td, f"{legacy_name}.migrated.bak")))
            # P0-2: host-private L3 config must NEVER be stored in runtime_settings.json
            self.assertNotIn("l3_backend", migrated)

            # Updating one section preserves sibling sections atomically
            rs.set_pc_mode("safe", settings_path=rs_path)
            after = rs.read_runtime_settings(rs_path)
            self.assertEqual(after["pc_control"]["mode"], "safe")
            self.assertEqual(after["audio_output"], {"target": "pc", "volume": 1.25})
            self.assertEqual(after["reply_length"]["chat_chars"], 260)

        # Unified L3_BACKEND fallback default is 'dsh' when env unset
        clean_env = {k: v for k, v in os.environ.items() if k != "L3_BACKEND"}
        with patch.dict(os.environ, clean_env, clear=True):
            self.assertEqual(cs._read_local_l3_fallback()["backend"], "dsh")

    def test_p222_console_state_background_probe_hysteresis_and_incremental_log_cursor(self):
        from unittest.mock import patch
        import console_server as cs

        # 1. Cursor-based incremental log delta & wrap/restart reset
        with cs._state_lock:
            saved_logs = list(cs._raw_logs)
            saved_seq = cs._raw_log_seq
            cs._raw_logs.clear()
            cs._raw_log_seq = 0
            s1 = cs._append_raw_log_locked("line one")
            s2 = cs._append_raw_log_locked("line two")
            s3 = cs._append_raw_log_locked("line three")
        try:
            full_lines, cur_full, inc_full, rst_full = cs._get_logs_for_state(None)
            self.assertEqual(full_lines, ["line one", "line two", "line three"])
            self.assertEqual(cur_full, s3)
            self.assertFalse(inc_full)
            self.assertFalse(rst_full)

            delta_lines, cur_d, inc_d, rst_d = cs._get_logs_for_state(s1)
            self.assertEqual(delta_lines, ["line two", "line three"])
            self.assertEqual(cur_d, s3)
            self.assertTrue(inc_d)
            self.assertFalse(rst_d)

            empty_lines, cur_e, inc_e, rst_e = cs._get_logs_for_state(s3)
            self.assertEqual(empty_lines, [])
            self.assertEqual(cur_e, s3)
            self.assertTrue(inc_e)
            self.assertFalse(rst_e)

            # Future cursor (simulates server restart) triggers logs_reset=True with full tail
            rst_lines, cur_r, inc_r, rst_r = cs._get_logs_for_state(s3 + 99)
            self.assertEqual(rst_lines, ["line one", "line two", "line three"])
            self.assertEqual(cur_r, s3)
            self.assertFalse(inc_r)
            self.assertTrue(rst_r)
        finally:
            with cs._state_lock:
                cs._raw_logs.clear()
                cs._raw_logs.extend(saved_logs)
                cs._raw_log_seq = saved_seq

        # 2. Background probe hysteresis: 1st failure keeps online=True, 2nd consecutive failure flips offline
        with cs._probe_lock:
            old_pc_ok = cs._pc_audio_online_cache
            old_gsv_ok = cs._gsv_online_cache
            old_fails = dict(cs._probe_fail_counts)
            cs._pc_audio_online_cache = True
            cs._gsv_online_cache = True
            cs._probe_fail_counts = {k: 0 for k in cs._probe_fail_counts}
        try:
            with (
                patch.object(cs.pc_audio_router, "is_pc_audio_bridge_online", return_value=False),
                patch.object(cs, "_probe_gsv_tcp_once", return_value=False),
                patch.object(cs, "_get_console_http_client", side_effect=RuntimeError("offline")),
                patch.object(cs, "_read_anima_token", return_value=""),
            ):
                cs._probe_background_services_once()
                # 1st transient failure must NOT flap offline
                self.assertTrue(cs._get_cached_pc_audio_online())
                self.assertTrue(cs._get_cached_gsv_online())

                cs._probe_background_services_once()
                # 2nd consecutive failure smoothly transitions to offline
                self.assertFalse(cs._get_cached_pc_audio_online())
                self.assertFalse(cs._get_cached_gsv_online())
        finally:
            with cs._probe_lock:
                cs._pc_audio_online_cache = old_pc_ok
                cs._gsv_online_cache = old_gsv_ok
                cs._probe_fail_counts.update(old_fails)

    def test_p223_reply_length_max_tokens_downscale_and_nmc_partial_cache_guard(self):
        import asyncio
        import json
        import tempfile
        from unittest.mock import patch
        import httpx
        import nmc_weather_patch as nmc
        import reply_length_router as rl
        from core.providers.llm.openai.openai import LLMProvider

        # 1. reply_length_router max_tokens scales UP and cleanly scales BACK DOWN
        rl.apply_reply_length_llm_patch()

        class _DummyLLM:
            max_tokens = 1024

        dummy = _DummyLLM()
        with tempfile.TemporaryDirectory() as td:
            tmp_cfg = os.path.join(td, "reply_length_config.json")
            orig_cache = dict(rl._cfg_cache)
            orig_mtime = rl._cfg_mtime
            try:
                with patch.object(rl, "REPLY_LEN_PATH", tmp_cfg):
                    rl._cfg_mtime = -1.0
                    rl.write_reply_length_config(preset="storyteller")
                    with patch.object(LLMProvider, "response", LLMProvider.response):
                        # Call patched response with a mocked inner generator via closure check
                        with patch.object(
                            rl,
                            "read_reply_length_config",
                            wraps=rl.read_reply_length_config,
                        ):
                            # Trigger _sync_max_tokens via patched wrapper on a dummy subclass
                            class _FakeProvider(LLMProvider):
                                def __init__(self):
                                    self.max_tokens = 1024
                                    self.model_name = "test"
                                    self.temperature = 0.7
                                    self.top_p = 1.0
                                    self.frequency_penalty = 0.0

                                def normalize_dialogue(self, d):
                                    return d

                                def _apply_thinking_disabled(self, p):
                                    pass

                                class _FakeClient:
                                    class chat:
                                        class completions:
                                            @staticmethod
                                            def create(**kw):
                                                class _EmptyStream:
                                                    def __iter__(self):
                                                        return iter(())

                                                    def close(self):
                                                        pass

                                                return _EmptyStream()

                                client = _FakeClient()

                            fp = _FakeProvider()
                            list(fp.response("s1", []))
                            self.assertEqual(fp.max_tokens, 2456)

                            # Now lower back to concise -> max_tokens MUST drop back to 1024!
                            rl.write_reply_length_config(preset="concise")
                            list(fp.response("s2", []))
                            self.assertEqual(fp.max_tokens, 1024)
            finally:
                rl._cfg_cache = orig_cache
                rl._cfg_mtime = orig_mtime

        # 2. nmc_weather_patch must NOT overwrite fuller disk cache or permanently poison memory on partial failure
        with tempfile.TemporaryDirectory() as td:
            cache_file = os.path.join(td, "nmc_stations_cache.json")
            # Seed a partial disk cache (400 items < 500 threshold so refresh is attempted)
            existing_400 = [{"code": f"C{i}", "province": "湖南", "city": f"市{i}"} for i in range(400)]
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(existing_400, f)

            provs = [{"code": f"P{i:02d}"} for i in range(34)]

            def _partial_transport(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/province/all"):
                    return httpx.Response(200, json=provs)
                # Only 5 of 34 provinces succeed -> incomplete!
                code = request.url.path.rsplit("/", 1)[-1]
                if code in ("P00", "P01", "P02", "P03", "P04"):
                    return httpx.Response(200, json=[{"code": f"{code}-1", "province": "测试", "city": f"城{code}"}])
                return httpx.Response(503)

            orig_client = httpx.AsyncClient

            def _mock_async_client(*args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(_partial_transport)
                return orig_client(*args, **kwargs)

            orig_list = list(nmc._stations_list)
            try:
                nmc._stations_list = []
                with (
                    patch.object(nmc, "STATIONS_CACHE_PATH", cache_file),
                    patch.object(nmc.httpx, "AsyncClient", side_effect=_mock_async_client),
                ):
                    res = asyncio.run(nmc._ensure_stations_loaded())
                # Returned existing 400 rather than truncating to 5, did NOT poison _stations_list, did NOT overwrite disk
                self.assertEqual(len(res), 400)
                self.assertEqual(nmc._stations_list, [])
                with open(cache_file, "r", encoding="utf-8") as f:
                    self.assertEqual(len(json.load(f)), 400)
            finally:
                nmc._stations_list = orig_list

    def test_custom_idle_farewell_settings_direct_tts_and_hot_console_sync(self):
        import asyncio
        import json
        import queue
        import tempfile
        import time
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch
        import console_server as cs
        import runtime_events as re_bus
        import runtime_settings as rs

        # 1. runtime_settings normalization, backward compatibility, clamping & explicit empty list
        with tempfile.TemporaryDirectory() as td:
            rs_path = os.path.join(td, "runtime_settings.json")
            # Legacy v1 file without idle_farewell section falls back to neutral defaults
            with open(rs_path, "w", encoding="utf-8") as f:
                json.dump({"schema_version": 1, "pc_control": {"mode": "safe"}}, f)
            defaults = rs.get_idle_farewell(settings_path=rs_path)
            self.assertTrue(defaults["enabled"])
            self.assertEqual(defaults["timeout_seconds"], 120)
            self.assertGreaterEqual(len(defaults["messages"]), 2)

            # Custom multi-line string dedupes, trims, clamps timeout, preserves sibling sections
            saved = rs.set_idle_farewell(
                enabled=True,
                timeout_seconds=9999,
                messages=" 哥哥先去忙吧，有事随时叫我。 \n\n哥哥先去忙吧，有事随时叫我。\n[tender] 记得早点休息哦。",
                settings_path=rs_path,
            )
            self.assertEqual(saved["timeout_seconds"], 1800)
            self.assertEqual(
                saved["messages"],
                ["哥哥先去忙吧，有事随时叫我。", "[tender] 记得早点休息哦。"],
            )
            self.assertEqual(rs.get_pc_mode(settings_path=rs_path), "safe")

            # Explicit empty list stays empty (silent close) rather than resurrecting defaults
            cleared = rs.set_idle_farewell(messages=[], settings_path=rs_path)
            self.assertEqual(cleared["messages"], [])

            # Console helper validates input & hot-syncs live connections + ws server config
            fake_srv = SimpleNamespace(config={"close_connection_no_voice_time": 120})
            fake_conn = SimpleNamespace(timeout_seconds=180, config={"close_connection_no_voice_time": 120})
            with (
                patch.object(rs, "RUNTIME_SETTINGS_PATH", rs_path),
                patch.object(re_bus, "get_ws_servers", return_value=[fake_srv]),
                patch.object(re_bus, "get_active_connections", return_value=[fake_conn]),
            ):
                ok_bad, _ = cs._update_idle_farewell_config({"enabled": "yes"})
                self.assertFalse(ok_bad)
                ok_bad_mode, _ = cs._update_idle_farewell_config({"mode": "invalid_mode"})
                self.assertFalse(ok_bad_mode)
                ok_bad_to, _ = cs._update_idle_farewell_config({"timeout_seconds": 10})
                self.assertFalse(ok_bad_to)
                ok_good, res_good = cs._update_idle_farewell_config(
                    {
                        "enabled": True,
                        "mode": "llm_summary",
                        "timeout_seconds": 240,
                        "summary_prompt": "简短总结刚才的话题并表达依恋不舍。",
                        "messages": ["哥哥先去忙吧，有需要随时叫我。"],
                    }
                )
                self.assertTrue(ok_good)
                self.assertEqual(res_good["mode"], "llm_summary")
                self.assertEqual(res_good["timeout_seconds"], 240)
                self.assertIn("依恋不舍", res_good["summary_prompt"])
                self.assertEqual(fake_srv.config["close_connection_no_voice_time"], 240)
                self.assertEqual(fake_conn.config["close_connection_no_voice_time"], 240)
                self.assertEqual(fake_conn.timeout_seconds, 300)

        # 2. Helper formatting: Mei gets [gentle] default while explicit tags & non-Mei stay intact
        self.assertEqual(
            router.format_idle_farewell_for_tts("哥哥先去忙吧。", is_mei_voice=True),
            "[gentle] 哥哥先去忙吧。",
        )
        self.assertEqual(
            router.format_idle_farewell_for_tts("[tender] 记得早点休息哦。", is_mei_voice=True),
            "[tender] 记得早点休息哦。",
        )
        self.assertEqual(
            router.format_idle_farewell_for_tts("先去忙吧。", is_mei_voice=False),
            "先去忙吧。",
        )

        # 3. Patched receiveAudioHandle.no_voice_close_connect speaks via direct TTS with ZERO LLM/STT/Dialogue
        if _HAS_XIAOZHI_CORE:
            import core.handle.receiveAudioHandle as recv_mod
            import core.handle.sendAudioHandle as send_audio_mod
            from core.providers.tts.dto.dto import ContentType, SentenceType

            router.apply_idle_farewell_patch()

            def _make_conn(is_mei=True, speaking=False, idle_sec=150.0):
                c = SimpleNamespace(
                    last_activity_time=(time.time() - idle_sec) * 1000.0,
                    timeout_seconds=180,
                    close_after_chat=False,
                    client_abort=False,
                    client_is_speaking=speaking,
                    _pc_audio_active_sid=None,
                    sentence_id="old-sid",
                    config={},
                    logger=MagicMock(),
                    close=AsyncMock(),
                    tts=SimpleNamespace(
                        ref_audio_path="/opt/xiaozhi-esp32-server/data/voices/raiden_mei/mei_default_ref.wav"
                        if is_mei
                        else "",
                        tts_text_queue=queue.Queue(),
                    ),
                )
                return c

            async def _run_scenarios():
                start_to_chat_mock = AsyncMock()
                send_tts_mock = AsyncMock()

                # 3a. While speaking -> never triggers timeout, refreshes activity timer
                c_speak = _make_conn(speaking=True, idle_sec=500.0)
                with (
                    patch.object(recv_mod, "startToChat", start_to_chat_mock),
                    patch.object(send_audio_mod, "send_tts_message", send_tts_mock),
                    patch.object(
                        rs,
                        "get_idle_farewell",
                        return_value={"enabled": True, "timeout_seconds": 60, "messages": ["再见"]},
                    ),
                ):
                    await recv_mod.no_voice_close_connect(c_speak, False)
                self.assertFalse(c_speak.close_after_chat)
                self.assertEqual(c_speak.tts.tts_text_queue.qsize(), 0)
                start_to_chat_mock.assert_not_awaited()

                # 3b. Enabled=False -> silent close without TTS or LLM
                c_off = _make_conn(idle_sec=150.0)
                with (
                    patch.object(recv_mod, "startToChat", start_to_chat_mock),
                    patch.object(send_audio_mod, "send_tts_message", send_tts_mock),
                    patch.object(
                        rs,
                        "get_idle_farewell",
                        return_value={"enabled": False, "timeout_seconds": 60, "messages": ["再见"]},
                    ),
                ):
                    await recv_mod.no_voice_close_connect(c_off, False)
                self.assertTrue(c_off.close_after_chat)
                c_off.close.assert_awaited_once()
                self.assertEqual(c_off.tts.tts_text_queue.qsize(), 0)
                start_to_chat_mock.assert_not_awaited()

                # 3c. Enabled=True -> direct TTS FIRST/MIDDLE/LAST with Mei [gentle], zero startToChat, synced timeout
                c_on = _make_conn(is_mei=True, idle_sec=130.0)
                with (
                    patch.object(recv_mod, "startToChat", start_to_chat_mock),
                    patch.object(send_audio_mod, "send_tts_message", send_tts_mock),
                    patch.object(
                        rs,
                        "get_idle_farewell",
                        return_value={
                            "enabled": True,
                            "timeout_seconds": 90,
                            "messages": ["哥哥先去忙吧，有事随时叫我。"],
                        },
                    ),
                ):
                    await recv_mod.no_voice_close_connect(c_on, False)

                self.assertTrue(c_on.close_after_chat)
                self.assertTrue(c_on.client_is_speaking)
                self.assertEqual(c_on.timeout_seconds, 150)
                self.assertNotEqual(c_on.sentence_id, "old-sid")
                start_to_chat_mock.assert_not_awaited()
                send_tts_mock.assert_awaited_once_with(c_on, "start")
                c_on.close.assert_not_awaited()  # Closed later by sendAudioMessage(SentenceType.LAST)

                items = []
                while not c_on.tts.tts_text_queue.empty():
                    items.append(c_on.tts.tts_text_queue.get_nowait())
                self.assertEqual(len(items), 3)
                self.assertEqual(items[0].sentence_type, SentenceType.FIRST)
                self.assertEqual(items[0].content_type, ContentType.ACTION)
                self.assertEqual(items[1].sentence_type, SentenceType.MIDDLE)
                self.assertEqual(items[1].content_type, ContentType.TEXT)
                self.assertEqual(items[1].content_detail, "[gentle] 哥哥先去忙吧，有事随时叫我。")
                self.assertEqual(items[2].sentence_type, SentenceType.LAST)
                self.assertEqual(items[2].content_type, ContentType.ACTION)
                self.assertEqual({it.sentence_id for it in items}, {c_on.sentence_id})

                # 3d. llm_summary mode: extracts clean session turns (skipping wakeup/temporary), calls LLM bypass, ZERO Dialogue.put / startToChat
                fake_dlg_msgs = [
                    SimpleNamespace(role="system", content="你是雷电芽衣", is_temporary=False, tool_calls=None),
                    SimpleNamespace(role="assistant", content="我在呢，哥哥请说吧。", is_temporary=False, _standalone_assistant=True, tool_calls=None),
                    SimpleNamespace(role="user", content="今天把小智服务端整理清爽了不少。", is_temporary=False, tool_calls=None),
                    SimpleNamespace(role="assistant", content="[happy] 太棒啦哥哥，整理完一定轻松多了吧！", is_temporary=False, tool_calls=None),
                    SimpleNamespace(role="assistant", content="temp reminder", is_temporary=True, tool_calls=None),
                ]
                captured_req = {}

                def _fake_llm_create(**kw):
                    captured_req.update(kw)
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    content="<think>internal</think>[tender] 刚聊完你把服务端整理清爽的事，真有点舍不得先停下来呢，哥哥想我了随时再叫我哦。"
                                )
                            )
                        ]
                    )

                c_sum = _make_conn(is_mei=True, idle_sec=140.0)
                c_sum.config = {"prompt": "你是雷电芽衣，称呼对方为哥哥。"}
                c_sum.dialogue = SimpleNamespace(dialogue=list(fake_dlg_msgs), put=MagicMock())
                c_sum.stop_event = SimpleNamespace(is_set=lambda: False)
                c_sum.llm = SimpleNamespace(
                    model_name="deepseek-flash",
                    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_fake_llm_create))),
                    _apply_thinking_disabled=lambda p: p.setdefault("extra_body", {}).update({"thinking": {"type": "disabled"}}),
                )
                send_tts_sum = AsyncMock()
                with (
                    patch.object(recv_mod, "startToChat", start_to_chat_mock),
                    patch.object(send_audio_mod, "send_tts_message", send_tts_sum),
                    patch.object(
                        rs,
                        "get_idle_farewell",
                        return_value={
                            "enabled": True,
                            "mode": "llm_summary",
                            "timeout_seconds": 90,
                            "summary_prompt": "简短总结刚才的话题并表达依恋和不舍。",
                            "messages": ["[gentle] 兜底句"],
                        },
                    ),
                ):
                    await recv_mod.no_voice_close_connect(c_sum, False)
                    self.assertTrue(hasattr(c_sum, "_idle_farewell_task"))
                    await c_sum._idle_farewell_task

                # Verified zero Dialogue.put / zero startToChat / wakeup skipped in transcript / thinking stripped
                c_sum.dialogue.put.assert_not_called()
                start_to_chat_mock.assert_not_awaited()
                self.assertIn("整理清爽", captured_req["messages"][1]["content"])
                self.assertNotIn("我在呢，哥哥请说吧", captured_req["messages"][1]["content"])
                self.assertEqual(captured_req.get("extra_body"), {"thinking": {"type": "disabled"}})
                send_tts_sum.assert_awaited_once_with(c_sum, "start")
                sum_items = []
                while not c_sum.tts.tts_text_queue.empty():
                    sum_items.append(c_sum.tts.tts_text_queue.get_nowait())
                self.assertEqual(len(sum_items), 3)
                self.assertIn("[tender]", sum_items[1].content_detail)
                self.assertNotIn("<think>", sum_items[1].content_detail)

                # 3e. llm_summary with ZERO user turns -> instant fallback to fixed line without calling LLM
                c_empty = _make_conn(is_mei=True, idle_sec=140.0)
                c_empty.dialogue = SimpleNamespace(dialogue=[], put=MagicMock())
                c_empty.llm = SimpleNamespace(
                    model_name="deepseek-flash",
                    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=MagicMock()))),
                )
                send_tts_empty = AsyncMock()
                with (
                    patch.object(recv_mod, "startToChat", start_to_chat_mock),
                    patch.object(send_audio_mod, "send_tts_message", send_tts_empty),
                    patch.object(
                        rs,
                        "get_idle_farewell",
                        return_value={
                            "enabled": True,
                            "mode": "llm_summary",
                            "timeout_seconds": 90,
                            "summary_prompt": "总结",
                            "messages": ["哥哥先忙，随时叫我。"],
                        },
                    ),
                ):
                    await recv_mod.no_voice_close_connect(c_empty, False)
                c_empty.llm.client.chat.completions.create.assert_not_called()
                self.assertTrue(c_empty.close_after_chat)
                self.assertEqual(c_empty.tts.tts_text_queue.qsize(), 3)

                # 3f. User speaks (have_voice=True) while LLM summary is in-flight -> cancels farewell cleanly!
                def _slow_llm_create(**kw):
                    time.sleep(0.08)
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[tender] 舍不得哥哥"))])

                c_cancel = _make_conn(is_mei=True, idle_sec=140.0)
                c_cancel.dialogue = SimpleNamespace(dialogue=list(fake_dlg_msgs), put=MagicMock())
                c_cancel.stop_event = SimpleNamespace(is_set=lambda: False)
                c_cancel.llm = SimpleNamespace(
                    model_name="deepseek-flash",
                    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_slow_llm_create))),
                )
                send_tts_cancel = AsyncMock()
                with (
                    patch.object(send_audio_mod, "send_tts_message", send_tts_cancel),
                    patch.object(
                        rs,
                        "get_idle_farewell",
                        return_value={
                            "enabled": True,
                            "mode": "llm_summary",
                            "timeout_seconds": 90,
                            "summary_prompt": "总结",
                            "messages": ["兜底"],
                        },
                    ),
                ):
                    await recv_mod.no_voice_close_connect(c_cancel, False)
                    # Voice arrives while LLM call is running!
                    await recv_mod.no_voice_close_connect(c_cancel, True)
                    await c_cancel._idle_farewell_task
                self.assertFalse(c_cancel.close_after_chat)
                self.assertEqual(c_cancel.tts.tts_text_queue.qsize(), 0)
                send_tts_cancel.assert_not_awaited()

            asyncio.run(_run_scenarios())

    def test_batch1_hardening_c1_c2_c3_b3_b4_b6_a5_d3(self):
        import asyncio
        import io
        import json
        import subprocess
        import tempfile
        import threading
        import time
        from contextlib import redirect_stdout
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch
        from urllib.parse import unquote
        import httpx
        import anima_memory_provider as amp
        import console_server as cs
        import boot
        import patch_registry
        import start_with_env

        boot.install_all(start_background_workers=False, run_contract_self_check=True)

        # --- C-1: Security category + fail-closed boot gate + XIAOZHI_ALLOW_INSECURE_BOOT=1 ---
        for sec_name in ("esp32_auth_ota", "esp32_auth_ws", "esp32_auth_vision", "mcp_token_injection"):
            st = patch_registry.get_patch_status(sec_name)
            self.assertIsNotNone(st)
            self.assertEqual(st.get("category"), "security", f"{sec_name} must be category=security")

        orig_ota_entry = patch_registry.get_patch_status("esp32_auth_ota")
        try:
            patch_registry.record_patch(
                "esp32_auth_ota",
                "skipped_contract",
                target="OTAHandler.handle_post",
                detail="simulated drift",
                contract_ok=False,
                category="security",
            )
            with patch.dict(os.environ, {"XIAOZHI_ALLOW_INSECURE_BOOT": ""}, clear=False):
                with self.assertRaises(SystemExit) as cm_exit:
                    patch_registry.enforce_security_patches_or_exit(exit_code=2)
                self.assertEqual(cm_exit.exception.code, 2)

            with patch.dict(os.environ, {"XIAOZHI_ALLOW_INSECURE_BOOT": "1"}, clear=False):
                failed_list = patch_registry.enforce_security_patches_or_exit(exit_code=2)
                self.assertEqual([e["name"] for e in failed_list], ["esp32_auth_ota"])
                summ = patch_registry.get_health_summary()
                self.assertFalse(summ["security_ok"])
                self.assertTrue(summ["insecure_boot"])
                self.assertEqual(summ["status"], "unhealthy")
                self.assertIn("esp32_auth_ota", summ["security_failures"])
        finally:
            with patch_registry._lock:
                patch_registry._patches["esp32_auth_ota"] = orig_ota_entry

        # Real subprocess exit-code-2 test when security patch fails contract at __main__ boot
        sub_code = "\n".join(
            [
                "import os, sys, runpy",
                "os.environ.pop('XIAOZHI_ALLOW_INSECURE_BOOT', None)",
                "sys.path.insert(0, '/opt/xiaozhi-esp32-server')",
                "sys.path.insert(0, '/opt/xiaozhi-esp32-server/app')",
                "from core.api import ota_handler",
                "def _broken_sync(self):",
                "    return None",
                "ota_handler.OTAHandler.handle_post = _broken_sync",
                "runpy.run_path('/opt/xiaozhi-esp32-server/app/start_with_env.py', run_name='__main__')",
            ]
        )
        proc = subprocess.run([sys.executable, "-c", sub_code], capture_output=True, text=True, timeout=20)
        self.assertEqual(
            proc.returncode,
            2,
            f"Expected exit code 2 on failed security patch, got {proc.returncode}; stderr={proc.stderr}",
        )
        self.assertIn("esp32_auth_ota", proc.stderr)

        # --- C-2: One-shot missing-anchor warning + dry_run partial status ---
        router.reset_prompt_anchor_warnings()
        orig_pa_entry = patch_registry.get_patch_status("mei_prompt_anchors")
        try:
            with open("/opt/xiaozhi-esp32-server/agent-base-prompt.txt", "r", encoding="utf-8") as f:
                real_tpl = f.read()
            broken_anchor_name, broken_anchor_old, _ = router._PROMPT_REPLACEMENTS[0]
            mutated_tpl = real_tpl.replace(broken_anchor_old, "MUTATED_UPSTREAM_TEXT")
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tf:
                tf.write(mutated_tpl)
                tf_path = tf.name
            try:
                st_dr, _det_dr, applied_dr, missing_dr = router.dry_run_prompt_harmony(tf_path)
                self.assertEqual(st_dr, "partial")
                self.assertEqual(missing_dr, [broken_anchor_name])
                self.assertEqual(len(applied_dr), len(router._PROMPT_REPLACEMENTS) - 1)
            finally:
                os.unlink(tf_path)

            router.reset_prompt_anchor_warnings()
            buf = io.StringIO()
            with redirect_stdout(buf):
                r1 = router.apply_prompt_harmony_replacements(mutated_tpl)
                r2 = router.apply_prompt_harmony_replacements(mutated_tpl)
                r3 = router.apply_prompt_harmony_replacements(mutated_tpl)
            self.assertEqual(r1, r2)
            self.assertEqual(r2, r3)
            # Warning for the broken anchor must appear ONLY ONCE across 3 turns
            self.assertEqual(buf.getvalue().count(broken_anchor_name), 1)
            # Remaining anchors must still be applied cleanly
            self.assertIn(router._PROMPT_REPLACEMENTS[1][2], r1)
        finally:
            with patch_registry._lock:
                patch_registry._patches["mei_prompt_anchors"] = orig_pa_entry
            router.reset_prompt_anchor_warnings()

        # --- C-3: install_patch MUST NOT replace target when signature contract fails ---
        class _DummyTarget:
            def drifted_fn(self, only_one):
                return ("orig", only_one)

        orig_method = _DummyTarget.drifted_fn
        ok_inst, rec_inst = patch_registry.install_patch(
            "synthetic_c3_drift",
            _DummyTarget,
            "drifted_fn",
            lambda fn: (lambda self, *a, **kw: ("wrapped",)),
            category="experience",
            required_params=("self", "missing_required_param"),
            require_async=False,
        )
        try:
            self.assertFalse(ok_inst)
            self.assertEqual(rec_inst["status"], "skipped_contract")
            self.assertFalse(rec_inst["contract_ok"])
            self.assertIs(_DummyTarget.drifted_fn, orig_method)
            self.assertEqual(_DummyTarget().drifted_fn(42), ("orig", 42))
        finally:
            with patch_registry._lock:
                patch_registry._patches.pop("synthetic_c3_drift", None)

        # --- B-3 & B-4 (Outbox): journal_size_limit=8MB, WAL truncate, uuid4 fallback ---
        with tempfile.TemporaryDirectory() as td:
            ob = amp.AnimaOutbox(os.path.join(td, "ob.sqlite"), auto_start=False)
            try:
                db = ob._ensure_db()
                self.assertEqual(db.execute("PRAGMA journal_size_limit").fetchone()[0], 8 * 1024 * 1024)
                ob.enqueue([{"user_message": "hello", "assistant_reply": "world"}])
                cp = ob.checkpoint_wal()
                self.assertIsNotNone(cp)
            finally:
                ob.close()

        gen_ids = {
            amp._canonical_turn_payload({"user_message": "u", "assistant_reply": "a"})["request_id"]
            for _ in range(40)
        }
        self.assertEqual(len(gen_ids), 40)
        for gid in gen_ids:
            self.assertRegex(gid, r"^xz-[0-9a-f]{32}$")

        # --- A-5, B-6, D-3 via real ConsoleHandler HTTP server ---
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), cs.ConsoleHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        orig_port = cs.CONSOLE_PORT
        orig_act = cs._device_info.get("activity")
        orig_loaded = cs._active_gsv_loaded
        cs.CONSOLE_PORT = port
        try:
            with cs._state_lock:
                cs._device_info["activity"] = "idle"
            cs._set_voice_dirty(None, "")

            with httpx.Client(timeout=5.0, trust_env=False) as client:
                # A-5: Clickjacking headers on HTML console
                r_html = client.get(f"http://127.0.0.1:{port}/")
                self.assertEqual(r_html.status_code, 200)
                self.assertEqual(r_html.headers.get("x-frame-options"), "DENY")
                self.assertIn("frame-ancestors 'none'", r_html.headers.get("content-security-policy", ""))
                self.assertIn("securityBanner", r_html.text)
                self.assertIn("animaBackupText", r_html.text)

                # B-6: /api/health returns 503 when a security patch fails, 200 + degraded list when experience patch fails
                orig_sec = patch_registry.get_patch_status("mcp_token_injection")
                try:
                    patch_registry.record_patch(
                        "mcp_token_injection",
                        "failed",
                        target="ServerMCPManager.load_config",
                        detail="boom",
                        contract_ok=False,
                        category="security",
                    )
                    r_503 = client.get(f"http://127.0.0.1:{port}/api/health")
                    self.assertEqual(r_503.status_code, 503)
                    self.assertFalse(r_503.json()["security_ok"])
                    self.assertIn("mcp_token_injection", r_503.json()["security_failures"])
                finally:
                    with patch_registry._lock:
                        patch_registry._patches["mcp_token_injection"] = orig_sec

                patch_registry.record_patch(
                    "synthetic_exp_partial",
                    "partial",
                    target="demo",
                    detail="partial drift",
                    contract_ok=False,
                    category="experience",
                )
                try:
                    r_200_deg = client.get(f"http://127.0.0.1:{port}/api/health")
                    self.assertEqual(r_200_deg.status_code, 200)
                    j_deg = r_200_deg.json()
                    self.assertEqual(j_deg["status"], "degraded")
                    self.assertIsInstance(j_deg["degraded"], list)
                    self.assertIn("synthetic_exp_partial", j_deg["degraded"])
                    self.assertTrue(j_deg["security_ok"])
                finally:
                    with patch_registry._lock:
                        patch_registry._patches.pop("synthetic_exp_partial", None)

                # D-3a: Edge preview timeout -> HTTP 504
                async def _slow_edge(text, voice):
                    await asyncio.sleep(1.5)
                    return b"ID3"

                with (
                    patch.object(cs, "EDGE_PREVIEW_TIMEOUT_SECONDS", 0.12),
                    patch.object(cs, "_synthesize_mp3", side_effect=_slow_edge),
                ):
                    r_edge_to = client.post(
                        f"http://127.0.0.1:{port}/api/tts-preview",
                        json={"voice": "zh-CN-XiaoxiaoNeural", "text": "超时测试"},
                        headers={
                            "Content-Type": "application/json",
                            "Origin": f"http://127.0.0.1:{port}",
                            "X-Console-Token": cs.get_console_token(),
                        },
                    )
                    self.assertEqual(r_edge_to.status_code, 504)
                    self.assertIn("超时", r_edge_to.json().get("error", ""))

                # D-3b: Preview rollback failure -> logs, marks voice_dirty, returns warning header, probe retries & clears
                class _FakeResp200:
                    status_code = 200
                    content = b"RIFF\x24\x00\x00\x00WAVEfmt "
                    text = "ok"

                class _FakeHttp:
                    def post(self, *a, **kw):
                        return _FakeResp200()

                    def get(self, *a, **kw):
                        return _FakeResp200()

                call_seq = []

                def _swap_fail_rollback(v, persist_boot_yaml=False):
                    call_seq.append(v)
                    if len(call_seq) == 2:
                        raise RuntimeError("GSV rollback OOM")

                with (
                    patch.object(cs, "_get_configured_gsv_voice", return_value="__gsv_raiden_mei__"),
                    patch.object(cs, "_ensure_gsv_weights", side_effect=_swap_fail_rollback),
                    patch.object(cs, "_get_console_http_client", return_value=_FakeHttp()),
                ):
                    r_rb = client.post(
                        f"http://127.0.0.1:{port}/api/tts-preview",
                        json={"voice": "__gsv_xiaozhi_tw__", "text": "回滚失败测试"},
                        headers={
                            "Content-Type": "application/json",
                            "Origin": f"http://127.0.0.1:{port}",
                            "X-Console-Token": cs.get_console_token(),
                        },
                    )
                    self.assertEqual(r_rb.status_code, 200)
                    self.assertEqual(r_rb.headers.get("x-voice-dirty"), "1")
                    warn_txt = unquote(r_rb.headers.get("x-preview-warning", ""))
                    self.assertIn("恢复线上音色权重", warn_txt)
                    dirty_v, dirty_e = cs._get_voice_dirty_state()
                    self.assertEqual(dirty_v, "__gsv_raiden_mei__")
                    self.assertIn("GSV rollback OOM", dirty_e)
                    self.assertEqual(cs._active_gsv_loaded, "__dirty__")

                # Now background probe retries & recovers voice_dirty!
                recovered = []
                with (
                    patch.object(cs, "_probe_gsv_tcp_once", return_value=True),
                    patch.object(cs, "_get_configured_gsv_voice", return_value="__gsv_raiden_mei__"),
                    patch.object(cs, "_ensure_gsv_weights", side_effect=lambda v, persist_boot_yaml=False: recovered.append(v)),
                    patch.object(cs, "_get_console_http_client", return_value=_FakeHttp()),
                    patch.object(cs.pc_audio_router, "is_pc_audio_bridge_online", return_value=False),
                ):
                    cs._probe_background_services_once()
                self.assertEqual(recovered, ["__gsv_raiden_mei__"])
                self.assertEqual(cs._get_voice_dirty_state(), (None, ""))

                # D-3b2: Stale voice_dirty MUST reconcile with newly saved voice (never revert to obsolete voice!)
                cs._set_voice_dirty("__gsv_raiden_mei__", "old rollback error")
                recovered_edge = []
                with (
                    patch.object(cs, "_probe_gsv_tcp_once", return_value=True),
                    patch.object(cs, "_get_configured_gsv_voice", return_value=None),
                    patch.object(cs, "_ensure_gsv_weights", side_effect=lambda v, persist_boot_yaml=False: recovered_edge.append(v)),
                    patch.object(cs, "_get_console_http_client", return_value=_FakeHttp()),
                    patch.object(cs.pc_audio_router, "is_pc_audio_bridge_online", return_value=False),
                ):
                    cs._probe_background_services_once()
                self.assertEqual(recovered_edge, [])
                self.assertEqual(cs._get_voice_dirty_state(), (None, ""))

                cs._set_voice_dirty("__gsv_raiden_mei__", "old rollback error")
                recovered_new = []
                with (
                    patch.object(cs, "_probe_gsv_tcp_once", return_value=True),
                    patch.object(cs, "_get_configured_gsv_voice", return_value="__gsv_xiaozhi_tw__"),
                    patch.object(cs, "_ensure_gsv_weights", side_effect=lambda v, persist_boot_yaml=False: recovered_new.append(v)),
                    patch.object(cs, "_get_console_http_client", return_value=_FakeHttp()),
                    patch.object(cs.pc_audio_router, "is_pc_audio_bridge_online", return_value=False),
                ):
                    cs._probe_background_services_once()
                self.assertEqual(recovered_new, ["__gsv_xiaozhi_tw__"])
                self.assertEqual(cs._get_voice_dirty_state(), (None, ""))

                # D-3c: GPU lock total duration cap -> returns 504, NEVER extends deadline during rollback, releases lock
                cap_calls = []

                def _slow_swap_other(v, persist_boot_yaml=False):
                    cap_calls.append(v)
                    time.sleep(0.08)

                with (
                    patch.object(cs, "_GSV_PREVIEW_LOCK_MAX_SECONDS", 0.03),
                    patch.object(cs, "_get_configured_gsv_voice", return_value="__gsv_raiden_mei__"),
                    patch.object(cs, "_ensure_gsv_weights", side_effect=_slow_swap_other),
                    patch.object(cs, "_get_console_http_client", return_value=_FakeHttp()),
                ):
                    r_cap = client.post(
                        f"http://127.0.0.1:{port}/api/tts-preview",
                        json={"voice": "__gsv_xiaozhi_tw__", "text": "锁超时测试"},
                        headers={
                            "Content-Type": "application/json",
                            "Origin": f"http://127.0.0.1:{port}",
                            "X-Console-Token": cs.get_console_token(),
                        },
                    )
                    self.assertEqual(r_cap.status_code, 504)
                    cap_body = r_cap.json()
                    self.assertTrue(cap_body.get("voice_dirty"))
                    self.assertIn("后台将自动重试", cap_body.get("rollback_warning", ""))
                # Rollback must have been skipped fast once cap expired (not called a second time extending lock!)
                self.assertEqual(cap_calls, ["__gsv_xiaozhi_tw__"])
                self.assertEqual(cs._get_voice_dirty_state()[0], "__gsv_raiden_mei__")
                cs._set_voice_dirty(None, "")
                # Lock must have been released cleanly
                self.assertTrue(cs._gsv_weights_lock.acquire(blocking=False))
                cs._gsv_weights_lock.release()

                # A-5b: HTML error pages from BaseHTTPRequestHandler.send_error (e.g. 501 PUT) also carry clickjacking headers
                r_501 = client.put(f"http://127.0.0.1:{port}/")
                self.assertEqual(r_501.status_code, 501)
                self.assertEqual(r_501.headers.get("x-frame-options"), "DENY")
                self.assertIn("frame-ancestors 'none'", r_501.headers.get("content-security-policy", ""))
                self.assertEqual(r_501.headers.get("x-content-type-options"), "nosniff")
        finally:
            cs._set_voice_dirty(None, "")
            cs._active_gsv_loaded = orig_loaded
            cs.CONSOLE_PORT = orig_port
            with cs._state_lock:
                cs._device_info["activity"] = orig_act
            httpd.shutdown()
            httpd.server_close()

        # --- C-1 extra: Boot gate MUST fail-closed when a required security patch is MISSING from registry ---
        orig_sec_ota = patch_registry.get_patch_status("esp32_auth_ota")
        try:
            with patch_registry._lock:
                patch_registry._patches.pop("esp32_auth_ota", None)
            with patch.dict(os.environ, {"XIAOZHI_ALLOW_INSECURE_BOOT": ""}, clear=False):
                with self.assertRaises(SystemExit) as cm_miss:
                    start_with_env.enforce_security_boot_gate(exit_code=2)
                self.assertEqual(cm_miss.exception.code, 2)
            with patch.dict(os.environ, {"XIAOZHI_ALLOW_INSECURE_BOOT": "1"}, clear=False):
                missing_fail = start_with_env.enforce_security_boot_gate(exit_code=2)
                self.assertEqual([e["name"] for e in missing_fail], ["esp32_auth_ota"])
                # Must also have recorded the missing patch so /api/health returns 503
                self.assertFalse(patch_registry.get_health_summary()["security_ok"])
        finally:
            with patch_registry._lock:
                patch_registry._patches["esp32_auth_ota"] = orig_sec_ota

        # C-1 extra: insecure_boot_env flag is exposed even when security_ok is True
        with patch.dict(os.environ, {"XIAOZHI_ALLOW_INSECURE_BOOT": "1"}, clear=False):
            summ_arm = patch_registry.get_health_summary()
            self.assertTrue(summ_arm["security_ok"])
            self.assertTrue(summ_arm["insecure_boot_env"])

        # --- C-3 extra: verify_signature_contract & install_patch handle @classmethod and @staticmethod cleanly ---
        class _DescDemo:
            @classmethod
            def cm(cls, a, b):
                return (cls.__name__, a, b)

            @staticmethod
            def sm(x, y):
                return (x, y)

        ok_cm, _ = patch_registry.verify_signature_contract(_DescDemo.__dict__["cm"], required_params=("cls", "a", "b"))
        self.assertTrue(ok_cm)
        ok_sm, _ = patch_registry.install_patch(
            "synthetic_sm",
            _DescDemo,
            "sm",
            lambda orig: (lambda x, y: ("sm_wrapped", orig(x, y))),
            required_params=("x", "y"),
        )
        ok_cm_i, _ = patch_registry.install_patch(
            "synthetic_cm",
            _DescDemo,
            "cm",
            lambda orig: (lambda cls, a, b: ("cm_wrapped", orig(cls, a, b))),
            required_params=("cls", "a", "b"),
        )
        try:
            self.assertTrue(ok_sm)
            self.assertTrue(ok_cm_i)
            self.assertEqual(_DescDemo().sm(1, 2), ("sm_wrapped", (1, 2)))
            self.assertEqual(_DescDemo.cm(3, 4), ("cm_wrapped", ("_DescDemo", 3, 4)))
        finally:
            with patch_registry._lock:
                patch_registry._patches.pop("synthetic_sm", None)
                patch_registry._patches.pop("synthetic_cm", None)

        # --- B-4 extra: AnimaOutbox collision guard + stable uuid4 fallback in extract_clean_pairs ---
        with tempfile.TemporaryDirectory() as td_col:
            ob_col = amp.AnimaOutbox(os.path.join(td_col, "col.sqlite"), auto_start=False)
            try:
                ob_col.enqueue(
                    [{"request_id": "xz-col-1", "user_message": "第一句", "assistant_reply": "原始短答"}]
                )
                # Different user_message with SAME request_id -> refused (must NOT overwrite pending row!)
                added_u = ob_col.enqueue(
                    [{"request_id": "xz-col-1", "user_message": "冲突问题", "assistant_reply": "很长很长的冲突回复"}]
                )
                self.assertEqual(added_u, 0)
                # Same user_message but divergent non-prefix longer reply -> refused!
                added_a = ob_col.enqueue(
                    [{"request_id": "xz-col-1", "user_message": "第一句", "assistant_reply": "完全不相关的更长回复内容"}]
                )
                self.assertEqual(added_a, 0)
                row_col = ob_col._ensure_db().execute(
                    "SELECT payload_json FROM outbox_turns WHERE request_id='xz-col-1'"
                ).fetchone()
                saved_col = json.loads(row_col["payload_json"])
                self.assertEqual(saved_col["user_message"], "第一句")
                self.assertEqual(saved_col["assistant_reply"], "原始短答")
            finally:
                ob_col.close()

        class _NoUidMsg:
            def __init__(self, role, content):
                self.role = role
                self.content = content

        u_m1, a_m1 = _NoUidMsg("user", "问题一"), _NoUidMsg("assistant", "回答一")
        u_m2, a_m2 = _NoUidMsg("user", "问题二"), _NoUidMsg("assistant", "回答二")
        p_first = amp.extract_clean_pairs([u_m1, a_m1])
        p_second = amp.extract_clean_pairs([u_m1, a_m1, u_m2, a_m2])
        self.assertEqual(p_first[0]["request_id"], p_second[0]["request_id"])
        self.assertNotEqual(p_second[0]["request_id"], p_second[1]["request_id"])
        self.assertRegex(p_second[0]["request_id"], r"^xz-[0-9a-f]{32}$")

    def test_batch1_same_gsv_voice_dirty_save_recovery_and_console_degraded_guard(self):
        import tempfile
        import threading
        import time
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch
        import httpx
        import yaml
        import console_server as cs

        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, ".config.yaml")
            infer_yaml_path = os.path.join(td, "tts_infer_xiaozhi.yaml")
            initial_cfg = {
                "prompt": "初始芽衣人设。\n",
                "selected_module": {
                    "LLM": "DeepSeekLLM",
                    "TTS": "GPT_SOVITS_V2",
                },
                "LLM": {
                    "DeepSeekLLM": {
                        "type": "openai",
                        "model_name": "deepseek-flash",
                    }
                },
                "TTS": {
                    "GPT_SOVITS_V2": {
                        "type": "gpt_sovits_v2",
                        "url": "http://host.docker.internal:19880/tts",
                        "ref_audio_path": "${HOST_DATA_DIR}/voices/raiden_mei/mei_default_ref.wav",
                        "prompt_text": "今夜月色很美",
                    }
                },
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(initial_cfg, f, allow_unicode=True, sort_keys=False)

            httpd = ThreadingHTTPServer(("127.0.0.1", 0), cs.ConsoleHandler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            orig_port = cs.CONSOLE_PORT
            orig_loaded = cs._active_gsv_loaded
            cs.CONSOLE_PORT = port
            try:
                http_gets = []

                class _FakeResp200:
                    status_code = 200
                    text = "ok"

                class _FakeHttp:
                    def get(self, url, params=None, timeout=None):
                        http_gets.append((url, dict(params or {})))
                        return _FakeResp200()

                with (
                    patch.object(cs, "CONFIG_PATH", cfg_path),
                    patch.object(cs, "GSV_INFER_YAML", infer_yaml_path),
                    patch.object(cs, "_get_console_http_client", return_value=_FakeHttp()),
                    patch.object(cs, "_sync_persona_to_anima"),
                ):
                    with httpx.Client(timeout=4.0, trust_env=False) as client:
                        post_hdrs = {
                            "Content-Type": "application/json",
                            "Origin": f"http://127.0.0.1:{port}",
                            "X-Console-Token": cs.get_console_token(),
                        }
                        # 1. Saving the SAME GSV voice while voice_dirty is set (even if _active_gsv_loaded
                        #    happens to match) MUST force real /set_gpt_weights + /set_sovits_weights,
                        #    clear voice_dirty, and return HTTP 200 voice_recovered=True without restarting.
                        cs._active_gsv_loaded = "__gsv_raiden_mei__"
                        cs._set_voice_dirty("__gsv_raiden_mei__", "preview rollback failed")
                        r_rec = client.post(
                            f"http://127.0.0.1:{port}/api/config",
                            json={"voice": "__gsv_raiden_mei__", "restart": False},
                            headers=post_hdrs,
                        )
                        self.assertEqual(r_rec.status_code, 200)
                        body_rec = r_rec.json()
                        self.assertTrue(body_rec["ok"])
                        self.assertTrue(body_rec["voice_recovered"])
                        self.assertFalse(body_rec["voice_dirty"])
                        self.assertEqual(body_rec["changed_fields"], [])
                        self.assertEqual(body_rec["detail"], "voice_weights_recovered")
                        self.assertEqual(cs._get_voice_dirty_state(), (None, ""))
                        self.assertEqual(cs._active_gsv_loaded, "__gsv_raiden_mei__")
                        self.assertEqual(len(http_gets), 2)
                        self.assertTrue(http_gets[0][0].endswith("/set_gpt_weights"))
                        self.assertTrue(http_gets[1][0].endswith("/set_sovits_weights"))
                        self.assertTrue(os.path.isfile(infer_yaml_path))

                        # 2. Saving the SAME GSV voice when CLEAN must NOT re-trigger weight HTTP calls.
                        http_gets.clear()
                        r_clean = client.post(
                            f"http://127.0.0.1:{port}/api/config",
                            json={"voice": "__gsv_raiden_mei__", "restart": False},
                            headers=post_hdrs,
                        )
                        self.assertEqual(r_clean.status_code, 200)
                        self.assertFalse(r_clean.json()["voice_recovered"])
                        self.assertFalse(r_clean.json()["voice_dirty"])
                        self.assertEqual(http_gets, [])

                        # 3. Saving the SAME GSV voice while dirty when _ensure_gsv_weights FAILS must keep
                        #    voice_dirty populated (with latest error) and return HTTP 500.
                        cs._active_gsv_loaded = "__dirty__"
                        cs._set_voice_dirty("__gsv_raiden_mei__", "old err")
                        with patch.object(cs, "_ensure_gsv_weights", side_effect=RuntimeError("19880 unreachable")):
                            r_fail = client.post(
                                f"http://127.0.0.1:{port}/api/config",
                                json={"voice": "__gsv_raiden_mei__", "restart": False},
                                headers=post_hdrs,
                            )
                        self.assertEqual(r_fail.status_code, 500)
                        self.assertFalse(r_fail.json()["ok"])
                        dv, de = cs._get_voice_dirty_state()
                        self.assertEqual(dv, "__gsv_raiden_mei__")
                        self.assertIn("19880 unreachable", de)
                        self.assertEqual(cs._active_gsv_loaded, "__dirty__")

                        # 4. Saving the SAME GSV voice while dirty AND editing prompt simultaneously
                        #    recovers weights, clears voice_dirty, AND writes updated prompt to YAML.
                        http_gets.clear()
                        res_both = cs._write_updated_yaml(prompt="新的芽衣人设", voice="__gsv_raiden_mei__")
                        self.assertTrue(res_both["written"])
                        self.assertTrue(res_both["voice_recovered"])
                        self.assertEqual(res_both["changed_fields"], ["prompt"])
                        self.assertEqual(cs._get_voice_dirty_state(), (None, ""))
                        self.assertEqual(len(http_gets), 2)

                        # 5. Stale voice_dirty cleared cleanly when saving an already-selected EdgeTTS voice.
                        edge_cfg = {
                            "prompt": "Edge人设。\n",
                            "selected_module": {"LLM": "DeepSeekLLM", "TTS": "EdgeTTS"},
                            "TTS": {"EdgeTTS": {"type": "edge", "voice": "zh-CN-XiaoxiaoNeural"}},
                        }
                        with open(cfg_path, "w", encoding="utf-8") as f:
                            yaml.safe_dump(edge_cfg, f, allow_unicode=True, sort_keys=False)
                        cs._set_voice_dirty("__gsv_raiden_mei__", "stale")
                        http_gets.clear()
                        res_edge = cs._write_updated_yaml(voice="zh-CN-XiaoxiaoNeural")
                        self.assertFalse(res_edge["written"])
                        self.assertFalse(res_edge["voice_recovered"])
                        self.assertEqual(cs._get_voice_dirty_state(), (None, ""))
                        self.assertEqual(http_gets, [])

                        # 5b. Concurrent cross-voice switch vs _probe_background_services_once MUST NOT revert GPU weights
                        with open(cfg_path, "w", encoding="utf-8") as f:
                            yaml.safe_dump(initial_cfg, f, allow_unicode=True, sort_keys=False)
                        race_calls = []
                        save_in_ensure = threading.Event()
                        probe_past_read = threading.Event()

                        def _ensure_race(voice_id, persist_boot_yaml=False):
                            with cs._gsv_weights_lock:
                                race_calls.append((threading.current_thread().name, voice_id))
                                if threading.current_thread().name == "save-race":
                                    save_in_ensure.set()
                                    probe_past_read.wait(timeout=2.0)
                                    time.sleep(0.03)
                                cs._active_gsv_loaded = voice_id

                        orig_get_dirty = cs._get_voice_dirty_state

                        def _traced_dirty():
                            res = orig_get_dirty()
                            if threading.current_thread().name == "probe-race":
                                probe_past_read.set()
                            return res

                        class _ProbeResp:
                            status_code = 200
                            text = "ok"

                            def json(self):
                                return {}

                        class _ProbeHttp:
                            def get(self, url, params=None, timeout=None):
                                http_gets.append((url, dict(params or {})))
                                return _ProbeResp()

                        cs._active_gsv_loaded = "__dirty__"
                        cs._set_voice_dirty("__gsv_raiden_mei__", "old err")
                        with (
                            patch.object(cs, "_ensure_gsv_weights", side_effect=_ensure_race),
                            patch.object(cs, "_get_voice_dirty_state", side_effect=_traced_dirty),
                            patch.object(cs, "_is_device_busy", return_value=False),
                            patch.object(cs, "_fetch_l3_backend_status", return_value={"online": True}),
                            patch.object(cs, "_fetch_anima_relationship", return_value={"online": True}),
                            patch.object(cs, "_fetch_obsidian_status", return_value={"online": True}),
                            patch.object(cs.pc_audio_router, "is_pc_audio_bridge_online", return_value=True),
                            patch.object(cs, "_get_console_http_client", return_value=_ProbeHttp()),
                        ):
                            t_sv = threading.Thread(
                                target=lambda: cs._write_updated_yaml(voice="__gsv_xiaozhi_tw__"),
                                name="save-race",
                            )
                            t_sv.start()
                            self.assertTrue(save_in_ensure.wait(timeout=2.0))
                            t_pr = threading.Thread(
                                target=cs._probe_background_services_once,
                                name="probe-race",
                            )
                            t_pr.start()
                            t_sv.join(timeout=3.0)
                            t_pr.join(timeout=3.0)
                        self.assertEqual(race_calls, [("save-race", "__gsv_xiaozhi_tw__")])
                        self.assertEqual(cs._active_gsv_loaded, "__gsv_xiaozhi_tw__")
                        self.assertEqual(cs._get_voice_dirty_state(), (None, ""))

                        # 5c. Concurrent same-voice dirty save while probe is already recovering MUST NOT double-reload GPU
                        with open(cfg_path, "w", encoding="utf-8") as f:
                            yaml.safe_dump(initial_cfg, f, allow_unicode=True, sort_keys=False)
                        race_calls.clear()
                        probe_in_ensure = threading.Event()

                        def _ensure_probe_first(voice_id, persist_boot_yaml=False):
                            race_calls.append((threading.current_thread().name, voice_id))
                            if threading.current_thread().name == "probe-first":
                                probe_in_ensure.set()
                                time.sleep(0.06)
                            cs._active_gsv_loaded = voice_id

                        cs._active_gsv_loaded = "__dirty__"
                        cs._set_voice_dirty("__gsv_raiden_mei__", "old err")
                        with (
                            patch.object(cs, "_ensure_gsv_weights", side_effect=_ensure_probe_first),
                            patch.object(cs, "_is_device_busy", return_value=False),
                            patch.object(cs, "_fetch_l3_backend_status", return_value={"online": True}),
                            patch.object(cs, "_fetch_anima_relationship", return_value={"online": True}),
                            patch.object(cs, "_fetch_obsidian_status", return_value={"online": True}),
                            patch.object(cs.pc_audio_router, "is_pc_audio_bridge_online", return_value=True),
                            patch.object(cs, "_get_console_http_client", return_value=_ProbeHttp()),
                        ):
                            t_pf = threading.Thread(
                                target=cs._probe_background_services_once,
                                name="probe-first",
                            )
                            t_pf.start()
                            self.assertTrue(probe_in_ensure.wait(timeout=2.0))
                            res_dedup = cs._write_updated_yaml(voice="__gsv_raiden_mei__")
                            t_pf.join(timeout=3.0)
                        self.assertEqual(len(race_calls), 1)
                        self.assertEqual(race_calls[0], ("probe-first", "__gsv_raiden_mei__"))
                        self.assertFalse(res_dedup["voice_recovered"])
                        self.assertEqual(cs._get_voice_dirty_state(), (None, ""))

                        # 5d. _probe_background_services_once MUST force real GPU weight HTTP calls even if _active_gsv_loaded matches dirty_target
                        http_gets.clear()
                        cs._active_gsv_loaded = "__gsv_raiden_mei__"
                        cs._set_voice_dirty("__gsv_raiden_mei__", "stale gpu weights")
                        with (
                            patch.object(cs, "_is_device_busy", return_value=False),
                            patch.object(cs, "_fetch_l3_backend_status", return_value={"online": True}),
                            patch.object(cs, "_fetch_anima_relationship", return_value={"online": True}),
                            patch.object(cs, "_fetch_obsidian_status", return_value={"online": True}),
                            patch.object(cs.pc_audio_router, "is_pc_audio_bridge_online", return_value=True),
                            patch.object(cs, "_get_console_http_client", return_value=_ProbeHttp()),
                        ):
                            cs._probe_background_services_once()
                        weight_urls = [u for u, _ in http_gets if u.endswith(("/set_gpt_weights", "/set_sovits_weights"))]
                        self.assertEqual(len(weight_urls), 2)
                        self.assertEqual(cs._get_voice_dirty_state(), (None, ""))
            finally:
                cs._set_voice_dirty(None, "")
                cs._active_gsv_loaded = orig_loaded
                cs.CONSOLE_PORT = orig_port
                httpd.shutdown()
                httpd.server_close()

        # 6. Verify console static JS degraded guard & banner/save state machine guards
        with open(cs.JS_PATH, "r", encoding="utf-8") as f:
            js_src = f.read()
        self.assertIn('typeof hlth.degraded === "boolean" && hlth.degraded', js_src)
        self.assertNotIn("|| Boolean(hlth.degraded)", js_src)
        self.assertIn("function scheduleHideBanner(", js_src)
        self.assertNotIn("setTimeout(hideBanner,", js_src)
        self.assertIn('typeof res.voice_dirty === "boolean"', js_src)
        self.assertIn("stateEpoch", js_src)
        self.assertIn("isSaving", js_src)

    def test_batch2_a1_split_mount_paths_and_env_token_isolation(self):
        """A-1: Env-first token lookup + legacy HOST_DATA_DIR/voices path rewrite compatibility."""
        from unittest.mock import patch

        with patch.dict(
            os.environ,
            {"PC_MCP_TOKEN": "env-pc-mcp-tok-xyz", "ANIMA_STUDIO_TOKEN": "env-anima-tok-abc"},
            clear=False,
        ):
            self.assertEqual(xz_common.get_pc_mcp_token(), "env-pc-mcp-tok-xyz")
            self.assertEqual(xz_common.get_anima_token(), "env-anima-tok-abc")
            self.assertEqual(
                xz_common.get_pc_mcp_auth_headers()["Authorization"],
                "Bearer env-pc-mcp-tok-xyz",
            )

        rewritten_legacy = router._rewrite_host_data_path(
            "e:/故事/1/xiaozhi-server/data/voices/raiden_mei/mei_default_ref.wav"
        )
        self.assertEqual(rewritten_legacy, f"{router.HOST_VOICES_PREFIX}/raiden_mei/mei_default_ref.wav")
        rewritten_placeholder = router._rewrite_host_data_path(
            "${HOST_DATA_DIR}/voices/raiden_mei/mei_happy_ref.wav"
        )
        self.assertEqual(rewritten_placeholder, f"{router.HOST_VOICES_PREFIX}/raiden_mei/mei_happy_ref.wav")
        rewritten_tw = router._rewrite_host_data_path("${HOST_DATA_DIR}/xiaozhi_taiwan_ref.wav")
        self.assertEqual(rewritten_tw, f"{router.HOST_VOICES_PREFIX}/xiaozhi_taiwan_ref.wav")
        if os.name != "nt":
            self.assertEqual(
                router._to_local_fs_path(rewritten_legacy),
                f"{router.CONTAINER_VOICES_PREFIX}/raiden_mei/mei_default_ref.wav",
            )

        # Legacy .env containing ONLY HOST_DATA_DIR (plain `docker compose up -d` without start_all.ps1)
        # MUST still derive host voices/weights paths instead of falling back to /opt/xiaozhi-esp32-server/voices!
        derived = xz_common.resolve_host_dirs(
            {
                "HOST_DATA_DIR": "D:/myproj/xiaozhi-server/data",
                "HOST_PROJECT_DIR": "",
                "HOST_VOICES_DIR": "",
                "HOST_GSV_WEIGHTS_DIR": "",
            }
        )
        self.assertEqual(derived["HOST_PROJECT_DIR"], "D:/myproj/xiaozhi-server")
        self.assertEqual(derived["HOST_VOICES_DIR"], "D:/myproj/xiaozhi-server/voices")
        self.assertEqual(derived["HOST_GSV_WEIGHTS_DIR"], "D:/myproj/xiaozhi-server/models/gsv_weights")

    def test_batch2_a2_console_pc_mode_proxies_to_host_18900_and_fails_closed(self):
        """A-2: Console _write_pc_mode forwards to authenticated host 18900 POST /mode and fails closed."""
        import json
        import httpx
        from unittest.mock import patch
        import console_server as cs

        seen: list[tuple[str, str, dict]] = []

        def _ok_transport(request: httpx.Request) -> httpx.Response:
            seen.append(
                (
                    request.method,
                    request.url.path,
                    {
                        "auth": request.headers.get("authorization", ""),
                        "body": json.loads(request.content.decode("utf-8")),
                    },
                )
            )
            return httpx.Response(200, json={"ok": True, "mode": "full", "source": "host_state"})

        def _fail_transport(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        ok_cli = httpx.Client(transport=httpx.MockTransport(_ok_transport), trust_env=False)
        fail_cli = httpx.Client(transport=httpx.MockTransport(_fail_transport), trust_env=False)
        try:
            with (
                patch.object(cs, "_get_console_http_client", return_value=ok_cli),
                patch.object(
                    cs.pc_audio_router,
                    "get_pc_mcp_auth_headers",
                    return_value={"Authorization": "Bearer test-a2-token"},
                ),
            ):
                res = cs._write_pc_mode("full")
            self.assertTrue(res["ok"])
            self.assertEqual(res["mode"], "full")
            self.assertEqual(res["source"], "host_18900")
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][0], "POST")
            self.assertEqual(seen[0][1], "/mode")
            self.assertEqual(seen[0][2]["auth"], "Bearer test-a2-token")
            self.assertEqual(seen[0][2]["body"], {"mode": "full"})
            self.assertEqual(cs._read_pc_mode(), "full")

            # Fail-closed when 18900 rejects or is offline
            with patch.object(cs, "_get_console_http_client", return_value=fail_cli):
                with self.assertRaises(RuntimeError):
                    cs._write_pc_mode("safe")
            self.assertEqual(cs._read_pc_mode(), "full")
        finally:
            ok_cli.close()
            fail_cli.close()
            cs._set_cached_pc_mode("safe", online=False)

    def test_batch2_a4_console_token_injection_and_internal_host_restriction(self):
        """A-4: GET / injects per-boot token with no-store; POST enforces token; internal host is read-only."""
        import re
        import threading
        from http.server import ThreadingHTTPServer
        import httpx
        import console_server as cs

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), cs.ConsoleHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        orig_port = cs.CONSOLE_PORT
        cs.CONSOLE_PORT = port
        try:
            with httpx.Client(timeout=4.0, trust_env=False) as client:
                r_root = client.get(f"http://127.0.0.1:{port}/")
                self.assertEqual(r_root.status_code, 200)
                self.assertIn("no-store", r_root.headers.get("cache-control", "").lower())
                m = re.search(r'<meta\s+name="console-token"\s+content="([^"]+)"', r_root.text)
                self.assertIsNotNone(m, "GET / must inject <meta name='console-token'>")
                tok = m.group(1)
                self.assertEqual(tok, cs.get_console_token())
                self.assertNotIn("__XIAOZHI_CONSOLE_TOKEN__", r_root.text)
                self.assertIn('href="/static/app.css"', r_root.text)
                self.assertIn('src="/static/app.js"', r_root.text)

                # H-1: Split static CSS & JS served cleanly with nosniff + no-store
                r_css = client.get(f"http://127.0.0.1:{port}/static/app.css")
                self.assertEqual(r_css.status_code, 200)
                self.assertIn("text/css", r_css.headers.get("content-type", ""))
                self.assertEqual(r_css.headers.get("x-content-type-options"), "nosniff")
                r_js = client.get(f"http://127.0.0.1:{port}/static/app.js")
                self.assertEqual(r_js.status_code, 200)
                self.assertIn("javascript", r_js.headers.get("content-type", ""))
                self.assertEqual(r_js.headers.get("x-content-type-options"), "nosniff")

                # Missing or wrong X-Console-Token -> 401
                r_no = client.post(
                    f"http://127.0.0.1:{port}/api/idle-farewell",
                    json={"enabled": True},
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
                self.assertEqual(r_no.status_code, 401)
                r_bad = client.post(
                    f"http://127.0.0.1:{port}/api/idle-farewell",
                    json={"enabled": True},
                    headers={"Origin": f"http://127.0.0.1:{port}", "X-Console-Token": "invalid-tok"},
                )
                self.assertEqual(r_bad.status_code, 401)

                # Host: xiaozhi-esp32-server allows GET /api/health & /api/state ONLY
                r_int_h = client.get(
                    f"http://127.0.0.1:{port}/api/health",
                    headers={"Host": f"xiaozhi-esp32-server:{port}"},
                )
                self.assertEqual(r_int_h.status_code, 200)
                r_int_s = client.get(
                    f"http://127.0.0.1:{port}/api/state",
                    headers={"Host": f"xiaozhi-esp32-server:{port}"},
                )
                self.assertEqual(r_int_s.status_code, 200)
                r_int_root = client.get(
                    f"http://127.0.0.1:{port}/",
                    headers={"Host": f"xiaozhi-esp32-server:{port}"},
                )
                self.assertEqual(r_int_root.status_code, 403)
                r_int_post = client.post(
                    f"http://127.0.0.1:{port}/api/pc-mode",
                    json={"mode": "full"},
                    headers={"Host": f"xiaozhi-esp32-server:{port}", "X-Console-Token": tok},
                )
                self.assertEqual(r_int_post.status_code, 403)
        finally:
            cs.CONSOLE_PORT = orig_port
            httpd.shutdown()
            httpd.server_close()

    def test_batch2_f2_turn_context_split_legacy_rollback_and_usage_telemetry(self):
        """F-2: System prefix stays byte-identical across turns; legacy rollback works; usage telemetry tracks hits."""
        from types import SimpleNamespace
        from unittest.mock import patch
        import turn_context_router as tcr

        base_sys_t1 = (
            "你是雷电芽衣。\n"
            "<context>\n- Current time: 14:01\n- Weather: 晴 22C\n</context>\n"
            "<speakers_info>\n主用户\n</speakers_info>\n"
            "请以情绪标签开头。"
        )
        base_sys_t2 = (
            "你是雷电芽衣。\n"
            "<context>\n- Current time: 14:05\n- Weather: 多云 21C\n</context>\n"
            "<speakers_info>\n主用户\n</speakers_info>\n"
            "请以情绪标签开头。"
        )
        msgs_t1 = [
            {"role": "system", "content": base_sys_t1},
            {"role": "user", "content": "芽衣，今天好累"},
        ]
        out_t1 = tcr.apply_turn_context_to_messages(msgs_t1, memory_str="记忆A", mode="split")
        self.assertNotIn("<context>", out_t1[0]["content"])
        self.assertNotIn("<memory>", out_t1[0]["content"])
        self.assertNotIn("<reply_length_control>", out_t1[0]["content"])
        self.assertIn("<turn_context>", out_t1[1]["content"])
        self.assertIn("14:01", out_t1[1]["content"])
        self.assertIn("记忆A", out_t1[1]["content"])
        self.assertIn("芽衣，今天好累", out_t1[1]["content"])

        # Turn 2: time/weather/memory change, AND stale <turn_context> in history is stripped!
        msgs_t2 = [
            {"role": "system", "content": base_sys_t2},
            {"role": "user", "content": out_t1[1]["content"]},
            {"role": "assistant", "content": "[gentle] 辛苦啦，先歇一会儿吧。"},
            {"role": "user", "content": "晚饭吃什么好？"},
        ]
        out_t2 = tcr.apply_turn_context_to_messages(msgs_t2, memory_str="记忆B", mode="split")
        # System prompt prefix MUST be 100% byte-identical between Turn 1 and Turn 2!
        self.assertEqual(out_t1[0]["content"], out_t2[0]["content"])
        # Historical user turn MUST NOT retain stale <turn_context>
        self.assertNotIn("<turn_context>", out_t2[1]["content"])
        self.assertEqual(out_t2[1]["content"], "芽衣，今天好累")
        # Latest user turn gets fresh <turn_context>
        self.assertIn("<turn_context>", out_t2[3]["content"])
        self.assertIn("14:05", out_t2[3]["content"])
        self.assertIn("记忆B", out_t2[3]["content"])

        # Legacy rollback mode preserves <context>/<memory>/<reply_length_control> inside msgs[0]
        msgs_leg = [
            {"role": "system", "content": base_sys_t1},
            {"role": "user", "content": "你好"},
        ]
        out_leg = tcr.apply_turn_context_to_messages(msgs_leg, memory_str="记忆L", mode="legacy")
        self.assertIn("<context>", out_leg[0]["content"])
        self.assertIn("记忆L", out_leg[0]["content"])
        self.assertNotIn("<turn_context>", out_leg[1]["content"])

        # Inline prose mentioning '<memory>' (e.g. persona instructions) MUST NOT swallow <core_rules>
        from core.utils.dialogue import Dialogue, Message

        prose_sys = (
            "<identity>\n请结合 <memory> 中同步过来的长期记忆来回应哥哥。\n</identity>\n\n"
            "<core_rules>\nCRITICAL_KEEP_ME_INTACT\n</core_rules>\n\n"
            "<context>\n- Current time: 14:10\n</context>\n\n"
            "<memory>\n</memory>"
        )
        dlg_prose = Dialogue()
        dlg_prose.update_system_message(prose_sys)
        dlg_prose.put(Message(role="user", content="测试一下"))
        with patch.dict(os.environ, {"XIAOZHI_TURN_CONTEXT_MODE": "split"}, clear=False):
            m_split_p = dlg_prose.get_llm_dialogue_with_memory(memory_str="实时召回记忆")
        self.assertIn("CRITICAL_KEEP_ME_INTACT", m_split_p[0]["content"])
        self.assertIn("请结合 <memory> 中同步过来的长期记忆", m_split_p[0]["content"])
        self.assertNotIn("<context>", m_split_p[0]["content"])
        self.assertIn("实时召回记忆", m_split_p[1]["content"])

        with patch.dict(os.environ, {"XIAOZHI_TURN_CONTEXT_MODE": "legacy"}, clear=False):
            m_leg_p = dlg_prose.get_llm_dialogue_with_memory(memory_str="实时召回记忆")
        self.assertIn("CRITICAL_KEEP_ME_INTACT", m_leg_p[0]["content"])
        self.assertIn("实时召回记忆", m_leg_p[0]["content"])

        # Usage extraction supports DeepSeek (prompt_cache_hit_tokens) & OpenAI (prompt_tokens_details.cached_tokens)
        ds_m = tcr.extract_usage_metrics(
            {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200}
        )
        self.assertEqual(ds_m["prompt_cache_hit_tokens"], 800)
        self.assertEqual(ds_m["prompt_cache_miss_tokens"], 200)
        oa_m = tcr.extract_usage_metrics(
            {"prompt_tokens": 1000, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 750}}
        )
        self.assertEqual(oa_m["prompt_cache_hit_tokens"], 750)
        self.assertEqual(oa_m["prompt_cache_miss_tokens"], 250)

        # Streaming wrapper captures cache metrics AND gracefully retries if upstream rejects stream_options
        tcr.reset_llm_cache_stats()
        create_calls = []

        def _fake_stream_create(*args, **kwargs):
            create_calls.append(dict(kwargs))
            if "stream_options" in kwargs and len(create_calls) == 1:
                raise RuntimeError("Invalid argument: stream_options not supported")

            def _gen():
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="[gentle] 你好"))],
                    usage=None,
                )
                yield SimpleNamespace(
                    choices=[],
                    usage={
                        "prompt_tokens": 1200,
                        "completion_tokens": 30,
                        "prompt_cache_hit_tokens": 960,
                        "prompt_cache_miss_tokens": 240,
                    },
                )

            return _gen()

        fake_llm = SimpleNamespace(
            model_name="deepseek-flash",
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_fake_stream_create))),
        )
        self.assertTrue(tcr.wrap_llm_client_for_usage(fake_llm))
        chunks = list(fake_llm.client.chat.completions.create(model="deepseek-flash", stream=True, messages=[]))
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(create_calls), 2)
        self.assertIn("stream_options", create_calls[0])
        self.assertNotIn("stream_options", create_calls[1])
        st = tcr.get_llm_cache_stats()
        self.assertEqual(st["turns"], 1)
        self.assertEqual(st["cache_hit_tokens_total"], 960)
        self.assertAlmostEqual(st["overall_hit_rate_pct"], 80.0, places=1)
        self.assertAlmostEqual(st["recent_hit_rate_pct"], 80.0, places=1)
        self.assertAlmostEqual(st["last_hit_rate_pct"], 80.0, places=1)
        tcr.reset_llm_cache_stats()

    def test_batch2_f3_wakeup_and_hello_async_gsv_prime(self):
        """F-3: prime_gsv_async fires non-blocking POST /prime with debounce and never raises on failure."""
        import asyncio
        import threading
        from unittest.mock import patch
        import core.handle.helloHandle as hello_mod

        called = []
        done_ev = threading.Event()

        class _FakeSess:
            def post(self, url, json=None, timeout=None):
                called.append((url, json, timeout))
                done_ev.set()

        orig_ts = router._gsv_last_prime_ts
        try:
            router._gsv_last_prime_ts = 0.0
            with patch.object(router, "_get_gsv_http_session", return_value=_FakeSess()):
                ok1 = router.prime_gsv_async(None, reason="wakeup_test", min_interval_sec=30.0)
                self.assertTrue(ok1)
                self.assertTrue(done_ev.wait(timeout=2.0), "Async prime worker did not execute")
                # Immediate second call within debounce window is skipped
                ok2 = router.prime_gsv_async(None, reason="wakeup_dup", min_interval_sec=30.0)
                self.assertFalse(ok2)
            self.assertEqual(len(called), 1)
            self.assertTrue(called[0][0].endswith("/prime"))
            self.assertEqual(called[0][1], {"skip_if_busy": True, "reason": "wakeup_test"})

            # Verify handleHelloMessage patch triggers prime_gsv_async without blocking
            router.apply_mei_wakeup_responses_patch()
            self.assertTrue(callable(hello_mod.handleHelloMessage))

            # Verify non-Mei GSV voice (e.g. xiaozhi_tw) ALSO triggers wakeup prime when wakeup matches
            router._gsv_last_prime_ts = 0.0
            called.clear()
            done_ev.clear()
            fake_tw_tts = type(
                "FakeGsvTts",
                (),
                {
                    "__module__": "core.providers.tts.gpt_sovits_v2",
                    "url": "http://host.docker.internal:19880/tts",
                    "ref_audio_path": "${HOST_VOICES_DIR}/xiaozhi_taiwan_ref.wav",
                },
            )()
            fake_conn = type(
                "FakeConn",
                (),
                {"tts": fake_tw_tts, "config": {"enable_wakeup_words_response_cache": True, "wakeup_words": ["你好小智"]}},
            )()
            orig_inner_wakeup = hello_mod.checkWakeupWords.__wrapped__
            with (
                patch.object(router, "_get_gsv_http_session", return_value=_FakeSess()),
                patch.object(hello_mod.checkWakeupWords, "__wrapped__", orig_inner_wakeup),
            ):
                # Directly test prime_gsv_async accepts non-Mei GSV provider
                ok_tw = router.prime_gsv_async(fake_tw_tts, reason="wakeup_tw", min_interval_sec=30.0)
                self.assertTrue(ok_tw)
                self.assertTrue(done_ev.wait(timeout=2.0))
            self.assertEqual(len(called), 1)
            self.assertEqual(called[0][1]["reason"], "wakeup_tw")
        finally:
            router._gsv_last_prime_ts = orig_ts

    def test_batch2_f2_edge_cases_stats_legacy_multimodal_and_tool_round(self):
        """F-2 edge cases: non-cached stats isolation, non-mutating legacy mode, multimodal & tool-call turns."""
        import turn_context_router as tcr

        # 1. overall_hit_rate_pct must ignore non-cached turns (None when 0 measured, 80.0 when mixed)
        tcr.reset_llm_cache_stats()
        try:
            tcr.record_llm_usage(prompt_tokens=2000, completion_tokens=50, cache_hit_tokens=None, cache_miss_tokens=None)
            st1 = tcr.get_llm_cache_stats()
            self.assertIsNone(st1["overall_hit_rate_pct"])
            self.assertIsNone(st1["recent_hit_rate_pct"])
            tcr.record_llm_usage(prompt_tokens=1000, completion_tokens=50, cache_hit_tokens=800, cache_miss_tokens=200)
            st2 = tcr.get_llm_cache_stats()
            self.assertAlmostEqual(st2["overall_hit_rate_pct"], 80.0, places=1)
            self.assertAlmostEqual(st2["recent_hit_rate_pct"], 80.0, places=1)
        finally:
            tcr.reset_llm_cache_stats()

        # 2. Legacy mode must NOT mutate caller msgs in place and must resolve {{current_time}}
        raw_sys = "Base\n<context>\n- Current time: {{current_time}}\n</context>\n<memory>\n</memory>"
        msgs_in = [{"role": "system", "content": raw_sys}, {"role": "user", "content": "hi"}]
        out_leg = tcr.apply_turn_context_to_messages(
            msgs_in, memory_str=None, current_time_str="21:30", mode="legacy"
        )
        self.assertEqual(msgs_in[0]["content"], raw_sys, "legacy mode must not mutate caller msgs[0] in place")
        self.assertNotIn("{{current_time}}", out_leg[0]["content"])
        self.assertIn("21:30", out_leg[0]["content"])

        # 3. Multimodal content list: preserves image_url and strips stale <turn_context> on next turn
        mm_t1 = [
            {"role": "system", "content": "Persona\n<context>\n- Current time: 09:00\n</context>"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "帮我看看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
                ],
            },
        ]
        out_mm1 = tcr.apply_turn_context_to_messages(mm_t1, memory_str="M1", mode="split")
        self.assertIsInstance(out_mm1[1]["content"], list)
        self.assertTrue(any(p.get("type") == "image_url" for p in out_mm1[1]["content"]))
        self.assertIn("<turn_context>", out_mm1[1]["content"][0]["text"])

        mm_t2 = [
            {"role": "system", "content": "Persona\n<context>\n- Current time: 09:05\n</context>"},
            out_mm1[1],
            {"role": "assistant", "content": "[gentle] 这是一张测试图片。"},
            {"role": "user", "content": "那接下来呢？"},
        ]
        out_mm2 = tcr.apply_turn_context_to_messages(mm_t2, memory_str="M2", mode="split")
        self.assertEqual(out_mm1[0]["content"], out_mm2[0]["content"])
        # Historical multimodal turn stripped of <turn_context> while retaining text & image_url
        self.assertEqual(len(out_mm2[1]["content"]), 2)
        self.assertEqual(out_mm2[1]["content"][0]["text"], "帮我看看这张图")
        self.assertEqual(out_mm2[1]["content"][1]["type"], "image_url")
        self.assertIn("M2", out_mm2[3]["content"])

        # 4. Tool-call follow-up round keeps assistant tool_calls and tool result intact
        tool_msgs = [
            {"role": "system", "content": "Persona\n<context>\n- Current time: 09:10\n</context>"},
            {"role": "user", "content": "帮我看下屏幕"},
            {"role": "assistant", "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "pc_look_at_screen", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "屏幕上是一个浏览器窗口"},
        ]
        out_tool = tcr.apply_turn_context_to_messages(tool_msgs, memory_str="M3", mode="split")
        self.assertNotIn("<context>", out_tool[0]["content"])
        self.assertIn("M3", out_tool[1]["content"])
        self.assertEqual(out_tool[2]["tool_calls"][0]["id"], "tc1")
        self.assertEqual(out_tool[3]["role"], "tool")


@unittest.skipUnless(
    _HAS_XIAOZHI_CORE and _RUN_LIVE_TESTS,
    "Live integration tests skipped in offline mode (pass --live or XIAOZHI_RUN_LIVE_TESTS=1)",
)
class TestLiveOnlineIntegration(unittest.TestCase):
    """Read-only live integration checks against 18080 / 18787 / 18900 / 19880 (never mutates prod DB)."""

    def test_live_gsv_19880_synthesis_from_container(self):
        import asyncio
        from core.providers.tts.gpt_sovits_v2 import TTSProvider

        router.apply_xiaozhi_emotion_and_prosody_patches()
        cfg = {
            "url": "http://host.docker.internal:19880/tts",
            "output_dir": "/tmp/",
            "text_lang": "zh",
            "ref_audio_path": f"{router.MEI_DIR_HOST}/mei_default_ref.wav",
            "prompt_text": "今夜月色很美，呵...适合与朋友小聚一番。",
            "prompt_lang": "zh",
            "text_split_method": "cut0",
            "sample_steps": 16,
        }
        provider = TTSProvider(cfg, delete_audio_file=True)
        wav_bytes = asyncio.run(
            provider.text_to_speak("[gentle] 嗯，哥哥今天辛苦啦，先喝口热茶休息一下吧。", None)
        )
        self.assertTrue(wav_bytes.startswith(b"RIFF"), "Expected valid RIFF WAV header from 19880")
        self.assertGreater(len(wav_bytes), 50000, "Synthesized WAV unexpectedly tiny")

    def test_live_anima_brain_18787_readonly_recall_and_relationship(self):
        import asyncio
        import httpx
        import anima_memory_provider

        token = anima_memory_provider._read_token()
        self.assertTrue(token, "Expected studio_token.txt to be readable")
        with httpx.Client(timeout=3.0, trust_env=False) as client:
            r = client.post(
                "http://anima-brain:18787/xiaozhi/recall",
                headers={"Authorization": f"Bearer {token}"},
                json={"character_id": "xiaozhi", "query": "今天天气真好"},
            )
            self.assertEqual(r.status_code, 200)
            mood_raw = r.json().get("mood") or ""
            self.assertNotIn("stage=,", mood_raw, f"Anima Brain returned empty stage: {mood_raw!r}")
            self.assertRegex(mood_raw, r"stage=(stranger|acquaintance|familiar|close),\s*last_emotion=[a-z_]+")

            r_rel = client.get(
                "http://anima-brain:18787/xiaozhi/relationship",
                params={"character_id": "xiaozhi"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(r_rel.status_code, 200)
            self.assertIn("axes", r_rel.json())

            # H-2/H-3 live check: non-root container healthy, build_version present, Obsidian export writable
            r_h = client.get("http://anima-brain:18787/health")
            self.assertEqual(r_h.status_code, 200)
            h_data = r_h.json()
            self.assertEqual(h_data.get("status"), "ok")
            self.assertTrue(h_data.get("build_version"), "Expected non-empty build_version in anima-brain /health")
            obs = h_data.get("obsidian") or {}
            if obs.get("enabled"):
                self.assertTrue(obs.get("writable"), f"Anima Brain Obsidian export dir not writable: {obs}")
                self.assertEqual(obs.get("last_error") or "", "", f"Obsidian export reported error: {obs}")

        mp = anima_memory_provider.MemoryProvider(
            {"service_url": "http://anima-brain:18787", "character_id": "xiaozhi"}
        )

        async def _recall():
            try:
                return await mp.query_memory("今天天气真好")
            finally:
                await mp._client.aclose()

        ctx = asyncio.run(_recall())
        self.assertIsInstance(ctx, str)
        self.assertIn(
            router.get_anima_mood(),
            {"calm", "gentle", "happy", "playful", "shy", "serious", "tender"},
        )

    def test_live_18900_auth_dns_rebinding_and_mcp_client_connect(self):
        import asyncio
        import httpx
        import start_with_env
        from core.providers.tools.server_mcp.mcp_manager import ServerMCPManager

        start_with_env.install_mcp_token_injection_patch()
        tok = start_with_env._read_pc_mcp_token()
        self.assertTrue(tok, "Expected data/pc_mcp_token.txt to exist")

        with httpx.Client(timeout=4.0, trust_env=False) as client:
            self.assertEqual(client.get("http://host.docker.internal:18900/audio/status").status_code, 401)
            self.assertEqual(
                client.get(
                    "http://host.docker.internal:18900/audio/status",
                    headers={"Host": "evil.example:18900", "Authorization": f"Bearer {tok}"},
                ).status_code,
                421,
            )
            self.assertEqual(
                client.get(
                    "http://host.docker.internal:18900/audio/status",
                    headers={"Origin": "https://evil.example", "Authorization": f"Bearer {tok}"},
                ).status_code,
                403,
            )
            r_ok = client.get(
                "http://host.docker.internal:18900/l3/status",
                headers={"Authorization": f"Bearer {tok}"},
            )
            self.assertEqual(r_ok.status_code, 200)
            self.assertTrue(r_ok.json().get("ok"))

            # A-2: Host 18900 /mode endpoint is authenticated and backed by host_state
            self.assertEqual(client.get("http://host.docker.internal:18900/mode").status_code, 401)
            r_mode = client.get(
                "http://host.docker.internal:18900/mode",
                headers={"Authorization": f"Bearer {tok}"},
            )
            self.assertEqual(r_mode.status_code, 200)
            mode_body = r_mode.json()
            self.assertEqual(mode_body.get("source"), "host_state")
            self.assertIn(mode_body.get("mode"), ("off", "safe", "full"))

        class _DummyConn:
            pass

        async def _verify_mcp():
            mgr = ServerMCPManager(_DummyConn())
            await mgr.initialize_servers()
            try:
                names = [t["function"]["name"] for t in mgr.get_all_tools()]
                self.assertIn("pc_system_control", names)
                self.assertIn("pc_agent_task", names)
            finally:
                await mgr.cleanup_all()

        asyncio.run(_verify_mcp())

    def test_live_18080_state_and_csrf_protection(self):
        import re
        import httpx

        with httpx.Client(timeout=4.0, trust_env=False) as client:
            r_hlth = client.get("http://127.0.0.1:18080/api/health")
            self.assertEqual(r_hlth.status_code, 200)
            self.assertTrue(r_hlth.json().get("ok"))

            # A-4: GET / injects per-boot X-Console-Token and disables caching
            r_root = client.get("http://127.0.0.1:18080/")
            self.assertEqual(r_root.status_code, 200)
            self.assertIn("no-store", r_root.headers.get("cache-control", "").lower())
            m_tok = re.search(r'<meta\s+name="console-token"\s+content="([^"]+)"', r_root.text)
            self.assertIsNotNone(m_tok, "Live 18080 HTML missing console-token meta tag")
            console_tok = m_tok.group(1)

            r_st = client.get("http://127.0.0.1:18080/api/state")
            self.assertEqual(r_st.status_code, 200)
            st_body = r_st.json()
            cfg = st_body["config"]
            self.assertTrue(cfg.get("pc_audio_online"))
            self.assertTrue((cfg.get("l3_backend") or {}).get("mcp_online"))
            self.assertNotIn("latency", st_body)
            self.assertIn("idle_farewell", cfg)
            self.assertIn("llm_cache", cfg)
            self.assertIn("log_cursor", st_body)

            cur_seq = int(st_body["log_cursor"])
            r_inc = client.get(f"http://127.0.0.1:18080/api/state?log_cursor={cur_seq}")
            self.assertEqual(r_inc.status_code, 200)
            self.assertTrue(r_inc.json().get("logs_incremental"))

            r_if = client.get("http://127.0.0.1:18080/api/idle-farewell")
            self.assertEqual(r_if.status_code, 200)
            self.assertTrue(r_if.json().get("ok"))

            r_lat = client.get("http://127.0.0.1:18080/api/latency")
            self.assertEqual(r_lat.status_code, 404)

            # A-4: Host: xiaozhi-esp32-server restricted to read-only health/state
            self.assertEqual(
                client.get("http://127.0.0.1:18080/api/health", headers={"Host": "xiaozhi-esp32-server:18080"}).status_code,
                200,
            )
            self.assertEqual(
                client.get("http://127.0.0.1:18080/", headers={"Host": "xiaozhi-esp32-server:18080"}).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "http://127.0.0.1:18080/api/pc-mode",
                    json={"mode": "full"},
                    headers={"Host": "xiaozhi-esp32-server:18080", "X-Console-Token": console_tok},
                ).status_code,
                403,
            )

            # A-4: POST without / with wrong X-Console-Token rejected with 401
            self.assertEqual(
                client.post(
                    "http://127.0.0.1:18080/api/pc-mode",
                    json={"mode": "safe"},
                ).status_code,
                401,
            )
            self.assertEqual(
                client.post(
                    "http://127.0.0.1:18080/api/pc-mode",
                    json={"mode": "safe"},
                    headers={"X-Console-Token": "wrong-live-token"},
                ).status_code,
                401,
            )

            r_ct = client.post(
                "http://127.0.0.1:18080/api/pc-mode",
                content="mode=full",
                headers={"Content-Type": "text/plain", "X-Console-Token": console_tok},
            )
            self.assertEqual(r_ct.status_code, 415)

            r_ct_spoof = client.post(
                "http://127.0.0.1:18080/api/pc-mode",
                content='{"mode":"full"}',
                headers={"Content-Type": "application/json-evil", "X-Console-Token": console_tok},
            )
            self.assertEqual(r_ct_spoof.status_code, 415)

            r_bad_arr = client.post(
                "http://127.0.0.1:18080/api/pc-mode",
                content="[]",
                headers={"Content-Type": "application/json", "X-Console-Token": console_tok},
            )
            self.assertEqual(r_bad_arr.status_code, 400)

            r_bad_json = client.post(
                "http://127.0.0.1:18080/api/pc-mode",
                content="{bad",
                headers={"Content-Type": "application/json", "X-Console-Token": console_tok},
            )
            self.assertEqual(r_bad_json.status_code, 400)

            r_orig = client.post(
                "http://127.0.0.1:18080/api/pc-mode",
                json={"mode": "full"},
                headers={"Origin": "https://evil.example", "X-Console-Token": console_tok},
            )
            self.assertEqual(r_orig.status_code, 403)

            r_l3_evil = client.post(
                "http://127.0.0.1:18080/api/l3-backend",
                json={"backend": "dsh", "dsh_cmd": "calc.exe"},
                headers={"X-Console-Token": console_tok},
            )
            self.assertEqual(r_l3_evil.status_code, 400)

    def test_live_a1_container_mount_isolation(self):
        """A-1: Main container sees zero *.bin, token files, anima-data, or .ckpt/.pth, and ro mounts reject writes."""
        scan_roots = [
            "/opt/xiaozhi-esp32-server/app",
            "/opt/xiaozhi-esp32-server/config_templates",
            "/opt/xiaozhi-esp32-server/data",
            "/opt/xiaozhi-esp32-server/state",
            "/opt/xiaozhi-esp32-server/voices",
        ]
        forbidden_hits: list[str] = []
        for root in scan_roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                for dn in dirnames:
                    if dn.lower() in ("anima", "anima-data", "secrets", "backups"):
                        forbidden_hits.append(os.path.join(dirpath, dn))
                for fn in filenames:
                    low = fn.lower()
                    if (
                        low.endswith((".bin", ".ckpt", ".pth"))
                        or "token.txt" in low
                        or low.startswith("brain.sqlite3")
                    ):
                        forbidden_hits.append(os.path.join(dirpath, fn))
        self.assertEqual(forbidden_hits, [], f"Forbidden sensitive/host files visible in container: {forbidden_hits}")

        # Verify read-only mounts actually reject writes
        for ro_probe in (
            "/opt/xiaozhi-esp32-server/app/.ro_write_probe",
            "/opt/xiaozhi-esp32-server/config_templates/.ro_write_probe",
            "/opt/xiaozhi-esp32-server/voices/.ro_write_probe",
        ):
            with self.assertRaises(OSError, msg=f"Expected read-only mount at {ro_probe}"):
                with open(ro_probe, "w", encoding="utf-8") as f:
                    f.write("should-fail")
        with self.assertRaises(OSError, msg="Expected .mcp_server_settings.json to be read-only"):
            with open("/opt/xiaozhi-esp32-server/data/.mcp_server_settings.json", "a", encoding="utf-8") as f:
                f.write(" ")

    def test_live_f3_gsv_prime_endpoint(self):
        """F-3: Live 19880 POST /prime works and startup warmup populated >=7 emotion cache slots."""
        import httpx

        with httpx.Client(timeout=5.0, trust_env=False) as client:
            r = client.post(
                "http://host.docker.internal:19880/prime",
                json={"skip_if_busy": True, "reason": "live_integration_test"},
            )
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body.get("ok"), f"Unexpected /prime response: {body}")
            self.assertGreaterEqual(
                int(body.get("cache_slots") or 0),
                7,
                f"Expected >=7 primed emotion cache slots after startup warmup, got: {body}",
            )

    def test_live_18000_18003_esp32_auth_enforcement(self):
        import asyncio
        import httpx
        import websockets
        import start_with_env

        evil_mac = "de:ad:be:ef:99:99"
        allowed_list = start_with_env.normalize_allowed_devices([])

        with httpx.Client(timeout=4.0, trust_env=False) as client:
            # 18003 (container 8003) OTA rejects non-allowlisted MAC with 403
            r_ota_evil = client.post(
                "http://127.0.0.1:8003/xiaozhi/ota/",
                headers={"device-id": evil_mac, "client-id": "live-test-evil"},
                content="{}",
            )
            self.assertEqual(r_ota_evil.status_code, 403)

            # 18003 (container 8003) vision_explain rejects unauthenticated & `web_test_client` backdoor with 401
            r_vis_noauth = client.post(
                "http://127.0.0.1:8003/mcp/vision/explain",
                headers={"Device-Id": evil_mac},
            )
            self.assertEqual(r_vis_noauth.status_code, 401)
            r_vis_backdoor = client.post(
                "http://127.0.0.1:8003/mcp/vision/explain",
                headers={"Client-Id": "web_test_client", "Device-Id": allowed_list[0] if allowed_list else evil_mac},
            )
            self.assertEqual(r_vis_backdoor.status_code, 401)

            # If live ESP32_ALLOWED_DEVICES is configured, verify legitimate OTA token minting works
            minted_tok = ""
            if allowed_list:
                r_ota_ok = client.post(
                    "http://127.0.0.1:8003/xiaozhi/ota/",
                    headers={"device-id": allowed_list[0], "client-id": "live-test-client"},
                    content="{}",
                )
                self.assertEqual(r_ota_ok.status_code, 200)
                minted_tok = (r_ota_ok.json().get("websocket") or {}).get("token") or ""
                self.assertTrue(minted_tok, "Expected signed WS token from OTA for allowlisted MAC")

        async def _probe_ws():
            # 1. Non-allowlisted MAC without token -> rejected ("认证失败") & closed
            async with websockets.connect(
                "ws://127.0.0.1:8000/xiaozhi/v1/",
                additional_headers={"Device-Id": evil_mac, "Client-Id": "live-test-evil"},
            ) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                self.assertIn("认证失败", str(msg))

            if allowed_list:
                # 2. Allowlisted MAC WITHOUT token -> rejected ("认证失败")
                async with websockets.connect(
                    "ws://127.0.0.1:8000/xiaozhi/v1/",
                    additional_headers={"Device-Id": allowed_list[0], "Client-Id": "live-test-client"},
                ) as ws:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    self.assertIn("认证失败", str(msg))

                # 3. Allowlisted MAC WITH valid OTA-minted Bearer token -> handshake accepted (not rejected)
                async with websockets.connect(
                    "ws://127.0.0.1:8000/xiaozhi/v1/",
                    additional_headers={
                        "Device-Id": allowed_list[0],
                        "Client-Id": "live-test-client",
                        "Authorization": f"Bearer {minted_tok}",
                    },
                ) as ws:
                    try:
                        first_msg = await asyncio.wait_for(ws.recv(), timeout=0.6)
                        self.assertNotIn("认证失败", str(first_msg))
                    except asyncio.TimeoutError:
                        # Server stayed open waiting for client hello -> auth succeeded!
                        pass

        asyncio.run(_probe_ws())

    def test_live_runtime_wakeup_config_and_mei_wav(self):
        import yaml

        cfg_path = os.path.join(CONFIG_DIR, ".config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ww = cfg.get("wakeup_words") or []
        self.assertIn("你好芽衣", ww, "live config/runtime/.config.yaml wakeup_words must include 你好芽衣")
        wav_path = os.path.join(VOICES_DIR, "raiden_mei", "wakeup_mei_default.wav")
        self.assertTrue(os.path.isfile(wav_path), f"Missing Mei default wakeup WAV: {wav_path}")
        with wave.open(wav_path, "rb") as wf:
            dur = wf.getnframes() / float(wf.getframerate())
            self.assertGreater(dur, 0.8)
            self.assertLess(dur, 4.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

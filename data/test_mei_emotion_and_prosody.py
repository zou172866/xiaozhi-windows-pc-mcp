"""Unit & integration tests for Raiden Mei multi-emotion routing and prosodic clause segmentation."""

import os
import sys
import unittest
import wave

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
if DATA_DIR not in sys.path:
    sys.path.insert(0, DATA_DIR)

import mei_emotion_router as router


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


class TestMeiEmotionAndProsody(unittest.TestCase):
    def test_all_seven_emotion_wavs_exist_and_valid_duration(self):
        emotions = router.load_mei_emotions(force_reload=True)
        expected = {"calm", "gentle", "happy", "playful", "shy", "serious", "tender"}
        self.assertTrue(expected.issubset(set(emotions.keys())))
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
            self.assertTrue(prof.get("prompt_text", "").strip(), f"Empty prompt_text for {key}")
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

        # Leaked XML tool-call closing tags from LLM (e.g. </parameter>, </invoke>) must be stripped
        self.assertEqual(router.clean_segment_for_tts("</parameter>"), "")
        self.assertEqual(router.clean_segment_for_tts("哪怕是很小很小的事，我也愿意听。</parameter>"), "哪怕是很小很小的事，我也愿意听。")
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
            "ref_audio_path": "e:/故事/1/xiaozhi-server/data/voices/raiden_mei/mei_default_ref.wav",
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
        with patch.object(sess, "post", side_effect=_fake_post):
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
        # Verify 1st sentence routed to mei_gentle_ref.wav + cut0 + V4 sample_steps=16 + no tags
        self.assertTrue(captured_payloads[0]["ref_audio_path"].endswith("mei_gentle_ref.wav"))
        self.assertEqual(captured_payloads[0]["text_split_method"], "cut0")
        self.assertEqual(captured_payloads[0]["sample_steps"], 16)
        self.assertEqual(captured_payloads[0]["text"], "哥哥，今天工作累不累？")
        # Verify 2nd sentence shifted to mei_playful_ref.wav + cut0 + exclamation preserved
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
        from unittest.mock import patch, MagicMock
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
                "ref_audio_path": "e:/故事/1/xiaozhi-server/data/voices/raiden_mei/mei_default_ref.wav",
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

            with patch.object(router._get_gsv_http_session(), "post", return_value=_FakeResp()):
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

        cfg_path = os.path.join(DATA_DIR, ".config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ww = cfg.get("wakeup_words") or []
        self.assertIn("你好芽衣", ww, "data/.config.yaml wakeup_words must include 你好芽衣")

        wav_path = os.path.join(DATA_DIR, "voices", "raiden_mei", "wakeup_mei_default.wav")
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

        cfg_path = os.path.join(DATA_DIR, ".config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        iw = cfg.get("interrupt_words") or []
        self.assertIn("听我说", iw, "data/.config.yaml interrupt_words must include 听我说")

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
        import json
        import tempfile
        from unittest.mock import patch
        import console_server as cs

        # 1. Verify no hardcoded personal MAC / LAN IP in console_server defaults
        with open(os.path.join(DATA_DIR, "console_server.py"), "r", encoding="utf-8") as _f:
            src = _f.read()
        self.assertNotIn("cc:68:c7:a8:19:dc", src)
        self.assertNotIn("192.168.31.200", src)
        self.assertNotIn("e:/故事/1", src)

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

        # Same-origin JSON POST passes
        ok_good, st_good, _ = cs.validate_console_request(
            "POST",
            {
                "Host": "localhost:18080",
                "Origin": "http://localhost:18080",
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        self.assertTrue(ok_good)
        self.assertEqual(st_good, 200)

        # 2. Log secret redaction
        red = cs._redact_secrets(
            "Authorization: Bearer abcdef1234567890 #token=mysecret sk-1234567890abcdef AIzaSyA12345678901234567890"
        )
        self.assertNotIn("abcdef1234567890", red)
        self.assertNotIn("mysecret", red)
        self.assertNotIn("sk-1234567890abcdef", red)
        self.assertNotIn("AIzaSyA12345678901234567890", red)

        # 3. Verify console.html sends application/json on clear-history and pauses polling on visibilitychange
        with open(os.path.join(DATA_DIR, "console.html"), "r", encoding="utf-8") as f:
            html_src = f.read()
        self.assertIn("visibilitychange", html_src)
        self.assertIn("document.hidden", html_src)
        self.assertNotIn('fetch("/api/clear-history", { method: "POST" })', html_src)

    def test_pc_audio_router_single_wait_dynamic_poll_queue_lock_and_bearer_auth(self):
        import asyncio
        import tempfile
        from unittest.mock import AsyncMock, MagicMock, patch
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
        cfg_path = os.path.join(DATA_DIR, ".config.example.yaml")
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
        import httpx

        with httpx.Client(timeout=4.0, trust_env=False) as client:
            r_hlth = client.get("http://127.0.0.1:18080/api/health")
            self.assertEqual(r_hlth.status_code, 200)
            self.assertTrue(r_hlth.json().get("ok"))

            r_st = client.get("http://127.0.0.1:18080/api/state")
            self.assertEqual(r_st.status_code, 200)
            cfg = r_st.json()["config"]
            self.assertTrue(cfg.get("pc_audio_online"))
            self.assertTrue((cfg.get("l3_backend") or {}).get("mcp_online"))

            r_ct = client.post(
                "http://127.0.0.1:18080/api/pc-mode",
                content="mode=full",
                headers={"Content-Type": "text/plain"},
            )
            self.assertEqual(r_ct.status_code, 415)

            r_ct_spoof = client.post(
                "http://127.0.0.1:18080/api/pc-mode",
                content='{"mode":"full"}',
                headers={"Content-Type": "application/json-evil"},
            )
            self.assertEqual(r_ct_spoof.status_code, 415)

            r_bad_arr = client.post(
                "http://127.0.0.1:18080/api/pc-mode",
                content="[]",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(r_bad_arr.status_code, 400)

            r_bad_json = client.post(
                "http://127.0.0.1:18080/api/pc-mode",
                content="{bad",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(r_bad_json.status_code, 400)

            r_orig = client.post(
                "http://127.0.0.1:18080/api/pc-mode",
                json={"mode": "full"},
                headers={"Origin": "https://evil.example"},
            )
            self.assertEqual(r_orig.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)

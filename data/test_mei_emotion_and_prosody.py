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


@unittest.skipUnless(_HAS_XIAOZHI_CORE, "Requires xiaozhi-esp32-server container environment")
class TestLiveContainerTTSPipeline(unittest.TestCase):
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

        emitted_segments = []
        with patch("requests.post", side_effect=_fake_post):
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
        # Verify 1st sentence routed to mei_gentle_ref.wav + cut0 + no tags
        self.assertTrue(captured_payloads[0]["ref_audio_path"].endswith("mei_gentle_ref.wav"))
        self.assertEqual(captured_payloads[0]["text_split_method"], "cut0")
        self.assertEqual(captured_payloads[0]["text"], "哥哥，今天工作累不累？")
        # Verify 2nd sentence shifted to mei_playful_ref.wav + cut0 + exclamation preserved
        self.assertTrue(captured_payloads[1]["ref_audio_path"].endswith("mei_playful_ref.wav"))
        self.assertEqual(captured_payloads[1]["text_split_method"], "cut0")
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

    def test_real_gsv_19880_synthesis_from_container(self):
        import asyncio
        from core.providers.tts.gpt_sovits_v2 import TTSProvider

        router.apply_xiaozhi_emotion_and_prosody_patches()
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
        wav_bytes = asyncio.run(
            provider.text_to_speak("[gentle] 嗯，哥哥今天辛苦啦，先喝口热茶休息一下吧。", None)
        )
        self.assertTrue(wav_bytes.startswith(b"RIFF"), "Expected valid RIFF WAV header from 19880")
        self.assertGreater(len(wav_bytes), 50000, "Synthesized WAV unexpectedly tiny")

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

            with patch("requests.post", return_value=_FakeResp()):
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

    def test_live_anima_brain_18787_recall_real_last_emotion(self):
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
        # Must NOT be the broken empty `stage=, last_emotion=calm`
        self.assertNotIn("stage=,", mood_raw, f"Anima Brain returned empty stage: {mood_raw!r}")
        self.assertRegex(mood_raw, r"stage=(stranger|acquaintance|familiar|close),\s*last_emotion=[a-z_]+")

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

    def test_reply_length_hot_config_and_prompt_injection(self):
        import reply_length_router as rl

        orig = rl.read_reply_length_config()
        try:
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
        finally:
            rl.write_reply_length_config(chat_chars=orig["chat_chars"], story_chars=orig["story_chars"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Offline unit tests for Cloud Audio API integration, low-spec memory optimization,
streaming TTS 48kHz/44.1kHz PC speaker passthrough, and 18080 Console routes.

Runs inside ephemeral `docker run --rm` (Layer 1 unit) with zero network or running-container dependency.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import socket
import sys
import tempfile
import threading
import types
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch
import wave

import httpx
import yaml

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


def _make_pcm16_bytes(sample_rate: int, duration_ms: int) -> bytes:
    n_samples = int(sample_rate * duration_ms / 1000.0)
    return b"\x01\x02" * n_samples


def _start_test_console():
    import console_server as cs

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), cs.ConsoleHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _post_headers(port: int, token: str) -> dict:
    return {
        "Host": f"127.0.0.1:{port}",
        "Origin": f"http://127.0.0.1:{port}",
        "X-Console-Token": token,
    }


class TestCloudAudioRuntimePatches(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import boot

        boot.install_all(start_background_workers=False, run_contract_self_check=True)

    def test_01_modelscope_unconditional_lazy_stub_without_local_sherpa(self):
        """Low-spec Cloud-ASR PCs without local Sherpa model files must still get modelscope lazy-stub."""
        from patches import asr as asr_patch

        saved = {k: sys.modules.pop(k, None) for k in ("modelscope", "modelscope.hub", "modelscope.hub.file_download")}
        orig_root = asr_patch.ROOT_DIR
        try:
            with tempfile.TemporaryDirectory() as empty_root:
                asr_patch.ROOT_DIR = empty_root
                asr_patch.install_memory_optimizations(start_trimmer=False)
                self.assertIn("modelscope.hub.file_download", sys.modules)
                dl_mod = sys.modules["modelscope.hub.file_download"]
                self.assertEqual(dl_mod.model_file_download.__name__, "_lazy_model_file_download")
                # Real modelscope package has __file__; our zero-cost stub does not
                self.assertFalse(hasattr(sys.modules["modelscope"], "__file__"))
        finally:
            asr_patch.ROOT_DIR = orig_root
            for k in ("modelscope", "modelscope.hub", "modelscope.hub.file_download"):
                sys.modules.pop(k, None)
                if saved[k] is not None:
                    sys.modules[k] = saved[k]

    def test_02_openai_asr_async_thread_timeout_and_placeholder_guard(self):
        from core.providers.asr.openai import ASRProvider as OpenaiASR
        from patches import cloud_audio

        with tempfile.TemporaryDirectory() as td:
            wav_path = os.path.join(td, "sample.wav")
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00" * 1600)

            artifacts = types.SimpleNamespace(file_path=wav_path)
            provider = OpenaiASR(
                {
                    "api_key": "sk-test-secret-key-99999999",
                    "base_url": "https://api.siliconflow.cn/v1/audio/transcriptions",
                    "model_name": "FunAudioLLM/SenseVoiceSmall",
                    "output_dir": td,
                },
                delete_audio_file=True,
            )

            captured = {}

            class _FakeResp:
                status_code = 200
                text = '{"text": "你好小智"}'

                def json(self):
                    return {"text": "你好小智"}

            def _fake_post(url, files=None, data=None, headers=None, timeout=None):
                captured["thread_id"] = threading.get_ident()
                captured["timeout"] = timeout
                captured["url"] = url
                captured["model"] = (data or {}).get("model")
                return _FakeResp()

            main_tid = threading.get_ident()
            fake_sess = MagicMock()
            fake_sess.post.side_effect = _fake_post

            with patch.object(cloud_audio, "_get_cloud_http_session", return_value=fake_sess):
                text, fp = asyncio.run(provider.speech_to_text([], "sess1", artifacts=artifacts))

            self.assertEqual(text, "你好小智")
            self.assertEqual(fp, wav_path)
            self.assertNotEqual(captured["thread_id"], main_tid, "OpenaiASR must offload blocking HTTP off event loop")
            self.assertGreaterEqual(float(captured["timeout"]), 3.0)
            self.assertEqual(captured["model"], "FunAudioLLM/SenseVoiceSmall")

            # Unexpanded placeholder must fail fast without firing HTTP request
            bad_prov = OpenaiASR(
                {
                    "api_key": "${SILICONFLOW_API_KEY}",
                    "base_url": "https://api.siliconflow.cn/v1/audio/transcriptions",
                    "model_name": "FunAudioLLM/SenseVoiceSmall",
                    "output_dir": td,
                },
                delete_audio_file=True,
            )
            fake_sess.post.reset_mock()
            with patch.object(cloud_audio, "_get_cloud_http_session", return_value=fake_sess):
                text2, _ = asyncio.run(bad_prov.speech_to_text([], "sess2", artifacts=artifacts))
            self.assertEqual(text2, "")
            fake_sess.post.assert_not_called()

    def test_03_qwen3_asr_offloads_off_event_loop(self):
        import core.providers.asr.qwen3_asr_flash as qwen_mod

        with tempfile.TemporaryDirectory() as td:
            prov = qwen_mod.ASRProvider(
                {
                    "api_key": "sk-dashscope-test-1234567890",
                    "model_name": "qwen3-asr-flash",
                    "output_dir": td,
                },
                delete_audio_file=True,
            )
            call_tids = []

            def _fake_call(**kwargs):
                call_tids.append(threading.get_ident())
                yield {"output": {"choices": [{"message": types.SimpleNamespace(content=[{"text": "测试百炼"}])}]}}

            main_tid = threading.get_ident()
            artifacts = types.SimpleNamespace(temp_path="/tmp/fake.wav", file_path="/tmp/fake.wav")
            with patch.object(qwen_mod.dashscope.MultiModalConversation, "call", side_effect=_fake_call):
                text, _ = asyncio.run(prov.speech_to_text([], "s1", artifacts=artifacts))

            self.assertEqual(text, "测试百炼")
            self.assertTrue(call_tids)
            self.assertNotEqual(call_tids[0], main_tid)

    def test_04_doubao_tts_safe_init_and_siliconflow_speed_and_fish_ref_cleanup(self):
        from core.providers.tts.doubao import TTSProvider as DoubaoTTS
        from core.providers.tts.siliconflow import TTSProvider as SiliconFlowTTS
        from core.providers.tts.fishspeech import TTSProvider as FishSpeechTTS
        from patches import cloud_audio

        # 1. DoubaoTTS must NOT crash with ValueError when appid is unexpanded placeholder
        with patch.dict(os.environ, {"DOUBAO_TTS_APPID": "", "DOUBAO_APPID": ""}, clear=False):
            db = DoubaoTTS(
                {
                    "appid": "${DOUBAO_TTS_APPID}",
                    "access_token": "${DOUBAO_TTS_ACCESS_TOKEN}",
                    "api_url": "https://openspeech.bytedance.com/api/v1/tts",
                    "cluster": "volcano_tts",
                    "voice": "BV001_streaming",
                    "output_dir": "tmp/",
                },
                delete_audio_file=True,
            )
            self.assertEqual(db.appid, "${DOUBAO_TTS_APPID}")

        # Fallback to unified DOUBAO_APPID / DOUBAO_ACCESS_TOKEN
        with patch.dict(os.environ, {"DOUBAO_APPID": "123456", "DOUBAO_ACCESS_TOKEN": "tok-abc123456"}, clear=False):
            db2 = DoubaoTTS(
                {
                    "appid": "${DOUBAO_TTS_APPID}",
                    "access_token": "${DOUBAO_TTS_ACCESS_TOKEN}",
                    "api_url": "https://openspeech.bytedance.com/api/v1/tts",
                    "cluster": "volcano_tts",
                    "voice": "BV001_streaming",
                    "output_dir": "tmp/",
                },
                delete_audio_file=True,
            )
            self.assertEqual(str(db2.appid), "123456")
            self.assertEqual(db2.access_token, "tok-abc123456")

        # 2. SiliconFlowTTS forwards speed and runs off event loop
        sf = SiliconFlowTTS(
            {
                "model": "FunAudioLLM/CosyVoice2-0.5B",
                "voice": "FunAudioLLM/CosyVoice2-0.5B:anna",
                "access_token": "sk-sf-valid-key-1234567890",
                "response_format": "wav",
                "speed": 1.15,
                "output_dir": "tmp/",
            },
            delete_audio_file=True,
        )
        sf_seen = {}

        class _OkAudio:
            status_code = 200
            content = b"RIFFfakewav"
            text = ""

        def _sf_post(url, json=None, headers=None, timeout=None):
            sf_seen["tid"] = threading.get_ident()
            sf_seen["json"] = json
            return _OkAudio()

        fake_sess = MagicMock()
        fake_sess.post.side_effect = _sf_post
        with patch.object(cloud_audio, "_get_cloud_http_session", return_value=fake_sess):
            out = asyncio.run(sf.text_to_speak("你好", None))
        self.assertEqual(out, b"RIFFfakewav")
        self.assertAlmostEqual(sf_seen["json"]["speed"], 1.15, places=2)
        self.assertNotEqual(sf_seen["tid"], threading.get_ident())

        # 3. FishSpeech omits upstream default wakeup_words.wav when cloud reference_id is set
        fish = FishSpeechTTS(
            {
                "api_key": "fish-key-1234567890abcdef",
                "api_url": "https://api.fish.audio/v1/tts",
                "reference_id": "model-id-abc123",
                "reference_audio": ["config/assets/wakeup_words.wav"],
                "reference_text": ["默认台词"],
                "response_format": "wav",
                "output_dir": "tmp/",
            },
            delete_audio_file=True,
        )
        import ormsgpack

        fish_seen = {}

        def _fish_post(url, data=None, headers=None, timeout=None):
            fish_seen["payload"] = ormsgpack.unpackb(data)
            return _OkAudio()

        fake_sess2 = MagicMock()
        fake_sess2.post.side_effect = _fish_post
        with patch.object(cloud_audio, "_get_cloud_http_session", return_value=fake_sess2):
            out_f = asyncio.run(fish.text_to_speak("测试音色", None))
        self.assertEqual(out_f, b"RIFFfakewav")
        self.assertEqual(fish_seen["payload"]["reference_id"], "model-id-abc123")
        self.assertEqual(fish_seen["payload"]["references"], [])

    def test_05_streaming_tts_emotion_tag_firewall(self):
        from patches import cloud_audio

        dummy = types.SimpleNamespace()
        # Split "[gentle] 你好呀" across token chunks
        self.assertEqual(cloud_audio.strip_streaming_emotion_chunk(dummy, "[gen", flush=False), "")
        self.assertEqual(cloud_audio.strip_streaming_emotion_chunk(dummy, "tle] 你好", flush=False), "你好")
        self.assertEqual(cloud_audio.strip_streaming_emotion_chunk(dummy, "呀 [hap", flush=False), "呀 ")
        self.assertEqual(cloud_audio.strip_streaming_emotion_chunk(dummy, "py] 太棒了", flush=False), "太棒了")
        self.assertEqual(cloud_audio.strip_streaming_emotion_chunk(dummy, "", flush=True), "")

    def test_06_streaming_pc_audio_passthrough_alibl_huoshan_minimax(self):
        """All 3 streaming cloud TTS engines must support lossless 48kHz/44.1kHz PC speaker passthrough."""
        from patches import pc_audio
        from core.providers.tts.dto.dto import SentenceType

        # Helper dummy encoder mimicking upstream OpusEncoderUtils
        class _FakeEncoder:
            def __init__(self, sample_rate=16000):
                self.sample_rate = sample_rate
                self.channels = 1
                self.frame_size_ms = 60
                self.orig_calls = 0

            def encode_pcm_to_opus_stream(self, pcm_data, end_of_stream=False, callback=None):
                self.orig_calls += 1
                if callback:
                    callback(b"REAL_OPUS")

        # 1. Sample rate resolution (48kHz for AliBL/Huoshan, 44.1kHz for MiniMax)
        self.assertEqual(pc_audio.resolve_streaming_pc_sample_rate("alibl_stream"), 48000)
        self.assertEqual(pc_audio.resolve_streaming_pc_sample_rate("huoshan_double_stream"), 48000)
        self.assertEqual(pc_audio.resolve_streaming_pc_sample_rate("minimax_httpstream"), 44100)

        # 2. _AliBLWSProxy upgrades run-task PCM sample_rate to 48000 in PC mode
        sent_msgs = []

        class _FakeWS:
            async def send(self, data):
                sent_msgs.append(data)

        class _WeakObj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        owner = _WeakObj(_pc_stream_ctx={"active": True, "sample_rate": 48000})
        proxy = pc_audio._AliBLWSProxy(_FakeWS(), owner)
        run_task = json.dumps(
            {
                "header": {"action": "run-task", "task_id": "t1"},
                "payload": {"parameters": {"format": "pcm", "sample_rate": 16000}},
            }
        )
        asyncio.run(proxy.send(run_task))
        rewritten = json.loads(sent_msgs[0])
        self.assertEqual(rewritten["payload"]["parameters"]["sample_rate"], 48000)

        # 3. End-to-end PCM -> WAV chunking + matched silent Opus frames + LAST flush + abort fence
        import queue

        played_wavs = []
        silent_pkts = []
        conn = _WeakObj(
            sentence_id="turn-1",
            sample_rate=16000,
            client_abort=False,
            loop=None,
        )
        tts_obj = _WeakObj(
            conn=conn,
            current_sentence_id="turn-1",
            tts_audio_queue=queue.Queue(),
            opus_encoder=_FakeEncoder(16000),
            handle_opus=lambda pkt: silent_pkts.append(pkt),
        )
        pc_audio._ensure_pc_stream_hooks(tts_obj)
        pc_audio._reset_pc_stream_ctx(tts_obj, active=True, sid="turn-1", sample_rate=48000, volume=0.85)

        def _fake_play_sync(wav_bytes, volume=None):
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                played_wavs.append((wf.getframerate(), wf.getnchannels(), wf.getsampwidth(), wf.getnframes(), volume))
            return True, "ok"

        with (
            patch.object(pc_audio, "play_wav_on_pc_sync", side_effect=_fake_play_sync),
            patch.object(pc_audio, "_get_silent_opus_frame", return_value=b"SILENT_60MS"),
        ):
            # Feed 200ms of 48kHz PCM (exceeds 160ms first_chunk_ms) -> flushes immediately
            tts_obj.opus_encoder.encode_pcm_to_opus_stream(
                _make_pcm16_bytes(48000, 200), end_of_stream=False, callback=tts_obj.handle_opus
            )
            # Feed 100ms tail -> buffered until SentenceType.LAST on _PCAwareAudioQueue
            tts_obj.opus_encoder.encode_pcm_to_opus_stream(
                _make_pcm16_bytes(48000, 100), end_of_stream=False, callback=tts_obj.handle_opus
            )
            self.assertEqual(len(played_wavs), 1)
            tts_obj.tts_audio_queue.put((SentenceType.LAST, [], None, "turn-1"))
            self.assertEqual(len(played_wavs), 2)
            # Total 300ms @ 48kHz -> 14400 samples, 5 silent 60ms Opus frames, original encoder NOT called
            self.assertEqual(sum(x[3] for x in played_wavs), 14400)
            self.assertEqual(played_wavs[0][0], 48000)
            self.assertAlmostEqual(played_wavs[0][4], 0.85, places=2)
            self.assertEqual(len(silent_pkts), 5)
            self.assertEqual(tts_obj.opus_encoder.orig_calls, 0)

            # Non-live callback (e.g. wakeup cache to_tts) must NOT be intercepted
            other_out = []
            tts_obj.opus_encoder.encode_pcm_to_opus_stream(
                _make_pcm16_bytes(16000, 60), end_of_stream=True, callback=other_out.append
            )
            self.assertEqual(other_out, [b"REAL_OPUS"])
            self.assertEqual(tts_obj.opus_encoder.orig_calls, 1)

            # Abort bumps _pc_audio_abort_gen and clears pending queue
            gen0 = conn._pc_audio_abort_gen
            pc_audio._clear_pc_stream_chunks(conn)
            self.assertEqual(conn._pc_audio_abort_gen, gen0 + 1)


class TestCloudAudioConsoleAPI(unittest.TestCase):
    def setUp(self):
        from console.routes import audio_api

        # Provider requests are mocked. Give URL validation deterministic DNS
        # answers too, without changing resolution for the local HTTP server.
        resolver = patch.object(audio_api, "socket", wraps=socket)
        self.validation_socket = resolver.start()
        self.addCleanup(resolver.stop)
        self.validation_socket.getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))
        ]

    def test_provider_resolving_to_private_network_is_rejected(self):
        from console.routes import audio_api

        self.validation_socket.getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.2", 443))
        ]
        with self.assertRaises(ValueError):
            audio_api.validate_safe_api_url("https://audio.example.com", allow_local_gateway=False)

    @classmethod
    def setUpClass(cls) -> None:
        import boot

        boot.install_all(start_background_workers=False, run_contract_self_check=True)

    def test_01_audio_providers_state_config_validation_and_zero_plaintext(self):
        import console_server as cs
        from console import shared

        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, ".config.yaml")
            initial_cfg = {
                "server": {"websocket": "ws://127.0.0.1:18000/xiaozhi/v1/"},
                "prompt": "你是小智，温和自然。",
                "selected_module": {"LLM": "DeepSeekLLM", "ASR": "SherpaASR", "TTS": "EdgeTTS"},
                "LLM": {"DeepSeekLLM": {"type": "openai", "model_name": "deepseek-flash", "api_key": "${DEEPSEEK_API_KEY}"}},
                "ASR": {"SherpaASR": {"type": "sherpa_onnx_local", "model_dir": "models/sherpa-onnx-sense-voice"}},
                "TTS": {"EdgeTTS": {"type": "edge", "voice": "zh-CN-XiaoxiaoNeural"}},
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(initial_cfg, f, allow_unicode=True, sort_keys=False)
            cs.invalidate_config_yaml_cache(cfg_path)

            httpd, port = _start_test_console()
            orig_port = cs.CONSOLE_PORT
            cs.CONSOLE_PORT = port
            try:
                tok = cs.get_console_token()
                hdrs = _post_headers(port, tok)
                with (
                    patch.object(cs, "CONFIG_PATH", cfg_path),
                    patch.dict(
                        os.environ,
                        {
                            "SILICONFLOW_API_KEY": "sk-sf-live-9876543210abcdef",
                            "DASHSCOPE_API_KEY": "sk-ds-live-9876543210abcdef",
                            "DOUBAO_APPID": "778899",
                            "DOUBAO_ACCESS_TOKEN": "db-tok-9876543210abcdef",
                        },
                        clear=False,
                    ),
                    patch.object(cs, "_sync_persona_to_anima"),
                ):
                    with httpx.Client(timeout=5.0, trust_env=False) as client:
                        # 1. GET /api/audio-providers returns catalogs & lowspec_presets with ZERO raw secrets
                        r_cat = client.get(f"http://127.0.0.1:{port}/api/audio-providers")
                        self.assertEqual(r_cat.status_code, 200)
                        cat = r_cat.json()
                        self.assertTrue(cat["ok"])
                        self.assertIn("SiliconFlowASR", cat["asr_providers"])
                        self.assertIn("AliBLTTS", cat["tts_providers"])
                        self.assertIn("HuoshanDoubleStreamTTS", cat["tts_providers"])
                        self.assertIn("MinimaxTTSHTTPStream", cat["tts_providers"])
                        self.assertTrue(cat["tts_providers"]["AliBLTTS"]["pc_audio_supported"])
                        self.assertTrue(cat["tts_providers"]["HuoshanDoubleStreamTTS"]["pc_audio_supported"])
                        self.assertTrue(cat["tts_providers"]["MinimaxTTSHTTPStream"]["pc_audio_supported"])
                        self.assertNotIn("sk-sf-live", r_cat.text)
                        self.assertNotIn("db-tok-987654", r_cat.text)

                        # 2. GET /api/state exposes audio_env_status & active ASR/TTS without secrets
                        r_st = client.get(f"http://127.0.0.1:{port}/api/state")
                        self.assertEqual(r_st.status_code, 200)
                        st_cfg = r_st.json()["config"]
                        self.assertEqual(st_cfg["asr_provider"], "SherpaASR")
                        self.assertEqual(st_cfg["tts_provider"], "EdgeTTS")
                        self.assertTrue(st_cfg["audio_env_status"]["siliconflow_key_ok"])
                        self.assertTrue(st_cfg["audio_env_status"]["dashscope_key_ok"])
                        self.assertTrue(st_cfg["audio_env_status"]["doubao_asr_ok"])
                        self.assertTrue(st_cfg["audio_env_status"]["doubao_tts_ok"])
                        self.assertNotIn("sk-sf-live", r_st.text)

                        # 3. Strict validation rejects secret injection, SSRF internal ports, bad schemes, invalid providers
                        bad_bodies = [
                            {"asr_provider": "UnknownASR"},
                            {"tts_provider": "UnknownTTS"},
                            {"asr_provider": "SiliconFlowASR", "asr_config": {"api_key": "sk-plaintext-leak"}},
                            {"tts_provider": "OpenAITTS", "tts_config": {"api_key": "sk-plaintext-leak"}},
                            {"asr_provider": "OpenaiASR", "asr_config": {"base_url": "http://127.0.0.1:18080/api/state"}},
                            {"asr_provider": "OpenaiASR", "asr_config": {"base_url": "http://anima-brain:18787/health"}},
                            {"tts_provider": "OpenAITTS", "tts_config": {"api_url": "http://host.docker.internal:18900/audio/play"}},
                            {"tts_provider": "OpenAITTS", "tts_config": {"api_url": "file:///etc/passwd"}},
                            {"tts_provider": "OpenAITTS", "tts_config": {"api_url": "https://user:pass@example.com/v1"}},
                            {"tts_provider": "CosyVoiceSiliconflow", "tts_config": {"speed": 9.9}},
                            {"tts_provider": "FishSpeech", "tts_config": {"response_format": "exe"}},
                        ]
                        for bb in bad_bodies:
                            rb = client.post(f"http://127.0.0.1:{port}/api/config", json=bb, headers=hdrs)
                            self.assertEqual(rb.status_code, 400, f"Expected 400 for {bb!r}, got {rb.status_code}: {rb.text}")

                        # 4. Valid cloud switch SherpaASR -> SiliconFlowASR + CosyVoiceSiliconflow
                        #    Crossing SherpaASR -> cloud ASR flags non-hot reload (restart to reclaim ~350MB RAM)
                        r_ok = client.post(
                            f"http://127.0.0.1:{port}/api/config",
                            json={
                                "asr_provider": "SiliconFlowASR",
                                "asr_config": {
                                    "base_url": "https://api.siliconflow.cn/v1/audio/transcriptions",
                                    "model_name": "FunAudioLLM/SenseVoiceSmall",
                                },
                                "tts_provider": "CosyVoiceSiliconflow",
                                "tts_config": {
                                    "model": "FunAudioLLM/CosyVoice2-0.5B",
                                    "voice": "FunAudioLLM/CosyVoice2-0.5B:claire",
                                    "speed": 1.05,
                                    "response_format": "wav",
                                },
                                "restart": False,
                            },
                            headers=hdrs,
                        )
                        self.assertEqual(r_ok.status_code, 200, r_ok.text)
                        d_ok = r_ok.json()
                        self.assertTrue(d_ok["ok"])
                        self.assertEqual(d_ok["detail"], "asr_local_cloud_boundary_changed")
                        self.assertFalse(d_ok["hot_reloaded"])

                        # Verify YAML persisted ONLY ${ENV_VAR} placeholders, zero plaintext keys
                        with open(cfg_path, encoding="utf-8") as _rf:
                            raw_yaml = _rf.read()
                        self.assertNotIn("sk-sf-live", raw_yaml)
                        saved = yaml.safe_load(raw_yaml)
                        self.assertEqual(saved["selected_module"]["ASR"], "SiliconFlowASR")
                        self.assertEqual(saved["selected_module"]["TTS"], "CosyVoiceSiliconflow")
                        self.assertEqual(saved["ASR"]["SiliconFlowASR"]["api_key"], "${SILICONFLOW_API_KEY}")
                        self.assertEqual(saved["TTS"]["CosyVoiceSiliconflow"]["access_token"], "${SILICONFLOW_API_KEY}")
                        self.assertEqual(saved["TTS"]["CosyVoiceSiliconflow"]["voice"], "FunAudioLLM/CosyVoice2-0.5B:claire")

                        # 5. MinimaxTTSHTTPStream writes speed under voice_setting.speed
                        r_mm = client.post(
                            f"http://127.0.0.1:{port}/api/config",
                            json={
                                "tts_provider": "MinimaxTTSHTTPStream",
                                "tts_config": {"model": "speech-02-turbo", "voice_id": "female-yujie", "speed": 1.1},
                                "restart": False,
                            },
                            headers=hdrs,
                        )
                        self.assertEqual(r_mm.status_code, 200, r_mm.text)
                        with open(cfg_path, encoding="utf-8") as _rf2:
                            saved2 = yaml.safe_load(_rf2)
                        self.assertEqual(saved2["TTS"]["MinimaxTTSHTTPStream"]["voice_id"], "female-yujie")
                        self.assertEqual(saved2["TTS"]["MinimaxTTSHTTPStream"]["voice_setting"]["speed"], 1.1)
                        self.assertEqual(saved2["TTS"]["MinimaxTTSHTTPStream"]["api_key"], "${MINIMAX_API_KEY}")

                # 6. Secret redaction covers all new audio API key/token patterns
                for raw_err, leak in (
                    ("SILICONFLOW_API_KEY=sf_secret_123456", "sf_secret_123456"),
                    ("Authorization: Bearer; doubao_secret_token_999", "doubao_secret_token_999"),
                    ("https://api.minimaxi.com/v1/t2a_v2?GroupId=9988776655", "9988776655"),
                    ("X-Api-Access-Key: volc_priv_key_abc123", "volc_priv_key_abc123"),
                    ("DASHSCOPE_API_KEY=sk-dashscope9876543210", "sk-dashscope9876543210"),
                ):
                    red = shared._redact_secrets(raw_err)
                    self.assertNotIn(leak, red, f"Secret leaked after redaction: {red!r}")
            finally:
                cs.CONSOLE_PORT = orig_port
                httpd.shutdown()
                httpd.server_close()

    def test_02_cloud_tts_preview_and_asr_test_endpoints(self):
        import console_server as cs
        from console.routes import audio_api
        from patches import cloud_audio

        httpd, port = _start_test_console()
        orig_port = cs.CONSOLE_PORT
        cs.CONSOLE_PORT = port
        try:
            tok = cs.get_console_token()
            hdrs = _post_headers(port, tok)
            fake_sess = MagicMock()

            def _mock_post(url, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                if "siliconflow.cn/v1/audio/speech" in url or "/v1/audio/speech" in url:
                    resp.content = b"RIFF_CLOUD_WAV"
                    resp.text = ""
                elif "openspeech.bytedance.com/api/v1/tts" in url:
                    resp.content = b'{"data": "UklGRg=="}'
                    resp.json.return_value = {"data": base64.b64encode(b"DOUBAO_WAV").decode()}
                    resp.text = ""
                elif "fish.audio/v1/tts" in url:
                    resp.content = b"FISH_WAV"
                    resp.text = ""
                elif "minimaxi.com/v1/t2a_v2" in url:
                    resp.content = b"{}"
                    resp.json.return_value = {"base_resp": {"status_code": 0}, "data": {"audio": b"MM_MP3".hex()}}
                    resp.text = ""
                elif "/audio/transcriptions" in url:
                    resp.content = b'{"text": ""}'
                    resp.json.return_value = {"text": ""}
                    resp.text = '{"text": ""}'
                else:
                    resp.status_code = 404
                    resp.content = b""
                    resp.text = "not found"
                return resp

            fake_sess.post.side_effect = _mock_post

            async def _fake_alibl_ws(*args, **kwargs):
                return b"ALIBL_MP3"

            async def _fake_huoshan_ws(*args, **kwargs):
                return b"HUOSHAN_MP3"

            with (
                patch.object(cloud_audio, "_get_cloud_http_session", return_value=fake_sess),
                patch.object(audio_api, "_preview_alibl_ws", side_effect=_fake_alibl_ws),
                patch.object(audio_api, "_preview_huoshan_ws", side_effect=_fake_huoshan_ws),
                patch.dict(
                    os.environ,
                    {
                        "SILICONFLOW_API_KEY": "sk-sf-1234567890abcd",
                        "OPENAI_AUDIO_API_KEY": "sk-oai-1234567890abcd",
                        "DASHSCOPE_API_KEY": "sk-ds-1234567890abcd",
                        "DOUBAO_APPID": "112233",
                        "DOUBAO_ACCESS_TOKEN": "db-tok-1234567890abcd",
                        "FISHSPEECH_API_KEY": "fish-1234567890abcd",
                        "MINIMAX_API_KEY": "mm-1234567890abcd",
                        "MINIMAX_GROUP_ID": "99001122",
                    },
                    clear=False,
                ),
            ):
                with httpx.Client(timeout=5.0, trust_env=False) as client:
                    # Preview all 7 cloud TTS providers
                    for prov, expected_bytes in (
                        ("CosyVoiceSiliconflow", b"RIFF_CLOUD_WAV"),
                        ("OpenAITTS", b"RIFF_CLOUD_WAV"),
                        ("DoubaoTTS", b"DOUBAO_WAV"),
                        ("FishSpeech", b"FISH_WAV"),
                        ("MinimaxTTSHTTPStream", b"MM_MP3"),
                        ("AliBLTTS", b"ALIBL_MP3"),
                        ("HuoshanDoubleStreamTTS", b"HUOSHAN_MP3"),
                    ):
                        r = client.post(
                            f"http://127.0.0.1:{port}/api/tts-preview",
                            json={"tts_provider": prov, "text": "你好小智"},
                            headers=hdrs,
                        )
                        self.assertEqual(r.status_code, 200, f"{prov} preview failed: {r.status_code} {r.text}")
                        self.assertEqual(r.content, expected_bytes)

                    # Reject unknown tts_provider -> 400
                    r_bad = client.post(
                        f"http://127.0.0.1:{port}/api/tts-preview",
                        json={"tts_provider": "BadEngine", "text": "你好"},
                        headers=hdrs,
                    )
                    self.assertEqual(r_bad.status_code, 400)

                    # ASR connectivity probe -> 200 ok
                    r_asr = client.post(
                        f"http://127.0.0.1:{port}/api/asr-test",
                        json={"asr_provider": "SiliconFlowASR"},
                        headers=hdrs,
                    )
                    self.assertEqual(r_asr.status_code, 200, r_asr.text)
                    self.assertTrue(r_asr.json()["ok"])
                    self.assertEqual(r_asr.json()["provider"], "SiliconFlowASR")
                    self.assertGreaterEqual(r_asr.json()["latency_ms"], 0.0)

                    # Missing key -> 400 with clear message
                    with patch.dict(os.environ, {"SILICONFLOW_API_KEY": ""}, clear=False):
                        r_nokey = client.post(
                            f"http://127.0.0.1:{port}/api/asr-test",
                            json={"asr_provider": "SiliconFlowASR"},
                            headers=hdrs,
                        )
                        self.assertEqual(r_nokey.status_code, 400)
                        self.assertIn("SILICONFLOW_API_KEY", r_nokey.json()["error"])
        finally:
            cs.CONSOLE_PORT = orig_port
            httpd.shutdown()
            httpd.server_close()

    def test_03_static_assets_and_ps1_hygiene(self):
        import console.server as srv_mod

        static_dir = os.path.join(os.path.dirname(srv_mod.__file__), "static")
        with open(os.path.join(static_dir, "app.js"), encoding="utf-8") as _f:
            app_js = _f.read()
        with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as _f:
            index_html = _f.read()
        self.assertNotIn('fetch("/api/tts-preview"', app_js)
        self.assertNotIn('fetch("/api/asr-test"', app_js)
        self.assertIn('consolePost("/api/asr-test"', app_js)
        self.assertIn("asrProviderSelect", index_html)
        self.assertIn("ttsProviderSelect", index_html)
        self.assertIn("lowspecPresetBar", index_html)

        host_scripts = os.environ.get("XIAOZHI_HOST_SCRIPTS_DIR", "")
        if host_scripts and os.path.isdir(host_scripts):
            for fname in ("init_setup.ps1", "start_all.ps1"):
                fp = os.path.join(host_scripts, fname)
                if os.path.isfile(fp):
                    with open(fp, "rb") as _bf:
                        raw = _bf.read()
                    self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), f"{fname} must be UTF-8 with BOM")
                    self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), f"{fname} must use CRLF line endings")
                    txt = raw.decode("utf-8-sig")
                    self.assertIn("SILICONFLOW_API_KEY", txt)
                    self.assertIn("DASHSCOPE_API_KEY", txt)

    def test_04_p0_p1_p2_security_emotion_vad_telemetry_and_multidevice_hardening(self):
        """Comprehensive regression test for P0-2/3, P1-1/3/4/5, and P2-4/5."""
        import requests
        import asr_corrections
        import console_server as cs
        import turn_context_router as tcr
        from console.routes import audio_api
        from patches import cloud_audio, weather
        from patches.emotion import splitter

        # 1. P0-2: SSRF ipaddress + non-canonical IP + container/admin port blocking
        for bad_url in (
            "http://xiaozhi-anima-brain:18787/xiaozhi/recall",
            "http://172.17.0.2:18787/health",
            "http://127.0.0.2:8000/xiaozhi/v1/",
            "http://[::ffff:127.0.0.1]:8003/mcp/vision/explain",
            "http://169.254.169.254/latest/meta-data/",
            "http://host.docker.internal:19880/exit",
            "http://0.0.0.0:9000/v1/audio/speech",
            "http://2130706433:8003/",
            "http://127.1:8000/",
            "http://0x7f000001:8003/",
            "http://2852039166/latest/meta-data/",
            "http://ip6-localhost:8003/",
            "http://external.example.com:8000/",
        ):
            with self.assertRaises(ValueError, msg=f"SSRF URL should be rejected: {bad_url}"):
                audio_api.validate_safe_api_url(bad_url, allow_local_gateway=True)
        for bad_cloud_only in (
            "http://ip6-localhost:9000/v1/tts",
            "http://singlelabelhost:9000/v1/tts",
        ):
            with self.assertRaises(ValueError, msg=f"Local host should be rejected when allow_local_gateway=False: {bad_cloud_only}"):
                audio_api.validate_safe_api_url(bad_cloud_only, allow_local_gateway=False)

        # Prefix-spoofed external domains must NEVER be treated as local HTTP gateways
        for spoof_url in (
            "http://localhost.evil.com/v1/tts",
            "http://127.0.0.1.evil.com/v1/tts",
            "http://10.evil.com/v1/tts",
            "http://192.168.evil.com/v1/tts",
            "http://host.docker.internal.evil.com/v1/tts",
        ):
            self.assertFalse(cloud_audio._is_local_http_url(spoof_url), f"Prefix spoof matched: {spoof_url}")
        self.assertTrue(cloud_audio._is_local_http_url("http://127.0.0.1:8080/v1/tts"))
        self.assertTrue(cloud_audio._is_local_http_url("http://192.168.1.50:8001/v1/audio/speech"))

        # Vendor-locked SiliconFlowASR & FishSpeech reject non-official / prefix-spoofed domains
        with self.assertRaises(ValueError):
            audio_api.validate_asr_update(
                "SiliconFlowASR",
                {"base_url": "https://evil.example.com/v1/audio/transcriptions"},
                cur_asr_provider="SherpaASR",
            )
        with self.assertRaises(ValueError):
            audio_api.validate_tts_cloud_update(
                "FishSpeech",
                {"api_url": "http://localhost.evil.com/v1/tts"},
            )

        # Override origin (host OR scheme downgrade OR port change) on OpenAITTS/OpenaiASR refuses to exfiltrate OPENAI_AUDIO_API_KEY
        with patch.dict(os.environ, {"OPENAI_AUDIO_API_KEY": "sk-oai-secret-do-not-leak"}, clear=False):
            for bad_ov_tts in (
                "https://attacker.example.com/v1/audio/speech",
                "http://api.openai.com/v1/audio/speech",
                "https://api.openai.com:8443/v1/audio/speech",
            ):
                with self.assertRaises(ValueError, msg=f"Should block key leak to {bad_ov_tts}"):
                    audio_api.synthesize_cloud_tts_preview(
                        "OpenAITTS",
                        "测试",
                        tts_config_override={"api_url": bad_ov_tts},
                        raw_cfg={"TTS": {"OpenAITTS": {"api_url": "https://api.openai.com/v1/audio/speech"}}},
                    )
            for bad_ov_asr in (
                "https://attacker.example.com/v1/audio/transcriptions",
                "http://api.openai.com/v1/audio/transcriptions",
                "https://api.openai.com:8443/v1/audio/transcriptions",
            ):
                with self.assertRaises(ValueError, msg=f"Should block key leak to {bad_ov_asr}"):
                    audio_api.test_asr_connectivity(
                        "OpenaiASR",
                        asr_config_override={"base_url": bad_ov_asr},
                        raw_cfg={"ASR": {"OpenaiASR": {"base_url": "https://api.openai.com/v1/audio/transcriptions"}}},
                    )

        # GroupId= + Doubao INFO headers/token redaction in cloud_audio._RedactingLoggerProxy + _NoRedirectSession
        self.assertNotIn(
            "99887766",
            cloud_audio.redact_cloud_error("HTTPSConnectionPool: url=/v1/t2a_v2?GroupId=99887766 failed"),
        )
        logged_info = []
        fake_raw_logger = types.SimpleNamespace(
            bind=lambda **kw: types.SimpleNamespace(info=lambda msg, *a, **k: logged_info.append(msg))
        )
        prox = cloud_audio._RedactingLoggerProxy(fake_raw_logger)
        prox.bind(tag="doubao_stream").info(
            "正在连接ASR服务，headers: {'X-Api-App-Key': '88776655', 'X-Api-Access-Key': 'secret-doubao-tok-999'}"
        )
        prox.bind(tag="doubao_stream").info(
            "发送初始化请求: {'app': {'appid': '88776655', 'token': 'secret-doubao-tok-999'}}"
        )
        for line_logged in logged_info:
            self.assertNotIn("secret-doubao-tok-999", line_logged)
            self.assertNotIn("88776655", line_logged)
        self.assertEqual(cloud_audio._get_cloud_http_session().max_redirects, 0)

        # 2. P0-3: AliyunBLStreamASR & DoubaoASR/DoubaoStreamASR create missing output_dir; Minimax preserves [emotion] in remaining_text & resets across turns; OpenaiASR strips SenseVoice tags
        from core.providers.asr.aliyunbl_stream import ASRProvider as AliyunBLStreamASR
        from core.providers.asr.doubao import ASRProvider as DoubaoASR
        from core.providers.asr.doubao_stream import ASRProvider as DoubaoStreamASR
        from core.providers.asr.openai import ASRProvider as OpenaiASR
        from core.providers.tts.minimax_httpstream import TTSProvider as MinimaxTTS

        with tempfile.TemporaryDirectory() as td:
            missing_out = os.path.join(td, "nested_alibl_out")
            AliyunBLStreamASR({"api_key": "${DASHSCOPE_API_KEY}", "output_dir": missing_out}, True)
            self.assertTrue(os.path.isdir(missing_out))
            missing_db_stream = os.path.join(td, "nested_doubao_stream_out")
            DoubaoStreamASR({"appid": "${DOUBAO_ASR_APPID}", "access_token": "${DOUBAO_ASR_ACCESS_TOKEN}", "output_dir": missing_db_stream}, True)
            self.assertTrue(os.path.isdir(missing_db_stream))
            DoubaoASR({"appid": "${DOUBAO_ASR_APPID}", "access_token": "${DOUBAO_ASR_ACCESS_TOKEN}"}, True)

            mm = MinimaxTTS(
                {"group_id": "12345", "api_key": "mm-key-123456", "model": "speech-02-turbo", "output_dir": td},
                True,
            )
            self.assertNotIn("emotion", mm.voice_setting)
            # Verify Minimax _process_remaining_text_stream strips [gentle] without speaking literal 'gentle' and resets across turns
            mm.conn = types.SimpleNamespace(sentence_id="mm-turn-1", device_id="aa:bb")
            mm.tts_text_buff = ["[sad] 别难过啦"]
            mm.processed_chars = 0
            mm_spoken = []
            with patch.object(
                mm,
                "to_tts_single_stream",
                side_effect=lambda txt, is_last=False: mm_spoken.append(
                    (txt, getattr(mm, "_pending_segment_emotion", None), getattr(mm, "_pending_mm_raw_emotion", None))
                ),
            ):
                mm._process_remaining_text_stream(True)
            self.assertEqual(mm_spoken, [("别难过啦", "tender", "sad")])
            # Turn 2 with new conn.sentence_id resets _turn_explicit_emotion cleanly
            mm.conn.sentence_id = "mm-turn-2"
            mm.tts_text_buff = ["今天天气真不错。"]
            mm.processed_chars = 0
            seg_t2 = mm._get_segment_text()
            self.assertEqual(seg_t2, "今天天气真不错。")
            self.assertIsNone(getattr(mm, "_turn_explicit_emotion", None))

            wav_p = os.path.join(td, "sv.wav")
            with wave.open(wav_p, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00" * 800)
            oai_asr = OpenaiASR(
                {"api_key": "sk-test-1234567890ab", "base_url": "https://api.openai.com/v1/audio/transcriptions", "output_dir": td},
                True,
            )
            fake_sess = MagicMock()
            fake_resp = MagicMock(status_code=200, text="")
            fake_resp.json.return_value = {"text": "<|zh|><|NEUTRAL|><|Speech|>你好小智"}
            fake_sess.post.return_value = fake_resp
            with patch.object(cloud_audio, "_get_cloud_http_session", return_value=fake_sess):
                cleaned_sv, _ = asyncio.run(
                    oai_asr.speech_to_text([], "s-sv", artifacts=types.SimpleNamespace(file_path=wav_p))
                )
            self.assertEqual(cleaned_sv, "你好小智")

        # 3. P1-1: Streaming TTS emotion tag pushes ESP32 OLED emotion
        pushed_emos = []
        fake_conn = types.SimpleNamespace(sentence_id="sid-emo-1", device_id="aa:bb")
        dummy_stream_tts = types.SimpleNamespace(conn=fake_conn)
        with patch.object(splitter, "_push_esp32_emotion_async", side_effect=lambda c, emj, emo: pushed_emos.append((emj, emo))):
            out_chunk = cloud_audio.strip_streaming_emotion_chunk(dummy_stream_tts, "[shy] 别这么看着我啦", flush=False)
        self.assertEqual(out_chunk, "别这么看着我啦")
        self.assertEqual(pushed_emos, [("😳", "embarrassed")])

        # 4. P1-3(1) & P2-5: requests.exceptions.Timeout -> HTTP 504 + strict CSP header on 18080
        httpd, port = _start_test_console()
        orig_port = cs.CONSOLE_PORT
        cs.CONSOLE_PORT = port
        try:
            tok = cs.get_console_token()
            hdrs = _post_headers(port, tok)
            timeout_sess = MagicMock()
            timeout_sess.post.side_effect = requests.exceptions.ReadTimeout("read timed out")
            with (
                patch.object(cloud_audio, "_get_cloud_http_session", return_value=timeout_sess),
                patch.dict(os.environ, {"SILICONFLOW_API_KEY": "sk-sf-timeout-test-1234"}, clear=False),
                httpx.Client(timeout=5.0, trust_env=False) as client,
            ):
                r_504_asr = client.post(
                    f"http://127.0.0.1:{port}/api/asr-test",
                    json={"asr_provider": "SiliconFlowASR"},
                    headers=hdrs,
                )
                self.assertEqual(r_504_asr.status_code, 504)
                csp = r_504_asr.headers.get("Content-Security-Policy", "")
                self.assertIn("script-src 'self'", csp)
                self.assertIn("object-src 'none'", csp)
                self.assertIn("frame-ancestors 'none'", csp)

                r_504_tts = client.post(
                    f"http://127.0.0.1:{port}/api/tts-preview",
                    json={"tts_provider": "CosyVoiceSiliconflow", "text": "超时测试"},
                    headers=hdrs,
                )
                self.assertEqual(r_504_tts.status_code, 504)
        finally:
            cs.CONSOLE_PORT = orig_port
            httpd.shutdown()
            httpd.server_close()

        # 5. P1-4: SherpaASR requires_file() == False when delete_audio_file=True and decodes pcm_bytes in memory
        from core.providers.asr.sherpa_onnx_local import ASRProvider as SherpaASR

        fake_sherpa = object.__new__(SherpaASR)
        fake_sherpa.delete_audio_file = True
        self.assertFalse(fake_sherpa.requires_file())
        fake_stream = types.SimpleNamespace(
            accepted=None,
            result=types.SimpleNamespace(text="<|zh|><|HAPPY|>内存直解成功"),
        )
        fake_stream.accept_waveform = lambda sr, samples: setattr(fake_stream, "accepted", (sr, len(samples)))
        fake_sherpa.model = types.SimpleNamespace(
            create_stream=lambda: fake_stream,
            decode_stream=lambda s: None,
        )
        fake_sherpa.read_wave = MagicMock(side_effect=AssertionError("Must not read WAV from disk when pcm_bytes present"))
        mem_art = types.SimpleNamespace(pcm_bytes=b"\x00\x01" * 1600, file_path=None)
        with patch.object(asr_corrections, "apply_asr_corrections", side_effect=lambda s: s):
            mem_txt, _ = asyncio.run(fake_sherpa.speech_to_text([], "s-mem", artifacts=mem_art))
        self.assertEqual(mem_txt, "内存直解成功")
        self.assertEqual(fake_stream.accepted, (16000, 1600))

        # 6. P1-5(2): Farewell suppress_llm_telemetry() does not pollute get_llm_cache_stats()
        tcr.reset_llm_cache_stats()
        fake_usage_resp = types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="晚安好梦。"))],
            usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=10, prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20),
        )
        fake_llm = types.SimpleNamespace(
            model_name="deepseek-flash",
            client=types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: fake_usage_resp))
            ),
        )
        tcr.wrap_llm_client_for_usage(fake_llm)
        with tcr.suppress_llm_telemetry():
            fake_llm.client.chat.completions.create(model="deepseek-flash", messages=[], stream=False)
        self.assertEqual(tcr.get_llm_cache_stats()["turns"], 0)
        fake_llm.client.chat.completions.create(model="deepseek-flash", messages=[], stream=False)
        st_non_stream = tcr.get_llm_cache_stats()
        self.assertEqual(st_non_stream["turns"], 1)
        self.assertIsNone(st_non_stream["recent_avg_ttft_ms"])
        self.assertIsNone(st_non_stream["last_ttft_ms"])
        tcr.reset_llm_cache_stats()

        # 7. P2-4: Overseas weather URL-encodes city names; AnimaMemoryProvider maps device_id -> character_id while isolating _recall_cache
        captured_wttr_urls = []

        class _FakeWttrClient:
            async def get(self, url, **kwargs):
                captured_wttr_urls.append(url)
                r = MagicMock(status_code=200)
                r.json.return_value = {"current_condition": [{"temp_C": "20", "FeelsLikeC": "19", "humidity": "55", "lang_zh": [{"value": "晴"}]}], "weather": []}
                return r

        with patch.object(weather, "_get_async_client", return_value=_FakeWttrClient()):
            wttr_out = asyncio.run(weather._fetch_overseas_wttr("New York/ Manhattan"))
        self.assertIn("New%20York%2F%20Manhattan", captured_wttr_urls[0])
        self.assertIn("20℃", wttr_out)

        import anima_memory_provider as amp

        with tempfile.TemporaryDirectory() as td_ob:
            ob_path = os.path.join(td_ob, "outbox.sqlite")
            prov_mem = amp.MemoryProvider(
                {
                    "character_id": "xiaozhi",
                    "device_character_map": {"aa:bb:cc:11:22:33": "kid_room", "dd:ee:ff:44:55:66": "study_room"},
                    "outbox_db": ob_path,
                }
            )
            try:
                self.assertEqual(prov_mem._resolve_character_id("aa:bb:cc:11:22:33"), "kid_room")
                self.assertEqual(prov_mem._resolve_character_id("dd:ee:ff:44:55:66"), "study_room")
                self.assertEqual(prov_mem._resolve_character_id("99:99:99:00:00:00"), "xiaozhi")
                # Per-device warm cache stays strictly isolated even for unmapped devices sharing default character
                prov_mem._record_recall_success("99:99:99:00:00:00", "客厅缓存", 5.0)
                self.assertIsNone(prov_mem._recall_cache.get("88:88:88:00:00:00"))
            finally:
                amp.get_default_outbox(auto_start=False).close()

    def test_05_direct_modelscope_download_avoids_pytorch_import_and_guards_paths(self):
        """First-boot SherpaASR download uses lightweight direct HTTP without importing real modelscope."""
        import httpx
        from patches import asr as asr_patch

        saved = {k: sys.modules.pop(k, None) for k in ("modelscope", "modelscope.hub", "modelscope.hub.file_download")}
        try:
            asr_patch.install_memory_optimizations(start_trimmer=False)
            dl_mod = sys.modules["modelscope.hub.file_download"]
            payload = b"ONNX_MOCK_PAYLOAD_" * 512

            class _FakeStreamCtx:
                def __init__(self, status_code=200, data=payload, cl_override=None, content_encoding=None):
                    self.status_code = status_code
                    self._data = data
                    cl = len(data) if cl_override is None else cl_override
                    self.headers = {}
                    if cl is not None:
                        self.headers["content-length"] = str(cl)
                    if content_encoding:
                        self.headers["content-encoding"] = content_encoding

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def iter_bytes(self, chunk_size=262144):
                    yield self._data

            with tempfile.TemporaryDirectory() as td:
                captured_calls = []

                def _fake_stream(method, url, **kwargs):
                    captured_calls.append((url, kwargs))
                    return _FakeStreamCtx()

                with patch.object(httpx, "stream", side_effect=_fake_stream):
                    out_fp = dl_mod.model_file_download(
                        model_id="org/sherpa-model",
                        file_path="model.int8.onnx",
                        local_dir=td,
                    )
                self.assertEqual(os.path.abspath(out_fp), os.path.abspath(os.path.join(td, "model.int8.onnx")))
                self.assertEqual(open(out_fp, "rb").read(), payload)
                self.assertIn("https://modelscope.cn/models/org/sherpa-model/resolve/master/model.int8.onnx", captured_calls[0][0])
                # Must prefer direct domestic connection (trust_env=False) & identity encoding
                self.assertIs(captured_calls[0][1].get("trust_env"), False)
                self.assertEqual((captured_calls[0][1].get("headers") or {}).get("Accept-Encoding"), "identity")
                # Real modelscope MUST NOT have been imported (zero ~330MB PyTorch/scipy spike)
                self.assertFalse(hasattr(sys.modules["modelscope"], "__file__"))

                # Gzip Content-Encoding (decompressed len != compressed content-length) must still succeed
                with patch.object(
                    httpx,
                    "stream",
                    return_value=_FakeStreamCtx(data=b"token_line\n" * 100, cl_override=42, content_encoding="gzip"),
                ):
                    tok_fp = dl_mod.model_file_download(
                        model_id="org/sherpa-model",
                        file_path="tokens.txt",
                        local_dir=td,
                    )
                self.assertTrue(os.path.isfile(tok_fp))
                self.assertEqual(open(tok_fp, "rb").read(), b"token_line\n" * 100)

                # Path traversal & invalid model_id return None (refused before any HTTP call)
                self.assertIsNone(
                    asr_patch._try_direct_modelscope_download(
                        model_id="org/repo", file_path="../escape.onnx", local_dir=td
                    )
                )
                self.assertIsNone(
                    asr_patch._try_direct_modelscope_download(
                        model_id="bad id with space", file_path="model.int8.onnx", local_dir=td
                    )
                )

                # Truncated Content-Length cleans up .part file and returns None cleanly
                with patch.object(
                    httpx,
                    "stream",
                    return_value=_FakeStreamCtx(data=b"short", cl_override=99999),
                ):
                    self.assertIsNone(
                        asr_patch._try_direct_modelscope_download(
                            model_id="org/repo", file_path="broken.onnx", local_dir=td
                        )
                    )
                self.assertFalse(os.path.exists(os.path.join(td, "broken.onnx")))
                self.assertEqual([f for f in os.listdir(td) if ".part." in f], [])
        finally:
            for k in ("modelscope", "modelscope.hub", "modelscope.hub.file_download"):
                sys.modules.pop(k, None)
                if saved[k] is not None:
                    sys.modules[k] = saved[k]


if __name__ == "__main__":
    unittest.main(verbosity=2)

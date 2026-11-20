"""Layer 2: Upstream Image Contract Tests (run inside xiaozhi-esp32-server after EVERY upstream image upgrade).

Validates without external network calls:
1. Upstream SERVER_VERSION matches patch_registry.EXPECTED_SERVER_VERSION (0.9.8)
2. Every monkeypatch registered by start_with_env.py installs cleanly with contract_ok=True
3. All 4 prompt-harmony replacement anchors exist in upstream agent-base-prompt.txt
4. patch_registry.rebind() rebinds early-bound upstream imports (listenMessageHandler, helloHandle, etc.)
5. Key upstream method signatures (ConnectionHandler, OpenAI.LLMProvider, SherpaASR, OTA/WS/Vision) stay compatible
"""

from __future__ import annotations

import inspect
import os
import sys
import unittest

ROOT_DIR = "/opt/xiaozhi-esp32-server"
DATA_DIR = os.path.join(ROOT_DIR, "data")
for _p in (ROOT_DIR, DATA_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class TestUpstreamImageContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Import start_with_env once to trigger all 18 monkeypatches & contract verifications
        import start_with_env  # noqa: F401
        import patch_registry

        cls.patch_registry = patch_registry

    def test_upstream_server_version_matches_expected(self):
        info = self.patch_registry.check_upstream_server_version()
        self.assertTrue(
            info.get("match"),
            f"Upstream SERVER_VERSION mismatch: got {info.get('actual')!r}, expected {info.get('expected')!r}",
        )

    def test_all_18_patches_registered_ok_with_valid_contracts(self):
        expected_patches = {
            "config_env_expand",
            "mcp_token_injection",
            "esp32_auth_ota",
            "esp32_auth_ws",
            "esp32_auth_vision",
            "anima_memory_factory",
            "anima_dialogue_hooks",
            "nmc_weather",
            "pc_audio_tts",
            "mei_prompt_anchors",
            "mei_emotion_and_prosody",
            "anima_turn_flush",
            "pc_audio_playback",
            "mei_wakeup_responses",
            "listen_interrupt_handler",
            "idle_farewell",
            "reply_length_llm",
            "sherpa_async_asr",
        }
        summary = self.patch_registry.get_health_summary()
        self.assertFalse(
            summary["degraded"],
            f"Patch registry degraded: {summary.get('degraded_reasons')}",
        )
        patches = summary.get("patches") or {}
        missing = sorted(expected_patches - set(patches.keys()))
        self.assertEqual(missing, [], f"Missing required patches: {missing}")
        for name in sorted(expected_patches):
            rec = patches[name]
            self.assertEqual(rec["status"], "ok", f"Patch {name} status={rec['status']}: {rec.get('detail')}")
            self.assertTrue(rec["contract_ok"], f"Patch {name} contract failed: {rec.get('detail')}")

    def test_prompt_harmony_anchors_present_in_upstream_template(self):
        import mei_emotion_router

        ok, missing = mei_emotion_router.verify_prompt_base_anchors()
        self.assertTrue(ok, f"Upstream agent-base-prompt.txt missing anchors: {missing}")

    def test_early_bound_upstream_imports_are_rebound(self):
        import core.handle.abortHandle as abort_mod
        import core.handle.helloHandle as hello_mod
        import core.handle.intentHandler as intent_mod
        import core.handle.receiveAudioHandle as recv_mod
        import core.handle.sendAudioHandle as send_mod
        import core.handle.textHandler.listenMessageHandler as listen_mod
        import config.config_loader as cfg_loader
        import config.settings as cfg_settings

        self.assertIs(listen_mod.send_tts_message, send_mod.send_tts_message)
        self.assertIs(hello_mod.send_tts_message, send_mod.send_tts_message)
        self.assertIs(recv_mod.handleAbortMessage, abort_mod.handleAbortMessage)
        self.assertIs(intent_mod.checkWakeupWords, hello_mod.checkWakeupWords)
        self.assertIs(cfg_settings.read_config, cfg_loader.read_config)

    def test_upstream_llm_chat_asr_and_auth_callable_contracts(self):
        from core.api.ota_handler import OTAHandler
        from core.api.vision_handler import VisionHandler
        from core.connection import ConnectionHandler
        from core.handle.receiveAudioHandle import no_voice_close_connect
        from core.providers.asr.sherpa_onnx_local import ASRProvider as SherpaASR
        from core.providers.llm.openai.openai import LLMProvider as OpenAILLM
        from core.websocket_server import WebSocketServer

        self.assertTrue(inspect.iscoroutinefunction(OTAHandler.handle_post))
        self.assertTrue(inspect.iscoroutinefunction(WebSocketServer._handle_auth))
        self.assertTrue(inspect.iscoroutinefunction(VisionHandler.handle_post))
        self.assertTrue(inspect.iscoroutinefunction(SherpaASR.speech_to_text))
        self.assertTrue(inspect.iscoroutinefunction(no_voice_close_connect))
        self.assertTrue(callable(ConnectionHandler.chat))
        self.assertTrue(callable(getattr(ConnectionHandler, "_handle_function_result", None)))
        self.assertTrue(callable(OpenAILLM.response))
        self.assertTrue(callable(OpenAILLM.response_with_functions))


if __name__ == "__main__":
    unittest.main(verbosity=2)

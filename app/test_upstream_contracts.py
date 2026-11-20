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
APP_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (ROOT_DIR, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class TestUpstreamImageContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Explicitly run boot.install_all() (H-1: importing boot/start_with_env has zero side effects)
        import boot
        import patch_registry

        boot.install_all(start_background_workers=False, run_contract_self_check=True)
        cls.patch_registry = patch_registry

    def test_upstream_server_version_matches_expected(self):
        info = self.patch_registry.check_upstream_server_version()
        self.assertTrue(
            info.get("match"),
            f"Upstream SERVER_VERSION mismatch: got {info.get('actual')!r}, expected {info.get('expected')!r}",
        )

    def test_all_19_patches_registered_ok_with_valid_contracts(self):
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
            "llm_prefix_cache",
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
        try:
            import core.handle.textHandler.helloMessageHandler as hello_msg_mod

            if hasattr(hello_msg_mod, "handleHelloMessage"):
                self.assertIs(hello_msg_mod.handleHelloMessage, hello_mod.handleHelloMessage)
        except ImportError:
            pass

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

    def test_h1_zero_import_side_effects_and_boot_contract_self_check(self):
        """H-1: Importing boot / start_with_env / patches has ZERO side effects; install_all records contract self-check."""
        import json as _json
        import subprocess
        import tempfile

        # 1. Verify startup lightweight contract self-check was recorded in registry
        sc = self.patch_registry.get_contract_self_check()
        self.assertTrue(sc.get("ran"), "boot.install_all() must run lightweight contract self-check")
        self.assertTrue(sc.get("ok"), f"Boot contract self-check failed: {sc}")
        for key in ("upstream_version", "prompt_anchors", "early_bound_rebind", "callable_contracts"):
            self.assertIn(key, sc.get("checks") or {})
            self.assertTrue(sc["checks"][key]["ok"], f"Self-check {key} failed: {sc['checks'][key]}")

        # 2. Fresh subprocess import test: cwd unchanged, zero extra threads, zero patches installed on import
        with tempfile.TemporaryDirectory() as td:
            probe = (
                "import json, os, sys, threading; "
                f"sys.path.insert(0, {APP_DIR!r}); "
                f"os.chdir({td!r}); "
                "cwd0 = os.getcwd(); "
                "th0 = {t.name for t in threading.enumerate()}; "
                "import boot, start_with_env, patch_registry, mei_emotion_router, pc_audio_router, reply_length_router, nmc_weather_patch, console_server; "
                "cwd1 = os.getcwd(); "
                "th1 = {t.name for t in threading.enumerate()}; "
                "patches = patch_registry.get_all_patches(); "
                "print(json.dumps({'cwd_same': cwd0 == cwd1, 'new_threads': sorted(th1 - th0), 'patches_count': len(patches), 'installed': boot.is_installed()}))"
            )
            proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, f"Subprocess import probe failed: {proc.stderr}")
            data = _json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(data["cwd_same"], "Importing boot/start_with_env must NOT call os.chdir")
            self.assertEqual(data["new_threads"], [], f"Importing modules must NOT start threads: {data['new_threads']}")
            self.assertEqual(data["patches_count"], 0, "Importing modules must NOT apply monkeypatches")
            self.assertFalse(data["installed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

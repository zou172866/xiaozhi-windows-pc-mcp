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
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DIR = os.environ.get("XIAOZHI_APP_DIR") or (
    os.path.join(ROOT_DIR, "app")
    if os.path.isdir(os.path.join(ROOT_DIR, "app"))
    else os.path.join(_REPO_ROOT, "app")
)
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

    def test_all_22_patches_registered_ok_and_match_catalog(self):
        catalog = self.patch_registry.get_patch_catalog()
        catalog_names = [p["name"] for p in catalog]
        self.assertEqual(len(catalog_names), len(set(catalog_names)), "Duplicate names in PATCH_CATALOG")
        expected_patches = set(catalog_names)
        self.assertEqual(len(expected_patches), 22, f"Expected 22 cataloged patches, got {len(expected_patches)}")

        # Verify security category metadata matches SECURITY_PATCH_NAMES 1-to-1
        for entry in catalog:
            expected_cat = "security" if entry["name"] in self.patch_registry.SECURITY_PATCH_NAMES else "experience"
            self.assertEqual(
                entry["category"],
                expected_cat,
                f"PATCH_CATALOG category mismatch for {entry['name']}: got {entry['category']!r}, expected {expected_cat!r}",
            )

        summary = self.patch_registry.get_health_summary()
        self.assertFalse(
            summary["degraded"],
            f"Patch registry degraded: {summary.get('degraded_reasons')}",
        )
        patches = summary.get("patches") or {}
        self.assertEqual(
            set(patches.keys()),
            expected_patches,
            f"Live registered patches drifted from PATCH_CATALOG: "
            f"live_only={sorted(set(patches.keys()) - expected_patches)}, "
            f"catalog_only={sorted(expected_patches - set(patches.keys()))}",
        )
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

        # Cloud audio API provider contracts (21st patch: cloud_audio_api)
        from core.providers.asr.aliyunbl_stream import ASRProvider as AliyunBLStreamASR
        from core.providers.asr.doubao import ASRProvider as DoubaoASR
        from core.providers.asr.doubao_stream import ASRProvider as DoubaoStreamASR
        from core.providers.asr.openai import ASRProvider as OpenaiASR
        from core.providers.asr.qwen3_asr_flash import ASRProvider as Qwen3ASR
        from core.providers.tts.alibl_stream import TTSProvider as AliBLTTS
        from core.providers.tts.doubao import TTSProvider as DoubaoTTS
        from core.providers.tts.fishspeech import TTSProvider as FishSpeechTTS
        from core.providers.tts.huoshan_double_stream import TTSProvider as HuoshanTTS
        from core.providers.tts.minimax_httpstream import TTSProvider as MinimaxTTS
        from core.providers.tts.openai import TTSProvider as OpenAITTS
        from core.providers.tts.siliconflow import TTSProvider as SiliconFlowTTS

        self.assertTrue(callable(AliyunBLStreamASR.__init__))
        self.assertTrue(getattr(AliyunBLStreamASR, "_cloud_audio_patched", False))
        self.assertTrue(getattr(DoubaoASR, "_cloud_audio_patched", False))
        self.assertTrue(getattr(DoubaoStreamASR, "_cloud_audio_patched", False))

        # Voiceprint & screen-peek MCP guard (22nd patch: voiceprint_mcp_guard)
        from core.providers.tools.server_mcp.mcp_manager import ServerMCPManager

        self.assertTrue(getattr(ServerMCPManager, "_voiceprint_mcp_guard_patched", False))
        self.assertTrue(inspect.iscoroutinefunction(ServerMCPManager.execute_tool))

        for coro_fn in (
            OpenaiASR.speech_to_text,
            Qwen3ASR.speech_to_text,
            DoubaoASR.speech_to_text,
            DoubaoStreamASR.receive_audio,
            OpenAITTS.text_to_speak,
            SiliconFlowTTS.text_to_speak,
            DoubaoTTS.text_to_speak,
            FishSpeechTTS.text_to_speak,
            AliBLTTS.text_to_speak,
            AliBLTTS.start_session,
            AliBLTTS.open_audio_channels,
            HuoshanTTS.text_to_speak,
            HuoshanTTS.start_session,
            HuoshanTTS.open_audio_channels,
            MinimaxTTS.text_to_speak,
            MinimaxTTS.open_audio_channels,
        ):
            self.assertTrue(inspect.iscoroutinefunction(coro_fn), f"Expected coroutine: {coro_fn}")
        self.assertTrue(getattr(AliBLTTS, "_pc_audio_stream_patched", False))
        self.assertTrue(getattr(HuoshanTTS, "_pc_audio_stream_patched", False))
        self.assertTrue(getattr(MinimaxTTS, "_pc_audio_stream_patched", False))

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
                "import patches, patches.auth, patches.memory_hooks, patches.asr, patches.cloud_audio, patches.contracts, patches.emotion, console.server, console.shared; "
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

    def test_h1_install_all_is_strictly_idempotent_and_mtime_cache_tracks_path(self):
        """H-1/H-5: Calling boot.install_all() twice is a clean no-op and MtimeJsonCache isolates distinct paths."""
        import io
        import tempfile
        import threading
        from contextlib import redirect_stdout
        import boot
        import xz_common

        th_before = threading.active_count()
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = boot.install_all(start_background_workers=False, run_contract_self_check=True)
        self.assertEqual(threading.active_count(), th_before)
        self.assertEqual(buf.getvalue().strip(), "", f"Second boot.install_all() should be silent no-op, got: {buf.getvalue()!r}")
        self.assertFalse(summary["degraded"])

        # MtimeJsonCache must not collide when two distinct files have identical (mtime_ns, st_size)
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, "a.json")
            p2 = os.path.join(td, "b.json")
            with open(p1, "w", encoding="utf-8") as f:
                f.write('{"v": 1}')
            with open(p2, "w", encoding="utf-8") as f:
                f.write('{"v": 2}')
            st1 = os.stat(p1)
            os.utime(p2, ns=(st1.st_atime_ns, st1.st_mtime_ns))
            cache = xz_common.MtimeJsonCache()
            self.assertEqual(cache.get(p1)["v"], 1)
            self.assertEqual(cache.get(p2)["v"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

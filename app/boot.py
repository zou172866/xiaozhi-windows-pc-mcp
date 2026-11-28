"""Single explicit boot entrypoint for xiaozhi-esp32-server (H-1).

Design contract:
- Importing `boot` (or `start_with_env` / `patches.*` / `console.*`) produces ZERO side effects:
  no `os.chdir()`, no background threads, no monkey-patching.
- Calling `install_all()` performs the complete deterministic startup sequence:
  1. Path setup & optional chdir to ROOT_DIR
  2. Upstream SERVER_VERSION check & runtime_settings migration
  3. Explicit installation of all security & experience patches in exact dependency order
  4. Lightweight startup upstream contract self-check (results recorded into `patches.registry`)
- Calling `main()` runs `install_all()` -> fail-closed security boot gate -> 18080 Web Console -> upstream `app.py`.
"""

from __future__ import annotations

import os
import runpy
import sys
import threading
from typing import Any, Dict, List

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import xz_common

ROOT_DIR = xz_common.ROOT_DIR
DATA_DIR = xz_common.DATA_DIR

_install_lock = threading.RLock()
_installed = False


def ensure_runtime_paths(*, chdir: bool = True) -> None:
    """Ensure ROOT_DIR / APP_DIR / DATA_DIR are importable and optionally chdir to ROOT_DIR."""
    for p in (ROOT_DIR, APP_DIR, DATA_DIR):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    if chdir and os.path.isdir(ROOT_DIR):
        os.chdir(ROOT_DIR)


def _install_nmc_weather() -> None:
    import nmc_weather_patch

    nmc_weather_patch.apply_nmc_weather_patch()


def _install_pc_audio_tts() -> None:
    import pc_audio_router

    pc_audio_router.apply_pc_audio_to_tts_patch()


def _install_mei_emotion() -> None:
    import mei_emotion_router

    mei_emotion_router.apply_xiaozhi_emotion_and_prosody_patches()


def _install_pc_audio_playback() -> None:
    import pc_audio_router

    pc_audio_router.apply_pc_audio_playback_patches()


def _install_mei_wakeup() -> None:
    import mei_emotion_router

    mei_emotion_router.apply_mei_wakeup_responses_patch()


def _install_idle_farewell() -> None:
    import mei_emotion_router

    mei_emotion_router.apply_idle_farewell_patch()


def _install_reply_length() -> None:
    import reply_length_router

    reply_length_router.apply_reply_length_llm_patch()


def _install_llm_prefix_cache() -> None:
    import turn_context_router

    turn_context_router.apply_llm_prefix_cache_patch()


def _install_vad_speculative() -> None:
    import vad_speculative_router

    vad_speculative_router.apply_vad_speculative_patch()


def _install_cloud_audio() -> None:
    from patches import cloud_audio

    cloud_audio.apply_cloud_audio_patches()


def _install_voiceprint_mcp_guard() -> None:
    from patches import voiceprint

    voiceprint.install_voiceprint_mcp_guard_patch()


def _install_pc_pcm_input() -> None:
    from patches import pc_pcm_input

    pc_pcm_input.install_pc_pcm_input_patch()


def _install_future_task_tool() -> None:
    from patches import future_task_tool

    future_task_tool.install_future_task_tool_patch()


def _install_llm_fallback_pool() -> None:
    import llm_fallback_router

    llm_fallback_router.apply_llm_fallback_pool_patch()


def _install_skill_progressive_loader() -> None:
    from patches import skill_loader_patch

    skill_loader_patch.install_skill_loader_patch()


def install_all(
    *,
    chdir: bool = True,
    start_background_workers: bool = True,
    run_contract_self_check: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """Explicitly install all XiaoZhi patches & run lightweight contract self-check (idempotent)."""
    global _installed
    with _install_lock:
        ensure_runtime_paths(chdir=chdir)

        import patch_registry
        import runtime_events
        import runtime_settings
        from patches import asr, auth, contracts, memory_hooks

        if _installed and not force and patch_registry.get_all_patches():
            if run_contract_self_check and not patch_registry.get_contract_self_check().get("ran"):
                contracts.run_lightweight_contract_checks()
            return patch_registry.get_health_summary()

        # 0. Verify upstream SERVER_VERSION & migrate legacy runtime JSONs
        ver_info = patch_registry.check_upstream_server_version()
        if not ver_info.get("match"):
            print(f"[PatchContract] WARNING: {ver_info.get('detail')}", flush=True)
        try:
            runtime_settings.migrate_legacy_runtime_settings()
        except Exception as rs_exc:
            print(f"[RuntimeSettings] 启动迁移跳过: {rs_exc}", flush=True)

        # 1. Config env expansion + security patches (fail-closed checked at boot gate)
        auth.install_config_env_expand_patch()
        auth.install_mcp_token_injection_patch()
        auth.install_esp32_auth_hardening_patch()

        # 2. Anima Brain memory factory & Dialogue hooks
        memory_hooks.install_anima_memory_factory_patch()
        memory_hooks.install_anima_dialogue_hooks_patch()

        # 3. Experience patches in exact dependency order
        for pname, pfn, ptarget, pprefix in (
            ("nmc_weather", _install_nmc_weather, "get_weather", "[NMCWeather]"),
            ("pc_audio_tts", _install_pc_audio_tts, "TTSProviderBase.to_tts_stream", "[PCAudioRouter]"),
            ("mei_emotion_and_prosody", _install_mei_emotion, "mei_emotion_router", "[MeiEmotionRouter]"),
            ("anima_turn_flush", memory_hooks.install_chat_turn_flush_patch, "ConnectionHandler.chat", "[AnimaTurnFlush]"),
            ("pc_audio_playback", _install_pc_audio_playback, "sendAudioHandle", "[PCAudioRouter]"),
            ("mei_wakeup_responses", _install_mei_wakeup, "helloHandle", "[MeiEmotionRouter]"),
            ("idle_farewell", _install_idle_farewell, "receiveAudioHandle.no_voice_close_connect", "[MeiEmotionRouter]"),
            ("reply_length_llm", _install_reply_length, "OpenAI.LLMProvider", "[ReplyLengthRouter]"),
            ("llm_fallback_pool", _install_llm_fallback_pool, "OpenAI.LLMProvider.__init__", "[LLMFallback]"),
            ("llm_prefix_cache", _install_llm_prefix_cache, "OpenAI.LLMProvider.__init__", "[LLMCache]"),
            (
                "vad_speculative",
                _install_vad_speculative,
                "ASRProviderBase/ConnectionHandler/receiveAudioHandle",
                "[SpeculativeVAD]",
            ),
            (
                "cloud_audio_api",
                _install_cloud_audio,
                "Cloud ASR/TTS providers (12 modules + 3 streaming PC audio)",
                "[CloudAudio]",
            ),
            (
                "voiceprint_mcp_guard",
                _install_voiceprint_mcp_guard,
                "ServerMCPManager.execute_tool",
                "[VoiceprintGuard]",
            ),
            (
                "pc_pcm_audio_input",
                _install_pc_pcm_input,
                "ConnectionHandler._decode_opus_packet",
                "[PCPcmInput]",
            ),
            (
                "future_task_tool",
                _install_future_task_tool,
                "plugins_func.register.all_function_registry",
                "[FutureTasks]",
            ),
            (
                "skill_progressive_loader",
                _install_skill_progressive_loader,
                "plugins_func.register.all_function_registry",
                "[SkillLoader]",
            ),
        ):
            patch_registry.run_patch_installer(
                pname, pfn, target=ptarget, category="experience", log_prefix=pprefix
            )

        # 4. Enable structured runtime event ring buffer for Console (P1-11)
        runtime_events.mark_hooks_active(True)

        # 5. Sherpa async ASR executor, modelscope lazy stub, ASR corrections, idle trim
        try:
            asr.install_memory_optimizations(start_trimmer=start_background_workers)
        except Exception as opt_exc:
            print(f"[MemOpt] 初始化异常: {opt_exc}", flush=True)

        # 6. Lightweight startup contract self-check recorded into patch_registry (H-1)
        if run_contract_self_check:
            sc = contracts.run_lightweight_contract_checks()
            if not sc.get("ok"):
                print(
                    f"[PatchContract] 启动轻量契约自检发现异常: {sc.get('failures')}",
                    flush=True,
                )

        _installed = True
        return patch_registry.get_health_summary()


def is_installed() -> bool:
    return bool(_installed)


def enforce_security_boot_gate(exit_code: int = 2) -> List[Dict[str, Any]]:
    """Fail-closed gate (C-1): exit(2) when any required security patch failed or is missing,
    unless XIAOZHI_ALLOW_INSECURE_BOOT=1."""
    import patch_registry

    return patch_registry.enforce_security_patches_or_exit(
        exit_code=exit_code,
        required_security_patches=patch_registry.SECURITY_PATCH_NAMES,
    )


def main() -> None:
    """Sole container boot entrypoint: install_all -> security gate -> console -> upstream app.py."""
    install_all(chdir=True, start_background_workers=True, run_contract_self_check=True)
    enforce_security_boot_gate(exit_code=2)
    try:
        import console_server

        console_server.start_console_background()
    except Exception as exc:
        print(f"[WebConsole] 启动异常: {exc}", flush=True)
    runpy.run_path(os.path.join(ROOT_DIR, "app.py"), run_name="__main__")


if __name__ == "__main__":
    main()

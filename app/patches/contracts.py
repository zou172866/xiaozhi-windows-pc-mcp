"""Lightweight startup upstream contract self-check (H-1).

Runs a fast, offline subset of `test_upstream_contracts` at boot right after
`install_all()` and writes structured results into `patches.registry` so
`/api/health` surfaces any upstream drift immediately on container startup.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, List

from patches import registry


def run_lightweight_contract_checks() -> Dict[str, Any]:
    """Run lightweight zero-network upstream contract checks and record into registry."""
    checks: Dict[str, Dict[str, Any]] = {}
    failures: List[str] = []

    # 1. Upstream SERVER_VERSION check
    ver = registry.check_upstream_server_version()
    ver_ok = bool(ver.get("match"))
    checks["upstream_version"] = {
        "ok": ver_ok,
        "detail": f"{ver.get('actual') or '?'} == {ver.get('expected')}"
        if ver_ok
        else (ver.get("detail") or "version mismatch"),
    }
    if not ver_ok:
        failures.append(checks["upstream_version"]["detail"])

    # 2. Prompt harmony anchors present in upstream agent-base-prompt.txt
    try:
        import mei_emotion_router

        anc_ok, anc_det = mei_emotion_router.verify_prompt_base_anchors()
        checks["prompt_anchors"] = {"ok": bool(anc_ok), "detail": str(anc_det)}
        if not anc_ok:
            failures.append(f"prompt_anchors: {anc_det}")
    except Exception as exc:
        checks["prompt_anchors"] = {"ok": False, "detail": str(exc)}
        failures.append(f"prompt_anchors error: {exc}")

    # 3. Early-bound upstream imports rebound cleanly
    rebind_errors: List[str] = []
    try:
        import config.config_loader as cfg_loader
        import config.settings as cfg_settings
        import core.handle.abortHandle as abort_mod
        import core.handle.helloHandle as hello_mod
        import core.handle.intentHandler as intent_mod
        import core.handle.receiveAudioHandle as recv_mod
        import core.handle.sendAudioHandle as send_mod
        import core.handle.textHandler.listenMessageHandler as listen_mod

        pairs = [
            ("listenMessageHandler.send_tts_message", listen_mod.send_tts_message, send_mod.send_tts_message),
            ("helloHandle.send_tts_message", hello_mod.send_tts_message, send_mod.send_tts_message),
            ("receiveAudioHandle.handleAbortMessage", recv_mod.handleAbortMessage, abort_mod.handleAbortMessage),
            ("intentHandler.checkWakeupWords", intent_mod.checkWakeupWords, hello_mod.checkWakeupWords),
            ("config.settings.read_config", cfg_settings.read_config, cfg_loader.read_config),
        ]
        for label, left, right in pairs:
            if left is not right:
                rebind_errors.append(f"{label} not rebound")
    except Exception as exc:
        rebind_errors.append(str(exc))

    rebind_ok = len(rebind_errors) == 0
    checks["early_bound_rebind"] = {
        "ok": rebind_ok,
        "detail": "5/5 early-bound imports verified" if rebind_ok else "; ".join(rebind_errors),
    }
    if not rebind_ok:
        failures.append(f"early_bound_rebind: {checks['early_bound_rebind']['detail']}")

    # 4. Key upstream callable contracts remain intact
    callable_errors: List[str] = []
    try:
        from core.api.ota_handler import OTAHandler
        from core.api.vision_handler import VisionHandler
        from core.connection import ConnectionHandler
        from core.handle.receiveAudioHandle import no_voice_close_connect
        from core.providers.asr.sherpa_onnx_local import ASRProvider as SherpaASR
        from core.providers.llm.openai.openai import LLMProvider as OpenAILLM
        from core.websocket_server import WebSocketServer

        for label, fn in (
            ("OTAHandler.handle_post", OTAHandler.handle_post),
            ("WebSocketServer._handle_auth", WebSocketServer._handle_auth),
            ("VisionHandler.handle_post", VisionHandler.handle_post),
            ("SherpaASR.speech_to_text", SherpaASR.speech_to_text),
            ("no_voice_close_connect", no_voice_close_connect),
        ):
            if not inspect.iscoroutinefunction(fn):
                callable_errors.append(f"{label} not coroutine")
        for label, fn in (
            ("ConnectionHandler.chat", getattr(ConnectionHandler, "chat", None)),
            ("ConnectionHandler._handle_function_result", getattr(ConnectionHandler, "_handle_function_result", None)),
            ("OpenAILLM.response", getattr(OpenAILLM, "response", None)),
            ("OpenAILLM.response_with_functions", getattr(OpenAILLM, "response_with_functions", None)),
        ):
            if not callable(fn):
                callable_errors.append(f"{label} not callable")
    except Exception as exc:
        callable_errors.append(str(exc))

    call_ok = len(callable_errors) == 0
    checks["callable_contracts"] = {
        "ok": call_ok,
        "detail": "9/9 upstream callables verified" if call_ok else "; ".join(callable_errors),
    }
    if not call_ok:
        failures.append(f"callable_contracts: {checks['callable_contracts']['detail']}")

    overall_ok = len(failures) == 0
    return registry.record_contract_self_check(ok=overall_ok, checks=checks, failures=failures)

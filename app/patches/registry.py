"""Startup patch registry, upstream contract verification, and module-wide symbol rebinding.

Provides:
- P1-8: Upstream SERVER_VERSION + inspect.signature contract checks, patch status tracking
  (ok / skipped / failed), and /api/health degradation reporting.
- P1-12: Universal rebind(old_obj, new_obj) across sys.modules so early-imported module bindings
  (e.g. listenMessageHandler.send_tts_message) never bypass patched functions.
"""

from __future__ import annotations

import inspect
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_EXPECTED_SERVER_VERSION = "0.9.8"
VALID_PATCH_STATUSES = frozenset({"ok", "partial", "skipped", "skipped_contract", "failed"})
VALID_PATCH_CATEGORIES = frozenset({"security", "experience"})
SECURITY_PATCH_NAMES = frozenset(
    {
        "config_env_expand",
        "esp32_auth_ota",
        "esp32_auth_ws",
        "esp32_auth_vision",
        "mcp_token_injection",
    }
)
_INSECURE_BOOT_ENV = "XIAOZHI_ALLOW_INSECURE_BOOT"

_lock = threading.RLock()
_patches: Dict[str, Dict[str, Any]] = {}
_version_info: Dict[str, Any] = {
    "checked": False,
    "expected": DEFAULT_EXPECTED_SERVER_VERSION,
    "actual": "",
    "match": True,
    "detail": "",
}
_contract_self_check: Dict[str, Any] = {
    "ran": False,
    "ok": True,
    "checks": {},
    "failures": [],
    "checked_at": 0.0,
}

# Declarative catalog of all startup monkey-patches (source of truth for docs/patches.md, I-1)
PATCH_CATALOG: List[Dict[str, Any]] = [
    {
        "name": "config_env_expand",
        "module": "app/patches/auth.py",
        "target": "config.config_loader.read_config",
        "category": "security",
        "contract": "sync callable, >=1 positional arg; rebinds config.settings.read_config",
        "failure_behavior": "fail-closed (sys.exit(2) unless XIAOZHI_ALLOW_INSECURE_BOOT=1 -> /api/health 503 + red banner)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "mcp_token_injection",
        "module": "app/patches/auth.py",
        "target": "core.providers.tools.server_mcp.mcp_manager.ServerMCPManager.load_config",
        "category": "security",
        "contract": "sync method accepting (self, ...)",
        "failure_behavior": "fail-closed (sys.exit(2) unless XIAOZHI_ALLOW_INSECURE_BOOT=1 -> /api/health 503 + red banner)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "esp32_auth_ota",
        "module": "app/patches/auth.py",
        "target": "core.api.ota_handler.OTAHandler.handle_post",
        "category": "security",
        "contract": "async coroutine accepting (self, request)",
        "failure_behavior": "fail-closed (sys.exit(2) unless XIAOZHI_ALLOW_INSECURE_BOOT=1 -> /api/health 503 + red banner)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "esp32_auth_ws",
        "module": "app/patches/auth.py",
        "target": "core.websocket_server.WebSocketServer.{_handle_auth,__init__} / ConnectionHandler.close",
        "category": "security",
        "contract": "async _handle_auth(self, websocket), sync __init__(self, config), async ConnectionHandler.close(self, ws=None)",
        "failure_behavior": "fail-closed (sys.exit(2) unless XIAOZHI_ALLOW_INSECURE_BOOT=1 -> /api/health 503 + red banner)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "esp32_auth_vision",
        "module": "app/patches/auth.py",
        "target": "core.api.vision_handler.VisionHandler.{_verify_auth_token,handle_post}",
        "category": "security",
        "contract": "sync _verify_auth_token(self, request) and async handle_post(self, request)",
        "failure_behavior": "fail-closed (sys.exit(2) unless XIAOZHI_ALLOW_INSECURE_BOOT=1 -> /api/health 503 + red banner)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "anima_memory_factory",
        "module": "app/patches/memory_hooks.py",
        "target": "core.utils.memory.create_instance",
        "category": "experience",
        "contract": "sync callable accepting >=2 positional args (class_name, *args, **kwargs)",
        "failure_behavior": "fail-soft (falls back to upstream memory factory, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_batch3_anima_outbox.py"],
    },
    {
        "name": "anima_dialogue_hooks",
        "module": "app/patches/memory_hooks.py",
        "target": "core.utils.dialogue.Dialogue.{put,get_llm_dialogue_with_memory}",
        "category": "experience",
        "contract": "sync methods put(self, message) and get_llm_dialogue_with_memory(self, memory_str, ...)",
        "failure_behavior": "fail-soft (skips mount on contract mismatch, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_batch3_pipeline_vad.py"],
    },
    {
        "name": "anima_turn_flush",
        "module": "app/patches/memory_hooks.py",
        "target": "core.connection.ConnectionHandler.{_background_initialize,_initialize_memory,chat,_handle_function_result,close}",
        "category": "experience",
        "contract": "async _background_initialize(self) / close(self, ...) and sync _initialize_memory / chat / _handle_function_result; pin the archive before handling device messages",
        "failure_behavior": "fail-soft (falls back to debounced loop flush, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_batch3_pipeline_vad.py"],
    },
    {
        "name": "nmc_weather",
        "module": "app/patches/weather.py",
        "target": "plugins_func.functions.get_weather.get_weather",
        "category": "experience",
        "contract": "registered ToolType.SYSTEM_CTL plugin callable accepting (conn, location, lang)",
        "failure_behavior": "fail-soft (retains upstream weather plugin, marks /api/health degraded)",
        "tests": ["tests/container/test_batch3_pipeline_vad.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "pc_audio_tts",
        "module": "app/patches/pc_audio.py",
        "target": "core.providers.tts.base.TTSProviderBase.to_tts_stream",
        "category": "experience",
        "contract": "sync method accepting (self, text, opus_handler, ...); must install before mei_emotion_and_prosody",
        "failure_behavior": "fail-soft (keeps ESP32 speaker path, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_batch3_pipeline_vad.py"],
    },
    {
        "name": "mei_prompt_anchors",
        "module": "app/patches/emotion/prompt.py",
        "target": "agent-base-prompt.txt (4 anchors) via PromptManager.build_enhanced_prompt",
        "category": "experience",
        "contract": "4 prompt-harmony anchor strings present in upstream agent-base-prompt.txt",
        "failure_behavior": "fail-soft per anchor (skips missing anchor with one-shot warning, records partial -> /api/health 200 degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "mei_emotion_and_prosody",
        "module": "app/patches/emotion/tts_route.py",
        "target": "TTSProviderBase.{tts_text_priority_thread,to_tts_stream} / GPT_SOVITS_V2 / ConnectionHandler._clean_response_garbage / sendAudioHandle.send_tts_message",
        "category": "experience",
        "contract": "sync TTS/clean hooks + async send_tts_message(conn, state, text); rebinds early imports",
        "failure_behavior": "fail-soft (falls back to upstream TTS/prosody, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_batch3_pipeline_vad.py"],
    },
    {
        "name": "pc_audio_playback",
        "module": "app/patches/pc_audio.py",
        "target": "core.handle.sendAudioHandle.send_tts_message / helloHandle / abortHandle",
        "category": "experience",
        "contract": "async send_tts_message & handleAbortMessage; rebinds listenMessageHandler & receiveAudioHandle",
        "failure_behavior": "fail-soft (falls back to ESP32 playback, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_batch3_pipeline_vad.py"],
    },
    {
        "name": "mei_wakeup_responses",
        "module": "app/patches/emotion/wakeup.py",
        "target": "core.handle.helloHandle.{checkWakeupWords,handleHelloMessage} / intentHandler.checkWakeupWords",
        "category": "experience",
        "contract": "async checkWakeupWords(conn, text) & handleHelloMessage(conn, msg_json); must install after pc_audio_playback",
        "failure_behavior": "fail-soft (falls back to upstream wakeup replies, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "listen_interrupt_handler",
        "module": "app/patches/emotion/wakeup.py",
        "target": "core.handle.textHandler.listenMessageHandler.ListenTextMessageHandler.handle",
        "category": "experience",
        "contract": "async method handle(self, conn, msg_json)",
        "failure_behavior": "fail-soft (falls back to upstream listen handler, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "idle_farewell",
        "module": "app/patches/emotion/farewell.py",
        "target": "core.handle.receiveAudioHandle.no_voice_close_connect",
        "category": "experience",
        "contract": "async coroutine no_voice_close_connect(conn, have_voice); rebinds callers",
        "failure_behavior": "fail-soft (falls back to upstream idle timeout close, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "reply_length_llm",
        "module": "app/patches/reply_length.py",
        "target": "core.providers.llm.openai.openai.LLMProvider.{response,response_with_functions}",
        "category": "experience",
        "contract": "sync generators response(self, session_id, dialogue, ...) & response_with_functions(...)",
        "failure_behavior": "fail-soft (uses upstream LLM max_tokens & messages, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "llm_prefix_cache",
        "module": "app/turn_context_router.py",
        "target": "core.providers.llm.openai.openai.LLMProvider.{__init__,response,response_with_functions}",
        "category": "experience",
        "contract": "sync __init__(self, config) + generators response & response_with_functions; hot-rollback via turn_context_mode=legacy",
        "failure_behavior": "fail-soft (keeps legacy system prompt layout, marks /api/health degraded)",
        "tests": ["tests/container/test_mei_emotion_and_prosody.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "vad_speculative",
        "module": "app/vad_speculative_router.py",
        "target": "ASRProviderBase.{receive_audio,handle_voice_stop,speech_to_text_wrapper} / ConnectionHandler.reset_audio_states / receiveAudioHandle.handleAudioMessage",
        "category": "experience",
        "contract": "async receive_audio/handle_voice_stop/speech_to_text_wrapper/handleAudioMessage + sync reset_audio_states; hot-toggleable to pure 1100ms VAD",
        "failure_behavior": "fail-soft (skips mount on contract mismatch -> pure 1100ms VAD, marks /api/health degraded)",
        "tests": ["tests/container/test_batch3_pipeline_vad.py", "tests/container/test_batch3_console.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "sherpa_async_asr",
        "module": "app/patches/asr.py",
        "target": "core.providers.asr.sherpa_onnx_local.ASRProvider.speech_to_text / ASRProviderBase.speech_to_text_wrapper",
        "category": "experience",
        "contract": "async coroutine speech_to_text(self, opus_data, session_id, artifacts=None); offloads decode_stream to dedicated 1-thread asr executor and applies G-4 ASR corrections across all ASR providers (incl. FunASR)",
        "failure_behavior": "fail-soft (runs upstream sync decode, marks /api/health degraded)",
        "tests": ["tests/container/test_batch3_pipeline_vad.py", "tests/container/test_upstream_contracts.py"],
    },
    {
        "name": "cloud_audio_api",
        "module": "app/patches/cloud_audio.py",
        "target": "core.providers.{asr.{openai,qwen3_asr_flash,aliyunbl_stream,doubao,doubao_stream},tts.{openai,siliconflow,doubao,fishspeech,alibl_stream,huoshan_double_stream,minimax_httpstream}}",
        "category": "experience",
        "contract": "async speech_to_text / receive_audio / text_to_speak / start_session / finish_session / open_audio_channels + sync {DoubaoTTS,DoubaoASR,DoubaoStreamASR,AliyunBLStreamASR,MinimaxTTSHTTPStream}.__init__; non-blocking cloud HTTP, SSRF/secret redaction, ESP32 emotion-tag sync, and 48kHz/44.1kHz PC speaker passthrough",
        "failure_behavior": "fail-soft (skips mismatched cloud provider targets, marks /api/health degraded)",
        "tests": ["tests/container/test_cloud_audio_api.py", "tests/container/test_upstream_contracts.py"],
    },
]


def get_patch_catalog() -> List[Dict[str, Any]]:
    """Return a copy of the declarative patch catalog (source of truth for docs/patches.md)."""
    return [dict(item, tests=list(item.get("tests") or [])) for item in PATCH_CATALOG]


def record_contract_self_check(
    *,
    ok: bool,
    checks: Dict[str, Dict[str, Any]],
    failures: List[str],
) -> Dict[str, Any]:
    """Record startup lightweight contract self-check results into the registry (H-1)."""
    snap = {
        "ran": True,
        "ok": bool(ok),
        "checks": {k: dict(v) for k, v in (checks or {}).items()},
        "failures": list(failures or []),
        "checked_at": time.time(),
    }
    with _lock:
        _contract_self_check.clear()
        _contract_self_check.update(snap)
    return dict(snap)


def get_contract_self_check() -> Dict[str, Any]:
    with _lock:
        return {
            "ran": bool(_contract_self_check.get("ran", False)),
            "ok": bool(_contract_self_check.get("ok", True)),
            "checks": {k: dict(v) for k, v in (_contract_self_check.get("checks") or {}).items()},
            "failures": list(_contract_self_check.get("failures") or []),
            "checked_at": float(_contract_self_check.get("checked_at") or 0.0),
        }


def get_expected_server_version() -> str:
    env_v = (os.environ.get("XIAOZHI_EXPECTED_SERVER_VERSION") or "").strip()
    return env_v or DEFAULT_EXPECTED_SERVER_VERSION


def reset_registry() -> None:
    """Clear registry state (primarily for isolated unit tests)."""
    with _lock:
        _patches.clear()
        _version_info.update(
            {
                "checked": False,
                "expected": get_expected_server_version(),
                "actual": "",
                "match": True,
                "detail": "",
            }
        )
        _contract_self_check.clear()
        _contract_self_check.update(
            {
                "ran": False,
                "ok": True,
                "checks": {},
                "failures": [],
                "checked_at": 0.0,
            }
        )


def check_upstream_server_version(
    expected_version: Optional[str] = None,
    actual_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare upstream config.logger.SERVER_VERSION against expected contract version."""
    expected = (expected_version or get_expected_server_version()).strip()
    actual = (actual_version or "").strip() if actual_version is not None else ""
    detail = ""
    if actual_version is None:
        try:
            import config.logger as _cfg_logger

            actual = str(getattr(_cfg_logger, "SERVER_VERSION", "") or "").strip()
            if not actual:
                detail = "config.logger.SERVER_VERSION missing or empty"
        except Exception as exc:
            detail = f"Failed to import config.logger.SERVER_VERSION: {exc}"
            actual = ""

    match = bool(actual) and actual == expected
    if not match and not detail:
        detail = f"Upstream SERVER_VERSION mismatch: expected {expected!r}, got {actual!r}"

    info = {
        "checked": True,
        "expected": expected,
        "actual": actual,
        "match": match,
        "detail": detail,
    }
    with _lock:
        _version_info.update(info)
    return dict(info)


def verify_signature_contract(
    func: Any,
    *,
    required_params: Iterable[str] = (),
    min_positional: Optional[int] = None,
    require_async: Optional[bool] = None,
) -> Tuple[bool, str]:
    """Inspect a callable's signature to validate upstream compatibility before patching.

    Follows `__wrapped__` if present so idempotent re-checks inspect the underlying contract.
    """
    if func is None:
        return False, "target is None or not callable"

    # Unwrap staticmethod / classmethod descriptors BEFORE callable() check (classmethod is not callable!)
    raw_fn = func
    if isinstance(raw_fn, (staticmethod, classmethod)):
        raw_fn = raw_fn.__func__

    if raw_fn is None or not callable(raw_fn):
        return False, "target is None or not callable"

    if require_async is not None:
        is_coro = inspect.iscoroutinefunction(raw_fn)
        if not is_coro and hasattr(raw_fn, "__wrapped__"):
            is_coro = inspect.iscoroutinefunction(getattr(raw_fn, "__wrapped__"))
        if is_coro != require_async:
            want = "async coroutine" if require_async else "sync callable"
            return False, f"expected {want}, got {'async' if is_coro else 'sync'}"

    try:
        sig = inspect.signature(raw_fn, follow_wrapped=True)
    except (TypeError, ValueError) as exc:
        return False, f"inspect.signature failed: {exc}"

    params = sig.parameters
    has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values())
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    req_list = [str(p) for p in required_params if p]
    missing = [
        name
        for name in req_list
        if name not in params and not (has_var_pos and has_var_kw)
    ]
    if missing:
        return (
            False,
            f"missing expected parameter(s) {missing} in signature {sig}",
        )

    if min_positional is not None and not has_var_pos:
        pos_count = sum(
            1
            for p in params.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
        if pos_count < int(min_positional):
            return (
                False,
                f"expected at least {min_positional} positional param(s), found {pos_count} in {sig}",
            )

    return True, str(sig)


def _resolve_category(name: str, category: Optional[str]) -> str:
    if category is not None:
        cat = str(category).strip().lower()
        if cat in VALID_PATCH_CATEGORIES:
            return cat
    return "security" if str(name) in SECURITY_PATCH_NAMES else "experience"


def is_insecure_boot_allowed() -> bool:
    raw = (os.environ.get(_INSECURE_BOOT_ENV) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def record_patch(
    name: str,
    status: str,
    *,
    target: str = "",
    detail: str = "",
    contract_ok: bool = True,
    required: bool = True,
    category: Optional[str] = None,
    rebound: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Record the outcome of applying a monkey-patch into the global patch registry."""
    st = str(status or "failed").strip().lower()
    if st not in VALID_PATCH_STATUSES:
        st = "failed"
    cat = _resolve_category(str(name), category)
    entry: Dict[str, Any] = {
        "name": str(name),
        "status": st,
        "category": cat,
        "target": str(target or ""),
        "detail": str(detail or ""),
        "contract_ok": bool(contract_ok),
        "required": bool(required),
        "rebound": list(rebound) if rebound else [],
        "updated_at": time.time(),
    }
    with _lock:
        _patches[str(name)] = entry
    return dict(entry)


def install_patch(
    name: str,
    target_obj: Any,
    attr_name: str,
    wrapper_factory: Callable[[Any], Any],
    *,
    category: Optional[str] = None,
    target: str = "",
    required: bool = True,
    required_params: Iterable[str] = (),
    min_positional: Optional[int] = None,
    require_async: Optional[bool] = None,
    rebind_modules: bool = False,
    rebind_prefixes: Optional[Iterable[str]] = ("core.", "config.", "plugins_func."),
    flag_obj: Any = None,
    flag_attr: Optional[str] = None,
    as_staticmethod: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """Verify signature contract and install a monkey-patch atomically; skip mounting on contract mismatch (C-3)."""
    target_label = target or f"{getattr(target_obj, '__name__', type(target_obj).__name__)}.{attr_name}"
    cat = _resolve_category(name, category)

    if flag_obj is not None and flag_attr and getattr(flag_obj, flag_attr, False):
        existing = get_patch_status(name)
        if existing is None:
            existing = record_patch(
                name,
                "ok",
                target=target_label,
                detail="already applied",
                contract_ok=True,
                required=required,
                category=cat,
            )
        return existing.get("status") == "ok" and bool(existing.get("contract_ok", True)), existing

    orig: Any = None
    if isinstance(target_obj, type) and attr_name in target_obj.__dict__:
        orig = target_obj.__dict__[attr_name]
    else:
        orig = getattr(target_obj, attr_name, None)

    if orig is None:
        entry = record_patch(
            name,
            "failed",
            target=target_label,
            detail=f"target attribute {attr_name!r} missing",
            contract_ok=False,
            required=required,
            category=cat,
        )
        return False, entry

    is_static = isinstance(orig, staticmethod)
    is_cls = isinstance(orig, classmethod)
    raw_orig = orig.__func__ if (is_static or is_cls) else orig

    ok_sig, det_sig = verify_signature_contract(
        orig,
        required_params=required_params,
        min_positional=min_positional,
        require_async=require_async,
    )
    if not ok_sig:
        entry = record_patch(
            name,
            "skipped_contract",
            target=target_label,
            detail=det_sig,
            contract_ok=False,
            required=required,
            category=cat,
        )
        print(
            f"[PatchContract] SKIPPED {name} ({cat}) due to signature contract mismatch on {target_label}: {det_sig}",
            flush=True,
        )
        return False, entry

    try:
        wrapped = wrapper_factory(raw_orig)
        if as_staticmethod or is_static:
            attr_val = wrapped if isinstance(wrapped, staticmethod) else staticmethod(wrapped)
        elif is_cls:
            attr_val = wrapped if isinstance(wrapped, classmethod) else classmethod(wrapped)
        else:
            attr_val = wrapped
        setattr(target_obj, attr_name, attr_val)
        if flag_obj is not None and flag_attr:
            setattr(flag_obj, flag_attr, True)
        rebound_list: List[str] = []
        if rebind_modules:
            rebind_new = wrapped.__func__ if isinstance(wrapped, (staticmethod, classmethod)) else wrapped
            rebound_list = rebind(raw_orig, rebind_new, module_prefixes=rebind_prefixes)
        entry = record_patch(
            name,
            "ok",
            target=target_label,
            detail=det_sig,
            contract_ok=True,
            required=required,
            category=cat,
            rebound=rebound_list,
        )
        return True, entry
    except Exception as exc:
        entry = record_patch(
            name,
            "failed",
            target=target_label,
            detail=str(exc),
            contract_ok=False,
            required=required,
            category=cat,
        )
        print(f"[PatchContract] FAILED {name} ({cat}) on {target_label}: {exc}", flush=True)
        return False, entry


def run_patch_installer(
    name: str,
    installer: Callable[[], Any],
    *,
    target: str = "",
    category: Optional[str] = None,
    required: bool = True,
    log_prefix: str = "[PatchContract]",
) -> bool:
    """Invoke a patch installer function and record failure in the registry if it raises."""
    cat = _resolve_category(name, category)
    try:
        installer()
        return True
    except Exception as exc:
        record_patch(
            name,
            "failed",
            target=target,
            detail=str(exc),
            contract_ok=False,
            required=required,
            category=cat,
        )
        print(f"{log_prefix} 补丁加载异常 ({name}): {exc}", flush=True)
        return False


def get_failed_security_patches(
    required_names: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Return all registered security-category patches that did not cleanly install,
    plus any missing required security patch names when `required_names` is specified."""
    with _lock:
        patches_copy = {k: dict(v) for k, v in _patches.items()}
    failed: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for name in sorted(patches_copy.keys()):
        entry = patches_copy[name]
        cat = entry.get("category") or _resolve_category(entry.get("name", ""), None)
        if cat != "security":
            continue
        if entry.get("status") != "ok" or not bool(entry.get("contract_ok", True)):
            failed.append(entry)
            seen.add(name)
    if required_names is not None:
        for req_name in sorted(set(str(n) for n in required_names if n)):
            if req_name not in patches_copy and req_name not in seen:
                failed.append(
                    {
                        "name": req_name,
                        "status": "missing",
                        "category": "security",
                        "target": req_name,
                        "detail": "required security patch was not registered at startup",
                        "contract_ok": False,
                        "required": True,
                    }
                )
    return failed


def enforce_security_patches_or_exit(
    exit_code: int = 2,
    *,
    required_security_patches: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Fail-closed boot gate (C-1): abort process with exit_code=2 if any security patch failed or is missing,
    unless XIAOZHI_ALLOW_INSECURE_BOOT=1 is explicitly set."""
    failed = get_failed_security_patches(required_names=required_security_patches)
    if not failed:
        return []

    lines = [
        "================================================================",
        "[SECURITY BOOT GATE] 安全类补丁加载失败、缺失或契约不匹配，拒绝默认放行启动！",
    ]
    for entry in failed:
        lines.append(
            f"  - {entry.get('name')} (status={entry.get('status')}, "
            f"target={entry.get('target')}): {entry.get('detail') or 'unknown error'}"
        )
    lines.append(
        "修复建议: 请检查上游镜像版本 (docker-compose.yml digest) 或运行 "
        "`python scripts/run_tests.py --layer contract` 排查签名变化。"
    )
    if is_insecure_boot_allowed():
        for entry in failed:
            if entry.get("status") == "missing":
                record_patch(
                    str(entry.get("name") or ""),
                    "failed",
                    target=str(entry.get("target") or ""),
                    detail=str(entry.get("detail") or "required security patch not registered"),
                    contract_ok=False,
                    category="security",
                )
        lines.append(
            f"[WARNING] 检测到 {_INSECURE_BOOT_ENV}=1，已按应急开关继续启动（不安全降级模式，"
            "18080 控制台将持续挂出红色告警横幅，/api/health 将返回 HTTP 503）。"
        )
        lines.append("================================================================")
        print("\n".join(lines), flush=True)
        return failed

    lines.append(
        f"若确需临时应急排障，请显式设置环境变量 {_INSECURE_BOOT_ENV}=1 后重启容器。"
    )
    lines.append("================================================================")
    print("\n".join(lines), file=sys.stderr, flush=True)
    sys.exit(int(exit_code))


def get_patch_status(name: str) -> Optional[Dict[str, Any]]:
    with _lock:
        entry = _patches.get(name)
        return dict(entry) if entry is not None else None


def get_all_patches() -> Dict[str, Dict[str, Any]]:
    with _lock:
        return {k: dict(v) for k, v in _patches.items()}


def get_health_summary() -> Dict[str, Any]:
    """Return structured health & patch contract status for /api/health."""
    with _lock:
        patches_copy = {k: dict(v) for k, v in _patches.items()}
        ver = dict(_version_info)

    counts = {"ok": 0, "partial": 0, "skipped": 0, "skipped_contract": 0, "failed": 0}
    degraded_items: List[str] = []
    degraded_reasons: List[str] = []
    security_failures: List[str] = []

    if ver.get("checked") and not ver.get("match", True):
        degraded_items.append("upstream_version")
        degraded_reasons.append(
            ver.get("detail")
            or f"upstream SERVER_VERSION mismatch ({ver.get('actual')} != {ver.get('expected')})"
        )

    for pname, entry in sorted(patches_copy.items()):
        st = entry.get("status", "failed")
        cat = entry.get("category") or _resolve_category(pname, None)
        entry["category"] = cat
        if st in counts:
            counts[st] += 1
        req = bool(entry.get("required", True))
        c_ok = bool(entry.get("contract_ok", True))
        is_problem = False
        if st == "failed":
            is_problem = True
            degraded_reasons.append(f"patch {pname} failed: {entry.get('detail') or 'unknown error'}")
        elif st == "skipped_contract":
            is_problem = True
            degraded_reasons.append(
                f"patch {pname} skipped (contract mismatch): {entry.get('detail') or 'signature drift'}"
            )
        elif st == "partial":
            is_problem = True
            degraded_reasons.append(
                f"patch {pname} partial: {entry.get('detail') or 'partial degradation'}"
            )
        elif not c_ok:
            is_problem = True
            degraded_reasons.append(
                f"patch {pname} contract mismatch: {entry.get('detail') or 'signature drift'}"
            )
        elif st == "skipped" and req:
            is_problem = True
            degraded_reasons.append(
                f"required patch {pname} skipped: {entry.get('detail') or 'missing target'}"
            )

        if is_problem:
            degraded_items.append(pname)
            if cat == "security":
                security_failures.append(pname)

    contract_sc = get_contract_self_check()
    if contract_sc.get("ran") and not contract_sc.get("ok", True):
        if "contract_self_check" not in degraded_items:
            degraded_items.append("contract_self_check")
        for f_msg in contract_sc.get("failures") or []:
            degraded_reasons.append(f"contract_self_check: {f_msg}")

    security_ok = len(security_failures) == 0
    insecure_boot_env = is_insecure_boot_allowed()
    insecure_boot = (not security_ok) and insecure_boot_env
    has_degraded = len(degraded_reasons) > 0
    if not security_ok:
        overall_status = "unhealthy"
    elif has_degraded:
        overall_status = "degraded"
    else:
        overall_status = "ok"

    return {
        "status": overall_status,
        "degraded": degraded_items,
        "is_degraded": has_degraded,
        "security_ok": security_ok,
        "security_failures": security_failures,
        "insecure_boot": insecure_boot,
        "insecure_boot_env": insecure_boot_env,
        "server_version": ver.get("actual", ""),
        "expected_server_version": ver.get("expected", get_expected_server_version()),
        "version_match": bool(ver.get("match", True)),
        "counts": counts,
        "patches": patches_copy,
        "contract_self_check": contract_sc,
        "degraded_reasons": degraded_reasons,
    }


def rebind(
    old_obj: Any,
    new_obj: Any,
    *,
    module_prefixes: Optional[Iterable[str]] = ("core.", "config.", "plugins_func."),
    exclude_modules: Iterable[str] = (
        "boot",
        "facade_helper",
        "patch_registry",
        "runtime_events",
        "start_with_env",
        "mei_emotion_router",
        "pc_audio_router",
        "reply_length_router",
        "nmc_weather_patch",
        "console_server",
        "anima_memory_provider",
        "turn_context_router",
        "vad_speculative_router",
    ),
) -> List[str]:
    """Replace all module-level references pointing to `old_obj` across `sys.modules` with `new_obj`.

    Protects saved `_orig*` attributes and excludes our own patch modules by default to avoid
    accidentally creating recursion loops when patch wrappers retain references to originals.
    """
    if old_obj is None or new_obj is None or old_obj is new_obj:
        return []

    prefixes = tuple(module_prefixes) if module_prefixes is not None else None
    excluded = set(exclude_modules or ())
    rebound: List[str] = []

    for mod_name, mod in list(sys.modules.items()):
        if mod is None or mod_name in excluded or mod_name.startswith(("patches.", "console.")):
            continue
        if prefixes is not None and not any(
            mod_name == p.rstrip(".") or mod_name.startswith(p) for p in prefixes
        ):
            continue
        mod_dict = getattr(mod, "__dict__", None)
        if not isinstance(mod_dict, dict):
            continue
        for attr_name, val in list(mod_dict.items()):
            if attr_name.startswith("_orig"):
                continue
            if val is old_obj:
                try:
                    setattr(mod, attr_name, new_obj)
                    rebound.append(f"{mod_name}.{attr_name}")
                except Exception:
                    pass
    return rebound

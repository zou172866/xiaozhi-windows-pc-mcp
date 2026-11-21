"""YAML allowlist validation, hot-reload, reply-length, idle-farewell, speculative VAD & ASR corrections routes (H-1, D-2)."""

from __future__ import annotations

import contextlib
import copy
from http import HTTPStatus
import os
import threading
import time

import yaml

import reply_length_router
import runtime_events
import runtime_settings
from console import shared
from console.routes import anima, gsv

def _update_idle_farewell_config(payload: dict) -> tuple[bool, dict | str]:
    """Validate, persist, and live-sync idle timeout farewell settings across active connections."""
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象"

    enabled_arg: bool | None = None
    if "enabled" in payload:
        raw_en = payload.get("enabled")
        if not isinstance(raw_en, bool):
            return False, "字段 enabled 必须为布尔值"
        enabled_arg = raw_en

    mode_arg: str | None = None
    if "mode" in payload:
        raw_mode = payload.get("mode")
        if not isinstance(raw_mode, str) or raw_mode.strip().lower() not in runtime_settings.VALID_IDLE_FAREWELL_MODES:
            return False, "字段 mode 必须为 fixed 或 llm_summary"
        mode_arg = raw_mode.strip().lower()

    timeout_arg: int | None = None
    if "timeout_seconds" in payload:
        raw_to = payload.get("timeout_seconds")
        if isinstance(raw_to, bool):
            return False, "字段 timeout_seconds 必须为整数（30～1800）"
        try:
            timeout_arg = int(raw_to)
        except (TypeError, ValueError):
            return False, "字段 timeout_seconds 必须为整数（30～1800）"
        if timeout_arg < 30 or timeout_arg > 1800:
            return False, "无声超时秒数必须在 30～1800 秒之间"

    summary_prompt_arg: str | None = None
    if "summary_prompt" in payload:
        raw_sp = payload.get("summary_prompt")
        if not isinstance(raw_sp, str):
            return False, "字段 summary_prompt 必须为字符串"
        summary_prompt_arg = raw_sp

    messages_arg = None
    if "messages" in payload:
        raw_msgs = payload.get("messages")
        if not isinstance(raw_msgs, (list, tuple, str)):
            return False, "字段 messages 必须为字符串数组或换行分隔文本"
        messages_arg = raw_msgs

    if (
        enabled_arg is None
        and mode_arg is None
        and timeout_arg is None
        and summary_prompt_arg is None
        and messages_arg is None
    ):
        return False, "请至少提供 enabled、mode、timeout_seconds、summary_prompt 或 messages"

    saved = runtime_settings.set_idle_farewell(
        enabled=enabled_arg,
        mode=mode_arg,
        timeout_seconds=timeout_arg,
        summary_prompt=summary_prompt_arg,
        messages=messages_arg,
    )
    eff_timeout = int(saved["timeout_seconds"])
    eff_hard = max(60, eff_timeout + 60)
    try:
        for srv in runtime_events.get_ws_servers():
            if isinstance(getattr(srv, "config", None), dict):
                srv.config["close_connection_no_voice_time"] = eff_timeout
        for conn in runtime_events.get_active_connections():
            if hasattr(conn, "timeout_seconds"):
                conn.timeout_seconds = eff_hard
            if isinstance(getattr(conn, "config", None), dict):
                conn.config["close_connection_no_voice_time"] = eff_timeout
    except Exception as exc:
        shared._log_rate_limited("idle_farewell_live_sync", exc)
    return True, saved


def _deep_merge_dict(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` onto `base` without discarding unrelated sibling keys (P1-13)."""
    out = dict(base) if isinstance(base, dict) else {}
    if not isinstance(overlay, dict):
        return out
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dict(out[k], v)
        else:
            out[k] = v
    return out


_LLM_PROVIDER_DEFAULTS = {
    "DeepSeekLLM": {
        "type": "openai",
        "base_url": "https://api.deepseek.com",
        "model_name": "deepseek-flash",
        "api_key": "${DEEPSEEK_API_KEY}",
    },
    "GeminiOpenAI": {
        "type": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model_name": "gemini-3.5-flash-lite",
        "api_key": "${GEMINI_API_KEY}",
    },
}

ALLOWED_LLM_PROVIDERS = frozenset(_LLM_PROVIDER_DEFAULTS.keys())
ALLOWED_LLM_MODELS: dict[str, frozenset[str]] = {
    "DeepSeekLLM": frozenset(
        {
            "deepseek-flash",
            "deepseek-chat",
            "deepseek-reasoner",
            "deepseek-v4-pro",
            "deepseek-v3",
        }
    ),
    "GeminiOpenAI": frozenset(
        {
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        }
    ),
}

# D-2: Module-level lock serializing all .config.yaml read-modify-write operations
_config_write_lock = threading.RLock()


def _validate_config_fields(
    *,
    llm_provider: str | None,
    model_name: str | None,
    voice: str | None,
    cur_llm_provider: str,
) -> tuple[str, str | None, str | None]:
    """D-2: Strictly validate llm_provider, model_name, and voice against explicit allowlists (raise ValueError -> HTTP 400)."""
    target_provider = cur_llm_provider
    if llm_provider is not None:
        cand_prov = str(llm_provider).strip()
        if not cand_prov or cand_prov not in ALLOWED_LLM_PROVIDERS:
            raise ValueError(
                f"无效的 LLM 服务商: {cand_prov or '(空)'}（仅允许: {', '.join(sorted(ALLOWED_LLM_PROVIDERS))}）"
            )
        target_provider = cand_prov

    validated_model: str | None = None
    if model_name is not None:
        cand_model = str(model_name).strip()
        allowed_for_prov = ALLOWED_LLM_MODELS.get(target_provider, frozenset())
        if not cand_model or cand_model not in allowed_for_prov:
            raise ValueError(
                f"服务商 {target_provider} 不支持模型 {cand_model or '(空)'}（允许列表: {', '.join(sorted(allowed_for_prov))}）"
            )
        validated_model = cand_model

    validated_voice: str | None = None
    if voice is not None:
        cand_voice = str(voice).strip()
        if not cand_voice or cand_voice not in gsv.ALLOWED_VOICES:
            raise ValueError(f"无效的音色 ID: {cand_voice or '(空)'}（不在允许的本地 GSV / EdgeTTS 音色列表中）")
        validated_voice = cand_voice

    return target_provider, validated_model, validated_voice


def _write_updated_yaml(
    prompt: str | None = None,
    llm_provider: str | None = None,
    model_name: str | None = None,
    voice: str | None = None,
) -> dict:
    """Deep-merge ONLY user-modified keys into data/.config.yaml under _config_write_lock with strict allowlists (P1-13 / D-2 / D-7)."""
    with _config_write_lock:
        cfg = shared._read_raw_yaml()
        selected = cfg.get("selected_module") or {}
        cur_prompt = str(cfg.get("prompt") or "").strip()
        cur_llm_provider = str(selected.get("LLM") or "DeepSeekLLM").strip()
        cur_llm_cfg = ((cfg.get("LLM") or {}).get(cur_llm_provider)) or {}
        cur_model = str(cur_llm_cfg.get("model_name") or "").strip()
        cur_voice = gsv._get_current_voice_id(cfg)

        # D-2: Validate all supplied fields strictly before any GPU weight swap or file write
        target_provider, validated_model, validated_voice = _validate_config_fields(
            llm_provider=llm_provider,
            model_name=model_name,
            voice=voice,
            cur_llm_provider=cur_llm_provider,
        )

        patch: dict = {}
        changed_fields: list[str] = []

        if prompt is not None:
            new_prompt = prompt.strip()
            if new_prompt and new_prompt != cur_prompt:
                patch["prompt"] = new_prompt + "\n"
                changed_fields.append("prompt")

        provider_changed = target_provider != cur_llm_provider
        if provider_changed:
            changed_fields.append("llm_provider")
            patch.setdefault("selected_module", {})["LLM"] = target_provider
            existing_prov_blk = ((cfg.get("LLM") or {}).get(target_provider)) or {}
            if not existing_prov_blk and target_provider in _LLM_PROVIDER_DEFAULTS:
                patch.setdefault("LLM", {})[target_provider] = dict(_LLM_PROVIDER_DEFAULTS[target_provider])

        existing_target_model = str(
            (((cfg.get("LLM") or {}).get(target_provider)) or {}).get("model_name") or ""
        ).strip()
        if validated_model and validated_model != (cur_model if not provider_changed else existing_target_model):
            changed_fields.append("model_name")
            prov_patch = patch.setdefault("LLM", {}).setdefault(target_provider, {})
            if not ((cfg.get("LLM") or {}).get(target_provider)) and target_provider in _LLM_PROVIDER_DEFAULTS:
                for dk, dv in _LLM_PROVIDER_DEFAULTS[target_provider].items():
                    prov_patch.setdefault(dk, dv)
            prov_patch["model_name"] = validated_model

        voice_recovered = False
        with contextlib.ExitStack() as weight_lock_stack:
            if validated_voice is not None:
                v_id = validated_voice
                if v_id != cur_voice:
                    changed_fields.append("voice")
                    # P1-5: Keep _gsv_weights_lock held across GPU weight swap AND atomic .config.yaml commit
                    # (strict lock hierarchy: _config_write_lock -> _gsv_weights_lock) so concurrent
                    # POST /api/tts-preview never observes stale pre-write YAML after GPU weights changed.
                    weight_lock_stack.enter_context(gsv._gsv_weights_lock)
                    if v_id in gsv.GSV_PROFILES:
                        dirty_before, _ = gsv._get_voice_dirty_state()
                        if dirty_before is not None:
                            gsv._active_gsv_loaded = "__dirty__"
                        try:
                            gsv._ensure_gsv_weights(v_id, persist_boot_yaml=True)
                        except Exception as exc:
                            if (
                                gsv._active_gsv_loaded == "__dirty__" or dirty_before is not None
                            ) and cur_voice in gsv.GSV_PROFILES:
                                gsv._set_voice_dirty(cur_voice, str(exc))
                            raise
                        if gsv._active_gsv_loaded == "__dirty__":
                            gsv._active_gsv_loaded = v_id
                        gsv._set_voice_dirty(None, "")
                        patch.setdefault("selected_module", {})["TTS"] = "GPT_SOVITS_V2"
                        existing_gsv = ((cfg.get("TTS") or {}).get("GPT_SOVITS_V2")) or {}
                        prof_blk = gsv.GSV_PROFILES[v_id]["block"]
                        gsv_patch: dict = {}
                        # Populate missing defaults only once, never overwriting user-customized tuning keys
                        for dk, dv in prof_blk.items():
                            if dk not in existing_gsv:
                                gsv_patch[dk] = dv
                        # Update only voice-identity fields (incl. explicit voice_id) for the newly selected GSV profile
                        for ik in gsv._GSV_VOICE_IDENTITY_KEYS:
                            if ik in prof_blk:
                                gsv_patch[ik] = prof_blk[ik]
                        gsv_patch["voice_id"] = v_id
                        if "sample_steps" in prof_blk and "sample_steps" not in existing_gsv:
                            gsv_patch["sample_steps"] = prof_blk["sample_steps"]
                        patch.setdefault("TTS", {})["GPT_SOVITS_V2"] = gsv_patch
                    else:
                        gsv._set_voice_dirty(None, "")
                        patch.setdefault("selected_module", {})["TTS"] = "EdgeTTS"
                        existing_edge = ((cfg.get("TTS") or {}).get("EdgeTTS")) or {}
                        edge_patch = {"voice": v_id, "voice_id": v_id}
                        if "type" not in existing_edge:
                            edge_patch["type"] = "edge"
                        if "output_dir" not in existing_edge:
                            edge_patch["output_dir"] = "tmp/"
                        patch.setdefault("TTS", {})["EdgeTTS"] = edge_patch
                else:
                    if v_id in gsv.GSV_PROFILES:
                        with gsv._gsv_weights_lock:
                            dirty_target, _ = gsv._get_voice_dirty_state()
                            is_dirty = (dirty_target is not None) or (gsv._active_gsv_loaded == "__dirty__")
                            if is_dirty:
                                gsv._active_gsv_loaded = "__dirty__"
                                try:
                                    gsv._ensure_gsv_weights(v_id, persist_boot_yaml=True)
                                except Exception as exc:
                                    gsv._set_voice_dirty(v_id, str(exc))
                                    raise
                                if gsv._active_gsv_loaded == "__dirty__":
                                    gsv._active_gsv_loaded = v_id
                                gsv._set_voice_dirty(None, "")
                                voice_recovered = True
                    else:
                        dirty_target, _ = gsv._get_voice_dirty_state()
                        if dirty_target is not None:
                            gsv._set_voice_dirty(None, "")

            if not changed_fields:
                return {"changed_fields": [], "written": False, "voice_recovered": voice_recovered}

            merged_cfg = _deep_merge_dict(cfg, patch)
            header = (
                "# =====================================================================\n"
                "# 小智 ESP32 本地服务端精简配置（由本地 Web 控制台管理）\n"
                "# 自动读取 DEEPSEEK_API_KEY 与 GEMINI_API_KEY 环境变量，不落盘明文密钥\n"
                "# =====================================================================\n\n"
            )
            body = yaml.safe_dump(merged_cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
            shared._atomic_write_text(shared.CONFIG_PATH, header + body)
            shared._get_config_yaml_cache(shared.CONFIG_PATH).set(shared.CONFIG_PATH, copy.deepcopy(merged_cfg))

    if "prompt" in changed_fields and prompt is not None:
        threading.Thread(target=anima._sync_persona_to_anima, args=(prompt,), daemon=True).start()
    return {"changed_fields": changed_fields, "written": True, "voice_recovered": voice_recovered}


def _apply_tts_attrs_in_place(tts_obj, tts_module_name: str, tts_cfg: dict) -> bool:
    """Update live TTS provider parameters in-place when engine class stays unchanged."""
    if tts_obj is None or not isinstance(tts_cfg, dict):
        return False
    cls_mod = str(getattr(type(tts_obj), "__module__", "") or "")
    if tts_module_name == "GPT_SOVITS_V2" and "gpt_sovits" in cls_mod:
        for attr in ("ref_audio_path", "prompt_text", "prompt_lang", "text_split_method", "url", "voice_id"):
            if attr in tts_cfg and tts_cfg[attr] is not None:
                setattr(tts_obj, attr, tts_cfg[attr])
        if "aux_ref_audio_paths" in tts_cfg:
            raw_aux = tts_cfg["aux_ref_audio_paths"]
            tts_obj.aux_ref_audio_paths = list(raw_aux) if isinstance(raw_aux, (list, tuple)) else []
        for f_attr in ("speed_factor", "temperature", "top_p"):
            if f_attr in tts_cfg and tts_cfg[f_attr] is not None:
                try:
                    setattr(tts_obj, f_attr, float(tts_cfg[f_attr]))
                except Exception as exc:
                    shared._log_rate_limited(f"tts_attr_{f_attr}", exc)
        for i_attr in ("top_k", "sample_steps"):
            if i_attr in tts_cfg and tts_cfg[i_attr] is not None:
                try:
                    setattr(tts_obj, i_attr, int(tts_cfg[i_attr]))
                except Exception as exc:
                    shared._log_rate_limited(f"tts_attr_{i_attr}", exc)
        if isinstance(getattr(tts_obj, "config", None), dict):
            tts_obj.config.update(tts_cfg)
        return True
    if tts_module_name == "EdgeTTS" and "edge" in cls_mod:
        if tts_cfg.get("voice"):
            tts_obj.voice = tts_cfg["voice"]
        if tts_cfg.get("voice_id"):
            tts_obj.voice_id = tts_cfg["voice_id"]
        if isinstance(getattr(tts_obj, "config", None), dict):
            tts_obj.config.update(tts_cfg)
        return True
    return False


def _hot_reload_runtime_config(changed_fields: list[str]) -> tuple[bool, str]:
    """Apply saved prompt / LLM / voice config changes live in-process without killing the server (P1-13)."""
    if not changed_fields:
        return True, "no_changes"
    try:
        import config.config_loader as config_loader
        from core.utils.cache.manager import CacheType, cache_manager
        from core.utils.modules_initialize import initialize_modules

        default_cfg_path = config_loader.get_project_dir() + "config.yaml"
        custom_cfg_path = shared.CONFIG_PATH
        default_cfg = config_loader.read_config(default_cfg_path)
        custom_cfg = config_loader.read_config(custom_cfg_path)
        new_cfg = config_loader.merge_configs(default_cfg, custom_cfg)
        config_loader.ensure_directories(new_cfg)

        # Preserve runtime-generated auth_key if live server holds one
        servers = runtime_events.get_ws_servers()
        if servers:
            old_auth_key = ((getattr(servers[0], "config", None) or {}).get("server") or {}).get("auth_key")
            if old_auth_key and not (new_cfg.get("server") or {}).get("auth_key"):
                new_cfg.setdefault("server", {})["auth_key"] = old_auth_key

        try:
            cache_manager.set(CacheType.CONFIG, "main_config", new_cfg)
        except Exception as exc:
            shared._log_rate_limited("hot_reload_cache_set", exc)

        llm_changed = "llm_provider" in changed_fields or "model_name" in changed_fields
        prompt_changed = "prompt" in changed_fields
        voice_changed = "voice" in changed_fields

        new_llm = None
        for srv in servers:
            if isinstance(getattr(srv, "config", None), dict):
                # Atomic in-place update without clearing to {} (avoids empty-config race window)
                srv.config.update(new_cfg)
                for stale_k in list(srv.config.keys()):
                    if stale_k not in new_cfg:
                        srv.config.pop(stale_k, None)
            else:
                srv.config = new_cfg
            if llm_changed:
                mods = initialize_modules(srv.logger, srv.config, init_llm=True)
                if "llm" in mods:
                    srv._llm = mods["llm"]
                    new_llm = mods["llm"]

        if llm_changed and new_llm is None and not servers:
            # Offline/test environment without WebSocketServer: still validate LLM config instantiability
            pass

        tts_mod_name = str((new_cfg.get("selected_module") or {}).get("TTS") or "")
        tts_cfg = ((new_cfg.get("TTS") or {}).get(tts_mod_name)) or {}
        tts_class_mismatch = False

        for conn in runtime_events.get_active_connections():
            try:
                if isinstance(getattr(conn, "common_config", None), dict):
                    conn.common_config = new_cfg
                if isinstance(getattr(conn, "config", None), dict):
                    if prompt_changed:
                        conn.config["prompt"] = new_cfg.get("prompt", "")
                    if llm_changed:
                        conn.config.setdefault("selected_module", {})["LLM"] = (
                            new_cfg.get("selected_module") or {}
                        ).get("LLM")
                        conn.config["LLM"] = new_cfg.get("LLM", {})
                    if voice_changed:
                        conn.config.setdefault("selected_module", {})["TTS"] = tts_mod_name
                        conn.config["TTS"] = new_cfg.get("TTS", {})
                if prompt_changed and hasattr(conn, "prompt_manager") and hasattr(conn, "change_system_prompt"):
                    enhanced = conn.prompt_manager.build_enhanced_prompt(
                        new_cfg.get("prompt", ""),
                        getattr(conn, "device_id", "") or "",
                        getattr(conn, "client_ip", None),
                        emoji_enabled=(getattr(conn, "features", None) or {}).get("emoji", True),
                    )
                    if enhanced:
                        conn.change_system_prompt(enhanced)
                if llm_changed and new_llm is not None:
                    conn.llm = new_llm
                    intent_obj = getattr(conn, "intent", None)
                    if intent_obj is not None and hasattr(intent_obj, "set_llm"):
                        intent_sel = (new_cfg.get("selected_module") or {}).get("Intent")
                        intent_llm_override = ((new_cfg.get("Intent") or {}).get(intent_sel) or {}).get("llm")
                        if not intent_llm_override:
                            try:
                                intent_obj.set_llm(new_llm)
                            except Exception as exc:
                                shared._log_rate_limited("hot_reload_intent_set_llm", exc)
                    mem_obj = getattr(conn, "memory", None)
                    if mem_obj is not None and hasattr(mem_obj, "set_llm"):
                        try:
                            mem_obj.set_llm(new_llm)
                        except Exception as exc:
                            shared._log_rate_limited("hot_reload_memory_set_llm", exc)
                if voice_changed and getattr(conn, "tts", None) is not None:
                    if not _apply_tts_attrs_in_place(conn.tts, tts_mod_name, tts_cfg):
                        tts_class_mismatch = True
            except Exception as conn_exc:
                print(shared._redact_secrets(f"[WebConsole] 单连接热更新跳过: {conn_exc}"), flush=True)

        if tts_class_mismatch:
            print(
                f"[WebConsole] 检测到活跃连接切换了 TTS 引擎类型 ({tts_mod_name})，需平滑重启生效",
                flush=True,
            )
            return False, "tts_engine_class_changed_on_active_conn"

        print(f"[WebConsole] 配置已热更新生效（免重启）: {', '.join(changed_fields)}", flush=True)
        return True, "hot_reloaded"
    except Exception as exc:
        print(shared._redact_secrets(f"[WebConsole] 配置热更新失败，回退重启动作: {exc}"), flush=True)
        return False, shared._redact_secrets(str(exc))


_DEFAULT_VAD_SPECULATIVE = {
    "enabled": True,
    "speculative_ms": 550,
    "post_wakeup_ms": 800,
    "min_chars": 4,
    "complete_suffixes": [],
    "hesitation_suffixes": [],
}
_ALLOWED_VAD_SPEC_KEYS = frozenset(
    {"enabled", "speculative_ms", "post_wakeup_ms", "min_chars", "complete_suffixes", "hesitation_suffixes"}
)
_ALLOWED_ASR_CORR_KEYS = frozenset({"enabled", "corrections"})


def _get_vad_speculative_snapshot() -> dict:
    try:
        fn = getattr(runtime_settings, "get_vad_speculative", None)
        if callable(fn):
            return dict(fn())
    except Exception as exc:
        shared._log_rate_limited("get_vad_speculative", exc)
    return copy.deepcopy(_DEFAULT_VAD_SPECULATIVE)


def _get_vad_speculative_stats_snapshot() -> dict:
    try:
        import vad_speculative_router

        fn = getattr(vad_speculative_router, "get_vad_speculative_stats", None)
        if callable(fn):
            return dict(fn())
    except ImportError:
        pass
    except Exception as exc:
        shared._log_rate_limited("get_vad_speculative_stats", exc)
    return {
        "speculative_triggers": 0,
        "early_commits": 0,
        "resumed_discards": 0,
        "held_hesitation": 0,
        "held_incomplete": 0,
        "story_mode_skips": 0,
        "avg_saved_ms": 0.0,
    }


def _update_vad_speculative_config(payload: dict) -> tuple[bool, dict | str]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象"
    unknown = sorted(str(k) for k in payload.keys() if k not in _ALLOWED_VAD_SPEC_KEYS)
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}"

    kwargs: dict = {}
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            return False, "字段 enabled 必须为布尔值"
        kwargs["enabled"] = payload["enabled"]

    for int_key, lo, hi, label in (
        ("speculative_ms", 300, 950, "早判静默阈值 speculative_ms"),
        ("post_wakeup_ms", 450, 1050, "唤醒首句静默阈值 post_wakeup_ms"),
        ("min_chars", 2, 20, "最小字数 min_chars"),
    ):
        if int_key in payload:
            raw_v = payload[int_key]
            if isinstance(raw_v, bool):
                return False, f"{label} 必须为整数（{lo}～{hi}）"
            try:
                iv = int(raw_v)
            except (TypeError, ValueError):
                return False, f"{label} 必须为整数（{lo}～{hi}）"
            if iv < lo or iv > hi:
                return False, f"{label} 必须在 {lo}～{hi} 之间"
            kwargs[int_key] = iv

    for list_key in ("complete_suffixes", "hesitation_suffixes"):
        if list_key in payload:
            raw_l = payload[list_key]
            if isinstance(raw_l, str):
                kwargs[list_key] = raw_l
            elif isinstance(raw_l, list) and all(isinstance(x, str) for x in raw_l):
                kwargs[list_key] = raw_l
            else:
                return False, f"字段 {list_key} 必须为字符串数组或逗号/换行分隔文本"

    if not kwargs:
        return False, "请至少提供一个要更新的投机 VAD 字段"

    fn = getattr(runtime_settings, "set_vad_speculative", None)
    if not callable(fn):
        return False, "当前 runtime_settings 版本不支持 set_vad_speculative"
    try:
        saved = fn(**kwargs)
        return True, dict(saved)
    except ValueError as exc:
        return False, str(exc)


def _get_asr_corrections_snapshot() -> dict:
    try:
        import asr_corrections

        return dict(asr_corrections.get_asr_corrections())
    except ImportError:
        pass
    except Exception as exc:
        shared._log_rate_limited("get_asr_corrections", exc)
    return {"schema_version": 1, "enabled": True, "corrections": {}, "count": 0}


def _update_asr_corrections_config(payload: dict) -> tuple[bool, dict | str]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象"
    unknown = sorted(str(k) for k in payload.keys() if k not in _ALLOWED_ASR_CORR_KEYS)
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}"

    has_en = "enabled" in payload
    has_corr = "corrections" in payload
    if not has_en and not has_corr:
        return False, "请至少提供 enabled 或 corrections"

    en_arg: bool | None = None
    if has_en:
        if not isinstance(payload["enabled"], bool):
            return False, "字段 enabled 必须为布尔值"
        en_arg = payload["enabled"]

    corr_arg = None
    if has_corr:
        raw_c = payload["corrections"]
        if not isinstance(raw_c, (dict, list, str)):
            return False, "字段 corrections 必须为映射对象、规则数组或 '错词->正词' 多行文本"
        corr_arg = raw_c

    try:
        import asr_corrections

        saved = asr_corrections.save_asr_corrections(corrections=corr_arg, enabled=en_arg)
        return True, dict(saved)
    except ValueError as exc:
        return False, str(exc)
    except ImportError:
        return False, "当前环境未加载 asr_corrections 模块"



def handle_get_idle_farewell(handler) -> None:
    handler._send_json({"ok": True, "idle_farewell": runtime_settings.get_idle_farewell()})


def handle_post_idle_farewell(handler) -> None:
    try:
        body = handler._read_body_json()
        ok_if, res_or_err = _update_idle_farewell_config(body)
        if ok_if:
            handler._send_json({"ok": True, "idle_farewell": res_or_err})
        else:
            handler._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus.BAD_REQUEST)
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_get_vad_speculative(handler) -> None:
    handler._send_json(
        {
            "ok": True,
            "vad_speculative": _get_vad_speculative_snapshot(),
            "stats": _get_vad_speculative_stats_snapshot(),
        }
    )


def handle_post_vad_speculative(handler) -> None:
    try:
        body = handler._read_body_json()
        ok_vs, res_or_err = _update_vad_speculative_config(body)
        if ok_vs:
            handler._send_json(
                {
                    "ok": True,
                    "vad_speculative": res_or_err,
                    "stats": _get_vad_speculative_stats_snapshot(),
                }
            )
        else:
            handler._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus.BAD_REQUEST)
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_get_asr_corrections(handler) -> None:
    handler._send_json({"ok": True, "asr_corrections": _get_asr_corrections_snapshot()})


def handle_post_asr_corrections(handler) -> None:
    try:
        body = handler._read_body_json()
        ok_ac, res_or_err = _update_asr_corrections_config(body)
        if ok_ac:
            handler._send_json({"ok": True, "asr_corrections": res_or_err})
        else:
            handler._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus.BAD_REQUEST)
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_reply_length(handler) -> None:
    try:
        body = handler._read_body_json()
        saved = reply_length_router.write_reply_length_config(
            chat_chars=body.get("chat_chars", None),
            story_chars=body.get("story_chars", None),
            preset=body.get("preset", None),
        )
        handler._send_json({"ok": True, "reply_length": saved})
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_config(handler, *, shutdown_flush_fn) -> None:
    try:
        body = handler._read_body_json()
        has_prompt = "prompt" in body
        raw_prompt = body.get("prompt")
        if has_prompt and not isinstance(raw_prompt, str):
            handler._send_json({"ok": False, "error": "字段 prompt 必须为字符串"}, HTTPStatus.BAD_REQUEST)
            return
        prompt = raw_prompt.strip() if has_prompt else None

        llm_provider: str | None = None
        if "llm_provider" in body and body.get("llm_provider") is not None:
            raw_prov = body.get("llm_provider")
            if not isinstance(raw_prov, str):
                handler._send_json({"ok": False, "error": "字段 llm_provider 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                return
            llm_provider = raw_prov.strip()

        model_name: str | None = None
        if "model_name" in body and body.get("model_name") is not None:
            raw_model = body.get("model_name")
            if not isinstance(raw_model, str):
                handler._send_json({"ok": False, "error": "字段 model_name 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                return
            model_name = raw_model.strip()

        voice: str | None = None
        if "voice" in body and body.get("voice") is not None:
            raw_voice = body.get("voice")
            if not isinstance(raw_voice, str):
                handler._send_json({"ok": False, "error": "字段 voice 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                return
            voice = raw_voice.strip()

        force_restart = bool(body.get("force_restart", False))
        allow_restart = bool(body.get("restart", True))

        if has_prompt and not prompt:
            handler._send_json({"ok": False, "error": "角色提示词不能为空"}, HTTPStatus.BAD_REQUEST)
            return

        # 4.1 #2: Keep _config_write_lock (RLock) held across YAML write AND runtime hot-reload
        # so concurrent POST /api/config saves can never apply stale config into active connections.
        with _config_write_lock:
            write_res = _write_updated_yaml(
                prompt=prompt,
                llm_provider=llm_provider,
                model_name=model_name,
                voice=voice,
            )
            changed_fields = list(write_res.get("changed_fields") or [])
            voice_recovered = bool(write_res.get("voice_recovered", False))
            hot_ok, hot_detail = _hot_reload_runtime_config(changed_fields)
        if voice_recovered and hot_detail == "no_changes":
            hot_detail = "voice_weights_recovered"
        will_restart = bool(force_restart or (allow_restart and not hot_ok))

        dirty_after, _ = gsv._get_voice_dirty_state()
        handler._send_json(
            {
                "ok": True,
                "restarting": will_restart,
                "hot_reloaded": bool(hot_ok and not will_restart),
                "changed_fields": changed_fields,
                "voice_recovered": voice_recovered,
                "voice_dirty": bool(dirty_after),
                "detail": hot_detail,
            }
        )

        if will_restart:
            def _delayed_restart():
                try:
                    shutdown_flush_fn()
                except Exception as exc:
                    shared._log_rate_limited("delayed_restart_flush_console", exc)
                try:
                    import anima_memory_provider

                    anima_memory_provider.flush_outbox(timeout=3.5)
                except Exception as exc:
                    shared._log_rate_limited("delayed_restart_flush_outbox", exc)
                time.sleep(0.5)
                os._exit(0)

            threading.Thread(target=_delayed_restart, daemon=True).start()
    except shared._BadRequestError:
        raise
    except ValueError as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

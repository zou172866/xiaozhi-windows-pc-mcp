"""YAML allowlist validation, hot-reload, reply-length, idle-farewell, speculative VAD & ASR corrections routes (H-1, D-2)."""

from __future__ import annotations

import asyncio
import contextlib
import copy
from http import HTTPStatus
import os
import re
import threading
import time

import httpx
import yaml

from patches import auth as auth_patch
import persona_versions
import recall_guard
import reply_length_router
import runtime_events
import runtime_settings
from console import shared
from console.routes import anima, audio_api, gsv

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
    "HuihuiLocal": {
        "type": "openai",
        "base_url": "http://host.docker.internal:19340/v1",
        "model_name": "huihui-qwen3-14b-companion",
        "api_key": "lm-studio",  # Non-secret SDK placeholder for the local server.
        "max_tokens": 1024,
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
    "HuihuiLocal": frozenset({_LLM_PROVIDER_DEFAULTS["HuihuiLocal"]["model_name"]}),
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

def _check_huihui_available() -> None:
    local = _LLM_PROVIDER_DEFAULTS["HuihuiLocal"]
    try:
        response = httpx.get(f"{local['base_url']}/models", timeout=3.0, trust_env=False)
        response.raise_for_status()
        models = response.json()["data"]
        if not isinstance(models, list):
            raise ValueError("invalid model list")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("无法读取本机模型列表，请确认 LM Studio 的本地服务已在 19340 端口启动。") from exc
    if not any(isinstance(model, dict) and model.get("id") == local["model_name"] for model in models):
        raise ValueError(f"请先在 LM Studio 加载模型 {local['model_name']}，再切换到本地陪伴。")

# D-2: Module-level lock serializing all .config.yaml read-modify-write operations
_config_write_lock = threading.RLock()


def _validate_config_fields(
    *,
    llm_provider: str | None,
    model_name: str | None,
    voice: str | None,
    cur_llm_provider: str,
    asr_provider: str | None = None,
    asr_config: dict | None = None,
    cur_asr_provider: str = "SherpaASR",
    tts_provider: str | None = None,
    tts_config: dict | None = None,
    cur_tts_provider: str = "EdgeTTS",
) -> tuple[str, str | None, str | None, str, dict, str | None, dict]:
    """D-2: Strictly validate LLM / ASR / TTS / voice fields against explicit allowlists (raise ValueError -> HTTP 400)."""
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

    target_asr, validated_asr_cfg = audio_api.validate_asr_update(
        asr_provider, asr_config, cur_asr_provider=cur_asr_provider
    )

    target_tts: str | None = None
    validated_tts_cfg: dict = {}
    validated_voice: str | None = None

    if tts_provider is not None:
        cand_tts = str(tts_provider).strip()
        if not cand_tts or cand_tts not in audio_api.ALLOWED_TTS_PROVIDERS:
            raise ValueError(
                f"无效的 TTS 语音引擎: {cand_tts or '(空)'}（仅允许: {', '.join(sorted(audio_api.ALLOWED_TTS_PROVIDERS))}）"
            )
        target_tts = cand_tts
        if target_tts in audio_api.CLOUD_TTS_PROVIDERS:
            _, validated_tts_cfg = audio_api.validate_tts_cloud_update(target_tts, tts_config)
        elif target_tts == "GPT_SOVITS_V2":
            if voice is not None:
                cand_voice = str(voice).strip()
                if not cand_voice or cand_voice not in gsv.ALLOWED_GSV_VOICES:
                    raise ValueError(f"无效的本地 GPT-SoVITS 音色 ID: {cand_voice or '(空)'}")
                validated_voice = cand_voice
        elif target_tts == "EdgeTTS":
            if voice is not None:
                cand_voice = str(voice).strip()
                if not cand_voice or cand_voice not in gsv.ALLOWED_EDGE_VOICES:
                    raise ValueError(f"无效的 EdgeTTS 音色 ID: {cand_voice or '(空)'}")
                validated_voice = cand_voice
    else:
        if tts_config is not None:
            if cur_tts_provider in audio_api.CLOUD_TTS_PROVIDERS:
                target_tts = cur_tts_provider
                _, validated_tts_cfg = audio_api.validate_tts_cloud_update(cur_tts_provider, tts_config)
            else:
                raise ValueError("修改 tts_config 时必须指定对应的云端 tts_provider")
        if voice is not None:
            cand_voice = str(voice).strip()
            if not cand_voice or cand_voice not in gsv.ALLOWED_VOICES:
                raise ValueError(f"无效的音色 ID: {cand_voice or '(空)'}（不在允许的本地 GSV / EdgeTTS 音色列表中）")
            validated_voice = cand_voice

    return (
        target_provider,
        validated_model,
        validated_voice,
        target_asr,
        validated_asr_cfg,
        target_tts,
        validated_tts_cfg,
    )


def _write_updated_yaml(
    prompt: str | None = None,
    llm_provider: str | None = None,
    model_name: str | None = None,
    voice: str | None = None,
    asr_provider: str | None = None,
    asr_config: dict | None = None,
    tts_provider: str | None = None,
    tts_config: dict | None = None,
    expected_prompt_revision: int | None = None,
    expected_prompt_digest: str | None = None,
    persona_source: str = "console_save",
    persona_note: str = "",
    rolled_back_from: int | None = None,
) -> dict:
    """Deep-merge ONLY user-modified keys into data/.config.yaml under _config_write_lock with strict allowlists (P1-13 / D-2 / D-7)."""
    persona_rec: dict | None = None
    with _config_write_lock:
        cfg = shared._read_raw_yaml()
        selected = cfg.get("selected_module") or {}
        cur_prompt = str(cfg.get("prompt") or "").replace("\r\n", "\n").strip()
        cur_llm_provider = str(selected.get("LLM") or "DeepSeekLLM").strip()
        cur_asr_provider = str(selected.get("ASR") or "SherpaASR").strip()
        cur_tts_provider = str(selected.get("TTS") or "EdgeTTS").strip()
        cur_llm_cfg = ((cfg.get("LLM") or {}).get(cur_llm_provider)) or {}
        cur_model = str(cur_llm_cfg.get("model_name") or "").strip()
        cur_voice = gsv._get_current_voice_id(cfg)

        # Persona boundary & CAS precondition check BEFORE touching weights or disk
        if prompt is not None:
            cand_prompt = prompt.replace("\r\n", "\n").strip()
            if cand_prompt:
                persona_versions.validate_persona_boundaries(cand_prompt)
                if expected_prompt_revision is not None or expected_prompt_digest is not None:
                    persona_versions.verify_cas(
                        cur_prompt,
                        expected_revision=expected_prompt_revision,
                        expected_digest=expected_prompt_digest,
                        char_name=anima._infer_character_name(cur_prompt),
                    )

        # D-2: Validate all supplied fields strictly before any GPU weight swap or file write
        (
            target_provider,
            validated_model,
            validated_voice,
            target_asr,
            validated_asr_cfg,
            target_tts,
            validated_tts_cfg,
        ) = _validate_config_fields(
            llm_provider=llm_provider,
            model_name=model_name,
            voice=voice,
            cur_llm_provider=cur_llm_provider,
            asr_provider=asr_provider,
            asr_config=asr_config,
            cur_asr_provider=cur_asr_provider,
            tts_provider=tts_provider,
            tts_config=tts_config,
            cur_tts_provider=cur_tts_provider,
        )

        patch: dict = {}
        changed_fields: list[str] = []

        if prompt is not None:
            new_prompt = prompt.replace("\r\n", "\n").strip()
            if new_prompt and new_prompt != cur_prompt:
                patch["prompt"] = new_prompt + "\n"
                changed_fields.append("prompt")

        provider_changed = target_provider != cur_llm_provider
        if provider_changed and target_provider == "HuihuiLocal":
            _check_huihui_available()
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

        # ASR engine & non-secret config update
        asr_provider_changed = target_asr != cur_asr_provider
        existing_asr_blk = ((cfg.get("ASR") or {}).get(target_asr)) or {}
        asr_defaults = copy.deepcopy((audio_api.ASR_PROVIDERS.get(target_asr) or {}).get("defaults") or {})
        asr_patch_blk: dict = {}
        if asr_provider_changed:
            changed_fields.append("asr_provider")
            patch.setdefault("selected_module", {})["ASR"] = target_asr
            for dk, dv in asr_defaults.items():
                if dk not in existing_asr_blk:
                    asr_patch_blk[dk] = dv
        asr_cfg_changed = False
        for ak, av in validated_asr_cfg.items():
            cur_av = existing_asr_blk.get(ak, asr_defaults.get(ak))
            if av != cur_av:
                asr_patch_blk[ak] = av
                asr_cfg_changed = True
        if asr_cfg_changed:
            for dk, dv in asr_defaults.items():
                if dk not in existing_asr_blk and dk not in asr_patch_blk:
                    asr_patch_blk[dk] = dv
            changed_fields.append("asr_config")
        if asr_patch_blk:
            patch.setdefault("ASR", {})[target_asr] = asr_patch_blk

        # TTS engine & voice update (supports Cloud TTS, EdgeTTS, and GPU GPT-SoVITS)
        voice_recovered = False
        with contextlib.ExitStack() as weight_lock_stack:
            if target_tts in audio_api.CLOUD_TTS_PROVIDERS:
                gsv._set_voice_dirty(None, "")
                tts_provider_changed = target_tts != cur_tts_provider
                existing_tts_blk = ((cfg.get("TTS") or {}).get(target_tts)) or {}
                tts_defaults = copy.deepcopy((audio_api.TTS_PROVIDERS.get(target_tts) or {}).get("defaults") or {})
                tts_patch_blk: dict = {}
                if tts_provider_changed:
                    changed_fields.append("tts_provider")
                    patch.setdefault("selected_module", {})["TTS"] = target_tts
                    for dk, dv in tts_defaults.items():
                        if dk not in existing_tts_blk:
                            tts_patch_blk[dk] = dv
                tts_cfg_changed = False
                for tk, tv in validated_tts_cfg.items():
                    if target_tts == "MinimaxTTSHTTPStream" and tk == "speed":
                        existing_vs = existing_tts_blk.get("voice_setting") or {}
                        cur_tv = existing_vs.get("speed", existing_tts_blk.get("speed", 1.0))
                        if tv != cur_tv:
                            tts_patch_blk.setdefault("voice_setting", {})["speed"] = tv
                            tts_cfg_changed = True
                        continue
                    cur_tv = existing_tts_blk.get(tk, tts_defaults.get(tk))
                    if tv != cur_tv:
                        tts_patch_blk[tk] = tv
                        tts_cfg_changed = True
                if tts_cfg_changed:
                    for dk, dv in tts_defaults.items():
                        if dk not in existing_tts_blk and dk not in tts_patch_blk:
                            tts_patch_blk[dk] = dv
                    changed_fields.append("tts_config")
                if tts_patch_blk:
                    patch.setdefault("TTS", {})[target_tts] = tts_patch_blk
            else:
                eff_voice = validated_voice
                if eff_voice is None and target_tts == "GPT_SOVITS_V2" and cur_tts_provider != "GPT_SOVITS_V2":
                    eff_voice = gsv._resolve_gsv_voice_id(((cfg.get("TTS") or {}).get("GPT_SOVITS_V2")) or {})
                elif eff_voice is None and target_tts == "EdgeTTS" and cur_tts_provider != "EdgeTTS":
                    edge_blk = ((cfg.get("TTS") or {}).get("EdgeTTS")) or {}
                    cand_e = str(edge_blk.get("voice_id") or edge_blk.get("voice") or "zh-CN-XiaoxiaoNeural").strip()
                    eff_voice = cand_e if cand_e in gsv.ALLOWED_EDGE_VOICES else "zh-CN-XiaoxiaoNeural"

                if eff_voice is not None:
                    v_id = eff_voice
                    need_tts_switch = (
                        (v_id in gsv.GSV_PROFILES and cur_tts_provider != "GPT_SOVITS_V2")
                        or (v_id not in gsv.GSV_PROFILES and cur_tts_provider != "EdgeTTS")
                    )
                    if v_id != cur_voice or need_tts_switch:
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
                            gsv_patch.update(gsv._voice_sampling_settings(existing_gsv, v_id))
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
                            existing_gsv_same = ((cfg.get("TTS") or {}).get("GPT_SOVITS_V2")) or {}
                            if (
                                v_id == "__gsv_luoxiaohei__"
                                and existing_gsv_same.get("sample_steps") == 16
                                and existing_gsv_same.get("first_segment_sample_steps") in (None, 4, 8)
                            ):
                                changed_fields.append("voice")
                                patch.setdefault("TTS", {}).setdefault("GPT_SOVITS_V2", {}).update(
                                    {"sample_steps": 4, "first_segment_sample_steps": 4}
                                )
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
                if prompt is not None and prompt.strip():
                    st = persona_versions.ensure_synced_with_prompt(
                        cur_prompt, char_name=anima._infer_character_name(cur_prompt)
                    )
                    persona_rec = {
                        "revision": st["current_revision"],
                        "digest": st["current_digest"],
                    }
                return {
                    "changed_fields": [],
                    "written": False,
                    "voice_recovered": voice_recovered,
                    "persona": persona_rec,
                }

            merged_cfg = _deep_merge_dict(cfg, patch)
            merged_gsv = ((merged_cfg.get("TTS") or {}).get("GPT_SOVITS_V2"))
            if isinstance(merged_gsv, dict):
                for opt_k in ("sample_steps", "first_segment_sample_steps"):
                    if opt_k in merged_gsv and merged_gsv[opt_k] is None:
                        merged_gsv.pop(opt_k, None)
            header = (
                "# =====================================================================\n"
                "# 小智 ESP32 本地服务端精简配置（由本地 Web 控制台管理）\n"
                "# 全部 API Key 仅通过环境变量内存展开，严禁落盘明文密钥\n"
                "# 注意：手工修改本文件后，除 wakeup_words / interrupt_words 自动热同步外，其余字段请通过 18080 控制台保存或重启容器生效\n"
                "# =====================================================================\n\n"
            )
            body = yaml.safe_dump(merged_cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
            shared._atomic_write_text(shared.CONFIG_PATH, header + body)
            shared._get_config_yaml_cache(shared.CONFIG_PATH).set(shared.CONFIG_PATH, copy.deepcopy(merged_cfg))

            if "prompt" in changed_fields and prompt is not None:
                try:
                    persona_rec = persona_versions.record_persona_version(
                        prompt.strip(),
                        char_name=anima._infer_character_name(prompt),
                        expected_revision=expected_prompt_revision,
                        expected_digest=expected_prompt_digest,
                        current_prompt_on_disk=cur_prompt,
                        source=persona_source,
                        note=persona_note,
                        rolled_back_from=rolled_back_from,
                    )
                except Exception as pv_exc:
                    shared._log_rate_limited("record_persona_version", pv_exc)

    if "prompt" in changed_fields and prompt is not None:
        sync_rev = (persona_rec or {}).get("revision")
        sync_dig = str((persona_rec or {}).get("digest") or "")
        threading.Thread(
            target=anima._sync_persona_to_anima,
            args=(prompt,),
            kwargs={"revision": sync_rev, "digest": sync_dig},
            daemon=True,
        ).start()
    return {
        "changed_fields": changed_fields,
        "written": True,
        "voice_recovered": voice_recovered,
        "prev_asr_provider": cur_asr_provider,
        "new_asr_provider": target_asr,
        "persona": persona_rec,
    }


def _apply_tts_attrs_in_place(tts_obj, tts_module_name: str, tts_cfg: dict) -> bool:
    """Update live TTS provider parameters in-place when engine class stays unchanged."""
    if tts_obj is None or not isinstance(tts_cfg, dict):
        return False
    cls_mod = str(getattr(type(tts_obj), "__module__", "") or "")
    if tts_module_name == "GPT_SOVITS_V2" and "gpt_sovits" in cls_mod:
        for attr in ("ref_audio_path", "prompt_text", "prompt_lang", "text_split_method", "url"):
            if attr in tts_cfg and tts_cfg[attr] is not None:
                setattr(tts_obj, attr, tts_cfg[attr])
        raw_vid = tts_cfg.get("voice_id")
        tts_obj.voice_id = raw_vid.strip() if isinstance(raw_vid, str) else ""
        if "aux_ref_audio_paths" in tts_cfg:
            raw_aux = tts_cfg["aux_ref_audio_paths"]
            tts_obj.aux_ref_audio_paths = list(raw_aux) if isinstance(raw_aux, (list, tuple)) else []
        for f_attr in ("speed_factor", "temperature", "top_p"):
            if f_attr in tts_cfg and tts_cfg[f_attr] is not None:
                try:
                    setattr(tts_obj, f_attr, float(tts_cfg[f_attr]))
                except Exception as exc:
                    shared._log_rate_limited(f"tts_attr_{f_attr}", exc)
        if "sample_steps" in tts_cfg and tts_cfg["sample_steps"] is None:
            tts_obj.sample_steps = None
        if "first_segment_sample_steps" in tts_cfg and tts_cfg["first_segment_sample_steps"] is None:
            tts_obj.first_segment_sample_steps = None
        for i_attr in ("top_k", "sample_steps", "first_segment_sample_steps"):
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
        raw_vid = tts_cfg.get("voice_id")
        tts_obj.voice_id = raw_vid.strip() if isinstance(raw_vid, str) else ""
        if isinstance(getattr(tts_obj, "config", None), dict):
            tts_obj.config.update(tts_cfg)
        return True
    if tts_module_name == "OpenAITTS" and cls_mod.endswith(".openai"):
        for attr in ("api_url", "model", "voice"):
            if tts_cfg.get(attr):
                setattr(tts_obj, attr, tts_cfg[attr])
        if tts_cfg.get("format"):
            tts_obj.audio_file_type = tts_cfg["format"]
        if tts_cfg.get("speed") is not None:
            try:
                tts_obj.speed = float(tts_cfg["speed"])
            except (TypeError, ValueError) as exc:
                shared._log_rate_limited("tts_attr_openai_speed", exc)
        return True
    if tts_module_name == "CosyVoiceSiliconflow" and "siliconflow" in cls_mod:
        for attr in ("model", "voice"):
            if tts_cfg.get(attr):
                setattr(tts_obj, attr, tts_cfg[attr])
        if tts_cfg.get("response_format"):
            tts_obj.audio_file_type = tts_cfg["response_format"]
        if tts_cfg.get("speed") is not None:
            try:
                tts_obj.speed = float(tts_cfg["speed"])
            except (TypeError, ValueError) as exc:
                shared._log_rate_limited("tts_attr_sf_speed", exc)
        return True
    if tts_module_name == "DoubaoTTS" and cls_mod.endswith(".doubao"):
        for attr in ("cluster", "voice", "api_url"):
            if tts_cfg.get(attr):
                setattr(tts_obj, attr, tts_cfg[attr])
        if tts_cfg.get("format"):
            tts_obj.audio_file_type = tts_cfg["format"]
        if tts_cfg.get("speed_ratio") is not None:
            try:
                tts_obj.speed_ratio = float(tts_cfg["speed_ratio"])
            except (TypeError, ValueError) as exc:
                shared._log_rate_limited("tts_attr_doubao_speed", exc)
        return True
    if tts_module_name == "FishSpeech" and "fishspeech" in cls_mod:
        if tts_cfg.get("api_url"):
            tts_obj.api_url = tts_cfg["api_url"]
        if "reference_id" in tts_cfg:
            tts_obj.reference_id = tts_cfg["reference_id"] or None
        if tts_cfg.get("response_format"):
            tts_obj.audio_file_type = tts_cfg["response_format"]
        return True
    if tts_module_name == "AliBLTTS" and "alibl_stream" in cls_mod:
        for attr in ("model", "voice"):
            if tts_cfg.get(attr):
                setattr(tts_obj, attr, tts_cfg[attr])
        if tts_cfg.get("rate") is not None:
            try:
                tts_obj.rate = float(tts_cfg["rate"])
            except (TypeError, ValueError) as exc:
                shared._log_rate_limited("tts_attr_alibl_rate", exc)
        return True
    if tts_module_name == "HuoshanDoubleStreamTTS" and "huoshan_double_stream" in cls_mod:
        if tts_cfg.get("resource_id"):
            tts_obj.resource_id = tts_cfg["resource_id"]
        if tts_cfg.get("speaker"):
            tts_obj.voice = tts_cfg["speaker"]
        return True
    if tts_module_name == "MinimaxTTSHTTPStream" and "minimax_httpstream" in cls_mod:
        if tts_cfg.get("model"):
            tts_obj.model = tts_cfg["model"]
        if tts_cfg.get("voice_id"):
            tts_obj.voice = tts_cfg["voice_id"]
            if isinstance(getattr(tts_obj, "voice_setting", None), dict):
                tts_obj.voice_setting["voice_id"] = tts_cfg["voice_id"]
        vs_cfg = tts_cfg.get("voice_setting") if isinstance(tts_cfg.get("voice_setting"), dict) else {}
        if "speed" in vs_cfg and isinstance(getattr(tts_obj, "voice_setting", None), dict):
            try:
                tts_obj.voice_setting["speed"] = float(vs_cfg["speed"])
            except (TypeError, ValueError) as exc:
                shared._log_rate_limited("tts_attr_minimax_speed", exc)
        return True
    return False


def _apply_asr_attrs_in_place(asr_obj, asr_module_name: str, asr_cfg: dict) -> bool:
    """Update live cloud ASR provider parameters in-place when engine class stays unchanged."""
    if asr_obj is None or not isinstance(asr_cfg, dict):
        return False
    cls_mod = str(getattr(type(asr_obj), "__module__", "") or "")
    if asr_module_name in ("SiliconFlowASR", "OpenaiASR") and cls_mod.endswith(".openai"):
        if asr_cfg.get("base_url"):
            asr_obj.api_url = asr_cfg["base_url"]
        if asr_cfg.get("model_name"):
            asr_obj.model = asr_cfg["model_name"]
        return True
    if asr_module_name == "Qwen3ASRFlash" and "qwen3_asr_flash" in cls_mod:
        if asr_cfg.get("model_name"):
            asr_obj.model_name = asr_cfg["model_name"]
        if "context" in asr_cfg:
            asr_obj.context = asr_cfg["context"] or ""
        return True
    if asr_module_name == "AliyunBLStreamASR" and "aliyunbl_stream" in cls_mod:
        if asr_cfg.get("model"):
            asr_obj.model = asr_cfg["model"]
        return True
    if asr_module_name == "DoubaoASR" and cls_mod.endswith(".doubao"):
        if asr_cfg.get("cluster"):
            asr_obj.cluster = asr_cfg["cluster"]
        return True
    if asr_module_name == "DoubaoStreamASR" and "doubao_stream" in cls_mod:
        if asr_cfg.get("resource_id"):
            asr_obj.resource_id = asr_cfg["resource_id"]
        return True
    return False


def _hot_reload_runtime_config(
    changed_fields: list[str],
    *,
    prev_asr_provider: str | None = None,
    new_asr_provider: str | None = None,
) -> tuple[bool, str]:
    """Apply saved prompt / LLM / ASR / TTS config changes live in-process without killing the server (P1-13)."""
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
        voice_changed = (
            "voice" in changed_fields
            or "tts_provider" in changed_fields
            or "tts_config" in changed_fields
        )
        asr_provider_changed = "asr_provider" in changed_fields
        asr_cfg_changed = "asr_config" in changed_fields
        asr_changed = asr_provider_changed or asr_cfg_changed

        # Switching to/from a heavy local ASR model (SherpaASR / FunASR) should restart cleanly
        # so the OS reclaims ~350MB-3.5GB of local model memory or initializes local ONNX weights cleanly.
        if asr_provider_changed:
            local_asrs = {"SherpaASR", "FunASR"}
            if (prev_asr_provider in local_asrs) or (new_asr_provider in local_asrs):
                print(
                    f"[WebConsole] 切换本地/云端 ASR 引擎 ({prev_asr_provider} -> {new_asr_provider})，平滑重启以释放或加载模型内存",
                    flush=True,
                )
                return False, "asr_local_cloud_boundary_changed"

        asr_mod_name = str((new_cfg.get("selected_module") or {}).get("ASR") or "")
        asr_cfg = ((new_cfg.get("ASR") or {}).get(asr_mod_name)) or {}

        new_llm = None
        new_asr = None
        active_conns = runtime_events.get_active_connections()
        device_busy = shared._is_device_busy() if active_conns else False
        if asr_provider_changed and active_conns and device_busy:
            print(
                f"[WebConsole] 检测到忙碌连接切换了 ASR 引擎类型 ({asr_mod_name})，需平滑重启生效",
                flush=True,
            )
            return False, "asr_engine_class_changed_on_active_conn"

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
            if asr_provider_changed:
                mods = initialize_modules(srv.logger, srv.config, init_asr=True)
                if "asr" in mods:
                    srv._asr = mods["asr"]
                    new_asr = mods["asr"]
            elif asr_cfg_changed and getattr(srv, "_asr", None) is not None:
                _apply_asr_attrs_in_place(srv._asr, asr_mod_name, asr_cfg)

        if llm_changed and new_llm is None and not servers:
            # Offline/test environment without WebSocketServer: still validate LLM config instantiability
            pass

        tts_mod_name = str((new_cfg.get("selected_module") or {}).get("TTS") or "")
        tts_cfg = ((new_cfg.get("TTS") or {}).get(tts_mod_name)) or {}
        tts_class_mismatch = False
        asr_class_mismatch = False

        def _retire_old_tts(old_tts_obj, conn_loop) -> None:
            if old_tts_obj is None:
                return
            retired_ev = threading.Event()
            retired_ev.set()
            old_tts_obj.conn = type(
                "_RetiredConnStub",
                (),
                {"stop_event": retired_ev, "client_abort": True, "sentence_id": "", "loop": conn_loop},
            )()
            close_fn = getattr(old_tts_obj, "close", None)
            if callable(close_fn) and conn_loop is not None and not conn_loop.is_closed():
                try:
                    res_c = close_fn()
                    if asyncio.iscoroutine(res_c):
                        asyncio.run_coroutine_threadsafe(res_c, conn_loop)
                except Exception as exc:
                    shared._log_rate_limited("hot_reload_retire_old_tts", exc)

        for conn in active_conns:
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
                    if asr_changed:
                        conn.config.setdefault("selected_module", {})["ASR"] = asr_mod_name
                        conn.config["ASR"] = new_cfg.get("ASR", {})
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
                        old_tts = conn.tts
                        old_cls_mod = str(getattr(type(old_tts), "__module__", "") or "")
                        conn_loop = getattr(conn, "loop", None)
                        crosses_local_gsv = (tts_mod_name == "GPT_SOVITS_V2") or ("gpt_sovits" in old_cls_mod)
                        if (
                            not device_busy
                            and not crosses_local_gsv
                            and conn_loop is not None
                            and not conn_loop.is_closed()
                            and conn_loop.is_running()
                        ):
                            from core.utils.modules_initialize import initialize_tts

                            fresh_tts = initialize_tts(new_cfg)
                            fut = asyncio.run_coroutine_threadsafe(fresh_tts.open_audio_channels(conn), conn_loop)
                            fut.result(timeout=5.0)
                            conn.tts = fresh_tts
                            _retire_old_tts(old_tts, conn_loop)
                        else:
                            tts_class_mismatch = True
                if asr_provider_changed:
                    if not device_busy:
                        from core.utils.modules_initialize import initialize_asr

                        old_asr = getattr(conn, "asr", None)
                        conn_loop = getattr(conn, "loop", None)
                        is_stream_asr = asr_mod_name in ("AliyunBLStreamASR", "DoubaoStreamASR")
                        fresh_asr = initialize_asr(new_cfg) if (is_stream_asr or new_asr is None) else new_asr
                        conn.asr = fresh_asr
                        close_asr = getattr(old_asr, "close", None)
                        if callable(close_asr) and conn_loop is not None and not conn_loop.is_closed():
                            try:
                                res_ca = close_asr()
                                if asyncio.iscoroutine(res_ca):
                                    asyncio.run_coroutine_threadsafe(res_ca, conn_loop)
                            except Exception as exc:
                                shared._log_rate_limited("hot_reload_close_old_asr", exc)
                    else:
                        asr_class_mismatch = True
                elif asr_cfg_changed and getattr(conn, "asr", None) is not None:
                    if not _apply_asr_attrs_in_place(conn.asr, asr_mod_name, asr_cfg):
                        asr_class_mismatch = True
            except Exception as conn_exc:
                print(shared._redact_secrets(f"[WebConsole] 单连接热更新跳过: {conn_exc}"), flush=True)

        if tts_class_mismatch:
            print(
                f"[WebConsole] 检测到活跃连接切换了 TTS 引擎类型 ({tts_mod_name})，需平滑重启生效",
                flush=True,
            )
            return False, "tts_engine_class_changed_on_active_conn"
        if asr_class_mismatch:
            print(
                f"[WebConsole] 检测到活跃连接 ASR 参数无法原位热更新 ({asr_mod_name})，需平滑重启生效",
                flush=True,
            )
            return False, "asr_engine_class_changed_on_active_conn"

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
_ALLOWED_RECALL_STYLE_KEYS = frozenset(
    {"sensitive_filter_enabled", "silent_memory_enabled", "anti_ai_flavor_enabled"}
)
_ALLOWED_PERSONA_ROLLBACK_KEYS = frozenset(
    {"target_revision", "expected_revision", "expected_digest", "note"}
)
_ALLOWED_ESP32_DEVICE_KEYS = frozenset({"action", "device_id"})
_ESP32_MAC_COLON_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
_ESP32_MAC_DASH_RE = re.compile(r"^[0-9a-f]{2}(?:-[0-9a-f]{2}){5}$")
_ESP32_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9:._-]{3,63}$")
_MAX_CONFIG_ALLOWED_DEVICES = 64


def _normalize_esp32_device_id(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("字段 device_id 必须为字符串")
    s = raw.strip().lower()
    if not s:
        raise ValueError("设备 MAC / ID 不能为空")
    if "${" in s or "}" in s or any(ch.isspace() for ch in s):
        raise ValueError("设备 MAC / ID 格式非法（不可包含空格或占位符）")
    if _ESP32_MAC_DASH_RE.match(s):
        s = s.replace("-", ":")
    if not (_ESP32_MAC_COLON_RE.match(s) or _ESP32_SAFE_ID_RE.match(s)):
        raise ValueError("设备 MAC / 客户端 ID 格式非法（例如 xx:xx:xx:xx:xx:xx 或 pc-talk-local，长度 4～64 位）")
    return s


def _get_esp32_auth_snapshot(cfg: dict | None = None) -> dict:
    cur_cfg = cfg if isinstance(cfg, dict) else shared._read_raw_yaml()
    srv_cfg = cur_cfg.get("server") if isinstance(cur_cfg.get("server"), dict) else {}
    auth_cfg = srv_cfg.get("auth") if isinstance(srv_cfg.get("auth"), dict) else {}
    auth_enabled = bool(auth_cfg.get("enabled", False))
    raw_yaml_devs = auth_cfg.get("allowed_devices")
    config_allowed = auth_patch.normalize_allowed_devices(raw_yaml_devs, env_val="")
    env_allowed = auth_patch.normalize_allowed_devices([], env_val=None)
    merged_allowed = auth_patch.normalize_allowed_devices(raw_yaml_devs, env_val=None)
    allowed_set = set(merged_allowed)
    pending = [
        item
        for item in runtime_events.get_rejected_devices()
        if str(item.get("device_id") or "").strip().lower() not in allowed_set
    ]
    return {
        "auth_enabled": auth_enabled,
        "allowed_devices": merged_allowed,
        "env_allowed_devices": env_allowed,
        "config_allowed_devices": config_allowed,
        "pending_devices": pending,
    }


def _sync_live_esp32_allowlist(merged_allowed: list[str]) -> None:
    runtime_events.set_live_allowed_devices(merged_allowed)
    try:
        from core.utils.cache.manager import CacheType, cache_manager

        cached_cfg = cache_manager.get(CacheType.CONFIG, "main_config")
        if isinstance(cached_cfg, dict):
            cached_cfg.setdefault("server", {}).setdefault("auth", {})["allowed_devices"] = list(merged_allowed)
            cache_manager.set(CacheType.CONFIG, "main_config", cached_cfg)
    except ImportError:
        return
    except Exception as exc:
        shared._log_rate_limited("esp32_allowlist_cache_sync", exc)


def _update_esp32_devices(payload: dict) -> tuple[bool, dict | str]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象"
    unknown = sorted(str(k) for k in payload.keys() if k not in _ALLOWED_ESP32_DEVICE_KEYS)
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}"

    raw_action = payload.get("action")
    if not isinstance(raw_action, str) or raw_action.strip().lower() not in ("add", "remove", "dismiss"):
        return False, "字段 action 必须为 add、remove 或 dismiss"
    action = raw_action.strip().lower()

    if action == "dismiss":
        raw_dev = payload.get("device_id")
        if not isinstance(raw_dev, str):
            return False, "字段 device_id 必须为字符串"
        dismiss_id = raw_dev.strip().lower()[:64]
        if not dismiss_id:
            return False, "设备 MAC / ID 不能为空"
        runtime_events.clear_rejected_device(dismiss_id)
        return True, {
            "action": action,
            "device_id": dismiss_id,
            "esp32_auth": _get_esp32_auth_snapshot(),
        }

    try:
        dev_norm = _normalize_esp32_device_id(payload.get("device_id"))
    except ValueError as ve:
        return False, str(ve)

    with _config_write_lock:
        cfg = shared._read_raw_yaml()
        srv = cfg.setdefault("server", {})
        if not isinstance(srv, dict):
            srv = {}
            cfg["server"] = srv
        auth_blk = srv.setdefault("auth", {})
        if not isinstance(auth_blk, dict):
            auth_blk = {}
            srv["auth"] = auth_blk

        raw_list = auth_blk.get("allowed_devices")
        if isinstance(raw_list, str):
            raw_entries = [raw_list]
        elif isinstance(raw_list, (list, tuple)):
            raw_entries = [str(x) for x in raw_list if x is not None and str(x).strip()]
        else:
            raw_entries = []

        env_placeholders = [e for e in raw_entries if "${" in e]
        if not env_placeholders:
            env_placeholders = ["${ESP32_ALLOWED_DEVICES}"]
        config_devs = auth_patch.normalize_allowed_devices(raw_entries, env_val="")
        env_devs = auth_patch.normalize_allowed_devices([], env_val=None)

        changed = False
        if action == "add":
            if dev_norm not in config_devs:
                if len(config_devs) >= _MAX_CONFIG_ALLOWED_DEVICES:
                    return False, f"控制台白名单数量已达上限（最多 {_MAX_CONFIG_ALLOWED_DEVICES} 台设备）"
                config_devs.append(dev_norm)
                changed = True
        else:  # remove
            if dev_norm not in config_devs:
                if dev_norm in env_devs:
                    return (
                        False,
                        f"设备 {dev_norm} 来自宿主机 .env 的 ESP32_ALLOWED_DEVICES，请在 .env 中修改后重启服务",
                    )
                return False, f"白名单中未找到设备 {dev_norm}"
            config_devs = [d for d in config_devs if d != dev_norm]
            changed = True

        next_yaml_list = list(env_placeholders) + list(config_devs)
        if changed:
            auth_blk["allowed_devices"] = next_yaml_list
            header = (
                "# =====================================================================\n"
                "# 小智 ESP32 本地服务端精简配置（由本地 Web 控制台管理）\n"
                "# 全部 API Key 仅通过环境变量内存展开，严禁落盘明文密钥\n"
                "# =====================================================================\n\n"
            )
            body = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
            shared._atomic_write_text(shared.CONFIG_PATH, header + body)
            shared._get_config_yaml_cache(shared.CONFIG_PATH).set(shared.CONFIG_PATH, copy.deepcopy(cfg))

        merged_allowed = auth_patch.normalize_allowed_devices(next_yaml_list, env_val=None)
        _sync_live_esp32_allowlist(merged_allowed)
        if action == "add":
            runtime_events.clear_rejected_device(dev_norm)

        return True, {
            "action": action,
            "device_id": dev_norm,
            "changed": changed,
            "esp32_auth": _get_esp32_auth_snapshot(cfg),
        }


def handle_get_esp32_devices(handler) -> None:
    handler._send_json({"ok": True, "esp32_auth": _get_esp32_auth_snapshot()})


def handle_post_esp32_devices(handler) -> None:
    try:
        body = handler._read_body_json()
        ok_ed, res_or_err = _update_esp32_devices(body)
        if ok_ed:
            handler._send_json({"ok": True, **res_or_err})
        else:
            handler._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus.BAD_REQUEST)
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


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
            anima.schedule_asr_corrections_sync()
            handler._send_json({"ok": True, "asr_corrections": res_or_err})
        else:
            handler._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus.BAD_REQUEST)
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def _get_recall_style_snapshot() -> dict:
    try:
        return dict(recall_guard.get_recall_style_config())
    except Exception as exc:
        shared._log_rate_limited("get_recall_style", exc)
        return {
            "sensitive_filter_enabled": True,
            "silent_memory_enabled": True,
            "anti_ai_flavor_enabled": True,
        }


def _update_recall_style_config(payload: dict) -> tuple[bool, dict | str]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象"
    unknown = sorted(str(k) for k in payload.keys() if k not in _ALLOWED_RECALL_STYLE_KEYS)
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}"
    if not payload:
        return False, "请至少提供 sensitive_filter_enabled、silent_memory_enabled 或 anti_ai_flavor_enabled"
    kwargs: dict[str, bool] = {}
    for key in _ALLOWED_RECALL_STYLE_KEYS:
        if key in payload:
            val = payload[key]
            if not isinstance(val, bool):
                return False, f"字段 {key} 必须为布尔值"
            kwargs[key] = val
    try:
        saved = recall_guard.save_recall_style_config(**kwargs)
        return True, saved
    except ValueError as exc:
        return False, str(exc)


def handle_get_recall_style(handler) -> None:
    handler._send_json(
        {
            "ok": True,
            "recall_style": _get_recall_style_snapshot(),
            "stats": recall_guard.get_recall_guard_stats(),
        }
    )


def handle_post_recall_style(handler) -> None:
    try:
        body = handler._read_body_json()
        ok_rs, res_or_err = _update_recall_style_config(body)
        if ok_rs:
            handler._send_json(
                {
                    "ok": True,
                    "recall_style": res_or_err,
                    "stats": recall_guard.get_recall_guard_stats(),
                }
            )
        else:
            handler._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus.BAD_REQUEST)
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_get_persona_history(handler, parsed=None) -> None:
    try:
        cfg = shared._read_raw_yaml()
        cur_prompt = str(cfg.get("prompt") or "").replace("\r\n", "\n").strip()
        char_name = anima._infer_character_name(cur_prompt)
        hist = persona_versions.list_persona_history(
            current_prompt=cur_prompt,
            char_name=char_name,
            include_full_prompt=False,
        )
        diff_obj = None
        if parsed is not None:
            from urllib.parse import parse_qs

            qs = parse_qs(parsed.query)
            raw_from = (qs.get("diff_from") or [None])[0]
            raw_to = (qs.get("diff_to") or [None])[0]
            if raw_from is not None:
                try:
                    from_rev = int(raw_from)
                    to_rev = int(raw_to) if raw_to is not None else None
                except (TypeError, ValueError):
                    handler._send_json({"ok": False, "error": "diff_from / diff_to 必须为整数版本号"}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    diff_obj = persona_versions.diff_persona_versions(
                        from_rev,
                        to_rev,
                        current_prompt=cur_prompt,
                        char_name=char_name,
                    )
                except KeyError as ke:
                    handler._send_json({"ok": False, "error": str(ke)}, HTTPStatus.NOT_FOUND)
                    return
        resp_body = {"ok": True, **hist}
        if diff_obj is not None:
            resp_body["diff"] = diff_obj
        handler._send_json(resp_body)
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_persona_rollback(handler) -> None:
    try:
        body = handler._read_body_json()
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_PERSONA_ROLLBACK_KEYS)
        if unknown:
            handler._send_json({"ok": False, "error": f"包含不支持的字段: {', '.join(unknown)}"}, HTTPStatus.BAD_REQUEST)
            return
        raw_target = body.get("target_revision")
        if isinstance(raw_target, bool) or not isinstance(raw_target, int) or raw_target <= 0:
            handler._send_json({"ok": False, "error": "字段 target_revision 必须为正整数"}, HTTPStatus.BAD_REQUEST)
            return
        exp_rev = body.get("expected_revision")
        if exp_rev is not None and (isinstance(exp_rev, bool) or not isinstance(exp_rev, int) or exp_rev < 0):
            handler._send_json({"ok": False, "error": "字段 expected_revision 必须为非负整数"}, HTTPStatus.BAD_REQUEST)
            return
        exp_dig = body.get("expected_digest")
        if exp_dig is not None and (not isinstance(exp_dig, str) or not exp_dig.strip()):
            handler._send_json({"ok": False, "error": "字段 expected_digest 必须为非空字符串"}, HTTPStatus.BAD_REQUEST)
            return
        note = str(body.get("note") or f"从 rev.{raw_target} 一键回滚").strip()[:160]

        with _config_write_lock:
            cfg = shared._read_raw_yaml()
            cur_prompt = str(cfg.get("prompt") or "").replace("\r\n", "\n").strip()
            char_name = anima._infer_character_name(cur_prompt)
            snap = persona_versions.get_snapshot(
                raw_target,
                current_prompt=cur_prompt,
                char_name=char_name,
            )
            if not snap:
                handler._send_json({"ok": False, "error": f"未找到人设快照版本 rev.{raw_target}"}, HTTPStatus.NOT_FOUND)
                return
            target_prompt = str(snap["prompt"]).strip()
            write_res = _write_updated_yaml(
                prompt=target_prompt,
                expected_prompt_revision=exp_rev,
                expected_prompt_digest=exp_dig,
                persona_source="rollback",
                persona_note=note,
                rolled_back_from=raw_target,
            )
            changed_fields = list(write_res.get("changed_fields") or [])
            hot_ok, hot_detail = _hot_reload_runtime_config(changed_fields)
            pv = write_res.get("persona") or {}
            if not pv:
                st = persona_versions.ensure_synced_with_prompt(
                    target_prompt, char_name=anima._infer_character_name(target_prompt)
                )
                pv = {"revision": st["current_revision"], "digest": st["current_digest"]}

        handler._send_json(
            {
                "ok": True,
                "rolled_back_from": raw_target,
                "revision": pv.get("revision"),
                "digest": pv.get("digest"),
                "prompt": target_prompt,
                "changed_fields": changed_fields,
                "hot_reloaded": bool(hot_ok),
                "detail": hot_detail,
            }
        )
    except shared._BadRequestError:
        raise
    except persona_versions.PersonaConflictError as ce:
        handler._send_json(
            {
                "ok": False,
                "error": str(ce),
                "conflict": True,
                "current_revision": ce.current_revision,
                "current_digest": ce.current_digest,
            },
            HTTPStatus.CONFLICT,
        )
    except persona_versions.PersonaBoundaryError as be:
        handler._send_json(
            {"ok": False, "error": str(be), "boundary_violations": be.violations},
            HTTPStatus.BAD_REQUEST,
        )
    except ValueError as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
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

        asr_provider: str | None = None
        if "asr_provider" in body and body.get("asr_provider") is not None:
            raw_ap = body.get("asr_provider")
            if not isinstance(raw_ap, str):
                handler._send_json({"ok": False, "error": "字段 asr_provider 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                return
            asr_provider = raw_ap.strip()

        asr_config: dict | None = None
        if "asr_config" in body and body.get("asr_config") is not None:
            raw_ac = body.get("asr_config")
            if not isinstance(raw_ac, dict):
                handler._send_json({"ok": False, "error": "字段 asr_config 必须为 JSON 对象"}, HTTPStatus.BAD_REQUEST)
                return
            asr_config = raw_ac

        tts_provider: str | None = None
        if "tts_provider" in body and body.get("tts_provider") is not None:
            raw_tp = body.get("tts_provider")
            if not isinstance(raw_tp, str):
                handler._send_json({"ok": False, "error": "字段 tts_provider 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                return
            tts_provider = raw_tp.strip()

        tts_config: dict | None = None
        if "tts_config" in body and body.get("tts_config") is not None:
            raw_tc = body.get("tts_config")
            if not isinstance(raw_tc, dict):
                handler._send_json({"ok": False, "error": "字段 tts_config 必须为 JSON 对象"}, HTTPStatus.BAD_REQUEST)
                return
            tts_config = raw_tc

        force_restart = bool(body.get("force_restart", False))
        allow_restart = bool(body.get("restart", True))

        exp_prompt_rev: int | None = None
        if "expected_prompt_revision" in body and body.get("expected_prompt_revision") is not None:
            raw_epr = body.get("expected_prompt_revision")
            if isinstance(raw_epr, bool) or not isinstance(raw_epr, int) or raw_epr < 0:
                handler._send_json(
                    {"ok": False, "error": "字段 expected_prompt_revision 必须为非负整数"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            exp_prompt_rev = raw_epr

        exp_prompt_dig: str | None = None
        if "expected_prompt_digest" in body and body.get("expected_prompt_digest") is not None:
            raw_epd = body.get("expected_prompt_digest")
            if not isinstance(raw_epd, str) or not raw_epd.strip():
                handler._send_json(
                    {"ok": False, "error": "字段 expected_prompt_digest 必须为非空字符串"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            exp_prompt_dig = raw_epd.strip()

        persona_note = str(body.get("prompt_note") or "").strip()[:160]

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
                asr_provider=asr_provider,
                asr_config=asr_config,
                tts_provider=tts_provider,
                tts_config=tts_config,
                expected_prompt_revision=exp_prompt_rev,
                expected_prompt_digest=exp_prompt_dig,
                persona_note=persona_note,
            )
            changed_fields = list(write_res.get("changed_fields") or [])
            voice_recovered = bool(write_res.get("voice_recovered", False))
            if "asr_provider" in changed_fields:
                hot_ok, hot_detail = _hot_reload_runtime_config(
                    changed_fields,
                    prev_asr_provider=write_res.get("prev_asr_provider"),
                    new_asr_provider=write_res.get("new_asr_provider"),
                )
            else:
                hot_ok, hot_detail = _hot_reload_runtime_config(changed_fields)
        if voice_recovered and hot_detail == "no_changes":
            hot_detail = "voice_weights_recovered"
        will_restart = bool(force_restart or (allow_restart and not hot_ok))

        dirty_after, _ = gsv._get_voice_dirty_state()
        pv_info = write_res.get("persona") or {}
        resp_payload = {
            "ok": True,
            "boot_id": shared.get_console_boot_id(),
            "restarting": will_restart,
            "hot_reloaded": bool(hot_ok and not will_restart),
            "changed_fields": changed_fields,
            "voice_recovered": voice_recovered,
            "voice_dirty": bool(dirty_after),
            "detail": hot_detail,
        }
        if pv_info:
            resp_payload["prompt_revision"] = pv_info.get("revision")
            resp_payload["prompt_digest"] = pv_info.get("digest")
        handler._send_json(resp_payload)

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
    except persona_versions.PersonaConflictError as ce:
        handler._send_json(
            {
                "ok": False,
                "error": str(ce),
                "conflict": True,
                "current_revision": ce.current_revision,
                "current_digest": ce.current_digest,
            },
            HTTPStatus.CONFLICT,
        )
    except persona_versions.PersonaBoundaryError as be:
        handler._send_json(
            {"ok": False, "error": str(be), "boundary_violations": be.violations},
            HTTPStatus.BAD_REQUEST,
        )
    except ValueError as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

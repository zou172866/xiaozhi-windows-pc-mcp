"""Background service prober plus /api/health, /api/state, /api/audio-output, and /api/clear-history routes (H-1, P2-22)."""

from __future__ import annotations

import asyncio
import atexit
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
import re
import secrets
import socket
import struct
import threading
import time
from urllib.parse import parse_qs, quote, urlparse
import wave

import httpx
import yaml

import asr_corrections
import mei_emotion_router
import patch_registry
import pc_audio_router
import reply_length_router
import runtime_events
import runtime_settings
import turn_context_router
import xz_common
from console import shared
from console.routes import anima, config, gsv, l3

_probe_lock = threading.Lock()
_pc_audio_online_cache: bool = False
_gsv_online_cache: bool = False
_anima_backup_lock = threading.Lock()
_anima_backup_cache: dict | None = None
_probe_fail_counts: dict[str, int] = {
    "pc_audio": 0,
    "l3": 0,
    "anima_rel": 0,
    "obsidian": 0,
    "gsv": 0,
}


def _get_cached_anima_backup_snapshot() -> dict:
    with _anima_backup_lock:
        if _anima_backup_cache is not None:
            snap = dict(_anima_backup_cache)
            snap["probed"] = True
            return snap
    return {
        "probed": False,
        "enabled": True,
        "last_backup_at": "",
        "last_backup_ts": 0.0,
        "last_backup_file": "",
        "integrity_ok": None,
        "daily_count": 0,
        "weekly_count": 0,
        "last_error": "",
    }
_PROBE_OFFLINE_THRESHOLD = 2
_PROBE_INTERVAL_SECONDS = 3.0


def _get_cached_pc_audio_online() -> bool:
    with _probe_lock:
        return bool(_pc_audio_online_cache)


def _set_cached_pc_audio_online(val: bool) -> None:
    with _probe_lock:
        global _pc_audio_online_cache
        _pc_audio_online_cache = bool(val)
        _probe_fail_counts["pc_audio"] = 0 if val else _PROBE_OFFLINE_THRESHOLD


def _get_cached_gsv_online() -> bool:
    with _probe_lock:
        return bool(_gsv_online_cache)


def _get_cached_l3_backend_snapshot() -> dict:
    with l3._l3_cache_lock:
        if l3._l3_cache is not None:
            return dict(l3._l3_cache)
    return l3._read_local_l3_fallback()


def _get_cached_anima_relationship_snapshot(cfg: dict | None = None) -> dict:
    with anima._anima_rel_lock:
        if anima._anima_rel_cache is not None:
            return dict(anima._anima_rel_cache)
    return anima._default_offline_relationship(anima._get_anima_character_id(cfg))


def _probe_gsv_tcp_once() -> bool:
    """Lightweight TCP connect check for 19880 without touching GPU weights or locks."""
    try:
        u = urlparse(shared.GSV_Host)
        host = u.hostname or "host.docker.internal"
        port = int(u.port or 19880)
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except Exception as exc:
        shared._log_rate_limited("probe_gsv_tcp", exc)
        return False


def _probe_background_services_once() -> dict[str, bool]:
    """Probe 18900 / 19880 / 18787 off the /api/state request path with anti-flap hysteresis (P2-22)."""
    global _pc_audio_online_cache, _gsv_online_cache

    # 1. 18900 PC Audio Bridge
    audio_ok = False
    try:
        audio_ok = bool(pc_audio_router.is_pc_audio_bridge_online(force=True))
    except Exception as exc:
        shared._log_rate_limited("probe_pc_audio", exc)
        audio_ok = False
    with _probe_lock:
        if audio_ok:
            _probe_fail_counts["pc_audio"] = 0
            _pc_audio_online_cache = True
        else:
            _probe_fail_counts["pc_audio"] += 1
            if _probe_fail_counts["pc_audio"] >= _PROBE_OFFLINE_THRESHOLD:
                _pc_audio_online_cache = False

    # 2. 18900 L3 Backend Status
    l3_ok = False
    try:
        client = shared._get_console_http_client()
        resp = client.get(
            f"{shared.PC_MCP_HOST}/l3/status",
            headers=pc_audio_router.get_pc_mcp_auth_headers(),
            timeout=1.6,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["mcp_online"] = True
            host_mode = str(data.get("pc_mode") or "").strip().lower()
            if host_mode in ("off", "safe", "full"):
                l3._set_cached_pc_mode(host_mode, online=True)
            with l3._l3_cache_lock:
                l3._l3_cache = dict(data)
                l3._l3_cache_ts = time.monotonic()
            l3_ok = True
    except Exception as exc:
        shared._log_rate_limited("probe_l3_status", exc)
        l3_ok = False
    with _probe_lock:
        if l3_ok:
            _probe_fail_counts["l3"] = 0
        else:
            _probe_fail_counts["l3"] += 1
            should_flip_l3 = _probe_fail_counts["l3"] >= _PROBE_OFFLINE_THRESHOLD
    if not l3_ok:
        with l3._l3_cache_lock:
            was_never_set = l3._l3_cache is None
        if was_never_set or should_flip_l3:
            l3._set_cached_pc_mode("safe", online=False)
            fb = l3._read_local_l3_fallback()
            with l3._l3_cache_lock:
                l3._l3_cache = dict(fb)
                l3._l3_cache_ts = time.monotonic()

    # 3. 18787 Anima Relationship
    rel_ok = False
    cfg = None
    try:
        cfg = shared._read_raw_yaml()
    except Exception as exc:
        shared._log_rate_limited("probe_read_yaml", exc)
        cfg = None
    char_id = anima._get_anima_character_id(cfg)
    anima_tok = anima._read_anima_token()
    if anima_tok:
        try:
            client = shared._get_console_http_client()
            resp = client.get(
                f"{anima.ANIMA_HOST}/xiaozhi/relationship",
                params={"character_id": char_id},
                headers={"Authorization": f"Bearer {anima_tok}"},
                timeout=1.8,
            )
            if resp.status_code == 200:
                data = resp.json()
                data["online"] = True
                with anima._anima_rel_lock:
                    anima._anima_rel_cache = dict(data)
                    anima._anima_rel_ts = time.monotonic()
                rel_ok = True
        except Exception as exc:
            shared._log_rate_limited("probe_anima_rel", exc)
            rel_ok = False
    with _probe_lock:
        if rel_ok:
            _probe_fail_counts["anima_rel"] = 0
        else:
            _probe_fail_counts["anima_rel"] += 1
            should_flip_rel = _probe_fail_counts["anima_rel"] >= _PROBE_OFFLINE_THRESHOLD
    if not rel_ok:
        with anima._anima_rel_lock:
            if anima._anima_rel_cache is None:
                anima._anima_rel_cache = anima._default_offline_relationship(char_id)
                anima._anima_rel_ts = time.monotonic()
            elif should_flip_rel:
                stale = dict(anima._anima_rel_cache)
                stale["online"] = False
                anima._anima_rel_cache = stale
                anima._anima_rel_ts = time.monotonic()

    # 4. 18787 Obsidian Sync Status
    obs_ok = False
    if anima_tok:
        try:
            client = shared._get_console_http_client()
            resp = client.get(
                f"{anima.ANIMA_HOST}/xiaozhi/obsidian/status",
                headers={"Authorization": f"Bearer {anima_tok}"},
                timeout=1.8,
            )
            if resp.status_code == 200:
                data = resp.json()
                data["online"] = True
                with anima._obsidian_lock:
                    anima._obsidian_cache = dict(data)
                    anima._obsidian_ts = time.monotonic()
                obs_ok = True
        except Exception as exc:
            shared._log_rate_limited("probe_obsidian", exc)
            obs_ok = False
    with _probe_lock:
        if obs_ok:
            _probe_fail_counts["obsidian"] = 0
        else:
            _probe_fail_counts["obsidian"] += 1
            should_flip_obs = _probe_fail_counts["obsidian"] >= _PROBE_OFFLINE_THRESHOLD
    if not obs_ok:
        with anima._obsidian_lock:
            was_never_set_obs = anima._obsidian_cache is None
        if was_never_set_obs or should_flip_obs:
            err_msg = "" if anima_tok else "未找到 Anima Brain 访问令牌 (studio_token.txt)"
            anima._mark_obsidian_cache_offline(err_msg)

    # 5. 19880 GPT-SoVITS TCP Liveness
    gsv_ok = _probe_gsv_tcp_once()
    with _probe_lock:
        if gsv_ok:
            _probe_fail_counts["gsv"] = 0
            _gsv_online_cache = True
        else:
            _probe_fail_counts["gsv"] += 1
            if _probe_fail_counts["gsv"] >= _PROBE_OFFLINE_THRESHOLD:
                _gsv_online_cache = False

    # 6. 18787 Anima Health & Online Backup Status (B-2 / B-1 inbox counters)
    try:
        client = shared._get_console_http_client()
        resp = client.get(f"{anima.ANIMA_HOST}/xiaozhi/health", timeout=1.5)
        if resp.status_code in (200, 503):
            h_data = resp.json()
            if isinstance(h_data, dict):
                if isinstance(h_data.get("backup"), dict):
                    snap_bk = dict(h_data["backup"])
                    if "inbox_pending" in h_data:
                        snap_bk["inbox_pending"] = int(h_data.get("inbox_pending") or 0)
                    if "inbox_dead" in h_data:
                        snap_bk["inbox_dead"] = int(h_data.get("inbox_dead") or 0)
                    with _anima_backup_lock:
                        global _anima_backup_cache
                        _anima_backup_cache = snap_bk
    except Exception as exc:
        shared._log_rate_limited("probe_anima_health", exc)

    # 7. Auto-retry dirty GSV voice weight rollback when GSV is online and ESP32 is idle (D-3)
    dirty_target, _dirty_err = gsv._get_voice_dirty_state()
    if dirty_target:
        # Always reconcile against current saved active GSV voice so stale dirty state never reverts a newly saved voice
        current_cfg_gsv = gsv._get_configured_gsv_voice()
        if current_cfg_gsv is None:
            gsv._set_voice_dirty(None, "")
            dirty_target = None
        elif current_cfg_gsv != dirty_target:
            dirty_target = current_cfg_gsv
    if gsv_ok and dirty_target and not shared._is_device_busy():
        acquired = gsv._gsv_weights_lock.acquire(timeout=0.1)
        if acquired:
            try:
                # Re-check dirty state, configured voice, and device busy status AFTER acquiring
                # _gsv_weights_lock so concurrent POST /api/config voice switches or recoveries are never reverted.
                cur_dirty, _ = gsv._get_voice_dirty_state()
                if cur_dirty and not shared._is_device_busy():
                    cur_cfg_gsv = gsv._get_configured_gsv_voice()
                    if cur_cfg_gsv is None:
                        gsv._set_voice_dirty(None, "")
                    else:
                        target_voice = cur_cfg_gsv
                        gsv._active_gsv_loaded = "__dirty__"
                        gsv._gsv_op_deadline_tls.deadline = time.monotonic() + 20.0
                        gsv._ensure_gsv_weights(target_voice, persist_boot_yaml=False)
                        if gsv._active_gsv_loaded == "__dirty__":
                            gsv._active_gsv_loaded = target_voice
                        gsv._set_voice_dirty(None, "")
                        msg = f"[WebConsole] 后台探测已自动恢复线上 GPT-SoVITS 音色权重: {target_voice}"
                        print(msg, flush=True)
                        with shared._state_lock:
                            shared._append_raw_log_locked(msg)
            except Exception as rb_exc:
                gsv._active_gsv_loaded = "__dirty__"
                cur_cfg_on_err = gsv._get_configured_gsv_voice()
                if cur_cfg_on_err is not None:
                    gsv._set_voice_dirty(cur_cfg_on_err, str(rb_exc))
                else:
                    gsv._set_voice_dirty(None, "")
                print(
                    f"[WebConsole] WARNING: 后台重试恢复线上音色权重 ({cur_cfg_on_err or dirty_target}) 仍失败: {rb_exc}",
                    flush=True,
                )
            finally:
                gsv._gsv_op_deadline_tls.deadline = None
                gsv._gsv_weights_lock.release()

    return {
        "pc_audio": _get_cached_pc_audio_online(),
        "l3": l3_ok,
        "anima_rel": rel_ok,
        "obsidian": obs_ok,
        "gsv": _get_cached_gsv_online(),
    }


def _service_probe_loop() -> None:
    while True:
        try:
            _probe_background_services_once()
        except Exception as exc:
            shared._log_rate_limited("service_probe_loop", exc)
        time.sleep(_PROBE_INTERVAL_SECONDS)



def handle_get_health(handler) -> None:
    shared._sync_runtime_events()
    with shared._state_lock:
        ready = bool(shared._device_info.get("server_ready"))
    health = patch_registry.get_health_summary()
    degraded_list = list(health.get("degraded") or [])
    security_ok = bool(health.get("security_ok", True))
    http_status = HTTPStatus.OK if security_ok else HTTPStatus.SERVICE_UNAVAILABLE
    handler._send_json(
        {
            "ok": security_ok and not degraded_list,
            "status": health.get("status", "ok"),
            "build_version": (os.environ.get("XIAOZHI_BUILD_VERSION") or "dev").strip() or "dev",
            "degraded": degraded_list,
            "security_ok": security_ok,
            "security_failures": list(health.get("security_failures") or []),
            "insecure_boot": bool(health.get("insecure_boot", False)),
            "insecure_boot_env": bool(health.get("insecure_boot_env", False)),
            "server_ready": ready,
            "server_version": health.get("server_version", ""),
            "expected_server_version": health.get("expected_server_version", ""),
            "version_match": bool(health.get("version_match", True)),
            "patch_counts": health.get("counts", {}),
            "patches": health.get("patches", {}),
            "contract_self_check": health.get("contract_self_check", {}),
            "degraded_reasons": health.get("degraded_reasons", []),
        },
        status=http_status,
    )


def handle_get_state(handler, parsed) -> None:
    cfg = shared._read_raw_yaml()
    selected = cfg.get("selected_module", {})
    llm_key = selected.get("LLM", "DeepSeekLLM")
    llm_cfg = cfg.get("LLM", {}).get(llm_key, {})
    srv_cfg = cfg.get("server", {})
    raw_ws = str(srv_cfg.get("websocket") or f"ws://{shared.HOST_LAN_IP}:18000/xiaozhi/v1/")
    ws_url = raw_ws.replace("${HOST_LAN_IP}", shared.HOST_LAN_IP)
    ota_url = ws_url.replace("ws://", "http://").replace(":18000/xiaozhi/v1/", ":18003/xiaozhi/ota/")

    ds_env = os.environ.get("DEEPSEEK_API_KEY", "")
    gem_env = os.environ.get("GEMINI_API_KEY", "")
    ds_ok = bool(ds_env and len(ds_env) > 10 and "请在这里" not in ds_env)
    gem_ok = bool(gem_env and len(gem_env) > 10 and "请在这里" not in gem_env)

    active_voice = gsv._get_current_voice_id(cfg)

    qs = parse_qs(parsed.query)
    req_cursor: int | None = None
    raw_cursor_vals = qs.get("log_cursor")
    if raw_cursor_vals:
        try:
            req_cursor = int(raw_cursor_vals[0])
        except (TypeError, ValueError):
            req_cursor = None

    shared._sync_runtime_events()
    with shared._state_lock:
        dev = dict(shared._device_info)
        msgs = list(shared._chat_messages[-60:])
    log_lines, latest_log_cursor, logs_inc, logs_reset = shared._get_logs_for_state(req_cursor)
    health_summary = patch_registry.get_health_summary()

    audio_cfg = pc_audio_router.read_audio_output_config()
    reply_len_cfg = reply_length_router.read_reply_length_config()
    idle_farewell_cfg = runtime_settings.get_idle_farewell()
    rel_cfg = _get_cached_anima_relationship_snapshot(cfg=cfg)
    l3_cfg = _get_cached_l3_backend_snapshot()
    obs_cfg = anima._get_cached_obsidian_snapshot()
    backup_cfg = _get_cached_anima_backup_snapshot()
    outbox_cfg = anima._get_anima_outbox_snapshot()
    vad_spec_cfg = config._get_vad_speculative_snapshot()
    vad_spec_stats = config._get_vad_speculative_stats_snapshot()
    asr_corr_cfg = config._get_asr_corrections_snapshot()
    dirty_voice, dirty_voice_err = gsv._get_voice_dirty_state()

    handler._send_json(
        {
            "config": {
                "prompt": (cfg.get("prompt") or "").strip(),
                "llm_provider": llm_key,
                "model_name": llm_cfg.get(
                    "model_name",
                    "deepseek-flash" if llm_key == "DeepSeekLLM" else "gemini-3.5-flash-lite",
                ),
                "voice": active_voice,
                "voice_dirty": bool(dirty_voice),
                "voice_dirty_target": dirty_voice or "",
                "voice_dirty_error": dirty_voice_err,
                "websocket": ws_url,
                "ota_url": ota_url,
                "deepseek_key_ok": ds_ok,
                "gemini_key_ok": gem_ok,
                "llm_cache": turn_context_router.get_llm_cache_stats(),
                "pc_mode": l3._read_pc_mode(),
                "pc_mode_online": l3._get_cached_pc_mode_online(),
                "audio_output": audio_cfg["target"],
                "pc_audio_volume": audio_cfg["volume"],
                "pc_audio_online": _get_cached_pc_audio_online(),
                "gsv_online": _get_cached_gsv_online(),
                "reply_length": reply_len_cfg,
                "idle_farewell": idle_farewell_cfg,
                "relationship": rel_cfg,
                "anima_backup": backup_cfg,
                "anima_outbox": outbox_cfg,
                "vad_speculative": vad_spec_cfg,
                "vad_speculative_stats": vad_spec_stats,
                "asr_corrections": asr_corr_cfg,
                "obsidian": obs_cfg,
                "l3_backend": l3_cfg,
                "agy_bridge_online": l3._check_agy_bridge_online(l3_cfg),
                "mei_emotions": [
                    {"id": k, "label": v.get("label_zh", k)}
                    for k, v in mei_emotion_router.load_mei_emotions().items()
                ],
            },
            "device": dev,
            "health": {
                "status": health_summary.get("status", "ok"),
                "degraded": bool(health_summary.get("degraded")),
                "degraded_list": list(health_summary.get("degraded") or []),
                "security_ok": bool(health_summary.get("security_ok", True)),
                "security_failures": list(health_summary.get("security_failures") or []),
                "insecure_boot": bool(health_summary.get("insecure_boot", False)),
                "insecure_boot_env": bool(health_summary.get("insecure_boot_env", False)),
                "server_version": health_summary.get("server_version", ""),
                "degraded_reasons": health_summary.get("degraded_reasons", []),
            },
            "messages": msgs,
            "logs": log_lines,
            "log_cursor": latest_log_cursor,
            "logs_incremental": logs_inc,
            "logs_reset": logs_reset,
        }
    )


def handle_post_audio_output(handler) -> None:
    try:
        body = handler._read_body_json()
        target = str(body.get("target") or "").strip().lower()
        volume = body.get("volume", None)
        if not target:
            target = pc_audio_router.read_audio_output_config()["target"]
        if target not in ("esp32", "pc"):
            handler._send_json({"ok": False, "error": "无效的音频输出目标"}, HTTPStatus.BAD_REQUEST)
            return
        saved = pc_audio_router.write_audio_output_config(target, volume)
        pc_online = pc_audio_router.is_pc_audio_bridge_online(force=True)
        _set_cached_pc_audio_online(pc_online)
        handler._send_json(
            {
                "ok": True,
                "target": saved["target"],
                "volume": saved["volume"],
                "pc_audio_online": pc_online,
            }
        )
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_clear_history(handler) -> None:
    handler._read_body_json()
    with shared._state_lock:
        shared._chat_messages.clear()
        shared._save_history()
    handler._send_json({"ok": True})

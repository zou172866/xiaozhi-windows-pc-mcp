"""Host 18900 PC control mode proxy and pluggable L3 backend routes (H-1, A-2)."""

from __future__ import annotations

from http import HTTPStatus
import json
import os
import threading
import time

import pc_audio_router
import xz_common
from console import shared

_pc_mode_lock = threading.Lock()
_pc_mode_cache: str = "safe"
_pc_mode_online_cache: bool = False


def _set_cached_pc_mode(mode: str, online: bool = True) -> None:
    global _pc_mode_cache, _pc_mode_online_cache
    m = str(mode or "safe").strip().lower()
    if m not in ("off", "safe", "full"):
        m = "safe"
    with _pc_mode_lock:
        _pc_mode_cache = m
        _pc_mode_online_cache = bool(online)


def _get_cached_pc_mode_online() -> bool:
    with _pc_mode_lock:
        return bool(_pc_mode_online_cache)


def _read_pc_mode() -> str:
    """Read PC control mode: cached host-18900 authority by default (A-2), or explicit test MODE_PATH."""
    if shared.MODE_PATH == shared._DEFAULT_MODE_PATH:
        with _pc_mode_lock:
            return _pc_mode_cache
    try:
        if os.path.exists(shared.MODE_PATH):
            with open(shared.MODE_PATH, "r", encoding="utf-8") as f:
                m = json.load(f).get("mode", "safe")
                if m in ("off", "safe", "full"):
                    return m
    except Exception as exc:
        shared._log_rate_limited("read_pc_mode", exc)
    return "safe"


class _EmergencyStopLockedError(RuntimeError):
    """Raised when host 18900 returns HTTP 423 Locked due to active physical emergency stop (P0-4)."""


def _write_pc_mode(mode: str) -> dict:
    """Switch PC control mode via authenticated host 18900 POST /mode (A-2, P0-4; fail-closed if 18900 unreachable)."""
    m = str(mode or "safe").strip().lower()
    if m not in ("off", "safe", "full"):
        raise ValueError("无效的权限挡位")
    if shared.MODE_PATH != shared._DEFAULT_MODE_PATH:
        xz_common.atomic_write_json(shared.MODE_PATH, {"mode": m})
        return {"ok": True, "mode": m, "source": "local_test"}

    client = shared._get_console_http_client()
    resp = client.post(
        f"{shared.PC_MCP_HOST}/mode",
        headers=pc_audio_router.get_pc_mcp_auth_headers(),
        json={"mode": m},
        timeout=3.0,
    )
    if resp.status_code == int(HTTPStatus.LOCKED):
        err_msg = "宿主机当前处于物理急停锁定状态（Ctrl+Alt+Pause），禁止通过控制台自动切至全权操控挡，请在宿主机侧显式复位急停后再切换。"
        try:
            body_423 = resp.json() if resp.content else {}
            if isinstance(body_423, dict) and body_423.get("error"):
                err_msg = shared._redact_secrets(str(body_423["error"]))
        except Exception as exc:
            shared._log_rate_limited("write_pc_mode_423_json", exc)
        raise _EmergencyStopLockedError(err_msg)
    if resp.status_code != 200:
        raise RuntimeError(
            f"宿主机 18900 MCP 拒绝切换挡位 (HTTP {resp.status_code}): {shared._redact_secrets(resp.text[:160])}"
        )
    data = resp.json() if resp.content else {}
    confirmed = str(data.get("mode") or m).strip().lower()
    if confirmed not in ("off", "safe", "full"):
        confirmed = m
    _set_cached_pc_mode(confirmed, online=True)
    return {
        "ok": True,
        "mode": confirmed,
        "source": "host_18900",
        "emergency_stopped": bool(data.get("emergency_stopped", False)),
    }


def _check_agy_bridge_online(l3_status: dict | None = None) -> bool:
    """Derive Antigravity sidecar heartbeat solely from host-private 18900 /l3/status (never ./data)."""
    src = l3_status
    if src is None:
        with _l3_cache_lock:
            src = dict(_l3_cache) if _l3_cache is not None else None
    if not isinstance(src, dict):
        return False
    detected = src.get("detected")
    if isinstance(detected, dict):
        agy = detected.get("antigravity")
        if isinstance(agy, dict):
            return bool(agy.get("ready"))
    return False


_l3_cache_lock = threading.Lock()
_l3_cache: dict | None = None
_l3_cache_ts = 0.0
_L3_CACHE_TTL = 3.0


def _read_local_l3_fallback() -> dict:
    """Read-only offline fallback when 18900 is unreachable (never reads or writes ./data L3 files)."""
    b = (os.environ.get("L3_BACKEND") or "dsh").strip().lower()
    if b not in shared.VALID_L3_BACKENDS:
        b = "dsh"
    labels = {
        "antigravity": "Google Antigravity（反重力）",
        "dsh": "DeepSeek DSH（DeepSeek Harness）",
        "workbuddy": "腾讯 WorkBuddy / CodeBuddy",
        "none": "关闭 L3 后台派单",
    }
    return {
        "ok": False,
        "mcp_online": False,
        "backend": b,
        "label_zh": labels.get(b, b),
        "ready": b == "none",
        "detail": (
            "宿主机 18900 MCP 离线（L3 配置仅存宿主机私有目录 pc-mcp/state/）"
            if b != "none"
            else "已关闭 L3 后台 Agent（仅保留 L1/L2）"
        ),
        "workdir": "",
        "effective_workdir": "",
        "dsh_cmd": "",
        "workbuddy_cmd": "",
        "workbuddy_auto_approve": False,
        "timeout_seconds": 300,
        "writable_fields": ["backend", "timeout_seconds"],
        "host_env_only_fields": ["workdir", "dsh_cmd", "workbuddy_cmd", "workbuddy_auto_approve"],
        "detected": {
            "antigravity": {"ready": False, "detail": "待宿主机 18900 MCP 在线探测"},
            "dsh": {"ready": False, "detail": "待宿主机 18900 MCP 在线探测"},
            "workbuddy": {"ready": False, "detail": "待宿主机 18900 MCP 在线探测"},
            "none": {"ready": True, "detail": "已关闭 L3 后台派单"},
        },
    }


def _fetch_l3_backend_status(force: bool = False) -> dict:
    global _l3_cache, _l3_cache_ts
    now = time.monotonic()
    with _l3_cache_lock:
        if not force and _l3_cache is not None and (now - _l3_cache_ts) < _L3_CACHE_TTL:
            return dict(_l3_cache)
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
            with _l3_cache_lock:
                _l3_cache = dict(data)
                _l3_cache_ts = time.monotonic()
            return data
    except Exception as exc:
        shared._log_rate_limited("fetch_l3_backend_status", exc)
    fb = _read_local_l3_fallback()
    with _l3_cache_lock:
        _l3_cache = dict(fb)
        _l3_cache_ts = time.monotonic()
    return fb


def _sanitize_l3_update_payload(payload: dict) -> tuple[bool, dict | str]:
    """Allow ONLY backend and timeout_seconds (or alias timeout); strictly reject host-env-only fields."""
    if not isinstance(payload, dict):
        return False, "请求体必须是 JSON 对象"
    forbidden = sorted(str(k) for k in payload.keys() if k not in shared.ALLOWED_L3_UPDATE_KEYS)
    if forbidden:
        return (
            False,
            f"仅允许修改 backend / timeout_seconds，宿主机命令与目录仅可从宿主机 .env 配置，已拒绝字段: {', '.join(forbidden)}",
        )

    out: dict = {}
    if "backend" in payload:
        b = str(payload.get("backend") or "").strip().lower()
        if b not in shared.VALID_L3_BACKENDS:
            return False, "无效的 L3 后端名称（可选: antigravity / dsh / workbuddy / none）"
        out["backend"] = b

    has_ts = "timeout_seconds" in payload
    has_t = "timeout" in payload
    if has_ts or has_t:
        parsed_vals = []
        for k in ("timeout_seconds", "timeout"):
            if k in payload:
                raw_v = payload[k]
                if isinstance(raw_v, bool):
                    return False, f"字段 {k} 必须为整数（30～1800）"
                try:
                    iv = int(raw_v)
                except (TypeError, ValueError):
                    return False, f"字段 {k} 必须为整数（30～1800）"
                parsed_vals.append(max(30, min(1800, iv)))
        if len(parsed_vals) == 2 and parsed_vals[0] != parsed_vals[1]:
            return False, "timeout_seconds 与 timeout 参数冲突"
        out["timeout_seconds"] = parsed_vals[0]

    if not out:
        return False, "请至少提供 backend 或 timeout_seconds"
    return True, out


def _update_l3_backend_config(payload: dict) -> tuple[bool, dict | str]:
    global _l3_cache, _l3_cache_ts
    ok_clean, clean_or_err = _sanitize_l3_update_payload(payload)
    if not ok_clean:
        return False, str(clean_or_err)
    forward_payload = clean_or_err
    try:
        client = shared._get_console_http_client()
        resp = client.post(
            f"{shared.PC_MCP_HOST}/l3/config",
            headers=pc_audio_router.get_pc_mcp_auth_headers(),
            json=forward_payload,
            timeout=3.5,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["mcp_online"] = True
            with _l3_cache_lock:
                _l3_cache = dict(data)
                _l3_cache_ts = time.monotonic()
            return True, data
        return False, f"宿主机 MCP 返回 HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as exc:
        return False, f"宿主机 18900 MCP 离线，无法更新 L3 配置（L3 配置仅存宿主机私有目录 pc-mcp/state/）: {exc}"



def handle_get_l3_backend(handler) -> None:
    l3_cfg = _fetch_l3_backend_status(force=True)
    handler._send_json({"ok": True, "l3_backend": l3_cfg})


def handle_post_pc_mode(handler) -> None:
    try:
        body = handler._read_body_json()
        mode = str(body.get("mode") or "safe").strip().lower()
        if mode not in ("off", "safe", "full"):
            handler._send_json({"ok": False, "error": "无效的权限挡位"}, HTTPStatus.BAD_REQUEST)
            return
        res = _write_pc_mode(mode)
        handler._send_json(res)
    except shared._BadRequestError:
        raise
    except _EmergencyStopLockedError as e:
        handler._send_json(
            {"ok": False, "emergency_stopped": True, "error": str(e)},
            HTTPStatus.LOCKED,
        )
    except ValueError as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
    except Exception as e:
        handler._send_json(
            {"ok": False, "error": f"宿主机 18900 MCP 不可用，未能切换挡位: {shared._redact_secrets(str(e))}"},
            HTTPStatus.BAD_GATEWAY,
        )


def handle_post_l3_backend(handler) -> None:
    try:
        body = handler._read_body_json()
        ok, result = _update_l3_backend_config(body)
        if ok:
            handler._send_json({"ok": True, "l3_backend": result})
        else:
            handler._send_json({"ok": False, "error": str(result)}, HTTPStatus.BAD_REQUEST)
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

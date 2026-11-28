"""18080 Web Console routes for Phase 2 multi-model LLM Fallback Pool."""

from __future__ import annotations

from http import HTTPStatus
import re
from typing import Any, Dict, List

import llm_fallback_router
import runtime_settings
from console import shared

_ALLOWED_TOP_KEYS = frozenset({"enabled", "cooldown_seconds", "nodes", "secrets", "reset_health"})
_ALLOWED_NODE_KEYS = frozenset({"id", "label", "base_url", "model", "api_key_env", "enabled"})
_SAFE_NODE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_SAFE_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def _validate_fallback_payload(body: dict) -> tuple[Dict[str, Any], Dict[str, Any], bool]:
    if not isinstance(body, dict) or not body:
        raise ValueError("请提供要更新的模型灾备配置字段")
    unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_TOP_KEYS)
    if unknown:
        raise ValueError(f"包含未允许的字段: {', '.join(unknown[:6])}")

    cfg_updates: Dict[str, Any] = {}
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise ValueError("字段 enabled 必须为布尔值")
        cfg_updates["enabled"] = body["enabled"]

    if "cooldown_seconds" in body:
        cd = body["cooldown_seconds"]
        if isinstance(cd, bool) or not isinstance(cd, int) or cd < 10 or cd > 600:
            raise ValueError("字段 cooldown_seconds 必须为 10～600 之间的整数")
        cfg_updates["cooldown_seconds"] = cd

    if "nodes" in body:
        raw_nodes = body["nodes"]
        if not isinstance(raw_nodes, list):
            raise ValueError("字段 nodes 必须为数组")
        if len(raw_nodes) > llm_fallback_router.MAX_FALLBACK_NODES:
            raise ValueError(f"备用节点最多配置 {llm_fallback_router.MAX_FALLBACK_NODES} 个")
        cleaned_nodes: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for idx, item in enumerate(raw_nodes):
            if not isinstance(item, dict):
                raise ValueError(f"nodes[{idx}] 必须为对象")
            unk_n = sorted(str(k) for k in item.keys() if k not in _ALLOWED_NODE_KEYS)
            if unk_n:
                raise ValueError(f"nodes[{idx}] 包含未允许字段: {', '.join(unk_n[:4])}")
            nid = str(item.get("id") or f"fb-{idx + 1}").strip()
            if not _SAFE_NODE_ID_RE.match(nid) or nid == "primary":
                raise ValueError(f"nodes[{idx}].id 非法（仅允许字母数字_- 且不可为 primary）")
            if nid in seen_ids:
                raise ValueError(f"节点 ID 重复: {nid}")
            seen_ids.add(nid)
            safe_url = llm_fallback_router.validate_fallback_base_url(str(item.get("base_url") or ""))
            model_name = str(item.get("model") or "").strip()[:120]
            if not model_name:
                raise ValueError(f"nodes[{idx}].model 不能为空")
            env_name = str(item.get("api_key_env") or "").strip()
            if env_name and not _SAFE_ENV_VAR_RE.match(env_name):
                raise ValueError(f"nodes[{idx}].api_key_env 格式非法（示例：DEEPSEEK_API_KEY）")
            label = str(item.get("label") or nid).strip()[:48] or nid
            en = item.get("enabled", True)
            if not isinstance(en, bool):
                raise ValueError(f"nodes[{idx}].enabled 必须为布尔值")
            cleaned_nodes.append(
                {
                    "id": nid,
                    "label": label,
                    "base_url": safe_url,
                    "model": model_name,
                    "api_key_env": env_name,
                    "enabled": en,
                }
            )
        cfg_updates["nodes"] = cleaned_nodes

    sec_updates: Dict[str, Any] = {}
    if "secrets" in body:
        raw_s = body["secrets"]
        if not isinstance(raw_s, dict):
            raise ValueError("字段 secrets 必须为 JSON 对象")
        for sk, sv in raw_s.items():
            if not _SAFE_NODE_ID_RE.match(str(sk)):
                raise ValueError(f"凭据节点 ID 非法: {sk}")
            if sv is not None and not isinstance(sv, str):
                raise ValueError(f"凭据 {sk} 必须为字符串或 null")
            sec_updates[str(sk)] = sv

    reset_h = bool(body.get("reset_health", False))
    if "reset_health" in body and not isinstance(body["reset_health"], bool):
        raise ValueError("字段 reset_health 必须为布尔值")
    return cfg_updates, sec_updates, reset_h


def handle_get_llm_fallback(handler) -> None:
    try:
        handler._send_json(llm_fallback_router.get_fallback_pool().get_status())
    except Exception as exc:
        shared._log_rate_limited("llm_fb_get", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_llm_fallback(handler) -> None:
    try:
        body = handler._read_body_json()
        cfg_updates, sec_updates, reset_h = _validate_fallback_payload(body)
        pool = llm_fallback_router.get_fallback_pool()
        if sec_updates:
            pool.update_node_secrets(sec_updates)
        if cfg_updates:
            runtime_settings.set_llm_fallback(**cfg_updates)
        if reset_h:
            pool.reset_health()
        handler._send_json(pool.get_status())
    except shared._BadRequestError:
        raise
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception as exc:
        shared._log_rate_limited("llm_fb_post", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

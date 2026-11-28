"""18080 Web Console routes for Phase 3 Official QQ Bot WebSocket bidirectional bridge."""

from __future__ import annotations

from http import HTTPStatus
import re
from typing import Any, Dict, List

import qq_bot_bridge
import runtime_settings
from console import shared

_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "enabled",
        "sandbox",
        "share_primary_memory",
        "memory_device_id",
        "allow_l3_dispatch",
        "owner_openid",
        "allowed_openids",
        "allowed_group_openids",
        "batch_quiet_ms",
        "secrets",
    }
)
_ALLOWED_NOTIFY_KEYS = frozenset({"title", "body", "source"})
_SAFE_OPENID_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")


def _clean_openid_list(raw: Any, field_name: str) -> List[str]:
    if not isinstance(raw, list):
        raise ValueError(f"字段 {field_name} 必须为字符串数组")
    out: List[str] = []
    seen: set[str] = set()
    for item in raw[:32]:
        s = str(item or "").strip()
        if not s:
            continue
        if not _SAFE_OPENID_RE.match(s):
            raise ValueError(f"字段 {field_name} 包含非法 ID: {s[:24]}")
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def handle_get_qq_bot(handler) -> None:
    try:
        handler._send_json(qq_bot_bridge.get_qq_bot_bridge().get_status())
    except Exception as exc:
        shared._log_rate_limited("qq_get_status", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_qq_bot(handler) -> None:
    try:
        body = handler._read_body_json()
        if not isinstance(body, dict) or not body:
            handler._send_json({"ok": False, "error": "请提供 QQ 机器人配置字段"}, HTTPStatus.BAD_REQUEST)
            return
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_CONFIG_KEYS)
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        cfg_updates: Dict[str, Any] = {}
        for bkey in ("enabled", "sandbox", "share_primary_memory", "allow_l3_dispatch"):
            if bkey in body:
                if not isinstance(body[bkey], bool):
                    raise ValueError(f"字段 {bkey} 必须为布尔值")
                cfg_updates[bkey] = body[bkey]

        if "memory_device_id" in body:
            if not isinstance(body["memory_device_id"], str):
                raise ValueError("字段 memory_device_id 必须为字符串")
            cfg_updates["memory_device_id"] = body["memory_device_id"].strip()[:64]

        if "owner_openid" in body:
            if not isinstance(body["owner_openid"], str):
                raise ValueError("字段 owner_openid 必须为字符串")
            raw_o = body["owner_openid"].strip()
            if raw_o and not _SAFE_OPENID_RE.match(raw_o):
                raise ValueError("owner_openid 格式非法")
            cfg_updates["owner_openid"] = raw_o

        if "allowed_openids" in body:
            cfg_updates["allowed_openids"] = _clean_openid_list(body["allowed_openids"], "allowed_openids")
        if "allowed_group_openids" in body:
            cfg_updates["allowed_group_openids"] = _clean_openid_list(
                body["allowed_group_openids"], "allowed_group_openids"
            )
        if "batch_quiet_ms" in body:
            raw_bq = body["batch_quiet_ms"]
            if isinstance(raw_bq, bool) or not isinstance(raw_bq, int):
                raise ValueError("字段 batch_quiet_ms 必须为整数（0～15000）")
            if raw_bq < 0 or raw_bq > 15000:
                raise ValueError("字段 batch_quiet_ms 必须在 0～15000 之间")
            cfg_updates["batch_quiet_ms"] = raw_bq

        bridge = qq_bot_bridge.get_qq_bot_bridge()
        prev_cfg = bridge.get_config()
        secrets_updated = False
        if "secrets" in body:
            sec = body["secrets"]
            if not isinstance(sec, dict):
                raise ValueError("字段 secrets 必须为对象")
            unk_s = sorted(str(k) for k in sec.keys() if k not in ("app_id", "app_secret"))
            if unk_s:
                raise ValueError(f"包含未允许凭据字段: {', '.join(unk_s)}")
            for sk in ("app_id", "app_secret"):
                if sk in sec and not isinstance(sec[sk], str):
                    raise ValueError(f"字段 secrets.{sk} 必须为字符串")
            bridge.update_secrets(
                app_id=sec["app_id"] if "app_id" in sec else None,
                app_secret=sec["app_secret"] if "app_secret" in sec else None,
            )
            secrets_updated = True

        if cfg_updates:
            runtime_settings.set_qq_bot(**cfg_updates)

        cur_cfg = bridge.get_config()
        sandbox_changed = ("sandbox" in cfg_updates) and (bool(prev_cfg.get("sandbox")) != bool(cur_cfg.get("sandbox")))
        if cur_cfg.get("enabled", False):
            if secrets_updated or sandbox_changed:
                bridge.request_reconnect()
            else:
                bridge.ensure_running_if_enabled()
        elif cfg_updates:
            bridge.ensure_running_if_enabled()

        handler._send_json(bridge.get_status())
    except shared._BadRequestError:
        raise
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception as exc:
        shared._log_rate_limited("qq_post_config", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_qq_bot_action(handler) -> None:
    try:
        body = handler._read_body_json()
        if not isinstance(body, dict):
            raise ValueError("请求体必须为 JSON 对象")
        unk_act = sorted(str(k) for k in body.keys() if k not in ("action", "openid", "set_as_owner"))
        if unk_act:
            raise ValueError(f"包含未允许字段: {', '.join(unk_act[:6])}")
        action = str(body.get("action") or "").strip().lower()
        bridge = qq_bot_bridge.get_qq_bot_bridge()

        if action == "start_pairing":
            pair_info = bridge.start_pairing()
            handler._send_json({"ok": True, "pairing": pair_info, "qq_bot": bridge.get_status()})
            return
        if action == "approve_pairing":
            raw_oid = body.get("openid")
            if not isinstance(raw_oid, str) or not raw_oid.strip():
                raise ValueError("openid 必须为非空字符串")
            raw_owner = body.get("set_as_owner", True)
            if not isinstance(raw_owner, bool):
                raise ValueError("set_as_owner 必须为布尔值")
            st = bridge.approve_pairing(raw_oid.strip(), set_as_owner=raw_owner)
            handler._send_json({"ok": True, "qq_bot": st})
            return
        if action == "reconnect":
            bridge.request_reconnect()
            handler._send_json({"ok": True, "qq_bot": bridge.get_status()})
            return

        raise ValueError(f"不支持的 QQ 机器人操作: {action!r}")
    except shared._BadRequestError:
        raise
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception as exc:
        shared._log_rate_limited("qq_post_action", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_qq_bot_notify(handler) -> None:
    try:
        body = handler._read_body_json()
        if not isinstance(body, dict) or not body:
            raise ValueError("请求体必须为非空 JSON 对象")
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_NOTIFY_KEYS)
        if unknown:
            raise ValueError(f"包含未允许字段: {', '.join(unknown[:6])}")

        raw_title = body.get("title", "")
        raw_body = body.get("body", "")
        raw_source = body.get("source", "antigravity")
        if not isinstance(raw_title, str):
            raise ValueError("字段 title 必须为字符串")
        if not isinstance(raw_body, str):
            raise ValueError("字段 body 必须为字符串")
        if not isinstance(raw_source, str):
            raise ValueError("字段 source 必须为字符串")

        title = raw_title.strip()
        text_body = raw_body.strip()
        source = raw_source.strip() or "antigravity"
        if not title and not text_body:
            raise ValueError("title 与 body 不能同时为空")
        if len(title) > 120:
            raise ValueError("字段 title 长度不能超过 120 字符")
        if len(text_body) > 900:
            raise ValueError("字段 body 长度不能超过 900 字符")
        if len(source) > 32:
            raise ValueError("字段 source 长度不能超过 32 字符")

        res = qq_bot_bridge.get_qq_bot_bridge().send_task_notification(title, text_body, source=source)
        if res.get("ok"):
            handler._send_json(res, HTTPStatus.OK)
            return
        reason = str(res.get("reason") or "")
        if reason in ("disabled", "no_owner_openid"):
            status = HTTPStatus.CONFLICT
        elif reason == "daily_limit":
            status = HTTPStatus.TOO_MANY_REQUESTS
        elif reason == "empty_content":
            status = HTTPStatus.BAD_REQUEST
        else:
            status = HTTPStatus.BAD_GATEWAY
        handler._send_json(res, status)
    except shared._BadRequestError:
        raise
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception as exc:
        shared._log_rate_limited("qq_post_notify", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

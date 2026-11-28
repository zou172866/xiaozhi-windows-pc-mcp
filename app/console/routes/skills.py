"""18080 Web Console routes for Phase 4 progressive `SKILL.md` loader."""

from __future__ import annotations

from http import HTTPStatus

import runtime_settings
import skill_loader
from console import shared

_ALLOWED_KEYS = frozenset({"enabled", "refresh"})


def handle_get_skills(handler) -> None:
    try:
        handler._send_json(skill_loader.get_skill_registry().get_status(force_scan=False))
    except Exception as exc:
        shared._log_rate_limited("skills_get", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_skills(handler) -> None:
    try:
        body = handler._read_body_json()
        if not isinstance(body, dict) or not body:
            handler._send_json({"ok": False, "error": "请提供技能加载配置字段"}, HTTPStatus.BAD_REQUEST)
            return
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_KEYS)
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise ValueError("字段 enabled 必须为布尔值")
            runtime_settings.set_skill_loader(enabled=body["enabled"])
        force_scan = bool(body.get("refresh", False))
        if "refresh" in body and not isinstance(body["refresh"], bool):
            raise ValueError("字段 refresh 必须为布尔值")
        handler._send_json(skill_loader.get_skill_registry().get_status(force_scan=force_scan))
    except shared._BadRequestError:
        raise
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception as exc:
        shared._log_rate_limited("skills_post", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

"""18080 Web Console routes for Phase 1 natural-language future tasks & Cron reminders."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Dict

import future_tasks
from console import shared

_ALLOWED_CREATE_KEYS = frozenset(
    {
        "action",
        "task_id",
        "title",
        "message",
        "schedule_type",
        "run_at",
        "delay_minutes",
        "cron_expr",
        "task_kind",
        "delivery",
        "respect_quiet_hours",
        "city",
        "force_dry_run",
    }
)


def handle_get_future_tasks(handler) -> None:
    try:
        eng = future_tasks.get_future_task_engine()
        handler._send_json(eng.get_status(include_inactive=True))
    except Exception as exc:
        shared._log_rate_limited("ft_get_status", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_future_tasks(handler) -> None:
    try:
        body = handler._read_body_json()
        if not isinstance(body, dict) or not body:
            handler._send_json({"ok": False, "error": "请提供定时任务请求参数"}, HTTPStatus.BAD_REQUEST)
            return
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_CREATE_KEYS)
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        eng = future_tasks.get_future_task_engine()
        action = str(body.get("action") or "create").strip().lower()

        if action == "create":
            req_quiet = body.get("respect_quiet_hours")
            if req_quiet is not None and not isinstance(req_quiet, bool):
                raise ValueError("字段 respect_quiet_hours 必须为布尔值")
            task = eng.create_task(
                title=str(body.get("title") or ""),
                message=str(body.get("message") or ""),
                schedule_type=str(body.get("schedule_type") or ""),
                run_at=body.get("run_at"),
                delay_minutes=body.get("delay_minutes"),
                cron_expr=str(body.get("cron_expr") or ""),
                task_kind=str(body.get("task_kind") or "reminder"),
                delivery=str(body.get("delivery") or "pc_and_mobile"),
                respect_quiet_hours=req_quiet,
                city=str(body.get("city") or ""),
                source="console",
            )
            handler._send_json({"ok": True, "task": task, "state": eng.get_status(include_inactive=True)})
            return

        tid = str(body.get("task_id") or "").strip()
        if not tid:
            raise ValueError(f"操作 {action} 需要提供 task_id")

        if action == "cancel":
            task = eng.cancel_task(tid)
            handler._send_json({"ok": True, "task": task, "state": eng.get_status(include_inactive=True)})
            return
        if action == "pause":
            task = eng.pause_task(tid)
            handler._send_json({"ok": True, "task": task, "state": eng.get_status(include_inactive=True)})
            return
        if action == "resume":
            task = eng.resume_task(tid)
            handler._send_json({"ok": True, "task": task, "state": eng.get_status(include_inactive=True)})
            return
        if action == "delete":
            deleted = eng.delete_task(tid)
            if not deleted:
                handler._send_json({"ok": False, "error": f"未找到任务 {tid}"}, HTTPStatus.NOT_FOUND)
                return
            handler._send_json({"ok": True, "deleted": tid, "state": eng.get_status(include_inactive=True)})
            return
        if action == "trigger":
            force_dry = body.get("force_dry_run", True)
            if not isinstance(force_dry, bool):
                raise ValueError("字段 force_dry_run 必须为布尔值")
            res = eng.trigger_now(tid, force_dry_run=force_dry)
            res["state"] = eng.get_status(include_inactive=True)
            handler._send_json(res, HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_GATEWAY)
            return

        raise ValueError(f"不支持的操作类型: {action}")
    except shared._BadRequestError:
        raise
    except KeyError as exc:
        handler._send_json({"ok": False, "error": str(exc).strip("'")}, HTTPStatus.NOT_FOUND)
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception as exc:
        shared._log_rate_limited("ft_post_action", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

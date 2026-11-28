"""Native LLM function tool patch for natural-language future tasks & Cron reminders (Phase 1).

Zero import-time side effects: call `install_future_task_tool_patch()` explicitly from `boot.install_all()`.
Registers `manage_future_task` into `plugins_func.register.all_function_registry` and auto-exposes it
via `patches.auth.register_auto_intent_function("manage_future_task")`.
"""

from __future__ import annotations

import logging
from typing import Any

import future_tasks
import patch_registry
from patches import auth as auth_patch

TAG = "future_task_tool"
_log = logging.getLogger(TAG)

FUTURE_TASK_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "manage_future_task",
        "description": (
            "创建、查询、暂停、恢复或取消定时提醒与周期性未来任务（如“10分钟后提醒我喝水”、"
            "“明天早上8点叫我顺便报天气”、“每晚10点提醒我收尾”、“看看我设了哪些提醒”、“取消喝水提醒”）。"
            "仅在用户明确要求设定/查看/取消定时提醒或周期简报时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "cancel", "pause", "resume"],
                    "description": "操作类型：create(新建)、list(查看列表)、cancel(取消)、pause(暂停)、resume(恢复)",
                },
                "title": {
                    "type": "string",
                    "description": "任务简短标题（例如：喝水提醒、早安天气简报）",
                },
                "message": {
                    "type": "string",
                    "description": "到点播报或推送的提醒正文内容（可留空，默认复用 title）",
                },
                "schedule_type": {
                    "type": "string",
                    "enum": ["once", "cron"],
                    "description": "once 为一次性提醒；cron 为周期性任务",
                },
                "delay_minutes": {
                    "type": "number",
                    "description": "一次性任务的相对延迟分钟数（如 10 表示 10 分钟后）",
                },
                "run_at": {
                    "type": "string",
                    "description": "一次性任务的本地绝对时间，格式 YYYY-MM-DD HH:MM 或 HH:MM（如 2026-11-28 08:00 或 08:00）",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "5 段 Cron 表达式（分 时 日 月 周），如每天早 8 点 `0 8 * * *`，最小间隔须 >= 5 分钟",
                },
                "task_kind": {
                    "type": "string",
                    "enum": ["reminder", "weather_briefing"],
                    "description": "reminder 为普通提醒；weather_briefing 到点自动附带实时天气简报",
                },
                "city": {
                    "type": "string",
                    "description": "当 task_kind=weather_briefing 时可选指定城市名（如 上海、长沙）",
                },
                "delivery": {
                    "type": "string",
                    "enum": ["pc_speaker", "mobile_push", "pc_and_mobile", "qq_bot"],
                    "description": "投递方式，默认 pc_and_mobile（电脑播报并抄送手机）",
                },
                "task_id": {
                    "type": "string",
                    "description": "cancel / pause / resume 时指定的任务 ID 或标题关键词",
                },
            },
            "required": ["action"],
        },
    },
}


def execute_future_task_tool(
    action: str,
    *,
    title: str = "",
    message: str = "",
    schedule_type: str = "",
    delay_minutes: Any = None,
    run_at: str = "",
    cron_expr: str = "",
    task_kind: str = "reminder",
    city: str = "",
    delivery: str = "pc_and_mobile",
    task_id: str = "",
    source: str = "voice",
) -> str:
    """Core logic shared by upstream FunctionItem handler and QQ Bot bridge."""
    eng = future_tasks.get_future_task_engine()
    act = str(action or "").strip().lower()

    if act == "list":
        tasks = eng.list_tasks(include_inactive=False)
        if not tasks:
            return "当前没有任何待执行的定时提醒或周期任务。"
        lines = [f"当前共有 {len(tasks)} 条定时任务："]
        for t in tasks[:15]:
            sched = f"周期 `{t['cron_expr']}`" if t["schedule_type"] == "cron" else "一次性"
            st_zh = "运行中" if t["status"] == "active" else "已暂停"
            lines.append(
                f"- [{t['id']}] {t['title']}（{st_zh} · {sched} · 下次 {t['next_run_text'] or '--'}）"
            )
        return "\n".join(lines)

    if act == "create":
        try:
            task = eng.create_task(
                title=title or message or "定时提醒",
                message=message or title,
                schedule_type=schedule_type or ("cron" if str(cron_expr or "").strip() else "once"),
                run_at=run_at or None,
                delay_minutes=delay_minutes,
                cron_expr=cron_expr,
                task_kind=task_kind or "reminder",
                delivery=delivery or "pc_and_mobile",
                city=city,
                source=source,
            )
            sched_desc = (
                f"周期 `{task['cron_expr']}`，首次触发 {task['next_run_text']}"
                if task["schedule_type"] == "cron"
                else f"触发时间 {task['next_run_text']}"
            )
            return f"已为你设定定时任务「{task['title']}」（编号 {task['id']}，{sched_desc}）。"
        except ValueError as exc:
            return f"创建定时任务失败：{exc}"
        except Exception as exc:
            _log.warning("create_task error: %s", exc)
            return "创建定时任务时出现内部异常，请稍后再试。"

    if act in ("cancel", "pause", "resume"):
        key = str(task_id or title or "").strip()
        if not key:
            return f"请说明要{act}的任务编号或标题关键词。"
        target = eng.get_task(key) or eng.find_task_by_keyword(key)
        if target is None:
            return f"没有找到匹配「{key}」的活跃定时任务。"
        try:
            if act == "cancel":
                updated = eng.cancel_task(target["id"])
                return f"已取消定时任务「{updated['title']}」（{updated['id']}）。"
            if act == "pause":
                updated = eng.pause_task(target["id"])
                return f"已暂停定时任务「{updated['title']}」（{updated['id']}）。"
            updated = eng.resume_task(target["id"])
            return f"已恢复定时任务「{updated['title']}」（下次触发 {updated['next_run_text']}）。"
        except ValueError as exc:
            return f"操作定时任务失败：{exc}"

    return f"不支持的操作类型：{action!r}（仅支持 create / list / cancel / pause / resume）。"


def manage_future_task_handler(
    conn,
    action: str = "list",
    title: str = "",
    message: str = "",
    schedule_type: str = "",
    delay_minutes: Any = None,
    run_at: str = "",
    cron_expr: str = "",
    task_kind: str = "reminder",
    city: str = "",
    delivery: str = "pc_and_mobile",
    task_id: str = "",
):
    from plugins_func.register import Action, ActionResponse

    result_text = execute_future_task_tool(
        action=action,
        title=title,
        message=message,
        schedule_type=schedule_type,
        delay_minutes=delay_minutes,
        run_at=run_at,
        cron_expr=cron_expr,
        task_kind=task_kind,
        city=city,
        delivery=delivery,
        task_id=task_id,
        source="voice",
    )
    return ActionResponse(Action.REQLLM, result_text, None)


def install_future_task_tool_patch() -> None:
    """Register `manage_future_task` into upstream `plugins_func.register.all_function_registry`."""
    import plugins_func.register as reg_mod

    FunctionItem = getattr(reg_mod, "FunctionItem", None)
    ToolType = getattr(reg_mod, "ToolType", None)
    all_function_registry = getattr(reg_mod, "all_function_registry", None)
    Action = getattr(reg_mod, "Action", None)
    ActionResponse = getattr(reg_mod, "ActionResponse", None)

    if (
        FunctionItem is None
        or ToolType is None
        or not isinstance(all_function_registry, dict)
        or Action is None
        or ActionResponse is None
        or not hasattr(ToolType, "SYSTEM_CTL")
    ):
        patch_registry.record_patch(
            "future_task_tool",
            "skipped_contract",
            target="plugins_func.register.all_function_registry",
            detail="missing FunctionItem/ToolType.SYSTEM_CTL/all_function_registry/ActionResponse",
            contract_ok=False,
            category="experience",
        )
        print("[FutureTasks] 跳过挂载定时任务工具补丁（上游注册表契约不匹配）", flush=True)
        return

    if getattr(reg_mod, "_future_task_tool_patched", False) and "manage_future_task" in all_function_registry:
        auth_patch.register_auto_intent_function("manage_future_task")
        if patch_registry.get_patch_status("future_task_tool") is None:
            patch_registry.record_patch(
                "future_task_tool",
                "ok",
                target="plugins_func.register.all_function_registry",
                detail="already registered",
                contract_ok=True,
                category="experience",
            )
        return

    all_function_registry["manage_future_task"] = FunctionItem(
        "manage_future_task",
        FUTURE_TASK_FUNCTION_DESC,
        manage_future_task_handler,
        ToolType.SYSTEM_CTL,
    )
    reg_mod._future_task_tool_patched = True
    auth_patch.register_auto_intent_function("manage_future_task")
    patch_registry.record_patch(
        "future_task_tool",
        "ok",
        target="plugins_func.register.all_function_registry",
        detail="registered manage_future_task ToolType.SYSTEM_CTL + auto-intent exposure",
        contract_ok=True,
        category="experience",
    )
    print("[FutureTasks] 已注册原生定时提醒与 Cron 工具 manage_future_task", flush=True)

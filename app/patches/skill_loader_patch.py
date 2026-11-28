"""Native LLM tool patch for progressive on-demand `SKILL.md` loading (Phase 4).

Zero import-time side effects: call `install_skill_loader_patch()` explicitly from `boot.install_all()`.
Registers `load_skill` into `plugins_func.register.all_function_registry` and auto-exposes it
via `patches.auth.register_auto_intent_function("load_skill")`.
"""

from __future__ import annotations

import patch_registry
import skill_loader
from patches import auth as auth_patch

LOAD_SKILL_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "按需加载指定本地操作技能（SKILL.md）的完整详细指引。"
            "仅当 `<available_skills>` 中列出的某项技能与用户当前任务直接相关、且需要查阅其操作步骤或注意事项时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "要加载的技能名称（须完全匹配 `<available_skills>` 中的名称）",
                }
            },
            "required": ["skill_name"],
        },
    },
}


def load_skill_handler(conn, skill_name: str = ""):
    from plugins_func.register import Action, ActionResponse

    reg = skill_loader.get_skill_registry()
    if not reg.is_enabled():
        return ActionResponse(Action.REQLLM, "技能渐进加载当前已在 18080 控制台关闭。", None)
    ok, body_or_err = reg.load_skill_body(skill_name)
    if not ok:
        return ActionResponse(Action.REQLLM, body_or_err, None)
    return ActionResponse(
        Action.REQLLM,
        f"<loaded_skill name=\"{skill_name}\">\n{body_or_err}\n</loaded_skill>",
        None,
    )


def install_skill_loader_patch() -> None:
    """Register `load_skill` into upstream `plugins_func.register.all_function_registry`."""
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
            "skill_progressive_loader",
            "skipped_contract",
            target="plugins_func.register.all_function_registry",
            detail="missing FunctionItem/ToolType.SYSTEM_CTL/all_function_registry/ActionResponse",
            contract_ok=False,
            category="experience",
        )
        print("[SkillLoader] 跳过挂载技能按需加载补丁（上游注册表契约不匹配）", flush=True)
        return

    if getattr(reg_mod, "_skill_loader_patched", False) and "load_skill" in all_function_registry:
        auth_patch.register_auto_intent_function("load_skill")
        if patch_registry.get_patch_status("skill_progressive_loader") is None:
            patch_registry.record_patch(
                "skill_progressive_loader",
                "ok",
                target="plugins_func.register.all_function_registry",
                detail="already registered",
                contract_ok=True,
                category="experience",
            )
        return

    all_function_registry["load_skill"] = FunctionItem(
        "load_skill",
        LOAD_SKILL_FUNCTION_DESC,
        load_skill_handler,
        ToolType.SYSTEM_CTL,
    )
    reg_mod._skill_loader_patched = True
    auth_patch.register_auto_intent_function("load_skill")
    patch_registry.record_patch(
        "skill_progressive_loader",
        "ok",
        target="plugins_func.register.all_function_registry",
        detail="registered load_skill ToolType.SYSTEM_CTL + prefix-cache-safe <available_skills> turn_context index",
        contract_ok=True,
        category="experience",
    )
    print("[SkillLoader] 已注册 SKILL.md 渐进式按需加载工具 load_skill", flush=True)

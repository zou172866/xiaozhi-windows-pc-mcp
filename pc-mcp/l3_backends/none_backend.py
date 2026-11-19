"""Disabled L3 backend (L1 + L2 local desktop control only)."""

from __future__ import annotations

from typing import Any, Dict

from . import L3Backend


class NoneBackend(L3Backend):
    name = "none"
    label_zh = "关闭 L3 后台派单"

    def detect(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        return {"ready": True, "detail": "已关闭 L3 后台 Agent（仅保留 L1 系统控制与 L2 桌面操控）"}

    def dispatch(
        self, task_id: str, instruction: str, follow_up_last: bool, cfg: Dict[str, Any]
    ) -> str:
        return "当前已在 18080 控制台关闭 L3 后台 Agent 派单。如需执行复杂工程任务，请先切换为 DeepSeek DSH、腾讯 WorkBuddy 或反重力。"

    def check_status(self, cfg: Dict[str, Any]) -> str:
        return "当前未启用 L3 后台 Agent 派单。"

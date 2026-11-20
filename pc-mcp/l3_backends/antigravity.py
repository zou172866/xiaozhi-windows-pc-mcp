"""Google Antigravity official Sidecar + agentapi L3 backend."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

from . import (
    ACTIVE_DIR,
    HEARTBEAT_FILE,
    L3Backend,
    PENDING_DIR,
    atomic_write_json,
    load_last_active_task,
    read_env_key,
)


def _get_brain_dir() -> Path:
    custom = read_env_key("ANTIGRAVITY_BRAIN_DIR")
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".gemini" / "antigravity" / "brain"


def _summarize_transcript(conv_id: str) -> str:
    t_path = _get_brain_dir() / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not t_path.exists():
        return f"会话 ({conv_id[:8]}) 已创建，正在初始化或等待执行中。"

    lines = t_path.read_text(encoding="utf-8", errors="replace").splitlines()
    last_model_reply = ""
    last_step_type = ""
    last_status = ""
    for ln in reversed(lines):
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
            if not last_step_type:
                last_step_type = obj.get("type", "")
                last_status = obj.get("status", "")
            if obj.get("type") == "PLANNER_RESPONSE" and obj.get("content"):
                last_model_reply = str(obj["content"]).strip()
                break
        except Exception:
            pass

    if last_model_reply:
        short = last_model_reply[:280].replace("\n", " ")
        return f"反重力最新进展（状态 {last_status}）：{short}"
    return f"反重力正在后台执行工具步骤中（当前步骤类型: {last_step_type}，共 {len(lines)} 步），请稍后再查询。"


def _is_worker_alive() -> bool:
    if not HEARTBEAT_FILE.exists():
        return False
    try:
        hb = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
        return (time.time() - float(hb.get("ts", 0))) < 15.0
    except Exception:
        return False


class AntigravityBackend(L3Backend):
    name = "antigravity"
    label_zh = "Google Antigravity（反重力）"

    def detect(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        alive = _is_worker_alive()
        local_app = os.environ.get("LOCALAPPDATA", "")
        exe_exists = bool(local_app and (Path(local_app) / "Programs" / "antigravity" / "Antigravity.exe").exists())
        if alive:
            return {"ready": True, "detail": "伴生桥接在线（可随时语音派单）"}
        if exe_exists:
            return {"ready": False, "detail": "已安装客户端，待在 Automations 面板开启 Xiaozhi Bridge"}
        return {"ready": False, "detail": "未检测到本机 Antigravity 客户端或伴生桥接"}

    def dispatch(
        self, task_id: str, instruction: str, follow_up_last: bool, cfg: Dict[str, Any]
    ) -> str:
        instr = (instruction or "").strip()
        if not instr:
            return "任务内容不能为空。"

        conv_id = ""
        if follow_up_last:
            last = load_last_active_task(backend="antigravity")
            if last and last.get("conversation_id"):
                conv_id = str(last["conversation_id"])

        task_obj = {
            "id": task_id,
            "backend": "antigravity",
            "title": f"[小智派单] {instr[:24]}",
            "prompt": instr,
            "conversation_id": conv_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        pending_file = PENDING_DIR / f"{task_id}.json"
        atomic_write_json(pending_file, task_obj)

        if not _is_worker_alive():
            return (
                "任务已为你存入派单队列！不过检测到反重力客户端里的【小智语音派单桥接】伴生服务还没开启，"
                "请在 Antigravity 的 Automations 面板点一下启用，启用后队列里的任务就会立刻自动跑起来。"
            )

        active_file = ACTIVE_DIR / f"{task_id}.json"
        for _ in range(14):
            if active_file.exists():
                try:
                    res = json.loads(active_file.read_text(encoding="utf-8"))
                except Exception:
                    break
                if res.get("status") == "dispatched":
                    cid_short = str(res.get("conversation_id") or "")[:8]
                    return f"已成功派单给本机反重力后台执行（会话 {cid_short}），不占用语音等待时间，稍后可随时问我执行结果！"
                return f"派单遇到错误：{res.get('stderr') or '请检查反重力客户端状态'}"
            time.sleep(0.25)

        return "已成功投递给本机反重力后台队列开始处理，稍后可随时问我进度！"

    def check_status(self, cfg: Dict[str, Any]) -> str:
        last = load_last_active_task(backend="antigravity")
        if not last:
            pending = list(PENDING_DIR.glob("*.json"))
            if pending:
                return f"当前有 {len(pending)} 个任务在队列中等待反重力伴生桥接领取执行。"
            return "目前还没有派发过反重力任务。"
        if last.get("status") != "dispatched" or not last.get("conversation_id"):
            return f"最近一次任务派发状态异常：{last.get('stderr') or last.get('stdout') or '未知错误'}"
        return _summarize_transcript(str(last["conversation_id"]))

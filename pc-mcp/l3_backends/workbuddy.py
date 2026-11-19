"""Tencent WorkBuddy / CodeBuddy CLI non-interactive L3 backend."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import (
    L3Backend,
    load_last_active_task,
    resolve_workdir,
    save_active_task,
    split_command,
    summarize_text_for_voice,
)

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def resolve_workbuddy_prefix(cfg: Dict[str, Any]) -> Tuple[List[str], str]:
    """Auto-detect Tencent WorkBuddy / CodeBuddy CLI (`workbuddy` / `codebuddy` / `cbc`)."""
    custom = split_command(str(cfg.get("workbuddy_cmd") or ""))
    if custom:
        return custom, f"自定义命令 ({custom[0]})"

    for candidate in ("workbuddy", "workbuddy.cmd", "codebuddy", "codebuddy.cmd", "cbc", "cbc.cmd"):
        found = shutil.which(candidate)
        if found:
            return [found], f"本机 CLI ({candidate})"

    appdata = os.environ.get("APPDATA", "")
    if appdata:
        npm_dir = Path(appdata) / "npm"
        for name in ("workbuddy.cmd", "codebuddy.cmd", "cbc.cmd"):
            p = npm_dir / name
            if p.is_file():
                return [str(p)], f"本机 npm CLI ({name})"

    return [], ""


def _has_workbuddy_desktop_app() -> bool:
    local_app = os.environ.get("LOCALAPPDATA", "")
    if not local_app:
        return False
    for rel in (
        Path("Programs") / "WorkBuddy" / "WorkBuddy.exe",
        Path("Programs") / "Tencent" / "WorkBuddy" / "WorkBuddy.exe",
    ):
        if (Path(local_app) / rel).exists():
            return True
    return False


class WorkBuddyBackend(L3Backend):
    name = "workbuddy"
    label_zh = "腾讯 WorkBuddy / CodeBuddy"

    def detect(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        prefix, source = resolve_workbuddy_prefix(cfg)
        if prefix:
            return {"ready": True, "detail": f"已就绪：{source}（国内直连）"}
        if _has_workbuddy_desktop_app():
            return {
                "ready": False,
                "detail": "检测到桌面客户端，但未找到 CLI，请安装 CodeBuddy/WorkBuddy CLI 或在下方填写命令路径",
            }
        return {
            "ready": False,
            "detail": "未检测到 workbuddy / codebuddy / cbc 命令，请安装腾讯 WorkBuddy / CodeBuddy CLI 或填写自定义路径",
        }

    def _build_command(
        self, prefix: List[str], prompt: str, follow_up_last: bool, auto_approve: bool
    ) -> List[str]:
        cmd = [*prefix]
        if follow_up_last:
            cmd.append("-c")
        cmd.extend(["-p", prompt, "--output-format", "text"])
        if auto_approve:
            cmd.append("-y")
        return cmd

    def dispatch(
        self, task_id: str, instruction: str, follow_up_last: bool, cfg: Dict[str, Any]
    ) -> str:
        instr = (instruction or "").strip()
        if not instr:
            return "任务内容不能为空。"

        prefix, source = resolve_workbuddy_prefix(cfg)
        if not prefix:
            return (
                "当前未检测到腾讯 WorkBuddy 或 CodeBuddy 命令行工具（workbuddy / codebuddy / cbc）。"
                "请先安装并登录对应 CLI，或在 18080 控制台的高级设置中填写命令路径。"
            )

        workdir = resolve_workdir(cfg)
        auto_approve = bool(cfg.get("workbuddy_auto_approve", True))
        cmd = self._build_command(prefix, instr, follow_up_last, auto_approve)
        timeout_sec = int(cfg.get("timeout_seconds", 300))

        task_record: Dict[str, Any] = {
            "id": task_id,
            "backend": "workbuddy",
            "title": f"[WorkBuddy派单] {instr[:24]}",
            "prompt": instr,
            "workdir": str(workdir),
            "command_source": source,
            "follow_up_last": bool(follow_up_last),
            "status": "running",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started_ts": time.time(),
        }
        save_active_task(task_id, task_record)

        def _worker():
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(workdir),
                    env=os.environ.copy(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_sec,
                    creationflags=_CREATE_NO_WINDOW,
                )
                stdout = (proc.stdout or "").strip()
                stderr = (proc.stderr or "").strip()
                task_record["returncode"] = proc.returncode
                task_record["stdout"] = stdout[-4000:]
                task_record["stderr"] = stderr[-2000:]
                task_record["summary"] = summarize_text_for_voice(stdout or stderr, 300)
                task_record["status"] = "completed" if proc.returncode == 0 else "error"
            except subprocess.TimeoutExpired:
                task_record["status"] = "error"
                task_record["stderr"] = f"执行超过 {timeout_sec} 秒已自动中止"
                task_record["summary"] = task_record["stderr"]
            except Exception as exc:
                task_record["status"] = "error"
                task_record["stderr"] = str(exc)
                task_record["summary"] = str(exc)
            finally:
                task_record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save_active_task(task_id, task_record)

        threading.Thread(target=_worker, name=f"l3-wb-{task_id}", daemon=True).start()
        return f"已成功派单给本机腾讯 WorkBuddy 后台执行（工作目录 {workdir.name}），稍后可随时问我进展！"

    def check_status(self, cfg: Dict[str, Any]) -> str:
        last = load_last_active_task(backend="workbuddy")
        if not last:
            return "目前还没有派发过腾讯 WorkBuddy 后台任务。"
        st = str(last.get("status") or "")
        if st == "running":
            elapsed = max(1, int(time.time() - float(last.get("started_ts") or time.time())))
            return f"腾讯 WorkBuddy 正在后台处理「{str(last.get('prompt') or '')[:24]}」，已运行约 {elapsed} 秒，请稍后再问我。"
        if st == "completed":
            summary = str(last.get("summary") or "").strip() or "任务已完成，未返回额外文字输出。"
            return f"腾讯 WorkBuddy 已完成任务：{summary}"
        err = str(last.get("stderr") or last.get("summary") or "未知错误")[:220]
        return f"腾讯 WorkBuddy 上一次任务执行异常：{err}"

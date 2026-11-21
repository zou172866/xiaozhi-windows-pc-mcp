"""Tencent WorkBuddy / CodeBuddy CLI non-interactive L3 backend."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import (
    L3Backend,
    claim_workspace_or_enqueue,
    get_process_create_time,
    get_task_log_paths,
    guard_leading_dash_prompt,
    is_task_cancelled,
    load_last_active_task,
    register_spawned_task_pid,
    release_workspace_slot,
    resolve_workdir,
    run_subprocess_with_tree_kill,
    save_active_task,
    split_command,
    start_worker_thread,
    summarize_text_for_voice,
    validate_task_id,
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
                "detail": "检测到桌面客户端，但未找到 CLI，请安装 CodeBuddy/WorkBuddy CLI 或在宿主机 .env 中配置 WORKBUDDY_CMD",
            }
        return {
            "ready": False,
            "detail": "未检测到 workbuddy / codebuddy / cbc 命令，请安装腾讯 WorkBuddy / CodeBuddy CLI 或在宿主机 .env 中配置 WORKBUDDY_CMD",
        }

    def _build_command(
        self, prefix: List[str], prompt: str, follow_up_last: bool, auto_approve: bool
    ) -> List[str]:
        safe_prompt = guard_leading_dash_prompt(
            prompt, prefix_tag="[追问]" if follow_up_last else "[指令]"
        )
        cmd = [*prefix]
        if follow_up_last:
            cmd.append("-c")
        cmd.extend(["-p", safe_prompt, "--output-format", "text"])
        if auto_approve:
            cmd.append("-y")
        return cmd

    def dispatch(
        self, task_id: str, instruction: str, follow_up_last: bool, cfg: Dict[str, Any]
    ) -> str:
        task_id = validate_task_id(task_id)
        instr = (instruction or "").strip()
        if not instr:
            return "任务内容不能为空。"

        prefix, source = resolve_workbuddy_prefix(cfg)
        if not prefix:
            return (
                "当前未检测到腾讯 WorkBuddy 或 CodeBuddy 命令行工具（workbuddy / codebuddy / cbc）。"
                "请先安装并登录对应 CLI，或在宿主机 .env 中配置 WORKBUDDY_CMD。"
            )

        workdir = resolve_workdir(cfg)
        auto_approve = bool(cfg.get("workbuddy_auto_approve", False))
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
            "owner_pid": os.getpid(),
            "owner_create_time": get_process_create_time(os.getpid()),
            "pid": None,
            "pid_create_time": None,
            "timeout_seconds": timeout_sec,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started_ts": time.time(),
        }

        def _start_now() -> None:
            if is_task_cancelled(task_id):
                release_workspace_slot(workdir, task_id)
                return

            stdout_log, stderr_log = get_task_log_paths(task_id)
            task_record["status"] = "running"
            task_record["started_ts"] = time.time()
            task_record["stdout_log"] = str(stdout_log)
            task_record["stderr_log"] = str(stderr_log)
            task_record.pop("queue_position", None)
            save_active_task(task_id, task_record)

            def _on_spawn(child_pid: int) -> None:
                # P1-11: Check cancellation atomically under _workspace_guard_lock; kill child immediately if cancelled.
                register_spawned_task_pid(task_id, task_record, child_pid)

            def _worker():
                if is_task_cancelled(task_id):
                    task_record["status"] = "cancelled"
                    task_record["stderr"] = "任务已被用户取消"
                    task_record["summary"] = "任务已被用户取消"
                    task_record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    save_active_task(task_id, task_record)
                    release_workspace_slot(workdir, task_id)
                    return

                env = os.environ.copy()
                env["PYTHONUTF8"] = "1"
                try:
                    proc = run_subprocess_with_tree_kill(
                        cmd,
                        cwd=str(workdir),
                        env=env,
                        timeout=timeout_sec,
                        creationflags=_CREATE_NO_WINDOW,
                        on_spawn=_on_spawn,
                        stdout_log_path=stdout_log,
                        stderr_log_path=stderr_log,
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
                    task_record["stderr"] = f"执行超过 {timeout_sec} 秒已自动中止并清理子进程树"
                    task_record["summary"] = task_record["stderr"]
                except Exception as exc:
                    task_record["status"] = "error"
                    task_record["stderr"] = str(exc)
                    task_record["summary"] = str(exc)
                finally:
                    if is_task_cancelled(task_id):
                        task_record["status"] = "cancelled"
                        task_record["stderr"] = "任务已被用户取消"
                        task_record["summary"] = "任务已被用户取消"
                    task_record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    save_active_task(task_id, task_record)
                    release_workspace_slot(workdir, task_id)

            start_worker_thread(task_id, f"l3-wb-{task_id}", _worker)

        outcome, guard_msg = claim_workspace_or_enqueue(
            workdir=workdir,
            task_id=task_id,
            backend="workbuddy",
            prompt=instr,
            cfg=cfg,
            start_fn=_start_now,
            queued_record=task_record,
        )
        if outcome in ("busy", "queued") and guard_msg:
            return guard_msg
        return f"已成功派单给本机腾讯 WorkBuddy 后台执行（工作目录 {workdir.name}），稍后可随时问我进展！"

    def check_status(self, cfg: Dict[str, Any]) -> str:
        last = load_last_active_task(backend="workbuddy")
        if not last:
            return "目前还没有派发过腾讯 WorkBuddy 后台任务。"
        st = str(last.get("status") or "")
        if st == "running":
            elapsed = max(1, int(time.time() - float(last.get("started_ts") or time.time())))
            return f"腾讯 WorkBuddy 正在后台处理「{str(last.get('prompt') or '')[:24]}」，已运行约 {elapsed} 秒，请稍后再问我。"
        if st == "queued":
            return f"腾讯 WorkBuddy 任务「{str(last.get('prompt') or '')[:24]}」正在队列中等待前序任务完成。"
        if st == "cancelled":
            return "腾讯 WorkBuddy 上一次任务已被取消。"
        if st == "completed":
            summary = str(last.get("summary") or "").strip() or "任务已完成，未返回额外文字输出。"
            return f"腾讯 WorkBuddy 已完成任务：{summary}"
        err = str(last.get("stderr") or last.get("summary") or "未知错误")[:220]
        return f"腾讯 WorkBuddy 上一次任务执行异常：{err}"

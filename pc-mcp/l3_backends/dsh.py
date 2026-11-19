"""DeepSeek Harness (DSH / @deepseek-ai/dsh) non-interactive headless L3 backend."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import (
    L3Backend,
    load_last_active_task,
    read_env_key,
    resolve_workdir,
    save_active_task,
    split_command,
    summarize_text_for_voice,
)

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def resolve_dsh_prefix(cfg: Dict[str, Any]) -> Tuple[List[str], str]:
    """Return (command_prefix, source_description). Uses `dsh` if installed, else `npx -y @deepseek-ai/dsh`."""
    custom = split_command(str(cfg.get("dsh_cmd") or ""))
    if custom:
        return custom, f"自定义命令 ({custom[0]})"

    for candidate in ("dsh", "dsh.cmd", "dsh.exe"):
        found = shutil.which(candidate)
        if found:
            return [found], f"本机 dsh CLI ({candidate})"

    for npx_cand in ("npx.cmd", "npx"):
        found = shutil.which(npx_cand)
        if found:
            return [found, "-y", "@deepseek-ai/dsh"], "npx @deepseek-ai/dsh（免全局安装）"

    return [], ""


class DshBackend(L3Backend):
    name = "dsh"
    label_zh = "DeepSeek DSH（DeepSeek Harness）"

    def detect(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        prefix, source = resolve_dsh_prefix(cfg)
        has_key = bool(read_env_key("DEEPSEEK_API_KEY"))
        if not prefix:
            return {
                "ready": False,
                "detail": "未检测到 dsh 或 npx，请安装 Node.js 或执行 npm i -g @deepseek-ai/dsh",
            }
        if not has_key:
            return {
                "ready": False,
                "detail": f"已找到 {source}，但未配置 DEEPSEEK_API_KEY 环境变量",
            }
        return {"ready": True, "detail": f"已就绪：{source}（国内直连）"}

    def _build_command(self, prefix: List[str], prompt: str) -> List[str]:
        return [*prefix, "--profile", "headless", prompt]

    def dispatch(
        self, task_id: str, instruction: str, follow_up_last: bool, cfg: Dict[str, Any]
    ) -> str:
        instr = (instruction or "").strip()
        if not instr:
            return "任务内容不能为空。"

        prefix, source = resolve_dsh_prefix(cfg)
        if not prefix:
            return (
                "当前未检测到 DeepSeek DSH 命令行工具（也未找到 npx）。"
                "请先安装 Node.js 或运行 npm i -g @deepseek-ai/dsh，或在 18080 控制台填写 DSH 命令路径。"
            )

        workdir = resolve_workdir(cfg)
        full_prompt = instr
        if follow_up_last:
            prev = load_last_active_task(backend="dsh")
            if prev:
                prev_prompt = str(prev.get("prompt") or "")[:300]
                prev_out = str(prev.get("summary") or prev.get("stdout") or "")[:500]
                full_prompt = (
                    f"[接续上一轮任务]\n上一轮任务：{prev_prompt}\n"
                    f"上一轮结果摘要：{prev_out}\n\n本次追加指令：{instr}"
                )

        task_record: Dict[str, Any] = {
            "id": task_id,
            "backend": "dsh",
            "title": f"[DSH派单] {instr[:24]}",
            "prompt": instr,
            "workdir": str(workdir),
            "command_source": source,
            "status": "running",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started_ts": time.time(),
        }
        save_active_task(task_id, task_record)

        cmd = self._build_command(prefix, full_prompt)
        timeout_sec = int(cfg.get("timeout_seconds", 300))

        def _worker():
            env = os.environ.copy()
            ds_key = read_env_key("DEEPSEEK_API_KEY")
            if ds_key:
                env["DEEPSEEK_API_KEY"] = ds_key
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(workdir),
                    env=env,
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

        threading.Thread(target=_worker, name=f"l3-dsh-{task_id}", daemon=True).start()
        return f"已成功派单给本机 DeepSeek DSH 后台执行（工作目录 {workdir.name}），不占用语音等待时间，稍后可随时问我进展！"

    def check_status(self, cfg: Dict[str, Any]) -> str:
        last = load_last_active_task(backend="dsh")
        if not last:
            return "目前还没有派发过 DeepSeek DSH 后台任务。"
        st = str(last.get("status") or "")
        if st == "running":
            elapsed = max(1, int(time.time() - float(last.get("started_ts") or time.time())))
            return f"DeepSeek DSH 正在后台处理「{str(last.get('prompt') or '')[:24]}」，已运行约 {elapsed} 秒，请稍后再问我。"
        if st == "completed":
            summary = str(last.get("summary") or "").strip() or "任务已完成，未返回额外文字输出。"
            return f"DeepSeek DSH 已完成任务：{summary}"
        err = str(last.get("stderr") or last.get("summary") or "未知错误")[:220]
        return f"DeepSeek DSH 上一次任务执行异常：{err}"

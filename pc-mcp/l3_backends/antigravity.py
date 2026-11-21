"""Google Antigravity official Sidecar + agentapi L3 backend."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict

from . import (
    ACTIVE_DIR,
    HEARTBEAT_FILE,
    L3Backend,
    PENDING_DIR,
    _find_active_workspace_task_locked,
    _is_pid_running,
    _workspace_guard_lock,
    atomic_write_json,
    find_busy_workspace_task,
    format_workspace_busy_message,
    guard_leading_dash_prompt,
    is_workspace_queue_mode,
    load_last_active_task,
    normalize_workspace_key,
    read_env_key,
    reap_stale_running_tasks,
    resolve_task_file,
    resolve_workdir,
    validate_task_id,
)

TRANSCRIPT_TAIL_BYTES = 64 * 1024  # H-6: Read at most the last 64 KB of transcript.jsonl
_CLAIMED_TMP_RE = re.compile(r"^\..+\.claimed\.(\d+)\.(\d+)\.tmp$")
_CLAIMED_TMP_MAX_AGE_SECONDS = 120.0


def _is_live_claim_file(pfp: Path, *, max_age_seconds: float = _CLAIMED_TMP_MAX_AGE_SECONDS) -> bool:
    """Return True if a sidecar `.claimed.<pid>.<ms>.tmp` file belongs to a live worker within TTL; else reap it."""
    m = _CLAIMED_TMP_RE.match(pfp.name)
    now = time.time()
    is_stale = True
    if m:
        try:
            claim_pid = int(m.group(1))
            claim_ts = int(m.group(2)) / 1000.0
            age = now - claim_ts
            if claim_pid > 0 and age <= max_age_seconds and _is_pid_running(claim_pid):
                is_stale = False
        except Exception:
            is_stale = True
    if is_stale:
        try:
            pfp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _guard_leading_dash_prompt(prompt: str, prefix_tag: str = "[指令]") -> str:
    """Prevent leading '-' prompt from being parsed as a CLI flag by agentapi without passing '--' (E-4, P1-3)."""
    return guard_leading_dash_prompt(prompt, prefix_tag=prefix_tag)


def _read_tail_lines(path: Path, max_bytes: int = TRANSCRIPT_TAIL_BYTES) -> tuple[list[str], bool]:
    """Read at most the last `max_bytes` bytes of a JSONL file, discarding any partial leading line (H-6)."""
    limit = max(1, int(max_bytes))
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        file_size = f.tell()
        truncated = file_size > limit
        if truncated:
            # Read 1 extra byte before the window to check whether the window starts right after '\n'
            f.seek(file_size - limit - 1, os.SEEK_SET)
            raw_plus_one = f.read(limit + 1)
            starts_on_line_boundary = raw_plus_one[:1] in (b"\n", b"\r")
            raw = raw_plus_one[1:]
        else:
            f.seek(0, os.SEEK_SET)
            raw = f.read(limit)
            starts_on_line_boundary = True
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and not starts_on_line_boundary and len(lines) > 1:
        lines = lines[1:]
    return lines, truncated


def _get_brain_dir() -> Path:
    custom = read_env_key("ANTIGRAVITY_BRAIN_DIR")
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".gemini" / "antigravity" / "brain"


def _summarize_transcript(conv_id: str, *, max_bytes: int = TRANSCRIPT_TAIL_BYTES) -> str:
    try:
        safe_conv = validate_task_id(conv_id)
    except ValueError:
        return "会话 ID 格式无效，拒绝读取会话记录。"
    brain_root = _get_brain_dir().resolve()
    t_path = (brain_root / safe_conv / ".system_generated" / "logs" / "transcript.jsonl").resolve()
    if not t_path.is_relative_to(brain_root):
        return "会话路径越界，拒绝读取会话记录。"
    if not t_path.exists():
        return f"会话 ({safe_conv[:8]}) 已创建，正在初始化或等待执行中。"

    lines, truncated = _read_tail_lines(t_path, max_bytes=max_bytes)
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
    step_desc = f"最近 {len(lines)} 步" if truncated else f"共 {len(lines)} 步"
    return f"反重力正在后台执行工具步骤中（当前步骤类型: {last_step_type}，{step_desc}），请稍后再查询。"


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
        safe_task_id = validate_task_id(task_id)
        instr = (instruction or "").strip()
        if not instr:
            return "任务内容不能为空。"

        workdir = resolve_workdir(cfg)
        ws_key = normalize_workspace_key(workdir)
        reap_stale_running_tasks()

        # P1-11: Serialize workspace busy check, pending-queue scan, and pending task file write
        # under _workspace_guard_lock to prevent concurrent dispatches from bypassing workspace exclusion.
        with _workspace_guard_lock:
            busy = _find_active_workspace_task_locked(ws_key)
            if busy is not None:
                return format_workspace_busy_message(workdir, busy)
            if not follow_up_last and not is_workspace_queue_mode(cfg) and PENDING_DIR.exists():
                live_claims = [
                    cfp
                    for cfp in sorted(PENDING_DIR.glob(".*.claimed.*.tmp"))
                    if not cfp.is_symlink() and _is_live_claim_file(cfp)
                ]
                pending_candidates = list(sorted(PENDING_DIR.glob("*.json"))) + live_claims
                for pfp in pending_candidates:
                    if pfp.is_symlink():
                        continue
                    try:
                        pobj = json.loads(pfp.read_text(encoding="utf-8"))
                        if not isinstance(pobj, dict):
                            continue
                        pwd = str(pobj.get("workdir") or "").strip()
                        if not pwd or normalize_workspace_key(pwd) == ws_key:
                            return format_workspace_busy_message(
                                workdir,
                                {
                                    "task_id": str(pobj.get("id") or pfp.stem),
                                    "prompt": str(pobj.get("prompt") or "")[:80],
                                },
                            )
                    except Exception:
                        continue

            conv_id = ""
            if follow_up_last:
                last = load_last_active_task(backend="antigravity")
                if last and last.get("conversation_id"):
                    cand_conv = str(last["conversation_id"]).strip()
                    try:
                        if not cand_conv.startswith("-"):
                            conv_id = validate_task_id(cand_conv)
                    except ValueError:
                        conv_id = ""

            safe_prompt = _guard_leading_dash_prompt(instr, prefix_tag="[追问]" if conv_id else "[指令]")
            task_obj = {
                "id": safe_task_id,
                "backend": "antigravity",
                "workdir": str(workdir),
                "title": f"[小智派单] {instr[:24]}",
                "prompt": safe_prompt,
                "conversation_id": conv_id,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            PENDING_DIR.mkdir(parents=True, exist_ok=True)
            pending_file = resolve_task_file(PENDING_DIR, safe_task_id)
            atomic_write_json(pending_file, task_obj)

        if not _is_worker_alive():
            return (
                "任务已为你存入派单队列！不过检测到反重力客户端里的【小智语音派单桥接】伴生服务还没开启，"
                "请在 Antigravity 的 Automations 面板点一下启用，启用后队列里的任务就会立刻自动跑起来。"
            )

        active_file = resolve_task_file(ACTIVE_DIR, safe_task_id)
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

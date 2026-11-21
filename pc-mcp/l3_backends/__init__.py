"""Pluggable L3 background Agent backend registry (Antigravity / DeepSeek DSH / Tencent WorkBuddy / None).

All paths are derived dynamically from the repository root so the project works
out of the box on any Windows machine without hardcoded drive letters or usernames.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

MCP_DIR = Path(__file__).resolve().parent.parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import common as _pc_common

SERVER_DIR = MCP_DIR.parent
DOTENV_FILE = SERVER_DIR / ".env"
STATE_DIR = MCP_DIR / "state"
TASKS_ROOT = STATE_DIR / "agy_tasks"
PENDING_DIR = TASKS_ROOT / "pending"
ACTIVE_DIR = TASKS_ROOT / "active"
ARCHIVE_DIR = TASKS_ROOT / "archive"
LOGS_DIR = TASKS_ROOT / "logs"
HEARTBEAT_FILE = TASKS_ROOT / "worker_heartbeat.json"
L3_CONFIG_FILE = STATE_DIR / "l3_backend_config.json"
MAX_ACTIVE_TASK_RECORDS = _pc_common.MAX_ACTIVE_TASK_RECORDS

for _d in (STATE_DIR, PENDING_DIR, ACTIVE_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

VALID_BACKENDS = ("antigravity", "dsh", "workbuddy", "none")
ALLOWED_L3_CONFIG_KEYS = frozenset({"backend", "timeout_seconds", "timeout"})
HOST_ENV_ONLY_L3_KEYS = frozenset({"workdir", "dsh_cmd", "workbuddy_cmd", "workbuddy_auto_approve"})
TASK_ID_RE = _pc_common.TASK_ID_RE
_WINDOWS_RESERVED_TASK_NAMES = _pc_common.WINDOWS_RESERVED_TASK_NAMES


def validate_task_id(task_id: Any) -> str:
    """Strictly validate an L3 task ID to prevent path traversal or reserved-device writes."""
    return _pc_common.validate_task_id(task_id, task_id_re=TASK_ID_RE)


def resolve_task_file(base_dir: Path, task_id: Any) -> Path:
    """Resolve `<base_dir>/<task_id>.json` with strict regex + `.resolve().is_relative_to()` containment."""
    return _pc_common.resolve_task_file(base_dir, task_id, task_id_re=TASK_ID_RE)

_cfg_lock = threading.Lock()
_dotenv_lock = threading.Lock()
_dotenv_cache_key: Optional[tuple[str, int, int]] = None
_dotenv_cache_map: Dict[str, str] = {}
_worker_threads_lock = threading.Lock()
_active_worker_threads: Dict[str, threading.Thread] = {}
_workspace_guard_lock = threading.Lock()
_workspace_active_tasks: Dict[str, Dict[str, Any]] = {}
_workspace_queues: Dict[str, List[Dict[str, Any]]] = {}
_cancelled_task_ids: set[str] = set()
_MAX_WORKSPACE_QUEUE_SIZE = 10


def start_worker_thread(
    task_id: str, name: str, target: Callable[[], None]
) -> threading.Thread:
    """Start a tracked daemon worker thread for an async L3 backend task."""
    thread_ref: List[threading.Thread] = []

    def _runner() -> None:
        try:
            target()
        finally:
            me = thread_ref[0] if thread_ref else threading.current_thread()
            with _worker_threads_lock:
                if _active_worker_threads.get(task_id) is me:
                    _active_worker_threads.pop(task_id, None)

    t = threading.Thread(target=_runner, name=name, daemon=True)
    thread_ref.append(t)
    with _worker_threads_lock:
        _active_worker_threads[task_id] = t
    try:
        t.start()
    except Exception:
        with _worker_threads_lock:
            if _active_worker_threads.get(task_id) is t:
                _active_worker_threads.pop(task_id, None)
        raise
    return t


def is_worker_thread_alive(task_id: str) -> bool:
    if not task_id:
        return False
    with _worker_threads_lock:
        t = _active_worker_threads.get(task_id)
    return bool(t is not None and t.is_alive())


def wait_for_worker_threads(
    task_ids: Optional[Iterable[str]] = None,
    timeout: float = 5.0,
) -> List[str]:
    """Join tracked L3 background worker threads and return names of any still alive."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    wanted = set(task_ids) if task_ids is not None else None
    with _worker_threads_lock:
        if wanted is None:
            threads = list(_active_worker_threads.values())
        else:
            threads = [t for tid, t in _active_worker_threads.items() if tid in wanted]
    cur = threading.current_thread()
    alive: List[str] = []
    for t in threads:
        if t is cur:
            continue
        remaining = max(0.0, deadline - time.monotonic())
        t.join(timeout=remaining)
        if t.is_alive():
            alive.append(t.name)
    return alive


def _strip_dotenv_value(raw_val: str) -> str:
    return _pc_common.strip_dotenv_value(raw_val)


def _parse_dotenv_text(text: str) -> Dict[str, str]:
    return _pc_common.parse_dotenv_text(text)


def _read_dotenv_map() -> Dict[str, str]:
    """Read project root .env with mtime+size cache (needed when WMI spawns 18900 without PowerShell env)."""
    global _dotenv_cache_key, _dotenv_cache_map
    with _dotenv_lock:
        if _dotenv_cache_key is None:
            _pc_common.invalidate_dotenv_cache(DOTENV_FILE)
    data = _pc_common.read_dotenv_map(DOTENV_FILE)
    with _dotenv_lock:
        try:
            if DOTENV_FILE.is_file():
                st = DOTENV_FILE.stat()
                _dotenv_cache_key = (str(DOTENV_FILE), st.st_mtime_ns, st.st_size)
                _dotenv_cache_map = dict(data)
            else:
                _dotenv_cache_key = None
                _dotenv_cache_map = {}
        except OSError:
            _dotenv_cache_key = None
            _dotenv_cache_map = {}
    return data


def read_env_key(name: str) -> str:
    """Read an environment variable from process env, Windows registry, or project .env fallback."""
    return _pc_common.read_env_key(name, dotenv_path=DOTENV_FILE)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Atomically write text via a same-directory temp file + os.replace."""
    _pc_common.atomic_write_text(path, text, encoding=encoding)


def atomic_write_json(path: Path, payload: Any, indent: int = 2) -> None:
    _pc_common.atomic_write_json(path, payload, indent=indent)


def _expand_batch_dp0(text: str, shim_dir: Path) -> str:
    return _pc_common.expand_batch_dp0(text, shim_dir)


def _find_node_exe(shim_dir: Path) -> Optional[str]:
    return _pc_common.find_node_exe(shim_dir)


def unwrap_windows_cmd_shim(cmd: List[str]) -> List[str]:
    """Unwrap Windows npm/batch .cmd/.bat shims into direct .exe invocations so
    CreateProcessW bypasses cmd.exe newline truncation and shell metacharacter parsing."""
    return _pc_common.unwrap_windows_cmd_shim(cmd)


def _sanitize_batch_text(text: str) -> str:
    return _pc_common.sanitize_batch_text(text)


def sanitize_arg_for_windows_batch(arg: str) -> str:
    """Fallback sanitizer when a command still ends in .cmd/.bat and must pass through cmd.exe."""
    return _pc_common.sanitize_arg_for_windows_batch(arg)


def prepare_subprocess_command(cmd: List[str]) -> List[str]:
    """Unwrap .cmd/.bat shims to direct .exe where possible; otherwise sanitize args against cmd.exe truncation."""
    return _pc_common.prepare_subprocess_command(cmd)


def guard_leading_dash_prompt(prompt: str, prefix_tag: str = "[指令]") -> str:
    """Prevent a leading '-' prompt from being parsed as a CLI option flag across all L3 backends (E-4, P1-3)."""
    return _pc_common.guard_leading_dash_prompt(prompt, prefix_tag=prefix_tag)


def _safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    return _pc_common.safe_int(val, default)


def _safe_float(val: Any, default: float = 0.0) -> float:
    return _pc_common.safe_float(val, default)


def get_process_create_time(pid: Optional[int]) -> Optional[float]:
    """Return psutil create_time() for a live PID, or None if unavailable/not running."""
    return _pc_common.get_process_create_time(pid)


def _is_pid_running(pid: Optional[int]) -> bool:
    return _pc_common.is_pid_running(pid)


def is_same_process(
    pid: Optional[int],
    expected_create_time: Optional[float],
    *,
    started_ts: Optional[float] = None,
    role: str = "child",
    max_drift: float = 2.0,
) -> bool:
    """Verify PID is alive AND matches expected create_time() so PID reuse never kills an unrelated process (P1-17)."""
    return _pc_common.is_same_process(
        pid,
        expected_create_time,
        started_ts=started_ts,
        role=role,
        max_drift=max_drift,
    )


def kill_process_tree(
    pid: int,
    grace_seconds: float = 2.0,
    *,
    expected_create_time: Optional[float] = None,
) -> bool:
    """Terminate ONLY the specified task subprocess and its descendant process tree."""
    return _pc_common.kill_process_tree(
        pid,
        grace_seconds=grace_seconds,
        expected_create_time=expected_create_time,
    )


def get_task_log_paths(task_id: str) -> tuple[Path, Path]:
    """Return safe (stdout_log_path, stderr_log_path) inside LOGS_DIR for a validated task_id (P1-11)."""
    safe_id = validate_task_id(task_id)
    base = ACTIVE_DIR.parent / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{safe_id}.stdout.log", base / f"{safe_id}.stderr.log"


def run_subprocess_with_tree_kill(
    cmd: List[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 300.0,
    creationflags: int = 0,
    on_spawn: Optional[Callable[[int], None]] = None,
    stdout_log_path: Optional[Path] = None,
    stderr_log_path: Optional[Path] = None,
    cleanup_log_files: bool = True,
) -> subprocess.CompletedProcess:
    """Run a subprocess with Windows .cmd shim unwrapping and guaranteed descendant tree kill on timeout."""
    return _pc_common.run_subprocess_with_tree_kill(
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout,
        creationflags=creationflags,
        on_spawn=on_spawn,
        prepare_fn=prepare_subprocess_command,
        kill_fn=kill_process_tree,
        stdout_log_path=stdout_log_path,
        stderr_log_path=stderr_log_path,
        cleanup_log_files=cleanup_log_files,
    )


def split_command(cmd_str: str) -> List[str]:
    """Split a custom Windows command string preserving quoted paths."""
    return _pc_common.split_command(cmd_str)


def _parse_env_bool(raw: str, default: bool = False) -> bool:
    val = (raw or "").strip().lower()
    if not val:
        return default
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


DEFAULT_L3_BACKEND = "dsh"


def _default_backend() -> str:
    env_b = (read_env_key("L3_BACKEND") or DEFAULT_L3_BACKEND).strip().lower()
    return env_b if env_b in VALID_BACKENDS else DEFAULT_L3_BACKEND


def _parse_timeout_seconds(value: Any, *, strict: bool = False) -> int:
    if isinstance(value, bool):
        if strict:
            raise ValueError("timeout_seconds 必须为整数（30-1800）")
        return 300
    try:
        if isinstance(value, float) and not value.is_integer():
            if strict:
                raise ValueError("timeout_seconds 必须为整数（30-1800）")
        t = int(value)
    except Exception as exc:
        if strict:
            raise ValueError("timeout_seconds 必须为整数（30-1800）") from exc
        return 300
    return max(30, min(1800, t))


def default_l3_config() -> Dict[str, Any]:
    return {
        "backend": _default_backend(),
        "workdir": read_env_key("XIAOZHI_L3_WORKDIR"),
        "dsh_cmd": read_env_key("DSH_CMD"),
        "workbuddy_cmd": read_env_key("WORKBUDDY_CMD"),
        "workbuddy_auto_approve": _parse_env_bool(
            read_env_key("WORKBUDDY_AUTO_APPROVE"), default=False
        ),
        "queue_mode": _parse_env_bool(
            read_env_key("L3_WORKSPACE_QUEUE_MODE"), default=False
        ),
        "timeout_seconds": 300,
    }


def _normalize_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize persisted L3 config; host-env-only fields NEVER come from JSON."""
    base = default_l3_config()
    merged = dict(base)
    if isinstance(raw, dict):
        raw_backend = raw.get("backend")
        if isinstance(raw_backend, str):
            b = raw_backend.strip().lower()
            if b in VALID_BACKENDS:
                merged["backend"] = b
        raw_timeout = raw.get("timeout_seconds")
        if raw_timeout is None and "timeout" in raw:
            raw_timeout = raw.get("timeout")
        if raw_timeout is not None:
            merged["timeout_seconds"] = _parse_timeout_seconds(raw_timeout, strict=False)
    # Explicitly guarantee host-env-only fields always come from host env / .env
    merged["workdir"] = str(base["workdir"] or "").strip()
    merged["dsh_cmd"] = str(base["dsh_cmd"] or "").strip()
    merged["workbuddy_cmd"] = str(base["workbuddy_cmd"] or "").strip()
    merged["workbuddy_auto_approve"] = bool(base["workbuddy_auto_approve"])
    merged["queue_mode"] = bool(base["queue_mode"])
    return merged


def read_l3_config() -> Dict[str, Any]:
    with _cfg_lock:
        if L3_CONFIG_FILE.is_file():
            try:
                raw = json.loads(L3_CONFIG_FILE.read_text(encoding="utf-8"))
                return _normalize_config(raw)
            except Exception:
                pass
        return default_l3_config()


def write_l3_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and persist ONLY `backend` and `timeout_seconds` (alias `timeout`) to host-private state."""
    if not isinstance(patch, dict):
        raise ValueError("配置参数必须为 JSON 对象")

    disallowed = sorted(str(k) for k in patch.keys() if k not in ALLOWED_L3_CONFIG_KEYS)
    if disallowed:
        raise ValueError(
            f"不允许通过 /l3/config 修改字段: {', '.join(disallowed)}（仅允许 backend 与 timeout_seconds；"
            "命令路径、工作目录与自动批准仅可由宿主机 .env 配置）"
        )

    parsed_backend: Optional[str] = None
    if "backend" in patch and patch["backend"] is not None:
        if not isinstance(patch["backend"], str):
            raise ValueError("backend 必须为字符串")
        b = patch["backend"].strip().lower()
        if b not in VALID_BACKENDS:
            raise ValueError(
                f"不支持的 L3 后端: {patch['backend']!r}（可选: {', '.join(VALID_BACKENDS)}）"
            )
        parsed_backend = b

    parsed_timeout: Optional[int] = None
    has_ts = "timeout_seconds" in patch and patch["timeout_seconds"] is not None
    has_t = "timeout" in patch and patch["timeout"] is not None
    if has_ts and has_t:
        t1 = _parse_timeout_seconds(patch["timeout_seconds"], strict=True)
        t2 = _parse_timeout_seconds(patch["timeout"], strict=True)
        if t1 != t2:
            raise ValueError("timeout 与 timeout_seconds 数值不一致")
        parsed_timeout = t1
    elif has_ts:
        parsed_timeout = _parse_timeout_seconds(patch["timeout_seconds"], strict=True)
    elif has_t:
        parsed_timeout = _parse_timeout_seconds(patch["timeout"], strict=True)

    with _cfg_lock:
        cur = default_l3_config()
        if L3_CONFIG_FILE.is_file():
            try:
                cur = _normalize_config(json.loads(L3_CONFIG_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
        if parsed_backend is not None:
            cur["backend"] = parsed_backend
        if parsed_timeout is not None:
            cur["timeout_seconds"] = parsed_timeout
        norm = _normalize_config(cur)
        persisted = {
            "backend": norm["backend"],
            "timeout_seconds": norm["timeout_seconds"],
        }
        atomic_write_json(L3_CONFIG_FILE, persisted)
        return norm


def migrate_legacy_l3_state(legacy_data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Explicit one-time safe migration from legacy `./data` into host-private `pc-mcp/state/`.

    SECURITY: Never trusts or migrates `dsh_cmd`, `workbuddy_cmd`, `workdir`, or
    `workbuddy_auto_approve` from `./data`, and never migrates untrusted `pending/` tasks.
    """
    legacy_dir = Path(legacy_data_dir) if legacy_data_dir is not None else (SERVER_DIR / "data")
    result: Dict[str, Any] = {"migrated_config": False, "migrated_active_tasks": 0}

    legacy_cfg = legacy_dir / "l3_backend_config.json"
    with _cfg_lock:
        if not L3_CONFIG_FILE.is_file() and legacy_cfg.is_file() and not legacy_cfg.is_symlink():
            try:
                raw = json.loads(legacy_cfg.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    norm = _normalize_config(raw)
                    atomic_write_json(
                        L3_CONFIG_FILE,
                        {
                            "backend": norm["backend"],
                            "timeout_seconds": norm["timeout_seconds"],
                        },
                    )
                    result["migrated_config"] = True
            except Exception:
                pass

    legacy_active = legacy_dir / "agy_tasks" / "active"
    if legacy_active.is_dir() and not legacy_active.is_symlink():
        ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
        legacy_root = legacy_active.resolve()
        for fp in sorted(legacy_active.glob("*.json")):
            if fp.is_symlink():
                continue
            try:
                if not fp.resolve().is_relative_to(legacy_root):
                    continue
                target = resolve_task_file(ACTIVE_DIR, fp.stem)
                if target.exists():
                    continue
                obj = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(obj, dict):
                    continue
                if "id" in obj:
                    validate_task_id(obj["id"])
                atomic_write_json(target, obj)
                result["migrated_active_tasks"] += 1
            except Exception:
                continue

    return result


def resolve_workdir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    c = cfg or read_l3_config()
    wd = str(c.get("workdir") or "").strip()
    if wd:
        p = Path(wd).expanduser()
        if p.exists() and p.is_dir():
            return p
    return SERVER_DIR.parent


def prune_active_tasks(
    active_dir: Optional[Path] = None,
    *,
    max_keep: int = MAX_ACTIVE_TASK_RECORDS,
    archive_dir: Optional[Path] = None,
) -> int:
    """Keep at most `max_keep` recent task records in `agy_tasks/active`, archiving older ones cleanly (H-6).

    Currently running or queued task records are never archived prematurely.
    """
    return _pc_common.prune_active_tasks(
        active_dir if active_dir is not None else ACTIVE_DIR,
        max_keep=max_keep,
        archive_dir=archive_dir,
    )


def save_active_task(task_id: str, payload: Dict[str, Any]) -> Path:
    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    out = resolve_task_file(ACTIVE_DIR, task_id)
    atomic_write_json(out, payload)
    prune_active_tasks()
    return out


def _task_recency_key(fp: Path, obj: Dict[str, Any], st_mtime_ns: int) -> tuple[float, int, str]:
    return _pc_common.task_recency_key(fp, obj, st_mtime_ns)


def _is_task_in_memory_queue(task_id: str) -> bool:
    if not task_id:
        return False
    with _workspace_guard_lock:
        for items in _workspace_queues.values():
            for item in items:
                if str(item.get("task_id") or "") == task_id:
                    return True
    return False


def _salvage_and_clean_task_logs(obj: Dict[str, Any]) -> None:
    """Salvage tail output from surviving child task log files after MCP restart and clean up temp logs (P1-11)."""
    for field, key in (("stdout_log", "stdout"), ("stderr_log", "stderr")):
        raw_p = str(obj.get(field) or "").strip()
        if not raw_p:
            continue
        p = Path(raw_p)
        try:
            if p.is_file():
                limit = 4000 if key == "stdout" else 2000
                tail = p.read_bytes()[-65536:].decode("utf-8", errors="replace").strip()
                if tail and not obj.get(key):
                    obj[key] = tail[-limit:]
                p.unlink(missing_ok=True)
        except OSError:
            pass


def reap_stale_running_tasks(now: Optional[float] = None) -> int:
    """Recover zombie `status == 'running'` and orphaned `status == 'queued'` task records (P1-17, P1-11).

    P1-17: Every file is isolated in its own try/except so one corrupt task JSON can NEVER crash 18900
    startup, and every PID kill verifies psutil create_time() to prevent killing a reused PID.
    P1-11: Also reaps in-memory `queued` records abandoned by a dead/restarted MCP parent so they
    cannot become permanent zombies that escape `prune_active_tasks()`.
    """
    try:
        if not ACTIVE_DIR.exists():
            return 0
        task_files = sorted(ACTIVE_DIR.glob("*.json"))
    except Exception:
        return 0

    cur_ts = _safe_float(time.time() if now is None else now, time.time())
    cur_pid = os.getpid()
    cur_pid_ct = get_process_create_time(cur_pid)
    reaped = 0

    for fp in task_files:
        try:
            if fp.is_symlink():
                continue
            orig_stat = fp.stat()
            obj = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(obj, dict):
                continue
            st = str(obj.get("status") or "").strip().lower()
            if st not in ("running", "queued"):
                continue

            task_id = str(obj.get("id") or fp.stem)
            started_ts = _safe_float(obj.get("started_ts"), 0.0)
            timeout_sec = max(30, _safe_int(obj.get("timeout_seconds"), 300) or 300)
            elapsed = (cur_ts - started_ts) if started_ts > 0 else (timeout_sec + 999)
            owner_pid = _safe_int(obj.get("owner_pid"), None)
            child_pid = _safe_int(obj.get("pid"), None)
            owner_ct = _safe_float(obj.get("owner_create_time"), 0.0) or None
            child_ct = _safe_float(obj.get("pid_create_time"), 0.0) or None

            worker_alive_here = is_worker_thread_alive(task_id)
            in_mem_queue_here = _is_task_in_memory_queue(task_id) if st == "queued" else False
            same_owner_here = (
                owner_pid is not None
                and owner_pid == cur_pid
                and (
                    worker_alive_here
                    or in_mem_queue_here
                    or (owner_ct is not None and (cur_pid_ct is None or abs(cur_pid_ct - owner_ct) <= 2.0))
                    or (
                        owner_ct is None
                        and (cur_pid_ct is None or started_ts <= 0.0 or cur_pid_ct <= started_ts + 5.0)
                    )
                )
            )
            worker_alive_here = same_owner_here and worker_alive_here

            owner_alive = False
            if owner_pid is not None:
                if same_owner_here:
                    owner_alive = True
                else:
                    owner_alive = is_same_process(
                        owner_pid, owner_ct, started_ts=started_ts, role="owner"
                    )

            reason = ""
            if st == "queued":
                if not owner_alive:
                    reason = "原宿主机服务已重启或退出，内存排队任务已失效并自动回收"
                elif same_owner_here and not in_mem_queue_here and elapsed > 5.0:
                    reason = "排队任务已不在当前内存队列中，僵尸排队状态已自动回收"
                elif owner_pid is None and elapsed > timeout_sec:
                    reason = "历史遗留排队任务已超时失联，已自动回收"
            else:
                child_verified_alive = (
                    is_same_process(child_pid, child_ct, started_ts=started_ts, role="child")
                    if child_pid is not None
                    else False
                )
                verified_child_ct = (
                    child_ct
                    if child_ct is not None
                    else (get_process_create_time(child_pid) if child_verified_alive else None)
                )

                if elapsed > timeout_sec + 30:
                    if child_pid and child_verified_alive:
                        kill_process_tree(child_pid, expected_create_time=verified_child_ct)
                    reason = f"后台任务超过 {timeout_sec} 秒仍未结束，已自动回收清理"
                elif owner_pid is not None and not same_owner_here and not owner_alive:
                    if child_pid and child_verified_alive:
                        # E-1: Running L3 child processes survive MCP restarts independently until finished or timed out.
                        continue
                    reason = "原宿主机服务已重启或退出，遗留运行中任务已自动回收"
                elif (
                    child_pid is not None
                    and elapsed > 5.0
                    and not worker_alive_here
                    and not child_verified_alive
                ):
                    reason = "后台任务子进程已提前退出，僵尸状态已自动回收"
                elif owner_pid is None and child_pid is None and elapsed > timeout_sec:
                    reason = "历史遗留运行中任务已超时失联，已自动回收"

            if reason:
                _salvage_and_clean_task_logs(obj)
                obj["status"] = "error"
                obj["stderr"] = reason
                obj["summary"] = obj.get("stdout") and summarize_text_for_voice(str(obj["stdout"]), 280) or reason
                obj["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                obj["reaped_zombie"] = True
                atomic_write_json(fp, obj)
                try:
                    os.utime(fp, ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns))
                except OSError:
                    pass
                reaped += 1
        except Exception:
            # Isolate corrupt/unreadable task files so one bad record never aborts startup or blocks other tasks
            continue
    prune_active_tasks()
    return reaped


def cleanup_running_task_processes() -> int:
    """Safely terminate ONLY verified active L3 task subprocess trees during shutdown/restart (P1-15 + P1-17).

    Never touches user browser windows opened via L1 `pc_open_app_or_web`.
    """
    try:
        if not ACTIVE_DIR.exists():
            return 0
        task_files = sorted(ACTIVE_DIR.glob("*.json"))
    except Exception:
        return 0

    cleaned = 0
    for fp in task_files:
        try:
            if fp.is_symlink():
                continue
            obj = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(obj, dict) or obj.get("status") != "running":
                continue
            started_ts = _safe_float(obj.get("started_ts"), 0.0)
            child_pid = _safe_int(obj.get("pid"), None)
            child_ct = _safe_float(obj.get("pid_create_time"), 0.0) or None
            if child_pid and is_same_process(child_pid, child_ct, started_ts=started_ts, role="child"):
                verified_ct = child_ct or get_process_create_time(child_pid)
                kill_process_tree(child_pid, grace_seconds=1.5, expected_create_time=verified_ct)
                cleaned += 1
            obj["status"] = "error"
            obj["stderr"] = "宿主机 MCP 服务关闭/重启，已清理 L3 子进程树"
            obj["summary"] = obj["stderr"]
            obj["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            obj["reaped_zombie"] = True
            atomic_write_json(fp, obj)
        except Exception:
            continue
    return cleaned


def load_last_active_task(backend: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not ACTIVE_DIR.exists():
        return None
    reap_stale_running_tasks()
    candidates: List[tuple[tuple[float, int, str], Dict[str, Any]]] = []
    for fp in ACTIVE_DIR.glob("*.json"):
        try:
            st = fp.stat()
            obj = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if backend is not None:
            item_backend = str(obj.get("backend") or "antigravity").strip().lower()
            if item_backend != backend:
                continue
        candidates.append((_task_recency_key(fp, obj, st.st_mtime_ns), obj))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def summarize_text_for_voice(text: str, limit: int = 280) -> str:
    clean = " ".join((text or "").replace("\r", "\n").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "……"


def normalize_workspace_key(workdir: Path | str) -> str:
    """Canonicalize a workspace directory path for per-workspace mutual exclusion (E-2)."""
    raw = str(workdir or "").strip()
    if not raw:
        raw = str(SERVER_DIR.parent)
    try:
        resolved = Path(raw).expanduser().resolve()
        return os.path.normcase(os.path.normpath(str(resolved)))
    except Exception:
        return os.path.normcase(os.path.normpath(raw))


def is_workspace_queue_mode(cfg: Optional[Dict[str, Any]] = None) -> bool:
    if isinstance(cfg, dict) and "queue_mode" in cfg:
        return bool(cfg["queue_mode"])
    return _parse_env_bool(read_env_key("L3_WORKSPACE_QUEUE_MODE"), default=False)


def is_task_cancelled(task_id: str) -> bool:
    if not task_id:
        return False
    with _workspace_guard_lock:
        return task_id in _cancelled_task_ids


def _mark_task_cancelled_id(task_id: str) -> None:
    if not task_id:
        return
    with _workspace_guard_lock:
        if len(_cancelled_task_ids) >= 256:
            _cancelled_task_ids.clear()
        _cancelled_task_ids.add(task_id)


def register_spawned_task_pid(
    task_id: str,
    task_record: Dict[str, Any],
    child_pid: int,
    child_ct: Optional[float] = None,
) -> bool:
    """Atomically record spawned child PID or immediately kill it if task was already cancelled (P1-11).

    Serialized under `_workspace_guard_lock` with `cancel_active_task()` so `_on_spawn` can NEVER
    overwrite a `cancelled` task back to `running` or leak an unkilled child process.
    Returns True if the child is allowed to keep running, False if killed due to cancellation.
    """
    verified_ct = child_ct if child_ct is not None else get_process_create_time(child_pid)
    cancelled = False
    with _workspace_guard_lock:
        if task_id in _cancelled_task_ids:
            cancelled = True
        else:
            task_record["pid"] = child_pid
            task_record["pid_create_time"] = verified_ct
            save_active_task(task_id, task_record)
    if cancelled:
        kill_process_tree(child_pid, grace_seconds=1.0, expected_create_time=verified_ct)
        return False
    return True


def _find_active_workspace_task_locked(ws_key: str) -> Optional[Dict[str, Any]]:
    """Locate an active running task for `ws_key` (must be called under `_workspace_guard_lock`)."""
    entry = _workspace_active_tasks.get(ws_key)
    if entry is not None:
        tid = str(entry.get("task_id") or "")
        if entry.get("starting") or is_worker_thread_alive(tid):
            return dict(entry)
        # Verify against disk record in current ACTIVE_DIR (protects test isolation & post-restart state)
        still_running = False
        try:
            fp = resolve_task_file(ACTIVE_DIR, tid)
            if fp.is_file():
                obj = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(obj, dict) and obj.get("status") == "running":
                    child_pid = _safe_int(obj.get("pid"), None)
                    child_ct = _safe_float(obj.get("pid_create_time"), 0.0) or None
                    started_ts = _safe_float(obj.get("started_ts"), 0.0)
                    if child_pid and is_same_process(child_pid, child_ct, started_ts=started_ts, role="child"):
                        still_running = True
        except Exception:
            still_running = False
        if still_running:
            return dict(entry)
        _workspace_active_tasks.pop(ws_key, None)

    # Disk fallback: detect L3 tasks that survived an MCP restart independently (E-1 + E-2)
    try:
        if ACTIVE_DIR.exists():
            for fp in sorted(ACTIVE_DIR.glob("*.json")):
                if fp.is_symlink():
                    continue
                try:
                    obj = json.loads(fp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("status") != "running":
                    continue
                rec_wd = str(obj.get("workdir") or "").strip()
                if not rec_wd or normalize_workspace_key(rec_wd) != ws_key:
                    continue
                tid = str(obj.get("id") or fp.stem)
                child_pid = _safe_int(obj.get("pid"), None)
                child_ct = _safe_float(obj.get("pid_create_time"), 0.0) or None
                started_ts = _safe_float(obj.get("started_ts"), 0.0)
                if is_worker_thread_alive(tid) or (
                    child_pid and is_same_process(child_pid, child_ct, started_ts=started_ts, role="child")
                ):
                    return {
                        "task_id": tid,
                        "backend": str(obj.get("backend") or ""),
                        "prompt": str(obj.get("prompt") or "")[:80],
                        "workdir": rec_wd,
                        "pid": child_pid,
                        "pid_create_time": child_ct,
                    }
    except Exception:
        pass
    return None


def find_busy_workspace_task(workdir: Path | str) -> Optional[Dict[str, Any]]:
    """Return active running task metadata if `workdir` currently has a running L3 task, else None (E-2)."""
    reap_stale_running_tasks()
    ws_key = normalize_workspace_key(workdir)
    with _workspace_guard_lock:
        return _find_active_workspace_task_locked(ws_key)


def format_workspace_busy_message(workdir: Path | str, busy_info: Optional[Dict[str, Any]] = None) -> str:
    ws_name = Path(workdir).name or str(workdir)
    tid = str((busy_info or {}).get("task_id") or "")
    prompt_snippet = str((busy_info or {}).get("prompt") or "")[:24]
    detail = f"（当前任务 {tid}：「{prompt_snippet}」）" if tid else ""
    return f"工作目录 {ws_name} 已有任务进行中，可追加指令或取消{detail}。"


def claim_workspace_or_enqueue(
    *,
    workdir: Path | str,
    task_id: str,
    backend: str,
    prompt: str,
    cfg: Optional[Dict[str, Any]] = None,
    start_fn: Optional[Callable[[], None]] = None,
    queued_record: Optional[Dict[str, Any]] = None,
    queue_if_busy: Optional[bool] = None,
) -> tuple[str, Optional[str]]:
    """Atomically claim single-active-task mutex for `workdir`, reject if busy, or enqueue when queue mode is on (E-2).

    Returns:
      - ("acquired", None): caller owns the workspace slot (and `start_fn` has been started if provided).
      - ("queued", user_message): task was queued behind the active task and will run automatically next.
      - ("busy", user_message): workspace is busy and queue mode is disabled (or queue full).
    """
    reap_stale_running_tasks()
    ws_key = normalize_workspace_key(workdir)
    use_queue = is_workspace_queue_mode(cfg) if queue_if_busy is None else bool(queue_if_busy)

    with _workspace_guard_lock:
        busy = _find_active_workspace_task_locked(ws_key)
        if busy is None:
            entry = {
                "task_id": task_id,
                "backend": backend,
                "prompt": str(prompt or "")[:80],
                "workdir": str(workdir),
                "claimed_ts": time.time(),
                "starting": True,
            }
            _workspace_active_tasks[ws_key] = entry
        else:
            if not use_queue or start_fn is None:
                return "busy", format_workspace_busy_message(workdir, busy)
            q = _workspace_queues.setdefault(ws_key, [])
            if len(q) >= _MAX_WORKSPACE_QUEUE_SIZE:
                return (
                    "busy",
                    f"工作目录 {Path(workdir).name} 排队队列已满（最多 {_MAX_WORKSPACE_QUEUE_SIZE} 个），已有任务进行中，可追加指令或取消。",
                )
            pos = len(q) + 1
            if queued_record is not None:
                rec = dict(queued_record)
                rec["status"] = "queued"
                rec["queue_position"] = pos
                save_active_task(task_id, rec)
            q.append(
                {
                    "task_id": task_id,
                    "backend": backend,
                    "prompt": str(prompt or "")[:80],
                    "workdir": str(workdir),
                    "start_fn": start_fn,
                }
            )
            return (
                "queued",
                f"工作目录 {Path(workdir).name} 已有任务进行中，已将任务加入排队队列（第 {pos} 位，任务 {task_id}），当前任务完成后将自动顺延执行。",
            )

    if start_fn is not None:
        try:
            start_fn()
        except Exception:
            with _workspace_guard_lock:
                if _workspace_active_tasks.get(ws_key, {}).get("task_id") == task_id:
                    _workspace_active_tasks.pop(ws_key, None)
            raise
    with _workspace_guard_lock:
        cur = _workspace_active_tasks.get(ws_key)
        if cur is not None and cur.get("task_id") == task_id:
            cur["starting"] = False
    return "acquired", None


def release_workspace_slot(workdir: Path | str, task_id: str) -> Optional[str]:
    """Release workspace mutex for `task_id` and automatically dispatch the next queued task if any (E-2)."""
    ws_key = normalize_workspace_key(workdir)
    while True:
        next_item: Optional[Dict[str, Any]] = None
        with _workspace_guard_lock:
            cur = _workspace_active_tasks.get(ws_key)
            if cur is not None and cur.get("task_id") == task_id:
                _workspace_active_tasks.pop(ws_key, None)
            if ws_key in _workspace_active_tasks:
                return None
            q = _workspace_queues.get(ws_key)
            if not q:
                _workspace_queues.pop(ws_key, None)
                return None
            next_item = q.pop(0)
            if not q:
                _workspace_queues.pop(ws_key, None)
            _workspace_active_tasks[ws_key] = {
                "task_id": next_item["task_id"],
                "backend": next_item["backend"],
                "prompt": next_item["prompt"],
                "workdir": next_item["workdir"],
                "claimed_ts": time.time(),
                "starting": True,
            }

        nxt_tid = str(next_item["task_id"])
        try:
            next_item["start_fn"]()
            with _workspace_guard_lock:
                cur_nxt = _workspace_active_tasks.get(ws_key)
                if cur_nxt is not None and cur_nxt.get("task_id") == nxt_tid:
                    cur_nxt["starting"] = False
            return nxt_tid
        except Exception as exc:
            try:
                fp = resolve_task_file(ACTIVE_DIR, nxt_tid)
                if fp.is_file():
                    rec = json.loads(fp.read_text(encoding="utf-8"))
                    if isinstance(rec, dict):
                        rec["status"] = "error"
                        rec["stderr"] = f"排队任务启动失败: {exc}"
                        rec["summary"] = rec["stderr"]
                        rec["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        save_active_task(nxt_tid, rec)
            except Exception:
                pass
            task_id = nxt_tid


def get_workspace_queue_snapshot(workdir: Optional[Path | str] = None) -> Dict[str, Any]:
    """Return active task and queued task IDs for a workspace (for diagnostics/tests)."""
    ws_key = normalize_workspace_key(workdir if workdir is not None else resolve_workdir())
    with _workspace_guard_lock:
        active = _workspace_active_tasks.get(ws_key)
        queued = [str(item.get("task_id") or "") for item in _workspace_queues.get(ws_key, [])]
    return {
        "workspace_key": ws_key,
        "active_task_id": str(active.get("task_id")) if active else None,
        "queued_task_ids": queued,
    }


def cancel_active_task(workdir: Optional[Path | str] = None) -> str:
    """Cancel active running task(s) and queued tasks in the specified (or configured) workspace (E-2)."""
    target_wd = Path(workdir) if workdir is not None else resolve_workdir()
    ws_key = normalize_workspace_key(target_wd)
    cancelled_ids: List[str] = []
    queued_cancelled: List[str] = []

    with _workspace_guard_lock:
        active_entry = _workspace_active_tasks.pop(ws_key, None)
        if active_entry and active_entry.get("task_id"):
            tid = str(active_entry["task_id"])
            cancelled_ids.append(tid)
            if len(_cancelled_task_ids) >= 256:
                _cancelled_task_ids.clear()
            _cancelled_task_ids.add(tid)
        queued_items = _workspace_queues.pop(ws_key, [])
        for qi in queued_items:
            qtid = str(qi.get("task_id") or "")
            if qtid:
                queued_cancelled.append(qtid)
                _cancelled_task_ids.add(qtid)

    # Also cancel any matching running/queued records on disk (including MCP-restart survivors)
    if ACTIVE_DIR.exists():
        for fp in sorted(ACTIVE_DIR.glob("*.json")):
            if fp.is_symlink():
                continue
            try:
                obj = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(obj, dict):
                    continue
                st = str(obj.get("status") or "").strip().lower()
                if st not in ("running", "queued"):
                    continue
                rec_wd = str(obj.get("workdir") or "").strip()
                tid = str(obj.get("id") or fp.stem)
                if tid not in cancelled_ids and tid not in queued_cancelled:
                    if not rec_wd or normalize_workspace_key(rec_wd) != ws_key:
                        continue
                _mark_task_cancelled_id(tid)
                child_pid = _safe_int(obj.get("pid"), None)
                child_ct = _safe_float(obj.get("pid_create_time"), 0.0) or None
                started_ts = _safe_float(obj.get("started_ts"), 0.0)
                if st == "running" and child_pid and is_same_process(
                    child_pid, child_ct, started_ts=started_ts, role="child"
                ):
                    verified_ct = child_ct or get_process_create_time(child_pid)
                    kill_process_tree(child_pid, grace_seconds=1.5, expected_create_time=verified_ct)
                obj["status"] = "cancelled"
                obj["stderr"] = "任务已被用户取消"
                obj["summary"] = "任务已被用户取消"
                obj["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                atomic_write_json(fp, obj)
                if st == "running" and tid not in cancelled_ids:
                    cancelled_ids.append(tid)
                elif st == "queued" and tid not in queued_cancelled:
                    queued_cancelled.append(tid)
            except Exception:
                continue

    if not cancelled_ids and not queued_cancelled:
        return f"工作目录 {target_wd.name} 当前没有进行中或排队中的 L3 任务。"
    parts = []
    if cancelled_ids:
        parts.append(f"已取消进行中任务 {len(cancelled_ids)} 个（{', '.join(cancelled_ids[:3])}）")
    if queued_cancelled:
        parts.append(f"已清空排队任务 {len(queued_cancelled)} 个")
    return f"工作目录 {target_wd.name}：{'，'.join(parts)}。"


class L3Backend:
    name: str = "base"
    label_zh: str = "通用后端"

    def detect(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def dispatch(
        self, task_id: str, instruction: str, follow_up_last: bool, cfg: Dict[str, Any]
    ) -> str:
        raise NotImplementedError

    def check_status(self, cfg: Dict[str, Any]) -> str:
        raise NotImplementedError


from .antigravity import AntigravityBackend  # noqa: E402
from .dsh import DshBackend  # noqa: E402
from .none_backend import NoneBackend  # noqa: E402
from .workbuddy import WorkBuddyBackend  # noqa: E402

BACKENDS: Dict[str, L3Backend] = {
    "antigravity": AntigravityBackend(),
    "dsh": DshBackend(),
    "workbuddy": WorkBuddyBackend(),
    "none": NoneBackend(),
}


def get_backend(name: Optional[str] = None) -> L3Backend:
    key = (name or read_l3_config()["backend"]).strip().lower()
    return BACKENDS.get(key, BACKENDS[DEFAULT_L3_BACKEND])


def get_status_overview() -> Dict[str, Any]:
    cfg = read_l3_config()
    detected: Dict[str, Dict[str, Any]] = {}
    for k, b in BACKENDS.items():
        try:
            detected[k] = b.detect(cfg)
        except Exception as exc:
            detected[k] = {"ready": False, "detail": f"探测异常: {exc}"}
    active_key = cfg["backend"]
    active_info = detected.get(active_key, {"ready": False, "detail": "未知后端"})
    return {
        "ok": True,
        "backend": active_key,
        "label_zh": BACKENDS[active_key].label_zh,
        "ready": bool(active_info.get("ready")),
        "detail": str(active_info.get("detail") or ""),
        "workdir": cfg["workdir"],
        "effective_workdir": str(resolve_workdir(cfg)),
        "dsh_cmd": cfg["dsh_cmd"],
        "workbuddy_cmd": cfg["workbuddy_cmd"],
        "workbuddy_auto_approve": cfg["workbuddy_auto_approve"],
        "timeout_seconds": cfg["timeout_seconds"],
        "writable_fields": ["backend", "timeout_seconds"],
        "host_env_only_fields": ["workdir", "dsh_cmd", "workbuddy_cmd", "workbuddy_auto_approve"],
        "detected": detected,
        "checked_at": time.strftime("%H:%M:%S"),
    }


def dispatch_task(
    instruction: str,
    follow_up_last: bool = False,
    *,
    queue_if_busy: Optional[bool] = None,
) -> str:
    cfg = read_l3_config()
    if queue_if_busy is not None:
        cfg = dict(cfg)
        cfg["queue_mode"] = bool(queue_if_busy)
    backend = get_backend(cfg["backend"])
    task_id = time.strftime("%Y%m%d_%H%M%S_") + os.urandom(3).hex()
    return backend.dispatch(task_id, instruction, follow_up_last, cfg)


def check_task_status() -> str:
    cfg = read_l3_config()
    backend = get_backend(cfg["backend"])
    return backend.check_status(cfg)

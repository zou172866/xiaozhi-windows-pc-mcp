import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import common as _pc_common

_CREATE_NO_WINDOW = _pc_common.CREATE_NO_WINDOW
SERVER_DIR = MCP_DIR.parent
STATE_DIR = MCP_DIR / "state"
TASKS_ROOT = STATE_DIR / "agy_tasks"
PENDING_DIR = TASKS_ROOT / "pending"
ACTIVE_DIR = TASKS_ROOT / "active"
HEARTBEAT_FILE = TASKS_ROOT / "worker_heartbeat.json"

for d in (STATE_DIR, PENDING_DIR, ACTIVE_DIR):
    d.mkdir(parents=True, exist_ok=True)

TASK_ID_RE = _pc_common.TASK_ID_RE
_WINDOWS_RESERVED_TASK_NAMES = _pc_common.WINDOWS_RESERVED_TASK_NAMES


def _expand_batch_dp0(text: str, shim_dir: Path) -> str:
    return _pc_common.expand_batch_dp0(text, shim_dir)


def _atomic_write_json(path: Path, payload: dict, indent: int | None = 2) -> None:
    _pc_common.atomic_write_json(path, payload, indent=indent)


def _unwrap_bat_prefix(prefix: list[str]) -> list[str]:
    return _pc_common.unwrap_windows_cmd_shim(prefix)


def _sanitize_batch_text(text: str) -> str:
    return _pc_common.sanitize_batch_text(text)


def _sanitize_arg_for_batch(arg: str) -> str:
    return _pc_common.sanitize_arg_for_windows_batch(arg)


def _prepare_cmd(cmd: list[str]) -> list[str]:
    return _pc_common.prepare_subprocess_command(cmd)


def _kill_process_tree(
    pid: int,
    grace_seconds: float = 2.0,
    *,
    expected_create_time: Optional[float] = None,
) -> bool:
    return _pc_common.kill_process_tree(
        pid,
        grace_seconds=grace_seconds,
        expected_create_time=expected_create_time,
    )


def validate_task_id(task_id: object) -> str:
    """Strictly validate sidecar task ID with fullmatch regex + reserved-name rejection."""
    return _pc_common.validate_task_id(task_id, task_id_re=TASK_ID_RE)


def _resolve_task_file(base_dir: Path, task_id: object) -> Path:
    """Resolve `<base_dir>/<task_id>.json` with regex + `.resolve().is_relative_to()` containment."""
    return _pc_common.resolve_task_file(base_dir, task_id, task_id_re=TASK_ID_RE)


def _read_dotenv_var(name: str) -> str:
    return _pc_common.read_dotenv_var(SERVER_DIR / ".env", name)


def _resolve_default_workdir() -> str:
    env_wd = os.environ.get("XIAOZHI_L3_WORKDIR", "").strip()
    if not env_wd and os.name == "nt":
        try:
            import winreg

            for root, subkey in (
                (winreg.HKEY_CURRENT_USER, r"Environment"),
                (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ):
                try:
                    with winreg.OpenKey(root, subkey) as k:
                        v, _ = winreg.QueryValueEx(k, "XIAOZHI_L3_WORKDIR")
                        if v:
                            env_wd = str(v).strip()
                            break
                except Exception:
                    pass
        except Exception:
            pass
    if not env_wd:
        env_wd = _read_dotenv_var("XIAOZHI_L3_WORKDIR")
    if env_wd:
        wd_path = Path(env_wd).expanduser()
        if wd_path.exists() and wd_path.is_dir():
            return str(wd_path)
    return str(SERVER_DIR.parent)

MY_PID = os.getpid()
PARENT_PID = os.getppid()


def _is_parent_alive() -> bool:
    if PARENT_PID <= 0:
        return True
    SYNCHRONIZE = 0x00100000
    h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, PARENT_PID)
    if not h:
        return False
    # WAIT_OBJECT_0 == 0 means process has exited; WAIT_TIMEOUT == 0x102 means still running
    res = ctypes.windll.kernel32.WaitForSingleObject(h, 0)
    ctypes.windll.kernel32.CloseHandle(h)
    return res == 0x00000102


def _build_agentapi_prefix() -> list[str]:
    env_exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE")
    if env_exe and os.path.exists(env_exe):
        if env_exe.lower().endswith("language_server.exe"):
            return [env_exe, "agentapi"]
        return [env_exe]
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        ls_default = Path(local_app) / "Programs" / "antigravity" / "resources" / "bin" / "language_server.exe"
        if ls_default.exists():
            return [str(ls_default), "agentapi"]
    in_path = shutil.which("agentapi.bat") or shutil.which("agentapi")
    if in_path:
        return [in_path]
    return [str(Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi.bat")]


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
MAX_ACTIVE_TASK_RECORDS = _pc_common.MAX_ACTIVE_TASK_RECORDS


def _guard_leading_dash_prompt(prompt: str, prefix_tag: str = "[指令]") -> str:
    """Prevent leading '-' prompt from being parsed as a CLI flag by agentapi without using '--' (E-4, P1-3).

    Empirically verified against `language_server.exe agentapi` (Go stdlib flag.FlagSet):
    - `new-conversation` parses leading '-' positional args as flags and errors out.
    - `send-message` stops flag parsing at `<recipient_id>` on Go flag.FlagSet, so passing '--'
      before `<content>` would corrupt `<content>` (and violates AGENTS.md §2).
    - Prefixing leading-dash prompts with a non-dash tag is universally safe without '--'.
    """
    return _pc_common.guard_leading_dash_prompt(prompt, prefix_tag=prefix_tag)


def _prune_active_tasks(
    active_dir: Path | None = None,
    *,
    max_keep: int = MAX_ACTIVE_TASK_RECORDS,
    archive_dir: Path | None = None,
) -> int:
    """Keep at most `max_keep` recent task records in `agy_tasks/active`, archiving older ones cleanly (H-6)."""
    return _pc_common.prune_active_tasks(
        active_dir if active_dir is not None else ACTIVE_DIR,
        max_keep=max_keep,
        archive_dir=archive_dir,
    )


def _write_heartbeat() -> float:
    ts = time.time()
    try:
        payload = {
            "ts": ts,
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pid": os.getpid(),
            "has_ls_env": bool(os.environ.get("ANTIGRAVITY_LS_ADDRESS")),
            "version": 3,
        }
        _atomic_write_json(HEARTBEAT_FILE, payload, indent=None)
    except Exception:
        pass
    return ts


def start_heartbeat_thread(
    stop_event: threading.Event,
    takeover_event: threading.Event,
    *,
    interval: float = 3.0,
) -> threading.Thread:
    """Run heartbeat updates on a dedicated daemon thread so long dispatches never stall heartbeats (E-4)."""
    initial_ts = _write_heartbeat()
    sleep_interval = max(0.05, float(interval))

    def _hb_loop() -> None:
        last_hb = initial_ts
        while not stop_event.wait(sleep_interval):
            try:
                if HEARTBEAT_FILE.exists():
                    cur = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
                    cur_pid = cur.get("pid")
                    cur_ts = float(cur.get("ts", 0))
                    if cur_pid not in (None, os.getpid()) and cur_ts > last_hb:
                        print(
                            f"[xiaozhi-agy-bridge] Newer worker {cur_pid} active, signaling exit for {os.getpid()}.",
                            flush=True,
                        )
                        takeover_event.set()
                        stop_event.set()
                        return
            except Exception:
                pass
            last_hb = _write_heartbeat()

    th = threading.Thread(target=_hb_loop, name="agy-sidecar-heartbeat", daemon=True)
    th.start()
    return th


def _process_task(task_file: Path) -> bool:
    pending_root = PENDING_DIR.resolve()
    try:
        if task_file.is_symlink():
            print(f"[xiaozhi-agy-bridge] Rejected symlink task file: {task_file.name}", file=sys.stderr, flush=True)
            return False
        resolved_tf = task_file.resolve()
        if not resolved_tf.is_relative_to(pending_root) or resolved_tf.parent != pending_root:
            print(f"[xiaozhi-agy-bridge] Rejected out-of-bounds task file: {task_file}", file=sys.stderr, flush=True)
            return False
    except Exception:
        return False

    # 4.1 #3: Atomically claim pending file via rename BEFORE launching 30s agentapi call
    # so worker handoff / dual-instance scan can never dispatch the same task twice.
    claim_path = pending_root / f".{resolved_tf.stem}.claimed.{MY_PID}.{int(time.time() * 1000)}.tmp"
    try:
        os.replace(resolved_tf, claim_path)
    except FileNotFoundError:
        return False
    except OSError:
        return False

    try:
        try:
            data = json.loads(claim_path.read_text(encoding="utf-8"))
        except Exception:
            return False

        if not isinstance(data, dict):
            print(f"[xiaozhi-agy-bridge] Rejected non-object task payload: {resolved_tf.name}", file=sys.stderr, flush=True)
            return False

        raw_task_id = data["id"] if "id" in data else resolved_tf.stem
        try:
            task_id = validate_task_id(raw_task_id)
            out_file = _resolve_task_file(ACTIVE_DIR, task_id)
        except ValueError as exc:
            print(f"[xiaozhi-agy-bridge] Rejected invalid task_id in {resolved_tf.name}: {exc}", file=sys.stderr, flush=True)
            return False

        data["id"] = task_id
        raw_prompt = str(data.get("prompt") or "").strip()
        title = str(data.get("title") or f"[小智派单] {raw_prompt[:22]}").strip()
        target_conv = str(data.get("conversation_id") or "").strip()
        if target_conv and not UUID_RE.fullmatch(target_conv):
            print(
                f"[xiaozhi-agy-bridge] Rejected invalid conversation_id in {resolved_tf.name}: {target_conv!r}",
                file=sys.stderr,
                flush=True,
            )
            data["status"] = "error"
            data["stderr"] = "invalid conversation_id format"
            _atomic_write_json(out_file, data, indent=2)
            _prune_active_tasks()
            return False

        prefix = _build_agentapi_prefix()
        if target_conv:
            safe_prompt = _guard_leading_dash_prompt(raw_prompt, prefix_tag="[追问]")
            cmd = prefix + ["send-message", f"--title={title}", target_conv, safe_prompt]
        else:
            default_wd = _resolve_default_workdir()
            enriched_prompt = _guard_leading_dash_prompt(f"[默认工作目录：{default_wd}]\n{raw_prompt}")
            cmd = prefix + ["new-conversation", f"--title={title}", enriched_prompt]

        prepared_cmd = _prepare_cmd(cmd)
        try:
            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = _CREATE_NO_WINDOW
            proc = subprocess.Popen(prepared_cmd, **popen_kwargs)
            proc_create_time = _pc_common.get_process_create_time(proc.pid)
            try:
                stdout_raw, stderr_raw = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc.pid, expected_create_time=proc_create_time)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                raise TimeoutError("agentapi 派单超过 30 秒已中止并清理子进程树")

            stdout = (stdout_raw or "").strip()
            stderr = (stderr_raw or "").strip()
            conv_id = target_conv
            if not conv_id:
                m = UUID_RE.search(stdout) or UUID_RE.search(stderr)
                if m:
                    conv_id = m.group(0)

            data["dispatched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            data["returncode"] = proc.returncode
            data["conversation_id"] = conv_id
            data["stdout"] = stdout[:800]
            data["stderr"] = stderr[:800]
            data["status"] = "dispatched" if proc.returncode == 0 else "error"
        except Exception as exc:
            data["status"] = "error"
            data["stderr"] = str(exc)

        _atomic_write_json(out_file, data, indent=2)
        _prune_active_tasks()
        return True
    finally:
        claim_path.unlink(missing_ok=True)


_CLAIMED_TMP_RE = re.compile(r"^\..+\.claimed\.(\d+)\.(\d+)\.tmp$")
_CLAIMED_TMP_MAX_AGE_SECONDS = 120.0


def _reap_orphaned_claim_files(
    pending_dir: Path | None = None,
    *,
    max_age_seconds: float = _CLAIMED_TMP_MAX_AGE_SECONDS,
) -> int:
    """Clean up orphaned `.claimed.<pid>.<ms>.tmp` files left behind by crashed sidecar workers."""
    pdir = pending_dir if pending_dir is not None else PENDING_DIR
    reaped = 0
    if not pdir.exists():
        return 0
    now = time.time()
    for cf in sorted(pdir.glob(".*.claimed.*.tmp")):
        if cf.is_symlink():
            continue
        m = _CLAIMED_TMP_RE.match(cf.name)
        stale = True
        if m:
            try:
                claim_pid = int(m.group(1))
                claim_ts = int(m.group(2)) / 1000.0
                if claim_pid > 0 and (now - claim_ts) <= max_age_seconds and _pc_common.is_pid_running(claim_pid):
                    stale = False
            except Exception:
                stale = True
        if stale:
            try:
                cf.unlink(missing_ok=True)
                reaped += 1
            except OSError:
                pass
    return reaped


def main():
    print(f"[xiaozhi-agy-bridge v3] Sidecar worker started (pid={MY_PID}, ppid={PARENT_PID}).", flush=True)
    _reap_orphaned_claim_files()
    stop_event = threading.Event()
    takeover_event = threading.Event()
    hb_thread = start_heartbeat_thread(stop_event, takeover_event, interval=3.0)
    try:
        while not stop_event.is_set():
            if takeover_event.is_set():
                sys.exit(0)
            if not _is_parent_alive():
                print(f"[xiaozhi-agy-bridge] Parent {PARENT_PID} exited, stopping orphan worker {MY_PID}.", flush=True)
                sys.exit(0)
            try:
                _reap_orphaned_claim_files()
                pending_files = sorted(PENDING_DIR.glob("*.json"))
            except Exception as e:
                print(f"[xiaozhi-agy-bridge] scan error: {e}", file=sys.stderr, flush=True)
                pending_files = []
            for tf in pending_files:
                if stop_event.is_set() or takeover_event.is_set():
                    break
                try:
                    _process_task(tf)
                except Exception as e:
                    print(f"[xiaozhi-agy-bridge] task error ({tf.name}): {e}", file=sys.stderr, flush=True)
            stop_event.wait(0.8)
    finally:
        stop_event.set()
        if hb_thread.is_alive():
            hb_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()

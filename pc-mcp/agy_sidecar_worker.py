import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SERVER_DIR / "data"
TASKS_ROOT = DATA_DIR / "agy_tasks"
PENDING_DIR = TASKS_ROOT / "pending"
ACTIVE_DIR = TASKS_ROOT / "active"
HEARTBEAT_FILE = TASKS_ROOT / "worker_heartbeat.json"
L3_CONFIG_FILE = DATA_DIR / "l3_backend_config.json"

for d in (PENDING_DIR, ACTIVE_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _resolve_default_workdir() -> str:
    try:
        if L3_CONFIG_FILE.is_file():
            wd = str(json.loads(L3_CONFIG_FILE.read_text(encoding="utf-8")).get("workdir") or "").strip()
            if wd and Path(wd).exists():
                return wd
    except Exception:
        pass
    env_wd = os.environ.get("XIAOZHI_L3_WORKDIR", "").strip()
    if env_wd and Path(env_wd).exists():
        return env_wd
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


def _write_heartbeat():
    try:
        payload = {
            "ts": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pid": os.getpid(),
            "has_ls_env": bool(os.environ.get("ANTIGRAVITY_LS_ADDRESS")),
            "version": 2,
        }
        HEARTBEAT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _process_task(task_file: Path):
    try:
        data = json.loads(task_file.read_text(encoding="utf-8"))
    except Exception:
        task_file.unlink(missing_ok=True)
        return

    task_id = data.get("id", task_file.stem)
    raw_prompt = str(data.get("prompt") or "").strip()
    title = str(data.get("title") or f"[小智派单] {raw_prompt[:22]}").strip()
    target_conv = str(data.get("conversation_id") or "").strip()

    prefix = _build_agentapi_prefix()
    if target_conv:
        cmd = prefix + ["send-message", f"--title={title}", target_conv, raw_prompt]
    else:
        default_wd = _resolve_default_workdir()
        enriched_prompt = f"[默认工作目录：{default_wd}]\n{raw_prompt}"
        cmd = prefix + ["new-conversation", f"--title={title}", enriched_prompt]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
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

    out_file = ACTIVE_DIR / f"{task_id}.json"
    out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    task_file.unlink(missing_ok=True)


def main():
    print(f"[xiaozhi-agy-bridge v3] Sidecar worker started (pid={MY_PID}, ppid={PARENT_PID}).", flush=True)
    _write_heartbeat()
    last_hb = time.time()
    while True:
        if not _is_parent_alive():
            print(f"[xiaozhi-agy-bridge] Parent {PARENT_PID} exited, stopping orphan worker {MY_PID}.", flush=True)
            sys.exit(0)
        now = time.time()
        if now - last_hb >= 3.0:
            try:
                if HEARTBEAT_FILE.exists():
                    cur = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
                    if cur.get("pid") not in (None, MY_PID) and float(cur.get("ts", 0)) > last_hb:
                        # A newer worker with fresh LS credentials has taken over
                        print(f"[xiaozhi-agy-bridge] Newer worker {cur.get('pid')} active, exiting {MY_PID}.", flush=True)
                        sys.exit(0)
            except Exception:
                pass
            _write_heartbeat()
            last_hb = now
        try:
            for tf in sorted(PENDING_DIR.glob("*.json")):
                _process_task(tf)
        except Exception as e:
            print(f"[xiaozhi-agy-bridge] loop error: {e}", file=sys.stderr, flush=True)
        time.sleep(0.8)


if __name__ == "__main__":
    main()

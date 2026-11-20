import ctypes
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_BAT_EXE_RE = re.compile(r'"([^"\r\n]+\.exe)"([^\r\n]*)%\*', flags=re.IGNORECASE)
_SAFE_CLI_FLAG_RE = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9._-]*$")
_FLAG_KEY_VAL_RE = re.compile(r"^(--?[A-Za-z0-9][A-Za-z0-9._-]*=)(.*)$", flags=re.DOTALL)


def _expand_batch_dp0(text: str, shim_dir: Path) -> str:
    prefix = str(shim_dir).rstrip("\\/") + "\\"
    out = re.sub(r"%~dp0[\\/]*", lambda _: prefix, text, flags=re.IGNORECASE)
    out = re.sub(r"%dp0%[\\/]*", lambda _: prefix, out, flags=re.IGNORECASE)
    return out


def _atomic_write_json(path: Path, payload: dict, indent: int | None = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=indent)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _unwrap_bat_prefix(prefix: list[str]) -> list[str]:
    if not prefix or os.name != "nt":
        return list(prefix)
    exe = prefix[0]
    resolved = exe if os.path.isfile(exe) else (shutil.which(exe) or exe)
    if not resolved.lower().endswith((".bat", ".cmd")) or not os.path.isfile(resolved):
        return list(prefix)
    shim_dir = Path(resolved).parent
    try:
        content = Path(resolved).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return list(prefix)
    for m in _BAT_EXE_RE.finditer(content):
        raw_exe = _expand_batch_dp0(m.group(1), shim_dir)
        if os.path.isfile(raw_exe):
            extra_raw = _expand_batch_dp0((m.group(2) or "").strip(), shim_dir)
            if "%" in extra_raw:
                continue
            extra = [p.strip('"').strip("'") for p in shlex.split(extra_raw, posix=False)] if extra_raw else []
            return [raw_exe, *extra, *prefix[1:]]
    return list(prefix)


def _sanitize_batch_text(text: str) -> str:
    s = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    for bad, rep in (
        ("%", "％"),
        ("!", "！"),
        ('"', "”"),
        ("&", "＆"),
        ("|", "｜"),
        ("<", "＜"),
        (">", "＞"),
        ("^", "＾"),
    ):
        s = s.replace(bad, rep)
    return s


def _sanitize_arg_for_batch(arg: str) -> str:
    s = str(arg)
    if not s:
        return s
    if _SAFE_CLI_FLAG_RE.fullmatch(s):
        return s
    if "\n" not in s and "\r" not in s and not any(c in s for c in '%!"&|<>^') and os.path.exists(s):
        return s
    m_kv = _FLAG_KEY_VAL_RE.match(s)
    if m_kv:
        return m_kv.group(1) + _sanitize_batch_text(m_kv.group(2))
    return _sanitize_batch_text(s)


def _prepare_cmd(cmd: list[str]) -> list[str]:
    unwrapped = _unwrap_bat_prefix(cmd)
    if not unwrapped or os.name != "nt":
        return unwrapped
    first = unwrapped[0]
    resolved = first if os.path.isfile(first) else (shutil.which(first) or first)
    if resolved.lower().endswith((".bat", ".cmd")):
        return [unwrapped[0], *(_sanitize_arg_for_batch(a) for a in unwrapped[1:])]
    return unwrapped


def _kill_process_tree(pid: int) -> None:
    if not pid or pid <= 0:
        return
    still_alive = True
    try:
        import psutil  # type: ignore

        try:
            root = psutil.Process(pid)
            targets = list(reversed(root.children(recursive=True))) + [root]
        except psutil.NoSuchProcess:
            return
        for p in targets:
            try:
                p.terminate()
            except Exception:
                pass
        _, alive = psutil.wait_procs(targets, timeout=2.0)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        if alive:
            _, alive = psutil.wait_procs(alive, timeout=1.0)
        still_alive = bool(alive) or psutil.pid_exists(pid)
    except Exception:
        still_alive = True
    if os.name == "nt" and still_alive:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception:
            pass

SERVER_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SERVER_DIR / "data"
TASKS_ROOT = DATA_DIR / "agy_tasks"
PENDING_DIR = TASKS_ROOT / "pending"
ACTIVE_DIR = TASKS_ROOT / "active"
HEARTBEAT_FILE = TASKS_ROOT / "worker_heartbeat.json"
L3_CONFIG_FILE = DATA_DIR / "l3_backend_config.json"

for d in (PENDING_DIR, ACTIVE_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _read_dotenv_var(name: str) -> str:
    env_file = SERVER_DIR / ".env"
    if not env_file.is_file():
        return ""
    found = ""
    try:
        for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.lstrip("\ufeff").strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != name:
                continue
            v = v.strip()
            if v and v[0] in ('"', "'"):
                end = v.find(v[0], 1)
                found = (v[1:end] if end != -1 else v[1:]).strip()
            else:
                for idx, ch in enumerate(v):
                    if ch == "#" and idx > 0 and v[idx - 1].isspace():
                        v = v[:idx]
                        break
                found = v.strip()
    except Exception:
        pass
    return found


def _resolve_default_workdir() -> str:
    try:
        if L3_CONFIG_FILE.is_file():
            wd = str(json.loads(L3_CONFIG_FILE.read_text(encoding="utf-8")).get("workdir") or "").strip()
            if wd and Path(wd).exists():
                return wd
    except Exception:
        pass
    env_wd = os.environ.get("XIAOZHI_L3_WORKDIR", "").strip() or _read_dotenv_var("XIAOZHI_L3_WORKDIR")
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
        _atomic_write_json(HEARTBEAT_FILE, payload, indent=None)
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
        try:
            stdout_raw, stderr_raw = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc.pid)
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

    out_file = ACTIVE_DIR / f"{task_id}.json"
    _atomic_write_json(out_file, data, indent=2)
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

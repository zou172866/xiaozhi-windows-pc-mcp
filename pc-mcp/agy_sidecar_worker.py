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

MCP_DIR = Path(__file__).resolve().parent
SERVER_DIR = MCP_DIR.parent
STATE_DIR = MCP_DIR / "state"
TASKS_ROOT = STATE_DIR / "agy_tasks"
PENDING_DIR = TASKS_ROOT / "pending"
ACTIVE_DIR = TASKS_ROOT / "active"
HEARTBEAT_FILE = TASKS_ROOT / "worker_heartbeat.json"

for d in (STATE_DIR, PENDING_DIR, ACTIVE_DIR):
    d.mkdir(parents=True, exist_ok=True)

TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_WINDOWS_RESERVED_TASK_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def validate_task_id(task_id: object) -> str:
    """Strictly validate sidecar task ID with fullmatch regex + reserved-name rejection."""
    if not isinstance(task_id, str):
        raise ValueError("invalid task_id: must be a string")
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"invalid task_id: {task_id!r}")
    if task_id.upper() in _WINDOWS_RESERVED_TASK_NAMES:
        raise ValueError(f"reserved task_id: {task_id!r}")
    return task_id


def _resolve_task_file(base_dir: Path, task_id: object) -> Path:
    """Resolve `<base_dir>/<task_id>.json` with regex + `.resolve().is_relative_to()` containment."""
    safe_id = validate_task_id(task_id)
    root = Path(base_dir).resolve()
    candidate = (root / f"{safe_id}.json").resolve()
    if not candidate.is_relative_to(root) or candidate.parent != root:
        raise ValueError(f"task path escapes base directory: {task_id!r}")
    return candidate


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
MAX_ACTIVE_TASK_RECORDS = 50


def _guard_leading_dash_prompt(prompt: str, prefix_tag: str = "[指令]") -> str:
    """Prevent leading '-' prompt from being parsed as a CLI flag by agentapi without using '--' (E-4).

    Empirically verified against `language_server.exe agentapi` (Go stdlib flag.FlagSet):
    - `new-conversation` parses leading '-' positional args as flags and errors out.
    - `send-message` stops flag parsing at `<recipient_id>` on Go flag.FlagSet, so passing '--'
      before `<content>` would corrupt `<content>` (and violates AGENTS.md §2).
    - Prefixing leading-dash prompts with a non-dash tag is universally safe without '--'.
    """
    s = str(prompt or "").strip()
    if s.startswith("-"):
        return f"{prefix_tag} {s}"
    return s


def _prune_active_tasks(
    active_dir: Path | None = None,
    *,
    max_keep: int = MAX_ACTIVE_TASK_RECORDS,
    archive_dir: Path | None = None,
) -> int:
    """Keep at most `max_keep` recent task records in `agy_tasks/active`, archiving older ones cleanly (H-6)."""
    active = Path(active_dir) if active_dir is not None else ACTIVE_DIR
    limit = max(1, int(max_keep))
    try:
        if not active.exists():
            return 0
        task_files = [fp for fp in active.glob("*.json") if not fp.is_symlink()]
    except Exception:
        return 0

    if len(task_files) <= limit:
        return 0

    protected: list[Path] = []
    archivable: list[tuple[tuple[float, int, str], Path]] = []
    for fp in task_files:
        try:
            st = fp.stat()
        except OSError:
            continue
        obj: dict = {}
        try:
            parsed = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                obj = parsed
        except Exception:
            obj = {}
        status = str(obj.get("status") or "").strip().lower()
        if status in ("running", "queued"):
            protected.append(fp)
        else:
            started = obj.get("started_ts")
            try:
                ts = float(started) if started is not None and float(started) > 0 else (st.st_mtime_ns / 1_000_000_000.0)
            except (TypeError, ValueError):
                ts = st.st_mtime_ns / 1_000_000_000.0
            archivable.append(((ts, st.st_mtime_ns, fp.name), fp))

    keep_other = max(0, limit - len(protected))
    if len(archivable) <= keep_other:
        return 0

    archivable.sort(key=lambda item: item[0], reverse=True)
    to_archive = [fp for _, fp in archivable[keep_other:]]
    if not to_archive:
        return 0

    arch_dir = Path(archive_dir) if archive_dir is not None else (active.parent / "archive")
    try:
        arch_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0

    archived = 0
    for fp in to_archive:
        dst = arch_dir / fp.name
        try:
            os.replace(fp, dst)
            archived += 1
        except OSError:
            try:
                shutil.move(str(fp), str(dst))
                archived += 1
            except Exception:
                continue
    return archived


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

    try:
        data = json.loads(resolved_tf.read_text(encoding="utf-8"))
    except Exception:
        resolved_tf.unlink(missing_ok=True)
        return False

    if not isinstance(data, dict):
        print(f"[xiaozhi-agy-bridge] Rejected non-object task payload: {resolved_tf.name}", file=sys.stderr, flush=True)
        resolved_tf.unlink(missing_ok=True)
        return False

    raw_task_id = data["id"] if "id" in data else resolved_tf.stem
    try:
        task_id = validate_task_id(raw_task_id)
        out_file = _resolve_task_file(ACTIVE_DIR, task_id)
    except ValueError as exc:
        print(f"[xiaozhi-agy-bridge] Rejected invalid task_id in {resolved_tf.name}: {exc}", file=sys.stderr, flush=True)
        resolved_tf.unlink(missing_ok=True)
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
        resolved_tf.unlink(missing_ok=True)
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

    _atomic_write_json(out_file, data, indent=2)
    _prune_active_tasks()
    resolved_tf.unlink(missing_ok=True)
    return True


def main():
    print(f"[xiaozhi-agy-bridge v3] Sidecar worker started (pid={MY_PID}, ppid={PARENT_PID}).", flush=True)
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

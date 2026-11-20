"""Pluggable L3 background Agent backend registry (Antigravity / DeepSeek DSH / Tencent WorkBuddy / None).

All paths are derived dynamically from the repository root so the project works
out of the box on any Windows machine without hardcoded drive letters or usernames.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import psutil

MCP_DIR = Path(__file__).resolve().parent.parent
SERVER_DIR = MCP_DIR.parent
DOTENV_FILE = SERVER_DIR / ".env"
STATE_DIR = MCP_DIR / "state"
TASKS_ROOT = STATE_DIR / "agy_tasks"
PENDING_DIR = TASKS_ROOT / "pending"
ACTIVE_DIR = TASKS_ROOT / "active"
HEARTBEAT_FILE = TASKS_ROOT / "worker_heartbeat.json"
L3_CONFIG_FILE = STATE_DIR / "l3_backend_config.json"

for _d in (STATE_DIR, PENDING_DIR, ACTIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

VALID_BACKENDS = ("antigravity", "dsh", "workbuddy", "none")
ALLOWED_L3_CONFIG_KEYS = frozenset({"backend", "timeout_seconds", "timeout"})
HOST_ENV_ONLY_L3_KEYS = frozenset({"workdir", "dsh_cmd", "workbuddy_cmd", "workbuddy_auto_approve"})
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


def validate_task_id(task_id: Any) -> str:
    """Strictly validate an L3 task ID to prevent path traversal or reserved-device writes."""
    if not isinstance(task_id, str):
        raise ValueError("invalid task_id: must be a string")
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"invalid task_id: {task_id!r}")
    if task_id.upper() in _WINDOWS_RESERVED_TASK_NAMES:
        raise ValueError(f"reserved task_id: {task_id!r}")
    return task_id


def resolve_task_file(base_dir: Path, task_id: Any) -> Path:
    """Resolve `<base_dir>/<task_id>.json` with strict regex + `.resolve().is_relative_to()` containment."""
    safe_id = validate_task_id(task_id)
    root = Path(base_dir).resolve()
    candidate = (root / f"{safe_id}.json").resolve()
    if not candidate.is_relative_to(root) or candidate.parent != root:
        raise ValueError(f"task path escapes base directory: {task_id!r}")
    return candidate

_cfg_lock = threading.Lock()
_dotenv_lock = threading.Lock()
_dotenv_cache_key: Optional[tuple[str, int, int]] = None
_dotenv_cache_map: Dict[str, str] = {}
_worker_threads_lock = threading.Lock()
_active_worker_threads: Dict[str, threading.Thread] = {}


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
    val = raw_val.strip()
    if not val:
        return ""
    if val[0] in ('"', "'"):
        quote = val[0]
        end = val.find(quote, 1)
        if end != -1:
            return val[1:end].strip()
        return val[1:].strip()
    # Strip unquoted inline comments preceded by whitespace (e.g. `L3_BACKEND=dsh # comment`)
    for idx, ch in enumerate(val):
        if ch == "#" and idx > 0 and val[idx - 1].isspace():
            val = val[:idx]
            break
    return val.strip()


def _parse_dotenv_text(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.lstrip("\ufeff").strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        eq = line.find("=")
        if eq <= 0:
            continue
        key = line[:eq].strip()
        val = _strip_dotenv_value(line[eq + 1 :])
        if key:
            out[key] = val
    return out


def _read_dotenv_map() -> Dict[str, str]:
    """Read project root .env with mtime+size cache (needed when WMI spawns 18900 without PowerShell env)."""
    global _dotenv_cache_key, _dotenv_cache_map
    env_path = DOTENV_FILE
    try:
        if not env_path.is_file():
            with _dotenv_lock:
                _dotenv_cache_key = None
                _dotenv_cache_map = {}
            return {}
        st = env_path.stat()
        cache_key = (str(env_path), st.st_mtime_ns, st.st_size)
        with _dotenv_lock:
            if _dotenv_cache_key != cache_key:
                _dotenv_cache_map = _parse_dotenv_text(
                    env_path.read_text(encoding="utf-8", errors="replace")
                )
                _dotenv_cache_key = cache_key
            return dict(_dotenv_cache_map)
    except Exception:
        return {}


def read_env_key(name: str) -> str:
    """Read an environment variable from process env, Windows registry, or project .env fallback."""
    val = os.environ.get(name, "")
    if not val and os.name == "nt":
        try:
            import winreg

            for root, subkey in (
                (winreg.HKEY_CURRENT_USER, r"Environment"),
                (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ):
                try:
                    with winreg.OpenKey(root, subkey) as k:
                        v, _ = winreg.QueryValueEx(k, name)
                        if v:
                            val = str(v)
                            break
                except Exception:
                    pass
        except Exception:
            pass
    if not val:
        val = _read_dotenv_map().get(name, "")
    return val.lstrip("\ufeff").strip()


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Atomically write text via a same-directory temp file + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{path.name}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
    tmp_path = path.with_name(tmp_name)
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(text)
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


def atomic_write_json(path: Path, payload: Any, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")


_CMD_NODE_SCRIPT_RE = re.compile(
    r'"(?:%~dp0|%dp0%)[\\/]+([^"\r\n]+\.(?:js|mjs|cjs))"',
    flags=re.IGNORECASE,
)
_CMD_NODE_ANY_BIN_RE = re.compile(
    r'"(?:%~dp0|%dp0%)[\\/]+(node_modules[\\/][^"\r\n]+)"\s+%\*',
    flags=re.IGNORECASE,
)
_CMD_EXE_TARGET_RE = re.compile(
    r'"([^"\r\n]+\.exe)"([^\r\n]*)%\*',
    flags=re.IGNORECASE,
)
_SAFE_CLI_FLAG_RE = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9._-]*$")
_FLAG_KEY_VAL_RE = re.compile(r"^(--?[A-Za-z0-9][A-Za-z0-9._-]*=)(.*)$", flags=re.DOTALL)


def _expand_batch_dp0(text: str, shim_dir: Path) -> str:
    prefix = str(shim_dir).rstrip("\\/") + "\\"
    out = re.sub(r"%~dp0[\\/]*", lambda _: prefix, text, flags=re.IGNORECASE)
    out = re.sub(r"%dp0%[\\/]*", lambda _: prefix, out, flags=re.IGNORECASE)
    return out


def _find_node_exe(shim_dir: Path) -> Optional[str]:
    cand = shim_dir / "node.exe"
    if cand.is_file():
        return str(cand)
    return shutil.which("node.exe") or shutil.which("node")


def unwrap_windows_cmd_shim(cmd: List[str]) -> List[str]:
    """Unwrap Windows npm/batch .cmd/.bat shims into direct .exe invocations so
    CreateProcessW bypasses cmd.exe newline truncation and shell metacharacter parsing."""
    if not cmd or os.name != "nt":
        return list(cmd)

    exe = cmd[0]
    resolved = exe if os.path.isfile(exe) else (shutil.which(exe) or exe)
    low = resolved.lower()
    if not low.endswith((".cmd", ".bat")) or not os.path.isfile(resolved):
        return list(cmd)

    shim_path = Path(resolved)
    shim_dir = shim_path.parent
    try:
        content = shim_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return list(cmd)

    # 1. Node/npm/npx .cmd shims (e.g. npx.cmd, corepack.cmd, dsh.cmd, workbuddy.cmd, codebuddy.cmd, cbc.cmd)
    node_exe = _find_node_exe(shim_dir)
    if node_exe:
        # Special case: official Node.js npx.cmd (checks node_modules\npm\bin\npx-cli.js)
        npx_js = shim_dir / "node_modules" / "npm" / "bin" / "npx-cli.js"
        if shim_path.stem.lower() == "npx" and npx_js.is_file():
            return [node_exe, str(npx_js), *cmd[1:]]

        # Standard npm global bin shim: pick the main entry script (skipping helper npm-prefix.js)
        candidates = [
            m.group(1)
            for m in _CMD_NODE_SCRIPT_RE.finditer(content)
            if not m.group(1).lower().endswith("npm-prefix.js")
        ]
        for m in _CMD_NODE_ANY_BIN_RE.finditer(content):
            rel = m.group(1)
            if rel not in candidates and not rel.lower().endswith("npm-prefix.js"):
                candidates.append(rel)
        for rel_js in reversed(candidates):
            js_path = (shim_dir / rel_js).resolve()
            if js_path.is_file():
                return [node_exe, str(js_path), *cmd[1:]]

    # 2. Generic .bat/.cmd delegating directly to a real .exe with %* (e.g. agentapi.bat)
    for m in _CMD_EXE_TARGET_RE.finditer(content):
        raw_exe = _expand_batch_dp0(m.group(1), shim_dir)
        exe_path = Path(raw_exe)
        if exe_path.is_file():
            extra_raw = _expand_batch_dp0((m.group(2) or "").strip(), shim_dir)
            # Abort unwrap if unexpanded batch variables remain (e.g. %VAR%)
            if "%" in extra_raw:
                continue
            extra_args = split_command(extra_raw) if extra_raw else []
            return [str(exe_path), *extra_args, *cmd[1:]]

    return list(cmd)


def _sanitize_batch_text(text: str) -> str:
    # Flatten CR/LF so cmd.exe never truncates arguments mid-prompt
    s = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    # Neutralize cmd.exe env-var (%VAR%), delayed-expansion (!VAR!), quote breakout ("), and command chaining (&|<>^)
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


def sanitize_arg_for_windows_batch(arg: str) -> str:
    """Fallback sanitizer when a command still ends in .cmd/.bat and must pass through cmd.exe."""
    s = str(arg)
    if not s:
        return s
    # Keep pure CLI flags (e.g. `-y`, `--profile`) and safe existing filesystem paths untouched
    if _SAFE_CLI_FLAG_RE.fullmatch(s):
        return s
    if "\n" not in s and "\r" not in s and not any(c in s for c in '%!"&|<>^') and os.path.exists(s):
        return s
    # For `--key=value` options (e.g. `--title=...`), keep the flag prefix and sanitize the value
    m_kv = _FLAG_KEY_VAL_RE.match(s)
    if m_kv:
        return m_kv.group(1) + _sanitize_batch_text(m_kv.group(2))
    return _sanitize_batch_text(s)


def prepare_subprocess_command(cmd: List[str]) -> List[str]:
    """Unwrap .cmd/.bat shims to direct .exe where possible; otherwise sanitize args against cmd.exe truncation."""
    unwrapped = unwrap_windows_cmd_shim(cmd)
    if not unwrapped or os.name != "nt":
        return unwrapped
    first = unwrapped[0]
    resolved_first = first if os.path.isfile(first) else (shutil.which(first) or first)
    if resolved_first.lower().endswith((".cmd", ".bat")):
        return [unwrapped[0], *(sanitize_arg_for_windows_batch(a) for a in unwrapped[1:])]
    return unwrapped


def _safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return default
    try:
        return int(val)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        f = float(val)
        if f != f or f in (float("inf"), float("-inf")):
            return default
        return f
    except (TypeError, ValueError, OverflowError):
        return default


def get_process_create_time(pid: Optional[int]) -> Optional[float]:
    """Return psutil create_time() for a live PID, or None if unavailable/not running."""
    pid_i = _safe_int(pid, None)
    if not pid_i or pid_i <= 0:
        return None
    try:
        proc = psutil.Process(pid_i)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return float(proc.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, OSError):
        return None


def _is_pid_running(pid: Optional[int]) -> bool:
    pid_i = _safe_int(pid, None)
    if not pid_i or pid_i <= 0:
        return False
    try:
        proc = psutil.Process(pid_i)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.AccessDenied:
        return True
    except (psutil.NoSuchProcess, ValueError, OSError):
        return False


def is_same_process(
    pid: Optional[int],
    expected_create_time: Optional[float],
    *,
    started_ts: Optional[float] = None,
    role: str = "child",
    max_drift: float = 2.0,
) -> bool:
    """Verify PID is alive AND matches expected create_time() so PID reuse never kills an unrelated process (P1-17)."""
    pid_i = _safe_int(pid, None)
    if not pid_i or pid_i <= 0:
        return False
    actual_ct = get_process_create_time(pid_i)
    if actual_ct is None:
        return False
    exp_ct = _safe_float(expected_create_time, 0.0)
    if exp_ct > 0.0:
        return abs(actual_ct - exp_ct) <= max(0.5, float(max_drift))
    # Legacy record without explicit create_time: bound by started_ts to protect against PID reuse
    st_ts = _safe_float(started_ts, 0.0)
    if st_ts <= 0.0:
        return False
    if role == "owner":
        # Owner process must have existed BEFORE the task was started
        return actual_ct <= st_ts + 5.0
    # Child process must have been spawned close to task start time
    return (st_ts - 5.0) <= actual_ct <= (st_ts + 30.0)


def kill_process_tree(
    pid: int,
    grace_seconds: float = 2.0,
    *,
    expected_create_time: Optional[float] = None,
) -> bool:
    """Terminate ONLY the specified task subprocess and its descendant process tree.

    When `expected_create_time` is provided, verifies `psutil.Process.create_time()` before
    terminating and before the Windows `taskkill /T` fallback to prevent killing a reused PID.
    Returns True if a matching process was targeted, False if skipped or already gone.
    """
    pid_i = _safe_int(pid, None)
    if not pid_i or pid_i <= 0:
        return False
    exp_ct = _safe_float(expected_create_time, 0.0)
    try:
        root = psutil.Process(pid_i)
        if exp_ct > 0.0:
            actual_ct = float(root.create_time())
            if abs(actual_ct - exp_ct) > 2.0:
                return False
        children = root.children(recursive=True)
    except psutil.NoSuchProcess:
        return False
    except Exception:
        if exp_ct > 0.0:
            # Fail closed when identity cannot be verified against expected_create_time
            return False
        children = []
        root = None

    targets = list(reversed(children))
    if root is not None:
        targets.append(root)

    for p in targets:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if targets:
        try:
            _, alive = psutil.wait_procs(targets, timeout=max(0.1, grace_seconds))
        except Exception:
            alive = targets
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            try:
                psutil.wait_procs(alive, timeout=1.0)
            except Exception:
                pass

    # Targeted Windows fallback ONLY for this specific verified task PID if still alive
    if os.name == "nt" and _is_pid_running(pid_i):
        if exp_ct > 0.0:
            cur_ct = get_process_create_time(pid_i)
            if cur_ct is None or abs(cur_ct - exp_ct) > 2.0:
                return True
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid_i)],
                capture_output=True,
                timeout=5,
                creationflags=0x08000000,
            )
        except Exception:
            pass
    return True


def run_subprocess_with_tree_kill(
    cmd: List[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 300.0,
    creationflags: int = 0,
    on_spawn: Optional[Callable[[int], None]] = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess with Windows .cmd shim unwrapping and guaranteed descendant tree kill on timeout."""
    prepared = prepare_subprocess_command(cmd)
    kwargs: Dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt" and creationflags:
        kwargs["creationflags"] = creationflags

    proc = subprocess.Popen(prepared, **kwargs)
    proc_create_time = get_process_create_time(proc.pid)
    if on_spawn is not None:
        try:
            on_spawn(proc.pid)
        except Exception:
            pass
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(prepared, proc.returncode, stdout=stdout, stderr=stderr)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid, expected_create_time=proc_create_time)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(prepared, timeout, output=stdout, stderr=stderr)


def split_command(cmd_str: str) -> List[str]:
    """Split a custom Windows command string preserving quoted paths."""
    raw = (cmd_str or "").strip()
    if not raw:
        return []
    try:
        parts = shlex.split(raw, posix=False)
    except Exception:
        parts = raw.split()
    return [p.strip('"').strip("'") for p in parts if p.strip()]


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


def save_active_task(task_id: str, payload: Dict[str, Any]) -> Path:
    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    out = resolve_task_file(ACTIVE_DIR, task_id)
    atomic_write_json(out, payload)
    return out


def _task_recency_key(fp: Path, obj: Dict[str, Any], st_mtime_ns: int) -> tuple[float, int, str]:
    started_ts = obj.get("started_ts")
    if started_ts is not None:
        try:
            ts = float(started_ts)
            if ts > 0:
                return (ts, st_mtime_ns, fp.name)
        except (TypeError, ValueError):
            pass
    return (st_mtime_ns / 1_000_000_000.0, st_mtime_ns, fp.name)


def reap_stale_running_tasks(now: Optional[float] = None) -> int:
    """Recover zombie `status == 'running'` task records whose worker/subprocess died or exceeded timeout.

    P1-17: Every file is isolated in its own try/except so one corrupt task JSON can NEVER crash 18900
    startup, and every PID kill verifies psutil create_time() to prevent killing a reused PID.
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
            if not isinstance(obj, dict) or obj.get("status") != "running":
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
            same_owner_here = (
                owner_pid is not None
                and owner_pid == cur_pid
                and (
                    worker_alive_here
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

            reason = ""
            if elapsed > timeout_sec + 30:
                if child_pid and child_verified_alive:
                    kill_process_tree(child_pid, expected_create_time=verified_child_ct)
                reason = f"后台任务超过 {timeout_sec} 秒仍未结束，已自动回收清理"
            elif owner_pid is not None and not same_owner_here and not owner_alive:
                if child_pid and child_verified_alive:
                    kill_process_tree(child_pid, expected_create_time=verified_child_ct)
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
                obj["status"] = "error"
                obj["stderr"] = reason
                obj["summary"] = reason
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


def dispatch_task(instruction: str, follow_up_last: bool = False) -> str:
    cfg = read_l3_config()
    backend = get_backend(cfg["backend"])
    task_id = time.strftime("%Y%m%d_%H%M%S_") + os.urandom(3).hex()
    return backend.dispatch(task_id, instruction, follow_up_last, cfg)


def check_task_status() -> str:
    cfg = read_l3_config()
    backend = get_backend(cfg["backend"])
    return backend.check_status(cfg)

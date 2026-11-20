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

SERVER_DIR = Path(__file__).resolve().parent.parent.parent
DOTENV_FILE = SERVER_DIR / ".env"
DATA_DIR = SERVER_DIR / "data"
TASKS_ROOT = DATA_DIR / "agy_tasks"
PENDING_DIR = TASKS_ROOT / "pending"
ACTIVE_DIR = TASKS_ROOT / "active"
HEARTBEAT_FILE = TASKS_ROOT / "worker_heartbeat.json"
L3_CONFIG_FILE = DATA_DIR / "l3_backend_config.json"

for _d in (PENDING_DIR, ACTIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

VALID_BACKENDS = ("antigravity", "dsh", "workbuddy", "none")

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


def _is_pid_running(pid: Optional[int]) -> bool:
    if not pid or int(pid) <= 0:
        return False
    try:
        proc = psutil.Process(int(pid))
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.AccessDenied:
        return True
    except (psutil.NoSuchProcess, ValueError, OSError):
        return False


def kill_process_tree(pid: int, grace_seconds: float = 2.0) -> None:
    """Terminate ONLY the specified task subprocess and its descendant process tree."""
    if not pid or int(pid) <= 0:
        return
    try:
        root = psutil.Process(int(pid))
        children = root.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    except Exception:
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

    # Targeted Windows fallback ONLY for this specific task PID if still alive
    if os.name == "nt" and _is_pid_running(pid):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(int(pid))],
                capture_output=True,
                timeout=5,
                creationflags=0x08000000,
            )
        except Exception:
            pass


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
    if on_spawn is not None:
        try:
            on_spawn(proc.pid)
        except Exception:
            pass
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(prepared, proc.returncode, stdout=stdout, stderr=stderr)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
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


def _default_backend() -> str:
    env_b = (read_env_key("L3_BACKEND") or "antigravity").strip().lower()
    return env_b if env_b in VALID_BACKENDS else "antigravity"


def default_l3_config() -> Dict[str, Any]:
    return {
        "backend": _default_backend(),
        "workdir": read_env_key("XIAOZHI_L3_WORKDIR"),
        "dsh_cmd": read_env_key("DSH_CMD"),
        "workbuddy_cmd": read_env_key("WORKBUDDY_CMD"),
        "workbuddy_auto_approve": True,
        "timeout_seconds": 300,
    }


def _normalize_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    base = default_l3_config()
    merged = dict(base)
    if isinstance(raw, dict):
        merged.update(raw)
    b = str(merged.get("backend") or base["backend"]).strip().lower()
    if b not in VALID_BACKENDS:
        b = base["backend"]
    merged["backend"] = b
    merged["workdir"] = str(merged.get("workdir") or "").strip()
    merged["dsh_cmd"] = str(merged.get("dsh_cmd") or "").strip()
    merged["workbuddy_cmd"] = str(merged.get("workbuddy_cmd") or "").strip()
    merged["workbuddy_auto_approve"] = bool(merged.get("workbuddy_auto_approve", True))
    try:
        t = int(merged.get("timeout_seconds", 300))
    except Exception:
        t = 300
    merged["timeout_seconds"] = max(30, min(1800, t))
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
    with _cfg_lock:
        cur = default_l3_config()
        if L3_CONFIG_FILE.is_file():
            try:
                cur = _normalize_config(json.loads(L3_CONFIG_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
        for k in ("backend", "workdir", "dsh_cmd", "workbuddy_cmd", "workbuddy_auto_approve", "timeout_seconds"):
            if k in patch and patch[k] is not None:
                cur[k] = patch[k]
        norm = _normalize_config(cur)
        atomic_write_json(L3_CONFIG_FILE, norm)
        return norm


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
    out = ACTIVE_DIR / f"{task_id}.json"
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
    """Recover zombie `status == 'running'` task records whose worker/subprocess died or exceeded timeout."""
    if not ACTIVE_DIR.exists():
        return 0
    cur_ts = float(time.time() if now is None else now)
    cur_pid = os.getpid()
    reaped = 0
    for fp in sorted(ACTIVE_DIR.glob("*.json")):
        try:
            orig_stat = fp.stat()
            obj = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("status") != "running":
            continue

        task_id = str(obj.get("id") or fp.stem)
        started_ts = float(obj.get("started_ts") or 0.0)
        timeout_sec = max(30, int(obj.get("timeout_seconds") or 300))
        elapsed = (cur_ts - started_ts) if started_ts > 0 else (timeout_sec + 999)
        owner_pid = obj.get("owner_pid")
        child_pid = obj.get("pid")
        worker_alive_here = (
            owner_pid is not None
            and int(owner_pid) == cur_pid
            and is_worker_thread_alive(task_id)
        )

        reason = ""
        if elapsed > timeout_sec + 30:
            if child_pid and _is_pid_running(int(child_pid)):
                kill_process_tree(int(child_pid))
            reason = f"后台任务超过 {timeout_sec} 秒仍未结束，已自动回收清理"
        elif owner_pid is not None and int(owner_pid) != cur_pid and not _is_pid_running(int(owner_pid)):
            if child_pid and _is_pid_running(int(child_pid)):
                kill_process_tree(int(child_pid))
            reason = "原宿主机服务已重启或退出，遗留运行中任务已自动回收"
        elif (
            child_pid is not None
            and elapsed > 5.0
            and not worker_alive_here
            and not _is_pid_running(int(child_pid))
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
            try:
                atomic_write_json(fp, obj)
                try:
                    os.utime(fp, ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns))
                except OSError:
                    pass
                reaped += 1
            except Exception:
                pass
    return reaped


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
    return BACKENDS.get(key, BACKENDS["antigravity"])


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

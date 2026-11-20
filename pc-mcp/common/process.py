"""Shared Windows shim unwrapping, batch arg sanitization, and create_time-verified process tree kill (H-5)."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - optional fallback outside .venv
    psutil = None  # type: ignore

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

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


def expand_batch_dp0(text: str, shim_dir: Path) -> str:
    prefix = str(shim_dir).rstrip("\\/") + "\\"
    out = re.sub(r"%~dp0[\\/]*", lambda _: prefix, text, flags=re.IGNORECASE)
    out = re.sub(r"%dp0%[\\/]*", lambda _: prefix, out, flags=re.IGNORECASE)
    return out


def find_node_exe(shim_dir: Path) -> Optional[str]:
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
    node_exe = find_node_exe(shim_dir)
    if node_exe:
        npx_js = shim_dir / "node_modules" / "npm" / "bin" / "npx-cli.js"
        if shim_path.stem.lower() == "npx" and npx_js.is_file():
            return [node_exe, str(npx_js), *cmd[1:]]

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
        raw_exe = expand_batch_dp0(m.group(1), shim_dir)
        exe_path = Path(raw_exe)
        if exe_path.is_file():
            extra_raw = expand_batch_dp0((m.group(2) or "").strip(), shim_dir)
            if "%" in extra_raw:
                continue
            extra_args = split_command(extra_raw) if extra_raw else []
            return [str(exe_path), *extra_args, *cmd[1:]]

    return list(cmd)


def sanitize_batch_text(text: str) -> str:
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
    if _SAFE_CLI_FLAG_RE.fullmatch(s):
        return s
    if "\n" not in s and "\r" not in s and not any(c in s for c in '%!"&|<>^') and os.path.exists(s):
        return s
    m_kv = _FLAG_KEY_VAL_RE.match(s)
    if m_kv:
        return m_kv.group(1) + sanitize_batch_text(m_kv.group(2))
    return sanitize_batch_text(s)


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


def safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return default
    try:
        return int(val)
    except (TypeError, ValueError, OverflowError):
        return default


def safe_float(val: Any, default: float = 0.0) -> float:
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
    pid_i = safe_int(pid, None)
    if not pid_i or pid_i <= 0 or psutil is None:
        return None
    try:
        proc = psutil.Process(pid_i)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return float(proc.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, OSError):
        return None


def is_pid_running(pid: Optional[int]) -> bool:
    pid_i = safe_int(pid, None)
    if not pid_i or pid_i <= 0:
        return False
    if psutil is None:
        return True
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
    pid_i = safe_int(pid, None)
    if not pid_i or pid_i <= 0:
        return False
    actual_ct = get_process_create_time(pid_i)
    if actual_ct is None:
        return False
    exp_ct = safe_float(expected_create_time, 0.0)
    if exp_ct > 0.0:
        return abs(actual_ct - exp_ct) <= max(0.5, float(max_drift))
    st_ts = safe_float(started_ts, 0.0)
    if st_ts <= 0.0:
        return False
    if role == "owner":
        return actual_ct <= st_ts + 5.0
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
    pid_i = safe_int(pid, None)
    if not pid_i or pid_i <= 0:
        return False
    exp_ct = safe_float(expected_create_time, 0.0)
    children: List[Any] = []
    root: Any = None

    if psutil is not None:
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
    elif exp_ct > 0.0:
        # Fail closed when identity cannot be verified without psutil
        return False

    if os.name == "nt" and is_pid_running(pid_i):
        if exp_ct > 0.0:
            cur_ct = get_process_create_time(pid_i)
            if cur_ct is None or abs(cur_ct - exp_ct) > 2.0:
                return True
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid_i)],
                capture_output=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
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
    prepare_fn: Callable[[List[str]], List[str]] = prepare_subprocess_command,
    kill_fn: Callable[..., bool] = kill_process_tree,
) -> subprocess.CompletedProcess:
    """Run a subprocess with Windows .cmd shim unwrapping and guaranteed descendant tree kill on timeout."""
    prepared = prepare_fn(cmd)
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
        kill_fn(proc.pid, expected_create_time=proc_create_time)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(prepared, timeout, output=stdout, stderr=stderr)

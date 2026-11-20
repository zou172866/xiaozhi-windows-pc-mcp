"""Headless supervisor watchdog for host 127.0.0.1:18900 PC Control MCP server (P1-15).

Runs inside the user's interactive desktop session (WinSta0\\Default) without a console window
(launched via WMI Win32_Process.Create ShowWindow=0 from data/start_all.ps1).

Features:
- Token-authenticated HTTP health probe against GET http://127.0.0.1:18900/audio/status
- Consecutive-failure threshold + exponential backoff (1s -> 2s -> 5s -> 15s -> 30s)
- Logs data/pc_mcp_18900_boot.log tail on startup/health failure
- Safe L3 child-tree cleanup (verified by psutil create_time()) without ever killing user browsers
- Single-instance PID lock and clean stop-flag shutdown
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

import httpx
import psutil

MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import l3_backends  # noqa: E402

SERVER_DIR = MCP_DIR.parent
DATA_DIR = SERVER_DIR / "data"
SECRETS_DIR = SERVER_DIR / "secrets"
LOGS_DIR = SERVER_DIR / "logs"
STATE_DIR = l3_backends.STATE_DIR
TOKEN_FILE = SECRETS_DIR / "pc_mcp_token.txt"
LEGACY_TOKEN_FILE = DATA_DIR / "pc_mcp_token.txt"
BOOT_LOG_FILE = LOGS_DIR / "pc_mcp_18900_boot.log"
MCP_LOG_FILE = LOGS_DIR / "pc_mcp_18900.log"
WATCHDOG_LOG_FILE = LOGS_DIR / "pc_mcp_watchdog.log"
PID_FILE = STATE_DIR / "pc_mcp_watchdog.pid"
STATE_FILE = STATE_DIR / "watchdog_state.json"
STOP_FLAG_FILE = STATE_DIR / "watchdog_stop.flag"
MCP_SCRIPT = MCP_DIR / "pc_control_mcp.py"

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEFAULT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 15.0, 30.0)


def read_bearer_token(token_file: Path = TOKEN_FILE) -> str:
    candidates = [token_file]
    if token_file == TOKEN_FILE and LEGACY_TOKEN_FILE not in candidates:
        candidates.append(LEGACY_TOKEN_FILE)
    for cand in candidates:
        try:
            if cand.is_file():
                tok = cand.read_text(encoding="utf-8", errors="ignore").strip()
                if tok:
                    return tok
        except Exception:
            pass
    return ""


def read_log_tail(log_path: Path, max_lines: int = 40) -> str:
    try:
        if not log_path.is_file() or log_path.stat().st_size == 0:
            return ""
        text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-max(1, int(max_lines)) :])
    except Exception:
        return ""


def probe_pc_mcp_health(
    *,
    host: str = "127.0.0.1",
    port: int = 18900,
    token: Optional[str] = None,
    token_file: Path = TOKEN_FILE,
    timeout: float = 2.5,
) -> Tuple[bool, str]:
    """Verify 18900 is alive and serving authenticated /audio/status."""
    tok = (token if token is not None else read_bearer_token(token_file)).strip()
    if not tok:
        return False, "missing pc_mcp_token.txt"
    url = f"http://{host}:{int(port)}/audio/status"
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {tok}"})
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
        data = resp.json() if resp.content else {}
        if not isinstance(data, dict) or not data.get("ok"):
            return False, f"unexpected payload: {str(data)[:120]}"
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def _is_pc_mcp_python_cmdline(proc_name: str, cmd: list[str], script_path: Path = MCP_SCRIPT) -> bool:
    """Return True ONLY if process is a Python interpreter directly executing pc_control_mcp.py."""
    base_name = os.path.basename(str(proc_name or "")).strip().lower()
    if not (base_name in ("python", "python.exe", "pythonw", "pythonw.exe") or base_name.startswith("python3")):
        return False
    if not cmd or len(cmd) < 2:
        return False
    try:
        target_resolved = str(script_path.resolve()).lower()
    except Exception:
        target_resolved = str(script_path).lower()
    target_name = script_path.name.lower()
    skip_next = False
    for arg in cmd[1:]:
        a = str(arg or "").strip()
        if skip_next:
            skip_next = False
            continue
        if a in ("-c", "-m"):
            # Everything after `-c` / `-m` is inline code or module args, NOT a script path
            return False
        if a in ("-X", "-W", "-Q"):
            skip_next = True
            continue
        if a.startswith("-"):
            continue
        a_clean = a.strip('"').strip("'")
        if os.path.basename(a_clean).lower() == target_name:
            try:
                if str(Path(a_clean).resolve()).lower() == target_resolved:
                    return True
            except Exception:
                pass
            return True
        # First positional argument is the executed script; if it isn't pc_control_mcp.py, stop checking
        return False
    return False


def find_pc_mcp_pids(port: int = 18900, script_path: Path = MCP_SCRIPT) -> list[int]:
    """Locate listening 18900 MCP server PID(s) without touching user shells, editors, or browsers."""
    pids: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == int(port) and conn.pid:
                pids.add(int(conn.pid))
    except Exception:
        pass
    try:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            name = p.info.get("name") or ""
            cmd = p.info.get("cmdline") or []
            if _is_pc_mcp_python_cmdline(name, cmd, script_path=script_path):
                pids.add(int(p.info["pid"]))
    except Exception:
        pass
    pids.discard(os.getpid())
    return sorted(pids)


def stop_pc_mcp_and_l3_children(port: int = 18900, log: Optional[logging.Logger] = None) -> dict:
    """Clean verified L3 child process trees first, then stop ONLY the 18900 MCP server process(es).

    Preserves user browser windows opened via L1 `pc_open_app_or_web` (honoring AGENTS.md).
    """
    l3_cleaned = 0
    try:
        l3_cleaned = l3_backends.cleanup_running_task_processes()
    except Exception as exc:
        if log:
            log.warning("Failed cleaning L3 child tasks: %s", exc)

    stopped_pids: list[int] = []
    for pid in find_pc_mcp_pids(port=port):
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.5)
            stopped_pids.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        except Exception as exc:
            if log:
                log.warning("Failed stopping 18900 PID=%s: %s", pid, exc)
    return {"l3_cleaned": l3_cleaned, "stopped_pids": stopped_pids}


def spawn_pc_mcp_server(
    *,
    python_exe: Optional[str] = None,
    script_path: Path = MCP_SCRIPT,
    boot_log_path: Path = BOOT_LOG_FILE,
    log: Optional[logging.Logger] = None,
) -> subprocess.Popen:
    """Spawn pc_control_mcp.py headlessly inside current WinSta0\\Default desktop."""
    py = python_exe or sys.executable
    boot_log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    log_fp = None
    for attempt in range(3):
        try:
            log_fp = open(boot_log_path, "wb")
            break
        except OSError as exc:
            if attempt < 2:
                time.sleep(0.15)
            elif log:
                log.warning("Could not open boot log %s (%s); spawning with DEVNULL", boot_log_path, exc)

    try:
        kwargs = {
            "cwd": str(SERVER_DIR),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": log_fp if log_fp is not None else subprocess.DEVNULL,
            "stderr": subprocess.STDOUT if log_fp is not None else subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        proc = subprocess.Popen([py, "-X", "utf8", str(script_path)], **kwargs)
    finally:
        # Child inherited file handle; parent closes its copy cleanly
        if log_fp is not None:
            log_fp.close()
    return proc


class WatchdogSupervisor:
    """Testable health-probe + backoff restart state machine for host 18900 MCP service."""

    def __init__(
        self,
        *,
        probe_fn: Callable[[], Tuple[bool, str]],
        restart_fn: Callable[[], bool],
        stop_check_fn: Callable[[], bool],
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
        failure_threshold: int = 2,
        backoff_schedule: Tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
        healthy_reset_seconds: float = 60.0,
        state_file: Optional[Path] = STATE_FILE,
        log: Optional[logging.Logger] = None,
    ):
        self._probe_fn = probe_fn
        self._restart_fn = restart_fn
        self._stop_check_fn = stop_check_fn
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        self.failure_threshold = max(1, int(failure_threshold))
        self.backoff_schedule = tuple(backoff_schedule) or (1.0,)
        self.healthy_reset_seconds = max(1.0, float(healthy_reset_seconds))
        self.state_file = state_file
        self.log = log or logging.getLogger("xiaozhi.pc_mcp_watchdog")

        self.consecutive_failures = 0
        self.restart_count = 0
        self.backoff_idx = 0
        self.last_restart_ts = 0.0
        self.last_ok_ts = 0.0
        self.last_status = "starting"
        self.last_error = ""

    def _write_state(self) -> None:
        if not self.state_file:
            return
        try:
            l3_backends.atomic_write_json(
                self.state_file,
                {
                    "pid": os.getpid(),
                    "ts": time.time(),
                    "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": self.last_status,
                    "consecutive_failures": self.consecutive_failures,
                    "restart_count": self.restart_count,
                    "last_error": self.last_error[:200],
                },
            )
        except Exception:
            pass

    def tick(self) -> str:
        """Run one supervision cycle. Returns 'stopped' | 'healthy' | 'degraded' | 'restarted' | 'restart_failed'."""
        if self._stop_check_fn():
            self.last_status = "stopped"
            self._write_state()
            return "stopped"

        now = self._clock_fn()
        ok, detail = self._probe_fn()
        if ok:
            self.consecutive_failures = 0
            self.last_error = ""
            self.last_ok_ts = now
            self.last_status = "healthy"
            if self.backoff_idx > 0 and self.last_restart_ts > 0 and (now - self.last_restart_ts) >= self.healthy_reset_seconds:
                self.backoff_idx = 0
            self._write_state()
            return "healthy"

        self.consecutive_failures += 1
        self.last_error = str(detail or "probe_failed")
        if self.consecutive_failures < self.failure_threshold:
            self.last_status = "degraded"
            self.log.warning(
                "18900 health probe failed (%d/%d): %s",
                self.consecutive_failures,
                self.failure_threshold,
                self.last_error,
            )
            self._write_state()
            return "degraded"

        delay = self.backoff_schedule[min(self.backoff_idx, len(self.backoff_schedule) - 1)]
        self.backoff_idx = min(self.backoff_idx + 1, len(self.backoff_schedule) - 1)
        self.log.error(
            "18900 unhealthy after %d consecutive probes (%s); restarting after %.1fs backoff...",
            self.consecutive_failures,
            self.last_error,
            delay,
        )
        if delay > 0:
            self._sleep_fn(delay)
        if self._stop_check_fn():
            self.last_status = "stopped"
            self._write_state()
            return "stopped"

        self.restart_count += 1
        self.last_restart_ts = self._clock_fn()
        self.consecutive_failures = 0
        restarted_ok = bool(self._restart_fn())
        self.last_status = "restarted" if restarted_ok else "restart_failed"
        self._write_state()
        return self.last_status


def _configure_watchdog_logging() -> logging.Logger:
    WATCHDOG_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("xiaozhi.pc_mcp_watchdog")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(
        str(WATCHDOG_LOG_FILE), maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    return logger


def _take_single_instance_lock(pid_file: Path = PID_FILE) -> None:
    """Stop any older verified watchdog process and write our PID + create_time."""
    if pid_file.is_file():
        try:
            old = json.loads(pid_file.read_text(encoding="utf-8"))
            old_pid = l3_backends._safe_int(old.get("pid"), None)
            old_ct = l3_backends._safe_float(old.get("create_time"), 0.0)
            if old_pid and old_pid != os.getpid() and l3_backends.is_same_process(old_pid, old_ct):
                try:
                    p = psutil.Process(old_pid)
                    p.terminate()
                    p.wait(timeout=2.0)
                except Exception:
                    try:
                        psutil.Process(old_pid).kill()
                    except Exception:
                        pass
        except Exception:
            pass
    l3_backends.atomic_write_json(
        pid_file,
        {
            "pid": os.getpid(),
            "create_time": l3_backends.get_process_create_time(os.getpid()),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Xiaozhi host 18900 PC MCP watchdog")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PC_MCP_PORT") or "18900"))
    parser.add_argument("--interval", type=float, default=8.0)
    parser.add_argument("--check-health", action="store_true", help="Run one authenticated /audio/status probe and exit")
    parser.add_argument("--cleanup-l3", action="store_true", help="Clean verified active L3 child process trees and exit")
    args = parser.parse_args()

    if args.check_health:
        ok, detail = probe_pc_mcp_health(port=args.port)
        print(json.dumps({"ok": ok, "detail": detail}, ensure_ascii=False))
        return 0 if ok else 1

    if args.cleanup_l3:
        n = l3_backends.cleanup_running_task_processes()
        print(json.dumps({"ok": True, "l3_cleaned": n}, ensure_ascii=False))
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FLAG_FILE.unlink(missing_ok=True)
    _take_single_instance_lock()
    log = _configure_watchdog_logging()
    stop_requested = {"flag": False}

    def _on_signal(_signum, _frame):
        stop_requested["flag"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except Exception:
            pass

    def _should_stop() -> bool:
        return bool(stop_requested["flag"] or STOP_FLAG_FILE.exists())

    def _do_restart() -> bool:
        stop_pc_mcp_and_l3_children(port=args.port, log=log)
        try:
            proc = spawn_pc_mcp_server(log=log)
        except Exception as exc:
            log.error("Failed to respawn pc_control_mcp.py: %s", exc)
            return False
        for _ in range(20):
            if _should_stop():
                return False
            rc = proc.poll()
            if rc is not None:
                tail = read_log_tail(BOOT_LOG_FILE, 30) or read_log_tail(MCP_LOG_FILE, 30)
                log.error(
                    "Respawned pc_control_mcp.py exited early (returncode=%s). boot.log tail:\n%s",
                    rc,
                    tail,
                )
                return False
            ok, _ = probe_pc_mcp_health(port=args.port, timeout=1.5)
            if ok:
                log.info("18900 PC MCP respawned and passed /audio/status health check.")
                return True
            time.sleep(0.5)
        tail = read_log_tail(BOOT_LOG_FILE, 30) or read_log_tail(MCP_LOG_FILE, 30)
        log.error("18900 failed health verification after respawn. boot.log tail:\n%s", tail)
        return False

    log.info("PC MCP watchdog started (pid=%d, port=%d, interval=%.1fs).", os.getpid(), args.port, args.interval)
    sup = WatchdogSupervisor(
        probe_fn=lambda: probe_pc_mcp_health(port=args.port),
        restart_fn=_do_restart,
        stop_check_fn=_should_stop,
        log=log,
    )

    try:
        while not _should_stop():
            res = sup.tick()
            if res == "stopped":
                break
            end_wait = time.monotonic() + max(1.0, float(args.interval))
            while time.monotonic() < end_wait and not _should_stop():
                time.sleep(0.5)
    finally:
        log.info("PC MCP watchdog exiting cleanly (pid=%d).", os.getpid())
        try:
            if PID_FILE.is_file():
                cur = json.loads(PID_FILE.read_text(encoding="utf-8"))
                if cur.get("pid") == os.getpid():
                    PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

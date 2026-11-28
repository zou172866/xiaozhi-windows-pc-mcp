"""Windows taskbar flash / notification 10-second debounce aggregator (host 18900).

Design & Privacy invariants:
- Strictly disabled by default: ZERO background threads, ZERO Win32 hooks until explicitly enabled.
- Pure in-memory rolling aggregation: NEVER persists notification or taskbar events to disk.
- Privacy-preserving: captures only process/app names (never raw window titles for taskbar flashes);
  optional notification previews are truncated to 60 chars and redacted through `redact_secrets`.
- Hard-ignores Windows shell/system noise (`conhost`, `explorer`, `msrdc`, `mstsc`, etc.) and self
  (`python`/`pythonw`/`xiaozhi`) to prevent self-triggering loops.
- 10-second quiet debounce window groups bursty signals into a single digest with monotonic `digest_id`.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import facade_attr
from .security import _get_audit_extra_secrets, redact_secrets
from .winapi import (
    _get_process_name_by_pid,
    _get_window_title,
    _kernel32,
    _list_top_windows,
    _user32,
)

DEFAULT_QUIET_SECONDS = 10.0
MIN_QUIET_SECONDS = 2.0
MAX_QUIET_SECONDS = 60.0
MAX_PREVIEW_CHARS = 60
MAX_DIGEST_ITEMS = 8
MAX_DIGEST_AGE_SECONDS = 900.0
MAX_RECENT_APPS = 12

# System / self processes that never represent an external human message
DEFAULT_IGNORE_PROCS: frozenset[str] = frozenset(
    {
        "conhost",
        "openconsole",
        "explorer",
        "msrdc",
        "mstsc",
        "searchhost",
        "searchapp",
        "searchui",
        "shellexperiencehost",
        "startmenuexperiencehost",
        "applicationframehost",
        "dwm",
        "ctfmon",
        "textinputhost",
        "python",
        "pythonw",
        "xiaozhi",
        "windows.systemtoast",
        "microsoft.windows",
    }
)

_HSHELL_REDRAW = 6
_HSHELL_FLASH = 0x8006
_WM_DESTROY = 0x0002
_WM_QUIT = 0x0012
_MAX_TITLES_TRACKED = 512
_MAX_STAGING_APPS = 64

_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HICON),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


if os.name == "nt":
    try:
        _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        _user32.RegisterWindowMessageW.restype = wintypes.UINT
        _user32.RegisterShellHookWindow.argtypes = [wintypes.HWND]
        _user32.RegisterShellHookWindow.restype = wintypes.BOOL
        _user32.DeregisterShellHookWindow.argtypes = [wintypes.HWND]
        _user32.DeregisterShellHookWindow.restype = wintypes.BOOL
        _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
        _user32.RegisterClassW.restype = wintypes.ATOM
        _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        _user32.UnregisterClassW.restype = wintypes.BOOL
        _user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        _user32.CreateWindowExW.restype = wintypes.HWND
        _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        _user32.DefWindowProcW.restype = ctypes.c_ssize_t
        _user32.DestroyWindow.argtypes = [wintypes.HWND]
        _user32.DestroyWindow.restype = wintypes.BOOL
        _user32.PostQuitMessage.argtypes = [ctypes.c_int]
        _user32.PostQuitMessage.restype = None
        _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        _user32.TranslateMessage.restype = wintypes.BOOL
        _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        _user32.DispatchMessageW.restype = ctypes.c_ssize_t
        _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        _user32.PostMessageW.restype = wintypes.BOOL
        _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        _kernel32.GetCurrentThreadId.argtypes = []
        _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    except Exception:
        pass


def _norm_token(name: str) -> str:
    s = str(name or "").strip().lower()
    if s.endswith(".exe"):
        s = s[:-4]
    return s


def _sanitize_preview(raw: str) -> str:
    flat = " ".join(str(raw or "").split()).strip()
    if not flat:
        return ""
    flat = redact_secrets(flat, _get_audit_extra_secrets())
    if len(flat) > MAX_PREVIEW_CHARS:
        flat = flat[: MAX_PREVIEW_CHARS - 1].rstrip() + "…"
    return flat


class TaskbarInboxMonitor:
    """Thread-safe taskbar flash & notification aggregator with 10s quiet debounce."""

    def __init__(
        self,
        *,
        clock_fn: Optional[Callable[[], float]] = None,
        mono_fn: Optional[Callable[[], float]] = None,
        enable_win32_hook: bool = True,
    ) -> None:
        self._clock = clock_fn or time.time
        self._mono = mono_fn or time.monotonic
        self._enable_win32_hook = bool(enable_win32_hook) and (os.name == "nt")

        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._enabled: bool = False
        self._quiet_seconds: float = DEFAULT_QUIET_SECONDS
        self._extra_ignore: List[str] = []
        self._recent_apps: List[str] = []
        self._last_error: str = ""

        # Staging buffer before quiet window settles
        self._staging: Dict[str, Dict[str, Any]] = {}
        self._stage_first_mono: float = 0.0
        self._stage_last_mono: float = 0.0

        # Latest settled digest (ready for consumption)
        self._digest_seq: int = 0
        self._ready_digest: Optional[Dict[str, Any]] = None

        # Win32 SHELLHOOK listener thread state
        self._thread: Optional[threading.Thread] = None
        self._hook_hwnd: int = 0
        self._hook_tid: int = 0
        self._stop_evt = threading.Event()
        self._titles: Dict[int, str] = {}

    def _is_ignored_locked(self, *names: str) -> bool:
        ignores = set(DEFAULT_IGNORE_PROCS)
        for item in self._extra_ignore:
            tok = _norm_token(item)
            if tok:
                ignores.add(tok)
        for raw in names:
            tok = _norm_token(raw)
            if not tok:
                continue
            if tok in ignores:
                return True
            for ig in ignores:
                if tok.startswith(f"{ig}."):
                    return True
        return False

    def record_event(
        self,
        *,
        app: str,
        exe: str = "",
        flashed: bool = False,
        notification_text: str = "",
    ) -> bool:
        """Ingest one taskbar flash or notification signal (in-memory only)."""
        app_clean = str(app or "").strip()[:64]
        exe_clean = str(exe or "").strip()[:64]
        if not app_clean and exe_clean:
            app_clean = exe_clean[:-4] if exe_clean.lower().endswith(".exe") else exe_clean
        if not app_clean:
            return False

        now_wall = float(self._clock())
        now_mono = float(self._mono())
        with self._lock:
            if not self._enabled:
                return False
            if self._is_ignored_locked(app_clean, exe_clean):
                return False
            key = _norm_token(exe_clean or app_clean) or app_clean.lower()
            entry = self._staging.get(key)
            if entry is None:
                if len(self._staging) >= _MAX_STAGING_APPS:
                    oldest_key = min(self._staging, key=lambda k: float(self._staging[k].get("last_seen_at") or 0.0))
                    self._staging.pop(oldest_key, None)
                entry = {
                    "app": app_clean,
                    "exe": exe_clean,
                    "flashed": False,
                    "notes": 0,
                    "preview": "",
                    "first_seen_at": now_wall,
                    "last_seen_at": now_wall,
                }
                self._staging[key] = entry
                if self._stage_first_mono <= 0.0:
                    self._stage_first_mono = now_mono
            if flashed:
                entry["flashed"] = True
            if notification_text:
                entry["notes"] = int(entry["notes"]) + 1
                prev = _sanitize_preview(notification_text)
                if prev:
                    entry["preview"] = prev
            entry["last_seen_at"] = now_wall
            self._stage_last_mono = now_mono

            if app_clean not in self._recent_apps:
                self._recent_apps.insert(0, app_clean)
                if len(self._recent_apps) > MAX_RECENT_APPS:
                    self._recent_apps = self._recent_apps[:MAX_RECENT_APPS]
            return True

    def _maybe_settle_locked(self, now_mono: float, now_wall: float) -> None:
        if not self._staging:
            return
        quiet_elapsed = now_mono - self._stage_last_mono
        max_wait = max(self._quiet_seconds * 3.0, 30.0)
        total_elapsed = now_mono - self._stage_first_mono if self._stage_first_mono > 0 else quiet_elapsed
        if quiet_elapsed < self._quiet_seconds and total_elapsed < max_wait:
            return

        sorted_items = sorted(
            self._staging.values(),
            key=lambda it: float(it.get("last_seen_at") or 0.0),
            reverse=True,
        )[:MAX_DIGEST_ITEMS]
        self._digest_seq += 1
        digest_id = f"inbox-{self._digest_seq}-{int(now_wall)}"
        self._ready_digest = {
            "digest_id": digest_id,
            "created_at": now_wall,
            "created_mono": now_mono,
            "items": [dict(it) for it in sorted_items],
        }
        self._staging.clear()
        self._stage_first_mono = 0.0
        self._stage_last_mono = 0.0

    def get_summary(self) -> Dict[str, Any]:
        """Return current settled digest (or empty if disabled/still quiet-debouncing/expired)."""
        now_wall = float(self._clock())
        now_mono = float(self._mono())
        with self._lock:
            if not self._enabled:
                return {
                    "enabled": False,
                    "quiet_seconds": self._quiet_seconds,
                    "staging_count": 0,
                    "digest_id": "",
                    "age_seconds": 0.0,
                    "items": [],
                    "recent_apps": list(self._recent_apps),
                    "extra_ignore": list(self._extra_ignore),
                    "error": self._last_error,
                }
            self._maybe_settle_locked(now_mono, now_wall)
            digest = self._ready_digest
            if digest is not None:
                age = max(0.0, now_mono - float(digest.get("created_mono") or now_mono))
                if age > MAX_DIGEST_AGE_SECONDS:
                    self._ready_digest = None
                    digest = None
            items_out: List[Dict[str, Any]] = []
            digest_id = ""
            age_out = 0.0
            if digest is not None:
                digest_id = str(digest["digest_id"])
                age_out = round(max(0.0, now_mono - float(digest["created_mono"])), 1)
                for it in digest.get("items") or []:
                    item_age = max(0.0, now_wall - float(it.get("last_seen_at") or now_wall))
                    items_out.append(
                        {
                            "app": str(it["app"]),
                            "exe": str(it.get("exe") or ""),
                            "flashed": bool(it.get("flashed", False)),
                            "notes": int(it.get("notes") or 0),
                            "preview": str(it.get("preview") or ""),
                            "age_seconds": round(item_age, 1),
                        }
                    )
            return {
                "enabled": True,
                "quiet_seconds": self._quiet_seconds,
                "staging_count": len(self._staging),
                "digest_id": digest_id,
                "age_seconds": age_out,
                "items": items_out,
                "recent_apps": list(self._recent_apps),
                "extra_ignore": list(self._extra_ignore),
                "error": self._last_error,
            }

    def clear(self) -> None:
        with self._lock:
            self._staging.clear()
            self._stage_first_mono = 0.0
            self._stage_last_mono = 0.0
            self._ready_digest = None

    def configure(
        self,
        *,
        enabled: Optional[bool] = None,
        quiet_seconds: Optional[float] = None,
        extra_ignore: Optional[Iterable[str]] = None,
        clear_pending: bool = False,
    ) -> Dict[str, Any]:
        with self._lifecycle_lock:
            should_start = False
            should_stop = False
            with self._lock:
                if quiet_seconds is not None:
                    if isinstance(quiet_seconds, bool) or not isinstance(quiet_seconds, (int, float)):
                        raise ValueError("quiet_seconds 必须为数字 (2..60)")
                    qv = float(quiet_seconds)
                    if qv != qv or qv < MIN_QUIET_SECONDS or qv > MAX_QUIET_SECONDS:
                        raise ValueError(f"quiet_seconds 必须在 {MIN_QUIET_SECONDS}..{MAX_QUIET_SECONDS} 秒之间")
                    self._quiet_seconds = round(qv, 1)
                if extra_ignore is not None:
                    cleaned: List[str] = []
                    seen: set[str] = set()
                    for item in extra_ignore:
                        s = str(item or "").strip()[:64]
                        low = _norm_token(s)
                        if s and low and low not in seen:
                            seen.add(low)
                            cleaned.append(s)
                        if len(cleaned) >= 40:
                            break
                    self._extra_ignore = cleaned
                if clear_pending:
                    self._staging.clear()
                    self._stage_first_mono = 0.0
                    self._stage_last_mono = 0.0
                    self._ready_digest = None
                if enabled is not None:
                    if not isinstance(enabled, bool):
                        raise ValueError("enabled 必须为布尔值")
                    if enabled and not self._enabled:
                        self._enabled = True
                        self._last_error = ""
                        should_start = True
                    elif not enabled and self._enabled:
                        self._enabled = False
                        self._staging.clear()
                        self._stage_first_mono = 0.0
                        self._stage_last_mono = 0.0
                        self._ready_digest = None
                        should_stop = True

            if should_start:
                self.start()
            elif should_stop:
                self.stop()
            return self.get_summary()

    def _on_shell_flash(self, hwnd: int) -> None:
        if not hwnd:
            return
        u32 = facade_attr("_user32", _user32)
        pid = wintypes.DWORD(0)
        try:
            u32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        except Exception:
            return
        pid_val = int(pid.value)
        if pid_val <= 0 or pid_val == os.getpid():
            return
        exe = _get_process_name_by_pid(pid_val) or ""
        app = exe[:-4] if exe.lower().endswith(".exe") else exe
        if app:
            self.record_event(app=app, exe=exe, flashed=True)

    def _hook_loop(self, ready: threading.Event) -> None:
        hwnd = 0
        hinst = None
        registered_class = False
        class_name = f"XiaoZhiTaskbarInbox_{os.getpid()}_{threading.get_ident()}"
        tid = int(_kernel32.GetCurrentThreadId()) if os.name == "nt" else 0
        try:
            with self._lock:
                self._hook_tid = tid
            # Snapshot current window titles so first HSHELL_REDRAW distinguishes flash vs title rename
            try:
                for w in (_list_top_windows() or [])[:_MAX_TITLES_TRACKED]:
                    if w.get("hwnd"):
                        self._titles[int(w["hwnd"])] = str(w.get("title") or "")
            except Exception:
                pass

            shell_msg = int(_user32.RegisterWindowMessageW("SHELLHOOK") or 0)
            if not shell_msg:
                raise RuntimeError("RegisterWindowMessageW(SHELLHOOK) failed")

            def _wndproc(h, msg, wp, lp):
                if msg == shell_msg:
                    code = int(wp)
                    target_h = int(lp)
                    if code == _HSHELL_FLASH:
                        self._on_shell_flash(target_h)
                    elif code == _HSHELL_REDRAW and target_h:
                        cur_title = _get_window_title(target_h)
                        prev_title = self._titles.get(target_h)
                        if len(self._titles) >= _MAX_TITLES_TRACKED and target_h not in self._titles:
                            self._titles.clear()
                        self._titles[target_h] = cur_title
                        if prev_title is not None and prev_title == cur_title:
                            self._on_shell_flash(target_h)
                elif msg == _WM_DESTROY:
                    _user32.PostQuitMessage(0)
                    return 0
                return _user32.DefWindowProcW(h, msg, wp, lp)

            proc_cb = _WNDPROC(_wndproc)
            hinst = _kernel32.GetModuleHandleW(None)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = proc_cb
            wc.hInstance = hinst
            wc.lpszClassName = class_name
            if not _user32.RegisterClassW(ctypes.byref(wc)):
                raise RuntimeError(f"RegisterClassW failed (err={ctypes.get_last_error()})")
            registered_class = True

            hwnd = int(
                _user32.CreateWindowExW(
                    0,
                    class_name,
                    class_name,
                    0,
                    0,
                    0,
                    0,
                    0,
                    None,
                    None,
                    hinst,
                    None,
                )
                or 0
            )
            if not hwnd:
                raise RuntimeError(f"CreateWindowExW failed (err={ctypes.get_last_error()})")
            if not _user32.RegisterShellHookWindow(wintypes.HWND(hwnd)):
                raise RuntimeError("RegisterShellHookWindow failed")

            with self._lock:
                self._hook_hwnd = hwnd
                self._last_error = ""
            ready.set()

            msg = wintypes.MSG()
            while not self._stop_evt.is_set():
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            with self._lock:
                self._last_error = f"任务栏监听初始化受限: {exc}"
            ready.set()
        finally:
            if hwnd:
                try:
                    _user32.DeregisterShellHookWindow(wintypes.HWND(hwnd))
                except Exception:
                    pass
                try:
                    _user32.DestroyWindow(wintypes.HWND(hwnd))
                except Exception:
                    pass
            if registered_class:
                try:
                    _user32.UnregisterClassW(class_name, hinst)
                except Exception:
                    pass
            self._titles.clear()
            with self._lock:
                self._hook_hwnd = 0
                self._hook_tid = 0

    def start(self) -> None:
        if not self._enable_win32_hook:
            return
        with self._lock:
            if not self._enabled:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_evt.clear()
            ready = threading.Event()
            t = threading.Thread(
                target=self._hook_loop,
                args=(ready,),
                name="pc-mcp-taskbar-inbox",
                daemon=True,
            )
            self._thread = t
            t.start()
        ready.wait(timeout=1.5)

    def stop(self) -> None:
        with self._lock:
            self._stop_evt.set()
            tid = self._hook_tid
            t = self._thread
        if tid and os.name == "nt":
            try:
                _user32.PostThreadMessageW(wintypes.DWORD(tid), _WM_QUIT, 0, 0)
            except Exception:
                pass
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        with self._lock:
            if self._thread is t:
                self._thread = None


_inbox_singleton_lock = threading.Lock()
_inbox_singleton: Optional[TaskbarInboxMonitor] = None


def get_taskbar_inbox() -> TaskbarInboxMonitor:
    global _inbox_singleton
    with _inbox_singleton_lock:
        if _inbox_singleton is None:
            _inbox_singleton = TaskbarInboxMonitor()
        return _inbox_singleton


def stop_taskbar_inbox() -> None:
    with _inbox_singleton_lock:
        inst = _inbox_singleton
    if inst is not None:
        inst.stop()

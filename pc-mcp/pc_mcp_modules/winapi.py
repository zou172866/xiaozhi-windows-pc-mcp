"""Win32 API bindings, system media/volume/power control, and desktop window focus helpers (P2-20)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import re
import subprocess
import threading
import time
from typing import Optional

import psutil

from . import _com_mta_scope, _gui_lock, facade_attr

# Win32 Virtual-Key Media & Power helpers
VK_VOLUME_MUTE = 0xAD
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

SMTO_NORMAL = 0x0000
SMTO_ABORTIFHUNG = 0x0002

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_size_t),
]
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.IsZoomed.argtypes = [wintypes.HWND]
_user32.IsZoomed.restype = wintypes.BOOL
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_user32.AllowSetForegroundWindow.restype = wintypes.BOOL
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = wintypes.LONG
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.argtypes = []
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.argtypes = []
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
_user32.GetMessageW.restype = ctypes.c_int
_user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HMONITOR
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
_user32.GetMonitorInfoW.restype = wintypes.BOOL


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_MONITOR_DEFAULTTONEAREST = 2

_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
_user32.GetGUIThreadInfo.restype = wintypes.BOOL
_user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
_user32.GetLastInputInfo.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_GA_ROOT = 2

_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.GetTickCount64.argtypes = []
_kernel32.GetTickCount64.restype = ctypes.c_uint64
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.BOOL,
    wintypes.DWORD,
]
_kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_shell32.ShellExecuteW.argtypes = [
    wintypes.HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_int,
]
_shell32.ShellExecuteW.restype = wintypes.HINSTANCE

_HWND_TOPMOST = wintypes.HWND(-1)
_HWND_NOTOPMOST = wintypes.HWND(-2)
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_SHOWWINDOW = 0x0040
_SW_SHOWNORMAL = 1
_SW_SHOW = 5
_SW_RESTORE = 9
_WS_EX_TOOLWINDOW = 0x00000080
_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000


def _tap_vk(vk_code: int) -> None:
    tmod = facade_attr("time", time)
    ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
    tmod.sleep(0.03)
    ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)


def _broadcast_screen_off(timeout_ms: int = 2000) -> bool:
    """Broadcast WM_SYSCOMMAND/SC_MONITORPOWER with timeout so hung windows never block the thread (P2-23)."""
    u32 = facade_attr("_user32", _user32)
    res = ctypes.c_size_t(0)
    ret = u32.SendMessageTimeoutW(
        0xFFFF,
        0x0112,  # WM_SYSCOMMAND
        0xF170,  # SC_MONITORPOWER
        2,       # POWER_OFF
        SMTO_ABORTIFHUNG,
        max(100, int(timeout_ms)),
        ctypes.byref(res),
    )
    return bool(ret)


def _set_or_get_volume(action: str, level: Optional[int] = None) -> str:
    with _com_mta_scope():
        from pycaw.pycaw import AudioUtilities

        speakers = AudioUtilities.GetSpeakers()
        vol = speakers.EndpointVolume
        if action == "get_volume":
            cur = round(vol.GetMasterVolumeLevelScalar() * 100)
            muted = bool(vol.GetMute())
            return f"当前电脑系统音量为 {cur}%（{'已静音' if muted else '未静音'}）。"
        elif action == "set_volume":
            pct = max(0, min(100, int(level if level is not None else 30)))
            vol.SetMute(0, None)
            vol.SetMasterVolumeLevelScalar(pct / 100.0, None)
            return f"已将电脑系统音量调整为 {pct}%。"
        elif action == "mute":
            vol.SetMute(1, None)
            return "电脑已静音。"
        elif action == "unmute":
            vol.SetMute(0, None)
            cur = round(vol.GetMasterVolumeLevelScalar() * 100)
            return f"已取消静音，当前音量为 {cur}%。"
    return "音量操作完成。"


def run_system_control(action: str, volume_percent: int = 30) -> str:
    act = (action or "").strip().lower()
    if act in ("set_volume", "get_volume", "mute", "unmute"):
        return _set_or_get_volume(act, volume_percent)
    if act == "media_play_pause":
        with _gui_lock:
            _tap_vk(VK_MEDIA_PLAY_PAUSE)
        return "已切换媒体播放/暂停状态。"
    if act == "media_next":
        with _gui_lock:
            _tap_vk(VK_MEDIA_NEXT_TRACK)
        return "已切换到下一首。"
    if act == "media_prev":
        with _gui_lock:
            _tap_vk(VK_MEDIA_PREV_TRACK)
        return "已切换到上一首。"
    if act == "lock_screen":
        with _gui_lock:
            ctypes.windll.user32.LockWorkStation()
        return "电脑屏幕已锁定。"
    if act == "screen_off":
        with _gui_lock:
            _broadcast_screen_off()
        return "显示器已息屏。"
    return f"不支持的系统动作: {action}"


def _is_window_cloaked(hwnd: int) -> bool:
    cloaked = ctypes.c_int(0)
    res = _dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return res == 0 and cloaked.value != 0


def _get_window_title(hwnd: int) -> str:
    if not hwnd:
        return ""
    u32 = facade_attr("_user32", _user32)
    length = u32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    u32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
    return buf.value.strip()


def _list_top_windows() -> list[dict]:
    """Enumerate visible, non-cloaked top-level desktop windows in Z-order."""
    u32 = facade_attr("_user32", _user32)
    results: list[dict] = []
    proc_cache: dict[int, str] = {}

    @_WNDENUMPROC
    def _cb(hwnd, _lparam):
        if not u32.IsWindowVisible(hwnd):
            return True
        exstyle = u32.GetWindowLongW(hwnd, -20)
        if exstyle & _WS_EX_TOOLWINDOW:
            return True
        if _is_window_cloaked(hwnd):
            return True
        title = _get_window_title(hwnd)
        if not title or title == "Program Manager":
            return True
        cbuf = ctypes.create_unicode_buffer(128)
        u32.GetClassNameW(hwnd, cbuf, 128)
        pid = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_val = int(pid.value)
        if pid_val not in proc_cache:
            try:
                proc_cache[pid_val] = psutil.Process(pid_val).name().lower()
            except Exception:
                proc_cache[pid_val] = ""
        results.append(
            {
                "hwnd": int(hwnd),
                "pid": pid_val,
                "proc": proc_cache[pid_val],
                "class": cbuf.value,
                "title": title,
            }
        )
        return True

    u32.EnumWindows(_cb, 0)
    return results


def _force_foreground_window(hwnd: int) -> bool:
    """Bypass Windows ForegroundLockTimeout to restore and pop target window to front."""
    u32 = facade_attr("_user32", _user32)
    if not hwnd or not u32.IsWindow(wintypes.HWND(hwnd)):
        return False
    h = wintypes.HWND(hwnd)

    if u32.IsIconic(h):
        u32.ShowWindow(h, _SW_RESTORE)
    else:
        u32.ShowWindow(h, _SW_SHOW)

    fg_hwnd = u32.GetForegroundWindow()
    fg_tid = u32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    target_tid = u32.GetWindowThreadProcessId(h, None)
    cur_tid = _kernel32.GetCurrentThreadId()

    attached_fg = False
    attached_target = False
    if fg_tid and fg_tid != cur_tid:
        attached_fg = bool(u32.AttachThreadInput(cur_tid, fg_tid, True))
    if target_tid and target_tid not in (cur_tid, fg_tid):
        attached_target = bool(u32.AttachThreadInput(cur_tid, target_tid, True))

    try:
        u32.SetWindowPos(h, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        u32.SetWindowPos(h, _HWND_NOTOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        u32.BringWindowToTop(h)
        u32.SetForegroundWindow(h)
    finally:
        if attached_target:
            u32.AttachThreadInput(cur_tid, target_tid, False)
        if attached_fg:
            u32.AttachThreadInput(cur_tid, fg_tid, False)

    if u32.GetForegroundWindow() != hwnd:
        # Synthesize benign unassigned key tap to unlock SetForegroundWindow for this thread
        u32.keybd_event(0xFF, 0, 0, 0)
        u32.keybd_event(0xFF, 0, 2, 0)
        u32.SetWindowPos(h, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        u32.SetWindowPos(h, _HWND_NOTOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        u32.BringWindowToTop(h)
        u32.SetForegroundWindow(h)

    return u32.GetForegroundWindow() == hwnd


def _get_process_name_by_pid(pid: int) -> str:
    if not pid or pid <= 0:
        return ""
    try:
        name = psutil.Process(pid).name()
        if name:
            return name.strip().lower()
    except Exception:
        pass
    if os.name == "nt":
        hproc = None
        try:
            hproc = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
            if hproc:
                size = wintypes.DWORD(512)
                buf = ctypes.create_unicode_buffer(512)
                if _kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                    img_path = buf.value.strip()
                    if img_path:
                        return os.path.basename(img_path).lower()
        except Exception:
            pass
        finally:
            if hproc:
                try:
                    _kernel32.CloseHandle(hproc)
                except Exception:
                    pass
    return ""


def _get_window_class_name(hwnd: int) -> str:
    if not hwnd:
        return ""
    u32 = facade_attr("_user32", _user32)
    try:
        cbuf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(wintypes.HWND(hwnd), cbuf, 256)
        return cbuf.value.strip()
    except Exception:
        return ""


_XAML_BRIDGE_CLASSES = frozenset({
    "microsoft.ui.content.desktopchildsitebridge",
    "microsoft.ui.content.contentwindowsitebridge",
    "windows.ui.composition.desktopwindowcontentbridge",
    "windows.ui.input.inputsite.windowclass",
    "inputsitewindowclass",
})

_INSPECT_CHILD_WINDOW_CLASSES = frozenset({
    "cabinetwclass",
    "#32770",
})

_UIA_GENERIC_CONTAINER_TYPES = frozenset({
    "",
    "panecontrol",
    "windowcontrol",
    "customcontrol",
    "groupcontrol",
})


def _extract_uia_control_fields(ctrl) -> dict:
    """Extract control_type, name, automation_id, class, ancestor_names, and ancestor_types from a UIA control."""
    fields: dict = {}
    if ctrl is None:
        return fields
    ctype = str(getattr(ctrl, "ControlTypeName", "") or "").strip()
    cname = str(getattr(ctrl, "Name", "") or "").strip()
    aid = str(getattr(ctrl, "AutomationId", "") or "").strip()
    ccls = str(getattr(ctrl, "ClassName", "") or "").strip()
    if ctype:
        fields["control_type"] = ctype
    if cname:
        fields["name"] = cname
    if aid:
        fields["automation_id"] = aid
    if ccls:
        fields["class"] = ccls
    anc_parts = []
    anc_types = []
    parent = ctrl
    for _ in range(10):
        try:
            parent = parent.GetParentControl()
        except Exception:
            break
        if parent is None:
            break
        p_type = str(getattr(parent, "ControlTypeName", "") or "").strip()
        p_name = str(getattr(parent, "Name", "") or "").strip()
        p_cls = str(getattr(parent, "ClassName", "") or "").strip()
        p_aid = str(getattr(parent, "AutomationId", "") or "").strip()
        if p_type:
            anc_types.append(p_type)
        for part in (p_name, p_cls, p_aid):
            if part:
                anc_parts.append(part)
    if anc_parts:
        fields["ancestor_names"] = " | ".join(anc_parts)
    if anc_types:
        fields["ancestor_types"] = " | ".join(anc_types)
    return fields


def _is_unresolved_xaml_bridge(ctrl_info: dict, uia_fields: dict, uia_ok: bool) -> bool:
    """Return True when a XAML Islands bridge HWND was not resolved to a concrete inner UIA control (P1-2)."""
    base_cls = str(ctrl_info.get("class") or "").strip().lower()
    if base_cls not in _XAML_BRIDGE_CLASSES:
        return False
    if not uia_ok or not uia_fields:
        return True
    ucls = str(uia_fields.get("class") or "").strip().lower()
    utype = str(uia_fields.get("control_type") or "").strip().lower()
    if ucls in _XAML_BRIDGE_CLASSES:
        return True
    if utype in _UIA_GENERIC_CONTAINER_TYPES and not uia_fields.get("automation_id") and not uia_fields.get("name"):
        return True
    return False


def _get_focused_control_info(fg_hwnd: int = 0, *, parent_class: str = "") -> dict:
    """Inspect focused child control via Win32 GetGUIThreadInfo and UIA (used for CabinetWClass/#32770 address bar guard)."""
    u32 = facade_attr("_user32", _user32)
    info: dict = {}
    if fg_hwnd:
        try:
            tid = int(u32.GetWindowThreadProcessId(wintypes.HWND(fg_hwnd), None) or 0)
            if tid:
                gti = _GUITHREADINFO()
                gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
                if u32.GetGUIThreadInfo(wintypes.DWORD(tid), ctypes.byref(gti)):
                    focus_hwnd = int(gti.hwndFocus or 0)
                    if focus_hwnd:
                        info["hwnd"] = focus_hwnd
                        info["class"] = _get_window_class_name(focus_hwnd)
                        info["title"] = _get_window_title(focus_hwnd)
        except Exception:
            pass

    if os.name == "nt":
        uia_ok = False
        uia_fields: dict = {}
        try:
            import uiautomation as auto

            with _com_mta_scope():
                ctrl = auto.GetFocusedControl()
                if ctrl is not None:
                    uia_fields = _extract_uia_control_fields(ctrl)
                    for k, v in uia_fields.items():
                        if k == "class":
                            if v and not info.get("class"):
                                info["class"] = v
                            elif v:
                                info["uia_class"] = v
                        else:
                            info[k] = v
                    uia_ok = bool(uia_fields)
        except Exception:
            uia_ok = False
        parent_cls_low = str(parent_class or "").strip().lower()
        if _is_unresolved_xaml_bridge(info, uia_fields, uia_ok) or (
            not uia_ok
            and (
                parent_cls_low == "#32770"
                or (parent_cls_low == "cabinetwclass" and not info.get("class"))
            )
        ):
            info["uia_error"] = True
    return info


def _get_window_info_by_hwnd(hwnd: int, *, include_focused_control: bool = False) -> dict:
    u32 = facade_attr("_user32", _user32)
    if not hwnd:
        return {"hwnd": 0, "pid": 0, "process": "", "class": "", "title": ""}
    h = wintypes.HWND(hwnd)
    title = ""
    try:
        title = _get_window_title(hwnd)
    except Exception:
        pass
    cls_name = _get_window_class_name(hwnd)
    pid_val = 0
    try:
        pid_dw = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid_dw))
        pid_val = int(pid_dw.value)
    except Exception:
        pass
    proc_name = _get_process_name_by_pid(pid_val)
    result: dict = {
        "hwnd": int(hwnd),
        "pid": pid_val,
        "process": proc_name,
        "class": cls_name,
        "title": title,
    }
    if include_focused_control and cls_name.lower() in _INSPECT_CHILD_WINDOW_CLASSES:
        fc = _get_focused_control_info(hwnd, parent_class=cls_name)
        if fc:
            result["focused_control"] = fc
    return result


def _get_window_info_at_point(x: int, y: int) -> Optional[dict]:
    """Inspect the top-level window (and child control class) located at physical screen point (x, y)."""
    u32 = facade_attr("_user32", _user32)
    try:
        pt = wintypes.POINT(int(x), int(y))
        child_hwnd = int(u32.WindowFromPoint(pt) or 0)
    except Exception:
        return None
    if not child_hwnd:
        return None
    try:
        root_hwnd = int(u32.GetAncestor(wintypes.HWND(child_hwnd), _GA_ROOT) or 0) or child_hwnd
    except Exception:
        root_hwnd = child_hwnd
    info = _get_window_info_by_hwnd(root_hwnd, include_focused_control=False)
    if info.get("class", "").lower() in _INSPECT_CHILD_WINDOW_CLASSES:
        pt_ctrl: dict = {}
        if child_hwnd != root_hwnd:
            child_cls = _get_window_class_name(child_hwnd)
            child_title = _get_window_title(child_hwnd)
            pt_ctrl["hwnd"] = child_hwnd
            if child_cls:
                pt_ctrl["class"] = child_cls
            if child_title:
                pt_ctrl["name"] = child_title
        if os.name == "nt":
            uia_ok = False
            uia_fields: dict = {}
            try:
                import uiautomation as auto

                with _com_mta_scope():
                    ctrl = auto.ControlFromPoint(int(x), int(y))
                    if ctrl is not None:
                        uia_fields = _extract_uia_control_fields(ctrl)
                        for k, v in uia_fields.items():
                            if k == "class":
                                if v and not pt_ctrl.get("class"):
                                    pt_ctrl["class"] = v
                                elif v:
                                    pt_ctrl["uia_class"] = v
                            else:
                                pt_ctrl[k] = v
                        uia_ok = bool(uia_fields)
            except Exception:
                uia_ok = False
            win_cls_low = info.get("class", "").lower()
            if _is_unresolved_xaml_bridge(pt_ctrl, uia_fields, uia_ok) or (
                not uia_ok
                and (
                    win_cls_low == "#32770"
                    or (win_cls_low == "cabinetwclass" and not pt_ctrl.get("class"))
                )
            ):
                pt_ctrl["uia_error"] = True
        if pt_ctrl:
            info["point_control"] = pt_ctrl
    return info


def _get_foreground_window_info() -> dict:
    """Inspect current Win32 foreground window's hwnd, pid, process name, class name, and title."""
    u32 = facade_attr("_user32", _user32)
    try:
        hwnd = int(u32.GetForegroundWindow() or 0)
    except Exception:
        hwnd = 0
    return _get_window_info_by_hwnd(hwnd, include_focused_control=True)


def check_system_status() -> str:
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    gpu_info = "未知"
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=3,
        ).strip()
        parts = [x.strip() for x in out.split(",")]
        if len(parts) >= 5:
            gpu_info = f"{parts[0]} 温度 {parts[1]}°C，显存 {parts[2]}/{parts[3]} MB，负载 {parts[4]}%"
    except Exception:
        pass

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buff = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
    fg_title = buff.value or "桌面"

    return (
        f"CPU 占用 {cpu}%，内存已用 {round(mem.used / (1024**3), 1)}/{round(mem.total / (1024**3), 1)} GB ({mem.percent}%)；"
        f"显卡：{gpu_info}；当前前台窗口：「{fg_title}」。"
    )


# ---------------------------------------------------------------------------
# Phase 1 ②: Read-only Desktop Rhythm Probe (zero screenshots, strict privacy)
# ---------------------------------------------------------------------------

_DESKTOP_SHELL_CLASSES = frozenset(
    {
        "progman",
        "workerw",
        "shell_traywnd",
        "shell_secondarytraywnd",
        "windows.ui.core.corewindow",
        "dv2controlhost",
    }
)

_TERMINAL_PROCESSES = frozenset(
    {
        "cmd",
        "powershell",
        "pwsh",
        "wt",
        "windowsterminal",
        "conhost",
        "mintty",
        "bash",
        "sh",
        "ssh",
        "putty",
        "xshell",
        "finalshell",
        "mobaxterm",
        "tabby",
        "electerm",
        "alacritty",
        "wezterm-gui",
        "hyper",
    }
)

_PASSWORD_MANAGER_PROCESSES = frozenset(
    {
        "1password",
        "keepass",
        "keepass2",
        "keepassxc",
        "bitwarden",
        "enpass",
        "dashlane",
        "lastpass",
        "roboform",
        "nordpass",
        "protonpass",
    }
)

_REMOTE_DESKTOP_PROCESSES = frozenset(
    {
        "mstsc",
        "anydesk",
        "todesk",
        "sunloginclient",
        "teamviewer",
        "parsec",
        "rustdesk",
    }
)

_CODING_PROCESSES = frozenset(
    {
        "code",
        "code - insiders",
        "cursor",
        "antigravity",
        "windsurf",
        "pycharm64",
        "pycharm",
        "idea64",
        "clion64",
        "goland64",
        "webstorm64",
        "rider64",
        "devenv",
        "sublime_text",
        "notepad++",
        "vim",
        "nvim",
        "zed",
        "trae",
        "kiro",
    }
) | _TERMINAL_PROCESSES

_BROWSER_PROCESSES = frozenset(
    {
        "chrome",
        "msedge",
        "firefox",
        "brave",
        "opera",
        "vivaldi",
        "arc",
        "360chrome",
        "360se",
        "qqbrowser",
        "quark",
        "sogouexplorer",
        "chromium",
    }
)

_MEDIA_PROCESSES = frozenset(
    {
        "potplayer",
        "potplayermini64",
        "potplayermini",
        "vlc",
        "mpv",
        "bilibili",
        "cloudmusic",
        "qqmusic",
        "spotify",
        "kugou",
        "kuwo",
        "foobar2000",
        "iqiyi",
        "youku",
        "qqlive",
        "plex",
        "kodi",
        "musicbee",
        "wmplayer",
    }
)

_MEETING_PROCESSES = frozenset(
    {
        "zoom",
        "wemeetapp",
        "wemeet",
        "teams",
        "ms-teams",
        "dingtalk",
        "feishu",
        "lark",
        "obs64",
        "obs32",
        "webex",
        "skype",
        "voovmeeting",
        "classin",
        "tencentmeeting",
    }
)

_CHAT_PROCESSES = frozenset(
    {
        "wechat",
        "weixin",
        "qq",
        "tim",
        "telegram",
        "discord",
        "slack",
        "whatsapp",
        "signal",
    }
)

_GAME_PROCESSES = frozenset(
    {
        "steam",
        "steamwebhelper",
        "epicgameslauncher",
        "riotclientservices",
        "league of legends",
        "valorant",
        "genshinimpact",
        "yuanshen",
        "starrail",
        "bh3",
        "zenlesszonezero",
        "wuthering waves",
        "eldenring",
        "cyberpunk2077",
        "cs2",
        "dota2",
        "minecraft",
        "wegame",
        "battle.net",
        "overwatch",
        "apex",
        "tslgame",
        "naraka",
        "b1-win64-shipping",
    }
)

_DESKTOP_PROCESSES = frozenset(
    {
        "searchhost",
        "shellexperiencehost",
        "lockapp",
        "startmenuexperiencehost",
    }
)

_CATEGORY_SAFE_TITLES = {
    "coding": "代码与开发窗口",
    "browser": "浏览器窗口",
    "media": "影音娱乐窗口",
    "game": "游戏窗口",
    "meeting": "会议/录制窗口",
    "chat": "聊天窗口",
    "desktop": "桌面",
    "other": "常规窗口",
}

_MEETING_TITLE_RE = re.compile(
    r"(?:腾讯会议|飞书会议|钉钉会议|zoom\s+meeting|google\s+meet|microsoft\s+teams\s+meeting|webex)",
    re.IGNORECASE,
)

_SENSITIVE_TITLE_RE = re.compile(
    r"(?:"
    r"无痕|隐私模式|隐私窗口|隐身|incognito|inprivate|private\s+browsing|"
    r"网银|网上银行|手机银行|银行|招商银行|工商银行|建设银行|农业银行|中国银行|交通银行|中信银行|平安银行|"
    r"支付宝|微信支付|云闪付|收银台|转账|付款|支付密码|信用卡|证券|工资|社保|医保|身份证|病历|"
    r"钱包|交易所|币安|欧易|binance|okx|metamask|coinbase|paypal|stripe|ledger|trezor|私钥|助记词|mnemonic|"
    r"密码|口令|验证码|二次验证|2fa|authenticator|otp|passkey|keychain|vault|"
    r"登录|登入|sign\s*in|log\s*in|password|secrets?|api[\s_-]?key|access[\s_-]?token|bearer|cookie|jwt|oauth|"
    r"\.env\b|id_rsa|private[\s_-]?key|credentials|"
    r"[A-Za-z]:\\Users\\|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|"
    r"sk-[A-Za-z0-9_\-]{10,}|AIza[0-9A-Za-z_\-]{12,}|ghp_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9\-]{10,}|"
    r"(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{13,19}(?!\d)"
    r")",
    re.IGNORECASE,
)


def _norm_proc_stem(process_name: str) -> str:
    stem = os.path.basename(str(process_name or "").strip()).lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    return stem


def _get_user_idle_seconds() -> float:
    """Return seconds since last keyboard/mouse input (overflow-safe across 32-bit tick wrap & timer skew)."""
    u32 = facade_attr("_user32", _user32)
    k32 = facade_attr("_kernel32", _kernel32)
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not u32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        tick64 = int(k32.GetTickCount64())
        tick32 = tick64 & 0xFFFFFFFF
        delta_ms = (tick32 - int(lii.dwTime)) & 0xFFFFFFFF
        # Guard against Windows timer granularity skew where dwTime leads GetTickCount64 by 1-16ms
        if delta_ms >= 0x80000000:
            delta_ms = 0
        return round(delta_ms / 1000.0, 2)
    except Exception:
        return 0.0


def _is_window_fullscreen(hwnd: int, *, process_name: str = "") -> bool:
    """Return True if `hwnd` is truly fullscreen (excluding shell, cloaked, minimized & maximized desktop apps)."""
    if not hwnd:
        return False
    u32 = facade_attr("_user32", _user32)
    try:
        h = wintypes.HWND(int(hwnd))
        if u32.IsIconic(h):
            return False
        if _is_window_cloaked(int(hwnd)):
            return False
        cls_low = _get_window_class_name(int(hwnd)).strip().lower()
        if cls_low in _DESKTOP_SHELL_CLASSES:
            return False
        rect = wintypes.RECT()
        if not u32.GetWindowRect(h, ctypes.byref(rect)):
            return False
        win_w = int(rect.right) - int(rect.left)
        win_h = int(rect.bottom) - int(rect.top)
        if win_w <= 0 or win_h <= 0:
            return False
        hmon = u32.MonitorFromWindow(h, _MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return False
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False
        mon = mi.rcMonitor
        mon_w = int(mon.right) - int(mon.left)
        mon_h = int(mon.bottom) - int(mon.top)
        if mon_w <= 0 or mon_h <= 0:
            return False
        tol = 2
        covers_monitor = (
            int(rect.left) <= int(mon.left) + tol
            and int(rect.top) <= int(mon.top) + tol
            and int(rect.right) >= int(mon.right) - tol
            and int(rect.bottom) >= int(mon.bottom) - tol
        )
        if not covers_monitor:
            return False

        # On auto-hide taskbar or taskbar-less secondary monitors (where rcWork matches or
        # nearly matches rcMonitor), normal maximized desktop windows cover rcMonitor while
        # keeping IsZoomed=True and standard WS_CAPTION (0x00C00000).
        # True borderless/exclusive fullscreen apps (games, F11 browsers, video players) drop
        # WS_CAPTION or are not zoomed. When a monitor clearly reserves a visible taskbar
        # (rcWork inset > 24px from rcMonitor), any window still covering rcMonitor is truly
        # fullscreen over the taskbar.
        work = mi.rcWork
        work_w = int(work.right) - int(work.left)
        work_h = int(work.bottom) - int(work.top)
        has_visible_taskbar = (
            work_w > 0
            and work_h > 0
            and (
                (int(work.left) - int(mon.left)) > 24
                or (int(work.top) - int(mon.top)) > 24
                or (int(mon.right) - int(work.right)) > 24
                or (int(mon.bottom) - int(work.bottom)) > 24
            )
        )
        if has_visible_taskbar:
            return True

        is_zoomed = False
        has_caption = False
        try:
            is_zoomed = bool(u32.IsZoomed(h))
            style = int(u32.GetWindowLongW(h, _GWL_STYLE)) & 0xFFFFFFFF
            has_caption = (style & _WS_CAPTION) == _WS_CAPTION
        except Exception:
            is_zoomed = False
            has_caption = False

        if is_zoomed and has_caption:
            proc_stem = _norm_proc_stem(process_name)
            if not proc_stem:
                try:
                    pid_dw = wintypes.DWORD(0)
                    u32.GetWindowThreadProcessId(h, ctypes.byref(pid_dw))
                    pid_val = int(pid_dw.value)
                    if pid_val > 0:
                        proc_stem = _norm_proc_stem(_get_process_name_by_pid(pid_val))
                except Exception:
                    proc_stem = ""
            if proc_stem not in _GAME_PROCESSES:
                return False
        return True
    except Exception:
        return False


def classify_desktop_activity(
    process_name: str, window_class: str = "", title: str = ""
) -> str:
    """Coarse-grained desktop activity classification for proactive flow-state gating."""
    stem = _norm_proc_stem(process_name)
    cls_low = str(window_class or "").strip().lower()
    title_s = str(title or "").strip()

    if not stem or cls_low in _DESKTOP_SHELL_CLASSES or stem in _DESKTOP_PROCESSES:
        return "desktop"
    if stem == "explorer":
        return "desktop" if (not title_s or cls_low in _DESKTOP_SHELL_CLASSES) else "other"
    if stem in _GAME_PROCESSES:
        return "game"
    if stem in _MEETING_PROCESSES:
        return "meeting"
    if stem in _CODING_PROCESSES:
        return "coding"
    if stem in _BROWSER_PROCESSES:
        if _MEETING_TITLE_RE.search(title_s):
            return "meeting"
        return "browser"
    if stem in _MEDIA_PROCESSES:
        return "media"
    if stem in _CHAT_PROCESSES:
        return "chat"
    return "other"


def sanitize_window_title(
    process_name: str,
    title: str,
    activity_category: str = "other",
) -> str:
    """Strictly redact sensitive window titles (terminals, password managers, incognito, banking, long titles)."""
    stem = _norm_proc_stem(process_name)
    safe_fallback = _CATEGORY_SAFE_TITLES.get(activity_category, "常规窗口")

    if stem in _TERMINAL_PROCESSES:
        return "终端窗口"
    if stem in _PASSWORD_MANAGER_PROCESSES:
        return "密码管理工具"
    if stem in _REMOTE_DESKTOP_PROCESSES:
        return "远程桌面窗口"
    if activity_category == "chat" or stem in _CHAT_PROCESSES:
        return "聊天窗口"

    cleaned = " ".join(str(title or "").strip().split())
    if not cleaned:
        return safe_fallback
    if len(cleaned) > 120:
        return safe_fallback
    if _SENSITIVE_TITLE_RE.search(cleaned):
        return safe_fallback
    if len(cleaned) > 60:
        return cleaned[:59].rstrip() + "…"
    return cleaned


_SCREEN_PEEK_EXTRA_PRIVATE_PROCESSES = frozenset(
    {
        "wxwork",
        "dingtalk",
        "feishu",
        "lark",
        "teams",
        "ms-teams",
    }
)

_SCREEN_PEEK_LOCK_OR_SHELL_PROCESSES = frozenset(
    {
        "lockapp",
        "logonui",
        "consent",
        "credentialuibroker",
        "shellexperiencehost",
        "searchhost",
        "startmenuexperiencehost",
    }
)

_SCREEN_PEEK_SHELL_CLASSES = frozenset({"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"})


def check_screen_peek_privacy(fg_info: Optional[dict] = None) -> tuple[bool, str]:
    """Return `(allowed, reason_zh)` for on-demand active-window screen peek (fail-closed on unknown/sensitive apps)."""
    info = fg_info if isinstance(fg_info, dict) else _get_foreground_window_info()
    if not isinstance(info, dict) or not info:
        return False, "无法确认前台活动窗口，为防隐私泄漏已自动禁止截屏"
    proc = str(info.get("process") or "").strip()
    title = str(info.get("title") or "")
    win_cls = str(info.get("class") or "").strip()
    stem = _norm_proc_stem(proc)
    if not stem:
        return False, "无法识别前台窗口所属应用（可能为锁屏、UAC 或受保护进程），已自动禁止截屏"
    if stem in _SCREEN_PEEK_LOCK_OR_SHELL_PROCESSES or win_cls.lower() in _SCREEN_PEEK_SHELL_CLASSES:
        return False, "当前前台为系统锁屏、桌面外壳或权限确认界面，隐私护栏已自动禁止截屏"
    cat = classify_desktop_activity(proc, win_cls, title)

    if stem in _PASSWORD_MANAGER_PROCESSES:
        return False, "当前前台为密码管理工具，隐私护栏已自动禁止截屏"
    if stem in _TERMINAL_PROCESSES:
        return False, "当前前台为命令行终端窗口，为防环境变量/密钥外泄已自动禁止截屏"
    if stem in _REMOTE_DESKTOP_PROCESSES:
        return False, "当前前台为远程桌面窗口，隐私护栏已自动禁止截屏"
    if cat == "chat" or stem in _CHAT_PROCESSES or stem in _SCREEN_PEEK_EXTRA_PRIVATE_PROCESSES:
        return False, "当前前台为私密或工作聊天软件，为保护对话隐私已自动禁止截屏"
    if _SENSITIVE_TITLE_RE.search(title):
        return False, "当前窗口标题命中隐私/金融/凭证保护规则，已自动禁止截屏"
    return True, ""


def _get_active_window_clipped_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """Return `(left, top, right, bottom)` of `hwnd` clipped to its monitor bounds (supports negative coords)."""
    if not hwnd:
        return None
    u32 = facade_attr("_user32", _user32)
    try:
        h = wintypes.HWND(int(hwnd))
        if u32.IsIconic(h) or _is_window_cloaked(int(hwnd)):
            return None
        rect = wintypes.RECT()
        if not u32.GetWindowRect(h, ctypes.byref(rect)):
            return None
        left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        hmon = u32.MonitorFromWindow(h, _MONITOR_DEFAULTTONEAREST)
        if hmon:
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                mon = mi.rcMonitor
                left = max(left, int(mon.left))
                top = max(top, int(mon.top))
                right = min(right, int(mon.right))
                bottom = min(bottom, int(mon.bottom))
        if (right - left) < 80 or (bottom - top) < 80:
            return None
        return (left, top, right, bottom)
    except Exception:
        return None


class DesktopRhythmTracker:
    """Lightweight state machine tracking continuous work streaks, post-focus pauses, and returns from away.

    Works consistently under both dense (5s-60s) and sparse (15min) polling intervals.
    """

    ACTIVE_IDLE_THRESHOLD_SECONDS = 90.0
    MIN_WORK_STREAK_FOR_PAUSE_SECONDS = 25 * 60.0  # 25 min continuous work
    PAUSE_MIN_SECONDS = 2 * 60.0                   # 2 min pause start
    PAUSE_MAX_SECONDS = 8 * 60.0                   # 8 min pause end
    AWAY_MIN_SECONDS = 45 * 60.0                   # 45 min away
    RETURNED_HOLD_SECONDS = 5 * 60.0               # hold returned_from_away flag for 5 min
    MAX_STEP_DT_SECONDS = 1800.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_sample_mono: Optional[float] = None
        self._last_idle: float = 0.0
        self._active_streak_seconds: float = 0.0
        self._peak_active_streak_seconds: float = 0.0
        self._max_away_idle_seconds: float = 0.0
        self._returned_from_away_until: float = 0.0

    def reset(self) -> None:
        with self._lock:
            self._last_sample_mono = None
            self._last_idle = 0.0
            self._active_streak_seconds = 0.0
            self._peak_active_streak_seconds = 0.0
            self._max_away_idle_seconds = 0.0
            self._returned_from_away_until = 0.0

    def sample(
        self, idle_seconds: float, *, now_mono: Optional[float] = None
    ) -> dict:
        now = float(time.monotonic() if now_mono is None else now_mono)
        idle = max(0.0, float(idle_seconds or 0.0))

        with self._lock:
            if self._last_sample_mono is None:
                self._last_sample_mono = now
                self._last_idle = idle
                if idle >= self.AWAY_MIN_SECONDS:
                    self._max_away_idle_seconds = idle
                return {
                    "active_streak_seconds": 0.0,
                    "just_paused": False,
                    "returned_from_away": False,
                }

            dt = max(0.0, now - self._last_sample_mono)
            prev_idle = self._last_idle
            self._last_sample_mono = now
            self._last_idle = idle

            # Track away period (>= 45 min idle) and return transition
            if idle >= self.AWAY_MIN_SECONDS:
                self._max_away_idle_seconds = max(self._max_away_idle_seconds, idle)
                self._active_streak_seconds = 0.0
                self._peak_active_streak_seconds = 0.0
                self._returned_from_away_until = 0.0
            elif idle < self.ACTIVE_IDLE_THRESHOLD_SECONDS and self._max_away_idle_seconds >= self.AWAY_MIN_SECONDS:
                self._returned_from_away_until = now + self.RETURNED_HOLD_SECONDS
                self._max_away_idle_seconds = 0.0
                self._active_streak_seconds = 0.0
                self._peak_active_streak_seconds = 0.0
            else:
                step_dt = min(dt, self.MAX_STEP_DT_SECONDS)
                if idle < self.ACTIVE_IDLE_THRESHOLD_SECONDS:
                    # User is actively interacting; avoid double-crediting pause time on resume
                    if prev_idle < self.ACTIVE_IDLE_THRESHOLD_SECONDS:
                        active_delta = max(0.0, step_dt - max(0.0, idle - prev_idle))
                    else:
                        active_delta = max(0.0, step_dt - idle)
                    self._active_streak_seconds += active_delta
                    self._peak_active_streak_seconds = max(
                        self._peak_active_streak_seconds, self._active_streak_seconds
                    )
                elif idle <= self.PAUSE_MAX_SECONDS:
                    # Covers both 90-120s transition and 2-8 min pause window: credit active work
                    # that occurred between the previous sample and when the pause started.
                    if step_dt > 0.0 and idle < (prev_idle + step_dt - 1.0):
                        pre_pause_active = max(0.0, step_dt - idle)
                        self._active_streak_seconds += pre_pause_active
                        self._peak_active_streak_seconds = max(
                            self._peak_active_streak_seconds, self._active_streak_seconds
                        )
                        if self._max_away_idle_seconds >= self.AWAY_MIN_SECONDS:
                            self._max_away_idle_seconds = 0.0
                else:
                    # Idle exceeded 8 min pause window -> break work streak
                    self._active_streak_seconds = 0.0
                    self._peak_active_streak_seconds = 0.0

            just_paused = bool(
                self.PAUSE_MIN_SECONDS <= idle <= self.PAUSE_MAX_SECONDS
                and self._peak_active_streak_seconds >= self.MIN_WORK_STREAK_FOR_PAUSE_SECONDS
            )
            returned_from_away = bool(
                self._returned_from_away_until > 0.0 and now <= self._returned_from_away_until
            )
            return {
                "active_streak_seconds": round(self._active_streak_seconds, 1),
                "just_paused": just_paused,
                "returned_from_away": returned_from_away,
            }


_desktop_rhythm_tracker = DesktopRhythmTracker()


def get_desktop_rhythm_snapshot(
    tracker: Optional[DesktopRhythmTracker] = None,
) -> dict:
    """Collect a read-only, privacy-sanitized desktop rhythm snapshot (zero screenshots)."""
    idle_fn = facade_attr("_get_user_idle_seconds", _get_user_idle_seconds)
    fg_fn = facade_attr("_get_foreground_window_info", _get_foreground_window_info)
    fs_fn = facade_attr("_is_window_fullscreen", _is_window_fullscreen)

    idle_sec = float(idle_fn() or 0.0)
    fg_info: dict = {}
    try:
        if fg_fn is not _get_foreground_window_info:
            fg_info = dict(fg_fn() or {})
        else:
            # Read basic foreground window info without slow UIA child inspection
            u32 = facade_attr("_user32", _user32)
            hwnd = int(u32.GetForegroundWindow() or 0)
            fg_info = _get_window_info_by_hwnd(hwnd, include_focused_control=False)
    except Exception:
        fg_info = {}

    hwnd_val = int(fg_info.get("hwnd") or 0)
    proc_name = str(fg_info.get("process") or "")
    win_cls = str(fg_info.get("class") or "")
    raw_title = str(fg_info.get("title") or "")

    if hwnd_val:
        if fs_fn is _is_window_fullscreen:
            is_fs = bool(fs_fn(hwnd_val, process_name=proc_name))
        else:
            is_fs = bool(fs_fn(hwnd_val))
    else:
        is_fs = False
    category = classify_desktop_activity(proc_name, win_cls, raw_title)
    safe_title = sanitize_window_title(proc_name, raw_title, activity_category=category)

    trk = tracker if tracker is not None else _desktop_rhythm_tracker
    rhythm_state = trk.sample(idle_sec)

    return {
        "ok": True,
        "idle_seconds": round(idle_sec, 2),
        "is_fullscreen": is_fs,
        "activity_category": category,
        "process_name": _norm_proc_stem(proc_name),
        "safe_title": safe_title,
        "active_streak_seconds": rhythm_state["active_streak_seconds"],
        "just_paused": rhythm_state["just_paused"],
        "returned_from_away": rhythm_state["returned_from_away"],
    }

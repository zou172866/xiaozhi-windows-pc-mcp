"""Win32 API bindings, system media/volume/power control, and desktop window focus helpers (P2-20)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess
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


_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
_user32.GetGUIThreadInfo.restype = wintypes.BOOL
_GA_ROOT = 2

_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
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
    "windows.ui.composition.desktopwindowcontentbridge",
})


def _extract_uia_control_fields(ctrl) -> dict:
    """Extract control_type, name, automation_id, class, and ancestor_names from a UIA control."""
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
    parent = ctrl
    for _ in range(4):
        try:
            parent = parent.GetParentControl()
        except Exception:
            break
        if parent is None:
            break
        p_name = str(getattr(parent, "Name", "") or "").strip()
        p_cls = str(getattr(parent, "ClassName", "") or "").strip()
        p_aid = str(getattr(parent, "AutomationId", "") or "").strip()
        for part in (p_name, p_cls, p_aid):
            if part:
                anc_parts.append(part)
    if anc_parts:
        fields["ancestor_names"] = " | ".join(anc_parts)
    return fields


def _get_focused_control_info(fg_hwnd: int = 0) -> dict:
    """Inspect focused child control via Win32 GetGUIThreadInfo and UIA (used for CabinetWClass address bar guard)."""
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
        if not uia_ok and str(info.get("class") or "").strip().lower() in _XAML_BRIDGE_CLASSES:
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
    if include_focused_control and cls_name.lower() == "cabinetwclass":
        fc = _get_focused_control_info(hwnd)
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
    if info.get("class", "").lower() == "cabinetwclass":
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
            if not uia_ok and str(pt_ctrl.get("class") or "").strip().lower() in _XAML_BRIDGE_CLASSES:
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

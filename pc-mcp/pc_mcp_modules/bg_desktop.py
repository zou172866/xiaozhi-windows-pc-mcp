"""Non-intrusive background window capture & input via Win32 PrintWindow + PostMessage.

Design & Security invariants:
- Pure Win32 `ctypes` + `Pillow` (zero extra third-party dependencies, zero persistent PowerShell helpers).
- Never moves the user's physical mouse cursor, never steals foreground focus, never touches the clipboard.
- Reuses full L2 security boundaries: host `full` mode + emergency stop check + terminal/Run/Explorer
  blocklist (`_check_foreground_input_guard` + strict fail-closed on `CabinetWClass` / `#32770`)
  + administrator/elevated process detection (`ELEVATED` fail-closed) + `l2_audit.jsonl`.
- Captures before/after 16x16 grayscale fingerprints so silent no-op delivery (e.g. games reading
  raw hardware input) is explicitly reported instead of silently pretending success.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time
from typing import Any, Optional, Tuple

from PIL import Image

from . import _gui_lock, facade_attr
from .security import (
    _check_foreground_input_guard,
    _check_hotkey_blacklist,
    _check_permission,
    _get_audit_extra_secrets,
    _parse_and_normalize_hotkey,
    _validate_hotkey_keys,
    _write_l2_audit_log,
    is_emergency_stop_active,
    redact_secrets,
)
from .vision import (
    _call_vision_llm,
    _compute_frame_fingerprint,
    _frames_changed,
    _parse_coord_float,
    _parse_json_obj,
    _save_feedback_image,
)
from .winapi import (
    _GUITHREADINFO,
    _get_window_info_by_hwnd,
    _kernel32,
    _list_top_windows,
    _user32,
)

# Win32 constants
_PW_CLIENTONLY = 0x00000001
_PW_RENDERFULLCONTENT = 0x00000002
_SW_SHOWNOACTIVATE = 4
_CWP_SKIPINVISIBLE = 0x0001
_CWP_SKIPDISABLED = 0x0002

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_CHAR = 0x0102
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105

_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_MOUSEWHEEL = 0x020A

_MK_LBUTTON = 0x0001
_MK_RBUTTON = 0x0002

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_DIB_RGB_COLORS = 0
_BI_RGB = 0

_BG_FORBIDDEN_CLASSES = frozenset({"cabinetwclass", "#32770"})

# Lazy ctypes bindings (Windows-only, safe to import on any platform for unit tests)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True) if os.name == "nt" else None
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True) if os.name == "nt" else None


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


if os.name == "nt":
    try:
        _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        _user32.GetClientRect.restype = wintypes.BOOL
        _user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        _user32.PrintWindow.restype = wintypes.BOOL
        _user32.GetDC.argtypes = [wintypes.HWND]
        _user32.GetDC.restype = wintypes.HDC
        _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        _user32.ReleaseDC.restype = ctypes.c_int
        _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        _user32.PostMessageW.restype = wintypes.BOOL
        _user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, wintypes.UINT]
        _user32.ChildWindowFromPointEx.restype = wintypes.HWND
        _user32.MapWindowPoints.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(wintypes.POINT), wintypes.UINT]
        _user32.MapWindowPoints.restype = ctypes.c_int
        _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        _user32.ClientToScreen.restype = wintypes.BOOL
        _user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
        _user32.GetGUIThreadInfo.restype = wintypes.BOOL
        _user32.GetKeyboardState.argtypes = [ctypes.c_void_p]
        _user32.GetKeyboardState.restype = wintypes.BOOL
        _user32.SetKeyboardState.argtypes = [ctypes.c_void_p]
        _user32.SetKeyboardState.restype = wintypes.BOOL
        _user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        _user32.MapVirtualKeyW.restype = wintypes.UINT
    except Exception:
        pass
    if _gdi32 is not None:
        try:
            _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
            _gdi32.CreateCompatibleDC.restype = wintypes.HDC
            _gdi32.DeleteDC.argtypes = [wintypes.HDC]
            _gdi32.DeleteDC.restype = wintypes.BOOL
            _gdi32.CreateDIBSection.argtypes = [
                wintypes.HDC,
                ctypes.POINTER(_BITMAPINFO),
                wintypes.UINT,
                ctypes.POINTER(ctypes.c_void_p),
                wintypes.HANDLE,
                wintypes.DWORD,
            ]
            _gdi32.CreateDIBSection.restype = wintypes.HBITMAP
            _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
            _gdi32.SelectObject.restype = wintypes.HGDIOBJ
            _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
            _gdi32.DeleteObject.restype = wintypes.BOOL
        except Exception:
            pass
    if _advapi32 is not None:
        try:
            _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
            _advapi32.OpenProcessToken.restype = wintypes.BOOL
        except Exception:
            pass


_VK_MAP: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "del": 0x2E,
    "delete": 0x2E,
}
for _i in range(1, 13):
    _VK_MAP[f"f{_i}"] = 0x6F + _i
for _ch in "0123456789":
    _VK_MAP[_ch] = ord(_ch)
for _ch in "abcdefghijklmnopqrstuvwxyz":
    _VK_MAP[_ch] = ord(_ch.upper())

_MODIFIER_LEFT_VK = {
    0x11: 0xA2,  # VK_CONTROL -> VK_LCONTROL
    0x10: 0xA0,  # VK_SHIFT   -> VK_LSHIFT
    0x12: 0xA4,  # VK_MENU    -> VK_LMENU
}


def _pack_xy_lparam(x: int, y: int) -> int:
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def _key_lparam(vk: int, *, key_up: bool = False) -> int:
    scan = 0
    try:
        scan = int(_user32.MapVirtualKeyW(int(vk), 0)) & 0xFF
    except Exception:
        scan = 0
    bits = 1 | (scan << 16)
    if (0x21 <= vk <= 0x28) or vk in (0x2D, 0x2E):
        bits |= 1 << 24
    if key_up:
        bits |= 0xC0000000
    return bits


def is_process_elevated(pid: int) -> bool:
    """Return True if target PID runs at higher privilege (unopenable process/token from standard user)."""
    if os.name != "nt" or not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return False
    hproc = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(int(pid)))
    if not hproc:
        return True
    try:
        if _advapi32 is None:
            return False
        htok = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(hproc, _TOKEN_QUERY, ctypes.byref(htok)):
            return True
        try:
            _kernel32.CloseHandle(htok)
        except Exception:
            pass
        return False
    finally:
        try:
            _kernel32.CloseHandle(hproc)
        except Exception:
            pass


def check_bg_window_guard(hwnd: int) -> Tuple[Optional[dict], Optional[str]]:
    """Validate that `hwnd` is alive, non-sensitive, non-Explorer/dialog, and non-elevated."""
    u32 = facade_attr("_user32", _user32)
    win_info_fn = facade_attr("_get_window_info_by_hwnd", _get_window_info_by_hwnd)
    fg_guard_fn = facade_attr("_check_foreground_input_guard", _check_foreground_input_guard)
    elev_fn = facade_attr("is_process_elevated", is_process_elevated)

    if not hwnd or int(hwnd) <= 0:
        return None, "无效的目标窗口句柄。"
    try:
        if not u32.IsWindow(wintypes.HWND(int(hwnd))):
            return None, "目标窗口已关闭或不存在。"
    except Exception:
        return None, "无法验证目标窗口状态。"

    info = win_info_fn(int(hwnd))
    if not isinstance(info, dict) or not info:
        return None, "安全护栏已拦截：无法识别后台目标窗口身份（fail-closed）。"

    cls_low = str(info.get("class") or "").strip().lower()
    if cls_low in _BG_FORBIDDEN_CLASSES:
        return info, (
            f"安全护栏已拦截：后台操控模式禁止向资源管理器或通用文件对话框（{info.get('class')}）投递消息。"
        )

    err = fg_guard_fn(info)
    if err:
        return info, err

    pid = int(info.get("pid") or 0)
    if pid > 0 and elev_fn(pid):
        title = str(info.get("title") or info.get("process") or "该窗口")
        return info, (
            f"ELEVATED: 窗口「{title}」以管理员权限运行，普通权限后台模式无法截屏或投递消息。"
            "如需操作它，请改用前台 pc_ui_action。"
        )
    return info, None


def find_bg_window(window_keyword: str) -> Optional[dict]:
    """Find best-matching visible top-level window by title or process name without changing focus."""
    kw = (window_keyword or "").strip().lower()
    if not kw:
        return None
    list_fn = facade_attr("_list_top_windows", _list_top_windows)
    wins = list_fn() or []

    # 1. Exact title or prefix match
    for w in wins:
        t = str(w.get("title") or "").strip().lower()
        if t == kw or t.startswith(kw):
            return dict(w)
    # 2. Substring in title
    for w in wins:
        t = str(w.get("title") or "").strip().lower()
        if kw in t:
            return dict(w)
    # 3. Process name match (with or without .exe); _list_top_windows uses key "proc"
    for w in wins:
        p = str(w.get("proc") or w.get("process") or "").strip().lower()
        stem = p[:-4] if p.endswith(".exe") else p
        if p == kw or stem == kw or (len(kw) >= 2 and kw in stem):
            return dict(w)
    return None


def restore_window_no_activate(hwnd: int) -> bool:
    """Restore a minimized window in-place without activating or stealing user focus."""
    u32 = facade_attr("_user32", _user32)
    tmod = facade_attr("time", time)
    try:
        if u32.IsIconic(wintypes.HWND(int(hwnd))):
            u32.ShowWindow(wintypes.HWND(int(hwnd)), _SW_SHOWNOACTIVATE)
            tmod.sleep(0.22)
        return not bool(u32.IsIconic(wintypes.HWND(int(hwnd))))
    except Exception:
        return False


def capture_window_client(
    hwnd: int,
    *,
    max_edge: int = 1280,
) -> Tuple[Image.Image, Tuple[int, int]]:
    """Capture a window's client area via PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT).

    Returns `(rgb_image, (client_w, client_h))` where `rgb_image` is scaled so its longest edge <= `max_edge`.
    """
    if os.name != "nt" or _gdi32 is None:
        raise RuntimeError("PrintWindow 仅支持 Windows 宿主机环境")
    u32 = facade_attr("_user32", _user32)
    h = wintypes.HWND(int(hwnd))
    if not u32.IsWindow(h):
        raise RuntimeError("目标窗口已关闭")
    if u32.IsIconic(h):
        restore_window_no_activate(int(hwnd))

    rect = wintypes.RECT()
    if not u32.GetClientRect(h, ctypes.byref(rect)):
        raise RuntimeError("GetClientRect 失败")
    cw = int(rect.right - rect.left)
    ch = int(rect.bottom - rect.top)
    if cw <= 0 or ch <= 0:
        raise RuntimeError("目标窗口客户区尺寸为空（可能已最小化或隐藏）")

    hdc_win = u32.GetDC(h)
    if not hdc_win:
        raise RuntimeError("GetDC 失败")
    hdc_mem = None
    hbmp = None
    old_obj = None
    try:
        hdc_mem = _gdi32.CreateCompatibleDC(hdc_win)
        if not hdc_mem:
            raise RuntimeError("CreateCompatibleDC 失败")
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = cw
        bmi.bmiHeader.biHeight = -ch  # top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = _BI_RGB
        bits_ptr = ctypes.c_void_p()
        hbmp = _gdi32.CreateDIBSection(
            hdc_mem,
            ctypes.byref(bmi),
            _DIB_RGB_COLORS,
            ctypes.byref(bits_ptr),
            None,
            0,
        )
        if not hbmp or not bits_ptr.value:
            raise RuntimeError("CreateDIBSection 失败")
        old_obj = _gdi32.SelectObject(hdc_mem, hbmp)
        ok = bool(u32.PrintWindow(h, hdc_mem, _PW_CLIENTONLY | _PW_RENDERFULLCONTENT))
        if not ok:
            ok = bool(u32.PrintWindow(h, hdc_mem, _PW_CLIENTONLY))
        if not ok:
            raise RuntimeError("PrintWindow 截取失败（窗口可能为管理员进程或受版权保护）")
        buf_size = cw * ch * 4
        raw_bytes = ctypes.string_at(bits_ptr.value, buf_size)
        img = Image.frombuffer("RGBA", (cw, ch), raw_bytes, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        if hdc_mem and old_obj:
            try:
                _gdi32.SelectObject(hdc_mem, old_obj)
            except Exception:
                pass
        if hbmp:
            try:
                _gdi32.DeleteObject(hbmp)
            except Exception:
                pass
        if hdc_mem:
            try:
                _gdi32.DeleteDC(hdc_mem)
            except Exception:
                pass
        try:
            u32.ReleaseDC(h, hdc_win)
        except Exception:
            pass

    long_edge = max(cw, ch)
    if max_edge > 0 and long_edge > max_edge:
        scale = float(max_edge) / float(long_edge)
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        img = img.resize((max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))), resample)
    return img, (cw, ch)


def _resolve_child_target(hwnd: int, client_x: int, client_y: int) -> Tuple[int, int, int]:
    """Walk nested child windows at `(client_x, client_y)` and map client coordinates."""
    u32 = facade_attr("_user32", _user32)
    cur = int(hwnd)
    pt = wintypes.POINT(int(client_x), int(client_y))
    for _ in range(12):
        try:
            child = int(
                u32.ChildWindowFromPointEx(
                    wintypes.HWND(cur),
                    pt,
                    _CWP_SKIPINVISIBLE | _CWP_SKIPDISABLED,
                )
                or 0
            )
        except Exception:
            break
        if not child or child == cur:
            break
        u32.MapWindowPoints(wintypes.HWND(cur), wintypes.HWND(child), ctypes.byref(pt), 1)
        cur = child
    return cur, int(pt.x), int(pt.y)


def _resolve_thread_focus(hwnd: int) -> Tuple[int, int]:
    """Find the target window thread's internal keyboard focus handle while running in background."""
    u32 = facade_attr("_user32", _user32)
    pid = wintypes.DWORD(0)
    tid = int(u32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid)) or 0)
    if tid:
        gti = _GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
        try:
            if u32.GetGUIThreadInfo(wintypes.DWORD(tid), ctypes.byref(gti)) and gti.hwndFocus:
                return int(gti.hwndFocus), tid
        except Exception:
            pass
    return int(hwnd), tid


def post_bg_click(hwnd: int, client_x: int, client_y: int, *, click_kind: str = "click") -> int:
    """Post mouse click to child window at `(client_x, client_y)` and return resolved child HWND."""
    u32 = facade_attr("_user32", _user32)
    tmod = facade_attr("time", time)
    target, tx, ty = _resolve_child_target(hwnd, client_x, client_y)
    h = wintypes.HWND(target)
    lp = _pack_xy_lparam(tx, ty)
    u32.PostMessageW(h, _WM_MOUSEMOVE, 0, lp)
    tmod.sleep(0.02)
    if click_kind == "right_click":
        u32.PostMessageW(h, _WM_RBUTTONDOWN, _MK_RBUTTON, lp)
        tmod.sleep(0.04)
        u32.PostMessageW(h, _WM_RBUTTONUP, 0, lp)
    elif click_kind == "double_click":
        u32.PostMessageW(h, _WM_LBUTTONDOWN, _MK_LBUTTON, lp)
        u32.PostMessageW(h, _WM_LBUTTONUP, 0, lp)
        tmod.sleep(0.04)
        u32.PostMessageW(h, _WM_LBUTTONDBLCLK, _MK_LBUTTON, lp)
        u32.PostMessageW(h, _WM_LBUTTONUP, 0, lp)
    else:
        u32.PostMessageW(h, _WM_LBUTTONDOWN, _MK_LBUTTON, lp)
        tmod.sleep(0.04)
        u32.PostMessageW(h, _WM_LBUTTONUP, 0, lp)
    return target


def post_bg_type(hwnd: int, text: str, *, press_enter: bool = False, target_hwnd: int = 0) -> None:
    u32 = facade_attr("_user32", _user32)
    tmod = facade_attr("time", time)
    if target_hwnd and int(target_hwnd) > 0:
        target = int(target_hwnd)
    else:
        target, _tid = _resolve_thread_focus(hwnd)
    h = wintypes.HWND(target)
    for ch in str(text or ""):
        if ch == "\r":
            continue
        if ch == "\n":
            u32.PostMessageW(h, _WM_KEYDOWN, 0x0D, _key_lparam(0x0D, key_up=False))
            u32.PostMessageW(h, _WM_KEYUP, 0x0D, _key_lparam(0x0D, key_up=True))
        else:
            raw_u16 = ch.encode("utf-16-le")
            for idx in range(0, len(raw_u16), 2):
                code_unit = int.from_bytes(raw_u16[idx : idx + 2], "little")
                u32.PostMessageW(h, _WM_CHAR, code_unit, 1)
        tmod.sleep(0.004)
    if press_enter:
        tmod.sleep(0.03)
        u32.PostMessageW(h, _WM_KEYDOWN, 0x0D, _key_lparam(0x0D, key_up=False))
        u32.PostMessageW(h, _WM_KEYUP, 0x0D, _key_lparam(0x0D, key_up=True))


def post_bg_hotkey(hwnd: int, canon_keys: list[str]) -> None:
    u32 = facade_attr("_user32", _user32)
    k32 = facade_attr("_kernel32", _kernel32)
    tmod = facade_attr("time", time)

    modifiers: list[int] = []
    main_vk: Optional[int] = None
    for k in canon_keys:
        vk = _VK_MAP.get(k.lower())
        if vk is None:
            raise ValueError(f"后台快捷键不支持按键: {k}")
        if vk in (0x10, 0x11, 0x12):
            if vk not in modifiers:
                modifiers.append(vk)
        else:
            main_vk = vk
    if main_vk is None:
        if not modifiers:
            raise ValueError("未指定有效按键")
        main_vk = modifiers.pop()

    target, target_tid = _resolve_thread_focus(hwnd)
    h = wintypes.HWND(target)
    use_alt = 0x12 in modifiers
    down_msg = _WM_SYSKEYDOWN if use_alt else _WM_KEYDOWN
    up_msg = _WM_SYSKEYUP if use_alt else _WM_KEYUP

    cur_tid = int(k32.GetCurrentThreadId()) if os.name == "nt" else 0
    attached = False
    saved_state = (ctypes.c_ubyte * 256)()
    state_saved = False
    try:
        if modifiers and target_tid and cur_tid and target_tid != cur_tid:
            attached = bool(u32.AttachThreadInput(wintypes.DWORD(cur_tid), wintypes.DWORD(target_tid), True))
            if attached and u32.GetKeyboardState(saved_state):
                state_saved = True
                mod_state = (ctypes.c_ubyte * 256)(*saved_state)
                for mvk in modifiers:
                    mod_state[mvk] = 0x80
                    lvk = _MODIFIER_LEFT_VK.get(mvk)
                    if lvk:
                        mod_state[lvk] = 0x80
                u32.SetKeyboardState(mod_state)

        for mvk in modifiers:
            u32.PostMessageW(h, _WM_SYSKEYDOWN if mvk == 0x12 else _WM_KEYDOWN, mvk, _key_lparam(mvk, key_up=False))
        u32.PostMessageW(h, down_msg, main_vk, _key_lparam(main_vk, key_up=False))
        tmod.sleep(0.03)
        u32.PostMessageW(h, up_msg, main_vk, _key_lparam(main_vk, key_up=True))
        for mvk in reversed(modifiers):
            u32.PostMessageW(h, _WM_SYSKEYUP if mvk == 0x12 else _WM_KEYUP, mvk, _key_lparam(mvk, key_up=True))
    finally:
        if state_saved:
            try:
                u32.SetKeyboardState(saved_state)
            except Exception:
                pass
        if attached:
            try:
                u32.AttachThreadInput(wintypes.DWORD(cur_tid), wintypes.DWORD(target_tid), False)
            except Exception:
                pass


def post_bg_scroll(hwnd: int, client_x: int, client_y: int, scroll_clicks: int) -> None:
    u32 = facade_attr("_user32", _user32)
    clamped_clicks = max(-50, min(50, int(scroll_clicks)))
    target, _tx, _ty = _resolve_child_target(hwnd, client_x, client_y)
    pt = wintypes.POINT(int(client_x), int(client_y))
    u32.ClientToScreen(wintypes.HWND(int(hwnd)), ctypes.byref(pt))
    delta = (clamped_clicks * 120) & 0xFFFF
    wp = delta << 16
    lp = _pack_xy_lparam(pt.x, pt.y)
    u32.PostMessageW(wintypes.HWND(target), _WM_MOUSEWHEEL, wp, lp)


def _locate_in_window_image(
    img: Image.Image,
    client_size: Tuple[int, int],
    element_name: str,
) -> Optional[Tuple[int, int, str]]:
    """Locate `element_name` inside a captured window client image via VLM (returns client pixel coords)."""
    call_vlm = facade_attr("_call_vision_llm", _call_vision_llm)
    prompt = (
        f"这是一张单个应用程序窗口客户区的截图，请精确定位目标：「{element_name.strip()}」。\n"
        '请严格返回 JSON 对象（不要输出额外文字）：{"found": true, "label": "简短名", '
        '"x_min": 0~1000, "y_min": 0~1000, "x_max": 0~1000, "y_max": 0~1000}。'
    )
    obj = _parse_json_obj(call_vlm(prompt, img, json_mode=True))
    if not obj or not obj.get("found"):
        return None
    x1 = _parse_coord_float(obj.get("x_min"), -1.0)
    y1 = _parse_coord_float(obj.get("y_min"), -1.0)
    x2 = _parse_coord_float(obj.get("x_max"), -1.0)
    y2 = _parse_coord_float(obj.get("y_max"), -1.0)
    if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
        return None
    cw, ch = int(client_size[0]), int(client_size[1])
    cx = max(2, min(cw - 2, int(((x1 + x2) / 2000.0) * cw)))
    cy = max(2, min(ch - 2, int(((y1 + y2) / 2000.0) * ch)))
    label = str(obj.get("label") or element_name).strip()[:32]
    return cx, cy, label


def _execute_bg_window_action_result(
    *,
    action: str,
    window_keyword: str = "",
    element_name: str = "",
    x: int = -1,
    y: int = -1,
    text: str = "",
    press_enter: bool = False,
    hotkey: str = "",
    scroll_clicks: int = -3,
    question: str = "",
    target_hwnd: int = 0,
) -> Tuple[bool, str]:
    """Structured `(ok, message)` entry for background window actions and locked-HWND procedure replay."""
    act = (action or "").strip().lower()
    perm_fn = facade_attr("_check_permission", _check_permission)
    estop_fn = facade_attr("is_emergency_stop_active", is_emergency_stop_active)
    audit_fn = facade_attr("_write_l2_audit_log", _write_l2_audit_log)
    tmod = facade_attr("time", time)

    perm_err = perm_fn("L2")
    if perm_err:
        audit_fn(
            tool="pc_bg_window_action",
            action=act,
            outcome="blocked",
            reason="emergency_stop" if estop_fn() else "mode_blocked",
            target=window_keyword,
            detail=perm_err,
        )
        return False, perm_err
    if estop_fn():
        msg = "安全急停已触发（Ctrl+Alt+Pause），已拒绝后台窗口操控。"
        audit_fn(
            tool="pc_bg_window_action",
            action=act,
            outcome="blocked",
            reason="emergency_stop",
            target=window_keyword,
            detail=msg,
        )
        return False, msg

    if act == "list_windows":
        list_fn = facade_attr("_list_top_windows", _list_top_windows)
        wins = list_fn() or []
        lines = [
            f"- {w.get('title', '')[:60]} ({w.get('proc') or w.get('process') or ''})"
            for w in wins[:25]
            if w.get("title")
        ]
        audit_fn(
            tool="pc_bg_window_action",
            action="list_windows",
            outcome="allowed",
            reason="ok",
            detail=f"count={len(lines)}",
        )
        if not lines:
            return True, "当前没有可列举的可见桌面窗口。"
        return True, f"当前可见窗口（共 {len(lines)} 个，后台模式不抢焦点）：\n" + "\n".join(lines)

    win: Optional[dict] = None
    if target_hwnd and int(target_hwnd) > 0:
        hwnd = int(target_hwnd)
    else:
        win = find_bg_window(window_keyword)
        if not win:
            audit_fn(
                tool="pc_bg_window_action",
                action=act,
                outcome="blocked",
                reason="window_not_found",
                target=window_keyword,
            )
            return False, f"未找到标题或进程匹配「{window_keyword}」的窗口，可先调 action='list_windows' 查看可用窗口。"
        hwnd = int(win["hwnd"])

    win_info, guard_err = check_bg_window_guard(hwnd)
    if guard_err:
        audit_fn(
            tool="pc_bg_window_action",
            action=act,
            outcome="blocked",
            reason="bg_window_blocked",
            fg_info=win_info,
            target=f"{window_keyword} (hwnd={hwnd})",
            detail=guard_err,
        )
        return False, guard_err

    win_label = str((win_info or win or {}).get("title") or window_keyword or f"hwnd:{hwnd}")[:48]
    capture_fn = facade_attr("capture_window_client", capture_window_client)

    try:
        if act == "inspect":
            with _gui_lock:
                img, (cw, ch) = capture_fn(hwnd)
            _save_feedback_image(img)
            call_vlm = facade_attr("_call_vision_llm", _call_vision_llm)
            q = (question or "").strip() or "请简明描述这个后台窗口当前显示的主要界面内容和可点击按钮。"
            ans = call_vlm(q, img, json_mode=False) or "视觉模型未能识别该窗口画面。"
            audit_fn(
                tool="pc_bg_window_action",
                action="inspect",
                outcome="allowed",
                reason="ok",
                fg_info=win_info,
                target=f"{win_label} ({cw}x{ch})",
            )
            return True, f"后台窗口「{win_label}」（客户区 {cw}×{ch}，未抢焦点）：\n{ans}"

        if act in ("click", "double_click", "right_click"):
            with _gui_lock:
                before_img, (cw, ch) = capture_fn(hwnd)
            fp_before = _compute_frame_fingerprint(before_img)
            src_desc = ""
            cx, cy = int(x), int(y)
            if element_name.strip():
                located = _locate_in_window_image(before_img, (cw, ch), element_name)
                if not located:
                    audit_fn(
                        tool="pc_bg_window_action",
                        action=act,
                        outcome="blocked",
                        reason="target_not_found",
                        fg_info=win_info,
                        target=f"{win_label}::{element_name}",
                    )
                    return False, f"未能在后台窗口「{win_label}」画面中找到「{element_name}」。"
                cx, cy, lbl = located
                src_desc = f"「{lbl}」"
            elif not (0 <= cx < cw and 0 <= cy < ch):
                return False, f"后台 click 需要提供 element_name 或有效客户区坐标 (0..{cw-1}, 0..{ch-1})。"

            with _gui_lock:
                if estop_fn():
                    return False, "安全急停已触发，已中止后台点击。"
                _, re_err = check_bg_window_guard(hwnd)
                if re_err:
                    return False, re_err
                post_bg_click(hwnd, cx, cy, click_kind=act)
            tmod.sleep(0.2)
            changed: Optional[bool] = None
            try:
                with _gui_lock:
                    after_img, (acw, ach) = capture_fn(hwnd)
                fp_after = _compute_frame_fingerprint(after_img)
                changed = _frames_changed(fp_before, fp_after)
                sx = int(round(cx * (after_img.width / float(max(1, acw)))))
                sy = int(round(cy * (after_img.height / float(max(1, ach)))))
                _save_feedback_image(after_img, mark_xy=(sx, sy))
            except Exception:
                pass
            audit_fn(
                tool="pc_bg_window_action",
                action=act,
                outcome="allowed",
                reason="ok",
                fg_info=win_info,
                target=f"{win_label}::{src_desc or f'({cx},{cy})'}",
                detail=f"client=({cx},{cy})/{cw}x{ch} changed={changed}",
            )
            verb = {"click": "点击", "double_click": "双击", "right_click": "右键点击"}[act]
            hint = ""
            if changed is False:
                hint = "（⚠️ 后台投递后窗口画面无变化：部分游戏或直读硬件输入程序不接收后台消息，请改用前台 pc_ui_action）"
            elif changed is True:
                hint = "（窗口画面已变化，红圈反馈图已更新）"
            return True, f"已在后台静默{verb}窗口「{win_label}」{src_desc} 客户区坐标 ({cx}, {cy})（未移动鼠标/未抢焦点）。{hint}"

        if act == "type_text":
            with _gui_lock:
                before_img, (cw, ch) = capture_fn(hwnd)
            fp_before = _compute_frame_fingerprint(before_img)
            click_pt: Optional[Tuple[int, int]] = None
            if element_name.strip():
                located = _locate_in_window_image(before_img, (cw, ch), element_name)
                if not located:
                    audit_fn(
                        tool="pc_bg_window_action",
                        action="type_text",
                        outcome="blocked",
                        reason="target_not_found",
                        fg_info=win_info,
                        target=f"{win_label}::{element_name}",
                        text=text,
                    )
                    return False, f"未能在后台窗口「{win_label}」定位到输入框「{element_name}」，已中止输入。"
                click_pt = (located[0], located[1])
            elif 0 <= x < cw and 0 <= y < ch:
                click_pt = (int(x), int(y))

            with _gui_lock:
                if estop_fn():
                    return False, "安全急停已触发，已中止后台输入。"
                _, re_err = check_bg_window_guard(hwnd)
                if re_err:
                    return False, re_err
                clicked_child = 0
                if click_pt is not None:
                    clicked_child = post_bg_click(hwnd, click_pt[0], click_pt[1], click_kind="click")
                    tmod.sleep(0.08)
                post_bg_type(hwnd, text, press_enter=press_enter, target_hwnd=clicked_child)

            tmod.sleep(0.18)
            changed = None
            try:
                with _gui_lock:
                    after_img, _ = capture_fn(hwnd)
                changed = _frames_changed(fp_before, _compute_frame_fingerprint(after_img))
                _save_feedback_image(after_img)
            except Exception:
                pass

            audit_fn(
                tool="pc_bg_window_action",
                action="type_text",
                outcome="allowed",
                reason="ok",
                fg_info=win_info,
                target=win_label,
                text=text,
                press_enter=press_enter,
                detail=f"changed={changed}",
            )
            safe_preview = redact_secrets(text[:30], _get_audit_extra_secrets())
            hint = "（⚠️ 窗口画面无变化，如未生效请改用前台 pc_ui_action）" if changed is False else ""
            return True, (
                f"已在后台向窗口「{win_label}」静默输入：「{safe_preview}」"
                + (" 并发送回车。" if press_enter else "。")
                + hint
            )

        if act == "hotkey":
            exec_keys, canon_keys = _parse_and_normalize_hotkey(hotkey)
            if not exec_keys:
                return False, "未提供有效的 hotkey。"
            canon_str = "+".join(canon_keys)
            hk_err = _check_hotkey_blacklist(canon_keys, hotkey)
            if hk_err:
                audit_fn(
                    tool="pc_bg_window_action",
                    action="hotkey",
                    outcome="blocked",
                    reason="hotkey_blacklisted",
                    fg_info=win_info,
                    hotkey=canon_str,
                    detail=hk_err,
                )
                return False, hk_err
            inv = _validate_hotkey_keys(exec_keys)
            if inv:
                return False, f"安全护栏已拦截：快捷键包含未识别按键（{', '.join(inv)}）。"
            with _gui_lock:
                if estop_fn():
                    return False, "安全急停已触发，已中止后台快捷键。"
                _, re_err = check_bg_window_guard(hwnd)
                if re_err:
                    return False, re_err
                post_bg_hotkey(hwnd, canon_keys)
            audit_fn(
                tool="pc_bg_window_action",
                action="hotkey",
                outcome="allowed",
                reason="ok",
                fg_info=win_info,
                target=win_label,
                hotkey=canon_str,
            )
            return True, f"已向后台窗口「{win_label}」发送快捷键：{canon_str}。"

        if act == "scroll":
            clamped_clicks = max(-50, min(50, int(scroll_clicks)))
            with _gui_lock:
                if estop_fn():
                    return False, "安全急停已触发，已中止后台滚动。"
                _, re_err = check_bg_window_guard(hwnd)
                if re_err:
                    return False, re_err
                _, (cw, ch) = capture_fn(hwnd)
                if estop_fn():
                    return False, "安全急停已触发，已中止后台滚动。"
                sx = int(x) if 0 <= x < cw else cw // 2
                sy = int(y) if 0 <= y < ch else ch // 2
                post_bg_scroll(hwnd, sx, sy, clamped_clicks)
            audit_fn(
                tool="pc_bg_window_action",
                action="scroll",
                outcome="allowed",
                reason="ok",
                fg_info=win_info,
                target=f"{win_label} ({sx},{sy}) clicks={clamped_clicks}",
            )
            return True, f"已在后台窗口「{win_label}」{'向下' if clamped_clicks < 0 else '向上'}滚动。"

        return False, f"未知的后台窗口操作: {action}（支持 list_windows / inspect / click / double_click / right_click / type_text / hotkey / scroll）"
    except Exception as exc:
        err_msg = str(exc)
        audit_fn(
            tool="pc_bg_window_action",
            action=act,
            outcome="blocked",
            reason="bg_action_error",
            fg_info=win_info,
            target=win_label,
            detail=err_msg[:180],
        )
        return False, f"后台窗口操作失败：{err_msg}"


def execute_bg_window_action(
    *,
    action: str,
    window_keyword: str = "",
    element_name: str = "",
    x: int = -1,
    y: int = -1,
    text: str = "",
    press_enter: bool = False,
    hotkey: str = "",
    scroll_clicks: int = -3,
    question: str = "",
) -> str:
    """Unified guarded entry for `pc_bg_window_action` MCP tool."""
    _, msg = _execute_bg_window_action_result(
        action=action,
        window_keyword=window_keyword,
        element_name=element_name,
        x=x,
        y=y,
        text=text,
        press_enter=press_enter,
        hotkey=hotkey,
        scroll_clicks=scroll_clicks,
        question=question,
    )
    return msg

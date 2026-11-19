import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import queue
import threading
import wave

# 必须在导入 pyautogui / PIL / uiautomation 之前开启 Per-Monitor DPI V2 感知，
# 将截图、UIA 控件矩形与鼠标点击统一锁死在 1:1 真实物理像素坐标系（消除 200% 缩放错位）
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

import io
import json
import os
import re
import subprocess
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Optional

import comtypes
import httpx
import psutil
import pyautogui
import pyperclip
from PIL import Image, ImageDraw, ImageFont, ImageGrab

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.08

DATA_DIR = Path(r"e:\故事\1\xiaozhi-server\data")
MODE_FILE = DATA_DIR / "pc_control_mode.json"
TASKS_ROOT = DATA_DIR / "agy_tasks"
PENDING_DIR = TASKS_ROOT / "pending"
ACTIVE_DIR = TASKS_ROOT / "active"
HEARTBEAT_FILE = TASKS_ROOT / "worker_heartbeat.json"
BRAIN_DIR = Path(r"C:\Users\Administrator\.gemini\antigravity\brain")

for _d in (PENDING_DIR, ACTIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _get_mode() -> str:
    try:
        if MODE_FILE.exists():
            m = json.loads(MODE_FILE.read_text(encoding="utf-8")).get("mode", "safe")
            if m in ("off", "safe", "full"):
                return m
    except Exception:
        pass
    return "safe"


def _check_permission(required_level: str) -> Optional[str]:
    """Return error message if blocked by current mode, else None."""
    mode = _get_mode()
    if mode == "off":
        return "电脑操控当前处于【关闭】挡位，请先在 18080 网页控制台开启电脑操控开关。"
    if required_level == "L2" and mode != "full":
        return "当前处于【日常/游戏安全挡】，已物理锁定模拟鼠标点击与键盘输入以防切屏误触。如需点按窗口按钮或打字，请在 18080 网页控制台切至【全权操控挡】。"
    return None


def _get_env_key(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        import winreg
        for root, subkey in [
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ]:
            try:
                with winreg.OpenKey(root, subkey) as k:
                    v, _ = winreg.QueryValueEx(k, name)
                    if v:
                        val = str(v)
                        break
            except Exception:
                pass
    return val.lstrip("\ufeff").strip()


# Win32 Virtual-Key Media & Power helpers
VK_VOLUME_MUTE = 0xAD
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3


def _tap_vk(vk_code: int):
    ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
    time.sleep(0.03)
    ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)


def _set_or_get_volume(action: str, level: Optional[int] = None) -> str:
    comtypes.CoInitialize()
    try:
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
    finally:
        comtypes.CoUninitialize()
    return "音量操作完成。"


from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

mcp = MCPServer("xiaozhi-pc-control")


@mcp.tool()
def pc_system_control(action: str, volume_percent: int = 30) -> str:
    """[L1 瞬时系统控制] 控制电脑音量、静音、音乐视频播放切歌、锁屏或息屏（不抢夺鼠标焦点）。
    action 可选值:
    - set_volume: 设置音量百分比 (0-100)，配合 volume_percent 参数
    - get_volume: 查询当前音量
    - mute / unmute: 静音 / 取消静音
    - media_play_pause: 播放或暂停当前音乐/视频
    - media_next / media_prev: 下一首 / 上一首
    - lock_screen: 锁定电脑屏幕
    - screen_off: 关闭显示器背光息屏
    """
    err = _check_permission("L1")
    if err:
        return err

    act = (action or "").strip().lower()
    if act in ("set_volume", "get_volume", "mute", "unmute"):
        return _set_or_get_volume(act, volume_percent)
    if act == "media_play_pause":
        _tap_vk(VK_MEDIA_PLAY_PAUSE)
        return "已切换媒体播放/暂停状态。"
    if act == "media_next":
        _tap_vk(VK_MEDIA_NEXT_TRACK)
        return "已切换到下一首。"
    if act == "media_prev":
        _tap_vk(VK_MEDIA_PREV_TRACK)
        return "已切换到上一首。"
    if act == "lock_screen":
        ctypes.windll.user32.LockWorkStation()
        return "电脑屏幕已锁定。"
    if act == "screen_off":
        ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
        return "显示器已息屏。"
    return f"不支持的系统动作: {action}"


from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

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
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_user32.AllowSetForegroundWindow.restype = wintypes.BOOL
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = wintypes.LONG
_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_shell32.ShellExecuteW.argtypes = [
    wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
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

_BROWSER_PROCS = {
    "msedge.exe",
    "chrome.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
    "arc.exe",
    "quark.exe",
    "doubao.exe",
    "qqbrowser.exe",
    "360chrome.exe",
    "360se.exe",
}

_COMMON_WEBSITES = {
    "b站": "https://www.bilibili.com",
    "哔哩哔哩": "https://www.bilibili.com",
    "bilibili": "https://www.bilibili.com",
    "百度": "https://www.baidu.com",
    "baidu": "https://www.baidu.com",
    "抖音": "https://www.douyin.com",
    "小红书": "https://www.xiaohongshu.com",
    "知乎": "https://www.zhihu.com",
    "微博": "https://weibo.com",
    "淘宝": "https://www.taobao.com",
    "京东": "https://www.jd.com",
    "豆包": "https://www.doubao.com",
    "deepseek": "https://chat.deepseek.com",
    "chatgpt": "https://chatgpt.com",
    "youtube": "https://www.youtube.com",
    "油管": "https://www.youtube.com",
    "github": "https://github.com",
    "google": "https://www.google.com",
    "谷歌": "https://www.google.com",
    "小智控制台": "http://localhost:18080",
    "控制台": "http://localhost:18080",
}

_BUILTIN_APPS = {
    "记事本": "notepad.exe",
    "notepad": "notepad.exe",
    "计算器": "calc.exe",
    "calc": "calc.exe",
    "画图": "mspaint.exe",
    "任务管理器": "taskmgr.exe",
    "资源管理器": "explorer.exe",
    "文件管理器": "explorer.exe",
    "终端": "wt.exe",
    "命令行": "cmd.exe",
    "vscode": "code",
    "vs code": "code",
    "微信": "weixin",
    "wechat": "weixin",
    "反重力": r"C:\Users\Administrator\AppData\Local\Programs\antigravity\Antigravity.exe",
    "antigravity": r"C:\Users\Administrator\AppData\Local\Programs\antigravity\Antigravity.exe",
}


def _is_window_cloaked(hwnd: int) -> bool:
    cloaked = ctypes.c_int(0)
    res = _dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return res == 0 and cloaked.value != 0


def _get_window_title(hwnd: int) -> str:
    if not hwnd:
        return ""
    length = _user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
    return buf.value.strip()


def _list_top_windows() -> list[dict]:
    """Enumerate visible, non-cloaked top-level desktop windows in Z-order."""
    results: list[dict] = []
    proc_cache: dict[int, str] = {}

    @_WNDENUMPROC
    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        exstyle = _user32.GetWindowLongW(hwnd, -20)
        if exstyle & _WS_EX_TOOLWINDOW:
            return True
        if _is_window_cloaked(hwnd):
            return True
        title = _get_window_title(hwnd)
        if not title or title == "Program Manager":
            return True
        cbuf = ctypes.create_unicode_buffer(128)
        _user32.GetClassNameW(hwnd, cbuf, 128)
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
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

    _user32.EnumWindows(_cb, 0)
    return results


def _force_foreground_window(hwnd: int) -> bool:
    """Bypass Windows ForegroundLockTimeout to restore and pop target window to front."""
    if not hwnd or not _user32.IsWindow(wintypes.HWND(hwnd)):
        return False
    h = wintypes.HWND(hwnd)

    if _user32.IsIconic(h):
        _user32.ShowWindow(h, _SW_RESTORE)
    else:
        _user32.ShowWindow(h, _SW_SHOW)

    fg_hwnd = _user32.GetForegroundWindow()
    fg_tid = _user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    target_tid = _user32.GetWindowThreadProcessId(h, None)
    cur_tid = _kernel32.GetCurrentThreadId()

    attached_fg = False
    attached_target = False
    if fg_tid and fg_tid != cur_tid:
        attached_fg = bool(_user32.AttachThreadInput(cur_tid, fg_tid, True))
    if target_tid and target_tid not in (cur_tid, fg_tid):
        attached_target = bool(_user32.AttachThreadInput(cur_tid, target_tid, True))

    try:
        _user32.SetWindowPos(h, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        _user32.SetWindowPos(h, _HWND_NOTOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        _user32.BringWindowToTop(h)
        _user32.SetForegroundWindow(h)
    finally:
        if attached_target:
            _user32.AttachThreadInput(cur_tid, target_tid, False)
        if attached_fg:
            _user32.AttachThreadInput(cur_tid, fg_tid, False)

    if _user32.GetForegroundWindow() != hwnd:
        # Synthesize benign unassigned key tap to unlock SetForegroundWindow for this thread
        _user32.keybd_event(0xFF, 0, 0, 0)
        _user32.keybd_event(0xFF, 0, 2, 0)
        _user32.SetWindowPos(h, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        _user32.SetWindowPos(h, _HWND_NOTOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        _user32.BringWindowToTop(h)
        _user32.SetForegroundWindow(h)

    return _user32.GetForegroundWindow() == hwnd


def _get_default_browser_proc() -> str:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as k:
            prog_id, _ = winreg.QueryValueEx(k, "ProgId")
            p = str(prog_id).lower()
            for kw, exe in [
                ("msedge", "msedge.exe"),
                ("chrome", "chrome.exe"),
                ("firefox", "firefox.exe"),
                ("brave", "brave.exe"),
                ("opera", "opera.exe"),
                ("vivaldi", "vivaldi.exe"),
                ("quark", "quark.exe"),
                ("doubao", "doubao.exe"),
                ("qqbrowser", "qqbrowser.exe"),
                ("360", "360chrome.exe"),
            ]:
                if kw in p:
                    return exe
    except Exception:
        pass
    return ""


def _shell_open(target_str: str, params: Optional[str] = None):
    _user32.AllowSetForegroundWindow(0xFFFFFFFF)
    res = int(_shell32.ShellExecuteW(None, "open", target_str, params, None, _SW_SHOWNORMAL))
    if res <= 32:
        if target_str.startswith("http"):
            webbrowser.open(target_str)
        else:
            subprocess.Popen(target_str, shell=True)


def _find_start_menu_shortcut(keyword: str) -> Optional[Path]:
    kw = keyword.lower().strip()
    start_dirs = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
        Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
    ]
    best = None
    for sd in start_dirs:
        if not sd.exists():
            continue
        for lnk in sd.rglob("*.lnk"):
            stem = lnk.stem.lower()
            if stem == kw:
                return lnk
            if kw in stem and best is None:
                best = lnk
    return best


@mcp.tool()
def pc_open_app_or_web(target: str) -> str:
    """[L1 打开软件或网页并弹出到最前] 按名称启动或切出电脑上的应用程序（如浏览器、微信、VS Code、记事本、计算器等），或打开网页/常见网站（如 B站、百度、抖音，或直接传 https:// 网址），并自动将对应窗口恢复并置顶弹出到屏幕最前方。"""
    err = _check_permission("L1")
    if err:
        return err

    t = (target or "").strip().rstrip("。！!？?，, ")
    if not t:
        return "未指定要打开的目标。"
    low = t.lower()

    # 1. 常见中文/英文网站名映射或网址识别
    url = ""
    if low in _COMMON_WEBSITES:
        url = _COMMON_WEBSITES[low]
    elif low.startswith(("http://", "https://")):
        url = t
    elif "." in t and " " not in t and not low.endswith((".exe", ".lnk", ".bat", ".cmd")):
        url = f"https://{t}"

    if url:
        pref_proc = _get_default_browser_proc()
        before_bwins = {
            w["hwnd"]: w["title"]
            for w in _list_top_windows()
            if w["proc"] in _BROWSER_PROCS
        }
        _shell_open(url)

        chosen = None
        deadline = time.time() + 2.6
        start_ts = time.time()
        while time.time() < deadline:
            time.sleep(0.15)
            bwins = [w for w in _list_top_windows() if w["proc"] in _BROWSER_PROCS]
            if pref_proc:
                pref_list = [w for w in bwins if w["proc"] == pref_proc]
                if pref_list:
                    bwins = pref_list
            # 优先匹配新创建的浏览器窗口或刚刚切换标签页导致标题变化的浏览器窗口
            for w in bwins:
                if w["hwnd"] not in before_bwins or before_bwins.get(w["hwnd"]) != w["title"]:
                    chosen = w
                    break
            if chosen:
                break
            if time.time() - start_ts >= 0.65 and bwins:
                chosen = bwins[0]
                break

        if chosen:
            _force_foreground_window(chosen["hwnd"])
            time.sleep(0.12)
            if _user32.GetForegroundWindow() != chosen["hwnd"]:
                _force_foreground_window(chosen["hwnd"])
            cur_title = _get_window_title(chosen["hwnd"]) or chosen["title"]
            return f"已在浏览器中打开网页并弹出窗口到最前: {url}（当前页面：{cur_title}）"
        return f"已在默认浏览器中打开网页: {url}"

    # 2. 用户直接说“打开浏览器 / 切到浏览器 / 打开 Edge / 打开 Chrome”
    if low in ("浏览器", "默认浏览器", "浏览器页面", "网页", "edge", "microsoft edge", "chrome", "谷歌浏览器"):
        if "chrome" in low or "谷歌" in low:
            want_proc, launch_exe = "chrome.exe", "chrome.exe"
        elif "edge" in low:
            want_proc, launch_exe = "msedge.exe", "msedge.exe"
        else:
            want_proc = _get_default_browser_proc() or "msedge.exe"
            launch_exe = want_proc

        existing = [w for w in _list_top_windows() if w["proc"] == want_proc]
        if not existing and low in ("浏览器", "默认浏览器", "浏览器页面", "网页"):
            existing = [w for w in _list_top_windows() if w["proc"] in _BROWSER_PROCS]

        if existing:
            win = existing[0]
            _force_foreground_window(win["hwnd"])
            time.sleep(0.1)
            if _user32.GetForegroundWindow() != win["hwnd"]:
                _force_foreground_window(win["hwnd"])
            return f"已将浏览器窗口恢复并弹到屏幕最前：「{win['title']}」"

        before_hwnds = {w["hwnd"] for w in _list_top_windows()}
        _shell_open(launch_exe)
        for _ in range(18):
            time.sleep(0.15)
            bwins = [w for w in _list_top_windows() if w["hwnd"] not in before_hwnds and w["proc"] in _BROWSER_PROCS]
            if bwins:
                _force_foreground_window(bwins[0]["hwnd"])
                return f"已启动浏览器并弹出窗口到最前：「{bwins[0]['title']}」"
        return "已启动浏览器。"

    # 3. 桌面常规软件启动与窗口前置
    before_hwnds = {w["hwnd"] for w in _list_top_windows()}
    launched_label = t
    exe_hint = ""

    if low in ("微信", "wechat", "weixin"):
        existing_wx = [
            w for w in _list_top_windows()
            if "微信" in w["title"] or w["proc"] in ("weixin.exe", "wechat.exe", "wechatappex.exe")
        ]
        if existing_wx:
            _force_foreground_window(existing_wx[0]["hwnd"])
            return f"已将微信窗口切到屏幕最前。"
        lnk = _find_start_menu_shortcut("微信") or _find_start_menu_shortcut("weixin")
        if lnk:
            _shell_open(str(lnk))
            launched_label = lnk.stem
        else:
            return "未在开始菜单找到微信快捷方式。"
    elif low in _BUILTIN_APPS:
        exe = _BUILTIN_APPS[low]
        exe_hint = Path(exe).stem.lower()
        _shell_open(exe)
    else:
        lnk = _find_start_menu_shortcut(t)
        if not lnk:
            return f"未在开始菜单或常用列表中找到名为「{t}」的软件。"
        _shell_open(str(lnk))
        launched_label = lnk.stem
        exe_hint = lnk.stem.lower()

    # 等待新窗口出现或匹配已有同名窗口并置顶
    chosen_app = None
    for step in range(16):
        time.sleep(0.15)
        wins = _list_top_windows()
        new_wins = [w for w in wins if w["hwnd"] not in before_hwnds]
        if new_wins:
            chosen_app = new_wins[0]
            break
        if step >= 4:
            for w in wins:
                if low in w["title"].lower() or (exe_hint and exe_hint in w["proc"]):
                    chosen_app = w
                    break
                if low in ("资源管理器", "文件管理器") and w["class"] == "CabinetWClass":
                    chosen_app = w
                    break
            if chosen_app:
                break

    if chosen_app:
        _force_foreground_window(chosen_app["hwnd"])
        time.sleep(0.1)
        if _user32.GetForegroundWindow() != chosen_app["hwnd"]:
            _force_foreground_window(chosen_app["hwnd"])
        return f"已启动并弹出窗口到最前: {launched_label}（「{chosen_app['title']}」）"

    return f"已启动应用: {launched_label}"


CONFIG_YAML = DATA_DIR / ".config.yaml"
LAST_GROUNDING_IMG = DATA_DIR / "last_som_grounding.jpg"

_POSITIONAL_HINTS = (
    "第", "首个", "最后", "左上", "右上", "左下", "右下", "中间", "正中",
    "左边", "右边", "上方", "下方", "封面", "缩略图", "图标", "头像", "卡片", "视频",
)


def _prefer_gemini_vision() -> bool:
    try:
        if CONFIG_YAML.exists():
            txt = CONFIG_YAML.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"^\s*LLM:\s*GeminiOpenAI\b", txt, re.MULTILINE):
                return True
    except Exception:
        pass
    return False


def _call_vision_llm(prompt_text: str, pil_img: Image.Image, json_mode: bool = False) -> Optional[str]:
    """统一多模态视觉调用：默认优先走国内低延迟 DeepSeek V4.1 Flash 原生视觉，自动互备 Gemini 3.5 Flash-Lite。"""
    buf = io.BytesIO()
    pil_img.convert("RGB").save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    ds_key = _get_env_key("DEEPSEEK_API_KEY")
    gem_key = _get_env_key("GEMINI_API_KEY")

    providers = []
    ds_entry = (
        "deepseek",
        "https://api.deepseek.com/chat/completions",
        ds_key,
        "deepseek-flash",
        {"thinking": {"type": "disabled"}},
    )
    gem_entry = (
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        gem_key,
        "gemini-3.5-flash-lite",
        {},
    )
    if _prefer_gemini_vision():
        providers = [gem_entry, ds_entry]
    else:
        providers = [ds_entry, gem_entry]

    for name, url, api_key, model_name, extra_body in providers:
        if not api_key:
            continue
        payload = {
            "model": model_name,
            "temperature": 0.0 if json_mode else 0.2,
            **extra_body,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            r = httpx.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=16.0,
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if content:
                    return str(content).strip()
        except Exception:
            continue
    return None


def _parse_json_obj(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def _collect_uia_elements(target_win=None, max_items: int = 70) -> list[dict]:
    """穿透 18 层（支持 Edge/Chrome 网页 DOM DocumentControl）采集当前可见可交互控件及其 1:1 物理像素矩形。"""
    import uiautomation as auto

    sw, sh = pyautogui.size()
    items: list[dict] = []
    interesting_types = {
        "HyperlinkControl",
        "ButtonControl",
        "EditControl",
        "TabItemControl",
        "MenuItemControl",
        "ListItemControl",
        "CheckBoxControl",
        "ComboBoxControl",
    }

    with auto.UIAutomationInitializerInThread():
        root = target_win or auto.GetForegroundControl()
        if not root:
            return items
        visited = 0
        for ctrl, _depth in auto.WalkControl(root, includeTop=False, maxDepth=18):
            visited += 1
            if visited > 2800 or len(items) >= max_items:
                break
            try:
                ctype = ctrl.ControlTypeName
                if ctype not in interesting_types:
                    continue
                rect = ctrl.BoundingRectangle
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w < 12 or h < 12 or w > sw * 0.92 or h > sh * 0.85:
                    continue
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                if cx < 4 or cy < 4 or cx > sw - 4 or cy > sh - 4:
                    continue
                cname = (ctrl.Name or "").strip()
                # 合并中心点几乎重合的重复父子包装节点
                dup = False
                for ex in items:
                    if abs(ex["cx"] - cx) <= 10 and abs(ex["cy"] - cy) <= 10:
                        if not ex["name"] and cname:
                            ex["name"] = cname
                        dup = True
                        break
                if dup:
                    continue
                items.append(
                    {
                        "id": len(items) + 1,
                        "type": ctype.replace("Control", ""),
                        "name": cname,
                        "left": int(rect.left),
                        "top": int(rect.top),
                        "right": int(rect.right),
                        "bottom": int(rect.bottom),
                        "cx": int(cx),
                        "cy": int(cy),
                    }
                )
            except Exception:
                continue
    return items


def _draw_som_overlay(base_img: Image.Image, elements: list[dict]) -> Image.Image:
    """在截图上绘制 Set-of-Mark 红色编号角标框，供多模态视觉模型做零误差数字锚定。"""
    annotated = base_img.copy()
    dr = ImageDraw.Draw(annotated)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:
        font = ImageFont.load_default()

    for el in elements[:60]:
        idx_str = str(el["id"])
        x1, y1, x2, y2 = el["left"], el["top"], el["right"], el["bottom"]
        dr.rectangle([x1, y1, x2, y2], outline=(220, 38, 38), width=2)
        tw = max(22, 11 * len(idx_str) + 8)
        th = 22
        bx1 = max(0, min(annotated.width - tw, x1))
        by1 = y1 - th if y1 >= th else y1
        dr.rectangle([bx1, by1, bx1 + tw, by1 + th], fill=(220, 38, 38))
        dr.text((bx1 + 4, by1 + 2), idx_str, fill=(255, 255, 255), font=font)
    return annotated


def _smart_locate_target(target_desc: str) -> Optional[tuple[int, int, str]]:
    """三阶高精度鼠标定位引擎：
    1) 明确文字控件优先走 18 层 UIA 原生矩形直点（0.15s 零误差）；
    2) 位置/视觉描述（如“第一个视频”）走 Set-of-Mark 编号角标 + 0~1000 归一化框多模态定位；
    3) 对无角标的小尺寸按钮自动触发 GUI-Lens 原生像素局部裁剪放大二次精修。
    """
    kw = target_desc.strip()
    kw_low = kw.lower()
    needs_visual_layout = any(h in kw for h in _POSITIONAL_HINTS)

    uia_elems = _collect_uia_elements(max_items=65)

    # 第一阶：非方位词描述时，尝试 UIA 精确或强子串匹配
    if not needs_visual_layout and uia_elems:
        exact_hit = None
        partial_hit = None
        for el in uia_elems:
            nm = el["name"].strip().lower()
            if not nm:
                continue
            if nm == kw_low:
                exact_hit = el
                break
            if len(kw_low) >= 2 and kw_low in nm and partial_hit is None:
                partial_hit = el
        hit = exact_hit or partial_hit
        if hit:
            return hit["cx"], hit["cy"], f"UIA控件「{hit['name'][:30]}」"

    # 第二阶：1:1 物理全屏截图 + Set-of-Mark 角标 + 0~1000 归一化坐标定位
    raw_img = ImageGrab.grab().convert("RGB")
    sw, sh = raw_img.size
    marked_subset = uia_elems[:55]
    som_img = _draw_som_overlay(raw_img, marked_subset) if marked_subset else raw_img

    send_w = min(1600, sw)
    send_h = int(sh * (send_w / sw))
    send_img = som_img.resize((send_w, send_h)) if send_w != sw else som_img

    legend_lines = [
        f"[{el['id']}] {el['type']}: {el['name'][:28]}"
        for el in marked_subset
        if el["name"]
    ][:35]
    legend_text = ("\n已知部分编号控件文字参考：\n" + "\n".join(legend_lines)) if legend_lines else ""

    prompt_coarse = (
        f"请在这张电脑屏幕截图中精确定位用户想要操作的目标：「{kw}」。\n"
        "图中部分可交互元素左上角标有红底白字编号（如 1, 2, 3...）。\n"
        "请返回严格 JSON 对象（不要输出任何额外解释）：\n"
        "{\n"
        '  "found": true,\n'
        '  "mark_id": 0,\n'
        '  "label": "识别到的目标简短名称",\n'
        '  "x_min": 120,\n'
        '  "y_min": 240,\n'
        '  "x_max": 360,\n'
        '  "y_max": 420\n'
        "}\n"
        "字段说明：\n"
        "- found: 画面中是否存在该目标（true/false）；\n"
        "- mark_id: 若某个红底数字框恰好就是该目标，填其整数编号；若目标（如视频封面大图、自绘按钮）没有贴合的红框，填 0；\n"
        "- x_min, y_min, x_max, y_max: 目标可点击区域在整张图中的边界框，全部归一化到 0~1000（左边缘 x=0，右边缘 x=1000；上边缘 y=0，下边缘 y=1000）。"
        + legend_text
    )

    resp_obj = _parse_json_obj(_call_vision_llm(prompt_coarse, send_img, json_mode=True))
    if not resp_obj or not resp_obj.get("found"):
        return None

    mark_id = int(resp_obj.get("mark_id") or 0)
    label = str(resp_obj.get("label") or kw)[:32]
    x_min = float(resp_obj.get("x_min", -1))
    y_min = float(resp_obj.get("y_min", -1))
    x_max = float(resp_obj.get("x_max", -1))
    y_max = float(resp_obj.get("y_max", -1))

    has_box = (0 <= x_min < x_max <= 1000) and (0 <= y_min < y_max <= 1000)
    vx = int(((x_min + x_max) / 2000.0) * sw) if has_box else -1
    vy = int(((y_min + y_max) / 2000.0) * sh) if has_box else -1
    vw = int(((x_max - x_min) / 1000.0) * sw) if has_box else 0
    vh = int(((y_max - y_min) / 1000.0) * sh) if has_box else 0

    final_x, final_y, src_tag = -1, -1, ""

    # 若命中了 Set-of-Mark 红框编号，校验其与视觉框没有偏离过远后直接吸附原生 UIA 中心
    if 1 <= mark_id <= len(marked_subset):
        el = marked_subset[mark_id - 1]
        if not has_box or (abs(el["cx"] - vx) <= max(200, vw) and abs(el["cy"] - vy) <= max(150, vh)):
            final_x, final_y = el["cx"], el["cy"]
            src_tag = f"SoM#{mark_id}「{el['name'][:24] or label}」"

    # 第三阶：若未吸附 UIA 角标，使用 0~1000 视觉框；若目标较小则自动裁剪 1:1 高清原图二次精修
    if final_x < 0 and has_box:
        final_x, final_y = vx, vy
        src_tag = f"视觉定位「{label}」"
        if vw < 200 or vh < 110:
            crop_w, crop_h = min(768, sw), min(512, sh)
            cx1 = max(0, min(sw - crop_w, vx - crop_w // 2))
            cy1 = max(0, min(sh - crop_h, vy - crop_h // 2))
            cx2, cy2 = cx1 + crop_w, cy1 + crop_h
            crop_img = raw_img.crop((cx1, cy1, cx2, cy2))
            prompt_fine = (
                f"这是从屏幕局部 1:1 放大裁剪出的高清区域，请精确定位其中「{kw}」的可点击中心框。\n"
                '返回严格 JSON：{"found": true, "x_min": 0~1000, "y_min": 0~1000, "x_max": 0~1000, "y_max": 0~1000}。'
            )
            fine_obj = _parse_json_obj(_call_vision_llm(prompt_fine, crop_img, json_mode=True))
            if fine_obj and fine_obj.get("found"):
                fx1 = float(fine_obj.get("x_min", -1))
                fy1 = float(fine_obj.get("y_min", -1))
                fx2 = float(fine_obj.get("x_max", -1))
                fy2 = float(fine_obj.get("y_max", -1))
                if 0 <= fx1 < fx2 <= 1000 and 0 <= fy1 < fy2 <= 1000:
                    final_x = cx1 + int(((fx1 + fx2) / 2000.0) * crop_w)
                    final_y = cy1 + int(((fy1 + fy2) / 2000.0) * crop_h)
                    src_tag = f"高清精修定位「{label}」"

    if final_x < 0 or final_y < 0:
        return None

    final_x = max(2, min(sw - 2, final_x))
    final_y = max(2, min(sh - 2, final_y))

    # 保存最近一次视觉落点校准图（绿十字准星），方便排查核验
    try:
        dbg = som_img.copy()
        d = ImageDraw.Draw(dbg)
        d.ellipse([final_x - 16, final_y - 16, final_x + 16, final_y + 16], outline=(16, 185, 129), width=4)
        d.line([final_x - 26, final_y, final_x + 26, final_y], fill=(16, 185, 129), width=3)
        d.line([final_x, final_y - 26, final_x, final_y + 26], fill=(16, 185, 129), width=3)
        dbg.resize((1280, int(sh * (1280 / sw)))).save(LAST_GROUNDING_IMG, format="JPEG", quality=82)
    except Exception:
        pass

    return final_x, final_y, src_tag


@mcp.tool()
def pc_look_at_screen(question: str = "请简明描述当前电脑屏幕上正在显示的主要内容。") -> str:
    """[L1 屏幕视觉识别] 仅当用户询问“看看我电脑屏幕上是什么 / 这个报错怎么回事 / 读一下屏幕内容”时调用。注意：如果要点击界面元素，请直接调用 pc_ui_action 并传入 element_name，不要先调本工具猜坐标！"""
    err = _check_permission("L1")
    if err:
        return err

    img = ImageGrab.grab().convert("RGB")
    w, h = img.size
    if w > 1600:
        img = img.resize((1600, int(h * (1600 / w))))

    prompt_q = f"用户正在通过语音询问他当前的电脑屏幕：{question}\n请用清晰简洁的中文直接给出准确观察结果或解答（控制在120字以内，便于语音播报）。"
    ans = _call_vision_llm(prompt_q, img, json_mode=False)
    if not ans:
        return "屏幕视觉识别接口暂不可用，请检查 DEEPSEEK_API_KEY 或 GEMINI_API_KEY。"
    return f"屏幕画面分析结果：{ans}"


@mcp.tool()
def pc_check_system_status() -> str:
    """[L1 电脑状态查询] 查询电脑当前 CPU 占用、内存使用率、显卡温度与显存占用，以及当前前台打开的窗口名称。"""
    err = _check_permission("L1")
    if err:
        return err

    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    gpu_info = "未知"
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,temperature.gpu,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
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


@mcp.tool()
def pc_inspect_window_ui(window_keyword: str = "") -> str:
    """[L2 窗口组件读取] 读取当前前台窗口（或标题包含 window_keyword 的窗口）内的可点击按钮、网页链接、输入框等 UI 元素名称与真实物理坐标（需全权操控挡）。"""
    err = _check_permission("L2")
    if err:
        return err

    import uiautomation as auto
    target_win = None
    win_name = "当前窗口"
    with auto.UIAutomationInitializerInThread():
        if window_keyword.strip():
            for w in auto.GetRootControl().GetChildren():
                if window_keyword.lower() in (w.Name or "").lower():
                    target_win = w
                    break
            if not target_win:
                return f"未找到标题包含「{window_keyword}」的顶层窗口。"
        else:
            target_win = auto.GetForegroundControl()
        win_name = (target_win.Name or "当前窗口") if target_win else "当前窗口"

    elems = _collect_uia_elements(target_win=target_win, max_items=45)
    if not elems:
        return f"窗口「{win_name}」未暴露标准 UIA 子控件，可直接调用 pc_ui_action(action='click', element_name='目标视觉描述') 进行视觉定位点击。"
    lines = [f"- [{e['id']}:{e['type']}] {e['name'][:38] or '(无文字控件)'} @({e['cx']},{e['cy']})" for e in elems]
    return f"窗口「{win_name}」可交互元素（前 {len(lines)} 项，1:1 物理像素）：\n" + "\n".join(lines)


@mcp.tool()
def pc_ui_action(
    action: str,
    element_name: str = "",
    x: int = -1,
    y: int = -1,
    text: str = "",
    press_enter: bool = False,
    hotkey: str = "",
    scroll_clicks: int = -5,
) -> str:
    """[L2 高精度界面点击与键鼠输入] 在电脑窗口或网页中精准点击元素、输入文字、按快捷键或滚轮翻页（需在 18080 控制台开启全权操控挡）。
    【重要调用准则】：点击任何按钮、网页视频封面、输入框或图标时，请直接把目标描述传给 element_name（例如 '第一个视频封面'、'搜索输入框'、'全屏按钮' 或具体标题文字），本工具内部已内置 1:1 物理像素 UIA + Set-of-Mark 多模态视觉精确定位！严禁自己先调 pc_look_at_screen 估算 x, y 像素盲点！
    action 可选:
    - click / double_click / right_click: 传入 element_name 自动精确定位并点击/双击/右键（仅在已知精确物理像素时才传 x, y）
    - type_text: 输入中英文文字 text（若传了 element_name 会先精确定位并点击该输入框；press_enter=True 则输完自动按回车）
    - hotkey: 发送快捷键，如 'ctrl+c'、'alt+tab'、'space'、'enter'、'esc'
    - scroll: 滚动鼠标滚轮，scroll_clicks 负数向下滚，正数向上滚
    """
    err = _check_permission("L2")
    if err:
        return err

    act = (action or "").strip().lower()

    def _move_and_click(cx: int, cy: int, click_kind: str = "click"):
        pyautogui.moveTo(cx, cy, duration=0.12)
        time.sleep(0.05)
        if click_kind == "double_click":
            pyautogui.doubleClick(cx, cy)
        elif click_kind == "right_click":
            pyautogui.rightClick(cx, cy)
        else:
            pyautogui.click(cx, cy)

    if act in ("click", "double_click", "right_click"):
        if element_name.strip():
            loc = _smart_locate_target(element_name)
            if not loc:
                return f"未能在当前画面上定位到「{element_name}」，请确认目标是否在屏幕可见区域或尝试向下滚动页面。"
            cx, cy, src_desc = loc
            _move_and_click(cx, cy, act)
            verb = {"click": "点击", "double_click": "双击", "right_click": "右键点击"}[act]
            return f"已精准{verb} {src_desc}，物理像素坐标 ({cx}, {cy})。"
        elif x >= 0 and y >= 0:
            _move_and_click(x, y, act)
            return f"已点击屏幕物理坐标 ({x}, {y})。"
        return "click 操作需要提供 element_name（推荐）或有效的 x, y 坐标。"

    if act == "type_text":
        if element_name.strip():
            loc = _smart_locate_target(element_name)
            if loc:
                _move_and_click(loc[0], loc[1], "click")
                time.sleep(0.15)
        old_clip = ""
        try:
            old_clip = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.1)
        if press_enter:
            pyautogui.press("enter")
        try:
            pyperclip.copy(old_clip)
        except Exception:
            pass
        return f"已输入文本：「{text[:30]}」" + (" 并按下回车。" if press_enter else "。")

    if act == "hotkey":
        keys = [k.strip().lower() for k in hotkey.replace("-", "+").split("+") if k.strip()]
        if not keys:
            return "未提供有效的 hotkey。"
        pyautogui.hotkey(*keys)
        return f"已按下快捷键: {'+'.join(keys)}"

    if act == "scroll":
        pyautogui.scroll(int(scroll_clicks) * 120)
        return f"已{'向下' if scroll_clicks < 0 else '向上'}滚动页面。"

    return f"未知的 UI 操作类型: {action}"


def _get_last_dispatched_task() -> Optional[dict]:
    files = sorted(ACTIVE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def _summarize_transcript(conv_id: str) -> str:
    t_path = BRAIN_DIR / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not t_path.exists():
        return f"会话 ({conv_id[:8]}) 已创建，正在初始化或等待执行中。"

    lines = t_path.read_text(encoding="utf-8", errors="replace").splitlines()
    last_model_reply = ""
    last_step_type = ""
    last_status = ""
    for ln in reversed(lines):
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
            if not last_step_type:
                last_step_type = obj.get("type", "")
                last_status = obj.get("status", "")
            if obj.get("type") == "PLANNER_RESPONSE" and obj.get("content"):
                last_model_reply = str(obj["content"]).strip()
                break
        except Exception:
            pass

    if last_model_reply:
        short = last_model_reply[:280].replace("\n", " ")
        return f"反重力最新进展（状态 {last_status}）：{short}"
    return f"反重力正在后台执行工具步骤中（当前步骤类型: {last_step_type}，共 {len(lines)} 步），请稍后再查询。"


@mcp.tool()
def antigravity_agent_task(
    action: str = "dispatch",
    task_instruction: str = "",
    follow_up_last: bool = False,
) -> str:
    """[L3 本地反重力重装大脑] 当用户要求执行复杂多步电脑工程任务（如整理归档文件夹、编写/修改代码、操作浏览器深度查询生成报告、运行视频工作流）或明确提到“反重力”时调用。
    action 可选:
    - dispatch: 异步派发新任务给本地已登录的反重力客户端执行（follow_up_last=True 则向最近一次派发的任务追加指令）
    - check_status: 查询最近一次派发给反重力的任务执行完没有及最终结果摘要
    """
    err = _check_permission("L3")
    if err:
        return err

    act = (action or "dispatch").strip().lower()
    if act == "check_status":
        last = _get_last_dispatched_task()
        if not last:
            pending = list(PENDING_DIR.glob("*.json"))
            if pending:
                return f"当前有 {len(pending)} 个任务在队列中等待反重力伴生桥接领取执行。"
            return "目前还没有派发过反重力任务。"
        if last.get("status") != "dispatched" or not last.get("conversation_id"):
            return f"最近一次任务派发状态异常：{last.get('stderr') or last.get('stdout') or '未知错误'}"
        return _summarize_transcript(last["conversation_id"])

    # dispatch
    instr = (task_instruction or "").strip()
    if not instr:
        return "任务内容不能为空。"

    task_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    conv_id = ""
    if follow_up_last:
        last = _get_last_dispatched_task()
        if last and last.get("conversation_id"):
            conv_id = last["conversation_id"]

    task_obj = {
        "id": task_id,
        "title": f"[小智派单] {instr[:24]}",
        "prompt": instr,
        "conversation_id": conv_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    pending_file = PENDING_DIR / f"{task_id}.json"
    pending_file.write_text(json.dumps(task_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    # Check if official Antigravity Sidecar worker is alive
    worker_alive = False
    if HEARTBEAT_FILE.exists():
        try:
            hb = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
            if time.time() - float(hb.get("ts", 0)) < 15.0:
                worker_alive = True
        except Exception:
            pass

    if not worker_alive:
        return (
            "任务已为你存入派单队列！不过检测到反重力客户端里的【小智语音派单桥接】伴生服务还没开启，"
            "请舰长在反重力 Automations 面板点一下启用，启用后队列里的任务就会立刻自动跑起来。"
        )

    # Wait briefly (up to 3.5s) for worker pickup confirmation
    active_file = ACTIVE_DIR / f"{task_id}.json"
    for _ in range(14):
        if active_file.exists():
            res = json.loads(active_file.read_text(encoding="utf-8"))
            if res.get("status") == "dispatched":
                cid_short = (res.get("conversation_id") or "")[:8]
                return f"已成功派单给本机反重力后台执行（会话 {cid_short}），不占用语音等待时间，稍后可随时问我执行结果！"
            return f"派单遇到错误：{res.get('stderr') or '请检查反重力客户端状态'}"
        time.sleep(0.25)

    return "已成功投递给本机反重力后台队列开始处理，稍后可随时问我进度！"


# =====================================================================
# Windows Native WinMM (waveOut) 48kHz Lossless PC Speaker Audio Bridge
# Mounted directly on existing 18900 HTTP server (zero extra port/RAM)
# =====================================================================
class _WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wt.WORD),
        ("nChannels", wt.WORD),
        ("nSamplesPerSec", wt.DWORD),
        ("nAvgBytesPerSec", wt.DWORD),
        ("nBlockAlign", wt.WORD),
        ("wBitsPerSample", wt.WORD),
        ("cbSize", wt.WORD),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_char_p),
        ("dwBufferLength", wt.DWORD),
        ("dwBytesRecorded", wt.DWORD),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", wt.DWORD),
        ("dwLoops", wt.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


_winmm = ctypes.windll.winmm
_winmm.waveOutGetNumDevs.argtypes = []
_winmm.waveOutGetNumDevs.restype = wt.UINT
_winmm.waveOutOpen.argtypes = [
    ctypes.POINTER(ctypes.c_void_p),
    wt.UINT,
    ctypes.POINTER(_WAVEFORMATEX),
    ctypes.c_size_t,
    ctypes.c_size_t,
    wt.DWORD,
]
_winmm.waveOutOpen.restype = wt.UINT
_winmm.waveOutPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), wt.UINT]
_winmm.waveOutPrepareHeader.restype = wt.UINT
_winmm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), wt.UINT]
_winmm.waveOutWrite.restype = wt.UINT
_winmm.waveOutUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), wt.UINT]
_winmm.waveOutUnprepareHeader.restype = wt.UINT
_winmm.waveOutReset.argtypes = [ctypes.c_void_p]
_winmm.waveOutReset.restype = wt.UINT
_winmm.waveOutClose.argtypes = [ctypes.c_void_p]
_winmm.waveOutClose.restype = wt.UINT

_WAVE_MAPPER = 0xFFFFFFFF
_WAVE_FORMAT_PCM = 1
_WHDR_DONE = 0x00000001


def _scale_pcm16(pcm_bytes: bytes, volume: float) -> bytes:
    if abs(volume - 1.0) <= 0.01 or not pcm_bytes:
        return pcm_bytes
    vol = max(0.05, min(1.8, float(volume)))
    try:
        import audioop
        return audioop.mul(pcm_bytes, 2, vol)
    except Exception:
        import array
        arr = array.array("h")
        arr.frombytes(pcm_bytes)
        for i, s in enumerate(arr):
            v = int(s * vol)
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            arr[i] = v
        return arr.tobytes()


class _WinMMPlayer:
    """Sequential lossless PCM WAV playback queue for Windows default audio device."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._gen = 0
        self._active_hwo: Optional[ctypes.c_void_p] = None
        self._active_fmt: Optional[tuple] = None
        self._playing = False
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._stop_signal = threading.Event()
        self._worker = threading.Thread(target=self._run_loop, name="winmm-audio-player", daemon=True)
        self._worker.start()

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "playing": self._playing,
                "queued": self._queue.qsize(),
                "devices": int(_winmm.waveOutGetNumDevs()),
            }

    def enqueue_wav(self, wav_bytes: bytes, turn_id: str = "", volume: float = 1.0) -> int:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            pcm = wf.readframes(nframes)
        if sampwidth != 2:
            raise ValueError(f"Only 16-bit PCM WAV is supported, got sampwidth={sampwidth}")
        if abs(volume - 1.0) > 0.01:
            pcm = _scale_pcm16(pcm, volume)
        dur_ms = int(round((nframes / float(framerate)) * 1000.0)) if framerate > 0 else 0
        with self._lock:
            gen = self._gen
            self._idle_event.clear()
            self._queue.put((gen, turn_id, pcm, channels, framerate, sampwidth * 8))
        return dur_ms

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self._stop_signal.set()
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
            hwo = self._active_hwo
        if hwo is not None:
            try:
                _winmm.waveOutReset(hwo)
            except Exception:
                pass

    def wait_idle(self, timeout: float = 8.0) -> bool:
        return self._idle_event.wait(timeout=max(0.1, float(timeout)))

    def _close_device(self):
        with self._lock:
            hwo = self._active_hwo
            self._active_hwo = None
            self._active_fmt = None
        if hwo is not None:
            try:
                _winmm.waveOutReset(hwo)
                _winmm.waveOutClose(hwo)
            except Exception:
                pass

    def _ensure_device(self, channels: int, sample_rate: int, bits: int) -> ctypes.c_void_p:
        fmt_key = (channels, sample_rate, bits)
        with self._lock:
            if self._active_hwo is not None and self._active_fmt == fmt_key:
                return self._active_hwo
        self._close_device()
        wfx = _WAVEFORMATEX()
        wfx.wFormatTag = _WAVE_FORMAT_PCM
        wfx.nChannels = channels
        wfx.nSamplesPerSec = sample_rate
        wfx.wBitsPerSample = bits
        wfx.nBlockAlign = channels * (bits // 8)
        wfx.nAvgBytesPerSec = sample_rate * wfx.nBlockAlign
        wfx.cbSize = 0
        hwo = ctypes.c_void_p()
        res = _winmm.waveOutOpen(ctypes.byref(hwo), _WAVE_MAPPER, ctypes.byref(wfx), 0, 0, 0)
        if res != 0:
            raise RuntimeError(f"waveOutOpen failed with code {res}")
        with self._lock:
            self._active_hwo = hwo
            self._active_fmt = fmt_key
        return hwo

    def _run_loop(self):
        while True:
            try:
                item = self._queue.get(timeout=0.8)
            except queue.Empty:
                # Release audio handle cleanly when idle so Windows sound sleep/switch stays clean
                self._close_device()
                with self._lock:
                    if self._queue.empty() and not self._playing:
                        self._idle_event.set()
                continue

            gen, _turn_id, pcm, channels, sample_rate, bits = item
            try:
                with self._lock:
                    if gen != self._gen:
                        continue
                    self._playing = True
                    self._stop_signal.clear()

                hwo = self._ensure_device(channels, sample_rate, bits)
                buf = ctypes.create_string_buffer(pcm, len(pcm))
                hdr = _WAVEHDR()
                hdr.lpData = ctypes.cast(buf, ctypes.c_char_p)
                hdr.dwBufferLength = len(pcm)
                hdr.dwFlags = 0

                if _winmm.waveOutPrepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr)) != 0:
                    self._close_device()
                    continue
                try:
                    if _winmm.waveOutWrite(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr)) != 0:
                        self._close_device()
                        continue
                    while not (hdr.dwFlags & _WHDR_DONE):
                        with self._lock:
                            aborted = gen != self._gen
                        if aborted:
                            _winmm.waveOutReset(hwo)
                            break
                        self._stop_signal.wait(0.012)
                    # Wait briefly for driver to mark WHDR_DONE after reset before unpreparing
                    for _ in range(25):
                        if hdr.dwFlags & _WHDR_DONE:
                            break
                        time.sleep(0.005)
                finally:
                    _winmm.waveOutUnprepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr))
            except Exception as exc:
                print(f"[PCAudio] playback error: {exc}", flush=True)
                self._close_device()
            finally:
                self._queue.task_done()
                with self._lock:
                    self._playing = False
                    if self._queue.empty():
                        self._idle_event.set()


_pc_audio_player = _WinMMPlayer()

from starlette.requests import Request as _StarletteRequest
from starlette.responses import JSONResponse as _StarletteJSONResponse


@mcp.custom_route("/audio/status", methods=["GET"])
async def _audio_status_route(request: _StarletteRequest):
    return _StarletteJSONResponse(_pc_audio_player.status())


@mcp.custom_route("/audio/play", methods=["POST"])
async def _audio_play_route(request: _StarletteRequest):
    try:
        wav_bytes = await request.body()
        if not wav_bytes:
            return _StarletteJSONResponse({"ok": False, "error": "empty body"}, status_code=400)
        turn_id = request.headers.get("X-Turn-Id", "")
        try:
            volume = float(request.headers.get("X-Volume", "1.0"))
        except Exception:
            volume = 1.0
        dur_ms = await asyncio.to_thread(_pc_audio_player.enqueue_wav, wav_bytes, turn_id, volume)
        return _StarletteJSONResponse({"ok": True, "duration_ms": dur_ms})
    except Exception as exc:
        return _StarletteJSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@mcp.custom_route("/audio/stop", methods=["POST"])
async def _audio_stop_route(request: _StarletteRequest):
    await asyncio.to_thread(_pc_audio_player.stop)
    return _StarletteJSONResponse({"ok": True})


@mcp.custom_route("/audio/wait", methods=["GET"])
async def _audio_wait_route(request: _StarletteRequest):
    try:
        timeout = float(request.query_params.get("timeout", "8.0"))
    except Exception:
        timeout = 8.0
    timeout = max(0.2, min(20.0, timeout))
    done = await asyncio.to_thread(_pc_audio_player.wait_idle, timeout)
    return _StarletteJSONResponse({"ok": True, "done": bool(done)})


if __name__ == "__main__":
    print("[xiaozhi-pc-control] Starting L1+L2+L3 MCP + PC Audio Bridge on 0.0.0.0:18900 ...", flush=True)
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=18900,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

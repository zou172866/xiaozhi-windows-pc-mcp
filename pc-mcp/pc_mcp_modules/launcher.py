"""Application & website launcher with foreground window restoration & Start Menu hardening (P2-20, H-7)."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
import time
from typing import Optional
import webbrowser

from . import facade_attr
from .winapi import (
    _SW_SHOWNORMAL,
    _force_foreground_window,
    _get_window_title,
    _list_top_windows,
    _shell32,
    _user32,
)

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

_LOCAL_APPDATA = os.environ.get("LOCALAPPDATA", "")
_ANTIGRAVITY_EXE = (
    str(Path(_LOCAL_APPDATA) / "Programs" / "antigravity" / "Antigravity.exe")
    if _LOCAL_APPDATA
    else "Antigravity.exe"
)
_WORKBUDDY_EXE = (
    str(Path(_LOCAL_APPDATA) / "Programs" / "WorkBuddy" / "WorkBuddy.exe")
    if _LOCAL_APPDATA
    else "WorkBuddy.exe"
)

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
    "反重力": _ANTIGRAVITY_EXE,
    "antigravity": _ANTIGRAVITY_EXE,
    "workbuddy": _WORKBUDDY_EXE,
    "腾讯workbuddy": _WORKBUDDY_EXE,
}


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


def _shell_open(target_str: str, params: Optional[str] = None) -> None:
    u32 = facade_attr("_user32", _user32)
    u32.AllowSetForegroundWindow(0xFFFFFFFF)
    res = int(_shell32.ShellExecuteW(None, "open", target_str, params, None, _SW_SHOWNORMAL) or 0)
    if res <= 32:
        if target_str.lower().startswith(("http://", "https://")):
            webbrowser.open(target_str)
            return
        if not params:
            try:
                os.startfile(target_str)
                return
            except OSError:
                pass
        resolved = target_str if os.path.isfile(target_str) else shutil.which(target_str)
        if not resolved:
            raise FileNotFoundError(f"无法启动目标程序或快捷方式: {target_str}")
        extra_args = [p.strip('"').strip("'") for p in shlex.split(params, posix=False)] if params else []
        popen_kw = {
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            popen_kw["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            )
        subprocess.Popen([resolved, *extra_args], **popen_kw)


# H-7: Start Menu shortcut blacklist blocking sensitive/system tools & uninstallers from fuzzy launch
_BLOCKED_SHORTCUT_DIR_NAMES = frozenset({
    "administrative tools",
    "windows administrative tools",
    "windows 管理工具",
    "管理工具",
    "windows powershell",
    "powershell",
})

_BLOCKED_SHORTCUT_EXACT_STEMS = frozenset({
    "run",
    "运行",
    "cmd",
    "wt",
    "wsl",
    "powershell",
    "pwsh",
    "regedit",
    "regedt32",
    "terminal",
    "终端",
    "shell",
    "console",
    "控制台",
    "services",
    "服务",
    "taskmgr",
    "msconfig",
    "gpedit",
    "secpol",
    "mmc",
})

_BLOCKED_SHORTCUT_SUBSTRINGS = (
    # Uninstallers / removers
    "uninstall",
    "卸载",
    "移除",
    "解除安装",
    "反安装",
    # Shells & terminals
    "powershell",
    "pwsh",
    "command prompt",
    "命令提示符",
    "windows terminal",
    "git bash",
    "git cmd",
    "wezterm",
    "mintty",
    "cygwin",
    "alacritty",
    "conemu",
    "cmder",
    "putty",
    "tabby",
    "mysql shell",
    "command line client",
    "developer command prompt",
    "native tools command prompt",
    "cross tools command prompt",
    "anaconda prompt",
    "conda prompt",
    "node.js command prompt",
    # Registry & system admin tools
    "regedit",
    "registry editor",
    "注册表",
    "msconfig",
    "system configuration",
    "系统配置",
    "gpedit",
    "组策略",
    "secpol",
    "security configuration",
    "安全策略",
    "computer management",
    "计算机管理",
    "disk management",
    "磁盘管理",
    "device manager",
    "设备管理器",
    "event viewer",
    "事件查看器",
    "task scheduler",
    "任务计划程序",
    "component services",
    "组件服务",
    "recovery drive",
    "recoverydrive",
    "恢复驱动器",
    "reset this pc",
    "重置此电脑",
    "remote desktop",
    "远程桌面",
    "mstsc",
    "task manager",
    "任务管理器",
    "administrative tools",
    "管理工具",
    "defender firewall",
    "防火墙",
    "dfrgui",
    "iscsi",
    "odbc",
    "memory diagnostics",
    "performance monitor",
    "resource monitor",
    "print management",
)

_BLOCKED_SHORTCUT_WORD_RE = re.compile(
    r"\b(?:cmd|wt|wsl|bash|sh|pwsh|powershell|regedit|regedt32|msconfig|gpedit|secpol|taskmgr|mstsc|mmc|diskpart|bcdedit|netsh|schtasks|wmic|cscript|wscript|mshta|rundll32|certutil|bitsadmin)\b",
    re.IGNORECASE,
)

_shortcut_cache_lock = threading.Lock()
_shortcut_cache: dict[tuple[str, ...], dict] = {}


def _clear_start_menu_cache() -> None:
    """Clear cached Start Menu shortcut index (useful in unit tests)."""
    with _shortcut_cache_lock:
        _shortcut_cache.clear()


def _is_blocked_shortcut(lnk: Path, *, root_dir: Optional[Path] = None) -> bool:
    """Return True if a Start Menu shortcut targets a sensitive/system tool or uninstaller (H-7)."""
    stem = lnk.stem.strip().lower()
    if not stem:
        return True
    if stem in _BLOCKED_SHORTCUT_EXACT_STEMS:
        return True
    if any(tok in stem for tok in _BLOCKED_SHORTCUT_SUBSTRINGS):
        return True
    if _BLOCKED_SHORTCUT_WORD_RE.search(stem):
        return True

    # Check relative parent directory names inside Start Menu (e.g. Administrative Tools, Windows PowerShell)
    try:
        if root_dir is not None:
            rel_parts = lnk.relative_to(root_dir).parent.parts
        else:
            rel_parts = lnk.parent.parts[-3:]
    except Exception:
        rel_parts = lnk.parent.parts[-3:]
    for part in rel_parts:
        p_low = part.strip().lower()
        if p_low in _BLOCKED_SHORTCUT_DIR_NAMES:
            return True
    return False


def _default_start_menu_dirs() -> list[Path]:
    return [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
        Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
    ]


def _stat_dir_mtime_ns(dir_str: str) -> int:
    try:
        return os.stat(dir_str).st_mtime_ns
    except OSError:
        return -1


def _get_start_menu_shortcuts(start_dirs: Optional[list[Path]] = None) -> list[tuple[str, Path]]:
    """Return cached non-blocked `(stem_lower, Path)` Start Menu shortcut list, invalidated by directory mtime (H-7)."""
    dirs = list(start_dirs) if start_dirs is not None else _default_start_menu_dirs()
    cache_key = tuple(os.path.normcase(os.path.normpath(str(d))) for d in dirs)

    with _shortcut_cache_lock:
        cached = _shortcut_cache.get(cache_key)
        if cached is not None:
            dir_mtimes: list[tuple[str, int]] = cached["dir_mtimes"]
            if all(_stat_dir_mtime_ns(d_str) == prev_mt for d_str, prev_mt in dir_mtimes):
                return list(cached["shortcuts"])

    # Cache miss or directory mtime changed: walk start menu directories and rebuild filtered index
    new_dir_mtimes: list[tuple[str, int]] = []
    shortcuts: list[tuple[str, Path]] = []
    seen_dirs: set[str] = set()

    for sd in dirs:
        sd_str = str(sd)
        if not sd.exists():
            if sd_str not in seen_dirs:
                seen_dirs.add(sd_str)
                new_dir_mtimes.append((sd_str, -1))
            continue
        for dirpath, dirnames, filenames in os.walk(sd_str):
            dirnames.sort()
            if dirpath not in seen_dirs:
                seen_dirs.add(dirpath)
                new_dir_mtimes.append((dirpath, _stat_dir_mtime_ns(dirpath)))
            for fn in sorted(filenames):
                if not fn.lower().endswith(".lnk"):
                    continue
                lnk_path = Path(dirpath) / fn
                if _is_blocked_shortcut(lnk_path, root_dir=sd):
                    continue
                shortcuts.append((lnk_path.stem.lower(), lnk_path))

    with _shortcut_cache_lock:
        if len(_shortcut_cache) >= 8 and cache_key not in _shortcut_cache:
            _shortcut_cache.pop(next(iter(_shortcut_cache)))
        _shortcut_cache[cache_key] = {
            "dir_mtimes": new_dir_mtimes,
            "shortcuts": shortcuts,
        }
    return list(shortcuts)


def _find_start_menu_shortcut(
    keyword: str,
    *,
    start_dirs: Optional[list[Path]] = None,
) -> Optional[Path]:
    kw = (keyword or "").lower().strip()
    if not kw:
        return None
    shortcuts = _get_start_menu_shortcuts(start_dirs=start_dirs)
    best: Optional[tuple[int, Path]] = None
    for stem, lnk in shortcuts:
        if stem == kw:
            return lnk
        if kw in stem:
            cand = (len(stem), lnk)
            if best is None or cand[0] < best[0]:
                best = cand
    return best[1] if best is not None else None


def _pc_open_app_or_web_locked(t: str) -> str:
    low = t.lower()
    u32 = facade_attr("_user32", _user32)
    tmod = facade_attr("time", time)
    list_wins = facade_attr("_list_top_windows", _list_top_windows)
    force_fg = facade_attr("_force_foreground_window", _force_foreground_window)
    shell_open = facade_attr("_shell_open", _shell_open)
    get_title = facade_attr("_get_window_title", _get_window_title)
    get_def_browser = facade_attr("_get_default_browser_proc", _get_default_browser_proc)
    find_shortcut = facade_attr("_find_start_menu_shortcut", _find_start_menu_shortcut)

    # 1. 常见中文/英文网站名映射或网址识别
    url = ""
    if low in _COMMON_WEBSITES:
        url = _COMMON_WEBSITES[low]
    elif low.startswith(("http://", "https://")):
        url = t
    elif "." in t and " " not in t and not low.endswith((".exe", ".lnk", ".bat", ".cmd")):
        url = f"https://{t}"

    if url:
        pref_proc = get_def_browser()
        before_bwins = {
            w["hwnd"]: w["title"]
            for w in list_wins()
            if w["proc"] in _BROWSER_PROCS
        }
        shell_open(url)

        chosen = None
        deadline = tmod.time() + 2.6
        start_ts = tmod.time()
        while tmod.time() < deadline:
            tmod.sleep(0.15)
            bwins = [w for w in list_wins() if w["proc"] in _BROWSER_PROCS]
            if pref_proc:
                pref_list = [w for w in bwins if w["proc"] == pref_proc]
                if pref_list:
                    bwins = pref_list
            for w in bwins:
                if w["hwnd"] not in before_bwins or before_bwins.get(w["hwnd"]) != w["title"]:
                    chosen = w
                    break
            if chosen:
                break
            if tmod.time() - start_ts >= 0.65 and bwins:
                chosen = bwins[0]
                break

        if chosen:
            force_fg(chosen["hwnd"])
            tmod.sleep(0.12)
            if u32.GetForegroundWindow() != chosen["hwnd"]:
                force_fg(chosen["hwnd"])
            cur_title = get_title(chosen["hwnd"]) or chosen["title"]
            return f"已在浏览器中打开网页并弹出窗口到最前: {url}（当前页面：{cur_title}）"
        return f"已在默认浏览器中打开网页: {url}"

    # 2. 用户直接说“打开浏览器 / 切到浏览器 / 打开 Edge / 打开 Chrome”
    if low in ("浏览器", "默认浏览器", "浏览器页面", "网页", "edge", "microsoft edge", "chrome", "谷歌浏览器"):
        if "chrome" in low or "谷歌" in low:
            want_proc, launch_exe = "chrome.exe", "chrome.exe"
        elif "edge" in low:
            want_proc, launch_exe = "msedge.exe", "msedge.exe"
        else:
            want_proc = get_def_browser() or "msedge.exe"
            launch_exe = want_proc

        existing = [w for w in list_wins() if w["proc"] == want_proc]
        if not existing and low in ("浏览器", "默认浏览器", "浏览器页面", "网页"):
            existing = [w for w in list_wins() if w["proc"] in _BROWSER_PROCS]

        if existing:
            win = existing[0]
            force_fg(win["hwnd"])
            tmod.sleep(0.1)
            if u32.GetForegroundWindow() != win["hwnd"]:
                force_fg(win["hwnd"])
            return f"已将浏览器窗口恢复并弹到屏幕最前：「{win['title']}」"

        before_hwnds = {w["hwnd"] for w in list_wins()}
        shell_open(launch_exe)
        for _ in range(18):
            tmod.sleep(0.15)
            bwins = [w for w in list_wins() if w["hwnd"] not in before_hwnds and w["proc"] in _BROWSER_PROCS]
            if bwins:
                force_fg(bwins[0]["hwnd"])
                return f"已启动浏览器并弹出窗口到最前：「{bwins[0]['title']}」"
        return "已启动浏览器。"

    # 3. 桌面常规软件启动与窗口前置
    before_hwnds = {w["hwnd"] for w in list_wins()}
    launched_label = t
    exe_hint = ""

    if low in ("微信", "wechat", "weixin"):
        existing_wx = [
            w for w in list_wins()
            if "微信" in w["title"] or w["proc"] in ("weixin.exe", "wechat.exe", "wechatappex.exe")
        ]
        if existing_wx:
            force_fg(existing_wx[0]["hwnd"])
            return "已将微信窗口切到屏幕最前。"
        lnk = find_shortcut("微信") or find_shortcut("weixin")
        if lnk:
            shell_open(str(lnk))
            launched_label = lnk.stem
        else:
            return "未在开始菜单找到微信快捷方式。"
    elif low in _BUILTIN_APPS:
        exe = _BUILTIN_APPS[low]
        exe_hint = Path(exe).stem.lower()
        shell_open(exe)
    else:
        lnk = find_shortcut(t)
        if not lnk:
            return f"未在开始菜单或常用列表中找到名为「{t}」的软件。"
        shell_open(str(lnk))
        launched_label = lnk.stem
        exe_hint = lnk.stem.lower()

    chosen_app = None
    for step in range(16):
        tmod.sleep(0.15)
        wins = list_wins()
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
        force_fg(chosen_app["hwnd"])
        tmod.sleep(0.1)
        if u32.GetForegroundWindow() != chosen_app["hwnd"]:
            force_fg(chosen_app["hwnd"])
        return f"已启动并弹出窗口到最前: {launched_label}（「{chosen_app['title']}」）"

    return f"已启动应用: {launched_label}"

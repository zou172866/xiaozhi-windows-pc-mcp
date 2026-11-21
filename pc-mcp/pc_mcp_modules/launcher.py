"""Application & website launcher with foreground window restoration & Start Menu hardening (P2-20, H-7)."""

from __future__ import annotations

import ctypes
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

from . import _com_mta_scope, facade_attr
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
    "资源管理器": "explorer.exe",
    "文件管理器": "explorer.exe",
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
    "control",
    "control panel",
    "控制面板",
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
    "命令行",
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

# P1-4: Use ASCII alphanumeric boundaries instead of Unicode \b so Chinese prefixes (打开cmd.lnk)
# and underscores (my_mshta.lnk) cannot bypass sensitive-tool keyword matching.
_BLOCKED_SHORTCUT_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:cmd|wt|wsl|bash|sh|pwsh|powershell|regedit|regedt32|msconfig|gpedit|secpol|taskmgr|mstsc|mmc|diskpart|bcdedit|netsh|schtasks|wmic|cscript|wscript|mshta|rundll32|certutil|bitsadmin)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_BLOCKED_SHORTCUT_TARGET_EXES = frozenset({
    "cmd.exe",
    "wt.exe",
    "windowsterminal.exe",
    "wsl.exe",
    "wslhost.exe",
    "bash.exe",
    "sh.exe",
    "zsh.exe",
    "mintty.exe",
    "conhost.exe",
    "openconsole.exe",
    "powershell.exe",
    "pwsh.exe",
    "powershell_ise.exe",
    "regedit.exe",
    "regedt32.exe",
    "msconfig.exe",
    "taskmgr.exe",
    "mstsc.exe",
    "mmc.exe",
    "diskpart.exe",
    "bcdedit.exe",
    "netsh.exe",
    "schtasks.exe",
    "wmic.exe",
    "cscript.exe",
    "wscript.exe",
    "mshta.exe",
    "rundll32.exe",
    "certutil.exe",
    "bitsadmin.exe",
    "msiexec.exe",
    "wezterm.exe",
    "wezterm-gui.exe",
    "alacritty.exe",
    "conemu.exe",
    "conemu64.exe",
    "conemuc.exe",
    "conemuc64.exe",
    "putty.exe",
    "tabby.exe",
    "hyper.exe",
    "cmder.exe",
    "git-bash.exe",
    "git-cmd.exe",
    "uninstall.exe",
    "unins000.exe",
    "uninst.exe",
    "uninstaller.exe",
    "recoverydrive.exe",
    "systemreset.exe",
    "control.exe",
    "pcalua.exe",
    "forfiles.exe",
    "regsvr32.exe",
    "hh.exe",
    "perfmon.exe",
    "resmon.exe",
    "eventvwr.exe",
    "dfrgui.exe",
    "iscsicpl.exe",
    "odbcad32.exe",
    "mdsched.exe",
    "msdt.exe",
    "odbcconf.exe",
    "installutil.exe",
    "cmstp.exe",
})

_BLOCKED_SHORTCUT_TARGET_STEMS = frozenset(
    Path(e).stem.lower() for e in _BLOCKED_SHORTCUT_TARGET_EXES
)

_BLOCKED_SHORTCUT_ARG_LOLBIN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:conhost|openconsole|windowsterminal|msiexec|regsvr32|forfiles|pcalua|installutil|cmstp|msdt|odbcconf)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_BLOCKED_SHORTCUT_TARGET_EXE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(re.escape(e) for e in sorted(_BLOCKED_SHORTCUT_TARGET_EXES))
    + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_BLOCKED_83_PREFIXES = frozenset(
    Path(e).stem.lower()[:6]
    for e in _BLOCKED_SHORTCUT_TARGET_EXES
    if len(Path(e).stem) > 6
)
_SHORT_83_STEM_RE = re.compile(r"^([a-z0-9_\-]{1,6})~\d+$", re.IGNORECASE)

_BLOCKED_SHORTCUT_TARGET_EXTS = frozenset({
    ".bat",
    ".cmd",
    ".ps1",
    ".vbs",
    ".vbe",
    ".js",
    ".jse",
    ".wsf",
    ".wsh",
    ".hta",
    ".msc",
    ".reg",
    ".scr",
    ".cpl",
})

_LNK_HEADER_MAGIC = b"\x4c\x00\x00\x00"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_COM_GUIDS_CACHE: Optional[tuple[_GUID, _GUID, _GUID]] = None


def _get_shell_link_guids() -> tuple[_GUID, _GUID, _GUID]:
    global _COM_GUIDS_CACHE
    if _COM_GUIDS_CACHE is not None:
        return _COM_GUIDS_CACHE
    ole32 = ctypes.windll.ole32

    def _parse(s: str) -> _GUID:
        g = _GUID()
        ole32.IIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
        return g

    _COM_GUIDS_CACHE = (
        _parse("{00021401-0000-0000-C000-000000000046}"),  # CLSID_ShellLink
        _parse("{000214F9-0000-0000-C000-000000000046}"),  # IID_IShellLinkW
        _parse("{0000010B-0000-0000-C000-000000000046}"),  # IID_IPersistFile
    )
    return _COM_GUIDS_CACHE


def _resolve_long_path_windows(path_str: str) -> str:
    """Resolve Windows 8.3 short paths (e.g. POWERS~1.EXE) to canonical long names when available (P1-4)."""
    if os.name != "nt" or not path_str:
        return path_str
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = int(ctypes.windll.kernel32.GetLongPathNameW(path_str, buf, 1024) or 0)
        if 0 < n < 1024 and buf.value:
            return buf.value
    except Exception:
        pass
    return path_str


def _resolve_lnk_targets(lnk: Path) -> Optional[tuple[list[str], str]]:
    """Resolve ALL candidate TargetPath forms (SLGP_RAWPATH + resolved path) and Arguments via IShellLinkW (P1-4).

    Ensures COM interfaces are ALWAYS released inside `_com_mta_scope()` BEFORE `CoUninitialize()`,
    and returns both raw and resolved target paths so neither EnvironmentVariableDataBlock nor
    8.3 short paths can mask a dangerous target.
    """
    if os.name != "nt":
        return None
    try:
        with open(lnk, "rb") as f:
            header = f.read(76)
        if len(header) < 76 or header[:4] != _LNK_HEADER_MAGIC:
            return None
    except OSError:
        return None

    ole32 = ctypes.windll.ole32
    clsid_sl, iid_sl, iid_pf = _get_shell_link_guids()
    try:
        with _com_mta_scope():
            psl = ctypes.c_void_p()
            ppf = ctypes.c_void_p()
            try:
                hr = ole32.CoCreateInstance(
                    ctypes.byref(clsid_sl),
                    None,
                    1,  # CLSCTX_INPROC_SERVER
                    ctypes.byref(iid_sl),
                    ctypes.byref(psl),
                )
                if hr != 0 or not psl.value:
                    return ["__parse_error__"], ""
                vtbl_sl = ctypes.cast(psl, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                qi_fn = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
                )(vtbl_sl[0])
                hr = qi_fn(psl, ctypes.byref(iid_pf), ctypes.byref(ppf))
                if hr != 0 or not ppf.value:
                    return ["__parse_error__"], ""
                vtbl_pf = ctypes.cast(ppf, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                pf_load = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32
                )(vtbl_pf[5])
                hr = pf_load(ppf, str(lnk), 0)  # STGM_READ
                if hr != 0:
                    return ["__parse_error__"], ""
                sl_get_path = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32
                )(vtbl_sl[3])
                sl_get_args = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int
                )(vtbl_sl[10])
                buf_raw = ctypes.create_unicode_buffer(1024)
                buf_res = ctypes.create_unicode_buffer(1024)
                buf_args = ctypes.create_unicode_buffer(2048)
                sl_get_path(psl, buf_raw, 1024, None, 0x4)  # SLGP_RAWPATH
                sl_get_path(psl, buf_res, 1024, None, 0x0)  # resolved path
                sl_get_args(psl, buf_args, 2048)
                targets: list[str] = []
                for raw_v in (buf_raw.value, buf_res.value):
                    s = (raw_v or "").strip()
                    if s and s not in targets:
                        targets.append(s)
                return targets, (buf_args.value or "").strip()
            finally:
                # Release COM pointers BEFORE _com_mta_scope() calls CoUninitialize().
                if ppf.value:
                    try:
                        vtbl_pf = ctypes.cast(ppf, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl_pf[2])(ppf)
                    except Exception:
                        pass
                if psl.value:
                    try:
                        vtbl_sl = ctypes.cast(psl, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl_sl[2])(psl)
                    except Exception:
                        pass
    except Exception:
        return ["__parse_error__"], ""


def _resolve_lnk_target(lnk: Path) -> Optional[tuple[str, str]]:
    """Backward-compatible wrapper returning primary target path and arguments from IShellLinkW (P1-4)."""
    res = _resolve_lnk_targets(lnk)
    if res is None:
        return None
    targets, args = res
    if not targets:
        return "", args
    primary = os.path.expandvars(targets[0]).strip() or targets[0]
    return primary, args


def _is_blocked_shortcut_target(target_path: str, arguments: str = "") -> bool:
    """Block shortcuts whose resolved TargetPath or Arguments invoke a shell/admin/script/uninstaller binary (P1-4)."""
    if target_path == "__parse_error__":
        return True
    raw_candidates: list[str] = []

    def _add_cand(val: str) -> None:
        c = (val or "").strip().strip('"').rstrip(" .")
        if c and c not in raw_candidates:
            raw_candidates.append(c)

    if target_path:
        _add_cand(target_path)
        expanded = os.path.expandvars(target_path)
        _add_cand(expanded)
        for base_c in list(raw_candidates):
            long_c = _resolve_long_path_windows(base_c)
            _add_cand(long_c)

    for cand in raw_candidates:
        p = Path(cand)
        name_low = p.name.lower()
        stem_low = p.stem.lower()
        ext_low = p.suffix.lower()
        if ext_low in _BLOCKED_SHORTCUT_TARGET_EXTS:
            return True
        if name_low in _BLOCKED_SHORTCUT_TARGET_EXES:
            return True
        if ext_low in ("", ".exe", ".com") and stem_low in _BLOCKED_SHORTCUT_TARGET_STEMS:
            return True
        if stem_low in _BLOCKED_SHORTCUT_EXACT_STEMS:
            return True
        if stem_low.startswith("unins") or "uninstall" in stem_low or "卸载" in stem_low:
            return True
        if _BLOCKED_SHORTCUT_WORD_RE.search(stem_low):
            return True
        m_83 = _SHORT_83_STEM_RE.match(stem_low)
        if m_83 and ext_low in (".exe", ".com", "") and m_83.group(1).lower() in _BLOCKED_83_PREFIXES:
            return True

    args_str = (arguments or "").strip()
    if args_str:
        arg_variants = [args_str]
        exp_args = os.path.expandvars(args_str).strip()
        if exp_args and exp_args != args_str:
            arg_variants.append(exp_args)
        for av in arg_variants:
            av_low = av.lower()
            if _BLOCKED_SHORTCUT_WORD_RE.search(av):
                return True
            if _BLOCKED_SHORTCUT_ARG_LOLBIN_RE.search(av):
                return True
            if _BLOCKED_SHORTCUT_TARGET_EXE_RE.search(av):
                return True
            if any(tok in av_low for tok in ("uninstall", "卸载")):
                return True
            if any(
                re.search(rf"{re.escape(ext)}(?![A-Za-z0-9])", av_low)
                for ext in _BLOCKED_SHORTCUT_TARGET_EXTS
            ):
                return True
    return False


_shortcut_cache_lock = threading.Lock()
_shortcut_cache: dict[tuple[str, ...], dict] = {}


def _clear_start_menu_cache() -> None:
    """Clear cached Start Menu shortcut index (useful in unit tests)."""
    with _shortcut_cache_lock:
        _shortcut_cache.clear()


def _is_blocked_shortcut(lnk: Path, *, root_dir: Optional[Path] = None) -> bool:
    """Return True if a Start Menu shortcut targets a sensitive/system tool or uninstaller (H-7, P1-4)."""
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

    # P1-4: Resolve real .lnk TargetPath (both raw & resolved) + Arguments via IShellLinkW so renamed shortcuts cannot bypass
    resolved = _resolve_lnk_targets(lnk)
    if resolved is not None:
        t_paths, t_args = resolved
        if t_paths:
            for t_path in t_paths:
                if _is_blocked_shortcut_target(t_path, t_args):
                    return True
        elif t_args and _is_blocked_shortcut_target("", t_args):
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
        if lnk and not _is_blocked_shortcut(lnk):
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
        if not lnk or _is_blocked_shortcut(lnk):
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

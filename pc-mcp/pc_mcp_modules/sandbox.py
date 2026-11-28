"""Windows native AppContainer workspace sandbox & Win32 handle-level escape guard (Phase 4).

Security invariants:
- Fail-closed: if AppContainer or Win32 Job Object creation is unavailable on the host OS,
  script execution is REFUSED (never silently falls back to unsandboxed host execution).
- Zero network capabilities (`Capabilities=None, CapabilityCount=0`): kernel blocks outbound sockets.
- Isolated workspace root: defaults to `pc-mcp/state/sandbox_workspace/`, overridable ONLY by
  host-private `.env` variable `PC_MCP_SANDBOX_ROOT` (never mutable from container or 18080).
- Handle-level anti-escape validation on every path segment using Win32 `CreateFileW` with
  `FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS`, `GetFileInformationByHandle`
  (rejecting `FILE_ATTRIBUTE_REPARSE_POINT` symlinks/junctions AND hardlinks `nNumberOfLinks > 1`),
  and `GetFinalPathNameByHandleW` canonical containment check.
- Minimal environment: strips all host secrets (`PC_MCP_TOKEN`, `*_API_KEY`, `*_SECRET`, etc.),
  runs isolated `python.exe -I`, enforces Win32 Job Object memory/process limits & timeout kill,
  caps stdout/stderr at 16 KB, and redacts any secret-shaped patterns before returning.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import STATE_DIR, _get_env_key, facade_attr
from .security import _check_permission, _write_l2_audit_log, is_emergency_stop_active

APPCONTAINER_PROFILE_NAME = "XiaoZhi.PcMcp.WorkspaceSandbox"
APPCONTAINER_DISPLAY_NAME = "XiaoZhi PC MCP Workspace Sandbox"
DEFAULT_SANDBOX_ROOT = STATE_DIR / "sandbox_workspace"

MAX_FILE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
DEFAULT_TIMEOUT_SECONDS = 15
MAX_TIMEOUT_SECONDS = 60
JOB_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
JOB_ACTIVE_PROCESS_LIMIT = 8

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
_APPCONTAINER_SID_RE = re.compile(r"^S-1-15-2(?:-\d+){7}$")
_SECRET_REDACT_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[已脱敏密钥]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[已脱敏密钥]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[已脱敏密钥]"),
    (re.compile(r"(?i)\b(bearer|token|apikey|api_key|secret|password)\s*[:=]\s*[^\s,;\"']{6,}"), r"\1=[已脱敏]"),
)

# Win32 constants
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_NAME_NORMALIZED = 0x0

HANDLE_FLAG_INHERIT = 0x00000001
STARTF_USESHOWWINDOW = 0x00000001
STARTF_USESTDHANDLES = 0x00000100
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
HRESULT_ALREADY_EXISTS = -2147024713  # 0x800700B7 as signed 32-bit int

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class SandboxSecurityError(PermissionError):
    """Raised when a sandbox path or process violates containment invariants."""


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_acl_lock = threading.Lock()
_acl_prepared_keys: set[Tuple[str, str]] = set()
_win32_apis_ready = False
_kernel32 = None
_advapi32 = None
_userenv = None


def _init_win32() -> None:
    global _win32_apis_ready, _kernel32, _advapi32, _userenv
    if _win32_apis_ready:
        return
    if sys.platform != "win32":
        raise SandboxSecurityError("Windows AppContainer 沙箱仅支持 Windows 宿主机")

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    a32 = ctypes.WinDLL("advapi32", use_last_error=True)
    uenv = ctypes.WinDLL("userenv", use_last_error=True)

    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k32.GetFileInformationByHandle.restype = wintypes.BOOL
    k32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.LocalFree.restype = ctypes.c_void_p
    k32.LocalFree.argtypes = [ctypes.c_void_p]

    k32.CreatePipe.restype = wintypes.BOOL
    k32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD,
    ]
    k32.SetHandleInformation.restype = wintypes.BOOL
    k32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    k32.ReadFile.restype = wintypes.BOOL
    k32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    k32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    k32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.DeleteProcThreadAttributeList.restype = None
    k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k32.CreateProcessW.restype = wintypes.BOOL
    k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    k32.ResumeThread.restype = wintypes.DWORD
    k32.ResumeThread.argtypes = [wintypes.HANDLE]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]

    uenv.CreateAppContainerProfile.restype = ctypes.c_long
    uenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]
    uenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    uenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
    ]
    a32.ConvertSidToStringSidW.restype = wintypes.BOOL
    a32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    a32.FreeSid.restype = ctypes.c_void_p
    a32.FreeSid.argtypes = [ctypes.c_void_p]

    _kernel32 = k32
    _advapi32 = a32
    _userenv = uenv
    _win32_apis_ready = True


def _redact_output(text: str) -> str:
    out = str(text or "")
    for pat, repl in _SECRET_REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


def get_sandbox_root(root_override: Optional[Path] = None) -> Path:
    if root_override is not None:
        root = Path(root_override).resolve()
    else:
        env_dir = _get_env_key("PC_MCP_SANDBOX_ROOT")
        root = Path(env_dir).resolve() if env_dir else DEFAULT_SANDBOX_ROOT.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _normalize_nt_path(raw_final: str) -> str:
    s = str(raw_final or "")
    if s.startswith("\\\\?\\UNC\\"):
        s = "\\\\" + s[8:]
    elif s.startswith("\\\\?\\"):
        s = s[4:]
    return os.path.normcase(os.path.normpath(s))


def inspect_handle_no_escape(path: Path, *, root_canon: str, allow_missing: bool = False) -> Optional[str]:
    """Inspect a path via Win32 native handle to block symlinks, junctions, and hardlinks."""
    _init_win32()
    p_str = str(path)
    h = _kernel32.CreateFileW(
        p_str,
        0,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if h == INVALID_HANDLE_VALUE or h == 0:
        err = ctypes.get_last_error()
        if allow_missing and err in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
            return None
        raise SandboxSecurityError(f"无法安全打开目标句柄 (Win32 err={err}): {path.name}")
    try:
        info = _BY_HANDLE_FILE_INFORMATION()
        if not _kernel32.GetFileInformationByHandle(h, ctypes.byref(info)):
            raise SandboxSecurityError(f"读取文件句柄元信息失败: {path.name}")
        if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise SandboxSecurityError(f"安全拦截：禁止访问符号链接或目录联接（Reparse Point）: {path.name}")
        is_dir = bool(info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
        if not is_dir and info.nNumberOfLinks > 1:
            raise SandboxSecurityError(f"安全拦截：禁止访问 NTFS 硬链接文件（links={info.nNumberOfLinks}）: {path.name}")

        buf = ctypes.create_unicode_buffer(1024)
        n = _kernel32.GetFinalPathNameByHandleW(h, buf, len(buf), FILE_NAME_NORMALIZED)
        if n == 0 or n >= len(buf):
            raise SandboxSecurityError("无法获取句柄规范物理路径")
        final_canon = _normalize_nt_path(buf.value)
        if final_canon != root_canon and not final_canon.startswith(root_canon + os.sep):
            raise SandboxSecurityError("安全拦截：句柄规范路径逃逸出沙箱工作区")
        return final_canon
    finally:
        _kernel32.CloseHandle(h)


def safe_resolve_workspace_path(
    rel_path: str,
    *,
    root: Optional[Path] = None,
    must_exist: bool = False,
) -> Path:
    """Resolve a relative path strictly inside sandbox root with per-segment Win32 handle verification."""
    ws_root = get_sandbox_root(root)
    _init_win32()
    # Verify root itself via handle to obtain authoritative canonical path
    h_root = _kernel32.CreateFileW(
        str(ws_root),
        0,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if h_root == INVALID_HANDLE_VALUE or h_root == 0:
        raise SandboxSecurityError("无法打开沙箱工作区根目录句柄")
    try:
        rinfo = _BY_HANDLE_FILE_INFORMATION()
        if not _kernel32.GetFileInformationByHandle(h_root, ctypes.byref(rinfo)):
            raise SandboxSecurityError("无法读取沙箱根目录属性")
        if rinfo.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise SandboxSecurityError("沙箱根目录不能是符号链接或目录联接")
        rbuf = ctypes.create_unicode_buffer(1024)
        rn = _kernel32.GetFinalPathNameByHandleW(h_root, rbuf, len(rbuf), FILE_NAME_NORMALIZED)
        if rn == 0 or rn >= len(rbuf):
            raise SandboxSecurityError("无法解析沙箱根目录物理路径")
        root_canon = _normalize_nt_path(rbuf.value)
    finally:
        _kernel32.CloseHandle(h_root)

    raw = str(rel_path or "").strip()
    if not raw or raw in (".", "./"):
        return ws_root
    if "\x00" in raw or ":" in raw or raw.startswith(("/", "\\")):
        raise SandboxSecurityError("禁止使用绝对路径、盘符、UNC 或非法控制字符")

    parts = [p for p in re.split(r"[\\/]+", raw) if p]
    if not parts:
        return ws_root
    if len(parts) > 12:
        raise SandboxSecurityError("沙箱相对路径层级过深（上限 12 层）")

    cur = ws_root
    for idx, seg in enumerate(parts):
        if seg in (".", ".."):
            raise SandboxSecurityError("禁止在沙箱路径中使用 `.` 或 `..` 穿越")
        stem = seg.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise SandboxSecurityError(f"禁止使用 Windows 保留设备名: {seg}")
        if any(ch in seg for ch in '<>"|?*'):
            raise SandboxSecurityError(f"路径段包含非法字符: {seg}")
        cur = cur / seg
        is_last = idx == len(parts) - 1
        inspect_handle_no_escape(cur, root_canon=root_canon, allow_missing=(not must_exist if is_last else True))

    return cur


# ---------------------------------------------------------------------------
# Workspace file operations (full-mode gated via MCP facade)
# ---------------------------------------------------------------------------

def sandbox_file_op(
    action: str,
    rel_path: str = "",
    content: str = "",
    *,
    root: Optional[Path] = None,
) -> str:
    ws_root = get_sandbox_root(root)
    act = str(action or "").strip().lower()

    if act == "list":
        target = safe_resolve_workspace_path(rel_path, root=ws_root, must_exist=True)
        if not target.is_dir():
            return f"路径不是目录: {rel_path}"
        entries: List[str] = []
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:100]:
            try:
                safe_resolve_workspace_path(str(item.relative_to(ws_root)), root=ws_root, must_exist=True)
            except SandboxSecurityError:
                entries.append(f"[已屏蔽重解析项] {item.name}")
                continue
            kind = "DIR " if item.is_dir() else f"{item.stat().st_size}B"
            entries.append(f"- {item.name} ({kind})")
        if not entries:
            return f"沙箱工作区 `{rel_path or '.'}` 当前为空。"
        return f"沙箱工作区 `{rel_path or '.'}` 内容（共 {len(entries)} 项）：\n" + "\n".join(entries)

    if act == "read":
        target = safe_resolve_workspace_path(rel_path, root=ws_root, must_exist=True)
        if target.is_dir():
            raise ValueError("目标是目录，请使用 action='list'")
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"文件体积超过沙箱上限（{MAX_FILE_BYTES} 字节）")
        data = target.read_text(encoding="utf-8", errors="replace")
        return _redact_output(data[:MAX_OUTPUT_BYTES])

    if act == "write":
        if not str(rel_path or "").strip():
            raise ValueError("写入文件必须提供相对路径 rel_path")
        encoded = str(content or "").encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError(f"写入内容超过单文件上限（{MAX_FILE_BYTES} 字节）")
        target = safe_resolve_workspace_path(rel_path, root=ws_root, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-verify parent and target right before writing
        safe_resolve_workspace_path(rel_path, root=ws_root, must_exist=False)
        target.write_bytes(encoded)
        return f"已写入沙箱文件 `{rel_path}`（{len(encoded)} 字节）。"

    if act == "delete":
        if not str(rel_path or "").strip():
            raise ValueError("禁止删除沙箱根目录")
        target = safe_resolve_workspace_path(rel_path, root=ws_root, must_exist=True)
        if target == ws_root:
            raise SandboxSecurityError("禁止删除沙箱根目录")
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()
        return f"已删除沙箱路径 `{rel_path}`。"

    raise ValueError("action 仅支持 list / read / write / delete")


# ---------------------------------------------------------------------------
# Windows Native AppContainer Process Execution
# ---------------------------------------------------------------------------

def _ensure_appcontainer_sid(profile_name: str = APPCONTAINER_PROFILE_NAME) -> Tuple[ctypes.c_void_p, str]:
    _init_win32()
    sid_ptr = ctypes.c_void_p(None)
    hr = _userenv.CreateAppContainerProfile(
        profile_name,
        APPCONTAINER_DISPLAY_NAME,
        "Zero-network isolated workspace sandbox for XiaoZhi PC MCP",
        None,
        0,
        ctypes.byref(sid_ptr),
    )
    if hr < 0:
        if hr == HRESULT_ALREADY_EXISTS or ctypes.get_last_error() == 183:
            hr2 = _userenv.DeriveAppContainerSidFromAppContainerName(profile_name, ctypes.byref(sid_ptr))
            if hr2 < 0 or not sid_ptr.value:
                raise SandboxSecurityError(f"无法推导 AppContainer SID (HRESULT=0x{hr2 & 0xFFFFFFFF:08x})")
        else:
            raise SandboxSecurityError(f"无法创建 Windows AppContainer Profile (HRESULT=0x{hr & 0xFFFFFFFF:08x})")

    str_sid = wintypes.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_sid)):
        _advapi32.FreeSid(sid_ptr)
        raise SandboxSecurityError("无法转换 AppContainer SID 为字符串")
    try:
        sid_text = str(str_sid.value or "").strip()
    finally:
        _kernel32.LocalFree(str_sid)

    if not _APPCONTAINER_SID_RE.match(sid_text):
        _advapi32.FreeSid(sid_ptr)
        raise SandboxSecurityError(f"AppContainer SID 格式异常: {sid_text!r}")
    return sid_ptr, sid_text


def _grant_appcontainer_acl(path: Path, sid_str: str, perm: str) -> None:
    """Grant minimal ACL (`(OI)(CI)M` or `(OI)(CI)RX`) ONLY to our specific AppContainer SID."""
    if not _APPCONTAINER_SID_RE.match(sid_str):
        raise SandboxSecurityError("非法 AppContainer SID")
    key = (str(path.resolve()), f"{sid_str}:{perm}")
    with _acl_lock:
        if key in _acl_prepared_keys:
            return
    proc = subprocess.run(
        ["icacls", str(path.resolve()), "/grant", f"*{sid_str}:{perm}", "/Q"],
        capture_output=True,
        timeout=15,
        creationflags=CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise SandboxSecurityError(f"配置沙箱目录最小 ACL 失败 (rc={proc.returncode})")
    with _acl_lock:
        _acl_prepared_keys.add(key)


def _resolve_base_python_exe() -> Tuple[Path, Path]:
    """Return `(base_python_exe, base_python_dir)` outside the repo venv so AppContainer only sees stdlib."""
    base_dir = Path(sys.base_prefix).resolve()
    cand = base_dir / "python.exe"
    if cand.is_file():
        return cand, base_dir
    exe = Path(sys.executable).resolve()
    return exe, exe.parent


def _build_minimal_env_block(ws_root: Path, base_py_dir: Path) -> ctypes.Array:
    """Build a sorted Unicode environment block with ZERO host tokens or API keys."""
    sys_root = os.environ.get("SystemRoot") or r"C:\Windows"
    sys_drive = os.environ.get("SystemDrive") or "C:"
    tmp_dir = ws_root / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env_items = {
        "APPDATA": str(tmp_dir),
        "ComSpec": f"{sys_root}\\System32\\cmd.exe",
        "LOCALAPPDATA": str(tmp_dir),
        "OS": os.environ.get("OS") or "Windows_NT",
        "PATH": f"{base_py_dir};{sys_root}\\System32;{sys_root}",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "SystemDrive": sys_drive,
        "SystemRoot": sys_root,
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "USERPROFILE": str(ws_root),
        "WINDIR": sys_root,
        "XIAOZHI_SANDBOX": "1",
    }
    ordered = sorted(env_items.items(), key=lambda kv: kv[0].upper())
    blob = "\0".join(f"{k}={v}" for k, v in ordered) + "\0\0"
    buf = ctypes.create_unicode_buffer(len(blob))
    buf[: len(blob)] = blob
    return buf


def _create_sandbox_job() -> wintypes.HANDLE:
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise SandboxSecurityError("无法创建 Win32 Job Object 沙箱资源限制器")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
    )
    info.BasicLimitInformation.ActiveProcessLimit = JOB_ACTIVE_PROCESS_LIMIT
    info.ProcessMemoryLimit = JOB_MEMORY_LIMIT_BYTES
    info.JobMemoryLimit = JOB_MEMORY_LIMIT_BYTES
    ok = _kernel32.SetInformationJobObject(
        job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        err = ctypes.get_last_error()
        _kernel32.CloseHandle(job)
        raise SandboxSecurityError(f"配置 Job Object 沙箱限额失败 (Win32 err={err})")
    return job


def _drain_pipe(handle: wintypes.HANDLE, bucket: bytearray, limit: int) -> None:
    buf = ctypes.create_string_buffer(4096)
    read_n = wintypes.DWORD(0)
    while True:
        ok = _kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read_n), None)
        if not ok or read_n.value == 0:
            break
        if len(bucket) < limit:
            take = min(read_n.value, limit - len(bucket))
            bucket.extend(buf.raw[:take])


def run_python_in_appcontainer(
    *,
    script_rel_path: str = "",
    inline_python: str = "",
    timeout_sec: int = DEFAULT_TIMEOUT_SECONDS,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute isolated Python (`-I`) strictly inside a zero-network Windows AppContainer sandbox."""
    _init_win32()
    ws_root = get_sandbox_root(root)
    timeout = max(1, min(MAX_TIMEOUT_SECONDS, int(timeout_sec or DEFAULT_TIMEOUT_SECONDS)))

    has_script = bool(str(script_rel_path or "").strip())
    has_inline = bool(str(inline_python or "").strip())
    if has_script == has_inline:
        raise ValueError("请二选一提供 script_rel_path（工作区内脚本路径）或 inline_python（内联 Python 代码）")

    py_exe, py_dir = _resolve_base_python_exe()
    if has_script:
        script_path = safe_resolve_workspace_path(script_rel_path, root=ws_root, must_exist=True)
        if script_path.is_dir():
            raise ValueError("script_rel_path 必须指向脚本文件而非目录")
        cmd_line = subprocess.list2cmdline([str(py_exe), "-I", str(script_path)])
    else:
        code = str(inline_python)
        if len(code.encode("utf-8")) > 32 * 1024:
            raise ValueError("inline_python 代码长度不能超过 32KB")
        cmd_line = subprocess.list2cmdline([str(py_exe), "-I", "-c", code])

    sid_ptr, sid_str = _ensure_appcontainer_sid()
    handles_to_close: List[wintypes.HANDLE] = []
    attr_list_ptr = None
    job_handle = None
    t0 = time.monotonic()

    try:
        # Grant minimal ACL ONLY to sandbox workspace (RW) and base Python runtime (RX)
        _grant_appcontainer_acl(ws_root, sid_str, "(OI)(CI)M")
        _grant_appcontainer_acl(py_dir, sid_str, "(OI)(CI)RX")

        # Create stdout & stderr pipes (child inherits ONLY write ends via HANDLE_LIST)
        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = None
        sa.bInheritHandle = True

        out_r, out_w = wintypes.HANDLE(), wintypes.HANDLE()
        err_r, err_w = wintypes.HANDLE(), wintypes.HANDLE()
        if not _kernel32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), ctypes.byref(sa), 0):
            raise SandboxSecurityError("创建沙箱 stdout 管道失败")
        handles_to_close.extend([out_r, out_w])
        if not _kernel32.CreatePipe(ctypes.byref(err_r), ctypes.byref(err_w), ctypes.byref(sa), 0):
            raise SandboxSecurityError("创建沙箱 stderr 管道失败")
        handles_to_close.extend([err_r, err_w])

        _kernel32.SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0)
        _kernel32.SetHandleInformation(err_r, HANDLE_FLAG_INHERIT, 0)

        # Build PROC_THREAD_ATTRIBUTE_LIST with 2 attributes: SECURITY_CAPABILITIES + HANDLE_LIST
        sec_caps = _SECURITY_CAPABILITIES()
        sec_caps.AppContainerSid = sid_ptr
        sec_caps.Capabilities = None  # Zero capabilities => kernel-enforced zero network
        sec_caps.CapabilityCount = 0
        sec_caps.Reserved = 0

        inherit_list = (wintypes.HANDLE * 2)(out_w, err_w)

        attr_size = ctypes.c_size_t(0)
        _kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(attr_size))
        attr_buf = ctypes.create_string_buffer(attr_size.value)
        attr_list_ptr = ctypes.cast(attr_buf, ctypes.c_void_p)
        if not _kernel32.InitializeProcThreadAttributeList(attr_list_ptr, 2, 0, ctypes.byref(attr_size)):
            raise SandboxSecurityError(f"初始化进程属性表失败 (Win32 err={ctypes.get_last_error()})")

        if not _kernel32.UpdateProcThreadAttribute(
            attr_list_ptr,
            0,
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(sec_caps),
            ctypes.sizeof(sec_caps),
            None,
            None,
        ):
            raise SandboxSecurityError(f"注入 AppContainer 安全能力失败 (Win32 err={ctypes.get_last_error()})")

        if not _kernel32.UpdateProcThreadAttribute(
            attr_list_ptr,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            ctypes.byref(inherit_list),
            ctypes.sizeof(inherit_list),
            None,
            None,
        ):
            raise SandboxSecurityError(f"注入受限句柄白名单失败 (Win32 err={ctypes.get_last_error()})")

        job_handle = _create_sandbox_job()

        si_ex = _STARTUPINFOEXW()
        si_ex.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        si_ex.StartupInfo.dwFlags = STARTF_USESHOWWINDOW | STARTF_USESTDHANDLES
        si_ex.StartupInfo.wShowWindow = 0
        si_ex.StartupInfo.hStdInput = None
        si_ex.StartupInfo.hStdOutput = out_w
        si_ex.StartupInfo.hStdError = err_w
        si_ex.lpAttributeList = attr_list_ptr

        pi = _PROCESS_INFORMATION()
        cmd_buf = ctypes.create_unicode_buffer(cmd_line)
        env_buf = _build_minimal_env_block(ws_root, py_dir)

        flags = (
            EXTENDED_STARTUPINFO_PRESENT
            | CREATE_NO_WINDOW
            | CREATE_SUSPENDED
            | CREATE_UNICODE_ENVIRONMENT
        )
        ok = _kernel32.CreateProcessW(
            str(py_exe),
            cmd_buf,
            None,
            None,
            True,
            flags,
            ctypes.byref(env_buf),
            str(ws_root),
            ctypes.byref(si_ex.StartupInfo),
            ctypes.byref(pi),
        )
        if not ok:
            raise SandboxSecurityError(f"启动 Windows AppContainer 沙箱进程失败 (Win32 err={ctypes.get_last_error()})")

        handles_to_close.extend([pi.hProcess, pi.hThread])

        if not _kernel32.AssignProcessToJobObject(job_handle, pi.hProcess):
            _kernel32.TerminateProcess(pi.hProcess, 1)
            raise SandboxSecurityError(f"绑定沙箱进程到 Job Object 失败 (Win32 err={ctypes.get_last_error()})")

        # Close parent's copies of pipe write ends so reader sees EOF when child exits
        _kernel32.CloseHandle(out_w)
        _kernel32.CloseHandle(err_w)
        handles_to_close.remove(out_w)
        handles_to_close.remove(err_w)

        out_buf = bytearray()
        err_buf = bytearray()
        t_out = threading.Thread(target=_drain_pipe, args=(out_r, out_buf, MAX_OUTPUT_BYTES), daemon=True)
        t_err = threading.Thread(target=_drain_pipe, args=(err_r, err_buf, MAX_OUTPUT_BYTES), daemon=True)
        t_out.start()
        t_err.start()

        _kernel32.ResumeThread(pi.hThread)

        wait_rc = _kernel32.WaitForSingleObject(pi.hProcess, int(timeout * 1000))
        timed_out = wait_rc == WAIT_TIMEOUT
        if timed_out:
            _kernel32.TerminateJobObject(job_handle, 124)
            _kernel32.WaitForSingleObject(pi.hProcess, 2000)

        exit_code = wintypes.DWORD(0)
        _kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)

        stdout_text = _redact_output(out_buf.decode("utf-8", errors="replace"))
        stderr_text = _redact_output(err_buf.decode("utf-8", errors="replace"))
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        return {
            "ok": (not timed_out) and int(exit_code.value) == 0,
            "exit_code": 124 if timed_out else int(exit_code.value),
            "timed_out": timed_out,
            "elapsed_ms": elapsed_ms,
            "appcontainer_sid": sid_str,
            "workspace": str(ws_root),
            "stdout": stdout_text,
            "stderr": stderr_text,
        }
    finally:
        if attr_list_ptr is not None:
            try:
                _kernel32.DeleteProcThreadAttributeList(attr_list_ptr)
            except Exception:
                pass
        if job_handle is not None:
            try:
                _kernel32.CloseHandle(job_handle)
            except Exception:
                pass
        for h in handles_to_close:
            try:
                _kernel32.CloseHandle(h)
            except Exception:
                pass
        if sid_ptr and sid_ptr.value:
            try:
                _advapi32.FreeSid(sid_ptr)
            except Exception:
                pass


def handle_sandbox_file_tool(action: str, rel_path: str = "", content: str = "") -> str:
    """Audited full-mode MCP handler for sandbox workspace file operations."""
    check_perm = facade_attr("_check_permission", _check_permission)
    estop_fn = facade_attr("is_emergency_stop_active", is_emergency_stop_active)
    audit_fn = facade_attr("_write_l2_audit_log", _write_l2_audit_log)

    err = check_perm("L3")
    if err:
        audit_fn(tool="pc_sandbox_file", action=action, outcome="blocked", reason="mode_blocked", target=rel_path, detail=err)
        return err
    if estop_fn():
        msg = "安全急停已触发（Ctrl+Alt+Pause），已中止沙箱文件操作。"
        audit_fn(tool="pc_sandbox_file", action=action, outcome="blocked", reason="emergency_stop", target=rel_path, detail=msg)
        return msg
    try:
        out = sandbox_file_op(action, rel_path=rel_path, content=content)
        audit_fn(tool="pc_sandbox_file", action=action, outcome="ok", reason="", target=rel_path, detail=out[:120])
        return out
    except (SandboxSecurityError, ValueError) as exc:
        msg = f"沙箱文件操作被拒绝：{exc}"
        audit_fn(tool="pc_sandbox_file", action=action, outcome="blocked", reason="sandbox_guard", target=rel_path, detail=msg)
        return msg
    except Exception as exc:
        msg = f"沙箱文件操作异常：{type(exc).__name__}: {exc}"
        audit_fn(tool="pc_sandbox_file", action=action, outcome="error", reason=type(exc).__name__, target=rel_path, detail=msg)
        return msg


def handle_sandbox_run_tool(script_rel_path: str = "", inline_python: str = "", timeout_sec: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Audited full-mode MCP handler for zero-network Windows AppContainer Python execution."""
    check_perm = facade_attr("_check_permission", _check_permission)
    estop_fn = facade_attr("is_emergency_stop_active", is_emergency_stop_active)
    audit_fn = facade_attr("_write_l2_audit_log", _write_l2_audit_log)
    target = script_rel_path or "<inline_python>"

    err = check_perm("L3")
    if err:
        audit_fn(tool="pc_sandbox_run", action="run_appcontainer", outcome="blocked", reason="mode_blocked", target=target, detail=err)
        return err
    if estop_fn():
        msg = "安全急停已触发（Ctrl+Alt+Pause），已中止 AppContainer 沙箱执行。"
        audit_fn(tool="pc_sandbox_run", action="run_appcontainer", outcome="blocked", reason="emergency_stop", target=target, detail=msg)
        return msg
    try:
        res = run_python_in_appcontainer(script_rel_path=script_rel_path, inline_python=inline_python, timeout_sec=timeout_sec)
        outcome = "ok" if res["ok"] else ("timeout" if res["timed_out"] else "nonzero")
        audit_fn(tool="pc_sandbox_run", action="run_appcontainer", outcome=outcome, reason=f"rc={res['exit_code']}", target=target, detail=f"{res['elapsed_ms']}ms")
        parts = [f"[AppContainer 沙箱退出码={res['exit_code']}，耗时 {res['elapsed_ms']}ms{'，已超时强杀' if res['timed_out'] else ''}]"]
        if res["stdout"]:
            parts.append("STDOUT:\n" + res["stdout"])
        if res["stderr"]:
            parts.append("STDERR:\n" + res["stderr"])
        return "\n".join(parts)
    except (SandboxSecurityError, ValueError) as exc:
        msg = f"AppContainer 沙箱拒绝执行：{exc}"
        audit_fn(tool="pc_sandbox_run", action="run_appcontainer", outcome="blocked", reason="sandbox_guard", target=target, detail=msg)
        return msg
    except Exception as exc:
        msg = f"AppContainer 沙箱异常：{type(exc).__name__}: {exc}"
        audit_fn(tool="pc_sandbox_run", action="run_appcontainer", outcome="error", reason=type(exc).__name__, target=target, detail=msg)
        return msg

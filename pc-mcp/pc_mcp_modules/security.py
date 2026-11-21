"""Host 18900 security guardrails: permissions, token I/O guard, L2 input/hotkey/audit, ASGI auth & log filters (P2-20, P2-21, P2-23)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import logging
import logging.handlers
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from typing import Optional

import l3_backends
import pyautogui
from starlette.datastructures import Headers as _StarletteHeaders
from starlette.responses import JSONResponse as _StarletteJSONResponse, Response as _StarletteResponse

from . import (
    CONTAINER_TOKENS_ENV_FILE as _DEFAULT_CONTAINER_TOKENS_ENV_FILE,
    L2_AUDIT_LOG_FILE as _DEFAULT_L2_AUDIT_LOG_FILE,
    LEGACY_TOKEN_FILE as _LEGACY_TOKEN_FILE,
    LOG_FILE as _DEFAULT_LOG_FILE,
    MODE_FILE as _DEFAULT_MODE_FILE,
    TOKEN_FILE as _DEFAULT_TOKEN_FILE,
    _audit_log_lock,
    _get_env_key,
    facade_attr,
)
from .winapi import _get_foreground_window_info, _kernel32, _user32

_ACTIVE_BEARER_TOKEN = ""
_VALID_MODES = frozenset({"off", "safe", "full"})
_mode_lock = threading.Lock()


class EmergencyStopLockedError(PermissionError):
    """Raised when an HTTP/remote caller tries to escalate to 'full' while physical emergency stop is active (P0-4)."""


# A-3: Global emergency stop state (triggered by Ctrl+Alt+Pause/Break or PyAutoGUI FailSafe)
# Note: On Windows, holding Ctrl while pressing Pause/Break generates VK_CANCEL (0x03), NOT VK_PAUSE (0x13);
# registering BOTH ensures physical keyboards (VK_CANCEL) and virtual/remapped keyboards (VK_PAUSE) both trigger.
_emergency_stop_event = threading.Event()
_emergency_hotkey_lock = threading.Lock()
_emergency_hotkey_thread: Optional[threading.Thread] = None
_emergency_hotkey_tid: int = 0
_EMERGENCY_HOTKEY_ID = 0x585A
_EMERGENCY_HOTKEY_CANCEL_ID = 0x585B
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_NOREPEAT = 0x4000
_VK_CANCEL = 0x03
_VK_PAUSE = 0x13
_EMERGENCY_HOTKEY_SPECS = (
    (_EMERGENCY_HOTKEY_CANCEL_ID, _VK_CANCEL, "VK_CANCEL(0x03)"),
    (_EMERGENCY_HOTKEY_ID, _VK_PAUSE, "VK_PAUSE(0x13)"),
)
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


def set_active_bearer_token(token: str) -> None:
    global _ACTIVE_BEARER_TOKEN
    if token:
        _ACTIVE_BEARER_TOKEN = token


def get_active_bearer_token() -> str:
    return facade_attr("_ACTIVE_BEARER_TOKEN", _ACTIVE_BEARER_TOKEN) or _ACTIVE_BEARER_TOKEN


def _sync_container_token_env(key: str, value: str, env_file: Optional[Path] = None) -> None:
    """Keep host-only secrets/pc_mcp_tokens.env in sync for Docker Compose env_file injection (A-1)."""
    if not key or not value:
        return
    target = Path(env_file) if env_file is not None else Path(
        facade_attr("CONTAINER_TOKENS_ENV_FILE", _DEFAULT_CONTAINER_TOKENS_ENV_FILE)
    )
    try:
        entries: dict[str, str] = {}
        if target.is_file():
            for raw in target.read_text(encoding="utf-8", errors="ignore").splitlines():
                ln = raw.lstrip("\ufeff").strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                k = k.strip()
                if k:
                    entries[k] = v.strip()
        if entries.get(key) == value:
            return
        entries[key] = value
        lines = [f"{k}={v}" for k, v in entries.items()]
        l3_mod = facade_attr("l3_backends", l3_backends)
        l3_mod.atomic_write_text(target, "\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_or_create_pc_mcp_token(token_file: Optional[Path] = None, *, retries: int = 3) -> str:
    """Load local 18900 Bearer token from host-private secrets/pc_mcp_token.txt or atomically generate one.

    Transient I/O errors when reading an existing token file are retried and NEVER cause silent
    regeneration/overwrite that would disconnect running clients (P2-23, A-1, A-2).
    """
    global _ACTIVE_BEARER_TOKEN
    default_tf = facade_attr("TOKEN_FILE", _DEFAULT_TOKEN_FILE)
    tf = Path(token_file) if token_file is not None else Path(default_tf)
    is_default_path = token_file is None
    tmod = facade_attr("time", time)
    last_io_err: Optional[Exception] = None
    max_tries = max(1, int(retries))

    for attempt in range(max_tries):
        try:
            existing = tf.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            last_io_err = None
            break
        except (OSError, UnicodeDecodeError) as exc:
            last_io_err = exc
            if attempt + 1 < max_tries:
                tmod.sleep(0.05 * (attempt + 1))
            continue
        if len(existing) >= 24:
            _ACTIVE_BEARER_TOKEN = existing
            if is_default_path:
                _sync_container_token_env("PC_MCP_TOKEN", existing)
            return existing
        if attempt + 1 < max_tries and existing == "":
            tmod.sleep(0.05)
            continue
        last_io_err = None
        break

    if last_io_err is not None:
        cached_tok = get_active_bearer_token()
        if is_default_path and len(cached_tok) >= 24:
            return cached_tok
        raise RuntimeError(
            f"读取令牌文件 {tf} 遇 I/O 异常，拒绝静默覆盖重建以免旧客户端失联: {last_io_err}"
        ) from last_io_err

    # One-time seamless migration from legacy data/pc_mcp_token.txt -> secrets/pc_mcp_token.txt (A-1)
    if is_default_path and tf == Path(_DEFAULT_TOKEN_FILE):
        legacy_tf = Path(facade_attr("LEGACY_TOKEN_FILE", _LEGACY_TOKEN_FILE))
        if legacy_tf != tf and legacy_tf.is_file():
            try:
                legacy_tok = legacy_tf.read_text(encoding="utf-8").strip()
                if len(legacy_tok) >= 24:
                    l3_mod = facade_attr("l3_backends", l3_backends)
                    l3_mod.atomic_write_text(tf, legacy_tok + "\n", encoding="utf-8")
                    _ACTIVE_BEARER_TOKEN = legacy_tok
                    _sync_container_token_env("PC_MCP_TOKEN", legacy_tok)
                    return legacy_tok
            except Exception:
                pass

    token = secrets.token_urlsafe(32)
    l3_mod = facade_attr("l3_backends", l3_backends)
    l3_mod.atomic_write_text(tf, token + "\n", encoding="utf-8")
    _ACTIVE_BEARER_TOKEN = token
    if is_default_path:
        _sync_container_token_env("PC_MCP_TOKEN", token)
    return token


def _extract_mode_from_json(raw: object) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    pc_sec = raw.get("pc_control")
    if isinstance(pc_sec, dict) and isinstance(pc_sec.get("mode"), str):
        m_norm = pc_sec["mode"].strip().lower()
        if m_norm in _VALID_MODES:
            return m_norm
    m = raw.get("mode")
    if isinstance(m, str):
        m_norm = m.strip().lower()
        if m_norm in _VALID_MODES:
            return m_norm
    return None


def _get_mode() -> str:
    """Read authoritative PC control mode EXCLUSIVELY from host-private pc-mcp/state/pc_control_mode.json (A-2).

    Container-writable data/runtime_settings.json is NEVER trusted by the host 18900 service.
    Missing, corrupt, or unrecognized mode files fail-safe to 'safe'.
    """
    mode_file = Path(facade_attr("MODE_FILE", _DEFAULT_MODE_FILE))
    try:
        if mode_file.is_file():
            raw = json.loads(mode_file.read_text(encoding="utf-8"))
            parsed = _extract_mode_from_json(raw)
            if parsed is not None:
                return parsed
            return "safe"
    except Exception:
        return "safe"
    return "safe"


def _notify_full_mode_escalation(prev_mode: str, source: str) -> None:
    """Best-effort non-blocking Windows desktop notification when host mode escalates to 'full' (A-2)."""
    if os.name != "nt":
        return
    if os.environ.get("PC_MCP_NOTIFY_ON_FULL", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    if "unittest" in sys.modules:
        return

    def _worker():
        try:
            import subprocess

            ps_cmd = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Warning; "
                "$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning; "
                "$n.BalloonTipTitle = 'XiaoZhi PC-MCP 安全提醒'; "
                "$n.BalloonTipText = '电脑操控已切至【全权操控挡】(full)。按 Ctrl+Alt+Pause 可随时急停降回安全挡。'; "
                "$n.Visible = $true; "
                "$n.ShowBalloonTip(4000); "
                "Start-Sleep -Milliseconds 4200; "
                "$n.Dispose()"
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                creationflags=0x08000000,
            )
        except Exception:
            pass

    try:
        threading.Thread(target=_worker, name="pc-mcp-full-notify", daemon=True).start()
    except Exception:
        pass


def set_host_mode(
    mode: str,
    *,
    source: str = "http_api",
    reason: str = "",
    allow_clear_emergency_stop: bool = False,
) -> dict:
    """Validate, atomically persist to host-private MODE_FILE, and audit every PC control mode change (A-2, P0-4).

    Physical emergency stop (Ctrl+Alt+Pause / PyAutoGUI FailSafe) is NEVER cleared by HTTP API callers;
    attempting to escalate to 'full' via HTTP while emergency stop is active is rejected fail-closed.
    """
    if not isinstance(mode, str):
        raise ValueError("mode 必须为字符串 (off / safe / full)")
    m = mode.strip().lower()
    if m not in _VALID_MODES:
        raise ValueError(f"无效的权限挡位: {mode!r}（仅允许 off / safe / full）")

    mode_file = Path(facade_attr("MODE_FILE", _DEFAULT_MODE_FILE))
    audit_fn = facade_attr("_write_l2_audit_log", _write_l2_audit_log)
    src = str(source or "http_api")
    can_clear_estop = bool(allow_clear_emergency_stop) and src not in ("emergency_stop", "http_api")

    with _mode_lock:
        prev_mode = _get_mode()
        if is_emergency_stop_active() and src != "emergency_stop" and not can_clear_estop:
            if m == "full":
                detail_blk = f"{prev_mode}->{m} blocked while emergency_stop active (source={src})"
                audit_fn(
                    tool="pc_mode",
                    action="set_mode",
                    outcome="blocked",
                    reason="emergency_stop_locked",
                    mode=prev_mode,
                    detail=detail_blk,
                )
                logging.getLogger("xiaozhi.pc_mcp").warning(
                    "[Mode] Rejected full-mode escalation while physical emergency stop is active: %s",
                    detail_blk,
                )
                raise EmergencyStopLockedError(
                    "宿主机当前处于物理急停锁定状态（Ctrl+Alt+Pause），禁止通过 POST /mode 自动解除急停或切至全权操控挡；"
                    "请在宿主机侧显式复位急停后再切换。"
                )

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "mode": m,
            "updated_at": ts,
            "source": src,
        }
        l3_mod = facade_attr("l3_backends", l3_backends)
        l3_mod.atomic_write_json(mode_file, payload)

        if can_clear_estop:
            clear_emergency_stop()

        audit_reason = "mode_escalated_to_full" if m == "full" else (
            "emergency_stop_downgrade" if src == "emergency_stop" else "mode_changed"
        )
        detail_str = f"{prev_mode}->{m} (source={src}" + (f", reason={reason})" if reason else ")")
        audit_fn(
            tool="pc_mode",
            action="set_mode",
            outcome="allowed",
            reason=audit_reason,
            mode=m,
            detail=detail_str,
        )
        logging.getLogger("xiaozhi.pc_mcp").info("[Mode] Host PC control mode changed: %s", detail_str)
        if m == "full" and prev_mode != "full":
            _notify_full_mode_escalation(prev_mode, src)
        return {
            "ok": True,
            "mode": m,
            "previous_mode": prev_mode,
            "updated_at": ts,
            "source": "host_state",
            "emergency_stopped": is_emergency_stop_active(),
        }


def is_emergency_stop_active() -> bool:
    return _emergency_stop_event.is_set()


def clear_emergency_stop() -> None:
    _emergency_stop_event.clear()


def trigger_emergency_stop(reason: str = "hotkey:ctrl+alt+pause") -> dict:
    """Immediately cancel active L2 UI operations and downgrade host mode to 'safe' (A-3)."""
    _emergency_stop_event.set()
    res = set_host_mode("safe", source="emergency_stop", reason=reason)
    logging.getLogger("xiaozhi.pc_mcp").warning(
        "[EMERGENCY STOP] Triggered (%s): L2 cancelled and mode downgraded to safe.", reason
    )
    return res


def start_emergency_stop_hotkey_listener() -> Optional[threading.Thread]:
    """Register global Windows Ctrl+Alt+Pause emergency stop hotkey on a background daemon thread (A-3)."""
    global _emergency_hotkey_thread, _emergency_hotkey_tid
    if os.name != "nt":
        return None
    with _emergency_hotkey_lock:
        if _emergency_hotkey_thread is not None and _emergency_hotkey_thread.is_alive():
            return _emergency_hotkey_thread

        ready = threading.Event()

        def _hotkey_loop():
            global _emergency_hotkey_tid
            u32 = facade_attr("_user32", _user32)
            k32 = facade_attr("_kernel32", _kernel32)
            trigger_fn = facade_attr("trigger_emergency_stop", trigger_emergency_stop)
            tid = int(k32.GetCurrentThreadId())
            _emergency_hotkey_tid = tid
            registered_ids: list[int] = []
            registered_labels: list[str] = []
            try:
                for hk_id, vk_code, label in _EMERGENCY_HOTKEY_SPECS:
                    try:
                        ok = bool(
                            u32.RegisterHotKey(
                                None,
                                hk_id,
                                _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT,
                                vk_code,
                            )
                        )
                        if ok:
                            registered_ids.append(hk_id)
                            registered_labels.append(label)
                        else:
                            logging.getLogger("xiaozhi.pc_mcp").warning(
                                "[Guardrail] Could not register global Ctrl+Alt+Pause hotkey %s (err=%s).",
                                label,
                                ctypes.get_last_error(),
                            )
                    except Exception as exc:
                        logging.getLogger("xiaozhi.pc_mcp").warning(
                            "[Guardrail] Hotkey registration error (%s): %s", label, exc
                        )
                if registered_ids:
                    logging.getLogger("xiaozhi.pc_mcp").info(
                        "[Guardrail] Global emergency stop hotkey registered: Ctrl+Alt+Pause/Break [%s]",
                        ", ".join(registered_labels),
                    )
                else:
                    logging.getLogger("xiaozhi.pc_mcp").warning(
                        "[Guardrail] No Ctrl+Alt+Pause/Break hotkey could be registered; PyAutoGUI FAILSAFE remains active."
                    )
            finally:
                ready.set()

            if not registered_ids:
                return

            registered_id_set = set(registered_ids)
            msg = wintypes.MSG()
            try:
                while True:
                    ret = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if ret <= 0:
                        break
                    if msg.message == _WM_HOTKEY and msg.wParam in registered_id_set:
                        try:
                            trigger_fn("hotkey:ctrl+alt+pause")
                        except Exception as exc:
                            logging.getLogger("xiaozhi.pc_mcp").warning(
                                "[EMERGENCY STOP] Callback failed: %s", exc
                            )
            finally:
                for hk_id in registered_ids:
                    try:
                        u32.UnregisterHotKey(None, hk_id)
                    except Exception:
                        pass
                _emergency_hotkey_tid = 0

        t = threading.Thread(target=_hotkey_loop, name="pc-mcp-emergency-hotkey", daemon=True)
        _emergency_hotkey_thread = t
        t.start()
        ready.wait(timeout=1.0)
        return t


def stop_emergency_stop_hotkey_listener() -> None:
    global _emergency_hotkey_thread, _emergency_hotkey_tid
    with _emergency_hotkey_lock:
        tid = _emergency_hotkey_tid
        t = _emergency_hotkey_thread
    if tid and os.name == "nt":
        try:
            _user32.PostThreadMessageW(wintypes.DWORD(tid), _WM_QUIT, 0, 0)
        except Exception:
            pass
    if t is not None and t.is_alive():
        t.join(timeout=1.5)
    with _emergency_hotkey_lock:
        if _emergency_hotkey_thread is t:
            _emergency_hotkey_thread = None
            _emergency_hotkey_tid = 0


def _check_permission(required_level: str) -> Optional[str]:
    """Return error message if blocked by current mode, else None."""
    get_mode_fn = facade_attr("_get_mode", _get_mode)
    mode = get_mode_fn()
    if mode == "off":
        return "电脑操控当前处于【关闭】挡位，请先在 18080 网页控制台开启电脑操控开关。"
    if required_level == "L1":
        return None
    if required_level == "L2" and is_emergency_stop_active():
        return "安全急停已触发（Ctrl+Alt+Pause），已锁定 L2 界面操控，请在宿主机侧显式复位急停后再试。"
    if required_level == "L2" and mode != "full":
        return "当前处于【日常/游戏安全挡】，已物理锁定模拟鼠标点击与键盘输入以防切屏误触。如需点按窗口按钮或打字，请在 18080 网页控制台切至【全权操控挡】(full)。"
    if required_level == "L3" and is_emergency_stop_active():
        return "安全急停已触发（Ctrl+Alt+Pause），已锁定 L3 本地后台 Agent，请在宿主机侧显式复位急停后再试。"
    if required_level == "L3" and mode != "full":
        return "当前处于【日常/游戏安全挡】，L3 本地后台 Agent 仅允许在【全权操控挡】(full) 下运行。如需派发或查询后台任务，请先在 18080 网页控制台切至【全权操控挡】(full)。"
    if mode != "full":
        return "当前挡位未授权该操作，请先在 18080 网页控制台切至【全权操控挡】(full)。"
    return None


_BLOCKED_INPUT_PROCESSES = frozenset({
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "windowsterminal.exe",
    "wt.exe",
    "conhost.exe",
    "openconsole.exe",
    "mintty.exe",
    "bash.exe",
    "wsl.exe",
    "wslhost.exe",
    "git-bash.exe",
    "sh.exe",
    "zsh.exe",
    "alacritty.exe",
    "wezterm-gui.exe",
    "hyper.exe",
    "tabby.exe",
    "putty.exe",
    "mobaxterm.exe",
    "xshell.exe",
    "securecrt.exe",
    "cmder.exe",
    "conemu.exe",
    "conemu64.exe",
    "conemuc.exe",
    "conemuc64.exe",
    "taskmgr.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "searchapp.exe",
    "searchui.exe",
    "shellexperiencehost.exe",
})

_BLOCKED_INPUT_WINDOW_CLASSES = frozenset({
    "consolewindowclass",
    "cascadia_hosting_window_class",
    "mintty",
    "virtualconsoleclass",
    "putty",
    "org.wezfurlong.wezterm",
    "dv2controlhost",
    "shell_traywnd",
    "shell_secondarytraywnd",
    "launchertipwnd",
})

_RUN_DIALOG_TITLES = frozenset({
    "运行",
    "run",
    "執行",
})

_CREATE_TASK_DIALOG_TITLES = frozenset({
    "创建新任务",
    "运行新任务",
    "create new task",
    "run new task",
    "建立新工作",
})

_EXPLORER_ADDRESS_BAR_IDS = frozenset({
    "41477",
    "1001",
    "1148",
    "addressbandroot",
    "searcheditbox",
    "textbox",
    "part_editabletextbox",
    "part_autosuggestbox",
})

_EXPLORER_ADDRESS_BAR_CLASSES = frozenset({
    "edit",
    "richeditd2dpt",
    "combobox",
    "comboboxex32",
    "address band root",
    "breadcrumb parent",
    "toolbarwindow32",
    "msctls_progress32",
})

_EXPLORER_ADDRESS_BAR_NAME_TOKENS = (
    "地址",
    "address",
    "路径",
    "path",
    "位置",
    "location",
    "搜索",
    "search",
)

_EXPLORER_XAML_BRIDGE_CLASSES = frozenset({
    "microsoft.ui.content.desktopchildsitebridge",
    "microsoft.ui.content.contentwindowsitebridge",
    "windows.ui.composition.desktopwindowcontentbridge",
    "windows.ui.input.inputsite.windowclass",
    "inputsitewindowclass",
})

_EXPLORER_ADDR_SEARCH_ANCESTOR_TOKENS = (
    "address",
    "地址",
    "breadcrumb",
    "autosuggest",
    "search",
    "搜索",
)

_EXPLORER_TOOLBAR_ADDR_ANCESTOR_TOKENS = _EXPLORER_ADDR_SEARCH_ANCESTOR_TOKENS + (
    "rebarwindow32",
    "travelband",
)

_SAFE_EXPLORER_FILE_LIST_TYPES = frozenset({
    "listitemcontrol",
    "listcontrol",
    "datagriditemcontrol",
    "datagridcontrol",
})

_ADDRESS_BAR_GUARDED_WINDOW_CLASSES = frozenset({
    "cabinetwclass",
    "#32770",
})

_EXPLORER_ADDRESS_BAR_HOTKEYS = frozenset({
    frozenset({"alt", "d"}),
    frozenset({"ctrl", "l"}),
    frozenset({"f3"}),
    frozenset({"f4"}),
    frozenset({"f6"}),
    frozenset({"ctrl", "e"}),
    frozenset({"ctrl", "f"}),
    frozenset({"shift", "f10"}),
    frozenset({"apps"}),
})

_EXPLORER_EXACT_HOTKEYS = frozenset({
    frozenset({"tab"}),
    frozenset({"shift", "tab"}),
})

_DIALOG_ADDRESS_BAR_HOTKEYS = _EXPLORER_ADDRESS_BAR_HOTKEYS | frozenset({
    frozenset({"alt", "n"}),
})


def _is_explorer_address_or_search_bar(info: dict) -> bool:
    """Detect focused or clicked Explorer (CabinetWClass) or Common File Dialog (#32770) address/search/edit bars (A-3, P0-2, P1-2)."""
    cls = str(info.get("class") or "").strip().lower()
    if cls not in _ADDRESS_BAR_GUARDED_WINDOW_CLASSES:
        return False
    for key in ("focused_control", "point_control"):
        ctrl = info.get(key)
        if not isinstance(ctrl, dict) or not ctrl:
            continue
        ctype = str(ctrl.get("control_type") or "").strip().lower()
        ccls = str(ctrl.get("class") or "").strip().lower()
        ucls = str(ctrl.get("uia_class") or "").strip().lower()
        cname = str(ctrl.get("name") or "").strip().lower()
        caid = str(ctrl.get("automation_id") or "").strip().lower()
        canc = str(ctrl.get("ancestor_names") or "").strip().lower()
        canc_types = str(ctrl.get("ancestor_types") or "").strip().lower()

        anc_type_tokens = {t.strip().lower() for t in canc_types.split("|") if t.strip()}
        has_list_item_ancestor = "listitemcontrol" in anc_type_tokens
        has_addr_search_ancestor = any(tok in canc for tok in _EXPLORER_ADDR_SEARCH_ANCESTOR_TOKENS)
        # P1-2: Strict inline file rename exemption: ONLY inside CabinetWClass file list items
        # verified via UIA ancestor_types (never untrusted ancestor_names folder titles, #32770, or XAML bridges).
        is_rename = (
            cls == "cabinetwclass"
            and has_list_item_ancestor
            and ("重命名" in cname or "rename" in cname)
            and caid not in _EXPLORER_ADDRESS_BAR_IDS
            and ccls not in _EXPLORER_XAML_BRIDGE_CLASSES
            and ucls not in _EXPLORER_XAML_BRIDGE_CLASSES
            and not any(tok in cname for tok in _EXPLORER_ADDRESS_BAR_NAME_TOKENS)
            and not has_addr_search_ancestor
        )
        if is_rename:
            continue

        # P1-2 & P0-2: Fail-closed for Win10/11 XAML Islands bridge controls unless positively confirmed as a
        # CabinetWClass file-list item (never #32770, never AutoSuggestBox/address/search/breadcrumb dropdowns).
        if ccls in _EXPLORER_XAML_BRIDGE_CLASSES or ucls in _EXPLORER_XAML_BRIDGE_CLASSES:
            confirmed_file_list = (
                cls == "cabinetwclass"
                and not ctrl.get("uia_error")
                and not has_addr_search_ancestor
                and not any(tok in cname for tok in _EXPLORER_ADDRESS_BAR_NAME_TOKENS)
                and ucls not in _EXPLORER_XAML_BRIDGE_CLASSES
                and (ctype in _SAFE_EXPLORER_FILE_LIST_TYPES or has_list_item_ancestor)
            )
            if not confirmed_file_list:
                return True
        elif ctrl.get("uia_error") and cls in _ADDRESS_BAR_GUARDED_WINDOW_CLASSES:
            return True

        if caid in _EXPLORER_ADDRESS_BAR_IDS:
            return True
        if ctype in ("edit", "editcontrol", "combobox", "comboboxcontrol"):
            return True
        for cand_cls in (ccls, ucls):
            if not cand_cls:
                continue
            if cand_cls in _EXPLORER_ADDRESS_BAR_CLASSES:
                if cand_cls in ("toolbarwindow32", "msctls_progress32"):
                    if any(tok in cname or tok in canc for tok in _EXPLORER_ADDRESS_BAR_NAME_TOKENS) or any(
                        tok in canc for tok in _EXPLORER_TOOLBAR_ADDR_ANCESTOR_TOKENS
                    ):
                        return True
                else:
                    return True
            if "autosuggest" in cand_cls or "searchedit" in cand_cls or cand_cls == "textbox":
                return True
        if any(tok in cname for tok in _EXPLORER_ADDRESS_BAR_NAME_TOKENS) and (
            "edit" in ctype or "combo" in ctype or "address" in canc or "地址" in canc or "breadcrumb" in ccls or "autosuggest" in canc
        ):
            return True
    return False


def _check_foreground_input_guard(fg_info: Optional[dict] = None) -> Optional[str]:
    """Refuse L2 keyboard/text/hotkey input when foreground window is a terminal, Start/Search,
    Windows Run dialog, Task Manager 'Run new task', or Explorer/#32770 address bar (A-3, P0-2)."""
    info = fg_info if fg_info is not None else _get_foreground_window_info()
    proc_raw = str(info.get("process") or "").strip().lower()
    proc = os.path.basename(proc_raw) if proc_raw else ""
    cls_raw = str(info.get("class") or "").strip()
    cls = cls_raw.lower()
    title = str(info.get("title") or "").strip()
    title_low = title.lower()

    if proc in _BLOCKED_INPUT_PROCESSES:
        return f"安全护栏已拦截：当前前台窗口为终端、搜索/开始菜单或系统敏感进程（{proc}），已拒绝 L2 输入。"
    if cls in _BLOCKED_INPUT_WINDOW_CLASSES:
        return f"安全护栏已拦截：当前前台窗口为终端控制台或系统外壳窗口（{cls_raw}），已拒绝 L2 输入。"
    if (cls == "#32770" and title_low in _RUN_DIALOG_TITLES) or (
        proc == "explorer.exe" and cls != "cabinetwclass" and title_low in _RUN_DIALOG_TITLES
    ):
        return f"安全护栏已拦截：当前前台窗口为 Windows「{title or '运行'}」对话框，已拒绝 L2 输入。"
    if title_low in _CREATE_TASK_DIALOG_TITLES and (
        cls == "#32770" or proc in ("", "taskmgr.exe", "explorer.exe")
    ):
        return f"安全护栏已拦截：当前前台窗口为任务管理器「{title or '创建新任务'}」对话框，已拒绝 L2 输入。"
    if _is_explorer_address_or_search_bar(info):
        return f"安全护栏已拦截：检测到焦点位于资源管理器/文件对话框（{cls_raw or 'CabinetWClass'}）地址栏、搜索框或输入控件，已拒绝 L2 输入以防命令执行绕过。"
    return None


def _check_foreground_hotkey_guard(canon_keys: list[str], fg_info: Optional[dict] = None) -> Optional[str]:
    """Block hotkeys that focus address/search bars or context menus while CabinetWClass or #32770 is foreground (A-3, P0-2, 4.1)."""
    info = fg_info if fg_info is not None else _get_foreground_window_info()
    cls = str(info.get("class") or "").strip().lower()
    if cls in _ADDRESS_BAR_GUARDED_WINDOW_CLASSES:
        combos = _DIALOG_ADDRESS_BAR_HOTKEYS if cls == "#32770" else _EXPLORER_ADDRESS_BAR_HOTKEYS
        key_set = frozenset(canon_keys)
        if key_set in _EXPLORER_EXACT_HOTKEYS:
            label = "+".join(sorted(key_set))
            return f"安全护栏已拦截：资源管理器/文件对话框窗口下禁止发送焦点切换快捷键（{label}）以防移入地址栏。"
        for combo in combos:
            if combo.issubset(key_set):
                label = "+".join(sorted(combo))
                return f"安全护栏已拦截：资源管理器/文件对话框窗口下禁止发送地址栏/搜索/菜单快捷键（{label}）。"
    return None


def _is_valid_window_target_info(info: Optional[dict]) -> bool:
    if not isinstance(info, dict) or not info:
        return False
    return bool(
        info.get("hwnd")
        or str(info.get("process") or "").strip()
        or str(info.get("class") or "").strip()
        or str(info.get("title") or "").strip()
    )


def _check_click_guard(
    act: str,
    fg_info: Optional[dict] = None,
    target_info: Optional[dict] = None,
) -> Optional[str]:
    """Guard L2 click / double_click / right_click against terminal right-click paste, Explorer context menus & unknown targets (A-3, 4.1)."""
    fg = fg_info if fg_info is not None else _get_foreground_window_info()
    fg_err = _check_foreground_input_guard(fg)

    # 4.1 #3: Fail closed when target window identity at click coordinates cannot be resolved.
    if not _is_valid_window_target_info(target_info):
        return "安全护栏已拦截：无法识别点击目标坐标所属窗口身份（fail-closed），已拒绝 L2 点击。"

    tgt_err = _check_foreground_input_guard(target_info)
    fg_cls = str(fg.get("class") or "").strip().lower() if isinstance(fg, dict) else ""
    tgt_cls = str(target_info.get("class") or "").strip().lower() if isinstance(target_info, dict) else ""

    if act == "right_click":
        if fg_err:
            return f"安全护栏已拦截：前台为终端或系统敏感窗口，已拒绝右键点击以防触发右键粘贴（{fg_err}）。"
        if tgt_err:
            return f"安全护栏已拦截：点击目标位于终端或系统敏感窗口，已拒绝右键点击（{tgt_err}）。"
        if fg_cls in _ADDRESS_BAR_GUARDED_WINDOW_CLASSES or tgt_cls in _ADDRESS_BAR_GUARDED_WINDOW_CLASSES:
            return "安全护栏已拦截：资源管理器/文件对话框禁止 L2 右键点击，以防触发「在终端中打开」等高危菜单。"
        return None

    # For left click / double_click: block clicking onto any sensitive window, or clicking when
    # foreground is sensitive and target window is unknown or the same sensitive window.
    if tgt_err:
        return f"安全护栏已拦截：点击目标位于终端、地址栏或系统敏感窗口，已拒绝 L2 点击（{tgt_err}）。"
    if fg_err:
        fg_hwnd = int(fg.get("hwnd") or 0) if isinstance(fg, dict) else 0
        tgt_hwnd = int(target_info.get("hwnd") or 0) if isinstance(target_info, dict) else 0
        if not tgt_hwnd or (fg_hwnd and tgt_hwnd == fg_hwnd):
            return f"安全护栏已拦截：当前前台为终端或系统敏感窗口，已拒绝 L2 点击（{fg_err}）。"
    return None


_HOTKEY_ALIAS_MAP = {
    "win": "win",
    "windows": "win",
    "lwin": "win",
    "rwin": "win",
    "winl": "win",
    "winr": "win",
    "winleft": "win",
    "winright": "win",
    "leftwin": "win",
    "rightwin": "win",
    "super": "win",
    "superl": "win",
    "superr": "win",
    "meta": "win",
    "metal": "win",
    "metar": "win",
    "cmd": "win",
    "command": "win",
    "gui": "win",
    "os": "win",
    "ctrl": "ctrl",
    "control": "ctrl",
    "lctrl": "ctrl",
    "rctrl": "ctrl",
    "ctrll": "ctrl",
    "ctrlr": "ctrl",
    "ctrlleft": "ctrl",
    "ctrlright": "ctrl",
    "controlleft": "ctrl",
    "controlright": "ctrl",
    "leftctrl": "ctrl",
    "rightctrl": "ctrl",
    "controll": "ctrl",
    "controlr": "ctrl",
    "shift": "shift",
    "lshift": "shift",
    "rshift": "shift",
    "shiftleft": "shift",
    "shiftright": "shift",
    "leftshift": "shift",
    "rightshift": "shift",
    "shiftl": "shift",
    "shiftr": "shift",
    "alt": "alt",
    "lalt": "alt",
    "ralt": "alt",
    "altleft": "alt",
    "altright": "alt",
    "leftalt": "alt",
    "rightalt": "alt",
    "altl": "alt",
    "altr": "alt",
    "option": "alt",
    "altgr": "alt",
    "esc": "esc",
    "escape": "esc",
    "del": "del",
    "delete": "del",
    "enter": "enter",
    "return": "enter",
    "ret": "enter",
    "space": "space",
    "spacebar": "space",
    "bs": "backspace",
    "backspace": "backspace",
    "apps": "apps",
    "menu": "apps",
    "contextmenu": "apps",
    "applications": "apps",
}

_BLOCKED_EXACT_HOTKEYS = frozenset({
    frozenset({"win"}),
})

_BLOCKED_SUBSET_HOTKEYS = frozenset({
    frozenset({"win", "r"}),
    frozenset({"win", "x"}),
    frozenset({"win", "l"}),
    frozenset({"win", "s"}),
    frozenset({"win", "q"}),
    frozenset({"win", "pause"}),
    frozenset({"win", "0"}),
    frozenset({"win", "1"}),
    frozenset({"win", "2"}),
    frozenset({"win", "3"}),
    frozenset({"win", "4"}),
    frozenset({"win", "5"}),
    frozenset({"win", "6"}),
    frozenset({"win", "7"}),
    frozenset({"win", "8"}),
    frozenset({"win", "9"}),
    frozenset({"ctrl", "esc"}),
    frozenset({"ctrl", "shift", "esc"}),
    frozenset({"ctrl", "alt", "del"}),
})

_BLOCKED_HOTKEY_COMBOS = _BLOCKED_EXACT_HOTKEYS | _BLOCKED_SUBSET_HOTKEYS


def _parse_and_normalize_hotkey(hotkey: str) -> tuple[list[str], list[str]]:
    """Parse hotkey string into (exec_keys, canonical_keys), normalizing case, separators, and key aliases."""
    raw = (hotkey or "").strip().replace("＋", "+")
    if not raw:
        return [], []
    if "+" in raw:
        parts = [p.strip() for p in raw.split("+") if p.strip()]
    elif re.search(r"[-,\s]", raw):
        parts = [p.strip() for p in re.split(r"[-,\s]+", raw) if p.strip()]
    else:
        parts = [raw]

    exec_keys: list[str] = []
    canon_keys: list[str] = []
    for p in parts:
        low = p.lower()
        compact = re.sub(r"[\s_\-]+", "", low)
        canon = _HOTKEY_ALIAS_MAP.get(compact, low)
        canon_keys.append(canon)
        exec_keys.append(canon if compact in _HOTKEY_ALIAS_MAP else low)
    return exec_keys, canon_keys


def _check_hotkey_blacklist(canon_keys: list[str], raw_hotkey: str = "") -> Optional[str]:
    """Block dangerous Windows system/shell hotkey combinations regardless of key order, alias, or trailing keys."""
    key_set = frozenset(canon_keys)
    matched: Optional[frozenset[str]] = None
    if key_set in _BLOCKED_EXACT_HOTKEYS:
        matched = key_set
    else:
        for combo in _BLOCKED_SUBSET_HOTKEYS:
            if combo.issubset(key_set):
                matched = combo
                break
    if matched is not None:
        canon_label = "+".join(sorted(matched))
        return f"安全护栏已拦截高危系统快捷键（{raw_hotkey or canon_label} -> {canon_label}），禁止通过 L2 触发系统运行/终端/任务管理器快捷键。"
    return None


def _validate_hotkey_keys(exec_keys: list[str]) -> list[str]:
    """Return any key names not recognized by pyautogui.isValidKey()."""
    pg = facade_attr("pyautogui", pyautogui)
    return [k for k in exec_keys if not pg.isValidKey(k)]


_SECRET_PATTERNS = (
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/=]{8,}", re.IGNORECASE), r"\1***"),
    (re.compile(r"(#token=|api_key=|authorization\s*[:=]\s*)[^\s&\"',;]+", re.IGNORECASE), r"\1***"),
    (
        re.compile(
            r"((?:password|passwd|pwd|secret|token|access_token|api[-_]?key)\s*[:=]\s*)[^\s&\"',;]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "sk-***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"), "AIza***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "gh_***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "xox-***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), "jwt.***"),
)


def redact_secrets(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    s = str(text)
    for sec in extra_secrets:
        if sec and len(sec) >= 8 and sec in s:
            s = s.replace(sec, "***")
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s


def _get_audit_extra_secrets() -> tuple[str, ...]:
    vals = (
        get_active_bearer_token(),
        os.environ.get("DEEPSEEK_API_KEY", ""),
        os.environ.get("GEMINI_API_KEY", ""),
    )
    return tuple(s for s in vals if s and len(s) >= 8)


def _sanitize_audit_text(value: str, max_len: int = 120) -> str:
    s = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if not s:
        return ""
    s = redact_secrets(s, _get_audit_extra_secrets())
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def _summarize_typed_text_for_audit(text: str) -> tuple[int, str]:
    s = str(text or "")
    if not s:
        return 0, ""
    return len(s), f"[REDACTED len={len(s)}]"


L2_AUDIT_MAX_BYTES = 5 * 1024 * 1024  # H-6: 5 MB per audit log file
L2_AUDIT_BACKUP_COUNT = 3  # H-6: Keep 3 rotated backups (.1 .. .3)


def _rotate_file_if_needed(
    path: Path,
    *,
    max_bytes: int = L2_AUDIT_MAX_BYTES,
    backup_count: int = L2_AUDIT_BACKUP_COUNT,
    pending_bytes: int = 0,
) -> bool:
    """Rotate `path` to `.1`..`.<backup_count>` when size + pending write exceeds `max_bytes` (H-6).

    Caller must hold `_audit_log_lock`.
    """
    if max_bytes <= 0 or backup_count <= 0:
        return False
    try:
        if not path.is_file():
            return False
        cur_size = path.stat().st_size
        if cur_size <= 0 or (cur_size + max(0, int(pending_bytes))) <= max_bytes:
            return False
        keep = int(backup_count)
        oldest = path.with_name(f"{path.name}.{keep}")
        try:
            oldest.unlink(missing_ok=True)
        except OSError:
            pass
        for idx in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{idx}")
            dst = path.with_name(f"{path.name}.{idx + 1}")
            if src.exists():
                os.replace(src, dst)
        os.replace(path, path.with_name(f"{path.name}.1"))
        return True
    except OSError:
        return False


def _write_l2_audit_log(
    *,
    tool: str,
    action: str,
    outcome: str,
    reason: str = "",
    mode: Optional[str] = None,
    fg_info: Optional[dict] = None,
    target: str = "",
    hotkey: str = "",
    text: str = "",
    press_enter: bool = False,
    detail: str = "",
) -> None:
    """Append a sanitized JSONL audit record to host-private pc-mcp/state/l2_audit.jsonl (with 5MBx3 rotation)."""
    get_fg_fn = facade_attr("_get_foreground_window_info", _get_foreground_window_info)
    get_mode_fn = facade_attr("_get_mode", _get_mode)
    audit_path = Path(facade_attr("L2_AUDIT_LOG_FILE", _DEFAULT_L2_AUDIT_LOG_FILE))
    try:
        info = fg_info if fg_info is not None else get_fg_fn()
    except Exception:
        info = {"process": "", "class": "", "title": ""}
    text_len, text_summary = _summarize_typed_text_for_audit(text)
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode if mode is not None else get_mode_fn(),
        "tool": tool,
        "action": str(action or ""),
        "outcome": str(outcome or ""),
        "reason": str(reason or ""),
        "fg_process": _sanitize_audit_text(info.get("process", ""), max_len=80),
        "fg_class": _sanitize_audit_text(info.get("class", ""), max_len=80),
        "fg_title": _sanitize_audit_text(info.get("title", ""), max_len=120),
        "target": _sanitize_audit_text(target, max_len=120),
        "hotkey": _sanitize_audit_text(hotkey, max_len=64),
        "text_len": text_len,
        "text_summary": text_summary,
        "press_enter": bool(press_enter),
        "detail": _sanitize_audit_text(detail, max_len=160),
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    line_bytes_len = len(line.encode("utf-8"))
    try:
        with _audit_log_lock:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_file_if_needed(
                audit_path,
                max_bytes=L2_AUDIT_MAX_BYTES,
                backup_count=L2_AUDIT_BACKUP_COUNT,
                pending_bytes=line_bytes_len,
            )
            with open(audit_path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as exc:
        logging.getLogger("xiaozhi.pc_mcp").warning("Failed to write L2 audit log: %s", exc)


class _SecretRedactingFilter(logging.Filter):
    def __init__(self, extra_secrets: tuple[str, ...] = ()):
        super().__init__()
        self._extra = tuple(s for s in extra_secrets if s and len(s) >= 8)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg, self._extra)
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(
                redact_secrets(a, self._extra) if isinstance(a, str) else a
                for a in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: (redact_secrets(v, self._extra) if isinstance(v, str) else v)
                for k, v in record.args.items()
            }
        return True


class _AccessLogNoiseFilter(logging.Filter):
    """Drop routine 200 OK polling logs for /l3/status, /audio/status, and GET /mode; keep all errors & mutations."""

    _QUIET_PATHS = frozenset({"/l3/status", "/audio/status", "/mode"})

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            method = str(args[1]).upper()
            raw_path = str(args[2]).split("?", 1)[0]
            try:
                status_code = int(args[4])
            except Exception:
                status_code = 0
            if method == "GET" and raw_path in self._QUIET_PATHS and status_code == 200:
                return False
        else:
            msg = record.getMessage()
            if '"GET /l3/status' in msg and msg.rstrip().endswith("200"):
                return False
            if '"GET /audio/status' in msg and msg.rstrip().endswith("200"):
                return False
            if '"GET /mode' in msg and msg.rstrip().endswith("200"):
                return False
        return True


def _build_allowed_hosts_and_origins(port: int = 18900) -> tuple[list[str], list[str]]:
    hosts = [
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"host.docker.internal:{port}",
        f"[::1]:{port}",
    ]
    origins = [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://host.docker.internal:{port}",
        f"http://[::1]:{port}",
        "http://127.0.0.1:18080",
        "http://localhost:18080",
        "http://[::1]:18080",
    ]
    return hosts, origins


class _LocalBearerAndHostMiddleware:
    """ASGI middleware enforcing Host/Origin allowlist and Bearer Token across ALL 18900 endpoints."""

    def __init__(self, app, token: str | None, allowed_hosts: list[str], allowed_origins: list[str]):
        self.app = app
        norm_token = token.strip() if isinstance(token, str) else ""
        self.expected_token = norm_token
        self.expected_token_bytes = norm_token.encode("utf-8") if norm_token else b""
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.allowed_origins = {o.lower() for o in allowed_origins}

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope_type not in ("http", "websocket"):
            return

        headers = _StarletteHeaders(scope=scope)
        host = (headers.get("host") or "").strip().lower()
        if not host or host not in self.allowed_hosts:
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "Invalid Host header"})
                return
            resp = _StarletteResponse("Invalid Host header", status_code=421)
            await resp(scope, receive, send)
            return

        origin = (headers.get("origin") or "").strip().lower()
        if origin and origin not in self.allowed_origins:
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "Invalid Origin header"})
                return
            resp = _StarletteResponse("Invalid Origin header", status_code=403)
            await resp(scope, receive, send)
            return

        auth = (headers.get("authorization") or "").strip()
        scheme, sep, supplied_token = auth.partition(" ")
        supplied_bytes = supplied_token.strip().encode("utf-8", errors="replace")
        if (
            not sep
            or scheme.lower() != "bearer"
            or not self.expected_token_bytes
            or not secrets.compare_digest(supplied_bytes, self.expected_token_bytes)
        ):
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "Unauthorized"})
                return
            resp = _StarletteJSONResponse(
                {"ok": False, "error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _configure_logging(active_token: str) -> logging.Logger:
    log_file = Path(facade_attr("LOG_FILE", _DEFAULT_LOG_FILE))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    extra_secrets = (
        active_token,
        _get_env_key("DEEPSEEK_API_KEY"),
        _get_env_key("GEMINI_API_KEY"),
    )
    redact_filter = _SecretRedactingFilter(extra_secrets)
    noise_filter = _AccessLogNoiseFilter()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redact_filter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)

    for lname in ("uvicorn", "uvicorn.error", "mcp"):
        lg = logging.getLogger(lname)
        lg.handlers.clear()
        lg.addHandler(file_handler)
        lg.propagate = False
        lg.setLevel(logging.INFO)

    acc = logging.getLogger("uvicorn.access")
    acc.handlers.clear()
    acc.filters.clear()
    acc.addFilter(noise_filter)
    acc.addHandler(file_handler)
    acc.propagate = False
    acc.setLevel(logging.INFO)

    return logging.getLogger("xiaozhi.pc_mcp")

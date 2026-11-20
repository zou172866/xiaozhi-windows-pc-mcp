"""Modularized Windows PC control MCP building blocks (P2-20).

Modules:
- winapi: DPI awareness, Win32 API bindings, volume/media/power control, window enumeration & focus
- security: runtime_settings/mode permissions, Bearer token I/O guard, L2 input & hotkey guardrails, audit & ASGI auth
- launcher: browser detection, safe ShellExecuteW, Start Menu lookup, app/web launcher
- vision: DeepSeek/Gemini multimodal vision, UIA 18-level control walk, SoM overlay & 3-stage grounding
- ui_tools: L2 window UI inspection and guarded click/type/hotkey/scroll execution
- audio_bridge: 48kHz WinMM double-buffered PCM player, soft limiter, /audio/* and /l3/* HTTP routes
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
import threading
from pathlib import Path
from typing import Any

# Ensure pc-mcp root is importable for l3_backends
MCP_DIR = Path(__file__).resolve().parent.parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import comtypes
import l3_backends

SERVER_DIR = MCP_DIR.parent
DATA_DIR = SERVER_DIR / "data"
SECRETS_DIR = SERVER_DIR / "secrets"
LOGS_DIR = SERVER_DIR / "logs"
CONFIG_RUNTIME_DIR = SERVER_DIR / "config" / "runtime"
STATE_DIR = getattr(l3_backends, "STATE_DIR", MCP_DIR / "state")
# Deprecated legacy alias kept only for backward-compatible test imports; host NEVER trusts container-writable runtime_settings.json (A-2).
RUNTIME_SETTINGS_FILE = DATA_DIR / "runtime_settings.json"
# Authoritative host-private PC control mode file (A-2): never mounted into Docker containers.
MODE_FILE = STATE_DIR / "pc_control_mode.json"
LEGACY_TOKEN_FILE = DATA_DIR / "pc_mcp_token.txt"
TOKEN_FILE = SECRETS_DIR / "pc_mcp_token.txt"
CONTAINER_TOKENS_ENV_FILE = SECRETS_DIR / "pc_mcp_tokens.env"
LOG_FILE = LOGS_DIR / "pc_mcp_18900.log"
CONFIG_YAML = CONFIG_RUNTIME_DIR / ".config.yaml"
LEGACY_CONFIG_YAML = DATA_DIR / ".config.yaml"
L2_AUDIT_LOG_FILE = STATE_DIR / "l2_audit.jsonl"
LAST_GROUNDING_IMG = STATE_DIR / "last_som_grounding.jpg"
TASKS_ROOT = STATE_DIR / "agy_tasks"
PENDING_DIR = TASKS_ROOT / "pending"
ACTIVE_DIR = TASKS_ROOT / "active"
HEARTBEAT_FILE = TASKS_ROOT / "worker_heartbeat.json"

for _d in (STATE_DIR, PENDING_DIR, ACTIVE_DIR, SECRETS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Lock hierarchy: _gui_lock (outer) -> _clipboard_lock / _audit_log_lock (inner) (P1-16)
_gui_lock = threading.RLock()
_clipboard_lock = threading.Lock()
_audit_log_lock = threading.Lock()
_RPC_E_CHANGED_MODE = -2147417850
_CF_UNICODETEXT = 13


def init_dpi_awareness() -> None:
    """Enable Per-Monitor DPI V2 before importing GUI/screenshot libraries (1:1 physical pixels)."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def facade_attr(name: str, fallback: Any) -> Any:
    """Resolve overridable symbol from pc_control_mcp facade when loaded (preserves test monkeypatching)."""
    mod = sys.modules.get("pc_control_mcp")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


@contextlib.contextmanager
def _com_mta_scope():
    """Unified MTA COM initializer safe across worker threads (prevents RPC_E_CHANGED_MODE)."""
    inited = False
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        inited = True
    except OSError as exc:
        if getattr(exc, "winerror", None) != _RPC_E_CHANGED_MODE:
            raise
    try:
        yield
    finally:
        if inited:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


def _get_env_key(name: str) -> str:
    l3_mod = facade_attr("l3_backends", l3_backends)
    return l3_mod.read_env_key(name)

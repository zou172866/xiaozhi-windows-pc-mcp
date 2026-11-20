"""Host 18900 security guardrails: permissions, token I/O guard, L2 input/hotkey/audit, ASGI auth & log filters (P2-20, P2-21, P2-23)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import logging.handlers
import os
from pathlib import Path
import re
import secrets
import time
from typing import Optional

import l3_backends
import pyautogui
from starlette.datastructures import Headers as _StarletteHeaders
from starlette.responses import JSONResponse as _StarletteJSONResponse, Response as _StarletteResponse

from . import (
    L2_AUDIT_LOG_FILE as _DEFAULT_L2_AUDIT_LOG_FILE,
    LOG_FILE as _DEFAULT_LOG_FILE,
    MODE_FILE as _DEFAULT_MODE_FILE,
    RUNTIME_SETTINGS_FILE as _DEFAULT_RUNTIME_SETTINGS_FILE,
    TOKEN_FILE as _DEFAULT_TOKEN_FILE,
    _audit_log_lock,
    _get_env_key,
    facade_attr,
)
from .winapi import _get_foreground_window_info

_ACTIVE_BEARER_TOKEN = ""
_VALID_MODES = frozenset({"off", "safe", "full"})


def set_active_bearer_token(token: str) -> None:
    global _ACTIVE_BEARER_TOKEN
    if token:
        _ACTIVE_BEARER_TOKEN = token


def get_active_bearer_token() -> str:
    return facade_attr("_ACTIVE_BEARER_TOKEN", _ACTIVE_BEARER_TOKEN) or _ACTIVE_BEARER_TOKEN


def load_or_create_pc_mcp_token(token_file: Optional[Path] = None, *, retries: int = 3) -> str:
    """Load local 18900 Bearer token from data/pc_mcp_token.txt or atomically generate one.

    Transient I/O errors when reading an existing token file are retried and NEVER cause silent
    regeneration/overwrite that would disconnect running clients (P2-23).
    """
    global _ACTIVE_BEARER_TOKEN
    default_tf = facade_attr("TOKEN_FILE", _DEFAULT_TOKEN_FILE)
    tf = Path(token_file) if token_file is not None else Path(default_tf)
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
            return existing
        if attempt + 1 < max_tries and existing == "":
            tmod.sleep(0.05)
            continue
        last_io_err = None
        break

    if last_io_err is not None:
        cached_tok = get_active_bearer_token()
        if token_file is None and len(cached_tok) >= 24:
            return cached_tok
        raise RuntimeError(
            f"读取令牌文件 {tf} 遇 I/O 异常，拒绝静默覆盖重建以免旧客户端失联: {last_io_err}"
        ) from last_io_err

    token = secrets.token_urlsafe(32)
    l3_mod = facade_attr("l3_backends", l3_backends)
    l3_mod.atomic_write_text(tf, token + "\n", encoding="utf-8")
    _ACTIVE_BEARER_TOKEN = token
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
    """Read PC control mode from unified data/runtime_settings.json with legacy pc_control_mode.json fallback (P2-21)."""
    mode_file = Path(facade_attr("MODE_FILE", _DEFAULT_MODE_FILE))
    rs_file = Path(facade_attr("RUNTIME_SETTINGS_FILE", _DEFAULT_RUNTIME_SETTINGS_FILE))

    # If a unit test explicitly monkeypatched MODE_FILE (without patching RUNTIME_SETTINGS_FILE), honor MODE_FILE first
    mode_overridden = mode_file != _DEFAULT_MODE_FILE and rs_file == _DEFAULT_RUNTIME_SETTINGS_FILE
    candidates = [mode_file] if mode_overridden else [rs_file, mode_file]

    for path in candidates:
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                parsed = _extract_mode_from_json(raw)
                if parsed is not None:
                    return parsed
                # If the primary settings file exists and is valid/corrupt without valid mode,
                # fall back to next candidate only if file was missing; otherwise fail-safe to 'safe'.
                if path == rs_file and mode_file.is_file() and isinstance(raw, dict) and "pc_control" not in raw:
                    continue
                return "safe"
        except Exception:
            if path == rs_file and not mode_overridden:
                # Corrupt runtime_settings.json must fail-safe to 'safe' rather than trusting stale legacy file
                return "safe"
            continue
    return "safe"


def _check_permission(required_level: str) -> Optional[str]:
    """Return error message if blocked by current mode, else None."""
    get_mode_fn = facade_attr("_get_mode", _get_mode)
    mode = get_mode_fn()
    if mode == "off":
        return "电脑操控当前处于【关闭】挡位，请先在 18080 网页控制台开启电脑操控开关。"
    if required_level == "L1":
        return None
    if required_level == "L2" and mode != "full":
        return "当前处于【日常/游戏安全挡】，已物理锁定模拟鼠标点击与键盘输入以防切屏误触。如需点按窗口按钮或打字，请在 18080 网页控制台切至【全权操控挡】(full)。"
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
})

_BLOCKED_INPUT_WINDOW_CLASSES = frozenset({
    "consolewindowclass",
    "cascadia_hosting_window_class",
    "mintty",
    "virtualconsoleclass",
    "putty",
    "org.wezfurlong.wezterm",
})

_RUN_DIALOG_TITLES = frozenset({
    "运行",
    "run",
    "執行",
})


def _check_foreground_input_guard(fg_info: Optional[dict] = None) -> Optional[str]:
    """Refuse L2 keyboard/text/hotkey input when foreground window is a terminal or Windows Run dialog."""
    info = fg_info if fg_info is not None else _get_foreground_window_info()
    proc_raw = str(info.get("process") or "").strip().lower()
    proc = os.path.basename(proc_raw) if proc_raw else ""
    cls_raw = str(info.get("class") or "").strip()
    cls = cls_raw.lower()
    title = str(info.get("title") or "").strip()
    title_low = title.lower()

    if proc in _BLOCKED_INPUT_PROCESSES:
        return f"安全护栏已拦截：当前前台窗口为终端或系统敏感进程（{proc}），已拒绝 L2 键盘输入。"
    if cls in _BLOCKED_INPUT_WINDOW_CLASSES:
        return f"安全护栏已拦截：当前前台窗口为终端控制台窗口（{cls_raw}），已拒绝 L2 键盘输入。"
    if (cls == "#32770" and title_low in _RUN_DIALOG_TITLES) or (
        proc == "explorer.exe" and cls != "cabinetwclass" and title_low in _RUN_DIALOG_TITLES
    ):
        return f"安全护栏已拦截：当前前台窗口为 Windows「{title or '运行'}」对话框，已拒绝 L2 键盘输入。"
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
    """Append a sanitized JSONL audit record to host-private pc-mcp/state/l2_audit.jsonl."""
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
    try:
        with _audit_log_lock:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
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
    """Drop routine 200 OK polling logs for /l3/status and /audio/status; keep all errors & mutations."""

    _QUIET_PATHS = frozenset({"/l3/status", "/audio/status"})

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

    def __init__(self, app, token: str, allowed_hosts: list[str], allowed_origins: list[str]):
        self.app = app
        self.expected_token = token
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.allowed_origins = {o.lower() for o in allowed_origins}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _StarletteHeaders(scope=scope)
        host = (headers.get("host") or "").strip().lower()
        if not host or host not in self.allowed_hosts:
            resp = _StarletteResponse("Invalid Host header", status_code=421)
            await resp(scope, receive, send)
            return

        origin = (headers.get("origin") or "").strip().lower()
        if origin and origin not in self.allowed_origins:
            resp = _StarletteResponse("Invalid Origin header", status_code=403)
            await resp(scope, receive, send)
            return

        auth = (headers.get("authorization") or "").strip()
        scheme, sep, supplied_token = auth.partition(" ")
        if (
            not sep
            or scheme.lower() != "bearer"
            or not secrets.compare_digest(supplied_token.strip(), self.expected_token)
        ):
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

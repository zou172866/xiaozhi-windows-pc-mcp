"""Pluggable L3 background Agent backend registry (Antigravity / DeepSeek DSH / Tencent WorkBuddy / None).

All paths are derived dynamically from the repository root so the project works
out of the box on any Windows machine without hardcoded drive letters or usernames.
"""

from __future__ import annotations

import json
import os
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVER_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = SERVER_DIR / "data"
TASKS_ROOT = DATA_DIR / "agy_tasks"
PENDING_DIR = TASKS_ROOT / "pending"
ACTIVE_DIR = TASKS_ROOT / "active"
HEARTBEAT_FILE = TASKS_ROOT / "worker_heartbeat.json"
L3_CONFIG_FILE = DATA_DIR / "l3_backend_config.json"

for _d in (PENDING_DIR, ACTIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

VALID_BACKENDS = ("antigravity", "dsh", "workbuddy", "none")

_cfg_lock = threading.Lock()


def read_env_key(name: str) -> str:
    """Read an environment variable from current process or Windows User/Machine registry."""
    val = os.environ.get(name, "")
    if not val and os.name == "nt":
        try:
            import winreg

            for root, subkey in (
                (winreg.HKEY_CURRENT_USER, r"Environment"),
                (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ):
                try:
                    with winreg.OpenKey(root, subkey) as k:
                        v, _ = winreg.QueryValueEx(k, name)
                        if v:
                            val = str(v)
                            break
                except Exception:
                    pass
        except Exception:
            pass
    return val.lstrip("\ufeff").strip()


def split_command(cmd_str: str) -> List[str]:
    """Split a custom Windows command string preserving quoted paths."""
    raw = (cmd_str or "").strip()
    if not raw:
        return []
    try:
        parts = shlex.split(raw, posix=False)
    except Exception:
        parts = raw.split()
    return [p.strip('"').strip("'") for p in parts if p.strip()]


def _default_backend() -> str:
    env_b = (read_env_key("L3_BACKEND") or "antigravity").strip().lower()
    return env_b if env_b in VALID_BACKENDS else "antigravity"


def default_l3_config() -> Dict[str, Any]:
    return {
        "backend": _default_backend(),
        "workdir": read_env_key("XIAOZHI_L3_WORKDIR"),
        "dsh_cmd": read_env_key("DSH_CMD"),
        "workbuddy_cmd": read_env_key("WORKBUDDY_CMD"),
        "workbuddy_auto_approve": True,
        "timeout_seconds": 300,
    }


def _normalize_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    base = default_l3_config()
    merged = dict(base)
    if isinstance(raw, dict):
        merged.update(raw)
    b = str(merged.get("backend") or base["backend"]).strip().lower()
    if b not in VALID_BACKENDS:
        b = base["backend"]
    merged["backend"] = b
    merged["workdir"] = str(merged.get("workdir") or "").strip()
    merged["dsh_cmd"] = str(merged.get("dsh_cmd") or "").strip()
    merged["workbuddy_cmd"] = str(merged.get("workbuddy_cmd") or "").strip()
    merged["workbuddy_auto_approve"] = bool(merged.get("workbuddy_auto_approve", True))
    try:
        t = int(merged.get("timeout_seconds", 300))
    except Exception:
        t = 300
    merged["timeout_seconds"] = max(30, min(1800, t))
    return merged


def read_l3_config() -> Dict[str, Any]:
    with _cfg_lock:
        if L3_CONFIG_FILE.is_file():
            try:
                raw = json.loads(L3_CONFIG_FILE.read_text(encoding="utf-8"))
                return _normalize_config(raw)
            except Exception:
                pass
        return default_l3_config()


def write_l3_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    with _cfg_lock:
        cur = default_l3_config()
        if L3_CONFIG_FILE.is_file():
            try:
                cur = _normalize_config(json.loads(L3_CONFIG_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
        for k in ("backend", "workdir", "dsh_cmd", "workbuddy_cmd", "workbuddy_auto_approve", "timeout_seconds"):
            if k in patch and patch[k] is not None:
                cur[k] = patch[k]
        norm = _normalize_config(cur)
        L3_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        L3_CONFIG_FILE.write_text(json.dumps(norm, ensure_ascii=False, indent=2), encoding="utf-8")
        return norm


def resolve_workdir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    c = cfg or read_l3_config()
    wd = str(c.get("workdir") or "").strip()
    if wd:
        p = Path(wd).expanduser()
        if p.exists() and p.is_dir():
            return p
    return SERVER_DIR.parent


def save_active_task(task_id: str, payload: Dict[str, Any]) -> Path:
    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    out = ACTIVE_DIR / f"{task_id}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_last_active_task(backend: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not ACTIVE_DIR.exists():
        return None
    files = sorted(ACTIVE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for fp in files:
        try:
            obj = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if backend is None:
            return obj
        item_backend = str(obj.get("backend") or "antigravity").strip().lower()
        if item_backend == backend:
            return obj
    return None


def summarize_text_for_voice(text: str, limit: int = 280) -> str:
    clean = " ".join((text or "").replace("\r", "\n").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "……"


class L3Backend:
    name: str = "base"
    label_zh: str = "通用后端"

    def detect(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def dispatch(
        self, task_id: str, instruction: str, follow_up_last: bool, cfg: Dict[str, Any]
    ) -> str:
        raise NotImplementedError

    def check_status(self, cfg: Dict[str, Any]) -> str:
        raise NotImplementedError


from .antigravity import AntigravityBackend  # noqa: E402
from .dsh import DshBackend  # noqa: E402
from .none_backend import NoneBackend  # noqa: E402
from .workbuddy import WorkBuddyBackend  # noqa: E402

BACKENDS: Dict[str, L3Backend] = {
    "antigravity": AntigravityBackend(),
    "dsh": DshBackend(),
    "workbuddy": WorkBuddyBackend(),
    "none": NoneBackend(),
}


def get_backend(name: Optional[str] = None) -> L3Backend:
    key = (name or read_l3_config()["backend"]).strip().lower()
    return BACKENDS.get(key, BACKENDS["antigravity"])


def get_status_overview() -> Dict[str, Any]:
    cfg = read_l3_config()
    detected: Dict[str, Dict[str, Any]] = {}
    for k, b in BACKENDS.items():
        try:
            detected[k] = b.detect(cfg)
        except Exception as exc:
            detected[k] = {"ready": False, "detail": f"探测异常: {exc}"}
    active_key = cfg["backend"]
    active_info = detected.get(active_key, {"ready": False, "detail": "未知后端"})
    return {
        "ok": True,
        "backend": active_key,
        "label_zh": BACKENDS[active_key].label_zh,
        "ready": bool(active_info.get("ready")),
        "detail": str(active_info.get("detail") or ""),
        "workdir": cfg["workdir"],
        "effective_workdir": str(resolve_workdir(cfg)),
        "dsh_cmd": cfg["dsh_cmd"],
        "workbuddy_cmd": cfg["workbuddy_cmd"],
        "workbuddy_auto_approve": cfg["workbuddy_auto_approve"],
        "timeout_seconds": cfg["timeout_seconds"],
        "detected": detected,
        "checked_at": time.strftime("%H:%M:%S"),
    }


def dispatch_task(instruction: str, follow_up_last: bool = False) -> str:
    cfg = read_l3_config()
    backend = get_backend(cfg["backend"])
    task_id = time.strftime("%Y%m%d_%H%M%S_") + os.urandom(3).hex()
    return backend.dispatch(task_id, instruction, follow_up_last, cfg)


def check_task_status() -> str:
    cfg = read_l3_config()
    backend = get_backend(cfg["backend"])
    return backend.check_status(cfg)

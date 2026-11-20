"""Shared .env and Windows environment/registry lookup utilities (H-5)."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, Optional

_dotenv_lock = threading.Lock()
_dotenv_caches: Dict[str, tuple[tuple[int, int], Dict[str, str]]] = {}


def strip_dotenv_value(raw_val: str) -> str:
    val = raw_val.strip()
    if not val:
        return ""
    if val[0] in ('"', "'"):
        quote = val[0]
        end = val.find(quote, 1)
        if end != -1:
            return val[1:end].strip()
        return val[1:].strip()
    # Strip unquoted inline comments preceded by whitespace (e.g. `L3_BACKEND=dsh # comment`)
    for idx, ch in enumerate(val):
        if ch == "#" and idx > 0 and val[idx - 1].isspace():
            val = val[:idx]
            break
    return val.strip()


def parse_dotenv_text(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.lstrip("\ufeff").strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        eq = line.find("=")
        if eq <= 0:
            continue
        key = line[:eq].strip()
        val = strip_dotenv_value(line[eq + 1 :])
        if key:
            out[key] = val
    return out


def read_dotenv_map(env_file: Path | str) -> Dict[str, str]:
    """Read a `.env` file with thread-safe mtime_ns+size caching."""
    env_path = Path(env_file)
    norm_key = str(env_path)
    try:
        if not env_path.is_file():
            with _dotenv_lock:
                _dotenv_caches.pop(norm_key, None)
            return {}
        st = env_path.stat()
        sig = (st.st_mtime_ns, st.st_size)
        with _dotenv_lock:
            cached = _dotenv_caches.get(norm_key)
            if cached is None or cached[0] != sig:
                parsed = parse_dotenv_text(env_path.read_text(encoding="utf-8", errors="replace"))
                _dotenv_caches[norm_key] = (sig, parsed)
                return dict(parsed)
            return dict(cached[1])
    except Exception:
        return {}


def read_dotenv_var(env_file: Path | str, name: str) -> str:
    """Read a single key from a `.env` file."""
    return read_dotenv_map(env_file).get(name, "")


def invalidate_dotenv_cache(env_file: Optional[Path | str] = None) -> None:
    """Clear cached `.env` entries (for tests or forced reloads)."""
    with _dotenv_lock:
        if env_file is None:
            _dotenv_caches.clear()
        else:
            _dotenv_caches.pop(str(Path(env_file)), None)


def read_env_key(name: str, *, dotenv_path: Optional[Path | str] = None) -> str:
    """Read an env var from process env, Windows registry, or `.env` fallback."""
    val = os.environ.get(name, "")
    if not val and os.name == "nt":
        try:
            import winreg

            for root, subkey in (
                (winreg.HKEY_CURRENT_USER, r"Environment"),
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                ),
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
    if not val and dotenv_path is not None:
        val = read_dotenv_var(dotenv_path, name)
    return val.lstrip("\ufeff").strip()

"""Shared container-side utilities for xiaozhi-esp32-server (P2-20).

Consolidates duplicate atomic file writers, mtime-cached JSON/text readers,
and Bearer token readers across start_with_env, console_server, pc_audio_router,
reply_length_router, anima_memory_provider, and nmc_weather_patch.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, Generic, Optional, TypeVar

ROOT_DIR = "/opt/xiaozhi-esp32-server"
DATA_DIR = os.path.join(ROOT_DIR, "data")
PC_MCP_TOKEN_PATH = os.path.join(DATA_DIR, "pc_mcp_token.txt")
ANIMA_TOKEN_PATH = os.path.join(DATA_DIR, "anima", "studio_token.txt")

T = TypeVar("T")


def atomic_write_text(path: str, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically write UTF-8 text via same-directory temp file + fsync + os.replace."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str,
    payload: Any,
    *,
    indent: Optional[int] = 2,
    ensure_ascii: bool = False,
) -> None:
    """Atomically serialize JSON to `path`."""
    serialized = json.dumps(payload, ensure_ascii=ensure_ascii, indent=indent)
    atomic_write_text(path, serialized)


class MtimeFileCache(Generic[T]):
    """Thread-safe mtime+size-keyed file cache with transient I/O fallback."""

    def __init__(
        self,
        parser: Callable[[str], T],
        default_factory: Callable[[], T],
        *,
        retries: int = 2,
    ) -> None:
        self._parser = parser
        self._default_factory = default_factory
        self._retries = max(1, int(retries))
        self._lock = threading.RLock()
        self._sig: Optional[tuple[int, int]] = None  # (mtime_ns, st_size)
        self._has_value = False
        self._value: Optional[T] = None

    def get(self, path: str, *, clone: Optional[Callable[[T], T]] = None) -> T:
        with self._lock:
            try:
                st = os.stat(path)
            except FileNotFoundError:
                self._sig = None
                self._has_value = False
                self._value = None
                return self._default_factory()
            except OSError:
                if self._has_value and self._value is not None:
                    return clone(self._value) if clone else self._value
                return self._default_factory()

            sig = (int(st.st_mtime_ns), int(st.st_size))
            if self._has_value and self._sig == sig and self._value is not None:
                return clone(self._value) if clone else self._value

            last_err: Optional[Exception] = None
            for attempt in range(self._retries):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = f.read()
                    parsed = self._parser(raw)
                    self._value = parsed
                    self._sig = sig
                    self._has_value = True
                    return clone(parsed) if clone else parsed
                except Exception as exc:
                    last_err = exc
                    if attempt + 1 < self._retries:
                        time.sleep(0.02)
            _ = last_err
            if self._has_value and self._value is not None:
                return clone(self._value) if clone else self._value
            return self._default_factory()

    def set(self, path: str, value: T) -> None:
        with self._lock:
            self._value = value
            self._has_value = True
            try:
                st = os.stat(path)
                self._sig = (int(st.st_mtime_ns), int(st.st_size))
            except OSError:
                self._sig = None

    def invalidate(self) -> None:
        with self._lock:
            self._sig = None
            self._has_value = False
            self._value = None


class MtimeJsonCache:
    """Thread-safe mtime-cached JSON dict reader with optional normalizer."""

    def __init__(
        self,
        normalizer: Optional[Callable[[dict], dict]] = None,
        default_factory: Optional[Callable[[], dict]] = None,
    ) -> None:
        self._norm = normalizer or (lambda d: dict(d) if isinstance(d, dict) else {})
        self._default = default_factory or (lambda: self._norm({}))
        self._inner: MtimeFileCache[dict] = MtimeFileCache(
            parser=self._parse,
            default_factory=self._default,
        )

    def _parse(self, raw: str) -> dict:
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {}
        return self._norm(data)

    def get(self, path: str) -> dict:
        return self._inner.get(path, clone=lambda d: json.loads(json.dumps(d)))

    def set(self, path: str, value: dict) -> None:
        self._inner.set(path, self._norm(value))

    def invalidate(self) -> None:
        self._inner.invalidate()


_token_caches_lock = threading.Lock()
_token_caches: Dict[str, MtimeFileCache[str]] = {}


def read_token_file(path: str) -> str:
    """Read a stripped single-line Bearer token with mtime caching and transient I/O tolerance."""
    norm_path = os.path.abspath(str(path or ""))
    if not norm_path:
        return ""
    with _token_caches_lock:
        cache = _token_caches.get(norm_path)
        if cache is None:
            cache = MtimeFileCache(
                parser=lambda raw: raw.strip(),
                default_factory=lambda: "",
                retries=2,
            )
            _token_caches[norm_path] = cache
    return cache.get(norm_path)


def invalidate_token_cache(path: Optional[str] = None) -> None:
    with _token_caches_lock:
        if path is None:
            for c in _token_caches.values():
                c.invalidate()
        else:
            c = _token_caches.get(os.path.abspath(str(path)))
            if c is not None:
                c.invalidate()


def get_pc_mcp_token(token_path: str = PC_MCP_TOKEN_PATH) -> str:
    return read_token_file(token_path)


def get_pc_mcp_auth_headers(
    extra: Optional[Dict[str, str]] = None,
    *,
    token_path: str = PC_MCP_TOKEN_PATH,
) -> Dict[str, str]:
    headers = dict(extra) if extra else {}
    tok = get_pc_mcp_token(token_path)
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def get_anima_token(token_path: str = ANIMA_TOKEN_PATH) -> str:
    return read_token_file(token_path)

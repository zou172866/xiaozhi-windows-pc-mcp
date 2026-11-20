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
APP_DIR = os.environ.get("XIAOZHI_APP_DIR") or os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get("XIAOZHI_CONFIG_DIR") or os.path.join(ROOT_DIR, "data")
STATE_DIR = os.environ.get("XIAOZHI_STATE_DIR") or (
    os.path.join(ROOT_DIR, "state")
    if os.path.isdir(os.path.join(ROOT_DIR, "state"))
    else os.path.join(ROOT_DIR, "data")
)
VOICES_DIR = os.environ.get("XIAOZHI_VOICES_DIR") or (
    os.path.join(ROOT_DIR, "voices")
    if os.path.isdir(os.path.join(ROOT_DIR, "voices"))
    else os.path.join(ROOT_DIR, "data", "voices")
)
# Backward-compatibility alias pointing to container config mount (/opt/xiaozhi-esp32-server/data)
DATA_DIR = CONFIG_DIR
PC_MCP_TOKEN_PATH = os.path.join(CONFIG_DIR, "pc_mcp_token.txt")
ANIMA_TOKEN_PATH = os.path.join(CONFIG_DIR, "anima", "studio_token.txt")


def resolve_host_dirs(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Resolve host project/data/voices/gsv_weights paths with backward-compatible derivation from HOST_DATA_DIR (A-1).

    Ensures plain `docker compose up -d` with a legacy `.env` defining only `HOST_DATA_DIR=.../data`
    still derives real Windows host paths for `HOST_VOICES_DIR` and `HOST_GSV_WEIGHTS_DIR` instead of
    sending Linux `/opt/xiaozhi-esp32-server/voices` paths to host 19880 GPT-SoVITS.
    """
    src = os.environ if env is None else env
    raw_proj = (src.get("HOST_PROJECT_DIR") or "").replace("\\", "/").rstrip("/")
    raw_data = (src.get("HOST_DATA_DIR") or "").replace("\\", "/").rstrip("/")
    raw_voices = (src.get("HOST_VOICES_DIR") or "").replace("\\", "/").rstrip("/")
    raw_weights = (src.get("HOST_GSV_WEIGHTS_DIR") or "").replace("\\", "/").rstrip("/")

    default_weights = f"{ROOT_DIR}/models/gsv_weights"
    if raw_proj and raw_proj != ROOT_DIR:
        host_project = raw_proj
    elif raw_data and raw_data != DATA_DIR and raw_data.lower().endswith("/data"):
        host_project = raw_data[:-5]
    else:
        host_project = raw_proj or ROOT_DIR

    has_external_proj = bool(host_project and host_project != ROOT_DIR)
    host_data = raw_data if (raw_data and raw_data != DATA_DIR) else (
        f"{host_project}/data" if has_external_proj else (raw_data or DATA_DIR)
    )
    host_voices = (
        raw_voices
        if (raw_voices and not (raw_voices == VOICES_DIR and has_external_proj))
        else (f"{host_project}/voices" if has_external_proj else (raw_voices or VOICES_DIR))
    )
    host_weights = (
        raw_weights
        if (raw_weights and not (raw_weights == default_weights and has_external_proj))
        else (f"{host_project}/models/gsv_weights" if has_external_proj else (raw_weights or default_weights))
    )
    return {
        "HOST_PROJECT_DIR": host_project,
        "HOST_DATA_DIR": host_data,
        "HOST_VOICES_DIR": host_voices,
        "HOST_GSV_WEIGHTS_DIR": host_weights,
    }

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
        self._path: Optional[str] = None
        self._sig: Optional[tuple[int, int]] = None  # (mtime_ns, st_size)
        self._has_value = False
        self._value: Optional[T] = None

    def get(self, path: str, *, clone: Optional[Callable[[T], T]] = None) -> T:
        norm_path = os.path.abspath(str(path or ""))
        with self._lock:
            try:
                st = os.stat(norm_path)
            except FileNotFoundError:
                self._path = None
                self._sig = None
                self._has_value = False
                self._value = None
                return self._default_factory()
            except OSError:
                if self._has_value and self._path == norm_path and self._value is not None:
                    return clone(self._value) if clone else self._value
                return self._default_factory()

            sig = (int(st.st_mtime_ns), int(st.st_size))
            if self._has_value and self._path == norm_path and self._sig == sig and self._value is not None:
                return clone(self._value) if clone else self._value

            last_err: Optional[Exception] = None
            for attempt in range(self._retries):
                try:
                    with open(norm_path, "r", encoding="utf-8") as f:
                        raw = f.read()
                    parsed = self._parser(raw)
                    self._value = parsed
                    self._path = norm_path
                    self._sig = sig
                    self._has_value = True
                    return clone(parsed) if clone else parsed
                except Exception as exc:
                    last_err = exc
                    if attempt + 1 < self._retries:
                        time.sleep(0.02)
            _ = last_err
            if self._has_value and self._path == norm_path and self._value is not None:
                return clone(self._value) if clone else self._value
            return self._default_factory()

    def set(self, path: str, value: T) -> None:
        norm_path = os.path.abspath(str(path or ""))
        with self._lock:
            self._value = value
            self._path = norm_path
            self._has_value = True
            try:
                st = os.stat(norm_path)
                self._sig = (int(st.st_mtime_ns), int(st.st_size))
            except OSError:
                self._sig = None

    def invalidate(self) -> None:
        with self._lock:
            self._path = None
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


def get_pc_mcp_token(token_path: Optional[str] = None) -> str:
    """Read 18900 Bearer token: env PC_MCP_TOKEN first (A-1 zero mounted secret files), legacy file fallback."""
    use_default = token_path is None or os.path.abspath(str(token_path)) == os.path.abspath(PC_MCP_TOKEN_PATH)
    if use_default:
        env_tok = (os.environ.get("PC_MCP_TOKEN") or "").strip()
        if env_tok:
            return env_tok
        return read_token_file(PC_MCP_TOKEN_PATH)
    return read_token_file(str(token_path))


def get_pc_mcp_auth_headers(
    extra: Optional[Dict[str, str]] = None,
    *,
    token_path: Optional[str] = None,
) -> Dict[str, str]:
    headers = dict(extra) if extra else {}
    tok = get_pc_mcp_token(token_path)
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def get_anima_token(token_path: Optional[str] = None) -> str:
    """Read Anima Brain Bearer token: env ANIMA_STUDIO_TOKEN first (A-1 zero mounted secret files), legacy file fallback."""
    use_default = token_path is None or os.path.abspath(str(token_path)) == os.path.abspath(ANIMA_TOKEN_PATH)
    if use_default:
        env_tok = (os.environ.get("ANIMA_STUDIO_TOKEN") or "").strip()
        if env_tok:
            return env_tok
        return read_token_file(ANIMA_TOKEN_PATH)
    return read_token_file(str(token_path))

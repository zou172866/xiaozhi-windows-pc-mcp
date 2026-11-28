"""Multi-model LLM Fallback Pool router with first-chunk stream safety & cooldown (Phase 2).

Zero import-time side effects: call `apply_llm_fallback_pool_patch()` explicitly from `boot.install_all()`.

Design invariants:
- Zero overhead pass-through when `llm_fallback.enabled` is False or no backup nodes are configured.
- Stream first-chunk protection: fails over ONLY before the first content chunk is emitted;
  never switches models mid-sentence once streaming text has begun.
- Node health cooldown: nodes failing with retryable errors (429/5xx/connect/timeout) enter a
  bounded cooldown window so subsequent turns route directly to healthy nodes, then half-open probe.
- Zero plaintext API keys in `runtime_settings.json` or logs: keys come from env vars (`api_key_env`)
  or the `0600` sidecar `state/.llm_fallback_secrets.json`; status APIs expose only masked previews.
- Hard SSRF URL validation: rejects internal management ports, link-local, multicast, and unsafe
  schemes while permitting local loopback inference gateways on non-management ports.
"""

from __future__ import annotations

import functools
import ipaddress
import json
import os
import re
import socket
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import patch_registry
import runtime_settings
import xz_common

SECRETS_PATH = os.path.join(xz_common.STATE_DIR, ".llm_fallback_secrets.json")
MAX_FALLBACK_NODES = 6
DEFAULT_COOLDOWN_SECONDS = 60
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 520, 522, 524})
AUTH_FAILOVER_HTTP_STATUSES = frozenset({401, 403})
BLOCKED_LOCAL_PORTS = frozenset({18000, 18003, 18080, 18787, 18900, 19880})
_SAFE_NODE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_SAFE_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SECRET_REDACT_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[已脱敏密钥]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[已脱敏密钥]"),
    (re.compile(r"(?i)\b(bearer|token|apikey|api_key|secret|password)\s*[:=]\s*[^\s,;\"']{6,}"), r"\1=[已脱敏]"),
    (re.compile(r"(?i)([?&](?:key|token|access_token|secret)=)[^&\s]+"), r"\1[已脱敏]"),
)


def _redact_error(raw: Any, limit: int = 180) -> str:
    txt = " ".join(str(raw or "").split())
    for pat, repl in _SECRET_REDACT_PATTERNS:
        txt = pat.sub(repl, txt)
    if len(txt) > limit:
        txt = txt[: max(1, limit - 1)].rstrip() + "…"
    return txt


def mask_secret_preview(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}***{s[-3:]}"


def validate_fallback_base_url(raw_url: str, *, resolve_dns: bool = False) -> str:
    """Validate an OpenAI-compatible fallback base URL against SSRF & credential-in-URL hazards."""
    s = str(raw_url or "").strip().rstrip("/")
    if not s:
        raise ValueError("备用节点 base_url 不能为空")
    parsed = urlparse(s)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("https", "http"):
        raise ValueError("base_url 仅支持 https:// 或本地回环 http://")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url 格式非法，禁止包含内嵌凭据、查询参数或片段")

    host = parsed.hostname.lower().strip("[]")
    port = parsed.port or (443 if scheme == "https" else 80)

    is_loopback_name = host in ("localhost", "127.0.0.1", "::1", "host.docker.internal")
    if port in BLOCKED_LOCAL_PORTS and (is_loopback_name or host in ("anima-brain", "xiaozhi-anima-brain", "xiaozhi-esp32-server")):
        raise ValueError(f"禁止指向内部管理端口 {port}")
    if host in ("anima-brain", "xiaozhi-anima-brain", "xiaozhi-esp32-server"):
        raise ValueError("禁止指向内部容器管理服务")

    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback:
            if port in BLOCKED_LOCAL_PORTS:
                raise ValueError(f"禁止指向本机管理端口 {port}")
        elif ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError(f"禁止指向不安全的保留/链路本地地址: {host}")
        elif not ip.is_global:
            # Reject container bridge / internal LAN metadata endpoints for HTTP(S) fallback
            if scheme != "https" or ip in ipaddress.ip_network("172.16.0.0/12"):
                raise ValueError(f"禁止指向内网/容器网桥地址: {host}")
    except ValueError as exc:
        if "禁止指向" in str(exc):
            raise
        # Domain name
        if scheme == "http" and not is_loopback_name:
            raise ValueError("远程备用节点必须使用 https://（仅本机回环推理网关允许 http://）")
        if resolve_dns and not is_loopback_name:
            try:
                infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as dns_exc:
                raise ValueError(f"无法解析域名 {host}: {dns_exc}") from dns_exc
            for info in infos:
                addr = info[4][0]
                ip_obj = ipaddress.ip_address(addr)
                if not ip_obj.is_global:
                    raise ValueError(f"域名 {host} 解析到非公网地址 {addr}，已拦截")

    return s


def classify_llm_error(exc: BaseException) -> Tuple[bool, bool, str]:
    """Return `(retryable_same_or_backup, failover_to_other_node_only, reason_label)`."""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        resp = getattr(exc, "response", None)
        status_code = getattr(resp, "status_code", None)
    try:
        sc = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        sc = None

    if sc in RETRYABLE_HTTP_STATUSES:
        return True, False, f"http_{sc}"
    if sc in AUTH_FAILOVER_HTTP_STATUSES:
        return False, True, f"http_{sc}"
    if sc is not None and 400 <= sc < 500:
        return False, False, f"http_{sc}"

    cls_name = type(exc).__name__.lower()
    if any(tok in cls_name for tok in ("ratelimit", "timeout", "apiconnection", "internalserver", "serviceunavailable", "connecterror")):
        return True, False, cls_name
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True, False, cls_name
    msg = str(exc).lower()
    if any(tok in msg for tok in ("429", "rate limit", "too many requests", "502", "503", "504", "timed out", "connection reset", "connection refused", "temporarily unavailable")):
        return True, False, "transient_msg"
    return False, False, cls_name


class _FirstChunkProtectedStream:
    """Wraps a streaming completion iterator after safely pre-fetching its first chunk."""

    def __init__(self, first_chunk: Any, rest_iter: Any, inner_stream: Any) -> None:
        self._first = first_chunk
        self._has_first = True
        self._rest = rest_iter
        self._inner = inner_stream

    def __iter__(self):
        return self

    def __next__(self):
        if self._has_first:
            self._has_first = False
            chunk = self._first
            self._first = None
            return chunk
        return next(self._rest)

    def close(self):
        close_fn = getattr(self._inner, "close", None)
        if callable(close_fn):
            return close_fn()
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _EmptyStream:
    def __init__(self, inner_stream: Any) -> None:
        self._inner = inner_stream

    def __iter__(self):
        return self

    def __next__(self):
        raise StopIteration

    def close(self):
        close_fn = getattr(self._inner, "close", None)
        if callable(close_fn):
            return close_fn()
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class LLMFallbackPool:
    """Manages ordered backup OpenAI-compatible nodes, secret sidecar, and health cooldown."""

    def __init__(
        self,
        *,
        settings_path: Optional[str] = None,
        secrets_path: Optional[str] = None,
        now_fn: Optional[Callable[[], float]] = None,
        client_factory: Optional[Callable[[str, str, float], Any]] = None,
    ) -> None:
        self.settings_path = settings_path
        self.secrets_path = os.path.abspath(secrets_path or SECRETS_PATH)
        self._now_fn = now_fn or time.time
        self._client_factory = client_factory or self._default_client_factory

        self._lock = threading.RLock()
        self._secrets_cache: Optional[Dict[str, str]] = None
        self._secrets_sig: Optional[Tuple[int, int]] = None
        self._clients: Dict[Tuple[str, str], Any] = {}
        self._health: Dict[str, Dict[str, Any]] = {}
        self._total_requests = 0
        self._total_failovers = 0
        self._last_active_node = "primary"
        self._last_failover_reason = ""
        self._last_failover_at = 0.0

    @staticmethod
    def _default_client_factory(base_url: str, api_key: str, timeout: float) -> Any:
        import openai

        return openai.OpenAI(api_key=api_key or "sk-placeholder", base_url=base_url, timeout=timeout)

    def _now(self) -> float:
        return float(self._now_fn())

    def _load_secrets_locked(self) -> Dict[str, str]:
        if not os.path.isfile(self.secrets_path):
            self._secrets_cache = {}
            self._secrets_sig = None
            return {}
        try:
            st = os.stat(self.secrets_path)
            sig = (st.st_mtime_ns, st.st_size)
            if self._secrets_cache is not None and self._secrets_sig == sig:
                return dict(self._secrets_cache)
            with open(self.secrets_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cleaned = {
                str(k): str(v).strip()
                for k, v in (data.items() if isinstance(data, dict) else [])
                if _SAFE_NODE_ID_RE.match(str(k)) and str(v or "").strip()
            }
            self._secrets_cache = cleaned
            self._secrets_sig = sig
            return dict(cleaned)
        except Exception:
            return {}

    def update_node_secrets(self, updates: Dict[str, Optional[str]]) -> None:
        """Persist per-node API keys to 0600 sidecar file (never inside runtime_settings.json)."""
        with self._lock:
            cur = self._load_secrets_locked()
            for node_id, raw_val in (updates or {}).items():
                nid = str(node_id or "").strip()
                if not _SAFE_NODE_ID_RE.match(nid):
                    raise ValueError(f"非法节点 ID: {nid!r}")
                if raw_val is None or str(raw_val).strip() == "":
                    cur.pop(nid, None)
                else:
                    cur[nid] = str(raw_val).strip()
            xz_common.atomic_write_json(self.secrets_path, cur)
            try:
                os.chmod(self.secrets_path, 0o600)
            except OSError:
                pass
            self._secrets_cache = dict(cur)
            try:
                st = os.stat(self.secrets_path)
                self._secrets_sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._secrets_sig = None
            self._clients.clear()

    def _resolve_node_key(self, node: Dict[str, Any], sidecar: Dict[str, str]) -> Tuple[str, str]:
        """Return `(api_key, source)` without leaking secret into config."""
        nid = str(node.get("id") or "")
        env_var = str(node.get("api_key_env") or "").strip()
        if env_var and _SAFE_ENV_VAR_RE.match(env_var):
            ev = (os.environ.get(env_var) or "").strip()
            if ev:
                return ev, f"env:{env_var}"
        sc = (sidecar.get(nid) or "").strip()
        if sc:
            return sc, "sidecar"
        return "", "unset"

    def get_config(self) -> Dict[str, Any]:
        return runtime_settings.get_llm_fallback(self.settings_path)

    def _get_node_health_locked(self, node_id: str) -> Dict[str, Any]:
        h = self._health.get(node_id)
        if h is None:
            h = {
                "cooldown_until": 0.0,
                "fail_count": 0,
                "success_count": 0,
                "last_error": "",
                "last_error_at": 0.0,
            }
            self._health[node_id] = h
        return h

    def _is_in_cooldown_locked(self, node_id: str, now: float) -> bool:
        h = self._health.get(node_id)
        return bool(h and float(h.get("cooldown_until") or 0.0) > now)

    def _record_success(self, node_id: str) -> None:
        with self._lock:
            h = self._get_node_health_locked(node_id)
            h["cooldown_until"] = 0.0
            h["success_count"] = int(h.get("success_count") or 0) + 1
            self._last_active_node = node_id

    def _record_failure(self, node_id: str, exc: BaseException, cooldown_seconds: int) -> None:
        now = self._now()
        with self._lock:
            h = self._get_node_health_locked(node_id)
            h["fail_count"] = int(h.get("fail_count") or 0) + 1
            h["cooldown_until"] = now + max(5, int(cooldown_seconds))
            h["last_error"] = _redact_error(exc)
            h["last_error_at"] = now

    def _get_or_create_client(self, base_url: str, api_key: str, timeout: float) -> Any:
        cache_key = (base_url, api_key)
        with self._lock:
            cli = self._clients.get(cache_key)
            if cli is not None:
                return cli
        cli = self._client_factory(base_url, api_key, timeout)
        with self._lock:
            if len(self._clients) >= 16:
                self._clients.clear()
            self._clients[cache_key] = cli
        return cli

    def execute_chat_completion(
        self,
        *,
        primary_create: Callable[..., Any],
        primary_model: str,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
        timeout: float = 30.0,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> Tuple[Any, str, str]:
        """Run chat completion with automatic failover & stream first-chunk protection.

        Returns `(result_or_stream, used_model_name, used_node_id)`.
        """
        cfg = self.get_config()
        enabled = bool(cfg.get("enabled", False))
        cooldown_sec = int(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
        raw_nodes = [n for n in (cfg.get("nodes") or []) if isinstance(n, dict) and n.get("enabled", True)]

        if not enabled or not raw_nodes:
            res = primary_create(*args, **kwargs)
            return res, str(kwargs.get("model") or primary_model or ""), "primary"

        now = self._now()
        with self._lock:
            self._total_requests += 1
            sidecar = self._load_secrets_locked()
            primary_cooling = self._is_in_cooldown_locked("primary", now)

        # Build ordered candidate list: healthy nodes first, cooling nodes as last-resort tail
        candidates: List[Dict[str, Any]] = []
        cooling_tail: List[Dict[str, Any]] = []

        primary_cand = {"id": "primary", "label": "主模型", "is_primary": True, "model": str(kwargs.get("model") or primary_model or "")}
        if primary_cooling:
            cooling_tail.append(primary_cand)
        else:
            candidates.append(primary_cand)

        for n in raw_nodes[:MAX_FALLBACK_NODES]:
            nid = str(n.get("id") or "")
            url = str(n.get("base_url") or "").strip()
            mdl = str(n.get("model") or "").strip()
            if not nid or not url or not mdl:
                continue
            try:
                safe_url = validate_fallback_base_url(url)
            except ValueError:
                continue
            api_key, _src = self._resolve_node_key(n, sidecar)
            cand = {
                "id": nid,
                "label": str(n.get("label") or nid),
                "is_primary": False,
                "base_url": safe_url,
                "model": mdl,
                "api_key": api_key,
            }
            with self._lock:
                is_cool = self._is_in_cooldown_locked(nid, now)
            if is_cool:
                cooling_tail.append(cand)
            else:
                candidates.append(cand)

        ordered = candidates + cooling_tail
        is_stream = bool(kwargs.get("stream", False))
        last_exc: Optional[BaseException] = None

        for idx, cand in enumerate(ordered):
            nid = cand["id"]
            mdl = cand["model"]
            req_kwargs = dict(kwargs)
            req_kwargs["model"] = mdl

            try:
                if cand["is_primary"]:
                    raw_res = primary_create(*args, **req_kwargs)
                else:
                    cli = self._get_or_create_client(cand["base_url"], cand["api_key"], timeout)
                    raw_res = cli.chat.completions.create(*args, **req_kwargs)

                if is_stream:
                    it = iter(raw_res)
                    try:
                        first_chunk = next(it)
                    except StopIteration:
                        wrapped_res: Any = _EmptyStream(raw_res)
                    else:
                        wrapped_res = _FirstChunkProtectedStream(first_chunk, it, raw_res)
                else:
                    wrapped_res = raw_res

                self._record_success(nid)
                if nid != "primary":
                    with self._lock:
                        self._total_failovers += 1
                        self._last_failover_at = self._now()
                        if last_exc is not None:
                            self._last_failover_reason = _redact_error(last_exc, limit=120)
                    msg = f"LLM 自动兜底切换成功 | node={nid} ({cand['label']}) | model={mdl}"
                    if callable(log_fn):
                        try:
                            log_fn(msg)
                        except Exception:
                            pass
                    else:
                        print(f"[LLMFallback] {msg}", flush=True)
                return wrapped_res, mdl, nid
            except Exception as exc:
                last_exc = exc
                retryable, auth_failover, reason = classify_llm_error(exc)
                has_next = idx + 1 < len(ordered)
                should_switch = has_next and (retryable or auth_failover)
                if retryable or auth_failover:
                    self._record_failure(nid, exc, cooldown_sec)
                if not should_switch:
                    raise
                warn_msg = f"LLM 节点 {nid} 异常({reason})，自动切换下一备用节点"
                if callable(log_fn):
                    try:
                        log_fn(warn_msg)
                    except Exception:
                        pass

        assert last_exc is not None
        raise last_exc

    def get_status(self) -> Dict[str, Any]:
        cfg = self.get_config()
        now = self._now()
        with self._lock:
            sidecar = self._load_secrets_locked()
            primary_h = dict(self._health.get("primary") or {})
            nodes_out: List[Dict[str, Any]] = []
            for n in (cfg.get("nodes") or [])[:MAX_FALLBACK_NODES]:
                nid = str(n.get("id") or "")
                key_val, key_src = self._resolve_node_key(n, sidecar)
                nh = self._health.get(nid) or {}
                rem = max(0, int(round(float(nh.get("cooldown_until") or 0.0) - now)))
                nodes_out.append(
                    {
                        "id": nid,
                        "label": str(n.get("label") or nid),
                        "base_url": str(n.get("base_url") or ""),
                        "model": str(n.get("model") or ""),
                        "api_key_env": str(n.get("api_key_env") or ""),
                        "enabled": bool(n.get("enabled", True)),
                        "key_configured": bool(key_val),
                        "key_source": key_src,
                        "key_masked": mask_secret_preview(key_val),
                        "cooling_seconds": rem,
                        "fail_count": int(nh.get("fail_count") or 0),
                        "success_count": int(nh.get("success_count") or 0),
                        "last_error": str(nh.get("last_error") or ""),
                    }
                )
            p_rem = max(0, int(round(float(primary_h.get("cooldown_until") or 0.0) - now)))
            return {
                "ok": True,
                "config": {
                    "enabled": bool(cfg.get("enabled", False)),
                    "cooldown_seconds": int(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
                },
                "primary": {
                    "cooling_seconds": p_rem,
                    "fail_count": int(primary_h.get("fail_count") or 0),
                    "success_count": int(primary_h.get("success_count") or 0),
                    "last_error": str(primary_h.get("last_error") or ""),
                },
                "nodes": nodes_out,
                "stats": {
                    "total_requests": self._total_requests,
                    "total_failovers": self._total_failovers,
                    "last_active_node": self._last_active_node,
                    "last_failover_reason": self._last_failover_reason,
                    "last_failover_at": self._last_failover_at,
                },
            }

    def reset_health(self) -> None:
        with self._lock:
            self._health.clear()
            self._last_active_node = "primary"
            self._last_failover_reason = ""


# ---------------------------------------------------------------------------
# Singleton & patch installer (zero import-time side effects)
# ---------------------------------------------------------------------------

_pool_lock = threading.Lock()
_pool: Optional[LLMFallbackPool] = None


def get_fallback_pool() -> LLMFallbackPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = LLMFallbackPool()
        return _pool


def reset_fallback_pool(pool: Optional[LLMFallbackPool] = None) -> None:
    global _pool
    with _pool_lock:
        _pool = pool


def wrap_llm_client_for_fallback(llm_provider: Any, *, log_fn=None) -> bool:
    """Idempotently wrap `llm_provider.client.chat.completions.create` with Fallback Pool."""
    client = getattr(llm_provider, "client", None)
    chat = getattr(client, "chat", None)
    completions = getattr(chat, "completions", None)
    orig_create = getattr(completions, "create", None)
    if completions is None or not callable(orig_create):
        return False
    if getattr(completions, "_xiaozhi_fallback_pool_wrapped", False):
        return True

    @functools.wraps(orig_create)
    def _fallback_create(*args, **kwargs):
        pool = get_fallback_pool()
        primary_model = str(kwargs.get("model") or getattr(llm_provider, "model_name", "") or "")
        timeout = float(getattr(llm_provider, "timeout", 30.0) or 30.0)
        res, _used_model, _used_node = pool.execute_chat_completion(
            primary_create=orig_create,
            primary_model=primary_model,
            args=args,
            kwargs=kwargs,
            timeout=timeout,
            log_fn=log_fn,
        )
        return res

    completions.create = _fallback_create
    completions._xiaozhi_fallback_pool_wrapped = True
    return True


def apply_llm_fallback_pool_patch() -> None:
    """Install OpenAI.LLMProvider.__init__ hook to enable automatic multi-model failover (Phase 2)."""
    from core.providers.llm.openai import openai as openai_llm_mod

    cls = openai_llm_mod.LLMProvider

    def _log_info(msg: str) -> None:
        openai_llm_mod.logger.bind(tag=openai_llm_mod.TAG).info(msg)

    def _wrap_init(_orig_init):
        @functools.wraps(_orig_init)
        def _patched_init(self, config, *args, **kwargs):
            _orig_init(self, config, *args, **kwargs)
            try:
                wrap_llm_client_for_fallback(self, log_fn=_log_info)
            except Exception:
                pass

        return _patched_init

    already_applied = bool(getattr(cls, "_llm_fallback_pool_patched", False))
    ok, _ = patch_registry.install_patch(
        "llm_fallback_pool",
        cls,
        "__init__",
        _wrap_init,
        category="experience",
        target="OpenAI.LLMProvider.__init__",
        required_params=("self", "config"),
        require_async=False,
        flag_obj=cls,
        flag_attr="_llm_fallback_pool_patched",
    )
    if ok and not already_applied:
        print("[LLMFallback] 已挂载主对话多模型自动兜底切换补丁 (llm_fallback_pool)", flush=True)

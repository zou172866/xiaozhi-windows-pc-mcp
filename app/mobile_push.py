"""Domestic zero-proxy lightweight mobile push notification bridge (Phase 3 ⑥).

Supports 5 domestic direct-connect push channels with zero extra dependencies (`httpx` + stdlib):
  1. `bark`       - iOS Bark (`https://api.day.app/<key>` or self-hosted HTTPS Bark)
  2. `feishu`     - Feishu / Lark custom bot webhook (+ optional HMAC-SHA256 signing)
  3. `wecom`      - WeCom (Enterprise WeChat) group bot webhook
  4. `pushplus`   - PushPlus WeChat push (`https://www.pushplus.plus/send`)
  5. `serverchan` - ServerChan Turbo (`sctapi.ftqq.com`) / SC3 (`<uid>.push.ft07.com`)

Design invariants:
- Strictly disabled by default (`mobile_push.enabled = False`): zero outbound HTTP calls,
  zero background threads, zero idle memory overhead.
- Zero plaintext secrets in `runtime_settings.json` or logs: credentials come from `.env`
  (`MOBILE_PUSH_*`) or the gitignored `0600` sidecar `state/.mobile_push_secrets.json`;
  status APIs expose only masked strings (`configured`, `source`, `masked`).
- Hard SSRF guard: HTTPS-only, port 443, `follow_redirects=False`, official host allowlists
  and global-IP DNS checks (`ipaddress.is_global`) before connecting.
- Mandatory content sanitization via `recall_guard` + secret-pattern redaction before sending.
"""

from __future__ import annotations

import base64
from collections import deque
from datetime import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

import recall_guard
import runtime_events
import runtime_settings
import xz_common

STATE_DIR = xz_common.STATE_DIR
SECRETS_PATH = os.path.join(STATE_DIR, ".mobile_push_secrets.json")

VALID_PROVIDERS: tuple[str, ...] = ("bark", "feishu", "wecom", "pushplus", "serverchan")
PROVIDER_LABELS_ZH: Dict[str, str] = {
    "bark": "Bark (iOS 通知栏)",
    "feishu": "飞书群机器人",
    "wecom": "企业微信群机器人",
    "pushplus": "PushPlus 微信推送",
    "serverchan": "Server酱 (微信推送)",
}

SECRET_FIELD_ENV_MAP: Dict[str, str] = {
    "bark_url": "MOBILE_PUSH_BARK_URL",
    "feishu_webhook": "MOBILE_PUSH_FEISHU_WEBHOOK",
    "feishu_secret": "MOBILE_PUSH_FEISHU_SECRET",
    "wecom_webhook": "MOBILE_PUSH_WECOM_WEBHOOK",
    "pushplus_token": "MOBILE_PUSH_PUSHPLUS_TOKEN",
    "serverchan_key": "MOBILE_PUSH_SERVERCHAN_KEY",
}

PROVIDER_REQUIRED_SECRET: Dict[str, str] = {
    "bark": "bark_url",
    "feishu": "feishu_webhook",
    "wecom": "wecom_webhook",
    "pushplus": "pushplus_token",
    "serverchan": "serverchan_key",
}

_FEISHU_HOSTS = frozenset({"open.feishu.cn", "open.larksuite.com"})
_WECOM_HOSTS = frozenset({"qyapi.weixin.qq.com"})
_PUSHPLUS_HOSTS = frozenset({"www.pushplus.plus"})
_SC3_KEY_RE = re.compile(r"^sctp(\d+)t[A-Za-z0-9_-]{4,120}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_EMOTION_TAG_RE = re.compile(r"\[(?:calm|gentle|happy|playful|shy|serious|tender)\]\s*", re.IGNORECASE)
_SECRET_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[已脱敏密钥]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[已脱敏密钥]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[已脱敏密钥]"),
    (re.compile(r"(?i)\b(bearer|token|apikey|api_key|secret|password)\s*[:=]\s*[^\s,;\"']{6,}"), r"\1=[已脱敏]"),
    (re.compile(r"(?i)([?&](?:key|token|access_token|secret)=)[^&\s]+"), r"\1[已脱敏]"),
)

DEDUP_WINDOW_SECONDS = 600.0
MAX_HISTORY = 20
HTTP_TIMEOUT_SECONDS = 8.0


def mask_secret_value(raw: str) -> str:
    """Return a safe masked preview without leaking usable credentials."""
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.startswith(("https://", "http://")):
        try:
            p = urlparse(s)
            host = p.hostname or ""
            tail = (p.path or "").rstrip("/")
            if len(tail) > 10:
                tail_mask = f"{tail[:4]}***{tail[-3:]}"
            elif tail:
                tail_mask = "/***"
            else:
                tail_mask = ""
            q_mask = "?***" if p.query else ""
            return f"{p.scheme}://{host}{tail_mask}{q_mask}"
        except Exception:
            pass
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:3]}***{s[-3:]}"


def redact_push_error(raw: Any) -> str:
    """Strip any accidental tokens/URLs with query keys from error descriptions."""
    text = str(raw or "").strip()
    if not text:
        return ""
    for pat, repl in _SECRET_REDACT_PATTERNS:
        text = pat.sub(repl, text)
    # Also mask full Bark/Feishu webhook paths if they somehow appear in httpx errors
    text = re.sub(r"https://[^\s\"']+", lambda m: mask_secret_value(m.group(0)), text)
    return text[:220]


def sanitize_push_text(text: str, *, max_len: int = 420) -> str:
    """Remove emotion tags and redact secrets/PII before sending text to external push relays."""
    s = _EMOTION_TAG_RE.sub("", str(text or "")).strip()
    if not s:
        return ""
    try:
        s, _ = recall_guard.sanitize_recall_context(s, record_stats=False)
    except Exception:
        pass
    for pat, repl in _SECRET_REDACT_PATTERNS:
        s = pat.sub(repl, s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s[:max_len]


def _is_in_quiet_hours(now_dt: datetime, quiet_start: str, quiet_end: str) -> bool:
    try:
        sh, sm = (int(x) for x in quiet_start.split(":", 1))
        eh, em = (int(x) for x in quiet_end.split(":", 1))
    except Exception:
        return False
    cur_m = now_dt.hour * 60 + now_dt.minute
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= cur_m < end_m
    return cur_m >= start_m or cur_m < end_m


def validate_outbound_https_url(
    url: str,
    *,
    allowed_hosts: Optional[frozenset[str]] = None,
    allowed_host_suffix: Optional[str] = None,
    required_path_prefix: Optional[str] = None,
    resolver: Optional[Callable[[str, int], List[str]]] = None,
) -> str:
    """Enforce strict HTTPS + port 443 + host allowlist + global-IP DNS anti-SSRF guard."""
    clean = str(url or "").strip()
    if not clean or len(clean) > 512:
        raise ValueError("推送目标 URL 为空或过长")
    parsed = urlparse(clean)
    if parsed.scheme.lower() != "https":
        raise ValueError("安全门禁仅允许 HTTPS 推送地址")
    if parsed.username or parsed.password:
        raise ValueError("推送 URL 不允许内嵌账号密码")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("推送 URL 缺少合法主机名")
    port = parsed.port or 443
    if port != 443:
        raise ValueError("安全门禁仅允许标准 443 端口")
    if allowed_hosts is not None and host not in allowed_hosts:
        if not (allowed_host_suffix and host.endswith(allowed_host_suffix)):
            raise ValueError(f"目标主机不在通道官方白名单内: {host}")
    elif allowed_host_suffix and not host.endswith(allowed_host_suffix):
        raise ValueError(f"目标主机不在通道域名白名单内: {host}")
    if required_path_prefix and not (parsed.path or "").startswith(required_path_prefix):
        raise ValueError(f"Webhook 路径格式不符合通道规范（应以 {required_path_prefix} 开头）")

    # Reject non-global IP literals immediately
    try:
        lit = ipaddress.ip_address(host)
        if not lit.is_global:
            raise ValueError("安全门禁拒绝向内网或回环 IP 发送推送")
        return clean
    except ValueError as exc:
        if "安全门禁" in str(exc):
            raise

    # Resolve DNS and verify EVERY resolved IP is global (prevents DNS rebinding / internal Docker SSRF)
    if callable(resolver):
        ips = list(resolver(host, port) or [])
    else:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = [str(sockaddr[0]) for _fam, _type, _proto, _canon, sockaddr in infos]
    if not ips:
        raise ValueError(f"无法解析目标域名: {host}")
    for ip_str in ips:
        ip_obj = ipaddress.ip_address(ip_str)
        if not ip_obj.is_global:
            raise ValueError(f"目标域名 {host} 解析到非公网地址，已按防 SSRF 拦截")
    return clean


def _write_secrets_atomic_0600(path: str, payload: Dict[str, str]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
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


def build_provider_request(
    provider: str,
    title: str,
    body: str,
    secrets: Dict[str, str],
    *,
    group: str = "Xiaozhi",
    now_ts: Optional[float] = None,
    resolver: Optional[Callable[[str, int], List[str]]] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """Build `(url, json_body, headers)` for the selected domestic push provider with SSRF validation."""
    prov = str(provider or "").strip().lower()
    if prov not in VALID_PROVIDERS:
        raise ValueError(f"不支持的手机推送通道: {provider}")

    safe_title = sanitize_push_text(title, max_len=80) or "小智陪伴提醒"
    safe_body = sanitize_push_text(body, max_len=420)
    if not safe_body:
        raise ValueError("推送正文为空或已全部脱敏")

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "xiaozhi-mobile-push/1.0",
    }

    if prov == "bark":
        raw_url = str(secrets.get("bark_url") or "").strip().rstrip("/")
        if not raw_url:
            raise ValueError("尚未配置 Bark 推送 URL（MOBILE_PUSH_BARK_URL）")
        parsed = urlparse(raw_url)
        if not (parsed.path or "").strip("/"):
            raise ValueError("Bark URL 缺少设备 Key 路径，例如 https://api.day.app/你的设备Key")
        url = validate_outbound_https_url(raw_url, resolver=resolver)
        payload = {
            "title": safe_title,
            "body": safe_body,
            "group": str(group or "Xiaozhi")[:32],
            "isArchive": 1,
        }
        return url, payload, headers

    if prov == "feishu":
        raw_url = str(secrets.get("feishu_webhook") or "").strip()
        if not raw_url:
            raise ValueError("尚未配置飞书机器人 Webhook（MOBILE_PUSH_FEISHU_WEBHOOK）")
        url = validate_outbound_https_url(
            raw_url,
            allowed_hosts=_FEISHU_HOSTS,
            required_path_prefix="/open-apis/bot/v2/hook/",
            resolver=resolver,
        )
        payload: Dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": f"{safe_title}\n{safe_body}"},
        }
        sign_secret = str(secrets.get("feishu_secret") or "").strip()
        if sign_secret:
            ts_str = str(int(now_ts if now_ts is not None else time.time()))
            string_to_sign = f"{ts_str}\n{sign_secret}"
            digest = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
            payload["timestamp"] = ts_str
            payload["sign"] = base64.b64encode(digest).decode("ascii")
        return url, payload, headers

    if prov == "wecom":
        raw_url = str(secrets.get("wecom_webhook") or "").strip()
        if not raw_url:
            raise ValueError("尚未配置企业微信机器人 Webhook（MOBILE_PUSH_WECOM_WEBHOOK）")
        url = validate_outbound_https_url(
            raw_url,
            allowed_hosts=_WECOM_HOSTS,
            required_path_prefix="/cgi-bin/webhook/send",
            resolver=resolver,
        )
        payload = {
            "msgtype": "text",
            "text": {"content": f"{safe_title}\n{safe_body}"},
        }
        return url, payload, headers

    if prov == "pushplus":
        token = str(secrets.get("pushplus_token") or "").strip()
        if not token:
            raise ValueError("尚未配置 PushPlus Token（MOBILE_PUSH_PUSHPLUS_TOKEN）")
        if not _SAFE_TOKEN_RE.match(token):
            raise ValueError("PushPlus Token 格式非法")
        url = validate_outbound_https_url(
            "https://www.pushplus.plus/send",
            allowed_hosts=_PUSHPLUS_HOSTS,
            resolver=resolver,
        )
        payload = {
            "token": token,
            "title": safe_title,
            "content": safe_body,
            "template": "txt",
        }
        return url, payload, headers

    # serverchan (Turbo or SC3)
    sckey = str(secrets.get("serverchan_key") or "").strip()
    if not sckey:
        raise ValueError("尚未配置 Server酱 SendKey（MOBILE_PUSH_SERVERCHAN_KEY）")
    if not _SAFE_TOKEN_RE.match(sckey):
        raise ValueError("Server酱 SendKey 格式非法")
    m_sc3 = _SC3_KEY_RE.match(sckey)
    if m_sc3:
        uid = m_sc3.group(1)
        raw_url = f"https://{uid}.push.ft07.com/send/{sckey}.send"
        url = validate_outbound_https_url(
            raw_url,
            allowed_host_suffix=".push.ft07.com",
            required_path_prefix="/send/",
            resolver=resolver,
        )
    else:
        raw_url = f"https://sctapi.ftqq.com/{sckey}.send"
        url = validate_outbound_https_url(
            raw_url,
            allowed_hosts=frozenset({"sctapi.ftqq.com"}),
            required_path_prefix="/",
            resolver=resolver,
        )
    payload = {"title": safe_title, "desp": safe_body}
    return url, payload, headers


def _check_provider_response(provider: str, status_code: int, data: Any) -> Tuple[bool, str]:
    if status_code < 200 or status_code >= 300:
        return False, f"推送通道返回 HTTP {status_code}"
    if not isinstance(data, dict):
        return True, "ok"
    if provider == "bark":
        code = data.get("code")
        if code is not None and int(code) != 200:
            return False, f"Bark 返回错误: {data.get('message') or code}"
        return True, "ok"
    if provider == "feishu":
        code = data.get("code") if "code" in data else data.get("StatusCode")
        if code is not None and int(code) != 0:
            return False, f"飞书返回错误: {data.get('msg') or data.get('StatusMessage') or code}"
        return True, "ok"
    if provider == "wecom":
        errcode = data.get("errcode")
        if errcode is not None and int(errcode) != 0:
            return False, f"企微返回错误: {data.get('errmsg') or errcode}"
        return True, "ok"
    if provider == "pushplus":
        code = data.get("code")
        if code is not None and int(code) != 200:
            return False, f"PushPlus 返回错误: {data.get('msg') or code}"
        return True, "ok"
    if provider == "serverchan":
        code = data.get("code")
        if code is not None and int(code) != 0:
            return False, f"Server酱返回错误: {data.get('message') or data.get('info') or code}"
        return True, "ok"
    return True, "ok"


class MobilePushBridge:
    """Stateful domestic mobile push dispatcher with secret isolation, dedup, and rate limiting."""

    def __init__(
        self,
        *,
        settings_path: Optional[str] = None,
        secrets_path: Optional[str] = None,
        now_fn: Optional[Callable[[], float]] = None,
        now_dt_fn: Optional[Callable[[], datetime]] = None,
        http_poster: Optional[Callable[[str, Dict[str, Any], Dict[str, str], float], Tuple[int, Any]]] = None,
        resolver: Optional[Callable[[str, int], List[str]]] = None,
    ) -> None:
        self.settings_path = settings_path
        self.secrets_path = os.path.abspath(secrets_path or SECRETS_PATH)
        self._now_fn = now_fn or time.time
        self._now_dt_fn = now_dt_fn or datetime.now
        self._http_poster = http_poster
        self._resolver = resolver

        self._lock = threading.RLock()
        self._day_key: str = ""
        self._daily_count: int = 0
        self._recent_hashes: Dict[str, float] = {}
        self._history: deque[Dict[str, Any]] = deque(maxlen=MAX_HISTORY)

    def get_config(self) -> Dict[str, Any]:
        return runtime_settings.get_mobile_push(self.settings_path)

    def is_enabled(self) -> bool:
        try:
            return bool(self.get_config().get("enabled", False))
        except Exception:
            return False

    def _read_file_secrets(self) -> Dict[str, str]:
        if not os.path.isfile(self.secrets_path):
            return {}
        try:
            with open(self.secrets_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return {}
            out: Dict[str, str] = {}
            for k in SECRET_FIELD_ENV_MAP:
                v = raw.get(k)
                if isinstance(v, str) and v.strip():
                    out[k] = v.strip()
            return out
        except Exception:
            return {}

    def get_effective_secrets(self) -> Dict[str, str]:
        """Resolve secrets from `0600` local file first, falling back to `.env` env vars."""
        file_sec = self._read_file_secrets()
        resolved: Dict[str, str] = {}
        for field, env_key in SECRET_FIELD_ENV_MAP.items():
            fv = file_sec.get(field, "").strip()
            ev = str(os.environ.get(env_key, "") or "").strip()
            resolved[field] = fv or ev
        return resolved

    def get_secrets_summary(self) -> Dict[str, Dict[str, Any]]:
        """Return safe masked status for every credential field (never returns plaintext)."""
        file_sec = self._read_file_secrets()
        summary: Dict[str, Dict[str, Any]] = {}
        for field, env_key in SECRET_FIELD_ENV_MAP.items():
            fv = file_sec.get(field, "").strip()
            ev = str(os.environ.get(env_key, "") or "").strip()
            if fv:
                summary[field] = {
                    "configured": True,
                    "source": "file",
                    "masked": mask_secret_value(fv),
                    "env_var": env_key,
                }
            elif ev:
                summary[field] = {
                    "configured": True,
                    "source": "env",
                    "masked": mask_secret_value(ev),
                    "env_var": env_key,
                }
            else:
                summary[field] = {
                    "configured": False,
                    "source": "none",
                    "masked": "",
                    "env_var": env_key,
                }
        return summary

    def update_secrets(self, updates: Dict[str, Optional[str]]) -> Dict[str, Dict[str, Any]]:
        """Persist non-empty secret fields or delete cleared fields in `0600` isolated file."""
        with self._lock:
            cur = self._read_file_secrets()
            for field, val in updates.items():
                if field not in SECRET_FIELD_ENV_MAP:
                    raise ValueError(f"不允许的凭据字段: {field}")
                if val is None or (isinstance(val, str) and not val.strip()):
                    cur.pop(field, None)
                    continue
                if not isinstance(val, str):
                    raise ValueError(f"凭据字段 {field} 必须为字符串")
                cleaned = val.strip()
                if len(cleaned) > 512:
                    raise ValueError(f"凭据字段 {field} 长度超限")
                # Basic structural validation before saving
                if field in ("bark_url", "feishu_webhook", "wecom_webhook") and not cleaned.startswith("https://"):
                    raise ValueError(f"字段 {field} 必须以 https:// 开头")
                if field in ("pushplus_token", "serverchan_key") and not _SAFE_TOKEN_RE.match(cleaned):
                    raise ValueError(f"字段 {field} 包含非法字符")
                cur[field] = cleaned
            if cur:
                _write_secrets_atomic_0600(self.secrets_path, cur)
            elif os.path.isfile(self.secrets_path):
                try:
                    os.unlink(self.secrets_path)
                except OSError:
                    pass
        return self.get_secrets_summary()

    def _rollover_day_locked(self, today_str: str) -> None:
        if self._day_key != today_str:
            self._day_key = today_str
            self._daily_count = 0

    def _default_post(self, url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Tuple[int, Any]:
        # Domestic direct-connect by default (avoid proxy hijacking domestic webhooks)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            try:
                data = resp.json() if resp.content else {}
            except Exception:
                data = {"raw": resp.text[:160]}
            return int(resp.status_code), data

    def send(
        self,
        title: str,
        body: str,
        *,
        category: str = "general",
        provider_override: Optional[str] = None,
        bypass_limits: bool = False,
        group: str = "Xiaozhi",
    ) -> Dict[str, Any]:
        """Send a sanitized mobile push notification with SSRF, quiet-hour, dedup & quota guards."""
        cfg = self.get_config()
        provider = str(provider_override or cfg.get("provider") or "bark").strip().lower()
        now_ts = float(self._now_fn())
        now_dt = self._now_dt_fn()
        today_str = now_dt.strftime("%Y-%m-%d")

        if not bypass_limits:
            if not cfg.get("enabled", False):
                return {"ok": False, "skipped": True, "reason": "disabled", "provider": provider}
            if cfg.get("respect_quiet_hours", True):
                pro_cfg = runtime_settings.get_proactive(self.settings_path)
                if _is_in_quiet_hours(now_dt, pro_cfg["quiet_start"], pro_cfg["quiet_end"]):
                    return {"ok": False, "skipped": True, "reason": "quiet_hours", "provider": provider}

        safe_title = sanitize_push_text(title, max_len=80) or "小智陪伴提醒"
        safe_body = sanitize_push_text(body, max_len=420)
        if not safe_body:
            return {"ok": False, "skipped": True, "reason": "empty_or_redacted", "provider": provider}

        content_hash = hashlib.sha256(f"{provider}:{safe_title}:{safe_body}".encode("utf-8")).hexdigest()[:24]

        with self._lock:
            self._rollover_day_locked(today_str)
            # Prune expired dedup keys
            cutoff = now_ts - DEDUP_WINDOW_SECONDS
            self._recent_hashes = {k: ts for k, ts in self._recent_hashes.items() if ts >= cutoff}

            if not bypass_limits:
                max_daily = int(cfg.get("max_daily_pushes", 12))
                if self._daily_count >= max_daily:
                    return {
                        "ok": False,
                        "skipped": True,
                        "reason": "daily_limit_reached",
                        "provider": provider,
                        "daily_count": self._daily_count,
                        "max_daily_pushes": max_daily,
                    }
                if content_hash in self._recent_hashes:
                    return {"ok": False, "skipped": True, "reason": "dedup_recent", "provider": provider}

        secrets = self.get_effective_secrets()
        try:
            url, payload, headers = build_provider_request(
                provider,
                safe_title,
                safe_body,
                secrets,
                group=group,
                now_ts=now_ts,
                resolver=self._resolver,
            )
        except Exception as exc:
            err = redact_push_error(exc)
            self._record_history(
                now_ts=now_ts,
                now_dt=now_dt,
                provider=provider,
                category=category,
                title=safe_title,
                preview=safe_body[:100],
                ok=False,
                error=err,
            )
            return {"ok": False, "skipped": False, "reason": "invalid_config", "error": err, "provider": provider}

        poster = self._http_poster or self._default_post
        try:
            status_code, resp_data = poster(url, payload, headers, HTTP_TIMEOUT_SECONDS)
            ok, detail = _check_provider_response(provider, status_code, resp_data)
        except Exception as exc:
            ok = False
            detail = redact_push_error(f"{type(exc).__name__}: {exc}")

        if ok:
            with self._lock:
                self._rollover_day_locked(today_str)
                if not bypass_limits:
                    self._daily_count += 1
                    self._recent_hashes[content_hash] = now_ts
        self._record_history(
            now_ts=now_ts,
            now_dt=now_dt,
            provider=provider,
            category=category,
            title=safe_title,
            preview=safe_body[:100],
            ok=ok,
            error="" if ok else redact_push_error(detail),
        )
        try:
            runtime_events.publish_event(
                "mobile_push",
                ok=ok,
                provider=provider,
                category=category,
                title=safe_title,
                preview=safe_body[:80],
                error="" if ok else redact_push_error(detail),
            )
        except Exception:
            pass
        return {
            "ok": ok,
            "skipped": False,
            "provider": provider,
            "category": category,
            "title": safe_title,
            "preview": safe_body[:120],
            "error": "" if ok else redact_push_error(detail),
        }

    def _record_history(
        self,
        *,
        now_ts: float,
        now_dt: datetime,
        provider: str,
        category: str,
        title: str,
        preview: str,
        ok: bool,
        error: str,
    ) -> None:
        with self._lock:
            self._history.appendleft(
                {
                    "ts": now_ts,
                    "time": now_dt.strftime("%H:%M:%S"),
                    "provider": provider,
                    "category": category,
                    "title": title,
                    "preview": preview,
                    "ok": bool(ok),
                    "error": error,
                }
            )

    def get_status(self) -> Dict[str, Any]:
        cfg = self.get_config()
        sec_summary = self.get_secrets_summary()
        prov = str(cfg.get("provider") or "bark")
        req_field = PROVIDER_REQUIRED_SECRET.get(prov, "bark_url")
        ready = bool(sec_summary.get(req_field, {}).get("configured", False))
        today_str = self._now_dt_fn().strftime("%Y-%m-%d")
        with self._lock:
            self._rollover_day_locked(today_str)
            daily_count = self._daily_count
            history = list(self._history)
        return {
            "ok": True,
            "config": cfg,
            "provider_label": PROVIDER_LABELS_ZH.get(prov, prov),
            "provider_ready": ready,
            "providers": [
                {
                    "id": pid,
                    "label": PROVIDER_LABELS_ZH[pid],
                    "configured": bool(sec_summary.get(PROVIDER_REQUIRED_SECRET[pid], {}).get("configured", False)),
                }
                for pid in VALID_PROVIDERS
            ],
            "secrets": sec_summary,
            "daily_count": daily_count,
            "max_daily_pushes": int(cfg.get("max_daily_pushes", 12)),
            "history": history,
        }


_bridge_lock = threading.Lock()
_default_bridge: Optional[MobilePushBridge] = None


def get_mobile_push_bridge() -> MobilePushBridge:
    global _default_bridge
    with _bridge_lock:
        if _default_bridge is None:
            _default_bridge = MobilePushBridge()
        return _default_bridge


def reset_default_bridge_for_tests(bridge: Optional[MobilePushBridge] = None) -> None:
    global _default_bridge
    with _bridge_lock:
        _default_bridge = bridge


def is_mobile_push_enabled(settings_path: Optional[str] = None) -> bool:
    if settings_path is not None:
        return bool(runtime_settings.get_mobile_push(settings_path).get("enabled", False))
    return get_mobile_push_bridge().is_enabled()


def send_mobile_notification(
    title: str,
    body: str,
    *,
    category: str = "general",
    provider_override: Optional[str] = None,
    bypass_limits: bool = False,
) -> Dict[str, Any]:
    return get_mobile_push_bridge().send(
        title,
        body,
        category=category,
        provider_override=provider_override,
        bypass_limits=bypass_limits,
    )

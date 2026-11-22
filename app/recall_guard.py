"""Recall-layer sensitive entity auto-filter & decoupled spoken style short rules.

Provides two lightweight, hot-toggleable mechanisms without touching persona prompt text:
1. Sensitive Entity Interception (default ON): strips API keys/passwords/tokens, mobile phones,
   bank card numbers, resident ID / passport numbers, and OTP verification codes from recalled
   memory context before LLM injection / voice playback.
2. Spoken Style Short Rules (default ON, decoupled from persona prompt):
   - Silent Memory Participation: use recalled knowledge naturally without robotic phrasing like
     "我记得你上次说过…".
   - Anti-AI-Flavor Colloquial Rules: avoid stiff bureaucratic transitions ("综上所述", "首先…其次…").
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, Optional, Tuple

import runtime_settings

_TAG_SECRET = "[已拦截隐私信息:密钥/口令]"
_TAG_ID_CARD = "[已拦截隐私信息:证件号]"
_TAG_BANK_CARD = "[已拦截隐私信息:银行卡]"
_TAG_PHONE = "[已拦截隐私信息:手机号]"
_TAG_OTP = "[已拦截隐私信息:验证码]"

_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b((?:authorization\s*[:=]\s*)?(?:Bearer|Basic|Token)\s+)[A-Za-z0-9._\-~+/=]{8,}"
)
_URL_CREDS_RE = re.compile(r"(?i)((?:https?|wss?|ftp)://)[^/\s:@\"']+:[^/\s@\"']+@")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api_key|apikey|access_token|refresh_token|auth_token|token|secret|key)=)[^\s&#\"',;)}\]]{6,}"
)
_KNOWN_API_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])(?:"
    r"sk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{12,}|"
    r"AIza[0-9A-Za-z_\-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9_]{16,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9\-]{12,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"AQ\.[A-Za-z0-9._\-]{14,}[A-Za-z0-9_\-]|"
    r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{10,}"
    r")(?![A-Za-z0-9_\-])"
)
_SECRET_KV_RE = re.compile(
    r"(?i)((?:api[\s_-]*key|apikey|(?:(?:access|refresh|auth|console|anima|mcp|api|session)[\s_-]*)?token|"
    r"secret[\s_-]*key|client[\s_-]*secret|password|passwd|pwd|"
    r"(?:登录|开机|支付|银行卡|取款|解锁|WiFi|wifi|账号|邮箱|手机|电脑)?密码|"
    r"(?:接口|API)?密钥|(?:访问|安全|会话)?令牌|私密口令|口令)\s*(?:[:=：]|是|为)\s*[\"'`]?)"
    r"([^\s，。！？；,;\"'<>）\)】\]]{4,64})"
)
_BENIGN_SECRET_VALUES = frozenset(
    {
        "无",
        "没有",
        "忘了",
        "忘记了",
        "不记得",
        "不知道",
        "保密",
        "未设置",
        "空",
        "none",
        "null",
        "n/a",
        "unknown",
        "***",
    }
)

_ID_CARD_18_RE = re.compile(
    r"(?<![0-9A-Za-z])([1-8]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9A-Za-z])"
)
_ID_CONTEXT_RE = re.compile(
    r"(?i)((?:身份证(?:号|号码)?|居民身份证|证件(?:号|号码)|护照(?:号|号码)?|港澳通行证|台胞证|社保号|驾驶证号)\s*(?:[:=：]|是|为)?\s*)"
    r"([A-Za-z0-9\-]{8,22})"
)
_BANK_CARD_CONTEXT_RE = re.compile(
    r"((?:银行卡(?:号)?|储蓄卡(?:号)?|信用卡(?:号)?|借记卡(?:号)?|工资卡(?:号)?|卡号)\s*(?:[:=：]|是|为)?\s*)"
    r"((?:\d[ -]?){13,19}\d)"
)
_BANK_CARD_STANDALONE_RE = re.compile(
    r"(?<![0-9A-Za-z])((?:(?:62|60|4\d|5[1-5]|9\d)(?:[ -]?\d){14,17})|(?:3[47](?:[ -]?\d){13}))(?![0-9A-Za-z])"
)
_PHONE_CN_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:\+?86[-\s]?)?(1[3-9]\d)[-\s]?(\d{4})[-\s]?(\d{4})(?![0-9A-Za-z])"
)
_OTP_AFTER_KW_RE = re.compile(
    r"(?i)((?:短信|手机|邮箱|登录|动态|安全|一次性|支付|注册)?(?:验证码|校验码|动态码|动态口令|口令码|OTP|verification\s*code|auth\s*code)"
    r"\s*(?:[:=：]|是|为|发来|收到|发的是)?\s*[【\[「『\"']?)"
    r"((?<![0-9A-Za-z])\d{4,8}(?![0-9A-Za-z])|(?<![0-9A-Za-z])(?=[A-Za-z0-9]{0,5}\d)(?=[A-Za-z0-9]{0,5}[A-Za-z])[A-Za-z0-9]{6}(?![0-9A-Za-z]))"
    r"([】\]」』\"']?)"
)
_OTP_BEFORE_KW_RE = re.compile(
    r"(?<![0-9A-Za-z])(\d{4,8})(?![0-9A-Za-z])(\s*(?:是(?:你的|我的|这次的|刚才的)?(?:短信|手机|登录)?(?:验证码|校验码|动态码)))"
)

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"

_stats_lock = threading.Lock()
_guard_stats: Dict[str, Any] = {
    "calls": 0,
    "redacted_calls": 0,
    "redacted_entities": 0,
    "categories": {
        "secret_key": 0,
        "id_card": 0,
        "bank_card": 0,
        "phone": 0,
        "otp_code": 0,
    },
}


def _valid_cn_id_checksum(code: str) -> bool:
    if len(code) != 18 or not code[:17].isdigit():
        return False
    total = sum(int(ch) * w for ch, w in zip(code[:17], _ID_WEIGHTS))
    return _ID_CHECK_CHARS[total % 11] == code[17].upper()


def _valid_luhn(digits: str) -> bool:
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    alt = False
    for ch in reversed(digits):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def redact_sensitive_recall_text(text: str) -> Tuple[str, Dict[str, Any]]:
    """Redact sensitive personal & credential entities from recalled memory text.

    Returns `(sanitized_text, meta)` where `meta` contains `redacted_count` and `categories`.
    """
    if not text or not isinstance(text, str):
        return text or "", {"redacted_count": 0, "categories": {}}

    counts: Dict[str, int] = {}

    def _bump(cat: str) -> None:
        counts[cat] = counts.get(cat, 0) + 1

    out = text

    def _sub_pem(m: re.Match) -> str:
        _bump("secret_key")
        return _TAG_SECRET

    out = _PEM_PRIVATE_KEY_RE.sub(_sub_pem, out)

    def _sub_auth(m: re.Match) -> str:
        if "[已拦截" in m.group(0):
            return m.group(0)
        _bump("secret_key")
        return f"{m.group(1)}{_TAG_SECRET}"

    out = _AUTH_HEADER_RE.sub(_sub_auth, out)

    def _sub_url_creds(m: re.Match) -> str:
        _bump("secret_key")
        return f"{m.group(1)}***:***@"

    out = _URL_CREDS_RE.sub(_sub_url_creds, out)

    def _sub_query_secret(m: re.Match) -> str:
        _bump("secret_key")
        return f"{m.group(1)}{_TAG_SECRET}"

    out = _QUERY_SECRET_RE.sub(_sub_query_secret, out)

    def _sub_known_key(m: re.Match) -> str:
        _bump("secret_key")
        return _TAG_SECRET

    out = _KNOWN_API_KEY_RE.sub(_sub_known_key, out)

    def _sub_secret_kv(m: re.Match) -> str:
        prefix, val = m.group(1), m.group(2)
        low = val.strip().lower()
        if "[已拦截" in val or low in _BENIGN_SECRET_VALUES or low.startswith("[redacted"):
            return m.group(0)
        # Skip plain Chinese prose words with no digits/ASCII token unless password-like >= 4 chars
        if not re.search(r"[0-9A-Za-z_\-!@#$%^&*]", val) and len(val) < 6:
            return m.group(0)
        _bump("secret_key")
        return f"{prefix}{_TAG_SECRET}"

    out = _SECRET_KV_RE.sub(_sub_secret_kv, out)

    def _sub_id_ctx(m: re.Match) -> str:
        prefix, val = m.group(1), m.group(2)
        if "[已拦截" in val or not re.search(r"\d{5,}", val):
            return m.group(0)
        _bump("id_card")
        return f"{prefix}{_TAG_ID_CARD}"

    out = _ID_CONTEXT_RE.sub(_sub_id_ctx, out)

    def _sub_id_18(m: re.Match) -> str:
        cand = m.group(1)
        if _valid_cn_id_checksum(cand):
            _bump("id_card")
            return _TAG_ID_CARD
        return m.group(0)

    out = _ID_CARD_18_RE.sub(_sub_id_18, out)

    def _sub_bank_ctx(m: re.Match) -> str:
        prefix, raw_num = m.group(1), m.group(2)
        digits = re.sub(r"\D", "", raw_num)
        if 13 <= len(digits) <= 19:
            _bump("bank_card")
            return f"{prefix}{_TAG_BANK_CARD}"
        return m.group(0)

    out = _BANK_CARD_CONTEXT_RE.sub(_sub_bank_ctx, out)

    def _sub_bank_standalone(m: re.Match) -> str:
        raw_num = m.group(1)
        digits = re.sub(r"\D", "", raw_num)
        if 15 <= len(digits) <= 19 and _valid_luhn(digits):
            _bump("bank_card")
            return _TAG_BANK_CARD
        return m.group(0)

    out = _BANK_CARD_STANDALONE_RE.sub(_sub_bank_standalone, out)

    def _sub_phone(m: re.Match) -> str:
        _bump("phone")
        return _TAG_PHONE

    out = _PHONE_CN_RE.sub(_sub_phone, out)

    def _sub_otp_after(m: re.Match) -> str:
        _bump("otp_code")
        return f"{m.group(1)}{_TAG_OTP}{m.group(3)}"

    out = _OTP_AFTER_KW_RE.sub(_sub_otp_after, out)

    def _sub_otp_before(m: re.Match) -> str:
        _bump("otp_code")
        return f"{_TAG_OTP}{m.group(2)}"

    out = _OTP_BEFORE_KW_RE.sub(_sub_otp_before, out)

    total = sum(counts.values())
    return out, {"redacted_count": total, "categories": counts}


def get_recall_style_config(settings_path: Optional[str] = None) -> Dict[str, Any]:
    """Return hot-reloaded recall privacy filter & spoken style toggles."""
    return dict(runtime_settings.get_recall_style(settings_path=settings_path))


def save_recall_style_config(
    *,
    sensitive_filter_enabled: Optional[bool] = None,
    silent_memory_enabled: Optional[bool] = None,
    anti_ai_flavor_enabled: Optional[bool] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    return dict(
        runtime_settings.set_recall_style(
            sensitive_filter_enabled=sensitive_filter_enabled,
            silent_memory_enabled=silent_memory_enabled,
            anti_ai_flavor_enabled=anti_ai_flavor_enabled,
            settings_path=settings_path,
        )
    )


def sanitize_recall_context(
    text: str,
    *,
    enabled: Optional[bool] = None,
    record_stats: bool = True,
) -> Tuple[str, Dict[str, Any]]:
    """Apply sensitive entity redaction to recall context according to hot config."""
    if not text or not isinstance(text, str):
        return text or "", {"redacted_count": 0, "categories": {}, "enabled": True}

    is_enabled = enabled
    if is_enabled is None:
        try:
            is_enabled = bool(get_recall_style_config().get("sensitive_filter_enabled", True))
        except Exception:
            is_enabled = True

    if not is_enabled:
        return text, {"redacted_count": 0, "categories": {}, "enabled": False}

    sanitized, meta = redact_sensitive_recall_text(text)
    meta["enabled"] = True
    if record_stats:
        with _stats_lock:
            _guard_stats["calls"] += 1
            if meta["redacted_count"] > 0:
                _guard_stats["redacted_calls"] += 1
                _guard_stats["redacted_entities"] += int(meta["redacted_count"])
                for cat, cnt in meta["categories"].items():
                    _guard_stats["categories"][cat] = _guard_stats["categories"].get(cat, 0) + int(cnt)
    return sanitized, meta


def get_recall_guard_stats() -> Dict[str, Any]:
    with _stats_lock:
        return {
            "calls": int(_guard_stats["calls"]),
            "redacted_calls": int(_guard_stats["redacted_calls"]),
            "redacted_entities": int(_guard_stats["redacted_entities"]),
            "categories": dict(_guard_stats["categories"]),
        }


def reset_recall_guard_stats() -> None:
    with _stats_lock:
        _guard_stats["calls"] = 0
        _guard_stats["redacted_calls"] = 0
        _guard_stats["redacted_entities"] = 0
        for k in list(_guard_stats["categories"].keys()):
            _guard_stats["categories"][k] = 0


def build_speech_style_directive(
    cfg: Optional[Dict[str, Any]] = None,
    *,
    has_memory: bool = True,
) -> str:
    """Build compact <speech_style_rules> block decoupled from persona prompt text."""
    eff = cfg if isinstance(cfg, dict) else get_recall_style_config()
    silent_mem = bool(eff.get("silent_memory_enabled", True))
    anti_ai = bool(eff.get("anti_ai_flavor_enabled", True))
    rules: list[str] = []
    if silent_mem and has_memory:
        rules.append(
            "- 记忆无声参与：把 <memory> 中的信息当成原本就自然知晓的背景融入聊天，"
            "切勿机械说“我记得你上次说过”“根据记录”“你之前提到过”这类播报式措辞；"
            "若记忆中有 [已拦截隐私信息:...] 占位符，切勿复述或追问隐私原值。"
        )
    if anti_ai:
        rules.append(
            "- 口语去AI味：用真实面对面聊天口吻直接开口，禁止“总而言之”“综上所述”“值得注意的是”"
            "“首先…其次…最后…”“作为一个AI”等书面公文或客服套话，不列枯燥编号清单。"
        )
    if not rules:
        return ""
    return "<speech_style_rules>\n" + "\n".join(rules) + "\n</speech_style_rules>"

"""Recall-layer sensitive entity sanitizer for Anima Brain (defense-in-depth).

Redacts API keys/passwords/tokens, mobile phones, bank cards, resident ID / passport numbers,
and OTP verification codes from recalled memory context and AutoDream evidence excerpts before
leaving Anima Brain or entering voice synthesis.
"""

from __future__ import annotations

import re
from typing import Any

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


def redact_sensitive_entities(text: str) -> tuple[str, dict[str, Any]]:
    """Redact sensitive personal & credential entities from text, returning `(sanitized, meta)`."""
    if not text or not isinstance(text, str):
        return text or "", {"redacted_count": 0, "categories": {}}

    counts: dict[str, int] = {}

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

    return out, {"redacted_count": sum(counts.values()), "categories": counts}

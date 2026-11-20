"""Runtime hot-configurable reply & storytelling character length controller.

Stored in data/reply_length_config.json with mtime caching so adjustments made from the
18080 Web Console take effect on the very next turn with ZERO container restart.
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading
from typing import Dict, Optional

import patch_registry
import runtime_settings
import xz_common

ROOT_DIR = xz_common.ROOT_DIR
DATA_DIR = xz_common.STATE_DIR
_DEFAULT_REPLY_LEN_PATH = os.path.join(xz_common.STATE_DIR, "reply_length_config.json")
REPLY_LEN_PATH = _DEFAULT_REPLY_LEN_PATH

PRESETS: Dict[str, Dict[str, int]] = {
    "concise": {"chat_chars": 60, "story_chars": 200},
    "balanced": {"chat_chars": 150, "story_chars": 450},
    "chatty": {"chat_chars": 260, "story_chars": 750},
    "storyteller": {"chat_chars": 200, "story_chars": 1000},
}

DEFAULT_CFG = {
    "preset": "balanced",
    "chat_chars": 150,
    "story_chars": 450,
}

_cfg_lock = threading.Lock()
_cfg_mtime = -1.0
_cfg_cache = dict(DEFAULT_CFG)
_cfg_file_cache: Optional[xz_common.MtimeJsonCache] = None
_cfg_file_cache_path = ""

_BLOCK_RE = re.compile(r"\n*<reply_length_control>.*?</reply_length_control>", re.DOTALL)


def _clamp_int(val, lo: int, hi: int, default: int) -> int:
    try:
        n = int(round(float(val)))
    except Exception:
        return default
    return max(lo, min(hi, n))


def _infer_preset(chat_chars: int, story_chars: int) -> str:
    for name, p in PRESETS.items():
        if p["chat_chars"] == chat_chars and p["story_chars"] == story_chars:
            return name
    return "custom"


def compute_dynamic_max_tokens(cfg: dict) -> int:
    peak_chars = max(int(cfg.get("chat_chars", 150)), int(cfg.get("story_chars", 450)))
    return max(1024, min(4096, int(peak_chars * 2.2) + 256))


def _normalize(data: dict) -> dict:
    preset_req = str(data.get("preset") or "").strip().lower()
    if preset_req in PRESETS and "chat_chars" not in data and "story_chars" not in data:
        chat_chars = PRESETS[preset_req]["chat_chars"]
        story_chars = PRESETS[preset_req]["story_chars"]
    else:
        chat_chars = _clamp_int(data.get("chat_chars", DEFAULT_CFG["chat_chars"]), 30, 600, DEFAULT_CFG["chat_chars"])
        story_chars = _clamp_int(data.get("story_chars", DEFAULT_CFG["story_chars"]), 100, 1500, DEFAULT_CFG["story_chars"])
    preset = _infer_preset(chat_chars, story_chars)
    out = {
        "preset": preset,
        "chat_chars": chat_chars,
        "story_chars": story_chars,
    }
    out["dynamic_max_tokens"] = compute_dynamic_max_tokens(out)
    return out


def _get_custom_cfg_cache() -> xz_common.MtimeJsonCache:
    global _cfg_file_cache
    if _cfg_file_cache is None:
        _cfg_file_cache = xz_common.MtimeJsonCache(
            normalizer=_normalize,
            default_factory=lambda: _normalize(_cfg_cache),
        )
    return _cfg_file_cache


def read_reply_length_config() -> dict:
    """Read reply_length settings from unified runtime_settings.json (or custom monkeypatched path via xz_common)."""
    global _cfg_mtime, _cfg_cache, _cfg_file_cache_path
    if REPLY_LEN_PATH == _DEFAULT_REPLY_LEN_PATH:
        return _normalize(runtime_settings.get_reply_length())
    with _cfg_lock:
        cache = _get_custom_cfg_cache()
        if _cfg_mtime < 0 or _cfg_file_cache_path != REPLY_LEN_PATH:
            cache.invalidate()
            _cfg_file_cache_path = REPLY_LEN_PATH
        res = cache.get(REPLY_LEN_PATH)
        _cfg_cache = dict(res)
        sig = cache._inner._sig
        _cfg_mtime = (sig[0] / 1_000_000_000.0) if sig else -1.0
        return dict(res)


def write_reply_length_config(
    chat_chars: Optional[int] = None,
    story_chars: Optional[int] = None,
    preset: Optional[str] = None,
) -> dict:
    global _cfg_mtime, _cfg_cache, _cfg_file_cache_path
    if REPLY_LEN_PATH == _DEFAULT_REPLY_LEN_PATH:
        saved = runtime_settings.set_reply_length(
            chat_chars=chat_chars,
            story_chars=story_chars,
            preset=preset,
        )
        return _normalize(saved)
    cur = read_reply_length_config()
    payload = {}
    p_key = str(preset or "").strip().lower()
    if p_key in PRESETS and chat_chars is None and story_chars is None:
        payload["preset"] = p_key
    else:
        payload["chat_chars"] = cur["chat_chars"] if chat_chars is None else chat_chars
        payload["story_chars"] = cur["story_chars"] if story_chars is None else story_chars
    norm = _normalize(payload)
    disk_data = {
        "preset": norm["preset"],
        "chat_chars": norm["chat_chars"],
        "story_chars": norm["story_chars"],
    }
    xz_common.atomic_write_json(REPLY_LEN_PATH, disk_data)
    with _cfg_lock:
        cache = _get_custom_cfg_cache()
        _cfg_file_cache_path = REPLY_LEN_PATH
        cache.set(REPLY_LEN_PATH, norm)
        _cfg_cache = dict(norm)
        sig = cache._inner._sig
        _cfg_mtime = (sig[0] / 1_000_000_000.0) if sig else -1.0
    return dict(norm)


def build_length_directive(cfg: Optional[dict] = None) -> str:
    c = cfg or read_reply_length_config()
    chat_n = int(c["chat_chars"])
    story_n = int(c["story_chars"])
    chat_lo = max(20, int(round(chat_n * 0.75 / 10.0) * 10))
    chat_hi = int(round(chat_n * 1.25 / 10.0) * 10)
    story_lo = max(80, int(round(story_n * 0.8 / 10.0) * 10))
    story_hi = int(round(story_n * 1.2 / 10.0) * 10)
    return (
        "<reply_length_control>\n"
        "实时字数与篇幅控制（由用户在控制台实时微调，优先级高于前文默认句数描述）：\n"
        f"- 日常聊天、问答、关心与互动：自然展开到约 {chat_n} 字（大致 {chat_lo}~{chat_hi} 字），保持口语顺畅有温度，不要用极短一两句敷衍，也不要生硬凑字。\n"
        f"- 讲故事、睡前故事、科普讲解或对方要求多讲一点时：充分展开讲足约 {story_n} 字（大致 {story_lo}~{story_hi} 字），写好细节、场景与情绪起伏，不要刚开个头就匆忙收尾。\n"
        "- 若对方在当前这句里临时口头指定了字数（如“简短说”或“讲个八百字的故事”），优先听对方当次口头要求。\n"
        "</reply_length_control>"
    )


def inject_reply_length_into_messages(msgs) -> list:
    if not msgs or not isinstance(msgs, list):
        return msgs
    if isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
        base_content = str(msgs[0].get("content") or "")
        cleaned = _BLOCK_RE.sub("", base_content).rstrip()
        msgs[0]["content"] = f"{cleaned}\n\n{build_length_directive()}"
    return msgs


def apply_reply_length_llm_patch() -> None:
    """Dynamically expand OpenAI-compatible LLM max_tokens when user raises story/chat length."""
    from core.providers.llm.openai.openai import LLMProvider

    if getattr(LLMProvider, "_reply_len_patched", False):
        if patch_registry.get_patch_status("reply_length_llm") is None:
            patch_registry.record_patch("reply_length_llm", "ok", target="LLMProvider", detail="already applied")
        return

    _orig_response = LLMProvider.response
    _orig_response_with_functions = LLMProvider.response_with_functions

    ok1, d1 = patch_registry.verify_signature_contract(
        _orig_response, required_params=("self", "session_id", "dialogue"), require_async=False
    )
    ok2, d2 = patch_registry.verify_signature_contract(
        _orig_response_with_functions, required_params=("self", "session_id", "dialogue"), require_async=False
    )
    if not (ok1 and ok2):
        patch_registry.record_patch(
            "reply_length_llm",
            "skipped_contract",
            target="OpenAI.LLMProvider.response/response_with_functions",
            detail=f"{d1}; {d2}",
            contract_ok=False,
            category="experience",
        )
        print(f"[ReplyLengthRouter] 跳过挂载回复字数补丁（签名契约不匹配）: {d1}; {d2}", flush=True)
        return

    def _sync_max_tokens(self):
        try:
            if not hasattr(self, "_reply_len_base_max_tokens"):
                raw_base = getattr(self, "max_tokens", None)
                try:
                    self._reply_len_base_max_tokens = int(raw_base) if raw_base is not None else None
                except (TypeError, ValueError):
                    self._reply_len_base_max_tokens = None
            dyn = int(read_reply_length_config()["dynamic_max_tokens"])
            base = self._reply_len_base_max_tokens
            # Sync both upward AND downward with dynamic_max_tokens (P2-23);
            # preserve explicit >4096 YAML override if present.
            self.max_tokens = dyn if (base is None or base <= 4096) else max(base, dyn)
        except Exception:
            pass

    @functools.wraps(_orig_response)
    def _patched_response(self, *args, **kwargs):
        _sync_max_tokens(self)
        return _orig_response(self, *args, **kwargs)

    @functools.wraps(_orig_response_with_functions)
    def _patched_response_with_functions(self, *args, **kwargs):
        _sync_max_tokens(self)
        return _orig_response_with_functions(self, *args, **kwargs)

    LLMProvider.response = _patched_response
    LLMProvider.response_with_functions = _patched_response_with_functions
    LLMProvider._reply_len_patched = True
    patch_registry.record_patch(
        "reply_length_llm",
        "ok",
        target="OpenAI.LLMProvider.response/response_with_functions",
        detail=f"{d1}; {d2}",
        contract_ok=True,
        category="experience",
    )
    print("[ReplyLengthRouter] 已挂载回复字数与讲故事篇幅实时热调补丁 (免重启生效)", flush=True)

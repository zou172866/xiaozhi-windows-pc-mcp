"""Runtime hot-configurable reply & storytelling character length controller.

Stored in data/reply_length_config.json with mtime caching so adjustments made from the
18080 Web Console take effect on the very next turn with ZERO container restart.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Dict, Optional

ROOT_DIR = "/opt/xiaozhi-esp32-server"
DATA_DIR = os.path.join(ROOT_DIR, "data")
REPLY_LEN_PATH = os.path.join(DATA_DIR, "reply_length_config.json")

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


def read_reply_length_config() -> dict:
    """Read reply_length_config.json with mtime cache (sub-microsecond hot path)."""
    global _cfg_mtime, _cfg_cache
    try:
        if not os.path.exists(REPLY_LEN_PATH):
            return _normalize(_cfg_cache)
        st = os.stat(REPLY_LEN_PATH)
        with _cfg_lock:
            if st.st_mtime != _cfg_mtime:
                with open(REPLY_LEN_PATH, "r", encoding="utf-8") as f:
                    raw = json.load(f) or {}
                _cfg_cache = _normalize(raw)
                _cfg_mtime = st.st_mtime
            return dict(_cfg_cache)
    except Exception:
        return _normalize(_cfg_cache)


def write_reply_length_config(
    chat_chars: Optional[int] = None,
    story_chars: Optional[int] = None,
    preset: Optional[str] = None,
) -> dict:
    global _cfg_mtime, _cfg_cache
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
    os.makedirs(os.path.dirname(REPLY_LEN_PATH), exist_ok=True)
    tmp_path = f"{REPLY_LEN_PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(disk_data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, REPLY_LEN_PATH)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise
    with _cfg_lock:
        _cfg_cache = dict(norm)
        try:
            _cfg_mtime = os.stat(REPLY_LEN_PATH).st_mtime
        except Exception:
            _cfg_mtime = -1.0
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
        return

    _orig_response = LLMProvider.response
    _orig_response_with_functions = LLMProvider.response_with_functions

    def _sync_max_tokens(self):
        try:
            dyn = read_reply_length_config()["dynamic_max_tokens"]
            cur = getattr(self, "max_tokens", None)
            if cur is None or int(cur) < dyn:
                self.max_tokens = dyn
        except Exception:
            pass

    def _patched_response(self, session_id, dialogue, **kwargs):
        _sync_max_tokens(self)
        return _orig_response(self, session_id, dialogue, **kwargs)

    def _patched_response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        _sync_max_tokens(self)
        return _orig_response_with_functions(self, session_id, dialogue, functions=functions, **kwargs)

    LLMProvider.response = _patched_response
    LLMProvider.response_with_functions = _patched_response_with_functions
    LLMProvider._reply_len_patched = True
    print("[ReplyLengthRouter] 已挂载回复字数与讲故事篇幅实时热调补丁 (免重启生效)", flush=True)

"""Raiden Mei emotion catalog, tag parsing/stripping, text cleaning, and Anima mood cache (H-1)."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import patch_registry
import runtime_events
import runtime_settings
import xz_common

_LEGACY_HOST_DATA_PREFIX = "e:/故事/1/xiaozhi-server/data"
_LEGACY_HOST_VOICES_PREFIX = "e:/故事/1/xiaozhi-server/voices"
CONTAINER_DATA_PREFIX = xz_common.DATA_DIR
CONTAINER_VOICES_PREFIX = xz_common.VOICES_DIR
_HOST_DIRS = xz_common.resolve_host_dirs()
HOST_DATA_PREFIX = _HOST_DIRS["HOST_DATA_DIR"]
_HOST_PROJECT_PREFIX = _HOST_DIRS["HOST_PROJECT_DIR"]
HOST_VOICES_PREFIX = _HOST_DIRS["HOST_VOICES_DIR"]
MEI_DIR_HOST = f"{HOST_VOICES_PREFIX}/raiden_mei"

DEFAULT_MEI_EMOTIONS: Dict[str, Dict[str, Any]] = {
    "calm": {
        "label_zh": "平静默认",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_default_ref.wav",
        "prompt_text": "今夜月色很美，呵...适合与朋友小聚一番。",
        "prompt_lang": "zh",
        "speed_factor": 1.00,
        "temperature": 0.98,
        "top_k": 12,
        "top_p": 1.00,
        "esp32_emotion": "relaxed",
        "esp32_emoji": "😌",
    },
    "gentle": {
        "label_zh": "温柔关怀",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_gentle_ref.wav",
        "prompt_text": "你今天已经很努力了，接下来的事就交给我吧。",
        "prompt_lang": "zh",
        "speed_factor": 0.96,
        "temperature": 0.95,
        "top_k": 12,
        "top_p": 0.98,
        "esp32_emotion": "loving",
        "esp32_emoji": "😍",
    },
    "happy": {
        "label_zh": "欣喜明朗",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_happy_ref.wav",
        "prompt_text": "今天天气挺不错的，要和我们一起出去野餐吗？",
        "prompt_lang": "zh",
        "speed_factor": 1.04,
        "temperature": 1.04,
        "top_k": 15,
        "top_p": 1.00,
        "esp32_emotion": "happy",
        "esp32_emoji": "😆",
    },
    "playful": {
        "label_zh": "俏皮打趣",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_playful_ref.wav",
        "prompt_text": "如果你能按时起床，我倒不介意多做一份早餐哦。",
        "prompt_lang": "zh",
        "speed_factor": 1.03,
        "temperature": 1.05,
        "top_k": 15,
        "top_p": 1.00,
        "esp32_emotion": "winking",
        "esp32_emoji": "😉",
    },
    "shy": {
        "label_zh": "羞赧微嗔",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_shy_ref.wav",
        "prompt_text": "面具？虽然有些不好意思，但心理上它的确是我过去的一部分。",
        "prompt_lang": "zh",
        "speed_factor": 0.97,
        "temperature": 1.00,
        "top_k": 14,
        "top_p": 1.00,
        "esp32_emotion": "embarrassed",
        "esp32_emoji": "😳",
    },
    "serious": {
        "label_zh": "沉稳认真",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_serious_ref.wav",
        "prompt_text": "以律者的身份守护人类，这就是雷电芽衣的选择。",
        "prompt_lang": "zh",
        "speed_factor": 1.02,
        "temperature": 0.90,
        "top_k": 10,
        "top_p": 0.95,
        "esp32_emotion": "confident",
        "esp32_emoji": "😏",
    },
    "tender": {
        "label_zh": "轻柔抚慰",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_tender_ref.wav",
        "prompt_text": "那些美好温暖的回忆，会永远留在我的心中。",
        "prompt_lang": "zh",
        "speed_factor": 0.93,
        "temperature": 0.92,
        "top_k": 12,
        "top_p": 0.98,
        "esp32_emotion": "sad",
        "esp32_emoji": "😔",
    },
}

EMOTION_ALIASES: Dict[str, str] = {
    # calm
    "calm": "calm",
    "default": "calm",
    "neutral": "calm",
    "relaxed": "calm",
    "normal": "calm",
    "peaceful": "calm",
    "平静": "calm",
    "默认": "calm",
    "自然": "calm",
    "舒缓": "calm",
    "淡定": "calm",
    "平和": "calm",
    # gentle
    "gentle": "gentle",
    "warm": "gentle",
    "caring": "gentle",
    "soft": "gentle",
    "loving": "gentle",
    "affectionate": "gentle",
    "kind": "gentle",
    "sleepy": "gentle",
    "温柔": "gentle",
    "体贴": "gentle",
    "关怀": "gentle",
    "暖心": "gentle",
    "柔和": "gentle",
    "宠溺": "gentle",
    "安心": "gentle",
    "柔声": "gentle",
    # happy
    "happy": "happy",
    "joy": "happy",
    "joyful": "happy",
    "excited": "happy",
    "cheerful": "happy",
    "smile": "happy",
    "smiling": "happy",
    "laughing": "happy",
    "curious": "happy",
    "delighted": "happy",
    "surprised": "happy",
    "开心": "happy",
    "高兴": "happy",
    "欣喜": "happy",
    "喜悦": "happy",
    "愉快": "happy",
    "明朗": "happy",
    "兴奋": "happy",
    "期待": "happy",
    "微笑": "happy",
    "惊喜": "happy",
    # playful
    "playful": "playful",
    "teasing": "playful",
    "winking": "playful",
    "funny": "playful",
    "amused": "playful",
    "mischievous": "playful",
    "silly": "playful",
    "witty": "playful",
    "俏皮": "playful",
    "打趣": "playful",
    "调侃": "playful",
    "轻笑": "playful",
    "狡黠": "playful",
    "调皮": "playful",
    "戏谑": "playful",
    "坏笑": "playful",
    # shy
    "shy": "shy",
    "flustered": "shy",
    "bashful": "shy",
    "embarrassed": "shy",
    "blushing": "shy",
    "apologetic": "shy",
    "sorry": "shy",
    "害羞": "shy",
    "羞涩": "shy",
    "不好意思": "shy",
    "脸红": "shy",
    "羞赧": "shy",
    "微嗔": "shy",
    "嗔怪": "shy",
    "抱歉": "shy",
    # serious
    "serious": "serious",
    "composed": "serious",
    "firm": "serious",
    "resolute": "serious",
    "focused": "serious",
    "confident": "serious",
    "cool": "serious",
    "guarded": "serious",
    "alert": "serious",
    "solemn": "serious",
    "stern": "serious",
    "angry": "serious",
    "annoyed": "serious",
    "anxious": "serious",
    "thinking": "serious",
    "认真": "serious",
    "严肃": "serious",
    "沉稳": "serious",
    "坚定": "serious",
    "果断": "serious",
    "专注": "serious",
    "警惕": "serious",
    "郑重": "serious",
    "冷静": "serious",
    # tender
    "tender": "tender",
    "sad": "tender",
    "comfort": "tender",
    "comforting": "tender",
    "empathetic": "tender",
    "worried": "tender",
    "hurt": "tender",
    "crying": "tender",
    "soothing": "tender",
    "melancholic": "tender",
    "心疼": "tender",
    "安慰": "tender",
    "抚慰": "tender",
    "低落": "tender",
    "难过": "tender",
    "怜惜": "tender",
    "担忧": "tender",
    "伤感": "tender",
    "叹气": "tender",
}

_EXPLICIT_PREFIXES = ("emotion", "mood", "tone", "情绪", "语气", "情感")
_BRACKET_CANDIDATE_RE = re.compile(
    r"(?:\[\s*([^\[\]\n]{1,28}?)\s*\]"
    r"|【\s*([^【】\n]{1,28}?)\s*】"
    r"|<\s*([^<>\n]{1,28}?)\s*>"
    r"|\(\s*([^\(\)\n]{1,18}?)\s*\)"
    r"|（\s*([^（）\n]{1,18}?)\s*）)"
)

_UNCLOSED_OPENERS = {
    "[": "]",
    "【": "】",
    "<": ">",
    "(": ")",
    "（": "）",
}

_SPOKEN_CHAR_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_LEAKED_XML_TOOL_TAG_RE = re.compile(
    r"(?:"
    r"</?\s*[|｜]{1,2}\s*DSML\s*[|｜]{1,2}[^>]*>"
    r"|<\s*[|｜]{1,2}\s*tool[\s_\u2581][a-z_\s\u2581]{2,32}\s*[|｜]{1,2}\s*>"
    r"|</?\s*(?:parameter|invoke|calls|tool_call|function_calls?|antml:[a-z_]+|minimax:[a-z_]+)\b[^>]*>"
    r")",
    flags=re.IGNORECASE,
)

_LEADING_JUNK_CHARS = set(" \t\r\n，,。.！!？?；;：:、-－")
_EMOJI_RANGES = (
    (0x1F600, 0x1F64F),
    (0x1F300, 0x1F5FF),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
)

_EMOJI_TO_MEI_EMOTION: Dict[str, str] = {
    "😌": "calm",
    "😶": "calm",
    "😍": "gentle",
    "😘": "gentle",
    "😴": "gentle",
    "🙂": "happy",
    "😆": "happy",
    "😲": "happy",
    "😉": "playful",
    "😂": "playful",
    "😜": "playful",
    "🤤": "playful",
    "😳": "shy",
    "😏": "serious",
    "😎": "serious",
    "🤔": "serious",
    "😠": "serious",
    "🙄": "serious",
    "😔": "tender",
    "😭": "tender",
    "😱": "tender",
}

_UNCLOSED_CANONICAL_TAG_RE = re.compile(
    r"[\[<【]\s*(?:(?:emotion|mood|tone|情绪|语气|情感)\s*[:：=]\s*)?"
    r"(calm|gentle|happy|playful|shy|serious|tender)"
    r"(?![^\[\]>】\n]{0,20}[\]>】])"
    r"(?=\s*[\u4e00-\u9fff，,。！!？?～~…\s]|$)",
    flags=re.IGNORECASE,
)

# Lexical cues for Layer-2 clause emotion inference when no explicit tag is present
_LEXICAL_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "shy",
        (
            "不好意思",
            "有些在意",
            "别这么夸",
            "突然这么夸",
            "害羞",
            "脸红",
            "讨厌啦",
            "别取笑",
            "怪难为情",
        ),
    ),
    (
        "tender",
        (
            "别难过",
            "不要难过",
            "心疼",
            "抱抱",
            "受委屈",
            "委屈你",
            "一直陪着你",
            "陪在你身边",
            "没关系的",
            "别太自责",
            "不哭",
            "迷茫时",
        ),
    ),
    (
        "playful",
        (
            "呵呵",
            "呵...",
            "呵…",
            "被我抓到",
            "猜你现在",
            "偷偷熬夜",
            "偷懒哟",
            "不介意多做",
            "难道你也是",
            "真是拿你没办法",
            "调皮",
            "耍赖",
        ),
    ),
    (
        "happy",
        (
            "太棒了",
            "太棒啦",
            "太好了",
            "太好啦",
            "真开心",
            "好开心",
            "很高兴",
            "恭喜",
            "庆祝一下",
            "真不错",
            "好期待",
            "出去野餐",
            "大功告成",
            "顺利搞定",
        ),
    ),
    (
        "gentle",
        (
            "辛苦了",
            "辛苦啦",
            "累不累",
            "早点睡",
            "早点休息",
            "注意休息",
            "喝口温水",
            "喝点热茶",
            "准备好了热茶",
            "添衣服",
            "别着凉",
            "别勉强",
            "身体会吃不消",
            "交给我吧",
            "慢点吃",
            "欢迎回来",
        ),
    ),
    (
        "serious",
        (
            "不能马虎",
            "不要放松警惕",
            "注意安全",
            "危险",
            "立即停止",
            "请确认",
            "检查完了",
            "派单给反重力",
            "锁在安全挡",
            "权限不足",
            "绝不能",
            "务必小心",
        ),
    ),
)

_DEFAULT_MOOD_DEVICE_KEY = "__default__"
_anima_lock = threading.Lock()
_last_anima_emotion_by_device: Dict[str, str] = {}
_last_anima_emotion: str = "calm"
_cached_manifest: Optional[Dict[str, Dict[str, Any]]] = None


def _rewrite_host_data_path(raw_path: str) -> str:
    """Rewrite legacy data/voices or ${HOST_DATA_DIR} / ${HOST_VOICES_DIR} paths to active host paths (A-1)."""
    p = (raw_path or "").replace("\\", "/").strip()
    if not p:
        return p
    if "${HOST_VOICES_DIR}" in p:
        p = p.replace("${HOST_VOICES_DIR}", HOST_VOICES_PREFIX)
    if "${HOST_DATA_DIR}" in p:
        p = p.replace("${HOST_DATA_DIR}", HOST_DATA_PREFIX)

    p_low = p.lower()
    # Map legacy .../data/voices/<rest> -> HOST_VOICES_PREFIX/<rest>
    for pfx in (
        f"{HOST_DATA_PREFIX}/voices/".lower(),
        f"{_LEGACY_HOST_DATA_PREFIX}/voices/".lower(),
        f"{_LEGACY_HOST_VOICES_PREFIX}/".lower(),
        "data/voices/",
        "voices/",
    ):
        if p_low.startswith(pfx):
            return f"{HOST_VOICES_PREFIX}/{p[len(pfx):]}"

    # Map legacy taiwan ref audio under data/ -> HOST_VOICES_PREFIX/
    for fname in ("xiaozhi_taiwan_ref.wav", "xiaozhi_taiwan_aux.wav", "wakeup_words_short.wav"):
        for pfx in (f"{HOST_DATA_PREFIX}/", f"{_LEGACY_HOST_DATA_PREFIX}/", "data/"):
            if p_low == f"{pfx.lower()}{fname}":
                return f"{HOST_VOICES_PREFIX}/{fname}"

    if p_low.startswith(_LEGACY_HOST_DATA_PREFIX.lower()):
        p = HOST_DATA_PREFIX + p[len(_LEGACY_HOST_DATA_PREFIX) :]
    elif p.startswith("data/"):
        p = HOST_DATA_PREFIX + "/" + p[len("data/") :]
    return p


def _to_local_fs_path(host_or_local_path: str) -> str:
    p = _rewrite_host_data_path(host_or_local_path)
    if os.name != "nt":
        p_low = p.lower()
        for v_pfx in (
            HOST_VOICES_PREFIX,
            f"{HOST_DATA_PREFIX}/voices",
            f"{_LEGACY_HOST_DATA_PREFIX}/voices",
            _LEGACY_HOST_VOICES_PREFIX,
        ):
            if v_pfx and p_low.startswith(v_pfx.lower()):
                return CONTAINER_VOICES_PREFIX + p[len(v_pfx) :]
        for d_pfx in (HOST_DATA_PREFIX, _LEGACY_HOST_DATA_PREFIX):
            if d_pfx and p_low.startswith(d_pfx.lower()):
                return CONTAINER_DATA_PREFIX + p[len(d_pfx) :]
    return p


def ref_audio_exists(host_ref_path: str) -> bool:
    local_p = _to_local_fs_path(host_ref_path)
    return bool(local_p and os.path.isfile(local_p))


def load_mei_emotions(force_reload: bool = False) -> Dict[str, Dict[str, Any]]:
    global _cached_manifest
    if _cached_manifest is not None and not force_reload:
        return _cached_manifest

    merged: Dict[str, Dict[str, Any]] = {
        k: dict(v) for k, v in DEFAULT_MEI_EMOTIONS.items()
    }
    manifest_candidates = [
        _to_local_fs_path(f"{MEI_DIR_HOST}/mei_emotions.json"),
        os.path.join(CONTAINER_VOICES_PREFIX, "raiden_mei", "mei_emotions.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices", "raiden_mei", "mei_emotions.json"),
    ]
    for mp in manifest_candidates:
        if os.path.isfile(mp):
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                emo_map = data.get("emotions") if isinstance(data, dict) else None
                if isinstance(emo_map, dict):
                    for k, val in emo_map.items():
                        if isinstance(val, dict) and k in merged:
                            merged[k].update(val)
                        elif isinstance(val, dict):
                            merged[k] = dict(val)
                break
            except Exception:
                pass

    for prof in merged.values():
        if isinstance(prof, dict) and prof.get("ref_audio_path"):
            prof["ref_audio_path"] = _rewrite_host_data_path(str(prof["ref_audio_path"]))

    _cached_manifest = merged
    return merged


def normalize_emotion_name(raw_token: str) -> Tuple[Optional[str], bool]:
    """Parse inner bracket content.

    Returns:
        (canonical_emotion_or_None, should_strip_tag)
    """
    if not raw_token:
        return None, False
    t = raw_token.strip()
    explicit_prefixed = False
    for sep in (":", "：", "="):
        if sep in t:
            left, right = t.split(sep, 1)
            if left.strip().lower() in _EXPLICIT_PREFIXES:
                explicit_prefixed = True
                t = right.strip()
                break

    key = t.strip().lower()
    if key in EMOTION_ALIASES:
        return EMOTION_ALIASES[key], True
    if explicit_prefixed:
        # Unknown value inside an explicit [emotion: xxx] wrapper: still strip it so TTS never reads it!
        return None, True
    return None, False


def extract_and_strip_emotion(
    text: str, flush_incomplete: bool = False
) -> Tuple[Optional[str], str]:
    """Extract the last/active hidden emotion tag from `text` and strip all emotion tags."""
    if not text:
        return None, ""

    detected: Optional[str] = None

    def _repl(match: re.Match) -> str:
        nonlocal detected
        inner = next((g for g in match.groups() if g is not None), "")
        is_paren = match.group(4) is not None or match.group(5) is not None
        emo, should_strip = normalize_emotion_name(inner)
        if not should_strip:
            return match.group(0)
        # For parentheses like (温柔) or (happy), only strip when it's a pure emotion/stage cue
        if is_paren and emo is None:
            return match.group(0)
        if emo is not None:
            detected = emo
        return ""

    cleaned = _BRACKET_CANDIDATE_RE.sub(_repl, text)

    # Defense-in-depth: strip malformed unclosed canonical English tags (e.g. "[gentle 哥哥")
    def _unclosed_repl(match: re.Match) -> str:
        nonlocal detected
        emo_cand = match.group(1).strip().lower()
        if emo_cand in EMOTION_ALIASES:
            detected = EMOTION_ALIASES[emo_cand]
        return ""

    if flush_incomplete or any(op in cleaned for op in ("[", "<", "【")):
        # Only strip unclosed canonical tags that already have trailing text OR when flushing
        if flush_incomplete:
            cleaned = _UNCLOSED_CANONICAL_TAG_RE.sub(_unclosed_repl, cleaned)
        else:
            # During streaming, only strip an unclosed canonical tag if followed by non-ASCII/punct text
            cleaned = re.sub(
                r"[\[<【]\s*(?:(?:emotion|mood|tone|情绪|语气|情感)\s*[:：=]\s*)?"
                r"(calm|gentle|happy|playful|shy|serious|tender)"
                r"(?![^\[\]>】\n]{0,20}[\]>】])"
                r"(?=\s*[\u4e00-\u9fff，,。！!？?])",
                _unclosed_repl,
                cleaned,
                flags=re.IGNORECASE,
            )

    if flush_incomplete:
        # Guard against stream cutoff mid-tag at the very start like "[gentle" or "[温"
        m_inc = re.match(
            r"^\s*[\[<【（(]\s*(?:(?:emotion|mood|tone|情绪|语气|情感)\s*[:：=]\s*)?([A-Za-z\u4e00-\u9fff_]{1,16})\s*$",
            cleaned,
            flags=re.IGNORECASE,
        )
        if m_inc:
            token = m_inc.group(1).strip().lower()
            emo, should_strip = normalize_emotion_name(token)
            if should_strip or any(a.startswith(token) for a in EMOTION_ALIASES):
                if emo:
                    detected = emo
                cleaned = ""

    # Fallback: if no bracket tag was present, map a leading emotion emoji before TTS strips emojis
    if detected is None and cleaned:
        idx = 0
        while idx < len(cleaned) and (cleaned[idx].isspace() or _is_emoji_char(cleaned[idx])):
            ch = cleaned[idx]
            if ch in _EMOJI_TO_MEI_EMOTION:
                detected = _EMOJI_TO_MEI_EMOTION[ch]
                break
            idx += 1

    return detected, cleaned


def strip_emotion_tags(text: str) -> str:
    _, cleaned = extract_and_strip_emotion(text, flush_incomplete=False)
    cleaned_no_xml = _LEAKED_XML_TOOL_TAG_RE.sub("", cleaned)
    if cleaned_no_xml != cleaned:
        cleaned = cleaned_no_xml.rstrip()
    else:
        cleaned = cleaned_no_xml
    return cleaned.lstrip() if text and not text[0].isspace() else cleaned


def _strip_leading_ws_and_emojis(text: str) -> str:
    idx = 0
    while idx < len(text) and (text[idx].isspace() or _is_emoji_char(text[idx])):
        idx += 1
    return text[idx:]


def has_unclosed_leading_tag(text: str) -> bool:
    """Return True if streaming buffer begins with an unclosed potential emotion tag."""
    if not text:
        return False
    s = _strip_leading_ws_and_emojis(text)
    if not s:
        return False
    opener = s[0]
    closer = _UNCLOSED_OPENERS.get(opener)
    if not closer:
        return False
    if closer in s[1:]:
        return False
    if len(s) > 24:
        return False
    if any(p in s for p in ("。", "！", "!", "？", "?", "\n")):
        return False
    inner = s[1:].strip().lower()
    if not inner:
        return True
    # Wait if inner could still become [emotion:...], [gentle], [温柔], etc.
    for pfx in _EXPLICIT_PREFIXES:
        if pfx.startswith(inner) or inner.startswith(pfx):
            return True
    for alias in EMOTION_ALIASES:
        if alias.startswith(inner):
            return True
    return False


def count_spoken_chars(text: str) -> int:
    _, clean = extract_and_strip_emotion(text, flush_incomplete=False)
    return len(_SPOKEN_CHAR_RE.findall(clean))


def _is_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def clean_segment_for_tts(text: str) -> str:
    """Strip hidden emotion tags, leaked XML tool tags, emojis, and leading orphan punctuation while keeping
    expressive internal and ending punctuation (，。！？～…) intact for GPT-SoVITS."""
    _, s = extract_and_strip_emotion(text, flush_incomplete=True)
    s = _LEAKED_XML_TOOL_TAG_RE.sub("", s)
    s = "".join(ch for ch in s if not _is_emoji_char(ch) and ch != "\r")
    s = re.sub(r"\s*\n+\s*", " ", s).strip()
    start = 0
    while start < len(s) and (s[start] in _LEADING_JUNK_CHARS or _is_emoji_char(s[start])):
        start += 1
    s = s[start:].strip()
    if not _SPOKEN_CHAR_RE.search(s):
        return ""
    return s


def safe_clean_response_garbage(text: str, *args, **kwargs) -> str:
    """Lossless replacement for upstream `ConnectionHandler._clean_response_garbage`.

    Strips genuine leaked JSON closure suffixes (e.g. `"}`, `"}}`, `\\n"}}`) without
    ever eating `]` on emotion tags (`[gentle]`) or `[安全挡]`, and without stripping
    trailing spaces off interior streaming chunks.
    """
    if not text:
        return text
    _line_garbage_chars = frozenset('"\'}）')
    lines = text.split("\n")
    cleaned_lines = []
    removed_line = False
    for line in lines:
        st = line.strip()
        if (
            st
            and len(st) <= 8
            and ("}" in st or '"' in st)
            and all(c in _line_garbage_chars for c in st)
        ):
            removed_line = True
            continue
        cleaned_lines.append(line)
    result = "\n".join(cleaned_lines)
    result_no_xml = _LEAKED_XML_TOOL_TAG_RE.sub("", result)
    xml_removed = result_no_xml != result
    result = result_no_xml
    m_tail = re.search(r'(?:["\']\s*\}+|\}{2,})\s*$', result)
    if m_tail:
        result = result[: m_tail.start()].rstrip()
    elif removed_line or xml_removed:
        result = result.rstrip()
    return result


def infer_emotion_from_text(text: str, allow_soft_wave_cue: bool = True) -> Optional[str]:
    """Infer emotion from Chinese lexical & prosodic cues when no explicit tag is present."""
    clean = strip_emotion_tags(text or "").strip()
    if not clean:
        return None
    for emo, keywords in _LEXICAL_RULES:
        for kw in keywords:
            if kw in clean:
                return emo
    if ("！" in clean or "!" in clean) and any(w in clean for w in ("棒", "好", "赞", "成", "耶", "啦")):
        return "happy"
    if allow_soft_wave_cue and ("～" in clean or "~" in clean):
        return "playful"
    return None


def _normalize_mood_device_key(device_id: Optional[str] = None) -> str:
    dev = str(device_id or "").strip().lower()
    return dev if dev else _DEFAULT_MOOD_DEVICE_KEY


def set_anima_mood(mood_summary: str, device_id: Optional[str] = None) -> None:
    """Update cached Anima Brain emotion from `/xiaozhi/recall` mood summary string, isolated per device_id (C-6)."""
    global _last_anima_emotion
    if not mood_summary:
        return
    m = re.search(r"last_emotion\s*=\s*([A-Za-z_]+)", mood_summary)
    raw = m.group(1).strip().lower() if m else mood_summary.strip().lower()
    emo, _ = normalize_emotion_name(raw)
    if emo:
        dev_key = _normalize_mood_device_key(device_id)
        with _anima_lock:
            _last_anima_emotion_by_device[dev_key] = emo
            if dev_key == _DEFAULT_MOOD_DEVICE_KEY:
                _last_anima_emotion = emo
            if len(_last_anima_emotion_by_device) > 256:
                for k in list(_last_anima_emotion_by_device.keys()):
                    if k != _DEFAULT_MOOD_DEVICE_KEY:
                        _last_anima_emotion_by_device.pop(k, None)
                        break


def get_anima_mood(device_id: Optional[str] = None) -> str:
    dev_key = _normalize_mood_device_key(device_id)
    with _anima_lock:
        if dev_key in _last_anima_emotion_by_device:
            return _last_anima_emotion_by_device[dev_key]
        return _last_anima_emotion_by_device.get(_DEFAULT_MOOD_DEVICE_KEY, _last_anima_emotion)


def clear_anima_mood(device_id: Optional[str] = None) -> None:
    """Reset cached Anima mood for one device_id or all devices (for cleanup/tests)."""
    global _last_anima_emotion
    with _anima_lock:
        if device_id is None:
            _last_anima_emotion_by_device.clear()
            _last_anima_emotion = "calm"
        else:
            dev_key = _normalize_mood_device_key(device_id)
            _last_anima_emotion_by_device.pop(dev_key, None)
            if dev_key == _DEFAULT_MOOD_DEVICE_KEY:
                _last_anima_emotion = "calm"


def resolve_mei_emotion(
    clean_text: str,
    explicit_segment_emotion: Optional[str] = None,
    turn_explicit_emotion: Optional[str] = None,
    device_id: Optional[str] = None,
) -> str:
    """3-layer emotion resolution:
    1. Explicit tag on current segment
    2. Turn-level explicit tag (unless a subsequent clause has a clear lexical mood shift;
       soft `~` punctuation never hijacks an explicit turn tag) or clause lexical cue
    3. Anima Brain PAD / turn_appraisals `last_emotion` continuity (isolated per device_id)
    4. Default 'calm'
    """
    emotions = load_mei_emotions()
    if explicit_segment_emotion and explicit_segment_emotion in emotions:
        chosen = explicit_segment_emotion
    else:
        if turn_explicit_emotion and turn_explicit_emotion in emotions:
            # Do not let casual `~` override an explicit turn tag like [happy] or [gentle]
            strong_lexical = infer_emotion_from_text(clean_text, allow_soft_wave_cue=False)
            if turn_explicit_emotion == "calm" and strong_lexical in emotions:
                chosen = strong_lexical
            elif strong_lexical in ("playful", "serious", "tender", "shy"):
                chosen = strong_lexical
            else:
                chosen = turn_explicit_emotion
        else:
            lexical = infer_emotion_from_text(clean_text, allow_soft_wave_cue=True)
            if lexical and lexical in emotions:
                chosen = lexical
            else:
                anima_emo = get_anima_mood(device_id=device_id)
                chosen = anima_emo if anima_emo in emotions else "calm"

    prof = emotions.get(chosen) or emotions["calm"]
    if not ref_audio_exists(prof.get("ref_audio_path", "")):
        return "calm"
    return chosen


def get_mei_emotion_profile(emotion_key: str) -> Dict[str, Any]:
    emotions = load_mei_emotions()
    prof = emotions.get(emotion_key) or emotions["calm"]
    if not ref_audio_exists(prof.get("ref_audio_path", "")):
        prof = emotions["calm"]
    return dict(prof)


def is_raiden_mei_voice(ref_audio_path: Optional[str]) -> bool:
    return "raiden_mei" in str(ref_audio_path or "").replace("\\", "/").lower()


def has_voice_emotion_tag_rule(prompt: str) -> bool:
    if not prompt:
        return False
    tag_hits = sum(
        1 for k in ("[calm]", "[gentle]", "[happy]", "[playful]", "[shy]", "[serious]", "[tender]") if k in prompt
    )
    return tag_hits >= 2 or ("情绪标签" in prompt and "[" in prompt)


def strip_voice_tag_instruction_for_anima(prompt: str) -> str:
    """Remove voice-only [calm]/[gentle] bracket tag output rule before syncing persona to
    18787 Companion Studio web chat, keeping personality and spoken style 100% aligned."""
    if not prompt:
        return ""
    lines = []
    for ln in prompt.splitlines():
        if has_voice_emotion_tag_rule(ln):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()

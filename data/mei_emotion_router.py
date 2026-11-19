"""Dynamic multi-emotion voice routing & smooth clause segmentation for Raiden Mei (GPT-SoVITS v4).

Responsibilities:
1. Parse and strip hidden LLM emotion tags (e.g. [gentle], [happy], [playful], [shy], [serious],
   [tender], [calm], Chinese equivalents, or <emotion:...>) so tags are NEVER spoken aloud by TTS,
   NEVER shown raw in ESP32 subtitles or 18080 Web Console chat bubbles, and NEVER pollute Anima Brain.
2. Route each clause to genuine Honkai Impact 3rd Raiden Mei (CV: 菊花花) emotion reference audio
   + tuned prosody parameters (speed_factor, temperature, top_k, top_p) with 3-layer fallback:
   Explicit LLM tag -> Clause affective cue inference -> Anima Brain PAD last_emotion -> calm.
3. Replace choppy 2-3 character opening comma splits (e.g. "哥哥，") with prosody-aware clause
   segmentation while preserving expressive ending punctuation (! / ? / ~ / …) and avoiding
   double-splitting inside GPT-SoVITS (cut0).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from typing import Any, Dict, Optional, Tuple

_LEGACY_HOST_DATA_PREFIX = "e:/故事/1/xiaozhi-server/data"
HOST_DATA_PREFIX = (
    (os.environ.get("HOST_DATA_DIR") or "").replace("\\", "/").rstrip("/")
    or _LEGACY_HOST_DATA_PREFIX
)
CONTAINER_DATA_PREFIX = "/opt/xiaozhi-esp32-server/data"
MEI_DIR_HOST = f"{HOST_DATA_PREFIX}/voices/raiden_mei"

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
    r"</?\s*(?:parameter|invoke|tool_call|function_calls?|antml:[a-z_]+|minimax:[a-z_]+)\b[^>]*>",
    flags=re.IGNORECASE,
)
_STRONG_PUNCT = ("。", "！", "!", "？", "?", "；", ";", "\n")
_SOFT_PUNCT = ("，", ",", "：")
_ALL_CUT_PUNCT = _STRONG_PUNCT + _SOFT_PUNCT

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

_anima_lock = threading.Lock()
_last_anima_emotion: str = "calm"
_cached_manifest: Optional[Dict[str, Dict[str, Any]]] = None


def _rewrite_host_data_path(raw_path: str) -> str:
    p = (raw_path or "").replace("\\", "/").strip()
    if not p:
        return p
    if "${HOST_DATA_DIR}" in p:
        p = p.replace("${HOST_DATA_DIR}", HOST_DATA_PREFIX)
    if p.lower().startswith(_LEGACY_HOST_DATA_PREFIX.lower()):
        p = HOST_DATA_PREFIX + p[len(_LEGACY_HOST_DATA_PREFIX) :]
    elif p.startswith("data/"):
        p = HOST_DATA_PREFIX + "/" + p[len("data/") :]
    elif p.startswith("voices/"):
        p = HOST_DATA_PREFIX + "/" + p
    return p


def _to_local_fs_path(host_or_local_path: str) -> str:
    p = _rewrite_host_data_path(host_or_local_path)
    if os.name != "nt":
        for pfx in (HOST_DATA_PREFIX, _LEGACY_HOST_DATA_PREFIX):
            if p.lower().startswith(pfx.lower()):
                return CONTAINER_DATA_PREFIX + p[len(pfx) :]
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
    cleaned = _LEAKED_XML_TOOL_TAG_RE.sub("", cleaned)
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


def safe_clean_response_garbage(text: str) -> str:
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
    result = _LEAKED_XML_TOOL_TAG_RE.sub("", result)
    m_tail = re.search(r'(?:["\']\s*\}+|\}{2,})\s*$', result)
    if m_tail:
        result = result[: m_tail.start()].rstrip()
    elif removed_line:
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


def set_anima_mood(mood_summary: str) -> None:
    """Update cached Anima Brain emotion from `/xiaozhi/recall` mood summary string."""
    global _last_anima_emotion
    if not mood_summary:
        return
    m = re.search(r"last_emotion\s*=\s*([A-Za-z_]+)", mood_summary)
    raw = m.group(1).strip().lower() if m else mood_summary.strip().lower()
    emo, _ = normalize_emotion_name(raw)
    if emo:
        with _anima_lock:
            _last_anima_emotion = emo


def get_anima_mood() -> str:
    with _anima_lock:
        return _last_anima_emotion


def resolve_mei_emotion(
    clean_text: str,
    explicit_segment_emotion: Optional[str] = None,
    turn_explicit_emotion: Optional[str] = None,
) -> str:
    """3-layer emotion resolution:
    1. Explicit tag on current segment
    2. Turn-level explicit tag (unless a subsequent clause has a clear lexical mood shift;
       soft `~` punctuation never hijacks an explicit turn tag) or clause lexical cue
    3. Anima Brain PAD / turn_appraisals `last_emotion` continuity
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
                anima_emo = get_anima_mood()
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


def find_prosody_cut_pos(
    current_text: str, is_first_sentence: bool, quote_safe_cut_fn
) -> int:
    """Find smooth clause boundary without chopping 2-3 char openings at the first comma."""
    if not current_text:
        return -1
    if has_unclosed_leading_tag(current_text):
        return -1

    min_strong = 6 if is_first_sentence else 4
    min_soft = 18 if is_first_sentence else 28

    # Check if a strong sentence boundary within comfortable breath length is already buffered
    first_strong_cut = -1
    for idx, ch in enumerate(current_text):
        if ch in _STRONG_PUNCT:
            safe = quote_safe_cut_fn(current_text, idx, _ALL_CUT_PUNCT)
            if safe != -1 and count_spoken_chars(current_text[: safe + 1]) >= min_strong:
                first_strong_cut = safe
                break

    if first_strong_cut != -1 and count_spoken_chars(current_text[: first_strong_cut + 1]) <= 38:
        return first_strong_cut

    # Otherwise allow soft comma/colon split only once the clause is long enough (>=18 / >=28 spoken chars)
    for idx, ch in enumerate(current_text):
        if ch in _STRONG_PUNCT:
            safe = quote_safe_cut_fn(current_text, idx, _ALL_CUT_PUNCT)
            if safe != -1 and count_spoken_chars(current_text[: safe + 1]) >= min_strong:
                return safe
        elif ch in _SOFT_PUNCT:
            safe = quote_safe_cut_fn(current_text, idx, _ALL_CUT_PUNCT)
            if safe != -1 and count_spoken_chars(current_text[: safe + 1]) >= min_soft:
                return safe

    return -1


async def _send_esp32_emotion_ws(conn, emoji: str, esp32_emotion: str) -> None:
    if not conn or not getattr(conn, "websocket", None):
        return
    if not (getattr(conn, "features", None) or {}).get("emoji", True):
        return
    cur_sid = getattr(conn, "sentence_id", None)
    if getattr(conn, "_last_pushed_mei_emotion", None) == (cur_sid, esp32_emotion):
        return
    conn._last_pushed_mei_emotion = (cur_sid, esp32_emotion)
    conn._last_pushed_mei_sid = cur_sid
    payload = json.dumps(
        {
            "type": "llm",
            "text": emoji,
            "emotion": esp32_emotion,
            "session_id": getattr(conn, "session_id", ""),
        }
    )
    try:
        await conn.websocket.send(payload)
    except Exception:
        pass


def _push_esp32_emotion_async(conn, emoji: str, esp32_emotion: str) -> None:
    if not conn or not getattr(conn, "websocket", None) or not getattr(conn, "loop", None):
        return
    if not (getattr(conn, "features", None) or {}).get("emoji", True):
        return
    cur_sid = getattr(conn, "sentence_id", None)
    if getattr(conn, "_last_pushed_mei_emotion", None) == (cur_sid, esp32_emotion):
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _send_esp32_emotion_ws(conn, emoji, esp32_emotion), conn.loop
        )
    except Exception:
        pass


def apply_xiaozhi_emotion_and_prosody_patches() -> None:
    """Patch xiaozhi-esp32-server TTS streaming, GPT-SoVITS v2/v4 emotion routing,
    direct_answer chunk cleaner, prompt harmony, subtitle firewall, and playback-synced
    ESP32 emotion indicator in memory at container startup."""
    import requests
    from core.providers.tts.base import TTSProviderBase, logger as base_logger
    from core.providers.tts import gpt_sovits_v2 as gsv_mod
    import core.handle.sendAudioHandle as send_audio_mod
    import core.utils.textUtils as text_utils_mod
    from core.connection import ConnectionHandler
    from core.utils.prompt_manager import PromptManager

    if getattr(TTSProviderBase, "_mei_prosody_patched", False):
        return

    # 0. Fix upstream direct_answer delta cleaner eating closing `]` on [gentle] / [playful] / [安全挡]
    ConnectionHandler._clean_response_garbage = staticmethod(safe_clean_response_garbage)

    # 0b. Harmonize agent-base-prompt.txt & direct_answer few-shot when [emotion] tag mode is active
    _orig_build_enhanced_prompt = PromptManager.build_enhanced_prompt
    _orig_inject_fewshot = ConnectionHandler._inject_tool_call_fewshot

    def _patched_build_enhanced_prompt(
        self, user_prompt: str, device_id: str, client_ip: str = None, *args, **kwargs
    ) -> str:
        if has_voice_emotion_tag_rule(user_prompt):
            kw = dict(kwargs)
            kw["emoji_enabled"] = False
            rendered = _orig_build_enhanced_prompt(
                self, user_prompt, device_id, client_ip, *args, **kw
            )
            rendered = rendered.replace(
                'NEVER use brackets to express inner thoughts or actions (e.g. ”[无奈地叹气]”,”(笑着说)”).',
                'Start every spoken reply (including direct_answer.response) with ONE hidden English emotion tag from [calm], [gentle], [happy], [playful], [shy], [serious], [tender] (optional second tag only if mood genuinely shifts mid-reply). NEVER use stage-direction brackets like ”[无奈地叹气]” or ”(笑着说)”.',
            )
            rendered = rendered.replace(
                '1. [Get to the point] Never be verbose. The first sentence must directly address the topic — no unnecessary pleasantries or preamble.',
                '1. [Natural pacing] Be engaging, warm, and thorough. Follow the length and conversational pacing described in <identity> rather than cutting answers overly short.',
            )
            rendered = rendered.replace(
                '- [Slice long content] For long-form content like storytelling or knowledge sharing, NEVER dump everything at once. Start with the core content directly, then naturally ask the user whether to continue at the end (e.g., tell an actual segment first, then ask ”要继续听吗？”). Start on demand, stop on demand, yield to interruptions.',
                '- [Rich storytelling & explanation] For storytelling, explanations, or thoughtful chat, give a full, vivid, satisfying multi-sentence segment instead of stopping after just one or two sentences; only pause to ask if the user wants to keep going after delivering a substantial part.',
            )
            rendered = rendered.replace('"咋回事"', '"怎么啦"')
            return rendered
        return _orig_build_enhanced_prompt(
            self, user_prompt, device_id, client_ip, *args, **kwargs
        )

    def _patched_inject_tool_call_fewshot(self):
        res = _orig_inject_fewshot(self)
        try:
            if has_voice_emotion_tag_rule(str((self.config or {}).get("prompt") or "")):
                for m in getattr(self.dialogue, "dialogue", []) or []:
                    if getattr(m, "is_temporary", False) and getattr(m, "tool_calls", None):
                        for tc in m.tool_calls:
                            fn = tc.get("function") if isinstance(tc, dict) else None
                            if isinstance(fn, dict) and fn.get("name") == "direct_answer":
                                fn["arguments"] = json.dumps(
                                    {
                                        "response": "[gentle] 好呀，哥哥想听什么类型的故事呢？轻松一点的日常，还是奇幻冒险的？"
                                    },
                                    ensure_ascii=False,
                                )
        except Exception:
            pass
        return res

    PromptManager.build_enhanced_prompt = _patched_build_enhanced_prompt
    ConnectionHandler._inject_tool_call_fewshot = _patched_inject_tool_call_fewshot

    # 1. Patch TTSProviderBase.tts_text_priority_thread FIRST reset + smooth segmentation
    _orig_store_tts_text = TTSProviderBase.store_tts_text
    _orig_to_tts = TTSProviderBase.to_tts
    _orig_to_tts_stream = TTSProviderBase.to_tts_stream

    def _patched_store_tts_text(self, sentence_id, text):
        clean = strip_emotion_tags(text or "")
        return _orig_store_tts_text(self, sentence_id, clean)

    def _patched_get_segment_text(self) -> Optional[str]:
        while True:
            full_text = "".join(self.tts_text_buff)
            current_text = full_text[self.processed_chars :]
            if not current_text:
                return None

            # Reset per-turn emotion state when a brand-new sentence_id starts
            cur_sid = getattr(self, "current_sentence_id", None)
            if getattr(self, "_emotion_turn_sid", None) != cur_sid:
                self._emotion_turn_sid = cur_sid
                self._turn_explicit_emotion = None
                self._pending_segment_emotion = None
                self._segment_emotion_queue = []

            # Absorb leading tag early ONLY for the very first unspoken sentence of the turn
            s_lead = _strip_leading_ws_and_emojis(current_text)
            m_lead = _BRACKET_CANDIDATE_RE.match(s_lead)
            if m_lead:
                emo_cand, should_strip = normalize_emotion_name(
                    next((g for g in m_lead.groups() if g is not None), "")
                )
                if should_strip and emo_cand:
                    if self._turn_explicit_emotion is None:
                        self._turn_explicit_emotion = emo_cand
                    conn = getattr(self, "conn", None)
                    if (
                        self.is_first_sentence
                        and getattr(self, "tts_audio_first_sentence", True)
                        and getattr(conn, "_last_pushed_mei_sid", None) != cur_sid
                    ):
                        prof = get_mei_emotion_profile(emo_cand)
                        _push_esp32_emotion_async(
                            conn,
                            prof["esp32_emoji"],
                            prof["esp32_emotion"],
                        )

            cut_pos = find_prosody_cut_pos(
                current_text, self.is_first_sentence, self._quote_safe_cut
            )
            if cut_pos != -1:
                segment_raw = current_text[: cut_pos + 1]
                self.processed_chars += len(segment_raw)
                seg_emo, stripped = extract_and_strip_emotion(segment_raw, flush_incomplete=False)
                if seg_emo:
                    self._turn_explicit_emotion = seg_emo
                    self._pending_segment_emotion = seg_emo
                cleaned = clean_segment_for_tts(stripped)
                if not cleaned:
                    # Pure emotion tag line (e.g. "[gentle]\n"): loop without flipping is_first_sentence
                    continue
                if self.is_first_sentence:
                    self.is_first_sentence = False
                return cleaned

            if self.tts_stop_request and current_text:
                self.processed_chars = len(full_text)
                seg_emo, stripped = extract_and_strip_emotion(current_text, flush_incomplete=True)
                if seg_emo:
                    self._turn_explicit_emotion = seg_emo
                    self._pending_segment_emotion = seg_emo
                self.is_first_sentence = True
                cleaned = clean_segment_for_tts(stripped)
                return cleaned or None

            return None

    def _patched_process_remaining_text_stream(self, opus_handler=None):
        did_any = False
        # First drain all complete prosodic sentences individually (preserving per-sentence emotion tags)
        while True:
            seg = self._get_segment_text()
            if not seg:
                break
            self.to_tts_stream(seg, opus_handler=opus_handler)
            did_any = True

        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
        self.processed_chars = len(full_text)
        if not remaining_text:
            return did_any
        cur_sid = getattr(self, "current_sentence_id", None)
        if getattr(self, "_emotion_turn_sid", None) != cur_sid:
            self._emotion_turn_sid = cur_sid
            self._turn_explicit_emotion = None
            self._pending_segment_emotion = None
            self._segment_emotion_queue = []
        seg_emo, stripped = extract_and_strip_emotion(remaining_text, flush_incomplete=True)
        if seg_emo:
            self._turn_explicit_emotion = seg_emo
            self._pending_segment_emotion = seg_emo
        segment_text = clean_segment_for_tts(stripped)
        if segment_text:
            self.to_tts_stream(segment_text, opus_handler=opus_handler)
            return True
        return did_any

    def _patched_to_tts_stream(self, text, opus_handler=None):
        seg_emo, stripped = extract_and_strip_emotion(text or "", flush_incomplete=True)
        if seg_emo:
            self._turn_explicit_emotion = seg_emo
            self._pending_segment_emotion = seg_emo
        cleaned = clean_segment_for_tts(stripped)
        if not cleaned:
            return None
        return _orig_to_tts_stream(self, cleaned, opus_handler=opus_handler)

    def _patched_to_tts(self, text):
        seg_emo, stripped = extract_and_strip_emotion(text or "", flush_incomplete=True)
        if seg_emo:
            self._turn_explicit_emotion = seg_emo
            self._pending_segment_emotion = seg_emo
        cleaned = clean_segment_for_tts(stripped)
        if not cleaned:
            return None
        return _orig_to_tts(self, cleaned)

    TTSProviderBase.store_tts_text = _patched_store_tts_text
    TTSProviderBase._get_segment_text = _patched_get_segment_text
    TTSProviderBase._process_remaining_text_stream = _patched_process_remaining_text_stream
    TTSProviderBase.to_tts_stream = _patched_to_tts_stream
    TTSProviderBase.to_tts = _patched_to_tts

    # 2. Patch GPT_SOVITS_V2.TTSProvider.text_to_speak for dynamic multi-emotion reference routing + cut0
    async def _patched_gsv_text_to_speak(self, text, output_file):
        seg_emo, stripped = extract_and_strip_emotion(text or "", flush_incomplete=True)
        clean_text = clean_segment_for_tts(stripped)
        if not clean_text:
            return b""

        pending_emo = getattr(self, "_pending_segment_emotion", None)
        explicit_seg = seg_emo or pending_emo
        turn_emo = getattr(self, "_turn_explicit_emotion", None)

        ref_audio_path = self.ref_audio_path
        aux_ref_audio_paths = self.aux_ref_audio_paths
        prompt_text = self.prompt_text
        prompt_lang = self.prompt_lang
        top_k = self.top_k
        top_p = self.top_p
        temperature = self.temperature
        speed_factor = self.speed_factor
        # Avoid double-splitting inside GPT-SoVITS when clauses are already segmented
        split_method = "cut0" if self.text_split_method in ("cut5", "cut0", "", None) else self.text_split_method

        chosen_prof = None
        if is_raiden_mei_voice(self.ref_audio_path):
            emo_key = resolve_mei_emotion(
                clean_text,
                explicit_segment_emotion=explicit_seg,
                turn_explicit_emotion=turn_emo,
            )
            prof = get_mei_emotion_profile(emo_key)
            chosen_prof = prof
            ref_audio_path = prof["ref_audio_path"]
            aux_ref_audio_paths = []
            prompt_text = prof["prompt_text"]
            prompt_lang = prof.get("prompt_lang", "zh")
            speed_factor = float(prof.get("speed_factor", self.speed_factor))
            temperature = float(prof.get("temperature", self.temperature))
            top_k = int(prof.get("top_k", self.top_k))
            top_p = float(prof.get("top_p", self.top_p))
            split_method = "cut0"

            base_logger.bind(tag=gsv_mod.TAG).info(
                f"[芽衣多情绪路由] 情绪={emo_key}({prof['label_zh']}) | speed={speed_factor} | 文本={clean_text}"
            )

        request_json = {
            "text": clean_text,
            "text_lang": self.text_lang,
            "ref_audio_path": ref_audio_path,
            "aux_ref_audio_paths": aux_ref_audio_paths,
            "prompt_text": prompt_text,
            "prompt_lang": prompt_lang,
            "top_k": top_k,
            "top_p": top_p,
            "temperature": temperature,
            "text_split_method": split_method,
            "batch_size": self.batch_size,
            "batch_threshold": self.batch_threshold,
            "split_bucket": self.split_bucket,
            "return_fragment": self.return_fragment,
            "speed_factor": speed_factor,
            "streaming_mode": self.streaming_mode,
            "seed": self.seed,
            "parallel_infer": self.parallel_infer,
            "repetition_penalty": self.repetition_penalty,
        }

        resp = requests.post(self.url, json=request_json, timeout=self.tts_timeout)
        if resp.status_code == 200:
            # Clear pending segment emotion only after synthesis succeeds (safe across retries)
            self._pending_segment_emotion = None
            if chosen_prof is not None:
                conn = getattr(self, "conn", None)
                cur_sid = getattr(self, "current_sentence_id", None) or getattr(
                    conn, "sentence_id", None
                )
                q = getattr(self, "_segment_emotion_queue", None)
                if q is None:
                    q = []
                    self._segment_emotion_queue = q
                q.append(
                    (
                        cur_sid,
                        clean_text,
                        chosen_prof["esp32_emoji"],
                        chosen_prof["esp32_emotion"],
                    )
                )
                if len(q) > 64:
                    del q[:-64]
                # Only push immediately if no audio has begun playing yet for this turn;
                # subsequent clause emotions are delivered by _patched_send_tts_message via AudioRateController!
                if getattr(self, "tts_audio_first_sentence", True) and getattr(
                    conn, "_last_pushed_mei_sid", None
                ) != cur_sid:
                    _push_esp32_emotion_async(
                        conn,
                        chosen_prof["esp32_emoji"],
                        chosen_prof["esp32_emotion"],
                    )
            if output_file:
                with open(output_file, "wb") as file:
                    file.write(resp.content)
            else:
                return resp.content
        else:
            error_msg = f"GPT_SoVITS_V2 TTS请求失败: {resp.status_code} - {resp.text}"
            base_logger.bind(tag=gsv_mod.TAG).error(error_msg)
            raise Exception(error_msg)

    gsv_mod.TTSProvider.text_to_speak = _patched_gsv_text_to_speak
    gsv_mod.TTSProvider.voice = property(
        lambda self: "raiden_mei" if is_raiden_mei_voice(getattr(self, "ref_audio_path", "")) else "default"
    )

    # 3. Subtitle firewall + playback-synchronized ESP32 emotion update on sentence_start
    _orig_send_tts_message = send_audio_mod.send_tts_message

    async def _patched_send_tts_message(conn, state, text=None):
        if text is not None:
            text = strip_emotion_tags(text).strip()
            if not text and state == "sentence_start":
                return
        if state == "sentence_start" and text and conn is not None:
            tts = getattr(conn, "tts", None)
            q = getattr(tts, "_segment_emotion_queue", None)
            if q:
                cur_sid = getattr(conn, "sentence_id", None)
                # Prune stale turns
                q[:] = [item for item in q if item[0] == cur_sid]
                matched_idx = next(
                    (i for i, item in enumerate(q) if item[1] == text),
                    0 if q else None,
                )
                if matched_idx is not None:
                    _, _, emj, esp_emo = q.pop(matched_idx)
                    await _send_esp32_emotion_ws(conn, emj, esp_emo)
        return await _orig_send_tts_message(conn, state, text)

    send_audio_mod.send_tts_message = _patched_send_tts_message

    # 4. Enhance textUtils.get_emotion so first-chunk tag/cue maps accurately without misfiring on "["
    _orig_get_emotion = text_utils_mod.get_emotion

    async def _patched_get_emotion(conn, text):
        if text and has_unclosed_leading_tag(text):
            # Wait for TTSProvider to push exact emotion once closing bracket arrives
            return
        seg_emo, stripped = extract_and_strip_emotion(text or "", flush_incomplete=False)
        if seg_emo:
            prof = get_mei_emotion_profile(seg_emo)
            _push_esp32_emotion_async(conn, prof["esp32_emoji"], prof["esp32_emotion"])
            return
        if conn and has_voice_emotion_tag_rule(str((getattr(conn, "config", None) or {}).get("prompt") or "")):
            # Avoid upstream default `🙂 happy` misfiring before Mei's first clause resolves
            return
        return await _orig_get_emotion(conn, stripped)

    text_utils_mod.get_emotion = _patched_get_emotion
    TTSProviderBase._mei_prosody_patched = True
    print("[MeiEmotionRouter] 已挂载芽衣7情绪动态参考音频路由 + 防碎句韵律切分补丁 (cut0)", flush=True)


MEI_WAKEUP_RESPONSES = [
    "我在呢，哥哥请说吧。",
    "在呢，哥哥有什么事呀？",
    "我在听呢，哥哥请讲。",
    "哥哥叫我吗？我在呢。",
    "在呢，怎么啦哥哥？",
    "我一直在呢，随时告诉我吧。",
]
MEI_WAKEUP_FALLBACK_TEXT = "我在呢，哥哥请说吧。"
MEI_WAKEUP_FALLBACK_WAV = "data/voices/raiden_mei/wakeup_mei_default.wav"


def _trim_wav_leading_breath(wav_bytes: bytes) -> bytes:
    """Trim soft leading breath/silence from short GPT-SoVITS wakeup clips for instant response."""
    import io
    import math
    import struct
    import wave

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
                return wav_bytes
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
        win = max(1, int(sr * 0.02))
        onset = 0
        for i in range(0, len(samples) - win, win):
            rms = math.sqrt(sum(s * s for s in samples[i : i + win]) / win)
            if rms > 850:
                onset = max(0, i - int(sr * 0.04))
                break
        end = len(samples)
        for i in range(len(samples) - win, onset, -win):
            rms = math.sqrt(sum(s * s for s in samples[i : i + win]) / win)
            if rms > 250:
                end = min(len(samples), i + win + int(sr * 0.12))
                break
        if end - onset < int(sr * 0.35):
            return wav_bytes
        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(struct.pack(f"<{end - onset}h", *samples[onset:end]))
        return out.getvalue()
    except Exception:
        return wav_bytes


def get_wakeup_responses_for_tts(tts) -> list:
    if tts is not None and is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")):
        return list(MEI_WAKEUP_RESPONSES)
    return []


def apply_mei_wakeup_responses_patch() -> None:
    """Call AFTER pc_audio_router.apply_pc_audio_playback_patches() so helloHandle import order is preserved."""
    import random
    import sys
    import time
    import uuid
    import core.handle.helloHandle as hello_mod
    from core.providers.tts.dto.dto import SentenceType
    from core.utils.dialogue import Message

    if getattr(hello_mod, "_mei_wakeup_patched", False):
        return

    _orig_check_wakeup = hello_mod.checkWakeupWords
    _orig_wakeup_response = hello_mod.wakeupWordsResponse
    _orig_preheat = hello_mod.preheat_wakeup_audio_cache
    _last_mei_reply = {"text": ""}

    async def _mei_aware_wakeup_words_response(conn):
        tts = getattr(conn, "tts", None)
        if tts is None or not is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")):
            return await _orig_wakeup_response(conn)

        try:
            if not await hello_mod._wakeup_response_lock.acquire():
                return

            pool = [r for r in MEI_WAKEUP_RESPONSES if r != _last_mei_reply["text"]] or list(MEI_WAKEUP_RESPONSES)
            result = random.choice(pool)
            _last_mei_reply["text"] = result

            # Guide GPT-SoVITS into Mei's warm 'gentle' reference clip without putting tags into OLED subtitles
            tts._pending_segment_emotion = "gentle"
            tts._turn_explicit_emotion = "gentle"
            tts_result = await asyncio.to_thread(tts.to_tts, result)
            if not tts_result:
                return

            voice = getattr(tts, "voice", "raiden_mei") or "raiden_mei"
            if isinstance(tts_result, str):
                wav_bytes = await asyncio.to_thread(
                    hello_mod.audio_file_to_wav_bytes, tts_result, conn.sample_rate
                )
            else:
                wav_bytes = hello_mod.opus_datas_to_wav_bytes(tts_result, sample_rate=conn.sample_rate)

            wav_bytes = _trim_wav_leading_breath(wav_bytes)
            file_path = hello_mod.wakeup_words_config.generate_file_path(voice)
            with open(file_path, "wb") as f:
                f.write(wav_bytes)
            hello_mod.wakeup_words_config.update_wakeup_response(voice, file_path, result)
            await hello_mod.audio_to_data(file_path, use_cache=True)
        finally:
            if hello_mod._wakeup_response_lock.locked():
                hello_mod._wakeup_response_lock.release()

    async def _mei_aware_check_wakeup_words(conn, text):
        tts = getattr(conn, "tts", None)
        # Wait briefly if TTS is still initializing (same behavior as upstream)
        if tts is None:
            start_t = time.time()
            while time.time() - start_t < 3:
                tts = getattr(conn, "tts", None)
                if tts is not None:
                    break
                await asyncio.sleep(0.1)

        if tts is None or not is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")):
            return await _orig_check_wakeup(conn, text)

        if not conn.config.get("enable_wakeup_words_response_cache"):
            return False

        _, filtered_text = hello_mod.remove_punctuation_and_length(text)
        if filtered_text not in (conn.config.get("wakeup_words") or []):
            return False

        conn.just_woken_up = True
        await hello_mod.send_tts_message(conn, "start")

        voice = getattr(tts, "voice", "raiden_mei") or "raiden_mei"
        response = hello_mod.wakeup_words_config.get_wakeup_response(voice)
        if not response or not response.get("file_path"):
            fallback_wav = (
                MEI_WAKEUP_FALLBACK_WAV
                if os.path.isfile(MEI_WAKEUP_FALLBACK_WAV)
                else "config/assets/wakeup_words_short.wav"
            )
            response = {
                "voice": voice,
                "file_path": fallback_wav,
                "time": 0,
                "text": MEI_WAKEUP_FALLBACK_TEXT,
            }
            if os.path.isfile(MEI_WAKEUP_FALLBACK_WAV):
                try:
                    hello_mod.wakeup_words_config.update_wakeup_response(
                        voice, MEI_WAKEUP_FALLBACK_WAV, MEI_WAKEUP_FALLBACK_TEXT
                    )
                except Exception:
                    pass

        opus_packets = await hello_mod.audio_to_data(response.get("file_path"), use_cache=True)
        conn.client_abort = False
        conn.sentence_id = str(uuid.uuid4().hex)

        prof = get_mei_emotion_profile("gentle")
        _push_esp32_emotion_async(conn, prof["esp32_emoji"], prof["esp32_emotion"])

        conn.dialogue.put(Message(role="assistant", content=response.get("text")))
        hello_mod._wakeup_logger.bind(tag=hello_mod.TAG).info(
            f"播放芽衣唤醒词回复: {response.get('text')}"
        )

        await hello_mod.sendAudioMessage(conn, SentenceType.FIRST, opus_packets, response.get("text"))
        asyncio.create_task(hello_mod._finish_wakeup_reply(conn))

        if time.time() - response.get("time", 0) > hello_mod.WAKEUP_CONFIG["refresh_time"]:
            if not hello_mod._wakeup_response_lock.locked():
                asyncio.create_task(_mei_aware_wakeup_words_response(conn))
        return True

    async def _mei_aware_preheat():
        await _orig_preheat()
        try:
            resp = hello_mod.wakeup_words_config.get_wakeup_response("raiden_mei")
            fp = (resp or {}).get("file_path") or (
                MEI_WAKEUP_FALLBACK_WAV if os.path.isfile(MEI_WAKEUP_FALLBACK_WAV) else None
            )
            if fp and os.path.isfile(fp):
                await hello_mod.audio_to_data(fp, use_cache=True)
        except Exception:
            pass

    hello_mod.wakeupWordsResponse = _mei_aware_wakeup_words_response
    hello_mod.checkWakeupWords = _mei_aware_check_wakeup_words
    hello_mod.preheat_wakeup_audio_cache = _mei_aware_preheat
    hello_mod._mei_wakeup_patched = True

    # Ensure already-imported references in intent/text/audio handlers point to the patched checkWakeupWords
    for mod_name in (
        "core.handle.intentHandler",
        "core.handle.textHandler.listenMessageHandler",
        "core.handle.receiveAudioHandle",
    ):
        try:
            m = sys.modules.get(mod_name) or __import__(mod_name, fromlist=["checkWakeupWords"])
        except Exception:
            m = sys.modules.get(mod_name)
        if m is not None and hasattr(m, "checkWakeupWords"):
            setattr(m, "checkWakeupWords", _mei_aware_check_wakeup_words)

    # Patch ListenTextMessageHandler so offline interrupt words (e.g. "听我说") switch straight to
    # listening with ZERO greeting TTS and ZERO 2s just_woken_up VAD suppression (even from IDLE).
    try:
        import core.handle.textHandler.listenMessageHandler as listen_mod
        import core.handle.abortHandle as abort_mod

        cls = getattr(listen_mod, "ListenTextMessageHandler", None)
        if cls is not None and not getattr(cls, "_interrupt_word_patched", False):
            _orig_listen_handle = cls.handle

            async def _interrupt_aware_listen_handle(self, conn, msg_json):
                if isinstance(msg_json, dict) and msg_json.get("state") == "detect" and "text" in msg_json:
                    original_text = str(msg_json.get("text") or "")
                    _, filtered_text = listen_mod.remove_punctuation_and_length(original_text)
                    cfg_words = (getattr(conn, "config", None) or {}).get("interrupt_words")
                    if not isinstance(cfg_words, (list, tuple, set)):
                        cfg_words = ("听我说",)
                    norm_words = {
                        listen_mod.remove_punctuation_and_length(str(w))[1]
                        for w in cfg_words
                        if str(w).strip()
                    }
                    if filtered_text and filtered_text in norm_words:
                        conn.client_have_voice = False
                        conn.reset_audio_states()
                        conn.last_activity_time = time.time() * 1000
                        conn.just_woken_up = False
                        if getattr(conn, "client_is_speaking", False):
                            try:
                                await abort_mod.handleAbortMessage(conn)
                            except Exception:
                                pass
                        await listen_mod.send_stt_message(conn, original_text)
                        await hello_mod.send_tts_message(conn, "stop", None)
                        conn.client_is_speaking = False
                        conn.logger.bind(tag=listen_mod.TAG).info(
                            f"收到打断词「{original_text}」，已静音并直接进入聆听模式"
                        )
                        return
                return await _orig_listen_handle(self, conn, msg_json)

            cls.handle = _interrupt_aware_listen_handle
            cls._interrupt_word_patched = True
    except Exception as exc:
        print(f"[MeiEmotionRouter] 挂载打断词直通聆听补丁跳过: {exc}", flush=True)

    print("[MeiEmotionRouter] 已挂载芽衣专属唤醒词秒回语 + 打断词直通聆听补丁 (raiden_mei 独立缓存 + gentle 温柔音色)", flush=True)

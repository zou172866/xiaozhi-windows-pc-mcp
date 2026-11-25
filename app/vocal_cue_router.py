"""Sherpa-ONNX SenseVoice user vocal emotion & acoustic event awareness (Phase 1 ①).

Zero import-time side effects: no background threads, no disk I/O, no monkey-patching.

Extracts SenseVoice acoustic metadata from both `stream.result.{emotion,event,lang}` attributes
and inline `<|...|>` tokens in raw ASR output, filters neutral/noise markers (`NEUTRAL`,
`Speech`, `BGM`, `EMO_UNKNOWN`, `Event_UNK`), caches a restrained one-shot `<user_vocal_cue>`
hint per `session_id` (bounded 64 sessions, 20s TTL), and publishes structured `user_vocal_cue`
events to `runtime_events` for 18080 Web Console observability.
"""

from __future__ import annotations

from collections import OrderedDict, deque
import re
import threading
import time
from typing import Any, Dict, List, Optional

import runtime_events
import runtime_settings

MAX_CACHED_SESSIONS = 64
DEFAULT_TTL_SECONDS = 20.0
RECENT_CUES_MAXLEN = 20

_SENSEVOICE_TAG_RE = re.compile(r"<\|([^|]+)\|>")

# Non-neutral user vocal emotions supported by SenseVoice
_EMOTION_LABELS_ZH: Dict[str, str] = {
    "HAPPY": "偏愉快、轻快",
    "SAD": "偏低落、有些疲惫或难过",
    "ANGRY": "偏急躁、有些生气或烦躁",
    "FEARFUL": "偏紧张、有些担忧或不安",
    "DISGUSTED": "偏反感、有些不耐烦",
    "SURPRISED": "带着明显的惊讶",
}

_IGNORED_EMOTIONS = frozenset(
    {
        "",
        "NEUTRAL",
        "OTHER",
        "EMO_UNKNOWN",
        "UNKNOWN",
        "NONE",
    }
)

# Human vocal acoustic events supported by SenseVoice (excluding generic Speech/BGM/noise/breath)
_EVENT_LABELS_ZH: Dict[str, str] = {
    "LAUGHTER": "带笑声",
    "CRYING": "带哭腔或哽咽",
    "SIGH": "检测到叹气",
    "COUGH": "检测到咳嗽",
    "SNEEZE": "检测到打喷嚏",
    "YAWN": "检测到打哈欠",
    "APPLAUSE": "带掌声",
    "SING": "在哼唱",
}

# SenseVoice tokens.txt emits `<|Cry|>` (not `<|Crying|>`), plus variant spellings across providers
_EVENT_ALIASES: Dict[str, str] = {
    "CRY": "CRYING",
    "LAUGH": "LAUGHTER",
    "COUGHING": "COUGH",
    "SNEEZING": "SNEEZE",
    "SIGHING": "SIGH",
    "YAWNING": "YAWN",
    "SINGING": "SING",
}

_IGNORED_EVENTS = frozenset(
    {
        "",
        "SPEECH",
        "SPEECH_NOISE",
        "BGM",
        "BREATH",
        "GBG",
        "EVENT_UNK",
        "UNK",
        "UNKNOWN",
        "NOISE",
        "MUSIC",
        "NONE",
    }
)

_KNOWN_LANG_TAGS = frozenset({"ZH", "EN", "JA", "KO", "YUE", "NOSPEECH"})

_lock = threading.Lock()
_session_cues: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_recent_cues: "deque[Dict[str, Any]]" = deque(maxlen=RECENT_CUES_MAXLEN)


def _strip_tag_wrapper(raw: Any) -> str:
    s = str(raw or "").strip()
    if s.startswith("<|") and s.endswith("|>"):
        s = s[2:-2].strip()
    return s


def _normalize_token(raw: Any) -> str:
    return _strip_tag_wrapper(raw).strip().upper()


def format_vocal_cue_block(
    *,
    emotion: str = "",
    events: Optional[List[str]] = None,
) -> tuple[str, str]:
    """Return `(prompt_block, summary_zh)` for a validated non-neutral vocal cue."""
    emo_key = str(emotion or "").strip().upper()
    emo_zh = _EMOTION_LABELS_ZH.get(emo_key, "")

    ev_keys: List[str] = []
    ev_zh_list: List[str] = []
    for raw_ev in events or []:
        ek = _EVENT_ALIASES.get(str(raw_ev or "").strip().upper(), str(raw_ev or "").strip().upper())
        if ek in _EVENT_LABELS_ZH and ek not in ev_keys:
            ev_keys.append(ek)
            ev_zh_list.append(_EVENT_LABELS_ZH[ek])

    if not emo_zh and not ev_zh_list:
        return "", ""

    ev_joined = "、".join(ev_zh_list)
    if emo_zh and ev_joined:
        body = (
            f"【语音语气参考】对方刚才说话语气{emo_zh}（{ev_joined}）；"
            "请自然共情回应，勿机械复述语气或声学标签。"
        )
        summary_zh = f"语气{emo_zh}（{ev_joined}）"
    elif emo_zh:
        body = (
            f"【语音语气参考】对方刚才说话语气{emo_zh}；"
            "请自然共情回应，勿机械复述语气标签。"
        )
        summary_zh = f"语气{emo_zh}"
    else:
        body = (
            f"【语音语气参考】对方刚才说话时{ev_joined}；"
            "请自然体贴地顺着聊，勿机械复述声学标签。"
        )
        summary_zh = ev_joined

    return f"<user_vocal_cue>\n{body}\n</user_vocal_cue>", summary_zh


def extract_vocal_cue(raw_text: str = "", *, result: Any = None) -> Optional[Dict[str, Any]]:
    """Extract non-neutral SenseVoice vocal emotion & acoustic events from result attrs and inline tags.

    Returns `None` when only neutral / generic speech / BGM / unknown tags are present.
    """
    emotion_key = ""
    events_found: List[str] = []
    lang_key = ""

    def _consume_emotion(val: Any) -> None:
        nonlocal emotion_key
        tok = _normalize_token(val)
        if tok in _IGNORED_EMOTIONS:
            return
        if tok in _EMOTION_LABELS_ZH and not emotion_key:
            emotion_key = tok

    def _consume_event(val: Any) -> None:
        tok = _normalize_token(val)
        tok = _EVENT_ALIASES.get(tok, tok)
        if tok in _IGNORED_EVENTS:
            return
        if tok in _EVENT_LABELS_ZH and tok not in events_found:
            events_found.append(tok)

    def _consume_lang(val: Any) -> None:
        nonlocal lang_key
        tok = _normalize_token(val)
        if tok in _KNOWN_LANG_TAGS and tok != "NOSPEECH" and not lang_key:
            lang_key = tok.lower()

    if result is not None:
        _consume_emotion(getattr(result, "emotion", ""))
        _consume_event(getattr(result, "event", ""))
        _consume_lang(getattr(result, "lang", ""))

    text_str = str(raw_text or "")
    if "<|" in text_str:
        for match in _SENSEVOICE_TAG_RE.finditer(text_str):
            tok = match.group(1).strip().upper()
            canon_ev = _EVENT_ALIASES.get(tok, tok)
            if tok in _EMOTION_LABELS_ZH:
                _consume_emotion(tok)
            elif canon_ev in _EVENT_LABELS_ZH:
                _consume_event(canon_ev)
            elif tok in _KNOWN_LANG_TAGS:
                _consume_lang(tok)

    prompt_block, summary_zh = format_vocal_cue_block(emotion=emotion_key, events=events_found)
    if not prompt_block:
        return None

    primary_event = events_found[0].lower() if events_found else ""
    return {
        "emotion": emotion_key.lower() if emotion_key else "",
        "emotion_zh": _EMOTION_LABELS_ZH.get(emotion_key, ""),
        "event": primary_event,
        "events": [e.lower() for e in events_found],
        "event_zh": "、".join(_EVENT_LABELS_ZH[e] for e in events_found),
        "lang": lang_key,
        "summary_zh": summary_zh,
        "prompt_block": prompt_block,
    }


def _evict_expired_locked(now_mono: float) -> None:
    stale = [sid for sid, item in _session_cues.items() if float(item.get("expires_at", 0.0)) <= now_mono]
    for sid in stale:
        _session_cues.pop(sid, None)


def is_vocal_cue_enabled(settings_path: Optional[str] = None) -> bool:
    """Return True only when SenseVoice vocal cue extraction is explicitly enabled (default False)."""
    try:
        return bool(runtime_settings.get_vocal_cue(settings_path).get("enabled", False))
    except Exception:
        return False


def record_vocal_cue(
    session_id: str,
    cue: Optional[Dict[str, Any]],
    *,
    source: str = "sherpa",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    publish: bool = True,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """Store a short-TTL one-shot vocal cue for `session_id`, or clear stale cue when disabled/neutral."""
    sid = str(session_id or "").strip()
    now_mono = time.monotonic()
    now_wall = time.time()

    if (not force) and (not is_vocal_cue_enabled()):
        if sid:
            with _lock:
                _session_cues.pop(sid, None)
                _evict_expired_locked(now_mono)
        return None

    if not cue or not isinstance(cue, dict) or not str(cue.get("prompt_block") or "").strip():
        if sid:
            with _lock:
                _session_cues.pop(sid, None)
                _evict_expired_locked(now_mono)
        return None

    ttl = max(1.0, min(120.0, float(ttl_seconds)))
    cue_copy = {
        "emotion": str(cue.get("emotion") or ""),
        "emotion_zh": str(cue.get("emotion_zh") or ""),
        "event": str(cue.get("event") or ""),
        "events": list(cue.get("events") or []),
        "event_zh": str(cue.get("event_zh") or ""),
        "lang": str(cue.get("lang") or ""),
        "summary_zh": str(cue.get("summary_zh") or ""),
        "prompt_block": str(cue.get("prompt_block") or ""),
    }
    record: Dict[str, Any] = {
        "session_id": sid,
        "source": str(source or "sherpa")[:32],
        "ts": now_wall,
        "time": time.strftime("%H:%M:%S", time.localtime(now_wall)),
        "expires_at": now_mono + ttl,
        **cue_copy,
    }

    with _lock:
        _evict_expired_locked(now_mono)
        if sid:
            _session_cues[sid] = record
            _session_cues.move_to_end(sid)
            while len(_session_cues) > MAX_CACHED_SESSIONS:
                _session_cues.popitem(last=False)
        _recent_cues.append(
            {
                "ts": record["ts"],
                "time": record["time"],
                "session_id": sid[:16] if sid else "",
                "source": record["source"],
                "emotion": record["emotion"],
                "emotion_zh": record["emotion_zh"],
                "event": record["event"],
                "events": list(record["events"]),
                "event_zh": record["event_zh"],
                "lang": record["lang"],
                "summary_zh": record["summary_zh"],
            }
        )

    if publish:
        try:
            runtime_events.publish_event(
                "user_vocal_cue",
                session_id=sid,
                source=record["source"],
                emotion=record["emotion"],
                event=record["event"],
                events=list(record["events"]),
                lang=record["lang"],
                summary_zh=record["summary_zh"],
            )
        except Exception:
            pass

    return dict(record)


def consume_vocal_cue(session_id: str, *, force: bool = False) -> str:
    """Atomically pop and return the `<user_vocal_cue>` prompt block for `session_id` if enabled and unexpired."""
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    now_mono = time.monotonic()
    with _lock:
        _evict_expired_locked(now_mono)
        entry = _session_cues.pop(sid, None)
    if (not force) and (not is_vocal_cue_enabled()):
        return ""
    if not entry:
        return ""
    if float(entry.get("expires_at", 0.0)) <= now_mono:
        return ""
    return str(entry.get("prompt_block") or "").strip()


def peek_vocal_cue(session_id: str) -> Optional[Dict[str, Any]]:
    """Read-only inspection of an active unexpired vocal cue for `session_id`."""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    now_mono = time.monotonic()
    with _lock:
        _evict_expired_locked(now_mono)
        entry = _session_cues.get(sid)
        if not entry or float(entry.get("expires_at", 0.0)) <= now_mono:
            return None
        return dict(entry)


def clear_vocal_cue(session_id: str) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _lock:
        _session_cues.pop(sid, None)


def get_recent_vocal_cues(limit: int = 10) -> List[Dict[str, Any]]:
    """Return recent vocal cue observations (newest first) for 18080 Console observability."""
    n = max(1, min(RECENT_CUES_MAXLEN, int(limit)))
    with _lock:
        items = list(_recent_cues)[-n:]
    items.reverse()
    return [dict(item) for item in items]


def get_latest_vocal_cue() -> Optional[Dict[str, Any]]:
    with _lock:
        if not _recent_cues:
            return None
        return dict(_recent_cues[-1])


def reset_vocal_cues() -> None:
    """Reset in-memory vocal cue state (for isolated unit tests)."""
    with _lock:
        _session_cues.clear()
        _recent_cues.clear()

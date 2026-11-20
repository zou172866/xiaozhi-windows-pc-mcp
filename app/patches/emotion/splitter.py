"""Streaming prosody clause splitter and ESP32 emotion push helpers (H-1)."""

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
from patches.emotion import tags

_STRONG_PUNCT = ("。", "！", "!", "？", "?", "；", ";", "\n")
_SOFT_PUNCT = ("，", ",", "：")
_ALL_CUT_PUNCT = _STRONG_PUNCT + _SOFT_PUNCT

FIRST_SENTENCE_MAX_STRONG_CHARS = 20
SUBSEQUENT_MAX_STRONG_CHARS = 38


def find_prosody_cut_pos(
    current_text: str, is_first_sentence: bool, quote_safe_cut_fn
) -> int:
    """Find smooth clause boundary without chopping 2-3 char openings at the first comma.

    P1-10: Tightens first-sentence strong-punctuation ceiling to ~20 spoken chars (vs 38 for
    later sentences) and allows soft comma split once >=14 spoken chars for faster first-audio.
    """
    if not current_text:
        return -1
    if tags.has_unclosed_leading_tag(current_text):
        return -1

    min_strong = 6 if is_first_sentence else 4
    min_soft = 14 if is_first_sentence else 28
    max_strong = FIRST_SENTENCE_MAX_STRONG_CHARS if is_first_sentence else SUBSEQUENT_MAX_STRONG_CHARS

    # Check if a strong sentence boundary within target first/breath length is already buffered
    first_strong_cut = -1
    for idx, ch in enumerate(current_text):
        if ch in _STRONG_PUNCT:
            safe = quote_safe_cut_fn(current_text, idx, _ALL_CUT_PUNCT)
            if safe != -1 and tags.count_spoken_chars(current_text[: safe + 1]) >= min_strong:
                first_strong_cut = safe
                break

    if first_strong_cut != -1 and tags.count_spoken_chars(current_text[: first_strong_cut + 1]) <= max_strong:
        return first_strong_cut

    # Otherwise allow soft comma/colon split once the clause is long enough (>=14 / >=28 spoken chars)
    for idx, ch in enumerate(current_text):
        if ch in _STRONG_PUNCT:
            safe = quote_safe_cut_fn(current_text, idx, _ALL_CUT_PUNCT)
            if safe != -1 and tags.count_spoken_chars(current_text[: safe + 1]) >= min_strong:
                return safe
        elif ch in _SOFT_PUNCT:
            safe = quote_safe_cut_fn(current_text, idx, _ALL_CUT_PUNCT)
            if safe != -1 and tags.count_spoken_chars(current_text[: safe + 1]) >= min_soft:
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

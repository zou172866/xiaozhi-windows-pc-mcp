"""Upstream agent-base-prompt.txt harmony anchor verification and soft replacement (H-1, C-2)."""

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

_PROMPT_REPLACEMENTS: Tuple[Tuple[str, str, str], ...] = (
    (
        "brackets_rule",
        'NEVER use brackets to express inner thoughts or actions (e.g. ”[无奈地叹气]”,”(笑着说)”).',
        'Start every spoken reply (including direct_answer.response) with ONE hidden English emotion tag from [calm], [gentle], [happy], [playful], [shy], [serious], [tender] (use [calm]/[gentle] for everyday chat, [happy] for good news or cheer, [playful] for teasing, [shy] for blushing, [serious] for focus, and reserve [tender] strictly for comforting sadness or distress; optional second tag only if mood genuinely shifts mid-reply). NEVER use stage-direction brackets like ”[无奈地叹气]” or ”(笑着说)”.',
    ),
    (
        "get_to_the_point",
        '1. [Get to the point] Never be verbose. The first sentence must directly address the topic — no unnecessary pleasantries or preamble.',
        '1. [Natural pacing] Be engaging, warm, and thorough. Follow the length and conversational pacing described in <identity> rather than cutting answers overly short.',
    ),
    (
        "slice_long_content",
        '- [Slice long content] For long-form content like storytelling or knowledge sharing, NEVER dump everything at once. Start with the core content directly, then naturally ask the user whether to continue at the end (e.g., tell an actual segment first, then ask ”要继续听吗？”). Start on demand, stop on demand, yield to interruptions.',
        '- [Rich storytelling & explanation] For storytelling, explanations, or thoughtful chat, give a full, vivid, satisfying multi-sentence segment instead of stopping after just one or two sentences; only pause to ask if the user wants to keep going after delivering a substantial part.',
    ),
    (
        "colloquial_zahuishi",
        '"咋回事"',
        '"怎么啦"',
    ),
)


_WARNED_MISSING_PROMPT_ANCHORS: set[str] = set()
_WARNED_ANCHORS_LOCK = threading.Lock()


def reset_prompt_anchor_warnings() -> None:
    """Clear one-shot missing-anchor warning cache (for isolated unit tests)."""
    with _WARNED_ANCHORS_LOCK:
        _WARNED_MISSING_PROMPT_ANCHORS.clear()


def verify_prompt_base_anchors(
    prompt_path: str = os.path.join(xz_common.ROOT_DIR, "agent-base-prompt.txt"),
) -> Tuple[bool, str]:
    """Verify upstream agent-base-prompt.txt still contains every anchor we .replace()."""
    if not os.path.isfile(prompt_path):
        return False, f"base prompt file missing: {prompt_path}"
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as exc:
        return False, f"failed to read base prompt: {exc}"
    missing = [name for name, anchor, _ in _PROMPT_REPLACEMENTS if anchor not in raw]
    if missing:
        return False, f"missing prompt anchor(s): {', '.join(missing)}"
    return True, f"verified {len(_PROMPT_REPLACEMENTS)} anchors"


def _apply_prompt_harmony_detailed(
    rendered: str, *, warn_once: bool = True
) -> Tuple[str, List[str], List[str]]:
    """Soft-apply prompt harmonization replacements, skipping missing anchors without raising (C-2)."""
    out = rendered
    applied: List[str] = []
    missing: List[str] = []
    for anchor_name, old_anchor, new_text in _PROMPT_REPLACEMENTS:
        if old_anchor in out:
            out = out.replace(old_anchor, new_text)
            applied.append(anchor_name)
        else:
            missing.append(anchor_name)
            if warn_once:
                should_warn = False
                with _WARNED_ANCHORS_LOCK:
                    if anchor_name not in _WARNED_MISSING_PROMPT_ANCHORS:
                        _WARNED_MISSING_PROMPT_ANCHORS.add(anchor_name)
                        should_warn = True
                if should_warn:
                    print(
                        f"[MeiEmotionRouter] WARNING: 上游提示词锚点缺失，已跳过该项替换 (C-2 软失败): {anchor_name}",
                        flush=True,
                    )
    return out, applied, missing


def dry_run_prompt_harmony(
    prompt_path: str = os.path.join(xz_common.ROOT_DIR, "agent-base-prompt.txt"),
) -> Tuple[str, str, List[str], List[str]]:
    """Run prompt harmony replacements against the real upstream template at startup (C-2)."""
    all_names = [name for name, _, _ in _PROMPT_REPLACEMENTS]
    if not os.path.isfile(prompt_path):
        return "failed", f"base prompt file missing: {prompt_path}", [], all_names
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as exc:
        return "failed", f"failed to read base prompt: {exc}", [], all_names

    _, applied, missing = _apply_prompt_harmony_detailed(raw, warn_once=True)
    total = len(_PROMPT_REPLACEMENTS)
    if not missing:
        return "ok", f"verified {len(applied)}/{total} anchors", applied, []
    if applied:
        return (
            "partial",
            f"applied {len(applied)}/{total} anchors; missing: {', '.join(missing)}",
            applied,
            missing,
        )
    return (
        "partial",
        f"0/{total} anchors matched; missing: {', '.join(missing)}",
        [],
        missing,
    )


def apply_prompt_harmony_replacements(rendered: str, *, record_registry: bool = True) -> str:
    """Soft-apply prompt harmonization replacements without ever crashing live turns on anchor drift (C-2)."""
    out, applied, missing = _apply_prompt_harmony_detailed(rendered, warn_once=True)
    if record_registry and missing:
        total = len(_PROMPT_REPLACEMENTS)
        patch_registry.record_patch(
            "mei_prompt_anchors",
            "partial",
            target="agent-base-prompt.txt",
            detail=f"runtime applied {len(applied)}/{total}; missing: {', '.join(missing)}",
            contract_ok=False,
            category="experience",
        )
    return out

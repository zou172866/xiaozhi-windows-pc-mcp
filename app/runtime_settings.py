"""Unified schema-versioned runtime settings store (P2-21).

Consolidates the 3 container-accessible runtime JSON files:
  - data/pc_control_mode.json     -> pc_control.mode
  - data/audio_output_mode.json   -> audio_output.{target, volume}
  - data/reply_length_config.json -> reply_length.{preset, chat_chars, story_chars}
into a single atomic, mtime-cached `data/runtime_settings.json` (`schema_version: 1`).

Security boundary (P0-2): host-private `pc-mcp/state/l3_backend_config.json` is NEVER
merged into container-mounted `./data/runtime_settings.json`.

First-boot migration: when `runtime_settings.json` does not yet exist, any present legacy
JSON files are losslessly merged into `runtime_settings.json` and renamed to `*.migrated.bak`.
If `runtime_settings.json` is absent and migration cannot write, reads gracefully fall back to
any legacy files on disk.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Optional

import xz_common

STATE_DIR = xz_common.STATE_DIR
DATA_DIR = STATE_DIR
RUNTIME_SETTINGS_PATH = os.path.join(STATE_DIR, "runtime_settings.json")
SCHEMA_VERSION = 1

VALID_PC_MODES = frozenset({"off", "safe", "full"})
VALID_AUDIO_TARGETS = frozenset({"esp32", "pc"})
VALID_IDLE_FAREWELL_MODES = frozenset({"fixed", "llm_summary"})
VALID_TURN_CONTEXT_MODES = frozenset({"split", "legacy"})

REPLY_LEN_PRESETS: Dict[str, Dict[str, int]] = {
    "concise": {"chat_chars": 60, "story_chars": 200},
    "balanced": {"chat_chars": 150, "story_chars": 450},
    "chatty": {"chat_chars": 260, "story_chars": 750},
    "storyteller": {"chat_chars": 200, "story_chars": 1000},
}

DEFAULT_IDLE_FAREWELL_MESSAGES: tuple[str, ...] = (
    "先去忙吧，有事随时叫我。",
    "那我先安静待在一旁啦，需要的时候再叫我哦。",
)

DEFAULT_IDLE_FAREWELL_SUMMARY_PROMPT: str = (
    "现在对方有一小会儿没说话，对话即将暂时结束。"
    "请以角色本人的口吻，先用半句话自然、简短地呼应总结一下刚才我们聊的内容，"
    "再真诚温柔地表达一点依恋和不舍，提醒对方有空或想聊天时随时再叫自己。"
    "严格控制在 35 到 65 字以内（1～2 句口语），直接说出告别的话，"
    "不要以“时间过得真快”开头，不要客套寒暄，不要输出 Markdown、括号动作或表情符号。"
)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "pc_control": {"mode": "safe"},
    "audio_output": {"target": "esp32", "volume": 1.0},
    "reply_length": {"preset": "balanced", "chat_chars": 150, "story_chars": 450},
    "idle_farewell": {
        "enabled": True,
        "mode": "fixed",
        "timeout_seconds": 120,
        "summary_prompt": DEFAULT_IDLE_FAREWELL_SUMMARY_PROMPT,
        "messages": list(DEFAULT_IDLE_FAREWELL_MESSAGES),
    },
    "turn_context": {"mode": "split"},
}

_settings_lock = threading.RLock()
_caches_lock = threading.Lock()
_caches: Dict[str, xz_common.MtimeJsonCache] = {}


def _clamp_int(val: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(round(float(val)))
    except Exception:
        return default
    return max(lo, min(hi, n))


def _infer_reply_preset(chat_chars: int, story_chars: int) -> str:
    for name, p in REPLY_LEN_PRESETS.items():
        if p["chat_chars"] == chat_chars and p["story_chars"] == story_chars:
            return name
    return "custom"


def normalize_pc_control(raw: Any) -> Dict[str, str]:
    d = raw if isinstance(raw, dict) else {}
    mode = str(d.get("mode") or "safe").strip().lower()
    if mode not in VALID_PC_MODES:
        mode = "safe"
    return {"mode": mode}


def normalize_audio_output(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    target = str(d.get("target") or "esp32").strip().lower()
    if target not in VALID_AUDIO_TARGETS:
        target = "esp32"
    try:
        vol = float(d.get("volume", 1.0))
    except Exception:
        vol = 1.0
    vol = round(max(0.2, min(1.5, vol)), 2)
    return {"target": target, "volume": vol}


def normalize_reply_length(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    preset_req = str(d.get("preset") or "").strip().lower()
    if preset_req in REPLY_LEN_PRESETS and "chat_chars" not in d and "story_chars" not in d:
        chat_chars = REPLY_LEN_PRESETS[preset_req]["chat_chars"]
        story_chars = REPLY_LEN_PRESETS[preset_req]["story_chars"]
    else:
        chat_chars = _clamp_int(d.get("chat_chars", 150), 30, 600, 150)
        story_chars = _clamp_int(d.get("story_chars", 450), 100, 1500, 450)
    return {
        "preset": _infer_reply_preset(chat_chars, story_chars),
        "chat_chars": chat_chars,
        "story_chars": story_chars,
    }


def normalize_idle_farewell(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    raw_enabled = d.get("enabled", True)
    enabled = raw_enabled if isinstance(raw_enabled, bool) else str(raw_enabled).strip().lower() not in ("0", "false", "no", "off")
    mode = str(d.get("mode") or "fixed").strip().lower()
    if mode not in VALID_IDLE_FAREWELL_MODES:
        mode = "fixed"
    timeout_seconds = _clamp_int(d.get("timeout_seconds", 120), 30, 1800, 120)

    raw_sp = d.get("summary_prompt")
    if isinstance(raw_sp, str) and raw_sp.strip():
        summary_prompt = raw_sp.strip()[:500]
    else:
        summary_prompt = DEFAULT_IDLE_FAREWELL_SUMMARY_PROMPT

    if "messages" not in d:
        messages = list(DEFAULT_IDLE_FAREWELL_MESSAGES)
    else:
        raw_msgs = d.get("messages")
        if isinstance(raw_msgs, str):
            raw_items = raw_msgs.splitlines()
        elif isinstance(raw_msgs, (list, tuple)):
            raw_items = raw_msgs
        else:
            raw_items = []
        messages = []
        seen: set[str] = set()
        for item in raw_items:
            s = str(item or "").strip()
            if not s:
                continue
            if len(s) > 120:
                s = s[:120].rstrip()
            if s and s not in seen:
                seen.add(s)
                messages.append(s)
            if len(messages) >= 20:
                break

    return {
        "enabled": bool(enabled),
        "mode": mode,
        "timeout_seconds": timeout_seconds,
        "summary_prompt": summary_prompt,
        "messages": messages,
    }


def normalize_turn_context(raw: Any) -> Dict[str, str]:
    d = raw if isinstance(raw, dict) else {}
    mode = str(d.get("mode") or "split").strip().lower()
    if mode not in VALID_TURN_CONTEXT_MODES:
        mode = "split"
    return {"mode": mode}


def normalize_runtime_settings(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "pc_control": normalize_pc_control(d.get("pc_control")),
        "audio_output": normalize_audio_output(d.get("audio_output")),
        "reply_length": normalize_reply_length(d.get("reply_length")),
        "idle_farewell": normalize_idle_farewell(d.get("idle_farewell")),
        "turn_context": normalize_turn_context(d.get("turn_context")),
    }


def _get_cache(path: str) -> xz_common.MtimeJsonCache:
    norm = os.path.abspath(path)
    with _caches_lock:
        c = _caches.get(norm)
        if c is None:
            c = xz_common.MtimeJsonCache(
                normalizer=normalize_runtime_settings,
                default_factory=lambda: normalize_runtime_settings(DEFAULT_SETTINGS),
            )
            _caches[norm] = c
        return c


def _legacy_paths(settings_path: str) -> Dict[str, str]:
    base_dir = os.path.dirname(os.path.abspath(settings_path))
    return {
        "pc_control": os.path.join(base_dir, "pc_control_mode.json"),
        "audio_output": os.path.join(base_dir, "audio_output_mode.json"),
        "reply_length": os.path.join(base_dir, "reply_length_config.json"),
    }


def _read_legacy_dict(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _read_merged_legacy_files(settings_path: str) -> tuple[Dict[str, Any], list[str]]:
    merged: Dict[str, Any] = normalize_runtime_settings(DEFAULT_SETTINGS)
    found_files: list[str] = []
    legacy = _legacy_paths(settings_path)

    pc_raw = _read_legacy_dict(legacy["pc_control"])
    if pc_raw is not None:
        merged["pc_control"] = normalize_pc_control(pc_raw)
        found_files.append(legacy["pc_control"])
    elif os.path.isfile(legacy["pc_control"]):
        found_files.append(legacy["pc_control"])

    aud_raw = _read_legacy_dict(legacy["audio_output"])
    if aud_raw is not None:
        merged["audio_output"] = normalize_audio_output(aud_raw)
        found_files.append(legacy["audio_output"])
    elif os.path.isfile(legacy["audio_output"]):
        found_files.append(legacy["audio_output"])

    rl_raw = _read_legacy_dict(legacy["reply_length"])
    if rl_raw is not None:
        merged["reply_length"] = normalize_reply_length(rl_raw)
        found_files.append(legacy["reply_length"])
    elif os.path.isfile(legacy["reply_length"]):
        found_files.append(legacy["reply_length"])

    return merged, found_files


def _archive_legacy_files(files: list[str]) -> list[str]:
    archived: list[str] = []
    for src in files:
        if not os.path.isfile(src):
            continue
        dst = f"{src}.migrated.bak"
        try:
            os.replace(src, dst)
            archived.append(dst)
        except OSError:
            pass
    return archived


def migrate_legacy_runtime_settings(settings_path: Optional[str] = None) -> Dict[str, Any]:
    """Losslessly migrate legacy 3 JSON files into runtime_settings.json and archive them as *.migrated.bak."""
    path = os.path.abspath(settings_path or RUNTIME_SETTINGS_PATH)
    with _settings_lock:
        if os.path.isfile(path):
            return _get_cache(path).get(path)

        merged, found_files = _read_merged_legacy_files(path)
        if not found_files:
            return merged

        try:
            xz_common.atomic_write_json(path, merged)
            _get_cache(path).set(path, merged)
            archived = _archive_legacy_files(found_files)
            names = ", ".join(os.path.basename(p) for p in archived) or ", ".join(
                os.path.basename(p) for p in found_files
            )
            print(
                f"[RuntimeSettings] 已无损迁移旧配置至 {os.path.basename(path)}，旧文件已归档: {names}",
                flush=True,
            )
        except Exception as exc:
            print(f"[RuntimeSettings] 自动迁移写盘跳过（继续内存降级读取）: {exc}", flush=True)
        return merged


def read_runtime_settings(
    settings_path: Optional[str] = None,
    *,
    auto_migrate: bool = True,
) -> Dict[str, Any]:
    """Read normalized runtime_settings.json with sub-microsecond mtime caching."""
    path = os.path.abspath(settings_path or RUNTIME_SETTINGS_PATH)
    if os.path.isfile(path):
        return _get_cache(path).get(path)
    if auto_migrate:
        return migrate_legacy_runtime_settings(path)
    merged, _ = _read_merged_legacy_files(path)
    return merged


def update_runtime_section(
    section: str,
    updater,
    *,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically read-modify-write one top-level section of runtime_settings.json."""
    path = os.path.abspath(settings_path or RUNTIME_SETTINGS_PATH)
    with _settings_lock:
        current = read_runtime_settings(path, auto_migrate=True)
        updated_section = updater(dict(current.get(section) or {}))
        current[section] = updated_section
        normalized = normalize_runtime_settings(current)
        xz_common.atomic_write_json(path, normalized)
        _get_cache(path).set(path, normalized)
        # If legacy files somehow remained alongside a brand-new write, archive them so single-source holds
        _, leftover = _read_merged_legacy_files(path)
        if leftover:
            _archive_legacy_files(leftover)
        return normalized


def get_pc_mode(settings_path: Optional[str] = None) -> str:
    return read_runtime_settings(settings_path)["pc_control"]["mode"]


def set_pc_mode(mode: str, settings_path: Optional[str] = None) -> str:
    m = str(mode or "safe").strip().lower()
    if m not in VALID_PC_MODES:
        raise ValueError(f"Unsupported pc_control mode: {mode}")
    saved = update_runtime_section("pc_control", lambda _: {"mode": m}, settings_path=settings_path)
    return saved["pc_control"]["mode"]


def get_audio_output(settings_path: Optional[str] = None) -> Dict[str, Any]:
    return dict(read_runtime_settings(settings_path)["audio_output"])


def set_audio_output(
    target: Optional[str] = None,
    volume: Optional[float] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    def _upd(cur: dict) -> dict:
        t = str(cur.get("target", "esp32") if target is None else target).strip().lower()
        if t not in VALID_AUDIO_TARGETS:
            raise ValueError(f"Unsupported audio output target: {target}")
        vol = cur.get("volume", 1.0) if volume is None else round(max(0.2, min(1.5, float(volume))), 2)
        return {"target": t, "volume": vol}

    saved = update_runtime_section("audio_output", _upd, settings_path=settings_path)
    return dict(saved["audio_output"])


def get_reply_length(settings_path: Optional[str] = None) -> Dict[str, Any]:
    return dict(read_runtime_settings(settings_path)["reply_length"])


def set_reply_length(
    chat_chars: Optional[int] = None,
    story_chars: Optional[int] = None,
    preset: Optional[str] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    def _upd(cur: dict) -> dict:
        p_key = str(preset or "").strip().lower()
        if p_key in REPLY_LEN_PRESETS and chat_chars is None and story_chars is None:
            return normalize_reply_length({"preset": p_key})
        return normalize_reply_length(
            {
                "chat_chars": cur.get("chat_chars", 150) if chat_chars is None else chat_chars,
                "story_chars": cur.get("story_chars", 450) if story_chars is None else story_chars,
            }
        )

    saved = update_runtime_section("reply_length", _upd, settings_path=settings_path)
    return dict(saved["reply_length"])


def get_idle_farewell(settings_path: Optional[str] = None) -> Dict[str, Any]:
    sec = read_runtime_settings(settings_path)["idle_farewell"]
    return {
        "enabled": bool(sec["enabled"]),
        "mode": str(sec["mode"]),
        "timeout_seconds": int(sec["timeout_seconds"]),
        "summary_prompt": str(sec["summary_prompt"]),
        "messages": list(sec["messages"]),
    }


def set_idle_farewell(
    enabled: Optional[bool] = None,
    timeout_seconds: Optional[int] = None,
    messages: Any = None,
    mode: Optional[str] = None,
    summary_prompt: Optional[str] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if enabled is not None:
            next_cfg["enabled"] = bool(enabled)
        if mode is not None:
            m = str(mode).strip().lower()
            if m not in VALID_IDLE_FAREWELL_MODES:
                raise ValueError(f"Unsupported idle_farewell mode: {mode}")
            next_cfg["mode"] = m
        if timeout_seconds is not None:
            next_cfg["timeout_seconds"] = timeout_seconds
        if summary_prompt is not None:
            next_cfg["summary_prompt"] = str(summary_prompt)
        if messages is not None:
            next_cfg["messages"] = messages
        return normalize_idle_farewell(next_cfg)

    saved = update_runtime_section("idle_farewell", _upd, settings_path=settings_path)
    sec = saved["idle_farewell"]
    return {
        "enabled": bool(sec["enabled"]),
        "mode": str(sec["mode"]),
        "timeout_seconds": int(sec["timeout_seconds"]),
        "summary_prompt": str(sec["summary_prompt"]),
        "messages": list(sec["messages"]),
    }


def get_turn_context_mode(settings_path: Optional[str] = None) -> str:
    return read_runtime_settings(settings_path)["turn_context"]["mode"]


def set_turn_context_mode(mode: str, settings_path: Optional[str] = None) -> str:
    m = str(mode or "split").strip().lower()
    if m not in VALID_TURN_CONTEXT_MODES:
        raise ValueError(f"Unsupported turn_context mode: {mode}")
    saved = update_runtime_section("turn_context", lambda _: {"mode": m}, settings_path=settings_path)
    return saved["turn_context"]["mode"]

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
import re
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
VALID_PROACTIVE_DELIVERY_MODES = frozenset({"dry_run", "pc_speaker", "mobile_push", "pc_and_mobile"})
VALID_VOICEPRINT_POLICIES = frozenset({"observe", "isolate_memory", "full_guard"})
VALID_MOBILE_PUSH_PROVIDERS = frozenset({"bark", "feishu", "wecom", "pushplus", "serverchan"})
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

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

DEFAULT_VAD_SPECULATIVE_COMPLETE_SUFFIXES: tuple[str, ...] = (
    "吗", "呢", "吧", "了", "啦", "呀", "哦", "嘛", "呗", "哈", "么",
    "好啊", "行啊", "对啊", "是啊", "可以啊", "真好啊",
    "好不好", "行不行", "可以吗", "对不对", "是不是", "有没有", "行吗", "好吗",
    "怎么样", "怎么了", "怎么办", "什么意思", "多少钱", "几点了", "几号",
    "星期几", "周几", "在哪里", "在哪儿", "在哪", "是谁", "为什么",
    "谢谢", "谢谢你", "再见", "拜拜", "晚安", "早安", "辛苦了",
    "算了", "不用了", "没关系", "没问题",
)

DEFAULT_VAD_SPECULATIVE_HESITATION_SUFFIXES: tuple[str, ...] = (
    "然后", "就是", "那个", "这个", "因为", "所以", "但是", "不过", "而且",
    "并且", "或者", "还是", "如果", "要是", "虽然", "尽管", "哪怕", "其实",
    "比如", "比如说", "好像", "大概", "可能", "应该", "还有", "另外", "顺便",
    "首先", "其次", "接着", "同时", "否则", "不然", "除非", "既然", "只要",
    "只有", "无论", "不管", "甚至", "尤其", "特别是", "也就是说", "怎么说呢",
    "就是说", "那个啥", "的时候", "以后", "之后", "之前", "的话", "方面",
    "我想", "我要", "我想问", "我想让你", "帮我", "给我", "请你", "能不能",
    "可不可以", "准备", "打算", "正在", "觉得", "感觉", "关于", "对于",
    "查一下", "看一下", "看看", "搜一下", "搜索", "找一下", "发给", "定一个",
    "嗯", "呃", "额", "诶", "唉", "哎", "啊",
    "那", "就", "和", "跟", "与", "把", "被", "给", "让", "对", "向", "从",
    "在", "到", "的", "地", "得", "着", "过",
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
    "vad_speculative": {
        "enabled": True,
        "speculative_ms": 550,
        "post_wakeup_ms": 800,
        "min_chars": 4,
        "complete_suffixes": list(DEFAULT_VAD_SPECULATIVE_COMPLETE_SUFFIXES),
        "hesitation_suffixes": list(DEFAULT_VAD_SPECULATIVE_HESITATION_SUFFIXES),
    },
    "recall_style": {
        "sensitive_filter_enabled": True,
        "silent_memory_enabled": True,
        "anti_ai_flavor_enabled": True,
    },
    "proactive": {
        "enabled": False,
        "delivery_mode": "dry_run",
        "check_interval_minutes": 15,
        "quiet_start": "23:00",
        "quiet_end": "08:00",
        "min_since_last_chat_minutes": 45,
        "min_cooldown_minutes": 90,
        "max_daily_nudges": 4,
        "pc_speaker_volume": 0.75,
        "allow_idle_chitchat": True,
        "allow_l3_finish_notify": True,
        "allow_open_loop_followup": True,
        "inner_life_enabled": False,
        "allow_taskbar_inbox": False,
    },
    "vocal_cue": {
        "enabled": False,
    },
    "voiceprint": {
        "enabled": False,
        "policy": "full_guard",
        "threshold": 0.58,
    },
    "screen_peek": {
        "enabled": False,
    },
    "mobile_push": {
        "enabled": False,
        "provider": "bark",
        "forward_proactive": True,
        "forward_briefing": False,
        "respect_quiet_hours": True,
        "max_daily_pushes": 12,
    },
    "llm_fallback": {
        "enabled": False,
        "cooldown_seconds": 60,
        "nodes": [],
    },
    "qq_bot": {
        "enabled": False,
        "sandbox": False,
        "share_primary_memory": True,
        "memory_device_id": "",
        "allow_l3_dispatch": False,
        "owner_openid": "",
        "allowed_openids": [],
        "allowed_group_openids": [],
        "batch_quiet_ms": 3500,
    },
    "skill_loader": {
        "enabled": True,
    },
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


def _normalize_word_list(raw: Any, default_items: tuple[str, ...], *, max_items: int, max_len: int = 24) -> list[str]:
    if raw is None:
        return list(default_items)
    if isinstance(raw, str):
        parts = [p for ln in raw.splitlines() for p in ln.replace("，", ",").replace("、", ",").split(",")]
    elif isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        return list(default_items)

    out: list[str] = []
    seen: set[str] = set()
    for item in parts:
        s = str(item or "").strip()
        if not s:
            continue
        if len(s) > max_len:
            s = s[:max_len].rstrip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def normalize_vad_speculative(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    raw_enabled = d.get("enabled", True)
    enabled = (
        raw_enabled
        if isinstance(raw_enabled, bool)
        else str(raw_enabled).strip().lower() not in ("0", "false", "no", "off")
    )
    spec_ms = _clamp_int(d.get("speculative_ms", 550), 300, 950, 550)
    post_wakeup_ms = _clamp_int(d.get("post_wakeup_ms", 800), 450, 1050, 800)
    if post_wakeup_ms < spec_ms:
        post_wakeup_ms = spec_ms
    min_chars = _clamp_int(d.get("min_chars", 4), 2, 20, 4)

    if "complete_suffixes" not in d:
        complete_suffixes = list(DEFAULT_VAD_SPECULATIVE_COMPLETE_SUFFIXES)
    else:
        complete_suffixes = _normalize_word_list(
            d.get("complete_suffixes"),
            DEFAULT_VAD_SPECULATIVE_COMPLETE_SUFFIXES,
            max_items=120,
        )

    if "hesitation_suffixes" not in d:
        hesitation_suffixes = list(DEFAULT_VAD_SPECULATIVE_HESITATION_SUFFIXES)
    else:
        hesitation_suffixes = _normalize_word_list(
            d.get("hesitation_suffixes"),
            DEFAULT_VAD_SPECULATIVE_HESITATION_SUFFIXES,
            max_items=160,
        )

    return {
        "enabled": bool(enabled),
        "speculative_ms": int(spec_ms),
        "post_wakeup_ms": int(post_wakeup_ms),
        "min_chars": int(min_chars),
        "complete_suffixes": complete_suffixes,
        "hesitation_suffixes": hesitation_suffixes,
    }


def _coerce_bool(val: Any, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def normalize_recall_style(raw: Any) -> Dict[str, bool]:
    d = raw if isinstance(raw, dict) else {}
    return {
        "sensitive_filter_enabled": _coerce_bool(d.get("sensitive_filter_enabled"), True),
        "silent_memory_enabled": _coerce_bool(d.get("silent_memory_enabled"), True),
        "anti_ai_flavor_enabled": _coerce_bool(d.get("anti_ai_flavor_enabled"), True),
    }


def _normalize_hhmm(raw: Any, default: str) -> str:
    s = str(raw or "").strip()
    if _HHMM_RE.match(s):
        return s
    return default


def normalize_proactive(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    mode = str(d.get("delivery_mode") or "dry_run").strip().lower()
    if mode not in VALID_PROACTIVE_DELIVERY_MODES:
        mode = "dry_run"
    try:
        vol = float(d.get("pc_speaker_volume", 0.75))
    except Exception:
        vol = 0.75
    if vol != vol:  # NaN guard
        vol = 0.75
    vol = round(max(0.2, min(1.2, vol)), 2)
    return {
        "enabled": _coerce_bool(d.get("enabled"), False),
        "delivery_mode": mode,
        "check_interval_minutes": _clamp_int(d.get("check_interval_minutes", 15), 5, 120, 15),
        "quiet_start": _normalize_hhmm(d.get("quiet_start"), "23:00"),
        "quiet_end": _normalize_hhmm(d.get("quiet_end"), "08:00"),
        "min_since_last_chat_minutes": _clamp_int(d.get("min_since_last_chat_minutes", 45), 5, 720, 45),
        "min_cooldown_minutes": _clamp_int(d.get("min_cooldown_minutes", 90), 15, 720, 90),
        "max_daily_nudges": _clamp_int(d.get("max_daily_nudges", 4), 1, 12, 4),
        "pc_speaker_volume": vol,
        "allow_idle_chitchat": _coerce_bool(d.get("allow_idle_chitchat"), True),
        "allow_l3_finish_notify": _coerce_bool(d.get("allow_l3_finish_notify"), True),
        "allow_open_loop_followup": _coerce_bool(d.get("allow_open_loop_followup"), True),
        "inner_life_enabled": _coerce_bool(d.get("inner_life_enabled"), False),
        "allow_taskbar_inbox": _coerce_bool(d.get("allow_taskbar_inbox"), False),
    }


def normalize_vocal_cue(raw: Any) -> Dict[str, bool]:
    d = raw if isinstance(raw, dict) else {}
    return {
        "enabled": _coerce_bool(d.get("enabled"), False),
    }


def normalize_voiceprint(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    policy = str(d.get("policy") or "full_guard").strip().lower()
    if policy not in VALID_VOICEPRINT_POLICIES:
        policy = "full_guard"
    try:
        thr = float(d.get("threshold", 0.58))
    except Exception:
        thr = 0.58
    if thr != thr:  # NaN guard
        thr = 0.58
    thr = round(max(0.30, min(0.90, thr)), 2)
    return {
        "enabled": _coerce_bool(d.get("enabled"), False),
        "policy": policy,
        "threshold": thr,
    }


def normalize_screen_peek(raw: Any) -> Dict[str, bool]:
    d = raw if isinstance(raw, dict) else {}
    return {
        "enabled": _coerce_bool(d.get("enabled"), False),
    }


def normalize_mobile_push(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    prov = str(d.get("provider") or "bark").strip().lower()
    if prov not in VALID_MOBILE_PUSH_PROVIDERS:
        prov = "bark"
    return {
        "enabled": _coerce_bool(d.get("enabled"), False),
        "provider": prov,
        "forward_proactive": _coerce_bool(d.get("forward_proactive"), True),
        "forward_briefing": _coerce_bool(d.get("forward_briefing"), False),
        "respect_quiet_hours": _coerce_bool(d.get("respect_quiet_hours"), True),
        "max_daily_pushes": _clamp_int(d.get("max_daily_pushes", 12), 1, 50, 12),
    }


def normalize_llm_fallback(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    raw_nodes = d.get("nodes") if isinstance(d.get("nodes"), list) else []
    nodes: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_nodes[:6]:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("id") or "").strip()[:32]
        url = str(item.get("base_url") or "").strip().rstrip("/")[:256]
        mdl = str(item.get("model") or "").strip()[:120]
        if not nid or nid == "primary" or nid in seen or not url or not mdl:
            continue
        seen.add(nid)
        nodes.append(
            {
                "id": nid,
                "label": str(item.get("label") or nid).strip()[:48] or nid,
                "base_url": url,
                "model": mdl,
                "api_key_env": str(item.get("api_key_env") or "").strip()[:64],
                "enabled": _coerce_bool(item.get("enabled"), True),
            }
        )
    return {
        "enabled": _coerce_bool(d.get("enabled"), False),
        "cooldown_seconds": _clamp_int(d.get("cooldown_seconds", 60), 10, 600, 60),
        "nodes": nodes,
    }


def normalize_qq_bot(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}

    def _clean_ids(val: Any) -> list[str]:
        if not isinstance(val, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in val[:32]:
            s = str(item or "").strip()[:128]
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    return {
        "enabled": _coerce_bool(d.get("enabled"), False),
        "sandbox": _coerce_bool(d.get("sandbox"), False),
        "share_primary_memory": _coerce_bool(d.get("share_primary_memory"), True),
        "memory_device_id": str(d.get("memory_device_id") or "").strip()[:64],
        "allow_l3_dispatch": _coerce_bool(d.get("allow_l3_dispatch"), False),
        "owner_openid": str(d.get("owner_openid") or "").strip()[:128],
        "allowed_openids": _clean_ids(d.get("allowed_openids")),
        "allowed_group_openids": _clean_ids(d.get("allowed_group_openids")),
        "batch_quiet_ms": _clamp_int(d.get("batch_quiet_ms", 3500), 0, 15000, 3500),
    }


def normalize_skill_loader(raw: Any) -> Dict[str, bool]:
    d = raw if isinstance(raw, dict) else {}
    return {"enabled": _coerce_bool(d.get("enabled"), True)}


def normalize_runtime_settings(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "pc_control": normalize_pc_control(d.get("pc_control")),
        "audio_output": normalize_audio_output(d.get("audio_output")),
        "reply_length": normalize_reply_length(d.get("reply_length")),
        "idle_farewell": normalize_idle_farewell(d.get("idle_farewell")),
        "turn_context": normalize_turn_context(d.get("turn_context")),
        "vad_speculative": normalize_vad_speculative(d.get("vad_speculative")),
        "recall_style": normalize_recall_style(d.get("recall_style")),
        "proactive": normalize_proactive(d.get("proactive")),
        "vocal_cue": normalize_vocal_cue(d.get("vocal_cue")),
        "voiceprint": normalize_voiceprint(d.get("voiceprint")),
        "screen_peek": normalize_screen_peek(d.get("screen_peek")),
        "mobile_push": normalize_mobile_push(d.get("mobile_push")),
        "llm_fallback": normalize_llm_fallback(d.get("llm_fallback")),
        "qq_bot": normalize_qq_bot(d.get("qq_bot")),
        "skill_loader": normalize_skill_loader(d.get("skill_loader")),
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


def get_vad_speculative(settings_path: Optional[str] = None) -> Dict[str, Any]:
    sec = read_runtime_settings(settings_path)["vad_speculative"]
    return {
        "enabled": bool(sec["enabled"]),
        "speculative_ms": int(sec["speculative_ms"]),
        "post_wakeup_ms": int(sec["post_wakeup_ms"]),
        "min_chars": int(sec["min_chars"]),
        "complete_suffixes": list(sec["complete_suffixes"]),
        "hesitation_suffixes": list(sec["hesitation_suffixes"]),
    }


def _require_int_param(name: str, val: Any) -> int:
    if isinstance(val, bool):
        raise ValueError(f"{name} must be an integer, not bool")
    try:
        return int(round(float(val)))
    except Exception as exc:
        raise ValueError(f"{name} must be an integer: {val!r}") from exc


def set_vad_speculative(
    enabled: Optional[bool] = None,
    speculative_ms: Optional[int] = None,
    post_wakeup_ms: Optional[int] = None,
    min_chars: Optional[int] = None,
    complete_suffixes: Any = None,
    hesitation_suffixes: Any = None,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(f"enabled must be a boolean, got {type(enabled).__name__}")
    spec_val = _require_int_param("speculative_ms", speculative_ms) if speculative_ms is not None else None
    post_val = _require_int_param("post_wakeup_ms", post_wakeup_ms) if post_wakeup_ms is not None else None
    min_c_val = _require_int_param("min_chars", min_chars) if min_chars is not None else None
    if complete_suffixes is not None and not isinstance(complete_suffixes, (list, tuple, set, str)):
        raise ValueError("complete_suffixes must be a list of strings or delimited string")
    if hesitation_suffixes is not None and not isinstance(hesitation_suffixes, (list, tuple, set, str)):
        raise ValueError("hesitation_suffixes must be a list of strings or delimited string")

    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if enabled is not None:
            next_cfg["enabled"] = bool(enabled)
        if spec_val is not None:
            next_cfg["speculative_ms"] = spec_val
        if post_val is not None:
            next_cfg["post_wakeup_ms"] = post_val
        if min_c_val is not None:
            next_cfg["min_chars"] = min_c_val
        if complete_suffixes is not None:
            next_cfg["complete_suffixes"] = complete_suffixes
        if hesitation_suffixes is not None:
            next_cfg["hesitation_suffixes"] = hesitation_suffixes
        return normalize_vad_speculative(next_cfg)

    saved = update_runtime_section("vad_speculative", _upd, settings_path=settings_path)
    sec = saved["vad_speculative"]
    return {
        "enabled": bool(sec["enabled"]),
        "speculative_ms": int(sec["speculative_ms"]),
        "post_wakeup_ms": int(sec["post_wakeup_ms"]),
        "min_chars": int(sec["min_chars"]),
        "complete_suffixes": list(sec["complete_suffixes"]),
        "hesitation_suffixes": list(sec["hesitation_suffixes"]),
    }


def get_recall_style(settings_path: Optional[str] = None) -> Dict[str, bool]:
    sec = read_runtime_settings(settings_path)["recall_style"]
    return {
        "sensitive_filter_enabled": bool(sec["sensitive_filter_enabled"]),
        "silent_memory_enabled": bool(sec["silent_memory_enabled"]),
        "anti_ai_flavor_enabled": bool(sec["anti_ai_flavor_enabled"]),
    }


def set_recall_style(
    *,
    sensitive_filter_enabled: Optional[bool] = None,
    silent_memory_enabled: Optional[bool] = None,
    anti_ai_flavor_enabled: Optional[bool] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, bool]:
    for fname, fval in (
        ("sensitive_filter_enabled", sensitive_filter_enabled),
        ("silent_memory_enabled", silent_memory_enabled),
        ("anti_ai_flavor_enabled", anti_ai_flavor_enabled),
    ):
        if fval is not None and not isinstance(fval, bool):
            raise ValueError(f"{fname} must be a boolean, got {type(fval).__name__}")

    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if sensitive_filter_enabled is not None:
            next_cfg["sensitive_filter_enabled"] = bool(sensitive_filter_enabled)
        if silent_memory_enabled is not None:
            next_cfg["silent_memory_enabled"] = bool(silent_memory_enabled)
        if anti_ai_flavor_enabled is not None:
            next_cfg["anti_ai_flavor_enabled"] = bool(anti_ai_flavor_enabled)
        return normalize_recall_style(next_cfg)

    saved = update_runtime_section("recall_style", _upd, settings_path=settings_path)
    sec = saved["recall_style"]
    return {
        "sensitive_filter_enabled": bool(sec["sensitive_filter_enabled"]),
        "silent_memory_enabled": bool(sec["silent_memory_enabled"]),
        "anti_ai_flavor_enabled": bool(sec["anti_ai_flavor_enabled"]),
    }


def get_proactive(settings_path: Optional[str] = None) -> Dict[str, Any]:
    return dict(read_runtime_settings(settings_path)["proactive"])


def set_proactive(
    *,
    enabled: Optional[bool] = None,
    delivery_mode: Optional[str] = None,
    check_interval_minutes: Optional[int] = None,
    quiet_start: Optional[str] = None,
    quiet_end: Optional[str] = None,
    min_since_last_chat_minutes: Optional[int] = None,
    min_cooldown_minutes: Optional[int] = None,
    max_daily_nudges: Optional[int] = None,
    pc_speaker_volume: Optional[float] = None,
    allow_idle_chitchat: Optional[bool] = None,
    allow_l3_finish_notify: Optional[bool] = None,
    allow_open_loop_followup: Optional[bool] = None,
    inner_life_enabled: Optional[bool] = None,
    allow_taskbar_inbox: Optional[bool] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    for bname, bval in (
        ("enabled", enabled),
        ("allow_idle_chitchat", allow_idle_chitchat),
        ("allow_l3_finish_notify", allow_l3_finish_notify),
        ("allow_open_loop_followup", allow_open_loop_followup),
        ("inner_life_enabled", inner_life_enabled),
        ("allow_taskbar_inbox", allow_taskbar_inbox),
    ):
        if bval is not None and not isinstance(bval, bool):
            raise ValueError(f"{bname} must be a boolean, got {type(bval).__name__}")

    mode_val: Optional[str] = None
    if delivery_mode is not None:
        if not isinstance(delivery_mode, str):
            raise ValueError("delivery_mode must be a string")
        m = delivery_mode.strip().lower()
        if m not in VALID_PROACTIVE_DELIVERY_MODES:
            raise ValueError(f"Unsupported proactive delivery_mode: {delivery_mode!r}")
        mode_val = m

    qs_val: Optional[str] = None
    if quiet_start is not None:
        if not isinstance(quiet_start, str) or not _HHMM_RE.match(quiet_start.strip()):
            raise ValueError(f"quiet_start must be HH:MM (00:00..23:59), got {quiet_start!r}")
        qs_val = quiet_start.strip()

    qe_val: Optional[str] = None
    if quiet_end is not None:
        if not isinstance(quiet_end, str) or not _HHMM_RE.match(quiet_end.strip()):
            raise ValueError(f"quiet_end must be HH:MM (00:00..23:59), got {quiet_end!r}")
        qe_val = quiet_end.strip()

    iv_val = _require_int_param("check_interval_minutes", check_interval_minutes) if check_interval_minutes is not None else None
    min_chat_val = _require_int_param("min_since_last_chat_minutes", min_since_last_chat_minutes) if min_since_last_chat_minutes is not None else None
    cd_val = _require_int_param("min_cooldown_minutes", min_cooldown_minutes) if min_cooldown_minutes is not None else None
    max_n_val = _require_int_param("max_daily_nudges", max_daily_nudges) if max_daily_nudges is not None else None

    vol_val: Optional[float] = None
    if pc_speaker_volume is not None:
        if isinstance(pc_speaker_volume, bool):
            raise ValueError("pc_speaker_volume must be a number, not bool")
        try:
            fv = float(pc_speaker_volume)
        except Exception as exc:
            raise ValueError(f"pc_speaker_volume must be a number: {pc_speaker_volume!r}") from exc
        if fv != fv or fv < 0.2 or fv > 1.2:
            raise ValueError("pc_speaker_volume must be between 0.2 and 1.2")
        vol_val = round(fv, 2)

    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if enabled is not None:
            next_cfg["enabled"] = bool(enabled)
        if mode_val is not None:
            next_cfg["delivery_mode"] = mode_val
        if iv_val is not None:
            next_cfg["check_interval_minutes"] = iv_val
        if qs_val is not None:
            next_cfg["quiet_start"] = qs_val
        if qe_val is not None:
            next_cfg["quiet_end"] = qe_val
        if min_chat_val is not None:
            next_cfg["min_since_last_chat_minutes"] = min_chat_val
        if cd_val is not None:
            next_cfg["min_cooldown_minutes"] = cd_val
        if max_n_val is not None:
            next_cfg["max_daily_nudges"] = max_n_val
        if vol_val is not None:
            next_cfg["pc_speaker_volume"] = vol_val
        if allow_idle_chitchat is not None:
            next_cfg["allow_idle_chitchat"] = bool(allow_idle_chitchat)
        if allow_l3_finish_notify is not None:
            next_cfg["allow_l3_finish_notify"] = bool(allow_l3_finish_notify)
        if allow_open_loop_followup is not None:
            next_cfg["allow_open_loop_followup"] = bool(allow_open_loop_followup)
        if inner_life_enabled is not None:
            next_cfg["inner_life_enabled"] = bool(inner_life_enabled)
        if allow_taskbar_inbox is not None:
            next_cfg["allow_taskbar_inbox"] = bool(allow_taskbar_inbox)
        return normalize_proactive(next_cfg)

    saved = update_runtime_section("proactive", _upd, settings_path=settings_path)
    return dict(saved["proactive"])


def get_vocal_cue(settings_path: Optional[str] = None) -> Dict[str, bool]:
    sec = read_runtime_settings(settings_path)["vocal_cue"]
    return {"enabled": bool(sec["enabled"])}


def set_vocal_cue(
    *,
    enabled: Optional[bool] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, bool]:
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(f"enabled must be a boolean, got {type(enabled).__name__}")

    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if enabled is not None:
            next_cfg["enabled"] = bool(enabled)
        return normalize_vocal_cue(next_cfg)

    saved = update_runtime_section("vocal_cue", _upd, settings_path=settings_path)
    return {"enabled": bool(saved["vocal_cue"]["enabled"])}


def get_voiceprint(settings_path: Optional[str] = None) -> Dict[str, Any]:
    sec = read_runtime_settings(settings_path)["voiceprint"]
    return {
        "enabled": bool(sec["enabled"]),
        "policy": str(sec["policy"]),
        "threshold": float(sec["threshold"]),
    }


def set_voiceprint(
    *,
    enabled: Optional[bool] = None,
    policy: Optional[str] = None,
    threshold: Optional[float] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(f"enabled must be a boolean, got {type(enabled).__name__}")
    pol_val: Optional[str] = None
    if policy is not None:
        if not isinstance(policy, str):
            raise ValueError("policy must be a string")
        p = policy.strip().lower()
        if p not in VALID_VOICEPRINT_POLICIES:
            raise ValueError(f"Unsupported voiceprint policy: {policy!r}")
        pol_val = p
    thr_val: Optional[float] = None
    if threshold is not None:
        if isinstance(threshold, bool):
            raise ValueError("threshold must be a number, not bool")
        try:
            fv = float(threshold)
        except Exception as exc:
            raise ValueError(f"threshold must be a number: {threshold!r}") from exc
        if fv != fv or fv < 0.30 or fv > 0.90:
            raise ValueError("threshold must be between 0.30 and 0.90")
        thr_val = round(fv, 2)

    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if enabled is not None:
            next_cfg["enabled"] = bool(enabled)
        if pol_val is not None:
            next_cfg["policy"] = pol_val
        if thr_val is not None:
            next_cfg["threshold"] = thr_val
        return normalize_voiceprint(next_cfg)

    saved = update_runtime_section("voiceprint", _upd, settings_path=settings_path)
    sec = saved["voiceprint"]
    return {
        "enabled": bool(sec["enabled"]),
        "policy": str(sec["policy"]),
        "threshold": float(sec["threshold"]),
    }


def get_screen_peek(settings_path: Optional[str] = None) -> Dict[str, bool]:
    sec = read_runtime_settings(settings_path)["screen_peek"]
    return {"enabled": bool(sec["enabled"])}


def set_screen_peek(
    *,
    enabled: Optional[bool] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, bool]:
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(f"enabled must be a boolean, got {type(enabled).__name__}")

    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if enabled is not None:
            next_cfg["enabled"] = bool(enabled)
        return normalize_screen_peek(next_cfg)

    saved = update_runtime_section("screen_peek", _upd, settings_path=settings_path)
    return {"enabled": bool(saved["screen_peek"]["enabled"])}


def get_mobile_push(settings_path: Optional[str] = None) -> Dict[str, Any]:
    return dict(read_runtime_settings(settings_path)["mobile_push"])


def set_mobile_push(
    *,
    enabled: Optional[bool] = None,
    provider: Optional[str] = None,
    forward_proactive: Optional[bool] = None,
    forward_briefing: Optional[bool] = None,
    respect_quiet_hours: Optional[bool] = None,
    max_daily_pushes: Optional[int] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    for name, bval in (
        ("enabled", enabled),
        ("forward_proactive", forward_proactive),
        ("forward_briefing", forward_briefing),
        ("respect_quiet_hours", respect_quiet_hours),
    ):
        if bval is not None and not isinstance(bval, bool):
            raise ValueError(f"{name} must be a boolean, got {type(bval).__name__}")
    prov_val: Optional[str] = None
    if provider is not None:
        if not isinstance(provider, str):
            raise ValueError("provider must be a string")
        p = provider.strip().lower()
        if p not in VALID_MOBILE_PUSH_PROVIDERS:
            raise ValueError(f"Unsupported mobile_push provider: {provider!r}")
        prov_val = p
    max_val: Optional[int] = None
    if max_daily_pushes is not None:
        if isinstance(max_daily_pushes, bool) or not isinstance(max_daily_pushes, int):
            raise ValueError("max_daily_pushes must be an integer")
        if max_daily_pushes < 1 or max_daily_pushes > 50:
            raise ValueError("max_daily_pushes must be between 1 and 50")
        max_val = max_daily_pushes

    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if enabled is not None:
            next_cfg["enabled"] = bool(enabled)
        if prov_val is not None:
            next_cfg["provider"] = prov_val
        if forward_proactive is not None:
            next_cfg["forward_proactive"] = bool(forward_proactive)
        if forward_briefing is not None:
            next_cfg["forward_briefing"] = bool(forward_briefing)
        if respect_quiet_hours is not None:
            next_cfg["respect_quiet_hours"] = bool(respect_quiet_hours)
        if max_val is not None:
            next_cfg["max_daily_pushes"] = max_val
        return normalize_mobile_push(next_cfg)

    saved = update_runtime_section("mobile_push", _upd, settings_path=settings_path)
    return dict(saved["mobile_push"])


def get_llm_fallback(settings_path: Optional[str] = None) -> Dict[str, Any]:
    sec = read_runtime_settings(settings_path)["llm_fallback"]
    return {
        "enabled": bool(sec["enabled"]),
        "cooldown_seconds": int(sec["cooldown_seconds"]),
        "nodes": [dict(n) for n in (sec.get("nodes") or [])],
    }


def set_llm_fallback(
    *,
    enabled: Optional[bool] = None,
    cooldown_seconds: Optional[int] = None,
    nodes: Optional[list[Dict[str, Any]]] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(f"enabled must be a boolean, got {type(enabled).__name__}")
    cd_val: Optional[int] = None
    if cooldown_seconds is not None:
        if isinstance(cooldown_seconds, bool) or not isinstance(cooldown_seconds, int):
            raise ValueError("cooldown_seconds must be an integer")
        if cooldown_seconds < 10 or cooldown_seconds > 600:
            raise ValueError("cooldown_seconds must be between 10 and 600")
        cd_val = cooldown_seconds
    if nodes is not None and not isinstance(nodes, list):
        raise ValueError("nodes must be a list")

    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if enabled is not None:
            next_cfg["enabled"] = bool(enabled)
        if cd_val is not None:
            next_cfg["cooldown_seconds"] = cd_val
        if nodes is not None:
            next_cfg["nodes"] = list(nodes)
        return normalize_llm_fallback(next_cfg)

    saved = update_runtime_section("llm_fallback", _upd, settings_path=settings_path)
    return get_llm_fallback(settings_path) if saved is None else {
        "enabled": bool(saved["llm_fallback"]["enabled"]),
        "cooldown_seconds": int(saved["llm_fallback"]["cooldown_seconds"]),
        "nodes": [dict(n) for n in (saved["llm_fallback"].get("nodes") or [])],
    }


def get_qq_bot(settings_path: Optional[str] = None) -> Dict[str, Any]:
    sec = read_runtime_settings(settings_path)["qq_bot"]
    return {
        "enabled": bool(sec["enabled"]),
        "sandbox": bool(sec["sandbox"]),
        "share_primary_memory": bool(sec["share_primary_memory"]),
        "memory_device_id": str(sec["memory_device_id"]),
        "allow_l3_dispatch": bool(sec["allow_l3_dispatch"]),
        "owner_openid": str(sec["owner_openid"]),
        "allowed_openids": list(sec["allowed_openids"]),
        "allowed_group_openids": list(sec["allowed_group_openids"]),
        "batch_quiet_ms": int(sec.get("batch_quiet_ms", 3500)),
    }


def set_qq_bot(
    *,
    enabled: Optional[bool] = None,
    sandbox: Optional[bool] = None,
    share_primary_memory: Optional[bool] = None,
    memory_device_id: Optional[str] = None,
    allow_l3_dispatch: Optional[bool] = None,
    owner_openid: Optional[str] = None,
    allowed_openids: Optional[list[str]] = None,
    allowed_group_openids: Optional[list[str]] = None,
    batch_quiet_ms: Optional[int] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    for name, bval in (
        ("enabled", enabled),
        ("sandbox", sandbox),
        ("share_primary_memory", share_primary_memory),
        ("allow_l3_dispatch", allow_l3_dispatch),
    ):
        if bval is not None and not isinstance(bval, bool):
            raise ValueError(f"{name} must be a boolean")
    if memory_device_id is not None and not isinstance(memory_device_id, str):
        raise ValueError("memory_device_id must be a string")
    if owner_openid is not None and not isinstance(owner_openid, str):
        raise ValueError("owner_openid must be a string")
    if allowed_openids is not None and not isinstance(allowed_openids, list):
        raise ValueError("allowed_openids must be a list")
    if allowed_group_openids is not None and not isinstance(allowed_group_openids, list):
        raise ValueError("allowed_group_openids must be a list")
    bq_val: Optional[int] = None
    if batch_quiet_ms is not None:
        if isinstance(batch_quiet_ms, bool) or not isinstance(batch_quiet_ms, int):
            raise ValueError("batch_quiet_ms must be an integer")
        if batch_quiet_ms < 0 or batch_quiet_ms > 15000:
            raise ValueError("batch_quiet_ms must be between 0 and 15000")
        bq_val = batch_quiet_ms

    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if enabled is not None:
            next_cfg["enabled"] = bool(enabled)
        if sandbox is not None:
            next_cfg["sandbox"] = bool(sandbox)
        if share_primary_memory is not None:
            next_cfg["share_primary_memory"] = bool(share_primary_memory)
        if memory_device_id is not None:
            next_cfg["memory_device_id"] = str(memory_device_id).strip()[:64]
        if allow_l3_dispatch is not None:
            next_cfg["allow_l3_dispatch"] = bool(allow_l3_dispatch)
        if owner_openid is not None:
            next_cfg["owner_openid"] = str(owner_openid).strip()[:128]
        if allowed_openids is not None:
            next_cfg["allowed_openids"] = list(allowed_openids)
        if allowed_group_openids is not None:
            next_cfg["allowed_group_openids"] = list(allowed_group_openids)
        if bq_val is not None:
            next_cfg["batch_quiet_ms"] = bq_val
        return normalize_qq_bot(next_cfg)

    saved = update_runtime_section("qq_bot", _upd, settings_path=settings_path)
    return dict(saved["qq_bot"])


def get_skill_loader(settings_path: Optional[str] = None) -> Dict[str, bool]:
    return {"enabled": bool(read_runtime_settings(settings_path)["skill_loader"]["enabled"])}


def set_skill_loader(
    *,
    enabled: Optional[bool] = None,
    settings_path: Optional[str] = None,
) -> Dict[str, bool]:
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(f"enabled must be a boolean, got {type(enabled).__name__}")

    def _upd(cur: dict) -> dict:
        next_cfg = dict(cur or {})
        if enabled is not None:
            next_cfg["enabled"] = bool(enabled)
        return normalize_skill_loader(next_cfg)

    saved = update_runtime_section("skill_loader", _upd, settings_path=settings_path)
    return {"enabled": bool(saved["skill_loader"]["enabled"])}

"""Post-ASR homophone correction table with hot mtime-cached reload (G-4).

Persists user-editable homophone corrections at `config/runtime/asr_corrections.json`
(mounted inside container at `/opt/xiaozhi-esp32-server/data/asr_corrections.json`),
seeded with defaults such as `牙医 -> 芽衣` and `雷电牙医 -> 雷电芽衣`.

Applies longest-key-first single-pass regex replacement so multi-character entries
take precedence without cascading double-replacements.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, Optional, Tuple

import xz_common

SCHEMA_VERSION = 1
MAX_CORRECTION_RULES = 500
MAX_RULE_TOKEN_LEN = 64

DEFAULT_CORRECTIONS: Dict[str, str] = {
    "雷电牙医": "雷电芽衣",
    "雷电亚衣": "雷电芽衣",
    "雷电雅衣": "雷电芽衣",
    "你好牙医": "你好芽衣",
    "牙医": "芽衣",
    "芽一": "芽衣",
    "亚衣": "芽衣",
    "雅衣": "芽衣",
    "小志": "小智",
    "小治": "小智",
}


def _resolve_default_path() -> str:
    if os.path.isdir(xz_common.CONFIG_DIR):
        return os.path.join(xz_common.CONFIG_DIR, "asr_corrections.json")
    proj_root = os.path.dirname(os.path.abspath(xz_common.APP_DIR))
    return os.path.join(proj_root, "config", "runtime", "asr_corrections.json")


def _resolve_seed_paths() -> Tuple[str, ...]:
    proj_root = os.path.dirname(os.path.abspath(xz_common.APP_DIR))
    return (
        os.path.join(xz_common.ROOT_DIR, "config_templates", "asr_corrections.example.json"),
        os.path.join(proj_root, "config", "asr_corrections.example.json"),
    )


ASR_CORRECTIONS_PATH = _resolve_default_path()

_lock = threading.RLock()
_caches_lock = threading.Lock()
_caches: Dict[str, xz_common.MtimeJsonCache] = {}
_compiled_lock = threading.Lock()
_compiled_cache: Dict[str, Tuple[Tuple[Tuple[str, str], ...], Optional[re.Pattern[str]]]] = {}


def _parse_corrections_input(raw: Any, *, strict: bool = False) -> Dict[str, str]:
    """Parse corrections from dict, list of pairs/objects, or 'wrong->right' lines."""
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, str):
                if strict:
                    raise ValueError("corrections keys and values must be strings")
                continue
            pairs.append((k, v))
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict) and ("from" in item or "src" in item):
                k = item.get("from") if "from" in item else item.get("src")
                v = item.get("to") if "to" in item else item.get("dst")
                if not isinstance(k, str) or not isinstance(v, str):
                    if strict:
                        raise ValueError("correction rule entries must contain string from/to")
                    continue
                pairs.append((k, v))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                k, v = item
                if not isinstance(k, str) or not isinstance(v, str):
                    if strict:
                        raise ValueError("correction rule pair must contain strings")
                    continue
                pairs.append((k, v))
            elif strict:
                raise ValueError("Unsupported correction rule item format")
    elif isinstance(raw, str):
        for ln in raw.splitlines():
            line = ln.strip()
            if not line or line.startswith("#"):
                continue
            sep_found = None
            for sep in ("->", "=>", "→", "=", "：", ":"):
                if sep in line:
                    sep_found = sep
                    break
            if sep_found is None:
                if strict:
                    raise ValueError(f"Invalid correction line (expected wrong->right): {line[:40]}")
                continue
            k, v = line.split(sep_found, 1)
            pairs.append((k, v))
    else:
        if strict:
            raise ValueError("corrections must be a dict, list of pairs, or newline text")
        return dict(DEFAULT_CORRECTIONS)

    cleaned: Dict[str, str] = {}
    for raw_k, raw_v in pairs:
        k = raw_k.strip()
        v = raw_v.strip()
        if not k:
            if strict:
                raise ValueError("Correction source key must not be empty")
            continue
        if not v:
            if strict:
                raise ValueError(f"Correction replacement for {k!r} must not be empty")
            continue
        if len(k) > MAX_RULE_TOKEN_LEN or len(v) > MAX_RULE_TOKEN_LEN:
            if strict:
                raise ValueError(f"Correction key/value exceeds {MAX_RULE_TOKEN_LEN} chars")
            k = k[:MAX_RULE_TOKEN_LEN]
            v = v[:MAX_RULE_TOKEN_LEN]
        if k == v:
            continue
        cleaned[k] = v
        if len(cleaned) > MAX_CORRECTION_RULES:
            if strict:
                raise ValueError(f"Too many ASR correction rules (max {MAX_CORRECTION_RULES})")
            break
    return cleaned


def _load_seed_defaults() -> Dict[str, Any]:
    for seed_p in _resolve_seed_paths():
        if os.path.isfile(seed_p):
            try:
                with open(seed_p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return normalize_asr_corrections(data)
            except Exception:
                pass
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": True,
        "corrections": dict(DEFAULT_CORRECTIONS),
        "count": len(DEFAULT_CORRECTIONS),
    }


def normalize_asr_corrections(raw: Any) -> Dict[str, Any]:
    """Normalize loaded JSON (supports structured schema OR flat {wrong: right} dict)."""
    if not isinstance(raw, dict):
        return _load_seed_defaults()

    if "corrections" in raw or "enabled" in raw or "schema_version" in raw:
        raw_enabled = raw.get("enabled", True)
        if isinstance(raw_enabled, bool):
            enabled = raw_enabled
        else:
            enabled = str(raw_enabled).strip().lower() not in ("0", "false", "no", "off")
        if "corrections" in raw:
            corr = _parse_corrections_input(raw.get("corrections"), strict=False)
        else:
            corr = dict(DEFAULT_CORRECTIONS)
    else:
        enabled = True
        corr = _parse_corrections_input(raw, strict=False)

    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": bool(enabled),
        "corrections": corr,
        "count": len(corr),
    }


def _get_cache(path: str) -> xz_common.MtimeJsonCache:
    norm = os.path.abspath(path)
    with _caches_lock:
        c = _caches.get(norm)
        if c is None:
            c = xz_common.MtimeJsonCache(
                normalizer=normalize_asr_corrections,
                default_factory=_load_seed_defaults,
            )
            _caches[norm] = c
        return c


def ensure_asr_corrections_file(path: Optional[str] = None) -> str:
    """Seed `config/runtime/asr_corrections.json` with defaults if absent (fail-soft if read-only)."""
    target = os.path.abspath(path or ASR_CORRECTIONS_PATH)
    with _lock:
        if not os.path.isfile(target):
            defaults = _load_seed_defaults()
            disk_payload = {
                "schema_version": SCHEMA_VERSION,
                "enabled": defaults["enabled"],
                "corrections": defaults["corrections"],
            }
            try:
                xz_common.atomic_write_json(target, disk_payload)
                _get_cache(target).set(target, defaults)
            except Exception:
                pass
    return target


def get_asr_corrections(path: Optional[str] = None) -> Dict[str, Any]:
    """Return mtime-cached normalized ASR homophone corrections config."""
    target = os.path.abspath(path or ASR_CORRECTIONS_PATH)
    data = _get_cache(target).get(target)
    return {
        "schema_version": int(data.get("schema_version", SCHEMA_VERSION)),
        "enabled": bool(data.get("enabled", True)),
        "corrections": dict(data.get("corrections") or {}),
        "count": len(data.get("corrections") or {}),
    }


def save_asr_corrections(
    corrections: Any = None,
    *,
    enabled: Optional[bool] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and atomically persist ASR homophone corrections to disk, updating mtime cache immediately."""
    target = os.path.abspath(path or ASR_CORRECTIONS_PATH)
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")

    with _lock:
        current = get_asr_corrections(target)
        next_enabled = current["enabled"] if enabled is None else enabled
        if corrections is None:
            next_corr = dict(current["corrections"])
        else:
            next_corr = _parse_corrections_input(corrections, strict=True)

        disk_payload = {
            "schema_version": SCHEMA_VERSION,
            "enabled": bool(next_enabled),
            "corrections": next_corr,
        }
        xz_common.atomic_write_json(target, disk_payload)
        normalized = {
            "schema_version": SCHEMA_VERSION,
            "enabled": bool(next_enabled),
            "corrections": dict(next_corr),
            "count": len(next_corr),
        }
        _get_cache(target).set(target, normalized)
        with _compiled_lock:
            _compiled_cache.pop(target, None)
        return {
            "schema_version": normalized["schema_version"],
            "enabled": normalized["enabled"],
            "corrections": dict(normalized["corrections"]),
            "count": normalized["count"],
        }


def _get_compiled_pattern(target: str, corr: Dict[str, str]) -> Optional[re.Pattern[str]]:
    if not corr:
        return None
    sig = tuple(sorted(corr.items()))
    with _compiled_lock:
        cached = _compiled_cache.get(target)
        if cached is not None and cached[0] == sig:
            return cached[1]
        keys_sorted = sorted(corr.keys(), key=lambda k: (-len(k), k))
        pat = re.compile("|".join(re.escape(k) for k in keys_sorted))
        _compiled_cache[target] = (sig, pat)
        return pat


def apply_asr_corrections(text: str, *, path: Optional[str] = None) -> str:
    """Apply hot-reloaded homophone corrections to an ASR output string."""
    if not text or not isinstance(text, str):
        return text
    target = os.path.abspath(path or ASR_CORRECTIONS_PATH)
    cfg = get_asr_corrections(target)
    if not cfg.get("enabled", True):
        return text
    corr = cfg.get("corrections") or {}
    if not corr:
        return text
    pat = _get_compiled_pattern(target, corr)
    if pat is None:
        return text
    return pat.sub(lambda m: corr[m.group(0)], text)

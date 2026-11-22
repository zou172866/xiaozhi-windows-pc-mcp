"""Anima Brain relationship, persona sync, Obsidian export, and SQLite outbox routes (H-1, B-5)."""

from __future__ import annotations

import asyncio
import atexit
from collections import deque
from datetime import date as _date_cls
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
import re
import secrets
import socket
import struct
import threading
import time
from urllib.parse import parse_qs, quote, urlparse
import wave

import httpx
import yaml

import asr_corrections
import mei_emotion_router
import patch_registry
import pc_audio_router
import persona_versions
import reply_length_router
import runtime_events
import runtime_settings
import turn_context_router
import xz_common
from console import shared

ANIMA_HOST = "http://anima-brain:18787"
ANIMA_TOKEN_PATH = xz_common.ANIMA_TOKEN_PATH
_anima_rel_lock = threading.Lock()
_anima_rel_cache: dict | None = None
_anima_rel_ts = 0.0
_ANIMA_REL_TTL = 3.0


def _read_anima_token() -> str:
    return xz_common.get_anima_token(ANIMA_TOKEN_PATH)


def _get_anima_character_id(cfg: dict | None = None) -> str:
    try:
        c = cfg if cfg is not None else shared._read_raw_yaml()
        cid = str(((c.get("Memory") or {}).get("AnimaBrain") or {}).get("character_id") or "").strip()
        if cid:
            return cid
    except Exception as exc:
        shared._log_rate_limited("get_anima_character_id", exc)
    return "xiaozhi"


def _default_offline_relationship(char_id: str = "xiaozhi") -> dict:
    return {
        "online": False,
        "character_id": char_id,
        "character_name": "小智",
        "relationship_enabled": True,
        "romance_enabled": True,
        "preset": "custom",
        "stage": "familiar",
        "stage_label_zh": "等待连接",
        "axes": {
            "familiarity": 0.62,
            "warmth": 0.66,
            "trust": 0.62,
            "romance": 0.20,
            "tension": 0.0,
        },
        "bands": {"familiarity": 2, "warmth": 2, "trust": 2, "romance": 0, "tension": 0},
        "guidance_zh": "",
    }


def _fetch_anima_relationship(force: bool = False, cfg: dict | None = None) -> dict:
    global _anima_rel_cache, _anima_rel_ts
    now = time.monotonic()
    with _anima_rel_lock:
        if not force and _anima_rel_cache is not None and (now - _anima_rel_ts) < _ANIMA_REL_TTL:
            return dict(_anima_rel_cache)

    char_id = _get_anima_character_id(cfg)
    token = _read_anima_token()
    if not token:
        return _default_offline_relationship(char_id)
    try:
        client = shared._get_console_http_client()
        resp = client.get(
            f"{ANIMA_HOST}/xiaozhi/relationship",
            params={"character_id": char_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=1.8,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["online"] = True
            with _anima_rel_lock:
                _anima_rel_cache = dict(data)
                _anima_rel_ts = time.monotonic()
            return data
    except Exception as exc:
        shared._log_rate_limited("fetch_anima_relationship", exc)
    with _anima_rel_lock:
        if _anima_rel_cache is not None:
            stale = dict(_anima_rel_cache)
            stale["online"] = False
            _anima_rel_ts = time.monotonic()
            return stale
    return _default_offline_relationship(char_id)


_RELATIONSHIP_AXIS_KEYS = frozenset({"familiarity", "warmth", "trust", "romance", "tension"})
_RELATIONSHIP_ALLOWED_KEYS = frozenset({"preset", "relationship_enabled", "romance_enabled", "axes"}) | _RELATIONSHIP_AXIS_KEYS
_RELATIONSHIP_PRESETS_ALLOWED = frozenset({"restrained", "confidant", "intimate"})


def _validate_relationship_payload(payload: dict) -> dict:
    """Strictly allowlist and validate /api/relationship fields; never allow caller to override character_id (4.1 #2)."""
    import math

    if not isinstance(payload, dict):
        raise ValueError("请求体必须为 JSON 对象")
    unknown = sorted(k for k in payload.keys() if k not in _RELATIONSHIP_ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"包含未允许的字段: {', '.join(unknown[:6])}")

    cleaned: dict = {}
    if "preset" in payload and payload["preset"] is not None:
        if not isinstance(payload["preset"], str):
            raise ValueError("字段 preset 必须为字符串")
        p_val = payload["preset"].strip().lower()
        if p_val and p_val not in _RELATIONSHIP_PRESETS_ALLOWED:
            raise ValueError(
                f"无效的 preset: {p_val!r}（仅允许 {', '.join(sorted(_RELATIONSHIP_PRESETS_ALLOWED))}）"
            )
        cleaned["preset"] = p_val

    for bkey in ("relationship_enabled", "romance_enabled"):
        if bkey in payload and payload[bkey] is not None:
            if not isinstance(payload[bkey], bool):
                raise ValueError(f"字段 {bkey} 必须为布尔值")
            cleaned[bkey] = payload[bkey]

    def _check_axis(name: str, val) -> float:
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"维度 {name} 必须为数字")
        fv = float(val)
        if not math.isfinite(fv) or fv < 0.0 or fv > 1.0:
            raise ValueError(f"维度 {name} 必须为 0.0 到 1.0 之间的有限数字")
        return fv

    for ax in _RELATIONSHIP_AXIS_KEYS:
        if ax in payload and payload[ax] is not None:
            cleaned[ax] = _check_axis(ax, payload[ax])

    if "axes" in payload and payload["axes"] is not None:
        raw_axes = payload["axes"]
        if not isinstance(raw_axes, dict):
            raise ValueError("字段 axes 必须为 JSON 对象")
        unknown_ax = sorted(k for k in raw_axes.keys() if k not in _RELATIONSHIP_AXIS_KEYS)
        if unknown_ax:
            raise ValueError(f"axes 包含未允许的维度: {', '.join(unknown_ax[:6])}")
        cleaned_axes: dict[str, float] = {}
        for ax, aval in raw_axes.items():
            if aval is not None:
                cleaned_axes[ax] = _check_axis(ax, aval)
        if cleaned_axes:
            cleaned["axes"] = cleaned_axes

    return cleaned


def _update_anima_relationship(payload: dict) -> tuple[bool, dict | str]:
    global _anima_rel_cache, _anima_rel_ts
    req_body = _validate_relationship_payload(payload)
    char_id = _get_anima_character_id()
    token = _read_anima_token()
    if not token:
        return False, "未找到 Anima Brain 访问令牌 (studio_token.txt)"
    # 4.1 #2: Always enforce configured character_id (never trust/preserve caller-supplied character_id)
    req_body["character_id"] = char_id
    try:
        client = shared._get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/relationship",
            headers={"Authorization": f"Bearer {token}"},
            json=req_body,
            timeout=4.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["online"] = True
            with _anima_rel_lock:
                _anima_rel_cache = dict(data)
                _anima_rel_ts = time.monotonic()
            return True, data
        return False, f"Anima Brain 返回 HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as exc:
        return False, f"无法连接 Anima Brain (18787): {exc}"


_obsidian_lock = threading.Lock()
_obsidian_cache: dict | None = None
_obsidian_ts = 0.0
_OBSIDIAN_TTL = 3.0


def _default_offline_obsidian() -> dict:
    return {
        "online": False,
        "enabled": False,
        "writable": False,
        "export_dir": "/obsidian",
        "host_export_dir": "",
        "snapshot_interval_seconds": 600,
        "last_export_at": "",
        "last_export_ts": 0.0,
        "last_export_reason": "",
        "last_error": "无法连接 Anima Brain (18787)",
        "stats": {
            "turns": 0,
            "days": 0,
            "active_facts": 0,
            "historical_revisions": 0,
            "open_loops": 0,
            "episodes": 0,
            "files_written": 0,
            "files_skipped": 0,
        },
    }


def _mark_obsidian_cache_offline(err_msg: str = "") -> dict:
    global _obsidian_cache, _obsidian_ts
    with _obsidian_lock:
        if _obsidian_cache is not None:
            offline = dict(_obsidian_cache)
            offline["online"] = False
            if err_msg:
                offline["last_error"] = err_msg
        else:
            offline = _default_offline_obsidian()
            if err_msg:
                offline["last_error"] = err_msg
        _obsidian_cache = dict(offline)
        _obsidian_ts = time.monotonic()
        return offline


def _get_cached_obsidian_snapshot() -> dict:
    with _obsidian_lock:
        if _obsidian_cache is not None:
            return dict(_obsidian_cache)
    return _default_offline_obsidian()


def _fetch_obsidian_status(force: bool = False) -> dict:
    global _obsidian_cache, _obsidian_ts
    now = time.monotonic()
    with _obsidian_lock:
        if not force and _obsidian_cache is not None and (now - _obsidian_ts) < _OBSIDIAN_TTL:
            return dict(_obsidian_cache)

    token = _read_anima_token()
    if not token:
        return _mark_obsidian_cache_offline("未找到 Anima Brain 访问令牌 (studio_token.txt)")
    try:
        client = shared._get_console_http_client()
        resp = client.get(
            f"{ANIMA_HOST}/xiaozhi/obsidian/status",
            headers={"Authorization": f"Bearer {token}"},
            timeout=1.8,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["online"] = True
            with _obsidian_lock:
                _obsidian_cache = dict(data)
                _obsidian_ts = time.monotonic()
            return data
    except Exception as exc:
        shared._log_rate_limited("fetch_obsidian_status", exc)
    return _mark_obsidian_cache_offline()


def _update_obsidian_config(payload: dict) -> tuple[bool, dict | str]:
    global _obsidian_cache, _obsidian_ts
    token = _read_anima_token()
    if not token:
        msg = "未找到 Anima Brain 访问令牌 (studio_token.txt)"
        _mark_obsidian_cache_offline(msg)
        return False, msg
    body = payload or {}
    has_en = "enabled" in body
    has_br = "briefing_enabled" in body
    if (
        (not has_en and not has_br)
        or (has_en and not isinstance(body.get("enabled"), bool))
        or (has_br and not isinstance(body.get("briefing_enabled"), bool))
    ):
        return False, "缺少布尔字段 enabled 或 briefing_enabled"
    req_json: dict = {}
    if has_en:
        req_json["enabled"] = bool(body["enabled"])
    if has_br:
        req_json["briefing_enabled"] = bool(body["briefing_enabled"])
    try:
        client = shared._get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/obsidian/config",
            headers={"Authorization": f"Bearer {token}"},
            json=req_json,
            timeout=15.0,
        )
        if resp.status_code == 200:
            raw = resp.json()
            data = dict(raw.get("obsidian") or raw)
            data["online"] = True
            with _obsidian_lock:
                _obsidian_cache = dict(data)
                _obsidian_ts = time.monotonic()
            return True, data
        return False, f"Anima Brain 返回 HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as exc:
        msg = f"无法连接 Anima Brain (18787): {exc}"
        _mark_obsidian_cache_offline(msg)
        return False, msg


def _trigger_obsidian_briefing(payload: dict | None = None) -> tuple[bool, dict | str]:
    global _obsidian_cache, _obsidian_ts
    token = _read_anima_token()
    if not token:
        msg = "未找到 Anima Brain 访问令牌 (studio_token.txt)"
        _mark_obsidian_cache_offline(msg)
        return False, msg
    req_json: dict = {}
    if isinstance(payload, dict) and payload.get("target_date") is not None:
        if not isinstance(payload.get("target_date"), str):
            return False, "字段 target_date 必须为 YYYY-MM-DD 字符串"
        td_clean = payload["target_date"].strip()
        if td_clean:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", td_clean):
                return False, "字段 target_date 必须为合法的 YYYY-MM-DD 日期"
            try:
                _date_cls.fromisoformat(td_clean)
            except ValueError:
                return False, "字段 target_date 不是有效的日历日期"
            req_json["target_date"] = td_clean
    try:
        client = shared._get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/obsidian/briefing",
            headers={"Authorization": f"Bearer {token}"},
            json=req_json,
            timeout=25.0,
        )
        raw = {}
        try:
            raw = resp.json()
        except Exception as exc:
            shared._log_rate_limited("obsidian_briefing_json", exc)
            raw = {}
        if resp.status_code == 200 and raw.get("ok"):
            status_data = dict(raw.get("obsidian") or {})
            status_data["online"] = True
            with _obsidian_lock:
                _obsidian_cache = dict(status_data)
                _obsidian_ts = time.monotonic()
            return True, {"obsidian": status_data, "result": raw.get("result") or raw}
        err = raw.get("error") or f"Anima Brain 返回 HTTP {resp.status_code}: {resp.text[:160]}"
        return False, str(err)
    except Exception as exc:
        msg = f"无法连接 Anima Brain (18787): {exc}"
        _mark_obsidian_cache_offline(msg)
        return False, msg


def _trigger_obsidian_rebuild() -> tuple[bool, dict | str]:
    global _obsidian_cache, _obsidian_ts
    token = _read_anima_token()
    if not token:
        msg = "未找到 Anima Brain 访问令牌 (studio_token.txt)"
        _mark_obsidian_cache_offline(msg)
        return False, msg
    try:
        client = shared._get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/obsidian/rebuild",
            headers={"Authorization": f"Bearer {token}"},
            json={},
            timeout=30.0,
        )
        raw = {}
        try:
            raw = resp.json()
        except Exception as exc:
            shared._log_rate_limited("obsidian_rebuild_json", exc)
            raw = {}
        if resp.status_code == 200 and raw.get("ok"):
            status_data = dict(raw.get("obsidian") or {})
            status_data["online"] = True
            with _obsidian_lock:
                _obsidian_cache = dict(status_data)
                _obsidian_ts = time.monotonic()
            return True, {"obsidian": status_data, "result": raw.get("result") or raw}
        err = raw.get("error") or f"Anima Brain 返回 HTTP {resp.status_code}: {resp.text[:160]}"
        if isinstance(raw.get("obsidian"), dict):
            status_data = dict(raw["obsidian"])
            status_data["online"] = True
            with _obsidian_lock:
                _obsidian_cache = dict(status_data)
                _obsidian_ts = time.monotonic()
        return False, str(err)
    except Exception as exc:
        msg = f"无法连接 Anima Brain (18787): {exc}"
        _mark_obsidian_cache_offline(msg)
        return False, msg


def _infer_character_name(prompt: str) -> str:
    p = (prompt or "").strip()
    if "雷电芽衣" in p:
        return "雷电芽衣"
    if "芽衣" in p:
        return "芽衣"
    m = re.search(r"你是(?:《[^》]+》中的)?([\u4e00-\u9fa5A-Za-z0-9]{2,8}?)(?:[，,。！!、\s]|$)", p)
    if m:
        cand = m.group(1).strip()
        if cand and not cand.startswith(("一个", "一名", "一位", "住在")):
            return cand
    return "小智"


def _sync_persona_to_anima(prompt: str, *, revision: int | None = None, digest: str = "") -> bool:
    """将 18080 控制台的角色姓名与提示词自动同步至 18787 Anima Brain 数据库，确保双端一致。"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return True
    eff_digest = digest or persona_versions.compute_prompt_digest(clean_prompt)
    new_name = _infer_character_name(clean_prompt)
    try:
        token = _read_anima_token()
        if not token:
            persona_versions.record_anima_sync_status(
                ok=False, revision=revision, digest=eff_digest, char_name=new_name, detail="missing_anima_token"
            )
            return False
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            boot = client.get(f"{ANIMA_HOST}/api/bootstrap", headers=headers)
            if boot.status_code != 200:
                persona_versions.record_anima_sync_status(
                    ok=False, revision=revision, digest=eff_digest, char_name=new_name, detail=f"bootstrap_http_{boot.status_code}"
                )
                return False
            chars = boot.json().get("characters") or []
            target_cid = _get_anima_character_id()
            target_spec = next((c for c in chars if c.get("id") == target_cid), None)
            if not target_spec:
                persona_versions.record_anima_sync_status(
                    ok=False, revision=revision, digest=eff_digest, char_name=new_name, detail="character_not_found"
                )
                return False
            anima_persona = mei_emotion_router.strip_voice_tag_instruction_for_anima(clean_prompt)
            if target_spec.get("name") == new_name and (target_spec.get("persona") or "").strip() == anima_persona:
                persona_versions.record_anima_sync_status(
                    ok=True, revision=revision, digest=eff_digest, char_name=new_name, detail="already_in_sync"
                )
                return True
            updated = dict(target_spec)
            updated["name"] = new_name
            updated["persona"] = anima_persona
            r = client.post(f"{ANIMA_HOST}/api/update_character", headers=headers, json={"spec": updated})
            if r.status_code == 200:
                persona_versions.record_anima_sync_status(
                    ok=True, revision=revision, digest=eff_digest, char_name=new_name, detail="synced"
                )
                print(f"[WebConsole] 已自动同步角色设定至 Anima Brain: name={new_name}", flush=True)
                return True
            else:
                persona_versions.record_anima_sync_status(
                    ok=False, revision=revision, digest=eff_digest, char_name=new_name, detail=f"update_http_{r.status_code}"
                )
                return False
    except Exception as exc:
        persona_versions.record_anima_sync_status(
            ok=False, revision=revision, digest=eff_digest, char_name=new_name, detail=shared._redact_secrets(str(exc))
        )
        print(shared._redact_secrets(f"[WebConsole] 同步 Anima Brain 设定跳过: {exc}"), flush=True)
        return False


def _sanitize_outbox_stats(raw: dict | None) -> dict:
    d = raw if isinstance(raw, dict) else {}
    return {
        "pending": int(d.get("pending") or 0),
        "dead": int(d.get("dead") or 0),
        "corrupt": int(d.get("corrupt") or 0),
        "sent": int(d.get("sent") or 0),
        "max_retries": int(d.get("max_retries") or 8),
        "dead_ttl_days": float(d.get("dead_ttl_days") or 14.0),
        "last_error": shared._redact_secrets(str(d.get("last_error") or "")),
        "worker_alive": bool(d.get("worker_alive", False)),
    }


def _get_anima_outbox_snapshot() -> dict:
    try:
        import anima_memory_provider

        return _sanitize_outbox_stats(anima_memory_provider.get_outbox_stats())
    except Exception as exc:
        shared._log_rate_limited("get_anima_outbox_stats", exc)
        return _sanitize_outbox_stats(None)


def _replay_anima_outbox_dead(payload: dict) -> tuple[bool, dict | str, int]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象", int(HTTPStatus.BAD_REQUEST)
    unknown = sorted(str(k) for k in payload.keys() if k not in ("limit", "request_ids"))
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}", int(HTTPStatus.BAD_REQUEST)

    limit_arg: int | None = None
    if "limit" in payload and payload.get("limit") is not None:
        raw_lim = payload.get("limit")
        if isinstance(raw_lim, bool):
            return False, "字段 limit 必须为正整数", int(HTTPStatus.BAD_REQUEST)
        try:
            limit_arg = int(raw_lim)
        except (TypeError, ValueError):
            return False, "字段 limit 必须为正整数", int(HTTPStatus.BAD_REQUEST)
        if limit_arg <= 0 or limit_arg > 1000:
            return False, "字段 limit 必须在 1～1000 之间", int(HTTPStatus.BAD_REQUEST)

    req_ids_arg: list[str] | None = None
    if "request_ids" in payload and payload.get("request_ids") is not None:
        raw_ids = payload.get("request_ids")
        if not isinstance(raw_ids, list) or not all(isinstance(x, str) and x.strip() for x in raw_ids):
            return False, "字段 request_ids 必须为非空字符串数组", int(HTTPStatus.BAD_REQUEST)
        req_ids_arg = [x.strip() for x in raw_ids]

    try:
        import anima_memory_provider

        fn = getattr(anima_memory_provider, "replay_dead_outbox_turns", None)
        if not callable(fn):
            return False, "当前 anima_memory_provider 版本不支持重放 dead 队列", int(HTTPStatus.NOT_IMPLEMENTED)
        res = fn(limit=limit_arg, request_ids=req_ids_arg)
        if not isinstance(res, dict) or not res.get("ok"):
            err = (res.get("error") if isinstance(res, dict) else None) or "重放 dead 队列失败"
            return False, str(err), int(HTTPStatus.INTERNAL_SERVER_ERROR)
        return (
            True,
            {
                "ok": True,
                "replayed": int(res.get("replayed") or 0),
                "outbox": _sanitize_outbox_stats(res.get("stats")),
            },
            int(HTTPStatus.OK),
        )
    except Exception as exc:
        shared._log_rate_limited("replay_anima_outbox_dead", exc)
        return False, f"重放 Anima dead 队列失败: {exc}", int(HTTPStatus.INTERNAL_SERVER_ERROR)



def handle_get_relationship(handler) -> None:
    rel_cfg = _fetch_anima_relationship(force=True)
    handler._send_json({"ok": bool(rel_cfg.get("online")), "relationship": rel_cfg})


def handle_post_relationship(handler) -> None:
    try:
        body = handler._read_body_json()
        ok, result = _update_anima_relationship(body)
        if ok:
            handler._send_json({"ok": True, "relationship": result})
        else:
            handler._send_json({"ok": False, "error": str(result)}, HTTPStatus.BAD_GATEWAY)
    except shared._BadRequestError:
        raise
    except ValueError as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_get_obsidian(handler) -> None:
    obs_cfg = _fetch_obsidian_status(force=True)
    handler._send_json({"ok": bool(obs_cfg.get("online")), "obsidian": obs_cfg})


def handle_post_obsidian_config(handler) -> None:
    try:
        body = handler._read_body_json()
        ok, result = _update_obsidian_config(body)
        if ok:
            handler._send_json({"ok": True, "obsidian": result})
        else:
            status = HTTPStatus.BAD_REQUEST if "enabled" in str(result) else HTTPStatus.BAD_GATEWAY
            err_payload = {"ok": False, "error": str(result)}
            if status == HTTPStatus.BAD_GATEWAY:
                err_payload["obsidian"] = _get_cached_obsidian_snapshot()
            handler._send_json(err_payload, status)
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_obsidian_rebuild(handler) -> None:
    try:
        handler._read_body_json()
        ok, result = _trigger_obsidian_rebuild()
        if ok and isinstance(result, dict):
            handler._send_json(
                {
                    "ok": True,
                    "obsidian": result.get("obsidian"),
                    "result": result.get("result"),
                }
            )
        else:
            handler._send_json(
                {"ok": False, "error": str(result), "obsidian": _get_cached_obsidian_snapshot()},
                HTTPStatus.BAD_GATEWAY,
            )
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_get_anima_outbox(handler) -> None:
    handler._send_json({"ok": True, "outbox": _get_anima_outbox_snapshot()})


def handle_post_anima_outbox_replay_dead(handler) -> None:
    try:
        body = handler._read_body_json()
        ok_rep, res_or_err, status_code = _replay_anima_outbox_dead(body)
        if ok_rep and isinstance(res_or_err, dict):
            handler._send_json(res_or_err, HTTPStatus(status_code))
        else:
            handler._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus(status_code))
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_obsidian_briefing(handler) -> None:
    try:
        body = handler._read_body_json()
        ok, result = _trigger_obsidian_briefing(body)
        if ok and isinstance(result, dict):
            handler._send_json(
                {
                    "ok": True,
                    "obsidian": result.get("obsidian"),
                    "result": result.get("result"),
                }
            )
        else:
            status = HTTPStatus.BAD_REQUEST if "target_date" in str(result) else HTTPStatus.BAD_GATEWAY
            handler._send_json(
                {"ok": False, "error": str(result), "obsidian": _get_cached_obsidian_snapshot()},
                status,
            )
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def _compute_local_asr_baseline(wrong: str, current_to: str = "") -> str:
    raw = json.dumps(
        {
            "kind": "asr_correction",
            "from": str(wrong or "").strip(),
            "current_to": str(current_to or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _annotate_dream_asr_baselines(dream_data: dict) -> dict:
    """Attach live local asr_corrections.json baseline verification to pending ASR proposals."""
    proposals = dream_data.get("proposals")
    if not isinstance(proposals, list):
        return dream_data
    try:
        cur_corr = dict((asr_corrections.get_asr_corrections() or {}).get("corrections") or {})
    except Exception as exc:
        shared._log_rate_limited("dream_annotate_asr", exc)
        return dream_data
    for prop in proposals:
        if not isinstance(prop, dict):
            continue
        if prop.get("kind") == "asr_correction" and prop.get("status") == "pending":
            pl = prop.get("payload") or {}
            wrong = str(pl.get("from") or "").strip()
            live_hash = _compute_local_asr_baseline(wrong, str(cur_corr.get(wrong) or ""))
            prop["baseline_ok"] = live_hash == str(prop.get("baseline_hash") or "")
            prop["live_baseline_hash"] = live_hash
    return dream_data


def _fetch_dream_status(
    status_filter: str | None = None, *, proposal_id: str | None = None
) -> tuple[bool, dict | str]:
    token = _read_anima_token()
    char_id = _get_anima_character_id()
    if not token:
        return False, "未找到 Anima Brain 访问令牌 (studio_token.txt)"
    params = {"character_id": char_id}
    if status_filter:
        params["status"] = status_filter
    if proposal_id:
        params["proposal_id"] = proposal_id
    try:
        client = shared._get_console_http_client()
        resp = client.get(
            f"{ANIMA_HOST}/xiaozhi/dream/status",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = _annotate_dream_asr_baselines(dict(resp.json()))
            data["online"] = True
            return True, data
        if resp.status_code == 404 and proposal_id:
            return True, {"online": True, "proposals": []}
        return False, f"Anima Brain 返回 HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as exc:
        return False, f"无法连接 Anima Brain (18787): {exc}"


def _trigger_dream_run() -> tuple[bool, dict | str]:
    token = _read_anima_token()
    char_id = _get_anima_character_id()
    if not token:
        return False, "未找到 Anima Brain 访问令牌 (studio_token.txt)"
    try:
        cur_asr = dict((asr_corrections.get_asr_corrections() or {}).get("corrections") or {})
    except Exception as exc:
        shared._log_rate_limited("dream_run_read_asr", exc)
        cur_asr = {}
    known_terms: list[str] = []
    try:
        cfg = shared._read_raw_yaml()
        cname = _infer_character_name(str(cfg.get("prompt") or ""))
        if cname:
            known_terms.append(cname)
        if "雷电芽衣" in str(cfg.get("prompt") or "") and "雷电芽衣" not in known_terms:
            known_terms.append("雷电芽衣")
    except Exception as exc:
        shared._log_rate_limited("dream_run_read_yaml", exc)
    try:
        client = shared._get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/dream/run",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "character_id": char_id,
                "existing_asr_corrections": cur_asr,
                "known_terms": known_terms,
            },
            timeout=20.0,
        )
        if resp.status_code == 200:
            data = _annotate_dream_asr_baselines(dict(resp.json()))
            data["online"] = True
            return True, data
        return False, f"Anima Brain 返回 HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as exc:
        return False, f"无法连接 Anima Brain (18787): {exc}"


_DREAM_CONFIG_ALLOWED_KEYS = frozenset({"enabled", "interval_seconds"})
_DREAM_APPROVE_ALLOWED_KEYS = frozenset({"proposal_id", "expected_baseline_hash", "note"})
_DREAM_REJECT_ALLOWED_KEYS = frozenset({"proposal_id", "note"})


def _update_dream_config(payload: dict) -> tuple[bool, dict | str, int]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象", int(HTTPStatus.BAD_REQUEST)
    unknown = sorted(str(k) for k in payload.keys() if k not in _DREAM_CONFIG_ALLOWED_KEYS)
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}", int(HTTPStatus.BAD_REQUEST)
    if not payload:
        return False, "请至少提供 enabled 或 interval_seconds", int(HTTPStatus.BAD_REQUEST)
    req_json: dict = {"character_id": _get_anima_character_id()}
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            return False, "字段 enabled 必须为布尔值", int(HTTPStatus.BAD_REQUEST)
        req_json["enabled"] = payload["enabled"]
    if "interval_seconds" in payload:
        iv = payload["interval_seconds"]
        if isinstance(iv, bool) or not isinstance(iv, int) or not (300 <= iv <= 604800):
            return False, "字段 interval_seconds 必须为 300～604800 之间的整数", int(HTTPStatus.BAD_REQUEST)
        req_json["interval_seconds"] = iv

    token = _read_anima_token()
    if not token:
        return False, "未找到 Anima Brain 访问令牌 (studio_token.txt)", int(HTTPStatus.BAD_GATEWAY)
    try:
        client = shared._get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/dream/config",
            headers={"Authorization": f"Bearer {token}"},
            json=req_json,
            timeout=6.0,
        )
        if resp.status_code == 200:
            return True, dict(resp.json()), int(HTTPStatus.OK)
        return False, f"Anima Brain 返回 HTTP {resp.status_code}: {resp.text[:160]}", resp.status_code
    except Exception as exc:
        return False, f"无法连接 Anima Brain (18787): {exc}", int(HTTPStatus.BAD_GATEWAY)


def _sync_asr_corrections_to_anima(corrections: dict[str, str] | None = None) -> bool:
    """Fire-and-forget sync of local asr_corrections.json to Anima Brain AutoDream meta (no mount boundary break)."""
    try:
        if corrections is None:
            cur_map = dict((asr_corrections.get_asr_corrections() or {}).get("corrections") or {})
        else:
            cur_map = {
                str(k).strip(): str(v).strip()
                for k, v in corrections.items()
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
            }
        token = _read_anima_token()
        if not token:
            return False
        client = shared._get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/dream/config",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "existing_asr_corrections": cur_map,
                "all_characters": True,
            },
            timeout=5.0,
        )
        if resp.status_code == 200:
            return True
        shared._log_rate_limited(
            "sync_asr_corrections_to_anima_http",
            RuntimeError(f"HTTP {resp.status_code}"),
        )
        return False
    except Exception as exc:
        shared._log_rate_limited("sync_asr_corrections_to_anima", exc)
        return False


_asr_sync_lock = threading.Lock()
_asr_sync_dirty = False
_asr_sync_running = False


def schedule_asr_corrections_sync() -> None:
    """Coalesced, strictly serialized background sync of latest asr_corrections.json to 18787.

    Prevents out-of-order HTTP completion races when multiple ASR edits happen back-to-back.
    """
    global _asr_sync_dirty, _asr_sync_running
    with _asr_sync_lock:
        _asr_sync_dirty = True
        if _asr_sync_running:
            return
        _asr_sync_running = True

    def _worker() -> None:
        global _asr_sync_dirty, _asr_sync_running
        try:
            while True:
                with _asr_sync_lock:
                    if not _asr_sync_dirty:
                        _asr_sync_running = False
                        return
                    _asr_sync_dirty = False
                try:
                    latest_map = dict((asr_corrections.get_asr_corrections() or {}).get("corrections") or {})
                except Exception as exc:
                    shared._log_rate_limited("schedule_asr_sync_read", exc)
                    latest_map = None
                try:
                    _sync_asr_corrections_to_anima(latest_map)
                except Exception as exc:
                    shared._log_rate_limited("schedule_asr_sync_run", exc)
        except BaseException:
            with _asr_sync_lock:
                _asr_sync_running = False
            raise

    threading.Thread(target=_worker, name="anima-asr-sync", daemon=True).start()


def _rollback_single_asr_rule(
    wrong: str,
    applied_right: str,
    *,
    had_prev: bool,
    prev_val: str,
) -> None:
    """Targeted single-key rollback under lock so concurrent/manual ASR edits are never clobbered."""
    with asr_corrections._lock:
        cur_corr = dict((asr_corrections.get_asr_corrections() or {}).get("corrections") or {})
        if cur_corr.get(wrong) != applied_right:
            return
        if had_prev:
            cur_corr[wrong] = prev_val
        else:
            cur_corr.pop(wrong, None)
        asr_corrections.save_asr_corrections(corrections=cur_corr)


def _approve_dream_proposal(payload: dict) -> tuple[bool, dict | str, int]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象", int(HTTPStatus.BAD_REQUEST)
    unknown = sorted(str(k) for k in payload.keys() if k not in _DREAM_APPROVE_ALLOWED_KEYS)
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}", int(HTTPStatus.BAD_REQUEST)
    pid = str(payload.get("proposal_id") or "").strip()
    if not pid:
        return False, "字段 proposal_id 不能为空", int(HTTPStatus.BAD_REQUEST)
    exp_bh = payload.get("expected_baseline_hash")
    if exp_bh is not None and (not isinstance(exp_bh, str) or not exp_bh.strip()):
        return False, "字段 expected_baseline_hash 必须为非空字符串", int(HTTPStatus.BAD_REQUEST)
    note = str(payload.get("note") or "").strip()[:240]

    token = _read_anima_token()
    char_id = _get_anima_character_id()
    if not token:
        return False, "未找到 Anima Brain 访问令牌 (studio_token.txt)", int(HTTPStatus.BAD_GATEWAY)

    try:
        ok_st, st_data = _fetch_dream_status(proposal_id=pid)
    except TypeError:
        ok_st, st_data = _fetch_dream_status()
    if not ok_st or not isinstance(st_data, dict):
        return False, str(st_data), int(HTTPStatus.BAD_GATEWAY)
    target_prop = next((p for p in (st_data.get("proposals") or []) if p.get("id") == pid), None)
    if not target_prop:
        return False, f"未找到提案: {pid}", int(HTTPStatus.NOT_FOUND)
    if target_prop.get("status") != "pending":
        return False, f"该提案当前状态为 {target_prop.get('status')}，不可重复审批", int(HTTPStatus.CONFLICT)

    client = shared._get_console_http_client()
    kind = str(target_prop.get("kind") or "")
    stored_bh = str(target_prop.get("baseline_hash") or "")

    if kind == "asr_correction":
        pl = target_prop.get("payload") or {}
        wrong = str(pl.get("from") or "").strip()
        right = str(pl.get("to") or "").strip()
        if not wrong or not right:
            return False, "ASR 纠错提案缺少有效的 from/to 字段", int(HTTPStatus.BAD_REQUEST)
        with asr_corrections._lock:
            cur_cfg = asr_corrections.get_asr_corrections()
            prev_corr = dict(cur_cfg.get("corrections") or {})
            had_prev = wrong in prev_corr
            prev_val = str(prev_corr.get(wrong) or "")
            live_bh = _compute_local_asr_baseline(wrong, prev_val)
            if live_bh != stored_bh or (exp_bh is not None and exp_bh.strip() != stored_bh):
                return (
                    False,
                    "基线校验失败：本地 asr_corrections.json 在提案生成后已发生变更，请重新运行离线梳理",
                    int(HTTPStatus.CONFLICT),
                )
            next_corr = dict(prev_corr)
            next_corr[wrong] = right
            saved_asr = asr_corrections.save_asr_corrections(corrections=next_corr)
        try:
            resp = client.post(
                f"{ANIMA_HOST}/xiaozhi/dream/approve",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "character_id": char_id,
                    "proposal_id": pid,
                    "expected_baseline_hash": stored_bh,
                    "note": note,
                    "external_applied": True,
                    "external_result": {"from": wrong, "to": right, "total_rules": saved_asr.get("count", 0)},
                },
                timeout=10.0,
            )
            if resp.status_code == 200:
                res_obj = dict(resp.json())
                res_obj["asr_corrections"] = saved_asr
                schedule_asr_corrections_sync()
                return True, res_obj, int(HTTPStatus.OK)
            # Targeted single-key rollback so concurrent approvals/manual edits survive intact
            try:
                _rollback_single_asr_rule(wrong, right, had_prev=had_prev, prev_val=prev_val)
            except Exception as rb_exc:
                shared._log_rate_limited("dream_asr_rollback", rb_exc)
            err_body = {}
            try:
                err_body = resp.json()
            except Exception as exc:
                shared._log_rate_limited("dream_approve_err_json", exc)
            return False, str(err_body.get("error") or f"Anima 返回 HTTP {resp.status_code}"), resp.status_code
        except Exception as exc:
            try:
                _rollback_single_asr_rule(wrong, right, had_prev=had_prev, prev_val=prev_val)
            except Exception as rb_exc:
                shared._log_rate_limited("dream_asr_rollback_exc", rb_exc)
            return False, f"批准提案时连接 Anima Brain 失败（已回滚本地纠错词表）: {exc}", int(HTTPStatus.BAD_GATEWAY)

    # memory_merge / style_adjustment: verified and committed atomically inside Anima Brain
    try:
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/dream/approve",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "character_id": char_id,
                "proposal_id": pid,
                "expected_baseline_hash": exp_bh.strip() if isinstance(exp_bh, str) else stored_bh,
                "note": note,
            },
            timeout=10.0,
        )
        raw = {}
        try:
            raw = resp.json()
        except Exception as exc:
            shared._log_rate_limited("dream_approve_json", exc)
        if resp.status_code == 200:
            return True, dict(raw), int(HTTPStatus.OK)
        return False, str(raw.get("error") or f"Anima 返回 HTTP {resp.status_code}"), resp.status_code
    except Exception as exc:
        return False, f"无法连接 Anima Brain (18787): {exc}", int(HTTPStatus.BAD_GATEWAY)


def _reject_dream_proposal(payload: dict) -> tuple[bool, dict | str, int]:
    if not isinstance(payload, dict):
        return False, "请求体必须为 JSON 对象", int(HTTPStatus.BAD_REQUEST)
    unknown = sorted(str(k) for k in payload.keys() if k not in _DREAM_REJECT_ALLOWED_KEYS)
    if unknown:
        return False, f"包含不支持的字段: {', '.join(unknown)}", int(HTTPStatus.BAD_REQUEST)
    pid = str(payload.get("proposal_id") or "").strip()
    if not pid:
        return False, "字段 proposal_id 不能为空", int(HTTPStatus.BAD_REQUEST)
    note = str(payload.get("note") or "").strip()[:240]
    token = _read_anima_token()
    if not token:
        return False, "未找到 Anima Brain 访问令牌 (studio_token.txt)", int(HTTPStatus.BAD_GATEWAY)
    try:
        client = shared._get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/dream/reject",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "character_id": _get_anima_character_id(),
                "proposal_id": pid,
                "note": note,
            },
            timeout=8.0,
        )
        raw = {}
        try:
            raw = resp.json()
        except Exception as exc:
            shared._log_rate_limited("dream_reject_json", exc)
        if resp.status_code == 200:
            return True, dict(raw), int(HTTPStatus.OK)
        return False, str(raw.get("error") or f"Anima 返回 HTTP {resp.status_code}"), resp.status_code
    except Exception as exc:
        return False, f"无法连接 Anima Brain (18787): {exc}", int(HTTPStatus.BAD_GATEWAY)


def handle_get_dream(handler, parsed=None) -> None:
    st_filter = None
    if parsed is not None:
        qs = parse_qs(parsed.query)
        st_filter = (qs.get("status") or [None])[0]
    ok, data = _fetch_dream_status(status_filter=st_filter)
    if ok and isinstance(data, dict):
        handler._send_json({"ok": True, "dream": data})
    else:
        handler._send_json(
            {"ok": False, "error": str(data), "dream": {"online": False, "pending_count": 0, "proposals": []}},
            HTTPStatus.BAD_GATEWAY,
        )


def handle_post_dream_run(handler) -> None:
    try:
        handler._read_body_json()
        ok, data = _trigger_dream_run()
        if ok and isinstance(data, dict):
            handler._send_json({"ok": True, "dream": data})
        else:
            handler._send_json({"ok": False, "error": str(data)}, HTTPStatus.BAD_GATEWAY)
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_dream_config(handler) -> None:
    try:
        body = handler._read_body_json()
        ok, res, code = _update_dream_config(body)
        if ok and isinstance(res, dict):
            handler._send_json(res, HTTPStatus(code))
        else:
            handler._send_json({"ok": False, "error": str(res)}, HTTPStatus(code))
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_dream_approve(handler) -> None:
    try:
        body = handler._read_body_json()
        ok, res, code = _approve_dream_proposal(body)
        if ok and isinstance(res, dict):
            handler._send_json(res, HTTPStatus(code))
        else:
            handler._send_json(
                {"ok": False, "error": str(res), "conflict": code == int(HTTPStatus.CONFLICT)},
                HTTPStatus(code),
            )
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_dream_reject(handler) -> None:
    try:
        body = handler._read_body_json()
        ok, res, code = _reject_dream_proposal(body)
        if ok and isinstance(res, dict):
            handler._send_json(res, HTTPStatus(code))
        else:
            handler._send_json({"ok": False, "error": str(res)}, HTTPStatus(code))
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_embedding_reset_breaker(handler) -> None:
    try:
        handler._read_body_json()
        token = _read_anima_token()
        if not token:
            handler._send_json({"ok": False, "error": "未找到 Anima Brain 访问令牌"}, HTTPStatus.BAD_GATEWAY)
            return
        client = shared._get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/embedding/reset-breaker",
            headers={"Authorization": f"Bearer {token}"},
            json={"character_id": _get_anima_character_id()},
            timeout=6.0,
        )
        if resp.status_code == 200:
            res_body = dict(resp.json())
            if isinstance(res_body.get("semantic_retrieval"), dict):
                try:
                    from console.routes import state as state_route

                    state_route._set_cached_anima_semantic(res_body["semantic_retrieval"])
                except Exception as cache_exc:
                    shared._log_rate_limited("reset_breaker_cache_sync", cache_exc)
            handler._send_json(res_body)
        else:
            handler._send_json(
                {"ok": False, "error": f"Anima Brain 返回 HTTP {resp.status_code}"},
                HTTPStatus.BAD_GATEWAY,
            )
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

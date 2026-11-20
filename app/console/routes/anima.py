"""Anima Brain relationship, persona sync, Obsidian export, and SQLite outbox routes (H-1, B-5)."""

from __future__ import annotations

import asyncio
import atexit
from collections import deque
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


def _update_anima_relationship(payload: dict) -> tuple[bool, dict | str]:
    global _anima_rel_cache, _anima_rel_ts
    char_id = _get_anima_character_id()
    token = _read_anima_token()
    if not token:
        return False, "未找到 Anima Brain 访问令牌 (studio_token.txt)"
    req_body = dict(payload or {})
    req_body.setdefault("character_id", char_id)
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
    if "enabled" not in (payload or {}) or not isinstance(payload.get("enabled"), bool):
        return False, "缺少布尔字段 enabled"
    try:
        client = shared._get_console_http_client()
        resp = client.post(
            f"{ANIMA_HOST}/xiaozhi/obsidian/config",
            headers={"Authorization": f"Bearer {token}"},
            json={"enabled": bool(payload["enabled"])},
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


def _sync_persona_to_anima(prompt: str):
    """将 18080 控制台的角色姓名与提示词自动同步至 18787 Anima Brain 数据库，确保双端一致。"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return
    try:
        token = _read_anima_token()
        if not token:
            return
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            boot = client.get(f"{ANIMA_HOST}/api/bootstrap", headers=headers)
            if boot.status_code != 200:
                return
            chars = boot.json().get("characters") or []
            target_spec = next((c for c in chars if c.get("id") == "xiaozhi"), None)
            if not target_spec:
                return
            new_name = _infer_character_name(clean_prompt)
            anima_persona = mei_emotion_router.strip_voice_tag_instruction_for_anima(clean_prompt)
            if target_spec.get("name") == new_name and (target_spec.get("persona") or "").strip() == anima_persona:
                return
            updated = dict(target_spec)
            updated["name"] = new_name
            updated["persona"] = anima_persona
            r = client.post(f"{ANIMA_HOST}/api/update_character", headers=headers, json={"spec": updated})
            if r.status_code == 200:
                print(f"[WebConsole] 已自动同步角色设定至 Anima Brain: name={new_name}", flush=True)
    except Exception as exc:
        print(f"[WebConsole] 同步 Anima Brain 设定跳过: {exc}", flush=True)


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

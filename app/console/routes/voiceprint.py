"""18080 Web Console routes for Phase 2 voiceprint guard, companion desktop widget & on-demand screen peek."""

from __future__ import annotations

import base64
from http import HTTPStatus
import math
import os
import sys
import threading
import time
from typing import Any, Dict

import mei_emotion_router
import proactive_companion
import runtime_events
import runtime_settings
import vocal_cue_router
import voiceprint_router
from console import shared
from console.routes import gsv

_ALLOWED_VP_CONFIG_KEYS = frozenset({"enabled", "policy", "threshold", "screen_peek_enabled"})
_ALLOWED_ENROLL_LAST_KEYS = frozenset({"display_name", "role", "speaker_id", "character_id", "session_id"})
_ALLOWED_ENROLL_WAV_KEYS = frozenset({"display_name", "role", "wav_base64", "speaker_id", "character_id"})
_ALLOWED_DELETE_KEYS = frozenset({"speaker_id"})
_ALLOWED_SCREEN_PEEK_KEYS = frozenset({"question", "speak"})

_download_lock = threading.Lock()
_download_state: Dict[str, Any] = {
    "downloading": False,
    "last_ok": None,
    "last_error": "",
    "updated_at": 0.0,
}


def _resolve_base_character_id() -> str:
    try:
        cfg = shared._read_raw_yaml()
        cid = str(((cfg.get("Memory") or {}).get("AnimaBrain") or {}).get("character_id") or "").strip()
        if cid:
            return cid
    except Exception as exc:
        shared._log_rate_limited("vp_resolve_base_char", exc)
    return "xiaozhi"


def _resolve_widget_character_info() -> tuple[str, str]:
    """Return `(character_key, display_name)` from active configured GSV/TTS voice or prompt."""
    try:
        cfg = shared._read_raw_yaml()
        sel = cfg.get("selected_module") or {}
        tts_name = str(sel.get("TTS") or "")
        if tts_name == "GPT_SOVITS_V2":
            gsv_cfg = (cfg.get("TTS") or {}).get("GPT_SOVITS_V2") or {}
            vid = gsv._resolve_gsv_voice_id(gsv_cfg)
            if vid == "__gsv_luoxiaohei__":
                return "luoxiaohei", "罗小黑"
            if vid == "__gsv_raiden_mei__":
                return "raiden_mei", "雷电芽衣"
            if vid == "__gsv_xiaozhi_tw__":
                return "xiaozhi", "小智"
        prompt = str(cfg.get("prompt") or "")
        if "雷电芽衣" in prompt or "芽衣" in prompt:
            return "raiden_mei", "雷电芽衣"
        if "罗小黑" in prompt:
            return "luoxiaohei", "罗小黑"
    except Exception as exc:
        shared._log_rate_limited("vp_resolve_widget_char", exc)
    return "raiden_mei", "小智"


def _extract_latest_widget_events() -> Dict[str, Any]:
    """Scan in-process runtime_events ring buffer for latest emotion, subtitle, proactive & screen peek."""
    evts = runtime_events.get_recent_events(limit=120)
    emotion = "calm"
    subtitle = ""
    subtitle_ts = 0.0
    proactive_bubble: Dict[str, Any] | None = None
    screen_peek_bubble: Dict[str, Any] | None = None

    for ev in evts:
        et = str(ev.get("type") or "")
        ts = float(ev.get("ts") or 0.0)
        if et == "assistant_message":
            txt = str(ev.get("text") or "").strip()
            if txt:
                subtitle = txt[:160]
                subtitle_ts = ts
        elif et == "tts_state":
            st = str(ev.get("state") or "")
            txt = str(ev.get("text") or "").strip()
            if st in ("sentence_start", "start") and txt:
                emo_tag, stripped = mei_emotion_router.extract_and_strip_emotion(txt, flush_incomplete=True)
                if emo_tag:
                    emotion = emo_tag
                clean_s = stripped.strip()
                if clean_s:
                    subtitle = clean_s[:160]
                    subtitle_ts = ts
        elif et == "proactive_nudge":
            if ev.get("action") == "SPEAK" and ev.get("text"):
                proactive_bubble = {
                    "text": str(ev.get("text") or "")[:120],
                    "emotion": str(ev.get("emotion") or "gentle"),
                    "mode": str(ev.get("mode") or "dry_run"),
                    "delivered": bool(ev.get("delivered", False)),
                    "ts": ts,
                }
                if ev.get("emotion"):
                    emotion = str(ev["emotion"])
        elif et == "screen_peek":
            if ev.get("text"):
                screen_peek_bubble = {
                    "text": str(ev.get("text") or "")[:140],
                    "emotion": str(ev.get("emotion") or "gentle"),
                    "safe_title": str(ev.get("safe_title") or ""),
                    "ts": ts,
                }
                if ev.get("emotion"):
                    emotion = str(ev["emotion"])

    # Fall back to last emotion tracked by mei_emotion_router only if events yielded default calm
    if emotion == "calm":
        try:
            recent_emo = getattr(mei_emotion_router, "get_last_routed_emotion", None)
            if callable(recent_emo):
                last_e = str(recent_emo() or "").strip().lower()
                if last_e:
                    emotion = last_e
        except Exception as exc:
            shared._log_rate_limited("vp_widget_last_emotion", exc)

    return {
        "emotion": emotion,
        "subtitle": subtitle,
        "subtitle_ts": subtitle_ts,
        "proactive_bubble": proactive_bubble,
        "screen_peek_bubble": screen_peek_bubble,
    }


def _validate_voiceprint_config(payload: dict) -> Dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("请提供要更新的声纹或浅看屏配置字段")
    unknown = sorted(str(k) for k in payload.keys() if k not in _ALLOWED_VP_CONFIG_KEYS)
    if unknown:
        raise ValueError(f"包含未允许的字段: {', '.join(unknown[:6])}")
    cleaned: Dict[str, Any] = {}
    for bkey in ("enabled", "screen_peek_enabled"):
        if bkey in payload:
            val = payload[bkey]
            if not isinstance(val, bool):
                raise ValueError(f"字段 {bkey} 必须为布尔值")
            cleaned[bkey] = val
    if "policy" in payload:
        raw_p = payload["policy"]
        if not isinstance(raw_p, str):
            raise ValueError("字段 policy 必须为字符串 (observe / isolate_memory / full_guard)")
        p = raw_p.strip().lower()
        if p not in runtime_settings.VALID_VOICEPRINT_POLICIES:
            raise ValueError("policy 仅支持 observe、isolate_memory 或 full_guard")
        cleaned["policy"] = p
    if "threshold" in payload:
        raw_t = payload["threshold"]
        if isinstance(raw_t, bool) or not isinstance(raw_t, (int, float)):
            raise ValueError("字段 threshold 必须为 0.30～0.90 之间的数字")
        ft = float(raw_t)
        if not math.isfinite(ft) or ft < 0.30 or ft > 0.90:
            raise ValueError("字段 threshold 必须在 0.30～0.90 之间")
        cleaned["threshold"] = round(ft, 2)
    return cleaned


def _build_voiceprint_payload() -> Dict[str, Any]:
    st = voiceprint_router.get_voiceprint_router().get_status()
    with _download_lock:
        dl = dict(_download_state)
    st["download"] = dl
    st["screen_peek_enabled"] = bool(runtime_settings.get_screen_peek().get("enabled", False))
    st["vocal_cue_enabled"] = vocal_cue_router.is_vocal_cue_enabled()
    return st


def handle_get_voiceprint(handler) -> None:
    try:
        handler._send_json(_build_voiceprint_payload())
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_voiceprint_config(handler) -> None:
    try:
        body = handler._read_body_json()
        cleaned = _validate_voiceprint_config(body)
        peek_en = cleaned.pop("screen_peek_enabled", None)
        if peek_en is not None:
            runtime_settings.set_screen_peek(enabled=peek_en)
        if cleaned:
            saved = runtime_settings.set_voiceprint(**cleaned)
            if cleaned.get("enabled") is False:
                # Explicitly unload ONNX speaker model & clear volatile PCM cache to reclaim RAM immediately
                voiceprint_router.unload_voiceprint_model(run_gc=True)
        else:
            saved = runtime_settings.get_voiceprint()
        payload = _build_voiceprint_payload()
        payload["config"] = saved
        handler._send_json(payload)
    except shared._BadRequestError:
        raise
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_voiceprint_enroll_last(handler) -> None:
    try:
        body = handler._read_body_json()
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_ENROLL_LAST_KEYS)
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        for skey in ("display_name", "role", "speaker_id", "character_id", "session_id"):
            if skey in body and body[skey] is not None and not isinstance(body[skey], str):
                handler._send_json({"ok": False, "error": f"字段 {skey} 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                return
        display_name = str(body.get("display_name") or "").strip()
        role = str(body.get("role") or "owner").strip().lower()
        speaker_id = str(body.get("speaker_id") or "").strip() or None
        character_id = str(body.get("character_id") or "").strip() or None
        session_id = str(body.get("session_id") or "").strip() or None

        prof = voiceprint_router.get_voiceprint_router().enroll_last_utterance(
            display_name=display_name,
            role=role,
            speaker_id=speaker_id,
            character_id=character_id,
            session_id=session_id,
            base_character_id=_resolve_base_character_id(),
        )
        payload = _build_voiceprint_payload()
        payload["enrolled"] = prof
        handler._send_json(payload)
    except shared._BadRequestError:
        raise
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except RuntimeError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_voiceprint_enroll_wav(handler) -> None:
    try:
        try:
            body = handler._read_body_json(max_bytes=4 * 1024 * 1024)
        except TypeError:
            body = handler._read_body_json()
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_ENROLL_WAV_KEYS)
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        for skey in ("display_name", "role", "speaker_id", "character_id"):
            if skey in body and body[skey] is not None and not isinstance(body[skey], str):
                handler._send_json({"ok": False, "error": f"字段 {skey} 必须为字符串"}, HTTPStatus.BAD_REQUEST)
                return
        display_name = str(body.get("display_name") or "").strip()
        role = str(body.get("role") or "owner").strip().lower()
        raw_b64 = body.get("wav_base64")
        if not isinstance(raw_b64, str) or not raw_b64.strip():
            handler._send_json({"ok": False, "error": "字段 wav_base64 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        if len(raw_b64) > 3 * 1024 * 1024:
            handler._send_json({"ok": False, "error": "上传 WAV 超过 2 MB 大小上限"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            wav_bytes = base64.b64decode(raw_b64.strip(), validate=True)
        except Exception:
            handler._send_json({"ok": False, "error": "wav_base64 不是有效的 Base64 编码"}, HTTPStatus.BAD_REQUEST)
            return
        speaker_id = str(body.get("speaker_id") or "").strip() or None
        character_id = str(body.get("character_id") or "").strip() or None

        prof = voiceprint_router.get_voiceprint_router().enroll_wav_bytes(
            wav_bytes,
            display_name=display_name,
            role=role,
            speaker_id=speaker_id,
            character_id=character_id,
            base_character_id=_resolve_base_character_id(),
        )
        payload = _build_voiceprint_payload()
        payload["enrolled"] = prof
        handler._send_json(payload)
    except shared._BadRequestError:
        raise
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except RuntimeError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_voiceprint_delete(handler) -> None:
    try:
        body = handler._read_body_json()
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_DELETE_KEYS)
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        raw_sid = body.get("speaker_id")
        if not isinstance(raw_sid, str) or not raw_sid.strip():
            handler._send_json({"ok": False, "error": "字段 speaker_id 必须为非空字符串"}, HTTPStatus.BAD_REQUEST)
            return
        sid = raw_sid.strip()
        deleted = voiceprint_router.get_voiceprint_router().delete_speaker(sid)
        if not deleted:
            handler._send_json({"ok": False, "error": f"未找到说话人: {sid}"}, HTTPStatus.NOT_FOUND)
            return
        payload = _build_voiceprint_payload()
        payload["deleted_speaker_id"] = sid
        handler._send_json(payload)
    except shared._BadRequestError:
        raise
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def _load_downloader_module():
    import importlib.util

    # Check common locations: host repo scripts/ or container scripts/
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "..", "scripts", "download_speaker_model.py"),
        "/opt/xiaozhi-esp32-server/scripts/download_speaker_model.py",
    ]
    for path in candidates:
        ap = os.path.abspath(path)
        if os.path.isfile(ap):
            spec = importlib.util.spec_from_file_location("_xz_download_speaker_model", ap)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
    return None


def _validate_download_url_ssrf(url: str, allowed_hosts: frozenset[str]) -> str:
    """Validate HTTPS scheme, host allowlist, port 443, and global IP (anti-SSRF before connecting)."""
    import ipaddress
    import socket
    import urllib.parse

    clean_url = str(url or "").strip()
    parsed = urllib.parse.urlparse(clean_url)
    if parsed.scheme != "https":
        raise ValueError("仅允许 HTTPS 下载链接")
    host = (parsed.hostname or "").strip().lower()
    if not host or host not in allowed_hosts:
        raise ValueError(f"下载主机不在白名单内: {host or 'empty'}")
    port = parsed.port or 443
    if port != 443:
        raise ValueError("仅允许 443 标准端口")
    try:
        ip_lit = ipaddress.ip_address(host)
        if not ip_lit.is_global:
            raise ValueError("拒绝访问非公网 IP")
    except ValueError as exc:
        if "非公网" in str(exc):
            raise
    has_proxy = any(
        os.environ.get(k, "").strip()
        for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
    )
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        for _fam, _type, _proto, _canon, sockaddr in infos:
            ip_obj = ipaddress.ip_address(sockaddr[0])
            if not ip_obj.is_global:
                raise ValueError(f"下载域名解析到非公网地址: {host}")
    except socket.gaierror:
        if not has_proxy:
            raise
    return clean_url


def _download_speaker_model_inline(target_dir: str) -> tuple[bool, str]:
    try:
        mod = _load_downloader_module()
        if mod is not None and callable(getattr(mod, "download_speaker_model", None)):
            expected_sha = os.environ.get("XIAOZHI_SPEAKER_MODEL_SHA256", "").strip() or None
            return mod.download_speaker_model(target_dir, expected_sha256=expected_sha)
    except Exception as exc:
        shared._log_rate_limited("vp_load_downloader", exc)

    # In container, scripts/ is not mounted; perform equivalent hop-by-hop SSRF-checked HTTPS download
    import hashlib
    import urllib.parse
    import httpx

    fname = voiceprint_router.PREFERRED_MODEL_FILENAME
    try:
        dest_dir = os.path.abspath(target_dir)
        os.makedirs(dest_dir, exist_ok=True)
        dest_file = os.path.join(dest_dir, fname)
        if os.path.isfile(dest_file) and os.path.getsize(dest_file) >= voiceprint_router.MIN_MODEL_BYTES:
            return True, dest_file
    except OSError as exc:
        return False, f"目录或文件访问失败: {exc}"

    allowed_hosts = frozenset(
        {
            "github.com",
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "hf-mirror.com",
            "huggingface.co",
            "cdn-lfs.huggingface.co",
            "cdn-lfs-us-1.huggingface.co",
            "cdn-lfs.hf.co",
            "cas-bridge.xethub.hf.co",
            "modelscope.cn",
            "www.modelscope.cn",
        }
    )
    urls = (
        "https://hf-mirror.com/csukuangfj/speaker-embedding-models/resolve/main/" + fname,
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/" + fname,
    )
    override = os.environ.get("XIAOZHI_SPEAKER_MODEL_URL", "").strip()
    if override:
        urls = (override,) + urls
    expected_sha = os.environ.get("XIAOZHI_SPEAKER_MODEL_SHA256", "").strip().lower()

    last_err = "no valid download URL"
    for start_url in urls:
        tmp_path = os.path.join(dest_dir, f".{fname}.{os.getpid()}.tmp")
        try:
            cur_url = _validate_download_url_ssrf(start_url, allowed_hosts)
            written = 0
            h = hashlib.sha256()
            with httpx.Client(follow_redirects=False, timeout=120.0, trust_env=True) as client:
                for _hop in range(6):
                    cur_url = _validate_download_url_ssrf(cur_url, allowed_hosts)
                    with client.stream(
                        "GET",
                        cur_url,
                        headers={"User-Agent": "xiaozhi-server-voiceprint/1.0"},
                    ) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308):
                            loc = (resp.headers.get("location") or "").strip()
                            if not loc:
                                raise ValueError("重定向响应缺少 Location")
                            cur_url = urllib.parse.urljoin(cur_url, loc)
                            continue
                        if resp.status_code != 200:
                            raise ValueError(f"HTTP {resp.status_code}")
                        with open(tmp_path, "wb") as out:
                            for chunk in resp.iter_bytes(chunk_size=128 * 1024):
                                if not chunk:
                                    continue
                                written += len(chunk)
                                if written > 120_000_000:
                                    raise ValueError("File exceeds safety cap")
                                out.write(chunk)
                                h.update(chunk)
                            out.flush()
                            os.fsync(out.fileno())
                        break
                else:
                    raise ValueError("重定向次数超过安全上限")
            if written < voiceprint_router.MIN_MODEL_BYTES:
                raise ValueError(f"Downloaded file too small ({written} bytes)")
            digest = h.hexdigest().lower()
            if expected_sha and digest != expected_sha:
                raise ValueError(f"SHA-256 mismatch: expected {expected_sha}, got {digest}")
            os.replace(tmp_path, dest_file)
            return True, dest_file
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
    return False, last_err


def handle_post_voiceprint_download_model(handler) -> None:
    try:
        # Always drain POST body on HTTP/1.1 keep-alive connections and enforce empty/whitelist body
        body = handler._read_body_json()
        if body:
            unknown = sorted(str(k) for k in body.keys())
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        router = voiceprint_router.get_voiceprint_router()
        if router.is_model_present():
            payload = _build_voiceprint_payload()
            payload["message"] = "声纹模型已存在，无需重复下载"
            handler._send_json(payload)
            return

        with _download_lock:
            if _download_state["downloading"]:
                handler._send_json(
                    {"ok": False, "downloading": True, "error": "声纹模型正在后台下载中，请稍候刷新查看进度"},
                    HTTPStatus.CONFLICT,
                )
                return
            _download_state["downloading"] = True
            _download_state["last_error"] = ""
            _download_state["updated_at"] = time.time()

        def _bg_dl() -> None:
            ok = False
            detail = "download failed"
            try:
                ok, detail = _download_speaker_model_inline(router.model_dir)
            except Exception as exc:
                ok = False
                detail = f"{type(exc).__name__}: {exc}"
                shared._log_rate_limited("vp_bg_download", exc)
            finally:
                with _download_lock:
                    _download_state["downloading"] = False
                    _download_state["last_ok"] = bool(ok)
                    _download_state["last_error"] = "" if ok else shared._redact_secrets(str(detail))
                    _download_state["updated_at"] = time.time()

        threading.Thread(target=_bg_dl, name="voiceprint-model-download", daemon=True).start()
        payload = _build_voiceprint_payload()
        payload["message"] = "已开始后台下载声纹模型（约 28 MB），完成后将自动就绪"
        handler._send_json(payload, status=HTTPStatus.ACCEPTED)
    except shared._BadRequestError:
        raise
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_get_companion_widget(handler) -> None:
    """Ultra-lightweight read-only snapshot (~2ms) for the standalone desktop floating companion widget."""
    try:
        char_key, char_name = _resolve_widget_character_info()
        rt_snap = runtime_events.get_latest_runtime_snapshot()
        ev_info = _extract_latest_widget_events()
        vp_cfg = runtime_settings.get_voiceprint()
        pro_cfg = runtime_settings.get_proactive()
        peek_cfg = runtime_settings.get_screen_peek()
        audio_cfg = runtime_settings.get_audio_output()
        pc_mode = runtime_settings.get_pc_mode()
        latest_spk = voiceprint_router.get_latest_identification() if vp_cfg.get("enabled") else None

        handler._send_json(
            {
                "ok": True,
                "ts": time.time(),
                "character_key": char_key,
                "character_name": char_name,
                "activity": str(rt_snap.get("activity") or "idle"),
                "emotion": ev_info["emotion"],
                "subtitle": ev_info["subtitle"],
                "subtitle_ts": ev_info["subtitle_ts"],
                "proactive_bubble": ev_info["proactive_bubble"],
                "screen_peek_bubble": ev_info["screen_peek_bubble"],
                "vocal_cue": vocal_cue_router.get_latest_vocal_cue() if vocal_cue_router.is_vocal_cue_enabled() else None,
                "speaker": latest_spk,
                "pc_mode": pc_mode,
                "audio_target": str(audio_cfg.get("target") or "esp32"),
                "proactive_enabled": bool(pro_cfg.get("enabled", False)),
                "proactive_mode": str(pro_cfg.get("delivery_mode") or "dry_run"),
                "voiceprint_enabled": bool(vp_cfg.get("enabled", False)),
                "screen_peek_enabled": bool(peek_cfg.get("enabled", False)),
            }
        )
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_screen_peek(handler) -> None:
    try:
        body = handler._read_body_json()
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_SCREEN_PEEK_KEYS)
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        question = body.get("question", "")
        speak = body.get("speak", False)
        if question is not None and not isinstance(question, str):
            handler._send_json({"ok": False, "error": "字段 question 必须为字符串"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(speak, bool):
            handler._send_json({"ok": False, "error": "字段 speak 必须为布尔值"}, HTTPStatus.BAD_REQUEST)
            return

        res = proactive_companion.run_on_demand_screen_peek(
            question=(question or "").strip()[:200],
            speak=speak,
        )
        if res.get("ok"):
            code = HTTPStatus.OK
        elif res.get("blocked"):
            code = HTTPStatus.FORBIDDEN
        else:
            code = HTTPStatus.BAD_GATEWAY
        handler._send_json(res, status=code)
    except shared._BadRequestError:
        raise
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

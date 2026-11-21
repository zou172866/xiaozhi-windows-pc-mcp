"""Idle timeout farewell dual-mode patch (fixed random quotes / LLM dialogue summary) (H-1)."""

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

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MD_DECOR_RE = re.compile(r"[*`#>]+")


def select_idle_farewell_text(messages: list[str] | tuple[str, ...], last_text: str = "") -> str:
    """Pick a non-empty farewell line randomly while avoiding immediate repeats."""
    import random

    clean = [str(m or "").strip() for m in (messages or []) if str(m or "").strip()]
    if not clean:
        return ""
    pool = [m for m in clean if m != last_text] or clean
    return random.choice(pool)


def format_idle_farewell_for_tts(
    text: str,
    *,
    is_mei_voice: bool = False,
    default_emotion: str = "gentle",
) -> str:
    """Prefix Mei's warm default emotion tag when the line has no explicit emotion tag."""
    s = str(text or "").strip()
    if not s:
        return ""
    if is_mei_voice:
        emo, _ = tags.extract_and_strip_emotion(s, flush_incomplete=True)
        if not emo:
            tag = default_emotion if default_emotion in ("gentle", "tender", "shy", "calm", "happy", "playful", "serious") else "gentle"
            return f"[{tag}] {s}"
    return s


def extract_session_dialogue_snippet(conn: Any, max_turns: int = 8) -> list[tuple[str, str]]:
    """Extract clean recent (role, text) turns from current session Dialogue without mutating it."""
    dlg_obj = getattr(conn, "dialogue", None)
    raw_list = getattr(dlg_obj, "dialogue", None)
    if not isinstance(raw_list, list) or not raw_list:
        return []

    turns: list[tuple[str, str]] = []
    for msg in raw_list:
        if getattr(msg, "is_temporary", False) or getattr(msg, "_standalone_assistant", False):
            continue
        role = str(getattr(msg, "role", "") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        if getattr(msg, "tool_calls", None):
            continue
        raw_content = getattr(msg, "content", None)
        if not isinstance(raw_content, str):
            continue
        cleaned = tags.strip_emotion_tags(tags.safe_clean_response_garbage(raw_content)).strip()
        if not cleaned:
            continue
        if len(cleaned) > 180:
            cleaned = cleaned[:180].rstrip() + "…"
        turns.append((role, cleaned))

    if not any(r == "user" for r, _ in turns):
        return []
    limit = max(2, int(max_turns) * 2)
    return turns[-limit:]


async def generate_llm_idle_farewell(
    conn: Any,
    summary_prompt: str,
    *,
    is_mei_voice: bool = False,
    timeout_sec: float = 6.0,
) -> str:
    """Bypass conn.dialogue/Anima to generate a concise warm farewell summarizing the current session."""
    llm = getattr(conn, "llm", None)
    client = getattr(llm, "client", None)
    model_name = getattr(llm, "model_name", None)
    if llm is None or client is None or not model_name:
        return ""

    turns = extract_session_dialogue_snippet(conn, max_turns=8)
    if not turns:
        return ""

    transcript_lines = [
        f"{'对方' if role == 'user' else '你'}：{txt}"
        for role, txt in turns
    ]
    transcript = "\n".join(transcript_lines)

    base_persona = str((getattr(conn, "config", None) or {}).get("prompt") or "").strip()
    if len(base_persona) > 1200:
        base_persona = base_persona[:1200]
    sys_parts = []
    if base_persona:
        sys_parts.append(base_persona)
    sys_parts.append(
        "【当前任务：空闲超时道别】这是本轮对话结束前的最后一句告别，严禁调用任何工具，"
        "严格遵循下方用户的字数与语气要求直接说出告别台词。"
    )
    if is_mei_voice:
        sys_parts.append("请在开头附上最贴合依恋温柔心境的一个英文方括号情绪标签（如 [tender]、[gentle] 或 [shy]）。")

    instr = str(summary_prompt or "").strip() or runtime_settings.DEFAULT_IDLE_FAREWELL_SUMMARY_PROMPT
    user_msg = (
        f"以下是我们刚才这段对话的记录：\n{transcript}\n\n"
        f"请按以下要求说一句告别的话：\n{instr}"
    )

    request_params: Dict[str, Any] = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "\n\n".join(sys_parts)},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "max_tokens": 160,
        "temperature": 0.85,
        "timeout": float(timeout_sec),
    }
    apply_think = getattr(llm, "_apply_thinking_disabled", None)
    if callable(apply_think):
        try:
            apply_think(request_params)
        except Exception:
            pass

    def _call_sync() -> str:
        resp = client.chat.completions.create(**request_params)
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return ""
        msg = getattr(choices[0], "message", None)
        content = getattr(msg, "content", "") if msg is not None else ""
        return str(content or "")

    try:
        raw_out = await asyncio.wait_for(
            asyncio.to_thread(_call_sync),
            timeout=float(timeout_sec) + 1.5,
        )
    except Exception:
        return ""

    cleaned = _THINK_BLOCK_RE.sub("", raw_out)
    cleaned = tags.safe_clean_response_garbage(cleaned)
    cleaned = _MD_DECOR_RE.sub("", cleaned)
    cleaned = " ".join(ln.strip() for ln in cleaned.splitlines() if ln.strip()).strip()
    if len(cleaned) > 150:
        # Trim gracefully at a sentence boundary if LLM over-generated
        cut_idx = max(cleaned.rfind("。", 0, 145), cleaned.rfind("！", 0, 145), cleaned.rfind("？", 0, 145))
        cleaned = cleaned[: cut_idx + 1] if cut_idx >= 15 else cleaned[:145].rstrip()
    return cleaned


def apply_idle_farewell_patch() -> None:
    """Replace upstream idle-timeout prompt with customizable fixed or zero-pollution LLM summary TTS farewell."""
    import uuid
    import core.handle.receiveAudioHandle as recv_mod
    import core.handle.sendAudioHandle as send_audio_mod
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO

    if getattr(recv_mod, "_idle_farewell_patched", False):
        if patch_registry.get_patch_status("idle_farewell") is None:
            patch_registry.record_patch(
                "idle_farewell",
                "ok",
                target="receiveAudioHandle.no_voice_close_connect",
                detail="already applied",
            )
        return

    _orig_no_voice_close = recv_mod.no_voice_close_connect
    ok_sig, det_sig = patch_registry.verify_signature_contract(
        _orig_no_voice_close,
        required_params=("conn", "have_voice"),
        require_async=True,
    )
    if not ok_sig:
        patch_registry.record_patch(
            "idle_farewell",
            "skipped_contract",
            target="receiveAudioHandle.no_voice_close_connect",
            detail=det_sig,
            contract_ok=False,
            category="experience",
        )
        print(f"[MeiEmotionRouter] 跳过挂载空闲告别补丁（签名契约不匹配）: {det_sig}", flush=True)
        return
    _last_farewell = {"text": ""}

    async def _speak_or_close(conn: Any, chosen: str, *, timeout_sec: int, default_emotion: str, reason: str) -> None:
        conn.close_after_chat = True
        conn.client_abort = False

        if not chosen:
            conn.logger.bind(tag=recv_mod.TAG).info(
                f"无声超时 {timeout_sec}s（{reason}），无有效告别台词，静默断开连接"
            )
            await conn.close()
            return

        tts = getattr(conn, "tts", None)
        tts_q = getattr(tts, "tts_text_queue", None)
        if tts is None or tts_q is None:
            conn.logger.bind(tag=recv_mod.TAG).info("无声超时但 TTS 通道未就绪，直接断开连接")
            await conn.close()
            return

        mei_active = tags.is_raiden_mei_voice(getattr(tts, "ref_audio_path", ""), tts=tts)
        tts_payload_text = format_idle_farewell_for_tts(
            chosen,
            is_mei_voice=mei_active,
            default_emotion=default_emotion,
        )
        _, stripped_spoken = tags.extract_and_strip_emotion(tts_payload_text, flush_incomplete=True)
        if not tags.clean_segment_for_tts(stripped_spoken):
            conn.logger.bind(tag=recv_mod.TAG).info(
                f"无声超时 {timeout_sec}s，告别语无有效朗读内容，静默断开连接"
            )
            await conn.close()
            return

        sid = uuid.uuid4().hex
        conn.sentence_id = sid
        conn.client_is_speaking = True
        conn.logger.bind(tag=recv_mod.TAG).info(
            f"无声超时 {timeout_sec}s（{reason}），直接播报告别语并结束对话: {tags.strip_emotion_tags(chosen)}"
        )
        await send_audio_mod.send_tts_message(conn, "start")
        tts_q.put(
            TTSMessageDTO(
                sentence_id=sid,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
        tts_q.put(
            TTSMessageDTO(
                sentence_id=sid,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail=tts_payload_text,
            )
        )
        tts_q.put(
            TTSMessageDTO(
                sentence_id=sid,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )

    @functools.wraps(_orig_no_voice_close)
    async def _custom_no_voice_close_connect(conn, have_voice, *args, **kwargs):
        now_ms = time.time() * 1000.0
        if have_voice:
            conn.last_activity_time = now_ms
            if getattr(conn, "_idle_farewell_inflight", False):
                conn._idle_farewell_cancelled = True
            return

        # Never trigger idle timeout while TTS / PC audio playback is actively speaking
        if (
            getattr(conn, "client_is_speaking", False)
            or getattr(conn, "_pc_audio_active_sid", None) is not None
        ):
            conn.last_activity_time = now_ms
            return

        farewell_cfg = runtime_settings.get_idle_farewell()
        timeout_sec = int(farewell_cfg.get("timeout_seconds", 120))
        desired_hard_timeout = max(60, timeout_sec + 60)
        if getattr(conn, "timeout_seconds", None) != desired_hard_timeout:
            conn.timeout_seconds = desired_hard_timeout

        if getattr(conn, "last_activity_time", 0.0) <= 0.0:
            return
        if getattr(conn, "close_after_chat", False) or getattr(conn, "_idle_farewell_inflight", False):
            return

        no_voice_ms = now_ms - float(conn.last_activity_time)
        if no_voice_ms <= 1000.0 * timeout_sec:
            return

        if not farewell_cfg.get("enabled", True):
            conn.close_after_chat = True
            conn.client_abort = False
            conn.logger.bind(tag=recv_mod.TAG).info(
                f"无声超时 {timeout_sec}s，已关闭超时告别语，静默断开连接"
            )
            await conn.close()
            return

        mode = str(farewell_cfg.get("mode") or "fixed").strip().lower()
        fallback_messages = farewell_cfg.get("messages") or []

        if mode == "llm_summary":
            tts = getattr(conn, "tts", None)
            mei_active = tags.is_raiden_mei_voice(getattr(tts, "ref_audio_path", ""), tts=tts) if tts is not None else False
            has_turns = bool(extract_session_dialogue_snippet(conn, max_turns=1))
            if has_turns and getattr(conn, "llm", None) is not None:
                trigger_activity_ts = float(conn.last_activity_time)
                summary_prompt = str(farewell_cfg.get("summary_prompt") or "")
                conn._idle_farewell_inflight = True
                conn._idle_farewell_cancelled = False

                async def _run_llm_farewell_task():
                    try:
                        generated = await generate_llm_idle_farewell(
                            conn,
                            summary_prompt,
                            is_mei_voice=mei_active,
                            timeout_sec=6.0,
                        )
                        stop_ev = getattr(conn, "stop_event", None)
                        if (
                            getattr(conn, "_idle_farewell_cancelled", False)
                            or getattr(conn, "client_abort", False)
                            or getattr(conn, "close_after_chat", False)
                            or getattr(conn, "client_is_speaking", False)
                            or (stop_ev is not None and stop_ev.is_set())
                            or float(getattr(conn, "last_activity_time", 0.0)) > trigger_activity_ts
                        ):
                            conn.logger.bind(tag=recv_mod.TAG).info(
                                "生成超时告别语期间检测到用户重新活跃，已自动取消挂断"
                            )
                            return

                        if generated:
                            await _speak_or_close(
                                conn,
                                generated,
                                timeout_sec=timeout_sec,
                                default_emotion="tender",
                                reason="AI总结依恋告别",
                            )
                        else:
                            fb = select_idle_farewell_text(fallback_messages, last_text=_last_farewell["text"])
                            if fb:
                                _last_farewell["text"] = fb
                            await _speak_or_close(
                                conn,
                                fb,
                                timeout_sec=timeout_sec,
                                default_emotion="gentle",
                                reason="AI生成超时降级固定台词",
                            )
                    except Exception as exc:
                        conn.logger.bind(tag=recv_mod.TAG).warning(
                            f"AI 总结告别任务异常，尝试安全收尾: {exc}"
                        )
                        if not getattr(conn, "_idle_farewell_cancelled", False) and not getattr(
                            conn, "close_after_chat", False
                        ):
                            fb = select_idle_farewell_text(fallback_messages, last_text=_last_farewell["text"])
                            await _speak_or_close(
                                conn,
                                fb,
                                timeout_sec=timeout_sec,
                                default_emotion="gentle",
                                reason="异常兜底告别",
                            )
                    finally:
                        conn._idle_farewell_inflight = False

                conn._idle_farewell_task = asyncio.create_task(_run_llm_farewell_task())
                return

        chosen = select_idle_farewell_text(fallback_messages, last_text=_last_farewell["text"])
        if chosen:
            _last_farewell["text"] = chosen
        await _speak_or_close(
            conn,
            chosen,
            timeout_sec=timeout_sec,
            default_emotion="gentle",
            reason="无本轮对话降级固定台词" if mode == "llm_summary" else "固定随机台词",
        )

    recv_mod.no_voice_close_connect = _custom_no_voice_close_connect
    recv_mod._idle_farewell_patched = True
    rebound = patch_registry.rebind(_orig_no_voice_close, _custom_no_voice_close_connect)
    patch_registry.record_patch(
        "idle_farewell",
        "ok",
        target="receiveAudioHandle.no_voice_close_connect",
        detail=det_sig,
        contract_ok=True,
        category="experience",
        rebound=rebound,
    )
    print("[MeiEmotionRouter] 已挂载自定义空闲超时告别语补丁（固定随机 / AI总结依恋双模式 · 零记忆污染 · 18080 免重启热改）", flush=True)

"""Raiden Mei custom wakeup responses, interrupt-word zero-greeting fast path, and WAV breath trim (H-1)."""

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
from patches.emotion import splitter, tags, tts_route

MEI_WAKEUP_RESPONSES = [
    "我在呢，哥哥请说吧。",
    "在呢，哥哥有什么事呀？",
    "我在听呢，哥哥请讲。",
    "哥哥叫我吗？我在呢。",
    "在呢，怎么啦哥哥？",
    "我一直在呢，随时告诉我吧。",
]
MEI_WAKEUP_FALLBACK_TEXT = "我在呢，哥哥请说吧。"
MEI_WAKEUP_FALLBACK_WAV = os.path.join(tags.CONTAINER_VOICES_PREFIX, "raiden_mei", "wakeup_mei_default.wav")


def _trim_wav_leading_breath(wav_bytes: bytes) -> bytes:
    """Trim soft leading breath/silence from short GPT-SoVITS wakeup clips for instant response."""
    import io
    import math
    import struct
    import wave

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
                return wav_bytes
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
        win = max(1, int(sr * 0.02))
        onset = 0
        for i in range(0, len(samples) - win, win):
            rms = math.sqrt(sum(s * s for s in samples[i : i + win]) / win)
            if rms > 850:
                onset = max(0, i - int(sr * 0.04))
                break
        end = len(samples)
        for i in range(len(samples) - win, onset, -win):
            rms = math.sqrt(sum(s * s for s in samples[i : i + win]) / win)
            if rms > 250:
                end = min(len(samples), i + win + int(sr * 0.12))
                break
        if end - onset < int(sr * 0.35):
            return wav_bytes
        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(struct.pack(f"<{end - onset}h", *samples[onset:end]))
        return out.getvalue()
    except Exception:
        return wav_bytes


def get_wakeup_responses_for_tts(tts) -> list:
    if tts is not None and tags.is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")):
        return list(MEI_WAKEUP_RESPONSES)
    return []


def apply_mei_wakeup_responses_patch() -> None:
    """Call AFTER pc_audio_router.apply_pc_audio_playback_patches() so helloHandle import order is preserved."""
    import random
    import time
    import uuid
    import core.handle.helloHandle as hello_mod
    from core.providers.tts.dto.dto import SentenceType
    from core.utils.dialogue import Message

    if getattr(hello_mod, "_mei_wakeup_patched", False):
        if patch_registry.get_patch_status("mei_wakeup_responses") is None:
            patch_registry.record_patch("mei_wakeup_responses", "ok", target="helloHandle", detail="already applied")
        return

    _orig_check_wakeup = hello_mod.checkWakeupWords
    _orig_wakeup_response = hello_mod.wakeupWordsResponse
    _orig_preheat = hello_mod.preheat_wakeup_audio_cache
    _orig_handle_hello = getattr(hello_mod, "handleHelloMessage", None)
    _last_mei_reply = {"text": ""}

    wk_checks = [
        ("helloHandle.checkWakeupWords", _orig_check_wakeup, ("conn", "text"), True),
        ("helloHandle.wakeupWordsResponse", _orig_wakeup_response, ("conn",), True),
        ("helloHandle.preheat_wakeup_audio_cache", _orig_preheat, (), True),
    ]
    if _orig_handle_hello is not None:
        wk_checks.append(("helloHandle.handleHelloMessage", _orig_handle_hello, ("conn", "msg_json"), True))
    wk_failures = []
    for cname, cfn, req_p, req_a in wk_checks:
        ok_s, det = patch_registry.verify_signature_contract(cfn, required_params=req_p, require_async=req_a)
        if not ok_s:
            wk_failures.append(f"{cname}: {det}")

    @functools.wraps(_orig_wakeup_response)
    async def _mei_aware_wakeup_words_response(conn, *args, **kwargs):
        tts = getattr(conn, "tts", None)
        if tts is None or not tags.is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")):
            return await _orig_wakeup_response(conn, *args, **kwargs)

        # C-4: Run wakeup cache refresh ONLY when GSV is idle and lock is uncontended,
        # using `async with` + an immutable request snapshot (ZERO mutation of shared `tts`).
        if tts_route.is_live_gsv_busy() or hello_mod._wakeup_response_lock.locked():
            return

        async with hello_mod._wakeup_response_lock:
            if tts_route.is_live_gsv_busy():
                return

            pool = [r for r in MEI_WAKEUP_RESPONSES if r != _last_mei_reply["text"]] or list(MEI_WAKEUP_RESPONSES)
            result = random.choice(pool)
            _last_mei_reply["text"] = result

            url, req_json, _, _ = tts_route.build_gsv_request_snapshot(
                tts,
                result,
                explicit_segment_emotion="gentle",
                turn_explicit_emotion="gentle",
                is_first_segment=False,
                suppress_first_seg_fast_steps=True,
            )
            if not url:
                return

            timeout_sec = float(getattr(tts, "tts_timeout", None) or 30.0)
            raw_wav = await asyncio.to_thread(
                tts_route._synthesize_gsv_wakeup_snapshot_sync,
                url,
                req_json,
                timeout_sec,
            )
            if not raw_wav:
                return

            voice = getattr(tts, "voice", "raiden_mei") or "raiden_mei"
            wav_bytes = await asyncio.to_thread(_trim_wav_leading_breath, raw_wav)
            file_path = hello_mod.wakeup_words_config.generate_file_path(voice)
            await asyncio.to_thread(tts_route._write_bytes_atomic, file_path, wav_bytes)
            hello_mod.wakeup_words_config.update_wakeup_response(voice, file_path, result)
            await hello_mod.audio_to_data(file_path, use_cache=True)

    @functools.wraps(_orig_check_wakeup)
    async def _mei_aware_check_wakeup_words(conn, text, *args, **kwargs):
        tts = getattr(conn, "tts", None)
        # Wait briefly if TTS is still initializing (same behavior as upstream)
        if tts is None:
            start_t = time.time()
            while time.time() - start_t < 3:
                tts = getattr(conn, "tts", None)
                if tts is not None:
                    break
                await asyncio.sleep(0.1)

        if tts is None or not tags.is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")):
            matched = await _orig_check_wakeup(conn, text, *args, **kwargs)
            if matched and tts is not None:
                tts_route.prime_gsv_async(tts, reason="wakeup")
            return matched

        if not conn.config.get("enable_wakeup_words_response_cache"):
            return False

        _, filtered_text = hello_mod.remove_punctuation_and_length(text)
        if filtered_text not in (conn.config.get("wakeup_words") or []):
            return False

        # F-3: Trigger non-blocking GSV /prime on wakeup so memory pages warm up while user speaks
        tts_route.prime_gsv_async(tts, reason="wakeup")
        conn.just_woken_up = True
        await hello_mod.send_tts_message(conn, "start")

        voice = getattr(tts, "voice", "raiden_mei") or "raiden_mei"
        response = hello_mod.wakeup_words_config.get_wakeup_response(voice)
        if not response or not response.get("file_path"):
            fallback_wav = (
                MEI_WAKEUP_FALLBACK_WAV
                if os.path.isfile(MEI_WAKEUP_FALLBACK_WAV)
                else "config/assets/wakeup_words_short.wav"
            )
            response = {
                "voice": voice,
                "file_path": fallback_wav,
                "time": 0,
                "text": MEI_WAKEUP_FALLBACK_TEXT,
            }
            if os.path.isfile(MEI_WAKEUP_FALLBACK_WAV):
                try:
                    hello_mod.wakeup_words_config.update_wakeup_response(
                        voice, MEI_WAKEUP_FALLBACK_WAV, MEI_WAKEUP_FALLBACK_TEXT
                    )
                except Exception:
                    pass

        opus_packets = await hello_mod.audio_to_data(response.get("file_path"), use_cache=True)
        conn.client_abort = False
        conn.sentence_id = str(uuid.uuid4().hex)

        prof = tags.get_mei_emotion_profile("gentle")
        splitter._push_esp32_emotion_async(conn, prof["esp32_emoji"], prof["esp32_emotion"])

        conn.dialogue.put(Message(role="assistant", content=response.get("text")))
        hello_mod._wakeup_logger.bind(tag=hello_mod.TAG).info(
            f"播放芽衣唤醒词回复: {response.get('text')}"
        )

        await hello_mod.sendAudioMessage(conn, SentenceType.FIRST, opus_packets, response.get("text"))
        asyncio.create_task(hello_mod._finish_wakeup_reply(conn))

        if time.time() - response.get("time", 0) > hello_mod.WAKEUP_CONFIG["refresh_time"]:
            if not hello_mod._wakeup_response_lock.locked() and not tts_route.is_live_gsv_busy():
                asyncio.create_task(_mei_aware_wakeup_words_response(conn))
        return True

    @functools.wraps(_orig_preheat)
    async def _mei_aware_preheat(*args, **kwargs):
        await _orig_preheat(*args, **kwargs)
        try:
            resp = hello_mod.wakeup_words_config.get_wakeup_response("raiden_mei")
            fp = (resp or {}).get("file_path") or (
                MEI_WAKEUP_FALLBACK_WAV if os.path.isfile(MEI_WAKEUP_FALLBACK_WAV) else None
            )
            if fp and os.path.isfile(fp):
                await hello_mod.audio_to_data(fp, use_cache=True)
        except Exception:
            pass

    wk_mounted = not wk_failures
    lh_mounted = False
    if wk_failures:
        patch_registry.record_patch(
            "mei_wakeup_responses",
            "skipped_contract",
            target="helloHandle",
            detail="; ".join(wk_failures),
            contract_ok=False,
            category="experience",
        )
        print(
            f"[MeiEmotionRouter] 跳过挂载唤醒回复补丁（签名契约不匹配）: {'; '.join(wk_failures)}",
            flush=True,
        )
    else:
        hello_mod.wakeupWordsResponse = _mei_aware_wakeup_words_response
        hello_mod.checkWakeupWords = _mei_aware_check_wakeup_words
        hello_mod.preheat_wakeup_audio_cache = _mei_aware_preheat
        rebound_hello: list[str] = []
        if _orig_handle_hello is not None:
            @functools.wraps(_orig_handle_hello)
            async def _prime_aware_handle_hello(conn, msg_json, *args, **kwargs):
                tts_route.prime_gsv_async(getattr(conn, "tts", None), reason="hello")
                return await _orig_handle_hello(conn, msg_json, *args, **kwargs)

            hello_mod.handleHelloMessage = _prime_aware_handle_hello
            rebound_hello = patch_registry.rebind(_orig_handle_hello, _prime_aware_handle_hello)
        hello_mod._mei_wakeup_patched = True

        # P1-12: Rebind ALL already-imported module references across sys.modules
        rebound_wk = (
            patch_registry.rebind(_orig_check_wakeup, _mei_aware_check_wakeup_words)
            + patch_registry.rebind(_orig_wakeup_response, _mei_aware_wakeup_words_response)
            + patch_registry.rebind(_orig_preheat, _mei_aware_preheat)
            + rebound_hello
        )
        patch_registry.record_patch(
            "mei_wakeup_responses",
            "ok",
            target="helloHandle",
            detail="ok",
            contract_ok=True,
            category="experience",
            rebound=rebound_wk,
        )

    # Patch ListenTextMessageHandler so offline interrupt words (e.g. "听我说") switch straight to
    # listening with ZERO greeting TTS and ZERO 2s just_woken_up VAD suppression (even from IDLE),
    # and publish listen_start to runtime_events (P1-11).
    try:
        import core.handle.textHandler.listenMessageHandler as listen_mod
        import core.handle.abortHandle as abort_mod

        cls = getattr(listen_mod, "ListenTextMessageHandler", None)
        if cls is not None and not getattr(cls, "_interrupt_word_patched", False):
            _orig_listen_handle = cls.handle
            ok_lh, det_lh = patch_registry.verify_signature_contract(
                _orig_listen_handle, required_params=("self", "conn", "msg_json"), require_async=True
            )

            @functools.wraps(_orig_listen_handle)
            async def _interrupt_aware_listen_handle(self, conn, msg_json, *args, **kwargs):
                if isinstance(msg_json, dict) and msg_json.get("state") == "start":
                    try:
                        runtime_events.publish_event(
                            "listen_start",
                            device_id=str(getattr(conn, "device_id", "") or ""),
                        )
                    except Exception:
                        pass
                if isinstance(msg_json, dict) and msg_json.get("state") == "detect" and "text" in msg_json:
                    original_text = str(msg_json.get("text") or "")
                    _, filtered_text = listen_mod.remove_punctuation_and_length(original_text)
                    cfg_words = (getattr(conn, "config", None) or {}).get("interrupt_words")
                    if not isinstance(cfg_words, (list, tuple, set)):
                        cfg_words = ("听我说",)
                    norm_words = {
                        listen_mod.remove_punctuation_and_length(str(w))[1]
                        for w in cfg_words
                        if str(w).strip()
                    }
                    if filtered_text and filtered_text in norm_words:
                        tts_route.prime_gsv_async(getattr(conn, "tts", None), reason="interrupt")
                        conn.client_have_voice = False
                        conn.reset_audio_states()
                        conn.last_activity_time = time.time() * 1000
                        conn.just_woken_up = False
                        if (
                            getattr(conn, "client_is_speaking", False)
                            or getattr(conn, "_pc_audio_active_sid", None) is not None
                        ):
                            try:
                                await abort_mod.handleAbortMessage(conn)
                            except Exception:
                                pass
                            if hasattr(conn, "_pc_audio_active_sid"):
                                conn._pc_audio_active_sid = None
                        await listen_mod.send_stt_message(conn, original_text)
                        await hello_mod.send_tts_message(conn, "stop", None)
                        conn.client_is_speaking = False
                        conn.logger.bind(tag=listen_mod.TAG).info(
                            f"收到打断词「{original_text}」，已静音并直接进入聆听模式"
                        )
                        return
                return await _orig_listen_handle(self, conn, msg_json, *args, **kwargs)

            if not ok_lh:
                patch_registry.record_patch(
                    "listen_interrupt_handler",
                    "skipped_contract",
                    target="ListenTextMessageHandler.handle",
                    detail=det_lh,
                    contract_ok=False,
                    category="experience",
                )
                print(
                    f"[MeiEmotionRouter] 跳过挂载打断词补丁（签名契约不匹配）: {det_lh}",
                    flush=True,
                )
            else:
                cls.handle = _interrupt_aware_listen_handle
                cls._interrupt_word_patched = True
                lh_mounted = True
                patch_registry.record_patch(
                    "listen_interrupt_handler",
                    "ok",
                    target="ListenTextMessageHandler.handle",
                    detail=det_lh,
                    contract_ok=True,
                    category="experience",
                )
    except Exception as exc:
        patch_registry.record_patch(
            "listen_interrupt_handler",
            "failed",
            target="ListenTextMessageHandler.handle",
            detail=str(exc),
            contract_ok=False,
            category="experience",
        )
        print(f"[MeiEmotionRouter] 挂载打断词直通聆听补丁跳过: {exc}", flush=True)

    if wk_mounted or lh_mounted:
        mounted_parts = []
        if wk_mounted:
            mounted_parts.append("芽衣专属唤醒词秒回语 (raiden_mei 独立缓存 + gentle 温柔音色)")
        if lh_mounted:
            mounted_parts.append("打断词直通聆听补丁")
        print(f"[MeiEmotionRouter] 已挂载{' + '.join(mounted_parts)}", flush=True)

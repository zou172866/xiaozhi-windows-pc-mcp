"""GPT-SoVITS v4 emotion reference routing, immutable snapshot synthesis, /prime warmup, and TTS patches (H-1)."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import wave

import patch_registry
import runtime_events
import runtime_settings
import xz_common
from patches.emotion import prompt, splitter, tags

GSV_PRIME_URL = os.environ.get("GSV_PRIME_URL", "http://host.docker.internal:19880/prime").strip()
_gsv_prime_lock = threading.Lock()
_gsv_last_prime_ts: float = 0.0

_gsv_session_lock = threading.Lock()
_gsv_session = None

# Shared GPU synthesis/weights lock between live ESP32 TTS and 18080 Console preview (P1-14)
GSV_GPU_LOCK = threading.RLock()
_gsv_inflight_lock = threading.Lock()
_gsv_live_inflight: int = 0

DEFAULT_FIRST_SEG_SAMPLE_STEPS = 8


def is_live_gsv_busy() -> bool:
    """Return True while a live ESP32 GPT-SoVITS synthesis request is in flight."""
    with _gsv_inflight_lock:
        return _gsv_live_inflight > 0


@contextlib.contextmanager
def _try_acquire_gsv_gpu_lock():
    """Non-blocking context manager around GSV_GPU_LOCK so background jobs never stall live TTS."""
    acquired = GSV_GPU_LOCK.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            GSV_GPU_LOCK.release()


def is_gsv_placeholder_zero_wav(wav_bytes: bytes | None) -> bool:
    """Return True if `wav_bytes` matches upstream TTS.py's `yield 16000, np.zeros(16000, int16)` error placeholder (R2 P0-2)."""
    if not wav_bytes or len(wav_bytes) < 44 or not wav_bytes.startswith(b"RIFF"):
        return False
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            sr = wf.getframerate()
            nframes = wf.getnframes()
            sampwidth = wf.getsampwidth()
            if sr != 16000 or sampwidth != 2 or not (15500 <= nframes <= 16500):
                return False
            raw = wf.readframes(nframes)
        return len(raw) > 0 and raw == (b"\x00" * len(raw))
    except Exception:
        return False


def _post_gsv_sync(url: str, request_json: Dict[str, Any], timeout: float, *, provider=None):
    """Run blocking GPT-SoVITS HTTP POST + GSV_GPU_LOCK on a worker thread, with CUDA Graph OOM zero-WAV retry (C-5, R2 P0-2)."""
    sess = _get_gsv_http_session()
    with GSV_GPU_LOCK:
        resp = sess.post(url, json=request_json, timeout=timeout)
        if (
            getattr(resp, "status_code", 0) == 200
            and is_gsv_placeholder_zero_wav(getattr(resp, "content", None))
            and request_json.get("use_cuda_graph") is not False
        ):
            if provider is not None:
                try:
                    provider._force_disable_cuda_graph = True
                except Exception:
                    pass
            retry_json = dict(request_json)
            retry_json["use_cuda_graph"] = False
            print(
                "[GPT-SoVITS] 检测到 16kHz 全零异常占位帧（疑似 CUDA Graph 内存不足），已自动切换 use_cuda_graph=False 重试",
                flush=True,
            )
            resp = sess.post(url, json=retry_json, timeout=timeout)
        return resp


def _synthesize_gsv_wakeup_snapshot_sync(
    url: str,
    request_json: Dict[str, Any],
    timeout: float,
) -> Optional[bytes]:
    """Synthesize a wakeup cache WAV ONLY when GSV is idle and GPU lock is uncontended (C-4)."""
    if is_live_gsv_busy():
        return None
    with _try_acquire_gsv_gpu_lock() as acquired:
        if not acquired or is_live_gsv_busy():
            return None
        sess = _get_gsv_http_session()
        resp = sess.post(url, json=request_json, timeout=timeout)
        if (
            getattr(resp, "status_code", 0) == 200
            and is_gsv_placeholder_zero_wav(getattr(resp, "content", None))
            and request_json.get("use_cuda_graph") is not False
        ):
            retry_json = dict(request_json)
            retry_json["use_cuda_graph"] = False
            resp = sess.post(url, json=retry_json, timeout=timeout)
        if getattr(resp, "status_code", 0) == 200 and getattr(resp, "content", None):
            content = bytes(resp.content)
            if is_gsv_placeholder_zero_wav(content):
                return None
            return content
        return None


def _write_bytes_atomic(file_path: str, data: bytes) -> None:
    parent = os.path.dirname(os.path.abspath(file_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{file_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_file_bytes(file_path: str, data: bytes) -> None:
    with open(file_path, "wb") as f:
        f.write(data)


def _connection_mood_key(conn):
    target = getattr(conn, "_anima_memory_target", None)
    return target.mood_key if target else (str(getattr(conn, "device_id", "") or "").strip() or None)


def build_gsv_request_snapshot(
    provider,
    clean_text: str,
    *,
    explicit_segment_emotion: Optional[str] = None,
    turn_explicit_emotion: Optional[str] = None,
    device_id: Optional[tags.MoodKey] = None,
    is_first_segment: bool = False,
    suppress_first_seg_fast_steps: bool = False,
) -> Tuple[str, Dict[str, Any], Optional[Dict[str, Any]], Optional[str]]:
    """Build an immutable GPT-SoVITS /tts request dict from `provider` without mutating `provider` (C-4)."""
    ref_audio_path = getattr(provider, "ref_audio_path", "")
    aux_ref_audio_paths = list(getattr(provider, "aux_ref_audio_paths", None) or [])
    prompt_text = getattr(provider, "prompt_text", "")
    prompt_lang = getattr(provider, "prompt_lang", "zh")
    top_k = getattr(provider, "top_k", 12)
    top_p = getattr(provider, "top_p", 1.0)
    temperature = getattr(provider, "temperature", 0.98)
    speed_factor = getattr(provider, "speed_factor", 1.0)
    raw_split = getattr(provider, "text_split_method", "cut0")
    # Avoid double-splitting inside GPT-SoVITS when clauses are already segmented
    split_method = "cut0" if raw_split in ("cut5", "cut0", "", None) else raw_split

    chosen_prof: Optional[Dict[str, Any]] = None
    emo_key: Optional[str] = None
    char_key = tags.resolve_emotion_character_key(ref_audio_path, tts=provider)
    is_mei = char_key == "raiden_mei"
    if char_key is not None:
        emo_key = tags.resolve_mei_emotion(
            clean_text,
            explicit_segment_emotion=explicit_segment_emotion,
            turn_explicit_emotion=turn_explicit_emotion,
            device_id=device_id,
            character_key=char_key,
        )
        prof = tags.get_character_emotion_profile(char_key, emo_key)
        chosen_prof = prof
        ref_audio_path = prof["ref_audio_path"]
        aux_ref_audio_paths = []
        prompt_text = prof["prompt_text"]
        prompt_lang = prof.get("prompt_lang", "zh")
        speed_factor = float(prof.get("speed_factor", speed_factor))
        temperature = float(prof.get("temperature", temperature))
        top_k = int(prof.get("top_k", top_k))
        top_p = float(prof.get("top_p", top_p))
        split_method = "cut0"

    request_json: Dict[str, Any] = {
        "text": clean_text,
        "text_lang": getattr(provider, "text_lang", "zh"),
        "ref_audio_path": ref_audio_path,
        "aux_ref_audio_paths": aux_ref_audio_paths,
        "prompt_text": prompt_text,
        "prompt_lang": prompt_lang,
        "top_k": top_k,
        "top_p": top_p,
        "temperature": temperature,
        "text_split_method": split_method,
        "batch_size": getattr(provider, "batch_size", 1),
        "return_fragment": getattr(provider, "return_fragment", False),
        "speed_factor": speed_factor,
        "streaming_mode": getattr(provider, "streaming_mode", False),
        "seed": getattr(provider, "seed", -1),
        "parallel_infer": getattr(provider, "parallel_infer", True),
        "repetition_penalty": getattr(provider, "repetition_penalty", 1.35),
    }
    effective_first = False if suppress_first_seg_fast_steps else bool(is_first_segment)
    sample_steps = _resolve_gsv_sample_steps(
        provider,
        chosen_prof,
        is_mei,
        is_first_segment=effective_first,
    )
    if sample_steps is not None:
        request_json["sample_steps"] = sample_steps
    if getattr(provider, "_force_disable_cuda_graph", False):
        request_json["use_cuda_graph"] = False

    url = str(getattr(provider, "url", "") or "").strip()
    return url, request_json, chosen_prof, emo_key


def _get_gsv_http_session():
    """Return a persistent keep-alive requests.Session with trust_env=False for local 19880 TTS."""
    global _gsv_session
    if _gsv_session is None:
        with _gsv_session_lock:
            if _gsv_session is None:
                import requests

                sess = requests.Session()
                sess.trust_env = False
                _gsv_session = sess
    return _gsv_session


def _resolve_gsv_sample_steps(
    provider,
    chosen_prof: Optional[Dict[str, Any]],
    is_mei: bool,
    *,
    is_first_segment: bool = False,
) -> Optional[int]:
    cand = None
    if isinstance(chosen_prof, dict) and chosen_prof.get("sample_steps") is not None:
        cand = chosen_prof.get("sample_steps")
    elif getattr(provider, "sample_steps", None) is not None:
        cand = getattr(provider, "sample_steps")
    elif isinstance(getattr(provider, "config", None), dict) and provider.config.get("sample_steps") is not None:
        cand = provider.config.get("sample_steps")
    elif is_mei:
        env_raw = (os.environ.get("GSV_V4_SAMPLE_STEPS") or "").strip()
        cand = env_raw if env_raw else 16
    if cand in (None, ""):
        return None
    try:
        base_steps = max(4, min(64, int(cand)))
    except Exception:
        base_steps = 16 if is_mei else None
    if base_steps is None:
        return None
    # R2 P1-1: Auto-heal legacy unmigrated V5-Turbo config where sample_steps remained Mei's 16
    # alongside default/unmigrated first_segment_sample_steps (None, 4, or Mei's 8)
    healed_lxh_legacy = False
    if base_steps == 16 and tags.is_luoxiaohei_voice(tts=provider):
        cfg_d = getattr(provider, "config", None)
        fast_raw = getattr(provider, "first_segment_sample_steps", None)
        if fast_raw is None and isinstance(cfg_d, dict):
            fast_raw = cfg_d.get("first_segment_sample_steps")
        if fast_raw in (None, "", 4, 8):
            base_steps = 4
            healed_lxh_legacy = True

    # P1-10: Lower first-segment DiT sample_steps (default 8) to cut first-audio latency while keeping
    # full quality on segment 2+ (which synthesize concurrently while segment 1 plays).
    if is_first_segment and not getattr(provider, "_suppress_first_seg_fast_steps", False):
        if healed_lxh_legacy:
            return 4
        fast_cand = getattr(provider, "first_segment_sample_steps", None)
        if fast_cand is None and isinstance(getattr(provider, "config", None), dict):
            fast_cand = provider.config.get("first_segment_sample_steps")
        if fast_cand in (None, ""):
            env_fast = (os.environ.get("GSV_FIRST_SEG_SAMPLE_STEPS") or "").strip()
            fast_cand = env_fast if env_fast else DEFAULT_FIRST_SEG_SAMPLE_STEPS
        try:
            fast_steps = max(4, min(base_steps, int(fast_cand)))
            return fast_steps
        except Exception:
            return min(base_steps, DEFAULT_FIRST_SEG_SAMPLE_STEPS)
    return base_steps


def _resolve_gsv_prime_url(tts=None) -> str:
    raw_url = str(getattr(tts, "url", "") or "").strip() if tts is not None else ""
    if raw_url:
        if raw_url.endswith("/tts"):
            return raw_url[:-4] + "/prime"
        if "/tts?" in raw_url:
            return raw_url.split("/tts?", 1)[0] + "/prime"
    return GSV_PRIME_URL


def prime_gsv_async(tts=None, *, reason: str = "wakeup", min_interval_sec: float = 12.0) -> bool:
    """Fire-and-forget async /prime ping to host 19880 on ESP32 wakeup/hello to page in warm memory (F-3)."""
    global _gsv_last_prime_ts
    if tts is not None:
        cls_mod = str(getattr(type(tts), "__module__", "") or "").lower()
        if cls_mod and "gpt_sovits" not in cls_mod and not tags.is_emotion_routed_gsv_voice(getattr(tts, "ref_audio_path", ""), tts=tts):
            return False

    now = time.monotonic()
    with _gsv_prime_lock:
        if min_interval_sec > 0 and (now - _gsv_last_prime_ts) < float(min_interval_sec):
            return False
        _gsv_last_prime_ts = now

    prime_url = _resolve_gsv_prime_url(tts)

    def _worker():
        try:
            sess = _get_gsv_http_session()
            sess.post(
                prime_url,
                json={"skip_if_busy": True, "reason": str(reason or "wakeup")},
                timeout=2.0,
            )
        except Exception:
            pass

    try:
        threading.Thread(target=_worker, name=f"gsv-prime-{reason}", daemon=True).start()
        return True
    except Exception:
        return False


def apply_xiaozhi_emotion_and_prosody_patches() -> None:
    """Patch xiaozhi-esp32-server TTS streaming, GPT-SoVITS v2/v4 emotion routing,
    direct_answer chunk cleaner, prompt harmony, subtitle firewall, and playback-synced
    ESP32 emotion indicator in memory at container startup."""
    from core.providers.tts.base import TTSProviderBase, logger as base_logger
    from core.providers.tts import gpt_sovits_v2 as gsv_mod
    import core.handle.sendAudioHandle as send_audio_mod
    import core.utils.textUtils as text_utils_mod
    from core.connection import ConnectionHandler
    from core.utils.prompt_manager import PromptManager

    if getattr(TTSProviderBase, "_mei_prosody_patched", False):
        if patch_registry.get_patch_status("mei_prompt_anchors") is None:
            anchors_status, anchors_detail, _applied_a, _missing_a = prompt.dry_run_prompt_harmony()
            patch_registry.record_patch(
                "mei_prompt_anchors",
                anchors_status,
                target="agent-base-prompt.txt",
                detail=anchors_detail,
                contract_ok=(anchors_status == "ok"),
                category="experience",
            )
        if patch_registry.get_patch_status("mei_emotion_and_prosody") is None:
            patch_registry.record_patch("mei_emotion_and_prosody", "ok", target="TTSProviderBase/GSV", detail="already applied", category="experience")
        return

    # P1-8: Startup contract verification for upstream targets + prompt anchors
    contract_checks = [
        ("ConnectionHandler._clean_response_garbage", ConnectionHandler.__dict__.get("_clean_response_garbage", getattr(ConnectionHandler, "_clean_response_garbage", None)), ("text",), False),
        ("PromptManager.build_enhanced_prompt", PromptManager.build_enhanced_prompt, ("self", "user_prompt", "device_id"), False),
        ("ConnectionHandler._inject_tool_call_fewshot", ConnectionHandler._inject_tool_call_fewshot, ("self",), False),
        ("TTSProviderBase.store_tts_text", TTSProviderBase.store_tts_text, ("self", "sentence_id", "text"), False),
        ("TTSProviderBase._get_segment_text", TTSProviderBase._get_segment_text, ("self",), False),
        ("TTSProviderBase._process_remaining_text_stream", TTSProviderBase._process_remaining_text_stream, ("self",), False),
        ("TTSProviderBase.to_tts_stream", TTSProviderBase.to_tts_stream, ("self", "text"), False),
        ("TTSProviderBase.to_tts", TTSProviderBase.to_tts, ("self", "text"), False),
        ("GSV.__init__", gsv_mod.TTSProvider.__init__, ("self", "config", "delete_audio_file"), False),
        ("GSV.text_to_speak", gsv_mod.TTSProvider.text_to_speak, ("self", "text", "output_file"), True),
        ("sendAudioHandle.send_tts_message", send_audio_mod.send_tts_message, ("conn", "state"), True),
        ("textUtils.get_emotion", text_utils_mod.get_emotion, ("conn", "text"), True),
    ]
    contract_failures = []
    for cname, cfn, req_params, req_async in contract_checks:
        ok_sig, sig_detail = patch_registry.verify_signature_contract(
            cfn, required_params=req_params, require_async=req_async
        )
        if not ok_sig:
            contract_failures.append(f"{cname}: {sig_detail}")

    anchors_status, anchors_detail, _applied_a, _missing_a = prompt.dry_run_prompt_harmony()
    patch_registry.record_patch(
        "mei_prompt_anchors",
        anchors_status,
        target="agent-base-prompt.txt",
        detail=anchors_detail,
        contract_ok=(anchors_status == "ok"),
        category="experience",
    )

    if contract_failures:
        patch_registry.record_patch(
            "mei_emotion_and_prosody",
            "skipped_contract",
            target="TTSProviderBase/GSV/send_tts_message/PromptManager",
            detail="; ".join(contract_failures),
            contract_ok=False,
            category="experience",
        )
        print(
            f"[MeiEmotionRouter] 跳过挂载情绪与韵律补丁（签名契约不匹配）: {'; '.join(contract_failures)}",
            flush=True,
        )
        return

    # 0. Fix upstream direct_answer delta cleaner eating closing `]` on [gentle] / [playful] / [安全挡]
    ConnectionHandler._clean_response_garbage = staticmethod(tags.safe_clean_response_garbage)

    # 0b. Harmonize agent-base-prompt.txt & direct_answer few-shot when [emotion] tag mode is active
    _orig_build_enhanced_prompt = PromptManager.build_enhanced_prompt
    _orig_inject_fewshot = ConnectionHandler._inject_tool_call_fewshot

    @functools.wraps(_orig_build_enhanced_prompt)
    def _patched_build_enhanced_prompt(
        self, user_prompt: str, device_id: str, *args, **kwargs
    ) -> str:
        if tags.has_voice_emotion_tag_rule(user_prompt):
            kw = dict(kwargs)
            kw["emoji_enabled"] = False
            rendered = _orig_build_enhanced_prompt(
                self, user_prompt, device_id, *args, **kw
            )
            return prompt.apply_prompt_harmony_replacements(rendered)
        return _orig_build_enhanced_prompt(
            self, user_prompt, device_id, *args, **kwargs
        )

    @functools.wraps(_orig_inject_fewshot)
    def _patched_inject_tool_call_fewshot(self, *args, **kwargs):
        res = _orig_inject_fewshot(self, *args, **kwargs)
        try:
            prompt_text = str((self.config or {}).get("prompt") or "")
            if tags.has_voice_emotion_tag_rule(prompt_text):
                fewshot_resp = (
                    "[gentle] 好呀，哥哥想听什么类型的故事呢？轻松一点的日常，还是奇幻冒险的？"
                    if "哥哥" in prompt_text
                    else "[gentle] 好呀，你想听什么类型的故事呢？轻松一点的日常，还是奇幻冒险的？"
                )
                for m in getattr(self.dialogue, "dialogue", []) or []:
                    if getattr(m, "is_temporary", False) and getattr(m, "tool_calls", None):
                        for tc in m.tool_calls:
                            fn = tc.get("function") if isinstance(tc, dict) else None
                            if isinstance(fn, dict) and fn.get("name") == "direct_answer":
                                fn["arguments"] = json.dumps(
                                    {"response": fewshot_resp},
                                    ensure_ascii=False,
                                )
        except Exception:
            pass
        return res

    PromptManager.build_enhanced_prompt = _patched_build_enhanced_prompt
    ConnectionHandler._inject_tool_call_fewshot = _patched_inject_tool_call_fewshot

    # 1. Patch TTSProviderBase.tts_text_priority_thread FIRST reset + smooth segmentation
    _orig_store_tts_text = TTSProviderBase.store_tts_text
    _orig_get_segment_text = TTSProviderBase._get_segment_text
    _orig_process_remaining = TTSProviderBase._process_remaining_text_stream
    _orig_to_tts = TTSProviderBase.to_tts
    _orig_to_tts_stream = TTSProviderBase.to_tts_stream

    @functools.wraps(_orig_store_tts_text)
    def _patched_store_tts_text(self, sentence_id, text, *args, **kwargs):
        clean = tags.strip_emotion_tags(text or "")
        return _orig_store_tts_text(self, sentence_id, clean, *args, **kwargs)

    @functools.wraps(_orig_get_segment_text)
    def _patched_get_segment_text(self, *args, **kwargs) -> Optional[str]:
        while True:
            full_text = "".join(self.tts_text_buff)
            current_text = full_text[self.processed_chars :]
            if not current_text:
                return None

            # Reset per-turn emotion & first-segment state when a brand-new sentence_id starts
            cur_sid = getattr(self, "current_sentence_id", None) or getattr(
                getattr(self, "conn", None), "sentence_id", None
            )
            if getattr(self, "_emotion_turn_sid", None) != cur_sid:
                self._emotion_turn_sid = cur_sid
                self._turn_explicit_emotion = None
                self._pending_segment_emotion = None
                self._segment_emotion_queue = []
                self._first_segment_done_sid = None
                self.is_first_sentence = True
                self.tts_audio_first_sentence = True

            # Absorb leading tag early ONLY for the very first unspoken sentence of the turn
            s_lead = tags._strip_leading_ws_and_emojis(current_text)
            m_lead = tags._BRACKET_CANDIDATE_RE.match(s_lead)
            if m_lead:
                emo_cand, should_strip = tags.normalize_emotion_name(
                    next((g for g in m_lead.groups() if g is not None), "")
                )
                if should_strip and emo_cand:
                    if self._turn_explicit_emotion is None:
                        self._turn_explicit_emotion = emo_cand
                    conn = getattr(self, "conn", None)
                    if (
                        self.is_first_sentence
                        and getattr(self, "tts_audio_first_sentence", True)
                        and getattr(conn, "_last_pushed_mei_sid", None) != cur_sid
                    ):
                        prof = tags.get_mei_emotion_profile(emo_cand, require_ref_audio=False)
                        splitter._push_esp32_emotion_async(
                            conn,
                            prof["esp32_emoji"],
                            prof["esp32_emotion"],
                        )

            cut_pos = splitter.find_prosody_cut_pos(
                current_text, self.is_first_sentence, self._quote_safe_cut
            )
            if cut_pos != -1:
                segment_raw = current_text[: cut_pos + 1]
                self.processed_chars += len(segment_raw)
                seg_emo, stripped = tags.extract_and_strip_emotion(segment_raw, flush_incomplete=False)
                if seg_emo:
                    self._turn_explicit_emotion = seg_emo
                    self._pending_segment_emotion = seg_emo
                cleaned = tags.clean_segment_for_tts(stripped)
                if not cleaned:
                    # Pure emotion tag line (e.g. "[gentle]\n"): loop without flipping is_first_sentence
                    continue
                if self.is_first_sentence:
                    self.is_first_sentence = False
                return cleaned

            if self.tts_stop_request and current_text:
                self.processed_chars = len(full_text)
                seg_emo, stripped = tags.extract_and_strip_emotion(current_text, flush_incomplete=True)
                if seg_emo:
                    self._turn_explicit_emotion = seg_emo
                    self._pending_segment_emotion = seg_emo
                self.is_first_sentence = True
                cleaned = tags.clean_segment_for_tts(stripped)
                return cleaned or None

            return None

    @functools.wraps(_orig_process_remaining)
    def _patched_process_remaining_text_stream(self, *args, **kwargs):
        opus_handler = kwargs.get("opus_handler", args[0] if args else None)
        did_any = False
        # First drain all complete prosodic sentences individually (preserving per-sentence emotion tags)
        while True:
            seg = self._get_segment_text()
            if not seg:
                break
            self.to_tts_stream(seg, opus_handler=opus_handler)
            did_any = True

        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
        self.processed_chars = len(full_text)
        if not remaining_text:
            return did_any
        cur_sid = getattr(self, "current_sentence_id", None) or getattr(
            getattr(self, "conn", None), "sentence_id", None
        )
        if getattr(self, "_emotion_turn_sid", None) != cur_sid:
            self._emotion_turn_sid = cur_sid
            self._turn_explicit_emotion = None
            self._pending_segment_emotion = None
            self._segment_emotion_queue = []
            self._first_segment_done_sid = None
        seg_emo, stripped = tags.extract_and_strip_emotion(remaining_text, flush_incomplete=True)
        if seg_emo:
            self._turn_explicit_emotion = seg_emo
            self._pending_segment_emotion = seg_emo
        segment_text = tags.clean_segment_for_tts(stripped)
        if segment_text:
            self.to_tts_stream(segment_text, opus_handler=opus_handler)
            return True
        return did_any

    def _enqueue_common_segment_emotion(tts_obj, clean_text: str, explicit_seg: Optional[str] = None) -> None:
        """Enqueue & push 7-emotion ESP32 OLED state for non-GSV TTS providers (EdgeTTS & Cloud HTTP TTS, P1-1)."""
        if getattr(tts_obj, "_handles_own_segment_emotion", False):
            return
        conn = getattr(tts_obj, "conn", None)
        eff_emo = (
            explicit_seg
            or getattr(tts_obj, "_pending_segment_emotion", None)
            or getattr(tts_obj, "_turn_explicit_emotion", None)
        )
        tts_obj._pending_segment_emotion = None
        if not eff_emo and conn is not None:
            prompt_str = str((getattr(conn, "config", None) or {}).get("prompt") or "")
            if tags.has_voice_emotion_tag_rule(prompt_str):
                eff_emo = tags.resolve_mei_emotion(
                    clean_text, device_id=_connection_mood_key(conn), require_ref_audio=False
                )
        if not eff_emo:
            return
        prof = tags.get_mei_emotion_profile(eff_emo, require_ref_audio=False)
        cur_sid = getattr(tts_obj, "current_sentence_id", None) or getattr(conn, "sentence_id", None)
        q = getattr(tts_obj, "_segment_emotion_queue", None)
        if q is None:
            q = []
            tts_obj._segment_emotion_queue = q
        q.append((cur_sid, clean_text, prof["esp32_emoji"], prof["esp32_emotion"]))
        if len(q) > 64:
            del q[:-64]
        if (
            conn is not None
            and getattr(tts_obj, "tts_audio_first_sentence", True)
            and getattr(conn, "_last_pushed_mei_sid", None) != cur_sid
        ):
            splitter._push_esp32_emotion_async(conn, prof["esp32_emoji"], prof["esp32_emotion"])

    @functools.wraps(_orig_to_tts_stream)
    def _patched_to_tts_stream(self, text, *args, **kwargs):
        seg_emo, stripped = tags.extract_and_strip_emotion(text or "", flush_incomplete=True)
        if seg_emo:
            self._turn_explicit_emotion = seg_emo
            self._pending_segment_emotion = seg_emo
        cleaned = tags.clean_segment_for_tts(stripped)
        if not cleaned:
            return None
        _enqueue_common_segment_emotion(self, cleaned, seg_emo)
        return _orig_to_tts_stream(self, cleaned, *args, **kwargs)

    @functools.wraps(_orig_to_tts)
    def _patched_to_tts(self, text, *args, **kwargs):
        seg_emo, stripped = tags.extract_and_strip_emotion(text or "", flush_incomplete=True)
        if seg_emo:
            self._turn_explicit_emotion = seg_emo
            self._pending_segment_emotion = seg_emo
        cleaned = tags.clean_segment_for_tts(stripped)
        if not cleaned:
            return None
        return _orig_to_tts(self, cleaned, *args, **kwargs)

    TTSProviderBase.store_tts_text = _patched_store_tts_text
    TTSProviderBase._get_segment_text = _patched_get_segment_text
    TTSProviderBase._process_remaining_text_stream = _patched_process_remaining_text_stream
    TTSProviderBase.to_tts_stream = _patched_to_tts_stream
    TTSProviderBase.to_tts = _patched_to_tts

    # 2. Patch GPT_SOVITS_V2.TTSProvider for dynamic multi-emotion reference routing + cut0 + V4 fast first-seg sample_steps + Session keep-alive
    _orig_gsv_init = gsv_mod.TTSProvider.__init__
    _orig_gsv_text_to_speak = gsv_mod.TTSProvider.text_to_speak

    @functools.wraps(_orig_gsv_init)
    def _patched_gsv_init(self, config, delete_audio_file, *args, **kwargs):
        _orig_gsv_init(self, config, delete_audio_file, *args, **kwargs)
        raw_vid = (config or {}).get("voice_id")
        self.voice_id = raw_vid.strip() if isinstance(raw_vid, str) else ""
        raw_steps = (config or {}).get("sample_steps")
        if raw_steps not in (None, ""):
            try:
                self.sample_steps = max(4, min(64, int(raw_steps)))
            except Exception:
                self.sample_steps = None
        else:
            self.sample_steps = None
        raw_fast = (config or {}).get("first_segment_sample_steps")
        if raw_fast not in (None, ""):
            try:
                self.first_segment_sample_steps = max(4, min(64, int(raw_fast)))
            except Exception:
                self.first_segment_sample_steps = None
        else:
            self.first_segment_sample_steps = None

    gsv_mod.TTSProvider.__init__ = _patched_gsv_init

    def _prepare_gsv_segment(self, text):
        seg_emo, stripped = tags.extract_and_strip_emotion(text or "", flush_incomplete=True)
        clean_text = tags.clean_segment_for_tts(stripped)
        if not clean_text:
            return None

        conn = getattr(self, "conn", None)
        cur_sid = getattr(self, "current_sentence_id", None) or getattr(conn, "sentence_id", None)
        is_first_segment = bool(
            cur_sid
            and getattr(self, "_first_segment_done_sid", None) != cur_sid
            and not getattr(self, "_suppress_first_seg_fast_steps", False)
        )

        pending_emo = getattr(self, "_pending_segment_emotion", None)
        explicit_seg = seg_emo or pending_emo
        turn_emo = getattr(self, "_turn_explicit_emotion", None)
        req_url, request_json, chosen_prof, emo_key = build_gsv_request_snapshot(
            self,
            clean_text,
            explicit_segment_emotion=explicit_seg,
            turn_explicit_emotion=turn_emo,
            device_id=_connection_mood_key(conn),
            is_first_segment=is_first_segment,
            suppress_first_seg_fast_steps=bool(getattr(self, "_suppress_first_seg_fast_steps", False)),
        )
        if chosen_prof is not None and emo_key:
            base_logger.bind(tag=gsv_mod.TAG).info(
                f"[芽衣多情绪路由] 情绪={emo_key}({chosen_prof['label_zh']}) | speed={request_json['speed_factor']} | 文本={clean_text}"
            )
        return clean_text, conn, cur_sid, is_first_segment, req_url, request_json, chosen_prof

    def _finalize_gsv_response(self, resp, clean_text, conn, cur_sid, is_first_segment, chosen_prof) -> bytes:
        if resp.status_code == 200:
            if is_gsv_placeholder_zero_wav(getattr(resp, "content", None)):
                err_zero = "GPT_SoVITS_V2 返回 16kHz 全零异常占位音频（疑似 CUDA Graph 或显存/内存不足），已拦截静音废音"
                base_logger.bind(tag=gsv_mod.TAG).error(err_zero)
                raise RuntimeError(err_zero)
            self._pending_segment_emotion = None
            if cur_sid and is_first_segment:
                self._first_segment_done_sid = cur_sid
            if chosen_prof is not None:
                q = getattr(self, "_segment_emotion_queue", None)
                if q is None:
                    q = []
                    self._segment_emotion_queue = q
                q.append(
                    (
                        cur_sid,
                        clean_text,
                        chosen_prof["esp32_emoji"],
                        chosen_prof["esp32_emotion"],
                    )
                )
                if len(q) > 64:
                    del q[:-64]
                if getattr(self, "tts_audio_first_sentence", True) and getattr(
                    conn, "_last_pushed_mei_sid", None
                ) != cur_sid:
                    splitter._push_esp32_emotion_async(
                        conn,
                        chosen_prof["esp32_emoji"],
                        chosen_prof["esp32_emotion"],
                    )
            return resp.content
        error_msg = f"GPT_SoVITS_V2 TTS请求失败: {resp.status_code} - {resp.text}"
        base_logger.bind(tag=gsv_mod.TAG).error(error_msg)
        raise Exception(error_msg)

    def _patched_gsv_text_to_speak_sync(self, text) -> bytes:
        global _gsv_live_inflight
        prep = _prepare_gsv_segment(self, text)
        if prep is None:
            return b""
        clean_text, conn, cur_sid, is_first_segment, req_url, request_json, chosen_prof = prep
        timeout_sec = float(getattr(self, "tts_timeout", None) or 30.0)
        with _gsv_inflight_lock:
            _gsv_live_inflight += 1
        try:
            resp = _post_gsv_sync(req_url or self.url, request_json, timeout_sec, provider=self)
        finally:
            with _gsv_inflight_lock:
                _gsv_live_inflight = max(0, _gsv_live_inflight - 1)
        return _finalize_gsv_response(self, resp, clean_text, conn, cur_sid, is_first_segment, chosen_prof)

    @functools.wraps(_orig_gsv_text_to_speak)
    async def _patched_gsv_text_to_speak(self, text, output_file, *args, **kwargs):
        global _gsv_live_inflight
        prep = _prepare_gsv_segment(self, text)
        if prep is None:
            return b""
        clean_text, conn, cur_sid, is_first_segment, req_url, request_json, chosen_prof = prep
        timeout_sec = float(getattr(self, "tts_timeout", None) or 30.0)
        with _gsv_inflight_lock:
            _gsv_live_inflight += 1
        try:
            resp = await asyncio.to_thread(
                functools.partial(_post_gsv_sync, req_url or self.url, request_json, timeout_sec, provider=self)
            )
        finally:
            with _gsv_inflight_lock:
                _gsv_live_inflight = max(0, _gsv_live_inflight - 1)
        content = _finalize_gsv_response(self, resp, clean_text, conn, cur_sid, is_first_segment, chosen_prof)
        if output_file:
            await asyncio.to_thread(_write_file_bytes, output_file, content)
        else:
            return content

    gsv_mod.TTSProvider._handles_own_segment_emotion = True
    gsv_mod.TTSProvider._text_to_speak_sync = _patched_gsv_text_to_speak_sync
    gsv_mod.TTSProvider.text_to_speak = _patched_gsv_text_to_speak
    gsv_mod.TTSProvider._patched_tts_speak_ref = _patched_gsv_text_to_speak
    gsv_mod.TTSProvider.voice = property(
        lambda self: tags.resolve_emotion_character_key(getattr(self, "ref_audio_path", ""), tts=self) or "default"
    )

    # 3. Subtitle firewall + playback-synchronized ESP32 emotion update + P1-11 ring buffer event
    _orig_send_tts_message = send_audio_mod.send_tts_message

    @functools.wraps(_orig_send_tts_message)
    async def _patched_send_tts_message(conn, state, text=None, *args, **kwargs):
        if text is not None:
            text = tags.strip_emotion_tags(text).strip()
            if not text and state == "sentence_start":
                return
        if state == "sentence_start" and text and conn is not None:
            tts = getattr(conn, "tts", None)
            q = getattr(tts, "_segment_emotion_queue", None)
            if q:
                cur_sid = getattr(conn, "sentence_id", None)
                # Prune stale turns
                q[:] = [item for item in q if item[0] == cur_sid]
                matched_idx = next(
                    (i for i, item in enumerate(q) if item[1] == text),
                    0 if q else None,
                )
                if matched_idx is not None:
                    _, _, emj, esp_emo = q.pop(matched_idx)
                    await splitter._send_esp32_emotion_ws(conn, emj, esp_emo)
        if state == "stop":
            try:
                return await _orig_send_tts_message(conn, state, text, *args, **kwargs)
            finally:
                try:
                    runtime_events.publish_event(
                        "tts_state",
                        state="stop",
                        text=None,
                        device_id=str(getattr(conn, "device_id", "") or ""),
                        sentence_id=str(getattr(conn, "sentence_id", "") or ""),
                    )
                except Exception:
                    pass
        try:
            runtime_events.publish_event(
                "tts_state",
                state=str(state or ""),
                text=text if isinstance(text, str) else None,
                device_id=str(getattr(conn, "device_id", "") or ""),
                sentence_id=str(getattr(conn, "sentence_id", "") or ""),
            )
        except Exception:
            pass
        return await _orig_send_tts_message(conn, state, text, *args, **kwargs)

    send_audio_mod.send_tts_message = _patched_send_tts_message
    rebound_tts = patch_registry.rebind(_orig_send_tts_message, _patched_send_tts_message)

    # 4. Enhance textUtils.get_emotion so first-chunk tag/cue maps accurately without misfiring on "["
    _orig_get_emotion = text_utils_mod.get_emotion

    @functools.wraps(_orig_get_emotion)
    async def _patched_get_emotion(conn, text, *args, **kwargs):
        if text and tags.has_unclosed_leading_tag(text):
            # Wait for TTSProvider to push exact emotion once closing bracket arrives
            return
        seg_emo, stripped = tags.extract_and_strip_emotion(text or "", flush_incomplete=False)
        if seg_emo:
            prof = tags.get_mei_emotion_profile(seg_emo, require_ref_audio=False)
            splitter._push_esp32_emotion_async(conn, prof["esp32_emoji"], prof["esp32_emotion"])
            return
        if conn and tags.has_voice_emotion_tag_rule(str((getattr(conn, "config", None) or {}).get("prompt") or "")):
            # Avoid upstream default `🙂 happy` misfiring before Mei's first clause resolves
            return
        return await _orig_get_emotion(conn, stripped, *args, **kwargs)

    text_utils_mod.get_emotion = _patched_get_emotion
    rebound_emo = patch_registry.rebind(_orig_get_emotion, _patched_get_emotion)

    TTSProviderBase._mei_prosody_patched = True
    patch_registry.record_patch(
        "mei_emotion_and_prosody",
        "ok",
        target="TTSProviderBase/GSV/send_tts_message/PromptManager",
        detail="ok",
        contract_ok=True,
        category="experience",
        rebound=rebound_tts + rebound_emo,
    )
    print("[MeiEmotionRouter] 已挂载芽衣7情绪动态参考音频路由 + 防碎句韵律切分补丁 (cut0)", flush=True)

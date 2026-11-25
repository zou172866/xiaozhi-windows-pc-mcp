"""Raw 16kHz 16-bit mono PCM input passthrough, hardware-mic exclusivity arbiter & PC wake-standby state machine (Patch #23).

Design contract:
- Zero import-time side effects: call `install_pc_pcm_input_patch()` explicitly from `boot.install_all()`.
- When a client explicitly negotiates `audio_params.format == "pcm"` in its `hello` handshake
  (stored by upstream `helloHandle.py` onto `conn.audio_format`), `ConnectionHandler._decode_opus_packet`
  bypasses `opuslib_next.Decoder` and validates + forwards the raw 16-bit PCM frame straight into
  `SileroVAD`, speculative VAD, `SherpaASR`/cloud ASR, SenseVoice vocal cues, and voiceprint guard.
- Hardware-mic exclusivity (enabled by default; disable with `XIAOZHI_PC_MIC_EXCLUSIVE=0`):
  While any PC microphone connection (`audio_format == "pcm"` + active mic listening) is open,
  existing hardware (`opus`) connections are cleanly closed back to IDLE standby, incoming hardware
  wakeups are silently dismissed without greeting TTS or error alerts, and stray Opus frames are dropped
  before touching VAD/ASR.
- PC wake-standby mode (`_pc_wake_mode`, `_pc_wake_state: "off" | "standby" | "awake"`):
  Allows the 18080 browser microphone to replace the ESP32 onboard mic continuously with a toggleable switch:
  standby listens locally, discards non-wakeup speech silently, keeps the WS alive without idle disconnect,
  wakes automatically on configured wakeup words (+ optional attached command), and returns smoothly to
  standby on exit commands / `handle_exit_intent` / idle timeout while flushing memory & trimming context.
- All standard ESP32 devices (`audio_format == "opus"`) keep the exact upstream Opus decode path
  whenever no PC microphone is active.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import re
import time
import uuid
from typing import Any, Callable, Coroutine, List, Optional, Tuple

import asr_corrections
import patch_registry
import runtime_events
import runtime_settings
import zh_pinyin

MAX_PCM_FRAME_BYTES = 32768
_PC_MIC_EXCLUSIVE_ENV = "XIAOZHI_PC_MIC_EXCLUSIVE"
PC_WAKE_STANDBY_HARD_TIMEOUT_SEC = 86400

_SENSEVOICE_TAG_RE = re.compile(r"<\|[^|]+\|>")
_WAKEUP_PUNCT_RE = re.compile(r"[\s,，.。!！?？;；:：、~～·…—\-_'\"“”‘’()（）\[\]【】<>《》]+")
_WAKEUP_LEADING_FILLER_RE = re.compile(
    r"^(?:那个|哎呀|诶呀|哎哟|喂喂|嗯嗯|啊啊|哦哦|诶诶|嘿嘿|哈哈|好啦|好了|[嗯啊哦呃额诶哎唉喂嘿嗨哈呀哇喔噢呵])"
)
_PURE_TRAILING_PARTICLES = frozenset(
    {"呀", "啊", "哦", "噢", "哈", "啦", "呢", "嘛", "诶", "哎", "喂", "嗯", "呀呀", "啊啊"}
)
_LEADING_SEP_CHARS = " \t\r\n,，.。!！?？;；:：、~～·…—-_\"'“”‘’()（）"
_EXIT_LEADING_FILLER_RE = re.compile(r"^(?:好了|好啦|行了|行啦|好的|那就|那我|先|请|嗯|好|行|那)")
_EXIT_TRAILING_PARTICLE_RE = re.compile(r"(?:吧|啦|了|呀|啊|哦|噢|嘛|哈|呢|呗|一下)+$")
_ASR_BREATH_FILLERS = frozenset({"uh", "um", "ah", "eh", "hm", "hmm", "mm", "mmm", "er"})
DEFAULT_PC_WAKE_EXIT_COMMANDS: Tuple[str, ...] = (
    "退出对话",
    "退下",
    "退下吧",
    "休眠",
    "休眠吧",
    "进入待命",
    "回待命",
    "回到待命",
    "退出吧",
    "结束对话",
    "停止对话",
    "拜拜",
    "再见",
)
_LOCAL_ASR_TYPE_TOKENS = frozenset({"sherpa_onnx_local", "fun_local", "vosk_local", "vosk"})
_LOCAL_ASR_SELECTED_NAMES = frozenset({"SherpaASR", "FunASR"})


def is_pc_mic_exclusive_enabled() -> bool:
    """Return True unless `XIAOZHI_PC_MIC_EXCLUSIVE` is explicitly disabled (`0`/`false`/`no`/`off`)."""
    raw = str(os.environ.get(_PC_MIC_EXCLUSIVE_ENV, "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def is_pcm_audio_connection(conn: Any) -> bool:
    """Return True iff the WebSocket connection explicitly negotiated raw PCM audio."""
    fmt = str(getattr(conn, "audio_format", "opus") or "opus").strip().lower()
    return fmt == "pcm"


def _is_conn_alive(conn: Any) -> bool:
    if conn is None:
        return False
    stop_evt = getattr(conn, "stop_event", None)
    if stop_evt is not None and callable(getattr(stop_evt, "is_set", None)):
        try:
            if stop_evt.is_set():
                return False
        except Exception:
            pass
    ws = getattr(conn, "websocket", None)
    if ws is not None:
        try:
            if getattr(ws, "closed", False):
                return False
            st = getattr(ws, "state", None)
            if st is not None and getattr(st, "name", "") == "CLOSED":
                return False
        except Exception:
            pass
    return True


def is_pc_mic_connection_active(conn: Any) -> bool:
    """Return True if `conn` is a live PCM connection with an active PC microphone."""
    return bool(
        conn is not None
        and is_pcm_audio_connection(conn)
        and getattr(conn, "_pc_mic_active", False)
        and _is_conn_alive(conn)
    )


def mark_pc_mic_active(conn: Any, active: bool = True) -> None:
    """Mark or clear the active PC microphone flag on a PCM connection."""
    if conn is None:
        return
    try:
        conn._pc_mic_active = bool(active)
    except Exception:
        pass


def has_active_pc_mic_connection(exclude: Any = None) -> bool:
    """Return True if hardware-mic exclusivity is enabled and any other PC mic session is live."""
    if not is_pc_mic_exclusive_enabled():
        return False
    for conn in runtime_events.get_active_connections():
        if conn is exclude:
            continue
        if is_pc_mic_connection_active(conn):
            return True
    return False


def should_suppress_hardware_conn(conn: Any) -> bool:
    """Return True if `conn` is a non-PCM hardware client that should yield to an active PC mic."""
    if conn is None or is_pcm_audio_connection(conn):
        return False
    if getattr(conn, "_yielded_to_pc_mic", False):
        return True
    return has_active_pc_mic_connection(exclude=conn)


def _schedule_coro_on_conn_loop(conn: Any, coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> bool:
    loop = getattr(conn, "loop", None)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is not None and not loop.is_closed():
        if running is loop:
            loop.create_task(coro_factory())
            return True
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(coro_factory(), loop)
            return True
    if running is not None and not running.is_closed():
        running.create_task(coro_factory())
        return True
    return False


def _clear_hardware_conn_local_state(hw_conn: Any) -> bool:
    """Synchronously mark `hw_conn` yielded, abort/clear queues, and return whether it owned active PC audio."""
    try:
        hw_conn._yielded_to_pc_mic = True
        hw_conn.client_abort = True
        hw_conn.client_is_speaking = False
    except Exception:
        pass

    owns_pc_audio = bool(
        getattr(hw_conn, "_pc_audio_active_sid", None) is not None
        or getattr(hw_conn, "_pc_audio_enqueued_sid", None) is not None
    )
    if owns_pc_audio:
        try:
            hw_conn._pc_mic_preempt_stop_pc_audio = True
        except Exception:
            pass
    try:
        import pc_audio_router

        pc_audio_router._ensure_conn_pc_state(hw_conn)
        pc_audio_router._clear_pc_clips(hw_conn)
        pc_audio_router._clear_pc_stream_chunks(hw_conn)
        with hw_conn._pc_audio_queue_lock:
            hw_conn._pc_audio_active_sid = None
            hw_conn._pc_audio_queued_ms = 0
            hw_conn._pc_audio_enqueued_ms = 0
            hw_conn._pc_audio_enqueued_sid = None
    except Exception:
        pass

    for attr in ("reset_audio_states", "clear_queues"):
        fn = getattr(hw_conn, attr, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
    audio_buf = getattr(hw_conn, "audio_buffer", None)
    if audio_buf is not None and callable(getattr(audio_buf, "clear", None)):
        try:
            audio_buf.clear()
        except Exception:
            pass
    return owns_pc_audio


async def _fast_close_websocket(ws: Any, *, timeout_sec: float = 0.25) -> bool:
    """Close a WebSocket promptly without blocking on websockets' default 10s close handshake timeout."""
    if ws is None:
        return False
    closed_any = False
    try:
        cur_timeout = getattr(ws, "close_timeout", None)
        if isinstance(cur_timeout, (int, float)) and cur_timeout > timeout_sec:
            ws.close_timeout = float(timeout_sec)
    except Exception:
        pass
    close_fn = getattr(ws, "close", None)
    if callable(close_fn):
        closed_any = True
        try:
            res = close_fn()
            if inspect.isawaitable(res):
                await asyncio.wait_for(res, timeout=float(timeout_sec) + 0.15)
        except Exception:
            pass
    transport = getattr(ws, "transport", None)
    if transport is not None:
        try:
            is_closing = getattr(transport, "is_closing", None)
            if not (callable(is_closing) and is_closing()):
                if callable(getattr(transport, "close", None)):
                    transport.close()
                    closed_any = True
        except Exception:
            pass
    return closed_any


async def close_suppressed_hardware_conn(
    hw_conn: Any,
    *,
    reason: str = "pc_mic_exclusive",
    delay_sec: float = 0.0,
) -> None:
    """Silently yield a hardware WebSocket back to IDLE standby without disrupting PC mic playback."""
    if hw_conn is None or is_pcm_audio_connection(hw_conn):
        return
    owns_pc_audio = _clear_hardware_conn_local_state(hw_conn)
    need_stop_pc = bool(owns_pc_audio or getattr(hw_conn, "_pc_mic_preempt_stop_pc_audio", False))
    try:
        hw_conn._pc_mic_preempt_stop_pc_audio = False
    except Exception:
        pass
    if delay_sec > 0.0:
        await asyncio.sleep(float(delay_sec))
    if not _is_conn_alive(hw_conn):
        return

    # Only stop host PC speaker when preempting a hardware conn that was itself playing there,
    # and never if any active PC mic connection is currently speaking.
    if reason == "preempt_for_pc_mic" and need_stop_pc:
        pc_speaking = any(
            is_pc_mic_connection_active(c) and runtime_events._conn_is_speaking(c)
            for c in runtime_events.get_active_connections()
        )
        if not pc_speaking:
            try:
                import pc_audio_router

                await pc_audio_router._post_stop_to_pc()
            except Exception:
                pass

    if not getattr(hw_conn, "_pc_mic_yield_logged", False):
        try:
            hw_conn._pc_mic_yield_logged = True
        except Exception:
            pass
        dev = str(getattr(hw_conn, "device_id", "") or "hardware")
        print(f"[PCMicArbiter] 电脑麦已开启，已静默让硬件设备 ({dev}) 退回待机 ({reason})", flush=True)

    ws = getattr(hw_conn, "websocket", None)
    if await _fast_close_websocket(ws, timeout_sec=0.25):
        return
    close_fn = getattr(hw_conn, "close", None)
    if callable(close_fn):
        try:
            res = close_fn()
            if inspect.isawaitable(res):
                await asyncio.wait_for(res, timeout=0.4)
        except Exception:
            pass


def schedule_close_suppressed_hardware_conn(
    hw_conn: Any,
    *,
    reason: str = "pc_mic_exclusive",
    delay_sec: float = 0.0,
) -> bool:
    """Deduplicated non-blocking scheduler to close a suppressed hardware WebSocket cleanly."""
    if hw_conn is None or is_pcm_audio_connection(hw_conn):
        return False
    _clear_hardware_conn_local_state(hw_conn)
    immediate = float(delay_sec) <= 0.0
    if immediate:
        if getattr(hw_conn, "_pc_mic_close_immediate_scheduled", False):
            return False
        try:
            hw_conn._pc_mic_close_immediate_scheduled = True
        except Exception:
            pass
    else:
        if getattr(hw_conn, "_pc_mic_close_immediate_scheduled", False) or getattr(
            hw_conn, "_pc_mic_close_fallback_scheduled", False
        ):
            return False
        try:
            hw_conn._pc_mic_close_fallback_scheduled = True
        except Exception:
            pass

    scheduled = _schedule_coro_on_conn_loop(
        hw_conn,
        lambda: close_suppressed_hardware_conn(hw_conn, reason=reason, delay_sec=delay_sec),
    )
    if not scheduled:
        try:
            if immediate:
                hw_conn._pc_mic_close_immediate_scheduled = False
            else:
                hw_conn._pc_mic_close_fallback_scheduled = False
        except Exception:
            pass
    return scheduled


def preempt_hardware_connections_for_pc_mic(pc_conn: Any) -> int:
    """Close any already-connected hardware ESP32 sessions so only the active PC mic listens."""
    if not is_pc_mic_exclusive_enabled():
        return 0
    preempted = 0
    for other in runtime_events.get_active_connections():
        if other is None or other is pc_conn or is_pcm_audio_connection(other):
            continue
        if not _is_conn_alive(other):
            continue
        _clear_hardware_conn_local_state(other)
        schedule_close_suppressed_hardware_conn(other, reason="preempt_for_pc_mic", delay_sec=0.0)
        preempted += 1
    return preempted


async def _close_preempted_pc_mic_conn(old_pc_conn: Any, *, reason: str = "replaced_by_new_pc_mic") -> None:
    """Notify and cleanly close a superseded PC mic WebSocket so two browser tabs never double-listen."""
    if old_pc_conn is None or not is_pcm_audio_connection(old_pc_conn):
        return
    try:
        old_pc_conn._pc_mic_active = False
        old_pc_conn._pc_wake_mode = False
        old_pc_conn._pc_wake_state = "off"
        old_pc_conn.client_abort = True
        old_pc_conn.client_is_speaking = False
        old_pc_conn.close_after_chat = False
    except Exception:
        pass
    _reset_pc_audio_and_vad_buffers(old_pc_conn)
    if not _is_conn_alive(old_pc_conn):
        return
    try:
        await _send_pc_mic_state_ws(old_pc_conn, state="off", reason=reason)
    except Exception:
        pass
    ws = getattr(old_pc_conn, "websocket", None)
    if await _fast_close_websocket(ws, timeout_sec=0.25):
        return
    close_fn = getattr(old_pc_conn, "close", None)
    if callable(close_fn):
        try:
            res = close_fn()
            if inspect.isawaitable(res):
                await asyncio.wait_for(res, timeout=0.4)
        except Exception:
            pass


def preempt_other_pc_mic_connections(pc_conn: Any) -> int:
    """Supersede any older active PC mic sessions (e.g. another browser tab) without affecting text-only PCM clients."""
    preempted = 0
    for other in runtime_events.get_active_connections():
        if other is None or other is pc_conn or not is_pc_mic_connection_active(other):
            continue
        if getattr(other, "_pc_mic_superseded", False):
            continue
        try:
            other._pc_mic_superseded = True
            other._pc_mic_active = False
            other._pc_wake_mode = False
            other._pc_wake_state = "off"
            other.client_abort = True
        except Exception:
            pass
        _schedule_coro_on_conn_loop(
            other,
            lambda target=other: _close_preempted_pc_mic_conn(target, reason="replaced_by_new_pc_mic"),
        )
        preempted += 1
    return preempted


def is_pc_wake_mode(conn: Any) -> bool:
    """Return True if `conn` is a PCM connection running in PC wake-standby mode."""
    return bool(conn is not None and is_pcm_audio_connection(conn) and getattr(conn, "_pc_wake_mode", False))


def get_pc_wake_state(conn: Any) -> str:
    """Return `"off"`, `"standby"`, or `"awake"` for a connection."""
    if not is_pc_wake_mode(conn):
        return "off"
    raw = str(getattr(conn, "_pc_wake_state", "standby") or "standby").strip().lower()
    return raw if raw in ("standby", "awake") else "standby"


def is_pc_wake_standby(conn: Any) -> bool:
    """Return True iff `conn` is in PC wake mode and currently waiting in standby."""
    return is_pc_wake_mode(conn) and get_pc_wake_state(conn) == "standby"


def is_pc_wake_awake(conn: Any) -> bool:
    """Return True iff `conn` is in PC wake mode and currently awakened for conversation."""
    return is_pc_wake_mode(conn) and get_pc_wake_state(conn) == "awake"


def is_local_asr_provider(conn_or_asr: Any) -> bool:
    """Return True if the active ASR engine is a local offline provider (zero cloud API cost)."""
    if conn_or_asr is None:
        return True
    asr = getattr(conn_or_asr, "asr", None) if hasattr(conn_or_asr, "audio_format") or hasattr(conn_or_asr, "config") else conn_or_asr
    if asr is not None:
        if callable(getattr(asr, "_speculative_decode_pcm", None)):
            return True
        mod_name = str(getattr(type(asr), "__module__", "") or "").lower()
        if any(tok in mod_name for tok in ("sherpa", "fun_local", "funasr", "vosk")):
            return True
        if any(tok in mod_name for tok in ("openai", "qwen", "aliyun", "doubao", "baidu", "tencent", "xunfei")):
            return False
    cfg = getattr(conn_or_asr, "config", None)
    if isinstance(cfg, dict):
        sel = str((cfg.get("selected_module") or {}).get("ASR") or "").strip()
        if sel:
            asr_blk = (cfg.get("ASR") or {}).get(sel)
            if isinstance(asr_blk, dict):
                atype = str(asr_blk.get("type") or "").strip().lower()
                if atype:
                    return atype in _LOCAL_ASR_TYPE_TOKENS
            return sel in _LOCAL_ASR_SELECTED_NAMES
    return True


def _clean_asr_text_for_match(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    cleaned = _SENSEVOICE_TAG_RE.sub("", raw).strip()
    if not cleaned:
        return ""
    try:
        cleaned = asr_corrections.apply_asr_corrections(cleaned)
    except Exception:
        pass
    return cleaned.strip()


def _normalize_match_token(raw_item: Any) -> str:
    cleaned = _WAKEUP_PUNCT_RE.sub("", str(raw_item or "").strip())
    if not cleaned:
        return ""
    return "".join(ch.lower() if ch.isascii() else ch for ch in cleaned)


def get_conn_wakeup_words(conn: Any) -> List[str]:
    """Return hot-synced wakeup words for `conn`, preserving config order."""
    if conn is not None:
        try:
            from patches.emotion import wakeup as _wakeup_mod

            _wakeup_mod.sync_wakeup_words_from_yaml(conn)
        except Exception:
            pass
    cfg = getattr(conn, "config", None) if conn is not None else None
    if isinstance(cfg, dict) and "wakeup_words" in cfg:
        raw_words = cfg.get("wakeup_words") or []
    else:
        raw_words = ("你好小智", "嘿你好呀")
    out: List[str] = []
    seen: set[str] = set()
    for item in raw_words:
        word = _WAKEUP_PUNCT_RE.sub("", str(item or "").strip())
        norm = _normalize_match_token(word)
        if word and norm and norm not in seen:
            seen.add(norm)
            out.append(word)
    return out


def get_conn_exit_commands(conn: Any) -> List[str]:
    """Return merged exact exit commands (`conn.cmd_exit` + built-in standby exit commands)."""
    raw_cmds: List[Any] = []
    if conn is not None:
        cmd_exit = getattr(conn, "cmd_exit", None)
        if isinstance(cmd_exit, (list, tuple, set)):
            raw_cmds.extend(cmd_exit)
        else:
            cfg = getattr(conn, "config", None)
            if isinstance(cfg, dict) and isinstance(cfg.get("exit_commands"), (list, tuple, set)):
                raw_cmds.extend(cfg.get("exit_commands") or ())
            else:
                raw_cmds.extend(("退出", "关闭"))
    else:
        raw_cmds.extend(("退出", "关闭"))
    raw_cmds.extend(DEFAULT_PC_WAKE_EXIT_COMMANDS)

    out: List[str] = []
    seen: set[str] = set()
    for item in raw_cmds:
        word = _WAKEUP_PUNCT_RE.sub("", str(item or "").strip())
        norm = _normalize_match_token(word)
        if word and norm and norm not in seen:
            seen.add(norm)
            out.append(word)
    return out


def match_pc_wakeup_word(conn: Any, text: Any, *, _depth: int = 0) -> Tuple[str, str]:
    """Match configured wakeup words via 3-tier Hanzi + Mandarin Pinyin + controlled acoustic-confusion matching.

    Tiers:
    1. Exact normalized Hanzi + `asr_corrections` (prefix, leading <=2-char filler, or sliding window on short <=12-char utterances).
    2. Exact tone-less Mandarin Pinyin sequence match (covers all homophones like 雅依/亚伊/压抑/讶异/呀一 for any Chinese wakeup word).
    3. Controlled acoustic-confusion Pinyin match for >=4-char Chinese wakeup words (at most 1 confusable syllable with >=3 exact
       syllables, e.g. 你好阿姨 / 你好爱衣 / 你好芽语 / 你好小诗->你好小智), plus leading-syllable clipped exact trailing match
       (e.g. 好芽衣 / 好牙医) on short <=5-char standalone utterances.

    Long casual sentences (>12 chars) remain strictly start-anchored so mid-sentence mentions never false-trigger.
    """
    corrected = _clean_asr_text_for_match(text)
    if not corrected:
        return "", ""

    norm_chars: List[str] = []
    orig_indices: List[int] = []
    for idx, ch in enumerate(corrected):
        if not _WAKEUP_PUNCT_RE.fullmatch(ch):
            norm_chars.append(ch.lower() if ch.isascii() else ch)
            orig_indices.append(idx)
    norm_text = "".join(norm_chars)
    if not norm_text:
        return "", ""

    words = get_conn_wakeup_words(conn)
    if not words:
        return "", ""

    candidates: List[Tuple[str, str, int]] = []
    for idx, w in enumerate(words):
        nw = _normalize_match_token(w)
        if nw:
            candidates.append((w, nw, idx))
    candidates.sort(key=lambda item: (-len(item[1]), item[2]))

    def _build_result(orig_word: str, match_end: int) -> Tuple[str, str]:
        if match_end >= len(orig_indices):
            return orig_word, ""
        rem = corrected[orig_indices[match_end] :].lstrip(_LEADING_SEP_CHARS).strip()
        if not rem:
            return orig_word, ""
        rem_norm = _normalize_match_token(rem)
        if not rem_norm or rem_norm in _PURE_TRAILING_PARTICLES:
            return orig_word, ""
        # Collapse repeated wakeup attempts in one utterance (e.g. "你好压抑，你好芽衣" or "你好压抑，你好芽衣今天天气")
        if _depth < 2:
            sub_wake, sub_rem = match_pc_wakeup_word(conn, rem, _depth=_depth + 1)
            if sub_wake:
                return sub_wake, sub_rem
        return orig_word, rem

    def _ascii_boundaries_ok(start_idx: int, end_idx: int) -> bool:
        if start_idx > 0:
            prev_ch = norm_text[start_idx - 1]
            first_ch = norm_text[start_idx]
            if (
                orig_indices[start_idx] == orig_indices[start_idx - 1] + 1
                and prev_ch.isascii()
                and prev_ch.isalnum()
                and first_ch.isascii()
                and first_ch.isalnum()
            ):
                return False
        if end_idx < len(norm_text):
            last_ch = norm_text[end_idx - 1]
            next_ch = norm_text[end_idx]
            if (
                orig_indices[end_idx] == orig_indices[end_idx - 1] + 1
                and last_ch.isascii()
                and last_ch.isalnum()
                and next_ch.isascii()
                and next_ch.isalnum()
            ):
                return False
        return True

    def _matches_prefix_boundary(start_idx: int, norm_word: str) -> bool:
        if not norm_text[start_idx:].startswith(norm_word):
            return False
        return _ascii_boundaries_ok(start_idx, start_idx + len(norm_word))

    m_filler = _WAKEUP_LEADING_FILLER_RE.match(norm_text)
    filler_offset = m_filler.end() if m_filler is not None else 0
    _NEGATION_OR_META_RE = re.compile(r"[不别没莫勿禁停说叫喊提讲聊]")

    # Optional secondary offset: ONLY after an explicit punctuation/pause break preceded by a brief (<=4-char)
    # non-negated prefix (e.g. "喂那个，你好芽衣" / "嗨，你好芽衣"), never inside "不要叫你好芽衣".
    clause_offsets: List[int] = []
    if len(norm_text) <= 12:
        for idx in range(1, min(len(norm_text), 5)):
            if orig_indices[idx] > orig_indices[idx - 1] + 1:
                prefix_seg = norm_text[:idx]
                if prefix_seg and not _NEGATION_OR_META_RE.search(prefix_seg):
                    clause_offsets.append(idx)

    def _candidate_offsets(word_len: int, *, allow_clause: bool) -> List[int]:
        max_start = len(norm_text) - word_len
        if max_start < 0:
            return []
        offsets: List[int] = [0]
        if 0 < filler_offset <= max_start:
            offsets.append(filler_offset)
        if allow_clause:
            for pos in clause_offsets:
                if pos <= max_start and pos not in offsets:
                    offsets.append(pos)
        return offsets

    # Tier 1: exact normalized Hanzi/ASCII at start, after short filler, or after brief non-negated pause
    for orig_word, norm_word, _ in candidates:
        for pos in _candidate_offsets(len(norm_word), allow_clause=True):
            if _matches_prefix_boundary(pos, norm_word):
                return _build_result(orig_word, pos + len(norm_word))

    # Prepare Mandarin pinyin sequences for CJK tiers (lazy-loaded stdlib table)
    text_pinyins = [zh_pinyin.get_char_pinyins(ch) for ch in norm_text]
    cjk_candidates: List[Tuple[str, str, List[frozenset[str]]]] = []
    for orig_word, norm_word, _ in candidates:
        if len(norm_word) < 2 or not all(zh_pinyin.is_cjk_char(ch) for ch in norm_word):
            continue
        wp = [zh_pinyin.get_char_pinyins(ch) for ch in norm_word]
        if all(wp):
            cjk_candidates.append((orig_word, norm_word, wp))

    if not cjk_candidates:
        return "", ""

    # Tier 2: exact tone-less Mandarin pinyin sequence match (all homophones like 雅依/亚伊/压抑/呀一)
    for orig_word, norm_word, word_pinyins in cjk_candidates:
        wlen = len(word_pinyins)
        for pos in _candidate_offsets(wlen, allow_clause=True):
            if all(
                zh_pinyin.syllable_sets_exact_match(text_pinyins[pos + i], word_pinyins[i])
                for i in range(wlen)
            ):
                return _build_result(orig_word, pos + wlen)

    # Tier 3a: controlled acoustic-confusion match ONLY for >=4-syllable Chinese wakeup words
    # (requires >= L-1 exact pinyin matches + at most 1 acoustically confusable syllable, e.g. 你好阿姨 / 你好小诗)
    for orig_word, norm_word, word_pinyins in cjk_candidates:
        wlen = len(word_pinyins)
        if wlen < 4:
            continue
        for pos in _candidate_offsets(wlen, allow_clause=True):
            exact_cnt = 0
            all_fuzzy_ok = True
            for i in range(wlen):
                tp = text_pinyins[pos + i]
                wp = word_pinyins[i]
                if zh_pinyin.syllable_sets_exact_match(tp, wp):
                    exact_cnt += 1
                elif not zh_pinyin.syllable_sets_fuzzy_match(tp, wp):
                    all_fuzzy_ok = False
                    break
            if all_fuzzy_ok and exact_cnt >= (wlen - 1):
                return _build_result(orig_word, pos + wlen)

    # Tier 3b: leading-syllable clipped exact pinyin match on short standalone utterances (<=5 chars)
    # e.g. browser AGC/SileroVAD clips opening "你" in "你好芽衣" -> "好芽衣" / "好牙医" / "好雅依"
    if len(norm_text) <= 5:
        for orig_word, norm_word, word_pinyins in cjk_candidates:
            wlen = len(word_pinyins)
            if wlen < 4:
                continue
            tail_pinyins = word_pinyins[1:]
            tlen = len(tail_pinyins)
            if len(norm_text) < tlen:
                continue
            if all(
                zh_pinyin.syllable_sets_exact_match(text_pinyins[i], tail_pinyins[i])
                for i in range(tlen)
            ):
                matched_w, rem = _build_result(orig_word, tlen)
                if not rem:
                    return matched_w, ""

    return "", ""


def is_pc_mic_asr_noise_hallucination(text: Any) -> bool:
    """Return True for 1-letter Latin or pure breath-filler ASR hallucinations (e.g. 'H.', 'A.', 'Uh.') with zero CJK."""
    cleaned = _clean_asr_text_for_match(text)
    if not cleaned:
        return True
    norm = _normalize_match_token(cleaned)
    if not norm:
        return True
    if any(zh_pinyin.is_cjk_char(ch) or ch.isdigit() for ch in norm):
        return False
    if norm.isascii() and norm.isalpha():
        if len(norm) == 1:
            return True
        if norm in _ASR_BREATH_FILLERS:
            return True
    return False


def match_pc_exit_command(conn: Any, text: Any) -> str:
    """Match a whole-utterance exit command (exact, optional brief filler/particle, or exact CJK homophone like '推出')."""
    corrected = _clean_asr_text_for_match(text)
    if not corrected:
        return ""
    norm_text = _normalize_match_token(corrected)
    if not norm_text:
        return ""

    forms: List[str] = [norm_text]
    no_tail = _EXIT_TRAILING_PARTICLE_RE.sub("", norm_text)
    if len(no_tail) >= 2 and no_tail not in forms:
        forms.append(no_tail)
    no_head = _EXIT_LEADING_FILLER_RE.sub("", norm_text)
    if len(no_head) >= 2 and no_head not in forms:
        forms.append(no_head)
    no_both = _EXIT_TRAILING_PARTICLE_RE.sub("", no_head)
    if len(no_both) >= 2 and no_both not in forms:
        forms.append(no_both)

    cmds = get_conn_exit_commands(conn)
    norm_cmds: List[Tuple[str, str]] = []
    for cmd in cmds:
        nc = _normalize_match_token(cmd)
        if nc:
            norm_cmds.append((cmd, nc))

    # Tier 1: exact normalized match (with optional brief leading filler / trailing particle)
    for form in forms:
        for orig_cmd, nc in norm_cmds:
            if form == nc:
                return orig_cmd

    # Tier 2: exact tone-less Mandarin pinyin match on whole standalone CJK utterance (e.g. "推出" -> "退出")
    for form in forms:
        if len(form) < 2 or len(form) > 5 or not all(zh_pinyin.is_cjk_char(ch) for ch in form):
            continue
        fp = [zh_pinyin.get_char_pinyins(ch) for ch in form]
        if not all(fp):
            continue
        for orig_cmd, nc in norm_cmds:
            if len(nc) != len(form) or not all(zh_pinyin.is_cjk_char(ch) for ch in nc):
                continue
            cp = [zh_pinyin.get_char_pinyins(ch) for ch in nc]
            if all(cp) and all(zh_pinyin.syllable_sets_exact_match(fp[i], cp[i]) for i in range(len(nc))):
                return orig_cmd

    return ""


def _restore_normal_timeout_seconds(conn: Any) -> None:
    if conn is None:
        return
    try:
        farewell_cfg = runtime_settings.get_idle_farewell()
        timeout_sec = int(farewell_cfg.get("timeout_seconds", 120))
        desired = max(60, timeout_sec + 60)
    except Exception:
        prev = getattr(conn, "_pc_wake_prev_timeout_seconds", None)
        desired = int(prev) if isinstance(prev, (int, float)) and prev > 0 else 180
    try:
        conn.timeout_seconds = desired
    except Exception:
        pass


async def _send_pc_mic_state_ws(
    conn: Any,
    *,
    state: str,
    reason: str = "",
    wake_word: str = "",
    trigger_text: str = "",
) -> bool:
    ws = getattr(conn, "websocket", None)
    if ws is None or not callable(getattr(ws, "send", None)) or not _is_conn_alive(conn):
        return False
    kws_active = False
    try:
        import pc_kws_engine

        kws_active = pc_kws_engine.is_kws_available_for_conn(conn)
    except Exception:
        kws_active = False
    payload: dict[str, Any] = {
        "type": "pc_mic_state",
        "state": str(state),
        "reason": str(reason or ""),
        "wakeup_words": get_conn_wakeup_words(conn),
        "local_asr": is_local_asr_provider(conn),
        "kws_active": kws_active,
        "session_id": str(getattr(conn, "session_id", "") or ""),
    }
    if wake_word:
        payload["wake_word"] = str(wake_word)
    if trigger_text:
        payload["trigger_text"] = str(trigger_text)
    try:
        res = ws.send(json.dumps(payload, ensure_ascii=False))
        if inspect.isawaitable(res):
            await res
        return True
    except Exception:
        return False


def _flush_and_trim_pc_wake_dialogue(conn: Any) -> None:
    dlg = getattr(conn, "dialogue", None)
    if dlg is None:
        return
    try:
        from patches import memory_hooks

        memory_hooks._cancel_dialogue_timer(dlg)
        memory_hooks._flush_dialogue_latest_pair(dlg, mark_closed=True)
    except Exception:
        pass
    raw_msgs = getattr(dlg, "dialogue", None)
    if isinstance(raw_msgs, list):
        raw_msgs[:] = [
            m
            for m in raw_msgs
            if getattr(m, "role", "") == "system" or bool(getattr(m, "is_temporary", False))
        ]
    if hasattr(dlg, "_turn_ctx_cache"):
        try:
            dlg._turn_ctx_cache = None
        except Exception:
            pass


def _reset_pc_audio_and_vad_buffers(conn: Any) -> None:
    if conn is None:
        return
    reset_fn = getattr(conn, "reset_audio_states", None)
    if callable(reset_fn):
        try:
            reset_fn()
        except Exception:
            pass
    else:
        for attr in ("client_have_voice", "client_voice_stop", "last_is_voice"):
            try:
                setattr(conn, attr, False)
            except Exception:
                pass
        for buf_attr in ("asr_audio", "client_audio_buffer", "client_voice_window"):
            buf = getattr(conn, buf_attr, None)
            if buf is not None and callable(getattr(buf, "clear", None)):
                try:
                    buf.clear()
                except Exception:
                    pass
    audio_buf = getattr(conn, "audio_buffer", None)
    if audio_buf is not None and callable(getattr(audio_buf, "clear", None)):
        try:
            audio_buf.clear()
        except Exception:
            pass


async def enter_pc_wake_standby(
    conn: Any,
    *,
    reason: str = "standby",
    trigger_text: str = "",
    flush_session: bool = False,
    abort_speaking: bool = False,
) -> bool:
    """Transition a PCM connection into silent wake-word standby without closing the WebSocket."""
    if conn is None:
        return False

    try:
        conn._pc_wake_mode = True
        conn._pc_wake_state = "standby"
        conn._pc_wake_last_standby_ts = time.monotonic()
        conn._idle_farewell_cancelled = True
        conn.close_after_chat = False
        conn.just_woken_up = False
        conn._vad_post_wakeup_pending = False
        conn._vad_dictation_active = False
    except Exception:
        pass

    vad_task = getattr(conn, "vad_resume_task", None)
    if vad_task is not None and callable(getattr(vad_task, "done", None)) and not vad_task.done():
        try:
            vad_task.cancel()
        except Exception:
            pass

    mark_pc_mic_active(conn, True)
    preempt_other_pc_mic_connections(conn)
    preempt_hardware_connections_for_pc_mic(conn)

    dlg_for_abort = getattr(conn, "dialogue", None)
    in_chat_turn = int(getattr(dlg_for_abort, "_in_chat_depth", 0) or 0) > 0 if dlg_for_abort is not None else False
    was_active_turn = bool(
        getattr(conn, "client_is_speaking", False)
        or getattr(conn, "_pc_audio_active_sid", None) is not None
        or getattr(conn, "_pc_audio_enqueued_sid", None) is not None
        or in_chat_turn
    )
    did_abort = False
    if abort_speaking and was_active_turn:
        did_abort = True
        try:
            conn.client_abort = True
            conn.sentence_id = f"standby-{uuid.uuid4().hex[:12]}"
        except Exception:
            pass
        try:
            import core.handle.abortHandle as abort_mod

            await abort_mod.handleAbortMessage(conn)
        except Exception:
            pass

    try:
        conn.client_is_speaking = False
        # Keep client_abort=True after aborting an active LLM/TTS turn so any in-flight
        # executor thread (`conn.chat`) and TTS queue workers stop cleanly; enter_pc_wake_awake
        # (and startToChat / checkWakeupWords) clear client_abort=False on the next turn.
        conn.client_abort = True if did_abort else False
        conn.close_after_chat = False
    except Exception:
        pass

    if flush_session:
        _flush_and_trim_pc_wake_dialogue(conn)

    _reset_pc_audio_and_vad_buffers(conn)
    try:
        import pc_kws_engine

        pc_kws_engine.reset_conn_kws(conn, drop_stream=False)
    except Exception:
        pass

    try:
        cur_timeout = int(getattr(conn, "timeout_seconds", 0) or 0)
        if cur_timeout > 0 and cur_timeout < 3600 and not hasattr(conn, "_pc_wake_prev_timeout_seconds"):
            conn._pc_wake_prev_timeout_seconds = cur_timeout
        conn.last_activity_time = time.time() * 1000.0
        conn.timeout_seconds = max(cur_timeout, PC_WAKE_STANDBY_HARD_TIMEOUT_SEC)
    except Exception:
        pass

    try:
        runtime_events.publish_event(
            "tts_state",
            state="stop",
            text=None,
            device_id=str(getattr(conn, "device_id", "") or ""),
            sentence_id=str(getattr(conn, "sentence_id", "") or ""),
            reason=f"pc_wake_standby:{reason}",
        )
    except Exception:
        pass

    await _send_pc_mic_state_ws(
        conn,
        state="standby",
        reason=reason,
        trigger_text=trigger_text,
    )
    print(f"[PCWakeStandby] 进入电脑麦唤醒待命 ({reason})", flush=True)
    return True


async def enter_pc_wake_awake(
    conn: Any,
    *,
    wake_word: str = "",
    text: str = "",
    reason: str = "wake_word",
) -> bool:
    """Transition a PC wake-standby connection into active awakened conversation."""
    if conn is None:
        return False

    try:
        conn._pc_wake_mode = True
        conn._pc_wake_state = "awake"
        conn.close_after_chat = False
        conn.client_abort = False
        conn._idle_farewell_cancelled = False
        conn.last_activity_time = time.time() * 1000.0
    except Exception:
        pass

    mark_pc_mic_active(conn, True)
    preempt_other_pc_mic_connections(conn)
    preempt_hardware_connections_for_pc_mic(conn)
    _restore_normal_timeout_seconds(conn)

    try:
        runtime_events.publish_event(
            "listen_start",
            device_id=str(getattr(conn, "device_id", "") or ""),
        )
    except Exception:
        pass

    await _send_pc_mic_state_ws(
        conn,
        state="awake",
        reason=reason,
        wake_word=wake_word,
        trigger_text=text,
    )
    if wake_word:
        print(f"[PCWakeStandby] 命中唤醒词「{wake_word}」，进入连续对话", flush=True)
    return True


async def exit_pc_wake_mode(
    conn: Any,
    *,
    reason: str = "client_off",
    send_state: bool = True,
) -> bool:
    """Disable PC wake-standby mode on `conn` and restore normal idle timeout."""
    if conn is None:
        return False
    try:
        conn._pc_wake_mode = False
        conn._pc_wake_state = "off"
    except Exception:
        pass
    try:
        import pc_kws_engine

        pc_kws_engine.reset_conn_kws(conn, drop_stream=True)
    except Exception:
        pass
    _restore_normal_timeout_seconds(conn)
    if send_state:
        await _send_pc_mic_state_ws(conn, state="off", reason=reason)
    return True


def validate_pcm_frame(packet: Any) -> Optional[bytes]:
    """Validate an incoming 16-bit mono PCM frame (`2..32768` even bytes) or return None."""
    if not isinstance(packet, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(packet)
    n = len(raw)
    if n < 2 or n > MAX_PCM_FRAME_BYTES or (n % 2) != 0:
        return None
    return raw


def install_pc_pcm_input_patch() -> None:
    """Install fail-soft raw PCM passthrough & hardware-mic exclusivity on `ConnectionHandler._decode_opus_packet`."""
    from core.connection import ConnectionHandler

    def _wrap_decode(_orig):
        @functools.wraps(_orig)
        def _patched_decode_opus_packet(self, opus_packet: bytes, *args, **kwargs):
            if is_pcm_audio_connection(self):
                frame = validate_pcm_frame(opus_packet)
                if frame is not None and not getattr(self, "_pc_mic_active", False):
                    mark_pc_mic_active(self, True)
                    preempt_other_pc_mic_connections(self)
                    preempt_hardware_connections_for_pc_mic(self)
                return frame
            if should_suppress_hardware_conn(self):
                schedule_close_suppressed_hardware_conn(
                    self,
                    reason="audio_while_pc_mic_active",
                    delay_sec=0.0,
                )
                return None
            return _orig(self, opus_packet, *args, **kwargs)

        return _patched_decode_opus_packet

    patch_registry.install_patch(
        "pc_pcm_audio_input",
        ConnectionHandler,
        "_decode_opus_packet",
        _wrap_decode,
        category="experience",
        target="core.connection.ConnectionHandler._decode_opus_packet",
        required_params=("self", "opus_packet"),
        require_async=False,
        flag_obj=ConnectionHandler,
        flag_attr="_pc_pcm_input_patched",
    )

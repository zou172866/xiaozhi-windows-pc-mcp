"""Speculative VAD early-commit router (F-1).

Preserves SileroVAD `min_silence_duration_ms: 1100` as the hard upper bound so mid-sentence
breathing pauses are never prematurely truncated, while running a speculative in-memory
Sherpa-ONNX ASR decode on the dedicated ASR thread at ~550ms silence (configurable, or
~800ms on the first utterance right after wakeup):
- If the utterance looks linguistically complete (meets min_chars, passes hesitation and
  unpaired-clause vetoes, and matches a complete suffix / command / question pattern),
  commits early (~0.4-0.55s tail wait reduction) and reuses the already-decoded text to
  avoid double-decoding Sherpa.
- If the utterance ends with hesitation/continuation markers, unpaired clause openers,
  bare transitive verbs, trailing comma/ellipsis, or lacks a completion signal, keeps
  waiting smoothly up to 1100ms.
- If speech resumes at any point (even mid-decode), increments the connection's turn
  generation and cleanly discards the speculative result.
- Auto-disables speculative early-commit in story/dictation/manual modes or on long
  monologue utterances (>8s).
- Hot-tunable via `runtime_settings.get_vad_speculative()` (zero-restart toggle & tuning).
"""

from __future__ import annotations

import asyncio
import functools
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

import asr_corrections
import patch_registry
import runtime_settings

TAG = "vad_speculative_router"

_SENSEVOICE_TAG_RE = re.compile(r"<\|[^|]+\|>")
_SPOKEN_CHAR_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_STRIP_PUNCT_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
_CLAUSE_SPLIT_RE = re.compile(r"[，,、；;]+")
_TRAILING_CONT_PUNCT = frozenset("，,、：:；;…-—~")
_QUESTION_PUNCT = frozenset("？?")

_MIN_AUDIO_BYTES = 1920 * 15  # 15 frames x 60ms = 900ms (matches upstream ASRProviderBase.receive_audio)
_LONG_MONOLOGUE_BYTES = 16000 * 2 * 8  # >= 8s of 16kHz 16-bit mono PCM -> auto-disable speculative commit

_BA_COMPLETE_CMD_RE = re.compile(
    r"把[\u4e00-\u9fffA-Za-z0-9]{1,16}"
    r"(?:打开|关闭|关掉|关了|开启|启动|暂停|停止|继续|调大|调小|调高|调低|增大|减小|静音|取消静音|清空|保存|删掉|删除|关机|重启|锁屏)$"
)

_BARE_TRANSITIVE_ENDINGS: Tuple[str, ...] = (
    "打开", "关闭", "关掉", "开启", "启动", "播放", "放一首", "放首",
    "设置", "定个", "定一个", "查一下", "看一下", "看看", "搜一下", "搜索",
    "找一下", "发给", "告诉", "问一下", "帮我开", "帮我关", "帮我查", "帮我找",
    "帮我发", "帮我定", "帮我放", "帮我设",
)

# Bare transitive verb + 了 without resultative/state completion cue often precedes an object
# e.g. "我昨天买了 [breath] 一个新键盘" -> hold to 1100ms!
_BARE_TRANSITIVE_LE_ENDINGS: Tuple[str, ...] = (
    "买了", "去了", "吃了", "看了", "说了", "问了", "找了", "拿了", "带了",
    "发了", "写了", "做了", "给了", "换了", "点了", "加了", "装了", "下了",
    "开了", "关了", "遇到", "碰到", "看到了", "听到了", "收到了", "找到了", "买到了",
)

_LE_COMPLETE_CUES: Tuple[str, ...] = (
    "几点了", "几点了", "怎么了", "辛苦了", "不用了", "算了", "太好了",
    "知道了", "明白了", "懂了", "记住了", "收到了", "搞定了", "完成了", "结束了",
    "吃饱了", "喝饱了", "下班了", "放学了", "到家了", "回家了", "起床了", "睡着了",
    "下雨了", "下雪了", "天黑了", "天亮了", "没电了", "卡住了", "断网了",
    "完了", "好了", "掉了", "够了", "累了", "困了", "饿了", "饱了", "行了", "可以了",
)

_COMMAND_VERB_OBJ_RE = re.compile(
    r"(?:打开|关闭|关掉|启动|查询|查一下|查一查|查查|看看|看一下|调大|调小|调高|调低|增大|减小|"
    r"暂停|继续|播放|放一首|放首|定一个|定个|设置|截个|截取|搜一下|搜索|讲一个|讲个|说一个|说个|来一首)"
    r"([\u4e00-\u9fffA-Za-z0-9]{2,18})$"
)

_SIMPLE_STANDALONE_COMMANDS: Tuple[str, ...] = (
    "截图", "截屏", "锁屏", "静音", "取消静音", "暂停音乐", "继续播放",
    "停止播放", "下一首", "上一首", "大声一点", "小声一点", "声音大一点", "声音小一点",
    "看看屏幕", "看下屏幕", "帮我看看屏幕", "帮我看下屏幕",
)

_INTERROGATIVE_RE = re.compile(r"(?:什么|怎么|哪|几|多少|谁|为何|为什么|是否|吗|呢|嘛|么)")
_TRAILING_VOCATIVES: Tuple[str, ...] = ("雷电芽衣", "芽衣", "小智")

_CORRELATIVE_PAIRS: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("causal", ("因为", "由于", "既然"), ("所以", "因此", "才", "那就")),
    ("concessive", ("虽然", "尽管", "虽说"), ("但是", "可是", "不过", "但", "却", "还是")),
    ("conditional", ("如果", "要是", "假如", "倘若", "只要", "只有"), ("就", "那就", "才", "便")),
    ("additive", ("不但", "不仅", "不光", "不只"), ("而且", "还", "也", "甚至", "并且")),
    ("parallel", ("一方面",), ("另一方面",)),
    ("enumeration", ("第一件事", "第一个"), ("第二", "最后")),
)

_DICTATION_ENTER_CUES: Tuple[str, ...] = (
    "开始听写", "进入听写", "听写模式", "开始口述", "口述模式",
    "帮我记一段", "帮我记录一下", "我来念你记", "我念你记",
    "下面我读一段", "我给你讲个故事", "听我慢慢说", "记录模式",
)

_DICTATION_EXIT_CUES: Tuple[str, ...] = (
    "结束听写", "退出听写", "听写结束", "记完了", "口述结束", "退出记录", "故事讲完了",
)

_stats_lock = threading.Lock()
_stats: Dict[str, Any] = {
    "speculative_triggers": 0,
    "early_commits": 0,
    "resumed_discards": 0,
    "held_hesitation": 0,
    "held_incomplete": 0,
    "story_mode_skips": 0,
    "cache_reuses": 0,
    "inflight_reuses": 0,
    "total_saved_ms": 0.0,
}


def reset_vad_speculative_stats() -> None:
    with _stats_lock:
        for k in list(_stats.keys()):
            _stats[k] = 0.0 if k == "total_saved_ms" else 0


def get_vad_speculative_stats() -> Dict[str, Any]:
    with _stats_lock:
        snap = dict(_stats)
    commits = int(snap["early_commits"])
    snap["avg_saved_ms"] = round(float(snap["total_saved_ms"]) / commits, 1) if commits > 0 else 0.0
    snap["total_saved_ms"] = round(float(snap["total_saved_ms"]), 1)
    return snap


def _bump_stat(key: str, delta: float = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + delta


def _has_unpaired_clause_opener(raw_text: str, clean_text: str, ends_question: bool) -> Optional[str]:
    """Detect unpaired correlative openers in the active/final clause (e.g. '因为今天太忙了')."""
    clauses = [c for c in _CLAUSE_SPLIT_RE.split(raw_text) if _STRIP_PUNCT_RE.sub("", c)]
    last_clause_clean = _STRIP_PUNCT_RE.sub("", clauses[-1]) if clauses else clean_text

    for name, openers, partners in _CORRELATIVE_PAIRS:
        has_opener_in_last = any(op in last_clause_clean for op in openers)
        if not has_opener_in_last:
            continue
        # Direct questions like "因为什么呀？" or "如果下雨怎么办？" are complete questions
        if name in ("causal", "conditional") and (
            ends_question
            or any(q in last_clause_clean for q in ("为什么", "因为什么", "怎么办", "怎么", "吗", "呢"))
        ):
            if any(q in last_clause_clean for q in ("因为什么", "怎么办", "吗", "呢")):
                continue
        has_partner = any(p in last_clause_clean for p in partners)
        if not has_partner:
            return name
    return None


def _is_bare_transitive_le(clean_text: str) -> bool:
    if not clean_text.endswith("了"):
        return False
    if any(clean_text.endswith(cue) for cue in _LE_COMPLETE_CUES):
        return False
    for ending in _BARE_TRANSITIVE_LE_ENDINGS:
        if clean_text.endswith(ending):
            # If preceded by "把...开/关/买/发/写/做/删/换/点/装了", object is already before verb -> complete!
            if "把" in clean_text and clean_text.index("把") < len(clean_text) - len(ending):
                return False
            return True
    return False


def judge_utterance_completeness(
    text: str,
    *,
    cfg: Optional[Dict[str, Any]] = None,
    is_story_or_dictation: bool = False,
) -> Tuple[bool, str]:
    """Judge whether a partial/speculative ASR transcript is complete enough to commit early.

    Designed for high precision (0% false truncation on mid-sentence breathing pauses):
    only returns True when ALL vetoes pass AND a positive completion signal matches.
    """
    if is_story_or_dictation:
        return False, "story_or_dictation_mode"

    active_cfg = cfg if isinstance(cfg, dict) else runtime_settings.get_vad_speculative()
    raw = str(text or "").strip()
    if not raw:
        return False, "empty"

    if raw.endswith("...") or raw[-1] in _TRAILING_CONT_PUNCT:
        return False, "trailing_continuation_punct"

    ends_question = raw[-1] in _QUESTION_PUNCT
    clean = _STRIP_PUNCT_RE.sub("", raw)
    spoken_len = len(_SPOKEN_CHAR_RE.findall(clean))
    min_chars = max(2, int(active_cfg.get("min_chars", 4)))
    if spoken_len < min_chars:
        return False, f"too_short:{spoken_len}<{min_chars}"

    comp_raw = active_cfg.get("complete_suffixes")
    if comp_raw is None:
        comp_list = list(runtime_settings.DEFAULT_VAD_SPECULATIVE_COMPLETE_SUFFIXES)
    else:
        comp_list = [str(x).strip() for x in comp_raw if str(x).strip()]

    hes_raw = active_cfg.get("hesitation_suffixes")
    if hes_raw is None:
        hes_list = list(runtime_settings.DEFAULT_VAD_SPECULATIVE_HESITATION_SUFFIXES)
    else:
        hes_list = [str(x).strip() for x in hes_raw if str(x).strip()]

    hes_multi = sorted((h for h in hes_list if len(h) >= 2), key=len, reverse=True)
    hes_single = [h for h in hes_list if len(h) == 1]
    comp_multi = sorted((c for c in comp_list if len(c) >= 2), key=len, reverse=True)
    comp_single = [c for c in comp_list if len(c) == 1]

    is_ba_cmd = bool(_BA_COMPLETE_CMD_RE.search(clean))

    # 1. Multi-character hesitation / continuation marker veto (highest priority)
    for h in hes_multi:
        if clean.endswith(h):
            if is_ba_cmd and h in _BARE_TRANSITIVE_ENDINGS:
                continue
            return False, f"hesitation_suffix:{h}"

    # 2. Bare transitive verb without following object (and not a 把+obj+verb command)
    if not is_ba_cmd:
        for verb in _BARE_TRANSITIVE_ENDINGS:
            if clean.endswith(verb):
                return False, f"incomplete_transitive_verb:{verb}"

    # 3. Unpaired correlative clause opener in the active clause (e.g. "因为今天工作太忙了")
    unpaired = _has_unpaired_clause_opener(raw, clean, ends_question)
    if unpaired is not None:
        return False, f"unpaired_clause:{unpaired}"

    # 4. Multi-character explicit complete suffixes (e.g. "怎么样", "几点了", "好啊", "谢谢你")
    for suf in comp_multi:
        if clean.endswith(suf):
            return True, f"complete_suffix:{suf}"

    # 5. Single-character hesitation / dangling function word veto (e.g. "嗯", "的", "和", "把")
    for h in hes_single:
        if clean.endswith(h):
            return False, f"hesitation_suffix:{h}"

    # 6. Complete 把-construction command (e.g. "帮我把浏览器打开", "把音量调大")
    if is_ba_cmd:
        return True, "complete_ba_command"

    # 7. Standalone complete command phrases (e.g. "帮我看看屏幕", "暂停音乐")
    for cmd in _SIMPLE_STANDALONE_COMMANDS:
        if clean.endswith(cmd):
            return True, f"complete_command:{cmd}"

    # 8. Single-character sentence-final particles (吗/呢/吧/了/啦/呀/哦/嘛/呗/哈/么)
    for suf in comp_single:
        if clean.endswith(suf):
            if suf == "了" and _is_bare_transitive_le(clean):
                return False, "incomplete_transitive_le"
            return True, f"complete_suffix:{suf}"

    # 9. Verb + concrete object voice command (e.g. "帮我打开微信", "查一下明天长沙天气")
    m_cmd = _COMMAND_VERB_OBJ_RE.search(clean)
    if m_cmd is not None:
        obj_part = m_cmd.group(1)
        # Ensure object part itself isn't a vague placeholder/hesitation
        if obj_part not in ("那个", "这个", "一下", "什么", "哪里") and not any(
            obj_part.endswith(h) for h in hes_multi
        ):
            return True, "complete_command_verb_obj"

    # 10. Explicit question mark + interrogative word (e.g. "明天上海多少度？")
    if ends_question and _INTERROGATIVE_RE.search(clean):
        return True, "complete_question"

    # 11. Trailing companion vocative after a complete clause (e.g. "谢谢你呀芽衣", "晚安芽衣")
    for voc in _TRAILING_VOCATIVES:
        if clean.endswith(voc) and len(clean) > len(voc) + 1:
            prefix = clean[: -len(voc)]
            if any(prefix.endswith(suf) for suf in comp_multi):
                return True, f"complete_vocative:{voc}"
            for suf in comp_single:
                if prefix.endswith(suf):
                    if suf == "了" and _is_bare_transitive_le(prefix):
                        break
                    return True, f"complete_vocative:{voc}"

    return False, "no_completion_signal"


def is_story_or_dictation_mode(conn: Any, *, audio_bytes_len: int = 0) -> bool:
    """Auto-disable speculative early-commit in story/dictation/manual modes, storyteller preset, or long monologues (F-1, P1-12)."""
    if conn is None:
        return False
    # P1-12: Always synchronize explicit dictation/story session state from dialogue history BEFORE
    # checking storyteller/monologue overrides so enter/exit cues are never missed if overrides later toggle off.
    dlg = getattr(conn, "dialogue", None)
    msgs = getattr(dlg, "dialogue", None)
    active_flag = bool(getattr(conn, "_vad_dictation_active", False))
    seen_objs = getattr(conn, "_vad_dictation_seen_objs", None)
    if not isinstance(seen_objs, dict):
        seen_objs = {}
        try:
            conn._vad_dictation_seen_objs = seen_objs
        except Exception:
            pass
    prev_dlg = getattr(conn, "_vad_dictation_dlg_ref", None)
    if dlg is not prev_dlg:
        active_flag = False
        seen_objs.clear()
        try:
            conn._vad_dictation_dlg_ref = dlg
            conn._vad_dictation_active = False
        except Exception:
            pass
    if isinstance(msgs, list) and msgs:
        for m in msgs:
            if getattr(m, "is_temporary", False):
                continue
            if getattr(m, "role", "") != "user":
                continue
            mid = id(m)
            if seen_objs.get(mid) is m:
                continue
            txt = str(getattr(m, "content", "") or "").strip()
            if not txt:
                continue
            seen_objs[mid] = m
            if any(cue in txt for cue in _DICTATION_EXIT_CUES):
                active_flag = False
            elif any(cue in txt for cue in _DICTATION_ENTER_CUES):
                active_flag = True
        live_ids = {id(m) for m in msgs}
        for stale_id in [k for k in seen_objs if k not in live_ids]:
            seen_objs.pop(stale_id, None)
        try:
            conn._vad_dictation_active = active_flag
        except Exception:
            pass
    else:
        active_flag = False
        seen_objs.clear()
        try:
            conn._vad_dictation_active = False
        except Exception:
            pass

    if getattr(conn, "client_listen_mode", "auto") == "manual":
        return True
    if (
        getattr(conn, "dictation_mode", False)
        or getattr(conn, "story_mode", False)
        or getattr(conn, "vad_speculative_disabled", False)
    ):
        return True
    cfg = getattr(conn, "config", None)
    if isinstance(cfg, dict) and (cfg.get("dictation_mode") or cfg.get("story_mode")):
        return True
    try:
        rl_cfg = runtime_settings.get_reply_length()
        if isinstance(rl_cfg, dict) and str(rl_cfg.get("preset") or "").strip().lower() == "storyteller":
            return True
    except Exception:
        pass
    if audio_bytes_len >= _LONG_MONOLOGUE_BYTES:
        return True
    return active_flag


class _SpeculativeState:
    __slots__ = (
        "generation",
        "task",
        "triggered_gen",
        "result_gen",
        "result_text",
        "result_pcm_len",
        "should_commit",
        "reason",
        "triggered_silence_ms",
        "committed_text",
        "committed_pcm_len",
    )

    def __init__(self) -> None:
        self.generation: int = 0
        self.task: Optional[asyncio.Task] = None
        self.triggered_gen: int = -1
        self.result_gen: int = -1
        self.result_text: Optional[str] = None
        self.result_pcm_len: int = 0
        self.should_commit: bool = False
        self.reason: str = ""
        self.triggered_silence_ms: float = 0.0
        self.committed_text: Optional[str] = None
        self.committed_pcm_len: int = 0

    def invalidate(self, *, count_discard: bool = False) -> None:
        had_pending = (
            (self.task is not None and not self.task.done())
            or (self.result_gen == self.generation and self.result_text is not None)
        )
        self.generation += 1
        t = self.task
        self.task = None
        if t is not None and not t.done():
            try:
                t.cancel()
            except Exception:
                pass
        self.triggered_gen = -1
        self.result_gen = -1
        self.result_text = None
        self.result_pcm_len = 0
        self.should_commit = False
        self.reason = ""
        self.triggered_silence_ms = 0.0
        self.committed_text = None
        self.committed_pcm_len = 0
        if count_discard and had_pending:
            _bump_stat("resumed_discards", 1)


def _get_spec_state(conn: Any) -> _SpeculativeState:
    st = getattr(conn, "_vad_spec_state", None)
    if not isinstance(st, _SpeculativeState):
        st = _SpeculativeState()
        conn._vad_spec_state = st
    return st


def _is_sherpa_asr(asr: Any) -> bool:
    if asr is None:
        return False
    if callable(getattr(asr, "_speculative_decode_pcm", None)):
        return True
    mod_name = str(getattr(type(asr), "__module__", "") or "").lower()
    return ("sherpa" in mod_name) and (getattr(asr, "model", None) is not None)


def _decode_pcm_with_sherpa_sync(asr: Any, pcm_bytes: bytes) -> str:
    """In-memory 16kHz PCM decode on Sherpa-ONNX (zero temp WAV disk I/O)."""
    custom_fn = getattr(asr, "_speculative_decode_pcm", None)
    if callable(custom_fn):
        raw = str(custom_fn(pcm_bytes) or "")
    else:
        import numpy as np

        if len(pcm_bytes) % 2 != 0:
            pcm_bytes = pcm_bytes[:-1]
        if not pcm_bytes:
            return ""
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        stream = asr.model.create_stream()
        stream.accept_waveform(16000, samples)
        asr.model.decode_stream(stream)
        raw = str(getattr(stream.result, "text", "") or "")
    cleaned = _SENSEVOICE_TAG_RE.sub("", raw).strip()
    if cleaned:
        cleaned = asr_corrections.apply_asr_corrections(cleaned)
    return cleaned


def _get_asr_executor_safe():
    try:
        import start_with_env

        fn = getattr(start_with_env, "get_asr_executor", None)
        if callable(fn):
            return fn()
    except Exception:
        pass
    return None


async def _run_speculative_decode_task(
    conn: Any,
    asr: Any,
    state: _SpeculativeState,
    gen: int,
    pcm_snapshot: bytes,
    cfg: Dict[str, Any],
    story_or_dictation: bool,
) -> None:
    try:
        loop = asyncio.get_running_loop()
        executor = _get_asr_executor_safe()
        text = await loop.run_in_executor(executor, _decode_pcm_with_sherpa_sync, asr, pcm_snapshot)
        # Speech may have resumed or state reset while Sherpa was decoding!
        if state.generation != gen:
            return
        should_commit, reason = judge_utterance_completeness(
            text,
            cfg=cfg,
            is_story_or_dictation=story_or_dictation,
        )
        if state.generation != gen:
            return
        state.result_gen = gen
        state.result_text = text
        state.result_pcm_len = len(pcm_snapshot)
        state.should_commit = should_commit
        state.reason = reason
        if not should_commit:
            if reason.startswith(("hesitation_suffix", "unpaired_clause", "trailing_continuation_punct", "incomplete_transitive")):
                _bump_stat("held_hesitation", 1)
            else:
                _bump_stat("held_incomplete", 1)
    except asyncio.CancelledError:
        return
    except Exception:
        return


def _get_upper_bound_silence_ms(conn: Any) -> float:
    vad = getattr(conn, "vad", None)
    raw = getattr(vad, "silence_threshold_ms", None)
    try:
        v = float(raw)
        if v > 0:
            return v
    except Exception:
        pass
    return 1100.0


def _effective_speculative_threshold_ms(conn: Any, cfg: Dict[str, Any]) -> float:
    upper = _get_upper_bound_silence_ms(conn)
    if getattr(conn, "_vad_post_wakeup_pending", False):
        target = float(cfg.get("post_wakeup_ms", 800))
    else:
        target = float(cfg.get("speculative_ms", 550))
    # Never exceed upper bound - 50ms (speculative must precede hard 1100ms cutoff)
    return min(target, max(300.0, upper - 50.0))


def _stash_asr_speculative_hit(asr: Any, session_id: str, pcm_len: int, text: str) -> None:
    cache = getattr(asr, "_vad_spec_text_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        asr._vad_spec_text_cache = cache
    cache[str(session_id or "")] = (int(pcm_len), str(text), time.monotonic())
    if len(cache) > 64:
        oldest = next(iter(cache))
        cache.pop(oldest, None)


def _pop_asr_speculative_hit(asr: Any, session_id: str, pcm_len: int) -> Optional[str]:
    cache = getattr(asr, "_vad_spec_text_cache", None)
    if not isinstance(cache, dict):
        return None
    entry = cache.pop(str(session_id or ""), None)
    if entry is None:
        return None
    cached_len, cached_text, ts = entry
    # Allow up to ~900ms (15 frames = 28800 bytes) of trailing silence appended between speculative trigger and commit/1100ms cutoff
    if (time.monotonic() - ts) > 3.0:
        return None
    if pcm_len < cached_len or (pcm_len - cached_len) > 1920 * 15:
        return None
    return cached_text


def apply_vad_speculative_patch() -> None:
    """Install contract-checked speculative VAD early-commit hooks (experience category, fail-soft)."""
    import core.handle.receiveAudioHandle as recv_mod
    from core.connection import ConnectionHandler
    from core.providers.asr.base import ASRProviderBase
    from core.providers.asr.dto.dto import InterfaceType

    if getattr(ASRProviderBase, "_vad_speculative_patched", False):
        if patch_registry.get_patch_status("vad_speculative") is None:
            patch_registry.record_patch(
                "vad_speculative",
                "ok",
                target="ASRProviderBase/ConnectionHandler/receiveAudioHandle",
                detail="already applied",
                contract_ok=True,
                category="experience",
            )
        return

    _orig_receive_audio = ASRProviderBase.receive_audio
    _orig_handle_voice_stop = ASRProviderBase.handle_voice_stop
    _orig_stt_wrapper = ASRProviderBase.speech_to_text_wrapper
    _orig_reset_audio = ConnectionHandler.reset_audio_states
    _orig_handle_audio = recv_mod.handleAudioMessage

    checks = [
        ("ASRProviderBase.receive_audio", _orig_receive_audio, ("self", "conn", "pcm_frame", "audio_have_voice"), True),
        ("ASRProviderBase.handle_voice_stop", _orig_handle_voice_stop, ("self", "conn", "asr_audio_task"), True),
        ("ASRProviderBase.speech_to_text_wrapper", _orig_stt_wrapper, ("self", "pcm_data", "session_id"), True),
        ("ConnectionHandler.reset_audio_states", _orig_reset_audio, ("self",), False),
        ("receiveAudioHandle.handleAudioMessage", _orig_handle_audio, ("conn", "pcm_frame"), True),
    ]
    failures = []
    for cname, cfn, req_p, req_a in checks:
        ok_s, det = patch_registry.verify_signature_contract(cfn, required_params=req_p, require_async=req_a)
        if not ok_s:
            failures.append(f"{cname}: {det}")

    if failures:
        patch_registry.record_patch(
            "vad_speculative",
            "skipped_contract",
            target="ASRProviderBase/ConnectionHandler/receiveAudioHandle",
            detail="; ".join(failures),
            contract_ok=False,
            category="experience",
        )
        print(f"[SpeculativeVAD] 跳过挂载投机性VAD补丁（签名契约不匹配）: {'; '.join(failures)}", flush=True)
        return

    @functools.wraps(_orig_handle_audio)
    async def _spec_aware_handle_audio(conn, pcm_frame, *args, **kwargs):
        if getattr(conn, "just_woken_up", False):
            conn._vad_post_wakeup_pending = True
        return await _orig_handle_audio(conn, pcm_frame, *args, **kwargs)

    @functools.wraps(_orig_reset_audio)
    def _spec_aware_reset_audio(self, *args, **kwargs):
        st = getattr(self, "_vad_spec_state", None)
        if isinstance(st, _SpeculativeState):
            st.invalidate(count_discard=False)
        return _orig_reset_audio(self, *args, **kwargs)

    @functools.wraps(_orig_receive_audio)
    async def _spec_aware_receive_audio(self, conn, pcm_frame, audio_have_voice, *args, **kwargs):
        if getattr(conn, "client_listen_mode", "auto") != "manual" and _is_sherpa_asr(self):
            state = _get_spec_state(conn)
            if audio_have_voice or getattr(conn, "last_is_voice", False):
                # P1-12: Speech is active or resumed (including 1-2 opening frames where Silero last_is_voice=True
                # before the sliding window flips audio_have_voice=True): discard any in-flight/completed
                # speculative snapshot immediately so a short resumed syllable is never dropped at 1100ms!
                if state.triggered_gen == state.generation or state.result_gen == state.generation:
                    state.invalidate(count_discard=True)
            elif getattr(conn, "client_voice_stop", False):
                # P1-12 + P1-4: Hard 1100ms cutoff reached: hand off completed speculative result,
                # keep valid in-flight decode alive for bounded await in _spec_aware_handle_voice_stop,
                # or cancel stale/resumed tasks.
                if (
                    state.result_gen == state.generation
                    and state.result_text
                    and not getattr(conn, "last_is_voice", False)
                ):
                    state.committed_text = state.result_text
                    state.committed_pcm_len = state.result_pcm_len
                elif state.task is not None and not state.task.done():
                    if state.triggered_gen != state.generation or getattr(conn, "last_is_voice", False):
                        state.invalidate(count_discard=False)
            elif getattr(conn, "client_have_voice", False) and not getattr(conn, "last_is_voice", False):
                # P1-12: Never early-commit or start speculative decode while SileroVAD last_is_voice=True
                # (protects the opening frames when a user resumes speaking inside the sliding window).
                cfg = runtime_settings.get_vad_speculative()
                if cfg.get("enabled", True) and getattr(self, "interface_type", None) != InterfaceType.STREAM:
                    now_ms = time.time() * 1000.0
                    last_voice_ms = float(getattr(conn, "vad_last_voice_time", 0.0) or 0.0)
                    stop_duration_ms = (now_ms - last_voice_ms) if last_voice_ms > 0 else 0.0
                    upper_ms = _get_upper_bound_silence_ms(conn)

                    # 1. Check if speculative result for current silence period is ready to commit early
                    if (
                        state.result_gen == state.generation
                        and state.should_commit
                        and state.result_text
                    ):
                        saved_ms = max(0.0, upper_ms - stop_duration_ms)
                        _bump_stat("early_commits", 1)
                        _bump_stat("total_saved_ms", saved_ms)
                        state.committed_text = state.result_text
                        state.committed_pcm_len = state.result_pcm_len
                        conn.client_voice_stop = True
                        try:
                            lg = getattr(conn, "logger", None)
                            if lg is not None:
                                lg.bind(tag=TAG).info(
                                    f"[SpeculativeVAD] 提前提交 ({stop_duration_ms:.0f}ms/{upper_ms:.0f}ms, "
                                    f"节省 {saved_ms:.0f}ms, reason={state.reason}): {state.result_text}"
                                )
                        except Exception:
                            pass
                    # 2. Otherwise trigger speculative decode once silence crosses speculative_ms (or post_wakeup_ms)
                    elif state.triggered_gen != state.generation:
                        threshold_ms = _effective_speculative_threshold_ms(conn, cfg)
                        if stop_duration_ms >= threshold_ms:
                            pcm_total_len = sum(len(f) for f in (getattr(conn, "asr_audio", None) or ())) + len(pcm_frame)
                            if pcm_total_len > _MIN_AUDIO_BYTES:
                                story_mode = is_story_or_dictation_mode(conn, audio_bytes_len=pcm_total_len)
                                if story_mode:
                                    state.triggered_gen = state.generation
                                    _bump_stat("story_mode_skips", 1)
                                else:
                                    pcm_snapshot = b"".join(list(conn.asr_audio) + [pcm_frame])
                                    state.triggered_gen = state.generation
                                    state.triggered_silence_ms = stop_duration_ms
                                    _bump_stat("speculative_triggers", 1)
                                    gen = state.generation
                                    state.task = asyncio.create_task(
                                        _run_speculative_decode_task(
                                            conn, self, state, gen, pcm_snapshot, cfg, False
                                        )
                                    )

        return await _orig_receive_audio(self, conn, pcm_frame, audio_have_voice, *args, **kwargs)

    @functools.wraps(_orig_handle_voice_stop)
    async def _spec_aware_handle_voice_stop(self, conn, asr_audio_task, *args, **kwargs):
        sid = str(getattr(conn, "session_id", "") or "")
        st = getattr(conn, "_vad_spec_state", None)
        stashed = False
        if isinstance(st, _SpeculativeState):
            if (
                not st.committed_text
                and st.task is not None
                and not st.task.done()
                and st.triggered_gen == st.generation
                and not getattr(conn, "last_is_voice", False)
            ):
                try:
                    await asyncio.wait_for(asyncio.shield(st.task), timeout=0.25)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
                if (
                    st.result_gen == st.generation
                    and st.result_text
                    and not getattr(conn, "last_is_voice", False)
                ):
                    st.committed_text = st.result_text
                    st.committed_pcm_len = st.result_pcm_len
                    _bump_stat("inflight_reuses", 1)
            if st.committed_text:
                _stash_asr_speculative_hit(self, sid, st.committed_pcm_len, st.committed_text)
                stashed = True
                st.committed_text = None
                st.committed_pcm_len = 0
        try:
            return await _orig_handle_voice_stop(self, conn, asr_audio_task, *args, **kwargs)
        finally:
            if getattr(conn, "_vad_post_wakeup_pending", False):
                conn._vad_post_wakeup_pending = False
            if stashed:
                cache = getattr(self, "_vad_spec_text_cache", None)
                if isinstance(cache, dict):
                    cache.pop(sid, None)

    @functools.wraps(_orig_stt_wrapper)
    async def _spec_aware_stt_wrapper(self, pcm_data, session_id, *args, **kwargs):
        if getattr(self, "delete_audio_file", True):
            pcm_len = sum(len(b) for b in (pcm_data or ()))
            hit = _pop_asr_speculative_hit(self, str(session_id or ""), pcm_len)
            if hit is not None:
                _bump_stat("cache_reuses", 1)
                return hit, None
        return await _orig_stt_wrapper(self, pcm_data, session_id, *args, **kwargs)

    ASRProviderBase.receive_audio = _spec_aware_receive_audio
    ASRProviderBase.handle_voice_stop = _spec_aware_handle_voice_stop
    ASRProviderBase.speech_to_text_wrapper = _spec_aware_stt_wrapper
    ASRProviderBase._vad_speculative_patched = True
    ConnectionHandler.reset_audio_states = _spec_aware_reset_audio
    recv_mod.handleAudioMessage = _spec_aware_handle_audio
    rebound = patch_registry.rebind(_orig_handle_audio, _spec_aware_handle_audio)

    patch_registry.record_patch(
        "vad_speculative",
        "ok",
        target="ASRProviderBase/ConnectionHandler/receiveAudioHandle",
        detail="ok (upper_bound=1100ms, speculative_ms hot-tunable)",
        contract_ok=True,
        category="experience",
        rebound=rebound,
    )
    print(
        "[SpeculativeVAD] 已挂载投机性VAD提前提交补丁 (1100ms上限不变 + ~550ms静音预解码 + 18080免重启热调)",
        flush=True,
    )

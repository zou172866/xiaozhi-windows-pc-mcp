"""LLM Prefix-Cache Stabilization & Usage Telemetry Router (F-2).

Splits volatile per-turn context (<context> time/date/weather, <memory> recall + intimacy guidance,
<reply_length_control>, <speakers_info>) out of the system prompt and injects them as a single
ephemeral <turn_context> block right before the latest user utterance:
- System prompt (msgs[0]) + few-shot + historical turns remain 100% byte-identical across turns,
  enabling DeepSeek/OpenAI/Gemini prompt prefix caching (>=70% hit rate by Turn 5).
- Historical messages never retain stale <turn_context> blocks.
- Controlled by `turn_context_mode` ("split" | "legacy") via env `XIAOZHI_TURN_CONTEXT_MODE`,
  `.config.yaml`, or `runtime_settings.json` for instant zero-code rollback.
- Wraps OpenAI-compatible LLMProvider streaming completions to record TTFT,
  `prompt_cache_hit_tokens`, and `prompt_cache_miss_tokens` for logs and 18080 Console display.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import functools
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import patch_registry
import reply_length_router
import runtime_events
import runtime_settings

VALID_MODES = frozenset({"split", "legacy"})

def _make_block_re(tag: str) -> re.Pattern:
    """Match ONLY standalone line-anchored <tag>...</tag> blocks, never inline prose mentions like '请结合 <memory> 中...'."""
    return re.compile(
        rf"(?im)(?:^|\n)[ \t]*<{tag}>(?:[ \t]*\n(?:(?!<{tag}>).)*?|[^\n<>]*)</{tag}>[ \t]*",
        re.DOTALL,
    )


_CONTEXT_BLOCK_RE = _make_block_re("context")
_MEMORY_BLOCK_RE = _make_block_re("memory")
_REPLY_LEN_BLOCK_RE = _make_block_re("reply_length_control")
_SPEAKERS_BLOCK_RE = _make_block_re("speakers_info")
_TURN_CONTEXT_BLOCK_RE = re.compile(
    r"\s*<turn_context>.*?</turn_context>\s*", re.DOTALL | re.IGNORECASE
)

_stats_lock = threading.Lock()
_usage_history: deque[dict[str, Any]] = deque(maxlen=40)
_usage_totals: dict[str, Any] = {
    "turns": 0,
    "prompt_tokens_total": 0,
    "cache_measured_prompt_tokens_total": 0,
    "cache_hit_tokens_total": 0,
    "cache_miss_tokens_total": 0,
    "completion_tokens_total": 0,
    "last_prompt_tokens": 0,
    "last_cache_hit_tokens": 0,
    "last_cache_miss_tokens": 0,
    "last_hit_rate": None,
    "last_ttft_ms": None,
    "last_model": "",
    "last_ts": 0.0,
}

_config_mode_override: Optional[str] = None


def set_config_turn_context_mode(mode: Optional[str]) -> None:
    """Record optional top-level `turn_context_mode` from `.config.yaml`."""
    global _config_mode_override
    m = str(mode or "").strip().lower()
    _config_mode_override = m if m in VALID_MODES else None


def get_turn_context_mode(explicit_mode: Optional[str] = None) -> str:
    """Resolve active turn_context mode: explicit arg > env > .config.yaml > runtime_settings > 'split'."""
    for cand in (
        explicit_mode,
        os.environ.get("XIAOZHI_TURN_CONTEXT_MODE"),
        _config_mode_override,
    ):
        m = str(cand or "").strip().lower()
        if m in VALID_MODES:
            return m
    try:
        m = str(runtime_settings.get_turn_context_mode() or "").strip().lower()
        if m in VALID_MODES:
            return m
    except Exception:
        pass
    return "split"


def strip_turn_context_from_text(text: str) -> str:
    """Remove any embedded <turn_context>...</turn_context> block from a message string."""
    if not text or "<turn_context>" not in text.lower():
        return text
    cleaned = _TURN_CONTEXT_BLOCK_RE.sub("\n\n", text)
    return cleaned.strip()


def _extract_and_strip(pattern: re.Pattern, text: str) -> Tuple[str, List[str]]:
    found: List[str] = []

    def _cap(m: re.Match) -> str:
        blk = m.group(0).strip()
        if blk:
            found.append(blk)
        return ""

    remaining = pattern.sub(_cap, text)
    return remaining, found


def _extract_inner_tag_text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _resolve_current_time_placeholder(text: str, current_time_str: Optional[str] = None) -> str:
    if "{{current_time}}" not in text:
        return text
    hhmm = current_time_str or datetime.now().strftime("%H:%M")
    return text.replace("{{current_time}}", hhmm)


def build_turn_context_block(
    *,
    context_block: str = "",
    memory_text: str = "",
    reply_length_block: str = "",
    speakers_block: str = "",
) -> str:
    """Assemble a single <turn_context> envelope combining all per-turn volatile signals."""
    parts: List[str] = []
    if context_block and context_block.strip():
        parts.append(context_block.strip())
    if memory_text and memory_text.strip():
        mem_s = memory_text.strip()
        if mem_s.lower().startswith("<memory>") and mem_s.lower().endswith("</memory>"):
            parts.append(mem_s)
        else:
            parts.append(f"<memory>\n{mem_s}\n</memory>")
    if reply_length_block and reply_length_block.strip():
        parts.append(reply_length_block.strip())
    if speakers_block and speakers_block.strip():
        parts.append(speakers_block.strip())
    if not parts:
        return ""
    body = "\n\n".join(parts)
    return f"<turn_context>\n{body}\n</turn_context>"


def apply_turn_context_to_messages(
    msgs: list,
    *,
    memory_str: Optional[str] = None,
    current_time_str: Optional[str] = None,
    mode: Optional[str] = None,
) -> list:
    """Stabilize system prompt prefix by relocating volatile per-turn blocks into <turn_context>.

    - In `legacy` mode: keeps volatile blocks in msgs[0] (100% backward-compatible rollback).
    - In `split` mode: strips <context>, <memory>, <reply_length_control>, <speakers_info> from
      msgs[0], strips any stale <turn_context> from all messages, and prefixes a single fresh
      <turn_context> right onto the latest user message so system + few-shot + prior turns stay
      byte-identical across turns.
    """
    if not msgs or not isinstance(msgs, list):
        return msgs

    active_mode = get_turn_context_mode(mode)
    mem_clean = str(memory_str).strip() if memory_str else ""

    if active_mode == "legacy":
        out_leg = [dict(m) if isinstance(m, dict) else m for m in msgs]
        if out_leg and isinstance(out_leg[0], dict) and out_leg[0].get("role") == "system":
            sys_c = _resolve_current_time_placeholder(
                str(out_leg[0].get("content") or ""), current_time_str
            )
            if mem_clean and mem_clean not in sys_c:
                if _MEMORY_BLOCK_RE.search(sys_c):
                    sys_c = _MEMORY_BLOCK_RE.sub(
                        lambda _m: f"\n<memory>\n{mem_clean}\n</memory>",
                        sys_c,
                        count=1,
                    )
                else:
                    sys_c = f"{sys_c}\n\n<memory>\n{mem_clean}\n</memory>"
            out_leg[0]["content"] = sys_c
        try:
            reply_length_router.inject_reply_length_into_messages(out_leg)
        except Exception:
            pass
        return out_leg

    # Split mode: clone list & modified message dicts so caller/Dialogue state is never mutated
    out_msgs: list = []
    for m in msgs:
        if isinstance(m, dict):
            mc = dict(m)
            c = mc.get("content")
            if isinstance(c, str) and "<turn_context>" in c.lower():
                mc["content"] = strip_turn_context_from_text(c)
            elif isinstance(c, list):
                new_parts = []
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        stripped_p = strip_turn_context_from_text(part["text"])
                        if stripped_p:
                            new_parts.append({**part, "text": stripped_p})
                    else:
                        new_parts.append(part)
                mc["content"] = new_parts
            out_msgs.append(mc)
        else:
            out_msgs.append(m)

    context_blocks: List[str] = []
    memory_blocks: List[str] = []
    speakers_blocks: List[str] = []

    if out_msgs and isinstance(out_msgs[0], dict) and out_msgs[0].get("role") == "system":
        sys_txt = str(out_msgs[0].get("content") or "")
        sys_txt, context_blocks = _extract_and_strip(_CONTEXT_BLOCK_RE, sys_txt)
        sys_txt, memory_blocks = _extract_and_strip(_MEMORY_BLOCK_RE, sys_txt)
        sys_txt, _ = _extract_and_strip(_REPLY_LEN_BLOCK_RE, sys_txt)
        sys_txt, speakers_blocks = _extract_and_strip(_SPEAKERS_BLOCK_RE, sys_txt)
        sys_txt = re.sub(r"\n{3,}", "\n\n", sys_txt).strip()
        out_msgs[0]["content"] = sys_txt

    # Resolve context block (with live HH:MM if placeholder still present)
    ctx_joined = "\n\n".join(
        _resolve_current_time_placeholder(b, current_time_str) for b in context_blocks if b.strip()
    )

    # Resolve memory text: prefer explicit memory_str, else non-empty <memory> extracted from system
    effective_memory = mem_clean
    if not effective_memory:
        for mb in memory_blocks:
            inner = _extract_inner_tag_text(mb, "memory")
            if inner:
                effective_memory = inner
                break

    try:
        reply_len_blk = reply_length_router.build_length_directive()
    except Exception:
        reply_len_blk = ""

    speakers_joined = "\n\n".join(b for b in speakers_blocks if b.strip())

    turn_ctx = build_turn_context_block(
        context_block=ctx_joined,
        memory_text=effective_memory,
        reply_length_block=reply_len_blk,
        speakers_block=speakers_joined,
    )
    if not turn_ctx:
        return out_msgs

    # Find the latest user message (skipping few-shot naturally since actual user turns come last)
    last_user_idx: Optional[int] = None
    for idx in range(len(out_msgs) - 1, -1, -1):
        m = out_msgs[idx]
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_idx = idx
            break

    if last_user_idx is not None:
        target = dict(out_msgs[last_user_idx])
        raw_content = target.get("content")
        if isinstance(raw_content, list):
            target["content"] = [{"type": "text", "text": f"{turn_ctx}\n\n"}] + list(raw_content)
        else:
            user_str = str(raw_content or "")
            target["content"] = f"{turn_ctx}\n\n{user_str}" if user_str else turn_ctx
        out_msgs[last_user_idx] = target
    elif out_msgs and isinstance(out_msgs[0], dict) and out_msgs[0].get("role") == "system":
        # Edge case: system-only dialogue with no user message yet
        base_sys = str(out_msgs[0].get("content") or "").rstrip()
        out_msgs[0]["content"] = f"{base_sys}\n\n{turn_ctx}" if base_sys else turn_ctx
    else:
        out_msgs.append({"role": "system", "content": turn_ctx})

    return out_msgs


def extract_usage_metrics(usage_obj: Any) -> Optional[Dict[str, Optional[int]]]:
    """Extract prompt_tokens, completion_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens from SDK usage."""
    if usage_obj is None:
        return None
    if isinstance(usage_obj, dict):
        d = usage_obj
    elif hasattr(usage_obj, "model_dump"):
        try:
            d = usage_obj.model_dump()
        except Exception:
            d = {}
    else:
        d = {}

    extra = getattr(usage_obj, "model_extra", None)
    if not isinstance(extra, dict):
        extra = {}

    def _get_int(*keys: str) -> Optional[int]:
        for k in keys:
            val = getattr(usage_obj, k, None)
            if val is None:
                val = d.get(k)
            if val is None:
                val = extra.get(k)
            if val is not None and not isinstance(val, bool):
                try:
                    return int(val)
                except (TypeError, ValueError):
                    pass
        return None

    pt = _get_int("prompt_tokens")
    ct = _get_int("completion_tokens")
    hit = _get_int("prompt_cache_hit_tokens")
    miss = _get_int("prompt_cache_miss_tokens")

    if hit is None:
        details = getattr(usage_obj, "prompt_tokens_details", None) or d.get("prompt_tokens_details")
        if isinstance(details, dict):
            raw_c = details.get("cached_tokens")
        else:
            raw_c = getattr(details, "cached_tokens", None)
        if raw_c is not None and not isinstance(raw_c, bool):
            try:
                hit = int(raw_c)
            except (TypeError, ValueError):
                pass

    if pt is None and hit is None and miss is None:
        return None
    pt_val = int(pt or 0)
    if hit is None and miss is not None and pt_val >= miss:
        hit = max(0, pt_val - miss)
    if miss is None and hit is not None and pt_val >= hit:
        miss = max(0, pt_val - hit)
    return {
        "prompt_tokens": pt_val,
        "completion_tokens": int(ct or 0),
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
    }


def record_llm_usage(
    *,
    prompt_tokens: int,
    completion_tokens: int = 0,
    cache_hit_tokens: Optional[int] = None,
    cache_miss_tokens: Optional[int] = None,
    ttft_ms: Optional[float] = None,
    model_name: str = "",
) -> Dict[str, Any]:
    """Record per-turn LLM prefix cache & TTFT stats in memory and publish to runtime_events."""
    pt = max(0, int(prompt_tokens or 0))
    ct = max(0, int(completion_tokens or 0))
    hit = max(0, int(cache_hit_tokens)) if cache_hit_tokens is not None else None
    miss = max(0, int(cache_miss_tokens)) if cache_miss_tokens is not None else (
        max(0, pt - hit) if hit is not None else None
    )
    hit_rate = round((hit / pt) * 100.0, 1) if (hit is not None and pt > 0) else None
    ttft_val = round(float(ttft_ms), 1) if ttft_ms is not None else None
    now = time.time()

    entry = {
        "ts": now,
        "model": str(model_name or ""),
        "mode": get_turn_context_mode(),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "hit_rate_pct": hit_rate,
        "ttft_ms": ttft_val,
    }
    with _stats_lock:
        _usage_history.append(entry)
        _usage_totals["turns"] += 1
        _usage_totals["prompt_tokens_total"] += pt
        _usage_totals["completion_tokens_total"] += ct
        if hit is not None:
            _usage_totals["cache_measured_prompt_tokens_total"] += pt
            _usage_totals["cache_hit_tokens_total"] += hit
            _usage_totals["last_cache_hit_tokens"] = hit
        if miss is not None:
            _usage_totals["cache_miss_tokens_total"] += miss
            _usage_totals["last_cache_miss_tokens"] = miss
        _usage_totals["last_prompt_tokens"] = pt
        _usage_totals["last_hit_rate"] = hit_rate
        if ttft_val is not None:
            _usage_totals["last_ttft_ms"] = ttft_val
        if model_name:
            _usage_totals["last_model"] = str(model_name)
        _usage_totals["last_ts"] = now

    try:
        runtime_events.publish_event(
            "llm_usage",
            model=entry["model"],
            prompt_tokens=pt,
            cache_hit_tokens=hit,
            cache_miss_tokens=miss,
            hit_rate_pct=hit_rate,
            ttft_ms=ttft_val,
        )
    except Exception:
        pass
    return entry


def reset_llm_cache_stats() -> None:
    """Clear in-memory LLM prefix cache stats (for isolated unit tests)."""
    with _stats_lock:
        _usage_history.clear()
        _usage_totals.update(
            {
                "turns": 0,
                "prompt_tokens_total": 0,
                "cache_measured_prompt_tokens_total": 0,
                "cache_hit_tokens_total": 0,
                "cache_miss_tokens_total": 0,
                "completion_tokens_total": 0,
                "last_prompt_tokens": 0,
                "last_cache_hit_tokens": 0,
                "last_cache_miss_tokens": 0,
                "last_hit_rate": None,
                "last_ttft_ms": None,
                "last_model": "",
                "last_ts": 0.0,
            }
        )


def get_llm_cache_stats() -> Dict[str, Any]:
    """Return structured prefix-cache hit rate & TTFT summary for 18080 Console /api/state."""
    with _stats_lock:
        tot = dict(_usage_totals)
        recent = list(_usage_history)[-10:]

    pt_sum = int(tot["prompt_tokens_total"])
    measured_pt_sum = int(tot.get("cache_measured_prompt_tokens_total") or 0)
    hit_sum = int(tot["cache_hit_tokens_total"])
    overall_rate = round((hit_sum / measured_pt_sum) * 100.0, 1) if measured_pt_sum > 0 else None

    recent_pt = sum(int(r["prompt_tokens"] or 0) for r in recent if r.get("prompt_cache_hit_tokens") is not None)
    recent_hit = sum(int(r["prompt_cache_hit_tokens"] or 0) for r in recent if r.get("prompt_cache_hit_tokens") is not None)
    recent_rate = round((recent_hit / recent_pt) * 100.0, 1) if recent_pt > 0 else None

    ttfts = [float(r["ttft_ms"]) for r in recent if r.get("ttft_ms") is not None]
    avg_ttft = round(sum(ttfts) / len(ttfts), 1) if ttfts else None

    return {
        "mode": get_turn_context_mode(),
        "turns": int(tot["turns"]),
        "overall_hit_rate_pct": overall_rate,
        "recent_hit_rate_pct": recent_rate,
        "last_hit_rate_pct": tot["last_hit_rate"],
        "last_prompt_tokens": int(tot["last_prompt_tokens"]),
        "last_cache_hit_tokens": int(tot["last_cache_hit_tokens"]),
        "last_cache_miss_tokens": int(tot["last_cache_miss_tokens"]),
        "prompt_tokens_total": pt_sum,
        "cache_hit_tokens_total": hit_sum,
        "cache_miss_tokens_total": int(tot["cache_miss_tokens_total"]),
        "last_ttft_ms": tot["last_ttft_ms"],
        "recent_avg_ttft_ms": avg_ttft,
        "last_model": tot["last_model"],
    }


class _UsageTrackingStream:
    """Transparent proxy around OpenAI SDK streaming responses that logs prefix cache usage & TTFT."""

    def __init__(self, inner: Any, *, t0: float, model_name: str = "", log_fn=None) -> None:
        self._inner = inner
        self._t0 = t0
        self._model_name = model_name
        self._log_fn = log_fn
        self._ttft_ms: Optional[float] = None
        self._usage_obj: Any = None
        self._recorded = False

    def _inspect_chunk(self, chunk: Any) -> None:
        if self._ttft_ms is None:
            choices = getattr(chunk, "choices", None)
            if choices:
                delta = getattr(choices[0], "delta", None)
                if delta is not None:
                    content = getattr(delta, "content", None)
                    tool_calls = getattr(delta, "tool_calls", None)
                    if content or tool_calls:
                        self._ttft_ms = (time.perf_counter() - self._t0) * 1000.0
        u = getattr(chunk, "usage", None)
        if u is not None:
            self._usage_obj = u

    def _finalize(self) -> None:
        if self._recorded:
            return
        self._recorded = True
        metrics = extract_usage_metrics(self._usage_obj)
        if metrics is None:
            return
        rec = record_llm_usage(
            prompt_tokens=int(metrics["prompt_tokens"] or 0),
            completion_tokens=int(metrics["completion_tokens"] or 0),
            cache_hit_tokens=metrics["prompt_cache_hit_tokens"],
            cache_miss_tokens=metrics["prompt_cache_miss_tokens"],
            ttft_ms=self._ttft_ms,
            model_name=self._model_name,
        )
        rate_str = f"{rec['hit_rate_pct']:.1f}%" if rec["hit_rate_pct"] is not None else "n/a"
        ttft_str = f"{rec['ttft_ms']:.0f}ms" if rec["ttft_ms"] is not None else "n/a"
        msg = (
            f"LLM 前缀缓存 | model={rec['model']} | hit={rec['prompt_cache_hit_tokens']}/"
            f"{rec['prompt_tokens']} ({rate_str}) | miss={rec['prompt_cache_miss_tokens']} | "
            f"out={rec['completion_tokens']} | ttft={ttft_str} | mode={rec['mode']}"
        )
        if callable(self._log_fn):
            try:
                self._log_fn(msg)
                return
            except Exception:
                pass
        print(f"[LLMCache] {msg}", flush=True)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._inner)
        except StopIteration:
            self._finalize()
            raise
        self._inspect_chunk(chunk)
        return chunk

    def close(self):
        self._finalize()
        close_fn = getattr(self._inner, "close", None)
        if callable(close_fn):
            return close_fn()
        return None

    def __enter__(self):
        enter_fn = getattr(self._inner, "__enter__", None)
        if callable(enter_fn):
            enter_fn()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._finalize()
        exit_fn = getattr(self._inner, "__exit__", None)
        if callable(exit_fn):
            return exit_fn(exc_type, exc_val, exc_tb)
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap_llm_client_for_usage(llm_provider: Any, *, log_fn=None) -> bool:
    """Idempotently wrap llm_provider.client.chat.completions.create to capture cache hit tokens & TTFT."""
    client = getattr(llm_provider, "client", None)
    chat = getattr(client, "chat", None)
    completions = getattr(chat, "completions", None)
    orig_create = getattr(completions, "create", None)
    if completions is None or not callable(orig_create):
        return False
    if getattr(completions, "_xiaozhi_cache_telemetry_wrapped", False):
        return True

    state = {"stream_options_unsupported": False}

    @functools.wraps(orig_create)
    def _tracked_create(*args, **kwargs):
        is_stream = bool(kwargs.get("stream", False))
        req_kwargs = dict(kwargs)
        added_stream_opts = False
        if is_stream and "stream_options" not in req_kwargs and not state["stream_options_unsupported"]:
            req_kwargs["stream_options"] = {"include_usage": True}
            added_stream_opts = True

        t0 = time.perf_counter()
        try:
            res = orig_create(*args, **req_kwargs)
        except Exception as exc:
            if added_stream_opts and "stream_options" in str(exc).lower():
                state["stream_options_unsupported"] = True
                req_kwargs.pop("stream_options", None)
                t0 = time.perf_counter()
                res = orig_create(*args, **req_kwargs)
            else:
                raise

        model_name = str(req_kwargs.get("model") or getattr(llm_provider, "model_name", "") or "")
        if is_stream:
            return _UsageTrackingStream(res, t0=t0, model_name=model_name, log_fn=log_fn)

        # Non-streaming response telemetry
        u = getattr(res, "usage", None)
        metrics = extract_usage_metrics(u)
        if metrics is not None:
            record_llm_usage(
                prompt_tokens=int(metrics["prompt_tokens"] or 0),
                completion_tokens=int(metrics["completion_tokens"] or 0),
                cache_hit_tokens=metrics["prompt_cache_hit_tokens"],
                cache_miss_tokens=metrics["prompt_cache_miss_tokens"],
                ttft_ms=(time.perf_counter() - t0) * 1000.0,
                model_name=model_name,
            )
        return res

    completions.create = _tracked_create
    completions._xiaozhi_cache_telemetry_wrapped = True
    return True


def apply_llm_prefix_cache_patch() -> None:
    """Install OpenAI.LLMProvider.__init__ hook to track prompt_cache_hit_tokens & TTFT (F-2)."""
    from core.providers.llm.openai import openai as openai_llm_mod

    cls = openai_llm_mod.LLMProvider

    def _log_info(msg: str) -> None:
        openai_llm_mod.logger.bind(tag=openai_llm_mod.TAG).info(msg)

    def _wrap_init(_orig_init):
        @functools.wraps(_orig_init)
        def _patched_init(self, config, *args, **kwargs):
            _orig_init(self, config, *args, **kwargs)
            try:
                wrap_llm_client_for_usage(self, log_fn=_log_info)
            except Exception:
                pass

        return _patched_init

    already_applied = bool(getattr(cls, "_prefix_cache_telemetry_patched", False))
    ok, _ = patch_registry.install_patch(
        "llm_prefix_cache",
        cls,
        "__init__",
        _wrap_init,
        category="experience",
        target="OpenAI.LLMProvider.__init__",
        required_params=("self", "config"),
        require_async=False,
        flag_obj=cls,
        flag_attr="_prefix_cache_telemetry_patched",
    )
    if ok and not already_applied:
        print(
            f"[LLMCache] 已挂载 LLM 前缀缓存与 TTFT 遥测补丁 (turn_context_mode={get_turn_context_mode()})",
            flush=True,
        )

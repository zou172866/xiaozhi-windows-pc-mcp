"""Dedicated Sherpa-ONNX streaming Keyword Spotter (KWS) engine for PC mic wake-standby.

Design contract:
- Zero import-time side effects: neither `sherpa_onnx` nor `numpy` is imported until a PC mic
  actually enters wake-standby and the optional ONNX model directory exists.
- Uses `sherpa-onnx-kws-zipformer-wenetspeech-3.3M` (`ppinyin` tokens) when present in
  `models/sherpa-onnx-kws/`, bypassing `SileroVAD` threshold misses and general ASR drift.
- Gracefully reports unavailable when model files are absent so standby seamlessly falls back
  to the existing `SileroVAD + SenseVoiceSmall` pipeline (zero breaking dependency).
- All `OnlineStream` operations execute strictly on the caller's serialized ASR worker thread
  (never shared concurrently across threads); event-loop resets set a pending flag instead.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Dict, FrozenSet, Optional, Tuple

import xz_common
import zh_pinyin

KWS_SAMPLE_RATE = 16000
KWS_CHUNK_BYTES = 1920 * 2  # 120ms @ 16kHz 16-bit mono PCM (2 x 60ms browser frames)
KWS_MAX_PENDING_BYTES = KWS_SAMPLE_RATE * 2 * 4  # 4s safety cap
KWS_HIT_DEBOUNCE_SEC = 1.2

DEFAULT_KEYWORDS_SCORE = 1.8
DEFAULT_KEYWORDS_THRESHOLD = 0.22
DEFAULT_NUM_TRAILING_BLANKS = 1
DEFAULT_NUM_THREADS = 2

_ENGINE_LOCK = threading.Lock()
_SPOTTER: Any = None
_VALID_TOKENS: Optional[FrozenSet[str]] = None
_LOADED_DIR: str = ""
_LOAD_FAILED_AT: float = 0.0
_LOAD_FAILED_DIR: str = ""
_LOAD_RETRY_COOLDOWN_SEC = 30.0
_EMPTY_KEYWORDS_FILE: str = ""


def is_kws_enabled() -> bool:
    raw = str(os.environ.get("XIAOZHI_PC_KWS_ENABLED", "1") or "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def get_kws_model_dir() -> Path:
    override = str(os.environ.get("XIAOZHI_KWS_MODEL_DIR", "") or "").strip()
    if override:
        return Path(override)
    return Path(xz_common.ROOT_DIR) / "models" / "sherpa-onnx-kws"


def _parse_float_env(name: str, default: float, lo: float, hi: float) -> float:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
        if val != val:  # NaN
            return default
        return max(lo, min(hi, val))
    except ValueError:
        return default


def _parse_int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
        return max(lo, min(hi, val))
    except ValueError:
        return default


def resolve_kws_model_files(model_dir: Optional[str | Path] = None) -> Optional[Dict[str, str]]:
    """Return `{tokens, encoder, decoder, joiner}` if all 4 required KWS files exist."""
    base = Path(model_dir) if model_dir is not None else get_kws_model_dir()
    if not base.is_dir():
        return None
    tokens = base / "tokens.txt"
    try:
        if not tokens.is_file() or tokens.stat().st_size <= 0:
            return None
    except OSError:
        return None

    def _pick(prefix: str) -> Optional[Path]:
        try:
            cands = sorted(
                p
                for p in base.iterdir()
                if p.is_file()
                and p.name.startswith(prefix)
                and p.name.endswith(".onnx")
                and p.stat().st_size > 0
            )
        except OSError:
            return None
        if not cands:
            return None
        int8_cands = [p for p in cands if p.name.endswith(".int8.onnx")]
        return int8_cands[0] if int8_cands else cands[0]

    enc = _pick("encoder-")
    dec = _pick("decoder-")
    joi = _pick("joiner-")
    if not (enc and dec and joi):
        return None
    return {
        "tokens": str(tokens),
        "encoder": str(enc),
        "decoder": str(dec),
        "joiner": str(joi),
    }


def _load_valid_tokens(tokens_path: str) -> FrozenSet[str]:
    toks: set[str] = set()
    with open(tokens_path, "r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            tok = s.split()[0]
            if tok and not tok.startswith("<") and not tok.startswith("#"):
                toks.add(tok)
    return frozenset(toks)


def _ensure_empty_keywords_file() -> str:
    global _EMPTY_KEYWORDS_FILE
    if _EMPTY_KEYWORDS_FILE and os.path.isfile(_EMPTY_KEYWORDS_FILE):
        return _EMPTY_KEYWORDS_FILE
    path = os.path.join(tempfile.gettempdir(), "xiaozhi_pc_kws_empty_keywords.txt")
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("")
    _EMPTY_KEYWORDS_FILE = path
    return path


def _ensure_spotter_loaded() -> Tuple[Any, FrozenSet[str]]:
    """Lazily initialize the process-wide `sherpa_onnx.KeywordSpotter` singleton."""
    global _SPOTTER, _VALID_TOKENS, _LOADED_DIR, _LOAD_FAILED_AT, _LOAD_FAILED_DIR
    if not is_kws_enabled():
        return None, frozenset()

    model_dir = str(get_kws_model_dir())
    if _SPOTTER is not None and _LOADED_DIR == model_dir and _VALID_TOKENS is not None:
        return _SPOTTER, _VALID_TOKENS

    now = time.monotonic()
    if (
        _LOAD_FAILED_DIR == model_dir
        and _LOAD_FAILED_AT > 0.0
        and (now - _LOAD_FAILED_AT) < _LOAD_RETRY_COOLDOWN_SEC
    ):
        return None, frozenset()

    with _ENGINE_LOCK:
        if _SPOTTER is not None and _LOADED_DIR == model_dir and _VALID_TOKENS is not None:
            return _SPOTTER, _VALID_TOKENS
        files = resolve_kws_model_files(model_dir)
        if files is None:
            return None, frozenset()
        try:
            import sherpa_onnx

            if not hasattr(sherpa_onnx, "KeywordSpotter"):
                raise RuntimeError("sherpa_onnx.KeywordSpotter not available")
            valid_tokens = _load_valid_tokens(files["tokens"])
            empty_kw = _ensure_empty_keywords_file()
            kw_score = _parse_float_env("XIAOZHI_PC_KWS_SCORE", DEFAULT_KEYWORDS_SCORE, 0.2, 5.0)
            kw_thresh = _parse_float_env("XIAOZHI_PC_KWS_THRESHOLD", DEFAULT_KEYWORDS_THRESHOLD, 0.05, 0.95)
            num_threads = _parse_int_env("XIAOZHI_PC_KWS_THREADS", DEFAULT_NUM_THREADS, 1, 4)
            trailing_blanks = _parse_int_env(
                "XIAOZHI_PC_KWS_TRAILING_BLANKS", DEFAULT_NUM_TRAILING_BLANKS, 0, 10
            )
            spotter = sherpa_onnx.KeywordSpotter(
                tokens=files["tokens"],
                encoder=files["encoder"],
                decoder=files["decoder"],
                joiner=files["joiner"],
                keywords_file=empty_kw,
                num_threads=num_threads,
                sample_rate=KWS_SAMPLE_RATE,
                feature_dim=80,
                max_active_paths=4,
                keywords_score=kw_score,
                keywords_threshold=kw_thresh,
                num_trailing_blanks=trailing_blanks,
                provider="cpu",
            )
            _SPOTTER = spotter
            _VALID_TOKENS = valid_tokens
            _LOADED_DIR = model_dir
            _LOAD_FAILED_AT = 0.0
            _LOAD_FAILED_DIR = ""
            print(
                f"[PCWakeKWS] 已加载 Sherpa-ONNX 流式唤醒模型 ({Path(files['encoder']).name}, "
                f"score={kw_score:.2f}, threshold={kw_thresh:.2f}, tokens={len(valid_tokens)})",
                flush=True,
            )
            return spotter, valid_tokens
        except Exception as exc:
            _LOAD_FAILED_AT = time.monotonic()
            _LOAD_FAILED_DIR = model_dir
            print(f"[PCWakeKWS] 加载流式唤醒模型失败，自动回退 VAD+ASR 待命: {exc}", flush=True)
            return None, frozenset()


def is_kws_model_ready() -> bool:
    """Cheap check (no model instantiation) whether KWS is enabled and model files are present."""
    if not is_kws_enabled():
        return False
    model_dir = str(get_kws_model_dir())
    if _SPOTTER is not None and _LOADED_DIR == model_dir:
        return True
    now = time.monotonic()
    if (
        _LOAD_FAILED_DIR == model_dir
        and _LOAD_FAILED_AT > 0.0
        and (now - _LOAD_FAILED_AT) < _LOAD_RETRY_COOLDOWN_SEC
    ):
        return False
    return resolve_kws_model_files(model_dir) is not None


def _sync_and_compile_conn_keywords(
    conn: Any,
    valid_tokens: Optional[FrozenSet[str]] = None,
) -> Tuple[str, ...]:
    """Read hot-synced `wakeup_words` on `conn` and compile deduplicated KWS keyword lines."""
    try:
        from patches.emotion import wakeup as _wakeup_patch

        _wakeup_patch.sync_wakeup_words_from_yaml(conn)
    except Exception:
        pass
    try:
        from patches import pc_pcm_input as _pc_pcm

        words = _pc_pcm.get_effective_wakeup_words(conn)
    except Exception:
        cfg = getattr(conn, "config", None) or {}
        raw = cfg.get("wakeup_words") if isinstance(cfg, dict) else None
        words = [str(w).strip() for w in (raw or ()) if str(w or "").strip()]
    return tuple(zh_pinyin.build_kws_keywords(words, valid_tokens=valid_tokens))


def is_kws_available_for_conn(conn: Any) -> bool:
    """Return True if KWS is enabled, model files exist, and `conn` has >=1 compilable Chinese wake word."""
    if conn is None or not is_kws_model_ready():
        return False
    # Fast check using cached valid_tokens (or None before first load) so pure-English configs fall back to ASR
    compiled = _sync_and_compile_conn_keywords(conn, valid_tokens=_VALID_TOKENS)
    return bool(compiled)


def reset_conn_kws(conn: Any, *, drop_stream: bool = False) -> None:
    """Clear buffered PCM and mark the per-conn KWS stream for safe reset/drop on the worker thread."""
    if conn is None:
        return
    buf = getattr(conn, "_pc_kws_pcm_buf", None)
    if isinstance(buf, bytearray):
        buf.clear()
    try:
        conn._pc_kws_reset_pending = True
        if drop_stream:
            conn._pc_kws_drop_pending = True
    except Exception:
        pass


def reset_engine_for_tests() -> None:
    """Reset module singleton state (unit tests only)."""
    global _SPOTTER, _VALID_TOKENS, _LOADED_DIR, _LOAD_FAILED_AT, _LOAD_FAILED_DIR
    with _ENGINE_LOCK:
        _SPOTTER = None
        _VALID_TOKENS = None
        _LOADED_DIR = ""
        _LOAD_FAILED_AT = 0.0
        _LOAD_FAILED_DIR = ""


def feed_and_detect_sync(conn: Any, pcm_bytes: bytes) -> Optional[str]:
    """Feed 16kHz 16-bit mono PCM bytes to `conn`'s streaming KWS worker and return matched label or None.

    Must be called serially per `conn` (e.g. inside the dedicated single-thread `asr` executor).
    """
    if conn is None or not pcm_bytes:
        return None
    spotter, valid_tokens = _ensure_spotter_loaded()
    if spotter is None:
        return None

    if getattr(conn, "_pc_kws_drop_pending", False):
        try:
            conn._pc_kws_stream = None
            conn._pc_kws_sig = ()
            conn._pc_kws_drop_pending = False
            conn._pc_kws_reset_pending = False
        except Exception:
            pass

    kw_lines = _sync_and_compile_conn_keywords(conn, valid_tokens=valid_tokens)
    if not kw_lines:
        return None

    stream = getattr(conn, "_pc_kws_stream", None)
    prev_sig = getattr(conn, "_pc_kws_sig", ())
    if stream is None or prev_sig != kw_lines:
        inline_kw = "/".join(kw_lines)
        stream = spotter.create_stream(inline_kw)
        conn._pc_kws_stream = stream
        conn._pc_kws_sig = kw_lines
        conn._pc_kws_reset_pending = False
        if prev_sig != kw_lines:
            labels = [line.rsplit("@", 1)[-1].strip() for line in kw_lines]
            print(f"[PCWakeKWS] 已激活流式唤醒词 ({len(labels)} 条): {', '.join(labels)}", flush=True)
    elif getattr(conn, "_pc_kws_reset_pending", False):
        try:
            spotter.reset_stream(stream)
        except Exception:
            stream = spotter.create_stream("/".join(kw_lines))
            conn._pc_kws_stream = stream
        conn._pc_kws_reset_pending = False

    if len(pcm_bytes) % 2 != 0:
        pcm_bytes = pcm_bytes[:-1]
    if not pcm_bytes:
        return None

    import numpy as np

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    stream.accept_waveform(KWS_SAMPLE_RATE, samples)

    matched: Optional[str] = None
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
        res = str(spotter.get_result(stream) or "").strip()
        if res:
            try:
                spotter.reset_stream(stream)
            except Exception:
                conn._pc_kws_stream = None
            matched = res
            break
    return matched

"""Sherpa-ONNX dedicated async ASR executor, modelscope lazy stub, ASR homophone corrections, and idle trim (H-1, G-1/2/4).

Zero import-time side effects: call `install_memory_optimizations()` explicitly from `boot.install_all()`.
"""

from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import ThreadPoolExecutor
import functools
import os
import re
import sys
import threading
import time

import asr_corrections
import patch_registry
import runtime_events
import xz_common

ROOT_DIR = xz_common.ROOT_DIR
_atexit_registered = False

# G-2: Dedicated single-thread Sherpa ASR executor (isolates CPU decode from default asyncio.to_thread pool)
_asr_executor_lock = threading.Lock()
_asr_executor: ThreadPoolExecutor | None = None


def get_asr_executor() -> ThreadPoolExecutor:
    global _asr_executor
    with _asr_executor_lock:
        if _asr_executor is None or getattr(_asr_executor, "_shutdown", False):
            _asr_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
        return _asr_executor


def _shutdown_asr_executor() -> None:
    global _asr_executor
    with _asr_executor_lock:
        ex = _asr_executor
        _asr_executor = None
    if ex is not None:
        try:
            ex.shutdown(wait=False)
        except Exception:
            pass




# G-1: Idle-triggered gc.collect() + glibc malloc_trim(0) after 25s of post-turn idle
IDLE_TRIM_DELAY_SECONDS = 25.0


def _load_libc_malloc_trim():
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        fn = libc.malloc_trim
        fn.argtypes = [ctypes.c_size_t]
        fn.restype = ctypes.c_int
        return fn
    except Exception:
        return None


def _is_pipeline_busy_for_trim() -> bool:
    try:
        if runtime_events.is_any_device_speaking():
            return True
        snap = runtime_events.get_latest_runtime_snapshot()
        if snap.get("activity") in ("speaking", "listening"):
            return True
        for conn in runtime_events.get_active_connections():
            if getattr(conn, "client_have_voice", False) or getattr(conn, "client_is_speaking", False):
                return True
    except Exception:
        pass
    try:
        import mei_emotion_router as _mer

        if _mer.is_live_gsv_busy():
            return True
    except Exception:
        pass
    return False


class IdleMemoryTrimmer:
    """Runs gc.collect() + malloc_trim(0) ONLY after `delay_seconds` (20-30s) of idle following a completed turn."""

    def __init__(
        self,
        *,
        delay_seconds: float = IDLE_TRIM_DELAY_SECONDS,
        busy_checker=None,
        trim_fn=None,
        gc_fn=None,
    ) -> None:
        import gc

        self.delay_seconds = max(0.02, float(delay_seconds))
        self._busy_checker = busy_checker or _is_pipeline_busy_for_trim
        self._trim_fn = trim_fn
        self._gc_fn = gc_fn or gc.collect
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._needs_trim = False
        self._idle_since: float = 0.0
        self._trim_count: int = 0
        self._suppressed_busy_count: int = 0
        self._last_trim_ts: float = 0.0
        self._thread: threading.Thread | None = None

    def on_activity_start(self) -> None:
        with self._cond:
            self._idle_since = 0.0
            self._cond.notify_all()

    def on_turn_completed(self) -> None:
        with self._cond:
            self._needs_trim = True
            self._idle_since = time.monotonic()
            self._cond.notify_all()

    def on_runtime_event(self, evt: dict) -> None:
        etype = str(evt.get("type") or "")
        if etype in ("listen_start", "user_message"):
            self.on_activity_start()
        elif etype == "tts_state":
            st = str(evt.get("state") or "")
            if st in ("start", "sentence_start"):
                self.on_activity_start()
            elif st == "stop":
                self.on_turn_completed()
        elif etype == "connection_closed":
            if not self._busy_checker():
                self.on_turn_completed()

    def _run_trim_once(self) -> bool:
        if self._busy_checker():
            with self._cond:
                self._suppressed_busy_count += 1
                self._idle_since = 0.0
            return False
        try:
            self._gc_fn()
        except Exception:
            pass
        if callable(self._trim_fn):
            try:
                self._trim_fn(0)
            except Exception:
                pass
        with self._cond:
            self._trim_count += 1
            self._last_trim_ts = time.monotonic()
            self._needs_trim = False
            self._idle_since = 0.0
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            should_run = False
            with self._cond:
                while not self._stop.is_set():
                    if not self._needs_trim or self._idle_since <= 0.0:
                        self._cond.wait(timeout=30.0)
                        continue
                    rem = (self._idle_since + self.delay_seconds) - time.monotonic()
                    if rem <= 0.0:
                        should_run = True
                        break
                    self._cond.wait(timeout=min(5.0, max(0.01, rem)))
            if self._stop.is_set():
                break
            if should_run:
                self._run_trim_once()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            t = threading.Thread(target=self._loop, name="glibc-idle-malloc-trim", daemon=True)
            t.start()
            self._thread = t

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)

    def stats(self) -> dict:
        with self._lock:
            return {
                "delay_seconds": self.delay_seconds,
                "needs_trim": self._needs_trim,
                "trim_count": self._trim_count,
                "suppressed_busy_count": self._suppressed_busy_count,
                "last_trim_ts": self._last_trim_ts,
                "alive": bool(self._thread and self._thread.is_alive()),
            }


_idle_memory_trimmer: IdleMemoryTrimmer | None = None


def get_idle_memory_trimmer() -> IdleMemoryTrimmer | None:
    return _idle_memory_trimmer


def get_idle_trim_stats() -> dict:
    if _idle_memory_trimmer is None:
        return {"delay_seconds": IDLE_TRIM_DELAY_SECONDS, "trim_count": 0, "alive": False}
    return _idle_memory_trimmer.stats()




# 6. Memory optimization for Sherpa-ONNX local ASR + dedicated ASR executor (G-2) + ASR corrections (G-4) + idle trim (G-1)
def _install_memory_optimizations(*, start_trimmer: bool = True):
    global _idle_memory_trimmer, _atexit_registered
    if not _atexit_registered:
        atexit.register(_shutdown_asr_executor)
        _atexit_registered = True
    import types

    # 6a. Lazy-stub modelscope.hub.file_download when Sherpa-ONNX model files already exist locally.
    #     Eagerly importing modelscope drags in PyTorch/scipy/datasets (~330MB RSS) even when unused.
    sherpa_dir = os.path.join(ROOT_DIR, "models", "sherpa-onnx-sense-voice")
    has_local_sherpa = os.path.isfile(os.path.join(sherpa_dir, "model.int8.onnx")) and os.path.isfile(
        os.path.join(sherpa_dir, "tokens.txt")
    )
    if has_local_sherpa and "modelscope" not in sys.modules:
        _ms_pkg = types.ModuleType("modelscope")
        _ms_pkg.__path__ = []
        _ms_hub = types.ModuleType("modelscope.hub")
        _ms_hub.__path__ = []
        _ms_dl = types.ModuleType("modelscope.hub.file_download")

        def _lazy_model_file_download(*args, **kwargs):
            for mod_name in ("modelscope.hub.file_download", "modelscope.hub", "modelscope"):
                if sys.modules.get(mod_name) in (_ms_dl, _ms_hub, _ms_pkg):
                    sys.modules.pop(mod_name, None)
            from modelscope.hub.file_download import model_file_download as _real_download
            return _real_download(*args, **kwargs)

        _ms_dl.model_file_download = _lazy_model_file_download
        _ms_hub.file_download = _ms_dl
        _ms_pkg.hub = _ms_hub
        sys.modules["modelscope"] = _ms_pkg
        sys.modules["modelscope.hub"] = _ms_hub
        sys.modules["modelscope.hub.file_download"] = _ms_dl

    # Seed default config/runtime/asr_corrections.json on startup if writable (fail-soft)
    try:
        asr_corrections.ensure_asr_corrections_file()
    except Exception:
        pass

    # 6b. Offload Sherpa-ONNX CPU decode_stream to dedicated 1-worker 'asr' executor (G-2)
    #     and apply hot-reloaded post-ASR homophone corrections (G-4) to ALL Sherpa outputs.
    try:
        import core.providers.asr.sherpa_onnx_local as _sherpa_mod

        _TAG_RE = re.compile(r"<\|[^|]+\|>")

        def _wrap_speech_to_text(_orig_stt):
            @functools.wraps(_orig_stt)
            async def _async_speech_to_text(self, opus_data, session_id, artifacts=None, *args, **kwargs):
                file_path = None
                try:
                    if artifacts is None:
                        return "", None
                    file_path = artifacts.file_path
                    t_asr_start = time.monotonic()

                    def _decode():
                        s = self.model.create_stream()
                        samples, sample_rate = self.read_wave(file_path)
                        s.accept_waveform(sample_rate, samples)
                        self.model.decode_stream(s)
                        return s.result.text

                    loop = asyncio.get_running_loop()
                    text = await loop.run_in_executor(get_asr_executor(), _decode)
                    asr_sec = time.monotonic() - t_asr_start
                    if text:
                        text = _TAG_RE.sub("", text).strip()
                    if text:
                        text = asr_corrections.apply_asr_corrections(text)
                    _sherpa_mod.logger.bind(tag=_sherpa_mod.TAG).debug(
                        f"语音识别耗时: {asr_sec:.3f}s | 结果: {text}"
                    )
                    return text, file_path
                except Exception as exc:
                    _sherpa_mod.logger.bind(tag=_sherpa_mod.TAG).error(
                        f"语音识别失败: {exc}", exc_info=True
                    )
                    return "", file_path

            return _async_speech_to_text

        patch_registry.install_patch(
            "sherpa_async_asr",
            _sherpa_mod.ASRProvider,
            "speech_to_text",
            _wrap_speech_to_text,
            category="experience",
            target="SherpaASR.ASRProvider.speech_to_text",
            required_params=("self", "opus_data", "session_id"),
            require_async=True,
            flag_obj=_sherpa_mod.ASRProvider,
            flag_attr="_sherpa_async_patched",
        )
    except Exception as _s_exc:
        patch_registry.record_patch(
            "sherpa_async_asr",
            "failed",
            target="SherpaASR.ASRProvider.speech_to_text",
            detail=str(_s_exc),
            contract_ok=False,
            category="experience",
        )
        print(f"[MemOpt] SherpaASR 异步补丁跳过: {_s_exc}", flush=True)

    # 6b-2. Universal post-ASR homophone corrections hook on ASRProviderBase.speech_to_text_wrapper (G-4)
    #       Ensures FunASR (dict {"content": ...}) and all non-Sherpa ASR providers also get hot-reloaded corrections.
    try:
        from core.providers.asr.base import ASRProviderBase

        if not getattr(ASRProviderBase, "_asr_corrections_wrapper_patched", False):
            _orig_base_stt_wrapper = ASRProviderBase.speech_to_text_wrapper

            @functools.wraps(_orig_base_stt_wrapper)
            async def _asr_corrections_stt_wrapper(self, pcm_data, session_id, *args, **kwargs):
                res = await _orig_base_stt_wrapper(self, pcm_data, session_id, *args, **kwargs)
                if not isinstance(res, tuple) or len(res) < 2:
                    return res
                text, file_path = res[0], res[1]
                # Patched SherpaASR already applied corrections inside speech_to_text(); avoid double-applying on chained rules.
                already_corrected = bool(
                    isinstance(text, str) and getattr(type(self), "_sherpa_async_patched", False)
                )
                if not already_corrected:
                    if isinstance(text, str) and text:
                        text = asr_corrections.apply_asr_corrections(text)
                    elif isinstance(text, dict) and isinstance(text.get("content"), str) and text["content"]:
                        text = dict(text)
                        text["content"] = asr_corrections.apply_asr_corrections(text["content"])
                return (text, file_path, *res[2:]) if len(res) > 2 else (text, file_path)

            ASRProviderBase.speech_to_text_wrapper = _asr_corrections_stt_wrapper
            ASRProviderBase._asr_corrections_wrapper_patched = True
    except Exception as _u_exc:
        print(f"[MemOpt] 通用 ASR 同音纠错补丁跳过: {_u_exc}", flush=True)

    # 6c. Idle-triggered gc.collect() + glibc malloc_trim(0) 25s after completed turns (G-1)
    try:
        if _idle_memory_trimmer is None:
            trimmer = IdleMemoryTrimmer(
                delay_seconds=IDLE_TRIM_DELAY_SECONDS,
                trim_fn=_load_libc_malloc_trim(),
            )
            runtime_events.subscribe(trimmer.on_runtime_event)
            if start_trimmer:
                trimmer.start()
            _idle_memory_trimmer = trimmer
    except Exception:
        pass



install_memory_optimizations = _install_memory_optimizations

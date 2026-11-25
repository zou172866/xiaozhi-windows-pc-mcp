"""Sherpa-ONNX dedicated async ASR executor, modelscope lazy stub, ASR homophone corrections, and idle trim (H-1, G-1/2/4).

Zero import-time side effects: call `install_memory_optimizations()` explicitly from `boot.install_all()`.
"""

from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import ThreadPoolExecutor
import functools
import gc
import os
import re
import sys
import threading
import time
from urllib.parse import quote

import asr_corrections
import patch_registry
import runtime_events
import vocal_cue_router
import voiceprint_router
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




_MODELSCOPE_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MODELSCOPE_REVISION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_MODELSCOPE_DIRECT_BASE = "https://modelscope.cn/models"


def _parse_model_file_download_args(args, kwargs) -> dict[str, object] | None:
    """Parse standard modelscope.hub.file_download.model_file_download arguments."""
    arg_names = (
        "model_id",
        "file_path",
        "revision",
        "cache_dir",
        "user_agent",
        "local_files_only",
        "cookies",
        "local_dir",
    )
    bound: dict[str, object] = {}
    if len(args) > len(arg_names):
        return None
    for name, val in zip(arg_names, args):
        bound[name] = val
    for k, v in kwargs.items():
        if k in bound:
            return None
        bound[k] = v
    if not bound.get("model_id") or not bound.get("file_path"):
        return None
    return bound


def _try_direct_modelscope_download(*args, **kwargs) -> str | None:
    """Download public ModelScope file directly via lightweight streaming HTTP (avoids ~330MB PyTorch/modelscope import spike)."""
    bound = _parse_model_file_download_args(args, kwargs)
    if not bound:
        return None
    if bound.get("local_files_only") or bound.get("cookies"):
        return None

    model_id = str(bound.get("model_id") or "").strip()
    raw_rel = str(bound.get("file_path") or "").replace("\\", "/").strip()
    revision = str(bound.get("revision") or "master").strip() or "master"
    local_dir = bound.get("local_dir")
    cache_dir = bound.get("cache_dir")

    if not _MODELSCOPE_MODEL_ID_RE.match(model_id):
        return None
    if not _MODELSCOPE_REVISION_RE.match(revision):
        return None
    if not raw_rel or raw_rel.startswith("/") or ".." in raw_rel.split("/"):
        return None

    if local_dir:
        base_dir = os.path.abspath(str(local_dir))
        dest_path = os.path.abspath(os.path.join(base_dir, *raw_rel.split("/")))
    elif cache_dir:
        base_dir = os.path.abspath(os.path.join(str(cache_dir), *model_id.split("/")))
        dest_path = os.path.abspath(os.path.join(base_dir, *raw_rel.split("/")))
    else:
        return None

    try:
        if os.path.commonpath([base_dir, dest_path]) != base_dir:
            return None
    except ValueError:
        return None

    if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path

    url = (
        f"{_MODELSCOPE_DIRECT_BASE}/"
        f"{quote(model_id, safe='/')}/resolve/"
        f"{quote(revision, safe='')}/{quote(raw_rel, safe='/')}"
    )
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp_path = f"{dest_path}.part.{os.getpid()}.{threading.get_ident()}"
    print(f"[MemOpt] 首次启动轻量直连下载离线模型 {model_id}/{raw_rel}（跳过重型 modelscope/PyTorch 加载）...", flush=True)
    try:
        import httpx

        timeout = httpx.Timeout(connect=20.0, read=60.0, write=20.0, pool=20.0)
        headers = {"User-Agent": "xiaozhi-server/1.0", "Accept-Encoding": "identity"}

        def _download_once(use_env_proxy: bool) -> int:
            written_bytes = 0
            expected_len = -1
            last_log_mb = 0
            with httpx.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=timeout,
                headers=headers,
                trust_env=use_env_proxy,
            ) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                has_content_encoding = bool((resp.headers.get("content-encoding") or "").strip())
                cl_hdr = resp.headers.get("content-length")
                if not has_content_encoding and cl_hdr and cl_hdr.isdigit():
                    expected_len = int(cl_hdr)
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=262144):
                        if not chunk:
                            continue
                        f.write(chunk)
                        written_bytes += len(chunk)
                        cur_mb = written_bytes // (32 * 1024 * 1024)
                        if cur_mb > last_log_mb:
                            last_log_mb = cur_mb
                            if expected_len > 0:
                                pct = min(100, int(written_bytes * 100 / expected_len))
                                print(
                                    f"[MemOpt] 下载进度 {raw_rel}: {written_bytes / (1024 * 1024):.1f} / {expected_len / (1024 * 1024):.1f} MiB ({pct}%)",
                                    flush=True,
                                )
                            else:
                                print(f"[MemOpt] 下载进度 {raw_rel}: {written_bytes / (1024 * 1024):.1f} MiB", flush=True)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
            if written_bytes <= 0 or (expected_len > 0 and written_bytes != expected_len):
                raise RuntimeError(f"下载体积不完整 (written={written_bytes}, expected={expected_len})")
            return written_bytes

        # Prefer direct domestic connection to modelscope.cn (immune to missing socksio when ALL_PROXY=socks5://...)
        try:
            written = _download_once(use_env_proxy=False)
        except (httpx.HTTPError, OSError):
            has_proxy_env = any(
                (os.environ.get(k) or "").strip()
                for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
            )
            if not has_proxy_env:
                raise
            written = _download_once(use_env_proxy=True)

        os.replace(tmp_path, dest_path)
        print(f"[MemOpt] 离线模型下载完成: {raw_rel} ({written / (1024 * 1024):.1f} MiB)", flush=True)
        gc.collect()
        return dest_path
    except Exception as exc:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        print(f"[MemOpt] 轻量直连下载 {raw_rel} 失败，回退原生 modelscope: {exc}", flush=True)
        return None


# 6. Memory optimization for Sherpa-ONNX local ASR + dedicated ASR executor (G-2) + ASR corrections (G-4) + idle trim (G-1)
def _install_memory_optimizations(*, start_trimmer: bool = True):
    global _idle_memory_trimmer, _atexit_registered
    if not _atexit_registered:
        atexit.register(_shutdown_asr_executor)
        _atexit_registered = True
    import types

    # 6a. Unconditionally lazy-stub modelscope.hub.file_download before importing sherpa_onnx_local.
    #     Eagerly importing modelscope drags in PyTorch/scipy/datasets (~330MB RSS) even on low-spec
    #     cloud-ASR PCs that never download local Sherpa models. If local download or another
    #     modelscope attribute is later accessed, the stub transparently loads real modelscope.
    if "modelscope" not in sys.modules:
        _ms_pkg = types.ModuleType("modelscope")
        _ms_pkg.__path__ = []
        _ms_hub = types.ModuleType("modelscope.hub")
        _ms_hub.__path__ = []
        _ms_dl = types.ModuleType("modelscope.hub.file_download")

        def _restore_real_modelscope():
            for mod_name in ("modelscope.hub.file_download", "modelscope.hub", "modelscope"):
                if sys.modules.get(mod_name) in (_ms_dl, _ms_hub, _ms_pkg):
                    sys.modules.pop(mod_name, None)

        def _lazy_model_file_download(*args, **kwargs):
            direct_path = _try_direct_modelscope_download(*args, **kwargs)
            if direct_path:
                return direct_path
            _restore_real_modelscope()
            from modelscope.hub.file_download import model_file_download as _real_download
            return _real_download(*args, **kwargs)

        def _ms_pkg_getattr(name: str):
            if name.startswith("__"):
                raise AttributeError(name)
            _restore_real_modelscope()
            import importlib
            real_pkg = importlib.import_module("modelscope")
            return getattr(real_pkg, name)

        def _ms_hub_getattr(name: str):
            if name.startswith("__"):
                raise AttributeError(name)
            _restore_real_modelscope()
            import importlib
            real_hub = importlib.import_module("modelscope.hub")
            return getattr(real_hub, name)

        _ms_dl.model_file_download = _lazy_model_file_download
        _ms_hub.file_download = _ms_dl
        _ms_hub.__getattr__ = _ms_hub_getattr
        _ms_pkg.hub = _ms_hub
        _ms_pkg.__getattr__ = _ms_pkg_getattr
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

        def _sherpa_requires_file(self) -> bool:
            return not bool(getattr(self, "delete_audio_file", True))

        def _wrap_speech_to_text(_orig_stt):
            @functools.wraps(_orig_stt)
            async def _async_speech_to_text(self, opus_data, session_id, artifacts=None, *args, **kwargs):
                file_path = None
                try:
                    if artifacts is None:
                        return "", None
                    file_path = getattr(artifacts, "file_path", None)
                    pcm_bytes = getattr(artifacts, "pcm_bytes", None)
                    use_mem_pcm = bool(
                        isinstance(pcm_bytes, (bytes, bytearray))
                        and len(pcm_bytes) >= 2
                        and (getattr(self, "delete_audio_file", True) or not file_path)
                    )
                    if not use_mem_pcm and not file_path:
                        return "", None
                    t_asr_start = time.monotonic()

                    def _decode():
                        s = self.model.create_stream()
                        raw_pcm_for_vp: bytes = b""
                        if use_mem_pcm:
                            import numpy as np

                            raw_pcm = bytes(pcm_bytes) if not isinstance(pcm_bytes, bytes) else pcm_bytes
                            if len(raw_pcm) % 2 != 0:
                                raw_pcm = raw_pcm[:-1]
                            raw_pcm_for_vp = raw_pcm
                            samples = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) / 32768.0
                            s.accept_waveform(16000, samples)
                        else:
                            samples, sample_rate = self.read_wave(file_path)
                            s.accept_waveform(sample_rate, samples)
                            if voiceprint_router.is_voiceprint_enabled() and sample_rate == 16000:
                                import numpy as np

                                raw_pcm_for_vp = (
                                    np.clip(np.round(np.asarray(samples, dtype=np.float32) * 32768.0), -32768, 32767)
                                    .astype(np.int16)
                                    .tobytes()
                                )
                        self.model.decode_stream(s)
                        raw_text = str(getattr(s.result, "text", "") or "")
                        cue = (
                            vocal_cue_router.extract_vocal_cue(raw_text, result=s.result)
                            if vocal_cue_router.is_vocal_cue_enabled()
                            else None
                        )
                        if raw_pcm_for_vp and voiceprint_router.is_voiceprint_enabled():
                            try:
                                clean_hint = _TAG_RE.sub("", raw_text).strip()
                                voiceprint_router.identify_turn_pcm(
                                    str(session_id or ""),
                                    raw_pcm_for_vp,
                                    transcript=clean_hint,
                                )
                            except Exception:
                                pass
                        return raw_text, cue

                    loop = asyncio.get_running_loop()
                    raw_text, cue = await loop.run_in_executor(get_asr_executor(), _decode)
                    asr_sec = time.monotonic() - t_asr_start
                    text = raw_text
                    if text:
                        text = _TAG_RE.sub("", text).strip()
                    if text:
                        text = asr_corrections.apply_asr_corrections(text)
                    try:
                        if text:
                            vocal_cue_router.record_vocal_cue(str(session_id or ""), cue, source="sherpa")
                        else:
                            vocal_cue_router.clear_vocal_cue(str(session_id or ""))
                    except Exception:
                        pass
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

        _ok_sh, _ = patch_registry.install_patch(
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
        if _ok_sh:
            _sherpa_mod.ASRProvider.requires_file = _sherpa_requires_file
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
            _BASE_TAG_RE = re.compile(r"<\|[^|]+\|>")

            @functools.wraps(_orig_base_stt_wrapper)
            async def _asr_corrections_stt_wrapper(self, pcm_data, session_id, *args, **kwargs):
                res = await _orig_base_stt_wrapper(self, pcm_data, session_id, *args, **kwargs)
                if not isinstance(res, tuple) or len(res) < 2:
                    return res
                text, file_path = res[0], res[1]
                # Patched SherpaASR / OpenaiASR already applied corrections inside speech_to_text(); avoid double-applying.
                already_corrected = bool(
                    isinstance(text, str)
                    and (
                        getattr(type(self), "_sherpa_async_patched", False)
                        or getattr(type(self), "_cloud_asr_self_corrected", False)
                    )
                )
                if not already_corrected:
                    raw_for_cue = ""
                    if isinstance(text, str) and text:
                        raw_for_cue = text
                        text = _BASE_TAG_RE.sub("", text).strip()
                        if text:
                            text = asr_corrections.apply_asr_corrections(text)
                    elif isinstance(text, dict) and isinstance(text.get("content"), str) and text["content"]:
                        text = dict(text)
                        raw_for_cue = text["content"]
                        cleaned_c = _BASE_TAG_RE.sub("", raw_for_cue).strip()
                        text["content"] = asr_corrections.apply_asr_corrections(cleaned_c) if cleaned_c else ""
                    if vocal_cue_router.is_vocal_cue_enabled():
                        try:
                            final_txt = text if isinstance(text, str) else (text.get("content", "") if isinstance(text, dict) else "")
                            if final_txt:
                                cue = vocal_cue_router.extract_vocal_cue(raw_for_cue) if "<|" in raw_for_cue else None
                                vocal_cue_router.record_vocal_cue(str(session_id or ""), cue, source="asr_inline")
                            else:
                                vocal_cue_router.clear_vocal_cue(str(session_id or ""))
                        except Exception:
                            pass
                if (
                    not getattr(type(self), "_sherpa_async_patched", False)
                    and voiceprint_router.is_voiceprint_enabled()
                ):
                    try:
                        pcm_joined = b"".join(b for b in (pcm_data or ()) if isinstance(b, (bytes, bytearray)))
                        if pcm_joined:
                            final_hint = (
                                text
                                if isinstance(text, str)
                                else (text.get("content", "") if isinstance(text, dict) else "")
                            )
                            loop = asyncio.get_running_loop()
                            await loop.run_in_executor(
                                get_asr_executor(),
                                lambda: voiceprint_router.identify_turn_pcm(
                                    str(session_id or ""),
                                    pcm_joined,
                                    transcript=str(final_hint or ""),
                                ),
                            )
                    except Exception:
                        pass
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

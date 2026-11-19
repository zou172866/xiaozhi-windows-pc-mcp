import os
import re
import sys
import runpy
import threading

ROOT_DIR = "/opt/xiaozhi-esp32-server"
DATA_DIR = os.path.join(ROOT_DIR, "data")
for p in (ROOT_DIR, DATA_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(ROOT_DIR)

# 1. Expand ${ENV_VAR} in memory so API keys never sit in plaintext on disk
import config.config_loader as config_loader

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_orig_read_config = config_loader.read_config


def _expand_env(obj):
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str) and "${" in obj:
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    return obj


def _patched_read_config(config_path):
    data = _orig_read_config(config_path)
    return _expand_env(data)


config_loader.read_config = _patched_read_config

# 2. Register custom Anima Brain memory provider without modifying container image
import core.utils.memory as memory_factory
import anima_memory_provider

_orig_create_memory = memory_factory.create_instance


def _patched_create_memory(class_name, *args, **kwargs):
    if class_name == "anima_brain":
        return anima_memory_provider.MemoryProvider(*args, **kwargs)
    return _orig_create_memory(class_name, *args, **kwargs)


memory_factory.create_instance = _patched_create_memory

# 3. Real-time per-turn async sync to Anima Brain + guaranteed <memory> injection
import core.utils.dialogue as dialogue_mod

_orig_put = dialogue_mod.Dialogue.put
_orig_get_llm_dialogue_with_memory = dialogue_mod.Dialogue.get_llm_dialogue_with_memory
_debounce_timers = {}
_timers_lock = threading.Lock()


def _patched_put(self, message):
    _orig_put(self, message)
    try:
        if getattr(message, "is_temporary", False):
            return
        if getattr(message, "role", "") != "assistant":
            return
        content = (getattr(message, "content", None) or "").strip()
        if not content:
            return
        pairs = anima_memory_provider.extract_clean_pairs(self.dialogue)
        if not pairs:
            return
        latest_pair = pairs[-1]
        req_id = latest_pair["request_id"]

        def _flush(pair=latest_pair, key=req_id):
            with _timers_lock:
                _debounce_timers.pop(key, None)
            anima_memory_provider.push_turns_async([pair])

        with _timers_lock:
            old_timer = _debounce_timers.pop(req_id, None)
            if old_timer is not None:
                old_timer.cancel()
            t = threading.Timer(1.0, _flush)
            t.daemon = True
            _debounce_timers[req_id] = t
            t.start()
    except Exception:
        pass


def _patched_get_llm_dialogue_with_memory(
    self, memory_str=None, voiceprint_config=None, current_speaker=None
):
    msgs = _orig_get_llm_dialogue_with_memory(
        self, memory_str, voiceprint_config, current_speaker
    )
    if memory_str and msgs and msgs[0].get("role") == "system":
        if memory_str not in msgs[0].get("content", ""):
            msgs[0]["content"] += f"\n\n<memory>\n{memory_str}\n</memory>"
    return msgs


dialogue_mod.Dialogue.put = _patched_put
dialogue_mod.Dialogue.get_llm_dialogue_with_memory = _patched_get_llm_dialogue_with_memory

# 4. Replace expired QWeather 401 demo key with keyless China National Meteorological Center (nmc.cn) weather
try:
    import nmc_weather_patch
    nmc_weather_patch.apply_nmc_weather_patch()
except Exception as _w_exc:
    print(f"[NMCWeather] 补丁加载异常: {_w_exc}", flush=True)

# 5. Mount PC speaker 48kHz audio bridge + Raiden Mei multi-emotion dynamic reference routing
try:
    import pc_audio_router
    pc_audio_router.apply_pc_audio_to_tts_patch()
except Exception as _pa1_exc:
    print(f"[PCAudioRouter] TTS 拦截层加载异常: {_pa1_exc}", flush=True)

try:
    import mei_emotion_router
    mei_emotion_router.apply_xiaozhi_emotion_and_prosody_patches()
except Exception as _m_exc:
    print(f"[MeiEmotionRouter] 补丁加载异常: {_m_exc}", flush=True)

try:
    import pc_audio_router
    pc_audio_router.apply_pc_audio_playback_patches()
except Exception as _pa2_exc:
    print(f"[PCAudioRouter] 播放同步层加载异常: {_pa2_exc}", flush=True)


# 6. Memory optimization for Sherpa-ONNX local ASR + periodic glibc heap trim
def _install_memory_optimizations():
    import asyncio
    import ctypes
    import gc
    import time
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

        # 6b. Offload Sherpa-ONNX CPU decode_stream to worker thread so asyncio loop stays responsive
        try:
            import core.providers.asr.sherpa_onnx_local as _sherpa_mod

            _TAG_RE = re.compile(r"<\|[^|]+\|>")

            async def _async_speech_to_text(self, opus_data, session_id, artifacts=None):
                file_path = None
                try:
                    if artifacts is None:
                        return "", None
                    file_path = artifacts.file_path
                    start_time = time.time()

                    def _decode():
                        s = self.model.create_stream()
                        samples, sample_rate = self.read_wave(file_path)
                        s.accept_waveform(sample_rate, samples)
                        self.model.decode_stream(s)
                        return s.result.text

                    text = await asyncio.to_thread(_decode)
                    if text:
                        text = _TAG_RE.sub("", text).strip()
                    _sherpa_mod.logger.bind(tag=_sherpa_mod.TAG).debug(
                        f"语音识别耗时: {time.time() - start_time:.3f}s | 结果: {text}"
                    )
                    return text, file_path
                except Exception as exc:
                    _sherpa_mod.logger.bind(tag=_sherpa_mod.TAG).error(
                        f"语音识别失败: {exc}", exc_info=True
                    )
                    return "", file_path

            _sherpa_mod.ASRProvider.speech_to_text = _async_speech_to_text
        except Exception as _s_exc:
            print(f"[MemOpt] SherpaASR 异步补丁跳过: {_s_exc}", flush=True)

    # 6c. Periodic glibc malloc_trim(0) so transient Python/ONNX heap returns to Linux/WSL2
    try:
        _libc = ctypes.CDLL("libc.so.6")
        _malloc_trim = _libc.malloc_trim
        _malloc_trim.argtypes = [ctypes.c_size_t]
        _malloc_trim.restype = ctypes.c_int

        def _trim_loop():
            time.sleep(8)
            while True:
                try:
                    gc.collect()
                    _malloc_trim(0)
                except Exception:
                    pass
                time.sleep(180)

        threading.Thread(target=_trim_loop, name="glibc-malloc-trim", daemon=True).start()
    except Exception:
        pass


try:
    _install_memory_optimizations()
except Exception as _opt_exc:
    print(f"[MemOpt] 初始化异常: {_opt_exc}", flush=True)

if __name__ == "__main__":
    try:
        import console_server
        console_server.start_console_background()
    except Exception as exc:
        print(f"[WebConsole] 启动异常: {exc}", flush=True)
    runpy.run_path(os.path.join(ROOT_DIR, "app.py"), run_name="__main__")

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

if __name__ == "__main__":
    try:
        import console_server
        console_server.start_console_background()
    except Exception as exc:
        print(f"[WebConsole] 启动异常: {exc}", flush=True)
    runpy.run_path(os.path.join(ROOT_DIR, "app.py"), run_name="__main__")

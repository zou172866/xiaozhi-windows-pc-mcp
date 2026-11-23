"""Idempotent source patcher for host Windows GPT-SoVITS (`19880`) integration.

Applies XiaoZhi's host GPT-SoVITS enhancements cleanly onto an upstream
`RVC-Boss/GPT-SoVITS` checkout (both classic v2/v4 and V5-Turbo branches):
  1. Optional `jieba_fast` fallback to pure-Python `jieba` in `chinese.py`,
     `chinese2.py`, and `tone_sandhi.py` (eliminates MSVC C++ build requirement
     on Windows) + optional `.venv` `jieba_fast` forwarding shim.
  2. `api_v2.py`: `--log_file` / `--log_max_bytes` / `--log_backup_count`
     in-process `RotatingFileHandler` (`5 MB x 3`, H-6), `GET/POST /prime`
     multi-emotion warmup endpoint, `GSV_IDLE_TRIM_*` memory-pressure working-set
     trimmer (F-3), and HTTP 500 rejection of `16kHz` all-zero failure frames.
  3. `GPT_SoVITS/TTS_infer_pack/TTS.py`: isolated `try/except` around `t2s_accel`
     (`torch_static_cuda_graph` / FlashAttention) so CPU/GPU OOM during CUDA Graph
     capture automatically disables CUDA Graph and falls back in-place to
     `infer_panel` (producing normal 48kHz audio instead of 1.0s 16kHz zeros),
     plus `prime_prompt_cache` / `touch_resident_cache` methods if absent.
  4. `GPT_SoVITS/text/g2pw/onnx_api.py`: default `CPUExecutionProvider` unless
     `GSV_G2PW_CUDA=1` (avoids ~1.6 GB commit spike on CUDA 12/13 ORT mismatch).

Usage:
  python gsv-overlay/apply_xiaozhi_gsv_patch.py --gsv-dir ../gpt-sovits-studio
  python gsv-overlay/apply_xiaozhi_gsv_patch.py --gsv-dir ../gpt-sovits-studio --install-venv-shims
  python gsv-overlay/apply_xiaozhi_gsv_patch.py --gsv-dir ../gpt-sovits-studio --check
"""

from __future__ import annotations

import argparse
from pathlib import Path
import py_compile
import re
import sys

_ROTATING_LOG_BLOCK = '''import asyncio
import atexit
import ctypes
import gc
import logging
from logging.handlers import RotatingFileHandler
import time

_GSV_DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
_GSV_DEFAULT_LOG_BACKUP_COUNT = 3


class _RotatingStreamWriter:
    """Thread-safe stdout/stderr wrapper backed by a single RotatingFileHandler (H-6)."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, handler: RotatingFileHandler):
        self._handler = handler
        self._lock = threading.Lock()
        self._buf = ""

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def _emit_line(self, line: str) -> None:
        try:
            rec = logging.LogRecord(
                name="gsv.api",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=line,
                args=(),
                exc_info=None,
            )
            self._handler.emit(rec)
        except Exception:
            pass

    def write(self, data: str) -> int:
        if not data:
            return 0
        text = data if isinstance(data, str) else str(data)
        with self._lock:
            self._buf += text.replace("\\r\\n", "\\n").replace("\\r", "\\n")
            while "\\n" in self._buf:
                line, self._buf = self._buf.split("\\n", 1)
                self._emit_line(line)
        return len(text)

    def writelines(self, lines) -> None:
        for ln in lines:
            self.write(ln)

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                self._emit_line(self._buf)
                self._buf = ""
            try:
                self._handler.flush()
            except Exception:
                pass

    def close(self) -> None:
        self.flush()


def _setup_gsv_rotating_log(
    log_file: str,
    *,
    max_bytes: int = _GSV_DEFAULT_LOG_MAX_BYTES,
    backup_count: int = _GSV_DEFAULT_LOG_BACKUP_COUNT,
):
    target = (log_file or "").strip()
    if not target:
        return None
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    handler = RotatingFileHandler(
        target,
        maxBytes=max(1024, int(max_bytes)),
        backupCount=max(1, int(backup_count)),
        encoding="utf-8",
        delay=False,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    old_out, old_err = sys.stdout, sys.stderr
    writer = _RotatingStreamWriter(handler)
    sys.stdout = writer
    sys.stderr = writer
    atexit.register(writer.flush)
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) in (old_out, old_err):
            try:
                h.setStream(writer)
            except Exception:
                pass
    return handler
'''

_ARGPARSE_LOG_BLOCK = '''parser.add_argument("--log_file", type=str, default=os.environ.get("GSV_LOG_FILE", ""), help="optional rotating log file")
parser.add_argument(
    "--log_max_bytes",
    type=int,
    default=int(os.environ.get("GSV_LOG_MAX_BYTES", str(_GSV_DEFAULT_LOG_MAX_BYTES)) or _GSV_DEFAULT_LOG_MAX_BYTES),
    help="max bytes per rotating log file",
)
parser.add_argument(
    "--log_backup_count",
    type=int,
    default=int(os.environ.get("GSV_LOG_BACKUP_COUNT", str(_GSV_DEFAULT_LOG_BACKUP_COUNT)) or _GSV_DEFAULT_LOG_BACKUP_COUNT),
    help="number of rotated log backups to keep",
)
args = parser.parse_args()
_gsv_log_handler = _setup_gsv_rotating_log(
    args.log_file,
    max_bytes=args.log_max_bytes,
    backup_count=args.log_backup_count,
)'''

_IDLE_TRIM_AND_PRIME_MODEL_BLOCK = '''class Prime_Request(BaseModel):
    items: list | None = None
    ref_audio_path: str | None = None
    prompt_text: str = ""
    prompt_lang: str = "zh"
    aux_ref_audio_paths: list | None = None
    skip_if_busy: bool = False
    reason: str = "prime"


_IDLE_TRIM_SECONDS = float(os.environ.get("GSV_IDLE_TRIM_SECONDS", "300") or 0)
_IDLE_TRIM_MEM_THRESHOLD = float(os.environ.get("GSV_IDLE_TRIM_MEM_THRESHOLD", "75") or 75)
_IDLE_TRIM_FORCE = os.environ.get("GSV_IDLE_TRIM_FORCE", "0").strip().lower() in ("1", "true", "yes")
_idle_trim_lock = threading.Lock()
_idle_trim_timer = None


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _get_memory_load_percent():
    if os.name != "nt":
        return None
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.dwMemoryLoad)
    except Exception:
        pass
    return None


def _should_trim_working_set():
    mem_load = _get_memory_load_percent()
    if _IDLE_TRIM_FORCE or _IDLE_TRIM_MEM_THRESHOLD <= 0:
        return True, mem_load
    if mem_load is None:
        return False, None
    return mem_load >= _IDLE_TRIM_MEM_THRESHOLD, mem_load


def _trim_process_memory(reason: str = "idle", *, respect_mem_pressure: bool = False):
    mem_load = _get_memory_load_percent()
    if respect_mem_pressure:
        should_trim, mem_load = _should_trim_working_set()
        if not should_trim:
            return False
    try:
        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
            psapi.EmptyWorkingSet.restype = ctypes.c_int
            psapi.EmptyWorkingSet(k32.GetCurrentProcess())
            print(f"[MemTrim] Trimmed cold working set ({reason}, mem_load={mem_load}%)", flush=True)
        except Exception:
            pass
    return True


def _get_or_init_tts_pipeline_lock():
    lock_obj = globals().get("_tts_pipeline_lock")
    if lock_obj is None:
        lock_obj = asyncio.Lock()
        globals()["_tts_pipeline_lock"] = lock_obj
    return lock_obj


def _cancel_idle_trim():
    global _idle_trim_timer
    with _idle_trim_lock:
        if _idle_trim_timer is not None:
            _idle_trim_timer.cancel()
            _idle_trim_timer = None


def _schedule_idle_trim():
    global _idle_trim_timer
    if _IDLE_TRIM_SECONDS <= 0:
        return

    def _on_idle():
        global _idle_trim_timer
        with _idle_trim_lock:
            _idle_trim_timer = None
        lock_obj = globals().get("_tts_pipeline_lock")
        if lock_obj is None or not lock_obj.locked():
            trimmed = _trim_process_memory("idle", respect_mem_pressure=True)
            if not trimmed and _IDLE_TRIM_SECONDS > 0:
                _schedule_idle_trim()

    with _idle_trim_lock:
        if _idle_trim_timer is not None:
            _idle_trim_timer.cancel()
        t = threading.Timer(_IDLE_TRIM_SECONDS, _on_idle)
        t.daemon = True
        _idle_trim_timer = t
        t.start()
'''

_PRIME_ROUTES_BLOCK = '''
@APP.get("/prime")
async def prime_get_endpoint(skip_if_busy: bool = True, reason: str = "wakeup"):
    return await _prime_handle(Prime_Request(skip_if_busy=skip_if_busy, reason=reason))


@APP.post("/prime")
async def prime_post_endpoint(request: Prime_Request):
    return await _prime_handle(request)


async def _prime_handle(req: Prime_Request):
    """Lightweight prompt-cache warmup & wakeup page-in endpoint without audio synthesis (F-3)."""
    t0 = time.perf_counter()
    lock_obj = _get_or_init_tts_pipeline_lock()
    if req.skip_if_busy and lock_obj.locked():
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "skipped": "busy",
                "reason": req.reason,
                "cache_slots": len(getattr(tts_pipeline, "_prompt_cache_slots", {})),
            },
        )

    await lock_obj.acquire()
    _cancel_idle_trim()
    try:
        items = list(req.items) if isinstance(req.items, list) else []
        if not items and req.ref_audio_path:
            items.append(
                {
                    "ref_audio_path": req.ref_audio_path,
                    "prompt_text": req.prompt_text,
                    "prompt_lang": req.prompt_lang,
                    "aux_ref_audio_paths": req.aux_ref_audio_paths or [],
                }
            )

        primed = 0
        hits = 0
        errors = []
        prime_fn = getattr(tts_pipeline, "prime_prompt_cache", None)
        touch_fn = getattr(tts_pipeline, "touch_resident_cache", None)
        runner = globals().get("_run_in_threadpool_shielded")

        async def _call_worker(fn, *a):
            if callable(runner):
                return await runner(fn, *a)
            from starlette.concurrency import run_in_threadpool

            return await run_in_threadpool(fn, *a)

        for idx, it in enumerate(items):
            if not isinstance(it, dict) or not it.get("ref_audio_path"):
                continue
            try:
                if callable(prime_fn):
                    res = await _call_worker(
                        prime_fn,
                        str(it["ref_audio_path"]),
                        str(it.get("prompt_text") or ""),
                        str(it.get("prompt_lang") or "zh"),
                        it.get("aux_ref_audio_paths") or [],
                    )
                    if isinstance(res, dict) and res.get("cache_hit"):
                        hits += 1
                else:
                    await _call_worker(tts_pipeline.set_ref_audio, str(it["ref_audio_path"]))
                primed += 1
            except Exception as exc:
                errors.append(f"item[{idx}]: {exc}")

        touched = await _call_worker(touch_fn) if callable(touch_fn) else primed
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return JSONResponse(
            status_code=200,
            content={
                "ok": len(errors) == 0,
                "reason": req.reason,
                "primed": primed,
                "cache_hits": hits,
                "touched": touched,
                "cache_slots": len(getattr(tts_pipeline, "_prompt_cache_slots", {})) or (1 if primed else 0),
                "mem_load_pct": _get_memory_load_percent(),
                "elapsed_ms": elapsed_ms,
                "errors": errors,
            },
        )
    except Exception as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    finally:
        lock_obj.release()
        _schedule_idle_trim()
'''

_TTS_PRIME_METHODS_FALLBACK = '''
    @staticmethod
    def _clone_prompt_cache_slot(slot: dict) -> dict:
        cloned = dict(slot)
        cloned["refer_spec"] = list(slot.get("refer_spec") or [])
        cloned["aux_ref_audio_paths"] = list(slot.get("aux_ref_audio_paths") or [])
        return cloned

    def _clear_prompt_cache_slots(self) -> None:
        slots = getattr(self, "_prompt_cache_slots", None)
        if slots is not None:
            slots.clear()

    def _sync_or_load_prompt_cache_slot(
        self,
        ref_audio_path,
        aux_ref_audio_paths,
        prompt_text,
        prompt_lang,
        no_prompt_text: bool,
    ) -> bool:
        """8-slot LRU prompt cache shared by `/prime` and `TTS.run()` without in-place slot aliasing (F-3)."""
        slots = getattr(self, "_prompt_cache_slots", None)
        if slots is None:
            self._prompt_cache_slots = OrderedDict()
            slots = self._prompt_cache_slots

        aux_list = [p for p in (aux_ref_audio_paths or []) if p not in [None, ""]]
        norm_prompt = ""
        if not no_prompt_text and prompt_text is not None:
            norm_prompt = str(prompt_text).strip("\\n")
            _splits = globals().get("splits") or {"，", "。", "？", "！", ",", ".", "?", "!", "~", ":", "：", "—", "…"}
            if norm_prompt and norm_prompt[-1] not in _splits:
                norm_prompt += "。" if str(prompt_lang or "").lower() != "en" else "."

        eff_ref = ref_audio_path if ref_audio_path not in [None, ""] else self.prompt_cache.get("ref_audio_path")
        norm_ref = os.path.normcase(os.path.abspath(eff_ref)) if eff_ref else ""
        norm_aux = tuple(os.path.normcase(os.path.abspath(p)) for p in aux_list)
        slot_key = (
            norm_ref,
            norm_aux,
            norm_prompt,
            str(prompt_lang or ""),
            getattr(self.configs, "version", ""),
            str(getattr(self.configs, "vits_weights_path", "")),
            str(getattr(self.configs, "device", "")),
            bool(getattr(self.configs, "is_half", True)),
        )

        hit = bool(norm_ref and slot_key in slots)
        if hit:
            cached = slots[slot_key]
            if getattr(self, "is_v2pro", False) and (
                not cached.get("refer_spec") or cached["refer_spec"][0][1] is None
            ):
                hit = False
            else:
                slots.move_to_end(slot_key)
                self.prompt_cache = self._clone_prompt_cache_slot(cached)
                return True

        # Detach from any cached slot before mutating self.prompt_cache
        self.prompt_cache = self._clone_prompt_cache_slot(self.prompt_cache)
        if ref_audio_path not in [None, ""]:
            if not os.path.exists(ref_audio_path):
                raise ValueError(f"{ref_audio_path} not exists")
            self.prompt_cache = {
                "ref_audio_path": None,
                "prompt_semantic": None,
                "refer_spec": [],
                "prompt_text": None,
                "prompt_lang": None,
                "phones": None,
                "bert_features": None,
                "norm_text": None,
                "aux_ref_audio_paths": [],
            }
            self.set_ref_audio(ref_audio_path)
        elif getattr(self, "is_v2pro", False) and self.prompt_cache["refer_spec"][0][1] is None:
            if not eff_ref or not os.path.exists(eff_ref):
                raise ValueError(f"{eff_ref} not exists")
            self.set_ref_audio(eff_ref)

        paths = set(aux_list) & set(self.prompt_cache.get("aux_ref_audio_paths") or [])
        if not (len(list(paths)) == len(aux_list) == len(self.prompt_cache.get("aux_ref_audio_paths") or [])):
            self.prompt_cache["aux_ref_audio_paths"] = list(aux_list)
            self.prompt_cache["refer_spec"] = [self.prompt_cache["refer_spec"][0]]
            for path in aux_list:
                if os.path.exists(path):
                    self.prompt_cache["refer_spec"].append(self._get_ref_spec(path))

        if not no_prompt_text:
            if (
                self.prompt_cache.get("prompt_text") != norm_prompt
                or self.prompt_cache.get("prompt_lang") != prompt_lang
            ):
                phones, bert_features, norm_text = self.text_preprocessor.segment_and_extract_feature_for_text(
                    norm_prompt, prompt_lang, self.configs.version
                )
                self.prompt_cache["prompt_text"] = norm_prompt
                self.prompt_cache["prompt_lang"] = prompt_lang
                self.prompt_cache["phones"] = phones
                self.prompt_cache["bert_features"] = bert_features
                self.prompt_cache["norm_text"] = norm_text
        else:
            self.prompt_cache["prompt_text"] = None
            self.prompt_cache["prompt_lang"] = None
            self.prompt_cache["phones"] = None
            self.prompt_cache["bert_features"] = None
            self.prompt_cache["norm_text"] = None

        if norm_ref:
            slots[slot_key] = self._clone_prompt_cache_slot(self.prompt_cache)
            slots.move_to_end(slot_key)
            while len(slots) > 8:
                slots.popitem(last=False)
        return False

    @torch.no_grad()
    def prime_prompt_cache(
        self,
        ref_audio_path: str,
        prompt_text: str = "",
        prompt_lang: str = "zh",
        aux_ref_audio_paths=None,
    ) -> dict:
        """Warm reference audio features into 8-slot LRU without synthesizing output audio (F-3)."""
        if not ref_audio_path or not os.path.exists(ref_audio_path):
            raise ValueError(f"ref_audio_path not found: {ref_audio_path}")
        plang = str(prompt_lang or "zh").lower()
        no_pt = prompt_text in [None, ""]
        hit = self._sync_or_load_prompt_cache_slot(
            ref_audio_path,
            aux_ref_audio_paths or [],
            str(prompt_text or ""),
            plang,
            no_pt,
        )
        return {"cache_hit": bool(hit), "cache_slots": len(getattr(self, "_prompt_cache_slots", {}))}

    @torch.no_grad()
    def touch_resident_cache(self) -> int:
        touched = 0
        for slot in getattr(self, "_prompt_cache_slots", {}).values():
            sem = slot.get("prompt_semantic") if isinstance(slot, dict) else None
            if isinstance(sem, torch.Tensor) and sem.numel() > 0:
                _ = int(sem.reshape(-1)[0].item())
                touched += 1
        if "cuda" in str(self.configs.device) and torch.cuda.is_available():
            _ = float(torch.zeros(1, device=self.configs.device).item())
        return touched
'''


def _patch_jieba_fast_file(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "missing (skipped)"
    orig = path.read_text(encoding="utf-8")
    text = orig
    if "except ImportError:\n    import jieba" in text:
        return False, "already patched"

    old_block = (
        "import jieba_fast\n"
        "import logging\n\n"
        "jieba_fast.setLogLevel(logging.CRITICAL)\n"
        "import jieba_fast.posseg as psg"
    )
    new_block = (
        "import logging\n"
        "try:\n"
        "    import jieba_fast\n"
        "    import jieba_fast.posseg as psg\n"
        "except ImportError:\n"
        "    import jieba as jieba_fast\n"
        "    import jieba.posseg as psg\n\n"
        "jieba_fast.setLogLevel(logging.CRITICAL)"
    )
    if old_block in text:
        text = text.replace(old_block, new_block, 1)
    elif "import jieba_fast as jieba" in text:
        text = text.replace(
            "import jieba_fast as jieba",
            "try:\n    import jieba_fast as jieba\nexcept ImportError:\n    import jieba",
            1,
        )
    if text == orig:
        return False, "no matching anchor"
    return True, text


def _patch_onnx_api(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "missing (skipped)"
    orig = path.read_text(encoding="utf-8")
    if "GSV_G2PW_CUDA" in orig:
        return False, "already patched"
    target = 'providers=["CUDAExecutionProvider", "CPUExecutionProvider"]'
    if target not in orig:
        return False, "already CPU-first or custom"
    text = orig.replace(
        target,
        'providers=(["CUDAExecutionProvider", "CPUExecutionProvider"] '
        'if os.environ.get("GSV_G2PW_CUDA") == "1" else ["CPUExecutionProvider"])',
        1,
    )
    return True, text


def _patch_api_v2(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        raise FileNotFoundError(f"api_v2.py not found in {path.parent}")
    orig = path.read_text(encoding="utf-8")
    text = orig

    # 1. Ensure threading + rotating log support
    if "_setup_gsv_rotating_log" not in text:
        if "import threading\n" in text:
            text = text.replace("import threading\n", "import threading\n\n" + _ROTATING_LOG_BLOCK + "\n", 1)
        elif "from pydantic import BaseModel\n" in text:
            text = text.replace(
                "from pydantic import BaseModel\n",
                "from pydantic import BaseModel\nimport threading\n\n" + _ROTATING_LOG_BLOCK + "\n",
                1,
            )
        else:
            raise RuntimeError("api_v2.py: could not locate import anchor for rotating log setup")

    if '"--log_file"' not in text and "'--log_file'" not in text:
        if "args = parser.parse_args()" not in text:
            raise RuntimeError("api_v2.py: could not locate `args = parser.parse_args()`")
        text = text.replace("args = parser.parse_args()", _ARGPARSE_LOG_BLOCK, 1)

    # 2. Ensure TTS_Request supports use_cuda_graph
    if "use_cuda_graph:" not in text and "class TTS_Request(BaseModel):" in text:
        text = text.replace(
            "    parallel_infer: bool = True\n",
            "    parallel_infer: bool = True\n    use_cuda_graph: bool = True\n",
            1,
        )

    # 3. Ensure Prime_Request & idle trim helpers exist
    if "class Prime_Request(BaseModel):" not in text:
        anchor = "APP = FastAPI()"
        if anchor not in text:
            raise RuntimeError("api_v2.py: could not locate `APP = FastAPI()`")
        text = text.replace(anchor, anchor + "\n\n" + _IDLE_TRIM_AND_PRIME_MODEL_BLOCK, 1)

    # 4. Ensure /prime routes exist
    if '"/prime"' not in text and "'/prime'" not in text:
        route_anchor = '@APP.get("/set_refer_audio")'
        if route_anchor in text:
            text = text.replace(route_anchor, _PRIME_ROUTES_BLOCK + "\n\n" + route_anchor, 1)
        elif 'if __name__ == "__main__":' in text:
            text = text.replace('if __name__ == "__main__":', _PRIME_ROUTES_BLOCK + '\n\nif __name__ == "__main__":', 1)
        else:
            text = text + "\n" + _PRIME_ROUTES_BLOCK

    # 4b. Hook _cancel_idle_trim / _schedule_idle_trim into tts_handle and weight-switch routes (F-3)
    if "await _tts_pipeline_lock.acquire()\n    tts_generator = None" in text:
        text = text.replace(
            "    await _tts_pipeline_lock.acquire()\n    tts_generator = None",
            "    await _tts_pipeline_lock.acquire()\n    _cancel_idle_trim()\n    tts_generator = None",
            1,
        )
    if "                finally:\n                    _tts_pipeline_lock.release()\n\n            stream_cleanup_task" in text:
        text = text.replace(
            "                finally:\n                    _tts_pipeline_lock.release()\n\n            stream_cleanup_task",
            "                finally:\n                    _tts_pipeline_lock.release()\n                    _schedule_idle_trim()\n\n            stream_cleanup_task",
            1,
        )
    if "            finally:\n                _tts_pipeline_lock.release()\n\n\n@APP.get(\"/control\")" in text:
        text = text.replace(
            "            finally:\n                _tts_pipeline_lock.release()\n\n\n@APP.get(\"/control\")",
            "            finally:\n                _tts_pipeline_lock.release()\n                _schedule_idle_trim()\n\n\n@APP.get(\"/control\")",
            1,
        )
    for weight_fn, trim_tag in (("init_t2s_weights", "set_gpt_weights"), ("init_vits_weights", "set_sovits_weights")):
        raw_w = f"        async with _tts_pipeline_lock:\n            await _run_in_threadpool_shielded(tts_pipeline.{weight_fn}, weights_path)"
        patched_w = (
            "        async with _tts_pipeline_lock:\n"
            "            _cancel_idle_trim()\n"
            f"            await _run_in_threadpool_shielded(tts_pipeline.{weight_fn}, weights_path)\n"
            "            if _IDLE_TRIM_SECONDS > 0:\n"
            f'                await _run_in_threadpool_shielded(_trim_process_memory, "{trim_tag}", respect_mem_pressure=True)'
        )
        if raw_w in text:
            text = text.replace(raw_w, patched_w, 1)

    # Classic v2/v4 api_v2.py without _tts_pipeline_lock in tts_handle: serialize with _get_or_init_tts_pipeline_lock()
    if "_tts_pipeline_lock.acquire()" not in text and "async def tts_handle(req: dict):" in text and "_tts_lock_classic" not in text:
        if (
            "async def tts_handle(req: dict):\n    try:\n" in text
            and '        return JSONResponse(status_code=400, content={"message": "tts failed", "Exception": str(e)})' in text
        ):
            text = text.replace(
                "async def tts_handle(req: dict):\n    try:\n",
                "async def tts_handle(req: dict):\n"
                "    _tts_lock_classic = _get_or_init_tts_pipeline_lock()\n"
                "    await _tts_lock_classic.acquire()\n"
                "    _cancel_idle_trim()\n"
                "    try:\n",
                1,
            )

    # 5. Ensure _api_raise_on_error + 16kHz all-zero guard + HTTP 500 on runtime exceptions
    if '"_api_raise_on_error"' not in text:
        if 'req["fixed_length_chunk"] = fixed_length_chunk\n' in text:
            text = text.replace(
                'req["fixed_length_chunk"] = fixed_length_chunk\n',
                'req["fixed_length_chunk"] = fixed_length_chunk\n        req["_api_raise_on_error"] = True\n',
                1,
            )
    stream_chunk_anchor = "                        sr, chunk = item\n                        if first_audio and media_type == \"wav\":"
    stream_chunk_patched = (
        "                        sr, chunk = item\n"
        "                        if (\n"
        "                            first_audio\n"
        "                            and int(sr) == 16000\n"
        "                            and isinstance(chunk, np.ndarray)\n"
        "                            and 15000 <= int(chunk.size) <= 17000\n"
        "                            and not np.any(chunk)\n"
        "                        ):\n"
        '                            raise RuntimeError("TTS yielded 16kHz all-zero failure placeholder instead of valid audio")\n'
        '                        if first_audio and media_type == "wav":'
    )
    if stream_chunk_anchor in text and "first_audio\n                            and int(sr) == 16000" not in text:
        text = text.replace(stream_chunk_anchor, stream_chunk_patched, 1)

    if "TTS yielded 16kHz all-zero failure placeholder\n" not in text and "sr, audio_data = item\n        if (" not in text and "sr, audio_data = next(tts_generator)\n        if (" not in text:
        v5_nonstream = (
            "        sr, audio_data = item\n"
            "        packed = await _run_in_threadpool_shielded(pack_audio, BytesIO(), audio_data, sr, media_type)\n"
            '        return Response(packed.getvalue(), media_type=f"audio/{media_type}")\n'
            "    except Exception as e:\n"
            '        return JSONResponse(status_code=400, content={"message": "tts failed", "Exception": str(e)})'
        )
        v5_nonstream_patched = (
            "        sr, audio_data = item\n"
            "        if (\n"
            "            int(sr) == 16000\n"
            "            and isinstance(audio_data, np.ndarray)\n"
            "            and 15000 <= int(audio_data.size) <= 17000\n"
            "            and not np.any(audio_data)\n"
            "        ):\n"
            '            raise RuntimeError("TTS yielded 16kHz all-zero failure placeholder instead of valid audio")\n'
            "        packed = await _run_in_threadpool_shielded(pack_audio, BytesIO(), audio_data, sr, media_type)\n"
            '        return Response(packed.getvalue(), media_type=f"audio/{media_type}")\n'
            "    except Exception as e:\n"
            '        return JSONResponse(status_code=500, content={"message": "tts failed", "Exception": str(e)})'
        )
        classic_nonstream = (
            "        sr, audio_data = next(tts_generator)\n"
            "        audio_data = pack_audio(BytesIO(), audio_data, sr, media_type).getvalue()\n"
            '        return Response(audio_data, media_type=f"audio/{media_type}")\n'
            "    except Exception as e:\n"
            '        return JSONResponse(status_code=400, content={"message": "tts failed", "Exception": str(e)})'
        )
        classic_tail = (
            "\n    finally:\n        _tts_lock_classic.release()\n        _schedule_idle_trim()"
            if "_tts_lock_classic = _get_or_init_tts_pipeline_lock()" in text
            else ""
        )
        classic_nonstream_patched = (
            "        sr, audio_data = next(tts_generator)\n"
            "        if (\n"
            "            int(sr) == 16000\n"
            "            and isinstance(audio_data, np.ndarray)\n"
            "            and 15000 <= int(audio_data.size) <= 17000\n"
            "            and not np.any(audio_data)\n"
            "        ):\n"
            '            raise RuntimeError("TTS yielded 16kHz all-zero failure placeholder instead of valid audio")\n'
            "        audio_data = pack_audio(BytesIO(), audio_data, sr, media_type).getvalue()\n"
            '        return Response(audio_data, media_type=f"audio/{media_type}")\n'
            "    except Exception as e:\n"
            '        return JSONResponse(status_code=500, content={"message": "tts failed", "Exception": str(e)})'
            + classic_tail
        )
        if v5_nonstream in text:
            text = text.replace(v5_nonstream, v5_nonstream_patched, 1)
        elif classic_nonstream in text:
            text = text.replace(classic_nonstream, classic_nonstream_patched, 1)

    if text == orig:
        return False, "already patched"
    return True, text


def _patch_tts_py(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        raise FileNotFoundError(f"TTS.py not found at {path}")
    orig = path.read_text(encoding="utf-8")
    text = orig

    if "from collections import OrderedDict" not in text:
        text = "from collections import OrderedDict\n" + text

    if "_cuda_graph_disabled" not in text and "self.t2s_accel = None" in text:
        text = text.replace(
            "        self.t2s_model: Text2SemanticLightningModule = None\n        self.t2s_accel = None",
            "        self.t2s_model: Text2SemanticLightningModule = None\n"
            "        self.t2s_accel = None\n"
            "        self._cuda_graph_disabled: bool = False\n"
            "        self._accel_disabled: bool = False",
            1,
        )
        text = text.replace(
            "        if self.t2s_accel is not None:\n            self.t2s_accel.close()\n            self.t2s_accel = None",
            "        if self.t2s_accel is not None:\n"
            "            self.t2s_accel.close()\n"
            "            self.t2s_accel = None\n"
            "        self._cuda_graph_disabled = False\n"
            "        self._accel_disabled = False",
        )
    elif "_accel_disabled" not in text and "self._cuda_graph_disabled: bool = False" in text:
        text = text.replace(
            "        self._cuda_graph_disabled: bool = False",
            "        self._cuda_graph_disabled: bool = False\n        self._accel_disabled: bool = False",
            1,
        )
        text = text.replace(
            "        self._cuda_graph_disabled = False",
            "        self._cuda_graph_disabled = False\n        self._accel_disabled = False",
        )

    # Wrap V5-Turbo t2s_accel CUDA Graph path in isolated try/except -> infer_panel fallback
    unpatched_accel = '''                if not streaming_mode:
                    print(f"############ {i18n('预测语义Token')} ############")
                    accel_batch_size = batch_size if parallel_infer else 1
                    if self.t2s_accel is not None and self.t2s_accel.max_batch_size != accel_batch_size:
                        self.t2s_accel.close()
                        self.t2s_accel = None
                    if self.t2s_accel is None:
                        self.t2s_accel = create_acceleration(
                            self.configs.t2s_weights_path,
                            device=torch.device(self.configs.device),
                            dtype=self.precision,
                            max_batch_size=accel_batch_size,
                            use_cuda_graph=use_cuda_graph,
                            use_flash_attention=use_flash_attention,
                        )
                    use_accel = self.t2s_accel is not None and self.t2s_accel.prepare(
                        use_cuda_graph, use_flash_attention
                    )
                    if use_accel:
                        pred_semantic_list, idx_list = [], []
                        for start in range(0, len(all_phoneme_ids), accel_batch_size):
                            end = start + accel_batch_size
                            tokens, token_lengths = self.t2s_accel.infer_batch(
                                all_phoneme_ids[start:end],
                                all_phoneme_lens[start:end],
                                prompt[start:end] if prompt is not None else None,
                                all_bert_features[start:end],
                                parallel_infer=parallel_infer,
                                use_cuda_graph=use_cuda_graph,
                                use_flash_attention=use_flash_attention,
                                top_k=top_k,
                                top_p=top_p,
                                temperature=temperature,
                                early_stop_num=self.configs.hz * self.configs.max_sec,
                                repetition_penalty=repetition_penalty,
                            )
                            pred_semantic_list.extend(tokens)
                            idx_list.extend(token_lengths)
                    else:
                        pred_semantic_list, idx_list = self.t2s_model.model.infer_panel(
                            all_phoneme_ids,
                            all_phoneme_lens,
                            prompt,
                            all_bert_features,
                            top_k=top_k,
                            top_p=top_p,
                            temperature=temperature,
                            early_stop_num=self.configs.hz * self.configs.max_sec,
                            max_len=max_len,
                            repetition_penalty=repetition_penalty,
                        )'''

    patched_accel = '''                if not streaming_mode:
                    print(f"############ {i18n('预测语义Token')} ############")
                    accel_batch_size = batch_size if parallel_infer else 1
                    eff_cuda_graph = bool(use_cuda_graph and not getattr(self, "_cuda_graph_disabled", False))
                    pred_semantic_list, idx_list = None, None
                    if not getattr(self, "_accel_disabled", False) and (eff_cuda_graph or use_flash_attention):
                        _had_accel = self.t2s_accel is not None
                        try:
                            if self.t2s_accel is not None and self.t2s_accel.max_batch_size != accel_batch_size:
                                self.t2s_accel.close()
                                self.t2s_accel = None
                                _had_accel = False
                            if self.t2s_accel is None:
                                self.t2s_accel = create_acceleration(
                                    self.configs.t2s_weights_path,
                                    device=torch.device(self.configs.device),
                                    dtype=self.precision,
                                    max_batch_size=accel_batch_size,
                                    use_cuda_graph=eff_cuda_graph,
                                    use_flash_attention=use_flash_attention,
                                )
                                _had_accel = True
                            use_accel = self.t2s_accel is not None and self.t2s_accel.prepare(
                                eff_cuda_graph, use_flash_attention
                            )
                            if use_accel:
                                _sem_out, _idx_out = [], []
                                for start in range(0, len(all_phoneme_ids), accel_batch_size):
                                    end = start + accel_batch_size
                                    tokens, token_lengths = self.t2s_accel.infer_batch(
                                        all_phoneme_ids[start:end],
                                        all_phoneme_lens[start:end],
                                        prompt[start:end] if prompt is not None else None,
                                        all_bert_features[start:end],
                                        parallel_infer=parallel_infer,
                                        use_cuda_graph=eff_cuda_graph,
                                        use_flash_attention=use_flash_attention,
                                        top_k=top_k,
                                        top_p=top_p,
                                        temperature=temperature,
                                        early_stop_num=self.configs.hz * self.configs.max_sec,
                                        repetition_penalty=repetition_penalty,
                                    )
                                    _sem_out.extend(tokens)
                                    _idx_out.extend(token_lengths)
                                pred_semantic_list, idx_list = _sem_out, _idx_out
                        except Exception as accel_exc:
                            print(
                                f"[TTS] AR acceleration / CUDA Graph failed ({accel_exc}); "
                                "disabling acceleration/CUDA Graph and falling back to infer_panel.",
                                flush=True,
                            )
                            if not _had_accel or not eff_cuda_graph:
                                self._accel_disabled = True
                            self._cuda_graph_disabled = True
                            if self.t2s_accel is not None:
                                try:
                                    self.t2s_accel.close()
                                except Exception:
                                    pass
                                self.t2s_accel = None
                            self.empty_cache()
                            pred_semantic_list, idx_list = None, None
                    if pred_semantic_list is None:
                        pred_semantic_list, idx_list = self.t2s_model.model.infer_panel(
                            all_phoneme_ids,
                            all_phoneme_lens,
                            prompt,
                            all_bert_features,
                            top_k=top_k,
                            top_p=top_p,
                            temperature=temperature,
                            early_stop_num=self.configs.hz * self.configs.max_sec,
                            max_len=max_len,
                            repetition_penalty=repetition_penalty,
                        )'''

    if unpatched_accel in text:
        text = text.replace(unpatched_accel, patched_accel, 1)

    # Wire 8-slot LRU prompt cache into upstream TTS.run() if not already using prompt_slot_key / _sync_or_load_prompt_cache_slot
    upstream_prompt_block = '''        if (ref_audio_path is not None) and (
            ref_audio_path != self.prompt_cache["ref_audio_path"]
            or (self.is_v2pro and self.prompt_cache["refer_spec"][0][1] is None)
        ):
            if not os.path.exists(ref_audio_path):
                raise ValueError(f"{ref_audio_path} not exists")
            self.set_ref_audio(ref_audio_path)

        aux_ref_audio_paths = aux_ref_audio_paths if aux_ref_audio_paths is not None else []
        paths = set(aux_ref_audio_paths) & set(self.prompt_cache["aux_ref_audio_paths"])
        if not (len(list(paths)) == len(aux_ref_audio_paths) == len(self.prompt_cache["aux_ref_audio_paths"])):
            self.prompt_cache["aux_ref_audio_paths"] = aux_ref_audio_paths
            self.prompt_cache["refer_spec"] = [self.prompt_cache["refer_spec"][0]]
            for path in aux_ref_audio_paths:
                if path in [None, ""]:
                    continue
                if not os.path.exists(path):
                    print(i18n("音频文件不存在，跳过："), path)
                    continue
                self.prompt_cache["refer_spec"].append(self._get_ref_spec(path))

        if not no_prompt_text:
            prompt_text = prompt_text.strip("\\n")
            if prompt_text[-1] not in splits:
                prompt_text += "。" if prompt_lang != "en" else "."
            print(i18n("实际输入的参考文本:"), prompt_text)
            if self.prompt_cache["prompt_text"] != prompt_text:
                phones, bert_features, norm_text = self.text_preprocessor.segment_and_extract_feature_for_text(
                    prompt_text, prompt_lang, self.configs.version
                )
                self.prompt_cache["prompt_text"] = prompt_text
                self.prompt_cache["prompt_lang"] = prompt_lang
                self.prompt_cache["phones"] = phones
                self.prompt_cache["bert_features"] = bert_features
                self.prompt_cache["norm_text"] = norm_text'''

    if (
        "prompt_slot_key" not in text
        and "_sync_or_load_prompt_cache_slot(" not in text
        and upstream_prompt_block in text
    ):
        text = text.replace(
            upstream_prompt_block,
            "        self._sync_or_load_prompt_cache_slot(\n"
            "            ref_audio_path, aux_ref_audio_paths, prompt_text, prompt_lang, no_prompt_text\n"
            "        )\n"
            "        if not no_prompt_text and self.prompt_cache.get('prompt_text'):\n"
            "            prompt_text = self.prompt_cache['prompt_text']",
            1,
        )
        vits_save_anchor = "        self.vits_model = vits_model\n"
        if vits_save_anchor in text and "_clear_prompt_cache_slots" not in text:
            text = text.replace(
                vits_save_anchor,
                "        self.vits_model = vits_model\n        self._clear_prompt_cache_slots()\n",
                1,
            )

    # Guard outer exception `yield 16000, np.zeros(int(16000), dtype=np.int16)` behind _api_raise_on_error
    if "_api_raise_on_error" not in text:
        text = re.sub(
            r"(except Exception as e:\n\s+traceback\.print_exc\(\)\n(?:\s+#[^\n]*\n)?)"
            r"(\s+)yield 16000,\s*np\.zeros\(int\(16000\),\s*dtype=np\.int16\)",
            r'\1\2if not inputs.get("_api_raise_on_error"):\n\2    yield 16000, np.zeros(int(16000), dtype=np.int16)',
            text,
            count=1,
        )

    # Ensure prime_prompt_cache / touch_resident_cache exist on TTS
    if "def prime_prompt_cache(" not in text:
        anchor = "    def empty_cache(self):"
        if anchor in text:
            text = text.replace(anchor, _TTS_PRIME_METHODS_FALLBACK.lstrip("\n") + "\n" + anchor, 1)

    if text == orig:
        return False, "already patched"
    return True, text


def install_jieba_fast_venv_shim(gsv_dir: Path, *, dry_run: bool = False) -> str:
    """Create a pure-Python `jieba_fast` forwarding shim in `.venv/Lib/site-packages` if C wheel is absent."""
    candidates = [
        gsv_dir / ".venv" / "Lib" / "site-packages",
        gsv_dir / "runtime" / "Lib" / "site-packages",
    ]
    site_pkgs = next((p for p in candidates if p.is_dir()), None)
    if site_pkgs is None:
        return "no .venv/Lib/site-packages found (skipped shim)"

    pkg_dir = site_pkgs / "jieba_fast"
    # Never overwrite a real compiled jieba_fast binary wheel if user has one
    if any(site_pkgs.glob("_jieba_fast_functions*.pyd")) or any(pkg_dir.glob("*.pyd")):
        return "native compiled jieba_fast wheel present (kept native)"

    init_code = (
        '"""Pure-Python jieba compatibility shim for Windows environments without MSVC Build Tools."""\n'
        "from jieba import *\n"  # noqa: F403
        "import jieba as _jieba\n\n"
        "def __getattr__(name: str):\n"
        "    return getattr(_jieba, name)\n"
    )
    posseg_code = (
        '"""Forward jieba_fast.posseg to jieba.posseg."""\n'
        "from jieba.posseg import *\n"  # noqa: F403
        "import jieba.posseg as _posseg\n\n"
        "def __getattr__(name: str):\n"
        "    return getattr(_posseg, name)\n"
    )
    init_file = pkg_dir / "__init__.py"
    posseg_file = pkg_dir / "posseg.py"
    if (
        init_file.is_file()
        and posseg_file.is_file()
        and init_file.read_text(encoding="utf-8") == init_code
        and posseg_file.read_text(encoding="utf-8") == posseg_code
    ):
        return f"shim already up to date ({pkg_dir})"
    if not dry_run:
        pkg_dir.mkdir(parents=True, exist_ok=True)
        init_file.write_text(init_code, encoding="utf-8")
        posseg_file.write_text(posseg_code, encoding="utf-8")
    return f"installed pure-Python jieba_fast shim ({pkg_dir})"


def apply_all_patches(
    gsv_dir: Path,
    *,
    check_only: bool = False,
    dry_run: bool = False,
    install_shims: bool = False,
) -> tuple[bool, list[str]]:
    """Apply or verify all XiaoZhi host GPT-SoVITS patches. Returns `(all_clean_or_ok, log_lines)`."""
    gsv_dir = gsv_dir.resolve()
    targets = [
        ("api_v2.py", gsv_dir / "api_v2.py", _patch_api_v2),
        ("GPT_SoVITS/TTS_infer_pack/TTS.py", gsv_dir / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py", _patch_tts_py),
        ("GPT_SoVITS/text/chinese.py", gsv_dir / "GPT_SoVITS" / "text" / "chinese.py", _patch_jieba_fast_file),
        ("GPT_SoVITS/text/chinese2.py", gsv_dir / "GPT_SoVITS" / "text" / "chinese2.py", _patch_jieba_fast_file),
        ("GPT_SoVITS/text/tone_sandhi.py", gsv_dir / "GPT_SoVITS" / "text" / "tone_sandhi.py", _patch_jieba_fast_file),
        ("GPT_SoVITS/text/g2pw/onnx_api.py", gsv_dir / "GPT_SoVITS" / "text" / "g2pw" / "onnx_api.py", _patch_onnx_api),
    ]
    logs: list[str] = []
    pending_changes = 0

    for rel_name, fp, patch_fn in targets:
        changed, result = patch_fn(fp)
        if not changed:
            logs.append(f"[OK] {rel_name}: {result}")
            continue
        pending_changes += 1
        if check_only:
            logs.append(f"[NEEDS_PATCH] {rel_name}")
            continue
        if dry_run:
            compile(result, str(fp), "exec")
            logs.append(f"[DRY_RUN] {rel_name}: syntax verified")
            continue
        bak = fp.with_suffix(fp.suffix + ".orig")
        if not bak.exists():
            bak.write_bytes(fp.read_bytes())
        fp.write_text(result, encoding="utf-8", newline="\n")
        py_compile.compile(str(fp), doraise=True)
        logs.append(f"[PATCHED] {rel_name}")

    if install_shims and not check_only:
        shim_msg = install_jieba_fast_venv_shim(gsv_dir, dry_run=dry_run)
        logs.append(f"[SHIM] {shim_msg}")

    if check_only:
        return pending_changes == 0, logs
    return True, logs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply idempotent XiaoZhi patches to a host GPT-SoVITS directory")
    parser.add_argument(
        "--gsv-dir",
        default="",
        help="Path to host GPT-SoVITS repo (default: ../gpt-sovits-studio relative to xiaozhi-server)",
    )
    parser.add_argument("--check", action="store_true", help="Exit 0 if already patched, 1 if patches are needed")
    parser.add_argument("--dry-run", action="store_true", help="Verify patch generation & syntax without writing files")
    parser.add_argument(
        "--install-venv-shims",
        action="store_true",
        help="Also install pure-Python jieba_fast forwarding shim into <gsv-dir>/.venv/Lib/site-packages",
    )
    args = parser.parse_args(argv)

    server_dir = Path(__file__).resolve().parent.parent
    gsv_dir = Path(args.gsv_dir) if args.gsv_dir else (server_dir.parent / "gpt-sovits-studio")
    try:
        ok, lines = apply_all_patches(
            gsv_dir,
            check_only=args.check,
            dry_run=args.dry_run,
            install_shims=args.install_venv_shims,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    for ln in lines:
        print(ln)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

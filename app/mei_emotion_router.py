"""Dynamic multi-emotion voice routing & smooth clause segmentation for Raiden Mei (GPT-SoVITS v4).

Responsibilities:
1. Parse and strip hidden LLM emotion tags (e.g. [gentle], [happy], [playful], [shy], [serious],
   [tender], [calm], Chinese equivalents, or <emotion:...>) so tags are NEVER spoken aloud by TTS,
   NEVER shown raw in ESP32 subtitles or 18080 Web Console chat bubbles, and NEVER pollute Anima Brain.
2. Route each clause to genuine Honkai Impact 3rd Raiden Mei (CV: 菊花花) emotion reference audio
   + tuned prosody parameters (speed_factor, temperature, top_k, top_p) with 3-layer fallback:
   Explicit LLM tag -> Clause affective cue inference -> Anima Brain PAD last_emotion -> calm.
3. Replace choppy 2-3 character opening comma splits (e.g. "哥哥，") with prosody-aware clause
   segmentation while preserving expressive ending punctuation (! / ? / ~ / …) and avoiding
   double-splitting inside GPT-SoVITS (cut0).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import patch_registry
import runtime_events
import runtime_settings
import xz_common

_LEGACY_HOST_DATA_PREFIX = "e:/故事/1/xiaozhi-server/data"
_LEGACY_HOST_VOICES_PREFIX = "e:/故事/1/xiaozhi-server/voices"
CONTAINER_DATA_PREFIX = xz_common.DATA_DIR
CONTAINER_VOICES_PREFIX = xz_common.VOICES_DIR
_HOST_DIRS = xz_common.resolve_host_dirs()
HOST_DATA_PREFIX = _HOST_DIRS["HOST_DATA_DIR"]
_HOST_PROJECT_PREFIX = _HOST_DIRS["HOST_PROJECT_DIR"]
HOST_VOICES_PREFIX = _HOST_DIRS["HOST_VOICES_DIR"]
MEI_DIR_HOST = f"{HOST_VOICES_PREFIX}/raiden_mei"
GSV_PRIME_URL = os.environ.get("GSV_PRIME_URL", "http://host.docker.internal:19880/prime").strip()
_gsv_prime_lock = threading.Lock()
_gsv_last_prime_ts: float = 0.0

DEFAULT_MEI_EMOTIONS: Dict[str, Dict[str, Any]] = {
    "calm": {
        "label_zh": "平静默认",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_default_ref.wav",
        "prompt_text": "今夜月色很美，呵...适合与朋友小聚一番。",
        "prompt_lang": "zh",
        "speed_factor": 1.00,
        "temperature": 0.98,
        "top_k": 12,
        "top_p": 1.00,
        "esp32_emotion": "relaxed",
        "esp32_emoji": "😌",
    },
    "gentle": {
        "label_zh": "温柔关怀",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_gentle_ref.wav",
        "prompt_text": "你今天已经很努力了，接下来的事就交给我吧。",
        "prompt_lang": "zh",
        "speed_factor": 0.96,
        "temperature": 0.95,
        "top_k": 12,
        "top_p": 0.98,
        "esp32_emotion": "loving",
        "esp32_emoji": "😍",
    },
    "happy": {
        "label_zh": "欣喜明朗",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_happy_ref.wav",
        "prompt_text": "今天天气挺不错的，要和我们一起出去野餐吗？",
        "prompt_lang": "zh",
        "speed_factor": 1.04,
        "temperature": 1.04,
        "top_k": 15,
        "top_p": 1.00,
        "esp32_emotion": "happy",
        "esp32_emoji": "😆",
    },
    "playful": {
        "label_zh": "俏皮打趣",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_playful_ref.wav",
        "prompt_text": "如果你能按时起床，我倒不介意多做一份早餐哦。",
        "prompt_lang": "zh",
        "speed_factor": 1.03,
        "temperature": 1.05,
        "top_k": 15,
        "top_p": 1.00,
        "esp32_emotion": "winking",
        "esp32_emoji": "😉",
    },
    "shy": {
        "label_zh": "羞赧微嗔",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_shy_ref.wav",
        "prompt_text": "面具？虽然有些不好意思，但心理上它的确是我过去的一部分。",
        "prompt_lang": "zh",
        "speed_factor": 0.97,
        "temperature": 1.00,
        "top_k": 14,
        "top_p": 1.00,
        "esp32_emotion": "embarrassed",
        "esp32_emoji": "😳",
    },
    "serious": {
        "label_zh": "沉稳认真",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_serious_ref.wav",
        "prompt_text": "以律者的身份守护人类，这就是雷电芽衣的选择。",
        "prompt_lang": "zh",
        "speed_factor": 1.02,
        "temperature": 0.90,
        "top_k": 10,
        "top_p": 0.95,
        "esp32_emotion": "confident",
        "esp32_emoji": "😏",
    },
    "tender": {
        "label_zh": "轻柔抚慰",
        "ref_audio_path": f"{MEI_DIR_HOST}/mei_tender_ref.wav",
        "prompt_text": "那些美好温暖的回忆，会永远留在我的心中。",
        "prompt_lang": "zh",
        "speed_factor": 0.93,
        "temperature": 0.92,
        "top_k": 12,
        "top_p": 0.98,
        "esp32_emotion": "sad",
        "esp32_emoji": "😔",
    },
}

EMOTION_ALIASES: Dict[str, str] = {
    # calm
    "calm": "calm",
    "default": "calm",
    "neutral": "calm",
    "relaxed": "calm",
    "normal": "calm",
    "peaceful": "calm",
    "平静": "calm",
    "默认": "calm",
    "自然": "calm",
    "舒缓": "calm",
    "淡定": "calm",
    "平和": "calm",
    # gentle
    "gentle": "gentle",
    "warm": "gentle",
    "caring": "gentle",
    "soft": "gentle",
    "loving": "gentle",
    "affectionate": "gentle",
    "kind": "gentle",
    "sleepy": "gentle",
    "温柔": "gentle",
    "体贴": "gentle",
    "关怀": "gentle",
    "暖心": "gentle",
    "柔和": "gentle",
    "宠溺": "gentle",
    "安心": "gentle",
    "柔声": "gentle",
    # happy
    "happy": "happy",
    "joy": "happy",
    "joyful": "happy",
    "excited": "happy",
    "cheerful": "happy",
    "smile": "happy",
    "smiling": "happy",
    "laughing": "happy",
    "curious": "happy",
    "delighted": "happy",
    "surprised": "happy",
    "开心": "happy",
    "高兴": "happy",
    "欣喜": "happy",
    "喜悦": "happy",
    "愉快": "happy",
    "明朗": "happy",
    "兴奋": "happy",
    "期待": "happy",
    "微笑": "happy",
    "惊喜": "happy",
    # playful
    "playful": "playful",
    "teasing": "playful",
    "winking": "playful",
    "funny": "playful",
    "amused": "playful",
    "mischievous": "playful",
    "silly": "playful",
    "witty": "playful",
    "俏皮": "playful",
    "打趣": "playful",
    "调侃": "playful",
    "轻笑": "playful",
    "狡黠": "playful",
    "调皮": "playful",
    "戏谑": "playful",
    "坏笑": "playful",
    # shy
    "shy": "shy",
    "flustered": "shy",
    "bashful": "shy",
    "embarrassed": "shy",
    "blushing": "shy",
    "apologetic": "shy",
    "sorry": "shy",
    "害羞": "shy",
    "羞涩": "shy",
    "不好意思": "shy",
    "脸红": "shy",
    "羞赧": "shy",
    "微嗔": "shy",
    "嗔怪": "shy",
    "抱歉": "shy",
    # serious
    "serious": "serious",
    "composed": "serious",
    "firm": "serious",
    "resolute": "serious",
    "focused": "serious",
    "confident": "serious",
    "cool": "serious",
    "guarded": "serious",
    "alert": "serious",
    "solemn": "serious",
    "stern": "serious",
    "angry": "serious",
    "annoyed": "serious",
    "anxious": "serious",
    "thinking": "serious",
    "认真": "serious",
    "严肃": "serious",
    "沉稳": "serious",
    "坚定": "serious",
    "果断": "serious",
    "专注": "serious",
    "警惕": "serious",
    "郑重": "serious",
    "冷静": "serious",
    # tender
    "tender": "tender",
    "sad": "tender",
    "comfort": "tender",
    "comforting": "tender",
    "empathetic": "tender",
    "worried": "tender",
    "hurt": "tender",
    "crying": "tender",
    "soothing": "tender",
    "melancholic": "tender",
    "心疼": "tender",
    "安慰": "tender",
    "抚慰": "tender",
    "低落": "tender",
    "难过": "tender",
    "怜惜": "tender",
    "担忧": "tender",
    "伤感": "tender",
    "叹气": "tender",
}

_EXPLICIT_PREFIXES = ("emotion", "mood", "tone", "情绪", "语气", "情感")
_BRACKET_CANDIDATE_RE = re.compile(
    r"(?:\[\s*([^\[\]\n]{1,28}?)\s*\]"
    r"|【\s*([^【】\n]{1,28}?)\s*】"
    r"|<\s*([^<>\n]{1,28}?)\s*>"
    r"|\(\s*([^\(\)\n]{1,18}?)\s*\)"
    r"|（\s*([^（）\n]{1,18}?)\s*）)"
)

_UNCLOSED_OPENERS = {
    "[": "]",
    "【": "】",
    "<": ">",
    "(": ")",
    "（": "）",
}

_SPOKEN_CHAR_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_LEAKED_XML_TOOL_TAG_RE = re.compile(
    r"(?:"
    r"</?\s*[|｜]{1,2}\s*DSML\s*[|｜]{1,2}[^>]*>"
    r"|<\s*[|｜]{1,2}\s*tool[\s_\u2581][a-z_\s\u2581]{2,32}\s*[|｜]{1,2}\s*>"
    r"|</?\s*(?:parameter|invoke|calls|tool_call|function_calls?|antml:[a-z_]+|minimax:[a-z_]+)\b[^>]*>"
    r")",
    flags=re.IGNORECASE,
)
_STRONG_PUNCT = ("。", "！", "!", "？", "?", "；", ";", "\n")
_SOFT_PUNCT = ("，", ",", "：")
_ALL_CUT_PUNCT = _STRONG_PUNCT + _SOFT_PUNCT

_LEADING_JUNK_CHARS = set(" \t\r\n，,。.！!？?；;：:、-－")
_EMOJI_RANGES = (
    (0x1F600, 0x1F64F),
    (0x1F300, 0x1F5FF),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
)

_EMOJI_TO_MEI_EMOTION: Dict[str, str] = {
    "😌": "calm",
    "😶": "calm",
    "😍": "gentle",
    "😘": "gentle",
    "😴": "gentle",
    "🙂": "happy",
    "😆": "happy",
    "😲": "happy",
    "😉": "playful",
    "😂": "playful",
    "😜": "playful",
    "🤤": "playful",
    "😳": "shy",
    "😏": "serious",
    "😎": "serious",
    "🤔": "serious",
    "😠": "serious",
    "🙄": "serious",
    "😔": "tender",
    "😭": "tender",
    "😱": "tender",
}

_UNCLOSED_CANONICAL_TAG_RE = re.compile(
    r"[\[<【]\s*(?:(?:emotion|mood|tone|情绪|语气|情感)\s*[:：=]\s*)?"
    r"(calm|gentle|happy|playful|shy|serious|tender)"
    r"(?![^\[\]>】\n]{0,20}[\]>】])"
    r"(?=\s*[\u4e00-\u9fff，,。！!？?～~…\s]|$)",
    flags=re.IGNORECASE,
)

# Lexical cues for Layer-2 clause emotion inference when no explicit tag is present
_LEXICAL_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "shy",
        (
            "不好意思",
            "有些在意",
            "别这么夸",
            "突然这么夸",
            "害羞",
            "脸红",
            "讨厌啦",
            "别取笑",
            "怪难为情",
        ),
    ),
    (
        "tender",
        (
            "别难过",
            "不要难过",
            "心疼",
            "抱抱",
            "受委屈",
            "委屈你",
            "一直陪着你",
            "陪在你身边",
            "没关系的",
            "别太自责",
            "不哭",
            "迷茫时",
        ),
    ),
    (
        "playful",
        (
            "呵呵",
            "呵...",
            "呵…",
            "被我抓到",
            "猜你现在",
            "偷偷熬夜",
            "偷懒哟",
            "不介意多做",
            "难道你也是",
            "真是拿你没办法",
            "调皮",
            "耍赖",
        ),
    ),
    (
        "happy",
        (
            "太棒了",
            "太棒啦",
            "太好了",
            "太好啦",
            "真开心",
            "好开心",
            "很高兴",
            "恭喜",
            "庆祝一下",
            "真不错",
            "好期待",
            "出去野餐",
            "大功告成",
            "顺利搞定",
        ),
    ),
    (
        "gentle",
        (
            "辛苦了",
            "辛苦啦",
            "累不累",
            "早点睡",
            "早点休息",
            "注意休息",
            "喝口温水",
            "喝点热茶",
            "准备好了热茶",
            "添衣服",
            "别着凉",
            "别勉强",
            "身体会吃不消",
            "交给我吧",
            "慢点吃",
            "欢迎回来",
        ),
    ),
    (
        "serious",
        (
            "不能马虎",
            "不要放松警惕",
            "注意安全",
            "危险",
            "立即停止",
            "请确认",
            "检查完了",
            "派单给反重力",
            "锁在安全挡",
            "权限不足",
            "绝不能",
            "务必小心",
        ),
    ),
)

_DEFAULT_MOOD_DEVICE_KEY = "__default__"
_anima_lock = threading.Lock()
_last_anima_emotion_by_device: Dict[str, str] = {}
_last_anima_emotion: str = "calm"
_cached_manifest: Optional[Dict[str, Dict[str, Any]]] = None

_gsv_session_lock = threading.Lock()
_gsv_session = None

# Shared GPU synthesis/weights lock between live ESP32 TTS and 18080 Console preview (P1-14)
GSV_GPU_LOCK = threading.RLock()
_gsv_inflight_lock = threading.Lock()
_gsv_live_inflight: int = 0

FIRST_SENTENCE_MAX_STRONG_CHARS = 20
SUBSEQUENT_MAX_STRONG_CHARS = 38
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


def _post_gsv_sync(url: str, request_json: Dict[str, Any], timeout: float):
    """Run blocking GPT-SoVITS HTTP POST + GSV_GPU_LOCK on a worker thread (C-5)."""
    sess = _get_gsv_http_session()
    with GSV_GPU_LOCK:
        return sess.post(url, json=request_json, timeout=timeout)


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
        if getattr(resp, "status_code", 0) == 200 and getattr(resp, "content", None):
            return bytes(resp.content)
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


def build_gsv_request_snapshot(
    provider,
    clean_text: str,
    *,
    explicit_segment_emotion: Optional[str] = None,
    turn_explicit_emotion: Optional[str] = None,
    device_id: Optional[str] = None,
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
    is_mei = is_raiden_mei_voice(ref_audio_path)
    if is_mei:
        emo_key = resolve_mei_emotion(
            clean_text,
            explicit_segment_emotion=explicit_segment_emotion,
            turn_explicit_emotion=turn_explicit_emotion,
            device_id=device_id,
        )
        prof = get_mei_emotion_profile(emo_key)
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
        "batch_threshold": getattr(provider, "batch_threshold", 0.75),
        "split_bucket": getattr(provider, "split_bucket", True),
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

    # P1-10: Lower first-segment DiT sample_steps (default 8) to cut first-audio latency while keeping
    # full quality on segment 2+ (which synthesize concurrently while segment 1 plays).
    if is_first_segment and not getattr(provider, "_suppress_first_seg_fast_steps", False):
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
        if cls_mod and "gpt_sovits" not in cls_mod and not is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")):
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


def _rewrite_host_data_path(raw_path: str) -> str:
    """Rewrite legacy data/voices or ${HOST_DATA_DIR} / ${HOST_VOICES_DIR} paths to active host paths (A-1)."""
    p = (raw_path or "").replace("\\", "/").strip()
    if not p:
        return p
    if "${HOST_VOICES_DIR}" in p:
        p = p.replace("${HOST_VOICES_DIR}", HOST_VOICES_PREFIX)
    if "${HOST_DATA_DIR}" in p:
        p = p.replace("${HOST_DATA_DIR}", HOST_DATA_PREFIX)

    p_low = p.lower()
    # Map legacy .../data/voices/<rest> -> HOST_VOICES_PREFIX/<rest>
    for pfx in (
        f"{HOST_DATA_PREFIX}/voices/".lower(),
        f"{_LEGACY_HOST_DATA_PREFIX}/voices/".lower(),
        f"{_LEGACY_HOST_VOICES_PREFIX}/".lower(),
        "data/voices/",
        "voices/",
    ):
        if p_low.startswith(pfx):
            return f"{HOST_VOICES_PREFIX}/{p[len(pfx):]}"

    # Map legacy taiwan ref audio under data/ -> HOST_VOICES_PREFIX/
    for fname in ("xiaozhi_taiwan_ref.wav", "xiaozhi_taiwan_aux.wav", "wakeup_words_short.wav"):
        for pfx in (f"{HOST_DATA_PREFIX}/", f"{_LEGACY_HOST_DATA_PREFIX}/", "data/"):
            if p_low == f"{pfx.lower()}{fname}":
                return f"{HOST_VOICES_PREFIX}/{fname}"

    if p_low.startswith(_LEGACY_HOST_DATA_PREFIX.lower()):
        p = HOST_DATA_PREFIX + p[len(_LEGACY_HOST_DATA_PREFIX) :]
    elif p.startswith("data/"):
        p = HOST_DATA_PREFIX + "/" + p[len("data/") :]
    return p


def _to_local_fs_path(host_or_local_path: str) -> str:
    p = _rewrite_host_data_path(host_or_local_path)
    if os.name != "nt":
        p_low = p.lower()
        for v_pfx in (
            HOST_VOICES_PREFIX,
            f"{HOST_DATA_PREFIX}/voices",
            f"{_LEGACY_HOST_DATA_PREFIX}/voices",
            _LEGACY_HOST_VOICES_PREFIX,
        ):
            if v_pfx and p_low.startswith(v_pfx.lower()):
                return CONTAINER_VOICES_PREFIX + p[len(v_pfx) :]
        for d_pfx in (HOST_DATA_PREFIX, _LEGACY_HOST_DATA_PREFIX):
            if d_pfx and p_low.startswith(d_pfx.lower()):
                return CONTAINER_DATA_PREFIX + p[len(d_pfx) :]
    return p


def ref_audio_exists(host_ref_path: str) -> bool:
    local_p = _to_local_fs_path(host_ref_path)
    return bool(local_p and os.path.isfile(local_p))


def load_mei_emotions(force_reload: bool = False) -> Dict[str, Dict[str, Any]]:
    global _cached_manifest
    if _cached_manifest is not None and not force_reload:
        return _cached_manifest

    merged: Dict[str, Dict[str, Any]] = {
        k: dict(v) for k, v in DEFAULT_MEI_EMOTIONS.items()
    }
    manifest_candidates = [
        _to_local_fs_path(f"{MEI_DIR_HOST}/mei_emotions.json"),
        os.path.join(CONTAINER_VOICES_PREFIX, "raiden_mei", "mei_emotions.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices", "raiden_mei", "mei_emotions.json"),
    ]
    for mp in manifest_candidates:
        if os.path.isfile(mp):
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                emo_map = data.get("emotions") if isinstance(data, dict) else None
                if isinstance(emo_map, dict):
                    for k, val in emo_map.items():
                        if isinstance(val, dict) and k in merged:
                            merged[k].update(val)
                        elif isinstance(val, dict):
                            merged[k] = dict(val)
                break
            except Exception:
                pass

    for prof in merged.values():
        if isinstance(prof, dict) and prof.get("ref_audio_path"):
            prof["ref_audio_path"] = _rewrite_host_data_path(str(prof["ref_audio_path"]))

    _cached_manifest = merged
    return merged


def normalize_emotion_name(raw_token: str) -> Tuple[Optional[str], bool]:
    """Parse inner bracket content.

    Returns:
        (canonical_emotion_or_None, should_strip_tag)
    """
    if not raw_token:
        return None, False
    t = raw_token.strip()
    explicit_prefixed = False
    for sep in (":", "：", "="):
        if sep in t:
            left, right = t.split(sep, 1)
            if left.strip().lower() in _EXPLICIT_PREFIXES:
                explicit_prefixed = True
                t = right.strip()
                break

    key = t.strip().lower()
    if key in EMOTION_ALIASES:
        return EMOTION_ALIASES[key], True
    if explicit_prefixed:
        # Unknown value inside an explicit [emotion: xxx] wrapper: still strip it so TTS never reads it!
        return None, True
    return None, False


def extract_and_strip_emotion(
    text: str, flush_incomplete: bool = False
) -> Tuple[Optional[str], str]:
    """Extract the last/active hidden emotion tag from `text` and strip all emotion tags."""
    if not text:
        return None, ""

    detected: Optional[str] = None

    def _repl(match: re.Match) -> str:
        nonlocal detected
        inner = next((g for g in match.groups() if g is not None), "")
        is_paren = match.group(4) is not None or match.group(5) is not None
        emo, should_strip = normalize_emotion_name(inner)
        if not should_strip:
            return match.group(0)
        # For parentheses like (温柔) or (happy), only strip when it's a pure emotion/stage cue
        if is_paren and emo is None:
            return match.group(0)
        if emo is not None:
            detected = emo
        return ""

    cleaned = _BRACKET_CANDIDATE_RE.sub(_repl, text)

    # Defense-in-depth: strip malformed unclosed canonical English tags (e.g. "[gentle 哥哥")
    def _unclosed_repl(match: re.Match) -> str:
        nonlocal detected
        emo_cand = match.group(1).strip().lower()
        if emo_cand in EMOTION_ALIASES:
            detected = EMOTION_ALIASES[emo_cand]
        return ""

    if flush_incomplete or any(op in cleaned for op in ("[", "<", "【")):
        # Only strip unclosed canonical tags that already have trailing text OR when flushing
        if flush_incomplete:
            cleaned = _UNCLOSED_CANONICAL_TAG_RE.sub(_unclosed_repl, cleaned)
        else:
            # During streaming, only strip an unclosed canonical tag if followed by non-ASCII/punct text
            cleaned = re.sub(
                r"[\[<【]\s*(?:(?:emotion|mood|tone|情绪|语气|情感)\s*[:：=]\s*)?"
                r"(calm|gentle|happy|playful|shy|serious|tender)"
                r"(?![^\[\]>】\n]{0,20}[\]>】])"
                r"(?=\s*[\u4e00-\u9fff，,。！!？?])",
                _unclosed_repl,
                cleaned,
                flags=re.IGNORECASE,
            )

    if flush_incomplete:
        # Guard against stream cutoff mid-tag at the very start like "[gentle" or "[温"
        m_inc = re.match(
            r"^\s*[\[<【（(]\s*(?:(?:emotion|mood|tone|情绪|语气|情感)\s*[:：=]\s*)?([A-Za-z\u4e00-\u9fff_]{1,16})\s*$",
            cleaned,
            flags=re.IGNORECASE,
        )
        if m_inc:
            token = m_inc.group(1).strip().lower()
            emo, should_strip = normalize_emotion_name(token)
            if should_strip or any(a.startswith(token) for a in EMOTION_ALIASES):
                if emo:
                    detected = emo
                cleaned = ""

    # Fallback: if no bracket tag was present, map a leading emotion emoji before TTS strips emojis
    if detected is None and cleaned:
        idx = 0
        while idx < len(cleaned) and (cleaned[idx].isspace() or _is_emoji_char(cleaned[idx])):
            ch = cleaned[idx]
            if ch in _EMOJI_TO_MEI_EMOTION:
                detected = _EMOJI_TO_MEI_EMOTION[ch]
                break
            idx += 1

    return detected, cleaned


def strip_emotion_tags(text: str) -> str:
    _, cleaned = extract_and_strip_emotion(text, flush_incomplete=False)
    cleaned_no_xml = _LEAKED_XML_TOOL_TAG_RE.sub("", cleaned)
    if cleaned_no_xml != cleaned:
        cleaned = cleaned_no_xml.rstrip()
    else:
        cleaned = cleaned_no_xml
    return cleaned.lstrip() if text and not text[0].isspace() else cleaned


def _strip_leading_ws_and_emojis(text: str) -> str:
    idx = 0
    while idx < len(text) and (text[idx].isspace() or _is_emoji_char(text[idx])):
        idx += 1
    return text[idx:]


def has_unclosed_leading_tag(text: str) -> bool:
    """Return True if streaming buffer begins with an unclosed potential emotion tag."""
    if not text:
        return False
    s = _strip_leading_ws_and_emojis(text)
    if not s:
        return False
    opener = s[0]
    closer = _UNCLOSED_OPENERS.get(opener)
    if not closer:
        return False
    if closer in s[1:]:
        return False
    if len(s) > 24:
        return False
    if any(p in s for p in ("。", "！", "!", "？", "?", "\n")):
        return False
    inner = s[1:].strip().lower()
    if not inner:
        return True
    # Wait if inner could still become [emotion:...], [gentle], [温柔], etc.
    for pfx in _EXPLICIT_PREFIXES:
        if pfx.startswith(inner) or inner.startswith(pfx):
            return True
    for alias in EMOTION_ALIASES:
        if alias.startswith(inner):
            return True
    return False


def count_spoken_chars(text: str) -> int:
    _, clean = extract_and_strip_emotion(text, flush_incomplete=False)
    return len(_SPOKEN_CHAR_RE.findall(clean))


def _is_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def clean_segment_for_tts(text: str) -> str:
    """Strip hidden emotion tags, leaked XML tool tags, emojis, and leading orphan punctuation while keeping
    expressive internal and ending punctuation (，。！？～…) intact for GPT-SoVITS."""
    _, s = extract_and_strip_emotion(text, flush_incomplete=True)
    s = _LEAKED_XML_TOOL_TAG_RE.sub("", s)
    s = "".join(ch for ch in s if not _is_emoji_char(ch) and ch != "\r")
    s = re.sub(r"\s*\n+\s*", " ", s).strip()
    start = 0
    while start < len(s) and (s[start] in _LEADING_JUNK_CHARS or _is_emoji_char(s[start])):
        start += 1
    s = s[start:].strip()
    if not _SPOKEN_CHAR_RE.search(s):
        return ""
    return s


def safe_clean_response_garbage(text: str, *args, **kwargs) -> str:
    """Lossless replacement for upstream `ConnectionHandler._clean_response_garbage`.

    Strips genuine leaked JSON closure suffixes (e.g. `"}`, `"}}`, `\\n"}}`) without
    ever eating `]` on emotion tags (`[gentle]`) or `[安全挡]`, and without stripping
    trailing spaces off interior streaming chunks.
    """
    if not text:
        return text
    _line_garbage_chars = frozenset('"\'}）')
    lines = text.split("\n")
    cleaned_lines = []
    removed_line = False
    for line in lines:
        st = line.strip()
        if (
            st
            and len(st) <= 8
            and ("}" in st or '"' in st)
            and all(c in _line_garbage_chars for c in st)
        ):
            removed_line = True
            continue
        cleaned_lines.append(line)
    result = "\n".join(cleaned_lines)
    result_no_xml = _LEAKED_XML_TOOL_TAG_RE.sub("", result)
    xml_removed = result_no_xml != result
    result = result_no_xml
    m_tail = re.search(r'(?:["\']\s*\}+|\}{2,})\s*$', result)
    if m_tail:
        result = result[: m_tail.start()].rstrip()
    elif removed_line or xml_removed:
        result = result.rstrip()
    return result


def infer_emotion_from_text(text: str, allow_soft_wave_cue: bool = True) -> Optional[str]:
    """Infer emotion from Chinese lexical & prosodic cues when no explicit tag is present."""
    clean = strip_emotion_tags(text or "").strip()
    if not clean:
        return None
    for emo, keywords in _LEXICAL_RULES:
        for kw in keywords:
            if kw in clean:
                return emo
    if ("！" in clean or "!" in clean) and any(w in clean for w in ("棒", "好", "赞", "成", "耶", "啦")):
        return "happy"
    if allow_soft_wave_cue and ("～" in clean or "~" in clean):
        return "playful"
    return None


def _normalize_mood_device_key(device_id: Optional[str] = None) -> str:
    dev = str(device_id or "").strip().lower()
    return dev if dev else _DEFAULT_MOOD_DEVICE_KEY


def set_anima_mood(mood_summary: str, device_id: Optional[str] = None) -> None:
    """Update cached Anima Brain emotion from `/xiaozhi/recall` mood summary string, isolated per device_id (C-6)."""
    global _last_anima_emotion
    if not mood_summary:
        return
    m = re.search(r"last_emotion\s*=\s*([A-Za-z_]+)", mood_summary)
    raw = m.group(1).strip().lower() if m else mood_summary.strip().lower()
    emo, _ = normalize_emotion_name(raw)
    if emo:
        dev_key = _normalize_mood_device_key(device_id)
        with _anima_lock:
            _last_anima_emotion_by_device[dev_key] = emo
            if dev_key == _DEFAULT_MOOD_DEVICE_KEY:
                _last_anima_emotion = emo
            if len(_last_anima_emotion_by_device) > 256:
                for k in list(_last_anima_emotion_by_device.keys()):
                    if k != _DEFAULT_MOOD_DEVICE_KEY:
                        _last_anima_emotion_by_device.pop(k, None)
                        break


def get_anima_mood(device_id: Optional[str] = None) -> str:
    dev_key = _normalize_mood_device_key(device_id)
    with _anima_lock:
        if dev_key in _last_anima_emotion_by_device:
            return _last_anima_emotion_by_device[dev_key]
        return _last_anima_emotion_by_device.get(_DEFAULT_MOOD_DEVICE_KEY, _last_anima_emotion)


def clear_anima_mood(device_id: Optional[str] = None) -> None:
    """Reset cached Anima mood for one device_id or all devices (for cleanup/tests)."""
    global _last_anima_emotion
    with _anima_lock:
        if device_id is None:
            _last_anima_emotion_by_device.clear()
            _last_anima_emotion = "calm"
        else:
            dev_key = _normalize_mood_device_key(device_id)
            _last_anima_emotion_by_device.pop(dev_key, None)
            if dev_key == _DEFAULT_MOOD_DEVICE_KEY:
                _last_anima_emotion = "calm"


def resolve_mei_emotion(
    clean_text: str,
    explicit_segment_emotion: Optional[str] = None,
    turn_explicit_emotion: Optional[str] = None,
    device_id: Optional[str] = None,
) -> str:
    """3-layer emotion resolution:
    1. Explicit tag on current segment
    2. Turn-level explicit tag (unless a subsequent clause has a clear lexical mood shift;
       soft `~` punctuation never hijacks an explicit turn tag) or clause lexical cue
    3. Anima Brain PAD / turn_appraisals `last_emotion` continuity (isolated per device_id)
    4. Default 'calm'
    """
    emotions = load_mei_emotions()
    if explicit_segment_emotion and explicit_segment_emotion in emotions:
        chosen = explicit_segment_emotion
    else:
        if turn_explicit_emotion and turn_explicit_emotion in emotions:
            # Do not let casual `~` override an explicit turn tag like [happy] or [gentle]
            strong_lexical = infer_emotion_from_text(clean_text, allow_soft_wave_cue=False)
            if turn_explicit_emotion == "calm" and strong_lexical in emotions:
                chosen = strong_lexical
            elif strong_lexical in ("playful", "serious", "tender", "shy"):
                chosen = strong_lexical
            else:
                chosen = turn_explicit_emotion
        else:
            lexical = infer_emotion_from_text(clean_text, allow_soft_wave_cue=True)
            if lexical and lexical in emotions:
                chosen = lexical
            else:
                anima_emo = get_anima_mood(device_id=device_id)
                chosen = anima_emo if anima_emo in emotions else "calm"

    prof = emotions.get(chosen) or emotions["calm"]
    if not ref_audio_exists(prof.get("ref_audio_path", "")):
        return "calm"
    return chosen


def get_mei_emotion_profile(emotion_key: str) -> Dict[str, Any]:
    emotions = load_mei_emotions()
    prof = emotions.get(emotion_key) or emotions["calm"]
    if not ref_audio_exists(prof.get("ref_audio_path", "")):
        prof = emotions["calm"]
    return dict(prof)


def is_raiden_mei_voice(ref_audio_path: Optional[str]) -> bool:
    return "raiden_mei" in str(ref_audio_path or "").replace("\\", "/").lower()


def has_voice_emotion_tag_rule(prompt: str) -> bool:
    if not prompt:
        return False
    tag_hits = sum(
        1 for k in ("[calm]", "[gentle]", "[happy]", "[playful]", "[shy]", "[serious]", "[tender]") if k in prompt
    )
    return tag_hits >= 2 or ("情绪标签" in prompt and "[" in prompt)


def strip_voice_tag_instruction_for_anima(prompt: str) -> str:
    """Remove voice-only [calm]/[gentle] bracket tag output rule before syncing persona to
    18787 Companion Studio web chat, keeping personality and spoken style 100% aligned."""
    if not prompt:
        return ""
    lines = []
    for ln in prompt.splitlines():
        if has_voice_emotion_tag_rule(ln):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def find_prosody_cut_pos(
    current_text: str, is_first_sentence: bool, quote_safe_cut_fn
) -> int:
    """Find smooth clause boundary without chopping 2-3 char openings at the first comma.

    P1-10: Tightens first-sentence strong-punctuation ceiling to ~20 spoken chars (vs 38 for
    later sentences) and allows soft comma split once >=14 spoken chars for faster first-audio.
    """
    if not current_text:
        return -1
    if has_unclosed_leading_tag(current_text):
        return -1

    min_strong = 6 if is_first_sentence else 4
    min_soft = 14 if is_first_sentence else 28
    max_strong = FIRST_SENTENCE_MAX_STRONG_CHARS if is_first_sentence else SUBSEQUENT_MAX_STRONG_CHARS

    # Check if a strong sentence boundary within target first/breath length is already buffered
    first_strong_cut = -1
    for idx, ch in enumerate(current_text):
        if ch in _STRONG_PUNCT:
            safe = quote_safe_cut_fn(current_text, idx, _ALL_CUT_PUNCT)
            if safe != -1 and count_spoken_chars(current_text[: safe + 1]) >= min_strong:
                first_strong_cut = safe
                break

    if first_strong_cut != -1 and count_spoken_chars(current_text[: first_strong_cut + 1]) <= max_strong:
        return first_strong_cut

    # Otherwise allow soft comma/colon split once the clause is long enough (>=14 / >=28 spoken chars)
    for idx, ch in enumerate(current_text):
        if ch in _STRONG_PUNCT:
            safe = quote_safe_cut_fn(current_text, idx, _ALL_CUT_PUNCT)
            if safe != -1 and count_spoken_chars(current_text[: safe + 1]) >= min_strong:
                return safe
        elif ch in _SOFT_PUNCT:
            safe = quote_safe_cut_fn(current_text, idx, _ALL_CUT_PUNCT)
            if safe != -1 and count_spoken_chars(current_text[: safe + 1]) >= min_soft:
                return safe

    return -1


async def _send_esp32_emotion_ws(conn, emoji: str, esp32_emotion: str) -> None:
    if not conn or not getattr(conn, "websocket", None):
        return
    if not (getattr(conn, "features", None) or {}).get("emoji", True):
        return
    cur_sid = getattr(conn, "sentence_id", None)
    if getattr(conn, "_last_pushed_mei_emotion", None) == (cur_sid, esp32_emotion):
        return
    conn._last_pushed_mei_emotion = (cur_sid, esp32_emotion)
    conn._last_pushed_mei_sid = cur_sid
    payload = json.dumps(
        {
            "type": "llm",
            "text": emoji,
            "emotion": esp32_emotion,
            "session_id": getattr(conn, "session_id", ""),
        }
    )
    try:
        await conn.websocket.send(payload)
    except Exception:
        pass


def _push_esp32_emotion_async(conn, emoji: str, esp32_emotion: str) -> None:
    if not conn or not getattr(conn, "websocket", None) or not getattr(conn, "loop", None):
        return
    if not (getattr(conn, "features", None) or {}).get("emoji", True):
        return
    cur_sid = getattr(conn, "sentence_id", None)
    if getattr(conn, "_last_pushed_mei_emotion", None) == (cur_sid, esp32_emotion):
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _send_esp32_emotion_ws(conn, emoji, esp32_emotion), conn.loop
        )
    except Exception:
        pass


_PROMPT_REPLACEMENTS: Tuple[Tuple[str, str, str], ...] = (
    (
        "brackets_rule",
        'NEVER use brackets to express inner thoughts or actions (e.g. ”[无奈地叹气]”,”(笑着说)”).',
        'Start every spoken reply (including direct_answer.response) with ONE hidden English emotion tag from [calm], [gentle], [happy], [playful], [shy], [serious], [tender] (optional second tag only if mood genuinely shifts mid-reply). NEVER use stage-direction brackets like ”[无奈地叹气]” or ”(笑着说)”.',
    ),
    (
        "get_to_the_point",
        '1. [Get to the point] Never be verbose. The first sentence must directly address the topic — no unnecessary pleasantries or preamble.',
        '1. [Natural pacing] Be engaging, warm, and thorough. Follow the length and conversational pacing described in <identity> rather than cutting answers overly short.',
    ),
    (
        "slice_long_content",
        '- [Slice long content] For long-form content like storytelling or knowledge sharing, NEVER dump everything at once. Start with the core content directly, then naturally ask the user whether to continue at the end (e.g., tell an actual segment first, then ask ”要继续听吗？”). Start on demand, stop on demand, yield to interruptions.',
        '- [Rich storytelling & explanation] For storytelling, explanations, or thoughtful chat, give a full, vivid, satisfying multi-sentence segment instead of stopping after just one or two sentences; only pause to ask if the user wants to keep going after delivering a substantial part.',
    ),
    (
        "colloquial_zahuishi",
        '"咋回事"',
        '"怎么啦"',
    ),
)


_WARNED_MISSING_PROMPT_ANCHORS: set[str] = set()
_WARNED_ANCHORS_LOCK = threading.Lock()


def reset_prompt_anchor_warnings() -> None:
    """Clear one-shot missing-anchor warning cache (for isolated unit tests)."""
    with _WARNED_ANCHORS_LOCK:
        _WARNED_MISSING_PROMPT_ANCHORS.clear()


def verify_prompt_base_anchors(
    prompt_path: str = os.path.join(xz_common.ROOT_DIR, "agent-base-prompt.txt"),
) -> Tuple[bool, str]:
    """Verify upstream agent-base-prompt.txt still contains every anchor we .replace()."""
    if not os.path.isfile(prompt_path):
        return False, f"base prompt file missing: {prompt_path}"
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as exc:
        return False, f"failed to read base prompt: {exc}"
    missing = [name for name, anchor, _ in _PROMPT_REPLACEMENTS if anchor not in raw]
    if missing:
        return False, f"missing prompt anchor(s): {', '.join(missing)}"
    return True, f"verified {len(_PROMPT_REPLACEMENTS)} anchors"


def _apply_prompt_harmony_detailed(
    rendered: str, *, warn_once: bool = True
) -> Tuple[str, List[str], List[str]]:
    """Soft-apply prompt harmonization replacements, skipping missing anchors without raising (C-2)."""
    out = rendered
    applied: List[str] = []
    missing: List[str] = []
    for anchor_name, old_anchor, new_text in _PROMPT_REPLACEMENTS:
        if old_anchor in out:
            out = out.replace(old_anchor, new_text)
            applied.append(anchor_name)
        else:
            missing.append(anchor_name)
            if warn_once:
                should_warn = False
                with _WARNED_ANCHORS_LOCK:
                    if anchor_name not in _WARNED_MISSING_PROMPT_ANCHORS:
                        _WARNED_MISSING_PROMPT_ANCHORS.add(anchor_name)
                        should_warn = True
                if should_warn:
                    print(
                        f"[MeiEmotionRouter] WARNING: 上游提示词锚点缺失，已跳过该项替换 (C-2 软失败): {anchor_name}",
                        flush=True,
                    )
    return out, applied, missing


def dry_run_prompt_harmony(
    prompt_path: str = os.path.join(xz_common.ROOT_DIR, "agent-base-prompt.txt"),
) -> Tuple[str, str, List[str], List[str]]:
    """Run prompt harmony replacements against the real upstream template at startup (C-2)."""
    all_names = [name for name, _, _ in _PROMPT_REPLACEMENTS]
    if not os.path.isfile(prompt_path):
        return "failed", f"base prompt file missing: {prompt_path}", [], all_names
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as exc:
        return "failed", f"failed to read base prompt: {exc}", [], all_names

    _, applied, missing = _apply_prompt_harmony_detailed(raw, warn_once=True)
    total = len(_PROMPT_REPLACEMENTS)
    if not missing:
        return "ok", f"verified {len(applied)}/{total} anchors", applied, []
    if applied:
        return (
            "partial",
            f"applied {len(applied)}/{total} anchors; missing: {', '.join(missing)}",
            applied,
            missing,
        )
    return (
        "partial",
        f"0/{total} anchors matched; missing: {', '.join(missing)}",
        [],
        missing,
    )


def apply_prompt_harmony_replacements(rendered: str, *, record_registry: bool = True) -> str:
    """Soft-apply prompt harmonization replacements without ever crashing live turns on anchor drift (C-2)."""
    out, applied, missing = _apply_prompt_harmony_detailed(rendered, warn_once=True)
    if record_registry and missing:
        total = len(_PROMPT_REPLACEMENTS)
        patch_registry.record_patch(
            "mei_prompt_anchors",
            "partial",
            target="agent-base-prompt.txt",
            detail=f"runtime applied {len(applied)}/{total}; missing: {', '.join(missing)}",
            contract_ok=False,
            category="experience",
        )
    return out


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
            anchors_status, anchors_detail, _applied_a, _missing_a = dry_run_prompt_harmony()
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

    anchors_status, anchors_detail, _applied_a, _missing_a = dry_run_prompt_harmony()
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
    ConnectionHandler._clean_response_garbage = staticmethod(safe_clean_response_garbage)

    # 0b. Harmonize agent-base-prompt.txt & direct_answer few-shot when [emotion] tag mode is active
    _orig_build_enhanced_prompt = PromptManager.build_enhanced_prompt
    _orig_inject_fewshot = ConnectionHandler._inject_tool_call_fewshot

    @functools.wraps(_orig_build_enhanced_prompt)
    def _patched_build_enhanced_prompt(
        self, user_prompt: str, device_id: str, *args, **kwargs
    ) -> str:
        if has_voice_emotion_tag_rule(user_prompt):
            kw = dict(kwargs)
            kw["emoji_enabled"] = False
            rendered = _orig_build_enhanced_prompt(
                self, user_prompt, device_id, *args, **kw
            )
            return apply_prompt_harmony_replacements(rendered)
        return _orig_build_enhanced_prompt(
            self, user_prompt, device_id, *args, **kwargs
        )

    @functools.wraps(_orig_inject_fewshot)
    def _patched_inject_tool_call_fewshot(self, *args, **kwargs):
        res = _orig_inject_fewshot(self, *args, **kwargs)
        try:
            if has_voice_emotion_tag_rule(str((self.config or {}).get("prompt") or "")):
                for m in getattr(self.dialogue, "dialogue", []) or []:
                    if getattr(m, "is_temporary", False) and getattr(m, "tool_calls", None):
                        for tc in m.tool_calls:
                            fn = tc.get("function") if isinstance(tc, dict) else None
                            if isinstance(fn, dict) and fn.get("name") == "direct_answer":
                                fn["arguments"] = json.dumps(
                                    {
                                        "response": "[gentle] 好呀，哥哥想听什么类型的故事呢？轻松一点的日常，还是奇幻冒险的？"
                                    },
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
        clean = strip_emotion_tags(text or "")
        return _orig_store_tts_text(self, sentence_id, clean, *args, **kwargs)

    @functools.wraps(_orig_get_segment_text)
    def _patched_get_segment_text(self, *args, **kwargs) -> Optional[str]:
        while True:
            full_text = "".join(self.tts_text_buff)
            current_text = full_text[self.processed_chars :]
            if not current_text:
                return None

            # Reset per-turn emotion & first-segment state when a brand-new sentence_id starts
            cur_sid = getattr(self, "current_sentence_id", None)
            if getattr(self, "_emotion_turn_sid", None) != cur_sid:
                self._emotion_turn_sid = cur_sid
                self._turn_explicit_emotion = None
                self._pending_segment_emotion = None
                self._segment_emotion_queue = []
                self._first_segment_done_sid = None

            # Absorb leading tag early ONLY for the very first unspoken sentence of the turn
            s_lead = _strip_leading_ws_and_emojis(current_text)
            m_lead = _BRACKET_CANDIDATE_RE.match(s_lead)
            if m_lead:
                emo_cand, should_strip = normalize_emotion_name(
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
                        prof = get_mei_emotion_profile(emo_cand)
                        _push_esp32_emotion_async(
                            conn,
                            prof["esp32_emoji"],
                            prof["esp32_emotion"],
                        )

            cut_pos = find_prosody_cut_pos(
                current_text, self.is_first_sentence, self._quote_safe_cut
            )
            if cut_pos != -1:
                segment_raw = current_text[: cut_pos + 1]
                self.processed_chars += len(segment_raw)
                seg_emo, stripped = extract_and_strip_emotion(segment_raw, flush_incomplete=False)
                if seg_emo:
                    self._turn_explicit_emotion = seg_emo
                    self._pending_segment_emotion = seg_emo
                cleaned = clean_segment_for_tts(stripped)
                if not cleaned:
                    # Pure emotion tag line (e.g. "[gentle]\n"): loop without flipping is_first_sentence
                    continue
                if self.is_first_sentence:
                    self.is_first_sentence = False
                return cleaned

            if self.tts_stop_request and current_text:
                self.processed_chars = len(full_text)
                seg_emo, stripped = extract_and_strip_emotion(current_text, flush_incomplete=True)
                if seg_emo:
                    self._turn_explicit_emotion = seg_emo
                    self._pending_segment_emotion = seg_emo
                self.is_first_sentence = True
                cleaned = clean_segment_for_tts(stripped)
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
        cur_sid = getattr(self, "current_sentence_id", None)
        if getattr(self, "_emotion_turn_sid", None) != cur_sid:
            self._emotion_turn_sid = cur_sid
            self._turn_explicit_emotion = None
            self._pending_segment_emotion = None
            self._segment_emotion_queue = []
            self._first_segment_done_sid = None
        seg_emo, stripped = extract_and_strip_emotion(remaining_text, flush_incomplete=True)
        if seg_emo:
            self._turn_explicit_emotion = seg_emo
            self._pending_segment_emotion = seg_emo
        segment_text = clean_segment_for_tts(stripped)
        if segment_text:
            self.to_tts_stream(segment_text, opus_handler=opus_handler)
            return True
        return did_any

    @functools.wraps(_orig_to_tts_stream)
    def _patched_to_tts_stream(self, text, *args, **kwargs):
        seg_emo, stripped = extract_and_strip_emotion(text or "", flush_incomplete=True)
        if seg_emo:
            self._turn_explicit_emotion = seg_emo
            self._pending_segment_emotion = seg_emo
        cleaned = clean_segment_for_tts(stripped)
        if not cleaned:
            return None
        return _orig_to_tts_stream(self, cleaned, *args, **kwargs)

    @functools.wraps(_orig_to_tts)
    def _patched_to_tts(self, text, *args, **kwargs):
        seg_emo, stripped = extract_and_strip_emotion(text or "", flush_incomplete=True)
        if seg_emo:
            self._turn_explicit_emotion = seg_emo
            self._pending_segment_emotion = seg_emo
        cleaned = clean_segment_for_tts(stripped)
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

    @functools.wraps(_orig_gsv_text_to_speak)
    async def _patched_gsv_text_to_speak(self, text, output_file, *args, **kwargs):
        global _gsv_live_inflight
        seg_emo, stripped = extract_and_strip_emotion(text or "", flush_incomplete=True)
        clean_text = clean_segment_for_tts(stripped)
        if not clean_text:
            return b""

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
        dev_id = str(getattr(conn, "device_id", "") or "").strip() or None

        req_url, request_json, chosen_prof, emo_key = build_gsv_request_snapshot(
            self,
            clean_text,
            explicit_segment_emotion=explicit_seg,
            turn_explicit_emotion=turn_emo,
            device_id=dev_id,
            is_first_segment=is_first_segment,
            suppress_first_seg_fast_steps=bool(getattr(self, "_suppress_first_seg_fast_steps", False)),
        )
        if chosen_prof is not None and emo_key:
            base_logger.bind(tag=gsv_mod.TAG).info(
                f"[芽衣多情绪路由] 情绪={emo_key}({chosen_prof['label_zh']}) | speed={request_json['speed_factor']} | 文本={clean_text}"
            )

        timeout_sec = float(getattr(self, "tts_timeout", None) or 30.0)
        with _gsv_inflight_lock:
            _gsv_live_inflight += 1
        try:
            resp = await asyncio.to_thread(_post_gsv_sync, req_url or self.url, request_json, timeout_sec)
        finally:
            with _gsv_inflight_lock:
                _gsv_live_inflight = max(0, _gsv_live_inflight - 1)

        if resp.status_code == 200:
            # Clear pending segment emotion only after synthesis succeeds (safe across retries)
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
                # Only push immediately if no audio has begun playing yet for this turn;
                # subsequent clause emotions are delivered by _patched_send_tts_message via AudioRateController!
                if getattr(self, "tts_audio_first_sentence", True) and getattr(
                    conn, "_last_pushed_mei_sid", None
                ) != cur_sid:
                    _push_esp32_emotion_async(
                        conn,
                        chosen_prof["esp32_emoji"],
                        chosen_prof["esp32_emotion"],
                    )
            if output_file:
                await asyncio.to_thread(_write_file_bytes, output_file, resp.content)
            else:
                return resp.content
        else:
            error_msg = f"GPT_SoVITS_V2 TTS请求失败: {resp.status_code} - {resp.text}"
            base_logger.bind(tag=gsv_mod.TAG).error(error_msg)
            raise Exception(error_msg)

    gsv_mod.TTSProvider.text_to_speak = _patched_gsv_text_to_speak
    gsv_mod.TTSProvider.voice = property(
        lambda self: "raiden_mei" if is_raiden_mei_voice(getattr(self, "ref_audio_path", "")) else "default"
    )

    # 3. Subtitle firewall + playback-synchronized ESP32 emotion update + P1-11 ring buffer event
    _orig_send_tts_message = send_audio_mod.send_tts_message

    @functools.wraps(_orig_send_tts_message)
    async def _patched_send_tts_message(conn, state, text=None, *args, **kwargs):
        if text is not None:
            text = strip_emotion_tags(text).strip()
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
                    await _send_esp32_emotion_ws(conn, emj, esp_emo)
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
        if text and has_unclosed_leading_tag(text):
            # Wait for TTSProvider to push exact emotion once closing bracket arrives
            return
        seg_emo, stripped = extract_and_strip_emotion(text or "", flush_incomplete=False)
        if seg_emo:
            prof = get_mei_emotion_profile(seg_emo)
            _push_esp32_emotion_async(conn, prof["esp32_emoji"], prof["esp32_emotion"])
            return
        if conn and has_voice_emotion_tag_rule(str((getattr(conn, "config", None) or {}).get("prompt") or "")):
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


MEI_WAKEUP_RESPONSES = [
    "我在呢，哥哥请说吧。",
    "在呢，哥哥有什么事呀？",
    "我在听呢，哥哥请讲。",
    "哥哥叫我吗？我在呢。",
    "在呢，怎么啦哥哥？",
    "我一直在呢，随时告诉我吧。",
]
MEI_WAKEUP_FALLBACK_TEXT = "我在呢，哥哥请说吧。"
MEI_WAKEUP_FALLBACK_WAV = os.path.join(CONTAINER_VOICES_PREFIX, "raiden_mei", "wakeup_mei_default.wav")


def _trim_wav_leading_breath(wav_bytes: bytes) -> bytes:
    """Trim soft leading breath/silence from short GPT-SoVITS wakeup clips for instant response."""
    import io
    import math
    import struct
    import wave

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
                return wav_bytes
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
        win = max(1, int(sr * 0.02))
        onset = 0
        for i in range(0, len(samples) - win, win):
            rms = math.sqrt(sum(s * s for s in samples[i : i + win]) / win)
            if rms > 850:
                onset = max(0, i - int(sr * 0.04))
                break
        end = len(samples)
        for i in range(len(samples) - win, onset, -win):
            rms = math.sqrt(sum(s * s for s in samples[i : i + win]) / win)
            if rms > 250:
                end = min(len(samples), i + win + int(sr * 0.12))
                break
        if end - onset < int(sr * 0.35):
            return wav_bytes
        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(struct.pack(f"<{end - onset}h", *samples[onset:end]))
        return out.getvalue()
    except Exception:
        return wav_bytes


def get_wakeup_responses_for_tts(tts) -> list:
    if tts is not None and is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")):
        return list(MEI_WAKEUP_RESPONSES)
    return []


def apply_mei_wakeup_responses_patch() -> None:
    """Call AFTER pc_audio_router.apply_pc_audio_playback_patches() so helloHandle import order is preserved."""
    import random
    import time
    import uuid
    import core.handle.helloHandle as hello_mod
    from core.providers.tts.dto.dto import SentenceType
    from core.utils.dialogue import Message

    if getattr(hello_mod, "_mei_wakeup_patched", False):
        if patch_registry.get_patch_status("mei_wakeup_responses") is None:
            patch_registry.record_patch("mei_wakeup_responses", "ok", target="helloHandle", detail="already applied")
        return

    _orig_check_wakeup = hello_mod.checkWakeupWords
    _orig_wakeup_response = hello_mod.wakeupWordsResponse
    _orig_preheat = hello_mod.preheat_wakeup_audio_cache
    _orig_handle_hello = getattr(hello_mod, "handleHelloMessage", None)
    _last_mei_reply = {"text": ""}

    wk_checks = [
        ("helloHandle.checkWakeupWords", _orig_check_wakeup, ("conn", "text"), True),
        ("helloHandle.wakeupWordsResponse", _orig_wakeup_response, ("conn",), True),
        ("helloHandle.preheat_wakeup_audio_cache", _orig_preheat, (), True),
    ]
    if _orig_handle_hello is not None:
        wk_checks.append(("helloHandle.handleHelloMessage", _orig_handle_hello, ("conn", "msg_json"), True))
    wk_failures = []
    for cname, cfn, req_p, req_a in wk_checks:
        ok_s, det = patch_registry.verify_signature_contract(cfn, required_params=req_p, require_async=req_a)
        if not ok_s:
            wk_failures.append(f"{cname}: {det}")

    @functools.wraps(_orig_wakeup_response)
    async def _mei_aware_wakeup_words_response(conn, *args, **kwargs):
        tts = getattr(conn, "tts", None)
        if tts is None or not is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")):
            return await _orig_wakeup_response(conn, *args, **kwargs)

        # C-4: Run wakeup cache refresh ONLY when GSV is idle and lock is uncontended,
        # using `async with` + an immutable request snapshot (ZERO mutation of shared `tts`).
        if is_live_gsv_busy() or hello_mod._wakeup_response_lock.locked():
            return

        async with hello_mod._wakeup_response_lock:
            if is_live_gsv_busy():
                return

            pool = [r for r in MEI_WAKEUP_RESPONSES if r != _last_mei_reply["text"]] or list(MEI_WAKEUP_RESPONSES)
            result = random.choice(pool)
            _last_mei_reply["text"] = result

            url, req_json, _, _ = build_gsv_request_snapshot(
                tts,
                result,
                explicit_segment_emotion="gentle",
                turn_explicit_emotion="gentle",
                is_first_segment=False,
                suppress_first_seg_fast_steps=True,
            )
            if not url:
                return

            timeout_sec = float(getattr(tts, "tts_timeout", None) or 30.0)
            raw_wav = await asyncio.to_thread(
                _synthesize_gsv_wakeup_snapshot_sync,
                url,
                req_json,
                timeout_sec,
            )
            if not raw_wav:
                return

            voice = getattr(tts, "voice", "raiden_mei") or "raiden_mei"
            wav_bytes = await asyncio.to_thread(_trim_wav_leading_breath, raw_wav)
            file_path = hello_mod.wakeup_words_config.generate_file_path(voice)
            await asyncio.to_thread(_write_bytes_atomic, file_path, wav_bytes)
            hello_mod.wakeup_words_config.update_wakeup_response(voice, file_path, result)
            await hello_mod.audio_to_data(file_path, use_cache=True)

    @functools.wraps(_orig_check_wakeup)
    async def _mei_aware_check_wakeup_words(conn, text, *args, **kwargs):
        tts = getattr(conn, "tts", None)
        # Wait briefly if TTS is still initializing (same behavior as upstream)
        if tts is None:
            start_t = time.time()
            while time.time() - start_t < 3:
                tts = getattr(conn, "tts", None)
                if tts is not None:
                    break
                await asyncio.sleep(0.1)

        if tts is None or not is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")):
            matched = await _orig_check_wakeup(conn, text, *args, **kwargs)
            if matched and tts is not None:
                prime_gsv_async(tts, reason="wakeup")
            return matched

        if not conn.config.get("enable_wakeup_words_response_cache"):
            return False

        _, filtered_text = hello_mod.remove_punctuation_and_length(text)
        if filtered_text not in (conn.config.get("wakeup_words") or []):
            return False

        # F-3: Trigger non-blocking GSV /prime on wakeup so memory pages warm up while user speaks
        prime_gsv_async(tts, reason="wakeup")
        conn.just_woken_up = True
        await hello_mod.send_tts_message(conn, "start")

        voice = getattr(tts, "voice", "raiden_mei") or "raiden_mei"
        response = hello_mod.wakeup_words_config.get_wakeup_response(voice)
        if not response or not response.get("file_path"):
            fallback_wav = (
                MEI_WAKEUP_FALLBACK_WAV
                if os.path.isfile(MEI_WAKEUP_FALLBACK_WAV)
                else "config/assets/wakeup_words_short.wav"
            )
            response = {
                "voice": voice,
                "file_path": fallback_wav,
                "time": 0,
                "text": MEI_WAKEUP_FALLBACK_TEXT,
            }
            if os.path.isfile(MEI_WAKEUP_FALLBACK_WAV):
                try:
                    hello_mod.wakeup_words_config.update_wakeup_response(
                        voice, MEI_WAKEUP_FALLBACK_WAV, MEI_WAKEUP_FALLBACK_TEXT
                    )
                except Exception:
                    pass

        opus_packets = await hello_mod.audio_to_data(response.get("file_path"), use_cache=True)
        conn.client_abort = False
        conn.sentence_id = str(uuid.uuid4().hex)

        prof = get_mei_emotion_profile("gentle")
        _push_esp32_emotion_async(conn, prof["esp32_emoji"], prof["esp32_emotion"])

        conn.dialogue.put(Message(role="assistant", content=response.get("text")))
        hello_mod._wakeup_logger.bind(tag=hello_mod.TAG).info(
            f"播放芽衣唤醒词回复: {response.get('text')}"
        )

        await hello_mod.sendAudioMessage(conn, SentenceType.FIRST, opus_packets, response.get("text"))
        asyncio.create_task(hello_mod._finish_wakeup_reply(conn))

        if time.time() - response.get("time", 0) > hello_mod.WAKEUP_CONFIG["refresh_time"]:
            if not hello_mod._wakeup_response_lock.locked() and not is_live_gsv_busy():
                asyncio.create_task(_mei_aware_wakeup_words_response(conn))
        return True

    @functools.wraps(_orig_preheat)
    async def _mei_aware_preheat(*args, **kwargs):
        await _orig_preheat(*args, **kwargs)
        try:
            resp = hello_mod.wakeup_words_config.get_wakeup_response("raiden_mei")
            fp = (resp or {}).get("file_path") or (
                MEI_WAKEUP_FALLBACK_WAV if os.path.isfile(MEI_WAKEUP_FALLBACK_WAV) else None
            )
            if fp and os.path.isfile(fp):
                await hello_mod.audio_to_data(fp, use_cache=True)
        except Exception:
            pass

    wk_mounted = not wk_failures
    lh_mounted = False
    if wk_failures:
        patch_registry.record_patch(
            "mei_wakeup_responses",
            "skipped_contract",
            target="helloHandle",
            detail="; ".join(wk_failures),
            contract_ok=False,
            category="experience",
        )
        print(
            f"[MeiEmotionRouter] 跳过挂载唤醒回复补丁（签名契约不匹配）: {'; '.join(wk_failures)}",
            flush=True,
        )
    else:
        hello_mod.wakeupWordsResponse = _mei_aware_wakeup_words_response
        hello_mod.checkWakeupWords = _mei_aware_check_wakeup_words
        hello_mod.preheat_wakeup_audio_cache = _mei_aware_preheat
        rebound_hello: list[str] = []
        if _orig_handle_hello is not None:
            @functools.wraps(_orig_handle_hello)
            async def _prime_aware_handle_hello(conn, msg_json, *args, **kwargs):
                prime_gsv_async(getattr(conn, "tts", None), reason="hello")
                return await _orig_handle_hello(conn, msg_json, *args, **kwargs)

            hello_mod.handleHelloMessage = _prime_aware_handle_hello
            rebound_hello = patch_registry.rebind(_orig_handle_hello, _prime_aware_handle_hello)
        hello_mod._mei_wakeup_patched = True

        # P1-12: Rebind ALL already-imported module references across sys.modules
        rebound_wk = (
            patch_registry.rebind(_orig_check_wakeup, _mei_aware_check_wakeup_words)
            + patch_registry.rebind(_orig_wakeup_response, _mei_aware_wakeup_words_response)
            + patch_registry.rebind(_orig_preheat, _mei_aware_preheat)
            + rebound_hello
        )
        patch_registry.record_patch(
            "mei_wakeup_responses",
            "ok",
            target="helloHandle",
            detail="ok",
            contract_ok=True,
            category="experience",
            rebound=rebound_wk,
        )

    # Patch ListenTextMessageHandler so offline interrupt words (e.g. "听我说") switch straight to
    # listening with ZERO greeting TTS and ZERO 2s just_woken_up VAD suppression (even from IDLE),
    # and publish listen_start to runtime_events (P1-11).
    try:
        import core.handle.textHandler.listenMessageHandler as listen_mod
        import core.handle.abortHandle as abort_mod

        cls = getattr(listen_mod, "ListenTextMessageHandler", None)
        if cls is not None and not getattr(cls, "_interrupt_word_patched", False):
            _orig_listen_handle = cls.handle
            ok_lh, det_lh = patch_registry.verify_signature_contract(
                _orig_listen_handle, required_params=("self", "conn", "msg_json"), require_async=True
            )

            @functools.wraps(_orig_listen_handle)
            async def _interrupt_aware_listen_handle(self, conn, msg_json, *args, **kwargs):
                if isinstance(msg_json, dict) and msg_json.get("state") == "start":
                    try:
                        runtime_events.publish_event(
                            "listen_start",
                            device_id=str(getattr(conn, "device_id", "") or ""),
                        )
                    except Exception:
                        pass
                if isinstance(msg_json, dict) and msg_json.get("state") == "detect" and "text" in msg_json:
                    original_text = str(msg_json.get("text") or "")
                    _, filtered_text = listen_mod.remove_punctuation_and_length(original_text)
                    cfg_words = (getattr(conn, "config", None) or {}).get("interrupt_words")
                    if not isinstance(cfg_words, (list, tuple, set)):
                        cfg_words = ("听我说",)
                    norm_words = {
                        listen_mod.remove_punctuation_and_length(str(w))[1]
                        for w in cfg_words
                        if str(w).strip()
                    }
                    if filtered_text and filtered_text in norm_words:
                        prime_gsv_async(getattr(conn, "tts", None), reason="interrupt")
                        conn.client_have_voice = False
                        conn.reset_audio_states()
                        conn.last_activity_time = time.time() * 1000
                        conn.just_woken_up = False
                        if (
                            getattr(conn, "client_is_speaking", False)
                            or getattr(conn, "_pc_audio_active_sid", None) is not None
                        ):
                            try:
                                await abort_mod.handleAbortMessage(conn)
                            except Exception:
                                pass
                            if hasattr(conn, "_pc_audio_active_sid"):
                                conn._pc_audio_active_sid = None
                        await listen_mod.send_stt_message(conn, original_text)
                        await hello_mod.send_tts_message(conn, "stop", None)
                        conn.client_is_speaking = False
                        conn.logger.bind(tag=listen_mod.TAG).info(
                            f"收到打断词「{original_text}」，已静音并直接进入聆听模式"
                        )
                        return
                return await _orig_listen_handle(self, conn, msg_json, *args, **kwargs)

            if not ok_lh:
                patch_registry.record_patch(
                    "listen_interrupt_handler",
                    "skipped_contract",
                    target="ListenTextMessageHandler.handle",
                    detail=det_lh,
                    contract_ok=False,
                    category="experience",
                )
                print(
                    f"[MeiEmotionRouter] 跳过挂载打断词补丁（签名契约不匹配）: {det_lh}",
                    flush=True,
                )
            else:
                cls.handle = _interrupt_aware_listen_handle
                cls._interrupt_word_patched = True
                lh_mounted = True
                patch_registry.record_patch(
                    "listen_interrupt_handler",
                    "ok",
                    target="ListenTextMessageHandler.handle",
                    detail=det_lh,
                    contract_ok=True,
                    category="experience",
                )
    except Exception as exc:
        patch_registry.record_patch(
            "listen_interrupt_handler",
            "failed",
            target="ListenTextMessageHandler.handle",
            detail=str(exc),
            contract_ok=False,
            category="experience",
        )
        print(f"[MeiEmotionRouter] 挂载打断词直通聆听补丁跳过: {exc}", flush=True)

    if wk_mounted or lh_mounted:
        mounted_parts = []
        if wk_mounted:
            mounted_parts.append("芽衣专属唤醒词秒回语 (raiden_mei 独立缓存 + gentle 温柔音色)")
        if lh_mounted:
            mounted_parts.append("打断词直通聆听补丁")
        print(f"[MeiEmotionRouter] 已挂载{' + '.join(mounted_parts)}", flush=True)


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MD_DECOR_RE = re.compile(r"[*`#>]+")


def select_idle_farewell_text(messages: list[str] | tuple[str, ...], last_text: str = "") -> str:
    """Pick a non-empty farewell line randomly while avoiding immediate repeats."""
    import random

    clean = [str(m or "").strip() for m in (messages or []) if str(m or "").strip()]
    if not clean:
        return ""
    pool = [m for m in clean if m != last_text] or clean
    return random.choice(pool)


def format_idle_farewell_for_tts(
    text: str,
    *,
    is_mei_voice: bool = False,
    default_emotion: str = "gentle",
) -> str:
    """Prefix Mei's warm default emotion tag when the line has no explicit emotion tag."""
    s = str(text or "").strip()
    if not s:
        return ""
    if is_mei_voice:
        emo, _ = extract_and_strip_emotion(s, flush_incomplete=True)
        if not emo:
            tag = default_emotion if default_emotion in ("gentle", "tender", "shy", "calm", "happy", "playful", "serious") else "gentle"
            return f"[{tag}] {s}"
    return s


def extract_session_dialogue_snippet(conn: Any, max_turns: int = 8) -> list[tuple[str, str]]:
    """Extract clean recent (role, text) turns from current session Dialogue without mutating it."""
    dlg_obj = getattr(conn, "dialogue", None)
    raw_list = getattr(dlg_obj, "dialogue", None)
    if not isinstance(raw_list, list) or not raw_list:
        return []

    turns: list[tuple[str, str]] = []
    for msg in raw_list:
        if getattr(msg, "is_temporary", False) or getattr(msg, "_standalone_assistant", False):
            continue
        role = str(getattr(msg, "role", "") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        if getattr(msg, "tool_calls", None):
            continue
        raw_content = getattr(msg, "content", None)
        if not isinstance(raw_content, str):
            continue
        cleaned = strip_emotion_tags(safe_clean_response_garbage(raw_content)).strip()
        if not cleaned:
            continue
        if len(cleaned) > 180:
            cleaned = cleaned[:180].rstrip() + "…"
        turns.append((role, cleaned))

    if not any(r == "user" for r, _ in turns):
        return []
    limit = max(2, int(max_turns) * 2)
    return turns[-limit:]


async def generate_llm_idle_farewell(
    conn: Any,
    summary_prompt: str,
    *,
    is_mei_voice: bool = False,
    timeout_sec: float = 6.0,
) -> str:
    """Bypass conn.dialogue/Anima to generate a concise warm farewell summarizing the current session."""
    llm = getattr(conn, "llm", None)
    client = getattr(llm, "client", None)
    model_name = getattr(llm, "model_name", None)
    if llm is None or client is None or not model_name:
        return ""

    turns = extract_session_dialogue_snippet(conn, max_turns=8)
    if not turns:
        return ""

    transcript_lines = [
        f"{'对方' if role == 'user' else '你'}：{txt}"
        for role, txt in turns
    ]
    transcript = "\n".join(transcript_lines)

    base_persona = str((getattr(conn, "config", None) or {}).get("prompt") or "").strip()
    if len(base_persona) > 1200:
        base_persona = base_persona[:1200]
    sys_parts = []
    if base_persona:
        sys_parts.append(base_persona)
    sys_parts.append(
        "【当前任务：空闲超时道别】这是本轮对话结束前的最后一句告别，严禁调用任何工具，"
        "严格遵循下方用户的字数与语气要求直接说出告别台词。"
    )
    if is_mei_voice:
        sys_parts.append("请在开头附上最贴合依恋温柔心境的一个英文方括号情绪标签（如 [tender]、[gentle] 或 [shy]）。")

    instr = str(summary_prompt or "").strip() or runtime_settings.DEFAULT_IDLE_FAREWELL_SUMMARY_PROMPT
    user_msg = (
        f"以下是我们刚才这段对话的记录：\n{transcript}\n\n"
        f"请按以下要求说一句告别的话：\n{instr}"
    )

    request_params: Dict[str, Any] = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "\n\n".join(sys_parts)},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "max_tokens": 160,
        "temperature": 0.85,
        "timeout": float(timeout_sec),
    }
    apply_think = getattr(llm, "_apply_thinking_disabled", None)
    if callable(apply_think):
        try:
            apply_think(request_params)
        except Exception:
            pass

    def _call_sync() -> str:
        resp = client.chat.completions.create(**request_params)
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return ""
        msg = getattr(choices[0], "message", None)
        content = getattr(msg, "content", "") if msg is not None else ""
        return str(content or "")

    try:
        raw_out = await asyncio.wait_for(
            asyncio.to_thread(_call_sync),
            timeout=float(timeout_sec) + 1.5,
        )
    except Exception:
        return ""

    cleaned = _THINK_BLOCK_RE.sub("", raw_out)
    cleaned = safe_clean_response_garbage(cleaned)
    cleaned = _MD_DECOR_RE.sub("", cleaned)
    cleaned = " ".join(ln.strip() for ln in cleaned.splitlines() if ln.strip()).strip()
    if len(cleaned) > 150:
        # Trim gracefully at a sentence boundary if LLM over-generated
        cut_idx = max(cleaned.rfind("。", 0, 145), cleaned.rfind("！", 0, 145), cleaned.rfind("？", 0, 145))
        cleaned = cleaned[: cut_idx + 1] if cut_idx >= 15 else cleaned[:145].rstrip()
    return cleaned


def apply_idle_farewell_patch() -> None:
    """Replace upstream idle-timeout prompt with customizable fixed or zero-pollution LLM summary TTS farewell."""
    import uuid
    import core.handle.receiveAudioHandle as recv_mod
    import core.handle.sendAudioHandle as send_audio_mod
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO

    if getattr(recv_mod, "_idle_farewell_patched", False):
        if patch_registry.get_patch_status("idle_farewell") is None:
            patch_registry.record_patch(
                "idle_farewell",
                "ok",
                target="receiveAudioHandle.no_voice_close_connect",
                detail="already applied",
            )
        return

    _orig_no_voice_close = recv_mod.no_voice_close_connect
    ok_sig, det_sig = patch_registry.verify_signature_contract(
        _orig_no_voice_close,
        required_params=("conn", "have_voice"),
        require_async=True,
    )
    if not ok_sig:
        patch_registry.record_patch(
            "idle_farewell",
            "skipped_contract",
            target="receiveAudioHandle.no_voice_close_connect",
            detail=det_sig,
            contract_ok=False,
            category="experience",
        )
        print(f"[MeiEmotionRouter] 跳过挂载空闲告别补丁（签名契约不匹配）: {det_sig}", flush=True)
        return
    _last_farewell = {"text": ""}

    async def _speak_or_close(conn: Any, chosen: str, *, timeout_sec: int, default_emotion: str, reason: str) -> None:
        conn.close_after_chat = True
        conn.client_abort = False

        if not chosen:
            conn.logger.bind(tag=recv_mod.TAG).info(
                f"无声超时 {timeout_sec}s（{reason}），无有效告别台词，静默断开连接"
            )
            await conn.close()
            return

        tts = getattr(conn, "tts", None)
        tts_q = getattr(tts, "tts_text_queue", None)
        if tts is None or tts_q is None:
            conn.logger.bind(tag=recv_mod.TAG).info("无声超时但 TTS 通道未就绪，直接断开连接")
            await conn.close()
            return

        mei_active = is_raiden_mei_voice(getattr(tts, "ref_audio_path", ""))
        tts_payload_text = format_idle_farewell_for_tts(
            chosen,
            is_mei_voice=mei_active,
            default_emotion=default_emotion,
        )
        _, stripped_spoken = extract_and_strip_emotion(tts_payload_text, flush_incomplete=True)
        if not clean_segment_for_tts(stripped_spoken):
            conn.logger.bind(tag=recv_mod.TAG).info(
                f"无声超时 {timeout_sec}s，告别语无有效朗读内容，静默断开连接"
            )
            await conn.close()
            return

        sid = uuid.uuid4().hex
        conn.sentence_id = sid
        conn.client_is_speaking = True
        conn.logger.bind(tag=recv_mod.TAG).info(
            f"无声超时 {timeout_sec}s（{reason}），直接播报告别语并结束对话: {strip_emotion_tags(chosen)}"
        )
        await send_audio_mod.send_tts_message(conn, "start")
        tts_q.put(
            TTSMessageDTO(
                sentence_id=sid,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
        tts_q.put(
            TTSMessageDTO(
                sentence_id=sid,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail=tts_payload_text,
            )
        )
        tts_q.put(
            TTSMessageDTO(
                sentence_id=sid,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )

    @functools.wraps(_orig_no_voice_close)
    async def _custom_no_voice_close_connect(conn, have_voice, *args, **kwargs):
        now_ms = time.time() * 1000.0
        if have_voice:
            conn.last_activity_time = now_ms
            if getattr(conn, "_idle_farewell_inflight", False):
                conn._idle_farewell_cancelled = True
            return

        # Never trigger idle timeout while TTS / PC audio playback is actively speaking
        if (
            getattr(conn, "client_is_speaking", False)
            or getattr(conn, "_pc_audio_active_sid", None) is not None
        ):
            conn.last_activity_time = now_ms
            return

        farewell_cfg = runtime_settings.get_idle_farewell()
        timeout_sec = int(farewell_cfg.get("timeout_seconds", 120))
        desired_hard_timeout = max(60, timeout_sec + 60)
        if getattr(conn, "timeout_seconds", None) != desired_hard_timeout:
            conn.timeout_seconds = desired_hard_timeout

        if getattr(conn, "last_activity_time", 0.0) <= 0.0:
            return
        if getattr(conn, "close_after_chat", False) or getattr(conn, "_idle_farewell_inflight", False):
            return

        no_voice_ms = now_ms - float(conn.last_activity_time)
        if no_voice_ms <= 1000.0 * timeout_sec:
            return

        if not farewell_cfg.get("enabled", True):
            conn.close_after_chat = True
            conn.client_abort = False
            conn.logger.bind(tag=recv_mod.TAG).info(
                f"无声超时 {timeout_sec}s，已关闭超时告别语，静默断开连接"
            )
            await conn.close()
            return

        mode = str(farewell_cfg.get("mode") or "fixed").strip().lower()
        fallback_messages = farewell_cfg.get("messages") or []

        if mode == "llm_summary":
            tts = getattr(conn, "tts", None)
            mei_active = is_raiden_mei_voice(getattr(tts, "ref_audio_path", "")) if tts is not None else False
            has_turns = bool(extract_session_dialogue_snippet(conn, max_turns=1))
            if has_turns and getattr(conn, "llm", None) is not None:
                trigger_activity_ts = float(conn.last_activity_time)
                summary_prompt = str(farewell_cfg.get("summary_prompt") or "")
                conn._idle_farewell_inflight = True
                conn._idle_farewell_cancelled = False

                async def _run_llm_farewell_task():
                    try:
                        generated = await generate_llm_idle_farewell(
                            conn,
                            summary_prompt,
                            is_mei_voice=mei_active,
                            timeout_sec=6.0,
                        )
                        stop_ev = getattr(conn, "stop_event", None)
                        if (
                            getattr(conn, "_idle_farewell_cancelled", False)
                            or getattr(conn, "client_abort", False)
                            or getattr(conn, "close_after_chat", False)
                            or getattr(conn, "client_is_speaking", False)
                            or (stop_ev is not None and stop_ev.is_set())
                            or float(getattr(conn, "last_activity_time", 0.0)) > trigger_activity_ts
                        ):
                            conn.logger.bind(tag=recv_mod.TAG).info(
                                "生成超时告别语期间检测到用户重新活跃，已自动取消挂断"
                            )
                            return

                        if generated:
                            await _speak_or_close(
                                conn,
                                generated,
                                timeout_sec=timeout_sec,
                                default_emotion="tender",
                                reason="AI总结依恋告别",
                            )
                        else:
                            fb = select_idle_farewell_text(fallback_messages, last_text=_last_farewell["text"])
                            if fb:
                                _last_farewell["text"] = fb
                            await _speak_or_close(
                                conn,
                                fb,
                                timeout_sec=timeout_sec,
                                default_emotion="gentle",
                                reason="AI生成超时降级固定台词",
                            )
                    except Exception as exc:
                        conn.logger.bind(tag=recv_mod.TAG).warning(
                            f"AI 总结告别任务异常，尝试安全收尾: {exc}"
                        )
                        if not getattr(conn, "_idle_farewell_cancelled", False) and not getattr(
                            conn, "close_after_chat", False
                        ):
                            fb = select_idle_farewell_text(fallback_messages, last_text=_last_farewell["text"])
                            await _speak_or_close(
                                conn,
                                fb,
                                timeout_sec=timeout_sec,
                                default_emotion="gentle",
                                reason="异常兜底告别",
                            )
                    finally:
                        conn._idle_farewell_inflight = False

                conn._idle_farewell_task = asyncio.create_task(_run_llm_farewell_task())
                return

        chosen = select_idle_farewell_text(fallback_messages, last_text=_last_farewell["text"])
        if chosen:
            _last_farewell["text"] = chosen
        await _speak_or_close(
            conn,
            chosen,
            timeout_sec=timeout_sec,
            default_emotion="gentle",
            reason="无本轮对话降级固定台词" if mode == "llm_summary" else "固定随机台词",
        )

    recv_mod.no_voice_close_connect = _custom_no_voice_close_connect
    recv_mod._idle_farewell_patched = True
    rebound = patch_registry.rebind(_orig_no_voice_close, _custom_no_voice_close_connect)
    patch_registry.record_patch(
        "idle_farewell",
        "ok",
        target="receiveAudioHandle.no_voice_close_connect",
        detail=det_sig,
        contract_ok=True,
        category="experience",
        rebound=rebound,
    )
    print("[MeiEmotionRouter] 已挂载自定义空闲超时告别语补丁（固定随机 / AI总结依恋双模式 · 零记忆污染 · 18080 免重启热改）", flush=True)

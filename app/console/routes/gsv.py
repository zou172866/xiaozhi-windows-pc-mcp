"""GSV/Edge voice profiles, GPU weight switching, TTS preview & PC audio chime routes (H-1, D-3/D-7)."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import io
import json
import math
import os
import struct
import threading
import time
from urllib.parse import quote
import wave

import httpx
import requests
import yaml
try:
    import edge_tts
except ImportError:
    edge_tts = None

import mei_emotion_router
import pc_audio_router
from console import shared
from console.routes import audio_api

_PORTABLE_MEI_GPT_WEIGHTS = f"{shared.HOST_GSV_WEIGHTS_DIR}/raiden_mei/v4/gpt.ckpt"
_PORTABLE_MEI_SOVITS_WEIGHTS = f"{shared.HOST_GSV_WEIGHTS_DIR}/raiden_mei/v4/sovits.pth"
_PORTABLE_MEI_VERSION = "v4"

_DEFAULT_MEI_GPT_WEIGHTS = (
    os.environ.get("GSV_MEI_GPT_WEIGHTS", "").strip()
    or _PORTABLE_MEI_GPT_WEIGHTS
)
_DEFAULT_MEI_SOVITS_WEIGHTS = (
    os.environ.get("GSV_MEI_SOVITS_WEIGHTS", "").strip()
    or _PORTABLE_MEI_SOVITS_WEIGHTS
)
_DEFAULT_MEI_VERSION = os.environ.get("GSV_MEI_VERSION", "").strip() or _PORTABLE_MEI_VERSION

GSV_PROFILES = {
    "__gsv_raiden_mei__": {
        "version": _DEFAULT_MEI_VERSION,
        "gpt_weights": _DEFAULT_MEI_GPT_WEIGHTS,
        "sovits_weights": _DEFAULT_MEI_SOVITS_WEIGHTS,
        "block": {
            "type": "gpt_sovits_v2",
            "voice_id": "__gsv_raiden_mei__",
            "url": f"{shared.GSV_Host}/tts",
            "output_dir": "tmp/",
            "text_lang": "zh",
            "ref_audio_path": "${HOST_VOICES_DIR}/raiden_mei/mei_default_ref.wav",
            "aux_ref_audio_paths": [],
            "prompt_text": "今夜月色很美，呵...适合与朋友小聚一番。",
            "prompt_lang": "zh",
            "top_k": 12,
            "top_p": 1,
            "temperature": 0.98,
            "text_split_method": "cut0",
            "batch_size": 1,
            "return_fragment": False,
            "speed_factor": 1.0,
            "streaming_mode": False,
            "seed": -1,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
            "sample_steps": 16,
        },
    },
    "__gsv_xiaozhi_tw__": {
        "version": "v2ProPlus",
        "gpt_weights": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
        "sovits_weights": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth",
        "block": {
            "type": "gpt_sovits_v2",
            "voice_id": "__gsv_xiaozhi_tw__",
            "url": f"{shared.GSV_Host}/tts",
            "output_dir": "tmp/",
            "text_lang": "zh",
            "ref_audio_path": "${HOST_VOICES_DIR}/xiaozhi_taiwan_ref.wav",
            "aux_ref_audio_paths": ["${HOST_VOICES_DIR}/xiaozhi_taiwan_aux.wav"],
            "prompt_text": "哈啰啊，我是小智啦，声音好听的台湾女孩一枚，超开心认识你耶，最近在忙啥？",
            "prompt_lang": "zh",
            "top_k": 12,
            "top_p": 1,
            "temperature": 0.98,
            "text_split_method": "cut0",
            "batch_size": 1,
            "return_fragment": False,
            "speed_factor": 1.04,
            "streaming_mode": False,
            "seed": -1,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
        },
    },
}

ALLOWED_GSV_VOICES = frozenset(GSV_PROFILES.keys())
ALLOWED_EDGE_VOICES = frozenset(
    {
        "zh-TW-HsiaoChenNeural",
        "zh-TW-YunJheNeural",
        "zh-CN-XiaoxiaoNeural",
        "zh-CN-XiaoyiNeural",
        "zh-CN-YunxiNeural",
        "zh-CN-YunjianNeural",
        "zh-CN-YunxiaNeural",
        "zh-HK-HiuGaaiNeural",
        "zh-CN-liaoning-XiaobeiNeural",
        "zh-CN-shaanxi-XiaoniNeural",
    }
)
ALLOWED_VOICES = ALLOWED_GSV_VOICES | ALLOWED_EDGE_VOICES
# Share the exact same RLock with live ESP32 GPT-SoVITS synthesis (P1-14)
_gsv_weights_lock = mei_emotion_router.GSV_GPU_LOCK
_active_gsv_loaded = None
_gsv_op_deadline_tls = threading.local()
_GSV_PREVIEW_LOCK_MAX_SECONDS = 45.0
EDGE_PREVIEW_TIMEOUT_SECONDS = 15.0
_voice_dirty_lock = threading.Lock()
_voice_dirty_target: str | None = None
_voice_dirty_error: str = ""


def _set_voice_dirty(target_voice: str | None, error_msg: str = "") -> None:
    global _voice_dirty_target, _voice_dirty_error
    with _voice_dirty_lock:
        _voice_dirty_target = target_voice
        _voice_dirty_error = shared._redact_secrets(error_msg) if error_msg else ""


def _get_voice_dirty_state() -> tuple[str | None, str]:
    with _voice_dirty_lock:
        return _voice_dirty_target, _voice_dirty_error


def _gsv_timeout_within_budget(default_timeout: float) -> float:
    deadline = getattr(_gsv_op_deadline_tls, "deadline", None)
    if deadline is None:
        return float(default_timeout)
    rem = float(deadline) - time.monotonic()
    if rem <= 0:
        raise TimeoutError("GPU 锁持有时间已达到上限，中止试听以保护线上语音播报")
    return min(float(default_timeout), rem)


_preserved_boot_mei_weights: dict[str, tuple[str, str, str]] = {}
_MEI_WEIGHTS_BACKUP_FILENAME = ".gsv_mei_custom_weights.json"


def _norm_weight_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/")


def _mei_weights_backup_path(yaml_path: str | None) -> str:
    if not yaml_path:
        return ""
    parent = os.path.dirname(os.path.abspath(yaml_path))
    return os.path.join(parent, _MEI_WEIGHTS_BACKUP_FILENAME) if parent else ""


def _read_preserved_mei_backup(yaml_path: str | None) -> tuple[str, str, str] | None:
    bpath = _mei_weights_backup_path(yaml_path)
    if not bpath or not os.path.isfile(bpath):
        return None
    try:
        with open(bpath, "r", encoding="utf-8") as rf:
            data = json.load(rf)
        if not isinstance(data, dict):
            return None
        ver = str(data.get("version") or "").strip() or _PORTABLE_MEI_VERSION
        t2s = str(data.get("t2s_weights_path") or "").strip()
        vits = str(data.get("vits_weights_path") or "").strip()
        if _is_custom_mei_weights(t2s, vits):
            return (ver, t2s, vits)
    except Exception as exc:
        shared._log_rate_limited("read_mei_weights_backup", exc)
    return None


def _persist_preserved_mei_backup(yaml_path: str | None, preserved: tuple[str, str, str]) -> None:
    bpath = _mei_weights_backup_path(yaml_path)
    if not bpath:
        return
    ver, t2s, vits = preserved
    if not _is_custom_mei_weights(t2s, vits):
        return
    try:
        payload = json.dumps(
            {
                "version": ver or _PORTABLE_MEI_VERSION,
                "t2s_weights_path": t2s,
                "vits_weights_path": vits,
            },
            ensure_ascii=False,
            indent=2,
        )
        shared._atomic_write_text(bpath, payload + "\n")
    except Exception as exc:
        shared._log_rate_limited("persist_mei_weights_backup", exc)


def _read_boot_gsv_custom() -> tuple[str, str, str] | None:
    """Read (version, t2s_weights_path, vits_weights_path) from shared.GSV_INFER_YAML if present."""
    yaml_path = shared.GSV_INFER_YAML
    if not yaml_path or not os.path.exists(yaml_path):
        return None
    try:
        with open(yaml_path, "r", encoding="utf-8") as rf:
            boot_cfg = yaml.safe_load(rf) or {}
        custom = boot_cfg.get("custom") or {}
        if not isinstance(custom, dict):
            return None
        ver = str(custom.get("version") or "").strip()
        t2s = str(custom.get("t2s_weights_path") or "").strip()
        vits = str(custom.get("vits_weights_path") or "").strip()
        if t2s or vits:
            return (ver, t2s, vits)
    except Exception as exc:
        shared._log_rate_limited("detect_boot_gsv_voice", exc)
    return None


def _is_custom_mei_weights(t2s: str, vits: str) -> bool:
    """Return True if a boot YAML weight pair is a user's custom fine-tuned raiden_mei / local model."""
    if not t2s or not vits:
        return False
    low_t2s = _norm_weight_path(t2s).lower()
    low_vits = _norm_weight_path(vits).lower()
    tw_prof = GSV_PROFILES["__gsv_xiaozhi_tw__"]
    if (
        low_t2s == _norm_weight_path(tw_prof["gpt_weights"]).lower()
        and low_vits == _norm_weight_path(tw_prof["sovits_weights"]).lower()
    ):
        return False
    if "raiden_mei" in low_t2s or "raiden_mei" in low_vits:
        return True
    # Preserve any user-customized local weight pair (including half-pretrained fine-tuned GPT or SoVITS)
    if (
        not low_t2s.startswith("gpt_sovits/pretrained_models/")
        or not low_vits.startswith("gpt_sovits/pretrained_models/")
    ):
        return True
    return False


def _snapshot_boot_mei_weights(*, persist_backup: bool = False) -> tuple[str, str, str] | None:
    """Remember custom fine-tuned weights from shared.GSV_INFER_YAML before any voice switch overwrites it."""
    yaml_path = shared.GSV_INFER_YAML
    boot = _read_boot_gsv_custom()
    if boot is not None and _is_custom_mei_weights(boot[1], boot[2]):
        preserved = (boot[0] or _PORTABLE_MEI_VERSION, boot[1], boot[2])
        if yaml_path:
            _preserved_boot_mei_weights[yaml_path] = preserved
            if persist_backup:
                _persist_preserved_mei_backup(yaml_path, preserved)
        return preserved
    if yaml_path and yaml_path in _preserved_boot_mei_weights:
        return _preserved_boot_mei_weights[yaml_path]
    disk_preserved = _read_preserved_mei_backup(yaml_path)
    if disk_preserved is not None and yaml_path:
        _preserved_boot_mei_weights[yaml_path] = disk_preserved
        return disk_preserved
    return None


def _resolve_gsv_weights(voice_id: str) -> tuple[str, str, str]:
    """Resolve effective (version, gpt_weights, sovits_weights) for a GSV voice profile.

    Priority for `__gsv_raiden_mei__`:
      1. Explicit `GSV_MEI_GPT_WEIGHTS` / `GSV_MEI_SOVITS_WEIGHTS` / `GSV_MEI_VERSION` env overrides
      2. Existing custom fine-tuned weights from `tts_infer_xiaozhi.yaml` (preserved across switches & restarts)
      3. Portable default profile weights under `HOST_GSV_WEIGHTS_DIR/raiden_mei/v4`
    """
    if voice_id != "__gsv_raiden_mei__":
        prof = GSV_PROFILES[voice_id]
        return str(prof["version"]), str(prof["gpt_weights"]), str(prof["sovits_weights"])

    version = _PORTABLE_MEI_VERSION
    gpt_w = _PORTABLE_MEI_GPT_WEIGHTS
    sovits_w = _PORTABLE_MEI_SOVITS_WEIGHTS

    preserved = _snapshot_boot_mei_weights()
    if preserved is not None:
        version, gpt_w, sovits_w = preserved

    env_ver = os.environ.get("GSV_MEI_VERSION", "").strip()
    env_gpt = os.environ.get("GSV_MEI_GPT_WEIGHTS", "").strip()
    env_sov = os.environ.get("GSV_MEI_SOVITS_WEIGHTS", "").strip()
    if env_ver:
        version = env_ver
    if env_gpt:
        gpt_w = env_gpt
    if env_sov:
        sovits_w = env_sov
    return version, gpt_w, sovits_w


def _detect_boot_gsv_voice() -> str | None:
    boot = _read_boot_gsv_custom()
    if boot is None:
        return None
    _ver, t2s, vits = boot
    norm_t2s = _norm_weight_path(t2s)
    norm_vits = _norm_weight_path(vits)
    for vid in GSV_PROFILES:
        _, prof_gpt, prof_sov = _resolve_gsv_weights(vid)
        if _norm_weight_path(prof_gpt) == norm_t2s and (not norm_vits or _norm_weight_path(prof_sov) == norm_vits):
            return vid
    return None


def _resolve_gsv_voice_id(tts_cfg: dict | None) -> str:
    """Resolve GSV voice ID from explicit persistent `voice_id` first, falling back to legacy ref_audio_path (D-7)."""
    cfg_d = tts_cfg if isinstance(tts_cfg, dict) else {}
    explicit = str(cfg_d.get("voice_id") or "").strip()
    if explicit in GSV_PROFILES:
        return explicit
    # Legacy fallback ONLY for older configs lacking explicit voice_id
    ref_p = str(cfg_d.get("ref_audio_path") or "")
    return "__gsv_raiden_mei__" if "raiden_mei" in ref_p else "__gsv_xiaozhi_tw__"


def _get_configured_gsv_voice() -> str | None:
    try:
        cfg = shared._read_raw_yaml()
        selected = cfg.get("selected_module") or {}
        if selected.get("TTS") != "GPT_SOVITS_V2":
            return None
        return _get_current_voice_id(cfg)
    except Exception as exc:
        shared._log_rate_limited("get_configured_gsv_voice", exc)
        return None


def _ensure_gsv_weights(voice_id: str, persist_boot_yaml: bool = False):
    global _active_gsv_loaded
    prof = GSV_PROFILES.get(voice_id)
    if not prof:
        return
    with _gsv_weights_lock:
        if _active_gsv_loaded is None:
            _active_gsv_loaded = _detect_boot_gsv_voice()
        _snapshot_boot_mei_weights(persist_backup=bool(persist_boot_yaml and voice_id != "__gsv_raiden_mei__"))
        version, gpt_weights, sovits_weights = _resolve_gsv_weights(voice_id)
        if _active_gsv_loaded != voice_id:
            client = shared._get_console_http_client()
            t_gpt = _gsv_timeout_within_budget(25.0)
            r1 = client.get(f"{shared.GSV_Host}/set_gpt_weights", params={"weights_path": gpt_weights}, timeout=t_gpt)
            if r1.status_code != 200:
                raise RuntimeError(
                    f"切换 GPT 权重失败 (HTTP {r1.status_code}): {r1.text[:160]}；"
                    "若未放入本地自定义权重，请在 .env 配置 GSV_MEI_GPT_WEIGHTS 或切换为 EdgeTTS / 云端 TTS"
                )
            # GPT weights are now mutated on GPU: mark state dirty (distinct from None) so a SoVITS
            # failure forces restoration instead of falling back to _detect_boot_gsv_voice().
            _active_gsv_loaded = "__dirty__"
            t_sov = _gsv_timeout_within_budget(30.0)
            r2 = client.get(f"{shared.GSV_Host}/set_sovits_weights", params={"weights_path": sovits_weights}, timeout=t_sov)
            if r2.status_code != 200:
                raise RuntimeError(
                    f"切换 SoVITS 权重失败 (HTTP {r2.status_code}): {r2.text[:160]}；"
                    "若未放入本地自定义权重，请在 .env 配置 GSV_MEI_SOVITS_WEIGHTS 或切换为 EdgeTTS / 云端 TTS"
                )
            _active_gsv_loaded = voice_id
        if persist_boot_yaml:
            boot_cfg = {}
            if os.path.exists(shared.GSV_INFER_YAML):
                try:
                    with open(shared.GSV_INFER_YAML, "r", encoding="utf-8") as rf:
                        boot_cfg = yaml.safe_load(rf) or {}
                except Exception as exc:
                    shared._log_rate_limited("read_gsv_infer_yaml", exc)
                    boot_cfg = {}
            prev_custom = boot_cfg.get("custom") if isinstance(boot_cfg.get("custom"), dict) else {}
            boot_cfg["custom"] = {
                "bert_base_path": prev_custom.get("bert_base_path")
                or "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
                "cnhuhbert_base_path": prev_custom.get("cnhuhbert_base_path")
                or "GPT_SoVITS/pretrained_models/chinese-hubert-base",
                "device": prev_custom.get("device") or "cuda",
                "is_half": prev_custom["is_half"] if "is_half" in prev_custom else True,
                "t2s_weights_path": gpt_weights,
                "version": version,
                "vits_weights_path": sovits_weights,
            }
            shared._atomic_write_text(shared.GSV_INFER_YAML, yaml.safe_dump(boot_cfg, allow_unicode=True, sort_keys=False))


def _get_current_voice_id(cfg: dict) -> str:
    selected = (cfg or {}).get("selected_module") or {}
    tts_key = selected.get("TTS", "EdgeTTS")
    tts_cfg = ((cfg or {}).get("TTS") or {}).get(tts_key) or {}
    if tts_key == "GPT_SOVITS_V2":
        return _resolve_gsv_voice_id(tts_cfg)
    if tts_key == "EdgeTTS":
        return str(tts_cfg.get("voice_id") or tts_cfg.get("voice") or "zh-CN-XiaoxiaoNeural")
    if tts_key == "HuoshanDoubleStreamTTS":
        return str(tts_cfg.get("speaker") or "zh_female_wanwanxiaohe_moon_bigtts")
    if tts_key == "FishSpeech":
        return str(tts_cfg.get("reference_id") or "")
    return str(tts_cfg.get("voice_id") or tts_cfg.get("voice") or "")


_GSV_VOICE_IDENTITY_KEYS = (
    "voice_id",
    "ref_audio_path",
    "aux_ref_audio_paths",
    "prompt_text",
    "prompt_lang",
    "speed_factor",
    "top_k",
    "top_p",
    "temperature",
    "text_split_method",
)


def _generate_test_chime_wav(duration_sec: float = 0.4, sample_rate: int = 48000) -> bytes:
    """Generate a clean 0.4s 48kHz mono 16-bit sine chime WAV in memory as audio-test fallback."""
    n_samples = max(1, int(sample_rate * duration_sec))
    fade_in = max(1, int(sample_rate * 0.02))
    fade_out = max(1, int(sample_rate * 0.06))
    half = n_samples // 2
    amp = 0.28 * 32767.0
    frames = bytearray()
    phase = 0.0
    two_pi_over_sr = 2.0 * math.pi / float(sample_rate)
    for i in range(n_samples):
        freq = 587.33 if i < half else 880.0
        env = 1.0
        if i < fade_in:
            env = 0.5 * (1.0 - math.cos(math.pi * i / fade_in))
        elif i >= n_samples - fade_out:
            rem = n_samples - 1 - i
            env = 0.5 * (1.0 - math.cos(math.pi * rem / fade_out))
        sample = int(max(-32767, min(32767, round(amp * env * math.sin(phase)))))
        phase += two_pi_over_sr * freq
        frames.extend(struct.pack("<h", sample))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


async def _synthesize_mp3(text: str, voice: str) -> bytes:
    comm = edge_tts.Communicate(text=text, voice=voice)
    chunks = bytearray()
    async for item in comm.stream():
        if item.get("type") == "audio":
            chunks.extend(item["data"])
    return bytes(chunks)


async def _synthesize_mp3_with_timeout(
    text: str, voice: str, timeout: float = EDGE_PREVIEW_TIMEOUT_SECONDS
) -> bytes:
    return await asyncio.wait_for(_synthesize_mp3(text=text, voice=voice), timeout=float(timeout))



def handle_post_audio_test(handler) -> None:
    try:
        body = handler._read_body_json()
        vol = body.get("volume", None)
        if vol is not None:
            if isinstance(vol, bool) or not isinstance(vol, (int, float)):
                handler._send_json({"ok": False, "error": "字段 volume 必须为 0.0~1.0 的数字"}, HTTPStatus.BAD_REQUEST)
                return
            vol_f = float(vol)
            if not math.isfinite(vol_f) or vol_f < 0.0 or vol_f > 1.0:
                handler._send_json({"ok": False, "error": "字段 volume 超出范围（允许 0.0~1.0）"}, HTTPStatus.BAD_REQUEST)
                return
            vol = vol_f
        ref_wav = os.path.join(shared.VOICES_DIR, "raiden_mei", "mei_gentle_ref.wav")
        if not os.path.isfile(ref_wav):
            ref_wav = os.path.join(shared.VOICES_DIR, "wakeup_words_short.wav")
        if not os.path.isfile(ref_wav):
            ref_wav = os.path.join(shared.DATA_DIR, "wakeup_words_short.wav")
        if os.path.isfile(ref_wav):
            with open(ref_wav, "rb") as f:
                wav_bytes = f.read()
        else:
            wav_bytes = _generate_test_chime_wav(0.4)
        ok, detail = pc_audio_router.play_wav_on_pc_sync(wav_bytes, volume=vol)
        if ok:
            handler._send_json({"ok": True})
        else:
            handler._send_json({"ok": False, "error": f"电脑音响桥接不可用: {detail}"}, HTTPStatus.BAD_GATEWAY)
    except shared._BadRequestError:
        raise
    except Exception as e:
        handler._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def handle_post_tts_preview(handler) -> None:
    try:
        body = handler._read_body_json()
        raw_tts_provider = body.get("tts_provider")
        if raw_tts_provider is not None and not isinstance(raw_tts_provider, str):
            handler._send_json({"ok": False, "error": "字段 tts_provider 必须为字符串"}, HTTPStatus.BAD_REQUEST)
            return
        tts_provider = (raw_tts_provider or "").strip() if raw_tts_provider is not None else ""

        raw_text = str(body.get("text") or "你好呀，我是小智，今天过得怎么样？有什么需要我帮忙的随时告诉我。").strip()
        req_emotion = str(body.get("emotion") or "auto").strip().lower()
        tag_emo, stripped_text = mei_emotion_router.extract_and_strip_emotion(
            raw_text, flush_incomplete=True
        )
        clean_text = mei_emotion_router.clean_segment_for_tts(stripped_text) or "你好呀，我是小智。"

        if tts_provider and tts_provider not in ("GPT_SOVITS_V2", "EdgeTTS"):
            if tts_provider not in audio_api.CLOUD_TTS_PROVIDERS:
                handler._send_json(
                    {"ok": False, "error": f"无效的试听 TTS 引擎: {tts_provider}"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            raw_tts_cfg = body.get("tts_config")
            if raw_tts_cfg is not None and not isinstance(raw_tts_cfg, dict):
                handler._send_json({"ok": False, "error": "字段 tts_config 必须为 JSON 对象"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                audio_bytes, content_type = audio_api.synthesize_cloud_tts_preview(
                    tts_provider,
                    clean_text[:120],
                    tts_config_override=raw_tts_cfg,
                )
            except ValueError as ve:
                handler._send_json({"ok": False, "error": shared._redact_secrets(str(ve))}, HTTPStatus.BAD_REQUEST)
                return
            except (asyncio.TimeoutError, TimeoutError, requests.exceptions.Timeout, httpx.TimeoutException):
                handler._send_json(
                    {
                        "ok": False,
                        "error": f"云端 TTS 试听超时（超过 {int(audio_api.CLOUD_PREVIEW_TIMEOUT_SECONDS)} 秒），请检查网络或代理设置。",
                    },
                    HTTPStatus.GATEWAY_TIMEOUT,
                )
                return
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", str(len(audio_bytes)))
            handler.end_headers()
            handler.wfile.write(audio_bytes)
            return

        raw_voice = body.get("voice", "zh-CN-XiaoxiaoNeural")
        if raw_voice is not None and not isinstance(raw_voice, str):
            handler._send_json({"ok": False, "error": "字段 voice 必须为字符串"}, HTTPStatus.BAD_REQUEST)
            return
        voice = (raw_voice or "zh-CN-XiaoxiaoNeural").strip()
        if voice not in ALLOWED_VOICES:
            handler._send_json(
                {"ok": False, "error": f"无效的试听音色 ID: {voice or '(空)'}"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if voice in GSV_PROFILES:
            configured_gsv = _get_configured_gsv_voice()
            needs_weight_swap = bool(configured_gsv and configured_gsv != voice)
            if needs_weight_swap and shared._is_device_busy():
                handler._send_json(
                    {
                        "ok": False,
                        "busy": True,
                        "error": "ESP32 设备正在语音播报或合成中，为避免抢占 GPU 显存权重，已拒绝试听非当前音色，请稍后再试。",
                    },
                    HTTPStatus.CONFLICT,
                )
                return

            lock_timeout = 0.25 if needs_weight_swap else 10.0
            acquired = _gsv_weights_lock.acquire(timeout=lock_timeout)
            if not acquired:
                handler._send_json(
                    {
                        "ok": False,
                        "busy": True,
                        "error": "本地 GPU 语音合成引擎正忙，请等待当前播报结束后再试听。",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            rollback_warning = ""
            total_cap = max(0.01, float(_GSV_PREVIEW_LOCK_MAX_SECONDS))
            lock_deadline = time.monotonic() + total_cap
            # P1-5: Re-read active configured GSV voice AFTER acquiring _gsv_weights_lock
            # to close the TOCTOU window against concurrent POST /api/config voice switches.
            configured_gsv = _get_configured_gsv_voice()
            needs_weight_swap = bool(configured_gsv and configured_gsv != voice)
            rollback_reserve = min(12.0, total_cap * 0.30) if needs_weight_swap else 0.0
            _gsv_op_deadline_tls.deadline = lock_deadline - rollback_reserve
            try:
                if needs_weight_swap and shared._is_device_busy():
                    handler._send_json(
                        {
                            "ok": False,
                            "busy": True,
                            "error": "ESP32 设备正在语音播报或合成中，已取消非当前音色试听。",
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                try:
                    _ensure_gsv_weights(voice, persist_boot_yaml=False)
                    req_payload = dict(GSV_PROFILES[voice]["block"])
                    if isinstance(req_payload.get("ref_audio_path"), str):
                        req_payload["ref_audio_path"] = req_payload["ref_audio_path"].replace(
                            "${HOST_VOICES_DIR}", shared.HOST_VOICES_DIR
                        ).replace("${HOST_DATA_DIR}", shared.HOST_DATA_DIR)
                    if isinstance(req_payload.get("aux_ref_audio_paths"), list):
                        req_payload["aux_ref_audio_paths"] = [
                            str(p).replace("${HOST_VOICES_DIR}", shared.HOST_VOICES_DIR).replace(
                                "${HOST_DATA_DIR}", shared.HOST_DATA_DIR
                            )
                            for p in req_payload["aux_ref_audio_paths"]
                        ]
                    req_payload["text"] = clean_text[:120]
                    req_payload["text_split_method"] = "cut0"
                    if voice == "__gsv_raiden_mei__":
                        explicit_emo = None
                        if req_emotion and req_emotion != "auto":
                            norm_emo, _ = mei_emotion_router.normalize_emotion_name(req_emotion)
                            explicit_emo = norm_emo or req_emotion
                        elif tag_emo:
                            explicit_emo = tag_emo
                        emo_key = mei_emotion_router.resolve_mei_emotion(
                            clean_text,
                            explicit_segment_emotion=explicit_emo,
                        )
                        emo_prof = mei_emotion_router.get_mei_emotion_profile(emo_key)
                        req_payload["ref_audio_path"] = emo_prof["ref_audio_path"]
                        req_payload["aux_ref_audio_paths"] = []
                        req_payload["prompt_text"] = emo_prof["prompt_text"]
                        req_payload["prompt_lang"] = emo_prof.get("prompt_lang", "zh")
                        req_payload["speed_factor"] = emo_prof["speed_factor"]
                        req_payload["temperature"] = emo_prof["temperature"]
                        req_payload["top_k"] = emo_prof["top_k"]
                        req_payload["top_p"] = emo_prof["top_p"]
                    for drop_k in ("type", "url", "output_dir", "voice_id"):
                        req_payload.pop(drop_k, None)
                    tts_timeout = _gsv_timeout_within_budget(25.0)
                    client = shared._get_console_http_client()
                    r = client.post(f"{shared.GSV_Host}/tts", json=req_payload, timeout=tts_timeout)
                    if r.status_code != 200:
                        raise RuntimeError(f"本地 GPT-SoVITS 未就绪或返回异常 ({r.status_code})")
                    wav_bytes = r.content
                finally:
                    global _active_gsv_loaded
                    # P1-5: Re-read latest configured GSV voice right before rollback while still
                    # holding _gsv_weights_lock so concurrent config switches are never reverted to old weights.
                    restore_target = _get_configured_gsv_voice()
                    should_restore = bool(
                        restore_target
                        and (restore_target != voice or _active_gsv_loaded == "__dirty__")
                    )
                    if should_restore:
                        _gsv_op_deadline_tls.deadline = lock_deadline
                        try:
                            if time.monotonic() >= lock_deadline:
                                raise TimeoutError("GPU 锁持有时间已达上限，跳过同步回滚，由后台自动恢复")
                            _ensure_gsv_weights(restore_target, persist_boot_yaml=False)
                            cur_dirty, _ = _get_voice_dirty_state()
                            if cur_dirty == restore_target:
                                _set_voice_dirty(None, "")
                        except Exception as rb_exc:
                            _active_gsv_loaded = "__dirty__"
                            _set_voice_dirty(restore_target, str(rb_exc))
                            redacted_rb = shared._redact_secrets(str(rb_exc))
                            rollback_warning = (
                                f"试听后恢复线上音色权重 ({restore_target}) 失败，后台将自动重试：{redacted_rb}"
                            )
                            warn_log = f"[WebConsole] WARNING: {rollback_warning}"
                            print(warn_log, flush=True)
                            with shared._state_lock:
                                shared._append_raw_log_locked(warn_log)
                    elif restore_target == voice and _active_gsv_loaded == voice:
                        cur_dirty, _ = _get_voice_dirty_state()
                        if cur_dirty == restore_target:
                            _set_voice_dirty(None, "")
            finally:
                _gsv_op_deadline_tls.deadline = None
                _gsv_weights_lock.release()
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "audio/wav")
            handler.send_header("Cache-Control", "no-store")
            if rollback_warning:
                handler.send_header("X-Voice-Dirty", "1")
                handler.send_header("X-Preview-Warning", quote(rollback_warning, safe=""))
            handler.send_header("Content-Length", str(len(wav_bytes)))
            handler.end_headers()
            handler.wfile.write(wav_bytes)
        else:
            try:
                mp3_bytes = asyncio.run(
                    _synthesize_mp3_with_timeout(
                        text=clean_text[:120],
                        voice=voice,
                        timeout=EDGE_PREVIEW_TIMEOUT_SECONDS,
                    )
                )
            except asyncio.TimeoutError:
                handler._send_json(
                    {
                        "ok": False,
                        "error": f"Edge 语音试听超时（超过 {int(EDGE_PREVIEW_TIMEOUT_SECONDS)} 秒），请稍后重试。",
                    },
                    HTTPStatus.GATEWAY_TIMEOUT,
                )
                return
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "audio/mpeg")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", str(len(mp3_bytes)))
            handler.end_headers()
            handler.wfile.write(mp3_bytes)
    except shared._BadRequestError:
        raise
    except (TimeoutError, requests.exceptions.Timeout, httpx.TimeoutException) as e:
        err_payload = {"ok": False, "error": shared._redact_secrets(str(e))}
        dirty_v, dirty_e = _get_voice_dirty_state()
        if dirty_v:
            err_payload["voice_dirty"] = True
            err_payload["rollback_warning"] = f"线上音色恢复失败，后台将自动重试：{dirty_e}"
        handler._send_json(err_payload, HTTPStatus.GATEWAY_TIMEOUT)
    except Exception as e:
        err_payload = {"ok": False, "error": shared._redact_secrets(f"音色试听合成失败: {e}")}
        dirty_v, dirty_e = _get_voice_dirty_state()
        if dirty_v:
            err_payload["voice_dirty"] = True
            err_payload["rollback_warning"] = f"线上音色恢复失败，后台将自动重试：{dirty_e}"
        handler._send_json(err_payload, HTTPStatus.INTERNAL_SERVER_ERROR)

"""Lightweight Sherpa-ONNX single-device speaker identification & guard (Phase 2 ④).

Design invariants:
- Zero import-time side effects: no model loading, no disk I/O, no background threads at import.
- Default OFF (`voiceprint.enabled = False`): when disabled, NEVER loads the ONNX speaker model,
  NEVER caches utterance PCM buffers, and adds zero memory or CPU overhead.
- Unload-on-disable: calling `unload_model()` explicitly releases the Sherpa extractor/manager,
  clears volatile PCM/session caches, and triggers `gc.collect()` + glibc `malloc_trim(0)`.
- Zero-lockout safety: until at least one `role="owner"` profile is enrolled (and the model is
  ready and `enabled=True`), `is_guard_active()` stays `False` and all turns / MCP tools pass
  through untouched.
"""

from __future__ import annotations

from collections import OrderedDict
import ctypes
import gc
import io
import json
import math
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid
import wave

import numpy as np

import runtime_events
import runtime_settings
import xz_common

STATE_DIR = xz_common.STATE_DIR
VOICEPRINTS_PATH = os.path.join(STATE_DIR, "voiceprints.json")
MODEL_DIR = os.environ.get(
    "XIAOZHI_SPEAKER_MODEL_DIR",
    os.path.join(xz_common.ROOT_DIR, "models", "sherpa-onnx-speaker"),
)

PREFERRED_MODEL_FILENAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
MIN_MODEL_BYTES = 20_000_000  # ~28 MB real model; aligned with downloader to reject truncated files

SAMPLE_RATE = 16000
MIN_AUDIO_SECONDS = 0.8
MAX_AUDIO_SECONDS = 10.0
MIN_PCM_BYTES = int(SAMPLE_RATE * 2 * MIN_AUDIO_SECONDS)  # 25,600 bytes (int16 mono)
MAX_PCM_BYTES = int(SAMPLE_RATE * 2 * MAX_AUDIO_SECONDS)  # 320,000 bytes
MAX_WAV_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB cap for manual WAV upload
MAX_SAMPLES_PER_SPEAKER = 5
MAX_RECENT_PCM_SESSIONS = 16
MAX_SESSION_SPEAKERS = 64
SHORT_UTTERANCE_STICKY_SECONDS = 60.0

VALID_ROLES = frozenset({"owner", "family"})
_SAFE_CHAR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")

PRIVILEGED_HOST_MCP_TOOLS = frozenset(
    {
        "pc_ui_action",
        "pc_agent_task",
        "pc_system_control",
        "pc_open_app_or_web",
        "pc_inspect_window_ui",
        "pc_look_at_screen",
        "pc_peek_active_window",
        "pc_check_system_status",
    }
)


def _malloc_trim_best_effort() -> None:
    try:
        libc = ctypes.CDLL("libc.so.6")
        fn = libc.malloc_trim
        fn.argtypes = [ctypes.c_size_t]
        fn.restype = ctypes.c_int
        fn(0)
    except Exception:
        pass


def _l2_normalize(vec: List[float] | np.ndarray) -> List[float]:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return []
    if not np.all(np.isfinite(arr)):
        return []
    norm = float(np.linalg.norm(arr))
    if not math.isfinite(norm) or norm <= 1e-12:
        return []
    return [float(x) for x in (arr / norm)]


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na <= 1e-12 or nb <= 1e-12:
        return -1.0
    sim = float(np.dot(va, vb) / (na * nb))
    if not math.isfinite(sim):
        return -1.0
    return max(-1.0, min(1.0, sim))


def _compute_centroid(embeddings: List[List[float]]) -> List[float]:
    valid = [e for e in embeddings if isinstance(e, list) and len(e) > 0]
    if not valid:
        return []
    dim = len(valid[0])
    aligned = [np.asarray(e, dtype=np.float32) for e in valid if len(e) == dim]
    if not aligned:
        return []
    mean_v = np.mean(np.stack(aligned, axis=0), axis=0)
    return _l2_normalize(mean_v)


def _safe_family_character_id(base_character_id: str, speaker_id: str) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "", str(base_character_id or "xiaozhi").strip().lower()) or "xiaozhi"
    slug = _SAFE_SLUG_RE.sub("", str(speaker_id or "").strip().lower())[:16] or uuid.uuid4().hex[:8]
    cand = f"{base}_spk_{slug}"[:48]
    return cand if _SAFE_CHAR_ID_RE.match(cand) else f"spk_{slug}"[:48]


def _safe_guest_character_id(base_character_id: str) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "", str(base_character_id or "xiaozhi").strip().lower()) or "xiaozhi"
    cand = f"{base}_guest"[:48]
    return cand if _SAFE_CHAR_ID_RE.match(cand) else "xiaozhi_guest"


def pcm16_bytes_to_float32(pcm_bytes: bytes) -> np.ndarray:
    raw = bytes(pcm_bytes)
    if len(raw) % 2 != 0:
        raw = raw[:-1]
    if not raw:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def parse_wav_to_pcm16_16k(wav_bytes: bytes) -> bytes:
    """Parse a PCM16 WAV buffer into 16kHz mono PCM16 bytes (linear resampling when needed)."""
    if not isinstance(wav_bytes, (bytes, bytearray)) or len(wav_bytes) < 44:
        raise ValueError("音频文件为空或格式无效，请提供 PCM WAV 音频")
    if len(wav_bytes) > MAX_WAV_UPLOAD_BYTES:
        raise ValueError("WAV 文件超过 2 MB 限制，请录制 2～8 秒人声")
    try:
        with wave.open(io.BytesIO(bytes(wav_bytes)), "rb") as wf:
            nch = int(wf.getnchannels())
            sampwidth = int(wf.getsampwidth())
            sr = int(wf.getframerate())
            nframes = int(wf.getnframes())
            if sampwidth != 2:
                raise ValueError(f"仅支持 16-bit PCM WAV（当前 {sampwidth * 8}-bit）")
            if nch not in (1, 2):
                raise ValueError(f"仅支持单声道或双声道 WAV（当前 {nch} 声道）")
            if sr < 8000 or sr > 48000:
                raise ValueError(f"WAV 采样率需在 8000～48000 Hz 之间（当前 {sr} Hz）")
            max_frames = int(sr * 20)
            frames = wf.readframes(min(nframes, max_frames))
    except wave.Error as exc:
        raise ValueError(f"无法解析 WAV 文件: {exc}") from exc

    arr = np.frombuffer(frames, dtype=np.int16)
    if nch == 2:
        even = (arr.size // 2) * 2
        arr = arr[:even].reshape(-1, 2).mean(axis=1).astype(np.float32)
    else:
        arr = arr.astype(np.float32)

    if sr != SAMPLE_RATE and arr.size > 1:
        dst_len = max(1, int(round(arr.size * (SAMPLE_RATE / float(sr)))))
        src_x = np.linspace(0.0, 1.0, num=arr.size, dtype=np.float64)
        dst_x = np.linspace(0.0, 1.0, num=dst_len, dtype=np.float64)
        arr = np.interp(dst_x, src_x, arr).astype(np.float32)

    pcm16 = np.clip(np.round(arr), -32768, 32767).astype(np.int16).tobytes()
    if len(pcm16) < MIN_PCM_BYTES:
        raise ValueError(f"有效人声时长不足 {MIN_AUDIO_SECONDS:.1f} 秒，请录制 2～6 秒清晰语音")
    return pcm16[:MAX_PCM_BYTES]


def find_speaker_model_path(model_dir: Optional[str] = None) -> Optional[str]:
    """Locate a valid local Sherpa-ONNX speaker embedding `.onnx` file without loading it."""
    d = os.path.abspath(model_dir or MODEL_DIR)
    if not os.path.isdir(d):
        return None
    preferred = os.path.join(d, PREFERRED_MODEL_FILENAME)
    if os.path.isfile(preferred) and os.path.getsize(preferred) >= MIN_MODEL_BYTES:
        return preferred
    try:
        candidates = sorted(
            os.path.join(d, fn)
            for fn in os.listdir(d)
            if fn.lower().endswith(".onnx")
            and not fn.startswith(".")
            and os.path.isfile(os.path.join(d, fn))
        )
    except OSError:
        return None
    for cand in candidates:
        try:
            if os.path.getsize(cand) >= MIN_MODEL_BYTES:
                return cand
        except OSError:
            continue
    return None


class _SherpaOnnxEmbeddingExtractor:
    """Thin wrapper around `sherpa_onnx.SpeakerEmbeddingExtractor`."""

    def __init__(self, model_path: str, num_threads: int = 1) -> None:
        import sherpa_onnx

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=model_path,
            num_threads=max(1, int(num_threads)),
            debug=False,
            provider="cpu",
        )
        if hasattr(cfg, "validate") and not cfg.validate():
            raise RuntimeError(f"Invalid Sherpa speaker model config: {model_path}")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.dim = int(getattr(self._extractor, "dim", 0) or 0)
        self.model_path = model_path

    def compute(self, samples_f32: np.ndarray, sample_rate: int = SAMPLE_RATE) -> List[float]:
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate, samples_f32)
        if hasattr(stream, "input_finished"):
            stream.input_finished()
        raw_vec = self._extractor.compute(stream)
        return _l2_normalize(list(raw_vec))


class VoiceprintRouter:
    """Lazy-loading single-device speaker identification and guard manager."""

    def __init__(
        self,
        *,
        store_path: Optional[str] = None,
        model_dir: Optional[str] = None,
        settings_path: Optional[str] = None,
        extractor_factory: Optional[Callable[[str], Any]] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.store_path = os.path.abspath(store_path or VOICEPRINTS_PATH)
        self.model_dir = os.path.abspath(model_dir or MODEL_DIR)
        self.settings_path = settings_path
        self._extractor_factory = extractor_factory
        self._now_fn = now_fn or time.time

        self._lock = threading.RLock()
        self._loaded_store = False
        self._profiles: Dict[str, Dict[str, Any]] = {}
        self._extractor: Any = None
        self._loaded_model_path: str = ""
        self._model_load_error: str = ""
        self._store_load_error: str = ""

        # Volatile in-memory state (never persisted to disk; cleared on unload)
        self._recent_pcm: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._session_speakers: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._latest_identification: Optional[Dict[str, Any]] = None

    def _now(self) -> float:
        return float(self._now_fn())

    def get_config(self) -> Dict[str, Any]:
        return runtime_settings.get_voiceprint(self.settings_path)

    def is_enabled(self) -> bool:
        try:
            return bool(self.get_config().get("enabled", False))
        except Exception:
            return False

    def _ensure_store_loaded_locked(self) -> None:
        if self._loaded_store:
            return
        self._loaded_store = True
        self._profiles = {}
        if not os.path.isfile(self.store_path):
            return
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_list = data.get("speakers") if isinstance(data, dict) else None
            if not isinstance(raw_list, list):
                return
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("speaker_id") or "").strip()
                name = str(item.get("display_name") or "").strip()[:32]
                role = str(item.get("role") or "family").strip().lower()
                if not sid or not name or role not in VALID_ROLES:
                    continue
                raw_embs = item.get("embeddings")
                if not isinstance(raw_embs, list):
                    continue
                clean_embs: List[List[float]] = []
                for e in raw_embs[:MAX_SAMPLES_PER_SPEAKER]:
                    if isinstance(e, list):
                        norm_e = _l2_normalize(e)
                        if norm_e:
                            clean_embs.append(norm_e)
                if not clean_embs:
                    continue
                cid = str(item.get("character_id") or "").strip().lower()
                if cid and not _SAFE_CHAR_ID_RE.match(cid):
                    cid = ""
                self._profiles[sid] = {
                    "speaker_id": sid,
                    "display_name": name,
                    "role": role,
                    "character_id": "" if role == "owner" else cid,
                    "embeddings": clean_embs,
                    "centroid": _compute_centroid(clean_embs),
                    "sample_count": len(clean_embs),
                    "updated_at": float(item.get("updated_at") or 0.0),
                }
            self._store_load_error = ""
        except Exception as exc:
            self._profiles = {}
            self._store_load_error = f"{type(exc).__name__}: {exc}"

    def _save_store_locked(self) -> None:
        if self._store_load_error and os.path.isfile(self.store_path):
            # Preserve corrupt store file as backup instead of silently wiping previous profiles
            backup_path = f"{self.store_path}.corrupt.{int(self._now())}"
            try:
                os.replace(self.store_path, backup_path)
            except OSError:
                pass
            self._store_load_error = ""

        payload = {
            "schema_version": 1,
            "updated_at": self._now(),
            "speakers": [
                {
                    "speaker_id": p["speaker_id"],
                    "display_name": p["display_name"],
                    "role": p["role"],
                    "character_id": p.get("character_id") or "",
                    "embeddings": p["embeddings"],
                    "sample_count": len(p["embeddings"]),
                    "updated_at": float(p.get("updated_at") or self._now()),
                }
                for p in self._profiles.values()
            ],
        }
        parent = os.path.dirname(os.path.abspath(self.store_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp_path = f"{self.store_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(serialized)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, self.store_path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def has_owner_enrolled(self) -> bool:
        with self._lock:
            self._ensure_store_loaded_locked()
            return any(
                p.get("role") == "owner" and bool(p.get("embeddings"))
                for p in self._profiles.values()
            )

    def is_model_present(self) -> bool:
        if self._extractor_factory is not None:
            return True
        return find_speaker_model_path(self.model_dir) is not None

    def is_model_loaded(self) -> bool:
        with self._lock:
            return self._extractor is not None

    def _get_or_load_extractor_locked(self) -> Any:
        if self._extractor is not None:
            return self._extractor
        if self._extractor_factory is not None:
            mpath = find_speaker_model_path(self.model_dir) or os.path.join(self.model_dir, PREFERRED_MODEL_FILENAME)
            ext = self._extractor_factory(mpath)
            self._extractor = ext
            self._loaded_model_path = mpath
            self._model_load_error = ""
            return ext
        mpath = find_speaker_model_path(self.model_dir)
        if not mpath:
            self._model_load_error = "model_missing"
            return None
        try:
            ext = _SherpaOnnxEmbeddingExtractor(mpath, num_threads=1)
            self._extractor = ext
            self._loaded_model_path = mpath
            self._model_load_error = ""
            return ext
        except Exception as exc:
            self._model_load_error = f"{type(exc).__name__}: {exc}"
            return None

    def unload_model(self, *, run_gc: bool = True) -> bool:
        """Explicitly drop ONNX speaker extractor and volatile PCM/session caches to reclaim RAM."""
        with self._lock:
            was_loaded = self._extractor is not None
            had_cached = bool(self._recent_pcm) or bool(self._session_speakers)
            self._extractor = None
            self._loaded_model_path = ""
            self._model_load_error = ""
            self._recent_pcm.clear()
            self._session_speakers.clear()
            self._latest_identification = None
        if run_gc and (was_loaded or had_cached):
            gc.collect()
            _malloc_trim_best_effort()
        return was_loaded

    def is_guard_active(self) -> bool:
        """Return True ONLY when enabled, model is available, and at least one owner is enrolled (zero lockout)."""
        if not self.is_enabled():
            return False
        if not self.has_owner_enrolled():
            return False
        return self.is_model_present()

    def cache_recent_pcm(self, session_id: str, pcm_bytes: bytes, *, transcript: str = "") -> bool:
        """Keep the latest 16kHz PCM clip in RAM for one-click enrollment (only when voiceprint is enabled)."""
        if not self.is_enabled():
            return False
        sid = str(session_id or "").strip() or "__default__"
        if not isinstance(pcm_bytes, (bytes, bytearray)) or len(pcm_bytes) < MIN_PCM_BYTES:
            return False
        clipped = bytes(pcm_bytes[:MAX_PCM_BYTES])
        now = self._now()
        with self._lock:
            self._recent_pcm[sid] = {
                "session_id": sid,
                "pcm_bytes": clipped,
                "duration_sec": round(len(clipped) / float(SAMPLE_RATE * 2), 2),
                "transcript": str(transcript or "").strip()[:80],
                "ts": now,
            }
            self._recent_pcm.move_to_end(sid)
            while len(self._recent_pcm) > MAX_RECENT_PCM_SESSIONS:
                self._recent_pcm.popitem(last=False)
        return True

    def get_recent_pcm_info(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._recent_pcm:
                return None
            _, latest = next(reversed(self._recent_pcm.items()))
            return {
                "available": True,
                "session_id": latest["session_id"][:24],
                "duration_sec": latest["duration_sec"],
                "transcript": latest["transcript"],
                "ts": latest["ts"],
            }

    def _extract_embedding_from_pcm_locked(self, pcm_bytes: bytes) -> List[float]:
        ext = self._get_or_load_extractor_locked()
        if ext is None:
            raise RuntimeError(
                "声纹模型尚未就绪，请先在 18080 点击「下载声纹模型」或将 ONNX 模型放入 models/sherpa-onnx-speaker/"
            )
        samples = pcm16_bytes_to_float32(pcm_bytes[:MAX_PCM_BYTES])
        if samples.size < int(SAMPLE_RATE * MIN_AUDIO_SECONDS):
            raise ValueError(f"有效人声时长不足 {MIN_AUDIO_SECONDS:.1f} 秒")
        vec = ext.compute(samples, SAMPLE_RATE)
        norm_vec = _l2_normalize(vec)
        if not norm_vec:
            raise RuntimeError("未能从音频中提取有效声纹向量")
        return norm_vec

    def enroll_pcm(
        self,
        pcm_bytes: bytes,
        *,
        display_name: str,
        role: str = "owner",
        speaker_id: Optional[str] = None,
        character_id: Optional[str] = None,
        base_character_id: str = "xiaozhi",
    ) -> Dict[str, Any]:
        """Extract speaker embedding from 16kHz mono PCM16 and persist to `state/voiceprints.json`."""
        name = str(display_name or "").strip()[:32]
        if not name:
            raise ValueError("说话人名称不能为空")
        role_low = str(role or "owner").strip().lower()
        if role_low not in VALID_ROLES:
            raise ValueError("说话人角色仅支持 owner（主人）或 family（家人）")
        if not isinstance(pcm_bytes, (bytes, bytearray)) or len(pcm_bytes) < MIN_PCM_BYTES:
            raise ValueError(f"有效语音时长不足 {MIN_AUDIO_SECONDS:.1f} 秒，请至少说 1～3 秒完整句子")

        unload_after = not self.is_enabled()
        try:
            with self._lock:
                self._ensure_store_loaded_locked()
                emb = self._extract_embedding_from_pcm_locked(bytes(pcm_bytes))

                sid = str(speaker_id or "").strip()
                existing = self._profiles.get(sid) if sid else None
                if existing is None:
                    # Reuse existing profile if same role+name already enrolled (appends sample up to 5)
                    for p in self._profiles.values():
                        if p["role"] == role_low and p["display_name"] == name:
                            existing = p
                            sid = p["speaker_id"]
                            break
                if existing is not None:
                    embs = list(existing["embeddings"])
                    if embs and len(embs[0]) != len(emb):
                        embs = [emb]
                    else:
                        embs.append(emb)
                        embs = embs[-MAX_SAMPLES_PER_SPEAKER:]
                    cid_eff = existing.get("character_id") or ""
                    if role_low == "owner":
                        cid_eff = ""
                    elif character_id is not None and str(character_id).strip():
                        cand_cid = str(character_id).strip().lower()
                        if not _SAFE_CHAR_ID_RE.match(cand_cid):
                            raise ValueError("character_id 仅允许小写字母、数字、下划线或连字符")
                        cid_eff = cand_cid
                    elif not cid_eff:
                        cid_eff = _safe_family_character_id(base_character_id, sid)
                    prof = {
                        "speaker_id": sid,
                        "display_name": name,
                        "role": role_low,
                        "character_id": cid_eff,
                        "embeddings": embs,
                        "centroid": _compute_centroid(embs),
                        "sample_count": len(embs),
                        "updated_at": self._now(),
                    }
                    self._profiles[sid] = prof
                else:
                    if not sid:
                        sid = f"{role_low}_{uuid.uuid4().hex[:8]}"
                    cid_eff = ""
                    if role_low == "family":
                        if character_id is not None and str(character_id).strip():
                            cand_cid = str(character_id).strip().lower()
                            if not _SAFE_CHAR_ID_RE.match(cand_cid):
                                raise ValueError("character_id 仅允许小写字母、数字、下划线或连字符")
                            cid_eff = cand_cid
                        else:
                            cid_eff = _safe_family_character_id(base_character_id, sid)
                    prof = {
                        "speaker_id": sid,
                        "display_name": name,
                        "role": role_low,
                        "character_id": cid_eff,
                        "embeddings": [emb],
                        "centroid": emb,
                        "sample_count": 1,
                        "updated_at": self._now(),
                    }
                    self._profiles[sid] = prof

                self._save_store_locked()
                # Clear stale session speaker cache so newly enrolled owner/family takes effect immediately
                self._session_speakers.clear()
                return self._public_profile(prof)
        finally:
            if unload_after:
                # Zero-idle-memory guarantee: never leave ~28 MB ONNX weights resident while switch is OFF
                self.unload_model(run_gc=True)

    def enroll_last_utterance(
        self,
        *,
        display_name: str,
        role: str = "owner",
        speaker_id: Optional[str] = None,
        character_id: Optional[str] = None,
        session_id: Optional[str] = None,
        base_character_id: str = "xiaozhi",
    ) -> Dict[str, Any]:
        with self._lock:
            entry = None
            sid_req = str(session_id or "").strip()
            if sid_req and sid_req in self._recent_pcm:
                entry = self._recent_pcm[sid_req]
            elif self._recent_pcm:
                _, entry = next(reversed(self._recent_pcm.items()))
        if not entry:
            raise ValueError("尚未捕获到可录入的语音片段：请先开启声纹开关，并对着设备说一句 1～3 秒的话后再点录入")
        return self.enroll_pcm(
            entry["pcm_bytes"],
            display_name=display_name,
            role=role,
            speaker_id=speaker_id,
            character_id=character_id,
            base_character_id=base_character_id,
        )

    def enroll_wav_bytes(
        self,
        wav_bytes: bytes,
        *,
        display_name: str,
        role: str = "owner",
        speaker_id: Optional[str] = None,
        character_id: Optional[str] = None,
        base_character_id: str = "xiaozhi",
    ) -> Dict[str, Any]:
        pcm_bytes = parse_wav_to_pcm16_16k(wav_bytes)
        return self.enroll_pcm(
            pcm_bytes,
            display_name=display_name,
            role=role,
            speaker_id=speaker_id,
            character_id=character_id,
            base_character_id=base_character_id,
        )

    def delete_speaker(self, speaker_id: str) -> bool:
        sid = str(speaker_id or "").strip()
        if not sid:
            return False
        with self._lock:
            self._ensure_store_loaded_locked()
            existed = self._profiles.pop(sid, None) is not None
            if existed:
                self._save_store_locked()
                self._session_speakers.clear()
                if (
                    not self._profiles
                    or (
                        isinstance(self._latest_identification, dict)
                        and self._latest_identification.get("speaker_id") == sid
                    )
                ):
                    self._latest_identification = None
            return existed

    def _public_profile(self, prof: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "speaker_id": prof["speaker_id"],
            "display_name": prof["display_name"],
            "role": prof["role"],
            "character_id": prof.get("character_id") or "",
            "sample_count": int(prof.get("sample_count") or len(prof.get("embeddings") or [])),
            "updated_at": float(prof.get("updated_at") or 0.0),
        }

    def list_speakers(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_store_loaded_locked()
            items = [self._public_profile(p) for p in self._profiles.values()]
        items.sort(key=lambda x: (0 if x["role"] == "owner" else 1, -x["updated_at"], x["display_name"]))
        return items

    def _match_embedding_locked(self, emb: List[float], threshold: float) -> Tuple[Optional[Dict[str, Any]], float]:
        best_prof: Optional[Dict[str, Any]] = None
        best_score = -1.0
        for prof in self._profiles.values():
            scores = [_cosine_similarity(emb, prof.get("centroid") or [])]
            for sample_e in prof.get("embeddings") or []:
                scores.append(_cosine_similarity(emb, sample_e))
            cand_score = max(scores) if scores else -1.0
            if cand_score > best_score:
                best_score = cand_score
                best_prof = prof
        if best_prof is not None and best_score >= threshold:
            return best_prof, round(best_score, 4)
        return None, round(max(0.0, best_score), 4)

    def identify_turn_pcm(
        self,
        session_id: str,
        pcm_bytes: bytes,
        *,
        transcript: str = "",
        base_character_id: str = "xiaozhi",
        publish: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Identify speaker for the current turn from 16kHz PCM16 (no-op when disabled)."""
        cfg = self.get_config()
        if not cfg.get("enabled", False):
            return None

        sid = str(session_id or "").strip() or "__default__"
        if isinstance(pcm_bytes, (bytes, bytearray)) and len(pcm_bytes) >= MIN_PCM_BYTES:
            self.cache_recent_pcm(sid, bytes(pcm_bytes), transcript=transcript)

        now = self._now()
        policy = str(cfg.get("policy") or "full_guard").strip().lower()
        threshold = float(cfg.get("threshold", 0.58))

        with self._lock:
            self._ensure_store_loaded_locked()
            has_owner = any(
                p.get("role") == "owner" and bool(p.get("embeddings"))
                for p in self._profiles.values()
            )
            if not has_owner:
                return {
                    "guard_active": False,
                    "role": "owner_passthrough",
                    "display_name": "主人（未录入声纹，默认放行）",
                    "speaker_id": "",
                    "character_id_override": "",
                    "similarity": 0.0,
                    "policy": policy,
                    "ts": now,
                }

            # Short-utterance (<0.8s) sticky reuse within 60s so "嗯/好的" won't falsely drop to guest
            if not isinstance(pcm_bytes, (bytes, bytearray)) or len(pcm_bytes) < MIN_PCM_BYTES:
                prev = self._session_speakers.get(sid)
                if prev and (now - float(prev.get("ts") or 0.0)) <= SHORT_UTTERANCE_STICKY_SECONDS:
                    reused = dict(prev)
                    reused["reused_short"] = True
                    return reused
                guest_rec = {
                    "guard_active": True,
                    "role": "guest",
                    "display_name": "未知访客",
                    "speaker_id": "",
                    "character_id_override": (
                        _safe_guest_character_id(base_character_id)
                        if policy in ("isolate_memory", "full_guard")
                        else ""
                    ),
                    "similarity": 0.0,
                    "policy": policy,
                    "reused_short": False,
                    "ts": now,
                }
                self._record_session_speaker_locked(sid, guest_rec)
                return dict(guest_rec)

            try:
                emb = self._extract_embedding_from_pcm_locked(bytes(pcm_bytes))
            except Exception:
                # Model missing/unavailable -> zero-lockout fail-open (clear stale guest/family lock)
                self._session_speakers.pop(sid, None)
                return {
                    "guard_active": False,
                    "role": "owner_passthrough",
                    "display_name": "主人（声纹模型未就绪，默认放行）",
                    "speaker_id": "",
                    "character_id_override": "",
                    "similarity": 0.0,
                    "policy": policy,
                    "ts": now,
                }

            matched, sim = self._match_embedding_locked(emb, threshold)
            if matched is not None:
                role = str(matched["role"])
                cid_override = ""
                if policy in ("isolate_memory", "full_guard") and role == "family":
                    cid_override = str(matched.get("character_id") or "") or _safe_family_character_id(
                        base_character_id, matched["speaker_id"]
                    )
                rec = {
                    "guard_active": True,
                    "role": role,
                    "display_name": matched["display_name"],
                    "speaker_id": matched["speaker_id"],
                    "character_id_override": cid_override,
                    "similarity": sim,
                    "policy": policy,
                    "reused_short": False,
                    "ts": now,
                }
            else:
                rec = {
                    "guard_active": True,
                    "role": "guest",
                    "display_name": "未知访客",
                    "speaker_id": "",
                    "character_id_override": (
                        _safe_guest_character_id(base_character_id)
                        if policy in ("isolate_memory", "full_guard")
                        else ""
                    ),
                    "similarity": sim,
                    "policy": policy,
                    "reused_short": False,
                    "ts": now,
                }
            self._record_session_speaker_locked(sid, rec)

        if publish:
            try:
                runtime_events.publish_event(
                    "speaker_identified",
                    session_id=sid,
                    role=rec["role"],
                    display_name=rec["display_name"],
                    speaker_id=rec["speaker_id"],
                    similarity=rec["similarity"],
                    policy=policy,
                    character_id_override=rec["character_id_override"],
                )
            except Exception:
                pass
        return dict(rec)

    def _record_session_speaker_locked(self, sid: str, rec: Dict[str, Any]) -> None:
        self._session_speakers[sid] = dict(rec)
        self._session_speakers.move_to_end(sid)
        while len(self._session_speakers) > MAX_SESSION_SPEAKERS:
            self._session_speakers.popitem(last=False)
        self._latest_identification = dict(rec)

    def get_latest_identification(self) -> Optional[Dict[str, Any]]:
        if not self.is_enabled():
            return None
        with self._lock:
            return dict(self._latest_identification) if self._latest_identification else None

    def get_session_speaker(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not self.is_enabled():
            return None
        sid = str(session_id or "").strip() or "__default__"
        with self._lock:
            item = self._session_speakers.get(sid)
            return dict(item) if item else None

    def get_speaker_character_override(
        self,
        session_id: str,
        *,
        base_character_id: Optional[str] = None,
    ) -> Optional[str]:
        """Return isolated Anima character_id for non-owner speaker in isolate_memory/full_guard mode."""
        if not self.is_guard_active():
            return None
        cfg = self.get_config()
        if cfg.get("policy") not in ("isolate_memory", "full_guard"):
            return None
        rec = self.get_session_speaker(session_id)
        if not rec or not rec.get("guard_active"):
            return None
        role = str(rec.get("role") or "")
        if role == "owner":
            return None
        if role == "guest" and base_character_id:
            cid = _safe_guest_character_id(base_character_id)
        else:
            cid = str(rec.get("character_id_override") or "").strip().lower()
        return cid if (cid and _SAFE_CHAR_ID_RE.match(cid)) else None

    def format_speakers_info_block(self, session_id: str) -> str:
        """Build a concise dynamic `<speakers_info>` hint for `<turn_context>` when guard is active."""
        if not self.is_guard_active():
            return ""
        rec = self.get_session_speaker(session_id)
        if not rec or not rec.get("guard_active"):
            return ""
        role = str(rec.get("role") or "")
        name = str(rec.get("display_name") or "").strip()
        if role == "owner":
            return f"<speakers_info>\n当前说话人：{name or '主人'}（已通过声纹确认为主人）\n</speakers_info>"
        if role == "family":
            return (
                f"<speakers_info>\n当前说话人：{name or '家人'}（已识别的家庭成员，非主人；"
                "请保持礼貌友好，不提及主人私密记忆，也不代为操作主人电脑）\n</speakers_info>"
            )
        return (
            "<speakers_info>\n当前说话人：未知访客（未匹配到主人声纹；"
            "请以自然友善的访客模式交流，绝不泄露主人隐私记忆，也不执行电脑操控）\n</speakers_info>"
        )

    def check_mcp_tool_permission(self, session_id: str, tool_name: str) -> Tuple[bool, str]:
        """Enforce full_guard owner-only access on privileged host MCP tools (zero-lockout safe)."""
        clean_tool = str(tool_name or "").strip()
        if clean_tool.startswith("mcp_"):
            clean_tool = clean_tool[4:]

        # On-demand screen peek switch applies regardless of voiceprint state
        if clean_tool == "pc_peek_active_window":
            if not bool(runtime_settings.get_screen_peek(self.settings_path).get("enabled", False)):
                return (
                    False,
                    "【按需浅看屏未开启】当前按需浅看屏开关处于关闭状态，请温和告知用户可在 18080 控制台按需开启后再试。",
                )

        if clean_tool not in PRIVILEGED_HOST_MCP_TOOLS:
            return True, ""

        if not self.is_guard_active():
            return True, ""

        cfg = self.get_config()
        if str(cfg.get("policy") or "full_guard") != "full_guard":
            return True, ""

        rec = self.get_session_speaker(session_id)
        if rec is None:
            # Text/console-originated turns without audio PCM do not break local pc_talk when no guest matched
            return True, ""
        if rec.get("role") == "owner":
            return True, ""

        who = str(rec.get("display_name") or "当前说话人")
        return (
            False,
            f"【声纹安全拦截】当前识别为「{who}」（非主人），电脑操控与看屏工具 `{clean_tool}` 仅授权给主人使用。"
            "请用自然礼貌的语气告知对方这项操作需要主人亲自语音授权。",
        )

    def get_status(self) -> Dict[str, Any]:
        cfg = self.get_config()
        mpath = find_speaker_model_path(self.model_dir)
        speakers = self.list_speakers()
        with self._lock:
            loaded = self._extractor is not None
            load_err = self._model_load_error
            latest = dict(self._latest_identification) if self._latest_identification else None
        has_owner = any(s["role"] == "owner" for s in speakers)
        return {
            "ok": True,
            "config": cfg,
            "model_dir": self.model_dir,
            "model_present": bool(mpath or self._extractor_factory is not None),
            "model_filename": os.path.basename(mpath) if mpath else PREFERRED_MODEL_FILENAME,
            "model_loaded": loaded,
            "model_load_error": load_err,
            "has_owner": has_owner,
            "guard_active": bool(cfg.get("enabled") and has_owner and (mpath or self._extractor_factory is not None)),
            "speakers": speakers,
            "recent_pcm": self.get_recent_pcm_info(),
            "latest_identification": latest,
        }


_router_lock = threading.Lock()
_default_router: Optional[VoiceprintRouter] = None


def get_voiceprint_router() -> VoiceprintRouter:
    global _default_router
    with _router_lock:
        if _default_router is None:
            _default_router = VoiceprintRouter()
        return _default_router


def reset_default_router_for_tests(router: Optional[VoiceprintRouter] = None) -> None:
    global _default_router
    with _router_lock:
        _default_router = router


def is_voiceprint_enabled(settings_path: Optional[str] = None) -> bool:
    if settings_path is not None:
        return bool(runtime_settings.get_voiceprint(settings_path).get("enabled", False))
    return get_voiceprint_router().is_enabled()


def identify_turn_pcm(
    session_id: str,
    pcm_bytes: bytes,
    *,
    transcript: str = "",
    base_character_id: str = "xiaozhi",
    publish: bool = True,
) -> Optional[Dict[str, Any]]:
    return get_voiceprint_router().identify_turn_pcm(
        session_id,
        pcm_bytes,
        transcript=transcript,
        base_character_id=base_character_id,
        publish=publish,
    )


def get_speaker_character_override(
    session_id: str,
    *,
    base_character_id: Optional[str] = None,
) -> Optional[str]:
    return get_voiceprint_router().get_speaker_character_override(
        session_id,
        base_character_id=base_character_id,
    )


def get_latest_identification() -> Optional[Dict[str, Any]]:
    return get_voiceprint_router().get_latest_identification()


def format_speakers_info_block(session_id: str) -> str:
    return get_voiceprint_router().format_speakers_info_block(session_id)


def check_mcp_tool_permission(session_id: str, tool_name: str) -> Tuple[bool, str]:
    return get_voiceprint_router().check_mcp_tool_permission(session_id, tool_name)


def unload_voiceprint_model(*, run_gc: bool = True) -> bool:
    return get_voiceprint_router().unload_model(run_gc=run_gc)

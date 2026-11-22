"""Cloud Audio API provider catalog, strict validation, browser TTS preview, and ASR connectivity test.

Zero plaintext secret persistence: API keys / tokens / appids live strictly in environment variables
and are stored in `.config.yaml` ONLY as `${ENV_VAR}` placeholders.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import gzip
from http import HTTPStatus
import io
import ipaddress
import json
import math
import os
import re
import socket
import struct
import tempfile
import time
import uuid
from urllib.parse import urlparse
import wave
from typing import Any, Dict, List, Optional, Tuple

import httpx
import requests

from console import shared
from patches import cloud_audio as cloud_audio_patch

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:/@\-]{1,160}$")
_BLOCKED_INTERNAL_CONTAINERS = frozenset(
    {"anima-brain", "xiaozhi-anima-brain", "xiaozhi-esp32-server"}
)
_BLOCKED_LOCAL_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)
_BLOCKED_INTERNAL_PORTS = frozenset({8000, 8003, 18000, 18003, 18080, 18787, 18900, 19880})
_DOCKER_BRIDGE_NET = ipaddress.ip_network("172.16.0.0/12")
_NON_CANONICAL_IP_RE = re.compile(r"^[0-9.xXa-fA-F]+$")
_FORBIDDEN_SECRET_FIELDS = frozenset(
    {"api_key", "access_token", "appid", "group_id", "authorization", "token", "secret"}
)

CLOUD_PREVIEW_TIMEOUT_SECONDS = 20.0
CLOUD_ASR_TEST_TIMEOUT_SECONDS = 15.0

_AUDIO_FMT_CONTENT_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "pcm": "audio/wav",
}


ASR_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "SiliconFlowASR": {
        "label": "硅基流动 SenseVoiceSmall（云端低配推荐 · 国内直连）",
        "short_label": "硅基流动 ASR",
        "group": "cloud",
        "type": "openai",
        "zero_local_model": True,
        "streaming": False,
        "env_keys": ["SILICONFLOW_API_KEY"],
        "env_hint": "SILICONFLOW_API_KEY",
        "description": "零本地模型、零显存、国内直连低成本，适合 4–8GB 低配/轻薄本电脑。",
        "editable_fields": ["base_url", "model_name"],
        "preset_models": [
            {"value": "FunAudioLLM/SenseVoiceSmall", "label": "FunAudioLLM/SenseVoiceSmall（中英粤日韩 · 推荐）"},
            {"value": "TeleAI/TeleSpeechASR", "label": "TeleAI/TeleSpeechASR（中文高精度）"},
        ],
        "defaults": {
            "type": "openai",
            "base_url": "https://api.siliconflow.cn/v1/audio/transcriptions",
            "model_name": "FunAudioLLM/SenseVoiceSmall",
            "api_key": "${SILICONFLOW_API_KEY}",
            "output_dir": "tmp/",
        },
    },
    "OpenaiASR": {
        "label": "通用 OpenAI 兼容 ASR（OpenAI / Groq / One-API / 自建）",
        "short_label": "OpenAI 兼容 ASR",
        "group": "cloud",
        "type": "openai",
        "zero_local_model": True,
        "streaming": False,
        "env_keys": ["OPENAI_AUDIO_API_KEY"],
        "env_hint": "OPENAI_AUDIO_API_KEY",
        "description": "支持任何兼容 /v1/audio/transcriptions 的中转站（One-API / New-API / Groq / OpenAI / 自建 Whisper）。",
        "editable_fields": ["base_url", "model_name"],
        "preset_models": [
            {"value": "gpt-4o-mini-transcribe", "label": "gpt-4o-mini-transcribe（OpenAI）"},
            {"value": "whisper-1", "label": "whisper-1（通用兼容）"},
            {"value": "whisper-large-v3-turbo", "label": "whisper-large-v3-turbo（Groq 极速）"},
            {"value": "FunAudioLLM/SenseVoiceSmall", "label": "FunAudioLLM/SenseVoiceSmall（硅基中转）"},
        ],
        "defaults": {
            "type": "openai",
            "base_url": "https://api.openai.com/v1/audio/transcriptions",
            "model_name": "gpt-4o-mini-transcribe",
            "api_key": "${OPENAI_AUDIO_API_KEY}",
            "output_dir": "tmp/",
        },
    },
    "Qwen3ASRFlash": {
        "label": "阿里云百炼 Qwen3-ASR-Flash（国内直连 HTTP）",
        "short_label": "百炼 Qwen3 ASR",
        "group": "cloud",
        "type": "qwen3_asr_flash",
        "zero_local_model": True,
        "streaming": False,
        "env_keys": ["DASHSCOPE_API_KEY"],
        "env_hint": "DASHSCOPE_API_KEY",
        "description": "阿里云百炼 DashScope 语音大模型识别，国内直连稳定，支持自动语种检测与上下文热词。",
        "editable_fields": ["model_name", "context"],
        "preset_models": [
            {"value": "qwen3-asr-flash", "label": "qwen3-asr-flash（推荐）"},
        ],
        "defaults": {
            "type": "qwen3_asr_flash",
            "api_key": "${DASHSCOPE_API_KEY}",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model_name": "qwen3-asr-flash",
            "enable_lid": True,
            "enable_itn": True,
            "context": "",
            "output_dir": "tmp/",
        },
    },
    "AliyunBLStreamASR": {
        "label": "阿里云百炼 Paraformer 实时流式（国内直连 WS）",
        "short_label": "百炼 Paraformer 流式",
        "group": "cloud",
        "type": "aliyunbl_stream",
        "zero_local_model": True,
        "streaming": True,
        "env_keys": ["DASHSCOPE_API_KEY"],
        "env_hint": "DASHSCOPE_API_KEY",
        "description": "WebSocket 边说边识别，国内直连低延迟，零本地模型占用。",
        "editable_fields": ["model"],
        "preset_models": [
            {"value": "paraformer-realtime-v2", "label": "paraformer-realtime-v2（16kHz 推荐）"},
            {"value": "gummy-realtime-v1", "label": "gummy-realtime-v1（多语种流式）"},
        ],
        "defaults": {
            "type": "aliyunbl_stream",
            "api_key": "${DASHSCOPE_API_KEY}",
            "model": "paraformer-realtime-v2",
            "format": "pcm",
            "sample_rate": 16000,
            "disfluency_removal_enabled": False,
            "semantic_punctuation_enabled": False,
            "max_sentence_silence": 200,
            "multi_threshold_mode_enabled": False,
            "punctuation_prediction_enabled": True,
            "inverse_text_normalization_enabled": True,
            "output_dir": "tmp/",
        },
    },
    "DoubaoASR": {
        "label": "火山引擎豆包语音识别（中文高精度）",
        "short_label": "豆包 ASR",
        "group": "cloud",
        "type": "doubao",
        "zero_local_model": True,
        "streaming": False,
        "env_keys": ["DOUBAO_ASR_APPID", "DOUBAO_ASR_ACCESS_TOKEN"],
        "env_hint": "DOUBAO_ASR_APPID + DOUBAO_ASR_ACCESS_TOKEN（缺省复用 DOUBAO_APPID / DOUBAO_ACCESS_TOKEN）",
        "description": "火山引擎一句话识别，中文口语准确率与标点表现优秀。",
        "editable_fields": ["cluster"],
        "preset_models": [
            {"value": "volcengine_input_common", "label": "volcengine_input_common（默认输入法集群）"},
        ],
        "defaults": {
            "type": "doubao",
            "appid": "${DOUBAO_ASR_APPID}",
            "access_token": "${DOUBAO_ASR_ACCESS_TOKEN}",
            "cluster": "volcengine_input_common",
            "output_dir": "tmp/",
        },
    },
    "DoubaoStreamASR": {
        "label": "火山引擎豆包大模型流式识别（低延迟 WS）",
        "short_label": "豆包流式 ASR",
        "group": "cloud",
        "type": "doubao_stream",
        "zero_local_model": True,
        "streaming": True,
        "env_keys": ["DOUBAO_ASR_APPID", "DOUBAO_ASR_ACCESS_TOKEN"],
        "env_hint": "DOUBAO_ASR_APPID + DOUBAO_ASR_ACCESS_TOKEN（缺省复用 DOUBAO_APPID / DOUBAO_ACCESS_TOKEN）",
        "description": "豆包 SAUC 实时流式大模型识别，低延迟中文口语体验极佳。",
        "editable_fields": ["resource_id"],
        "preset_models": [
            {"value": "volc.bigasr.sauc.duration", "label": "volc.bigasr.sauc.duration（大模型时长版）"},
            {"value": "volc.seedasr.sauc.duration", "label": "volc.seedasr.sauc.duration（SeedASR V2）"},
        ],
        "defaults": {
            "type": "doubao_stream",
            "appid": "${DOUBAO_ASR_APPID}",
            "access_token": "${DOUBAO_ASR_ACCESS_TOKEN}",
            "resource_id": "volc.bigasr.sauc.duration",
            "enable_multilingual": False,
            "end_window_size": 200,
            "output_dir": "tmp/",
        },
    },
    "SherpaASR": {
        "label": "本地 Sherpa-ONNX SenseVoice（int8 轻量离线 · ~350MB 内存）",
        "short_label": "本地 SherpaASR",
        "group": "local",
        "type": "sherpa_onnx_local",
        "zero_local_model": False,
        "streaming": False,
        "env_keys": [],
        "env_hint": "无需密钥（需本地模型 models/sherpa-onnx-sense-voice）",
        "description": "离线免费、支持投机早判 VAD；内存占用约 350MB，需要一定 CPU 算力。",
        "editable_fields": [],
        "preset_models": [],
        "defaults": {
            "type": "sherpa_onnx_local",
            "model_dir": "models/sherpa-onnx-sense-voice",
            "output_dir": "tmp/",
            "model_type": "sense_voice",
        },
    },
    "FunASR": {
        "label": "本地 FunASR PyTorch（旧版重型 · ~3.5GB 内存）",
        "short_label": "本地 FunASR",
        "group": "local",
        "type": "fun_local",
        "zero_local_model": False,
        "streaming": False,
        "env_keys": [],
        "env_hint": "无需密钥（不推荐低配电脑）",
        "description": "旧版 PyTorch 本地识别，仅保留兼容，低配电脑建议使用云端 ASR 或 SherpaASR。",
        "editable_fields": [],
        "preset_models": [],
        "defaults": {
            "type": "fun_local",
            "model_dir": "models/SenseVoiceSmall",
            "output_dir": "tmp/",
            "language": "auto",
        },
    },
}


TTS_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "EdgeTTS": {
        "label": "微软 EdgeTTS（免费免显存 · 开箱即用）",
        "short_label": "EdgeTTS",
        "group": "free",
        "type": "edge",
        "zero_gpu": True,
        "streaming": False,
        "pc_audio_supported": True,
        "pc_audio_rate": 48000,
        "env_keys": [],
        "env_hint": "无需密钥",
        "description": "零显存、零费用，内置 10 组中文/方言神经语音，支持 ESP32 与电脑音响直通。",
        "editable_fields": ["voice"],
        "preset_models": [],
        "preset_voices": [],
        "defaults": {
            "type": "edge",
            "voice": "zh-CN-XiaoxiaoNeural",
            "output_dir": "tmp/",
        },
    },
    "CosyVoiceSiliconflow": {
        "label": "硅基流动 CosyVoice2（云端国内直连推荐 · 零显存）",
        "short_label": "硅基流动 TTS",
        "group": "cloud",
        "type": "siliconflow",
        "zero_gpu": True,
        "streaming": False,
        "pc_audio_supported": True,
        "pc_audio_rate": 48000,
        "env_keys": ["SILICONFLOW_API_KEY"],
        "env_hint": "SILICONFLOW_API_KEY",
        "description": "国内直连高拟真中文语音，可与硅基流动 ASR 共用同一个 Key，支持电脑 48kHz 音响直通。",
        "editable_fields": ["model", "voice", "speed", "response_format"],
        "preset_models": [
            {"value": "FunAudioLLM/CosyVoice2-0.5B", "label": "FunAudioLLM/CosyVoice2-0.5B（推荐）"},
            {"value": "fnlp/MOSS-TTSD-v0.5", "label": "fnlp/MOSS-TTSD-v0.5（对话风格）"},
        ],
        "preset_voices": [
            {"value": "FunAudioLLM/CosyVoice2-0.5B:anna", "label": "Anna · 沉稳温柔女声"},
            {"value": "FunAudioLLM/CosyVoice2-0.5B:claire", "label": "Claire · 自然清亮女声"},
            {"value": "FunAudioLLM/CosyVoice2-0.5B:bella", "label": "Bella · 甜美活泼少女音"},
            {"value": "FunAudioLLM/CosyVoice2-0.5B:diana", "label": "Diana · 温和知性女声"},
            {"value": "FunAudioLLM/CosyVoice2-0.5B:alex", "label": "Alex · 沉稳阳光男声"},
            {"value": "FunAudioLLM/CosyVoice2-0.5B:benjamin", "label": "Benjamin · 低沉磁性男声"},
            {"value": "FunAudioLLM/CosyVoice2-0.5B:charles", "label": "Charles · 清晰播音男声"},
            {"value": "FunAudioLLM/CosyVoice2-0.5B:david", "label": "David · 欢快明朗男声"},
        ],
        "defaults": {
            "type": "siliconflow",
            "model": "FunAudioLLM/CosyVoice2-0.5B",
            "voice": "FunAudioLLM/CosyVoice2-0.5B:anna",
            "access_token": "${SILICONFLOW_API_KEY}",
            "response_format": "wav",
            "speed": 1.0,
            "output_dir": "tmp/",
        },
    },
    "OpenAITTS": {
        "label": "通用 OpenAI 兼容 TTS（OpenAI / One-API / New-API / 自建）",
        "short_label": "OpenAI 兼容 TTS",
        "group": "cloud",
        "type": "openai",
        "zero_gpu": True,
        "streaming": False,
        "pc_audio_supported": True,
        "pc_audio_rate": 48000,
        "env_keys": ["OPENAI_AUDIO_API_KEY"],
        "env_hint": "OPENAI_AUDIO_API_KEY",
        "description": "兼容任何 /v1/audio/speech 协议（OpenAI / One-API / New-API / 自建 Kokoro-FastAPI 等）。",
        "editable_fields": ["api_url", "model", "voice", "speed", "format"],
        "preset_models": [
            {"value": "tts-1", "label": "tts-1（低延迟）"},
            {"value": "tts-1-hd", "label": "tts-1-hd（高清）"},
            {"value": "gpt-4o-mini-tts", "label": "gpt-4o-mini-tts（新一代自然声线）"},
        ],
        "preset_voices": [
            {"value": "nova", "label": "Nova · 温暖自然女声"},
            {"value": "shimmer", "label": "Shimmer · 清亮柔和女声"},
            {"value": "alloy", "label": "Alloy · 均衡中性"},
            {"value": "coral", "label": "Coral · 亲切口语"},
            {"value": "echo", "label": "Echo · 沉稳男声"},
            {"value": "onyx", "label": "Onyx · 磁性低音"},
            {"value": "fable", "label": "Fable · 故事叙事"},
        ],
        "defaults": {
            "type": "openai",
            "api_url": "https://api.openai.com/v1/audio/speech",
            "api_key": "${OPENAI_AUDIO_API_KEY}",
            "model": "tts-1",
            "voice": "nova",
            "format": "wav",
            "speed": 1.0,
            "output_dir": "tmp/",
        },
    },
    "AliBLTTS": {
        "label": "阿里云百炼 CosyVoice 双向流式（国内直连 · 48kHz 音响直通）",
        "short_label": "百炼 CosyVoice 流式",
        "group": "cloud",
        "type": "alibl_stream",
        "zero_gpu": True,
        "streaming": True,
        "pc_audio_supported": True,
        "pc_audio_rate": 48000,
        "env_keys": ["DASHSCOPE_API_KEY"],
        "env_hint": "DASHSCOPE_API_KEY",
        "description": "国内直连双向 WebSocket 流式合成，超低首字延迟，已适配电脑 48kHz 无损音响直通。",
        "editable_fields": ["model", "voice", "rate"],
        "preset_models": [
            {"value": "cosyvoice-v2", "label": "cosyvoice-v2（稳定推荐）"},
            {"value": "cosyvoice-v3-flash", "label": "cosyvoice-v3-flash（新一代极速）"},
            {"value": "cosyvoice-v1", "label": "cosyvoice-v1（经典）"},
        ],
        "preset_voices": [
            {"value": "longxiaochun_v2", "label": "龙小淳 · 知性温柔女声"},
            {"value": "longxiaoxia_v2", "label": "龙小夏 · 甜美温暖女声"},
            {"value": "longwan_v2", "label": "龙婉 · 柔和陪伴女声"},
            {"value": "longhua_v2", "label": "龙华 · 活泼少女音"},
            {"value": "longcheng_v2", "label": "龙橙 · 阳光男声"},
            {"value": "longshu_v2", "label": "龙书 · 沉稳叙事男声"},
        ],
        "defaults": {
            "type": "alibl_stream",
            "api_key": "${DASHSCOPE_API_KEY}",
            "ws_url": "wss://dashscope.aliyuncs.com/api-ws/v1/inference/",
            "model": "cosyvoice-v2",
            "voice": "longxiaochun_v2",
            "format": "pcm",
            "rate": 1.0,
            "output_dir": "tmp/",
        },
    },
    "DoubaoTTS": {
        "label": "火山引擎豆包 HTTP TTS（中文自然 · 48kHz 音响直通）",
        "short_label": "豆包 HTTP TTS",
        "group": "cloud",
        "type": "doubao",
        "zero_gpu": True,
        "streaming": False,
        "pc_audio_supported": True,
        "pc_audio_rate": 48000,
        "env_keys": ["DOUBAO_TTS_APPID", "DOUBAO_TTS_ACCESS_TOKEN"],
        "env_hint": "DOUBAO_TTS_APPID + DOUBAO_TTS_ACCESS_TOKEN（缺省复用 DOUBAO_APPID / DOUBAO_ACCESS_TOKEN）",
        "description": "火山引擎标准语音合成，稳定易配，支持 ESP32 与电脑音响直通。",
        "editable_fields": ["cluster", "voice", "speed_ratio"],
        "preset_models": [],
        "preset_voices": [
            {"value": "BV001_streaming", "label": "通用女声 (BV001_streaming)"},
            {"value": "BV002_streaming", "label": "通用男声 (BV002_streaming)"},
            {"value": "BV700_V2_streaming", "label": "灿灿 2.0 · 温柔甜美"},
            {"value": "BV406_V2_streaming", "label": "梓梓 2.0 · 活泼少女"},
            {"value": "BV701_V2_streaming", "label": "擎苍 2.0 · 沉稳磁性"},
            {"value": "BV102_streaming", "label": "儒雅青年 (BV102_streaming)"},
        ],
        "defaults": {
            "type": "doubao",
            "api_url": "https://openspeech.bytedance.com/api/v1/tts",
            "appid": "${DOUBAO_TTS_APPID}",
            "access_token": "${DOUBAO_TTS_ACCESS_TOKEN}",
            "cluster": "volcano_tts",
            "authorization": "Bearer;",
            "voice": "BV001_streaming",
            "format": "wav",
            "speed_ratio": 1.0,
            "volume_ratio": 1.0,
            "pitch_ratio": 1.0,
            "output_dir": "tmp/",
        },
    },
    "HuoshanDoubleStreamTTS": {
        "label": "火山引擎豆包大模型双流式 V3（高拟人 · 48kHz 音响直通）",
        "short_label": "豆包双流式 V3",
        "group": "cloud",
        "type": "huoshan_double_stream",
        "zero_gpu": True,
        "streaming": True,
        "pc_audio_supported": True,
        "pc_audio_rate": 48000,
        "env_keys": ["DOUBAO_TTS_APPID", "DOUBAO_TTS_ACCESS_TOKEN"],
        "env_hint": "DOUBAO_TTS_APPID + DOUBAO_TTS_ACCESS_TOKEN（缺省复用 DOUBAO_APPID / DOUBAO_ACCESS_TOKEN）",
        "description": "豆包双向流式大模型音色（如湾湾小何），拟人韵律极强，已适配电脑 48kHz 无损音响直通。",
        "editable_fields": ["resource_id", "speaker"],
        "preset_models": [
            {"value": "volc.service_type.10029", "label": "volc.service_type.10029（大模型语音合成 1.0）"},
            {"value": "seed-tts-2.0", "label": "seed-tts-2.0（豆包 2.0）"},
        ],
        "preset_voices": [
            {"value": "zh_female_wanwanxiaohe_moon_bigtts", "label": "湾湾小何 · 台湾腔甜美女声"},
            {"value": "zh_female_shuangkuaisisi_moon_bigtts", "label": "爽快思思 · 自然活泼女声"},
            {"value": "zh_female_linjia_mars_bigtts", "label": "邻家女孩 · 温暖陪伴女声"},
            {"value": "zh_male_wennuanahu_moon_bigtts", "label": "温暖阿虎 · 亲切男声"},
            {"value": "zh_male_shaonianzixin_moon_bigtts", "label": "少年梓辛 · 清朗少年音"},
        ],
        "defaults": {
            "type": "huoshan_double_stream",
            "ws_url": "wss://openspeech.bytedance.com/api/v3/tts/bidirection",
            "appid": "${DOUBAO_TTS_APPID}",
            "access_token": "${DOUBAO_TTS_ACCESS_TOKEN}",
            "resource_id": "volc.service_type.10029",
            "speaker": "zh_female_wanwanxiaohe_moon_bigtts",
            "enable_ws_reuse": True,
            "audio_params": {"speech_rate": 0, "loudness_rate": 0},
            "additions": {"post_process": {"pitch": 0}},
            "output_dir": "tmp/",
        },
    },
    "FishSpeech": {
        "label": "Fish Audio / FishSpeech（高拟真音色克隆 · 48kHz 音响直通）",
        "short_label": "Fish Audio",
        "group": "cloud",
        "type": "fishspeech",
        "zero_gpu": True,
        "streaming": False,
        "pc_audio_supported": True,
        "pc_audio_rate": 48000,
        "env_keys": ["FISHSPEECH_API_KEY"],
        "env_hint": "FISHSPEECH_API_KEY",
        "description": "支持 Fish Audio 云端（填写 reference_id 模型 ID）或自建远程 Fish Speech 服务。",
        "editable_fields": ["api_url", "reference_id", "response_format"],
        "preset_models": [],
        "preset_voices": [],
        "defaults": {
            "type": "fishspeech",
            "api_url": "https://api.fish.audio/v1/tts",
            "api_key": "${FISHSPEECH_API_KEY}",
            "reference_id": "",
            "reference_audio": [],
            "reference_text": [],
            "response_format": "wav",
            "normalize": True,
            "max_new_tokens": 1024,
            "chunk_length": 200,
            "top_p": 0.7,
            "repetition_penalty": 1.2,
            "temperature": 0.7,
            "streaming": False,
            "use_memory_cache": "on",
            "channels": 1,
            "rate": 44100,
            "output_dir": "tmp/",
        },
    },
    "MinimaxTTSHTTPStream": {
        "label": "MiniMax 海螺语音 T2A V2（多情绪高表现 · 44.1kHz 音响直通）",
        "short_label": "MiniMax 海螺",
        "group": "cloud",
        "type": "minimax_httpstream",
        "zero_gpu": True,
        "streaming": True,
        "pc_audio_supported": True,
        "pc_audio_rate": 44100,
        "env_keys": ["MINIMAX_API_KEY", "MINIMAX_GROUP_ID"],
        "env_hint": "MINIMAX_API_KEY + MINIMAX_GROUP_ID",
        "description": "MiniMax 海螺 T2A V2 实时流式语音合成，已适配电脑 44.1kHz 无损音响直通。",
        "editable_fields": ["model", "voice_id", "speed"],
        "preset_models": [
            {"value": "speech-02-turbo", "label": "speech-02-turbo（新一代低延迟 · 推荐）"},
            {"value": "speech-02-hd", "label": "speech-02-hd（新一代高保真）"},
            {"value": "speech-01-turbo", "label": "speech-01-turbo（经典快速）"},
            {"value": "speech-01-hd", "label": "speech-01-hd（经典高保真）"},
        ],
        "preset_voices": [
            {"value": "female-shaonv", "label": "灵动少女 (female-shaonv)"},
            {"value": "female-yujie", "label": "温柔御姐 (female-yujie)"},
            {"value": "female-tianmei", "label": "甜美陪伴 (female-tianmei)"},
            {"value": "female-chengshu", "label": "知性成熟 (female-chengshu)"},
            {"value": "male-qn-qingse", "label": "青涩青年 (male-qn-qingse)"},
            {"value": "male-qn-jingying", "label": "沉稳精英 (male-qn-jingying)"},
        ],
        "defaults": {
            "type": "minimax_httpstream",
            "group_id": "${MINIMAX_GROUP_ID}",
            "api_key": "${MINIMAX_API_KEY}",
            "model": "speech-02-turbo",
            "voice_id": "female-shaonv",
            "output_dir": "tmp/",
        },
    },
    "GPT_SOVITS_V2": {
        "label": "本机 RTX GPU GPT-SoVITS V4（需独立显卡 19880）",
        "short_label": "本机 GPU GSV",
        "group": "gpu",
        "type": "gpt_sovits_v2",
        "zero_gpu": False,
        "streaming": False,
        "pc_audio_supported": True,
        "pc_audio_rate": 48000,
        "env_keys": [],
        "env_hint": "需本机 RTX 独显运行 19880 引擎",
        "description": "支持本机独显自定义模型推理（可选配置 7 情绪动态声线或本地音色复刻）。",
        "editable_fields": ["voice"],
        "preset_models": [],
        "preset_voices": [],
        "defaults": {},
    },
}

ALLOWED_ASR_PROVIDERS = frozenset(ASR_PROVIDERS.keys())
ALLOWED_TTS_PROVIDERS = frozenset(TTS_PROVIDERS.keys())
CLOUD_TTS_PROVIDERS = frozenset(k for k, v in TTS_PROVIDERS.items() if v.get("group") == "cloud")
CLOUD_ASR_PROVIDERS = frozenset(k for k, v in ASR_PROVIDERS.items() if v.get("group") == "cloud")

LOWSPEC_PRESETS: List[Dict[str, Any]] = [
    {
        "id": "siliconflow_edge",
        "label": "极简低配 · 硅基 ASR + 免费 EdgeTTS",
        "desc": "只需 1 个 SILICONFLOW_API_KEY，零本地模型、零显存、零 TTS 费用。",
        "asr_provider": "SiliconFlowASR",
        "tts_provider": "EdgeTTS",
    },
    {
        "id": "siliconflow_full",
        "label": "国内直连 · 硅基流动全家桶",
        "desc": "SenseVoice ASR + CosyVoice2 TTS，共用 SILICONFLOW_API_KEY。",
        "asr_provider": "SiliconFlowASR",
        "tts_provider": "CosyVoiceSiliconflow",
    },
    {
        "id": "dashscope_full",
        "label": "阿里百炼 · Paraformer + CosyVoice 流式",
        "desc": "国内低延迟双向流式，共用 DASHSCOPE_API_KEY，支持 48kHz 电脑音响。",
        "asr_provider": "AliyunBLStreamASR",
        "tts_provider": "AliBLTTS",
    },
    {
        "id": "doubao_full",
        "label": "火山豆包 · 流式 ASR + 双流式 V3",
        "desc": "高拟人度中文对话体验，共用 DOUBAO_APPID / DOUBAO_ACCESS_TOKEN，支持 48kHz 电脑音响。",
        "asr_provider": "DoubaoStreamASR",
        "tts_provider": "HuoshanDoubleStreamTTS",
    },
    {
        "id": "openai_compat",
        "label": "通用 OpenAI 兼容协议",
        "desc": "适配 One-API / New-API / Groq / OpenAI，共用 OPENAI_AUDIO_API_KEY。",
        "asr_provider": "OpenaiASR",
        "tts_provider": "OpenAITTS",
    },
]


def _env_present(name: str) -> bool:
    val = (os.environ.get(name) or "").strip()
    return bool(val and not cloud_audio_patch.is_unexpanded_secret(val))


def _resolve_doubao_creds(kind: str) -> Tuple[str, str]:
    if kind == "asr":
        appid = (os.environ.get("DOUBAO_ASR_APPID") or os.environ.get("DOUBAO_APPID") or "").strip()
        tok = (os.environ.get("DOUBAO_ASR_ACCESS_TOKEN") or os.environ.get("DOUBAO_ACCESS_TOKEN") or "").strip()
    else:
        appid = (os.environ.get("DOUBAO_TTS_APPID") or os.environ.get("DOUBAO_APPID") or "").strip()
        tok = (os.environ.get("DOUBAO_TTS_ACCESS_TOKEN") or os.environ.get("DOUBAO_ACCESS_TOKEN") or "").strip()
    return appid, tok


def is_local_sherpa_available() -> bool:
    sherpa_dir = os.path.join(shared.ROOT_DIR, "models", "sherpa-onnx-sense-voice")
    return os.path.isfile(os.path.join(sherpa_dir, "model.int8.onnx")) and os.path.isfile(
        os.path.join(sherpa_dir, "tokens.txt")
    )


def get_audio_env_status() -> Dict[str, Any]:
    sf_ok = _env_present("SILICONFLOW_API_KEY")
    oai_ok = _env_present("OPENAI_AUDIO_API_KEY")
    ds_ok = _env_present("DASHSCOPE_API_KEY")
    db_asr_id, db_asr_tok = _resolve_doubao_creds("asr")
    db_tts_id, db_tts_tok = _resolve_doubao_creds("tts")
    db_asr_ok = (not cloud_audio_patch.is_unexpanded_secret(db_asr_id)) and (
        not cloud_audio_patch.is_unexpanded_secret(db_asr_tok)
    )
    db_tts_ok = (not cloud_audio_patch.is_unexpanded_secret(db_tts_id)) and (
        not cloud_audio_patch.is_unexpanded_secret(db_tts_tok)
    )
    fish_ok = _env_present("FISHSPEECH_API_KEY")
    mm_ok = _env_present("MINIMAX_API_KEY") and _env_present("MINIMAX_GROUP_ID")
    return {
        "siliconflow_key_ok": sf_ok,
        "openai_audio_key_ok": oai_ok,
        "dashscope_key_ok": ds_ok,
        "doubao_asr_ok": db_asr_ok,
        "doubao_tts_ok": db_tts_ok,
        "fishspeech_key_ok": fish_ok,
        "minimax_ok": mm_ok,
        "local_sherpa_available": is_local_sherpa_available(),
    }


def _is_official_domain(url: str, suffixes: Tuple[str, ...]) -> bool:
    return cloud_audio_patch._is_official_https_domain(url, suffixes)


def _normalized_url_origin(url: str) -> Tuple[str, str, int]:
    parsed = urlparse(str(url or "").strip())
    scheme_low = parsed.scheme.lower()
    host_low = (parsed.hostname or "").strip().lower().rstrip(".")
    try:
        raw_port = parsed.port
    except ValueError:
        raw_port = None
    eff_port = raw_port if raw_port is not None else (443 if scheme_low in ("https", "wss") else 80)
    return scheme_low, host_low, eff_port


def _check_resolved_ip_obj(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    host_label: str,
    *,
    allow_local_gateway: bool,
) -> bool:
    """Validate an IPv4/IPv6 address and return True if it is loopback/private/reserved."""
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    if ip_obj.is_unspecified or ip_obj.is_link_local or ip_obj.is_multicast:
        raise ValueError(f"拒绝指向受限网络地址 ({host_label})")
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _DOCKER_BRIDGE_NET:
        raise ValueError(f"拒绝指向容器内部网桥地址段 ({host_label})")
    is_local_or_priv = bool(ip_obj.is_loopback or ip_obj.is_private or ip_obj.is_reserved)
    if not allow_local_gateway and is_local_or_priv:
        raise ValueError(f"该云端语音通道不允许指向本地回环或内网地址 ({host_label})")
    return is_local_or_priv


def validate_safe_api_url(
    url: str,
    *,
    allow_ws: bool = False,
    allow_local_gateway: bool = True,
) -> str:
    """Strictly validate an audio API URL and block SSRF against internal containers, subnets, non-canonical IPs, DNS mappings, and admin ports (P0-2)."""
    s = str(url or "").strip()
    if not s or len(s) > 512:
        raise ValueError("API URL 不能为空且长度不能超过 512 字符")
    if any(ch.isspace() or ord(ch) < 32 for ch in s):
        raise ValueError("API URL 包含非法空白或控制字符")
    parsed = urlparse(s)
    scheme_low = parsed.scheme.lower()
    allowed_schemes = ("http", "https", "ws", "wss") if allow_ws else ("http", "https")
    if scheme_low not in allowed_schemes:
        raise ValueError(f"API URL 协议不受支持: {parsed.scheme or '(空)'}（仅允许 {', '.join(allowed_schemes)}）")
    if parsed.username or parsed.password:
        raise ValueError("API URL 不允许内嵌用户名或密码，请使用环境变量注入密钥")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("API URL 缺少有效主机名")
    try:
        raw_port = parsed.port
    except ValueError as exc:
        raise ValueError(f"API URL 端口非法: {exc}") from exc
    eff_port = raw_port if raw_port is not None else (443 if scheme_low in ("https", "wss") else 80)

    if host in _BLOCKED_INTERNAL_CONTAINERS:
        raise ValueError(f"拒绝指向容器内部服务主机 ({host})")
    if eff_port in _BLOCKED_INTERNAL_PORTS:
        raise ValueError(f"拒绝指向小智内部服务或管理端口 ({host}:{eff_port})")

    parsed_ip = None
    try:
        parsed_ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        parsed_ip = None

    if parsed_ip is not None:
        _check_resolved_ip_obj(parsed_ip, host, allow_local_gateway=allow_local_gateway)
    else:
        # Block non-canonical integer/hex/octal/shorthand IPv4 encodings (e.g. 2130706433, 0x7f000001, 127.1)
        if _NON_CANONICAL_IP_RE.fullmatch(host) and ("." in host or host.isdigit() or host.startswith("0x")):
            try:
                socket.inet_aton(host)
            except OSError:
                pass
            else:
                raise ValueError(f"拒绝使用非标准 IP 地址编码 ({host})")

        is_local_host = (
            host in _BLOCKED_LOCAL_HOSTNAMES
            or "." not in host
            or host.endswith(".internal")
            or host.endswith(".local")
            or host.endswith(".lan")
        )
        if not allow_local_gateway and is_local_host:
            raise ValueError(f"该云端语音通道不允许指向本地回环或内网地址 ({host})")

        if not is_local_host:
            try:
                addr_infos = socket.getaddrinfo(host, eff_port, type=socket.SOCK_STREAM)
            except OSError:
                addr_infos = []
            for info in addr_infos:
                sockaddr = info[4]
                if not sockaddr:
                    continue
                try:
                    res_ip = ipaddress.ip_address(str(sockaddr[0]).split("%")[0])
                except ValueError:
                    continue
                _check_resolved_ip_obj(res_ip, host, allow_local_gateway=allow_local_gateway)
    return s


def _validate_identifier(value: Any, field_label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"字段 {field_label} 必须为字符串")
    s = value.strip()
    if not s:
        if allow_empty:
            return ""
        raise ValueError(f"字段 {field_label} 不能为空")
    if "${" in s or "}" in s:
        raise ValueError(f"字段 {field_label} 不允许包含未受控模板占位符")
    if not _SAFE_ID_RE.match(s):
        raise ValueError(f"字段 {field_label} 格式非法（仅允许字母、数字及 _ . : / @ -，长度 1~160）")
    return s


def _validate_speed(value: Any, field_label: str, *, lo: float = 0.5, hi: float = 2.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"字段 {field_label} 必须为数字（{lo}~{hi}）")
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"字段 {field_label} 必须为数字（{lo}~{hi}）") from exc
    if not math.isfinite(v) or v < lo or v > hi:
        raise ValueError(f"字段 {field_label} 超出允许范围（{lo}~{hi}）")
    return round(v, 2)


def validate_asr_update(
    asr_provider: Optional[str],
    asr_config: Optional[dict],
    *,
    cur_asr_provider: str,
) -> Tuple[str, Dict[str, Any]]:
    """Validate optional ASR provider + non-secret config overrides."""
    target = cur_asr_provider
    if asr_provider is not None:
        cand = str(asr_provider).strip()
        if not cand or cand not in ALLOWED_ASR_PROVIDERS:
            raise ValueError(
                f"无效的 ASR 识别引擎: {cand or '(空)'}（仅允许: {', '.join(sorted(ALLOWED_ASR_PROVIDERS))}）"
            )
        target = cand

    sanitized: Dict[str, Any] = {}
    if asr_config is not None:
        if not isinstance(asr_config, dict):
            raise ValueError("字段 asr_config 必须为 JSON 对象")
        meta = ASR_PROVIDERS.get(target) or {}
        editable = set(meta.get("editable_fields") or [])
        for raw_k, raw_v in asr_config.items():
            k = str(raw_k).strip()
            if k.lower() in _FORBIDDEN_SECRET_FIELDS:
                raise ValueError(f"安全限制：不允许通过控制台写入敏感密钥字段 {k}，请使用环境变量配置")
            if k not in editable:
                raise ValueError(f"ASR 引擎 {target} 不支持通过控制台修改字段: {k}")
            if k == "base_url":
                allow_local = target == "OpenaiASR"
                validated_u = validate_safe_api_url(
                    str(raw_v or ""), allow_ws=False, allow_local_gateway=allow_local
                )
                if target == "SiliconFlowASR" and not _is_official_domain(
                    validated_u, ("siliconflow.cn", "siliconflow.com")
                ):
                    raise ValueError(
                        "SiliconFlowASR 仅允许官方 siliconflow.cn / siliconflow.com HTTPS 域名；如需第三方中转或自建网关请选用 OpenaiASR"
                    )
                if target == "OpenaiASR" and not cloud_audio_patch._is_local_http_url(validated_u):
                    if urlparse(validated_u).scheme.lower() != "https":
                        raise ValueError("安全限制：非本地内网 OpenaiASR 接口必须使用 HTTPS 协议")
                sanitized[k] = validated_u
            elif k in ("model_name", "model", "cluster", "resource_id"):
                sanitized[k] = _validate_identifier(raw_v, f"asr_config.{k}")
            elif k == "context":
                if not isinstance(raw_v, str):
                    raise ValueError("字段 asr_config.context 必须为字符串")
                ctx_s = raw_v.strip()
                if len(ctx_s) > 400:
                    raise ValueError("字段 asr_config.context 长度不能超过 400 字符")
                sanitized[k] = ctx_s
    return target, sanitized


def validate_tts_cloud_update(
    tts_provider: str,
    tts_config: Optional[dict],
) -> Tuple[str, Dict[str, Any]]:
    """Validate cloud TTS provider + non-secret config overrides."""
    cand = str(tts_provider or "").strip()
    if cand not in CLOUD_TTS_PROVIDERS:
        raise ValueError(
            f"无效的云端 TTS 引擎: {cand or '(空)'}（仅允许: {', '.join(sorted(CLOUD_TTS_PROVIDERS))}）"
        )
    meta = TTS_PROVIDERS[cand]
    editable = set(meta.get("editable_fields") or [])
    sanitized: Dict[str, Any] = {}
    if tts_config is not None:
        if not isinstance(tts_config, dict):
            raise ValueError("字段 tts_config 必须为 JSON 对象")
        for raw_k, raw_v in tts_config.items():
            k = str(raw_k).strip()
            if k.lower() in _FORBIDDEN_SECRET_FIELDS:
                raise ValueError(f"安全限制：不允许通过控制台写入敏感密钥字段 {k}，请使用环境变量配置")
            if k not in editable:
                raise ValueError(f"TTS 引擎 {cand} 不支持通过控制台修改字段: {k}")
            if k == "api_url":
                allow_local = cand in ("OpenAITTS", "FishSpeech")
                validated_u = validate_safe_api_url(
                    str(raw_v or ""), allow_ws=False, allow_local_gateway=allow_local
                )
                if cand == "FishSpeech" and not (
                    _is_official_domain(validated_u, ("fish.audio",))
                    or cloud_audio_patch._is_local_http_url(validated_u)
                ):
                    raise ValueError(
                        "FishSpeech 仅允许官方 fish.audio HTTPS 域名或本地内网 HTTP 自建网关"
                    )
                if cand == "OpenAITTS" and not cloud_audio_patch._is_local_http_url(validated_u):
                    if urlparse(validated_u).scheme.lower() != "https":
                        raise ValueError("安全限制：非本地内网 OpenAITTS 接口必须使用 HTTPS 协议")
                sanitized[k] = validated_u
            elif k in ("model", "voice", "speaker", "voice_id", "cluster", "resource_id"):
                sanitized[k] = _validate_identifier(raw_v, f"tts_config.{k}")
            elif k == "reference_id":
                sanitized[k] = _validate_identifier(raw_v, "tts_config.reference_id", allow_empty=True)
            elif k in ("speed", "speed_ratio", "rate"):
                sanitized[k] = _validate_speed(raw_v, f"tts_config.{k}", lo=0.5, hi=2.0)
            elif k in ("format", "response_format"):
                fmt = str(raw_v or "").strip().lower()
                if fmt not in ("wav", "mp3", "pcm", "opus"):
                    raise ValueError(f"音频格式不受支持: {fmt or '(空)'}（仅允许 wav / mp3 / pcm / opus）")
                sanitized[k] = fmt
    return cand, sanitized


def extract_safe_asr_state(cfg: dict) -> Tuple[str, Dict[str, Any]]:
    selected = (cfg or {}).get("selected_module") or {}
    asr_key = str(selected.get("ASR") or "SherpaASR").strip()
    raw_blk = ((cfg or {}).get("ASR") or {}).get(asr_key) or {}
    meta = ASR_PROVIDERS.get(asr_key) or {}
    defaults = meta.get("defaults") or {}
    editable = list(meta.get("editable_fields") or [])
    out: Dict[str, Any] = {}
    for k in editable:
        val = raw_blk.get(k, defaults.get(k, ""))
        if isinstance(val, str):
            out[k] = val
        elif val is not None:
            out[k] = val
    return asr_key, out


def extract_safe_tts_state(cfg: dict) -> Tuple[str, Dict[str, Any]]:
    selected = (cfg or {}).get("selected_module") or {}
    tts_key = str(selected.get("TTS") or "EdgeTTS").strip()
    raw_blk = ((cfg or {}).get("TTS") or {}).get(tts_key) or {}
    meta = TTS_PROVIDERS.get(tts_key) or {}
    defaults = meta.get("defaults") or {}
    editable = list(meta.get("editable_fields") or [])
    out: Dict[str, Any] = {}
    for k in editable:
        if tts_key == "MinimaxTTSHTTPStream" and k == "speed":
            vs = raw_blk.get("voice_setting") or {}
            val = vs.get("speed", raw_blk.get("speed", 1.0))
        else:
            val = raw_blk.get(k, defaults.get(k, ""))
        if val is not None:
            out[k] = val
    return tts_key, out


def build_audio_providers_payload(cfg: Optional[dict] = None) -> Dict[str, Any]:
    """Build secret-free provider catalog payload for GET /api/audio-providers."""
    cur_cfg = cfg if isinstance(cfg, dict) else shared._read_raw_yaml()
    asr_blocks = (cur_cfg.get("ASR") or {}) if isinstance(cur_cfg, dict) else {}
    tts_blocks = (cur_cfg.get("TTS") or {}) if isinstance(cur_cfg, dict) else {}

    asr_catalog: Dict[str, Any] = {}
    for key, meta in ASR_PROVIDERS.items():
        saved_blk = asr_blocks.get(key) or {}
        safe_saved: Dict[str, Any] = {}
        for fk in meta.get("editable_fields") or []:
            val = saved_blk.get(fk, (meta.get("defaults") or {}).get(fk, ""))
            if val is not None:
                safe_saved[fk] = val
        asr_catalog[key] = {
            "id": key,
            "label": meta["label"],
            "short_label": meta["short_label"],
            "group": meta["group"],
            "type": meta["type"],
            "zero_local_model": bool(meta.get("zero_local_model")),
            "streaming": bool(meta.get("streaming")),
            "env_keys": list(meta.get("env_keys") or []),
            "env_hint": meta.get("env_hint", ""),
            "description": meta.get("description", ""),
            "editable_fields": list(meta.get("editable_fields") or []),
            "preset_models": list(meta.get("preset_models") or []),
            "saved": safe_saved,
        }

    tts_catalog: Dict[str, Any] = {}
    for key, meta in TTS_PROVIDERS.items():
        saved_blk = tts_blocks.get(key) or {}
        safe_saved = {}
        for fk in meta.get("editable_fields") or []:
            if key == "MinimaxTTSHTTPStream" and fk == "speed":
                vs = saved_blk.get("voice_setting") or {}
                val = vs.get("speed", saved_blk.get("speed", 1.0))
            else:
                val = saved_blk.get(fk, (meta.get("defaults") or {}).get(fk, ""))
            if val is not None:
                safe_saved[fk] = val
        tts_catalog[key] = {
            "id": key,
            "label": meta["label"],
            "short_label": meta["short_label"],
            "group": meta["group"],
            "type": meta["type"],
            "zero_gpu": bool(meta.get("zero_gpu")),
            "streaming": bool(meta.get("streaming")),
            "pc_audio_supported": bool(meta.get("pc_audio_supported")),
            "pc_audio_rate": int(meta.get("pc_audio_rate", 48000)),
            "env_keys": list(meta.get("env_keys") or []),
            "env_hint": meta.get("env_hint", ""),
            "description": meta.get("description", ""),
            "editable_fields": list(meta.get("editable_fields") or []),
            "preset_models": list(meta.get("preset_models") or []),
            "preset_voices": list(meta.get("preset_voices") or []),
            "saved": safe_saved,
        }

    return {
        "ok": True,
        "asr_providers": asr_catalog,
        "tts_providers": tts_catalog,
        "lowspec_presets": copy.deepcopy(LOWSPEC_PRESETS),
        "env_status": get_audio_env_status(),
    }


def _merged_provider_cfg(cfg: dict, section: str, provider: str, overrides: Optional[dict]) -> dict:
    catalog = ASR_PROVIDERS if section == "ASR" else TTS_PROVIDERS
    defaults = copy.deepcopy((catalog.get(provider) or {}).get("defaults") or {})
    existing = copy.deepcopy(((cfg or {}).get(section) or {}).get(provider) or {})
    merged = {**defaults, **existing}
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if k.lower() not in _FORBIDDEN_SECRET_FIELDS:
                merged[k] = v
    return merged


def _resolve_secret_from_env(raw_val: Any, env_names: Tuple[str, ...]) -> str:
    for env_name in env_names:
        v = (os.environ.get(env_name) or "").strip()
        if v and not cloud_audio_patch.is_unexpanded_secret(v):
            return v
    cand = str(raw_val or "").strip()
    if cand and not cloud_audio_patch.is_unexpanded_secret(cand):
        return cand
    return ""


async def _preview_alibl_ws(api_key: str, ws_url: str, model: str, voice: str, rate: float, text: str) -> bytes:
    import websockets

    session_id = uuid.uuid4().hex
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-DashScope-DataInspection": "enable",
    }
    audio_chunks = bytearray()
    async with websockets.connect(
        ws_url,
        additional_headers=headers,
        open_timeout=8,
        close_timeout=5,
        max_size=10 * 1024 * 1024,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "header": {"action": "run-task", "task_id": session_id, "streaming": "duplex"},
                    "payload": {
                        "task_group": "audio",
                        "task": "tts",
                        "function": "SpeechSynthesizer",
                        "model": model,
                        "parameters": {
                            "text_type": "PlainText",
                            "voice": voice,
                            "format": "mp3",
                            "sample_rate": 24000,
                            "volume": 50,
                            "rate": rate,
                            "pitch": 1.0,
                        },
                        "input": {},
                    },
                }
            )
        )
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
            if isinstance(msg, str):
                data = json.loads(msg)
                hdr = data.get("header") or {}
                ev = hdr.get("event")
                if ev == "task-started":
                    break
                if ev == "task-failed":
                    raise RuntimeError(f"阿里百炼合成启动失败: {hdr.get('error_code')} - {hdr.get('error_message')}")

        await ws.send(
            json.dumps(
                {
                    "header": {"action": "continue-task", "task_id": session_id, "streaming": "duplex"},
                    "payload": {"input": {"text": text}},
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "header": {"action": "finish-task", "task_id": session_id, "streaming": "duplex"},
                    "payload": {"input": {}},
                }
            )
        )
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
            if isinstance(msg, (bytes, bytearray)):
                audio_chunks.extend(msg)
            elif isinstance(msg, str):
                data = json.loads(msg)
                hdr = data.get("header") or {}
                ev = hdr.get("event")
                if ev == "task-finished":
                    break
                if ev == "task-failed":
                    raise RuntimeError(f"阿里百炼合成失败: {hdr.get('error_code')} - {hdr.get('error_message')}")
    if not audio_chunks:
        raise RuntimeError("阿里百炼未返回有效音频数据")
    return bytes(audio_chunks)


async def _preview_huoshan_ws(
    appid: str,
    access_token: str,
    ws_url: str,
    resource_id: str,
    speaker: str,
    text: str,
) -> bytes:
    import websockets
    from core.providers.tts import huoshan_double_stream as hs

    session_id = uuid.uuid4().hex
    ws_headers = {
        "X-Api-App-Key": appid,
        "X-Api-Access-Key": access_token,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }
    audio_chunks = bytearray()

    def _build_payload(event_id: int, req_text: str = "") -> bytes:
        req: Dict[str, Any] = {
            "user": {"uid": "console-preview"},
            "event": event_id,
            "namespace": "BidirectionalTTS",
            "req_params": {
                "speaker": speaker,
                "audio_params": {"format": "mp3", "sample_rate": 24000, "speech_rate": 0, "loudness_rate": 0},
                "additions": json.dumps({"disable_markdown_filter": True}),
            },
        }
        if req_text:
            req["req_params"]["text"] = req_text
        return json.dumps(req).encode("utf-8")

    async def _send_ev(ws, event_id: int, payload: bytes):
        hdr = hs.Header(
            message_type=hs.FULL_CLIENT_REQUEST,
            message_type_specific_flags=hs.MsgTypeFlagWithEvent,
            serial_method=hs.JSON,
        ).as_bytes()
        opt = hs.Optional(event=event_id, sessionId=session_id).as_bytes()
        frame = bytearray(hdr)
        frame.extend(opt)
        frame.extend(len(payload).to_bytes(4, "big"))
        frame.extend(payload)
        await ws.send(bytes(frame))

    async with websockets.connect(
        ws_url,
        additional_headers=ws_headers,
        open_timeout=8,
        close_timeout=5,
        max_size=10 * 1024 * 1024,
    ) as ws:
        await _send_ev(ws, hs.EVENT_StartSession, _build_payload(hs.EVENT_StartSession))
        await _send_ev(ws, hs.EVENT_TaskRequest, _build_payload(hs.EVENT_TaskRequest, text))
        await _send_ev(ws, hs.EVENT_FinishSession, b"{}")

        parser = hs.TTSProvider.parser_response
        dummy = object()
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
            res = parser(dummy, msg)
            if res.optional.event == hs.EVENT_TTSResponse and res.header.message_type == hs.AUDIO_ONLY_RESPONSE:
                if res.payload:
                    audio_chunks.extend(res.payload)
            elif res.optional.event in (hs.EVENT_SessionFinished, hs.EVENT_SessionCanceled):
                break
            elif res.header.message_type == hs.ERROR_INFORMATION or res.optional.event == hs.EVENT_SessionFailed:
                raise RuntimeError(f"火山双流式合成失败: {res.payload}")

    if not audio_chunks:
        raise RuntimeError("火山双流式未返回有效音频数据")
    return bytes(audio_chunks)


def synthesize_cloud_tts_preview(
    tts_provider: str,
    text: str,
    *,
    tts_config_override: Optional[dict] = None,
    raw_cfg: Optional[dict] = None,
) -> Tuple[bytes, str]:
    """Synthesize preview audio bytes for any supported cloud TTS provider.

    Returns `(audio_bytes, content_type)`. Raises `ValueError` for invalid config/missing keys
    and `RuntimeError` / `TimeoutError` for upstream errors.
    """
    prov, validated_override = validate_tts_cloud_update(tts_provider, tts_config_override)
    cfg = raw_cfg if isinstance(raw_cfg, dict) else shared._read_raw_yaml()
    merged = _merged_provider_cfg(cfg, "TTS", prov, validated_override)
    clip_text = str(text or "").strip()[:120] or "你好，这是一段云端语音合成试听测试。"

    sess = cloud_audio_patch._get_cloud_http_session()

    if prov == "CosyVoiceSiliconflow":
        token = _resolve_secret_from_env(merged.get("access_token"), ("SILICONFLOW_API_KEY",))
        if not token:
            raise ValueError("尚未配置环境变量 SILICONFLOW_API_KEY，无法试听硅基流动 TTS")
        fmt = str(merged.get("response_format") or "wav").strip().lower()
        payload = {
            "model": str(merged.get("model") or "FunAudioLLM/CosyVoice2-0.5B"),
            "input": clip_text,
            "voice": str(merged.get("voice") or "FunAudioLLM/CosyVoice2-0.5B:anna"),
            "response_format": fmt if fmt in ("wav", "mp3", "opus") else "wav",
            "speed": float(merged.get("speed", 1.0) or 1.0),
        }
        resp = sess.post(
            "https://api.siliconflow.cn/v1/audio/speech",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=CLOUD_PREVIEW_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            raise RuntimeError("硅基流动 TTS 接口返回非预期重定向，已拦截")
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"硅基流动 TTS 试听失败 (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.content, _AUDIO_FMT_CONTENT_TYPES.get(payload["response_format"], "audio/wav")

    if prov == "OpenAITTS":
        api_url = validate_safe_api_url(
            str(merged.get("api_url") or "https://api.openai.com/v1/audio/speech"),
            allow_local_gateway=True,
        )
        saved_blk = ((cfg or {}).get("TTS") or {}).get("OpenAITTS") or {}
        saved_url = str(saved_blk.get("api_url") or TTS_PROVIDERS["OpenAITTS"]["defaults"]["api_url"])
        has_url_override = bool(isinstance(validated_override, dict) and "api_url" in validated_override)
        origin_mismatch = bool(
            has_url_override and _normalized_url_origin(api_url) != _normalized_url_origin(saved_url)
        )
        is_local_gw = cloud_audio_patch._is_local_http_url(api_url)
        if not is_local_gw and urlparse(api_url).scheme.lower() != "https":
            raise ValueError("安全限制：非本地内网 OpenAITTS 接口必须使用 HTTPS 协议")

        api_key = "" if origin_mismatch else _resolve_secret_from_env(merged.get("api_key"), ("OPENAI_AUDIO_API_KEY",))
        if origin_mismatch and not is_local_gw:
            raise ValueError("安全限制：试听未保存的新 API 地址时拒绝自动附带服务端环境变量密钥，请先保存配置后再试听")
        if not api_key and not is_local_gw:
            raise ValueError("尚未配置环境变量 OPENAI_AUDIO_API_KEY，无法试听通用 OpenAI 兼容 TTS")
        fmt = str(merged.get("format") or "wav").strip().lower()
        eff_fmt = fmt if fmt in ("wav", "mp3", "opus") else "wav"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": str(merged.get("model") or "tts-1"),
            "input": clip_text,
            "voice": str(merged.get("voice") or "nova"),
            "response_format": eff_fmt,
            "speed": float(merged.get("speed", 1.0) or 1.0),
        }
        resp = sess.post(
            api_url,
            json=payload,
            headers=headers,
            timeout=CLOUD_PREVIEW_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            raise RuntimeError("OpenAI 兼容 TTS 接口返回非预期重定向，已拦截")
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"OpenAI 兼容 TTS 试听失败 (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.content, _AUDIO_FMT_CONTENT_TYPES.get(eff_fmt, "audio/wav")

    if prov == "DoubaoTTS":
        appid, token = _resolve_doubao_creds("tts")
        if not appid or not token or cloud_audio_patch.is_unexpanded_secret(appid) or cloud_audio_patch.is_unexpanded_secret(token):
            raise ValueError("尚未配置火山豆包环境变量 DOUBAO_TTS_APPID/DOUBAO_TTS_ACCESS_TOKEN（或 DOUBAO_APPID/DOUBAO_ACCESS_TOKEN）")
        api_url = validate_safe_api_url(
            str(merged.get("api_url") or "https://openspeech.bytedance.com/api/v1/tts"),
            allow_local_gateway=False,
        )
        if not _is_official_domain(api_url, ("bytedance.com", "volces.com")):
            raise ValueError("安全限制：DoubaoTTS 仅允许官方 bytedance.com / volces.com HTTPS 域名")
        enc = str(merged.get("format") or "wav").strip().lower()
        eff_enc = enc if enc in ("wav", "mp3") else "wav"
        req_json = {
            "app": {
                "appid": appid,
                "token": token,
                "cluster": str(merged.get("cluster") or "volcano_tts"),
            },
            "user": {"uid": "console-preview"},
            "audio": {
                "voice_type": str(merged.get("voice") or "BV001_streaming"),
                "encoding": eff_enc,
                "speed_ratio": float(merged.get("speed_ratio", 1.0) or 1.0),
                "volume_ratio": float(merged.get("volume_ratio", 1.0) or 1.0),
                "pitch_ratio": float(merged.get("pitch_ratio", 1.0) or 1.0),
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": clip_text,
                "text_type": "plain",
                "operation": "query",
                "with_frontend": 1,
                "frontend_type": "unitTson",
            },
        }
        auth_prefix = str(merged.get("authorization") or "Bearer;")
        resp = sess.post(
            api_url,
            data=json.dumps(req_json),
            headers={"Authorization": f"{auth_prefix}{token}", "Content-Type": "application/json"},
            timeout=CLOUD_PREVIEW_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            raise RuntimeError("火山豆包 TTS 接口返回非预期重定向，已拦截")
        body = resp.json() if resp.content else {}
        if isinstance(body, dict) and body.get("data"):
            return base64.b64decode(body["data"]), _AUDIO_FMT_CONTENT_TYPES.get(eff_enc, "audio/wav")
        raise RuntimeError(f"火山豆包 TTS 试听失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    if prov == "FishSpeech":
        import ormsgpack

        api_url = validate_safe_api_url(
            str(merged.get("api_url") or "https://api.fish.audio/v1/tts"),
            allow_local_gateway=True,
        )
        is_official_fish = _is_official_domain(api_url, ("fish.audio",))
        raw_fish_key = _resolve_secret_from_env(merged.get("api_key"), ("FISHSPEECH_API_KEY",))
        if not is_official_fish and not cloud_audio_patch._is_local_http_url(api_url):
            raise ValueError("安全限制：FishSpeech 云端试听仅允许官方 fish.audio HTTPS 域名或本地自建网关")
        api_key = raw_fish_key if is_official_fish else ""
        if not api_key and not cloud_audio_patch._is_local_http_url(api_url):
            raise ValueError("尚未配置环境变量 FISHSPEECH_API_KEY，无法试听 Fish Audio")
        fmt = str(merged.get("response_format") or "wav").strip().lower()
        eff_fmt = fmt if fmt in ("wav", "mp3") else "wav"
        ref_id = str(merged.get("reference_id") or "").strip() or None
        payload = {
            "text": clip_text,
            "references": [],
            "reference_id": ref_id,
            "normalize": True,
            "format": eff_fmt,
            "max_new_tokens": 1024,
            "chunk_length": 200,
            "top_p": 0.7,
            "repetition_penalty": 1.2,
            "temperature": 0.7,
            "streaming": False,
            "use_memory_cache": "on",
        }
        headers = {"Content-Type": "application/msgpack"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = sess.post(
            api_url,
            data=ormsgpack.packb(payload),
            headers=headers,
            timeout=CLOUD_PREVIEW_TIMEOUT_SECONDS + 5.0,
            allow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            raise RuntimeError("Fish Audio 接口返回非预期重定向，已拦截")
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"Fish Audio 试听失败 (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.content, _AUDIO_FMT_CONTENT_TYPES.get(eff_fmt, "audio/wav")

    if prov == "MinimaxTTSHTTPStream":
        api_key = _resolve_secret_from_env(merged.get("api_key"), ("MINIMAX_API_KEY",))
        group_id = _resolve_secret_from_env(merged.get("group_id"), ("MINIMAX_GROUP_ID",))
        if not api_key or not group_id:
            raise ValueError("尚未配置环境变量 MINIMAX_API_KEY 与 MINIMAX_GROUP_ID，无法试听 MiniMax")
        vs = merged.get("voice_setting") if isinstance(merged.get("voice_setting"), dict) else {}
        speed_v = float(merged.get("speed", vs.get("speed", 1.0)) or 1.0)
        payload = {
            "model": str(merged.get("model") or "speech-02-turbo"),
            "text": clip_text,
            "stream": False,
            "voice_setting": {
                "voice_id": str(merged.get("voice_id") or "female-shaonv"),
                "speed": speed_v,
                "vol": 1.0,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
        }
        resp = sess.post(
            f"https://api.minimaxi.com/v1/t2a_v2?GroupId={group_id}",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=CLOUD_PREVIEW_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            raise RuntimeError("MiniMax 接口返回非预期重定向，已拦截")
        body = resp.json() if resp.content else {}
        base_resp = body.get("base_resp") if isinstance(body, dict) else None
        if isinstance(base_resp, dict) and int(base_resp.get("status_code") or 0) != 0:
            raise RuntimeError(f"MiniMax 试听失败: {base_resp.get('status_code')} - {base_resp.get('status_msg')}")
        audio_hex = (body.get("data") or {}).get("audio") if isinstance(body, dict) else None
        if resp.status_code == 200 and audio_hex:
            return bytes.fromhex(audio_hex), "audio/mpeg"
        raise RuntimeError(f"MiniMax 试听失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    if prov == "AliBLTTS":
        api_key = _resolve_secret_from_env(merged.get("api_key"), ("DASHSCOPE_API_KEY",))
        if not api_key:
            raise ValueError("尚未配置环境变量 DASHSCOPE_API_KEY，无法试听阿里云百炼 CosyVoice")
        ws_url = validate_safe_api_url(
            str(merged.get("ws_url") or "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"),
            allow_ws=True,
            allow_local_gateway=False,
        )
        if not _is_official_domain(ws_url, ("aliyuncs.com",)):
            raise ValueError("安全限制：AliBLTTS 仅允许官方 aliyuncs.com WSS 域名")
        model = str(merged.get("model") or "cosyvoice-v2")
        voice = str(merged.get("voice") or "longxiaochun_v2")
        rate = float(merged.get("rate", 1.0) or 1.0)
        mp3_bytes = asyncio.run(
            asyncio.wait_for(
                _preview_alibl_ws(api_key, ws_url, model, voice, rate, clip_text),
                timeout=CLOUD_PREVIEW_TIMEOUT_SECONDS,
            )
        )
        return mp3_bytes, "audio/mpeg"

    if prov == "HuoshanDoubleStreamTTS":
        appid, token = _resolve_doubao_creds("tts")
        if not appid or not token or cloud_audio_patch.is_unexpanded_secret(appid) or cloud_audio_patch.is_unexpanded_secret(token):
            raise ValueError("尚未配置火山豆包环境变量 DOUBAO_TTS_APPID/DOUBAO_TTS_ACCESS_TOKEN（或 DOUBAO_APPID/DOUBAO_ACCESS_TOKEN）")
        ws_url = validate_safe_api_url(
            str(merged.get("ws_url") or "wss://openspeech.bytedance.com/api/v3/tts/bidirection"),
            allow_ws=True,
            allow_local_gateway=False,
        )
        if not _is_official_domain(ws_url, ("bytedance.com", "volces.com")):
            raise ValueError("安全限制：HuoshanDoubleStreamTTS 仅允许官方 bytedance.com / volces.com WSS 域名")
        resource_id = str(merged.get("resource_id") or "volc.service_type.10029")
        speaker = str(merged.get("speaker") or "zh_female_wanwanxiaohe_moon_bigtts")
        mp3_bytes = asyncio.run(
            asyncio.wait_for(
                _preview_huoshan_ws(appid, token, ws_url, resource_id, speaker, clip_text),
                timeout=CLOUD_PREVIEW_TIMEOUT_SECONDS,
            )
        )
        return mp3_bytes, "audio/mpeg"

    raise ValueError(f"不支持试听的云端 TTS 引擎: {prov}")


def _generate_asr_test_wav(duration_sec: float = 0.4, sample_rate: int = 16000) -> bytes:
    """Generate a compact 16kHz 16-bit mono WAV clip in memory for cloud ASR connectivity testing."""
    n_samples = max(1, int(sample_rate * duration_sec))
    frames = bytearray()
    for i in range(n_samples):
        val = int(round(1200.0 * math.sin(2.0 * math.pi * 440.0 * i / float(sample_rate))))
        frames.extend(struct.pack("<h", val))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


async def _probe_alibl_asr_ws(api_key: str, model: str) -> str:
    import websockets

    task_id = uuid.uuid4().hex
    async with websockets.connect(
        "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        additional_headers={"Authorization": f"Bearer {api_key}"},
        open_timeout=8,
        close_timeout=4,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {
                        "task_group": "audio",
                        "task": "asr",
                        "function": "recognition",
                        "model": model,
                        "parameters": {"format": "pcm", "sample_rate": 16000},
                        "input": {},
                    },
                }
            )
        )
        msg = await asyncio.wait_for(ws.recv(), timeout=8.0)
        data = json.loads(msg) if isinstance(msg, str) else {}
        hdr = data.get("header") or {}
        if hdr.get("event") == "task-failed":
            raise RuntimeError(f"百炼流式 ASR 鉴权/启动失败: {hdr.get('error_code')} - {hdr.get('error_message')}")
        await ws.send(
            json.dumps(
                {
                    "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {"input": {}},
                }
            )
        )
        return f"WebSocket 握手与 {model} 任务启动成功"


async def _probe_doubao_stream_asr_ws(appid: str, access_token: str, resource_id: str) -> str:
    import websockets

    headers = {
        "X-Api-App-Key": appid,
        "X-Api-Access-Key": access_token,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }
    async with websockets.connect(
        "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
        additional_headers=headers,
        open_timeout=8,
        close_timeout=4,
    ) as ws:
        req = {
            "user": {"uid": "console-asr-test"},
            "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
            "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True},
        }
        payload_gz = gzip.compress(json.dumps(req).encode("utf-8"))
        hdr = bytearray([0x11, 0x10, 0x11, 0x00])
        hdr.extend(len(payload_gz).to_bytes(4, "big"))
        hdr.extend(payload_gz)
        await ws.send(bytes(hdr))
        resp = await asyncio.wait_for(ws.recv(), timeout=8.0)
        if not resp:
            raise RuntimeError("豆包流式 ASR 未返回握手帧")
        return f"豆包流式 WebSocket 握手成功 ({resource_id})"


def test_asr_connectivity(
    asr_provider: str,
    *,
    asr_config_override: Optional[dict] = None,
    raw_cfg: Optional[dict] = None,
) -> Dict[str, Any]:
    """Probe ASR engine availability / cloud API auth & latency without exposing secrets."""
    cfg = raw_cfg if isinstance(raw_cfg, dict) else shared._read_raw_yaml()
    cur_asr = str(((cfg or {}).get("selected_module") or {}).get("ASR") or "SherpaASR")
    prov, validated_override = validate_asr_update(asr_provider, asr_config_override, cur_asr_provider=cur_asr)
    merged = _merged_provider_cfg(cfg, "ASR", prov, validated_override)
    t0 = time.monotonic()

    if prov == "SherpaASR":
        ok = is_local_sherpa_available()
        return {
            "ok": ok,
            "provider": prov,
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "detail": "本地 Sherpa-ONNX 模型已就位" if ok else "未检测到本地 models/sherpa-onnx-sense-voice/model.int8.onnx（低配电脑推荐切换云端 ASR）",
        }

    if prov == "FunASR":
        pt = os.path.join(shared.ROOT_DIR, "models", "SenseVoiceSmall", "model.pt")
        ok = os.path.isfile(pt)
        return {
            "ok": ok,
            "provider": prov,
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "detail": "本地 FunASR model.pt 已就位" if ok else "未检测到本地 models/SenseVoiceSmall/model.pt",
        }

    if prov in ("SiliconFlowASR", "OpenaiASR"):
        env_key = "SILICONFLOW_API_KEY" if prov == "SiliconFlowASR" else "OPENAI_AUDIO_API_KEY"
        base_url = validate_safe_api_url(
            str(merged.get("base_url") or ""),
            allow_local_gateway=(prov == "OpenaiASR"),
        )
        if prov == "SiliconFlowASR" and not _is_official_domain(base_url, ("siliconflow.cn", "siliconflow.com")):
            raise ValueError("安全限制：SiliconFlowASR 仅允许官方 siliconflow.cn / siliconflow.com HTTPS 域名")

        origin_mismatch = False
        if prov == "OpenaiASR" and isinstance(validated_override, dict) and "base_url" in validated_override:
            saved_blk = ((cfg or {}).get("ASR") or {}).get("OpenaiASR") or {}
            saved_url = str(saved_blk.get("base_url") or ASR_PROVIDERS["OpenaiASR"]["defaults"]["base_url"])
            origin_mismatch = _normalized_url_origin(base_url) != _normalized_url_origin(saved_url)

        is_local_gw = cloud_audio_patch._is_local_http_url(base_url)
        if prov == "OpenaiASR" and not is_local_gw and urlparse(base_url).scheme.lower() != "https":
            raise ValueError("安全限制：非本地内网 OpenaiASR 接口必须使用 HTTPS 协议")

        api_key = "" if origin_mismatch else _resolve_secret_from_env(merged.get("api_key"), (env_key,))
        if origin_mismatch and not is_local_gw:
            raise ValueError("安全限制：测试未保存的新 API 地址时拒绝自动附带服务端环境变量密钥，请先保存配置后再测试")
        if not api_key and not is_local_gw:
            raise ValueError(f"尚未配置环境变量 {env_key}，无法测试 {prov}")
        model_name = str(merged.get("model_name") or "FunAudioLLM/SenseVoiceSmall")
        wav_bytes = _generate_asr_test_wav()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        sess = cloud_audio_patch._get_cloud_http_session()
        resp = sess.post(
            base_url,
            files={"file": ("probe.wav", wav_bytes, "audio/wav")},
            data={"model": model_name},
            headers=headers,
            timeout=CLOUD_ASR_TEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            raise RuntimeError(f"{prov} 接口返回非预期重定向，已拦截")
        latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
        if resp.status_code == 200:
            return {
                "ok": True,
                "provider": prov,
                "latency_ms": latency_ms,
                "detail": f"连接与鉴权成功（model={model_name}，耗时 {latency_ms:.0f}ms）",
            }
        raise RuntimeError(f"ASR 接口返回 HTTP {resp.status_code}: {resp.text[:200]}")

    if prov == "Qwen3ASRFlash":
        import dashscope

        api_key = _resolve_secret_from_env(merged.get("api_key"), ("DASHSCOPE_API_KEY",))
        if not api_key:
            raise ValueError("尚未配置环境变量 DASHSCOPE_API_KEY，无法测试阿里云百炼 Qwen3-ASR-Flash")
        model_name = str(merged.get("model_name") or "qwen3-asr-flash")
        wav_bytes = _generate_asr_test_wav()
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                tf.write(wav_bytes)
                tmp_path = tf.name
            with cloud_audio_patch._dashscope_lock:
                dashscope.api_key = api_key
                resp = dashscope.MultiModalConversation.call(
                    model=model_name,
                    messages=[{"role": "user", "content": [{"audio": tmp_path}]}],
                    result_format="message",
                    stream=False,
                )
            status_code = getattr(resp, "status_code", 200)
            latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
            if status_code == 200:
                return {
                    "ok": True,
                    "provider": prov,
                    "latency_ms": latency_ms,
                    "detail": f"百炼 Qwen3-ASR-Flash 连通成功（耗时 {latency_ms:.0f}ms）",
                }
            raise RuntimeError(f"百炼 Qwen3-ASR-Flash 返回异常 ({status_code}): {getattr(resp, 'message', '')}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    if prov == "AliyunBLStreamASR":
        api_key = _resolve_secret_from_env(merged.get("api_key"), ("DASHSCOPE_API_KEY",))
        if not api_key:
            raise ValueError("尚未配置环境变量 DASHSCOPE_API_KEY，无法测试阿里云百炼流式 ASR")
        model = str(merged.get("model") or "paraformer-realtime-v2")
        detail = asyncio.run(
            asyncio.wait_for(_probe_alibl_asr_ws(api_key, model), timeout=CLOUD_ASR_TEST_TIMEOUT_SECONDS)
        )
        latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
        return {"ok": True, "provider": prov, "latency_ms": latency_ms, "detail": f"{detail}（耗时 {latency_ms:.0f}ms）"}

    if prov in ("DoubaoASR", "DoubaoStreamASR"):
        appid, token = _resolve_doubao_creds("asr")
        if not appid or not token or cloud_audio_patch.is_unexpanded_secret(appid) or cloud_audio_patch.is_unexpanded_secret(token):
            raise ValueError("尚未配置火山豆包 ASR 环境变量 DOUBAO_ASR_APPID/DOUBAO_ASR_ACCESS_TOKEN（或 DOUBAO_APPID/DOUBAO_ACCESS_TOKEN）")
        if prov == "DoubaoStreamASR":
            res_id = str(merged.get("resource_id") or "volc.bigasr.sauc.duration")
            detail = asyncio.run(
                asyncio.wait_for(
                    _probe_doubao_stream_asr_ws(appid, token, res_id),
                    timeout=CLOUD_ASR_TEST_TIMEOUT_SECONDS,
                )
            )
            latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
            return {"ok": True, "provider": prov, "latency_ms": latency_ms, "detail": f"{detail}（耗时 {latency_ms:.0f}ms）"}
        # DoubaoASR v2 handshake probe
        import websockets

        async def _probe_v2():
            async with websockets.connect(
                "wss://openspeech.bytedance.com/api/v2/asr",
                additional_headers={"Authorization": f"Bearer; {token}"},
                open_timeout=8,
                close_timeout=4,
            ):
                return "豆包 v2 ASR WebSocket 鉴权握手成功"

        detail = asyncio.run(asyncio.wait_for(_probe_v2(), timeout=CLOUD_ASR_TEST_TIMEOUT_SECONDS))
        latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
        return {"ok": True, "provider": prov, "latency_ms": latency_ms, "detail": f"{detail}（耗时 {latency_ms:.0f}ms）"}

    raise ValueError(f"不支持测试的 ASR 引擎: {prov}")


def handle_get_audio_providers(handler) -> None:
    handler._send_json(build_audio_providers_payload())


def handle_post_asr_test(handler) -> None:
    try:
        body = handler._read_body_json()
        raw_prov = body.get("asr_provider")
        if raw_prov is not None and not isinstance(raw_prov, str):
            handler._send_json({"ok": False, "error": "字段 asr_provider 必须为字符串"}, HTTPStatus.BAD_REQUEST)
            return
        raw_cfg_ov = body.get("asr_config")
        if raw_cfg_ov is not None and not isinstance(raw_cfg_ov, dict):
            handler._send_json({"ok": False, "error": "字段 asr_config 必须为 JSON 对象"}, HTTPStatus.BAD_REQUEST)
            return
        cfg = shared._read_raw_yaml()
        prov = (raw_prov or str(((cfg or {}).get("selected_module") or {}).get("ASR") or "SherpaASR")).strip()
        res = test_asr_connectivity(prov, asr_config_override=raw_cfg_ov, raw_cfg=cfg)
        handler._send_json(res)
    except shared._BadRequestError:
        raise
    except ValueError as e:
        handler._send_json({"ok": False, "error": shared._redact_secrets(str(e))}, HTTPStatus.BAD_REQUEST)
    except (asyncio.TimeoutError, TimeoutError, requests.exceptions.Timeout, httpx.TimeoutException):
        handler._send_json(
            {"ok": False, "error": f"云端 ASR 接口测试超时（超过 {int(CLOUD_ASR_TEST_TIMEOUT_SECONDS)} 秒），请检查网络或代理设置"},
            HTTPStatus.GATEWAY_TIMEOUT,
        )
    except Exception as e:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(f"ASR 接口测试失败: {e}")},
            HTTPStatus.BAD_GATEWAY,
        )

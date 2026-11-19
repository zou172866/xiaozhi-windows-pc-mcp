"""Doubao clone 2.0 NDJSON protocol. No fallback, retries or instruction tags."""

import base64
import binascii
import json

from .providers import ProviderError

ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
RESOURCE = "seed-icl-2.0"
MAX_AUDIO_BYTES = 25 * 1024 * 1024


def request_body(text: str, speaker: str, model: str) -> dict:
    return {
        "user": {"uid": "companion-engine"},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "model": model,
            "audio_params": {
                "format": "mp3",
                "sample_rate": 24000,
                "bit_rate": 64000,
                "speech_rate": 0,
                "loudness_rate": 0,
            },
        },
    }


def collect_audio(content: bytes) -> tuple[bytes, dict]:
    chunks, total, terminal, usage = [], 0, False, {}
    try:
        for line in content.decode("utf-8").splitlines():
            if not line.strip():
                continue
            if terminal:
                raise ValueError("Data after terminal response")
            record = json.loads(line)
            if not isinstance(record, dict) or type(record.get("code")) is not int:
                raise ValueError("Invalid stream record")
            if record["code"] not in (0, 20000000):
                # Only the numeric code is surfaced; provider messages may echo private input.
                raise ProviderError(f"Doubao code {record['code']}; not retried")
            encoded = record.get("data")
            if encoded not in (None, ""):
                if not isinstance(encoded, str):
                    raise ValueError("Non-string audio")
                data = base64.b64decode("".join(encoded.split()), validate=True)
                if not data:
                    raise ValueError("Empty audio chunk")
                total += len(data)
                if total > MAX_AUDIO_BYTES:
                    raise ValueError("Audio limit exceeded")
                chunks.append(data)
            words = record.get("usage", {})
            words = words.get("text_words") if isinstance(words, dict) else None
            if type(words) is int and words >= 0:
                usage = {"text_words": words}
            terminal = record["code"] == 20000000
        if not terminal or not total:
            raise ValueError("Missing complete audio")
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise ProviderError(
            "Invalid/incomplete Doubao audio; not retried; billing unknown"
        ) from None
    return b"".join(chunks), usage or {"billing": "unknown"}

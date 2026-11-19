"""Optional streaming adapters. Provider keys never leave the server.

Doubao: 16k PCM ASR + 24k PCM clone TTS. Vidu S2: component edition,
managed Volcengine RTC, no second LLM and no connection to Anima's Convex.
"""

import asyncio
import base64
import gzip
import json
import struct
import time
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import httpx

from .doubao import ENDPOINT, RESOURCE, request_body
from .media import endpoint
from .providers import ProviderError


def asr_frame(payload: bytes, sequence: int, *, audio=False, final=False):
    return (
        bytes([0x11, (0x20 if audio else 0x10) | (3 if final else 1), 0 if audio else 0x10, 0])
        + struct.pack(">iI", -sequence if final else sequence, len(payload))
        + payload
    )


def asr_message(raw: bytes):
    if len(raw) < 8 or raw[0] >> 4 != 1:
        raise ProviderError("Invalid ASR frame")
    kind, flags = raw[1] >> 4, raw[1] & 15
    offset = (raw[0] & 15) * 4
    if flags & 1:
        offset += 4
    if kind == 15:
        code = struct.unpack_from(">I", raw, offset)[0]
        raise ProviderError(f"ASR rejected the stream ({code})")
    if len(raw) < offset + 4:
        raise ProviderError("Incomplete ASR frame")
    size = struct.unpack_from(">I", raw, offset)[0]
    body = raw[offset + 4 :]
    if size != len(body) or size > 1024 * 1024:
        raise ProviderError("Invalid ASR payload size")
    if raw[2] & 15 == 1:
        # Streaming responses are bounded both before and after decompression.
        import io

        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            body = stream.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ProviderError("ASR payload too large")
    value = json.loads(body) if raw[2] >> 4 == 1 else {}
    if not isinstance(value, dict):
        raise ProviderError("Invalid ASR result")
    return value


class DoubaoASR:
    def __init__(self, settings, language):
        self.settings, self.language = settings, language
        self.socket = None
        self.sequence = 1

    async def open(self):
        from websockets.asyncio.client import connect

        key = (
            self.settings.call_asr_key.get_secret_value()
            or self.settings.speech_key.get_secret_value()
        )
        self.socket = await connect(
            "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
            additional_headers={
                "X-Api-Key": key,
                "X-Api-App-Key": "PlgvMymc7f3tQnJ6",
                "X-Api-Resource-Id": self.settings.call_asr_resource,
                "X-Api-Connect-Id": str(uuid4()),
                "X-Api-Request-Id": str(uuid4()),
            },
            open_timeout=15,
            close_timeout=3,
            max_size=1024 * 1024,
        )
        body = {
            "user": {"uid": "companion-engine"},
            "audio": {
                "format": "pcm",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
                "language": self.language,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_punc": True,
                "show_utterances": True,
                "end_window_size": 650,
                "result_type": "full",
            },
        }
        await self.socket.send(asr_frame(json.dumps(body).encode(), self.sequence))
        self.sequence += 1
        # Fail before telling the user we're listening if credentials/resource are rejected.
        raw = await asyncio.wait_for(self.socket.recv(), 15)
        asr_message(raw)

    async def send(self, pcm):
        await self.socket.send(asr_frame(pcm, self.sequence, audio=True))
        self.sequence += 1

    async def results(self):
        seen = set()
        last_partial = ""
        async for raw in self.socket:
            data = asr_message(raw)
            result = data.get("result") or {}
            for row in result.get("utterances", []):
                key = (row.get("start_time"), row.get("end_time"), row.get("text"))
                if row.get("definite") and key not in seen and row.get("text", "").strip():
                    seen.add(key)
                    yield {"type": "final", "text": row["text"]}
            partial = [
                r.get("text", "") for r in result.get("utterances", []) if not r.get("definite")
            ]
            text = "".join(partial).strip()
            if text and text != last_partial:
                last_partial = text
                yield {"type": "partial", "text": text}

    async def close(self):
        if self.socket:
            await self.socket.close()


async def stream_speech(settings, voice_id, text, *, transport=None):
    body = request_body(text, voice_id, settings.speech_model)
    body["req_params"]["audio_params"] = {
        "format": "pcm",
        "sample_rate": 24000,
        "speech_rate": 0,
        "loudness_rate": 0,
    }
    terminal = False
    total = 0
    async with httpx.AsyncClient(
        transport=transport, timeout=settings.media_timeout_seconds
    ) as client:
        async with client.stream(
            "POST",
            ENDPOINT,
            json=body,
            headers={
                "X-Api-Key": settings.speech_key.get_secret_value(),
                "X-Api-Resource-Id": RESOURCE,
                "X-Api-Request-Id": str(uuid4()),
            },
        ) as response:
            if response.status_code != 200:
                raise ProviderError(f"Speech HTTP {response.status_code}; not retried")
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                if len(line) > 2 * 1024 * 1024 or terminal:
                    raise ProviderError("Invalid speech stream")
                row = json.loads(line)
                if row.get("code") not in (0, 20000000):
                    raise ProviderError(f"Speech code {row.get('code')}; not retried")
                if row.get("data"):
                    pcm = base64.b64decode(row["data"], validate=True)
                    total += len(pcm)
                    if len(pcm) % 2 or total > 24_000 * 2 * 180:
                        raise ProviderError("Invalid or oversized speech audio")
                    for offset in range(0, len(pcm), 4800):
                        yield pcm[offset : offset + 4800]
                terminal = row.get("code") == 20000000
    if not terminal or not total:
        raise ProviderError("Speech stream incomplete; billing unknown")


def avatar_signal(kind, seq, text=""):
    payload = (
        {"conn_init": {"version": 1}}
        if kind == 1
        else {"hangup": {"hangup_reason": "client_hangup"}}
        if kind == 5
        else {
            "text_msg": {
                "msg_id": str(uuid4()),
                "content": text,
                "timestamp": int(time.time() * 1000),
            }
        }
        if kind in (9, 10)
        else {}
    )
    return json.dumps({"type": kind, "seq_id": seq, "payload": payload})


class ViduAvatar:
    def __init__(self, settings, image, *, on_locator=None):
        self.settings, self.image = settings, image
        self.live_id = None
        self.socket = None
        self.sequence = 0
        self.on_locator = on_locator
        self.create_uncertain = False

    async def open(self):
        from websockets.asyncio.client import connect

        base = endpoint(self.settings.vidu_base_url)
        self.create_uncertain = True
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                base + "/live/s_avatar/component",
                headers={"Authorization": "Token " + self.settings.vidu_key.get_secret_value()},
                json={
                    "model": "vidu-s2",
                    "image_uri": self.image,
                    "managed_rtc": {"provider": "volcengine", "enable_recording": False},
                },
            )
            if response.status_code != 200:
                if 400 <= response.status_code < 500:
                    self.create_uncertain = False
                raise ProviderError(f"Avatar HTTP {response.status_code}; not retried")
            data = response.json()
        self.live_id = str(data.get("live", {}).get("id") or "")
        if self.live_id:
            self.create_uncertain = False
            if self.on_locator:
                self.on_locator(self.live_id)
        if not self.live_id or not data.get("client_secret"):
            raise ProviderError("Avatar create response incomplete; check provider; not retried")
        rtc = data.get("managed_rtc", {})
        if rtc.get("provider") != "volcengine" or not all(
            rtc.get(k) for k in ("app_id", "channel_id", "user_id", "token")
        ):
            raise ProviderError("Vidu must return managed Volcengine RTC credentials")
        parsed = urlparse(base)
        url = (
            "wss://"
            + parsed.netloc
            + "/live/v1/external-lives/"
            + self.live_id
            + "/stream?"
            + urlencode({"conn_id": str(uuid4()), "client_secret": data["client_secret"]})
        )
        self.socket = await connect(url, open_timeout=15, close_timeout=3, max_size=128 * 1024)
        async with asyncio.timeout(90):
            while True:
                await self.signal(1)
                msg = json.loads(await self.socket.recv())
                ack = msg.get("payload", {}).get("conn_init_ack", {})
                if ack.get("success"):
                    break
                if ack.get("error_code") != "NOT_READY":
                    raise ProviderError("Avatar initialization rejected")
                await asyncio.sleep(1)
        return {k: rtc[k] for k in ("provider", "app_id", "channel_id", "user_id", "token")}

    async def signal(self, kind, text=""):
        self.sequence += 1
        if self.socket:
            await self.socket.send(avatar_signal(kind, self.sequence, text))

    async def send(self, pcm):
        await self.socket.send(pcm)

    async def monitor(self):
        async for raw in self.socket:
            if isinstance(raw, str) and json.loads(raw).get("type") == 6:
                raise ProviderError("Avatar provider ended the call")
        raise ProviderError("Avatar connection closed")

    async def close(self):
        if self.create_uncertain and not self.live_id:
            raise ProviderError("Avatar create outcome unknown; check provider console")
        try:
            if self.socket:
                try:
                    await self.signal(5)
                finally:
                    await self.socket.close()
            elif self.live_id:
                from websockets.asyncio.client import connect

                base = urlparse(endpoint(self.settings.vidu_base_url))
                async with connect(
                    "wss://" + base.netloc + "/live/v1/external-lives/" + self.live_id + "/stream",
                    additional_headers={
                        "Authorization": "Token " + self.settings.vidu_key.get_secret_value()
                    },
                    open_timeout=10,
                    close_timeout=3,
                ) as ws:
                    await ws.send(avatar_signal(5, 1))
        except Exception:
            # A provider may already have closed the socket. Verify the authoritative
            # status before deciding cleanup failed; never assume a network error is success.
            if not self.live_id:
                raise ProviderError("Could not stop the avatar") from None
        if self.live_id:
            await self.confirm_stopped()

    async def confirm_stopped(self):
        async with httpx.AsyncClient(timeout=5) as client:
            for attempt in range(3):
                response = await client.get(
                    endpoint(self.settings.vidu_base_url) + "/live/v1/lives/" + self.live_id,
                    headers={"Authorization": "Token " + self.settings.vidu_key.get_secret_value()},
                )
                if response.status_code != 200:
                    raise ProviderError("Cannot confirm avatar cleanup")
                data = response.json()
                status = str(data.get("live", {}).get("status") or data.get("status")).lower()
                if status in {"ended", "failed", "closed"}:
                    return
                if attempt < 2:
                    await asyncio.sleep(1)
        raise ProviderError("Avatar is still active; check provider console")

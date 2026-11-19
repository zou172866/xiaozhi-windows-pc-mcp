import asyncio
import base64
import gzip
import json
import struct

import httpx
import pytest
from starlette.testclient import TestClient

from companion_engine import BrainConfig, CompanionBrain, Settings
from companion_engine.call_providers import ViduAvatar, asr_frame, asr_message, stream_speech
from companion_engine.calls import CallSession
from companion_engine.engine import Conflict
from companion_engine.http_server import create_app
from companion_engine.providers import Completion, ProviderError


class Reader:
    model = "test"

    def complete(self, messages, **kwargs):
        return Completion(
            json.dumps(
                {
                    "assistant_reply": "Tea and a quiet evening sounds good.",
                    "memories": [{"key": "drink", "text": "Prefers tea", "source_quote": "tea"}],
                }
            ),
            self.model,
            None,
        )


class ASR:
    def __init__(self, *_):
        self.input = asyncio.Queue()
        self.closed = False
        self.frames = []

    async def open(self):
        pass

    async def send(self, pcm):
        self.frames.append(pcm)

    async def results(self):
        while not self.closed:
            yield await self.input.get()

    async def close(self):
        self.closed = True


async def speech(*_):
    for _ in range(3):
        yield bytes(4800)


def make(tmp_path, **kwargs):
    b = CompanionBrain(
        tmp_path / "call.sqlite3",
        provider=Reader(),
        settings=Settings(
            reader_enabled=False,
            speech_provider="doubao",
            speech_model="tts",
            speech_key="private",
            call_asr_key="asr",
        ),
        config=BrainConfig(start_worker=False, capture_context=True),
    )
    b.create_character(character_id="rina", name="Rina", persona="Warm", voice_id="voice", **kwargs)
    return b


def test_protocol_and_bounded_error():
    frame = asr_frame(b"ab", 3, audio=True, final=True)
    assert frame[:4] == bytes([0x11, 0x23, 0, 0]) and struct.unpack(">i", frame[4:8])[0] == -3
    body = gzip.compress(json.dumps({"result": {"text": "hello"}}).encode())
    raw = bytes([0x11, 0x91, 0x11, 0]) + struct.pack(">iI", 1, len(body)) + body
    assert asr_message(raw)["result"]["text"] == "hello"
    for raw in (b"", bytes([0x11, 0x90, 0x10, 0]) + struct.pack(">I", 100) + b"{}"):
        with pytest.raises(ProviderError):
            asr_message(raw)
    body = gzip.compress(b"a" * (1024 * 1024 + 1))
    with pytest.raises(ProviderError):
        asr_message(bytes([0x11, 0x90, 0x11, 0]) + struct.pack(">I", len(body)) + body)


def test_real_runtime_call_saves_memory_delivery_and_isolation(tmp_path):
    async def run():
        with make(tmp_path) as b:
            call = CallSession(b.runtime, "rina", asr_factory=ASR, speech=speech)
            await call.open()
            with pytest.raises(Conflict):
                b.chat_sync("rina", "Hello")
            with pytest.raises(Conflict):
                await CallSession(b.runtime, "rina", asr_factory=ASR).open()
            with pytest.raises(RuntimeError):
                b.close()
            with pytest.raises(ValueError):
                await call.send_audio(b"a")
            await call.mute(True)
            await call.send_audio(b"aa")
            assert call.asr.frames == [b"\0\0"]
            await call.mute(False)
            await call.asr.input.put({"type": "final", "text": "I prefer tea"})
            received = []
            async for event in call.events():
                received.append(event)
                if event.type == "audio_end":
                    break
            assert sum(len(e.audio) for e in received) == 14400
            await call.close()
            assert call.asr.closed and b.history("rina")[0]["user_message"] == "I prefer tea"
            assert b.memories("rina")[0].text == "Prefers tea"
            row = b.engine.db.execute("SELECT * FROM call_turns").fetchone()
            assert row["status"] == "audio_sent" and row["audio_bytes"] == 14400
            assert b.engine.db.execute("SELECT status FROM call_sessions").fetchone()[0] == "ended"
            assert not getattr(b.engine, "_live_calls", set())

    asyncio.run(run())


def test_review_policy_stops_without_autoapproval(tmp_path):
    async def run():
        with make(tmp_path, memory_policy="review") as b:
            call = CallSession(b.runtime, "rina", asr_factory=ASR, speech=speech)
            await call.open()
            await call.asr.input.put({"type": "final", "text": "I prefer tea"})
            async for event in call.events():
                if event.type == "closed":
                    assert event.data["reason"] == "review_required"
            assert b.engine.pending_turn("rina") and not b.memories("rina")

    asyncio.run(run())


def test_interruption_and_hangup_do_not_claim_audio_delivered(tmp_path):
    async def slow(*_):
        yield bytes(4800)
        await asyncio.sleep(60)

    async def run():
        with make(tmp_path) as b:
            call = CallSession(b.runtime, "rina", asr_factory=ASR, speech=slow)
            await call.open()
            await call.asr.input.put({"type": "final", "text": "I prefer tea"})
            async for e in call.events():
                if e.audio:
                    break
            await call.interrupt()
            await asyncio.sleep(0.05)
            await call.close()
            row = b.engine.db.execute("SELECT * FROM call_turns").fetchone()
            assert row["status"] == "interrupted" and row["audio_bytes"] == 4800
            assert not [e for e in list(call.queue._queue) if e.audio]

    asyncio.run(run())


def test_tts_stream_requires_terminal_and_valid_pcm():
    async def run():
        settings = Settings(speech_key="x", speech_model="seed-tts-2.0-expressive")

        def transport(terminal):
            rows = [{"code": 0, "data": base64.b64encode(b"\0" * 10000).decode()}]
            if terminal:
                rows.append({"code": 20000000})
            return httpx.MockTransport(
                lambda r: httpx.Response(200, text="\n".join(json.dumps(x) for x in rows))
            )

        chunks = [
            v async for v in stream_speech(settings, "voice", "hello", transport=transport(True))
        ]
        assert b"".join(chunks) == bytes(10000) and max(map(len, chunks)) <= 4800
        with pytest.raises(ProviderError):
            _ = [
                v
                async for v in stream_speech(settings, "voice", "hello", transport=transport(False))
            ]
        avatar = ViduAvatar(settings, "image")
        avatar.create_uncertain = True
        with pytest.raises(ProviderError):
            await avatar.close()

    asyncio.run(run())


def test_websocket_rejects_foreign_origin_and_invalid_auth(tmp_path):
    from starlette.websockets import WebSocketDisconnect

    with make(tmp_path) as b:
        app = create_app(
            b.runtime, "local-token-000000000000000000", include_mcp=False, start_worker=False
        )
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    "/api/call",
                    headers={"host": "127.0.0.1:8765", "origin": "https://evil.example"},
                ):
                    pass
            with client.websocket_connect(
                "/api/call", headers={"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}
            ) as ws:
                ws.send_json({"token": "wrong"})
                with pytest.raises(WebSocketDisconnect):
                    ws.receive_json()


def test_hangup_during_setup_waits_for_and_closes_provider(tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowASR(ASR):
            async def open(self):
                entered.set()
                await release.wait()

        with make(tmp_path) as b:
            call = CallSession(b.runtime, "rina", asr_factory=SlowASR)
            opening = asyncio.create_task(call.open())
            await entered.wait()
            closing = asyncio.create_task(call.close())
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(Conflict):
                await opening
            await closing
            assert call.closed and call.asr.closed
            assert b.call_history("rina")[0]["status"] == "ended"

    asyncio.run(run())


def test_receipts_and_recovery_are_owner_scoped(tmp_path):
    async def run():
        with make(tmp_path) as b:
            with b.engine._transaction():
                b.engine.db.execute(
                    "INSERT INTO call_sessions VALUES(?,?,?,?,?,?,?,?,?)",
                    ("stale", b.engine.owner, "rina", "video", "starting", 0, None, None, None),
                )
            with pytest.raises(Conflict):
                await b.recover_call("stale")
            await b.recover_call("stale", confirm_provider_stopped=True)
            assert b.call_history("rina")[0]["status"] == "ended"
            from companion_engine import NotFound

            with CompanionBrain(
                tmp_path / "call.sqlite3", user_id="other", config=BrainConfig(start_worker=False)
            ) as other:
                with pytest.raises(NotFound):
                    await other.recover_call("stale")

    asyncio.run(run())


def test_avatar_cleanup_checks_provider_state(monkeypatch):
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"live": {"status": "ended"}})

        client = httpx.AsyncClient
        monkeypatch.setattr(
            "companion_engine.call_providers.httpx.AsyncClient",
            lambda **kw: client(transport=httpx.MockTransport(handle), **kw),
        )
        avatar = ViduAvatar(Settings(vidu_key="secret"), "image")
        avatar.live_id = "live-test"
        await avatar.confirm_stopped()
        assert requests[0].url.path == "/live/v1/lives/live-test"
        assert requests[0].headers["Authorization"] == "Token secret"

    asyncio.run(run())


def test_short_user_interrupt_is_not_mistaken_for_echo(tmp_path):
    class OnceASR(ASR):
        async def results(self):
            yield {"type": "final", "text": "no"}

    async def run():
        with make(tmp_path) as b:
            call = CallSession(b.runtime, "rina", asr_factory=OnceASR)
            call.asr = OnceASR()
            call.current_reply = "No rush, we can just chat."
            call.audio_active = True
            await call.read_asr()
            assert call.inputs.get_nowait() == "no"

    asyncio.run(run())

import base64
import json

import httpx
import pytest
from starlette.testclient import TestClient

from companion_engine import CharacterSpec
from companion_engine.engine import Conflict
from companion_engine.http_server import create_app
from companion_engine.media import Media
from companion_engine.providers import Completion, ProviderError
from companion_engine.runtime import Runtime, Settings


class FakeProvider:
    model = "synthetic"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete(self, context):
        self.calls += 1
        return Completion(json.dumps(self.payload), self.model, None)


def test_review_commit_retry_no_duplicate_provider(engine):
    provider = FakeProvider(
        {
            "assistant_reply": "Tea, noted.",
            "memories": [{"key": "drink", "text": "Prefers tea", "source_quote": "tea"}],
        }
    )
    runtime = Runtime(engine, Settings(reader_enabled=False), provider=provider)
    result = runtime.chat("nova", "Please remember tea", "1")
    assert result["needs_review"] and not engine.memories("nova")
    replay = runtime.chat("nova", "Please remember tea", "1")
    assert replay == result and provider.calls == 1
    runtime.approve("nova", result["turn_id"], retain=True)
    assert runtime.chat("nova", "Please remember tea", "1")["replayed"]
    assert len(engine.memories("nova")) == 1 and provider.calls == 1


def test_decline_review_keeps_reply_without_memory(engine):
    runtime = Runtime(
        engine,
        provider=FakeProvider(
            {
                "assistant_reply": "Okay",
                "memories": [{"key": "p", "text": "private", "source_quote": "private"}],
            }
        ),
    )
    result = runtime.chat("nova", "private", "1")
    runtime.approve("nova", result["turn_id"], retain=False)
    assert not engine.memories("nova") and len(engine.history("nova")) == 1


def test_automatic_memory_is_explicit_setting(engine):
    engine.update_character(
        CharacterSpec(id="nova", name="Nova", persona="Hi", memory_policy="automatic")
    )
    runtime = Runtime(
        engine,
        provider=FakeProvider(
            {
                "assistant_reply": "Okay",
                "memories": [{"key": "p", "text": "tea", "source_quote": "tea"}],
            }
        ),
    )
    assert runtime.chat("nova", "tea", "1")["committed"]
    assert len(engine.memories("nova")) == 1


def test_maintenance_resumes_and_marks_processed_not_just_cited(engine):
    from companion_engine import CommitRequest

    turn = engine.prepare_turn("nova", "I live in Boston", request_id="first")
    engine.commit_turn("nova", turn.turn_id, CommitRequest(assistant_reply="Noted"))

    class Writer:
        model = "synthetic"
        calls = 0

        def structured(self, instructions, data):
            self.calls += 1
            parsed = json.loads(data)
            if "new_source" in parsed:
                text = {"description": "Attributed original", "keywords": [], "links": []}
            else:
                eid = next(s["id"] for s in parsed["sources"] if s["speaker"] == "user")
                text = {
                    "episode": "Boston discussed",
                    "core": "Lives in Boston",
                    "episode_source_ids": [eid],
                    "core_source_ids": [eid],
                }
            return Completion(json.dumps(text), self.model, None)

    writer = Writer()
    runtime = Runtime(engine, provider=writer)
    assert runtime.maintain("nova", "maintenance-1")["annotated"] == 2
    assert writer.calls == 3
    assert engine.db.execute("SELECT count(*) FROM maintenance_processed").fetchone()[0] == 2
    assert runtime.maintain("nova", "maintenance-2")["annotated"] == 0
    assert writer.calls == 3  # uncited assistant acknowledgements must not loop forever


def test_reader_expands_originals_then_separate_final_reply(engine):
    from companion_engine import CommitRequest

    old = engine.prepare_turn("nova", "My dog's name is Mochi", request_id="old")
    engine.commit_turn("nova", old.turn_id, CommitRequest(assistant_reply="Lovely name"))

    class Reader(FakeProvider):
        plans = 0

        def structured(self, instructions, data):
            self.plans += 1
            return Completion(
                json.dumps(
                    {"action": "search", "query": "dog Mochi"}
                    if self.plans == 1
                    else {"action": "finish"}
                ),
                self.model,
                None,
            )

        def complete(self, context):
            assert any("Mochi" in s["text"] for s in context.evidence)
            return super().complete(context)

    provider = Reader({"assistant_reply": "Your dog is Mochi."})
    runtime = Runtime(engine, provider=provider)
    assert runtime.chat("nova", "Remind me who my pet is", "new")["committed"]
    assert provider.plans == 2 and provider.calls == 1


def test_reader_unknown_failure_does_not_block_reply(engine):
    class FailingPlanner(FakeProvider):
        def structured(self, *_):
            raise ProviderError("unavailable")

    runtime = Runtime(engine, provider=FailingPlanner({"assistant_reply": "Hello"}))
    assert runtime.chat("nova", "Hello", "1")["committed"]
    assert engine.usage_summary("nova")["unpriced_attempts"] == 2


def test_http_auth_csrf_secrets_and_json_flow(engine):
    runtime = Runtime(engine, provider=FakeProvider({"assistant_reply": "Hello"}))
    token = "x" * 32
    with TestClient(
        create_app(runtime, token, include_mcp=False), base_url="http://127.0.0.1:8765"
    ) as client:
        assert client.get("/").status_code == 401
        assert client.get("/api/bootstrap").status_code == 401
        client.headers["Authorization"] = "Bearer " + token
        assert (
            client.get("/api/bootstrap", headers={"Origin": "https://evil.example"}).status_code
            == 403
        )
        assert client.get("/api/bootstrap", headers={"Host": "evil.example"}).status_code == 403
        result = client.post("/api/settings", json={"settings": {"api_key": "TOP_SECRET"}})
        assert result.status_code == 200 and "TOP_SECRET" not in result.text
        assert "TOP_SECRET" not in client.get("/api/bootstrap").text
        result = client.post(
            "/api/chat", json={"character_id": "nova", "message": "hello", "request_id": "web-1"}
        )
        assert result.json()["reply"] == "Hello"
        assert (
            client.get("/api/conversation?character_id=nova").json()["turns"][0]["assistant_reply"]
            == "Hello"
        )
        assert client.get("/api/chat").status_code == 400


def test_http_mcp_initialize_and_tools(engine):
    token = "y" * 32
    with TestClient(create_app(Runtime(engine), token), base_url="http://127.0.0.1:8765") as client:
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        }
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["serverInfo"]["name"] == "Companion Engine"
        response = client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        assert response.status_code == 200, response.text
        names = {t["name"] for t in response.json()["result"]["tools"]}
        assert {"annotate_evidence", "generate_voice_message", "recall_evidence"} <= names


@pytest.mark.parametrize("vendor", ["elevenlabs", "minimax"])
def test_speech_contract_and_idempotency(engine, vendor):
    engine.update_character(
        CharacterSpec(id="nova", name="Nova", persona="Hello", voice_id="voice_1")
    )
    calls = []

    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload["text"] == "hello"
        if vendor == "elevenlabs":
            assert request.headers["xi-api-key"] == "SECRET"
            return httpx.Response(200, content=b"ID3audio", headers={"Content-Type": "audio/mpeg"})
        assert payload["voice_setting"]["voice_id"] == "voice_1"
        return httpx.Response(
            200,
            json={
                "base_resp": {"status_code": 0},
                "data": {"audio": b"ID3audio".hex()},
                "extra_info": {"usage_characters": 5},
            },
        )

    runtime = Runtime(
        engine,
        Settings(speech_provider=vendor, speech_model="test", speech_key="SECRET"),
        transport=httpx.MockTransport(handler),
    )
    media = Media(runtime)
    a = media.speech("nova", "hello", "same")
    b = media.speech("nova", "hello", "same")
    assert a == b and len(calls) == 1
    path, mime = media.artifact("nova", a["artifact"])
    assert path.read_bytes() == b"ID3audio" and mime == "audio/mpeg"
    with pytest.raises(ValueError):
        media.artifact("nova", "../../secret")
    engine.create_character(CharacterSpec(id="other", name="Other", persona="Other"))
    with pytest.raises(ValueError):
        media.artifact("other", a["artifact"])


def test_image_and_transcription_contract(engine):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path.endswith("/images/generations"):
            assert json.loads(request.content)["quality"] == "medium"
            return httpx.Response(
                200,
                json={
                    "data": [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic").decode()}]
                },
            )
        assert b'filename="recording.webm"' in request.content
        return httpx.Response(200, json={"text": "hello from audio"})

    settings = Settings(
        image_base_url="https://image.example/v1",
        image_model="image-test",
        transcription_base_url="https://audio.example/v1",
        transcription_model="transcript-test",
    )
    runtime = Runtime(engine, settings, transport=httpx.MockTransport(handler))
    media = Media(runtime)
    assert media.image("nova", "A garden", "image-1")["mime"] == "image/png"
    result = media.transcribe("nova", b"recording", "audio/webm", "audio-1")
    assert result["text"] == "hello from audio" and not result["sent_as_message"]
    assert len(seen) == 2


def test_failed_operation_not_automatically_retried(engine):
    runtime = Runtime(engine)

    def fail():
        raise ProviderError("failed")

    with pytest.raises(ProviderError):
        runtime.operation("nova", "test", "1", {}, fail)
    with pytest.raises(Conflict):
        runtime.operation("nova", "test", "1", {}, fail)
    assert runtime.receipts("nova")[0]["status"] == "failed_unknown"


def test_embedding_batch_contract(engine):
    from companion_engine import CommitRequest

    context = engine.prepare_turn("nova", "dog", request_id="1")
    engine.commit_turn("nova", context.turn_id, CommitRequest(assistant_reply="Mochi!"))

    def handler(request):
        texts = json.loads(request.content)["input"]
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": [1, 0]} for i in reversed(range(len(texts)))],
                "usage": {"total_tokens": 5},
            },
        )

    runtime = Runtime(
        engine,
        Settings(embedding_base_url="https://embed.example/v1", embedding_model="embedding-test"),
        transport=httpx.MockTransport(handler),
    )
    assert runtime.index("nova", "batch1")["indexed"] == 2
    assert runtime.recall("nova", "pet")["sources"]

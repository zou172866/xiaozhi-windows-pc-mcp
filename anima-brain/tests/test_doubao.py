import base64
import gzip
import json

import httpx
import pytest

from companion_engine import CharacterSpec
from companion_engine.doubao import collect_audio
from companion_engine.engine import Conflict
from companion_engine.media import Media, post
from companion_engine.providers import ProviderError
from companion_engine.runtime import Runtime, Settings


def wire(*records):
    return "\n".join(json.dumps(record) for record in records).encode()


def test_doubao_contract_receipt_and_replay(engine):
    engine.update_character(
        CharacterSpec(id="nova", name="Nova", persona="Unchanged", voice_id="S_owned_clone")
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
        assert request.headers["X-Api-Key"] == "PRIVATE_TEST_KEY"
        assert request.headers["X-Api-Resource-Id"] == "seed-icl-2.0"
        assert "Authorization" not in request.headers
        payload = json.loads(request.content)["req_params"]
        assert payload["speaker"] == "S_owned_clone"
        assert payload["text"] == "你好。"
        assert payload["model"] == "seed-tts-2.0-expressive"
        assert payload["audio_params"]["format"] == "mp3"
        assert "context_texts" not in payload and "use_tag_parser" not in payload
        return httpx.Response(
            200,
            content=wire(
                {"code": 0, "data": base64.b64encode(b"ID3first").decode()},
                {
                    "code": 20000000,
                    "data": base64.b64encode(b"last").decode(),
                    "usage": {"text_words": 3},
                },
            ),
        )

    runtime = Runtime(
        engine,
        Settings(
            speech_provider="doubao",
            speech_model="seed-tts-2.0-expressive",
            speech_key="PRIVATE_TEST_KEY",
        ),
        transport=httpx.MockTransport(handler),
    )
    media = Media(runtime)
    result = media.speech("nova", "你好。", "one")
    assert media.speech("nova", "你好。", "one") == result
    assert len(calls) == 1
    assert result["usage"] == {"text_words": 3}
    assert media.artifact("nova", result["artifact"])[0].read_bytes() == b"ID3firstlast"


@pytest.mark.parametrize(
    "response",
    [
        b"",
        b"not json",
        b"null",
        b"[]",
        b"1",
        b'{"code":true}',
        wire({"code": 0, "data": base64.b64encode(b"partial").decode()}),
        wire({"code": 20000000}),
        wire({"code": 0, "data": "bad###"}, {"code": 20000000}),
        wire({"code": 0, "data": 42}, {"code": 20000000}),
        wire({"code": 0, "data": "  "}, {"code": 20000000}),
        wire({"code": 20000000, "data": "SUQz"}, {"code": 0}),
        wire({"code": 45000030, "message": "private input must not be echoed"}),
    ],
)
def test_doubao_rejects_partial_or_invalid_records(response):
    with pytest.raises(ProviderError) as caught:
        collect_audio(response)
    assert "private input" not in str(caught.value)


def test_doubao_limits_decoded_audio(monkeypatch):
    monkeypatch.setattr("companion_engine.doubao.MAX_AUDIO_BYTES", 3)
    with pytest.raises(ProviderError):
        collect_audio(wire({"code": 20000000, "data": "SUQzYWJj"}))


def test_doubao_failure_no_saved_partial_and_no_retry(engine):
    engine.update_character(
        CharacterSpec(id="nova", name="Nova", persona="Unchanged", voice_id="S_owned_clone")
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, text="PRIVATE_TEST_KEY must not be printed")

    runtime = Runtime(
        engine,
        Settings(
            speech_provider="doubao",
            speech_model="seed-tts-2.0-expressive",
            speech_key="PRIVATE_TEST_KEY",
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError) as caught:
        Media(runtime).speech("nova", "你好。", "one")
    assert "PRIVATE_TEST_KEY" not in str(caught.value)
    with pytest.raises(Conflict):
        Media(runtime).speech("nova", "你好。", "one")
    assert len(calls) == 1 and not runtime.media_dir.exists()


def test_compressed_provider_response_is_decoded_once():
    raw = wire({"code": 20000000, "data": "SUQz", "usage": {"text_words": 1}})
    compressed = gzip.compress(raw)

    def handler(request):
        return httpx.Response(
            200,
            stream=httpx.ByteStream(compressed),
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
                "X-Tt-Logid": "test-log-id",
            },
        )

    response = post(
        "https://provider.example/test", transport=httpx.MockTransport(handler), json={}
    )
    assert response.content == raw
    assert "Content-Encoding" not in response.headers
    assert int(response.headers["Content-Length"]) == len(raw)
    assert response.headers["X-Tt-Logid"] == "test-log-id"
    assert collect_audio(response.content) == (b"ID3", {"text_words": 1})

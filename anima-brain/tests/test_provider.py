import json

import httpx
import pytest
from pydantic import ValidationError

from companion_engine.generation import draft_reply
from companion_engine.providers import CompatibleProvider, ProviderError


def provider(handler, **kwargs):
    return CompatibleProvider(
        base_url="https://model.example/v1",
        model="test-model",
        api_key="SECRET",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def response(text, usage=True):
    data = {"choices": [{"message": {"content": text}}]}
    if usage:
        data["usage"] = {"prompt_tokens": 100, "completion_tokens": 20}
    return httpx.Response(200, json=data)


def test_provider_preview_and_usage_no_implicit_commit(engine):
    engine.prepare_turn("nova", "hello", request_id="one")

    def handle(request):
        assert request.headers["Authorization"] == "Bearer SECRET"
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 2048
        assert "response_guidance" in payload["messages"][1]["content"]
        return response('{"assistant_reply":"Hello!","memories":[],"appraisal":null}')

    result = draft_reply(
        engine, "nova", provider(handle, input_usd_per_million=1, output_usd_per_million=5)
    )
    assert result["committed"] is False
    assert result["usage"]["estimated_usd"] == pytest.approx(0.0002)
    assert engine.pending_turn("nova")
    assert engine.memories("nova") == []
    assert len(engine.export_character("nova")["provider_usage"]) == 1


def test_invalid_model_json_still_accounts_reported_usage(engine):
    engine.prepare_turn("nova", "hello", request_id="one")
    with pytest.raises(ValidationError):
        draft_reply(engine, "nova", provider(lambda _: response("not JSON")))
    assert len(engine.export_character("nova")["provider_usage"]) == 1
    assert engine.pending_turn("nova")


def test_unknown_usage_is_not_zero(engine):
    engine.prepare_turn("nova", "hello", request_id="one")
    result = draft_reply(
        engine, "nova", provider(lambda _: response('{"assistant_reply":"hi"}', usage=False))
    )
    assert result["usage"] is None and result["usage_status"] == "unknown"
    assert engine.usage_summary("nova")["unpriced_attempts"] == 1
    assert engine.usage_summary("nova")["complete_cost_estimate"] is False


def test_failed_provider_attempt_is_recorded_as_unknown(engine):
    engine.prepare_turn("nova", "hello", request_id="one")
    with pytest.raises(ProviderError):
        draft_reply(engine, "nova", provider(lambda _: httpx.Response(503)))
    assert engine.export_character("nova")["provider_usage"][0]["status"] == "failed_unknown"
    assert engine.usage_summary("nova")["complete_cost_estimate"] is False


def test_provider_error_redaction_no_retry(engine):
    context = engine.prepare_turn("nova", "hello", request_id="one")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="SECRET and private body")

    with pytest.raises(ProviderError) as error:
        provider(handler).complete(context)
    assert "SECRET" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url", ["http://remote.example/v1", "https://key@host/v1", "https://host/v1?key=secret"]
)
def test_endpoint_validation(url):
    with pytest.raises(ValueError):
        CompatibleProvider(base_url=url, model="model")

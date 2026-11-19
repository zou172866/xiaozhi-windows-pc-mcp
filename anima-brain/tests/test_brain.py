import asyncio
import json
import threading

import httpx
import pytest
from starlette.testclient import TestClient

from companion_engine import (
    BrainConfig,
    ChatFailed,
    CompanionBrain,
    Conflict,
    Settings,
)
from companion_engine.http_server import create_app
from companion_engine.providers import Completion, ProviderError


class Model:
    model = "test-model"

    def __init__(self):
        self.contexts = []

    def complete(self, context):
        self.contexts.append(context.model_copy(deep=True))
        payload = {"assistant_reply": "I'm here."}
        if "prefer tea" in context.user_message:
            payload["memories"] = [
                {
                    "key": "drink",
                    "text": "Prefers tea",
                    "source_quote": "prefer tea",
                }
            ]
        elif "drink" in context.user_message:
            assert any(m.key == "drink" for m in context.memories)
            payload["assistant_reply"] = "You prefer tea."
        return Completion(json.dumps(payload), self.model, None)


def brain(path, provider=None, **kwargs):
    return CompanionBrain(
        path, provider=provider or Model(), settings=Settings(reader_enabled=False), **kwargs
    )


def create(app, **kwargs):
    return app.create_character(
        character_id="mika", name="Mika", persona="Warm, direct, curious.", **kwargs
    )


def test_chat_restart_recall_replay_and_owner_isolation(tmp_path):
    path = tmp_path / "brain.sqlite3"
    model = Model()
    with brain(path, model, user_id="alice") as app:
        create(app)
        reply = app.chat_sync("mika", "I prefer tea", request_id="first")
        assert reply.committed and reply.memory_ids and not reply.needs_review
        assert reply.context is None
        assert reply.usage.estimated_usd is None
        assert reply.usage.missing_token_reports == 1
        assert app.runtime.automation.policy("mika").embedding
        assert not app.runtime.automation.policy("mika").host_summaries
    with brain(path, model, user_id="alice") as app:
        reply = asyncio.run(app.chat("mika", "What do I drink?", request_id="second"))
        assert reply.text == "You prefer tea."
        assert reply.generation["memory_ids"]
        assert reply.generation["recent_turns"] == 1
        replay = app.chat_sync("mika", "What do I drink?", request_id="second")
        assert replay.replayed and replay.generation == reply.generation
        assert replay.usage == reply.usage and len(model.contexts) == 2
        with pytest.raises(Conflict):
            app.chat_sync("mika", "different", request_id="second")
    with brain(path, user_id="bob") as app:
        assert app.characters() == []
        create(app)
        assert app.memories("mika") == [] and app.history("mika") == []
    # A successful replay works with no configured provider or network access.
    with CompanionBrain(path, user_id="alice") as app:
        assert app.chat_sync("mika", "I prefer tea", request_id="first").replayed


@pytest.mark.parametrize("retain", [False, True])
def test_review_replays_draft_and_commits_once(tmp_path, retain):
    model = Model()
    with brain(tmp_path / "test.db", model) as app:
        create(app, memory_policy="review")
        reply = app.chat_sync("mika", "Remember I prefer tea", request_id="one")
        assert reply.needs_review and not reply.committed and reply.proposed_memories
        assert app.memories("mika") == [] and app.history("mika") == []
        app.chat_sync("mika", "Remember I prefer tea", request_id="one")
        assert len(model.contexts) == 1
        committed = app.approve("mika", reply.turn_id, retain=retain)
        assert committed.committed and not committed.needs_review
        assert bool(committed.memory_ids) == retain
        assert len(app.history("mika")) == 1 and bool(app.memories("mika")) == retain


def test_generated_reply_survives_commit_failure_without_second_call(tmp_path, monkeypatch):
    model = Model()
    with brain(tmp_path / "test.db", model) as app:
        create(app)
        commit = app.runtime.commit
        monkeypatch.setattr(app.runtime, "commit", lambda *a: (_ for _ in ()).throw(OSError()))
        with pytest.raises(ChatFailed) as failed:
            app.chat_sync("mika", "hello", request_id="one")
        assert failed.value.reply_text == "I'm here."
        assert failed.value.pending_turn_id == app.pending("mika")["id"]
        monkeypatch.setattr(app.runtime, "commit", commit)
        assert app.chat_sync("mika", "hello", request_id="one").committed
        assert len(model.contexts) == 1


def test_uncertain_provider_failure_not_retried_or_hidden(tmp_path):
    class Broken(Model):
        def complete(self, context):
            self.contexts.append(context)
            raise ProviderError("SECRET body must not leak")

    model = Broken()
    with brain(tmp_path / "test.db", model) as app:
        create(app)
        with pytest.raises(ChatFailed) as failed:
            app.chat_sync("mika", "hi", request_id="one")
        assert "SECRET" not in str(failed.value)
        assert failed.value.request_id == "one" and failed.value.reply_text is None
        with pytest.raises(Conflict):
            app.chat_sync("mika", "hi", request_id="one")
        assert len(model.contexts) == 1
        assert app.receipts("mika")[0]["status"] == "failed_unknown"
        app.discard_pending("mika", failed.value.pending_turn_id)
        assert app.pending("mika") is None


def test_options_reach_model_and_capture_is_opt_in(tmp_path):
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.extensions["timeout"]["read"] == 15
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"assistant_reply":"hey"}'}}],
                "usage": {"prompt_tokens": 123, "completion_tokens": 45},
            },
        )

    with CompanionBrain(
        tmp_path / "test.db",
        settings=Settings(
            base_url="https://model.example/v1",
            model="my-model",
            api_key="private-test-key",
            temperature=0.4,
            max_output_tokens=4096,
            timeout_seconds=15,
            reader_max_steps=0,
        ),
        config=BrainConfig(
            capture_context=True,
            recent_turns=1,
            reply_instructions="Use concise Spanish.",
        ),
        transport=httpx.MockTransport(handle),
    ) as app:
        create(app, relationship_enabled=False)
        for i in range(3):
            reply = app.chat_sync("mika", f"hello {i}", request_id=str(i))
        assert len(reply.context["recent_turns"]) == 1
        assert "Spanish" in reply.context["response_guidance"]
        assert "private-test-key" not in reply.model_dump_json()
        assert reply.usage.reported_input_tokens == 123 and reply.usage.attempts == 1
        assert requests[0]["temperature"] == 0.4 and requests[0]["max_tokens"] == 4096


def test_concurrent_replay_across_instances_only_generates_once(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow(Model):
        def complete(self, context):
            entered.set()
            assert release.wait(5)
            return super().complete(context)

    model = Slow()
    with brain(tmp_path / "test.db", model) as first, brain(tmp_path / "test.db", model) as second:
        create(first)

        async def run():
            task = asyncio.create_task(first.chat("mika", "hello", request_id="one"))
            assert await asyncio.to_thread(entered.wait, 5)
            try:
                with pytest.raises(Conflict):
                    await second.chat("mika", "hello", request_id="one")
            finally:
                release.set()
            assert (await task).committed

        asyncio.run(run())
        assert len(model.contexts) == 1


def test_character_and_policy_creation_roll_back_together(tmp_path, monkeypatch):
    with brain(tmp_path / "test.db") as app:
        monkeypatch.setattr(
            app.runtime.automation, "configure", lambda *a: (_ for _ in ()).throw(OSError())
        )
        with pytest.raises(OSError):
            create(app)
        assert app.characters() == []


def test_http_shares_sdk_defaults_and_response_flow(tmp_path):
    with brain(tmp_path / "test.db") as app:
        with TestClient(
            create_app(app.runtime, "test-token-" * 4, include_mcp=False, start_worker=False),
            base_url="http://127.0.0.1:8765",
        ) as client:
            assert client.get("/").status_code == 401
            assert client.get("/api/bootstrap").status_code == 401
            client.headers["Authorization"] = "Bearer " + "test-token-" * 4
            result = client.post(
                "/api/create_character",
                json={
                    "spec": {
                        "id": "mika",
                        "name": "Mika",
                        "persona": "Concise and direct.",
                    }
                },
            )
            assert result.status_code == 200
            assert app.runtime.automation.policy("mika").embedding
            assert not app.runtime.automation.policy("mika").host_summaries
            result = client.post(
                "/api/chat",
                json={
                    "character_id": "mika",
                    "message": "I prefer tea",
                    "request_id": "one",
                },
            )
            assert result.status_code == 200 and result.json()["committed"]
            assert app.memories("mika")


def test_no_ambient_credentials_and_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANION_API_KEY", "do-not-inherit")
    app = CompanionBrain(tmp_path / "test.db")
    assert not app.runtime.settings.api_key.get_secret_value()
    app.close()
    app.close()
    with pytest.raises(RuntimeError, match="closed"):
        app.characters()


def test_missing_configuration_does_not_reserve_or_block_request(tmp_path):
    with CompanionBrain(tmp_path / "test.db") as app:
        create(app)
        with pytest.raises(ChatFailed):
            app.chat_sync("mika", "hi", request_id="one")
        assert not app.receipts("mika") and app.pending("mika") is None
        app.runtime._provider = Model()
        assert app.chat_sync("mika", "hi", request_id="one").committed


def test_canceled_waiter_can_recover_committed_reply(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow(Model):
        def complete(self, context):
            entered.set()
            assert release.wait(5)
            return super().complete(context)

    model = Slow()
    with brain(tmp_path / "test.db", model) as app:
        create(app)

        async def run():
            task = asyncio.create_task(app.chat("mika", "hello", request_id="one"))
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            try:
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
            replay = await app.chat("mika", "hello", request_id="one")
            assert replay.committed and replay.replayed

        asyncio.run(run())
        assert len(model.contexts) == 1


def test_memory_off_keeps_history_but_does_not_create_facts(tmp_path):
    with brain(tmp_path / "test.db") as app:
        create(app, memory_policy="off")
        reply = app.chat_sync("mika", "hello", request_id="one")
        assert reply.committed and app.history("mika") and not app.memories("mika")


def test_invalid_arguments_fail_before_network(tmp_path):
    with pytest.raises(ValueError):
        BrainConfig(recent_turns=-1)
    model = Model()
    with brain(tmp_path / "test.db", model) as app:
        create(app)
        with pytest.raises(ValueError):
            app.chat_sync("mika", "hello", request_id="")
        assert model.contexts == [] and app.pending("mika") is None

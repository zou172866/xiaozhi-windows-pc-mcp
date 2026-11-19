import asyncio
import base64
import json

import httpx
import pytest

from companion_engine import (
    AudioInput,
    BrainConfig,
    ChatFailed,
    CompanionBrain,
    Conflict,
    ImageInput,
    ImageOptions,
    MediaFailed,
    MemoryConfig,
    MemoryDraft,
    Settings,
    SpeechOptions,
    TranscriptionOptions,
)
from companion_engine.context_policy import memory_size
from companion_engine.models import CommitRequest
from companion_engine.providers import Completion

PNG = b"\x89PNG\r\n\x1a\nsynthetic fixture"


class Reader:
    model = "text-only"

    def __init__(self, *, save_visual=False):
        self.contexts = []
        self.save_visual = save_visual
        self.plans = 0

    def structured(self, instructions, data):
        self.plans += 1
        return Completion(
            json.dumps({"action": "search", "query": "jasmine tea"}), self.model, None
        )

    def complete(self, context):
        self.contexts.append(context.model_copy(deep=True))
        out = {"assistant_reply": "That blue jacket works."}
        if self.save_visual:
            observation = context.media_observations[0]
            out["memories"] = [
                {
                    "key": "outfit",
                    "text": "An uploaded image shows a blue jacket.",
                    "source_quote": observation.observation,
                    "source_media_ref": observation.source_ref,
                    "details": {"confidence": 0.6, "subject": "third_party"},
                }
            ]
        return Completion(json.dumps(out), self.model, None)


def make_brain(tmp_path, *, reader=None, handler=None, settings=None, config=None):
    app = CompanionBrain(
        tmp_path / "brain.sqlite3",
        provider=reader or Reader(),
        transport=httpx.MockTransport(handler) if handler else None,
        settings=settings or Settings(reader_enabled=False),
        config=config or BrainConfig(start_worker=False, capture_context=True),
    )
    app.create_character(
        character_id="mika",
        name="Mika",
        persona="Warm and direct.",
        relationship_enabled=False,
        appearance="Short blue hair.",
        voice_id="character-voice",
    )
    return app


def vision_settings():
    return Settings(
        vision_base_url="https://vision.example/v1",
        vision_model="vision-only",
        vision_key="not-a-real-secret",
        reader_enabled=False,
    )


def vision_handler(calls):
    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "vision-only"
        assert request.headers["authorization"] == "Bearer not-a-real-secret"
        parts = body["messages"][1]["content"]
        assert base64.b64decode(parts[1]["image_url"]["url"].split(",")[1]) == PNG
        descriptors = json.loads(parts[0]["text"])["images_in_order"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "observations": [
                                        {
                                            "source_ref": d["source_ref"],
                                            "kind": "image",
                                            "observation": "A blue jacket",
                                            "confidence": 0.7,
                                        }
                                        for d in descriptors
                                    ]
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 10},
            },
        )

    return handle


def test_vision_bytes_to_grounded_memory_and_exact_replay(tmp_path):
    calls, reader = [], Reader(save_visual=True)
    with make_brain(
        tmp_path, reader=reader, handler=vision_handler(calls), settings=vision_settings()
    ) as app:
        image = ImageInput(data=PNG)
        reply = asyncio.run(
            app.chat("mika", "How does this look?", attachments=[image], request_id="photo")
        )
        assert reply.committed and len(reply.memory_ids) == 1
        assert app.memories("mika")[0].origin == "host_observation"
        assert reply.usage.attempts == 2 and reply.usage.reported_input_tokens == 40
        assert reply.generation["media"]["mode"] == "vision_observations"
        assert reply.context["media_observations"][0]["observation"] == "A blue jacket"
        assert "base64" not in json.dumps(reply.model_dump())
        # Config may be removed for successful replay; original bytes establish identity.
        app.runtime.settings = Settings()
        replay = app.chat_sync(
            "mika", "How does this look?", attachments=[image], request_id="photo"
        )
        assert replay.replayed and replay.usage == reply.usage
        for attachments in ([], [ImageInput(data=PNG + b"different")]):
            with pytest.raises(Conflict):
                app.chat_sync(
                    "mika", "How does this look?", attachments=attachments, request_id="photo"
                )
        assert len(calls) == len(reader.contexts) == 1
        stored = " ".join(
            row[0]
            for row in app.engine.db.execute(
                "SELECT result FROM operations WHERE result IS NOT NULL"
            )
        )
        assert base64.b64encode(PNG).decode() not in stored


def test_visual_review_approval_does_not_call_vision_again(tmp_path):
    calls = []
    with make_brain(
        tmp_path,
        reader=Reader(save_visual=True),
        handler=vision_handler(calls),
        settings=vision_settings(),
    ) as app:
        character = app.characters()[0]
        character.memory_policy = "review"
        app.update_character(character)
        reply = app.chat_sync("mika", "Remember this outfit", attachments=[ImageInput(data=PNG)])
        assert reply.needs_review and not app.memories("mika")
        app.runtime.settings = Settings()
        approved = app.approve("mika", reply.turn_id, retain=True)
        assert approved.committed and len(app.memories("mika")) == 1 and len(calls) == 1


def test_missing_vision_and_bad_images_do_not_reserve_or_spend(tmp_path):
    with make_brain(tmp_path) as app:
        with pytest.raises(ChatFailed) as failure:
            app.chat_sync("mika", "look", attachments=[ImageInput(data=PNG)], request_id="missing")
        assert failure.value.pending_turn_id is None
        assert not app.receipts("mika")
        for images in ([ImageInput(data=b"not an image")], [ImageInput(data=PNG)] * 2):
            with pytest.raises(ValueError):
                app.chat_sync("mika", "look", attachments=images)
        assert not app.pending("mika") and not app.receipts("mika")


def test_failed_vision_never_falls_back_to_text_or_retries(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, text="secret-provider-body")

    reader = Reader()
    with make_brain(tmp_path, reader=reader, handler=handler, settings=vision_settings()) as app:
        with pytest.raises(ChatFailed) as failure:
            app.chat_sync("mika", "look", attachments=[ImageInput(data=PNG)], request_id="failed")
        assert "secret" not in str(failure.value)
        with pytest.raises(Conflict):
            app.chat_sync("mika", "look", attachments=[ImageInput(data=PNG)], request_id="failed")
        assert not reader.contexts and len(calls) == 1


def test_read_off_still_stores_history_but_never_retrieves_or_plans(tmp_path, monkeypatch):
    reader = Reader()
    with make_brain(
        tmp_path,
        reader=reader,
        settings=Settings(),
        config=BrainConfig(
            start_worker=False,
            memory=MemoryConfig(read_enabled=False),
            capture_context=True,
        ),
    ) as app:
        app.remember("mika", MemoryDraft(key="tea", text="Loves jasmine tea", source_quote="tea"))
        app.chat_sync("mika", "first")

        def forbidden(*args, **kwargs):
            raise AssertionError("Disabled memory must not be searched")

        monkeypatch.setattr(app.engine.retrieval, "search", forbidden)
        monkeypatch.setattr(app.engine.memory, "recall", forbidden)
        result = app.chat_sync("mika", "tea")
        context = reader.contexts[-1]
        assert context.retrieval_mode == "disabled" and not reader.plans
        assert memory_size(context) == 0
        assert context.recent_turns[0]["user_message"] == "first"
        assert len(app.history("mika")) == 2 and len(app.memories("mika")) == 1
        assert result.committed


def test_final_retrieval_limits_survive_followup_and_budgets(tmp_path):
    reader = Reader()
    config = BrainConfig(
        start_worker=False,
        recent_turns=10,
        recent_chars=100,
        memory=MemoryConfig(fact_limit=2, evidence_limit=1, document_limit=0, context_chars=1800),
    )
    with make_brain(tmp_path, reader=reader, settings=Settings(), config=config) as app:
        for i in range(8):
            app.remember(
                "mika",
                MemoryDraft(key=f"tea-{i}", text=f"Jasmine tea preference {i}", source_quote="tea"),
            )
            context = app.engine.prepare_turn("mika", f"jasmine tea {i}", request_id=f"old-{i}")
            app.engine.commit_turn("mika", context.turn_id, CommitRequest(assistant_reply="Yes"))
        reply = app.chat_sync("mika", "What tea do I like?")
        context = reader.contexts[-1]
        assert reader.plans >= 1
        assert len(context.memories) <= 2 and len(context.evidence) <= 1
        assert not context.core_memory
        assert memory_size(context) <= 1800
        assert len(json.dumps(context.recent_turns, ensure_ascii=False)) <= 100
        assert reply.generation["context_chars"] <= config.context_chars


def media_settings():
    return Settings(
        image_base_url="https://images.example/v1",
        image_model="images",
        speech_provider="minimax",
        speech_model="voice",
        speech_key="secret",
        transcription_base_url="https://audio.example/v1",
        transcription_model="asr",
        media_timeout_seconds=42,
    )


def test_image_options_references_multi_artifact_acl_and_replay(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.extensions["timeout"]["read"] == 42
        if request.url.path.endswith("/edits"):
            assert b'name="image[]"' in request.content
            assert b"Short blue hair" in request.content and PNG in request.content
            assert b"1536x1024" in request.content and b"high" in request.content
        else:
            body = json.loads(request.content)
            assert body["n"] == 2 and body["quality"] == "high"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"b64_json": base64.b64encode(PNG + bytes([i])).decode()} for i in range(2)
                ],
                "usage": {"total_tokens": 12},
            },
        )

    with make_brain(tmp_path, handler=handler, settings=media_settings()) as app:
        opts = ImageOptions(count=2, quality="high", size="1536x1024")
        result = asyncio.run(
            app.generate_image("mika", "At a cafe", options=opts, request_id="new")
        )
        assert len(result.artifacts) == 2 and result.usage["total_tokens"] == 12
        replay = app.generate_image_sync("mika", "At a cafe", options=opts, request_id="new")
        assert replay == result and len(calls) == 1
        app.create_character(character_id="other", name="Other", persona="Other")
        for artifact in result.artifacts:
            assert app.artifact_path("mika", artifact.artifact).read_bytes().startswith(PNG)
            with pytest.raises(ValueError):
                app.artifact_path("other", artifact.artifact)
        edited = app.generate_image_sync(
            "mika", "At a cafe", options=opts, references=[ImageInput(data=PNG)], request_id="ref"
        )
        assert len(edited.artifacts) == 2 and len(calls) == 2
        with pytest.raises(Conflict):
            app.generate_image_sync("mika", "At a cafe", request_id="new")


def test_speech_and_transcription_options_and_no_implicit_chat(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/transcriptions"):
            assert b'name="language"' in request.content and b"en" in request.content
            assert b"Jasmine" in request.content
            return httpx.Response(200, json={"text": "I like jasmine tea"})
        voice = json.loads(request.content)["voice_setting"]
        assert voice == {"voice_id": "override", "speed": 0.8, "vol": 0.6, "pitch": -2}
        return httpx.Response(
            200, json={"base_resp": {"status_code": 0}, "data": {"audio": b"ID3audio".hex()}}
        )

    with make_brain(tmp_path, handler=handler, settings=media_settings()) as app:
        speech = asyncio.run(
            app.speak(
                "mika",
                "Hello",
                options=SpeechOptions(voice_id="override", speed=0.8, volume=0.6, pitch=-2),
            )
        )
        assert speech.artifact.mime == "audio/mpeg"
        audio = AudioInput(data=b"synthetic audio", mime="audio/wav")
        transcript = asyncio.run(
            app.transcribe(
                "mika", audio, options=TranscriptionOptions(language="en", prompt="Jasmine")
            )
        )
        assert transcript.text == "I like jasmine tea" and not transcript.sent_as_message
        assert transcript.source_ref.startswith("sha256:")
        assert not app.history("mika") and not app.memories("mika")
        app.runtime.settings.speech_provider = "elevenlabs"
        with pytest.raises(ValueError, match="require MiniMax"):
            app.speak_sync("mika", "hello", options=SpeechOptions(speed=0.8))
        assert len(calls) == 2


def test_media_failure_returns_request_id_without_provider_body(tmp_path):
    with make_brain(
        tmp_path,
        settings=media_settings(),
        handler=lambda r: httpx.Response(500, text="private-body"),
    ) as app:
        with pytest.raises(MediaFailed) as exc:
            app.generate_image_sync("mika", "cafe")
        assert exc.value.request_id and exc.value.operation == "image"
        assert "private-body" not in str(exc.value)
        assert app.receipts("mika")[0]["status"] == "failed_unknown"


def test_summary_can_use_independent_provider_with_no_chat_credentials(tmp_path):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "summary-only"
        assert request.headers["authorization"] == "Bearer summary-key"
        calls.append(body)
        data = json.loads(body["messages"][1]["content"])
        if "new_source" in data:
            out = {"description": "Tea preference", "keywords": ["tea"], "links": []}
        else:
            ids = [s["id"] for s in data["sources"]]
            out = {
                "episode": "Discussed tea",
                "core": "Prefers jasmine tea",
                "episode_source_ids": ids,
                "core_source_ids": ids,
            }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})

    with make_brain(
        tmp_path,
        handler=handler,
        settings=Settings(
            summary_base_url="https://summary.example/v1",
            summary_model="summary-only",
            summary_key="summary-key",
            reader_enabled=False,
        ),
    ) as app:
        app.chat_sync("mika", "I like jasmine tea")
        result = app.runtime.maintain("mika", "summary")
        assert result["annotated"] == 2 and len(calls) == 3
        assert len(app.engine.memory.documents("mika")) == 2


def test_summary_worker_recognizes_separate_provider(tmp_path):
    from companion_engine import MemoryAutomation

    with CompanionBrain(
        tmp_path / "brain.sqlite3",
        config=BrainConfig(start_worker=False),
        settings=Settings(summary_base_url="https://summary.example/v1", summary_model="writer"),
    ) as app:
        assert app.capabilities()["summaries"] and not app.capabilities()["chat"]
        assert app.runtime.automation.availability(MemoryAutomation(summaries=True)) == []
        app.runtime.settings.summary_model = ""
        assert "summary_provider_missing" in app.runtime.automation.availability(
            MemoryAutomation(summaries=True)
        )


def test_read_enabled_is_independent_of_write_off_and_zero_recent_budget(tmp_path):
    reader = Reader()
    with make_brain(
        tmp_path,
        reader=reader,
        config=BrainConfig(
            start_worker=False,
            recent_chars=0,
        ),
    ) as app:
        spec = app.characters()[0]
        spec.memory_policy = "off"
        app.update_character(spec)
        app.remember("mika", MemoryDraft(key="tea", text="Likes jasmine tea", source_quote="tea"))
        app.chat_sync("mika", "Jasmine tea")
        app.chat_sync("mika", "Tell me about tea")
        assert any(m.key == "tea" for m in reader.contexts[-1].memories)
        assert not reader.contexts[-1].recent_turns
        assert len(app.history("mika")) == 2 and len(app.memories("mika")) == 1


def test_pending_turn_blocks_image_spend_across_instances(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()
    calls = []

    def handler(request):
        entered.set()
        assert release.wait(5)
        return vision_handler(calls)(request)

    with make_brain(tmp_path, handler=handler, settings=vision_settings()) as first:
        with CompanionBrain(
            first.engine.database,
            provider=Reader(),
            settings=vision_settings(),
            transport=httpx.MockTransport(handler),
            config=BrainConfig(start_worker=False),
        ) as second:
            with ThreadPoolExecutor(2) as pool:
                future = pool.submit(
                    first.chat_sync,
                    "mika",
                    "look",
                    attachments=[ImageInput(data=PNG)],
                    request_id="one",
                )
                try:
                    assert entered.wait(5)
                    with pytest.raises(Conflict):
                        second.chat_sync(
                            "mika",
                            "different message",
                            attachments=[ImageInput(data=PNG)],
                            request_id="two",
                        )
                finally:
                    release.set()
                assert future.result(timeout=5).committed
    assert len(calls) == 1


def test_vision_draft_commit_failure_can_recover_without_media_call(tmp_path, monkeypatch):
    calls = []
    with make_brain(
        tmp_path,
        reader=Reader(save_visual=True),
        handler=vision_handler(calls),
        settings=vision_settings(),
    ) as app:
        commit = app.runtime.commit

        def fail(*args, **kwargs):
            raise RuntimeError("simulated local save failure")

        monkeypatch.setattr(app.runtime, "commit", fail)
        with pytest.raises(ChatFailed) as failure:
            app.chat_sync("mika", "look", attachments=[ImageInput(data=PNG)], request_id="save")
        assert failure.value.reply_text and failure.value.pending_turn_id
        monkeypatch.setattr(app.runtime, "commit", commit)
        app.runtime.settings = Settings()
        result = app.chat_sync(
            "mika", "look", attachments=[ImageInput(data=PNG)], request_id="save"
        )
        assert result.committed and len(calls) == 1 and len(app.memories("mika")) == 1


def test_followup_can_replace_full_lanes_instead_of_being_dropped(tmp_path):
    class TargetedReader(Reader):
        def structured(self, instructions, data):
            self.plans += 1
            if self.plans == 1:
                initial = json.loads(data)
                assert initial["memories"][0]["key"] == "tea"
                out = {"action": "search", "query": "Mochi cat"}
            else:
                out = {"action": "finish"}
            return Completion(json.dumps(out), self.model, None)

    reader = TargetedReader()
    with make_brain(
        tmp_path,
        reader=reader,
        settings=Settings(),
        config=BrainConfig(
            start_worker=False,
            recent_turns=0,
            memory=MemoryConfig(fact_limit=1, evidence_limit=1, document_limit=0),
        ),
    ) as app:
        for key, text in (("cat", "My cat is called Mochi"), ("tea", "I prefer jasmine tea")):
            app.remember("mika", MemoryDraft(key=key, text=text, source_quote=text))
            context = app.engine.prepare_turn("mika", text, request_id=key)
            app.engine.commit_turn("mika", context.turn_id, CommitRequest(assistant_reply="Okay"))
        app.chat_sync("mika", "jasmine tea")
        delivered = reader.contexts[-1]
        assert [m.key for m in delivered.memories] == ["cat"]
        assert len(delivered.evidence) == 1 and "Mochi" in delivered.evidence[0]["text"]

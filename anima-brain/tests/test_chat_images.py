"""Real chat/storage boundary with deterministic text and image providers (no paid calls)."""

import base64
import json

import httpx
import pytest
from starlette.testclient import TestClient

from companion_engine import BrainConfig, CompanionBrain, Conflict, ImageInput, Settings
from companion_engine.http_server import create_app
from companion_engine.media import Media
from companion_engine.providers import Completion

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5l8AAAAASUVORK5CYII="
)
TOKEN = "test-appearance-" * 4


class Reader:
    model = "fixture"

    def __init__(self):
        self.calls = 0

    def complete(self, context):
        self.calls += 1
        request = (
            {"prompt": "A casual character selfie", "request_quote": "发个自拍"}
            if "发个自拍" in context.user_message
            else None
        )
        payload = {"assistant_reply": "好呀", "image_request": request}
        if "tea" in context.user_message:
            payload["memories"] = [{"key": "drink", "text": "Likes tea", "source_quote": "tea"}]
        return Completion(json.dumps(payload), self.model, None)


def brain(path, calls, *, enabled=True, fail=False):
    def provider(request):
        calls.append(request)
        return (
            httpx.Response(503, text="private-provider-details")
            if fail
            else httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        )

    return CompanionBrain(
        path,
        settings=Settings(
            reader_enabled=False, image_base_url="https://images.example/v1", image_model="image"
        ),
        provider=Reader(),
        transport=httpx.MockTransport(provider),
        config=BrainConfig(start_worker=False, allow_chat_images=enabled, capture_context=True),
    )


def create(app, character_id="rina", **kwargs):
    app.create_character(character_id=character_id, name="Rina", persona="Friendly", **kwargs)


def test_chat_reference_pixels_persist_replay_and_review(tmp_path):
    calls = []
    path = tmp_path / "brain.sqlite3"
    with brain(path, calls) as app:
        create(app, memory_policy="review")
        reference = app.set_appearance_reference("rina", ImageInput(data=PNG))
        reply = app.chat_sync("rina", "I like tea, 给我发个自拍", request_id="selfie")
        assert reply.needs_review and reply.image_result["status"] == "succeeded"
        assert len(calls) == 1 and calls[0].url.path == "/v1/images/edits"
        assert PNG in calls[0].content and b'name="image[]"' in calls[0].content
        assert "base64" not in json.dumps(reply.model_dump())
        assert (
            app.chat_sync("rina", "I like tea, 给我发个自拍", request_id="selfie").image_result
            == reply.image_result
        )
        approved = app.approve("rina", reply.turn_id, retain=False)
        assert approved.image_result == reply.image_result
        assert app.history("rina")[0]["image_result"] == reply.image_result
        assert app.runtime.inspect_turn("rina", reply.turn_id)["image_result"] == reply.image_result
    with brain(path, calls) as app:
        assert app.appearance_reference("rina") == reference
        assert app.history("rina")[0]["image_result"] == reply.image_result
        replay = app.chat_sync("rina", "I like tea, 给我发个自拍", request_id="selfie")
        assert replay.replayed and replay.image_result == reply.image_result
        assert len(calls) == 1
        assert app.runtime.provider().calls == 0


@pytest.mark.parametrize(
    "enabled,appearance,status",
    [(False, "Blue hair", "unavailable"), (True, "", "needs_appearance")],
)
def test_unavailable_or_missing_appearance_does_not_call_provider(
    tmp_path, enabled, appearance, status
):
    calls = []
    with brain(tmp_path / "brain.sqlite3", calls, enabled=enabled) as app:
        create(app, appearance=appearance)
        reply = app.chat_sync("rina", "发个自拍")
        assert reply.image_result["status"] == status and not calls


def test_normal_chat_no_image_and_description_only_generation(tmp_path):
    calls = []
    with brain(tmp_path / "brain.sqlite3", calls) as app:
        create(app, appearance="Short blue hair")
        assert app.chat_sync("rina", "Hello").image_result is None and not calls
        reply = app.chat_sync("rina", "发个自拍")
        assert reply.image_result["status"] == "succeeded" and len(calls) == 1
        assert calls[0].url.path == "/v1/images/generations"
        assert "Short blue hair" in json.loads(calls[0].content)["prompt"]


def test_provider_failure_is_visible_and_not_retried(tmp_path):
    calls = []
    with brain(tmp_path / "brain.sqlite3", calls, fail=True) as app:
        create(app, appearance="Short blue hair")
        reply = app.chat_sync("rina", "发个自拍", request_id="image-error")
        assert reply.committed and reply.image_result["status"] == "failed"
        assert "private-provider-details" not in json.dumps(reply.model_dump())
        replay = app.chat_sync("rina", "发个自拍", request_id="image-error")
        assert replay.image_result == reply.image_result and len(calls) == 1


def test_reference_upload_is_scoped_and_atomic(tmp_path, monkeypatch):
    with brain(tmp_path / "brain.sqlite3", []) as app:
        create(app)
        create(app, "other")
        api = create_app(app.runtime, TOKEN, include_mcp=False, start_worker=False)
        with TestClient(api, base_url="http://127.0.0.1:8765") as client:
            client.headers["Authorization"] = "Bearer " + TOKEN
            spec = app.characters()[0].model_dump()
            spec["id"] = "rina"
            body = {"spec": spec, "reference_image": base64.b64encode(PNG).decode()}
            assert client.post("/api/update_character", json=body).status_code == 200
            ref = app.appearance_reference("rina")
            url = "/media/" + ref["artifact"]
            assert client.get(url + "?character_id=rina").content == PNG
            assert client.get(url + "?character_id=other").status_code == 404
            client.headers.pop("Authorization")
            assert client.get(url + "?character_id=rina").status_code == 401
            client.headers["Authorization"] = "Bearer " + TOKEN
            original = app.characters()
            body["spec"]["name"] = "Must roll back"
            body["reference_image"] = base64.b64encode(b"bad image").decode()
            assert client.post("/api/update_character", json=body).status_code == 400
            assert app.characters() == original
            assert app.appearance_reference("rina") == ref
            body["reference_image"] = base64.b64encode(PNG).decode()
            save = Media._save

            def disk_full(*args, **kwargs):
                raise OSError("Disk full")

            monkeypatch.setattr(Media, "_save", disk_full)
            # Direct handler test client propagates an unexpected disk failure; no DB edits survive.
            with pytest.raises(OSError):
                client.post("/api/update_character", json=body)
            assert app.characters() == original
            assert client.get(url + "?character_id=rina").content == PNG
            monkeypatch.setattr(Media, "_save", save)
            assert client.post("/api/update_character", json=body).status_code == 200
            assert app.appearance_reference("rina")["artifact"] != ref["artifact"]
            assert not (app.runtime.media_dir / ref["artifact"]).exists()
            body["reference_image"] = None
            assert client.post("/api/update_character", json=body).status_code == 200
            assert app.appearance_reference("rina") is None


def test_pending_turn_prevents_reference_changes(tmp_path):
    with brain(tmp_path / "brain.sqlite3", []) as app:
        create(app, memory_policy="review")
        app.engine.prepare_turn("rina", "Hello", request_id="pending")
        with pytest.raises(Conflict):
            app.set_appearance_reference("rina", ImageInput(data=PNG))

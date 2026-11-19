"""The optional UI must preserve the headless API's owner and file boundaries."""

import base64
import json

import pytest
from starlette.testclient import TestClient

from companion_engine import BrainConfig, CompanionBrain, Settings
from companion_engine.http_server import create_app
from companion_engine.providers import Completion

TOKEN = "showcase-test-token-" * 3


class Provider:
    model = "fixture"
    calls = 0

    def complete(self, context):
        self.calls += 1
        return Completion(
            json.dumps(
                {
                    "assistant_reply": "Quiet evenings, noted.",
                    "memories": [
                        {
                            "key": "quiet",
                            "text": "Prefers quiet evenings",
                            "source_quote": "quiet evenings",
                            "category": "preference",
                        }
                    ],
                }
            ),
            self.model,
            None,
        )


@pytest.fixture
def setup(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>studio</html>")
    (dist / "assets" / "app.js").write_text("/* built JS */")
    (dist / "assets" / "font.woff").write_bytes(b"font")
    (dist / ".env").write_text("PRIVATE")
    outside = tmp_path / "private.js"
    outside.write_text("PRIVATE")
    (dist / "assets" / "escape.js").symlink_to(outside)
    provider = Provider()
    with CompanionBrain(
        tmp_path / "brain.sqlite3",
        provider=provider,
        settings=Settings(reader_enabled=False),
        config=BrainConfig(start_worker=False, capture_context=True),
    ) as brain:
        brain.create_character(
            character_id="one", name="One", persona="Kind", memory_policy="review"
        )
        brain.create_character(character_id="two", name="Two", persona="Quiet")
        app = create_app(
            brain.runtime, TOKEN, include_mcp=False, start_worker=False, frontend_dir=dist
        )
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            yield brain, client, provider


def test_public_assets_private_api_and_path_boundaries(setup):
    _, client, _ = setup
    assert client.get("/").status_code == 200
    assert client.head("/assets/app.js").status_code == 200
    assert client.get("/assets/font.woff").status_code == 200
    assert client.get("/api/bootstrap").status_code == 401
    assert client.get("/assets/escape.js").status_code == 401
    assert client.get("/", headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.test"}).status_code == 403
    client.headers["Authorization"] = "Bearer " + TOKEN
    for path in ["/.env", "/assets/escape.js", "/assets/%2e%2e/%2e%2e/private.js"]:
        result = client.get(path)
        assert result.status_code == 404 and "PRIVATE" not in result.text
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]
    assert client.get("/api/bootstrap").json()["capabilities"]["chat"]


def test_saved_context_scoped_review_replay_and_memory_revision(setup):
    brain, client, provider = setup
    client.headers["Authorization"] = "Bearer " + TOKEN
    request = {"character_id": "one", "message": "Remember quiet evenings", "request_id": "r1"}
    result = client.post("/api/chat", json=request).json()
    turn_id = result["turn_id"]
    assert result["needs_review"] and not brain.engine.memories("one")
    detail = client.get("/api/turn", params={"character_id": "one", "turn_id": turn_id}).json()
    assert detail["reply"] == "Quiet evenings, noted."
    assert detail["context"]["user_message"] == request["message"]
    assert detail["proposals"]["memories"][0]["text"] == "Prefers quiet evenings"
    assert (
        client.get("/api/turn", params={"character_id": "two", "turn_id": turn_id}).status_code
        == 404
    )
    assert (
        client.post(
            "/api/approve", json={"character_id": "one", "turn_id": turn_id, "retain": True}
        ).status_code
        == 200
    )
    assert client.post("/api/chat", json=request).json()["replayed"]
    assert provider.calls == 1
    memory = client.get("/api/memory_panel?character_id=one").json()["facts"][0]
    correction = {
        "character_id": "one",
        "memory_id": memory["id"],
        "expected_revision": memory["revision"],
        "draft": {
            "key": memory["key"],
            "text": "Prefers lively evenings",
            "source_quote": "Prefers lively evenings",
            "category": "preference",
        },
    }
    changed = client.post("/api/correct_memory", json=correction).json()
    assert changed["origin"] == "manual" and changed["revision"] == memory["revision"] + 1
    assert client.post("/api/correct_memory", json=correction).status_code == 409
    assert client.post(
        "/api/delete_memory",
        json={
            "character_id": "one",
            "memory_id": memory["id"],
            "expected_revision": changed["revision"],
        },
    ).json()["deleted"]
    assert len(brain.engine.history("one")) == 1


@pytest.mark.parametrize(
    "attachments",
    [
        [{"path": "/etc/passwd"}],
        [{"data": "not-base64!"}],
        [{"data": base64.b64encode(b"bad-image").decode()}],
        [{"data": "a"}] * 5,
        "not-a-list",
    ],
)
def test_attachment_rejected_before_any_spend(setup, attachments):
    brain, client, provider = setup
    client.headers["Authorization"] = "Bearer " + TOKEN
    response = client.post(
        "/api/chat",
        json={
            "character_id": "one",
            "message": "An image",
            "request_id": "bad",
            "attachments": attachments,
        },
    )
    assert response.status_code in (400, 422)
    assert provider.calls == 0 and brain.engine.pending_turn("one") is None


def test_loaded_images_forwarded_through_runtime_without_paths(setup, monkeypatch):
    brain, client, _ = setup
    client.headers["Authorization"] = "Bearer " + TOKEN
    seen = []

    def chat(character, message, request_id, *, attachments):
        seen.extend(attachments)
        return {"reply": "Image received"}

    monkeypatch.setattr(brain.runtime, "chat", chat)
    data = b"\x89PNG\r\n\x1a\nfixture"
    response = client.post(
        "/api/chat",
        json={
            "character_id": "one",
            "message": "Look",
            "request_id": "image",
            "attachments": [{"data": base64.b64encode(data).decode(), "detail": "low"}],
        },
    )
    assert response.status_code == 200
    assert seen[0].data == data and seen[0].detail == "low"
    assert seen[0].mime == "image/png"

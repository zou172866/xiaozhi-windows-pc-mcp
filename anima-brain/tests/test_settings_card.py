import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from companion_engine import CommitRequest
from companion_engine import provider_config as config
from companion_engine.mcp_server import create_server
from companion_engine.runtime import Runtime, Settings
from companion_engine.settings_ui import SETTINGS_URI, provider_card


class Vault:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, password):
        self.values[(service, account)] = password

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        del self.values[(service, account)]


def test_keychain_migration_clear_and_failure(tmp_path, monkeypatch):
    original = Settings(speech_key="SECRET_FIXTURE", speech_model="fixture")
    config.save_provider_settings(original)
    vault = Vault()
    monkeypatch.setattr(config, "_keychain", lambda: vault)
    monkeypatch.setenv("COMPANION_SECRET_STORE", "keychain")
    config.save_provider_settings(original)
    raw = config.settings_path().read_text()
    assert "SECRET_FIXTURE" not in raw and "speech_key" not in raw
    assert config.load_provider_settings()["speech_key"] == "SECRET_FIXTURE"
    assert config.storage_status()["saved_secrets"] == "macos_keychain"
    config.save_provider_settings(Settings(speech_key=""))
    assert config.load_provider_settings()["speech_key"] == ""
    assert len(vault.values) == 1
    before = config.settings_path().read_bytes()

    def fail(*args):
        raise RuntimeError("DO_NOT_LEAK_SECRET")

    monkeypatch.setattr(vault, "set_password", fail)
    with pytest.raises(ValueError, match="existing settings unchanged") as error:
        config.save_provider_settings(original)
    assert "DO_NOT_LEAK" not in str(error.value)
    assert config.settings_path().read_bytes() == before


def test_settings_card_and_app_only_launcher(engine):
    r = Runtime(engine, Settings(speech_key="NEVER_RETURN", speech_model="fixture"))
    assert "NEVER_RETURN" not in json.dumps(provider_card(r))

    async def run():
        server = create_server(engine, r)
        tools = await server.list_tools()
        tools = {t.name: t for t in tools}
        assert tools["show_provider_settings"].meta["ui"]["resourceUri"] == SETTINGS_URI
        assert tools["open_local_provider_settings"].meta["ui"]["visibility"] == ["app"]
        assert tools["open_local_provider_settings"].input_schema["properties"] == {}
        result = await server.call_tool("show_provider_settings", {})
        assert result.structured_content["speech"]
        assert "NEVER_RETURN" not in str(result)

    asyncio.run(run())


def test_local_launcher_auth_and_shared_runtime(engine, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "companion_engine.local_settings.webbrowser.open", lambda url: opened.append(url) or True
    )
    r = Runtime(engine, Settings())
    try:
        result = r.local_settings.open()
        assert result == {"opened_on_engine_computer": True, "keys_shared_with_chat": False}
        assert "token" not in json.dumps(result)
        parsed = urlparse(opened[0])
        params = parse_qs(parsed.fragment)
        assert parsed.path == "/settings"
        base = f"{parsed.scheme}://{parsed.netloc}"
        with httpx.Client(base_url=base) as c:
            assert c.get("/settings").status_code == 200
            assert c.get("/settings.js").status_code == 200
            assert c.get("/api/settings").status_code == 401
            c.headers["Authorization"] = "Bearer " + params["token"][0]
            assert (
                c.get("/api/settings", headers={"Origin": "https://untrusted.test"}).status_code
                == 403
            )
            response = c.post(
                "/api/settings",
                json={"settings": {"speech_model": "fixture", "speech_key": "PRIVATE_LOCAL"}},
            )
            assert response.status_code == 200 and "PRIVATE_LOCAL" not in response.text
            assert r.settings.speech_key.get_secret_value() == "PRIVATE_LOCAL"
            assert "PRIVATE_LOCAL" not in c.get("/api/settings").text
            for path in (
                "/",
                "/app.js",
                "/style.css",
                "/api/bootstrap",
                "/api/conversation",
                "/media/example.mp3",
                "/mcp",
            ):
                assert c.get(path).status_code == 404
            assert c.post("/api/chat", json={"message": "hi"}).status_code == 404
        r.local_settings.open()
        assert opened[0] == opened[1]
    finally:
        r.local_settings.close()


def test_removed_chat_assets_and_settings_transport_bounds(engine):
    from pathlib import Path

    from starlette.testclient import TestClient

    import companion_engine
    from companion_engine.http_server import create_app

    static = Path(companion_engine.__file__).parent / "static"
    assert not any((static / name).exists() for name in ("index.html", "app.js", "style.css"))
    assert all(
        (static / name).is_file()
        for name in (
            "voice-player.html",
            "memory-panel.html",
            "provider-settings.html",
            "local-settings.html",
        )
    )
    runtime = Runtime(engine, Settings())
    with pytest.raises(ValueError, match="must not start"):
        create_app(runtime, "x" * 32, settings_only=True)
    with TestClient(
        create_app(runtime, "x" * 32, include_mcp=False), base_url="http://127.0.0.1:8765"
    ) as client:
        client.headers["Authorization"] = "Bearer " + "x" * 32
        for path in ("/", "/app.js", "/style.css", "/settings", "/settings.js"):
            assert client.get(path).status_code == 404


def test_persona_and_guidance_shared_with_host_and_provider(engine):
    from companion_engine.providers import CompatibleProvider

    r = Runtime(engine, Settings())
    ctx = r.prepare("nova", "Can you reflect on this with me?", "guidance")
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": CommitRequest(assistant_reply="Sure").model_dump_json()
                        }
                    }
                ]
            },
        )

    provider = CompatibleProvider(
        base_url="https://example.test/v1", model="fixture", transport=httpx.MockTransport(handle)
    )
    provider.complete(ctx)
    assert sent[0]["messages"][0]["content"].startswith(ctx.response_guidance)
    supplied = json.loads(sent[0]["messages"][1]["content"])
    assert supplied["character"]["persona"] == ctx.character.persona
    assert supplied["response_guidance"] == ctx.response_guidance

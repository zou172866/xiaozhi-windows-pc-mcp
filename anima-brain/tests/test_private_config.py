import asyncio

import pytest

from companion_engine.mcp_server import create_server
from companion_engine.provider_config import load_provider_settings
from companion_engine.runtime import Runtime, Settings


def test_private_credentials_absent_by_default(engine, monkeypatch):
    monkeypatch.delenv("COMPANION_ALLOW_CHAT_CREDENTIALS", raising=False)

    async def run():
        s = create_server(engine, Runtime(engine, Settings()))
        assert "configure_private_provider" not in {t.name for t in await s.list_tools()}

    asyncio.run(run())


def test_private_credentials_persist_apply_and_redact(engine, monkeypatch):
    monkeypatch.setenv("COMPANION_ALLOW_CHAT_CREDENTIALS", "1")
    r = Runtime(engine, Settings(speech_key="OLD", speech_model="old-model"))

    async def run():
        server = create_server(engine, r)
        before = "\n".join(engine.db.iterdump())
        result = await server.call_tool(
            "configure_private_provider",
            {
                "service": "speech",
                "settings": {
                    "api_key": "NEW_PRIVATE_FIXTURE",
                    "model": "new-model",
                    "provider": "doubao",
                },
            },
        )
        assert not result.is_error and result.structured_content["saved"]
        assert not result.structured_content["provider_tested"]
        assert "NEW_PRIVATE_FIXTURE" not in str(result)
        assert r.settings.speech_key.get_secret_value() == "NEW_PRIVATE_FIXTURE"
        assert load_provider_settings()["speech_key"] == "NEW_PRIVATE_FIXTURE"
        assert "\n".join(engine.db.iterdump()) == before
        result = await server.call_tool(
            "configure_private_provider",
            {
                "service": "speech",
                "settings": {"api_key": "", "model": "another-model"},
            },
        )
        assert not result.is_error
        assert r.settings.speech_key.get_secret_value() == "NEW_PRIVATE_FIXTURE"
        skill = await server.call_tool("get_companion_workflow", {})
        assert "configure_private_provider" in str(skill)

    asyncio.run(run())


@pytest.mark.parametrize(
    "service,patch",
    [
        ("speech", {"api_key": "SECRET_BAD", "provider": "SECRET_BAD"}),
        ("chat", {"api_key": "SECRET_BAD", "base_url": "https://example.test?key=SECRET_BAD"}),
        ("embedding", {"api_key": "SECRET_BAD", "surprise": "SECRET_BAD"}),
        ("SECRET_BAD", {"api_key": "SECRET_BAD"}),
        ("image", {"api_key": {"hidden": "SECRET_BAD"}}),
    ],
)
def test_private_credentials_errors_never_echo_or_apply(engine, monkeypatch, service, patch):
    monkeypatch.setenv("COMPANION_ALLOW_CHAT_CREDENTIALS", "1")
    r = Runtime(engine, Settings(speech_key="OLD"))
    before = r.settings.model_dump()

    async def run():
        s = create_server(engine, r)
        result = await s.call_tool(
            "configure_private_provider", {"service": service, "settings": patch}
        )
        assert result.is_error
        assert "SECRET_BAD" not in str(result)
        assert r.settings.model_dump() == before
        assert load_provider_settings() == {}

    asyncio.run(run())


def test_private_credentials_storage_failure_keeps_live_config(engine, monkeypatch):
    monkeypatch.setenv("COMPANION_ALLOW_CHAT_CREDENTIALS", "1")
    r = Runtime(engine, Settings(speech_key="OLD"))

    def fail(_):
        raise OSError("SECRET_IN_BACKEND_ERROR")

    monkeypatch.setattr("companion_engine.provider_config.save_provider_settings", fail)

    async def run():
        s = create_server(engine, r)
        result = await s.call_tool(
            "configure_private_provider",
            {
                "service": "speech",
                "settings": {"api_key": "NEW"},
            },
        )
        assert result.is_error and "SECRET_IN_BACKEND_ERROR" not in str(result)
        assert r.settings.speech_key.get_secret_value() == "OLD"

    asyncio.run(run())

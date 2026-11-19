import asyncio
import base64
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from starlette.testclient import TestClient

from companion_engine import CharacterSpec, CommitRequest, Engine
from companion_engine.cli import main
from companion_engine.http_server import create_app
from companion_engine.media import Media
from companion_engine.plugin_support import (
    CHUNK_BYTES,
    PLAYER_URI,
    voice_card,
    voice_chunk,
    workflow,
)
from companion_engine.provider_config import load_provider_settings, save_provider_settings
from companion_engine.runtime import Runtime, Settings


def saved_audio(engine, audio):
    runtime = Runtime(engine, Settings())
    media = Media(runtime)
    result = runtime.operation(
        "nova",
        "speech",
        "audio-1",
        {"text": "test"},
        lambda: media._save(audio, "audio/mpeg", "mp3", {"network_calls": 0}),
    )
    return media, result


def test_private_audio_chunks_reassemble_and_are_receipt_scoped(engine):
    original = b"ID3" + bytes(range(256)) * 2200
    media, result = saved_audio(engine, original)
    card = voice_card(media, "nova", result["artifact"])
    assert card.structured_content["bytes"] == len(original)
    assert "path" not in card.structured_content
    parts, offset = [], 0
    while offset < len(original):
        chunk = voice_chunk(media, "nova", result["artifact"], offset)
        data = base64.b64decode(chunk.meta["audio_base64"])
        assert len(data) <= CHUNK_BYTES
        assert "audio_base64" not in chunk.structured_content
        parts.append(data)
        offset = chunk.structured_content["next_offset"]
        assert chunk.structured_content["eof"] == (offset == len(original))
    assert b"".join(parts) == original
    assert len(media.runtime.receipts("nova")) == 1  # reads never create paid operations
    engine.create_character(CharacterSpec(id="other", name="Other", persona="Other"))
    for bad in ("../private.mp3", "/etc/passwd", "missing.mp3"):
        with pytest.raises(ValueError):
            voice_card(media, "nova", bad)
    with pytest.raises(ValueError):
        voice_chunk(media, "other", result["artifact"], 0)
    with Engine(engine.database, owner="another-owner") as other_engine:
        with pytest.raises(ValueError):
            voice_card(Media(Runtime(other_engine, Settings())), "nova", result["artifact"])
    for offset in (-1, True, len(original), len(original) + 1):
        with pytest.raises(ValueError):
            voice_chunk(media, "nova", result["artifact"], offset)
    assert len(engine.history("nova")) == 0


def test_player_rejects_images_missing_files_and_symlinks(engine, tmp_path):
    media, result = saved_audio(engine, b"ID3audio")
    image = media.runtime.operation(
        "nova",
        "image",
        "i1",
        {},
        lambda: media._save(b"image", "image/png", "png", {}),
    )
    with pytest.raises(ValueError, match="not a voice"):
        voice_card(media, "nova", image["artifact"])
    path = Path(result["path"])
    path.unlink()
    with pytest.raises(ValueError):
        voice_card(media, "nova", result["artifact"])
    target = tmp_path / "unrelated"
    target.write_bytes(b"secret")
    path.symlink_to(target)
    with pytest.raises(ValueError):
        voice_card(media, "nova", result["artifact"])


def test_provider_configuration_explicit_private_redacted(engine, tmp_path, monkeypatch):
    config = tmp_path / "providers.json"
    runtime = Runtime(engine, Settings())
    token = "t" * 32
    with TestClient(
        create_app(runtime, token, include_mcp=False), base_url="http://127.0.0.1:8765"
    ) as client:
        client.headers["Authorization"] = "Bearer " + token
        settings = {
            "speech_provider": "minimax",
            "speech_model": "test-model",
            "speech_key": "PRIVATE_TEST_KEY",
        }
        local = client.post("/api/settings", json={"settings": settings})
        assert local.status_code == 200 and not config.exists()
        assert "PRIVATE_TEST_KEY" not in local.text
        saved = client.post("/api/settings", json={"settings": {}, "persist": True})
        assert saved.status_code == 200 and saved.json()["persisted"]
        assert "PRIVATE_TEST_KEY" not in saved.text
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert Settings.from_env().speech_key.get_secret_value() == "PRIVATE_TEST_KEY"
        monkeypatch.setenv("COMPANION_SPEECH_KEY", "ENV_OVERRIDE")
        assert Settings.from_env().speech_key.get_secret_value() == "ENV_OVERRIDE"
        client.post("/api/settings", json={"clear_keys": ["speech_key"], "persist": True})
        assert load_provider_settings()["speech_key"] == ""
    config.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_provider_settings()
    config.unlink()
    other = tmp_path / "other-settings.json"
    other.write_text("{}")
    config.symlink_to(other)
    with pytest.raises(ValueError):
        load_provider_settings()
    with pytest.raises(ValueError):
        save_provider_settings(Settings())
    assert other.read_text() == "{}"


def test_plugin_archive_is_self_contained_without_db_or_credentials(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "companion.zip"
    monkeypatch.setattr(
        sys, "argv", ["companion", "plugin-package", str(archive), "--app-id", "asdk_app_test"]
    )
    main()
    assert json.loads(capsys.readouterr().out)["registered_mcp_mapping"]
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert "skills/companion-conversation/SKILL.md" in names
        assert "skills/companion-conversation/references/setup.md" in names
        assert not any(n.endswith((".sqlite3", ".env")) for n in names)
        manifest = json.loads(bundle.read("plugin.json"))
        assert manifest["extensions"]["com.openai"]["apps"] == "./.app.json"
        assert (
            json.loads(bundle.read(".app.json"))["apps"]["companion_engine"]["id"]
            == "asdk_app_test"
        )
    assert workflow().startswith("---\nname: companion-conversation")
    with pytest.raises(ValueError):
        workflow("../../etc/passwd")
    with pytest.raises(SystemExit):
        main()  # no overwrite of a previous package


def test_real_mcp_workflow_provider_status_and_player(tmp_path, monkeypatch):
    db = tmp_path / "stdio.sqlite3"
    with Engine(db) as engine:
        engine.create_character(CharacterSpec(id="nova", name="Nova", persona="Warm"))
        _, result = saved_audio(engine, b"ID3synthetic-audio")
    monkeypatch.setenv("COMPANION_SPEECH_KEY", "NEVER_RETURN_THIS")
    monkeypatch.setenv("COMPANION_SPEECH_MODEL", "test-model")

    async def scenario():
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "companion_engine", "--db", str(db), "serve"],
            env={
                "COMPANION_SETTINGS_FILE": os.environ["COMPANION_SETTINGS_FILE"],
                "COMPANION_SPEECH_KEY": "NEVER_RETURN_THIS",
                "COMPANION_SPEECH_MODEL": "test-model",
            },
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=20) as client:
                info = await client.initialize()
                assert "get_companion_workflow" in info.instructions
                tools = {t.name: t for t in (await client.list_tools()).tools}
                assert tools["show_voice_message"].meta["ui"]["resourceUri"] == PLAYER_URI
                assert tools["read_voice_chunk"].meta["ui"]["visibility"] == ["app"]
                skill = await client.call_tool("get_companion_workflow", {})
                assert not skill.is_error
                skill_data = skill.structured_content or json.loads(skill.content[0].text)
                assert "instructions" in skill_data
                status = await client.call_tool("provider_status", {})
                status_data = status.structured_content or json.loads(status.content[0].text)
                assert status_data["speech"]["ready"]
                assert "NEVER_RETURN_THIS" not in status.model_dump_json()
                card = await client.call_tool(
                    "show_voice_message", {"character_id": "nova", "artifact": result["artifact"]}
                )
                assert not card.is_error and card.structured_content["mime"] == "audio/mpeg"
                chunk = await client.call_tool(
                    "read_voice_chunk",
                    {"character_id": "nova", "artifact": result["artifact"], "offset": 0},
                )
                assert base64.b64decode(chunk.meta["audio_base64"]) == b"ID3synthetic-audio"
                resource = await client.read_resource(PLAYER_URI)
                assert resource.contents[0].mime_type == "text/html;profile=mcp-app"
                assert "<audio" in resource.contents[0].text
                assert resource.contents[0].meta["ui"]["csp"]["connectDomains"] == []
                assert resource.contents[0].meta["ui"]["csp"]["resourceDomains"] == ["blob:"]
                assert resource.contents[0].meta["openai/widgetCSP"]["resource_domains"] == [
                    "blob:"
                ]

    asyncio.run(scenario())


def test_multiturn_correction_and_recovery_survive_restart(tmp_path):
    db = tmp_path / "workflow.sqlite3"
    with Engine(db) as engine:
        engine.create_character(CharacterSpec(id="nova", name="Nova", persona="Warm"))
        turn = engine.prepare_turn("nova", "Please remember I prefer tea", request_id="1")
        from companion_engine.models import MemoryDraft

        result = engine.commit_turn(
            "nova",
            turn.turn_id,
            CommitRequest(
                assistant_reply="Tea, got it.",
                memories=[
                    MemoryDraft(key="drink", text="Prefers tea", source_quote="I prefer tea")
                ],
            ),
        )
    with Engine(db) as engine:
        turn = engine.prepare_turn(
            "nova", "Actually I prefer coffee, please update it", request_id="2"
        )
        assert engine.search("nova", "tea")
        engine.commit_turn(
            "nova",
            turn.turn_id,
            CommitRequest.model_validate(
                {
                    "assistant_reply": "Updated to coffee.",
                    "corrections": [
                        {
                            "memory_id": result.memory_ids[0],
                            "expected_revision": 1,
                            "kind": "superseded",
                            "draft": {
                                "key": "drink",
                                "text": "Prefers coffee",
                                "source_quote": "I prefer coffee",
                            },
                        }
                    ],
                }
            ),
        )
        pending = engine.prepare_turn("nova", "No advice please", request_id="3")
    with Engine(db) as engine:
        assert engine.pending_turn("nova")["id"] == pending.turn_id
        resumed = engine.prepare_turn("nova", "No advice please", request_id="3")
        commit = CommitRequest(assistant_reply="I'll listen.", memories=[])
        engine.commit_turn("nova", resumed.turn_id, commit)
        assert engine.commit_turn("nova", resumed.turn_id, commit).replayed
        assert len(engine.history("nova")) == 3
        assert engine.search("nova", "coffee")[0].revision == 2
        assert not engine.search("nova", "tea")
        assert len(engine.memories("nova")) == 1  # transient boundary not made permanent

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from companion_engine.hosts import export_hosts, mcp_config


def test_export_is_configuration_only_and_preserves_existing_output(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-exported")
    db = tmp_path / "private" / "brain.sqlite3"
    output = tmp_path / "package with spaces"
    export_hosts(output, db, "test-owner", settings_file=tmp_path / "providers.json")
    assert not db.parent.exists()
    text = "\n".join(p.read_text() for p in output.rglob("*") if p.is_file())
    assert "must-not-be-exported" not in text
    desktop = json.loads((output / "claude-desktop-mcp.json").read_text())
    assert desktop == json.loads((output / "claude/.mcp.json").read_text())
    assert desktop["mcpServers"]["companion-engine"]["command"] == sys.executable
    before = text
    with pytest.raises(FileExistsError):
        export_hosts(output, tmp_path / "different.db", "other")
    assert "\n".join(p.read_text() for p in output.rglob("*") if p.is_file()) == before


def test_exported_hosts_share_persistent_memory_and_enforce_isolation(tmp_path):
    db = tmp_path / "shared.sqlite3"
    output = tmp_path / "hosts"
    settings = tmp_path / "providers.json"
    export_hosts(output, db, "owner-one", settings_file=settings)
    claude = json.loads((output / "claude/.mcp.json").read_text())["mcpServers"]["companion-engine"]
    dsh = json.loads((output / "deepseek/companion.cordis.yml").read_text())[0]["insert"][0][
        "config"
    ]

    @asynccontextmanager
    async def session(config):
        env = {"PATH": os.environ["PATH"], "COMPANION_SECRET_STORE": "file", **config["env"]}
        params = StdioServerParameters(command=config["command"], args=config["args"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=20) as client:
                await client.initialize()
                yield client

    async def call(client, name, args):
        response = await client.call_tool(name, args)
        assert not response.is_error, response
        return response.structured_content or json.loads(response.content[0].text)

    async def scenario():
        async with session(claude) as client:
            tools = await client.list_tools()
            assert "configure_private_provider" not in {tool.name for tool in tools.tools}
            await call(client, "get_companion_workflow", {})
            for character in ("nova", "other"):
                await call(
                    client,
                    "create_character",
                    {
                        "spec": {
                            "id": character,
                            "name": character,
                            "persona": "A fictional companion",
                        }
                    },
                )
            context = await call(
                client,
                "prepare_turn",
                {
                    "character_id": "nova",
                    "user_message": "Please remember I prefer tea",
                    "request_id": "host-test-1",
                },
            )
            request = {
                "character_id": "nova",
                "turn_id": context["turn_id"],
                "request": {
                    "assistant_reply": "Tea, got it.",
                    "attunement": {
                        "intent": "answer",
                        "user_cue": "Memory request",
                        "source_quote": "Please remember",
                    },
                    "appraisal": {
                        "emotion": "warm",
                        "warmth": 1,
                        "source_quote": "Please remember",
                    },
                    "memories": [
                        {
                            "key": "drink",
                            "text": "Prefers tea",
                            "source_quote": "I prefer tea",
                            "category": "preference",
                        }
                    ],
                },
            }
            saved = await call(client, "commit_turn", request)
            memory_id = saved["memory_ids"][0]
        # Fresh process and client, using the other host's exported command, not old chat context.
        async with session(dsh) as client:
            assert (await call(client, "commit_turn", request))["replayed"]
            hits = await call(client, "search_memory", {"character_id": "nova", "query": "tea"})
            assert "Prefers tea" in json.dumps(hits)
            updated = await call(
                client,
                "correct_memory",
                {
                    "character_id": "nova",
                    "memory_id": memory_id,
                    "expected_revision": 1,
                    "draft": {
                        "key": "drink",
                        "text": "Prefers coffee",
                        "source_quote": "Actually coffee",
                        "category": "preference",
                    },
                },
            )
            assert updated["revision"] == 2
            other = await call(client, "search_memory", {"character_id": "other", "query": "tea"})
            assert "Prefers tea" not in json.dumps(other)
        async with session(claude) as client:
            hits = await call(client, "search_memory", {"character_id": "nova", "query": "coffee"})
            assert "Prefers coffee" in json.dumps(hits)
            hits = await call(client, "search_memory", {"character_id": "nova", "query": "tea"})
            assert "Prefers tea" not in json.dumps(hits)
        separate = mcp_config(db, "owner-two", settings_file=settings)["mcpServers"][
            "companion-engine"
        ]
        async with session(separate) as client:
            denied = await client.call_tool(
                "search_memory", {"character_id": "nova", "query": "coffee"}
            )
            assert denied.is_error

    asyncio.run(scenario())

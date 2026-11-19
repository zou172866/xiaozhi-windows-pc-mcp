import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from companion_engine import Engine


def unpack(result):
    assert not result.is_error, result
    # MCP has a structured object and a text fallback for clients with different renderers.
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_real_stdio_roundtrip(tmp_path):
    db = tmp_path / "mcp.sqlite3"

    async def scenario():
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "companion_engine", "--db", str(db), "--owner", "mcp-owner", "serve"],
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=20) as client:
                info = await client.initialize()
                assert info.server_info.name == "Companion Engine"
                listed = await client.list_tools()
                names = {t.name for t in listed.tools}
                annotations = {t.name: t.annotations for t in listed.tools}
                assert annotations["list_characters"].read_only_hint
                assert annotations["delete_memory"].destructive_hint
                assert {
                    "prepare_turn",
                    "commit_turn",
                    "search_memory",
                    "correct_memory",
                    "forget_character",
                    "prepare_memory_summary",
                    "commit_memory_summary",
                    "get_companion_workflow",
                } <= names
                assert all(
                    "owner" not in t.input_schema.get("properties", {}) for t in listed.tools
                )
                unpack(
                    await client.call_tool(
                        "create_character",
                        {
                            "spec": {
                                "id": "nova",
                                "name": "Nova",
                                "persona": "A warm fictional companion",
                            }
                        },
                    )
                )
                ctx = unpack(
                    await client.call_tool(
                        "prepare_turn",
                        {
                            "character_id": "nova",
                            "user_message": "Please remember I prefer tea",
                            "request_id": "test",
                        },
                    )
                )
                assert ctx["character"]["relationship_enabled"]
                assert ctx["continuity"]["policy"]["embedding"]
                assert ctx["continuity"]["policy"]["host_summaries"]
                args = {
                    "character_id": "nova",
                    "turn_id": ctx["turn_id"],
                    "request": {
                        "assistant_reply": "Tea, got it.",
                        "attunement": {
                            "intent": "answer",
                            "user_cue": "Explicit memory request",
                            "source_quote": "Please remember",
                        },
                        "appraisal": {
                            "source_quote": "Please remember",
                            "emotion": "warm",
                            "warmth": 1,
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
                commit = unpack(await client.call_tool("commit_turn", args))
                assert len(commit["memory_ids"]) == 1
                assert unpack(await client.call_tool("commit_turn", args))["replayed"]
                batch = unpack(
                    await client.call_tool("prepare_memory_summary", {"character_id": "nova"})
                )
                eid = ctx["turn_id"] + ":user"
                saved = unpack(
                    await client.call_tool(
                        "commit_memory_summary",
                        {
                            "character_id": "nova",
                            "batch_id": batch["batch_id"],
                            "episode": "User asked to remember a tea preference.",
                            "core": "",
                            "episode_source_ids": [eid],
                            "core_source_ids": [eid],
                            "notebook": {
                                "entries": [
                                    {
                                        "section": "life_now",
                                        "text": "Prefers tea.",
                                        "source_ids": [eid],
                                    }
                                ]
                            },
                        },
                    )
                )
                assert saved["saved"]
                assert commit["continuity_status"]["attunement_recorded"]
                bad = await client.call_tool(
                    "prepare_turn",
                    {"character_id": "not-mine", "user_message": "hello", "request_id": "x"},
                )
                assert bad.is_error
                memory_id = commit["memory_ids"][0]
                updated = unpack(
                    await client.call_tool(
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
                )
                assert updated["revision"] == 2

    asyncio.run(scenario())
    # The real server process has stopped; prove persisted corrected data, not an in-memory mock.
    with Engine(db, owner="mcp-owner") as reopened:
        assert reopened.search("nova", "coffee")[0].text == "Prefers coffee"
        assert reopened.search("nova", "tea") == []

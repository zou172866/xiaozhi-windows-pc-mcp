import asyncio

import pytest

from companion_engine import CommitRequest, Engine, MemoryDraft
from companion_engine.engine import Conflict
from companion_engine.mcp_server import create_server
from companion_engine.runtime import Runtime, Settings


def add(engine, n):
    ctx = engine.prepare_turn("nova", f"I prefer tea {n}", request_id=str(n))
    engine.commit_turn("nova", ctx.turn_id, CommitRequest(assistant_reply="Okay"))


def finish(runtime, batch):
    ids = batch["batch_source_ids"]
    return runtime.host_memory.commit(
        "nova", batch["batch_id"], "Tea discussed.", "User likes tea.", ids, ids
    )


def test_host_summary_no_provider_and_idempotence(engine):
    r = Runtime(engine, Settings())
    r.automation.configure("nova", {"host_summaries": True, "summary_turns": 2})
    add(engine, 1)
    assert not r.host_memory.notice("nova")["due"]
    add(engine, 2)
    assert r.host_memory.notice("nova")["due"]
    assert not r.automation.availability(r.automation.policy("nova"))
    assert not r.automation.run_once()
    batch = r.host_memory.prepare("nova")
    assert len(batch["batch_source_ids"]) == 4
    assert finish(r, batch)["saved"]
    assert finish(r, batch)["replayed"]
    assert r.automation.status("nova")["summary_backlog"]["turns"] == 0
    assert len(engine.memory.documents("nova")) == 2
    assert engine.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    with pytest.raises(Conflict):
        r.host_memory.commit(
            "nova",
            batch["batch_id"],
            "Changed",
            "Changed",
            batch["batch_source_ids"],
            batch["batch_source_ids"],
        )
    with Engine(engine.database, owner=engine.owner) as other:
        reopened = Runtime(other, Settings())
        assert finish(reopened, batch)["replayed"]
        assert reopened.automation.policy("nova").host_summaries


def test_host_bounds_citations_stale_facts_and_scope(engine):
    r = Runtime(engine, Settings())
    r.automation.configure("nova", {"host_summaries": True})
    for n in range(5):
        add(engine, n)
    batch = r.host_memory.prepare("nova")
    assert len(batch["batch_source_ids"]) == 8
    with pytest.raises(ValueError):
        r.host_memory.commit("nova", batch["batch_id"], "Episode", "Core", ["fake"], ["fake"])
    assert not engine.memory.documents("nova")
    engine.remember("nova", MemoryDraft(key="drink", text="Tea", source_quote="Tea"))
    with pytest.raises(Conflict):
        finish(r, batch)
    batch = r.host_memory.prepare("nova")
    finish(r, batch)
    assert r.host_memory.notice("nova")["pending_turns"] == 1
    with Engine(engine.database, owner="someone-else") as other:
        with pytest.raises(ValueError):
            Runtime(other, Settings()).host_memory.prepare("nova")


def test_host_disabled_and_mutually_exclusive(engine):
    r = Runtime(engine, Settings())
    r.automation.configure("nova", {})
    with pytest.raises(ValueError):
        r.host_memory.prepare("nova")
    with pytest.raises(ValueError):
        r.automation.configure("nova", {"host_summaries": True, "summaries": True})
    r.automation.configure("nova", {"host_summaries": True})
    spec = next(c for c in engine.characters() if c.id == "nova")
    engine.update_character(spec.model_copy(update={"memory_policy": "off"}))
    assert not r.host_memory.notice("nova")["due"]
    with pytest.raises(ValueError):
        r.host_memory.prepare("nova")


def test_mcp_host_switch_preserves_embedding_and_exposes_notice(engine):
    async def run():
        r = Runtime(engine, Settings())
        r.automation.configure("nova", {"summaries": True, "embedding": True, "summary_turns": 1})
        server = create_server(engine, r)
        await server.call_tool("configure_host_summaries", {"character_id": "nova"})
        p = r.automation.policy("nova")
        assert p.embedding and p.host_summaries and not p.summaries
        add(engine, 1)
        ctx = r.prepare("nova", "Hi", "new")
        assert ctx.memory_maintenance["due"]
        result = await server.call_tool("prepare_memory_summary", {"character_id": "nova"})
        assert result
        batch = r.host_memory.prepare("nova")
        result = await server.call_tool(
            "commit_memory_summary",
            {
                "character_id": "nova",
                "batch_id": batch["batch_id"],
                "episode": "Discussed tea.",
                "core": "Likes tea.",
                "episode_source_ids": batch["batch_source_ids"],
                "core_source_ids": batch["batch_source_ids"],
            },
        )
        assert result
        assert r.host_memory.notice("nova")["pending_turns"] == 0

    asyncio.run(run())

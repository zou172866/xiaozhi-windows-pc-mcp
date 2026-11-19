import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from companion_engine import CharacterSpec, CommitRequest, Engine, MemoryDraft
from companion_engine.automation import MemoryAutomation
from companion_engine.engine import Conflict
from companion_engine.mcp_server import create_server
from companion_engine.memory_ui import MEMORY_URI
from companion_engine.providers import Completion
from companion_engine.runtime import Runtime, Settings


def turn(engine, text="Tea please", request="one"):
    ctx = engine.prepare_turn("nova", text, request_id=request)
    engine.commit_turn("nova", ctx.turn_id, CommitRequest(assistant_reply="Okay"))
    return ctx


def embeddings(calls, fail=False):
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if fail:
            return httpx.Response(500, json={"error": "secret-must-not-leak"})
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": [1, 0]} for i, _ in enumerate(payload["input"])],
                "usage": {"total_tokens": len(payload["input"])},
            },
        )

    return httpx.MockTransport(handler)


def settings():
    return Settings(embedding_base_url="https://example.test/v1", embedding_model="embed")


class Summarizer:
    model = "fake"

    def structured(self, instructions, text):
        data = json.loads(text)
        if "new_source" in data:
            out = {"description": "A beverage conversation", "keywords": ["tea"], "links": []}
        else:
            sources = [s["id"] for s in data["sources"]]
            out = {
                "episode": "Discussed tea.",
                "core": "Tea came up.",
                "episode_source_ids": sources,
                "core_source_ids": sources,
            }
        return Completion(json.dumps(out), self.model, None)


def test_explicit_disabled_policy_and_restart_backlog(engine):
    calls = []
    r = Runtime(engine, settings(), transport=embeddings(calls))
    r.automation.configure("nova", {})  # an explicit off choice beats automatic defaults
    turn(engine)
    assert not r.automation.run_once()
    assert not calls
    r.automation.configure("nova", {"embedding": True})
    with Engine(engine.database, owner=engine.owner) as reopened:
        other = Runtime(reopened, settings(), transport=embeddings(calls))
        assert other.automation.run_once()
        assert not other.automation.run_once()
        status = other.automation.status("nova")
        assert status["indexed_count"] == status["evidence_count"] == 2
        assert status["jobs"][0]["state"] == "succeeded"
    assert len(calls) == 1


def test_summary_threshold_then_reindex_and_idle(engine):
    calls = []
    r = Runtime(engine, settings(), provider=Summarizer(), transport=embeddings(calls))
    r.automation.configure("nova", {"embedding": True, "summaries": True, "summary_turns": 2})
    turn(engine)
    assert r.automation.run_once()  # index below summary threshold
    assert not r.automation.run_once()
    turn(engine, request="two")
    assert r.automation.run_once()  # summarize four original excerpts
    assert {d["kind"] for d in engine.memory.documents("nova")} == {"core", "episode"}
    assert r.automation.status("nova")["summary_backlog"]["turns"] == 0
    assert r.automation.run_once()  # notes invalidated earlier vectors; reindex
    assert r.automation.status("nova")["indexed_count"] == 4
    turn(engine, request="three")
    engine.db.execute("UPDATE evidence SET observed_at=?", (time.time() - 500,))
    assert r.automation.run_once()  # idle flush below count threshold
    assert r.automation.status("nova")["summary_backlog"]["turns"] == 0


def test_two_process_connections_claim_only_once(engine):
    turn(engine)
    r = Runtime(engine, settings())
    r.automation.configure("nova", {"embedding": True})
    with Engine(engine.database, owner=engine.owner) as other:
        second = Runtime(other, settings())
        with ThreadPoolExecutor(2) as pool:
            jobs = list(pool.map(lambda a: a.claim(), [r.automation, second.automation]))
        assert sum(j is not None for j in jobs) == 1


def test_provider_failure_no_silent_retry_and_explicit_resume(engine):
    calls = []
    r = Runtime(engine, settings(), transport=embeddings(calls, fail=True))
    turn(engine)
    r.automation.configure("nova", {"embedding": True})
    assert r.automation.run_once()
    status = r.automation.status("nova")
    assert status["jobs"][0]["state"] == "review"
    assert "secret-must-not-leak" not in json.dumps(status)
    assert not r.automation.run_once()
    assert len(calls) == 1
    r.automation.acknowledge("nova", status["jobs"][0]["id"])
    r.transport = embeddings(calls)
    assert r.automation.run_once()
    assert len(calls) == 2


def test_crashed_provider_reservation_is_reviewed_before_recharge(engine):
    r = Runtime(engine, settings())
    turn(engine)
    r.automation.configure("nova", {"embedding": True})
    job = r.automation.claim()
    engine.db.execute("UPDATE memory_jobs SET lease_until=0")
    assert not r.automation.claim()
    assert r.automation.status("nova")["jobs"][0]["state"] == "review"
    r.automation.acknowledge("nova", job["id"])
    assert r.automation.claim()


def test_missing_provider_off_mode_and_daily_bound(engine):
    turn(engine)
    r = Runtime(engine, Settings())
    r.automation.configure("nova", {"embedding": True, "summaries": True})
    assert set(r.automation.status("nova")["provider_issues"]) == {
        "embedding_provider_missing",
        "summary_provider_missing",
    }
    assert not r.automation.run_once()
    r.settings = settings()
    r.transport = embeddings([])
    r.automation.configure("nova", {"embedding": True, "daily_jobs": 1})
    assert r.automation.run_once()
    turn(engine, request="two")
    assert not r.automation.run_once()  # capped despite backlog
    r.automation.configure("nova", {"embedding": True, "daily_jobs": 10})
    engine.update_character(
        CharacterSpec(id="nova", name="Nova", persona="Hi", memory_policy="off")
    )
    assert not r.automation.run_once()


def test_prepare_semantic_replay_no_duplicate_query_charge(engine):
    calls = []
    r = Runtime(engine, settings(), transport=embeddings(calls))
    turn(engine, "My dog is Mochi")
    r.automation.configure("nova", {"embedding": True, "semantic_retrieval": True})
    r.automation.run_once()
    ctx = r.prepare("nova", "What is my pet called?", "query")
    assert ctx.retrieval_mode == "hybrid_rrf"
    assert any("Mochi" in s["text"] for s in ctx.evidence)
    assert r.prepare("nova", "What is my pet called?", "query").turn_id == ctx.turn_id
    assert len(calls) == 2  # one document batch + one query; exact replay cached


def test_panel_scoped_and_revision_guarded(engine):
    m = engine.remember("nova", MemoryDraft(key="drink", text="tea", source_quote="tea"))
    r = Runtime(engine, Settings())
    assert r.memory_panel("nova")["total"] == 1
    with Engine(engine.database, owner="other") as other:
        other.create_character(CharacterSpec(id="nova", name="Nova", persona="Hi"))
        assert Runtime(other, Settings()).memory_panel("nova")["total"] == 0

    async def scenario():
        client = create_server(engine, r)
        tools = {t.name: t for t in (await client.list_tools())}
        assert tools["show_memory_panel"].meta["ui"]["resourceUri"] == MEMORY_URI
        assert tools["edit_memory_from_panel"].meta["ui"]["visibility"] == ["app"]
        panel = await client.call_tool("show_memory_panel", {"character_id": "nova"})
        assert panel.structured_content["total"] == 1
        args = {
            "character_id": "nova",
            "memory_id": m.id,
            "expected_revision": 1,
            "text": "coffee",
        }
        edit = await client.call_tool("edit_memory_from_panel", args)
        assert not edit.is_error
        with pytest.raises(Exception, match="Error executing tool") as exc:
            await client.call_tool("edit_memory_from_panel", args)
        assert isinstance(exc.value.__cause__, Conflict)
        delete = await client.call_tool(
            "delete_memory_from_panel",
            {"character_id": "nova", "memory_id": m.id, "expected_revision": 2},
        )
        assert not delete.is_error
        assert r.memory_panel("nova")["total"] == 0

    asyncio.run(scenario())


def test_policy_validation():
    with pytest.raises(ValueError):
        MemoryAutomation(summary_turns=0)


def test_consolidation_rejects_fact_change_during_provider_call(engine):
    ctx = turn(engine)
    original = engine.remember("nova", MemoryDraft(key="drink", text="tea", source_quote="tea"))
    version = engine.memory.fact_version("nova")
    engine.correct_memory(
        "nova",
        original.id,
        MemoryDraft(key="drink", text="coffee", source_quote="coffee"),
        expected_revision=1,
    )
    with pytest.raises(Conflict, match="stale summary"):
        engine.memory.consolidate_pair(
            "nova",
            "old tea",
            "old tea",
            [ctx.turn_id + ":user"],
            expected_facts=version,
            processed_ids=[ctx.turn_id + ":user"],
        )
    assert not engine.memory.documents("nova")
    assert engine.db.execute("SELECT count(*) FROM maintenance_processed").fetchone()[0] == 0


def test_query_provider_failure_preserves_turn_and_lexical_context(engine):
    calls = []
    r = Runtime(engine, settings(), transport=embeddings(calls, fail=True))
    turn(engine, "tea")
    r.automation.configure("nova", {"semantic_retrieval": True})
    ctx = r.prepare("nova", "tea?", "failed-query")
    assert ctx.retrieval_mode == "lexical_semantic_unavailable"
    assert ctx.evidence
    r.prepare("nova", "tea?", "failed-query")
    assert len(calls) == 1
    engine.commit_turn("nova", ctx.turn_id, CommitRequest(assistant_reply="Still here"))


def test_http_memory_settings_and_status_roundtrip(engine):
    from starlette.testclient import TestClient

    from companion_engine.http_server import create_app

    runtime = Runtime(engine, Settings())
    token = "test-private-" * 3
    with TestClient(
        create_app(runtime, token, include_mcp=False), base_url="http://127.0.0.1:8765"
    ) as client:
        assert client.get("/api/memory_panel?character_id=nova").status_code == 401
        client.headers["Authorization"] = "Bearer " + token
        response = client.post(
            "/api/configure_memory",
            json={"character_id": "nova", "policy": {"embedding": True, "summary_turns": 6}},
        )
        assert response.status_code == 200
        panel = client.get("/api/memory_panel?character_id=nova").json()
        assert panel["automation"]["policy"]["summary_turns"] == 6
        assert panel["automation"]["provider_issues"] == ["embedding_provider_missing"]


def test_background_worker_runs_without_another_host_tool_call(engine):
    calls = []
    runtime = Runtime(engine, settings(), transport=embeddings(calls))
    turn(engine)
    runtime.automation.configure("nova", {"embedding": True})
    with runtime.automation.worker():
        deadline = time.monotonic() + 3
        while runtime.automation.status("nova")["indexed_count"] != 2:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert len(calls) == 1

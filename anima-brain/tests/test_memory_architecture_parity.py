"""Behavioral regressions for the original Anima memory mechanisms (no provider spend)."""

import asyncio
import json
import time
from datetime import date, datetime, timezone

import httpx
import pytest

from companion_engine import CommitRequest, Engine, MemoryDraft
from companion_engine.automation import MemoryAutomation
from companion_engine.engine import Conflict, NotFound
from companion_engine.knowledge import Transitions
from companion_engine.memory import Note
from companion_engine.models import TemporalQuery
from companion_engine.note_evolution import apply, snapshot
from companion_engine.providers import Completion
from companion_engine.runtime import Runtime, Settings
from companion_engine.temporal import lookup


def turn(engine, text, *, key=None, details=None, reply="Okay"):
    context = engine.prepare_turn("nova", text, request_id=f"test-{time.time_ns()}")
    drafts = (
        [MemoryDraft(key=key, text=text, source_quote=text, details=details or {})] if key else []
    )
    engine.commit_turn(
        "nova", context.turn_id, CommitRequest(assistant_reply=reply, memories=drafts)
    )
    return context.turn_id


def current(key, valid=None, **kwargs):
    return {"state_key": key, "temporal_kind": "current", "valid_from": valid, **kwargs}


def drain_empty(engine):
    while batch := engine.knowledge.prepare("nova"):
        engine.knowledge.apply("nova", batch, Transitions())


def decision(batch, old, *, kind="superseded"):
    return {
        "transitions": [
            {
                "old_id": old.id,
                "expected_revision": old.revision,
                "kind": kind,
                "same_attribute": True,
                "incompatible": True,
                "explicit_change": True,
                "explicit_correction": kind == "corrected",
                "old_quote": old.source_quote,
                "new_quote": batch["new"]["source_quote"],
            }
        ]
    }


def test_contextual_vectors_causal_chunks_and_note_lane(engine):
    first = turn(
        engine, "My sister Ada adopted a dog named Mochi.", reply="How is Mochi settling in?"
    )
    second = turn(
        engine, "He keeps chewing her shoes.", reply="Try keeping the shoes out of reach."
    )
    source = second + ":user"
    index = engine.evidence_index
    raw = json.loads(index.representation("nova", source + ":0", "raw"))
    assert [c["id"] for c in raw["context"]] == [first + ":user", first + ":assistant"]
    assert raw["excerpt"]["speaker"] == "user" and raw["excerpt"]["observed_at"]
    assert "out of reach" not in json.dumps(raw)
    text = index.representation("nova", source + ":0", "raw")
    index.store("nova", source + ":0", "raw", "model", [1, 0], text)
    engine.memory.annotate(
        "nova",
        source,
        Note(description="Ada's dog Mochi chews her shoes.", keywords=["Mochi", "Ada"]),
    )
    note_text = index.representation("nova", source + ":0", "note")
    index.store("nova", source + ":0", "note", "model", [0, 1], note_text)
    engine.memory.annotate(
        "nova", source, Note(description="Her dog chews shoes"), expected_revision=1
    )
    assert engine.db.execute("SELECT lane FROM evidence_embeddings").fetchall()[0][0] == "raw"
    with pytest.raises(Conflict, match="stale vector"):
        index.store("nova", source + ":0", "note", "model", [0, 1], note_text)
    # A later topic cannot alter the immutable original representation.
    turn(engine, "Actually my brother bought a boat yesterday")
    assert index.representation("nova", source + ":0", "raw") == text
    long_turn = turn(engine, "Earlier details. " * 850 + "WHALEANCHOR at the end")
    chunks = engine.db.execute(
        "SELECT start,end FROM evidence_chunks WHERE evidence_id=? ORDER BY start",
        (long_turn + ":user",),
    ).fetchall()
    assert len(chunks) > 1 and chunks[0][1] - chunks[1][0] == 180
    found = engine.memory.recall("nova", "WHALEANCHOR")["sources"][0]
    assert found["partial_source"] and "WHALEANCHOR" in found["text"]
    assert found["source_length"] > 10000
    with Engine(engine.database, owner=engine.owner) as reopened:
        assert reopened.evidence_index.representation("nova", source + ":0", "raw") == text
    with Engine(engine.database, owner="someone-else") as other:
        with pytest.raises((NotFound, ValueError)):
            other.evidence_index.representation("nova", source + ":0", "raw")


def test_raw_and_note_semantics_fuse_with_lexical_and_one_hop(engine):
    first = turn(engine, "The exam was rough") + ":user"
    second = turn(engine, "About that exam, I passed") + ":user"
    engine.memory.annotate(
        "nova",
        second,
        Note(
            description="Exam result",
            links=[
                {
                    "target_id": first,
                    "relation": "continuation",
                    "source_quote": "that exam",
                    "target_quote": "exam was rough",
                }
            ],
        ),
    )
    for eid, lane, vector in [
        (first, "raw", [1, 0]),
        (second, "raw", [1, 0.1]),
        (second, "note", [1, 0]),
    ]:
        text = engine.evidence_index.representation("nova", eid + ":0", lane)
        engine.evidence_index.store("nova", eid + ":0", lane, "model", vector, text)
    out = engine.memory.recall("nova", "exam", query_vector=[1, 0], model="model")
    assert out["sources"][0]["id"] == second  # lexical plus note vector
    assert {s["id"] for s in out["sources"]} >= {first, second}
    assert out["sources"][0]["context"]


def test_linked_old_note_evolves_atomically_preserving_raw_vector(engine):
    old = turn(engine, "The appointment is Tuesday.") + ":user"
    new = turn(engine, "By appointment I meant Ada's visa interview, not mine.") + ":user"
    engine.memory.annotate("nova", old, Note(description="Appointment Tuesday"))
    before = snapshot(engine, "nova", old)
    fresh = snapshot(engine, "nova", new)
    proposal = {
        "description": "User clarifies Ada's visa interview.",
        "keywords": ["Ada"],
        "links": [
            {
                "target_id": old,
                "relation": "clarification",
                "source_quote": "Ada's visa interview",
                "target_quote": "appointment",
            }
        ],
        "updates": [
            {
                "evidence_id": old,
                "expected_revision": 1,
                "description": "Ada's visa interview is Tuesday.",
                "keywords": ["Ada"],
            }
        ],
    }
    assert apply(engine, "nova", fresh, [before], proposal) == [new, old]
    assert snapshot(engine, "nova", old)["note"]["revision"] == 2
    assert engine.memory._source("nova", old)["text"] == "The appointment is Tuesday."
    assert engine.db.execute("SELECT count(*) FROM note_dependencies").fetchone()[0] == 2
    with pytest.raises(Conflict):
        apply(engine, "nova", fresh, [before], proposal)
    third = turn(engine, "The train was late") + ":user"
    invalid = {**proposal, "links": []}
    with pytest.raises(ValueError, match="supported link"):
        apply(
            engine,
            "nova",
            snapshot(engine, "nova", third),
            [snapshot(engine, "nova", old)],
            invalid,
        )
    assert snapshot(engine, "nova", third)["note"] is None  # whole operation rolled back


def test_archive_rank_and_explicit_lexical_fallback(engine):
    a = engine.remember(
        "nova",
        MemoryDraft(
            key="a",
            text="Likes jasmine tea",
            source_quote="tea",
            details={"importance": 9, "event_date": "2000-01-01"},
        ),
    )
    b = engine.remember(
        "nova",
        MemoryDraft(key="b", text="Likes black tea", source_quote="tea", details={"importance": 9}),
    )
    for row in engine.retrieval.candidates("nova", "model"):
        engine.retrieval.store("nova", row["id"], "model", [1, 0], row["text"])
    out = engine.retrieval.search("nova", "tea", vector=[1, 0], model="model")
    assert out["facts"][0]["id"] == b.id
    assert out["scores"]["fact:" + a.id]["recency"] < 0.001
    assert 0.98 < out["scores"]["fact:" + b.id]["score"] < 1
    lexical = engine.retrieval.search("nova", "tea")
    assert all(d["relevance_basis"] == "lexical_rank_proxy" for d in lexical["scores"].values())


def test_reinforcement_preserves_event_and_observation_clocks(engine):
    details = {"event_date": "2025-02-01", "temporal_kind": "event"}
    tid = turn(engine, "I graduated in February", key="graduation", details=details)
    first = engine.memories("nova")[0]
    turn(engine, "I graduated in February", key="graduation", details=details)
    reinforced = engine.memories("nova")[0]
    assert first.source_turn_id == reinforced.source_turn_id == tid
    assert reinforced.lifecycle.observed_at == first.lifecycle.observed_at
    assert reinforced.lifecycle.mentions == 2
    assert reinforced.lifecycle.last_reinforced_at > first.lifecycle.last_reinforced_at
    assert reinforced.details.event_date == date(2025, 2, 1)


def test_cross_key_state_update_preserves_history_and_invalidates_current_views(engine):
    first = turn(engine, "I live in Boston", key="home", details=current("user:home", "2024-01-01"))
    old = engine.memories("nova")[0]
    drain_empty(engine)
    engine.memory.consolidate("nova", "core", "Lives in Boston", [first + ":user"])
    turn(
        engine,
        "I moved from Boston to Seattle",
        key="new-city",
        details=current("user:location", "2025-01-01"),
    )
    batch = engine.knowledge.prepare("nova")
    assert old.id in {m["id"] for m in batch["candidates"]}
    engine.knowledge.apply("nova", batch, decision(batch, old))
    assert not engine.memory.documents("nova")
    assert old.id not in {m["id"] for m in engine.retrieval.search("nova", "Boston")["facts"]}
    ctx = engine.prepare_turn("nova", "where do I live?", request_id="after-change")
    assert old.id not in {m.id for m in ctx.memories}
    past = lookup(engine, "nova", TemporalQuery(mode="asof", start="2024-06-01"))
    assert [m["text"] for m in past["facts"]] == ["I live in Boston"]
    present = lookup(engine, "nova", TemporalQuery(mode="current"))
    assert [m["text"] for m in present["facts"]] == ["I moved from Boston to Seattle"]
    assert engine.memory.recall("nova", "Boston")["sources"]  # originals still inspectable


def test_correction_excludes_false_history_and_requires_exact_new_evidence(engine):
    turn(engine, "I own a dog", key="dog", details=current("user:pet"))
    old = engine.memories("nova")[0]
    drain_empty(engine)
    text = "I was wrong, the dog is Ada's, not mine"
    turn(
        engine,
        text,
        key="ada-dog",
        details=current("ada:pet", subject="third_party", correction_evidence=text),
    )
    batch = engine.knowledge.prepare("nova")
    invalid = decision(batch, old, kind="corrected")
    invalid["transitions"][0]["new_quote"] = "fabricated correction"
    with pytest.raises(ValueError, match="exact quotes"):
        engine.knowledge.apply("nova", batch, invalid)
    engine.knowledge.apply("nova", batch, decision(batch, old, kind="corrected"))
    history = lookup(engine, "nova", TemporalQuery(mode="history"))
    assert old.id not in {m["id"] for m in history["facts"]}


def test_reconciliation_preserves_additive_facts_and_rejects_races(engine):
    turn(engine, "I work at the cafe", key="job1", details=current("user:job"))
    old = engine.memories("nova")[0]
    drain_empty(engine)
    turn(engine, "I also tutor on weekends", key="job2", details=current("user:job"))
    batch = engine.knowledge.prepare("nova")
    # Same state_key alone cannot retire anything: the model may return no transition.
    engine.knowledge.apply("nova", batch, Transitions())
    assert len(lookup(engine, "nova", TemporalQuery(mode="current"))["facts"]) == 2
    turn(engine, "I quit the cafe", key="quit", details=current("user:job"))
    batch = engine.knowledge.prepare("nova")
    engine.correct_memory(
        "nova",
        old.id,
        MemoryDraft(key=old.key, text="Cafe in Boston", source_quote="Boston"),
        expected_revision=old.revision,
    )
    with pytest.raises(Conflict, match="Old fact changed"):
        engine.knowledge.apply("nova", batch, decision(batch, old))


def test_worker_runs_reconciliation_note_evolution_consolidation_and_dual_index(engine):
    class Writer:
        model = "deterministic-test"

        def structured(self, prompt, text):
            data = json.loads(text)
            if "new" in data:
                old = next(m for m in data["candidates"] if m["key"] == "home")
                out = {
                    "transitions": [
                        {
                            "old_id": old["id"],
                            "expected_revision": old["revision"],
                            "kind": "superseded",
                            "same_attribute": True,
                            "incompatible": True,
                            "explicit_change": True,
                            "new_quote": data["new"]["source_quote"],
                            "old_quote": old["source_quote"],
                        }
                    ]
                }
            elif "new_source" in data:
                assert "context" in data["new_source"]
                out = {"description": data["new_source"]["text"], "links": [], "updates": []}
            else:
                ids = [s["id"] for s in data["sources"]]
                assert all(not m["lifecycle"]["invalidation"] for m in data["current_facts"])
                out = {
                    "episode": "Moved cities",
                    "core": "Now lives in Seattle",
                    "episode_source_ids": ids,
                    "core_source_ids": ids,
                    "episode_metadata": {"importance": 8, "arousal": 0.7},
                }
            return Completion(json.dumps(out), self.model, None)

    payloads = []

    def embed(request):
        payload = json.loads(request.content)
        payloads.extend(payload["input"])
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": [1, 0]} for i, _ in enumerate(payload["input"])]
            },
        )

    runtime = Runtime(
        engine,
        Settings(embedding_base_url="https://embed.test", embedding_model="x"),
        provider=Writer(),
        transport=httpx.MockTransport(embed),
    )
    runtime.automation.configure(
        "nova", MemoryAutomation(embedding=True, summaries=True, reconcile=True, summary_turns=1)
    )
    turn(engine, "I live in Boston", key="home", details=current("user:home"))
    assert runtime.automation.run_once()  # no older facts: no LLM call
    turn(engine, "I moved to Seattle", key="new-home", details=current("user:city"))
    for _ in range(10):
        if not runtime.automation.run_once():
            break
    assert not engine.knowledge.pending("nova")
    assert {d["kind"] for d in engine.memory.documents("nova")} == {"core", "episode"}
    lanes = {r[0] for r in engine.db.execute("SELECT lane FROM evidence_embeddings")}
    assert lanes == {"raw", "note"}
    assert any('"context"' in p for p in payloads)
    assert all(r["state"] == "succeeded" for r in runtime.automation.status("nova")["jobs"])
    episode = next(d for d in engine.memory.documents("nova") if d["kind"] == "episode")
    assert engine.memory.document_metadata(episode["id"])["importance"] == 8


def test_temporal_reader_is_bounded_and_returns_actual_lookup(engine):
    turn(
        engine,
        "The interview is on May 7",
        key="interview",
        details={"event_date": "2026-05-07", "temporal_kind": "event"},
    )

    class Reader:
        model = "test"
        calls = 0

        def structured(self, prompt, text):
            assert "observation" in prompt
            self.calls += 1
            return Completion(
                json.dumps(
                    {
                        "action": "temporal",
                        "temporal": {
                            "mode": "events",
                            "start": "2026-05-01",
                            "end": "2026-05-31",
                            "limit": 12,
                        },
                    }
                ),
                "test",
                None,
            )

    runtime = Runtime(engine)
    context = runtime.prepare("nova", "What happens in May?", "time-query")
    reader = Reader()
    out = runtime.context("nova", context, reader)
    assert reader.calls == 2
    assert out.temporal_memory["facts"][0]["text"] == "The interview is on May 7"


def test_host_exposes_grounded_reconciliation_without_model_calls(engine):
    from companion_engine.mcp_server import create_server

    turn(engine, "I live in Boston", key="home", details=current("user:home"))
    server = create_server(engine)

    async def check():
        prepared = await server.call_tool(
            "prepare_knowledge_reconciliation", {"character_id": "nova"}
        )
        data = json.loads(prepared.content[0].text)
        assert data["batch"]["new"]["text"] == "I live in Boston"
        result = await server.call_tool(
            "commit_knowledge_reconciliation",
            {
                "character_id": "nova",
                "batch_id": data["batch_id"],
                "decisions": {"transitions": []},
            },
        )
        assert json.loads(result.content[0].text)["retired"] == 0

    asyncio.run(check())


def test_temporal_reader_respects_legacy_host_context_budget(engine, monkeypatch):
    from companion_engine import temporal

    runtime = Runtime(engine)
    context = runtime.prepare("nova", "Tell me about earlier events", "bounded-temporal")
    engine.context_chars = len(context.model_dump_json()) + 6000
    monkeypatch.setattr(
        temporal,
        "lookup",
        lambda *args: {
            "facts": [{"id": str(i), "text": "a" * 5000} for i in range(12)],
            "uncertain_validity": [],
            "time_basis": "history",
        },
    )

    class Reader:
        model = "test"

        def structured(self, *args):
            return Completion(
                json.dumps({"action": "temporal", "temporal": {"mode": "history", "limit": 12}}),
                "test",
                None,
            )

    out = runtime.context("nova", context, Reader())
    assert len(out.model_dump_json()) <= engine.context_chars
    assert out.temporal_memory["truncated_for_context"]
    assert len(out.temporal_memory["facts"]) == 1
    assert len(out.temporal_memory["facts"][0]["text"]) == 5000


def test_current_state_eligibility_uses_character_local_date(engine, monkeypatch):
    spec = engine.characters()[0].model_copy(update={"timezone": "America/Los_Angeles"})
    engine.update_character(spec)
    engine.remember(
        "nova",
        MemoryDraft(
            key="home",
            text="Living in Seattle",
            source_quote="Seattle",
            details=current("user:home", "2026-05-01"),
        ),
    )
    clock = datetime(2026, 5, 1, 0, 30, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(time, "time", lambda: clock)
    # It is May 1 in UTC, but still April 30 for this character.
    assert not engine.retrieval.search("nova", "Seattle")["facts"]


def test_reconciliation_pages_do_not_drop_older_candidates(engine):
    for i in range(31):
        engine.remember(
            "nova",
            MemoryDraft(
                key=f"job-{i}",
                text=f"Project number {i}",
                source_quote=f"Project {i}",
                details=current("user:project"),
            ),
        )
    older = {m.id for m in engine.memories("nova")}
    turn(engine, "I changed projects", key="new-project", details=current("user:project"))
    seen, pages = set(), 0
    while batch := engine.knowledge.prepare("nova"):
        seen.update(m["id"] for m in batch["candidates"])
        engine.knowledge.apply("nova", batch, Transitions())
        pages += 1
        assert pages <= 3
    assert seen == older and pages == 3


def test_reconciliation_cursor_survives_retirement_of_the_previous_page(engine, monkeypatch):
    for i in range(31):
        engine.remember(
            "nova",
            MemoryDraft(
                key=f"old-{i}",
                text=f"I own car {i}",
                source_quote=f"I own car {i}",
                details=current("user:car"),
            ),
        )
    older = {m.id for m in engine.memories("nova")}
    # Exercise the durable sweep, without lexical extras masking skipped rows.
    monkeypatch.setattr(engine.retrieval, "review_update", lambda *args: {"candidates": []})
    turn(engine, "I sold all those cars", key="sold", details=current("user:car"))
    seen, pages = set(), 0
    while batch := engine.knowledge.prepare("nova"):
        seen.update(m["id"] for m in batch["candidates"])
        transitions = []
        if pages == 0:
            for candidate in batch["candidates"]:
                old = engine._memory(engine._get_memory("nova", candidate["id"]))
                transitions.extend(decision(batch, old)["transitions"])
        engine.knowledge.apply("nova", batch, {"transitions": transitions})
        pages += 1
        assert pages <= 3
    assert pages == 3 and seen == older


def test_consolidation_notebook_and_character_core_keep_provenance(engine):
    tid = turn(engine, "I like tea", reply="In our story I grew up by the sea")
    user, assistant = tid + ":user", tid + ":assistant"
    notebook = {"entries": [{"section": "identity", "text": "Likes tea", "source_ids": [user]}]}
    engine.memory.consolidate_pair(
        "nova",
        "Discussed tea",
        "ignored",
        [user],
        core_source_ids=[user],
        notebook=notebook,
        character_core="Fictional childhood by the sea",
        character_source_ids=[assistant],
        episode_metadata={"importance": 7, "arousal": 0.2},
    )
    docs = engine.memory.documents("nova")
    core = next(d for d in docs if d["kind"] == "core")
    assert "Likes tea" in core["text"]
    assert core["notebook"] == {
        "entries": [{**notebook["entries"][0], "basis": "user_statement", "confidence": 1.0}]
    }
    assert next(d for d in docs if d["kind"] == "character_core")["source_ids"] == [assistant]
    with pytest.raises(ValueError, match="attributed original"):
        engine.memory.consolidate_pair(
            "nova",
            "Discussed tea",
            "bad",
            [assistant],
            core_source_ids=[assistant],
            notebook={
                "entries": [
                    {
                        "section": "identity",
                        "text": "User grew up by sea",
                        "source_ids": [assistant],
                    }
                ]
            },
        )
    # Explicitly empty structured core is allowed after all durable claims were removed.
    engine.memory.consolidate_pair(
        "nova", "Conversation retained", "", [user], core_source_ids=[], notebook={"entries": []}
    )
    assert next(d for d in engine.memory.documents("nova") if d["kind"] == "core")["text"] == ""


def test_working_window_retains_recent_turns_but_manual_flush_is_available(engine):
    runtime = Runtime(engine)
    runtime.automation.configure(
        "nova", MemoryAutomation(summaries=True, retain_recent_turns=2, summary_turns=1)
    )
    first = turn(engine, "Old conversation")
    turn(engine, "Middle conversation")
    turn(engine, "Recent conversation")
    policy = runtime.automation.policy("nova")
    sources = engine.memory.maintenance_sources("nova", policy)
    assert {s["turn_id"] for s in sources} == {first}
    all_sources = engine.memory.maintenance_sources(
        "nova", policy.model_copy(update={"retain_recent_turns": 0})
    )
    assert len(all_sources) == 6


def test_forget_removes_new_indexes_and_metadata(engine):
    tid = turn(engine, "I live in Boston", key="home", details=current("user:home"))
    engine.knowledge.prepare("nova")
    engine.memory.consolidate("nova", "episode", "Boston", [tid + ":user"])
    engine.forget_character("nova", confirmation="nova")
    for table in (
        "evidence_context",
        "evidence_chunks",
        "evidence_embeddings",
        "fact_lifecycle",
        "fact_reconciliation",
        "document_metadata",
        "note_dependencies",
    ):
        assert engine.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_background_consolidation_cannot_overwrite_a_newer_core(engine):
    tid = turn(engine, "An important event")
    version = engine.memory.core_version("nova")
    engine.memory.consolidate("nova", "core", "Newer curated core", [tid + ":user"])
    with pytest.raises(Conflict, match="Core changed"):
        engine.memory.consolidate_pair(
            "nova", "Episode", "Stale core", [tid + ":user"], expected_documents=version
        )
    assert engine.memory.documents("nova")[0]["text"] == "Newer curated core"

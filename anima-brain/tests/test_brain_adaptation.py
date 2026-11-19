"""Cross-turn regressions for the adapted local/host memory lifecycle."""

import json
from datetime import date, datetime, timezone

import pytest

from companion_engine import CharacterSpec, CommitRequest, Engine, MemoryDraft
from companion_engine.engine import Conflict
from companion_engine.models import (
    Appraisal,
    Attunement,
    MemoryCorrection,
    MemoryDetails,
    ObservedMedia,
    ResponseStyle,
    StyleRule,
    StyleSource,
    TemporalQuery,
)
from companion_engine.runtime import Runtime, Settings
from companion_engine.temporal import calendar, lookup


def turn(engine, text, **kwargs):
    import uuid

    c = engine.prepare_turn("nova", text, request_id=str(uuid.uuid4()))
    engine.commit_turn("nova", c.turn_id, CommitRequest(assistant_reply="Understood.", **kwargs))
    return c.turn_id


def test_old_episode_and_fact_retrieval_beyond_recent_windows(engine):
    eid = (
        turn(
            engine,
            "My dog is named Mochi.",
            memories=[MemoryDraft(key="pet", text="Dog named Mochi", source_quote="Mochi")],
        )
        + ":user"
    )
    old = engine.memory.consolidate("nova", "episode", "Adopted Mochi the dog", [eid])
    for i in range(205):
        engine.remember(
            "nova", MemoryDraft(key=f"noise{i}", text=f"Unrelated fact {i}", source_quote="x")
        )
    for i in range(12):
        engine.memory.consolidate("nova", "episode", f"Other day {i}", [eid])
    result = engine.retrieval.search("nova", "Mochi")
    assert result["facts"][0]["key"] == "pet"
    assert old["id"] in [d["id"] for d in result["documents"]]
    r = Runtime(engine, Settings())
    ctx = r.prepare("nova", "Mochi", "query")
    assert any(m.key == "pet" for m in ctx.memories)
    assert old["id"] in [d["id"] for d in ctx.core_memory]


def test_derived_vectors_invalidated_by_all_writers(engine):
    eid = (
        turn(
            engine,
            "I live in Paris.",
            memories=[MemoryDraft(key="home", text="Paris", source_quote="Paris")],
        )
        + ":user"
    )
    fact = engine.memories("nova")[0]
    item = next(
        r for r in engine.retrieval.candidates("nova", "test") if r["id"].startswith("fact:")
    )
    engine.retrieval.store("nova", item["id"], "test", [1, 0], item["text"])
    assert engine.retrieval.search("nova", "residence", vector=[1, 0], model="test")["facts"]
    engine.correct_memory(
        "nova",
        fact.id,
        MemoryDraft(key="home", text="Rome", source_quote="Rome"),
        expected_revision=1,
    )
    assert engine.db.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 0
    with pytest.raises(Conflict):
        engine.retrieval.store("nova", item["id"], "test", [1, 0], item["text"])
    engine.memory.consolidate("nova", "episode", "Rome", [eid])
    engine.forget_character("nova", confirmation="nova")
    assert not engine.db.execute("SELECT * FROM retrieval_search").fetchall()
    assert not engine.db.execute("SELECT * FROM retrieval_items").fetchall()


def test_state_candidates_find_old_cross_wording_facts(engine):
    engine.remember(
        "nova",
        MemoryDraft(
            key="old_job",
            text="Works at Atlas",
            source_quote="Atlas",
            details=MemoryDetails(state_key="user:employer", temporal_kind="current"),
        ),
    )
    for i in range(110):
        engine.remember("nova", MemoryDraft(key=str(i), text=f"Noise {i}", source_quote="noise"))
    proposal = MemoryDraft(
        key="new_job",
        text="Joined Boreal",
        source_quote="Joined Boreal",
        details=MemoryDetails(state_key="user:employer", temporal_kind="current"),
    )
    result = engine.retrieval.review_update("nova", proposal)
    assert any(r["key"] == "old_job" for r in result["candidates"])
    assert len(engine.memories("nova", limit=200)) == 111  # candidate search does not invalidate


def test_temporal_unknown_dates_events_and_paging(engine):
    engine.remember(
        "nova",
        MemoryDraft(
            key="unknown",
            text="Lives in Rome",
            source_quote="Rome",
            details=MemoryDetails(temporal_kind="current"),
        ),
    )
    engine.remember(
        "nova",
        MemoryDraft(
            key="event",
            text="Visited Tokyo in 2020",
            source_quote="Tokyo",
            details=MemoryDetails(temporal_kind="event", event_date=date(2020, 2, 1)),
        ),
    )
    asof = lookup(engine, "nova", TemporalQuery(mode="asof", start="2020-01-01"))
    assert not asof["facts"] and asof["uncertain_validity"]
    events = lookup(
        engine, "nova", TemporalQuery(mode="events", start="2020-01-01", end="2020-12-31")
    )
    assert [f["key"] for f in events["facts"]] == ["event"]
    for i in range(205):
        engine.remember("nova", MemoryDraft(key=f"x{i}", text="Extra", source_quote="Extra"))
    ids = []
    for offset in (0, 100, 200):
        page = lookup(engine, "nova", TemporalQuery(mode="history", offset=offset, limit=100))
        ids += [f["id"] for f in page["facts"]]
    assert len(ids) == len(set(ids)) == 207


def test_calendar_timezone_and_gentle_anniversary(engine):
    engine.update_character(
        CharacterSpec(id="nova", name="Nova", persona="Hello", timezone="America/Los_Angeles")
    )
    engine.remember(
        "nova",
        MemoryDraft(
            key="anniversary",
            text="A loss anniversary",
            source_quote="loss",
            category="date",
            details=MemoryDetails(
                event_date=date(2020, 12, 31), recurrence="annual", date_kind="gentle"
            ),
        ),
    )
    now = datetime(2026, 1, 1, 3, tzinfo=timezone.utc).timestamp()
    result = calendar(engine, "nova", now=now)
    assert result[0]["date"] == "2025-12-31" and result[0]["days_away"] == 0
    assert "do not congratulate" in result[0]["guidance"]
    with pytest.raises(ValueError):
        CharacterSpec(id="n", name="N", persona="p", timezone="not/a/zone")


def test_feedback_repairs_and_daily_weather_are_idempotent(engine):
    engine.update_character(CharacterSpec(id="nova", name="Nova", persona="Warm"))
    r = Runtime(engine, Settings())
    c = r.prepare("nova", "Please listen.", "one")
    assert c.character_state["wobble_day"]
    before = engine.relationship("nova")["state"]["axes"]
    r.commit("nova", c.turn_id, CommitRequest(assistant_reply="I hear you."))
    assert r.engine.continuity.backlog("nova")[0]["needs_appraisal"]
    a = Appraisal(source_quote="Please listen.", warmth=1)
    at = Attunement(intent="listen", user_cue="Explicit request", source_quote="Please listen.")
    for _ in range(2):
        engine.continuity.repair("nova", c.turn_id, attunement=at, appraisal=a)
    assert not engine.continuity.backlog("nova")
    assert engine.relationship("nova")["state"]["axes"] == before  # no late double delta
    c2 = r.prepare("nova", "That's not what I meant", "two")
    assert c2.reaction_feedback["label"] == "correction"
    r.prepare("nova", "That's not what I meant", "two")
    assert engine.db.execute("SELECT count(*) FROM reaction_links").fetchone()[0] == 1
    with pytest.raises(Conflict):
        engine.continuity.repair(
            "nova", c.turn_id, appraisal=Appraisal(source_quote="Please", warmth=2)
        )


def test_style_deactivates_when_source_changes(engine):
    mid = engine.remember(
        "nova",
        MemoryDraft(
            key="style", category="style", text="Avoid unsolicited advice", source_quote="No advice"
        ),
    )
    style = ResponseStyle(
        rules=[
            StyleRule(
                kind="avoid",
                text="Unsolicited advice",
                sources=[StyleSource(memory_id=mid.id, revision=1)],
            )
        ]
    )
    assert engine.continuity.set_style("nova", style)["rules"]
    engine.correct_memory(
        "nova",
        mid.id,
        MemoryDraft(
            key="style",
            category="style",
            text="Offer suggestions when relevant",
            source_quote="Suggestions",
        ),
        expected_revision=1,
    )
    assert engine.continuity.style("nova")["rules"] == []
    assert engine.continuity.style("nova")["stale_rules"] == 1
    with pytest.raises(Conflict):
        engine.continuity.set_style("nova", style, expected_revision=1)


def test_correction_preserves_unrelated_episode_and_marks_original(engine):
    first = turn(
        engine, "Paris", memories=[MemoryDraft(key="home", text="Paris", source_quote="Paris")]
    )
    second = turn(engine, "I love tea")
    engine.memory.consolidate("nova", "episode", "Paris", [first + ":user"])
    keep = engine.memory.consolidate("nova", "episode", "Tea", [second + ":user"])
    old = engine.memories("nova")[0]
    turn(
        engine,
        "Rome",
        corrections=[
            MemoryCorrection(
                memory_id=old.id,
                expected_revision=1,
                draft=MemoryDraft(key="home", text="Rome", source_quote="Rome"),
            )
        ],
    )
    assert [d["id"] for d in engine.memory.documents("nova")] == [keep["id"]]
    original = engine.memory.recall("nova", "Paris")["sources"][0]
    assert original["knowledge_updates"][0]["operation"] == "corrected"


def test_host_self_notebook_is_separate_and_cited(engine):
    r = Runtime(engine, Settings())
    turn(engine, "How was your day?")
    batch = r.host_memory.prepare("nova")
    ids = batch["batch_source_ids"]
    result = r.host_memory.commit(
        "nova",
        batch["batch_id"],
        "A conversation",
        "Asked about the day",
        ids,
        ids,
        character_core="No new life events established.",
        character_source_ids=ids,
    )
    assert result["saved"]
    assert {d["kind"] for d in engine.memory.documents("nova")} == {
        "core",
        "episode",
        "character_core",
    }
    assert r.host_memory.commit(
        "nova",
        batch["batch_id"],
        "A conversation",
        "Asked about the day",
        ids,
        ids,
        character_core="No new life events established.",
        character_source_ids=ids,
    )["replayed"]


def test_visual_observation_is_not_misrepresented_as_user_statement(engine):
    media = ObservedMedia(
        source_ref="attachment:photo1", observation="A brown dog with a red collar", confidence=0.8
    )
    ctx = engine.prepare_turn(
        "nova", "Look at my dog", request_id="photo", media_observations=[media]
    )
    with pytest.raises(Conflict):
        engine.prepare_turn("nova", "Look at my dog", request_id="photo")
    fact = MemoryDraft(
        key="dog",
        text="Dog appears to wear a red collar",
        source_quote="red collar",
        source_media_ref="attachment:photo1",
        details=MemoryDetails(confidence=0.8),
    )
    engine.commit_turn(
        "nova", ctx.turn_id, CommitRequest(assistant_reply="Cute dog.", memories=[fact])
    )
    assert engine.memories("nova")[0].origin == "host_observation"
    sources = engine.memory.recall("nova", "brown collar")["sources"]
    assert any(s["speaker"] == "host_observation" for s in sources)
    assert len(engine.export_character("nova")["turn_media"]) == 1


def test_layer_indexes_survive_restart_and_are_owner_scoped(engine):
    turn(engine, "茶", memories=[MemoryDraft(key="drink", text="喜欢乌龙茶", source_quote="茶")])
    assert engine.retrieval.search("nova", "乌龙")["facts"]
    with Engine(engine.database, owner="alice") as restored:
        assert restored.retrieval.search("nova", "乌龙")["facts"]
    with Engine(engine.database, owner="bob") as other:
        other.create_character(CharacterSpec(id="nova", name="Other", persona="Other"))
        assert not other.retrieval.search("nova", "乌龙")["facts"]


def test_worker_indexes_derived_layers_and_preserves_disabled_policy(engine):
    from test_automation import embeddings, settings

    calls = []
    r = Runtime(engine, settings(), transport=embeddings(calls))
    turn(engine, "Tea", memories=[MemoryDraft(key="drink", text="Tea", source_quote="Tea")])
    assert r.automation.run_once()
    assert engine.db.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 1
    assert len(calls) == 1
    before = len(calls)
    r.automation.configure("nova", {})
    turn(
        engine, "Coffee", memories=[MemoryDraft(key="coffee", text="Coffee", source_quote="Coffee")]
    )
    assert not r.automation.run_once()
    assert len(calls) == before


def test_host_summary_large_history_remains_bounded(engine):
    r = Runtime(engine, Settings())
    for i in range(80):
        text = f"Fact {i} " + "long detail " * 100
        turn(engine, text, memories=[MemoryDraft(key=str(i), text=text, source_quote=text)])
    batch = r.host_memory.prepare("nova")
    assert len(json.dumps(batch)) < 110000
    assert len(batch["sources"]) <= 60
    assert batch["batch_source_ids"]


def test_real_mcp_new_workflow_roundtrip(tmp_path):
    import asyncio
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from test_mcp import unpack

    async def scenario():
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "companion_engine", "--db", str(tmp_path / "mcp.db"), "serve"],
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=20) as client:
                await client.initialize()
                names = {t.name for t in (await client.list_tools()).tools}
                assert {
                    "review_memory_update",
                    "lookup_memory_time",
                    "update_response_style",
                    "inspect_continuity_tasks",
                    "complete_turn_analysis",
                } <= names
                unpack(
                    await client.call_tool(
                        "create_character",
                        {"spec": {"id": "luna", "name": "Luna", "persona": "Thoughtful companion"}},
                    )
                )
                ctx = unpack(
                    await client.call_tool(
                        "prepare_turn",
                        {
                            "character_id": "luna",
                            "user_message": "Please listen",
                            "request_id": "one",
                        },
                    )
                )
                unpack(
                    await client.call_tool(
                        "commit_turn",
                        {
                            "character_id": "luna",
                            "turn_id": ctx["turn_id"],
                            "request": {"assistant_reply": "I am listening."},
                        },
                    )
                )
                tasks = unpack(
                    await client.call_tool("inspect_continuity_tasks", {"character_id": "luna"})
                )
                assert tasks["pending_analysis"][0]["needs_attunement"]
                repaired = unpack(
                    await client.call_tool(
                        "complete_turn_analysis",
                        {
                            "character_id": "luna",
                            "turn_id": ctx["turn_id"],
                            "attunement": {
                                "intent": "listen",
                                "user_cue": "Explicit request",
                                "source_quote": "Please listen",
                            },
                            "appraisal": {"source_quote": "Please listen"},
                        },
                    )
                )
                assert not repaired["remaining"] and not repaired["state_applied"]
                dates = unpack(
                    await client.call_tool(
                        "lookup_memory_time",
                        {"character_id": "luna", "request": {"mode": "history"}},
                    )
                )
                assert not dates["facts"]

    asyncio.run(scenario())

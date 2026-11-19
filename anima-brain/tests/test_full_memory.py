import json
from datetime import date

import pytest

from companion_engine import CharacterSpec, CommitRequest, Engine, MemoryDraft
from companion_engine.engine import Conflict, NotFound
from companion_engine.memory import Note, NoteLink
from companion_engine.models import MemoryCorrection, MemoryDetails


def exchange(engine, text, reply="Understood", memories=None, corrections=None):
    import uuid

    context = engine.prepare_turn("nova", text, request_id=str(uuid.uuid4()))
    engine.commit_turn(
        "nova",
        context.turn_id,
        CommitRequest(
            assistant_reply=reply, memories=memories or [], corrections=corrections or []
        ),
    )
    return context.turn_id + ":user"


def test_original_evidence_attribution_restart(engine):
    eid = exchange(engine, "Mira recommended The Dispossessed.", "I visited Mars (fiction).")
    result = engine.memory.recall("nova", "Dispossessed")
    assert result["sources"][0]["id"] == eid
    assert result["sources"][0]["speaker"] == "user"
    with Engine(engine.database, owner="alice") as reopened:
        assert reopened.memory.recall("nova", "Mars")["sources"][0]["speaker"] == "assistant"
    with Engine(engine.database, owner="bob") as other:
        other.create_character(CharacterSpec(id="nova", name="Other", persona="Other"))
        assert not other.memory.recall("nova", "Dispossessed")["sources"]
        with pytest.raises(NotFound):
            other.memory.expand("nova", [eid])


def test_grounded_notes_links_and_revision(engine):
    first = exchange(engine, "I have an interview at Atlas.")
    second = exchange(engine, "The Atlas interview went well.")
    note = Note(
        description="Atlas job interview outcome",
        keywords=["Atlas", "career"],
        links=[
            NoteLink(
                target_id=first,
                relation="continuation",
                source_quote="Atlas interview",
                target_quote="interview at Atlas",
            )
        ],
    )
    assert engine.memory.annotate("nova", second, note)["revision"] == 1
    with pytest.raises(Conflict):
        engine.memory.annotate("nova", second, note)
    bad = note.model_copy(deep=True)
    bad.links[0].source_quote = "got hired"
    with pytest.raises(ValueError):
        engine.memory.annotate("nova", second, bad, expected_revision=1)
    assert first in {s["id"] for s in engine.memory.expand("nova", [second])["sources"]}


def test_embeddings_hybrid_and_stale_description(engine):
    eid = exchange(engine, "My canine companion is called Mochi.")
    engine.memory.store_vector("nova", eid, "model", [1, 0], "My canine companion is called Mochi.")
    found = engine.memory.recall("nova", "pet name", query_vector=[0.99, 0.01], model="model")
    assert found["mode"] == "hybrid_rrf" and found["sources"][0]["id"] == eid
    with pytest.raises(ValueError):
        engine.memory.recall("nova", "pet", query_vector=[1, 0, 0], model="model")
    engine.memory.annotate("nova", eid, Note(description="User's dog Mochi"))
    assert engine.db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0
    with pytest.raises(ValueError):
        engine.memory.store_vector(
            "nova", eid, "model", [1, 0], "My canine companion is called Mochi."
        )


def test_temporal_supersession_and_atomic_correction(engine):
    memory = MemoryDraft(
        key="residence",
        text="Lives in Boston",
        source_quote="Boston",
        details=MemoryDetails(temporal_kind="current", valid_from=date(2024, 1, 1)),
    )
    exchange(engine, "Boston", memories=[memory])
    old = engine.memories("nova")[0]
    newer = MemoryDraft(
        key="residence",
        text="Lives in Seattle",
        source_quote="Seattle",
        details=MemoryDetails(temporal_kind="current", valid_from=date(2025, 1, 1)),
    )
    exchange(
        engine,
        "Seattle",
        corrections=[
            MemoryCorrection(memory_id=old.id, expected_revision=1, kind="superseded", draft=newer)
        ],
    )
    assert engine.memory.facts_at("nova", "2024-06-01")["facts"][0]["text"] == "Lives in Boston"
    assert engine.memory.facts_at("nova", "2025-06-01")["facts"][0]["text"] == "Lives in Seattle"
    eid = engine.memory.recall("nova", "Seattle")["sources"][0]["id"]
    engine.memory.consolidate("nova", "core", "Seattle resident", [eid])
    engine.correct_memory(
        "nova",
        old.id,
        MemoryDraft(key="residence", text="Lives in Portland", source_quote="Portland"),
        expected_revision=2,
    )
    assert not engine.memory.documents("nova")


def test_off_mode_atomic_no_memories(engine):
    engine.update_character(
        CharacterSpec(id="nova", name="Nova", persona="Hi", memory_policy="off")
    )
    ctx = engine.prepare_turn("nova", "tea", request_id="off")
    with pytest.raises(ValueError):
        engine.commit_turn(
            "nova",
            ctx.turn_id,
            CommitRequest(
                assistant_reply="ok",
                memories=[MemoryDraft(key="drink", text="tea", source_quote="tea")],
            ),
        )
    assert engine.memories("nova") == []
    assert engine.db.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_forget_cascades_derived_data(engine):
    eid = exchange(engine, "tea")
    engine.memory.annotate("nova", eid, Note(description="tea preference"))
    engine.memory.store_vector("nova", eid, "test", [1, 2], "tea\ntea preference")
    engine.memory.consolidate("nova", "episode", "tea", [eid])
    engine.forget_character("nova", confirmation="nova")
    for table in ["evidence", "evidence_search", "vectors", "notes", "documents"]:
        assert engine.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_existing_schema_one_migrates_without_losing_facts(engine):
    engine.remember("nova", MemoryDraft(key="a", text="alpha", source_quote="alpha"))
    engine.db.execute("PRAGMA user_version=1")
    with Engine(engine.database, owner="alice") as next_engine:
        assert next_engine.search("nova", "alpha")[0].text == "alpha"
        assert next_engine.db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_export_details_not_silently_capped(engine):
    for i in range(205):
        engine.remember("nova", MemoryDraft(key=str(i), text=str(i), source_quote=str(i)))
    data = engine.export_character("nova")
    assert len(data["memory_details"]) == 205
    json.dumps(data)


def test_migration_backfills_originals_without_network(engine):
    eid = exchange(engine, "Original transcript survives")
    engine.db.execute("DELETE FROM evidence")
    engine.db.execute("PRAGMA user_version=1")
    with Engine(engine.database, owner="alice") as reopened:
        assert reopened.memory.recall("nova", "survives")["sources"][0]["id"] == eid
    assert engine.db.execute("SELECT count(*) FROM evidence").fetchone()[0] == 2


def test_same_claim_changed_metadata_requires_explicit_revision(engine):
    engine.remember(
        "nova", MemoryDraft(key="interview", text="Interview", source_quote="Interview")
    )
    with pytest.raises(Conflict):
        engine.remember(
            "nova",
            MemoryDraft(
                key="interview",
                text="Interview",
                source_quote="Interview",
                details=MemoryDetails(loop_status="resolved"),
            ),
        )


def test_core_stays_pinned_and_invalid_consolidation_is_atomic(engine):
    eid = exchange(engine, "I live in Boston")
    engine.memory.consolidate("nova", "core", "Lives in Boston", [eid])
    for i in range(12):
        engine.memory.consolidate("nova", "episode", f"Episode {i}", [eid])
    assert engine.memory.documents("nova")[0]["kind"] == "core"
    with pytest.raises(NotFound):
        engine.memory.consolidate_pair(
            "nova", "new episode", "bad core", [eid], core_source_ids=["invented"]
        )
    assert engine.memory.documents("nova")[0]["text"] == "Lives in Boston"


def test_history_cursor_has_no_overlap(engine):
    for i in range(52):
        exchange(engine, f"Message {i}")
    recent = engine.history("nova")
    old = engine.history("nova", before=recent[0]["cursor"])
    assert len(recent) == 50 and len(old) == 2
    assert not {t["id"] for t in recent} & {t["id"] for t in old}

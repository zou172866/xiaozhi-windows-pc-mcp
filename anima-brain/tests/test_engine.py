import concurrent.futures
import json
import sqlite3

import pytest

from companion_engine import (
    Appraisal,
    CharacterSpec,
    CommitRequest,
    Conflict,
    Engine,
    MemoryDraft,
    NotFound,
)


def draft(text="I prefer tea", key="preferred_drink", **kwargs):
    return MemoryDraft(key=key, text=text, source_quote=text, category="preference", **kwargs)


def committed(engine, message="I prefer tea", request_id="one"):
    context = engine.prepare_turn("nova", message, request_id=request_id)
    result = engine.commit_turn(
        "nova", context.turn_id, CommitRequest(assistant_reply="Got it.", memories=[draft(message)])
    )
    return context, result


def test_persistence_and_correction_survive_restart(engine):
    committed(engine)
    memory = engine.search("nova", "tea")[0]
    changed = engine.correct_memory(
        "nova", memory.id, draft("I prefer coffee"), expected_revision=1
    )
    assert changed.revision == 2
    assert engine.search("nova", "tea") == []
    with Engine(engine.database, owner="alice") as reopened:
        assert reopened.search("nova", "coffee")[0].text == "I prefer coffee"
        assert len(reopened.export_character("nova")["revisions"]) == 1


def test_scope_isolation_on_all_operations(engine):
    context, result = committed(engine)
    memory_id = result.memory_ids[0]
    with Engine(engine.database, owner="bob") as other:
        assert other.characters() == []
        for operation in (
            lambda: other.search("nova", "tea"),
            lambda: other.memories("nova"),
            lambda: other.export_character("nova"),
            lambda: other.pending_turn("nova"),
            lambda: other.commit_turn(
                "nova", context.turn_id, CommitRequest(assistant_reply="Got it.")
            ),
            lambda: other.correct_memory("nova", memory_id, draft("coffee"), expected_revision=1),
            lambda: other.delete_memory("nova", memory_id, expected_revision=1),
            lambda: other.forget_character("nova", confirmation="nova"),
        ):
            with pytest.raises(NotFound):
                operation()
        other.create_character(CharacterSpec(id="nova", name="Other", persona="Different"))
        assert other.memories("nova") == []
        other.remember("nova", draft("Bob likes tea"))
        other.forget_character("nova", confirmation="nova")
    assert engine.search("nova", "tea")[0].text == "I prefer tea"


def test_character_isolation(engine):
    engine.create_character(CharacterSpec(id="sage", name="Sage", persona="Quiet"))
    _, result = committed(engine)
    assert engine.search("sage", "tea") == []
    with pytest.raises(NotFound):
        engine.correct_memory("sage", result.memory_ids[0], draft("coffee"), expected_revision=1)


def test_exact_retry_and_changed_retry(engine):
    ctx = engine.prepare_turn("nova", "I prefer tea", request_id="one")
    again = engine.prepare_turn("nova", "I prefer tea", request_id="one")
    assert ctx.turn_id == again.turn_id
    request = CommitRequest(assistant_reply="Got it", memories=[draft()])
    first = engine.commit_turn("nova", ctx.turn_id, request)
    assert not first.replayed
    assert engine.commit_turn("nova", ctx.turn_id, request).replayed
    assert len(engine.memories("nova")) == 1
    with pytest.raises(Conflict):
        engine.commit_turn("nova", ctx.turn_id, CommitRequest(assistant_reply="Changed"))
    with pytest.raises(Conflict):
        engine.prepare_turn("nova", "I prefer tea", request_id="one")


def test_bad_evidence_rolls_back_all(engine):
    ctx = engine.prepare_turn("nova", "I prefer tea", request_id="one")
    with pytest.raises(ValueError, match="exact quote"):
        engine.commit_turn(
            "nova",
            ctx.turn_id,
            CommitRequest(
                assistant_reply="ok",
                memories=[draft(), draft("assistant invented this", key="made_up")],
            ),
        )
    assert engine.memories("nova") == []
    assert engine.pending_turn("nova")["id"] == ctx.turn_id


def test_appraisal_failure_rolls_back_memory_and_transcript(engine):
    ctx = engine.prepare_turn("nova", "I prefer tea", request_id="one")
    with pytest.raises(ValueError, match="not enabled"):
        engine.commit_turn(
            "nova",
            ctx.turn_id,
            CommitRequest(
                assistant_reply="ok",
                memories=[draft()],
                appraisal=Appraisal(source_quote="tea", warmth=1),
            ),
        )
    assert engine.memories("nova") == []
    assert engine.export_character("nova")["turns"][0]["assistant_reply"] is None


def test_pending_recovery_abort(engine):
    ctx = engine.prepare_turn("nova", "hello", request_id="one")
    with pytest.raises(Conflict):
        engine.prepare_turn("nova", "new", request_id="two")
    engine.abort_turn("nova", ctx.turn_id)
    engine.abort_turn("nova", ctx.turn_id)
    with pytest.raises(Conflict):
        engine.commit_turn("nova", ctx.turn_id, CommitRequest(assistant_reply="late"))
    assert engine.prepare_turn("nova", "new", request_id="two").recent_turns == []


def test_stale_revision_and_no_implicit_overwrite(engine):
    original = engine.remember("nova", draft())
    engine.correct_memory("nova", original.id, draft("I prefer coffee"), expected_revision=1)
    with pytest.raises(Conflict):
        engine.correct_memory("nova", original.id, draft(), expected_revision=1)
    with pytest.raises(Conflict):
        engine.delete_memory("nova", original.id, expected_revision=1)
    with pytest.raises(Conflict):
        engine.remember("nova", draft())
    assert engine.memories("nova")[0].text == "I prefer coffee"


def test_cjk_retrieval_and_adversarial_query(engine):
    memory = engine.remember("nova", draft("我喜欢喝乌龙茶", "drink"))
    assert engine.search("nova", "乌龙茶")[0].id == memory.id
    assert engine.search("nova", "茶")[0].id == memory.id
    assert engine.search("nova", '" OR * - NOT (') == []
    assert engine.search("nova", "") == []


def test_current_boundaries_are_always_in_context(engine):
    boundary = MemoryDraft(
        key="boundary",
        text="Do not call me baby",
        category="boundary",
        source_quote="Do not call me baby",
    )
    engine.remember("nova", boundary)
    ctx = engine.prepare_turn("nova", "weather?", request_id="x")
    assert ctx.memories[0].category == "boundary"
    assert "current user correction" in ctx.response_guidance


def test_context_budget_does_not_truncate_user(engine):
    with Engine(engine.database, owner="alice", context_chars=4000) as small:
        with pytest.raises(ValueError, match="budget"):
            small.prepare_turn("nova", "x" * 5000, request_id="too_big")
        assert small.pending_turn("nova") is None
        for n in range(8):
            ctx = small.prepare_turn("nova", "hello " + str(n), request_id=str(n))
            assert len(ctx.model_dump_json()) <= 4000
            small.commit_turn("nova", ctx.turn_id, CommitRequest(assistant_reply="a" * 1500))
        ctx = small.prepare_turn("nova", "current", request_id="next")
        assert ctx.user_message == "current"
        assert ctx.omitted_recent_turns > 0


def test_delete_memory_removes_revisions_index_but_not_source(engine):
    _, result = committed(engine)
    ident = result.memory_ids[0]
    engine.correct_memory("nova", ident, draft("coffee"), expected_revision=1)
    engine.delete_memory("nova", ident, expected_revision=2)
    data = engine.export_character("nova")
    assert data["memories"] == data["revisions"] == []
    assert engine.search("nova", "coffee tea") == []
    assert data["turns"][0]["user_message"] == "I prefer tea"


def test_forget_cascades_all_owned_data(engine):
    committed(engine)
    with pytest.raises(ValueError):
        engine.forget_character("nova", confirmation="wrong")
    engine.forget_character("nova", confirmation="nova")
    for table in (
        "characters",
        "turns",
        "memories",
        "revisions",
        "memory_search",
        "provider_usage",
    ):
        assert engine.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_future_schema_rejected_without_changes(tmp_path):
    db = tmp_path / "future.db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=9")
    with pytest.raises(ValueError, match="Unsupported"):
        Engine(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9


def test_concurrent_identical_prepare(engine):
    def run(_):
        with Engine(engine.database, owner="alice") as conn:
            return conn.prepare_turn("nova", "hello", request_id="same").turn_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(run, range(8)))) == 1


def test_concurrent_initialization(tmp_path):
    def run(_):
        with Engine(tmp_path / "fresh.sqlite3") as conn:
            return conn.characters()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(run, range(8))) == [[]] * 8


def test_relationship_idempotency_and_romance_optin(engine):
    spec = engine.characters()[0].model_copy(update={"relationship_enabled": True})
    engine.update_character(spec)
    ctx = engine.prepare_turn("nova", "thanks for listening", request_id="one")
    request = CommitRequest(
        assistant_reply="Of course", appraisal=Appraisal(source_quote="thanks", warmth=2)
    )
    engine.commit_turn("nova", ctx.turn_id, request)
    state = engine.export_character("nova")["state"]
    engine.commit_turn("nova", ctx.turn_id, request)
    assert engine.export_character("nova")["state"] == state
    ctx = engine.prepare_turn("nova", "hi", request_id="two")
    with pytest.raises(ValueError, match="Romance"):
        engine.commit_turn(
            "nova",
            ctx.turn_id,
            CommitRequest(assistant_reply="hey", appraisal=Appraisal(source_quote="hi", romance=1)),
        )


def test_export_json_portable(engine):
    committed(engine)
    assert json.loads(json.dumps(engine.export_character("nova")))["version"] == 2


def test_key_conflict_rolls_back_earlier_insert(engine):
    engine.remember("nova", draft("coffee"))
    ctx = engine.prepare_turn("nova", "I prefer tea", request_id="one")
    with pytest.raises(Conflict):
        engine.commit_turn(
            "nova",
            ctx.turn_id,
            CommitRequest(assistant_reply="ok", memories=[draft(key="new_key"), draft()]),
        )
    assert [m.key for m in engine.memories("nova")] == ["preferred_drink"]
    assert engine.pending_turn("nova")


def test_duplicate_keys_rejected(engine):
    ctx = engine.prepare_turn("nova", "I prefer tea", request_id="one")
    with pytest.raises(ValueError, match="Duplicate"):
        engine.commit_turn(
            "nova", ctx.turn_id, CommitRequest(assistant_reply="ok", memories=[draft(), draft()])
        )
    assert engine.memories("nova") == []


def test_replay_after_delete_does_not_restore_fact(engine):
    ctx, receipt = committed(engine)
    engine.delete_memory("nova", receipt.memory_ids[0], expected_revision=1)
    again = engine.commit_turn(
        "nova", ctx.turn_id, CommitRequest(assistant_reply="Got it.", memories=[draft()])
    )
    assert again.replayed
    assert engine.memories("nova") == []


def test_recent_correction_in_context_even_when_query_does_not_match(engine):
    memory = engine.remember("nova", draft("tea"))
    engine.correct_memory("nova", memory.id, draft("coffee"), expected_revision=1)
    ctx = engine.prepare_turn("nova", "what would you pick?", request_id="one")
    assert [m.text for m in ctx.memories] == ["coffee"]


def test_concurrent_corrections_one_wins(engine):
    memory = engine.remember("nova", draft())

    def change(value):
        with Engine(engine.database, owner="alice") as other:
            try:
                other.correct_memory("nova", memory.id, draft(value), expected_revision=1)
                return "updated"
            except Conflict:
                return "stale"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, ["coffee", "water"])) == ["stale", "updated"]


def test_invalid_model_instance_is_revalidated(engine):
    invalid = engine.characters()[0].model_copy(update={"romance_enabled": True})
    with pytest.raises(ValueError, match="Romance"):
        engine.update_character(invalid)
    assert engine.characters()[0].romance_enabled is False

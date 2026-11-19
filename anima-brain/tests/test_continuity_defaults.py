import asyncio

import pytest
from test_automation import embeddings, settings

from companion_engine import Appraisal, CharacterSpec, CommitRequest, Engine, MemoryDraft
from companion_engine.mcp_server import create_server
from companion_engine.models import Attunement, MemoryCorrection, MemoryNotebook
from companion_engine.runtime import Runtime, Settings


def setup(tmp_path, **kwargs):
    e = Engine(tmp_path / "brain.db", **kwargs)
    e.create_character(CharacterSpec(id="luna", name="Luna", persona="A thoughtful companion."))
    return e


def exchange(r, text, key, **kwargs):
    c = r.prepare("luna", text, key)
    return c, r.commit("luna", c.turn_id, CommitRequest(assistant_reply="Understood.", **kwargs))


def test_defaults_and_configured_indexer(tmp_path):
    with setup(tmp_path) as e:
        r = Runtime(e, Settings())
        p = r.automation.policy("luna")
        assert p.embedding and p.semantic_retrieval and p.host_summaries and not p.summaries
        spec = e.characters()[0]
        assert spec.relationship_enabled and not spec.romance_enabled
        assert spec.memory_policy == "automatic"
        c, _ = exchange(r, "I like tea.", "first")
        assert c.retrieval_mode == "lexical_embedding_provider_missing"
        assert not r.automation.run_once()
        calls = []
        r = Runtime(e, settings(), transport=embeddings(calls))
        assert r.automation.run_once()
        assert r.automation.status("luna")["indexed_count"] == 2
        assert not r.automation.run_once()
        c = r.prepare("luna", "Which drink?", "second")
        assert "semantic" in c.retrieval_mode or "hybrid" in c.retrieval_mode
        assert calls and c.evidence
        e.abort_turn("luna", c.turn_id)
        r.automation.configure("luna", {})
        assert not r.automation.policy("luna").embedding


def test_emotions_persist_are_scoped_and_not_facts(tmp_path):
    with setup(tmp_path) as e:
        r = Runtime(e, Settings())
        c, result = exchange(
            r,
            "Please just listen today.",
            "one",
            attunement=Attunement(
                intent="listen",
                user_cue="Wants listening",
                source_quote="just listen",
                uncertainty="Underlying mood unknown",
            ),
            appraisal=Appraisal(
                emotion="warm", intensity=1, warmth=1, source_quote="Please just listen"
            ),
        )
        assert result["continuity_status"]["attunement_recorded"]
        assert result["continuity_status"]["character_appraisal_recorded"]
        assert not e.memories("luna")
        path, owner = e.database, e.owner
    with Engine(path, owner=owner) as e:
        r = Runtime(e, Settings())
        c = r.prepare("luna", "Actually, help me plan now.", "two")
        assert c.recent_attunement[0]["interpretation"]["intent"] == "listen"
        assert c.character_state["axes"]["warmth"] > 0
        assert "corrections take priority" in c.recent_attunement[0]["caution"]
        e.abort_turn("luna", c.turn_id)
        e.create_character(CharacterSpec(id="other", name="Other", persona="Another companion"))
        other = r.prepare("other", "Hello", "other")
        assert not other.recent_attunement
        assert other.character_state["axes"]["warmth"] < c.character_state["axes"]["warmth"]


def commit_notebook(r, text, source_id, section="life_now", **kwargs):
    b = r.host_memory.prepare("luna")
    notebook = {
        "entries": []
        if text is None
        else [dict(section=section, text=text, source_ids=[source_id], **kwargs)]
    }
    return r.host_memory.commit(
        "luna",
        b["batch_id"],
        "Conversation recorded.",
        "",
        b["batch_source_ids"],
        [source_id] if text else [],
        notebook,
    )


def test_cited_notebook_correction_rebuild_and_delete(tmp_path):
    with setup(tmp_path) as e:
        r = Runtime(e, Settings())
        c, _ = exchange(
            r,
            "I live in Boston.",
            "one",
            memories=[
                MemoryDraft(key="city", text="Lives in Boston", source_quote="I live in Boston.")
            ],
        )
        commit_notebook(r, "Lives in Boston", c.turn_id + ":user")
        doc = next(d for d in e.memory.documents("luna") if d["kind"] == "core")
        assert doc["notebook"]["entries"][0]["section"] == "life_now"
        fact = e.memories("luna")[0]
        c2, _ = exchange(
            r,
            "I moved to New York.",
            "two",
            corrections=[
                MemoryCorrection(
                    memory_id=fact.id,
                    expected_revision=1,
                    kind="superseded",
                    draft=MemoryDraft(
                        key="city", text="Lives in New York", source_quote="I moved to New York."
                    ),
                )
            ],
        )
        assert not e.memory.documents("luna")
        assert r.host_memory.notice("luna")["due"]
        batch = r.host_memory.prepare("luna")
        assert batch["knowledge_updates"][0]["operation"] == "superseded"
        assert batch["current_facts"][0]["text"] == "Lives in New York"
        commit_notebook(r, "Lives in New York", c2.turn_id + ":user")
        assert not r.host_memory.notice("luna")["due"]
        e.delete_memory("luna", fact.id, expected_revision=2)
        assert r.host_memory.notice("luna")["due"]
        assert r.host_memory.notice("luna")["pending_turns"] == 0
        commit_notebook(r, None, None)
        assert not r.host_memory.notice("luna")["due"]
        assert next(d for d in e.memory.documents("luna") if d["kind"] == "core")["notebook"] == {
            "entries": []
        }
        assert not e.memories("luna")
        c3 = r.prepare("luna", "Where do I live?", "three")
        assert c3.continuity["knowledge_updates"][0]["operation"] == "deleted"


def test_notebook_requires_grounded_uncertainty(tmp_path):
    with setup(tmp_path) as e:
        r = Runtime(e, Settings())
        c, _ = exchange(r, "I am tired today.", "one")
        with pytest.raises(ValueError, match="user-authored"):
            commit_notebook(r, "Always tired", c.turn_id + ":assistant")
        with pytest.raises(ValueError, match="interpretation"):
            MemoryNotebook(
                entries=[
                    dict(
                        section="read_on_them",
                        text="May want quiet",
                        source_ids=[c.turn_id + ":user"],
                    )
                ]
            )
        with pytest.raises(ValueError, match="uncertainty"):
            commit_notebook(
                r,
                "May want quiet",
                c.turn_id + ":user",
                "read_on_them",
                basis="interpretation",
                confidence=1,
            )
        commit_notebook(
            r,
            "May want quiet today; not confirmed",
            c.turn_id + ":user",
            "read_on_them",
            basis="interpretation",
            confidence=0.5,
        )
        assert "[Tentative interpretation]" in e.memory.documents("luna")[0]["text"]


def test_runtime_budget_and_tool_discovery(tmp_path):
    with setup(tmp_path, context_chars=6000) as e:
        r = Runtime(e, Settings())
        for n in range(110):
            e.remember(
                "luna",
                MemoryDraft(key=str(n), text="Long fact " + "x" * 100, source_quote="Manual"),
            )
        c = r.prepare("luna", "Hello", "one")
        assert len(c.model_dump_json()) <= 6000
        if "omitted_facts" in c.continuity:
            assert c.continuity["omitted_facts"] + len(c.continuity["current_facts"]) == 110

        async def check():
            server = create_server(e, r)
            tools = {t.name: t for t in await server.list_tools()}
            assert {
                "prepare_memory_summary",
                "commit_memory_summary",
                "configure_memory_automation",
                "memory_maintenance_status",
                "get_companion_workflow",
            } <= set(tools)
            assert "notebook" in tools["commit_memory_summary"].input_schema["properties"]

        asyncio.run(check())

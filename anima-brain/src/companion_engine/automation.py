"""Durable local maintenance with explicit per-character overrides.

One leased job per character across processes.

Provider outcomes after a crash are uncertain: quarantine, never silently recharge.
The SQLite queue is for a local owner, not a distributed cloud scheduler.
"""

import json
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

from pydantic import Field, model_validator

from .models import Model


class MemoryAutomation(Model):
    embedding: bool = False
    summaries: bool = False
    reconcile: bool = False
    summary_batch_messages: int = Field(default=20, ge=2, le=60, strict=True)
    retain_recent_turns: int = Field(default=0, ge=0, le=30, strict=True)
    host_summaries: bool = False
    semantic_retrieval: bool = False
    summary_turns: int = Field(default=4, ge=1, le=100)
    summary_chars: int = Field(default=12000, ge=1000, le=100000)
    idle_seconds: int = Field(default=300, ge=30, le=86400)
    daily_jobs: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def one_summary_writer(self):
        if self.summaries and self.host_summaries:
            raise ValueError("Choose host summaries or background summaries, not both")
        return self


def default_policy():
    """New/unconfigured companions use host summaries plus configured embeddings."""
    return MemoryAutomation(embedding=True, semantic_retrieval=True, host_summaries=True)


class Automation:
    def __init__(self, runtime):
        self.runtime = runtime
        self.engine = runtime.engine
        with self.engine._transaction():
            self.engine.db.execute(
                "CREATE TABLE IF NOT EXISTS memory_automation (owner TEXT, character_id TEXT, "
                "config TEXT NOT NULL, PRIMARY KEY(owner,character_id), FOREIGN KEY "
                "(owner,character_id) REFERENCES characters(owner,id) ON DELETE CASCADE)"
            )
            self.engine.db.execute(
                "CREATE TABLE IF NOT EXISTS memory_jobs (id TEXT PRIMARY KEY, owner TEXT, "
                "character_id TEXT, kind TEXT NOT NULL, state TEXT NOT NULL, created REAL, "
                "lease_until REAL, error TEXT, FOREIGN KEY(owner,character_id) "
                "REFERENCES characters(owner,id) ON DELETE CASCADE)"
            )
            self.engine.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS memory_job_active ON memory_jobs"
                "(owner,character_id) WHERE state IN ('running','review')"
            )
            self.engine.db.execute(
                "CREATE INDEX IF NOT EXISTS memory_jobs_scope ON memory_jobs"
                "(owner,character_id,created)"
            )

    def policy(self, character_id):
        with self.engine._lock:
            self.engine._character(character_id)
            row = self.engine.db.execute(
                "SELECT config FROM memory_automation WHERE owner=? AND character_id=?",
                (self.engine.owner, character_id),
            ).fetchone()
            return (
                MemoryAutomation.model_validate_json(row[0])
                if row
                else self.runtime.default_automation or default_policy()
            )

    def configure(self, character_id, policy):
        policy = MemoryAutomation.model_validate(policy)
        with self.engine._transaction():
            self.engine._character(character_id)
            self.engine.db.execute(
                "INSERT INTO memory_automation VALUES(?,?,?) ON CONFLICT(owner,character_id) "
                "DO UPDATE SET config=excluded.config",
                (self.engine.owner, character_id, policy.model_dump_json()),
            )
        return self.status(character_id)

    def backlog(self, character_id):
        """Committed evidence itself is the durable queue; no post-commit enqueue gap."""
        rows = self.engine.db.execute(
            "SELECT count(*),count(DISTINCT e.turn_id),coalesce(sum(length(e.text)),0),"
            "max(e.observed_at) FROM evidence e WHERE e.owner=? AND e.character_id=? "
            "AND NOT EXISTS (SELECT 1 FROM maintenance_processed p WHERE p.evidence_id=e.id)",
            (self.engine.owner, character_id),
        ).fetchone()
        return dict(zip(("excerpts", "turns", "chars", "latest"), rows))

    def availability(self, policy):
        s = self.runtime.settings
        issues = []
        if (policy.embedding or policy.semantic_retrieval) and not (
            s.embedding_base_url and s.embedding_model
        ):
            issues.append("embedding_provider_missing")
        if s.summary_base_url or s.summary_model:
            summary_ready = bool(s.summary_base_url and s.summary_model)
        elif self.runtime._provider is not None:
            summary_ready = hasattr(self.runtime._provider, "structured")
        else:
            summary_ready = bool(s.base_url and s.model)
        if (policy.summaries or policy.reconcile) and not summary_ready:
            issues.append("summary_provider_missing")
        return issues

    def status(self, character_id):
        policy = self.policy(character_id)
        with self.engine._lock:
            latest = self.engine.db.execute(
                "SELECT id,kind,state,created,error FROM memory_jobs WHERE owner=? "
                "AND character_id=? ORDER BY created DESC LIMIT 10",
                (self.engine.owner, character_id),
            ).fetchall()
            total = self.engine.db.execute(
                "SELECT count(*) FROM evidence WHERE owner=? AND character_id=?",
                (self.engine.owner, character_id),
            ).fetchone()[0]
            from .media import Embeddings

            namespace = (
                Embeddings(self.runtime.settings, self.runtime.transport).namespace
                if self.runtime.settings.embedding_base_url
                and self.runtime.settings.embedding_model
                else ""
            )
            indexed = self.engine.db.execute(
                "SELECT count(DISTINCT c.evidence_id) FROM evidence_embeddings v "
                "JOIN evidence_chunks c ON c.id=v.chunk_id JOIN evidence e ON e.id=c.evidence_id "
                "WHERE e.owner=? AND e.character_id=? AND v.model=? AND v.lane='raw'",
                (self.engine.owner, character_id, namespace),
            ).fetchone()[0]
            spec = json.loads(self.engine._character(character_id)["spec"])
            return {
                "policy": policy.model_dump(),
                "provider_issues": self.availability(policy),
                "memory_policy": spec["memory_policy"],
                "evidence_count": total,
                "indexed_count": indexed,
                "derived_unindexed": len(
                    self.engine.retrieval.candidates(character_id, namespace, 1000)
                ),
                "pending_analysis": [
                    {k: row[k] for k in ("id", "needs_attunement", "needs_appraisal")}
                    for row in self.engine.continuity.backlog(character_id)
                ],
                "summary_backlog": self.backlog(character_id),
                "host_summary": self.runtime.host_memory.notice(character_id),
                "jobs": [dict(r) for r in latest],
                "worker_required": "Keep companion http, serve, or worker running.",
            }

    def acknowledge(self, character_id, job_id):
        """Explicit user retry authorization; new calls may incur duplicate provider cost."""
        with self.engine._transaction():
            self.engine._character(character_id)
            changed = self.engine.db.execute(
                "UPDATE memory_jobs SET state='acknowledged' WHERE id=? AND owner=? "
                "AND character_id=? AND state='review'",
                (job_id, self.engine.owner, character_id),
            ).rowcount
            if not changed:
                raise ValueError("No interrupted/failed job awaiting review")
            # A dead worker may have left its provider-operation reservation running.
            self.engine.db.execute(
                "UPDATE operations SET status='failed_unknown' WHERE owner=? "
                "AND character_id=? AND request_id=? AND status='running'",
                (self.engine.owner, character_id, "auto-" + job_id),
            )
        return self.status(character_id)

    def claim(self):
        from .media import Embeddings

        now = time.time()
        with self.engine._transaction():
            self.engine.db.execute(
                "UPDATE memory_jobs SET state='review',error='interrupted_outcome_unknown' "
                "WHERE owner=? AND state='running' AND lease_until<?",
                (self.engine.owner, now),
            )
            # Explicit policies (including disabled ones) override automatic defaults.
            rows = self.engine.db.execute(
                "SELECT c.id AS character_id,a.config,c.spec FROM characters c "
                "LEFT JOIN memory_automation a ON c.owner=a.owner AND c.id=a.character_id "
                "WHERE c.owner=? ORDER BY coalesce((SELECT max(j.created) FROM memory_jobs j "
                "WHERE j.owner=c.owner AND j.character_id=c.id),0)",
                (self.engine.owner,),
            ).fetchall()
            for row in rows:
                cid = row["character_id"]
                p = (
                    MemoryAutomation.model_validate_json(row["config"])
                    if row["config"]
                    else self.runtime.default_automation or default_policy()
                )
                if json.loads(row["spec"])["memory_policy"] == "off":
                    continue
                if self.engine.db.execute(
                    "SELECT 1 FROM memory_jobs WHERE owner=? AND character_id=? "
                    "AND state IN ('running','review')",
                    (self.engine.owner, cid),
                ).fetchone():
                    continue
                used = self.engine.db.execute(
                    "SELECT count(*) FROM memory_jobs WHERE owner=? AND character_id=? "
                    "AND created>?",
                    (self.engine.owner, cid, now - 86400),
                ).fetchone()[0]
                if used >= p.daily_jobs:
                    continue
                issues = self.availability(p)
                backlog = self.backlog(cid)
                kind = None
                if (
                    p.reconcile
                    and "summary_provider_missing" not in issues
                    and self.engine.knowledge.pending(cid)
                ):
                    kind = "reconcile"
                dirty = self.engine.db.execute(
                    "SELECT 1 FROM knowledge_updates WHERE owner=? AND character_id=? "
                    "AND processed=0 LIMIT 1",
                    (self.engine.owner, cid),
                ).fetchone()
                if (
                    not kind
                    and p.summaries
                    and "summary_provider_missing" not in issues
                    and (backlog["excerpts"] or dirty)
                ):
                    if (
                        dirty
                        or backlog["turns"] >= p.summary_turns
                        or backlog["chars"] >= p.summary_chars
                        or now - backlog["latest"] >= p.idle_seconds
                    ):
                        if self.engine.memory.maintenance_sources(cid, p):
                            kind = "maintenance"
                if not kind and p.embedding and "embedding_provider_missing" not in issues:
                    embed = Embeddings(self.runtime.settings, self.runtime.transport)
                    if self.engine.evidence_index.candidates(
                        cid, embed.namespace, limit=1
                    ) or self.engine.retrieval.candidates(cid, embed.namespace, limit=1):
                        kind = "index"
                if kind:
                    jid = str(uuid4())
                    self.engine.db.execute(
                        "INSERT INTO memory_jobs VALUES(?,?,?,?,?,?,?,NULL)",
                        (jid, self.engine.owner, cid, kind, "running", now, now + 120),
                    )
                    return {"id": jid, "character_id": cid, "kind": kind}
        return None

    def run_once(self):
        job = self.claim()
        if not job:
            return False
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(20):
                with self.engine._transaction():
                    self.engine.db.execute(
                        "UPDATE memory_jobs SET lease_until=? WHERE id=? AND state='running'",
                        (time.time() + 120, job["id"]),
                    )

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        state, error = "succeeded", None
        try:
            method = {
                "maintenance": self.runtime.maintain,
                "index": self.runtime.index,
                "reconcile": self.runtime.reconcile,
            }[job["kind"]]
            method(
                job["character_id"],
                "auto-" + job["id"],
                **({"flush": False} if job["kind"] == "maintenance" else {}),
            )
        except Exception:
            # Never expose provider response text/credentials through the status UI.
            state, error = "review", "provider_or_validation_failed_outcome_unknown"
        finally:
            stop.set()
            thread.join()
            with self.engine._transaction():
                self.engine.db.execute(
                    "UPDATE memory_jobs SET state=?,error=? WHERE id=?",
                    (state, error, job["id"]),
                )
        return True

    @contextmanager
    def worker(self):
        stop = threading.Event()

        def run():
            while not stop.is_set():
                try:
                    worked = self.run_once()
                except Exception:
                    worked = False
                stop.wait(0.1 if worked else 5)

        thread = threading.Thread(target=run, daemon=True, name="companion-memory")
        thread.start()
        try:
            yield self
        finally:
            stop.set()
            # Finish the bounded in-flight request before closing its SQLite connection.
            thread.join()

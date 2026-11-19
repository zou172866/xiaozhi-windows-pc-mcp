"""Causal contextual representations, independent raw/note vectors and chunk offsets.

Original messages are immutable. Index at most 24k characters in overlapping chunks;
retain the whole source for inspection. Old uncontextualized vectors are not relabelled.
"""

import hashlib
import heapq
import json
from datetime import datetime, timezone

from .memory import checked_vector


def serialize(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EvidenceIndex:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db
        with engine._transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS evidence_context (evidence_id TEXT PRIMARY KEY "
                "REFERENCES evidence(id) ON DELETE CASCADE, context TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS evidence_chunks (id TEXT PRIMARY KEY, "
                "evidence_id TEXT REFERENCES evidence(id) ON DELETE CASCADE, "
                "start INTEGER, end INTEGER, raw TEXT NOT NULL, UNIQUE(evidence_id,start))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS evidence_embeddings (chunk_id TEXT REFERENCES "
                "evidence_chunks(id) ON DELETE CASCADE, lane TEXT, model TEXT, digest TEXT, "
                "vector TEXT, PRIMARY KEY(chunk_id,lane,model))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS note_dependencies (evidence_id TEXT REFERENCES "
                "evidence(id) ON DELETE CASCADE, target_id TEXT REFERENCES evidence(id) "
                "ON DELETE CASCADE, source_quote TEXT, target_quote TEXT, "
                "PRIMARY KEY(evidence_id,target_id))"
            )
            # Deterministic migration: use causal message order, never newly appended future turns.
            rows = self.db.execute(
                "SELECT e.* FROM evidence e JOIN turns t ON t.id=e.turn_id WHERE NOT EXISTS "
                "(SELECT 1 FROM evidence_context c WHERE c.evidence_id=e.id) "
                "ORDER BY t.created_at,t.rowid,CASE e.speaker WHEN 'user' THEN 0 "
                "WHEN 'host_observation' THEN 1 ELSE 2 END"
            ).fetchall()
            for row in rows:
                self.add(dict(row))

    def add(self, row):
        """Called in the source commit transaction. Current assistant is never a user hint."""
        t = self.db.execute("SELECT rowid FROM turns WHERE id=?", (row["turn_id"],)).fetchone()[0]
        previous = self.db.execute(
            "SELECT e.id,e.speaker,e.text,e.observed_at FROM evidence e JOIN turns t "
            "ON t.id=e.turn_id WHERE e.owner=? AND e.character_id=? AND "
            "(t.created_at<? OR (t.created_at=? AND t.rowid<?) OR "
            "(t.id=? AND e.speaker='user' AND ?!='user')) "
            "ORDER BY t.created_at DESC,t.rowid DESC,CASE e.speaker WHEN 'assistant' THEN 2 "
            "WHEN 'host_observation' THEN 1 ELSE 0 END DESC LIMIT 2",
            (
                row["owner"],
                row["character_id"],
                row["observed_at"],
                row["observed_at"],
                t,
                row["turn_id"],
                row["speaker"],
            ),
        ).fetchall()
        context = [{**dict(r), "text": r["text"][-600:]} for r in reversed(previous)]
        self.db.execute(
            "INSERT OR IGNORE INTO evidence_context VALUES(?,?)", (row["id"], serialize(context))
        )
        text = row["text"][:24000]
        for start in range(0, len(text), 1220):
            end = min(start + 1400, len(text))
            self.db.execute(
                "INSERT OR IGNORE INTO evidence_chunks VALUES(?,?,?,?,?)",
                (f"{row['id']}:{start}", row["id"], start, end, text[start:end]),
            )
            if end == len(text):
                break

    def context(self, evidence_id):
        row = self.db.execute(
            "SELECT context FROM evidence_context WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        return json.loads(row[0]) if row else []

    def order(self, evidence_id):
        row = self.db.execute(
            "SELECT e.observed_at,t.rowid,CASE e.speaker WHEN 'user' THEN 0 "
            "WHEN 'host_observation' THEN 1 ELSE 2 END FROM evidence e "
            "JOIN turns t ON t.id=e.turn_id WHERE e.id=?",
            (evidence_id,),
        ).fetchone()
        if not row:
            raise ValueError("Unknown evidence order")
        return tuple(row)

    def representation(self, character_id, chunk_id, lane):
        row = self.db.execute(
            "SELECT c.*,e.speaker,e.observed_at FROM evidence_chunks c JOIN evidence e "
            "ON e.id=c.evidence_id WHERE c.id=? AND e.owner=? AND e.character_id=?",
            (chunk_id, self.engine.owner, character_id),
        ).fetchone()
        if row is None or lane not in ("raw", "note"):
            raise ValueError("Unknown contextual embedding source/lane")
        data = {
            "representation": "contextual-v1",
            "context": self.context(row["evidence_id"]),
            "excerpt": {
                "speaker": row["speaker"],
                "observed_at": datetime.fromtimestamp(row["observed_at"], timezone.utc).isoformat(),
                "text": row["raw"],
            },
        }
        if lane == "note":
            note = self.db.execute(
                "SELECT * FROM notes WHERE evidence_id=?", (row["evidence_id"],)
            ).fetchone()
            if not note:
                return None
            data.update(
                retrieval_context=note["description"],
                keywords=json.loads(note["keywords"]),
                revision=note["revision"],
            )
        return serialize(data)

    def candidates(self, character_id, model, limit=32):
        if not 1 <= limit <= 128:
            raise ValueError("limit must be 1..128")
        with self.engine._lock:
            self.engine._character(character_id)
            rows = self.db.execute(
                "SELECT c.id,c.evidence_id,lanes.lane FROM evidence_chunks c JOIN evidence e "
                "ON e.id=c.evidence_id CROSS JOIN (SELECT 'raw' AS lane UNION ALL SELECT 'note') "
                "lanes LEFT JOIN evidence_embeddings v ON v.chunk_id=c.id AND v.lane=lanes.lane "
                "AND v.model=? WHERE e.owner=? AND e.character_id=? AND v.chunk_id IS NULL "
                "AND (lanes.lane='raw' OR EXISTS(SELECT 1 FROM notes n "
                "WHERE n.evidence_id=e.id)) ORDER BY e.observed_at,c.start,lanes.lane DESC LIMIT ?",
                (model, self.engine.owner, character_id, limit),
            ).fetchall()
            return [
                {**dict(r), "text": self.representation(character_id, r["id"], r["lane"])}
                for r in rows
            ]

    def store(self, character_id, chunk_id, lane, model, vector, text):
        from .engine import Conflict

        values = checked_vector(vector)
        with self.engine._transaction():
            if self.representation(character_id, chunk_id, lane) != text:
                raise Conflict("Context/note changed while indexing; stale vector discarded")
            prior = self.db.execute(
                "SELECT vector FROM evidence_embeddings WHERE model=? LIMIT 1", (model,)
            ).fetchone()
            derived = self.db.execute(
                "SELECT length(vector)/4 FROM retrieval_vectors WHERE model=? LIMIT 1", (model,)
            ).fetchone()
            if (prior and len(json.loads(prior[0])) != len(values)) or (
                derived and derived[0] != len(values)
            ):
                raise ValueError("Embedding dimensions changed; use a new namespace")
            self.db.execute(
                "INSERT OR REPLACE INTO evidence_embeddings VALUES(?,?,?,?,?)",
                (
                    chunk_id,
                    lane,
                    model,
                    hashlib.sha256(text.encode()).hexdigest(),
                    serialize(values),
                ),
            )

    def invalidate_note(self, evidence_id):
        self.db.execute(
            "DELETE FROM evidence_embeddings WHERE lane='note' AND chunk_id IN "
            "(SELECT id FROM evidence_chunks WHERE evidence_id=?)",
            (evidence_id,),
        )

    def rankings(self, character_id, vector, model, lo, hi, before_id=None):
        q = checked_vector(vector)
        cutoff = self.order(before_id) if before_id else None
        ranks, hits = [], {}
        for lane, limit in (("raw", 32), ("note", 16)):
            heap = []
            for row in self.db.execute(
                "SELECT c.*,v.vector,e.rowid AS source_order FROM evidence_embeddings v "
                "JOIN evidence_chunks c ON c.id=v.chunk_id JOIN evidence e ON e.id=c.evidence_id "
                "WHERE v.model=? AND v.lane=? AND e.owner=? AND e.character_id=? "
                "AND date(e.observed_at,'unixepoch') BETWEEN ? AND ?",
                (model, lane, self.engine.owner, character_id, lo, hi),
            ):
                if cutoff and self.order(row["evidence_id"]) >= cutoff:
                    continue
                values = json.loads(row["vector"])
                if len(values) != len(q):
                    raise ValueError("Embedding dimensions changed; use a new namespace")
                score = sum(a * b for a, b in zip(values, q))
                if score > 0:
                    heapq.heappush(
                        heap, (score, row["id"], row["evidence_id"], row["start"], row["end"])
                    )
                    if len(heap) > limit:
                        heapq.heappop(heap)
            ids = []
            for score, _, eid, start, end in sorted(heap, reverse=True):
                if eid not in ids:
                    ids.append(eid)
                hit = {"start": start, "end": end, "cosine": score, "lane": lane}
                hits.setdefault(eid, []).append(hit)
            ranks.append(ids)
        return ranks, hits

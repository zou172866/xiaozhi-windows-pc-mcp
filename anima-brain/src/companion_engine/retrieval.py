"""Derived memory search alongside immutable evidence, with atomic local indexes.

SQLite triggers keep facts/documents searchable across every writer (including MCP
and host summaries). Vectors are optional, namespace-scoped binary float arrays.
Exact search remains linear; this is a local engine, not an ANN service.
"""

import hashlib
import heapq
import json
import math
import re
import time
from array import array
from datetime import datetime, timezone

from .memory import checked_vector, parse_namespace_dim


class LayeredMemory:
    def __init__(self, engine):
        self.engine = engine
        self.db = engine.db
        with engine._transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS retrieval_items (id TEXT PRIMARY KEY, owner TEXT, "
                "character_id TEXT, kind TEXT, text TEXT, "
                "FOREIGN KEY(owner,character_id) REFERENCES characters(owner,id) ON DELETE CASCADE)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS retrieval_scope ON retrieval_items(owner,character_id)"
            )
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_search USING fts5(id UNINDEXED,text)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS retrieval_vectors (id TEXT REFERENCES "
                "retrieval_items(id) ON DELETE CASCADE, model TEXT, digest TEXT, vector BLOB, "
                "PRIMARY KEY(id,model))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS retrieval_terms_state ("
                "id TEXT PRIMARY KEY REFERENCES retrieval_items(id) ON DELETE CASCADE, "
                "raw_text TEXT)"
            )
            for event, body in {
                "INSERT": "INSERT INTO retrieval_search VALUES(new.id,new.text);",
                "DELETE": "DELETE FROM retrieval_search WHERE id=old.id;",
                "UPDATE": "DELETE FROM retrieval_search WHERE id=old.id; "
                "INSERT INTO retrieval_search VALUES(new.id,new.text); "
                "DELETE FROM retrieval_vectors WHERE id=old.id AND old.text!=new.text;",
            }.items():
                self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS retrieval_{event.lower()} AFTER {event} "
                    f"ON retrieval_items BEGIN {body} END"
                )
            for table, prefix, kind, text in [
                ("memories", "fact:", "'fact'", "new.key || ' ' || new.text"),
                ("documents", "doc:", "new.kind", "new.text"),
            ]:
                for event in ("INSERT", "UPDATE"):
                    self.db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS retrieval_{table}_{event.lower()} "
                        f"AFTER {event} ON {table} BEGIN INSERT INTO retrieval_items "
                        f"VALUES('{prefix}'||new.id,new.owner,new.character_id,{kind},{text}) "
                        "ON CONFLICT(id) DO UPDATE SET text=excluded.text,kind=excluded.kind; END"
                    )
                self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS retrieval_{table}_delete AFTER DELETE "
                    f"ON {table} BEGIN DELETE FROM retrieval_items WHERE id='{prefix}'||old.id; END"
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO retrieval_items "
                    f"SELECT '{prefix}'||new.id,new.owner,new.character_id,{kind},{text} "
                    f"FROM {table} new"
                )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS memory_state_key ON memory_details"
                "(json_extract(data,'$.state_key'))"
            )
        with engine._lock:
            self._sync_tokenized_fts_locked()

    def _sync_tokenized_fts_locked(self) -> None:
        from .engine import terms

        stale = self.db.execute(
            "SELECT i.id, i.text FROM retrieval_items i "
            "LEFT JOIN retrieval_terms_state s ON s.id = i.id "
            "WHERE s.id IS NULL OR s.raw_text != i.text"
        ).fetchall()
        if not stale:
            return
        with self.engine._transaction():
            for rid, rtext in stale:
                raw = str(rtext or "")
                tok_str = " ".join(terms(raw))
                combined = f"{raw} {tok_str}".strip() if tok_str else raw
                self.db.execute("DELETE FROM retrieval_search WHERE id=?", (rid,))
                self.db.execute("INSERT INTO retrieval_search VALUES(?,?)", (rid, combined))
                self.db.execute(
                    "INSERT OR REPLACE INTO retrieval_terms_state VALUES(?,?)", (rid, raw)
                )

    def candidates(self, character_id, model, limit=32):
        with self.engine._lock:
            self.engine._character(character_id)
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT i.id,i.text FROM retrieval_items i LEFT JOIN retrieval_vectors v "
                    "ON v.id=i.id AND v.model=? WHERE i.owner=? AND i.character_id=? "
                    "AND v.id IS NULL ORDER BY i.id LIMIT ?",
                    (model, self.engine.owner, character_id, limit),
                )
            ]

    def store(self, character_id, item_id, model, vector, text):
        from .engine import Conflict

        values = checked_vector(vector, expected_dim=parse_namespace_dim(model))
        with self.engine._transaction():
            row = self.db.execute(
                "SELECT text FROM retrieval_items WHERE id=? AND owner=? AND character_id=?",
                (item_id, self.engine.owner, character_id),
            ).fetchone()
            if not row or row[0] != text:
                raise Conflict("Derived memory changed while indexing; stale vector discarded")
            prior = self.db.execute(
                "SELECT length(vector)/4 FROM retrieval_vectors WHERE model=? LIMIT 1", (model,)
            ).fetchone()
            original = self.db.execute(
                "SELECT vector FROM vectors WHERE model=? LIMIT 1", (model,)
            ).fetchone()
            if (prior and prior[0] != len(values)) or (
                original and len(json.loads(original[0])) != len(values)
            ):
                raise ValueError("Embedding dimensions changed; use a new namespace")
            self.db.execute(
                "INSERT OR REPLACE INTO retrieval_vectors VALUES(?,?,?,?)",
                (
                    item_id,
                    model,
                    hashlib.sha256(text.encode()).hexdigest(),
                    array("f", values).tobytes(),
                ),
            )

    def search(self, character_id, query, *, vector=None, model=None, limit=12):
        from .engine import terms

        if not 1 <= limit <= 30 or len(query) > 16000:
            raise ValueError("Invalid layered search bounds")
        with self.engine._lock:
            self.engine._character(character_id)
            self._sync_tokenized_fts_locked()
            ranks = []
            cosine = {}
            tokens = terms(query)[:48]
            if tokens:
                match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
                ranks.append(
                    [
                        r[0]
                        for r in self.db.execute(
                            "SELECT i.id FROM retrieval_search s "
                            "JOIN retrieval_items i ON i.id=s.id "
                            "WHERE retrieval_search MATCH ? AND i.owner=? AND i.character_id=? "
                            "ORDER BY bm25(retrieval_search) LIMIT 60",
                            (match, self.engine.owner, character_id),
                        )
                    ]
                )
                # Bounded CJK substring candidates ranked by matching bigram count.
                cjk = [t for t in tokens if 2 <= len(t) <= 8 and re.search(r"[\u3400-\u9fff]", t)][
                    :16
                ]
                if cjk:
                    clause = " OR ".join("instr(text,?)>0" for _ in cjk)
                    hits = " + ".join("(CASE WHEN instr(text,?)>0 THEN 1 ELSE 0 END)" for _ in cjk)
                    ranks.append(
                        [
                            r[0]
                            for r in self.db.execute(
                                f"SELECT id FROM retrieval_items WHERE owner=? AND character_id=? "
                                f"AND ({clause}) ORDER BY ({hits}) DESC, id ASC LIMIT 60",
                                (self.engine.owner, character_id, *cjk, *cjk),
                            )
                        ]
                    )
            if vector is not None:
                if not model:
                    raise ValueError("Embedding namespace required")
                q = checked_vector(vector, expected_dim=parse_namespace_dim(model))
                heap = []
                for row in self.db.execute(
                    "SELECT i.id,v.vector FROM retrieval_vectors v JOIN retrieval_items i "
                    "ON i.id=v.id WHERE i.owner=? AND i.character_id=? AND v.model=?",
                    (self.engine.owner, character_id, model),
                ):
                    values = array("f")
                    values.frombytes(row[1])
                    if len(values) != len(q):
                        raise ValueError("Embedding dimensions changed; use a new namespace")
                    score = sum(a * b for a, b in zip(values, q))
                    if any(row[0] in rank for rank in ranks):
                        cosine[row[0]] = score
                    if score > 0:
                        heapq.heappush(heap, (score, row[0]))
                        if len(heap) > 60:
                            heapq.heappop(heap)
                cosine.update({key: value for value, key in heap})
                ranks.append([key for _, key in sorted(heap, reverse=True)])
            scores = {}
            for rank in ranks:
                for position, key in enumerate(rank):
                    scores[key] = scores.get(key, 0) + 1 / (60 + position + 1)
            from .knowledge import eligible
            from .temporal import local_today

            ranked, metadata = {}, {}
            now = time.time()
            today = local_today(self.engine, character_id, now)
            max_rrf = max(scores.values(), default=1)
            for key, rrf in scores.items():
                if key.startswith("fact:"):
                    memory = self.engine._memory(self.engine._get_memory(character_id, key[5:]))
                    if not eligible(memory, now=now, today=today):
                        continue
                    d = memory.details
                    when = (
                        datetime.combine(
                            d.event_date, datetime.min.time(), timezone.utc
                        ).timestamp()
                        if d.event_date
                        else memory.lifecycle.last_reinforced_at or memory.updated_at
                    )
                    importance = d.importance
                else:
                    detail = self.engine.memory.document_metadata(key[4:])
                    when, importance = detail["covers_to"], detail["importance"]
                relevance = cosine.get(key, rrf / max_rrf)
                recency = math.exp(-max(0, now - when) / (45 * 86400))
                ranked[key] = 0.6 * relevance + 0.25 * recency + 0.15 * importance / 10
                metadata[key] = {
                    "relevance": relevance,
                    "relevance_basis": "cosine" if key in cosine else "lexical_rank_proxy",
                    "recency": recency,
                    "importance": importance,
                    "time_basis": when,
                    "score": ranked[key],
                }
            result = {
                "facts": [],
                "documents": [],
                "bounded": True,
                "ranking": "0.6*relevance + 0.25*exp(-age_days/45) + 0.15*importance/10",
                "scores": {},
            }
            for key in sorted(ranked, key=lambda k: (-ranked[k], k))[:limit]:
                result["scores"][key] = metadata[key]
                if key.startswith("fact:"):
                    item = self.engine._memory(self.engine._get_memory(character_id, key[5:]))
                    result["facts"].append(item.model_dump(mode="json"))
                else:
                    row = self.db.execute(
                        "SELECT * FROM documents WHERE id=?", (key[4:],)
                    ).fetchone()
                    result["documents"].append(
                        {
                            "id": row["id"],
                            "kind": row["kind"],
                            "text": row["text"],
                            "source_ids": json.loads(row["sources"]),
                            "caution": "Derived memory; inspect originals before "
                            "asserting history.",
                        }
                    )
            return result

    def review_update(self, character_id, draft):
        """Find candidate conflicts; never equate similarity/key equality with contradiction."""
        with self.engine._lock:
            found = self.search(character_id, draft.key + " " + draft.text)["facts"]
            if draft.details.state_key:
                rows = self.db.execute(
                    "SELECT m.* FROM memories m JOIN memory_details d ON d.memory_id=m.id "
                    "WHERE m.owner=? AND m.character_id=? AND json_extract(d.data,'$.state_key')=? "
                    "ORDER BY m.updated_at DESC LIMIT 40",
                    (self.engine.owner, character_id, draft.details.state_key),
                )
                found = [self.engine._memory(r).model_dump(mode="json") for r in rows] + found
            return {
                "candidates": list({f["id"]: f for f in found}.values()),
                "instruction": "Inspect same subject, attribute, time and original quote. "
                "Use corrected only for an explicit retraction; superseded for a formerly true "
                "state. Concurrent jobs, additive preferences and different events can coexist. "
                "Similarity is NOT proof. Use exact IDs/revisions in commit_turn corrections.",
            }

    def vector_spaces(self, character_id: str | None = None) -> list[dict]:
        """List coexisting multi-space embedding namespaces and vector counts."""
        spaces: dict[str, dict] = {}

        def _label_of(ns: str, dim: int | None) -> str:
            raw = ns.split(":", 1)[1] if ":" in ns else ns
            if "@" in raw:
                return raw
            return f"{raw}@{dim}" if dim else raw

        with self.engine._lock:
            if character_id is not None:
                ev_rows = self.db.execute(
                    "SELECT v.model, count(*), min(v.vector) FROM evidence_embeddings v "
                    "JOIN evidence_chunks c ON c.id=v.chunk_id "
                    "JOIN evidence e ON e.id=c.evidence_id "
                    "WHERE e.owner=? AND e.character_id=? GROUP BY v.model",
                    (self.engine.owner, character_id),
                ).fetchall()
                rv_rows = self.db.execute(
                    "SELECT v.model, count(*), min(length(v.vector)/4) FROM retrieval_vectors v "
                    "JOIN retrieval_items i ON i.id=v.id "
                    "WHERE i.owner=? AND i.character_id=? GROUP BY v.model",
                    (self.engine.owner, character_id),
                ).fetchall()
                lg_rows = self.db.execute(
                    "SELECT v.model, count(*), min(v.vector) FROM vectors v "
                    "JOIN evidence e ON e.id=v.evidence_id "
                    "WHERE e.owner=? AND e.character_id=? GROUP BY v.model",
                    (self.engine.owner, character_id),
                ).fetchall()
            else:
                ev_rows = self.db.execute(
                    "SELECT v.model, count(*), min(v.vector) FROM evidence_embeddings v "
                    "JOIN evidence_chunks c ON c.id=v.chunk_id "
                    "JOIN evidence e ON e.id=c.evidence_id "
                    "WHERE e.owner=? GROUP BY v.model",
                    (self.engine.owner,),
                ).fetchall()
                rv_rows = self.db.execute(
                    "SELECT v.model, count(*), min(length(v.vector)/4) FROM retrieval_vectors v "
                    "JOIN retrieval_items i ON i.id=v.id "
                    "WHERE i.owner=? GROUP BY v.model",
                    (self.engine.owner,),
                ).fetchall()
                lg_rows = self.db.execute(
                    "SELECT v.model, count(*), min(v.vector) FROM vectors v "
                    "JOIN evidence e ON e.id=v.evidence_id "
                    "WHERE e.owner=? GROUP BY v.model",
                    (self.engine.owner,),
                ).fetchall()

        for ns, cnt, sample_vec in ev_rows:
            dim = parse_namespace_dim(ns)
            if dim is None and sample_vec:
                try:
                    dim = len(json.loads(sample_vec))
                except Exception:
                    dim = None
            rec = spaces.setdefault(
                ns,
                {
                    "namespace": ns,
                    "label": _label_of(ns, dim),
                    "dim": dim,
                    "evidence_vectors": 0,
                    "retrieval_vectors": 0,
                    "legacy_vectors": 0,
                },
            )
            rec["evidence_vectors"] += int(cnt or 0)
            if rec["dim"] is None and dim is not None:
                rec["dim"] = dim
                rec["label"] = _label_of(ns, dim)

        for ns, cnt, sample_dim in rv_rows:
            dim = parse_namespace_dim(ns) or (int(sample_dim) if sample_dim else None)
            rec = spaces.setdefault(
                ns,
                {
                    "namespace": ns,
                    "label": _label_of(ns, dim),
                    "dim": dim,
                    "evidence_vectors": 0,
                    "retrieval_vectors": 0,
                    "legacy_vectors": 0,
                },
            )
            rec["retrieval_vectors"] += int(cnt or 0)
            if rec["dim"] is None and dim is not None:
                rec["dim"] = dim
                rec["label"] = _label_of(ns, dim)

        for ns, cnt, sample_vec in lg_rows:
            dim = parse_namespace_dim(ns)
            if dim is None and sample_vec:
                try:
                    dim = len(json.loads(sample_vec))
                except Exception:
                    dim = None
            rec = spaces.setdefault(
                ns,
                {
                    "namespace": ns,
                    "label": _label_of(ns, dim),
                    "dim": dim,
                    "evidence_vectors": 0,
                    "retrieval_vectors": 0,
                    "legacy_vectors": 0,
                },
            )
            rec["legacy_vectors"] += int(cnt or 0)
            if rec["dim"] is None and dim is not None:
                rec["dim"] = dim
                rec["label"] = _label_of(ns, dim)

        return sorted(
            spaces.values(),
            key=lambda s: (
                -(s["evidence_vectors"] + s["retrieval_vectors"] + s["legacy_vectors"]),
                s["namespace"],
            ),
        )

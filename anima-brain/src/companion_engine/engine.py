"""Local SQLite engine. All mutations are transactional and owner-scoped."""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from . import emotion
from .guidance import RESPONSE_GUIDANCE
from .models import (
    CharacterSpec,
    CommitRequest,
    CommitResult,
    Key,
    Memory,
    MemoryDraft,
    ObservedMedia,
    Text,
    TurnContext,
    Usage,
)


class NotFound(ValueError):
    """Missing or outside this engine's owner scope (intentionally indistinguishable)."""


class Conflict(ValueError):
    """An explicit retry, revision, or pending-turn conflict."""


SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE characters (
 owner TEXT NOT NULL, id TEXT NOT NULL, spec TEXT NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(owner,id)
);
CREATE TABLE turns (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, character_id TEXT NOT NULL,
 request_id TEXT NOT NULL, user_message TEXT NOT NULL, assistant_reply TEXT,
 status TEXT NOT NULL CHECK(status IN ('pending','committed','aborted')),
 created_at REAL NOT NULL, commit_hash TEXT, result TEXT,
 UNIQUE(owner,character_id,request_id),
 FOREIGN KEY(owner,character_id) REFERENCES characters(owner,id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX one_pending ON turns(owner,character_id) WHERE status='pending';
CREATE INDEX turn_history ON turns(owner,character_id,status,created_at);
CREATE TABLE memories (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, character_id TEXT NOT NULL,
 key TEXT NOT NULL, text TEXT NOT NULL, category TEXT NOT NULL, source_quote TEXT NOT NULL,
 source_turn_id TEXT, origin TEXT NOT NULL, revision INTEGER NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(owner,character_id,key),
 FOREIGN KEY(owner,character_id) REFERENCES characters(owner,id) ON DELETE CASCADE,
 FOREIGN KEY(source_turn_id) REFERENCES turns(id) ON DELETE SET NULL
);
CREATE TABLE revisions (
 memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, previous TEXT NOT NULL, changed_at REAL NOT NULL,
 PRIMARY KEY(memory_id,revision)
);
CREATE VIRTUAL TABLE memory_search USING fts5(memory_id UNINDEXED, terms);
CREATE TRIGGER memory_deleted AFTER DELETE ON memories BEGIN
 DELETE FROM memory_search WHERE memory_id=old.id;
END;
CREATE TABLE provider_usage (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, character_id TEXT NOT NULL, turn_id TEXT NOT NULL,
 model TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
 estimated_usd REAL, status TEXT NOT NULL, created_at REAL NOT NULL,
 FOREIGN KEY(owner,character_id) REFERENCES characters(owner,id) ON DELETE CASCADE,
 FOREIGN KEY(turn_id) REFERENCES turns(id) ON DELETE CASCADE
);
PRAGMA user_version=1;
COMMIT;
"""


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def terms(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    stop = {"a", "an", "the", "i", "my", "me", "is", "are", "was", "do", "you", "what", "and", "to"}
    result = [word for word in re.findall(r"[^\W_]+", normalized) if word not in stop]
    # Explicit lexical CJK support; no claim of semantic/embedding retrieval.
    for sequence in re.findall(r"[\u3400-\u9fff\u3040-\u30ff]+", normalized):
        result.extend(sequence)
        result.extend(sequence[i : i + 2] for i in range(len(sequence) - 1))
    return sorted(set(result))


class Engine:
    """One trusted local owner per instance. Not a multi-tenant network auth layer."""

    def __init__(
        self,
        database: str | Path,
        *,
        owner: str = "local",
        context_chars: int = 48000,
        recent_turns: int = 6,
        memory_config=None,
        recent_chars: int = 12000,
    ):
        self.owner = TypeAdapter(Key).validate_python(owner)
        if not 4000 <= context_chars <= 200000:
            raise ValueError("context_chars must be between 4000 and 200000")
        self.context_chars = context_chars
        if (
            isinstance(recent_turns, bool)
            or not isinstance(recent_turns, int)
            or not 0 <= recent_turns <= 100
        ):
            raise ValueError("recent_turns must be an integer between 0 and 100")
        self.recent_turns = recent_turns
        from .sdk_models import MemoryConfig

        self.memory_config = (
            MemoryConfig.model_validate(memory_config).model_copy(deep=True)
            if memory_config is not None
            else None
        )
        if isinstance(recent_chars, bool) or not isinstance(recent_chars, int) or recent_chars < 0:
            raise ValueError("recent_chars must be a nonnegative integer")
        self.recent_chars = recent_chars
        self._lock = threading.RLock()
        path = Path(database).expanduser().resolve()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.database = path
        self.db = sqlite3.connect(path, timeout=15, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        # SQLite's journal-mode transition can return SQLITE_BUSY immediately even
        # with busy_timeout when multiple processes open a new file together.
        deadline = time.monotonic() + 15
        while True:
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if (
                    code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                    or time.monotonic() >= deadline
                ):
                    self.db.close()
                    raise
                time.sleep(0.025)
        # Serializes schema initialization across concurrently launched local processes.
        self.db.execute("BEGIN IMMEDIATE")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self.db.rollback()
            self.db.close()
            raise ValueError(f"Unsupported database schema {version}; no changes made")
        if version == 0:
            # Unlike executescript, individual executes preserve the initialization lock.
            statement = ""
            try:
                for line in (
                    SCHEMA.replace("BEGIN IMMEDIATE;", "").replace("COMMIT;", "").splitlines()
                ):
                    statement += line + "\n"
                    if sqlite3.complete_statement(statement):
                        self.db.execute(statement)
                        statement = ""
                self.db.commit()
            except BaseException:
                self.db.rollback()
                self.db.close()
                raise
        else:
            self.db.commit()
        from .memory import SCHEMA as MEMORY_SCHEMA
        from .memory import MemorySystem

        with self._transaction():
            for statement in MEMORY_SCHEMA:
                self.db.execute(statement)
            self.db.execute("PRAGMA user_version=2")
        self.memory = MemorySystem(self)
        from .evidence_index import EvidenceIndex
        from .knowledge import Knowledge

        self.evidence_index = EvidenceIndex(self)
        self.knowledge = Knowledge(self)
        # Existing v1 conversations remain first-class originals after migration.
        # This is local indexing only: no extraction, model call or changed transcript.
        with self._transaction():
            for row in self.db.execute(
                "SELECT t.* FROM turns t WHERE owner=? AND status='committed' "
                "AND NOT EXISTS (SELECT 1 FROM evidence e WHERE e.turn_id=t.id)",
                (self.owner,),
            ).fetchall():
                self.memory.record_exchange(
                    row["character_id"],
                    row["id"],
                    row["user_message"],
                    row["assistant_reply"],
                    row["created_at"],
                )

        from .continuity import Continuity
        from .retrieval import LayeredMemory

        self.retrieval = LayeredMemory(self)
        self.continuity = Continuity(self)

    def close(self):
        with self._lock:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            # Compose existing operations atomically (e.g. character + automation policy).
            savepoint = "nested_" + uuid4().hex if self.db.in_transaction else None
            self.db.execute(f"SAVEPOINT {savepoint}" if savepoint else "BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                if savepoint:
                    self.db.execute(f"ROLLBACK TO {savepoint}")
                    self.db.execute(f"RELEASE {savepoint}")
                else:
                    self.db.rollback()
                raise
            else:
                if savepoint:
                    self.db.execute(f"RELEASE {savepoint}")
                else:
                    self.db.commit()

    def _character(self, character_id: str):
        row = self.db.execute(
            "SELECT * FROM characters WHERE owner=? AND id=?", (self.owner, character_id)
        ).fetchone()
        if row is None:
            raise NotFound("Character not found")
        return row

    def create_character(self, spec: CharacterSpec) -> CharacterSpec:
        spec = CharacterSpec.model_validate(spec)
        with self._transaction():
            try:
                self.db.execute(
                    "INSERT INTO characters VALUES(?,?,?,?)",
                    (
                        self.owner,
                        spec.id,
                        spec.model_dump_json(),
                        encoded(emotion.initial(time.time())),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("Character already exists; use update_character") from exc
        return spec

    def update_character(self, spec: CharacterSpec) -> CharacterSpec:
        spec = CharacterSpec.model_validate(spec)
        with self._transaction():
            self._character(spec.id)
            self.db.execute(
                "UPDATE characters SET spec=? WHERE owner=? AND id=?",
                (spec.model_dump_json(), self.owner, spec.id),
            )
        return spec

    def characters(self) -> list[CharacterSpec]:
        with self._lock:
            return [
                CharacterSpec.model_validate_json(r[0])
                for r in self.db.execute(
                    "SELECT spec FROM characters WHERE owner=? ORDER BY id", (self.owner,)
                )
            ]

    def _memory(self, row) -> Memory:
        data = {k: row[k] for k in Memory.model_fields if k not in ("details", "lifecycle")}
        details = self.db.execute(
            "SELECT data FROM memory_details WHERE memory_id=?", (row["id"],)
        ).fetchone()
        if details:
            data["details"] = json.loads(details[0])
        lifecycle = self.db.execute(
            "SELECT data FROM fact_lifecycle WHERE memory_id=?", (row["id"],)
        ).fetchone()
        if lifecycle:
            data["lifecycle"] = json.loads(lifecycle[0])
        return Memory.model_validate(data)

    def _get_memory(self, character_id: str, memory_id: str):
        row = self.db.execute(
            "SELECT * FROM memories WHERE owner=? AND character_id=? AND id=?",
            (self.owner, character_id, memory_id),
        ).fetchone()
        if row is None:
            raise NotFound("Memory not found")
        return row

    def memories(self, character_id: str, *, limit: int = 100, offset: int = 0) -> list[Memory]:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("limit must be 1..200 and offset >= 0")
        with self._lock:
            self._character(character_id)
            return [
                self._memory(r)
                for r in self.db.execute(
                    "SELECT * FROM memories WHERE owner=? AND character_id=? "
                    "ORDER BY id LIMIT ? OFFSET ?",
                    (self.owner, character_id, limit, offset),
                )
            ]

    def search(self, character_id: str, query: str, *, limit: int = 8) -> list[Memory]:
        if not 1 <= limit <= 50 or len(query) > 16000:
            raise ValueError("limit must be 1..50; query must be at most 16000 characters")
        tokens = terms(query)[:64]
        with self._lock:
            self._character(character_id)
            if not tokens:
                return []
            match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            return [
                self._memory(r)
                for r in self.db.execute(
                    "SELECT m.* FROM memory_search s JOIN memories m ON m.id=s.memory_id "
                    "WHERE memory_search MATCH ? AND m.owner=? AND m.character_id=? "
                    "ORDER BY bm25(memory_search),m.updated_at DESC,m.id LIMIT ?",
                    (match, self.owner, character_id, limit),
                )
            ]

    def _index(self, memory_id: str, text: str, key: str):
        self.db.execute("DELETE FROM memory_search WHERE memory_id=?", (memory_id,))
        self.db.execute(
            "INSERT INTO memory_search VALUES(?,?)", (memory_id, " ".join(terms(text + " " + key)))
        )

    def _insert_memory(self, character_id: str, draft: MemoryDraft, turn_id: str | None) -> Memory:
        existing = self.db.execute(
            "SELECT * FROM memories WHERE owner=? AND character_id=? AND key=?",
            (self.owner, character_id, draft.key),
        ).fetchone()
        if existing:
            current = self._memory(existing)
            if (
                current.text == draft.text
                and current.category == draft.category
                and current.details == draft.details
            ):
                self.knowledge.reinforce(current.id, turn_id)
                return self._memory(self._get_memory(character_id, current.id))
            raise Conflict(
                f"Memory key {draft.key!r} exists; explicitly correct it with expected_revision"
            )
        memory_id = str(uuid4())
        self.db.execute(
            "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id,
                self.owner,
                character_id,
                draft.key,
                draft.text,
                draft.category,
                draft.source_quote,
                turn_id,
                "host_observation"
                if draft.source_media_ref
                else "user_assertion"
                if turn_id
                else "manual",
                1,
                time.time(),
            ),
        )
        self._index(memory_id, draft.text, draft.key)
        self.db.execute(
            "INSERT INTO memory_details VALUES(?,?)", (memory_id, draft.details.model_dump_json())
        )
        self.knowledge.initialize(memory_id, turn_id, time.time())
        return self._memory(self._get_memory(character_id, memory_id))

    def remember(self, character_id: str, draft: MemoryDraft) -> Memory:
        """Explicit human-approved/manual assertion. Not an automatically verified fact."""
        draft = MemoryDraft.model_validate(draft)
        if draft.source_media_ref:
            raise ValueError("Media observations must be registered and committed with their turn")
        with self._transaction():
            self._character(character_id)
            return self._insert_memory(character_id, draft, None)

    def correct_memory(
        self, character_id: str, memory_id: str, draft: MemoryDraft, *, expected_revision: int
    ) -> Memory:
        """Explicit correction; history retained until delete_memory/forget_character."""
        draft = MemoryDraft.model_validate(draft)
        if draft.source_media_ref:
            raise ValueError("Media corrections must use commit_turn with registered observations")
        with self._transaction():
            return self._correct_memory(character_id, memory_id, draft, expected_revision)

    def _correct_memory(
        self, character_id, memory_id, draft, expected_revision, kind="corrected", turn_id=None
    ):
        row = self._get_memory(character_id, memory_id)
        if row["revision"] != expected_revision:
            raise Conflict("Memory changed since inspection; read it again before correcting")
        if draft.key != row["key"]:
            raise ValueError("Correction must retain the memory key")
        self.db.execute(
            "INSERT INTO revisions VALUES(?,?,?,?)",
            (
                memory_id,
                row["revision"],
                encoded(
                    {
                        **dict(row),
                        "details": self._memory(row).details.model_dump(mode="json"),
                        "lifecycle": self._memory(row).lifecycle.model_dump(mode="json"),
                        "invalidation": kind,
                        "valid_until": str(draft.details.valid_from)
                        if draft.details.valid_from
                        else None,
                    }
                ),
                time.time(),
            ),
        )
        self.db.execute(
            "UPDATE memories SET text=?,category=?,source_quote=?,"
            "source_turn_id=NULL,origin='manual',"
            "revision=revision+1,updated_at=? WHERE id=? AND owner=? AND character_id=?",
            (
                draft.text,
                draft.category,
                draft.source_quote,
                time.time(),
                memory_id,
                self.owner,
                character_id,
            ),
        )
        self._index(memory_id, draft.text, draft.key)
        self.db.execute(
            "INSERT OR REPLACE INTO memory_details VALUES(?,?)",
            (memory_id, draft.details.model_dump_json()),
        )
        if turn_id:
            self.db.execute(
                "UPDATE memories SET source_turn_id=?,origin=? WHERE id=?",
                (
                    turn_id,
                    "host_observation" if draft.source_media_ref else "user_assertion",
                    memory_id,
                ),
            )
        # A cached derived core must not re-introduce a corrected claim.
        self._knowledge_update(character_id, row, kind)
        self._invalidate_documents(character_id, row["source_turn_id"])
        self.knowledge.initialize(memory_id, turn_id, time.time())
        return self._memory(self._get_memory(character_id, memory_id))

    def _knowledge_update(self, character_id, old_row, operation):
        # Retain the invalidation marker, not deleted fact content; original chats remain.
        self.db.execute(
            "INSERT INTO knowledge_updates VALUES(?,?,?,?,?,?,?,0)",
            (
                str(uuid4()),
                self.owner,
                character_id,
                old_row["key"],
                operation,
                old_row["source_turn_id"]
                + (":media" if old_row["origin"] == "host_observation" else ":user")
                if old_row["source_turn_id"]
                else None,
                time.time(),
            ),
        )

    def delete_memory(self, character_id: str, memory_id: str, *, expected_revision: int):
        with self._transaction():
            row = self._get_memory(character_id, memory_id)
            if row["revision"] != expected_revision:
                raise Conflict("Memory changed since inspection")
            self._knowledge_update(character_id, row, "deleted")
            self.db.execute(
                "DELETE FROM memories WHERE id=? AND owner=? AND character_id=?",
                (memory_id, self.owner, character_id),
            )
            self._invalidate_documents(character_id, row["source_turn_id"])

    def _invalidate_documents(self, character_id, source_turn_id):
        # Rebuild current core; invalidate dependent episodes, preserving unrelated history.
        if source_turn_id:
            self.db.execute(
                "DELETE FROM documents WHERE owner=? AND character_id=? AND "
                "(kind='core' OR EXISTS(SELECT 1 FROM json_each(documents.sources) "
                "WHERE value=? OR value=? OR value=?))",
                (
                    self.owner,
                    character_id,
                    source_turn_id + ":user",
                    source_turn_id + ":assistant",
                    source_turn_id + ":media",
                ),
            )
        else:
            self.db.execute(
                "DELETE FROM documents WHERE owner=? AND character_id=?", (self.owner, character_id)
            )

    def prepare_turn(
        self,
        character_id: str,
        user_message: str,
        *,
        request_id: str,
        media_observations: list[ObservedMedia] | None = None,
    ) -> TurnContext:
        user_message = TypeAdapter(Text).validate_python(user_message)
        request_id = TypeAdapter(Key).validate_python(request_id)
        media = [ObservedMedia.model_validate(m) for m in (media_observations or [])]
        if len(media) > 4 or len({m.source_ref for m in media}) != len(media):
            raise ValueError("At most four unique media observations per turn")
        media_json = encoded([m.model_dump(mode="json") for m in media])
        with self._transaction():
            row = self._character(character_id)
            existing = self.db.execute(
                "SELECT * FROM turns WHERE owner=? AND character_id=? AND request_id=?",
                (self.owner, character_id, request_id),
            ).fetchone()
            if existing:
                if existing["user_message"] != user_message or existing["status"] != "pending":
                    raise Conflict(
                        "Request ID already used with different content or completed/aborted turn"
                    )
                turn_id = existing["id"]
                stored_media = self.db.execute(
                    "SELECT data FROM turn_media WHERE turn_id=?", (turn_id,)
                ).fetchone()
                if (stored_media[0] if stored_media else "[]") != media_json:
                    raise Conflict("Turn retry contains different media observations")
            else:
                turn_id = str(uuid4())
                try:
                    self.db.execute(
                        "INSERT INTO turns(id,owner,character_id,request_id,"
                        "user_message,status,created_at) "
                        "VALUES(?,?,?,?,?,'pending',?)",
                        (turn_id, self.owner, character_id, request_id, user_message, time.time()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict("Another turn is pending; commit or abort it first") from exc
            self.db.execute("INSERT OR IGNORE INTO turn_media VALUES(?,?)", (turn_id, media_json))
            spec = CharacterSpec.model_validate_json(row["spec"])
            # Seed daily character weather exactly once in the persisted timeline.
            state = emotion.decayed(
                json.loads(row["state"]), time.time(), self.owner + ":" + character_id
            )
            if spec.relationship_enabled:
                self.db.execute(
                    "UPDATE characters SET state=? WHERE owner=? AND id=?",
                    (encoded(state), self.owner, character_id),
                )
            from .temporal import calendar, local_today

            context = TurnContext(
                turn_id=turn_id,
                character=spec,
                user_message=user_message,
                recent_turns=[],
                memories=[],
                relationship_note=emotion.note(
                    state, time.time(), romance_enabled=spec.romance_enabled
                )
                if spec.relationship_enabled
                else None,
                response_guidance=RESPONSE_GUIDANCE,
                media_observations=media,
                current_date=local_today(self, character_id).isoformat(),
            )

            def fits():
                # Reserve a few bytes for the final omission counters.
                return len(context.model_dump_json()) <= self.context_chars - 64

            if not fits():
                raise ValueError(
                    "Persona and current message exceed context budget; increase context_chars"
                )
            read_memory = self.memory_config is None or (
                self.memory_config.read_enabled and self.memory_config.context_chars > 0
            )
            if read_memory:
                context.reaction_feedback = self.continuity.feedback(
                    character_id, turn_id, user_message
                )
                context.response_style = self.continuity.style(character_id)
                context.calendar = calendar(self, character_id)
                if not fits():
                    context.response_style = {
                        "instruction": "Read inspect_response_style if needed."
                    }
                    context.calendar = []
                if not fits():
                    context.reaction_feedback = {}
            if spec.relationship_enabled:
                context.character_state = state
                if not fits():
                    context.character_state = None
            candidates = []
            if read_memory:
                analyses = self.db.execute(
                    "SELECT t.id,t.created_at,a.data FROM turns t JOIN turn_analysis a "
                    "ON a.turn_id=t.id WHERE t.owner=? AND t.character_id=? "
                    "AND t.status='committed' "
                    "AND t.id IN (SELECT id FROM turns WHERE owner=? AND character_id=? "
                    "AND status='committed' ORDER BY created_at DESC,rowid DESC LIMIT 6) "
                    "ORDER BY t.created_at DESC LIMIT 6",
                    (self.owner, character_id, self.owner, character_id),
                ).fetchall()
                for analysis in analyses:
                    context.recent_attunement.append(
                        {
                            "turn_id": analysis["id"],
                            "observed_at": analysis["created_at"],
                            "interpretation": json.loads(analysis["data"]),
                            "subsequent_feedback": [
                                dict(r)
                                for r in self.db.execute(
                                    "SELECT label,quote FROM reaction_links "
                                    "WHERE previous_turn_id=? "
                                    "ORDER BY rowid DESC LIMIT 4",
                                    (analysis["id"],),
                                )
                            ],
                            "caution": "Fallible turn-specific interpretation, not a lasting fact. "
                            "Current statements and corrections take priority.",
                        }
                    )
                    if not fits():
                        context.recent_attunement.pop()
                # Explicit boundaries precede matching facts. Never revive archived revisions.
                pinned = [
                    self._memory(r)
                    for r in self.db.execute(
                        "SELECT * FROM memories WHERE owner=? AND character_id=? "
                        "AND category IN ('boundary','style') "
                        "ORDER BY updated_at DESC,id LIMIT 12",
                        (self.owner, character_id),
                    )
                ]
                corrected = [
                    self._memory(r)
                    for r in self.db.execute(
                        "SELECT * FROM memories WHERE owner=? AND character_id=? AND revision>1 "
                        "ORDER BY updated_at DESC,id LIMIT 8",
                        (self.owner, character_id),
                    )
                ]
                layers = self.retrieval.search(
                    character_id,
                    user_message,
                    limit=max(1, self.memory_config.fact_limit, self.memory_config.document_limit)
                    if self.memory_config
                    else 12,
                )
                candidates = list(
                    {
                        m.id: m
                        for m in pinned
                        + corrected
                        + [Memory.model_validate(m) for m in layers["facts"]]
                        + self.search(
                            character_id,
                            user_message,
                            limit=max(1, self.memory_config.fact_limit)
                            if self.memory_config
                            else 8,
                        )
                    }.values()
                )
                from .knowledge import eligible

                today = local_today(self, character_id)
                for memory in candidates:
                    if not eligible(memory, today=today):
                        continue
                    context.memories.append(memory)
                    if not fits():
                        context.memories.pop()
                for field, values in [
                    (
                        "core_memory",
                        list(
                            {
                                d["id"]: d
                                for d in [
                                    d
                                    for d in self.memory.documents(character_id)
                                    if d["kind"] != "episode"
                                ]
                                + layers["documents"]
                                + self.memory.documents(character_id)
                            }.values()
                        ),
                    ),
                    ("open_loops", self.memory.loops(character_id)),
                    (
                        "evidence",
                        self.memory.recall(
                            character_id,
                            user_message,
                            limit=max(1, self.memory_config.evidence_limit)
                            if self.memory_config
                            else 8,
                        )["sources"],
                    ),
                ]:
                    items = getattr(context, field)
                    for value in values:
                        items.append(value)
                        if not fits():
                            items.pop()
            from .context_policy import enforce_context_policy

            enforce_context_policy(self, context)
            rows = self.db.execute(
                "SELECT user_message,assistant_reply FROM turns WHERE owner=? AND character_id=? "
                "AND status='committed' ORDER BY created_at DESC,rowid DESC LIMIT ?",
                (self.owner, character_id, self.recent_turns),
            ).fetchall()
            for recent in rows:
                context.recent_turns.insert(0, dict(recent))
                if not fits():
                    context.recent_turns.pop(0)
                    break
            context.omitted_recent_turns = len(rows) - len(context.recent_turns)
            context.omitted_memories = len(candidates) - len(context.memories)
            return enforce_context_policy(self, context)

    def pending_turn(self, character_id: str) -> dict | None:
        with self._lock:
            self._character(character_id)
            row = self.db.execute(
                "SELECT id,request_id,user_message FROM turns WHERE owner=? "
                "AND character_id=? AND status='pending'",
                (self.owner, character_id),
            ).fetchone()
            return dict(row) if row else None

    def attach_media(self, character_id: str, turn_id: str, observations: list[ObservedMedia]):
        """Register perception once on an already reserved turn, before reply generation."""
        media = [ObservedMedia.model_validate(m) for m in observations]
        if not media or len(media) > 4 or len({m.source_ref for m in media}) != len(media):
            raise ValueError("Supply 1..4 distinct media observations")
        payload = encoded([m.model_dump(mode="json") for m in media])
        with self._transaction():
            turn = self._turn(character_id, turn_id)
            if turn["status"] != "pending":
                raise Conflict("Media can only be attached to a pending turn")
            stored = self.db.execute(
                "SELECT data FROM turn_media WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if (stored and stored[0] not in ("[]", payload)) or self.db.execute(
                "SELECT 1 FROM turn_drafts WHERE turn_id=?", (turn_id,)
            ).fetchone():
                raise Conflict("Cannot replace observations or alter a generated draft")
            self.db.execute("INSERT OR REPLACE INTO turn_media VALUES(?,?)", (turn_id, payload))
        return media

    def _turn(self, character_id: str, turn_id: str):
        row = self.db.execute(
            "SELECT * FROM turns WHERE owner=? AND character_id=? AND id=?",
            (self.owner, character_id, turn_id),
        ).fetchone()
        if row is None:
            raise NotFound("Turn not found")
        return row

    def commit_turn(self, character_id: str, turn_id: str, request: CommitRequest) -> CommitResult:
        request = CommitRequest.model_validate(request)
        digest = hashlib.sha256(encoded(request.model_dump(mode="json")).encode()).hexdigest()
        with self._transaction():
            turn = self._turn(character_id, turn_id)
            if turn["status"] == "committed":
                if turn["commit_hash"] != digest:
                    raise Conflict("Turn committed with different content")
                return CommitResult.model_validate_json(turn["result"]).model_copy(
                    update={"replayed": True}
                )
            if turn["status"] != "pending":
                raise Conflict("Turn was aborted")
            media = {
                m["source_ref"]: m
                for r in self.db.execute("SELECT data FROM turn_media WHERE turn_id=?", (turn_id,))
                for m in json.loads(r[0])
            }
            for draft in [*request.memories, *[c.draft for c in request.corrections]]:
                if draft.details.correction_evidence and (
                    draft.details.correction_evidence not in draft.source_quote
                ):
                    raise ValueError("correction_evidence must be an exact source_quote excerpt")
            for proposal in [
                *request.memories,
                *[c.draft for c in request.corrections],
                *([request.appraisal] if request.appraisal else []),
                *([request.attunement] if request.attunement else []),
            ]:
                media_ref = getattr(proposal, "source_media_ref", None)
                if media_ref:
                    source = media.get(media_ref)
                    if (
                        not source
                        or proposal.source_quote not in source["observation"]
                        or proposal.details.confidence > source["confidence"]
                    ):
                        raise ValueError(
                            "Visual facts need registered host evidence and uncertainty"
                        )
                    continue
                if proposal.source_quote not in turn["user_message"]:
                    raise ValueError(
                        "source_quote must be an exact quote from this turn's user message"
                    )
            if len({m.key for m in request.memories}) != len(request.memories):
                raise ValueError("Duplicate memory keys in one commit")
            memory_ids = [
                self._insert_memory(character_id, draft, turn_id).id for draft in request.memories
            ]
            row = self._character(character_id)
            spec = CharacterSpec.model_validate_json(row["spec"])
            if spec.memory_policy == "off" and (request.memories or request.corrections):
                raise ValueError("Long-term memory is disabled")
            if len({c.memory_id for c in request.corrections}) != len(request.corrections):
                raise ValueError("Duplicate correction")
            for change in request.corrections:
                memory_ids.append(
                    self._correct_memory(
                        character_id,
                        change.memory_id,
                        change.draft,
                        change.expected_revision,
                        change.kind,
                        turn_id,
                    ).id
                )
            self.db.execute(
                "INSERT INTO turn_continuity VALUES(?,?)", (turn_id, int(spec.relationship_enabled))
            )
            if request.appraisal:
                if not spec.relationship_enabled:
                    raise ValueError("Relationship simulation is not enabled for this character")
                if request.appraisal.romance and not spec.romance_enabled:
                    raise ValueError("Romance is not enabled for this character")
                state = emotion.advance(
                    json.loads(row["state"]), request.appraisal, time.time(), spec.romance_enabled
                )
                self.db.execute(
                    "UPDATE characters SET state=? WHERE owner=? AND id=?",
                    (encoded(state), self.owner, character_id),
                )
                self.db.execute(
                    "INSERT INTO emotion_transitions VALUES(?,?,?)",
                    (turn_id, row["state"], encoded(state)),
                )
                self.db.execute(
                    "INSERT INTO turn_appraisals VALUES(?,?)",
                    (turn_id, request.appraisal.model_dump_json()),
                )
            if request.attunement:
                self.db.execute(
                    "INSERT INTO turn_analysis VALUES(?,?)",
                    (turn_id, request.attunement.model_dump_json()),
                )
            self.memory.record_exchange(
                character_id,
                turn_id,
                turn["user_message"],
                request.assistant_reply,
                turn["created_at"],
            )
            result = CommitResult(turn_id=turn_id, memory_ids=memory_ids)
            self.db.execute(
                "UPDATE turns SET status='committed',assistant_reply=?,commit_hash=?,result=? "
                "WHERE owner=? AND character_id=? AND id=?",
                (
                    request.assistant_reply,
                    digest,
                    result.model_dump_json(),
                    self.owner,
                    character_id,
                    turn_id,
                ),
            )
            return result

    def history(self, character_id, *, limit=50, before=None):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        with self._lock:
            self._character(character_id)
            rows = self.db.execute(
                "SELECT rowid AS "
                "cursor,id,user_message,assistant_reply,created_at,status FROM turns "
                "WHERE owner=? AND character_id=? AND status='committed' AND rowid<? "
                "ORDER BY rowid DESC LIMIT ?",
                (self.owner, character_id, before or 9223372036854775807, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def relationship(self, character_id):
        with self._lock:
            row = self._character(character_id)
            spec = CharacterSpec.model_validate_json(row["spec"])
            state = emotion.decayed(json.loads(row["state"]), time.time())
            return {
                "enabled": spec.relationship_enabled,
                "state": state,
                "note": emotion.note(state, time.time(), romance_enabled=spec.romance_enabled)
                if spec.relationship_enabled
                else None,
                "caution": "Fictional character state, not a psychological assessment.",
            }

    def tick(self, character_id, *, now=None):
        with self._transaction():
            row = self._character(character_id)
            state = emotion.decayed(
                json.loads(row["state"]), now or time.time(), self.owner + ":" + character_id
            )
            self.db.execute(
                "UPDATE characters SET state=? WHERE owner=? AND id=?",
                (encoded(state), self.owner, character_id),
            )
            return state

    def abort_turn(self, character_id: str, turn_id: str):
        with self._transaction():
            turn = self._turn(character_id, turn_id)
            if turn["status"] == "committed":
                raise Conflict("Cannot abort a committed turn")
            self.db.execute("UPDATE turns SET status='aborted' WHERE id=?", (turn_id,))

    def export_character(self, character_id: str) -> dict:
        """A consistent portable JSON snapshot including revision history and transcripts."""
        with self._transaction():
            character = self._character(character_id)
            data = dict(
                format="companion-engine-export",
                version=1,
                exported_at=time.time(),
                character=json.loads(character["spec"]),
                state=json.loads(character["state"]),
            )
            for table in (
                "turns",
                "memories",
                "provider_usage",
                "evidence",
                "documents",
                "knowledge_updates",
            ):
                data[table] = [
                    dict(r)
                    for r in self.db.execute(
                        f"SELECT * FROM {table} WHERE owner=? AND character_id=? ORDER BY rowid",
                        (self.owner, character_id),
                    )
                ]
            data["revisions"] = [
                dict(r)
                for r in self.db.execute(
                    "SELECT r.* FROM revisions r JOIN memories m ON m.id=r.memory_id "
                    "WHERE m.owner=? AND m.character_id=? ORDER BY r.memory_id,r.revision",
                    (self.owner, character_id),
                )
            ]
            data["memory_details"] = [
                self._memory(r).model_dump(mode="json")
                for r in self.db.execute(
                    "SELECT * FROM memories WHERE owner=? AND character_id=? ORDER BY id",
                    (self.owner, character_id),
                )
            ]
            data["version"] = 2
            for table in (
                "turn_analysis",
                "turn_appraisals",
                "turn_continuity",
                "reaction_links",
                "emotion_transitions",
                "turn_media",
            ):
                data[table] = [
                    dict(r)
                    for r in self.db.execute(
                        f"SELECT a.* FROM {table} a JOIN turns t ON t.id=a.turn_id "
                        "WHERE t.owner=? AND t.character_id=?",
                        (self.owner, character_id),
                    )
                ]
            data["response_style"] = self.continuity.style(character_id)
            data["notebooks"] = [
                dict(r)
                for r in self.db.execute(
                    "SELECT n.* FROM notebooks n JOIN documents d ON d.id=n.document_id "
                    "WHERE d.owner=? AND d.character_id=?",
                    (self.owner, character_id),
                )
            ]
            data["notes"] = [
                dict(r)
                for r in self.db.execute(
                    "SELECT n.* FROM notes n JOIN evidence e ON e.id=n.evidence_id WHERE "
                    "e.owner=? AND e.character_id=?",
                    (self.owner, character_id),
                )
            ]
            data["links"] = [
                dict(r)
                for r in self.db.execute(
                    "SELECT l.* FROM evidence_links l JOIN evidence e ON "
                    "e.id=l.source_id WHERE e.owner=? AND e.character_id=?",
                    (self.owner, character_id),
                )
            ]
            return data

    def record_usage(
        self,
        character_id: str,
        turn_id: str,
        model: str,
        usage: Usage | None,
        *,
        status: str = "reported",
    ):
        """Append an API attempt; missing usage is unknown, never inferred as free."""
        if status not in ("reported", "unknown", "failed_unknown"):
            raise ValueError("Invalid usage status")
        if (usage is None) != (status != "reported"):
            raise ValueError("Reported usage requires token counts; unknown usage must be null")
        if usage is not None:
            usage = Usage.model_validate(usage)
        with self._transaction():
            self._turn(character_id, turn_id)
            self.db.execute(
                "INSERT INTO provider_usage VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    self.owner,
                    character_id,
                    turn_id,
                    model,
                    usage.input_tokens if usage else None,
                    usage.output_tokens if usage else None,
                    usage.estimated_usd if usage else None,
                    status,
                    time.time(),
                ),
            )

    def usage_summary(self, character_id: str) -> dict:
        with self._lock:
            self._character(character_id)
            row = self.db.execute(
                "SELECT count(*) AS attempts, "
                "coalesce(sum(input_tokens),0) AS reported_input_tokens, "
                "coalesce(sum(output_tokens),0) AS reported_output_tokens, "
                "coalesce(sum(estimated_usd),0) AS priced_estimate_usd, "
                "coalesce(sum(estimated_usd IS NULL),0) AS unpriced_attempts "
                "FROM provider_usage WHERE owner=? AND character_id=?",
                (self.owner, character_id),
            ).fetchone()
            return {**dict(row), "complete_cost_estimate": row["unpriced_attempts"] == 0}

    def forget_character(self, character_id: str, *, confirmation: str):
        """Delete this owner's character, transcripts, memories, revisions and usage."""
        if confirmation != character_id:
            raise ValueError("confirmation must equal the character ID")
        with self._transaction():
            self._character(character_id)
            self.db.execute(
                "DELETE FROM characters WHERE owner=? AND id=?", (self.owner, character_id)
            )

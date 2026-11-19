"""Grounded feedback, durable missing-work receipts and learned response style.

Feedback challenges an interpretation, never rewrites a person's feelings or
silently rewards intimacy. Host reasoning stays with the host.
"""

import json
import re

from .models import Appraisal, Attunement, ResponseStyle

REACTIONS = [
    ("boundary", r"别再提|不要再问|别这么叫我|leave me alone|don't call me|stop calling me"),
    (
        "rejected_strategy",
        r"别给我建议|不要给我建议|别分析我|别老问|don't give me advice|"
        r"stop asking questions|stop analyzing me",
    ),
    (
        "correction",
        r"不是这个意思|你理解错了|你误会了|你记错了|我没说过|you misunderstood|"
        r"that's not what i meant|you got that wrong",
    ),
    (
        "acceptance",
        r"这次懂我了|这正是我想要的|谢谢你听我说|that helped|"
        r"that's exactly what i needed|thanks for listening",
    ),
]


class Continuity:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db
        with engine._transaction():
            for sql in [
                "CREATE TABLE IF NOT EXISTS response_styles "
                "(owner TEXT,character_id TEXT,"
                "revision INTEGER,data TEXT,PRIMARY KEY(owner,character_id),"
                "FOREIGN KEY(owner,character_id) REFERENCES characters(owner,id) "
                "ON DELETE CASCADE)",
                "CREATE TABLE IF NOT EXISTS turn_continuity (turn_id TEXT PRIMARY KEY "
                "REFERENCES turns(id) ON DELETE CASCADE,appraisal_required INTEGER)",
                "CREATE TABLE IF NOT EXISTS reaction_links (turn_id TEXT PRIMARY KEY "
                "REFERENCES turns(id) ON DELETE CASCADE,previous_turn_id TEXT REFERENCES "
                "turns(id) ON DELETE CASCADE,label TEXT,quote TEXT)",
                "CREATE TABLE IF NOT EXISTS emotion_transitions (turn_id TEXT PRIMARY KEY "
                "REFERENCES turns(id) ON DELETE CASCADE,state_before TEXT,state_after TEXT)",
            ]:
                self.db.execute(sql)

    def feedback(self, character_id, turn_id, message):
        # Called in prepare's transaction. Exact retry cannot duplicate a feedback event.
        previous = self.db.execute(
            "SELECT id FROM turns WHERE owner=? AND character_id=? AND status='committed' "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (self.engine.owner, character_id),
        ).fetchone()
        if not previous:
            return {}
        for label, pattern in REACTIONS:
            match = re.search(pattern, message, re.I)
            if match:
                self.db.execute(
                    "INSERT OR IGNORE INTO reaction_links VALUES(?,?,?,?)",
                    (turn_id, previous[0], label, match.group()),
                )
                return {
                    "previous_turn_id": previous[0],
                    "label": label,
                    "source_quote": match.group(),
                    "confidence": "explicit_phrase_only",
                    "instruction": "Check whether this refers to your previous reply. "
                    "Repair a misread; current instructions override old interpretations. "
                    "This signal does not automatically change relationship scores.",
                }
        return {}

    def backlog(self, character_id, limit=8):
        with self.engine._lock:
            self.engine._character(character_id)
            rows = self.db.execute(
                "SELECT t.id,t.user_message,t.assistant_reply,t.created_at,"
                "a.turn_id IS NULL AS needs_attunement,"
                "(c.appraisal_required=1 AND p.turn_id IS NULL) AS needs_appraisal "
                "FROM turn_continuity c JOIN turns t ON t.id=c.turn_id "
                "LEFT JOIN turn_analysis a ON a.turn_id=t.id "
                "LEFT JOIN turn_appraisals p ON p.turn_id=t.id "
                "WHERE t.owner=? AND t.character_id=? AND t.status='committed' "
                "AND (a.turn_id IS NULL OR (c.appraisal_required=1 AND p.turn_id IS NULL)) "
                "ORDER BY t.created_at,t.rowid LIMIT ?",
                (self.engine.owner, character_id, limit),
            )
            return [dict(r) for r in rows]

    def repair(self, character_id, turn_id, *, attunement=None, appraisal=None):
        from .engine import Conflict, encoded

        if attunement is None and appraisal is None:
            raise ValueError("Supply missing analysis; never invent it automatically")
        with self.engine._transaction():
            turn = self.engine._turn(character_id, turn_id)
            if turn["status"] != "committed":
                raise Conflict("Only committed turns can be repaired")
            for table, value, model in [
                ("turn_analysis", attunement, Attunement),
                ("turn_appraisals", appraisal, Appraisal),
            ]:
                if value is None:
                    continue
                value = model.model_validate(value)
                if value.source_quote not in turn["user_message"]:
                    raise ValueError("Repair quote must be from the ORIGINAL user turn")
                row = self.db.execute(
                    f"SELECT data FROM {table} WHERE turn_id=?", (turn_id,)
                ).fetchone()
                if row and json.loads(row[0]) != value.model_dump(mode="json"):
                    raise Conflict("Existing analysis cannot be overwritten by repair")
                self.db.execute(
                    f"INSERT OR IGNORE INTO {table} VALUES(?,?)",
                    (turn_id, encoded(value.model_dump(mode="json"))),
                )
            # Never apply an old appraisal to today's mood (or apply a delta twice).
            return {
                "saved": True,
                "state_applied": False,
                "meaning": "Historical annotation only; no retroactive relationship delta.",
                "remaining": self.backlog(character_id),
            }

    def style(self, character_id):
        with self.engine._lock:
            self.engine._character(character_id)
            row = self.db.execute(
                "SELECT revision,data FROM response_styles WHERE owner=? AND character_id=?",
                (self.engine.owner, character_id),
            ).fetchone()
            if not row:
                return {"revision": 0, "rules": [], "stale_rules": 0}
            rules, stale = [], 0
            for rule in ResponseStyle.model_validate_json(row["data"]).rules:
                valid = True
                for source in rule.sources:
                    r = self.db.execute(
                        "SELECT revision FROM memories WHERE id=? AND owner=? AND character_id=?",
                        (source.memory_id, self.engine.owner, character_id),
                    ).fetchone()
                    if not r or r[0] != source.revision:
                        valid = False
                if valid:
                    rules.append(rule.model_dump(mode="json"))
                else:
                    stale += 1
            return {
                "revision": row["revision"],
                "rules": rules,
                "stale_rules": stale,
                "priority": "Current user request and boundaries override this learned style.",
            }

    def set_style(self, character_id, style, expected_revision=0):
        from .engine import Conflict

        style = ResponseStyle.model_validate(style)
        with self.engine._transaction():
            current = self.style(character_id)
            if current["revision"] != expected_revision:
                raise Conflict("Style changed; inspect before updating")
            for rule in style.rules:
                for source in rule.sources:
                    memory = self.engine._memory(
                        self.engine._get_memory(character_id, source.memory_id)
                    )
                    if memory.revision != source.revision or memory.category not in (
                        "style",
                        "boundary",
                    ):
                        raise Conflict("Style needs current, user-grounded style/boundary memories")
            self.db.execute(
                "INSERT INTO response_styles VALUES(?,?,?,?) ON CONFLICT(owner,"
                "character_id) DO UPDATE SET revision=excluded.revision,data=excluded.data",
                (self.engine.owner, character_id, expected_revision + 1, style.model_dump_json()),
            )
            return self.style(character_id)

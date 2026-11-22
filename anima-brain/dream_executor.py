"""AutoDream + Read-Only Dream Executor with human-approved offline reflection proposals.

Design invariants:
1. Strict Read-Only Analysis Boundary:
   - `ReadOnlyDreamAnalyzer` opens SQLite with `?mode=ro` URI + `PRAGMA query_only = ON`
     (never writes to turns/evidence/memories/documents/characters during analysis).
2. First-Hand Evidence Only (Anti-Recursive Contamination):
   - Reads ONLY committed `evidence` rows with `speaker='user'` (joined with `turns.status='committed'`).
   - Explicitly excludes derived `documents` (`core`/`episode`/`character_core`), `notes`,
     `speaker='assistant'` self-outputs, and prior proposals.
3. Zero Auto-Publish + Baseline Verification (CAS):
   - Proposals are stored as `status='pending'` in `xiaozhi_dream_proposals` inside the single
     authoritative `brain.sqlite3` database (no second parallel memory database).
   - Every proposal carries human-readable `diff_preview`, PII-redacted first-hand `evidence`,
     and a SHA-256 `baseline_hash` verified before human approval takes effect.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from companion_engine.engine import Conflict
from companion_engine.models import MemoryDetails, MemoryDraft
import recall_sanitizer

VALID_PROPOSAL_KINDS = frozenset({"asr_correction", "memory_merge", "style_adjustment"})
VALID_PROPOSAL_STATUSES = frozenset({"pending", "approved", "rejected", "stale", "failed"})

_SELF_CORRECTION_PATTERNS = (
    # "不是牙医，是芽衣" / "不是牙医是芽衣"
    re.compile(r"不是\s*([\u4e00-\u9fffA-Za-z0-9]{1,10})\s*[，,、]?\s*(?:而)?是\s*([\u4e00-\u9fffA-Za-z0-9]{1,10})"),
    # "我说的是芽衣，不是牙医"
    re.compile(
        r"(?:我说的是|我叫的是|是)\s*([\u4e00-\u9fffA-Za-z0-9]{1,10})\s*[，,、]\s*不是\s*([\u4e00-\u9fffA-Za-z0-9]{1,10})"
    ),
    # "把芽衣听成牙医" / "把芽衣识别成牙医"
    re.compile(
        r"把\s*([\u4e00-\u9fffA-Za-z0-9]{1,10})\s*(?:听成了?|识别成了?|打成了?)\s*([\u4e00-\u9fffA-Za-z0-9]{1,10})"
    ),
    # "把牙医改成芽衣" / "牙医应该是芽衣"
    re.compile(
        r"([\u4e00-\u9fffA-Za-z0-9]{2,10})\s*(?:应该是|其实是|要改成|改成)\s*([\u4e00-\u9fffA-Za-z0-9]{2,10})"
    ),
)

_STYLE_HABIT_RULES: tuple[dict[str, Any], ...] = (
    {
        "habit_id": "concise_replies",
        "key": "dream_style_concise",
        "title": "表达习惯建议：平时回复偏简洁直接",
        "suggestion": "平时对话尽量简短直接，少铺垫客套，先说重点再视情况展开。",
        "patterns": (
            re.compile(r"(?:说话|回复|回答).{0,4}(?:简短|简洁|短一点|少说|精简)"),
            re.compile(r"(?:太长了|别说太长|不用说那么长|长话短说)"),
        ),
    },
    {
        "habit_id": "detailed_replies",
        "key": "dream_style_detailed",
        "title": "表达习惯建议：讲故事与解释可更充分展开",
        "suggestion": "聊到故事、原理或感兴趣的话题时多展开细节，不必急着一句话结束。",
        "patterns": (
            re.compile(r"(?:多讲一点|多说一点|详细一点|讲细一点|展开说说|展开讲讲)"),
        ),
    },
    {
        "habit_id": "casual_warm_tone",
        "key": "dream_style_casual_warm",
        "title": "相处习惯建议：保持自然松弛、不客套的口吻",
        "suggestion": "相处时用自然轻松、亲近不客套的日常口吻聊天，避免官方敬语与生硬客气。",
        "patterns": (
            re.compile(r"(?:别太正式|不要太正式|不用那么客气|别这么客气|自然一点|随意一点|放松一点)"),
        ),
    },
    {
        "habit_id": "gentle_slow_pace",
        "key": "dream_style_gentle_pace",
        "title": "表达习惯建议：语气柔和、节奏放慢安抚",
        "suggestion": "对话节奏放舒缓柔和一些，多一点轻声安抚和耐心倾听，不催促。",
        "patterns": (
            re.compile(r"(?:慢一点说|说慢一点|温柔一点|轻声一点|语气柔和)"),
        ),
    },
)

_ADDRESSING_PREF_RE = re.compile(
    r"(?:以后|平时|直接)?(?:就)?叫我\s*[“\"「]?([\u4e00-\u9fffA-Za-z0-9]{1,8}?)(?:就行|就好|好了|吧|呀|啊)?[”\"」]?(?=$|[\s，,。！!？?；;、”\"」]|就行|就好|好了|吧|呀|啊)"
)
_NICK_TRAILING_PARTICLE_RE = re.compile(r"(?:就行|就好|好了|吧|呀|啊)+$")

_ASR_NOISE_STOPWORDS = frozenset(
    {
        "这样",
        "那样",
        "这个",
        "那个",
        "什么",
        "怎么",
        "没有",
        "不是",
        "可以",
        "好的",
        "今天",
        "明天",
        "昨天",
        "现在",
        "时候",
        "事情",
        "东西",
        "地方",
        "问题",
        "意思",
        "自己",
        "我们",
        "你们",
        "他们",
    }
)


class DreamConflictError(RuntimeError):
    """Raised when baseline verification (CAS) fails during proposal approval."""

    def __init__(
        self,
        message: str,
        *,
        expected_baseline_hash: str = "",
        current_baseline_hash: str = "",
    ):
        super().__init__(message)
        self.expected_baseline_hash = expected_baseline_hash
        self.current_baseline_hash = current_baseline_hash


def _sha256_json(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_asr_baseline_hash(wrong: str, current_to: str = "") -> str:
    """Deterministic baseline checksum for a single ASR homophone rule."""
    return _sha256_json(
        {
            "kind": "asr_correction",
            "from": str(wrong or "").strip(),
            "current_to": str(current_to or "").strip(),
        }
    )


def _compute_style_key_baseline_hash(
    key: str, existing_rows: list[tuple[str, int, str]]
) -> str:
    """Per-key CAS baseline hash so sibling style proposals do not self-invalidate."""
    return _sha256_json(
        {
            "kind": "style_adjustment",
            "key": str(key or "").strip().lower(),
            "existing": sorted(existing_rows),
        }
    )


def _fmt_iso(ts: float | None, tz_name: str = "Asia/Shanghai") -> str:
    if not ts:
        return ""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return datetime.fromtimestamp(float(ts), tz).isoformat(timespec="seconds")


def _extract_cjk_bigrams(text: str) -> set[str]:
    clean = re.sub(r"[\s\W_]+", "", str(text or "").lower())
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / float(len(a | b))


def _edit_distance_le_one(a: str, b: str) -> bool:
    if a == b:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    short, long_s = (a, b) if len(a) < len(b) else (b, a)
    i = j = diffs = 0
    while i < len(short) and j < len(long_s):
        if short[i] == long_s[j]:
            i += 1
            j += 1
        else:
            diffs += 1
            if diffs > 1:
                return False
            j += 1
    return True


def _sanitize_evidence_excerpt(text: str, max_len: int = 160) -> str:
    clean = " ".join(str(text or "").strip().split())
    sanitized, _ = recall_sanitizer.redact_sensitive_entities(clean)
    if len(sanitized) > max_len:
        sanitized = sanitized[: max_len - 1].rstrip() + "…"
    return sanitized


def ensure_dream_tables(engine) -> None:
    """Create persistent AutoDream proposal & schedule metadata tables in `brain.sqlite3`."""
    with engine._transaction():
        engine.db.execute(
            "CREATE TABLE IF NOT EXISTS xiaozhi_dream_proposals ("
            "id TEXT PRIMARY KEY, "
            "owner TEXT NOT NULL, "
            "character_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "title TEXT NOT NULL, "
            "summary TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, "
            "diff_preview TEXT NOT NULL, "
            "evidence_json TEXT NOT NULL, "
            "baseline_hash TEXT NOT NULL, "
            "dedup_key TEXT NOT NULL, "
            "created_at REAL NOT NULL, "
            "reviewed_at REAL, "
            "review_note TEXT, "
            "applied_result_json TEXT)"
        )
        engine.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dream_proposals_scope "
            "ON xiaozhi_dream_proposals(owner, character_id, status, created_at DESC)"
        )
        engine.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dream_proposals_dedup "
            "ON xiaozhi_dream_proposals(owner, character_id, dedup_key)"
        )
        engine.db.execute(
            "CREATE TABLE IF NOT EXISTS xiaozhi_dream_meta ("
            "owner TEXT NOT NULL, "
            "character_id TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 1, "
            "interval_seconds INTEGER NOT NULL DEFAULT 21600, "
            "last_run_at REAL NOT NULL DEFAULT 0, "
            "last_run_reason TEXT NOT NULL DEFAULT '', "
            "last_user_evidence_count INTEGER NOT NULL DEFAULT 0, "
            "known_asr_json TEXT NOT NULL DEFAULT '', "
            "PRIMARY KEY(owner, character_id))"
        )
        cols = {
            str(r[1])
            for r in engine.db.execute("PRAGMA table_info(xiaozhi_dream_meta)").fetchall()
        }
        if "known_asr_json" not in cols:
            engine.db.execute(
                "ALTER TABLE xiaozhi_dream_meta ADD COLUMN known_asr_json TEXT NOT NULL DEFAULT ''"
            )
        engine.db.execute(
            "CREATE TABLE IF NOT EXISTS xiaozhi_dream_globals ("
            "owner TEXT PRIMARY KEY, "
            "known_asr_json TEXT NOT NULL DEFAULT '', "
            "updated_at REAL NOT NULL DEFAULT 0)"
        )


def _open_readonly_sqlite(db_path: Path) -> sqlite3.Connection:
    """Open a genuine SQLite read-only connection (`mode=ro` + `PRAGMA query_only = ON`)."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 3000")
    return conn


class ReadOnlyDreamAnalyzer:
    """Strict read-only reflection analyzer over first-hand user evidence."""

    def __init__(self, db_path: Path, owner: str):
        self.db_path = Path(db_path)
        self.owner = str(owner)

    def _connect_ro(self) -> sqlite3.Connection:
        return _open_readonly_sqlite(self.db_path)

    def analyze(
        self,
        character_id: str,
        *,
        existing_asr_corrections: dict[str, str] | None = None,
        known_terms: list[str] | None = None,
        max_evidence: int = 300,
    ) -> dict[str, Any]:
        """Run strictly read-only analysis over first-hand user evidence and active memories."""
        asr_map = {
            str(k).strip(): str(v).strip()
            for k, v in (existing_asr_corrections or {}).items()
            if str(k).strip() and str(v).strip()
        }
        conn = self._connect_ro()
        try:
            char_row = conn.execute(
                "SELECT id, spec FROM characters WHERE owner=? AND id=?",
                (self.owner, character_id),
            ).fetchone()
            if not char_row:
                return {
                    "character_id": character_id,
                    "scanned_user_evidence": 0,
                    "proposals": [],
                }
            spec_obj = json.loads(char_row["spec"])
            char_name = str(spec_obj.get("name") or "").strip()

            # 1. FIRST-HAND USER EVIDENCE ONLY (strictly excludes documents, notes, assistant self-output)
            ev_rows = conn.execute(
                "SELECT e.id AS evidence_id, e.turn_id, e.speaker, e.text, e.observed_at "
                "FROM evidence e "
                "JOIN turns t ON t.id = e.turn_id "
                "WHERE e.owner=? AND e.character_id=? "
                "AND e.speaker='user' AND t.status='committed' "
                "ORDER BY e.observed_at DESC, e.rowid DESC LIMIT ?",
                (self.owner, character_id, int(max(10, min(1000, max_evidence)))),
            ).fetchall()
            user_evidence = [dict(r) for r in ev_rows]

            # 2. Active memories + first-hand provenance (for merge/conflict & style dedup)
            mem_rows = conn.execute(
                "SELECT m.id, m.key, m.text, m.category, m.source_quote, m.source_turn_id, "
                "m.revision, m.updated_at, "
                "coalesce(d.data, '{}') AS details_json, "
                "coalesce(l.data, '{}') AS lifecycle_json "
                "FROM memories m "
                "LEFT JOIN memory_details d ON d.memory_id = m.id "
                "LEFT JOIN fact_lifecycle l ON l.memory_id = m.id "
                "WHERE m.owner=? AND m.character_id=? "
                "ORDER BY m.updated_at DESC, m.id ASC LIMIT 300",
                (self.owner, character_id),
            ).fetchall()

            active_memories: list[dict[str, Any]] = []
            for r in mem_rows:
                rec = dict(r)
                try:
                    lc = json.loads(rec.get("lifecycle_json") or "{}")
                except Exception:
                    lc = {}
                if lc.get("invalidation"):
                    continue
                try:
                    det = json.loads(rec.get("details_json") or "{}")
                except Exception:
                    det = {}
                rec["details"] = det
                rec["lifecycle"] = lc
                active_memories.append(rec)

            # Map turn_id -> first-hand user evidence row for fast provenance verification
            user_ev_by_turn: dict[str, dict[str, Any]] = {
                str(ev["turn_id"]): ev for ev in user_evidence if ev.get("turn_id")
            }

            proposals: list[dict[str, Any]] = []
            proposals.extend(
                self._propose_asr_corrections(
                    user_evidence,
                    asr_map=asr_map,
                    char_name=char_name,
                    known_terms=known_terms or [],
                )
            )
            proposals.extend(
                self._propose_memory_merges(
                    conn,
                    character_id,
                    active_memories,
                    user_ev_by_turn,
                )
            )
            proposals.extend(
                self._propose_style_adjustments(
                    user_evidence,
                    active_memories,
                )
            )

            return {
                "character_id": character_id,
                "scanned_user_evidence": len(user_evidence),
                "active_memories_scanned": len(active_memories),
                "proposals": proposals,
            }
        finally:
            conn.close()

    def _propose_asr_corrections(
        self,
        user_evidence: list[dict[str, Any]],
        *,
        asr_map: dict[str, str],
        char_name: str,
        known_terms: list[str],
    ) -> list[dict[str, Any]]:
        pair_hits: dict[tuple[str, str], list[dict[str, Any]]] = {}

        for ev in user_evidence:
            text = str(ev.get("text") or "").strip()
            if not text or len(text) < 4:
                continue
            # Pattern 0: "不是X是Y" -> wrong=X, right=Y
            for m in _SELF_CORRECTION_PATTERNS[0].finditer(text):
                wrong, right = m.group(1).strip(), m.group(2).strip()
                self._collect_asr_pair(pair_hits, wrong, right, ev, asr_map, explicit=True)
            # Pattern 1: "我说的是Y，不是X" -> right=group1, wrong=group2
            for m in _SELF_CORRECTION_PATTERNS[1].finditer(text):
                right, wrong = m.group(1).strip(), m.group(2).strip()
                self._collect_asr_pair(pair_hits, wrong, right, ev, asr_map, explicit=True)
            # Pattern 2: "把Y听成X" -> right=group1, wrong=group2
            for m in _SELF_CORRECTION_PATTERNS[2].finditer(text):
                right, wrong = m.group(1).strip(), m.group(2).strip()
                self._collect_asr_pair(pair_hits, wrong, right, ev, asr_map, explicit=True)
            # Pattern 3: "X应该是Y" -> wrong=group1, right=group2
            for m in _SELF_CORRECTION_PATTERNS[3].finditer(text):
                wrong, right = m.group(1).strip(), m.group(2).strip()
                self._collect_asr_pair(pair_hits, wrong, right, ev, asr_map, explicit=True)

        # Also check repeated edit-distance-1 confusions against protected character/entity names (>=2 turns)
        target_terms: list[str] = []
        for raw_t in [char_name, *known_terms]:
            t = str(raw_t or "").strip()
            if 2 <= len(t) <= 6 and re.fullmatch(r"[\u4e00-\u9fff]+", t) and t not in target_terms:
                target_terms.append(t)

        if target_terms:
            variant_hits: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for ev in user_evidence:
                text = str(ev.get("text") or "")
                cjk_spans = re.findall(r"[\u4e00-\u9fff]{2,}", text)
                seen_in_ev: set[tuple[str, str]] = set()
                for target in target_terms:
                    tlen = len(target)
                    if tlen < 3:
                        # 2-char edit-distance-1 has too many coincidental Chinese words; keep strictly for >=3-char entity names
                        continue
                    for span in cjk_spans:
                        if len(span) < tlen:
                            continue
                        for i in range(0, len(span) - tlen + 1):
                            cand = span[i : i + tlen]
                            if cand == target or cand in asr_map or cand in _ASR_NOISE_STOPWORDS:
                                continue
                            if _edit_distance_le_one(cand, target):
                                key = (cand, target)
                                if key not in seen_in_ev:
                                    seen_in_ev.add(key)
                                    variant_hits.setdefault(key, []).append(ev)
            for (wrong, right), evs in variant_hits.items():
                if len(evs) >= 2 and (wrong, right) not in pair_hits:
                    pair_hits[(wrong, right)] = evs

        proposals: list[dict[str, Any]] = []
        for (wrong, right), evs in sorted(pair_hits.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if wrong in asr_map and asr_map[wrong] == right:
                continue
            cur_to = asr_map.get(wrong, "")
            baseline = compute_asr_baseline_hash(wrong, cur_to)
            ev_citations = [
                {
                    "evidence_id": e["evidence_id"],
                    "turn_id": e["turn_id"],
                    "speaker": "user",
                    "observed_at": float(e["observed_at"]),
                    "observed_at_iso": _fmt_iso(e["observed_at"]),
                    "excerpt": _sanitize_evidence_excerpt(e["text"]),
                }
                for e in evs[:4]
            ]
            diff_lines = [
                "--- config/runtime/asr_corrections.json",
                "+++ config/runtime/asr_corrections.json (待审批变更)",
            ]
            if cur_to:
                diff_lines.append(f'- "{wrong}": "{cur_to}"')
            diff_lines.append(f'+ "{wrong}": "{right}"  # 来自 {len(evs)} 条一手用户原话')
            proposals.append(
                {
                    "kind": "asr_correction",
                    "title": f"新增 ASR 同音纠错：{wrong} → {right}",
                    "summary": f"从 {len(evs)} 条一手用户对话中发现「{wrong}」疑似误识，建议人工审核后加入同音词纠错表。",
                    "payload": {
                        "from": wrong,
                        "to": right,
                        "previous_to": cur_to,
                        "occurrences": len(evs),
                    },
                    "diff_preview": "\n".join(diff_lines),
                    "evidence": ev_citations,
                    "baseline_hash": baseline,
                    "dedup_key": f"asr_correction:{wrong}->{right}",
                }
            )
            if len(proposals) >= 8:
                break
        return proposals

    @staticmethod
    def _collect_asr_pair(
        pair_hits: dict[tuple[str, str], list[dict[str, Any]]],
        wrong: str,
        right: str,
        ev: dict[str, Any],
        asr_map: dict[str, str],
        *,
        explicit: bool,
    ) -> None:
        if not wrong or not right or wrong == right:
            return
        if len(wrong) < 2 or len(right) < 2 or len(wrong) > 10 or len(right) > 10:
            return
        if wrong in _ASR_NOISE_STOPWORDS or right in _ASR_NOISE_STOPWORDS:
            return
        if asr_map.get(wrong) == right:
            return
        # Guard against wildly unrelated long clauses in casual "不是A是B" sentences:
        # require similar length (|len diff| <= 1) for ASR homophone self-corrections.
        if abs(len(wrong) - len(right)) > 1:
            return
        key = (wrong, right)
        lst = pair_hits.setdefault(key, [])
        if all(x["evidence_id"] != ev["evidence_id"] for x in lst):
            lst.append(ev)

    def _propose_memory_merges(
        self,
        conn: sqlite3.Connection,
        character_id: str,
        active_memories: list[dict[str, Any]],
        user_ev_by_turn: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if len(active_memories) < 2:
            return []

        proposals: list[dict[str, Any]] = []
        used_ids: set[str] = set()

        def _fetch_user_evidence_for_memories(mems: list[dict[str, Any]]) -> list[dict[str, Any]]:
            citations: list[dict[str, Any]] = []
            seen_eids: set[str] = set()
            for m in mems:
                tid = str(m.get("source_turn_id") or "")
                ev = user_ev_by_turn.get(tid)
                if ev is None and tid:
                    row = conn.execute(
                        "SELECT e.id AS evidence_id, e.turn_id, e.speaker, e.text, e.observed_at "
                        "FROM evidence e JOIN turns t ON t.id = e.turn_id "
                        "WHERE e.owner=? AND e.character_id=? AND e.turn_id=? "
                        "AND e.speaker='user' AND t.status='committed' LIMIT 1",
                        (self.owner, character_id, tid),
                    ).fetchone()
                    if row:
                        ev = dict(row)
                if ev and ev["evidence_id"] not in seen_eids:
                    seen_eids.add(ev["evidence_id"])
                    citations.append(
                        {
                            "evidence_id": ev["evidence_id"],
                            "turn_id": ev["turn_id"],
                            "speaker": "user",
                            "observed_at": float(ev["observed_at"]),
                            "observed_at_iso": _fmt_iso(ev["observed_at"]),
                            "excerpt": _sanitize_evidence_excerpt(ev["text"]),
                        }
                    )
                elif m.get("source_quote"):
                    # Fallback citation only when quote came from a committed memory without turn link
                    citations.append(
                        {
                            "evidence_id": f"memory_quote:{m['id']}",
                            "turn_id": tid,
                            "speaker": "user",
                            "observed_at": float(m.get("updated_at") or 0.0),
                            "observed_at_iso": _fmt_iso(m.get("updated_at")),
                            "excerpt": _sanitize_evidence_excerpt(str(m.get("source_quote") or "")),
                        }
                    )
            return citations[:4]

        def _build_merge_proposal(group: list[dict[str, Any]], reason_zh: str, reason_code: str) -> dict[str, Any] | None:
            # Sort newest first; prefer richer text on tie
            ordered = sorted(
                group,
                key=lambda m: (float(m.get("updated_at") or 0.0), int(m.get("revision") or 1), len(m.get("text") or "")),
                reverse=True,
            )
            keep = ordered[0]
            retire = ordered[1:]
            if not retire:
                return None
            citations = _fetch_user_evidence_for_memories(ordered)
            if not citations:
                return None
            baseline_rows = sorted(
                [
                    {
                        "id": m["id"],
                        "revision": int(m["revision"]),
                        "key": str(m["key"]),
                        "text": str(m["text"]),
                        "active": True,
                    }
                    for m in ordered
                ],
                key=lambda x: x["id"],
            )
            baseline = _sha256_json({"kind": "memory_merge", "memories": baseline_rows})
            diff_lines = [
                f"--- memories ({keep['category']})",
                f"+++ memories ({keep['category']} · 待审批合并/归档)",
            ]
            for rm in retire:
                diff_lines.append(f"- [淘汰 rev.{rm['revision']}] ({rm['key']}) {_sanitize_evidence_excerpt(rm['text'], 100)}")
            diff_lines.append(
                f"+ [保留 rev.{keep['revision']}] ({keep['key']}) {_sanitize_evidence_excerpt(keep['text'], 100)}"
            )
            retire_ids_sorted = ",".join(sorted(r["id"] for r in retire))
            return {
                "kind": "memory_merge",
                "title": f"清理/合并冲突或重复记忆：{keep['key']}（归档 {len(retire)} 条旧记录）",
                "summary": f"{reason_zh}。建议保留较新的「{_sanitize_evidence_excerpt(keep['text'], 48)}」，并将 {len(retire)} 条旧/重复记忆标记为已替代（superseded），保留修订历史不删原始证据。",
                "payload": {
                    "reason_code": reason_code,
                    "keep_memory_id": keep["id"],
                    "keep_revision": int(keep["revision"]),
                    "keep_key": str(keep["key"]),
                    "keep_text": _sanitize_evidence_excerpt(str(keep["text"]), 240),
                    "retire_memories": [
                        {
                            "id": rm["id"],
                            "revision": int(rm["revision"]),
                            "key": str(rm["key"]),
                            "text": _sanitize_evidence_excerpt(str(rm["text"]), 240),
                        }
                        for rm in retire
                    ],
                },
                "diff_preview": "\n".join(diff_lines),
                "evidence": citations,
                "baseline_hash": baseline,
                "dedup_key": f"memory_merge:{keep['id']}:{retire_ids_sorted}",
            }

        # Group A: Active memories sharing the same non-empty state_key (mutable state conflict)
        by_state_key: dict[str, list[dict[str, Any]]] = {}
        for m in active_memories:
            sk = str((m.get("details") or {}).get("state_key") or "").strip().lower()
            if sk:
                by_state_key.setdefault(sk, []).append(m)
        for sk, group in by_state_key.items():
            if len(group) >= 2:
                prop = _build_merge_proposal(
                    group,
                    f"检测到同一可变状态键 `{sk}` 存在 {len(group)} 条并发有效记忆",
                    "same_state_key",
                )
                if prop:
                    proposals.append(prop)
                    for m in group:
                        used_ids.add(m["id"])

        # Group B: Same category with identical normalized key OR high bigram overlap (>=0.72)
        remaining = [m for m in active_memories if m["id"] not in used_ids]
        by_cat: dict[str, list[dict[str, Any]]] = {}
        for m in remaining:
            by_cat.setdefault(str(m.get("category") or "fact"), []).append(m)

        for cat, items in by_cat.items():
            if len(items) < 2:
                continue
            visited: set[str] = set()
            grams = {m["id"]: _extract_cjk_bigrams(m.get("text") or "") for m in items}
            for i, m1 in enumerate(items):
                if m1["id"] in visited:
                    continue
                cluster = [m1]
                k1 = str(m1.get("key") or "").strip().lower()
                g1 = grams[m1["id"]]
                for m2 in items[i + 1 :]:
                    if m2["id"] in visited:
                        continue
                    k2 = str(m2.get("key") or "").strip().lower()
                    same_key = bool(k1 and k1 == k2)
                    sim = _jaccard(g1, grams[m2["id"]]) if (len(g1) >= 3 and len(grams[m2["id"]]) >= 3) else 0.0
                    if same_key or sim >= 0.72:
                        cluster.append(m2)
                if len(cluster) >= 2:
                    for cm in cluster:
                        visited.add(cm["id"])
                    prop = _build_merge_proposal(
                        cluster,
                        f"在「{cat}」分类下发现 {len(cluster)} 条高度重叠或同键记忆",
                        "duplicate_or_high_overlap",
                    )
                    if prop:
                        proposals.append(prop)
                        if len(proposals) >= 8:
                            return proposals
        return proposals[:8]

    def _propose_style_adjustments(
        self,
        user_evidence: list[dict[str, Any]],
        active_memories: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        style_mems = [m for m in active_memories if m.get("category") == "style"]
        existing_style_keys = {str(m.get("key") or "").strip().lower() for m in style_mems}
        existing_style_blob = " ".join(str(m.get("text") or "") for m in style_mems)
        style_rows_by_key: dict[str, list[tuple[str, int, str]]] = {}
        for m in style_mems:
            mk = str(m.get("key") or "").strip().lower()
            style_rows_by_key.setdefault(mk, []).append((m["id"], int(m["revision"]), str(m["text"])))

        proposals: list[dict[str, Any]] = []

        # 1. Recurring conversational habit feedback (>= 2 distinct committed user turns)
        for rule in _STYLE_HABIT_RULES:
            if rule["key"] in existing_style_keys:
                continue
            matched_evs: list[dict[str, Any]] = []
            for ev in user_evidence:
                txt = str(ev.get("text") or "").strip()
                if not txt:
                    continue
                if any(pat.search(txt) for pat in rule["patterns"]):
                    matched_evs.append(ev)
            if len(matched_evs) >= 2:
                latest_ev = matched_evs[0]
                safe_quote = _sanitize_evidence_excerpt(latest_ev["text"], max_len=180)
                citations = [
                    {
                        "evidence_id": e["evidence_id"],
                        "turn_id": e["turn_id"],
                        "speaker": "user",
                        "observed_at": float(e["observed_at"]),
                        "observed_at_iso": _fmt_iso(e["observed_at"]),
                        "excerpt": _sanitize_evidence_excerpt(e["text"]),
                    }
                    for e in matched_evs[:4]
                ]
                diff_lines = [
                    "--- memories (category=style)",
                    "+++ memories (category=style · 待审批新增表达偏好)",
                    f"+ [{rule['key']}] {rule['suggestion']}",
                ]
                per_key_baseline = _compute_style_key_baseline_hash(
                    rule["key"], style_rows_by_key.get(rule["key"].lower(), [])
                )
                proposals.append(
                    {
                        "kind": "style_adjustment",
                        "title": rule["title"],
                        "summary": f"你在 {len(matched_evs)} 轮一手对话中多次表达相似交互偏好，建议经确认后记入 style 长期记忆（仅调表达习惯，不涉及工具权限）。",
                        "payload": {
                            "habit_id": rule["habit_id"],
                            "key": rule["key"],
                            "category": "style",
                            "text": rule["suggestion"],
                            "source_quote": safe_quote,
                            "source_turn_id": latest_ev["turn_id"],
                        },
                        "diff_preview": "\n".join(diff_lines),
                        "evidence": citations,
                        "baseline_hash": per_key_baseline,
                        "dedup_key": f"style_adjustment:{rule['key']}",
                    }
                )

        # 2. Recurring explicit addressing preference ("以后叫我XX" >= 2 turns)
        addr_hits: dict[str, list[dict[str, Any]]] = {}
        for ev in user_evidence:
            txt = str(ev.get("text") or "").strip()
            m = _ADDRESSING_PREF_RE.search(txt)
            if m:
                nick = m.group(1).strip()
                if len(nick) >= 2:
                    stripped = _NICK_TRAILING_PARTICLE_RE.sub("", nick).strip()
                    if stripped:
                        nick = stripped
                if 1 <= len(nick) <= 8 and nick not in _ASR_NOISE_STOPWORDS:
                    addr_hits.setdefault(nick, []).append(ev)
        for nick, evs in addr_hits.items():
            if len(evs) < 2:
                continue
            if nick in existing_style_blob:
                continue
            skey = f"dream_style_address_{hashlib.sha256(nick.encode('utf-8')).hexdigest()[:8]}"
            if skey in existing_style_keys:
                continue
            latest_ev = evs[0]
            suggestion = f"用户希望在日常交流中被称呼为「{nick}」。"
            safe_quote = _sanitize_evidence_excerpt(latest_ev["text"], max_len=180)
            per_key_baseline = _compute_style_key_baseline_hash(
                skey, style_rows_by_key.get(skey.lower(), [])
            )
            citations = [
                {
                    "evidence_id": e["evidence_id"],
                    "turn_id": e["turn_id"],
                    "speaker": "user",
                    "observed_at": float(e["observed_at"]),
                    "observed_at_iso": _fmt_iso(e["observed_at"]),
                    "excerpt": _sanitize_evidence_excerpt(e["text"]),
                }
                for e in evs[:4]
            ]
            proposals.append(
                {
                    "kind": "style_adjustment",
                    "title": f"称呼习惯建议：日常称呼对方为「{nick}」",
                    "summary": f"在 {len(evs)} 轮一手对话中对方多次提到希望被称呼为「{nick}」，批准后将作为 style 记忆沉淀。",
                    "payload": {
                        "habit_id": "addressing_preference",
                        "key": skey,
                        "category": "style",
                        "text": suggestion,
                        "source_quote": safe_quote,
                        "source_turn_id": latest_ev["turn_id"],
                    },
                    "diff_preview": (
                        "--- memories (category=style)\n"
                        "+++ memories (category=style · 待审批新增称呼偏好)\n"
                        f"+ [{skey}] {suggestion}"
                    ),
                    "evidence": citations,
                    "baseline_hash": per_key_baseline,
                    "dedup_key": f"style_adjustment:{skey}",
                }
            )
        return proposals[:6]


class DreamManager:
    """Coordinates read-only AutoDream runs, proposal storage, baseline CAS verification, and human approval."""

    def __init__(
        self,
        brain,
        *,
        default_enabled: bool = True,
        default_interval_seconds: int = 21600,
    ):
        self.brain = brain
        self.engine = brain.engine
        self.default_enabled = bool(default_enabled)
        self.default_interval_seconds = max(300, min(604800, int(default_interval_seconds)))
        self._lock = threading.RLock()
        self._default_known_asr_json: str = ""
        ensure_dream_tables(self.engine)
        self._default_known_asr_json = self._load_persisted_default_known_asr()

    def _load_persisted_default_known_asr(self) -> str:
        with self.engine._lock:
            row = self.engine.db.execute(
                "SELECT known_asr_json FROM xiaozhi_dream_globals WHERE owner=?",
                (self.engine.owner,),
            ).fetchone()
        return str(row["known_asr_json"] or "") if row else ""

    @staticmethod
    def _normalize_asr_corrections(raw: dict[str, Any]) -> dict[str, str]:
        if not isinstance(raw, dict):
            raise ValueError("existing_asr_corrections must be a dict")
        out: dict[str, str] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            ks = k.strip()[:128]
            vs = v.strip()[:128]
            if ks and vs:
                out[ks] = vs
                if len(out) >= 2000:
                    break
        return out

    def _ensure_meta_row(self, character_id: str) -> dict[str, Any]:
        with self.engine._transaction():
            row = self.engine.db.execute(
                "SELECT * FROM xiaozhi_dream_meta WHERE owner=? AND character_id=?",
                (self.engine.owner, character_id),
            ).fetchone()
            if not row:
                g_row = self.engine.db.execute(
                    "SELECT known_asr_json FROM xiaozhi_dream_globals WHERE owner=?",
                    (self.engine.owner,),
                ).fetchone()
                default_known = (
                    str(g_row["known_asr_json"] or "")
                    if g_row is not None
                    else self._default_known_asr_json
                )
                self.engine.db.execute(
                    "INSERT INTO xiaozhi_dream_meta"
                    "(owner, character_id, enabled, interval_seconds, last_run_at, last_run_reason, last_user_evidence_count, known_asr_json) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        self.engine.owner,
                        character_id,
                        1 if self.default_enabled else 0,
                        self.default_interval_seconds,
                        0.0,
                        "",
                        0,
                        default_known,
                    ),
                )
                row = self.engine.db.execute(
                    "SELECT * FROM xiaozhi_dream_meta WHERE owner=? AND character_id=?",
                    (self.engine.owner, character_id),
                ).fetchone()
        return dict(row)

    def get_config(self, character_id: str) -> dict[str, Any]:
        meta = self._ensure_meta_row(character_id)
        known_asr_count = 0
        raw_known = str(meta.get("known_asr_json") or "").strip()
        if raw_known:
            try:
                parsed = json.loads(raw_known)
                if isinstance(parsed, dict):
                    known_asr_count = len(parsed)
            except Exception:
                known_asr_count = 0
        return {
            "enabled": bool(meta["enabled"]),
            "interval_seconds": int(meta["interval_seconds"]),
            "last_run_at": _fmt_iso(meta["last_run_at"]) if meta["last_run_at"] else "",
            "last_run_ts": float(meta["last_run_at"] or 0.0),
            "last_run_reason": str(meta["last_run_reason"] or ""),
            "last_user_evidence_count": int(meta["last_user_evidence_count"] or 0),
            "known_asr_count": known_asr_count,
        }

    def sync_known_asr_corrections(
        self,
        existing_asr_corrections: dict[str, str],
        *,
        character_id: str | None = None,
        all_characters: bool = False,
    ) -> dict[str, Any]:
        """Persist local asr_corrections snapshot into xiaozhi_dream_meta and mark drifted pending ASR proposals stale."""
        norm_known = self._normalize_asr_corrections(existing_asr_corrections)
        known_json = json.dumps(norm_known, ensure_ascii=False, sort_keys=True)
        now = time.time()
        with self._lock:
            if all_characters or not character_id:
                self._default_known_asr_json = known_json
            stale_marked = 0
            synced_chars: list[str] = []
            with self.engine._transaction():
                if all_characters or not character_id:
                    self.engine.db.execute(
                        "INSERT INTO xiaozhi_dream_globals(owner, known_asr_json, updated_at) "
                        "VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET "
                        "known_asr_json=excluded.known_asr_json, updated_at=excluded.updated_at",
                        (self.engine.owner, known_json, now),
                    )
                    c_rows = self.engine.db.execute(
                        "SELECT id FROM characters WHERE owner=?",
                        (self.engine.owner,),
                    ).fetchall()
                    m_rows = self.engine.db.execute(
                        "SELECT character_id FROM xiaozhi_dream_meta WHERE owner=?",
                        (self.engine.owner,),
                    ).fetchall()
                    targets = {str(r[0]).strip() for r in c_rows if str(r[0] or "").strip()}
                    targets.update(str(r[0]).strip() for r in m_rows if str(r[0] or "").strip())
                    if character_id and str(character_id).strip():
                        targets.add(str(character_id).strip())
                    target_list = sorted(targets)
                else:
                    target_list = [str(character_id).strip()]

                for cid in target_list:
                    self.engine.db.execute(
                        "INSERT OR IGNORE INTO xiaozhi_dream_meta"
                        "(owner, character_id, enabled, interval_seconds, last_run_at, last_run_reason, last_user_evidence_count, known_asr_json) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            self.engine.owner,
                            cid,
                            1 if self.default_enabled else 0,
                            self.default_interval_seconds,
                            0.0,
                            "",
                            0,
                            known_json,
                        ),
                    )
                    self.engine.db.execute(
                        "UPDATE xiaozhi_dream_meta SET known_asr_json=? WHERE owner=? AND character_id=?",
                        (known_json, self.engine.owner, cid),
                    )
                    synced_chars.append(cid)
                    pending_rows = self.engine.db.execute(
                        "SELECT id, payload_json, baseline_hash FROM xiaozhi_dream_proposals "
                        "WHERE owner=? AND character_id=? AND status='pending' AND kind='asr_correction'",
                        (self.engine.owner, cid),
                    ).fetchall()
                    for prow in pending_rows:
                        try:
                            ppayload = json.loads(prow["payload_json"])
                        except Exception:
                            continue
                        wrong = str(ppayload.get("from") or "").strip()
                        live_h = compute_asr_baseline_hash(wrong, str(norm_known.get(wrong) or ""))
                        if live_h != prow["baseline_hash"]:
                            self.engine.db.execute(
                                "UPDATE xiaozhi_dream_proposals SET status='stale', reviewed_at=?, "
                                "review_note='asr_corrections_synced' WHERE id=?",
                                (now, prow["id"]),
                            )
                            stale_marked += 1
            return {
                "known_asr_count": len(norm_known),
                "synced_characters": synced_chars,
                "stale_marked": stale_marked,
            }

    def update_config(
        self,
        character_id: str,
        *,
        enabled: bool | None = None,
        interval_seconds: int | None = None,
        existing_asr_corrections: dict[str, str] | None = None,
        sync_all_characters: bool = False,
    ) -> dict[str, Any]:
        # Validate all fields before mutating SQLite
        if interval_seconds is not None:
            if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
                raise ValueError("interval_seconds must be an integer")
            if not (300 <= interval_seconds <= 604800):
                raise ValueError("interval_seconds must be between 300 and 604800")
        norm_asr: dict[str, str] | None = None
        if existing_asr_corrections is not None:
            norm_asr = self._normalize_asr_corrections(existing_asr_corrections)

        with self._lock:
            meta = self._ensure_meta_row(character_id)
            if enabled is not None or interval_seconds is not None:
                next_enabled = bool(meta["enabled"]) if enabled is None else bool(enabled)
                next_interval = int(meta["interval_seconds"]) if interval_seconds is None else int(interval_seconds)
                with self.engine._transaction():
                    self.engine.db.execute(
                        "UPDATE xiaozhi_dream_meta SET enabled=?, interval_seconds=? WHERE owner=? AND character_id=?",
                        (1 if next_enabled else 0, next_interval, self.engine.owner, character_id),
                    )
            sync_res: dict[str, Any] | None = None
            if norm_asr is not None:
                sync_res = self.sync_known_asr_corrections(
                    norm_asr,
                    character_id=character_id,
                    all_characters=sync_all_characters,
                )
            cfg = self.get_config(character_id)
            if sync_res is not None:
                cfg["stale_marked"] = sync_res["stale_marked"]
                cfg["synced_characters"] = sync_res["synced_characters"]
            return cfg

    def _compute_live_baseline(
        self,
        character_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        existing_asr_corrections: dict[str, str] | None = None,
    ) -> str | None:
        if kind == "asr_correction":
            if existing_asr_corrections is None:
                return None
            wrong = str(payload.get("from") or "").strip()
            return compute_asr_baseline_hash(wrong, str(existing_asr_corrections.get(wrong) or ""))

        with self.engine._lock:
            if kind == "memory_merge":
                mids = [str(payload.get("keep_memory_id") or "")] + [
                    str(r.get("id") or "") for r in (payload.get("retire_memories") or []) if isinstance(r, dict)
                ]
                mids = [m for m in mids if m]
                rows_info = []
                for mid in sorted(set(mids)):
                    r = self.engine.db.execute(
                        "SELECT m.id, m.revision, m.key, m.text, coalesce(l.data, '{}') AS lc "
                        "FROM memories m LEFT JOIN fact_lifecycle l ON l.memory_id = m.id "
                        "WHERE m.owner=? AND m.character_id=? AND m.id=?",
                        (self.engine.owner, character_id, mid),
                    ).fetchone()
                    if not r:
                        rows_info.append({"id": mid, "missing": True})
                    else:
                        try:
                            lc = json.loads(r["lc"] or "{}")
                        except Exception:
                            lc = {}
                        rows_info.append(
                            {
                                "id": r["id"],
                                "revision": int(r["revision"]),
                                "key": str(r["key"]),
                                "text": str(r["text"]),
                                "active": not bool(lc.get("invalidation")),
                            }
                        )
                return _sha256_json({"kind": "memory_merge", "memories": rows_info})

            if kind == "style_adjustment":
                target_key = str(payload.get("key") or "").strip().lower()
                rows = self.engine.db.execute(
                    "SELECT m.id, m.revision, m.key, m.text, coalesce(l.data, '{}') AS lc "
                    "FROM memories m LEFT JOIN fact_lifecycle l ON l.memory_id = m.id "
                    "WHERE m.owner=? AND m.character_id=? AND m.category='style' AND lower(m.key)=?",
                    (self.engine.owner, character_id, target_key),
                ).fetchall()
                style_tuples = []
                for r in rows:
                    try:
                        lc = json.loads(r["lc"] or "{}")
                    except Exception:
                        lc = {}
                    if not lc.get("invalidation"):
                        style_tuples.append((r["id"], int(r["revision"]), str(r["text"])))
                return _compute_style_key_baseline_hash(target_key, style_tuples)
        return None

    def _serialize_row(self, row, *, existing_asr_corrections: dict[str, str] | None = None) -> dict[str, Any]:
        d = dict(row)
        payload = json.loads(d["payload_json"])
        evidence = json.loads(d["evidence_json"])
        applied = json.loads(d["applied_result_json"]) if d.get("applied_result_json") else None
        baseline_ok: bool | None = None
        if d["status"] == "pending":
            live_hash = self._compute_live_baseline(
                d["character_id"],
                d["kind"],
                payload,
                existing_asr_corrections=existing_asr_corrections,
            )
            if live_hash is not None:
                baseline_ok = live_hash == d["baseline_hash"]
        return {
            "id": d["id"],
            "character_id": d["character_id"],
            "kind": d["kind"],
            "status": d["status"],
            "title": d["title"],
            "summary": d["summary"],
            "payload": payload,
            "diff_preview": d["diff_preview"],
            "evidence": evidence,
            "baseline_hash": d["baseline_hash"],
            "baseline_ok": baseline_ok,
            "created_at": _fmt_iso(d["created_at"]),
            "created_ts": float(d["created_at"]),
            "reviewed_at": _fmt_iso(d["reviewed_at"]) if d.get("reviewed_at") else "",
            "review_note": str(d.get("review_note") or ""),
            "applied_result": applied,
        }

    def run_dream_cycle(
        self,
        character_id: str,
        *,
        reason: str = "manual",
        existing_asr_corrections: dict[str, str] | None = None,
        known_terms: list[str] | None = None,
        skip_if_no_new_evidence: bool = False,
    ) -> dict[str, Any]:
        """Run a read-only reflection pass and store new pending proposals (zero auto-publish)."""
        with self._lock:
            meta = self._ensure_meta_row(character_id)
            with self.engine._lock:
                cur_ev_count = int(
                    self.engine.db.execute(
                        "SELECT count(*) FROM evidence e JOIN turns t ON t.id=e.turn_id "
                        "WHERE e.owner=? AND e.character_id=? AND e.speaker='user' AND t.status='committed'",
                        (self.engine.owner, character_id),
                    ).fetchone()[0]
                )
            if skip_if_no_new_evidence and cur_ev_count == int(meta["last_user_evidence_count"] or 0):
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "no_new_user_evidence",
                    "created": 0,
                    **self.status(character_id, existing_asr_corrections=existing_asr_corrections),
                }

            eff_asr = existing_asr_corrections
            if eff_asr is None:
                raw_known = str(meta.get("known_asr_json") or "").strip()
                if raw_known:
                    try:
                        parsed_known = json.loads(raw_known)
                        if isinstance(parsed_known, dict):
                            eff_asr = parsed_known
                    except Exception:
                        eff_asr = None
            else:
                norm_known = self._normalize_asr_corrections(eff_asr)
                with self.engine._transaction():
                    self.engine.db.execute(
                        "UPDATE xiaozhi_dream_meta SET known_asr_json=? WHERE owner=? AND character_id=?",
                        (json.dumps(norm_known, ensure_ascii=False, sort_keys=True), self.engine.owner, character_id),
                    )
                eff_asr = norm_known

            analyzer = ReadOnlyDreamAnalyzer(self.engine.database, self.engine.owner)
            analysis = analyzer.analyze(
                character_id,
                existing_asr_corrections=eff_asr,
                known_terms=known_terms,
            )

            now = time.time()
            created = 0
            stale_marked = 0
            with self.engine._transaction():
                # Mark drifted pending proposals as stale before inserting fresh ones
                pending_rows = self.engine.db.execute(
                    "SELECT * FROM xiaozhi_dream_proposals WHERE owner=? AND character_id=? AND status='pending'",
                    (self.engine.owner, character_id),
                ).fetchall()
                for prow in pending_rows:
                    ppayload = json.loads(prow["payload_json"])
                    live_h = self._compute_live_baseline(
                        character_id,
                        prow["kind"],
                        ppayload,
                        existing_asr_corrections=eff_asr,
                    )
                    if live_h is not None and live_h != prow["baseline_hash"]:
                        self.engine.db.execute(
                            "UPDATE xiaozhi_dream_proposals SET status='stale', reviewed_at=?, "
                            "review_note='baseline_drifted_before_approval' WHERE id=?",
                            (now, prow["id"]),
                        )
                        stale_marked += 1

                for prop in analysis["proposals"]:
                    dup = self.engine.db.execute(
                        "SELECT id, status, baseline_hash FROM xiaozhi_dream_proposals "
                        "WHERE owner=? AND character_id=? AND dedup_key=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (self.engine.owner, character_id, prop["dedup_key"]),
                    ).fetchone()
                    if dup and dup["status"] in ("pending", "approved", "rejected"):
                        if dup["status"] in ("approved", "rejected") or dup["baseline_hash"] == prop["baseline_hash"]:
                            continue
                    pid = str(uuid4())
                    self.engine.db.execute(
                        "INSERT INTO xiaozhi_dream_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            pid,
                            self.engine.owner,
                            character_id,
                            prop["kind"],
                            "pending",
                            prop["title"],
                            prop["summary"],
                            json.dumps(prop["payload"], ensure_ascii=False),
                            prop["diff_preview"],
                            json.dumps(prop["evidence"], ensure_ascii=False),
                            prop["baseline_hash"],
                            prop["dedup_key"],
                            now,
                            None,
                            "",
                            None,
                        ),
                    )
                    created += 1

                self.engine.db.execute(
                    "UPDATE xiaozhi_dream_meta SET last_run_at=?, last_run_reason=?, "
                    "last_user_evidence_count=? WHERE owner=? AND character_id=?",
                    (now, str(reason or "manual")[:64], cur_ev_count, self.engine.owner, character_id),
                )

            return {
                "ok": True,
                "skipped": False,
                "scanned_user_evidence": int(analysis.get("scanned_user_evidence") or 0),
                "created": created,
                "stale_marked": stale_marked,
                **self.status(character_id, existing_asr_corrections=existing_asr_corrections),
            }

    def status(
        self,
        character_id: str,
        *,
        status_filter: str | None = None,
        limit: int = 30,
        existing_asr_corrections: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        cfg = self.get_config(character_id)
        lim = max(1, min(100, int(limit)))
        with self.engine._lock:
            counts_rows = self.engine.db.execute(
                "SELECT status, count(*) FROM xiaozhi_dream_proposals "
                "WHERE owner=? AND character_id=? GROUP BY status",
                (self.engine.owner, character_id),
            ).fetchall()
            counts = {str(r[0]): int(r[1]) for r in counts_rows}
            if status_filter and status_filter in VALID_PROPOSAL_STATUSES:
                rows = self.engine.db.execute(
                    "SELECT * FROM xiaozhi_dream_proposals "
                    "WHERE owner=? AND character_id=? AND status=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (self.engine.owner, character_id, status_filter, lim),
                ).fetchall()
            else:
                rows = self.engine.db.execute(
                    "SELECT * FROM xiaozhi_dream_proposals "
                    "WHERE owner=? AND character_id=? "
                    "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC LIMIT ?",
                    (self.engine.owner, character_id, lim),
                ).fetchall()

        proposals = [
            self._serialize_row(r, existing_asr_corrections=existing_asr_corrections) for r in rows
        ]
        return {
            "character_id": character_id,
            "config": cfg,
            "pending_count": int(counts.get("pending", 0)),
            "approved_count": int(counts.get("approved", 0)),
            "rejected_count": int(counts.get("rejected", 0)),
            "stale_count": int(counts.get("stale", 0)),
            "counts": counts,
            "proposals": proposals,
            "readonly_boundary": True,
            "auto_publish": False,
        }

    def get_proposal(self, character_id: str, proposal_id: str) -> dict[str, Any] | None:
        with self.engine._lock:
            row = self.engine.db.execute(
                "SELECT * FROM xiaozhi_dream_proposals WHERE owner=? AND character_id=? AND id=?",
                (self.engine.owner, character_id, proposal_id),
            ).fetchone()
        return self._serialize_row(row) if row else None

    def approve_proposal(
        self,
        character_id: str,
        proposal_id: str,
        *,
        expected_baseline_hash: str | None = None,
        note: str = "",
        external_applied: bool = False,
        external_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Verify baseline (CAS) and apply a human-approved Dream proposal."""
        with self._lock:
            with self.engine._lock:
                row = self.engine.db.execute(
                    "SELECT * FROM xiaozhi_dream_proposals WHERE owner=? AND character_id=? AND id=?",
                    (self.engine.owner, character_id, proposal_id),
                ).fetchone()
            if not row:
                raise KeyError(f"Proposal not found: {proposal_id}")
            if row["status"] != "pending":
                raise DreamConflictError(
                    f"Proposal is no longer pending (current status: {row['status']})",
                    expected_baseline_hash=row["baseline_hash"],
                )

            kind = str(row["kind"])
            payload = json.loads(row["payload_json"])
            stored_baseline = str(row["baseline_hash"])
            if expected_baseline_hash is not None and str(expected_baseline_hash).strip() != stored_baseline:
                raise DreamConflictError(
                    "Baseline hash mismatch: proposal has changed or stale client view",
                    expected_baseline_hash=str(expected_baseline_hash).strip(),
                    current_baseline_hash=stored_baseline,
                )

            now = time.time()
            applied_result: dict[str, Any] = {}
            clean_note = str(note or "").strip()[:240]

            if kind == "asr_correction":
                if not external_applied:
                    raise ValueError(
                        "asr_correction proposal must be committed to config/runtime/asr_corrections.json by console with external_applied=true"
                    )
                applied_result = dict(external_result or {"applied_from": payload.get("from"), "applied_to": payload.get("to")})
                with self.engine._transaction():
                    self.engine.db.execute(
                        "UPDATE xiaozhi_dream_proposals SET status='approved', reviewed_at=?, "
                        "review_note=?, applied_result_json=? WHERE id=?",
                        (now, clean_note, json.dumps(applied_result, ensure_ascii=False), proposal_id),
                    )
                    # Keep cached known_asr_json aligned so subsequent scheduled runs do not re-propose
                    m_row = self.engine.db.execute(
                        "SELECT known_asr_json FROM xiaozhi_dream_meta WHERE owner=? AND character_id=?",
                        (self.engine.owner, character_id),
                    ).fetchone()
                    if m_row is not None:
                        try:
                            known_asr = json.loads(m_row["known_asr_json"] or "{}")
                            if not isinstance(known_asr, dict):
                                known_asr = {}
                        except Exception:
                            known_asr = {}
                        wrong_k = str(payload.get("from") or "").strip()
                        right_v = str(payload.get("to") or "").strip()
                        if wrong_k and right_v:
                            known_asr[wrong_k] = right_v
                            self.engine.db.execute(
                                "UPDATE xiaozhi_dream_meta SET known_asr_json=? WHERE owner=? AND character_id=?",
                                (json.dumps(known_asr, ensure_ascii=False, sort_keys=True), self.engine.owner, character_id),
                            )
            elif kind == "memory_merge":
                live_hash = self._compute_live_baseline(character_id, kind, payload)
                if live_hash != stored_baseline:
                    with self.engine._transaction():
                        self.engine.db.execute(
                            "UPDATE xiaozhi_dream_proposals SET status='stale', reviewed_at=?, "
                            "review_note='baseline_mismatch_on_approve' WHERE id=?",
                            (now, proposal_id),
                        )
                    raise DreamConflictError(
                        "Underlying memories changed since proposal creation; proposal marked stale",
                        expected_baseline_hash=stored_baseline,
                        current_baseline_hash=str(live_hash or ""),
                    )
                keep_id = str(payload["keep_memory_id"])
                retired_ids: list[str] = []
                with self.engine._transaction():
                    keep_row = self.engine._get_memory(character_id, keep_id)
                    keep_mem = self.engine._memory(keep_row)
                    for rm in payload.get("retire_memories") or []:
                        mid = str(rm["id"])
                        exp_rev = int(rm["revision"])
                        cur_m = self.engine._get_memory(character_id, mid)
                        if int(cur_m["revision"]) != exp_rev:
                            raise Conflict(f"Revision conflict on memory {mid}")
                        old_mem = self.engine._memory(cur_m)
                        life = old_mem.lifecycle.model_copy(
                            update={
                                "invalidation": "superseded",
                                "invalidated_at": now,
                                "valid_until": keep_mem.details.valid_from,
                                "superseded_by": keep_id,
                            }
                        )
                        self.engine.db.execute(
                            "INSERT OR REPLACE INTO fact_lifecycle VALUES(?,?)",
                            (mid, life.model_dump_json()),
                        )
                        self.engine.db.execute(
                            "UPDATE memories SET revision=revision+1, updated_at=? "
                            "WHERE owner=? AND character_id=? AND id=?",
                            (now, self.engine.owner, character_id, mid),
                        )
                        self.engine._knowledge_update(character_id, cur_m, "superseded")
                        self.engine._invalidate_documents(character_id, cur_m["source_turn_id"])
                        retired_ids.append(mid)
                    applied_result = {"keep_memory_id": keep_id, "retired_memory_ids": retired_ids}
                    self.engine.db.execute(
                        "UPDATE xiaozhi_dream_proposals SET status='approved', reviewed_at=?, "
                        "review_note=?, applied_result_json=? WHERE id=?",
                        (now, clean_note, json.dumps(applied_result, ensure_ascii=False), proposal_id),
                    )
            elif kind == "style_adjustment":
                live_hash = self._compute_live_baseline(character_id, kind, payload)
                if live_hash != stored_baseline:
                    with self.engine._transaction():
                        self.engine.db.execute(
                            "UPDATE xiaozhi_dream_proposals SET status='stale', reviewed_at=?, "
                            "review_note='baseline_mismatch_on_approve' WHERE id=?",
                            (now, proposal_id),
                        )
                    raise DreamConflictError(
                        "Existing style memories changed since proposal creation; proposal marked stale",
                        expected_baseline_hash=stored_baseline,
                        current_baseline_hash=str(live_hash or ""),
                    )
                safe_quote = _sanitize_evidence_excerpt(
                    str(payload.get("source_quote") or payload["text"]), max_len=300
                )
                draft = MemoryDraft(
                    key=str(payload["key"])[:64],
                    text=str(payload["text"])[:400],
                    category="style",
                    source_quote=safe_quote,
                    details=MemoryDetails(
                        subject="user",
                        temporal_kind="current",
                        importance=7,
                    ),
                )
                with self.engine._transaction():
                    saved_mem = self.engine.remember(character_id, draft)
                    applied_result = {"memory_id": saved_mem.id, "key": saved_mem.key, "category": "style"}
                    self.engine.db.execute(
                        "UPDATE xiaozhi_dream_proposals SET status='approved', reviewed_at=?, "
                        "review_note=?, applied_result_json=? WHERE id=?",
                        (now, clean_note, json.dumps(applied_result, ensure_ascii=False), proposal_id),
                    )
            else:
                raise ValueError(f"Unsupported proposal kind: {kind}")

            updated = self.get_proposal(character_id, proposal_id)
            return {"ok": True, "proposal": updated, "applied_result": applied_result}

    def reject_proposal(
        self,
        character_id: str,
        proposal_id: str,
        *,
        note: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            with self.engine._lock:
                row = self.engine.db.execute(
                    "SELECT id, status FROM xiaozhi_dream_proposals WHERE owner=? AND character_id=? AND id=?",
                    (self.engine.owner, character_id, proposal_id),
                ).fetchone()
            if not row:
                raise KeyError(f"Proposal not found: {proposal_id}")
            if row["status"] != "pending":
                raise DreamConflictError(
                    f"Proposal is no longer pending (current status: {row['status']})"
                )
            with self.engine._transaction():
                self.engine.db.execute(
                    "UPDATE xiaozhi_dream_proposals SET status='rejected', reviewed_at=?, review_note=? WHERE id=?",
                    (time.time(), str(note or "").strip()[:240], proposal_id),
                )
            return {"ok": True, "proposal": self.get_proposal(character_id, proposal_id)}

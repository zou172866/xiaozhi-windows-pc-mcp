#!/usr/bin/env python3
"""Unified Anima Brain + Companion Studio + XiaoZhi Fast Streaming Bridge.

- Serves the official Companion Studio React UI on port 18787
- Persists brain.sqlite3 and studio_token.txt in /data/anima
- Exposes ultra-low-latency /xiaozhi/recall (<15ms local SQLite/FTS5/PAD read)
  and asynchronous /xiaozhi/ingest (grounded fact/correction/emotion commit)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse

from companion_engine import BrainConfig, CompanionBrain, Settings
from companion_engine import emotion
from companion_engine.automation import MemoryAutomation
from companion_engine.engine import Conflict
from companion_engine.http_server import create_app
from companion_engine.knowledge import eligible
from companion_engine.models import (
    Appraisal,
    Attunement,
    CharacterSpec,
    CommitRequest,
    MemoryCorrection,
    MemoryDraft,
)
from companion_engine.temporal import local_today

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [anima-bridge] %(message)s",
)
log = logging.getLogger("anima-bridge")

PORT = int(os.environ.get("ANIMA_PORT", "18787"))
DATA_DIR = Path(os.environ.get("ANIMA_DATA_DIR", "/data/anima"))
DB_PATH = DATA_DIR / "brain.sqlite3"
TOKEN_PATH = DATA_DIR / "studio_token.txt"
URL_PATH = DATA_DIR / "studio_url.txt"
FRONTEND_DIR = Path("/app/examples/showcase/dist")
DEFAULT_CHARACTER_ID = os.environ.get("ANIMA_DEFAULT_CHARACTER", "xiaozhi")
LAN_IP = os.environ.get("ANIMA_LAN_IP", "192.168.31.200").strip()

DEFAULT_PERSONA = (
    "你是小智，住在桌面小屏幕音箱里的贴心 AI 伙伴。说话自然、真诚、简短有温度，"
    "像认识很久的老朋友一样口语交流，每次语音回答尽量控制在一到三句话以内，"
    "不要输出 Markdown 符号或列表格式。你会自然记住对方提过的生活细节、喜好与未完待续的事，"
    "也会带着真实的相处情绪余温回应，不说生硬客服套话。"
)

EXTRACTION_SYSTEM_PROMPT = """You extract grounded memory updates and character emotional appraisal from a completed voice conversation turn.
Return ONLY a valid JSON object with keys:
{
  "memories": [
    {
      "key": "short_snake_case_topic_key",
      "text": "Clear concise durable fact in Chinese about the user, preference, boundary, style, date, or unresolved user event",
      "category": "identity|preference|relationship|event|goal|boundary|style|emotional|date",
      "source_quote": "EXACT verbatim substring copied from user_message",
      "details": {
        "subject": "user",
        "temporal_kind": "current|historical|event|uncertain",
        "importance": 5,
        "confidence": 0.95,
        "valence": 0.0,
        "arousal": 0.2,
        "loop_status": "none|open|resolved",
        "followup": "ask|background_only|never_unless_asked"
      }
    }
  ],
  "corrections": [
    {
      "memory_id": "id from existing_facts",
      "expected_revision": 1,
      "kind": "corrected|superseded",
      "draft": { ...same structure as a memory item, keeping the SAME key as the existing fact... }
    }
  ],
  "appraisal": {
    "emotion": "calm|warm|happy|curious|hurt|sad|guarded|playful|tender|flustered|annoyed|anxious",
    "intensity": 1,
    "trust": 0,
    "warmth": 0,
    "tension": 0,
    "disclosure": 0,
    "romance": 0,
    "source_quote": "EXACT verbatim substring copied from user_message"
  },
  "attunement": {
    "intent": "listen|practical|answer|repair|space|playful|uncertain",
    "user_cue": "Brief note on user's current tone/need in Chinese",
    "source_quote": "EXACT verbatim substring copied from user_message",
    "uncertainty": "",
    "avoid": []
  }
}

Strict rules:
1. Every `source_quote` MUST be an EXACT character-for-character substring of `user_message`. Never paraphrase quotes!
2. Only extract durable, meaningful user facts/preferences/plans/open-loops. For casual greetings or filler chat with no new durable fact, set `"memories": []` and `"corrections": []`.
3. If the user updates or contradicts an item in `existing_facts`, put it in `corrections` using that item's `id`, `revision`, and exact `key` (`"superseded"` if state changed over time, `"corrected"` if old fact was wrong).
4. If the user mentions an upcoming unresolved personal event (e.g. exam, trip, interview, task tomorrow), set `details.loop_status = "open"` and `details.followup = "ask"`. If the user later says an `open` event is finished/past or no longer bothering them, update it via `corrections` with `details.loop_status = "resolved"` and `details.followup = "background_only"`.
5. `trust`, `warmth`, `tension`, `disclosure` deltas are integers in -3..3 (`trust`/`warmth`: 0 or 1 for friendly chat; `disclosure`: 1 whenever the user shares personal feelings, daily life, stories, or fondness); `romance` must be 0 unless romance is enabled.
"""


def load_or_create_token() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.is_file():
        existing = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(existing) >= 24:
            return existing
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


def install_gemini_embedding_compat_patch() -> None:
    """Gemini's OpenAI-compatible /embeddings omits per-item `index` keys while preserving input order.
    Patch companion_engine.media.Embeddings.encode so indexing, semantic recall, and reconciliation work cleanly."""
    from companion_engine import media as _media_mod
    from companion_engine.providers import ProviderError

    if getattr(_media_mod.Embeddings, "_gemini_index_patched", False):
        return

    def _compat_encode(self, texts):
        response = _media_mod.post(
            self.base + "/embeddings",
            key=self.key,
            transport=self.transport,
            json={"model": self.model, "input": texts, "encoding_format": "float"},
        )
        try:
            data = response.json()
            raw_rows = data["data"]
            if not isinstance(raw_rows, list) or len(raw_rows) != len(texts):
                raise ValueError("Invalid embedding count")
            if all(isinstance(r, dict) and "index" in r for r in raw_rows):
                rows = sorted(raw_rows, key=lambda r: r["index"])
                if [r["index"] for r in rows] != list(range(len(texts))):
                    raise ValueError("Invalid embedding indices")
            else:
                rows = raw_rows
            return {
                "vectors": [_media_mod.checked_vector(r["embedding"]) for r in rows],
                "usage": data.get("usage") or {"billing": "unknown"},
            }
        except (ValueError, KeyError, TypeError):
            raise ProviderError("Invalid embedding envelope; billing may have occurred") from None

    _media_mod.Embeddings.encode = _compat_encode
    _media_mod.Embeddings._gemini_index_patched = True
    log.info("Applied Gemini OpenAI-compatible /embeddings index compatibility patch")


def ensure_default_character(brain: CompanionBrain) -> None:
    existing_ids = {c.id for c in brain.characters()}
    if DEFAULT_CHARACTER_ID not in existing_ids:
        brain.create_character(
            character_id=DEFAULT_CHARACTER_ID,
            name="小智",
            persona=DEFAULT_PERSONA,
            memory_policy="automatic",
            relationship_enabled=True,
            romance_enabled=False,
            timezone="Asia/Shanghai",
        )
        log.info("Created default Anima Brain character: %s (小智)", DEFAULT_CHARACTER_ID)


def clear_stale_review_jobs(brain: CompanionBrain, max_age_seconds: int = 600) -> int:
    """Auto-clear transient failed automation jobs stuck in 'review' so summaries/indexing never stall forever."""
    engine = brain.engine
    cutoff = time.time() - max_age_seconds
    cleared = 0
    try:
        with engine._lock:
            rows = engine.db.execute(
                "SELECT id, character_id, kind, error, created FROM memory_jobs "
                "WHERE owner=? AND state='review' AND (created IS NULL OR created < ?)",
                (engine.owner, cutoff),
            ).fetchall()
            if rows:
                with engine._transaction():
                    for r in rows:
                        engine.db.execute("DELETE FROM memory_jobs WHERE id=?", (r["id"],))
                        cleared += 1
                log.info("Auto-cleared %d stale review memory_job(s) so background indexing/summaries can resume", cleared)
    except Exception as exc:
        log.warning("clear_stale_review_jobs skipped: %s", exc)
    return cleared


def _fmt_mem_date(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except Exception:
        return ""


def build_fast_recall_context(brain: CompanionBrain, character_id: str, query: str) -> dict[str, Any]:
    """Read-only sub-15ms recall from SQLite/FTS5/PAD without reserving a pending turn."""
    engine = brain.engine
    query = (query or "").strip()[:4000]
    now = time.time()

    with engine._lock:
        try:
            row = engine._character(character_id)
        except Exception:
            ensure_default_character(brain)
            row = engine._character(DEFAULT_CHARACTER_ID)
            character_id = DEFAULT_CHARACTER_ID

        spec = CharacterSpec.model_validate_json(row["spec"])
        today = local_today(engine, character_id)

        # 1. Decayed emotional & relationship state (read-only preview)
        rel_note = None
        mood_summary = None
        if spec.relationship_enabled:
            state = emotion.decayed(
                json.loads(row["state"]), now, f"{engine.owner}:{character_id}"
            )
            rel_note = emotion.note(state, now, romance_enabled=spec.romance_enabled)
            fam_band = max(0, min(3, int((state.get("bands") or {}).get("familiarity", 0))))
            stage_label = ("stranger", "acquaintance", "familiar", "close")[fam_band]
            mood_label = state.get("last_emotion") or ""
            if not mood_label:
                last_app_row = engine.db.execute(
                    "SELECT p.data, t.created_at FROM turn_appraisals p "
                    "JOIN turns t ON t.id = p.turn_id "
                    "WHERE t.owner=? AND t.character_id=? AND t.status='committed' "
                    "ORDER BY t.created_at DESC, t.rowid DESC LIMIT 1",
                    (engine.owner, character_id),
                ).fetchone()
                if last_app_row and (now - float(last_app_row["created_at"] or 0.0)) <= 6 * 3600:
                    try:
                        mood_label = str(json.loads(last_app_row["data"]).get("emotion") or "")
                    except Exception:
                        mood_label = ""
            if not mood_label:
                pad_p = float((state.get("pad") or [0.0])[0])
                warmth_band = int((state.get("bands") or {}).get("warmth", 0))
                mood_label = "warm" if (warmth_band >= 2 and pad_p >= 0.35) else "calm"
            mood_summary = f"stage={stage_label}, last_emotion={mood_label}"

        # 2. Pinned boundaries/style + layered retrieval + lexical FTS5 search
        pinned = [
            engine._memory(r)
            for r in engine.db.execute(
                "SELECT * FROM memories WHERE owner=? AND character_id=? "
                "AND category IN ('boundary','style') ORDER BY updated_at DESC LIMIT 8",
                (engine.owner, character_id),
            ).fetchall()
        ]
        recent_facts = [
            engine._memory(r)
            for r in engine.db.execute(
                "SELECT * FROM memories WHERE owner=? AND character_id=? "
                "ORDER BY updated_at DESC LIMIT 10",
                (engine.owner, character_id),
            ).fetchall()
        ]

        layered_docs = []
        layered_facts = []
        lexical_facts = []
        evidence_sources = []
        if query:
            try:
                from companion_engine.models import Memory as MemoryModel

                layers = engine.retrieval.search(character_id, query, limit=8)
                layered_facts = [MemoryModel.model_validate(m) for m in layers.get("facts", [])]
                layered_docs = layers.get("documents", [])
            except Exception as exc:
                log.debug("Layered search skipped: %s", exc)
            try:
                lexical_facts = engine.search(character_id, query, limit=8)
            except Exception as exc:
                log.debug("FTS search skipped: %s", exc)
            try:
                evidence_sources = engine.memory.recall(character_id, query, limit=4).get("sources", [])
            except Exception as exc:
                log.debug("Evidence recall skipped: %s", exc)

        # Deduplicate & filter eligible active facts
        seen_ids: set[str] = set()
        active_facts = []
        for m in [*pinned, *layered_facts, *lexical_facts, *recent_facts]:
            if m.id in seen_ids:
                continue
            if not eligible(m, today=today):
                continue
            seen_ids.add(m.id)
            active_facts.append(m)
            if len(active_facts) >= 14:
                break

        # 3. Core & episode notebooks
        all_docs = engine.memory.documents(character_id)
        core_docs = [d for d in all_docs if d["kind"] in ("core", "character_core")]
        episode_docs = [d for d in layered_docs if d["kind"] == "episode"][:2]
        if not episode_docs:
            episode_docs = [d for d in all_docs if d["kind"] == "episode"][:1]

        # 4. Open loops (unresolved user events to follow up naturally)
        open_loops = [m for m in engine.memory.loops(character_id)[:4] if eligible(m, today=today)]

        # 5. Recent committed cross-session turns (bridges Web UI chat <-> ESP32 hardware!)
        recent_rows = engine.db.execute(
            "SELECT user_message, assistant_reply, created_at FROM turns "
            "WHERE owner=? AND character_id=? AND status='committed' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 4",
            (engine.owner, character_id),
        ).fetchall()
        recent_turns = list(reversed([dict(r) for r in recent_rows]))

    # Format compact Chinese context block for XiaoZhi streaming LLM
    sections: list[str] = []
    sections.append(f"【角色身份设定（来自 Companion Studio）】\n姓名：{spec.name}\n设定：{spec.persona}")
    if spec.core_persona.strip():
        sections.append(f"【角色深层内核】\n{spec.core_persona.strip()}")

    if rel_note:
        sections.append(f"【当前情绪与关系连续性状态】\n{rel_note}")

    if core_docs:
        core_texts = "\n".join(d["text"].strip()[:1200] for d in core_docs if d.get("text"))
        if core_texts:
            sections.append(f"【长期核心人物笔记】\n{core_texts}")

    if active_facts:
        fact_lines = [
            f"- [{_fmt_mem_date(getattr(m, 'updated_at', None)) or '历史'} | {m.category}] {m.text}"
            for m in active_facts
        ]
        sections.append(
            "【已确认的关于对方的长期记忆（带记录日期，切勿把往日事项误说成今天）】\n"
            + "\n".join(fact_lines)
        )

    if open_loops:
        loop_lines = [
            f"- [{_fmt_mem_date(getattr(m, 'updated_at', None)) or '近期'}] {m.text}"
            for m in open_loops
        ]
        sections.append(
            "【未完待续事项（仅作背景知情，切勿每次打招呼都机械重复追问同一件旧事，除非对方主动聊起）】\n"
            + "\n".join(loop_lines)
        )

    if episode_docs:
        ep_lines = [d["text"].strip()[:500] for d in episode_docs if d.get("text")]
        if ep_lines:
            sections.append("【过往互动片段摘要】\n" + "\n".join(ep_lines))

    if evidence_sources:
        ev_lines = []
        prefers_gege = "哥哥" in (spec.persona or "")
        for s in evidence_sources[:4]:
            txt = (s.get("text") or "")[:180]
            if prefers_gege and s.get("speaker") != "user" and "舰长" in txt:
                continue
            ev_lines.append(f"- ({'用户原话' if s.get('speaker') == 'user' else '你曾回复'}) {txt}")
        if ev_lines:
            sections.append("【相关历史原话溯源】\n" + "\n".join(ev_lines))

    if recent_turns:
        rt_lines = []
        for t in recent_turns:
            u = (t.get("user_message") or "").strip()[:180]
            a = (t.get("assistant_reply") or "").strip()[:320]
            rt_lines.append(f"用户：{u}\n{spec.name}：{a}")
        sections.append("【最近跨端对话衔接（若当前在同一轮已包含则勿重复）】\n" + "\n---\n".join(rt_lines))

    formatted = "\n\n".join(sections)
    return {
        "character_id": character_id,
        "character_name": spec.name,
        "mood": mood_summary,
        "facts_count": len(active_facts),
        "open_loops_count": len(open_loops),
        "context_text": formatted,
    }


def _safe_key(raw_key: str, fallback_idx: int) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_:\-\u4e00-\u9fff]+", "_", (raw_key or "").strip().lower()).strip("_")
    if not cleaned:
        cleaned = f"fact_{int(time.time())}_{fallback_idx}"
    return cleaned[:100]


def extract_and_commit_turn(
    brain: CompanionBrain,
    character_id: str,
    request_id: str,
    user_message: str,
    assistant_reply: str,
) -> dict[str, Any]:
    """Synchronously run in worker thread: extract grounded facts/appraisal & commit turn."""
    engine = brain.engine
    runtime = brain.runtime
    user_message = (user_message or "").strip()
    assistant_reply = (assistant_reply or "").strip()
    if not user_message or not assistant_reply:
        return {"skipped": "empty_message"}

    request_id = re.sub(r"[^a-zA-Z0-9_\-:.]", "_", request_id)[:110] or f"xz-{int(time.time()*1000)}"

    with engine._lock:
        try:
            row = engine._character(character_id)
        except Exception:
            ensure_default_character(brain)
            character_id = DEFAULT_CHARACTER_ID
            row = engine._character(character_id)

        existing_turn = engine.db.execute(
            "SELECT id, status FROM turns WHERE owner=? AND character_id=? AND request_id=?",
            (engine.owner, character_id, request_id),
        ).fetchone()
        if existing_turn and existing_turn["status"] == "committed":
            return {"committed": True, "replayed": True, "turn_id": existing_turn["id"]}

        spec = CharacterSpec.model_validate_json(row["spec"])
        today = local_today(engine, character_id)
        current_memories = [
            m for m in engine.memories(character_id, limit=100) if eligible(m, today=today)
        ]
        by_key = {m.key: m for m in current_memories}
        by_id = {m.id: m for m in current_memories}

    # Run LLM extraction outside SQLite lock so Companion Studio UI stays snappy!
    memories_out: list[MemoryDraft] = []
    corrections_out: list[MemoryCorrection] = []
    appraisal_out: Appraisal | None = None
    attunement_out: Attunement | None = None

    should_extract = spec.memory_policy != "off" or spec.relationship_enabled
    if should_extract and runtime.settings.base_url and runtime.settings.model:
        try:
            provider = runtime.provider()
            envelope = json.dumps(
                {
                    "current_date": today.isoformat(),
                    "character_name": spec.name,
                    "relationship_enabled": spec.relationship_enabled,
                    "romance_enabled": spec.romance_enabled,
                    "memory_policy": spec.memory_policy,
                    "user_message": user_message,
                    "assistant_reply": assistant_reply,
                    "existing_facts": [
                        {
                            "id": m.id,
                            "key": m.key,
                            "revision": m.revision,
                            "category": m.category,
                            "text": m.text,
                        }
                        for m in current_memories[:60]
                    ],
                },
                ensure_ascii=False,
            )
            completion = provider.structured(EXTRACTION_SYSTEM_PROMPT, envelope)
            parsed = json.loads(completion.text)
            fallback_quote = user_message[: min(len(user_message), 120)]

            if spec.memory_policy == "automatic":
                seen_keys: set[str] = set()
                seen_corr_ids: set[str] = set()

                for idx, raw_corr in enumerate(parsed.get("corrections") or []):
                    try:
                        mid = str(raw_corr.get("memory_id") or "")
                        if mid not in by_id or mid in seen_corr_ids:
                            continue
                        target_mem = by_id[mid]
                        raw_draft = dict(raw_corr.get("draft") or {})
                        quote = str(raw_draft.get("source_quote") or "").strip()
                        if not quote or quote not in user_message:
                            continue
                        raw_draft["key"] = target_mem.key
                        raw_draft["source_media_ref"] = None
                        if isinstance(raw_draft.get("details"), dict):
                            ce = str(raw_draft["details"].get("correction_evidence") or "")
                            if ce and ce not in quote:
                                raw_draft["details"]["correction_evidence"] = ""
                        draft = MemoryDraft.model_validate(raw_draft)
                        kind = raw_corr.get("kind") if raw_corr.get("kind") in ("corrected", "superseded") else "superseded"
                        corrections_out.append(
                            MemoryCorrection(
                                memory_id=target_mem.id,
                                expected_revision=target_mem.revision,
                                kind=kind,
                                draft=draft,
                            )
                        )
                        seen_corr_ids.add(mid)
                        seen_keys.add(target_mem.key)
                    except Exception as exc:
                        log.debug("Skipped invalid correction %s: %s", idx, exc)

                for idx, raw_mem in enumerate(parsed.get("memories") or []):
                    try:
                        raw_mem = dict(raw_mem)
                        quote = str(raw_mem.get("source_quote") or "").strip()
                        if not quote or quote not in user_message:
                            continue
                        k = _safe_key(str(raw_mem.get("key") or ""), idx)
                        if k in seen_keys:
                            continue
                        raw_mem["key"] = k
                        raw_mem["source_media_ref"] = None
                        if isinstance(raw_mem.get("details"), dict):
                            ce = str(raw_mem["details"].get("correction_evidence") or "")
                            if ce and ce not in quote:
                                raw_mem["details"]["correction_evidence"] = ""
                        draft = MemoryDraft.model_validate(raw_mem)
                        if k in by_key:
                            existing_mem = by_key[k]
                            if existing_mem.text != draft.text and existing_mem.id not in seen_corr_ids:
                                corrections_out.append(
                                    MemoryCorrection(
                                        memory_id=existing_mem.id,
                                        expected_revision=existing_mem.revision,
                                        kind="superseded",
                                        draft=draft,
                                    )
                                )
                                seen_corr_ids.add(existing_mem.id)
                                seen_keys.add(k)
                                continue
                        memories_out.append(draft)
                        seen_keys.add(k)
                    except Exception as exc:
                        log.debug("Skipped invalid memory draft %s: %s", idx, exc)

            if spec.relationship_enabled and isinstance(parsed.get("appraisal"), dict):
                try:
                    raw_app = dict(parsed["appraisal"])
                    q = str(raw_app.get("source_quote") or "").strip()
                    raw_app["source_quote"] = q if (q and q in user_message) else fallback_quote
                    if not spec.romance_enabled:
                        raw_app["romance"] = 0
                    for int_field in ("intensity", "trust", "warmth", "tension", "disclosure", "romance"):
                        if int_field in raw_app:
                            raw_app[int_field] = int(round(float(raw_app[int_field])))
                    appraisal_out = Appraisal.model_validate(raw_app)
                except Exception as exc:
                    log.debug("Skipped invalid appraisal: %s", exc)

            if isinstance(parsed.get("attunement"), dict):
                try:
                    raw_att = dict(parsed["attunement"])
                    q = str(raw_att.get("source_quote") or "").strip()
                    raw_att["source_quote"] = q if (q and q in user_message) else fallback_quote
                    attunement_out = Attunement.model_validate(raw_att)
                except Exception as exc:
                    log.debug("Skipped invalid attunement: %s", exc)

        except Exception as exc:
            log.warning("Extraction call failed (will still commit raw voice turn): %s", exc)

    # Wait briefly if Companion Studio web chat currently holds a pending turn
    for _ in range(25):
        pending = engine.pending_turn(character_id)
        if not pending or pending["request_id"] == request_id:
            break
        time.sleep(0.2)

    started = time.monotonic()
    with brain._lock:
        pending = engine.pending_turn(character_id)
        if pending and pending["request_id"] != request_id:
            return {"skipped": "another_turn_pending"}

        ctx = engine.prepare_turn(character_id, user_message, request_id=request_id)
        commit_req = CommitRequest(
            assistant_reply=assistant_reply,
            memories=memories_out[:12],
            corrections=corrections_out[:8],
            appraisal=appraisal_out,
            attunement=attunement_out,
        )
        try:
            committed = runtime.commit(character_id, ctx.turn_id, commit_req)
        except Exception as exc:
            log.warning("Rich commit failed (%s), falling back to clean transcript commit", exc)
            commit_req = CommitRequest(
                assistant_reply=assistant_reply,
                memories=[],
                corrections=[],
                appraisal=appraisal_out,
                attunement=attunement_out,
            )
            try:
                committed = runtime.commit(character_id, ctx.turn_id, commit_req)
            except Exception:
                commit_req = CommitRequest(assistant_reply=assistant_reply)
                committed = runtime.commit(character_id, ctx.turn_id, commit_req)

        # Save turn_drafts so Companion Studio's "Inspect Turn" works on ESP32 voice turns too
        draft_payload = {
            "turn_id": ctx.turn_id,
            "commit_request": commit_req.model_dump(mode="json"),
            "generation": {
                "model": f"xiaozhi-voice + {runtime.settings.model or 'gemini'}",
                "retrieval_mode": "xiaozhi_fast_hybrid",
                "memory_ids": [m.id for m in ctx.memories],
                "evidence_ids": [e["id"] for e in ctx.evidence],
                "document_ids": [d["id"] for d in ctx.core_memory],
                "recent_turns": len(ctx.recent_turns),
                "context_chars": len(ctx.model_dump_json()),
                "media": {"mode": "voice_esp32", "inputs": [], "model": "SenseVoiceSmall + EdgeTTS"},
                "elapsed_seconds": round(time.monotonic() - started, 3),
            },
            "context": ctx.model_dump(mode="json"),
            "committed": True,
        }
        with engine._transaction():
            engine.db.execute(
                "INSERT OR REPLACE INTO turn_drafts VALUES(?,?)",
                (ctx.turn_id, json.dumps(draft_payload, ensure_ascii=False)),
            )

    log.info(
        "Committed ESP32 voice turn %s: +%d memories, +%d corrections, emotion=%s",
        ctx.turn_id[:8],
        len(commit_req.memories),
        len(commit_req.corrections),
        commit_req.appraisal.emotion if commit_req.appraisal else "none",
    )
    return {
        "committed": True,
        "turn_id": ctx.turn_id,
        "memory_ids": committed.get("memory_ids", []),
    }


def create_combined_asgi_app(brain: CompanionBrain, token: str):
    studio_app = create_app(
        brain.runtime,
        token,
        port=PORT,
        include_mcp=True,
        start_worker=True,
        frontend_dir=FRONTEND_DIR,
        preview_mode=False,
    )

    allowed_hosts = {
        f"127.0.0.1:{PORT}",
        f"localhost:{PORT}",
        f"anima-brain:{PORT}",
    }
    if LAN_IP:
        allowed_hosts.add(f"{LAN_IP}:{PORT}")

    ingest_queue: asyncio.Queue[tuple[str, str, str, str]] = asyncio.Queue(maxsize=256)

    async def ingest_worker():
        while True:
            character_id, req_id, user_msg, reply_msg = await ingest_queue.get()
            try:
                await asyncio.to_thread(
                    extract_and_commit_turn,
                    brain,
                    character_id,
                    req_id,
                    user_msg,
                    reply_msg,
                )
            except Exception as exc:
                log.error("Background ingest error for %s: %s", req_id, exc)
            finally:
                ingest_queue.task_done()

    worker_task: asyncio.Task | None = None
    heal_task: asyncio.Task | None = None

    async def review_healer():
        while True:
            await asyncio.sleep(180)
            try:
                await asyncio.to_thread(clear_stale_review_jobs, brain, 180)
            except Exception:
                pass

    async def combined_app(scope, receive, send):
        nonlocal worker_task, heal_task
        if scope["type"] == "lifespan":
            async def wrapped_receive():
                msg = await receive()
                nonlocal worker_task, heal_task
                if msg["type"] == "lifespan.startup":
                    worker_task = asyncio.create_task(ingest_worker())
                    heal_task = asyncio.create_task(review_healer())
                elif msg["type"] == "lifespan.shutdown":
                    if worker_task:
                        worker_task.cancel()
                    if heal_task:
                        heal_task.cancel()
                return msg

            await studio_app(scope, wrapped_receive, send)
            return

        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            headers_list = list(scope.get("headers", []))
            headers_dict = {k.decode("latin1").lower(): v.decode("latin1") for k, v in headers_list}

            # Handle XiaoZhi fast bridge endpoints directly
            if scope["type"] == "http" and path.startswith("/xiaozhi/"):
                req = Request(scope, receive=receive)
                if path == "/xiaozhi/health":
                    resp = JSONResponse({"status": "ok", "character": DEFAULT_CHARACTER_ID})
                    await resp(scope, receive, send)
                    return

                auth = headers_dict.get("authorization", "")
                if not secrets.compare_digest(auth, f"Bearer {token}"):
                    resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
                    await resp(scope, receive, send)
                    return

                try:
                    body = await req.json()
                except Exception:
                    body = {}

                char_id = str(body.get("character_id") or DEFAULT_CHARACTER_ID).strip()

                if path == "/xiaozhi/recall" and req.method == "POST":
                    query = str(body.get("query") or "")
                    data = await asyncio.to_thread(build_fast_recall_context, brain, char_id, query)
                    resp = JSONResponse(data)
                    await resp(scope, receive, send)
                    return

                if path == "/xiaozhi/ingest" and req.method == "POST":
                    turns = body.get("turns")
                    if not isinstance(turns, list):
                        turns = [body]
                    enqueued = 0
                    for item in turns:
                        if not isinstance(item, dict):
                            continue
                        u_msg = str(item.get("user_message") or "").strip()
                        a_msg = str(item.get("assistant_reply") or "").strip()
                        r_id = str(item.get("request_id") or f"xz-{int(time.time()*1000)}").strip()
                        if u_msg and a_msg:
                            try:
                                ingest_queue.put_nowait((char_id, r_id, u_msg, a_msg))
                                enqueued += 1
                            except asyncio.QueueFull:
                                log.warning("Ingest queue full; dropping turn %s", r_id)
                    resp = JSONResponse({"queued": enqueued})
                    await resp(scope, receive, send)
                    return

                resp = JSONResponse({"error": "Not found"}, status_code=404)
                await resp(scope, receive, send)
                return

            # Normalize trusted LAN/Docker Host & Origin headers for Companion Studio UI
            host_val = headers_dict.get("host", "")
            if host_val in allowed_hosts and host_val not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
                new_headers = []
                for k, v in headers_list:
                    kl = k.lower()
                    if kl == b"host":
                        new_headers.append((b"host", f"localhost:{PORT}".encode("latin1")))
                    elif kl == b"origin" and v.decode("latin1") == f"http://{host_val}":
                        new_headers.append((b"origin", f"http://localhost:{PORT}".encode("latin1")))
                    else:
                        new_headers.append((k, v))
                scope = dict(scope, headers=new_headers)

        await studio_app(scope, receive, send)

    return combined_app


def main():
    install_gemini_embedding_compat_patch()
    token = load_or_create_token()
    studio_url = f"http://localhost:{PORT}/#token={token}"
    URL_PATH.write_text(studio_url + "\n", encoding="utf-8")

    settings = Settings.from_env()
    # Keep reader_enabled off by default for fast single-hop chat in Companion Studio too
    if "COMPANION_READER_ENABLED" not in os.environ:
        settings.reader_enabled = False

    automation = MemoryAutomation(
        embedding=bool(settings.embedding_base_url and settings.embedding_model),
        semantic_retrieval=bool(settings.embedding_base_url and settings.embedding_model),
        summaries=True,
        reconcile=True,
        summary_turns=12,
        summary_batch_messages=16,
        retain_recent_turns=8,
        idle_seconds=180,
    )

    with CompanionBrain(
        DB_PATH,
        user_id="local",
        settings=settings,
        config=BrainConfig(
            capture_context=True,
            start_worker=False,  # started once by create_app lifespan
            allow_chat_images=True,
            automation=automation,
            reply_instructions=(
                "默认使用自然、真诚、温暖的中文口语交流（除非对方用其他语言提问）。"
                "根据话题自然舒展地陪伴聊天或讲故事，不说生硬客服套话。"
            ),
        ),
    ) as brain:
        ensure_default_character(brain)
        clear_stale_review_jobs(brain)
        app = create_combined_asgi_app(brain, token)
        log.info("==========================================================")
        log.info("Anima Brain + Companion Studio is READY on port %d", PORT)
        log.info("Database: %s", DB_PATH)
        log.info("Studio URL: %s", studio_url)
        log.info("==========================================================")
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=PORT,
            access_log=False,
            ws_max_size=2 * 1024 * 1024,
        )


if __name__ == "__main__":
    main()

"""Application orchestration shared by SDK, HTTP API and optional MCP calls.

Host tools never require a reply-model key. Standalone chat does. Every paid
operation is journaled. Configured embeddings and host summaries default on for new companions;
uncertain provider outcomes are never automatically retried.
"""

import hashlib
import json
import os
import time
from uuid import uuid4

from pydantic import Field, SecretStr, TypeAdapter

from .context_policy import enforce_context_policy
from .engine import Conflict, Engine
from .generation import draft_reply
from .models import CommitRequest, Key, Memory, Model, Text, Usage
from .providers import CompatibleProvider, ProviderError


class Settings(Model):
    base_url: str = ""
    model: str = ""
    api_key: SecretStr = SecretStr("")
    vision_base_url: str = ""
    vision_model: str = ""
    vision_key: SecretStr = SecretStr("")
    vision_max_output_tokens: int = Field(default=1200, ge=128, le=8192)
    summary_base_url: str = ""
    summary_model: str = ""
    summary_key: SecretStr = SecretStr("")
    memory_max_output_tokens: int = Field(default=8192, ge=1024, le=32768)
    media_timeout_seconds: float = Field(default=120, ge=1, le=600)
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_key: SecretStr = SecretStr("")
    speech_provider: str = Field(default="elevenlabs", pattern="^(elevenlabs|minimax|doubao)$")
    speech_model: str = ""
    speech_key: SecretStr = SecretStr("")
    image_base_url: str = ""
    image_model: str = ""
    image_key: SecretStr = SecretStr("")
    transcription_base_url: str = ""
    transcription_model: str = ""
    transcription_key: SecretStr = SecretStr("")
    call_asr_key: SecretStr = SecretStr("")
    call_asr_resource: str = "volc.bigasr.sauc.duration"
    vidu_key: SecretStr = SecretStr("")
    vidu_base_url: str = "https://api.vidu.com"
    reader_enabled: bool = True
    reader_max_steps: int = Field(default=2, ge=0, le=2)
    max_output_tokens: int = Field(default=2048, ge=128, le=32768)
    timeout_seconds: float = Field(default=60, ge=1, le=600)
    temperature: float | None = Field(default=None, ge=0, le=2)

    @classmethod
    def from_env(cls):
        from .provider_config import load_provider_settings

        values = load_provider_settings()
        for name in cls.model_fields:
            raw = os.environ.get("COMPANION_" + name.upper())
            if raw is not None:
                values[name] = raw
        return cls(**values)

    def public(self):
        return {
            k: bool(v.get_secret_value()) if isinstance(v, SecretStr) else v
            for k, v in self.__dict__.items()
        }


class Runtime:
    def __init__(
        self,
        engine: Engine,
        settings: Settings | None = None,
        *,
        provider=None,
        transport=None,
        reply_instructions: str = "",
        capture_context: bool = False,
        allow_chat_images: bool = False,
        default_automation=None,
    ):
        self.engine = engine
        self.settings = settings or Settings.from_env()
        self._provider = provider
        self.transport = transport
        self.reply_instructions = reply_instructions
        self.capture_context = capture_context
        self.allow_chat_images = allow_chat_images
        self.media_dir = engine.database.parent / (engine.database.stem + "-media")
        from .chat_images import ChatImages

        self.chat_images = ChatImages(self)
        with engine._transaction():
            engine.db.execute(
                "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, owner TEXT "
                "NOT NULL, character_id TEXT NOT NULL, kind TEXT NOT NULL, request_id "
                "TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, result TEXT, "
                "usage TEXT, created_at REAL NOT NULL, "
                "UNIQUE(owner,character_id,kind,request_id), FOREIGN "
                "KEY(owner,character_id) REFERENCES characters(owner,id) ON DELETE "
                "CASCADE)"
            )

        from .automation import Automation, MemoryAutomation

        self.automation = Automation(self)
        self.default_automation = (
            MemoryAutomation.model_validate(default_automation).model_copy(deep=True)
            if default_automation is not None
            else None
        )
        from .host_memory import HostMemory

        self.host_memory = HostMemory(self)
        from .local_settings import LocalSettings

        self.local_settings = LocalSettings(self)
        from .calls import initialize

        initialize(self.engine)

    def create_character(self, spec):
        """Share application defaults with the SDK and HTTP API."""
        with self.engine._transaction():
            created = self.engine.create_character(spec)
            if self.default_automation is not None:
                self.automation.configure(created.id, self.default_automation)
        return created

    def prepare(self, character_id, message, request_id, media_observations=None):
        context = self.engine.prepare_turn(
            character_id, message, request_id=request_id, media_observations=media_observations
        )
        config = self.engine.memory_config
        if config is not None and (not config.read_enabled or config.context_chars == 0):
            return enforce_context_policy(self.engine, context)
        context.memory_maintenance = self.host_memory.notice(character_id)
        policy = self.automation.policy(character_id)
        context.continuity = {
            "policy": policy.model_dump(),
            "knowledge_update": "Compare this message with current facts. Use corrections "
            "for changed state (superseded) or errors (corrected), with exact current quotes "
            "and inspected revisions. Historical recollections are not current-state changes.",
            "per_turn": "Commit a brief attunement (intent, cue, uncertainty, avoid) based on "
            "the current message. When relationship_enabled, also submit the character's "
            "appraisal; neutral/zero deltas are valid. No extra model call is needed.",
            "current_facts": [],
        }
        # This inventory helps the host notice conflicting state even when wording changes.
        layers = self.engine.retrieval.search(character_id, message, **self._layer_limits())
        self._merge_layers(context, layers)
        relevant = [Memory.model_validate(m) for m in layers["facts"]]
        facts = list(
            {
                m.id: m
                for m in relevant
                + (self.engine.memories(character_id, limit=100) if config is None else [])
            }.values()
        )
        from .knowledge import eligible
        from .temporal import local_today

        today = local_today(self.engine, character_id)
        for fact in facts:
            if not eligible(fact, today=today):
                continue
            item = {
                k: fact.model_dump(mode="json")[k]
                for k in ("id", "key", "text", "revision", "details")
            }
            context.continuity["current_facts"].append(item)
            if len(context.model_dump_json()) > self.engine.context_chars:
                context.continuity["current_facts"].pop()
                break
        context.continuity["omitted_facts"] = max(
            0,
            self.engine.db.execute(
                "SELECT count(*) FROM memories WHERE owner=? AND character_id=?",
                (self.engine.owner, character_id),
            ).fetchone()[0]
            - len(context.continuity["current_facts"]),
        )
        context.continuity["knowledge_updates"] = [
            dict(row)
            for row in self.engine.db.execute(
                "SELECT memory_key,operation,source_id FROM knowledge_updates "
                "WHERE owner=? AND character_id=? ORDER BY created DESC LIMIT 40",
                (self.engine.owner, character_id),
            )
        ]
        context.continuity["invalidation_rule"] = (
            "Deleted claims must not be restored from transcripts; current facts override "
            "superseded/corrected claims. Use current_or_historical_facts to inspect changes."
        )
        context.continuity["pending_analysis"] = self.engine.continuity.backlog(
            character_id, limit=2
        )
        context.continuity["repair_instruction"] = (
            "Use complete_turn_analysis for missing records using ORIGINAL messages. "
            "Historical repair does not retroactively change character mood. "
            "For a proposed changed fact, use review_memory_update to find older matches."
        )
        while (
            context.continuity["pending_analysis"]
            and len(context.model_dump_json()) > self.engine.context_chars
        ):
            context.continuity["pending_analysis"].pop()
        enforce_context_policy(self.engine, context)
        # Preserve the engine's whole-entry budget even when optional guidance is large.
        for field in ("recent_turns", "evidence", "core_memory", "recent_attunement"):
            values = getattr(context, field)
            while values and len(context.model_dump_json()) > self.engine.context_chars:
                values.pop(0 if field == "recent_turns" else -1)
                if field == "recent_turns":
                    context.omitted_recent_turns += 1
        if len(context.model_dump_json()) > self.engine.context_chars:
            context.continuity = {"instruction": "Use get_companion_workflow for continuity steps."}
        if len(context.model_dump_json()) > self.engine.context_chars:
            raise ValueError("Continuity metadata exceeds context budget; increase context_chars")
        if policy.semantic_retrieval:
            if not self.settings.embedding_base_url or not self.settings.embedding_model:
                context.retrieval_mode = "lexical_embedding_provider_missing"
                return enforce_context_policy(self.engine, context)
            try:
                from .media import Embeddings

                embed = Embeddings(self.settings, self.transport)
                result = self.operation(
                    character_id,
                    "query_embedding",
                    "turn-" + context.turn_id,
                    {"query": message, "namespace": embed.namespace},
                    lambda: embed.encode([message]),
                )
                found = self.engine.memory.recall(
                    character_id,
                    message,
                    query_vector=result["vectors"][0],
                    model=embed.namespace,
                    **self._evidence_limits(),
                )
                self._merge_layers(
                    context,
                    self.engine.retrieval.search(
                        character_id,
                        message,
                        vector=result["vectors"][0],
                        model=embed.namespace,
                        **self._layer_limits(),
                    ),
                    prioritize=True,
                )
                context.retrieval_mode = found["mode"]
                context.evidence = []
                for source in found["sources"]:
                    context.evidence.append(source)
                    if len(context.model_dump_json()) > self.engine.context_chars:
                        context.evidence.pop()
            except (ValueError, ProviderError, Conflict):
                context.retrieval_mode = "lexical_semantic_unavailable"
        return enforce_context_policy(self.engine, context)

    def _evidence_limits(self):
        config = self.engine.memory_config
        return {"limit": max(1, config.evidence_limit)} if config else {}

    def _layer_limits(self):
        config = self.engine.memory_config
        return {"limit": max(1, config.fact_limit, config.document_limit)} if config else {}

    def _merge_layers(self, context, layers, *, prioritize=False):
        """Keep core/pinned content; add whole relevant items within the existing budget."""
        for field, values in [
            ("memories", [Memory.model_validate(m) for m in layers["facts"]]),
            ("core_memory", layers["documents"]),
        ]:
            items = getattr(context, field)
            if prioritize and self.engine.memory_config is not None:
                # Admit targeted follow-up results even when the initial lane is full.
                # Pinned boundaries/corrected state and core documents retain precedence.
                if field == "memories":
                    pinned = [
                        m for m in items if m.category in ("boundary", "style") or m.revision > 1
                    ]
                else:
                    pinned = [d for d in items if d["kind"] != "episode"]
                unique = {}
                for item in pinned + values + items:
                    key = item.id if field == "memories" else item["id"]
                    unique.setdefault(key, item)
                setattr(context, field, list(unique.values()))
                continue
            known = {m.id if field == "memories" else m["id"] for m in items}
            for item in values:
                key = item.id if field == "memories" else item["id"]
                if key in known:
                    continue
                # Recent irrelevant episodes should not crowd out an old matching episode.
                removed = None
                document_limit = (
                    self.engine.memory_config.document_limit if self.engine.memory_config else 8
                )
                if field == "core_memory" and len(items) >= document_limit:
                    remove = next(
                        (i for i in range(len(items) - 1, -1, -1) if items[i]["kind"] == "episode"),
                        None,
                    )
                    if remove is not None:
                        removed = (remove, items.pop(remove))
                items.append(item)
                if len(context.model_dump_json()) > self.engine.context_chars:
                    items.pop()
                    if removed is not None:
                        items.insert(removed[0], removed[1])
                else:
                    known.add(key)
        if prioritize:
            enforce_context_policy(self.engine, context)

    def commit(self, character_id, turn_id, request):
        request = CommitRequest.model_validate(request)
        result = self.engine.commit_turn(character_id, turn_id, request).model_dump()
        spec = json.loads(self.engine._character(character_id)["spec"])
        result["memory_maintenance"] = self.host_memory.notice(character_id)
        result["continuity_status"] = {
            "attunement_recorded": request.attunement is not None,
            "character_appraisal_recorded": request.appraisal is not None,
            "relationship_enabled": spec["relationship_enabled"],
            "instruction": "Use attunement and character appraisal on subsequent turns when "
            "enabled. If host summary is due, prepare_memory_summary then commit_memory_summary.",
        }
        result["continuity_status"]["pending_analysis"] = [
            {k: t[k] for k in ("id", "needs_attunement", "needs_appraisal")}
            for t in self.engine.continuity.backlog(character_id)
        ]
        return result

    def memory_panel(self, character_id, offset=0):
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("Invalid memory page offset")
        with self.engine._lock:
            character = self.engine._character(character_id)
            facts = self.engine.memories(character_id, limit=50, offset=offset)
            total = self.engine.db.execute(
                "SELECT count(*) FROM memories WHERE owner=? AND character_id=?",
                (self.engine.owner, character_id),
            ).fetchone()[0]
            return {
                "character_id": character_id,
                "name": json.loads(character["spec"])["name"],
                "facts": [m.model_dump(mode="json") for m in facts],
                "total": total,
                "offset": offset,
                "documents": self.engine.memory.documents(character_id),
                "automation": self.automation.status(character_id),
            }

    def capabilities(self):
        """Configuration readiness, not a provider connectivity test."""
        from .automation import MemoryAutomation
        from .calls import available

        s = self.settings
        return {
            **available(self),
            "chat": bool(self._provider or (s.base_url and s.model)),
            "vision": bool(s.vision_base_url and s.vision_model),
            "image_generation": bool(s.image_base_url and s.image_model),
            "chat_images": bool(self.allow_chat_images and s.image_base_url and s.image_model),
            "transcription": bool(s.transcription_base_url and s.transcription_model),
            "speech": bool(s.speech_model and s.speech_key.get_secret_value()),
            "embeddings": bool(s.embedding_base_url and s.embedding_model),
            "summaries": "summary_provider_missing"
            not in self.automation.availability(MemoryAutomation(summaries=True)),
        }

    def inspect_turn(self, character_id, turn_id):
        """Return saved provenance for this owner's character; no model call."""
        with self.engine._lock:
            turn = self.engine._turn(character_id, turn_id)
            row = self.engine.db.execute(
                "SELECT payload FROM turn_drafts WHERE turn_id=?", (turn_id,)
            ).fetchone()
            draft = json.loads(row[0]) if row else {}
            return {
                "turn_id": turn_id,
                "status": turn["status"],
                "user_message": turn["user_message"],
                "reply": turn["assistant_reply"]
                or draft.get("commit_request", {}).get("assistant_reply", ""),
                "generation": draft.get("generation"),
                "context": draft.get("context"),
                "image_result": draft.get("image_result"),
                "proposals": {
                    key: draft.get("commit_request", {}).get(key, [])
                    for key in ("memories", "corrections")
                },
            }

    def provider(self):
        if self._provider:
            return self._provider
        if not self.settings.base_url or not self.settings.model:
            raise ValueError(
                "Configure a chat endpoint and model for standalone chat; MCP host mode "
                "needs no chat key"
            )
        return CompatibleProvider(
            base_url=self.settings.base_url,
            model=self.settings.model,
            api_key=self.settings.api_key.get_secret_value(),
            max_output_tokens=self.settings.max_output_tokens,
            timeout_seconds=self.settings.timeout_seconds,
            temperature=self.settings.temperature,
            transport=self.transport,
        )

    def configure_settings(self, updates, *, clear_keys=(), persist=False):
        """Apply one settings patch; persist successfully before changing the live runtime."""
        from .media import endpoint
        from .provider_config import save_provider_settings

        with self.engine._lock:
            try:
                if not isinstance(updates, dict) or not isinstance(persist, bool):
                    raise ValueError()
                values = self.settings.model_dump()
                for name, value in updates.items():
                    if name not in Settings.model_fields:
                        raise ValueError()
                    if name.endswith("key") and value == "":
                        continue
                    values[name] = value
                for name in clear_keys:
                    if name not in Settings.model_fields or not name.endswith("key"):
                        raise ValueError()
                    values[name] = ""
                configured = Settings.model_validate(values)
                for name in Settings.model_fields:
                    if name.endswith("base_url") and getattr(configured, name):
                        endpoint(getattr(configured, name))
            except (ValueError, TypeError, AttributeError):
                raise ValueError(
                    "Invalid provider settings; check fields and endpoint format"
                ) from None
            if persist:
                save_provider_settings(configured)
            self.settings = configured
            return {"settings": configured.public(), "persisted": persist}

    def operation(self, character_id, kind, request_id, payload, run):
        """Exact replay returns receipt; unknown/failed requests require explicit new ID.
        Single SQLite reservation works across processes and does not hold locks over I/O.
        """
        import hashlib

        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        if not request_id or len(request_id) > 120:
            raise ValueError("request_id must contain 1..120 characters")
        with self.engine._transaction():
            self.engine._character(character_id)
            existing = self.engine.db.execute(
                "SELECT * FROM operations WHERE owner=? AND character_id=? AND kind=? "
                "AND request_id=?",
                (self.engine.owner, character_id, kind, request_id),
            ).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise Conflict("Operation request ID already used with another payload")
                if existing["status"] == "succeeded":
                    return json.loads(existing["result"])
                raise Conflict(
                    "Operation is pending/failed; inspect receipts before explicitly "
                    "retrying with a new ID"
                )
            if (
                kind in ("index", "maintenance", "reconcile")
                and self.engine.db.execute(
                    "SELECT 1 FROM operations WHERE owner=? AND character_id=? "
                    "AND kind IN ('index','maintenance','reconcile') AND status='running'",
                    (self.engine.owner, character_id),
                ).fetchone()
            ):
                raise Conflict("Another memory maintenance operation is running or interrupted")
            op = str(uuid4())
            self.engine.db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,NULL,NULL,?)",
                (
                    op,
                    self.engine.owner,
                    character_id,
                    kind,
                    request_id,
                    digest,
                    "running",
                    time.time(),
                ),
            )
        try:
            result = run()
            with self.engine._transaction():
                self.engine.db.execute(
                    "UPDATE operations SET status='succeeded',result=?,usage=? WHERE id=?",
                    (
                        json.dumps(result, ensure_ascii=False),
                        json.dumps(result.get("usage", {"billing": "unknown"})),
                        op,
                    ),
                )
            return result
        except Exception:
            with self.engine._transaction():
                self.engine.db.execute(
                    "UPDATE operations SET status='failed_unknown',usage=? WHERE id=?",
                    ('{"billing":"unknown","retried":false}', op),
                )
            raise

    def receipts(self, character_id):
        with self.engine._lock:
            self.engine._character(character_id)
            return [
                dict(r)
                for r in self.engine.db.execute(
                    "SELECT id,kind,request_id,status,usage,created_at FROM operations "
                    "WHERE owner=? AND character_id=? ORDER BY created_at DESC LIMIT 100",
                    (self.engine.owner, character_id),
                )
            ]

    def artifacts(self, character_id):
        with self.engine._lock:
            self.engine._character(character_id)
            return [
                json.loads(r[0])
                for r in self.engine.db.execute(
                    "SELECT result FROM operations WHERE owner=? AND character_id=? "
                    "AND kind IN ('speech','image') AND status='succeeded' "
                    "ORDER BY created_at DESC LIMIT 12",
                    (self.engine.owner, character_id),
                )
            ]

    def chat(self, character_id, message, request_id, *, attachments=(), call_id=None):
        with self.engine._lock:
            active = self.engine.db.execute(
                "SELECT id FROM call_sessions WHERE owner=? AND character_id=? "
                "AND status IN ('starting','active','cleanup_required')",
                (self.engine.owner, character_id),
            ).fetchone()
            if active and active[0] != call_id:
                raise Conflict("End the active call before sending a text message")
        message = TypeAdapter(Text).validate_python(message)
        request_id = TypeAdapter(Key).validate_python(request_id)
        payload = {"message": message}
        if attachments:
            payload["attachments"] = [image.descriptor() for image in attachments]
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        with self.engine._lock:
            operation = self.engine.db.execute(
                "SELECT digest FROM operations WHERE owner=? AND character_id=? "
                "AND kind='chat' AND request_id=?",
                (self.engine.owner, character_id, request_id),
            ).fetchone()
            if operation and operation["digest"] != digest:
                raise Conflict("Request ID reused with different message or attachments")
            existing = self.engine.db.execute(
                "SELECT * FROM turns WHERE owner=? AND character_id=? AND request_id=?",
                (self.engine.owner, character_id, request_id),
            ).fetchone()
            if existing and existing["user_message"] != message:
                raise Conflict("Request ID reused with different message")
            if existing and existing["status"] == "committed":
                saved = self.engine.db.execute(
                    "SELECT payload FROM turn_drafts WHERE turn_id=?", (existing["id"],)
                ).fetchone()
                return {
                    **(json.loads(saved[0]) if saved else {}),
                    **json.loads(existing["result"]),
                    "turn_id": existing["id"],
                    "reply": existing["assistant_reply"],
                    "committed": True,
                    "needs_review": False,
                    "replayed": True,
                }

            saved_operation = self.engine.db.execute(
                "SELECT status FROM operations WHERE owner=? AND character_id=? "
                "AND kind='chat' AND request_id=?",
                (self.engine.owner, character_id, request_id),
            ).fetchone()
        # Validate local provider configuration before reserving a NEW operation. Saved
        # drafts and successful replays remain usable after credentials are removed.
        provider = self.provider() if saved_operation is None else None
        from .media import Media

        if saved_operation is None:
            pending = self.engine.pending_turn(character_id)
            if pending and pending["request_id"] != request_id:
                raise Conflict("Another turn is pending; commit or abort it first")
            if attachments:
                # Reject missing local configuration before reserving.
                Media(self).vision_provider()

        def generate():
            assert provider is not None
            started = time.monotonic()
            # Reserve the character's turn before ANY vision call. A different request in
            # another process cannot pay for perception while this turn is still active.
            context = self.prepare(character_id, message, request_id)
            context.response_guidance += self.chat_images.guidance(character_id)
            if self.reply_instructions:
                context.response_guidance += (
                    "\nApplication reply preferences:\n" + self.reply_instructions
                )
                context = enforce_context_policy(self.engine, context)
                if len(context.model_dump_json()) > self.engine.context_chars:
                    raise ValueError("Reply instructions exceed context budget")
            vision = (
                Media(self).observe(character_id, message, attachments, request_id)
                if attachments
                else None
            )
            if vision:
                self.engine.record_usage(
                    character_id,
                    context.turn_id,
                    vision["model"],
                    Usage.model_validate(vision["usage"]) if vision["usage"] else None,
                    status="reported" if vision["usage"] else "unknown",
                )
                context.media_observations = self.engine.attach_media(
                    character_id, context.turn_id, vision["observations"]
                )
                context = enforce_context_policy(self.engine, context)
            enriched = self.context(character_id, context, provider)

            result = draft_reply(self.engine, character_id, provider, context=enriched)
            result["generation"] = {
                "model": getattr(provider, "model", "custom"),
                "retrieval_mode": enriched.retrieval_mode,
                "memory_ids": [m.id for m in enriched.memories],
                "evidence_ids": [e["id"] for e in enriched.evidence],
                "document_ids": [d["id"] for d in enriched.core_memory],
                "recent_turns": len(enriched.recent_turns),
                "context_chars": len(enriched.model_dump_json()),
                "media": {
                    "mode": "vision_observations" if vision else "text",
                    "inputs": payload.get("attachments", []),
                    "model": vision["model"] if vision else None,
                },
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            if self.capture_context:
                result["context"] = enriched.model_dump(mode="json")
            with self.engine._transaction():
                self.engine.db.execute(
                    "INSERT OR REPLACE INTO turn_drafts VALUES(?,?)",
                    (context.turn_id, json.dumps(result)),
                )
            return result

        draft = self.operation(character_id, "chat", request_id, payload, generate)
        draft = self.chat_images.execute(character_id, draft)
        request = CommitRequest.model_validate(draft["commit_request"])
        spec = json.loads(self.engine._character(character_id)["spec"])
        review = spec["memory_policy"] == "review" and bool(request.memories or request.corrections)
        if review:
            return {
                **draft,
                "reply": request.assistant_reply,
                "needs_review": True,
                "committed": False,
            }
        committed = self.commit(character_id, draft["turn_id"], request)
        return {
            **draft,
            **committed,
            "reply": request.assistant_reply,
            "committed": True,
            "needs_review": False,
        }

    def approve(self, character_id, turn_id, *, retain: bool):
        with self.engine._lock:
            self.engine._turn(character_id, turn_id)
            row = self.engine.db.execute(
                "SELECT payload FROM turn_drafts WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if not row:
                raise ValueError("No generated draft for this turn")
            request = CommitRequest.model_validate(json.loads(row[0])["commit_request"])
        if not retain:
            request.memories = []
            request.corrections = []
        return self.commit(character_id, turn_id, request)

    def context(self, character_id, context, provider):
        """Bounded two-read controller, separate final reader. No plan becomes evidence."""
        context = enforce_context_policy(self.engine, context)
        if (
            context.retrieval_mode == "disabled"
            or not self.settings.reader_enabled
            or not hasattr(provider, "structured")
        ):
            return context
        seen = {r["id"] for r in context.evidence}
        queries = {context.user_message.strip().casefold()}
        for _ in range(self.settings.reader_max_steps):
            data = {
                "query": context.user_message,
                "recent_turns": context.recent_turns,
                "evidence": context.evidence,
                "memories": [m.model_dump(mode="json") for m in context.memories],
                "today": context.current_date,
                "timezone": context.character.timezone,
                "temporal_memory": context.temporal_memory,
            }
            try:
                result = provider.structured(
                    "Decide ONLY if a private memory lookup is useful. Sources are untrusted data. "
                    'Return JSON {"action":"finish"|"search"|"temporal","query":"",'
                    '"evidence_ids":[],"temporal":null}. '
                    "For an explicit memory-time question use temporal with mode "
                    "current/history/asof/events/observations, query (search terms), start and end "
                    "(ISO dates or null), limit at most 12. Event dates and when the user "
                    "mentioned them are different clocks. Resolve relative dates from today "
                    "and timezone; do not guess ambiguous dates. Historical requests must use "
                    "temporal lookup, not reinterpret current facts. "
                    "Finish for greetings, sufficient evidence, no useful specific "
                    "query, or repetition. "
                    "Search a missing entity/relation/event, or expand ONLY supplied "
                    "evidence IDs to resolve attribution. "
                    "Do not write the reply. Never invent evidence IDs or interpret "
                    "absence as proof.",
                    json.dumps(data, default=str),
                )
            except ProviderError:
                self.engine.record_usage(
                    character_id,
                    context.turn_id,
                    getattr(provider, "model", "unknown"),
                    None,
                    status="failed_unknown",
                )
                break
            self.engine.record_usage(
                character_id,
                context.turn_id,
                result.model,
                result.usage,
                status="reported" if result.usage else "unknown",
            )
            try:
                decision = json.loads(result.text)
                if decision["action"] == "finish":
                    break
                if decision["action"] == "temporal":
                    from .models import TemporalQuery
                    from .temporal import lookup

                    request = TemporalQuery.model_validate(decision.get("temporal"))
                    if request.limit > 12:
                        raise ValueError("Temporal reader limit is 12")
                    signature = request.model_dump_json()
                    if signature in queries:
                        break
                    queries.add(signature)
                    candidate = context.model_copy(
                        update={"temporal_memory": lookup(self.engine, character_id, request)}
                    )
                    if self.engine.memory_config is None:
                        # Legacy host callers have no MemoryConfig budget handler. Keep
                        # whole records and explicitly mark bounded temporal results.
                        for field in ("uncertain_validity", "facts", "sources"):
                            records = candidate.temporal_memory.get(field, [])
                            while (
                                records
                                and len(candidate.model_dump_json()) > self.engine.context_chars
                            ):
                                records.pop()
                                candidate.temporal_memory["truncated_for_context"] = True
                        if len(candidate.model_dump_json()) > self.engine.context_chars:
                            raise ValueError("Temporal lookup exceeds available context")
                    context = enforce_context_policy(self.engine, candidate)
                    continue
                if decision["action"] != "search":
                    raise ValueError("Invalid reader decision")
                ids = decision.get("evidence_ids", [])
                if not isinstance(ids, list) or len(ids) > 3 or any(e not in seen for e in ids):
                    raise ValueError("Unseen evidence ID")
                query = decision.get("query", "")
                if not isinstance(query, str) or len(query) > 500:
                    raise ValueError("Invalid query")
                if ids:
                    found = self.engine.memory.expand(character_id, ids)
                elif query.strip() and query.strip().casefold() not in queries:
                    queries.add(query.strip().casefold())
                    found = self.recall(character_id, query)
                else:
                    break
                if "layers" in found:
                    self._merge_layers(context, found["layers"], prioritize=True)
                sources = found["sources"]
                if self.engine.memory_config is not None:
                    new_sources = [source for source in sources if source["id"] not in seen]
                    context.evidence = new_sources + context.evidence
                    sources = []
                for source in sources:
                    if source["id"] not in seen:
                        context.evidence.append(source)
                        if len(context.model_dump_json()) > self.engine.context_chars:
                            context.evidence.pop()
                            continue
                        seen.add(source["id"])
                context = enforce_context_policy(self.engine, context)
                seen = {source["id"] for source in context.evidence}
            except (ValueError, KeyError, TypeError):
                break  # malformed planner cannot replace originals or prevent the final reader
        return enforce_context_policy(self.engine, context)

    def recall(self, character_id, query):
        config = self.engine.memory_config
        if config is not None and (not config.read_enabled or config.context_chars == 0):
            return {"sources": [], "mode": "disabled", "layers": {"facts": [], "documents": []}}
        if (
            not self.settings.embedding_model
            or not self.automation.policy(character_id).semantic_retrieval
        ):
            return {
                **self.engine.memory.recall(character_id, query, **self._evidence_limits()),
                "layers": self.engine.retrieval.search(character_id, query, **self._layer_limits()),
            }
        from .media import Embeddings

        embed = Embeddings(self.settings, self.transport)
        result = self.operation(
            character_id,
            "query_embedding",
            str(uuid4()),
            {"query": query},
            lambda: embed.encode([query]),
        )
        return {
            **self.engine.memory.recall(
                character_id,
                query,
                query_vector=result["vectors"][0],
                model=embed.namespace,
                **self._evidence_limits(),
            ),
            "layers": self.engine.retrieval.search(
                character_id,
                query,
                vector=result["vectors"][0],
                model=embed.namespace,
                **self._layer_limits(),
            ),
        }

    def index(self, character_id, request_id):
        from .media import Embeddings

        embed = Embeddings(self.settings, self.transport)

        def run():
            rows = self.engine.evidence_index.candidates(character_id, embed.namespace)
            derived = self.engine.retrieval.candidates(
                character_id, embed.namespace, 32 - len(rows)
            )
            if not rows and not derived:
                return {"indexed": 0, "usage": {"network_calls": 0}}
            texts = [r["text"] for r in rows]
            texts += [r["text"] for r in derived]
            result = embed.encode(texts)
            for row, text, vector in zip(rows, texts, result["vectors"]):
                self.engine.evidence_index.store(
                    character_id, row["id"], row["lane"], embed.namespace, vector, text
                )
            for row, vector in zip(derived, result["vectors"][len(rows) :]):
                self.engine.retrieval.store(
                    character_id, row["id"], embed.namespace, vector, row["text"]
                )
            return {"indexed": len(rows), "derived_indexed": len(derived), "usage": result["usage"]}

        return self.operation(character_id, "index", request_id, {}, run)

    def memory_provider(self):
        if self.settings.summary_model or self.settings.summary_base_url:
            return CompatibleProvider(
                base_url=self.settings.summary_base_url,
                model=self.settings.summary_model,
                api_key=self.settings.summary_key.get_secret_value(),
                timeout_seconds=self.settings.timeout_seconds,
                max_output_tokens=self.settings.memory_max_output_tokens,
                transport=self.transport,
            )
        if self._provider is not None:
            return self._provider
        if not self.settings.base_url or not self.settings.model:
            raise ValueError("Configure a memory writer or chat provider")
        return CompatibleProvider(
            base_url=self.settings.base_url,
            model=self.settings.model,
            api_key=self.settings.api_key.get_secret_value(),
            timeout_seconds=self.settings.timeout_seconds,
            max_output_tokens=self.settings.memory_max_output_tokens,
            transport=self.transport,
        )

    def reconcile(self, character_id, request_id):
        from .knowledge import RECONCILE_PROMPT, Transitions

        provider = self.memory_provider()

        def run():
            batch = self.engine.knowledge.prepare(character_id)
            if not batch:
                return {"retired": 0, "usage": {"network_calls": 0}}
            if (
                self.settings.embedding_model
                and self.automation.policy(character_id).semantic_retrieval
            ):
                found = self.recall(character_id, batch["new"]["text"])
                batch = self.engine.knowledge.prepare(
                    character_id, related_ids=[m["id"] for m in found["layers"]["facts"]]
                )
            decisions = Transitions()
            if batch["candidates"]:
                result = provider.structured(
                    RECONCILE_PROMPT + json.dumps(Transitions.model_json_schema()),
                    json.dumps(batch),
                )
                self.engine.record_usage(
                    character_id,
                    batch["new"]["source_turn_id"],
                    result.model,
                    result.usage,
                    status="reported" if result.usage else "unknown",
                )
                decisions = Transitions.model_validate_json(result.text)
            return {
                **self.engine.knowledge.apply(character_id, batch, decisions),
                "usage": {"billing": "see provider_usage"},
            }

        return self.operation(character_id, "reconcile", request_id, {}, run)

    def maintain(self, character_id, request_id, *, flush=True):
        """Bounded note evolution and citation-backed episodic/core consolidation."""
        from . import note_evolution
        from .models import EpisodeMetadata, MemoryNotebook

        provider = self.memory_provider()

        def run():
            policy = self.automation.policy(character_id)
            sources = self.engine.memory.maintenance_sources(
                character_id,
                policy.model_copy(update={"retain_recent_turns": 0}) if flush else policy,
            )
            updated = []
            for source in sources:
                with self.engine._lock:
                    existing_note = self.engine.db.execute(
                        "SELECT 1 FROM notes WHERE evidence_id=?", (source["id"],)
                    ).fetchone()
                if existing_note:
                    continue
                with self.engine._lock:
                    candidate_ids = [
                        c["id"]
                        for c in self.engine.memory.recall(
                            character_id, source["text"][:16000], limit=5, before_id=source["id"]
                        )["sources"]
                    ]
                if self.settings.embedding_model and policy.semantic_retrieval:
                    from .media import Embeddings

                    embed = Embeddings(self.settings, self.transport)
                    encoded = self.operation(
                        character_id,
                        "note_query_embedding",
                        str(uuid4()),
                        {"source_id": source["id"], "query": source["text"]},
                        lambda: embed.encode([source["text"][:16000]]),
                    )
                    semantic = self.engine.memory.recall(
                        character_id,
                        source["text"][:16000],
                        limit=5,
                        before_id=source["id"],
                        query_vector=encoded["vectors"][0],
                        model=embed.namespace,
                    )
                    candidate_ids = list(
                        dict.fromkeys([c["id"] for c in semantic["sources"]] + candidate_ids)
                    )[:5]
                with self.engine._lock:
                    new_source = note_evolution.snapshot(self.engine, character_id, source["id"])
                    candidates = [
                        note_evolution.snapshot(self.engine, character_id, eid)
                        for eid in candidate_ids
                    ]
                result = provider.structured(
                    note_evolution.NOTE_PROMPT
                    + json.dumps(note_evolution.NoteEvolution.model_json_schema()),
                    json.dumps({"new_source": new_source, "candidates": candidates}),
                )
                self.engine.record_usage(
                    character_id,
                    source["turn_id"],
                    result.model,
                    result.usage,
                    status="reported" if result.usage else "unknown",
                )
                proposal = note_evolution.NoteEvolution.model_validate_json(result.text)
                updated.extend(
                    note_evolution.apply(
                        self.engine, character_id, new_source, candidates, proposal
                    )
                )
            if sources:
                # Summary citations are selected by model and checked, no invented source IDs.
                with self.engine._lock:
                    documents = [
                        document
                        for document in self.engine.memory.documents(character_id)
                        if document["kind"] in ("core", "character_core")
                    ]
                    core_version = self.engine.memory.core_version(character_id)
                old_ids = {
                    eid
                    for d in documents
                    if d["kind"] in ("core", "character_core")
                    for eid in d["source_ids"]
                }
                # Re-present originals behind the old core. Never let a summary be its own proof.
                originals = {s["id"]: s for s in sources}
                with self.engine._lock:
                    for eid in sorted(old_ids):
                        originals[eid] = self.engine.memory._source(character_id, eid)
                if len(originals) > 60 or len(json.dumps(list(originals.values()))) > 100000:
                    raise ValueError(
                        "Core consolidation window exceeded; explicitly curate core "
                        "sources before continuing"
                    )
                with self.engine._lock:
                    fact_version = self.engine.memory.fact_version(character_id)
                    current_facts = [
                        m.model_dump(mode="json")
                        for m in self.engine.memories(character_id, limit=100)
                        if not m.lifecycle.invalidation
                    ]
                with self.engine._lock:
                    for fact in current_facts:
                        if not fact["source_turn_id"] or len(originals) >= 60:
                            continue
                        eid = fact["source_turn_id"] + (
                            ":media" if fact["origin"] == "host_observation" else ":user"
                        )
                        originals[eid] = self.engine.memory._source(character_id, eid)
                if len(json.dumps(list(originals.values()))) > 100000:
                    raise ValueError("Core originals exceed consolidation budget")
                data = {
                    "sources": list(originals.values()),
                    "existing_core": documents,
                    "current_facts": current_facts,
                    "knowledge_updates": [
                        dict(r)
                        for r in self.engine.db.execute(
                            "SELECT memory_key,operation,source_id FROM knowledge_updates "
                            "WHERE owner=? AND character_id=? ORDER BY created DESC LIMIT 200",
                            (self.engine.owner, character_id),
                        )
                    ],
                }
                result = provider.structured(
                    'Return JSON {"episode":"...","core":"...",'
                    '"episode_source_ids":[...],"core_source_ids":[...]}. '
                    "Summarize the supplied original sources; merge useful prior core "
                    "with explicit new corrections. Current facts override "
                    "conflicting old originals; "
                    "do not revive corrected or superseded facts. "
                    "Separate user facts, shared interactions, and character fiction. "
                    "Preserve dates and unresolved outcomes. "
                    "Return notebook with source-cited entries in identity, their_people, "
                    "life_now, emotional_landscape, drives, open_threads, dates, us, read_on_them, "
                    "communication and pinned sections. Leave unsupported sections empty. "
                    "Tentative interpretations "
                    "must retain basis and uncertainty. Use this schema: "
                    + json.dumps(MemoryNotebook.model_json_schema())
                    + " "
                    "Optionally return character_core and character_source_ids for established "
                    "fictional self-experiences, clearly distinct from user facts. Do not invent "
                    "a life story to fill this field. Preserve supported prior character memory. "
                    "Also return episode_metadata with importance (1–10), valence (-1..1), "
                    "arousal (0..1), explicitly grounded event_dates and unresolved outcomes. "
                    + json.dumps(EpisodeMetadata.model_json_schema())
                    + " "
                    "Do not invent details. Include source IDs supporting the new "
                    "summary; at most 6000 characters per text. "
                    "Prior summaries are hints, not independent evidence. Never erase uncertainty.",
                    json.dumps(
                        {
                            **data,
                            "policy": "Episode citations support this new batch; "
                            "core citations retain the "
                            "most durable, current facts (maximum 40 originals). Older episodes "
                            "and original evidence remain searchable; core is not a full archive.",
                        }
                    ),
                )
                self.engine.record_usage(
                    character_id,
                    sources[-1]["turn_id"],
                    result.model,
                    result.usage,
                    status="reported" if result.usage else "unknown",
                )
                out = json.loads(result.text)
                allowed = set(originals)
                for key in ("episode_source_ids", "core_source_ids"):
                    if not isinstance(out.get(key), list) or not set(out[key]) <= allowed:
                        raise ValueError("Invalid consolidation citations")
                if (
                    out.get("character_core") is not None
                    and not set(out.get("character_source_ids", [])) <= allowed
                ):
                    raise ValueError("Character citations were not supplied to writer")
                self.engine.memory.consolidate_pair(
                    character_id,
                    out["episode"],
                    out["core"],
                    out["episode_source_ids"],
                    core_source_ids=out["core_source_ids"],
                    processed_ids=[s["id"] for s in sources],
                    expected_facts=fact_version,
                    expected_documents=core_version,
                    episode_metadata=out.get("episode_metadata"),
                    notebook=out.get("notebook"),
                    character_core=out.get("character_core"),
                    character_source_ids=out.get("character_source_ids", []),
                )
            return {
                "annotated": len(updated),
                "usage": {"billing": "see provider_usage; unknown prices are not zero"},
            }

        return self.operation(character_id, "maintenance", request_id, {"flush": flush}, run)

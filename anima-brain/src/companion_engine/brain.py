"""Small application API over Engine + Runtime; no second memory implementation."""

import asyncio
import json
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import Field, TypeAdapter

from .automation import MemoryAutomation
from .engine import Conflict, Engine, NotFound
from .models import CharacterSpec, Key, MemoryCorrection, MemoryDraft, Model, Text
from .providers import Provider
from .runtime import Runtime, Settings
from .sdk_models import (
    AudioInput,
    ImageInput,
    ImageOptions,
    ImageResult,
    MediaArtifact,
    MemoryConfig,
    SpeechOptions,
    SpeechResult,
    TranscriptionOptions,
    TranscriptionResult,
    load_images,
)

NATURAL_REPLY = """Use conversational language appropriate to the character and user.
For English, aim for 1–8 words for a simple acknowledgment, 10–35 for ordinary chat,
and 40–90 when the request needs explanation. Other languages should have comparable brevity.
These are soft targets, not truncation rules. Expand when the user asks for depth or clarity
requires it. Usually choose one useful conversational move, then leave room for the user.
Avoid unsolicited lists, customer-service closings and repetitive reassurance. Lists are fine
when requested or useful for a concrete task. Match slang and pauses naturally; do not force
them, manufacture typos, or pretend to be human. Acknowledge feelings without automatically
agreeing with conclusions. Do not invent shared experiences or unnecessary follow-up questions.
""".strip()


class BrainConfig(Model):
    allow_chat_images: bool = False
    context_chars: int = Field(default=48000, ge=4000, le=200000, strict=True)
    recent_turns: int = Field(default=10, ge=0, le=100, strict=True)
    recent_chars: int = Field(default=12000, ge=0, le=100000, strict=True)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    reply_instructions: str = Field(default=NATURAL_REPLY, max_length=8000)
    capture_context: bool = False
    # Standalone apps need a backend writer; MCP hosts keep their own authoring workflow.
    # Providers must be configured; explicit persisted/off policies are never overwritten.
    automation: MemoryAutomation = Field(
        default_factory=lambda: MemoryAutomation(
            embedding=True,
            semantic_retrieval=True,
            summaries=True,
            reconcile=True,
            summary_turns=20,
            summary_batch_messages=20,
            retain_recent_turns=10,
        )
    )
    start_worker: bool = True


class TurnUsage(Model):
    """Chat/reader/vision usage for this turn; other operations have separate receipts."""

    attempts: int = 0
    reported_input_tokens: int = 0
    reported_output_tokens: int = 0
    missing_token_reports: int = 0
    estimated_usd: float | None = None


class ChatReply(Model):
    text: str
    character_id: str
    request_id: str
    turn_id: str
    committed: bool
    needs_review: bool = False
    replayed: bool = False
    memory_ids: list[str] = Field(default_factory=list)
    proposed_memories: list[MemoryDraft] = Field(default_factory=list)
    proposed_corrections: list[MemoryCorrection] = Field(default_factory=list)
    usage: TurnUsage = Field(default_factory=TurnUsage)
    elapsed_seconds: float
    generation: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    context: dict | None = None
    image_result: dict | None = None


class ChatFailed(RuntimeError):
    """Inspectable failure. No automatic retry or fabricated successful reply."""

    def __init__(self, request_id, pending_turn_id, reply_text, cause_type):
        self.request_id = request_id
        self.pending_turn_id = pending_turn_id
        self.reply_text = reply_text
        self.cause_type = cause_type
        super().__init__(
            f"Chat did not complete ({cause_type}); inspect pending turn and receipts. "
            "A saved draft can be retried with the SAME request ID without regenerating. "
            "An uncertain provider operation requires explicit discard and a NEW request ID."
        )


class MediaFailed(RuntimeError):
    def __init__(self, request_id, operation, cause_type):
        self.request_id, self.operation, self.cause_type = request_id, operation, cause_type
        super().__init__(
            f"{operation} did not complete ({cause_type}); inspect receipts before retrying. "
            "Provider billing may be unknown. No automatic retry was made."
        )


class CompanionBrain:
    """One trusted user's local brain. Configure your app's authentication separately.

    chat() is async, chat_sync() is blocking. Calls on this instance are serialized;
    cross-process conflicts are enforced by SQLite. Canceling an async waiter does not
    cancel a provider request already running in its worker thread. Reuse request_id.
    """

    def __init__(
        self,
        storage: str | Path = "brain.sqlite3",
        *,
        user_id: str = "local",
        settings: Settings | None = None,
        config: BrainConfig | None = None,
        provider: Provider | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.config = BrainConfig.model_validate(config or BrainConfig()).model_copy(deep=True)
        # Explicit default: the library never loads another application's saved credentials.
        settings = Settings.model_validate(settings or Settings()).model_copy(deep=True)
        self._lock = threading.RLock()
        self._closed = False
        self._resources = ExitStack()
        try:
            self.engine = self._resources.enter_context(
                Engine(
                    storage,
                    owner=user_id,
                    context_chars=self.config.context_chars,
                    recent_turns=self.config.recent_turns,
                    recent_chars=self.config.recent_chars,
                    memory_config=self.config.memory,
                )
            )
            self.runtime = Runtime(
                self.engine,
                settings,
                provider=provider,
                transport=transport,
                reply_instructions=self.config.reply_instructions,
                capture_context=self.config.capture_context,
                allow_chat_images=self.config.allow_chat_images,
                default_automation=self.config.automation,
            )
            self._resources.callback(self.runtime.local_settings.close)
            if self.config.start_worker:
                self._resources.enter_context(self.runtime.automation.worker())
        except BaseException:
            self._resources.close()
            raise

    def _check_open(self):
        if self._closed:
            raise RuntimeError("CompanionBrain is closed")

    def create_character(
        self,
        *,
        name: str,
        persona: str,
        character_id: str | None = None,
        memory_policy: Literal["automatic", "review", "off"] = "automatic",
        relationship_enabled: bool = True,
        romance_enabled: bool = False,
        timezone: str = "UTC",
        core_persona: str = "",
        appearance: str = "",
        voice_id: str = "",
    ) -> CharacterSpec:
        with self._lock:
            self._check_open()
            spec = CharacterSpec(
                id=character_id or str(uuid4()),
                name=name,
                persona=persona,
                memory_policy=memory_policy,
                relationship_enabled=relationship_enabled,
                romance_enabled=romance_enabled,
                timezone=timezone,
                core_persona=core_persona,
                appearance=appearance,
                voice_id=voice_id,
            )
            return self.runtime.create_character(spec)

    def characters(self) -> list[CharacterSpec]:
        with self._lock:
            self._check_open()
            return self.engine.characters()

    def appearance_reference(self, character_id: str):
        """Metadata for the character's saved appearance image; no provider call."""
        with self._lock:
            self._check_open()
            return self.runtime.chat_images.reference(character_id)

    def set_appearance_reference(self, character_id: str, image: ImageInput | None):
        """Save/replace an appearance reference, or remove it with None. No provider call."""
        with self._lock:
            self._check_open()
            loaded = ImageInput.model_validate(image).load() if image is not None else None
            return self.runtime.chat_images.set_reference(character_id, loaded)

    def capabilities(self) -> dict[str, bool]:
        """Local configuration readiness only; no API request or model-support guarantee."""
        with self._lock:
            self._check_open()
            return self.runtime.capabilities()

    def update_character(self, character: CharacterSpec) -> CharacterSpec:
        with self._lock:
            self._check_open()
            return self.engine.update_character(character)

    async def chat(
        self,
        character_id: str,
        message: str,
        *,
        request_id: str | None = None,
        attachments: list[ImageInput] | None = None,
    ) -> ChatReply:
        return await asyncio.to_thread(
            self.chat_sync, character_id, message, request_id=request_id, attachments=attachments
        )

    def chat_sync(
        self,
        character_id: str,
        message: str,
        *,
        request_id: str | None = None,
        attachments: list[ImageInput] | None = None,
    ) -> ChatReply:
        with self._lock:
            self._check_open()
            request_id = str(uuid4()) if request_id is None else request_id
            request_id = TypeAdapter(Key).validate_python(request_id)
            message = TypeAdapter(Text).validate_python(message)
            images = load_images(attachments or [])
            started = time.monotonic()
            try:
                result = self.runtime.chat(character_id, message, request_id, attachments=images)
            except (Conflict, NotFound):
                raise
            except Exception as exc:
                # Do not leak provider bodies, keys or a validation dump through error messages.
                with self.engine._lock:
                    row = self.engine.db.execute(
                        "SELECT t.id,d.payload FROM turns t LEFT JOIN turn_drafts d "
                        "ON d.turn_id=t.id WHERE t.owner=? AND t.character_id=? "
                        "AND t.request_id=?",
                        (self.engine.owner, character_id, request_id),
                    ).fetchone()
                text = (
                    json.loads(row["payload"])["commit_request"]["assistant_reply"]
                    if (row and row["payload"])
                    else None
                )
                raise ChatFailed(
                    request_id, row["id"] if row else None, text, type(exc).__name__
                ) from None
            return self._reply(character_id, request_id, result, time.monotonic() - started)

    def _reply(self, character_id, request_id, result, elapsed):
        with self.engine._lock:
            usage = self.engine.db.execute(
                "SELECT count(*) attempts, coalesce(sum(input_tokens),0) input_tokens, "
                "coalesce(sum(output_tokens),0) output_tokens, "
                "coalesce(sum(input_tokens IS NULL),0) missing, "
                "coalesce(sum(estimated_usd IS NULL),0) unpriced, sum(estimated_usd) cost "
                "FROM provider_usage WHERE owner=? AND character_id=? AND turn_id=?",
                (self.engine.owner, character_id, result["turn_id"]),
            ).fetchone()
        request = result.get("commit_request", {})
        generation = result.get("generation", {})
        warnings = []
        mode = generation.get("retrieval_mode", "")
        if "missing" in mode or "unavailable" in mode:
            warnings.append(mode)
        if usage["missing"]:
            warnings.append("Some model calls did not report token usage.")
        if request and not request.get("attunement"):
            warnings.append("No attunement record was supplied by the model.")
        if result.get("needs_review"):
            warnings.append("Reply and proposed memories await review; call approve().")
        return ChatReply(
            text=result["reply"],
            character_id=character_id,
            request_id=request_id,
            turn_id=result["turn_id"],
            committed=result["committed"],
            needs_review=result.get("needs_review", False),
            replayed=result.get("replayed", False),
            memory_ids=result.get("memory_ids", []),
            proposed_memories=request.get("memories", []),
            proposed_corrections=request.get("corrections", []),
            usage=TurnUsage(
                attempts=usage["attempts"],
                reported_input_tokens=usage["input_tokens"],
                reported_output_tokens=usage["output_tokens"],
                missing_token_reports=usage["missing"],
                estimated_usd=(
                    usage["cost"] if usage["attempts"] and not usage["unpriced"] else None
                ),
            ),
            elapsed_seconds=round(elapsed, 3),
            generation=generation,
            image_result=result.get("image_result"),
            warnings=warnings,
            context=result.get("context") if self.config.capture_context else None,
        )

    def approve(self, character_id: str, turn_id: str, *, retain: bool) -> ChatReply:
        """Commit the displayed draft, with or without its proposed durable memory changes."""
        with self._lock:
            self._check_open()
            started = time.monotonic()
            committed = self.runtime.approve(character_id, turn_id, retain=retain)
            with self.engine._lock:
                row = dict(self.engine._turn(character_id, turn_id))
                draft = self.engine.db.execute(
                    "SELECT payload FROM turn_drafts WHERE turn_id=?", (turn_id,)
                ).fetchone()
            result = {
                **json.loads(draft[0]),
                **committed,
                "turn_id": turn_id,
                "reply": row["assistant_reply"],
                "committed": True,
                "needs_review": False,
            }
            return self._reply(character_id, row["request_id"], result, time.monotonic() - started)

    def _media_call(self, kind, character_id, request_id, call):
        with self._lock:
            self._check_open()
            request_id = TypeAdapter(Key).validate_python(
                str(uuid4()) if request_id is None else request_id
            )
            self.engine._character(character_id)
            from .media import Media

            try:
                result = call(Media(self.runtime), request_id)
            except (Conflict, NotFound, ValueError):
                raise
            except Exception as exc:
                raise MediaFailed(request_id, kind, type(exc).__name__) from None
            return request_id, result

    async def generate_image(
        self,
        character_id: str,
        prompt: str,
        *,
        options: ImageOptions | None = None,
        references: list[ImageInput] | None = None,
        request_id: str | None = None,
    ) -> ImageResult:
        return await asyncio.to_thread(
            self.generate_image_sync,
            character_id,
            prompt,
            options=options,
            references=references,
            request_id=request_id,
        )

    def generate_image_sync(
        self,
        character_id: str,
        prompt: str,
        *,
        options: ImageOptions | None = None,
        references: list[ImageInput] | None = None,
        request_id: str | None = None,
    ) -> ImageResult:
        options = ImageOptions.model_validate(options or ImageOptions())
        images = load_images(references or [])
        request_id, result = self._media_call(
            "image",
            character_id,
            request_id,
            lambda media, key: media.image(
                character_id, prompt, key, options=options, references=images
            ),
        )
        return ImageResult(
            character_id=character_id,
            request_id=request_id,
            artifacts=[self._artifact(a) for a in result.get("artifacts", [result])],
            usage=result["usage"],
        )

    async def speak(
        self,
        character_id: str,
        text: str,
        *,
        options: SpeechOptions | None = None,
        request_id: str | None = None,
    ) -> SpeechResult:
        return await asyncio.to_thread(
            self.speak_sync, character_id, text, options=options, request_id=request_id
        )

    def speak_sync(
        self,
        character_id: str,
        text: str,
        *,
        options: SpeechOptions | None = None,
        request_id: str | None = None,
    ) -> SpeechResult:
        options = SpeechOptions.model_validate(options or SpeechOptions())
        request_id, result = self._media_call(
            "speech",
            character_id,
            request_id,
            lambda media, key: media.speech(character_id, text, key, options=options),
        )
        return SpeechResult(
            character_id=character_id,
            request_id=request_id,
            artifact=self._artifact(result),
            usage=result["usage"],
        )

    async def transcribe(
        self,
        character_id: str,
        audio: AudioInput,
        *,
        options: TranscriptionOptions | None = None,
        request_id: str | None = None,
    ) -> TranscriptionResult:
        return await asyncio.to_thread(
            self.transcribe_sync, character_id, audio, options=options, request_id=request_id
        )

    def transcribe_sync(
        self,
        character_id: str,
        audio: AudioInput,
        *,
        options: TranscriptionOptions | None = None,
        request_id: str | None = None,
    ) -> TranscriptionResult:
        audio = AudioInput.model_validate(audio)
        options = TranscriptionOptions.model_validate(options or TranscriptionOptions())
        raw = audio.load()
        request_id, result = self._media_call(
            "transcription",
            character_id,
            request_id,
            lambda media, key: media.transcribe(
                character_id, raw, audio.mime, key, options=options
            ),
        )
        return TranscriptionResult(character_id=character_id, request_id=request_id, **result)

    @staticmethod
    def _artifact(result):
        return MediaArtifact(**{key: result[key] for key in MediaArtifact.model_fields})

    def artifact_path(self, character_id: str, artifact: str) -> Path:
        """Resolve only an artifact owned by this user and character."""
        with self._lock:
            self._check_open()
            from .media import Media

            return Media(self.runtime).artifact(character_id, artifact)[0]

    def configure_automation(self, character_id: str, policy: MemoryAutomation):
        with self._lock:
            self._check_open()
            return self.runtime.automation.configure(character_id, policy)

    def remember(self, character_id: str, memory: MemoryDraft):
        """Explicit application-approved assertion; no LLM call."""
        with self._lock:
            self._check_open()
            return self.engine.remember(character_id, memory)

    def delete_memory(self, character_id: str, memory_id: str, *, expected_revision: int):
        """Delete a fact and invalidate derived memories; original transcript remains."""
        with self._lock:
            self._check_open()
            return self.engine.delete_memory(
                character_id, memory_id, expected_revision=expected_revision
            )

    def pending(self, character_id: str) -> dict | None:
        with self._lock:
            self._check_open()
            return self.engine.pending_turn(character_id)

    def discard_pending(self, character_id: str, turn_id: str) -> None:
        """Explicitly abandon an interrupted/draft turn. This does not refund provider calls."""
        with self._lock:
            self._check_open()
            self.engine.abort_turn(character_id, turn_id)

    def memories(self, character_id: str, *, limit: int = 100, offset: int = 0):
        with self._lock:
            self._check_open()
            return self.engine.memories(character_id, limit=limit, offset=offset)

    def history(self, character_id: str, *, limit: int = 50, before=None):
        with self._lock:
            self._check_open()
            return self.runtime.chat_images.history(character_id, limit=limit, before=before)

    def call(self, character_id: str, options=None):
        """Return a live session; use async with, then send_audio() / events()."""
        from .calls import CallSession

        self._check_open()
        return CallSession(self.runtime, character_id, options)

    def call_history(self, character_id: str, *, limit: int = 50):
        from .calls import call_history

        self._check_open()
        return call_history(self.runtime, character_id, limit=limit)

    async def recover_call(self, call_id: str, *, confirm_provider_stopped: bool = False):
        from .calls import recover_call

        self._check_open()
        await recover_call(self.runtime, call_id, confirm_provider_stopped=confirm_provider_stopped)

    def receipts(self, character_id: str):
        with self._lock:
            self._check_open()
            return self.runtime.receipts(character_id)

    def close(self):
        with self._lock:
            if not self._closed:
                if getattr(self.engine, "_live_calls", set()):
                    raise RuntimeError(
                        "Close live calls with await call.close() before closing the brain"
                    )
                self._closed = True
                self._resources.close()

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *_):
        self.close()

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *_):
        for call in list(getattr(self.engine, "_live_calls", set())):
            await call.close()
        await asyncio.to_thread(self.close)

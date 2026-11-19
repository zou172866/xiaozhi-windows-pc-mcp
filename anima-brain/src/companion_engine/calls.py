"""Transport-independent live call sessions over the existing memory/emotion runtime."""

import asyncio
import base64
import json
import time
from contextlib import suppress
from typing import Literal
from uuid import uuid4

from pydantic import Field

from .call_providers import DoubaoASR, ViduAvatar, stream_speech
from .engine import Conflict
from .models import Model
from .providers import ProviderError
from .sdk_models import ImageInput


class CallOptions(Model):
    mode: Literal["voice", "video"] = "voice"
    language: Literal["zh-CN", "en-US", "ja-JP"] = "zh-CN"
    max_seconds: int = Field(default=600, ge=15, le=3600)
    camera_max_age_seconds: int = Field(default=5, ge=1, le=15)


class CallEvent(Model):
    type: str
    data: dict = Field(default_factory=dict)
    audio: bytes = Field(default=b"", exclude=True)
    sequence: int = 0


def available(runtime):
    import importlib.util

    s = runtime.settings
    voice = bool(
        importlib.util.find_spec("websockets")
        and s.speech_provider == "doubao"
        and s.speech_model
        and s.speech_key.get_secret_value()
        and (s.call_asr_key.get_secret_value() or s.speech_key.get_secret_value())
        and (runtime._provider or (s.base_url and s.model))
    )
    return {"voice_call": voice, "video_call": voice and bool(s.vidu_key.get_secret_value())}


def initialize(engine):
    with engine._transaction():
        engine.db.execute(
            (
                "CREATE TABLE IF NOT EXISTS call_sessions (id TEXT PRIMARY KEY, "
                "owner TEXT NOT NULL, character_id TEXT NOT NULL, mode TEXT NOT "
                "NULL, status TEXT NOT NULL, created_at REAL NOT NULL, ended_at "
                "REAL, avatar_id TEXT, detail TEXT, FOREIGN "
                "KEY(owner,character_id) REFERENCES characters(owner,id) ON DELETE "
                "CASCADE)"
            )
        )
        engine.db.execute(
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS one_live_call ON "
                "call_sessions(owner,character_id) WHERE status IN "
                "('starting','active','cleanup_required')"
            )
        )
        engine.db.execute(
            (
                "CREATE TABLE IF NOT EXISTS call_turns (call_id TEXT NOT NULL "
                "REFERENCES call_sessions(id) ON DELETE CASCADE, sequence INTEGER "
                "NOT NULL, turn_id TEXT, status TEXT NOT NULL, audio_bytes INTEGER "
                "NOT NULL DEFAULT 0, detail TEXT, PRIMARY KEY(call_id,sequence))"
            )
        )


class CallSession:
    """Use ``async with brain.call(...)``; send PCM16 mono 16k, consume 24k audio events.

    No microphone, network or provider calls occur until entering/opening the session.
    Closing stops ASR/avatar immediately, then drains any already-running brain request.
    That request may still incur charges. Completed text and playback delivery are separate.
    """

    def __init__(
        self,
        runtime,
        character_id,
        options=None,
        *,
        asr_factory=DoubaoASR,
        speech=stream_speech,
        avatar_factory=ViduAvatar,
    ):
        self.runtime, self.engine = runtime, runtime.engine
        self.character_id = character_id
        self.options = CallOptions.model_validate(options or CallOptions())
        self.settings = runtime.settings.model_copy(deep=True)
        self.id = str(uuid4())
        self.asr_factory, self.speech, self.avatar_factory = asr_factory, speech, avatar_factory
        self.asr = self.avatar = None
        self.queue = asyncio.Queue(maxsize=128)
        self.inputs = asyncio.Queue(maxsize=4)
        self.tasks = []
        self.worker = self.tts_task = None
        self.opening_task = None
        self.closed = False
        self.cancel_requested = False
        self.opened = False
        self.sequence = self.epoch = 0
        self.audio_bytes = 0
        self.frame = None
        self.frame_at = 0
        self.current_reply = ""
        self.audio_active = False
        self.echo_until = 0.0
        self.muted = False
        self.interrupted = False
        self.closing = asyncio.Lock()
        initialize(self.engine)

    async def emit(self, kind, data=None, *, audio=b"", sequence=None):
        if self.closed:
            return
        event = CallEvent(
            type=kind,
            data=data or {},
            audio=audio,
            sequence=self.sequence if sequence is None else sequence,
        )
        try:
            await asyncio.wait_for(self.queue.put(event), 5)
        except TimeoutError:
            raise RuntimeError("Call consumer too slow") from None

    def mark(self, status, detail=None):
        with self.engine._transaction():
            self.engine.db.execute(
                "UPDATE call_sessions SET status=?,detail=?,ended_at=? WHERE id=? AND owner=?",
                (
                    status,
                    detail,
                    time.time() if status not in ("starting", "active") else None,
                    self.id,
                    self.engine.owner,
                ),
            )

    def locator(self, live_id):
        with self.engine._transaction():
            self.engine.db.execute(
                "UPDATE call_sessions SET avatar_id=? WHERE id=?", (live_id, self.id)
            )

    async def open(self):
        if self.opened or self.closed:
            raise Conflict("A call object cannot be restarted")
        s = self.settings
        spec = self.engine._character(self.character_id)
        self.character = json.loads(spec["spec"])
        if not available(self.runtime)[self.options.mode + "_call"]:
            raise ValueError(
                (
                    "Configure Doubao streaming voice, ASR and a chat model; video "
                    "also needs a Vidu key"
                )
            )
        if not self.character.get("voice_id"):
            raise ValueError("Set this character's Doubao voice ID before calling")
        if self.engine.pending_turn(self.character_id):
            raise Conflict("Finish or discard the pending reply first")
        reference = (
            self.runtime.chat_images.load_reference(self.character_id)
            if self.options.mode == "video"
            else []
        )
        if self.options.mode == "video" and not reference:
            raise ValueError("Upload this character's appearance reference before a video call")
        with self.engine._transaction():
            if self.engine.db.execute(
                (
                    "SELECT 1 FROM call_sessions WHERE owner=? AND character_id=? AND "
                    "status IN ('starting','active','cleanup_required')"
                ),
                (self.engine.owner, self.character_id),
            ).fetchone():
                raise Conflict(
                    "This character already has a live or unfinished call; close it first"
                )
            self.engine.db.execute(
                (
                    "INSERT INTO "
                    "call_sessions(id,owner,character_id,mode,status,created_at) "
                    "VALUES(?,?,?,?,?,?)"
                ),
                (
                    self.id,
                    self.engine.owner,
                    self.character_id,
                    self.options.mode,
                    "starting",
                    time.time(),
                ),
            )
        self.opened = True
        self.opening_task = asyncio.current_task()
        active = getattr(self.engine, "_live_calls", None)
        if active is None:
            self.engine._live_calls = active = set()
        active.add(self)
        try:
            await self.emit("state", {"phase": "connecting", "call_id": self.id})
            self.asr = self.asr_factory(s, self.options.language)
            await self.asr.open()
            if self.cancel_requested:
                raise Conflict("Call cancelled during setup")
            if reference:
                await self.emit("state", {"phase": "starting_avatar"})
                image = reference[0]
                self.avatar = self.avatar_factory(
                    s,
                    "data:" + image.mime + ";base64," + base64.b64encode(image.data).decode(),
                    on_locator=self.locator,
                )
                rtc = await self.avatar.open()
                if self.cancel_requested:
                    raise Conflict("Call cancelled during setup")
                await self.emit("avatar", rtc)
                self.tasks.append(asyncio.create_task(self.watch(self.avatar.monitor())))
            self.mark("active")
            self.worker = asyncio.create_task(self.run_turns())
            self.tasks += [
                asyncio.create_task(self.watch(self.read_asr())),
                asyncio.create_task(self.deadline()),
            ]
            await self.emit(
                "state",
                {
                    "phase": "listening",
                    "sample_rate": 24000,
                    "input_sample_rate": 16000,
                    "call_id": self.id,
                },
            )
            self.opening_task = None
            return self
        except BaseException:
            await self.emit(
                "error",
                {
                    ("message"): (
                        "Call connection failed or was cancelled. Check provider "
                        "configuration before trying again."
                    )
                },
            )
            await self.close(reason="connect_failed")
            raise

    async def watch(self, task):
        try:
            await task
            if not self.closed:
                raise RuntimeError("Call provider disconnected")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.emit(
                "error",
                {
                    ("message"): (
                        "The live connection ended. Check provider settings; no automatic retry."
                    )
                },
            )
            await self.close(reason="provider_disconnected")

    async def deadline(self):
        await asyncio.sleep(self.options.max_seconds)
        await self.emit("notice", {"message": "Call time limit reached."})
        await self.close(reason="time_limit")

    async def send_audio(self, pcm: bytes):
        if not self.opened or self.closed or not self.asr:
            raise Conflict("Call is not connected")
        if not isinstance(pcm, bytes) or not 0 < len(pcm) <= 6400 or len(pcm) % 2:
            raise ValueError("Send at most 200ms of PCM16 mono 16kHz per frame")
        await self.asr.send(bytes(len(pcm)) if self.muted else pcm)

    def set_frame(self, image: ImageInput | None):
        if self.options.mode != "video" or self.closed:
            raise ValueError("Camera frames require an active video call")
        loaded = image.load() if image else None
        if loaded and len(loaded.data) > 512 * 1024:
            raise ValueError("Camera frame exceeds 512KB")
        self.frame, self.frame_at = loaded, time.monotonic()

    async def mute(self, enabled: bool):
        self.muted = enabled
        await self.emit("muted", {"muted": enabled})

    async def interrupt(self):
        self.epoch += 1
        self.interrupted = self.sequence > 0
        self.audio_active = False
        if self.tts_task:
            self.tts_task.cancel()
        if self.avatar:
            await self.avatar.signal(7)
        await self.emit("interrupted")

    async def read_asr(self):
        async for event in self.asr.results():
            if self.closed or self.muted:
                continue
            text = event["text"].strip()
            if event["type"] == "partial":
                await self.emit("partial", {"text": text})
                continue
            if not text:
                continue
            # Browser AEC and sustained-onset interrupt handle audio echo. Also reject an
            # exact echo of the current response; do not infer new user facts from it.
            import re

            def norm(x):
                return re.sub(r"[\W_]", "", x).lower()

            candidate, reply = norm(text), norm(self.current_reply)
            if (self.audio_active or time.monotonic() < self.echo_until) and (
                len(candidate) >= 8 and len(candidate) >= len(reply) * 0.6 and candidate in reply
            ):
                continue
            await self.interrupt()
            await self.emit("transcript", {"speaker": "user", "text": text})
            if self.avatar:
                await self.avatar.signal(9, text)
            if self.inputs.full():
                raise RuntimeError("Too many pending call turns")
            self.inputs.put_nowait(text)

    def delivery(self, seq, status, *, turn_id=None, count=0, detail=None):
        with self.engine._transaction():
            self.engine.db.execute(
                (
                    "INSERT INTO call_turns VALUES(?,?,?,?,?,?) ON "
                    "CONFLICT(call_id,sequence) DO UPDATE SET "
                    "status=excluded.status,audio_bytes=excluded.audio_bytes,detail=excluded.detail,turn_id=COALESCE(excluded.turn_id,call_turns.turn_id)"
                ),
                (self.id, seq, turn_id, status, count, detail),
            )

    async def run_turns(self):
        from .runtime import Runtime

        while not self.closed:
            text = await self.inputs.get()
            if text is None or self.closed:
                break
            self.sequence += 1
            seq, epoch = self.sequence, self.epoch
            self.audio_bytes = 0
            self.delivery(seq, "thinking")
            await self.emit("state", {"phase": "thinking"})
            # Per-call guidance, never mutate the shared runtime or user's saved settings.
            guidance = self.runtime.reply_instructions + (
                "\nYou are on a live voice call. Use a short speakable reply "
                "without markdown. Do not generate images during this call."
            )
            if self.interrupted:
                guidance += (
                    " The previous spoken reply may have been cut off; do not assume "
                    "the user heard it completely."
                )
            call_runtime = Runtime(
                self.engine,
                self.settings,
                provider=self.runtime._provider,
                transport=self.runtime.transport,
                reply_instructions=guidance,
                capture_context=self.runtime.capture_context,
            )
            attachments = (
                [self.frame]
                if self.frame
                and time.monotonic() - self.frame_at <= self.options.camera_max_age_seconds
                else []
            )
            try:
                result = await asyncio.to_thread(
                    call_runtime.chat,
                    self.character_id,
                    text,
                    self.id + "-" + str(seq),
                    attachments=attachments,
                    call_id=self.id,
                )
                self.current_reply = result["reply"]
                self.delivery(seq, "generated", turn_id=result["turn_id"])
                if self.closed or epoch != self.epoch:
                    self.delivery(
                        seq,
                        "not_played",
                        turn_id=result["turn_id"],
                        detail="Interrupted during generation",
                    )
                    continue
                await self.emit(
                    "transcript",
                    {
                        "speaker": "assistant",
                        "text": result["reply"],
                        "turn_id": result["turn_id"],
                        "needs_review": result.get("needs_review", False),
                    },
                )
                if self.avatar:
                    await self.avatar.signal(10, result["reply"])
                self.audio_active = True
                self.interrupted = False
                await self.emit("state", {"phase": "speaking"})
                self.tts_task = asyncio.create_task(self.play(result["reply"], seq))
                try:
                    count = await self.tts_task
                    self.delivery(seq, "audio_sent", count=count)
                    await self.emit("audio_end", {"audio_bytes": count})
                except asyncio.CancelledError:
                    self.delivery(
                        seq,
                        "interrupted",
                        count=self.audio_bytes,
                        detail="Playback interrupted; full text is retained separately",
                    )
                finally:
                    self.tts_task = None
                    self.audio_active = False
                    self.echo_until = time.monotonic() + 1.5
                if result.get("needs_review"):
                    await self.emit(
                        "notice",
                        {
                            ("message"): (
                                "This character uses memory review. Approve or decline this reply "
                                "in chat before continuing."
                            )
                        },
                    )
                    await self.close(reason="review_required")
                    return
                await self.emit("state", {"phase": "listening"})
            except Exception:
                self.delivery(
                    seq, "failed", detail="Provider/save error; billing may be unknown; no retry"
                )
                await self.emit(
                    "error",
                    {
                        ("message"): (
                            "This turn failed. No automatic retry; check the saved call and "
                            "pending reply."
                        )
                    },
                )
                await self.close(reason="turn_failed")
                return
            finally:
                call_runtime.local_settings.close()

    async def play(self, text, seq):
        count, started = 0, None
        async for pcm in self.speech(self.settings, self.character["voice_id"], text):
            if self.closed:
                break
            if started is None:
                started = time.monotonic()
            # Real-time pacing bounds provider/RTC buffering and keeps interrupts responsive.
            wait = started + count / 48000 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if self.avatar:
                await self.avatar.send(pcm)
            await self.emit("audio", audio=pcm, sequence=seq)
            count += len(pcm)
            self.audio_bytes = count
        return count

    async def events(self):
        while True:
            event = await self.queue.get()
            yield event
            if event.type == "closed":
                break

    async def close(self, reason="hangup"):
        self.cancel_requested = True
        opening = self.opening_task
        if opening and opening is not asyncio.current_task() and not opening.done():
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(opening)
        if self.closed:
            return
        async with self.closing:
            if self.closed:
                return
            self.closed = True
            self.epoch += 1
            if self.tts_task:
                self.tts_task.cancel()
            cleanup_failed = False
            for transport in (self.asr, self.avatar):
                if transport:
                    try:
                        await transport.close()
                    except Exception:
                        cleanup_failed = True
            for task in self.tasks:
                if task is not asyncio.current_task():
                    task.cancel()
            while not self.inputs.empty():
                self.inputs.get_nowait()
            self.inputs.put_nowait(None)
            # Don't cancel a thread that's committing SQLite while its owner is closing.
            if self.worker and self.worker is not asyncio.current_task():
                with suppress(asyncio.CancelledError):
                    await self.worker
            if self.opened:
                self.mark("cleanup_required" if cleanup_failed else "ended", reason)
            self.frame = None
            getattr(self.engine, "_live_calls", set()).discard(self)
            # No stale PCM may be played after a local hangup. Retain diagnostic events.
            pending = []
            while not self.queue.empty():
                event = self.queue.get_nowait()
                if not event.audio:
                    pending.append(event)
            for event in pending[-126:]:
                self.queue.put_nowait(event)
            self.queue.put_nowait(
                CallEvent(
                    type="closed",
                    data={"reason": reason, "cleanup_required": cleanup_failed, "call_id": self.id},
                )
            )

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, *_):
        await self.close()


def call_history(runtime, character_id, *, limit=50):
    """Private session/turn receipts. No audio, room tokens or provider keys are stored here."""
    if not 1 <= limit <= 200:
        raise ValueError("Call history limit must be 1..200")
    engine = runtime.engine
    with engine._lock:
        engine._character(character_id)
        sessions = engine.db.execute(
            "SELECT * FROM call_sessions WHERE owner=? AND character_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (engine.owner, character_id, limit),
        ).fetchall()
        result = []
        for row in sessions:
            item = dict(row)
            item["turns"] = [
                dict(r)
                for r in engine.db.execute(
                    "SELECT * FROM call_turns WHERE call_id=? ORDER BY sequence", (row["id"],)
                ).fetchall()
            ]
            result.append(item)
        return result


async def recover_call(runtime, call_id, *, confirm_provider_stopped=False):
    """Explicit recovery after a process crash. Caller must ensure no other worker owns it.

    A known avatar is hung up through Vidu. An unlocatable video create requires
    checking the provider console first, then confirm_provider_stopped=True.
    """
    engine = runtime.engine
    with engine._lock:
        row = engine.db.execute(
            "SELECT * FROM call_sessions WHERE id=? AND owner=?", (call_id, engine.owner)
        ).fetchone()
        if row is None:
            from .engine import NotFound

            raise NotFound("Call not found")
        if any(c.id == call_id for c in getattr(engine, "_live_calls", set())):
            raise Conflict("Close the live CallSession instead")
        if row["status"] == "ended":
            return
    if row["avatar_id"]:
        avatar = ViduAvatar(runtime.settings, "")
        avatar.live_id = row["avatar_id"]
        try:
            await avatar.close()
        except Exception:
            if not confirm_provider_stopped:
                raise ProviderError(
                    "Cannot confirm avatar cleanup; check provider console"
                ) from None
    elif row["mode"] == "video" and not confirm_provider_stopped:
        raise Conflict("Unknown video create outcome; verify provider stopped it before confirming")
    with engine._transaction():
        engine.db.execute(
            "UPDATE call_sessions SET status='ended',ended_at=?,detail='explicit_recovery' "
            "WHERE id=? AND owner=?",
            (time.time(), call_id, engine.owner),
        )

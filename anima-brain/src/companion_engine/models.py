"""Public, versionable contracts. No provider-specific types in the core."""

from datetime import date
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]
Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Fact = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Delta = Annotated[int, Field(strict=True, ge=-3, le=3)]


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", revalidate_instances="always", validate_assignment=True
    )


class CharacterSpec(Model):
    id: Key
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    persona: Text
    relationship_enabled: bool = True
    romance_enabled: bool = False
    memory_policy: Literal["review", "automatic", "off"] = "automatic"
    core_persona: str = Field(default="", max_length=16000)
    voice_id: str = Field(default="", max_length=200)
    appearance: str = Field(default="", max_length=2000)
    timezone: str = "UTC"

    @model_validator(mode="after")
    def valid_timezone(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone, e.g. America/New_York") from exc
        return self

    @model_validator(mode="after")
    def romance_requires_relationship(self) -> Self:
        if self.romance_enabled and not self.relationship_enabled:
            raise ValueError("Romance requires relationship simulation to be enabled")
        return self


class MemoryDetails(Model):
    state_key: str = Field(default="", max_length=160)
    correction_evidence: str = Field(default="", max_length=2000)
    recurrence: Literal["none", "annual"] = "none"
    date_kind: Literal["ordinary", "celebrate", "gentle", "deadline"] = "ordinary"
    subject: Literal["user", "character", "shared", "third_party"] = "user"
    temporal_kind: Literal["current", "historical", "event", "uncertain"] = "uncertain"
    event_date: date | None = None
    event_end: date | None = None
    valid_from: date | None = None
    importance: int = Field(default=5, ge=1, le=10)
    confidence: float = Field(default=1, ge=0, le=1)
    valence: float = Field(default=0, ge=-1, le=1)
    arousal: float = Field(default=0, ge=0, le=1)
    loop_status: Literal["none", "open", "resolved", "suppressed", "stale"] = "none"
    followup: Literal["ask", "background_only", "never_unless_asked"] = "background_only"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.recurrence == "annual" and self.event_date is None:
            raise ValueError("Annual dates require an explicitly known event_date")
        if self.event_end and (not self.event_date or self.event_end < self.event_date):
            raise ValueError("event_end requires an earlier event_date")
        if self.loop_status == "open" and self.subject != "user":
            raise ValueError("Open loops must describe the user's own life")
        return self


class EpisodeMetadata(Model):
    importance: int = Field(default=5, ge=1, le=10, strict=True)
    valence: float = Field(default=0, ge=-1, le=1)
    arousal: float = Field(default=0, ge=0, le=1)
    event_dates: list[date] = Field(default_factory=list, max_length=20)
    unresolved: list[str] = Field(default_factory=list, max_length=10)


class ObservedMedia(Model):
    """Host-supplied perception, NOT backend-verified bytes or a user assertion."""

    source_ref: str = Field(min_length=1, max_length=500)
    kind: Literal["image", "audio"] = "image"
    observation: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(default=0.8, ge=0, lt=1)


class MemoryDraft(Model):
    """A candidate claim, not automatically proven true by a model."""

    key: Key
    source_media_ref: str | None = Field(default=None, max_length=500)
    text: Fact
    category: Literal[
        "identity",
        "preference",
        "relationship",
        "event",
        "goal",
        "boundary",
        "style",
        "emotional",
        "date",
    ] = "event"
    source_quote: Fact
    details: MemoryDetails = Field(default_factory=MemoryDetails)


class MemoryLifecycle(Model):
    observed_at: float | None = None
    last_reinforced_at: float | None = None
    mentions: int = 1
    invalidation: Literal["corrected", "superseded"] | None = None
    invalidated_at: float | None = None
    valid_until: date | None = None
    superseded_by: str | None = None


class Memory(Model):
    id: str
    character_id: str
    key: str
    text: str
    category: str
    source_quote: str
    source_turn_id: str | None
    origin: Literal["user_assertion", "manual", "host_observation"]
    revision: int
    updated_at: float
    details: MemoryDetails = Field(default_factory=MemoryDetails)
    lifecycle: MemoryLifecycle = Field(default_factory=MemoryLifecycle)


class Appraisal(Model):
    """An attributed interpretation of this turn, not a diagnosis of the user."""

    emotion: Literal[
        "calm",
        "warm",
        "happy",
        "curious",
        "hurt",
        "sad",
        "guarded",
        "playful",
        "tender",
        "flustered",
        "annoyed",
        "anxious",
    ] = "calm"
    intensity: Annotated[int, Field(strict=True, ge=0, le=3)] = 0
    trust: Delta = 0
    warmth: Delta = 0
    tension: Delta = 0
    disclosure: Annotated[int, Field(strict=True, ge=0, le=3)] = 0
    romance: Delta = 0
    source_quote: Fact


class TurnContext(Model):
    turn_id: str
    character: CharacterSpec
    user_message: str
    recent_turns: list[dict[str, str]]
    memories: list[Memory]
    relationship_note: str | None
    response_guidance: str
    retrieval_mode: str = "lexical_fts5"
    memory_maintenance: dict = Field(default_factory=dict)
    continuity: dict = Field(default_factory=dict)
    recent_attunement: list[dict] = Field(default_factory=list)
    character_state: dict | None = None
    evidence: list[dict] = Field(default_factory=list)
    core_memory: list[dict] = Field(default_factory=list)
    open_loops: list[Memory] = Field(default_factory=list)
    media_observations: list[ObservedMedia] = Field(default_factory=list, max_length=4)
    temporal_memory: dict = Field(default_factory=dict)
    current_date: str = ""
    calendar: list[dict] = Field(default_factory=list)
    response_style: dict = Field(default_factory=dict)
    reaction_feedback: dict = Field(default_factory=dict)
    omitted_recent_turns: int = 0
    omitted_memories: int = 0


class MemoryCorrection(Model):
    memory_id: str
    expected_revision: int = Field(ge=1)
    kind: Literal["corrected", "superseded"] = "corrected"
    draft: MemoryDraft


class Attunement(Model):
    """Short attributed decision, never private chain-of-thought or diagnosis."""

    alternative: str = Field(default="", max_length=500)
    character_reaction: str = Field(default="", max_length=500)
    responsive_move: str = Field(default="", max_length=500)
    intent: Literal["listen", "practical", "answer", "repair", "space", "playful", "uncertain"]
    user_cue: str = Field(max_length=500)
    source_quote: Fact
    uncertainty: str = Field(default="", max_length=500)
    avoid: list[str] = Field(default_factory=list, max_length=8)


class ImageRequest(Model):
    """A single explicitly requested character image, never a local file or URL."""

    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    request_quote: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=1000)
    ]


class CommitRequest(Model):
    assistant_reply: Text
    memories: list[MemoryDraft] = Field(default_factory=list, max_length=12)
    appraisal: Appraisal | None = None
    corrections: list[MemoryCorrection] = Field(default_factory=list, max_length=8)
    attunement: Attunement | None = None
    image_request: ImageRequest | None = None


class CommitResult(Model):
    turn_id: str
    memory_ids: list[str]
    replayed: bool = False


NOTEBOOK_SECTIONS = {
    "identity": "Identity",
    "their_people": "Their people",
    "life_now": "Life right now",
    "emotional_landscape": "Emotional landscape",
    "drives": "What drives them",
    "open_threads": "Open threads",
    "dates": "Dates that matter",
    "us": "Us",
    "read_on_them": "Read on them",
    "communication": "How to talk to them",
    "pinned": "Pinned",
}


class NotebookEntry(Model):
    section: Literal[
        "identity",
        "their_people",
        "life_now",
        "emotional_landscape",
        "drives",
        "open_threads",
        "dates",
        "us",
        "read_on_them",
        "communication",
        "pinned",
    ]
    text: str = Field(min_length=1, max_length=800)
    source_ids: list[str] = Field(min_length=1, max_length=8)
    basis: Literal["user_statement", "shared_event", "interpretation", "host_observation"] = (
        "user_statement"
    )
    confidence: float = Field(default=1, ge=0, le=1)

    @model_validator(mode="after")
    def tentative_read(self):
        if self.section == "read_on_them" and self.basis != "interpretation":
            raise ValueError("Read on them must be marked as an interpretation")
        if self.basis in ("interpretation", "host_observation") and self.confidence >= 1:
            raise ValueError("Interpretations must preserve uncertainty")
        return self


class MemoryNotebook(Model):
    entries: list[NotebookEntry] = Field(default_factory=list, max_length=48)

    def render(self):
        lines = []
        for key, title in NOTEBOOK_SECTIONS.items():
            items = [e for e in self.entries if e.section == key]
            if items:
                lines.append("## " + title)
                for e in items:
                    prefix = "[Tentative interpretation] " if e.basis == "interpretation" else ""
                    lines.append("- " + prefix + e.text)
        return "\n".join(lines)

    @model_validator(mode="after")
    def bounded(self):
        if len(self.render()) > 6000:
            raise ValueError("Notebook exceeds 6000 characters; merge and curate")
        for section, limit in [("pinned", 10), ("open_threads", 5)]:
            if sum(e.section == section for e in self.entries) > limit:
                raise ValueError("Too many entries in notebook section")
        return self


class Usage(Model):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_usd: float | None = Field(default=None, ge=0)


class TemporalQuery(Model):
    mode: Literal["current", "history", "asof", "events", "observations"] = "current"
    query: str = Field(default="", max_length=2000)
    start: date | None = None
    end: date | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=200)

    @model_validator(mode="after")
    def valid_range(self):
        if self.mode in ("asof", "events", "observations") and self.start is None:
            raise ValueError("A resolved start date is required; do not guess unknown dates")
        if self.end and (not self.start or self.end < self.start):
            raise ValueError("Invalid date interval")
        if self.mode == "asof" and self.end and self.end != self.start:
            raise ValueError("asof takes one day")
        return self


class StyleSource(Model):
    memory_id: str
    revision: int = Field(ge=1)


class StyleRule(Model):
    kind: Literal["prefer", "avoid", "perception", "response", "intensity", "example"]
    text: str = Field(min_length=1, max_length=600)
    when: str = Field(default="", max_length=300)
    sources: list[StyleSource] = Field(min_length=1, max_length=6)


class ResponseStyle(Model):
    rules: list[StyleRule] = Field(default_factory=list, max_length=16)

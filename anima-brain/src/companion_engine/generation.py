"""Optional generation without auto-committing model-inferred memories."""

from .engine import Engine
from .models import CommitRequest, ObservedMedia, TurnContext
from .providers import Provider, ProviderError


def draft_reply(
    engine: Engine, character_id: str, provider: Provider, *, context: TurnContext | None = None
) -> dict:
    pending = engine.pending_turn(character_id)
    if not pending:
        raise ValueError("Prepare a turn before generating a draft")
    if context is None:
        with engine._lock:
            row = engine.db.execute(
                "SELECT data FROM turn_media WHERE turn_id=?", (pending["id"],)
            ).fetchone()
        import json

        context = engine.prepare_turn(
            character_id,
            pending["user_message"],
            request_id=pending["request_id"],
            media_observations=[ObservedMedia.model_validate(m) for m in json.loads(row[0])]
            if row
            else [],
        )
    if context.turn_id != pending["id"] or context.character.id != character_id:
        raise ValueError("Prepared context does not match the pending turn")
    try:
        result = provider.complete(context)
    except ProviderError:
        engine.record_usage(
            character_id,
            context.turn_id,
            getattr(provider, "model", "unknown"),
            None,
            status="failed_unknown",
        )
        raise
    # Record returned usage even if the model JSON is subsequently invalid.
    # Remote timeouts/malformed envelopes cannot be billed accurately here: never guess zero.
    engine.record_usage(
        character_id,
        context.turn_id,
        result.model,
        result.usage,
        status="reported" if result.usage else "unknown",
    )
    request = CommitRequest.model_validate_json(result.text)
    if request.image_request and request.image_request.request_quote not in context.user_message:
        raise ValueError("Image request must cite this user's current message")
    # Catch unsupported evidence before presenting a seemingly valid draft for approval.
    for proposal in [
        *request.memories,
        *[c.draft for c in request.corrections],
        *([request.appraisal] if request.appraisal else []),
        *([request.attunement] if request.attunement else []),
    ]:
        source_ref = getattr(proposal, "source_media_ref", None)
        observation = (
            next((m for m in context.media_observations if m.source_ref == source_ref), None)
            if source_ref
            else None
        )
        source = observation.observation if observation else context.user_message
        if source_ref and (
            observation is None or proposal.details.confidence > observation.confidence
        ):
            raise ValueError("Generated media memory lacks a valid uncertain observation")
        if proposal.source_quote not in source:
            raise ValueError(
                "Generated proposal cites text absent from this user turn; not committed"
            )
    if request.appraisal and not context.character.relationship_enabled:
        raise ValueError(
            "Generated appraisal for a character with relationship simulation disabled"
        )
    return {
        "turn_id": context.turn_id,
        "commit_request": request.model_dump(mode="json"),
        "usage": result.usage.model_dump() if result.usage else None,
        "usage_status": "reported" if result.usage else "unknown",
        "committed": False,
    }

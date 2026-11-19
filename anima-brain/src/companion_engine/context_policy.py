"""One budget applied before every reader call, including after follow-up retrieval."""

import json

MEMORY_FIELDS = (
    "temporal_memory",
    "memories",
    "evidence",
    "core_memory",
    "open_loops",
    "calendar",
    "response_style",
    "reaction_feedback",
    "recent_attunement",
    "memory_maintenance",
    "continuity",
)


def memory_size(context):
    values = context.model_dump(mode="json")
    return sum(
        len(json.dumps(values[key], ensure_ascii=False)) for key in MEMORY_FIELDS if values[key]
    )


def enforce_context_policy(engine, context):
    config = engine.memory_config
    if config is None:
        return context  # Existing host integrations retain their original policy.
    if not config.read_enabled or config.context_chars == 0:
        for field in MEMORY_FIELDS:
            setattr(context, field, [] if isinstance(getattr(context, field), list) else {})
        context.retrieval_mode = "disabled"
    else:
        context.omitted_memories += max(0, len(context.memories) - config.fact_limit)
        context.memories = context.memories[: config.fact_limit]
        context.evidence = context.evidence[: config.evidence_limit]
        context.core_memory = context.core_memory[: config.document_limit]
        # Inventory is a view of already delivered facts, not an uncapped second retrieval.
        if "current_facts" in context.continuity:
            context.continuity["current_facts"] = [
                {
                    key: m.model_dump(mode="json")[key]
                    for key in ("id", "key", "text", "revision", "details")
                }
                for m in context.memories
            ]
        # Drop optional historical metadata first, then complete lowest-priority items.
        # Never cut a source mid-sentence or leave half of a user/assistant pair.
        for field in (
            "continuity",
            "temporal_memory",
            "memory_maintenance",
            "recent_attunement",
            "reaction_feedback",
            "response_style",
            "calendar",
            "open_loops",
            "core_memory",
            "evidence",
            "memories",
        ):
            values = getattr(context, field)
            if isinstance(values, dict):
                if memory_size(context) > config.context_chars:
                    setattr(context, field, {})
            else:
                while values and memory_size(context) > config.context_chars:
                    values.pop()
                    if field == "memories":
                        context.omitted_memories += 1
    while (
        context.recent_turns
        and len(json.dumps(context.recent_turns, ensure_ascii=False)) > engine.recent_chars
    ):
        context.recent_turns.pop(0)
        context.omitted_recent_turns += 1
    # Total envelope includes persona, current message, observations and instructions.
    for field in (
        "recent_turns",
        "continuity",
        "temporal_memory",
        "core_memory",
        "evidence",
        "memories",
        "open_loops",
        "calendar",
        "response_style",
        "recent_attunement",
        "reaction_feedback",
        "memory_maintenance",
    ):
        values = getattr(context, field)
        if isinstance(values, dict):
            if len(context.model_dump_json()) > engine.context_chars:
                setattr(context, field, {})
        else:
            while values and len(context.model_dump_json()) > engine.context_chars:
                values.pop(0 if field == "recent_turns" else -1)
                if field == "recent_turns":
                    context.omitted_recent_turns += 1
                if field == "memories":
                    context.omitted_memories += 1
    if len(context.model_dump_json()) > engine.context_chars:
        raise ValueError("Persona, message and instructions exceed context_chars")
    return context

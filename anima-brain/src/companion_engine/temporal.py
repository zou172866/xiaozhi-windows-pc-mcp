"""Occurrence dates, observation dates and state validity are separate clocks."""

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import CharacterSpec, MemoryDetails, TemporalQuery


def local_today(engine, character_id, now=None):
    spec = CharacterSpec.model_validate_json(engine._character(character_id)["spec"])
    return (
        datetime.fromtimestamp(now, ZoneInfo(spec.timezone)).date()
        if now is not None
        else (datetime.now(ZoneInfo(spec.timezone)).date())
    )


def lookup(engine, character_id, request):
    request = TemporalQuery.model_validate(request)
    with engine._lock:
        spec = CharacterSpec.model_validate_json(engine._character(character_id)["spec"])
        if request.mode == "observations":
            zone = ZoneInfo(spec.timezone)
            lo = datetime.combine(request.start, time.min, zone).timestamp()
            hi = datetime.combine(
                (request.end or request.start) + timedelta(days=1), time.min, zone
            ).timestamp()
            matches, count = [], 0
            for row in engine.db.execute(
                "SELECT * FROM evidence WHERE owner=? AND character_id=? AND observed_at>=? "
                "AND observed_at<? ORDER BY observed_at,id",
                (engine.owner, character_id, lo, hi),
            ):
                if not _matches(row["text"], request.query):
                    continue
                if count >= request.offset:
                    matches.append(dict(row))
                count += 1
                if len(matches) > request.limit:
                    break
            out = engine.memory._pack(matches[: request.limit])
            out.update(
                time_basis="observation_time",
                timezone=spec.timezone,
                has_more=len(matches) > request.limit,
                next_offset=request.offset + request.limit
                if len(matches) > request.limit
                else None,
            )
            return out
        target = request.start or local_today(engine, character_id)
        found, unknown, count = [], [], 0
        # Stream current rows, including all revisions, rather than dropping facts after 200.
        for row in engine.db.execute(
            "SELECT * FROM memories WHERE owner=? AND character_id=? ORDER BY id",
            (engine.owner, character_id),
        ):
            current = engine._memory(row).model_dump(mode="json")
            versions = [current]
            if request.mode in ("history", "asof", "events"):
                versions += [
                    json.loads(r[0])
                    for r in engine.db.execute(
                        "SELECT previous FROM revisions WHERE memory_id=? ORDER BY revision DESC",
                        (row["id"],),
                    )
                    if json.loads(r[0]).get("invalidation") == "superseded"
                ]
            for item in versions:
                d = MemoryDetails.model_validate(item.get("details", {}))
                lifecycle = item.get("lifecycle", {})
                if lifecycle.get("invalidation") == "corrected":
                    continue
                if lifecycle.get("invalidation"):
                    item = {
                        **item,
                        "invalidation": lifecycle["invalidation"],
                        "valid_until": lifecycle.get("valid_until"),
                    }
                    if request.mode == "current":
                        continue
                if not _matches(item["text"], request.query):
                    continue
                # Direct inspection may show suppressed facts, with their policy retained.
                until = date.fromisoformat(item["valid_until"]) if item.get("valid_until") else None
                if request.mode == "current":
                    if d.temporal_kind == "historical" or (d.valid_from and d.valid_from > target):
                        continue
                elif request.mode == "asof":
                    if d.temporal_kind != "current" or not d.valid_from:
                        if len(unknown) < request.limit:
                            unknown.append(item)
                        continue
                    if d.valid_from > target or (until and target >= until):
                        continue
                    if (item is not current or lifecycle.get("invalidation")) and not until:
                        if len(unknown) < request.limit:
                            unknown.append(item)
                        continue
                elif request.mode == "events":
                    if d.event_date is None:
                        continue
                    if (d.event_end or d.event_date) < request.start or (
                        d.event_date > (request.end or request.start)
                    ):
                        continue
                if count >= request.offset:
                    found.append(item)
                count += 1
                if len(found) > request.limit:
                    break
            if len(found) > request.limit:
                break
        return {
            "facts": found[: request.limit],
            "uncertain_validity": unknown,
            "has_more": len(found) > request.limit,
            "next_offset": request.offset + request.limit if len(found) > request.limit else None,
            "time_basis": request.mode,
            "timezone": spec.timezone,
            "caution": "Unknown validity is not proof of a state at an exact date. "
            "Corrected errors are excluded; superseded facts describe the past. "
            "Suppressed followup policy remains binding. This is not a complete life timeline.",
        }


def _matches(text, query):
    from .engine import terms

    tokens = terms(query)
    return not tokens or any(token in text.casefold() for token in tokens)


def calendar(engine, character_id, *, now=None, days=7):
    today = local_today(engine, character_id, now)
    end = today + timedelta(days=days)
    items = []
    for row in engine.db.execute(
        "SELECT m.* FROM memories m JOIN memory_details d ON d.memory_id=m.id "
        "WHERE m.owner=? AND m.character_id=? AND json_extract(d.data,'$.event_date') IS NOT NULL",
        (engine.owner, character_id),
    ):
        m = engine._memory(row)
        d = m.details
        if (
            m.lifecycle.invalidation
            or d.followup == "never_unless_asked"
            or d.loop_status in ("suppressed", "stale")
        ):
            continue
        due = d.event_date
        if d.recurrence == "annual":
            candidates = []
            for year in (today.year, today.year + 1):
                try:
                    candidates.append(due.replace(year=year))
                except ValueError:  # February 29: do not invent a different anniversary.
                    pass
            due = next((v for v in candidates if v >= today), None)
        if due and today <= due <= end:
            items.append(
                {
                    "memory_id": m.id,
                    "text": m.text,
                    "date": str(due),
                    "kind": d.date_kind,
                    "days_away": (due - today).days,
                    "guidance": "Acknowledge gently; do not congratulate."
                    if d.date_kind == "gentle"
                    else "Mention only when contextually welcome.",
                }
            )
    return sorted(items, key=lambda item: (item["date"], item["memory_id"]))[:12]

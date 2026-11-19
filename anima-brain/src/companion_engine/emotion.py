"""Deterministic port of Anima emotion.ts: PAD, axes, habituation, daily
caps, seeded weather, absence easing and band hysteresis.

These are character-writing controls, not validated measurements of human emotion.
Time is supplied by the caller; reads never mutate or repeatedly apply decay.
"""

from copy import deepcopy
from datetime import datetime, timezone
from math import exp2

from .models import Appraisal

BASELINE = (0.15, -0.15, 0.05)
PADS = {
    "calm": (0.2, -0.35, 0.1),
    "warm": (0.45, -0.05, 0.1),
    "happy": (0.55, 0.3, 0.15),
    "curious": (0.25, 0.25, 0.05),
    "hurt": (-0.5, 0.15, -0.3),
    "sad": (-0.5, -0.25, -0.2),
    "guarded": (-0.1, 0.1, 0.3),
    "playful": (0.45, 0.4, 0.2),
    "tender": (0.5, -0.15, -0.05),
    "flustered": (0.2, 0.5, -0.25),
    "annoyed": (-0.35, 0.35, 0.2),
    "anxious": (-0.3, 0.45, -0.3),
}


def initial(now: float) -> dict:
    axes = dict(trust=0.08, warmth=0.1, familiarity=0.06, tension=0.0, romance=0.0)
    return dict(
        at=now,
        interaction_at=now,
        pad=list(BASELINE),
        axes=axes,
        day="",
        motion=dict.fromkeys(axes, 0.0),
        repeats=0,
        wobble_day="",
        bands=dict.fromkeys(axes, 0),
    )


def _bands(state):
    previous = state.get("bands", {})
    result = {}
    for key, value in state["axes"].items():
        thresholds = [0.15, 0.4, 0.7] if key == "tension" else [0.25, 0.5, 0.75]
        raw = sum(value >= t for t in thresholds)
        prev = previous.get(key, raw)
        if raw > prev and value < thresholds[raw - 1] + 0.035:
            raw = prev
        elif raw < prev and value >= thresholds[prev - 1] - 0.035:
            raw = prev
        result[key] = raw
    return result


def _seeded(seed):
    h = 2166136261
    raw = seed.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        h = ((h ^ int.from_bytes(raw[i : i + 2], "little")) * 16777619) & 0xFFFFFFFF
    return (h % 20000) / 10000 - 1


def decayed(state: dict, now: float, seed: str = "") -> dict:
    # Copy nested structures: preview must not change persisted state.
    hours = max(0, now - state["at"]) / 3600
    result = deepcopy(state)
    axes = result["axes"]
    axes["tension"] *= exp2(-hours / 72)
    # Separate interaction time from decay time so repeated reads/ticks cannot
    # multiply absence cooling. Continuous easing is an intentional port repair.
    last = state.get("interaction_at", state["at"])
    old_days = max(0, (state["at"] - last) / 86400 - 7)
    new_days = max(0, (max(now, state["at"]) - last) / 86400 - 7)
    anchor = axes["familiarity"] * 0.6
    if axes["warmth"] > anchor:
        axes["warmth"] = anchor + (axes["warmth"] - anchor) * exp2(-(new_days - old_days) / 35)
    result["at"] = max(now, state["at"])
    result["pad"] = [b + (v - b) * exp2(-hours / 12) for v, b in zip(state["pad"], BASELINE)]
    day = datetime.fromtimestamp(result["at"], timezone.utc).date().isoformat()
    if seed and result.get("wobble_day") != day:
        for index, axis, amplitude in [(0, "p", 0.08), (1, "a", 0.05)]:
            result["pad"][index] = max(
                -1, min(1, result["pad"][index] + _seeded(f"{seed}:{day}:{axis}") * amplitude)
            )
        result["wobble_day"] = day
    result["bands"] = _bands(result)
    return result


def advance(state: dict, appraisal: Appraisal, now: float, romance_enabled: bool) -> dict:
    result = decayed(state, now)
    day = datetime.fromtimestamp(result["at"], timezone.utc).date().isoformat()
    if day != result["day"]:
        result["day"] = day
        result["motion"] = dict.fromkeys(result["axes"], 0.0)
        result["repeats"] = 0
    pull = 0.3 * appraisal.intensity / 3
    result["pad"] = [
        v + pull * (target - v) for v, target in zip(result["pad"], PADS[appraisal.emotion])
    ]
    changes = dict(
        trust=appraisal.trust
        * (3.5 * (1 + 0.6 * (1 - result["axes"]["familiarity"])) if appraisal.trust < 0 else 1),
        warmth=appraisal.warmth / (1 + 0.6 * result.get("repeats", 0))
        if appraisal.warmth > 0
        else appraisal.warmth * 1.5,
        familiarity=appraisal.disclosure * 0.9,
        tension=appraisal.tension * (2.2 if appraisal.tension > 0 else 1),
        romance=appraisal.romance * 0.8
        if appraisal.romance > 0 and romance_enabled
        else min(0, appraisal.romance)
        if romance_enabled
        else 0,
    )
    for key, units in changes.items():
        # Production evaluates the gate AFTER this turn's familiarity movement.
        if key == "romance" and units > 0 and result["axes"]["familiarity"] <= 0.2:
            units = 0
        value = result["axes"][key]
        delta = units * 0.013 * (1 - 0.75 * value if units > 0 else 0.25 + 0.75 * value)
        remaining = max(0, 0.12 - result["motion"][key])
        delta = max(-remaining, min(remaining, delta))
        next_value = max(0, min(1, value + delta))
        result["axes"][key] = next_value
        result["motion"][key] += abs(next_value - value)
    result["interaction_at"] = result["at"]
    result["repeats"] = result.get("repeats", 0) + int(appraisal.warmth > 0)
    result["bands"] = _bands(result)
    return result


def note(state: dict, now: float, *, romance_enabled: bool = False) -> str:
    current = decayed(state, now)
    axes = current["axes"]
    bands = current["bands"]
    familiarity = [
        "barely acquainted",
        "getting acquainted",
        "know each other well",
        "a deeply familiar shared history",
    ][bands["familiarity"]]
    tension = [
        "",
        "Some unresolved friction remains.",
        "There is real unresolved strain.",
        "Significant tension needs room for repair, without guilt or pressure.",
    ][bands["tension"]]
    warmth = [
        "Keep warmth light and unassuming.",
        "Quiet friendliness fits.",
        "Genuine warmth is earned here.",
        "Tenderness fits this established relationship.",
    ][bands["warmth"]]
    trust = [
        "Do not presume deep trust.",
        "Trust is still being established.",
        "There is earned trust.",
        "There is deeply established trust.",
    ][bands["trust"]]
    mood = (
        "The character feels subdued."
        if current["pad"][0] < -0.15
        else "The character feels steady."
    )
    if current["pad"][1] > 0.25:
        mood += " A little more energy in their expression fits."
    romance = (
        "User-led romantic warmth is allowed; never escalate beyond their current intent."
        if romance_enabled and axes["romance"] > 0.15
        else "Do not assume romantic intimacy."
    )
    return (
        f"Character relationship context: {familiarity}. {warmth} {trust} "
        f"{mood} {tension} {romance} "
        "This is fictional character state, not evidence about the user's feelings. "
        "Follow their current boundaries; never demand attention or emotional repayment."
        " Character mood must never make the response dismissive during user vulnerability."
    ).strip()

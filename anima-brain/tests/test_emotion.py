import copy

import pytest

from companion_engine import Appraisal
from companion_engine.emotion import advance, decayed, initial


def test_production_reference_fixture():
    # Generated read-only from makima.io/convex/emotion.ts, 2026-10-29.
    state = initial(1728000000)
    ap = Appraisal(
        source_quote="thanks", emotion="warm", intensity=2, trust=1, warmth=2, disclosure=1
    )
    for _ in range(5):
        state = advance(state, ap, 1728000000, False)
    assert state["pad"] == pytest.approx([0.351696, -0.082768, 0.083616])
    assert state["axes"]["trust"] == pytest.approx(0.13992011011682284)
    assert state["axes"]["warmth"] == pytest.approx(0.16436462365279764)
    assert state["axes"]["familiarity"] == pytest.approx(0.11489559135750367)


def test_weather_once_daily_and_absence_partition_invariance():
    state = initial(1728000000)
    weather = decayed(state, 1728000000, "test-user")
    assert decayed(weather, 1728000000, "test-user") == weather
    state["axes"]["warmth"] = 0.8
    direct = decayed(state, 1728000000 + 20 * 86400)
    steps = decayed(decayed(state, 1728000000 + 10 * 86400), 1728000000 + 20 * 86400)
    assert steps["axes"] == pytest.approx(direct["axes"])


def test_daily_cap_and_decay_without_mutating_state():
    now = 1728000000.0
    state = initial(now)
    appraisal = Appraisal(source_quote="thanks", warmth=3, trust=3, disclosure=3, tension=3)
    for _ in range(100):
        state = advance(state, appraisal, now, romance_enabled=False)
    assert all(v <= 0.1200000001 for v in state["motion"].values())
    assert state["axes"]["romance"] == 0
    saved = copy.deepcopy(state)
    first = decayed(state, now + 3 * 86400)
    assert first == decayed(state, now + 3 * 86400)
    assert state == saved
    assert first["axes"]["tension"] == pytest.approx(state["axes"]["tension"] / 2)
    assert decayed(state, now - 86400) == state


def test_daily_cap_resets_and_all_values_bounded():
    now = 1728000000.0
    state = initial(now)
    for day in range(20):
        for _ in range(10):
            state = advance(
                state,
                Appraisal(
                    source_quote="x",
                    intensity=3,
                    emotion="hurt",
                    trust=-3,
                    warmth=-3,
                    tension=3,
                    disclosure=3,
                ),
                now + day * 86400,
                False,
            )
    assert all(0 <= v <= 1 for v in state["axes"].values())
    assert all(-1 <= v <= 1 for v in state["pad"])

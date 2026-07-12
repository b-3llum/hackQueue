"""The leaderboard math — the part of the bot that gets bug reports."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hackqueue.services.scoring import (
    Period,
    composite_scores,
    normalize_max,
    period_start,
    points_delta,
)


def dt(day: int, hour: int = 12, month: int = 7, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ── period_start ─────────────────────────────────────────────────────────────


def test_weekly_starts_monday_midnight_utc():
    # 2026-07-12 is a Sunday; its week began Monday 2026-07-06.
    assert period_start(Period.WEEKLY, dt(12)) == datetime(2026, 7, 6, tzinfo=UTC)


def test_weekly_on_monday_is_same_day():
    assert period_start(Period.WEEKLY, dt(6, hour=0)) == datetime(2026, 7, 6, tzinfo=UTC)


def test_monthly_starts_first_of_month():
    assert period_start(Period.MONTHLY, dt(12)) == datetime(2026, 7, 1, tzinfo=UTC)


def test_alltime_has_no_start():
    assert period_start(Period.ALLTIME, dt(12)) is None


def test_period_start_converts_to_utc():
    from datetime import timedelta, timezone

    # 2026-07-06 01:00 +03:00 is 2026-07-05 22:00 UTC — still the PREVIOUS week.
    local = datetime(2026, 7, 6, 1, tzinfo=timezone(timedelta(hours=3)))
    assert period_start(Period.WEEKLY, local) == datetime(2026, 6, 29, tzinfo=UTC)


# ── points_delta ─────────────────────────────────────────────────────────────

WEEK_START = datetime(2026, 7, 6, tzinfo=UTC)


def test_empty_history_is_zero():
    assert points_delta([], WEEK_START) == 0
    assert points_delta([], None) == 0


def test_alltime_is_latest_value():
    history = [(dt(1), 100), (dt(10), 250)]
    assert points_delta(history, None) == 250


def test_delta_uses_last_snapshot_before_period_as_baseline():
    history = [(dt(3), 100), (dt(5), 120), (dt(8), 180)]
    assert points_delta(history, WEEK_START) == 60  # 180 - 120, not 180 - 100


def test_new_member_baseline_is_first_snapshot_in_period():
    # Linked mid-week with 500 pre-existing points: those must not count.
    history = [(dt(8), 500), (dt(10), 560)]
    assert points_delta(history, WEEK_START) == 60


def test_single_snapshot_in_period_scores_zero():
    assert points_delta([(dt(8), 500)], WEEK_START) == 0


def test_all_snapshots_before_period_scores_zero():
    history = [(dt(1), 100), (dt(4), 150)]
    assert points_delta(history, WEEK_START) == 0


def test_negative_delta_floors_at_zero():
    # Retired boxes / rebalances can shrink platform scores.
    history = [(dt(5), 200), (dt(8), 150)]
    assert points_delta(history, WEEK_START) == 0


def test_snapshot_exactly_at_period_start_is_baseline():
    history = [(WEEK_START, 100), (dt(8), 130)]
    assert points_delta(history, WEEK_START) == 30


# ── normalize_max ────────────────────────────────────────────────────────────


def test_normalize_empty():
    assert normalize_max({}) == {}


def test_normalize_leader_is_100():
    result = normalize_max({"a": 50.0, "b": 25.0, "c": 0.0})
    assert result == {"a": 100.0, "b": 50.0, "c": 0.0}


def test_normalize_all_zero_no_division_crash():
    assert normalize_max({"a": 0.0, "b": 0.0}) == {"a": 0.0, "b": 0.0}


def test_normalize_negative_values_clamp_to_zero():
    result = normalize_max({"a": -5.0, "b": 10.0})
    assert result == {"a": 0.0, "b": 100.0}


# ── composite_scores ─────────────────────────────────────────────────────────


def test_composite_equal_weights():
    scores = composite_scores(
        {"htb": {1: 100.0, 2: 50.0}, "thm": {1: 0.0, 2: 30.0}},
        {"htb": 1.0, "thm": 1.0},
    )
    # member 1: (100 + 0)/2 = 50 ; member 2: (50 + 100)/2 = 75
    assert scores[1] == pytest.approx(50.0)
    assert scores[2] == pytest.approx(75.0)


def test_composite_member_missing_from_platform_scores_zero_there():
    scores = composite_scores(
        {"htb": {1: 100.0}, "thm": {2: 40.0}},
        {"htb": 1.0, "thm": 1.0},
    )
    assert scores[1] == pytest.approx(50.0)
    assert scores[2] == pytest.approx(50.0)


def test_composite_weighting():
    scores = composite_scores(
        {"htb": {1: 100.0, 2: 0.0}, "claims": {1: 0.0, 2: 10.0}},
        {"htb": 3.0, "claims": 1.0},
    )
    assert scores[1] == pytest.approx(75.0)  # 100*3 / 4
    assert scores[2] == pytest.approx(25.0)  # 100*1 / 4


def test_composite_zero_weight_platform_excluded():
    scores = composite_scores(
        {"htb": {1: 100.0}, "thm": {2: 999.0}},
        {"htb": 1.0, "thm": 0.0},
    )
    assert scores == {1: pytest.approx(100.0)}


def test_composite_dead_platform_dilutes_instead_of_inflating():
    # THM degraded all week -> all zeros. Member 1's htb lead shouldn't
    # become a 100 composite; the dead platform still carries its weight.
    scores = composite_scores(
        {"htb": {1: 100.0, 2: 50.0}, "thm": {1: 0.0, 2: 0.0}},
        {"htb": 1.0, "thm": 1.0},
    )
    assert scores[1] == pytest.approx(50.0)


def test_composite_unknown_platform_defaults_to_weight_one():
    scores = composite_scores({"newplat": {1: 10.0}}, {"htb": 1.0})
    assert scores[1] == pytest.approx(100.0)


def test_composite_all_platforms_zero():
    scores = composite_scores({"htb": {1: 0.0, 2: 0.0}}, {"htb": 1.0})
    assert scores == {1: 0.0, 2: 0.0}


def test_composite_no_platforms():
    assert composite_scores({}, {"htb": 1.0}) == {}

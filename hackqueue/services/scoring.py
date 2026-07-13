"""Pure leaderboard math. No I/O, no Discord, no SQLAlchemy — everything here
takes plain values and returns plain values, so it can be tested exhaustively.

Definitions (documented in the README so servers can audit their boards):

- The **baseline** for a period is the last snapshot at/before the period
  start. A member with no snapshot before the period (they linked mid-period)
  uses their first snapshot *inside* it — so pre-existing points never count
  as period gains.
- A **delta** is latest minus baseline, floored at 0 (platforms occasionally
  shrink scores via retirements/rebalances; a board should never show
  negative "gains").
- **Composite**: per platform, member deltas are normalized within the server
  to 0-100 (top gainer = 100; a platform where nobody gained contributes 0),
  then combined as a weighted average using scoring.toml weights — the result
  is itself on a 0-100 scale.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TypeVar

K = TypeVar("K")

#: (taken_at, points) — must be sorted ascending by time.
PointHistory = Sequence[tuple[datetime, int]]


class Period(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ALLTIME = "alltime"


def period_start(period: Period, now: datetime) -> datetime | None:
    """UTC start of the current period; None means all-time (no baseline)."""
    now = now.astimezone(UTC)
    if period is Period.WEEKLY:
        monday = now.date() - timedelta(days=now.weekday())
        return datetime(monday.year, monday.month, monday.day, tzinfo=UTC)
    if period is Period.MONTHLY:
        return datetime(now.year, now.month, 1, tzinfo=UTC)
    return None


def points_delta(history: PointHistory, start: datetime | None) -> int:
    """Point gain over the period per the baseline rules above.

    With ``start=None`` (all-time) this is simply the latest value.
    """
    if not history:
        return 0
    latest = history[-1][1]
    if start is None:
        return latest
    baseline: int | None = None
    for taken_at, points in history:
        if taken_at <= start:
            baseline = points
        else:
            break
    if baseline is None:
        baseline = history[0][1]
    return max(0, latest - baseline)


def normalize_max(values: Mapping[K, float]) -> dict[K, float]:
    """Scale values to 0-100 with max-scaling. All-zero (or negative-only)
    input maps everyone to 0 rather than dividing by zero."""
    top = max(values.values(), default=0.0)
    if top <= 0:
        return dict.fromkeys(values, 0.0)
    return {k: (v / top) * 100.0 if v > 0 else 0.0 for k, v in values.items()}


def composite_breakdown(
    platform_values: Mapping[str, Mapping[K, float]],
    weights: Mapping[str, float],
) -> dict[K, dict[str, float]]:
    """Each member's composite score, split into what each platform contributed.

    A member's contributions sum to their composite score, which is what lets
    the web board draw a stacked bar that *is* the formula.

    Every platform present in ``platform_values`` participates with weight
    ``weights.get(platform, 1.0)`` — including platforms where all deltas are
    zero, so one dead platform dilutes rather than inflates the rest.
    Members missing from a platform contribute 0 there.
    """
    included = {p: v for p, v in platform_values.items() if weights.get(p, 1.0) > 0}
    total_weight = sum(weights.get(p, 1.0) for p in included)
    if total_weight <= 0:
        return {}
    members: set[K] = set()
    for values in included.values():
        members.update(values.keys())
    normalized = {p: normalize_max(v) for p, v in included.items()}
    return {
        m: {p: normalized[p].get(m, 0.0) * weights.get(p, 1.0) / total_weight for p in included}
        for m in members
    }


def composite_scores(
    platform_values: Mapping[str, Mapping[K, float]],
    weights: Mapping[str, float],
) -> dict[K, float]:
    """Weighted average of per-platform normalized scores (0-100)."""
    return {
        member: sum(parts.values())
        for member, parts in composite_breakdown(platform_values, weights).items()
    }

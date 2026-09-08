"""Was the forecast right?

Deliberately separate from "was the suggestion useful?", which lives in the
feedback table and can only come from the user. The occupancy sensor counts
bodies at a doorway, not identities, so nothing here can tell whether a
particular person showed up. Conflating the two makes both unanalysable.

Everything in this module is derivable from stored readings, so it needs no
user input and can be run retroactively.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import mean, median

from density import percentage

# Below this many readings, a window was not observed well enough to score.
# The collector can miss a stretch; a window with two readings should not be
# scored as confidently as one with twelve.
MIN_WINDOW_SAMPLES = 4


@dataclass
class Score:
    predicted_pct: float
    actual_pct: float
    samples: int

    @property
    def error(self) -> float:
        """Signed: positive means the forecast was too high"""
        return self.predicted_pct - self.actual_pct

    @property
    def absolute_error(self) -> float:
        return abs(self.error)


def observed_pct(readings, start: datetime, end: datetime):
    """Mean occupancy actually observed in [start, end), or None if too sparse"""
    inside = [
        percentage(r.count, r.capacity)
        for r in readings
        if r.capacity and start <= r.observed_at < end
    ]
    if len(inside) < MIN_WINDOW_SAMPLES:
        return None
    return mean(inside)


def score(readings, predicted_pct: float, start: datetime, end: datetime):
    """Compare one prediction against what happened. None if unscoreable."""
    actual = observed_pct(readings, start, end)
    if actual is None:
        return None
    inside = sum(1 for r in readings if r.capacity and start <= r.observed_at < end)
    return Score(predicted_pct=predicted_pct, actual_pct=actual, samples=inside)


def forecast_for(readings, target: date, tz, bucket_minutes: int, before: datetime):
    """What the typical-weekday curve would have said about `target`.

    `before` cuts off the history, so a backtest cannot see past the day it is
    predicting. Returns {bucket: median_pct}, plus how many prior instances of
    that weekday backed it and how much they disagreed.
    """
    buckets: dict[int, list[float]] = {}
    days: set[date] = set()
    for reading in readings:
        if not reading.capacity or reading.observed_at >= before:
            continue
        local = reading.observed_at.astimezone(tz)
        if local.weekday() != target.weekday() or local.date() == target:
            continue
        days.add(local.date())
        slot = (local.hour * 60 + local.minute) // bucket_minutes
        buckets.setdefault(slot, []).append(percentage(reading.count, reading.capacity))

    curve = {slot: median(values) for slot, values in buckets.items()}
    spread = spread_of(buckets)
    return curve, len(days), spread


def spread_of(buckets: dict[int, list[float]]):
    """Typical disagreement within a bucket, as a mean of per-bucket ranges.

    A count of instances alone is a crude confidence signal: three weekdays
    that agree and three that range 20%-70% both pass a "three or more" gate.
    """
    ranges = [max(values) - min(values) for values in buckets.values() if len(values) > 1]
    return mean(ranges) if ranges else None


def backtest(readings, target: date, tz, bucket_minutes: int, window_minutes: int = 60):
    """Score the curve against one day it never saw.

    Returns None when there is not enough prior history, mirroring the rule
    that the curve stays hidden below three prior instances.
    """
    midnight = datetime(target.year, target.month, target.day, tzinfo=tz)
    curve, weeks, spread = forecast_for(
        readings, target, tz, bucket_minutes, before=midnight
    )
    if weeks < 3 or not curve:
        return None

    scores = []
    for slot, predicted in sorted(curve.items()):
        start = midnight + timedelta(minutes=slot * bucket_minutes)
        result = score(readings, predicted, start, start + timedelta(minutes=bucket_minutes))
        if result:
            scores.append(result)
    if not scores:
        return None

    return {
        "date": target,
        "basis_weeks": weeks,
        "basis_spread": spread,
        "buckets_scored": len(scores),
        "mean_absolute_error": mean(s.absolute_error for s in scores),
        "bias": mean(s.error for s in scores),
    }

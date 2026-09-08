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
from statistics import mean, median, quantiles

from density import percentage

# Below this many readings, a window was not observed well enough to score.
# The collector can miss a stretch; a window with two readings should not be
# scored as confidently as one with twelve.
MIN_WINDOW_SAMPLES = 4

# Below this many instances, quartiles are interpolated from too few points to
# mean anything, so dispersion falls back to the range.
IQR_MIN_INSTANCES = 8


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


def weekday_bands(readings, target: date, tz, bucket_minutes: int, before=None):
    """What prior instances of `target`'s weekday looked like, per time bucket.

    The single source of truth for the typical-weekday curve: the dashboard
    draws it and the backtest scores it, so they cannot drift apart.

    Each bucket carries a median and the range across instances. A count alone
    is a crude confidence signal -- three weekdays that agree closely and three
    that range 20%-70% both pass a "three or more" gate, and only the range
    distinguishes them.

    `before` cuts off history so a backtest cannot see past the day it is
    predicting. Omit it for display, where all history except the viewed day
    is fair game.

    Returns (bands, instance_count, day_level_spread).
    """
    buckets: dict[int, list[float]] = {}
    days: set[date] = set()
    for reading in readings:
        if not reading.capacity:
            continue
        if before is not None and reading.observed_at >= before:
            continue
        local = reading.observed_at.astimezone(tz)
        if local.weekday() != target.weekday() or local.date() == target:
            continue
        days.add(local.date())
        slot = (local.hour * 60 + local.minute) // bucket_minutes
        buckets.setdefault(slot, []).append(percentage(reading.count, reading.capacity))

    bands = {
        slot: {"median": median(values), "low": min(values), "high": max(values)}
        for slot, values in buckets.items()
    }
    return bands, len(days), spread_of(buckets)


def band_spread(bands, slots) -> float | None:
    """Disagreement across just the buckets a suggestion would cover.

    Gating reads this, not the day-level figure: a day can be predictable at
    9am and chaotic at 6pm, and an average across the whole day would either
    block good windows or wave through bad ones.
    """
    widths = [bands[slot]["high"] - bands[slot]["low"] for slot in slots if slot in bands]
    return mean(widths) if widths else None


def dispersion(values: list[float]):
    """How much instances of a weekday disagree about one time bucket.

    Range below IQR_MIN_INSTANCES, interquartile range above it. The switch is
    not cosmetic: the expected value of a range grows with sample count even
    when the underlying variability is unchanged, because more samples means
    more chances to catch a tail. Comparing a range from 3 instances against
    one from 12 would suggest the gym had become less predictable when only n
    had changed -- and that comparison is exactly what the stored basis_spread
    is for.

    IQR rather than standard deviation: occupancy is bounded and skewed near
    opening and closing, and IQR ignores the one-off closure for the same
    reason the curve itself uses a median.
    """
    if len(values) < 2:
        return None
    if len(values) < IQR_MIN_INSTANCES:
        return max(values) - min(values)
    lower, _, upper = quantiles(values, n=4)
    return upper - lower


def spread_of(buckets: dict[int, list[float]]):
    """Day-level disagreement: the mean of per-bucket dispersions.

    A count of instances alone is a crude confidence signal -- three weekdays
    that agree and three that range 20%-70% both pass a "three or more" gate.
    """
    spreads = [d for values in buckets.values() if (d := dispersion(values)) is not None]
    return mean(spreads) if spreads else None


def backtest(readings, target: date, tz, bucket_minutes: int, window_minutes: int = 60):
    """Score the curve against one day it never saw.

    Returns None when there is not enough prior history, mirroring the rule
    that the curve stays hidden below three prior instances.
    """
    midnight = datetime(target.year, target.month, target.day, tzinfo=tz)
    bands, weeks, spread = weekday_bands(
        readings, target, tz, bucket_minutes, before=midnight
    )
    if weeks < 3 or not bands:
        return None

    scores = []
    for slot, band in sorted(bands.items()):
        predicted = band["median"]
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

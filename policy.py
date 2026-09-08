"""Turning a forecast into a recommendation.

Kept separate from the model on purpose. A perfectly accurate forecast can
still produce a useless suggestion -- "the quietest hour is 9:55-10:55pm" was
correct and unusable, because a session starting then cannot finish before
closing. Model errors and policy errors look identical from outside and have
nothing to do with each other, so they are fixed in different places.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import evaluate

SESSION_MINUTES = 60
# A session must finish and leave before closing. This is arithmetic, not
# taste: arriving an hour before close means being turned out mid-set. Note
# there is no matching rule at opening -- an empty gym at 7am is a genuinely
# good recommendation, because a full session fits.
WIND_DOWN_MINUTES = 15
# Above this, say nothing. "Go at 2pm, it will be 90% full" answers the
# question asked and is useless as advice.
CROWDING_CEILING = 0.85
MAX_SUGGESTIONS = 3


@dataclass
class Window:
    start: datetime
    end: datetime
    predicted_pct: float
    spread: float | None


@dataclass
class Suggestions:
    windows: list[Window]
    # Why there is nothing to suggest, for an honest empty state
    refusal: str | None = None


def overlaps(start, end, other_start, other_end) -> bool:
    return start < other_end and other_start < end


def candidate_windows(bands, midnight, day_hours, bucket_minutes,
                      session=SESSION_MINUTES, busy=()):
    """Every window that could legitimately be suggested, unranked.

    Windows start on bucket boundaries: the forecast has that resolution, and
    proposing 2:07pm would imply a precision the model does not have.
    """
    if not day_hours or day_hours.opens is None or day_hours.closes is None:
        return []

    opens, closes = day_hours.opens, day_hours.closes
    if closes <= opens:
        closes += 24 * 60
    latest_start = closes - session - WIND_DOWN_MINUTES

    windows = []
    start_minute = -(-opens // bucket_minutes) * bucket_minutes   # round up
    while start_minute <= latest_start:
        slots = [
            (start_minute + offset) // bucket_minutes
            for offset in range(0, session, bucket_minutes)
        ]
        if all(slot in bands for slot in slots):
            start = midnight + timedelta(minutes=start_minute)
            end = start + timedelta(minutes=session)
            if not any(overlaps(start, end, b_start, b_end) for b_start, b_end in busy):
                windows.append(
                    Window(
                        start=start,
                        end=end,
                        predicted_pct=sum(bands[s]["median"] for s in slots) / len(slots),
                        spread=evaluate.band_spread(bands, slots),
                    )
                )
        start_minute += bucket_minutes
    return windows


def suggest(bands, midnight, day_hours, bucket_minutes, busy=(),
            limit=MAX_SUGGESTIONS, ceiling=CROWDING_CEILING):
    """The quietest non-overlapping windows, or a reason there are none.

    Ranking is by predicted occupancy alone among eligible windows, rather
    than a weighted score: any weights would be invented, with no data yet to
    fit them against.
    """
    if not bands:
        return Suggestions([], refusal="no forecast for this day yet")
    if not day_hours or day_hours.opens is None:
        return Suggestions([], refusal="closed")

    candidates = candidate_windows(bands, midnight, day_hours, bucket_minutes, busy=busy)
    if not candidates:
        return Suggestions([], refusal="no free window long enough")

    within_ceiling = [w for w in candidates if w.predicted_pct <= ceiling]
    if not within_ceiling:
        return Suggestions([], refusal="every window is predicted busier than the ceiling")

    chosen: list[Window] = []
    # Greedy and non-overlapping: ranking alone would return 2:00, 2:30 and
    # 3:00 as three names for one recommendation.
    for window in sorted(within_ceiling, key=lambda w: w.predicted_pct):
        if any(overlaps(window.start, window.end, c.start, c.end) for c in chosen):
            continue
        chosen.append(window)
        if len(chosen) == limit:
            break
    return Suggestions(sorted(chosen, key=lambda w: w.start))

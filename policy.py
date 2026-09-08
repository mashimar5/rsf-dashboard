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

# Parts of the day, by the minute a window starts. One suggestion from each
# gives genuinely different choices to match against a schedule; the three
# quietest windows overall are nearly always consecutive, because the quiet
# part of a day is one contiguous stretch rather than three scattered ones.
# Three per section would reintroduce that same clustering one level down.
SECTIONS = (
    ("Morning", 0, 12 * 60),
    ("Afternoon", 12 * 60, 17 * 60),
    ("Evening", 17 * 60, 24 * 60),
)


@dataclass
class Window:
    start: datetime
    end: datetime
    predicted_pct: float
    spread: float | None
    section: str | None = None


@dataclass
class Suggestions:
    windows: list[Window]
    # Why there is nothing to suggest, for an honest empty state
    refusal: str | None = None


def overlaps(start, end, other_start, other_end) -> bool:
    return start < other_end and other_start < end


def candidate_windows(bands, midnight, day_hours, bucket_minutes,
                      session=SESSION_MINUTES, busy=(), not_before=None):
    """Every window that could legitimately be suggested, unranked.

    Windows start on bucket boundaries: the forecast has that resolution, and
    proposing 2:07pm would imply a precision the model does not have.

    `not_before` drops windows that have already started -- on today's view
    the question is what to do with the rest of the day, and 7am is not a
    suggestion at 5pm.
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
            already_passed = not_before is not None and start < not_before
            if not already_passed and not any(
                overlaps(start, end, b_start, b_end) for b_start, b_end in busy
            ):
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


def suggest(bands, midnight, day_hours, bucket_minutes, busy=(), not_before=None,
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

    candidates = candidate_windows(bands, midnight, day_hours, bucket_minutes,
                                   busy=busy, not_before=not_before)
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


def section_of(start_minute: int):
    for name, begins, ends in SECTIONS:
        if begins <= start_minute < ends:
            return name
    return SECTIONS[-1][0]          # a closing time past midnight


def suggest_by_section(bands, midnight, day_hours, bucket_minutes, busy=(), not_before=None,
                       ceiling=CROWDING_CEILING):
    """The quietest eligible window in each part of the day.

    A section with no eligible window is simply absent rather than padded --
    on a packed day there may genuinely be no free morning hour, and an empty
    slot would read as a failure rather than a fact.
    """
    if not bands:
        return Suggestions([], refusal="no forecast for this day yet")
    if not day_hours or day_hours.opens is None:
        return Suggestions([], refusal="closed")

    candidates = [
        window
        for window in candidate_windows(bands, midnight, day_hours, bucket_minutes,
                                        busy=busy, not_before=not_before)
        if window.predicted_pct <= ceiling
    ]
    if not candidates:
        return Suggestions([], refusal="nothing left today that is quiet enough")

    best: dict[str, Window] = {}
    for window in candidates:
        minute = int((window.start - midnight).total_seconds() // 60)
        window.section = section_of(minute)
        if window.section not in best or window.predicted_pct < best[window.section].predicted_pct:
            best[window.section] = window

    return Suggestions(sorted(best.values(), key=lambda w: w.start))

"""A random forest against the typical-weekday curve.

The curve (store.weekday_bands) is the median of the last eight comparable
days, and it is hard to beat: it shrugs off a bad day and already compares
term with term. What a median of past instances cannot express is anything
that moves between them -- the first weeks of term running hotter than the
last, a year busier than the one before, a week that has been unusually
quiet. A forest can use those signals. This module finds out whether they are
worth anything, and the comparison is built to be fair before it is built to
be flattering:

- Forecasts are day-ahead. Every feature for a day comes from before that
  day, including the curve itself, which the forest is given as a feature.
  The curve is recomputed in memory by the dashboard query's own rules, and a
  test holds the two equal.
- The forest is retrained each month on everything before that month, as it
  would run for real, and never sees the month it is scored on.
- Both are scored on the same hours of the same days, against the same hourly
  means, with evaluate's four-reading minimum and the dashboard's
  three-instance gate.
- FOREST_PARAMS were fixed before any test-year day was scored. The second
  half of 2022 (tools/backtest_forest.py --dev) checked them, and they were
  kept rather than tuned on so little data.

numpy and scikit-learn are offline dependencies (requirements-ml.txt),
imported only inside the functions that need them. The running app never
imports this module, and the production machine has no memory to spare.
"""

import csv
import math
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

import evaluate
import store

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
BUCKET_MINUTES = 60           # hourly: half an hour of ten-minute history never has four readings
FIRST_DAY = date(2021, 9, 6)  # the first day of clean imported history
MIN_INSTANCES = 3             # the dashboard's gate, and evaluate.backtest's
MISSING = -1.0                # occupancy is never negative, so a tree can split an absence off
PERIOD_KINDS = ("instruction", "rrr", "finals", "break", "summer", "holiday")

FOREST_PARAMS = {
    "n_estimators": 300,
    "min_samples_leaf": 5,
    "max_features": 0.5,
    "n_jobs": -1,
    "random_state": 0,
}

FEATURES = [
    "hour", "weekday",
    *(f"period_{kind}" for kind in PERIOD_KINDS),
    "days_into_period", "period_progress", "days_since_term_start",
    "day_of_year", "days_since_start",
    "same_hour_yesterday", "same_hour_last_week", "same_hour_week_mean", "yesterday_mean",
    "curve_median", "curve_spread", "curve_instances",
]


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    kind: str


@dataclass
class Row:
    """One local hour of one day: what was knowable the night before, and what happened."""
    day: date
    hour: int
    kind: str | None
    features: list[float]
    actual: float | None   # None when the hour has too few readings to learn from or score
    curve: float | None    # the dashboard curve's median for this hour
    weeks: int             # how many past instances the curve was built from


@dataclass(frozen=True)
class Forecast:
    day: date
    hour: int
    kind: str | None
    actual: float
    curve: float
    forest: float


@dataclass(frozen=True)
class DayScore:
    day: date
    kind: str
    curve_error: float
    forest_error: float
    curve_bias: float
    forest_bias: float


@dataclass(frozen=True)
class Comparison:
    kind: str
    days: int
    curve_error: float
    forest_error: float
    curve_bias: float
    forest_bias: float
    forest_better: float   # share of days on which the forest's error was lower


def hourly_occupancy(conn) -> dict[tuple[date, int], tuple[float, int]]:
    """Mean occupancy and reading count for every local hour on record."""
    rows = conn.execute(
        """SELECT (observed_at AT TIME ZONE %(zone)s)::date AS day,
                  EXTRACT(HOUR FROM observed_at AT TIME ZONE %(zone)s)::int AS hour,
                  AVG(count::float / capacity) AS pct,
                  COUNT(*) AS readings
           FROM occupancy
           WHERE capacity > 0
           GROUP BY 1, 2""",
        {"zone": str(LOCAL_TZ)},
    ).fetchall()
    return {(row["day"], row["hour"]): (row["pct"], row["readings"]) for row in rows}


def observed(hourly, day: date, hour: int) -> float | None:
    """The hour's mean, if it has enough readings to count as an answer."""
    found = hourly.get((day, hour))
    return found[0] if found and found[1] >= evaluate.MIN_WINDOW_SAMPLES else None


def calendar_periods(path=None) -> list[Period]:
    with open(path or store.CALENDAR_PATH, newline="") as handle:
        return [Period(date.fromisoformat(r["start"]), date.fromisoformat(r["end"]), r["kind"])
                for r in csv.DictReader(handle)]


def calendar_features(day: date, kind: str | None, periods: list[Period]) -> list[float]:
    """Where a day sits in the academic year: its kind, and how far into it."""
    containing = [p for p in periods if p.kind != "holiday" and p.start <= day <= p.end]
    if containing:
        period = max(containing, key=lambda p: store.PERIOD_RANK[p.kind])
        into = float((day - period.start).days)
        progress = into / ((period.end - period.start).days + 1)
    else:
        into = progress = MISSING
    term_starts = [p.start for p in periods if p.kind == "instruction" and p.start <= day]
    since_term = float((day - max(term_starts)).days) if term_starts else MISSING
    return [
        *(1.0 if kind == each else 0.0 for each in PERIOD_KINDS),
        into, progress, since_term,
        float(day.timetuple().tm_yday), float((day - FIRST_DAY).days),
    ]


def _or_missing(value):
    return MISSING if value is None else value


def recent_features(hourly, day: date, hour: int) -> list[float]:
    """The same hour yesterday and a week ago, that hour's week, and yesterday's level."""
    week = [value for back in range(1, 8)
            if (value := observed(hourly, day - timedelta(days=back), hour)) is not None]
    yesterday = [value for each in range(24)
                 if (value := observed(hourly, day - timedelta(days=1), each)) is not None]
    return [
        _or_missing(observed(hourly, day - timedelta(days=1), hour)),
        _or_missing(observed(hourly, day - timedelta(days=7), hour)),
        mean(week) if week else MISSING,
        mean(yesterday) if yesterday else MISSING,
    ]


def calendar_kinds(conn) -> dict[date, str]:
    """The labels store.weekday_bands matches on: calendar_days, synced from the CSV."""
    return {row["day"]: row["kind"] for row in conn.execute("SELECT day, kind FROM calendar_days")}


def weekday_index(hourly) -> dict[int, list[date]]:
    """Days with any reading, by ISO weekday, oldest first."""
    index = defaultdict(list)
    for day in sorted({day for day, _ in hourly}):
        index[day.isoweekday()].append(day)
    return index


def percentile_cont(values: list[float], fraction: float) -> float:
    """Postgres's percentile_cont: linear interpolation between the nearest ranks."""
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def curve_bands(hourly, kinds, index, day: date, window: int = evaluate.WINDOW_INSTANCES):
    """store.weekday_bands for `day` as of the night before, in hourly buckets.

    Computed from hourly means already in memory, because one query per day
    took six and a half minutes from the production machine. The rules are the
    query's, and a test holds the two equal: same-weekday days before `day`
    that have any reading, restricted to `day`'s kind of period when the
    calendar knows it, the most recent `window` of them, and for each hour the
    percentile_cont quartiles, the range and the count across those days.
    """
    target_kind = kinds.get(day)
    candidates = index.get(day.isoweekday(), [])
    recent = []
    for earlier in reversed(candidates[:bisect_left(candidates, day)]):
        if target_kind is None or kinds.get(earlier) == target_kind:
            recent.append(earlier)
            if len(recent) == window:
                break
    bands = {}
    for hour in range(24):
        values = [hourly[(each, hour)][0] for each in recent if (each, hour) in hourly]
        if values:
            bands[hour] = {
                "median": percentile_cont(values, 0.5),
                "q1": percentile_cont(values, 0.25),
                "q3": percentile_cont(values, 0.75),
                "low": min(values), "high": max(values), "n": len(values),
            }
    return bands, len(recent)


def build_rows(conn, start: date, end: date) -> list[Row]:
    """Every hour of every day in [start, end], with its day-ahead features."""
    hourly = hourly_occupancy(conn)
    kinds = calendar_kinds(conn)
    index = weekday_index(hourly)
    periods = calendar_periods()
    rows = []
    day = start
    while day <= end:
        # the dashboard's own curve, exactly as it stood the night before
        bands, weeks = curve_bands(hourly, kinds, index, day)
        kind = kinds.get(day)
        calendar = calendar_features(day, kind, periods)
        for hour in range(24):
            band = bands.get(hour)
            spread = evaluate.band_dispersion(band) if band else None
            rows.append(Row(
                day=day, hour=hour, kind=kind,
                features=[float(hour), float(day.weekday()), *calendar,
                          *recent_features(hourly, day, hour),
                          band["median"] if band else MISSING,
                          _or_missing(spread), float(weeks)],
                actual=observed(hourly, day, hour),
                curve=band["median"] if band else None,
                weeks=weeks,
            ))
        day += timedelta(days=1)
    return rows


def months(start: date, end: date):
    """(first, last) day of each calendar month overlapping [start, end]."""
    first = start
    while first <= end:
        following = (first.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield first, min(end, following - timedelta(days=1))
        first = following


def scoreable(row: Row) -> bool:
    return row.actual is not None and row.curve is not None and row.weeks >= MIN_INSTANCES


def default_model():
    from sklearn.ensemble import RandomForestRegressor

    return RandomForestRegressor(**FOREST_PARAMS)


def walk_forward(rows: list[Row], start: date, end: date, model_factory=default_model) -> list[Forecast]:
    """Forecast every scoreable hour in [start, end], retraining at the start of
    each month on every earlier hour -- never on the month being forecast."""
    import numpy as np

    forecasts = []
    for first, last in months(start, end):
        train = [r for r in rows if r.day < first and r.actual is not None and r.curve is not None]
        test = [r for r in rows if first <= r.day <= last and scoreable(r)]
        if not train or not test:
            continue
        model = model_factory()
        model.fit(np.array([r.features for r in train]), np.array([r.actual for r in train]))
        predicted = model.predict(np.array([r.features for r in test]))
        forecasts += [Forecast(r.day, r.hour, r.kind, r.actual, r.curve, float(p))
                      for r, p in zip(test, predicted)]
    return forecasts


def per_day(forecasts: list[Forecast]) -> list[DayScore]:
    """One score per day, as evaluate.backtest does: weighting by hours would
    let a fully observed day outweigh a patchy one."""
    grouped = defaultdict(list)
    for f in forecasts:
        grouped[f.day].append(f)
    return [
        DayScore(
            day=day, kind=items[0].kind or "unlabelled",
            curve_error=mean(abs(f.curve - f.actual) for f in items),
            forest_error=mean(abs(f.forest - f.actual) for f in items),
            curve_bias=mean(f.curve - f.actual for f in items),
            forest_bias=mean(f.forest - f.actual for f in items),
        )
        for day, items in sorted(grouped.items())
    ]


def compare(days: list[DayScore]) -> list[Comparison]:
    groups = defaultdict(list)
    for d in days:
        groups[d.kind].append(d)
        groups["all"].append(d)
    return [
        Comparison(
            kind=kind, days=len(items),
            curve_error=mean(d.curve_error for d in items),
            forest_error=mean(d.forest_error for d in items),
            curve_bias=mean(d.curve_bias for d in items),
            forest_bias=mean(d.forest_bias for d in items),
            forest_better=sum(d.forest_error < d.curve_error for d in items) / len(items),
        )
        for kind, items in sorted(groups.items(), key=lambda group: (group[0] == "all", -len(group[1])))
    ]


def weekly_bootstrap(days: list[DayScore], resamples: int = 2000, seed: int = 0) -> tuple[float, float]:
    """A 95% interval for the change in mean daily error, forest minus curve.

    Negative means the forest is better. Whole weeks are resampled rather than
    single days: neighbouring days share a regime, so treating them as
    independent would claim more evidence than there is.
    """
    import numpy as np

    weeks = defaultdict(list)
    for d in days:
        weeks[tuple(d.day.isocalendar())[:2]].append(d.forest_error - d.curve_error)
    blocks = list(weeks.values())
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(resamples):
        sample = [x for i in rng.integers(0, len(blocks), size=len(blocks)) for x in blocks[i]]
        means.append(sum(sample) / len(sample))
    low, high = np.percentile(means, [2.5, 97.5])
    return float(low), float(high)


def forecast_day(conn, day: date, model_factory=default_model) -> dict[int, float] | None:
    """The forest's forecast for every hour of `day`, trained on every hour before it.

    None when the dashboard would show no curve for the day -- fewer than
    MIN_INSTANCES comparable days -- because the forest is judged as an
    improvement on the curve and has nothing to improve on there.
    """
    import numpy as np

    rows = build_rows(conn, FIRST_DAY, day)
    target = [r for r in rows if r.day == day]
    train = [r for r in rows if r.day < day and r.actual is not None and r.curve is not None]
    if not target or target[0].weeks < MIN_INSTANCES or not train:
        return None
    model = model_factory()
    model.fit(np.array([r.features for r in train]), np.array([r.actual for r in train]))
    predicted = model.predict(np.array([r.features for r in target]))
    return {r.hour: float(p) for r, p in zip(target, predicted)}

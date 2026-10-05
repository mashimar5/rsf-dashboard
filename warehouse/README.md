# Warehouse models

A dbt project over the same Postgres the app uses, for analytics that do not
belong in a request. The dashboard does not read any of it: `store.py` still
owns every query the page makes.

## The rule that shapes it

dbt reads the app's tables as **sources** and never redefines them.

- `occupancy` is taken as a source, not rebuilt, so the rule about where
  imported history stops exists once, in `store.SCHEMA`.
- The academic calendar arrives as a **seed exported from `calendar_days`**,
  not parsed from `data/academic_calendar.csv`. The CSV holds date ranges, and
  the precedence that makes a holiday outrank a break lives in
  `store.calendar_days_from`. Re-implementing that in SQL would be a second
  definition free to drift from the first.

The project already carries one duplicated computation on purpose — the curve,
in `store.weekday_bands` and again in `forest.curve_bands`, with a test holding
them equal. A third copy would have been one too many.

## Models

| Model | Grain | Notes |
| --- | --- | --- |
| `stg_occupancy` | one row per reading | The only model that converts time zones. Adds local day, ISO weekday, half-hour bucket. |
| `daily_bucket_means` | day × bucket | The "every day gets one vote" aggregation: live sampling is every 4 minutes, history every 10. |
| `hourly_occupancy` | day × hour | The grain the random forest trains on, with `samples` so a consumer can apply the four-reading minimum. |
| `weekday_period_profile` | kind × weekday × bucket | Median, quartiles and range over the trailing year, anchored to the newest reading rather than today. |

## Tests

`dbt build` runs 36 nodes — a seed, four models and 31 tests. Beyond not-null
and uniqueness, two singular tests state claims the project actually depends on:

- `assert_one_row_per_day_and_bucket` is the grain behind one vote per day. If
  it ever returns rows, a busy day could be counted twice in a median.
- `assert_pct_within_plausible_bounds` allows above 100%, because the sensor
  counts entries minus exits and 2026-09-03 genuinely peaked at 157 of 150.

Source freshness on `readings` carries the app's own thresholds: warn at 15
minutes, error at 60. On a local copy that stopped updating it reports stale,
which is the correct answer.

## Running it

```bash
cd warehouse
.venv/bin/dbt build --profiles-dir .          # seed, models, tests
.venv/bin/dbt docs generate --profiles-dir .  # lineage graph
.venv/bin/dbt source freshness --profiles-dir .
```

Nothing sensitive is committed: `profiles.yml` reads the environment, and the
whole directory is excluded from the app's Docker image.

Regenerate the calendar seed after editing `data/academic_calendar.csv`, since
the seed is a snapshot of the resolved table:

```bash
.venv/bin/python -c "import csv, store; rows = store.connection().__enter__().execute('SELECT day, kind, label FROM calendar_days ORDER BY day').fetchall(); w = csv.writer(open('warehouse/seeds/calendar_days.csv', 'w', newline='')); w.writerow(['day','kind','label']); [w.writerow([r['day'].isoformat(), r['kind'], r['label']]) for r in rows]"
```

## Checked against the app

`daily_bucket_means` reproduces the aggregation inside `store.weekday_bands`
row for row: 84,008 rows on both sides, no disagreement beyond 1.1e-16. That
check is what makes the local-time macro trustworthy rather than plausible.

## Snowflake

The profile has a `snowflake` target reading `SNOWFLAKE_*` from the
environment, and `macros/local_time.sql` branches on `target.type` for the
things the two engines spell differently: `AT TIME ZONE` against
`CONVERT_TIMEZONE`, `EXTRACT(ISODOW …)` against `DAYOFWEEKISO`, and integer
division. Those branches are written from the documentation and remain
**unproven until they run against a real account** — what `CONVERT_TIMEZONE`
returns for a `TIMESTAMP_TZ` input is the first thing to check there.

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

The same models run against Snowflake with `--target snowflake`, on a copy of
the data loaded by `load_snowflake.py`: exported to gzipped CSV, pushed to each
table's internal stage with `PUT`, and pulled in with `COPY INTO`.

```bash
set -a; . warehouse/.env; set +a
.venv/bin/python load_snowflake.py
.venv/bin/dbt build --profiles-dir . --target snowflake
```

Both targets build the same 36 nodes, pass the same 31 tests, and produce the
same numbers:

| | Postgres | Snowflake |
| --- | --- | --- |
| `daily_bucket_means` | 84,008 rows | 84,008 rows |
| `weekday_period_profile` | 1,824 rows | 1,824 rows |
| Worst value difference | — | 2.2e-16 |
| 2026-03-08, buckets (spring forward) | 46 | 46 |
| 2025-11-02, buckets (fall back) | 48 | 48 |
| `dbt build` | 0.8 s | 8.2 s |

### What actually differed

- **Local time.** `AT TIME ZONE` against `CONVERT_TIMEZONE`, and
  `EXTRACT(ISODOW …)` against `DAYOFWEEKISO`. They agree to floating-point
  noise, including on the days a clock change leaves 46 or 48 buckets.
- **Integer division.** Postgres truncates `int / int`; Snowflake returns a
  decimal, so the bucket index needs an explicit `floor`.
- **Identifiers.** Snowflake folds unquoted names to upper case, so the models
  land as `DAILY_BUCKET_MEANS` and read back with upper-case columns.
- **Compute is something you size.** The loader sets `COMPUTE_WH` to XSMALL
  with a 60-second auto-suspend. Storage and compute bill separately, which has
  no Postgres equivalent.
- **Speed, honestly.** Postgres is ten times faster here. At 250k rows the
  warehouse is paying for network latency and a resume with nothing to show for
  it; it would win at a scale this project does not have.

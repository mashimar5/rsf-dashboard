# RSF Dashboard

Live and historical occupancy for the UC Berkeley Recreational Sports Facility
weight rooms.

**Live: [rsf-dashboard.fly.dev](https://rsf-dashboard.fly.dev)**

Shows how full the weight rooms are right now, today's occupancy curve, and any
previous day's. A collector records a reading every five minutes, so the history
builds on its own.

## How it works

```
Density API ──> collector ──> readings.db ──┐
                (every 5m)     (SQLite)     ├──> Flask ──> dashboard
RecWell hours page ──> scraper ──> cache ───┘
```

Three independent pieces:

- **Occupancy** comes from the Density sensor API behind the RSF's public crowd
  meter. The share token cannot be used directly: it is exchanged for a
  15-minute access token, which is then used to read the display endpoint. The
  response carries no measurement time, so readings are stamped at fetch time.
- **History** is appended to SQLite. The collector runs in-process in
  deployment, and can be run standalone or from cron locally.
- **Opening hours** are scraped from the RecWell hours page, which carries up to
  three kinds of table. An explicit date beats a seasonal date range, which beats
  the undated standing schedule.

## Running locally

Requires Python 3.13+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Put your Density share token in `.env` (gitignored):

```
DENSITY_SHARE_TOKEN=shr_...
```

Then start the web app:

```bash
.venv/bin/python app.py
```

It serves on <http://localhost:5001>. Port 5000 is avoided because macOS
ControlCenter occupies it; override with `PORT` if you like.

Collect readings — one-shot, so cron can drive it:

```bash
.venv/bin/python collect.py
```

Or run it standalone with its own timer:

```bash
.venv/bin/python collect.py --interval 300
```

## Tests

```bash
.venv/bin/python -m unittest discover -p 'test_*.py'
```

No network access required: HTTP is mocked and the hours parser runs against a
saved fixture in `tests/`.

## Endpoints

| Path | Purpose |
| --- | --- |
| `/` | Dashboard. `?date=YYYY-MM-DD` selects a day, clamped to the recorded range. |
| `/api/current` | Live count, capacity, percentage, and whether it came from the API or the last stored reading. |
| `/api/history?hours=N` | Raw readings for the last N hours (default 24). |
| `/api/hours` | What the hours scraper parsed, which table each day resolved to, and the cache age. Use this when the hours line disappears. |

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DENSITY_SHARE_TOKEN` | — | Required. Read from `.env` locally, injected as a Fly secret in deployment. |
| `RSF_DB_PATH` | `./readings.db` | SQLite location. Points at the mounted volume in deployment. |
| `RSF_HOURS_CACHE` | `./hours_cache.json` | Cached hours tables. |
| `COLLECT_INTERVAL` | unset | Seconds between in-process collections. Unset means the web app does not collect, which is the local default. |
| `PORT` | `5001` | Web server port. |

## Deployment

Runs on Fly.io as a single machine with a 1 GB volume mounted at `/data`, so
readings and the hours cache survive redeploys.

```bash
fly deploy
```

Two things that must stay as they are:

- **One gunicorn worker.** The collector runs in-process, so a second worker
  would mean a second collector writing duplicate samples.
- **`auto_stop_machines = false`.** Fly's default is to sleep idle machines,
  which would silently stop collection whenever nobody is looking at the page.

## Layout

| File | Contents |
| --- | --- |
| `density.py` | The Density API client and the `Reading` dataclass. |
| `store.py` | SQLite schema and queries. |
| `collect.py` | The recorder. One-shot by default. |
| `hours.py` | Hours scraping, table selection, and caching. |
| `app.py` | Flask routes, chart geometry, and day statistics. |
| `templates/index.html` | The whole UI, including the chart and hover interaction. |
| `tools/make_icons.py` | Regenerates the home-screen icon. Needs Pillow, which is deliberately not a runtime dependency. |

## Notes on a few decisions

**Timestamps are stored in UTC and compared in UTC.** They are compared as text,
so any local-time bound must be converted first — a local midnight compared
against `+00:00` values silently pulls in the previous evening.

**The page never saves readings.** Collection lives only in the collector, so
samples land at regular intervals and refreshing the page cannot skew history.

**Day statistics are scoped to opening hours.** The gym reads zero all night;
including those readings drags a day's average toward nothing.

**The typical-weekday curve is a median, not a mean**, and excludes the day being
viewed from its own comparison. It stays hidden until at least three prior
instances of that weekday exist, below which it is noise rather than signal.

**Hours failures hide the line rather than guessing.** Wrong hours are worse than
no hours when the point is deciding whether to walk over. `/api/hours` exists
because that failure is otherwise invisible.

## Not built yet

[DESIGN-gym-time-suggestions.md](DESIGN-gym-time-suggestions.md) sketches
suggesting a gym time from predicted crowding and calendar availability. It is
blocked on having a few weeks of per-weekday data, and on the dashboard being
public — calendar data must not land on an unauthenticated page.

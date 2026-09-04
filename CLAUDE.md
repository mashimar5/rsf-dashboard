# RSF Dashboard — working notes

Occupancy tracker for the UC Berkeley RSF weight rooms. See [README.md](README.md)
for setup, endpoints and configuration; this file is the context that is not
obvious from the code.

- **Live:** https://rsf-dashboard.fly.dev (Fly app `rsf-dashboard`, region `sjc`)
- **Repo:** https://github.com/mashimar5/rsf-dashboard (public)
- **Data:** collecting every 5 minutes since 2026-08-29, on a 1 GB Fly volume at `/data`

## Shape of the thing

Flask + SQLite backend, React 19 + TypeScript frontend built with Vite.
`frontend/` builds to `static/app/`, which Flask serves at `/`. The client makes
one request to `/api/day` for the whole view model.

Backend: `density.py` (API client) → `store.py` (SQLite) → `collect.py` (recorder),
with `hours.py` scraping opening hours and `app.py` tying it together.

## Commands

```bash
.venv/bin/python app.py                              # serve on :5001
.venv/bin/python -m unittest discover -p 'test_*.py' # 33 tests, no network needed
cd frontend && npm run dev                           # Vite dev server, proxies /api to :5001
cd frontend && npm run build                         # emits ../static/app
fly deploy                                           # Docker multi-stage: node build, then python
```

The frontend must be built for `/` to serve anything; it returns a 503 with
instructions if `static/app/index.html` is missing.

## Invariants — do not change these casually

- **One gunicorn worker.** The collector runs in-process; a second worker means a
  second collector writing duplicate samples.
- **`auto_stop_machines = false`** in `fly.toml`. Fly's default sleeps idle
  machines, which silently stops collection when nobody is looking at the page.
- **Timestamps are stored and compared in UTC.** They are compared as *text*, so
  any local-time bound must be converted first. This bug shipped once: a local
  midnight compared against `+00:00` values pulled in the previous evening and
  overstated a day's sample count by 62.
- **The web page never saves readings.** Collection lives only in the collector,
  so samples land at regular intervals and refreshes cannot skew history.
- **Day statistics are scoped to opening hours.** The gym reads zero all night;
  including those readings dragged a busy Sunday's average from 61% to 32%.

## Gotchas that cost time

- **The RecWell hours page writes dashes as `&#8211;`.** Parsing without
  `html.unescape` makes every seasonal date range silently fail to match and fall
  through to the standing schedule. A spot-check missed this because two tables
  happened to share the same Saturday hours; the test caught it only because it
  asserts *which table* answered, not just the value.
- **The local `readings.db` is stale.** Local collectors were stopped once Fly
  took over, so the local database holds only 2026-08-29/30. Running `app.py`
  locally shows an empty chart for today. That is the data gap, not a bug.
- **Port 5000 is occupied by macOS ControlCenter.** The app defaults to 5001.
- **Xcode is not installed on this Mac**, so the iOS Simulator tools do not work.
  Anything iPhone-related has to be verified by the user on their device.
- **Counts can exceed capacity.** The sensor counts entries minus exits, so
  readings above 150 are real; 2026-09-03 peaked at 157 of 150 (105%). Nothing
  clamps this.

## Testing conventions

`unittest`, no pytest. HTTP is mocked; the hours parser runs against
`tests/rsf_hours_fixture.html`. Tests must not depend on wall-clock state — one
did, and it passed all afternoon then failed at midnight when today's table was
empty. Patch `store.between` / `app.todays_readings` rather than hitting the real
database.

Verify a change actually fails without the fix before trusting a green suite.

## State and what's next

- The **typical-weekday curve** is built, tested and deployed but **not yet
  visible**: it needs three prior instances of a weekday, so each weekday
  activates around 2026-09-21. Nothing to do but wait.
- **`/api/hours`** exists for when the hours scraper breaks. Failures hide the
  hours line by design, so that endpoint is the only way to tell a genuine "no
  hours posted" from a parser that broke on edited markup. Watch it when the
  semester schedule changes.
- **Quietest Hour still tends to land right before closing** (e.g. 9:58–10:58 PM),
  which is true but a poor recommendation. The user was offered a cutoff
  excluding the final 60–90 minutes and has not decided.
- **[DESIGN-gym-time-suggestions.md](DESIGN-gym-time-suggestions.md)** sketches
  suggesting gym times from predicted crowding plus calendar free/busy. Blocked
  on data, and on the fact that **the dashboard has no authentication** — calendar
  data must not land on a public page. Read-only free/busy scope only; scheduling
  is via generated `.ics`, never calendar write access.

## Working style that fit this project

The user is a student building this partly for a resume, learning as they go —
explain the *why* behind design choices, not just the change. They review
screenshots of the deployed result, so verify in the browser and share proof
rather than asserting it works. Deploy after each accepted change; the working
tree stays clean and every commit is pushed.

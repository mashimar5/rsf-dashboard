# RSF Dashboard

[![CI](https://github.com/mashimar5/rsf-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/mashimar5/rsf-dashboard/actions/workflows/ci.yml)

Live and historical occupancy for the UC Berkeley Recreational Sports Facility
weight rooms, and an agent that suggests when to go.

**Live: [rsf-dashboard.fly.dev](https://rsf-dashboard.fly.dev)**

Python · Flask · Postgres · SQL · React 19 · TypeScript · Vite · Claude API · Google OAuth · Docker · Fly.io · GitHub Actions

Shows how full the weight rooms are right now, the day's occupancy curve, and
any previous day's. A collector records a reading every four minutes, and five
years of the same sensor's history from before that, cleaned on import, back
the forecast. From it the dashboard proposes workout windows around your
calendar and — on one click — writes the chosen one to a calendar it created. A
chat panel answers open-ended questions about the data and sets the preferences
that shape those suggestions.

## How it works

```
Density API ──> collector ──> Postgres ─────┐
                (every 4m)      (Neon)      │
Sensor history (CSV) ──> one-time import ───┤
                                            ├──> Flask ──> React dashboard
RecWell hours page ──> scraper ──> cache ───┤
Registrar calendars ──> calendar CSV ───────┤
                                            │
Google Calendar (free/busy) ────────────────┘
```

The frontend is a React + TypeScript app built with Vite (`frontend/`), served
by Flask from `static/app`. It fetches one endpoint, `/api/day`, which returns
the whole view model for a day; `frontend/src/types.ts` mirrors that contract,
so a backend change that breaks it fails the typecheck.

Backend pieces, each independent:

- **Occupancy** comes from the Density sensor API behind the RSF's public crowd
  meter. The share token cannot be used directly: it is exchanged for a
  short-lived access token, which is then used to read the display endpoint.
  **The exchange runs on every poll and the token is discarded** — it is valid
  for 15 minutes, but caching it would buy one saved request per four-minute
  cycle in exchange for expiry handling and a retry-on-401 path, so the
  stateless version wins at this rate. The response carries no measurement
  time, so readings are stamped at fetch time.
- **History** is appended to Postgres. The collector runs in-process in
  deployment, and can be run standalone or from cron locally.
- **Imported history** covers the years before collection began: the sensor's
  own ten-minute counts from September 2021, archived by Anthony Ozerov and
  published under CC0 at <https://aozerov.com/berkeley/weightroom/>. It is
  loaded once by `tools/backfill_history.py` into a separate `history` table
  and cleaned on the way in — see the notes below.
- **The academic calendar** (`data/academic_calendar.csv`, transcribed from the
  Registrar's calendars) labels every day as instruction, review week, finals,
  break, summer or holiday, and is synced into the database on startup.
- **Opening hours** are scraped from the RecWell hours page, which carries up to
  three kinds of table. An explicit date beats a seasonal date range, which beats
  the undated standing schedule.
- **Prediction** is a single SQL query (`store.weekday_bands`): a median per
  half-hour bucket across the last eight instances of that weekday in the same
  kind of academic period, plus the range. `evaluate.py` keeps the parts SQL
  has no answer for — choosing a dispersion measure, scoring, backtesting.
- **Suggestion** (`policy.py`) turns that curve into recommendations, filtered
  by opening hours and — when signed in — by Google Calendar free/busy.

## Running locally

Requires Python 3.13+, Node 22+, and Postgres 17.

```bash
brew services start postgresql@17
createdb rsf_dev && createdb rsf_test

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`DATABASE_URL` defaults to `postgresql:///rsf_dev`; the schema is created on
first connection.

Put your Density share token in `.env` (gitignored):

```
DENSITY_SHARE_TOKEN=shr_...
```

Build the frontend (required — Flask serves the build, and returns a 503 with
instructions if it is missing):

```bash
cd frontend && npm install && npm run build
```

Then start the web app:

```bash
.venv/bin/python app.py
```

For frontend work, Vite's dev server gives hot reload and proxies `/api` to
Flask on 5001:

```bash
cd frontend && npm run dev
```

It serves on <http://localhost:5001>. Port 5000 is avoided because macOS
ControlCenter occupies it; override with `PORT` if you like.

Collect readings — one-shot, so cron can drive it:

```bash
.venv/bin/python collect.py
```

Or run it standalone with its own timer. Deployment uses 240 seconds — see
Configuration for why:

```bash
.venv/bin/python collect.py --interval 240
```

Google sign-in needs the OAuth variables below; without them the dashboard runs
fine and simply offers no calendar features.

## Tests

```bash
.venv/bin/python -m unittest discover -p 'test_*.py'
```

HTTP is mocked and the hours parser runs against a saved fixture in `tests/`,
so no external service is reached. A local Postgres *is* required: the weekday
curve uses `AT TIME ZONE` and `percentile_cont`, which have no in-memory
substitute, and testing against a different engine than production runs would
defeat the point of putting the query there. `testing.py` points the pool at
`rsf_test` and truncates between tests.

GitHub Actions runs the same suite on every push, plus a TypeScript typecheck,
a frontend build, and a Docker build. The Python job installs from
`requirements.txt` on a clean machine, which is what catches a dependency that
works locally only because it was installed once and never declared.

`tools/check_readme.py` also runs in CI and fails the build when this file
documents a route, environment variable, or module the code does not have — or
omits one it does. Only structural claims can be checked that way; prose like
"the token is cached" is a claim about behaviour and still needs a reader.

## Endpoints

| Path | Purpose |
| --- | --- |
| `/` | Dashboard. `?date=YYYY-MM-DD` selects a day, clamped to the recorded range. |
| `/api/day` | Everything one day's view needs: live reading or summary, samples, typical curve, hours, suggestions, booking, navigation bounds. |
| `/api/current` | Live count, capacity, percentage, and whether it came from the API or the last stored reading. |
| `/api/history?hours=N` | Raw readings for the last N hours (default 24). |
| `/api/hours` | What the hours scraper parsed, which table each day resolved to, and the cache age. Use this when the hours line disappears. |
| `/api/book` | `POST` writes a suggested window to the calendar; `DELETE` cancels it. Signed in only. |
| `/api/feedback` | `POST` records whether a booked session happened. |
| `/api/ask` | `POST` a transcript; answers questions about the data and sets scheduling preferences. Signed in only, rate limited. |
| `/api/preferences` | `DELETE` clears scheduling preferences. |
| `/auth/google`, `/auth/callback`, `/auth/logout` | Google sign-in. |
| `/health` | Liveness. Returns 503 only for conditions a restart could fix; Fly's health check watches this. |
| `/health/freshness` | Returns 503 when readings have stopped. For an external uptime monitor, which pages a human rather than restarting. |
| `/privacy` | Privacy policy. |

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DENSITY_SHARE_TOKEN` | — | Required. Read from `.env` locally, injected as a Fly secret in deployment. |
| `DATABASE_URL` | `postgresql:///rsf_dev` | Postgres connection string. Use the pooled endpoint on Neon. |
| `RSF_HOURS_CACHE` | `./hours_cache.json` | Cached hours tables. |
| `COLLECT_INTERVAL` | unset | Seconds between in-process collections. Unset means the web app does not collect, which is the local default. Deployment uses 240 rather than 300 to stay inside Neon's ~5 minute idle suspend. |
| `PORT` | `5001` | Web server port. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | — | OAuth client. Calendar features are simply absent without them. |
| `ALLOWED_EMAILS` | empty | Comma-separated allowlist. Empty admits nobody, so a misconfigured deploy fails closed. |
| `ANTHROPIC_API_KEY` | — | Enables the data chat. Absent, the panel is hidden and everything else works. |
| `SECRET_KEY` | — | Signs the session cookie. |
| `TOKEN_ENCRYPTION_KEY` | — | Fernet key encrypting stored refresh tokens. |

## Deployment

Runs on Fly.io as a single machine, with the database on Neon (free tier, US
West 2, reached over the **pooled** endpoint). The 1 GB Fly volume now holds
only the hours cache.

`COLLECT_INTERVAL` is 240 seconds rather than 300 because Neon's free tier
suspends the compute after roughly five minutes idle; a five-minute interval
sits exactly on that boundary and pays a cold start most cycles.

`tools/migrate_sqlite_to_postgres.py` moves an existing SQLite database across.
It is idempotent on `observed_at` — a reading is identified by its instant — so
it can be re-run and can run while the collector is already writing. That
mattered: the collector wrote its first row to the new database seconds after
deployment, and an importer that refused a non-empty target would have bailed
mid-cutover.

`tools/backfill_history.py` imports the sensor history the same way, idempotent
on the instant, but production never cleans anything. Parsing five years of
rows takes about 145 MB, and the 256 MB machine that runs the app and the
collector has about 70 MB free, with no swap. So the file is cleaned and
exported locally, copied onto the machine, and streamed into COPY one row at a
time, which stays flat at about 45 MB. The load runs from inside the machine,
so `DATABASE_URL` never leaves Fly:

```bash
python tools/backfill_history.py --export history-clean.csv
fly ssh sftp put history-clean.csv /tmp/history-clean.csv
fly ssh console -C "python tools/backfill_history.py --cleaned /tmp/history-clean.csv"
```

A bad row anywhere in the file aborts the whole load, and `TRUNCATE history`
undoes it without touching a single live reading.

```bash
fly deploy
```

Three things that must stay as they are:

- **One gunicorn worker.** The collector runs in-process, so a second worker
  would mean a second collector writing duplicate samples.
- **`auto_stop_machines = false`.** Fly's default is to sleep idle machines,
  which would silently stop collection whenever nobody is looking at the page.
- **`ProxyFix`.** Fly terminates TLS at its edge and forwards plain HTTP, so
  without it `url_for(_external=True)` builds `http://` URLs and Google rejects
  the OAuth redirect as a mismatch.

## Monitoring

Ingestion cannot fail silently, which matters more here than in most pipelines
because there is no replay: the endpoint this app polls returns a point-in-time
reading, so a missed poll cannot be requested again. The years of history
before collection began exist only because someone else archived them; that was
a one-time import, not a way to fill tomorrow's gaps.

| Signal | Endpoint | Watched by | On failure |
| --- | --- | --- | --- |
| Liveness | `/health` | Fly health check, every 60s | Restart the machine |
| Freshness | `/health/freshness` | UptimeRobot, every 5 min | Email a human |

The split is deliberate. `/health` returns 503 only for conditions a restart
could plausibly fix — the database unreachable, or the collector thread dead
while gunicorn carried on serving, which is otherwise invisible. Stale readings
do **not** fail it: if the sensor API is down, restarting every sixty seconds
turns one outage into two. Staleness gets its own endpoint that nothing
restarts on, because the right response is to tell someone.

Threshold is 15 minutes — three missed cycles at the deployed interval, which
absorbs a transient API failure without crying wolf. The dashboard also says
plainly when readings have stopped, so the headline number is never mistaken
for current occupancy.

## Layout

| File | Contents |
| --- | --- |
| `density.py` | The Density API client and the `Reading` dataclass. |
| `store.py` | Postgres schema, queries, the connection pool, the weekday-curve SQL, and the calendar sync. |
| `collect.py` | The recorder. One-shot by default. |
| `hours.py` | Hours scraping, table selection, and caching. |
| `evaluate.py` | The typical-weekday curve, dispersion, scoring and backtesting. |
| `policy.py` | Turning a forecast into suggested windows. |
| `google_auth.py` | OAuth, free/busy, and calendar writes. |
| `intent.py` | The preferences schema, and the bounds a schema cannot enforce. |
| `ask.py` | The data chat: tools over vetted queries, and the conversation loop. |
| `app.py` | Flask routes and the day view model. |
| `frontend/src/App.tsx` | Top-level view: fetches `/api/day`, owns the selected date. |
| `frontend/src/components/` | `Chart`, `StatTiles`, `DayNav`, `Suggestions`, `Feedback`. |
| `frontend/src/types.ts` | The `/api/day` contract as TypeScript interfaces. |
| `tools/make_icons.py` | Regenerates the home-screen icon. Needs Pillow, which is deliberately not a runtime dependency. |
| `tools/backfill_history.py` | The one-time import of sensor history, and the rules that decide what to keep. |
| `tools/backtest_curve.py` | Scores the curve against history it never saw, with and without period matching. |
| `data/academic_calendar.csv` | Berkeley's academic periods and holidays, from the Registrar's calendars. |

The Docker build is multi-stage: Node builds the frontend, then the Python image
copies the built assets in.

## Notes on a few decisions

**Never compare timestamps as text in the frontend.** The database no longer
allows this mistake — see `timestamptz` below — but JavaScript still does. The
same instant arrives as `…T16:30:00+00:00` from one source and
`…T09:30:00-07:00` from another, and `===` between them is false, which once
meant a confirmed booking never displayed as confirmed. `sameInstant()` in
`lib/format.ts` parses before comparing; use it.

**Postgres, not SQLite — for two specific reasons, not for scale.** At a few
thousand rows SQLite was the right tool and stayed right for weeks.

The first reason is that the typical-weekday curve could not be expressed in
it. Bucketing a UTC instant by *local* weekday and time of day needs a
DST-aware timezone database, which SQLite does not have — its `localtime`
modifier uses the server's zone, UTC in production. It also has no median. So
the central computation had to load every candidate row into Python. In
Postgres it is one query: `AT TIME ZONE` for correct local time,
`percentile_cont` for a real median, `LIMIT` for the rolling window.

The second is that `timestamptz` retires a bug class rather than defending
against it. SQLite stored times as text, and comparing ISO strings in different
offsets caused four separate bugs here — a query bound, an ad-hoc query, a
frontend `===`, and an unnormalised write. A column that holds an instant
cannot be compared wrongly. Only the frontend half of the hazard survives,
which is the note above.

**Connections come from a pool** and are returned on request teardown. A
Postgres connection is a socket and a server-side process, unlike a SQLite
handle that is simply garbage collected, so a leaked one is a real leak.

**The page never saves readings.** Collection lives only in the collector, so
samples land at regular intervals and refreshing the page cannot skew history.

**Day statistics are scoped to opening hours.** The gym reads zero all night;
including those readings drags a day's average toward nothing.

**The typical-weekday curve is a median, not a mean**, and excludes the day being
viewed from its own comparison. It stays hidden until at least three prior
instances of that weekday exist, below which it is noise rather than signal.

**Uncertainty is shown, not hidden.** Each bucket carries the range across
instances, drawn as a band behind the median, because a count of instances is a
crude confidence signal — three weekdays that agree closely and three that range
20%–70% pass the same gate. Dispersion switches from range to interquartile
range at eight instances: the expected value of a range grows with sample count
even when variability is unchanged, so stored spreads would otherwise drift
upward and suggest the gym had become less predictable.

**The curve uses a rolling window** of the last eight instances. Without one,
every past Monday is weighted equally forever, so by November a typical Monday
would blend quiet late-August ones with busy October ones — the curve would
degrade as data accumulated.

**Those instances come from the same kind of academic period.** A window alone
breaks at every boundary: in mid-September the eight most recent Mondays are
mostly summer ones, when the gym runs about 30% quieter and closes early, so
the curve for a Monday in term read 67% at 5 PM and 0% at 9 PM instead of 85%
and 89%. Each day is labelled from the Registrar's calendar, and the curve
compares term with term, finals with finals, summer with summer. Backtested
against 1,304 days from January 2023 to August 2026 that it had not seen,
matching halves the mean error, from 12.8 to 6.7 points of capacity: 12.8 to
7.6 in term, 8.5 to 4.5 in summer, and 24.0 to 5.3 in breaks, where plain
recency forecast a term-sized crowd. Review week is the one period where it
changes nothing (7.5 to 7.4). `tools/backtest_curve.py` reproduces the table.

**Each instance gets one vote.** Live collection samples every four minutes and
imported history every ten, so pooling readings would let a live day outvote a
historical one about 2.5 to 1. Each day is averaged per half hour first and the
median taken across days, which also makes the band a spread across instances —
the thing the range-to-IQR switch was always counting.

**Imported history lives in its own table**, read through an `occupancy` view
that only analytics use. The collector, the day view, date navigation and both
health checks read `readings` exactly as before, so nothing that describes live
collection can be fooled by a five-year-old row, and the import can be undone
with one `TRUNCATE`. The view admits history only before the first live
reading, and at today's capacity of 150 — the room did not change size, and the
140 cap posted in earlier years would make percentages incomparable across them.

**Imported history is cleaned, never corrected.** It is the same sensor — over
the days both cover, its counts match live readings to a median of two people —
but five years of it include faults, and a stretch that cannot be trusted is
left out rather than repaired:

- Nothing before 2021-09-06. The gym ran under pandemic restrictions until
  then; the weekly median daily peak went from 75 people to 153 in one week.
- A count of 10 or more that stays exactly the same for an hour is a stalled
  feed, not a crowd: 2,159 rows in 152 stretches, almost all a small leftover
  held from late evening until the nightly reset.
- A day with any count above 180 (120% of capacity), or with more than 20
  people still counted at 00:30 after closing, drifted, and that error builds
  over the day, so the whole day goes: 63 days, 44 and 19 respectively. The
  ceiling sits well clear of real crowds — 2026-09-03 genuinely peaked at 157.

That leaves 250,532 readings. Only the count column is used: the file's min and
max columns contradict it in a fifth to a third of rows, and live readings from
the same sensor side with the count.

**Model and policy are separate**, because they fail for unrelated reasons. A
correct forecast can still produce a useless suggestion: "the quietest hour is
9:55–10:55 PM" was accurate and unusable, since a session starting then cannot
finish before closing. Windows must therefore end before closing with a
wind-down margin — arithmetic, not taste. There is deliberately no matching rule
at opening, because an empty gym at 7 AM is a real recommendation.

**One suggestion per part of the day.** Ranking by quietness alone returns
consecutive windows, since the quiet part of a day is one contiguous stretch;
three names for one recommendation are useless as alternatives.

**Forecast accuracy and suggestion usefulness are recorded separately.** Whether
the forecast was right is derivable from stored readings and needs no input.
Whether the advice was useful cannot be derived at all — the sensor counts
bodies at a doorway, not identities — so it can only come from the user. Keeping
them in one table would lose the distinction between "answered no" and "never
answered", which are very different signals.

**Calendar scopes are the narrowest that work.** `calendar.freebusy` returns
busy start and end times only — no titles, attendees or locations — and
`calendar.app.created` can write only to a calendar the app itself created,
never to an existing one. Both are non-sensitive, which is also why the app
needs no Google verification; `calendar.readonly` or `calendar.events` would
flip it into sensitive-scope territory.

**Sign-in gates only calendar-derived output.** Occupancy, chart and hours stay
public. Filtering suggestions by a calendar leaks that calendar: the difference
between the publicly computable quietest windows and the ones shown is exactly
the user's schedule, so gating was not optional.

**The model sits at the edge, not in the loop.** `ask.py` is the only place
that calls an LLM. Preferences it sets are stored as plain numbers, and every
downstream decision — which windows are eligible, how they rank, what gets
booked — reads those numbers. A suggestion is therefore never one model call
away from being different, the policy stays unit-testable without a network,
and the feature degrades to "hidden" rather than "broken" when no API key is
set.

**The model gets tools, not a database.** Eight vetted queries with typed
arguments: the data overview, narrower slices, the weekday curve, past
sessions, and three that read or write the single settings row. A confused
model can ask a sensible question of the wrong slice; it cannot write SQL,
reach another table, or touch anything but that one row. Bad arguments come
back as `{"error": …}` rather than exceptions.

**Handing over the whole dataset beats querying it.** The first version of the
chat had four narrow lookups and could answer "what was X" but never find a
pattern, because it never saw more than one slice at a time. A weekday-by-hour
grid is 7 × 24 = 168 cells however long collection runs — about 550 tokens — so
the entire shape fits in context and the model reasons across it. That was an
architectural fix, not a prompting one.

**One text input, not two.** Preferences used to have their own box, which
looked like a chat, didn't reply, and silently changed the suggestions above
it. They are now set through the chat, because a preference is local, visible
and reversible in a sentence. Booking is not, and keeps its explicit click —
the chat has no booking tool at all.

**Each answer shows which tools it read**, so a claim can be checked against
what it actually looked at rather than trusted. The chat is rate limited per
hour: every turn costs money and runs several queries, and an unbounded loop of
paid calls is the failure mode worth preventing.

Structured output guarantees the *shape* of what comes back, not its sense —
`session_minutes: 600` is schema-valid nonsense. Ranges are enforced after
parsing: implausible lengths clamp, impossible hours are dropped rather than
clamped (clamping 99 to 23 would invent a preference nobody stated), and a
crowding ceiling written as `60` is read as 60% rather than rejected. The model
also returns a one-sentence summary of what it understood, plus the list of
clauses it found, both shown back so a misreading is visible rather than
silent.

**Field order in the schema is load-bearing.** Structured output is generated
left to right, so `clauses` is declared first on purpose: it makes the model
enumerate everything stated before committing to any value. Without it,
extraction was order-dependent — *"four times a week, and never more than half
full"* returned the frequency and silently dropped the ceiling, while the same
two clauses reversed returned both.

**The agent proposes and waits.** Nothing reaches the calendar without an
explicit click, and `/api/book` only accepts a window the policy is currently
suggesting, so a crafted request cannot write arbitrary events and a stale tab
cannot book a window that no longer makes sense.

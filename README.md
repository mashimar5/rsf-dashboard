# RSF Dashboard

[![CI](https://github.com/mashimar5/rsf-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/mashimar5/rsf-dashboard/actions/workflows/ci.yml)

Live and historical occupancy for the UC Berkeley Recreational Sports Facility
weight rooms, and an agent that suggests when to go.

**Live: [rsf-dashboard.fly.dev](https://rsf-dashboard.fly.dev)**

Python · Flask · SQLite · React 19 · TypeScript · Vite · Google OAuth · Docker · Fly.io

Shows how full the weight rooms are right now, the day's occupancy curve, and
any previous day's. A collector records a reading every five minutes, so the
history builds on its own. Once a weekday has enough history, it also proposes
workout windows around your calendar and — on one click — writes the chosen one
to a calendar it created.

## How it works

```
Density API ──> collector ──> readings.db ──┐
                (every 5m)     (SQLite)     │
                                            ├──> Flask ──> React dashboard
RecWell hours page ──> scraper ──> cache ───┤
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
  15-minute access token, which is then used to read the display endpoint. The
  response carries no measurement time, so readings are stamped at fetch time.
- **History** is appended to SQLite. The collector runs in-process in
  deployment, and can be run standalone or from cron locally.
- **Opening hours** are scraped from the RecWell hours page, which carries up to
  three kinds of table. An explicit date beats a seasonal date range, which beats
  the undated standing schedule.
- **Prediction** (`evaluate.py`) builds a per-weekday curve from the last few
  instances of that weekday: a median per half-hour bucket, plus the range
  across instances.
- **Suggestion** (`policy.py`) turns that curve into recommendations, filtered
  by opening hours and — when signed in — by Google Calendar free/busy.

## Running locally

Requires Python 3.13+ and Node 22+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

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

Or run it standalone with its own timer:

```bash
.venv/bin/python collect.py --interval 300
```

Google sign-in needs the OAuth variables below; without them the dashboard runs
fine and simply offers no calendar features.

## Tests

```bash
.venv/bin/python -m unittest discover -p 'test_*.py'
```

No network access required: HTTP is mocked and the hours parser runs against a
saved fixture in `tests/`.

GitHub Actions runs the same suite on every push, plus a TypeScript typecheck,
a frontend build, and a Docker build. The Python job installs from
`requirements.txt` on a clean machine, which is what catches a dependency that
works locally only because it was installed once and never declared.

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
| `/auth/google`, `/auth/callback`, `/auth/logout` | Google sign-in. |
| `/privacy` | Privacy policy. |

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DENSITY_SHARE_TOKEN` | — | Required. Read from `.env` locally, injected as a Fly secret in deployment. |
| `RSF_DB_PATH` | `./readings.db` | SQLite location. Points at the mounted volume in deployment. |
| `RSF_HOURS_CACHE` | `./hours_cache.json` | Cached hours tables. |
| `COLLECT_INTERVAL` | unset | Seconds between in-process collections. Unset means the web app does not collect, which is the local default. |
| `PORT` | `5001` | Web server port. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | — | OAuth client. Calendar features are simply absent without them. |
| `ALLOWED_EMAILS` | empty | Comma-separated allowlist. Empty admits nobody, so a misconfigured deploy fails closed. |
| `SECRET_KEY` | — | Signs the session cookie. |
| `TOKEN_ENCRYPTION_KEY` | — | Fernet key encrypting stored refresh tokens. |

## Deployment

Runs on Fly.io as a single machine with a 1 GB volume mounted at `/data`, so
readings, the hours cache and stored tokens survive redeploys.

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

## Layout

| File | Contents |
| --- | --- |
| `density.py` | The Density API client and the `Reading` dataclass. |
| `store.py` | SQLite schema, queries, and small migrations. |
| `collect.py` | The recorder. One-shot by default. |
| `hours.py` | Hours scraping, table selection, and caching. |
| `evaluate.py` | The typical-weekday curve, dispersion, scoring and backtesting. |
| `policy.py` | Turning a forecast into suggested windows. |
| `google_auth.py` | OAuth, free/busy, and calendar writes. |
| `app.py` | Flask routes and the day view model. |
| `frontend/src/App.tsx` | Top-level view: fetches `/api/day`, owns the selected date. |
| `frontend/src/components/` | `Chart`, `StatTiles`, `DayNav`, `Suggestions`, `Feedback`. |
| `frontend/src/types.ts` | The `/api/day` contract as TypeScript interfaces. |
| `tools/make_icons.py` | Regenerates the home-screen icon. Needs Pillow, which is deliberately not a runtime dependency. |

The Docker build is multi-stage: Node builds the frontend, then the Python image
copies the built assets in.

## Notes on a few decisions

**Timestamps are stored in UTC and compared in UTC.** They are compared as text,
so any local-time bound must be converted first — a local midnight compared
against `+00:00` values silently pulls in the previous evening. The same class of
bug appeared three times: in a query bound, in an ad-hoc SQL comparison, and in
a frontend `===` between two spellings of the same instant.

**SQLite runs in WAL mode** because `collect.py` can run as a separate process
against the file the web app is serving from. It matters in deployment too:
SQLite locks per connection rather than per process, so the in-process collector
and each request contend even under a single worker. At one write every five
minutes the contention is rare — this is cheap insurance, not a fix for a
measured problem. WAL still allows only one writer at a time; what it removes is
readers blocking the writer.

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

**The agent proposes and waits.** Nothing reaches the calendar without an
explicit click, and `/api/book` only accepts a window the policy is currently
suggesting, so a crafted request cannot write arbitrary events and a stale tab
cannot book a window that no longer makes sense.

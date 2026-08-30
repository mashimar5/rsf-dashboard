# Suggesting a gym time

Status: **design only, not built.** Blocked on data (see Prerequisites).

## Goal

The dashboard suggests when to go to the RSF today, considering both how
crowded it is predicted to be and when the user is actually free, and lets
them put the chosen slot on their calendar.

## Prerequisites

Needs the typical-weekday curve to be meaningful: at least 3 prior instances
of each weekday, ideally 6-8. The collector has been running since
2026-08-30, so the earliest this is worth attempting is late September 2026.
Nothing below can be usefully built before then.

## Algorithm

Intersect four constraints over today, then rank:

1. **Open hours** - from `hours.todays_hours()`, already built.
2. **Predicted crowding** - the median curve from `app.typical_curve()`,
   already built.
3. **Calendar free/busy** - busy intervals subtracted from the candidates.
4. **Minimum session length** - a 20-minute gap is not a gym trip. Needs a
   configurable duration, plus a travel buffer on each side.

Rank surviving windows by predicted occupancy, surface the best two or three.

## Calendar integration

### Read: free/busy only

Use Google's FreeBusy endpoint (`POST /calendar/v3/freeBusy`), which returns
only busy start/end intervals -- no titles, attendees, or locations. Request
the `calendar.freebusy` scope, **not** full calendar read. The app is then
structurally unable to see event contents.

Setup cost is real: Google Cloud project, OAuth consent screen, client
credentials, refresh-token flow. Add yourself as a test user to skip app
verification for personal use.

iCloud has no equivalent public API. If the user's calendar is iCloud-only,
the options are CalDAV with an app-specific password, or a published read-only
`.ics` feed. Both are worse. Prefer Google, or Google-synced phone calendars.

### Write: do not

Generate an `.ics` file for the chosen slot and let the user's own calendar
app import it. No write scope, no stored tokens with mutation rights, and it
works identically on iPhone, Google, and Outlook. ~20 lines.

## The blocker: the dashboard is public

`rsf-dashboard.fly.dev` has no authentication. Anyone with the link can load
it. Rendering calendar-derived data on it would publish the user's schedule
shape to the world -- free/busy intervals leak plenty even without titles.

**Authentication must land before any calendar data reaches the page.** This
is the piece most likely to be skipped in enthusiasm and regretted.

## Staging

1. **Quietest windows today** - constraints 1, 2, 4 only. No calendar, no
   auth, no accounts. Probably most of the value.
2. **Add to Calendar** - `.ics` download for a suggested window. Closes the
   loop with zero auth.
3. **Free/busy filtering** - constraint 3. Requires auth on the dashboard
   first. This is an explicit product requirement, not optional: suggestions
   must avoid times the user is already booked.

## Open parameters

Decide when building, not now: session duration, travel buffer, earliest and
latest acceptable hour, how many suggestions to show, and whether to consider
tomorrow when today is already busy or nearly over.

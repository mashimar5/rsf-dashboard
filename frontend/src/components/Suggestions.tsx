import type { Auth, SuggestionSet } from '../types'
import { clock, levelColor, pct } from '../lib/format'

interface Props {
  suggestions: SuggestionSet
  auth: Auth
}

async function signOut() {
  await fetch('/auth/logout', { method: 'POST' })
  window.location.reload()
}

export function Suggestions({ suggestions, auth }: Props) {
  return (
    <div className="card">
      <div className="cardhead">
        <h2>When to go</h2>
        {auth.signedIn ? (
          <span className="calstatus">
            Avoiding your calendar
            <button onClick={signOut} title={auth.email ?? undefined}>Disconnect</button>
          </span>
        ) : (
          <a className="connect" href="/auth/google">Connect Google Calendar</a>
        )}
      </div>

      {suggestions.windows.length ? (
        <ul className="suggestions">
          {suggestions.windows.map((window) => (
            <li key={window.start}>
              <span className="when">
                {window.section && <b>{window.section}</b>}
                {clock(window.start)}–{clock(window.end)}
              </span>
              <span className="level">
                <i style={{ background: levelColor(window.predictedPct) }} />
                {pct(window.predictedPct)}
                {window.spread != null && window.spread > 0.2 && (
                  <em title="past weeks disagreed a lot here"> · rough estimate</em>
                )}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty">{suggestions.refusal ?? 'Nothing to suggest right now.'}</p>
      )}

      {!auth.signedIn && suggestions.windows.length > 0 && (
        <p className="hint">These ignore your schedule. Connect a calendar to skip times you are busy.</p>
      )}
    </div>
  )
}

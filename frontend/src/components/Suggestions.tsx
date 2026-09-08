import { useState } from 'react'
import type { Auth, Booking, Preferences, SuggestionSet } from '../types'
import { clock, levelColor, pct, sameInstant } from '../lib/format'

interface Props {
  suggestions: SuggestionSet
  auth: Auth
  booking: Booking | null
  preferences: Preferences | null
  onChange: () => void
}

async function signOut() {
  await fetch('/auth/logout', { method: 'POST' })
  window.location.reload()
}

export function Suggestions({ suggestions, auth, booking, preferences, onChange }: Props) {
  const [busy, setBusy] = useState<string | null>(null)

  const activePreferences = describe(preferences)
  const [error, setError] = useState<string | null>(null)

  // The agent only writes after an explicit confirmation; nothing reaches the
  // calendar without this click.
  async function book(start: string) {
    setBusy(start)
    setError(null)
    const response = await fetch('/api/book', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start }),
    })
    setBusy(null)
    if (!response.ok) {
      setError((await response.json().catch(() => ({}))).error ?? 'Could not add it')
      return
    }
    onChange()
  }

  async function cancel() {
    setBusy('cancel')
    await fetch('/api/book', { method: 'DELETE' })
    setBusy(null)
    onChange()
  }

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
            <li key={window.start} className={sameInstant(booking?.start, window.start) ? 'booked' : ''}>
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
                {window.note && <em className="record"> · {window.note}</em>}
              </span>
              {auth.signedIn && (
                sameInstant(booking?.start, window.start) ? (
                  <button className="act on" onClick={cancel} disabled={busy !== null}>
                    {busy === 'cancel' ? '…' : 'On your calendar ✓'}
                  </button>
                ) : (
                  <button className="act" onClick={() => book(window.start)} disabled={busy !== null}>
                    {busy === window.start ? '…' : booking ? 'Move here' : 'Add'}
                  </button>
                )
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty">{suggestions.refusal ?? 'Nothing to suggest right now.'}</p>
      )}

      {activePreferences.length > 0 && (
        /* set through the chat below; shown here because this is what they affect */
        <p className="hint">Filtered by: {activePreferences.join(' · ')}</p>
      )}

      {error && <p className="hint err">{error}</p>}

      {!auth.signedIn && suggestions.windows.length > 0 && (
        <p className="hint">These ignore your schedule. Connect a calendar to skip times you are busy.</p>
      )}
    </div>
  )
}

const HOUR = (h: number) => `${h % 12 || 12}${h < 12 ? 'am' : 'pm'}`

/** Active preferences in words, for the line above the suggestions. */
function describe(p: Preferences | null): string[] {
  if (!p) return []
  const parts: string[] = []
  if (p.session_minutes) parts.push(`${p.session_minutes} min sessions`)
  if (p.earliest_hour != null) parts.push(`from ${HOUR(p.earliest_hour)}`)
  if (p.latest_hour != null) parts.push(`starting by ${HOUR(p.latest_hour)}`)
  if (p.max_crowding_pct != null) parts.push(`under ${Math.round(p.max_crowding_pct * 100)}% full`)
  if (p.travel_buffer_minutes) parts.push(`${p.travel_buffer_minutes} min around meetings`)
  return parts
}

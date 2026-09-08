import { useState } from 'react'
import type { Auth, Booking, SuggestionSet } from '../types'
import { clock, levelColor, pct } from '../lib/format'

interface Props {
  suggestions: SuggestionSet
  auth: Auth
  booking: Booking | null
  onChange: () => void
}

async function signOut() {
  await fetch('/auth/logout', { method: 'POST' })
  window.location.reload()
}

export function Suggestions({ suggestions, auth, booking, onChange }: Props) {
  const [busy, setBusy] = useState<string | null>(null)
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
            <li key={window.start} className={booking?.start === window.start ? 'booked' : ''}>
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
              {auth.signedIn && (
                booking?.start === window.start ? (
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

      {error && <p className="hint err">{error}</p>}

      {!auth.signedIn && suggestions.windows.length > 0 && (
        <p className="hint">These ignore your schedule. Connect a calendar to skip times you are busy.</p>
      )}
    </div>
  )
}

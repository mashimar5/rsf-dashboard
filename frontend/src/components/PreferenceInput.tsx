import { useState } from 'react'
import type { Preferences } from '../types'

interface Props {
  preferences: Preferences | null
  onChange: () => void
}

/** Says what it wants in words; the model turns that into numbers once, here.
 *  Everything downstream reads the numbers, so a suggestion is never one
 *  model call away from being different. */
export function PreferenceInput({ preferences, onChange }: Props) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!text.trim()) return
    setBusy(true)
    setError(null)
    const response = await fetch('/api/preferences', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    })
    setBusy(false)
    if (!response.ok) {
      setError((await response.json().catch(() => ({}))).error ?? 'Could not read that')
      return
    }
    setText('')
    onChange()
  }

  async function clear() {
    await fetch('/api/preferences', { method: 'DELETE' })
    onChange()
  }

  return (
    <div className="prefs">
      {preferences ? (
        <div className="understood">
          <span>{preferences.summary}</span>
          <button className="act" onClick={clear}>Clear</button>
        </div>
      ) : (
        <form onSubmit={submit}>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="e.g. 90 minute sessions, nothing after 11am, 30 min either side of meetings"
            disabled={busy}
          />
          <button className="act" type="submit" disabled={busy || !text.trim()}>
            {busy ? '…' : 'Apply'}
          </button>
        </form>
      )}
      {error && <p className="hint err">{error}</p>}
    </div>
  )
}

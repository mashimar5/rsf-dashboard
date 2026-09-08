import { useEffect, useRef, useState } from 'react'

interface Turn {
  role: 'user' | 'assistant'
  content: string
  toolsUsed?: string[]
}

/** A conversation about the data: patterns, comparisons, what to expect.
 *  The dashboard already answers "how busy is it" and "when should I go"
 *  better than a sentence could; this is for the open-ended questions. */
export function Ask({ onChange }: { onChange: () => void }) {
  const [turns, setTurns] = useState<Turn[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [turns, busy])

  async function send(event: React.FormEvent) {
    event.preventDefault()
    const question = draft.trim()
    if (!question || busy) return

    const next: Turn[] = [...turns, { role: 'user', content: question }]
    setTurns(next)
    setDraft('')
    setBusy(true)
    setError(null)

    const response = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // the whole exchange goes back, so follow-ups need no restating
      body: JSON.stringify({ messages: next.map(({ role, content }) => ({ role, content })) }),
    })
    setBusy(false)

    if (!response.ok) {
      setError((await response.json().catch(() => ({}))).error ?? 'Could not answer that')
      setTurns(turns)   // drop the unanswered question rather than stranding it
      return
    }
    const result = await response.json()
    setTurns([...next, { role: 'assistant', content: result.answer, toolsUsed: result.toolsUsed }])
    // a turn may have changed settings, so refresh what the page is showing
    if ((result.toolsUsed ?? []).some((t: string) => t.endsWith('_preferences'))) onChange()
  }

  return (
    <div className="card chat">
      <div className="cardhead">
        <h2>Ask about the data</h2>
        {turns.length > 0 && (
          <button className="act" onClick={() => { setTurns([]); setError(null) }}>Clear</button>
        )}
      </div>

      {turns.length === 0 && !busy && (
        <p className="hint">
          Ask about patterns — “what patterns do you see?”, “how busy will Thursday
          evening be?” — or set what you want from suggestions: “90 minute sessions,
          nothing after 11am”.
        </p>
      )}

      <div className="transcript">
        {turns.map((turn, index) => (
          <div key={index} className={`turn ${turn.role}`}>
            <p>{turn.content}</p>
            {turn.toolsUsed && turn.toolsUsed.length > 0 && (
              /* shown so an answer can be checked against what it actually read */
              <p className="tools">read: {[...new Set(turn.toolsUsed)].join(', ')}</p>
            )}
          </div>
        ))}
        {busy && <div className="turn assistant"><p className="thinking">Looking…</p></div>}
        <div ref={endRef} />
      </div>

      {error && <p className="hint err">{error}</p>}

      <form className="askbox" onSubmit={send}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={turns.length ? 'Follow up…' : 'Ask anything about the occupancy data'}
          disabled={busy}
        />
        <button className="act" type="submit" disabled={busy || !draft.trim()}>Send</button>
      </form>
    </div>
  )
}

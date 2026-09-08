import { useState } from 'react'

interface Exchange {
  question: string
  answer: string
  toolsUsed: string[]
}

/** For questions no fixed widget can anticipate — comparisons across
 *  weekdays, whether a day was unusual. Everything the dashboard already
 *  answers directly stays where it is. */
export function Ask() {
  const [question, setQuestion] = useState('')
  const [history, setHistory] = useState<Exchange[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    const asked = question.trim()
    if (!asked) return
    setBusy(true)
    setError(null)
    const response = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: asked }),
    })
    setBusy(false)
    if (!response.ok) {
      setError((await response.json().catch(() => ({}))).error ?? 'Could not answer that')
      return
    }
    const result = await response.json()
    setHistory((prior) => [{ question: asked, ...result }, ...prior].slice(0, 5))
    setQuestion('')
  }

  return (
    <div className="card">
      <div className="cardhead"><h2>Ask about the data</h2></div>

      <form className="askbox" onSubmit={submit}>
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. are Monday evenings busier than Friday evenings?"
          disabled={busy}
        />
        <button className="act" type="submit" disabled={busy || !question.trim()}>
          {busy ? '…' : 'Ask'}
        </button>
      </form>

      {error && <p className="hint err">{error}</p>}

      {history.map((exchange, index) => (
        <div className="exchange" key={index}>
          <p className="q">{exchange.question}</p>
          <p className="a">{exchange.answer}</p>
          {exchange.toolsUsed.length > 0 && (
            /* shown so an answer can be checked against what it actually read */
            <p className="tools">read: {[...new Set(exchange.toolsUsed)].join(', ')}</p>
          )}
        </div>
      ))}
    </div>
  )
}

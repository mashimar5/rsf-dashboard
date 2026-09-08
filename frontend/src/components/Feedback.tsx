import { useState } from 'react'
import type { FeedbackPrompt } from '../types'
import { clock } from '../lib/format'

interface Props {
  prompt: FeedbackPrompt
  onAnswer: () => void
}

/** The only place attendance can come from. The occupancy sensor counts bodies
 *  at a doorway, not identities, so nothing in the data can tell whether a
 *  particular person turned up. */
export function Feedback({ prompt, onAnswer }: Props) {
  const [saving, setSaving] = useState(false)

  async function answer(went: boolean) {
    setSaving(true)
    await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ predictionId: prompt.predictionId, went }),
    })
    setSaving(false)
    onAnswer()
  }

  const when = `${clock(prompt.start)}–${clock(prompt.end)}`

  if (prompt.answered !== null) {
    return (
      <div className="card askrow">
        <span>{prompt.answered ? `You went at ${when}.` : `You skipped ${when}.`}</span>
        <button className="act" onClick={() => answer(!prompt.answered)} disabled={saving}>
          Change
        </button>
      </div>
    )
  }

  return (
    <div className="card askrow">
      <span>You booked {when}. Did you go?</span>
      <span className="answers">
        <button className="act" onClick={() => answer(true)} disabled={saving}>Yes</button>
        <button className="act" onClick={() => answer(false)} disabled={saving}>No</button>
      </span>
    </div>
  )
}

import type { SuggestionSet } from '../types'
import { clock, levelColor, pct } from '../lib/format'

export function Suggestions({ suggestions }: { suggestions: SuggestionSet }) {
  if (!suggestions.windows.length) {
    return (
      <div className="card">
        <h2>When to go</h2>
        <p className="empty">{suggestions.refusal ?? 'Nothing to suggest right now.'}</p>
      </div>
    )
  }

  return (
    <div className="card">
      <h2>When to go</h2>
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
              {/* a wide spread means past instances of this weekday disagreed
                  here, so the single number should not be read too closely */}
              {window.spread != null && window.spread > 0.2 && (
                <em title="past weeks disagreed a lot here"> · rough estimate</em>
              )}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

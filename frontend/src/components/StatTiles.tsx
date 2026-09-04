import type { Summary } from '../types'
import { clock, pct } from '../lib/format'

export function StatTiles({ summary }: { summary: Summary }) {
  return (
    <div className="stats">
      <div className="stat">
        <div className="label">Peak</div>
        <div className="value">{pct(summary.peak.percentage)}</div>
        <div className="sub">
          {summary.peak.count} of {summary.peak.capacity} at {clock(summary.peak.at)}
        </div>
      </div>

      {summary.quietest && (
        <div className="stat">
          <div className="label">Quietest Hour</div>
          <div className="value">{pct(summary.quietest.percentage)}</div>
          <div className="sub">
            {clock(summary.quietest.start)}–{clock(summary.quietest.end)}
          </div>
        </div>
      )}

      <div className="stat">
        <div className="label">Average</div>
        <div className="value">{pct(summary.averagePct)}</div>
        <div className="sub">{summary.openOnly ? 'While open' : 'Whole day'}</div>
      </div>
    </div>
  )
}

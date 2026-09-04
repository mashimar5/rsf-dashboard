import { useCallback, useEffect, useState } from 'react'
import { Chart } from './components/Chart'
import { DayNav } from './components/DayNav'
import { StatTiles } from './components/StatTiles'
import type { DayView } from './types'
import { clock, levelColor, pct } from './lib/format'

/** The viewed date lives in the URL, so a day stays linkable and the back
 *  button works -- the same contract the server-rendered version had. */
function dateFromUrl() {
  return new URLSearchParams(window.location.search).get('date') ?? ''
}

export default function App() {
  const [date, setDate] = useState(dateFromUrl)
  const [day, setDay] = useState<DayView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const onPop = () => setDate(dateFromUrl())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const response = await fetch(`/api/day${date ? `?date=${date}` : ''}`, { signal })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      setDay(await response.json())
      setError(null)
    } catch (problem) {
      if ((problem as Error).name !== 'AbortError') setError(String(problem))
    }
  }, [date])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  // today refreshes itself; a past day never changes
  useEffect(() => {
    if (!day?.isToday) return
    const timer = setInterval(() => load(), 60_000)
    return () => clearInterval(timer)
  }, [day?.isToday, load])

  const select = (next: string) => {
    const isToday = next === day?.nav.today
    window.history.pushState({}, '', isToday ? '/' : `/?date=${next}`)
    setDate(isToday ? '' : next)
  }

  if (error && !day) return <main><p className="empty">Could not load: {error}</p></main>
  if (!day) return <main><p className="empty">Loading…</p></main>

  const live = day.live
  const accent = levelColor(live ? live.percentage : day.summary?.peak.percentage)

  return (
    <main>
      <h1>RSF WEIGHT ROOMS</h1>

      <DayNav date={day.date} label={day.label} isToday={day.isToday}
              nav={day.nav} onSelect={select} />

      <div className="card">
        {day.isToday && live && (
          <>
            <div className="count">
              {live.count}<small> / {live.capacity}</small>
            </div>
            <div className="bar">
              <div style={{ width: `${(live.percentage ?? 0) * 100}%`, background: accent }} />
            </div>
            <div className="meta">
              {live.percentage != null && `${pct(live.percentage)} full · `}
              {live.isLive
                ? `as of ${clock(live.observedAt)}`
                : <span className="stale">API unreachable — last reading {clock(live.observedAt)}</span>}
            </div>
          </>
        )}

        {!day.isToday && day.summary && <StatTiles summary={day.summary} />}
        {!day.isToday && !day.summary && (
          <div className="empty">Nothing was recorded on this day.</div>
        )}

        {day.hours && (
          <div className={`hours${!day.isToday && day.summary ? ' after-stats' : ''}`}>
            {day.isToday ? 'Open today ' : 'Hours '}
            {day.hours.closed
              ? <span className="closed">{day.hours.text}</span>
              : <b>{day.hours.text}</b>}
          </div>
        )}
      </div>

      <div className="card">
        <Chart day={day} />
      </div>
    </main>
  )
}

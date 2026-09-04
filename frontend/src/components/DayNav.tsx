import type { Nav } from '../types'

interface Props {
  date: string
  label: string
  isToday: boolean
  nav: Nav
  onSelect: (date: string) => void
}

export function DayNav({ date, label, isToday, nav, onSelect }: Props) {
  return (
    <div className="nav">
      <button onClick={() => nav.prev && onSelect(nav.prev)} disabled={!nav.prev}
              title="Previous day" aria-label="Previous day">←</button>
      <button onClick={() => nav.next && onSelect(nav.next)} disabled={!nav.next}
              title="Next day" aria-label="Next day">→</button>

      <span className="day">{label}</span>

      {!isToday && (
        <button className="today" onClick={() => onSelect(nav.today)}>Today</button>
      )}

      <input
        type="date"
        value={date}
        min={nav.earliest}
        max={nav.today}
        onChange={(event) => event.target.value && onSelect(event.target.value)}
      />
    </div>
  )
}

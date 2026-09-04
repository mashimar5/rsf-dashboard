import { useMemo, useRef, useState } from 'react'
import type { DayView } from '../types'
import { hourLabel, pct } from '../lib/format'

const WIDTH = 720
const HEIGHT = 180
const MINUTES_IN_DAY = 1440
const NEAR_MINUTES = 15   // beyond this, the cursor is over a time with no reading

interface Hover {
  minute: number
  count: number
  capacity: number
}

const x = (minute: number) => (minute / MINUTES_IN_DAY) * WIDTH
const y = (fraction: number) => HEIGHT - fraction * HEIGHT

export function Chart({ day }: { day: DayView }) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<Hover | null>(null)

  const xTicks = useMemo(
    () => Array.from({ length: 25 }, (_, hour) => ({ hour, major: hour % 4 === 0 })),
    [],
  )
  const yTicks = useMemo(() => [0, 25, 50, 75, 100], [])

  const todayPoints = useMemo(
    () =>
      day.samples
        .filter(([, , capacity]) => capacity > 0)
        .map(([minute, count, capacity]) => `${x(minute).toFixed(1)},${y(count / capacity).toFixed(1)}`)
        .join(' '),
    [day.samples],
  )

  const typicalPoints = useMemo(
    () =>
      day.typical?.points
        .map(([minute, fraction]) => `${x(minute).toFixed(1)},${y(fraction).toFixed(1)}`)
        .join(' ') ?? '',
    [day.typical],
  )

  function track(event: React.PointerEvent<HTMLDivElement>) {
    const box = svgRef.current?.getBoundingClientRect()
    if (!box) return
    const fraction = (event.clientX - box.left) / box.width
    if (fraction < 0 || fraction > 1) return setHover(null)

    const target = fraction * MINUTES_IN_DAY
    let best: Hover | null = null
    let bestGap = Infinity
    for (const [minute, count, capacity] of day.samples) {
      const gap = Math.abs(minute - target)
      if (gap < bestGap) {
        bestGap = gap
        best = { minute, count, capacity }
      }
    }
    setHover(bestGap > NEAR_MINUTES ? null : best)
  }

  if (!day.samples.length && !day.typical) {
    return (
      <div className="empty">
        {day.isToday ? 'Waiting for today’s first readings.' : 'Nothing recorded on this day.'}
      </div>
    )
  }

  const hoverFraction = hover && hover.capacity ? hover.count / hover.capacity : 0

  return (
    <div className="chart">
      <div className="plot" onPointerMove={track} onPointerDown={track}
           onPointerLeave={() => setHover(null)}>
        <div className="ylabels">
          {yTicks.map((tick) => (
            <span key={tick} style={{ top: `${100 - tick}%` }}>{tick}%</span>
          ))}
        </div>

        <svg ref={svgRef} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none"
             role="img" aria-label={`Occupancy across ${day.label}`}>
          {xTicks.map(({ hour, major }) => (
            <line key={hour} x1={x(hour * 60)} y1={0} x2={x(hour * 60)} y2={HEIGHT}
                  stroke="var(--line)" vectorEffect="non-scaling-stroke"
                  opacity={major ? 1 : 0.45} />
          ))}
          {yTicks.map((tick) => (
            <line key={tick} x1={0} y1={y(tick / 100)} x2={WIDTH} y2={y(tick / 100)}
                  stroke="var(--line)" vectorEffect="non-scaling-stroke"
                  opacity={tick === 0 || tick === 100 ? 1 : 0.45} />
          ))}

          {typicalPoints && (
            <polyline points={typicalPoints} fill="none" stroke="var(--dim)" strokeWidth={2}
                      strokeDasharray="5 4" strokeLinejoin="round" strokeLinecap="round"
                      vectorEffect="non-scaling-stroke" />
          )}
          {todayPoints && (
            <polyline points={todayPoints} fill="none" stroke="var(--accent)" strokeWidth={2}
                      strokeLinejoin="round" strokeLinecap="round"
                      vectorEffect="non-scaling-stroke" />
          )}
        </svg>

        {hover && (
          <>
            <div className="cursor" style={{ left: `${x(hover.minute) / WIDTH * 100}%` }} />
            <div className="dot" style={{
              left: `${x(hover.minute) / WIDTH * 100}%`,
              top: `${(1 - hoverFraction) * 100}%`,
            }} />
            <div className="tip" style={{
              left: `${Math.min(Math.max(x(hover.minute) / WIDTH * 100, 8), 92)}%`,
              top: `${(1 - hoverFraction) * 100}%`,
            }}>
              <b>{hover.count}</b> / {hover.capacity} · <b>{pct(hoverFraction)}</b>
              <br />
              <span className="t">{minuteClock(hover.minute)}</span>
            </div>
          </>
        )}
      </div>

      <div className="ticks">
        {xTicks.map(({ hour, major }, index) => (
          <span key={hour} className={major ? 'major' : 'minor'}
                style={{
                  left: `${(hour / 24) * 100}%`,
                  transform: `translateX(${index === 0 ? '0' : index === xTicks.length - 1 ? '-100%' : '-50%'})`,
                }}>
            {hourLabel(hour)}
          </span>
        ))}
      </div>

      <div className="legend">
        {day.samples.length > 1 && (
          <span><i className="today" />{day.shortLabel} · {day.samples.length} samples</span>
        )}
        {day.typical && (
          <span><i className="typical" />Typical {day.typical.weekday} · {day.typical.weeks} weeks</span>
        )}
      </div>
    </div>
  )
}

/** Minutes since local midnight -> "9:23 AM" */
function minuteClock(minute: number) {
  const hour = Math.floor(minute / 60) % 24
  const rest = Math.floor(minute % 60)
  const suffix = hour < 12 ? 'AM' : 'PM'
  return `${hour % 12 || 12}:${String(rest).padStart(2, '0')} ${suffix}`
}

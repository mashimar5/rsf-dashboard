/** Shapes returned by GET /api/day. Kept in one place so a backend change
 *  that breaks the contract shows up as a type error rather than undefined. */

export interface Reading {
  count: number
  capacity: number
  percentage: number
  at: string
}

export interface Quietest {
  percentage: number
  start: string
  end: string
}

export interface Summary {
  peak: Reading
  quietest: Quietest | null
  averagePct: number
  openOnly: boolean
}

export interface Live {
  count: number
  capacity: number
  percentage: number | null
  observedAt: string
  isLive: boolean
}

export interface Hours {
  text: string
  opens: number | null
  closes: number | null
  closed: boolean
}

export interface Typical {
  weeks: number
  weekday: string
  /** Mean disagreement across the day, for context. Null when every bucket
   *  has a single instance. */
  spread: number | null
  /** [minuteOfDay, median, low, high] — low/high are the range across past
   *  instances of this weekday, drawn as a band behind the median line. */
  points: [number, number, number, number][]
}

export interface Nav {
  prev: string | null
  next: string | null
  earliest: string
  today: string
}

export interface DayView {
  date: string
  isToday: boolean
  label: string
  shortLabel: string
  nav: Nav
  live: Live | null
  summary: Summary | null
  /** [minuteOfDay, count, capacity] */
  samples: [number, number, number][]
  typical: Typical | null
  hours: Hours | null
}

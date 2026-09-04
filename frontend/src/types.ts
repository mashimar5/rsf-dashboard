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
  /** [minuteOfDay, fractionFull] */
  points: [number, number][]
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

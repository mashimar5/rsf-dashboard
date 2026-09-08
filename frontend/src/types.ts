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

export interface SuggestedWindow {
  start: string
  end: string
  predictedPct: number
  /** Disagreement across past instances in these buckets; null when a single
   *  instance backs them. Wide means treat the number loosely. */
  spread: number | null
  section: string | null
  /** How this part of the day has fared before, once there is enough
   *  evidence to say. Advisory: it never removes a suggestion. */
  note: string | null
}

export interface SuggestionSet {
  windows: SuggestedWindow[]
  /** Why there is nothing to suggest, when windows is empty. */
  refusal: string | null
}

export interface Booking {
  start: string
  end: string
  predictedPct: number | null
}

export interface FeedbackPrompt {
  predictionId: number
  start: string
  end: string
  /** null means never asked, which is different from answering no. */
  answered: boolean | null
}

export interface Preferences {
  session_minutes: number | null
  earliest_hour: number | null
  latest_hour: number | null
  max_crowding_pct: number | null
  travel_buffer_minutes: number | null
  sessions_per_week: number | null
  /** What the model understood, shown back so it can be corrected. */
  summary: string
}

export interface Auth {
  signedIn: boolean
  email: string | null
  /** True when suggestions are filtered by the signed-in user's calendar. */
  calendarAware: boolean
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
  /** Today only — 'when should I go' is not a question about a finished day. */
  suggestions: SuggestionSet | null
  /** Today's confirmed window, written to the app's own calendar. */
  booking: Booking | null
  /** Set on a past day that had a booking, so the visit can be confirmed. */
  feedback: FeedbackPrompt | null
  /** True when the newest reading is old enough that collection is
   *  probably broken. Occupancy cannot be backfilled, so silence is costly. */
  stale: boolean
  preferences: Preferences | null
  /** False when no API key is configured; the input is hidden. */
  /** False when no API key is configured; the question box is hidden. */
  askAvailable: boolean
  auth: Auth
  hours: Hours | null
}

export const pct = (fraction: number) => `${Math.round(fraction * 100)}%`

export const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })

/** Minutes since midnight -> "8a", "12p" */
export const hourLabel = (hour: number) => {
  const h = hour % 24
  return `${h % 12 || 12}${h < 12 ? 'a' : 'p'}`
}

/** Green below half full, amber to 85%, red above. Mirrors level_color() in app.py. */
export const levelColor = (fraction: number | null | undefined) => {
  if (fraction == null) return 'var(--neutral)'
  if (fraction < 0.5) return 'var(--green)'
  if (fraction < 0.85) return 'var(--amber)'
  return 'var(--red)'
}

// Formatting rules — FRONTEND_SPEC §05. Apply everywhere, no exceptions.

const inr = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  maximumFractionDigits: 0,
})

const inrPlain = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 })

/** Every `_paise` field ÷ 100, Indian digit grouping. Never render raw paise. */
export function rupees(paise: number): string {
  return inr.format(paise / 100)
}

/** Signed rupee delta, e.g. "+₹28" / "−₹2,697" (uses a true minus sign). */
export function rupeesSigned(paise: number): string {
  const v = paise / 100
  const sign = v >= 0 ? '+' : '−'
  return `${sign}₹${inrPlain.format(Math.abs(v))}`
}

export function count(n: number): string {
  return inrPlain.format(n)
}

export function signedCount(n: number): string {
  const sign = n >= 0 ? '+' : '−'
  return `${sign}${inrPlain.format(Math.abs(n))}`
}

export function pct(x: number, digits = 1): string {
  return `${(x * 100).toFixed(digits)}%`
}

/** mm:ss countdown from seconds. */
export function mmss(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  const m = Math.floor(s / 60)
  const r = s % 60
  return `${m}:${String(r).padStart(2, '0')}`
}

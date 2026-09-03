import { motion } from 'framer-motion'
import type { Overview } from '../lib/types'
import { count } from '../lib/format'
import { usePrefersReducedMotion } from '../lib/hooks'

// §03.3 + §06.2 — horizontal staged bars, left-to-right 60ms stagger fill.
// Every stage is a real count from the overview payload.
export function Funnel({ o }: { o: Overview }) {
  const reduced = usePrefersReducedMotion()
  const stages: { label: string; value: number; color: string }[] = [
    { label: 'At risk', value: o.at_risk, color: 'var(--slate-600)' },
    { label: 'Actions taken', value: o.actions_taken, color: 'var(--brand-tint)' },
    { label: 'Retries attempted', value: o.retry_attempted, color: 'var(--brand-soft)' },
    { label: 'Nudges sent', value: o.nudges_sent, color: 'var(--brand)' },
    { label: 'Recovered', value: o.recovered, color: 'var(--good)' },
    { label: 'Escalated', value: o.escalated, color: 'var(--warn)' },
    { label: 'Blocked', value: o.blocked, color: 'var(--critical)' },
  ]
  const max = Math.max(1, ...stages.map((s) => s.value))

  return (
    <div className="grid gap-2.5">
      {stages.map((s, i) => (
        <div key={s.label} className="flex items-center gap-3">
          <div className="w-36 shrink-0 text-sm text-slate-600">{s.label}</div>
          <div className="relative h-7 flex-1 overflow-hidden rounded-md bg-slate-100">
            <motion.div
              className="h-full rounded-md"
              style={{ background: s.color }}
              initial={reduced ? false : { width: 0 }}
              animate={{ width: `${(s.value / max) * 100}%` }}
              transition={reduced ? { duration: 0 } : { delay: i * 0.06, duration: 0.5, ease: 'easeOut' }}
            />
          </div>
          <div className="w-16 shrink-0 text-right tabular-nums text-sm font-semibold text-ink">
            {count(s.value)}
          </div>
        </div>
      ))}
    </div>
  )
}

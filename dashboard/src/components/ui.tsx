import type { ReactNode } from 'react'
import type { Outcome, RootCauseClass, Verdict } from '../lib/types'

// ── Numbered editorial section header (§02 structural motif) ──────────────
export function SectionHeader({
  index,
  title,
  caption,
  right,
}: {
  index: string
  title: string
  caption?: ReactNode
  right?: ReactNode
}) {
  return (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div>
        <div className="flex items-baseline gap-2">
          <span className="font-medium tabular-nums text-slate-600">{index} —</span>
          <h2 className="text-lg font-bold text-ink-deep">{title}</h2>
        </div>
        {caption && <p className="mt-1 max-w-2xl text-sm text-slate-600">{caption}</p>}
      </div>
      {right}
    </div>
  )
}

export function Panel({ children, className = '', elevated = false }: { children: ReactNode; className?: string; elevated?: boolean }) {
  return (
    <section
      className={`rounded-xl border border-slate-300 bg-surface ${elevated ? 'shadow-[0_1px_2px_rgba(8,13,41,0.04),0_12px_32px_-16px_rgba(8,13,41,0.18)]' : ''} ${className}`}
    >
      {children}
    </section>
  )
}

// ── Status palette (RESERVED: recovered/blocked/escalated/written-off) ────
// Always icon + label, never color alone (§02).
const STATUS: Record<Outcome, { label: string; color: string; bg: string; icon: string }> = {
  recovered: { label: 'Recovered', color: 'var(--good)', bg: 'rgba(72,208,140,0.12)', icon: '✓' },
  blocked: { label: 'Blocked', color: 'var(--critical)', bg: 'rgba(240,38,60,0.10)', icon: '⊘' },
  escalated: { label: 'Escalated', color: 'var(--warn)', bg: 'rgba(245,166,35,0.14)', icon: '↑' },
  written_off: { label: 'Written off', color: 'var(--neutral)', bg: 'rgba(108,132,157,0.12)', icon: '–' },
  at_risk: { label: 'At risk', color: 'var(--brand)', bg: 'var(--brand-wash)', icon: '•' },
}

export function StatusChip({ outcome }: { outcome: Outcome }) {
  const s = STATUS[outcome]
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium"
      style={{ color: s.color, background: s.bg }}
    >
      <span aria-hidden className="text-[11px] leading-none">{s.icon}</span>
      {s.label}
    </span>
  )
}

export function statusColor(o: Outcome) {
  return STATUS[o].color
}

// ── Verdict chips (§04) ───────────────────────────────────────────────────
const VERDICT: Record<Exclude<Verdict, null>, { label: string; color: string; bg: string }> = {
  APPROVE: { label: 'APPROVE', color: 'var(--brand)', bg: 'var(--brand-wash)' },
  DENY: { label: 'DENY', color: 'var(--critical)', bg: 'rgba(240,38,60,0.10)' },
  ESCALATE_HUMAN: { label: 'ESCALATE HUMAN', color: 'var(--warn)', bg: 'rgba(245,166,35,0.14)' },
  REDIRECT_TO_WINDOW: { label: 'REDIRECT TO WINDOW', color: 'var(--warn)', bg: 'rgba(245,166,35,0.14)' },
}

export function VerdictChip({ verdict, large = false }: { verdict: Verdict; large?: boolean }) {
  if (!verdict) return <span className="text-sm text-slate-600">—</span>
  const v = VERDICT[verdict]
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-lg font-bold tracking-wide ${large ? 'px-3 py-1.5 text-sm' : 'px-2 py-0.5 text-xs'}`}
      style={{ color: v.color, background: v.bg }}
    >
      {v.label}
    </span>
  )
}

export function verdictColor(v: Verdict) {
  return v ? VERDICT[v].color : 'var(--slate-600)'
}

// ── Root-cause identity chips (NOT the status palette — §03.2) ────────────
const ROOT: Record<RootCauseClass, { label: string; color: string }> = {
  TD: { label: 'TD', color: 'var(--id-td)' },
  BD_transient: { label: 'BD transient', color: 'var(--id-bdt)' },
  BD_hard: { label: 'BD hard', color: 'var(--id-bdh)' },
}

export function RootCauseChip({ root }: { root: RootCauseClass | null }) {
  // Root cause is unset on attempts the classifier hasn't reached yet
  // (e.g. a censored/not-yet-observed retry) — render a neutral placeholder
  // rather than crash on an unindexed lookup.
  if (!root) {
    return (
      <span className="inline-flex items-center rounded-md border border-slate-300 px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
        —
      </span>
    )
  }
  const r = ROOT[root]
  return (
    <span
      className="inline-flex items-center rounded-md border px-1.5 py-0.5 font-mono text-[11px]"
      style={{ color: r.color, borderColor: r.color }}
    >
      {r.label}
    </span>
  )
}

// ── Attempts mini-bar (4 segments, filled = used) ─────────────────────────
export function AttemptsBar({ used, remaining }: { used: number; remaining: number }) {
  const cap = used + remaining || 4
  const exhausted = remaining <= 0
  return (
    <div className="flex items-center gap-2">
      <span className="tabular-nums text-sm text-ink">
        {used} / {cap}
      </span>
      <div className="flex gap-0.5" aria-hidden>
        {Array.from({ length: cap }).map((_, i) => (
          <span
            key={i}
            className="h-3 w-2 rounded-[2px]"
            style={{
              background: i < used ? (exhausted ? 'var(--critical)' : 'var(--brand)') : 'var(--slate-300)',
            }}
          />
        ))}
      </div>
    </div>
  )
}

// ── Data states ───────────────────────────────────────────────────────────
export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-3 py-10 text-sm text-slate-600">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-brand" />
      {label}…
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const msg = error instanceof Error ? error.message : String(error)
  return (
    <div className="rounded-xl border border-slate-300 bg-surface p-6">
      <p className="text-sm font-medium text-ink-deep">Couldn’t load this from the API.</p>
      <p className="mt-1 font-mono text-xs text-critical">{msg}</p>
      <p className="mt-2 text-xs text-slate-600">
        No number is invented client-side. Point <code className="font-mono">VITE_API_BASE</code> at the
        FastAPI backend, or run against the bundled demo dataset.
      </p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-3 rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-ink hover:bg-brand-wash"
        >
          Retry
        </button>
      )}
    </div>
  )
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-surface p-8 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {hint && <p className="mt-1 text-sm text-slate-600">{hint}</p>}
    </div>
  )
}

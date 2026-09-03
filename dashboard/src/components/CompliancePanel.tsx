import { useState } from 'react'
import type { ComposedVerdict, CompliancePanelData } from '../lib/types'
import { rupees } from '../lib/format'
import { AttemptsBar, RootCauseChip, VerdictChip, verdictColor } from './ui'
import { WindowStrip } from './WindowStrip'

// FRONTEND_SPEC §04. One shape, one component. Renders exactly what the API
// returns and computes nothing — not even the "is this good" framing. The
// verdict colors carry that. Reused standalone on /compliance and embedded in
// /invoices/[id].
export function CompliancePanel({ data }: { data: CompliancePanelData }) {
  return (
    <div className="grid gap-4">
      {/* rule row */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <RuleTile title="NPCI retry cap" verdict={data.npci.verdict} detail={data.npci.detail}>
          <AttemptsBar used={data.npci.attempts_used} remaining={data.npci.attempts_remaining} />
        </RuleTile>

        <RuleTile title="AFA ceiling" verdict={data.afa.verdict} detail={data.afa.detail}>
          <CeilingGauge amount={data.amount_paise} ceiling={data.afa.ceiling_paise} />
          <p className="mt-1 text-xs text-slate-600">MCC · {data.afa.mcc_category}</p>
        </RuleTile>

        <RuleTile title="Consent gate" verdict={data.consent.verdict} detail={data.consent.detail}>
          <span className="inline-flex items-center rounded-md bg-slate-100 px-2 py-0.5 text-sm font-medium capitalize text-ink">
            {data.consent.status}
          </span>
        </RuleTile>

        <RuleTile title="Pre-debit notice" verdict={data.pre_debit_notice.verdict} detail={data.pre_debit_notice.detail}>
          <div className="text-xs text-slate-600">
            <div>notice · {data.pre_debit_notice.notice_sent_at ?? '—'}</div>
            <div>charge · {data.pre_debit_notice.charge_at ?? '—'}</div>
          </div>
        </RuleTile>
      </div>

      {/* window strip (shared with /compliance) */}
      <div className="rounded-xl border border-slate-300 bg-surface p-4">
        <div className="mb-3 flex items-center gap-2 text-xs font-medium text-slate-600">
          <span>Non-peak window</span>
          {data.root_cause && (
            <span className="ml-auto flex items-center gap-1">
              root cause <RootCauseChip root={data.root_cause} />
            </span>
          )}
        </div>
        <WindowStrip window={data.window} />
      </div>

      {/* two composed verdict cards */}
      <div className="grid gap-4 md:grid-cols-2">
        <VerdictCard title="Retry" v={data.retry} />
        <VerdictCard title="Nudge" v={data.nudge} />
      </div>
    </div>
  )
}

function RuleTile({
  title,
  verdict,
  detail,
  children,
}: {
  title: string
  verdict: CompliancePanelData['npci']['verdict']
  detail: string
  children: React.ReactNode
}) {
  return (
    <div className="rounded-xl border border-slate-300 bg-surface p-4">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-600">{title}</span>
        <VerdictChip verdict={verdict} />
      </div>
      {children}
      <p className="mt-2 text-xs leading-snug text-slate-600">{detail}</p>
    </div>
  )
}

function CeilingGauge({ amount, ceiling }: { amount: number; ceiling: number }) {
  const ratio = Math.min(1, amount / ceiling)
  const over = amount > ceiling
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm">
        <span className="tabular-nums font-semibold text-ink">{rupees(amount)}</span>
        <span className="tabular-nums text-xs text-slate-600">/ {rupees(ceiling)}</span>
      </div>
      <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-slate-100">
        <div
          className="h-full rounded-full"
          style={{ width: `${ratio * 100}%`, background: over ? 'var(--critical)' : 'var(--brand)' }}
        />
      </div>
    </div>
  )
}

// A composed verdict card — large chip, full authorizing_rule in mono, and an
// expandable per-rule results list so a reviewer sees which rule decided it.
function VerdictCard({ title, v }: { title: string; v: ComposedVerdict }) {
  const [open, setOpen] = useState(false)

  // retry.verdict === null → plain disabled state with its detail, never a fake verdict.
  if (v.verdict === null || v.verdict === undefined) {
    return (
      <div className="rounded-xl border border-dashed border-slate-300 bg-slate-100/60 p-4">
        <div className="flex items-center justify-between">
          <span className="text-sm font-bold text-slate-600">{title}</span>
          <span className="text-xs font-medium text-slate-600">not applicable</span>
        </div>
        <p className="mt-2 text-sm text-slate-600">{v.detail ?? 'No decision on record.'}</p>
      </div>
    )
  }

  const accent = verdictColor(v.verdict)
  return (
    <div className="rounded-xl border bg-surface p-4" style={{ borderColor: accent }}>
      <div className="flex items-center justify-between">
        <span className="text-sm font-bold text-ink-deep">{title}</span>
        <VerdictChip verdict={v.verdict} large />
      </div>

      {v.authorizing_rule && (
        <p className="mt-3 break-words rounded-md bg-slate-100 p-2 font-mono text-xs leading-relaxed text-ink">
          {v.authorizing_rule}
        </p>
      )}

      {v.stop_reason && (
        <p className="mt-2 font-mono text-xs" style={{ color: 'var(--critical)' }}>
          stop_reason · {v.stop_reason}
        </p>
      )}

      {v.suggested_slots && v.suggested_slots.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-slate-600">slots</span>
          {v.suggested_slots.map((s) => (
            <span key={s} className="rounded border border-slate-300 px-1.5 py-0.5 font-mono text-[11px] text-ink">
              {s}
            </span>
          ))}
        </div>
      )}

      {v.results && v.results.length > 0 && (
        <div className="mt-3">
          <button
            onClick={() => setOpen((o) => !o)}
            className="text-xs font-medium text-brand hover:text-brand-dim"
          >
            {open ? 'Hide' : 'Show'} {v.results.length} rule verdicts
          </button>
          {open && (
            <ul className="mt-2 divide-y divide-slate-300 rounded-md border border-slate-300">
              {v.results.map((r) => (
                <li key={r.rule} className="flex items-center gap-3 px-3 py-2">
                  <VerdictChip verdict={r.verdict} />
                  <span className="font-mono text-xs text-ink">{r.rule}</span>
                  <span className="ml-auto text-right text-xs text-slate-600">{r.detail}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

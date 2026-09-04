import { Link, useParams, useSearchParams } from 'react-router-dom'
import { getInvoice, getInvoiceCompliance, HttpError } from '../lib/api'
import { rupees } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { CopyableId, EmptyState, ErrorState, Panel, PanelSkeleton, SectionHeader, Skeleton, StatusChip, RootCauseChip, VerdictChip } from '../components/ui'
import { CompliancePanel } from '../components/CompliancePanel'
import type { Attempt, AuditRow, Candidate, Decision } from '../lib/types'

export function InvoicePage() {
  const { id = '' } = useParams()
  const [params] = useSearchParams()
  const runId = params.get('run_id') ?? undefined

  const invoice = useAsync(() => getInvoice(id, runId), [id, runId])
  const compliance = useAsync(() => getInvoiceCompliance(id), [id])

  if (invoice.loading) return <InvoiceSkeleton />
  if (invoice.error) {
    if (invoice.error instanceof HttpError && invoice.error.status === 404)
      return (
        <EmptyState title={`No such invoice: ${id}`} hint="It may belong to a different run, or never existed." />
      )
    return <ErrorState error={invoice.error} onRetry={invoice.reload} />
  }
  const inv = invoice.data!
  const header = inv.invoice

  return (
    <div className="space-y-10">
      <Link to="/worklist" className="text-sm text-brand hover:text-brand-dim">
        ← back to worklist
      </Link>

      {/* 01 — Header */}
      <div>
        <div className="flex flex-wrap items-baseline gap-3">
          <span className="font-medium tabular-nums text-slate-600">01 —</span>
          <h1 className="text-2xl font-bold text-ink-deep">
            <CopyableId value={header.invoice_id} className="text-2xl font-bold" />
          </h1>
          <span className="tnum text-2xl font-black text-ink-deep">{rupees(header.amount_paise)}</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-slate-600">
          <Tag>{header.method}</Tag>
          <Tag>{header.bank}</Tag>
          <Tag className="capitalize">{header.mcc_category}</Tag>
          <Tag className="capitalize">subscription · {header.subscription_status}</Tag>
          <Tag>status · {header.status}</Tag>
          <Tag>customer · <span className="font-mono">{header.customer_hash}</span></Tag>
        </div>
      </div>

      {/* 02 — Compliance panel (primary content) */}
      <div>
        <SectionHeader index="02" title="Compliance decision" caption="Rendered verbatim from the guardrail — nothing recomputed client-side." />
        {compliance.loading && <PanelSkeleton rows={4} />}
        {compliance.error != null && <ErrorState error={compliance.error} onRetry={compliance.reload} />}
        {compliance.data && <CompliancePanel data={compliance.data} />}
      </div>

      {/* 03 — Attempt timeline */}
      <Panel className="p-6">
        <SectionHeader index="03" title="Attempt timeline" />
        <Timeline attempts={inv.attempts} />
      </Panel>

      {/* Underwriting context — the fields the guardrail actually reasoned over */}
      <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Fact label="Consent" value={header.consent_status} />
        <Fact label="Paid / remaining" value={`${header.paid_count} / ${header.remaining_count}`} />
        <Fact label="Notice sent" value={header.notice_sent_at ? new Date(header.notice_sent_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : 'never'} />
        <Fact label="Charge at (IST)" value={header.charge_at_ist} />
      </div>

      {/* 04 — Decision cards */}
      <div>
        <SectionHeader index="04" title="Decisions" caption="Every scored candidate action × slot — winner highlighted, losers with their refusal reason." />
        <div className="space-y-4">
          {inv.decisions.map((d, i) => (
            <DecisionCard key={i} d={d} />
          ))}
          {inv.decisions.length === 0 && <EmptyState title="No decisions recorded for this invoice." />}
        </div>
      </div>

      {/* 05 — Audit trail */}
      <Panel className="p-6">
        <SectionHeader index="05" title="Audit trail" caption="Append-only. Real Razorpay entity IDs appear as monospace chips when a live action ran." />
        <AuditTrail rows={inv.audit_trail} />
      </Panel>
    </div>
  )
}

// Header-shaped + two panel-shaped blocks, matching what's about to render.
function InvoiceSkeleton() {
  return (
    <div className="space-y-10">
      <Skeleton className="h-4 w-32" />
      <div>
        <Skeleton className="h-8 w-64" />
        <Skeleton className="mt-3 h-4 w-80" />
      </div>
      <PanelSkeleton rows={4} />
      <PanelSkeleton rows={3} />
    </div>
  )
}

function Tag({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <span className={`rounded-md border border-slate-300 bg-surface px-2 py-0.5 ${className}`}>{children}</span>
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-300 p-3">
      <div className="text-xs uppercase tracking-wide text-slate-600">{label}</div>
      <div className="mt-1 truncate text-sm font-medium text-ink">{value}</div>
    </div>
  )
}

function Timeline({ attempts }: { attempts: Attempt[] }) {
  if (attempts.length === 0) return <EmptyState title="No attempts on record." />
  return (
    <ol className="space-y-3">
      {attempts.map((a) => (
        <li
          key={a.attempt_number}
          className={`flex flex-wrap items-center gap-3 rounded-lg border p-3 ${a.observed ? 'border-slate-300 bg-surface' : 'border-dashed border-slate-300 bg-slate-100/60'}`}
        >
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-100 text-sm font-bold tabular-nums text-ink">
            {a.attempt_number}
          </span>
          <span className="font-mono text-xs text-slate-600">{a.attempted_at_ist}</span>
          <span className="text-sm text-slate-900">{a.action}</span>
          <span
            className="rounded px-1.5 py-0.5 text-xs font-medium"
            style={
              a.outcome === 'blocked'
                ? { color: 'var(--critical)', background: 'rgba(240,38,60,0.1)' }
                : { color: 'var(--slate-900)', background: 'var(--slate-100)' }
            }
          >
            {a.outcome}
          </span>
          {a.error_code && (
            <span className="font-mono text-xs text-slate-600">
              {a.error_code} · {a.error_reason}
            </span>
          )}
          <RootCauseChip root={a.root_cause_class} />
          {!a.observed && (
            <span className="ml-auto rounded-md border border-slate-300 px-1.5 py-0.5 text-[11px] italic text-slate-600">
              counterfactual (oracle replay)
            </span>
          )}
        </li>
      ))}
    </ol>
  )
}

function DecisionCard({ d }: { d: Decision }) {
  // The winning candidate is the one whose `kind` matches the decision's
  // `final_action` and that wasn't ruled out before scoring.
  const winner = d.candidate_set.find((c) => c.kind === d.final_action && !c.ruled_out)
  return (
    <Panel className="p-5">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <span className="text-sm font-bold text-ink-deep">final action · {d.final_action}</span>
        <span className="text-xs text-slate-600">arm {d.arm}</span>
        <span className="tnum ml-auto text-sm text-slate-600">
          expected value <span className="font-semibold text-ink">{rupees(d.expected_value_paise)}</span>
        </span>
      </div>
      <p className="mb-4 break-words rounded-md bg-slate-100 p-2 font-mono text-xs leading-relaxed text-ink">
        {d.authorizing_rule}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-slate-600">
              <th className="py-2 pr-4 font-medium">Kind</th>
              <th className="py-2 pr-4 font-medium">Slot</th>
              <th className="py-2 pr-4 text-right font-medium">p(success)</th>
              <th className="py-2 pr-4 text-right font-medium">Expected ₹</th>
              <th className="py-2 font-medium">Verdict</th>
            </tr>
          </thead>
          <tbody>
            {d.candidate_set.map((c: Candidate, i) => {
              const isWinner = c === winner
              return (
                <tr
                  key={i}
                  className="border-t border-slate-300"
                  style={isWinner ? { background: 'var(--brand-wash)' } : undefined}
                >
                  <td className="py-2 pr-4 font-mono text-xs text-ink">
                    {isWinner && <span className="mr-1 text-brand">▸</span>}
                    {c.kind}
                  </td>
                  <td className="py-2 pr-4 font-mono text-xs text-slate-600">
                    {new Date(c.execute_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })}
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums">{c.p_success != null ? c.p_success.toFixed(2) : '—'}</td>
                  <td className="py-2 pr-4 text-right tabular-nums font-semibold text-ink">
                    {c.expected_value_paise != null ? rupees(c.expected_value_paise) : '—'}
                  </td>
                  <td className="py-2">
                    <div className="flex items-center gap-2">
                      <VerdictChip verdict={c.verdict} />
                      {c.stop_reason && <span className="text-xs text-slate-600">{c.stop_reason}</span>}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

function AuditTrail({ rows }: { rows: AuditRow[] }) {
  if (rows.length === 0) return <EmptyState title="No audit rows." />
  return (
    <ul className="space-y-2">
      {rows.map((r) => (
        <li key={r.event_id} className="flex flex-wrap items-center gap-3 border-b border-slate-300 py-2 last:border-0 text-sm">
          <span className="font-mono text-xs text-slate-600">{r.ts_ist}</span>
          <span className="text-slate-900">{r.action_taken}</span>
          {r.outcome === 'blocked' ? <StatusChip outcome="blocked" /> : <span className="text-xs text-slate-600">{r.outcome}</span>}
          <span
            className="rounded px-1.5 py-0.5 text-[11px] font-medium"
            style={
              r.execution_mode === 'live'
                ? { color: 'var(--good)', background: 'rgba(72,208,140,0.12)' }
                : { color: 'var(--slate-600)', background: 'var(--slate-100)' }
            }
          >
            {r.execution_mode}
          </span>
          {r.razorpay_entity_id && (
            <span className="rounded border border-slate-300 px-1.5 py-0.5 text-brand-dim">
              <CopyableId value={r.razorpay_entity_id} className="text-[11px] text-brand-dim" />
            </span>
          )}
          {r.stop_reason && (
            <span className="ml-auto font-mono text-[11px]" style={{ color: 'var(--critical)' }}>
              {r.stop_reason}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

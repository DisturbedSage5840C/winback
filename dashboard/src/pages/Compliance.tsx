import { useEffect, useState } from 'react'
import { getComplianceWindow, getInvoiceCompliance, getLiveWorklist, HttpError } from '../lib/api'
import { useAsync } from '../lib/hooks'
import { EmptyState, ErrorState, Panel, SectionHeader, Skeleton } from '../components/ui'
import { WindowStrip } from '../components/WindowStrip'
import { CompliancePanel } from '../components/CompliancePanel'
import type { CompliancePanelData } from '../lib/types'

// Static rule reference — hand-written copy sourced from docs/COMPLIANCE.md,
// clearly separated from the live per-invoice data above (§03.4).
const RULES = [
  { name: 'NPCI 1 + 3 retry cap', desc: 'One initial presentment plus three retries — four attempts maximum per mandate. The fifth is refused even when the model recommends retrying.' },
  { name: 'AFA ceiling', desc: 'Auto-approve ≤ ₹15,000; up to ₹1,00,000 for insurance / SIP / credit-card MCCs; anything above escalates to a human.' },
  { name: 'Consent gate', desc: 'Requires active consent and no 90-day DND cooloff. Transactional consent window is 7 days. Withdrawn customers are blocked from every nudge.' },
  { name: 'Pre-debit notice', desc: 'Notice must be sent at least 24h before charge. Blocks a new debit; warns but does not block a within-cycle retry.' },
]

export function CompliancePage() {
  const window = useAsync(getComplianceWindow, [])
  // The example id in the placeholder is a real invoice off the live queue, not
  // a made-up one — typing the hint should actually resolve.
  const sample = useAsync(() => getLiveWorklist(1, 0), [])
  const sampleId = sample.data?.items[0]?.invoice_id
  const [query, setQuery] = useState('')
  // The moment the rules are asked about. Empty means now, which is the honest default
  // for a live panel — but the dataset is frozen, so every time-based window in it has
  // long since expired against the wall clock. Being able to name the moment is what
  // separates "this invoice was never contactable" from "this invoice stopped being
  // contactable in July", and only one of those is a fact about the policy.
  const [asOf, setAsOf] = useState('')
  const [lookup, setLookup] = useState<CompliancePanelData | null>(null)
  const [lookupErr, setLookupErr] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  // refetch the window every 30s (§03.4)
  useEffect(() => {
    const id = setInterval(window.reload, 30_000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function evaluate(id: string, at: string) {
    if (!id) return
    setBusy(true)
    setLookupErr(null)
    try {
      setLookup(await getInvoiceCompliance(id, at || undefined))
    } catch (err) {
      setLookup(null)
      setLookupErr(err)
    } finally {
      setBusy(false)
    }
  }

  function submit(e: React.FormEvent) {
    e.preventDefault()
    void evaluate(query.trim(), asOf)
  }

  // `datetime-local` wants `YYYY-MM-DDTHH:MM`; the API hands back a UTC ISO string with
  // an offset. Slicing is the conversion, because both are already UTC — parsing this
  // through `Date` would silently re-render it in the viewer's timezone and the panel
  // would then be judging a different instant than the one on the label.
  const chargeMoment = lookup?.pre_debit_notice.charge_at?.slice(0, 16) ?? null

  return (
    <div className="space-y-10">
      {/* 01 — Live window strip */}
      <Panel className="p-6">
        <SectionHeader index="01" title="Live non-peak window" caption="Ticks against the guardrail’s own arithmetic — re-fetched every 30s, never a drifting client clock." />
        {window.loading && <WindowSkeleton />}
        {window.error != null && <ErrorState error={window.error} onRetry={window.reload} />}
        {window.data && <WindowStrip window={window.data} />}
      </Panel>

      {/* 02 — Invoice lookup */}
      <div>
        <SectionHeader
          index="02"
          title="Per-invoice guardrail lookup"
          caption="The rules are pure functions of the invoice’s facts and a moment. Leave the moment blank to ask about now, or name one to ask what the guardrail said while the invoice was live."
        />
        <form onSubmit={submit} className="flex flex-wrap gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={sampleId ? `invoice id, e.g. ${sampleId}` : 'invoice id'}
            className="min-w-[14rem] flex-1 rounded-lg border border-slate-300 bg-surface px-3 py-2 font-mono text-sm text-ink outline-none focus:border-brand focus:ring-2 focus:ring-brand-wash"
          />
          <input
            type="datetime-local"
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
            aria-label="Evaluate as of (UTC) — blank means now"
            title="Evaluate as of (UTC). Blank means now."
            className="rounded-lg border border-slate-300 bg-surface px-3 py-2 font-mono text-sm text-ink outline-none focus:border-brand focus:ring-2 focus:ring-brand-wash"
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand-dim disabled:opacity-50"
          >
            {busy ? 'Looking up…' : 'Evaluate'}
          </button>
        </form>
        <div className="mt-4">
          {lookupErr != null &&
            (lookupErr instanceof HttpError && lookupErr.status === 404 ? (
              <EmptyState title={`No such invoice: ${query}`} />
            ) : (
              <ErrorState error={lookupErr} />
            ))}
          {lookup && (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-3 text-xs text-slate-600">
                <span>
                  evaluated as of{' '}
                  <span className="font-mono text-ink">{lookup.evaluated_at.slice(0, 19)}Z</span>
                </span>
                {chargeMoment && chargeMoment !== asOf && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      setAsOf(chargeMoment)
                      void evaluate(lookup.invoice_id, chargeMoment)
                    }}
                    className="font-medium text-brand hover:text-brand-dim disabled:opacity-50"
                  >
                    ask at the charge moment ({chargeMoment.replace('T', ' ')}Z)
                  </button>
                )}
                {asOf && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      setAsOf('')
                      void evaluate(lookup.invoice_id, '')
                    }}
                    className="font-medium text-brand hover:text-brand-dim disabled:opacity-50"
                  >
                    back to now
                  </button>
                )}
              </div>
              <CompliancePanel data={lookup} />
            </>
          )}
        </div>
      </div>

      {/* 03 — Static rule reference */}
      <Panel className="p-6">
        <SectionHeader index="03" title="What the rules are" caption="Reference copy from docs/COMPLIANCE.md — not invoice-specific, kept distinct from the live data above." />
        <div className="grid gap-4 sm:grid-cols-2">
          {RULES.map((r) => (
            <div key={r.name} className="rounded-lg border border-slate-300 p-4">
              <div className="text-sm font-bold text-ink-deep">{r.name}</div>
              <p className="mt-1 text-sm leading-snug text-slate-600">{r.desc}</p>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  )
}

// Shaped like WindowStrip's own pill + 24h band + slots row.
function WindowSkeleton() {
  return (
    <div>
      <div className="flex items-center gap-3">
        <Skeleton className="h-8 w-56 rounded-lg" />
        <Skeleton className="ml-auto h-4 w-32" />
      </div>
      <Skeleton className="mt-3 h-7 w-full rounded-md" />
      <div className="mt-3 flex gap-2">
        <Skeleton className="h-5 w-16 rounded-md" />
        <Skeleton className="h-5 w-16 rounded-md" />
        <Skeleton className="h-5 w-16 rounded-md" />
      </div>
    </div>
  )
}

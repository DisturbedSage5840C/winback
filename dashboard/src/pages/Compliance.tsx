import { useEffect, useState } from 'react'
import { getComplianceWindow, getInvoiceCompliance, HttpError } from '../lib/api'
import { useAsync } from '../lib/hooks'
import { EmptyState, ErrorState, Panel, SectionHeader, Spinner } from '../components/ui'
import { WindowStrip } from '../components/WindowStrip'
import { CompliancePanel } from '../components/CompliancePanel'
import { STAR_INVOICE } from '../data/demo'
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
  const [query, setQuery] = useState('')
  const [lookup, setLookup] = useState<CompliancePanelData | null>(null)
  const [lookupErr, setLookupErr] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  // refetch the window every 30s (§03.4)
  useEffect(() => {
    const id = setInterval(window.reload, 30_000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    const id = query.trim()
    if (!id) return
    setBusy(true)
    setLookupErr(null)
    try {
      setLookup(await getInvoiceCompliance(id))
    } catch (err) {
      setLookup(null)
      setLookupErr(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-10">
      {/* 01 — Live window strip */}
      <Panel className="p-6">
        <SectionHeader index="01" title="Live non-peak window" caption="Ticks against the guardrail’s own arithmetic — re-fetched every 30s, never a drifting client clock." />
        {window.loading && <Spinner />}
        {window.error != null && <ErrorState error={window.error} onRetry={window.reload} />}
        {window.data && <WindowStrip window={window.data} />}
      </Panel>

      {/* 02 — Invoice lookup */}
      <div>
        <SectionHeader index="02" title="Per-invoice guardrail lookup" />
        <form onSubmit={submit} className="flex gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`invoice id, e.g. ${STAR_INVOICE}`}
            className="flex-1 rounded-lg border border-slate-300 bg-surface px-3 py-2 font-mono text-sm text-ink outline-none focus:border-brand focus:ring-2 focus:ring-brand-wash"
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
          {lookup && <CompliancePanel data={lookup} />}
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

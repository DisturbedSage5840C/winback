import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { getInvoiceCompliance, getLiveWorklist, getRunWorklist, getRuns } from '../lib/api'
import { count } from '../lib/format'
import { useAsync, usePrefersReducedMotion } from '../lib/hooks'
import { EmptyState, ErrorState, Panel, SectionHeader, Spinner } from '../components/ui'
import { RunSelector } from '../components/RunSelector'
import { WorklistTable } from '../components/WorklistTable'
import { CompliancePanel } from '../components/CompliancePanel'
import type { WorklistRow } from '../lib/types'

const PAGE = 50
const OUTCOMES = ['', 'recovered', 'blocked', 'escalated', 'written_off', 'at_risk']

export function WorklistPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const runs = useAsync(getRuns, [])

  const runId = params.get('run_id')
  const outcome = params.get('outcome') ?? ''
  const offset = Number(params.get('offset') ?? 0)
  const runMode = Boolean(runId)

  const [quicklook, setQuicklook] = useState<WorklistRow | null>(null)

  const page = useAsync(
    () =>
      runMode
        ? getRunWorklist(runId!, PAGE, offset, outcome || undefined)
        : getLiveWorklist(PAGE, offset),
    [runId, outcome, offset],
  )

  const setParam = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === '') next.delete(k)
      else next.set(k, v)
    }
    setParams(next)
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <SectionHeader
          index="02"
          title={runMode ? 'Run worklist' : 'Exception worklist — live queue'}
          caption={
            runMode
              ? 'What one batch decided about every invoice it touched.'
              : 'Invoices still at risk, nothing decided yet. This shrinks as a batch runs.'
          }
        />
      </div>

      {runs.data && (
        <RunSelector
          runs={runs.data}
          selected={runId}
          allowLive
          live={!runMode}
          onSelect={(id) => setParam({ run_id: id, offset: null, outcome: null })}
        />
      )}

      {runMode && (
        <div className="flex items-center gap-2">
          <span className="text-xs text-slate-600">Filter outcome</span>
          {OUTCOMES.map((o) => (
            <button
              key={o || 'all'}
              onClick={() => setParam({ outcome: o || null, offset: null })}
              className={`rounded-full border px-2.5 py-1 text-xs capitalize transition-colors ${
                outcome === o ? 'border-brand bg-brand-wash text-brand-dim' : 'border-slate-300 text-slate-900 hover:bg-slate-100'
              }`}
            >
              {o ? o.replace('_', ' ') : 'all'}
            </button>
          ))}
        </div>
      )}

      {page.loading && <Spinner label="Loading worklist" />}
      {page.error != null && <ErrorState error={page.error} onRetry={page.reload} />}

      {page.data && (
        <>
          {page.data.items.length === 0 ? (
            <EmptyState
              title={runMode ? 'No invoices match this filter.' : 'Queue is empty.'}
              hint={runMode ? undefined : 'Nothing outstanding — a good state, not an error.'}
            />
          ) : (
            <>
              <WorklistTable rows={page.data.items} runMode={runMode} onRowClick={setQuicklook} />
              <Pagination
                total={page.data.total}
                offset={offset}
                onPrev={() => setParam({ offset: String(Math.max(0, offset - PAGE)) })}
                onNext={() => setParam({ offset: String(offset + PAGE) })}
              />
            </>
          )}
        </>
      )}

      {/* §06.4 quick-look drawer */}
      <QuickLookDrawer
        row={quicklook}
        onClose={() => setQuicklook(null)}
        onOpenFull={(id) => {
          setQuicklook(null)
          navigate(`/invoices/${id}${runId ? `?run_id=${runId}` : ''}`)
        }}
      />
    </div>
  )
}

function Pagination({
  total,
  offset,
  onPrev,
  onNext,
}: {
  total: number
  offset: number
  onPrev: () => void
  onNext: () => void
}) {
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(total, offset + PAGE)
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="tabular-nums text-slate-600">
        {from}–{to} of {count(total)}
      </span>
      <div className="flex gap-2">
        <button
          disabled={offset === 0}
          onClick={onPrev}
          className="rounded-lg border border-slate-300 px-3 py-1.5 font-medium text-ink disabled:opacity-40 enabled:hover:bg-brand-wash"
        >
          Prev
        </button>
        <button
          disabled={to >= total}
          onClick={onNext}
          className="rounded-lg border border-slate-300 px-3 py-1.5 font-medium text-ink disabled:opacity-40 enabled:hover:bg-brand-wash"
        >
          Next
        </button>
      </div>
    </div>
  )
}

function QuickLookDrawer({
  row,
  onClose,
  onOpenFull,
}: {
  row: WorklistRow | null
  onClose: () => void
  onOpenFull: (id: string) => void
}) {
  const reduced = usePrefersReducedMotion()
  const compliance = useAsync(
    () => (row ? getInvoiceCompliance(row.invoice_id) : Promise.reject(new Error('no row'))),
    [row?.invoice_id],
  )

  return (
    <AnimatePresence>
      {row && (
        <>
          <motion.div
            className="fixed inset-0 z-40 bg-ink-deep/30"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
          />
          <motion.aside
            className="fixed inset-y-0 right-0 z-50 w-full max-w-2xl overflow-y-auto border-l border-slate-300 bg-surface-dim p-6 shadow-2xl"
            initial={reduced ? false : { x: '100%' }}
            animate={{ x: 0 }}
            exit={reduced ? { opacity: 0 } : { x: '100%' }}
            transition={{ duration: reduced ? 0 : 0.3, ease: 'easeOut' }}
          >
            <div className="mb-4 flex items-center justify-between">
              <div>
                <div className="font-mono text-sm text-ink-deep">{row.invoice_id}</div>
                <div className="text-xs text-slate-600">quick look</div>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => onOpenFull(row.invoice_id)}
                  className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-dim"
                >
                  Open full drill-down
                </button>
                <button onClick={onClose} className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-ink hover:bg-slate-100">
                  Close
                </button>
              </div>
            </div>
            {compliance.loading && <Spinner />}
            {compliance.error != null && <ErrorState error={compliance.error} />}
            {compliance.data && <CompliancePanel data={compliance.data} />}
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  )
}

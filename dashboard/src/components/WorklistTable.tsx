import type { WorklistRow } from '../lib/types'
import { rupees } from '../lib/format'
import { AttemptsBar, CopyableId, RootCauseChip, StatusChip } from './ui'

// §03.2 — columns in fixed order, sourced from exception_worklist. Table is
// pre-sorted by the API; never re-sort client-side. `runMode` adds the
// decision columns.
export function WorklistTable({
  rows,
  runMode,
  onRowClick,
}: {
  rows: WorklistRow[]
  runMode: boolean
  onRowClick: (r: WorklistRow) => void
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-slate-300 bg-surface">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-slate-300 text-left text-xs uppercase tracking-wide text-slate-600">
            <th className="px-4 py-3 font-medium">Invoice</th>
            <th className="px-4 py-3 text-right font-medium">Amount</th>
            <th className="px-4 py-3 font-medium">Method</th>
            <th className="px-4 py-3 font-medium">Bank</th>
            <th className="px-4 py-3 font-medium">MCC</th>
            <th className="px-4 py-3 font-medium">Attempts</th>
            <th className="px-4 py-3 font-medium">Root cause</th>
            {runMode && (
              <>
                <th className="px-4 py-3 font-medium">Action</th>
                <th className="px-4 py-3 font-medium">Outcome</th>
                <th className="px-4 py-3 text-center font-medium">Viol.</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.invoice_id}
              onClick={() => onRowClick(r)}
              className="cursor-pointer border-b border-slate-300 last:border-0 transition-colors hover:bg-brand-wash/60"
            >
              <td className="px-4 py-3 text-xs">
                <CopyableId value={r.invoice_id} className="text-xs" />
              </td>
              <td className="px-4 py-3 text-right tabular-nums font-semibold text-ink">{rupees(r.amount_paise)}</td>
              <td className="px-4 py-3 text-slate-900">{r.method}</td>
              <td className="px-4 py-3 text-slate-900">{r.bank}</td>
              <td className="px-4 py-3 capitalize text-slate-900">{r.mcc_category}</td>
              <td className="px-4 py-3">
                <AttemptsBar used={r.attempts_used} remaining={r.attempts_remaining} />
              </td>
              <td className="px-4 py-3">
                <RootCauseChip root={r.latest_root_cause} />
              </td>
              {runMode && (
                <>
                  <td className="px-4 py-3 font-mono text-xs text-slate-900">{r.action_taken ?? '—'}</td>
                  <td className="px-4 py-3">{r.outcome ? <StatusChip outcome={r.outcome} /> : '—'}</td>
                  {/* red dot if true, silence otherwise — never a green check */}
                  <td className="px-4 py-3 text-center">
                    {r.compliance_violation ? (
                      <span
                        className="inline-block h-2.5 w-2.5 rounded-full"
                        style={{ background: 'var(--critical)' }}
                        title="compliance violation"
                      />
                    ) : null}
                  </td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

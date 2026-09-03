import type { RunSummary } from '../lib/types'
import { count } from '../lib/format'

// Row of pills, one per run_id (§03.1). Dot = touched_live (blue) vs simulated
// (slate). Clicking switches the whole page.
export function RunSelector({
  runs,
  selected,
  onSelect,
  allowLive = false,
  live,
}: {
  runs: RunSummary[]
  selected: string | null
  onSelect: (id: string | null) => void
  allowLive?: boolean
  live?: boolean
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {allowLive && (
        <button
          onClick={() => onSelect(null)}
          className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm transition-colors ${
            live ? 'border-brand bg-brand-wash text-brand-dim' : 'border-slate-300 text-ink hover:bg-slate-100'
          }`}
        >
          <span className="h-2 w-2 animate-pulse rounded-full bg-brand" />
          Live queue
        </button>
      )}
      {runs.map((r) => {
        const active = r.run_id === selected
        return (
          <button
            key={r.run_id}
            onClick={() => onSelect(r.run_id)}
            className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm transition-colors ${
              active ? 'border-brand bg-brand-wash text-brand-dim' : 'border-slate-300 text-ink hover:bg-slate-100'
            }`}
            title={r.run_id}
          >
            <span
              className="h-2 w-2 rounded-full"
              style={{ background: r.touched_live ? 'var(--brand)' : 'var(--slate-300)' }}
            />
            <span className="font-semibold">Arm {r.arm}</span>
            <span className="tabular-nums text-slate-600">{count(r.invoices)}</span>
          </button>
        )
      })}
    </div>
  )
}

import { useSearchParams } from 'react-router-dom'
import { getConfig, getOverview, getRuns } from '../lib/api'
import { count, pct, rupees } from '../lib/format'
import { useAsync, useCountUp, usePrefersReducedMotion } from '../lib/hooks'
import { EmptyState, ErrorState, Panel, SectionHeader, Spinner } from '../components/ui'
import { RunSelector } from '../components/RunSelector'
import { Funnel } from '../components/Funnel'
import { LiveTrace } from '../components/LiveTrace'

export function OverviewPage() {
  const [params, setParams] = useSearchParams()
  const runs = useAsync(getRuns, [])
  const config = useAsync(getConfig, [])

  const selected = params.get('run_id') ?? runs.data?.[0]?.run_id ?? null
  const selectedRun = runs.data?.find((r) => r.run_id === selected)

  // `selected` is briefly null while `runs` is still loading (nothing to default to
  // yet) — hooks run every render regardless, so without this guard the very first
  // render fires `GET /runs/null/overview`, a wasted 404 before the real run_id lands.
  const overview = useAsync(() => (selected ? getOverview(selected) : Promise.resolve(null)), [selected])

  if (runs.loading) return <Spinner label="Loading runs" />
  if (runs.error) return <ErrorState error={runs.error} onRetry={runs.reload} />
  if (!runs.data || runs.data.length === 0)
    return <EmptyState title="No runs yet." hint="Run a batch (agent.orchestrator) and it appears here — no example rows." />

  return (
    <div className="space-y-10">
      <RunSelector runs={runs.data} selected={selected} onSelect={(id) => setParams(id ? { run_id: id } : {})} />

      {overview.loading && <Spinner label="Loading overview" />}
      {overview.error != null && <ErrorState error={overview.error} onRetry={overview.reload} />}

      {overview.data && (
        <>
          {/* 02 — Headline */}
          <Headline
            recoveredPaise={overview.data.recovered_paise}
            recovered={overview.data.recovered}
            atRisk={overview.data.at_risk}
            arm={overview.data.arm}
            runKey={overview.data.run_id}
          />

          {/* 03 — Funnel */}
          <Panel className="p-6">
            <SectionHeader index="03" title="Recovery funnel" caption="Each stage is a real count from the run — nothing interpolated." />
            <Funnel o={overview.data} />
          </Panel>

          <div className="grid gap-6 lg:grid-cols-2">
            {/* 04 — Stop reasons */}
            <Panel className="p-6">
              <SectionHeader
                index="04"
                title="Why not just retry everything"
                caption="Stop reasons, descending — this is the answer to “why didn’t you brute-force it.”"
              />
              <StopReasons rows={overview.data.stop_reasons} />
            </Panel>

            {/* 05 — Compliance strip */}
            <Panel className="p-6">
              <SectionHeader index="05" title="Compliance strip" />
              <div className="grid grid-cols-3 gap-3">
                <StatTile label="Violations" value={count(overview.data.violations)} good={overview.data.violations === 0} bad={overview.data.violations > 0} />
                <StatTile label="Escalated" value={count(overview.data.escalated)} accent="var(--warn)" />
                <StatTile label="Stopped" value={count(overview.data.stopped)} accent="var(--slate-600)" />
              </div>
            </Panel>
          </div>

          {/* Live trace for the run (§06.3 / §07) */}
          {selectedRun?.touched_live && (
            <Panel className="p-6" elevated>
              <SectionHeader index="06" title="Live agent trace" caption="Decisions stream in; the authorizing rule flashes red when a guardrail blocks." />
              <LiveTrace runId={overview.data.run_id} />
            </Panel>
          )}

          {/* Config footer */}
          {config.data && (
            <p className="text-center text-xs text-slate-600">
              execution mode <span className="font-mono text-ink">{config.data.execution_mode}</span> · model{' '}
              <span className="font-mono text-ink">{config.data.model_version}</span>
            </p>
          )}
        </>
      )}
    </div>
  )
}

function Headline({
  recoveredPaise,
  recovered,
  atRisk,
  arm,
  runKey,
}: {
  recoveredPaise: number
  recovered: number
  atRisk: number
  arm: string
  runKey: string
}) {
  const reduced = usePrefersReducedMotion()
  const animated = useCountUp(recoveredPaise, runKey, reduced)
  return (
    <div>
      <div className="flex items-baseline gap-3">
        <span className="font-medium tabular-nums text-slate-600">02 —</span>
        <span className="text-sm font-medium uppercase tracking-wide text-slate-600">Money recovered</span>
      </div>
      <div className="tnum mt-1 text-6xl font-black leading-none text-ink-deep">{rupees(animated)}</div>
      <p className="mt-2 text-slate-600">
        <span className="tabular-nums font-semibold text-ink">{count(recovered)}</span> invoices recovered from a batch of{' '}
        <span className="tabular-nums font-semibold text-ink">{count(atRisk)}</span> ·{' '}
        <span className="tabular-nums">{pct(atRisk ? recovered / atRisk : 0)}</span> · arm{' '}
        <span className="font-semibold text-ink">{arm}</span>
      </p>
    </div>
  )
}

function StopReasons({ rows }: { rows: { stop_reason: string; invoices: number }[] }) {
  if (rows.length === 0) return <EmptyState title="No stops — every invoice ran to a decision." />
  const max = Math.max(...rows.map((r) => r.invoices))
  return (
    <div className="space-y-2">
      {rows.map((r) => (
        <div key={r.stop_reason} className="flex items-center gap-3">
          <div className="w-64 shrink-0 truncate font-mono text-xs text-ink" title={r.stop_reason}>
            {r.stop_reason}
          </div>
          <div className="h-4 flex-1 overflow-hidden rounded bg-slate-100">
            <div className="h-full rounded bg-slate-600" style={{ width: `${(r.invoices / max) * 100}%` }} />
          </div>
          <div className="w-10 shrink-0 text-right tabular-nums text-sm text-ink">{r.invoices}</div>
        </div>
      ))}
    </div>
  )
}

function StatTile({
  label,
  value,
  good,
  bad,
  accent,
}: {
  label: string
  value: string
  good?: boolean
  bad?: boolean
  accent?: string
}) {
  const color = good ? 'var(--good)' : bad ? 'var(--critical)' : accent ?? 'var(--ink-deep)'
  return (
    <div className="rounded-lg border border-slate-300 p-4">
      <div className="text-xs uppercase tracking-wide text-slate-600">{label}</div>
      <div className="tnum mt-1 text-3xl font-bold" style={{ color }}>
        {value}
      </div>
    </div>
  )
}

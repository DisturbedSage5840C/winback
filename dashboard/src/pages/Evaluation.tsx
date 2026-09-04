import { useMemo } from 'react'
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { getEvaluation } from '../lib/api'
import { count, rupees, rupeesSigned, signedCount } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { ErrorState, Panel, PanelSkeleton, SectionHeader, Skeleton } from '../components/ui'
import type { ArmResult, IntervalRow, ViolationRow } from '../lib/types'

// Status-palette hues for stacked stop_reasons stay in the reserved family;
// stop_reason series get distinct neutral/critical tones with a legend.
const STOP_COLORS: Record<string, string> = {
  peak_window_presentment: '#f0263c',
  pre_debit_notice_missing: '#f5a623',
}

export function EvaluationPage() {
  const evaluation = useAsync(() => getEvaluation(), [])

  if (evaluation.loading) return <EvaluationSkeleton />
  if (evaluation.error) return <ErrorState error={evaluation.error} onRetry={evaluation.reload} />
  const e = evaluation.data!

  return (
    <div className="space-y-10">
      <SectionHeader
        index="01"
        title="Four-arm paired evaluation"
        caption={`Run ${e.run.run_id} · same coin flips across arms (paired bootstrap). Headline metric is ₹ per legal attempt, not raw ₹.`}
      />

      {/* CI headline */}
      <div className="grid gap-4 md:grid-cols-2">
        {e.intervals.map((iv) => (
          <IntervalCard key={`${iv.comparison}-${iv.statistic}-${iv.arm}`} iv={iv} />
        ))}
      </div>

      {/* 02 — Four-arm table */}
      <Panel className="p-6">
        <SectionHeader index="02" title="Per-arm results" />
        <ArmTable arms={e.arms} />
      </Panel>

      {/* 03 — Violations by arm */}
      <Panel className="p-6">
        <SectionHeader index="03" title="Violations by arm" caption="Arm B’s violations concentrate in one rule — peak-window presentments — not spread thin." />
        <ViolationsChart violations={e.violations} />
      </Panel>

      {/* 04 — Calibration */}
      <Panel className="p-6">
        <SectionHeader index="04" title="Calibration & realism" caption="The committed report’s own charts (docs/assets/*.png) — the identical evidence in docs/EVALUATION.md, not reconstructed." />
        <div className="grid gap-4 sm:grid-cols-3">
          {['calibration.png', 'realism.png', 'four_arms.png'].map((f) => (
            <figure key={f} className="rounded-lg border border-slate-300 bg-slate-100 p-4">
              <img
                src={`/docs/assets/${f}`}
                alt={`${f} — committed report chart from docs/EVALUATION.md`}
                className="aspect-[4/3] w-full rounded border border-slate-300 bg-white object-contain"
              />
              <figcaption className="mt-2 font-mono text-[11px] text-slate-600">{f}</figcaption>
            </figure>
          ))}
        </div>
      </Panel>

      {/* 05 — Honesty note */}
      <div className="rounded-xl border-l-4 p-5" style={{ borderColor: 'var(--warn)', background: 'rgba(245,166,35,0.08)' }}>
        <div className="text-sm font-bold text-ink-deep">05 — What this evaluation cannot claim</div>
        <p className="mt-2 text-sm leading-relaxed text-ink">
          Arm D beats B and C <em>within this simulator</em>; the simulator is a model, not the world — the
          comparison is circular to the extent the world model is wrong. We take that seriously and measure it:
          calibration error is <span className="font-mono font-semibold">ECE 0.034</span> on the observed slice
          the model had data for, but <span className="font-mono font-semibold">0.442</span> on the censored slice
          the legacy policy never sampled. Naming this gap is the point — a headline AUC would have hidden it.
        </p>
      </div>
    </div>
  )
}

// CI-card-shaped + table-shaped, matching the two heaviest panels below it.
function EvaluationSkeleton() {
  return (
    <div className="space-y-10">
      <div>
        <Skeleton className="h-4 w-32" />
        <Skeleton className="mt-2 h-6 w-96" />
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <Skeleton className="h-28 w-full rounded-xl" />
        <Skeleton className="h-28 w-full rounded-xl" />
      </div>
      <PanelSkeleton rows={5} />
      <PanelSkeleton rows={4} />
    </div>
  )
}

function IntervalCard({ iv }: { iv: IntervalRow }) {
  // The API states no unit — every `_paise` statistic is rupees at render time, per
  // §05; everything else is a plain count.
  const isPaise = iv.statistic.endsWith('_paise')
  const fmt = (v: number) => (isPaise ? rupees(v) : count(v))
  const fmtSigned = (v: number) => (isPaise ? rupeesSigned(v) : signedCount(v))
  // scale marks onto 0..100 of the [lo, hi] span (with a touch of padding)
  const span = iv.ci_high - iv.ci_low || 1
  const pos = (v: number) => ((v - iv.ci_low) / span) * 100
  const crossesZero = iv.ci_low < 0 && iv.ci_high > 0

  return (
    <div className="rounded-xl border border-slate-300 bg-surface p-5">
      <div className="text-xs uppercase tracking-wide text-slate-600">
        arm {iv.arm} · {iv.comparison.replace('_vs_', ' vs ')} · {iv.statistic}
      </div>
      <div className="tnum mt-1 text-3xl font-bold text-ink-deep">{fmtSigned(iv.point)}</div>
      <div className="mt-3">
        <div className="relative h-8">
          {/* zero reference */}
          {crossesZero && (
            <div className="absolute inset-y-0 w-px bg-slate-600/50" style={{ left: `${pos(0)}%` }} />
          )}
          {/* CI bar */}
          <div
            className="absolute top-1/2 h-1.5 -translate-y-1/2 rounded-full"
            style={{ left: `${pos(iv.ci_low)}%`, right: `${100 - pos(iv.ci_high)}%`, background: 'var(--brand-tint)' }}
          />
          {/* point estimate */}
          <div
            className="absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white"
            style={{ left: `${pos(iv.point)}%`, background: 'var(--brand)' }}
          />
        </div>
        <div className="mt-1 flex justify-between font-mono text-[11px] tabular-nums text-slate-600">
          <span>{fmt(iv.ci_low)}</span>
          <span>95% CI</span>
          <span>{fmt(iv.ci_high)}</span>
        </div>
      </div>
    </div>
  )
}

function ArmTable({ arms }: { arms: ArmResult[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-300 text-left text-xs uppercase tracking-wide text-slate-600">
            <th className="py-2 pr-4 font-medium">Arm</th>
            <th className="py-2 pr-4 text-right font-medium">Recovered</th>
            <th className="py-2 pr-4 text-right font-medium">Attempts</th>
            <th className="py-2 pr-4 text-right font-medium">Legal attempts</th>
            <th className="py-2 pr-4 text-right font-medium" style={{ color: 'var(--brand-dim)' }}>₹ / legal attempt</th>
            <th className="py-2 pr-4 text-right font-medium">Nudges</th>
            <th className="py-2 pr-4 text-right font-medium">Escalations</th>
            <th className="py-2 pr-4 text-right font-medium">Violations</th>
            <th className="py-2 text-right font-medium">Written off</th>
          </tr>
        </thead>
        <tbody>
          {arms.map((a) => (
            <tr key={a.arm} className="border-b border-slate-300 last:border-0">
              <td className="py-2.5 pr-4 font-medium text-ink">{a.arm_label}</td>
              <td className="py-2.5 pr-4 text-right tabular-nums text-ink">{rupees(a.recovered_paise)}</td>
              <td className="py-2.5 pr-4 text-right tabular-nums text-slate-900">{count(a.attempts_consumed)}</td>
              <td className="py-2.5 pr-4 text-right tabular-nums text-slate-900">{count(a.legal_attempts_consumed)}</td>
              <td className="py-2.5 pr-4 text-right tabular-nums font-bold" style={{ background: 'var(--brand-wash)', color: 'var(--brand-dim)' }}>
                {rupees(a.paise_per_legal_attempt)}
              </td>
              <td className="py-2.5 pr-4 text-right tabular-nums text-slate-900">{count(a.nudges_sent)}</td>
              <td className="py-2.5 pr-4 text-right tabular-nums text-slate-900">{count(a.escalations)}</td>
              <td
                className="py-2.5 pr-4 text-right tabular-nums font-semibold"
                style={{ color: a.compliance_violations === 0 ? 'var(--good)' : 'var(--critical)' }}
              >
                {count(a.compliance_violations)}
              </td>
              <td className="py-2.5 text-right tabular-nums text-slate-900">{count(a.written_off)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ViolationsChart({ violations }: { violations: ViolationRow[] }) {
  const { data, reasons } = useMemo(() => {
    const reasons = Array.from(new Set(violations.map((v) => v.stop_reason)))
    const byArm = new Map<string, Record<string, number | string>>()
    for (const v of violations) {
      const row = byArm.get(v.arm) ?? { arm: `Arm ${v.arm}` }
      row[v.stop_reason] = ((row[v.stop_reason] as number) ?? 0) + v.violations
      byArm.set(v.arm, row)
    }
    return { data: Array.from(byArm.values()), reasons }
  }, [violations])

  return (
    <div style={{ width: '100%', height: 300 }}>
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
          <CartesianGrid vertical={false} stroke="var(--slate-300)" />
          <XAxis dataKey="arm" tick={{ fill: 'var(--slate-600)', fontSize: 12 }} axisLine={{ stroke: 'var(--slate-300)' }} tickLine={false} />
          <YAxis tick={{ fill: 'var(--slate-600)', fontSize: 12 }} axisLine={false} tickLine={false} allowDecimals={false} />
          <Tooltip
            contentStyle={{ borderRadius: 8, border: '1px solid var(--slate-300)', fontSize: 12 }}
            cursor={{ fill: 'rgba(48,94,255,0.06)' }}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          {reasons.map((r) => (
            <Bar key={r} dataKey={r} stackId="v" fill={STOP_COLORS[r] ?? 'var(--slate-600)'} radius={[2, 2, 0, 0]} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

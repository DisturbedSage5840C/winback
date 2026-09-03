import { useEffect, useState } from 'react'
import type { ComplianceWindow } from '../lib/types'
import { mmss } from '../lib/format'

// 24h timeline with peak bands shaded red; only seconds_to_transition ticks
// locally between fetches (§03.4) — never a client-side clock that could drift.
export function WindowStrip({ window: w }: { window: ComplianceWindow }) {
  const [secs, setSecs] = useState(w.seconds_to_transition)

  useEffect(() => {
    setSecs(w.seconds_to_transition)
    const id = setInterval(() => setSecs((s) => Math.max(0, s - 1)), 1000)
    return () => clearInterval(id)
  }, [w.seconds_to_transition, w.now_ist])

  const toMin = (hhmm: string) => {
    const [h, m] = hhmm.split(':').map(Number)
    return h * 60 + m
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <span
          className="inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm font-bold"
          style={
            w.is_non_peak
              ? { color: 'var(--good)', background: 'rgba(72,208,140,0.12)' }
              : { color: 'var(--critical)', background: 'rgba(240,38,60,0.10)' }
          }
        >
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: w.is_non_peak ? 'var(--good)' : 'var(--critical)' }}
          />
          {w.is_non_peak ? 'Non-peak — presentments legal' : 'Peak window — presentments blocked'}
        </span>
        <span className="text-sm text-slate-600">{w.now_ist}</span>
        <span className="ml-auto text-sm text-slate-600">
          {w.is_non_peak ? 'peak begins in' : 'reopens in'}{' '}
          <span className="tabular-nums font-semibold text-ink">{mmss(secs)}</span>
        </span>
      </div>

      {/* 24h band */}
      <div className="relative mt-3 h-7 overflow-hidden rounded-md border border-slate-300 bg-slate-100">
        {w.peak_windows_ist.map(([start, end], i) => {
          const left = (toMin(start) / 1440) * 100
          const width = ((toMin(end) - toMin(start)) / 1440) * 100
          return (
            <div
              key={i}
              className="absolute inset-y-0 flex items-center justify-center text-[10px] font-medium"
              style={{ left: `${left}%`, width: `${width}%`, background: 'rgba(240,38,60,0.14)', color: 'var(--critical)' }}
              title={`peak ${start}–${end}`}
            >
              {start}–{end}
            </div>
          )
        })}
      </div>
      <div className="mt-1 flex justify-between text-[10px] tabular-nums text-slate-600">
        <span>00:00</span>
        <span>12:00</span>
        <span>24:00</span>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-xs text-slate-600">Next legal slots</span>
        {w.next_legal_slots_ist.map((s) => (
          <span key={s} className="rounded-md border border-slate-300 px-2 py-0.5 font-mono text-xs text-ink">
            {s}
          </span>
        ))}
      </div>
    </div>
  )
}

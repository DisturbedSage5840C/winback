import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import type { TraceEvent } from '../lib/types'
import { getEvents } from '../lib/api'
import { usePageVisible, usePrefersReducedMotion } from '../lib/hooks'
import { VerdictChip } from './ui'

// §06.3 + §07 — the twenty-second scene. Cursored polling every 1.5s; each new
// row slides/fades in at the top; the authorizing_rule chip flashes blue on
// APPROVE, red on compliance_violation or a stop_reason. Polling pauses in a
// backgrounded tab.
export function LiveTrace({ runId }: { runId: string }) {
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [caughtUp, setCaughtUp] = useState(false)
  const cursor = useRef<number | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const visible = usePageVisible()
  const reduced = usePrefersReducedMotion()

  useEffect(() => {
    // reset on run switch
    setEvents([])
    setCaughtUp(false)
    cursor.current = null
  }, [runId])

  useEffect(() => {
    let alive = true
    if (!visible) return

    async function poll() {
      try {
        const { cursor: next, events: fresh } = await getEvents(runId, cursor.current, 50)
        if (!alive) return
        if (fresh.length) {
          setEvents((prev) => [...fresh.slice().reverse(), ...prev].slice(0, 60))
          cursor.current = next
          setCaughtUp(false)
        } else {
          setCaughtUp(true)
        }
      } catch {
        /* a finished run just stops producing events; not an error */
      }
      if (alive) timer.current = setTimeout(poll, 1500)
    }
    poll()
    return () => {
      alive = false
      if (timer.current) clearTimeout(timer.current)
    }
  }, [runId, visible])

  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <span className="flex items-center gap-2 text-sm font-medium text-ink">
          <span className={`h-2 w-2 rounded-full bg-brand ${visible && !caughtUp ? 'animate-pulse' : ''}`} />
          {!visible ? 'Paused (tab hidden)' : caughtUp ? 'Caught up' : 'Streaming decisions'}
        </span>
        <span className="ml-auto tabular-nums text-xs text-slate-600">{events.length} events</span>
      </div>

      <div className="max-h-[520px] space-y-2 overflow-y-auto pr-1">
        <AnimatePresence initial={false}>
          {events.map((e) => {
            const red = e.compliance_violation || Boolean(e.stop_reason)
            return (
              <motion.div
                key={e.event_id}
                layout={!reduced}
                initial={reduced ? false : { opacity: 0, y: -8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: reduced ? 0 : 0.28, ease: 'easeOut' }}
                className="rounded-lg border bg-surface p-3"
                style={{ borderColor: red ? 'var(--critical)' : 'var(--slate-300)' }}
              >
                <div className="flex items-center gap-3">
                  <span className="font-mono text-xs text-slate-600">{e.ts_ist}</span>
                  <span className="font-mono text-xs text-ink">{e.invoice_id}</span>
                  <span className="text-xs text-slate-900">{e.action_taken}</span>
                  <span className="ml-auto">
                    <VerdictChip verdict={e.guardrail_verdict} />
                  </span>
                </div>
                <motion.p
                  key={`chip-${e.event_id}`}
                  initial={reduced ? false : { backgroundColor: red ? 'rgba(240,38,60,0.35)' : 'rgba(48,94,255,0.28)' }}
                  animate={{ backgroundColor: red ? 'rgba(240,38,60,0.08)' : 'rgba(48,94,255,0.06)' }}
                  transition={{ duration: reduced ? 0 : 1.1 }}
                  className="mt-2 break-words rounded-md p-2 font-mono text-xs leading-relaxed"
                  style={{ color: red ? 'var(--critical)' : 'var(--ink)' }}
                >
                  {e.authorizing_rule}
                  {e.stop_reason && <span className="block font-semibold">stop_reason · {e.stop_reason}</span>}
                </motion.p>
              </motion.div>
            )
          })}
        </AnimatePresence>
        {events.length === 0 && (
          <p className="py-8 text-center text-sm text-slate-600">Waiting for the first decision…</p>
        )}
      </div>
    </div>
  )
}

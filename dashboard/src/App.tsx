import { AnimatePresence, motion } from 'framer-motion'
import { NavLink, Route, HashRouter as Router, Routes, useLocation } from 'react-router-dom'
import { API_BASE, getHealth } from './lib/api'
import { useAsync, usePrefersReducedMotion } from './lib/hooks'
import { OverviewPage } from './pages/Overview'
import { WorklistPage } from './pages/Worklist'
import { InvoicePage } from './pages/Invoice'
import { CompliancePage } from './pages/Compliance'
import { EvaluationPage } from './pages/Evaluation'

const NAV = [
  { to: '/', label: '01 · Overview', end: true },
  { to: '/worklist', label: '02 · Worklist' },
  { to: '/compliance', label: '03 · Compliance' },
  { to: '/evaluation', label: '04 · Evaluation' },
]

function Shell({ children }: { children: React.ReactNode }) {
  // The badge reports whether the API actually answers, not whether a build-time
  // env var was set — there is no offline dataset to fall back to, so "is the
  // backend up" is the only state worth surfacing.
  const health = useAsync(getHealth, [])
  const up = health.data?.status === 'ok'
  const db = health.data?.database
  const location = useLocation()
  const reduced = usePrefersReducedMotion()
  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-30 border-b border-slate-300 bg-surface/85 backdrop-blur">
        {/* Wraps below ~1000px: the four nav labels plus the status badge cannot
            share one row on a phone without pushing the page into a horizontal
            scroll, and a dashboard that scrolls sideways reads as broken. */}
        <div className="mx-auto flex max-w-[1180px] flex-wrap items-center gap-x-6 gap-y-2 px-6 py-3">
          <NavLink to="/" className="flex items-baseline gap-2">
            <span className="text-lg font-black tracking-tight text-ink-deep">winback</span>
            <span className="hidden text-xs text-slate-600 sm:inline">
              recover the money you’re legally allowed to
            </span>
          </NavLink>
          <nav className="order-last -mx-6 flex w-[calc(100%+3rem)] items-center gap-1 overflow-x-auto px-6 sm:order-none sm:mx-0 sm:ml-auto sm:w-auto sm:overflow-visible sm:px-0">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.end}
                className={({ isActive }) =>
                  `rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                    isActive ? 'bg-brand-wash text-brand-dim' : 'text-slate-900 hover:bg-slate-100'
                  }`
                }
              >
                {n.label}
              </NavLink>
            ))}
          </nav>
          <span
            className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-full border border-slate-300 px-2.5 py-1 text-xs text-slate-600 sm:ml-0"
            title={
              health.loading
                ? `checking ${API_BASE}`
                : up
                  ? `live API · ${API_BASE} · connected as ${db?.role} · ${db?.append_only_triggers} append-only triggers on ${db?.tables} tables`
                  : `no answer from ${API_BASE} — start the backend (scripts/run_demo.sh)`
            }
          >
            <span
              className="h-1.5 w-1.5 rounded-full"
              style={{ background: health.loading ? 'var(--slate-600)' : up ? 'var(--good)' : 'var(--critical)' }}
            />
            {health.loading ? 'connecting' : up ? 'live API' : 'API unreachable'}
          </span>
        </div>
      </header>
      <main className="mx-auto max-w-[1180px] px-6 py-8">
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={location.pathname}
            initial={reduced ? false : { opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduced ? undefined : { opacity: 0 }}
            transition={{ duration: reduced ? 0 : 0.15 }}
          >
            {children}
          </motion.div>
        </AnimatePresence>
      </main>
      <footer className="mx-auto max-w-[1180px] px-6 pb-10 pt-4 text-xs text-slate-600">
        Winback · Razorpay AI Buildathon Track 03 · every number traces to an API response — no mocks.
      </footer>
    </div>
  )
}

export default function App() {
  return (
    <Router>
      <Shell>
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/worklist" element={<WorklistPage />} />
          <Route path="/invoices/:id" element={<InvoicePage />} />
          <Route path="/compliance" element={<CompliancePage />} />
          <Route path="/evaluation" element={<EvaluationPage />} />
        </Routes>
      </Shell>
    </Router>
  )
}

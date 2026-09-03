import { NavLink, Route, HashRouter as Router, Routes } from 'react-router-dom'
import { LIVE, API_BASE } from './lib/api'
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
  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-30 border-b border-slate-300 bg-surface/85 backdrop-blur">
        <div className="mx-auto flex max-w-[1180px] items-center gap-6 px-6 py-3">
          <NavLink to="/" className="flex items-baseline gap-2">
            <span className="text-lg font-black tracking-tight text-ink-deep">winback</span>
            <span className="hidden text-xs text-slate-600 sm:inline">
              recover the money you’re legally allowed to
            </span>
          </NavLink>
          <nav className="ml-auto flex items-center gap-1">
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
            className="inline-flex items-center gap-1.5 rounded-full border border-slate-300 px-2.5 py-1 text-xs text-slate-600"
            title={LIVE ? `live API · ${API_BASE}` : 'no VITE_API_BASE set — rendering the bundled demo dataset'}
          >
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: LIVE ? 'var(--good)' : 'var(--warn)' }} />
            {LIVE ? 'live API' : 'demo data'}
          </span>
        </div>
      </header>
      <main className="mx-auto max-w-[1180px] px-6 py-8">{children}</main>
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

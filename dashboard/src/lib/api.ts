// Data client — FRONTEND_SPEC §01/§07/§08.
//
// Every number on screen comes from a real FastAPI response. There is no demo
// dataset and no fallback: if the backend is unreachable the UI says so and
// renders nothing, because a dashboard that invents a plausible ₹ figure when
// its API is down is worse than one that admits the API is down. Plain fetch,
// all GET, no auth, no client library.
import type {
  CompliancePanelData,
  ComplianceWindow,
  Config,
  Evaluation,
  EventsResponse,
  Health,
  InvoiceDetail,
  Overview,
  Paged,
  RunSummary,
  WorklistRow,
} from './types'

// Defaults to the port `scripts/run_demo.sh` serves the API on, so a fresh
// clone works with no configuration; override with VITE_API_BASE.
export const API_BASE: string =
  (import.meta as any).env?.VITE_API_BASE ?? 'http://localhost:8000'

export class HttpError extends Error {
  constructor(
    public status: number,
    msg: string,
  ) {
    super(msg)
  }
}

async function get<T>(path: string): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`)
  } catch {
    // fetch rejects with an opaque "Failed to fetch" on a refused connection —
    // name the actual cause, since "is the backend running" is the answer 9
    // times out of 10.
    throw new Error(`Can’t reach the API at ${API_BASE} — is the backend running?`)
  }
  if (!res.ok) throw new HttpError(res.status, `${res.status} on ${path}`)
  return (await res.json()) as T
}

// `/worklist` and `/runs/{id}/worklist` answer `{ total, rows }` — the run_id-scoped
// endpoint also carries a top-level `run_id` alongside them. Neither echoes back the
// `limit`/`offset` it was called with, so those come from the request, not the response.
async function getPage<T>(path: string, limit: number, offset: number): Promise<Paged<T>> {
  const { total, rows } = await get<{ total: number; rows: T[] }>(path)
  return { items: rows, total, limit, offset }
}

// ── Endpoints ─────────────────────────────────────────────────────────────

export function getHealth(): Promise<Health> {
  return get('/health')
}

export function getConfig(): Promise<Config> {
  return get('/config')
}

export function getRuns(): Promise<RunSummary[]> {
  return get('/runs')
}

export function getOverview(runId: string): Promise<Overview> {
  return get(`/runs/${runId}/overview`)
}

export function getLiveWorklist(limit: number, offset: number): Promise<Paged<WorklistRow>> {
  return getPage(`/worklist?limit=${limit}&offset=${offset}`, limit, offset)
}

export function getRunWorklist(
  runId: string,
  limit: number,
  offset: number,
  outcome?: string,
): Promise<Paged<WorklistRow>> {
  const q = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (outcome) q.set('outcome', outcome)
  return getPage(`/runs/${runId}/worklist?${q.toString()}`, limit, offset)
}

export function getInvoice(id: string, runId?: string): Promise<InvoiceDetail> {
  return get(`/invoices/${id}${runId ? `?run_id=${runId}` : ''}`)
}

// `at` is a UTC instant without an offset suffix, which the API reads as UTC. A `+` in a
// query string decodes to a space, so shipping a full ISO offset here would need
// escaping to survive the round trip — and the server already has exactly one answer for
// a bare timestamp.
export function getInvoiceCompliance(id: string, at?: string): Promise<CompliancePanelData> {
  const q = at ? `?at=${encodeURIComponent(at)}` : ''
  return get(`/invoices/${id}/compliance${q}`)
}

export function getComplianceWindow(): Promise<ComplianceWindow> {
  return get('/compliance/window')
}

export function getEvaluation(runId?: string): Promise<Evaluation> {
  return get(`/evaluation${runId ? `?run_id=${runId}` : ''}`)
}

// Cursored event stream (§07). `since` is `int | None` server-side: FastAPI
// accepts an omitted param as None but 422s on the literal empty string, so it
// must be left out of the querystring entirely on the first poll.
export function getEvents(runId: string, since: number | null, limit = 50): Promise<EventsResponse> {
  const q = new URLSearchParams({ limit: String(limit) })
  if (since != null) q.set('since', String(since))
  return get(`/runs/${runId}/events?${q.toString()}`)
}

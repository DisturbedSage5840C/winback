// Data client — FRONTEND_SPEC §01/§07/§08.
// When VITE_API_BASE is set, every call hits the real FastAPI backend (no
// client library, plain fetch, all GET, no auth). When it is unset (e.g. the
// Figma Make preview with no backend), it falls back to the labeled demo
// dataset in ../data/demo.ts so the scenes still render. No number is ever
// invented — demo rows mirror the exact API shapes.
import * as demo from '../data/demo'
import type {
  CompliancePanelData,
  Config,
  Evaluation,
  EventsResponse,
  InvoiceDetail,
  Overview,
  Paged,
  RunSummary,
  WorklistRow,
} from './types'

export const API_BASE: string | undefined = (import.meta as any).env?.VITE_API_BASE
export const LIVE = Boolean(API_BASE)

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) throw new HttpError(res.status, `${res.status} on ${path}`)
  return (await res.json()) as T
}

export class HttpError extends Error {
  constructor(public status: number, msg: string) {
    super(msg)
  }
}

const delay = (ms = 140) => new Promise((r) => setTimeout(r, ms))

function page<T>(rows: T[], limit: number, offset: number): Paged<T> {
  return { items: rows.slice(offset, offset + limit), total: rows.length, limit, offset }
}

// `/worklist` and `/runs/{id}/worklist` answer `{ total, rows }` — the run_id-scoped
// endpoint also carries a top-level `run_id` alongside them. Neither echoes back the
// `limit`/`offset` it was called with, so those come from the request, not the response.
async function getPage<T>(path: string, limit: number, offset: number): Promise<Paged<T>> {
  const { total, rows } = await get<{ total: number; rows: T[] }>(path)
  return { items: rows, total, limit, offset }
}

// ── Endpoints ─────────────────────────────────────────────────────────────

export async function getHealth(): Promise<{ ok: boolean }> {
  if (LIVE) return get('/health')
  await delay(60)
  return { ok: true }
}

export async function getConfig(): Promise<Config> {
  if (LIVE) return get('/config')
  await delay()
  return demo.config
}

export async function getRuns(): Promise<RunSummary[]> {
  if (LIVE) return get('/runs')
  await delay()
  return demo.runs
}

export async function getOverview(runId: string): Promise<Overview> {
  if (LIVE) return get(`/runs/${runId}/overview`)
  await delay()
  const o = demo.overviews[runId]
  if (!o) throw new HttpError(404, 'no such run')
  return o
}

export async function getLiveWorklist(limit: number, offset: number): Promise<Paged<WorklistRow>> {
  if (LIVE) return getPage(`/worklist?limit=${limit}&offset=${offset}`, limit, offset)
  await delay()
  return page(demo.liveQueue, limit, offset)
}

export async function getRunWorklist(
  runId: string,
  limit: number,
  offset: number,
  outcome?: string,
): Promise<Paged<WorklistRow>> {
  if (LIVE) {
    const q = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    if (outcome) q.set('outcome', outcome)
    return getPage(`/runs/${runId}/worklist?${q.toString()}`, limit, offset)
  }
  await delay()
  let rows = demo.runWorklists[runId] ?? []
  if (outcome) rows = rows.filter((r) => r.outcome === outcome)
  return page(rows, limit, offset)
}

export async function getInvoice(id: string, runId?: string): Promise<InvoiceDetail> {
  if (LIVE) return get(`/invoices/${id}${runId ? `?run_id=${runId}` : ''}`)
  await delay()
  const inv = demo.invoices[id]
  if (!inv) throw new HttpError(404, 'no such invoice')
  return inv
}

export async function getInvoiceCompliance(id: string): Promise<CompliancePanelData> {
  if (LIVE) return get(`/invoices/${id}/compliance`)
  await delay()
  const c = demo.complianceByInvoice[id]
  if (!c) throw new HttpError(404, 'no such invoice')
  return c
}

export async function getComplianceWindow(): Promise<import('./types').ComplianceWindow> {
  if (LIVE) return get('/compliance/window')
  await delay()
  return demo.complianceWindow()
}

export async function getEvaluation(runId?: string): Promise<Evaluation> {
  if (LIVE) return get(`/evaluation${runId ? `?run_id=${runId}` : ''}`)
  await delay()
  return demo.evaluation
}

// Cursored event stream (§07). In demo mode, reveal events incrementally so
// the trace visibly streams and lands on the red-flash block.
export async function getEvents(runId: string, since: number | null, limit = 50): Promise<EventsResponse> {
  if (LIVE) {
    // `since` is `int | None` server-side: FastAPI accepts an omitted param as
    // None but 422s on the literal empty string, so it must be left out of the
    // querystring entirely on the first poll rather than sent as `since=`.
    const q = new URLSearchParams({ limit: String(limit) })
    if (since != null) q.set('since', String(since))
    return get(`/runs/${runId}/events?${q.toString()}`)
  }
  await delay(120)
  const all = demo.traceEvents[runId] ?? []
  const cursor = since ?? 0
  // reveal at most 2 new events per poll so the stream is watchable
  const next = all.filter((e) => e.event_id > cursor).slice(0, 2)
  const newCursor = next.length ? next[next.length - 1].event_id : cursor
  return { cursor: next.length ? newCursor : cursor, events: next }
}

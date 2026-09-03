// ─────────────────────────────────────────────────────────────────────────
// DEMO DATASET — NOT production data.
// This is a labeled, swappable stand-in that matches the exact API shapes in
// api/main.py (FRONTEND_SPEC §03/§04). When VITE_API_BASE points at a live
// FastAPI backend, none of this is used — the client fetches real rows instead
// (see lib/api.ts). It exists only so the Figma Make preview can render the
// key scenes (§00 batch/guardrail-block) without a running Postgres backend.
// ─────────────────────────────────────────────────────────────────────────
import type {
  ArmResult,
  AuditRow,
  CompliancePanelData,
  ComplianceWindow,
  Config,
  Evaluation,
  InvoiceDetail,
  IntervalRow,
  Overview,
  RootCauseClass,
  RunSummary,
  TraceEvent,
  ViolationRow,
  WorklistRow,
} from '../lib/types'

export const config: Config = { execution_mode: 'simulated', model_version: 'v1' }

export const runs: RunSummary[] = [
  { run_id: 'run_d_2026-09-02', arm: 'D', invoices: 500, touched_live: true },
  { run_id: 'run_c_2026-09-01', arm: 'C', invoices: 500, touched_live: false },
  { run_id: 'run_b_2026-09-01', arm: 'B', invoices: 500, touched_live: false },
  { run_id: 'run_a_2026-08-31', arm: 'A', invoices: 500, touched_live: false },
]

export const overviews: Record<string, Overview> = {
  'run_d_2026-09-02': {
    run_id: 'run_d_2026-09-02',
    arm: 'D',
    at_risk: 500,
    actions_taken: 431,
    retry_attempted: 318,
    nudges_sent: 113,
    recovered: 214,
    recovered_paise: 150582000,
    escalated: 47,
    blocked: 22,
    violations: 0,
    stopped: 69,
    stop_reasons: [
      { stop_reason: 'npci_1_plus_3_cap_exhausted', invoices: 22 },
      { stop_reason: 'afa_ceiling_escalate_human', invoices: 19 },
      { stop_reason: 'consent_withdrawn', invoices: 14 },
      { stop_reason: 'bd_hard_registration_required', invoices: 9 },
      { stop_reason: 'pre_debit_notice_missing', invoices: 5 },
    ],
  },
  'run_c_2026-09-01': {
    run_id: 'run_c_2026-09-01',
    arm: 'C',
    at_risk: 500,
    actions_taken: 389,
    retry_attempted: 372,
    nudges_sent: 17,
    recovered: 176,
    recovered_paise: 128940000,
    escalated: 0,
    blocked: 8,
    violations: 12,
    stopped: 8,
    stop_reasons: [
      { stop_reason: 'legacy_fixed_schedule_exhausted', invoices: 8 },
    ],
  },
  'run_b_2026-09-01': {
    run_id: 'run_b_2026-09-01',
    arm: 'B',
    at_risk: 500,
    actions_taken: 500,
    retry_attempted: 500,
    nudges_sent: 0,
    recovered: 209,
    recovered_paise: 147902000,
    escalated: 0,
    blocked: 0,
    violations: 66,
    stopped: 0,
    stop_reasons: [
      { stop_reason: 'retry_everything_no_stop', invoices: 500 },
    ],
  },
  'run_a_2026-08-31': {
    run_id: 'run_a_2026-08-31',
    arm: 'A',
    at_risk: 500,
    actions_taken: 500,
    retry_attempted: 0,
    nudges_sent: 0,
    recovered: 0,
    recovered_paise: 0,
    escalated: 500,
    blocked: 0,
    violations: 0,
    stopped: 500,
    stop_reasons: [{ stop_reason: 'always_escalate_never_retry', invoices: 500 }],
  },
}

// ── Worklist generation ──────────────────────────────────────────────────
const banks = ['HDFC', 'ICICI', 'SBI', 'Axis', 'Kotak', 'PNB', 'Yes']
const methods = ['upi_autopay', 'card', 'emandate', 'netbanking']
const mccs = ['saas', 'insurance', 'edtech', 'ott', 'utility', 'sip']
const roots: RootCauseClass[] = ['TD', 'BD_transient', 'BD_hard']
const outcomes = ['recovered', 'blocked', 'escalated', 'written_off', 'at_risk'] as const
const actions = ['retry_in_window', 'send_nudge', 'escalate_human', 'redirect_to_window', '—']

function mulberry(seed: number) {
  return function () {
    seed |= 0
    seed = (seed + 0x6d2b79f5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function makeRow(i: number, rnd: () => number, mode: 'live' | 'run'): WorklistRow {
  const used = 1 + Math.floor(rnd() * 4)
  const root = roots[Math.floor(rnd() * roots.length)]
  const base: WorklistRow = {
    invoice_id: `inv_${(204000 + i).toString(36).toUpperCase()}`,
    amount_paise: Math.round((299 + rnd() * 24000) * 100),
    method: methods[Math.floor(rnd() * methods.length)],
    bank: banks[Math.floor(rnd() * banks.length)],
    mcc_category: mccs[Math.floor(rnd() * mccs.length)],
    attempts_used: used,
    attempts_remaining: 4 - used,
    latest_root_cause: root,
  }
  if (mode === 'run') {
    const outcome = outcomes[Math.floor(rnd() * outcomes.length)]
    base.outcome = outcome
    base.action_taken = actions[Math.floor(rnd() * actions.length)]
    base.compliance_violation = false
  }
  return base
}

function buildList(seed: number, mode: 'live' | 'run', total: number): WorklistRow[] {
  const rnd = mulberry(seed)
  const rows: WorklistRow[] = []
  for (let i = 0; i < total; i++) rows.push(makeRow(i, rnd, mode))
  // API pre-sorts by amount desc.
  return rows.sort((a, b) => b.amount_paise - a.amount_paise)
}

export const liveQueue: WorklistRow[] = buildList(7, 'live', 138)
export const runWorklists: Record<string, WorklistRow[]> = {
  'run_d_2026-09-02': buildList(42, 'run', 431),
  'run_c_2026-09-01': buildList(40, 'run', 389),
  'run_b_2026-09-01': buildList(41, 'run', 500),
  'run_a_2026-08-31': buildList(39, 'run', 500),
}

// ── Compliance window (shared) ───────────────────────────────────────────
export function complianceWindow(): ComplianceWindow {
  return {
    now_utc: '2026-09-02T09:42:11Z',
    now_ist: '2026-09-02 15:12 IST',
    is_non_peak: true,
    seconds_to_transition: 108 * 60 + 42,
    peak_windows_ist: [
      ['10:00', '13:00'],
      ['17:00', '21:30'],
    ],
    next_legal_slots_ist: ['15:40 IST', '16:10 IST', '16:40 IST'],
  }
}

// ── The star invoice — its 5th attempt is blocked by the NPCI cap ────────
const STAR = 'inv_204871'

export const complianceByInvoice: Record<string, CompliancePanelData> = {
  [STAR]: {
    invoice_id: STAR,
    evaluated_at: '2026-09-02 15:12 IST',
    amount_paise: 1889900,
    npci: {
      attempts_used: 4,
      attempts_remaining: 0,
      cap: 4,
      verdict: 'DENY',
      detail: 'attempt 5/4 refused — 1 initial + 3 retries exhausted',
      stop_reason: 'npci_1_plus_3_cap_exhausted',
    },
    afa: {
      ceiling_paise: 1500000,
      mcc_category: 'saas',
      verdict: 'APPROVE',
      detail: '₹18,899 ≤ ₹1,00,000 AFA ceiling for this MCC class',
    },
    consent: {
      status: 'active',
      verdict: 'APPROVE',
      detail: 'active consent, outside 90-day DND cooloff',
    },
    pre_debit_notice: {
      notice_sent_at: '2026-09-01 14:00 IST',
      charge_at: '2026-09-02 15:40 IST',
      verdict: 'APPROVE',
      detail: 'notice sent 25h 40m before charge (≥ 24h)',
    },
    window: complianceWindow(),
    root_cause: 'TD',
    retry: {
      verdict: 'DENY',
      authorizing_rule:
        'npci_1_plus_3: attempt 5/4 refused — cap exhausted (window ok, next slot 15:40 IST)',
      stop_reason: 'npci_1_plus_3_cap_exhausted',
      results: [
        { rule: 'npci_retry_cap', verdict: 'DENY', detail: '4/4 attempts used; 5th blocked' },
        { rule: 'non_peak_window', verdict: 'APPROVE', detail: 'non-peak; next slot 15:40 IST' },
        { rule: 'afa_threshold', verdict: 'APPROVE', detail: '₹18,899 ≤ ceiling' },
        { rule: 'consent_gate', verdict: 'APPROVE', detail: 'active consent' },
        { rule: 'pre_debit_notice', verdict: 'APPROVE', detail: 'notice ≥ 24h' },
      ],
      suggested_slots: [],
    },
    nudge: {
      verdict: 'ESCALATE_HUMAN',
      authorizing_rule: 'retry_cap_exhausted → escalate_human (no legal attempts remain)',
      stop_reason: 'npci_1_plus_3_cap_exhausted',
      results: [
        { rule: 'consent_gate', verdict: 'APPROVE', detail: 'active consent for transactional nudge' },
        { rule: 'npci_retry_cap', verdict: 'DENY', detail: 'no attempts remain to convert a nudge into' },
      ],
      suggested_slots: [],
    },
  },
  inv_2049QF: {
    invoice_id: 'inv_2049QF',
    evaluated_at: '2026-09-02 15:12 IST',
    amount_paise: 649900,
    npci: { attempts_used: 2, attempts_remaining: 2, cap: 4, verdict: 'APPROVE', detail: 'attempt 3/4 permitted' },
    afa: { ceiling_paise: 1500000, mcc_category: 'ott', verdict: 'APPROVE', detail: '₹6,499 ≤ ₹15,000 auto-approve' },
    consent: { status: 'active', verdict: 'APPROVE', detail: 'active consent' },
    pre_debit_notice: {
      notice_sent_at: '2026-09-01 10:00 IST',
      charge_at: '2026-09-02 15:40 IST',
      verdict: 'APPROVE',
      detail: 'notice sent 29h 40m before charge',
    },
    window: complianceWindow(),
    root_cause: 'TD',
    retry: {
      verdict: 'APPROVE',
      authorizing_rule: 'npci_1_plus_3: attempt 3/4 permitted; window ok (next slot 15:40 IST)',
      results: [
        { rule: 'npci_retry_cap', verdict: 'APPROVE', detail: 'attempt 3/4 permitted' },
        { rule: 'non_peak_window', verdict: 'APPROVE', detail: 'non-peak; next slot 15:40 IST' },
        { rule: 'afa_threshold', verdict: 'APPROVE', detail: 'auto-approve band' },
        { rule: 'consent_gate', verdict: 'APPROVE', detail: 'active consent' },
        { rule: 'pre_debit_notice', verdict: 'APPROVE', detail: 'notice ≥ 24h' },
      ],
      suggested_slots: ['15:40 IST', '16:10 IST', '16:40 IST'],
    },
    nudge: { verdict: null, detail: 'no root cause on record requiring a nudge — retry path is open' },
  },
}

export const invoices: Record<string, InvoiceDetail> = {
  [STAR]: {
    invoice: {
      invoice_id: STAR,
      subscription_id: 'sub_star',
      cycle_number: 3,
      amount_paise: 1889900,
      charge_at: '2026-08-30T03:30:00Z',
      charge_at_ist: '2026-08-30 09:00 IST',
      notice_sent_at: '2026-08-27T09:00:00Z',
      status: 'escalated',
      method: 'upi_autopay',
      bank: 'HDFC',
      mcc_category: 'saas',
      subscription_status: 'active',
      mandate_start: '2026-01-30',
      paid_count: 2,
      remaining_count: 9,
      cohort: 'demo',
      customer_hash: 'c4f9a1b02e7d',
      consent_status: 'active',
      consent_updated_at: '2026-06-01T00:00:00Z',
      salary_day: 1,
    },
    attempts: [
      { attempt_id: 'att_star_1', attempt_number: 1, attempted_at: '2026-08-30T03:30:00Z', attempted_at_ist: '2026-08-30 09:00 IST', outcome: 'failed', action: 'initial_charge', amount_paise: 1889900, error_code: 'U69', error_reason: 'insufficient_funds', root_cause_class: 'TD', is_non_peak: true, observed: true },
      { attempt_id: 'att_star_2', attempt_number: 2, attempted_at: '2026-08-31T08:40:00Z', attempted_at_ist: '2026-08-31 14:10 IST', outcome: 'failed', action: 'retry', amount_paise: 1889900, error_code: 'U69', error_reason: 'insufficient_funds', root_cause_class: 'TD', is_non_peak: true, observed: true },
      { attempt_id: 'att_star_3', attempt_number: 3, attempted_at: '2026-09-01T08:10:00Z', attempted_at_ist: '2026-09-01 13:40 IST', outcome: 'failed', action: 'retry', amount_paise: 1889900, error_code: 'U69', error_reason: 'insufficient_funds', root_cause_class: 'TD', is_non_peak: true, observed: true },
      { attempt_id: 'att_star_4', attempt_number: 4, attempted_at: '2026-09-02T03:10:00Z', attempted_at_ist: '2026-09-02 08:40 IST', outcome: 'failed', action: 'retry', amount_paise: 1889900, error_code: 'U69', error_reason: 'insufficient_funds', root_cause_class: 'TD', is_non_peak: true, observed: true },
      { attempt_id: 'att_star_5', attempt_number: 5, attempted_at: '2026-09-02T09:42:00Z', attempted_at_ist: '2026-09-02 15:12 IST', outcome: 'blocked', action: 'retry', amount_paise: 1889900, error_code: null, error_reason: 'blocked_by_guardrail', root_cause_class: 'TD', is_non_peak: true, observed: false },
    ],
    decisions: [
      {
        decision_id: 'dec_demo_star_0001',
        run_id: 'run_d_2026-09-02',
        arm: 'D',
        invoice_id: STAR,
        subscription_id: 'sub_star',
        triggering_attempt_id: 'att_star_2',
        model_version: 'v1',
        calibrated_prob: 0.58,
        candidate_set: [
          { kind: 'retry', verdict: 'APPROVE', p_success: 0.58, ruled_out: false, execute_at: '2026-09-01T13:40:00+05:30', nudge_first: false, authorizing_rule: 'npci_1_plus_3: attempt 3/4 permitted; window ok (next slot 13:40 IST)', expected_value_paise: 1102340 },
          { kind: 'nudge', verdict: 'APPROVE', p_success: 0.19, ruled_out: false, execute_at: '2026-09-01T13:40:00+05:30', nudge_first: true, authorizing_rule: 'consent_gate: active consent', expected_value_paise: 358900 },
          { kind: 'escalate_human', verdict: 'APPROVE', p_success: 0.0, ruled_out: true, execute_at: '2026-09-01T13:40:00+05:30', nudge_first: false, stop_reason: 'lower expected value than retry', authorizing_rule: 'cost_policy: dominated candidate', expected_value_paise: 0 },
        ],
        expected_value_paise: 1102340,
        proposed_action: 'retry',
        guardrail_verdict: 'APPROVE',
        authorizing_rule: 'npci_1_plus_3: attempt 3/4 permitted; window ok (next slot 13:40 IST)',
        final_action: 'retry',
        scheduled_for: '2026-09-01T08:10:00Z',
        human_approval_required: false,
        decided_by: 'agent',
        decided_at: '2026-09-01T08:10:00Z',
      },
      {
        decision_id: 'dec_demo_star_0002',
        run_id: 'run_d_2026-09-02',
        arm: 'D',
        invoice_id: STAR,
        subscription_id: 'sub_star',
        triggering_attempt_id: 'att_star_5',
        model_version: 'v1',
        calibrated_prob: null,
        candidate_set: [
          { kind: 'retry', verdict: 'DENY', p_success: 0.44, ruled_out: true, execute_at: '2026-09-02T15:40:00+05:30', nudge_first: false, stop_reason: 'npci_1_plus_3_cap_exhausted', authorizing_rule: 'npci_1_plus_3: attempt 5/4 refused — cap exhausted', expected_value_paise: 831500 },
          { kind: 'nudge', verdict: 'DENY', p_success: 0.12, ruled_out: true, execute_at: '2026-09-02T15:40:00+05:30', nudge_first: true, stop_reason: 'no legal attempt to convert into', authorizing_rule: 'npci_1_plus_3: attempt 5/4 refused — cap exhausted', expected_value_paise: 226700 },
          { kind: 'escalate_human', verdict: 'ESCALATE_HUMAN', p_success: null, ruled_out: false, execute_at: '2026-09-02T15:40:00+05:30', nudge_first: false, authorizing_rule: 'npci_1_plus_3: cap exhausted — human review required', expected_value_paise: 0 },
        ],
        expected_value_paise: 0,
        proposed_action: 'escalate_human',
        guardrail_verdict: 'ESCALATE_HUMAN',
        authorizing_rule: 'npci_1_plus_3: attempt 5/4 refused — cap exhausted',
        final_action: 'escalate_human',
        scheduled_for: '2026-09-02T09:42:00Z',
        human_approval_required: true,
        decided_by: 'agent',
        decided_at: '2026-09-02T09:42:00Z',
      },
    ],
    audit_trail: [
      { event_id: 1, run_id: 'run_d_2026-09-02', arm: 'D', ts_utc: '2026-08-30T03:30:00Z', ts_ist: '2026-08-30 09:00 IST', agent_id: 'winback-orchestrator', agent_version: '1.0.0', subject_type: 'invoice', subject_id: STAR, trigger: 'batch_scan', observed_data: {}, action_taken: 'initial_charge', execution_mode: 'simulated', outcome: 'failed', recovered_amount_paise: 0, stop_reason: null, compliance_violation: false },
      { event_id: 2, run_id: 'run_d_2026-09-02', arm: 'D', ts_utc: '2026-08-31T08:40:00Z', ts_ist: '2026-08-31 14:10 IST', agent_id: 'winback-orchestrator', agent_version: '1.0.0', subject_type: 'invoice', subject_id: STAR, trigger: 'batch_scan', observed_data: {}, action_taken: 'retry', execution_mode: 'simulated', outcome: 'failed', recovered_amount_paise: 0, compliance_violation: false },
      { event_id: 3, run_id: 'run_d_2026-09-02', arm: 'D', ts_utc: '2026-09-01T08:10:00Z', ts_ist: '2026-09-01 13:40 IST', agent_id: 'winback-orchestrator', agent_version: '1.0.0', subject_type: 'invoice', subject_id: STAR, trigger: 'batch_scan', observed_data: {}, action_taken: 'retry', execution_mode: 'live', razorpay_entity_id: 'plink_PzK9xQ2m4Rb7Tc', outcome: 'failed', recovered_amount_paise: 0, compliance_violation: false },
      { event_id: 4, run_id: 'run_d_2026-09-02', arm: 'D', ts_utc: '2026-09-02T03:10:00Z', ts_ist: '2026-09-02 08:40 IST', agent_id: 'winback-orchestrator', agent_version: '1.0.0', subject_type: 'invoice', subject_id: STAR, trigger: 'batch_scan', observed_data: {}, action_taken: 'retry', execution_mode: 'simulated', outcome: 'failed', recovered_amount_paise: 0, compliance_violation: false },
      { event_id: 5, run_id: 'run_d_2026-09-02', arm: 'D', ts_utc: '2026-09-02T09:42:00Z', ts_ist: '2026-09-02 15:12 IST', agent_id: 'winback-orchestrator', agent_version: '1.0.0', subject_type: 'invoice', subject_id: STAR, trigger: 'batch_scan', observed_data: {}, action_taken: 'retry', execution_mode: 'simulated', outcome: 'blocked', recovered_amount_paise: 0, stop_reason: 'npci_1_plus_3_cap_exhausted', compliance_violation: true },
      { event_id: 6, run_id: 'run_d_2026-09-02', arm: 'D', ts_utc: '2026-09-02T09:42:00Z', ts_ist: '2026-09-02 15:12 IST', agent_id: 'winback-orchestrator', agent_version: '1.0.0', subject_type: 'invoice', subject_id: STAR, trigger: 'batch_scan', observed_data: {}, action_taken: 'escalate_human', execution_mode: 'simulated', outcome: 'queued', recovered_amount_paise: 0, compliance_violation: false },
    ],
  },
  inv_2049QF: {
    invoice: {
      invoice_id: 'inv_2049QF',
      subscription_id: 'sub_2049',
      cycle_number: 1,
      amount_paise: 649900,
      charge_at: '2026-09-01T03:30:00Z',
      charge_at_ist: '2026-09-01 09:00 IST',
      notice_sent_at: '2026-08-29T09:00:00Z',
      status: 'at_risk',
      method: 'upi_autopay',
      bank: 'ICICI',
      mcc_category: 'ott',
      subscription_status: 'active',
      mandate_start: '2026-06-01',
      paid_count: 2,
      remaining_count: 10,
      cohort: 'demo',
      customer_hash: 'a19bd3f5c88e',
      consent_status: 'active',
      consent_updated_at: '2026-06-01T00:00:00Z',
      salary_day: 5,
    },
    attempts: [
      { attempt_id: 'att_2049_1', attempt_number: 1, attempted_at: '2026-09-01T03:30:00Z', attempted_at_ist: '2026-09-01 09:00 IST', outcome: 'failed', action: 'initial_charge', amount_paise: 649900, error_code: 'U69', error_reason: 'insufficient_funds', root_cause_class: 'TD', is_non_peak: true, observed: true },
      { attempt_id: 'att_2049_2', attempt_number: 2, attempted_at: '2026-09-02T08:10:00Z', attempted_at_ist: '2026-09-02 13:40 IST', outcome: 'failed', action: 'retry', amount_paise: 649900, error_code: 'U69', error_reason: 'insufficient_funds', root_cause_class: 'TD', is_non_peak: true, observed: true },
    ],
    decisions: [
      {
        decision_id: 'dec_demo_2049_0001',
        run_id: 'run_d_2026-09-02',
        arm: 'D',
        invoice_id: 'inv_2049QF',
        subscription_id: 'sub_2049',
        triggering_attempt_id: 'att_2049_2',
        model_version: 'v1',
        calibrated_prob: 0.58,
        candidate_set: [
          { kind: 'retry', verdict: 'APPROVE', p_success: 0.58, ruled_out: false, execute_at: '2026-09-02T15:40:00+05:30', nudge_first: false, authorizing_rule: 'npci_1_plus_3: attempt 3/4 permitted; window ok (next slot 15:40 IST)', expected_value_paise: 377000 },
          { kind: 'nudge', verdict: 'APPROVE', p_success: 0.2, ruled_out: true, execute_at: '2026-09-02T15:40:00+05:30', nudge_first: true, stop_reason: 'lower expected value than retry', authorizing_rule: 'consent_gate: active consent', expected_value_paise: 129900 },
        ],
        expected_value_paise: 377000,
        proposed_action: 'retry',
        guardrail_verdict: 'APPROVE',
        authorizing_rule: 'npci_1_plus_3: attempt 3/4 permitted; window ok (next slot 15:40 IST)',
        final_action: 'retry',
        scheduled_for: '2026-09-02T08:10:00Z',
        human_approval_required: false,
        decided_by: 'agent',
        decided_at: '2026-09-02T08:10:00Z',
      },
    ],
    audit_trail: [
      { event_id: 7, run_id: 'run_d_2026-09-02', arm: 'D', ts_utc: '2026-09-01T03:30:00Z', ts_ist: '2026-09-01 09:00 IST', agent_id: 'winback-orchestrator', agent_version: '1.0.0', subject_type: 'invoice', subject_id: 'inv_2049QF', trigger: 'batch_scan', observed_data: {}, action_taken: 'initial_charge', execution_mode: 'simulated', outcome: 'failed', recovered_amount_paise: 0, compliance_violation: false },
      { event_id: 8, run_id: 'run_d_2026-09-02', arm: 'D', ts_utc: '2026-09-02T08:10:00Z', ts_ist: '2026-09-02 13:40 IST', agent_id: 'winback-orchestrator', agent_version: '1.0.0', subject_type: 'invoice', subject_id: 'inv_2049QF', trigger: 'batch_scan', observed_data: {}, action_taken: 'retry', execution_mode: 'live', razorpay_entity_id: 'pay_PzL2wR8n5Tc9Kd', outcome: 'failed', recovered_amount_paise: 0, compliance_violation: false },
    ],
  },
}

// ── Live trace event stream for the star run (§06.3 / §00 scene) ─────────
// Ends on the red-flash 5th-attempt block. Served incrementally by lib/api.ts.
export const traceEvents: Record<string, TraceEvent[]> = {
  'run_d_2026-09-02': [
    { event_id: 1, ts_utc: '2026-09-02T09:42:01Z', ts_ist: '15:12:01 IST', invoice_id: 'inv_2049QF', action_taken: 'retry_in_window', channel: 'simulated_upi', outcome: 'retry_scheduled', recovered_amount_paise: 0, execution_mode: 'simulated', compliance_violation: false, trigger: 'batch_scan', authorizing_rule: 'npci_1_plus_3: attempt 3/4 permitted; window ok', guardrail_verdict: 'APPROVE', calibrated_prob: 0.71, expected_value_paise: 84200 },
    { event_id: 2, ts_utc: '2026-09-02T09:42:03Z', ts_ist: '15:12:03 IST', invoice_id: 'inv_204A3P', action_taken: 'send_nudge', channel: 'simulated_sms', outcome: 'nudge_sent', recovered_amount_paise: 0, execution_mode: 'simulated', compliance_violation: false, trigger: 'batch_scan', authorizing_rule: 'consent_gate: active; transactional window ok', guardrail_verdict: 'APPROVE', calibrated_prob: null, expected_value_paise: 5100 },
    { event_id: 3, ts_utc: '2026-09-02T09:42:05Z', ts_ist: '15:12:05 IST', invoice_id: 'inv_204B7X', action_taken: 'redirect_to_window', channel: null, outcome: 'rescheduled', recovered_amount_paise: 0, execution_mode: 'simulated', compliance_violation: false, trigger: 'batch_scan', authorizing_rule: 'non_peak_window: peak 17:00–21:30 — redirected to 15:40 IST', guardrail_verdict: 'REDIRECT_TO_WINDOW', calibrated_prob: 0.58, expected_value_paise: 19900 },
    { event_id: 4, ts_utc: '2026-09-02T09:42:08Z', ts_ist: '15:12:08 IST', invoice_id: 'inv_204C1M', action_taken: 'escalate_human', channel: null, outcome: 'escalated', recovered_amount_paise: 0, execution_mode: 'simulated', compliance_violation: false, trigger: 'batch_scan', authorizing_rule: 'afa_threshold: ₹1,04,200 > ₹1,00,000 ceiling → escalate_human', guardrail_verdict: 'ESCALATE_HUMAN', stop_reason: 'afa_ceiling_escalate_human', calibrated_prob: null, expected_value_paise: 104200 },
    { event_id: 5, ts_utc: '2026-09-02T09:42:10Z', ts_ist: '15:12:10 IST', invoice_id: 'inv_204D9K', action_taken: 'retry_in_window', channel: 'simulated_upi', outcome: 'recovered', recovered_amount_paise: 41200, execution_mode: 'simulated', compliance_violation: false, trigger: 'batch_scan', authorizing_rule: 'npci_1_plus_3: attempt 2/4 permitted; window ok', guardrail_verdict: 'APPROVE', calibrated_prob: 0.83, expected_value_paise: 41200 },
    { event_id: 6, ts_utc: '2026-09-02T09:42:13Z', ts_ist: '15:12:13 IST', invoice_id: 'inv_204E4T', action_taken: 'send_nudge', channel: null, outcome: 'blocked', recovered_amount_paise: 0, execution_mode: 'simulated', compliance_violation: false, trigger: 'batch_scan', authorizing_rule: 'consent_gate: withdrawn — nudge blocked', guardrail_verdict: 'DENY', stop_reason: 'consent_withdrawn', calibrated_prob: null, expected_value_paise: 0 },
    { event_id: 7, ts_utc: '2026-09-02T09:42:16Z', ts_ist: '15:12:16 IST', invoice_id: 'inv_204F8W', action_taken: 'retry_in_window', channel: 'simulated_upi', outcome: 'recovered', recovered_amount_paise: 27600, execution_mode: 'simulated', compliance_violation: false, trigger: 'batch_scan', authorizing_rule: 'npci_1_plus_3: attempt 4/4 permitted; window ok', guardrail_verdict: 'APPROVE', calibrated_prob: 0.62, expected_value_paise: 27600 },
    { event_id: 8, ts_utc: '2026-09-02T09:42:19Z', ts_ist: '15:12:19 IST', invoice_id: STAR, action_taken: 'retry_in_window', channel: null, outcome: 'blocked', recovered_amount_paise: 0, execution_mode: 'simulated', compliance_violation: true, trigger: 'batch_scan', authorizing_rule: 'npci_1_plus_3: attempt 5/4 refused — cap exhausted', guardrail_verdict: 'DENY', stop_reason: 'npci_1_plus_3_cap_exhausted', calibrated_prob: null, expected_value_paise: 0 },
  ],
}

// ── Evaluation (§03.5) ───────────────────────────────────────────────────
const evalArms: ArmResult[] = [
  { arm: 'A', arm_label: 'A — Never retry, always escalate', recovered_paise: 0, attempts_consumed: 500, legal_attempts_consumed: 500, paise_per_legal_attempt: 0, nudges_sent: 0, escalations: 500, compliance_violations: 0, written_off: 500 },
  { arm: 'B', arm_label: 'B — Retry everything to 1+3, any time', recovered_paise: 147902000, attempts_consumed: 1740, legal_attempts_consumed: 1512, paise_per_legal_attempt: 97819, nudges_sent: 0, escalations: 0, compliance_violations: 66, written_off: 291 },
  { arm: 'C', arm_label: 'C — Legacy fixed T+1/T+2/T+3', recovered_paise: 128940000, attempts_consumed: 1116, legal_attempts_consumed: 1104, paise_per_legal_attempt: 116793, nudges_sent: 17, escalations: 0, compliance_violations: 12, written_off: 324 },
  { arm: 'D', arm_label: 'D — Winback (model + policy + guardrail)', recovered_paise: 150582000, attempts_consumed: 831, legal_attempts_consumed: 831, paise_per_legal_attempt: 181205, nudges_sent: 113, escalations: 47, compliance_violations: 0, written_off: 239 },
]

const evalViolations: ViolationRow[] = [
  { arm: 'B', stop_reason: 'peak_window_presentment', violations: 51 },
  { arm: 'B', stop_reason: 'pre_debit_notice_missing', violations: 15 },
  { arm: 'C', stop_reason: 'peak_window_presentment', violations: 9 },
  { arm: 'C', stop_reason: 'pre_debit_notice_missing', violations: 3 },
  { arm: 'D', stop_reason: 'peak_window_presentment', violations: 0 },
  { arm: 'A', stop_reason: 'peak_window_presentment', violations: 0 },
]

const evalIntervals: IntervalRow[] = [
  { run_id: 'run_d_2026-09-02', arm: 'D', comparison: 'D_vs_B', statistic: 'paise_per_legal_attempt', point: 83386, ci_low: 61200, ci_high: 105900, resamples: 2000, confidence: 0.95 },
  { run_id: 'run_d_2026-09-02', arm: 'D', comparison: 'D_vs_B', statistic: 'compliance_violations', point: -66, ci_low: -96, ci_high: -42, resamples: 2000, confidence: 0.95 },
]

export const evaluation: Evaluation = {
  run: {
    run_id: 'run_d_2026-09-02',
    created_at: '2026-09-02T00:00:00Z',
    model_version: 'v1',
    dataset_version: 'demo',
    dataset_fingerprint: 'demo-fixture',
    seed: 0,
    bootstrap_resamples: 2000,
    cohort: 'demo',
  },
  arms: evalArms,
  violations: evalViolations,
  intervals: evalIntervals,
}

export function auditNoop(_: AuditRow) {}
export const STAR_INVOICE = STAR

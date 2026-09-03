// API response shapes — mirror api/main.py exactly (FRONTEND_SPEC §03/§04).
// Every field name here is what the endpoint returns; nothing is invented.

export type Verdict =
  | 'APPROVE'
  | 'DENY'
  | 'ESCALATE_HUMAN'
  | 'REDIRECT_TO_WINDOW'
  | null

export type Outcome =
  | 'recovered'
  | 'blocked'
  | 'escalated'
  | 'written_off'
  | 'at_risk'

export type RootCauseClass = 'TD' | 'BD_transient' | 'BD_hard'

export interface RunSummary {
  run_id: string
  arm: string
  invoices: number
  touched_live: boolean
}

export interface StopReasonRow {
  stop_reason: string
  invoices: number
}

export interface Overview {
  run_id: string
  arm: string
  at_risk: number
  actions_taken: number
  retry_attempted: number
  nudges_sent: number
  recovered: number
  recovered_paise: number
  escalated: number
  blocked: number
  violations: number
  stopped: number
  stop_reasons: StopReasonRow[]
}

export interface Config {
  execution_mode: 'simulated' | 'live'
  model_version: string
}

export interface WorklistRow {
  invoice_id: string
  amount_paise: number
  method: string
  bank: string
  mcc_category: string
  attempts_used: number
  attempts_remaining: number
  latest_root_cause: RootCauseClass
  action_taken?: string | null
  outcome?: Outcome | null
  compliance_violation?: boolean
}

export interface Paged<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Attempt {
  attempt_id: string
  attempt_number: number
  attempted_at: string
  attempted_at_ist: string
  outcome: string
  action: string
  amount_paise: number
  error_code?: string | null
  error_source?: string | null
  error_reason?: string | null
  root_cause_class: RootCauseClass | null
  is_non_peak: boolean
  observed: boolean
  run_id?: string | null
}

// One scored `(kind × slot)` candidate the guardrail considered — `kind` is
// the action family (retry/nudge/write_off/escalate), `execute_at` the slot
// it was scored for. `p_success` is the model's raw probability (distinct
// from the decision-level `calibrated_prob`) and is null when the guardrail
// ruled the candidate out before scoring (e.g. BD_hard: nothing to score).
export interface Candidate {
  kind: string
  verdict: Verdict
  p_success: number | null
  ruled_out: boolean
  execute_at: string
  nudge_first: boolean
  stop_reason?: string | null
  authorizing_rule: string
  expected_value_paise: number | null
}

export interface Decision {
  decision_id: string
  run_id: string
  arm: string
  invoice_id: string
  subscription_id: string
  triggering_attempt_id: string
  model_version: string
  calibrated_prob: number | null
  candidate_set: Candidate[]
  expected_value_paise: number
  proposed_action: string
  guardrail_verdict: Verdict
  authorizing_rule: string
  final_action: string
  scheduled_for: string
  human_approval_required: boolean
  supersedes_decision_id?: string | null
  decided_by: string
  decided_at: string
}

export interface AuditRow {
  event_id: number
  run_id: string
  arm: string
  ts_utc: string
  ts_ist: string
  agent_id: string
  agent_version: string
  subject_type: string
  subject_id: string
  trigger: string
  observed_data: Record<string, unknown>
  decision_id?: string | null
  action_taken: string
  channel?: string | null
  execution_mode: 'simulated' | 'live'
  razorpay_entity_id?: string | null
  outcome: string
  recovered_amount_paise: number
  stop_reason?: string | null
  compliance_violation: boolean
}

export interface InvoiceRow {
  invoice_id: string
  subscription_id: string
  cycle_number: number
  amount_paise: number
  charge_at: string
  charge_at_ist: string
  notice_sent_at?: string | null
  status: string
  method: string
  bank: string
  mcc_category: string
  subscription_status: string
  mandate_start: string
  paid_count: number
  remaining_count: number
  cohort: string
  customer_hash: string
  consent_status: string
  consent_updated_at?: string | null
  salary_day: number
}

export interface InvoiceDetail {
  invoice: InvoiceRow
  attempts: Attempt[]
  decisions: Decision[]
  audit_trail: AuditRow[]
}

export interface ComplianceWindow {
  now_utc: string
  now_ist: string
  is_non_peak: boolean
  seconds_to_transition: number
  peak_windows_ist: [string, string][]
  next_legal_slots_ist: string[]
}

export interface RuleResult {
  rule: string
  verdict: Verdict
  detail: string
}

export interface ComposedVerdict {
  verdict: Verdict
  authorizing_rule?: string
  stop_reason?: string | null
  detail?: string
  results?: RuleResult[]
  suggested_slots?: string[]
}

export interface CompliancePanelData {
  invoice_id: string
  evaluated_at: string
  amount_paise: number
  npci: {
    attempts_used: number
    attempts_remaining: number
    cap: number
    verdict: Verdict
    detail: string
    stop_reason?: string | null
  }
  afa: {
    ceiling_paise: number
    mcc_category: string
    verdict: Verdict
    detail: string
    stop_reason?: string | null
  }
  consent: { status: string; verdict: Verdict; detail: string; stop_reason?: string | null }
  pre_debit_notice: {
    notice_sent_at: string | null
    charge_at: string | null
    verdict: Verdict
    detail: string
    stop_reason?: string | null
  }
  window: ComplianceWindow
  root_cause: RootCauseClass | null
  retry: ComposedVerdict
  nudge: ComposedVerdict
}

export interface TraceEvent {
  event_id: number
  ts_utc: string
  ts_ist: string
  invoice_id: string
  action_taken: string
  channel?: string | null
  outcome: string
  stop_reason?: string | null
  recovered_amount_paise: number
  execution_mode: 'simulated' | 'live'
  razorpay_entity_id?: string | null
  compliance_violation: boolean
  trigger: string
  authorizing_rule: string
  guardrail_verdict: Verdict
  calibrated_prob: number | null
  expected_value_paise: number
}

export interface EventsResponse {
  run_id?: string
  cursor: number | null
  events: TraceEvent[]
}

export interface ArmResult {
  arm: string
  arm_label: string
  recovered_paise: number
  attempts_consumed: number
  legal_attempts_consumed: number
  paise_per_legal_attempt: number
  nudges_sent: number
  escalations: number
  compliance_violations: number
  written_off: number
}

export interface ViolationRow {
  arm: string
  stop_reason: string
  violations: number
}

export interface IntervalRow {
  run_id: string
  arm: string
  comparison: string
  statistic: string
  point: number
  ci_low: number
  ci_high: number
  resamples: number
  confidence: number
}

export interface EvalRun {
  run_id: string
  created_at: string
  model_version: string
  dataset_version: string
  dataset_fingerprint: string
  seed: number
  bootstrap_resamples: number
  cohort: string
}

export interface Evaluation {
  run: EvalRun
  arms: ArmResult[]
  violations: ViolationRow[]
  intervals: IntervalRow[]
}

-- Winback — core schema
-- Razorpay AI Buildathon 2026, Track 03 (AI Revenue Recovery)
--
-- Design notes that matter for the panel:
--  * Money is ALWAYS paise (BIGINT). No floats touch a rupee value, anywhere.
--  * Timestamps are TIMESTAMPTZ (UTC). IST wall-clock is a generated column,
--    because every NPCI rule in this system is expressed in IST wall-clock and
--    re-deriving it at query time is how you get a compliance bug.
--  * payment_attempts holds BOTH the observational history (run_id IS NULL) and
--    the attempts produced by each evaluation arm (run_id IS NOT NULL). One table,
--    one filter, no divergent code paths between "training data" and "what the
--    agent did".
--  * audit_log is append-only, enforced by trigger + revoked grants (02, 03).

BEGIN;

-- ---------------------------------------------------------------- enums-as-checks
-- Deliberately CHECK constraints rather than PG enums: the value sets are quoted
-- verbatim from Razorpay/NPCI docs and will change as those docs change. ALTER TYPE
-- on a live enum is a worse migration than editing a CHECK.

-- ---------------------------------------------------------------- customers
CREATE TABLE customers (
    customer_id        TEXT PRIMARY KEY,
    -- sha256(customer_id)[:12]; the ONLY customer identifier allowed into
    -- audit_log.observed_data. Redaction happens at write time, not render time.
    customer_hash      TEXT NOT NULL UNIQUE,
    signup_date        DATE NOT NULL,

    consent_status     TEXT NOT NULL
                       CHECK (consent_status IN ('active', 'dnd', 'withdrawn')),
    consent_updated_at TIMESTAMPTZ NOT NULL,

    -- Salary day drives the balance-replenishment process in sim/world.py.
    -- This is the real mechanism behind India's UPI-Autopay insufficient_funds
    -- failures, and the timing signal the model has to discover for itself.
    salary_day         INT NOT NULL CHECK (salary_day BETWEEN 1 AND 28),

    -- Populated only for the live cohort (real Razorpay test-mode entities).
    rzp_customer_id    TEXT
);

-- ---------------------------------------------------------------- subscriptions
CREATE TABLE subscriptions (
    subscription_id     TEXT PRIMARY KEY,
    customer_id         TEXT NOT NULL REFERENCES customers(customer_id),

    method              TEXT NOT NULL
                        CHECK (method IN ('upi_autopay', 'card_mandate', 'netbanking')),
    bank                TEXT NOT NULL,
    mcc_category        TEXT NOT NULL,
    amount_paise        BIGINT NOT NULL CHECK (amount_paise > 0),

    -- Razorpay subscription lifecycle, verbatim from the Subscriptions docs.
    status              TEXT NOT NULL
                        CHECK (status IN ('created', 'authenticated', 'active', 'pending',
                                          'halted', 'cancelled', 'completed', 'expired')),
    mandate_start       DATE NOT NULL,
    paid_count          INT NOT NULL DEFAULT 0,
    remaining_count     INT NOT NULL DEFAULT 0,

    -- Split assignment. Frozen at generation time, BEFORE any model is fit.
    -- Split is by customer AND by time (see ml/features.py) so no customer
    -- straddles two splits and no future leaks backwards.
    cohort              TEXT NOT NULL
                        CHECK (cohort IN ('train', 'calibrate', 'test')),

    is_live_cohort      BOOLEAN NOT NULL DEFAULT FALSE,
    rzp_subscription_id TEXT,
    rzp_token_id        TEXT
);

CREATE INDEX ON subscriptions (customer_id);
CREATE INDEX ON subscriptions (cohort);
CREATE INDEX ON subscriptions (status) WHERE status IN ('pending', 'halted');

-- ---------------------------------------------------------------- invoices
-- One row per billing cycle. This is the unit "revenue at risk" is measured on,
-- and the unit the NPCI 1+3 attempt budget is scoped to.
CREATE TABLE invoices (
    invoice_id       TEXT PRIMARY KEY,
    subscription_id  TEXT NOT NULL REFERENCES subscriptions(subscription_id),
    cycle_number     INT NOT NULL,
    amount_paise     BIGINT NOT NULL CHECK (amount_paise > 0),

    -- T=0: the scheduled charge moment for this cycle.
    charge_at        TIMESTAMPTZ NOT NULL,
    charge_at_ist    TIMESTAMP GENERATED ALWAYS AS
                     (charge_at AT TIME ZONE 'Asia/Kolkata') STORED,

    -- RBI e-mandate: a debit requires a pre-transaction notice >= 24h ahead.
    -- NULL means no notice on record -> compliance/pre_debit_notice.py blocks any
    -- NEW debit (it does not block a retry of an already-noticed cycle).
    notice_sent_at   TIMESTAMPTZ,

    status           TEXT NOT NULL
                     CHECK (status IN ('paid', 'at_risk', 'recovered', 'written_off')),

    UNIQUE (subscription_id, cycle_number)
);

CREATE INDEX ON invoices (subscription_id);
CREATE INDEX ON invoices (status) WHERE status = 'at_risk';

-- ---------------------------------------------------------------- payment_attempts
CREATE TABLE payment_attempts (
    attempt_id       TEXT PRIMARY KEY,
    invoice_id       TEXT NOT NULL REFERENCES invoices(invoice_id),
    subscription_id  TEXT NOT NULL REFERENCES subscriptions(subscription_id),

    -- 1 = the original charge, 2..4 = the three retries NPCI OC-215 permits.
    -- The CHECK is a backstop only; compliance/npci_retry_cap.py is the real gate,
    -- and it is unit-tested to block attempt 5 even when the model wants it.
    attempt_number   INT NOT NULL CHECK (attempt_number BETWEEN 1 AND 4),

    attempted_at     TIMESTAMPTZ NOT NULL,
    attempted_at_ist TIMESTAMP GENERATED ALWAYS AS
                     (attempted_at AT TIME ZONE 'Asia/Kolkata') STORED,
    -- Denormalised on purpose: this is the field the compliance panel and the
    -- violations-by-arm chart both read, and recomputing the NPCI window in SQL
    -- in two places is exactly how the two would drift apart.
    is_non_peak      BOOLEAN NOT NULL,

    action           TEXT NOT NULL,
    amount_paise     BIGINT NOT NULL CHECK (amount_paise > 0),

    outcome          TEXT NOT NULL CHECK (outcome IN ('captured', 'failed')),

    -- Razorpay error object, verbatim field names.
    error_code       TEXT,
    error_source     TEXT,
    error_step       TEXT,
    error_reason     TEXT,

    -- Deterministic lookup from (code, source, step, reason). NEVER model output.
    root_cause_class TEXT CHECK (root_cause_class IN ('TD', 'BD_transient', 'BD_hard')),

    -- FALSE = this attempt exists in the oracle but the legacy policy never made it,
    -- so its outcome was never observed. Excluded from training; available to the
    -- counterfactual evaluator. This is what makes the training data honestly censored.
    observed         BOOLEAN NOT NULL DEFAULT TRUE,
    -- Which legacy filter suppressed this retry. Recorded rather than inferred: the
    -- two reasons are different kinds of bias and the calibration report splits on
    -- them. 'legacy_value_floor' is a decision someone made; 'legacy_rail_excluded'
    -- is a rail the retry job was never extended to.
    censoring_reason TEXT CHECK (
        censoring_reason IN ('legacy_value_floor', 'legacy_rail_excluded')
    ),

    -- NULL/NULL => observational history. Set together for evaluation-arm attempts.
    run_id           TEXT,
    arm              TEXT CHECK (arm IN ('A', 'B', 'C', 'D')),

    -- hash(subject, invoice, attempt_number, action, slot) -> the same coin flip is
    -- returned no matter which arm asks for it. This is what makes arm comparison
    -- paired rather than independent, and the whole evaluation reproducible.
    oracle_seed      TEXT NOT NULL,

    CHECK ((run_id IS NULL) = (arm IS NULL)),
    -- An unobserved row without a reason is an unexplained hole in the training
    -- data, and an observed row with one is a contradiction. Neither may exist.
    CHECK (observed = (censoring_reason IS NULL)),
    UNIQUE (invoice_id, attempt_number, run_id)
);

CREATE INDEX ON payment_attempts (invoice_id);
CREATE INDEX ON payment_attempts (subscription_id);
CREATE INDEX ON payment_attempts (run_id, arm);
-- The training-set index: observational history only.
CREATE INDEX ON payment_attempts (observed) WHERE run_id IS NULL;

-- ---------------------------------------------------------------- decisions
-- One row per agent decision, written BEFORE the action executes.
CREATE TABLE decisions (
    decision_id             TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL,
    arm                     TEXT NOT NULL CHECK (arm IN ('A', 'B', 'C', 'D')),

    invoice_id              TEXT NOT NULL REFERENCES invoices(invoice_id),
    subscription_id         TEXT NOT NULL REFERENCES subscriptions(subscription_id),
    -- The failed attempt that triggered this decision.
    triggering_attempt_id   TEXT REFERENCES payment_attempts(attempt_id),

    model_version           TEXT NOT NULL,
    calibrated_prob         NUMERIC(6,5) CHECK (calibrated_prob BETWEEN 0 AND 1),

    -- EVERY scored (action x slot) candidate, not just the winner. This is what
    -- makes the drill-down drawer convincing: you can see what it rejected and why.
    candidate_set           JSONB NOT NULL DEFAULT '[]'::jsonb,
    expected_value_paise    BIGINT,

    proposed_action         TEXT NOT NULL,

    -- The guardrail can only ever return one of these four.
    guardrail_verdict       TEXT NOT NULL
                            CHECK (guardrail_verdict IN
                                   ('APPROVE', 'REDIRECT_TO_WINDOW', 'ESCALATE_HUMAN', 'DENY')),
    -- Rendered verbatim into the audit row and onto the UI chip, e.g.
    -- "npci_1_plus_3: attempt 2/4 permitted; window ok (next slot 13:40 IST)"
    authorizing_rule        TEXT NOT NULL,

    final_action            TEXT NOT NULL,
    scheduled_for           TIMESTAMPTZ,

    human_approval_required BOOLEAN NOT NULL DEFAULT FALSE,

    -- decisions is append-only (02_append_only.sql). A human overturning an
    -- escalation does not UPDATE this row -- it writes a NEW row pointing back here.
    -- "Nothing in this system is ever mutated; a reversal is a new row that
    -- supersedes" is a much shorter answer to a panelist than any audit-diff scheme.
    supersedes_decision_id  TEXT REFERENCES decisions(decision_id),
    decided_by              TEXT NOT NULL DEFAULT 'agent'
                            CHECK (decided_by IN ('agent', 'human')),

    decided_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON decisions (run_id, arm);
CREATE INDEX ON decisions (invoice_id);
CREATE INDEX ON decisions (guardrail_verdict);

-- ---------------------------------------------------------------- audit_log
-- The compliance artifact AND the dashboard drill-down source. Append-only.
CREATE TABLE audit_log (
    event_id               BIGSERIAL PRIMARY KEY,
    run_id                 TEXT,
    arm                    TEXT CHECK (arm IN ('A', 'B', 'C', 'D')),

    ts_utc                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    ts_ist                 TIMESTAMP GENERATED ALWAYS AS
                           (ts_utc AT TIME ZONE 'Asia/Kolkata') STORED,

    agent_id               TEXT NOT NULL,
    agent_version          TEXT NOT NULL,

    subject_type           TEXT NOT NULL
                           CHECK (subject_type IN ('subscription', 'invoice', 'attempt', 'customer')),
    subject_id             TEXT NOT NULL,

    trigger                TEXT NOT NULL,   -- 'batch_scan' | 'webhook:subscription.pending' | ...

    -- PII-redacted: customer_hash only, never customer_id/email/contact.
    observed_data          JSONB NOT NULL DEFAULT '{}'::jsonb,

    decision_id            TEXT REFERENCES decisions(decision_id),

    action_taken           TEXT,
    channel                TEXT,

    -- Which executor actually ran. Test mode moves no real money, so the batch runs
    -- against the seeded oracle; the live cohort hits real Razorpay test-mode APIs.
    -- Recording this per row is the honest-metrics move, not a footnote.
    execution_mode         TEXT CHECK (execution_mode IN ('live', 'simulated')),
    razorpay_entity_id     TEXT,            -- real plink_/order_/pay_/inv_ id when live

    outcome                TEXT CHECK (outcome IN
                                       ('recovered', 'failed', 'deferred', 'escalated', 'blocked')),
    recovered_amount_paise BIGINT NOT NULL DEFAULT 0,
    stop_reason            TEXT,

    -- TRUE when the action breached an NPCI/RBI/TRAI rule. Arm D must be 0 by
    -- construction; arms B and C will not be, and that contrast is the thesis.
    compliance_violation   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX ON audit_log (run_id, arm);
CREATE INDEX ON audit_log (subject_type, subject_id);
CREATE INDEX ON audit_log (decision_id);
CREATE INDEX ON audit_log (compliance_violation) WHERE compliance_violation;
CREATE INDEX ON audit_log (ts_utc DESC);

-- ---------------------------------------------------------------- evaluation
CREATE TABLE eval_runs (
    run_id           TEXT PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_version    TEXT NOT NULL,
    dataset_version  TEXT NOT NULL,
    seed             BIGINT NOT NULL,
    cohort           TEXT NOT NULL CHECK (cohort IN ('train', 'calibrate', 'test')),
    notes            TEXT
);

CREATE TABLE eval_arm_results (
    run_id                   TEXT NOT NULL REFERENCES eval_runs(run_id),
    arm                      TEXT NOT NULL CHECK (arm IN ('A', 'B', 'C', 'D')),
    arm_label                TEXT NOT NULL,

    invoices_evaluated       INT NOT NULL,
    recovered_paise          BIGINT NOT NULL,
    attempts_consumed        INT NOT NULL,
    legal_attempts_consumed  INT NOT NULL,
    nudges_sent              INT NOT NULL,
    escalations              INT NOT NULL,
    written_off              INT NOT NULL,
    compliance_violations    INT NOT NULL,

    -- The headline metric. Not raw rupees recovered: a policy that recovers more
    -- by breaking the retry cap has not won anything a merchant can actually ship.
    paise_per_legal_attempt  NUMERIC(14,2),

    -- Paired bootstrap over subscriptions (same oracle seeds across arms).
    ci_low_paise             BIGINT,
    ci_high_paise            BIGINT,

    PRIMARY KEY (run_id, arm)
);

-- ---------------------------------------------------------------- views
CREATE VIEW recovery_funnel AS
SELECT
    run_id,
    arm,
    count(DISTINCT subject_id) FILTER (WHERE subject_type = 'invoice')      AS at_risk,
    count(*) FILTER (WHERE action_taken IS NOT NULL)                        AS actions_taken,
    count(*) FILTER (WHERE action_taken LIKE 'retry%')                      AS retry_attempted,
    count(*) FILTER (WHERE action_taken LIKE '%nudge%')                     AS nudges_sent,
    count(*) FILTER (WHERE outcome = 'recovered')                           AS recovered,
    coalesce(sum(recovered_amount_paise) FILTER (WHERE outcome = 'recovered'), 0)
                                                                            AS recovered_paise,
    count(*) FILTER (WHERE outcome = 'escalated')                           AS escalated,
    count(*) FILTER (WHERE outcome = 'blocked')                             AS blocked,
    count(*) FILTER (WHERE stop_reason IS NOT NULL)                         AS stopped,
    count(*) FILTER (WHERE compliance_violation)                            AS violations
FROM audit_log
GROUP BY run_id, arm;

-- Exception worklist: at-risk invoices ranked by rupees at risk, with the attempt
-- budget already computed. This is the query the worklist page runs.
CREATE VIEW exception_worklist AS
SELECT
    i.invoice_id,
    i.subscription_id,
    s.customer_id,
    c.customer_hash,
    s.method,
    s.bank,
    s.mcc_category,
    i.amount_paise,
    i.charge_at,
    i.charge_at_ist,
    i.notice_sent_at,
    s.status                                        AS subscription_status,
    c.consent_status,
    -- `AND a.observed` is load-bearing, not defensive. Rows with observed = FALSE
    -- are counterfactual: outcomes the oracle knows for retries the legacy policy
    -- never made. They cost no NPCI budget, because they never reached a rail.
    -- Counting them here would tell the worklist a censored invoice had used all
    -- four attempts when it had used one, and the agent would decline to retry the
    -- very invoices the censoring makes most interesting.
    count(a.attempt_id) FILTER (WHERE a.run_id IS NULL AND a.observed) AS attempts_used,
    4 - count(a.attempt_id) FILTER (WHERE a.run_id IS NULL AND a.observed)
                                                    AS attempts_remaining,
    max(a.attempted_at) FILTER (WHERE a.run_id IS NULL AND a.observed) AS last_attempt_at,
    (array_agg(a.root_cause_class ORDER BY a.attempt_number DESC)
        FILTER (WHERE a.run_id IS NULL AND a.observed AND a.root_cause_class IS NOT NULL))[1]
                                                    AS latest_root_cause
FROM invoices i
JOIN subscriptions s USING (subscription_id)
JOIN customers c USING (customer_id)
LEFT JOIN payment_attempts a USING (invoice_id)
WHERE i.status = 'at_risk'
GROUP BY i.invoice_id, i.subscription_id, s.customer_id, c.customer_hash,
         s.method, s.bank, s.mcc_category, i.amount_paise, i.charge_at,
         i.charge_at_ist, i.notice_sent_at, s.status, c.consent_status;

COMMIT;

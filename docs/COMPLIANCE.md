# Compliance

Every rule Winback enforces, where it comes from, and the test that proves it
**blocks** rather than merely warns. This document is the specification the modules in
`compliance/` are written against, test-first.

**None of these are LLM judgments.** They are pure functions over explicit inputs, and
the agent cannot execute a money-moving tool without one of them returning an approval
that is recorded in `decisions.authorizing_rule` before the fact.

---

## 01 — The rules

### 1.1 NPCI retry cap — `compliance/npci_retry_cap.py`

**Source.** NPCI circular **OC-215-A**, effective 1 August 2025. A UPI Autopay mandate
permits **one execution attempt plus a maximum of three retries** per mandate per
invoice sequence — four in total.

**Rule.** `attempt_number ∈ {1,2,3,4}`. A fifth is refused with
`stop_reason = "npci_1_plus_3_cap_exhausted"`, **irrespective of the model's
probability estimate.** The remaining budget is scoped to `(subscription_id,
invoice_id)`, not to the subscription: a new billing cycle restores a fresh budget.

**Must-block test.** The model returns `p = 0.97` on attempt 5; the guardrail returns
`DENY`, no attempt row is written, and the audit row carries the stop reason.

### 1.2 Non-peak execution window — `compliance/non_peak_window.py`

**Source.** OC-215-A restricts mandate execution to non-peak hours. Peak, in IST:
**10:00–13:00** and **17:00–21:30**.

**Rule.** A proposal falling inside a peak window is **not** dropped — it is
**converted** to `retry_in_window` with the next valid slot attached. Silently
discarding a legal action because its timing was wrong loses money for no compliance
benefit.

**Must-block tests.** Boundaries at 09:59 / 10:00 / 13:00 / 16:59 / 17:00 / 21:29 /
21:30, each resolving to the correct side, plus a midnight-crossing case where the
next valid slot is on the following day.

### 1.3 AFA thresholds — `compliance/afa_threshold.py`

**Source.** RBI e-mandate framework, as amended through 2026. Recurring debits up to
**₹15,000** per transaction execute without per-cycle Additional Factor of
Authentication. The limit is **₹1,00,000** for insurance premiums, mutual-fund SIPs,
and credit-card bill payments.

**Rule.** Above the applicable ceiling the action becomes `ESCALATE_HUMAN`. Winback
never auto-debits above an AFA threshold.

**Must-block tests.** Exact boundaries at 15,000 / 15,001 and 1,00,000 / 1,00,001, for
both MCC classes. A ₹20,000 SaaS debit escalates; a ₹20,000 SIP debit does not.

### 1.4 Consent and DND — `compliance/consent_gate.py`

**Source.** TRAI **TCCCPR 2018** and the February 2025 amendments.

**Rule.** No nudge on any channel without active consent. A customer inside the
**90-day DND cooloff** is blocked outright. Transactional consent is valid for a
**7-day** window from the triggering transaction.

**Must-block test.** A withdrawn-consent customer with ₹40,000 at risk receives
nothing on any channel, and the audit row records `blocked` with the consent reason —
the case where the money is large enough to make the temptation real.

> **Channel note.** Real SMS/WhatsApp sending requires DLT registration, so the
> channel is simulated (`channel = "simulated_sms"`). The gate in front of it is not:
> it runs, it blocks, and it is logged, exactly as it would in production. Payment
> links in the live lane are created with `notify: {sms: false, email: false}` — a
> real Razorpay artifact with the send suppressed.

### 1.5 Pre-debit notice — `compliance/pre_debit_notice.py`

**Source.** RBI e-mandate framework: a pre-transaction notification at least
**24 hours** before the debit.

**Rule.** `notice_sent_at ≤ charge_at − 24h`. A missing notice **blocks a new debit**.
It does **not** block a retry within a cycle that was already noticed — the notice
attaches to the cycle, not to the attempt — but it does raise
`pre_debit_notice_missing` on the audit row.

**Must-block test.** A new cycle with `notice_sent_at = NULL` is refused; a retry of
an already-noticed cycle proceeds with the warning attached.

### 1.6 Root-cause classification — `compliance/root_cause.py`

**Source.** NPCI's Technical Decline / Business Decline taxonomy. TD is bank, gateway
or network; BD is the customer. Historically ~18% TD, ~82% BD.

**Rule.** A deterministic lookup from Razorpay's own
`(error_code, source, step, reason)` to `TD` / `BD_transient` / `BD_hard`. An unmapped
combination **raises**; it never defaults to a class. Retrying a `BD_hard` — a revoked
mandate, a closed account — burns a legal attempt that can never succeed, so this
classification is worth more than any model output on top of it.

**Must-block test.** Every `(code, source, step, reason)` tuple present in the dataset
maps to exactly one class; an invented tuple raises rather than falling through.

---

## 02 — Verdicts

The guardrail returns exactly one of four values, and nothing else:

| Verdict | Meaning |
|---|---|
| `APPROVE` | Execute now. |
| `REDIRECT_TO_WINDOW` | Legal, but not at this hour. Rescheduled to the attached slot. |
| `ESCALATE_HUMAN` | Above an AFA ceiling, or otherwise not the agent's call. |
| `DENY` | Refused. `stop_reason` names the rule. |

Each carries an `authorizing_rule` string written verbatim into the audit row and
rendered as a chip in the dashboard, e.g.

```
npci_1_plus_3: attempt 2/4 permitted; window ok (next slot 13:40 IST)
```

## 03 — The audit trail

`audit_log` is append-only, defended twice over:

1. A `BEFORE UPDATE OR DELETE` trigger that raises `append_only_violation` — it
   refuses **the superuser too**, which is the layer that answers "what stops you from
   editing the number in your own demo?"
2. Revoked `UPDATE`/`DELETE`/`TRUNCATE` grants for the application role, so the
   application is refused before a trigger is even consulted.

`TRUNCATE` is caught by a separate statement-level trigger; without it the whole
scheme has a one-word bypass. `payment_attempts` and `decisions` are protected the
same way — rewriting an attempt would rewrite both the training data and the
evaluation.

A correction is never an `UPDATE`. A human overturning a decision writes a **new** row
with `supersedes_decision_id` pointing back at the original and `decided_by = 'human'`.

Customer identifiers in `observed_data` are redacted to `sha256(customer_id)[:12]` **at
write time**, not at render time — redaction that happens in the view layer is not
redaction.

See `core/tests/test_append_only.py`: 20 tests, each of which tries to rewrite history
the way a careless or motivated caller would, and asserts the database refuses.

## 04 — What Winback deliberately does not do

- It does not send real SMS, WhatsApp, or voice messages.
- It does not hold a live Razorpay key. `core/config.py` refuses any `RAZORPAY_KEY_ID`
  that does not begin with `rzp_test_`, at startup.
- It does not auto-debit above an AFA threshold.
- It does not decide any of the above with a language model.

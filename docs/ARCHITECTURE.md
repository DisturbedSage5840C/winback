# Architecture

## 01 — The loop

```
  at-risk invoice
        │
        ▼
  root_cause()            deterministic lookup from Razorpay's own error object
        │                 TD | BD_transient | BD_hard  — never a model output
        ▼
  guardrail.candidates()  enumerate LEGAL (action × next 3 valid non-peak slots)
        │                 the compliance layer generates the option set; the model
        ▼                 never gets to consider an illegal action in the first place
  model.score()           calibrated P(success | attempt, action, slot)
        │
        ▼
  policy.argmax()         expected ₹ net of action cost, under the remaining budget
        │
        ▼
  guardrail.approve()     APPROVE | REDIRECT_TO_WINDOW | ESCALATE_HUMAN | DENY
        │                 writes decisions row with candidate_set + authorizing_rule
        ▼
  can_use_tool gate       hard block: no approval on record → tool never executes
        │
        ▼
  adapter.execute()       LiveRazorpayAdapter | SimulatedAdapter
        │
        ▼
  audit_log append        execution_mode, razorpay_entity_id, outcome, stop_reason
```

The ordering matters and is the whole design: **the compliance layer generates the
candidate set before the model ranks it.** The model is never asked to weigh a
retry it is not allowed to make, so there is no path by which a confident prediction
can talk the system into a violation.

## 02 — Two executors, one decision path

There is no Razorpay endpoint that charges a subscription on demand
(see [`WHAT_BROKE.md`](WHAT_BROKE.md)), and test mode moves no real money. So execution
is the only thing that varies:

| | `LiveRazorpayAdapter` | `SimulatedAdapter` |
|---|---|---|
| Scope | Live cohort, ~10–20 subscriptions | Full batch, 500 subscriptions |
| Backing | Real Razorpay test-mode API | Seeded counterfactual oracle |
| Produces | Real `plink_…` / `order_…` / `pay_…` IDs | Deterministic outcomes |
| Answers | "Does this work against the real API?" | "How much money did the policy recover?" |

Both sit behind the same guardrail and write the same audit rows.
`audit_log.execution_mode` records which one ran, per row — so no number in the
dashboard is ambiguous about where it came from.

What the live adapter can genuinely do on a fresh test account, with no S2S activation:
`create_payment_link` / `create_payment_link_upi` (with `notify:{sms:false,
email:false}`), `create_order`, `fetch_payment`, `fetch_all_payments`,
`fetch_order_payments`, `fetch_tokens`. Findings recorded in
[`LIVE_LANE_FINDINGS.md`](LIVE_LANE_FINDINGS.md).

## 03 — The agent layer

One Claude Agent SDK orchestrator run per batch, iterating at-risk invoices. Claude
owns sequencing and explanation; the tools own everything that touches money.

**In-process MCP tools** (`@tool` + `create_sdk_mcp_server`):

| Tool | Does |
|---|---|
| `assess_recoverability` | Root-cause lookup + calibrated probability + candidate set |
| `compliance_guardrail` | The four-verdict gate. Pure functions, no model. |
| `simulated_notify` | Consent-gated nudge on a stubbed channel |
| `execute_recovery` | Adapter façade — the only money-moving tool |

**Razorpay MCP** is mounted alongside, mode-switched between remote (HTTP, Basic auth)
and local (Docker, stdio) via `agent/mcp_config.py`. Never hard-coded, because the
Day-8 failure drill kills the local server mid-run and the batch has to survive it.

**The gate.** `ClaudeAgentOptions(can_use_tool=money_gate)` is the structural block: a
money-moving tool call with no `compliance_guardrail` approval on record returns
`PermissionResultDeny` and never executes. This is enforcement, not prompting — the
agent cannot be talked out of it, because the check does not run inside the model.
A `PostToolUse` hook handles audit-log append only.

`max_turns` is capped per item so a confused loop stops rather than spending.

**An approval is a single-use key, not a flag.** `compliance_guardrail` files each
approval under exact coordinates — `invoice_id | action | execute_at` — and
`execute_recovery` *pops* it. One guardrail call therefore authorises exactly one
presentment, at exactly the slot it was granted for. A near-miss is a miss: approval for a
13:40 slot is not approval for a 12:00 one, and the gate does no rounding, because a
rounded timestamp is how a peak-window presentment gets through.

**The audit trail is the checkpoint.** `audit_log` is append-only and keyed by `run_id`,
so re-running the same `--run-id` resumes rather than restarting — there is no progress
file that could disagree with the evidence. This is a correctness property before it is a
convenience one: NPCI counts presentments, not batches, so a resume that redid its
completed work would spend legal attempts to buy nothing. The resume query reads
`audit_log` **and** `decisions`, because a conclusion is not always an action.

**Every invoice leaves a row — including the ones nothing was done to.** Three paths write
to `audit_log`, and only the first is a hook:

| Path | Writes | Because |
|---|---|---|
| `PostToolUse` | Actions taken, and tool refusals | A tool ran, or refused to |
| `record_conclusion` | Write-offs and escalations | They call no tool; the conclusion *is* the event |
| `record_silence` | Invoices the agent left untouched | No tool ran at all; the absence is the event |

Without the second, a batch that concluded 190 invoices wrote 156 rows and said nothing
about the 34 where the answer was a rule. Without the third, an approval the agent
obtained and never spent was indistinguishable from an invoice never opened. Both are
recorded in [`WHAT_BROKE.md`](WHAT_BROKE.md).

## 04 — Roles

| Role | Used by | Can |
|---|---|---|
| `winback_owner` | Migrations, world regeneration | DDL. Nothing at runtime connects as this. |
| `winback_agent` | Agent, simulator | INSERT facts; UPDATE live state; never rewrite history |
| `winback_reader` | FastAPI backend → dashboard | SELECT only |

A read-only dashboard cannot corrupt an audit trail even if the API has a bug. "Who is
allowed to move money" is answerable from the database, not only from the code.

## 05 — The read API

FastAPI over `winback_reader`. Ten endpoints, all `GET`, and a test asserts that the
set of HTTP verbs the app exposes is a subset of `{GET, HEAD}` — the grant is the
enforcement, that test is the cheap second lock.

| Endpoint | Source of truth |
|---|---|
| `/health` | `core.db.healthcheck` — row counts, not a bare `ok` |
| `/runs` | `audit_log`, grouped. A run exists iff it wrote something unretractable |
| `/runs/{id}/overview` | the `recovery_funnel` view, verbatim, plus a stop-reason breakdown |
| `/runs/{id}/worklist` | `audit_log` ⋈ `exception_worklist` — what the run decided, per invoice |
| `/runs/{id}/events` | `audit_log` ⋈ `decisions`, cursored on `event_id` — the live trace |
| `/worklist` | `exception_worklist` filtered to `at_risk` — the queue still outstanding |
| `/invoices/{id}` | facts, every attempt, every decision with its full `candidate_set`, the trail |
| `/invoices/{id}/compliance` | `compliance/` itself — the rule functions, called |
| `/compliance/window` | `compliance.non_peak_window` — where the clock is, and the countdown |
| `/evaluation` | the four `eval_*` tables — the same rows `EVALUATION.md` is generated from |
| `/config` | the key **id**, execution mode, and the model version that actually scored |

**The compliance panel asks the rules; it does not restate them.**
`/invoices/{id}/compliance` imports `compliance/` and calls the same pure functions the
agent calls, for both a proposed retry and a proposed nudge. A panel that recomputed the
1+3 cap or the peak-window arithmetic in TypeScript would be a second implementation of
the law, free to drift from the one that gates the money — and the screen a reviewer
trusts would then be the copy rather than the original. Nothing it does writes,
schedules, or reserves: it is the guardrail answering a hypothetical at a moment.

**The trace cursors on `event_id`, not on a timestamp.** `audit_log.event_id` is
`BIGSERIAL` and the batch is a single writer, so ids commit in the order they are issued
and a row already sent can never move under the cursor. Two rows can share a microsecond;
a client resuming from `ts_utc` would either skip one or replay it.

**No number is computed here that the database can compute.** An API that recalculated
the funnel would be a second implementation of the evaluation, free to disagree with
`docs/EVALUATION.md`. So the handlers select and shape; they do not aggregate. Every
query is a constant string with bound parameters, including the optional filters, so
nothing a caller sends becomes SQL. Rupees leave as integer paise.

## 06 — Layout

```
winback/
├── core/          config.py · db.py            settings, connections, redaction
├── db/            01_schema · 02_append_only · 03_grants
├── compliance/    the six rule modules + tests  ← read this first
├── sim/           world.py (oracle) · legacy_policy.py · generate.py
├── ml/            features · train · calibrate · evaluate · policy · artifacts/
├── eval/          arms · counterfactual · report
├── agent/         orchestrator · tools · gate · hooks · mcp_config · adapters/
├── api/           main.py                      FastAPI, read-only
├── dashboard/     Next.js
└── scripts/       bootstrap.sh · run_demo.sh
```

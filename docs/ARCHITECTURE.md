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

## 04 — Roles

| Role | Used by | Can |
|---|---|---|
| `winback_owner` | Migrations, world regeneration | DDL. Nothing at runtime connects as this. |
| `winback_agent` | Agent, simulator | INSERT facts; UPDATE live state; never rewrite history |
| `winback_reader` | FastAPI backend → dashboard | SELECT only |

A read-only dashboard cannot corrupt an audit trail even if the API has a bug. "Who is
allowed to move money" is answerable from the database, not only from the code.

## 05 — Layout

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

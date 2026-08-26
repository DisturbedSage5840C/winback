# Live lane findings

> **Status: pending credentials.** No Razorpay account exists yet. This document is
> the protocol for the feasibility spike, to be filled in with results the moment
> test-mode keys are available. Nothing downstream is blocked on it — the batch lane
> runs against the simulator by design, and the live cohort widens or narrows the
> `LiveRazorpayAdapter` surface without changing the architecture.

## 01 — Why this document exists

The build plan assumed retries could be driven through the Subscriptions REST API.
They cannot — there is no charge-on-demand endpoint, and the real primitive
(`POST /v1/payments/create/recurring`) requires S2S Recurring Payments to be activated
on the account by Razorpay support. So the useful question is not "what does the
documentation describe", it is **"what does a fresh test account actually permit
today"**. That is an empirical question and it gets an empirical answer.

## 02 — Prerequisites

1. Sign up at razorpay.com and stay in **Test Mode**.
2. Dashboard → Account & Settings → API Keys → **Generate Test Key**.
3. Put `rzp_test_…` and the secret in `.env`. `core/config.py` refuses at startup any
   key id that does not begin with `rzp_test_`.
4. `RAZORPAY_MCP_MODE=remote` for the handshake test, `local` for the Docker stdio test.

## 03 — The probe matrix

Each row: call it, record the outcome verbatim, do not infer. Sixty minutes, timeboxed.

| # | Call | Expected | Result | Notes |
|---|---|---|---|---|
| 1 | Remote MCP handshake — `https://mcp.razorpay.com/mcp`, `Authorization: Basic base64(key:secret)` | tool list returned | — | |
| 2 | Local MCP — `docker run --rm -i -e RAZORPAY_KEY_ID -e RAZORPAY_KEY_SECRET razorpay/mcp` | tool list returned | — | note whether `create_registration_link` appears (remote-restricted) |
| 3 | `create_order` | real `order_…` | — | |
| 4 | `create_payment_link` with `notify:{sms:false,email:false}` | real `plink_…`, **no message sent** | — | the TRAI-clean nudge primitive |
| 5 | `create_payment_link_upi` | real `plink_…` | — | |
| 6 | `fetch_payment`, `fetch_all_payments`, `fetch_order_payments` | reads succeed | — | the ingest lane |
| 7 | `fetch_tokens` for a customer | list (likely empty) | — | mandate tokens; empty is the expected answer without a completed authorization |
| 8 | `create_subscription` on a test plan | `sub_…` | — | |
| 9 | `POST /v1/payments/create/recurring` | **expected to fail** — S2S not activated | — | record the exact error; this is the evidence for the adapter split |
| 10 | `POST /v1/invoices/:id/notify_by/:medium` | — | — | keep wired but disabled behind the consent gate |
| 11 | `TOOLSETS` / `READ_ONLY` env flags | tool surface changes | — | `READ_ONLY=true` is the safe default for exploration |

## 04 — Decision rule

Whatever passes goes into the live cohort (~10–20 subscriptions) and produces real
entity IDs in `audit_log.razorpay_entity_id`. Whatever fails is recorded here with its
verbatim error and routed to `SimulatedAdapter`. The full 500-subscription batch runs
simulated either way.

If probe 9 unexpectedly **succeeds**, the live lane widens and this document says so —
but the architecture does not change, because the adapter split was never a workaround.

## 05 — Safety rails

- Test mode only, always. A live key is refused at startup.
- `WINBACK_LIVE_CALL_BUDGET` caps how many real API calls a single batch may make; a
  runaway agent loop against a live account is worth a cheap counter.
- Payment links are always created with notifications suppressed. No message reaches a
  real phone or inbox from this system, in any lane.

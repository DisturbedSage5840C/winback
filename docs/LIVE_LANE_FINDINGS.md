# Live lane findings

> **Status: run 2026-08-28** against test account `rzp_test_TUTwFJpAsZY1p5`.
> **7 of 10 probes pass. The live lane is real and larger than expected** — orders,
> payment links (both variants), the full read lane, customer/token endpoints and the
> remote MCP server all work on a fresh, un-activated test account.
>
> The three that fail fail *informatively*, and are the empirical justification for
> the two-adapter architecture. Reproduce with
> `.venv/bin/python -m scripts.probe_live_lane`.

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

| # | Call | Expected | Result | Evidence |
|---|---|---|---|---|
| 1 | Remote MCP handshake — `https://mcp.razorpay.com/mcp`, `Authorization: Basic base64(key:secret)` | tool list returned | **PASS** (200) | `serverInfo: {name: "razorpay-mcp-server", version: "1.0.0"}`, `protocolVersion: 2024-11-05`, advertises `tools`, `resources`, `logging` |
| 2 | Local MCP — `docker run --rm -i … razorpay/mcp` | tool list returned | *deferred* | Day 6, alongside the stdio transport and the MCP-down failure drill |
| 3 | `create_order` | real `order_…` | **PASS** (200) | `order_TUTyn8b44vnlyR` |
| 4 | `create_payment_link`, `notify:{sms:false,email:false}` | real `plink_…`, no message sent | **PASS** (200) | `plink_TUTyngim6AODfr` · `short_url=https://rzp.io/rzp/08B926N` · Razorpay echoed **`notify={email: false, sms: false, whatsapp: false}`** |
| 5 | `create_payment_link_upi` | real `plink_…` | **PASS** (200) | `plink_TUTyobRtWEuVOD` |
| 6 | `fetch_all_payments`, `fetch_order_payments` | reads succeed | **PASS** (200/200) | the ingest lane is fully open |
| 7 | `create_customer` + `fetch_tokens` | list, likely empty | **PASS** (200) | `cust_TUTyqN9FKdjMnO`, `token count = 0` — expected without a completed mandate authorization |
| 8 | `create_subscription` (and `create_plan`) | `sub_…` | **FAIL** (401) | `{"error": "Unauthorized"}` on **both read and write**, on `/plans` *and* `/subscriptions`. `/orders` returns 200 with the same credentials → product-level gating, not an auth problem |
| 9 | `POST /v1/payments/create/recurring` | expected to fail — S2S not activated | **FAIL** (400) | `BAD_REQUEST_ERROR · "The requested URL was not found on the server." · source: internal` |
| 10 | `POST /v1/invoices/:id/notify_by/:medium` | — | **INCONCLUSIVE** (404) | `{"message": "no Route matched with those values"}` — a Kong *gateway* miss, not an app-level unknown-invoice 404. `GET /invoices` returns 200, so the Invoices product itself is available |
| 11 | `TOOLSETS` / `READ_ONLY` env flags | tool surface changes | *deferred* | Day 6, with probe 2 |

### 03.1 — Probe 9 is not a wrong URL, and here is the control

The obvious objection to probe 9 is that `/payments/create/recurring` simply doesn't
exist and I mistyped the route. It does exist — it is Razorpay's documented S2S
recurring endpoint — but the error message alone cannot distinguish "route absent" from
"route not granted to you". So the probe was repeated against a **sibling in the same
route family**:

```
POST /v1/payments/create/upi          → 400 "The requested URL was not found on the server."
POST /v1/payments/create/recurring    → 400 "The requested URL was not found on the server."
GET  /v1/orders?count=1               → 200 (same credentials, same base URL)
```

`/payments/create/upi` is a real, documented S2S endpoint. Both members of the
`/payments/create/*` family return byte-identical errors while `/orders` succeeds on the
same key. **The family is not routed for accounts without S2S Recurring activated.**
That is the activation gate, observed rather than assumed.

### 03.2 — What this changes, and what it does not

**It does not change the architecture.** `LiveRazorpayAdapter` and `SimulatedAdapter`
were never a workaround for a missing key; they exist because "measured money recovered"
is only *measurable* against a counterfactual oracle, and test mode moves no real money
regardless of which endpoints answer.

**It does widen the live lane.** The original assumption was reads plus maybe a payment
link. In fact every non-mandate primitive works, including the one that matters most:

> **Probe 4 is the TRAI-clean nudge, confirmed end to end.** A real `plink_…` with a real
> `rzp.io` short URL, and Razorpay's own response echoing all three channels suppressed.
> The artifact is genuine, the send is stubbed, and the consent gate in front of it is
> the real `compliance/consent_gate.py`. No message reaches a real phone from this
> system in any lane — and now that claim is backed by the API's own response body
> rather than by my assertion.

**Subscriptions being 401 is the more interesting failure.** It confirms the Day-1
finding from a second direction: even with the Subscriptions product enabled there is no
charge-on-demand endpoint, so the product's absence costs Winback nothing it was going
to use. Mandate state is modelled in `subscriptions`/`invoices` locally, which is where
the counterfactual oracle needs it anyway.

## 04 — Decision rule

Applied, given the results above.

| Capability | Lane | Why |
|---|---|---|
| Order creation, all reads, customer/token lookups | **live** | work on a fresh account, cost nothing, produce real ids |
| Payment-link nudge (`plink_…`, notifications suppressed) | **live** | the recovery action a customer actually receives; genuine artifact, stubbed send |
| Mandate presentment / retry | **simulated** | `/payments/create/*` is not routed without S2S activation (probe 9) |
| Subscription lifecycle | **simulated** | product returns 401; and no charge-on-demand endpoint exists even when enabled |
| Real SMS / WhatsApp / voice | **simulated, permanently** | TRAI DLT registration; a compliance decision, not a technical limit |

`audit_log.execution_mode` records `live` or `simulated` per row, so the distinction is
visible in the dashboard and in any exported audit trail rather than buried in a README.

Live cohort: ~10–20 subscriptions carrying real `order_…` / `plink_…` / `cust_…` ids.
The full 500-subscription batch runs simulated, because that is the only lane in which
a counterfactual is defined.

## 05 — Safety rails

- Test mode only, always. A live key is refused at startup.
- `WINBACK_LIVE_CALL_BUDGET` caps how many real API calls a single batch may make; a
  runaway agent loop against a live account is worth a cheap counter.
- Payment links are always created with notifications suppressed. No message reaches a
  real phone or inbox from this system, in any lane.

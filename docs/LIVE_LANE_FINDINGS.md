# Live lane findings

> **Status: run 2026-08-28 (second pass)** against test account `rzp_test_TUTwFJpAsZY1p5`.
> **9 PASS · 1 FAIL · 1 EXPECTED · 0 inconclusive · 0 skipped, of 11 probes.**
> Every open question from the first pass is now closed with evidence.
>
> Reproduce with `.venv/bin/python -m scripts.probe_live_lane`.

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
4. `docker pull razorpay/mcp` for probes 2 and 11.

## 03 — A probe reports five outcomes, not two

This matters enough to state before the matrix, because getting it wrong is how the
first pass produced two wrong rows.

| Outcome | Means | Wired into the live adapter? |
|---|---|---|
| `PASS` | The capability works on this account, observed | yes |
| `FAIL` | Confirmed absent, and the error says why | no |
| `EXPECTED` | Confirmed absent, and that *was* the hypothesis | no — it is the finding |
| `INCONC` | Ran, but the evidence cannot separate two live hypotheses | no, and say so |
| `SKIP` | Did not run. **The default.** | no, and claim nothing |

`SKIPPED` being the default is the load-bearing part: a probe that never executes
cannot be silently scored as a failure, which is exactly what the first pass did to
probes 2 and 11. See `WHAT_BROKE.md`, 2026-08-28.

## 04 — The probe matrix

Each row: call it, record the outcome verbatim, do not infer.

| # | Call | Outcome | Evidence |
|---|---|---|---|
| 1 | Remote MCP handshake — `https://mcp.razorpay.com/mcp`, `Authorization: Basic base64(key:secret)` | **PASS** (200) | `serverInfo: {name: "razorpay-mcp-server", version: "1.0.0"}`, `protocolVersion: 2024-11-05`, advertises `tools`, `resources`, `logging` |
| 2 | Local MCP — `docker run --rm -i … razorpay/mcp`, JSON-RPC over stdio | **PASS** (stdio) | **41 tools.** See §05 — the tool list contradicts the build plan |
| 3 | `create_order` | **PASS** (200) | `order_TUUBry5EFvREqs` |
| 4 | `create_payment_link`, `notify:{sms:false,email:false}` | **PASS** (200) | `plink_TUUBsalUhAItNt` · `short_url=https://rzp.io/rzp/S0QEMEZ` · Razorpay echoed **`notify={email: false, sms: false, whatsapp: false}`** |
| 5 | `create_payment_link_upi` | **PASS** (200) | `plink_TUUBtNddCieg9S` |
| 6 | `fetch_all_payments`, `fetch_order_payments` | **PASS** (200/200) | the ingest lane is fully open |
| 7 | `create_customer` + `fetch_tokens` | **PASS** (200) | `cust_TUUBuwz1S6UqST`, `token count = 0` — expected without a completed mandate authorization |
| 8 | `create_plan` / `create_subscription` | **FAIL** (401) | `{"error": "Unauthorized"}` on **both read and write**, on `/plans` *and* `/subscriptions`. `/orders` returns 200 with the same credentials → product-level gating, not an auth problem |
| 9 | `POST /v1/payments/create/recurring` | **EXPECTED** (400) | `BAD_REQUEST_ERROR · "The requested URL was not found on the server."` — with a control, §04.1 |
| 10 | `POST /v1/invoices/:id/notify_by/:medium` | **PASS** (400) | `"Operation not allowed for Invoice in draft status."` — an *application* refusal, so the route exists. §04.2 |
| 11 | `TOOLSETS` / `READ_ONLY` env flags | **PASS** (stdio) — but see §04.3 | default 41 tools · `READ_ONLY=true` → 25 (16 removed) · `TOOLSETS=orders` → 5 |

### 04.3 — Probe 11 proved less than it looked like it proved (added 3 Sep)

Probe 11 passed exactly one toolset name, `orders`. One value cannot reveal a separator,
and the multi-value string later written into `.env` on the strength of this row was
comma-joined — which the image reads as a single toolset name and refuses to start on.
`RAZORPAY_MCP_MODE=off` is the correct default for the batch, so nothing tried to start
it again for a week. Re-probed properly on 3 Sep, against the same image:

```text
TOOLSETS=(unset)                      → 41 tools
TOOLSETS=orders payments payment_links → 20 tools
TOOLSETS=orders,payments              → exit 1, "toolset orders,payments does not exist"
TOOLSETS=payment-links                → exit 1, not a toolset name (underscore, not hyphen)
TOOLSETS=subscriptions                → exit 1, not a toolset in this image at all
```

**`TOOLSETS` is space-separated and the names use underscores.** The 20-tool set above
contains every tool the live lane uses. Full account in `docs/WHAT_BROKE.md`, 3 Sep.

### 04.1 — Probe 9 is not a wrong URL, and here is the control

The obvious objection to probe 9 is that `/payments/create/recurring` simply doesn't
exist and I mistyped the route. It does exist — it is Razorpay's documented S2S
recurring endpoint — but the error message alone cannot distinguish "route absent" from
"route not granted to you". So the probe now runs a **sibling in the same route family**
automatically, every time, as part of the same pass:

```text
POST /v1/payments/create/upi          → 400 "The requested URL was not found on the server."
POST /v1/payments/create/recurring    → 400 "The requested URL was not found on the server."
GET  /v1/orders?count=1               → 200 (same credentials, same base URL)
```

`/payments/create/upi` is a real, documented S2S endpoint. Both members of the
`/payments/create/*` family return byte-identical errors while `/orders` succeeds on the
same key. **The family is not routed for accounts without S2S Recurring activated.**
That is the activation gate, observed rather than assumed.

### 04.2 — Probe 10, resolved: how to ask without sending

The first pass called `notify_by` on a deliberately nonexistent invoice id and got
Kong's `{"message": "no Route matched with those values"}` — which cannot distinguish
*unregistered path* from *unknown entity*, so it was correctly recorded as inconclusive.

Resolving it required asking about a **real** invoice. But calling `notify_by` on a real
issued invoice would attempt a real delivery to a real contact — the one thing this
project promises never to do. So the probe creates the invoice as a **draft**:

```text
POST /v1/invoices          {"draft": "1", "sms_notify": 0, "email_notify": 0, …}
   → 200  inv_TUUBxXl3aJfobS
POST /v1/invoices/inv_TUUBxXl3aJfobS/notify_by/sms
   → 400  BAD_REQUEST_ERROR · "Operation not allowed for Invoice in draft status."
                            · input_validation_failed
```

A draft cannot be delivered, so the send is impossible by construction rather than by
promise. And the answer is unambiguous: `input_validation_failed` with a message about
*invoice state* is Razorpay's **application** talking, not Kong. The route is registered
and reachable on this account.

Which is a capability Winback deliberately does not use. The point of closing the probe
was to stop carrying an unknown into Day 6, not to unlock a send.

## 05 — The local MCP tool surface, and a plan correction

The build plan (§1.1) listed `create_registration_link` as *"local MCP only — the
correct real action for `BD_hard` mandate failures."* **That tool is not in the image.**

The 41 tools the local server advertises contain **no mandate, subscription,
registration, plan or recurring tool of any kind**. The surface is orders, payments,
payment links, QR codes, refunds, settlements, payouts, and tokens:

```text
capture_payment · close_qr_code · create_instant_settlement · create_order
create_payment_link · create_qr_code · create_refund · fetch_all_instant_settlements
fetch_all_orders · fetch_all_payment_links · fetch_all_payments · fetch_all_payouts
fetch_all_qr_codes · fetch_all_refunds · fetch_all_settlements
fetch_instant_settlement_with_id · fetch_multiple_refunds_for_payment · fetch_order
fetch_order_payments · fetch_payment · fetch_payment_card_details · fetch_payment_link
fetch_payments_for_qr_code · fetch_payout_with_id · fetch_qr_code
fetch_qr_codes_by_customer_id · fetch_qr_codes_by_payment_id · fetch_refund
fetch_settlement_recon_details · fetch_settlement_with_id · fetch_specific_refund_for_payment
fetch_tokens · initiate_payment · payment_link_notify · payment_link_upi_create
resend_otp · submit_otp · update_order · update_payment · update_payment_link · update_refund
```

Three consequences, all decided now rather than on Day 6:

1. **Nothing changes architecturally.** The mandate lane was already simulated because
   of probe 9. The registration-link fallback for `BD_hard` becomes a *simulated* action
   with a real payment link attached, which is what a merchant would actually send
   anyway.
2. **`payment_link_notify` must be excluded from `allowed_tools`.** It is the one tool
   in this list that would deliver a message to a real contact. The consent gate governs
   Winback's own nudge path; a tool the agent can call that bypasses it is a hole. It is
   named here so the Day-6 `allowed_tools` list is written deliberately.
3. **`READ_ONLY=true` is the right default for the ingest agent.** It removes 16 of 41
   tools, `payment_link_notify` among them — a second, independent layer under the
   `can_use_tool` gate rather than a replacement for it.

## 06 — Decision rule

Applied, given the results above.

| Capability | Lane | Why |
|---|---|---|
| Order creation, all reads, customer/token lookups | **live** | work on a fresh account, cost nothing, produce real ids |
| Payment-link nudge (`plink_…`, notifications suppressed) | **live** | the recovery action a customer actually receives; genuine artifact, stubbed send |
| Invoice `notify_by` | **available, unused** | route confirmed reachable (probe 10); Winback never sends |
| Mandate presentment / retry | **simulated** | `/payments/create/*` is not routed without S2S activation (probe 9) |
| Subscription lifecycle | **simulated** | product returns 401; and no charge-on-demand endpoint exists even when enabled |
| Mandate re-registration for `BD_hard` | **simulated + real payment link** | no registration tool exists in either MCP surface (§05) |
| Real SMS / WhatsApp / voice | **simulated, permanently** | TRAI DLT registration; a compliance decision, not a technical limit |

`audit_log.execution_mode` records `live` or `simulated` per row, so the distinction is
visible in the dashboard and in any exported audit trail rather than buried in a README.

Live cohort: ~10–20 subscriptions carrying real `order_…` / `plink_…` / `cust_…` ids.
The full 500-subscription batch runs simulated, because that is the only lane in which
a counterfactual is defined.

## 07 — Safety rails

- Test mode only, always. A live key is refused at startup.
- `WINBACK_LIVE_CALL_BUDGET` caps how many real API calls a single batch may make; a
  runaway agent loop against a live account is worth a cheap counter.
- Payment links are always created with notifications suppressed.
- The probe's own invoice is created as a **draft**, so probe 10 cannot deliver even if
  the route had accepted it.
- MCP credentials reach the container through the environment (`-e NAME`, values never
  on the command line), so they do not appear in `ps` or shell history.

No message reaches a real phone or inbox from this system, in any lane.

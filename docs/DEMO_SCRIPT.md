# Demo script — 5:00

> **Status: firm.** Every id, string and number below was read out of the running
> system on 3–4 Sep and is reproducible from a fresh clone via `scripts/run_demo.sh`.
> Nothing on screen is invented, and §"What is deliberately *not* in this video"
> exists because one beat in the original outline turned out to have no data behind it.

## The shot list

| Time | Beat | On screen | What to have open |
|---|---|---|---|
| 0:00–0:30 | **The problem, quantified.** "UPI Autopay fails on 8–15% of debits versus 2–3% for card mandates. Since August 2025, NPCI caps you at one attempt plus three retries, non-peak only. You cannot brute-force this back." | The failure-rate and retry-cap numbers, nothing else | `docs/COMPLIANCE.md` §01 |
| 0:30–1:00 | **The loop in one sentence.** Compliance generates the legal option set, the model ranks it, the gate executes it, the ledger records it. | Architecture diagram — 10 seconds, no more | `docs/ARCHITECTURE.md` |
| 1:00–2:20 | **The batch, on real rows.** Overview → worklist → drill-down. 190 invoices, ₹3,57,468 recovered, 65 deferred, 22 blocked, 17 failed. Open one drill-down and show the **scored candidate set** — every action×slot the guardrail allowed, priced, with the winner marked. | Overview funnel filling; worklist; the drawer | dashboard on run `batch_v2` |
| 2:20–2:45 | **The live lane is genuinely live.** Same code path, different adapter. Show `plink_…` ids in the audit rows and open one `short_url` in a browser tab. | Worklist filtered to run `live_v2`; a real Razorpay payment link | run `live_v2` |
| 2:45–3:30 | **The refusal.** Compliance page → paste `inv_3890_01` → Evaluate. Four of the five rules approve. The root-cause rule says in words *"TD may be retried"*. The answer is still **DENY**. | The red chip, `stop_reason · npci_1_plus_3_cap_exhausted`, and the expanded five-rule list held long enough to read | `/compliance` |
| 3:30–3:50 | **The drill.** `scripts/failure_drill.sh` — the local MCP is killed mid-batch and the cohort still finishes with a complete audit trail. Say plainly what the drill found the *first* time it was run. | Terminal, two green phases | `docs/WHAT_BROKE.md`, 3 Sep |
| 3:50–4:35 | **Rigour.** Reliability diagram, ECE, the rupee-priced confusion matrix, the four-arm table. Name the simulator limitation here, unprompted, and the observed-vs-censored gap right after it. | Evaluation page | `docs/EVALUATION.md` |
| 4:35–5:00 | **Close.** This is Razorpay's own listed direction — "mandate retry sequencer" — built independently on the public MCP server and the Claude Agent SDK, in ten days. | Repo, one-command demo | `README.md` |

## The 2:45 beat, exactly

This is the best forty-five seconds in the video, so it is written out rather than
summarised. Type `inv_3890_01` into the compliance lookup and leave the moment blank.
What comes back, verbatim from the API on 3 Sep:

```text
NPCI retry cap    DENY     attempt 5 refused, budget of 4 exhausted for this invoice
AFA ceiling       APPROVE  ₹24,846 within the ₹1,00,000 ceiling for mutual_fund_sip
Pre-debit notice  APPROVE  notice sent 39.1h before the debit
Non-peak window   APPROVE  outside peak hours
Root cause        APPROVE  TD may be retried

Retry  DENY    stop_reason · npci_1_plus_3_cap_exhausted
```

The line to say over it: **"The amount is fine. The timing is fine. The notice went out.
The failure is a technical decline, and the rule that classifies it says out loud that
it may be retried. Four of five rules approve, and the answer is still no — because the
budget is spent. That verdict is a pure function; there is no model in it, and there is
no prompt in it. Using an LLM to decide a legal retry cap would be a bug."**

Then expand "Show 5 rule verdicts" and leave it on screen for a beat. That list is the
whole compliance argument in one screenshot.

Three things make this beat honest and worth defending:

- Nothing was staged. `inv_3890_01` is one of **354 invoices** in the live worklist
  that have used all four attempts. Any of them shows the same refusal.
- The panel is not a second implementation. `api/main.py` imports `compliance/` and
  calls the same functions `agent/gate.py` calls; the string on the chip is the same
  string that goes into `decisions.authorizing_rule`.
- It is computed at the moment you ask. The "as of" control beside the invoice box
  supplies the clock, so the same invoice can be asked about at the moment it was
  actually live — which is how a frozen dataset stays answerable without anyone
  editing a row.

## What is deliberately *not* in this video

**There is no red DENY in the streaming agent trace, and I will not fake one.**

All 428 rows in `decisions` carry `guardrail_verdict = APPROVE`. That is not the gate
failing to fire — it is the architecture working as designed. The compliance layer
*generates* the legal option set before the model ranks it, so by the time the agent
proposes an action, an illegal one is not on the menu. The gate is still there, it is
still the hard block, and `agent/tests/test_gate.py` proves it denies; it simply never
had to during the batch.

That is a better story than a red flash, and it is the one to tell:

> "The guardrail said no zero times in this batch. Not because it is weak — because it
> runs *first*. It hands the model a menu of legal actions and the model picks from that
> menu. The gate underneath is the thing that would catch a model that tried to order
> off-menu, and there is a test that proves it does. What you just saw on the compliance
> page is that same function, asked directly."

Similarly not claimed on camera:

- **A mid-run MCP demotion row.** Phase 2 of the drill kills the transport and the batch
  finishes, which is the property that matters. It does not always write an
  `mcp_degraded` row, because the SDK does not surface a dead stdio mount unless a call
  is in flight. The script says so in its own output and `WHAT_BROKE.md` says why.
- **A real mandate retry.** `/v1/payments/create/*` is not routed without S2S Recurring
  activation. Presentments are simulated, and `audit_log.execution_mode` says so on
  every row. Say this in the 2:20 beat rather than letting someone find it.

## The rigour beat, with the numbers already picked

| Claim | Number | Source |
|---|---|---|
| Naive baseline vs Winback, on money | ₹6,39,598 vs ₹6,39,626 — paired interval [−₹2,697, ₹2,781], **contains zero** | `EVALUATION.md` §04 |
| Naive baseline vs Winback, on legality | 66 violations vs **0** — interval [−96, −42], excludes zero | same |
| ₹ per legal attempt | ₹3,045.70 (B) vs **₹3,246.83** (D) | same |
| Legacy policy | ₹5,57,737 recovered, of which only **₹53,490 legally** | same |
| Calibration, where there is data | ECE **0.034** (observed slice, n=2400) | `ml/artifacts/metrics_v1.json` |
| Calibration, where there is none | ECE **0.442** (censored slice, n=85) | same |
| Cost matrix, in rupees | net ₹19,71,345 margin; 52 attempts declined on cost | same |

**Say the limitation before the reliability diagram, not after:** *"D beats B and C inside
this simulator, and the simulator is a model, not the world. The strongest evidence that
I did not tune my way to this is the last row of that table: where the legacy policy
never collected labels, my calibration error is thirteen times worse, and I am showing
you that rather than the slice that flatters me."*

## Non-negotiables

- The red DENY chip on screen long enough to read, with the four APPROVEs beside it.
  The contrast is the point; a lone red chip proves nothing.
- Say out loud: *"using an LLM to decide a legal retry cap would be a bug."*
- State the simulator limitation before anyone asks.
- Say that the guardrail denied nothing during the batch, and why.
- No mocked data anywhere on screen.

## Practicalities

`ffmpeg` is not installed on this machine. Record with QuickTime / macOS screen
capture. Bring the API up with `scripts/run_demo.sh` and confirm the health badge in the
dashboard header is green before recording — a red badge on camera costs more than a
retake. Record the rough cut as insurance before the final take, not after.

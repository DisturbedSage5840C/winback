# Winback

**Recover the money you're legally allowed to.**

A compliance-gated recovery agent for failed subscription and UPI-Autopay debits.
Built for the **Razorpay AI Buildathon 2026, Track 03 — AI Revenue Recovery**, on the
public Razorpay MCP server and the Claude Agent SDK.

---

## 01 — The problem, quantified

UPI Autopay mandates fail on **8–15%** of debits; card mandates fail on 2–3%. The
dominant cause is not fraud or churn, it is an empty account on the wrong day of the
month. Roughly 20 million mandates a month are revoked in India over low balances.

The obvious fix — retry until it works — became illegal on **1 August 2025**. NPCI
circular **OC-215-A** caps a mandate at **one attempt plus three retries** per
invoice, and permits execution **only outside peak hours** (peak IST: 10:00–13:00 and
17:00–21:30). RBI's e-mandate framework adds a 24-hour pre-debit notice and AFA
thresholds; TRAI's TCCCPR rules govern whether you may message the customer at all.

So a merchant has, at most, four chances and a narrow clock. **Which four, and when,
is now a decision worth real money** — and it is exactly the decision nobody is making
deliberately.

## 02 — The thesis

> A naive retry-everything policy recovers money by breaking NPCI's cap. Winback
> recovers comparable money while consuming **fewer legal attempts** and committing
> **zero violations** — and proves it with a paired counterfactual evaluation on a
> held-out cohort.

The headline metric is therefore not rupees recovered. It is **rupees recovered per
legal attempt consumed**, reported next to a compliance-violations-by-arm count. A
policy that wins on raw recovery by burning an illegal fifth attempt has not won
anything a merchant can ship.

## 03 — What is and is not an LLM decision

| Decided by deterministic code | Decided by the model | Decided by Claude |
|---|---|---|
| The 1+3 retry cap | `P(success ∣ attempt, action, slot)` | Which at-risk invoice to work next |
| The peak/non-peak window | Expected rupees per candidate action | How to sequence tools |
| AFA rupee thresholds | | Plain-English explanation of a decision |
| Consent / DND state | | |
| Technical vs business decline classification | | |

Using an LLM to decide a legal retry cap would be a bug. The guardrail is pure
functions with boundary tests, and the agent physically cannot execute a money-moving
tool without an approval from it on record — enforced through the Agent SDK's
`can_use_tool` callback, not through prompting.

## 04 — Status

Day 4 of 10 (26 Aug → 5 Sep 2026). **360 tests passing, 99% coverage on `compliance/`.**

| | |
|---|---|
| ✅ | Pinned environment, Postgres in Docker, full schema |
| ✅ | Append-only audit trail — enforced by grants *and* triggers, 20 passing tests |
| ✅ | Six compliance rules + the composing guardrail — pure functions, written test-first |
| ✅ | Live-lane spike — all 11 probes resolved, no unknowns carried forward ([findings](docs/LIVE_LANE_FINDINGS.md)) |
| ✅ | World simulator + counterfactual oracle — dataset **frozen** at `c32b2b063cd87707`, 4,000 mandates / 33,866 attempts, realism gate 13 PASS / 6 ungraded / 0 FAIL ([data](docs/DATA.md)) |
| ✅ | Calibrated model **v1 frozen** — sigmoid chosen out-of-fold, isotonic disqualified for asserting certainty; test ECE **0.034** where the merchant had data and **0.442** where it did not, and it still ranks correctly there ([evaluation](docs/EVALUATION.md)) |
| ⬜ | Four-arm paired evaluation (Day 5) |
| ⬜ | Agent orchestrator + Razorpay MCP (Day 6) |
| ⬜ | Dashboard (Days 7–8) |

## 05 — Running it

```bash
cp .env.example .env          # no Razorpay credentials needed for the batch lane
docker compose up -d          # Postgres 17 + schema + append-only DDL
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest    # the compliance and audit suites are the thing to read first
```

`scripts/run_demo.sh` (Day 9) runs the whole loop end to end from a fresh clone.

## 06 — Honest limitations

Stated here rather than waiting to be asked.

- **The batch runs against a simulator.** Razorpay test mode moves no real money, so
  "measured money recovered" is only measurable against a counterfactual oracle. The
  live cohort uses real test-mode API calls and records real Razorpay entity IDs, but
  it is small and it does not settle funds. `audit_log.execution_mode` says which lane
  produced every single row.
- **The evaluation is circular to a degree.** Winback beats the baselines *inside a
  world I wrote*. The simulator uses a deliberately different functional form from the
  model that learns it, the training data is censored by a biased legacy policy so the
  model must generalise beyond what it observed, and calibration is reported
  separately on the observed and censored slices — but a simulator is a model, not the
  world, and no amount of care makes that circularity vanish.
- **Messages are simulated, deliberately.** Real SMS/WhatsApp in India requires DLT
  registration under TRAI TCCCPR. The channel is stubbed; the consent and DND gates in
  front of it are real, and payment links are created with `notify: {sms: false,
  email: false}` so a real artifact exists without an unregistered send.

## 07 — Documentation

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The loop, the adapters, the gate |
| [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) | Every rule, its source, and the test that proves it blocks |
| [`docs/DATA.md`](docs/DATA.md) | Schema, the simulator, and how the training data is censored |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | Four-arm results, generated from the database |
| [`docs/LIVE_LANE_FINDINGS.md`](docs/LIVE_LANE_FINDINGS.md) | What the Razorpay API actually permits |
| [`docs/WHAT_BROKE.md`](docs/WHAT_BROKE.md) | The failure log, written as it happened |

---

Independent submission. Not affiliated with or endorsed by Razorpay.

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

## 04 — The result

On the held-out cohort — 190 failed invoices, replayed against the same oracle seeds by
every arm, 10,000-resample paired bootstrap over subscriptions:

| Arm | Policy | Legally recovered | Legal attempts | Violations | ₹ / legal attempt |
|---|---|---:|---:|---:|---:|
| A | Never retry, always escalate | ₹0 | 0 | 0 | — |
| B | Retry everything to the cap, any time | ₹6,39,598 | 210 | **66** | ₹3,045.70 |
| C | Legacy fixed-offset dunning | ₹53,490 | 63 | **120** | ₹849.05 |
| **D** | **Winback** | **₹6,39,626** | 197 | **0** | **₹3,246.83** |

**Against the naive baseline, Winback recovers ₹28 more — on ₹6.4 lakh, with a paired
interval of [−₹2,697, ₹2,781] that contains zero.** The money is a tie, and it is
reported as a tie. What is not a tie is the legality: **66 violations against 0**,
interval [−96, −42], excluding zero. Same money, inside the law, on thirteen fewer
attempts.

Arm C is the interesting one. It recovers ₹5,57,737 in raw rupees — and only **₹53,490**
of that legally, because 81 of its retries land inside a peak window. A merchant running
it today would believe it works.

## 05 — Status

Complete through **Day 8 of 10** (26 Aug → 5 Sep 2026). **612 tests passing, 99%
coverage on `compliance/`.**

| | |
|---|---|
| ✅ | Pinned environment, Postgres in Docker, full schema |
| ✅ | Append-only audit trail — enforced by grants *and* triggers, 20 passing tests |
| ✅ | Six compliance rules + the composing guardrail — pure functions, written test-first |
| ✅ | Live-lane spike — all 11 probes resolved, no unknowns carried forward ([findings](docs/LIVE_LANE_FINDINGS.md)) |
| ✅ | World simulator + counterfactual oracle — dataset **frozen** at `c32b2b063cd87707`, 4,000 mandates / 33,866 attempts, realism gate 13 PASS / 6 ungraded / 0 FAIL ([data](docs/DATA.md)) |
| ✅ | Calibrated model **v1 frozen** — sigmoid chosen out-of-fold, isotonic disqualified for asserting certainty; test ECE **0.034** where the merchant had data and **0.442** where it did not, and it still ranks correctly there ([evaluation](docs/EVALUATION.md)) |
| ✅ | Four-arm paired counterfactual evaluation — 10,000-resample cluster bootstrap, design frozen before results existed |
| ✅ | Agent orchestrator on the Claude Agent SDK — `can_use_tool` money gate, append-only audit hooks, Razorpay MCP mode switch, both execution adapters. Batch **190/190 unattended**; the live cohort carries real `plink_…` IDs |
| ✅ | Dashboard — overview, worklist, drill-down, compliance panel, evaluation. No mocked data anywhere |

## 06 — Running it

Two commands from a fresh clone:

```bash
./scripts/bootstrap.sh    # venv, pinned deps, Postgres 17, schema, append-only DDL
./scripts/run_demo.sh     # seed → model → batch → API on :8000 → dashboard on :8443
```

`bootstrap.sh` writes `.env` from `.env.example` for you; **no Razorpay credentials are
needed** for the batch lane. `run_demo.sh --no-ui` runs the backend alone, and
`--reseed` rebuilds the world from the frozen seed — it is the only thing in this
repository that will delete an audit trail, which is why it is opt-in.

The recovery batch is the one stage that needs a Claude credential: `claude_agent_sdk`
drives the `claude` CLI as a subprocess, so either that binary is on `PATH` and signed
in, or `ANTHROPIC_API_KEY` is exported. `run_demo.sh` checks for this *before* seeding
rather than failing three minutes in. Seed, training, evaluation, the API and the
dashboard all run without one.

The dashboard installs with **`npm install`** — that is the tested path. A
`pnpm-lock.yaml` is committed too, but pnpm is not installed on the build machine and
that route is unverified; if you have a preference, npm is the one with evidence
behind it.

**This was checked, not assumed — and it failed the first time.** On 4 September this
repository was cloned from GitHub into an empty directory on a machine with no Winback
state, bootstrapped, and run. 11 tables and the append-only DDL loaded, the dataset
regenerated to the same fingerprint `c32b2b063cd87707` and the same 33,866 attempts, the
dashboard installed and built clean, and `python -m eval.report --check` confirmed the
committed `docs/EVALUATION.md` is byte-for-byte what that fresh database generates.

Then the full suite ran in the clone and three tests failed that pass here. The demo
script was seeding through a duplicate loader that never wrote the `world_manifest` row,
so the batch's own `require_fingerprint()` guard would have killed `run_demo.sh` three
stages in — on every machine except this one, which was loaded by the correct path back
on Day 5 and had been sailing through the guard ever since. The duplicate is deleted and
the fix is verified against the clone's database, where it actually failed. The whole
entry is [in the log](docs/WHAT_BROKE.md); it is the clearest thing in this repository
about why "it works on my machine" is not a test.

## 07 — What broke

[`docs/WHAT_BROKE.md`](docs/WHAT_BROKE.md) has **42 entries**, written as they happened
rather than reconstructed at the end. Four worth reading, because they are the four
kinds of mistake this project actually made:

- **`allowed_tools` auto-approves before the money gate is consulted.** The whole
  security claim rests on `can_use_tool` refusing unapproved spend — and listing a
  money-moving tool in `allowed_tools` short-circuits that callback entirely. The gate
  was real and the configuration quietly bypassed it. The money tools are now
  deliberately absent from `allowed_tools`, and an approval is a single-use key at exact
  coordinates, popped rather than read.
- **The censored slice was easier because my code made it easier.** The
  observed-vs-censored calibration gap is a headline claim, and for a while it was
  flattering me: the censored rows were being constructed in a way that made them
  simpler to predict. A finding that makes your own result look better is the one to
  distrust first.
- **The window rule wrote the wrong hour into the permanent record.** It formatted the
  time in whatever timezone the caller passed and appended the literal string "IST". The
  *verdict* was never wrong — but that sentence is copied verbatim into
  `decisions.authorizing_rule` and shown on the compliance chip, so every API lookup
  recorded an hour 5h30m off, about the one rule that is entirely about the hour. Every
  test fixture was IST-aware, which is exactly why nothing caught it.
- **The demo script called a duplicate loader that never wrote the manifest row.** Found
  on the last day, by the clean-checkout test, three stages into the one command this
  README tells you to run. It could not fail here — this machine's database was loaded
  by the correct path on Day 5 — and it would have failed on yours. "Works on my machine"
  is not a test, and this is the entry that proves it.

## 08 — Honest limitations

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

## 09 — Documentation

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The loop, the adapters, the gate |
| [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) | Every rule, its source, and the test that proves it blocks |
| [`docs/DATA.md`](docs/DATA.md) | Schema, the simulator, and how the training data is censored |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | Four-arm results, generated from the database |
| [`docs/LIVE_LANE_FINDINGS.md`](docs/LIVE_LANE_FINDINGS.md) | What the Razorpay API actually permits |
| [`docs/WHAT_BROKE.md`](docs/WHAT_BROKE.md) | The failure log, written as it happened |
| [`docs/FRONTEND_SPEC.md`](docs/FRONTEND_SPEC.md) | The dashboard contract — pages, endpoints, palette, motion budget |
| [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) | The five-minute video, shot by shot, with what is deliberately not claimed |
| [`COMPLETION_REPORT.md`](COMPLETION_REPORT.md) | Live build status against the ten-day plan — what is done, what is left, and who owns it |

---

Independent submission. Not affiliated with or endorsed by Razorpay.

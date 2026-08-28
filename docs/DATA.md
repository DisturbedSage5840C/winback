# Data

The dataset is **frozen**: `Winback dataset v1 (f04fd87f6eb050fa)`. The fingerprint is
a content hash over every generated row, pinned in `sim/tests/test_generate.py`. Change
a world constant and that test fails — which forces the realism gate to be re-run and
this document updated in the same commit, instead of the headline numbers quietly
drifting away from the prose describing them.

| Quantity | Value |
|---|---|
| customers | 4,000 |
| subscriptions | 4,000 — train 2,400 / calibrate 800 / test 800 |
| invoices | 30,210 |
| payment attempts | 34,764 — 33,078 observed, 1,686 censored (4.8%) |
| first-charge failure rate | 8.82% over 30,210 debits |
| invoice outcomes | 27,545 paid · 1,648 recovered · 860 written off · 157 at risk |
| revenue at risk | ₹5,63,260 |
| as of | 2026-08-24 IST |

Regenerate and load with `.venv/bin/python -m sim.generate --load`; verify with
`.venv/bin/python -m sim.validate_realism`.

## 01 — Schema

Eight tables and two views in `db/01_schema.sql`, split along one line: **facts are
immutable, state is not.**

| Table | Kind | Notes |
|---|---|---|
| `customers` | state | `customer_hash` = `sha256(customer_id)[:12]`, the only identifier permitted into `audit_log` |
| `subscriptions` | state | `cohort ∈ {train, calibrate, test}` frozen at generation, before any model exists |
| `invoices` | state | One row per billing cycle. Both revenue-at-risk and the 1+3 budget are scoped to it. |
| `payment_attempts` | **immutable** | Observational history (`run_id IS NULL`) *and* evaluation-arm attempts, one table |
| `decisions` | **immutable** | Includes `candidate_set` — every scored option, not just the winner |
| `audit_log` | **immutable** | Append-only, `execution_mode` per row |
| `eval_runs` / `eval_arm_results` | regenerated | So `EVALUATION.md` is generated from the database, never hand-typed |
| `exception_worklist` / `recovery_funnel` | views | What the dashboard reads |

Three conventions that are load-bearing:

- **Money is always paise, always `BIGINT`.** No float touches a rupee value anywhere in
  this system.
- **IST wall-clock is a generated column**, not application arithmetic. Every NPCI rule
  here is expressed in IST wall-clock, and re-deriving it at two call sites is how two
  call sites drift apart. `attempted_at_ist`, `charge_at_ist` and `ts_ist` are computed
  by Postgres from the `TIMESTAMPTZ`.
- **The 1+3 cap is a `CHECK` constraint**, not only a Python rule:
  `attempt_number CHECK (BETWEEN 1 AND 4)`. A policy bug that proposes a fifth attempt
  fails to persist even if every layer above it were wrong.

Two invariants pair columns that must agree:

```sql
CHECK ((run_id IS NULL) = (arm IS NULL))          -- history xor evaluation, never half
CHECK (observed = (censoring_reason IS NULL))     -- no unexplained hole in the training data
```

## 02 — The simulator is a counterfactual oracle

`sim/world.py` implements a **structural** hazard model — deliberately a different
functional form from the gradient-boosted tree that will try to learn it, so the
evaluation is not a model checking its own homework:

```
p_success(attempt) = clip(
      base(method, bank)                # UPI Autopay 8-15% failure, card mandate 2-3%
    × f_rootcause(TD | BD_transient | BD_hard)
    × g_attempt(attempt_number)         # TD decays slowly; BD_hard ≈ 0
    × h_window(is_non_peak)             # congestion — applies to TD only
    × balance_process(customer, date)   # salary cycle: balance replenishes days 1-7
    × k_recency(days_since_last_success),  0, 1)
```

`balance_process` is the realism that matters. `insufficient_funds` retries succeed far
more often just after payday, which is the actual mechanism behind India's UPI-Autopay
failures — and it gives the model a **genuine timing signal to discover** rather than a
coefficient to memorise.

**Determinism.** Every outcome is drawn from a seed derived from
`sha256(channel, subject_id, invoice_id, attempt_number, action, slot)`, stored in
`payment_attempts.oracle_seed`. The key contains **no `run_id` and no `arm`** — that
omission is the whole design. The coin flip for any `(attempt, action, slot)` is fixed
regardless of which policy asks for it, which buys two things:

- a full counterfactual over actions that were never taken;
- **paired** comparison across policy arms — the same coin flips, so the difference
  between arms is policy and nothing else, and a paired bootstrap is legitimate.

## 03 — The training data is censored, on purpose

`sim/legacy_policy.py` generates the *historical* dataset the model trains on, the way a
crude pre-2025 merchant would have:

> retry at fixed T+1 / T+2 / T+3 at 09:00 IST — **but only if `amount > ₹500` and
> `method != netbanking`**; above ₹2,000 take the "chase it hard" branch instead, at
> 11:30 IST.

Every clause is a decision someone plausibly made, and each has a consequence:

| Clause | Why it was set | What it does to the data |
|---|---|---|
| `amount > ₹500` | "don't waste a gateway call on small invoices" | **1,107 censored retries** — bias on a variable correlated with the outcome |
| `method != netbanking` | the retry job was never extended to a late-onboarded rail | **579 censored retries** — not a policy at all, just a gap nobody closed |
| 11:30 IST urgent branch | legal when it was written; NPCI moved in Aug 2025 | countable peak-window **violations** in arms B and C |
| 09:00 IST standard branch | so a human could watch the run | legal under OC-215-A by accident, not by design |

Censored retries are materialised with `observed = FALSE` and a `censoring_reason`, and
— the load-bearing part — **their outcomes are discarded when deciding the invoice's
status.** In real history those retries never happened, so a shadow retry the oracle says
would have captured must not turn an unpaid invoice into a recovered one. Letting it
would hand the model a label it could never have seen and inflate every arm at once.

**The censored region is genuinely different, and measurably so.** Mean oracle
`p(success)` is **75.6% on the retries the legacy filters suppressed** versus **58.2% on
the retries it actually made**. The suppressed region is the *easier* one — small
invoices and a rail with lower balance exposure — which is exactly the bias a value
floor produces. That gap is what makes the Day-4 observed-vs-censored calibration split
a real test rather than a formality.

## 04 — Splits

Held out **by `customer_id` and by time**: train 2,400 earliest, calibrate 800, test 800
latest, ordered by mandate start (train ≤ 2026-01-24, test ≥ 2026-04-25). No customer
straddles two splits and no future information leaks backwards. The test set is frozen
before any tuning and scored **once**.

A time-ordered split makes the test cohort structurally the thinnest: the newest mandates
have had the fewest billing cycles to fail in. That is a property of the design, not a
defect of it — but it sets the floor on how large the population has to be, and §08
records what that floor turned out to be.

| Cohort | Subscriptions | Invoices | Observed retries | Failed first charges |
|---|---|---|---|---|
| train | 2,400 | 23,626 | 2,262 | 2,025 |
| calibrate | 800 | 4,378 | 415 | 403 |
| test | 800 | 2,206 | 196 | 237 |

## 05 — Realism gate

`sim/validate_realism.py` is a gate, not a report: 16 checks, each carrying its source,
its `n`, and its band. It exits non-zero if any graded check falls outside its band.
Current state — **PASS = 13, REPORT = 3, FAIL = 0**.

![Realism gate](assets/realism.png)

Three checks are deliberately **ungraded** and printed as `[REPORT]`: the netbanking
failure rate, the hard/transient split within business declines, and the censoring
breakdown. There is no published figure worth quoting for any of them. Grading them
would mean inventing a band and then tuning the world until it hit — which is how a
simulator stops being evidence of anything.

| Check | Measured | Band | Source |
|---|---|---|---|
| UPI Autopay failure rate | **11.79%** (n=18,735) | 8-15% | industry reporting on UPI Autopay mandates |
| Card mandate failure rate | **2.75%** (n=9,100) | 2-3% | card e-mandate rates run far below UPI |
| Netbanking failure rate | 8.72% (n=2,375) | *ungraded* | no citable published figure |
| Technical declines / all declines | **17.38%** (n=4,286) | 14-22% | NPCI TD/BD taxonomy, ~18% technical |
| Business declines / all declines | **82.62%** (n=4,286) | 78-86% | complement of the above |
| Hard / business declines | 42.33% (n=3,541) | *ungraded* | finer than NPCI's own split |
| UPI balance failures across salary cycle | **6.77% → 15.05% (2.2×)** | ≥ 2.0× | mechanism, must be learnable |
| Cards feel the cycle less than UPI | **card 1.3× vs UPI 2.2×** | card < UPI | a credit line absorbs what an empty account cannot |
| Technical declines in peak hours | **1.97% → 5.42%** | peak > off-peak | OC-215-A rations peak because peak is congested |
| Recoveries after a hard decline | **0 of 1,109** | exactly 0 | revoked / expired / closed are terminal |
| Observed vs censored `p(success)` | **58.2% vs 75.6%** | differ ≥ 3pp | the filters select on outcome-correlated variables |
| Population straddles ₹500 / ₹2,000 / ₹15,000 | 962/3,038 · 2,431/1,569 · 3,768/232 | ≥ 20 each side | every threshold must be exercised |
| Cohorts ordered in time | train ≤ 2026-01-24, test ≥ 2026-04-25 | strict order | no leakage backwards |

## 06 — Every tuning pass, in full

There have been two. Both are recorded here, including the one that revealed the first
had been partly wrong, because a simulator whose fitting history is undisclosed is not
evidence — it is a number generator with a citation list.

**The governing rule, in both passes: a constant must be mechanistically justifiable
independently of the number it was aimed at.** If it can only be defended by its target,
it is a fitted parameter wearing a mechanism's clothes, and the gate it passes proves
nothing. The corollary is stricter and matters more: **the bands never move.** A band
relaxed because the world missed it is not a gate.

### Pass 1 — at 500 subscriptions

The first generated world missed two bands, and two constants were set to close them.

**`balance_exposure`** — how much of an empty account each rail actually feels:

```python
("upi_autopay", 1.00)    # debits a savings account directly, right now
("netbanking",  0.90)    # same account, but batch-presented and retried within the day
("card_mandate", 0.05)   # a credit line absorbs the debit; the balance is not the binding constraint
```

**`authorization_base`** — per-cycle authorization *surfaces*, each of which can fail:

```python
("upi_autopay", 0.013)   # resolve a VPA + reach a PSP + look up the mandate at NPCI = 3
("netbanking",  0.009)   # reach the bank + look up the mandate = 2
("card_mandate", 0.006)  # AFA happened once at registration; one network authorization = 1
```

The ordering 3 > 2 > 1 is a count of things that exist, not a ranking chosen to produce
`0.013 > 0.009 > 0.006`. The constants were read off that count, and the resulting failure
rates landed inside the cited bands — in that order, not the reverse.

### Pass 2 — at 4,000 subscriptions, after the population grew

Raising the population for the reason in §08 moved two measurements outside their bands:
the card failure rate to 3.03% and the UPI salary-cycle ratio to 1.96×. Neither band was
touched. What had actually happened is that **two constants had been fitted to
small-sample noise**: the card rate was read at n≈1,000 attempts, where its standard error
was wide enough to sit inside a 1-point band by luck. At n≈9,100 it reads stably. Re-reading
a magnitude at the sample size where the estimate is stable is calibration; moving the band
it is measured against would not be.

Five constants changed, each staying inside the mechanism it already claimed:

| Constant | From | To | The mechanism it still has to obey |
|---|---|---|---|
| `balance_exposure["card_mandate"]` | 0.10 | 0.05 | still far below netbanking's 0.90 and UPI's 1.00 — a credit line, not an account |
| `authorization_base["card_mandate"]` | 0.007 | 0.006 | still the smallest of the three; the surface count 3 > 2 > 1 is unchanged |
| `balance_floor` | 0.012 | 0.005 | an account credited this morning should decline a ₹399 debit rarely |
| `balance_ceiling` | 0.185 | 0.25 | depletion depth at the end of the cycle — the payday signal's amplitude |
| `technical_base` (all three rails) | ×1.20 | | TD share is a *share*; deepening the balance hazard dilutes it, so these were rescaled to keep holding the documented ~18% |

The last row is the one worth reading twice. `technical_base` is *defined* as whatever
value holds the TD/BD ratio at NPCI's ~18%, so rescaling it when the denominator changed
is following the mechanism, not evading it — and it is the reason the TD share reads 17.4%
rather than drifting to the 14.8% the deeper balance hazard alone would have produced.

After both passes every graded check sits mid-band rather than against an edge, which is
the point: a world that only passes by a tenth of a point is one reseed away from failing,
and that fragility would itself be evidence the constants were fitted.

Cards being flatter across the salary cycle is asserted by the gate, and a card hazard
pinned at zero would satisfy that assertion trivially. So a separate test
(`test_a_card_still_feels_the_cycle_in_the_same_direction`) asserts cards still track the
cycle in the same direction — flatter, not flat.

## 07 — What the worklist contains

The 157 at-risk invoices were not curated; they are what the legacy policy left behind.
They contain one of every case the compliance layer exists to handle, which is why the
Day-6 demo runs on real rows rather than a fixture:

| Case | Count | Example | What must happen |
|---|---|---|---|
| Above the RBI AFA ceiling | 10 | `inv_0090_08`, ₹18,392 netbanking | escalate to a human, never auto-debit |
| Attempt budget exhausted | 5 | `inv_0488_03`, 4 of 4 used | the 5th attempt is **blocked** — the demo's proof shot |
| Consent withdrawn or DND | 24 | `inv_0132_09` (withdrawn) | every nudge channel blocked regardless of model score |
| Hard decline | 32 | `inv_0190_13`, `card_expired` | re-register the mandate; retrying is known-futile |
| Budget remaining, transient | 125 | — | the actual recovery opportunity |

Distribution of remaining budget: 129 invoices with 3 attempts left, 15 with 2, 8 with 1,
and 5 with none. The categories overlap on purpose — `inv_2757_03` is ₹24,772 *and*
consent-withdrawn, and an invoice that trips two rules is where a guardrail composed of
independent gates is worth more than a decision tree.

## 08 — Scale and population

| Rail | Subscriptions | Min | Mean | Max |
|---|---|---|---|---|
| UPI Autopay | 2,511 | ₹100 | ₹3,451 | ₹59,778 |
| Card mandate | 1,087 | ₹102 | ₹3,833 | ₹57,501 |
| Netbanking | 402 | ₹100 | ₹3,286 | ₹46,528 |

Attempts by number: 30,210 first charges, then 2,536 / 1,104 / 914 retries. The taper is
the dunning funnel — most invoices are settled or abandoned before the budget runs out.

Amounts are spread so that **every threshold the compliance layer knows about is
exercised on both sides**: the ₹500 legacy value floor (962 below / 3,038 above), the
₹2,000 urgent branch (2,431 / 1,569), and the ₹15,000 RBI AFA ceiling (3,768 / 232). A
rule whose boundary no row ever crosses is untested in practice however many unit tests
it has.

**Why 4,000 and not 500.** The population was sized by the *test* cohort, not by the
total. The split is ordered in time, so the test cohort holds the newest mandates — the
ones with the fewest billing cycles behind them, and therefore the fewest failures. At 500
subscriptions it contained **20 observed retries and 24 failed first charges**. The Day-5
headline claim is "measured money recovered across a batch"; resting it on 24 invoices
would have produced a paired bootstrap interval wide enough to cover all four arms, which
is a result that cannot distinguish the submission from the baseline it is meant to beat.

At 4,000 the same cohort holds **196 observed retries and 237 recovery opportunities** —
roughly ten times the evidence, for 2.3 seconds of generation. The cost of finding this
on Day 5 instead of Day 4 would have been the entire evaluation.

## 09 — Regenerating

```bash
.venv/bin/python -m sim.generate           # build in memory, print the summary
.venv/bin/python -m sim.generate --load    # calls winback_reset_world(), then loads
.venv/bin/python -m sim.validate_realism   # gate + regenerate docs/assets/realism.png
```

Regeneration is the one operation that legitimately removes immutable rows — it is
rebuilding history, not editing it. It routes through the `winback_reset_world()`
`SECURITY DEFINER` function rather than through a `DELETE` grant, so the escape hatch is
a single named, greppable, log-emitting call instead of an ambient permission.

Everything else is genuinely append-only, and this was verified against the loaded
dataset rather than assumed:

| Attempt | Role | Result |
|---|---|---|
| `UPDATE`/`DELETE` on `audit_log` | `winback_agent` | `permission denied` — the grant layer |
| `UPDATE`/`DELETE` on `audit_log` | `winback_owner` | `append_only_violation` — the trigger, which the table's own owner cannot bypass |
| `TRUNCATE audit_log` | `winback_owner` | `append_only_violation` — caught by a separate statement-level trigger |
| `UPDATE` on `payment_attempts` | `winback_agent` | `permission denied` |

The owner case is the one that matters. A trigger the owner can bypass is a convention;
a trigger that refuses the owner is a control.

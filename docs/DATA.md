# Data

The dataset is **frozen**: `Winback dataset v1 (c32b2b063cd87707)`. The fingerprint is
a content hash over every generated row, pinned in `sim/tests/test_generate.py`. Change
a world constant and that test fails — which forces the realism gate to be re-run and
this document updated in the same commit, instead of the headline numbers quietly
drifting away from the prose describing them.

| Quantity | Value |
|---|---|
| customers | 4,000 |
| subscriptions | 4,000 — train 2,400 / calibrate 800 / test 800 |
| invoices | 30,210 |
| payment attempts | 33,866 — 33,080 observed, 786 censored (2.3%) |
| first-charge failure rate | 8.82% over 30,210 debits |
| invoice outcomes | 27,545 paid · 1,650 recovered · 860 written off · 155 at risk |
| revenue at risk | ₹5,60,124 |
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
| `amount > ₹500` | "don't waste a gateway call on small invoices" | **531 censored retries** — bias on a variable correlated with the outcome |
| `method != netbanking` | the retry job was never extended to a late-onboarded rail | **255 censored retries** — not a policy at all, just a gap nobody closed |
| 11:30 IST urgent branch | legal when it was written; NPCI moved in Aug 2025 | countable peak-window **violations** in arms B and C |
| 09:00 IST standard branch | so a human could watch the run | legal under OC-215-A by accident, not by design |

Censored retries are materialised with `observed = FALSE` and a `censoring_reason`, and
— the load-bearing part — **their outcomes are discarded when deciding the invoice's
status.** In real history those retries never happened, so a shadow retry the oracle says
would have captured must not turn an unpaid invoice into a recovered one. Letting it
would hand the model a label it could never have seen and inflate every arm at once.

**The censored region is different, but not in the way a headline rate would show.**
Mean oracle `p(success)` is **61.8% on the retries the legacy filters suppressed** versus
**58.2% on the retries it actually made** — a 3.6-point gap, and most of that is mix
rather than difficulty. Held at a fixed attempt number the two slices are close, and at
the fourth attempt the censored slice is the *harder* one:

| Attempt | Observed | Censored | n (obs / cens) |
|---|---|---|---|
| 2 | 72.0% | 75.5% | 1,973 / 562 |
| 3 | 36.7% | 38.2% | 543 / 138 |
| 4 | 14.6% | 10.5% | 354 / 86 |

The two filter variables were then priced directly, on **first charges** — the one
population no legacy filter touches, so the comparison is not conditioned on the
selection being measured. Crossing the ₹500 floor is worth **+1.0 points** of
`p(success)` (92.1% below vs 91.1% above, n=5,688/24,522); netbanking versus UPI Autopay
is worth **+3.4 points** (91.7% vs 88.3%, n=2,375/18,735). The value floor barely moves
the outcome at all; the rail exclusion moves it, and it was never a policy in the first
place.

So the bias is **not in the marginal — it is in the covariates.** The censored region is
cheap, netbanking, and early in a mandate's life, and the observed data contains almost
none of that combination. That is the harder version of the selection problem, not the
easier one: a merchant watching their recovery rate would see nothing wrong, because
there is nothing wrong with the rate. What is wrong is that the model has no evidence
about a corner of the space it will be asked to act in. Day 4 measured the consequence —
ECE **0.4420** on the censored slice against **0.0342** on the observed one, uniformly
pessimistic, and still correctly ordered. See `EVALUATION.md` §06.

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
| train | 2,400 | 23,626 | 2,263 | 2,025 |
| calibrate | 800 | 4,378 | 413 | 403 |
| test | 800 | 2,206 | 194 | 237 |

`ml/dataset.py` turns those cohorts into feature matrices, one row per attempt: **train
25,889 · calibrate 4,791 · test 2,400**, plus **118 / 85** censored rows held out of both
fits and used only to measure the model against the oracle. Censored rows never enter a
training or calibration matrix — `ml/tests/test_features.py` asserts it, because a
counterfactual row and a real row live in the same table and every consumer has to
declare which one it means.

## 05 — Realism gate

`sim/validate_realism.py` is a gate, not a report: 19 checks, each carrying its source,
its `n`, and its band. It exits non-zero if any graded check falls outside its band.
Current state — **PASS = 13, REPORT = 6, FAIL = 0**.

![Realism gate](assets/realism.png)

Six checks are deliberately **ungraded** and printed as `[REPORT]`: the netbanking
failure rate, the hard/transient split within business declines, the censoring
breakdown, and the three that decompose the observed-vs-censored gap. There is no
published figure worth quoting for any of them. Grading them would mean inventing a band
and then tuning the world until it hit — which is how a simulator stops being evidence
of anything. The file's own rule is **no band without a source**, and it is enforced by
being cheaper to obey than to argue with: an ungraded check still prints its number.

| Check | Measured | Band | Source |
|---|---|---|---|
| UPI Autopay failure rate | **11.79%** (n=18,735) | 8-15% | industry reporting on UPI Autopay mandates |
| Card mandate failure rate | **2.75%** (n=9,100) | 2-3% | card e-mandate rates run far below UPI |
| Netbanking failure rate | 8.72% (n=2,375) | *ungraded* | no citable published figure |
| Technical declines / all declines | **17.38%** (n=4,184) | 14-22% | NPCI TD/BD taxonomy, ~18% technical |
| Business declines / all declines | **82.62%** (n=4,184) | 78-86% | complement of the above |
| Hard / business declines | 43.36% (n=3,457) | *ungraded* | finer than NPCI's own split |
| UPI balance failures across salary cycle | **6.77% → 15.05% (2.2×)** | ≥ 2.0× | mechanism, must be learnable |
| Cards feel the cycle less than UPI | **card 1.3× vs UPI 2.2×** | card < UPI | a credit line absorbs what an empty account cannot |
| Technical declines in peak hours | **1.97% → 5.73%** | peak > off-peak | OC-215-A rations peak because peak is congested |
| Recoveries after a hard decline | **0 of 1,109** | exactly 0 | revoked / expired / closed are terminal |
| Censored retries, by reason | 786 rows: value floor 531, rail 255 | *ungraded* | a count, not a rate |
| Observed vs censored `p(success)` | **58.2% vs 61.8%** | differ ≥ 3pp | the filters select on outcome-correlated variables |
| …how much of that gap is mix | per attempt: 72.0/75.5 · 36.7/38.2 · 14.6/10.5 | *ungraded* | most of it; see §03 |
| …what the ₹500 floor is worth | 92.1% vs 91.1% (+1.0pp, n=5,688/24,522) | *ungraded* | measured on first charges, which no filter touches |
| …what the rail exclusion is worth | 91.7% vs 88.3% (+3.4pp, n=2,375/18,735) | *ungraded* | same, for netbanking vs UPI Autopay |
| Population straddles ₹500 / ₹2,000 / ₹15,000 | 962/3,038 · 2,431/1,569 · 3,768/232 | ≥ 20 each side | every threshold must be exercised |
| Cohorts ordered in time | train ≤ 2026-01-24, test ≥ 2026-04-25 | strict order | no leakage backwards |

**One graded check now sits near its edge, and it is the interesting one.** The
observed-vs-censored gap reads 3.6 points against a ≥3-point band. Earlier in the build
it read 17.4 points, and that was an artefact: shadow retries were being scheduled by a
different code path from real ones, so the two slices were not comparable populations.
Fixing that (§06, pass 3) collapsed the gap to something close to parity. The band was
**not** lowered to accommodate it. Instead the three decomposition rows above were added,
because a single marginal-rate check was the wrong instrument for the claim being made:
the selection bias here lives in the covariates, and a check on the marginal cannot see
it. If that check ever fails, the correct response is to read §03 and the calibration
result in `EVALUATION.md` §06 — not to touch the band.

## 06 — Every tuning pass, in full

Two passes moved constants, and a third moved none but re-froze the dataset anyway. All
three are recorded here, including the one that revealed the first had been partly wrong,
because a simulator whose fitting history is undisclosed is not evidence — it is a number
generator with a citation list.

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

### Pass 3 — no constant moved, and the headline number halved anyway

Two generator bugs were found on Day 4, both by tests written for the *model* rather than
for the world. Neither touched a world constant; both changed which rows exist. The
fingerprint went from `f04fd87f6eb050fa` to `c32b2b063cd87707`, attempts from 34,764 to
33,866, and censored attempts from 1,686 to 786.

**The shadow schedule ran a different control flow from the real one.** The observed
branch stops presenting debits at the first capture, because a merchant who has been paid
does not keep debiting. The censored branch did not: it materialised the whole
counterfactual schedule unconditionally, so an invoice whose shadow retry #2 captured
still got shadow retries #3 and #4 — attempts nobody would ever have made, drawn under
exactly the conditions that had just succeeded. That is what produced the
**75.6% vs 58.2%** observed-vs-censored gap this document reported until Day 4. It was
not a finding about the legacy filters; it was an artefact of two code paths that were
supposed to be the same policy. With the branch corrected, the honest gap is 3.6 points,
and §03 is what replaced the paragraph that quoted the old one.

**A retry could precede the charge it was retrying.** The urgent branch fires at 11:30
IST with a T+0 offset, so an invoice charged at 15:00 got its "same-day" retry at 11:30
that morning — three and a half hours before the failure it was responding to. 122
attempts dataset-wide. `retry_schedule` now rolls a slot forward whole days until it is
strictly after both the charge and the previous retry, which is what a fixed-hour cron
actually does: it picks the failure up on its next run.

Both are written up in `WHAT_BROKE.md`. The reason they belong in *this* section as well
is that §06 exists to disclose everything that moved the numbers, and "we fixed a bug"
moves them exactly as much as "we changed a constant" does.

## 07 — What the worklist contains

The 155 at-risk invoices were not curated; they are what the legacy policy left behind.
They contain one of every case the compliance layer exists to handle, which is why the
Day-6 demo runs on real rows rather than a fixture:

| Case | Count | Example | What must happen |
|---|---|---|---|
| Above the RBI AFA ceiling | 10 | `inv_0090_08`, ₹18,392 netbanking | escalate to a human, never auto-debit |
| Attempt budget exhausted | 5 | `inv_0488_03`, 4 of 4 used | the 5th attempt is **blocked** — the demo's proof shot |
| Consent withdrawn or DND | 24 | `inv_0132_09` (withdrawn) | every nudge channel blocked regardless of model score |
| Hard decline | 32 | `inv_0190_13`, `card_expired` | re-register the mandate; retrying is known-futile |
| Budget remaining, transient | 123 | — | the actual recovery opportunity |

Distribution of remaining budget: 130 invoices with 3 attempts left, 12 with 2, 8 with 1,
and 5 with none. The categories overlap on purpose — `inv_2757_03` is ₹24,772 *and*
consent-withdrawn, and an invoice that trips two rules is where a guardrail composed of
independent gates is worth more than a decision tree.

## 08 — Scale and population

| Rail | Subscriptions | Min | Mean | Max |
|---|---|---|---|---|
| UPI Autopay | 2,511 | ₹100 | ₹3,451 | ₹59,778 |
| Card mandate | 1,087 | ₹102 | ₹3,833 | ₹57,501 |
| Netbanking | 402 | ₹100 | ₹3,286 | ₹46,528 |

Attempts by number: 30,210 first charges, then **1,973 / 543 / 354** observed retries and
**562 / 138 / 86** censored ones. The taper is the dunning funnel — most invoices are
settled or abandoned before the budget runs out — and it appears in both slices because
the counterfactual schedule stops at the first shadow capture exactly as the real one
does (§06, pass 3).

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

At 4,000 the same cohort holds **194 observed retries and 237 recovery opportunities** —
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

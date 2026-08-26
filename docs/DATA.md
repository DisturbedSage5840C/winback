# Data

## 01 — Schema

Eight tables in `db/01_schema.sql`, split along one line: **facts are immutable, state
is not.**

| Table | Kind | Notes |
|---|---|---|
| `customers` | state | `customer_hash` = `sha256(customer_id)[:12]`, the only identifier permitted into `audit_log` |
| `subscriptions` | state | `cohort ∈ {train, calibrate, test}` frozen at generation, before any model exists |
| `invoices` | state | One row per billing cycle. The unit revenue-at-risk and the 1+3 budget are both scoped to. |
| `payment_attempts` | **immutable** | Observational history (`run_id IS NULL`) *and* evaluation-arm attempts, one table |
| `decisions` | **immutable** | Includes `candidate_set` — every scored option, not just the winner |
| `audit_log` | **immutable** | Append-only, `execution_mode` per row |
| `eval_runs` / `eval_arm_results` | regenerated | So `EVALUATION.md` is generated from the database, never hand-typed |

Two conventions that are load-bearing:

- **Money is always paise, always `BIGINT`.** No float touches a rupee value anywhere
  in this system.
- **IST wall-clock is a generated column**, not application arithmetic. Every NPCI rule
  here is expressed in IST wall-clock, and re-deriving it at two call sites is how the
  two drift apart. `attempted_at_ist`, `charge_at_ist`, and `ts_ist` are computed by
  Postgres from the `TIMESTAMPTZ`.

## 02 — The simulator is a counterfactual oracle

`sim/world.py` implements a **structural** hazard model — deliberately a different
functional form from the gradient-boosted tree that will try to learn it, so the
evaluation is not a model checking its own homework:

```
p_success(attempt) = clip(
      base(method, bank)                # UPI Autopay 8–15% failure, card mandate 2–3%
    × f_rootcause(TD | BD_transient | BD_hard)
    × g_attempt(attempt_number)         # TD decays slowly; BD_hard ≈ 0
    × h_window(is_non_peak)             # congestion — applies to TD only
    × balance_process(customer, date)   # salary cycle: balance replenishes days 1–7
    × k_recency(days_since_last_success),  0, 1)
```

`balance_process` is the realism that matters. `insufficient_funds` retries succeed far
more often just after payday, which is the actual mechanism behind India's UPI-Autopay
failures — and it gives the model a **genuine timing signal to discover** rather than a
coefficient to memorise.

**Determinism.** Every outcome is drawn from a seed derived from
`hash(subject_id, invoice_id, attempt_number, action, slot)`, stored in
`payment_attempts.oracle_seed`. The coin flip for any `(attempt, action, slot)` is
therefore **fixed regardless of which policy asks for it**. That single property buys
two things:

- a full counterfactual over actions that were never taken;
- **paired** comparison across policy arms — the same coin flips, so the variance
  between arms is policy difference and nothing else, and a paired bootstrap is
  legitimate.

## 03 — The training data is censored, on purpose

`sim/legacy_policy.py` generates the *historical* dataset the model trains on, the way
a crude pre-2025 merchant would have:

> retry at fixed T+1 / T+2 / T+3 at 09:00 IST — **but only if `amount > ₹500` and
> `method != netbanking`**

Everything about this is deliberate:

- Outcomes for low-value and netbanking invoices are **never observed**
  (`payment_attempts.observed = FALSE`). They exist in the oracle; they were not in
  the data. That is real selection bias on a variable correlated with the outcome.
- The model must generalise into a region its training data never covered.
- It makes it possible to report something a hackathon submission almost never
  reports: **calibration on the observed slice versus the censored slice, measured
  against the oracle.** "ECE 0.02 where we have data, 0.07 where we don't" is a
  stronger credibility signal than any headline AUC.

Some legacy retries are also seeded **inside peak windows**, which makes them countable
compliance violations in the baseline arms rather than an assertion in a slide.

## 04 — Splits

Held out **by `customer_id` and by time**: train ≈ 60% earliest, calibrate ≈ 20%,
test ≈ 20% latest. No customer straddles two splits, and no future information leaks
backwards. The test set is frozen before any tuning and scored **once**.

## 05 — Scale

500 subscriptions, roughly 700–900 attempts, ~10–20 of them in the live cohort against
real Razorpay test-mode APIs. Generated data and model artifacts are committed
deliberately, so a fresh clone reproduces the committed numbers byte-for-byte; only
scratch output is gitignored.

## 06 — Regenerating

```bash
.venv/bin/python -m sim.generate      # calls winback_reset_world(), then rebuilds
```

Regeneration is the one operation that legitimately removes immutable rows — it is
rebuilding history, not editing it. It routes through the `winback_reset_world()`
`SECURITY DEFINER` function rather than through a `DELETE` grant, so the escape hatch
is a single named, greppable, log-emitting call instead of an ambient permission.

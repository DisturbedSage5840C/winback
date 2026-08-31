# Evaluation

> **Status: measured.** §01–§03 fix the design, and they were written before the results
> existed so that the results could not be chosen after seeing them. §04–§06 are
> **generated** — `python -m eval` writes five runs to Postgres and `python -m eval.report`
> renders those sections straight out of `eval_runs` / `eval_arm_results` /
> `eval_intervals` / `eval_arm_violations`. Nothing between the two markers is typed by a
> human, because a number a human can retype is a number a human can round;
> `python -m eval.report --check` fails if the committed file drifts from the database.
> The figure in §07 is drawn by `python -m eval.charts` from those same four tables, so it
> cannot disagree with the tables above it.
> §07 onward is the reading of those numbers, hand-written on purpose — a conclusion
> generated from a template is a conclusion nobody checked — and every claim it makes is
> pinned by a test in `eval/tests/`, including the one about what the evaluation *failed*
> to show. §09–§11 are the frozen model results from `ml/artifacts/metrics_v1.json`.

## 01 — The metric

**Rupees recovered per legal attempt consumed**, reported beside a
compliance-violations count. Not rupees recovered.

A policy that recovers more money by taking a fifth attempt has not beaten anything —
it has broken NPCI OC-215-A, and no merchant can ship it. Making legality part of the
denominator rather than a footnote is what makes the comparison honest, and it is why
the naive baseline loses on grounds that were fixed before the experiment ran.

## 02 — The four arms

| Arm | Policy | Role |
|---|---|---|
| A | Never retry, always escalate | Over-conservative floor |
| B | Retry everything to the cap, any hour | Naive baseline — **and illegal** |
| C | Legacy policy (fixed T+1/2/3 at 09:00, amount- and method-filtered) | What the merchant does today |
| D | **Winback** — calibrated model + cost policy + guardrail | The submission |

All four are scored on the **same** held-out invoices with the **same** oracle seeds,
so a difference between arms is a difference in policy and not in luck. Confidence
intervals come from a paired bootstrap over subscriptions.

## 03 — What gets reported, per arm

Rupees recovered · attempts consumed · **legal** attempts consumed ·
**rupees per legal attempt** · nudges sent · escalations · **compliance violations** ·
invoices written off · paired bootstrap CI.

Plus, for the model itself: ECE (10-bin) · Brier · PR-AUC · minority-class
precision/recall · reliability diagram · the confusion matrix **priced in rupees**
(a false positive costs one burned legal attempt plus messaging; a false negative
costs the invoice times margin). Never plain accuracy — on an 8–15% failure base rate
accuracy is a number that rewards predicting nothing.

---

<!-- BEGIN GENERATED — python -m eval.report -->

## 04 — The result

Dataset `v1` fingerprint `c32b2b063cd87707` · model `v1` · cohort `test` · 190 failed invoices replayed · 10,000 bootstrap resamples, seed `20260905`.

Every arm faced the same invoices with the same oracle seeds. Re-running the
harness reproduces every rupee below exactly; the intervals are sampling
uncertainty about which 800 customers were in the cohort, not simulation noise.

| Arm | Policy | Recovered | Legally recovered | Attempts | Legal attempts | Nudges | Escalated | Written off | Violations | ₹ / legal attempt |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A | Never retry, always escalate | ₹0 | ₹0 | 0 | 0 | 0 | 190 | 0 | 0 | — |
| B | Retry everything to the cap, any time | ₹6,39,598 | ₹6,39,598 | 276 | 210 | 0 | 0 | 23 | 66 | ₹3,045.70 |
| C | Legacy fixed-offset dunning | ₹5,57,737 | ₹53,490 | 183 | 63 | 0 | 0 | 71 | 120 | ₹849.05 |
| **D** | Winback: calibrated model + cost policy + guardrail | ₹6,39,626 | **₹6,39,626** | 197 | **197** | 78 | 0 | 23 | **0** | **₹3,246.83** |

### The paired comparison

Differenced *inside* each resample, over subscriptions. Marginal intervals
overlap almost entirely here because the arms move together when a lucky
customer is resampled in — which is exactly why reading significance off two
overlapping marginal intervals would be wrong.

| Comparison | Legally recovered | Legal attempts | Violations | ₹ per legal attempt | Excludes zero |
|---|---:|---:|---:|---:|---|
| **D − A** | ₹6,39,626 [₹4,68,398, ₹8,35,540] | 197 [166, 230] | 0 [0, 0] | — | legally recovered, legal attempts |
| **D − B** | ₹28 [−₹2,697, ₹2,781] | −13 [−32, 5] | −66 [−96, −42] | ₹201.13 [−₹79, ₹501] | violations |
| **D − C** | ₹5,86,136 [₹4,15,611, ₹7,82,898] | 134 [107, 163] | −120 [−152, −91] | ₹2,397.78 [₹1,629, ₹3,286] | legally recovered, legal attempts, violations, ₹ per legal attempt |

### What each arm's violations were, and what they bought it

| Arm | Rule broken | Times | Rupees those attempts recovered |
|---|---|---:|---:|
| B | `bd_hard_not_retryable` | 66 | ₹0 |
| C | `peak_window` | 81 | ₹5,04,247 |
| C | `bd_hard_not_retryable` | 39 | ₹0 |

## 05 — The same four arms, by region

The legacy policy never retried an invoice under ₹500 or on a netbanking
mandate, so the model has no training labels in the censored region. Splitting
the arms the same way asks whether the advantage survives outside the data.

| Region | Cases | Arm | Legally recovered | Legal attempts | Violations | ₹ / legal attempt |
|---|---:|---|---:|---:|---:|---:|
| **Observed** | 133 | A | ₹0 | 0 | 0 | — |
|  |  | B | ₹5,59,114 | 151 | 39 | ₹3,702.74 |
|  |  | C | ₹53,490 | 63 | 120 | ₹849.05 |
|  |  | D | ₹5,59,142 | 140 | 0 | ₹3,993.87 |
| **Censored** | 57 | A | ₹0 | 0 | 0 | — |
|  |  | B | ₹80,484 | 59 | 27 | ₹1,364.14 |
|  |  | C | ₹0 | 0 | 0 | — |
|  |  | D | ₹80,484 | 57 | 0 | ₹1,412 |

## 06 — Sensitivity to the nudge assumption

The one number in this system that cannot be measured without sending real
messages. The **world's** nudge effect moves across these runs; the **policy's**
belief about it does not. What the table measures is not whether nudges work —
it is how much the policy loses by being wrong about them.

| World nudge multiplier | Arm | Nudges sent | Legally recovered | Legal attempts | ₹ / legal attempt |
|---|---|---:|---:|---:|---:|
| **1.00** (nudge does nothing) | B | 0 | ₹6,39,598 | 210 | ₹3,045.70 |
|  | D | 78 | ₹6,39,438 | 199 | ₹3,213.26 |
| **0.62** | B | 0 | ₹6,39,598 | 210 | ₹3,045.70 |
|  | D | 78 | ₹6,39,626 | 197 | ₹3,246.83 |
| **0.40** | B | 0 | ₹6,39,598 | 210 | ₹3,045.70 |
|  | D | 78 | ₹6,40,525 | 196 | ₹3,267.98 |

<!-- END GENERATED -->

---

## 07 — What the numbers say, and what they do not

![Four arms, one cohort](assets/four_arms.png)

**Against the naive baseline, this is a tie on money and a rout on legality.** Arm B
retries everything to the cap at any hour. It recovers ₹6,39,598 that the law would have
allowed; Winback recovers ₹6,39,626. The paired interval on that difference is
[−₹2,697, ₹2,781] — it contains zero, comfortably, and it is meant to. The difference
in legal attempts, −13, also contains zero. **Winback does not beat retry-everything on
rupees, and this document will not claim it does.** What separates them is the third
column: 66 violations against zero, interval [−96, −42]. The finding is that the naive
policy's lawbreaking buys it nothing — it reaches the same money by a route a merchant
cannot ship.

That is sharper than a lift number would have been, and it is the reason
`eval/tests/test_bootstrap.py::test_the_money_claim_against_retry_everything_is_a_tie_and_must_stay_one`
exists. It asserts the interval still spans zero. If a later change makes Winback look
better on rupees, that test fails and someone has to decide deliberately whether the
change is real or whether the harness has started flattering the submission.

**The two baselines break the law in two different ways, and only measurement told them
apart.** Every one of arm B's 66 violations is `bd_hard_not_retryable`: it re-presents
mandates the bank has permanently declined. Those 66 presentments recovered **₹0**. B
spends legality and receives nothing for it. Arm C is the opposite: 81 of its 120
violations are `peak_window` presentments, and those recovered **₹5,04,247 — 90% of
everything arm C appears to collect.** Score arm C on rupees and it places second; score
it on rupees it was *allowed* to collect and it places last of the three arms that
present at all, at ₹53,490. An evaluation with only a money column would have ranked
these two baselines in the wrong order.

Arm B commits no window violations at all. It was written expecting them to be its
characteristic failure, and the data refused: this dataset's presentment hours are 01–09,
14, 15 and 22 IST, none of which fall in a peak window, so an arm that ignores the clock
never happens to hit one. The docstrings were corrected to match the measurement rather
than the other way round; `docs/WHAT_BROKE.md` records it.

**The advantage survives where the model has no training data.** In the censored region —
under ₹500 or on netbanking, where the legacy policy never retried and so never generated
a label — arms B and D recover the identical ₹80,484, but D does it in 57 legal attempts
against 59, with zero violations against 27. Given §10's finding that the model is badly
miscalibrated there and still ranks correctly, a policy that picks the best of several
scored candidates rather than thresholding on a probability is exactly the design that
should survive that region. It did.

**The nudge assumption barely matters, which is the useful version of that result.**
Across a world where the nudge does nothing (multiplier 1.00) and one where it works far
harder than assumed (0.40), with the policy's belief held wrong at 0.80 throughout,
Winback's legally-recovered total moves by ₹1,087 — 0.17%. The nudge shifts which
marginal invoices get presented, not how much is there to collect. This is the one
parameter that cannot be measured without sending real messages, and the honest thing to
report is not that the nudge works but that the result does not rest on it.

**Arm A is a floor, not a competitor.** It never presents, so it recovers nothing and
consumes no legal attempts. Its ratio is left undefined rather than printed as zero: the
denominator does not exist, and a ₹/legal-attempt figure for an arm that took no attempts
would be arithmetic on nothing. `eval/bootstrap.py` skips ratio intervals for it for the
same reason.

## 08 — Calibration on the censored slice

Reported separately for the slice the legacy policy observed and the slice it never
retried (low-value, netbanking). The gap between them is the honest measure of how far
the model can be trusted outside its training distribution, and it is reported whether
or not it flatters the result. It did not. See §10.

---

## 09 — Model v1, frozen

`python -m ml` trains, calibrates, evaluates and writes every artifact in one command.
The test split is touched **once**, at the end of that file, after the calibrator has
already been chosen on the calibration split.

| | |
|---|---|
| Model | XGBoost binary classifier, `max_depth=5`, `learning_rate=0.05`, `min_child_weight=10`, `random_state=20260828` |
| Early stopping | iteration **339** of 600, on a time-ordered inner split (22,005 fit / 3,884 inner validation) |
| Rows | train 25,889 · calibrate 4,791 · test 2,400 · censored calibrate 118 · censored test 85 |
| Base rate | 87.0% captured on test — which is why accuracy appears nowhere below |

**Gain-ranked features.** The three that carry the model are the ones a dunning
operator would name: what went wrong last time, how many attempts have already been
spent, and which rail the mandate sits on.

| Feature | Gain |
|---|---|
| `prior_root_cause_bd_hard` | 43.4% |
| `attempt_number` | 19.3% |
| `method_upi_autopay` | 8.2% |
| `action_is_retry` | 4.9% |
| `paid_count` | 4.1% |
| `prior_root_cause_bd_transient` | 2.6% |
| `bank_method_failure_rate` | 2.1% |
| `cycle_number` | 2.0% |
| `mandate_age_days` | 1.9% |
| everything else | < 1.5% each |

`salary_day` is not in the table because it is not in the model. The simulator uses it
to drive the balance hazard; a merchant cannot see it, so the feature builder never
reads it, and `ml/tests/test_features.py` shuffles every customer's payday and asserts
the feature row comes back byte-identical. The model has to *discover* the payday
effect through `day_of_month`, or not at all.

### The calibrator, chosen out-of-fold

Five contiguous time-ordered folds inside the calibration split. Each candidate is
scored on rows it did not see; the winner is then refit on the whole split.

| Calibrator | ECE (out-of-fold) | ECE (in-sample) | Rows at exactly 0 or 1 | |
|---|---|---|---|---|
| uncalibrated | 0.0390 | — | — | the baseline to beat |
| **sigmoid** | **0.0373** | 0.0372 | 0 | **chosen** |
| temperature | 0.0430 | 0.0423 | 0 | |
| isotonic | 0.0006 | 0.0000 | 913 | **disqualified** |

Isotonic posts an ECE sixty times lower than the winner and loses anyway. It is a step
function bounded by its outermost knots, so every score outside the range it was fitted
on maps to exactly 0.0 or exactly 1.0 — which it does to 242 calibration rows at zero,
560 at one, and **111 of the 118 censored calibration rows**, every one of them at
zero. A probability of exactly zero is not a low probability; it is a claim no evidence
can revise. The Day-5 policy ranks candidate actions by expected rupees, and an
expected value of exactly zero can never be the argmax — so a calibrator that zeroes
the censored region would make Winback decline to retry precisely the invoices the
legacy policy declined to retry, reimplementing the selection bias this project exists
to remove, one layer further down and silently. Admissibility is therefore a gate, not
a tiebreak, and it is checked on the calibration cohort's censored slice so that
disqualifying isotonic costs nothing that was being held back.

The gap between the two ECE columns is why selection runs out-of-fold at all. An
earlier version of `ml/calibrate.py` scored each calibrator on the rows it was fitted
on, and isotonic won with an ECE of exactly 0.0000 — because a free monotone step
function fitted on 4,791 rows can reproduce those 4,791 rows. Three methods of very
different capacity cannot be compared in-sample; the comparison ranks them by how much
they can memorise. Both columns are printed so a reader can see the distance.

### Test cohort, scored once

| Slice | n | ECE | MCE | Brier | PR-AUC (failure) | ROC-AUC |
|---|---|---|---|---|---|---|
| test, uncalibrated | 2,400 | 0.0775 | 0.6477 | 0.0694 | 0.6834 | 0.8197 |
| **test, observed** | 2,400 | **0.0342** | 0.6549 | 0.0602 | 0.6834 | 0.8197 |
| test, censored | 85 | **0.4420** | 0.7740 | 0.4103 | 0.8911 | 0.8643 |

Calibration halves the ECE and moves nothing else: sigmoid is monotone, so it cannot
reorder predictions, and PR-AUC and ROC-AUC are rank statistics. That is the expected
result and it is reported rather than dressed up.

### The confusion matrix, priced in rupees

At the **per-invoice break-even threshold** implied by the cost matrix — attempt when
`p · amount · margin > (1 − p) · (attempt cost + burned attempt)`, which solves to
`p > c / (c + amount · margin)` — with `attempt_cost = ₹3`, `burned attempt = ₹12`,
`margin = 0.25`:

| | |
|---|---|
| Margin recovered | ₹19,752 |
| Wasted attempt cost | ₹39 |
| Margin forgone | ₹0 |
| Net | ₹19,713 |
| Attempts declined on cost alone | **52 of 2,400** |

Those thresholds land between 0.0010 and 0.3704, median **0.0425**. That is the
finding, and it is not a flattering one for threshold-based framings of this problem:
**the money almost never says no.** A retry costs ₹15 all-in and an invoice averages
several hundred, so expected value alone would attempt nearly everything. Money is not
the scarce resource here — the four legal attempts are. This is precisely why the
Day-5 policy maximises expected rupees *subject to the NPCI budget* rather than
thresholding on probability, and why the headline metric is rupees per legal attempt.

![Model v1 calibration](assets/calibration.png)

## 10 — What the censored slice actually showed

The legacy policy never retried an invoice under ₹500 or on netbanking, so the model
has no training evidence in that region — but the oracle knows what would have happened
there. Scored against it:

| | Observed slice | Censored slice |
|---|---|---|
| ECE | 0.0342 | **0.4420** |
| Mean signed gap vs the oracle | −0.021 | **−0.452** |
| Mean absolute gap | 0.095 | 0.500 |
| ROC-AUC | 0.8197 | **0.8643** |
| PR-AUC (failure) | 0.6834 | 0.8911 |

**The model is badly miscalibrated off-distribution and still ranks correctly there.**
Every censored prediction falls below 0.40 while 56% of those attempts capture; the
model is not confused about which censored invoices are the good ones, it is uniformly
too pessimistic about all of them. In the 0.1–0.2 bin it predicts 14% and observes 80%.

This is a stronger result than uniform failure would have been, and it is the reason
the policy layer is built the way it is. Miscalibrated-but-ordered means the *absolute*
probability is untrustworthy in the censored region while the *relative* one survives —
so a policy that picks the best of several candidate actions is far less damaged there
than one that thresholds on a number. `ml/tests/test_calibrate.py` asserts all of it:
the gap, its direction, and that ranking holds.

One thing this does **not** show. The censored region's marginal success rate is close
to the observed one (61.8% vs 58.2% pooled; +3.5 / +1.6 / −3.7 points at attempts 2, 3
and 4), and the ₹500 value floor barely moves p(success) at all — +1.0 points measured
on first charges, which no filter touches. The netbanking exclusion moves it more, +3.4
points. So the bias is not in the marginal; it is in the covariates. The censored region
is cheap, netbanking, and early in a mandate's life, and the observed data contains
almost none of that combination. A near-identical marginal success rate alongside a
large calibration gap is not a weaker result than the reverse would have been — it is
the more interesting one, because it is the case a merchant would never catch by
watching their recovery rate.

## 11 — The limitation, stated first

Arm D beats arms B and C **inside a world I wrote**. Three things were done to make
that less circular than it sounds — the simulator uses a deliberately different
functional form from the model, the training data is censored by a biased legacy
policy so the model must generalise past what it observed, and calibration is measured
on both slices — but a simulator is a model, not the world. Naming this before a
panelist does is not modesty; it is the only reading of the evidence that survives
contact with someone who has run a real dunning system.

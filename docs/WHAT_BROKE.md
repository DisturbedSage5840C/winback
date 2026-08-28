# What broke

A running log, written on the day each thing happened rather than reconstructed at
the end. Razorpay scores *Failure Recovery* explicitly; this is the honest version of
that answer, including the entries that make the build look less clever than it was.

Format: what I believed → what was actually true → what it cost → what changed.

---

## 2026-08-26 · There is no "charge this subscription now" API

**Believed.** The build plan assumed retries could be driven through the Razorpay
Subscriptions REST API — fetch the pending subscription, call something like
`POST /subscriptions/:id/charge`.

**Actually true.** That endpoint does not exist. The Subscriptions API exposes
create / fetch / update / cancel / pause / resume / scheduled-changes / invoices, and
nothing else. Razorpay's own dunning is automatic and internal (T+3), and the only
manual trigger is the "Charge this now" button in the dashboard. The real programmatic
primitive is `POST /v1/payments/create/recurring`, which requires a mandate `token`
**and** S2S Recurring Payments activated on the account by Razorpay support — an
approval process, not an API call, and not obtainable inside a ten-day build.

**Cost.** Half a day of research, before any code was written. Cheap because it was
found by reading the API reference rather than by writing an adapter against an
imagined endpoint.

**Changed.** This became the architecture rather than a workaround. One decision path,
one guardrail, one audit trail, and two interchangeable executors —
`LiveRazorpayAdapter` and `SimulatedAdapter` — with `audit_log.execution_mode`
recording which one actually ran, per row. The live adapter does what a fresh test
account genuinely permits (payment links with notifications suppressed, orders,
payment/token reads) and produces real `plink_…` / `order_…` / `pay_…` IDs in the
audit trail. The 500-subscription batch runs against the seeded oracle, because test
mode moves no real money and "measured money recovered" is only measurable against a
counterfactual. Stating that plainly is the point, not a caveat buried in a footnote.

---

## 2026-08-26 · `CalibratedClassifierCV(cv='prefit')` no longer exists

**Believed.** Prefit calibration is `CalibratedClassifierCV(model, cv='prefit')` —
which is what almost every tutorial and every model in my own older projects uses.

**Actually true.** Removed in scikit-learn 1.9. The current signature is
`CalibratedClassifierCV(estimator=None, *, method='sigmoid', cv=None, n_jobs=None,
ensemble='auto')`, and prefit calibration is now spelled with an explicit wrapper:

```python
from sklearn.frozen import FrozenEstimator
cal = CalibratedClassifierCV(FrozenEstimator(fitted_xgb), method="sigmoid")
cal.fit(X_calib, y_calib)
```

**Cost.** None yet — caught while pinning dependencies rather than on Day 4 with a
half-built pipeline.

**Changed.** Pinned `scikit-learn==1.9.0` and noted it in `requirements.txt` at the
pin itself, where the next person will actually read it. The upside: 1.9 also adds
`method='temperature'`, so the calibration step compares all three methods on the
calibration split instead of assuming sigmoid.

---

## 2026-08-26 · The Docker daemon was not running, and `docker compose` said something else

**Believed.** `docker compose up -d` failing meant a problem with the compose file.

**Actually true.** The daemon was not running at all:
`failed to connect to the docker API at unix:///Users/…/docker.sock`. The compose file
was fine.

**Cost.** Two minutes.

**Changed.** `scripts/bootstrap.sh` checks `docker info` first and prints the actual
remedy ("start Docker Desktop") rather than letting a fresh-clone user debug a healthy
YAML file. Small, but this is exactly the class of thing that turns a five-minute demo
setup into a twenty-minute one.

---

## 2026-08-26 · The append-only tests passed against the wrong layer

**Believed.** One test per table: try `DELETE FROM audit_log`, assert the trigger
raises `append_only_violation`.

**Actually true.** Every one of those tests failed — with `InsufficientPrivilege`,
not `RestrictViolation`. The grant layer refuses the statement *before* Postgres ever
evaluates a row-level trigger, so as the application role the trigger is unreachable
and the test proved nothing about it.

**Cost.** One failing test run. Worth far more than it cost, because the naive test
would have passed if I had written it slightly differently and I would have shipped a
trigger I had never actually exercised.

**Changed.** The suite now tests the two layers separately and says why:
`winback_agent` is refused by the grant (`permission denied`), and `winback_owner` —
the superuser, for whom grants are not a constraint — is refused by the trigger
(`append_only_violation`). The second is the one that answers a panelist asking what
stops *me* from editing the number in my own demo.

---

## 2026-08-26 · `OLD` is unassigned in a statement-level trigger

**Believed.** One trigger function could serve both the row-level
(`BEFORE UPDATE OR DELETE`) and statement-level (`BEFORE TRUNCATE`) triggers, printing
`OLD` in the error message either way.

**Actually true.** In a statement-level trigger `OLD` is not assigned, and reading it
raises `55000 object_not_in_prerequisite_state` — a *different* error from the one the
test asserts on. The TRUNCATE test would have passed while the trigger was failing for
an unrelated reason, and the real message would never have reached anyone.

**Cost.** Caught by reasoning about the error code before running it, so: nothing.
It would have been expensive to find later, since the symptom is a passing test.

**Changed.** The function branches on `TG_LEVEL` and the TRUNCATE test asserts on the
message text, not merely on the exception type.

---

## 2026-08-26 · My own tests violated the invariant I had just written

**Believed.** The rule "every non-`APPROVE` verdict must name a `stop_reason`" was
uncontroversial enough to enforce in `RuleResult.__post_init__` and move on.

**Actually true.** Two of the eleven tests in `test_result.py` failed immediately —
mine, constructing `REDIRECT_TO_WINDOW` and `ESCALATE_HUMAN` results with no
`stop_reason`. I had internalised the invariant as being about denials.

**Cost.** Ten minutes, and a decision: fix the tests or soften the rule.

**Changed.** The tests. The invariant is right — a redirect and an escalation stop the
action *as proposed* just as surely as a denial does, and an audit row that records a
stop without a reason is the exact failure this project exists to avoid. The error
message was rewritten to read correctly for all three blocking verdicts rather than
sounding like it was about `DENY` alone.

The general lesson, which is why this is logged rather than quietly fixed: when the
first thing an invariant catches is your own code, that is evidence the invariant is
load-bearing, not evidence it is too strict.

---

## 2026-08-26 · Nearly gated mandate retries on telecom consent

**Believed.** While composing `guardrail.py`, the obvious shape was to run every rule
on every action. More gates, more caution, better.

**Actually true.** It would have been a compliance error in the expensive direction. A
mandate is a standing authorisation to debit; TRAI's TCCCPR governs **messages**.
Running `consent_gate` against a `RETRY` would have blocked recovery on every customer
who ever opted out of marketing SMS — while looking, in a demo, maximally responsible.
The mirror-image mistake was in the same commit: running the NPCI 1+3 cap against a
`NUDGE`, which would silence exactly the customer who most needs a payment link, over a
budget that counts mandate presentments and not contacts.

**Cost.** None in wall-clock, because it was caught while writing the test names — the
sentence "a withdrawn-consent customer cannot be retried" does not survive being said
out loud. It would have cost a large fraction of measured recovery if it had shipped.

**Changed.** `_rules_for` dispatches on `ActionKind`, and both directions are pinned by
a test that asserts the *absence* of a rule name from `authorizing_rule`:
`test_consent_does_not_gate_a_debit` and `test_the_retry_cap_does_not_gate_a_message`.
Documented as a decision in `COMPLIANCE.md` §1.7, not left to be re-derived.

---

## 2026-08-26 · My probe scored a gateway route-miss as a pass

**Believed.** For probe 10 (`POST /v1/invoices/:id/notify_by/:medium`) I wrote
`p.ok = p.status in (400, 404)` with the comment *"a 400/404 here means the route
exists"* — reasoning that hitting a deliberately nonexistent invoice id should produce
an app-level 404, which would prove the route was reachable.

**Actually true.** The response body was `{"message": "no Route matched with those
values"}`. That is **Kong's** message — the API gateway, before Razorpay's application
ever sees the request. It means the *path pattern* is not registered, which is the
opposite conclusion from the one my check drew. My probe printed `[PASS]` next to a
capability the account does not have.

**Cost.** Ten minutes, and it was caught only because I read the note text under the
verdict instead of trusting the verdict. Had I skimmed, `LIVE_LANE_FINDINGS.md` would
have claimed a working endpoint, and Day 6 would have discovered otherwise while wiring
the adapter.

**Changed.** Probe 10 now inspects the body for Kong's signature and reports
**INCONCLUSIVE** rather than guessing. The same suspicion was then applied to probe 9,
which produced the useful part of the whole spike: running
`POST /payments/create/upi` as a control showed both members of the
`/payments/create/*` family returning byte-identical errors while `/orders` returned
200 on the same key — turning "S2S is probably not activated" into an observation with
a control rather than an assumption.

**The general lesson.** A status code is not a finding. Two failures with the same HTTP
code can have opposite causes, and the only way to tell them apart is a control request
you already know the answer to. Every ambiguous row in that matrix now has one.

---

## 2026-08-26 · A boolean verdict field made three states impossible to tell apart

**Believed.** After fixing the probe-10 verdict, the probe harness was correct. Each
probe had `ok: bool`, set from `response.is_success`, and the run printed `7/10 passed`.

**Actually true.** The boolean had been the *cause* of the probe-10 bug, not a
bystander, and two more rows were still wrong because of it. `ok=False` was doing the
work of three genuinely different claims:

- probe 8 — **confirmed absent**, with an error message explaining why;
- probe 9 — **confirmed absent, and that was the hypothesis**; the whole justification
  for the two-adapter architecture, printed as if the build were broken;
- probes 2 and 11 — **never ran at all**, and therefore entitled to claim nothing.

Printing `FAIL` next to a probe that never executed is the same error as printing
`PASS` next to a gateway route-miss: a verdict asserting more than the evidence
supports. And `7/10 passed` quietly counted "I didn't try" as evidence of absence.

**Cost.** Twenty minutes to restructure, and it paid for itself immediately — see the
next entry, which only surfaced because closing the "skipped" probes stopped being
optional.

**Changed.** `Outcome` is now a five-member enum — `PASS`, `FAIL`, `EXPECTED`,
`INCONC`, `SKIP` — and **`SKIP` is the default**, so a probe that never runs cannot be
scored as anything else. The summary line reports every bucket rather than a single
ratio, and a separate `usable` property (`outcome is PASS`) is what gates a capability
into `LiveRazorpayAdapter`. All eleven probes now resolve: 9 PASS, 1 FAIL, 1 EXPECTED,
zero inconclusive, zero skipped.

The general lesson, and the reason this is a separate entry from the probe-10 one:
fixing the wrong verdict was not the same as fixing the thing that produced it. A type
too narrow to express the real answer will keep manufacturing wrong answers, one row at
a time, until it is widened.

---

## 2026-08-26 · The tool the plan chose for dead mandates does not exist

**Believed.** Build plan §1.1: `create_registration_link` is available on the **local**
MCP server (it is one of four tools documented as remote-restricted), and is *"the
correct real action for `BD_hard` mandate failures"* — when a mandate is revoked or the
account is closed, ask the customer to re-register rather than burning legal attempts.

**Actually true.** It is not in the image. Running `tools/list` against
`docker run --rm -i razorpay/mcp` returns **41 tools, none of which mention
registration, mandate, subscription, plan or recurring**. The local surface is orders,
payments, payment links, QR codes, refunds, settlements, payouts and tokens. Three of
the four documented remote-restricted tools are there — `create_refund`,
`close_qr_code`, `create_instant_settlement` — and the fourth simply is not.

**Cost.** None, because probe 2 was run on Day 2 instead of Day 6. Had it stayed
"deferred", this would have surfaced while wiring the `BD_hard` branch of the adapter,
on the day with the least slack in the schedule.

**Changed.** The `BD_hard` action becomes a simulated re-registration carrying a real
`plink_…`, which is what a merchant would actually send a customer with a dead mandate
anyway. Recorded in `LIVE_LANE_FINDINGS.md` §05.

Two things fell out of reading the tool list that were not what the probe was looking
for, which is the argument for reading it rather than counting it:

- **`payment_link_notify` is in the list**, and it is the one tool an agent could call
  that would deliver a message to a real contact, around the consent gate. It goes on
  the Day-6 `allowed_tools` denylist, named now rather than noticed later.
- **`READ_ONLY=true` removes 16 of the 41 tools**, `payment_link_notify` among them, and
  `TOOLSETS=orders` narrows to 5 (probe 11). That is a cheap second layer underneath
  `can_use_tool` — defence in depth, not a replacement for the gate.

---

## 2026-08-27 · The worklist counted retries that never happened

**Believed.** `exception_worklist.attempts_used` could simply count rows in
`payment_attempts` for the invoice.

**Actually true.** That count includes censored rows — attempts the legacy policy never
made. An invoice whose retries were all suppressed by the value floor would show its
full budget consumed, and be filtered out of the worklist as exhausted. The failure mode
is precise and nasty: **the invoices the censoring makes most interesting are exactly the
ones this would hide**, and it would have looked like a small worklist rather than a bug.

**Cost.** Caught while reading the view against the loaded data, not by a test.

**Changed.** Every aggregate in the view now carries `AND a.observed`, with a comment
saying why. The general shape — a counterfactual row and a real row in the same table —
is worth the ergonomics, but every consumer has to declare which one it means.

---

## 2026-08-27 · Two of my tests asserted the opposite of the design

**Believed.** Writing the generator's test suite, two properties looked obviously
correct: no retry should follow a hard decline, and every open invoice should have
attempt budget left.

**Actually true.** Both failed, and both were wrong *as tests*.

The legacy cron fires on a fixed schedule and never reads the error code — that is the
point of it. It retries dead mandates because nobody wired the decline reason into the
scheduler. Asserting it doesn't would have been asserting that the baseline is smarter
than it is, and would have deleted arm C's central inefficiency, which
₹-per-legal-attempt exists to expose.

The exhausted at-risk invoice (`inv_0488_03`, 4 of 4 used) is likewise real: the cap is
spent and the invoice is still open. That is not a broken row, it is the row the Day-6
demo needs — the one where the guardrail blocks a fifth attempt on camera.

**Cost.** Half an hour, mostly deciding rather than typing.

**Changed.** Both tests, into the claims actually worth making:
`test_the_legacy_cron_keeps_retrying_a_dead_mandate` paired with
`test_no_retry_after_a_dead_mandate_ever_collects` (the waste exists *and* it never
pays), and `test_the_worklist_is_mostly_actionable_and_partly_exhausted`.

The lesson is the one that keeps recurring: when a test fails, the first question is
whether the test or the code is describing the world correctly. Twice here it was the
test, and "fixing" the code would have quietly made the evaluation less honest.

---

## 2026-08-27 · The censoring reason had nowhere to go

**Believed.** `sim/legacy_policy.py` documented the reason as "written to
`payment_attempts.observed = FALSE` with the reason kept alongside."

**Actually true.** There was no such column. The reason existed on the in-memory row and
was silently dropped at load. Found by running a `GROUP BY censoring_reason` against the
loaded data expecting the 144/48 split from the realism gate.

**Cost.** Minutes, but it would have cost a day later — the Day-4 calibration report
splits on exactly this field, and it would have gone missing at the point of use.

**Changed.** Added `censoring_reason TEXT` with a value `CHECK`, plus
`CHECK (observed = (censoring_reason IS NULL))` so an unobserved row without a reason —
an unexplained hole in the training data — cannot be persisted at all. A comment
describing a column is not a column.

---

## 2026-08-27 · Three chart layouts that only failed when looked at

**Believed.** The realism figure was done once the numbers were right and the palette
validated by script.

**Actually true.** Rendered and opened, it had label collisions in three places: an NPCI
annotation sitting on a panel subtitle, stacked-bar labels overlapping each other, and
the UPI line running through the legend. The palette validator passes on all of them —
it checks colour, not geometry.

**Cost.** Three render-and-look rounds.

**Changed.** The layout, and the working habit: render the output and look at it before
calling it done. A fourth defect was caught the same way and was not cosmetic — the
panel title read "hid 4% of its own retries", computed against *all* attempts when the
sentence says retries. The correct denominator is 37%. A wrong number in 28pt type,
which no test covers, is exactly the sort of thing that gets noticed on a projector.

---

## 2026-08-28 · The test cohort was too thin to measure anything with

**Believed.** 500 subscriptions was a reasonable population. It produced 3,762 invoices
and a realism gate that passed 13 of 13 graded checks, so the dataset looked finished.

**Actually true.** The split is ordered in time, which makes the test cohort the *newest*
mandates — the ones with the fewest billing cycles behind them and therefore the fewest
failures. Counting what was actually in it: **20 observed retries and 24 failed first
charges.** The Day-5 headline is "measured money recovered across a batch" with a paired
bootstrap CI. On 24 invoices that interval would have been wide enough to cover all four
arms, meaning the evaluation could not have distinguished Winback from the baseline it
exists to beat. The gate passed because it grades the *whole* population; nothing in it
looked at the cohort the results would actually be computed on.

**Cost.** Half a day, and it re-opened the world constants after they had been frozen —
paid on Day 4 instead of Day 5, which is the only reason it was affordable at all.

**Changed.** `N_SUBSCRIPTIONS` to 4,000, chosen by measuring test-cohort thickness at
500 / 1,500 / 2,500 / 4,000 rather than by picking a round number: 196 observed retries
and 237 recovery opportunities, for 2.3 seconds of generation. `docs/DATA.md` §04 now
carries a per-cohort table, so the number that actually constrains the evaluation is
visible instead of derivable.

---

## 2026-08-28 · Two world constants had been fitted to noise

**Believed.** The two constants tuned on Day 3 were set from mechanism and confirmed by
the gate. Both card-rail figures sat inside their bands, so both were right.

**Actually true.** They sat inside their bands *at n≈1,000 attempts*, where the standard
error on a 2-3% rate is wide enough to land inside a one-point band by luck. Growing the
population to 4,000 moved the card failure rate to 3.03% (n≈9,100) — outside the band —
and the UPI salary-cycle ratio to 1.96×, just under its 2.0× floor. The band had not
changed and the mechanism had not changed; only the sample size had, and the sample size
was what had been holding the numbers up.

The tempting move was to widen the band by a tenth of a point. That would have been the
single most dishonest edit available anywhere in this repo — a gate relaxed because it
failed is not a gate, and every claim downstream of it inherits the relaxation.

**Cost.** Two hours, most of it spent on a sweep harness whose first version computed the
salary-cycle ratio differently from the gate (it used the `insufficient_funds` share where
the gate uses the overall failure rate on first charges) and reported 5.2× where the gate
reported 2.0×. Optimising against a metric that is not the one being graded is worse than
not optimising, because it produces confident wrong answers. The fix was to delete the
reimplementation and import `_cycle_ratio` from `sim/validate_realism.py` directly.

**Changed.** Five constants re-read at the larger sample, each held inside the mechanism
it already claimed and documented one-by-one in `docs/DATA.md` §06, which is now titled
"Every tuning pass, in full" rather than "The one tuning pass". Every graded check now
sits mid-band rather than against an edge — a world that passes by a tenth of a point is
one reseed from failing, and that fragility is itself evidence of fitting.

---

## 2026-08-28 · Five attempts were dated into the future

**Believed.** `AS_OF` being a constant rather than `now()` was enough to keep the dataset
reproducible and free of future-dated rows. A test asserted exactly that, and it passed.

**Actually true.** The legacy policy schedules retries at T+1/T+2/T+3 from the failed
charge. For an invoice in its current cycle the schedule is truncated by a random draw
of "how far the merchant's cron happened to get" — but nothing checked the resulting
timestamps against `AS_OF`. An invoice whose first charge failed on 23 August therefore
got a T+1 retry at 09:00 on 24 August: nine hours past the freeze instant, with an oracle
outcome nobody could have observed. At 500 subscriptions no invoice landed in that
one-day window, so the test passed for two days by luck. At 4,000, five did.

**Cost.** Twenty minutes, and only because the population change surfaced it. It would
otherwise have shipped as five training rows containing information from the future.

**Changed.** The current-cycle schedule is now truncated by **both** limits, the tighter
winning: how far the cron got, and how far the clock allows. The existing test needed no
change — it was correct all along and had simply never been given a population large
enough to fail on, which is the argument for keeping assertions that look redundant.

---

## 2026-08-28 · The censored slice was easier because my code made it easier

**Believed.** The headline evidence for the whole selection-bias argument: mean oracle
`p(success)` was **75.6%** on the retries the legacy filters suppressed against **58.2%**
on the retries actually made. A 17-point gap, reported in `DATA.md` §03 and graded by the
realism gate. The story wrote itself — the value floor and the netbanking exclusion were
hiding the easy money.

**Actually true.** The gap was an artefact of my own control flow. The observed dunning
branch stops presenting debits at the first capture, because a merchant who has been paid
does not keep debiting. The shadow branch — the counterfactual schedule materialised for
censored invoices — did not stop. It ran all three retries unconditionally, so an invoice
whose shadow retry #2 captured still got shadow retries #3 and #4, drawn under exactly
the conditions that had just succeeded. The censored slice was padded with attempts
nobody would ever have made, all of them easy.

With the two branches doing the same thing, the honest figure is **61.8% vs 58.2%**, and
censored attempts fall from 1,686 to 786.

**Cost.** Half a day, and it cost the most quotable number in the project. Found by
reading the shadow branch beside the observed one while writing `ml/dataset.py`, not by
any test — both branches were individually correct, and nothing asserted they agreed.

**Changed.** The shadow loop now breaks on capture and carries
`last_technical_failure_at` forward exactly as the real one does; the docstring says
explicitly that what is discarded for a censored invoice is the shadow *status*, not the
shadow control flow. Then the argument was rebuilt on what is actually true, which turned
out to be the stronger version: the bias is not in the marginal, it is in the covariates.
The censored region is cheap, netbanking, and early in a mandate's life, and the observed
data holds almost none of that combination. Day 4 priced it — ECE **0.4420** there against
**0.0342** on the observed slice, uniformly pessimistic and *still correctly ordered*.

Three ungraded decomposition rows were added to the realism gate (the gap per attempt
number, and what each filter variable is worth measured on first charges, which no filter
touches) because the surviving graded check now reads 3.6 points against a ≥3-point band.
**The band was not lowered.** A marginal-rate check is simply the wrong instrument for a
bias that lives in the covariates, and the fix for a check that can no longer see the
thing it was written about is a better check, not a wider one.

The general lesson: a number that flatters the thesis deserves more scrutiny than one
that does not, and "two code paths that are supposed to be the same policy" is a place to
look. I found this one by luck — the assertion that would have caught it is
`test_the_shadow_schedule_stops_at_the_first_capture_too`, which now sits next to the
observed-branch test it should always have been paired with.

---

## 2026-08-28 · A retry that happened before the charge it was retrying

**Believed.** The retry schedule was settled: fixed offsets from the failed charge, a
fixed hour, clamped to `AS_OF` so nothing lands in the future. Two days of green tests.

**Actually true.** The urgent branch fires at **11:30 IST with a T+0 offset**. An invoice
charged at 15:00 therefore got its "same-day" retry at 11:30 *that morning* — three and a
half hours before the failure it was responding to. **122 attempts** dataset-wide.

The damage was not cosmetic. `PriorState.before` builds a candidate's features from every
attempt dated earlier than it, so a retry preceding its own charge handed the model a
first charge that already had a failure behind it: `attempt_number=1` with
`prior_failures=1`, a sequence no merchant can present.

**Cost.** An hour, and it was caught by `ml/tests/test_features.py` —
`test_a_censored_row_sees_its_own_counterfactual_history`, which asserts
`attempt_number == prior_failures + 1` and failed on 1 of 85 rows. The realism gate,
which is the thing nominally responsible for the dataset, has no check that could have
seen it: every rate it grades was still inside its band.

**Changed.** `retry_schedule` rolls a slot forward whole days until it is strictly after
both the charge and the previous retry, and says so in the rationale
(`"(next run, +1d: the 11:30 job had already run)"`). That is what a fixed-hour cron
actually does — it picks the failure up on its next run — so the offsets describe the
job's schedule rather than a guaranteed spacing. Three regression tests were added,
including one asserting a slipped retry still consumes its full legal budget.

The general lesson, and the reason this is logged separately from the shadow-schedule
entry it shares a day with: **the layer that finds a bug is rarely the layer responsible
for it.** Both of Day 4's dataset bugs were found by model code, because the model is the
first consumer that reads the rows *in order* rather than in aggregate. A validation gate
that only grades marginal rates cannot see a sequencing error, however many checks it has.

---

## 2026-08-28 · The calibrator with the best score was the one that had to lose

**Believed.** Fit sigmoid, isotonic and temperature on the calibration split, report ECE
for each, take the winner. Isotonic won by a factor of sixty — ECE 0.0000 against
sigmoid's 0.0372.

**Actually true.** Two things were wrong, and the second is the one that mattered.

*It was scored in-sample.* Each calibrator was being evaluated on the rows it had just
been fitted on. A free monotone step function fitted on 4,791 rows can reproduce those
4,791 rows, so the comparison was not ranking calibration quality — it was ranking
capacity to memorise, between three methods of wildly different capacity. Out of fold,
isotonic's ECE rises to 0.0006 and its advantage is real but much smaller.

*It emits exact certainties.* Isotonic is a step function bounded by its outermost knots,
so every score outside its fitted range maps to exactly 0.0 or exactly 1.0 — which it did
to 242 calibration rows at zero, 560 at one, and **111 of the 118 censored calibration
rows, every one of them at zero.** The Day-5 policy ranks candidate actions by expected
rupees, and an expected value of exactly zero can never be the argmax. Shipping isotonic
would have made Winback decline to retry precisely the invoices the legacy policy declined
to retry — reimplementing the selection bias this project exists to remove, one layer
further down, and silently. It would have posted the best calibration number in the repo
while doing it.

**Cost.** An afternoon, most of it spent deciding whether admissibility was a tiebreak or
a gate.

**Changed.** Selection runs over five contiguous time-ordered folds inside the calibration
split, and **both** columns are printed so a reader can see the distance between them.
Admissibility is a hard gate evaluated before ECE is compared at all, checked on the
calibration cohort's censored slice so disqualifying isotonic costs nothing that was being
held back. If no candidate is admissible the pipeline raises rather than falling back to
the uncalibrated booster — that fallback is a decision about what the system predicts, and
a person should make it after reading the message, not an `except` branch. All of it is
pinned by tests, including `test_the_choice_would_have_been_different_in_sample`, which
keeps the bug alive as an assertion.

---

## 2026-08-28 · I invented a band, then withdrew it

**Believed.** While adding the decomposition checks to the realism gate, I wrote a graded
check on how much of the observed-vs-censored gap survives conditioning on attempt number,
with a band around it.

**Actually true.** There is no published figure for that quantity, and there could not be
— it is a property of a censoring policy I wrote. The band came from the number the world
already produced, which makes it a target the world was guaranteed to hit, and a gate that
cannot fail is decoration. `sim/validate_realism.py`'s own stated rule is **no band without
a source**, and I had broken it inside the file that states it.

**Cost.** Ten minutes, entirely because I re-read the rule while looking for something
else.

**Changed.** All three decomposition checks are `[REPORT]`, ungraded, printing their
numbers and their `n` without a verdict. Six of the nineteen checks are now ungraded, and
that ratio is a feature: the gate's credibility comes from the bands it *does* grade
having sources, which is only worth something if inventing one is not an option.

---

## 2026-08-28 · Four more chart defects that only failed when looked at

**Believed.** The calibration figure was done: palette validated by script, numbers
correct, three panels laid out.

**Actually true.** Rendered and opened, it had four defects a test could not have caught.
Two annotations used `←` and `∈`, neither of which exists in Helvetica Neue — matplotlib
substitutes a tofu box rather than failing, so the committed PNG carried two blank
rectangles while the build printed a warning nobody reads. The right panel's subtitle ran
off the figure edge. The reliability legend sat on top of the observed curve, and the
calibrator legend collided with the temperature bar. Fixing the first three surfaced a
fourth: the retitled left subtitle then overran into the right column's.

**Cost.** Four render-and-look rounds — the same working habit as the realism figure two
days earlier, applied because that entry exists.

**Changed.** ASCII notes, explicit legend corners, and subtitles kept under ~100
characters, each with a comment saying what the constraint actually is (`_panel_title`
reserves 18pt of pad, so a second line grows *up* into the title). The lesson is unchanged
from 26 August and is repeated here because repeating it is the point: **render the output
and look at it before calling it done.** The palette validator checks colour, not geometry,
and no test in this repository will ever fail because a figure is unreadable.

---

## Open

- **S2S Recurring activation** — assumed unavailable. If it is granted, the live lane
  widens; the architecture does not change. Tracked in `docs/LIVE_LANE_FINDINGS.md`.
- **`HookMatcher`'s exact shape** in `claude_agent_sdk.types` — the published example
  is loose. To be read directly from the installed package on Day 6 rather than copied
  from the docs.

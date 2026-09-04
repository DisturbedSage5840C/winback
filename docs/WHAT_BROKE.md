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

## 2026-08-28 · The uniqueness constraint that was wrong twice over

**Believed.** `payment_attempts` was keyed `UNIQUE (invoice_id, attempt_number, run_id)`,
written on Day 1 with the four-arm evaluation already in mind. One table for the
observational history and the arm replays, separated by `run_id`.

**Actually true.** Found while designing the harness, before a single arm row was written.
Two independent faults in one line:

1. **The arm is not in the key.** All four arms share a single `run_id` — that is what
   makes them one comparable run. Arm B replaying invoice `inv_0007` attempt 2 and arm D
   replaying the same invoice's attempt 2 are the same `(invoice_id, attempt_number,
   run_id)`, so the second arm to write would have been rejected. The evaluation would
   have failed on its first insert. Cheap, loud, and not the interesting half.
2. **It enforced nothing on the history.** Postgres treats NULLs as distinct in a unique
   constraint by default, so for observational rows — where `run_id IS NULL` — every row
   compared unequal to every other. The table could have held two attempt 3s for the same
   invoice and the constraint would have called it fine. The history is the training set;
   a duplicate there is a silently doubled row inside a feature window, and nothing
   downstream would have complained.

The second fault is the one worth recording. It was invisible, it protected the most
important table in the repository, and it would have stayed invisible because the
constraint *looked* like it was doing its job.

**Cost.** Twenty minutes, and a `docker compose down -v`. No data lost: the loader does not
exist yet, so the database held only schema.

**Changed.** `UNIQUE NULLS NOT DISTINCT (invoice_id, attempt_number, run_id, arm)`
(Postgres 15+; the container is 17). The arm joins the key, and NULLs now compare equal, so
the observational history is genuinely constrained to one row per
`(invoice, attempt_number)` for the first time. Verified against the running container
rather than against the editor — the IDE's SQL parser is a T-SQL one and flags
`NULLS NOT DISTINCT` as a syntax error, which is a fact about the editor, not the schema.

---

## 2026-08-30 · The baseline broke a different law than the one I wrote it to break

**Believed.** Arm B — "retry everything to the cap, any time" — was documented as the arm
whose violations come from ignoring NPCI's execution window. Its docstring said so in as
many words: *"the violations this arm commits are a consequence of retrying at the same
time of day as the charge."*

**Actually true.** B commits **zero** window violations, and the reason is structural. It
retries at the hour the original charge was presented; the generator's presentment hours
are 1–9, 14, 15 and 22 IST, none of which is inside 10:00–13:00 or 17:00–21:30. B inherits
the charge's legality along with its hour. Every one of its 66 violations is
`bd_hard_not_retryable`: it re-presents mandates that are revoked, closed or hard-declined,
because "retry everything" never reads the decline reason.

Found by printing the violation breakdown by `stop_reason` rather than by trusting the
prose. The corrected story is better than the one I had written, and in two directions:

- **B's 66 illegal presentments recovered exactly ₹0.** They were all on dead mandates,
  which never pay. B does not trade legality for money; it spends legality and gets
  nothing. That is a sharper indictment than "it retried at the wrong time".
- **The window violations live in arm C**, where they were always going to: 81 of C's 120,
  from the 11:30 IST urgent slot. And unlike B's, C's violations *do* pay — ₹504,247 of
  the ₹557,737 C appears to recover, 90% of its total, arrives through presentments the
  guardrail refuses. Hold C to the law and it recovers ₹53,490.

So the two baselines fail in two different ways, and neither was arranged: B wastes legal
budget on invoices that cannot pay, C books most of its revenue illegally.

**Cost.** Fifteen minutes. Nothing downstream was wrong — the harness measured all of this
correctly and had been reporting it since the first run; only my description of it was
wrong, in a docstring that would have been read as a claim about the results.

**Changed.** `eval/arms.py` docstrings for both B and C now state what the arms measurably
do, with the mechanism, and mark it as found by running rather than by design. The general
lesson is the one already in this file twice: **the code was right and the prose about the
code was wrong**, and only looking at the output caught it.

---

## 2026-08-31 · Two tests passed for the wrong reason, and the reason was a no-op

**Believed.** The tests that prove `eval/report.py` writes only its own block —
`test_writing_replaces_only_the_block` and `test_rewriting_an_unchanged_file_is_a_no_op` —
did `monkeypatch.setattr(report, "REGIONS", ())` so they would render just the headline
table against the cheap `eval_test` run, and not the four optional runs.

**Actually true.** The monkeypatch did nothing. `render()` was declared as
`def render(run_id=HEADLINE, *, regions=REGIONS, sensitivity=SENSITIVITY)`, and a default
argument is evaluated **once, when the function is defined**. By the time the test rebound
the module attribute, `render` was already holding the original tuple. Both tests were
quietly rendering the real `v1_observed` and `v1_censored` runs from the production rows —
and passing, because those runs happen to exist.

They would have kept passing until the day someone ran the suite against a database with
only the test run in it, at which point two tests about *writing files* would have failed
with a `LookupError` about a missing evaluation run. A green suite that is green for a
reason you did not write is worse than a red one.

Found by asking why the write tests were slower than the render tests that use the same
fixture. They were doing four times the work.

**Cost.** Twenty minutes, most of it spent confirming the tests were wrong rather than the
code.

**Changed.** `render()` now takes `regions=None` / `sensitivity=None` and resolves the
module constants inside the body, so the monkeypatch reaches it. More usefully, the two
helpers underneath — `_region_table` and `_sensitivity_table` — were changed to take
their runs as a **required** parameter with no default at all. The trap cannot recur in
this module, because there is no longer a default to capture. The general lesson: a
mutable-looking module constant used as a default argument is a snapshot, not a reference,
and a test that patches one is testing nothing.

---

## 2026-08-31 · A zero-width bar still draws its edge

**Believed.** The recovery panel of `docs/assets/four_arms.png` draws each arm's legal
recovery as a solid bar and its illegal recovery as a hatched one beside it. Arms A, B and
D recovered nothing illegally, so their hatched segment has width zero and therefore
draws nothing.

**Actually true.** Matplotlib draws the *edge* of a zero-width bar. Three arms that have
never broken a rule each carried a thin red tick at the origin — in a panel whose entire
point is that only arm C recovers money illegally. The figure was making the opposite of
its own argument, in the reserved status colour.

Three more defects in the same render, all of the same kind: the first panel's legend sat
on top of arm D's bar and its value label; the third panel was mostly white space, because
two forest-plot rows were given the same axes height as four bars.

**Cost.** Ten minutes, and only because the procedure says to open the PNG. The validator
had already passed the palette on all six checks — it grades colour, not layout, and it
cannot see a red tick that should not be there.

**Changed.** The illegal series is now filtered to the arms with a non-zero value before
it is drawn at all, rather than drawn at zero width; `_legend` takes a `loc`; the figure
uses `height_ratios=[1.0, 1.0, 0.62]`. This is the third entry in this file about a chart
defect that no test would ever have caught, and the reason "render it and look at it" is a
step and not a suggestion.

---

## 2026-08-31 · `allowed_tools` auto-approves before the money gate is consulted

**Believed.** `ClaudeAgentOptions(allowed_tools=[...], can_use_tool=money_gate)` composes
the obvious way: `allowed_tools` says which tools exist for this run, `can_use_tool`
decides whether each individual call is permitted. So the full permitted set goes in
`allowed_tools`, and the gate sits behind it.

**Actually true.** They do not compose — they short-circuit. An entry in `allowed_tools`
is a standing *approval*, and the SDK grants it **before** `can_use_tool` runs. Listing
`execute_recovery` there meant the permission callback was never reached for the one call
it exists to refuse. The gate was decorative. Every unapproved presentment in the first
smoke run went straight through.

The SDK does say so, in the only way that matters: it emits a `CanUseToolShadowedWarning`
at construction time naming the shadowed tools. I had been reading the batch output, not
the warnings above it.

**Cost.** Forty minutes, and the uncomfortable realisation that if the smoke run had
happened to produce no illegal action, this would have shipped as a compliance story with
no compliance in it.

**Changed.** The tool list is now split at the source rather than filtered at the call
site. `agent/tools.py` exports `PREAPPROVED_TOOLS` (assess, guardrail — asking a question
is not a privileged act) and `GATED_TOOLS` (execute, notify — the two that move money or
reach a customer), with `ALLOWED_TOOLS` as the union for documentation only. The
orchestrator passes **`PREAPPROVED_TOOLS`** to the SDK and nothing else, so the gated two
have no standing approval and every call to them falls through to the callback.

`test_the_gated_tools_are_not_pre_approved` asserts `set(PREAPPROVED_TOOLS).isdisjoint(
GATED_TOOLS)`. If someone later "tidies up" by passing the union, the suite fails before
the batch does. The general lesson: when a framework offers both a static allow-list and a
dynamic callback, find out which one wins **before** relying on the other.

---

## 2026-08-31 · `PostToolUse` hands you a bare list, not an envelope

**Believed.** The `PostToolUse` hook's `input_data["tool_response"]` is the tool result
object — `{"content": [{"type": "text", "text": "..."}]}` — the same shape the tool itself
returned.

**Actually true.** It arrives as the bare `list` of content blocks. `payload.get("content")`
on a list raises `AttributeError`, and the SDK swallows exceptions raised inside a hook, so
the audit writer silently wrote nothing for a while and the run reported success.

**Cost.** Twenty minutes, most of it spent printing `type(...)` inside a hook because
nothing else could tell me what was in it.

**Changed.** `_payload()` in `agent/hooks.py` accepts both shapes — a list of blocks, a
dict with `content`, or a raw string — and keeps unparseable text rather than discarding
it, so a malformed response becomes a visible audit row instead of a missing one. Four
tests in `agent/tests/test_hooks.py` pin each shape.

---

## 2026-08-31 · JSON has no infinity, and the hook that found out said nothing

**Believed.** The batch had completed. 25 invoices, an audit row for each. The one thing
left was to confirm the `decisions` rows lined up with them.

**Actually true.** They did not. `inv_0027_02` had an `audit_log` row whose `decision_id`
was `NULL` — an executed action pointing at a decision that was never written. Reproducing
the insert directly gave the real error, which the batch had never shown me:

```text
InvalidTextRepresentation: invalid input syntax for type json
DETAIL:  Token "-Infinity" is invalid.
```

`ml/policy.py` uses `float("-inf")` as the sentinel for a candidate the guardrail ruled
out — correct inside the policy, where the argmax needs a real number to compare. But
`json.dumps` serialises it as the bare token `-Infinity`, which is **not RFC 8259**, and
PostgreSQL's `jsonb` rejects it outright. So any invoice whose candidate set contained at
least one denied action failed to write. `inv_0007_01` worked and `inv_0027_02` did not,
which is exactly the kind of intermittent that looks like a database problem and is not.

Three separate things had to be wrong at once for this to be invisible: Python emits a
non-standard token by default, Postgres refuses it, and **the SDK swallows whatever a
`PostToolUse` hook raises**. The failure surfaced as an absence.

**Cost.** An hour, nearly all of it before the error message was in hand. Reading the rows
rather than re-running the batch is what found it — the orphan `decision_id` named the
failing insert precisely.

**Changed.** Three layers, because one would have been a patch:

1. **At the source.** `ScoredCandidate.to_dict()` emits `expected_value_paise: null` plus
   an explicit `ruled_out: true` when the value is non-finite. Nothing is lost — the
   `verdict` and `stop_reason` on the same row already say *why* it scored negative
   infinity, and that is the part a reviewer reads. The float sentinel is untouched inside
   the policy, so no evaluation number moves.
2. **At the boundary.** Every `jsonb` write goes through `_jsonb(value)`, which is
   `json.dumps(..., allow_nan=False)`. A non-finite number that reaches the database now
   raises against the line that produced it instead of being discovered later as a gap.
3. **At the silence.** The audit hook catches its own write failures into
   `AuditWriter.write_failures`; `BatchReport` carries them, prints them, and `main()`
   exits non-zero. A batch that finished with a hole in its audit trail did not finish.

`agent/tests/test_audit_writes.py` writes 20 consecutive real invoices into a rolled-back
transaction — the regression as a property, not as one remembered id.

The general lesson is not about JSON. It is that an audit trail whose failures are
invisible is not an audit trail, and that a hook runner which swallows exceptions makes
every write inside it a silent one until you make it otherwise.

---

## 2026-08-31 · `HookMatcher` — resolved, and it was the loose part of the docs

Carried as an open item since Day 1. Read directly from the installed
`claude_agent_sdk.types` (0.2.144) rather than copied from the published example:

```python
HookMatcher(matcher: str | None = None, hooks: list = [], timeout: float | None = None)
```

`matcher=None` matches every tool — which is what `audit_matcher()` uses, filtering by
tool name inside the hook instead, so a tool added later cannot quietly escape the audit
by not matching a pattern. No architectural change was needed; the risk row in the plan
("audit hooks can fall back to wrapping the adapter") does not need to be spent.

---

## 2026-08-31 · Every customer with no money was recorded as a compliance block

The first full batch wrote 11 rows with `trigger='tool_error'`, `outcome='blocked'`. I
looked at what was failing, expecting exceptions, and found this in `stop_reason`:

```text
inv_0631_03: {'error_code': 'BAD_REQUEST_ERROR', 'error_source': 'customer',
              'error_reason': 'insufficient_funds', 'root_cause_class': 'BD_transient'}
inv_0843_01: {'error_code': 'GATEWAY_ERROR', 'error_source': 'network', ...}
inv_1632_04: {'error_code': 'GATEWAY_ERROR', 'error_source': 'bank',
              'error_reason': 'issuer_down', 'root_cause_class': 'TD'}
```

Those are not tool errors. They are **declined debits** — the ordinary outcome this whole
project exists to predict. The audit hook branched on truthiness:

```python
if payload.get("error"):        # ← wrong: a failed debit has one too
    ... trigger="tool_error", outcome="blocked"
```

and `ExecutionResult.error` is, by its own docstring, *"Razorpay's error fields, verbatim,
when a presentment failed."* Both shapes carry an `error` key. One means the executor
broke; the other means the bank said no.

**Why this one mattered more than it looks.** `blocked` is not a neutral label here — it
is *the compliance signal*. It is the red chip in the demo, the outcome the violations
chart counts, the row a panelist would read as "the guardrail stopped this." Eleven
customers with an empty account were recorded as eleven actions the guardrail refused.
The aggregate is the tell: that run has **zero** `outcome='failed'` rows across 190
invoices, in a dataset built around an 8–15% failure rate. Every decline had been
relabelled.

The irony is that the codebase already argues against exactly this, twice, in the
opposite direction. `AdapterError`: *"Conflating them would let an outage look like a
customer with no money, which is the single most misleading thing this system could
record."* And `_execute`: *"An executor failure is not a declined payment, and must never
be recorded as one."* I had written the guard for one direction and then walked into it
backwards.

The fix is a named predicate rather than a tighter truthiness test, because the
discriminator is a fact about the two return shapes and deserves to be stated once:

```python
def _is_tool_refusal(payload) -> bool:
    # A tool that refused never reached the adapter, so it has no ``outcome``.
    # A tool that ran always returns one — every ExecutionResult has the field.
    return bool(payload.get("error")) and "outcome" not in payload
```

Four tests pin it, one per shape: a decline, an outage, a spent approval, a clean run.

**What it cost.** `audit_log` is append-only in three independent ways, which is the
property that makes it worth anything — so the mislabelled rows could not be corrected in
place, by me or by anyone at a psql prompt. The only route to a clean trail was the
sanctioned one: `reset_world()` → `sim.load` → `python -m eval` → `eval.report`, then re-run
the batch from zero. `docs/EVALUATION.md` regenerated byte-for-byte identically, which is
the check that the reload actually restored the same world. An append-only table making a
mistake expensive to erase is the design working, not the design failing.

---

## 2026-09-01 · The audit trail recorded only what the batch did

The first full batch finished clean: 190 invoices, none errored, exit 0. Then the two
tables disagreed.

```
decisions: 184        audit_log: 156
```

Twenty-eight invoices had reached a conclusion and left no trace of it. The conclusion in
every one of those cases was a **write-off** — and a write-off calls no tool, because
there is nothing to call. `audit_log` was written by `PostToolUse`, which fires on tools
that ran. So the trail recorded every action the agent took and nothing about the actions
it declined to take.

That is the wrong half. An audit trail exists to answer one question — *why was this
customer not charged* — and the invoices it was silent about are precisely the ones where
the answer is a rule.

The reasons existed. They had been computed on every batch and written into
`decisions.candidate_set`, where they can be read one row at a time and not counted:

```sql
-- 118 ruled-out candidates across 56 decisions
bd_hard_not_retryable   90
dnd_registered          16
consent_withdrawn       12
```

**What I nearly built instead.** My first instinct was that the guardrail "never denies"
because the batch never walks an invoice to its cap, and that the fix was to make it —
multi-wave batching, or an in-agent walk to conclusion, so that an `npci_1_plus_3_cap_
exhausted` denial would finally appear. Checking the evaluation stopped that: **arm D
consumes 196 attempts over 190 invoices.** It essentially never reaches attempt 3, let
alone 5. In a rewound replay the cap *cannot* bind, and any run in which it did would be
one I had arranged. The refusals that actually happen are the three above, and the defect
was never that they were absent — it was that they were unreachable.

So the fix records what is real rather than manufacturing what would demo well:
`TERMINAL_ACTIONS`, a `record_conclusion` on the audit writer, and `_binding_refusal`,
which asks the scored candidate set which rule closed the door. Presentments are searched
first: a `bd_hard_not_retryable` on the retry is the reason the invoice cannot be
recovered, while a `dnd_registered` on the nudge is only the reason the customer cannot be
told about it. Both are true; only the first answers the question.

The distinction the row now carries, and deliberately does not flatten: `blocked` **with**
a `stop_reason` is a compliance stop; `blocked` **without** one is an economic judgement
about an attempt that was legally available and not worth making. Filing the second under
the first would inflate the compliance story with decisions the law had nothing to do
with.

`batch_v1`'s trail was not backfilled and could not have been. A corrected run gets a new
`run_id`.

---

## 2026-09-02 · `--live` had never once run live

`live_v1` completed, and its own headline said:

```
run live_v1 [simulated]: ... executor: simulated
```

It had been invoked with `--live`. It exited 0.

Two definitions of one name, in two layers:

```python
# core/config.py    — a Literal, so `settings.execution_mode` is a plain str
ExecutionMode = Literal["simulated", "live"]

# agent/adapters/base.py — a StrEnum, because the adapter layer wants a type
class ExecutionMode(StrEnum): ...
```

and one identity check across the seam:

```python
if settings.execution_mode is not ExecutionMode.LIVE:   # ← never True for "live"
    return None
```

`"live" is ExecutionMode.LIVE` is `False`. The live adapter was unreachable from the CLI
and had been for its entire existence.

**The part worth keeping.** There was a test for this, and it passed:

```python
_adapter_for(replace(settings, execution_mode=ExecutionMode.LIVE))
```

It injected the enum member. `get_settings()` returns the string. The test was asserting
against a state the program never produces — a green test standing exactly where the bug
was. The lesson is not "use `==`"; it is that a test which constructs its own input can
certify a path that nothing else can reach. The new test starts by asserting
`get_settings().execution_mode == "simulated"` *is a str*, so it fails if the comparison
ever goes back to identity.

The fix coerces at the boundary — `ExecutionMode(settings.execution_mode)` — and then adds
a second, independent check in `main()`, because the first failure was invisible in every
artifact the run produced:

```python
if args.live and report.execution_mode != "live":
    return 1
```

What was asked for, compared against what the report says happened, by a different path
from the one that chose the adapter. Two paths now have to agree before a live run passes.

`live_v2` printed `executor: live` and wrote ten real payment-link ids into
`audit_log.razorpay_entity_id` — `plink_TX74boAx41rZbw`, `plink_TX74uG3aUrqWYR`, and eight
more — alongside the first `outcome='blocked'` row the agent has ever written with a real
`stop_reason` on it. Both fixes, visible in one table.

---

## 2026-09-02 · An invoice that concluded in prose and nowhere else

`live_v2` reported `12/12 invoices (0 errored)` and wrote 11 audit rows. The arithmetic
was there to be done and nothing in the report did it.

`inv_0007_01` had a `decisions` row with a full approval:

```
proposed_action  retry
guardrail_verdict  APPROVE
authorizing_rule   npci_1_plus_3: attempt 2/4 permitted; non_peak_window: 13:30 IST is
                   outside peak hours; afa_threshold: ₹1,091 within the ₹15,000 ceiling
                   for saas; pre_debit_notice: notice sent 33.8h before the debit;
                   root_cause_retryability: TD may be retried
```

and nothing else. Its closing sentence was *"The guardrail approved the retry (NPCI 1+3:
attempt 2/4, non-peak 13:30 IST, AFA ₹1,091 under th…"* — the agent obtained the approval,
narrated it, and reached `max_turns` before calling `execute_recovery`. The live path is
slower and talkier than the simulated one; six turns is comfortable for one and evidently
not always for the other.

Nothing illegal happened and no money moved. The problem is what the trail then said:
exactly as much about that invoice as about one the batch had never opened. An
approval-with-no-attempt and a never-attempted invoice are different facts and were
recorded identically.

This is the one case hooks structurally cannot catch. `PostToolUse` fires on tools; the
event here is that no tool ran. So the batch loop checks its own coverage after each
invoice and closes the gap itself — `AuditWriter.covered`, filled inside `record_action`
rather than by each caller, so no write path can forget to mark one.

The two reasons are kept apart, for the same reason as the entry above:

| `stop_reason` | What it means |
|---|---|
| `approval_granted_not_spent` | The guardrail authorised the presentment. The attempt was lost to the agent's turn budget, not to a rule. |
| `no_conclusion_reached` | The agent ended without acting *or* concluding. |

Recording the first as a compliance stop would read as a law protecting the merchant from
a recovery it was entitled to make — a lie in the merchant's favour, which is the
direction an audit trail must never lean. The count is now in the headline
(`· 1 unacted`), because a recovery lost to a turn budget is a real loss and burying it in
a table nobody opens is how it stays lost.

---

## 2026-09-02 · The worklist emptied itself whenever the batch succeeded

**Symptom.** `GET /runs/batch_v2/worklist` returned `{"total": 25, "rows": []}`. Twenty-five
invoices worked, twenty-five audit rows, and nothing to show for any of them. Two tests
skipped rather than failed, because both were written to skip on an empty worklist —
which is how a defect gets to look like a legitimately quiet database.

**Cause.** `exception_worklist` opened with `WHERE i.status = 'at_risk'`, which is exactly
right for the view's original job: the queue of invoices still needing work. But the
agent's own actions move an invoice off that status — `recovered` on a successful
presentment, `written_off` on a `BD_hard` conclusion. So joining a *completed* run against
an at-risk-only view returns the empty set by construction. The page whose entire purpose
is to show what the batch decided went blank precisely when the batch had decided
something.

```
run worklist  =  audit_log (what the run did)  ⋈  exception_worklist (at_risk only)
                                                                      ↑
                                        the run's success removes its own rows from here
```

**Fix.** The filter moved out of the view and into the caller. `exception_worklist` now
exposes `invoice_status` and filters nothing; `GET /worklist` — the live queue — filters
`at_risk`, and `GET /runs/{id}/worklist` filters nothing, because a concluded invoice is
the thing it exists to display. The budget arithmetic (`attempts_used` /
`attempts_remaining`, still excluding counterfactual rows) stays in the view, where it is
computed once.

**The lesson is about the two tests that skipped.** `pytest.skip("this run touched no
at-risk invoice still in the worklist")` was written to tolerate a legitimately empty
database. It also tolerated this. A skip that can absorb a defect is a test that has
stopped being one, so both are now preceded by
`test_a_finished_run_still_has_a_worklist`, which asserts the rows exist rather than
stepping around their absence.

---

## 2026-09-02 · The headline ₹ figure was a string

**Symptom.** `test_the_overview_is_the_view_and_not_a_second_opinion` failed on one field
out of twelve:

```
AssertionError: recovered_paise
assert '15058200' == Decimal('15058200')
```

**Cause.** `sum()` over a `bigint` column returns `numeric` in Postgres, psycopg maps
`numeric` to `Decimal`, and FastAPI — correctly, since JSON has no decimal type and
float would lose precision — serialises `Decimal` as a **string**. Every other field in
the funnel is a `count(*)`, which comes back as `bigint` and serialises as a number. So
the one field that was a sum, and the only one carrying rupees, was the one that came out
quoted.

**Why this would have survived to the demo.** `"15058200"` renders as `15058200`. A
dashboard formatting it as currency shows the right number. It fails only where JavaScript
adds it to something — a total across runs, a delta against the baseline — and `+` on a
string concatenates instead of adding, silently and without an error. The failure mode is
a wrong ₹ total on the slide the whole submission rests on, with nothing anywhere
reporting a problem.

**Fix.** `api/main._number` converts every `Decimal` at the edge: to `int` when the value
is integral, which every paise total and every count in this API is, and to `float`
otherwise, which is where the genuinely fractional numbers live (bootstrap bounds, ECE in
`eval_*`). Nothing is rounded — an integral `Decimal` converts exactly.
`test_the_rupee_total_is_a_number_and_not_a_string` asserts the type, not the value,
because the value was never wrong.

---

## 2026-09-03 · The resume query had come to mean the opposite of what it said

**Symptom.** `batch_v2` halted a second time on the account session limit, this time
mid-item at `inv_1957_01`. The audit trail afterwards read 75 invoices, 75 rows — and
`decisions` read 76. One invoice had an `APPROVE` for a retry on record and no audit row
of any kind, and `_already_worked` would have skipped it forever on the next resume.

**Cause.** `_already_worked` unioned `audit_log` with `decisions`, and the reason was
sound when it was written: an invoice the guardrail denied concluded with a decision row
and no audited action, so reading the action table alone found 77 of 85 concluded
invoices and would have re-worked the eight write-offs. Then `record_conclusion` and
`record_silence` were added — a write-off, an escalation, and an invoice nothing was done
to now each write their own row — and the premise quietly expired. Post-fix, a decision
without an audit row can only mean the item died *between* the guardrail's answer and the
tool call. That is the one case that must be re-worked, and the union was the one thing
guaranteeing it never would be.

```
before record_conclusion:   decision, no audit row  →  "wrote it off"        →  skip
after  record_conclusion:   decision, no audit row  →  "died mid-item"       →  RE-WORK
                            (the query never noticed the meaning had flipped)
```

**Fix.** The query reads `audit_log` alone. Re-working is safe against the NPCI cap:
`execute_recovery` writes its row through the `PostToolUse` hook, so an invoice with no
row had no presentment, and the attempt budget is counted from `payment_attempts`, never
from this query. The test that asserted the union now asserts its inverse, with the
interrupted item as its fixture.

**Lesson.** A fix elsewhere can invalidate the premise of a correct query without
touching a line of it. The union was never wrong about the data; it was wrong about what
the data had come to mean, and only a second interruption made that visible.

---

## 2026-09-03 · Calling one endpoint from another passed it a `Query` object

**Symptom.** `GET /invoices/{id}/compliance` raised
`TypeError: '<' not supported between instances of 'Query' and 'int'`, from a comparison
inside `compliance.non_peak_window.next_slots` — a pure function with full test coverage
that had never been given a non-integer in its life.

**Cause.** The panel embedded the window snapshot by calling the handler directly:
`"window": compliance_window()`. FastAPI resolves `n: int = Query(3, ge=1, le=12)` per
*request*; called as an ordinary Python function, `n` is the `Query` object itself. The
validation, the coercion, and the bounds all live in the request path, and nothing in
this call went through it.

**Fix.** The snapshot moved into a plain `_window(now, n)` that both the endpoint and the
panel call, so the handler is only ever a handler. It also removed a second bug worth
naming: the panel had been evaluating the rules `at` one moment and reporting the window
at a slightly later one.

**Lesson.** A FastAPI endpoint is not a reusable function. Anything two callers need goes
under the handler, not through it.

---

## 2026-09-03 · The local MCP had never once started, and nothing noticed

**Symptom.** The new lane ladder's first real use, on its first probe:

```
local unreachable — exit 1: failed to create toolsets:
toolset orders,payments,payment-links,subscriptions does not exist
```

`RAZORPAY_MCP_MODE=local` had never worked. Not since Day 1.

**Cause.** Three things wrong in one environment variable. `razorpay/mcp` reads
`TOOLSETS` as a **space**-separated list, so a comma-joined value is one toolset name;
the valid name is `payment_links`, not `payment-links`; and `subscriptions` is not a
toolset in the image at all. Bisected against the real image — `orders payments
payment_links` starts and exposes 20 of the default 41 tools, including every tool the
live lane uses.

**Why it survived a week.** Day 1's probe 11 established that `TOOLSETS` *works* by
passing exactly one value, `orders`. One value cannot reveal a separator. The multi-value
string was then written by hand from that finding, and `RAZORPAY_MCP_MODE=off` — the
correct default for the batch — meant nothing ever tried to start it again.

**Fix.** Corrected in `.env` and `.env.example`, and the default in `core/config.py`,
which still held the comma-joined `payment-links` spelling. Commas are now normalised to
spaces in `local_server()`, because the failure is silent in the worst way: local mode
does not start, the ladder falls to remote, and everything keeps working well enough that
nobody looks. `test_comma_joined_toolsets_are_normalised_to_spaces` holds it.

**Lesson.** A config value proven with one element is not proven. The separator is the
part that breaks, and it is exactly the part a single-element test cannot see.

---

## 2026-09-03 · The failure drill found that there was nothing to fail

**Symptom.** The Day-8 drill, run as the plan specifies it — `docker kill` the local MCP
mid-batch — did nothing at all. All 8 invoices completed, no demotion, no audit row, no
error. The fallback ladder built that morning had not fired because nothing had gone
wrong.

**Cause.** Two separate defects, discovered in the course of explaining the first.

*One: the Razorpay MCP was mounted where no tool could reach it.* `ALLOWED_TOOLS` is an
allow-list and the gate denies everything off it, and not one Razorpay tool was on it. The
live adapter talks to `api.razorpay.com` over REST, not over MCP. So every invoice started
a Docker container, the gate refused to let the agent speak to it, and the container was
thrown away — and killing it mid-batch was killing something the run had never used. The
comment above `ALLOWED_TOOLS` said the server was "mounted for reads", which had been the
intent and was never the behaviour.

*Two: the SDK does not surface a dead mount.* Verified directly, with the probes stubbed
healthy and the image pointed at `razorpay/mcp:does-not-exist-drill`: the batch ran to
completion, concluded its invoice normally, and reported `lane: local`. No exception
reached the orchestrator. That is worse than a wasted container — it means the lane named
in the run header and stored in `BatchReport.mcp_lane` was an unverified claim, in a
system whose entire argument is that its records are trustworthy.

**Fix.** The mount now follows reachability in both directions:

- `permitted_tools(execution_mode)` adds six Razorpay **reads** — `fetch_payment`,
  `fetch_order`, `fetch_order_payments`, `fetch_all_payments`, `fetch_payment_link`,
  `fetch_tokens` — on the live lane, making the "mounted for reads" claim true. Every
  send and every create stays denied on both lanes; those run through `execute_recovery`,
  which cannot fire without a guardrail approval already on record.
- The reads are **live-only**. In simulated mode every id is one the seeder invented, so a
  fetch can only 404. The 500-invoice batch behind `docs/EVALUATION.md` therefore sees the
  identical tool set it has always seen, which is why not one committed number moved.
- `run_batch` no longer probes, and `_options` no longer mounts, a lane no permitted tool
  can reach — so a simulated run reports `off` and says why, instead of asserting `local`
  about a transport it never opened.

**And the drill was rebuilt around what is actually observable.**
`scripts/failure_drill.sh` now runs two phases on the live lane. Phase 1 kills the
transport *before* the run reaches it, by asking for a toolset the image does not have —
the exact shape of the bug above. The preflight probe catches it, the lane steps down to
remote, and the batch finishes; all three are asserted. Phase 2 is the plan's literal
`docker kill` mid-batch, and it asserts only that the cohort still completes with a full
audit trail — it does **not** require a demotion, because the SDK will not report one
unless a Razorpay call happened to be in flight. Both phases pass:

```
── phase 1 · the local MCP cannot start
  ✓ the preflight probe caught it and the lane stepped down
  ✓ the run report names the lane it finished on
  ✓ the batch finished its cohort with a complete audit trail (exit 0)
── phase 2 · the local MCP is killed mid-batch
  ✓ the transport was killed while the batch was working
  ✓ the batch finished anyway (exit 0)
  · 2 of 2 invoices concluded
```

**Lesson.** The drill was worth more for failing than it would have been for passing. A
fallback that cannot be observed to fire is indistinguishable from one that does not
exist, and "the kill changed nothing" was the only symptom that would ever have exposed
either defect. Injecting a fault you are confident about is how you find out that the
thing you were protecting was not plugged in.

---

## 2026-09-04 · The window rule wrote the wrong hour into the permanent record

**Believed.** The compliance panel was the last thing left to point a camera at, and it
worked: paste a cap-exhausted invoice, get a red `DENY` with a five-rule breakdown. While
reading one of those breakdowns to write the shot list, the window rule said
`non_peak_window: 20:37 IST is outside peak hours`. Peak is 17:00–21:30 IST. 20:37 is
inside it. Either the rule was broken or the sentence was.

**Actually true.** The sentence was. `_check_window` formatted `execute_at` with a bare
`strftime('%H:%M')` — which renders in whatever timezone the caller's datetime carries —
and then appended the literal string `IST` unconditionally. `is_non_peak` has always done
its own conversion, so **the verdict was never wrong**; only the words were. The real IST
time was 02:07, comfortably non-peak, and the approval was correct.

The reason it survived twelve days is that the two callers disagree in a way that hid it.
`ml/policy.py` enumerates candidate slots via `non_peak_window.next_slots`, which returns
IST-aware datetimes — so every one of the 428 committed `decisions` rows was formatted in
IST and is correct. `api/main.py` passes `datetime.now(UTC)`, so the compliance panel was
wrong on **every lookup it had ever served**, by 5h30m, and had been since the endpoint
was written. No test caught it because every guardrail test constructs its fixtures with
`tzinfo=IST`, where the bug is invisible by construction.

**Cost.** Twenty minutes, and only because a peak-hour string on screen contradicted a
rule I happened to know by heart. It would have cost far more on camera: the one number a
reviewer can check against a published NPCI window, wrong, in the middle of the beat
whose entire argument is that the record can be trusted.

**Changed.**

- `compliance/guardrail.py` converts to IST before formatting, for both the approval and
  the redirect sentence — and for the suggested slot in the redirect, so a redirect
  cannot name its two timestamps in two different zones.
- Two parametrized tests assert the same instant expressed as UTC, `America/New_York` and
  IST produces one identical sentence. The fixtures deliberately do *not* all use IST,
  which is the property the old suite was missing rather than a bug it failed to catch.
- Separately, `at` on `/invoices/{id}/compliance` was documented and unusable: a naive
  timestamp reached `consent_gate`, which correctly refuses to guess a timezone, and the
  panel returned a 500. It now normalises to UTC, and the dashboard exposes the control —
  which is what makes a frozen dataset answerable at the moment each invoice was live
  instead of only against a wall clock that has left every consent window behind.

**Lesson.** A string that is only ever read by a human is still an assertion, and this one
was load-bearing: `authorizing_rule` is copied verbatim into `decisions` and onto the
chip. The verdict being right is not evidence the record is right, and a rule about time
is exactly where a timezone will be assumed rather than converted. The tests all passed
because they all shared the one assumption that made the bug invisible — every fixture in
IST. Parametrizing over zones costs one line and is the only thing that would have found
it.

---

## 2026-09-04 · Two loaders, and the demo called the one that lies

**What happened.** The clean-checkout test — clone from GitHub into an empty directory,
bootstrap, run — passed every gate it was written to check. Eleven tables, the dataset
back to the same fingerprint `c32b2b063cd87707`, `python -m eval` reproducing arms B and
C exactly, and `python -m eval.report --check` confirming the committed
`docs/EVALUATION.md` is byte-for-byte what that fresh database generates.

Then the test suite ran in the clone, and three tests in `sim/tests/test_load.py` failed
that pass here:

```text
E  AssertionError: no world loaded — run `python -m sim.load` first
```

The clone had 30,210 invoices and 33,866 attempts and zero rows in `world_manifest`.

**Why.** There are two loaders. `sim/load.py` is the real one: `COPY`, one transaction,
and the `world_manifest` row written last and inside that same transaction, on the stated
reasoning that "a manifest that could be committed without them would be a claim about a
world that does not exist." `sim/generate.py` carried an older `load()` from Day 3 —
`executemany`, no manifest — and `scripts/run_demo.sh` called *that* one:

```bash
"$PY" -m sim.generate --load
```

The two drifted the moment `sim/load.py` was written, and nothing connected them, so
nothing noticed.

The consequence is not cosmetic. `agent/orchestrator.py:402` opens the batch with
`require_fingerprint()`. On a fresh clone that call raises. **`./scripts/run_demo.sh` —
the one command in the README, the Day-9 gate, the thing a judge runs — would have
seeded, trained a model, and then died at the recovery batch on a database it had just
finished loading correctly.**

This machine could not see it. The development database was loaded through `sim.load`
back on Day 5, so it has had a manifest row ever since, and every batch since has sailed
through the guard. The bug was invisible to every run on the machine that wrote it, and
visible on the first run anywhere else.

**Changed.**

- `sim/generate.py` no longer has a writer. `--load` delegates to `sim.load.load()`,
  which is the one that was always correct. Seventy-six lines of duplicate `INSERT`
  statements deleted rather than repaired — the defect was that there were two, so
  fixing the second one to match would have left the defect in place.
- Verified where it failed, not where it passes: the clone's database, manifest empty,
  `python -m sim.generate --load` → one manifest row at `v1 / c32b2b063cd87707 / 33866`,
  `require_fingerprint()` returning the fingerprint instead of raising, and all ten tests
  in `sim/tests/test_load.py` green.
- `require_fingerprint`'s docstring claimed the API calls it at startup. The API does
  not, and should not — it is a read-only view and refusing to boot would take the
  dashboard down over a condition `/health` already reports. The docstring now says what
  the code does.

**Lesson.** The gate was "one command works on a fresh clone," and it was met by every
measure that had been written down — schema, fingerprint, evaluation reproducibility, all
green — while the command itself was broken three stages in. What found it was not a
check anyone designed; it was running the *whole* suite somewhere else and reading three
failures that had no business failing. A duplicated write path is the specific shape of
this: both copies work, one is missing a line, and the one missing the line is the one
the entry point calls. The real tell was there to be read months earlier — an error
message naming a command (`run python -m sim.load first`) that the demo script does not
run.

---

## 2026-09-04 · The dashboard still introduced itself as the scaffold it came from

**What happened.** Auditing dependency licences before choosing one for the repository,
`license-checker --production` returned a clean permissive tree with a single
`UNLICENSED` row:

```text
figma-make-app@1.0.0 → UNLICENSED
```

That is not a dependency. That is this project. `dashboard/package.json` had carried
`"name": "figma-make-app"` since the export it was scaffolded from, through nine days of
rewriting, and `dashboard/AGENTS.md` opened with the same name over a description of a
project that no longer existed — "Primary application component and the usual starting
point for UI work" pointing at `src/App.tsx`, which by then was forty lines of
`HashRouter` handing off to five pages and seven components it did not mention.

**Why it survived.** Nothing reads either one. `private: true` means the name is never
published or resolved, so npm never had cause to complain; `AGENTS.md` is instructions
for whoever works on the code next, and the person working on the code already knew what
was in it. Both files are in the class of thing that is only ever wrong for someone
else. The build did not care, the tests did not care, and the one tool that did care —
a licence audit — was not a thing anyone had run before today.

**What it would have cost.** A judge opening `dashboard/package.json` on a submission
that argues for provenance and honest measurement would have read the name of a
different product. The Figma-Make origin is disclosed, and has been since Day 7
(`COMPLETION_REPORT.md` §02 and §04 both name it) — so this was never concealment. It
was worse in a smaller way: a repository that had not finished saying what it was.

**What changed.** `dashboard/package.json` and `package-lock.json` renamed to
`winback-dashboard`, with `"license": "Apache-2.0"` added; the build re-run and verified
to emit the identical bundle hash, because a rename must not be a change. `AGENTS.md`
rewritten against the actual tree — `src/pages/`, `src/components/`, `src/lib/`, the
semantic palette, the four-animation motion budget — and every claim in it checked
against the source before it was written, including the two I had got wrong on the first
pass (`hooks.ts` exports `useAsync`/`usePageVisible`/`usePrefersReducedMotion`/`useCountUp`,
not a paging hook; `ui.tsx` exports named chips, not generic shells).

`.figma/make/` stays, as a directory, without checking each file in it — right, for a
licence-audit day: deleting the whole thing because its name looks like leftovers is how
you break a build on the last day. The per-file question sat open for nine more days.
Closed below.

**The lesson.** Identity metadata is the last thing to be checked because nothing
executes it. The check that finally caught it was a question asked for an unrelated
reason — what are all the licences in this tree — which is the second time in two days
that running a tool for a *different* purpose found the defect that the purpose-built
gates could not. Broad tools find things narrow tests were never pointed at.

---

## 2026-09-04 · One imported file defended eight that were never read

**What happened.** A repo-wide cleanup pass re-opened the entry above and read it
literally: "`.figma/make/` stays... `vite.config.ts` imports `site.json` from it." True
of one file. Read the other eight — `analyze-routes`, `deploy`, `deploy-preview`,
`dev`, `dev.json`, `format`, `install`, `langserver` — and each is a thin wrapper
shelling out to Figma's own hosting CLI (`figma make deploy`, `figma-analyze`,
`pnpm dlx @vtsls/language-server`) or documenting Figma's own dev-server file-watch
config. None is imported by `vite.config.ts`, named in any `package.json` script, or
read by `scripts/run_demo.sh`. `dashboard/.gitattributes` was the same shape: Git-LFS
tracking rules for roughly eighty file extensions, in a repo that has never used LFS —
confirmed by diffing the three committed PNG pairs byte-for-byte against plain blobs,
not pointer files. So was `dashboard/pnpm-lock.yaml`: real and internally consistent,
but the tested install path has been `npm install` since Day 7 (README §06,
`AGENTS.md`), and its one remaining reader in the tree, `dev.json`'s `installOn`, was
leaving with the other eight anyway.

**Why it survived.** The Day-9 entry answered "is this directory residue," not "which
files in it are," and a directory-level *no* reads as a blanket clearance. Nobody went
back to ask the same question nine files at a time, and nothing forced the question:
`npm run build`, `run_demo.sh`, and the fresh-clone bootstrap in README §06 all pass
today without touching any of the eight, so their presence has never once failed a
check.

**What it would have cost.** Nothing functional — that's the point; unread files don't
break builds, they just sit there. What they cost is legibility on a submission whose
whole thesis is that every decision is auditable: a judge opening `dashboard/` and
finding `deploy`, `langserver`, and `.gitattributes` has to either know what Figma
Make's own CLI looks like or wonder what they're for. Neither is the impression a
compliance-and-audit project wants to make of its own repository.

**What changed.** `git rm --cached` on the eight `.figma/make/` scripts,
`.gitattributes`, and `pnpm-lock.yaml` — untracked, not deleted; all ten files stay on
disk, and `.figma/make/site.json` is untouched and still tracked. `dashboard/.gitignore`
now lists them by name with the reasoning inline, so nobody re-adds one without reading
why it left. `AGENTS.md`, this file's entry above, and `README.md` §06 corrected to
name `site.json` specifically instead of the directory, and to say `pnpm-lock.yaml` is
present but no longer committed rather than "committed too."

**The lesson.** "The directory is fine" and "every file in the directory is fine" are
different claims, and the first one only ever proved itself. A defense written at the
grain of one file should stay scoped to that file — restate it there, don't let it
stand in for its neighbours nine days running.

---

## Open

- ~~**`batch_v2` is 75/190 and resuming.**~~ **Closed — it finished, and this line was
  stale for a day.** Queried on 4 Sep while verifying the figures going onto the
  application form: `batch_v2` holds **190 invoices, 190 audit rows, ₹3,57,468 recovered,
  zero violations** — 86 recovered, 65 deferred, 22 blocked, 17 failed. It halted three
  times on the Claude account's own session limit, never on anything in this repo (at
  `inv_1448_01`, then `inv_1957_01`), and each time the halt path did what it was written
  to do: reason in the report line, unattempted invoices counted and named, resume command
  printed. The second halt is what surfaced the resume-query defect above, so it paid for
  itself. It then resumed to completion and nobody came back to say so. **The stale line
  is the finding.** An "Open" section is only worth having if closing things out of it is
  as routine as adding them, and a report that overstates what is unfinished is wrong in
  the same way as one that overstates what is done — it just fails in the flattering
  direction, which is why it survived. The Day-6 gate was already met by `batch_v1`
  (190/190, unattended, exit 0); what `batch_v2` adds is the audit coverage `batch_v1`
  cannot have, its 168 rows for 190 invoices predating the three-write-path fix and not
  being backfilled, because `audit_log` is append-only and a corrected run gets a new
  `run_id`.
- **S2S Recurring activation** — assumed unavailable. If it is granted, the live lane
  widens; the architecture does not change. Tracked in `docs/LIVE_LANE_FINDINGS.md`.

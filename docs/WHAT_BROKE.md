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

## Open

- **S2S Recurring activation** — assumed unavailable. If it is granted, the live lane
  widens; the architecture does not change. Tracked in `docs/LIVE_LANE_FINDINGS.md`.
- **`HookMatcher`'s exact shape** in `claude_agent_sdk.types` — the published example
  is loose. To be read directly from the installed package on Day 6 rather than copied
  from the docs.

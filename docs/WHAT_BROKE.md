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

## 2026-08-28 · My own tests violated the invariant I had just written

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

## 2026-08-28 · Nearly gated mandate retries on telecom consent

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

## 2026-08-28 · My probe scored a gateway route-miss as a pass

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

## Open

- **S2S Recurring activation** — assumed unavailable. If it is granted, the live lane
  widens; the architecture does not change. Tracked in `docs/LIVE_LANE_FINDINGS.md`.
- **`HookMatcher`'s exact shape** in `claude_agent_sdk.types` — the published example
  is loose. To be read directly from the installed package on Day 6 rather than copied
  from the docs.

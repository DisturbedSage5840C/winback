# Completion report

> **Live document.** Rewritten after every working prompt, not at the end. If a line
> here disagrees with the repo, the repo is right and this file is stale — say so and
> it gets fixed in the same turn.

**As of:** 4 September 2026 · **Plan day:** 9 of 10 — **Day 9 is closed except for the
video: the one-command demo works on a fresh clone, the repo is public, `v1.0.0` is
tagged** ·
**Head:** `1cd6a77` + this prompt's commit, pushed to
[`DisturbedSage5840C/winback`](https://github.com/DisturbedSage5840C/winback) (**public**) ·
**Deadline:** 5 September 2026.

**The Day-9 gate is met, and it cost a real defect to meet it.** The plan's wording was
*"One-command demo works on a fresh clone."* That was tested the only way it can honestly
be tested: this repository was cloned **from GitHub** — not from the local path, so what
is actually pushed is what got run — into an empty directory on a machine carrying no
Winback state. `bootstrap.sh` came up cold: venv, pinned deps, 11 tables, append-only
DDL, immutability tests green. The dataset regenerated to the identical fingerprint
`c32b2b063cd87707` with identical counts. `python -m eval` reproduced arms B and C
exactly. `python -m eval.report --check` confirmed the committed `docs/EVALUATION.md` is
byte-for-byte what that fresh database generates — plan §12's reproducibility gate, met
from cold. The dashboard installed and built clean on `npm install`, and the API answered
`/health` from the clone with the reader role and all 11 tables.

**Then the suite ran in the clone and three tests failed that pass here — and they were
right.** `scripts/run_demo.sh` seeded through `sim.generate --load`, a Day-3 duplicate
loader that writes the four fact tables and **not** the `world_manifest` row. The batch
opens with `require_fingerprint()` (`agent/orchestrator.py:402`). So on any machine but
this one, the single command in the README would have seeded, trained a model, and then
died at the recovery batch on a database it had just loaded correctly. It was invisible
here because this database was loaded through the correct path on Day 5 and has had a
manifest row ever since. The duplicate is deleted — seventy-six lines, not repaired, because
the defect was that there were two — and `--load` now delegates to `sim.load.load()`. The
fix was verified in the clone, where it actually failed: manifest written, guard clearing,
all ten `sim/tests/test_load.py` green. Entry 42 in `WHAT_BROKE.md`.

**Two smaller things fixed off the same test.** `run_demo.sh` validated docker, npm and
python but not the Claude credential, so a stranger with neither the CLI nor a key would
have crashed inside the batch *after* waiting through seeding and training; there is now
a preflight that fails early with the actual remedy. And both scripts accepted a schema
of `>= 8` tables while the real schema is 11 — a volume missing all four `eval_*` tables
would have passed as healthy and only complained a day later, from `python -m eval`.

**One thing in the demo script is a deliberate absence, not an oversight.** All 428
`decisions` rows carry `guardrail_verdict = APPROVE`, so there is no red DENY in the
streaming trace and none has been staged. That is the architecture working: compliance
*generates* the legal option set before the model ranks it, so an illegal action is
never on the menu. The refusal shown on camera is computed live on the compliance page,
on one of **354** genuinely cap-exhausted invoices, by the same functions the gate
calls. The script says all of this out loud rather than hoping nobody asks.

**The repo is public and `v1.0.0` is tagged.** Before tagging, every non-placeholder
value in `.env` was searched against every blob in every commit: `RAZORPAY_KEY_SECRET`
appears in none of them, `.env` was never tracked, and the only real credential in the
published tree is the Razorpay test **`key_id`** — the public half of the pair. Details
in §05.

**Two things left, and both are yours.** Record the video today; submit tomorrow, early.
§05 has both, with dates.

---

## 01 — Where the project stands

| | |
|---|---|
| Tests | **612 passing**, 0 skipped, 0 xfail — 590 across `compliance` / `sim` / `ml` / `eval` / `core` / `agent`, 22 across `api`. Also run green from a cold GitHub clone |
| Coverage | **99%** on `compliance/` — the suite a panelist reads first |
| Dataset | **frozen** at fingerprint `c32b2b063cd87707` — 4,000 mandates, 30,210 invoices, 33,866 attempts, 786 censored (2.3%) |
| Realism gate | 19 checks — **13 PASS · 6 ungraded `[REPORT]` · 0 FAIL** |
| Model | **v1 frozen** — XGBoost + sigmoid calibration, chosen out-of-fold, scored once on the held-out cohort |
| Headline honesty number | test ECE **0.034** where the merchant had data, **0.442** where it did not, still correctly ordered there |
| Headline result | **−66 compliance violations vs the naive baseline, CI [−96, −42]**, at ₹28 more legally recovered — a difference whose interval spans zero, and is reported as a tie |
| Docs | 3,697 lines across 9 files, all committed — `WHAT_BROKE.md` alone is 1,457, at 42 entries |
| Agent | full batch **190/190 unattended, exit 0**; live cohort carries real `plink_…` IDs |
| Lint | `ruff check .` clean |
| Fresh clone | cloned from GitHub into an empty directory and run cold: bootstrap green, fingerprint reproduced, `eval.report --check` byte-for-byte, dashboard built, API serving |

The thesis has not moved, and as of today it is **measured** rather than asserted:
**rupees recovered per legal attempt consumed**, reported beside compliance violations by
arm, over four arms sharing one cohort and one set of oracle seeds. The numbers are in
[`docs/EVALUATION.md`](docs/EVALUATION.md) §04–§06, generated from Postgres, and
`python -m eval.report --check` fails if the committed file ever drifts from the database.

---

## 02 — Plan map

Maps one-to-one onto `docs/IMPLEMENTATION_PLAN.md` §10 (the day plan). "Gate" is the
plan's own gate wording, and it is only ticked when the gate is actually met.

| Day | Plan date | Deliverable | Gate met? | Evidence |
|---|---|---|---|---|
| **1** | 26–27 Aug | Repo, venv, pinned deps, Postgres + schema + append-only DDL, test keys, MCP handshake, live-lane spike | ✅ | [`docs/LIVE_LANE_FINDINGS.md`](docs/LIVE_LANE_FINDINGS.md) — 11 probes, all resolved, no unknowns carried |
| **2** | 28 Aug | Six compliance modules TDD + root-cause lookup | ✅ | [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md); every rule has a test proving it **blocks**; 99% coverage |
| **3** | 29 Aug | Oracle + legacy policy + generator, dataset frozen, realism chart | ✅ | [`docs/DATA.md`](docs/DATA.md), [`docs/assets/realism.png`](docs/assets/realism.png) |
| **4** | 30 Aug | Features + XGBoost + 3-way calibration, model v1 frozen, observed-vs-censored split | ✅ | [`docs/EVALUATION.md`](docs/EVALUATION.md) §05–§07, `ml/artifacts/metrics_v1.json`, [`docs/assets/calibration.png`](docs/assets/calibration.png) |
| **5** | 31 Aug | Policy layer + four-arm paired harness + bootstrap CIs → `EVALUATION.md` | ✅ | [`docs/EVALUATION.md`](docs/EVALUATION.md) §04–§07, [`docs/assets/four_arms.png`](docs/assets/four_arms.png), `eval_runs` / `eval_arm_results` / `eval_arm_violations` / `eval_intervals` in Postgres |
| **6** | 1 Sep | Agent SDK orchestrator, `can_use_tool` gate, PostToolUse audit, MCP mode switch, both adapters, full batch | ✅ *(finished 2 Sep)* | `agent/` — `orchestrator`, `tools`, `gate`, `hooks`, `mcp_config`, `adapters/`; 112 tests. `batch_v1` **190/190 unattended, exit 0**; `live_v1` / `live_v2` carry real `plink_…` IDs; `decisions` + `audit_log` populated in Postgres |
| **7** | 2 Sep | FastAPI + Next.js — overview, worklist, drill-down on real data | ✅ *(Vite, not Next.js — see §04)* | `api/main.py` — 10 read-only endpoints over `winback_reader`, 22 tests, no mocked data anywhere. Live trace (`/runs/{id}/events`, cursored on `event_id`) and compliance panel (`/invoices/{id}/compliance`, `/compliance/window`) call `compliance/` rather than restating it. `dashboard/` (Figma-Make Vite export) delivered; Overview, Worklist, Invoice, and the live trace verified against real API shapes and fixed where they weren't (§03) |
| **8** | 3 Sep | Compliance panel + evaluation page + four animations; failure drill; rough-cut video — **MVP checkpoint** | ✅ *(finished 4 Sep)* | `dashboard/src/pages/Compliance.tsx` + `Evaluation.tsx` on live data, with an "as of" control that supplies the clock the rules are functions of; the four animations audited against `FRONTEND_SPEC.md`; `scripts/failure_drill.sh` passing both phases; `docs/DEMO_SCRIPT.md` firm, every string in it read out of the running system |
| **9** | 4 Sep | Docs, fresh-clone `run_demo.sh`, final video, repo public, tagged release | 🟡 **gate met; video + repo flip are yours** | Cloned from GitHub into an empty directory: bootstrap cold to 11 tables, dataset back to `c32b2b063cd87707`, `python -m eval` reproducing arms B and C, **`python -m eval.report --check` byte-for-byte green**, dashboard `npm install` + `npm run build` clean, API `/health` answering from the clone. The test found the manifest-loader defect that would have broken `run_demo.sh` on every machine but this one — fixed and re-verified in the clone. README rewritten (it still claimed Day 4 / 360 tests); `WHAT_BROKE.md` at 42 entries. All five plan docs present |
| **10** | 5 Sep | Buffer, submit early | ⬜ | — |

Plan sections not tied to a single day, and their state:

| Plan § | Subject | State |
|---|---|---|
| §1.1 | No "charge now" API → guarded path, two adapters | ✅ Done — `LiveRazorpayAdapter` and `SimulatedAdapter` behind one guardrail; `audit_log.execution_mode` records which ran, per row |
| §1.2 | sklearn 1.9 `FrozenEstimator` calibration | ✅ Done — that is exactly how `ml/calibrate.py` fits |
| §1.3 | `can_use_tool` as the hard gate, not a hook | ✅ Done, and the plan's caution was right — `allowed_tools` auto-approves *before* `can_use_tool`, so the money tools are deliberately left out of it. An approval is a single-use key at exact coordinates, popped not read |
| §6 | Agent layer, Claude Agent SDK | ✅ Done — four in-process MCP tools, `max_turns=6` per item, `setting_sources=[]`, `payment_link_notify` kept out of `allowed_tools` |
| §3.1 | Deterministic counterfactual oracle | ✅ Done — seed excludes `run_id` and `arm`, so arms are genuinely paired |
| §3.2 | Biased legacy policy censors the training data | ✅ Done, and the finding it produced is stronger than the one planned (see §03 below) |
| §3.3 | Four-arm paired evaluation | ✅ Done — design frozen in `EVALUATION.md` §01–§03 **before** results existed, then executed against it unchanged; 10,000-resample cluster bootstrap over subscriptions, differenced inside each resample |
| §4 | Compliance layer, zero LLM | ✅ Done — six modules + composing guardrail, pure functions |
| §5 | ML pipeline | ✅ Done end to end — features, XGBoost, three-way calibration, and the **decision policy** (`ml/policy.py`: argmax expected ₹ over legal action×slot candidates, under the remaining attempt budget) |
| §7 | Schema, append-only, PII redaction | ✅ Done — `REVOKE` + triggers, verified against the owner role |
| §8 | Front end | ✅ Done — five pages on real data, the four-animation motion budget honoured and no more, `prefers-reduced-motion` respected. Stack is Vite + React 19, not the Next.js the plan named; the docs now say so |
| §11 | 5-minute video | Script firm (`docs/DEMO_SCRIPT.md`) — shot list, the verbatim 2:45 beat, and an explicit list of what is *not* claimed on camera. Recording is Day 9 and is yours (§05) |

---

## 03 — What is complete, in detail

**Foundation.** Python 3.14 venv with a pinned lockfile; Postgres 17 in Docker with the
full schema; `core/config.py` refuses any key that is not `rzp_test_…` at startup, so a
live key cannot be loaded by accident. `.env` is gitignored and confirmed absent from
GitHub.

**The audit trail is genuinely append-only.** Not a convention — `REVOKE UPDATE, DELETE`
plus `BEFORE UPDATE OR DELETE` and `BEFORE TRUNCATE` triggers that raise, verified
against the owner role, 20 tests. This is a ten-second answer to the question a panelist
will ask.

**Six compliance rules + the guardrail that composes them**, written test-first: NPCI
1+3 cap, non-peak window (peak IST 10:00–13:00 and 17:00–21:30), AFA thresholds, consent
/ DND, pre-debit notice, and the deterministic TD/BD root-cause lookup. Every rule has a
boundary test that proves it **blocks**, including with the model asking for the
opposite. None of it is an LLM judgment call.

**The live lane is scoped, not guessed.** Eleven probes against a real test account,
each resolved to a verdict; what the API actually permits is written down rather than
assumed. Payment links are created with `notify: {sms:false, email:false}` — a real
Razorpay artifact with the send stubbed, which is the honest answer to TRAI DLT.

**The world simulator is a counterfactual oracle, and it is checked.** Outcomes are
seeded from `sha256(channel, subject_id, invoice_id, attempt_number, action, slot)` —
no `run_id`, no `arm` — so every arm sees the same coin flips and the comparison is
paired. `sim/validate_realism.py` grades it against published NPCI/industry figures:
13 graded checks pass, and six checks are deliberately left ungraded because no
published band exists to grade them against. The file's own rule is *no band without a
source*, and one graded check currently sits at 3.6 against a ≥3 floor — disclosed in
`DATA.md` §05 rather than made comfortable by lowering the band.

**Model v1 is frozen and the selection is defensible.** Three calibrators fitted on the
calibration split; **isotonic scored best and was disqualified anyway**, because
clamping to its outermost knots makes it assert exact 0.0 and 1.0 off-distribution —
111 of 118 censored rows at literal zero. Sigmoid ships. Held-out metrics were computed
once.

**The censoring finding changed on Day 4, and the replacement is stronger.** The Day-3
claim that the suppressed region was *easier* turned out to be a generator bug: the
shadow dunning branch ran the whole schedule unconditionally while the observed branch
stops at the first capture. Fixed, dataset re-frozen, and the honest figures are 61.8%
vs 58.2% — near-identical. The bias is not in the rate, it is in the covariates: the
censored region is cheap, netbanking, and early in a mandate's life. **A merchant
watching their recovery rate would never catch it, because nothing is wrong with the
rate.** That reads consistently in five places now.

**The four-arm evaluation ran, and its headline is a tie that is being reported as a
tie.** Arms A/B/C/D replay the same 190 failed invoices in the held-out test cohort
against the same oracle seeds — the seed key deliberately excludes `run_id` and `arm`, so
the coin flip for any `(attempt, action, hour)` is identical across arms and the
comparison is genuinely paired. Winback recovers ₹6,39,626 legally; retry-everything
recovers ₹6,39,598. The paired difference is **+₹28, CI [−₹2,697, ₹2,781]** — it contains
zero, and `EVALUATION.md` §07 says in bold that Winback does not beat retry-everything on
rupees. What separates them is **66 compliance violations against zero, CI [−96, −42]**.
The naive policy reaches the same money by a route a merchant cannot ship.

**A test now prevents that tie from quietly becoming a win.**
`test_the_money_claim_against_retry_everything_is_a_tie_and_must_stay_one` asserts the
interval still spans zero. If a later change makes Winback look better on money, that
test fails and someone has to decide deliberately whether the improvement is real or
whether the harness has started flattering the submission.

**Measurement separated two baselines that a violations count alone would have made look
alike.** All 66 of arm B's violations are `bd_hard_not_retryable` — re-presenting
mandates the bank has permanently declined — and they recovered **₹0**. Arm C's 120
violations are mostly `peak_window`, and they recovered **₹5,04,247: 90% of everything
arm C appears to collect.** Held to the law, the legacy policy collects ₹53,490 against
Winback's ₹6,39,626. That is the strongest single fact in the evaluation, and it exists
only because recovered rupees are attributed per violating presentment rather than
counted in aggregate.

**The advantage survives off-distribution.** In the censored region — under ₹500 or on
netbanking, where the legacy policy never retried and the model therefore has no training
labels — B and D recover the identical ₹80,484, but D uses 57 legal attempts against 59
and commits 0 violations against 27. A policy that picks the best of several scored
candidates, rather than thresholding a probability, is the design that should survive a
region where the probabilities are known to be badly calibrated. It did.

**The one unmeasurable parameter was tested for how much it matters, and the answer is
almost nothing.** The nudge's true effect cannot be measured without sending real
messages. Across worlds where it does nothing (multiplier 1.00) and where it works far
harder than the policy assumes (0.40), with the policy's belief held deliberately wrong
at 0.80 throughout, Winback's legally-recovered total moves ₹1,087 — 0.17%.

**`EVALUATION.md` is generated where it states numbers and hand-written where it draws
conclusions,** and the split is stated at the top of the file. `python -m eval.report`
rewrites only the region between two markers; `--check` exits 1 on drift; rewriting an
unchanged file is a verified no-op. Nineteen tests in `eval/tests/test_report.py` cover
the whole path — persist, read back, render — because the claim "every number here came
out of the database" is a claim about all three.

**The agent runs a full cohort unattended, and the gate is structural.** `batch_v1`
worked all 190 at-risk invoices in one run, exit 0, no supervision. The money gate is
`can_use_tool`, not a prompt: `execute_recovery` and `simulated_notify` are held out of
`allowed_tools` precisely because `allowed_tools` auto-approves *before* the permission
callback ever runs. An approval is filed under exact coordinates — `invoice_id | action |
execute_at` — and **popped**, so one guardrail call authorises exactly one presentment at
exactly the slot it was granted for. A near-miss is a miss; the gate does no rounding,
because a rounded timestamp is how a peak-window presentment gets through.

**The live lane produces real Razorpay artifacts.** `live_v1` and `live_v2` ran against
the test account and wrote real `plink_…` IDs into `audit_log.razorpay_entity_id`, with
`notify: {sms:false, email:false}` — the link is real, the send is stubbed, the consent
gate is real. `payment_link_notify` is excluded from the agent's tools entirely: it is the
one call that could deliver a message around that gate.

**Four defects were found in the agent layer and all four are in `WHAT_BROKE.md`.** The
sharpest is that `--live` had never once run live — a `Literal` compared with `is`
against a `StrEnum` member, and the existing test passed because it injected the enum the
CLI never produced. The second is that the audit trail recorded only what the batch *did*:
184 decisions, 156 rows, and complete silence about the 118 invoices ruled out on a rule.
The third is an invoice that concluded in prose and nowhere else — the agent obtained a
guardrail approval, narrated it, and exhausted its turn budget before spending it. That
one is now a named `stop_reason`, `approval_granted_not_spent`, kept distinct from
`no_conclusion_reached`, because filing an unspent approval as a compliance stop would be
a lie in the merchant's favour.

**Every invoice a batch concludes leaves a row.** Three write paths — the `PostToolUse`
hook for actions and refusals, `record_conclusion` for write-offs and escalations that
call no tool, and `record_silence` for the invoices where no tool ran at all. `batch_v2`
demonstrates it: 75 invoices concluded, 75 audit rows, and the only decision without
one is the item a session limit killed mid-flight — which the resume now re-works rather
than skipping.
`batch_v1`'s 168 rows for 190 invoices predate the fix and are **not** being backfilled —
`audit_log` is append-only, so a corrected run gets a new `run_id`.

**The read-only backend is real and cannot write.** Ten `GET` endpoints over
`winback_reader`, a role holding `SELECT` and nothing else; a test asserts the app's verb
set is a subset of `{GET, HEAD}`, and another proves an `INSERT` through that connection
raises `InsufficientPrivilege`. No number is computed in Python that the database can
compute — the funnel is the `recovery_funnel` view verbatim, the evaluation is the same
`eval_*` rows `EVALUATION.md` is generated from — so the dashboard and the committed
report cannot disagree. Two defects surfaced immediately and are recorded: the headline ₹
figure was serialised as a **string** (`sum()` → `numeric` → `Decimal` → JSON string,
which JavaScript would have concatenated rather than added), and the worklist emptied
itself whenever a batch succeeded, because the view filtered `status = 'at_risk'` and the
agent's own actions move invoices off it.

**The compliance panel asks the rules; the live trace cursors on an id.** Two endpoints
finish the non-frontend half of Day 7. `/invoices/{id}/compliance` imports `compliance/`
and calls the same pure functions the agent calls — NPCI cap, window, AFA ceiling,
consent, pre-debit notice — and composes the full guardrail for *both* a proposed retry
and a proposed nudge, because the two are governed by different rules and an invoice can
be un-retryable and contactable at once. A panel that recomputed the 1+3 cap in
TypeScript would be a second implementation of the law, free to drift from the one that
gates the money. `/runs/{id}/events` streams the audit trail cursored on the `BIGSERIAL`
`event_id`, never on a timestamp: two rows can share a microsecond, and a client resuming
from `ts_utc` would either skip a decision or show it twice — on camera. Each event
carries the `authorizing_rule` from its decision, because a blocked presentment with the
rule that blocked it is the demonstration; without it, it is a shrug.

**Two more defects, both found by running the code rather than reading it.** The resume
query had come to mean the opposite of what it said: it unioned `decisions` with
`audit_log` so a write-off would not be re-worked, but once `record_conclusion` and
`record_silence` guaranteed a row for every conclusion, a decision with no audit row
could only mean an item that died between the guardrail's answer and the tool call — the
one case that must be re-worked. A session limit produced exactly that, and the invoice
would have been stranded with an approval on record, no action, and nothing explaining
the silence. And the compliance panel embedded the window snapshot by calling the window
*endpoint* as a function, which handed a `Query` object where an `int` was expected;
FastAPI resolves those per request, so the snapshot now lives under both handlers rather
than through one.

**`docs/WHAT_BROKE.md` is 1,238 lines written as it happened**, including the two Day-4
generator bugs, the calibrator that scored best and had to lose, a band I invented and
then withdrew, the arm-B window violations the docstrings predicted and the data refused
to produce, a monkeypatch that silently did nothing and let two tests pass for the wrong
reason, and the chart defects that only failed when the PNG was actually looked at.
Razorpay scores Failure Recovery explicitly; this file is not being written on Day 9.

**The delivered frontend had five real defects, each the same shape, and four are now
fixed.** `frontend.zip` — the Figma-Make export — assumed API response shapes rather
than reading them off the running backend, and every page that touched a non-trivial
endpoint was wrong in the same way types.ts had guessed at a field name the backend
never returns:

- **Worklist** expected `{items, total, limit, offset}`; the real `/worklist` and
  `/runs/{id}/worklist` return `{total, rows}` with no echoed `limit`/`offset`. Fixed
  in `lib/api.ts`'s `getPage`.
- **Evaluation** expected `run` as a bare string and a `unit`/`verdict_label` pair on
  each interval row that the API doesn't send; the real shape is a full `EvalRun`
  object and `IntervalRow` carries `run_id`/`arm`/`resamples`/`confidence` instead.
  Fixed in `types.ts`, `pages/Evaluation.tsx`, and (since it shares the type) the demo
  fallback fixtures in `data/demo.ts`.
- **Invoice** was the largest: the frontend's `InvoiceDetail` was flat and invented,
  where `/invoices/{id}` actually nests everything under `{invoice, attempts,
  decisions, audit_trail}` with renamed fields throughout (`kind`/`p_success`/
  `execute_at`/`stop_reason` on scored candidates instead of `action`/
  `calibrated_prob`/`slot`/`refusal_reason`; `ts_ist`/`action_taken` on audit rows
  instead of `timestamp_ist`/`action`). Rewrote `types.ts`, `pages/Invoice.tsx`'s
  header/`DecisionCard`/`AuditTrail`, and both demo fixtures to match, byte-for-byte
  against a real `curl`.
- **`RootCauseChip` crashed on a real, correct data point.** Attempt 2 of invoice
  `inv_0667_01` genuinely has `root_cause_class: null` — not every attempt has been
  classified yet — but the chip's lookup table was unconditional and indexing it with
  `null` threw `Cannot read properties of undefined`. The frontend's non-nullable
  assumption was wrong, not the backend's data; fixed by rendering a neutral "—" for
  the null case rather than loosening the (correctly non-nullable) `WorklistRow` type,
  which 155 real rows confirm never sends null there.
- **The live trace 422-looped on every first poll.** `GET /runs/{id}/events` takes
  `since: int | None` — FastAPI resolves an *omitted* param to `None` but 422s on the
  literal empty string `''`. `getEvents()` built `since=${since ?? ''}`, which sends
  the empty string on exactly the first poll of every run. Fixed by building the query
  string with `URLSearchParams` and omitting `since` entirely when null, the same
  pattern the `outcome` param already used. The same page had a second, quieter defect
  once the network call worked: `TraceEvent` used invented field names (`seq`,
  `action`, `at_ist`) where the real stream sends `event_id`, `action_taken`,
  `ts_ist`, plus several fields the type dropped entirely (`channel`,
  `execution_mode`, `razorpay_entity_id`, `calibrated_prob`, `expected_value_paise`).
  Fixed in `types.ts`, `LiveTrace.tsx`, and the demo fixture. Verified live against
  `run_id=live_v2`: the trace streams and reaches "Caught up · 11 events" with zero
  console errors.

Compliance was checked against the real `/invoices/{id}/compliance` and
`/compliance/window` responses and found already correct — no fix needed there. Each
fix was verified three ways: `tsc --noEmit` clean, `vite build` clean, and a real
Playwright browser session against the live backend with zero console errors (a hard
reload, not HMR — Vite's HMR was observed to leave a stale error boundary in the tab
even after the dev server served corrected source).

**`docs/FRONTEND_SPEC.md` is the frontend contract, written and complete.** Every page,
every endpoint each pixel reads, the exact response shapes checked against the live
database (not guessed), the palette, the ten-endpoint reference table, the four-animation
motion budget, the polling contract for the live trace, and a build-order master prompt
at the end. This is deliberately a spec, not code — you scoped Day 7–8 frontend work as
yours to build (or to hand to a fresh session via the master prompt); this file is what
makes that buildable without re-deriving the API shapes from scratch.

**Day 8 — the MVP checkpoint, and the two defects it turned up.**

The gate work itself was unglamorous and is done: `dashboard/src/data/demo.ts` deleted
rather than left orphaned (a deleted file cannot be imported back by accident, an
unused one can); a `/health` badge in the header so a dead backend is visible before
it is embarrassing; responsive and dark-mode passes; page title and meta corrected off
the Figma-Make defaults; the four animations checked against the motion budget in
`FRONTEND_SPEC.md` — ₹ count-up, funnel stagger, trace row flash, drawer slide, and
nothing else. `scripts/run_demo.sh` brings the whole stack up with one command.

**The failure drill is a script, not a story.** `scripts/failure_drill.sh` runs two
phases: a clean baseline cohort, then the same cohort with the local MCP container
killed mid-run. The batch finishes both times with a complete audit trail. Getting
there exposed that the fallback ladder was believing its own configuration — a stdio
mount that had never come up still looked healthy — so `mcp_config.py` now demotes
local → remote → off with the reason carried in the lane description and written to
the audit row. It also produced the `TOOLSETS` finding now in `LIVE_LANE_FINDINGS.md`
§04.3: `razorpay/mcp` reads a comma-joined value as a *single* toolset name and
refuses to start, so the separator is a space, and commas are normalised in code so a
stale `.env` cannot silently disable local mode. The script says plainly in its own
output that phase 2 does not always write an `mcp_degraded` row — the SDK does not
surface a dead stdio mount unless a call is in flight — because a drill that
overstates what it proved is worse than no drill.

**The compliance panel's `at` parameter existed and had never worked.** Supplying a
moment is the whole point of that control: the rules are pure functions of facts and a
clock, so handing them the clock is how a reviewer asks *what would the guardrail have
said while this invoice was live* without a second implementation to trust — and the
frozen dataset's newest `charge_at` is 23 Aug, so against the wall clock every consent
and transactional window has expired. A hand-typed timestamp carries no offset,
reached `consent_gate`, which correctly refuses to guess a timezone, and returned a
500. It is now normalised to UTC — the only defensible reading in a system whose every
stored column is UTC — and surfaced in the panel as an "as of" control with a button
that jumps straight to the invoice's own charge moment.

**And underneath it, the one that mattered: the window rule wrote the wrong hour into
the permanent record.** `_check_window` called `strftime` on whatever timezone the
caller passed and then appended the literal string "IST". The *verdict* was never
wrong — `is_non_peak` always did its own conversion — but that sentence is copied
verbatim into `decisions.authorizing_rule` and rendered on the compliance chip, so
every API lookup since the endpoint was written produced a permanent record naming an
hour 5h30m off, about the one rule that is entirely about the hour. The batch was
unaffected: `ml/policy.py` passes IST-aware slots, so all 428 committed rows are
correct. Every guardrail test fixture was also IST-aware, which is precisely why
nothing caught it — the bug was invisible by construction. Fixed by converting before
formatting in both branches and for the suggested slot, and pinned by two tests
parametrized over UTC, `America/New_York` and IST. Both defects are in
`WHAT_BROKE.md` under 4 Sep.

**`docs/DEMO_SCRIPT.md` is firm, and one beat in it changed on contact with the data.**
The plan's marquee moment was a red DENY flashing in the live agent trace. There is no
such row: all 428 `decisions` carry `guardrail_verdict = APPROVE`. That is not the gate
failing to fire — compliance *generates* the legal option set before the model ranks
it, so an illegal action is never on the menu, and `agent/tests/test_gate.py` proves
the gate underneath still denies. Rather than stage one, the refusal is now shown where
it is real: the compliance page computes it live on `inv_3890_01`, one of **354**
invoices that have genuinely used all four attempts, where four of five rules approve
and the root-cause rule says in words *"TD may be retried"* — and the answer is still
DENY, because the budget is spent. The script carries that beat verbatim, the five
rigour numbers already chosen, and a section titled "What is deliberately *not* in this
video" listing every claim not being made on camera.

---

## 04 — What is left, for me

In order. Each line is the plan's gate, not a vague intention.

**Both items carried out of Day 5 are now closed.** The dataset loader (`sim/load.py`)
populates `customers` / `subscriptions` / `invoices` / `payment_attempts` from the frozen
seed, because the API reads tables and not a generator; and the per-presentment
`decisions` and `audit_log` rows now exist under `run_id` and `arm`, which is what gives
the drill-down something to drill into.

**Day 7 — closed.** Delivered as a Vite/React build rather than Next.js; overview,
worklist, invoice drill-down and the live trace verified against real API shapes.
The last mock in the tree — `dashboard/src/data/demo.ts` — is deleted, not merely
unused, so the "no mocks anywhere" gate is enforced by the compiler and not by
discipline. A header health badge polls `/health`, because a red badge on camera is
cheaper than a silent empty page.

**Day 8 — closed, and the MVP checkpoint is green.** Compliance panel and evaluation
page live on real rows; the four motion-budget animations audited against
`FRONTEND_SPEC.md`; responsive and dark-mode checked; the deliberate failure drill
written as `scripts/failure_drill.sh` and passing both phases; `docs/DEMO_SCRIPT.md`
finished to a shot list with the exact strings to be shown. The compliance panel's
`at` control now works, which is what makes a frozen dataset answerable at the moment
each invoice was live — without editing a row.

**Reconciled.** `ARCHITECTURE.md` §06 and `FRONTEND_SPEC.md` now describe the shipped
stack (Vite + React 19 + TypeScript + Tailwind v4 + Framer Motion + `HashRouter`),
not the originally-specified Next.js App Router.

**Day 9 — the gate is met; what is left of it is yours.** The fresh-clone test ran from
a GitHub clone in an empty directory and is described in the header — including the
manifest-loader defect it found, which would have broken the one command in the README
on every machine except this one. Also closed today: the README rewritten (it was still
advertising "Day 4 of 10, 360 tests passing" with Days 5–8 unchecked, which understated
four finished days to the first person who reads the repo) and now carrying the result
table, the honest tie-on-money framing, a "what broke" section, and the fresh-clone
evidence; a Claude-credential preflight in `run_demo.sh`; the schema floor corrected from
8 to 11 in both scripts; and the carried-in Day-8 documentation item — `npm install` is
the tested path, now stated in the README **and** verified from the clone
(`npm install` + `npm run build`, 0 vulnerabilities, clean build).

**Day 9 is closed.** The repo is public, the history was swept for secrets before tagging
(§05), and **`v1.0.0` is tagged and released** at the commit that met the fresh-clone
gate. The only Day-9 item still open is the **video**, which is yours — §05.

**Day 10 — buffer.** Submit early in the day, not at the wire.

**Not being done, and deliberately:** the stretch lane (checkout-abandonment). §06
recorded on Day 5 that the two-day buffer was spent; the plan's cut order puts stretch
lanes first, and the Day-8 gate is met without them.

---

## 05 — What is left, for you

Ordered by date. **The repo flip is done. Two items remain, one of them today.**

| When | What | Why it has to be you |
|---|---|---|
| ~~**Day 9 · 4 Sep**~~ | ~~**Say the word to make the repo public.**~~ ✅ **Done — [`DisturbedSage5840C/winback`](https://github.com/DisturbedSage5840C/winback) is `PUBLIC`.** Secret sweep run against every commit in the history before tagging: `RAZORPAY_KEY_SECRET` appears in **no commit**, `.env` was **never tracked**, and the only real credential value anywhere in the tree is the Razorpay test **`key_id`** in `LIVE_LANE_FINDINGS.md` — which is the public half of the pair, test-mode, and checkout-facing by design. The `WINBACK_DB_URL*` passwords in `.env.example` are `*_dev` literals bound to `localhost:55432` and matching `docker-compose.yml`; they have to be there for `bootstrap.sh` to work and they authorise nothing off this machine. 164 tracked files, no data dumps, no PII — the customer data is synthetic and is not committed at all. | Publication decision |
| **Day 9 · 4 Sep** | **Record the 5-minute video.** `docs/DEMO_SCRIPT.md` is now firm, not an outline: an eight-row shot list with what to have open in each beat, the 2:45 refusal written out verbatim with the exact invoice id to type, the five numbers to say in the rigour beat, and a section naming what is deliberately *not* claimed on camera. Bring the stack up with `scripts/run_demo.sh` and check the health badge is green before you start. `ffmpeg` is not installed — QuickTime screen capture is the path. | Your voice, your screen |
| **Day 10 · 5 Sep** | **Submit the application** at razorpay.com/buildathon, with whatever student-eligibility proof the form asks for. Track 03. Early in the day. | It is your application |

One small decision, whenever you get to it:

- **`docs/index.html` is in the working tree and I did not write it.** 287 lines, a
  generic dark gradient on a system font stack — none of the Satoshi / `#0e0b08` /
  `#305eff` language the plan settled on. It is **excluded from every commit** until you
  say what it is. Delete it, or tell me to replace it when the frontend block starts.

Optional, and none of it blocks the build:

- **Install `ffmpeg`** if you want to edit the video programmatically rather than in
  QuickTime. Decide before you record — after is too late to matter.
- **MCP connectors.** `figma`, `atlassian` and `supabase` need an OAuth flow this
  session cannot run, and the `github` MCP server fails to connect (badly formatted
  Authorization header). **None of them are needed** — this is a Python/Postgres
  project and git + the `gh` CLI cover everything the GitHub server would. Fix them
  only if you want them for other work.
- **S2S Recurring Payments activation.** Requesting it from Razorpay support is the
  only way to get a real "charge this subscription now" call. The plan already assumes
  this will not land inside ten days (§13, risk one) and the two-adapter architecture
  exists precisely so it does not matter. Not recommended, listed for completeness.

Already done by you, so it never comes back: Razorpay test keys provided, Docker
running, the four standing decisions (test keys · maximum evaluation rigour · best-of-all
front end on Razorpay's motion language · 8+ hrs/day).

---

## 06 — Risks, live

| Risk | Standing response | Changed? |
|---|---|---|
| Model does not beat retry-everything on raw ₹ | Headline is ₹/legal-attempt; arm B is disqualified on legality, decided before results existed | **Realised, exactly as anticipated.** The money difference is +₹28 with an interval spanning zero. The pre-committed response is the one now in `EVALUATION.md` §07, and a test keeps the tie a tie |
| Simulator circularity challenged in the panel | Raised first, in the README and on camera, with the observed-vs-censored ECE gap (0.034 vs 0.442) as evidence it was taken seriously | Stronger now than planned — the covariate finding is a better example than the one it replaced |
| Next.js eats Days 7–8 | Overview, worklist, drill-down and the compliance panel are mandatory; the evaluation page may degrade to committed PNGs | **Closed, and the fallback was never needed.** Both days landed inside their gates and the evaluation page renders live from the API rather than from PNGs. The risk was mis-named: it was never the framework — the delivered stack is Vite, not Next.js — it was that the frontend's types were guessed rather than measured, which is what actually cost the time |
| `HookMatcher` shape differs from published docs | Day 6: read the installed types. `can_use_tool` is the load-bearing gate; audit hooks can fall back to wrapping the adapter | **Closed.** Read `claude_agent_sdk.types` directly and the loose part of the docs was real: `matcher=None` matches every tool, `tool_response` arrives as a bare list, and exceptions raised inside a `PostToolUse` hook are swallowed. All three are in `WHAT_BROKE.md`. No fallback needed |
| A batch halts part-way through | `audit_log` is the checkpoint; re-running the same `--run-id` resumes rather than restarting, which is a correctness property before a convenience one — NPCI counts presentments, not batches | **Realised twice and it worked both times.** Once when the process was killed, twice on the account's session limit. `batch_v2` resumed from 24, from 50 and from 75, with the halt reason and the resume command in the report line each time. The third halt paid for itself: it exposed a resume-query defect that would have stranded an approved invoice |
| Time overrun | Cut order: stretch lanes → dashboard polish → animations → live cohort. Never compliance tests, audit trail, or `EVALUATION.md` | **Worse than yesterday.** The two-day buffer was spent on Day 5 and the schedule now has no slack. The cut order stands and the stretch lane is, in practice, gone |

---

## 07 — Update log

Newest first. One entry per working prompt.

| # | Date | What changed |
|---|---|---|
| 10 | 4 Sep 2026 | **Repo public; history swept for secrets; `v1.0.0` tagged and released.** Going public makes every commit readable, so the sweep ran before the tag rather than after: each non-placeholder value in `.env` was searched against **every blob in every commit** by a script that never put a secret on a command line or printed one. Result — **`RAZORPAY_KEY_SECRET` is in no commit**, `.env` was never tracked in any commit, and the only real credential value published anywhere is the Razorpay test **`key_id`** in `LIVE_LANE_FINDINGS.md`, which is the public half of the pair and checkout-facing by design. The `WINBACK_DB_URL*` passwords in `.env.example` are `*_dev` literals bound to `localhost:55432`, matching `docker-compose.yml` — required for `bootstrap.sh` and authorising nothing off this machine. Published surface audited too: 164 tracked files, largest are `ml/artifacts/` and the three chart PNGs, no data dumps and no PII (the customer data is synthetic and is not committed at all). `v1.0.0` tagged at this commit — the tree that met the fresh-clone gate, plus this report. |
| 9 | 4 Sep 2026 | **Day 9 gate met — and the fresh-clone test earned its place by failing.** The repository was cloned **from GitHub** (not the local path, so the thing tested is the thing pushed) into an empty directory on a machine with no Winback state, with the development database dumped to insurance first and brought down without `-v` so its volume survived. Cold results: `bootstrap.sh` exit 0 — venv, pinned deps, 11 tables, append-only DDL, immutability tests; dataset regenerated to the identical fingerprint `c32b2b063cd87707` and identical counts (4,000 / 4,000 / 30,210 / 33,866); `python -m eval` exit 0 with arms B and C reproducing exactly; **`python -m eval.report --check` → "matches the database"**, which is plan §12's byte-for-byte reproducibility gate met from cold; `npm install` + `npm run build` clean with 0 vulnerabilities; API `/health` answering from the clone with the `winback_reader` role and 11 tables. *Then the suite ran there and three `sim/tests/test_load.py` tests failed that pass here.* **There were two loaders.** `sim/load.py` writes the four fact tables **and** the `world_manifest` row in one transaction; `sim/generate.py` carried a Day-3 duplicate that wrote the tables and not the manifest — and `run_demo.sh` called that one. The batch opens with `require_fingerprint()`, so on any machine but this one the single command in the README would have seeded, trained a model, and died at the recovery batch on a database it had just loaded correctly. Invisible here only because this database was loaded by the correct path on Day 5. The duplicate is **deleted** — 76 lines, not repaired, because the defect was that there were two — and `--load` delegates to `sim.load.load()`; verified in the clone where it failed: manifest written at `v1 / c32b2b063cd87707 / 33866`, `require_fingerprint()` returning instead of raising, all ten tests green. Two more fixes off the same test: a **Claude-credential preflight** in `run_demo.sh` (it validated docker, npm and python but not the one credential the batch needs, so a stranger crashed *after* seeding and training), and the schema floor corrected **8 → 11** in both scripts (a volume missing all four `eval_*` tables passed as healthy and only complained a day later). `require_fingerprint`'s docstring claimed the API calls it at startup; the API does not, and should not — corrected rather than wired up, because a read-only view refusing to boot would take the dashboard down over a condition `/health` already reports. **README rewritten** — it still said "Day 4 of 10, 360 tests passing" with Days 5–8 unchecked, understating four finished days to the first person to read the repo; it now carries the four-arm result with the money reported as the tie it is (₹28, interval containing zero) and the legality as the result that is not (66 violations against 0), a "what broke" section, and the fresh-clone evidence including the defect. `WHAT_BROKE.md` entry 42. |
| 8 | 4 Sep 2026 | **Day 7 and Day 8 both closed; the MVP checkpoint is green.** *Gate work:* the last mock (`dashboard/src/data/demo.ts`) deleted rather than orphaned, so "no mocks anywhere" is compiler-enforced; a `/health` badge in the header; responsive and dark-mode passes; the four animations audited against `FRONTEND_SPEC.md`; page title and meta corrected; `ARCHITECTURE.md` §06 and `FRONTEND_SPEC.md` reconciled to the shipped Vite stack. *Resilience:* `scripts/run_demo.sh` (one-command bring-up) and `scripts/failure_drill.sh` (two-phase: a clean baseline, then the local MCP killed mid-run) both written and passing; the MCP fallback ladder now demotes local → remote → off with the reason recorded, after a mount/reachability defect that made a dead stdio server look healthy; `LIVE_LANE_FINDINGS.md` §04.3 records why `TOOLSETS` must be space-separated (`razorpay/mcp` reads a comma-joined value as one toolset name and refuses to start). *Two defects, one of them serious:* `GET /invoices/{id}/compliance?at=` returned a 500 on every call — a naive timestamp reached `consent_gate`, which refuses to guess a timezone — now normalised to UTC and exposed in the panel as an "as of" control, which is what makes the frozen dataset answerable at the moment each invoice was live; and **the window rule `strftime`'d in the caller's timezone and labelled it "IST" unconditionally**, so every API lookup wrote a permanent `authorizing_rule` naming an hour 5h30m off, about the one rule that is entirely about the hour. The verdict was always correct and every test fixture was IST-aware, which is why nothing caught it — now converted before formatting, with two tests parametrized over three zones. *Demo:* `docs/DEMO_SCRIPT.md` rewritten from a 27-line outline to a firm shot list, with the 2:45 refusal written out verbatim (`inv_3890_01`, four APPROVEs against one DENY) and a section stating what is deliberately **not** claimed on camera — there is no red DENY in the batch trace, because all 428 `decisions` rows are APPROVE by design, and none was staged. Two `WHAT_BROKE.md` entries. 612 tests pass, `ruff` clean, `tsc --noEmit` and `vite build` clean. |
| 7 | 3 Sep 2026 | **Frontend delivered (`frontend.zip`, Vite not Next.js) and hardened page by page against the live API.** Five real defects found and fixed the same way each time — curl the real endpoint, diff against the frontend's assumed TypeScript types, correct types + components + demo fixtures, verify with `tsc`/`vite build`/a real Playwright browser: Worklist's `{items,total}` vs the real `{total,rows}`; Evaluation's bare-string `run` and stale `unit`/`verdict_label` fields; Invoice's fully-nested `{invoice,attempts,decisions,audit_trail}` shape with renamed fields throughout (largest single fix); `RootCauseChip` crashing on a genuinely-`null` (not-yet-classified) `root_cause_class`; the live trace's `since=''` 422-looping on every first poll (fixed by omitting the param when null, `URLSearchParams`-built like the existing `outcome` param) plus its own separately-invented `TraceEvent` field names. Compliance page checked and found already correct. CORS opened for the Vite dev port (8443). `api/main.py` CORS change plus the full `dashboard/` tree committed. |
| 5 | 3 Sep 2026 | **Day 7, non-frontend half complete.** Three endpoints added: `GET /runs/{id}/events` (live trace, cursored on the `BIGSERIAL` `event_id`, each event joined to its `authorizing_rule`), `GET /invoices/{id}/compliance` (every rule's verdict plus the composed guardrail for a retry *and* a nudge, by calling `compliance/` rather than restating it), `GET /compliance/window` (peak/non-peak, countdown, next legal slots). Six new tests. Two defects fixed: `_already_worked` unioned `decisions` with `audit_log`, which — after `record_conclusion`/`record_silence` guaranteed a row per conclusion — inverted its own meaning and would have permanently stranded an invoice killed between its guardrail approval and its tool call (a session limit produced exactly one); and the panel called the window endpoint as a plain function, receiving FastAPI's `Query` default object instead of an `int`. Two `WHAT_BROKE.md` entries, `ARCHITECTURE.md` §05 rewritten. `batch_v2` halted a second time on the account session limit at 75/190 and is resuming. 609 tests pass, `ruff` clean. |
| 4 | 2 Sep 2026 | **Read-only backend + two defects it exposed.** `api/main.py` — 7 `GET` endpoints over `winback_reader`; `api/tests/test_main.py`, 16 tests, no mocked database. Fixed: the headline ₹ figure serialised as a JSON **string** (`Decimal` from `sum()`), and `exception_worklist` filtering `status = 'at_risk'` so a run's worklist emptied itself the moment the run succeeded — the view now exposes `invoice_status` and the caller filters, with a new `GET /worklist` as the live queue. Two `WHAT_BROKE.md` entries; `ARCHITECTURE.md` §05 added. `batch_v2` halted at 50/190 on the account session limit (resets 7pm IST) — resumable, not a defect. 603 tests pass, `ruff` clean. |
| 6 | 3 Sep 2026 | **`docs/FRONTEND_SPEC.md` written.** The frontend contract: five pages mapped to exact endpoints and response shapes (checked against live API calls, not guessed), the shared compliance-panel component, the palette and its status-color reservation, the four-animation motion budget, the live-trace polling contract, formatting rules, and a build-order master prompt. No frontend code written — this is the spec you asked for to build from. |
| 5 | 3 Sep 2026 | **Day 7, non-frontend half complete.** Three endpoints added: `GET /runs/{id}/events`, `GET /invoices/{id}/compliance`, `GET /compliance/window`. Six new tests. Two defects fixed: `_already_worked` had inverted its own meaning after `record_conclusion`/`record_silence` shipped and would have permanently stranded a session-limit-interrupted invoice; the panel called an endpoint as a function and received a `Query` object instead of an `int`. Two `WHAT_BROKE.md` entries, `ARCHITECTURE.md` §05 rewritten. `batch_v2` halted a second time at 75/190, resumed, now past 120/190. 609 tests pass, `ruff` clean. |
| 3 | 2 Sep 2026 | **Day 6 complete; gate met.** `agent/` — `orchestrator`, `tools`, `gate`, `hooks`, `mcp_config`, `adapters/{live_razorpay,simulated}`; `sim/load.py`; 112 tests. `batch_v1` 190/190 unattended, exit 0; `live_v1`/`live_v2` carry real `plink_…` IDs. Four defects found and fixed: `--live` had never run live (`Literal` vs `StrEnum` compared with `is`); the audit trail recorded only actions, never rule-based conclusions; an invoice that concluded in prose and nowhere else (now `approval_granted_not_spent`); every customer with no money recorded as a compliance block. Three `WHAT_BROKE.md` entries and the three-write-path table in `ARCHITECTURE.md` §03. |
| 2 | 31 Aug 2026 | **Day 5 complete; gate met.** `ml/policy.py` + `ml/scorer.py`; `eval/` — `counterfactual.py`, `arms.py`, `bootstrap.py`, `persist.py`, `report.py`, `charts.py`, `__main__.py` — and four `eval_*` tables. Five runs persisted; `EVALUATION.md` §04–§06 generated from Postgres and guarded by `--check`, §07 hand-written and pinned by tests. `docs/assets/four_arms.png` rendered, re-read, four layout defects fixed. Two `WHAT_BROKE.md` entries added (a monkeypatch that did nothing; a zero-width bar that drew its edge). 465 tests pass, `ruff` clean, `--check` green. |
| 1 | 28 Aug 2026 | Realism chart re-read and confirmed fixed (segment labels beside the bar, caption carrying the covariate finding). `ruff` clean, 360 tests pass. Day 4 committed as `5fe711f` and pushed. This report created. |

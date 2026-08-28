# Completion report

> **Live document.** Rewritten after every working prompt, not at the end. If a line
> here disagrees with the repo, the repo is right and this file is stale — say so and
> it gets fixed in the same turn.

**As of:** 28 August 2026 · **Plan day:** 4 of 10 complete · **Head:** `5fe711f`, pushed
to `DisturbedSage5840C/winback` (private) · **Deadline:** 5 September 2026.

**Calendar position: two days ahead.** The plan scheduled Day 4 for 30 August. It
finished on 28 August. That buffer is banked, not spent — the day plan below still
runs in order, it just starts earlier than it had to.

**Nothing is blocked on you right now.** Your first required action is on Day 9. The
full list is in §05, with dates.

---

## 01 — Where the project stands

| | |
|---|---|
| Tests | **360 passing**, 0 skipped, 0 xfail |
| Coverage | **99%** on `compliance/` — the suite a panelist reads first |
| Dataset | **frozen** at fingerprint `c32b2b063cd87707` — 4,000 mandates, 30,210 invoices, 33,866 attempts, 786 censored (2.3%) |
| Realism gate | 19 checks — **13 PASS · 6 ungraded `[REPORT]` · 0 FAIL** |
| Model | **v1 frozen** — XGBoost + sigmoid calibration, chosen out-of-fold, scored once on the held-out cohort |
| Headline honesty number | test ECE **0.034** where the merchant had data, **0.442** where it did not, still correctly ordered there |
| Docs | 2,056 lines across 8 files, all committed |
| Lint | `ruff check .` clean |

The thesis has not moved: **rupees recovered per legal attempt consumed**, reported
beside compliance violations by arm. Day 5 produces the first number for it.

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
| **5** | 31 Aug | Policy layer + four-arm paired harness + bootstrap CIs → `EVALUATION.md` | ⬜ | — |
| **6** | 1 Sep | Agent SDK orchestrator, `can_use_tool` gate, PostToolUse audit, MCP mode switch, both adapters, full batch | ⬜ | — |
| **7** | 2 Sep | FastAPI + Next.js — overview, worklist, drill-down on real data | ⬜ | — |
| **8** | 3 Sep | Compliance panel + evaluation page + four animations; failure drill; rough-cut video — **MVP checkpoint** | ⬜ | — |
| **9** | 4 Sep | Docs, fresh-clone `run_demo.sh`, final video, repo public, tagged release | ⬜ | — |
| **10** | 5 Sep | Buffer, submit early | ⬜ | — |

Plan sections not tied to a single day, and their state:

| Plan § | Subject | State |
|---|---|---|
| §1.1 | No "charge now" API → guarded path, two adapters | Decided in writing, adapter package scaffolded (`agent/adapters/`), implementations land Day 6 |
| §1.2 | sklearn 1.9 `FrozenEstimator` calibration | ✅ Done — that is exactly how `ml/calibrate.py` fits |
| §1.3 | `can_use_tool` as the hard gate, not a hook | Day 6. Guardrail it will call is already written and tested |
| §3.1 | Deterministic counterfactual oracle | ✅ Done — seed excludes `run_id` and `arm`, so arms are genuinely paired |
| §3.2 | Biased legacy policy censors the training data | ✅ Done, and the finding it produced is stronger than the one planned (see §03 below) |
| §3.3 | Four-arm paired evaluation | Design frozen in `EVALUATION.md` §01–§04 **before** results existed; execution is Day 5 |
| §4 | Compliance layer, zero LLM | ✅ Done — six modules + composing guardrail, pure functions |
| §5 | ML pipeline | ✅ Done through calibration; the **decision policy** (argmax expected ₹) is Day 5 |
| §7 | Schema, append-only, PII redaction | ✅ Done — `REVOKE` + triggers, verified against the owner role |
| §8 | Front end | Days 7–8 |
| §11 | 5-minute video | Outline committed (`docs/DEMO_SCRIPT.md`); shot list Day 8, recorded Day 9 |

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

**`docs/WHAT_BROKE.md` is 615 lines written as it happened**, including the two Day-4
generator bugs, the calibrator that scored best and had to lose, a band I invented and
then withdrew, and five chart defects that only failed when the PNG was actually looked
at. Razorpay scores Failure Recovery explicitly; this file is not being written on
Day 9.

---

## 04 — What is left, for me

In order. Each line is the plan's gate, not a vague intention.

**Day 5 — evaluation (next).**
- `ml/policy.py`: enumerate legal `(action × next three valid non-peak slots)` from the
  guardrail, score each, pick argmax expected ₹ net of action cost under the remaining
  attempt budget.
- The rupee-priced confusion matrix — FP = a burned legal attempt + messaging cost,
  FN = invoice × margin.
- `eval/arms.py` + `eval/counterfactual.py`: arms A/B/C/D over the same held-out
  invoices with the same oracle seeds.
- Paired bootstrap CIs over subscriptions.
- `python -m eval.report` writes `EVALUATION.md` §04 and the arm sections **from the
  database** — the numbers must never be hand-typed, and re-running must reproduce them.
- Gate: the ₹/legal-attempt and violations-by-arm table exists and survives being
  argued with.

**Day 6 — the agent.**
- In-process MCP tools via `@tool` + `create_sdk_mcp_server`: `assess_recoverability`,
  `compliance_guardrail`, `simulated_notify`, `execute_recovery`.
- `can_use_tool` money gate as the hard structural block; `PostToolUse` for audit
  append only. Read `claude_agent_sdk.types` directly rather than trusting the docs.
- `agent/mcp_config.py` mode switch: remote HTTP vs local Docker stdio.
- Both adapters implemented. `payment_link_notify` stays **out** of `allowed_tools` —
  it is the one tool that could deliver a message around the consent gate.
- Full batch run unattended; `decisions` and `audit_log` fully populated; the small
  live cohort carries real Razorpay entity IDs.

**Day 7 — API + dashboard skeleton.** FastAPI over the real tables; Next.js overview,
exception worklist, decision drill-down. No mocked data anywhere.

**Day 8 — the parts that win the video.** Compliance guardrail panel, evaluation page,
the four animations (₹ count-up, funnel stagger, live agent trace with the red rule
chip, drill-down drawer). The deliberate failure drill: kill the local MCP mid-batch and
prove it degrades to remote + simulated with a clean audit trail. Rough-cut video as
insurance. **MVP checkpoint — if this day is not green, the stretch lane does not
happen.**

**Day 9 — shipping.** All docs final, fresh-clone test of `scripts/run_demo.sh` in a
clean directory, tagged release, repo flipped public (on your word — see §05).

**Day 10 — buffer.** Submit early in the day, not at the wire.

**Carried, not forgotten:** `docs/ARCHITECTURE.md` gets its final diagram once the agent
loop exists; `docs/DEMO_SCRIPT.md` is an outline until Day 8.

---

## 05 — What is left, for you

Ordered by date. Nothing here is due before 4 September.

| When | What | Why it has to be you |
|---|---|---|
| **Day 9 · 4 Sep** | **Say the word to make the repo public.** `gh` is authenticated as `DisturbedSage5840C`, so the flip itself takes one command — but publishing the repo is irreversible in practice and outward-facing, so it does not happen without you saying so. | Publication decision |
| **Day 9 · 4 Sep** | **Record the 5-minute video.** Script is in `docs/DEMO_SCRIPT.md`; I will have the shot list and the running dashboard ready. `ffmpeg` is not installed — QuickTime screen capture is the path unless you want it installed on Day 8. | Your voice, your screen |
| **Day 10 · 5 Sep** | **Submit the application** at razorpay.com/buildathon, with whatever student-eligibility proof the form asks for. Track 03. Early in the day. | It is your application |

Optional, and none of it blocks the build:

- **Install `ffmpeg`** if you want to edit the video programmatically rather than in
  QuickTime. Decide by Day 8.
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
| Model does not beat retry-everything on raw ₹ | Headline is ₹/legal-attempt; arm B is disqualified on legality, decided before results existed | No |
| Simulator circularity challenged in the panel | Raised first, in the README and on camera, with the observed-vs-censored ECE gap (0.034 vs 0.442) as evidence it was taken seriously | Stronger now than planned — the covariate finding is a better example than the one it replaced |
| Next.js eats Days 7–8 | Overview, worklist, drill-down and the compliance panel are mandatory; the evaluation page may degrade to committed PNGs | No |
| `HookMatcher` shape differs from published docs | Day 6: read the installed types. `can_use_tool` is the load-bearing gate; audit hooks can fall back to wrapping the adapter | No |
| Time overrun | Cut order: stretch lanes → dashboard polish → animations → live cohort. Never compliance tests, audit trail, or `EVALUATION.md` | Two days of buffer banked as of today |

---

## 07 — Update log

Newest first. One entry per working prompt.

| # | Date | What changed |
|---|---|---|
| 1 | 28 Aug 2026 | Realism chart re-read and confirmed fixed (segment labels beside the bar, caption carrying the covariate finding). `ruff` clean, 360 tests pass. Day 4 committed as `5fe711f` and pushed. This report created. |

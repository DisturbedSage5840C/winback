# Winback — Razorpay AI Buildathon 2026, Track 03 (AI Revenue Recovery)

## Context

You have three research documents that converge on one recommendation: enter **Track 03 — AI Revenue Recovery** with a bounded, compliance-gated, audited subscription/UPI-Autopay recovery agent. Applications close **5 September 2026**; today is **26 August 2026**, so this is a ~10-day full-time solo build where *the build is the application*.

I read all three in the order you asked (`compass_artifact` → `rans.md` → `razorpay-buildathon-track3-build-spec.md`) and then verified their load-bearing claims against live sources. The strategy holds. **Three things in the build spec do not, and they change the architecture** — they are resolved in §1 below.

Verified against live sources today:
- Track 03 wording, "The bar", and all five tracks — confirmed verbatim from razorpay.com/buildathon.
- Deadline **5 September 2026**, ₹75,000/month, 6 or 12 months, in-person Bangalore, students only — confirmed.
- NPCI **1 attempt + 3 retries** per mandate, **non-peak only** (peak = 10:00–13:00 and 17:00–21:30 IST), enforced from 1 Aug 2025 under OC-215 — confirmed by two independent sources.
- Razorpay MCP server: ~45 tools, remote at `https://mcp.razorpay.com/mcp` with `Authorization: Basic <base64(key:secret)>`; exactly 4 tools are remote-restricted (`create_refund`, `close_qr_code`, `create_instant_settlement`, `create_registration_link`); local via `docker run --rm -i -e RAZORPAY_KEY_ID -e RAZORPAY_KEY_SECRET razorpay/mcp`, with `TOOLSETS` and `READ_ONLY` env flags — confirmed.

Your decisions: no Razorpay account yet · maximum evaluation rigour · best-of-all front end informed by Razorpay's own motion language · 8+ hrs/day.

---

## 1. Three corrections to the build spec (read these first)

### 1.1 There is no "charge this subscription now" API — this is the biggest one

The build spec §9 says *"you drive retries via the Subscriptions REST API directly."* **That endpoint does not exist.** The Subscriptions API exposes create / fetch / update / cancel / pause / resume / scheduled-changes / invoices — and no charge or retry endpoint. Razorpay's dunning is automatic and internal (T+3), triggerable only from the dashboard's "Charge this now" in test mode.

The real retry primitive is the **Recurring Payments (S2S) API**:

```
POST https://api.razorpay.com/v1/payments/create/recurring
{ "email", "contact", "amount", "currency", "order_id",
  "customer_id", "token", "recurring": true, "description", "notes" }
→ { "razorpay_payment_id": "pay_..." }
```

It needs a `token` from a completed mandate authorization, and **S2S Recurring Payments must be activated on the account by Razorpay support** — which you don't have and probably can't get inside 10 days.

**Resolution — one guarded code path, two execution adapters.** This is the architectural spine of the project, not a workaround:

```
                 ┌─ LiveRazorpayAdapter  → real test-mode API calls, real IDs
Guardrail ──────►│
(single gate)    └─ SimulatedAdapter     → seeded counterfactual oracle
```

Every action goes through the identical decision + guardrail + audit path; only the executor differs, and `audit_log.execution_mode` records which one ran. Do **not** hide this — lead with it. "Measured money recovered" is only *measurable* against a simulator, because test mode has no real money; saying so plainly is the honest-metrics move the rubric rewards.

What the live adapter can genuinely do on a fresh test account (no activation needed) — these produce real `plink_…` / `order_…` / `inv_…` IDs in the audit trail:
- `create_payment_link` / `create_payment_link_upi` with `notify: {sms:false, email:false}` → **a real Razorpay artifact, with the send simulated.** This is the cleanest possible answer to the TRAI/DLT problem: *the link is real, the channel is stubbed, the consent gate is real.*
- `create_order`, `fetch_payment`, `fetch_all_payments`, `fetch_order_payments`, `fetch_tokens` — real reads for the ingest lane.
- `POST /v1/invoices/:id/notify_by/:medium` — exists; keep it wired but disabled behind the consent gate.
- `create_registration_link` (local MCP only) — the correct real action for `BD_hard` mandate failures.

Day 1 includes a 60-minute feasibility spike that probes exactly which of these work, recorded in `docs/LIVE_LANE_FINDINGS.md`. Whatever passes goes into a small live cohort (~10–20 subscriptions); the full 500-subscription batch runs on the simulator.

### 1.2 `CalibratedClassifierCV(cv='prefit')` is removed in scikit-learn 1.9

Build spec §8 specifies `cv='prefit'`. Current signature is `CalibratedClassifierCV(_estimator=None, *, method='sigmoid', cv=None, n_jobs=None, ensemble='auto')`, and prefit calibration is now:

```python
from sklearn.frozen import FrozenEstimator
from sklearn.calibration import CalibratedClassifierCV
cal = CalibratedClassifierCV(FrozenEstimator(fitted_xgb), method="sigmoid")
cal.fit(X_calib, y_calib)      # calibration split only, never the test set
```

`method` now accepts `'sigmoid'`, `'isotonic'`, **and `'temperature'`** (new in 1.8). Fit all three on the calibration split, report ECE/Brier for each, pick the winner on the calibration split, and report the chosen one **once** on the frozen test set. Comparing three calibrators is nearly free and is exactly the rigour that reads as real.

### 1.3 Enforce the money gate with `can_use_tool`, not a `PreToolUse` hook

The build spec puts the hard block in a `PreToolUse` hook. The Python SDK's precisely-documented enforcement point is the permission callback:

```python
async def money_gate(tool_name, input_data, context):
    if tool_name in MONEY_MOVING_TOOLS and not guardrail_approved(input_data):
        return PermissionResultDeny(message="No compliance_guardrail approval on record", interrupt=False)
    return PermissionResultAllow(updated_input=input_data)

options = ClaudeAgentOptions(can_use_tool=money_gate, ...)
```

Use `can_use_tool` as the **hard structural block** and a `PostToolUse` hook (via `HookMatcher`) purely for **audit-log append**. Verify `HookMatcher`'s exact shape against the installed `claude_agent_sdk.types` on Day 6 — the published example is loose. Expose `compliance_guardrail` as an in-process MCP tool via `@tool` + `create_sdk_mcp_server` so the agent must call it and the gate can check that it did.

---

## 2. The thesis (this is what you defend in the panel)

> A naive retry-everything dunning policy recovers money by **breaking NPCI's law**. Winback recovers comparable money while consuming fewer legal attempts and zero violations — and proves it with a paired counterfactual evaluation on a held-out cohort.

The headline metric is therefore **not** raw ₹ recovered. It is **₹ recovered per legal attempt consumed**, with a compliance-violations-by-arm chart alongside it. This defuses the build spec's own biggest risk ("what if the model doesn't beat retry-everything?") by construction: the naive baseline is disqualified on legality, not on lift.

Positioning line for the video: this is Razorpay's own listed example direction **"Mandate retry sequencer"**, built independently on the public MCP server and the Claude Agent SDK.

**Repo / product name:** `winback` (alt: `vasool`). Tagline: *"Recover the money you're legally allowed to."*

---

## 3. Maximum-rigour evaluation design

This is the differentiator you chose, and it is the intellectual core. Three pieces:

### 3.1 The world simulator = a deterministic counterfactual oracle

`sim/world.py` implements a **structural** hazard model — deliberately a different functional form from the XGBoost that will try to learn it:

```
p_success(attempt) = clip(
      base(method, bank)              # UPI Autopay 8–15%, card mandate 2–3% failure priors
    × f_rootcause(TD | BD_transient | BD_hard)
    × g_attempt(attempt_number)       # TD decays slowly; BD_hard ≈ 0
    × h_window(is_non_peak)           # congestion effect — applies to TD only
    × balance_process(customer, date) # salary-cycle: balance replenishes days 1–7
    × k_recency(days_since_last_success),  0, 1)
```

`balance_process` is the key realism move: `insufficient_funds` retries succeed far more just after payday. That is the true mechanism behind India's UPI-Autopay failures (Business Standard: ~20M monthly revocations on low balances), and it gives the model a **genuine timing signal to discover** rather than a coefficient to memorise.

Outcomes are drawn from a seed derived from `hash(subject_id, invoice_id, attempt_number, action, slot)` — so the coin flip for any `(attempt, action)` pair is **fixed regardless of which policy asks for it**. That single property gives you:
- a full counterfactual oracle over actions never taken;
- **paired** policy comparison (same coin flips across arms) → dramatically lower variance than independent replication, and a legitimate paired bootstrap.

### 3.2 A biased legacy policy makes the training data honestly censored

`sim/legacy_policy.py` generates the *historical* dataset the model trains on, as a crude pre-2025 merchant would have done:

> retry at fixed T+1 / T+2 / T+3 at 09:00 IST, **but only if `amount > ₹500` and `method != netbanking`**

Consequences, all of them good:
- Outcomes for low-value and netbanking invoices are **never observed** → real selection bias on a variable correlated with the outcome.
- The model must generalise into a region its training data never saw.
- You can then report something almost nobody at a hackathon reports: **calibration on the observed slice vs. the censored slice, measured against the oracle.** "Our ECE is 0.02 where we have data and 0.07 where we don't" is a stronger credibility signal than any headline AUC.

Also seed some legacy retries **inside peak windows** — those become visible, countable compliance violations in the baseline arm.

### 3.3 Four-arm paired policy evaluation

| Arm | Policy | Purpose |
|---|---|---|
| A | Never retry, always escalate | Over-conservative floor |
| B | Retry everything to the 1+3 cap, any time | Naive baseline (and illegal) |
| C | Legacy policy | What the merchant does today |
| D | **Winback**: model + cost policy + guardrail | The submission |

Held-out cohort split **by `customer_id` AND by time** (train ≈60% earliest, calibrate ≈20%, test ≈20% latest); the test set is frozen before any tuning and scored once. Per arm report: ₹ recovered · attempts consumed · **₹ per legal attempt** · nudges sent · escalations · **compliance violations** · written-off. Paired bootstrap CIs over subscriptions. All of it lands in `docs/EVALUATION.md` as a static committed report, not just live in the demo.

**State the circularity limitation explicitly in the README.** D beats B/C *within this simulator*; the simulator is a model, not the world. Naming your own limitation before a panelist does is worth more than hiding it.

---

## 4. Compliance layer — pure functions, TDD, zero LLM

Reuse the exact structural pattern from `~/Documents/NeuroSynth/src/neurosynth/validation/gates.py` (`GateResult` / `GateDecision.to_dict()` / `ValidationGates.evaluate()`). The compliance guardrail is that same hard-gate/soft-gate machine applied to money instead of models — and `to_dict()` already gives you the audit row.

Write these **test-first** (`superpowers:test-driven-development`) — they are pure, boundary-heavy functions and are the credibility centrepiece:

| Module | Rule | Boundary tests that must prove a *block* |
|---|---|---|
| `compliance/npci_retry_cap.py` | 1 attempt + 3 retries = 4 max per `(subscription_id, invoice_id)` | attempts 1–4 allowed; 5th blocked with `stop_reason="npci_1_plus_3_cap_exhausted"` **even when the model says retry** |
| `compliance/non_peak_window.py` | Peak IST 10:00–13:00 and 17:00–21:30 | 09:59 / 10:00 / 13:00 / 17:00 / 21:29 / 21:30 all resolve correctly; a peak-time proposal is **converted** to `retry_in_window` with the next valid slot, never silently dropped |
| `compliance/afa_threshold.py` | ≤₹15,000 auto; ≤₹1,00,000 for insurance/SIP/credit-card MCCs; above → `escalate_human` | exact-boundary tests at 15000/15001 and 100000/100001 per MCC class |
| `compliance/consent_gate.py` | active consent + not in 90-day DND cooloff; transactional consent window 7 days | withdrawn/DND customers blocked from every nudge |
| `compliance/pre_debit_notice.py` | `notice_sent_at ≤ charge_at − 24h` | blocks a **new** debit; warns (`pre_debit_notice_missing`) but does not block a within-cycle retry |
| `compliance/root_cause.py` | deterministic `(code, source, step, reason) → TD / BD_transient / BD_hard` lookup | every combination in the dataset maps to exactly one class; unknown combos raise, never default |

The guardrail returns only `APPROVE`, `REDIRECT_TO_WINDOW`, `ESCALATE_HUMAN`, or `DENY` — with an `authorizing_rule` string written verbatim into the audit row (`"npci_1_plus_3: attempt 2/4 permitted; window ok (next slot 13:40 IST)"`).

---

## 5. ML pipeline

**Target:** `P(success | attempt, action, slot)` — action-conditioned, so the policy can score candidate actions.
**Model:** XGBoost binary classifier. Root-cause class is an **input feature**, never the model's output (§1 of the spec is right about this — Razorpay hands you `source`/`reason`; using an LLM or a model there is a negative AI-judgment signal).

**Features:** method · amount · MCC · bank · `attempt_number` · root-cause class · hour-of-day + is_non_peak · day-of-week · **day-of-month** (the payday signal) · mandate age · days since last success · bank×method historical failure rate · `paid_count` / `remaining_count` · crosses-₹15k flag.

**Calibration:** `FrozenEstimator` + `CalibratedClassifierCV`, fit on the calibration split; compare `sigmoid` / `isotonic` / `temperature`; report ECE (10-bin) · Brier · reliability diagram · PR-AUC · minority-class precision/recall. Never plain accuracy.

**Decision policy:** for each at-risk invoice, enumerate legal `(action × next-3-valid-non-peak-slots)` candidates from the guardrail, score each, pick argmax of **expected ₹ net of action cost** under the remaining attempt budget. Cost matrix in rupees: FP = one burned legal attempt + messaging cost; FN = invoice × margin. Present the confusion matrix **in rupees**.

**Reuse, don't rewrite:**
- `_compute_ece(y_true, y_proba, n_bins=10)` — `~/Documents/RaceJudge/packages/ml/predictor.py:370`, copy directly.
- `_PlattCalibrator` and the `evaluate()` metric-dict shape — `~/Documents/NeuroSynth/src/neurosynth/models/calibrated_ensemble.py:102` and `:625`.
- Train/save/load/predict class shape — `~/Documents/RaceJudge/packages/ml/predictor.py:144`.

---

## 6. Agent layer — Claude Agent SDK

One orchestrator run per **batch**, iterating at-risk invoices. Claude owns the loop; you own the tools and the gate.

- In-process MCP tools via `@tool` + `create_sdk_mcp_server`: `assess_recoverability` (ML + rule lookup), `compliance_guardrail` (pure deterministic), `simulated_notify`, `execute_recovery` (adapter façade).
- Razorpay MCP mounted alongside, mode-switched between remote (HTTP, Basic auth) and local (Docker stdio) — `agent/mcp_config.py`, never hard-coded.
- `ClaudeAgentOptions`: `allowed_tools`, `permission_mode="default"`, `max_turns=6` per item (hard stop against runaway loops), `can_use_tool=money_gate` (§1.3), `hooks={"PostToolUse": [HookMatcher(...)]}` for audit append, `setting_sources=[]` so the agent doesn't inherit your local Claude Code settings.
- Model: `claude-sonnet-5` for the batch loop (volume); an optional `explainer` subagent on Opus for the plain-English drawer text — narrative only, never a decision.

**Deliberately not an LLM:** the retry cap, the window arithmetic, the ₹ thresholds, the consent check, the TD/BD mapping. Say this out loud in the video — "using an LLM to decide a legal retry cap would be a bug."

---

## 7. Data model (PostgreSQL in Docker)

Take the build spec §6 schema as-is, with these additions:

- `payment_attempts`: `+ is_non_peak BOOLEAN`, `+ observed BOOLEAN` (false = censored by the legacy policy; excluded from training, available to the oracle).
- `decisions`: `+ candidate_set JSONB` (all scored action×slot candidates, not just the winner — this is what makes the drill-down convincing), `+ expected_value_paise`.
- `audit_log`: `+ execution_mode TEXT CHECK (execution_mode IN ('live','simulated'))`, `+ razorpay_entity_id TEXT` (real `plink_…`/`order_…`/`pay_…` when live), `+ arm TEXT` (for the four-arm eval).
- New `eval_runs` / `eval_arm_results` tables so `docs/EVALUATION.md` is generated from the database, not hand-typed.
- **Make append-only real, not aspirational:** `REVOKE UPDATE, DELETE ON audit_log` from the app role + a `BEFORE UPDATE OR DELETE` trigger that raises. A panelist will ask; having the DDL is a 10-minute answer.
- PII redaction (`sha256(customer_id)[:12]`) applied in `observed_data` at write time, not at render time.

Note: `psql` is not installed locally — use `docker exec` or `psycopg` for schema work.

---

## 8. Front end — Razorpay's motion language, not Razorpay's design

I pulled and analysed their live CSS. Findings to build from (**inspiration, never replication**):

| Signal | What Razorpay actually does | What Winback does with it |
|---|---|---|
| Buildathon micro-site type | **Satoshi** (Fontshare, 400/500/700) on near-black `#0e0b08` | Satoshi via Fontshare for display + UI. Fallback stack `Manrope, system-ui` |
| Main site type | Lato | Not used — the buildathon identity is the one judges are living in |
| Palette | ink `#192839` / `#080d29`; brand blue `#305eff`, `#2950da`, `#4d7fff`, `#75a3ff`, `#E9F0FF`; slates `#40566d` `#6c849d` `#cbd5e2` `#f8fafc`; semantic `#48d08c` green, `#F0263C` red | Same family, re-composed: blue = agent action, green = recovered, red = **blocked by guardrail**, amber = escalated, slate = written off |
| Motion stack | **Rive** (interactive vector) + **AOS** (scroll reveal); soft, low-frequency, purposeful | Framer Motion. Scroll-reveal on section entry, no bounce, no parallax |
| Editorial structure | `01 —` numbered sections, labelled blocks ("Why now:", "The bar:"), dense type hierarchy, very low chrome | Mirror the numbered/labelled rhythm in the docs pages and the architecture page |

**Motion budget — four animations, each earning its place:**
1. **₹ recovered count-up** on the overview headline (spring, ~700ms, once per batch completion).
2. **Funnel bars fill left-to-right with stagger** (at-risk → detected → retried → recovered → escalated → written-off), 60ms stagger.
3. **Live agent trace**: during a batch run, decision rows stream in and each one flashes its `authorizing_rule` chip — blue on approve, **red on block**. This is the demo's best 20 seconds; the red flash on the 5th attempt *is* the compliance proof, visually.
4. **Drill-down drawer** slides from the right with the full audit row + the scored candidate set.

Everything else is static. `prefers-reduced-motion` respected throughout.

**Stack:** Next.js (App Router) + Tailwind + Recharts + Framer Motion, reading a FastAPI backend. Pages: Overview · Exception worklist · Decision drill-down · **Compliance guardrail panel** (attempts used vs 1+3, live peak/non-peak countdown, AFA flag, consent/DND) · Calibration & evaluation.

Load the `dataviz` skill before writing the first chart, and `frontend-design` before the first component.

---

## 9. Repository layout

```
winback/
├── README.md                  # problem · architecture · one-command demo · "what broke" log
├── docs/ ARCHITECTURE.md · DATA.md · COMPLIANCE.md · EVALUATION.md
│         LIVE_LANE_FINDINGS.md · DEMO_SCRIPT.md
├── db/schema.sql · db/append_only.sql
├── sim/       world.py (oracle) · legacy_policy.py · generate.py · validate_realism.py
├── compliance/  npci_retry_cap · non_peak_window · afa_threshold · consent_gate
│               pre_debit_notice · root_cause   (+ tests/ alongside)
├── ml/        features.py · train.py · calibrate.py · evaluate.py · policy.py · artifacts/
├── eval/      arms.py · counterfactual.py · report.py
├── agent/     orchestrator.py · tools.py · gate.py · hooks.py · mcp_config.py
│              adapters/{live_razorpay.py, simulated.py}
├── api/       main.py
├── dashboard/ (Next.js)
└── scripts/   run_demo.sh · bootstrap.sh
```

**Environment:** Python 3.14.0 is installed and every dependency has macOS-arm64 cp314 wheels (xgboost 3.4.1 ships universal `py3`). Use a venv with a **pinned** `requirements.txt` — pandas is at 3.0.x, a major version with breaking changes, so pin it and don't drift mid-build. Docker 29.6.1 is available for Postgres and the local MCP server.

---

## 10. Day plan (26 Aug → 5 Sept)

| Day | Deliverable | Gate |
|---|---|---|
| **1** (26–27 Aug) | Repo + venv + pinned deps; Postgres in Docker with schema + append-only DDL; **Razorpay signup, test keys**; remote MCP handshake; **live-lane feasibility spike** → `LIVE_LANE_FINDINGS.md` | You know exactly which live calls work; adapter split is decided in writing |
| **2** (28 Aug) | All six compliance modules **TDD**, plus root-cause lookup | Every rule has a test that proves it *blocks*; coverage on `compliance/` ≥95% |
| **3** (29 Aug) | `sim/world.py` oracle + legacy policy + generator; 500 subscriptions / ~700–900 attempts **frozen**; realism chart | Realised distributions match the cited NPCI/Baymard/industry table; censoring rate reported |
| **4** (30 Aug) | Features + XGBoost + 3-way calibration; **model v1 frozen**; ECE/Brier/PR-AUC/₹-cost-matrix; observed-vs-censored calibration split | Held-out metrics computed **once** and committed |
| **5** (31 Aug) | Policy layer + four-arm paired counterfactual harness + bootstrap CIs → `EVALUATION.md` | ₹/legal-attempt and violations-by-arm table exists and is defensible |
| **6** (1 Sep) | Agent SDK orchestrator: in-process tools, `can_use_tool` gate, PostToolUse audit, MCP mode switch, both adapters; full 500-batch run | Batch completes unattended; `decisions` + `audit_log` fully populated; live cohort shows real Razorpay IDs |
| **7** (2 Sep) | FastAPI + Next.js: overview, worklist, drill-down on real data | No mocks anywhere in the UI |
| **8** (3 Sep) | Compliance panel + evaluation page + the four animations; **deliberate failure drill** (kill local MCP mid-run, prove fallback + graceful audit); **rough-cut video as insurance** | **MVP checkpoint. Not green → no stretch lane, no exceptions** |
| **9** (4 Sep) | All five docs, README + "what broke" log, clean-checkout test of `run_demo.sh`, final 5-min video, public repo, tagged release | One-command demo works on a *fresh clone* |
| **10** (5 Sep) | Buffer + **submit early in the day** | Submitted, not at the wire |

**Stretch (only if Day 8 is green):** checkout-abandonment lane — same agent loop, trigger = `order.created` with no payment in N minutes, action = payment-link nudge. Reuses ~80% of the guardrail/audit infrastructure. Then promise-to-pay tracker. Never cut: compliance tests, the audit trail, `EVALUATION.md`.

**Keep a running `docs/WHAT_BROKE.md` from Day 1.** Razorpay scores *Failure Recovery* explicitly and asks what broke and how you recovered. Writing it retrospectively on Day 9 shows.

---

## 11. Demo video (5:00)

- **0:00–0:30** Quantified problem: "UPI Autopay fails 8–15% of the time versus 2–3% for card mandates. NPCI caps you at one attempt plus three retries, non-peak only. You cannot brute-force this back."
- **0:30–1:00** The closed loop in one sentence; architecture on screen 10 seconds, no more.
- **1:00–3:00** Live batch run: ingest → deterministic TD/BD classification → calibrated probability → **candidate set scored** → guardrail decision → execution (real `plink_…` on the live cohort, oracle on the batch) → funnel updates with measured ₹ recovered.
- **3:00–3:45** The handled failure, on purpose: the 5th attempt blocked by the cap with the red rule chip and the audit row on screen; plus the Day-8 MCP-down fallback. Say plainly that this is what broke and how it was made to fail safely.
- **3:45–4:30** Rigour slide: reliability diagram, ECE, ₹ cost matrix, **four-arm table with ₹/legal-attempt and violations-by-arm**, and the observed-vs-censored calibration gap. Name the simulator limitation yourself.
- **4:30–5:00** Close: Razorpay's own "mandate retry sequencer" direction, built independently on the public MCP server and the Claude Agent SDK in ten days.

Note: `ffmpeg` is not installed — record with QuickTime / macOS screen capture, or install ffmpeg on Day 8 if you want programmatic editing.

---

## 12. Verification

- `pytest compliance/ -v` — every rule blocks at its boundary; this is the suite you show a panelist first.
- `pytest ml/ eval/` — determinism: same seed ⇒ identical oracle outcomes ⇒ identical arm results.
- `python -m eval.report` regenerates `docs/EVALUATION.md` from the database; re-running must reproduce the committed numbers byte-for-byte.
- `scripts/run_demo.sh` **on a fresh clone in a clean directory**: seed → train → run batch → serve → dashboard renders real rows. Run this on Day 9 before recording, not after.
- Live-lane smoke test: one `create_payment_link` with notifications off, one `fetch_payment`, both landing real IDs in `audit_log.razorpay_entity_id`.
- Failure drill: `docker stop` the local MCP mid-batch; the run must degrade to remote + simulated, log `stop_reason`, and finish the batch.

---

## 13. Risks and pre-committed responses

| Risk | Response (decided now, not in the moment) |
|---|---|
| S2S recurring never activates | Expected. Live cohort covers links/orders/reads only; batch runs on the oracle. Already the plan — no scramble. |
| Model doesn't beat retry-everything on raw ₹ | Already handled by the thesis: headline is ₹/legal-attempt + zero violations. B is disqualified on legality. |
| Simulator circularity challenged in the panel | You raise it first, in the README and on camera, with the observed-vs-censored calibration gap as evidence you took it seriously. |
| Next.js eats Day 7–8 | Overview + worklist + drill-down are mandatory; compliance panel is mandatory; the evaluation page may degrade to embedded static PNGs from `ml/artifacts/`. |
| `HookMatcher` shape differs from docs | Day 6, read `claude_agent_sdk.types` directly. `can_use_tool` is the load-bearing gate; audit hooks can fall back to wrapping the adapter. |
| Time overrun | Cut order: stretch lanes → dashboard polish → animations → live cohort. Never compliance tests, audit trail, or `EVALUATION.md`. |

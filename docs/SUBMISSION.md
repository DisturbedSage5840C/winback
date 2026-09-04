# Submission pack

> **Purpose.** Everything the application form needs, written down the day before, so
> submitting is transcription rather than composition. Every number here is sourced to a
> file in this repository; nothing is rounded in your favour and nothing is asserted that
> `python -m eval.report --check` would not reproduce.

**Form:** <https://forms.gle/d9r2gvxp8cmoZhon9> · **Track:** 03 — AI Revenue Recovery ·
**Closes:** 5 September 2026. Submit in the morning. The gate in the plan is *"submitted,
not at the wire"*, and a form that closes at midnight has closed early before.

## The artefacts

| Asked for | Give them |
|---|---|
| A public repo | <https://github.com/DisturbedSage5840C/winback> — public, Apache-2.0, `v1.0.1` tagged |
| A 5-minute pitch video | *(paste the link when it exists — see the check below)* |
| The architecture | [`docs/ARCHITECTURE.md`](ARCHITECTURE.md), linked from README §09. Do not attach a slide; the doc is better and it is versioned |

## One line

> A compliance-gated recovery agent for failed subscription and UPI-Autopay debits: it
> recovers the same money as a retry-everything policy while committing zero NPCI
> violations instead of 66, and proves it with a paired counterfactual evaluation.

## One paragraph

> UPI Autopay mandates fail on 8–15% of debits, and since 1 August 2025 NPCI's OC-215-A
> caps a merchant at one attempt plus three retries per invoice, outside peak hours only.
> So retrying harder is now illegal, and *which* four attempts, and when, is a decision
> worth real money. Winback makes that decision: six pure-function compliance rules
> generate the legal option set, a calibrated XGBoost model ranks it by expected rupees,
> and a Claude Agent SDK loop executes it through a `can_use_tool` gate that physically
> cannot spend money without a guardrail approval on record. Every action lands in an
> append-only audit trail enforced by grants and triggers, not by convention. On a
> held-out cohort of 190 failed invoices, replayed against identical oracle seeds by four
> arms, Winback recovers ₹6,39,626 against the naive baseline's ₹6,39,598 — a tie, and
> reported as one — on thirteen fewer legal attempts and with **0 compliance violations
> against 66**, interval [−96, −42].

## The bar, clause by clause

Track 03's wording is *"Don't just identify the problem. Show measured money recovered
across a batch, with compliant escalation, stopping rules, and an audit trail."* If the
form gives you one box, answer it in this order.

| Their clause | The evidence |
|---|---|
| **Measured money recovered** | Four-arm paired evaluation, 10,000-resample cluster bootstrap over subscriptions, design frozen before results existed — [`EVALUATION.md`](EVALUATION.md) §04–§06, generated from Postgres and guarded by `python -m eval.report --check` |
| **Across a batch** | Two full runs, **190/190 invoices each, unattended, exit 0**. `batch_v2` adds one audit row per conclusion — 190 rows for 190 invoices, no exceptions |
| **Compliant escalation** | AFA thresholds route above-ceiling debits to `escalate_human`; consent and DND gate every nudge; a peak-time proposal is *converted* to the next legal slot, never dropped — [`COMPLIANCE.md`](COMPLIANCE.md) |
| **Stopping rules** | The NPCI 1+3 cap, as a pure function with boundary tests that prove it **blocks** at attempt 5 even when the model says retry. 354 invoices in the live worklist are cap-exhausted; `inv_3890_01` is the one on camera |
| **An audit trail** | `audit_log`, append-only by `REVOKE UPDATE, DELETE` **and** a `BEFORE UPDATE OR DELETE` trigger, with 20 tests proving the immutability. `execution_mode` records live-vs-simulated per row |

## Numbers, if a box asks for them

All from [`EVALUATION.md`](EVALUATION.md) unless noted. Keep the arm results and the
batch results apart — they measure different things, and conflating them is the easiest
way to be caught overstating.

**Four-arm evaluation** (190 held-out invoices, same oracle seeds every arm):

| Arm | Legally recovered | Legal attempts | Violations | ₹ / legal attempt |
|---|---:|---:|---:|---:|
| A — never retry | ₹0 | 0 | 0 | — |
| B — retry everything | ₹6,39,598 | 210 | **66** | ₹3,045.70 |
| C — legacy dunning | ₹53,490 | 63 | **120** | ₹849.05 |
| **D — Winback** | **₹6,39,626** | 197 | **0** | **₹3,246.83** |

Money difference vs B: **+₹28, interval [−₹2,697, ₹2,781]** — spans zero, so it is a tie
and is reported as a tie. Violations difference: **−66, interval [−96, −42]** — excludes
zero. That asymmetry *is* the result.

**Agent batch** (`batch_v2`, the run shown in the video): 190 invoices concluded,
**₹3,57,468 recovered, zero compliance violations**, and **190 audit rows for 190
invoices** — one per conclusion, no exceptions. Outcomes: **86 recovered · 65 deferred ·
22 blocked · 17 failed**. Queried from Postgres on 4 Sep, not copied from an earlier
report.

If a judge counts `decisions` they will find **191 rows for those 190 invoices**, and the
extra one is worth volunteering. `inv_1957_01` was decided at 15:14, then killed by a
session limit *between the guardrail's approval and the tool call* — a decision on record,
no action, no audit row. The resume is keyed on `audit_log` precisely so that case gets
re-worked rather than skipped, and at 18:59 it was: second decision, one presentment, one
audit row, outcome `recovered`. The asymmetry is the design. Skipping it would have left
an approval with nothing behind it; re-working an invoice that *had* concluded would have
presented a mandate twice, and NPCI counts presentments.

**Build:** 612 tests passing · 99% coverage on `compliance/` · dataset frozen at
`c32b2b063cd87707` (4,000 mandates, 30,210 invoices, 33,866 attempts) · test ECE **0.034**
where the merchant had data, **0.442** where it did not, still correctly ranked there ·
43 entries in [`WHAT_BROKE.md`](WHAT_BROKE.md).

## What to say about the limits, if there is room

Say it first rather than waiting to be asked; [`README.md`](../README.md) §08 already
does. The batch runs against a simulator, because Razorpay test mode moves no real money
and "measured recovery" is only measurable against a counterfactual oracle. The live lane
uses real test-mode API calls and records real `plink_…` ids, but it is small and settles
no funds. And the evaluation is circular to a degree — Winback wins inside a world this
repository wrote. The simulator uses a deliberately different functional form from the
model that learns it, and calibration is reported separately on the observed and censored
slices, but no amount of care makes that circularity vanish.

## Before you press submit

- [ ] **Open the video link in a private window.** An unlisted YouTube link is fine; a
      Google Drive link defaulting to "restricted" is the classic way to hand a judge a
      permission wall. Check it logged out, not logged in.
- [ ] **Open the repo URL logged out.** It is public and Apache-2.0 as of 4 Sep — confirm
      it, do not assume it.
- [ ] Check the README renders on GitHub: the result table in §04 and the status table in
      §05 are the two a judge reads first.
- [ ] `v1.0.1` is the release to point at if the form wants a version. `v1.0.0` predates
      the licence and its source archive carries none.
- [ ] Student-eligibility proof — whatever the form asks for. Have it as a file before you
      open the form, not during.
- [ ] Submit. Then stop.

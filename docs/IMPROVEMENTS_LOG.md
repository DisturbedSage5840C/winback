# Improvements log

A running log of additive, backend/model/agent changes made after the frozen model and
the cited numbers were committed — kept separate from `docs/WHAT_BROKE.md`, which is an
incident log (believed → actually true → cost → changed) and not the right shape for
"here is a new endpoint." Each entry names what changed and what was run to verify it
didn't touch anything on the do-not-touch list.

Format: what changed → what was verified.

---

## 2026-09-04 · `GET /model/importances`

**Changed.** A new read-only endpoint in `api/main.py` that reads
`ml/artifacts/metrics_v1.json["importances"]` — the 27 gain-based feature importances
`ml/train.py` already computed at training time and nothing downstream had ever served —
sorts by gain share descending, and returns the top N (`?top=`, default 12, max 27).

**Verified.** Added `test_model_importances_are_read_from_the_training_artifact_sorted_by_gain`
and `test_model_importances_caps_at_the_frozen_feature_count` to `api/tests/test_main.py`,
asserting the response is sorted descending, the shares sum to ≤ 1.0, and `top` is bounded
by `Query(..., le=27)`. Full suite green (612 → 617 passing), `ruff check .` clean. Does not
read `ml/artifacts/model_v1.json` or `calibrator_v1.joblib`, and computes nothing — it sorts
and slices a dict that was already on disk.

## 2026-09-04 · `Cache-Control` on the endpoints that describe frozen data

**Changed.** `/evaluation` and `/model/importances` always set
`Cache-Control: public, max-age=300` — both describe files (`eval_runs`/`eval_arm_results`,
written only by `python -m eval.report`; `metrics_v1.json`, written only by `python -m ml`)
that never change as a side effect of a request. `/runs/{run_id}/overview` sets the same
header only when `run_id` is *not* the run behind the newest row in `audit_log` — the
orchestrator runs one batch at a time, so exactly one run_id can still be receiving writes
at any moment, and every other run_id is settled for good.

**Verified.** Added `test_frozen_artifact_responses_are_cacheable` and
`test_a_settled_runs_overview_is_cacheable_the_latest_one_is_not` to
`api/tests/test_main.py`. The latter skips if the database holds fewer than two runs,
which is a legitimate state on a fresh clone rather than a failure. Full suite green,
`ruff check .` clean. No response body changed — headers only.

## 2026-09-04 · Segmentation query params on `GET /worklist`

**Changed.** `/worklist` (the live, at-risk queue) now accepts optional `?bank=`,
`?method=`, and `?root_cause=` filters against columns `exception_worklist` already
computes (`bank`, `method`, `latest_root_cause`) — no new aggregation, and every value is
bound as a query parameter rather than interpolated into the SQL text, so an unrecognised
value returns zero rows rather than an error. `/runs/{run_id}/worklist` was checked first
and already ships its own `?outcome=` filter; it was left alone.

**Verified.** Added `test_the_live_queue_can_be_segmented_by_bank_method_or_root_cause` to
`api/tests/test_main.py`, covering a real filter value, a garbage value (zero rows, not a
500), and that a filtered total is never larger than the unfiltered one. Full suite green,
`ruff check .` clean.

## 2026-09-04 · Frozen-model regression fixture

**Changed.** `ml/tests/test_frozen_model_unchanged.py`, backed by
`ml/tests/testdata/frozen_model_golden.json` — eight feature rows generated once from a
seeded RNG (independent of `sim.generate`, on purpose: this test's job is to catch a
dependency bump or a pickling difference under the artifacts, not to re-derive the
training cohort), with their expected probabilities committed alongside them. Every run
re-scores the same eight rows through `ml.scorer.load_scorer` and asserts the probabilities
match to `1e-12`. This is a supplement to `ml/tests/test_scorer.py`'s existing
dataset-cohort regression test, not a replacement for it — smaller, faster, and with one
fewer moving part between "did the artifacts change" and the answer.

**Verified.** The new test passes against the current `model_v1.json` +
`calibrator_v1.joblib`. Full suite green, `ruff check .` clean. Touches no file under
`ml/artifacts/`.

## 2026-09-04 · `agent/explain.py` + `GET /invoices/{invoice_id}/explain`

**Changed.** Wired the previously-unused `WINBACK_EXPLAINER_MODEL` setting
(`core.config.Settings.explainer_model` was read from `.env` but nothing downstream ever
consumed it). `agent/explain.py` reads an already-written `decisions` row (joined to its
`audit_log` outcome, if one exists) through `core.db.read_connection` — the same
`winback_reader` role `api/main.py` uses — and asks the explainer model for one
plain-English paragraph. It is mounted with **zero tools**: `mcp_servers={}`,
`allowed_tools=[]`. It cannot call `compliance_guardrail` or `execute_recovery` because
neither is reachable from the process, not because a policy omits them. `GET
/invoices/{invoice_id}/explain` calls it lazily — on the first request for a given
decision, cached in-process by `decision_id` afterwards (decisions are append-only and
never mutated, so the cache can never go stale) — never pre-computed for every invoice in
a batch.

**Verified.** `agent/tests/test_explain.py` asserts `explainer_options(...)` carries no
MCP servers and no allowed tools, that a decision which was never written raises
`DecisionNotFound` before any network call is made (no API key or network needed for that
path), and that `_decision_record` reads through the reader role. `api/tests/test_main.py`
adds `test_an_invoice_with_no_decision_has_no_explanation`, confirming the 404 path never
reaches the model either. No test in either file prompts a real model — consistent with
`agent/tests/conftest.py`'s existing rule that nothing in this package should depend on
what an LLM says. Full suite green, `ruff check .` clean. Does not touch `agent/gate.py`,
`ALLOWED_TOOLS`/`PREAPPROVED_TOOLS`/`GATED_TOOLS`, or `agent/orchestrator.py`'s control
flow.

"""What the frozen dataset promises the rest of the project.

Every number in ``docs/EVALUATION.md`` is computed on these rows, so the dataset is
evidence in the same way the legacy policy is. Four families of claim:

1. **It is frozen.** Regenerating reproduces it byte-for-byte, and the fingerprint is
   pinned here so a stray edit to a world constant fails a test instead of quietly
   changing every headline figure.
2. **The censoring is honest.** Counterfactual rows exist for the oracle and are
   marked ``observed = FALSE``, and they never move an invoice's status. A shadow
   retry that "would have" captured must not turn an unpaid invoice into a recovered
   one — that would hand the model a label no merchant could ever have seen.
3. **Postgres will accept it.** Every CHECK, FK and UNIQUE constraint in
   ``db/01_schema.sql`` is asserted here, in memory, so a bad row fails in a
   millisecond rather than 4,000 inserts into a transaction that then rolls back.
4. **The population is usable.** Both cohorts and both consent states the demo needs
   actually exist, in the quantities the downstream work assumes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from itertools import pairwise

import pytest

from compliance.non_peak_window import IST, is_non_peak
from compliance.root_cause import RootCause, classify
from sim import generate
from sim.generate import AS_OF, Dataset
from sim.legacy_policy import MAX_ATTEMPTS, censoring_reason
from sim.world import Mandate

#: The dataset the whole project runs on. Built once — it is pure, so sharing it
#: across the module costs nothing and every test sees the same rows the model will.
FROZEN_FINGERPRINT = "c32b2b063cd87707"


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return generate.build_dataset()


@pytest.fixture(scope="module")
def by_invoice(dataset: Dataset) -> dict[str, list[generate.AttemptRow]]:
    grouped: dict[str, list[generate.AttemptRow]] = {}
    for attempt in dataset.attempts:
        grouped.setdefault(attempt.invoice_id, []).append(attempt)
    return grouped


def _consecutive_observed(
    attempts: list[generate.AttemptRow],
) -> list[tuple[generate.AttemptRow, generate.AttemptRow]]:
    """Adjacent pairs of attempts that really happened, in the order they happened."""
    observed = sorted((a for a in attempts if a.observed), key=lambda a: a.attempt_number)
    return list(pairwise(observed))


# ------------------------------------------------------------------ it is frozen


def test_the_dataset_is_reproducible(dataset: Dataset) -> None:
    assert generate.build_dataset().fingerprint() == dataset.fingerprint()


def test_the_fingerprint_is_the_one_the_documents_describe(dataset: Dataset) -> None:
    """Pinned deliberately.

    ``docs/DATA.md`` and every figure in the evaluation are statements about *this*
    dataset. Changing a world constant is allowed; changing one silently is not, so
    this test is the thing that forces the realism gate to be re-run and the docs to
    be updated in the same commit.
    """
    assert dataset.fingerprint() == FROZEN_FINGERPRINT


def test_each_entity_is_seeded_on_itself_not_on_call_order(dataset: Dataset) -> None:
    """A smaller run must be a strict prefix of the frozen one.

    ``_rng`` keys on what it is generating rather than on a shared stream, which is
    what lets a test build 40 subscriptions and reason about the same customers the
    4,000-subscription dataset holds. If this ever fails, every fast test in this file
    is quietly testing a different world than the one that ships.
    """
    small = generate.build_dataset(40)
    assert small.customers == dataset.customers[:40]
    assert small.invoices == dataset.invoices[: len(small.invoices)]

    # Cohort is the one field that legitimately depends on the whole population:
    # the split is by time across everyone who exists, so the earliest 40 mandates
    # are all "train" on their own and spread across three cohorts in the full run.
    without_cohort = [replace(row, cohort="") for row in small.subscriptions]
    assert without_cohort == [
        replace(row, cohort="") for row in dataset.subscriptions[:40]
    ]


# ------------------------------------------------------- the censoring is honest


def test_a_censored_retry_never_recovers_an_invoice(
    dataset: Dataset, by_invoice: dict[str, list[generate.AttemptRow]]
) -> None:
    """The load-bearing invariant of the whole censoring design.

    The oracle knows what a censored retry would have done. History does not — the
    legacy policy never made it. So a counterfactual capture must leave the invoice
    exactly as unpaid as it really was.
    """
    for invoice in dataset.invoices:
        shadow = [a for a in by_invoice.get(invoice.invoice_id, []) if not a.observed]
        if not shadow:
            continue
        assert invoice.status != "recovered", (
            f"{invoice.invoice_id} is censored yet marked recovered; a retry that "
            "never happened has been credited with the money"
        )


def test_censored_invoices_really_do_contain_captures(dataset: Dataset) -> None:
    """Otherwise the invariant above is vacuous.

    The counterfactual rows are only interesting because some of them succeeded —
    that gap between what was observed and what was recoverable is the thing the
    model has to generalise into.
    """
    shadow_captures = sum(
        1 for a in dataset.attempts if not a.observed and a.outcome == "captured"
    )
    assert shadow_captures > 0


def test_only_retries_are_ever_censored(dataset: Dataset) -> None:
    """The legacy policy filtered *retries*. The first charge always went out — it is
    the mandate presenting itself, not a dunning decision — so a censored first
    attempt would mean the bias is in the wrong place entirely."""
    assert all(a.observed for a in dataset.attempts if a.attempt_number == 1)


def test_the_censoring_reason_is_the_one_the_legacy_policy_would_give(
    dataset: Dataset,
) -> None:
    """Recorded reasons have to be reproducible from the policy, or ``docs/DATA.md``'s
    breakdown is a claim about nothing."""
    subscriptions = {s.subscription_id: s for s in dataset.subscriptions}

    for attempt in dataset.attempts:
        if attempt.observed:
            assert attempt.censoring_reason is None
            continue
        subscription = subscriptions[attempt.subscription_id]
        mandate = Mandate(
            subscription_id=subscription.subscription_id,
            method=subscription.method,
            bank=subscription.bank,
            amount_paise=subscription.amount_paise,
            paid_count=subscription.paid_count,
        )
        # Non-None matters on its own: db/01_schema.sql refuses an unobserved row
        # with no reason, so a hole here becomes a load failure rather than a
        # silently unexplained gap in the training data.
        assert attempt.censoring_reason is not None
        assert attempt.censoring_reason == censoring_reason(mandate)


def test_the_censored_slice_is_large_enough_to_measure(dataset: Dataset) -> None:
    """Day 4 reports calibration separately on the observed and censored slices. A
    handful of rows would make that comparison noise rather than evidence."""
    censored = [a for a in dataset.attempts if not a.observed]
    assert len(censored) >= 100


# --------------------------------------------------- Postgres will accept it


def test_no_invoice_exceeds_the_npci_attempt_budget(
    by_invoice: dict[str, list[generate.AttemptRow]],
) -> None:
    """``payment_attempts.attempt_number CHECK (BETWEEN 1 AND 4)``."""
    for attempts in by_invoice.values():
        assert all(1 <= a.attempt_number <= MAX_ATTEMPTS for a in attempts)


def test_attempt_numbers_are_unique_within_an_invoice(
    by_invoice: dict[str, list[generate.AttemptRow]],
) -> None:
    """``UNIQUE (invoice_id, attempt_number, run_id)``, with ``run_id`` null for all
    of history. Two rows numbered 2 would be rejected at insert time."""
    for invoice_id, attempts in by_invoice.items():
        numbers = [a.attempt_number for a in attempts]
        assert len(numbers) == len(set(numbers)), invoice_id


def test_attempts_are_contiguous_from_one(
    by_invoice: dict[str, list[generate.AttemptRow]],
) -> None:
    """A gap would mean an attempt was consumed without a row, and the worklist's
    ``attempts_remaining`` would over-count the budget the agent has left."""
    for invoice_id, attempts in by_invoice.items():
        numbers = sorted(a.attempt_number for a in attempts)
        assert numbers == list(range(1, len(numbers) + 1)), invoice_id


def test_every_row_points_at_a_row_that_exists(dataset: Dataset) -> None:
    """The four foreign keys, checked before the database has to."""
    customers = {c.customer_id for c in dataset.customers}
    subscriptions = {s.subscription_id: s for s in dataset.subscriptions}
    invoices = {i.invoice_id: i for i in dataset.invoices}

    assert all(s.customer_id in customers for s in dataset.subscriptions)
    assert all(i.subscription_id in subscriptions for i in dataset.invoices)
    for attempt in dataset.attempts:
        assert attempt.invoice_id in invoices
        assert attempt.subscription_id in subscriptions


def test_identifiers_are_unique(dataset: Dataset) -> None:
    """Four primary keys."""
    for ids in (
        [c.customer_id for c in dataset.customers],
        [s.subscription_id for s in dataset.subscriptions],
        [i.invoice_id for i in dataset.invoices],
        [a.attempt_id for a in dataset.attempts],
    ):
        assert len(ids) == len(set(ids))


def test_salary_days_stay_inside_the_schema_check(dataset: Dataset) -> None:
    """``customers.salary_day CHECK (BETWEEN 1 AND 28)`` — the cap exists so that
    ``_add_months`` never has to clamp a 31st into a short month."""
    assert all(1 <= c.salary_day <= 28 for c in dataset.customers)


def test_the_non_peak_flag_agrees_with_the_compliance_module(dataset: Dataset) -> None:
    """The column is denormalised for the dashboard's benefit. It must be derived
    from the same function the guardrail uses, not computed a second way."""
    assert all(a.is_non_peak == is_non_peak(a.attempted_at) for a in dataset.attempts)


def test_nothing_is_dated_after_the_dataset_was_frozen(dataset: Dataset) -> None:
    """``AS_OF`` is what makes the dataset reproducible tomorrow. An attempt in the
    future would mean something read the wall clock."""
    assert all(a.attempted_at <= AS_OF for a in dataset.attempts)
    assert all(i.charge_at <= AS_OF for i in dataset.invoices)


# ------------------------------------------------- outcomes are internally coherent


def test_a_captured_attempt_carries_no_error(dataset: Dataset) -> None:
    for attempt in dataset.attempts:
        if attempt.outcome != "captured":
            continue
        assert attempt.error_code is None
        assert attempt.root_cause_class is None


def test_every_failure_is_classified_by_the_production_function(dataset: Dataset) -> None:
    """The training label and the live label come out of the same lookup. A parallel
    labelling route in the generator is exactly the drift the design forbids."""
    for attempt in dataset.attempts:
        if attempt.outcome != "failed":
            continue
        assert attempt.error_code is not None
        assert attempt.root_cause_class == classify(
            attempt.error_code,
            attempt.error_source,  # type: ignore[arg-type]
            attempt.error_step,  # type: ignore[arg-type]
            attempt.error_reason,  # type: ignore[arg-type]
        )


def test_a_recovered_invoice_was_recovered_by_an_attempt_that_happened(
    dataset: Dataset, by_invoice: dict[str, list[generate.AttemptRow]]
) -> None:
    for invoice in dataset.invoices:
        if invoice.status != "recovered":
            continue
        observed = [a for a in by_invoice[invoice.invoice_id] if a.observed]
        assert any(a.outcome == "captured" for a in observed)


def test_dunning_stops_at_the_first_capture(
    by_invoice: dict[str, list[generate.AttemptRow]],
) -> None:
    """No policy retries an invoice it has already collected. A row after a capture
    would be a burned legal attempt that never existed."""
    for invoice_id, attempts in by_invoice.items():
        observed = sorted(
            (a for a in attempts if a.observed), key=lambda a: a.attempt_number
        )
        outcomes = [a.outcome for a in observed]
        if "captured" in outcomes:
            assert outcomes.index("captured") == len(outcomes) - 1, invoice_id


def test_the_shadow_schedule_stops_at_the_first_capture_too(
    by_invoice: dict[str, list[generate.AttemptRow]],
) -> None:
    """The counterfactual branch has to obey the same control flow as the real one.

    It did not, once. The censored branch materialised all three retries
    unconditionally while the observed branch returned on the first capture, so a
    censored invoice could carry a shadow attempt #4 dated after a shadow attempt #2
    that had already collected the money. Those rows describe a world that does not
    exist, and they were being fed to the observed-versus-censored calibration report
    as if they were evidence about one that does.
    """
    for invoice_id, attempts in by_invoice.items():
        shadow = sorted(
            (a for a in attempts if not a.observed), key=lambda a: a.attempt_number
        )
        outcomes = [a.outcome for a in shadow]
        if "captured" in outcomes:
            assert outcomes.index("captured") == len(outcomes) - 1, invoice_id


def test_a_shadow_retry_is_numbered_as_if_it_had_been_presented(
    by_invoice: dict[str, list[generate.AttemptRow]],
) -> None:
    """Censored retries run 2, 3, 4 without gaps, continuing the real first charge.

    The numbering is what makes a censored row coherent enough to score: NPCI counts
    presented debits, so a shadow attempt #4 is a claim that #2 and #3 were also
    presented, and both must be present for the row's prior-failure count to mean
    anything. A gap here would mean ``ml/dataset.py`` was building feature rows whose
    ``attempt_number`` and ``prior_failures_this_invoice`` disagree.
    """
    for invoice_id, attempts in by_invoice.items():
        shadow = sorted(a.attempt_number for a in attempts if not a.observed)
        if shadow:
            assert shadow == list(range(2, 2 + len(shadow))), invoice_id


def test_every_attempt_in_the_dataset_happens_in_order(
    dataset: Dataset, by_invoice: dict[str, list[generate.AttemptRow]]
) -> None:
    """Attempt *n+1* is later than attempt *n*, and all of them are later than the
    charge.

    122 attempts failed this before the urgent branch learned that a cron cannot run
    in the past. The damage is not cosmetic: ``PriorState.before`` reads every attempt
    dated earlier than the candidate, so a retry that precedes its own first charge
    hands the model a first charge that already has a failure behind it — a feature
    row describing a sequence no merchant can ever present.
    """
    charge_at = {invoice.invoice_id: invoice.charge_at for invoice in dataset.invoices}
    for invoice_id, attempts in by_invoice.items():
        for observed in (True, False):
            slice_ = sorted(
                (a for a in attempts if a.observed is observed),
                key=lambda a: a.attempt_number,
            )
            moments = [a.attempted_at for a in slice_]
            assert moments == sorted(moments), invoice_id
            assert len(set(moments)) == len(moments), invoice_id
            assert all(m >= charge_at[invoice_id] for m in moments), invoice_id


def test_a_written_off_invoice_collected_nothing(
    dataset: Dataset, by_invoice: dict[str, list[generate.AttemptRow]]
) -> None:
    for invoice in dataset.invoices:
        if invoice.status != "written_off":
            continue
        observed = [a for a in by_invoice[invoice.invoice_id] if a.observed]
        assert all(a.outcome == "failed" for a in observed)


def test_the_legacy_cron_keeps_retrying_a_dead_mandate(
    by_invoice: dict[str, list[generate.AttemptRow]],
) -> None:
    """Not a defect — the finding.

    The legacy policy is a cron that fires on T+1/T+2/T+3 without reading the error
    code, so it spends legal attempts on mandates that were revoked days earlier.
    That waste is precisely what ₹-per-legal-attempt is meant to expose, and if the
    generator quietly skipped those retries arm C would look smarter than the policy
    it is supposed to represent.
    """
    wasted = [
        later
        for attempts in by_invoice.values()
        for earlier, later in _consecutive_observed(attempts)
        if earlier.root_cause_class == RootCause.BD_HARD
    ]
    assert wasted, "arm C's central inefficiency is absent from the data"


def test_no_retry_after_a_dead_mandate_ever_collects(
    by_invoice: dict[str, list[generate.AttemptRow]],
) -> None:
    """The other half: the attempts are burned, and none of them work.

    ``sim/world.py`` guarantees this per attempt. Asserting it again over the
    assembled dataset is what rules out the generator resurrecting a mandate by
    reusing a stale context between cycles.
    """
    for attempts in by_invoice.values():
        for earlier, later in _consecutive_observed(attempts):
            if earlier.root_cause_class == RootCause.BD_HARD:
                assert later.outcome == "failed"


# ------------------------------------------------------- the population is usable


def test_cohorts_are_disjoint_and_split_by_customer(dataset: Dataset) -> None:
    """Split by customer AND by time. Leakage here would inflate every held-out
    metric on Day 4, and nothing downstream would notice."""
    seen: dict[str, str] = {}
    for subscription in dataset.subscriptions:
        customer = subscription.customer_id
        assert seen.setdefault(customer, subscription.cohort) == subscription.cohort


def test_cohorts_are_ordered_in_time(dataset: Dataset) -> None:
    """The test cohort holds the newest mandates: a model is always asked to score
    subscriptions younger than the ones it learned from."""
    starts = {
        cohort: [s.mandate_start for s in dataset.subscriptions if s.cohort == cohort]
        for cohort in ("train", "calibrate", "test")
    }
    assert max(starts["train"]) <= min(starts["calibrate"])
    assert max(starts["calibrate"]) <= min(starts["test"])


def test_every_cohort_is_big_enough_to_train_and_score_on(dataset: Dataset) -> None:
    counts = Counter(s.cohort for s in dataset.subscriptions)
    assert counts["train"] >= 250
    assert counts["calibrate"] >= 80
    assert counts["test"] >= 80
    assert sum(counts.values()) == len(dataset.subscriptions)


def test_both_blocked_consent_states_are_represented(dataset: Dataset) -> None:
    """``compliance/consent_gate.py`` blocks withdrawn and DND customers. The demo
    can only show that happening if the population contains some."""
    states = Counter(c.consent_status for c in dataset.customers)
    assert states["withdrawn"] >= 10
    assert states["dnd"] >= 10


def test_some_invoices_are_still_open_for_the_agent_to_work(dataset: Dataset) -> None:
    """The at-risk set is the live worklist. An empty one means the Day 6 batch run
    has nothing to decide about."""
    assert sum(i.status == "at_risk" for i in dataset.invoices) >= 20


def test_the_worklist_is_mostly_actionable_and_partly_exhausted(
    dataset: Dataset, by_invoice: dict[str, list[generate.AttemptRow]]
) -> None:
    """Both halves matter, for opposite reasons.

    Most at-risk invoices must still have NPCI budget, or the Day 6 batch is a parade
    of denials with nothing recovered. But at least one must have burned all four,
    because that is the invoice where the agent proposes a fifth attempt and
    ``compliance/npci_retry_cap.py`` refuses it — the demo's single best moment, and
    it has to come out of real history rather than a staged row.
    """
    used = {
        invoice.invoice_id: sum(1 for a in by_invoice[invoice.invoice_id] if a.observed)
        for invoice in dataset.invoices
        if invoice.status == "at_risk"
    }
    exhausted = [n for n in used.values() if n >= MAX_ATTEMPTS]

    assert len(exhausted) >= 1, "no invoice can demonstrate the retry cap blocking"
    assert len(exhausted) < len(used) // 2, "most of the worklist must be workable"


def test_the_population_straddles_every_threshold_the_rules_turn_on(
    dataset: Dataset,
) -> None:
    """₹500 legacy floor, ₹2,000 urgent branch, ₹15,000 AFA ceiling. A rule nobody
    crosses is a rule nobody can demonstrate."""
    amounts = [s.amount_paise for s in dataset.subscriptions]
    for threshold in (500_00, 2_000_00, 15_000_00):
        assert sum(a <= threshold for a in amounts) >= 20
        assert sum(a > threshold for a in amounts) >= 20


def test_charges_are_presented_outside_the_peak_windows(dataset: Dataset) -> None:
    """Mandate presentment is a batch job that runs overnight, so the *first* charge
    is never the source of a peak-window violation. Every violation in arm C comes
    from the legacy retry cron, which is where the finding belongs."""
    assert all(is_non_peak(i.charge_at) for i in dataset.invoices)


def test_the_pre_debit_notice_is_usually_but_not_always_there(dataset: Dataset) -> None:
    """RBI requires 24h notice. Real merchants miss some, and
    ``compliance/pre_debit_notice.py`` has a branch for it that would otherwise be
    dead code in the demo."""
    missing = [i for i in dataset.invoices if i.notice_sent_at is None]
    assert 0 < len(missing) < len(dataset.invoices) // 2
    for invoice in dataset.invoices:
        if invoice.notice_sent_at is not None:
            assert invoice.notice_sent_at < invoice.charge_at


def test_the_summary_reports_what_the_documents_quote(dataset: Dataset) -> None:
    """``docs/DATA.md`` quotes these keys directly, so a rename has to break here."""
    summary = dataset.summary()
    assert summary["fingerprint"] == FROZEN_FINGERPRINT
    assert summary["dataset_version"] == generate.DATASET_VERSION
    assert summary["attempts_observed"] + summary["attempts_censored"] == len(
        dataset.attempts
    )
    assert 0.0 < summary["censoring_rate"] < 0.5  # type: ignore[operator]


def test_the_timestamps_are_ist(dataset: Dataset) -> None:
    """Every rule in ``compliance/`` is expressed in IST wall clock. A row carrying
    UTC would land in the wrong peak window and nothing would complain."""
    assert all(a.attempted_at.tzinfo is IST for a in dataset.attempts)
    assert all(i.charge_at.tzinfo is IST for i in dataset.invoices)

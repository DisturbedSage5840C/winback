"""The leakage barrier, asserted rather than asserted-to.

Every claim in ``docs/EVALUATION.md`` assumes the model never saw the answer. That
assumption is worth exactly as much as the tests here, so these are written to fail
loudly if a future edit widens what a feature row can reach.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from ml.dataset import build_matrix, build_splits
from ml.features import (
    FEATURE_NAMES,
    NEVER_SUCCEEDED_DAYS,
    BankMethodRates,
    Candidate,
    PriorState,
    features_for,
)
from sim import generate
from sim.generate import Dataset


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return generate.build_dataset()


@pytest.fixture(scope="module")
def rates(dataset: Dataset) -> BankMethodRates:
    return BankMethodRates.fit(dataset, cohort="train")


def _row(dataset: Dataset, rates: BankMethodRates, attempt: generate.AttemptRow) -> dict:
    subs = {s.subscription_id: s for s in dataset.subscriptions}
    customers = {c.customer_id: c for c in dataset.customers}
    invoices = {i.invoice_id: i for i in dataset.invoices}
    subscription = subs[attempt.subscription_id]
    history = tuple(
        sorted(
            (a for a in dataset.attempts if a.subscription_id == attempt.subscription_id
             and a.observed),
            key=lambda a: (a.attempted_at, a.attempt_number),
        )
    )
    candidate = Candidate.from_attempt(attempt)
    return features_for(
        subscription=subscription,
        customer=customers[subscription.customer_id],
        invoice=invoices[attempt.invoice_id],
        candidate=candidate,
        prior=PriorState.before(
            candidate, invoice_id=attempt.invoice_id, history=history
        ),
        rates=rates,
    )


def test_the_candidate_carries_no_outcome() -> None:
    """The barrier is structural: there is nothing on a Candidate to leak.

    This is the test that makes every other leakage argument in the project cheap. If
    ``Candidate`` ever grows an outcome field, a hundred careful call sites stop being
    the thing protecting the model and this fails instead.
    """
    forbidden = {"outcome", "error_code", "error_reason", "root_cause_class",
                 "p_success", "observed", "oracle_seed", "censoring_reason"}
    assert forbidden.isdisjoint(Candidate.__slots__)


def test_the_payday_is_not_readable_from_the_features(
    dataset: Dataset, rates: BankMethodRates
) -> None:
    """Shuffling every customer's salary day must change nothing.

    ``salary_day`` is the mechanism behind the balance hazard and the simulator knows
    it; a merchant does not. The model is supposed to *discover* the payday signal from
    ``day_of_month``, so if reassigning paydays moved a single feature value, it would
    mean the answer was reachable from the input.
    """
    attempt = next(a for a in dataset.attempts if a.observed and a.attempt_number > 1)
    before = _row(dataset, rates, attempt)

    shifted = replace(
        dataset,
        customers=tuple(
            replace(c, salary_day=(c.salary_day % 28) + 1) for c in dataset.customers
        ),
    )
    after = _row(shifted, rates, attempt)
    assert before == after


def test_prior_state_never_reads_the_attempt_it_is_describing(
    dataset: Dataset,
) -> None:
    """An attempt at the same instant is not prior to itself.

    Ties are the interesting case: a ``<=`` here instead of ``<`` would let every row
    fold in its own outcome, which would look like a spectacular model rather than a
    bug.
    """
    attempt = next(a for a in dataset.attempts if a.observed and a.attempt_number > 1)
    history = tuple(
        sorted(
            (a for a in dataset.attempts if a.subscription_id == attempt.subscription_id
             and a.observed),
            key=lambda a: (a.attempted_at, a.attempt_number),
        )
    )
    candidate = Candidate.from_attempt(attempt)
    prior = PriorState.before(
        candidate, invoice_id=attempt.invoice_id, history=history
    )
    assert prior.lifetime_attempts == sum(
        1 for a in history if a.attempted_at < attempt.attempted_at
    )
    assert prior.last_attempt_at is None or prior.last_attempt_at < attempt.attempted_at
    assert prior.last_success_at is None or prior.last_success_at < attempt.attempted_at


def test_a_later_attempt_cannot_inform_an_earlier_one(dataset: Dataset) -> None:
    """Appending a future attempt to the history leaves the earlier row untouched."""
    attempt = next(a for a in dataset.attempts if a.observed and a.attempt_number == 1)
    history = (attempt,)
    candidate = Candidate.from_attempt(attempt)
    baseline = PriorState.before(candidate, invoice_id=attempt.invoice_id, history=history)

    future = replace(
        attempt,
        attempt_id="att_from_the_future",
        attempted_at=attempt.attempted_at + timedelta(days=1),
        outcome="captured",
    )
    assert (
        PriorState.before(
            candidate, invoice_id=attempt.invoice_id, history=(*history, future)
        )
        == baseline
    )


def test_the_column_order_is_fixed(dataset: Dataset, rates: BankMethodRates) -> None:
    """A silent reordering between training and serving is how a boosted tree starts
    scoring noise while every metric still looks healthy."""
    attempt = next(a for a in dataset.attempts if a.observed)
    assert tuple(_row(dataset, rates, attempt)) == FEATURE_NAMES
    assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)


def test_a_mandate_that_never_paid_gets_the_sentinel(
    dataset: Dataset, rates: BankMethodRates
) -> None:
    """``days_since_last_success`` has one meaning, not NaN and one meaning."""
    attempt = next(
        a
        for a in dataset.attempts
        if a.observed
        and a.attempt_number == 1
        and not any(
            o.outcome == "captured" and o.attempted_at < a.attempted_at
            for o in dataset.attempts
            if o.subscription_id == a.subscription_id
        )
    )
    assert _row(dataset, rates, attempt)["days_since_last_success"] == NEVER_SUCCEEDED_DAYS


def test_bank_rates_are_fitted_on_the_training_cohort_only(dataset: Dataset) -> None:
    """A rate computed over the whole dataset carries test outcomes into a training
    feature — mild leakage, and invisible once the numbers are in a table."""
    train_only = BankMethodRates.fit(dataset, cohort="train")
    everything = BankMethodRates.fit(dataset, cohort="test")
    assert train_only.rates != everything.rates


def test_no_feature_is_nan_or_infinite(dataset: Dataset) -> None:
    """Trees tolerate NaN by routing it to a default branch, which turns a bug into a
    slightly worse model rather than an error."""
    import numpy as np

    splits = build_splits(dataset)
    for name in ("train", "calibrate", "test", "censored_calibrate", "censored_test"):
        matrix = getattr(splits, name)
        assert np.isfinite(matrix.X).all(), name


def test_censored_rows_never_reach_the_training_matrices(dataset: Dataset) -> None:
    """A censored attempt carries an oracle outcome no merchant ever saw."""
    splits = build_splits(dataset)
    censored_ids = {a.attempt_id for a in dataset.attempts if not a.observed}
    for name in ("train", "calibrate", "test"):
        matrix = getattr(splits, name)
        assert censored_ids.isdisjoint(matrix.attempt_ids), name
    assert set(splits.censored_test.attempt_ids) <= censored_ids


def test_a_censored_row_sees_its_own_counterfactual_history(dataset: Dataset) -> None:
    """Attempt number and prior-failure count must agree, or the row is impossible.

    Built from observed history alone, a shadow attempt #4 reported one prior failure —
    the real first charge — because the shadow attempts #2 and #3 that make it a #4 were
    filtered out. The model has never seen that combination and never could, so scoring
    it and calling the result selection bias would have been measuring the harness.
    """
    splits = build_splits(dataset)
    number = FEATURE_NAMES.index("attempt_number")
    priors = FEATURE_NAMES.index("prior_failures_this_invoice")
    rows = splits.censored_test.X
    assert len(rows)
    assert (rows[:, number] == rows[:, priors] + 1).all()


def test_the_matrices_are_deterministic(dataset: Dataset) -> None:
    """Same seed, same rows, same order — twice."""
    import numpy as np

    rates = BankMethodRates.fit(dataset, cohort="train")
    first = build_matrix(dataset, cohort="test", rates=rates)
    second = build_matrix(dataset, cohort="test", rates=rates)
    assert np.array_equal(first.X, second.X)
    assert first.attempt_ids == second.attempt_ids

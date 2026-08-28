"""Turn the frozen simulator rows into design matrices, one per cohort.

Separated from ``ml/features.py`` so that the *what* of a feature and the *which rows*
of a training set are edited independently. This module owns three decisions:

**Only observed attempts train.** Censored rows carry an oracle outcome the merchant
never saw. They are assembled here too — the observed-vs-censored calibration report
needs them — but into their own matrix, never mixed into training.

**Labels are successes, not failures.** ``y = 1`` means the attempt captured. The
positive class is the money, which keeps the ₹ cost matrix and the PR curve pointed at
the same thing.

**History is per subscription and ordered.** A mandate's attempts are gathered once,
sorted ascending, and sliced by the candidate slot inside
:meth:`~ml.features.PriorState.before`. Sorting once here rather than per row is the
difference between seconds and minutes at 34,764 attempts.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ml.features import (
    FEATURE_NAMES,
    BankMethodRates,
    Candidate,
    PriorState,
    features_for,
)
from sim.generate import AttemptRow, Dataset

#: Cohorts in the order the pipeline consumes them.
COHORTS: tuple[str, ...] = ("train", "calibrate", "test")


@dataclass(frozen=True, slots=True)
class Matrix:
    """A design matrix plus the identifiers needed to trace any row back to its source.

    ``attempt_ids`` and ``oracle_p`` are carried alongside rather than inside ``X``: the
    first so a prediction can be joined back to an audit row, the second so the
    evaluation can compare calibrated output against the oracle's own probability
    without that probability ever being available to the model.
    """

    X: np.ndarray
    y: np.ndarray
    attempt_ids: tuple[str, ...]
    oracle_p: np.ndarray
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def __len__(self) -> int:
        return int(self.X.shape[0])

    @property
    def positive_rate(self) -> float:
        return float(self.y.mean()) if len(self) else 0.0


def _history_by_subscription(
    dataset: Dataset, *, observed_only: bool
) -> dict[str, tuple[AttemptRow, ...]]:
    """Every attempt on a mandate, ascending by time.

    ``observed_only`` picks which world the timeline belongs to, and the two matrices
    need different ones.

    For the observed matrix it is true, and it has to be: a censored retry never
    happened, so it cannot inform a decision the merchant actually made.

    For the censored matrix it is false, and it has to be that too. A censored row is
    the counterfactual *fourth* attempt on an invoice whose second and third were also
    counterfactual. Folding in only the observed history produces a row that says
    "attempt 4, one prior failure" — a combination that cannot occur in any world, and
    one the model has therefore never seen. Scoring the model on impossible rows and
    reporting the result as evidence of selection bias would be measuring the harness.
    The counterfactual timeline is the coherent one: it is the history a merchant
    without the legacy filters would have had, and the oracle's seed contains no arm,
    so those shadow outcomes are the same outcomes the observed world would have
    produced had it reached them.
    """
    grouped: dict[str, list[AttemptRow]] = {}
    for attempt in dataset.attempts:
        if observed_only and not attempt.observed:
            continue
        grouped.setdefault(attempt.subscription_id, []).append(attempt)
    return {
        sub_id: tuple(sorted(rows, key=lambda a: (a.attempted_at, a.attempt_number)))
        for sub_id, rows in grouped.items()
    }


def build_matrix(
    dataset: Dataset,
    *,
    cohort: str,
    rates: BankMethodRates,
    observed: bool = True,
) -> Matrix:
    """Assemble one cohort's rows.

    ``observed=False`` selects the censored slice — the retries the legacy policy's
    filters suppressed. Those rows have an oracle outcome but were never seen by any
    merchant, which is exactly what makes them the interesting half of the calibration
    report in ``docs/EVALUATION.md``.
    """
    if cohort not in COHORTS:
        raise ValueError(f"unknown cohort {cohort!r}; expected one of {COHORTS}")

    subs = {s.subscription_id: s for s in dataset.subscriptions if s.cohort == cohort}
    customers = {c.customer_id: c for c in dataset.customers}
    invoices = {i.invoice_id: i for i in dataset.invoices}
    history = _history_by_subscription(dataset, observed_only=observed)

    rows: list[list[float]] = []
    labels: list[int] = []
    ids: list[str] = []
    oracle: list[float] = []

    for attempt in dataset.attempts:
        if attempt.observed != observed:
            continue
        subscription = subs.get(attempt.subscription_id)
        if subscription is None:
            continue

        candidate = Candidate.from_attempt(attempt)
        prior = PriorState.before(
            candidate,
            invoice_id=attempt.invoice_id,
            history=history.get(attempt.subscription_id, ()),
        )
        feature_row = features_for(
            subscription=subscription,
            customer=customers[subscription.customer_id],
            invoice=invoices[attempt.invoice_id],
            candidate=candidate,
            prior=prior,
            rates=rates,
        )
        rows.append([feature_row[name] for name in FEATURE_NAMES])
        labels.append(int(attempt.outcome == "captured"))
        ids.append(attempt.attempt_id)
        oracle.append(attempt.p_success)

    return Matrix(
        X=np.asarray(rows, dtype=np.float64).reshape(-1, len(FEATURE_NAMES)),
        y=np.asarray(labels, dtype=np.int8),
        attempt_ids=tuple(ids),
        oracle_p=np.asarray(oracle, dtype=np.float64),
    )


@dataclass(frozen=True, slots=True)
class Splits:
    """The three cohorts plus a censored slice for each of the two that get scored.

    ``censored_calibrate`` exists so that a calibrator's off-distribution behaviour can
    be *checked before it is chosen*. Without it the only way to discover that a
    calibrator collapses outside the region it was fitted on is to run it on
    ``censored_test`` — which would make the test set part of the selection procedure
    and quietly void the number it is being kept for.
    """

    train: Matrix
    calibrate: Matrix
    test: Matrix
    censored_calibrate: Matrix
    censored_test: Matrix
    rates: BankMethodRates

    def summary(self) -> str:
        lines = [
            f"{'cohort':20}{'rows':>8}{'captured':>10}{'rate':>8}",
        ]
        for name in ("train", "calibrate", "test", "censored_calibrate", "censored_test"):
            m: Matrix = getattr(self, name)
            lines.append(
                f"{name:20}{len(m):8,d}{int(m.y.sum()):10,d}{m.positive_rate:8.1%}"
            )
        return "\n".join(lines)


def build_splits(dataset: Dataset) -> Splits:
    """Build every matrix the Day-4 pipeline needs, with rates fitted on train only."""
    rates = BankMethodRates.fit(dataset, cohort="train")
    return Splits(
        train=build_matrix(dataset, cohort="train", rates=rates),
        calibrate=build_matrix(dataset, cohort="calibrate", rates=rates),
        test=build_matrix(dataset, cohort="test", rates=rates),
        censored_calibrate=build_matrix(
            dataset, cohort="calibrate", rates=rates, observed=False
        ),
        censored_test=build_matrix(dataset, cohort="test", rates=rates, observed=False),
        rates=rates,
    )

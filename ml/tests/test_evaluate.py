"""The metrics, checked against cases whose answer is known by hand.

Every number in ``docs/EVALUATION.md`` comes out of this module, so a quiet arithmetic
error here would not surface as a crash — it would surface as a slightly better result.
The inputs below are small enough that the expected value can be computed on paper.
"""

from __future__ import annotations

import numpy as np
import pytest

from ml.evaluate import (
    DEFAULT_COSTS,
    CostMatrix,
    break_even_threshold,
    evaluate,
    expected_calibration_error,
    maximum_calibration_error,
    reliability,
    rupee_confusion,
)


def test_a_perfectly_calibrated_input_scores_zero() -> None:
    """Half the rows at 0.5, half of those captured: the bin's gap is exactly zero."""
    y_true = np.array([1, 0, 1, 0])
    y_prob = np.array([0.5, 0.5, 0.5, 0.5])

    assert expected_calibration_error(y_true, y_prob) == pytest.approx(0.0)
    assert maximum_calibration_error(y_true, y_prob) == pytest.approx(0.0)


def test_confident_and_wrong_scores_one() -> None:
    """The worst attainable ECE, as a fixed point for the scale."""
    assert expected_calibration_error(
        np.zeros(10, dtype=int), np.ones(10)
    ) == pytest.approx(1.0)


def test_ece_is_weighted_by_how_many_rows_are_in_the_bin() -> None:
    """Nine rows right and one row badly wrong is not a coin flip's worth of error.

    MCE reports that same bin at full strength, which is why both are printed: a model
    can post a good ECE while being useless in exactly the sparse region a policy
    operates in.
    """
    y_true = np.array([1] * 9 + [0])
    y_prob = np.array([0.95] * 9 + [0.95])

    assert expected_calibration_error(y_true, y_prob) == pytest.approx(0.05, abs=1e-9)
    assert maximum_calibration_error(y_true, y_prob) == pytest.approx(0.05, abs=1e-9)


def test_a_prediction_of_exactly_one_lands_in_the_last_bin() -> None:
    """``np.digitize`` would otherwise drop it out of the histogram entirely, and a
    calibrator that emits 1.0 is precisely the case this project disqualifies — it must
    be counted, not lost."""
    bins = reliability(np.array([1, 0]), np.array([1.0, 0.0]))

    assert sum(b.count for b in bins) == 2
    assert bins[-1].count == 1
    assert bins[0].count == 1


def test_empty_bins_do_not_contribute_error() -> None:
    bins = reliability(np.array([1, 1]), np.array([0.05, 0.05]))
    assert [b.count for b in bins] == [2] + [0] * 9
    assert expected_calibration_error(np.array([1, 1]), np.array([0.05, 0.05])) == (
        pytest.approx(0.95)
    )


def test_precision_and_recall_are_computed_on_failure_not_capture() -> None:
    """88% of attempts capture, so the majority-class figures are noise.

    The question a policy asks is "of the attempts I called doomed, how many were" —
    which is precision on ``1 - y``. Getting this inverted would report a model that
    looks excellent and is answering a question nobody asked.
    """
    y_true = np.array([1, 1, 1, 0])
    y_prob = np.array([0.9, 0.9, 0.2, 0.2])  # two rows predicted to fail, one really did

    metrics = evaluate(y_true, y_prob, slice_name="t", threshold=0.5)

    assert metrics.failure_precision == pytest.approx(0.5)
    assert metrics.failure_recall == pytest.approx(1.0)
    assert metrics.positive_rate == pytest.approx(0.75)


def test_a_single_class_slice_reports_zero_rather_than_a_perfect_score() -> None:
    """ROC-AUC is undefined on one class, and 1.0 would read as a triumph."""
    metrics = evaluate(np.ones(5, dtype=int), np.full(5, 0.9), slice_name="t")

    assert metrics.roc_auc == 0.0
    assert metrics.pr_auc_failure == 0.0


# --------------------------------------------------------------- the cost matrix


def test_the_break_even_threshold_solves_the_equation_in_its_docstring() -> None:
    """``p > c / (c + amount · margin)`` — computed independently here."""
    costs = CostMatrix()
    amounts = np.array([100_00.0, 500_00.0])
    c = costs.attempt_cost_paise + costs.burned_attempt_paise

    expected = np.array([c / (c + a * costs.margin) for a in amounts])
    assert break_even_threshold(amounts) == pytest.approx(expected)


def test_a_bigger_invoice_is_worth_a_longer_shot() -> None:
    """Monotone in amount, and never outside (0, 1)."""
    thresholds = break_even_threshold(np.array([149_00.0, 1_000_00.0, 18_392_00.0]))

    assert (np.diff(thresholds) < 0).all()
    assert ((thresholds > 0) & (thresholds < 1)).all()


def test_the_cost_matrix_almost_never_says_do_not_attempt() -> None:
    """The finding, asserted rather than asserted-to.

    On the invoice sizes this dataset actually contains, break-even lands in the low
    single-digit percents — so a policy that only thresholds on probability would
    attempt nearly everything. That is why Winback maximises expected rupees *subject to
    the NPCI budget* instead: money is not the scarce resource here, the four legal
    attempts are. If this ever fails, that argument needs rewriting, not the test.
    """
    thresholds = break_even_threshold(np.array([149_00.0, 706_00.0, 5_000_00.0]))

    assert thresholds.max() < 0.30
    assert np.median(thresholds) < 0.10


def test_the_default_cost_matrix_is_one_object_everywhere() -> None:
    """Two copies of the constants could drift; a rupee figure in the evaluation report
    and a rupee figure in the policy have to come from the same one."""
    assert break_even_threshold.__defaults__ is None  # keyword-only, so check __kwdefaults__
    assert break_even_threshold.__kwdefaults__["costs"] is DEFAULT_COSTS
    assert rupee_confusion.__kwdefaults__["costs"] is DEFAULT_COSTS


def test_rupees_not_counts_is_the_whole_point_of_the_confusion_matrix() -> None:
    """One ₹18,392 miss and one ₹149 miss are the same count and very different money."""
    y_true = np.array([1, 1, 0])
    y_prob = np.array([0.9, 0.01, 0.9])
    amounts = np.array([18_392_00.0, 149_00.0, 1_000_00.0])

    result = rupee_confusion(y_true, y_prob, amounts, threshold=0.5)
    costs = CostMatrix()

    assert result["margin_recovered_paise"] == pytest.approx(18_392_00.0 * costs.margin)
    assert result["margin_forgone_paise"] == pytest.approx(149_00.0 * costs.margin)
    assert result["wasted_attempt_cost_paise"] == pytest.approx(
        costs.attempt_cost_paise + costs.burned_attempt_paise
    )
    assert result["net_paise"] == pytest.approx(
        result["margin_recovered_paise"] - result["wasted_attempt_cost_paise"]
    )


def test_the_threshold_may_be_one_value_per_row() -> None:
    """The per-invoice break-even is a vector, and passing it must not broadcast wrong.

    Same predictions, same outcomes, two different thresholds: the row that clears its
    own threshold is attempted and the row that does not is skipped, which a scalar
    threshold cannot express.
    """
    y_true = np.array([1, 1])
    y_prob = np.array([0.30, 0.30])
    amounts = np.array([1_000_00.0, 1_000_00.0])

    result = rupee_confusion(
        y_true, y_prob, amounts, threshold=np.array([0.20, 0.40])
    )

    assert result["margin_recovered_paise"] == pytest.approx(1_000_00.0 * 0.25)
    assert result["margin_forgone_paise"] == pytest.approx(1_000_00.0 * 0.25)

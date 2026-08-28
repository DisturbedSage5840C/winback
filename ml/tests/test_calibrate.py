"""The model section's two load-bearing claims, graded.

The first is the one ``sim/validate_realism.py`` explicitly defers to this file: the
censored region — the invoices the legacy policy refused to retry — is a region the model
gets *wrong*, and it is wrong there in a specific and reportable way. The realism gate
declines to grade that because whether a model is wrong is a question about a model, not
about a dataset. It is graded here.

The second is that the winning calibrator was chosen honestly: out-of-fold, on the
calibration split, with a certainty-asserting calibrator disqualified before its ECE was
so much as compared.

The fixtures are module-scoped because the pipeline they build is the expensive part of
this repository's test suite; every assertion below reads the same trained model.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from ml import calibrate
from ml.__main__ import METRICS_PATH
from ml.dataset import Splits, build_splits
from ml.train import train
from sim.generate import Dataset, build_dataset


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return build_dataset()


@pytest.fixture(scope="module")
def splits(dataset: Dataset) -> Splits:
    return build_splits(dataset)


@pytest.fixture(scope="module")
def trained(splits: Splits):
    return train(splits)


@pytest.fixture(scope="module")
def report(trained, splits: Splits) -> calibrate.CalibrationReport:
    return calibrate.choose(trained, splits.calibrate, splits.censored_calibrate)


@pytest.fixture(scope="module")
def committed() -> dict:
    """The numbers ``docs/EVALUATION.md`` quotes, as written by ``python -m ml``."""
    return json.loads(METRICS_PATH.read_text())


# ------------------------------------------------- the observed/censored gap


def test_the_model_is_calibrated_where_the_merchant_had_data(
    report: calibrate.CalibrationReport, splits: Splits
) -> None:
    observed, _ = calibrate.observed_versus_censored(report.winner, splits)

    assert observed.n == len(splits.test)
    assert observed.ece < 0.05


def test_the_model_is_badly_miscalibrated_where_it_did_not(
    report: calibrate.CalibrationReport, splits: Splits
) -> None:
    """The promise ``sim/validate_realism.check_censoring`` makes and does not keep.

    The band is directional, not tuned: the censored slice has to be *materially* worse,
    where materially means a different order of magnitude rather than a percentage point
    that could move with a seed. If this ever comes down to parity, the honest reading is
    not that the model improved — it is that the legacy policy stopped censoring anything
    worth censoring, and the selection-bias argument in the README has lost its subject.
    """
    observed, censored = calibrate.observed_versus_censored(report.winner, splits)

    assert censored.n > 0
    assert censored.ece > 5 * observed.ece


def test_it_is_wrong_in_one_direction_pessimism(
    report: calibrate.CalibrationReport, splits: Splits
) -> None:
    """Every censored row is a debit the legacy policy would not present, so the model's
    only evidence about that region is the region next to it. It reads across as
    hopelessness — predicting failure on invoices the oracle says would have paid.

    The direction is the finding. A model that were merely *noisy* off-distribution would
    be a weaker claim; a model that is systematically pessimistic there is an argument
    that the legacy policy's blind spot is exactly where the recoverable money is.
    """
    probabilities = report.winner.predict_proba(splits.censored_test.X)
    signed_gap = float(np.mean(probabilities - splits.censored_test.oracle_p))

    assert signed_gap < -0.20
    assert splits.censored_test.y.mean() > probabilities.mean()


def test_but_it_still_ranks_correctly_off_distribution(
    report: calibrate.CalibrationReport, splits: Splits
) -> None:
    """The sharpest result in the model section, and the reason the policy layer works.

    Miscalibrated-but-ordered means the *absolute* probability is untrustworthy in the
    censored region while the *relative* one is not — so a policy that picks the best of
    several candidate actions is far less damaged there than one that thresholds on a
    number. Winback's policy is the first kind, and this is why.
    """
    observed, censored = calibrate.observed_versus_censored(report.winner, splits)

    assert censored.roc_auc > 0.75
    assert censored.roc_auc > 0.9 * observed.roc_auc


def test_the_censored_slice_is_never_used_to_fit_anything(splits: Splits) -> None:
    """It is evidence about the model, and evidence the model has seen is not evidence."""
    fitted_on = set(splits.train.attempt_ids) | set(splits.calibrate.attempt_ids)
    censored = set(splits.censored_calibrate.attempt_ids) | set(
        splits.censored_test.attempt_ids
    )

    assert fitted_on.isdisjoint(censored)
    assert fitted_on.isdisjoint(splits.test.attempt_ids)


# ------------------------------------------------------- how the winner was chosen


def test_isotonic_is_disqualified_for_asserting_certainty(
    report: calibrate.CalibrationReport,
) -> None:
    """It posts the lowest ECE on the panel and still loses.

    A probability of exactly zero is not a low probability — it is a claim no evidence
    can revise, and the Day-5 policy ranks by expected rupees, where an expected value of
    exactly zero can never be the argmax. A calibrator that zeroes the censored region
    would make Winback decline to retry precisely the invoices the legacy policy declined
    to retry, reimplementing the selection bias this project exists to remove.
    """
    isotonic = next(c for c in report.candidates if c.method == "isotonic")

    assert not isotonic.admissible
    assert isotonic.degenerate_censored_rows > 0
    assert isotonic.out_of_fold.ece < report.winner.out_of_fold.ece
    assert report.winner.method != "isotonic"


def test_the_winner_asserts_certainty_about_nothing(
    report: calibrate.CalibrationReport, splits: Splits
) -> None:
    for matrix in (splits.calibrate, splits.censored_calibrate, splits.test):
        probabilities = report.winner.predict_proba(matrix.X)
        assert ((probabilities > 0.0) & (probabilities < 1.0)).all()


def test_the_choice_would_have_been_different_in_sample(
    report: calibrate.CalibrationReport,
) -> None:
    """The bug this file's docstring was written about, kept alive as a test.

    Isotonic reproduces the rows it was fitted on almost exactly, so an in-sample
    comparison between three methods of wildly different capacity ranks them by how much
    they can memorise. Out-of-fold, it has no such advantage.
    """
    isotonic = next(c for c in report.candidates if c.method == "isotonic")

    assert isotonic.in_sample.ece < 1e-6
    assert isotonic.out_of_fold.ece > isotonic.in_sample.ece
    assert min(report.candidates, key=lambda c: c.in_sample.ece).method == "isotonic"


def test_ranking_goes_through_the_out_of_fold_metrics(
    report: calibrate.CalibrationReport,
) -> None:
    """``candidate.metrics`` is what anything ranking candidates is supposed to read."""
    for candidate in report.candidates:
        assert candidate.metrics is candidate.out_of_fold


def test_no_admissible_calibrator_is_fatal_rather_than_silent(
    trained, splits: Splits, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback a reader expects — ship the uncalibrated booster — is a decision
    about what the system predicts, and a person should make it after reading the
    message, not an ``except`` branch."""
    monkeypatch.setattr(calibrate, "_degenerate", lambda _: 1)

    with pytest.raises(RuntimeError, match=r"exact 0\.0 or 1\.0"):
        calibrate.choose(trained, splits.calibrate, splits.censored_calibrate)


def test_the_folds_cover_the_split_exactly_once() -> None:
    """Contiguous and non-overlapping: a row scored twice would be weighted twice, and a
    row scored never would leave the choice resting on less evidence than it reports."""
    for n in (4_791, 100, 7, 5, 3):
        bounds = calibrate._fold_bounds(n, calibrate.SELECTION_FOLDS)
        covered = [i for lo, hi in bounds for i in range(lo, hi)]
        assert covered == list(range(n))


def test_a_fold_is_scored_by_a_calibrator_that_never_saw_it(
    trained, splits: Splits
) -> None:
    """If ``_out_of_fold`` were quietly returning in-sample predictions, every claim on
    this page would still pass and mean nothing."""
    out_of_fold = calibrate._out_of_fold(trained, splits.calibrate, "isotonic")
    in_sample = next(
        c for c in calibrate.fit_calibrators(
            trained, splits.calibrate, splits.censored_calibrate
        ) if c.method == "isotonic"
    ).calibrator.predict_proba(splits.calibrate.X)[:, 1]

    assert not np.isnan(out_of_fold).any()
    assert not np.allclose(out_of_fold, in_sample)


# ------------------------------------------------------------------ frozen numbers


def test_the_pipeline_reproduces_the_committed_metrics(
    report: calibrate.CalibrationReport, splits: Splits, committed: dict
) -> None:
    """``ml/artifacts/metrics_v1.json`` is quoted in the docs and the dashboard.

    Recomputing it from the seeded dataset has to land on the same digits, or "the test
    set was scored once" is a claim about a file rather than about the pipeline that
    wrote it. Regenerate with ``python -m ml`` when a change is intended.
    """
    observed, censored = calibrate.observed_versus_censored(report.winner, splits)

    assert committed["calibration"]["chosen"] == report.winner.method
    assert committed["test"]["observed"]["ece"] == pytest.approx(observed.ece, rel=1e-9)
    assert committed["test"]["censored"]["ece"] == pytest.approx(censored.ece, rel=1e-9)
    assert committed["test"]["observed"]["n"] == observed.n
    assert committed["test"]["censored"]["n"] == censored.n


def test_training_twice_gives_the_same_model(splits: Splits, trained) -> None:
    """Same seed, same rows, same booster — otherwise every frozen number above is a
    coincidence that happened to survive to the commit."""
    again = train(splits)

    assert again.best_iteration == trained.best_iteration
    assert np.array_equal(
        again.predict_proba(splits.test.X), trained.predict_proba(splits.test.X)
    )

"""Train, calibrate, evaluate, freeze. One command, one artifact set, once.

    python -m ml            # full pipeline, writes ml/artifacts/ and the chart
    python -m ml --no-save  # same numbers, writes nothing

Running this a second time must reproduce the committed numbers exactly: the dataset
is seeded, the split is by time, ``PARAMS`` carries a fixed ``random_state``, and
nothing here samples. If a rerun moves a digit, something is non-deterministic and that
is a bug worth chasing, not a rounding difference to shrug at.

**The test split is scored exactly once, at the end of this file.** Everything above it
— early stopping, calibrator choice — is decided on train and calibrate. That ordering
is the only reason the final numbers mean anything, so it is enforced by the shape of
the module rather than left to whoever edits it next.
"""

from __future__ import annotations

import argparse
import json

import numpy as np

from ml import calibrate as calibration
from ml import charts
from ml import train as training
from ml.dataset import Splits, build_splits
from ml.evaluate import (
    CostMatrix,
    Metrics,
    break_even_threshold,
    evaluate,
    rupee_confusion,
)
from sim.generate import Dataset, build_dataset

ARTIFACTS = training.ARTIFACTS
METRICS_PATH = ARTIFACTS / "metrics_v1.json"


def _amounts_paise(splits: Splits, matrix_name: str) -> np.ndarray:
    """Recover each row's amount from the design matrix.

    ``amount_rupees`` is a feature, so the matrix already carries it and there is no
    need to re-join against the dataset — which would risk pairing a prediction with
    the wrong invoice, the sort of bug that produces a plausible ₹ figure and no error.
    """
    matrix = getattr(splits, matrix_name)
    column = matrix.feature_names.index("amount_rupees")
    return matrix.X[:, column] * 100.0


def _oracle_gap(y_prob: np.ndarray, oracle_p: np.ndarray) -> dict[str, float]:
    """How far the model's probability sits from the one that generated the outcome.

    Only the simulator makes this measurable, and it is a stronger statement than any
    calibration curve: a curve says the model is right *on average within a bin*, this
    says how close it is row by row to the truth it was never shown.
    """
    gap = y_prob - oracle_p
    # A calibrator that clamps every row to the same value has no variance, and the
    # correlation with it is undefined rather than zero. Reporting nan is right —
    # substituting 0.0 would read as "uncorrelated" when the truth is "constant", and
    # those are very different failures.
    degenerate = y_prob.std() == 0.0 or oracle_p.std() == 0.0
    return {
        "mean_signed_gap": float(gap.mean()),
        "mean_absolute_gap": float(np.abs(gap).mean()),
        "p90_absolute_gap": float(np.quantile(np.abs(gap), 0.90)),
        "correlation": float("nan") if degenerate else float(np.corrcoef(y_prob, oracle_p)[0, 1]),
        "distinct_predictions": int(np.unique(y_prob).size),
    }


def _metrics_dict(metrics: Metrics) -> dict[str, object]:
    return {
        "slice": metrics.slice_name,
        "n": metrics.n,
        "positive_rate": metrics.positive_rate,
        "ece": metrics.ece,
        "mce": metrics.mce,
        "brier": metrics.brier,
        "pr_auc_failure": metrics.pr_auc_failure,
        "roc_auc": metrics.roc_auc,
        "failure_precision": metrics.failure_precision,
        "failure_recall": metrics.failure_recall,
        "threshold": metrics.threshold,
        "bins": [
            {
                "lower": b.lower,
                "upper": b.upper,
                "count": b.count,
                "mean_predicted": b.mean_predicted,
                "observed_rate": b.observed_rate,
            }
            for b in metrics.bins
        ],
    }


def run(*, dataset: Dataset | None = None, save: bool = True) -> dict[str, object]:
    """The whole Day-4 pipeline, in the order the splits allow."""
    dataset = dataset or build_dataset()
    splits = build_splits(dataset)
    print(splits.summary(), end="\n\n")

    trained = training.train(splits)
    print(
        f"booster: stopped at iteration {trained.best_iteration} of "
        f"{training.PARAMS['n_estimators']} "
        f"({trained.train_rows:,d} fit rows, {trained.inner_validation_rows:,d} inner val)\n"
    )
    print("gain-ranked features")
    for name, share in training.importances(trained):
        print(f"  {name:32}{share:7.1%}")
    print()

    report = calibration.choose(trained, splits.calibrate, splits.censored_calibrate)
    print(report.table(), end="\n\n")

    # --- everything below this line touches the test split, and only once. ---------
    winner = report.winner
    test_prob = winner.predict_proba(splits.test.X)
    observed, censored = calibration.observed_versus_censored(winner, splits)
    uncalibrated_test = evaluate(
        splits.test.y,
        trained.predict_proba(splits.test.X),
        slice_name="test · uncalibrated",
    )

    print(f"{'slice':22}{'n':>8}{'ECE':>9}{'MCE':>9}{'Brier':>9}{'PR-AUC':>10}{'ROC-AUC':>9}")
    print("-" * 76)
    for m in (uncalibrated_test, observed, censored):
        print(m.as_row())
    print()

    test_amounts = _amounts_paise(splits, "test")
    thresholds = break_even_threshold(test_amounts)
    rupees = rupee_confusion(
        splits.test.y, test_prob, test_amounts, threshold=thresholds
    )
    declined = int((test_prob < thresholds).sum())
    print(
        f"₹ confusion at the per-invoice break-even threshold "
        f"({thresholds.min():.4f} to {thresholds.max():.4f}, median {np.median(thresholds):.4f}) "
        f"on the test cohort"
    )
    for key, value in rupees.items():
        unit = "₹" if key.endswith("_paise") else ""
        shown = f"{unit}{value / 100:,.0f}" if key.endswith("_paise") else f"{value:,.0f}"
        print(f"  {key:32}{shown:>14}")
    print(
        f"  {'attempts declined on cost alone':32}"
        f"{declined:>14,d}  of {len(test_prob):,d} — "
        "the money almost never says no, the attempt budget does"
    )
    print()

    gaps = {
        "observed": _oracle_gap(test_prob, splits.test.oracle_p),
        "censored": _oracle_gap(
            winner.predict_proba(splits.censored_test.X), splits.censored_test.oracle_p
        ),
    }
    print(
        f"{'oracle gap':22}{'signed':>10}{'abs':>10}{'p90 abs':>10}{'corr':>10}"
        f"{'distinct':>10}"
    )
    print("-" * 72)
    for name, g in gaps.items():
        print(
            f"{name:22}{g['mean_signed_gap']:>10.4f}{g['mean_absolute_gap']:>10.4f}"
            f"{g['p90_absolute_gap']:>10.4f}{g['correlation']:>10.4f}"
            f"{g['distinct_predictions']:>10,d}"
        )
    print()

    payload: dict[str, object] = {
        "model": {
            "best_iteration": trained.best_iteration,
            "train_rows": trained.train_rows,
            "inner_validation_rows": trained.inner_validation_rows,
            "params": dict(training.PARAMS),
        },
        "calibration": {
            "chosen": winner.method,
            "selection_folds": calibration.SELECTION_FOLDS,
            "on_calibration_split": {
                "uncalibrated": _metrics_dict(report.uncalibrated),
                **{
                    c.method: {
                        "out_of_fold": _metrics_dict(c.out_of_fold),
                        "in_sample": _metrics_dict(c.in_sample),
                        "admissible": c.admissible,
                        "degenerate_rows": c.degenerate_rows,
                        "degenerate_censored_rows": c.degenerate_censored_rows,
                    }
                    for c in report.candidates
                },
            },
        },
        "test": {
            "uncalibrated": _metrics_dict(uncalibrated_test),
            "observed": _metrics_dict(observed),
            "censored": _metrics_dict(censored),
        },
        "rupee_confusion": {
            "threshold_rule": "per-invoice break-even implied by the cost matrix",
            "threshold_median": float(np.median(thresholds)),
            "attempts_declined_on_cost": declined,
            "attempts_scored": len(test_prob),
            "cost_matrix": {
                "attempt_cost_paise": CostMatrix().attempt_cost_paise,
                "margin": CostMatrix().margin,
                "burned_attempt_paise": CostMatrix().burned_attempt_paise,
            },
            **rupees,
        },
        "oracle_gap": gaps,
        "importances": dict(training.importances(trained, top=28)),
    }

    if save:
        model_path = training.save(trained)
        calibrator_path = calibration.save(report)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        METRICS_PATH.write_text(json.dumps(payload, indent=2) + "\n")
        chart_path = charts.calibration_chart(
            report=report, observed=observed, censored=censored, test_prob=test_prob
        )
        for path in (model_path, calibrator_path, METRICS_PATH, chart_path):
            print(f"wrote {path}")

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="compute and print everything, write nothing (for checking a change "
        "against the committed numbers before overwriting them)",
    )
    args = parser.parse_args()
    run(save=not args.no_save)


if __name__ == "__main__":
    main()

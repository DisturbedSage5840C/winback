"""Fit and choose among three calibrators, then freeze the winner.

A gradient-boosted tree optimising log-loss is already roughly calibrated, so this step
often earns little — which is why it is measured rather than assumed. All three methods
scikit-learn 1.9 offers are fitted on the calibration split and compared there; the
winner is then scored **once** on the frozen test split, and that number is what
``docs/EVALUATION.md`` reports.

| Method | What it fits | When it wins |
|---|---|---|
| ``sigmoid`` | a two-parameter Platt logistic | small calibration sets; monotone distortion |
| ``isotonic`` | a free monotone step function | larger sets; overfits when thin |
| ``temperature`` | one parameter, scaling the logit | new in 1.8; the least able to overfit |

**The prefit API changed.** ``CalibratedClassifierCV(cv='prefit')`` was removed in
scikit-learn 1.9. The current spelling wraps the fitted estimator in ``FrozenEstimator``,
which is stricter than the old flag: the wrapper makes refitting impossible rather than
merely skipped, so there is no configuration in which the calibration split leaks back
into the booster.

**Why the choice is made on the calibration split and not on test.** Picking the best of
three on the test set turns "held out" into "selected on", and the reported ECE becomes
the minimum of three draws rather than an estimate of anything. The cost of doing it
correctly is that the winner may not be the best of the three on test. That is the
honest outcome and it is reported as such.

**Why the choice is made out-of-fold and not in-sample.** The first version of this file
scored each calibrator on the same rows it was fitted on, and isotonic won with an ECE
of exactly 0.0000 — because a free monotone step function fitted on 4,791 rows can
reproduce those 4,791 rows, and reproducing your own training data is not calibration.
The three methods have wildly different capacity, so an in-sample comparison between
them is not a comparison at all; it ranks them by how much they can memorise. Selection
therefore runs on out-of-fold predictions from contiguous time-ordered folds, and only
the winner is refitted on the whole split. In-sample ECE is still computed and reported
next to the out-of-fold figure, because the distance between them is the evidence that
this paragraph is necessary.

``CalibratedClassifierCV`` will not do this for us: passing ``cv=5`` alongside a
``FrozenEstimator`` is silently ignored — ``calibrated_classifiers_`` comes back with a
single element — so the folds are run here explicitly.

**Why the lowest ECE is not automatically the winner.** Isotonic regression is a step
function bounded by its outermost knots, so every score below the lowest knot it saw
maps to exactly 0.0 and every score above the highest maps to exactly 1.0. On this
problem it does that to 242 calibration rows at zero, 560 at one, and — the part that
matters — to 111 of the 118 *censored* calibration rows, the ones standing in for
exactly the region Winback exists to operate in. Not one of those 111 is a 1.0.

A probability of exactly zero is not a low probability. It is a claim that no evidence
could revise, made by a model on the strength of a region it has no data in; the log
loss of being wrong about it is infinite. Downstream it is worse than wrong, it is
self-fulfilling: the Day-5 policy ranks candidate actions by expected rupees, an
expected value of exactly zero can never be the argmax, and a calibrator that zeroes
the censored region would make Winback decline to retry precisely the invoices the
legacy policy declined to retry — reimplementing the selection bias this project was
built to remove, silently, one layer further down.

So admissibility is a gate, not a tiebreak: a calibrator that emits exactly 0.0 or
exactly 1.0 anywhere in the calibration cohort is disqualified before ECE is compared.
It is checked on the calibration cohort's own censored slice, never on the test set's,
so the gate costs nothing that was being held back.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator

from ml.dataset import Matrix, Splits
from ml.evaluate import Metrics, evaluate
from ml.train import ARTIFACTS, TrainedModel

Method = Literal["sigmoid", "isotonic", "temperature"]

#: All three, always compared. Comparing them is nearly free and reporting only the
#: winner without saying what it beat is the kind of omission that reads as a result.
METHODS: tuple[Method, ...] = ("sigmoid", "isotonic", "temperature")

#: Folds used to score the candidates out-of-sample. Contiguous rather than shuffled,
#: for the same reason every other split in this project is: shuffling would let a
#: mandate's later cycles calibrate its earlier ones.
SELECTION_FOLDS = 5

#: Tie-break order when two calibrators are indistinguishable out-of-fold. Fewer
#: parameters wins, because the one with less freedom is the one more likely to still
#: be right on rows neither of them has seen.
COMPLEXITY: dict[Method, int] = {"temperature": 0, "sigmoid": 1, "isotonic": 2}


@dataclass(frozen=True, slots=True)
class CalibrationCandidate:
    """One fitted calibrator, scored both ways and checked for degeneracy.

    ``out_of_fold`` is what selection uses. ``in_sample`` is kept only so the report can
    show the two side by side — see the module docstring on why that gap matters.
    """

    method: Method
    calibrator: CalibratedClassifierCV
    out_of_fold: Metrics
    in_sample: Metrics
    #: Calibration-cohort rows assigned exactly 0.0 or exactly 1.0, observed and
    #: censored slices pooled. Any at all is disqualifying.
    degenerate_rows: int
    #: Of those, the ones in the censored slice — reported separately because that is
    #: where the failure does its damage.
    degenerate_censored_rows: int

    @property
    def admissible(self) -> bool:
        return self.degenerate_rows == 0

    @property
    def metrics(self) -> Metrics:
        """The honest one. Anything that ranks candidates must go through here."""
        return self.out_of_fold

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return self.calibrator.predict_proba(X)[:, 1]


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """The full comparison, plus the uncalibrated baseline it has to beat."""

    uncalibrated: Metrics
    candidates: tuple[CalibrationCandidate, ...]
    winner: CalibrationCandidate

    def table(self) -> str:
        header = (
            f"{'calibrator':22}{'n':>8}{'ECE':>9}{'MCE':>9}{'Brier':>9}"
            f"{'PR-AUC':>10}{'ROC-AUC':>9}{'in-sample':>11}{'p∈{0,1}':>10}"
        )
        lines = [
            header,
            "-" * len(header),
            f"{self.uncalibrated.as_row()}{'—':>11}{'—':>10}",
        ]
        for c in self.candidates:
            flag = "" if c.admissible else "  DISQUALIFIED"
            lines.append(
                f"{c.out_of_fold.as_row()}{c.in_sample.ece:>11.4f}"
                f"{c.degenerate_rows:>10,d}{flag}"
            )
        lines.append("")
        for c in self.candidates:
            if not c.admissible:
                lines.append(
                    f"{c.method} emits an exact 0.0 or 1.0 on {c.degenerate_rows:,d} "
                    f"calibration rows, {c.degenerate_censored_rows:,d} of them in the "
                    f"censored slice — not admissible regardless of ECE"
                )
        lines.append(
            f"chosen out-of-fold on the calibration split: {self.winner.method} "
            f"(ECE {self.winner.out_of_fold.ece:.4f} vs uncalibrated "
            f"{self.uncalibrated.ece:.4f})"
        )
        return "\n".join(lines)


def _fold_bounds(n: int, folds: int) -> list[tuple[int, int]]:
    """Contiguous, near-equal index ranges covering ``range(n)`` exactly once."""
    edges = [round(i * n / folds) for i in range(folds + 1)]
    return [(lo, hi) for lo, hi in pairwise(edges) if hi > lo]


def _out_of_fold(
    trained: TrainedModel, calibration: Matrix, method: Method
) -> np.ndarray:
    """Predict every calibration row from a calibrator that never saw it.

    A fold whose training portion carries only one class cannot fit a calibrator, and
    that is a real possibility on a slice this imbalanced. Rather than silently
    substituting the uncalibrated score — which would flatter whichever method the fold
    was standing in for — the fold is left as ``nan`` and excluded from the score, so
    the reported ``n`` says how much evidence the choice actually rests on.
    """
    frozen = FrozenEstimator(trained.model)
    out = np.full(len(calibration), np.nan, dtype=np.float64)
    for lo, hi in _fold_bounds(len(calibration), SELECTION_FOLDS):
        mask = np.ones(len(calibration), dtype=bool)
        mask[lo:hi] = False
        y_fit = calibration.y[mask]
        if len(np.unique(y_fit)) < 2:
            continue
        fold = CalibratedClassifierCV(frozen, method=method)
        fold.fit(calibration.X[mask], y_fit)
        out[lo:hi] = fold.predict_proba(calibration.X[lo:hi])[:, 1]
    return out


def _degenerate(probabilities: np.ndarray) -> int:
    """Rows asserting certainty. See the module docstring on why this disqualifies."""
    return int(((probabilities <= 0.0) | (probabilities >= 1.0)).sum())


def fit_calibrators(
    trained: TrainedModel, calibration: Matrix, censored: Matrix
) -> tuple[CalibrationCandidate, ...]:
    """Score every method out-of-fold, then refit each on the whole calibration split.

    Both steps are needed. The out-of-fold pass produces the number that chooses; the
    refit produces the artifact that ships, because a calibrator fitted on four fifths
    of the split would be throwing away a fifth of the only data it is allowed to use.

    ``censored`` is the calibration cohort's censored slice, used only for the
    admissibility check.
    """
    frozen = FrozenEstimator(trained.model)
    candidates: list[CalibrationCandidate] = []
    for method in METHODS:
        oof = _out_of_fold(trained, calibration, method)
        scored = ~np.isnan(oof)

        calibrator = CalibratedClassifierCV(frozen, method=method)
        calibrator.fit(calibration.X, calibration.y)

        on_observed = calibrator.predict_proba(calibration.X)[:, 1]
        on_censored = calibrator.predict_proba(censored.X)[:, 1]
        degenerate_censored = _degenerate(on_censored)

        candidates.append(
            CalibrationCandidate(
                method=method,
                calibrator=calibrator,
                out_of_fold=evaluate(
                    calibration.y[scored], oof[scored], slice_name=f"{method} (oof)"
                ),
                in_sample=evaluate(
                    calibration.y, on_observed, slice_name=f"{method} (in-sample)"
                ),
                degenerate_rows=_degenerate(on_observed) + degenerate_censored,
                degenerate_censored_rows=degenerate_censored,
            )
        )
    return tuple(candidates)


def choose(
    trained: TrainedModel, calibration: Matrix, censored: Matrix
) -> CalibrationReport:
    """Fit all three, disqualify the degenerate ones, rank the rest on out-of-fold ECE."""
    uncalibrated = evaluate(
        calibration.y,
        trained.predict_proba(calibration.X),
        slice_name="uncalibrated (calib)",
    )
    candidates = fit_calibrators(trained, calibration, censored)

    admissible = [c for c in candidates if c.admissible]
    if not admissible:
        # Deliberately fatal. The fallback a reader would expect here — ship the
        # uncalibrated booster — is a decision about what the system predicts, and it
        # should be made by a person reading this message, not by an except branch.
        raise RuntimeError(
            "every calibrator emits an exact 0.0 or 1.0 on the calibration cohort: "
            + ", ".join(f"{c.method}={c.degenerate_rows}" for c in candidates)
        )

    winner = min(
        admissible,
        key=lambda c: (round(c.out_of_fold.ece, 4), COMPLEXITY[c.method]),
    )
    return CalibrationReport(
        uncalibrated=uncalibrated, candidates=candidates, winner=winner
    )


def save(report: CalibrationReport, *, directory: Path = ARTIFACTS) -> Path:
    """Persist the winning calibrator. It wraps the frozen booster, so this is the
    single artifact the policy layer and the API both load."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "calibrator_v1.joblib"
    joblib.dump({"method": report.winner.method, "model": report.winner.calibrator}, path)
    return path


def observed_versus_censored(
    winner: CalibrationCandidate, splits: Splits
) -> tuple[Metrics, Metrics]:
    """Calibration on the slice the merchant saw, and on the slice it never did.

    This is the credibility centrepiece of the whole model section. The legacy policy
    refused to retry anything under ₹500 or on netbanking, so the model has no training
    evidence in that region — but the oracle knows what would have happened there. A
    model that is calibrated where it has data and badly miscalibrated where it does not
    is a model whose confidence should not be trusted off-distribution, and saying so
    with a number is worth more than any headline AUC.
    """
    observed = evaluate(
        splits.test.y,
        winner.predict_proba(splits.test.X),
        slice_name="test · observed",
    )
    censored = evaluate(
        splits.censored_test.y,
        winner.predict_proba(splits.censored_test.X),
        slice_name="test · censored",
    )
    return observed, censored

"""Fit the uncalibrated XGBoost classifier for ``P(success | attempt, action, slot)``.

**Where early stopping looks.** Boosting needs a validation set to decide when to stop,
and there are three candidates: the test split (disqualified — it is scored once, at the
end), the calibration split (tempting, and wrong), or a slice carved out of train.

The calibration split is the subtle one. Spending it on early stopping *and* on fitting
the calibrator means the calibrator is fitted on data the model already stopped against,
which biases the reliability diagram in the flattering direction — and a reliability
diagram is the one artifact whose entire value is that it is not flattering. So train is
split again, by time, and the newest 15% of training mandates become the inner
validation set. The calibration split is never touched here.

**Class weighting is deliberately absent.** 88% of attempts capture, so the failures are
the minority — but the model's job is to produce a *probability*, not a decision, and
``scale_pos_weight`` deforms probabilities to move a threshold that this system never
applies. The policy layer converts probability to action using a rupee cost matrix, which
is the honest place for the asymmetry to live.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from xgboost import XGBClassifier

from ml.dataset import Matrix, Splits

ARTIFACTS = Path("ml/artifacts")

#: Fixed a priori, not searched. A hyperparameter sweep needs a validation set to score
#: against, and every split here already has a job; a sweep would have to reuse one and
#: the honest gain over sensible defaults on 26k rows and 28 features is small. Modest
#: depth and a low learning rate favour a smooth, well-ordered probability surface, which
#: is what calibration then has to work with.
PARAMS: dict[str, object] = {
    "n_estimators": 600,
    "max_depth": 5,
    "learning_rate": 0.05,
    "subsample": 0.85,
    "colsample_bytree": 0.85,
    "min_child_weight": 10,
    "reg_lambda": 2.0,
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "tree_method": "hist",
    "random_state": 20260828,
    "n_jobs": 4,
}

#: Fraction of the training cohort held back, by time, to stop boosting on.
INNER_VALIDATION_FRACTION = 0.15


@dataclass(frozen=True, slots=True)
class TrainedModel:
    """A fitted booster plus everything needed to reproduce and audit it."""

    model: XGBClassifier
    feature_names: tuple[str, ...]
    best_iteration: int
    inner_validation_rows: int
    train_rows: int

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """P(captured) for each row, uncalibrated."""
        return self.model.predict_proba(X)[:, 1]


def _time_ordered_inner_split(matrix: Matrix) -> tuple[np.ndarray, np.ndarray]:
    """Indices for (fit, inner-validation), split by position in the training matrix.

    Rows are emitted in dataset order, which is mandate order, which the generator built
    in ascending ``mandate_start``. So a tail slice is the newest mandates — the same
    kind of split that separates train from test, applied once more inside train, rather
    than a random shuffle that would let a mandate's later cycles validate its earlier
    ones.
    """
    n = len(matrix)
    cut = int(n * (1.0 - INNER_VALIDATION_FRACTION))
    return np.arange(cut), np.arange(cut, n)


def train(splits: Splits) -> TrainedModel:
    """Fit on the training cohort, stopping on its newest slice."""
    fit_idx, val_idx = _time_ordered_inner_split(splits.train)
    X, y = splits.train.X, splits.train.y

    model = XGBClassifier(**PARAMS, early_stopping_rounds=40)
    model.fit(
        X[fit_idx],
        y[fit_idx],
        eval_set=[(X[val_idx], y[val_idx])],
        verbose=False,
    )

    return TrainedModel(
        model=model,
        feature_names=splits.train.feature_names,
        best_iteration=int(model.best_iteration),
        inner_validation_rows=len(val_idx),
        train_rows=len(fit_idx),
    )


def importances(trained: TrainedModel, *, top: int = 12) -> list[tuple[str, float]]:
    """Gain-ranked features, for the record in ``docs/EVALUATION.md``.

    Gain rather than the default weight: weight counts how often a feature was split on,
    which rewards high-cardinality columns like ``amount_rupees`` for being splittable
    rather than for being informative.
    """
    booster = trained.model.get_booster()
    scores = booster.get_score(importance_type="gain")
    named = {
        trained.feature_names[int(key[1:])]: value
        for key, value in scores.items()
        if key.startswith("f")
    }
    total = sum(named.values()) or 1.0
    ranked = sorted(named.items(), key=lambda kv: kv[1], reverse=True)
    return [(name, value / total) for name, value in ranked[:top]]


def save(trained: TrainedModel, *, directory: Path = ARTIFACTS) -> Path:
    """Write the booster and its column order side by side.

    The feature names are saved with the model rather than re-derived at load time. A
    model whose columns are inferred from whatever the caller happens to build is a model
    that will one day score a reordered matrix and report nothing wrong.
    """
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / "model_v1.json"
    trained.model.get_booster().save_model(str(model_path))
    (directory / "model_v1.meta.json").write_text(
        json.dumps(
            {
                "feature_names": list(trained.feature_names),
                "best_iteration": trained.best_iteration,
                "train_rows": trained.train_rows,
                "inner_validation_rows": trained.inner_validation_rows,
                "params": {k: v for k, v in PARAMS.items()},
            },
            indent=2,
        )
        + "\n"
    )
    return model_path

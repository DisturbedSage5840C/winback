"""What the frozen artifacts on disk are worth.

Everything in ``ml/`` up to this point is about producing a model. This file is about
the model surviving the trip through a file: the numbers ``docs/EVALUATION.md`` quotes
were produced by an object in memory, and what the policy will actually run is a pickle
and a JSON file. Those are only the same model if something checks, and nothing did
until here.

The strongest test in the file is the first one. It rebuilds the frozen test cohort,
scores it with the artifacts as loaded from disk, and asserts the resulting ECE is
*exactly* the one committed in ``metrics_v1.json`` — not close to it. Approximate
agreement would be satisfied by a subtly different model, which is the failure this is
looking for.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from ml import scorer
from ml.__main__ import METRICS_PATH
from ml.dataset import Splits, build_splits
from ml.evaluate import evaluate
from ml.train import ARTIFACTS
from sim.generate import Dataset, build_dataset


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return build_dataset()


@pytest.fixture(scope="module")
def splits(dataset: Dataset) -> Splits:
    return build_splits(dataset)


@pytest.fixture(scope="module")
def loaded() -> scorer.Scorer:
    return scorer.load_scorer(ARTIFACTS)


@pytest.fixture(scope="module")
def committed() -> dict:
    return json.loads(METRICS_PATH.read_text())


# ------------------------------------------------- the artifacts are the model


def test_the_artifacts_on_disk_reproduce_the_committed_test_metrics(
    loaded: scorer.Scorer, splits: Splits, committed: dict
) -> None:
    """The report and the runtime agree, to the last digit.

    ``docs/EVALUATION.md`` claims an ECE on the observed test slice. This scores that
    same cohort through the load path the policy uses and asserts the identical number
    comes back, which is what entitles the document to describe the thing that runs.
    """
    probabilities = loaded.calibrator.predict_proba(splits.test.X)[:, 1]
    measured = evaluate(splits.test.y, probabilities, slice_name="observed (test)")
    assert measured.ece == pytest.approx(committed["test"]["observed"]["ece"], abs=1e-12)
    assert measured.brier == pytest.approx(committed["test"]["observed"]["brier"], abs=1e-12)
    assert measured.n == committed["test"]["observed"]["n"]


def test_the_readable_artifact_is_the_one_that_runs(loaded: scorer.Scorer, splits: Splits) -> None:
    """``model_v1.json`` is not decorative.

    A reviewer can open the JSON booster and read its trees; they cannot read the
    pickle. That is only useful if the two are the same model, so this scores both over
    the frozen test cohort and demands they agree exactly.
    """
    gap = scorer.verify_artifacts(splits.test.X, directory=ARTIFACTS)
    assert gap == 0.0


def test_the_winning_calibrator_is_named_by_the_artifact(
    loaded: scorer.Scorer, committed: dict
) -> None:
    """Which calibrator won is a fact about this run, and it travels with the run."""
    assert loaded.method == committed["calibration"]["chosen"]


# ------------------------------------------------- the column order is enforced


def _row(loaded: scorer.Scorer, values: np.ndarray) -> dict[str, float]:
    return {name: float(value) for name, value in zip(loaded.feature_names, values, strict=True)}


def test_a_row_is_assembled_by_name_not_by_position(loaded: scorer.Scorer, splits: Splits) -> None:
    """The dictionary path and the matrix path give the same answer."""
    row = _row(loaded, splits.test.X[0])
    shuffled = dict(reversed(list(row.items())))
    assert loaded.score_one(shuffled) == pytest.approx(
        float(loaded.calibrator.predict_proba(splits.test.X[:1])[:, 1][0])
    )


def test_the_frozen_order_is_load_bearing(loaded: scorer.Scorer, splits: Splits) -> None:
    """A matrix built in the wrong order scores cleanly and answers wrongly.

    This is the failure the frozen names exist to prevent, demonstrated rather than
    asserted: nothing raises, no shape is violated, and the probability is simply not
    the right one. A model that inferred its columns from the caller would have no way
    to notice.
    """
    correct = loaded.calibrator.predict_proba(splits.test.X[:64])[:, 1]
    permuted = splits.test.X[:64][:, ::-1]
    wrong = loaded.calibrator.predict_proba(permuted)[:, 1]
    assert not np.allclose(correct, wrong)


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda row: {k: v for k, v in row.items() if k != "attempt_number"}, "missing"),
        (lambda row: {**row, "p_success": 0.9}, "unexpected"),
    ],
    ids=["missing_feature", "extra_feature"],
)
def test_a_row_that_is_not_the_frozen_feature_set_is_refused(
    loaded: scorer.Scorer, splits: Splits, mutate, fragment: str
) -> None:
    """Missing is the obvious error. Extra is the one worth catching.

    ``p_success`` is the oracle's own answer and is excluded from the feature set by
    ``ml/features.py`` on purpose; a caller that reaches this far holding it has built a
    leaking row, and a scorer that quietly dropped the column would let that row look
    fine all the way to the audit trail.
    """
    with pytest.raises(scorer.ArtifactError, match=fragment):
        loaded.score([mutate(_row(loaded, splits.test.X[0]))])


def test_scoring_nothing_returns_nothing(loaded: scorer.Scorer) -> None:
    """The policy enumerates candidates, and a guardrail that legally permits none is a
    normal outcome — a write-off — not an error condition."""
    assert loaded.score([]).shape == (0,)


# ------------------------------------------------- missing and mismatched artifacts


def test_a_missing_artifact_says_how_to_produce_it(tmp_path) -> None:
    with pytest.raises(scorer.ArtifactError, match="python -m ml"):
        scorer.load_scorer(tmp_path)


def test_artifacts_from_different_runs_are_refused(tmp_path, loaded: scorer.Scorer) -> None:
    """A calibrator expecting 28 columns beside metadata naming 27 is two runs mixed
    together, and it would otherwise fail as a shape error somewhere further down."""
    meta = json.loads((ARTIFACTS / "model_v1.meta.json").read_text())
    meta["feature_names"] = meta["feature_names"][:-1]
    (tmp_path / "model_v1.meta.json").write_text(json.dumps(meta))
    (tmp_path / "calibrator_v1.joblib").write_bytes(
        (ARTIFACTS / "calibrator_v1.joblib").read_bytes()
    )
    with pytest.raises(scorer.ArtifactError, match="different runs"):
        scorer.load_scorer(tmp_path)

"""A regression test that protects the frozen model, not one that evaluates it.

``test_scorer.py`` already proves the artifacts on disk reproduce the committed test
metrics — but that proof runs through ``sim.generate.build_dataset`` and
``ml.dataset.build_splits``, which is a lot of machinery between "did the artifacts
change" and the answer. This file removes that machinery: eight fixed feature rows,
hand-generated once from a seeded RNG and committed alongside their expected
probabilities in ``testdata/frozen_model_golden.json``, scored again here on every run.

A failure here means something changed underneath ``model_v1.json`` or
``calibrator_v1.joblib`` without the model itself changing on purpose — a dependency
bump that alters floating-point behaviour, a pickling difference across a joblib or
scikit-learn upgrade, a column silently reordered. It is not a test of whether the model
is *good*; ``ml/evaluate.py`` and ``docs/EVALUATION.md`` already answer that. It is a
test of whether the model is still *this* model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml import scorer
from ml.train import ARTIFACTS

GOLDEN_PATH = Path(__file__).parent / "testdata" / "frozen_model_golden.json"


@pytest.fixture(scope="module")
def loaded() -> scorer.Scorer:
    return scorer.load_scorer(ARTIFACTS)


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def test_the_frozen_artifacts_still_score_the_golden_fixture_unchanged(
    loaded: scorer.Scorer, golden: dict
) -> None:
    assert tuple(golden["feature_names"]) == loaded.feature_names, (
        "the frozen column order moved — every row in the golden fixture is now "
        "assembled in the wrong order, which is exactly the failure this test exists "
        "to catch"
    )
    scored = [loaded.score_one(row) for row in golden["rows"]]
    for expected, actual in zip(golden["probabilities"], scored, strict=True):
        assert actual == pytest.approx(expected, abs=1e-12)

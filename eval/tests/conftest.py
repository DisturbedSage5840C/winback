"""One replay run, shared by every test in this package.

The harness is the most expensive thing in the test suite — four arms over every failed
invoice in the test cohort, each presentment a call into the oracle — and all three test
modules assert against the same run. Building it once at session scope keeps `pytest
eval/` in seconds, and, more usefully, means the arms tests and the bootstrap tests are
provably talking about the same numbers.

The fixtures are deliberately the real thing: the frozen dataset, the frozen model, the
real rate table. A replay harness tested against a fixture of its own making would prove
only that it is self-consistent.
"""

from __future__ import annotations

import pytest

from eval.counterfactual import EvalRun, ReplayCase, build_cases, run_evaluation
from ml.features import BankMethodRates
from ml.scorer import Scorer, load_scorer
from ml.train import ARTIFACTS
from sim.generate import Dataset, build_dataset

#: Not the production ``run_id``. Anything written under this name is a test artefact.
TEST_RUN_ID = "eval_test"


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    return build_dataset()


@pytest.fixture(scope="session")
def scorer() -> Scorer:
    return load_scorer(ARTIFACTS)


@pytest.fixture(scope="session")
def rates(dataset: Dataset) -> BankMethodRates:
    """Fitted on train only — the same table the policy sees in production."""
    return BankMethodRates.fit(dataset, cohort="train")


@pytest.fixture(scope="session")
def cases(dataset: Dataset) -> tuple[ReplayCase, ...]:
    return build_cases(dataset, cohort="test")


@pytest.fixture(scope="session")
def run(dataset: Dataset, scorer: Scorer, rates: BankMethodRates) -> EvalRun:
    return run_evaluation(
        dataset,
        scorer=scorer,
        rates=rates,
        run_id=TEST_RUN_ID,
        model_version="v1",
    )


@pytest.fixture(scope="session")
def arms_by_id(run: EvalRun) -> dict:
    return {arm.arm: arm for arm in run.arms}

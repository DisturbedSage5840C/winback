"""One workbench over the frozen cohort, shared by every test in this package.

**No LLM runs here, on purpose.** These tests exercise the rails — the ledger, the
permission gate, the audit writer, the adapters — by calling them directly. That is not a
shortcut around testing the agent; it is the point. Everything this package guarantees is
guaranteed *whatever the model does*, so a test that had to prompt a model to observe the
guarantee would be testing the wrong thing, and would be non-deterministic besides.

The one thing these tests cannot show is that the SDK wires the callbacks up the way the
docs say. That was established by running the real orchestrator, and both defects it
turned up — ``allowed_tools`` shadowing ``can_use_tool``, and ``tool_response`` arriving
as a bare list — are pinned below by tests that would have caught them.

The fixtures are the real dataset, the real frozen model and the real rate table, for the
same reason ``eval/tests`` uses them: a gate tested against a fixture of its own making
proves only that it is self-consistent.
"""

from __future__ import annotations

import pytest

from agent.adapters.simulated import SimulatedAdapter
from agent.tools import Workbench, build_tools, workbench_from_dataset
from ml.features import BankMethodRates
from ml.scorer import Scorer, load_scorer
from ml.train import ARTIFACTS
from sim.generate import Dataset, build_dataset

#: Anything written to the database under this name is a test artefact. ``audit_log`` is
#: append-only, so these rows are permanent — which is exactly why the tests that touch
#: it are marked ``db`` and kept to the few that genuinely need a real INSERT.
TEST_RUN_ID = "agent_test"


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    return build_dataset()


@pytest.fixture(scope="session")
def scorer() -> Scorer:
    return load_scorer(ARTIFACTS)


@pytest.fixture(scope="session")
def rates(dataset: Dataset) -> BankMethodRates:
    return BankMethodRates.fit(dataset, cohort="train")


@pytest.fixture
def bench(scorer: Scorer, rates: BankMethodRates) -> Workbench:
    """Function-scoped: the ledger is mutable state and must not leak between tests."""
    return workbench_from_dataset(scorer=scorer, rates=rates, cohort="test")


@pytest.fixture
def tools(bench: Workbench) -> dict:
    return {t.name: t for t in build_tools(bench)}


@pytest.fixture
def invoice_id(bench: Workbench) -> str:
    """A stable pick from the frozen cohort, so failures name the same invoice twice."""
    return sorted(bench.cases)[0]


@pytest.fixture
def adapter() -> SimulatedAdapter:
    return SimulatedAdapter.from_dataset(cohort="test")

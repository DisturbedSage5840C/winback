"""The audit writer against a real PostgreSQL, inside a transaction that is rolled back.

These are the tests that would have caught the ``-Infinity`` defect end to end. Every
other test in this package feeds the writer a payload and checks what it *would* send;
only a real ``INSERT`` finds out that ``jsonb`` will not take the bytes. The bug shipped
precisely because nothing here existed yet.

Following ``core/tests/test_append_only.py``: everything happens inside one transaction
that is rolled back, so the suite leaves no residue in an append-only table it could not
clean up afterwards.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import psycopg
import pytest

from agent import hooks
from agent.hooks import AGENT_ID, MODEL_VERSION, AuditWriter
from agent.tools import MONEY_TOOL
from core.db import agent_connection
from eval.counterfactual import DECISION_LAG_HOURS

pytestmark = pytest.mark.db

RUN_ID = "agent_test_rollback"


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """One open transaction, rolled back at the end whatever the tests did."""
    try:
        with agent_connection() as connection:
            connection.rollback()  # start clean; the context manager would commit
            yield connection
            connection.rollback()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment guard
        pytest.skip(f"Postgres not reachable ({exc}); run `docker compose up -d`")


@pytest.fixture
def writer(bench, conn, monkeypatch) -> AuditWriter:
    """A writer whose every statement lands in the rolled-back transaction above."""

    @contextmanager
    def _same_connection():
        yield conn

    monkeypatch.setattr(hooks, "agent_connection", _same_connection)
    return AuditWriter(bench=bench, run_id=RUN_ID, arm="D")


async def _plan_for(tools, bench, invoice_id: str) -> str:
    now = bench.cases[invoice_id].first_charge.attempted_at + timedelta(hours=DECISION_LAG_HOURS)
    await tools["assess_recoverability"].handler({"invoice_id": invoice_id, "now": now.isoformat()})
    return now.isoformat()


def _payload(invoice_id: str, execute_at: str, **over) -> dict:
    return {
        "invoice_id": invoice_id,
        "action": "retry",
        "execute_at": execute_at,
        "verdict": "APPROVE",
        "authorizing_rule": "npci_1_plus_3: attempt 2/4 permitted",
        "authorised": True,
        **over,
    }


async def test_a_decision_row_lands_with_its_full_candidate_set(writer, tools, bench, conn):
    """The row the drill-down drawer reads. A candidate set that would not serialise is
    the whole defect this file exists for — including the losing candidates, which are
    the ones carrying ``-inf``."""
    invoice_id = sorted(bench.cases)[0]
    at = await _plan_for(tools, bench, invoice_id)

    decision_id = writer.record_decision(_payload(invoice_id, at))
    assert decision_id is not None

    row = conn.execute("SELECT * FROM decisions WHERE decision_id = %s", (decision_id,)).fetchone()
    assert row["run_id"] == RUN_ID
    assert row["arm"] == "D"
    assert row["invoice_id"] == invoice_id
    assert row["model_version"] == MODEL_VERSION
    assert row["guardrail_verdict"] == "APPROVE"
    assert row["final_action"] == "retry"
    assert row["decided_by"] == "agent"

    candidates = row["candidate_set"]
    if isinstance(candidates, str):
        candidates = json.loads(candidates)
    assert len(candidates) == len(bench.plans[invoice_id].candidates)
    assert any(c["ruled_out"] for c in candidates) or all(
        c["expected_value_paise"] is not None for c in candidates
    )


async def test_a_decision_row_survives_a_ruled_out_candidate(writer, tools, bench, conn):
    """The regression, stated as the property. Every invoice in the cohort must be
    writable — not just the ones whose candidates all scored finite."""
    written = 0
    for invoice_id in sorted(bench.cases)[:20]:
        at = await _plan_for(tools, bench, invoice_id)
        assert writer.record_decision(_payload(invoice_id, at)) is not None
        written += 1

    count = conn.execute(
        "SELECT count(*) AS n FROM decisions WHERE run_id = %s", (RUN_ID,)
    ).fetchone()["n"]
    assert count == written == 20


async def test_an_action_row_points_back_at_the_decision_that_authorised_it(
    writer, tools, bench, conn
):
    """``audit_log.decision_id`` NULL on an executed action is the signature of a
    ``decisions`` write that failed silently — which is exactly how the defect first
    showed itself."""
    invoice_id = sorted(bench.cases)[0]
    at = await _plan_for(tools, bench, invoice_id)
    decision_id = writer.record_decision(_payload(invoice_id, at))

    writer.record_action(
        _payload(
            invoice_id,
            at,
            execution_mode="simulated",
            outcome="recovered",
            recovered_paise=109100,
            customer_hash="abc123def456",
        ),
        trigger="batch_scan",
    )

    row = conn.execute(
        "SELECT * FROM audit_log WHERE run_id = %s AND subject_id = %s", (RUN_ID, invoice_id)
    ).fetchone()
    assert row["decision_id"] == decision_id
    assert row["agent_id"] == AGENT_ID
    assert row["subject_type"] == "invoice"
    assert row["execution_mode"] == "simulated"
    assert row["outcome"] == "recovered"
    assert row["recovered_amount_paise"] == 109100


async def test_a_gate_denial_lands_as_a_blocked_row(writer, bench, conn):
    """The row the demo puts on screen. ``PostToolUse`` never fires for a refused call,
    so without the gate writing this directly there would be no evidence at all that the
    cap was enforced against an agent that wanted to act."""
    invoice_id = sorted(bench.cases)[0]
    writer.record_denial(
        MONEY_TOOL, {"invoice_id": invoice_id}, "no_guardrail_approval: nothing on record"
    )

    row = conn.execute(
        "SELECT * FROM audit_log WHERE run_id = %s AND trigger = 'permission_gate'", (RUN_ID,)
    ).fetchone()
    assert row["outcome"] == "blocked"
    assert row["action_taken"] == "retry"
    assert row["stop_reason"] == "no_guardrail_approval"
    assert row["recovered_amount_paise"] == 0


async def test_observed_data_carries_a_hash_and_never_a_customer_id(writer, bench, conn):
    """Redaction at write time, so the raw value was never in the table — rather than at
    render time, which would make the protection a property of the dashboard."""
    invoice_id = sorted(bench.cases)[0]
    case = bench.cases[invoice_id]
    writer.record_denial(MONEY_TOOL, {"invoice_id": invoice_id}, "reason")

    row = conn.execute(
        "SELECT observed_data FROM audit_log WHERE run_id = %s", (RUN_ID,)
    ).fetchone()
    observed = row["observed_data"]
    if isinstance(observed, str):
        observed = json.loads(observed)

    assert observed["customer_hash"] == case.customer.customer_hash
    assert case.customer.customer_id not in json.dumps(observed)


async def test_an_invoice_outside_the_cohort_writes_no_decision(writer, conn):
    assert writer.record_decision(_payload("inv_not_real", "2026-05-01T09:00:00+05:30")) is None
    assert writer.rows_written == 0

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
from agent.adapters.simulated import ATTEMPT_ROW
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


async def _approved_retry_slot(tools, bench, invoice_id: str) -> str:
    """Assess, take the first slot the guardrail approves a retry for, and spend the
    guardrail call so an unspent approval is on record for ``execute_recovery``."""
    from compliance.guardrail import ActionKind

    await _plan_for(tools, bench, invoice_id)
    for candidate in bench.plans[invoice_id].candidates:
        if candidate.kind is ActionKind.RETRY and candidate.allowed:
            slot = candidate.execute_at.isoformat()
            await tools["compliance_guardrail"].handler(
                {"invoice_id": invoice_id, "action": "retry", "execute_at": slot}
            )
            return slot
    raise AssertionError(f"no approved retry slot for {invoice_id}")


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


async def test_a_resumed_run_does_not_collide_the_decision_id_with_the_crashed_attempt(
    writer, tools, bench, conn
):
    """The defect closed in ``agent/hooks.py``, stated as regression.

    ``_already_worked`` (``agent/orchestrator.py``) deliberately re-works exactly the
    invoice that died between a guardrail approval and the tool call — the one case with
    a ``decisions`` row already on record for this ``run_id``/``arm``/``invoice_id``. A
    resumed run builds a brand-new :class:`AuditWriter` with an empty in-memory
    ``_decisions``, which is exactly what regenerated the crashed run's own
    ``decision_id`` and raised a ``UniqueViolation`` on it. Counting from the ``decisions``
    table itself, instead of from this process's memory, is what closes it."""
    invoice_id = sorted(bench.cases)[0]
    at = await _plan_for(tools, bench, invoice_id)
    first_id = writer.record_decision(_payload(invoice_id, at))
    assert first_id is not None

    resumed = AuditWriter(bench=bench, run_id=RUN_ID, arm="D")
    second_id = resumed.record_decision(_payload(invoice_id, at))
    assert second_id is not None
    assert second_id != first_id

    rows = conn.execute(
        "SELECT decision_id FROM decisions WHERE run_id = %s AND invoice_id = %s",
        (RUN_ID, invoice_id),
    ).fetchall()
    assert {row["decision_id"] for row in rows} == {first_id, second_id}


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


async def test_a_presentment_actually_lands_a_row_in_payment_attempts(writer, tools, bench, conn):
    """The defect this file was missing, stated end to end.

    ``SimulatedAdapter.present`` builds a full ``AttemptRow`` and ``ExecutionResult``
    carries it as ``metadata[ATTEMPT_ROW]`` (``agent/adapters/simulated.py``).
    ``agent/tools.py::_execute`` appends the whole row — metadata included — to
    ``bench.executions``, then strips ``metadata`` before handing anything back to the
    agent, because the oracle's true probability rides along with it and must never be
    agent-visible. What reached ``record_action`` used to be exactly that stripped
    payload, so the row the schema was built to hold was constructed and thrown away on
    every single run. This test drives a real tool call through ``execute_recovery``,
    parses the JSON the agent would actually receive back — the same round trip
    ``_text()``/``json.dumps`` puts it through — and hands that to ``record_action``
    exactly as ``agent/hooks.py::make_audit_hook`` would, then checks the database
    rather than the in-memory row.
    """
    invoice_id = sorted(bench.cases)[0]
    slot = await _approved_retry_slot(tools, bench, invoice_id)

    result = await tools["execute_recovery"].handler({"invoice_id": invoice_id, "execute_at": slot})
    payload = json.loads(result["content"][0]["text"])
    assert payload.get("action") == "retry", payload

    writer.record_action(payload, trigger="batch_scan")

    attempt = next(
        row["metadata"][ATTEMPT_ROW]
        for row in bench.executions
        if row["invoice_id"] == invoice_id and row["execute_at"] == slot
    )

    row = conn.execute(
        "SELECT * FROM payment_attempts WHERE attempt_id = %s", (attempt.attempt_id,)
    ).fetchone()
    assert row is not None
    assert row["invoice_id"] == invoice_id
    assert row["subscription_id"] == attempt.subscription_id
    assert row["attempt_number"] == attempt.attempt_number
    assert row["run_id"] == RUN_ID
    assert row["arm"] == "D"

    audit_row = conn.execute(
        "SELECT * FROM audit_log WHERE run_id = %s AND subject_id = %s", (RUN_ID, invoice_id)
    ).fetchone()
    assert audit_row["action_taken"] == "retry"


async def test_a_recovered_outcome_moves_the_invoice_off_the_worklist(writer, tools, bench, conn):
    """``api/main.py``'s ``/worklist`` is filtered ``WHERE status = 'at_risk'`` -- the
    only thing that makes it shrink as the agent works, per its own docstring. Nothing
    wrote ``invoices.status`` at runtime until now, so a batch that recovered every
    invoice it touched left the live queue looking identical before and after."""
    invoice_id = sorted(bench.cases)[0]
    at = await _plan_for(tools, bench, invoice_id)

    before = conn.execute(
        "SELECT status FROM invoices WHERE invoice_id = %s", (invoice_id,)
    ).fetchone()
    assert before["status"] == "at_risk"

    writer.record_action(
        _payload(
            invoice_id,
            at,
            execution_mode="simulated",
            outcome="recovered",
            recovered_paise=109100,
        ),
        trigger="batch_scan",
    )

    after = conn.execute(
        "SELECT status FROM invoices WHERE invoice_id = %s", (invoice_id,)
    ).fetchone()
    assert after["status"] == "recovered"


async def test_a_write_off_moves_the_invoice_to_written_off(writer, bench, conn):
    """The other conclusion that must leave the live worklist. A write-off calls no
    tool -- there is nothing to execute -- so this reaches ``record_action`` only
    through :meth:`AuditWriter.record_conclusion`, exercised here at the level below it
    since building a real all-candidates-refused plan is what that method's own caller
    is for."""
    invoice_id = sorted(bench.cases)[0]
    writer.record_action(
        {"invoice_id": invoice_id, "action": "write_off", "execution_mode": "simulated"},
        trigger="batch_scan",
        outcome="blocked",
        stop_reason="bd_hard_not_retryable",
    )
    row = conn.execute(
        "SELECT status FROM invoices WHERE invoice_id = %s", (invoice_id,)
    ).fetchone()
    assert row["status"] == "written_off"


async def test_a_denied_action_leaves_the_invoice_at_risk(writer, bench, conn):
    """A refused single action is not a conclusion. The cap may still have budget, or a
    later slot may be legal -- only ``recovered`` and ``write_off`` may move
    ``invoices.status`` off ``at_risk``."""
    invoice_id = sorted(bench.cases)[0]
    writer.record_denial(
        MONEY_TOOL, {"invoice_id": invoice_id}, "no_guardrail_approval: nothing on record"
    )
    row = conn.execute(
        "SELECT status FROM invoices WHERE invoice_id = %s", (invoice_id,)
    ).fetchone()
    assert row["status"] == "at_risk"


async def test_a_nudge_writes_no_payment_attempts_row(writer, tools, bench, conn):
    """A nudge has no presentment behind it — ``SimulatedAdapter.nudge`` produces no
    ``AttemptRow`` at all (``agent/adapters/simulated.py``) — so ``record_action`` must
    not manufacture one. Only ``execute_recovery`` (a RETRY) can ever populate
    ``payment_attempts``."""
    invoice_id = sorted(bench.cases)[0]
    now = await _plan_for(tools, bench, invoice_id)
    await tools["compliance_guardrail"].handler(
        {"invoice_id": invoice_id, "action": "nudge", "execute_at": now}
    )
    result = await tools["simulated_notify"].handler({"invoice_id": invoice_id, "execute_at": now})
    payload = json.loads(result["content"][0]["text"])
    assert payload.get("action") == "nudge", payload

    writer.record_action(payload, trigger="batch_scan")

    count = conn.execute(
        "SELECT count(*) AS n FROM payment_attempts WHERE run_id = %s", (RUN_ID,)
    ).fetchone()["n"]
    assert count == 0


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

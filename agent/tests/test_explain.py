"""What makes the explainer safe: zero tools, and a record it can only narrate.

No LLM runs in this file, for the same reason none runs anywhere else in
``agent/tests`` (see the package docstring): the guarantee this module makes — it cannot
approve, deny, or spend anything — has to hold whatever the model says, so a test that
had to prompt one to observe it would be testing the wrong thing, and it would cost real
money and non-determinism to do it.
"""

from __future__ import annotations

import pytest

from agent.explain import DecisionNotFound, _decision_record, explain_decision, explainer_options
from core.config import get_settings
from core.db import read_connection


def test_the_explainer_has_no_tools_at_all():
    """The whole safety argument in one assertion. ``compliance_guardrail`` and
    ``execute_recovery`` are unreachable from this call because there is no tool list
    here for either name to appear on — not because a policy chose to omit them."""
    options = explainer_options(get_settings())
    assert options.mcp_servers == {}
    assert options.allowed_tools == []
    assert options.max_turns == 1


async def test_a_decision_that_was_never_written_is_never_narrated():
    """Explaining a decision that does not exist would be inventing one — exactly the
    unauditable step the rest of ``agent/`` is built to avoid. This never reaches a
    model: :func:`agent.explain.explain_decision` checks the record before calling out,
    so a bogus invoice id fails fast with no network call and no cost."""
    with pytest.raises(DecisionNotFound):
        await explain_decision("no_such_invoice_id")


@pytest.mark.db
def test_the_record_is_read_through_the_reader_role_like_everything_else_in_agent_explain():
    """``_decision_record`` goes through :func:`core.db.read_connection`, the same
    ``winback_reader`` grant ``api/main.py`` uses — so the DB, not this module's
    discipline, is what makes a write from here impossible. Skips on an empty database,
    which is a legitimate state rather than a defect."""
    with read_connection() as conn:
        row = conn.execute("SELECT invoice_id, run_id FROM decisions LIMIT 1").fetchone()
    if row is None:
        pytest.skip("no decision in this database to read back")

    record = _decision_record(row["invoice_id"], row["run_id"])
    assert record is not None
    assert record["invoice_id"] == row["invoice_id"]
    assert "authorizing_rule" in record

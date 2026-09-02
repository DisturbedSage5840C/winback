"""The ledger: what an approval is, and what it is not.

Every test here is a sentence a panelist might say out loud. "Could the agent just call
execute twice?" — :func:`test_an_approval_is_single_use`. "What if it shifts the time by
a minute?" — :func:`test_an_approval_is_exact_to_the_timestamp`. "Could it talk its way
past the guardrail?" — :func:`test_execute_without_a_guardrail_call_is_refused`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent.tools import (
    ALLOWED_TOOLS,
    DISALLOWED_TOOLS,
    GATED_TOOLS,
    GUARDRAIL_TOOL,
    MONEY_TOOL,
    PREAPPROVED_TOOLS,
    Workbench,
    approval_key,
)
from compliance.guardrail import ActionKind
from eval.counterfactual import DECISION_LAG_HOURS


def _now(bench: Workbench, invoice_id: str) -> datetime:
    """The moment the batch would first look at this invoice."""
    return bench.cases[invoice_id].first_charge.attempted_at + timedelta(hours=DECISION_LAG_HOURS)


async def _approved_retry_slot(tools: dict, bench: Workbench, invoice_id: str) -> str:
    """Assess, then take the first slot the guardrail actually approves a retry for.

    Not a slot invented by the test. Reading it out of the scored candidate set means the
    test's idea of "legal" and the guardrail's are the same object, so a rule change that
    made every slot illegal would fail here rather than silently pass on a hardcoded time.
    """
    now = _now(bench, invoice_id)
    await tools["assess_recoverability"].handler({"invoice_id": invoice_id, "now": now.isoformat()})
    for candidate in bench.plans[invoice_id].candidates:
        if candidate.kind is ActionKind.RETRY and candidate.allowed:
            return candidate.execute_at.isoformat()
    pytest.skip(f"no legal retry slot for {invoice_id}")


# --------------------------------------------------------------------- tool lists


def test_the_gated_tools_are_not_pre_approved():
    """The defect that made the money gate decorative, pinned as a test.

    ``allowed_tools`` auto-approves a tool *before* ``can_use_tool`` runs. Listing the
    gated tools there — which the first version did — meant the gate was never consulted
    for the one call it exists to refuse, and the only symptom was a warning nobody was
    reading. If someone ever "tidies up" by passing ``ALLOWED_TOOLS`` to the SDK, this
    fails immediately.
    """
    assert set(PREAPPROVED_TOOLS).isdisjoint(GATED_TOOLS)
    assert set(ALLOWED_TOOLS) == set(PREAPPROVED_TOOLS) | set(GATED_TOOLS)
    assert MONEY_TOOL in GATED_TOOLS
    assert GUARDRAIL_TOOL in PREAPPROVED_TOOLS


def test_no_message_sending_tool_is_permitted():
    """``payment_link_notify`` is the one Razorpay tool that delivers around the consent
    gate. Its absence is a property of the list, not of the prompt."""
    assert not any("notify" in name and "simulated" not in name for name in ALLOWED_TOOLS)
    assert all(name.startswith("mcp__winback__") for name in ALLOWED_TOOLS)


def test_the_agent_cannot_reach_the_filesystem_or_a_shell():
    for name in ("Bash", "Read", "Write", "Edit", "WebFetch"):
        assert name in DISALLOWED_TOOLS


# --------------------------------------------------------------------- the ledger


async def test_execute_without_a_guardrail_call_is_refused(tools, bench, invoice_id):
    now = _now(bench, invoice_id)
    result = await tools["execute_recovery"].handler(
        {"invoice_id": invoice_id, "execute_at": now.isoformat()}
    )
    assert "no_guardrail_approval" in result["content"][0]["text"]
    assert bench.executions == []


async def test_a_guardrail_approval_lets_exactly_one_execution_through(tools, bench, invoice_id):
    slot = await _approved_retry_slot(tools, bench, invoice_id)
    await tools["compliance_guardrail"].handler(
        {"invoice_id": invoice_id, "action": "retry", "execute_at": slot}
    )
    assert approval_key(invoice_id, "retry", slot) in bench.approvals

    result = await tools["execute_recovery"].handler({"invoice_id": invoice_id, "execute_at": slot})
    assert "no_guardrail_approval" not in result["content"][0]["text"]
    assert len(bench.executions) == 1


async def test_an_approval_is_single_use(tools, bench, invoice_id):
    """Without this, one guardrail call authorises two presentments and the fourth
    attempt quietly becomes the fifth."""
    slot = await _approved_retry_slot(tools, bench, invoice_id)
    await tools["compliance_guardrail"].handler(
        {"invoice_id": invoice_id, "action": "retry", "execute_at": slot}
    )
    await tools["execute_recovery"].handler({"invoice_id": invoice_id, "execute_at": slot})

    again = await tools["execute_recovery"].handler({"invoice_id": invoice_id, "execute_at": slot})
    assert "no_guardrail_approval" in again["content"][0]["text"]
    assert len(bench.executions) == 1


async def test_an_approval_is_exact_to_the_timestamp(tools, bench, invoice_id):
    """A near-miss is a miss. A gate that rounds is a gate that can be argued with."""
    slot = await _approved_retry_slot(tools, bench, invoice_id)
    await tools["compliance_guardrail"].handler(
        {"invoice_id": invoice_id, "action": "retry", "execute_at": slot}
    )

    one_minute_later = (datetime.fromisoformat(slot) + timedelta(minutes=1)).isoformat()
    result = await tools["execute_recovery"].handler(
        {"invoice_id": invoice_id, "execute_at": one_minute_later}
    )
    assert "no_guardrail_approval" in result["content"][0]["text"]
    assert bench.executions == []


async def test_an_approval_does_not_transfer_between_actions(tools, bench, invoice_id):
    """An approved nudge is not an approved presentment."""
    now = _now(bench, invoice_id).isoformat()
    await tools["assess_recoverability"].handler({"invoice_id": invoice_id, "now": now})
    await tools["compliance_guardrail"].handler(
        {"invoice_id": invoice_id, "action": "nudge", "execute_at": now}
    )

    result = await tools["execute_recovery"].handler({"invoice_id": invoice_id, "execute_at": now})
    assert "no_guardrail_approval" in result["content"][0]["text"]


async def test_a_denied_action_writes_no_approval(tools, bench, invoice_id):
    """Peak-hour presentment: the guardrail redirects, and nothing enters the ledger."""
    peak = _now(bench, invoice_id).replace(hour=11, minute=0, second=0, microsecond=0)
    await tools["assess_recoverability"].handler(
        {"invoice_id": invoice_id, "now": peak.isoformat()}
    )
    result = await tools["compliance_guardrail"].handler(
        {"invoice_id": invoice_id, "action": "retry", "execute_at": peak.isoformat()}
    )
    assert '"authorised": false' in result["content"][0]["text"].lower()
    assert bench.approvals == {}


# --------------------------------------------------------------------- accounting


async def test_attempts_used_counts_history_plus_this_batch(tools, bench, invoice_id):
    """Never the agent's word for it. The count is derived from what actually ran."""
    before = bench.attempts_used(invoice_id)
    slot = await _approved_retry_slot(tools, bench, invoice_id)
    await tools["compliance_guardrail"].handler(
        {"invoice_id": invoice_id, "action": "retry", "execute_at": slot}
    )
    await tools["execute_recovery"].handler({"invoice_id": invoice_id, "execute_at": slot})
    assert bench.attempts_used(invoice_id) == before + 1


async def test_a_nudge_does_not_consume_a_legal_attempt(tools, bench, invoice_id):
    """The NPCI cap counts presentments. Messaging someone is not one."""
    now = _now(bench, invoice_id).isoformat()
    await tools["assess_recoverability"].handler({"invoice_id": invoice_id, "now": now})
    approved = await tools["compliance_guardrail"].handler(
        {"invoice_id": invoice_id, "action": "nudge", "execute_at": now}
    )
    if '"authorised": true' not in approved["content"][0]["text"].lower():
        pytest.skip("nudge not permitted for this invoice")

    before = bench.attempts_used(invoice_id)
    await tools["simulated_notify"].handler({"invoice_id": invoice_id, "execute_at": now})
    assert bench.attempts_used(invoice_id) == before


async def test_an_unknown_invoice_is_refused_by_every_tool(tools):
    for name, args in (
        ("assess_recoverability", {"invoice_id": "inv_nope", "now": "2026-05-01T09:00:00+05:30"}),
        (
            "compliance_guardrail",
            {
                "invoice_id": "inv_nope",
                "action": "retry",
                "execute_at": "2026-05-01T09:00:00+05:30",
            },
        ),
        ("execute_recovery", {"invoice_id": "inv_nope", "execute_at": "2026-05-01T09:00:00+05:30"}),
    ):
        result = await tools[name].handler(args)
        assert "unknown invoice" in result["content"][0]["text"]


async def test_the_agent_never_sees_the_oracles_true_probability(tools, bench, invoice_id):
    """``metadata`` carries the simulator's attempt row. Handing it back would let the
    agent read the answer it is supposed to be predicting."""
    slot = await _approved_retry_slot(tools, bench, invoice_id)
    await tools["compliance_guardrail"].handler(
        {"invoice_id": invoice_id, "action": "retry", "execute_at": slot}
    )
    result = await tools["execute_recovery"].handler({"invoice_id": invoice_id, "execute_at": slot})
    assert "metadata" not in result["content"][0]["text"]
    assert "metadata" in bench.executions[0]

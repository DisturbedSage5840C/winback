"""The permission callback: the wall, not the sign on the wall.

``can_use_tool`` runs before a tool executes, so a ``PermissionResultDeny`` means the call
does not happen at all. These tests drive the callback directly with the arguments the SDK
would hand it, which is the only way to observe the refusal — by the time a tool's own
code runs, the gate has already said yes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from agent.gate import DENIED_TOOL, MALFORMED, NO_APPROVAL, make_money_gate
from agent.tools import GUARDRAIL_TOOL, MONEY_TOOL, Workbench, approval_key, permitted_tools
from compliance.guardrail import ActionKind
from eval.counterfactual import DECISION_LAG_HOURS

NOTIFY_TOOL = "mcp__winback__simulated_notify"


@pytest.fixture
def gate(bench: Workbench):
    return make_money_gate(bench)


async def _ask(gate, tool_name: str, **args):
    """One permission question, shaped the way the SDK asks it."""
    return await gate(tool_name, dict(args), None)


def _legal_slot(bench: Workbench, invoice_id: str) -> str:
    now = bench.cases[invoice_id].first_charge.attempted_at + timedelta(hours=DECISION_LAG_HOURS)
    return now.isoformat()


# --------------------------------------------------------------- the allow-list


async def test_a_tool_that_is_not_on_the_list_is_refused(gate):
    """An allow-list, not a block-list: a tool that appears on the Razorpay MCP server
    tomorrow is denied today, which is the only safe direction for this to fail."""
    result = await _ask(gate, "mcp__razorpay__payment_link_notify", invoice_id="inv_0001_01")
    assert isinstance(result, PermissionResultDeny)
    assert DENIED_TOOL in result.message


async def test_an_ungated_tool_is_allowed_without_an_approval(gate):
    """Asking the guardrail is not itself a privileged act."""
    result = await _ask(gate, GUARDRAIL_TOOL, invoice_id="inv_0001_01", action="retry")
    assert isinstance(result, PermissionResultAllow)


async def test_a_razorpay_read_is_refused_on_the_simulated_lane(gate):
    """Every id in the simulated database is one the seeder invented, so a fetch there
    can only 404. The gate is where that is decided, not the prompt."""
    result = await _ask(gate, "mcp__razorpay__fetch_payment", payment_id="pay_sim_0007_01")
    assert isinstance(result, PermissionResultDeny)
    assert DENIED_TOOL in result.message


async def test_a_razorpay_read_is_allowed_on_the_live_lane_but_a_send_is_not(bench):
    """Widening the list for the live lane widens it by reads only. `payment_link_notify`
    is the tool that would message a real person without a consent check, and it stays
    denied on both lanes — the live lane's sends run through `execute_recovery`, which
    cannot fire without a guardrail approval already on record."""
    live_gate = make_money_gate(bench, None, permitted_tools("live"))

    allowed = await _ask(live_gate, "mcp__razorpay__fetch_payment", payment_id="pay_ABC")
    assert isinstance(allowed, PermissionResultAllow)

    refused = await _ask(live_gate, "mcp__razorpay__payment_link_notify", id="plink_ABC")
    assert isinstance(refused, PermissionResultDeny)
    assert DENIED_TOOL in refused.message


# --------------------------------------------------------------- the money gate


async def test_the_money_tool_is_refused_without_an_approval(gate, bench, invoice_id):
    result = await _ask(
        gate, MONEY_TOOL, invoice_id=invoice_id, execute_at=_legal_slot(bench, invoice_id)
    )
    assert isinstance(result, PermissionResultDeny)
    assert NO_APPROVAL in result.message


async def test_the_money_tool_is_allowed_with_an_approval_on_record(gate, bench, invoice_id):
    slot = _legal_slot(bench, invoice_id)
    bench.approvals[approval_key(invoice_id, str(ActionKind.RETRY), slot)] = object()

    result = await _ask(gate, MONEY_TOOL, invoice_id=invoice_id, execute_at=slot)
    assert isinstance(result, PermissionResultAllow)


async def test_the_gate_reads_the_approval_and_does_not_spend_it(gate, bench, invoice_id):
    """``_execute`` pops. If the gate popped too, every permitted call would arrive at a
    tool that then found nothing and refused itself."""
    slot = _legal_slot(bench, invoice_id)
    key = approval_key(invoice_id, str(ActionKind.RETRY), slot)
    bench.approvals[key] = object()

    await _ask(gate, MONEY_TOOL, invoice_id=invoice_id, execute_at=slot)
    assert key in bench.approvals


async def test_a_nudge_approval_does_not_open_the_money_tool(gate, bench, invoice_id):
    slot = _legal_slot(bench, invoice_id)
    bench.approvals[approval_key(invoice_id, str(ActionKind.NUDGE), slot)] = object()

    result = await _ask(gate, MONEY_TOOL, invoice_id=invoice_id, execute_at=slot)
    assert isinstance(result, PermissionResultDeny)


async def test_the_notify_tool_is_gated_too(gate, bench, invoice_id):
    """The channel is simulated; the consent gate in front of it is real."""
    result = await _ask(
        gate, NOTIFY_TOOL, invoice_id=invoice_id, execute_at=_legal_slot(bench, invoice_id)
    )
    assert isinstance(result, PermissionResultDeny)
    assert NO_APPROVAL in result.message


# --------------------------------------------------------------- malformed calls


@pytest.mark.parametrize(
    "args",
    [
        {"invoice_id": "inv_0001_01"},
        {"execute_at": "2026-05-01T09:00:00+05:30"},
        {"invoice_id": 7, "execute_at": "2026-05-01T09:00:00+05:30"},
        {"invoice_id": "inv_0001_01", "execute_at": "not-a-timestamp"},
    ],
)
async def test_a_malformed_call_is_refused_rather_than_guessed_at(gate, args):
    result = await _ask(gate, MONEY_TOOL, **args)
    assert isinstance(result, PermissionResultDeny)
    assert MALFORMED in result.message


# --------------------------------------------------------------- denial behaviour


async def test_a_denial_does_not_interrupt_the_run(gate, bench, invoice_id):
    """The 5th-attempt block is a *normal* event this system is built to produce.
    Interrupting on it would turn every compliance block into an outage."""
    result = await _ask(
        gate, MONEY_TOOL, invoice_id=invoice_id, execute_at=_legal_slot(bench, invoice_id)
    )
    assert result.interrupt is False


async def test_a_denial_tells_the_agent_what_to_do_instead(gate, bench, invoice_id):
    """A refusal the agent cannot act on just becomes a retry loop."""
    result = await _ask(
        gate, MONEY_TOOL, invoice_id=invoice_id, execute_at=_legal_slot(bench, invoice_id)
    )
    assert "compliance_guardrail" in result.message
    assert "suggested_slots" in result.message


async def test_a_denial_is_recorded_by_the_writer(bench, invoice_id):
    """``PostToolUse`` never fires for a call that did not run, so a refused presentment
    would leave no trace at all unless the gate writes it itself."""
    recorded: list[tuple] = []

    class _Writer:
        def record_denial(self, tool_name, input_data, reason):
            recorded.append((tool_name, input_data, reason))

    gate = make_money_gate(bench, _Writer())
    await gate(
        MONEY_TOOL, {"invoice_id": invoice_id, "execute_at": _legal_slot(bench, invoice_id)}, None
    )

    assert len(recorded) == 1
    assert recorded[0][0] == MONEY_TOOL
    assert NO_APPROVAL in recorded[0][2]


async def test_an_allowed_call_is_not_recorded_as_a_denial(bench, invoice_id):
    recorded: list = []

    class _Writer:
        def record_denial(self, *args):
            recorded.append(args)

    slot = _legal_slot(bench, invoice_id)
    bench.approvals[approval_key(invoice_id, str(ActionKind.RETRY), slot)] = object()
    gate = make_money_gate(bench, _Writer())
    await gate(MONEY_TOOL, {"invoice_id": invoice_id, "execute_at": slot}, None)

    assert recorded == []


async def test_two_batches_do_not_share_a_ledger(scorer, rates, invoice_id):
    """Closed over the workbench rather than a global, so a run cannot spend an approval
    that a different run was granted."""
    from agent.tools import workbench_from_dataset

    one = workbench_from_dataset(scorer=scorer, rates=rates, cohort="test")
    two = workbench_from_dataset(scorer=scorer, rates=rates, cohort="test")
    slot = _legal_slot(one, invoice_id)
    one.approvals[approval_key(invoice_id, str(ActionKind.RETRY), slot)] = object()

    result = await make_money_gate(two)(
        MONEY_TOOL, {"invoice_id": invoice_id, "execute_at": slot}, None
    )
    assert isinstance(result, PermissionResultDeny)


async def test_the_timestamp_is_matched_after_normalisation_not_before(gate, bench, invoice_id):
    """The ledger key uses ``datetime.isoformat()`` on both sides, so two spellings of
    the same instant match and two different instants never do."""
    slot = _legal_slot(bench, invoice_id)
    bench.approvals[approval_key(invoice_id, str(ActionKind.RETRY), slot)] = object()

    shifted = (datetime.fromisoformat(slot) + timedelta(seconds=1)).isoformat()
    assert isinstance(
        await _ask(gate, MONEY_TOOL, invoice_id=invoice_id, execute_at=shifted),
        PermissionResultDeny,
    )

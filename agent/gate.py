"""The money gate. The one place an agent's intention is turned down by structure.

``can_use_tool`` is the SDK's permission callback: it runs before a tool executes, and
returning :class:`PermissionResultDeny` means the call does not happen. That is a
different kind of guarantee from a tool that checks its own arguments, and this project
uses both — the same belt-and-braces reasoning as the append-only ``audit_log``, which
has a trigger *and* a revoked grant. If the gate is bypassed, ``_execute`` still refuses.
If ``_execute`` is edited wrongly, the gate still refuses. Two independent mechanisms have
to fail together before an unapproved presentment can happen.

**What the gate checks is not what the agent says.** It re-derives the approval key from
the tool call's own arguments and looks it up in the ledger. The only writer of that ledger
is ``compliance_guardrail``, which is deterministic Python. So the question the gate asks
is never "did the model claim this was compliant" — it is "did the rule engine, on this
exact invoice at this exact timestamp, return APPROVE". A model cannot produce that row
by being persuasive.

**The gate does not spend the approval.** It reads; ``_execute`` pops. If the gate consumed
it, every permitted call would arrive at a tool that then found nothing and refused itself.

**Deny is not a message to the agent, it is a wall.** ``interrupt=False`` is deliberate:
the agent is told why, in words it can act on, and is left free to do something legal
instead — usually to ask the guardrail for the next valid slot. Interrupting the whole run
on a denial would turn every ordinary compliance block into an outage, and the 5th-attempt
block is a *normal* event this system is built to produce, not an error.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from agent.tools import ALLOWED_TOOLS, GATED_TOOLS, MONEY_TOOL, Workbench, approval_key

#: Imported rather than restated. When this file kept its own copy of the gated set, the
#: SDK's ``allowed_tools`` could pre-approve a tool this gate still believed it was
#: guarding — two lists that agreed on paper and not in the running process.
_GATED = frozenset(GATED_TOOLS)

#: What the money-moving tools' arguments are called. Named here so a rename in
#: ``agent.tools`` that this file does not follow fails loudly at the gate instead of
#: quietly approving everything.
INVOICE_ARG = "invoice_id"
SLOT_ARG = "execute_at"

DENIED_TOOL = "tool_not_permitted"
NO_APPROVAL = "no_guardrail_approval"
MALFORMED = "malformed_gated_call"


def _kind_for(tool_name: str) -> str:
    return "retry" if tool_name == MONEY_TOOL else "nudge"


def make_money_gate(bench: Workbench, writer: Any | None = None):
    """Build the ``can_use_tool`` callback for one batch.

    Closed over the workbench rather than reading a global, so two batches running in the
    same process cannot see each other's approvals.

    ``writer`` is an :class:`agent.hooks.AuditWriter`. It is passed here — rather than
    left to the ``PostToolUse`` hook — because that hook does not fire for a call this
    gate refuses, and a refused presentment is exactly the row the compliance argument
    rests on. Optional so the gate can be unit-tested without a database.
    """

    def _deny(tool_name: str, input_data: dict[str, Any], message: str) -> PermissionResultDeny:
        if writer is not None:
            writer.record_denial(tool_name, input_data, message)
        return PermissionResultDeny(message=message, interrupt=False)

    async def money_gate(
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ):
        # Anything not on the list is refused outright, including every Razorpay tool
        # that could deliver a message. An allow-list rather than a block-list: a new
        # tool appearing on the Razorpay MCP server is denied by default, which is the
        # only safe direction for that to fail.
        if tool_name not in ALLOWED_TOOLS:
            return _deny(
                tool_name,
                input_data,
                f"{DENIED_TOOL}: {tool_name} is not permitted in this batch. "
                f"Permitted tools are {', '.join(ALLOWED_TOOLS)}.",
            )

        if tool_name not in _GATED:
            return PermissionResultAllow(updated_input=input_data)

        invoice_id = input_data.get(INVOICE_ARG)
        raw_slot = input_data.get(SLOT_ARG)
        if not isinstance(invoice_id, str) or not isinstance(raw_slot, str):
            return _deny(
                tool_name,
                input_data,
                f"{MALFORMED}: {tool_name} needs both {INVOICE_ARG} and {SLOT_ARG} "
                "as strings. Nothing was executed.",
            )

        try:
            execute_at = datetime.fromisoformat(raw_slot)
        except ValueError:
            return _deny(
                tool_name,
                input_data,
                f"{MALFORMED}: {raw_slot!r} is not an ISO-8601 timestamp.",
            )

        key = approval_key(invoice_id, _kind_for(tool_name), execute_at.isoformat())
        if key not in bench.approvals:
            # The message names the next legal slot when the guardrail has one, because
            # a denial the agent cannot act on just becomes a retry loop.
            return _deny(
                tool_name,
                input_data,
                f"{NO_APPROVAL}: no unspent compliance_guardrail APPROVE on record for "
                f"invoice {invoice_id} at {execute_at.isoformat()}. Call "
                "compliance_guardrail with these exact arguments first; if it returns "
                "REDIRECT_TO_WINDOW, use one of its suggested_slots. Approvals are "
                "single-use and are matched on the exact timestamp.",
            )

        # Read, never popped — `agent.tools._execute` spends it.
        return PermissionResultAllow(updated_input=input_data)

    return money_gate

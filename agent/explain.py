"""A plain-English narration of a decision already written to the audit trail.

This is deliberately the least powerful thing in ``agent/``. It reads one decision row
(and its outcome, if the batch has one yet), hands the JSON to a model, and returns the
paragraph that comes back. It runs **after the fact**, over rows that already exist —
never inside the batch loop, never before a decision is made — and it is given **zero
tools**: no MCP server is mounted, ``allowed_tools`` is empty. It cannot call
``compliance_guardrail`` or ``execute_recovery`` because neither is reachable from this
process, not because a policy says not to. Worst case this produces a bad sentence about
a decision the batch already made; it cannot make one.

It reads through :func:`core.db.read_connection`, the same ``winback_reader`` role
``api/main.py`` uses, so the same grant-level guarantee applies here: this module cannot
write to ``decisions``, ``audit_log``, or any other table even if the prompt above it
were compromised.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

from core.config import Settings, get_settings
from core.db import read_connection

SYSTEM_PROMPT = """\
You explain automated payment-recovery decisions to a non-technical reader, in exactly \
one plain-English paragraph. You are given the full decision record as JSON: which rule \
authorised the action, the model's calibrated probability, the expected-value \
computation, and the outcome if one is known yet. Explain what was decided and why, in \
plain language, from that record alone. You have no tools and cannot look anything up \
or take any action — you narrate the record you are given, nothing else.\
"""


class DecisionNotFound(LookupError):
    """No decision matches the requested invoice (and run, if one was given)."""


def _decision_record(invoice_id: str, run_id: str | None) -> dict[str, Any] | None:
    with read_connection() as conn:
        row = conn.execute(
            """
            SELECT d.decision_id, d.run_id, d.arm, d.invoice_id, d.model_version,
                   d.calibrated_prob, d.expected_value_paise, d.proposed_action,
                   d.guardrail_verdict, d.authorizing_rule, d.final_action,
                   d.scheduled_for, d.human_approval_required, d.decided_by,
                   d.decided_at, a.outcome, a.stop_reason, a.compliance_violation,
                   a.recovered_amount_paise
              FROM decisions d
              LEFT JOIN audit_log a USING (decision_id)
             WHERE d.invoice_id = %(inv)s
               AND (%(run)s::text IS NULL OR d.run_id = %(run)s)
             ORDER BY d.decided_at DESC
             LIMIT 1
            """,
            {"inv": invoice_id, "run": run_id},
        ).fetchone()
    return dict(row) if row is not None else None


def explainer_options(settings: Settings) -> ClaudeAgentOptions:
    """No MCP server, no allowed tools, no disallowed-tools list to maintain — there is
    nothing here for a permission check to gate. That absence is the whole safety
    argument, so it is asserted directly in ``agent/tests/test_explain.py`` rather than
    left to be inferred from the fact that no tool call ever shows up in practice."""
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={},
        allowed_tools=[],
        disallowed_tools=[],
        permission_mode="default",
        max_turns=1,
        model=settings.explainer_model,
        setting_sources=[],
    )


async def explain_decision(invoice_id: str, run_id: str | None = None) -> str:
    """One paragraph explaining the most recent decision written for ``invoice_id``.

    Raises :class:`DecisionNotFound` if no decision has been written yet — narrating a
    decision that does not exist would be inventing one, which is exactly the kind of
    unauditable step the rest of ``agent/`` is built to avoid.
    """
    record = _decision_record(invoice_id, run_id)
    if record is None:
        raise DecisionNotFound(invoice_id)

    options = explainer_options(get_settings())
    prompt = (
        "Explain this automated payment-recovery decision, from the record alone:\n\n"
        f"{json.dumps(record, default=str, indent=2)}"
    )

    paragraph = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    paragraph = block.text.strip()
    return paragraph

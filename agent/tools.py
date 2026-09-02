"""The four tools the agent is given, and the ledger that makes the gate enforceable.

The agent does not compute anything. It reads an invoice, calls
``assess_recoverability`` to find out what the calibrated model and the cost policy think,
calls ``compliance_guardrail`` to find out what the law permits, and then calls
``execute_recovery``. Every number it repeats came from deterministic Python; every rule
it cites was evaluated by ``compliance/``. If the model were removed the agent could not
guess a probability, and if the guardrail were removed it could not act at all.

**The ledger is the load-bearing idea.** ``compliance_guardrail`` is pure deterministic
code — the same ``compliance.guardrail.evaluate`` the evaluation harness ran — and when it
approves an action it records that approval under an exact key:
``(invoice_id, action, execute_at)``. ``agent.gate`` refuses ``execute_recovery`` unless
that key is present. The agent therefore cannot talk its way into a payment: an approval
is not a sentence it produces, it is a row only the rule engine can write.

Three properties follow, and each one is a question a panelist will ask.

**An approval is single-use.** It is popped when it is spent, so one guardrail call
cannot authorise two presentments. Without this, an agent that got approval for attempt 3
could execute it twice and consume a fifth attempt the cap forbids.

**An approval is exact.** The key includes the timestamp, so approval for a 13:40 slot is
not approval for a 12:00 one. A near-miss is a miss; the gate does no rounding, because a
gate that rounds is a gate that can be argued with.

**``payment_link_notify`` is not in the tool list, in any mode.** It is the one Razorpay
tool that could deliver a message around the consent gate. Its absence is enforced in
``ALLOWED_TOOLS`` below rather than by asking the agent not to use it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from agent.adapters.base import (
    Adapter,
    AdapterError,
    ExecutionRequest,
    ExecutionResult,
)
from agent.adapters.simulated import SimulatedAdapter
from compliance.guardrail import ActionKind, ActionRequest, GuardrailDecision, evaluate
from ml.features import BankMethodRates
from ml.policy import DEFAULT_POLICY, InvoiceState, Plan, PolicyParams, decide
from ml.scorer import Scorer

#: The SDK mounts these as ``mcp__winback__<name>``.
SERVER_NAME = "winback"

#: The tool that moves money. Named once, here, and imported by the gate — so the gate
#: and the tool list can never drift into disagreeing about which one is dangerous.
MONEY_TOOL = f"mcp__{SERVER_NAME}__execute_recovery"
GUARDRAIL_TOOL = f"mcp__{SERVER_NAME}__compliance_guardrail"

#: The tools that act on the customer relationship. One presents a mandate, the other
#: messages a person. Both require a guardrail approval on record, and both are therefore
#: kept OUT of the SDK's ``allowed_tools`` — see :data:`PREAPPROVED_TOOLS`.
GATED_TOOLS = (f"mcp__{SERVER_NAME}__simulated_notify", MONEY_TOOL)

#: Handed to ``ClaudeAgentOptions.allowed_tools``, and deliberately *not* the full set.
#:
#: The SDK auto-approves anything listed there **before** ``can_use_tool`` is consulted —
#: it says so at construction time with a ``CanUseToolShadowedWarning``, which is how this
#: was caught. Listing all four tools made the money gate decorative: it was never invoked
#: for the one call it exists to refuse. Only the two harmless tools are pre-approved;
#: the two gated ones are omitted so they fall through to the permission callback, which
#: is the enforcement point.
PREAPPROVED_TOOLS = (f"mcp__{SERVER_NAME}__assess_recoverability", GUARDRAIL_TOOL)

#: Every tool the agent may call at all — what the gate checks against. Razorpay's MCP
#: tools are mounted for reads and are deliberately absent: the live lane runs through
#: ``execute_recovery`` so that every real API call is preceded by a guardrail approval.
#: Note what is *not* present — ``mcp__razorpay__payment_link_notify`` and every other
#: send. Anything off this list is denied, so a new tool appearing on the Razorpay server
#: is refused by default rather than silently permitted.
ALLOWED_TOOLS = (*PREAPPROVED_TOOLS, *GATED_TOOLS)

#: Built-in tools the batch has no business touching. A recovery agent that could read
#: the filesystem or run a shell could reach the model artifacts, the ``.env`` file and
#: the audit database directly — around every control in this package.
DISALLOWED_TOOLS = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
)


def approval_key(invoice_id: str, action: str, execute_at: str) -> str:
    """The exact identity of one authorised action."""
    return f"{invoice_id}|{action}|{execute_at}"


@dataclass
class Workbench:
    """Everything the tools need, and the state the gate reads.

    One per batch run. Holds no conversation state: if the agent's context is lost, the
    ledger and the decision records survive, because they are the parts that have to be
    auditable.
    """

    cases: dict[str, Any]
    scorer: Scorer
    rates: BankMethodRates
    adapter: Adapter
    params: PolicyParams = DEFAULT_POLICY

    #: Approvals the guardrail has granted and the gate has not yet spent.
    approvals: dict[str, GuardrailDecision] = field(default_factory=dict, repr=False)
    #: Plans by invoice, so ``execute_recovery`` can write ``decisions.candidate_set``
    #: without the agent having to carry a large JSON blob through its context.
    plans: dict[str, Plan] = field(default_factory=dict, repr=False)
    #: Everything that happened, in order. The orchestrator persists this.
    executions: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def state_for(self, invoice_id: str, *, now: datetime, attempts_used: int) -> InvoiceState:
        case = self.cases[invoice_id]
        return InvoiceState(
            subscription=case.subscription,
            customer=case.customer,
            invoice=case.invoice,
            attempts_used=attempts_used,
            root_cause=case.root_cause,
            prior=_prior_for(case, attempts_used),
            now=now,
            nudged_at=self.nudged_at(invoice_id),
        )

    def nudged_at(self, invoice_id: str) -> datetime | None:
        """When this batch last messaged the customer about this invoice."""
        for row in reversed(self.executions):
            if row["invoice_id"] == invoice_id and row["action"] == str(ActionKind.NUDGE):
                return datetime.fromisoformat(row["execute_at"])
        return None

    def attempts_used(self, invoice_id: str) -> int:
        """History plus what this batch has already spent. Never the agent's word for it."""
        spent = sum(
            1
            for row in self.executions
            if row["invoice_id"] == invoice_id and row["action"] == str(ActionKind.RETRY)
        )
        return 1 + spent


def _prior_for(case: Any, attempts_used: int):
    from ml.features import Candidate, PriorState

    return PriorState.before(
        Candidate(
            attempt_number=attempts_used + 1,
            action="retry",
            execute_at=case.first_charge.attempted_at,
            amount_paise=case.invoice.amount_paise,
        ),
        invoice_id=case.invoice.invoice_id,
        history=case.base_history,
    )


def _text(payload: dict[str, Any]) -> dict[str, Any]:
    """An MCP tool result. JSON in a text block, which is what the SDK transports."""
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str, indent=2)}]}


def _action_request(state: InvoiceState, kind: ActionKind, execute_at: datetime) -> ActionRequest:
    return ActionRequest(
        kind=kind,
        execute_at=execute_at,
        amount_paise=state.invoice.amount_paise,
        mcc_category=state.subscription.mcc_category,
        attempts_used=state.attempts_used,
        root_cause=state.root_cause,
        charge_at=state.invoice.charge_at,
        notice_sent_at=state.invoice.notice_sent_at,
        consent_status=state.customer.consent_status,
        consent_updated_at=state.customer.consent_updated_at,
        last_transaction_at=state.last_transaction_at,
    )


def build_tools(bench: Workbench) -> list[SdkMcpTool[Any]]:
    """The four tools, closed over one batch's workbench."""

    @tool(
        "assess_recoverability",
        "Score one at-risk invoice: calibrated P(success) for every legal action and slot, "
        "the expected rupee value of each, and the action the cost policy recommends. "
        "Deterministic — the model and the lookahead, not a judgement.",
        {
            "invoice_id": Annotated[str, "The at-risk invoice to assess"],
            "now": Annotated[str, "ISO-8601 timestamp of the decision moment"],
        },
    )
    async def assess_recoverability(args: dict[str, Any]) -> dict[str, Any]:
        invoice_id = args["invoice_id"]
        if invoice_id not in bench.cases:
            return _text({"error": f"unknown invoice {invoice_id}"})

        state = bench.state_for(
            invoice_id,
            now=datetime.fromisoformat(args["now"]),
            attempts_used=bench.attempts_used(invoice_id),
        )
        plan = decide(state, scorer=bench.scorer, rates=bench.rates, params=bench.params)
        bench.plans[invoice_id] = plan

        return _text(
            {
                "invoice_id": invoice_id,
                "amount_paise": state.invoice.amount_paise,
                "attempts_used": state.attempts_used,
                "attempts_remaining": max(0, 4 - state.attempts_used),
                "root_cause": str(state.root_cause),
                "recommended": plan.chosen.to_dict(),
                "candidates": [c.to_dict() for c in plan.candidates],
                "note": (
                    "Recommendation only. It is not an authorisation: call "
                    "compliance_guardrail before execute_recovery."
                ),
            }
        )

    @tool(
        "compliance_guardrail",
        "Evaluate one proposed action against NPCI OC-215-A (1 attempt + 3 retries, "
        "non-peak only), the RBI AFA thresholds, consent/DND, pre-debit notice, and "
        "root-cause retryability. Returns APPROVE, REDIRECT_TO_WINDOW, ESCALATE_HUMAN or "
        "DENY. An APPROVE here is what execute_recovery requires; nothing else authorises "
        "an action.",
        {
            "invoice_id": Annotated[str, "The invoice the action concerns"],
            "action": Annotated[str, "One of: retry, nudge, escalate, write_off"],
            "execute_at": Annotated[str, "ISO-8601 timestamp the action would run at"],
        },
    )
    async def compliance_guardrail(args: dict[str, Any]) -> dict[str, Any]:
        invoice_id = args["invoice_id"]
        if invoice_id not in bench.cases:
            return _text({"error": f"unknown invoice {invoice_id}"})
        try:
            kind = ActionKind(args["action"])
        except ValueError:
            return _text({"error": f"unknown action {args['action']!r}"})

        execute_at = datetime.fromisoformat(args["execute_at"])
        state = bench.state_for(
            invoice_id, now=execute_at, attempts_used=bench.attempts_used(invoice_id)
        )
        decision = evaluate(_action_request(state, kind, execute_at), now=execute_at)

        # The only place an approval is ever written. Recorded under the exact slot it
        # was granted for, so approval for one moment cannot be spent at another.
        if decision.allowed:
            bench.approvals[approval_key(invoice_id, str(kind), execute_at.isoformat())] = decision

        return _text(
            {
                "invoice_id": invoice_id,
                "action": str(kind),
                "execute_at": execute_at.isoformat(),
                **decision.to_dict(),
                "authorised": decision.allowed,
                "suggested_slots": [s.isoformat() for s in decision.suggested_slots],
            }
        )

    @tool(
        "simulated_notify",
        "Record that the customer would be told their payment failed. No message is sent "
        "to any real phone or inbox — TRAI DLT registration makes real delivery "
        "non-compliant here, so the channel is simulated while the consent gate in front "
        "of it is real. Requires an approved nudge from compliance_guardrail.",
        {
            "invoice_id": Annotated[str, "The invoice the customer is told about"],
            "execute_at": Annotated[str, "ISO-8601 timestamp the message would be sent"],
        },
    )
    async def simulated_notify(args: dict[str, Any]) -> dict[str, Any]:
        return _text(_execute(bench, args["invoice_id"], ActionKind.NUDGE, args["execute_at"]))

    @tool(
        "execute_recovery",
        "Present the mandate for one invoice. This is the action that moves money. It is "
        "refused unless compliance_guardrail has already approved this exact invoice, "
        "action and timestamp.",
        {
            "invoice_id": Annotated[str, "The invoice to present"],
            "execute_at": Annotated[str, "ISO-8601 timestamp, must match the approval exactly"],
        },
    )
    async def execute_recovery(args: dict[str, Any]) -> dict[str, Any]:
        return _text(_execute(bench, args["invoice_id"], ActionKind.RETRY, args["execute_at"]))

    return [assess_recoverability, compliance_guardrail, simulated_notify, execute_recovery]


def _execute(
    bench: Workbench, invoice_id: str, kind: ActionKind, execute_at_raw: str
) -> dict[str, Any]:
    """Spend an approval and run the adapter.

    The approval is popped rather than read. A second call with the same arguments finds
    nothing and is refused, which is what stops a retried tool call from consuming two
    legal attempts against a cap that only permits four.
    """
    if invoice_id not in bench.cases:
        return {"error": f"unknown invoice {invoice_id}"}

    execute_at = datetime.fromisoformat(execute_at_raw)
    key = approval_key(invoice_id, str(kind), execute_at.isoformat())
    decision = bench.approvals.pop(key, None)
    if decision is None:
        return {
            "error": "no_guardrail_approval",
            "detail": (
                f"no unspent approval for {key}. Call compliance_guardrail for this exact "
                "invoice, action and timestamp first. An approval is single-use."
            ),
        }

    case = bench.cases[invoice_id]
    request = ExecutionRequest(
        kind=kind,
        decision=decision,
        subscription_id=case.subscription.subscription_id,
        invoice_id=invoice_id,
        customer_hash=case.customer.customer_hash,
        amount_paise=case.invoice.amount_paise,
        execute_at=execute_at,
        attempt_number=bench.attempts_used(invoice_id) + 1,
    )

    try:
        result: ExecutionResult = (
            bench.adapter.present(request)
            if kind is ActionKind.RETRY
            else bench.adapter.nudge(request)
        )
    except AdapterError as exc:
        # An executor failure is not a declined payment, and must never be recorded as
        # one. Nothing is appended to `executions`, so no attempt is counted against
        # the cap for a call that never reached the rail.
        return {"error": "adapter_error", "detail": str(exc)}

    row = {
        "invoice_id": invoice_id,
        "subscription_id": case.subscription.subscription_id,
        "customer_hash": case.customer.customer_hash,
        "action": str(kind),
        "execute_at": execute_at.isoformat(),
        "attempt_number": request.attempt_number,
        "authorizing_rule": decision.authorizing_rule,
        "amount_paise": case.invoice.amount_paise,
        **result.to_dict(),
    }
    bench.executions.append(row)

    # The oracle's attempt row is machinery for the database, not something the agent
    # should read back — it carries the true probability.
    return {k: v for k, v in row.items() if k != "metadata"}


def winback_server(bench: Workbench) -> McpSdkServerConfig:
    """The in-process MCP server, mounted alongside Razorpay's."""
    return create_sdk_mcp_server(SERVER_NAME, "1.0.0", tools=build_tools(bench))


def workbench_from_dataset(
    *,
    scorer: Scorer,
    rates: BankMethodRates,
    adapter: Adapter | None = None,
    cohort: str = "test",
    params: PolicyParams = DEFAULT_POLICY,
) -> Workbench:
    """A workbench over the frozen test cohort, executing against the oracle by default."""
    simulated = SimulatedAdapter.from_dataset(cohort=cohort)
    return Workbench(
        cases=dict(simulated.cases),
        scorer=scorer,
        rates=rates,
        adapter=adapter or simulated,
        params=params,
    )

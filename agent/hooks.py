"""The audit trail, and the two paths that write to it.

``audit_log`` is the compliance artifact. It is append-only in three independent ways —
a ``BEFORE UPDATE OR DELETE`` trigger, a ``BEFORE TRUNCATE`` trigger, and revoked grants
— so what this module writes cannot afterwards be tidied up, by this code or by anyone at
a psql prompt. That is the property that makes it worth anything.

**Two paths, because a denial is an audit event.** The SDK's ``PostToolUse`` hook fires
after a tool runs, which covers everything the agent was allowed to do. It does not fire
for a call the permission gate refused — and a refused presentment is the single most
important row in this table, because it is the evidence that the cap was enforced against
an agent that wanted to act. So :class:`AuditWriter` is called from both places: by the
hook on success, and directly by ``agent.gate`` on denial. Putting the writer here rather
than in the gate keeps every INSERT into ``audit_log`` in one file, which is the file a
panelist should be able to read end to end.

**Redaction happens at write time.** ``observed_data`` carries ``customer_hash`` —
``sha256(customer_id)[:12]``, computed by the generator — and never a customer id, an
email or a phone number. Redacting at render time would mean the raw value was in the
table and the protection was a property of the dashboard; doing it here means the value
was never written.

**A decision row precedes every action row.** ``decisions`` records what was proposed,
what the guardrail said, and the full scored candidate set — including the candidates
that lost. ``audit_log.decision_id`` points back at it. An audit trail that recorded only
what happened could not distinguish a policy that chose well from one that had no other
option, and the drill-down drawer would have nothing to show.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher

from agent.tools import GUARDRAIL_TOOL, MONEY_TOOL, Workbench
from compliance.guardrail import ActionKind
from core.db import agent_connection

AGENT_ID = "winback-orchestrator"
AGENT_VERSION = "1.0.0"
MODEL_VERSION = "v1"

NOTIFY_TOOL = "mcp__winback__simulated_notify"
ASSESS_TOOL = "mcp__winback__assess_recoverability"

#: The tools whose calls produce an audit row. Assessment does not: scoring an invoice
#: observes nothing about the customer and changes nothing, and logging it would bury
#: the rows that matter under rows that do not.
AUDITED = frozenset({GUARDRAIL_TOOL, MONEY_TOOL, NOTIFY_TOOL})

#: Actions that conclude an invoice without touching the customer, and therefore without
#: calling a tool that ``PostToolUse`` could fire on. They still have to reach
#: ``audit_log`` — see :meth:`AuditWriter.record_conclusion`.
TERMINAL_ACTIONS = frozenset({"write_off", "escalate"})


def _jsonb(value: Any) -> str:
    """Serialise for a PostgreSQL ``jsonb`` column, refusing anything it would reject.

    ``allow_nan=False`` is the whole point. Python's default is to emit the bare tokens
    ``NaN``, ``Infinity`` and ``-Infinity``, which are not RFC 8259 and which ``jsonb``
    rejects with ``invalid input syntax for type json``. That failure surfaced as a
    swallowed exception inside the ``PostToolUse`` hook and a missing ``decisions`` row,
    which is the worst possible way for an audit trail to break: quietly. Raising here
    instead means a non-finite number that reaches this boundary is reported against the
    line that produced it rather than discovered later as a gap in the table.
    """
    return json.dumps(value, default=str, allow_nan=False)


def _payload(response: Any) -> dict[str, Any]:
    """Unwrap the JSON an in-process tool returned in its text block.

    ``tool_response`` arrives from the SDK as a **bare list** of content blocks, not as
    the ``{"content": [...]}`` envelope the tool itself returned. Both shapes are handled
    because the tool's own return value is what the unit tests feed in, and a helper that
    understood only one of them is exactly how this went wrong the first time: the list
    form fell through to ``{"unparsed": ...}``, every payload arrived without an
    ``invoice_id``, and the writer discarded all of it in silence.
    """
    blocks = response.get("content") if isinstance(response, dict) else response
    if isinstance(blocks, list) and blocks:
        first = blocks[0]
        text = first.get("text") if isinstance(first, dict) else None
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"unparsed": text}
    if isinstance(response, dict):
        return response
    return {"unparsed": str(response)}


def _binding_refusal(plan: Any) -> str | None:
    """Which rule closed the door, when an invoice was concluded without acting.

    The policy scores every legal ``(action x slot)`` pair and keeps the ones the
    guardrail refused, each carrying its own ``stop_reason``. When the winner is a
    write-off, the interesting fact is never the write-off — that always approves,
    because moving no money breaks no rule — it is *why nothing else was available*.
    That reason is computed on every batch and, until this existed, was written only
    into ``decisions.candidate_set``, where it can be read but not counted.

    Presentments first. A ``bd_hard_not_retryable`` on the retry is the reason the
    invoice cannot be recovered; a ``dnd_registered`` on the nudge is the reason the
    customer cannot be told about it. Both are true and only the first is the answer to
    "why was no attempt made", so the retry candidates are searched before the rest.

    ``None`` when nothing was refused: the guardrail permitted a presentment and the
    policy declined it on value. That is an economic stop, not a compliance one, and the
    two must not arrive in the table wearing the same label.
    """
    if plan is None:
        return None
    candidates = getattr(plan, "candidates", ())
    refused = [c for c in candidates if not c.allowed and c.decision.stop_reason]
    for candidate in refused:
        if candidate.kind is ActionKind.RETRY:
            return candidate.decision.stop_reason
    return refused[0].decision.stop_reason if refused else None


@dataclass
class AuditWriter:
    """Appends to ``decisions`` and ``audit_log``. Never updates either."""

    bench: Workbench
    run_id: str
    arm: str = "D"
    #: Decision rows written this batch, by ``(invoice_id, action, execute_at)``, so an
    #: action row can reference the decision that authorised it.
    _decisions: dict[str, str] = field(default_factory=dict, repr=False)
    rows_written: int = field(default=0, init=False)
    #: Invoices that reached ``audit_log`` at least once. The batch checks this after each
    #: invoice and closes any gap itself — see :meth:`record_silence`. Every conclusion the
    #: agent reaches has to leave a row; an invoice the trail says nothing about is
    #: indistinguishable from one that was never worked.
    covered: set[str] = field(default_factory=set, init=False, repr=False)
    #: Writes that raised. Reported by the batch, never zero-by-assumption. An audit
    #: trail whose gaps are invisible is not an audit trail, and the SDK's hook runner
    #: swallows exceptions raised inside a hook — so unless the count is carried out to
    #: the report, a failed INSERT looks exactly like a tool that was never called.
    write_failures: list[str] = field(default_factory=list, init=False)

    # ------------------------------------------------------------------ decisions

    def record_decision(self, payload: dict[str, Any]) -> str | None:
        """One guardrail evaluation, with the candidate set that produced it."""
        invoice_id = payload.get("invoice_id")
        if not invoice_id or invoice_id not in self.bench.cases:
            return None

        case = self.bench.cases[invoice_id]
        action = payload.get("action", "")
        execute_at = payload.get("execute_at")
        decision_id = f"dec_{self.run_id}_{self.arm}_{invoice_id}_{len(self._decisions):04d}"

        plan = self.bench.plans.get(invoice_id)
        chosen = plan.chosen if plan else None

        with agent_connection() as conn:
            conn.execute(
                """
                INSERT INTO decisions (
                    decision_id, run_id, arm, invoice_id, subscription_id,
                    triggering_attempt_id, model_version, calibrated_prob,
                    candidate_set, expected_value_paise, proposed_action,
                    guardrail_verdict, authorizing_rule, final_action,
                    scheduled_for, human_approval_required, decided_by
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    decision_id,
                    self.run_id,
                    self.arm,
                    invoice_id,
                    case.subscription.subscription_id,
                    case.first_charge.attempt_id,
                    MODEL_VERSION,
                    chosen.p_success if chosen else None,
                    _jsonb([c.to_dict() for c in plan.candidates] if plan else []),
                    int(chosen.expected_value_paise) if chosen else None,
                    action,
                    payload.get("verdict", "DENY"),
                    payload.get("authorizing_rule") or payload.get("stop_reason") or "unstated",
                    action if payload.get("authorised") else "blocked",
                    execute_at,
                    payload.get("verdict") == "ESCALATE_HUMAN",
                    "agent",
                ),
            )

        if execute_at:
            self._decisions[f"{invoice_id}|{action}|{execute_at}"] = decision_id
        self.rows_written += 1
        return decision_id

    # ------------------------------------------------------------------ audit_log

    def record_action(
        self,
        payload: dict[str, Any],
        *,
        trigger: str,
        outcome: str | None = None,
        stop_reason: str | None = None,
        violation: bool = False,
    ) -> None:
        """One thing that happened — or one thing that was refused."""
        invoice_id = payload.get("invoice_id")
        if not invoice_id:
            return
        self.covered.add(invoice_id)

        action = payload.get("action")
        execute_at = payload.get("execute_at")
        decision_id = self._decisions.get(f"{invoice_id}|{action}|{execute_at}")

        observed = {
            "customer_hash": payload.get("customer_hash"),
            "amount_paise": payload.get("amount_paise"),
            "attempt_number": payload.get("attempt_number"),
            "authorizing_rule": payload.get("authorizing_rule"),
            "execute_at": execute_at,
            "detail": payload.get("detail"),
            "error": payload.get("error"),
        }

        with agent_connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    run_id, arm, agent_id, agent_version, subject_type, subject_id,
                    trigger, observed_data, decision_id, action_taken, channel,
                    execution_mode, razorpay_entity_id, outcome,
                    recovered_amount_paise, stop_reason, compliance_violation
                ) VALUES (
                    %s, %s, %s, %s, 'invoice', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    self.run_id,
                    self.arm,
                    AGENT_ID,
                    AGENT_VERSION,
                    invoice_id,
                    trigger,
                    _jsonb(observed),
                    decision_id,
                    action,
                    payload.get("channel"),
                    payload.get("execution_mode"),
                    payload.get("razorpay_entity_id"),
                    outcome or payload.get("outcome"),
                    payload.get("recovered_paise", 0) or 0,
                    stop_reason,
                    violation,
                ),
            )
        self.rows_written += 1

    def record_conclusion(self, payload: dict[str, Any]) -> None:
        """An invoice concluded without touching the customer.

        **The defect this closes.** ``audit_log`` was written only by the tools that
        *do* something, so a write-off — which calls no tool, because there is nothing to
        call — produced a ``decisions`` row and no audit row at all. The first full batch
        finished with 184 decisions and 156 audit rows: it had reached a conclusion on
        every invoice in the cohort and the compliance artifact was silent about 34 of
        them. An audit trail that records only what a system did, and never what it
        declined to do, cannot answer the one question it exists for — *why was this
        customer not charged* — and the invoices it omits are precisely the ones where
        the answer is a rule.

        ``outcome`` is ``blocked`` for a write-off and ``escalated`` for an escalation,
        and ``stop_reason`` carries the rule that closed the door, or ``None`` when the
        guardrail permitted a presentment and the policy declined it on value. The
        distinction matters and is deliberately not flattened: ``blocked`` with a
        ``stop_reason`` is a compliance stop, ``blocked`` without one is an economic
        judgement about an attempt that was legally available.

        ``compliance_violation`` stays false. Nothing was breached — that is the point.
        """
        invoice_id = payload.get("invoice_id")
        if not isinstance(invoice_id, str):
            return
        case = self.bench.cases.get(invoice_id)
        if case is None:
            return

        action = payload.get("action")
        self.record_action(
            {
                "invoice_id": invoice_id,
                "customer_hash": case.customer.customer_hash,
                "amount_paise": case.invoice.amount_paise,
                "action": action,
                "execute_at": payload.get("execute_at"),
                "authorizing_rule": payload.get("authorizing_rule"),
                "execution_mode": str(self.bench.adapter.mode),
            },
            trigger="batch_scan",
            outcome="escalated" if action == str(ActionKind.ESCALATE) else "blocked",
            stop_reason=_binding_refusal(self.bench.plans.get(invoice_id)),
        )

    def record_silence(self, invoice_id: str) -> None:
        """The agent finished an invoice and left no trace of it.

        **The defect this closes.** ``live_v2`` worked twelve invoices and wrote eleven
        audit rows. ``inv_0007_01`` had a ``decisions`` row with a full APPROVE and its
        authorizing rule — ``npci_1_plus_3: attempt 2/4 permitted; non_peak_window: 13:30
        IST is outside peak hours; …`` — and then nothing. Its closing sentence was *"The
        guardrail approved the retry"*: the agent obtained the approval, narrated it, and
        ran out of turns before calling ``execute_recovery``. Nothing illegal happened,
        and no money moved. But the trail said the same thing about that invoice as it
        would about one the batch had never opened, and those two are not the same fact.

        An unspent approval is the sharper case and is named separately. It means the
        guardrail did authorise a presentment, the invoice was legally chargeable, and the
        attempt was simply never made — a recovery lost to the agent's own turn budget
        rather than to a rule. Reporting that as a compliance stop would be a lie in the
        merchant's favour, so ``stop_reason`` distinguishes the two:
        ``approval_granted_not_spent`` versus ``no_conclusion_reached``.

        Recorded from the batch loop rather than a hook, because there is no tool call to
        hang it on — that absence is the whole event.
        """
        case = self.bench.cases.get(invoice_id)
        if case is None:
            return

        prefix = f"{invoice_id}|"
        unspent = any(key.startswith(prefix) for key in self.bench.approvals)
        self.record_action(
            {
                "invoice_id": invoice_id,
                "customer_hash": case.customer.customer_hash,
                "amount_paise": case.invoice.amount_paise,
                "action": None,
                "detail": (
                    "guardrail approved a presentment that was never executed"
                    if unspent
                    else "the agent ended without acting or concluding"
                ),
                "execution_mode": str(self.bench.adapter.mode),
            },
            trigger="batch_scan",
            outcome="blocked",
            stop_reason="approval_granted_not_spent" if unspent else "no_conclusion_reached",
        )

    def record_denial(self, tool_name: str, input_data: dict[str, Any], reason: str) -> None:
        """A refusal by the permission gate.

        Written with ``outcome='blocked'`` and the gate's own reason string. This is the
        row the demo puts on screen when the 5th attempt is refused, and it exists
        because the gate calls this directly — ``PostToolUse`` never fires for a tool
        that did not run.

        ``action_taken`` is the tool that was refused, not a guess at what it would have
        done. An off-list tool has no Winback action to name, and writing "nudge" against
        a blocked ``payment_link_notify`` would put a message in the audit trail that was
        never even proposed.
        """
        invoice_id = input_data.get("invoice_id")
        if not isinstance(invoice_id, str):
            return
        case = self.bench.cases.get(invoice_id)
        action = {MONEY_TOOL: "retry", NOTIFY_TOOL: "nudge"}.get(tool_name, f"blocked:{tool_name}")
        self.record_action(
            {
                "invoice_id": invoice_id,
                "customer_hash": case.customer.customer_hash if case else None,
                "amount_paise": case.invoice.amount_paise if case else None,
                "action": action,
                "execute_at": input_data.get("execute_at"),
                "detail": reason,
                "execution_mode": str(self.bench.adapter.mode),
            },
            trigger="permission_gate",
            outcome="blocked",
            stop_reason=reason.split(":")[0],
        )


def _is_tool_refusal(payload: dict[str, Any]) -> bool:
    """Did the tool refuse to run, or did the action run and fail?

    Both shapes carry an ``error`` key, and telling them apart by its truthiness alone
    was wrong. ``ExecutionResult.error`` is *Razorpay's error fields, verbatim, when a
    presentment failed* — a declined debit is a **result**, and an
    ``insufficient_funds`` retry is the single most ordinary thing this system observes.
    Reading that as a tool error wrote eight of them into ``audit_log`` as
    ``trigger='tool_error', outcome='blocked'``, which is the compliance signal: the red
    chip in the demo, the column the violations chart counts. Eight customers with no
    money were recorded as eight actions the guardrail stopped.

    ``AdapterError`` says the same thing from the other side — "conflating them would
    let an outage look like a customer with no money" — and this is that sentence
    inverted, so the discriminator is spelled out here rather than left to a truthiness
    check. A tool that refused never reached the adapter and so has no ``outcome``; a
    tool that ran always returns one, because every ``ExecutionResult`` has the field.
    """
    return bool(payload.get("error")) and "outcome" not in payload


def make_audit_hook(writer: AuditWriter):
    """The ``PostToolUse`` callback. Appends, and returns nothing that steers the agent.

    Deliberately returns an empty dict rather than feedback. A hook that could tell the
    agent what to do next would be a second, undocumented decision-maker sitting behind
    the guardrail, and the whole argument of this project is that there is exactly one.
    """

    async def audit_hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        if tool_name not in AUDITED:
            return {}

        payload = _payload(input_data.get("tool_response"))
        try:
            if _is_tool_refusal(payload):
                # A tool that refused itself. Recorded, because a refusal the audit trail
                # cannot see is a refusal nobody can verify happened.
                writer.record_action(
                    {**payload, "execution_mode": str(writer.bench.adapter.mode)},
                    trigger="tool_error",
                    outcome="blocked",
                    stop_reason=str(payload.get("error")),
                )
            elif tool_name == GUARDRAIL_TOOL:
                writer.record_decision(payload)
                # A write-off or an escalation is a conclusion, and no tool follows it —
                # so this is the only moment at which it can reach `audit_log`. Guarded
                # on `authorised` so a *refused* terminal action is not recorded as one
                # that was taken.
                if payload.get("action") in TERMINAL_ACTIONS and payload.get("authorised"):
                    writer.record_conclusion(payload)
            else:
                writer.record_action(payload, trigger="batch_scan")
        except Exception as exc:  # broad on purpose — see below
            # Caught rather than raised, because the SDK swallows what a hook raises and
            # the batch must finish unattended either way. The difference is that the
            # failure is now *counted* and printed at the end of the run instead of
            # scrolling past on stderr. The first time this happened the only evidence
            # was an audit row whose ``decision_id`` was NULL.
            writer.write_failures.append(
                f"{tool_name} {payload.get('invoice_id', '?')}: {type(exc).__name__}: {exc}"
            )
        return {}

    return audit_hook


def audit_matcher(writer: AuditWriter) -> HookMatcher:
    """``HookMatcher(matcher=None, hooks=[...])`` — verified against the installed SDK.

    ``matcher=None`` means every tool, and the filtering happens in the callback against
    :data:`AUDITED`. Matching in Python rather than in a pattern string keeps the list of
    audited tools next to the list of allowed tools, where a reviewer can compare them.
    """
    return HookMatcher(matcher=None, hooks=[make_audit_hook(writer)])

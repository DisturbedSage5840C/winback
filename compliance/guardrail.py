"""The single gate. Every action Winback takes is evaluated here first.

The individual rule modules each answer one legal question. This module answers
the operational one: *given a concrete proposed action, may it happen?* It does
three things and deliberately nothing else.

**It selects the applicable rules by action kind.** A mandate presentment is
governed by NPCI's attempt cap, NPCI's execution window, RBI's AFA ceiling, RBI's
pre-debit notice, and whether the mandate is still alive. A message is governed by
TRAI consent. Conflating the two is a real and expensive mistake in both
directions: gating a debit on marketing consent forfeits recovery on every
customer who ever opted out of SMS, and gating a message on the debit cap silences
exactly the customer who most needs to hear from us.

**It resolves conflicts by taking the most restrictive verdict.** A permissive
rule can never launder a blocking one.

**It produces the audit row.** ``authorizing_rule`` is the verbatim string written
to ``decisions.authorizing_rule``; every individual result is kept, because
"blocked by the cap, and also mistimed" is a different operational story from
"blocked by the cap alone".

What it does not do is take a probability. ``evaluate`` has no parameter a model
can set, and ``ActionRequest`` has no field for a score, a confidence or an
override. That is the structural claim of the project: the model chooses among
legal actions, and never argues about which actions are legal. A test asserts it
on the signature, so the claim cannot quietly stop being true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from compliance import afa_threshold, consent_gate, npci_retry_cap, pre_debit_notice
from compliance.non_peak_window import IST, is_non_peak, next_slots
from compliance.result import RuleResult, Verdict, most_restrictive
from compliance.root_cause import RootCause, is_retryable

WINDOW_RULE = "non_peak_window"
PEAK_WINDOW = "peak_window"

RETRYABILITY_RULE = "root_cause_retryability"
BD_HARD_BLOCKED = "bd_hard_not_retryable"

NO_OP_RULE = "no_money_moves"

#: How many legal slots to hand back with a redirect. Three is enough for the
#: policy layer to score a real choice and few enough to fit in an audit row.
REDIRECT_SLOT_COUNT = 3


class ActionKind(StrEnum):
    """What the agent proposes to do about an at-risk invoice."""

    #: Present the mandate again. Moves money. The heavily governed path.
    RETRY = "retry"
    #: Message the customer, or send a payment link. Governed by consent, not NPCI.
    NUDGE = "nudge"
    #: Hand the invoice to a human. Moves nothing, sends nothing.
    ESCALATE = "escalate"
    #: Stop working the invoice. Moves nothing, sends nothing.
    WRITE_OFF = "write_off"


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionRequest:
    """A concrete proposed action, with every fact the rules need to judge it.

    Frozen, because a rule that could be re-run against mutated inputs is not a
    record of anything — the audit trail has to replay to the same verdict.
    """

    kind: ActionKind
    #: When the action would actually run. For a redirected retry this is the slot
    #: being *proposed*, not the moment of the decision.
    execute_at: datetime

    amount_paise: int
    mcc_category: str

    #: Legal attempts already spent on this invoice, observational history included.
    attempts_used: int
    root_cause: RootCause

    charge_at: datetime
    notice_sent_at: datetime | None

    consent_status: str
    consent_updated_at: datetime
    last_transaction_at: datetime | None

    @property
    def attempt_number(self) -> int:
        """The 1-indexed attempt this action would become.

        Derived rather than passed, so ``attempts_used`` and ``attempt_number`` can
        never disagree — a discrepancy there would put the notice rule and the cap
        rule on different sides of the same invoice.
        """
        return self.attempts_used + 1


@dataclass(frozen=True, slots=True)
class GuardrailDecision:
    """The verdict, and everything needed to defend it later."""

    verdict: Verdict
    authorizing_rule: str
    stop_reason: str | None
    results: tuple[RuleResult, ...]
    suggested_slots: tuple[datetime, ...] = field(default=())

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.APPROVE

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe, for a JSONB column. No custom encoder required downstream."""
        return {
            "verdict": str(self.verdict),
            "authorizing_rule": self.authorizing_rule,
            "stop_reason": self.stop_reason,
            "results": [result.to_dict() for result in self.results],
            "suggested_slots": [slot.isoformat() for slot in self.suggested_slots],
        }


def _check_window(execute_at: datetime) -> RuleResult:
    """NPCI's execution window, wrapped as a rule result.

    Lives here rather than in ``non_peak_window`` because it is the only rule whose
    answer depends on the *shape* of the guardrail's contract: a mistimed action is
    redirected, and the slots that make a redirect actionable are produced by the
    same call.
    """
    # Converted before it is formatted, not merely before it is judged. `is_non_peak`
    # has always done its own conversion, so the *verdict* was never wrong — but the
    # detail string used to `strftime` whatever zone the caller happened to hand in and
    # then label it "IST" regardless, and that string is written verbatim into
    # `decisions.authorizing_rule` and onto the compliance chip. A UTC caller therefore
    # produced a permanent record that named the wrong hour about the one rule that is
    # entirely about the hour. The batch passes IST-aware slots and so was never
    # affected; the API panel passes `datetime.now(UTC)` and was, on every lookup.
    # See docs/WHAT_BROKE.md, 4 Sep.
    local = execute_at.astimezone(IST)
    if is_non_peak(execute_at):
        return RuleResult(
            rule=WINDOW_RULE,
            verdict=Verdict.APPROVE,
            detail=f"{WINDOW_RULE}: {local.strftime('%H:%M')} IST is outside peak hours",
            metadata={"execute_at": execute_at.isoformat()},
        )

    slots = next_slots(execute_at, n=REDIRECT_SLOT_COUNT)
    return RuleResult(
        rule=WINDOW_RULE,
        verdict=Verdict.REDIRECT_TO_WINDOW,
        detail=(
            f"{WINDOW_RULE}: {local.strftime('%H:%M')} IST is inside a peak window; "
            f"next legal slot {slots[0].astimezone(IST).strftime('%H:%M')} IST"
        ),
        stop_reason=PEAK_WINDOW,
        metadata={
            "execute_at": execute_at.isoformat(),
            "suggested_slots": [slot.isoformat() for slot in slots],
        },
    )


def _check_retryability(root_cause: RootCause) -> RuleResult:
    """Whether the mandate is still alive enough to be worth a legal attempt."""
    if is_retryable(root_cause):
        return RuleResult(
            rule=RETRYABILITY_RULE,
            verdict=Verdict.APPROVE,
            detail=f"{RETRYABILITY_RULE}: {root_cause} may be retried",
            metadata={"root_cause": str(root_cause)},
        )
    return RuleResult(
        rule=RETRYABILITY_RULE,
        verdict=Verdict.DENY,
        detail=(
            f"{RETRYABILITY_RULE}: {root_cause} means the authorisation no longer "
            f"exists; a retry spends a legal attempt against nothing"
        ),
        stop_reason=BD_HARD_BLOCKED,
        metadata={"root_cause": str(root_cause)},
    )


def _rules_for(request: ActionRequest, now: datetime) -> list[RuleResult]:
    match request.kind:
        case ActionKind.RETRY:
            return [
                npci_retry_cap.check(attempts_used=request.attempts_used),
                _check_window(request.execute_at),
                afa_threshold.check(
                    amount_paise=request.amount_paise, mcc_category=request.mcc_category
                ),
                pre_debit_notice.check(
                    notice_sent_at=request.notice_sent_at,
                    charge_at=request.charge_at,
                    attempt_number=request.attempt_number,
                ),
                _check_retryability(request.root_cause),
            ]
        case ActionKind.NUDGE:
            return [
                consent_gate.check_nudge(
                    consent_status=request.consent_status,
                    consent_updated_at=request.consent_updated_at,
                    last_transaction_at=request.last_transaction_at,
                    now=now,
                )
            ]
        case ActionKind.ESCALATE | ActionKind.WRITE_OFF:
            # Nothing to govern. A guardrail that could block the safe fallback would
            # leave the agent with no legal move at all, which is how an autonomous
            # system ends up choosing an illegal one.
            return [
                RuleResult(
                    rule=NO_OP_RULE,
                    verdict=Verdict.APPROVE,
                    detail=f"{NO_OP_RULE}: {request.kind} moves no money and sends nothing",
                    metadata={"kind": str(request.kind)},
                )
            ]
        case _:
            # Unreachable today, and deliberately loud rather than absent. Without
            # this arm a new ActionKind falls through the match, returns None, and
            # surfaces as a TypeError inside most_restrictive -- i.e. an ungoverned
            # action kind reaching the money path, reported as a type bug. Whoever
            # adds a kind must decide which rules govern it.
            raise NotImplementedError(
                f"guardrail: no rule set defined for ActionKind {request.kind!r}. "
                "Every action kind must state which rules govern it; there is no "
                "default, because the default would be 'none'."
            )


def evaluate(request: ActionRequest, *, now: datetime) -> GuardrailDecision:
    """Judge a proposed action against every rule that applies to it.

    ``now`` is passed rather than read from the clock so that a decision replays to
    the same verdict from the audit trail, months later, on a different machine.
    """
    results = _rules_for(request, now)
    winner = most_restrictive(results)

    slots: tuple[datetime, ...] = ()
    if winner.verdict is Verdict.REDIRECT_TO_WINDOW:
        slots = tuple(next_slots(request.execute_at, n=REDIRECT_SLOT_COUNT))

    return GuardrailDecision(
        verdict=winner.verdict,
        authorizing_rule="; ".join(result.detail for result in results),
        stop_reason=winner.stop_reason,
        results=tuple(results),
        suggested_slots=slots,
    )

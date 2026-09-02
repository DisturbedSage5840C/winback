"""The executor contract, and why there are two of them.

Razorpay has no "charge this subscription now" endpoint. The Subscriptions API creates,
fetches, updates, cancels, pauses and resumes; it does not present a mandate. The only
real retry primitive is `POST /v1/payments/create/recurring`, which needs a token from a
completed mandate authorisation and needs S2S Recurring Payments activated on the account
by Razorpay support. That activation is not obtainable inside this build.

That constraint is the reason this package exists, and the design answer is deliberately
not a mock. **Every action takes the identical decision → guardrail → audit path, and only
the last inch differs.** One adapter calls Razorpay's test-mode API and comes back with a
real `plink_…` id; the other calls the seeded counterfactual oracle and comes back with an
outcome that was already determined before anyone asked. ``audit_log.execution_mode``
records which one ran, per row, and nothing anywhere else in the system knows the
difference.

Two properties this contract is shaped to enforce.

**An adapter cannot decide.** Its input is an already-approved action; there is no
parameter through which it could refuse one, and no path by which it could take one that
was not approved. The guardrail is upstream of both implementations, so "we checked
compliance in the simulator but not in the live lane" is not a state this code can reach.

**An adapter cannot send a message to a real person.** Both implementations treat the
notification channel as simulated: TRAI's DLT registration makes real SMS impractical and
non-compliant for a ten-day build, so the live adapter creates a genuine payment link with
``notify: {sms: false, email: false}`` and records ``channel = "simulated_sms"``. The link
is real, the send is stubbed, and the consent gate in front of it is real. Saying that
plainly is worth more than a demo that pretends otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from compliance.guardrail import ActionKind, GuardrailDecision

#: What every adapter records as the channel for a customer message. Never "sms".
#: There is one spelling of this so that a grep for real sends finds nothing.
SIMULATED_CHANNEL = "simulated_sms"


class ExecutionMode(StrEnum):
    """Which executor ran. Written verbatim into ``audit_log.execution_mode``."""

    LIVE = "live"
    SIMULATED = "simulated"


class Outcome(StrEnum):
    """What happened, in the vocabulary ``audit_log.outcome`` accepts."""

    RECOVERED = "recovered"
    FAILED = "failed"
    DEFERRED = "deferred"
    ESCALATED = "escalated"
    BLOCKED = "blocked"


class AdapterError(RuntimeError):
    """The executor could not complete the action it was handed.

    Distinct from a *failed* action: a declined presentment is a result, and a network
    timeout is not. Conflating them would let an outage look like a customer with no
    money, which is the single most misleading thing this system could record.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionRequest:
    """An approved action, ready to run.

    Carries the guardrail decision that authorised it rather than a boolean. The
    executor does not read it — but the audit hook does, and an adapter that were handed
    only ``approved=True`` would make it possible to write an audit row that says an
    action was allowed without saying which rule allowed it.
    """

    kind: ActionKind
    decision: GuardrailDecision

    subscription_id: str
    invoice_id: str
    #: Redacted at the boundary: ``sha256(customer_id)[:12]``, never the id itself.
    customer_hash: str
    amount_paise: int

    #: When the action runs. For a retry redirected out of a peak window this is the
    #: legal slot, not the moment the decision was made.
    execute_at: datetime
    attempt_number: int

    #: Only for the live lane, and only ever a test-mode entity.
    rzp_customer_id: str | None = None
    rzp_token_id: str | None = None

    def __post_init__(self) -> None:
        if not self.decision.allowed:
            raise AdapterError(
                f"refusing to execute a {self.kind} the guardrail did not approve "
                f"(verdict {self.decision.verdict}, stop_reason {self.decision.stop_reason}). "
                "An adapter is downstream of the gate; it is not a second chance at it."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionResult:
    """What the executor did, in the shape the audit row needs."""

    outcome: Outcome
    execution_mode: ExecutionMode
    recovered_paise: int = 0

    #: A real Razorpay id when the live lane ran: ``plink_…`` / ``order_…`` / ``pay_…``.
    #: ``None`` in simulation, and never a fabricated identifier — an audit trail with
    #: invented entity ids is worse than one with honest nulls.
    razorpay_entity_id: str | None = None

    channel: str | None = None
    #: Razorpay's error fields, verbatim, when a presentment failed.
    error: dict[str, Any] | None = None
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def recovered(self) -> bool:
        return self.outcome is Outcome.RECOVERED

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "execution_mode": str(self.execution_mode),
            "recovered_paise": self.recovered_paise,
            "razorpay_entity_id": self.razorpay_entity_id,
            "channel": self.channel,
            "error": self.error,
            "detail": self.detail,
            "metadata": self.metadata,
        }


@runtime_checkable
class Adapter(Protocol):
    """What both executors implement.

    Narrow on purpose. Every method takes an approved request and returns a result;
    none takes a policy, a probability, or a compliance flag.
    """

    mode: ExecutionMode

    def present(self, request: ExecutionRequest) -> ExecutionResult:
        """Present the mandate. The only method that moves money."""
        ...

    def nudge(self, request: ExecutionRequest) -> ExecutionResult:
        """Tell the customer. The channel is simulated in both implementations."""
        ...

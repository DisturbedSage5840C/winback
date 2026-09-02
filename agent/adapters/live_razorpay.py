"""The executor that calls Razorpay, and the honest account of what it cannot do.

Probe 9 in ``docs/LIVE_LANE_FINDINGS.md`` settled this with a control rather than an
assumption: ``POST /v1/payments/create/recurring`` and its documented sibling
``/payments/create/upi`` both return a byte-identical 400 on this account while
``/orders`` returns 200 on the same credentials. The whole ``/payments/create/*`` family
is not routed without S2S Recurring activation, which Razorpay grants by support ticket.
**So this adapter cannot present a mandate, and it does not pretend to.**

What it does instead is the action a customer would actually receive: a real payment
link, with a real ``plink_…`` id and a real short URL, created with every notification
channel suppressed. That is a genuine Razorpay artifact — not a mock, not a fixture —
and it lands in ``audit_log.razorpay_entity_id`` where anyone can look it up. What it is
*not* is a recovery. A link that exists is money that might arrive; this adapter returns
``DEFERRED`` for it and ``recovered_paise = 0``, because reporting an unpaid link as
recovered revenue is precisely the kind of number this project exists to argue against.

**Nothing here sends a message.** Every link is created with
``notify: {sms: false, email: false}`` — Razorpay echoed the suppression back on probe 4,
including a ``whatsapp: false`` this code does not even set — and the channel is recorded
as ``simulated_sms``. The ``notify_by`` route was confirmed reachable on probe 10 and is
deliberately never called. TRAI's DLT registration makes real delivery non-compliant for
a build of this length, and that is a compliance decision rather than a technical limit.

**Every call is counted.** ``WINBACK_LIVE_CALL_BUDGET`` caps how many real API calls one
batch may make, and exhausting it raises rather than silently falling back to simulation:
a batch that quietly changed lanes halfway would produce an audit trail whose
``execution_mode`` column was true row by row and misleading in aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from agent.adapters.base import (
    SIMULATED_CHANNEL,
    AdapterError,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    Outcome,
)
from compliance.guardrail import ActionKind
from core.config import Settings, get_settings

API = "https://api.razorpay.com/v1"
TIMEOUT = 30.0

#: Suppression, spelled once. Passed on every link this module creates; Razorpay echoes
#: it back with ``whatsapp: false`` added, which is why probe 4 is quoted rather than
#: trusted. Never build a link payload without it.
NOTIFY_OFF = {"sms": False, "email": False}


class LiveBudgetExhausted(AdapterError):
    """The batch has spent its allowance of real API calls.

    Raised rather than handled, and deliberately not caught into a simulated fallback.
    The point of a budget is that hitting it is an event someone reads.
    """


@dataclass
class LiveRazorpayAdapter:
    """Executes against Razorpay test mode. Real ids, suppressed sends, no presentment."""

    mode: ExecutionMode = field(default=ExecutionMode.LIVE, init=False)

    client: httpx.Client
    budget: int
    calls_made: int = field(default=0, init=False)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> LiveRazorpayAdapter:
        """Build from ``.env``, or from a settings object handed in. Refuses to exist
        without test-mode credentials.

        ``require_razorpay()`` raises when the keys are unset, and ``core.config``
        already refuses any key that does not start with ``rzp_test_`` at startup. So
        there is no code path from here to a live merchant account.

        The parameter exists because a caller that has already resolved settings — the
        orchestrator does, to decide the mode in the first place — must not have this
        method quietly consult a *different* object. It read the global cache before, so
        a settings object with the live mode set and no credentials produced an adapter
        built from whatever happened to be in ``.env``.
        """
        settings = settings or get_settings()
        key_id, secret = settings.require_razorpay()
        return cls(
            client=httpx.Client(auth=(key_id, secret), timeout=TIMEOUT),
            budget=settings.live_call_budget,
        )

    # ------------------------------------------------------------------ plumbing

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        """One real API call, counted before it is made.

        Counted first on purpose: a call that was attempted and timed out still reached
        Razorpay, and a budget that only counted successes would not be a budget.
        """
        if self.calls_made >= self.budget:
            raise LiveBudgetExhausted(
                f"live call budget of {self.budget} exhausted "
                f"(WINBACK_LIVE_CALL_BUDGET). Nothing further was sent."
            )
        self.calls_made += 1

        try:
            response = self.client.request(method, f"{API}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise AdapterError(f"{method} {path} failed in transport: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}

        if not response.is_success:
            raise AdapterError(
                f"{method} {path} returned {response.status_code}: "
                f"{body.get('error', body) if isinstance(body, dict) else body}"
            )
        return body

    def _create_link(self, request: ExecutionRequest, description: str) -> dict[str, Any]:
        """A real payment link with every channel suppressed.

        ``reference_id`` carries the invoice id so a link found in the Razorpay
        dashboard can be traced back to the decision that created it, and ``notes``
        carries the redacted customer hash — never the customer id, and never a
        contact detail.
        """
        return self._call(
            "POST",
            "/payment_links",
            {
                "amount": request.amount_paise,
                "currency": "INR",
                "accept_partial": False,
                "description": description,
                "reference_id": f"{request.invoice_id}-a{request.attempt_number}",
                "notify": NOTIFY_OFF,
                "reminder_enable": False,
                "notes": {
                    "subscription_id": request.subscription_id,
                    "invoice_id": request.invoice_id,
                    "customer_hash": request.customer_hash,
                    "authorizing_rule": request.decision.authorizing_rule[:250],
                },
            },
        )

    # ------------------------------------------------------------------ actions

    def present(self, request: ExecutionRequest) -> ExecutionResult:
        """The presentment this account cannot make, and what stands in for it.

        Creates a real order and a real payment link. Returns ``DEFERRED`` — never
        ``RECOVERED`` — because no money has moved and none will until a human pays
        the link. The detail string says why in words, so the reason survives into the
        audit trail rather than living only in this docstring.
        """
        if request.kind is not ActionKind.RETRY:
            raise AdapterError(f"present() is for retries, not {request.kind}")

        order = self._call(
            "POST",
            "/orders",
            {
                "amount": request.amount_paise,
                "currency": "INR",
                "receipt": f"{request.invoice_id}-a{request.attempt_number}"[:40],
                "notes": {"invoice_id": request.invoice_id, "customer_hash": request.customer_hash},
            },
        )
        link = self._create_link(request, f"Winback recovery · attempt {request.attempt_number}")

        return ExecutionResult(
            outcome=Outcome.DEFERRED,
            execution_mode=ExecutionMode.LIVE,
            recovered_paise=0,
            razorpay_entity_id=link.get("id"),
            channel=SIMULATED_CHANNEL,
            detail=(
                "S2S recurring is not routed on this account (see LIVE_LANE_FINDINGS "
                "probe 9), so no mandate was presented. A real payment link was created "
                "with notifications suppressed; nothing is recovered until it is paid."
            ),
            metadata={
                "order_id": order.get("id"),
                "payment_link_id": link.get("id"),
                "short_url": link.get("short_url"),
                "notify_echoed": link.get("notify"),
            },
        )

    def nudge(self, request: ExecutionRequest) -> ExecutionResult:
        """Create the artifact a nudge would carry, and do not deliver it.

        The link is real. The send is not made. ``channel`` records
        ``simulated_sms`` so no reader of the audit trail can conclude a message
        reached a phone.
        """
        if request.kind is not ActionKind.NUDGE:
            raise AdapterError(f"nudge() is for nudges, not {request.kind}")

        link = self._create_link(request, "Winback · your payment did not go through")

        return ExecutionResult(
            outcome=Outcome.DEFERRED,
            execution_mode=ExecutionMode.LIVE,
            razorpay_entity_id=link.get("id"),
            channel=SIMULATED_CHANNEL,
            detail=(
                f"payment link {link.get('id')} created for {request.customer_hash}; "
                "delivery suppressed (notify sms/email false, notify_by never called)"
            ),
            metadata={"short_url": link.get("short_url"), "notify_echoed": link.get("notify")},
        )

    # ------------------------------------------------------------------ reads

    def reconcile(self, payment_link_id: str) -> ExecutionResult:
        """Has anyone paid this link?

        The one call that could legitimately turn a ``DEFERRED`` into a ``RECOVERED``,
        and it does so only on Razorpay's own ``paid_amount``. Reads are cheap and real;
        this is how the live cohort would close the loop if a link were ever paid by
        hand during the demo.
        """
        link = self._call("GET", f"/payment_links/{payment_link_id}")
        paid = int(link.get("amount_paid") or 0)

        return ExecutionResult(
            outcome=Outcome.RECOVERED if paid > 0 else Outcome.DEFERRED,
            execution_mode=ExecutionMode.LIVE,
            recovered_paise=paid,
            razorpay_entity_id=payment_link_id,
            detail=f"link status {link.get('status')}, amount_paid {paid}",
            metadata={"status": link.get("status")},
        )

    def close(self) -> None:
        self.client.close()

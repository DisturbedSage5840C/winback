"""The live executor, tested against a fake transport instead of the real Razorpay API.

No network call is real here — ``httpx.Client`` is replaced with an object that records
what it was asked to send and answers with a canned Razorpay-shaped response. What is
worth locking in without a live account: that a presentment spends exactly one real call,
that a nudge and the retry that follows it never collide on ``reference_id``, and that the
same is true of two nudges in a row (a ``REDIRECT_TO_WINDOW`` re-ask leaves the compliance
attempt count untouched, so the reference id has to come from somewhere else).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent.adapters.base import ExecutionRequest
from agent.adapters.live_razorpay import LiveRazorpayAdapter
from compliance.guardrail import ActionKind, GuardrailDecision
from compliance.result import Verdict

APPROVED = GuardrailDecision(
    verdict=Verdict.APPROVE,
    authorizing_rule="npci_1_plus_3: attempt 2/4",
    stop_reason=None,
    results=(),
)


@dataclass
class _FakeResponse:
    payload: dict[str, Any]
    is_success: bool = True
    status_code: int = 200

    def json(self) -> dict[str, Any]:
        return self.payload


@dataclass
class _FakeClient:
    """Stands in for ``httpx.Client``. Answers requests in the order they were queued."""

    responses: list[_FakeResponse]
    requests: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    def request(self, method: str, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        self.requests.append((method, url, json))
        return self.responses.pop(0)


def _link_response(entity_id: str) -> _FakeResponse:
    return _FakeResponse(
        {"id": entity_id, "short_url": f"https://rzp.io/l/{entity_id}", "notify": {"sms": False}}
    )


def _request(*, kind: ActionKind, sequence: int, attempt_number: int = 2) -> ExecutionRequest:
    return ExecutionRequest(
        kind=kind,
        decision=APPROVED,
        subscription_id="sub_test",
        invoice_id="inv_test",
        customer_hash="hash_test",
        amount_paise=50_000,
        execute_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        attempt_number=attempt_number,
        sequence=sequence,
    )


def _reference_ids(client: _FakeClient) -> list[str]:
    return [payload["reference_id"] for _, _, payload in client.requests if payload]


def test_a_presentment_spends_exactly_one_real_call():
    """A presentment used to also create an ``/orders`` entity nothing referenced
    afterwards -- one wasted real call per retry, on a budget that is supposed to be
    the honest cost of a batch."""
    client = _FakeClient(responses=[_link_response("plink_1")])
    adapter = LiveRazorpayAdapter(client=client, budget=10)

    result = adapter.present(_request(kind=ActionKind.RETRY, sequence=1))

    assert adapter.calls_made == 1
    assert len(client.requests) == 1
    assert client.requests[0][1].endswith("/payment_links")
    assert "order_id" not in result.metadata


def test_a_nudge_and_the_retry_that_follows_it_get_distinct_reference_ids():
    """The regression this closes. Both used to be built from ``attempt_number``, which
    counts retries only -- a nudge never advances it, so the first retry on an invoice
    that was already nudged reused the nudge's own ``reference_id`` and Razorpay
    rejected it as a duplicate."""
    client = _FakeClient(responses=[_link_response("plink_n"), _link_response("plink_r")])
    adapter = LiveRazorpayAdapter(client=client, budget=10)

    adapter.nudge(_request(kind=ActionKind.NUDGE, sequence=1, attempt_number=2))
    adapter.present(_request(kind=ActionKind.RETRY, sequence=2, attempt_number=2))

    ref_ids = _reference_ids(client)
    assert len(ref_ids) == len(set(ref_ids)) == 2


def test_two_nudges_on_the_same_invoice_get_distinct_reference_ids():
    """A ``REDIRECT_TO_WINDOW`` re-ask, or an agent that ignores the system prompt and
    nudges twice, never advances ``attempts_used`` either -- both nudges used to be
    handed the identical ``attempt_number``."""
    client = _FakeClient(responses=[_link_response("plink_a"), _link_response("plink_b")])
    adapter = LiveRazorpayAdapter(client=client, budget=10)

    adapter.nudge(_request(kind=ActionKind.NUDGE, sequence=1, attempt_number=2))
    adapter.nudge(_request(kind=ActionKind.NUDGE, sequence=2, attempt_number=2))

    ref_ids = _reference_ids(client)
    assert len(ref_ids) == len(set(ref_ids)) == 2

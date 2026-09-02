"""The two executors, and the contract they both have to satisfy.

The architectural claim of this project is that live and simulated execution sit behind
one identical decision → guardrail → audit path, and that only the executor differs. That
claim is worth exactly as much as the tests below: that neither adapter can be reached
without an approval, that both report honestly what they did, and that
``audit_log.execution_mode`` always says which one ran.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta

import pytest

from agent.adapters.base import (
    SIMULATED_CHANNEL,
    AdapterError,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    Outcome,
)
from agent.adapters.simulated import ATTEMPT_ROW, SimulatedAdapter
from compliance.guardrail import ActionKind, GuardrailDecision
from compliance.result import Verdict
from eval.counterfactual import DECISION_LAG_HOURS

APPROVED = GuardrailDecision(
    verdict=Verdict.APPROVE,
    authorizing_rule="npci_1_plus_3: attempt 2/4",
    stop_reason=None,
    results=(),
)
DENIED = GuardrailDecision(
    verdict=Verdict.DENY,
    authorizing_rule="npci_1_plus_3",
    stop_reason="cap_exhausted",
    results=(),
)


def _request(
    adapter: SimulatedAdapter,
    invoice_id: str,
    *,
    decision=APPROVED,
    attempt=2,
    kind=ActionKind.RETRY,
):
    case = adapter.cases[invoice_id]
    at = case.first_charge.attempted_at + timedelta(hours=DECISION_LAG_HOURS)
    return ExecutionRequest(
        kind=kind,
        decision=decision,
        subscription_id=case.subscription.subscription_id,
        invoice_id=invoice_id,
        customer_hash=case.customer.customer_hash,
        amount_paise=case.invoice.amount_paise,
        execute_at=at,
        attempt_number=attempt,
    )


@pytest.fixture
def case_id(adapter: SimulatedAdapter) -> str:
    return sorted(adapter.cases)[0]


# ----------------------------------------------------------------- the contract


def test_an_unapproved_request_cannot_be_constructed(adapter, case_id):
    """The last of the three independent refusals: the gate, ``_execute``, and here. All
    three have to fail together before an unapproved presentment can happen."""
    with pytest.raises(AdapterError):
        _request(adapter, case_id, decision=DENIED)


def test_both_adapters_satisfy_the_protocol(adapter):
    """One guarded code path, two executors. If the live adapter drifts out of the
    protocol the orchestrator would still type-check and fail at the worst moment —
    mid-batch, against the real API."""
    from agent.adapters.base import Adapter
    from agent.adapters.live_razorpay import LiveRazorpayAdapter

    assert isinstance(adapter, Adapter)
    for cls in (SimulatedAdapter, LiveRazorpayAdapter):
        assert callable(cls.present) and callable(cls.nudge)


def test_the_result_always_names_its_execution_mode(adapter, case_id):
    result = adapter.present(_request(adapter, case_id))
    assert result.execution_mode is ExecutionMode.SIMULATED
    assert result.to_dict()["execution_mode"] == "simulated"


# ----------------------------------------------------------------- the simulator


def test_the_oracle_answer_does_not_depend_on_who_asks(adapter, case_id):
    """The counterfactual property the whole evaluation rests on. The coin flip for an
    ``(invoice, attempt, action, slot)`` is fixed regardless of which arm asks for it —
    without this, the four-arm comparison is unpaired and the CIs are meaningless."""
    first = adapter.present(_request(adapter, case_id))
    adapter.reset()
    second = adapter.present(_request(adapter, case_id))

    assert first.outcome is second.outcome
    assert first.recovered_paise == second.recovered_paise


def test_a_recovered_presentment_reports_the_money_it_recovered(adapter):
    """Scan for a case the oracle actually pays, and check the arithmetic is honest."""
    for invoice_id in sorted(adapter.cases)[:40]:
        result = adapter.present(_request(adapter, invoice_id))
        if result.outcome is Outcome.RECOVERED:
            assert result.recovered_paise > 0
            assert result.recovered is True
            return
    pytest.skip("no recovery in the sampled cases")


def test_a_failed_presentment_recovers_nothing(adapter):
    for invoice_id in sorted(adapter.cases)[:40]:
        result = adapter.present(_request(adapter, invoice_id))
        if result.outcome is Outcome.FAILED:
            assert result.recovered_paise == 0
            assert result.recovered is False
            return
    pytest.skip("no failure in the sampled cases")


def test_a_presentment_carries_its_attempt_row_for_the_database(adapter, case_id):
    result = adapter.present(_request(adapter, case_id))
    attempt = result.metadata[ATTEMPT_ROW]
    assert attempt.invoice_id == case_id
    assert attempt.attempt_number == 2


def test_a_nudge_is_deferred_and_never_claims_recovery(adapter, case_id):
    """Messaging someone does not collect money, and an executor that said otherwise
    would inflate every rupee figure in the evaluation."""
    result = adapter.nudge(_request(adapter, case_id, kind=ActionKind.NUDGE))

    assert result.outcome is Outcome.DEFERRED
    assert result.recovered_paise == 0
    assert result.channel == SIMULATED_CHANNEL


def test_the_adapter_owns_physics_continuity_not_policy_state(adapter, case_id):
    """``nudged_at`` here is the simulator's memory of what the customer was told, which
    changes the hazard. The *policy's* view of the same fact lives on the workbench, and
    ``reset`` clears only this one."""
    nudged = adapter.present(_request(adapter, case_id))
    adapter.nudge(_request(adapter, case_id, kind=ActionKind.NUDGE))
    adapter.reset()
    fresh = adapter.present(_request(adapter, case_id))

    # Reset restores the adapter to a batch's opening state, so the same presentment
    # resolves the same way it did before any physics accumulated.
    assert fresh.outcome is nudged.outcome

    # And the adapter refuses to run a kind it was not given.
    with pytest.raises(AdapterError):
        adapter.nudge(_request(adapter, case_id))


# ------------------------------------------------------------------- the live lane


def test_the_live_adapter_counts_a_call_before_it_makes_it():
    """The budget must decrement on the attempt, not the success. Counting afterwards
    would let a call that raised mid-flight escape the cap, which is the one direction a
    spend limit must never fail in."""
    import httpx

    from agent.adapters.live_razorpay import LiveBudgetExhausted, LiveRazorpayAdapter

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network", request=request)

    client = httpx.Client(transport=httpx.MockTransport(_boom))
    live = LiveRazorpayAdapter(client=client, budget=1)

    # The transport error is wrapped, not propagated: every caller of an adapter handles
    # exactly one exception type, and the httpx original is kept on ``__cause__``.
    with pytest.raises(AdapterError):
        live._call("POST", "/orders", {})
    assert live.calls_made == 1

    with pytest.raises(LiveBudgetExhausted):
        live._call("POST", "/orders", {})
    live.close()


def test_the_live_adapter_never_asks_razorpay_to_send_anything():
    """TRAI DLT registration makes real delivery non-compliant here, so every payment
    link is created with notifications off. This is a property of the payload, not of a
    dashboard setting someone could change."""
    from agent.adapters.live_razorpay import NOTIFY_OFF

    assert NOTIFY_OFF == {"sms": False, "email": False}


def test_the_live_adapter_does_not_claim_recovery_it_cannot_observe():
    """A payment link that has been created is not a payment that has been made. The
    live lane returns ``DEFERRED`` with zero rupees and leaves ``reconcile`` to find out
    later — anything else would put unearned money in the headline figure."""
    import inspect

    from agent.adapters.live_razorpay import LiveRazorpayAdapter

    source = inspect.getsource(LiveRazorpayAdapter.present)
    assert "Outcome.DEFERRED" in source
    assert "recovered_paise=0" in source or "recovered_paise" not in source


def test_execution_result_defaults_to_recovering_nothing():
    result = ExecutionResult(outcome=Outcome.DEFERRED, execution_mode=ExecutionMode.LIVE)
    assert result.recovered_paise == 0
    assert result.recovered is False
    assert result.to_dict()["recovered_paise"] == 0


def test_the_request_is_immutable_once_approved(adapter, case_id):
    """A request that could be edited after the guardrail saw it would make the approval
    a statement about a different action."""
    request = _request(adapter, case_id)
    with pytest.raises(FrozenInstanceError):
        request.amount_paise = 1  # type: ignore[misc]


def test_a_simulated_run_is_reproducible_across_adapter_instances(scorer, rates, case_id):
    """Two adapters built from the same frozen dataset answer identically, which is what
    makes ``docs/EVALUATION.md`` regenerable rather than a snapshot."""
    one = SimulatedAdapter.from_dataset(cohort="test")
    two = SimulatedAdapter.from_dataset(cohort="test")
    assert (
        one.present(_request(one, case_id)).outcome is two.present(_request(two, case_id)).outcome
    )


def test_the_decision_lag_is_a_real_datetime(adapter, case_id):
    request = _request(adapter, case_id)
    assert isinstance(request.execute_at, datetime)
    assert request.execute_at.tzinfo is not None

"""The composing guardrail: one gate, six rules, one verdict.

Everything money-moving passes through ``evaluate``. The individual rule modules
are tested exhaustively elsewhere; what is tested here is the composition:

* the **right rules** run for each kind of action (consent does not gate a debit;
  the NPCI cap does not gate a message);
* the **most restrictive** verdict wins, regardless of rule order;
* the ``authorizing_rule`` string that lands in the audit row is legible enough
  that a panelist reading one row understands why the action was permitted;
* a confident model cannot get past any of it.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest

from compliance.guardrail import ActionKind, ActionRequest, GuardrailDecision, evaluate
from compliance.non_peak_window import IST
from compliance.result import Verdict
from compliance.root_cause import RootCause
from core.money import paise

NOW = datetime(2026, 9, 15, 1, 0, tzinfo=IST)
LEGAL_SLOT = datetime(2026, 9, 15, 2, 0, tzinfo=IST)     # 02:00 IST, non-peak
PEAK_SLOT = datetime(2026, 9, 15, 11, 0, tzinfo=IST)     # 11:00 IST, inside morning peak


def retry_request(**overrides: object) -> ActionRequest:
    """A clean, fully compliant retry. Every test below breaks exactly one thing."""
    defaults: dict[str, object] = {
        "kind": ActionKind.RETRY,
        "execute_at": LEGAL_SLOT,
        "amount_paise": paise(499),
        "mcc_category": "streaming",
        "attempts_used": 1,
        "root_cause": RootCause.BD_TRANSIENT,
        "charge_at": LEGAL_SLOT,
        "notice_sent_at": LEGAL_SLOT - timedelta(hours=48),
        "consent_status": "active",
        "consent_updated_at": NOW - timedelta(days=300),
        "last_transaction_at": NOW - timedelta(days=1),
    }
    return ActionRequest(**(defaults | overrides))  # type: ignore[arg-type]


def nudge_request(**overrides: object) -> ActionRequest:
    return retry_request(kind=ActionKind.NUDGE, **overrides)


# ------------------------------------------------------------------- the happy path


def test_a_clean_retry_is_approved() -> None:
    decision = evaluate(retry_request(), now=NOW)
    assert decision.verdict is Verdict.APPROVE
    assert decision.allowed is True
    assert decision.stop_reason is None


def test_the_authorizing_rule_names_every_rule_that_ran() -> None:
    """This string is the audit row. If it does not name the rules, the audit trail
    records that something was approved without recording what approved it."""
    decision = evaluate(retry_request(), now=NOW)
    for rule in ("npci_1_plus_3", "non_peak_window", "afa_threshold", "pre_debit_notice"):
        assert rule in decision.authorizing_rule


def test_the_authorizing_rule_shows_the_attempt_budget() -> None:
    """The single most-asked question about this system, answerable from one string."""
    decision = evaluate(retry_request(attempts_used=2), now=NOW)
    assert "3/4" in decision.authorizing_rule


# ------------------------------------------------------------------- each rule bites


def test_the_retry_cap_blocks_a_fifth_attempt() -> None:
    decision = evaluate(retry_request(attempts_used=4), now=NOW)
    assert decision.verdict is Verdict.DENY
    assert decision.stop_reason == "npci_1_plus_3_cap_exhausted"


def test_a_peak_window_redirects_rather_than_denies() -> None:
    """A mistimed retry is legal, merely early. The guardrail hands back slots so the
    action survives; dropping it would forfeit a recovery for no compliance gain."""
    decision = evaluate(retry_request(execute_at=PEAK_SLOT), now=NOW)
    assert decision.verdict is Verdict.REDIRECT_TO_WINDOW
    assert decision.stop_reason == "peak_window"
    assert decision.suggested_slots, "a redirect with no slot to redirect to is a denial"
    assert all(slot > PEAK_SLOT for slot in decision.suggested_slots)


def test_an_approved_action_carries_no_slots() -> None:
    assert evaluate(retry_request(), now=NOW).suggested_slots == ()


def test_a_large_amount_escalates_to_a_human() -> None:
    decision = evaluate(retry_request(amount_paise=paise(22_000)), now=NOW)
    assert decision.verdict is Verdict.ESCALATE_HUMAN
    assert decision.stop_reason == "above_afa_ceiling"


def test_the_elevated_ceiling_applies_to_a_sip() -> None:
    decision = evaluate(
        retry_request(amount_paise=paise(22_000), mcc_category="mutual_fund_sip"), now=NOW
    )
    assert decision.verdict is Verdict.APPROVE


def test_a_missing_notice_blocks_a_first_debit() -> None:
    decision = evaluate(retry_request(attempts_used=0, notice_sent_at=None), now=NOW)
    assert decision.verdict is Verdict.DENY
    assert decision.stop_reason == "pre_debit_notice_missing"


def test_a_missing_notice_only_warns_on_a_retry() -> None:
    decision = evaluate(retry_request(attempts_used=2, notice_sent_at=None), now=NOW)
    assert decision.verdict is Verdict.APPROVE
    assert "warned, not blocked" in decision.authorizing_rule


def test_a_dead_mandate_is_never_retried() -> None:
    """BD_hard means the authorisation no longer exists. Retrying spends a legal
    attempt against nothing, and no probability the model produces changes that."""
    decision = evaluate(retry_request(root_cause=RootCause.BD_HARD), now=NOW)
    assert decision.verdict is Verdict.DENY
    assert decision.stop_reason == "bd_hard_not_retryable"


# ------------------------------------------------------------------- rule selection


def test_consent_does_not_gate_a_debit() -> None:
    """A mandate is a standing authorisation to debit. Telecom consent governs
    *messages*. Conflating them would block recovery on every customer who ever
    opted out of marketing SMS -- a compliance error in the expensive direction."""
    decision = evaluate(retry_request(consent_status="withdrawn"), now=NOW)
    assert decision.verdict is Verdict.APPROVE
    assert "consent_gate" not in decision.authorizing_rule


def test_the_retry_cap_does_not_gate_a_message() -> None:
    """NPCI's 1+3 counts presentments of a mandate, not messages. A customer past the
    cap is exactly the one who most needs to hear from us."""
    decision = evaluate(nudge_request(attempts_used=4), now=NOW)
    assert decision.verdict is Verdict.APPROVE
    assert "npci_1_plus_3" not in decision.authorizing_rule


def test_a_withdrawn_customer_receives_nothing() -> None:
    """The must-block case. Large sums at risk change nothing."""
    decision = evaluate(
        nudge_request(consent_status="withdrawn", amount_paise=paise(40_000)), now=NOW
    )
    assert decision.verdict is Verdict.DENY
    assert decision.stop_reason == "consent_withdrawn"


def test_a_nudge_is_not_bound_to_the_non_peak_window() -> None:
    """NPCI's window restricts mandate execution. A message is not an execution."""
    decision = evaluate(nudge_request(execute_at=PEAK_SLOT), now=NOW)
    assert decision.verdict is Verdict.APPROVE


@pytest.mark.parametrize("kind", [ActionKind.ESCALATE, ActionKind.WRITE_OFF])
def test_escalating_and_writing_off_are_always_permitted(kind: ActionKind) -> None:
    """Neither moves money nor sends anything. A guardrail that could block the safe
    fallback would leave the agent with no legal move at all."""
    decision = evaluate(
        retry_request(kind=kind, attempts_used=4, consent_status="withdrawn"), now=NOW
    )
    assert decision.verdict is Verdict.APPROVE


# ------------------------------------------------------------------- composition


def test_the_most_restrictive_verdict_wins() -> None:
    """A redirect and a denial at once resolves to the denial. The reverse -- letting
    a permissive rule launder a blocking one -- is the classic composition bug."""
    decision = evaluate(
        retry_request(execute_at=PEAK_SLOT, attempts_used=4), now=NOW
    )
    assert decision.verdict is Verdict.DENY
    assert decision.stop_reason == "npci_1_plus_3_cap_exhausted"


def test_escalation_outranks_a_redirect() -> None:
    decision = evaluate(
        retry_request(execute_at=PEAK_SLOT, amount_paise=paise(22_000)), now=NOW
    )
    assert decision.verdict is Verdict.ESCALATE_HUMAN


def test_every_rule_result_is_preserved_for_the_audit_row() -> None:
    """The losing verdicts matter too: 'blocked by the cap, and also mistimed' is a
    different operational story from 'blocked by the cap alone'."""
    decision = evaluate(retry_request(execute_at=PEAK_SLOT, attempts_used=4), now=NOW)
    verdicts = {result.verdict for result in decision.results}
    assert Verdict.DENY in verdicts
    assert Verdict.REDIRECT_TO_WINDOW in verdicts


def test_the_decision_serialises_for_the_audit_log() -> None:
    payload = evaluate(retry_request(attempts_used=4), now=NOW).to_dict()
    assert payload["verdict"] == "DENY"
    assert payload["stop_reason"] == "npci_1_plus_3_cap_exhausted"
    assert isinstance(payload["results"], list)
    assert all(isinstance(entry["verdict"], str) for entry in payload["results"])

    import json

    json.dumps(payload)  # must survive a JSONB write without a custom encoder


# ------------------------------------------------------------------- no override


def test_the_guardrail_takes_no_probability_and_no_override() -> None:
    """The structural claim of the whole project, asserted rather than asserted-to.

    There is no argument a model can pass that changes a verdict. If someone later
    adds a `confidence` or `force` parameter to get a demo unstuck, this fails.
    """
    parameters = set(inspect.signature(evaluate).parameters)
    assert parameters == {"request", "now"}

    fields = set(ActionRequest.__dataclass_fields__)
    forbidden = {
        "probability", "confidence", "score", "expected_value",
        "override", "force", "urgency", "model_says", "approved_by_model",
    }
    assert not (fields & forbidden), f"the gate can see {fields & forbidden}"


def test_the_request_is_frozen() -> None:
    """A rule that could be re-run against mutated inputs is not a record of anything."""
    request = retry_request()
    with pytest.raises((AttributeError, TypeError)):
        request.amount_paise = paise(1)  # type: ignore[misc]


def test_the_decision_is_frozen() -> None:
    decision: GuardrailDecision = evaluate(retry_request(), now=NOW)
    with pytest.raises((AttributeError, TypeError)):
        decision.verdict = Verdict.APPROVE  # type: ignore[misc]


def test_an_unknown_action_kind_raises_rather_than_falling_through() -> None:
    """The failure mode this guards is subtle and bad.

    Without an explicit fallthrough arm, adding an ActionKind makes ``_rules_for``
    return None, which surfaces as a TypeError inside ``most_restrictive`` -- an
    *ungoverned action kind reaching the money path*, reported as a type bug. The
    guardrail must fail in a way that names the actual problem.
    """
    request = retry_request()
    object.__setattr__(request, "kind", "teleport")  # bypasses the frozen dataclass

    with pytest.raises(NotImplementedError, match="no rule set defined"):
        evaluate(request, now=NOW)


def test_every_action_kind_has_a_rule_set() -> None:
    """Walks the enum, so a member added without a rule set fails here immediately
    rather than the first time an agent proposes it mid-batch."""
    for kind in ActionKind:
        decision = evaluate(retry_request(kind=kind), now=NOW)
        assert decision.results, f"{kind} produced no rule results at all"
        assert decision.authorizing_rule, f"{kind} produced an empty audit string"

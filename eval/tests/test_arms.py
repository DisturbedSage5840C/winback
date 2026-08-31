"""What each arm does, and what the run showed it actually did.

Two kinds of test, kept apart on purpose:

* **Behaviour**, on synthetic situations — an arm put in a state the frozen dataset does
  not happen to produce (an exhausted budget, a mandate the legacy job refuses to touch).
* **Findings**, on the real run — the claims ``docs/EVALUATION.md`` makes about how the
  baselines fail. These are pinned here so the prose cannot drift away from the numbers,
  which has already happened once (``docs/WHAT_BROKE.md``, 30 August). They assert the
  shape of each finding rather than its exact value, because the value belongs to the
  generated report and the shape is the argument.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from compliance.guardrail import ActionKind
from compliance.non_peak_window import IST, is_non_peak
from compliance.root_cause import RootCause
from core.money import paise
from eval.arms import (
    MAX_ATTEMPTS,
    Legacy,
    NeverRetry,
    RetryEverything,
    Situation,
    Winback,
    all_arms,
)
from ml.features import PriorState
from sim.generate import CustomerRow, InvoiceRow, SubscriptionRow

NOW = datetime(2026, 9, 14, 2, 0, tzinfo=IST)

CUSTOMER = CustomerRow(
    customer_id="cust_0001",
    customer_hash="a1b2c3d4e5f6",
    signup_date=date(2025, 6, 1),
    consent_status="active",
    consent_updated_at=datetime(2025, 6, 1, 9, 0, tzinfo=IST),
    salary_day=1,
    monthly_headroom_paise=paise(12_000),
)
SUBSCRIPTION = SubscriptionRow(
    subscription_id="sub_0001",
    customer_id="cust_0001",
    method="upi_autopay",
    bank="HDFC",
    mcc_category="saas",
    amount_paise=paise(1_499),
    status="active",
    mandate_start=date(2025, 6, 1),
    paid_count=3,
    remaining_count=9,
    cohort="test",
)
INVOICE = InvoiceRow(
    invoice_id="inv_0001",
    subscription_id="sub_0001",
    cycle_number=4,
    amount_paise=paise(1_499),
    charge_at=NOW - timedelta(hours=2),
    notice_sent_at=NOW - timedelta(days=2),
    status="failed",
)
PRIOR = PriorState(
    prior_root_cause="TD",
    prior_failures_this_invoice=1,
    last_attempt_at=NOW - timedelta(hours=2),
    last_success_at=NOW - timedelta(days=30),
    lifetime_attempts=4,
    lifetime_failures=1,
)


def situation(**overrides) -> Situation:
    base = {
        "subscription": SUBSCRIPTION,
        "customer": CUSTOMER,
        "invoice": INVOICE,
        "attempts_used": 1,
        "root_cause": RootCause.TD,
        "prior": PRIOR,
        "now": NOW,
        "nudged_at": None,
        "paid_count": 3,
    }
    return Situation(**(base | overrides))


# ------------------------------------------------------------------ behaviour


def test_arm_a_escalates_unconditionally():
    for attempts_used in range(1, MAX_ATTEMPTS + 1):
        move = NeverRetry().move(situation(attempts_used=attempts_used))
        assert move.kind is ActionKind.ESCALATE


@pytest.mark.parametrize(("attempts_used", "offset_days"), [(1, 1), (2, 2), (3, 3)])
def test_arm_b_retries_at_a_fixed_offset_from_the_charge(attempts_used, offset_days):
    move = RetryEverything().move(situation(attempts_used=attempts_used))
    assert move.kind is ActionKind.RETRY
    assert move.execute_at == INVOICE.charge_at + timedelta(days=offset_days)
    # And at the charge's own hour, which is where its legality comes from.
    assert move.execute_at.astimezone(IST).hour == INVOICE.charge_at.astimezone(IST).hour


def test_arm_b_stops_at_the_cap_because_the_schema_could_not_store_a_fifth():
    move = RetryEverything().move(situation(attempts_used=MAX_ATTEMPTS))
    assert move.kind is ActionKind.WRITE_OFF


def test_arm_b_never_consults_the_guardrail():
    """It proposes a peak-window slot when the charge was in one, with no complaint.

    The harness judges it anyway; that is the difference between a referee and a player.
    """
    peak_charge = INVOICE.charge_at.replace(hour=11, minute=30)
    assert not is_non_peak(peak_charge)
    move = RetryEverything().move(situation(invoice=replace(INVOICE, charge_at=peak_charge)))
    assert not is_non_peak(move.execute_at)


def test_arm_c_writes_off_the_mandates_the_legacy_job_refuses_to_touch():
    """Netbanking is one of the two censoring conditions. The arm inherits the refusal."""
    netbanking = replace(SUBSCRIPTION, method="netbanking")
    move = Legacy().move(situation(subscription=netbanking))
    assert move.kind is ActionKind.WRITE_OFF
    assert "never extended" in move.rationale


def test_arm_c_exhausts_its_schedule_then_writes_off():
    move = Legacy().move(situation(attempts_used=MAX_ATTEMPTS))
    assert move.kind is ActionKind.WRITE_OFF


def test_arm_d_carries_the_scored_candidate_set(scorer, rates):
    """The drill-down's evidence. An arm that cannot show its alternatives cannot explain."""
    move = Winback(scorer=scorer, rates=rates).move(situation())
    assert move.plan is not None
    assert len(move.plan.candidates) > 1
    assert move.rationale


def test_arm_d_resumes_exactly_one_nudge_lead_after_nudging(scorer, rates):
    """Resuming at any other moment would execute a plan the policy never scored."""
    arm = Winback(scorer=scorer, rates=rates)
    moves = [arm.move(situation(attempts_used=n)) for n in range(1, MAX_ATTEMPTS)]
    nudges = [m for m in moves if m.kind is ActionKind.NUDGE]
    if not nudges:
        pytest.skip("this synthetic state never nudges; covered on the real run")
    for move in nudges:
        assert move.resume_at == move.execute_at + timedelta(hours=arm.params.nudge_lead_hours)


def test_all_four_arms_are_distinct_and_in_report_order(scorer, rates):
    arms = all_arms(scorer=scorer, rates=rates)
    assert [a.arm for a in arms] == ["A", "B", "C", "D"]
    assert len({a.label for a in arms}) == 4


# ------------------------------------------------------------------ findings


def test_arm_b_breaks_the_root_cause_rule_not_the_window_rule(arms_by_id):
    """Measured, not designed — see docs/WHAT_BROKE.md, 30 August.

    B retries at the charge's own hour, and the generator bills outside the peak windows,
    so B inherits the charge's legality. Every violation it commits is a re-presentment of
    a mandate that is revoked, closed or hard-declined.
    """
    reasons = {step.decision.stop_reason for step in arms_by_id["B"].steps() if step.violation}
    assert reasons == {"bd_hard_not_retryable"}


def test_arm_bs_illegal_presentments_bought_it_nothing(arms_by_id):
    """The indictment of "retry everything": it spends legality and gets no money.

    Dead mandates do not pay, so the attempts B burns on them recover zero — which is why
    its raw recovery and its compliant recovery are the same number.
    """
    arm = arms_by_id["B"]
    assert sum(step.recovered_paise for step in arm.steps() if step.violation) == 0
    assert arm.recovered_paise == arm.compliant_recovered_paise


def test_most_of_arm_cs_recovery_is_illegal(arms_by_id):
    """Why raw rupees is the wrong headline.

    C's urgent branch fires at 11:30 IST, inside the morning peak. Scored on rupees it
    looks like the second-best arm; scored on rupees it was allowed to collect, it is the
    worst of the three arms that present at all.
    """
    arm = arms_by_id["C"]
    illegal = arm.recovered_paise - arm.compliant_recovered_paise
    assert illegal > arm.compliant_recovered_paise, "the finding has reversed"

    reasons = {step.decision.stop_reason for step in arm.steps() if step.violation}
    assert "peak_window" in reasons


def test_winback_matches_the_naive_arm_on_money_using_fewer_legal_attempts(arms_by_id):
    """The thesis, as an assertion.

    Not "D recovers more" — it does not, and the report says so. D recovers what B
    recovers, out of a smaller legal budget, without breaking the rule once.
    """
    b, d = arms_by_id["B"], arms_by_id["D"]
    assert d.compliant_recovered_paise >= b.compliant_recovered_paise
    assert d.legal_attempts_consumed < b.legal_attempts_consumed
    assert d.compliance_violations == 0 < b.compliance_violations
    assert d.paise_per_legal_attempt > b.paise_per_legal_attempt

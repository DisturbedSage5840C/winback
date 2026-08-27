"""What the legacy policy must do — including the parts of it that are wrong.

Most of this file asserts that a policy behaves badly. That is the point: the legacy
policy is evidence, and evidence that drifts is worthless. If someone "improves" the
value floor or moves the urgent cron out of peak hours, the censoring story and the
violations-by-arm column both quietly change, and every number in
``docs/EVALUATION.md`` becomes a claim about a different world than the one the text
describes. These tests make that impossible to do by accident.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from compliance.non_peak_window import IST, is_non_peak
from core.money import paise
from sim.legacy_policy import (
    BELOW_VALUE_FLOOR,
    DEFAULT_LEGACY,
    MAX_ATTEMPTS,
    UNSUPPORTED_RAIL,
    LegacyParams,
    censoring_reason,
    retry_schedule,
    violations,
    would_retry,
)
from sim.world import Mandate

CHARGE_AT = datetime(2026, 9, 3, 4, 0, tzinfo=IST)  # 04:00 IST, comfortably non-peak


def mandate(
    *, amount: int = paise(1_200), method: str = "upi_autopay", sub_id: str = "sub_0001"
) -> Mandate:
    return Mandate(
        subscription_id=sub_id, method=method, bank="HDFC", amount_paise=amount, paid_count=3
    )


# ----------------------------------------------------------------- the censoring


def test_a_small_invoice_was_never_retried() -> None:
    """The value floor. Half of the selection bias the model has to survive."""
    small = mandate(amount=paise(300))
    assert not would_retry(small)
    assert censoring_reason(small) == BELOW_VALUE_FLOOR
    assert retry_schedule(small, CHARGE_AT) == ()


def test_the_value_floor_is_exclusive_at_the_boundary() -> None:
    """``amount > ₹500``, so ₹500 exactly is censored and ₹500.01 is not.

    The boundary matters because generate.py samples amounts near it; an
    off-by-one here shifts the observed/censored ratio without anything failing.
    """
    assert not would_retry(mandate(amount=paise(500)))
    assert would_retry(mandate(amount=paise(500) + 1))


def test_netbanking_was_never_retried_at_all() -> None:
    """The other half, and the more honest kind of bias: nobody decided this."""
    nb = mandate(method="netbanking", amount=paise(5_000))
    assert not would_retry(nb)
    assert censoring_reason(nb) == UNSUPPORTED_RAIL
    assert retry_schedule(nb, CHARGE_AT) == ()


def test_the_rail_exclusion_outranks_the_value_floor() -> None:
    """A cheap netbanking mandate is censored for the rail, not for the amount.

    Both conditions hold, so the reason recorded has to be the one the legacy job
    would actually have hit first, or ``docs/DATA.md``'s censoring breakdown
    attributes rows to the wrong cause.
    """
    both = mandate(method="netbanking", amount=paise(100))
    assert censoring_reason(both) == UNSUPPORTED_RAIL


def test_an_ordinary_upi_invoice_was_observed() -> None:
    assert censoring_reason(mandate()) is None
    assert would_retry(mandate())


# ----------------------------------------------------------------- the schedule


def test_the_standard_branch_is_three_fixed_offsets_at_nine_am() -> None:
    schedule = retry_schedule(mandate(amount=paise(1_200)), CHARGE_AT)

    assert [r.attempt_number for r in schedule] == [2, 3, 4]
    assert [(r.execute_at - CHARGE_AT).days for r in schedule] == [1, 2, 3]
    assert {r.execute_at.astimezone(IST).hour for r in schedule} == {9}


def test_the_schedule_ignores_everything_except_the_amount() -> None:
    """Same offsets whatever the rail, the bank, or the mandate's history.

    This is the substantive difference from Winback, so it is worth an assertion
    rather than a comment: the legacy policy has no timing model at all.
    """
    baseline = retry_schedule(mandate(), CHARGE_AT)
    variants = [
        mandate(method="card_mandate"),
        Mandate("sub_0002", "upi_autopay", "SBI", paise(1_200), paid_count=41),
        Mandate("sub_0003", "upi_autopay", "HDFC", paise(1_200), paid_count=0),
    ]
    for variant in variants:
        assert [r.execute_at for r in retry_schedule(variant, CHARGE_AT)] == [
            r.execute_at for r in baseline
        ]


def test_the_standard_branch_happens_to_be_legal() -> None:
    """09:00 IST is outside both peak windows — by luck, not by design.

    Worth pinning: if this were illegal too, arm C's violation count would stop
    discriminating between the two branches and the peak-window finding would be
    an artifact of every legacy retry rather than of the urgent one.
    """
    schedule = retry_schedule(mandate(amount=paise(1_200)), CHARGE_AT)
    assert all(is_non_peak(r.execute_at) for r in schedule)
    assert violations(mandate(amount=paise(1_200)), CHARGE_AT) == ()


# ----------------------------------------------------------------- the violation


def test_a_high_value_invoice_takes_the_urgent_branch() -> None:
    schedule = retry_schedule(mandate(amount=paise(5_000)), CHARGE_AT)

    assert [r.attempt_number for r in schedule] == [2, 3, 4]
    assert [(r.execute_at - CHARGE_AT).days for r in schedule] == [0, 1, 2]
    assert {r.execute_at.astimezone(IST).hour for r in schedule} == {11}


def test_every_urgent_retry_lands_inside_the_npci_peak_window() -> None:
    """11:30 IST sits inside 10:00-13:00. This is arm C's compliance violation."""
    high_value = mandate(amount=paise(5_000))
    schedule = retry_schedule(high_value, CHARGE_AT)

    assert all(r.in_peak_window for r in schedule)
    assert violations(high_value, CHARGE_AT) == ("peak_window",) * 3


def test_the_urgent_threshold_is_exclusive_at_the_boundary() -> None:
    assert not retry_schedule(mandate(amount=paise(2_000)), CHARGE_AT)[0].in_peak_window
    assert retry_schedule(mandate(amount=paise(2_000) + 1), CHARGE_AT)[0].in_peak_window


def test_one_violated_rule_is_reported_once_per_offending_attempt() -> None:
    """``eval_arm_results.compliance_violations`` counts audit rows, and each of
    these retries produces one. Collapsing them to a single ``"peak_window"``
    would under-report arm C by two thirds."""
    assert len(violations(mandate(amount=paise(5_000)), CHARGE_AT)) == 3


# ----------------------------------------------------------------- the invariants


@pytest.mark.parametrize(
    ("params", "amount"),
    [
        (LegacyParams(retry_offsets_days=(1, 2, 3, 4)), paise(1_200)),  # standard branch
        (LegacyParams(urgent_offsets_days=(0, 1, 2, 3)), paise(5_000)),  # urgent branch
    ],
    ids=["standard", "urgent"],
)
def test_no_branch_can_exceed_the_npci_attempt_cap(
    params: LegacyParams, amount: int
) -> None:
    """``payment_attempts.attempt_number`` is CHECKed to 1..4.

    The legacy policy does not know about the cap, but the table it writes into
    does. A schedule that cannot be stored has to fail here, where the offsets are
    edited, rather than as an insert error inside generate.py.

    Parametrised so each branch is reached by an amount that actually selects it —
    a single test body would let one over-long branch hide behind the other.
    """
    with pytest.raises(ValueError, match="at most 4"):
        retry_schedule(mandate(amount=amount), CHARGE_AT, params)


def test_both_default_branches_consume_exactly_the_legal_budget() -> None:
    """Three retries each, so arm C burns all four attempts and no more.

    Its inefficiency is the finding, not illegality-by-count — that framing only
    holds if both branches really do stop at the cap.
    """
    for amount in (paise(1_200), paise(5_000)):
        schedule = retry_schedule(mandate(amount=amount), CHARGE_AT, DEFAULT_LEGACY)
        assert len(schedule) == MAX_ATTEMPTS - 1
        assert schedule[-1].attempt_number == MAX_ATTEMPTS


def test_retries_are_returned_in_execution_order() -> None:
    """generate.py walks this list until success and stops; out-of-order slots
    would silently mis-assign which attempt got which coin flip."""
    for amount in (paise(1_200), paise(5_000)):
        schedule = retry_schedule(mandate(amount=amount), CHARGE_AT)
        moments = [r.execute_at for r in schedule]
        assert moments == sorted(moments)
        assert [r.attempt_number for r in schedule] == sorted(r.attempt_number for r in schedule)


def test_the_cron_keeps_ist_wall_clock_across_a_month_boundary() -> None:
    """The legacy cron is ``30 11 * * *`` in Asia/Kolkata.

    Reproducing the policy means reproducing its clock, so the hour must not drift
    when the offsets cross into a new month, and must not depend on the tzinfo the
    charge time happens to be expressed in.
    """
    month_end = datetime(2026, 9, 30, 4, 0, tzinfo=IST)
    schedule = retry_schedule(mandate(amount=paise(5_000)), month_end)

    assert [r.execute_at.astimezone(IST).strftime("%Y-%m-%d %H:%M") for r in schedule] == [
        "2026-09-30 11:30",
        "2026-10-01 11:30",
        "2026-10-02 11:30",
    ]


def test_the_schedule_does_not_depend_on_how_the_charge_time_is_expressed() -> None:
    from datetime import UTC

    as_utc = CHARGE_AT.astimezone(UTC)
    assert as_utc.hour != CHARGE_AT.hour  # the two really are different wall clocks

    assert [r.execute_at for r in retry_schedule(mandate(), as_utc)] == [
        r.execute_at for r in retry_schedule(mandate(), CHARGE_AT)
    ]


def test_a_rationale_is_written_for_every_retry() -> None:
    """Arm C has to be able to explain itself in the drill-down drawer too, or the
    comparison against arm D is not like-for-like."""
    for amount in (paise(1_200), paise(5_000)):
        for retry in retry_schedule(mandate(amount=amount), CHARGE_AT):
            assert retry.rationale.startswith("legacy ")
            assert "T+" in retry.rationale


def test_a_censored_mandate_commits_no_violations() -> None:
    """No retries means nothing to violate — including for a high-value netbanking
    mandate, which would otherwise take the urgent branch."""
    assert violations(mandate(method="netbanking", amount=paise(5_000)), CHARGE_AT) == ()


def test_a_charge_late_in_the_day_still_retries_the_next_morning() -> None:
    """The cron fires on a wall clock, so a 23:00 charge is retried at 09:00 the
    following day — not 09:00 twenty-four hours later."""
    late = datetime(2026, 9, 3, 23, 30, tzinfo=IST)
    first = retry_schedule(mandate(), late)[0].execute_at.astimezone(IST)

    assert first.strftime("%Y-%m-%d %H:%M") == "2026-09-04 09:00"
    assert first - late < timedelta(hours=10)

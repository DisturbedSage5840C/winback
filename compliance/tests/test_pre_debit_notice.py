"""RBI's 24-hour pre-debit notification.

The rule itself is one subtraction. What makes it worth a test file is the
asymmetry between a *new* debit and a *retry within the same cycle*: the notice
authorises the cycle, not the individual attempt. Treating a retry as a fresh
debit would block every legitimate recovery this project exists to make; treating
a new debit as covered by an old notice would debit an unwarned customer.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from compliance.non_peak_window import IST
from compliance.pre_debit_notice import (
    NOTICE_LEAD_TIME,
    NOTICE_MISSING,
    NOTICE_TOO_LATE,
    check,
)
from compliance.result import Verdict

CHARGE_AT = datetime(2026, 9, 15, 2, 0, tzinfo=IST)


def before_charge(hours: float) -> datetime:
    return CHARGE_AT - timedelta(hours=hours)


def test_the_lead_time_is_twenty_four_hours() -> None:
    assert NOTICE_LEAD_TIME == timedelta(hours=24)


# ------------------------------------------------------------------- a new debit


def test_a_timely_notice_approves_the_first_attempt() -> None:
    result = check(
        notice_sent_at=before_charge(48), charge_at=CHARGE_AT, attempt_number=1
    )
    assert result.verdict is Verdict.APPROVE
    assert result.stop_reason is None


@pytest.mark.parametrize(
    ("hours_before", "allowed", "why"),
    [
        (72, True, "three days of notice is ample"),
        (24.5, True, "comfortably clear"),
        (24.0, True, "exactly 24h is compliant -- the rule reads 'at least'"),
        (23.99, False, "just inside the deadline is still a breach"),
        (1, False, "an hour's warning is not a pre-debit notification"),
        (0, False, "simultaneous notice warns nobody"),
    ],
)
def test_lead_time_boundary(hours_before: float, allowed: bool, why: str) -> None:
    result = check(
        notice_sent_at=before_charge(hours_before), charge_at=CHARGE_AT, attempt_number=1
    )
    assert result.allowed is allowed, why


def test_a_late_notice_blocks_with_its_own_reason() -> None:
    result = check(
        notice_sent_at=before_charge(6), charge_at=CHARGE_AT, attempt_number=1
    )
    assert result.verdict is Verdict.DENY
    assert result.stop_reason == NOTICE_TOO_LATE
    assert "18" in result.detail or "6" in result.detail


def test_a_missing_notice_blocks_a_new_debit() -> None:
    result = check(notice_sent_at=None, charge_at=CHARGE_AT, attempt_number=1)
    assert result.verdict is Verdict.DENY
    assert result.stop_reason == NOTICE_MISSING


def test_a_notice_sent_after_the_debit_is_not_a_notice() -> None:
    """Backdating aside, this is the shape a bug takes: the notice row is written
    when the debit fails, not before it fires."""
    result = check(
        notice_sent_at=CHARGE_AT + timedelta(hours=1), charge_at=CHARGE_AT, attempt_number=1
    )
    assert result.verdict is Verdict.DENY
    assert result.stop_reason == NOTICE_TOO_LATE


# ------------------------------------------------------------------- a retry


def test_a_retry_is_not_blocked_by_a_missing_notice() -> None:
    """The money case. A retry is the same authorised debit trying again, and the
    notice attaches to the cycle. Blocking here would forfeit every recovery on an
    invoice whose notice row is merely absent from our copy of the data."""
    result = check(notice_sent_at=None, charge_at=CHARGE_AT, attempt_number=2)
    assert result.verdict is Verdict.APPROVE


def test_a_retry_still_carries_the_warning_into_the_audit_row() -> None:
    """Not blocking is not the same as not noticing. The warning has to survive into
    the audit trail, or a systemic gap in notice data becomes invisible."""
    result = check(notice_sent_at=None, charge_at=CHARGE_AT, attempt_number=3)
    assert result.metadata["warning"] == NOTICE_MISSING
    assert "retry" in result.detail


def test_a_retry_with_a_late_notice_warns_too() -> None:
    result = check(
        notice_sent_at=before_charge(2), charge_at=CHARGE_AT, attempt_number=2
    )
    assert result.allowed is True
    assert result.metadata["warning"] == NOTICE_TOO_LATE


def test_a_retry_with_a_good_notice_carries_no_warning() -> None:
    result = check(
        notice_sent_at=before_charge(48), charge_at=CHARGE_AT, attempt_number=2
    )
    assert result.allowed is True
    assert "warning" not in result.metadata


# ------------------------------------------------------------------- input hygiene


@pytest.mark.parametrize("attempt_number", [0, -1])
def test_a_non_positive_attempt_number_raises(attempt_number: int) -> None:
    """Attempt 0 does not exist. Silently treating it as a retry would exempt it
    from the notice rule entirely, which is the most dangerous available default."""
    with pytest.raises(ValueError, match="attempt_number"):
        check(notice_sent_at=None, charge_at=CHARGE_AT, attempt_number=attempt_number)


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        check(
            notice_sent_at=datetime(2026, 9, 13, 2, 0),
            charge_at=CHARGE_AT,
            attempt_number=1,
        )


def test_a_naive_charge_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        check(
            notice_sent_at=before_charge(48),
            charge_at=datetime(2026, 9, 15, 2, 0),
            attempt_number=1,
        )


def test_the_rule_compares_instants_not_wall_clocks() -> None:
    """A notice timestamped in UTC and a charge timestamped in IST describe the same
    timeline. Comparing their wall-clock faces would grant a spurious 5h30m of
    lead time on every single invoice."""
    from datetime import UTC

    # 2026-09-13T20:30Z is 2026-09-14T02:00 IST -- exactly 24h before the charge.
    notice_utc = datetime(2026, 9, 13, 20, 30, tzinfo=UTC)
    assert check(notice_sent_at=notice_utc, charge_at=CHARGE_AT, attempt_number=1).allowed

    # One minute later is a breach, and no timezone arithmetic should rescue it.
    late = notice_utc + timedelta(minutes=1)
    assert not check(notice_sent_at=late, charge_at=CHARGE_AT, attempt_number=1).allowed

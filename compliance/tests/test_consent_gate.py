"""TRAI TCCCPR consent rules for customer contact.

Two separate questions, deliberately two separate functions:

* **May we nudge this customer now?** Requires recorded active consent *and* a live
  7-day transactional window opened by the failed debit itself.
* **May we ask this customer to re-consent?** Not within 90 days of a withdrawal.

Collapsing those into one boolean is the mistake worth guarding against: a system that
treats "cannot message" and "cannot ask again" as the same state will either spam a
customer who just opted out, or write off a customer whose window merely lapsed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from compliance.consent_gate import (
    DND_COOLOFF_DAYS,
    TRANSACTIONAL_WINDOW_DAYS,
    check_nudge,
    may_request_reconsent,
)
from compliance.non_peak_window import IST
from compliance.result import Verdict

NOW = datetime(2026, 9, 15, 14, 0, tzinfo=IST)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def test_the_windows_are_seven_and_ninety_days() -> None:
    assert TRANSACTIONAL_WINDOW_DAYS == 7
    assert DND_COOLOFF_DAYS == 90


# --------------------------------------------------------------------- the nudge gate


def test_active_consent_inside_the_window_is_approved() -> None:
    result = check_nudge(
        consent_status="active",
        consent_updated_at=days_ago(200),
        last_transaction_at=days_ago(1),
        now=NOW,
    )
    assert result.verdict is Verdict.APPROVE
    assert "6 days" in result.detail or "6d" in result.detail


def test_withdrawn_consent_blocks_every_channel() -> None:
    result = check_nudge(
        consent_status="withdrawn",
        consent_updated_at=days_ago(10),
        last_transaction_at=days_ago(1),
        now=NOW,
    )
    assert result.verdict is Verdict.DENY
    assert result.stop_reason == "consent_withdrawn"


def test_dnd_registration_blocks_the_nudge() -> None:
    result = check_nudge(
        consent_status="dnd",
        consent_updated_at=days_ago(400),
        last_transaction_at=days_ago(1),
        now=NOW,
    )
    assert result.verdict is Verdict.DENY
    assert result.stop_reason == "dnd_registered"


def test_a_large_amount_at_risk_changes_nothing() -> None:
    """The temptation case, made explicit.

    ``check_nudge`` cannot see the amount, so it cannot be tempted by it. Asserted on
    the signature so that a later 'just for high-value customers' parameter fails a
    test rather than shipping.
    """
    import inspect

    parameters = set(inspect.signature(check_nudge).parameters)
    assert parameters == {
        "consent_status",
        "consent_updated_at",
        "last_transaction_at",
        "now",
    }


# --------------------------------------------------------------------- 7-day window


@pytest.mark.parametrize(
    ("days_since_transaction", "allowed"),
    [
        (0, True),
        (1, True),
        (6.9, True),
        (7.0, True),      # exactly at the edge: still inside
        (7.1, False),     # past it
        (30, False),
    ],
)
def test_transactional_window_boundary(days_since_transaction: float, allowed: bool) -> None:
    result = check_nudge(
        consent_status="active",
        consent_updated_at=days_ago(200),
        last_transaction_at=days_ago(days_since_transaction),
        now=NOW,
    )
    assert result.allowed is allowed


def test_an_expired_window_denies_with_its_own_reason() -> None:
    """Distinct from a consent problem: this customer never objected, the clock ran
    out. The audit trail has to say which, because the remedies differ."""
    result = check_nudge(
        consent_status="active",
        consent_updated_at=days_ago(200),
        last_transaction_at=days_ago(20),
        now=NOW,
    )
    assert result.stop_reason == "transactional_window_expired"


def test_no_transaction_on_record_denies() -> None:
    """No triggering transaction means no transactional basis to message at all."""
    result = check_nudge(
        consent_status="active",
        consent_updated_at=days_ago(200),
        last_transaction_at=None,
        now=NOW,
    )
    assert result.stop_reason == "no_transactional_basis"


def test_a_future_transaction_is_refused_as_a_data_error() -> None:
    """A transaction timestamped after 'now' is a clock or ingest bug. Treating it as
    a freshly-opened window would be the most permissive possible reading of a bug."""
    with pytest.raises(ValueError, match="future"):
        check_nudge(
            consent_status="active",
            consent_updated_at=days_ago(200),
            last_transaction_at=NOW + timedelta(hours=1),
            now=NOW,
        )


def test_an_unknown_consent_status_raises() -> None:
    """There is no safe default here. An unrecognised status means the consent record
    is not understood, and guessing either way is worse than stopping."""
    with pytest.raises(ValueError, match="unknown consent status"):
        check_nudge(
            consent_status="probably_fine",
            consent_updated_at=days_ago(10),
            last_transaction_at=days_ago(1),
            now=NOW,
        )


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        check_nudge(
            consent_status="active",
            consent_updated_at=datetime(2026, 3, 1),
            last_transaction_at=days_ago(1),
            now=NOW,
        )


# --------------------------------------------------------------------- 90-day cooloff


@pytest.mark.parametrize(
    ("days_since_withdrawal", "may_ask"),
    [
        (0, False),
        (45, False),
        (89.9, False),
        (90.1, True),
    ],
)
def test_reconsent_cooloff_boundary(days_since_withdrawal: float, may_ask: bool) -> None:
    assert (
        may_request_reconsent(
            consent_status="withdrawn",
            consent_updated_at=days_ago(days_since_withdrawal),
            now=NOW,
        )
        is may_ask
    )


def test_an_active_customer_may_always_be_asked() -> None:
    """The cooloff attaches to a withdrawal, not to everybody."""
    assert (
        may_request_reconsent(
            consent_status="active", consent_updated_at=days_ago(1), now=NOW
        )
        is True
    )


def test_the_cooloff_and_the_nudge_gate_are_independent() -> None:
    """Ninety-one days after withdrawing, the customer may be *asked* to re-consent —
    but must still not be nudged until they actually say yes."""
    withdrawn_long_ago = days_ago(91)
    assert may_request_reconsent(
        consent_status="withdrawn", consent_updated_at=withdrawn_long_ago, now=NOW
    )
    assert (
        check_nudge(
            consent_status="withdrawn",
            consent_updated_at=withdrawn_long_ago,
            last_transaction_at=days_ago(1),
            now=NOW,
        ).allowed
        is False
    )

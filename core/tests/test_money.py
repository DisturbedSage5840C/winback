"""Rupee formatting, in the Indian digit grouping.

Money is stored in paise as integers everywhere in this system; this is the one place
it becomes a string. Worth testing properly because ``₹1,00,000`` and ``₹100,000``
are the same number and only one of them reads as written by someone who has seen an
Indian bank statement — and the AFA ceiling this system enforces is *stated* as one
lakh, so the grouping and the rule line up.
"""

from __future__ import annotations

import pytest

from core.money import format_rupees, paise, rupees


def test_small_amounts() -> None:
    assert format_rupees(0) == "₹0"
    assert format_rupees(100) == "₹1"
    assert format_rupees(49_900) == "₹499"
    assert format_rupees(99_900) == "₹999"


def test_thousands_group_in_threes() -> None:
    assert format_rupees(100_000) == "₹1,000"
    assert format_rupees(1_499_900) == "₹14,999"
    assert format_rupees(1_500_000) == "₹15,000"


def test_above_a_lakh_groups_in_twos() -> None:
    """The rule that distinguishes Indian grouping: three digits, then pairs."""
    assert format_rupees(10_000_000) == "₹1,00,000"
    assert format_rupees(10_000_100) == "₹1,00,001"
    assert format_rupees(123_456_700) == "₹12,34,567"


def test_crore() -> None:
    assert format_rupees(1_000_000_000) == "₹1,00,00,000"
    assert format_rupees(1_234_567_800) == "₹1,23,45,678"


def test_paise_are_shown_only_when_they_exist() -> None:
    """A recovered total of ₹4,999.50 must not silently render as ₹4,999."""
    assert format_rupees(499_950) == "₹4,999.50"
    assert format_rupees(499_901) == "₹4,999.01"
    assert format_rupees(499_900) == "₹4,999"


def test_negative_amounts_keep_the_sign_outside_the_symbol() -> None:
    assert format_rupees(-1_500_000) == "-₹15,000"


def test_conversion_helpers_round_trip() -> None:
    assert paise(15_000) == 1_500_000
    assert rupees(1_500_000) == 15_000


def test_rupees_returns_an_exact_decimal_not_a_float() -> None:
    """A float rupee value is how a total drifts by a paisa per row and then by a
    rupee across a batch. The conversion is exact or it is not offered."""
    from decimal import Decimal

    assert rupees(499_950) == Decimal("4999.50")
    assert isinstance(rupees(499_950), Decimal)


def test_paise_refuses_a_fractional_input() -> None:
    with pytest.raises(ValueError, match="whole paise"):
        paise(15_000.005)

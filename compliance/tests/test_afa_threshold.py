"""RBI e-mandate AFA thresholds.

A recurring debit executes without per-cycle Additional Factor of Authentication up to
₹15,000 — or up to ₹1,00,000 where the mandate is for an insurance premium, a mutual
fund SIP, or a credit-card bill. Above the applicable ceiling, Winback escalates to a
human and never auto-debits.

The interesting tests here are the exact boundaries and the unknown-category default.
An unknown MCC that quietly inherits the *elevated* ceiling would let a ₹90,000 debit
through on a category nobody has classified, which is the failure mode this file
exists to prevent.
"""

from __future__ import annotations

import pytest

from compliance.afa_threshold import (
    ELEVATED_CEILING_PAISE,
    ELEVATED_MCC_CATEGORIES,
    STANDARD_CEILING_PAISE,
    ceiling_for,
    check,
)
from compliance.result import Verdict
from core.money import paise


def test_the_ceilings_are_fifteen_thousand_and_one_lakh() -> None:
    assert STANDARD_CEILING_PAISE == paise(15_000)
    assert ELEVATED_CEILING_PAISE == paise(1_00_000)


# --------------------------------------------------------------------- boundaries


@pytest.mark.parametrize(
    ("rupee_amount", "allowed"),
    [
        (1, True),
        (14_999, True),
        (15_000, True),      # at the ceiling: permitted
        (15_001, False),     # one rupee over: escalates
        (20_000, False),
    ],
)
def test_standard_ceiling_boundary(rupee_amount: int, allowed: bool) -> None:
    result = check(amount_paise=paise(rupee_amount), mcc_category="saas")
    assert result.allowed is allowed


@pytest.mark.parametrize(
    ("rupee_amount", "allowed"),
    [
        (15_000, True),
        (15_001, True),      # over the standard ceiling, well under the elevated one
        (99_999, True),
        (1_00_000, True),    # at the elevated ceiling: permitted
        (1_00_001, False),   # one rupee over: escalates
    ],
)
def test_elevated_ceiling_boundary(rupee_amount: int, allowed: bool) -> None:
    result = check(amount_paise=paise(rupee_amount), mcc_category="mutual_fund_sip")
    assert result.allowed is allowed


def test_the_same_amount_resolves_differently_by_category() -> None:
    """₹20,000 is the amount that makes the rule visible: an escalation for SaaS, a
    routine debit for an SIP."""
    amount = paise(20_000)
    assert check(amount_paise=amount, mcc_category="saas").allowed is False
    assert check(amount_paise=amount, mcc_category="mutual_fund_sip").allowed is True


def test_a_breach_escalates_rather_than_denying() -> None:
    """A large debit is not illegal, it merely is not the agent's call. Denying it
    outright would write off recoverable revenue that a human could authorise."""
    result = check(amount_paise=paise(50_000), mcc_category="saas")
    assert result.verdict is Verdict.ESCALATE_HUMAN
    assert result.stop_reason == "above_afa_ceiling"


# --------------------------------------------------------------------- categories


def test_the_elevated_categories_are_the_three_rbi_names() -> None:
    assert ELEVATED_MCC_CATEGORIES == frozenset(
        {"insurance", "mutual_fund_sip", "credit_card_bill"}
    )


@pytest.mark.parametrize("category", sorted(ELEVATED_MCC_CATEGORIES))
def test_every_elevated_category_gets_the_elevated_ceiling(category: str) -> None:
    assert ceiling_for(category) == ELEVATED_CEILING_PAISE


def test_an_unknown_category_gets_the_stricter_ceiling() -> None:
    """Defaulting to the *lower* limit is the whole point. A new MCC must not inherit
    a one-lakh auto-debit allowance because nobody has classified it yet."""
    assert ceiling_for("something_nobody_has_seen") == STANDARD_CEILING_PAISE
    assert check(amount_paise=paise(90_000), mcc_category="something_new").allowed is False


def test_category_matching_is_case_insensitive_and_trimmed() -> None:
    """Category strings arrive from a data pipeline, and ' Insurance' failing open to
    the strict ceiling would be a confusing false escalation rather than a breach —
    but it would still be wrong."""
    assert ceiling_for("  Insurance ") == ELEVATED_CEILING_PAISE
    assert ceiling_for("MUTUAL_FUND_SIP") == ELEVATED_CEILING_PAISE


# --------------------------------------------------------------------- detail & input


def test_the_detail_names_the_amount_and_the_ceiling_in_rupees() -> None:
    """Rendered onto the escalation chip, where a reviewer needs both numbers to
    decide in one glance — in the grouping the ceiling is written in."""
    detail = check(amount_paise=paise(1_50_000), mcc_category="saas").detail
    assert "₹1,50,000" in detail
    assert "₹15,000" in detail


def test_an_approval_names_the_headroom() -> None:
    detail = check(amount_paise=paise(499), mcc_category="saas").detail
    assert "₹499" in detail
    assert "₹15,000" in detail


@pytest.mark.parametrize("bad_amount", [0, -1, -paise(500)])
def test_a_non_positive_amount_is_refused(bad_amount: int) -> None:
    """A zero-rupee debit is not a compliance question, it is a data bug, and it must
    not be answered with a cheerful approval."""
    with pytest.raises(ValueError, match="positive"):
        check(amount_paise=bad_amount, mcc_category="saas")

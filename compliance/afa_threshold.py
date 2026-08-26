"""RBI e-mandate Additional Factor of Authentication thresholds.

Source: RBI's e-mandate framework for recurring transactions, as amended through 2026.
A recurring debit executes without per-cycle AFA up to ₹15,000; the limit is
₹1,00,000 for insurance premiums, mutual-fund SIPs and credit-card bill payments.

Above the applicable ceiling the verdict is ``ESCALATE_HUMAN``, not ``DENY``. A large
debit is not illegal — it simply is not the agent's call. Denying it outright would
write off revenue a human could have authorised in thirty seconds, which is the wrong
kind of caution: it costs money and buys no compliance.
"""

from __future__ import annotations

from compliance.result import RuleResult, Verdict
from core.money import format_rupees, paise

RULE = "afa_threshold"

STANDARD_CEILING_PAISE = paise(15_000)
ELEVATED_CEILING_PAISE = paise(1_00_000)

#: The categories RBI grants the higher ceiling. Anything not named here gets the
#: standard limit — see ``ceiling_for``.
ELEVATED_MCC_CATEGORIES = frozenset({"insurance", "mutual_fund_sip", "credit_card_bill"})

ABOVE_CEILING = "above_afa_ceiling"


def ceiling_for(mcc_category: str) -> int:
    """The AFA ceiling, in paise, for a merchant category.

    An unrecognised category gets the **stricter** ceiling. Failing to the lower limit
    means a category nobody has classified yet cannot inherit a one-lakh auto-debit
    allowance by omission; the cost of the mistake is a false escalation, which a
    human resolves, rather than an unauthorised debit, which they cannot undo.
    """
    normalised = mcc_category.strip().lower()
    if normalised in ELEVATED_MCC_CATEGORIES:
        return ELEVATED_CEILING_PAISE
    return STANDARD_CEILING_PAISE


def check(amount_paise: int, mcc_category: str) -> RuleResult:
    """Whether this debit may execute without per-cycle authentication."""
    if amount_paise <= 0:
        raise ValueError(
            f"amount_paise must be positive (got {amount_paise}). A zero-value debit "
            "is a data bug, and answering it with an approval would bury the bug."
        )

    ceiling = ceiling_for(mcc_category)
    amount_text, ceiling_text = format_rupees(amount_paise), format_rupees(ceiling)
    metadata = {
        "amount_paise": amount_paise,
        "ceiling_paise": ceiling,
        "mcc_category": mcc_category,
    }

    if amount_paise > ceiling:
        return RuleResult(
            rule=RULE,
            verdict=Verdict.ESCALATE_HUMAN,
            detail=(
                f"{RULE}: {amount_text} exceeds the {ceiling_text} e-mandate ceiling "
                f"for {mcc_category}; human authorisation required"
            ),
            stop_reason=ABOVE_CEILING,
            metadata=metadata,
        )

    return RuleResult(
        rule=RULE,
        verdict=Verdict.APPROVE,
        detail=f"{RULE}: {amount_text} within the {ceiling_text} ceiling for {mcc_category}",
        metadata=metadata,
    )

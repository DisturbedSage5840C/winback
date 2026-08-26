"""Money. Paise in, strings out.

Every rupee value in Winback is an integer number of paise. No float touches money at
any point — not in the database, not in the policy's expected-value arithmetic, not in
the evaluation totals. This module is the only place a paise integer becomes something
a person reads.

Formatting uses the Indian grouping (three digits, then pairs) because the rule this
system enforces is *stated* in lakhs: the AFA ceiling is one lakh, and ``₹1,00,000``
is the form in which that ceiling is recognisable.
"""

from __future__ import annotations

from decimal import Decimal

PAISE_PER_RUPEE = 100


def paise(rupee_amount: int | float | Decimal) -> int:
    """Rupees to paise, refusing anything that is not a whole number of paise."""
    exact = Decimal(str(rupee_amount)) * PAISE_PER_RUPEE
    if exact != exact.to_integral_value():
        raise ValueError(
            f"{rupee_amount} is not a whole paise amount ({exact}). Rounding money "
            "silently is how a batch total drifts."
        )
    return int(exact)


def rupees(paise_amount: int) -> Decimal:
    """Paise to an exact decimal rupee value. Never a float."""
    return Decimal(paise_amount) / PAISE_PER_RUPEE


def _group_indian(digits: str) -> str:
    """Three digits, then pairs: 1234567 -> 12,34,567."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while len(head) > 2:
        head, pair = head[:-2], head[-2:]
        pairs.insert(0, pair)
    return ",".join([head, *pairs, tail])


def format_rupees(paise_amount: int) -> str:
    """A paise integer as a readable rupee string.

    Paise are shown only when they are non-zero: a recovered total of ₹4,999.50 must
    not render as ₹4,999, and a clean ₹15,000 must not render as ₹15,000.00 and read
    like a spreadsheet export.
    """
    sign = "-" if paise_amount < 0 else ""
    whole, remainder = divmod(abs(paise_amount), PAISE_PER_RUPEE)
    formatted = f"{sign}₹{_group_indian(str(whole))}"
    return formatted if remainder == 0 else f"{formatted}.{remainder:02d}"

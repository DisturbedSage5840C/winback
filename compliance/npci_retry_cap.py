"""NPCI retry cap: one attempt plus a maximum of three retries, per invoice.

Source: NPCI circular OC-215-A, effective 1 August 2025.

``check`` takes exactly one argument — the number of attempts already spent on this
invoice. It takes no probability, no expected value, no urgency and no override,
because a legal limit that can see the model's confidence is a legal limit that can be
argued with. The absence of those parameters is asserted structurally in
``test_npci_retry_cap.py``, so adding one later fails a test rather than quietly
becoming policy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from compliance.result import RuleResult, Verdict

RULE = "npci_1_plus_3"

#: One execution attempt plus three retries. Scoped to (subscription, invoice) —
#: a new billing cycle restores the full budget.
MAX_ATTEMPTS_PER_INVOICE = 4

CAP_EXHAUSTED = "npci_1_plus_3_cap_exhausted"


def _validate(attempts_used: int) -> None:
    if attempts_used < 0:
        raise ValueError(
            f"attempts_used cannot be negative (got {attempts_used}). Clamping this "
            "would hide an upstream counting error behind a permissive answer."
        )


def attempts_remaining(attempts_used: int) -> int:
    """Legal attempts still available for this invoice, floored at zero.

    Floored because this number is rendered on the dashboard, and a negative budget
    reads as a bug that costs the viewer their trust in every other figure on the page.
    """
    _validate(attempts_used)
    return max(0, MAX_ATTEMPTS_PER_INVOICE - attempts_used)


def check(attempts_used: int) -> RuleResult:
    """Whether NPCI permits one more execution attempt on this invoice."""
    _validate(attempts_used)
    next_attempt = attempts_used + 1

    if attempts_used >= MAX_ATTEMPTS_PER_INVOICE:
        return RuleResult(
            rule=RULE,
            verdict=Verdict.DENY,
            detail=(
                f"{RULE}: attempt {next_attempt} refused, "
                f"budget of {MAX_ATTEMPTS_PER_INVOICE} exhausted for this invoice"
            ),
            stop_reason=CAP_EXHAUSTED,
            metadata={"attempts_used": attempts_used, "attempts_remaining": 0},
        )

    return RuleResult(
        rule=RULE,
        verdict=Verdict.APPROVE,
        detail=f"{RULE}: attempt {next_attempt}/{MAX_ATTEMPTS_PER_INVOICE} permitted",
        metadata={
            "attempts_used": attempts_used,
            "attempts_remaining": attempts_remaining(attempts_used),
        },
    )


def attempts_used_for_invoice(
    attempts: Iterable[Mapping[str, Any]],
    invoice_id: str,
    run_id: str | None = None,
) -> int:
    """Count the attempts that consume this invoice's budget.

    ``run_id`` scopes the count to one evaluation arm: observational history
    (``run_id IS NULL``) always counts, plus whatever the named run has already spent.
    Without that split, every arm after the first would inherit the others' attempts
    and look artificially constrained — which would invalidate the four-arm comparison
    the whole submission rests on.

    A row without an ``invoice_id`` raises rather than being skipped: an uncounted
    attempt is an over-budget retry waiting to happen.
    """
    return sum(
        1
        for attempt in attempts
        if attempt["invoice_id"] == invoice_id
        and attempt.get("run_id") in (None, run_id)
    )

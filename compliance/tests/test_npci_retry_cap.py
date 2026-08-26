"""NPCI OC-215-A: one attempt plus a maximum of three retries, per invoice.

The single most important test in this repository is `test_the_cap_holds_against_a
_confident_model`. Everything else Winback claims rests on the retry budget being
uninfluenceable — if a high enough predicted probability can buy a fifth attempt, the
system is a dunning bot with a compliance-themed dashboard.
"""

from __future__ import annotations

import inspect

import pytest

from compliance.npci_retry_cap import (
    MAX_ATTEMPTS_PER_INVOICE,
    attempts_remaining,
    attempts_used_for_invoice,
    check,
)
from compliance.result import Verdict


def test_the_cap_is_four() -> None:
    """One execution attempt plus three retries. Stated as a constant so the number
    has exactly one home."""
    assert MAX_ATTEMPTS_PER_INVOICE == 4


@pytest.mark.parametrize("used", [0, 1, 2, 3])
def test_attempts_one_through_four_are_permitted(used: int) -> None:
    result = check(attempts_used=used)
    assert result.verdict is Verdict.APPROVE
    assert result.allowed is True
    assert result.stop_reason is None
    assert f"attempt {used + 1}/4" in result.detail


def test_the_fifth_attempt_is_refused() -> None:
    result = check(attempts_used=4)
    assert result.verdict is Verdict.DENY
    assert result.allowed is False
    assert result.stop_reason == "npci_1_plus_3_cap_exhausted"


@pytest.mark.parametrize("used", [5, 6, 40])
def test_anything_beyond_the_cap_stays_refused(used: int) -> None:
    """Over-budget should not wrap, saturate oddly, or start approving again."""
    assert check(attempts_used=used).stop_reason == "npci_1_plus_3_cap_exhausted"


def test_the_cap_holds_against_a_confident_model() -> None:
    """The load-bearing test.

    ``check`` takes no probability, no score, no confidence and no override. It
    *cannot* be influenced by the model, because it cannot see it -- which is a
    stronger guarantee than a rule that merely chooses to ignore one. Asserted
    structurally so that adding such a parameter later fails here rather than
    silently in production.
    """
    parameters = set(inspect.signature(check).parameters)
    forbidden = {"probability", "prob", "p", "score", "confidence", "expected_value",
                 "override", "force", "model", "urgency", "amount_paise"}
    assert not (parameters & forbidden), (
        f"the retry cap must not be able to see {parameters & forbidden}: a legal "
        "limit that takes a probability is a limit that can be argued with"
    )
    assert parameters == {"attempts_used"}


def test_the_detail_string_is_written_for_a_human_reviewer() -> None:
    """It is rendered verbatim into decisions.authorizing_rule and onto the dashboard
    chip, so it has to read as a sentence rather than as a log line."""
    assert check(attempts_used=1).detail == "npci_1_plus_3: attempt 2/4 permitted"
    assert check(attempts_used=4).detail == (
        "npci_1_plus_3: attempt 5 refused, budget of 4 exhausted for this invoice"
    )


# --------------------------------------------------------------------- budget maths


@pytest.mark.parametrize(("used", "left"), [(0, 4), (1, 3), (3, 1), (4, 0), (9, 0)])
def test_attempts_remaining(used: int, left: int) -> None:
    assert attempts_remaining(used) == left


def test_remaining_never_goes_negative() -> None:
    """A negative budget rendered on a dashboard reads as a bug and destroys trust in
    every other number on the page."""
    assert attempts_remaining(100) == 0


def test_a_negative_count_is_refused() -> None:
    """Silently clamping a negative would mask a real upstream counting error."""
    with pytest.raises(ValueError, match="cannot be negative"):
        check(attempts_used=-1)
    with pytest.raises(ValueError, match="cannot be negative"):
        attempts_remaining(-1)


# --------------------------------------------------------------------- scoping


def _attempt(invoice_id: str, number: int) -> dict[str, object]:
    return {"invoice_id": invoice_id, "attempt_number": number, "outcome": "failed"}


def test_the_budget_is_scoped_to_one_invoice() -> None:
    """The cap is per mandate *per invoice*, not per subscription. Counting across
    cycles would strangle a healthy long-lived subscription after four lifetime
    failures."""
    history = [
        _attempt("inv_jan", 1),
        _attempt("inv_jan", 2),
        _attempt("inv_feb", 1),
    ]
    assert attempts_used_for_invoice(history, "inv_jan") == 2
    assert attempts_used_for_invoice(history, "inv_feb") == 1


def test_a_new_billing_cycle_restores_the_full_budget() -> None:
    exhausted = [_attempt("inv_jan", n) for n in range(1, 5)]
    assert check(attempts_used=attempts_used_for_invoice(exhausted, "inv_jan")).allowed is False
    assert check(attempts_used=attempts_used_for_invoice(exhausted, "inv_feb")).allowed is True


def test_an_invoice_with_no_history_has_used_nothing() -> None:
    assert attempts_used_for_invoice([], "inv_new") == 0


def test_successful_attempts_still_consume_budget() -> None:
    """NPCI caps *executions*, not failures. A capture consumed a slot too -- though
    in practice a captured invoice is no longer at risk and never reaches the cap."""
    history = [
        {"invoice_id": "inv_1", "attempt_number": 1, "outcome": "failed"},
        {"invoice_id": "inv_1", "attempt_number": 2, "outcome": "captured"},
    ]
    assert attempts_used_for_invoice(history, "inv_1") == 2


def test_counterfactual_attempts_from_other_runs_are_excluded() -> None:
    """Evaluation arms write attempts into the same table under a run_id. Counting
    another arm's attempts against this invoice would make every arm after the first
    look artificially constrained -- and would quietly invalidate the comparison the
    whole submission rests on."""
    history = [
        {"invoice_id": "inv_1", "attempt_number": 1, "outcome": "failed", "run_id": None},
        {"invoice_id": "inv_1", "attempt_number": 2, "outcome": "failed", "run_id": "run_b"},
        {"invoice_id": "inv_1", "attempt_number": 3, "outcome": "failed", "run_id": "run_d"},
    ]
    assert attempts_used_for_invoice(history, "inv_1", run_id=None) == 1
    assert attempts_used_for_invoice(history, "inv_1", run_id="run_d") == 2
    assert attempts_used_for_invoice(history, "inv_1", run_id="run_b") == 2


def test_history_missing_an_invoice_id_is_refused() -> None:
    """A row that cannot be attributed to an invoice must not be silently skipped:
    an uncounted attempt is an over-budget retry waiting to happen."""
    with pytest.raises(KeyError):
        attempts_used_for_invoice([{"attempt_number": 1}], "inv_1")

"""Deterministic failure classification: TD / BD_transient / BD_hard.

This is the module most likely to be handed to an LLM by someone in a hurry, and
the one where that would be most clearly wrong. Razorpay already tells you the
``source``, ``step`` and ``reason`` of a failure. Turning four known strings into
one of three known strings is a lookup. A model here would add latency, cost,
non-determinism and an unauditable step, in exchange for nothing.

The class it produces is a *feature* fed to the ML model and a *precondition* for
the policy — a BD_hard mandate must never be retried, however confident anything
downstream is. So the table is the contract, and an unmapped combination raises.
"""

from __future__ import annotations

import inspect

import pytest

from compliance.root_cause import (
    ERROR_CODES,
    ERROR_SOURCES,
    ERROR_STEPS,
    RETRYABLE_CLASSES,
    RootCause,
    classify,
    known_combinations,
)


def test_the_three_classes_match_the_database_check_constraint() -> None:
    """These strings are written into payment_attempts.root_cause_class, which has a
    CHECK constraint. A rename here without a migration is a runtime failure at the
    only moment that matters -- mid-batch, on a write."""
    assert {c.value for c in RootCause} == {"TD", "BD_transient", "BD_hard"}


def test_only_a_hard_decline_is_unretryable() -> None:
    assert RETRYABLE_CLASSES == frozenset({RootCause.TD, RootCause.BD_TRANSIENT})
    assert RootCause.BD_HARD not in RETRYABLE_CLASSES


# ------------------------------------------------------------------- the lookup


TECHNICAL = [
    ("GATEWAY_ERROR", "bank", "payment_authorization", "issuer_down"),
    ("GATEWAY_ERROR", "gateway", "payment_authorization", "gateway_technical_error"),
    ("GATEWAY_ERROR", "bank", "payment_authorization", "payment_timed_out"),
    ("GATEWAY_ERROR", "network", "payment_authorization", "network_error"),
    ("SERVER_ERROR", "internal", "payment_authorization", "server_error"),
    ("GATEWAY_ERROR", "bank", "payment_initiation", "npci_unavailable"),
]

TRANSIENT = [
    ("BAD_REQUEST_ERROR", "customer", "payment_authorization", "insufficient_funds"),
    ("BAD_REQUEST_ERROR", "customer", "payment_authorization", "limit_exceeded"),
    ("BAD_REQUEST_ERROR", "customer", "payment_authentication", "authentication_failed"),
    ("BAD_REQUEST_ERROR", "bank", "payment_authorization", "debit_not_permitted"),
]

HARD = [
    ("BAD_REQUEST_ERROR", "customer", "payment_initiation", "mandate_revoked"),
    ("BAD_REQUEST_ERROR", "customer", "payment_initiation", "invalid_vpa"),
    ("BAD_REQUEST_ERROR", "customer", "payment_authorization", "account_closed"),
    ("BAD_REQUEST_ERROR", "customer", "payment_initiation", "card_expired"),
    ("BAD_REQUEST_ERROR", "bank", "payment_authorization", "account_blocked"),
    ("BAD_REQUEST_ERROR", "bank", "payment_initiation", "mandate_not_found"),
]


@pytest.mark.parametrize(("code", "source", "step", "reason"), TECHNICAL)
def test_technical_declines(code: str, source: str, step: str, reason: str) -> None:
    assert classify(code, source, step, reason) is RootCause.TD


@pytest.mark.parametrize(("code", "source", "step", "reason"), TRANSIENT)
def test_transient_business_declines(code: str, source: str, step: str, reason: str) -> None:
    assert classify(code, source, step, reason) is RootCause.BD_TRANSIENT


@pytest.mark.parametrize(("code", "source", "step", "reason"), HARD)
def test_hard_business_declines(code: str, source: str, step: str, reason: str) -> None:
    assert classify(code, source, step, reason) is RootCause.BD_HARD


def test_insufficient_funds_is_transient_not_hard() -> None:
    """The single most consequential row in the table. India's UPI Autopay failures
    are dominated by low balances, and they clear on payday. Classifying this as
    hard would write off the largest recoverable segment there is."""
    assert (
        classify("BAD_REQUEST_ERROR", "customer", "payment_authorization", "insufficient_funds")
        is RootCause.BD_TRANSIENT
    )


def test_a_revoked_mandate_is_hard_not_transient() -> None:
    """The mirror-image error. Retrying a revoked mandate burns legal attempts against
    an authorisation that no longer exists; the correct action is re-registration."""
    assert (
        classify("BAD_REQUEST_ERROR", "customer", "payment_initiation", "mandate_revoked")
        is RootCause.BD_HARD
    )


# ------------------------------------------------------------------- no defaults


def test_an_unmapped_combination_raises() -> None:
    """The rule with teeth. A default -- any default -- silently mislabels a failure
    mode nobody has looked at, and that label then flows into a training feature and
    a retry decision. Loud beats plausible."""
    with pytest.raises(KeyError, match="unmapped"):
        classify("BAD_REQUEST_ERROR", "customer", "payment_authorization", "cosmic_rays")


def test_an_unmapped_combination_names_what_it_saw() -> None:
    """The exception is the first thing a human reads when the simulator emits a new
    reason. It has to carry the tuple, not just complain."""
    with pytest.raises(KeyError) as excinfo:
        classify("BAD_REQUEST_ERROR", "customer", "payment_authorization", "cosmic_rays")
    assert "cosmic_rays" in str(excinfo.value)
    assert "customer" in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("code", "TOTALLY_FINE_ERROR"),
        ("source", "vibes"),
        ("step", "payment_vibes"),
    ],
)
def test_an_unknown_vocabulary_value_raises(field: str, bad_value: str) -> None:
    """Validating code and step even though the class is decided by (source, reason):
    garbage in any field means the ingest mapping is wrong, and a lookup that happens
    to succeed anyway would hide it."""
    args = {
        "error_code": "BAD_REQUEST_ERROR",
        "error_source": "customer",
        "error_step": "payment_authorization",
        "error_reason": "insufficient_funds",
    }
    args[f"error_{field}"] = bad_value
    with pytest.raises(ValueError, match=field):
        classify(**args)


def test_classification_ignores_amount_and_confidence() -> None:
    """A signature check, as with the retry cap. The class of a failure is a property
    of the failure, not of how much money is riding on it."""
    parameters = set(inspect.signature(classify).parameters)
    assert parameters == {"error_code", "error_source", "error_step", "error_reason"}


# ------------------------------------------------------------------- the table itself


def test_every_known_combination_classifies() -> None:
    """Walks the published table end to end, so a row added without a class -- or with
    a source or step outside the vocabulary -- fails here rather than mid-batch."""
    combinations = known_combinations()
    assert len(combinations) >= 15, "the table is thinner than the dataset it must cover"
    for source, reason in combinations:
        assert source in ERROR_SOURCES, f"{source!r} is outside the source vocabulary"
        result = classify("BAD_REQUEST_ERROR", source, "payment_authorization", reason)
        assert isinstance(result, RootCause)


def test_the_test_fixtures_cover_the_whole_table() -> None:
    """Guards the tests themselves: a row added to the module without a case here
    would otherwise be asserted on by nothing."""
    covered = {(source, reason) for _, source, _, reason in TECHNICAL + TRANSIENT + HARD}
    assert covered == known_combinations()


def test_the_vocabularies_are_not_empty() -> None:
    assert ERROR_CODES and ERROR_SOURCES and ERROR_STEPS


def test_classification_is_deterministic() -> None:
    """Stated as a test because the whole argument for not using a model here is that
    the same input gives the same answer every time, including across processes."""
    args = ("BAD_REQUEST_ERROR", "customer", "payment_authorization", "insufficient_funds")
    assert len({classify(*args) for _ in range(100)}) == 1


def test_reason_and_source_are_normalised() -> None:
    """Ingest data arrives with inconsistent casing and stray whitespace. Normalising
    is safe here in a way that defaulting is not: it changes nothing about which row
    is matched, only whether a row is matched at all."""
    assert (
        classify("BAD_REQUEST_ERROR", " Customer ", "payment_authorization", "INSUFFICIENT_FUNDS")
        is RootCause.BD_TRANSIENT
    )

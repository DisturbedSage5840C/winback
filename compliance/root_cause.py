"""Deterministic classification of a payment failure into TD / BD_transient / BD_hard.

Razorpay returns four fields on a failed payment — ``error_code``,
``error_source``, ``error_step`` and ``error_reason``. NPCI's own taxonomy splits
declines into **technical** (the bank, gateway or network could not process the
request) and **business** (the customer's account said no). Roughly 18% of
subscription failures are technical and 82% business.

Winback splits the business half again, because the two halves demand opposite
actions:

``TD``
    Technical decline. Nothing is wrong with the mandate or the account. A retry
    in a quieter window is the correct response and usually succeeds.
``BD_transient``
    The customer's account said no *for now* — most often an empty balance. The
    correct response is a retry timed to when the account is likely funded, which
    in India means the days after payday.
``BD_hard``
    The customer's account said no *permanently* — the mandate is revoked, the
    VPA is dead, the card has expired. Retrying burns legal attempts against an
    authorisation that no longer exists. The correct response is re-registration
    or escalation, and it is never a retry.

**This is a lookup table, not a model, and that is a deliberate choice.** Razorpay
has already done the classification work upstream; turning four known strings into
one of three known strings by calling an LLM would add latency, cost,
non-determinism and an unauditable step in exchange for nothing. The class it
produces feeds an ML feature *and* gates a retry decision, so it has to be the
same answer in training, in the batch run, and in a replayed audit trail.

**An unmapped combination raises.** There is no default. A default would silently
mislabel a failure mode nobody has looked at, and that label would then flow into
a training feature and a money-moving decision. A KeyError mid-batch is cheap; a
BD_hard mandate quietly labelled retryable is not.
"""

from __future__ import annotations

from enum import StrEnum


class RootCause(StrEnum):
    """Values written verbatim into ``payment_attempts.root_cause_class``."""

    TD = "TD"
    BD_TRANSIENT = "BD_transient"
    BD_HARD = "BD_hard"


#: Classes for which spending one of the four legal attempts can be justified.
RETRYABLE_CLASSES = frozenset({RootCause.TD, RootCause.BD_TRANSIENT})

#: Razorpay's top-level error codes.
ERROR_CODES = frozenset({"BAD_REQUEST_ERROR", "GATEWAY_ERROR", "SERVER_ERROR"})

#: Razorpay's ``error_source`` vocabulary.
ERROR_SOURCES = frozenset(
    {"customer", "business", "bank", "gateway", "network", "issuer", "internal"}
)

#: Razorpay's ``error_step`` vocabulary.
ERROR_STEPS = frozenset(
    {
        "payment_initiation",
        "payment_authentication",
        "payment_authorization",
        "payment_capture",
    }
)

#: The contract. Keyed on ``(error_source, error_reason)``, because that pair is
#: what actually determines the class: ``insufficient_funds`` means the same thing
#: whether it surfaced at authentication or authorization. ``error_code`` and
#: ``error_step`` are still validated on every call — garbage in either means the
#: ingest mapping is wrong, and a lookup that happened to succeed anyway would
#: hide that.
_TABLE: dict[tuple[str, str], RootCause] = {
    # --- technical: the rails failed, the mandate is fine ---------------------
    ("bank", "issuer_down"): RootCause.TD,
    ("bank", "payment_timed_out"): RootCause.TD,
    ("bank", "npci_unavailable"): RootCause.TD,
    ("gateway", "gateway_technical_error"): RootCause.TD,
    ("network", "network_error"): RootCause.TD,
    ("internal", "server_error"): RootCause.TD,
    # --- business, recoverable: the account said no for now ------------------
    # The largest and most valuable bucket. UPI Autopay failures in India are
    # dominated by low balances, and they clear on payday -- which is the timing
    # signal the whole policy layer exists to exploit.
    ("customer", "insufficient_funds"): RootCause.BD_TRANSIENT,
    ("customer", "limit_exceeded"): RootCause.BD_TRANSIENT,
    ("customer", "authentication_failed"): RootCause.BD_TRANSIENT,
    # Bank-flagged, but it describes a per-transaction restriction on a live
    # account rather than a dead one -- so it is transient, not hard.
    ("bank", "debit_not_permitted"): RootCause.BD_TRANSIENT,
    # --- business, terminal: the authorisation is gone -----------------------
    ("customer", "mandate_revoked"): RootCause.BD_HARD,
    ("customer", "invalid_vpa"): RootCause.BD_HARD,
    ("customer", "account_closed"): RootCause.BD_HARD,
    ("customer", "card_expired"): RootCause.BD_HARD,
    ("bank", "account_blocked"): RootCause.BD_HARD,
    ("bank", "mandate_not_found"): RootCause.BD_HARD,
}


def _validate(value: str, allowed: frozenset[str], field: str) -> str:
    if value not in allowed:
        raise ValueError(
            f"root_cause: unknown error_{field} {value!r}. The {field} vocabulary is "
            f"fixed by Razorpay's API; a value outside it means the ingest mapping is "
            f"wrong. Known: {sorted(allowed)}."
        )
    return value


def classify(
    error_code: str,
    error_source: str,
    error_step: str,
    error_reason: str,
) -> RootCause:
    """Map a Razorpay failure onto its root-cause class.

    Raises:
        ValueError: any of the four fields is outside Razorpay's vocabulary.
        KeyError: the ``(source, reason)`` pair has no row in the table.
    """
    code = _validate(error_code.strip().upper(), ERROR_CODES, "code")
    source = _validate(error_source.strip().lower(), ERROR_SOURCES, "source")
    step = _validate(error_step.strip().lower(), ERROR_STEPS, "step")
    reason = error_reason.strip().lower()

    try:
        return _TABLE[(source, reason)]
    except KeyError:
        raise KeyError(
            f"root_cause: unmapped failure (code={code!r}, source={source!r}, "
            f"step={step!r}, reason={reason!r}). Add a row to compliance/root_cause.py "
            f"with a stated reason for the class. There is deliberately no default: "
            f"guessing here mislabels a training feature and a retry decision at once."
        ) from None


def known_combinations() -> set[tuple[str, str]]:
    """Every ``(source, reason)`` pair the table classifies.

    Used by the test suite to walk the contract end to end, and by the simulator to
    assert that it never emits a failure Winback cannot classify.
    """
    return set(_TABLE)


def is_retryable(root_cause: RootCause) -> bool:
    """Whether spending a legal attempt on this class can be justified at all.

    A convenience over ``RETRYABLE_CLASSES`` for the policy layer. It answers
    "would a retry be pointless?", never "should we retry?" — that second question
    belongs to the model and the cost policy, downstream of this one.
    """
    return root_cause in RETRYABLE_CLASSES

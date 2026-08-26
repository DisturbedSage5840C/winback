"""TRAI TCCCPR consent rules for contacting a customer.

Source: TRAI's Telecom Commercial Communications Customer Preference Regulations,
2018, as amended in February 2025. Two obligations matter to a dunning agent:

* A commercial message needs **recorded consent**, and consent that has been
  withdrawn cannot be relied on again until the customer gives it afresh.
* A transactional message rides on a **transaction**, not on a standing
  relationship. The window it opens is short — seven days here — and once it
  lapses the merchant is back to needing promotional consent.

Two questions, deliberately two functions:

``check_nudge``
    May we message this customer *now*?
``may_request_reconsent``
    May we ask this customer to opt back in? Not within 90 days of a withdrawal.

Collapsing them into one boolean is the mistake this module exists to prevent. A
system that treats "cannot message" and "cannot ask again" as one state will
either re-solicit someone who just opted out, or permanently write off a customer
whose seven-day window merely lapsed.

Note what this module cannot see: the amount at risk. That omission is the point.
Consent is not a function of how much money is on the table, and a signature that
accepted an amount would eventually be asked to bend for a large one.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from compliance.result import RuleResult, Verdict

RULE = "consent_gate"

#: Days a completed (or attempted) transaction keeps a transactional window open.
TRANSACTIONAL_WINDOW_DAYS = 7

#: Days after a withdrawal before re-consent may be solicited.
DND_COOLOFF_DAYS = 90

CONSENT_WITHDRAWN = "consent_withdrawn"
DND_REGISTERED = "dnd_registered"
WINDOW_EXPIRED = "transactional_window_expired"
NO_BASIS = "no_transactional_basis"

#: Mirrors the CHECK constraint on ``customers.consent_status``.
VALID_STATUSES = frozenset({"active", "dnd", "withdrawn"})

#: Statuses that represent a standing objection, and so start the cooloff clock.
_OBJECTING_STATUSES = frozenset({"dnd", "withdrawn"})


def _require_aware(moment: datetime, label: str) -> datetime:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"{RULE} requires a timezone-aware datetime for {label}; a naive "
            "timestamp would have to be guessed at, and consent windows are "
            "measured in real elapsed time."
        )
    return moment


def _require_known_status(consent_status: str) -> str:
    normalised = consent_status.strip().lower()
    if normalised not in VALID_STATUSES:
        raise ValueError(
            f"{RULE}: unknown consent status {consent_status!r}. There is no safe "
            f"default here — a status nobody recognises means the consent record is "
            f"not understood, and both guesses are worse than stopping. "
            f"Known: {sorted(VALID_STATUSES)}."
        )
    return normalised


def _format_remaining(remaining: timedelta) -> str:
    if remaining >= timedelta(days=1):
        return f"{remaining.days} days"
    hours = int(remaining.total_seconds() // 3600)
    return f"{hours} hours"


def check_nudge(
    consent_status: str,
    consent_updated_at: datetime,
    last_transaction_at: datetime | None,
    now: datetime,
) -> RuleResult:
    """Whether a recovery message may be sent to this customer at ``now``.

    Both a consent problem and a lapsed window produce a ``DENY``, but with
    different ``stop_reason`` values, because the remedies differ: one customer
    said no, the other simply went quiet for too long. An audit trail that blurs
    them cannot answer "why was this account never contacted?".
    """
    _require_aware(consent_updated_at, "consent_updated_at")
    _require_aware(now, "now")
    status = _require_known_status(consent_status)

    metadata: dict[str, object] = {
        "consent_status": status,
        "consent_updated_at": consent_updated_at.isoformat(),
    }

    if status == "withdrawn":
        return RuleResult(
            rule=RULE,
            verdict=Verdict.DENY,
            detail=f"{RULE}: consent withdrawn on {consent_updated_at.date()}; no channel is open",
            stop_reason=CONSENT_WITHDRAWN,
            metadata=metadata,
        )

    if status == "dnd":
        return RuleResult(
            rule=RULE,
            verdict=Verdict.DENY,
            detail=f"{RULE}: customer is on DND as of {consent_updated_at.date()}",
            stop_reason=DND_REGISTERED,
            metadata=metadata,
        )

    if last_transaction_at is None:
        return RuleResult(
            rule=RULE,
            verdict=Verdict.DENY,
            detail=(
                f"{RULE}: no transaction on record, so no transactional window is open "
                f"and consent alone does not authorise a message"
            ),
            stop_reason=NO_BASIS,
            metadata=metadata,
        )

    _require_aware(last_transaction_at, "last_transaction_at")
    if last_transaction_at > now:
        raise ValueError(
            f"{RULE}: last_transaction_at ({last_transaction_at.isoformat()}) is in the "
            f"future relative to now ({now.isoformat()}). That is a clock or ingest bug, "
            "and reading it as a freshly-opened window would be the most permissive "
            "possible response to a bug."
        )

    expires_at = last_transaction_at + timedelta(days=TRANSACTIONAL_WINDOW_DAYS)
    metadata |= {
        "last_transaction_at": last_transaction_at.isoformat(),
        "window_expires_at": expires_at.isoformat(),
    }

    if now > expires_at:
        overdue = now - expires_at
        return RuleResult(
            rule=RULE,
            verdict=Verdict.DENY,
            detail=(
                f"{RULE}: the {TRANSACTIONAL_WINDOW_DAYS}-day transactional window closed "
                f"{_format_remaining(overdue)} ago"
            ),
            stop_reason=WINDOW_EXPIRED,
            metadata=metadata,
        )

    return RuleResult(
        rule=RULE,
        verdict=Verdict.APPROVE,
        detail=(
            f"{RULE}: active consent, transactional window open for another "
            f"{_format_remaining(expires_at - now)}"
        ),
        metadata=metadata,
    )


def may_request_reconsent(
    consent_status: str,
    consent_updated_at: datetime,
    now: datetime,
) -> bool:
    """Whether the customer may be *asked* to opt back in.

    Distinct from ``check_nudge`` on purpose: a customer 91 days past a withdrawal
    may be invited to re-consent, and still must not be nudged about an invoice
    until they actually say yes. This is the function that lets a written-off
    account come back without letting a fresh opt-out be argued with.
    """
    _require_aware(consent_updated_at, "consent_updated_at")
    _require_aware(now, "now")
    status = _require_known_status(consent_status)

    if status not in _OBJECTING_STATUSES:
        return True

    return now - consent_updated_at >= timedelta(days=DND_COOLOFF_DAYS)

"""RBI's 24-hour pre-debit notification requirement.

Source: RBI's e-mandate framework for recurring transactions. The customer must be
notified at least 24 hours before a recurring debit is presented, and given the
chance to opt out of that particular cycle.

The whole rule is one subtraction. The judgment is in its scope:

* A **new debit** without a timely notice is blocked. The customer was never
  warned, and no amount of downstream care fixes that.
* A **retry inside the same cycle** is not blocked. The notice authorises the
  cycle, not the individual attempt — a second presentment of an already-notified
  debit is the same debit trying again. Blocking it would forfeit essentially
  every recovery this project exists to make, in exchange for no protection the
  customer did not already receive.

A retry with a missing or late notice still carries the fact into
``metadata["warning"]``, so a systemic gap in notice data shows up in the audit
trail as a pattern rather than disappearing because nothing blocked.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from compliance.result import RuleResult, Verdict

RULE = "pre_debit_notice"

#: RBI's minimum lead time between notification and presentment.
NOTICE_LEAD_TIME = timedelta(hours=24)

NOTICE_MISSING = "pre_debit_notice_missing"
NOTICE_TOO_LATE = "pre_debit_notice_too_late"


def _require_aware(moment: datetime, label: str) -> datetime:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"{RULE} requires a timezone-aware datetime for {label}; the rule is a "
            "24-hour gap between two instants, and a naive timestamp has no instant."
        )
    return moment


def check(
    notice_sent_at: datetime | None,
    charge_at: datetime,
    attempt_number: int,
) -> RuleResult:
    """Whether the pre-debit notice permits this presentment.

    ``attempt_number`` is 1 for the cycle's first presentment and 2-4 for retries
    under the NPCI cap; it is the only thing that decides whether a notice defect
    blocks or merely warns.
    """
    if attempt_number < 1:
        raise ValueError(
            f"{RULE}: attempt_number must be 1 or greater (got {attempt_number}). "
            "Attempt 0 does not exist, and treating it as a retry would exempt it "
            "from the notice rule entirely."
        )

    _require_aware(charge_at, "charge_at")
    is_retry = attempt_number > 1

    if notice_sent_at is None:
        defect, defect_detail = NOTICE_MISSING, "no pre-debit notice on record"
    else:
        _require_aware(notice_sent_at, "notice_sent_at")
        lead = charge_at - notice_sent_at
        if lead >= NOTICE_LEAD_TIME:
            hours = lead.total_seconds() / 3600
            return RuleResult(
                rule=RULE,
                verdict=Verdict.APPROVE,
                detail=f"{RULE}: notice sent {hours:.1f}h before the debit",
                metadata={
                    "notice_sent_at": notice_sent_at.isoformat(),
                    "charge_at": charge_at.isoformat(),
                    "lead_hours": round(hours, 2),
                    "attempt_number": attempt_number,
                },
            )
        hours = lead.total_seconds() / 3600
        defect = NOTICE_TOO_LATE
        defect_detail = f"notice sent only {hours:.1f}h before the debit, short of 24h"

    metadata: dict[str, object] = {
        "notice_sent_at": notice_sent_at.isoformat() if notice_sent_at else None,
        "charge_at": charge_at.isoformat(),
        "attempt_number": attempt_number,
    }

    if is_retry:
        return RuleResult(
            rule=RULE,
            verdict=Verdict.APPROVE,
            detail=(
                f"{RULE}: {defect_detail}, but attempt {attempt_number} is a retry "
                f"within an already-notified cycle -- warned, not blocked"
            ),
            metadata=metadata | {"warning": defect},
        )

    return RuleResult(
        rule=RULE,
        verdict=Verdict.DENY,
        detail=f"{RULE}: {defect_detail}; a new debit may not be presented unwarned",
        stop_reason=defect,
        metadata=metadata,
    )

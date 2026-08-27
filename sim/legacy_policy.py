"""The dunning policy the merchant runs today, reproduced faithfully — flaws included.

This module has two jobs, and the second one is the reason it exists.

**It is arm C of the evaluation**: what the merchant actually does now, and therefore
the only baseline whose improvement a merchant can bank. A policy that beats "never
retry" has proved very little.

**It censors the training data, on purpose.** The legacy system only ever retried
invoices above ₹500 that were not netbanking. Those two conditions decide which
outcomes were *ever observed*, and both correlate with the outcome — a small debit
against the same monthly headroom is under less amount pressure and so more likely
to clear. The censored region is systematically *easier* than the observed one, so a
model fit on observed rows alone will be miscalibrated exactly where it has no data,
in a direction that costs money: it will under-predict recovery on precisely the
invoices it was never allowed to try.

That is the point. `docs/EVALUATION.md` reports calibration on the observed and the
censored slices separately, measured against the oracle, and the gap between them is
a more honest credibility signal than any headline AUC. A model that has never been
shown a hard case cannot be trusted on one, and saying so with a number is better
than hoping nobody asks.

**It commits real violations.** The schedule below predates NPCI OC-215-A (1 August
2025) and was never updated, which is the ordinary reason production systems break
new rules: the rule changed, the cron did not. Its high-value branch fires at 11:30
IST, inside NPCI's morning peak window. Those attempts are counted, not hidden —
they are what makes the violations-by-arm column non-trivial for arm C.

What the legacy policy is *not* is over the attempt cap. It schedules three retries
on both branches, so it consumes exactly the four attempts NPCI allows. Its failure
is subtler and more expensive than illegality: it burns all four on nearly every
invoice regardless of whether any of them could plausibly succeed. That is what the
₹-per-legal-attempt metric is built to expose, and it is the honest reason to beat
this baseline rather than a strawman one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from compliance.non_peak_window import IST, is_non_peak
from core.money import paise
from sim.world import Mandate

#: Why an invoice's retry outcomes were never observed. Written to
#: ``payment_attempts.observed = FALSE`` with the reason kept alongside, so the
#: censoring can be reported rather than inferred.
BELOW_VALUE_FLOOR = "legacy_value_floor"
UNSUPPORTED_RAIL = "legacy_rail_excluded"

#: NPCI OC-215-A: one attempt plus three retries. Mirrored from
#: ``payment_attempts.attempt_number CHECK (BETWEEN 1 AND 4)`` in db/01_schema.sql.
MAX_ATTEMPTS = 4


@dataclass(frozen=True, slots=True)
class LegacyParams:
    """The legacy system's configuration, as it was actually set.

    Every value here is a decision someone made years ago for a reason that made
    sense then. None of them were revisited when OC-215-A landed.
    """

    #: "Don't waste a gateway call on small invoices." Sensible-sounding, and the
    #: source of half the censoring.
    value_floor_paise: int = paise(500)
    #: Netbanking mandates were onboarded late and the retry job was never extended
    #: to them. The other half of the censoring, and the more honest kind: not a
    #: policy decision at all, just a gap nobody closed.
    excluded_methods: tuple[str, ...] = ("netbanking",)

    #: The standard branch: T+1, T+2, T+3. Fixed offsets, no regard for the
    #: customer's salary cycle — which is the substantive thing Winback does
    #: differently.
    retry_offsets_days: tuple[int, ...] = (1, 2, 3)
    #: 09:00 IST: start of the working day, chosen so a human could watch the run.
    #: Legal under OC-215-A by accident rather than by design.
    retry_hour: time = time(9, 0)

    #: Above this the invoice takes the "chase it hard" branch instead: same-day,
    #: then T+1 and T+2. Same number of retries — someone did read the old NPCI
    #: guidance — but compressed, and started inside a window that only became
    #: illegal later.
    urgent_threshold_paise: int = paise(2_000)
    urgent_offsets_days: tuple[int, ...] = (0, 1, 2)
    #: 11:30 IST — inside NPCI's 10:00-13:00 peak window. Illegal since 1 Aug 2025,
    #: and still scheduled, because the constant lives in a config file nobody read.
    urgent_hour: time = time(11, 30)


DEFAULT_LEGACY = LegacyParams()


@dataclass(frozen=True, slots=True)
class ScheduledRetry:
    """One retry the legacy policy would have made."""

    attempt_number: int
    execute_at: datetime
    #: Free text for the audit row and the drill-down drawer. The legacy arm has to
    #: be able to explain itself too, or the comparison is not like-for-like.
    rationale: str

    @property
    def in_peak_window(self) -> bool:
        return not is_non_peak(self.execute_at)


def censoring_reason(
    mandate: Mandate, params: LegacyParams = DEFAULT_LEGACY
) -> str | None:
    """Why this mandate's retry outcomes were never observed, or ``None`` if they were.

    Checked in the order the legacy job checked them, so the reason recorded is the
    one that actually stopped it.
    """
    if mandate.method in params.excluded_methods:
        return UNSUPPORTED_RAIL
    if mandate.amount_paise <= params.value_floor_paise:
        return BELOW_VALUE_FLOOR
    return None


def would_retry(mandate: Mandate, params: LegacyParams = DEFAULT_LEGACY) -> bool:
    """Whether the legacy system would have touched this invoice at all."""
    return censoring_reason(mandate, params) is None


def _at_hour(day: datetime, clock: time) -> datetime:
    """A wall-clock IST time on the given day.

    IST wall-clock rather than a UTC offset because that is how the legacy cron was
    written — ``0 9 * * *`` in ``Asia/Kolkata`` — and reproducing the policy means
    reproducing its clock.
    """
    local = day.astimezone(IST)
    return local.replace(
        hour=clock.hour, minute=clock.minute, second=0, microsecond=0
    )


def retry_schedule(
    mandate: Mandate, charge_at: datetime, params: LegacyParams = DEFAULT_LEGACY
) -> tuple[ScheduledRetry, ...]:
    """Every retry the legacy policy would schedule for a failed charge.

    Returned in execution order and numbered continuously from the original charge,
    so ``attempt_number`` means the same thing here as it does everywhere else and
    the NPCI cap can be applied to this arm without a translation step.

    Two branches, chosen only on invoice value, because that is the only thing the
    legacy job looks at. Note what is *not* here: the failure's root cause, the
    customer's salary day, the bank, or how many attempts remain. The legacy policy
    reads none of them. That is not a strawman — it is what a fixed-offset dunning
    cron is.
    """
    if not would_retry(mandate, params):
        return ()

    if mandate.amount_paise > params.urgent_threshold_paise:
        offsets, clock, branch = params.urgent_offsets_days, params.urgent_hour, "urgent"
    else:
        offsets, clock, branch = params.retry_offsets_days, params.retry_hour, "standard"

    schedule = tuple(
        ScheduledRetry(
            attempt_number=index + 2,  # attempt 1 is the original charge
            execute_at=_at_hour(charge_at + timedelta(days=offset), clock),
            rationale=f"legacy {branch}: fixed T+{offset} retry at {clock:%H:%M} IST",
        )
        for index, offset in enumerate(offsets)
    )

    # Both branches must fit inside NPCI's 1+3. Not because the legacy system knows
    # about the cap -- it does not -- but because payment_attempts.attempt_number is
    # CHECKed to 1..4, so a schedule longer than this could not be stored and would
    # surface as an insert error three modules downstream instead of here.
    if schedule and schedule[-1].attempt_number > MAX_ATTEMPTS:
        raise ValueError(
            f"legacy {branch} branch schedules {len(schedule)} retries, which needs "
            f"attempt_number {schedule[-1].attempt_number}; payment_attempts allows "
            f"at most {MAX_ATTEMPTS}. Change the offsets, or change the schema first."
        )

    return schedule


def violations(
    mandate: Mandate, charge_at: datetime, params: LegacyParams = DEFAULT_LEGACY
) -> tuple[str, ...]:
    """Which NPCI rules this schedule breaks, named.

    Computed here so the legacy arm's violation count comes from the schedule itself
    rather than from the evaluator noticing after the fact — the same discipline the
    guardrail applies to Winback, applied to the baseline it is measured against.

    Returns rule names, not a count: a single schedule can break one rule across
    three attempts, and the audit row for each of those attempts needs to say which
    rule it broke. ``eval_arm_results.compliance_violations`` counts audit rows.
    """
    schedule = retry_schedule(mandate, charge_at, params)

    # The cap is not checked here: retry_schedule refuses to build a schedule that
    # exceeds it, so an over-cap legacy run is a programming error rather than a
    # compliance finding. The window is the rule this policy actually breaks.
    return tuple(
        "peak_window" for retry in schedule if retry.in_peak_window
    )

"""Action-conditioned features for ``P(success | attempt, action, slot)``.

The model scores *candidate* actions, so every feature here has to be knowable before
the attempt is made. That is easy to say and easy to violate, so leakage is prevented
structurally rather than by discipline: :func:`features_for` is not given the attempt
row at all. It receives the static attributes, the attempts that came strictly before,
and a :class:`Candidate` describing the proposed slot — and a :class:`Candidate` has no
outcome, no ``error_code``, no ``root_cause_class`` and no ``p_success`` field to leak.
The forbidden columns are out of scope, not merely unused.

Two exclusions are deliberate and worth stating, because both would improve the metrics
and both would be cheating:

``p_success``
    The oracle's own probability. It is the label's generating function; a model given
    it would score near-perfectly and would have learned nothing. It never leaves
    ``sim/`` except into the evaluation, which compares against it rather than trains
    on it.

``salary_day``
    The customer's payday is the mechanism behind the balance hazard, and the simulator
    knows it. A merchant does not. Handing it over would let the model read the answer
    off a column instead of inferring timing from ``day_of_month``, which is what a real
    recovery system has to do — and since salary days differ across customers,
    ``day_of_month`` is a genuinely noisy proxy for it. The payday signal is meant to be
    *discovered*; see ``docs/DATA.md`` §02.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import cos, log1p, pi, sin

from compliance.non_peak_window import IST, is_non_peak
from sim.generate import AttemptRow, CustomerRow, Dataset, InvoiceRow, SubscriptionRow

#: Column order for the design matrix. Fixed and asserted against at build time — the
#: model artifact stores it, and a silent reordering between training and serving is the
#: classic way a boosted tree starts scoring noise while looking healthy.
FEATURE_NAMES: tuple[str, ...] = (
    # --- static mandate attributes -------------------------------------------------
    "method_upi_autopay",
    "method_card_mandate",
    "method_netbanking",
    "amount_rupees",
    "log_amount",
    "crosses_afa_15k",
    "mcc_is_high_afa_ceiling",
    "bank_method_failure_rate",
    # --- where this attempt sits in the invoice's budget ----------------------------
    "attempt_number",
    "attempts_remaining",
    "action_is_retry",
    # --- what the *previous* failure was --------------------------------------------
    "prior_root_cause_td",
    "prior_root_cause_bd_transient",
    "prior_root_cause_bd_hard",
    "prior_failures_this_invoice",
    "hours_since_prior_attempt",
    # --- the candidate slot ----------------------------------------------------------
    "hour_of_day",
    "is_non_peak",
    "day_of_week",
    "day_of_month",
    "day_of_month_sin",
    "day_of_month_cos",
    # --- the mandate's own history ---------------------------------------------------
    "mandate_age_days",
    "paid_count",
    "remaining_count",
    "days_since_last_success",
    "lifetime_failure_rate",
    "cycle_number",
)

#: MCC categories RBI allows a ₹1,00,000 AFA ceiling rather than ₹15,000. The model does
#: not enforce the rule — ``compliance/afa_threshold.py`` does — but the ceiling changes
#: which invoices a merchant would historically have pursued, so it carries signal.
HIGH_CEILING_MCC: frozenset[str] = frozenset({"insurance", "mutual_fund_sip", "credit_card"})

#: Stand-in for "no successful charge has ever been observed on this mandate". Chosen
#: rather than NaN so the column has one meaning; trees split it off cleanly at the top.
NEVER_SUCCEEDED_DAYS: float = 9_999.0


@dataclass(frozen=True, slots=True)
class Candidate:
    """A *proposed* attempt: everything decided before the money moves, and nothing else.

    This type is the leakage barrier. It deliberately has no outcome field, so a feature
    function that receives one cannot accidentally read the future — the compiler and the
    reader agree there is nothing there to read.
    """

    attempt_number: int
    action: str
    execute_at: datetime
    amount_paise: int

    @classmethod
    def from_attempt(cls, attempt: AttemptRow) -> Candidate:
        """Project a historical row down to only its pre-decision fields.

        Training rows come from attempts that already happened, and this is where their
        outcome is dropped. Everything downstream sees the projection, never the row.
        """
        return cls(
            attempt_number=attempt.attempt_number,
            action=attempt.action,
            execute_at=attempt.attempted_at,
            amount_paise=attempt.amount_paise,
        )


@dataclass(frozen=True, slots=True)
class BankMethodRates:
    """Historical failure rate per ``(bank, method)``, fitted on the training cohort only.

    A rate computed over the whole dataset would carry test-cohort outcomes into a
    training feature — mild leakage, but the kind that quietly inflates a held-out score
    and is invisible once the numbers are in a table.
    """

    rates: dict[tuple[str, str], float]
    fallback: float

    def get(self, bank: str, method: str) -> float:
        return self.rates.get((bank, method), self.fallback)

    @classmethod
    def fit(cls, dataset: Dataset, *, cohort: str = "train") -> BankMethodRates:
        subs = {s.subscription_id: s for s in dataset.subscriptions if s.cohort == cohort}
        seen: dict[tuple[str, str], list[int]] = {}
        total: list[int] = []
        for attempt in dataset.attempts:
            if not attempt.observed:
                continue
            sub = subs.get(attempt.subscription_id)
            if sub is None:
                continue
            failed = int(attempt.outcome == "failed")
            seen.setdefault((sub.bank, sub.method), []).append(failed)
            total.append(failed)
        # A pair seen only a handful of times has a rate that is mostly noise, so it is
        # shrunk toward the global mean rather than trusted. m=50 is one cohort-month of
        # attempts for a mid-sized bank; below that the prior should dominate.
        grand = sum(total) / len(total) if total else 0.0
        m = 50.0
        rates = {
            key: (sum(vals) + m * grand) / (len(vals) + m) for key, vals in seen.items()
        }
        return cls(rates=rates, fallback=grand)


@dataclass(frozen=True, slots=True)
class PriorState:
    """What the mandate's history says, as of the moment just before a candidate runs.

    Assembled once per attempt from rows strictly earlier than the candidate slot. Kept
    as its own type so the "strictly earlier" filter happens in exactly one place.
    """

    prior_root_cause: str | None
    prior_failures_this_invoice: int
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    lifetime_attempts: int
    lifetime_failures: int

    @classmethod
    def before(
        cls,
        candidate: Candidate,
        *,
        invoice_id: str,
        history: tuple[AttemptRow, ...],
    ) -> PriorState:
        """Fold the observed attempts that precede ``candidate`` into a state summary.

        ``history`` is the mandate's whole observed timeline, ascending. Only rows that
        started strictly before the candidate slot are folded in — an attempt cannot
        inform a decision made at the same instant, and ties would otherwise let a row
        read itself.
        """
        prior_root_cause: str | None = None
        prior_failures = 0
        last_attempt_at: datetime | None = None
        last_success_at: datetime | None = None
        lifetime_attempts = 0
        lifetime_failures = 0

        for row in history:
            if row.attempted_at >= candidate.execute_at:
                break
            lifetime_attempts += 1
            if row.outcome == "captured":
                last_success_at = row.attempted_at
            else:
                lifetime_failures += 1
            last_attempt_at = row.attempted_at
            if row.invoice_id == invoice_id and row.outcome == "failed":
                prior_failures += 1
                prior_root_cause = row.root_cause_class

        return cls(
            prior_root_cause=prior_root_cause,
            prior_failures_this_invoice=prior_failures,
            last_attempt_at=last_attempt_at,
            last_success_at=last_success_at,
            lifetime_attempts=lifetime_attempts,
            lifetime_failures=lifetime_failures,
        )


def features_for(
    *,
    subscription: SubscriptionRow,
    customer: CustomerRow,
    invoice: InvoiceRow,
    candidate: Candidate,
    prior: PriorState,
    rates: BankMethodRates,
) -> dict[str, float]:
    """Build one feature row. Never sees an outcome — see the module docstring.

    ``customer`` is passed for ``signup_date`` only; ``salary_day`` is deliberately not
    read, and the assertion at the bottom of this module's test suite enforces that by
    checking a shuffled ``salary_day`` produces identical features.
    """
    slot = candidate.execute_at.astimezone(IST)
    rupees = candidate.amount_paise / 100.0

    # Day-of-month as a circular quantity as well as a linear one. A tree can carve the
    # linear column into buckets, but the month wraps: day 30 and day 1 are adjacent in
    # the balance cycle and far apart on a number line.
    angle = 2.0 * pi * (slot.day - 1) / 31.0

    row: dict[str, float] = {
        "method_upi_autopay": float(subscription.method == "upi_autopay"),
        "method_card_mandate": float(subscription.method == "card_mandate"),
        "method_netbanking": float(subscription.method == "netbanking"),
        "amount_rupees": rupees,
        "log_amount": log1p(rupees),
        "crosses_afa_15k": float(candidate.amount_paise > 15_000_00),
        "mcc_is_high_afa_ceiling": float(subscription.mcc_category in HIGH_CEILING_MCC),
        "bank_method_failure_rate": rates.get(subscription.bank, subscription.method),
        "attempt_number": float(candidate.attempt_number),
        "attempts_remaining": float(4 - candidate.attempt_number),
        "action_is_retry": float(candidate.action == "retry"),
        "prior_root_cause_td": float(prior.prior_root_cause == "TD"),
        "prior_root_cause_bd_transient": float(prior.prior_root_cause == "BD_transient"),
        "prior_root_cause_bd_hard": float(prior.prior_root_cause == "BD_hard"),
        "prior_failures_this_invoice": float(prior.prior_failures_this_invoice),
        "hours_since_prior_attempt": (
            (candidate.execute_at - prior.last_attempt_at).total_seconds() / 3600.0
            if prior.last_attempt_at is not None
            else 0.0
        ),
        "hour_of_day": float(slot.hour + slot.minute / 60.0),
        "is_non_peak": float(is_non_peak(candidate.execute_at)),
        "day_of_week": float(slot.weekday()),
        "day_of_month": float(slot.day),
        "day_of_month_sin": sin(angle),
        "day_of_month_cos": cos(angle),
        "mandate_age_days": float((slot.date() - subscription.mandate_start).days),
        "paid_count": float(subscription.paid_count),
        "remaining_count": float(subscription.remaining_count),
        "days_since_last_success": (
            (candidate.execute_at - prior.last_success_at).total_seconds() / 86400.0
            if prior.last_success_at is not None
            else NEVER_SUCCEEDED_DAYS
        ),
        "lifetime_failure_rate": (
            prior.lifetime_failures / prior.lifetime_attempts
            if prior.lifetime_attempts
            else rates.get(subscription.bank, subscription.method)
        ),
        "cycle_number": float(invoice.cycle_number),
    }

    assert set(row) == set(FEATURE_NAMES), (
        "feature row does not match FEATURE_NAMES; the artifact's column order and the "
        "builder have diverged"
    )
    return row

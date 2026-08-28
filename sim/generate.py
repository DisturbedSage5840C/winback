"""Build the frozen Winback dataset: a population, its billing history, and the
attempts the legacy policy did — and did not — make.

Three properties this file exists to guarantee.

**Frozen.** ``AS_OF`` is a constant, not ``now()``. Every random draw comes from an
RNG seeded on the entity's own id rather than from a single sequential stream, so
adding a customer, reordering a loop, or inserting a new field does not silently
re-roll the customers that already exist. Regenerating must reproduce the committed
dataset exactly, because ``docs/EVALUATION.md`` quotes numbers computed from it.

**Honest about what was observed.** An attempt row is written for every debit the
legacy policy made (``observed = TRUE``) *and* for the retries it declined to make on
censored invoices (``observed = FALSE``). The second kind never reached a rail, cost
no NPCI budget, and changed no invoice's status — they exist only so the
counterfactual evaluator can score the region the training data cannot see. Confusing
the two is the single easiest way to fabricate a result, so the distinction is carried
in the schema, in ``exception_worklist``, and here.

**Outcomes are never decided here.** This module chooses *who exists* and *what was
attempted*. Whether an attempt succeeded is always ``sim.world.resolve`` — one oracle,
one seed derivation, one place. A generator that decided its own outcomes could not be
replayed by the evaluator, and the pairing across arms would be a fiction.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from compliance.non_peak_window import IST, is_non_peak
from compliance.root_cause import RootCause
from core.money import format_rupees, paise
from sim import legacy_policy
from sim.legacy_policy import LegacyParams, censoring_reason, retry_schedule
from sim.world import (
    DEFAULT_PARAMS,
    AttemptContext,
    Customer,
    Mandate,
    WorldParams,
    oracle_key,
    resolve,
)

# --------------------------------------------------------------------------- frozen

#: Bump when a change here alters the generated rows. Written to
#: ``eval_runs.dataset_version`` so a committed result can always be traced to the
#: data that produced it.
DATASET_VERSION = "v1"

#: Every per-entity RNG is seeded from this plus the entity's id.
POPULATION_SEED = "winback-population-2026"

#: The dataset's "today". A constant, deliberately: seeding from the wall clock
#: would mean the numbers in docs/EVALUATION.md quietly stopped matching the data.
AS_OF = datetime(2026, 8, 24, 0, 0, tzinfo=IST)

#: Sized by the *test* cohort, not by the total. The split is ordered in time, so the
#: test cohort is the newest mandates — the ones with the fewest billing cycles behind
#: them. At 500 subscriptions it held 20 observed retries and 24 failed first charges,
#: which is too thin to calibrate on and far too thin for the four-arm money headline:
#: a paired bootstrap over 24 invoices produces an interval wide enough to cover every
#: arm. 4,000 gives the test cohort 173 retries and 210 recovery opportunities.
N_SUBSCRIPTIONS = 4_000

#: How far back the oldest mandate starts. Long enough that ``paid_count`` reaches
#: the reliability plateau (8 cycles) for a decent share of the population, so the
#: model has both new and seasoned mandates to separate.
HISTORY_MONTHS = 15

# --------------------------------------------------------------------------- mix

#: India's recurring rails, weighted the way an Indian subscription book actually
#: looks: UPI Autopay dominant and growing, cards second, netbanking a long tail.
#: Netbanking's small share matters -- it is one of the two censoring conditions,
#: so it sets how much of the training data is missing for rail reasons.
METHOD_MIX: tuple[tuple[str, float], ...] = (
    ("upi_autopay", 0.62),
    ("card_mandate", 0.28),
    ("netbanking", 0.10),
)

#: Issuer mix. The names feed ``world.bank_factor``, which hashes them into a stable
#: per-bank reliability multiplier, so this list decides how much bank-level variance
#: the model has to explain.
BANK_MIX: tuple[tuple[str, float], ...] = (
    ("HDFC", 0.19),
    ("SBI", 0.17),
    ("ICICI", 0.15),
    ("Axis", 0.12),
    ("Kotak", 0.09),
    ("PNB", 0.08),
    ("BoB", 0.07),
    ("Yes", 0.05),
    ("IndusInd", 0.04),
    ("Federal", 0.04),
)

#: Salary days cluster hard at the 1st and the 7th in India, with a month-end tail
#: for government and PSU payrolls. This is the distribution that makes day-of-month
#: a real signal rather than noise -- if every customer were paid on the 1st, the
#: model could learn the calendar instead of the mechanism.
SALARY_DAY_MIX: tuple[tuple[int, float], ...] = (
    (1, 0.38),
    (2, 0.07),
    (5, 0.09),
    (7, 0.19),
    (10, 0.06),
    (15, 0.05),
    (25, 0.06),
    (28, 0.10),
)

#: (category, min ₹, max ₹). Spread chosen so the population straddles all three
#: rupee boundaries the system cares about: ₹500 (legacy value floor), ₹2,000
#: (legacy urgent branch), ₹15,000 (RBI AFA standard ceiling). A population that
#: never crosses a boundary cannot test the rule that lives there.
MCC_MIX: tuple[tuple[str, float, int, int], ...] = (
    ("ott_subscription", 0.20, 149, 899),
    ("saas", 0.13, 499, 4_999),
    ("news_media", 0.09, 99, 499),
    ("edtech", 0.10, 999, 6_999),
    ("fitness", 0.08, 499, 2_999),
    ("utility", 0.10, 300, 3_000),
    ("insurance", 0.11, 1_500, 25_000),
    ("mutual_fund_sip", 0.13, 500, 25_000),
    ("credit_card_bill", 0.06, 2_000, 60_000),
)

#: Consent state. Most customers are contactable; the rest exist so the consent gate
#: has something to block in the batch rather than only in its unit tests.
CONSENT_MIX: tuple[tuple[str, float], ...] = (
    ("active", 0.86),
    ("dnd", 0.09),
    ("withdrawn", 0.05),
)

#: IST hours a merchant actually presents an auto-debit. All non-peak: the original
#: charge is an execution too, and OC-215-A governs it. Keeping originals legal is
#: what isolates arm C's violations to the legacy urgent branch, where the story
#: says they are.
PRESENTMENT_HOURS: tuple[tuple[int, float], ...] = (
    (1, 0.14), (2, 0.16), (3, 0.14), (4, 0.11), (5, 0.09), (6, 0.08),
    (7, 0.06), (8, 0.05), (9, 0.05), (14, 0.05), (15, 0.04), (22, 0.03),
)

#: Median multiple of the debit amount that a customer can spare per month for
#: recurring payments, and its log-spread.
#:
#: This is the parameter that decides how hard the balance mechanism bites, so it is
#: worth being precise about what ``monthly_headroom_paise`` means: not income, and
#: not total discretionary spending, but the money available for debits of this kind.
#: ``world.balance_hazard`` divides the amount by it, so a multiple of 1.0 would mean
#: the debit consumes the customer's entire recurring budget. People broadly
#: subscribe in proportion to what they can spare, which is why this is sampled as a
#: multiple of the amount rather than as an absolute rupee figure -- an absolute
#: figure would make every ₹149 mandate risk-free and every SIP hopeless.
HEADROOM_MULTIPLE_MEDIAN = 2.1
HEADROOM_MULTIPLE_SIGMA = 0.80

#: Share of invoices billed without a compliant 24h pre-debit notice, so
#: ``compliance/pre_debit_notice.py`` has real rows to warn on.
MISSING_NOTICE_RATE = 0.08

#: How much of the current cycle's dunning has already happened when the batch runs.
#: Weighted so the worklist contains invoices at every point in the NPCI budget --
#: including exhausted ones, which is what produces the cap DENY on screen.
RETRIES_ALREADY_MADE_MIX: tuple[tuple[int, float], ...] = (
    (0, 0.42),
    (1, 0.27),
    (2, 0.19),
    (3, 0.12),
)

#: Fractions of the population, ordered by mandate start, assigned to each split.
#: Split by customer *and* by time: no customer straddles two splits, and the test
#: cohort's mandates begin latest.
COHORT_SPLIT: tuple[tuple[str, float], ...] = (
    ("train", 0.60),
    ("calibrate", 0.20),
    ("test", 0.20),
)

# --------------------------------------------------------------------------- rows


@dataclass(frozen=True, slots=True)
class CustomerRow:
    customer_id: str
    customer_hash: str
    signup_date: date
    consent_status: str
    consent_updated_at: datetime
    salary_day: int
    #: Not a column: carried so the oracle can be called without a second lookup.
    monthly_headroom_paise: int


@dataclass(frozen=True, slots=True)
class SubscriptionRow:
    subscription_id: str
    customer_id: str
    method: str
    bank: str
    mcc_category: str
    amount_paise: int
    status: str
    mandate_start: date
    paid_count: int
    remaining_count: int
    cohort: str


@dataclass(frozen=True, slots=True)
class InvoiceRow:
    invoice_id: str
    subscription_id: str
    cycle_number: int
    amount_paise: int
    charge_at: datetime
    notice_sent_at: datetime | None
    status: str


@dataclass(frozen=True, slots=True)
class AttemptRow:
    attempt_id: str
    invoice_id: str
    subscription_id: str
    attempt_number: int
    attempted_at: datetime
    is_non_peak: bool
    action: str
    amount_paise: int
    outcome: str
    error_code: str | None
    error_source: str | None
    error_step: str | None
    error_reason: str | None
    root_cause_class: str | None
    observed: bool
    oracle_seed: str
    #: Not a column. The oracle's ground-truth success probability, kept for
    #: sim/validate_realism.py and for the observed-vs-censored calibration report.
    #: It is never a model feature: leaking it would make every metric meaningless.
    p_success: float
    #: Which legacy filter suppressed this retry, or None if it actually happened.
    #: Set exactly when ``observed`` is false — the schema enforces the pair.
    censoring_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Dataset:
    customers: tuple[CustomerRow, ...]
    subscriptions: tuple[SubscriptionRow, ...]
    invoices: tuple[InvoiceRow, ...]
    attempts: tuple[AttemptRow, ...]
    dataset_version: str = DATASET_VERSION

    def fingerprint(self) -> str:
        """A stable hash of every generated row.

        The frozen-dataset claim is only worth something if it is checkable, and a
        test that regenerates and compares this is cheaper than diffing 4,000 rows.
        """
        digest = hashlib.sha256()
        for table in (self.customers, self.subscriptions, self.invoices, self.attempts):
            for row in table:
                digest.update(repr(sorted(asdict(row).items())).encode())
        return digest.hexdigest()[:16]

    def summary(self) -> dict[str, object]:
        observed = [a for a in self.attempts if a.observed]
        censored = [a for a in self.attempts if not a.observed]
        first = [a for a in observed if a.attempt_number == 1]
        return {
            "dataset_version": self.dataset_version,
            "fingerprint": self.fingerprint(),
            "customers": len(self.customers),
            "subscriptions": len(self.subscriptions),
            "invoices": len(self.invoices),
            "attempts_total": len(self.attempts),
            "attempts_observed": len(observed),
            "attempts_censored": len(censored),
            "censoring_rate": len(censored) / max(len(self.attempts), 1),
            "first_charges": len(first),
            "first_charge_failure_rate": sum(a.outcome == "failed" for a in first)
            / max(len(first), 1),
            "invoices_at_risk": sum(i.status == "at_risk" for i in self.invoices),
            "invoices_recovered": sum(i.status == "recovered" for i in self.invoices),
            "invoices_written_off": sum(i.status == "written_off" for i in self.invoices),
            "cohorts": dict(Counter(s.cohort for s in self.subscriptions)),
        }


# --------------------------------------------------------------------------- helpers


def _rng(*parts: object) -> random.Random:
    """A generator seeded on what it is generating, not on call order.

    Sequential seeding is the usual way a "frozen" dataset silently thaws: someone
    adds a field, every subsequent draw shifts, and the committed metrics no longer
    describe the committed data. Keying on the entity id makes each entity's draws
    independent of every other entity's existence.
    """
    key = "|".join([POPULATION_SEED, *(str(part) for part in parts)])
    return random.Random(hashlib.sha256(key.encode()).hexdigest())  # noqa: S311


def _pick[T](rng: random.Random, mix: tuple[tuple[T, float], ...]) -> T:
    values = [value for value, _ in mix]
    weights = [weight for _, weight in mix]
    return rng.choices(values, weights=weights, k=1)[0]


def _add_months(moment: datetime, months: int) -> datetime:
    """Same day-of-month, `months` later, in IST wall clock.

    Billing days above 28 are not sampled (``salary_day`` is CHECKed to 1..28 and
    mandate starts are drawn on the same range), so no month-length clamping is
    needed and none is done -- a silent clamp would move a debit off its billing day
    and quietly change its position in the salary cycle.
    """
    local = moment.astimezone(IST)
    total = local.month - 1 + months
    year = local.year + total // 12
    month = total % 12 + 1
    return local.replace(year=year, month=month)


def _customer_hash(customer_id: str) -> str:
    """The only customer identifier permitted into ``audit_log.observed_data``."""
    return hashlib.sha256(customer_id.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- population


def _build_customer(index: int) -> CustomerRow:
    customer_id = f"cust_{index:04d}"
    rng = _rng("customer", customer_id)

    salary_day = _pick(rng, SALARY_DAY_MIX)
    consent = _pick(rng, CONSENT_MIX)
    signup = (AS_OF - timedelta(days=rng.randint(40, 1400))).date()

    return CustomerRow(
        customer_id=customer_id,
        customer_hash=_customer_hash(customer_id),
        signup_date=signup,
        consent_status=consent,
        consent_updated_at=AS_OF - timedelta(days=rng.randint(1, 400)),
        salary_day=salary_day,
        # Placeholder: headroom depends on the mandate amount, which is drawn next.
        # Set for real in _build_subscription's caller.
        monthly_headroom_paise=0,
    )


def _build_subscription(index: int, customer: CustomerRow) -> tuple[SubscriptionRow, int]:
    """Returns the subscription and the customer's headroom implied by its amount."""
    subscription_id = f"sub_{index:04d}"
    rng = _rng("subscription", subscription_id)

    mcc_row = _pick(rng, tuple((row, row[1]) for row in MCC_MIX))
    mcc_category, _, low_rupees, high_rupees = mcc_row

    # Log-uniform within the category: subscription prices cluster at the cheap end
    # of any tier, and a uniform draw would over-populate the top of every band.
    amount_paise = paise(
        round(math.exp(rng.uniform(math.log(low_rupees), math.log(high_rupees))))
    )

    headroom_multiple = rng.lognormvariate(
        math.log(HEADROOM_MULTIPLE_MEDIAN), HEADROOM_MULTIPLE_SIGMA
    )
    headroom_paise = max(amount_paise // 2, round(amount_paise * headroom_multiple))

    months_ago = rng.randint(1, HISTORY_MONTHS)
    start_moment = _add_months(AS_OF, -months_ago).replace(day=rng.randint(1, 28))

    total_count = rng.choice([12, 12, 24, 36])

    return (
        SubscriptionRow(
            subscription_id=subscription_id,
            customer_id=customer.customer_id,
            method=_pick(rng, METHOD_MIX),
            bank=_pick(rng, BANK_MIX),
            mcc_category=mcc_category,
            amount_paise=amount_paise,
            status="active",  # revised once the history is walked
            mandate_start=start_moment.date(),
            paid_count=0,
            remaining_count=total_count,
            cohort="train",  # revised once every mandate start is known
            ),
        headroom_paise,
    )


def _assign_cohorts(
    subscriptions: list[SubscriptionRow],
) -> dict[str, str]:
    """Split by customer AND by time, as ``subscriptions.cohort`` promises.

    One subscription per customer here, so "by customer" is automatic; the ordering
    by mandate start is what makes it "by time". The test cohort therefore holds the
    newest mandates, which is the harder and more honest direction -- a model is
    always asked to score mandates younger than the ones it learned from.
    """
    ordered = sorted(subscriptions, key=lambda s: (s.mandate_start, s.subscription_id))

    cohorts: dict[str, str] = {}
    start = 0
    for cohort, share in COHORT_SPLIT:
        stop = start + round(share * len(ordered))
        for row in ordered[start:stop]:
            cohorts[row.subscription_id] = cohort
        start = stop
    # Rounding can leave a tail; it belongs to the last (latest) split.
    for row in ordered[start:]:
        cohorts[row.subscription_id] = COHORT_SPLIT[-1][0]
    return cohorts


# --------------------------------------------------------------------------- history


def _presentment(subscription: SubscriptionRow, cycle_number: int) -> datetime:
    """When cycle N is presented. Fixed hour per subscription, monthly cadence."""
    rng = _rng("presentment", subscription.subscription_id)
    hour = _pick(rng, PRESENTMENT_HOURS)
    first = datetime.combine(
        subscription.mandate_start, datetime.min.time(), tzinfo=IST
    ).replace(hour=hour)
    return _add_months(first, cycle_number - 1)


def _attempt_row(
    *,
    subscription: SubscriptionRow,
    invoice: InvoiceRow,
    customer: Customer,
    mandate: Mandate,
    context: AttemptContext,
    observed: bool,
    censored_because: str | None,
    params: WorldParams,
) -> AttemptRow:
    outcome = resolve(customer, mandate, context, params)

    return AttemptRow(
        attempt_id=f"att_{invoice.invoice_id}_{context.attempt_number}",
        invoice_id=invoice.invoice_id,
        subscription_id=subscription.subscription_id,
        attempt_number=context.attempt_number,
        attempted_at=context.execute_at,
        is_non_peak=is_non_peak(context.execute_at),
        action=context.action,
        amount_paise=invoice.amount_paise,
        outcome="captured" if outcome.captured else "failed",
        # Carries error_code/source/step/reason and root_cause_class together,
        # because they are one fact: the class is a pure function of the tuple.
        **outcome.error_fields,
        observed=observed,
        oracle_seed=oracle_key(mandate, context),
        p_success=outcome.p_success,
        censoring_reason=censored_because,
    )


@dataclass
class _History:
    """Mutable accumulator for one subscription's walk through its cycles."""

    invoices: list[InvoiceRow] = field(default_factory=list)
    attempts: list[AttemptRow] = field(default_factory=list)
    paid_count: int = 0
    halted: bool = False
    pending: bool = False


def _walk_subscription(
    subscription: SubscriptionRow,
    customer_row: CustomerRow,
    legacy: LegacyParams,
    params: WorldParams,
) -> _History:
    """Replay every billing cycle from mandate start to AS_OF.

    The current cycle is treated differently from the closed ones, and that is the
    whole point of the function. A closed cycle ran its dunning to completion, so its
    status is known. The current cycle is mid-flight: some of its legacy retries have
    happened and the rest have not, which is exactly the state the agent is asked to
    take over. Generating it any other way would give the batch an empty worklist or
    a worklist where every invoice had the same attempts remaining.
    """
    history = _History()
    customer = Customer(
        customer_id=customer_row.customer_id,
        salary_day=customer_row.salary_day,
        monthly_headroom_paise=customer_row.monthly_headroom_paise,
    )
    censored_because = censoring_reason(
        Mandate(subscription.subscription_id, subscription.method, subscription.bank,
                subscription.amount_paise, 0),
        legacy,
    )

    cycle = 0
    while True:
        cycle += 1
        charge_at = _presentment(subscription, cycle)
        if charge_at > AS_OF:
            break

        invoice_id = f"inv_{subscription.subscription_id[4:]}_{cycle:02d}"
        rng = _rng("invoice", invoice_id)
        notice_sent_at = (
            None
            if rng.random() < MISSING_NOTICE_RATE
            else charge_at - timedelta(hours=rng.uniform(25, 72))
        )

        mandate = Mandate(
            subscription_id=subscription.subscription_id,
            method=subscription.method,
            bank=subscription.bank,
            amount_paise=subscription.amount_paise,
            paid_count=history.paid_count,
        )
        invoice = InvoiceRow(
            invoice_id=invoice_id,
            subscription_id=subscription.subscription_id,
            cycle_number=cycle,
            amount_paise=subscription.amount_paise,
            charge_at=charge_at,
            notice_sent_at=notice_sent_at,
            status="paid",  # revised below
        )

        first = _attempt_row(
            subscription=subscription,
            invoice=invoice,
            customer=customer,
            mandate=mandate,
            context=AttemptContext(
                invoice_id=invoice_id,
                cycle_number=cycle,
                attempt_number=1,
                action="initial_charge",
                execute_at=charge_at,
            ),
            observed=True,
            censored_because=None,
            params=params,
        )
        history.attempts.append(first)

        if first.outcome == "captured":
            history.invoices.append(invoice)
            history.paid_count += 1
            continue

        # The charge failed. Whether this cycle is closed or still in flight decides
        # how much of the legacy schedule has run.
        is_current = _presentment(subscription, cycle + 1) > AS_OF
        status = _dun(
            history=history,
            subscription=subscription,
            invoice=invoice,
            customer=customer,
            mandate=mandate,
            legacy=legacy,
            params=params,
            censored_because=censored_because,
            is_current=is_current,
            last_technical_failure_at=(
                first.attempted_at if first.root_cause_class == RootCause.TD else None
            ),
        )
        history.invoices.append(
            InvoiceRow(**{**asdict(invoice), "status": status})  # type: ignore[arg-type]
        )

        if status == "recovered":
            history.paid_count += 1
            continue
        if status == "at_risk":
            history.pending = True
        else:
            history.halted = True
        break

    return history


def _dun(
    *,
    history: _History,
    subscription: SubscriptionRow,
    invoice: InvoiceRow,
    customer: Customer,
    mandate: Mandate,
    legacy: LegacyParams,
    params: WorldParams,
    censored_because: str | None,
    is_current: bool,
    last_technical_failure_at: datetime | None,
) -> str:
    """Run the legacy dunning schedule for one failed invoice; return its status.

    Censored invoices take the counterfactual path: the schedule the legacy policy
    *would* have run had its filters not existed is materialised with
    ``observed = FALSE``, and — this is the load-bearing part — the resulting
    outcomes are discarded when deciding the invoice's status. In real history those
    retries never happened, so a shadow retry that the oracle says would have
    captured must not turn an unpaid invoice into a recovered one. Letting it would
    hand the model a label it could never have seen and inflate every arm at once.
    """
    if censored_because is not None:
        uncensored = LegacyParams(
            value_floor_paise=0,
            excluded_methods=(),
            retry_offsets_days=legacy.retry_offsets_days,
            retry_hour=legacy.retry_hour,
            urgent_threshold_paise=legacy.urgent_threshold_paise,
            urgent_offsets_days=legacy.urgent_offsets_days,
            urgent_hour=legacy.urgent_hour,
        )
        if not is_current:
            for retry in retry_schedule(mandate, invoice.charge_at, uncensored):
                history.attempts.append(
                    _attempt_row(
                        subscription=subscription,
                        invoice=invoice,
                        customer=customer,
                        mandate=mandate,
                        context=AttemptContext(
                            invoice_id=invoice.invoice_id,
                            cycle_number=invoice.cycle_number,
                            attempt_number=retry.attempt_number,
                            action="retry",
                            execute_at=retry.execute_at,
                            last_technical_failure_at=last_technical_failure_at,
                        ),
                        observed=False,
                        censored_because=censored_because,
                        params=params,
                    )
                )
        # No observed retry ever happened, so the invoice is unpaid either way.
        return "at_risk" if is_current else "written_off"

    schedule = retry_schedule(mandate, invoice.charge_at, legacy)
    if is_current:
        already = _pick(_rng("dunning", invoice.invoice_id), RETRIES_ALREADY_MADE_MIX)
        # Two separate limits, and the tighter one wins. The draw says how far a
        # merchant's cron happened to get; AS_OF says how far it *could* have got.
        # An invoice that failed yesterday has a T+1 retry on the schedule that the
        # clock has not reached yet — recording it would date an attempt into the
        # future and hand the model an outcome nobody could have observed. Offsets
        # increase, so the reachable set is a prefix.
        reachable = sum(1 for r in schedule if r.execute_at <= AS_OF)
        schedule = schedule[: min(already, reachable)]

    for retry in schedule:
        row = _attempt_row(
            subscription=subscription,
            invoice=invoice,
            customer=customer,
            mandate=mandate,
            context=AttemptContext(
                invoice_id=invoice.invoice_id,
                cycle_number=invoice.cycle_number,
                attempt_number=retry.attempt_number,
                action="retry",
                execute_at=retry.execute_at,
                last_technical_failure_at=last_technical_failure_at,
            ),
            observed=True,
            censored_because=None,
            params=params,
        )
        history.attempts.append(row)
        if row.outcome == "captured":
            return "recovered"
        if row.root_cause_class == RootCause.TD:
            last_technical_failure_at = row.attempted_at

    # Every retry the legacy policy was going to make has been made and none worked.
    # A still-current invoice keeps whatever budget is left; a closed one is done.
    return "at_risk" if is_current else "written_off"


# --------------------------------------------------------------------------- build


def build_dataset(
    n_subscriptions: int = N_SUBSCRIPTIONS,
    *,
    legacy: LegacyParams = legacy_policy.DEFAULT_LEGACY,
    params: WorldParams = DEFAULT_PARAMS,
) -> Dataset:
    """Generate the whole dataset in memory. Pure, deterministic, no database."""
    customers: list[CustomerRow] = []
    subscriptions: list[SubscriptionRow] = []

    for index in range(1, n_subscriptions + 1):
        customer = _build_customer(index)
        subscription, headroom = _build_subscription(index, customer)
        customers.append(
            CustomerRow(**{**asdict(customer), "monthly_headroom_paise": headroom})  # type: ignore[arg-type]
        )
        subscriptions.append(subscription)

    cohorts = _assign_cohorts(subscriptions)

    invoices: list[InvoiceRow] = []
    attempts: list[AttemptRow] = []
    final_subscriptions: list[SubscriptionRow] = []

    for customer, subscription in zip(customers, subscriptions, strict=True):
        history = _walk_subscription(subscription, customer, legacy, params)
        invoices.extend(history.invoices)
        attempts.extend(history.attempts)

        if history.halted:
            status = "halted"
        elif history.pending:
            status = "pending"
        else:
            status = "active"

        final_subscriptions.append(
            SubscriptionRow(
                **{
                    **asdict(subscription),
                    "status": status,
                    "paid_count": history.paid_count,
                    "remaining_count": max(
                        0, subscription.remaining_count - history.paid_count
                    ),
                    "cohort": cohorts[subscription.subscription_id],
                }  # type: ignore[arg-type]
            )
        )

    return Dataset(
        customers=tuple(customers),
        subscriptions=tuple(final_subscriptions),
        invoices=tuple(invoices),
        attempts=tuple(attempts),
    )


# --------------------------------------------------------------------------- load


LOAD_ORDER = ("customers", "subscriptions", "invoices", "payment_attempts")


def load(dataset: Dataset) -> dict[str, int]:
    """Write the dataset into Postgres, replacing whatever was there.

    Imported lazily so ``build_dataset`` stays usable — and testable — with no
    database running. Day 3's realism work needs the rows, not the server.
    """
    from core.db import owner_connection, reset_world

    reset_world()
    written: dict[str, int] = {}

    with owner_connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO customers (customer_id, customer_hash, signup_date,"
            " consent_status, consent_updated_at, salary_day)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (c.customer_id, c.customer_hash, c.signup_date, c.consent_status,
                 c.consent_updated_at, c.salary_day)
                for c in dataset.customers
            ],
        )
        written["customers"] = len(dataset.customers)

        cur.executemany(
            "INSERT INTO subscriptions (subscription_id, customer_id, method, bank,"
            " mcc_category, amount_paise, status, mandate_start, paid_count,"
            " remaining_count, cohort) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (s.subscription_id, s.customer_id, s.method, s.bank, s.mcc_category,
                 s.amount_paise, s.status, s.mandate_start, s.paid_count,
                 s.remaining_count, s.cohort)
                for s in dataset.subscriptions
            ],
        )
        written["subscriptions"] = len(dataset.subscriptions)

        cur.executemany(
            "INSERT INTO invoices (invoice_id, subscription_id, cycle_number,"
            " amount_paise, charge_at, notice_sent_at, status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (i.invoice_id, i.subscription_id, i.cycle_number, i.amount_paise,
                 i.charge_at, i.notice_sent_at, i.status)
                for i in dataset.invoices
            ],
        )
        written["invoices"] = len(dataset.invoices)

        cur.executemany(
            "INSERT INTO payment_attempts (attempt_id, invoice_id, subscription_id,"
            " attempt_number, attempted_at, is_non_peak, action, amount_paise, outcome,"
            " error_code, error_source, error_step, error_reason, root_cause_class,"
            " observed, censoring_reason, oracle_seed)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (a.attempt_id, a.invoice_id, a.subscription_id, a.attempt_number,
                 a.attempted_at, a.is_non_peak, a.action, a.amount_paise, a.outcome,
                 a.error_code, a.error_source, a.error_step, a.error_reason,
                 a.root_cause_class, a.observed, a.censoring_reason, a.oracle_seed)
                for a in dataset.attempts
            ],
        )
        written["payment_attempts"] = len(dataset.attempts)

        conn.commit()

    return written


# --------------------------------------------------------------------------- cli


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--load", action="store_true", help="write the dataset into Postgres"
    )
    parser.add_argument(
        "-n", "--subscriptions", type=int, default=N_SUBSCRIPTIONS
    )
    args = parser.parse_args()

    dataset = build_dataset(args.subscriptions)
    summary = dataset.summary()

    print(f"Winback dataset {summary['dataset_version']}  ({summary['fingerprint']})")
    print(f"  as of                 {AS_OF:%Y-%m-%d} IST, frozen")
    print(f"  customers             {summary['customers']:,}")
    print(f"  subscriptions         {summary['subscriptions']:,}  {summary['cohorts']}")
    print(f"  invoices              {summary['invoices']:,}")
    print(
        f"  attempts              {summary['attempts_total']:,}"
        f"  ({summary['attempts_observed']:,} observed,"
        f" {summary['attempts_censored']:,} censored"
        f" = {summary['censoring_rate']:.1%})"
    )
    print(
        f"  first-charge failure  {summary['first_charge_failure_rate']:.2%}"
        f"  over {summary['first_charges']:,} debits"
    )
    print(
        f"  invoices at risk      {summary['invoices_at_risk']:,}"
        f"   recovered {summary['invoices_recovered']:,}"
        f"   written off {summary['invoices_written_off']:,}"
    )
    at_risk = sum(
        i.amount_paise for i in dataset.invoices if i.status == "at_risk"
    )
    print(f"  revenue at risk       {format_rupees(at_risk)}")

    if args.load:
        written = load(dataset)
        print("\nloaded into Postgres:")
        for table in LOAD_ORDER:
            print(f"  {table:<18} {written[table]:,}")


if __name__ == "__main__":
    main()

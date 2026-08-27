"""The world, as a deterministic counterfactual oracle.

This module answers one question — *what happens if this mandate is presented at
this moment?* — and it answers it the same way every time, for every caller. That
property is what makes the four-arm evaluation possible at all, so it is worth
being precise about.

**The coin flip is fixed by the question, not by the asker.** Every random draw is
derived from ``sha256`` over ``(channel, subject, invoice, attempt_number, action,
slot)``. The key deliberately contains no ``run_id`` and no ``arm``. So when arm B
and arm D both ask "retry invoice X, attempt 2, at 02:00 IST on the 14th", they
receive the identical outcome — and any difference in what the arms recover is a
difference in *policy*, not in luck. That turns policy comparison from independent
sampling into a paired design, which is both far lower variance and the only honest
basis for a paired bootstrap.

**The slot is quantised to the IST hour.** The world's state is (date, hour): a
retry at 02:00 and one at 02:30 meet the same congestion and the same bank balance,
so they draw the same flip. Quantising is what makes pairing actually bite — two
arms proposing "early morning" land on the same coin rather than near-misses.

**Failure reasons come out of the mechanism that caused them.** The world does not
label an attempt ``BD_transient``; it decides that the balance check failed and
emits Razorpay's ``insufficient_funds`` tuple, which
``compliance/root_cause.classify`` then maps — the *same function the live path
uses*. There is no parallel labelling route that could drift from production.

**The functional form is deliberately not the model's.** Hazards compose
multiplicatively over a continuous exponential in days-since-salary. XGBoost fits
additive step functions. It has to approximate this shape from censored data rather
than recover coefficients it was handed, which is the entire point of §3.1 of the
build plan.

Every constant lives in :class:`WorldParams`, in one place, with its source in the
comment beside it. ``sim/validate_realism.py`` checks that what the world actually
produces matches what those constants claim.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from datetime import datetime

from compliance.non_peak_window import IST, is_non_peak
from compliance.root_cause import RootCause, classify

#: A Razorpay error, in the shape the API returns it and the shape
#: ``compliance/root_cause.classify`` consumes: (code, source, step, reason).
type ErrorTuple = tuple[str, str, str, str]

#: Which hazard fired. Not persisted — this is ground truth the model never sees,
#: used only by the realism report and by tests that need to know *why*.
type Mechanism = str

CAPTURED: Mechanism = "captured"
TECHNICAL: Mechanism = "technical"
BALANCE: Mechanism = "balance"
AUTHORIZATION: Mechanism = "authorization"
MANDATE_DEAD: Mechanism = "mandate_dead"

METHODS = ("upi_autopay", "card_mandate", "netbanking")

# --------------------------------------------------------------------- failure catalogues
# Every tuple below must be a key ``compliance/root_cause.classify`` already knows.
# A test walks these catalogues and asserts exactly that, so an invented error
# reason fails at test time rather than at generation time.

_TECHNICAL_FAILURES: tuple[tuple[ErrorTuple, float], ...] = (
    (("GATEWAY_ERROR", "bank", "payment_authorization", "issuer_down"), 0.34),
    (("GATEWAY_ERROR", "bank", "payment_authorization", "payment_timed_out"), 0.26),
    (("GATEWAY_ERROR", "bank", "payment_initiation", "npci_unavailable"), 0.16),
    (("GATEWAY_ERROR", "gateway", "payment_initiation", "gateway_technical_error"), 0.12),
    (("GATEWAY_ERROR", "network", "payment_initiation", "network_error"), 0.08),
    (("SERVER_ERROR", "internal", "payment_initiation", "server_error"), 0.04),
)

#: The single most consequential row in this file. India's UPI-Autopay failures are
#: dominated by an empty account on the wrong day of the month, and that is a
#: *transient* condition that clears on payday — not a dead mandate.
_BALANCE_FAILURE: ErrorTuple = (
    "BAD_REQUEST_ERROR", "customer", "payment_authorization", "insufficient_funds"
)

_AUTHORIZATION_FAILURES: tuple[tuple[ErrorTuple, float], ...] = (
    (("BAD_REQUEST_ERROR", "customer", "payment_authentication", "authentication_failed"), 0.45),
    (("BAD_REQUEST_ERROR", "customer", "payment_authorization", "limit_exceeded"), 0.30),
    (("BAD_REQUEST_ERROR", "bank", "payment_authorization", "debit_not_permitted"), 0.25),
)

#: Hard failures differ by rail, because the ways a mandate dies differ by rail: a
#: card expires, a VPA is deleted, a bank account is closed.
_HARD_FAILURES: dict[str, tuple[tuple[ErrorTuple, float], ...]] = {
    "upi_autopay": (
        (("BAD_REQUEST_ERROR", "customer", "payment_authorization", "mandate_revoked"), 0.42),
        (("BAD_REQUEST_ERROR", "customer", "payment_initiation", "invalid_vpa"), 0.24),
        (("BAD_REQUEST_ERROR", "customer", "payment_authorization", "account_closed"), 0.16),
        (("BAD_REQUEST_ERROR", "bank", "payment_authorization", "mandate_not_found"), 0.11),
        (("BAD_REQUEST_ERROR", "bank", "payment_authorization", "account_blocked"), 0.07),
    ),
    "card_mandate": (
        (("BAD_REQUEST_ERROR", "customer", "payment_authorization", "card_expired"), 0.55),
        (("BAD_REQUEST_ERROR", "customer", "payment_authorization", "mandate_revoked"), 0.30),
        (("BAD_REQUEST_ERROR", "bank", "payment_authorization", "account_blocked"), 0.15),
    ),
    "netbanking": (
        (("BAD_REQUEST_ERROR", "customer", "payment_authorization", "account_closed"), 0.40),
        (("BAD_REQUEST_ERROR", "customer", "payment_authorization", "mandate_revoked"), 0.35),
        (("BAD_REQUEST_ERROR", "bank", "payment_authorization", "mandate_not_found"), 0.25),
    ),
}


@dataclass(frozen=True, slots=True)
class WorldParams:
    """Every constant the world runs on, with its justification.

    Frozen and passed explicitly rather than read from module globals, so a test can
    run a degenerate world (all hazards zero, or a certain outage) without monkey-
    patching, and so the realism report can state which parameter set produced it.
    """

    # --- technical decline (TD) -------------------------------------------------
    #: Per-rail baseline technical failure. UPI Autopay rides NPCI and inherits its
    #: outages; card mandates clear on a comparatively boring network. These are the
    #: TD share of the headline failure rates (~18% of failures are TD).
    technical_base: tuple[tuple[str, float], ...] = (
        ("upi_autopay", 0.021),
        ("card_mandate", 0.006),
        ("netbanking", 0.014),
    )
    #: NPCI's peak windows are congested — which is *why* OC-215-A bans execution in
    #: them. Congestion is a technical effect, so it multiplies the TD hazard only:
    #: a peak-hour retry does not make a customer's balance any lower.
    peak_congestion_multiplier: float = 2.4
    #: Banks are not equally reliable. A per-bank multiplier the model can learn from
    #: a bank x method historical failure-rate feature.
    bank_spread: float = 0.55

    # --- balance / business decline (BD_transient) ------------------------------
    #: Hazard on the customer's salary day: the account was just funded.
    balance_floor: float = 0.012
    #: Asymptotic hazard deep into the salary cycle, before amount pressure.
    balance_ceiling: float = 0.185
    #: Days for depletion to reach 1 - 1/e of the way from floor to ceiling. Twelve
    #: puts the steep part of the curve in the second and third weeks, which is where
    #: Indian subscription debits actually start bouncing.
    balance_tau_days: float = 12.0
    #: A debit large relative to the customer's monthly headroom is likelier to
    #: bounce than a ₹149 one. Scales the balance hazard by amount/headroom, capped.
    amount_pressure_cap: float = 1.6
    #: A mandate that has paid many cycles belongs to someone with stable income.
    #: This is the ``k_recency`` term of the build plan, expressed as history rather
    #: than as raw days: it discounts the balance hazard by up to this fraction.
    reliability_discount: float = 0.45
    #: Cycles of clean payment history at which the discount saturates.
    reliability_cycles: int = 8
    #: How much of the account-depletion hazard each rail is actually exposed to.
    #:
    #: This is the single term that separates UPI Autopay's 8-15% failure rate from
    #: the card mandate's 2-3%, and it is a mechanism rather than a fitted constant:
    #: a UPI Autopay debit lands on the savings account the moment it is presented,
    #: so an empty account is an immediate decline. A card mandate draws on a credit
    #: line, which absorbs the debit whether or not the customer has cash that day --
    #: exhausting a credit limit is a much rarer event than running a balance down.
    #: Netbanking e-mandates hit the account like UPI, but present in a batch that
    #: is retried within the banking day, so a little less of the depletion bites.
    #:
    #: Without this the simulator gives every rail the same balance hazard and card
    #: mandates fail at 8%, which sim/validate_realism.py catches and refuses.
    balance_exposure: tuple[tuple[str, float], ...] = (
        ("upi_autopay", 1.00),
        ("card_mandate", 0.10),
        ("netbanking", 0.90),
    )

    # --- authorization (the rest of BD_transient) -------------------------------
    #: Per-rail authorization/limit failures, independent of balance.
    #:
    #: Ordered by how much can go wrong in the rail's per-cycle authorization, which
    #: is not the same for all three. A UPI Autopay debit resolves a VPA, reaches a
    #: PSP, and looks up the mandate at NPCI on every execution -- three places to
    #: fail. A card mandate does its AFA once at registration and thereafter presents
    #: a single network authorization with no re-authentication step, so it has the
    #: fewest surfaces. Netbanking sits between them.
    authorization_base: tuple[tuple[str, float], ...] = (
        ("upi_autopay", 0.013),
        ("card_mandate", 0.007),
        ("netbanking", 0.009),
    )

    # --- mandate death (BD_hard) ------------------------------------------------
    #: Per-cycle hazard that the mandate itself dies — revoked, closed, expired.
    #: ~20 million Indian mandates are revoked monthly, overwhelmingly after a run of
    #: low-balance failures, so this is deliberately not negligible.
    death_hazard_per_cycle: tuple[tuple[str, float], ...] = (
        ("upi_autopay", 0.016),
        ("card_mandate", 0.011),
        ("netbanking", 0.008),
    )

    # --- retry dynamics ---------------------------------------------------------
    #: A technical decline usually reflects an outage with a lifetime measured in
    #: hours. A retry *inside* that window inherits an elevated hazard; one the next
    #: day does not. This is the signal that should teach the policy to wait.
    outage_persistence_hours: float = 6.0
    outage_persistence_multiplier: float = 3.1

    def technical_rate(self, method: str) -> float:
        return dict(self.technical_base)[method]

    def authorization_rate(self, method: str) -> float:
        return dict(self.authorization_base)[method]

    def death_rate(self, method: str) -> float:
        return dict(self.death_hazard_per_cycle)[method]

    def balance_exposure_for(self, method: str) -> float:
        return dict(self.balance_exposure)[method]


DEFAULT_PARAMS = WorldParams()


@dataclass(frozen=True, slots=True)
class Customer:
    """The parts of a customer the world's physics depend on."""

    customer_id: str
    #: Day of month the account is funded. 1..28 — the schema's own constraint,
    #: chosen so no customer's payday vanishes in a short February.
    salary_day: int
    #: Discretionary money available in a month, in paise. Sets how much amount
    #: pressure a given debit represents.
    monthly_headroom_paise: int


@dataclass(frozen=True, slots=True)
class Mandate:
    """The parts of a subscription the world's physics depend on."""

    subscription_id: str
    method: str
    bank: str
    amount_paise: int
    #: Cycles already paid cleanly. The reliability signal.
    paid_count: int


@dataclass(frozen=True, slots=True)
class AttemptContext:
    """The specific presentment being asked about."""

    invoice_id: str
    cycle_number: int
    attempt_number: int
    #: What is being attempted. Part of the oracle key, so "retry now" and "retry
    #: after a nudge" are genuinely different questions with different answers.
    action: str
    execute_at: datetime
    #: When the most recent technical decline on this invoice happened, if any.
    #: Drives outage persistence. ``None`` means no TD yet.
    last_technical_failure_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Hazards:
    """The decomposed truth behind one attempt.

    Kept as a separate value from the outcome because the realism report and the
    calibration analysis both want the *probability*, not just the draw.
    """

    technical: float
    balance: float
    authorization: float
    mandate_dead: bool

    @property
    def p_success(self) -> float:
        """Survival across all three independent hazards.

        Multiplicative rather than additive on purpose: this is a survival model,
        and it is a different functional form from the one that will try to learn it.
        """
        if self.mandate_dead:
            return 0.0
        return (1 - self.technical) * (1 - self.balance) * (1 - self.authorization)


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """What the world returns. The oracle's answer for one (attempt, action, slot)."""

    captured: bool
    mechanism: Mechanism
    #: The probability that *actually* governed this draw. Ground truth the model
    #: never sees at training time and the policy never sees at decision time —
    #: available only to the evaluation, where it supports a reliability check
    #: against true probabilities rather than against outcomes alone.
    p_success: float
    error: ErrorTuple | None = None
    root_cause: RootCause | None = None

    @property
    def error_fields(self) -> dict[str, str | None]:
        """Ready for a ``payment_attempts`` insert."""
        code, source, step, reason = self.error or (None, None, None, None)
        return {
            "error_code": code,
            "error_source": source,
            "error_step": step,
            "error_reason": reason,
            "root_cause_class": str(self.root_cause) if self.root_cause else None,
        }


# ------------------------------------------------------------------ deterministic draws


def _uniform(channel: str, *parts: object) -> float:
    """A uniform in [0, 1) determined entirely by the question being asked.

    ``channel`` separates the independent draws an attempt needs (technical,
    balance, authorization, which-error) so they do not correlate with each other
    while each stays reproducible on its own.
    """
    key = "|".join([channel, *(str(part) for part in parts)])
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _slot_key(execute_at: datetime) -> str:
    """The IST hour an attempt lands in.

    Quantised because the world's state is (date, hour): two retries in the same
    hour meet the same congestion and the same bank balance. Quantising is also what
    makes the paired design bite — arms that both choose "early morning" draw the
    same coin instead of two nearby but independent ones.
    """
    return execute_at.astimezone(IST).strftime("%Y-%m-%dT%H")


def oracle_key(mandate: Mandate, context: AttemptContext) -> str:
    """The identity of a counterfactual question.

    Contains no ``run_id`` and no ``arm``, which is the whole point: two policies
    asking the same question get the same answer, so their difference is policy and
    not luck. Persisted to ``payment_attempts.oracle_seed`` so any row in the
    database can be replayed years later.
    """
    return "|".join([
        mandate.subscription_id,
        context.invoice_id,
        str(context.attempt_number),
        context.action,
        _slot_key(context.execute_at),
    ])


def _choose(u: float, catalogue: tuple[tuple[ErrorTuple, float], ...]) -> ErrorTuple:
    """Weighted pick from a catalogue, using an already-drawn uniform."""
    cumulative = 0.0
    total = sum(weight for _, weight in catalogue)
    for error, weight in catalogue:
        cumulative += weight / total
        if u < cumulative:
            return error
    return catalogue[-1][0]  # float-arithmetic tail; the last entry is the fallback


# ------------------------------------------------------------------ the physics


def bank_factor(bank: str, params: WorldParams = DEFAULT_PARAMS) -> float:
    """A stable per-bank reliability multiplier centred on 1.0.

    Derived from the bank's name rather than stored, so it is identical in the
    generator, the oracle and any later analysis without a table to keep in sync.
    """
    return 1.0 + params.bank_spread * (2 * _uniform("bank", bank) - 1)


def days_since_salary(execute_at: datetime, salary_day: int) -> int:
    """Days elapsed in the customer's salary cycle, 0 on payday.

    Uses IST wall-clock, because a salary credit is a wall-clock event in India and
    a debit at 01:00 IST on the 15th belongs to the 15th, not to the 14th in UTC.
    """
    day = execute_at.astimezone(IST).day
    return (day - salary_day) % 30


def balance_hazard(
    customer: Customer,
    mandate: Mandate,
    execute_at: datetime,
    params: WorldParams = DEFAULT_PARAMS,
) -> float:
    """The salary-cycle depletion curve. The signal the model has to find.

    Floor on payday, rising exponentially towards a ceiling as the month wears on,
    scaled by how large this debit is against the customer's headroom, discounted by
    a history of clean payments, and finally scaled by how exposed the rail is to the
    account balance at all — a card mandate draws on credit, so most of this curve
    simply does not reach it.
    """
    elapsed = days_since_salary(execute_at, customer.salary_day)
    depletion = 1 - math.exp(-elapsed / params.balance_tau_days)
    hazard = params.balance_floor + (params.balance_ceiling - params.balance_floor) * depletion

    pressure = min(
        params.amount_pressure_cap,
        mandate.amount_paise / max(customer.monthly_headroom_paise, 1),
    )
    hazard *= pressure
    hazard *= params.balance_exposure_for(mandate.method)

    reliability = min(mandate.paid_count, params.reliability_cycles) / params.reliability_cycles
    hazard *= 1 - params.reliability_discount * reliability

    return min(hazard, 1.0)


def technical_hazard(
    mandate: Mandate,
    context: AttemptContext,
    params: WorldParams = DEFAULT_PARAMS,
) -> float:
    """Rail baseline, times bank reliability, times congestion, times outage memory."""
    hazard = params.technical_rate(mandate.method) * bank_factor(mandate.bank, params)

    if not is_non_peak(context.execute_at):
        hazard *= params.peak_congestion_multiplier

    last = context.last_technical_failure_at
    if last is not None:
        hours = (context.execute_at - last).total_seconds() / 3600
        if 0 <= hours < params.outage_persistence_hours:
            # Still inside the outage. Retrying into it burns a legal attempt on a
            # bank that has not come back yet — the mistake the policy should learn
            # to stop making, and it can only learn it if the world punishes it.
            hazard *= params.outage_persistence_multiplier

    return min(hazard, 1.0)


def is_mandate_dead(
    mandate: Mandate, cycle_number: int, params: WorldParams = DEFAULT_PARAMS
) -> bool:
    """Whether the mandate has died by this cycle, drawn once and stably.

    Modelled as a geometric survival over cycles and evaluated from the subscription
    id alone, so the oracle stays self-contained: given the ids, the entire
    counterfactual is reproducible with nothing read from the database.
    """
    return cycle_number >= death_cycle(mandate, params)


def death_cycle(mandate: Mandate, params: WorldParams = DEFAULT_PARAMS) -> int:
    """The cycle at which this mandate dies. Large means "not within any horizon"."""
    rate = params.death_rate(mandate.method)
    u = _uniform("death", mandate.subscription_id)
    # Geometric: P(dies at or before cycle n) = 1 - (1 - rate)^n.
    # Floored at 1 because cycle numbering starts at 1: a death_cycle of 0 would
    # make ``cycle_number >= death_cycle`` vacuously true and kill every mandate.
    return max(1, math.ceil(math.log(1 - u) / math.log(1 - rate)))


def hazards(
    customer: Customer,
    mandate: Mandate,
    context: AttemptContext,
    params: WorldParams = DEFAULT_PARAMS,
) -> Hazards:
    """Decompose one presentment into its independent failure hazards."""
    return Hazards(
        technical=technical_hazard(mandate, context, params),
        balance=balance_hazard(customer, mandate, context.execute_at, params),
        authorization=params.authorization_rate(mandate.method),
        mandate_dead=is_mandate_dead(mandate, context.cycle_number, params),
    )


def resolve(
    customer: Customer,
    mandate: Mandate,
    context: AttemptContext,
    params: WorldParams = DEFAULT_PARAMS,
) -> AttemptOutcome:
    """The oracle. What happens if this mandate is presented here, now, this way.

    Hazards are checked in the order a real rail would encounter them — is the
    mandate alive, does the network carry the request, is there money, does the bank
    authorise it — and each check consumes its own independent uniform. The failure
    reason emitted is the one belonging to whichever check fired, and it is then
    classified by the *production* classifier rather than labelled directly.
    """
    key = oracle_key(mandate, context)
    h = hazards(customer, mandate, context, params)

    def finish(mechanism: Mechanism, catalogue_choice: ErrorTuple) -> AttemptOutcome:
        code, source, step, reason = catalogue_choice
        return AttemptOutcome(
            captured=False,
            mechanism=mechanism,
            p_success=h.p_success,
            error=catalogue_choice,
            root_cause=classify(
                error_code=code, error_source=source, error_step=step, error_reason=reason
            ),
        )

    if h.mandate_dead:
        return finish(
            MANDATE_DEAD,
            _choose(_uniform("which_hard", key), _HARD_FAILURES[mandate.method]),
        )

    if _uniform("technical", key) < h.technical:
        return finish(
            TECHNICAL, _choose(_uniform("which_tech", key), _TECHNICAL_FAILURES)
        )

    if _uniform("balance", key) < h.balance:
        return finish(BALANCE, _BALANCE_FAILURE)

    if _uniform("authorization", key) < h.authorization:
        return finish(
            AUTHORIZATION, _choose(_uniform("which_auth", key), _AUTHORIZATION_FAILURES)
        )

    return AttemptOutcome(captured=True, mechanism=CAPTURED, p_success=h.p_success)


def counterfactual(
    customer: Customer,
    mandate: Mandate,
    context: AttemptContext,
    *,
    action: str | None = None,
    execute_at: datetime | None = None,
    params: WorldParams = DEFAULT_PARAMS,
) -> AttemptOutcome:
    """What *would* have happened under a different action or a different slot.

    The counterfactual an arm never took is exactly as well-defined as the one it
    did: same function, different key. This is the method by which "measured money
    recovered" becomes measurable at all in an environment that moves no real money.
    """
    return resolve(
        customer,
        mandate,
        replace(
            context,
            action=action if action is not None else context.action,
            execute_at=execute_at if execute_at is not None else context.execute_at,
        ),
        params,
    )


def all_error_tuples() -> set[ErrorTuple]:
    """Every error this world can emit. Walked by a test against ``root_cause``."""
    catalogues = [_TECHNICAL_FAILURES, _AUTHORIZATION_FAILURES, *_HARD_FAILURES.values()]
    return {_BALANCE_FAILURE} | {error for cat in catalogues for error, _ in cat}

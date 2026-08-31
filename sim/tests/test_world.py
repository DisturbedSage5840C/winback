"""The oracle's contract.

Three groups of claims, in descending order of how much the evaluation depends on
them:

1. **Determinism and pairing** — the same question always gets the same answer, and
   the answer does not depend on who asked. Without this the four-arm comparison is
   not paired and the bootstrap is not legitimate.
2. **The physics are the ones claimed** — payday matters, peak congestion is a
   technical effect only, a dead mandate is dead, an outage persists.
3. **The world speaks Razorpay** — every error it can emit is one the production
   classifier already knows, so the training label and the live label come from the
   same function.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from compliance.non_peak_window import IST
from compliance.root_cause import RootCause, classify, known_combinations
from core.money import paise
from sim import world
from sim.world import (
    AttemptContext,
    Customer,
    Mandate,
    WorldParams,
    counterfactual,
    hazards,
    oracle_key,
    resolve,
)

PAYDAY = 1
CUSTOMER = Customer(
    customer_id="cust_0001", salary_day=PAYDAY, monthly_headroom_paise=paise(12_000)
)
MANDATE = Mandate(
    subscription_id="sub_0001",
    method="upi_autopay",
    bank="HDFC",
    amount_paise=paise(499),
    paid_count=3,
)
#: 02:00 IST — legal under OC-215-A, and day 14 sits mid-salary-cycle.
SLOT = datetime(2026, 9, 14, 2, 0, tzinfo=IST)
CONTEXT = AttemptContext(
    invoice_id="inv_0001",
    cycle_number=4,
    attempt_number=2,
    action="retry",
    execute_at=SLOT,
)


def at(day: int, hour: int = 2) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=IST)


# ------------------------------------------------------- determinism and pairing


def test_the_same_question_always_gets_the_same_answer() -> None:
    first = resolve(CUSTOMER, MANDATE, CONTEXT)
    second = resolve(CUSTOMER, MANDATE, CONTEXT)
    assert first == second


def test_the_oracle_key_contains_no_arm_and_no_run() -> None:
    """The structural claim the whole paired evaluation rests on.

    If an arm identifier ever reaches the key, two policies asking the same question
    get different coins, every arm difference becomes part policy and part luck, and
    the paired bootstrap silently stops being valid. Asserted on the signature so it
    cannot quietly stop being true.
    """
    parameters = set(inspect.signature(oracle_key).parameters)
    assert parameters == {"mandate", "context"}

    surface = set(AttemptContext.__dataclass_fields__) | set(Mandate.__dataclass_fields__)
    assert not (surface & {"arm", "run_id", "policy", "cohort"})


def test_two_arms_asking_the_same_question_get_the_same_outcome() -> None:
    """Pairing, demonstrated rather than asserted about."""
    arm_b_view = replace(CONTEXT)
    arm_d_view = AttemptContext(
        invoice_id=CONTEXT.invoice_id,
        cycle_number=CONTEXT.cycle_number,
        attempt_number=CONTEXT.attempt_number,
        action=CONTEXT.action,
        execute_at=CONTEXT.execute_at,
    )
    assert resolve(CUSTOMER, MANDATE, arm_b_view) == resolve(CUSTOMER, MANDATE, arm_d_view)


def test_the_slot_is_quantised_to_the_ist_hour() -> None:
    """Two retries in the same hour meet the same world, so they draw the same coin.

    This is what makes pairing bite: arms that both pick "early morning" land on one
    coin instead of two nearby independent ones.
    """
    on_the_hour = replace(CONTEXT, execute_at=SLOT)
    half_past = replace(CONTEXT, execute_at=SLOT + timedelta(minutes=30))
    assert oracle_key(MANDATE, on_the_hour) == oracle_key(MANDATE, half_past)


def test_a_different_hour_is_a_different_question() -> None:
    later = replace(CONTEXT, execute_at=SLOT + timedelta(hours=1))
    assert oracle_key(MANDATE, later) != oracle_key(MANDATE, CONTEXT)


def test_the_key_is_the_same_instant_expressed_in_any_zone() -> None:
    """UTC in, IST reasoning. A slot is an instant, not a wall-clock string."""
    utc_view = replace(CONTEXT, execute_at=SLOT.astimezone(UTC))
    assert utc_view.execute_at.hour != SLOT.hour, "the wall clocks must actually differ"
    assert oracle_key(MANDATE, utc_view) == oracle_key(MANDATE, CONTEXT)


def test_the_action_is_part_of_the_question() -> None:
    """'retry now' and 'retry after a nudge' are different questions. If they shared
    a coin, the policy's action choice could never change an outcome and the whole
    evaluation would measure nothing."""
    nudged = replace(CONTEXT, action="retry_after_nudge")
    assert oracle_key(MANDATE, nudged) != oracle_key(MANDATE, CONTEXT)


def test_the_attempt_number_is_part_of_the_question() -> None:
    assert oracle_key(MANDATE, replace(CONTEXT, attempt_number=3)) != oracle_key(MANDATE, CONTEXT)


def test_a_counterfactual_is_the_same_function_with_a_different_key() -> None:
    """The action never taken is exactly as well-defined as the one that was."""
    moved = counterfactual(CUSTOMER, MANDATE, CONTEXT, execute_at=at(2))
    direct = resolve(CUSTOMER, MANDATE, replace(CONTEXT, execute_at=at(2)))
    assert moved == direct


def test_a_counterfactual_with_no_overrides_is_the_original() -> None:
    assert counterfactual(CUSTOMER, MANDATE, CONTEXT) == resolve(CUSTOMER, MANDATE, CONTEXT)


# ------------------------------------------------------- the world speaks Razorpay


def test_every_error_the_world_emits_is_one_production_can_classify() -> None:
    """No parallel labelling path.

    The model's training label comes from ``compliance/root_cause.classify`` — the
    same function that will classify a real Razorpay error on Day 6. If the world
    invented a reason string, training labels and live labels would come from two
    different places and could drift apart without anything failing.
    """
    for code, source, step, reason in world.all_error_tuples():
        classify(error_code=code, error_source=source, error_step=step, error_reason=reason)


def test_the_world_uses_only_known_source_reason_pairs() -> None:
    emitted = {(source, reason) for _, source, _, reason in world.all_error_tuples()}
    assert emitted <= known_combinations()


def test_an_empty_account_is_transient_not_fatal() -> None:
    """The single most consequential classification in the project.

    India's UPI-Autopay failures are dominated by low balances that clear on payday.
    Treating ``insufficient_funds`` as a dead mandate would write off precisely the
    invoices most worth retrying, and would do it while looking prudent.
    """
    code, source, step, reason = world._BALANCE_FAILURE
    assert reason == "insufficient_funds"
    assert (
        classify(error_code=code, error_source=source, error_step=step, error_reason=reason)
        is RootCause.BD_TRANSIENT
    )


def test_a_captured_attempt_carries_no_error() -> None:
    outcome = resolve(
        CUSTOMER,
        # A dead-certain success: no hazards at all.
        MANDATE,
        CONTEXT,
        WorldParams(
            technical_base=(("upi_autopay", 0.0), ("card_mandate", 0.0), ("netbanking", 0.0)),
            balance_floor=0.0,
            balance_ceiling=0.0,
            authorization_base=(("upi_autopay", 0.0), ("card_mandate", 0.0), ("netbanking", 0.0)),
            death_hazard_per_cycle=(
                ("upi_autopay", 1e-9),
                ("card_mandate", 1e-9),
                ("netbanking", 1e-9),
            ),
        ),
    )
    assert outcome.captured is True
    assert outcome.error is None
    assert outcome.root_cause is None
    assert outcome.error_fields == {
        "error_code": None,
        "error_source": None,
        "error_step": None,
        "error_reason": None,
        "root_cause_class": None,
    }


def test_a_failed_attempt_is_ready_for_the_attempts_table() -> None:
    outcome = resolve(CUSTOMER, MANDATE, CONTEXT, _certain_failure())
    fields = outcome.error_fields
    assert fields["root_cause_class"] in {"TD", "BD_transient", "BD_hard"}
    assert all(fields[k] is not None for k in fields)


# ------------------------------------------------------- the payday signal


def test_a_debit_on_payday_is_safer_than_one_late_in_the_cycle() -> None:
    """The mechanism the whole project is about, and the timing signal the model has
    to discover for itself."""
    on_payday = world.balance_hazard(CUSTOMER, MANDATE, at(PAYDAY))
    late = world.balance_hazard(CUSTOMER, MANDATE, at(26))
    assert on_payday < late
    assert late > 3 * on_payday, "the signal must be strong enough to be learnable"


def test_the_depletion_curve_is_monotone_across_the_cycle() -> None:
    """Monotone and continuous — an exponential, not a step function. XGBoost fits
    step functions, so it has to approximate this shape rather than recover it."""
    curve = [world.balance_hazard(CUSTOMER, MANDATE, at(day)) for day in range(1, 29)]
    assert curve == sorted(curve)
    assert len(set(curve)) > 20, "a near-continuous curve, not a handful of buckets"


def test_the_cycle_wraps_around_the_salary_day() -> None:
    """A customer paid on the 25th is flush on the 26th, not starving."""
    late_payer = replace(CUSTOMER, salary_day=25)
    assert world.days_since_salary(at(26), late_payer.salary_day) == 1
    assert world.balance_hazard(late_payer, MANDATE, at(26)) < world.balance_hazard(
        late_payer, MANDATE, at(20)
    )


def test_a_large_debit_bounces_more_than_a_small_one() -> None:
    small = replace(MANDATE, amount_paise=paise(149))
    large = replace(MANDATE, amount_paise=paise(4_999))
    assert world.balance_hazard(CUSTOMER, small, SLOT) < world.balance_hazard(CUSTOMER, large, SLOT)


def test_a_long_clean_history_lowers_the_balance_hazard() -> None:
    """Stable income shows up as paid cycles. The build plan's ``k_recency`` term."""
    fresh = replace(MANDATE, paid_count=0)
    seasoned = replace(MANDATE, paid_count=12)
    assert world.balance_hazard(CUSTOMER, seasoned, SLOT) < world.balance_hazard(
        CUSTOMER, fresh, SLOT
    )


def test_the_reliability_discount_saturates() -> None:
    """A customer who has paid 8 cycles and one who has paid 80 are the same customer."""
    params = WorldParams()
    at_cap = replace(MANDATE, paid_count=params.reliability_cycles)
    far_past = replace(MANDATE, paid_count=params.reliability_cycles * 10)
    assert world.balance_hazard(CUSTOMER, at_cap, SLOT) == world.balance_hazard(
        CUSTOMER, far_past, SLOT
    )


# ------------------------------------------------------- how much of it each rail feels


def test_a_card_mandate_barely_feels_an_empty_account() -> None:
    """The rail difference is a mechanism, not a fitted constant.

    A UPI Autopay debit lands on the savings account the instant it is presented; a
    card mandate draws on a credit line, which pays whether or not the customer has
    cash that day. Without this term every rail inherits the same depletion curve
    and card mandates fail at 8% instead of the cited 2-3%.
    """
    on_card = world.balance_hazard(CUSTOMER, replace(MANDATE, method="card_mandate"), SLOT)
    on_upi = world.balance_hazard(CUSTOMER, MANDATE, SLOT)
    assert on_card < on_upi / 5


def test_the_rails_are_ordered_by_how_directly_they_touch_the_account() -> None:
    """UPI debits the account now, netbanking debits it in the day's batch, a card
    does not debit it at all. Any other ordering would contradict the docstring the
    constants carry, and ``sim/validate_realism.py`` would report a world nobody
    described."""
    hazard = {
        method: world.balance_hazard(CUSTOMER, replace(MANDATE, method=method), SLOT)
        for method in world.METHODS
    }
    assert hazard["upi_autopay"] > hazard["netbanking"] > hazard["card_mandate"]


@pytest.mark.parametrize("method", world.METHODS)
def test_every_rail_declares_its_exposure(method: str) -> None:
    """A rail added to ``METHODS`` without an exposure entry must fail here, not with
    a ``KeyError`` halfway through generating a dataset."""
    assert 0.0 <= WorldParams().balance_exposure_for(method) <= 1.0


def test_exposure_scales_the_hazard_rather_than_replacing_it() -> None:
    """It is one multiplier on the end of the curve, so a rail at zero exposure has
    no balance hazard at all and a rail at full exposure is unchanged."""
    params = WorldParams()
    full = world.balance_hazard(CUSTOMER, MANDATE, SLOT, params)

    immune = replace(params, balance_exposure=(("upi_autopay", 0.0),))
    assert world.balance_hazard(CUSTOMER, MANDATE, SLOT, immune) == 0.0

    half = replace(params, balance_exposure=(("upi_autopay", 0.5),))
    assert world.balance_hazard(CUSTOMER, MANDATE, SLOT, half) == pytest.approx(full / 2)


def test_a_card_still_feels_the_cycle_in_the_same_direction() -> None:
    """Flatter, not flat.

    The realism gate asserts cards track the salary cycle *less* than UPI. That check
    would be satisfied trivially by a card hazard pinned at zero, which is not what is
    claimed — a credit line absorbs a bad month, it does not abolish one.
    """
    card = replace(MANDATE, method="card_mandate")
    assert world.balance_hazard(CUSTOMER, card, at(PAYDAY)) < world.balance_hazard(
        CUSTOMER, card, at(26)
    )


# ------------------------------------------------------- the nudge

# The one assumed constant in the world, so the tests here are about its *shape*
# rather than its size: which hazard it touches, how long it lasts, and that it
# cannot rescue a mandate that is already dead. The size is a stated assumption and
# docs/EVALUATION.md reports the arm results across a range of it instead.


def test_a_nudge_lowers_the_balance_hazard_and_only_the_balance_hazard() -> None:
    """A message can make a customer top up. It cannot restart a bank."""
    nudged_at = SLOT - timedelta(hours=12)
    context = replace(CONTEXT, nudged_at=nudged_at)

    quiet = hazards(CUSTOMER, MANDATE, CONTEXT)
    told = hazards(CUSTOMER, MANDATE, context)

    assert told.balance < quiet.balance
    assert told.technical == quiet.technical
    assert told.authorization == quiet.authorization


def test_the_nudge_effect_expires() -> None:
    """Beyond the window the customer has either topped up or has not, and the
    message is no longer the reason either way."""
    params = WorldParams()
    inside = world.balance_hazard(
        CUSTOMER, MANDATE, SLOT, nudged_at=SLOT - timedelta(hours=params.nudge_effect_hours)
    )
    outside = world.balance_hazard(
        CUSTOMER,
        MANDATE,
        SLOT,
        nudged_at=SLOT - timedelta(hours=params.nudge_effect_hours + 1),
    )
    unnudged = world.balance_hazard(CUSTOMER, MANDATE, SLOT)

    assert inside < unnudged
    assert outside == unnudged


def test_a_nudge_sent_after_the_debit_cannot_have_funded_it() -> None:
    """Including one sent at the same instant. A zero-hour gap is the same mistake
    with a rounding error hiding it."""
    unnudged = world.balance_hazard(CUSTOMER, MANDATE, SLOT)
    assert world.balance_hazard(CUSTOMER, MANDATE, SLOT, nudged_at=SLOT) == unnudged
    assert (
        world.balance_hazard(CUSTOMER, MANDATE, SLOT, nudged_at=SLOT + timedelta(hours=1))
        == unnudged
    )


def test_a_nudge_does_not_resurrect_a_dead_mandate() -> None:
    """BD_hard is an authorisation that no longer exists. Telling the customer about
    an invoice does not re-register their mandate, and a world where it did would
    teach the policy to spend messages on revoked mandates."""
    dead_cycle = world.death_cycle(MANDATE) + 5
    context = replace(CONTEXT, cycle_number=dead_cycle, nudged_at=SLOT - timedelta(hours=12))
    outcome = resolve(CUSTOMER, MANDATE, context)
    assert outcome.mechanism == world.MANDATE_DEAD
    assert outcome.p_success == 0.0


def test_the_nudge_is_not_in_the_oracle_key() -> None:
    """So "the same retry, but they had been told" is one coin compared against two
    thresholds, not two unrelated flips. Without this the measured value of a nudge
    would be its effect plus the variance of a fresh draw, and at these hazard sizes
    the second term is the larger one.
    """
    told = replace(CONTEXT, nudged_at=SLOT - timedelta(hours=12))
    assert oracle_key(MANDATE, told) == oracle_key(MANDATE, CONTEXT)


def test_a_nudge_can_flip_an_outcome_but_only_a_balance_one() -> None:
    """The end-to-end statement, on the population a nudge is actually for: a large
    debit against a thin account, late in the salary cycle, on a fresh mandate.

    Over that population telling people recovers money, and every single invoice it
    recovers is one that had failed for lack of funds — never a dead mandate, never a
    bank that was down.
    """
    stretched = replace(CUSTOMER, monthly_headroom_paise=paise(2_000))
    late = datetime(2026, 9, 28, 2, 0, tzinfo=IST)  # 27 days after payday
    flipped = 0
    for index in range(400):
        mandate = replace(
            MANDATE, subscription_id=f"sub_{index:04d}", amount_paise=paise(1_499), paid_count=0
        )
        context = replace(CONTEXT, invoice_id=f"inv_{index:04d}", cycle_number=2, execute_at=late)
        quiet = resolve(stretched, mandate, context)
        told = counterfactual(stretched, mandate, context, nudged_at=late - timedelta(hours=12))
        if quiet.captured != told.captured:
            assert not quiet.captured and told.captured
            assert quiet.mechanism == world.BALANCE
            flipped += 1
    assert flipped > 0, "a nudge that never changes an outcome is not a mechanism"


# ------------------------------------------------------- congestion and outages


def test_peak_hours_raise_the_technical_hazard() -> None:
    """Which is why OC-215-A bans execution in them."""
    peak = replace(CONTEXT, execute_at=at(14, hour=11))
    quiet = replace(CONTEXT, execute_at=at(14, hour=2))
    assert world.technical_hazard(MANDATE, peak) > world.technical_hazard(MANDATE, quiet)


def test_peak_hours_do_not_touch_the_balance() -> None:
    """Congestion is a network effect. Retrying at 11:00 does not make a customer's
    account any emptier, and a world that pretended otherwise would hand the model a
    correlation that does not exist in the real one."""
    assert world.balance_hazard(CUSTOMER, MANDATE, at(14, hour=11)) == world.balance_hazard(
        CUSTOMER, MANDATE, at(14, hour=2)
    )


def test_retrying_into_a_live_outage_is_punished() -> None:
    """A TD is an outage with a lifetime in hours. Retrying inside it burns a legal
    attempt on a bank that has not come back — the mistake the policy must learn to
    stop making, which it can only do if the world charges for it."""
    outage_at = SLOT - timedelta(hours=1)
    during = replace(CONTEXT, last_technical_failure_at=outage_at)
    clean = replace(CONTEXT, last_technical_failure_at=None)
    assert world.technical_hazard(MANDATE, during) > world.technical_hazard(MANDATE, clean)


def test_waiting_out_the_outage_removes_the_penalty() -> None:
    params = WorldParams()
    stale = SLOT - timedelta(hours=params.outage_persistence_hours + 1)
    waited = replace(CONTEXT, last_technical_failure_at=stale)
    clean = replace(CONTEXT, last_technical_failure_at=None)
    assert world.technical_hazard(MANDATE, waited) == world.technical_hazard(MANDATE, clean)


def test_banks_differ_but_stay_put() -> None:
    factors = {bank: world.bank_factor(bank) for bank in ("HDFC", "SBI", "ICICI", "AXIS")}
    assert len(set(factors.values())) == len(factors), "banks must be distinguishable"
    assert world.bank_factor("HDFC") == factors["HDFC"], "and stable across calls"
    assert all(0.4 < f < 1.6 for f in factors.values())


# ------------------------------------------------------- dead mandates


def test_a_dead_mandate_never_succeeds() -> None:
    certain_death = WorldParams(
        death_hazard_per_cycle=(
            ("upi_autopay", 0.999),
            ("card_mandate", 0.999),
            ("netbanking", 0.999),
        )
    )
    outcome = resolve(CUSTOMER, MANDATE, CONTEXT, certain_death)
    assert outcome.captured is False
    assert outcome.root_cause is RootCause.BD_HARD
    assert outcome.p_success == 0.0
    assert outcome.mechanism == world.MANDATE_DEAD


def test_a_dead_mandate_stays_dead_in_later_cycles() -> None:
    """Death is absorbing. A revoked mandate does not un-revoke next month."""
    params = WorldParams()
    dies_at = world.death_cycle(MANDATE, params)
    for cycle in range(dies_at, dies_at + 6):
        assert world.is_mandate_dead(MANDATE, cycle, params) is True


def test_a_mandate_is_alive_before_its_death_cycle() -> None:
    params = WorldParams()
    dies_at = world.death_cycle(MANDATE, params)
    for cycle in range(1, dies_at):
        assert world.is_mandate_dead(MANDATE, cycle, params) is False


def test_no_mandate_dies_before_its_first_cycle() -> None:
    """A ``death_cycle`` of 0 would make every mandate vacuously dead."""
    for n in range(500):
        mandate = replace(MANDATE, subscription_id=f"sub_{n:04d}")
        assert world.death_cycle(mandate) >= 1


def test_hard_failures_are_rail_appropriate() -> None:
    """A UPI mandate does not fail because a card expired."""
    upi_reasons = {reason for (_, _, _, reason), _ in world._HARD_FAILURES["upi_autopay"]}
    card_reasons = {reason for (_, _, _, reason), _ in world._HARD_FAILURES["card_mandate"]}
    assert "card_expired" not in upi_reasons
    assert "invalid_vpa" not in card_reasons
    assert "card_expired" in card_reasons


@pytest.mark.parametrize("method", world.METHODS)
def test_every_rail_has_a_hard_failure_catalogue(method: str) -> None:
    assert world._HARD_FAILURES[method]


# ------------------------------------------------------- the survival composition


def test_survival_is_multiplicative_across_independent_hazards() -> None:
    h = hazards(CUSTOMER, MANDATE, CONTEXT)
    expected = (1 - h.technical) * (1 - h.balance) * (1 - h.authorization)
    assert h.p_success == pytest.approx(expected)


def test_the_outcome_carries_the_probability_that_governed_it() -> None:
    """Ground truth the model never sees, kept so the evaluation can check
    calibration against true probabilities and not only against outcomes."""
    outcome = resolve(CUSTOMER, MANDATE, CONTEXT)
    assert outcome.p_success == pytest.approx(hazards(CUSTOMER, MANDATE, CONTEXT).p_success)
    assert 0.0 <= outcome.p_success <= 1.0


def test_p_success_stays_in_bounds_across_the_whole_parameter_surface() -> None:
    for method in world.METHODS:
        for day in (1, 14, 28):
            for hour in (2, 11, 19, 23):
                mandate = replace(MANDATE, method=method, amount_paise=paise(50_000))
                context = replace(CONTEXT, execute_at=at(day, hour))
                assert 0.0 <= hazards(CUSTOMER, mandate, context).p_success <= 1.0


def _certain_failure() -> WorldParams:
    """A world where the authorization hazard is 1.0.

    Deliberately *not* the balance hazard: that one is scaled by amount/headroom, so
    setting it to 1.0 does not make failure certain — a ₹499 debit against ₹12,000 of
    headroom still lands at ~0.04. Authorization is the one hazard that takes no
    scaling, which makes it the honest way to force a failure.
    """
    return WorldParams(
        authorization_base=(("upi_autopay", 1.0), ("card_mandate", 1.0), ("netbanking", 1.0))
    )

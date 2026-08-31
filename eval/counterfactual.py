"""Replaying every arm against the same coin flips.

Each closed test-cohort invoice whose original charge failed is rewound to the moment
that failure became known, and each of the four arms is then given the invoice and left
to work it to a conclusion. The legacy retries that actually happened are discarded for
every arm — including arm C, which reproduces them exactly, and that reproduction is
:func:`~eval.tests.test_counterfactual` 's strongest check that this harness replays the
world faithfully rather than approximately.

**Why this is paired and not merely repeated.** ``sim.world.oracle_key`` is
``subscription | invoice | attempt_number | action | IST-hour`` — no ``run_id`` and no
``arm``. Two arms that present the same invoice as attempt 2 in the same hour draw the
*same* uniform, so the difference between their results is policy and never luck. That
turns a comparison that would need thousands of replications into one that needs one,
and it is what makes the bootstrap in :mod:`eval.bootstrap` a paired bootstrap over
subscriptions rather than an independent one.

**Why the replay starts at attempt 2 and not attempt 1.** The original charge is not a
decision — no policy chose to make it, the billing schedule did. Rewinding past it would
mean re-drawing an outcome the dataset has already committed to and the model has already
trained on. Every arm therefore inherits the same failure, the same root cause, and the
same one spent attempt, which is exactly the position a recovery system is handed in
production.

**Why only closed invoices.** ``at_risk`` invoices are current: the generator truncated
their dunning at ``AS_OF`` because the clock had not reached the later slots. Replaying
them forward would mean scoring arms on attempts dated into the future, which is a
different and much weaker claim than "here is what would have happened". They are the
worklist the agent runs on in the demo; they are not the evaluation.

**The guardrail is evaluated on every action, by the harness, for every arm.** Arms B and
C never consult it — see :mod:`eval.arms` — but their actions are judged by it all the
same, and an action that executes despite a non-``APPROVE`` verdict is recorded as a
compliance violation and still allowed to happen. A baseline that cannot break the rule
cannot demonstrate what breaking it is worth.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from compliance.guardrail import ActionKind, ActionRequest, GuardrailDecision, evaluate
from compliance.non_peak_window import is_non_peak
from compliance.result import Verdict
from compliance.root_cause import RootCause, classify
from eval.arms import MAX_ATTEMPTS, Arm, Move, Situation, all_arms
from ml.features import BankMethodRates, Candidate, PriorState
from ml.policy import DEFAULT_POLICY, PolicyParams
from ml.scorer import Scorer
from sim.generate import AttemptRow, CustomerRow, Dataset, InvoiceRow, SubscriptionRow
from sim.world import (
    DEFAULT_PARAMS,
    AttemptContext,
    Customer,
    Mandate,
    WorldParams,
    oracle_key,
    resolve,
)

#: How long after a presentment fails before the recovery system is asked what to do.
#: An hour: a webhook lands, a batch picks it up. Short enough that arms B and C — whose
#: schedules are computed from ``charge_at`` and not from this moment — are never asked to
#: act at a slot their own cron would already have passed.
DECISION_LAG_HOURS = 1.0

#: How long the harness waits after a nudge when the arm expresses no preference.
DEFAULT_RESUME_AFTER_NUDGE_HOURS = 12.0

#: Hard stop on the decision loop. The attempt cap bounds a well-behaved arm at four
#: presentments plus a nudge; this bounds a misbehaving one, so a future arm that
#: proposes nudges forever fails loudly here instead of hanging a batch run.
MAX_STEPS = 12

#: Invoice statuses whose dunning window has fully elapsed. See the module docstring.
CLOSED_STATUSES = frozenset({"recovered", "written_off"})

#: Written into ``payment_attempts.action`` for every replayed presentment. It must be
#: the string the generator used, because ``action`` is in the oracle key: a replay that
#: said "dunning_retry" would ask the world a question no historical row ever asked and
#: would silently unpair the comparison.
RETRY_ACTION = "retry"

#: Recorded on the audit row for a nudge. Real SMS is not sent — TRAI DLT registration
#: makes it neither practical nor compliant inside this project — so the channel is
#: simulated while the consent gate in front of it is real.
NUDGE_CHANNEL = "simulated_sms"


# --------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class Step:
    """One decision, its verdict, and — if money moved — what the world did about it.

    Carries everything three tables need, because a decision and its consequence are one
    fact and splitting them here would mean re-joining them in every consumer.
    """

    arm: str
    invoice_id: str
    subscription_id: str
    step_index: int
    #: When the arm was asked. Distinct from ``execute_at``: a cron decides its whole
    #: schedule at T+0 and acts later.
    decided_at: datetime
    kind: ActionKind
    execute_at: datetime | None
    rationale: str
    decision: GuardrailDecision
    #: Scored alternatives, ``decisions.candidate_set``. Empty for arms that score none —
    #: which is itself the finding about them.
    candidate_set: tuple[dict[str, Any], ...] = ()
    expected_value_paise: float | None = None
    #: The model's calibrated probability for the action taken. Arm D only.
    calibrated_prob: float | None = None
    #: The ``payment_attempts`` row, when this step presented the mandate.
    attempt: AttemptRow | None = None
    recovered_paise: int = 0

    @property
    def executed(self) -> bool:
        """Whether the action actually happened, verdict notwithstanding."""
        return self.kind in (ActionKind.RETRY, ActionKind.NUDGE)

    @property
    def approved(self) -> bool:
        return self.decision.verdict is Verdict.APPROVE

    @property
    def violation(self) -> bool:
        """An action that moved money or sent a message without an APPROVE verdict.

        This is the entire definition, and it is deliberately blind to which arm did it.
        Arm D cannot produce one because its candidate generator only ever returns
        approved actions; that is a property of the policy, proved here rather than
        asserted.
        """
        return self.executed and not self.approved

    @property
    def outcome(self) -> str:
        """``audit_log.outcome``."""
        if self.kind is ActionKind.ESCALATE:
            return "escalated"
        if self.kind is ActionKind.WRITE_OFF:
            return "blocked"
        if self.attempt is not None:
            return "recovered" if self.attempt.outcome == "captured" else "failed"
        return "deferred"  # a nudge: something was sent, nothing has resolved yet


@dataclass(frozen=True, slots=True)
class InvoiceReplay:
    """One arm's complete handling of one invoice."""

    arm: str
    invoice_id: str
    subscription_id: str
    amount_paise: int
    steps: tuple[Step, ...]

    @property
    def recovered_paise(self) -> int:
        return sum(step.recovered_paise for step in self.steps)

    @property
    def compliant_recovered_paise(self) -> int:
        """Money recovered by a presentment the guardrail approved.

        The split exists because "recovered ₹X" and "recovered ₹X legally" are different
        claims and only one of them is bankable. Reported alongside the raw figure, never
        instead of it — hiding what a rule-breaking baseline actually collected would make
        the comparison less honest, not more.
        """
        return sum(step.recovered_paise for step in self.steps if step.approved)

    @property
    def attempts(self) -> int:
        return sum(step.attempt is not None for step in self.steps)

    @property
    def legal_attempts(self) -> int:
        return sum(step.attempt is not None and step.approved for step in self.steps)

    @property
    def nudges(self) -> int:
        return sum(step.kind is ActionKind.NUDGE for step in self.steps)

    @property
    def violations(self) -> int:
        return sum(step.violation for step in self.steps)

    @property
    def escalated(self) -> bool:
        return any(step.kind is ActionKind.ESCALATE for step in self.steps)

    @property
    def written_off(self) -> bool:
        return any(step.kind is ActionKind.WRITE_OFF for step in self.steps)

    @property
    def recovered(self) -> bool:
        return self.recovered_paise > 0


@dataclass(frozen=True, slots=True)
class SubscriptionTotals:
    """One arm's numbers for one subscription. The bootstrap's atom."""

    recovered_paise: int = 0
    compliant_recovered_paise: int = 0
    attempts: int = 0
    legal_attempts: int = 0
    violations: int = 0


@dataclass(frozen=True, slots=True)
class ArmResult:
    """One arm, over the whole replay set."""

    arm: str
    label: str
    replays: tuple[InvoiceReplay, ...]

    @property
    def invoices_evaluated(self) -> int:
        return len(self.replays)

    @property
    def recovered_paise(self) -> int:
        return sum(r.recovered_paise for r in self.replays)

    @property
    def compliant_recovered_paise(self) -> int:
        return sum(r.compliant_recovered_paise for r in self.replays)

    @property
    def attempts_consumed(self) -> int:
        return sum(r.attempts for r in self.replays)

    @property
    def legal_attempts_consumed(self) -> int:
        return sum(r.legal_attempts for r in self.replays)

    @property
    def nudges_sent(self) -> int:
        return sum(r.nudges for r in self.replays)

    @property
    def escalations(self) -> int:
        return sum(r.escalated for r in self.replays)

    @property
    def written_off(self) -> int:
        return sum(r.written_off for r in self.replays)

    @property
    def compliance_violations(self) -> int:
        return sum(r.violations for r in self.replays)

    @property
    def invoices_recovered(self) -> int:
        return sum(r.recovered for r in self.replays)

    @property
    def paise_per_legal_attempt(self) -> float | None:
        """**The headline.** Compliant rupees recovered per legal attempt spent.

        ``None`` — rendered "—" — when the arm spent no attempts at all, which is arm A
        by construction. A zero would read as "tried and failed" and it did not try.

        Both halves of the ratio are restricted to approved actions on purpose. An arm
        that recovers by presenting inside a peak window has not earned a better ratio
        for doing so, and the attempt it burned doing it is not evidence about how
        efficiently it uses the budget it is legally allowed.
        """
        if not self.legal_attempts_consumed:
            return None
        return self.compliant_recovered_paise / self.legal_attempts_consumed

    def by_subscription(self) -> dict[str, SubscriptionTotals]:
        """Everything the bootstrap resamples, aggregated to its resampling unit.

        The subscription rather than the invoice: one mandate's cycles share a customer,
        a salary day and a bank, so they succeed and fail together. See
        :mod:`eval.bootstrap`.
        """
        totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
        for replay in self.replays:
            bucket = totals[replay.subscription_id]
            bucket[0] += replay.recovered_paise
            bucket[1] += replay.compliant_recovered_paise
            bucket[2] += replay.attempts
            bucket[3] += replay.legal_attempts
            bucket[4] += replay.violations
        return {key: SubscriptionTotals(*values) for key, values in totals.items()}

    def steps(self):
        for replay in self.replays:
            yield from replay.steps


@dataclass(frozen=True, slots=True)
class EvalRun:
    """Everything one execution of the harness produced."""

    run_id: str
    cohort: str
    dataset_version: str
    dataset_fingerprint: str
    model_version: str
    world_params: WorldParams
    policy_params: PolicyParams
    arms: tuple[ArmResult, ...]
    #: Every subscription in the cohort, including the ones that never missed a payment
    #: and so never entered the replay set. This is the bootstrap's sampling frame —
    #: see :mod:`eval.bootstrap` for why the frame is not the replay set.
    cohort_subscription_ids: tuple[str, ...]

    @property
    def invoices_evaluated(self) -> int:
        return max((arm.invoices_evaluated for arm in self.arms), default=0)


# --------------------------------------------------------------------------- indexing


@dataclass(frozen=True, slots=True)
class ReplayCase:
    """One invoice, rewound, with every fact all four arms start from.

    Built once and shared, so no arm can be advantaged by a different reading of the
    history. If two arms diverge, it is because they decided differently.
    """

    subscription: SubscriptionRow
    customer: CustomerRow
    invoice: InvoiceRow
    #: The original charge. Failed, by construction of the replay set.
    first_charge: AttemptRow
    #: Successful cycles on this mandate strictly before this invoice — what
    #: ``sim.world`` reads as the reliability signal. Derived rather than taken from
    #: ``SubscriptionRow.paid_count``, which is the mandate's *final* count and would
    #: hand every early cycle a reliability discount it had not yet earned.
    paid_count: int
    #: The subscription's observed attempts with this invoice's legacy retries removed,
    #: ascending. The base every arm's own attempts are appended to.
    base_history: tuple[AttemptRow, ...]

    @property
    def root_cause(self) -> RootCause:
        return classify(
            error_code=self.first_charge.error_code,
            error_source=self.first_charge.error_source,
            error_step=self.first_charge.error_step,
            error_reason=self.first_charge.error_reason,
        )

    @property
    def mandate(self) -> Mandate:
        return Mandate(
            subscription_id=self.subscription.subscription_id,
            method=self.subscription.method,
            bank=self.subscription.bank,
            amount_paise=self.subscription.amount_paise,
            paid_count=self.paid_count,
        )

    @property
    def world_customer(self) -> Customer:
        return Customer(
            customer_id=self.customer.customer_id,
            salary_day=self.customer.salary_day,
            monthly_headroom_paise=self.customer.monthly_headroom_paise,
        )


def build_cases(dataset: Dataset, *, cohort: str = "test") -> tuple[ReplayCase, ...]:
    """Select and rewind every invoice the evaluation runs on.

    Ordered by ``(subscription_id, cycle_number)`` rather than left in dataset order, so
    the run is reproducible independently of how the generator happened to emit rows.
    """
    customers = {c.customer_id: c for c in dataset.customers}
    subscriptions = {s.subscription_id: s for s in dataset.subscriptions if s.cohort == cohort}

    attempts_by_subscription: dict[str, list[AttemptRow]] = defaultdict(list)
    attempts_by_invoice: dict[str, list[AttemptRow]] = defaultdict(list)
    for attempt in dataset.attempts:
        if attempt.subscription_id in subscriptions and attempt.observed:
            attempts_by_subscription[attempt.subscription_id].append(attempt)
            attempts_by_invoice[attempt.invoice_id].append(attempt)

    invoices_by_subscription: dict[str, list[InvoiceRow]] = defaultdict(list)
    for invoice in dataset.invoices:
        if invoice.subscription_id in subscriptions:
            invoices_by_subscription[invoice.subscription_id].append(invoice)

    cases: list[ReplayCase] = []
    for subscription_id in sorted(subscriptions):
        subscription = subscriptions[subscription_id]
        cycles = sorted(invoices_by_subscription[subscription_id], key=lambda i: i.cycle_number)
        history = sorted(attempts_by_subscription[subscription_id], key=lambda a: a.attempted_at)

        paid_before = 0
        for invoice in cycles:
            rows = sorted(attempts_by_invoice[invoice.invoice_id], key=lambda a: a.attempt_number)
            first = rows[0] if rows else None

            if (
                first is not None
                and first.attempt_number == 1
                and first.outcome == "failed"
                and invoice.status in CLOSED_STATUSES
            ):
                cases.append(
                    ReplayCase(
                        subscription=subscription,
                        customer=customers[subscription.customer_id],
                        invoice=invoice,
                        first_charge=first,
                        paid_count=paid_before,
                        base_history=tuple(
                            row
                            for row in history
                            if not (row.invoice_id == invoice.invoice_id and row.attempt_number > 1)
                        ),
                    )
                )

            if invoice.status in ("paid", "recovered"):
                paid_before += 1

    return tuple(cases)


def split_by_region(
    dataset: Dataset, cases: tuple[ReplayCase, ...]
) -> tuple[tuple[ReplayCase, ...], tuple[ReplayCase, ...]]:
    """``(observed, censored)`` — the two regions, by the legacy policy's own filter.

    A censored invoice is one the legacy dunning job declined to retry: under ₹500, or on
    a netbanking mandate. The model has no training labels there, and ``docs/EVALUATION.md``
    reports its calibration separately for that reason (ECE 0.0342 observed against 0.4420
    censored). The same split is applied to the arms, because an advantage that exists only
    where the training data already was is a much weaker claim than one that survives into
    the region the data never covered — and arm C, which is the policy that did the
    censoring, recovers nothing there by construction.
    """
    censored_invoices = {a.invoice_id for a in dataset.attempts if not a.observed}
    observed = tuple(c for c in cases if c.invoice.invoice_id not in censored_invoices)
    censored = tuple(c for c in cases if c.invoice.invoice_id in censored_invoices)
    return observed, censored


# --------------------------------------------------------------------------- the replay


@dataclass
class _Walk:
    """Mutable state carried through one arm's handling of one invoice."""

    attempts_used: int
    root_cause: RootCause
    now: datetime
    nudged_at: datetime | None = None
    last_technical_failure_at: datetime | None = None
    history: list[AttemptRow] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)


def _request(
    case: ReplayCase, move: Move, *, attempts_used: int, execute_at: datetime
) -> ActionRequest:
    return ActionRequest(
        kind=move.kind,
        execute_at=execute_at,
        amount_paise=case.invoice.amount_paise,
        mcc_category=case.subscription.mcc_category,
        attempts_used=attempts_used,
        root_cause=case.root_cause,
        charge_at=case.invoice.charge_at,
        notice_sent_at=case.invoice.notice_sent_at,
        consent_status=case.customer.consent_status,
        consent_updated_at=case.customer.consent_updated_at,
        # The debit presented against this customer is itself the transaction that
        # opens the messaging window — the same reading ml/policy.py takes, and it has
        # to be the same one or the policy would score a nudge the harness then refuses.
        last_transaction_at=case.invoice.charge_at,
    )


def _present(
    case: ReplayCase,
    walk: _Walk,
    *,
    run_id: str,
    arm: str,
    execute_at: datetime,
    world: WorldParams,
) -> AttemptRow:
    """Ask the oracle what happens, and build the ``payment_attempts`` row for it."""
    context = AttemptContext(
        invoice_id=case.invoice.invoice_id,
        cycle_number=case.invoice.cycle_number,
        attempt_number=walk.attempts_used + 1,
        action=RETRY_ACTION,
        execute_at=execute_at,
        last_technical_failure_at=walk.last_technical_failure_at,
        nudged_at=walk.nudged_at,
    )
    outcome = resolve(case.world_customer, case.mandate, context, world)

    return AttemptRow(
        attempt_id=f"att_{run_id}_{arm}_{case.invoice.invoice_id}_{context.attempt_number}",
        invoice_id=case.invoice.invoice_id,
        subscription_id=case.subscription.subscription_id,
        attempt_number=context.attempt_number,
        attempted_at=execute_at,
        is_non_peak=is_non_peak(execute_at),
        action=RETRY_ACTION,
        amount_paise=case.invoice.amount_paise,
        outcome="captured" if outcome.captured else "failed",
        **outcome.error_fields,
        observed=True,
        oracle_seed=oracle_key(case.mandate, context),
        p_success=outcome.p_success,
    )


def replay_invoice(
    arm: Arm,
    case: ReplayCase,
    *,
    run_id: str,
    world: WorldParams = DEFAULT_PARAMS,
) -> InvoiceReplay:
    """Hand one arm one rewound invoice and let it work to a conclusion."""
    walk = _Walk(
        attempts_used=1,
        root_cause=case.root_cause,
        now=case.first_charge.attempted_at + timedelta(hours=DECISION_LAG_HOURS),
        last_technical_failure_at=(
            case.first_charge.attempted_at
            if case.first_charge.root_cause_class == RootCause.TD
            else None
        ),
        history=list(case.base_history),
    )

    for step_index in range(MAX_STEPS):
        prior = PriorState.before(
            Candidate(
                attempt_number=walk.attempts_used + 1,
                action=RETRY_ACTION,
                execute_at=walk.now,
                amount_paise=case.invoice.amount_paise,
            ),
            invoice_id=case.invoice.invoice_id,
            history=tuple(sorted(walk.history, key=lambda a: a.attempted_at)),
        )
        move = arm.move(
            Situation(
                subscription=case.subscription,
                customer=case.customer,
                invoice=case.invoice,
                attempts_used=walk.attempts_used,
                root_cause=walk.root_cause,
                prior=prior,
                now=walk.now,
                nudged_at=walk.nudged_at,
                paid_count=case.paid_count,
            )
        )
        execute_at = move.execute_at or walk.now
        decision = evaluate(
            _request(case, move, attempts_used=walk.attempts_used, execute_at=execute_at),
            now=walk.now,
        )
        plan = move.plan
        chosen = plan.chosen if plan is not None else None
        step = Step(
            arm=arm.arm,
            invoice_id=case.invoice.invoice_id,
            subscription_id=case.subscription.subscription_id,
            step_index=step_index,
            decided_at=walk.now,
            kind=move.kind,
            execute_at=move.execute_at,
            rationale=move.rationale,
            decision=decision,
            candidate_set=(tuple(c.to_dict() for c in plan.candidates) if plan is not None else ()),
            expected_value_paise=chosen.expected_value_paise if chosen else None,
            calibrated_prob=chosen.p_success if chosen else None,
        )

        if move.kind in (ActionKind.ESCALATE, ActionKind.WRITE_OFF):
            walk.steps.append(step)
            break

        if move.kind is ActionKind.NUDGE:
            walk.steps.append(step)
            walk.nudged_at = execute_at
            walk.now = move.resume_at or execute_at + timedelta(
                hours=DEFAULT_RESUME_AFTER_NUDGE_HOURS
            )
            continue

        if walk.attempts_used >= MAX_ATTEMPTS:
            # Unreachable: every arm stops itself at the cap, and arm D is stopped by
            # the guardrail as well. Loud rather than silently dropped, because a
            # harness that quietly swallowed a fifth presentment would report an arm as
            # compliant on the strength of its own bug.
            raise RuntimeError(
                f"arm {arm.arm} proposed presentment {walk.attempts_used + 1} on "
                f"{case.invoice.invoice_id}; NPCI allows {MAX_ATTEMPTS} and the schema "
                f"stores {MAX_ATTEMPTS}. The arm, not the harness, must stop."
            )

        attempt = _present(
            case, walk, run_id=run_id, arm=arm.arm, execute_at=execute_at, world=world
        )
        walk.history.append(attempt)
        walk.attempts_used += 1
        captured = attempt.outcome == "captured"
        walk.steps.append(
            replace(
                step,
                attempt=attempt,
                recovered_paise=case.invoice.amount_paise if captured else 0,
            )
        )
        if captured:
            break

        if attempt.root_cause_class == RootCause.TD:
            walk.last_technical_failure_at = attempt.attempted_at
        walk.root_cause = classify(
            error_code=attempt.error_code,
            error_source=attempt.error_source,
            error_step=attempt.error_step,
            error_reason=attempt.error_reason,
        )
        walk.now = execute_at + timedelta(hours=DECISION_LAG_HOURS)
    else:
        raise RuntimeError(
            f"arm {arm.arm} took {MAX_STEPS} steps on {case.invoice.invoice_id} without "
            f"reaching a terminal action. An arm that never stops is a bug, not a policy."
        )

    return InvoiceReplay(
        arm=arm.arm,
        invoice_id=case.invoice.invoice_id,
        subscription_id=case.subscription.subscription_id,
        amount_paise=case.invoice.amount_paise,
        steps=tuple(walk.steps),
    )


def run_evaluation(
    dataset: Dataset,
    *,
    scorer: Scorer,
    rates: BankMethodRates,
    run_id: str,
    model_version: str,
    cohort: str = "test",
    world: WorldParams = DEFAULT_PARAMS,
    policy: PolicyParams = DEFAULT_POLICY,
    cases: tuple[ReplayCase, ...] | None = None,
) -> EvalRun:
    """Run all four arms over the same rewound invoices.

    ``cases`` overrides the replay set, for the sliced runs the report needs — observed
    region against censored region, where the question is whether the model's advantage
    survives into the region its training data never saw. The sampling frame stays the
    whole cohort regardless of the slice, because a narrower slice does not make the
    customer base narrower; it just means more of the frame contributes zero.
    """
    cases = build_cases(dataset, cohort=cohort) if cases is None else cases
    results = tuple(
        ArmResult(
            arm=arm.arm,
            label=arm.label,
            replays=tuple(replay_invoice(arm, case, run_id=run_id, world=world) for case in cases),
        )
        for arm in all_arms(scorer=scorer, rates=rates, params=policy)
    )
    return EvalRun(
        run_id=run_id,
        cohort=cohort,
        dataset_version=dataset.dataset_version,
        dataset_fingerprint=dataset.fingerprint(),
        model_version=model_version,
        world_params=world,
        policy_params=policy,
        arms=results,
        cohort_subscription_ids=tuple(
            sorted(s.subscription_id for s in dataset.subscriptions if s.cohort == cohort)
        ),
    )

"""The four policies, behind one interface.

Each arm answers the same question — *what do you do about this invoice, right now* —
and the harness treats all four identically: it evaluates the compliance guardrail on
whatever they propose, records the verdict, and then asks the oracle what happens. The
arms differ only in what they propose.

**The guardrail is the referee, never the player.** Arms A and D consult it and act
within it. Arms B and C do not consult it at all — a fixed-offset cron has never heard
of OC-215-A, which is exactly why production systems break new rules. The harness
evaluates it on their actions anyway and records the verdict, so the violations-by-arm
column is a measurement of what the baselines actually did rather than a penalty
invented by the scorer. That is also why the baselines are allowed to commit violations
instead of being blocked: a baseline that cannot break the rule cannot demonstrate the
cost of breaking it.

**Why B respects the attempt cap.** It ignores the execution window, and that is where
its violations come from. It does not attempt a fifth presentment, because
``payment_attempts.attempt_number`` is CHECKed to 1..4 — an over-cap arm could not be
stored, and an evaluation whose baseline cannot be written to the database is not an
evaluation. What a fifth attempt would cost is demonstrated by the guardrail's own tests
and by the failure drill, where it belongs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from compliance.guardrail import ActionKind
from compliance.root_cause import RootCause
from ml.features import BankMethodRates, PriorState
from ml.policy import DEFAULT_POLICY, InvoiceState, Plan, PolicyParams, decide
from ml.scorer import Scorer
from sim.generate import CustomerRow, InvoiceRow, SubscriptionRow
from sim.legacy_policy import DEFAULT_LEGACY, LegacyParams, retry_schedule
from sim.world import Mandate

#: NPCI OC-215-A. Mirrored from ``compliance/npci_retry_cap.py``, which is the authority.
MAX_ATTEMPTS = 4


@dataclass(frozen=True, slots=True)
class Situation:
    """What an arm is told at one decision point.

    Deliberately the merchant's view and nothing more: no oracle probability, no future
    outcome, no knowledge of what another arm did. An arm handed any of those would be
    scoring itself.
    """

    subscription: SubscriptionRow
    customer: CustomerRow
    invoice: InvoiceRow
    #: Legal attempts already spent on this invoice, the original charge included.
    attempts_used: int
    #: Classification of the most recent failure — deterministic, from the error fields.
    root_cause: RootCause
    prior: PriorState
    now: datetime
    nudged_at: datetime | None
    #: Successful cycles on this mandate *before* this invoice. Needed because the
    #: world's reliability discount reads it, and the subscription row carries the
    #: mandate's final count rather than its count at the time.
    paid_count: int

    @property
    def mandate(self) -> Mandate:
        return Mandate(
            subscription_id=self.subscription.subscription_id,
            method=self.subscription.method,
            bank=self.subscription.bank,
            amount_paise=self.subscription.amount_paise,
            paid_count=self.paid_count,
        )


@dataclass(frozen=True, slots=True)
class Move:
    """One arm's answer. ``execute_at`` is ``None`` only for actions that do nothing."""

    kind: ActionKind
    execute_at: datetime | None
    #: Written verbatim into the audit row. Every arm has to be able to explain itself,
    #: or the drill-down can only justify the winner and the comparison is not honest.
    rationale: str
    #: Arm D only: the full scored candidate set, for ``decisions.candidate_set``.
    plan: Plan | None = None
    #: When the arm wants to be asked again. Only meaningful after a nudge, where the
    #: arm — not the harness — knows how long it intends to wait before presenting.
    #: ``None`` lets the harness apply its own lag.
    resume_at: datetime | None = None


class Arm(Protocol):
    """The whole interface. Four policies, one question."""

    arm: str
    label: str

    def move(self, situation: Situation) -> Move: ...


@dataclass(frozen=True, slots=True)
class NeverRetry:
    """Arm A — the over-conservative floor.

    Recovers nothing and consumes no legal attempts, which makes it the only arm whose
    rupees-per-legal-attempt is undefined rather than merely bad. Reported as "—".
    It exists to bound the comparison from below: any arm that fails to beat "do
    nothing, ask a human" has not earned the attempt budget it spent.
    """

    arm: str = "A"
    label: str = "Never retry, always escalate"

    def move(self, situation: Situation) -> Move:
        return Move(
            kind=ActionKind.ESCALATE,
            execute_at=situation.now,
            rationale="arm A: every failed invoice goes to a human, unconditionally",
        )


@dataclass(frozen=True, slots=True)
class RetryEverything:
    """Arm B — the naive baseline, and an illegal one.

    Retries T+1, T+2, T+3 at the hour the original charge was presented, because that is
    what "just retry it tomorrow" means when nobody has thought about the clock. Whether
    that lands inside NPCI's peak window is decided by when the merchant's billing runs,
    which is to say by the data — the violations this arm commits are not engineered into
    it.

    **What it actually does, measured rather than assumed.** On this dataset B commits no
    window violations at all: the generator bills at hours that all happen to fall outside
    10:00-13:00 and 17:00-21:30, so retrying at the charge's own hour inherits its
    legality. Every one of B's violations is ``bd_hard_not_retryable`` — it re-presents
    mandates that are revoked, closed or hard-declined, because "retry everything" does
    not read the decline reason. Those presentments recovered exactly zero paise, which is
    the whole argument against the arm: it spends attempts, and legality, on invoices that
    were never going to pay.

    This was found by running the arm and reading the violation breakdown, not by design.
    Recorded in ``docs/WHAT_BROKE.md``.
    """

    arm: str = "B"
    label: str = "Retry everything to the cap, any time"

    def move(self, situation: Situation) -> Move:
        if situation.attempts_used >= MAX_ATTEMPTS:
            return Move(
                kind=ActionKind.WRITE_OFF,
                execute_at=situation.now,
                rationale=f"arm B: {MAX_ATTEMPTS} attempts made, nothing left to try",
            )
        offset = situation.attempts_used  # attempt 2 goes at T+1, and so on
        charge_at = situation.invoice.charge_at
        return Move(
            kind=ActionKind.RETRY,
            execute_at=charge_at + timedelta(days=offset),
            rationale=(
                f"arm B: fixed T+{offset} retry at the charge's own hour "
                f"({charge_at.strftime('%H:%M')} IST), no window check"
            ),
        )


@dataclass(frozen=True, slots=True)
class Legacy:
    """Arm C — what the merchant does today.

    Reuses ``sim/legacy_policy.py`` rather than reimplementing it, so the arm and the
    generator that censored the training data are provably the same policy. Two
    consequences fall out of that and both are the point:

    * Its urgent branch fires at 11:30 IST, inside the morning peak. Illegal since
      1 August 2025 and still scheduled, because the constant lives in a config file
      nobody read.
    * It refuses to touch cheap invoices and netbanking mandates at all, so on the
      censored region it writes off immediately and recovers nothing. That is not a
      strawman; it is the reason the censored region is censored.

    It is also the arm that shows why raw recovery is the wrong headline. C appears to
    recover far more than its compliant total, because 90% of its recovery arrives through
    presentments the guardrail refuses — overwhelmingly that 11:30 slot. Hold it to the
    law and almost all of it disappears. An evaluation scored on rupees alone would rank
    this arm second; scored on rupees it was allowed to collect, it ranks last of the
    three arms that present at all.
    """

    params: LegacyParams = DEFAULT_LEGACY
    arm: str = "C"
    label: str = "Legacy fixed-offset dunning"

    def move(self, situation: Situation) -> Move:
        schedule = retry_schedule(situation.mandate, situation.invoice.charge_at, self.params)
        wanted = situation.attempts_used + 1
        for retry in schedule:
            if retry.attempt_number == wanted:
                return Move(
                    kind=ActionKind.RETRY,
                    execute_at=retry.execute_at,
                    rationale=retry.rationale,
                )
        reason = (
            "the legacy job was never extended to this invoice"
            if not schedule
            else "the legacy schedule is exhausted"
        )
        return Move(
            kind=ActionKind.WRITE_OFF,
            execute_at=situation.now,
            rationale=f"arm C: {reason}",
        )


@dataclass(frozen=True, slots=True)
class Winback:
    """Arm D — the submission.

    Holds no logic of its own. It is an adapter from :class:`Situation` to
    ``ml.policy.decide``, which is the point: the thing being evaluated is the same code
    the agent runs in production, not an evaluation-only reimplementation of it that
    could quietly diverge.
    """

    scorer: Scorer
    rates: BankMethodRates
    params: PolicyParams = DEFAULT_POLICY
    arm: str = "D"
    label: str = "Winback: calibrated model + cost policy + guardrail"

    def move(self, situation: Situation) -> Move:
        plan = decide(
            InvoiceState(
                subscription=situation.subscription,
                customer=situation.customer,
                invoice=situation.invoice,
                attempts_used=situation.attempts_used,
                root_cause=situation.root_cause,
                prior=situation.prior,
                now=situation.now,
                nudged_at=situation.nudged_at,
            ),
            scorer=self.scorer,
            rates=self.rates,
            params=self.params,
        )
        chosen = plan.chosen
        resume_at = None
        if chosen.kind is ActionKind.NUDGE and chosen.execute_at is not None:
            # The lookahead priced the nudge on the assumption that the presentment
            # follows it by exactly this lead. Resuming at any other moment would
            # execute a plan the policy never scored.
            resume_at = chosen.execute_at + timedelta(hours=self.params.nudge_lead_hours)
        return Move(
            kind=chosen.kind,
            execute_at=chosen.execute_at,
            rationale=chosen.decision.authorizing_rule,
            plan=plan,
            resume_at=resume_at,
        )


def all_arms(*, scorer: Scorer, rates: BankMethodRates, params: PolicyParams = DEFAULT_POLICY):
    """The four arms in report order, which is also worst-to-best on legality."""
    return (
        NeverRetry(),
        RetryEverything(),
        Legacy(),
        Winback(scorer=scorer, rates=rates, params=params),
    )

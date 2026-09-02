"""Choosing what to do about one at-risk invoice.

The model says how likely a presentment is to succeed. The guardrail says which
presentments are legal. Neither of them decides anything; this does, and it decides by
maximising expected rupees **under the remaining attempt budget**, which is a different
and much more interesting problem than picking the slot with the highest probability.

**Why greedy argmax is wrong here.** NPCI gives an invoice four presentments, ever. A
policy that retries whenever expected value is positive spends the budget on the first
three merely-acceptable opportunities and arrives at the best one with nothing left. The
value of a retry is therefore not ``p x amount - cost``; it is that, plus the value of
whatever remains available *after* it fails:

    V(budget, t) = max over legal actions of
        retry at slot s:  p(s) * reward - cost + (1 - p(s)) * V(budget - 1, s + gap)
        nudge, then act:  -nudge_cost + V(budget, t + lead)      [once, if consent allows]
        write off:        0

Recursion, not a heuristic, because the recursion is the definition. It terminates on the
attempt cap, so the tree is at most three levels deep and is enumerated exactly rather
than sampled.

**Why ``burned_attempt_paise`` is not charged here.** ``ml/evaluate.py`` prices a wasted
legal attempt at a flat 1,200 paise for the rupee confusion matrix, where a single-shot
classification has no way to express what the attempt was worth. This function does have
a way: the ``(1 - p) * V(budget - 1, ...)`` term *is* the option that was spent, valued at
what the remaining budget is actually worth for this specific invoice. Charging the flat
constant as well would count the same loss twice, and would make the policy most
reluctant to retry exactly where retrying is most valuable.

**Escalation is not an economic choice.** It is never scored against retries, because it
recovers nothing this system can measure — a human takes the invoice and what happens
next is outside the batch. It happens when the guardrail says a human must authorise the
debit, and only then. This keeps the division of labour clean: the law decides when a
person is required; the model never argues its way past that and never argues its way
into it either.

**What the policy believes about nudges, and why it is deliberately wrong.**
``sim/world.py`` gives a message a specific effect on the balance hazard.
:class:`PolicyParams` states a *different* belief, in a different functional form, and
the two are never reconciled. Handing the policy the world's own constant would make arm
D's advantage an artifact of knowing the answer; the belief here is what a merchant might
plausibly assume from published dunning numbers, and the sensitivity table in
``docs/EVALUATION.md`` moves the world's constant while leaving this one fixed. What that
table measures is not "does the nudge work" but "how much does the policy lose by being
wrong about it", which is the question a merchant is actually exposed to.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from compliance.guardrail import ActionKind, ActionRequest, GuardrailDecision, evaluate
from compliance.non_peak_window import next_slots
from compliance.result import Verdict
from compliance.root_cause import RootCause
from ml.evaluate import DEFAULT_COSTS, CostMatrix
from ml.features import BankMethodRates, Candidate, PriorState, features_for
from ml.scorer import Scorer
from sim.generate import CustomerRow, InvoiceRow, SubscriptionRow


@dataclass(frozen=True, slots=True)
class PolicyParams:
    """The policy's own beliefs and operating choices.

    Split from ``WorldParams`` on purpose and never sourced from it. Every value here is
    something a merchant would have to decide without knowing the truth.
    """

    #: How many legal slots to consider at each decision point. The guardrail hands back
    #: three; taking more would widen the tree without widening the choice much, since
    #: consecutive non-peak anchors on the same night score almost identically.
    horizon_slots: int = 3

    #: **A belief, not a measurement.** The multiplier the policy applies to *total*
    #: failure probability for a retry that follows a message. The world applies its own,
    #: different multiplier to the balance hazard alone — so this belief is wrong in form
    #: as well as in size, crediting a message for technical declines and revoked
    #: mandates it cannot possibly help. 0.80 is a deliberately weak reading of the
    #: 15-30% dunning-recovery figures the industry quotes for whole programmes.
    assumed_nudge_failure_multiplier: float = 0.80

    #: How long the policy assumes a message keeps working. Shorter than the world's
    #: window, so the policy sometimes declines to bank an effect that is still there.
    assumed_nudge_effect_hours: float = 48.0

    #: How long to wait after messaging before presenting again. A customer needs time to
    #: move money; a debit an hour after the SMS tests nothing.
    nudge_lead_hours: float = 12.0

    #: Cost of one simulated message. Small, but not zero — a policy that nudges for free
    #: would nudge on every invoice and the consent gate would be the only thing stopping
    #: it, which is not a business decision, it is an absence of one.
    nudge_cost_paise: int = 25

    #: Minimum gap between presentments. An operational choice, not a legal one: NPCI caps
    #: how many times, not how often. Retrying four times in one night would technically
    #: be legal and would tell the merchant almost nothing new each time.
    retry_gap_hours: float = 12.0

    #: Hard ceiling on tree expansion. The cap makes the tree small by construction; this
    #: exists so a future change that widens the horizon cannot quietly make the batch
    #: run take hours.
    max_nodes: int = 512


DEFAULT_POLICY = PolicyParams()


@dataclass(frozen=True, slots=True)
class InvoiceState:
    """One at-risk invoice, at one moment, with everything a decision needs.

    Carries no outcome and no oracle probability — the policy is entitled to exactly what
    a merchant would have at the moment of deciding, and the type is the enforcement.
    """

    subscription: SubscriptionRow
    customer: CustomerRow
    invoice: InvoiceRow
    #: Legal attempts already spent on this invoice, history included.
    attempts_used: int
    #: The classification of the most recent failure. Deterministic, from Razorpay's own
    #: error fields — never a model output.
    root_cause: RootCause
    prior: PriorState
    #: The moment the decision is being made.
    now: datetime
    #: When, if ever, this customer has already been told about this invoice.
    nudged_at: datetime | None = None

    @property
    def last_transaction_at(self) -> datetime:
        """What opens the transactional messaging window.

        The debit presented against the customer is itself the transaction: it is the
        service relationship, and it is the thing the message would be about. Using the
        last *successful* charge instead would silence exactly the customer whose
        payments have been failing, which inverts the rule's purpose.
        """
        return self.invoice.charge_at


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """One considered action, its verdict, and what it was thought to be worth.

    Denied candidates are kept. ``decisions.candidate_set`` is meant to answer "what else
    did it consider", and an audit trail that records only the winner cannot distinguish
    a policy that chose well from one that had no other option.
    """

    kind: ActionKind
    execute_at: datetime | None
    #: Whether this branch messages the customer before presenting.
    nudge_first: bool
    #: Calibrated P(captured), after the policy's nudge belief. ``None`` when the action
    #: presents nothing.
    p_success: float | None
    #: Expected rupees, in paise, including everything the remaining budget is worth.
    expected_value_paise: float
    decision: GuardrailDecision

    @property
    def allowed(self) -> bool:
        return self.decision.verdict is Verdict.APPROVE

    def to_dict(self) -> dict[str, Any]:
        """The serialisable view. Every consumer of this turns it into JSON.

        ``expected_value_paise`` becomes ``None`` when the candidate was ruled out, because
        the sentinel for that is ``-inf`` and **JSON has no infinity**. ``json.dumps`` will
        happily emit the bare token ``-Infinity``, which is not RFC 8259 and which
        PostgreSQL's ``jsonb`` rejects outright — that is how a whole ``decisions`` row
        went missing on the first agent batch, leaving an ``audit_log`` entry pointing at
        a decision that was never written. Nothing is lost by the substitution: the
        ``verdict`` and ``stop_reason`` on this same row already say why the candidate
        scored negative infinity, which is the part a reviewer reads.

        The sentinel itself stays a float inside the policy, where the argmax needs it to
        compare. Only the wire format changes.
        """
        expected = self.expected_value_paise
        return {
            "kind": str(self.kind),
            "execute_at": self.execute_at.isoformat() if self.execute_at else None,
            "nudge_first": self.nudge_first,
            "p_success": self.p_success,
            "expected_value_paise": round(expected, 2) if isfinite(expected) else None,
            "ruled_out": not isfinite(expected),
            "verdict": str(self.decision.verdict),
            "authorizing_rule": self.decision.authorizing_rule,
            "stop_reason": self.decision.stop_reason,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """What the policy decided, and everything it considered on the way."""

    chosen: ScoredCandidate
    candidates: tuple[ScoredCandidate, ...]
    #: How many tree nodes were expanded. Recorded because a lookahead whose cost nobody
    #: measures is a lookahead that will one day be quietly too expensive.
    nodes_expanded: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "chosen": self.chosen.to_dict(),
            "candidate_set": [candidate.to_dict() for candidate in self.candidates],
            "nodes_expanded": self.nodes_expanded,
        }


def _request(
    state: InvoiceState, kind: ActionKind, execute_at: datetime, attempts_used: int
) -> ActionRequest:
    return ActionRequest(
        kind=kind,
        execute_at=execute_at,
        amount_paise=state.invoice.amount_paise,
        mcc_category=state.subscription.mcc_category,
        attempts_used=attempts_used,
        root_cause=state.root_cause,
        charge_at=state.invoice.charge_at,
        notice_sent_at=state.invoice.notice_sent_at,
        consent_status=state.customer.consent_status,
        consent_updated_at=state.customer.consent_updated_at,
        last_transaction_at=state.last_transaction_at,
    )


def _advance(prior: PriorState, *, root_cause: RootCause, at: datetime) -> PriorState:
    """The prior state the policy would be in if a presentment at ``at`` failed.

    Built directly rather than through :meth:`PriorState.before`, because there is no
    attempt row to fold in: this is a hypothetical failure inside a lookahead, and
    inventing an ``AttemptRow`` for it would mean inventing an outcome and an error code
    that the world never produced.

    The assumed failure keeps the current root cause. That is the policy's belief and it
    is a mild one — a mandate failing for insufficient funds is far likelier to fail that
    way again than to switch mechanisms — but it is a belief, and the deeper the lookahead
    the more of the value rests on it.
    """
    return replace(
        prior,
        prior_root_cause=str(root_cause),
        prior_failures_this_invoice=prior.prior_failures_this_invoice + 1,
        last_attempt_at=at,
        lifetime_attempts=prior.lifetime_attempts + 1,
        lifetime_failures=prior.lifetime_failures + 1,
    )


class _Search:
    """The expected-value recursion, memoised over ``(attempts_used, nudged, slot)``."""

    def __init__(
        self,
        state: InvoiceState,
        *,
        scorer: Scorer,
        rates: BankMethodRates,
        params: PolicyParams,
        costs: CostMatrix,
    ) -> None:
        self.state = state
        self.scorer = scorer
        self.rates = rates
        self.params = params
        self.costs = costs
        self.reward = costs.margin * state.invoice.amount_paise
        self.nodes = 0
        # Keyed on everything that can change a node's value, including the two prior
        # fields the lookahead mutates. The slot arithmetic happens to make those
        # redundant today; keying on them anyway means a future change to the gap or the
        # horizon cannot silently start returning another node's answer.
        self._memo: dict[tuple[int, datetime | None, datetime, int, datetime | None], float] = {}

    # -- scoring ---------------------------------------------------------------

    def probability(
        self, *, attempts_used: int, at: datetime, prior: PriorState, nudged_at: datetime | None
    ) -> float:
        """Calibrated P(captured) for a presentment, adjusted by the nudge belief.

        The model has no nudge feature and could not have one: the historical data
        contains no nudges, so there is nothing in it to learn the effect from. The
        adjustment is therefore applied *outside* the model, where it is visible as an
        assumption rather than buried as a coefficient.
        """
        row = features_for(
            subscription=self.state.subscription,
            customer=self.state.customer,
            invoice=self.state.invoice,
            candidate=Candidate(
                attempt_number=attempts_used + 1,
                action="retry",
                execute_at=at,
                amount_paise=self.state.invoice.amount_paise,
            ),
            prior=prior,
            rates=self.rates,
        )
        p = self.scorer.score_one(row)

        if nudged_at is not None:
            hours = (at - nudged_at).total_seconds() / 3600
            if 0 < hours <= self.params.assumed_nudge_effect_hours:
                p = 1.0 - (1.0 - p) * self.params.assumed_nudge_failure_multiplier
        return p

    # -- the recursion ---------------------------------------------------------

    def value(
        self,
        *,
        attempts_used: int,
        at: datetime,
        prior: PriorState,
        nudged_at: datetime | None,
    ) -> float:
        """Expected paise from playing this invoice out optimally from here."""
        key = (
            attempts_used,
            nudged_at,
            at,
            prior.prior_failures_this_invoice,
            prior.last_attempt_at,
        )
        if key in self._memo:
            return self._memo[key]
        if self.nodes >= self.params.max_nodes:
            # Writing off is the value-zero action, so truncating to it can only
            # understate a branch. A truncation that could overstate one would make the
            # cap a source of bad decisions rather than a bound on cost.
            return 0.0
        self.nodes += 1

        best = 0.0  # write-off: stop working the invoice, gain and lose nothing further
        for candidate in self.candidates(
            attempts_used=attempts_used, at=at, prior=prior, nudged_at=nudged_at
        ):
            best = max(best, candidate.expected_value_paise)

        self._memo[key] = best
        return best

    def candidates(
        self,
        *,
        attempts_used: int,
        at: datetime,
        prior: PriorState,
        nudged_at: datetime | None,
    ) -> list[ScoredCandidate]:
        """Every action legally available at this node, each priced.

        The candidate set is generated by the compliance layer and priced by the model,
        in that order. There is no path by which an illegal action acquires a value and
        then has to be argued down.
        """
        out: list[ScoredCandidate] = []

        for slot in next_slots(at, n=self.params.horizon_slots):
            request = _request(self.state, ActionKind.RETRY, slot, attempts_used)
            decision = evaluate(request, now=at)
            if decision.verdict is not Verdict.APPROVE:
                out.append(
                    ScoredCandidate(
                        kind=ActionKind.RETRY,
                        execute_at=slot,
                        nudge_first=False,
                        p_success=None,
                        expected_value_paise=float("-inf"),
                        decision=decision,
                    )
                )
                continue

            p = self.probability(
                attempts_used=attempts_used, at=slot, prior=prior, nudged_at=nudged_at
            )
            failed_value = self.value(
                attempts_used=attempts_used + 1,
                at=slot + timedelta(hours=self.params.retry_gap_hours),
                prior=_advance(prior, root_cause=self.state.root_cause, at=slot),
                nudged_at=nudged_at,
            )
            expected = p * self.reward + (1 - p) * failed_value - self.costs.attempt_cost_paise
            out.append(
                ScoredCandidate(
                    kind=ActionKind.RETRY,
                    execute_at=slot,
                    nudge_first=False,
                    p_success=p,
                    expected_value_paise=expected,
                    decision=decision,
                )
            )

        if nudged_at is None:
            request = _request(self.state, ActionKind.NUDGE, at, attempts_used)
            decision = evaluate(request, now=at)
            if decision.verdict is Verdict.APPROVE:
                after = at + timedelta(hours=self.params.nudge_lead_hours)
                expected = (
                    self.value(attempts_used=attempts_used, at=after, prior=prior, nudged_at=at)
                    - self.params.nudge_cost_paise
                )
            else:
                expected = float("-inf")
            out.append(
                ScoredCandidate(
                    kind=ActionKind.NUDGE,
                    execute_at=at,
                    nudge_first=True,
                    p_success=None,
                    expected_value_paise=expected,
                    decision=decision,
                )
            )

        return out


def _write_off(state: InvoiceState) -> ScoredCandidate:
    request = _request(state, ActionKind.WRITE_OFF, state.now, state.attempts_used)
    return ScoredCandidate(
        kind=ActionKind.WRITE_OFF,
        execute_at=state.now,
        nudge_first=False,
        p_success=None,
        expected_value_paise=0.0,
        decision=evaluate(request, now=state.now),
    )


def _escalation(state: InvoiceState, decision: GuardrailDecision) -> ScoredCandidate:
    """The action the guardrail compelled, carrying the rule that compelled it."""
    return ScoredCandidate(
        kind=ActionKind.ESCALATE,
        execute_at=state.now,
        nudge_first=False,
        p_success=None,
        expected_value_paise=0.0,
        decision=decision,
    )


def decide(
    state: InvoiceState,
    *,
    scorer: Scorer,
    rates: BankMethodRates,
    params: PolicyParams = DEFAULT_POLICY,
    costs: CostMatrix = DEFAULT_COSTS,
) -> Plan:
    """Choose one action for one at-risk invoice.

    Three outcomes, in strict precedence:

    1. **Escalate**, if the guardrail returned ``ESCALATE_HUMAN`` on a presentment — an
       AFA ceiling was crossed and a person has to authorise it. Not scored against
       anything; the law is not a term in an expected value.
    2. **The highest-value legal action**, if any is worth more than doing nothing.
    3. **Write off.** Zero is a real option and it wins whenever every legal action is
       worth less than nothing — which is the outcome that makes ``₹ per legal
       attempt`` move, and the one a policy tuned on raw recovery would never take.
    """
    search = _Search(state, scorer=scorer, rates=rates, params=params, costs=costs)
    considered = search.candidates(
        attempts_used=state.attempts_used,
        at=state.now,
        prior=state.prior,
        nudged_at=state.nudged_at,
    )

    escalated = next((c for c in considered if c.decision.verdict is Verdict.ESCALATE_HUMAN), None)
    if escalated is not None:
        chosen = _escalation(state, escalated.decision)
        return Plan(
            chosen=chosen,
            candidates=(*considered, chosen),
            nodes_expanded=search.nodes,
        )

    write_off = _write_off(state)
    everything = (*considered, write_off)
    chosen = max(everything, key=lambda c: c.expected_value_paise)
    return Plan(chosen=chosen, candidates=everything, nodes_expanded=search.nodes)

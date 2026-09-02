"""The executor that asks the oracle instead of the rail.

This is the lane the 500-subscription batch runs on, and it is the only lane in which
"measured money recovered" is a measurement rather than a figure of speech. Test-mode
Razorpay moves no money; a real merchant account would move money that nobody involved
is entitled to spend on a hackathon. The seeded counterfactual world in ``sim.world``
is the third option, and it is the one that makes the claim checkable: the coin for a
given ``(subscription, invoice, attempt_number, action, IST-hour)`` was flipped before
any policy asked, so what this adapter returns is not generated in response to the
agent's choice.

**What this adapter owns, and what it does not.** It owns *physics continuity* — the two
facts that change the world's answer without being anybody's decision: when this invoice
last drew a technical decline (outages persist, so a retry ten minutes after one is
worth less than a retry the next morning), and when the customer was last told the debit
had failed (a nudged retry is scored against the same coin at a different threshold).
Both are consequences of what this executor has already done, so it is the thing that
knows them. It owns nothing else. Attempts consumed, whether the cap is exhausted, what
to try next — all of that lives upstream, and this class cannot see it.

**It does not consult the oracle's probability.** ``AttemptOutcome.p_success`` comes back
with every call and is carried into the attempt row for the calibration report, which is
evaluation machinery. It is never read here and never returned to the agent. An executor
that could see the true probability would be an executor that could quietly decline the
attempts it knew would fail, and every recovery number after that would be a measurement
of the simulator reading its own answer key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from agent.adapters.base import (
    SIMULATED_CHANNEL,
    AdapterError,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    Outcome,
)
from compliance.guardrail import ActionKind
from compliance.non_peak_window import is_non_peak
from compliance.root_cause import RootCause
from eval.counterfactual import ReplayCase, build_cases
from sim.generate import AttemptRow, Dataset, build_dataset
from sim.world import DEFAULT_PARAMS, AttemptContext, WorldParams, oracle_key, resolve

#: The action string that goes into the oracle key for a presentment. Must match
#: ``eval.counterfactual.RETRY_ACTION`` exactly — if the agent's retries keyed on a
#: different string they would draw different coins from the same world, and the batch
#: would no longer be comparable to arm D of the evaluation.
RETRY_ACTION = "retry"

#: Where the built ``payment_attempts`` row is handed back on the result. Only the
#: simulated lane produces one: the live lane has no presentment API, so it has no
#: attempt to record, and inventing one would be the exact fabrication this project
#: is meant to be an argument against.
ATTEMPT_ROW = "attempt_row"


@dataclass
class _Physics:
    """Per-invoice continuity. Reset whenever the batch rewinds."""

    last_technical_failure_at: datetime | None = None
    nudged_at: datetime | None = None


@dataclass
class SimulatedAdapter:
    """Executes against ``sim.world``. Deterministic under a fixed dataset."""

    mode: ExecutionMode = field(default=ExecutionMode.SIMULATED, init=False)

    cases: dict[str, ReplayCase]
    world: WorldParams = DEFAULT_PARAMS
    run_id: str = "batch"
    arm: str = "D"

    _physics: dict[str, _Physics] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def from_dataset(
        cls,
        dataset: Dataset | None = None,
        *,
        cohort: str = "test",
        run_id: str = "batch",
        arm: str = "D",
        world: WorldParams = DEFAULT_PARAMS,
    ) -> SimulatedAdapter:
        """Index the frozen world by invoice.

        Defaults to the ``test`` cohort — the same 800 subscriptions arm D was scored
        on, and deliberately not the training cohort. A batch run against invoices the
        model was fitted on would produce a number nobody should quote.
        """
        dataset = build_dataset() if dataset is None else dataset
        return cls(
            cases={case.invoice.invoice_id: case for case in build_cases(dataset, cohort=cohort)},
            world=world,
            run_id=run_id,
            arm=arm,
        )

    def _case(self, invoice_id: str) -> ReplayCase:
        case = self.cases.get(invoice_id)
        if case is None:
            raise AdapterError(
                f"invoice {invoice_id} is not in the replay index. The simulated lane can "
                "only execute against invoices the frozen dataset actually contains."
            )
        return case

    def present(self, request: ExecutionRequest) -> ExecutionResult:
        """Ask the world what happens if this mandate is presented here, now."""
        if request.kind is not ActionKind.RETRY:
            raise AdapterError(f"present() is for retries, not {request.kind}")

        case = self._case(request.invoice_id)
        physics = self._physics.setdefault(request.invoice_id, _Physics())

        context = AttemptContext(
            invoice_id=case.invoice.invoice_id,
            cycle_number=case.invoice.cycle_number,
            attempt_number=request.attempt_number,
            action=RETRY_ACTION,
            execute_at=request.execute_at,
            last_technical_failure_at=physics.last_technical_failure_at,
            nudged_at=physics.nudged_at,
        )
        outcome = resolve(case.world_customer, case.mandate, context, self.world)

        attempt = AttemptRow(
            attempt_id=(
                f"att_{self.run_id}_{self.arm}_{case.invoice.invoice_id}_{request.attempt_number}"
            ),
            invoice_id=case.invoice.invoice_id,
            subscription_id=case.subscription.subscription_id,
            attempt_number=request.attempt_number,
            attempted_at=request.execute_at,
            is_non_peak=is_non_peak(request.execute_at),
            action=RETRY_ACTION,
            amount_paise=case.invoice.amount_paise,
            outcome="captured" if outcome.captured else "failed",
            **outcome.error_fields,
            observed=True,
            oracle_seed=oracle_key(case.mandate, context),
            p_success=outcome.p_success,
        )

        # Recorded before the result is returned, so the next presentment on this
        # invoice sees the outage this one just hit. An adapter that updated this
        # afterwards, or not at all, would make every retry look like the first.
        if outcome.root_cause is RootCause.TD:
            physics.last_technical_failure_at = request.execute_at

        return ExecutionResult(
            outcome=Outcome.RECOVERED if outcome.captured else Outcome.FAILED,
            execution_mode=ExecutionMode.SIMULATED,
            recovered_paise=case.invoice.amount_paise if outcome.captured else 0,
            error=(
                None
                if outcome.captured
                else {k: v for k, v in outcome.error_fields.items() if v is not None}
            ),
            detail=(
                f"presented attempt {request.attempt_number} at "
                f"{request.execute_at:%Y-%m-%d %H:%M IST}: {attempt.outcome}"
            ),
            metadata={ATTEMPT_ROW: attempt},
        )

    def nudge(self, request: ExecutionRequest) -> ExecutionResult:
        """Tell the customer. Consumes no legal attempt, and moves no money.

        Recorded as ``deferred`` rather than ``failed``: a nudge that lands has not
        recovered anything yet, and scoring it as a failure would understate a policy
        whose whole point is that the *next* presentment is worth more.
        """
        if request.kind is not ActionKind.NUDGE:
            raise AdapterError(f"nudge() is for nudges, not {request.kind}")

        self._case(request.invoice_id)
        self._physics.setdefault(request.invoice_id, _Physics()).nudged_at = request.execute_at

        return ExecutionResult(
            outcome=Outcome.DEFERRED,
            execution_mode=ExecutionMode.SIMULATED,
            channel=SIMULATED_CHANNEL,
            detail=f"nudge delivered to {request.customer_hash} at {request.execute_at:%H:%M IST}",
        )

    def reset(self) -> None:
        """Forget physics continuity. Called between batches, never inside one."""
        self._physics.clear()

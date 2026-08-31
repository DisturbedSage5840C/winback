"""Confidence intervals for a comparison that is paired by construction.

Every arm faced the same invoices and, because ``sim.world.oracle_key`` contains no
``run_id`` and no ``arm``, drew the same coins for the same questions. The uncertainty
that remains is therefore **not** simulation noise — rerunning the harness reproduces
every rupee exactly. It is sampling uncertainty about the 800 test subscriptions: would
a different 800 customers have told the same story?

That question is answered by resampling the customers, not the invoices. A subscription
contributes several cycles, all sharing one salary day, one bank, one mandate and one
customer's headroom, so its invoices fail and recover together. Resampling invoices would
treat those as independent draws and would report an interval several times too narrow —
the standard cluster-bootstrap mistake, and an easy one to make here because the invoice
is the unit everything else in this project is measured on.

**Why the difference gets its own interval.** Arm-level intervals overlap far more than
the paired difference does, because both arms move together when a lucky subscription is
resampled in. Reading "the intervals overlap, so the arms are indistinguishable" off two
marginal intervals is wrong whenever the arms are positively correlated, and here they are
almost perfectly correlated by design. :func:`bootstrap_run` resamples once and
differences *within* the resample, which is the interval that actually answers "is D
better than B".

**The resampling frame is every subscription in the cohort, not only the ones that
failed.** A resampled customer base contains customers who never missed a payment, and
how many of those you get is part of the sampling variation. Restricting the frame to the
190 subscriptions with a failed invoice would condition on the very thing being sampled
and report an interval that is too narrow for the claim it is attached to.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields

from eval.counterfactual import ArmResult, EvalRun, SubscriptionTotals

#: Resamples. Ten thousand is enough that the 2.5th percentile is stable to the rupee at
#: this sample size, and cheap: each resample is a sum over 800 precomputed numbers.
DEFAULT_RESAMPLES = 10_000

#: Fixed, and recorded in ``eval_runs.seed``. An interval that moves when nobody changed
#: anything is not a claim about the data.
DEFAULT_SEED = 20260905

#: A resample reduced to one number, from that resample's column sums. Kept as named
#: functions rather than lambdas so the report can print what it measured.
#:
#: Statistics read *sums*, not the 800 rows behind them, because every statistic here is
#: a function of totals. Summing each column once per resample and sharing the result
#: across all four statistics is what keeps ten thousand resamples of four arms in
#: seconds rather than minutes.
Statistic = Callable[[Mapping[str, int]], float]

#: The columns carried per subscription, in ``SubscriptionTotals`` field order.
COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(SubscriptionTotals))


def compliant_recovered(sums: Mapping[str, int]) -> float:
    """Paise recovered by presentments the guardrail approved."""
    return float(sums["compliant_recovered_paise"])


def legal_attempts(sums: Mapping[str, int]) -> float:
    """Approved presentments made."""
    return float(sums["legal_attempts"])


def violations(sums: Mapping[str, int]) -> float:
    """Presentments the guardrail refused, counted whether or not the arm asked it."""
    return float(sums["violations"])


def paise_per_legal_attempt(sums: Mapping[str, int]) -> float:
    """The headline ratio, recomputed *inside* each resample.

    A ratio of two resampled sums, not a resampled ratio of per-subscription ratios. The
    latter would weight a subscription that made one attempt as heavily as one that made
    twelve, which is a different quantity and not the one the merchant experiences.

    Returns 0.0 for the degenerate empty-denominator resample. That case is unreachable
    for the three arms this statistic is computed for — each spends dozens of attempts
    across the frame — and the one arm where the denominator really is zero (A) is
    excluded from this statistic entirely by :func:`bootstrap_run` rather than being
    given a fabricated zero. See ``ArmResult.paise_per_legal_attempt``.
    """
    spent = sums["legal_attempts"]
    if not spent:
        return 0.0
    return sums["compliant_recovered_paise"] / spent


#: What gets an interval, in report order.
STATISTICS: tuple[tuple[str, Statistic], ...] = (
    ("compliant_recovered_paise", compliant_recovered),
    ("legal_attempts_consumed", legal_attempts),
    ("compliance_violations", violations),
    ("paise_per_legal_attempt", paise_per_legal_attempt),
)

#: Statistics that are undefined, not zero, for an arm that never presented. Reported as
#: "—" rather than given an interval around a denominator that does not exist.
RATIO_STATISTICS = frozenset({"paise_per_legal_attempt"})


@dataclass(frozen=True, slots=True)
class Interval:
    """A percentile interval and the point estimate it surrounds."""

    point: float
    low: float
    high: float
    resamples: int
    confidence: float

    @property
    def excludes_zero(self) -> bool:
        return self.low > 0 or self.high < 0

    def as_rupees(self) -> str:
        return f"₹{self.point / 100:,.0f} [{self.low / 100:,.0f}, {self.high / 100:,.0f}]"


@dataclass(frozen=True, slots=True)
class ArmIntervals:
    """Every statistic for one arm: its own interval, and its paired gap to arm D."""

    arm: str
    marginal: dict[str, Interval]
    #: ``D - this arm``, differenced inside each resample. Empty for arm D itself.
    versus_winback: dict[str, Interval]


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile.

    Written out rather than pulled from NumPy because this module is imported by the
    report generator, and a five-line function is a smaller dependency surface than an
    array library for the one call it would make.
    """
    if not sorted_values:
        return 0.0
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _draws(units: list[str], *, resamples: int, seed: int) -> list[list[int]]:
    """One shared set of resamples, as index lists, used for every arm and statistic.

    Shared on purpose: this is what makes the intervals paired. Drawing fresh
    subscriptions per arm would compare arm D on one hypothetical customer base against
    arm B on another, and the difference between them would be mostly customers.
    """
    rng = random.Random(seed)  # noqa: S311 - resampling, not a secret
    size = len(units)
    return [[rng.randrange(size) for _ in range(size)] for _ in range(resamples)]


def _columns(arm: ArmResult, units: list[str]) -> dict[str, list[int]]:
    """The arm's totals as one list per column, in the frame's order.

    Zero-filled for the subscriptions the arm never touched — a customer who never missed
    a payment is a real member of the frame contributing zero, not an absence.
    """
    per_subscription = arm.by_subscription()
    empty = SubscriptionTotals()
    rows = [per_subscription.get(unit, empty) for unit in units]
    return {column: [getattr(row, column) for row in rows] for column in COLUMNS}


def _sums(columns: dict[str, list[int]], draw: list[int]) -> dict[str, int]:
    """Column totals for one resample."""
    return {name: sum(map(column.__getitem__, draw)) for name, column in columns.items()}


def _totals(columns: dict[str, list[int]]) -> dict[str, int]:
    """Column totals for the observed sample, which is the point estimate."""
    return {name: sum(column) for name, column in columns.items()}


def _series(columns: dict[str, list[int]], draws: list[list[int]]) -> dict[str, list[float]]:
    """Every statistic's resampled distribution, from one pass over the draws."""
    out: dict[str, list[float]] = {name: [] for name, _ in STATISTICS}
    for draw in draws:
        sums = _sums(columns, draw)
        for name, statistic in STATISTICS:
            out[name].append(statistic(sums))
    return out


def _interval(values: list[float], point: float, *, resamples: int, confidence: float) -> Interval:
    values.sort()
    tail = (1 - confidence) / 2
    return Interval(
        point=point,
        low=_percentile(values, tail),
        high=_percentile(values, 1 - tail),
        resamples=resamples,
        confidence=confidence,
    )


def bootstrap_run(
    run: EvalRun,
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    confidence: float = 0.95,
) -> dict[str, ArmIntervals]:
    """Marginal and paired-against-D intervals for every arm and every statistic.

    A paired interval on ``paise_per_legal_attempt`` that excludes zero is the strongest
    single sentence this evaluation can produce, and a marginal one that does not is worth
    reporting just as plainly.
    """
    units = list(run.cohort_subscription_ids)
    draws = _draws(units, resamples=resamples, seed=seed)
    columns = {arm.arm: _columns(arm, units) for arm in run.arms}
    series = {name: _series(cols, draws) for name, cols in columns.items()}
    points = {name: _totals(cols) for name, cols in columns.items()}

    winback = series.get("D")
    winback_point = points.get("D")

    out: dict[str, ArmIntervals] = {}
    for arm in run.arms:
        mine, mine_point = series[arm.arm], points[arm.arm]
        marginal: dict[str, Interval] = {}
        paired: dict[str, Interval] = {}

        for name, statistic in STATISTICS:
            if name in RATIO_STATISTICS and not legal_attempts(mine_point):
                # Arm A. An interval around an undefined ratio would be a claim about a
                # denominator that does not exist, and the paired gap "D beats A by
                # ₹3,247 per legal attempt" would be arithmetic on nothing.
                continue
            marginal[name] = _interval(
                list(mine[name]),
                statistic(mine_point),
                resamples=resamples,
                confidence=confidence,
            )

            if winback is None or winback_point is None or arm.arm == "D":
                continue
            paired[name] = _interval(
                [d - m for d, m in zip(winback[name], mine[name], strict=True)],
                statistic(winback_point) - statistic(mine_point),
                resamples=resamples,
                confidence=confidence,
            )

        out[arm.arm] = ArmIntervals(arm=arm.arm, marginal=marginal, versus_winback=paired)
    return out

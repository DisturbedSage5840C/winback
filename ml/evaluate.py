"""Metrics for a probability, not for a decision.

Accuracy is absent on purpose. 88% of attempts capture, so predicting "always captures"
scores 88% and is worth nothing: it cannot tell a merchant which retry to spend a legal
attempt on. Every metric here answers a question that survives that baseline.

| Metric | The question it answers |
|---|---|
| ECE | when the model says 70%, does it happen 70% of the time? |
| Brier | is it both calibrated *and* sharp, or calibrated by being timid? |
| PR-AUC (failure) | can it find the minority class the money depends on? |
| ₹ cost matrix | what does being wrong actually cost, in rupees |

``_compute_ece`` in ``~/Documents/RaceJudge`` is multiclass and averages over classes;
this problem is binary, so the binary form is written out rather than adapted — the
multiclass average of a two-class problem double-counts the same bins.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

#: Ten equal-width bins, the convention ECE is normally reported under. Equal-width and
#: not equal-mass: the interesting failure is a model confident in a sparse region, and
#: equal-mass bins would dissolve exactly that region into its neighbours.
DEFAULT_BINS = 10


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """One row of the reliability diagram."""

    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_rate: float

    @property
    def gap(self) -> float:
        return self.observed_rate - self.mean_predicted


def reliability(
    y_true: np.ndarray, y_prob: np.ndarray, *, n_bins: int = DEFAULT_BINS
) -> list[ReliabilityBin]:
    """Bin predictions and measure what actually happened in each bin."""
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    # Rightmost edge is inclusive so a prediction of exactly 1.0 lands in the last bin
    # rather than falling out of the histogram entirely.
    idx = np.clip(np.digitize(y_prob, edges[1:-1], right=False), 0, n_bins - 1)

    bins: list[ReliabilityBin] = []
    for b in range(n_bins):
        mask = idx == b
        count = int(mask.sum())
        bins.append(
            ReliabilityBin(
                lower=float(edges[b]),
                upper=float(edges[b + 1]),
                count=count,
                mean_predicted=float(y_prob[mask].mean()) if count else 0.0,
                observed_rate=float(y_true[mask].mean()) if count else 0.0,
            )
        )
    return bins


def expected_calibration_error(
    y_true: np.ndarray, y_prob: np.ndarray, *, n_bins: int = DEFAULT_BINS
) -> float:
    """Count-weighted mean absolute gap between confidence and observed frequency."""
    bins = reliability(y_true, y_prob, n_bins=n_bins)
    total = sum(b.count for b in bins)
    if not total:
        return 0.0
    return sum(b.count * abs(b.gap) for b in bins) / total


def maximum_calibration_error(
    y_true: np.ndarray, y_prob: np.ndarray, *, n_bins: int = DEFAULT_BINS
) -> float:
    """The worst bin, reported next to ECE.

    A model can post a respectable ECE while being badly wrong in one sparse region,
    because ECE weights by count. That region is often exactly where a policy operates.
    """
    bins = [b for b in reliability(y_true, y_prob, n_bins=n_bins) if b.count]
    return max((abs(b.gap) for b in bins), default=0.0)


@dataclass(frozen=True, slots=True)
class CostMatrix:
    """What each kind of error costs, in paise.

    A false positive is a legal attempt spent on a debit that was going to fail: one of
    only four the mandate has, plus the gateway cost. Its true price is the *option* that
    attempt represented, which is why the retry cap makes this problem interesting at
    all. A false negative is an invoice abandoned that would have paid — the merchant
    loses the margin on it.
    """

    #: Gateway/processing cost of presenting a debit.
    attempt_cost_paise: int = 300
    #: Merchant margin on a recovered invoice, as a fraction of face value.
    margin: float = 0.25
    #: Rupee value assigned to one burned legal attempt out of the NPCI budget of four.
    burned_attempt_paise: int = 1_200


#: The one instance every default argument in this module points at. A fresh
#: ``CostMatrix()`` per call would be identical, but a single object means a rupee
#: figure in the report and a rupee figure in the policy provably came from the same
#: constants rather than from two copies that could drift.
DEFAULT_COSTS = CostMatrix()


@dataclass(frozen=True, slots=True)
class Metrics:
    """Everything reported for one model on one slice."""

    slice_name: str
    n: int
    positive_rate: float
    ece: float
    mce: float
    brier: float
    pr_auc_failure: float
    roc_auc: float
    failure_precision: float
    failure_recall: float
    threshold: float
    bins: list[ReliabilityBin] = field(default_factory=list)

    def as_row(self) -> str:
        return (
            f"{self.slice_name:22}{self.n:>8,d}{self.ece:>9.4f}{self.mce:>9.4f}"
            f"{self.brier:>9.4f}{self.pr_auc_failure:>10.4f}{self.roc_auc:>9.4f}"
        )


def evaluate(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    *,
    slice_name: str,
    threshold: float = 0.5,
    n_bins: int = DEFAULT_BINS,
) -> Metrics:
    """Score one slice.

    The minority class here is *failure*, so precision and recall are computed on
    ``1 - y``: "of the attempts we flagged as likely to fail, how many did", which is the
    question a policy deciding whether to spend an attempt actually asks.
    """
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob, dtype=np.float64)

    fail_true = 1 - y_true
    fail_prob = 1.0 - y_prob
    predicted_fail = fail_prob >= threshold

    tp = int((predicted_fail & (fail_true == 1)).sum())
    fp = int((predicted_fail & (fail_true == 0)).sum())
    fn = int((~predicted_fail & (fail_true == 1)).sum())

    # Both classes must be present for the ranking metrics to mean anything; a slice of
    # all-captures makes ROC-AUC undefined rather than perfect.
    both_classes = 0 < y_true.sum() < len(y_true)

    return Metrics(
        slice_name=slice_name,
        n=len(y_true),
        positive_rate=float(y_true.mean()) if len(y_true) else 0.0,
        ece=expected_calibration_error(y_true, y_prob, n_bins=n_bins),
        mce=maximum_calibration_error(y_true, y_prob, n_bins=n_bins),
        brier=float(brier_score_loss(y_true, y_prob)) if len(y_true) else 0.0,
        pr_auc_failure=(
            float(average_precision_score(fail_true, fail_prob)) if both_classes else 0.0
        ),
        roc_auc=float(roc_auc_score(y_true, y_prob)) if both_classes else 0.0,
        failure_precision=tp / (tp + fp) if (tp + fp) else 0.0,
        failure_recall=tp / (tp + fn) if (tp + fn) else 0.0,
        threshold=threshold,
        bins=reliability(y_true, y_prob, n_bins=n_bins),
    )


def break_even_threshold(
    amounts_paise: np.ndarray, *, costs: CostMatrix = DEFAULT_COSTS
) -> np.ndarray:
    """The probability above which attempting this invoice pays for itself.

    Attempting wins when ``p * amount * margin > (1 - p) * (attempt + burned attempt)``,
    which solves to ``p > c / (c + amount * margin)``. It is computed per row rather than
    once, because a fixed threshold applied to invoices spanning ₹149 to ₹18,392 is
    three different decisions wearing one number.

    Read the values it returns before trusting a threshold-based framing of this
    problem: on this dataset they come out near 0.02, meaning the cost matrix almost
    never says "don't". That is not a bug in the matrix. It is the finding — a retry is
    cheap and an invoice is not, so **money is not the scarce resource here; the four
    legal attempts are.** Which is exactly why Winback's policy layer maximises expected
    rupees *subject to the NPCI budget* instead of thresholding on probability.
    """
    amounts = np.asarray(amounts_paise, dtype=np.float64)
    c = float(costs.attempt_cost_paise + costs.burned_attempt_paise)
    return c / (c + amounts * costs.margin)


def rupee_confusion(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    amounts_paise: np.ndarray,
    *,
    threshold: float | np.ndarray,
    costs: CostMatrix = DEFAULT_COSTS,
) -> dict[str, float]:
    """The confusion matrix denominated in rupees rather than counts.

    Two errors that are equally frequent are not equally expensive when one of them is a
    ₹149 invoice and the other is ₹18,392. Presenting this in counts is how a model gets
    chosen that is right about the cheap cases.

    ``threshold`` may be a scalar or one value per row — see :func:`break_even_threshold`.
    """
    y_true = np.asarray(y_true).astype(int)
    amounts = np.asarray(amounts_paise, dtype=np.float64)
    will_attempt = np.asarray(y_prob, dtype=np.float64) >= threshold

    recovered = float((amounts[will_attempt & (y_true == 1)]).sum()) * costs.margin
    wasted = float(
        (will_attempt & (y_true == 0)).sum()
        * (costs.attempt_cost_paise + costs.burned_attempt_paise)
    )
    forgone = float((amounts[~will_attempt & (y_true == 1)]).sum()) * costs.margin
    correctly_skipped = int((~will_attempt & (y_true == 0)).sum())

    return {
        "margin_recovered_paise": recovered,
        "wasted_attempt_cost_paise": wasted,
        "margin_forgone_paise": forgone,
        "attempts_correctly_skipped": float(correctly_skipped),
        "net_paise": recovered - wasted,
    }

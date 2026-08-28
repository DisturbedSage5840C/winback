"""The calibration figure for ``docs/EVALUATION.md``.

Three panels, and the one that matters is the first. A reliability diagram is the only
chart in this project that is *supposed* to look boring: a line on the diagonal means
the probabilities are worth acting on. What makes it interesting here is that it is
drawn twice — once on the slice the legacy policy let the merchant observe, once on the
slice it censored — because the gap between those two curves is the honest measure of
how far off-distribution this model can be trusted.

Palette and rcParams are shared with ``sim/validate_realism.py`` deliberately: two
figures in the same document drawn from different palettes read as two documents.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from sim.validate_realism import HAIRLINE, INK, MUTED, _panel_title, _style

if TYPE_CHECKING:
    from ml.calibrate import CalibrationReport
    from ml.evaluate import Metrics

ARTIFACT = Path("docs/assets/calibration.png")

#: The observed slice keeps the brand blue used for "the system acting"; the censored
#: slice takes the same amber the realism figure gives card mandates. Checked with the
#: dataviz validator against white: ΔE 28 protan, 21 tritan, both well clear of the
#: floor, and neither is the reserved red that means "blocked".
OBSERVED, CENSORED = "#305eff", "#e06c1f"
#: The reserved status red, used here for the same thing it means everywhere else in
#: Winback: a rule refused this, and no amount of scoring well overrides that.
DISQUALIFIED = "#c81e3a"
#: Ordinal, not categorical — these are four points on one axis of "how much freedom
#: the calibrator had", so they get a ramp rather than four unrelated hues.
CALIBRATOR_RAMP = {
    "uncalibrated": "#cbd5e2",
    "temperature": "#93b4ff",
    "sigmoid": "#4d7fff",
    "isotonic": "#305eff",
}


def _reliability_panel(ax, observed: Metrics, censored: Metrics) -> None:
    """Predicted probability against realised frequency, both slices, with counts.

    Bins holding fewer than 20 rows are drawn hollow. A bin of three attempts can sit
    anywhere between 0 and 1 by luck, and a reader who cannot see that will read noise
    as miscalibration — which on the censored slice, where the whole point is that data
    is scarce, would be the wrong conclusion drawn from the right chart.
    """
    ax.plot([0, 1], [0, 1], color=HAIRLINE, linewidth=1.6, zorder=1)
    ax.annotate(
        "perfect calibration",
        xy=(0.62, 0.62),
        xytext=(6, -12),
        textcoords="offset points",
        fontsize=8,
        color=MUTED,
        rotation=38,
        rotation_mode="anchor",
    )

    for metrics, color in ((observed, OBSERVED), (censored, CENSORED)):
        bins = [b for b in metrics.bins if b.count]
        if not bins:
            continue
        xs = [b.mean_predicted for b in bins]
        ys = [b.observed_rate for b in bins]
        ax.plot(xs, ys, color=color, linewidth=2.0, zorder=3)
        thin = [b.count < 20 for b in bins]
        ax.scatter(
            [x for x, t in zip(xs, thin, strict=True) if not t],
            [y for y, t in zip(ys, thin, strict=True) if not t],
            s=42, color=color, edgecolor="white", linewidth=2.0, zorder=4,
        )
        ax.scatter(
            [x for x, t in zip(xs, thin, strict=True) if t],
            [y for y, t in zip(ys, thin, strict=True) if t],
            s=42, facecolor="white", edgecolor=color, linewidth=2.0, zorder=4,
        )

    ax.plot([], [], color=OBSERVED, linewidth=2.0,
            label=f"observed  n={observed.n:,d}  ECE {observed.ece:.3f}")
    ax.plot([], [], color=CENSORED, linewidth=2.0,
            label=f"censored  n={censored.n:,d}  ECE {censored.ece:.3f}")
    ax.scatter([], [], s=42, facecolor="white", edgecolor=MUTED, linewidth=2.0,
               label="bin holds fewer than 20 attempts")
    ax.legend(loc="lower right", handlelength=1.6)

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("predicted P(capture)")
    ax.set_ylabel("observed capture rate")
    _panel_title(
        ax,
        "Calibrated where there was data; far too pessimistic where there was none",
        # Kept under ~100 characters: the left panel is wide, but its subtitle runs
        # past the axes and into the right column's before it wraps.
        "test cohort, 10 equal-width bins — above the diagonal, the model called it a "
        "failure and got paid",
    )


def _calibrator_panel(ax, report: CalibrationReport) -> None:
    """Out-of-fold ECE of every calibrator, with its in-sample figure marked.

    Both numbers are drawn because the distance between them is the point. Isotonic's
    in-sample tick sits at zero — it can reproduce the rows it was fitted on — and the
    bar beside it is what it actually earns on rows it has not seen.
    """
    rows = [("uncalibrated", report.uncalibrated.ece, None, True)]
    rows += [
        (c.method, c.out_of_fold.ece, c.in_sample.ece, c.admissible)
        for c in report.candidates
    ]
    rows.sort(key=lambda r: r[1], reverse=True)

    ys = np.arange(len(rows))
    for y, (name, ece, in_sample, ok) in zip(ys, rows, strict=True):
        # A disqualified calibrator is drawn hollow rather than dropped. Its ECE is the
        # lowest on the panel, and a reader who cannot see that will wonder why it lost.
        ax.barh(
            y, ece, height=0.62, zorder=2,
            color=CALIBRATOR_RAMP[name] if ok else "white",
            edgecolor=CALIBRATOR_RAMP[name] if ok else DISQUALIFIED,
            linewidth=0 if ok else 1.6,
            hatch=None if ok else "////",
        )
        if in_sample is not None:
            ax.plot(
                [in_sample, in_sample], [y - 0.34, y + 0.34],
                color=INK, linewidth=1.6, zorder=4,
            )
        # Plain ASCII: the figure renders in Helvetica Neue, which has no arrow and no
        # set-membership glyph, and matplotlib substitutes a tofu box rather than
        # failing — a defect that only shows up in the committed PNG.
        note = "  chosen" if name == report.winner.method else ("" if ok else "  emits 0 or 1")
        ax.annotate(
            f"{ece:.4f}{note}",
            xy=(ece, y),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=8.5,
            color=INK if name == report.winner.method else (MUTED if ok else DISQUALIFIED),
        )
    ax.plot([], [], color=INK, linewidth=1.6, label="in-sample (what it can memorise)")
    ax.legend(loc="upper right", handlelength=1.2)
    ax.set_yticks(ys, [name for name, _, _, _ in rows])
    ax.set_xlim(0, max(r[1] for r in rows) * 1.55)
    ax.set_xlabel("expected calibration error, out-of-fold")
    ax.grid(axis="y", visible=False)
    _panel_title(
        ax,
        "Lowest error is not the winner: isotonic asserts certainty",
        # One line, and short enough to fit the panel: _panel_title reserves 18pt of
        # title pad, so a second line grows up into the title rather than down.
        "5 contiguous folds in the calibration split; an exact 0 or 1 disqualifies",
    )


def _distribution_panel(ax, test_prob: np.ndarray) -> None:
    """Where the model's probabilities actually live.

    A reliability diagram flatters a model whose predictions all pile up in one bin,
    because one bin is easy to get right. Showing sharpness next to calibration is what
    stops "well calibrated" from meaning "always says 88%".
    """
    ax.hist(test_prob, bins=np.linspace(0, 1, 41), color=OBSERVED, zorder=2)
    ax.axvline(float(np.median(test_prob)), color=INK, linewidth=1.4, zorder=3)
    ax.annotate(
        f"median {np.median(test_prob):.2f}",
        xy=(float(np.median(test_prob)), 1),
        xytext=(-6, -12),
        xycoords=("data", "axes fraction"),
        textcoords="offset points",
        ha="right",
        fontsize=8.5,
        color=INK,
    )
    ax.set_xlim(0, 1)
    ax.set_xlabel("predicted P(capture)")
    ax.set_ylabel("attempts")
    _panel_title(
        ax,
        "Sharp, not merely calibrated",
        "a model that predicted the base rate every time would be a single spike here",
    )


def calibration_chart(
    *,
    report: CalibrationReport,
    observed: Metrics,
    censored: Metrics,
    test_prob: np.ndarray,
    path: Path = ARTIFACT,
) -> Path:
    """Draw and write the figure. Returns the path written."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _style()
    fig = plt.figure(figsize=(13.5, 6.4))
    grid = fig.add_gridspec(2, 2, width_ratios=(1.25, 1.0), hspace=0.75, wspace=0.22)

    _reliability_panel(fig.add_subplot(grid[:, 0]), observed, censored)
    _calibrator_panel(fig.add_subplot(grid[0, 1]), report)
    _distribution_panel(fig.add_subplot(grid[1, 1]), test_prob)

    fig.suptitle(
        "Winback — model v1 calibration",
        x=0.008, y=0.985, ha="left", fontsize=12.5, color=INK,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.955))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return path

"""The four-arm figure for ``docs/EVALUATION.md``.

Three panels, arranged as the argument runs rather than as the data is stored.

The first panel is the one to look at. It draws what each arm *collected* and then what
the law would have let it *keep*, and the legacy arm's bar loses 90% of its length
between the two. A table can state that; a bar that shrinks in front of you is the same
fact arriving faster.

The second panel answers the question the first one raises — if breaking the rule pays
that well for arm C, what did it pay arm B? Nothing. Sixty-six violations, zero rupees.
Two baselines break the same law for entirely different reasons, and an evaluation that
reported only a violations count would have made them look alike.

The third is the rigour panel and it is the one that refuses to flatter the submission.
The paired interval against retry-everything crosses zero. That is drawn at the same
size and in the same ink as the interval that does not, because a figure that draws its
null result smaller than its positive one is arguing, not reporting.

Every number is read from Postgres through ``eval.persist`` — the same rows
``eval/report.py`` renders — so the figure cannot drift from the table beside it.
Palette and rcParams are shared with ``sim/validate_realism.py`` and ``ml/charts.py``:
three figures in one document drawn from three palettes read as three documents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.money import format_rupees
from eval.persist import load_arms, load_intervals, load_violations
from sim.validate_realism import HAIRLINE, INK, MUTED, _panel_title, _style

ARTIFACT = Path("docs/assets/four_arms.png")

HEADLINE = "v1"

#: Blue is the system acting legally and is the same blue the calibration figure gives
#: the observed slice. Red is the reserved status colour, used here for exactly what it
#: means everywhere else in Winback: a rule refused this. Amber separates the two
#: violation classes without leaving the warning family.
#:
#: Validated with the dataviz palette script against a light surface: worst adjacent
#: pair #e06c1f/#c81e3a at ΔE 12.9 deutan, 10.6 tritan, 15.4 normal — clear of the 8
#: target on all three. The illegal segment additionally carries a hatch, because
#: "this money did not count" is a status and no status should rest on hue alone.
LEGAL = "#305eff"
ILLEGAL = "#c81e3a"
PEAK = "#e06c1f"

#: Arms in the order they are argued, top to bottom.
ORDER = ("A", "B", "C", "D")

RULE_COLOURS = {"peak_window": PEAK, "bd_hard_not_retryable": ILLEGAL}
RULE_LABELS = {
    "peak_window": "presented inside an NPCI peak window",
    "bd_hard_not_retryable": "re-presented a permanently declined mandate",
}


def _rows(run_id: str) -> dict[str, dict[str, Any]]:
    return {row["arm"]: row for row in load_arms(run_id)}


def _lakhs(paise: float) -> float:
    return paise / 100 / 100_000


def _recovery_panel(ax, arms: dict[str, dict[str, Any]]) -> None:
    """Collected versus kept. The gap is what the guardrail would have refused."""
    ys = range(len(ORDER))
    legal = [_lakhs(arms[a]["compliant_recovered_paise"]) for a in ORDER]
    illegal = [
        _lakhs(arms[a]["recovered_paise"] - arms[a]["compliant_recovered_paise"]) for a in ORDER
    ]

    ax.barh(ys, legal, height=0.52, color=LEGAL, zorder=3)
    # Only the arms that actually recovered something illegally. A zero-width bar with
    # an edge colour still draws its edge, which would put a red tick against three arms
    # that never broke a rule — the exact opposite of what this panel says.
    illegal_ys = [y for y, value in zip(ys, illegal, strict=True) if value > 0]
    if illegal_ys:
        ax.barh(
            illegal_ys,
            [illegal[y] for y in illegal_ys],
            height=0.52,
            left=[legal[y] + 0.04 for y in illegal_ys],  # a surface gap, not a shared edge
            color="white",
            edgecolor=ILLEGAL,
            hatch="////",
            linewidth=1.2,
            zorder=3,
        )

    for y, arm in enumerate(ORDER):
        row = arms[arm]
        kept = _lakhs(row["compliant_recovered_paise"])
        total = _lakhs(row["recovered_paise"])
        if total == 0:
            ax.annotate(
                "never presented",
                xy=(0.06, y),
                fontsize=8.5,
                color=MUTED,
                va="center",
            )
            continue
        ax.annotate(
            format_rupees(row["compliant_recovered_paise"]),
            xy=(total + 0.16, y),
            fontsize=8.5,
            color=INK,
            va="center",
            fontweight="bold" if arm == "D" else "normal",
        )
        if total - kept > 0.01:
            ax.annotate(
                f"of {format_rupees(row['recovered_paise'])} collected",
                xy=(total + 0.16, y),
                xytext=(0, -11),
                textcoords="offset points",
                fontsize=7.5,
                color=MUTED,
                va="center",
            )

    _set_arm_axis(ax, ys)
    ax.set_xlim(0, 7.9)
    ax.set_xlabel("₹ lakh")
    _panel_title(
        ax,
        "Hold every arm to the law and the legacy policy loses 90% of it",
        "Solid: recovered by presentments the guardrail allows. Hatched: recovered by "
        "presentments it refuses.",
    )
    _legend(
        ax,
        [
            (LEGAL, None, "legally recovered"),
            (ILLEGAL, "////", "recovered in violation — not counted"),
        ],
        loc="upper right",
    )


def _violations_panel(ax, arms: dict[str, dict[str, Any]], breakdown: dict) -> None:
    """What each arm's lawbreaking actually bought it."""
    ys = range(len(ORDER))
    for rule, colour in RULE_COLOURS.items():
        left = [0.0] * len(ORDER)
        widths = []
        for index, arm in enumerate(ORDER):
            rows = {r["stop_reason"]: r for r in breakdown.get(arm, [])}
            widths.append(rows.get(rule, {}).get("violations", 0))
            left[index] = sum(
                r["violations"]
                for r in breakdown.get(arm, [])
                if list(RULE_COLOURS).index(r["stop_reason"]) < list(RULE_COLOURS).index(rule)
            )
        ax.barh(ys, widths, height=0.52, left=left, color=colour, zorder=3)

    for y, arm in enumerate(ORDER):
        rows = breakdown.get(arm, [])
        total = sum(r["violations"] for r in rows)
        if not total:
            ax.annotate("no violations", xy=(3, y), fontsize=8.5, color=MUTED, va="center")
            continue
        bought = sum(r["recovered_paise"] for r in rows)
        ax.annotate(f"{total}", xy=(total + 4, y), fontsize=8.5, color=INK, va="center")
        ax.annotate(
            f"bought {format_rupees(bought)}",
            xy=(total + 4, y),
            xytext=(0, -11),
            textcoords="offset points",
            fontsize=7.5,
            color=MUTED,
            va="center",
        )

    _set_arm_axis(ax, ys)
    ax.set_xlim(0, 152)
    ax.set_xlabel("presentments that broke a rule")
    _panel_title(
        ax,
        "The two baselines break the law for entirely different returns",
        "Arm B spends its legality on mandates the bank has permanently declined and "
        "collects nothing for it.",
    )
    _legend(
        ax,
        [(RULE_COLOURS[r], None, RULE_LABELS[r]) for r in RULE_COLOURS],
        loc="upper right",
    )


def _paired_panel(ax, intervals: dict) -> None:
    """D minus each baseline on the headline metric, with the 95% paired interval."""
    key = "paise_per_legal_attempt"
    baselines = [b for b in ("B", "C") if (b, key, "versus_winback") in intervals]
    ys = range(len(baselines))

    ax.axvline(0, color=HAIRLINE, linewidth=1.6, zorder=1)
    for y, arm in enumerate(baselines):
        row = intervals[(arm, key, "versus_winback")]
        low, point, high = (float(row[k]) / 100 for k in ("ci_low", "point", "ci_high"))
        crosses = low <= 0 <= high
        colour = MUTED if crosses else LEGAL
        ax.plot([low, high], [y, y], color=colour, linewidth=2.0, zorder=3)
        for end in (low, high):
            ax.plot([end, end], [y - 0.08, y + 0.08], color=colour, linewidth=2.0, zorder=3)
        ax.scatter([point], [y], s=64, color=colour, edgecolor="white", linewidth=2.0, zorder=4)
        ax.annotate(
            "crosses zero — a tie" if crosses else "excludes zero",
            xy=(high, y),
            xytext=(10, 0),
            textcoords="offset points",
            fontsize=8.5,
            color=MUTED if crosses else INK,
            va="center",
        )

    ax.set_yticks(list(ys))
    ax.set_yticklabels([f"D − {arm}" for arm in baselines], fontsize=9.5, color=INK)  # noqa: RUF001
    ax.set_ylim(-0.6, len(baselines) - 0.4)
    ax.invert_yaxis()
    ax.set_xlabel("₹ per legal attempt, paired difference")
    ax.grid(axis="y", visible=False)
    _panel_title(
        ax,
        "Winback does not beat retry-everything on money, and the figure says so",
        "10,000 cluster bootstrap resamples over subscriptions, differenced inside each resample.",
    )


def _set_arm_axis(ax, ys) -> None:
    ax.set_yticks(list(ys))
    ax.set_yticklabels(list(ORDER), fontsize=9.5, color=INK)
    ax.set_ylim(-0.6, len(ORDER) - 0.4)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)


def _legend(ax, entries: list[tuple[str, str | None, str]], loc: str = "lower right") -> None:
    """Identity is never colour alone — every swatch ships with its words."""
    from matplotlib.patches import Patch

    ax.legend(
        handles=[
            Patch(
                facecolor="white" if hatch else colour,
                edgecolor=colour,
                hatch=hatch,
                label=label,
            )
            for colour, hatch, label in entries
        ],
        loc=loc,
        fontsize=8,
    )


def build(path: Path = ARTIFACT, run_id: str = HEADLINE) -> Path:
    """Draw the figure from the stored run. Returns the path written."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _style()
    arms = _rows(run_id)
    breakdown = load_violations(run_id)
    intervals = load_intervals(run_id)

    # The paired panel carries two rows against the others' four, so it gets less
    # height rather than the same height full of white space.
    fig, axes = plt.subplots(
        3, 1, figsize=(11.0, 11.0), gridspec_kw={"height_ratios": [1.0, 1.0, 0.62]}
    )
    _recovery_panel(axes[0], arms)
    _violations_panel(axes[1], arms, breakdown)
    _paired_panel(axes[2], intervals)

    fig.suptitle(
        "Four arms, one cohort, the same oracle seeds",
        fontsize=13,
        color=INK,
        x=0.045,
        ha="left",
        y=0.982,
    )
    evaluated = max(row["invoices_evaluated"] for row in arms.values())
    fig.text(
        0.045,
        0.016,
        f"run {run_id} · {evaluated:,} failed invoices in the held-out test cohort · "
        "drawn from eval_arm_results / eval_arm_violations / eval_intervals by "
        "eval/charts.py",
        fontsize=7.5,
        color=MUTED,
    )

    fig.tight_layout(rect=(0.03, 0.035, 0.98, 0.965), h_pad=5.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return path


if __name__ == "__main__":
    print(f"wrote {build()}")

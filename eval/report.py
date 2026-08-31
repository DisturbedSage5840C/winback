"""``python -m eval.report`` — regenerate the results in ``docs/EVALUATION.md``.

**Only the numbers are generated.** The block between the two markers is replaced
wholesale from the database; everything around it — the metric's definition, the reading
of the result, the limitation — is written by hand and stays put. That split is on
purpose in both directions:

* A number a human can retype is a number a human can round. Every figure in the tables
  below came out of ``eval_arm_results`` and ``eval_intervals`` and can be re-derived by
  re-running the pipeline.
* An *argument* generated from a template is an argument nobody checked. The prose that
  says what the tables mean is hand-written, and the claims it makes are pinned by
  ``eval/tests/test_arms.py`` and ``eval/tests/test_bootstrap.py`` — including the
  negative one. If the evaluation ever stops being a tie on rupees against arm B, a test
  fails and the sentence gets rewritten deliberately rather than drifting.

Re-running with an unchanged database reproduces the file byte for byte.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from core.money import format_rupees
from eval.persist import load_arms, load_intervals, load_run, load_violations

REPORT = Path(__file__).resolve().parent.parent / "docs" / "EVALUATION.md"

BEGIN = "<!-- BEGIN GENERATED — python -m eval.report -->"
END = "<!-- END GENERATED -->"

HEADLINE = "v1"
REGIONS = (("v1_observed", "Observed"), ("v1_censored", "Censored"))
SENSITIVITY = (("v1_nudge_100", "1.00"), ("v1", "0.62"), ("v1_nudge_040", "0.40"))

#: How each statistic is written. Counts are counts; the rest are money.
_MONEY = {"compliant_recovered_paise", "paise_per_legal_attempt"}

STATISTIC_LABELS = {
    "compliant_recovered_paise": "Legally recovered",
    "legal_attempts_consumed": "Legal attempts",
    "compliance_violations": "Violations",
    "paise_per_legal_attempt": "₹ per legal attempt",
}


def _minus(text: str) -> str:
    """A real minus sign. ``-₹2,697`` in a right-aligned column reads as a dash."""
    return text.replace("-", "−", 1) if text.startswith("-") else text  # noqa: RUF001


def _value(statistic: str, value: float, *, to_the_rupee: bool = False) -> str:
    """``to_the_rupee`` drops paise — used only on bootstrap percentiles.

    ``core.money`` prints paise whenever they are non-zero, which is right for a fact:
    a recovered total of ₹4,999.50 must not render as ₹4,999. An interval endpoint is
    not a fact, it is the 2.5th percentile of ten thousand resamples of an 800-customer
    cohort, and ``₹4,68,397.95`` claims a precision the estimate does not have. The arm
    totals above keep every paise; only the intervals are shown to the rupee.
    """
    if statistic not in _MONEY:
        return _minus(f"{value:,.0f}")
    amount = round(value)
    if to_the_rupee:
        amount = round(amount / 100) * 100
    return _minus(format_rupees(amount))


def _interval(row: dict[str, Any] | None, statistic: str) -> str:
    """A point estimate and its 95% interval, or an em dash where it is undefined."""
    if row is None:
        return "—"
    point = _value(statistic, float(row["point"]))
    low = _value(statistic, float(row["ci_low"]), to_the_rupee=True)
    high = _value(statistic, float(row["ci_high"]), to_the_rupee=True)
    return f"{point} [{low}, {high}]"


def _ratio(arm: dict[str, Any]) -> str:
    """Undefined, not zero, for an arm that never presented."""
    value = arm["paise_per_legal_attempt"]
    return "—" if value is None else format_rupees(round(float(value)))


def _arm_table(arms: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Arm | Policy | Recovered | Legally recovered | Attempts | Legal attempts "
        "| Nudges | Escalated | Written off | Violations | ₹ / legal attempt |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in arms:
        bold = "**" if arm["arm"] == "D" else ""
        lines.append(
            f"| {bold}{arm['arm']}{bold} | {arm['arm_label']} "
            f"| {format_rupees(arm['recovered_paise'])} "
            f"| {bold}{format_rupees(arm['compliant_recovered_paise'])}{bold} "
            f"| {arm['attempts_consumed']:,} "
            f"| {bold}{arm['legal_attempts_consumed']:,}{bold} "
            f"| {arm['nudges_sent']:,} "
            f"| {arm['escalations']:,} "
            f"| {arm['written_off']:,} "
            f"| {bold}{arm['compliance_violations']:,}{bold} "
            f"| {bold}{_ratio(arm)}{bold} |"
        )
    return lines


def _paired_table(arms: list[dict[str, Any]], intervals: dict) -> list[str]:
    statistics = [
        "compliant_recovered_paise",
        "legal_attempts_consumed",
        "compliance_violations",
        "paise_per_legal_attempt",
    ]
    header = " | ".join(STATISTIC_LABELS[s] for s in statistics)
    lines = [
        # Not "D wins on" — the interval says the difference is real, not which side of
        # it is good. Fewer violations is better; more legal attempts spent is not.
        # Direction is read off the sign in the cell, and argued in §07.
        f"| Comparison | {header} | Excludes zero |",
        "|---|" + "---:|" * len(statistics) + "---|",
    ]
    for arm in arms:
        if arm["arm"] == "D":
            continue
        cells = []
        separable = []
        for statistic in statistics:
            row = intervals.get((arm["arm"], statistic, "versus_winback"))
            cells.append(_interval(row, statistic))
            if row is not None and (float(row["ci_low"]) > 0 or float(row["ci_high"]) < 0):
                separable.append(STATISTIC_LABELS[statistic].lower())
        verdict = ", ".join(separable) if separable else "no"
        label = f"**D − {arm['arm']}**"  # noqa: RUF001 - a minus sign, not a hyphen
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {verdict} |")
    return lines


def _violations_table(violations: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines = [
        "| Arm | Rule broken | Times | Rupees those attempts recovered |",
        "|---|---|---:|---:|",
    ]
    for arm in sorted(violations):
        for row in violations[arm]:
            lines.append(
                f"| {arm} | `{row['stop_reason']}` | {row['violations']:,} "
                f"| {format_rupees(row['recovered_paise'])} |"
            )
    if len(lines) == 2:
        lines.append("| — | no violations by any arm | 0 | ₹0 |")
    return lines


def _missing(runs: tuple[tuple[str, str], ...]) -> list[str]:
    """Absent runs are announced, not skipped.

    ``python -m eval --headline-only`` writes §04 and nothing else. Rendering §05 and §06
    as empty tables would look like a measured null result; omitting them silently would
    leave a reader wondering whether the section was ever there. Both are worse than a
    line saying which command was not run.
    """
    absent = [run_id for run_id, _ in runs if not _present(run_id)]
    if not absent:
        return []
    return [
        f"> **Not generated.** No run in the database for {', '.join(f'`{r}`' for r in absent)}"
        " — this section needs the full `python -m eval` rather than `--headline-only`.",
        "",
    ]


def _present(run_id: str) -> bool:
    try:
        load_run(run_id)
    except LookupError:
        return False
    return True


def _region_table(runs: tuple[tuple[str, str], ...]) -> list[str]:
    lines = [
        "| Region | Cases | Arm | Legally recovered | Legal attempts | Violations "
        "| ₹ / legal attempt |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for run_id, label in runs:
        if not _present(run_id):
            continue
        arms = load_arms(run_id)
        for index, arm in enumerate(arms):
            region = f"**{label}**" if index == 0 else ""
            cases = f"{arm['invoices_evaluated']:,}" if index == 0 else ""
            lines.append(
                f"| {region} | {cases} | {arm['arm']} "
                f"| {format_rupees(arm['compliant_recovered_paise'])} "
                f"| {arm['legal_attempts_consumed']:,} "
                f"| {arm['compliance_violations']:,} "
                f"| {_ratio(arm)} |"
            )
    return lines


def _sensitivity_table(runs: tuple[tuple[str, str], ...]) -> list[str]:
    """Arm D against arm B as the world's nudge effect moves under a fixed belief."""
    lines = [
        "| World nudge multiplier | Arm | Nudges sent | Legally recovered "
        "| Legal attempts | ₹ / legal attempt |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for run_id, multiplier in runs:
        if not _present(run_id):
            continue
        run = load_run(run_id)
        assumed = float(run["policy_params"]["assumed_nudge_failure_multiplier"])
        label = f"**{multiplier}**"
        if multiplier == f"{assumed:.2f}":
            label += " (policy's belief)"
        elif multiplier == "1.00":
            label += " (nudge does nothing)"
        for index, arm in enumerate(a for a in load_arms(run_id) if a["arm"] in ("B", "D")):
            lines.append(
                f"| {label if index == 0 else ''} | {arm['arm']} "
                f"| {arm['nudges_sent']:,} "
                f"| {format_rupees(arm['compliant_recovered_paise'])} "
                f"| {arm['legal_attempts_consumed']:,} "
                f"| {_ratio(arm)} |"
            )
    return lines


def render(
    run_id: str = HEADLINE,
    *,
    regions: tuple[tuple[str, str], ...] | None = None,
    sensitivity: tuple[tuple[str, str], ...] | None = None,
) -> str:
    """The generated block, from the database and nothing else.

    ``regions`` and ``sensitivity`` default to the module constants, resolved here rather
    than in the signature so that a test can substitute them.
    """
    regions = REGIONS if regions is None else regions
    sensitivity = SENSITIVITY if sensitivity is None else sensitivity
    run = load_run(run_id)
    arms = load_arms(run_id)
    intervals = load_intervals(run_id)
    violations = load_violations(run_id)
    evaluated = max((a["invoices_evaluated"] for a in arms), default=0)

    out: list[str] = [BEGIN, ""]
    out += [
        "## 04 — The result",
        "",
        f"Dataset `{run['dataset_version']}` fingerprint `{run['dataset_fingerprint']}` · "
        f"model `{run['model_version']}` · cohort `{run['cohort']}` · "
        f"{evaluated:,} failed invoices replayed · "
        f"{run['bootstrap_resamples']:,} bootstrap resamples, seed `{run['seed']}`.",
        "",
        "Every arm faced the same invoices with the same oracle seeds. Re-running the",
        "harness reproduces every rupee below exactly; the intervals are sampling",
        "uncertainty about which 800 customers were in the cohort, not simulation noise.",
        "",
    ]
    out += _arm_table(arms)
    out += [
        "",
        "### The paired comparison",
        "",
        "Differenced *inside* each resample, over subscriptions. Marginal intervals",
        "overlap almost entirely here because the arms move together when a lucky",
        "customer is resampled in — which is exactly why reading significance off two",
        "overlapping marginal intervals would be wrong.",
        "",
    ]
    out += _paired_table(arms, intervals)
    out += [
        "",
        "### What each arm's violations were, and what they bought it",
        "",
    ]
    out += _violations_table(violations)
    out += [
        "",
        "## 05 — The same four arms, by region",
        "",
        "The legacy policy never retried an invoice under ₹500 or on a netbanking",
        "mandate, so the model has no training labels in the censored region. Splitting",
        "the arms the same way asks whether the advantage survives outside the data.",
        "",
    ]
    out += _missing(regions)
    out += _region_table(regions)
    out += [
        "",
        "## 06 — Sensitivity to the nudge assumption",
        "",
        "The one number in this system that cannot be measured without sending real",
        "messages. The **world's** nudge effect moves across these runs; the **policy's**",
        "belief about it does not. What the table measures is not whether nudges work —",
        "it is how much the policy loses by being wrong about them.",
        "",
    ]
    out += _missing(sensitivity)
    out += _sensitivity_table(sensitivity)
    out += ["", END]
    return "\n".join(out)


def write(path: Path = REPORT, run_id: str = HEADLINE) -> bool:
    """Replace the generated block in place. Returns whether the file changed."""
    text = path.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise ValueError(
            f"{path} has no generated block. Expected the markers {BEGIN!r} and {END!r}."
        )
    head, _, rest = text.partition(BEGIN)
    _, _, tail = rest.partition(END)
    updated = head + render(run_id) + tail
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.report", description=__doc__)
    parser.add_argument("--run-id", default=HEADLINE)
    parser.add_argument("--out", type=Path, default=REPORT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed file is not what the database would generate",
    )
    args = parser.parse_args(argv)

    if args.check:
        current = args.out.read_text(encoding="utf-8")
        head, _, rest = current.partition(BEGIN)
        _, _, tail = rest.partition(END)
        if head + render(args.run_id) + tail != current:
            print(f"{args.out} is stale — run `python -m eval.report`", file=sys.stderr)
            return 1
        print(f"{args.out} matches the database")
        return 0

    changed = write(args.out, args.run_id)
    print(f"{args.out}: {'rewritten' if changed else 'already current'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

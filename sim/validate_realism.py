"""Does the simulated world match the one the README makes claims about?

This is the Day-3 gate, and it is adversarial on purpose. Every number Winback
reports is downstream of `sim/world.py`, so a world tuned to whatever the constants
happened to produce would make the entire evaluation a tautology dressed up as a
result. The defence is to write the target bands down *from cited sources first*,
measure against them, and let the run fail.

Three rules this file follows, borrowed from `scripts/probe_live_lane.py` because
they were learned the expensive way there:

**A check that could not be computed is INCONCLUSIVE, not PASS.** An empty slice is
the absence of evidence. Scoring it as agreement is how a validator ends up
certifying a world nobody measured.

**No band without a source.** Netbanking mandate failure rates are not published in
any form worth quoting, so the netbanking check reports its measurement and declines
to grade it. Inventing a plausible-looking band would be worse than having none: it
would look like corroboration.

**Shape checks, not just level checks.** Matching an aggregate failure rate is easy
and nearly meaningless — a world with the right average and no payday signal would
pass. The directional checks (failure rises across the salary cycle, technical
declines rise in peak hours, hard declines never recover) are what make the
mechanism, rather than the mean, the thing being validated.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from statistics import mean

from compliance.non_peak_window import IST
from compliance.root_cause import RootCause
from sim.generate import AttemptRow, Dataset, build_dataset
from sim.world import days_since_salary

ARTIFACT = Path("docs/assets/realism.png")


class Verdict(StrEnum):
    PASS = "PASS"  # noqa: S105 — a check verdict, not a credential
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONC"
    #: Measured and reported, deliberately not graded: no citable target exists.
    UNGRADED = "REPORT"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    measured: str
    expected: str
    verdict: Verdict
    #: Where the expected band comes from. Empty only for UNGRADED checks.
    source: str = ""
    note: str = ""

    def render(self) -> str:
        head = f"[{self.verdict:^6}] {self.name:<44} {self.measured:>18}   want {self.expected}"
        tail = ""
        if self.source:
            tail += f"\n{'':>9}source: {self.source}"
        if self.note:
            tail += f"\n{'':>9}{self.note}"
        return head + tail


def _band(
    name: str,
    value: float | None,
    low: float,
    high: float,
    source: str,
    *,
    unit: str = "%",
    n: int = 0,
    min_n: int = 30,
) -> Check:
    """Grade a measurement against a closed band, refusing to grade thin slices."""
    expected = f"{low:.3g}-{high:.3g}{unit}"
    if value is None or n < min_n:
        return Check(
            name=name,
            measured="n/a" if value is None else f"{value:.2f}{unit}",
            expected=expected,
            verdict=Verdict.INCONCLUSIVE,
            source=source,
            note=f"only {n} observations; {min_n} needed before this means anything",
        )
    verdict = Verdict.PASS if low <= value <= high else Verdict.FAIL
    return Check(
        name=name,
        measured=f"{value:.2f}{unit}  (n={n:,})",
        expected=expected,
        verdict=verdict,
        source=source,
    )


def _rate(rows: list[AttemptRow]) -> float | None:
    if not rows:
        return None
    return 100 * sum(r.outcome == "failed" for r in rows) / len(rows)


# --------------------------------------------------------------------------- checks


def check_failure_rates(dataset: Dataset) -> list[Check]:
    """Per-rail first-charge failure, against the rates the README quotes.

    First charges only. Mixing retries in would measure the legacy policy's taste in
    invoices rather than the rail's reliability: it only ever retried the invoices it
    liked, so the retry population is not a sample of anything.
    """
    method_of = {s.subscription_id: s.method for s in dataset.subscriptions}
    by_method: dict[str, list[AttemptRow]] = defaultdict(list)
    for attempt in dataset.attempts:
        if attempt.attempt_number == 1 and attempt.observed:
            by_method[method_of[attempt.subscription_id]].append(attempt)

    upi = by_method["upi_autopay"]
    card = by_method["card_mandate"]
    netbanking = by_method["netbanking"]

    checks = [
        _band(
            "UPI Autopay debit failure rate",
            _rate(upi), 8.0, 15.0,
            "README §01; Razorpay/industry reporting on UPI Autopay mandate failures",
            n=len(upi),
        ),
        _band(
            "Card mandate debit failure rate",
            _rate(card), 2.0, 3.0,
            "README §01; card e-mandate failure rates run far below UPI Autopay",
            n=len(card),
        ),
    ]

    rate = _rate(netbanking)
    checks.append(
        Check(
            name="Netbanking debit failure rate",
            measured="n/a" if rate is None else f"{rate:.2f}%  (n={len(netbanking):,})",
            expected="no citable band",
            verdict=Verdict.UNGRADED,
            note=(
                "Deliberately ungraded: no published figure worth quoting. Reported so "
                "the rail is visible, not scored so the world is not tuned to a "
                "number nobody published."
            ),
        )
    )
    return checks


def check_decline_taxonomy(dataset: Dataset) -> list[Check]:
    """NPCI's split of declines into technical and business.

    Graded over *every* failed attempt, observed or not: the taxonomy is a property
    of the rails, and restricting it to observed rows would let the legacy policy's
    selection bias leak into a check about the world.
    """
    failures = [a for a in dataset.attempts if a.outcome == "failed"]
    classes = Counter(a.root_cause_class for a in failures)
    total = sum(classes.values())

    td_share = 100 * classes[RootCause.TD] / total if total else None
    checks = [
        _band(
            "Technical declines, share of all declines",
            td_share, 14.0, 22.0,
            "NPCI TD/BD taxonomy: technical declines run ~18% of failed mandate debits",
            n=total,
        )
    ]

    business = classes[RootCause.BD_TRANSIENT] + classes[RootCause.BD_HARD]
    bd_share = 100 * business / total if total else None
    checks.append(
        _band(
            "Business declines, share of all declines",
            bd_share, 78.0, 86.0,
            "NPCI TD/BD taxonomy: the complement of the ~18% technical share",
            n=total,
        )
    )

    hard = classes[RootCause.BD_HARD]
    hard_share = 100 * hard / business if business else None
    checks.append(
        Check(
            name="Hard declines, share of business declines",
            measured="n/a" if hard_share is None else f"{hard_share:.2f}%  (n={business:,})",
            expected="no citable band",
            verdict=Verdict.UNGRADED,
            note=(
                "Winback's own BD_transient/BD_hard split is finer than NPCI's, so "
                "there is nothing to grade it against. It drives the policy, so it is "
                "reported."
            ),
        )
    )
    return checks


def _cycle_ratio(
    dataset: Dataset, method: str
) -> tuple[float | None, float | None, int, int]:
    """Failure rate early in the salary cycle vs late, for one rail."""
    salary_day = {c.customer_id: c.salary_day for c in dataset.customers}
    customer_of = {s.subscription_id: s.customer_id for s in dataset.subscriptions}
    method_of = {s.subscription_id: s.method for s in dataset.subscriptions}

    early: list[AttemptRow] = []
    late: list[AttemptRow] = []
    for attempt in dataset.attempts:
        if attempt.attempt_number != 1 or method_of[attempt.subscription_id] != method:
            continue
        day = days_since_salary(
            attempt.attempted_at, salary_day[customer_of[attempt.subscription_id]]
        )
        if day <= 3:
            early.append(attempt)
        elif day >= 20:
            late.append(attempt)

    return _rate(early), _rate(late), len(early), len(late)


def check_payday_signal(dataset: Dataset) -> list[Check]:
    """The mechanism the whole project is about, checked as a shape.

    If this is flat, the model has nothing to discover, the timing policy is
    arbitrary, and every downstream result is noise that happens to have a
    confidence interval. Graded on direction and strength rather than a level: no
    source publishes an insufficient-funds rate by day-of-salary-cycle, but that a
    depleted account bounces more than a freshly funded one is not in dispute.

    Measured on UPI Autopay alone, not pooled across rails. Pooling was the first
    version of this check and it was measuring the wrong population: a card mandate
    draws on a credit line, so it is barely exposed to the salary cycle at all, and
    including it dilutes a real signal into a weak one. The claim being validated is
    about debits that land on a bank account, so the check has to be about those.
    """
    early_rate, late_rate, n_early, n_late = _cycle_ratio(dataset, "upi_autopay")

    if early_rate is None or late_rate is None or min(n_early, n_late) < 30:
        return [
            Check(
                name="UPI: balance failure rises across salary cycle",
                measured=f"n={n_early} early / {n_late} late",
                expected="late >= 2x early",
                verdict=Verdict.INCONCLUSIVE,
                source="mechanism check",
                note="not enough UPI debits at one end of the cycle to compare",
            )
        ]

    ratio = late_rate / early_rate if early_rate else float("inf")
    checks = [
        Check(
            name="UPI: balance failure rises across salary cycle",
            measured=f"{early_rate:.2f}% -> {late_rate:.2f}%  ({ratio:.1f}x)",
            expected=">=2.0x",
            verdict=Verdict.PASS if ratio >= 2.0 else Verdict.FAIL,
            source=(
                "Mechanism, not a published rate: ~20M UPI mandates a month are "
                "revoked in India over low balances (Business Standard, cited in "
                "README §01). The signal must be strong enough to be learnable."
            ),
        )
    ]

    # The interaction, not just the main effect. The policy's timing decisions are
    # rail-dependent -- waiting for payday is worth a lot on UPI Autopay and almost
    # nothing on a card mandate -- so a world where both rails had the same cycle
    # shape would make half the policy's reasoning unjustifiable.
    card_early, card_late, card_n_early, card_n_late = _cycle_ratio(
        dataset, "card_mandate"
    )
    if card_early is None or card_late is None or min(card_n_early, card_n_late) < 30:
        checks.append(
            Check(
                name="Cards feel the salary cycle less than UPI",
                measured=f"n={card_n_early} early / {card_n_late} late",
                expected="card ratio < UPI ratio",
                verdict=Verdict.INCONCLUSIVE,
                source="a credit line absorbs a debit an empty savings account cannot",
                note="not enough card debits at one end of the cycle to compare",
            )
        )
        return checks

    card_ratio = card_late / card_early if card_early else float("inf")
    checks.append(
        Check(
            name="Cards feel the salary cycle less than UPI",
            measured=f"card {card_ratio:.1f}x  vs  UPI {ratio:.1f}x",
            expected="card ratio < UPI ratio",
            verdict=Verdict.PASS if card_ratio < ratio else Verdict.FAIL,
            source="a credit line absorbs a debit an empty savings account cannot",
        )
    )
    return checks


def check_congestion(dataset: Dataset) -> list[Check]:
    """Technical declines must be worse inside NPCI's peak windows.

    This is the reason the non-peak rule exists, so a world where the window did not
    matter would make Winback's central compliance constraint a pure cost. Only the
    legacy urgent branch presents in peak, so the slice is small by construction —
    which is exactly why the thin-slice guard is here and not assumed away.
    """
    peak = [a for a in dataset.attempts if not a.is_non_peak]
    off_peak = [a for a in dataset.attempts if a.is_non_peak]

    def td_rate(rows: list[AttemptRow]) -> float | None:
        if not rows:
            return None
        return 100 * sum(r.root_cause_class == RootCause.TD for r in rows) / len(rows)

    peak_rate, off_rate = td_rate(peak), td_rate(off_peak)
    if peak_rate is None or off_rate is None or len(peak) < 30:
        return [
            Check(
                name="Technical declines worse in peak hours",
                measured=f"n={len(peak)} peak attempts",
                expected="peak > off-peak",
                verdict=Verdict.INCONCLUSIVE,
                source="NPCI OC-215-A rations peak capacity because peak is congested",
                note="too few peak-window attempts to compare",
            )
        ]

    return [
        Check(
            name="Technical declines worse in peak hours",
            measured=f"{off_rate:.2f}% -> {peak_rate:.2f}%",
            expected="peak > off-peak",
            verdict=Verdict.PASS if peak_rate > off_rate else Verdict.FAIL,
            source="NPCI OC-215-A rations peak capacity because peak is congested",
        )
    ]


def check_hard_declines_are_hard(dataset: Dataset) -> list[Check]:
    """A revoked mandate must never be recovered by retrying it.

    The policy's whole justification for escalating instead of retrying rests on
    this. If a single BD_hard invoice were recovered by a retry, retry-everything
    would be the right answer and Winback would be solving a problem it invented.
    """
    by_invoice: dict[str, list[AttemptRow]] = defaultdict(list)
    for attempt in dataset.attempts:
        by_invoice[attempt.invoice_id].append(attempt)

    checked = recovered_after_hard = 0
    for attempts in by_invoice.values():
        ordered = sorted(attempts, key=lambda a: a.attempt_number)
        seen_hard = False
        for attempt in ordered:
            if seen_hard:
                checked += 1
                recovered_after_hard += attempt.outcome == "captured"
            seen_hard = seen_hard or attempt.root_cause_class == RootCause.BD_HARD

    if checked == 0:
        return [
            Check(
                name="No retry ever recovers a hard decline",
                measured="no retries follow a hard decline",
                expected="0 recoveries",
                verdict=Verdict.INCONCLUSIVE,
                source="mandate revoked / card expired / account closed are terminal",
                note="the dataset contains no retry after a BD_hard failure to check",
            )
        ]

    return [
        Check(
            name="No retry ever recovers a hard decline",
            measured=f"{recovered_after_hard} of {checked:,} post-hard retries",
            expected="0 recoveries",
            verdict=Verdict.PASS if recovered_after_hard == 0 else Verdict.FAIL,
            source="mandate revoked / card expired / account closed are terminal states",
        )
    ]


def check_censoring(dataset: Dataset) -> list[Check]:
    """Report the censoring rather than grade it — then check it actually bites.

    The rate itself has no correct value; it is whatever the legacy policy's filters
    imply. What *is* checkable is that the censored region differs from the observed
    one. If the two slices had the same success rate, the biased-legacy-policy story
    would be decoration and the observed-vs-censored calibration split in
    docs/EVALUATION.md would be measuring nothing.
    """
    censored = [a for a in dataset.attempts if not a.observed]
    observed_retries = [a for a in dataset.attempts if a.observed and a.attempt_number > 1]

    reasons = Counter(a.censoring_reason for a in censored)
    checks = [
        Check(
            name="Censored retries, by reason",
            measured=f"{len(censored):,} rows",
            expected="reported, not graded",
            verdict=Verdict.UNGRADED,
            note="  ".join(f"{reason}={count:,}" for reason, count in reasons.most_common()),
        )
    ]

    if len(censored) < 30 or len(observed_retries) < 30:
        checks.append(
            Check(
                name="Censored region is genuinely different",
                measured=f"n={len(censored)} censored / {len(observed_retries)} observed",
                expected="mean p_success differs",
                verdict=Verdict.INCONCLUSIVE,
                source="selection bias only matters if it selects on the outcome",
                note="one of the two slices is too thin to compare",
            )
        )
        return checks

    censored_p = 100 * mean(a.p_success for a in censored)
    observed_p = 100 * mean(a.p_success for a in observed_retries)
    checks.append(
        Check(
            name="Censored region is genuinely different",
            measured=f"observed {observed_p:.1f}% vs censored {censored_p:.1f}% p(success)",
            expected="differ by >=3pp",
            verdict=(
                Verdict.PASS if abs(censored_p - observed_p) >= 3.0 else Verdict.FAIL
            ),
            source=(
                "The legacy filters select on amount and rail, both of which move "
                "p(success); if they did not, there would be no bias to survive."
            ),
        )
    )
    return checks


def check_boundary_coverage(dataset: Dataset) -> list[Check]:
    """Every rupee threshold in the compliance layer must have invoices on both sides.

    A rule with no population near it is untested by the batch no matter how many
    unit tests it has, and "zero violations" is a hollow claim if nothing in the
    dataset could ever have triggered one.
    """
    amounts = [s.amount_paise for s in dataset.subscriptions]
    boundaries = (
        ("₹500 legacy value floor", 500_00),
        ("₹2,000 legacy urgent branch", 2_000_00),
        ("₹15,000 RBI AFA standard ceiling", 15_000_00),
    )

    checks = []
    for label, threshold in boundaries:
        above = sum(a > threshold for a in amounts)
        below = len(amounts) - above
        thin = min(above, below)
        checks.append(
            Check(
                name=f"Population straddles {label}",
                measured=f"{below:,} below / {above:,} above",
                expected=">=20 on each side",
                verdict=Verdict.PASS if thin >= 20 else Verdict.FAIL,
                source="db/01_schema.sql and compliance/afa_threshold.py",
            )
        )
    return checks


def check_cohorts(dataset: Dataset) -> list[Check]:
    """The split must be by customer AND by time, as the schema claims."""
    by_cohort: dict[str, list] = defaultdict(list)
    for subscription in dataset.subscriptions:
        by_cohort[subscription.cohort].append(subscription.mandate_start)

    if not all(by_cohort.get(name) for name in ("train", "calibrate", "test")):
        return [
            Check(
                name="Cohorts are ordered in time",
                measured=f"{ {k: len(v) for k, v in by_cohort.items()} }",
                expected="train < calibrate < test by mandate start",
                verdict=Verdict.INCONCLUSIVE,
                source="db/01_schema.sql, subscriptions.cohort",
                note="a cohort is empty",
            )
        ]

    ordered = max(by_cohort["train"]) <= min(by_cohort["calibrate"]) and max(
        by_cohort["calibrate"]
    ) <= min(by_cohort["test"])

    return [
        Check(
            name="Cohorts are ordered in time",
            measured=(
                f"train<={max(by_cohort['train'])}  "
                f"test>={min(by_cohort['test'])}"
            ),
            expected="train < calibrate < test by mandate start",
            verdict=Verdict.PASS if ordered else Verdict.FAIL,
            source="db/01_schema.sql, subscriptions.cohort",
        )
    ]


CHECKS = (
    check_failure_rates,
    check_decline_taxonomy,
    check_payday_signal,
    check_congestion,
    check_hard_declines_are_hard,
    check_censoring,
    check_boundary_coverage,
    check_cohorts,
)


def run(dataset: Dataset) -> list[Check]:
    return [check for group in CHECKS for check in group(dataset)]


# --------------------------------------------------------------------------- chart


#: The figure's categorical hues, in fixed slot order. Validated with the dataviz
#: skill's checker against a white surface: every adjacent pair clears the CVD
#: separation floor by a wide margin (worst pair ΔE 32 protan / 24 tritan), every
#: slot clears the chroma floor and 3:1 contrast. Slots are assigned to *rails*, and
#: a rail keeps its hue across every panel — the reader learns "UPI is blue" once.
RAIL_HUE = {
    "upi_autopay": "#305eff",
    "card_mandate": "#e06c1f",
    "netbanking": "#8b5cf6",
}
#: Severity is ordered, so the taxonomy gets an ordinal blue ramp rather than three
#: unrelated hues — and its terminal class gets the status red used everywhere in
#: Winback for "nothing further is permitted".
SEVERITY_HUE = {
    RootCause.TD: "#93b4ff",
    RootCause.BD_TRANSIENT: "#305eff",
    RootCause.BD_HARD: "#c81e3a",
}
INK, MUTED, HAIRLINE, BAND = "#192839", "#6c849d", "#dbe3ec", "#e9f0ff"

#: Bucket width for the salary-cycle panel, in days. Six days is wide enough that
#: every bucket carries a few hundred attempts — the raw per-day series is a sawtooth
#: of n≈40 samples, which shows noise rather than the mechanism underneath it.
CYCLE_BUCKET_DAYS = 6


def _style() -> None:
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
            "text.color": INK,
            "axes.labelcolor": MUTED,
            "axes.labelsize": 8.5,
            "axes.edgecolor": HAIRLINE,
            "axes.linewidth": 0.8,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "xtick.major.size": 0,
            "ytick.major.size": 0,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.color": HAIRLINE,
            "grid.linewidth": 0.8,
            "legend.frameon": False,
            "legend.fontsize": 8.5,
            "figure.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )


def _panel_title(ax: object, title: str, subtitle: str = "") -> None:
    """Title above the axes, subtitle beneath it in muted ink.

    Panel titles state the *finding*, not the variable — a reader who only looks at
    the figure should come away with the same conclusion the text argues.
    """
    ax.set_title(title, fontsize=10.5, loc="left", color=INK, pad=18 if subtitle else 8)
    if subtitle:
        ax.annotate(
            subtitle,
            xy=(0, 1),
            xytext=(0, 8),
            xycoords="axes fraction",
            textcoords="offset points",
            fontsize=8.5,
            color=MUTED,
            va="bottom",
        )


def _balance_rate(rows: list[AttemptRow]) -> float | None:
    """Share of attempts that bounced on an empty account, not overall failure.

    The salary-cycle claim is specifically about balance. Pooling in technical and
    hard declines — neither of which has any reason to track the cycle — dilutes the
    signal with noise that does not belong to the mechanism being shown.
    """
    if not rows:
        return None
    return 100 * sum(r.error_reason == "insufficient_funds" for r in rows) / len(rows)


def chart(dataset: Dataset, path: Path = ARTIFACT) -> Path:
    """The realism figure for docs/DATA.md.

    Four panels, each the visual form of a check above. The figure is meant to be
    read on its own: every panel is titled with its conclusion, every band is
    sourced, and every rate carries the n it was computed from — because a rate
    without an n is a claim a reader cannot weigh.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _style()

    fig, axes = plt.subplots(2, 2, figsize=(13.5, 9))
    fig.suptitle(
        "Does the simulated world behave like the one NPCI publishes about?",
        fontsize=14, fontweight="bold", color=INK, x=0.045, ha="left", y=0.975,
    )

    method_of = {s.subscription_id: s.method for s in dataset.subscriptions}
    salary_day = {c.customer_id: c.salary_day for c in dataset.customers}
    customer_of = {s.subscription_id: s.customer_id for s in dataset.subscriptions}
    observed = [a for a in dataset.attempts if a.observed]
    firsts = [a for a in observed if a.attempt_number == 1]

    # --- 1. rail failure rates against their cited bands ----------------------
    ax = axes[0][0]
    bands = {"upi_autopay": (8.0, 15.0), "card_mandate": (2.0, 3.0)}
    rails = ("upi_autopay", "card_mandate", "netbanking")
    rows_by_rail = {m: [a for a in firsts if method_of[a.subscription_id] == m] for m in rails}
    values = [_rate(rows_by_rail[m]) or 0.0 for m in rails]

    for index, rail in enumerate(rails):
        if rail not in bands:
            continue
        low, high = bands[rail]
        ax.add_patch(
            plt.Rectangle((index - 0.46, low), 0.92, high - low, color=BAND, zorder=0)
        )
    ax.bar(
        range(len(rails)), values, width=0.44,
        color=[RAIL_HUE[m] for m in rails], zorder=2,
    )
    # The cited band rides under its own rail rather than floating in the plot: a
    # reader should never have to work out which shaded rectangle a label refers to.
    for index, (rail, value) in enumerate(zip(rails, values, strict=True)):
        ax.text(index, value + 0.45, f"{value:.1f}%", ha="center", color=INK,
                fontsize=10, fontweight="medium")
        band = (
            f"cited {bands[rail][0]:.0f}-{bands[rail][1]:.0f}%"
            if rail in bands
            else "no published band"
        )
        ax.text(index, -1.5, f"n={len(rows_by_rail[rail]):,}  ·  {band}", ha="center",
                color=MUTED, fontsize=8)
    ax.set_xticks(range(len(rails)))
    ax.set_xticklabels([m.replace("_", " ") for m in rails], color=INK, fontsize=9.5)
    ax.set_ylim(0, 17)
    ax.set_ylabel("first-charge failure rate")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.tick_params(axis="x", pad=16)
    _panel_title(
        ax,
        "Both cited rails land inside their bands",
        "First charge only — retries would double-count the same failing mandates",
    )

    # --- 2. decline taxonomy --------------------------------------------------
    ax = axes[0][1]
    classes = Counter(a.root_cause_class for a in dataset.attempts if a.outcome == "failed")
    total = sum(classes.values()) or 1
    order = (RootCause.TD, RootCause.BD_TRANSIENT, RootCause.BD_HARD)
    shares = [100 * classes[c] / total for c in order]
    labels = ("TD\ntechnical", "BD transient\nretryable", "BD hard\nterminal")

    ax.barh(
        range(len(order)), shares, height=0.5,
        color=[SEVERITY_HUE[c] for c in order], zorder=2,
    )
    for index, (share, root_cause) in enumerate(zip(shares, order, strict=True)):
        ax.text(share + 1.2, index, f"{share:.1f}%", va="center", color=INK,
                fontsize=10, fontweight="medium")
        ax.text(share + 7.5, index, f"{classes[root_cause]:,} attempts", va="center",
                color=MUTED, fontsize=8)
    # The published figure is about the technical share alone, so the reference mark
    # sits under the TD row only. Drawn across all three it would read as a threshold
    # the other two classes were also being judged against.
    ax.plot([18, 18], [0.25, 0.44], color=INK, linewidth=1.4, zorder=3)
    ax.text(18.8, 0.44, "NPCI: ~18% of declines are technical", color=INK,
            fontsize=8, va="center", ha="left")
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(labels, color=INK, fontsize=9)
    ax.set_xlim(0, max(shares) * 1.5)
    ax.set_xlabel("share of all failed attempts")
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    _panel_title(
        ax,
        "The technical share matches NPCI's taxonomy",
        "Classified by compliance/root_cause.py — the same lookup the live path uses",
    )

    # --- 3. the salary cycle, per rail ---------------------------------------
    ax = axes[1][0]
    buckets: dict[tuple[str, int], list[AttemptRow]] = defaultdict(list)
    for attempt in observed:
        rail = method_of[attempt.subscription_id]
        day = days_since_salary(
            attempt.attempted_at, salary_day[customer_of[attempt.subscription_id]]
        )
        buckets[rail, day // CYCLE_BUCKET_DAYS].append(attempt)

    bucket_ids = sorted({key[1] for key in buckets})
    centres = [b * CYCLE_BUCKET_DAYS + CYCLE_BUCKET_DAYS / 2 for b in bucket_ids]
    for rail in ("upi_autopay", "card_mandate"):
        series = [_balance_rate(buckets[rail, b]) for b in bucket_ids]
        ax.plot(centres, series, color=RAIL_HUE[rail], linewidth=2,
                marker="o", markersize=5.5, markeredgecolor="white",
                markeredgewidth=2, zorder=3, label=rail.replace("_", " "))
        ax.annotate(
            f"{rail.replace('_', ' ')}  {series[-1]:.1f}%",
            xy=(centres[-1], series[-1]), xytext=(8, 0), textcoords="offset points",
            color=RAIL_HUE[rail], fontsize=9, fontweight="medium", va="center",
        )
    ax.set_xlim(0, 38)
    # Headroom above the tallest bucket so the legend never crosses a line.
    ax.set_ylim(0, max(_balance_rate(buckets["upi_autopay", b]) or 0 for b in bucket_ids) * 1.32)
    ax.set_xticks([b * CYCLE_BUCKET_DAYS for b in bucket_ids] + [30])
    ax.set_xlabel(f"days since salary credit ({CYCLE_BUCKET_DAYS}-day buckets)")
    ax.set_ylabel("share of attempts bouncing on balance")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.legend(loc="upper left", ncols=2, bbox_to_anchor=(0.0, 1.0))
    _panel_title(
        ax,
        "Accounts deplete across the month — credit lines do not",
        "This is the timing signal the model has to find, and the reason cards differ",
    )

    # --- 4. censoring ---------------------------------------------------------
    ax = axes[1][1]
    censored = [a for a in dataset.attempts if not a.observed]
    reasons = Counter(a.censoring_reason for a in censored)
    observed_retries = sum(1 for a in observed if a.attempt_number > 1)
    total_retries = observed_retries + len(censored)

    # Two rows rather than one stacked bar: the suppressed segments are narrow, and
    # three labels along a single bar collide no matter where they are placed.
    ax.barh([1], [observed_retries], height=0.34, color=RAIL_HUE["upi_autopay"], zorder=2)
    ax.text(observed_retries + total_retries * 0.015, 1, f"{observed_retries:,}",
            va="center", color=INK, fontsize=10, fontweight="medium")

    gap_width = total_retries * 0.006  # a surface gap, not a stroke around the marks
    left = 0.0
    for reason, colour, note in (
        ("legacy_value_floor", "#e06c1f", "under ₹500"),
        ("legacy_rail_excluded", "#8b5cf6", "netbanking"),
    ):
        count = reasons[reason]
        ax.barh([0], [count], left=left, height=0.34, color=colour, zorder=2)
        ax.text(left + count / 2, -0.28, f"{count:,}\n{note}", ha="center", va="top",
                color=INK, fontsize=8.5, linespacing=1.5)
        left += count + gap_width

    ax.set_yticks([1, 0])
    ax.set_yticklabels(
        ["retries the merchant\nactually made", "retries its filters\nsuppressed"],
        color=INK, fontsize=9, linespacing=1.4,
    )

    caption = f"{len(censored) / total_retries:.0%} of all retries were never observed"
    seen_unseen = _censoring_gap(dataset)
    if seen_unseen is not None:
        seen, unseen = seen_unseen
        caption += (
            f" — and the oracle puts the suppressed ones at\n{unseen:.0f}% success "
            f"against {seen:.0f}% for the ones that happened, so the bias is on "
            "the outcome itself"
        )
    ax.annotate(
        caption, xy=(0, -0.21), xycoords="axes fraction", fontsize=8.5,
        color=MUTED, va="top", linespacing=1.6,
    )

    ax.set_ylim(-0.75, 1.55)
    ax.set_xlim(0, total_retries * 1.08)
    ax.set_xlabel("retry attempts in the frozen dataset")
    ax.grid(axis="y", visible=False)
    _panel_title(
        ax,
        f"The legacy policy hid {len(censored) / total_retries:.0%} of its own retries",
        "Selection bias on amount and rail — both of which move p(success)",
    )

    summary = dataset.summary()
    fig.text(
        0.045, 0.022,
        f"dataset {summary['dataset_version']} · fingerprint {summary['fingerprint']} · "
        f"{summary['subscriptions']:,} subscriptions · {summary['invoices']:,} invoices · "
        f"{summary['attempts_total']:,} attempts · generated by sim/generate.py, "
        f"checked by sim/validate_realism.py",
        fontsize=7.5, color=MUTED,
    )

    fig.tight_layout(rect=(0.03, 0.045, 0.98, 0.945), h_pad=4.5, w_pad=5)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return path


def _censoring_gap(dataset: Dataset) -> tuple[float, float] | None:
    """Mean oracle p(success) on retries that happened vs retries that did not.

    Both numbers come from the same oracle, so the difference is the selection bias
    itself rather than an artifact of measuring the two slices differently.
    """
    seen = [a.p_success for a in dataset.attempts if a.observed and a.attempt_number > 1]
    unseen = [a.p_success for a in dataset.attempts if not a.observed]
    if not seen or not unseen:
        return None
    return 100 * mean(seen), 100 * mean(unseen)



# --------------------------------------------------------------------------- cli


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--subscriptions", type=int, default=None)
    parser.add_argument("--chart", action="store_true", help=f"write {ARTIFACT}")
    args = parser.parse_args()

    dataset = (
        build_dataset(args.subscriptions) if args.subscriptions else build_dataset()
    )
    checks = run(dataset)

    summary = dataset.summary()
    print(
        f"Winback realism gate — dataset {summary['dataset_version']} "
        f"({summary['fingerprint']}), {summary['subscriptions']:,} subscriptions, "
        f"{summary['invoices']:,} invoices, {summary['attempts_total']:,} attempts"
    )
    print(f"{'':>2}as of {dataset.invoices[0].charge_at.astimezone(IST):%Y}, frozen\n")

    for check in checks:
        print(check.render())

    tally = Counter(check.verdict for check in checks)
    print(
        "\n"
        + "  ".join(f"{verdict}={tally[verdict]}" for verdict in Verdict if tally[verdict])
        + f"  (of {len(checks)})"
    )

    if args.chart:
        print(f"\nwrote {chart(dataset)}")

    # INCONCLUSIVE fails the gate too. A check that could not be computed is an
    # open question, and Day 3 does not close with open questions.
    return 0 if tally[Verdict.PASS] + tally[Verdict.UNGRADED] == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""The intervals, and the four ways they could quietly be wrong.

1. **Not paired.** If each arm were resampled against its own customer base, the gap
   between two arms would be mostly customers. The draws are shared; these tests prove
   the sharing is doing work by showing the paired interval is far narrower than the
   marginals it sits between.
2. **Not clustered.** Resampling invoices instead of subscriptions would report an
   interval several times too narrow. The frame is asserted to be the cohort.
3. **Not reproducible.** An interval that moves when nobody changed anything is not a
   claim about the data.
4. **Defined where it is not.** Arm A never presented, so its rupees-per-legal-attempt is
   undefined rather than zero, and a paired gap against it would be arithmetic on nothing.

The last group pins the run's actual conclusions — including the one that is negative.
A test that fails when the evaluation starts claiming a win it does not have is worth
more than one that fails when it loses a win it does.
"""

from __future__ import annotations

import pytest

from eval.bootstrap import (
    RATIO_STATISTICS,
    STATISTICS,
    Interval,
    _percentile,
    bootstrap_run,
)

#: Enough for the qualitative conclusions to be stable, few enough to keep the suite fast.
#: The report runs ten thousand.
RESAMPLES = 500
SEED = 20260905


@pytest.fixture(scope="module")
def intervals(run):
    return bootstrap_run(run, resamples=RESAMPLES, seed=SEED)


# ------------------------------------------------------------------ the percentile


def test_percentile_interpolates_between_neighbours():
    values = [0.0, 10.0, 20.0, 30.0, 40.0]
    assert _percentile(values, 0.0) == 0.0
    assert _percentile(values, 1.0) == 40.0
    assert _percentile(values, 0.5) == 20.0
    assert _percentile(values, 0.125) == pytest.approx(5.0)


def test_percentile_of_nothing_is_zero_not_an_exception():
    assert _percentile([], 0.5) == 0.0


# ------------------------------------------------------------------ reproducibility


def test_the_same_seed_gives_the_same_interval(run):
    first = bootstrap_run(run, resamples=200, seed=7)
    second = bootstrap_run(run, resamples=200, seed=7)
    assert first == second


def test_a_different_seed_moves_the_interval_but_never_the_point(run):
    first = bootstrap_run(run, resamples=200, seed=7)
    second = bootstrap_run(run, resamples=200, seed=8)
    for arm, one in first.items():
        for name, interval in one.marginal.items():
            other = second[arm].marginal[name]
            # The point estimate is the observed sample, not a resample of it.
            assert interval.point == other.point


# ------------------------------------------------------------------ the point estimates


def test_point_estimates_are_the_arms_own_totals(run, intervals):
    for arm in run.arms:
        marginal = intervals[arm.arm].marginal
        assert marginal["compliant_recovered_paise"].point == arm.compliant_recovered_paise
        assert marginal["legal_attempts_consumed"].point == arm.legal_attempts_consumed
        assert marginal["compliance_violations"].point == arm.compliance_violations
        if "paise_per_legal_attempt" in marginal:
            assert marginal["paise_per_legal_attempt"].point == pytest.approx(
                arm.paise_per_legal_attempt
            )


def test_every_interval_contains_its_point(intervals):
    for arm in intervals.values():
        for name, interval in (arm.marginal | arm.versus_winback).items():
            assert interval.low <= interval.point <= interval.high, f"{arm.arm}/{name}"


def test_the_paired_point_is_the_difference_of_the_two_points(intervals):
    winback = intervals["D"].marginal
    for other in intervals.values():
        for name, interval in other.versus_winback.items():
            expected = winback[name].point - other.marginal[name].point
            assert interval.point == pytest.approx(expected)


# ------------------------------------------------------------------ the undefined ratio


def test_arm_a_gets_no_ratio_interval(intervals):
    """Undefined, not zero — and no paired gap against an undefined denominator."""
    a = intervals["A"]
    for name in RATIO_STATISTICS:
        assert name not in a.marginal
        assert name not in a.versus_winback
    # Everything else it does have.
    assert a.marginal["compliant_recovered_paise"].point == 0.0
    assert a.marginal["legal_attempts_consumed"].point == 0.0


@pytest.mark.parametrize("arm", ["B", "C", "D"])
def test_the_arms_that_presented_do_get_a_ratio_interval(intervals, arm):
    interval = intervals[arm].marginal["paise_per_legal_attempt"]
    assert interval.point > 0
    assert interval.low < interval.high


def test_every_statistic_is_reported_for_every_arm_that_can_have_it(intervals):
    for arm, entry in intervals.items():
        expected = {name for name, _ in STATISTICS if not (name in RATIO_STATISTICS and arm == "A")}
        assert set(entry.marginal) == expected


# ------------------------------------------------------------------ pairing and clustering


def test_the_paired_interval_is_far_narrower_than_the_marginals(intervals):
    """The reason the paired interval exists at all.

    Arms D and B are almost perfectly correlated by construction — they face the same
    invoices and draw the same coins — so their marginal intervals overlap almost
    entirely while their difference is pinned down tightly. Reading "the intervals
    overlap, so the arms are indistinguishable" off the marginals would be wrong here,
    and this asserts by how much.
    """
    d = intervals["D"].marginal["compliant_recovered_paise"]
    b = intervals["B"].marginal["compliant_recovered_paise"]
    paired = intervals["B"].versus_winback["compliant_recovered_paise"]

    marginal_width = min(d.high - d.low, b.high - b.low)
    assert (paired.high - paired.low) < marginal_width / 10


def test_the_marginal_intervals_are_wide_because_the_frame_is_the_whole_cohort(run, intervals):
    """A sanity check on the cluster bootstrap's most important choice.

    Resampling 800 subscriptions of which only a fifth ever failed produces real
    variation in how many failures a resample contains. An interval tight around the
    point would mean the frame had been conditioned on failing.
    """
    d = intervals["D"].marginal["compliant_recovered_paise"]
    assert (d.high - d.low) > 0.2 * d.point
    assert len(run.cohort_subscription_ids) > 4 * len(
        {r.subscription_id for r in run.arms[0].replays}
    )


def test_an_arm_with_no_violations_has_a_degenerate_interval(intervals):
    """Zero violations in every resample. Nothing to be uncertain about."""
    violations = intervals["D"].marginal["compliance_violations"]
    assert violations.point == violations.low == violations.high == 0.0
    assert not violations.excludes_zero


# ------------------------------------------------------------------ the conclusions


def test_winback_beats_the_legacy_arm_on_everything_that_matters(intervals):
    versus_c = intervals["C"].versus_winback
    assert versus_c["compliant_recovered_paise"].excludes_zero
    assert versus_c["compliant_recovered_paise"].point > 0
    assert versus_c["paise_per_legal_attempt"].excludes_zero
    assert versus_c["compliance_violations"].excludes_zero
    assert versus_c["compliance_violations"].point < 0


def test_the_separable_claim_against_retry_everything_is_legality(intervals):
    """66 fewer violations, and the interval is nowhere near zero."""
    gap = intervals["B"].versus_winback["compliance_violations"]
    assert gap.point < 0
    assert gap.excludes_zero


def test_the_money_claim_against_retry_everything_is_a_tie_and_must_stay_one(intervals):
    """The honest limitation, pinned so the report cannot quietly start overclaiming.

    D and B recover the same money. If this test ever fails, the evaluation has found a
    real difference and ``docs/EVALUATION.md`` needs rewriting — deliberately, not by
    a sentence drifting.
    """
    gap = intervals["B"].versus_winback["compliant_recovered_paise"]
    assert not gap.excludes_zero


# ------------------------------------------------------------------ rendering


def test_rupees_render_from_paise():
    interval = Interval(
        point=63_962_600, low=46_839_795, high=83_554_045, resamples=1, confidence=0.95
    )
    assert interval.as_rupees() == "₹639,626 [468,398, 835,540]"


def test_excludes_zero_is_strict_on_both_sides():
    assert Interval(1, 0.5, 2, 1, 0.95).excludes_zero
    assert Interval(-1, -2, -0.5, 1, 0.95).excludes_zero
    assert not Interval(1, -0.5, 2, 1, 0.95).excludes_zero
    assert not Interval(0, 0, 0, 1, 0.95).excludes_zero

"""What the replay harness has to be true for the evaluation to mean anything.

Four claims, in descending order of how badly a failure would hurt:

1. **The replay reproduces reality.** Arm C *is* the legacy policy — the same module that
   generated the training data. Handed the same invoice, it must reproduce the generator's
   recorded attempts exactly: same slots, same outcomes, same oracle seeds. This is the
   only test in the repository that checks the harness against ground truth rather than
   against itself, and it independently validates the rewind (per-cycle ``paid_count``,
   the technical-failure clock, the action string, the oracle key) in one assertion. If it
   fails, every number in ``docs/EVALUATION.md`` is fiction.
2. **The comparison is paired.** The oracle key contains no arm and no run, so two arms
   presenting the same invoice at the same moment must get the same answer. That is what
   licenses the paired bootstrap; without it the arms differ by luck as well as by policy.
3. **The harness is deterministic.** Same inputs, same rupees, twice.
4. **Nothing escapes the guardrail.** Arm D never violates. Every arm stops at the cap.
   The compliant subtotals are subtotals.
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from compliance.guardrail import ActionKind
from eval.arms import MAX_ATTEMPTS, Legacy
from eval.counterfactual import (
    CLOSED_STATUSES,
    RETRY_ACTION,
    EvalRun,
    ReplayCase,
    build_cases,
    replay_invoice,
    run_evaluation,
)
from sim.generate import Dataset

# ------------------------------------------------- 1. the replay reproduces reality


def _uncensored(dataset: Dataset, cases: tuple[ReplayCase, ...]) -> list[ReplayCase]:
    """Cases whose legacy retries actually reached a rail.

    A censored invoice has no recorded retries to compare against — that is what censored
    means — so it cannot participate in a ground-truth check. The shadow schedule the
    generator writes for those is the oracle's opinion about a debit nobody made, and
    comparing the replay to it would be comparing the oracle to itself.
    """
    censored = {attempt.invoice_id for attempt in dataset.attempts if not attempt.observed}
    return [case for case in cases if case.invoice.invoice_id not in censored]


def test_legacy_arm_reproduces_the_recorded_history(dataset, cases):
    """Arm C replays the generator's own attempts, field for field."""
    recorded = defaultdict(list)
    for attempt in dataset.attempts:
        if attempt.observed:
            recorded[attempt.invoice_id].append(attempt)

    comparable = _uncensored(dataset, cases)
    assert len(comparable) >= 100, "too few uncensored cases to be evidence of anything"

    compared = 0
    for case in comparable:
        replay = replay_invoice(Legacy(), case, run_id="ground_truth")
        replayed = [step.attempt for step in replay.steps if step.attempt is not None]
        original = sorted(
            (a for a in recorded[case.invoice.invoice_id] if a.attempt_number > 1),
            key=lambda a: a.attempt_number,
        )

        assert len(replayed) == len(original), (
            f"{case.invoice.invoice_id}: replayed {len(replayed)} attempts, "
            f"history recorded {len(original)}"
        )
        for got, want in zip(replayed, original, strict=True):
            assert got.attempt_number == want.attempt_number
            assert got.attempted_at == want.attempted_at
            assert got.outcome == want.outcome
            assert got.error_code == want.error_code
            # The seed is the counterfactual oracle's whole contract. Equal seeds mean
            # the replay asked the identical question, not a similar one.
            assert got.oracle_seed == want.oracle_seed
            compared += 1

    assert compared > 0


def test_every_case_is_a_closed_invoice_whose_first_charge_failed(cases):
    """The replay set's definition, asserted rather than assumed.

    ``at_risk`` invoices were truncated at the dataset's ``AS_OF``; replaying them would
    score arms on attempts dated into the future.
    """
    for case in cases:
        assert case.invoice.status in CLOSED_STATUSES
        assert case.first_charge.attempt_number == 1
        assert case.first_charge.outcome == "failed"
        assert case.first_charge.invoice_id == case.invoice.invoice_id


def test_paid_count_is_the_count_at_the_time_not_the_final_count(dataset, cases):
    """The rewind's subtlest field.

    ``SubscriptionRow.paid_count`` is the mandate's count today. Handing that to the world
    would give a customer's first cycle the reliability discount it only earned later.
    """
    by_subscription = defaultdict(list)
    for case in cases:
        by_subscription[case.subscription.subscription_id].append(case)

    for subscription_id, group in by_subscription.items():
        group.sort(key=lambda c: c.invoice.cycle_number)
        counts = [c.paid_count for c in group]
        assert counts == sorted(counts), f"{subscription_id}: paid_count went backwards"
        assert counts[0] < group[0].invoice.cycle_number
        for case in group:
            assert case.paid_count <= case.subscription.paid_count


def test_the_invoices_own_legacy_retries_are_rewound_away(cases):
    """The arm must not be able to see the retries it is being asked to decide about."""
    for case in cases:
        own = [
            a
            for a in case.base_history
            if a.invoice_id == case.invoice.invoice_id and a.attempt_number > 1
        ]
        assert own == [], f"{case.invoice.invoice_id}: history leaked its own retries"
        assert case.first_charge in case.base_history


# ------------------------------------------------- 2. the comparison is paired


def test_the_same_question_gets_the_same_answer_from_every_arm(run: EvalRun):
    """The property the whole evaluation design rests on.

    ``sim.world.oracle_key`` hashes ``(subscription, invoice, attempt_number, action,
    slot)`` and deliberately not the arm or the run. So wherever two arms happened to
    present the same invoice at the same moment, the coin they drew must be the same coin.
    """
    answers: dict[tuple, tuple] = {}
    shared = 0
    for arm in run.arms:
        for step in arm.steps():
            if step.attempt is None:
                continue
            key = (
                step.invoice_id,
                step.attempt.attempt_number,
                step.attempt.attempted_at,
                step.attempt.action,
            )
            answer = (step.attempt.outcome, step.attempt.error_code, step.attempt.oracle_seed)
            if key in answers:
                assert answers[key] == answer, (
                    f"arm {arm.arm} got a different outcome for {key} — the oracle is "
                    "not acting as a counterfactual"
                )
                shared += 1
            else:
                answers[key] = answer

    assert shared > 0, "no two arms ever presented the same slot; pairing is untested"


def test_every_presentment_uses_the_retry_action(run: EvalRun):
    """``action`` is inside the oracle key, so a drifted string silently unpairs the arms."""
    for arm in run.arms:
        for step in arm.steps():
            if step.attempt is not None:
                assert step.attempt.action == RETRY_ACTION


# ------------------------------------------------- 3. the harness is deterministic


def test_two_runs_agree_to_the_rupee(dataset, scorer, rates):
    second = run_evaluation(
        dataset, scorer=scorer, rates=rates, run_id="eval_test", model_version="v1"
    )
    first = run_evaluation(
        dataset, scorer=scorer, rates=rates, run_id="eval_test", model_version="v1"
    )
    for a, b in zip(first.arms, second.arms, strict=True):
        assert a.arm == b.arm
        assert a.recovered_paise == b.recovered_paise
        assert a.compliant_recovered_paise == b.compliant_recovered_paise
        assert a.legal_attempts_consumed == b.legal_attempts_consumed
        assert a.compliance_violations == b.compliance_violations


# ------------------------------------------------- 4. nothing escapes the guardrail


def test_winback_never_violates(arms_by_id):
    """The submission's central claim, and the one a panelist will check first."""
    assert arms_by_id["D"].compliance_violations == 0


def test_the_baselines_are_free_to_violate_and_do(arms_by_id):
    """A baseline that cannot break the rule cannot demonstrate the cost of breaking it.

    Both baselines must actually violate, or the violations column is untested and the
    comparison proves nothing about compliance.
    """
    assert arms_by_id["B"].compliance_violations > 0
    assert arms_by_id["C"].compliance_violations > 0


def test_no_arm_exceeds_the_npci_cap(run: EvalRun):
    for arm in run.arms:
        for replay in arm.replays:
            numbers = [step.attempt.attempt_number for step in replay.steps if step.attempt]
            assert numbers == sorted(numbers)
            assert all(2 <= n <= MAX_ATTEMPTS for n in numbers)
            # The original charge plus this arm's presentments.
            assert 1 + len(numbers) <= MAX_ATTEMPTS


def test_every_replay_reaches_a_terminal_action(run: EvalRun):
    """An arm that never stops is a bug, not a policy."""
    terminal = {ActionKind.ESCALATE, ActionKind.WRITE_OFF}
    for arm in run.arms:
        for replay in arm.replays:
            last = replay.steps[-1]
            assert last.kind in terminal or last.recovered_paise > 0


def test_compliant_totals_are_subtotals(run: EvalRun):
    for arm in run.arms:
        assert arm.compliant_recovered_paise <= arm.recovered_paise
        assert arm.legal_attempts_consumed <= arm.attempts_consumed
        assert arm.attempts_consumed - arm.legal_attempts_consumed == arm.compliance_violations


def test_arm_a_spends_nothing_and_its_ratio_is_undefined(arms_by_id):
    """Undefined, not zero. A zero would read as "tried and failed" and it did not try."""
    a = arms_by_id["A"]
    assert a.attempts_consumed == 0
    assert a.recovered_paise == 0
    assert a.paise_per_legal_attempt is None
    assert a.escalations == a.invoices_evaluated


def test_the_bootstrap_frame_is_the_cohort_not_the_replay_set(run: EvalRun, dataset):
    """Conditioning the frame on "had a failure" would report an interval too narrow."""
    cohort = {s.subscription_id for s in dataset.subscriptions if s.cohort == "test"}
    frame = set(run.cohort_subscription_ids)
    assert frame == cohort
    assert len(run.cohort_subscription_ids) == len(frame), "frame has duplicates"

    replayed = {r.subscription_id for r in run.arms[0].replays}
    assert replayed < frame, "every cohort subscription failed; that is not a cohort"


def test_all_arms_face_the_identical_invoice_set(run: EvalRun):
    """If two arms saw different invoices, the difference between them is the invoices."""
    sets = [tuple(r.invoice_id for r in arm.replays) for arm in run.arms]
    assert len({s for s in sets}) == 1


@pytest.mark.parametrize("cohort", ["train", "calibrate", "test"])
def test_cases_are_drawn_only_from_the_named_cohort(dataset, cohort):
    for case in build_cases(dataset, cohort=cohort):
        assert case.subscription.cohort == cohort

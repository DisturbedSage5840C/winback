"""The path from harness to document: persist, read back, render.

These are the only tests in `eval/` that touch Postgres, and they have to. The claim
`docs/EVALUATION.md` makes about itself — that every number in it came out of the
database and can be regenerated — is a claim about the write path, the read path and the
renderer together. Testing `render()` against a hand-built dict would test none of it.

Everything here is written under `TEST_RUN_ID` and deleted afterwards, so a test run
never disturbs the `v1` rows the committed report is generated from.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.db import agent_connection
from eval import report
from eval.bootstrap import bootstrap_run
from eval.counterfactual import EvalRun
from eval.persist import load_arms, load_intervals, load_run, load_violations, save
from eval.tests.conftest import TEST_RUN_ID

#: Enough resamples for an interval to exist; far fewer than the report's 10,000, because
#: nothing here asserts on the width of one.
RESAMPLES = 200


@pytest.fixture(scope="session")
def persisted(run: EvalRun) -> Iterator[EvalRun]:
    intervals = bootstrap_run(run, resamples=RESAMPLES, seed=1)
    save(run, intervals, seed=1, resamples=RESAMPLES, notes="test fixture")
    yield run
    with agent_connection() as conn, conn.transaction():
        for table in ("eval_intervals", "eval_arm_violations", "eval_arm_results", "eval_runs"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = %s", (TEST_RUN_ID,))


# ------------------------------------------------------------------ the round trip


def test_the_run_header_survives_the_round_trip(persisted: EvalRun) -> None:
    row = load_run(TEST_RUN_ID)
    assert row["dataset_fingerprint"] == persisted.dataset_fingerprint
    assert row["model_version"] == persisted.model_version
    assert row["bootstrap_resamples"] == RESAMPLES


def test_the_world_and_policy_parameters_are_stored_with_the_run(persisted: EvalRun) -> None:
    """A result without the parameters that produced it is not reproducible."""
    row = load_run(TEST_RUN_ID)
    assert row["world_params"]["nudge_balance_multiplier"] == pytest.approx(
        persisted.world_params.nudge_balance_multiplier
    )
    assert row["policy_params"]["assumed_nudge_failure_multiplier"] == pytest.approx(
        persisted.policy_params.assumed_nudge_failure_multiplier
    )


def test_every_arm_total_comes_back_exactly(persisted: EvalRun) -> None:
    stored = {row["arm"]: row for row in load_arms(TEST_RUN_ID)}
    assert set(stored) == {arm.arm for arm in persisted.arms}
    for arm in persisted.arms:
        row = stored[arm.arm]
        assert row["recovered_paise"] == arm.recovered_paise
        assert row["compliant_recovered_paise"] == arm.compliant_recovered_paise
        assert row["legal_attempts_consumed"] == arm.legal_attempts_consumed
        assert row["compliance_violations"] == arm.compliance_violations


def test_the_violation_breakdown_adds_up_to_the_arm_total(persisted: EvalRun) -> None:
    """The per-reason table and the summary column cannot be allowed to disagree."""
    breakdown = load_violations(TEST_RUN_ID)
    for arm in persisted.arms:
        rows = breakdown.get(arm.arm, [])
        assert sum(row["violations"] for row in rows) == arm.compliance_violations


def test_saving_twice_leaves_one_run(persisted: EvalRun) -> None:
    """Idempotent by deletion. An upsert would strand rows the second run did not emit."""
    before = len(load_arms(TEST_RUN_ID))
    intervals = bootstrap_run(persisted, resamples=RESAMPLES, seed=1)
    save(persisted, intervals, seed=1, resamples=RESAMPLES)
    assert len(load_arms(TEST_RUN_ID)) == before
    assert load_run(TEST_RUN_ID)["notes"] is None


def test_an_absent_run_says_which_command_to_run() -> None:
    with pytest.raises(LookupError, match="python -m eval"):
        load_run("no_such_run")


def test_every_stored_interval_contains_its_own_point(persisted: EvalRun) -> None:
    rows = load_intervals(TEST_RUN_ID)
    assert rows
    for (arm, statistic, _), row in rows.items():
        assert row["ci_low"] <= row["point"] <= row["ci_high"], (arm, statistic)


# ------------------------------------------------------------------ the rendering


def test_the_block_is_delimited_and_nothing_escapes_it(persisted: EvalRun) -> None:
    block = report.render(TEST_RUN_ID, regions=(), sensitivity=())
    assert block.startswith(report.BEGIN)
    assert block.endswith(report.END)
    assert block.count(report.BEGIN) == 1
    assert block.count(report.END) == 1


def test_the_headline_numbers_in_the_block_are_the_stored_ones(persisted: EvalRun) -> None:
    from core.money import format_rupees

    block = report.render(TEST_RUN_ID, regions=(), sensitivity=())
    for arm in persisted.arms:
        assert format_rupees(arm.compliant_recovered_paise) in block
    assert persisted.dataset_fingerprint in block


def test_an_arm_that_never_presented_gets_a_dash_and_not_a_zero(persisted: EvalRun) -> None:
    """₹0 per legal attempt is a measurement. Arm A took no attempts; there is no ratio."""
    arm_a = next(a for a in load_arms(TEST_RUN_ID) if a["arm"] == "A")
    assert arm_a["legal_attempts_consumed"] == 0
    assert report._ratio(arm_a) == "—"


def test_missing_optional_runs_are_announced_rather_than_skipped(persisted: EvalRun) -> None:
    block = report.render(TEST_RUN_ID, regions=(("absent_run", "Nowhere"),), sensitivity=())
    assert "Not generated" in block
    assert "`absent_run`" in block
    assert "--headline-only" in block


# ------------------------------------------------------------------ how numbers read


def test_negatives_use_a_real_minus_sign() -> None:
    """A right-aligned column of ``-66`` reads as an em dash. U+2212 does not."""
    assert report._value("compliance_violations", -66) == "−66"  # noqa: RUF001
    assert report._value("compliant_recovered_paise", -269_700) == "−₹2,697"  # noqa: RUF001
    assert "-" not in report._value("compliant_recovered_paise", -269_700)


def test_money_is_grouped_the_indian_way() -> None:
    assert report._value("compliant_recovered_paise", 63_962_600) == "₹6,39,626"


def test_interval_endpoints_drop_paise_but_point_estimates_do_not() -> None:
    """The precision decision, pinned. See ``_value``'s docstring for why."""
    row = {"point": 20_113.0, "ci_low": -7_894.0, "ci_high": 50_085.0}
    assert report._interval(row, "paise_per_legal_attempt") == "₹201.13 [−₹79, ₹501]"  # noqa: RUF001


def test_counts_are_never_dressed_up_as_money() -> None:
    assert report._value("legal_attempts_consumed", 197) == "197"
    assert "₹" not in report._value("compliance_violations", 66)


# ------------------------------------------------------------------ writing the file


HAND_WRITTEN = "# Evaluation\n\nProse a human wrote.\n\n{begin}\nstale\n{end}\n\nMore prose.\n"


def _fixture_doc(tmp_path, name: str = "EVALUATION.md"):
    path = tmp_path / name
    path.write_text(HAND_WRITTEN.format(begin=report.BEGIN, end=report.END), encoding="utf-8")
    return path


def test_writing_replaces_only_the_block(persisted: EvalRun, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(report, "REGIONS", ())
    monkeypatch.setattr(report, "SENSITIVITY", ())
    path = _fixture_doc(tmp_path)

    assert report.write(path, TEST_RUN_ID) is True
    text = path.read_text(encoding="utf-8")

    assert text.startswith("# Evaluation\n\nProse a human wrote.\n")
    assert text.endswith("\n\nMore prose.\n")
    assert "stale" not in text
    assert "## 04 — The result" in text


def test_rewriting_an_unchanged_file_is_a_no_op(persisted: EvalRun, tmp_path, monkeypatch) -> None:
    """The byte-for-byte reproducibility claim, as a test rather than a promise."""
    monkeypatch.setattr(report, "REGIONS", ())
    monkeypatch.setattr(report, "SENSITIVITY", ())
    path = _fixture_doc(tmp_path)

    report.write(path, TEST_RUN_ID)
    first = path.read_text(encoding="utf-8")
    assert report.write(path, TEST_RUN_ID) is False
    assert path.read_text(encoding="utf-8") == first


def test_a_document_without_markers_is_refused(persisted: EvalRun, tmp_path) -> None:
    """Better to fail loudly than to append a second copy of the results."""
    path = tmp_path / "no_markers.md"
    path.write_text("# Evaluation\n\nNothing to replace.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no generated block"):
        report.write(path, TEST_RUN_ID)


def test_check_passes_on_a_fresh_file_and_fails_once_it_drifts(
    persisted: EvalRun, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(report, "REGIONS", ())
    monkeypatch.setattr(report, "SENSITIVITY", ())
    path = _fixture_doc(tmp_path)
    argv = ["--run-id", TEST_RUN_ID, "--out", str(path), "--check"]

    report.write(path, TEST_RUN_ID)
    assert report.main(argv) == 0

    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("## 04 — The result", "## 04 — The results"), encoding="utf-8")
    assert report.main(argv) == 1

"""The loader: what reaches Postgres, and what deliberately does not.

**Nothing here calls :func:`sim.load.load`.** It is destructive by design — it routes
through ``winback_reset_world()`` and drops every fact in the database, evaluation runs
included. A test that called it would delete whatever run the developer was in the middle
of, which is precisely the kind of helpful-looking test that costs an afternoon. So the
write path is tested by its column lists and its row-builder, both of which are pure, and
the read path is tested against whatever world is loaded.

The load itself is exercised for real every time `scripts/run_demo.sh` runs, and its
result is asserted by `require_fingerprint` at the top of every batch.
"""

from __future__ import annotations

import pytest

from sim.generate import build_dataset
from sim.load import (
    ATTEMPT_COLUMNS,
    CUSTOMER_COLUMNS,
    INVOICE_COLUMNS,
    SUBSCRIPTION_COLUMNS,
    LoadError,
    LoadReport,
    _attempt_tuple,
    loaded_manifest,
    require_fingerprint,
)

# --------------------------------------------------------------- oracle truth stays out


def test_no_column_anywhere_exposes_the_oracles_answer_key():
    """``p_success`` is what the *simulator* knows, not what a merchant would have.

    If it were queryable, a feature could reach it by accident and the evaluation would
    quietly become a measurement of the simulator reading its own answer sheet. Same for
    ``monthly_headroom_paise``, which is the balance process the model is supposed to be
    *inferring* from day-of-month.
    """
    every_column = set(CUSTOMER_COLUMNS + SUBSCRIPTION_COLUMNS + INVOICE_COLUMNS + ATTEMPT_COLUMNS)
    for forbidden in ("p_success", "monthly_headroom_paise", "headroom_paise"):
        assert forbidden not in every_column


def test_history_cannot_be_written_under_an_arm():
    """``run_id`` and ``arm`` are absent from the column list entirely rather than written
    as NULL, so there is no line a later edit could set to an arm by accident. The four
    arms write their own attempt rows; these are the merchant's actual past."""
    assert "run_id" not in ATTEMPT_COLUMNS
    assert "arm" not in ATTEMPT_COLUMNS


def test_the_attempt_tuple_matches_its_column_list():
    """Positional ``COPY``. A column added to one and not the other would misalign every
    field after it, and Postgres would only complain if the types happened to disagree."""
    attempt = build_dataset().attempts[0]
    assert len(_attempt_tuple(attempt)) == len(ATTEMPT_COLUMNS)


def test_the_attempt_tuple_carries_the_censoring_evidence():
    """Censored rows are loaded, not dropped. They are not training data, but they are the
    evidence for what the legacy policy chose never to look at — and the observed-vs-
    censored calibration gap is the honesty result the submission leads with."""
    assert "observed" in ATTEMPT_COLUMNS
    assert "censoring_reason" in ATTEMPT_COLUMNS

    dataset = build_dataset()
    censored = [row for row in dataset.attempts if not row.observed]
    assert censored, "the frozen dataset is supposed to contain censored attempts"
    assert all(row.censoring_reason for row in censored)


# --------------------------------------------------------------- the report


def _report(**over) -> LoadReport:
    base = dict(
        dataset_version="v1",
        dataset_fingerprint="c32b2b063cd87707",
        customers=4_000,
        subscriptions=4_000,
        invoices=30_210,
        attempts=33_866,
        censored=786,
    )
    return LoadReport(**{**base, **over})


def test_the_report_states_the_censoring_rate_it_loaded():
    assert _report().censoring_rate == pytest.approx(786 / 33_866, abs=1e-6)
    assert "2.3%" in str(_report())


def test_an_empty_load_does_not_divide_by_zero():
    """A guard, not a scenario. It exists so the failure of an empty load is the empty
    numbers rather than a ZeroDivisionError three frames away from the cause."""
    assert _report(attempts=0, censored=0).censoring_rate == 0.0


def test_the_report_names_the_fingerprint_it_loaded():
    """Every number in ``docs/EVALUATION.md`` is quoted against one specific fingerprint,
    so the line that says what was loaded has to carry it."""
    assert "c32b2b063cd87707" in str(_report())


# --------------------------------------------------------------- the read path


@pytest.mark.db
def test_the_database_says_which_world_it_holds():
    """One ``world_manifest`` row, so anything downstream can ask instead of assuming."""
    manifest = loaded_manifest()
    assert manifest["dataset_fingerprint"] == build_dataset().fingerprint()
    assert manifest["attempts"] > 0


@pytest.mark.db
def test_require_fingerprint_passes_against_the_loaded_world():
    assert require_fingerprint() == build_dataset().fingerprint()


@pytest.mark.db
def test_a_fingerprint_mismatch_is_an_error_and_not_a_warning():
    """The whole point of the check. A batch whose audit rows point at invoice ids from a
    world that has since been regenerated is unreproducible, and the only moment that is
    cheap to notice is before the batch starts."""
    with pytest.raises(LoadError, match="Re-run"):
        require_fingerprint("0000000000000000")

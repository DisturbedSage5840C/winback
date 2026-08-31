"""The evaluation's results, into and out of Postgres.

``docs/EVALUATION.md`` is generated from these tables and never hand-typed. That is not
tidiness — a number typed into a document is a number that can disagree with the code
that produced it, and the one thing an evaluation cannot survive is a table nobody can
reproduce. The path is one-way: harness -> database -> report.

**Idempotent by deletion, not by upsert.** Re-running an evaluation under an existing
``run_id`` clears that run's rows first. An upsert would leave behind any row the new run
no longer produces — an arm that was dropped, a statistic that became undefined — and the
report would render a stale row beside fresh ones with nothing to mark the difference.

These four tables are the only mutable ones in the schema, which is deliberate and
explained in ``db/03_grants.sql``: they hold derived summaries, and a summary that cannot
be recomputed in place is a summary that drifts away from the facts it summarises. The
facts themselves — ``payment_attempts``, ``decisions``, ``audit_log`` — stay append-only.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from core.db import agent_connection, read_connection
from eval.bootstrap import DEFAULT_RESAMPLES, DEFAULT_SEED, ArmIntervals
from eval.counterfactual import ArmResult, EvalRun


def _violation_breakdown(arm: ArmResult) -> dict[str, tuple[int, int]]:
    """``stop_reason -> (violations, paise those violations recovered)``.

    The second number is the one that distinguishes a baseline that breaks the rule and
    gains nothing from one that breaks it and books most of its revenue that way.
    """
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for step in arm.steps():
        if not step.violation:
            continue
        reason = step.decision.stop_reason or "unspecified"
        counts[reason][0] += 1
        counts[reason][1] += step.recovered_paise
    return {reason: (n, paise) for reason, (n, paise) in counts.items()}


def save(
    run: EvalRun,
    intervals: dict[str, ArmIntervals],
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    notes: str | None = None,
) -> None:
    """Write one evaluation run, replacing any previous run under the same id."""
    with agent_connection() as conn, conn.transaction():
        conn.execute("DELETE FROM eval_intervals WHERE run_id = %s", (run.run_id,))
        conn.execute("DELETE FROM eval_arm_violations WHERE run_id = %s", (run.run_id,))
        conn.execute("DELETE FROM eval_arm_results WHERE run_id = %s", (run.run_id,))
        conn.execute("DELETE FROM eval_runs WHERE run_id = %s", (run.run_id,))

        conn.execute(
            """
            INSERT INTO eval_runs (run_id, model_version, dataset_version,
                                   dataset_fingerprint, seed, bootstrap_resamples,
                                   cohort, world_params, policy_params, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run.run_id,
                run.model_version,
                run.dataset_version,
                run.dataset_fingerprint,
                seed,
                resamples,
                run.cohort,
                json.dumps(asdict(run.world_params)),
                json.dumps(asdict(run.policy_params)),
                notes,
            ),
        )

        for arm in run.arms:
            conn.execute(
                """
                INSERT INTO eval_arm_results (
                    run_id, arm, arm_label, invoices_evaluated, invoices_recovered,
                    recovered_paise, compliant_recovered_paise, attempts_consumed,
                    legal_attempts_consumed, nudges_sent, escalations, written_off,
                    compliance_violations, paise_per_legal_attempt)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run.run_id,
                    arm.arm,
                    arm.label,
                    arm.invoices_evaluated,
                    arm.invoices_recovered,
                    arm.recovered_paise,
                    arm.compliant_recovered_paise,
                    arm.attempts_consumed,
                    arm.legal_attempts_consumed,
                    arm.nudges_sent,
                    arm.escalations,
                    arm.written_off,
                    arm.compliance_violations,
                    arm.paise_per_legal_attempt,
                ),
            )

            for reason, (count, recovered) in sorted(_violation_breakdown(arm).items()):
                conn.execute(
                    """
                    INSERT INTO eval_arm_violations
                        (run_id, arm, stop_reason, violations, recovered_paise)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (run.run_id, arm.arm, reason, count, recovered),
                )

            entry = intervals.get(arm.arm)
            if entry is None:
                continue
            for comparison, group in (
                ("marginal", entry.marginal),
                ("versus_winback", entry.versus_winback),
            ):
                for statistic, interval in group.items():
                    conn.execute(
                        """
                        INSERT INTO eval_intervals (run_id, arm, statistic, comparison,
                                                    point, ci_low, ci_high, resamples,
                                                    confidence)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run.run_id,
                            arm.arm,
                            statistic,
                            comparison,
                            interval.point,
                            interval.low,
                            interval.high,
                            interval.resamples,
                            interval.confidence,
                        ),
                    )


# --------------------------------------------------------------------------- reading


def load_run(run_id: str) -> dict[str, Any]:
    """The run's header row. Raises if it is not there — a report needs a run."""
    with read_connection() as conn:
        row = conn.execute("SELECT * FROM eval_runs WHERE run_id = %s", (run_id,)).fetchone()
    if row is None:
        raise LookupError(
            f"no evaluation run {run_id!r} in the database. Run `python -m eval` first."
        )
    return dict(row)


def load_arms(run_id: str) -> list[dict[str, Any]]:
    with read_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM eval_arm_results WHERE run_id = %s ORDER BY arm", (run_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def load_violations(run_id: str) -> dict[str, list[dict[str, Any]]]:
    """``arm -> rows``, most violations first."""
    with read_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM eval_arm_violations WHERE run_id = %s
            ORDER BY arm, violations DESC, stop_reason
            """,
            (run_id,),
        ).fetchall()
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[row["arm"]].append(dict(row))
    return dict(out)


def load_intervals(run_id: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    """``(arm, statistic, comparison) -> row``."""
    with read_connection() as conn:
        rows = conn.execute("SELECT * FROM eval_intervals WHERE run_id = %s", (run_id,)).fetchall()
    return {(r["arm"], r["statistic"], r["comparison"]): dict(r) for r in rows}


def latest_run_id(cohort: str = "test") -> str | None:
    with read_connection() as conn:
        row = conn.execute(
            """
            SELECT run_id FROM eval_runs WHERE cohort = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (cohort,),
        ).fetchone()
    return row["run_id"] if row else None

"""Put the frozen world into Postgres.

Until now the dataset has lived only in memory: ``build_dataset()`` rebuilds it from a
seed, and the model and the evaluation harness both consume it that way. That was
sufficient for those two, and it is not sufficient for anything after them. The API
serves rows, the dashboard drills into rows, and the audit trail references rows by
foreign key. A generator is not a row.

Three properties this module is written to hold.

**The database is a projection of the frozen dataset, never a second source of truth.**
Loading is a full replace through ``winback_reset_world()`` — the one sanctioned path
that removes immutable rows — followed by a single transaction that writes all four
fact tables. There is no incremental mode and no upsert, because a partially-refreshed
world is a world whose fingerprint means nothing.

**The fingerprint is written with the rows and checked on read.** One ``world_manifest``
row records which world the database is holding, so anything downstream can ask instead
of assuming. If the loaded fingerprint and the generator's disagree, the mismatch is an
error and not a warning: every number in ``docs/EVALUATION.md`` is quoted against one
specific fingerprint.

**Oracle truth does not get loaded.** ``AttemptRow.p_success`` and
``CustomerRow.monthly_headroom_paise`` are what the *simulator* knows, not what a merchant
would have; neither has a column, and that is deliberate rather than an oversight. If the
balance headroom were queryable, a feature could reach it by accident and the whole
evaluation would quietly become a measurement of the simulator reading its own answer
key.

**Attempts arrive as history, not as evaluation rows.** ``run_id`` and ``arm`` stay NULL
for everything written here. The four arms write their own attempt rows under a run,
and the schema's ``UNIQUE NULLS NOT DISTINCT (invoice_id, attempt_number, run_id, arm)``
is what keeps the two from colliding. The censored rows are loaded too, with
``observed = FALSE`` and their censoring reason — they are not training data, but they
are the evidence for what the legacy policy chose not to look at, and the calibration
report in ``docs/EVALUATION.md`` §10 is about exactly them.

``COPY`` rather than ``executemany``: 33,866 attempts is not a large table, but it is
large enough that the difference is the difference between a demo that reseeds in
two seconds and one that reseeds in forty.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.db import agent_connection, reset_world
from sim.generate import AttemptRow, Dataset, build_dataset

if TYPE_CHECKING:
    import psycopg

#: Column order for every COPY below. Written out rather than derived from the dataclass,
#: because the dataclass has fields the table does not (``p_success`` is oracle truth and
#: has no business being queryable by the dashboard) and the table has columns the
#: dataclass does not (``run_id``, ``arm``).
CUSTOMER_COLUMNS = (
    "customer_id",
    "customer_hash",
    "signup_date",
    "consent_status",
    "consent_updated_at",
    "salary_day",
)

SUBSCRIPTION_COLUMNS = (
    "subscription_id",
    "customer_id",
    "method",
    "bank",
    "mcc_category",
    "amount_paise",
    "status",
    "mandate_start",
    "paid_count",
    "remaining_count",
    "cohort",
)

INVOICE_COLUMNS = (
    "invoice_id",
    "subscription_id",
    "cycle_number",
    "amount_paise",
    "charge_at",
    "notice_sent_at",
    "status",
)

ATTEMPT_COLUMNS = (
    "attempt_id",
    "invoice_id",
    "subscription_id",
    "attempt_number",
    "attempted_at",
    "is_non_peak",
    "action",
    "amount_paise",
    "outcome",
    "error_code",
    "error_source",
    "error_step",
    "error_reason",
    "root_cause_class",
    "observed",
    "censoring_reason",
    "oracle_seed",
)


class LoadError(RuntimeError):
    """The database does not hold the world it claims to hold."""


@dataclass(frozen=True, slots=True)
class LoadReport:
    """What went in. Printed by the CLI and asserted on by the tests."""

    dataset_version: str
    dataset_fingerprint: str
    customers: int
    subscriptions: int
    invoices: int
    attempts: int
    censored: int

    @property
    def censoring_rate(self) -> float:
        return self.censored / self.attempts if self.attempts else 0.0

    def __str__(self) -> str:
        return (
            f"loaded {self.dataset_version} @ {self.dataset_fingerprint}: "
            f"{self.customers:,} customers · {self.subscriptions:,} subscriptions · "
            f"{self.invoices:,} invoices · {self.attempts:,} attempts "
            f"({self.censored:,} censored, {self.censoring_rate:.1%})"
        )


def _copy(conn: psycopg.Connection, table: str, columns: tuple[str, ...], rows) -> int:
    """Stream rows into one table. Returns the count written."""
    written = 0
    statement = f"COPY {table} ({', '.join(columns)}) FROM STDIN"
    with conn.cursor().copy(statement) as copy:
        for row in rows:
            copy.write_row(row)
            written += 1
    return written


def _attempt_tuple(attempt: AttemptRow) -> tuple:
    """One attempt as history.

    ``run_id`` and ``arm`` are absent from ``ATTEMPT_COLUMNS`` entirely rather than
    written as NULL, so there is no line here that a later edit could set to an arm
    by accident. History is history because of where it is written from.
    """
    return (
        attempt.attempt_id,
        attempt.invoice_id,
        attempt.subscription_id,
        attempt.attempt_number,
        attempt.attempted_at,
        attempt.is_non_peak,
        attempt.action,
        attempt.amount_paise,
        attempt.outcome,
        attempt.error_code,
        attempt.error_source,
        attempt.error_step,
        attempt.error_reason,
        attempt.root_cause_class,
        attempt.observed,
        attempt.censoring_reason,
        attempt.oracle_seed,
    )


def load(dataset: Dataset | None = None) -> LoadReport:
    """Replace the world in Postgres with the frozen dataset.

    Destructive by design and by the only route that is allowed to be: everything
    generated is dropped first, including any evaluation runs, because an ``eval_runs``
    row that points at a fingerprint the database no longer holds is worse than no row.
    Re-run ``python -m eval`` after a reload; it takes about a minute.
    """
    dataset = build_dataset() if dataset is None else dataset
    digest = dataset.fingerprint()

    reset_world()

    with agent_connection() as conn, conn.transaction():
        customers = _copy(
            conn,
            "customers",
            CUSTOMER_COLUMNS,
            (
                (
                    row.customer_id,
                    row.customer_hash,
                    row.signup_date,
                    row.consent_status,
                    row.consent_updated_at,
                    row.salary_day,
                )
                for row in dataset.customers
            ),
        )
        subscriptions = _copy(
            conn,
            "subscriptions",
            SUBSCRIPTION_COLUMNS,
            (
                (
                    row.subscription_id,
                    row.customer_id,
                    row.method,
                    row.bank,
                    row.mcc_category,
                    row.amount_paise,
                    row.status,
                    row.mandate_start,
                    row.paid_count,
                    row.remaining_count,
                    row.cohort,
                )
                for row in dataset.subscriptions
            ),
        )
        invoices = _copy(
            conn,
            "invoices",
            INVOICE_COLUMNS,
            (
                (
                    row.invoice_id,
                    row.subscription_id,
                    row.cycle_number,
                    row.amount_paise,
                    row.charge_at,
                    row.notice_sent_at,
                    row.status,
                )
                for row in dataset.invoices
            ),
        )
        attempts = _copy(
            conn,
            "payment_attempts",
            ATTEMPT_COLUMNS,
            (_attempt_tuple(row) for row in dataset.attempts),
        )

        report = LoadReport(
            dataset_version=dataset.dataset_version,
            dataset_fingerprint=digest,
            customers=customers,
            subscriptions=subscriptions,
            invoices=invoices,
            attempts=attempts,
            censored=sum(1 for row in dataset.attempts if not row.observed),
        )
        # Written last and inside the same transaction as the rows it describes. A
        # manifest that could be committed without them would be a claim about a world
        # that does not exist.
        conn.execute(
            """
            INSERT INTO world_manifest (
                dataset_version, dataset_fingerprint,
                customers, subscriptions, invoices, attempts, censored_attempts
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                report.dataset_version,
                report.dataset_fingerprint,
                report.customers,
                report.subscriptions,
                report.invoices,
                report.attempts,
                report.censored,
            ),
        )

    return report


def loaded_manifest() -> dict:
    """What the database says it is holding.

    Raises rather than returning a default when there is nothing to report: an empty
    database is a condition where the caller's next line would be wrong, and it should
    not be discoverable only by the numbers looking odd later.
    """
    with agent_connection() as conn:
        row = conn.execute("SELECT * FROM world_manifest").fetchone()

    if row is None:
        raise LoadError("no world loaded — run `python -m sim.load` first")
    return dict(row)


def require_fingerprint(expected: str | None = None) -> str:
    """Assert the database holds the dataset the caller is about to reason about.

    Called by the agent before a batch — the one place where being wrong is permanent,
    because the audit rows are append-only. The API is deliberately not a caller: it is a
    read-only view and refusing to start would take the dashboard down over a condition
    ``/health`` already reports. The cost of the check is one query; the cost of skipping
    it is a run whose numbers are attributed to the wrong world, discovered — if ever —
    by someone re-deriving a figure by hand.
    """
    expected = build_dataset().fingerprint() if expected is None else expected
    actual = loaded_manifest()["dataset_fingerprint"]
    if actual != expected:
        raise LoadError(
            f"database holds dataset {actual}, code expects {expected}. "
            "Re-run `python -m sim.load` and then `python -m eval`."
        )
    return actual


def main() -> int:
    # An argument parser for a command that takes no arguments, because without one this
    # module accepted anything and wiped the database anyway. `python -m sim.load
    # --dry-run` would have destroyed the world it was asked not to touch. argparse
    # rejects the unknown flag instead, which is the whole reason it is here.
    argparse.ArgumentParser(
        prog="python -m sim.load",
        description=(
            "Replace the world in Postgres with the frozen dataset. Always a full "
            "replace: there is no incremental mode, because a partially-refreshed world "
            "is a world whose fingerprint means nothing. Re-run `python -m eval` after."
        ),
    ).parse_args()

    print(load())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

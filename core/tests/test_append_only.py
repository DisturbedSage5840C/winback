"""The audit trail is append-only because the database says so, not because we say so.

``db/02_append_only.sql`` and ``db/03_grants.sql`` defend the same property with two
independent mechanisms, and these tests exercise them separately because each alone
has a hole:

  * **Grant layer** -- ``winback_agent`` has no UPDATE/DELETE on the fact tables, so
    the application is refused before a trigger is even consulted. A superuser can
    still get past this.
  * **Trigger layer** -- the ``BEFORE UPDATE OR DELETE`` trigger refuses *everyone*,
    ``winback_owner`` included. This is the one that answers "what stops you, at a
    psql prompt, from editing the number in your own demo?".

Everything runs inside a transaction that is rolled back, so the suite leaves no
residue in the very tables it is proving are immutable.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from core.db import agent_connection, owner_connection

pytestmark = pytest.mark.db

IMMUTABLE_TABLES = ("audit_log", "decisions", "payment_attempts")


def _connect(factory):  # type: ignore[no-untyped-def]
    try:
        with factory() as connection:
            yield connection
    except psycopg.OperationalError as exc:  # pragma: no cover - environment guard
        pytest.skip(f"Postgres not reachable ({exc}); run `docker compose up -d`")


@pytest.fixture(scope="module")
def agent_conn() -> Iterator[psycopg.Connection]:
    """How the agent and the simulator connect. Insert-only on the fact tables."""
    yield from _connect(agent_connection)


@pytest.fixture(scope="module")
def owner_conn() -> Iterator[psycopg.Connection]:
    """The superuser. Grants do not constrain it; the trigger still does."""
    yield from _connect(owner_connection)


@pytest.fixture
def seeded(owner_conn: psycopg.Connection) -> Iterator[dict[str, str]]:
    """One row in every immutable table, rolled back afterwards."""
    suffix = uuid.uuid4().hex[:10]
    ids = {
        "customer": f"cust_{suffix}",
        "subscription": f"sub_{suffix}",
        "invoice": f"inv_{suffix}",
        "attempt": f"att_{suffix}",
        "decision": f"dec_{suffix}",
        "run": f"run_{suffix}",
    }
    now = datetime.now(UTC)

    with owner_conn.transaction(force_rollback=True):
        owner_conn.execute(
            """
            INSERT INTO customers (customer_id, customer_hash, signup_date,
                                   consent_status, consent_updated_at, salary_day)
            VALUES (%s, %s, %s, 'active', %s, 7)
            """,
            (
                ids["customer"],
                hashlib.sha256(ids["customer"].encode()).hexdigest()[:12],
                now.date(),
                now,
            ),
        )
        owner_conn.execute(
            """
            INSERT INTO subscriptions (subscription_id, customer_id, method, bank,
                                       mcc_category, amount_paise, status,
                                       mandate_start, cohort)
            VALUES (%s, %s, 'upi_autopay', 'HDFC', 'saas', 49900, 'pending', %s, 'test')
            """,
            (ids["subscription"], ids["customer"], now.date()),
        )
        owner_conn.execute(
            """
            INSERT INTO invoices (invoice_id, subscription_id, cycle_number,
                                  amount_paise, charge_at, status)
            VALUES (%s, %s, 1, 49900, %s, 'at_risk')
            """,
            (ids["invoice"], ids["subscription"], now - timedelta(days=1)),
        )
        owner_conn.execute(
            """
            INSERT INTO payment_attempts (attempt_id, invoice_id, subscription_id,
                                          attempt_number, attempted_at, is_non_peak,
                                          action, amount_paise, outcome, oracle_seed)
            VALUES (%s, %s, %s, 1, %s, TRUE, 'initial_charge', 49900, 'failed', 'seed')
            """,
            (ids["attempt"], ids["invoice"], ids["subscription"], now - timedelta(days=1)),
        )
        owner_conn.execute(
            """
            INSERT INTO decisions (decision_id, run_id, arm, invoice_id, subscription_id,
                                   triggering_attempt_id, model_version, proposed_action,
                                   guardrail_verdict, authorizing_rule, final_action)
            VALUES (%s, %s, 'D', %s, %s, %s, 'v0-test', 'retry_now',
                    'APPROVE', 'test_rule', 'retry_now')
            """,
            (ids["decision"], ids["run"], ids["invoice"], ids["subscription"], ids["attempt"]),
        )
        owner_conn.execute(
            """
            INSERT INTO audit_log (run_id, arm, agent_id, agent_version, subject_type,
                                   subject_id, trigger, decision_id, action_taken,
                                   execution_mode, outcome, recovered_amount_paise,
                                   compliance_violation)
            VALUES (%s, 'D', 'winback', 'v0-test', 'invoice', %s, 'batch_scan', %s,
                    'retry_now', 'simulated', 'recovered', 49900, TRUE)
            """,
            (ids["run"], ids["invoice"], ids["decision"]),
        )
        yield ids


def _expect_refusal(
    conn: psycopg.Connection,
    error: type[psycopg.Error],
    sql: str,
    params: tuple[object, ...] = (),
) -> str:
    """Run a statement expected to be refused; return the error message.

    The savepoint matters: without it the first refusal aborts the surrounding
    transaction and every later assertion fails for the wrong reason.
    """
    with pytest.raises(error) as excinfo, conn.transaction():
        conn.execute(sql, params)
    return str(excinfo.value)


# --------------------------------------------------------------- layer 1: grants


@pytest.mark.parametrize("table", IMMUTABLE_TABLES)
def test_agent_role_has_no_delete_grant(agent_conn: psycopg.Connection, table: str) -> None:
    """Permission is checked before any row is touched, so this needs no fixture data."""
    message = _expect_refusal(
        agent_conn, psycopg.errors.InsufficientPrivilege, f"DELETE FROM {table} WHERE false"
    )
    assert "permission denied" in message


@pytest.mark.parametrize("table", IMMUTABLE_TABLES)
def test_agent_role_has_no_update_grant(agent_conn: psycopg.Connection, table: str) -> None:
    message = _expect_refusal(
        agent_conn,
        psycopg.errors.InsufficientPrivilege,
        f"UPDATE {table} SET run_id = run_id WHERE false",
    )
    assert "permission denied" in message


def test_agent_role_can_still_do_its_job(agent_conn: psycopg.Connection) -> None:
    """Locking down history must not lock out the agent: it still appends facts and
    advances live state. A guardrail that breaks the product is not a guardrail."""
    with agent_conn.transaction(force_rollback=True):
        agent_conn.execute(
            """
            INSERT INTO audit_log (agent_id, agent_version, subject_type, subject_id,
                                   trigger, action_taken, execution_mode, outcome)
            VALUES ('winback', 'v0-test', 'subscription', 'sub_grantcheck',
                    'grant_check', 'noop', 'simulated', 'deferred')
            """
        )
        row = agent_conn.execute(
            "SELECT count(*) AS n FROM audit_log WHERE subject_id = 'sub_grantcheck'"
        ).fetchone()

    assert row is not None
    assert row["n"] == 1


# --------------------------------------------------------------- layer 2: triggers


@pytest.mark.parametrize("table", IMMUTABLE_TABLES)
def test_trigger_refuses_delete_even_for_the_owner(
    owner_conn: psycopg.Connection, seeded: dict[str, str], table: str
) -> None:
    """The question a panelist actually asks: what stops *you* from editing this?"""
    message = _expect_refusal(
        owner_conn, psycopg.errors.RestrictViolation, f"DELETE FROM {table}"
    )
    assert "append_only_violation" in message
    assert "DELETE" in message


def test_a_recorded_violation_cannot_be_downgraded(
    owner_conn: psycopg.Connection, seeded: dict[str, str]
) -> None:
    """The attack this table exists to stop: quietly clearing a compliance breach."""
    message = _expect_refusal(
        owner_conn,
        psycopg.errors.RestrictViolation,
        "UPDATE audit_log SET compliance_violation = FALSE WHERE subject_id = %s",
        (seeded["invoice"],),
    )
    assert "append_only_violation" in message


def test_recovered_amount_cannot_be_inflated(
    owner_conn: psycopg.Connection, seeded: dict[str, str]
) -> None:
    """The other attack: making the headline number bigger after the run."""
    message = _expect_refusal(
        owner_conn,
        psycopg.errors.RestrictViolation,
        "UPDATE audit_log SET recovered_amount_paise = 9999999 WHERE subject_id = %s",
        (seeded["invoice"],),
    )
    assert "append_only_violation" in message


def test_attempt_outcome_cannot_be_rewritten(
    owner_conn: psycopg.Connection, seeded: dict[str, str]
) -> None:
    """Rewriting an attempt rewrites both the training data and the evaluation."""
    message = _expect_refusal(
        owner_conn,
        psycopg.errors.RestrictViolation,
        "UPDATE payment_attempts SET outcome = 'captured' WHERE attempt_id = %s",
        (seeded["attempt"],),
    )
    assert "append_only_violation" in message


def test_guardrail_verdict_cannot_be_rewritten(
    owner_conn: psycopg.Connection, seeded: dict[str, str]
) -> None:
    """A DENY stays a DENY. Overturning one means a new, superseding decision row."""
    message = _expect_refusal(
        owner_conn,
        psycopg.errors.RestrictViolation,
        "UPDATE decisions SET guardrail_verdict = 'APPROVE' WHERE decision_id = %s",
        (seeded["decision"],),
    )
    assert "append_only_violation" in message


@pytest.mark.parametrize("table", IMMUTABLE_TABLES)
def test_truncate_is_refused(owner_conn: psycopg.Connection, table: str) -> None:
    """TRUNCATE is neither UPDATE nor DELETE and needs a statement-level trigger of
    its own -- without it, the whole scheme has a one-word bypass."""
    message = _expect_refusal(
        owner_conn, psycopg.errors.RestrictViolation, f"TRUNCATE TABLE {table} CASCADE"
    )
    assert "append_only_violation" in message
    assert "TRUNCATE" in message


# --------------------------------------------------------------- corrections & state


def test_supersede_is_the_sanctioned_correction_path(
    owner_conn: psycopg.Connection, seeded: dict[str, str]
) -> None:
    """History is immutable but not unamendable: a reversal is appended, not applied."""
    with owner_conn.transaction():
        owner_conn.execute(
            """
            INSERT INTO decisions (decision_id, run_id, arm, invoice_id, subscription_id,
                                   model_version, proposed_action, guardrail_verdict,
                                   authorizing_rule, final_action,
                                   supersedes_decision_id, decided_by)
            VALUES (%s, %s, 'D', %s, %s, 'v0-test', 'escalate_human', 'DENY',
                    'human_override', 'write_off', %s, 'human')
            """,
            (
                f"{seeded['decision']}_rev",
                seeded["run"],
                seeded["invoice"],
                seeded["subscription"],
                seeded["decision"],
            ),
        )
        row = owner_conn.execute(
            "SELECT supersedes_decision_id, decided_by FROM decisions WHERE decision_id = %s",
            (f"{seeded['decision']}_rev",),
        ).fetchone()

    assert row is not None
    assert row["supersedes_decision_id"] == seeded["decision"]
    assert row["decided_by"] == "human"


def test_live_state_remains_mutable(
    owner_conn: psycopg.Connection, seeded: dict[str, str]
) -> None:
    """Immutability applies to facts, not to state. An invoice has to be able to move
    from at_risk to recovered or the product does not work."""
    with owner_conn.transaction():
        owner_conn.execute(
            "UPDATE invoices SET status = 'recovered' WHERE invoice_id = %s",
            (seeded["invoice"],),
        )
        row = owner_conn.execute(
            "SELECT status FROM invoices WHERE invoice_id = %s", (seeded["invoice"],)
        ).fetchone()

    assert row is not None
    assert row["status"] == "recovered"


def test_ist_wall_clock_is_derived_by_the_database(
    owner_conn: psycopg.Connection, seeded: dict[str, str]
) -> None:
    """Every NPCI rule in this system is expressed in IST wall-clock. Deriving it in
    application code in more than one place is how a compliance bug gets shipped."""
    row = owner_conn.execute(
        """
        SELECT attempted_at, attempted_at_ist,
               attempted_at_ist - (attempted_at AT TIME ZONE 'UTC') AS offset_applied
        FROM payment_attempts WHERE attempt_id = %s
        """,
        (seeded["attempt"],),
    ).fetchone()

    assert row is not None
    assert row["offset_applied"] == timedelta(hours=5, minutes=30)

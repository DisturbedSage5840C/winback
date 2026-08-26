"""Database access.

Three connection factories mirroring the three roles in ``db/03_grants.sql``. The
split is not ceremony: ``read_connection()`` physically cannot write, so the API and
the dashboard cannot corrupt the audit trail even with a bug in a query builder.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from core.config import get_settings


@contextmanager
def agent_connection() -> Iterator[psycopg.Connection]:
    """The agent and the simulator. INSERT-only on the immutable fact tables."""
    with psycopg.connect(get_settings().db_url, row_factory=dict_row) as conn:
        yield conn


@contextmanager
def read_connection() -> Iterator[psycopg.Connection]:
    """The FastAPI backend. SELECT only, enforced by the grant, not by convention."""
    with psycopg.connect(get_settings().db_url_readonly, row_factory=dict_row) as conn:
        yield conn


@contextmanager
def owner_connection() -> Iterator[psycopg.Connection]:
    """Migrations and world regeneration only. Nothing in the request path uses this."""
    with psycopg.connect(get_settings().db_url_owner, row_factory=dict_row) as conn:
        yield conn


def reset_world() -> None:
    """Drop every generated fact and rebuild from scratch.

    Routed through the ``winback_reset_world()`` SECURITY DEFINER function rather
    than issuing DELETEs, so the one path that removes immutable rows stays a single
    named, greppable, log-emitting call. See ``db/02_append_only.sql``.
    """
    with agent_connection() as conn:
        conn.execute("SELECT winback_reset_world()")
        conn.commit()


def healthcheck() -> dict[str, object]:
    """Enough to fail fast in ``scripts/run_demo.sh`` with a useful message."""
    with read_connection() as conn:
        row = conn.execute(
            """
            SELECT current_database()                                    AS database,
                   current_user                                          AS role,
                   (SELECT count(*) FROM pg_tables
                     WHERE schemaname = 'public')                        AS tables,
                   (SELECT count(*) FROM pg_trigger
                     WHERE NOT tgisinternal
                       AND tgname LIKE '%_no_mutate')                    AS append_only_triggers
            """
        ).fetchone()
    assert row is not None
    return dict(row)

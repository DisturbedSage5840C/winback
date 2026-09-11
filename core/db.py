"""Database access.

Three connection factories mirroring the three roles in ``db/03_grants.sql``. The
split is not ceremony: ``read_connection()`` physically cannot write, so the API and
the dashboard cannot corrupt the audit trail even with a bug in a query builder.

**The agent and reader roles are pooled; the owner role is not.** Before this, every
call opened a fresh TCP connection and ran the Postgres auth handshake from scratch —
cheap in isolation, and expensive at the rates this project actually runs at:
``agent/hooks.py`` opens one connection per audit row, so a 190-invoice batch alone
opened several hundred, and a single dashboard page load fans out to four or five API
handlers, each opening and closing its own. A pool amortises that cost across calls
instead of paying it every time. ``owner_connection()`` stays unpooled on purpose —
migrations and world regeneration are rare, already expensive relative to a connection
handshake, and destructive enough when misused that the extra caution of a connection
nobody else is holding a reference to is worth more than the latency it costs.
"""

from __future__ import annotations

import atexit
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from core.config import get_settings

#: One pool per role, created lazily on first use and kept for the life of the
#: process. Keyed by role name rather than by DSN because ``get_settings()`` is itself
#: cached for the life of the process — the DSN it returns cannot change out from
#: under a pool already open on it.
_pools: dict[str, ConnectionPool] = {}

#: Small on purpose. Nothing in this project's demo, batch, or API load has ever needed
#: more than a handful of connections concurrently, and a pool sized for load nobody
#: has measured is a guess dressed up as an optimisation.
_MIN_SIZE = 1
_MAX_SIZE = 10

#: Set on the first pool this process ever opens, so the `atexit` fallback below is
#: registered exactly once per process, not once per pool -- `close_pools()` clears
#: `_pools`, so a process that closes and later reopens (every test in
#: ``core/tests/test_db.py`` does this) must not re-register the hook each time.
_atexit_armed = False


def _pool(role: str, conninfo: str) -> ConnectionPool:
    global _atexit_armed
    pool = _pools.get(role)
    if pool is None:
        pool = ConnectionPool(
            conninfo,
            min_size=_MIN_SIZE,
            max_size=_MAX_SIZE,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        _pools[role] = pool
        if not _atexit_armed:
            # A process that never explicitly calls `close_pools()` -- `eval.report`,
            # `ml`, and any other short CLI run rather than a long-lived server --
            # still leaves a pool with live background threads at interpreter exit.
            # `atexit` runs before Python's own shutdown finalization begins, which
            # matters here: a pool nobody closed tries to join those threads from its
            # own `__del__` *during* that finalization, and on 3.14 that raises
            # `PythonFinalizationError` instead of just leaking quietly.
            # `close_pools()` from an explicit shutdown path (`api/main.py`'s
            # lifespan, `agent/orchestrator.py`'s `main()`) empties `_pools` first,
            # making this fallback a no-op wherever one already runs.
            atexit.register(close_pools)
            _atexit_armed = True
    return pool


def close_pools() -> None:
    """Release every pooled connection. Idempotent, so it is safe to call from both
    an explicit shutdown path and the `atexit` fallback registered above.

    Not calling this is not a leak in the way an unclosed file handle is — the
    connections die with the process either way — but closing them cleanly lets
    Postgres see a normal disconnect instead of however many simultaneous resets a
    killed TCP connection produces.
    """
    for pool in _pools.values():
        pool.close()
    _pools.clear()


@contextmanager
def agent_connection() -> Iterator[psycopg.Connection]:
    """The agent and the simulator. INSERT-only on the immutable fact tables."""
    with _pool("agent", get_settings().db_url).connection() as conn:
        yield conn


@contextmanager
def read_connection() -> Iterator[psycopg.Connection]:
    """The FastAPI backend. SELECT only, enforced by the grant, not by convention."""
    with _pool("read", get_settings().db_url_readonly).connection() as conn:
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

"""The pooling contract: agent and reader are pooled, owner is not, and closing works.

Nothing exercised the mechanics of ``core.db`` itself before pooling landed — every
other test file imports ``agent_connection``/``owner_connection`` and uses them, but
none of them asserted anything about *how* a connection is produced. These tests are
about that layer: that a pooled connection survives its ``with`` block (so the next
caller gets it back) while an owner connection does not, that the three roles really
are three different Postgres roles, and that ``close_pools()`` closes what it claims to.

Every test closes the pools it touches on the way out, so pool state from one test
never leaks into the next — including into ``test_append_only.py``'s
module-scoped ``agent_connection`` fixture, which would otherwise see whatever
connection object this file left checked out.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from core import db
from core.db import agent_connection, close_pools, healthcheck, owner_connection, read_connection

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _reset_pools() -> Iterator[None]:
    """A closed pool state on the way in and the way out."""
    close_pools()
    try:
        yield
    finally:
        close_pools()


def _skip_if_unreachable(exc: psycopg.OperationalError) -> None:
    pytest.skip(f"Postgres not reachable ({exc}); run `docker compose up -d`")


def test_agent_and_reader_connect_as_different_roles() -> None:
    """The split ``read_connection()``'s docstring claims -- checked, not assumed."""
    try:
        with agent_connection() as conn:
            agent_role = conn.execute("SELECT current_user").fetchone()["current_user"]
        with read_connection() as conn:
            reader_role = conn.execute("SELECT current_user").fetchone()["current_user"]
    except psycopg.OperationalError as exc:
        _skip_if_unreachable(exc)
        return
    assert agent_role == "winback_agent"
    assert reader_role == "winback_reader"
    assert agent_role != reader_role


def test_a_pooled_connection_survives_its_context_exit() -> None:
    """Exiting ``with agent_connection()`` returns the connection to the pool -- it
    does not close the socket. A closed connection here would mean every call pays
    the handshake cost pooling exists to amortise."""
    try:
        with agent_connection() as conn:
            pass
    except psycopg.OperationalError as exc:
        _skip_if_unreachable(exc)
        return
    assert not conn.closed


def test_an_owner_connection_does_not_survive_its_context_exit() -> None:
    """The one factory that stays a bare ``psycopg.connect()`` on purpose."""
    try:
        with owner_connection() as conn:
            pass
    except psycopg.OperationalError as exc:
        _skip_if_unreachable(exc)
        return
    assert conn.closed


def test_repeated_agent_connections_reuse_the_pool_instead_of_opening_fresh_ones() -> None:
    """The actual point of pooling: ten calls should not open ten connections.

    ``ConnectionPool.get_stats()['connections_num']`` counts every connection the
    pool has ever opened, for the life of the pool -- not the same thing as
    ``requests_num``, the number of checkouts served. Before pooling, every call in
    this codebase was a bare ``psycopg.connect()``, so this ratio would have been
    1:1; a pool that is actually being reused serves many more requests than the
    connections it opened to do it.
    """
    try:
        for _ in range(10):
            with agent_connection() as conn:
                conn.execute("SELECT 1")
    except psycopg.OperationalError as exc:
        _skip_if_unreachable(exc)
        return
    stats = db._pools["agent"].get_stats()
    assert stats["requests_num"] == 10
    # A freshly opened pool can race its own background min_size fill against the
    # first foreground checkout, so this can land at 2 rather than a clean 1 -- but
    # never anywhere near one connection per request.
    assert stats["connections_num"] <= 2


def test_close_pools_actually_closes_the_underlying_connections() -> None:
    try:
        with agent_connection() as conn:
            pass
    except psycopg.OperationalError as exc:
        _skip_if_unreachable(exc)
        return
    assert not conn.closed  # returned to the pool, still open
    close_pools()
    assert conn.closed  # the pool closed it, not just forgot about it


def test_close_pools_clears_pool_state_so_the_next_call_reopens() -> None:
    try:
        with agent_connection() as conn:
            pass
    except psycopg.OperationalError as exc:
        _skip_if_unreachable(exc)
        return
    assert "agent" in db._pools
    close_pools()
    assert db._pools == {}
    # And using it again afterwards must not raise -- a closed pool is not a dead one.
    with agent_connection() as conn:
        conn.execute("SELECT 1")


def test_healthcheck_reports_the_reader_role() -> None:
    try:
        result = healthcheck()
    except psycopg.OperationalError as exc:
        _skip_if_unreachable(exc)
        return
    assert result["role"] == "winback_reader"
    assert result["database"] == "winback"
    assert result["tables"] > 0

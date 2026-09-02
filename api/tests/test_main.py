"""The read-only backend: what it is allowed to do, and what it must never invent.

Every test here is marked ``db``. There is no mocked database, because the two properties
worth asserting about this API are both properties of the connection it opens — that it
cannot write, and that its numbers come out of views rather than out of Python. A fixture
standing in for Postgres would satisfy neither.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.main import app

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def a_run(client: TestClient) -> str:
    """The most recent batch. Skips rather than fails on a database with no runs in it —
    an empty world is a legitimate state (``reset_world`` leaves one), and a test that
    treated it as a defect would fail for the wrong reason on a fresh clone."""
    runs = client.get("/runs").json()
    if not runs:
        pytest.skip("no agent run in this database to read")
    return runs[0]["run_id"]


# ------------------------------------------------------------------ the read-only claim


def test_the_api_holds_no_write_verb():
    """The grant is the enforcement, and this is the cheap second lock on it. A ``POST``
    route added to this file would be refused by ``winback_reader`` at the database — but
    it would be refused at request time, in front of a panelist, rather than here."""
    methods = {method for route in app.routes for method in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}


def test_a_write_through_the_read_role_is_refused(client: TestClient):
    """The claim under the claim. ``winback_reader`` holds SELECT and nothing else, so
    even a handler that tried to write could not — which is why the dashboard being
    read-only does not depend on anyone reviewing this file carefully."""
    import psycopg

    from core.db import read_connection

    with pytest.raises(psycopg.errors.InsufficientPrivilege), read_connection() as conn:
        conn.execute(
            "INSERT INTO audit_log (agent_id, agent_version, subject_type, subject_id, "
            "trigger) VALUES ('x', 'x', 'invoice', 'x', 'x')"
        )


# ------------------------------------------------------------------------- the endpoints


def test_health_reports_what_the_database_holds(client: TestClient):
    """Not a bare ``ok``. The failure this catches is a reachable but empty Postgres,
    which every other endpoint would report as a legitimate absence of data."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert isinstance(body["database"], dict)


def test_the_overview_is_the_view_and_not_a_second_opinion(client: TestClient, a_run: str):
    """``recovery_funnel`` is a view in ``db/01_schema.sql``. If this endpoint recomputed
    the funnel, the dashboard and the database could disagree about how much money was
    recovered, and there would be no way to tell which was right."""
    from core.db import read_connection

    body = client.get(f"/runs/{a_run}/overview").json()
    with read_connection() as conn:
        row = conn.execute("SELECT * FROM recovery_funnel WHERE run_id = %s", (a_run,)).fetchone()

    assert row is not None
    for key, value in row.items():
        # Compared numerically, because ``sum()`` comes back as ``Decimal`` and the
        # endpoint deliberately does not — see the test below.
        assert body[key] == (float(value) if isinstance(value, Decimal) else value), key


def test_the_rupee_total_is_a_number_and_not_a_string(client: TestClient, a_run: str):
    """``sum()`` over ``bigint`` is ``numeric`` in Postgres, ``Decimal`` in psycopg, and a
    JSON **string** by the time FastAPI is done with it. The dashboard's headline is that
    field. It would have rendered ``"15058200"`` and, worse, any arithmetic on it would
    have concatenated rather than added — a wrong ₹ total that never raised anything."""
    body = client.get(f"/runs/{a_run}/overview").json()
    assert isinstance(body["recovered_paise"], int)
    assert not isinstance(body["recovered_paise"], bool)

    for run in client.get("/runs").json():
        assert isinstance(run["recovered_paise"], int)
        assert isinstance(run["invoices"], int)


def test_the_overview_names_the_rule_behind_every_block(client: TestClient, a_run: str):
    """A ``blocked`` row with a ``stop_reason`` is a compliance stop; one without is an
    economic judgement about an attempt that was legally available. The funnel counts them
    together, so the breakdown has to be able to separate them again."""
    body = client.get(f"/runs/{a_run}/overview").json()
    reasons = {row["stop_reason"] for row in body["stop_reasons"]}
    assert None not in reasons  # the unlabelled case is named, never left null
    assert sum(row["invoices"] for row in body["stop_reasons"]) == (
        body["blocked"] + body["escalated"]
    )


def test_an_unknown_run_is_a_404_and_not_an_empty_funnel(client: TestClient):
    """Zeroes would render as a batch that ran and recovered nothing, which is a
    different and much worse claim than a batch that does not exist."""
    assert client.get("/runs/no_such_run/overview").status_code == 404


def test_the_worklist_carries_the_attempt_budget(client: TestClient, a_run: str):
    """``attempts_used`` comes from ``exception_worklist``, which excludes counterfactual
    rows. Counting those would tell the page a censored invoice had spent all four NPCI
    attempts when it had spent one — and the page is where a reviewer checks the cap."""
    body = client.get(f"/runs/{a_run}/worklist", params={"limit": 5}).json()
    if not body["rows"]:
        pytest.skip("this run touched no at-risk invoice still in the worklist")

    for row in body["rows"]:
        assert 0 <= row["attempts_used"] <= 4
        assert row["attempts_used"] + row["attempts_remaining"] == 4
        assert "customer_id" in row and "customer_hash" in row


def test_a_finished_run_still_has_a_worklist(client: TestClient, a_run: str):
    """The view filtered ``status = 'at_risk'`` and the agent's own actions move an
    invoice off that status, so a run's worklist emptied itself the moment the run
    succeeded — the page that shows what the batch decided went blank exactly when there
    was something to show. Recorded in ``WHAT_BROKE.md``."""
    body = client.get(f"/runs/{a_run}/worklist", params={"limit": 500}).json()
    assert body["rows"], "a run that concluded invoices has rows to show for them"
    assert len(body["rows"]) == min(body["total"], 500)


def test_the_live_queue_holds_only_what_is_still_outstanding(client: TestClient):
    """The counterpart. This one *is* filtered, at the call site rather than in the view,
    and it is the number that has to fall as the agent works."""
    body = client.get("/worklist", params={"limit": 5}).json()
    for row in body["rows"]:
        assert row["invoice_status"] == "at_risk"


def test_the_worklist_is_ordered_by_rupees_at_risk(client: TestClient, a_run: str):
    body = client.get(f"/runs/{a_run}/worklist", params={"limit": 20}).json()
    amounts = [row["amount_paise"] for row in body["rows"]]
    assert amounts == sorted(amounts, reverse=True)


def test_the_drill_down_returns_the_losing_candidates_too(client: TestClient, a_run: str):
    """The winner alone makes the drill-down a claim. ``candidate_set`` holds every scored
    ``action x slot`` pair including the refused ones and the reason each was refused,
    which is what makes it checkable — and is the whole reason the column exists."""
    rows = client.get(f"/runs/{a_run}/worklist", params={"limit": 1}).json()["rows"]
    if not rows:
        pytest.skip("this run touched no at-risk invoice still in the worklist")

    body = client.get(f"/invoices/{rows[0]['invoice_id']}", params={"run_id": a_run}).json()
    assert body["invoice"]["invoice_id"] == rows[0]["invoice_id"]
    assert body["audit_trail"], "an invoice in the worklist has at least one audit row"

    for decision in body["decisions"]:
        assert isinstance(decision["candidate_set"], list)


def test_an_unknown_invoice_is_a_404(client: TestClient):
    assert client.get("/invoices/inv_9999_99").status_code == 404


def test_the_trace_cursors_on_the_id_and_never_replays_a_row(client: TestClient, a_run: str):
    """``event_id`` is ``BIGSERIAL``. Cursoring on ``ts_utc`` instead would either skip or
    duplicate a row whenever two events shared a microsecond, and the live trace is the
    demo's best twenty seconds — a decision shown twice, or lost, is visible on camera."""
    first = client.get(f"/runs/{a_run}/events", params={"limit": 5}).json()
    assert first["events"], "a run in /runs has audit rows by construction"
    assert [row["event_id"] for row in first["events"]] == sorted(
        row["event_id"] for row in first["events"]
    )
    assert first["cursor"] == first["events"][-1]["event_id"]

    nxt = client.get(f"/runs/{a_run}/events", params={"since": first["cursor"], "limit": 5}).json()
    seen = {row["event_id"] for row in first["events"]}
    assert seen.isdisjoint({row["event_id"] for row in nxt["events"]})


def test_every_traced_event_carries_the_rule_that_authorised_it(client: TestClient, a_run: str):
    """A blocked presentment with the rule that blocked it is the demonstration. A blocked
    presentment on its own is a shrug — so the join onto ``decisions`` is load-bearing."""
    events = client.get(f"/runs/{a_run}/events", params={"limit": 20}).json()["events"]
    acted = [row for row in events if row["action_taken"] in {"retry", "nudge"}]
    if not acted:
        pytest.skip("this page of the trace holds no action")
    for row in acted:
        assert row["authorizing_rule"], row["invoice_id"]
        assert row["guardrail_verdict"] is not None


def test_the_window_countdown_is_served_and_not_left_to_the_browser(client: TestClient):
    """A client clock is not the clock the agent schedules against, and a panel that
    disagreed with the guardrail by a few minutes would be wrong exactly at the boundary —
    the only place the peak-window rule is interesting."""
    body = client.get("/compliance/window").json()
    assert isinstance(body["is_non_peak"], bool)
    assert body["seconds_to_transition"] > 0
    assert len(body["next_legal_slots_ist"]) == 3
    assert client.get("/compliance/window", params={"n": 5}).json()["next_legal_slots_ist"] != []


def test_the_compliance_panel_calls_the_rules_rather_than_restating_them(client: TestClient):
    """The endpoint imports ``compliance/`` and calls the same pure functions the agent
    calls, which is the whole point of it: a panel that recomputed the 1+3 cap in
    TypeScript would be a second implementation of the law, free to drift from the one
    that gates the money, and the screen a reviewer trusts would be the copy."""
    from compliance import npci_retry_cap

    rows = client.get("/worklist", params={"limit": 1}).json()["rows"]
    if not rows:
        pytest.skip("nothing outstanding in this database to evaluate")

    body = client.get(f"/invoices/{rows[0]['invoice_id']}/compliance").json()
    used = body["npci"]["attempts_used"]
    assert body["npci"]["attempts_remaining"] == npci_retry_cap.attempts_remaining(used)
    assert body["npci"]["verdict"] == str(npci_retry_cap.check(used).verdict)

    # Retry and nudge are governed by different rules — NPCI and the window govern
    # presentments, consent governs messages — so an invoice can be un-retryable and
    # contactable at the same time. That case is what the policy turns on, so the panel
    # has to answer both questions rather than one.
    assert "retry" in body and "nudge" in body
    assert body["nudge"]["verdict"] in {"APPROVE", "DENY", "REDIRECT_TO_WINDOW", "ESCALATE_HUMAN"}


def test_the_panel_refuses_to_guess_a_root_cause(client: TestClient):
    """Retryability is decided from Razorpay's own error object and never guessed. An
    invoice with no failed attempt has nothing to classify, and saying so beats defaulting
    it to retryable — which is being wrong in the permissive direction, on the one axis
    where permissive means a violation."""
    from core.db import read_connection

    with read_connection() as conn:
        row = conn.execute(
            "SELECT invoice_id FROM exception_worklist WHERE latest_root_cause IS NULL LIMIT 1"
        ).fetchone()
    if row is None:
        pytest.skip("every invoice in this database has a classified failure")

    body = client.get(f"/invoices/{row['invoice_id']}/compliance").json()
    assert body["retry"]["verdict"] is None
    assert "no root cause" in body["retry"]["detail"]


def test_an_unknown_invoice_has_no_compliance_panel(client: TestClient):
    assert client.get("/invoices/inv_9999_99/compliance").status_code == 404


def test_the_evaluation_comes_from_the_tables_that_generate_the_report(client: TestClient):
    """Same rows as ``docs/EVALUATION.md``, so the page and the committed report cannot
    disagree without one of them being stale — and ``eval.report --check`` catches that."""
    body = client.get("/evaluation")
    if body.status_code == 404:
        pytest.skip("no evaluation run persisted in this database")

    payload = body.json()
    assert {arm["arm"] for arm in payload["arms"]} == {"A", "B", "C", "D"}
    for arm in payload["arms"]:
        # The constraint the table itself enforces, asserted again at the edge that
        # serves it: violations are exactly the attempts the guardrail would have refused.
        assert (
            arm["attempts_consumed"] - arm["legal_attempts_consumed"]
            == arm["compliance_violations"]
        )


def test_the_config_endpoint_never_returns_the_secret(client: TestClient):
    """The key id is public and identifies the test account. The secret has no endpoint,
    and this test exists so that adding one is a deliberate act rather than a typo."""
    body = client.get("/config").json()
    assert "razorpay_key_secret" not in body
    assert not any("secret" in key for key in body)
    if body["razorpay_key_id"]:
        assert body["razorpay_key_id"].startswith("rzp_test_")


def test_the_config_endpoint_reports_the_version_that_actually_scored(client: TestClient):
    """Read from the last decision written, not from a constant in this process. A
    constant reports what this deployment *would* use; the column reports what scored the
    rows about to be displayed."""
    body = client.get("/config").json()
    assert body["execution_mode"] in {"simulated", "live"}
    assert body["model_version"] in {None, "v1"}

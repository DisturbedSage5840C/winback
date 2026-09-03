"""The read-only backend the dashboard reads.

**Read-only is a grant, not a convention.** Every endpoint here goes through
``core.db.read_connection``, which connects as ``winback_reader`` — a role holding
``SELECT`` and nothing else. A bug in this file cannot corrupt the audit trail, and
"can the dashboard move money" is answerable from ``db/03_grants.sql`` rather than from
a careful reading of the request handlers. That is the whole reason the third role
exists.

**No number is computed here that the database can compute.** ``recovery_funnel`` and
``exception_worklist`` are views in ``db/01_schema.sql``, and the four-arm results live
in ``eval_arm_results``. An API that recalculated either would be a second implementation
of the evaluation, free to disagree with ``docs/EVALUATION.md`` — and the whole point of
generating that file from Postgres was to have exactly one place where the numbers are
decided. So the handlers below select and shape; they do not aggregate.

Rupees are returned as paise, in integers, and formatted at the edge. A float rupee value
is a rounding error waiting for a total.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from compliance import (
    afa_threshold,
    consent_gate,
    guardrail,
    non_peak_window,
    npci_retry_cap,
    pre_debit_notice,
)
from compliance.guardrail import ActionKind, ActionRequest
from compliance.result import RuleResult
from compliance.root_cause import RootCause
from core.config import get_settings
from core.db import healthcheck, read_connection

IST = ZoneInfo("Asia/Kolkata")

app = FastAPI(
    title="Winback API",
    version="1.0",
    summary="Read-only access to the recovery batch, its audit trail, and the evaluation.",
)

# The dashboard is served from a different origin in development. Credentials are never
# sent — there is no auth on a read-only view of a test-mode dataset — so the permissive
# origin list costs nothing and is scoped to local development anyway.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8443",
        "http://127.0.0.1:8443",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _number(value: Any) -> Any:
    """Postgres ``sum()`` over ``bigint`` returns ``numeric``, and psycopg maps ``numeric``
    to :class:`~decimal.Decimal` — which FastAPI serialises as a JSON **string**. The
    dashboard would then receive ``"15058200"`` for the headline rupees recovered and
    either render it verbatim or add it to something and get a concatenation.

    JSON has no decimal type, so a ``Decimal`` has to become one of the two things JSON
    does have. It becomes an integer when it is one — every paise total and every count in
    this API is integral — and a float otherwise, which is where the genuinely fractional
    numbers live: bootstrap interval bounds and ECE in the ``eval_*`` tables. Nothing is
    rounded; an integral ``Decimal`` converts exactly.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _rows(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with read_connection() as conn:
        return [
            {key: _number(value) for key, value in row.items()}
            for row in conn.execute(sql, params or {}).fetchall()
        ]


def _row(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


# ------------------------------------------------------------------------ health


@app.get("/health")
def health() -> dict[str, Any]:
    """Whether the database is reachable and what it holds.

    Returns the row counts rather than a bare ``{"ok": true}``, because the failure this
    catches in practice is not an unreachable Postgres — it is a reachable one that has
    been reset and is empty, which every endpoint below would then report as a legitimate
    absence of data.
    """
    return {"status": "ok", "database": healthcheck()}


# -------------------------------------------------------------------------- runs


@app.get("/runs")
def runs() -> list[dict[str, Any]]:
    """Every batch the agent has run, newest first.

    Sourced from ``audit_log`` because that table is append-only: a run exists here if and
    only if it wrote something that cannot be taken back. There is no separate runs table
    to fall out of step with it.
    """
    return _rows(
        """
        SELECT run_id,
               arm,
               min(ts_utc)                                                AS started_at,
               max(ts_utc)                                                AS ended_at,
               count(DISTINCT subject_id)                                 AS invoices,
               count(*)                                                   AS audit_rows,
               count(*) FILTER (WHERE outcome = 'recovered')              AS recovered,
               coalesce(sum(recovered_amount_paise), 0)                   AS recovered_paise,
               count(*) FILTER (WHERE compliance_violation)               AS violations,
               max(execution_mode) FILTER (WHERE execution_mode = 'live') IS NOT NULL
                                                                          AS touched_live
          FROM audit_log
         WHERE subject_type = 'invoice'
         GROUP BY run_id, arm
         ORDER BY started_at DESC
        """
    )


@app.get("/runs/{run_id}/overview")
def overview(run_id: str) -> dict[str, Any]:
    """The funnel, straight out of the ``recovery_funnel`` view.

    This is the page that carries the ₹-recovered headline, so it is the page most likely
    to be quoted at me. It therefore adds nothing of its own.
    """
    row = _row("SELECT * FROM recovery_funnel WHERE run_id = %(run)s", {"run": run_id})
    if row is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} in audit_log")

    # The stop reasons behind the blocks, which is the part of the funnel that is an
    # argument rather than a count. A `blocked` with a reason is a compliance stop; one
    # without is an economic judgement about an attempt that was legally available.
    row["stop_reasons"] = _rows(
        """
        SELECT coalesce(stop_reason, 'declined_on_expected_value') AS stop_reason,
               count(*)                                            AS invoices
          FROM audit_log
         WHERE run_id = %(run)s AND outcome IN ('blocked', 'escalated')
         GROUP BY 1
         ORDER BY 2 DESC
        """,
        {"run": run_id},
    )
    return row


@app.get("/runs/{run_id}/worklist")
def worklist(
    run_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    outcome: str | None = Query(None, description="recovered|failed|deferred|escalated|blocked"),
) -> dict[str, Any]:
    """Every invoice this run touched, with what it decided about each, ranked by rupees.

    Joined onto ``exception_worklist``, which already computes the attempt budget — and
    computes it excluding counterfactual attempts, for a reason worth repeating here:
    counting oracle rows as consumed budget would tell the worklist that a censored
    invoice had spent all four NPCI attempts when it had spent one.

    This is the *run* worklist, and it is deliberately not filtered to at-risk invoices.
    A concluded invoice is no longer at risk — that is what the run did to it — so
    filtering here would empty this page precisely when the batch succeeded. The live
    queue of unworked invoices is ``GET /worklist``.
    """
    # The optional filter is a parameter, not an interpolated fragment. Every query in
    # this file is a constant string: nothing a caller sends ever becomes SQL, so the
    # read-only grant is not the only thing standing between a query string and the
    # database.
    rows = _rows(
        """
        WITH latest AS (
            SELECT DISTINCT ON (subject_id)
                   subject_id AS invoice_id, action_taken, outcome, stop_reason,
                   recovered_amount_paise, execution_mode, razorpay_entity_id,
                   decision_id, ts_ist, compliance_violation
              FROM audit_log
             WHERE run_id = %(run)s AND subject_type = 'invoice'
             ORDER BY subject_id, ts_utc DESC
        )
        SELECT w.*, d.action_taken, d.outcome, d.stop_reason,
               d.recovered_amount_paise, d.execution_mode, d.razorpay_entity_id,
               d.decision_id, d.ts_ist AS decided_at_ist, d.compliance_violation
          FROM exception_worklist w
          JOIN latest d USING (invoice_id)
         WHERE (%(outcome)s::text IS NULL OR d.outcome = %(outcome)s)
         ORDER BY w.amount_paise DESC
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"run": run_id, "limit": limit, "offset": offset, "outcome": outcome},
    )
    total = _row(
        """
        SELECT count(DISTINCT subject_id) AS n FROM audit_log
         WHERE run_id = %(run)s AND subject_type = 'invoice'
        """,
        {"run": run_id},
    )
    return {"run_id": run_id, "total": (total or {}).get("n", 0), "rows": rows}


@app.get("/runs/{run_id}/events")
def events(
    run_id: str,
    since: int | None = Query(None, ge=0, description="event_id from the previous poll"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """The run's audit rows in the order they were appended — the live trace.

    Polled with ``since`` set to the last ``event_id`` seen, so a page watching a batch
    receives each decision once as it lands. Cursoring on the ``BIGSERIAL`` id rather
    than on a timestamp is deliberate: two rows can share a microsecond, and a client
    resuming from a timestamp would either skip one or replay it. ``audit_log`` is
    append-only, so a row already sent can never change under the cursor, and the batch
    is a single writer, so ids commit in the order they are issued.

    Each row carries the ``authorizing_rule`` from its decision, because the rule is the
    interesting part of the event — a blocked presentment with the rule that blocked it
    is the demonstration; a blocked presentment on its own is a shrug.
    """
    rows = _rows(
        """
        SELECT a.event_id, a.ts_utc, a.ts_ist, a.subject_id AS invoice_id,
               a.action_taken, a.channel, a.outcome, a.stop_reason,
               a.recovered_amount_paise, a.execution_mode, a.razorpay_entity_id,
               a.compliance_violation, a.trigger,
               d.authorizing_rule, d.guardrail_verdict, d.calibrated_prob,
               d.expected_value_paise
          FROM audit_log a
          LEFT JOIN decisions d USING (decision_id)
         WHERE a.run_id = %(run)s
           AND (%(since)s::bigint IS NULL OR a.event_id > %(since)s::bigint)
         ORDER BY a.event_id
         LIMIT %(limit)s
        """,
        {"run": run_id, "since": since, "limit": limit},
    )
    return {
        "run_id": run_id,
        "cursor": rows[-1]["event_id"] if rows else since,
        "events": rows,
    }


@app.get("/worklist")
def live_worklist(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """The queue: invoices still at risk, nothing decided about them yet.

    The counterpart to the run worklist above. This one is the merchant's inbox — what
    is outstanding right now, ranked by rupees, with the NPCI budget each one has left —
    and it shrinks as the agent works, which is the behaviour that makes it a queue.
    """
    rows = _rows(
        """
        SELECT * FROM exception_worklist
         WHERE invoice_status = 'at_risk'
         ORDER BY amount_paise DESC
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"limit": limit, "offset": offset},
    )
    total = _row("SELECT count(*) AS n FROM exception_worklist WHERE invoice_status = 'at_risk'")
    return {"total": (total or {}).get("n", 0), "rows": rows}


# ------------------------------------------------------------------- drill-down


@app.get("/invoices/{invoice_id}")
def invoice(invoice_id: str, run_id: str | None = None) -> dict[str, Any]:
    """One invoice, end to end: the facts, every decision, and the whole audit trail.

    ``candidate_set`` comes back in full — every scored ``action x slot`` pair the policy
    considered, including the ones the guardrail refused and the reason each was refused.
    The winner alone would make the drill-down a claim; the losers are what make it
    checkable, and they are the reason the column exists.
    """
    facts = _row(
        """
        SELECT i.*, s.method, s.bank, s.mcc_category, s.status AS subscription_status,
               s.mandate_start, s.paid_count, s.remaining_count, s.cohort,
               c.customer_hash, c.consent_status, c.consent_updated_at, c.salary_day
          FROM invoices i
          JOIN subscriptions s USING (subscription_id)
          JOIN customers c USING (customer_id)
         WHERE i.invoice_id = %(inv)s
        """,
        {"inv": invoice_id},
    )
    if facts is None:
        raise HTTPException(status_code=404, detail=f"no invoice {invoice_id!r}")

    # ``run_id`` narrows the drill-down to one batch when given, and shows every batch
    # that ever touched this invoice when omitted — expressed as a parameter rather than
    # an appended clause, for the reason given in `worklist`.
    params = {"inv": invoice_id, "run": run_id}

    return {
        "invoice": facts,
        # Every attempt, observed and counterfactual, flagged — because which of the two
        # a row is decides whether it consumed an NPCI attempt.
        "attempts": _rows(
            """
            SELECT attempt_id, attempt_number, attempted_at, attempted_at_ist, outcome,
                   action, amount_paise, error_code, error_source, error_reason,
                   root_cause_class, is_non_peak, observed, run_id
              FROM payment_attempts
             WHERE invoice_id = %(inv)s
             ORDER BY attempt_number, attempted_at
            """,
            {"inv": invoice_id},
        ),
        "decisions": _rows(
            """
            SELECT * FROM decisions
             WHERE invoice_id = %(inv)s
               AND (%(run)s::text IS NULL OR run_id = %(run)s)
             ORDER BY decided_at
            """,
            params,
        ),
        "audit_trail": _rows(
            """
            SELECT * FROM audit_log
             WHERE subject_id = %(inv)s AND subject_type = 'invoice'
               AND (%(run)s::text IS NULL OR run_id = %(run)s)
             ORDER BY ts_utc
            """,
            params,
        ),
    }


# ----------------------------------------------------------- the compliance panel


@app.get("/compliance/window")
def compliance_window(n: int = Query(3, ge=1, le=12)) -> dict[str, Any]:
    """Where the clock is against NPCI's peak windows, right now.

    Peak IST is 10:00-13:00 and 17:00-21:30, and a presentment inside either is a
    violation regardless of how good the model thinks it looks. The countdown is what
    makes that visible on the panel before it is visible in an audit row.

    ``seconds_to_transition`` is served rather than computed in the browser because a
    client clock is not the clock the agent will schedule against, and a panel that
    disagreed with the guardrail by a few minutes would be wrong exactly at the boundary
    — the only place it matters.
    """
    return _window(datetime.now(UTC), n)


def _window(now: datetime, n: int = 3) -> dict[str, Any]:
    """The window snapshot as a plain function, so the invoice panel can embed it.

    Separate from the handler above on purpose: calling a FastAPI endpoint as a Python
    function hands its parameters the ``Query`` *default objects* rather than values, and
    ``n`` arrives as a ``Query`` instance that blows up on the first comparison. It did.
    """
    return {
        "now_utc": now.isoformat(),
        "now_ist": now.astimezone(IST).isoformat(),
        "is_non_peak": non_peak_window.is_non_peak(now),
        "seconds_to_transition": int(non_peak_window.time_to_next_transition(now).total_seconds()),
        "peak_windows_ist": ["10:00-13:00", "17:00-21:30"],
        "next_legal_slots_ist": [
            slot.astimezone(IST).isoformat() for slot in non_peak_window.next_slots(now, n)
        ],
    }


@app.get("/invoices/{invoice_id}/compliance")
def invoice_compliance(invoice_id: str, at: datetime | None = None) -> dict[str, Any]:
    """What every rule says about this invoice, right now — asked, not reimplemented.

    This endpoint imports `compliance/` and calls the same pure functions the agent
    calls. That is the whole point of it. A compliance panel that recomputed the 1+3 cap
    or the peak-window arithmetic in TypeScript would be a second implementation of the
    law, free to drift from the one that actually gates the money, and the screen a
    reviewer trusts would be the copy rather than the original.

    Both the retry and the nudge are evaluated, because they are governed by different
    rules — NPCI and the window govern presentments, consent governs messages — and the
    panel's job is to show that an invoice can be simultaneously un-retryable and
    contactable, which is the case the whole policy turns on.

    Nothing here writes, schedules, or reserves anything: it is the guardrail answering a
    hypothetical at a moment, which is exactly what a panel should be showing.

    **``at`` is the moment, and it is the whole reason this endpoint is interesting.**
    The rules are pure functions of facts and a clock, so supplying the clock is how a
    reviewer asks "what would the guardrail have said while this invoice was live"
    without a second implementation to trust. Until 3 Sep the parameter existed and did
    not work: a naive timestamp reached ``consent_gate``, which correctly refuses to
    guess a timezone, and the panel returned a 500 instead of an answer. It is normalised
    here rather than made mandatory, because a hand-typed URL will not carry an offset
    and ``+`` does not survive a query string unescaped. UTC is the only defensible
    reading of a bare timestamp in a system whose every stored column is UTC.
    """
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=UTC)

    facts = _row(
        """
        SELECT w.*, c.consent_updated_at
          FROM exception_worklist w
          JOIN customers c ON c.customer_id = w.customer_id
         WHERE w.invoice_id = %(inv)s
        """,
        {"inv": invoice_id},
    )
    if facts is None:
        raise HTTPException(status_code=404, detail=f"no invoice {invoice_id!r}")

    now = at or datetime.now(UTC)
    attempts_used = int(facts["attempts_used"])

    panel: dict[str, Any] = {
        "invoice_id": invoice_id,
        "evaluated_at": now.isoformat(),
        "amount_paise": facts["amount_paise"],
        "npci": {
            "attempts_used": attempts_used,
            "attempts_remaining": npci_retry_cap.attempts_remaining(attempts_used),
            "cap": 4,
            **_result(npci_retry_cap.check(attempts_used)),
        },
        "afa": {
            "ceiling_paise": afa_threshold.ceiling_for(facts["mcc_category"]),
            "mcc_category": facts["mcc_category"],
            **_result(afa_threshold.check(facts["amount_paise"], facts["mcc_category"])),
        },
        "consent": {
            "status": facts["consent_status"],
            **_result(
                consent_gate.check_nudge(
                    consent_status=facts["consent_status"],
                    consent_updated_at=facts["consent_updated_at"],
                    # The debit presented against the customer *is* the transaction that
                    # opens the messaging window — the same reading `ml/policy.py` takes.
                    # Using the last successful charge would silence precisely the
                    # customer whose payments keep failing.
                    last_transaction_at=facts["charge_at"],
                    now=now,
                )
            ),
        },
        "pre_debit_notice": {
            "notice_sent_at": _iso(facts["notice_sent_at"]),
            "charge_at": _iso(facts["charge_at"]),
            **_result(
                pre_debit_notice.check(
                    notice_sent_at=facts["notice_sent_at"],
                    charge_at=facts["charge_at"],
                    attempt_number=attempts_used + 1,
                )
            ),
        },
        "window": _window(now),
        "root_cause": facts["latest_root_cause"],
    }

    # The composed verdicts. A retry needs a root cause, because retryability is decided
    # from Razorpay's own error object and never guessed — an invoice with no failed
    # attempt on record has nothing to classify, and saying so is more useful than
    # defaulting it to retryable and being wrong in the permissive direction.
    if facts["latest_root_cause"] is None:
        panel["retry"] = {
            "verdict": None,
            "detail": "no failed attempt on record, so there is no root cause to classify",
        }
    else:
        panel["retry"] = _verdict(facts, ActionKind.RETRY, attempts_used, now)
    panel["nudge"] = _verdict(facts, ActionKind.NUDGE, attempts_used, now)
    return panel


def _result(result: RuleResult) -> dict[str, Any]:
    """One rule's answer, flattened for the panel. ``detail`` is the string that goes on
    the chip — written for a person reading an audit trail, and served verbatim so the
    screen and the trail say the same words."""
    return {
        "verdict": str(result.verdict),
        "detail": result.detail,
        "stop_reason": result.stop_reason,
    }


def _verdict(
    facts: dict[str, Any], kind: ActionKind, attempts_used: int, now: datetime
) -> dict[str, Any]:
    """The full guardrail, composed, for one proposed action at ``now``."""
    request = ActionRequest(
        kind=kind,
        execute_at=now,
        amount_paise=facts["amount_paise"],
        mcc_category=facts["mcc_category"],
        attempts_used=attempts_used,
        root_cause=RootCause(facts["latest_root_cause"] or RootCause.TD),
        charge_at=facts["charge_at"],
        notice_sent_at=facts["notice_sent_at"],
        consent_status=facts["consent_status"],
        consent_updated_at=facts["consent_updated_at"],
        last_transaction_at=facts["charge_at"],
    )
    return guardrail.evaluate(request, now=now).to_dict()


# ------------------------------------------------------------------- evaluation


@app.get("/evaluation")
def evaluation(run_id: str | None = None) -> dict[str, Any]:
    """The four-arm result, read from the same tables ``docs/EVALUATION.md`` is generated
    from — so the page and the committed report cannot disagree without one of them being
    stale, and ``python -m eval.report --check`` catches that."""
    latest = _row("SELECT run_id FROM eval_runs ORDER BY created_at DESC LIMIT 1") or {}
    run = run_id or latest.get("run_id")
    if run is None:
        raise HTTPException(status_code=404, detail="no evaluation run persisted")

    return {
        "run": _row("SELECT * FROM eval_runs WHERE run_id = %(run)s", {"run": run}),
        "arms": _rows(
            "SELECT * FROM eval_arm_results WHERE run_id = %(run)s ORDER BY arm", {"run": run}
        ),
        "violations": _rows(
            """
            SELECT * FROM eval_arm_violations
             WHERE run_id = %(run)s ORDER BY arm, violations DESC
            """,
            {"run": run},
        ),
        "intervals": _rows(
            """
            SELECT * FROM eval_intervals
             WHERE run_id = %(run)s ORDER BY statistic, comparison, arm
            """,
            {"run": run},
        ),
    }


@app.get("/config")
def config() -> dict[str, Any]:
    """What this deployment is pointed at. Never the credentials themselves.

    The execution mode is on the overview page for the same reason it is on every audit
    row: which lane ran is never left to be inferred. ``--live`` spent its entire
    existence silently running the simulator (``WHAT_BROKE.md``, 2 Sep) precisely because
    that fact lived in one place and nothing cross-checked it.

    ``model_version`` is read from the last decision actually written rather than from a
    constant in this process. A constant would report what this deployment *would* use;
    the column reports what scored the rows the dashboard is about to display, and when
    those two differ the second one is the answer to the question being asked.
    """
    settings = get_settings()
    scored_by = _row("SELECT model_version FROM decisions ORDER BY decided_at DESC LIMIT 1")
    return {
        "execution_mode": settings.execution_mode,
        "model_version": (scored_by or {}).get("model_version"),
        "agent_model": settings.agent_model,
        "max_turns_per_item": settings.max_turns_per_item,
        # The key *id*, which is public and identifies the test account. The secret is
        # never read here and has no endpoint that could return it.
        "razorpay_key_id": settings.razorpay_key_id,
        "live_call_budget": settings.live_call_budget,
    }

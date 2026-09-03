"""The audit trail, and the three ways it broke before it worked.

Each of the first three tests here is a defect that reached a live batch run. They cost
an evening between them, and all three had the same shape: the audit path failed
*silently*, so the only symptom was a table with fewer rows in it than the run had
actions. That is the failure mode this file exists to make impossible to reintroduce.

The database tests are marked ``db`` and write under a test ``run_id``. ``audit_log`` is
genuinely append-only, so those rows are permanent — which is why there are only as many
of them as the invariants actually require.
"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager

import pytest

from agent.hooks import (
    AUDITED,
    TERMINAL_ACTIONS,
    AuditWriter,
    _binding_refusal,
    _is_tool_refusal,
    _jsonb,
    _payload,
    audit_matcher,
    make_audit_hook,
)
from agent.tools import GUARDRAIL_TOOL, MONEY_TOOL

NOTIFY_TOOL = "mcp__winback__simulated_notify"
ASSESS_TOOL = "mcp__winback__assess_recoverability"


# ------------------------------------------------------- defect 1: the response shape


def test_a_tool_response_arrives_as_a_bare_list():
    """**The defect.** ``tool_response`` comes from the SDK as a bare list of content
    blocks, not the ``{"content": [...]}`` envelope the tool returned. The first version
    of ``_payload`` understood only the envelope, so every payload fell through to
    ``{"unparsed": ...}``, arrived without an ``invoice_id``, and was discarded without a
    word. The batch reported zero audit rows and no error."""
    blocks = [{"type": "text", "text": json.dumps({"invoice_id": "inv_0001_01"})}]
    assert _payload(blocks) == {"invoice_id": "inv_0001_01"}


def test_a_tool_response_still_parses_in_the_envelope_shape():
    """What the tool itself returns, which is what the unit tests feed in."""
    payload = {"invoice_id": "inv_0001_01", "action": "retry"}
    envelope = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    assert _payload(envelope) == payload


def test_unparseable_text_is_kept_rather_than_dropped():
    assert _payload([{"type": "text", "text": "not json"}]) == {"unparsed": "not json"}


def test_an_empty_response_does_not_raise():
    for response in ([], None, "", {}):
        assert isinstance(_payload(response), dict)


# ------------------------------------------- defect 4: a declined debit is not an error


def test_a_declined_debit_is_a_result_and_not_a_tool_error():
    """**The defect.** ``ExecutionResult.error`` carries Razorpay's error fields verbatim
    when a presentment *failed* — an ``insufficient_funds`` retry is the most ordinary
    thing this system observes. The hook branched on ``payload.get("error")`` alone, so
    eight declined debits in ``batch_v1`` were written as ``trigger='tool_error',
    outcome='blocked'``: the compliance signal, the column the violations chart counts.
    Eight customers with no money were recorded as eight actions the guardrail stopped.
    """
    declined = {
        "invoice_id": "inv_0631_03",
        "action": "retry",
        "outcome": "failed",
        "recovered_paise": 0,
        "error": {
            "error_code": "BAD_REQUEST_ERROR",
            "error_source": "customer",
            "error_reason": "insufficient_funds",
            "root_cause_class": "BD_transient",
        },
    }
    assert _is_tool_refusal(declined) is False


def test_an_outage_is_a_tool_error_and_not_a_declined_debit():
    """The inverse, which is the conflation ``AdapterError`` was written to prevent: a
    network timeout must never look like a customer with no money. It never reached the
    adapter, so it has no ``outcome`` — that absence is the discriminator."""
    assert _is_tool_refusal({"invoice_id": "inv_0001_01", "error": "adapter_error"}) is True


def test_a_spent_approval_is_a_tool_refusal():
    """The gate's own refusal path, which is the one row of this kind that genuinely
    belongs under ``tool_error``: the tool declined to act because no unspent approval
    existed, and nothing was presented."""
    refusal = {"invoice_id": "inv_0001_01", "error": "no_guardrail_approval", "detail": "..."}
    assert _is_tool_refusal(refusal) is True


def test_a_clean_execution_is_neither():
    """A successful retry has no ``error`` at all, and must take the ordinary path."""
    assert _is_tool_refusal({"invoice_id": "inv_0001_01", "outcome": "recovered"}) is False
    assert _is_tool_refusal({"invoice_id": "inv_0001_01", "outcome": "failed", "error": None}) is (
        False
    )


# ------------------------------- defect 5: the trail recorded only what the batch did


def _candidate(kind, stop_reason: str | None):
    """One scored candidate, approved when it carries no stop reason."""
    from compliance.guardrail import GuardrailDecision
    from compliance.result import Verdict
    from ml.policy import ScoredCandidate

    return ScoredCandidate(
        kind=kind,
        execute_at=None,
        nudge_first=False,
        p_success=None,
        expected_value_paise=float("-inf") if stop_reason else 100.0,
        decision=GuardrailDecision(
            verdict=Verdict.DENY if stop_reason else Verdict.APPROVE,
            authorizing_rule="rule",
            stop_reason=stop_reason,
            results=(),
        ),
    )


class _Plan:
    def __init__(self, *candidates):
        self.candidates = candidates


def test_the_reason_an_invoice_was_written_off_is_the_refused_presentment():
    """A ``dnd_registered`` nudge and a ``bd_hard_not_retryable`` retry are both true, and
    only one answers "why was no attempt made". The presentment is searched first."""
    from compliance.guardrail import ActionKind

    plan = _Plan(
        _candidate(ActionKind.NUDGE, "dnd_registered"),
        _candidate(ActionKind.RETRY, "bd_hard_not_retryable"),
    )
    assert _binding_refusal(plan) == "bd_hard_not_retryable"


def test_a_write_off_the_guardrail_never_refused_names_no_rule():
    """An economic stop, not a compliance one. The guardrail permitted a presentment and
    the policy declined it on value — labelling that with a rule would put a compliance
    block in the table where a judgement call belongs."""
    from compliance.guardrail import ActionKind

    assert _binding_refusal(_Plan(_candidate(ActionKind.RETRY, None))) is None
    assert _binding_refusal(_Plan()) is None
    assert _binding_refusal(None) is None


def test_a_written_off_invoice_reaches_the_audit_trail(bench, monkeypatch):
    """**The defect.** ``audit_log`` was written only by the tools that *do* something, so
    a write-off — which calls no tool, because there is nothing to call — produced a
    ``decisions`` row and no audit row at all. ``batch_v1`` finished with 184 decisions
    and 156 audit rows: it had concluded every invoice in the cohort and the compliance
    artifact was silent about 34 of them, which are exactly the ones where the answer to
    "why was this customer not charged" is a rule.
    """
    from compliance.guardrail import ActionKind

    seen: list[tuple[dict, dict]] = []
    monkeypatch.setattr(
        AuditWriter, "record_action", lambda self, payload, **kw: seen.append((payload, kw))
    )
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    invoice_id = sorted(bench.cases)[0]
    bench.plans[invoice_id] = _Plan(_candidate(ActionKind.RETRY, "bd_hard_not_retryable"))

    writer.record_conclusion(
        {"invoice_id": invoice_id, "action": "write_off", "authorizing_rule": "no_money_moves: ..."}
    )

    payload, kwargs = seen[0]
    assert payload["action"] == "write_off"
    assert payload["customer_hash"] == bench.cases[invoice_id].customer.customer_hash
    assert kwargs == {
        "trigger": "batch_scan",
        "outcome": "blocked",
        "stop_reason": "bd_hard_not_retryable",
    }


def test_an_escalation_is_recorded_as_escalated_and_not_as_blocked(bench, monkeypatch):
    """The two terminal actions mean different things to whoever reads the table: one
    invoice is finished, the other is on a person's desk."""
    seen: list[tuple[dict, dict]] = []
    monkeypatch.setattr(
        AuditWriter, "record_action", lambda self, payload, **kw: seen.append((payload, kw))
    )
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    writer.record_conclusion({"invoice_id": sorted(bench.cases)[0], "action": "escalate"})

    assert seen[0][1]["outcome"] == "escalated"


async def test_a_refused_terminal_action_is_not_recorded_as_a_conclusion(bench, monkeypatch):
    """``authorised`` gates the write. A guardrail call that came back DENY is a decision
    row and nothing else — recording it as a conclusion would put a write-off in the audit
    trail that the system was never permitted to make."""
    seen: list[dict] = []
    monkeypatch.setattr(AuditWriter, "record_decision", lambda self, payload: None)
    monkeypatch.setattr(
        AuditWriter, "record_conclusion", lambda self, payload: seen.append(payload)
    )
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    hook = make_audit_hook(writer)
    invoice_id = sorted(bench.cases)[0]

    for authorised in (False, True):
        await hook(
            {
                "tool_name": GUARDRAIL_TOOL,
                "tool_response": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "invoice_id": invoice_id,
                                "action": "write_off",
                                "authorised": authorised,
                            }
                        ),
                    }
                ],
            },
            None,
            None,
        )

    assert [row["authorised"] for row in seen] == [True]


def test_only_the_actions_that_call_no_tool_are_terminal():
    """A retry and a nudge each have a tool behind them, and ``PostToolUse`` records those.
    Listing either here would write the invoice into ``audit_log`` twice."""
    assert TERMINAL_ACTIONS == {"write_off", "escalate"}


# --------------------------- defect 7: an invoice that concluded in prose and nowhere else


@contextmanager
def _no_database():
    """Stands in for ``agent_connection`` so the INSERT is exercised up to, and not
    including, the wire. What is under test here is which rows the writer decides to
    write, which is decided entirely before the connection is opened."""

    class _Conn:
        def execute(self, *_a, **_k):
            return None

    yield _Conn()


def test_an_invoice_that_left_no_row_gets_one(bench, monkeypatch):
    """**The defect.** ``live_v2`` worked twelve invoices and wrote eleven audit rows.
    ``inv_0007_01`` had a ``decisions`` row carrying a full APPROVE and its authorizing
    rule, and its closing sentence was "The guardrail approved the retry" — the agent got
    the approval, said so, and ran out of turns before calling ``execute_recovery``. The
    trail then said exactly as much about that invoice as about one never opened.
    """
    seen: list[tuple[dict, dict]] = []
    monkeypatch.setattr(
        AuditWriter, "record_action", lambda self, payload, **kw: seen.append((payload, kw))
    )
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    invoice_id = sorted(bench.cases)[0]

    writer.record_silence(invoice_id)

    payload, kwargs = seen[0]
    assert payload["invoice_id"] == invoice_id
    assert payload["action"] is None  # nothing was done; naming an action would invent one
    assert kwargs["outcome"] == "blocked"
    assert kwargs["stop_reason"] == "no_conclusion_reached"


def test_an_approval_the_agent_never_spent_is_named_as_such(bench, monkeypatch):
    """The sharper case, and the reason the two are not flattened into one reason string.
    An unspent approval means the guardrail *did* authorise the presentment: the invoice
    was legally chargeable and the attempt was lost to the agent's turn budget, not to a
    rule. Filing that under a compliance stop would read as a law protecting the merchant
    from a recovery it was entitled to make."""
    from agent.tools import approval_key

    seen: list[tuple[dict, dict]] = []
    monkeypatch.setattr(
        AuditWriter, "record_action", lambda self, payload, **kw: seen.append((payload, kw))
    )
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    invoice_id = sorted(bench.cases)[0]
    bench.approvals[approval_key(invoice_id, "retry", "2026-07-01T13:30:00")] = object()

    writer.record_silence(invoice_id)

    assert seen[0][1]["stop_reason"] == "approval_granted_not_spent"


def test_every_write_marks_its_invoice_as_covered(bench, monkeypatch):
    """``covered`` is what the batch consults before writing a silence row, and it is
    filled by ``record_action`` itself rather than by each caller — so no path that writes
    a row can forget to mark it, and a second row can never be written for an invoice the
    hooks already recorded."""
    monkeypatch.setattr("agent.hooks.agent_connection", _no_database)
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    assert writer.covered == set()

    invoice_id = sorted(bench.cases)[0]
    writer.record_action({"invoice_id": invoice_id, "action": "retry"}, trigger="tool_result")
    assert writer.covered == {invoice_id}


def test_a_degradation_is_deferred_and_not_escalated(bench, monkeypatch):
    """The Day-8 drill's audit row, and the one outcome value it must not use.

    ``recovery_funnel`` counts ``escalated`` straight into the number the dashboard's
    compliance strip renders as *Escalated*, so a Docker container dying would have
    appeared on screen as the guardrail routing a payment to a human — an infrastructure
    event wearing a compliance event's clothes, on the one panel whose whole job is to be
    trusted. ``deferred`` reaches the funnel through ``stopped`` instead, via the
    ``stop_reason``, which is where an operator should find it."""
    seen: list[tuple[dict, dict]] = []
    monkeypatch.setattr(
        AuditWriter, "record_action", lambda self, payload, **kw: seen.append((payload, kw))
    )
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    invoice_id = sorted(bench.cases)[0]

    writer.record_degradation(invoice_id, "local failed — ProcessError", "remote")

    payload, kwargs = seen[0]
    assert payload["invoice_id"] == invoice_id
    assert kwargs["trigger"] == "mcp_degraded"
    assert kwargs["outcome"] == "deferred"
    assert kwargs["stop_reason"] == "mcp_degraded_to_remote"


def test_a_degradation_does_not_cover_the_invoice_it_names(bench, monkeypatch):
    """``covers=False``. The row is *about* the invoice without concluding it, and the
    whole point of the demotion is that the invoice is retried on the new lane. Marking
    it covered would suppress ``record_silence`` on that retry — reopening the exact hole
    ``record_silence`` exists to close, with the machinery meant to survive a failure."""
    monkeypatch.setattr("agent.hooks.agent_connection", _no_database)
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    invoice_id = sorted(bench.cases)[0]

    writer.record_degradation(invoice_id, "local failed — ProcessError", "remote")

    assert writer.covered == set()


def test_an_unknown_invoice_is_not_invented(bench):
    """``record_silence`` is called from the loop, not from a hook, so it takes a bare id.
    One that is not in the cohort has no amount and no customer to redact, and writing a
    row about it would put a subject in the audit trail that the batch never worked."""
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    writer.record_silence("inv_9999_99")
    assert writer.rows_written == 0


# ------------------------------------------------------- defect 2: -Infinity in jsonb


def test_a_non_finite_number_is_refused_at_the_database_boundary():
    """**The defect.** The policy uses ``-inf`` for a candidate the guardrail ruled out.
    ``json.dumps`` emits the bare token ``-Infinity``, which is not RFC 8259, and
    PostgreSQL's ``jsonb`` rejects it — so one ``decisions`` row was never written and
    its ``audit_log`` entry pointed at a decision id that did not exist. The exception
    was swallowed by the SDK's hook runner and appeared only as an unexplained line of
    stderr. Refusing here means it is reported against the line that produced it."""
    with pytest.raises(ValueError):
        _jsonb({"expected_value_paise": float("-inf")})
    with pytest.raises(ValueError):
        _jsonb({"p": float("nan")})


def test_a_ruled_out_candidate_serialises_to_valid_json():
    """The fix, at the source. ``None`` plus ``ruled_out``, with the ``stop_reason`` on
    the same row still saying why — nothing a reviewer reads is lost, and the sentinel
    stays a float inside the policy where the argmax needs it to compare."""
    from compliance.guardrail import ActionKind, GuardrailDecision
    from compliance.result import Verdict
    from ml.policy import ScoredCandidate

    candidate = ScoredCandidate(
        kind=ActionKind.RETRY,
        execute_at=None,
        nudge_first=False,
        p_success=None,
        expected_value_paise=float("-inf"),
        decision=GuardrailDecision(
            verdict=Verdict.DENY,
            authorizing_rule="npci_1_plus_3",
            stop_reason="npci_1_plus_3_cap_exhausted",
            results=(),
        ),
    )
    assert not math.isfinite(candidate.expected_value_paise)

    row = candidate.to_dict()
    assert row["expected_value_paise"] is None
    assert row["ruled_out"] is True
    assert row["stop_reason"] == "npci_1_plus_3_cap_exhausted"
    assert json.loads(_jsonb(row))["ruled_out"] is True  # would have raised before the fix


async def test_every_candidate_in_a_real_plan_is_jsonb_safe(tools, bench, invoice_id):
    """The property, not the example: whatever the policy scores, the audit trail can
    store. Run against a real plan so a new ``-inf`` branch added later fails here."""
    from datetime import timedelta

    from eval.counterfactual import DECISION_LAG_HOURS

    now = bench.cases[invoice_id].first_charge.attempted_at + timedelta(hours=DECISION_LAG_HOURS)
    await tools["assess_recoverability"].handler({"invoice_id": invoice_id, "now": now.isoformat()})

    plan = bench.plans[invoice_id]
    blob = _jsonb([c.to_dict() for c in plan.candidates])
    reloaded = json.loads(blob)
    assert len(reloaded) == len(plan.candidates)
    for candidate in reloaded:
        assert candidate["ruled_out"] is (candidate["expected_value_paise"] is None)


# ------------------------------------------------- defect 3: a swallowed write failure


async def test_a_failed_audit_write_is_counted_rather_than_lost(bench):
    """**The defect.** An exception raised inside a ``PostToolUse`` hook is swallowed by
    the SDK, so the run carried on and the report said nothing. A batch that finished
    with a hole in its audit trail did not really finish, and now it says so."""

    class _Exploding(AuditWriter):
        def record_action(self, *args, **kwargs):
            raise RuntimeError("boom")

    writer = _Exploding(bench=bench, run_id="agent_test_nowrite")
    hook = make_audit_hook(writer)
    await hook(
        {
            "tool_name": MONEY_TOOL,
            "tool_response": [{"type": "text", "text": json.dumps({"invoice_id": "inv_0001_01"})}],
        },
        None,
        None,
    )

    assert len(writer.write_failures) == 1
    assert "RuntimeError" in writer.write_failures[0]
    assert writer.rows_written == 0


# ------------------------------------------------------------------ what is audited


def test_assessment_is_not_audited():
    """Scoring an invoice observes nothing about the customer and changes nothing.
    Logging it would bury the rows that matter under rows that do not."""
    assert ASSESS_TOOL not in AUDITED
    assert AUDITED == {GUARDRAIL_TOOL, MONEY_TOOL, NOTIFY_TOOL}


async def test_an_unaudited_tool_writes_nothing(bench):
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    await make_audit_hook(writer)({"tool_name": ASSESS_TOOL, "tool_response": []}, None, None)
    assert writer.rows_written == 0
    assert writer.write_failures == []


def test_the_hook_matcher_matches_every_tool(bench):
    """``HookMatcher(matcher=None, ...)`` — verified against the installed SDK, which is
    the open item the plan flagged for this day. Filtering happens in the callback so the
    audited list sits next to the allowed list, where a reviewer can compare them."""
    matcher = audit_matcher(AuditWriter(bench=bench, run_id="agent_test_noop"))
    assert matcher.matcher is None
    assert len(matcher.hooks) == 1


def test_the_hook_returns_nothing_that_steers_the_agent(bench):
    """A hook that could tell the agent what to do next would be a second, undocumented
    decision-maker sitting behind the guardrail."""
    import asyncio

    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    out = asyncio.run(
        make_audit_hook(writer)({"tool_name": ASSESS_TOOL, "tool_response": []}, None, None)
    )
    assert out == {}


# ------------------------------------------------------------------ denial recording


def test_a_blocked_off_list_tool_is_not_recorded_as_a_nudge(bench, monkeypatch):
    """``action_taken`` names the tool that was refused, not a guess at what it would
    have done. Writing "nudge" against a blocked ``payment_link_notify`` would put a
    message in the audit trail that was never even proposed."""
    seen: list[dict] = []
    monkeypatch.setattr(
        AuditWriter, "record_action", lambda self, payload, **kw: seen.append(payload)
    )
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    invoice_id = sorted(bench.cases)[0]

    writer.record_denial(MONEY_TOOL, {"invoice_id": invoice_id}, "no_guardrail_approval: x")
    writer.record_denial(NOTIFY_TOOL, {"invoice_id": invoice_id}, "no_guardrail_approval: x")
    writer.record_denial(
        "mcp__razorpay__payment_link_notify", {"invoice_id": invoice_id}, "tool_not_permitted: x"
    )

    assert [row["action"] for row in seen] == [
        "retry",
        "nudge",
        "blocked:mcp__razorpay__payment_link_notify",
    ]


def test_a_denial_without_an_invoice_id_writes_nothing(bench, monkeypatch):
    seen: list = []
    monkeypatch.setattr(AuditWriter, "record_action", lambda self, *a, **k: seen.append(a))
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")

    writer.record_denial(MONEY_TOOL, {}, "reason")
    writer.record_denial(MONEY_TOOL, {"invoice_id": 42}, "reason")
    assert seen == []


def test_a_denial_redacts_the_customer(bench, monkeypatch):
    """``observed_data`` carries ``sha256(customer_id)[:12]`` and never a customer id,
    an email or a phone number — and the redaction happens here, at write time, so the
    raw value is never in the table at all."""
    seen: list[dict] = []
    monkeypatch.setattr(
        AuditWriter, "record_action", lambda self, payload, **kw: seen.append(payload)
    )
    writer = AuditWriter(bench=bench, run_id="agent_test_noop")
    invoice_id = sorted(bench.cases)[0]
    writer.record_denial(MONEY_TOOL, {"invoice_id": invoice_id}, "reason")

    payload = seen[0]
    assert payload["customer_hash"] == bench.cases[invoice_id].customer.customer_hash
    assert len(payload["customer_hash"]) == 12
    assert "customer_id" not in payload
    assert "email" not in payload

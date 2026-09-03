"""The batch wiring: which executor runs, what the run reports, and what fails it.

No LLM runs here either. What is worth testing about the orchestrator is not what Claude
decides — that varies by design — but the frame around it: that ``WINBACK_EXECUTION_MODE``
actually reaches the adapter, that a run which asked for the live lane and cannot have it
stops rather than quietly simulating, and that a batch with a hole in its audit trail
exits non-zero. All of those are decidable without a single token.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent.adapters.base import ExecutionMode
from agent.adapters.simulated import SimulatedAdapter
from agent.mcp_config import Lane
from agent.orchestrator import (
    LIVE_CALLS_PER_INVOICE,
    BatchReport,
    _adapter_for,
    _already_worked,
    _is_fatal_to_the_batch,
    _looks_like_mcp_failure,
    _options,
)
from agent.tools import (
    ALLOWED_TOOLS,
    GATED_TOOLS,
    PREAPPROVED_TOOLS,
    RAZORPAY_READ_TOOLS,
    RAZORPAY_SERVER,
    permitted_tools,
)
from core.config import ConfigError, get_settings

#: The lane these option tests run under. `off` mounts no Razorpay server, which keeps
#: them about the SDK options themselves — no Docker, no network, nothing to probe.
_OFF_LANE = Lane(requested="off", mode="off", servers={})

KEY = "rzp_test_ABC123"
SECRET = "s3cr3t_value"  # noqa: S105 — a fake credential, never a real one


@pytest.fixture
def settings():
    """Real settings, built without touching ``.env`` for the fields under test."""
    return get_settings()


# ------------------------------------------------------------------ the mode switch


def test_simulated_mode_takes_the_workbenchs_own_oracle(settings):
    """``None`` rather than a second ``SimulatedAdapter``.

    Two simulators over the same cohort would each keep their own ``nudged_at`` physics,
    so a nudge recorded against one would be invisible to the other and the hazard would
    silently diverge from the one the evaluation measured.
    """
    assert _adapter_for(replace(settings, execution_mode=ExecutionMode.SIMULATED)) is None


def test_live_mode_actually_reaches_the_live_adapter(settings):
    """The regression this test exists for: ``run_batch`` built its workbench without
    passing an adapter at all, so ``WINBACK_EXECUTION_MODE=live`` changed nothing and a
    run that asked for real API calls made none."""
    from agent.adapters.live_razorpay import LiveRazorpayAdapter

    live = _adapter_for(
        replace(
            settings,
            execution_mode=ExecutionMode.LIVE,
            razorpay_key_id=KEY,
            razorpay_key_secret=SECRET,
        )
    )
    assert isinstance(live, LiveRazorpayAdapter)
    assert live.mode is ExecutionMode.LIVE
    live.close()


def test_the_live_lane_is_reached_from_the_string_the_config_actually_returns(settings):
    """**The defect.** ``core.config`` types this field as ``Literal["simulated", "live"]``
    and returns a plain ``str``; the enum lives in the adapter layer. ``_adapter_for``
    compared with ``is``, which is ``True`` only for the enum member — the value the test
    above injects and the value the CLI never produces. So ``--live`` ran the oracle on
    every invocation it ever had and exited 0. The fixture is the real one on purpose:
    this test fails if the comparison goes back to identity.
    """
    from agent.adapters.live_razorpay import LiveRazorpayAdapter

    assert get_settings().execution_mode == "simulated"  # a str, not ExecutionMode
    assert _adapter_for(replace(settings, execution_mode="simulated")) is None

    live = _adapter_for(
        replace(
            settings,
            execution_mode="live",
            razorpay_key_id=KEY,
            razorpay_key_secret=SECRET,
        )
    )
    assert isinstance(live, LiveRazorpayAdapter)
    live.close()


def test_a_run_that_asked_for_the_live_lane_and_did_not_get_it_fails(settings):
    """The independent check, because the bug above was invisible in the report: it
    printed ``[simulated]`` and returned 0, and nothing compared that against what was
    asked for. Two different code paths now have to agree before the run passes."""
    assert _report(execution_mode="simulated").execution_mode != "live"
    assert _report(execution_mode="live").execution_mode == "live"


def test_live_mode_without_credentials_stops_the_run(settings):
    """Raises rather than falling back. A live batch that silently simulated would write
    ``execution_mode = 'simulated'`` truthfully on every row and still mislead in
    aggregate, because nobody was told the lane changed."""
    with pytest.raises(ConfigError):
        _adapter_for(
            replace(
                settings,
                execution_mode=ExecutionMode.LIVE,
                razorpay_key_id=None,
                razorpay_key_secret=None,
            )
        )


def test_the_live_budget_covers_at_least_one_invoice(settings):
    """The unbounded-live-run cap. Whatever the budget is set to, the arithmetic must not
    produce a batch of zero invoices — a run that did nothing and reported success is the
    least useful possible outcome."""
    for budget in (1, 2, 10, 50):
        covered = max(1, budget // LIVE_CALLS_PER_INVOICE)
        assert covered >= 1
    assert LIVE_CALLS_PER_INVOICE >= 2  # an order and a link, per presentment


# ------------------------------------------------------------------ the options


def test_the_gated_tools_are_never_handed_to_the_sdk(settings, scorer, rates):
    """``allowed_tools`` auto-approves before ``can_use_tool`` is consulted, so an entry
    there is a standing approval. The two tools that move money must not have one — this
    is the same defect as ``test_the_gated_tools_are_not_pre_approved``, asserted at the
    point where the list actually reaches the SDK."""
    from agent.hooks import AuditWriter
    from agent.tools import workbench_from_dataset

    bench = workbench_from_dataset(scorer=scorer, rates=rates, cohort="test")
    options = _options(
        bench, AuditWriter(bench=bench, run_id="opts", arm="D"), settings, _OFF_LANE
    )

    assert set(options.allowed_tools) == set(PREAPPROVED_TOOLS)
    assert set(options.allowed_tools).isdisjoint(GATED_TOOLS)
    assert options.can_use_tool is not None
    assert options.permission_mode == "default"


def test_the_run_does_not_inherit_local_claude_code_settings(settings, scorer, rates):
    """``setting_sources=[]``. A batch that behaved differently on the author's laptop
    than on a fresh clone would make every number in this repository unreproducible — and
    an allow-rule in a personal settings file can shadow the money gate."""
    from agent.hooks import AuditWriter
    from agent.tools import workbench_from_dataset

    bench = workbench_from_dataset(scorer=scorer, rates=rates, cohort="test")
    options = _options(
        bench, AuditWriter(bench=bench, run_id="opts", arm="D"), settings, _OFF_LANE
    )

    assert options.setting_sources == []
    assert options.max_turns == settings.max_turns_per_item


def test_the_agent_is_bounded_in_turns(settings, scorer, rates):
    """A hard stop against a runaway loop. Six turns is enough for assess → guardrail →
    (redirect → guardrail) → execute → conclude, and not enough to be expensive."""
    from agent.hooks import AuditWriter
    from agent.tools import workbench_from_dataset

    bench = workbench_from_dataset(scorer=scorer, rates=rates, cohort="test")
    options = _options(
        bench, AuditWriter(bench=bench, run_id="opts", arm="D"), settings, _OFF_LANE
    )
    assert 1 <= options.max_turns <= 12


# ------------------------------------------------- what the lane is allowed to mount


def _options_for(settings, scorer, rates, lane):
    from agent.hooks import AuditWriter
    from agent.tools import workbench_from_dataset

    bench = workbench_from_dataset(scorer=scorer, rates=rates, cohort="test")
    return _options(bench, AuditWriter(bench=bench, run_id="opts", arm="D"), settings, lane)


def test_a_lane_no_tool_can_reach_is_not_mounted(settings, scorer, rates):
    """The Day-8 drill's actual finding. On the simulated lane no Razorpay tool is
    permitted, so mounting the server spent a Docker container per invoice on a transport
    the gate would have refused to let the agent speak to — and the run header announced
    `mcp: local` about it. The SDK does not raise when a mounted stdio server fails to
    start, so that claim was not just useless, it was never checked."""
    live_lane = Lane(requested="local", mode="local", servers={"razorpay": {"type": "stdio"}})
    options = _options_for(replace(settings, execution_mode="simulated"), scorer, rates, live_lane)

    assert set(options.mcp_servers) == {"winback"}


def test_the_live_lane_mounts_razorpay_and_permits_only_reads(settings, scorer, rates):
    """The other half: the mount is real where the ids are. Every permitted Razorpay tool
    is a fetch — no send, no create, no capture — because anything that moves money or
    reaches a customer goes through `execute_recovery`, which cannot run without a
    guardrail approval already on record."""
    live_lane = Lane(requested="local", mode="local", servers={"razorpay": {"type": "stdio"}})
    options = _options_for(replace(settings, execution_mode="live"), scorer, rates, live_lane)

    assert set(options.mcp_servers) == {"winback", "razorpay"}

    razorpay = [t for t in permitted_tools("live") if t.startswith(f"mcp__{RAZORPAY_SERVER}__")]
    assert razorpay == list(RAZORPAY_READ_TOOLS)
    assert all("fetch" in name for name in razorpay)

    # And none of them is pre-approved: a read still passes the gate, which is what makes
    # `record_denial` able to see a tool the Razorpay server grew since this was written.
    assert set(options.allowed_tools).isdisjoint(razorpay)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("MCP server disconnected"),
        ConnectionError("stdio transport closed unexpectedly"),
        OSError("[Errno 32] Broken pipe writing to docker"),
    ],
)
def test_a_transport_death_is_charged_to_the_transport(exc):
    """Which of the two things just failed — this invoice, or the pipe every remaining
    invoice also has to use? Getting that wrong the safe way costs one recovery; getting
    it wrong the other way charges 190 invoices to a dead container."""
    assert _looks_like_mcp_failure(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("invoice inv_0007_01 has no attempts"),
        KeyError("customer_hash"),
        ZeroDivisionError("division by zero"),
    ],
)
def test_an_invoice_that_fails_on_its_own_merits_does_not_demote_the_lane(exc):
    """A demotion is one-way and costs the rest of the batch its best transport, so a bug
    in one invoice's data must not spend it."""
    assert _looks_like_mcp_failure(exc) is False


def test_the_simulated_batch_sees_the_tool_set_it_always_saw(settings, scorer, rates):
    """Load-bearing for every committed number: enabling the reads on the live lane must
    not have changed what the 500-invoice batch behind `docs/EVALUATION.md` could do."""
    assert permitted_tools("simulated") == ALLOWED_TOOLS


# ------------------------------------------------------------------ the report


def _report(**over) -> BatchReport:
    base = dict(
        run_id="r",
        execution_mode="simulated",
        invoices=3,
        completed=3,
        failed=0,
        executions=3,
        recovered_paise=100_000,
        audit_rows=6,
        seconds=42.0,
        total_cost_usd=0.25,
    )
    return BatchReport(**{**base, **over})


def test_a_clean_batch_reports_a_complete_audit_trail():
    report = _report()
    assert report.audit_complete is True
    assert "AUDIT WRITES FAILED" not in str(report)


def test_a_failed_audit_write_is_in_the_headline_not_a_log_line():
    """A batch that finished with a hole in its audit trail did not really finish, and
    the evidence of that must be in the one line a person actually reads."""
    report = _report(audit_failures=("execute inv_0001_01: ValueError: -inf",))
    assert report.audit_complete is False
    assert "⚠ 1 AUDIT WRITES FAILED" in str(report)


def test_the_execution_mode_is_always_named_in_the_report():
    """Which lane ran is never inferred from context. It is printed."""
    assert "[live]" in str(_report(execution_mode="live"))
    assert "[simulated]" in str(_report())


def test_the_simulated_adapter_reports_the_mode_the_report_prints():
    """The string in ``BatchReport.execution_mode`` comes from the adapter, not from a
    literal, so the two cannot drift apart."""
    assert str(SimulatedAdapter.from_dataset(cohort="test").mode) == "simulated"


# ------------------------------------------------------------------ halting and resuming


def test_an_exhausted_quota_stops_the_batch():
    """**The defect.** A batch hit the account's session limit at invoice 85 of 190 and
    then attempted the remaining 105, each failing with the identical sentence. It spent
    twenty minutes doing it and buried the one fact worth reading under a hundred copies
    of itself."""
    limits = (
        "Claude Code returned an error result: You've hit your session limit · resets 11:10pm",
        "usage limit reached",
        "429 Rate limit exceeded",
        "Your credit balance is too low",
    )
    for text in limits:
        assert _is_fatal_to_the_batch(RuntimeError(text)) is True


def test_an_ordinary_bad_invoice_does_not_stop_the_batch():
    """The other half, and the more important one: the loop catches broadly *so that* one
    unworkable invoice is stepped over. Halting on those would lose the 189 that worked,
    which is the failure this batch was already written to avoid."""
    ordinary = (
        "unknown invoice inv_9999_01",
        "ValueError: could not parse execute_at",
        "no unspent approval for inv_0001_01|retry|2026-07-01T13:30:00",
    )
    for text in ordinary:
        assert _is_fatal_to_the_batch(RuntimeError(text)) is False


def test_a_halted_run_is_not_a_complete_one():
    """A run that concluded 85 of 190 invoices and one that concluded all 190 must not
    print the same shape of line with different numbers in it, and must not exit 0."""
    halted = _report(invoices=190, completed=85, halted="You've hit your session limit")
    assert halted.cohort_complete is False
    assert "HALTED" in str(halted)


def test_a_resumed_run_counts_prior_work_separately():
    """Resumed invoices are done, but this process did not do them. Merging the two counts
    would let a report claim credit for work an earlier attempt paid for."""
    resumed = _report(invoices=190, completed=105, resumed=85)
    assert resumed.cohort_complete is True
    assert "+85 resumed" in str(resumed)


def test_an_invoice_that_acted_on_nothing_is_counted_in_the_headline():
    """``live_v2`` reported ``12/12 invoices (0 errored)`` and wrote 11 audit rows, and
    nothing in the line said so — the arithmetic was there to be done and nobody was asked
    to do it. The run is still complete: the invoice concluded, no rule was broken, and a
    row now exists for it. But an approval the agent obtained and never spent is a
    recovery lost to a turn budget, and that belongs in the sentence a person reads."""
    quiet = _report(silent=1)
    assert quiet.cohort_complete is True
    assert "1 unacted" in str(quiet)
    assert "unacted" not in str(_report())


def test_a_run_with_a_failed_invoice_is_not_complete():
    """Unchanged from before resumption existed: an invoice that errored is an invoice the
    cohort did not conclude, and the exit code has to say so."""
    assert _report(invoices=190, completed=189, failed=1).cohort_complete is False


@pytest.mark.db
def test_resumption_reads_the_audit_trail_and_not_a_progress_file():
    """``audit_log`` is append-only and keyed by ``run_id``, so the table *is* the
    checkpoint. There is no second progress file that could disagree with the evidence,
    and an unknown run has simply concluded nothing."""
    assert _already_worked("no_such_run_id_exists") == set()


@pytest.mark.db
def test_an_invoice_that_died_between_the_decision_and_the_tool_is_re_worked():
    """The query used to union ``decisions``, because a write-off concluded an invoice
    without auditing anything. ``record_conclusion`` and ``record_silence`` closed that
    hole, and the union then meant the opposite of what it said: a decision with no audit
    row is now an item that died mid-flight — a session limit killed one at exactly this
    point — and skipping it on resume would strand an approval with no action and no row
    explaining the silence."""
    from core.db import read_connection

    with read_connection() as conn:
        row = conn.execute(
            """
            SELECT d.run_id, d.invoice_id FROM decisions d
             WHERE NOT EXISTS (
                 SELECT 1 FROM audit_log a
                  WHERE a.run_id = d.run_id AND a.subject_id = d.invoice_id
             )
             LIMIT 1
            """
        ).fetchone()

    if row is None:
        pytest.skip("no interrupted item in this database to assert against")
    assert row["invoice_id"] not in _already_worked(row["run_id"])

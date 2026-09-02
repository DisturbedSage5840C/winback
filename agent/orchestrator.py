"""The batch loop. Claude owns the reasoning; this file owns the rails.

One agent run per invoice, not one per batch. A single conversation over 190 invoices
would carry every previous customer's details into every subsequent decision, which is
both a privacy problem and a correctness one — the model would start pattern-matching on
its own recent outputs instead of on the invoice in front of it. A fresh context per
invoice costs more tokens and is worth it: each decision is reproducible on its own.

**What the agent is actually for.** Not the arithmetic. The probability comes from a
calibrated XGBoost, the expected values from a recursive lookahead, the legality from
deterministic rule modules, and the execution from an adapter — all of it Python that
runs the same whether an LLM is in the loop or not. What the agent contributes is the
sequencing: reading a situation, deciding *which* action to ask about, responding to a
denial by asking for a legal slot instead of giving up, and knowing when an invoice is
finished. That is a genuine agentic task, and it is bounded by ``max_turns`` so it cannot
become an expensive one.

**Every guarantee here is structural, not prompted.** The system prompt below asks the
agent to call the guardrail before acting — and if it ignores that instruction entirely,
``agent.gate`` denies the call and ``agent.tools._execute`` refuses it again. The prompt
is there to make the agent efficient, never to make it safe. A build whose compliance
rests on a paragraph of English is a build with no compliance.

**``setting_sources=[]``** so the run does not inherit local Claude Code settings. A batch
that behaved differently on the author's laptop than on a fresh clone would make every
number in this repository unreproducible.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from agent.adapters.base import Adapter, ExecutionMode
from agent.gate import make_money_gate
from agent.hooks import AuditWriter, audit_matcher
from agent.mcp_config import describe, razorpay_servers
from agent.tools import (
    DISALLOWED_TOOLS,
    GATED_TOOLS,
    PREAPPROVED_TOOLS,
    Workbench,
    winback_server,
)
from core.config import get_settings
from core.db import read_connection
from eval.counterfactual import DECISION_LAG_HOURS
from ml.dataset import BankMethodRates
from ml.scorer import load_scorer
from sim.generate import build_dataset
from sim.load import require_fingerprint

SYSTEM_PROMPT = """\
You are Winback's recovery orchestrator. You work one failed subscription invoice at a
time and your job is to recover the money the law allows, and no more than that.

The protocol, in order, every time:

1. Call `assess_recoverability` for the invoice. It returns the calibrated probability
   for every legal action and slot, the expected rupee value of each, and a recommended
   action. Those numbers are computed by a frozen model and a cost policy. Use them.
   Never estimate a probability or a value yourself — you do not have the information to,
   and a number you invent would end up in a compliance record.

2. Call `compliance_guardrail` for the action you intend to take, with the exact invoice
   id, action and timestamp. It applies NPCI OC-215-A (one attempt plus three retries,
   non-peak windows only), the RBI AFA thresholds, consent and DND, the pre-debit notice
   rule, and root-cause retryability.

   - APPROVE: proceed to step 3.
   - REDIRECT_TO_WINDOW: your timestamp was inside a peak window. Pick the first entry
     from `suggested_slots` and call `compliance_guardrail` again with that timestamp.
   - ESCALATE_HUMAN: a person must authorise this debit. Do not execute. Say so and stop.
   - DENY: the action is not permitted. Read `stop_reason`. Do not try to work around it,
     and do not retry the same action hoping for a different answer — the guardrail is
     deterministic and will give you the same one. Stop, or choose a different action.

3. Call `execute_recovery` (to present the mandate) or `simulated_notify` (to message the
   customer) with the SAME invoice id, action and timestamp the guardrail approved. An
   approval is matched on the exact timestamp and is single-use.

Then stop. One invoice, one conclusion. Finish with a single short sentence saying what
you did and which rule authorised it.

Things that are true and worth knowing:
- The retry cap is four presentments per invoice, ever. Attempts already spent are in the
  assessment. When the budget is gone, the answer is to write the invoice off, not to ask
  again in a different way.
- No message reaches a real person. The channel is simulated; the consent gate in front
  of it is real and will block you for a withdrawn or DND customer.
- If a tool refuses you, the refusal is the answer. Report it plainly rather than
  attempting a variation.
"""

#: A live presentment costs two API calls (an order and a payment link); a live nudge
#: costs one. Two is the conservative figure, and it is what an unbounded live run is
#: divided by to decide how many invoices the call budget can actually cover.
LIVE_CALLS_PER_INVOICE = 2


@dataclass(frozen=True, slots=True)
class BatchReport:
    """What one batch did. Printed by the CLI and asserted on by the tests."""

    run_id: str
    execution_mode: str
    invoices: int
    completed: int
    failed: int
    executions: int
    recovered_paise: int
    audit_rows: int
    seconds: float
    total_cost_usd: float
    #: Audit writes that raised. Part of the report rather than a log line, because a
    #: batch that finished with a hole in its audit trail did not really finish.
    audit_failures: tuple[str, ...] = ()
    #: Invoices skipped because a previous attempt at this ``run_id`` already concluded
    #: them. Counted separately from ``completed``: they are done, but this process did
    #: not do them, and a report that merged the two would overstate what it just ran.
    resumed: int = 0
    #: Why the batch stopped early, or ``None`` if it worked the whole cohort.
    halted: str | None = None
    #: Invoices that concluded without calling a tool the audit hook could fire on, and
    #: for which the loop wrote the row instead. Surfaced in the headline rather than
    #: buried, because it counts guardrail approvals the agent obtained and never spent.
    silent: int = 0

    @property
    def audit_complete(self) -> bool:
        return not self.audit_failures

    @property
    def cohort_complete(self) -> bool:
        """Every invoice in the cohort concluded, by this attempt or an earlier one."""
        return self.halted is None and self.completed + self.resumed == self.invoices

    def __str__(self) -> str:
        gap = f" · ⚠ {len(self.audit_failures)} AUDIT WRITES FAILED" if self.audit_failures else ""
        prior = f" (+{self.resumed} resumed)" if self.resumed else ""
        stop = f" · ⚠ HALTED: {self.halted}" if self.halted else ""
        quiet = f" · {self.silent} unacted" if self.silent else ""
        return (
            f"run {self.run_id} [{self.execution_mode}]: "
            f"{self.completed}/{self.invoices} invoices{prior} ({self.failed} errored) · "
            f"{self.executions} actions · ₹{self.recovered_paise / 100:,.0f} recovered · "
            f"{self.audit_rows} audit rows · {self.seconds:.0f}s · "
            f"${self.total_cost_usd:.2f}{quiet}{gap}{stop}"
        )


#: Substrings that mark a failure as environmental rather than about the invoice. A batch
#: that meets one of these will meet it again on invoice after invoice, so the run stops
#: instead of grinding through the remainder. The first full batch did not: it hit an
#: account quota at 85/190 and then reported 105 further "errors" that were all the same
#: sentence, which buried the one fact worth reading and spent twenty minutes doing it.
#: Matched case-insensitively against the exception text, because these arrive as prose
#: inside ``ResultError`` and there is no error code to switch on.
FATAL_TO_THE_BATCH = (
    "session limit",
    "usage limit",
    "rate limit",
    "credit balance",
    "insufficient credit",
)


def _is_fatal_to_the_batch(exc: Exception) -> bool:
    """Will every remaining invoice fail this way too?

    The distinction is between an invoice this batch cannot work and a batch that cannot
    work. One is stepped over — that is why the loop catches broadly. The other is a
    condition of the environment, and continuing past it converts one legible failure into
    a hundred illegible ones while the run's own progress counter keeps climbing.
    """
    text = str(exc).lower()
    return any(marker in text for marker in FATAL_TO_THE_BATCH)


def _already_worked(run_id: str) -> set[str]:
    """Invoices this run has already reached a conclusion on.

    Resumption is possible at all because ``audit_log`` is append-only and keyed by
    ``run_id``: the table *is* the checkpoint, so there is no second progress file to keep
    honest, and no way for the checkpoint to disagree with the evidence. Re-running the
    same ``--run-id`` continues where the last attempt stopped rather than starting over.

    Skipping matters for correctness and not only for time. Working an invoice twice under
    one run would present its mandate twice, and NPCI counts presentments, not batches —
    a resume that redid its completed work would spend legal attempts to buy nothing.

    **``audit_log`` alone, because every conclusion now leaves a row there.** This query
    used to union ``decisions`` as well: an invoice the guardrail denied produced a
    decision and no audit row, so the action table alone found 77 of the 85 invoices an
    interrupted batch had concluded and would have re-worked the eight it wrote off.
    ``record_conclusion`` and ``record_silence`` closed that hole — a write-off, an
    escalation, and an invoice nothing was done to each write their own row — and the
    union then became actively wrong in the other direction. A decision with no audit row
    no longer means "concluded without acting". It means the item died between the
    guardrail's answer and the tool call, which is exactly what a session-limit halt does,
    and it is the one case that *must* be re-worked. Skipping it would leave an invoice
    with an approval on record, no action, and no audit row explaining the silence.

    Re-working it is safe against the cap: ``execute_recovery`` writes its audit row
    through the ``PostToolUse`` hook, so an invoice with no row had no presentment, and
    the attempt budget is counted from ``payment_attempts`` rather than from this query.
    """
    with read_connection() as conn:
        rows = conn.execute(
            """
            SELECT subject_id AS invoice_id FROM audit_log
             WHERE run_id = %(run)s AND subject_type = 'invoice'
            """,
            {"run": run_id},
        ).fetchall()
    return {row["invoice_id"] for row in rows}


@dataclass
class _Tally:
    completed: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    #: Invoices that finished without leaving an audit row of their own, and had one
    #: written for them by the loop. Not an error — the run is still complete — but a
    #: number worth reading, because each one is a decision the agent reached in prose and
    #: never carried out.
    silent: int = 0
    errors: list[str] = field(default_factory=list)
    #: Set when the batch stopped early. Named in the report, because a run that worked
    #: 85 of 190 invoices and one that worked all 190 must not print the same shape of
    #: line with different numbers in it.
    halted: str | None = None


def _options(bench: Workbench, writer: AuditWriter, settings) -> ClaudeAgentOptions:
    """Everything that constrains one agent run.

    ``permission_mode="default"`` rather than anything more permissive: the money gate is
    a permission callback, and a mode that bypassed permissions would bypass it.

    ``allowed_tools`` is :data:`PREAPPROVED_TOOLS` and not the full permitted set. An
    entry there auto-approves a tool *before* ``can_use_tool`` is consulted, so listing
    the two gated tools would silently disable the gate — the SDK warns about this at
    construction time, and the warning was correct. The gated tools are omitted here on
    purpose, so every call to them falls through to the callback.
    """
    servers = {"winback": winback_server(bench), **razorpay_servers(settings)}

    # The SDK warns, once per run, that ``can_use_tool`` will not fire for the entries in
    # ``allowed_tools``. That is true and it is the design: the two tools listed there are
    # reading a model and asking a rule engine, neither of which is a privileged act. The
    # warning was load-bearing while the money tools were wrongly on that list (see
    # docs/WHAT_BROKE.md, 31 Aug) — but it now fires 190 times per batch saying something
    # the run header states once, in the affirmative, naming both halves of the split.
    # Suppressed here rather than globally, so any *other* shadowing warning still shows.
    warnings.filterwarnings(
        "ignore",
        message=r"can_use_tool will not be invoked for: .*assess_recoverability.*",
        category=Warning,
    )

    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers=servers,
        allowed_tools=list(PREAPPROVED_TOOLS),
        disallowed_tools=list(DISALLOWED_TOOLS),
        can_use_tool=make_money_gate(bench, writer),
        hooks={"PostToolUse": [audit_matcher(writer)]},
        permission_mode="default",
        max_turns=settings.max_turns_per_item,
        model=settings.agent_model,
        setting_sources=[],
    )


async def _run_one(
    invoice_id: str, now: datetime, options: ClaudeAgentOptions
) -> tuple[str, float]:
    """One invoice, one fresh context, at most ``max_turns`` turns."""
    prompt = (
        f"Invoice {invoice_id} failed its scheduled debit. The current time is "
        f"{now.isoformat()}. Work it to a conclusion following the protocol."
    )
    summary, cost = "", 0.0
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    summary = block.text.strip()
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
    return summary, cost


def _adapter_for(settings) -> Adapter | None:
    """The executor this run was configured for, or ``None`` to take the oracle.

    Returning ``None`` rather than constructing a ``SimulatedAdapter`` here keeps a single
    source of that object: ``workbench_from_dataset`` builds one anyway to supply the
    cases, and two simulators over the same cohort would each carry their own physics
    state — a nudge recorded against one and invisible to the other.

    The live branch is deliberately noisy about failing. ``from_settings`` raises when the
    keys are missing, and that raise ends the run. A live batch that silently fell back to
    the oracle would write ``execution_mode = 'simulated'`` truthfully on every row and
    still be a lie in aggregate, because someone asked for real API calls and was told
    nothing when they did not happen.

    **Coerced, not compared by identity.** ``core.config`` types this field as a
    ``Literal["simulated", "live"]`` and hands back an ordinary ``str``; the enum lives
    here, in the adapter layer, because config has no business importing an adapter. So
    ``settings.execution_mode is ExecutionMode.LIVE`` is ``False`` for the real string
    ``"live"`` and ``True`` only for the enum member — which is what the test injected and
    the CLI never produced. ``--live`` therefore ran the oracle on every invocation it has
    ever had, printed ``[simulated]`` in its own report, and exited 0. See
    docs/WHAT_BROKE.md, 2 Sep.
    """
    if ExecutionMode(settings.execution_mode) is not ExecutionMode.LIVE:
        return None

    from agent.adapters.live_razorpay import LiveRazorpayAdapter

    return LiveRazorpayAdapter.from_settings(settings)


async def run_batch(
    *,
    limit: int | None = None,
    run_id: str | None = None,
    cohort: str = "test",
    verbose: bool = True,
) -> BatchReport:
    """Work every at-risk invoice in the cohort.

    Refuses to start unless the database holds the dataset this code was built against.
    One query, and it prevents a whole class of unreproducible run: a batch whose audit
    rows point at invoice ids from a world that has since been regenerated.
    """
    settings = get_settings()
    require_fingerprint()

    run_id = run_id or f"batch_{datetime.now():%Y%m%d_%H%M%S}"
    dataset = build_dataset()
    scorer = load_scorer()
    rates = BankMethodRates.fit(dataset, cohort="train")

    from agent.tools import workbench_from_dataset

    bench = workbench_from_dataset(
        scorer=scorer, rates=rates, cohort=cohort, adapter=_adapter_for(settings)
    )
    writer = AuditWriter(bench=bench, run_id=run_id, arm="D")
    options = _options(bench, writer, settings)

    invoice_ids = sorted(bench.cases)
    if limit is not None:
        invoice_ids = invoice_ids[:limit]

    # A live run without an explicit --limit is almost certainly a mistake: 190 invoices
    # against the real API would spend the call budget in the first dozen and then raise
    # on every invoice after it. Capped at the budget rather than refused, so the failure
    # mode of forgetting the flag is a small correct run instead of 178 errors.
    if bench.adapter.mode is ExecutionMode.LIVE and limit is None:
        invoice_ids = invoice_ids[: max(1, settings.live_call_budget // LIVE_CALLS_PER_INVOICE)]

    # Resumption, before anything is spent. An invoice with audit rows under this run_id
    # has already been concluded and must not be worked twice — see `_already_worked`.
    done = _already_worked(run_id) & set(invoice_ids)
    pending = [invoice_id for invoice_id in invoice_ids if invoice_id not in done]

    if verbose:
        print(f"run {run_id} · {len(invoice_ids)} invoices · {describe(settings)}")
        if done:
            print(f"resuming: {len(done)} already concluded, {len(pending)} to work")
        print(f"executor: {bench.adapter.mode} · model {settings.agent_model}")
        # Stated positively at the top of every run, because "which tools are gated" is
        # the first question a reviewer asks and the answer should not require reading
        # the source. Anything in the second list reaches `can_use_tool` on every call.
        print(f"pre-approved: {', '.join(sorted(n.split('__')[-1] for n in PREAPPROVED_TOOLS))}")
        print(f"gated:        {', '.join(sorted(n.split('__')[-1] for n in GATED_TOOLS))}\n")

    tally = _Tally()
    started = time.monotonic()

    for index, invoice_id in enumerate(pending, start=1):
        case = bench.cases[invoice_id]
        now = case.first_charge.attempted_at + timedelta(hours=DECISION_LAG_HOURS)
        try:
            summary, cost = await _run_one(invoice_id, now, options)
            tally.completed += 1
            tally.cost_usd += cost

            # Every invoice the agent finishes must leave a row, and one that reached a
            # conclusion in prose and never in the table is the one case the hooks cannot
            # see: `PostToolUse` fires on tools, and the event here is that no tool ran.
            # `live_v2` did exactly this once in twelve — approved a retry, said so, and
            # exhausted its turn budget. See `AuditWriter.record_silence`.
            if invoice_id not in writer.covered:
                tally.silent += 1
                writer.record_silence(invoice_id)
        except Exception as exc:  # broad on purpose — one bad invoice must not end the batch
            # The batch is meant to complete unattended. An invoice that throws is
            # recorded and stepped over; ending the run would lose the 189 that worked.
            tally.failed += 1
            tally.errors.append(f"{invoice_id}: {type(exc).__name__}: {exc}")
            summary = f"ERROR {type(exc).__name__}"

            # Unless it is not about this invoice at all. An exhausted quota or a rate
            # limit will meet every invoice left, so the batch stops and says so — and
            # what it has written stays written, ready to be resumed under the same
            # --run-id once the condition clears.
            if _is_fatal_to_the_batch(exc):
                tally.halted = str(exc).strip()[:160]
                if verbose:
                    print(f"\n⚠ halted at {invoice_id}: {tally.halted}")
                    print(f"  {len(pending) - index} invoice(s) not attempted. Resume with:")
                    print(f"    python -m agent.orchestrator --run-id {run_id}")
                break

        if verbose:
            recovered = sum(row.get("recovered_paise", 0) for row in bench.executions)
            print(
                f"[{index:>3}/{len(pending)}] {invoice_id} · "
                f"₹{recovered / 100:,.0f} so far · {summary[:96]}"
            )

    if verbose and tally.errors:
        print(f"\n{len(tally.errors)} invoice(s) errored:")
        for line in tally.errors[:10]:
            print(f"  {line}")

    if verbose and writer.write_failures:
        print(f"\n{len(writer.write_failures)} audit write(s) failed:")
        for line in writer.write_failures[:10]:
            print(f"  {line}")

    return BatchReport(
        run_id=run_id,
        execution_mode=str(bench.adapter.mode),
        invoices=len(invoice_ids),
        completed=tally.completed,
        failed=tally.failed,
        executions=len(bench.executions),
        recovered_paise=sum(row.get("recovered_paise", 0) for row in bench.executions),
        audit_rows=writer.rows_written,
        seconds=time.monotonic() - started,
        total_cost_usd=tally.cost_usd,
        audit_failures=tuple(writer.write_failures),
        resumed=len(done),
        halted=tally.halted,
        silent=tally.silent,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Winback recovery batch.")
    parser.add_argument("--limit", type=int, default=None, help="work only the first N invoices")
    parser.add_argument("--run-id", default=None, help="override the generated run id")
    parser.add_argument("--cohort", default="test", help="which cohort to work (default: test)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="execute against the real Razorpay test-mode API (creates real plink_ ids)",
    )
    args = parser.parse_args()

    # The flag sets the environment rather than being threaded through as a parameter, so
    # there is exactly one place that decides the execution mode — `core.config` — and the
    # flag and `.env` cannot disagree about which lane a run is in. Set before the first
    # `get_settings()`, whose result is cached.
    if args.live:
        os.environ["WINBACK_EXECUTION_MODE"] = "live"

    report = asyncio.run(run_batch(limit=args.limit, run_id=args.run_id, cohort=args.cohort))
    print(f"\n{report}")

    # What was asked for, checked against what the report says happened — by a different
    # path from the one that chose the adapter. `--live` silently ran the oracle for its
    # whole existence because nothing ever compared those two, and the run exited 0 while
    # printing `[simulated]` in its own headline.
    if args.live and report.execution_mode != "live":
        print(f"\n⚠ --live was requested and the batch ran [{report.execution_mode}].")
        return 1

    # A hole in the audit trail fails the run as surely as a crashed invoice does, and a
    # batch that stopped before the end of its cohort has not finished no matter how many
    # invoices it got through.
    return 0 if report.cohort_complete and report.audit_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())

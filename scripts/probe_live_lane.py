"""Probe what a fresh Razorpay test account actually permits.

The build plan assumed retries could be driven through the Subscriptions REST API.
They cannot. So the useful question is not what the documentation describes, it is
what this account permits *today* — an empirical question, given an empirical
answer here and recorded verbatim in ``docs/LIVE_LANE_FINDINGS.md``.

**A probe reports one of five outcomes, not a boolean.** The first version of this
script had ``ok: bool``, which forced three genuinely different states through two
slots: a capability confirmed absent, a probe that never ran, and evidence that
cannot distinguish between hypotheses were all printed as ``FAIL``. That is the
same defect as scoring a gateway route-miss as a pass — a verdict asserting more
than the evidence supports. The default outcome is now ``SKIPPED``, so a probe that
never executes cannot be silently counted as anything else.

Safety rails, all of them load-bearing:

* Test mode only. ``core/config.py`` refuses a non-``rzp_test_`` key at startup.
* Payment links are created with ``notify: {sms: false, email: false}``, and the
  invoice in probe 10 is created as a **draft** precisely so that ``notify_by``
  cannot deliver anything. No message reaches a real phone or inbox from this
  system, in any lane.
* Read-heavy: a handful of writes, all to test-mode entities that cost nothing.

Run: ``.venv/bin/python -m scripts.probe_live_lane``
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from core.config import get_settings

API = "https://api.razorpay.com/v1"
MCP_URL = "https://mcp.razorpay.com/mcp"
MCP_IMAGE = "razorpay/mcp"
TIMEOUT = 30.0
MCP_STARTUP_TIMEOUT = 60.0


class Outcome(StrEnum):
    """What a probe is entitled to claim, given what it actually observed."""

    #: The capability works on this account. Goes in the live lane.
    PASS = "PASS"  # noqa: S105 — a probe verdict, not a credential
    #: The capability is confirmed absent, and the error text says why.
    FAIL = "FAIL"
    #: Confirmed absent, and that *was* the hypothesis. Probe 9 is the whole reason
    #: the two-adapter architecture exists; scoring it as a failure would read as a
    #: build defect rather than as the finding it is.
    EXPECTED_FAIL = "EXPECTED"
    #: Ran, but the evidence cannot distinguish between two live hypotheses. Never
    #: to be rounded to either neighbour.
    INCONCLUSIVE = "INCONC"
    #: Did not run. The default, so that "not attempted" can never be read as a
    #: result — the bug this enum was introduced to make impossible.
    SKIPPED = "SKIP"


@dataclass
class Probe:
    number: int
    name: str
    outcome: Outcome = Outcome.SKIPPED
    status: int | None = None
    #: How the probe talked to Razorpay. Not every probe is HTTP — the local MCP
    #: probes speak JSON-RPC over a container's stdio and have no status code, and
    #: printing "did not run" for those was itself a verdict the evidence did not
    #: support. The transport is stated rather than inferred from a null status.
    channel: str = "http"
    entity_id: str | None = None
    note: str = ""
    body: Any = field(default=None, repr=False)

    @property
    def usable(self) -> bool:
        """Whether this capability may be wired into ``LiveRazorpayAdapter``."""
        return self.outcome is Outcome.PASS

    def render(self) -> str:
        if self.status is not None:
            where = f"HTTP {self.status}"
        elif self.outcome is Outcome.SKIPPED:
            where = "did not run"
        else:
            where = self.channel
        head = f"[{self.outcome:^7}] {self.number:>2}. {self.name}  ({where})"
        tail = f"\n          -> {self.entity_id}" if self.entity_id else ""
        note = f"\n          {self.note}" if self.note else ""
        return head + tail + note


def _short(body: Any, limit: int = 300) -> str:
    text = json.dumps(body) if not isinstance(body, str) else body
    return text if len(text) <= limit else text[:limit] + "…"


def _error_text(body: Any) -> str:
    """Razorpay nests the useful part under error.description."""
    if isinstance(body, dict) and "error" in body:
        err = body["error"]
        if isinstance(err, dict):
            parts = [err.get("code"), err.get("description"), err.get("reason")]
            return " | ".join(str(p) for p in parts if p)
    return _short(body)


def _is_gateway_miss(body: Any) -> bool:
    """Kong's signature for an unregistered path pattern.

    Structurally different from an application 404 for an entity that does not
    exist: one says "this route is not served here", the other says "this route is
    served, and your id is wrong". Both arrive as 404.
    """
    return isinstance(body, dict) and "no Route matched" in str(body.get("message", ""))


def run(client: httpx.Client, probes: list[Probe]) -> None:
    """Execute the REST matrix in order, threading created ids between probes."""
    order_id: str | None = None
    customer_id: str | None = None

    def call(
        probe: Probe, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        try:
            response = client.request(method, f"{API}{path}", json=payload)
        except httpx.HTTPError as exc:
            probe.outcome = Outcome.INCONCLUSIVE
            probe.note = f"transport error, nothing learned about the endpoint: {exc}"
            return None
        probe.status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = response.text
        probe.body = body
        probe.outcome = Outcome.PASS if response.is_success else Outcome.FAIL
        if not response.is_success:
            probe.note = _error_text(body)
        return body

    # --- 3. create_order -----------------------------------------------------
    p = probes[2]
    body = call(p, "POST", "/orders", {
        "amount": 49900, "currency": "INR", "receipt": "winback_probe_order",
        "notes": {"source": "winback_live_lane_probe"},
    })
    if p.usable and isinstance(body, dict):
        order_id = body.get("id")
        p.entity_id = order_id

    # --- 4. create_payment_link, notifications suppressed --------------------
    p = probes[3]
    body = call(p, "POST", "/payment_links", {
        "amount": 49900, "currency": "INR",
        "description": "Winback probe - notifications suppressed",
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"source": "winback_live_lane_probe"},
    })
    if p.usable and isinstance(body, dict):
        p.entity_id = body.get("id")
        sent = body.get("notify", {})
        p.note = f"notify={sent}, short_url={body.get('short_url')}"

    # --- 5. create_payment_link_upi (UPI-only accept) ------------------------
    p = probes[4]
    body = call(p, "POST", "/payment_links", {
        "amount": 49900, "currency": "INR",
        "description": "Winback probe - UPI only",
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "accept_partial": False,
        "options": {"checkout": {"method": {"upi": "1"}}},
    })
    if p.usable and isinstance(body, dict):
        p.entity_id = body.get("id")

    # --- 6. the read lane ----------------------------------------------------
    p = probes[5]
    reads: list[str] = []
    call(p, "GET", "/payments?count=3")
    reads.append(f"fetch_all_payments={p.status}")
    if order_id:
        sub = Probe(0, "")
        call(sub, "GET", f"/orders/{order_id}/payments")
        reads.append(f"fetch_order_payments={sub.status}")
        if not sub.usable:
            p.outcome = Outcome.FAIL
    p.note = ", ".join(reads)

    # --- 7. customer + tokens (the mandate lane) -----------------------------
    p = probes[6]
    stamp = int(time.time())
    created = Probe(0, "")
    cust = call(created, "POST", "/customers", {
        "name": "Winback Probe", "contact": "9000090000",
        "email": f"winback.probe.{stamp}@example.com", "fail_existing": "0",
    })
    if created.usable and isinstance(cust, dict):
        customer_id = cust.get("id")
        body = call(p, "GET", f"/customers/{customer_id}/tokens")
        count = body.get("count") if isinstance(body, dict) else "?"
        p.entity_id = customer_id
        p.note = f"customer created; token count={count} (empty is expected)"
    else:
        p.status, p.outcome = created.status, Outcome.FAIL
        p.note = f"customer create failed: {_error_text(cust)}"

    # --- 8. create_subscription (needs a plan first) -------------------------
    p = probes[7]
    plan_probe = Probe(0, "")
    plan = call(plan_probe, "POST", "/plans", {
        "period": "monthly", "interval": 1,
        "item": {"name": "Winback probe plan", "amount": 49900, "currency": "INR"},
    })
    if plan_probe.usable and isinstance(plan, dict):
        body = call(p, "POST", "/subscriptions", {
            "plan_id": plan["id"], "total_count": 12, "customer_notify": 0,
        })
        if p.usable and isinstance(body, dict):
            p.entity_id = body.get("id")
            p.note = f"plan={plan['id']}, status={body.get('status')}"
    else:
        p.status, p.outcome = plan_probe.status, Outcome.FAIL
        p.note = f"plan create failed: {_error_text(plan)}"

    # --- 9. the one that matters: S2S recurring ------------------------------
    # Expected to fail. The exact error is the evidence for the adapter split, and
    # the control below is what turns "probably not activated" into an observation.
    p = probes[8]
    body = call(p, "POST", "/payments/create/recurring", {
        "email": "winback.probe@example.com", "contact": "9000090000",
        "amount": 49900, "currency": "INR",
        "order_id": order_id or "order_missing",
        "customer_id": customer_id or "cust_missing",
        "token": "token_probe_nonexistent",
        "recurring": "1", "description": "Winback S2S probe",
    })
    if p.outcome is Outcome.FAIL:
        # The control: a documented sibling in the same route family, whose failure
        # mode I already know. Identical errors across the family while /orders
        # returns 200 means the family is not routed, not that I mistyped a URL.
        control = Probe(0, "")
        control_body = call(control, "POST", "/payments/create/upi", {
            "amount": 49900, "currency": "INR", "order_id": order_id or "order_missing",
        })
        same = _error_text(control_body) == _error_text(body)
        p.outcome = Outcome.EXPECTED_FAIL
        p.note = (
            f"[expected] {_error_text(body)} — control POST /payments/create/upi "
            f"returned {control.status}, {'identical error' if same else 'a different error'}; "
            f"the /payments/create/* family is not routed without S2S activation"
        )
    else:
        p.note = "UNEXPECTEDLY SUCCEEDED — re-read the live lane assumptions"

    # --- 10. invoice notify_by, against a real invoice -----------------------
    # The earlier pass hit a deliberately nonexistent invoice id and got Kong's
    # "no Route matched", which cannot distinguish an unregistered path from an
    # unknown entity. This pass creates a real invoice and asks about *that*, so
    # a gateway miss and an application response are finally separable.
    #
    # The invoice is created as a **draft** on purpose. A draft cannot be
    # delivered, so this probe learns whether the route exists without any
    # possibility of a send — the TRAI rail holds while the question gets answered.
    p = probes[9]
    inv_probe = Probe(0, "")
    invoice = call(inv_probe, "POST", "/invoices", {
        "type": "invoice", "draft": "1", "currency": "INR",
        "description": "Winback probe - draft, never issued",
        "customer": {
            "name": "Winback Probe",
            "contact": "9000090000",
            "email": f"winback.probe.inv.{stamp}@example.com",
        },
        "line_items": [{
            "name": "Winback probe line item", "amount": 49900,
            "currency": "INR", "quantity": 1,
        }],
        "sms_notify": 0, "email_notify": 0,
    })
    if not inv_probe.usable or not isinstance(invoice, dict):
        p.status, p.outcome = inv_probe.status, Outcome.INCONCLUSIVE
        p.note = f"could not create the draft invoice to ask about: {_error_text(invoice)}"
    else:
        invoice_id = invoice.get("id")
        p.entity_id = invoice_id
        body = call(p, "POST", f"/invoices/{invoice_id}/notify_by/sms")
        if _is_gateway_miss(body):
            p.outcome = Outcome.FAIL
            p.note = (
                "gateway reports no route for this path pattern even against a real "
                f"invoice ({invoice_id}) — the endpoint is not served on this account"
            )
        elif p.usable:
            p.outcome = Outcome.INCONCLUSIVE
            p.note = (
                "route exists and accepted a notify on a DRAFT invoice, which should "
                "not be deliverable. Nothing was sent (draft), but the acceptance is "
                "not understood — do not wire this until it is."
            )
        else:
            p.outcome = Outcome.PASS
            p.note = (
                f"route exists: reached the application, which refused the draft on "
                f"business grounds — {_error_text(body)}. Not a gateway miss. No send."
            )


def probe_remote_mcp(key_id: str, secret: str) -> Probe:
    """Probe 1: the remote MCP handshake."""
    remote = Probe(1, "Remote MCP handshake (mcp.razorpay.com)")
    token = base64.b64encode(f"{key_id}:{secret}".encode()).decode()
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "winback-probe", "version": "0.1.0"},
        },
    }
    try:
        response = httpx.post(
            MCP_URL,
            json=payload,
            headers={
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=TIMEOUT,
        )
        remote.status = response.status_code
        remote.outcome = Outcome.PASS if response.is_success else Outcome.FAIL
        remote.note = _short(response.text, 240)
    except httpx.HTTPError as exc:
        remote.outcome = Outcome.INCONCLUSIVE
        remote.note = f"transport error: {exc}"
    return remote


def _local_mcp_tools(
    key_id: str, secret: str, extra_env: dict[str, str] | None = None
) -> tuple[list[str], str]:
    """List the tools a local ``razorpay/mcp`` container advertises over stdio.

    Returns ``(tool_names, diagnostic)``. An empty list with a non-empty diagnostic
    means the handshake did not complete — which is a *skip*, not a finding about
    the tool surface.
    """
    env_args: list[str] = ["-e", "RAZORPAY_KEY_ID", "-e", "RAZORPAY_KEY_SECRET"]
    for name in (extra_env or {}):
        env_args += ["-e", name]

    child_env = os.environ | {
        "RAZORPAY_KEY_ID": key_id, "RAZORPAY_KEY_SECRET": secret
    } | (extra_env or {})

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "winback-probe", "version": "0.1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    stdin = "".join(json.dumps(r) + "\n" for r in requests)

    try:
        # Suppressions justified: argv is a fixed literal plus env *names* (never
        # values), and the secrets travel through the environment rather than the
        # command line, so they never appear in `ps` or in a shell history.
        completed = subprocess.run(  # noqa: S603
            ["docker", "run", "--rm", "-i", *env_args, MCP_IMAGE],  # noqa: S607
            input=stdin, capture_output=True, text=True,
            timeout=MCP_STARTUP_TIMEOUT, env=child_env, check=False,
        )
    except FileNotFoundError:
        return [], "docker is not on PATH"
    except subprocess.TimeoutExpired:
        return [], f"container did not answer within {MCP_STARTUP_TIMEOUT:.0f}s"

    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == 2 and "result" in message:
            tools = message["result"].get("tools", [])
            return sorted(t["name"] for t in tools), ""

    stderr = completed.stderr.strip().splitlines()
    detail = stderr[-1] if stderr else _short(completed.stdout, 160) or "no output"
    return [], f"no tools/list response (exit {completed.returncode}): {detail}"


def probe_local_mcp(key_id: str, secret: str) -> Probe:
    """Probe 2: the local Docker stdio transport, and what it exposes."""
    p = Probe(2, f"Local MCP over stdio ({MCP_IMAGE})", channel="docker stdio")
    tools, diagnostic = _local_mcp_tools(key_id, secret)
    if not tools:
        # Explicitly a skip, not a failure: nothing was learned about the image.
        p.outcome = Outcome.SKIPPED
        p.note = f"handshake did not complete, so nothing is claimed: {diagnostic}"
        return p

    p.outcome = Outcome.PASS
    restricted = sorted({
        "create_refund", "close_qr_code", "create_instant_settlement",
        "create_registration_link",
    } & set(tools))
    # The question the build plan actually needs answered: the plan assumed
    # `create_registration_link` was available locally and would be the real action
    # for a dead mandate. Checked here rather than assumed, because discovering its
    # absence while wiring the adapter would be expensive.
    mandate_tools = [t for t in tools if any(
        word in t for word in ("regist", "mandate", "subscri", "recurring", "plan")
    )]
    p.note = (
        f"{len(tools)} tools over stdio; remote-restricted tools present locally: "
        f"{restricted or 'none'}; mandate/registration tools: "
        f"{mandate_tools or 'NONE — the local image has no mandate surface at all'}"
    )
    return p


def probe_toolset_flags(key_id: str, secret: str, baseline: Probe) -> Probe:
    """Probe 11: whether ``TOOLSETS`` / ``READ_ONLY`` actually narrow the surface."""
    p = Probe(
        11, "TOOLSETS / READ_ONLY narrow the local tool surface", channel="docker stdio"
    )
    if not baseline.usable:
        p.note = "depends on probe 2, which did not complete"
        return p

    full, _ = _local_mcp_tools(key_id, secret)
    read_only, ro_diag = _local_mcp_tools(key_id, secret, {"READ_ONLY": "true"})
    scoped, sc_diag = _local_mcp_tools(key_id, secret, {"TOOLSETS": "orders"})

    if not read_only and not scoped:
        p.outcome = Outcome.INCONCLUSIVE
        p.note = f"neither flag run produced a tool list: {ro_diag or sc_diag}"
        return p

    writes_removed = sorted(set(full) - set(read_only))
    narrowed = len(read_only) < len(full) or len(scoped) < len(full)
    p.outcome = Outcome.PASS if narrowed else Outcome.FAIL
    p.note = (
        f"default={len(full)} tools · READ_ONLY=true → {len(read_only)} "
        f"(removed {len(writes_removed)}) · TOOLSETS=orders → {len(scoped)}"
    )
    return p


def main() -> int:
    settings = get_settings()
    key_id, secret = settings.require_razorpay()
    print(f"Probing as {key_id} (test mode)\n" + "=" * 76)

    probes = [
        Probe(1, "Remote MCP handshake"),
        Probe(2, "Local MCP over stdio"),
        Probe(3, "create_order"),
        Probe(4, "create_payment_link (notify suppressed)"),
        Probe(5, "create_payment_link_upi"),
        Probe(6, "read lane: payments / order payments"),
        Probe(7, "customer + fetch_tokens"),
        Probe(8, "create_subscription"),
        Probe(9, "POST /payments/create/recurring (S2S)"),
        Probe(10, "POST /invoices/:id/notify_by/:medium"),
    ]

    probes[0] = probe_remote_mcp(key_id, secret)
    probes[1] = probe_local_mcp(key_id, secret)

    with httpx.Client(auth=(key_id, secret), timeout=TIMEOUT) as client:
        run(client, probes)

    probes.append(probe_toolset_flags(key_id, secret, probes[1]))

    print()
    for probe in probes:
        print(probe.render())

    tally = {outcome: 0 for outcome in Outcome}
    for probe in probes:
        tally[probe.outcome] += 1

    print("\n" + "=" * 76)
    print(
        "  ".join(f"{outcome}={tally[outcome]}" for outcome in Outcome)
        + f"  (of {len(probes)})"
    )
    print(
        f"{sum(1 for p in probes if p.usable)} capabilities are cleared for the live "
        "lane. Record verbatim in docs/LIVE_LANE_FINDINGS.md."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

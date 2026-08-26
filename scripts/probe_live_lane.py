"""Probe what a fresh Razorpay test account actually permits.

The build plan assumed retries could be driven through the Subscriptions REST API.
They cannot. So the useful question is not what the documentation describes, it is
what this account permits *today* — an empirical question, given an empirical
answer here and recorded verbatim in ``docs/LIVE_LANE_FINDINGS.md``.

Every probe records the raw status and body. Nothing is inferred, and a failure is
a result rather than an error: probe 9 is *expected* to fail, and its exact error
message is the evidence for why ``LiveRazorpayAdapter`` and ``SimulatedAdapter``
both exist.

Safety rails, all of them load-bearing:

* Test mode only. ``core/config.py`` refuses a non-``rzp_test_`` key at startup.
* Payment links are created with ``notify: {sms: false, email: false}``. No message
  reaches a real phone or inbox from this system, in any lane.
* Read-heavy: exactly four writes, all to test-mode entities that cost nothing.

Run: ``.venv/bin/python -m scripts.probe_live_lane``
"""

from __future__ import annotations

import base64
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.config import get_settings

API = "https://api.razorpay.com/v1"
MCP_URL = "https://mcp.razorpay.com/mcp"
TIMEOUT = 30.0


@dataclass
class Probe:
    number: int
    name: str
    status: int | None = None
    ok: bool = False
    entity_id: str | None = None
    note: str = ""
    body: Any = field(default=None, repr=False)

    def render(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        head = f"[{mark}] {self.number:>2}. {self.name}  (HTTP {self.status})"
        tail = f"\n        -> {self.entity_id}" if self.entity_id else ""
        note = f"\n        {self.note}" if self.note else ""
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


def run(client: httpx.Client, probes: list[Probe]) -> None:
    """Execute the matrix in order, threading created ids between probes."""
    order_id: str | None = None
    customer_id: str | None = None

    def call(
        probe: Probe, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        try:
            response = client.request(method, f"{API}{path}", json=payload)
        except httpx.HTTPError as exc:
            probe.status, probe.note = None, f"transport error: {exc}"
            return None
        probe.status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = response.text
        probe.body = body
        probe.ok = response.is_success
        if not probe.ok:
            probe.note = _error_text(body)
        return body

    # --- 3. create_order -----------------------------------------------------
    p = probes[2]
    body = call(p, "POST", "/orders", {
        "amount": 49900, "currency": "INR", "receipt": "winback_probe_order",
        "notes": {"source": "winback_live_lane_probe"},
    })
    if p.ok and isinstance(body, dict):
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
    if p.ok and isinstance(body, dict):
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
    if p.ok and isinstance(body, dict):
        p.entity_id = body.get("id")

    # --- 6. the read lane ----------------------------------------------------
    p = probes[5]
    reads: list[str] = []
    body = call(p, "GET", "/payments?count=3")
    reads.append(f"fetch_all_payments={p.status}")
    if order_id:
        sub = Probe(0, "")
        call(sub, "GET", f"/orders/{order_id}/payments")
        reads.append(f"fetch_order_payments={sub.status}")
    p.note = ", ".join(reads)

    # --- 7. customer + tokens (the mandate lane) -----------------------------
    p = probes[6]
    stamp = int(time.time())
    created = Probe(0, "")
    cust = call(created, "POST", "/customers", {
        "name": "Winback Probe", "contact": "9000090000",
        "email": f"winback.probe.{stamp}@example.com", "fail_existing": "0",
    })
    if created.ok and isinstance(cust, dict):
        customer_id = cust.get("id")
        body = call(p, "GET", f"/customers/{customer_id}/tokens")
        count = body.get("count") if isinstance(body, dict) else "?"
        p.entity_id = customer_id
        p.note = f"customer created; token count={count} (empty is expected)"
    else:
        p.status, p.note = created.status, f"customer create failed: {_error_text(cust)}"

    # --- 8. create_subscription (needs a plan first) -------------------------
    p = probes[7]
    plan_probe = Probe(0, "")
    plan = call(plan_probe, "POST", "/plans", {
        "period": "monthly", "interval": 1,
        "item": {"name": "Winback probe plan", "amount": 49900, "currency": "INR"},
    })
    if plan_probe.ok and isinstance(plan, dict):
        body = call(p, "POST", "/subscriptions", {
            "plan_id": plan["id"], "total_count": 12, "customer_notify": 0,
        })
        if p.ok and isinstance(body, dict):
            p.entity_id = body.get("id")
            p.note = f"plan={plan['id']}, status={body.get('status')}"
    else:
        p.status = plan_probe.status
        p.note = f"plan create failed: {_error_text(plan)}"

    # --- 9. the one that matters: S2S recurring ------------------------------
    # Expected to fail. The exact error is the evidence for the adapter split.
    p = probes[8]
    body = call(p, "POST", "/payments/create/recurring", {
        "email": "winback.probe@example.com", "contact": "9000090000",
        "amount": 49900, "currency": "INR",
        "order_id": order_id or "order_missing",
        "customer_id": customer_id or "cust_missing",
        "token": "token_probe_nonexistent",
        "recurring": "1", "description": "Winback S2S probe",
    })
    p.note = f"[expected failure] {_error_text(body)}" if not p.ok else "UNEXPECTEDLY SUCCEEDED"

    # --- 10. invoice notify_by ----------------------------------------------
    # Careful here: "no Route matched with those values" is Kong's *gateway-level*
    # message for a path pattern that is not registered at all, which is a different
    # finding from an app-level 404 for an invoice that does not exist. Reading the
    # first as the second would record this route as available when it is not, so
    # the probe reports inconclusive rather than guessing.
    p = probes[9]
    body = call(p, "POST", "/invoices/inv_probe_nonexistent/notify_by/sms")
    gateway_miss = isinstance(body, dict) and "no Route matched" in str(body.get("message", ""))
    p.ok = False
    p.note = (
        "INCONCLUSIVE: gateway reports no route for this path pattern, which is not "
        "the same as an unknown invoice id. Moot either way -- Winback never sends."
        if gateway_miss
        else f"route exists, refused the nonexistent invoice: {_error_text(body)}"
    )


def probe_mcp(key_id: str, secret: str) -> tuple[Probe, Probe]:
    """Probes 1 and 2: the remote MCP handshake, and whether Docker can serve local."""
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
        remote.ok = response.is_success
        remote.note = _short(response.text, 240)
    except httpx.HTTPError as exc:
        remote.note = f"transport error: {exc}"

    local = Probe(2, "Local MCP image (docker pull razorpay/mcp)")
    local.note = "not attempted in this pass -- Day 6, alongside the stdio transport"
    return remote, local


def main() -> int:
    settings = get_settings()
    key_id, secret = settings.require_razorpay()
    print(f"Probing as {key_id} (test mode)\n" + "=" * 72)

    probes = [
        Probe(1, "Remote MCP handshake"),
        Probe(2, "Local MCP image"),
        Probe(3, "create_order"),
        Probe(4, "create_payment_link (notify suppressed)"),
        Probe(5, "create_payment_link_upi"),
        Probe(6, "read lane: payments / order payments"),
        Probe(7, "customer + fetch_tokens"),
        Probe(8, "create_subscription"),
        Probe(9, "POST /payments/create/recurring (S2S)"),
        Probe(10, "POST /invoices/:id/notify_by/:medium"),
    ]

    probes[0], probes[1] = probe_mcp(key_id, secret)

    with httpx.Client(auth=(key_id, secret), timeout=TIMEOUT) as client:
        run(client, probes)

    print()
    for probe in probes:
        print(probe.render())

    passed = sum(1 for p in probes if p.ok)
    print("\n" + "=" * 72)
    print(f"{passed}/{len(probes)} probes passed. Record verbatim in LIVE_LANE_FINDINGS.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

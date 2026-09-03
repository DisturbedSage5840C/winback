"""Where the Razorpay MCP server comes from, decided by configuration rather than by code.

Three modes, because the demo needs all three and switching between them must not be an
edit. ``off`` mounts nothing and is the default: the batch runs entirely on Winback's own
in-process tools, so a laptop with no Docker and no network still reproduces every number
in ``docs/EVALUATION.md``. ``remote`` mounts Razorpay's hosted server over HTTPS.
``local`` mounts the official Docker image over stdio, which is the mode the Day-8 failure
drill kills mid-batch to prove the run degrades instead of crashing.

**Credentials never appear on a command line.** The local invocation passes ``-e NAME``
with no value, which tells Docker to forward that variable from its own environment into
the container; the value is placed in the ``docker`` process's environment by the SDK
instead. So the secret is not in ``ps`` output and not in shell history. This is the same
rail the Day-1 probe used, and it is the reason the local mode is written this way rather
than the more obvious ``-e NAME=value``.

**The remote header is built here and nowhere else.** ``Authorization: Basic
base64(key_id:secret)`` — verified against the live server on probe 1, which answered
with ``serverInfo {name: "razorpay-mcp-server", version: "1.0.0"}``.

Nothing in this module decides what the agent may *do* with these tools. Mounting a
server and permitting a tool are separate acts: the allow-list in ``agent.tools`` and the
money gate in ``agent.gate`` are what stand between a mounted server and an action, and
they are deliberately not co-located with the transport.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import dataclass, field

import httpx
from claude_agent_sdk.types import McpHttpServerConfig, McpServerConfig, McpStdioServerConfig

from core.config import Settings, get_settings

REMOTE_URL = "https://mcp.razorpay.com/mcp"
IMAGE = "razorpay/mcp"

#: How long a transport gets to complete an MCP ``initialize`` before it is treated as
#: unreachable. Generous enough for a cold ``docker run`` on a laptop, short enough that
#: a dead lane is diagnosed in seconds rather than stalling a batch.
PROBE_TIMEOUT_SECONDS = 20.0

#: The key the Razorpay server is mounted under. Its tools therefore address as
#: ``mcp__razorpay__<tool>``, which is the spelling the allow-list and the money gate
#: both match on.
SERVER_NAME = "razorpay"


def basic_auth_header(key_id: str, secret: str) -> str:
    """``Basic base64(key:secret)``, the scheme the remote server accepts."""
    token = base64.b64encode(f"{key_id}:{secret}".encode()).decode()
    return f"Basic {token}"


def remote_server(key_id: str, secret: str) -> McpHttpServerConfig:
    return {
        "type": "http",
        "url": REMOTE_URL,
        "headers": {"Authorization": basic_auth_header(key_id, secret)},
    }


def local_server(
    key_id: str, secret: str, *, toolsets: str, read_only: bool
) -> McpStdioServerConfig:
    """The official image over stdio, with the secret passed by environment only.

    ``TOOLSETS`` and ``READ_ONLY`` are forwarded because probe 11 confirmed they work:
    the default surface is 41 tools, ``READ_ONLY=true`` trims it to 25, and
    ``TOOLSETS=orders`` to 5. Narrowing the surface at the container is defence in depth
    under the allow-list, not a replacement for it.

    **``TOOLSETS`` is space-separated.** Probe 11 only ever passed a single name, so the
    separator was never established, and the multi-value string that later landed in
    ``.env`` was comma-joined — which the image reads as one toolset name and dies on:
    ``failed to create toolsets: toolset orders,payments does not exist``. Commas are
    translated to spaces here rather than only fixed in ``.env``, because the failure is
    silent in the worst way: local mode simply never starts, the run falls down the lane
    ladder to remote, and everything keeps working well enough that nobody looks. See
    ``docs/WHAT_BROKE.md``, 3 Sep.
    """
    env = {"RAZORPAY_KEY_ID": key_id, "RAZORPAY_KEY_SECRET": secret}
    args = ["run", "--rm", "-i", "-e", "RAZORPAY_KEY_ID", "-e", "RAZORPAY_KEY_SECRET"]

    if toolsets:
        args += ["-e", "TOOLSETS"]
        env["TOOLSETS"] = " ".join(toolsets.replace(",", " ").split())
    if read_only:
        args += ["-e", "READ_ONLY"]
        env["READ_ONLY"] = "true"

    return {"type": "stdio", "command": "docker", "args": [*args, IMAGE], "env": env}


def razorpay_servers(settings: Settings | None = None) -> dict[str, McpServerConfig]:
    """The Razorpay server for the configured mode, or nothing at all.

    Returns an empty mapping in ``off`` mode and — deliberately — also when credentials
    are missing in a mode that needs them. A batch that silently ran without the server
    it was asked to mount would be a worse outcome than one that raised, so the caller
    is told which it got rather than left to infer it from behaviour.
    """
    settings = get_settings() if settings is None else settings

    if settings.mcp_mode == "off" or not settings.has_razorpay_credentials:
        return {}

    key_id, secret = settings.require_razorpay()

    if settings.mcp_mode == "remote":
        return {SERVER_NAME: remote_server(key_id, secret)}
    return {
        SERVER_NAME: local_server(
            key_id,
            secret,
            toolsets=settings.mcp_toolsets,
            read_only=settings.mcp_read_only,
        )
    }


def describe(settings: Settings | None = None) -> str:
    """One line for the run header, with no secret in it."""
    settings = get_settings() if settings is None else settings
    if settings.mcp_mode == "off":
        return "razorpay mcp: off (in-process tools only)"
    if not settings.has_razorpay_credentials:
        return f"razorpay mcp: {settings.mcp_mode} requested but no credentials — not mounted"
    if settings.mcp_mode == "remote":
        return f"razorpay mcp: remote {REMOTE_URL}"
    flags = f"toolsets={settings.mcp_toolsets or 'all'} read_only={settings.mcp_read_only}"
    return f"razorpay mcp: local docker {IMAGE} ({flags})"


# ─────────────────────────────── reachability ────────────────────────────────
#
# The Day-8 drill kills the local MCP mid-batch. For the run to *degrade* rather than
# fail, something has to be able to answer "is this transport actually alive" at a
# moment other than process start — so the two probes below speak the real MCP
# handshake to the real transport and return either None (alive) or one short sentence
# saying what went wrong. They deliberately do not raise: a probe that throws would
# turn a recoverable transport fault into the crash it exists to prevent.


def probe_remote(key_id: str, secret: str) -> str | None:
    """``initialize`` against the hosted server. None if it answered."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "winback", "version": "0.1.0"},
        },
    }
    try:
        response = httpx.post(
            REMOTE_URL,
            json=request,
            headers={
                "Authorization": basic_auth_header(key_id, secret),
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}"[:160]

    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    if "serverInfo" not in response.text:
        return "handshake returned no serverInfo"
    return None


def probe_local(key_id: str, secret: str, *, toolsets: str, read_only: bool) -> str | None:
    """``initialize`` against a freshly started container. None if it answered.

    Runs a throwaway container rather than inspecting ``docker ps``, because the
    question is not whether *a* container is up — the SDK starts its own per session —
    but whether one can still be started and talked to at all. That is the condition
    the drill actually breaks, and the only one worth reporting.

    Secrets travel by environment, never on the command line, exactly as in
    :func:`local_server`; the argv below carries variable *names* only.
    """
    config = local_server(key_id, secret, toolsets=toolsets, read_only=read_only)
    handshake = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "winback", "version": "0.1.0"},
                },
            }
        )
        + "\n"
    )

    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv + env var names only
            [config["command"], *config["args"]],
            input=handshake,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=os.environ | dict(config.get("env") or {}),
            check=False,
        )
    except FileNotFoundError:
        return "docker is not on PATH"
    except subprocess.TimeoutExpired:
        return f"no handshake within {PROBE_TIMEOUT_SECONDS:.0f}s"

    for line in completed.stdout.splitlines():
        if line.strip().startswith("{") and "serverInfo" in line:
            return None

    stderr = completed.stderr.strip().splitlines()
    detail = stderr[-1] if stderr else "no output"
    return f"exit {completed.returncode}: {detail}"[:160]


@dataclass
class Lane:
    """Which Razorpay transport is in force, and every step down it has taken.

    A lane is not the same thing as the configured mode. ``requested`` is what ``.env``
    asked for and never changes; ``mode`` is what the batch is actually running on, and
    moves down the ladder ``local → remote → off`` as transports are found dead. The
    ladder is one-way on purpose: a transport that failed once during a run is not
    promoted back, because a flapping lane would mean two invoices in the same batch
    were worked under different infrastructure with nothing in the record saying which.
    Every step is kept in ``demotions`` so the run report and the audit trail can say
    what was lost and why, rather than the run quietly becoming a different run.
    """

    requested: str
    mode: str
    servers: dict[str, McpServerConfig]
    demotions: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.demotions)

    def describe(self) -> str:
        base = {
            "off": "off (in-process tools only)",
            "remote": f"remote {REMOTE_URL}",
            "local": f"local docker {IMAGE}",
        }[self.mode]
        if not self.degraded:
            return f"razorpay mcp: {base}"
        why = "; ".join(self.demotions)
        return f"razorpay mcp: {base} — degraded from {self.requested} ({why})"


#: The rungs, in order. Falling off the bottom is ``off``, which always works: Winback's
#: own tools are in-process and the simulated adapter needs no network at all, so there
#: is no failure of Razorpay's infrastructure that can stop the batch from finishing.
LADDER = ("local", "remote", "off")


def _servers_for(mode: str, settings: Settings) -> dict[str, McpServerConfig]:
    if mode == "off" or not settings.has_razorpay_credentials:
        return {}
    key_id, secret = settings.require_razorpay()
    if mode == "remote":
        return {SERVER_NAME: remote_server(key_id, secret)}
    return {
        SERVER_NAME: local_server(
            key_id, secret, toolsets=settings.mcp_toolsets, read_only=settings.mcp_read_only
        )
    }


def open_lane(settings: Settings | None = None, *, start_at: str | None = None) -> Lane:
    """Descend the ladder from the requested mode until a transport answers.

    Called once before the batch and again whenever a transport looks dead mid-run.
    ``start_at`` forces the descent to begin below the current rung, which is how a
    mid-run demotion avoids re-probing the mode that just failed.
    """
    settings = get_settings() if settings is None else settings
    requested = settings.mcp_mode

    if requested == "off":
        return Lane(requested="off", mode="off", servers={})
    if not settings.has_razorpay_credentials:
        return Lane(
            requested=requested,
            mode="off",
            servers={},
            demotions=[f"{requested} requested but no credentials are configured"],
        )

    key_id, secret = settings.require_razorpay()
    begin = start_at or requested
    rungs = LADDER[LADDER.index(begin) :] if begin in LADDER else ("off",)

    demotions: list[str] = []
    for mode in rungs:
        if mode == "off":
            break
        if mode == "local":
            reason = probe_local(
                key_id, secret, toolsets=settings.mcp_toolsets, read_only=settings.mcp_read_only
            )
        else:
            reason = probe_remote(key_id, secret)
        if reason is None:
            return Lane(requested=requested, mode=mode, servers=_servers_for(mode, settings),
                        demotions=demotions)
        demotions.append(f"{mode} unreachable — {reason}")

    return Lane(requested=requested, mode="off", servers={}, demotions=demotions)

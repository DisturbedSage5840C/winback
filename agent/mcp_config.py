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

from claude_agent_sdk.types import McpHttpServerConfig, McpServerConfig, McpStdioServerConfig

from core.config import Settings, get_settings

REMOTE_URL = "https://mcp.razorpay.com/mcp"
IMAGE = "razorpay/mcp"

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
    """
    env = {"RAZORPAY_KEY_ID": key_id, "RAZORPAY_KEY_SECRET": secret}
    args = ["run", "--rm", "-i", "-e", "RAZORPAY_KEY_ID", "-e", "RAZORPAY_KEY_SECRET"]

    if toolsets:
        args += ["-e", "TOOLSETS"]
        env["TOOLSETS"] = toolsets
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

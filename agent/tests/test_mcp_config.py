"""Where the Razorpay server comes from, and where the secret does not go.

The load-bearing test in this file is
:func:`test_the_secret_never_appears_on_a_command_line`. Everything else is switching
logic; that one is the reason the local invocation is written the awkward way it is.
"""

from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from agent import mcp_config
from agent.mcp_config import (
    IMAGE,
    REMOTE_URL,
    SERVER_NAME,
    basic_auth_header,
    describe,
    local_server,
    open_lane,
    razorpay_servers,
    remote_server,
)
from core.config import Settings

KEY = "rzp_test_ABC123"
SECRET = "s3cr3t_value"  # noqa: S105 — a fake credential is the point of this file


@pytest.fixture
def settings() -> Settings:
    """A settings object with credentials, built without touching ``.env``."""
    from core.config import get_settings

    return replace(get_settings(), razorpay_key_id=KEY, razorpay_key_secret=SECRET)


# ----------------------------------------------------------------- credentials


def test_the_secret_never_appears_on_a_command_line():
    """``-e NAME`` with no value tells Docker to forward the variable from its own
    environment. ``-e NAME=value`` would put the key in ``ps`` output and in shell
    history, where anything on the machine could read it."""
    config = local_server(KEY, SECRET, toolsets="orders", read_only=True)
    flat = " ".join(config["args"])

    assert SECRET not in flat
    assert KEY not in flat
    assert "RAZORPAY_KEY_SECRET" in config["args"]
    assert config["env"]["RAZORPAY_KEY_SECRET"] == SECRET
    assert not any("=" in arg for arg in config["args"] if arg.startswith("RAZORPAY"))


def test_the_remote_header_is_basic_auth_over_the_key_pair():
    """Verified against the live server, which answered with
    ``serverInfo {name: "razorpay-mcp-server"}``."""
    header = basic_auth_header(KEY, SECRET)
    scheme, token = header.split(" ")
    assert scheme == "Basic"
    assert base64.b64decode(token).decode() == f"{KEY}:{SECRET}"


def test_the_remote_config_points_at_razorpays_own_endpoint():
    config = remote_server(KEY, SECRET)
    assert config["type"] == "http"
    assert config["url"] == REMOTE_URL
    assert config["headers"]["Authorization"].startswith("Basic ")


def test_the_local_config_runs_the_official_image_over_stdio():
    config = local_server(KEY, SECRET, toolsets="", read_only=False)
    assert config["type"] == "stdio"
    assert config["command"] == "docker"
    assert config["args"][-1] == IMAGE
    assert "--rm" in config["args"]


def test_narrowing_flags_are_forwarded_only_when_asked_for():
    """``READ_ONLY`` trims the surface from 41 tools to 25 and ``TOOLSETS=orders`` to 5.
    Defence in depth under the allow-list, not a replacement for it."""
    wide = local_server(KEY, SECRET, toolsets="", read_only=False)
    assert "TOOLSETS" not in wide["env"] and "READ_ONLY" not in wide["env"]

    narrow = local_server(KEY, SECRET, toolsets="orders", read_only=True)
    assert narrow["env"]["TOOLSETS"] == "orders"
    assert narrow["env"]["READ_ONLY"] == "true"


def test_comma_joined_toolsets_are_normalised_to_spaces():
    """A comma-joined value is not a near-miss the image tolerates — it is read as one
    toolset name, and the container exits with ``toolset orders,payments does not
    exist``. That failure is invisible in practice: local mode simply never starts and
    the lane ladder quietly carries the batch on remote. So the separator is repaired
    here rather than trusted to whatever ``.env`` a fresh clone happens to inherit."""
    for value in ("orders,payments,payment_links", "orders, payments ,payment_links"):
        config = local_server(KEY, SECRET, toolsets=value, read_only=False)
        assert config["env"]["TOOLSETS"] == "orders payments payment_links"


# ----------------------------------------------------------------- mode switch


def test_off_mode_mounts_nothing(settings):
    """The default. A laptop with no Docker and no network still reproduces every number
    in ``docs/EVALUATION.md``."""
    assert razorpay_servers(replace(settings, mcp_mode="off")) == {}


def test_remote_and_local_modes_mount_one_server_each(settings):
    for mode, expected in (("remote", "http"), ("local", "stdio")):
        servers = razorpay_servers(replace(settings, mcp_mode=mode))
        assert set(servers) == {SERVER_NAME}
        assert servers[SERVER_NAME]["type"] == expected


def test_a_mode_that_needs_credentials_mounts_nothing_without_them(settings):
    """Silently running without the server it was asked to mount would be a worse
    outcome than raising, so the caller is told which it got — see ``describe``."""
    bare = replace(settings, razorpay_key_id=None, razorpay_key_secret=None)
    assert razorpay_servers(replace(bare, mcp_mode="remote")) == {}


# ----------------------------------------------------------------- the run header


def test_the_run_header_never_carries_a_secret(settings):
    for mode in ("off", "remote", "local"):
        line = describe(replace(settings, mcp_mode=mode))
        assert SECRET not in line
        assert "Basic " not in line


def test_the_run_header_says_when_a_requested_server_was_not_mounted(settings):
    bare = replace(settings, razorpay_key_id=None, razorpay_key_secret=None, mcp_mode="remote")
    assert "not mounted" in describe(bare)


# ------------------------------------------------------------- the fallback ladder
#
# The Day-8 drill: the local MCP dies mid-batch and the run has to keep going. These
# tests drive the ladder with the probes stubbed, so they assert the descent logic
# without needing Docker, a network, or a real credential.


def _stub_probes(monkeypatch, *, local: str | None, remote: str | None) -> None:
    monkeypatch.setattr(mcp_config, "probe_local", lambda *a, **k: local)
    monkeypatch.setattr(mcp_config, "probe_remote", lambda *a, **k: remote)


def test_a_healthy_local_lane_stays_local(monkeypatch, settings):
    _stub_probes(monkeypatch, local=None, remote=None)
    lane = open_lane(replace(settings, mcp_mode="local"))

    assert lane.mode == "local"
    assert not lane.degraded
    assert SERVER_NAME in lane.servers


def test_a_dead_local_lane_falls_to_remote(monkeypatch, settings):
    """The drill's headline behaviour: `docker stop` must cost the run a transport, not
    the batch."""
    _stub_probes(monkeypatch, local="exit 125: no such container", remote=None)
    lane = open_lane(replace(settings, mcp_mode="local"))

    assert lane.mode == "remote"
    assert lane.degraded
    assert lane.servers[SERVER_NAME]["url"] == REMOTE_URL
    assert "local unreachable" in lane.demotions[0]


def test_both_transports_dead_still_leaves_a_runnable_lane(monkeypatch, settings):
    """`off` is the bottom of the ladder and always works: Winback's own tools are
    in-process and the simulated adapter needs no network, so no failure of Razorpay's
    infrastructure can stop the batch from finishing."""
    _stub_probes(monkeypatch, local="docker is not on PATH", remote="HTTP 503")
    lane = open_lane(replace(settings, mcp_mode="local"))

    assert lane.mode == "off"
    assert lane.servers == {}
    assert len(lane.demotions) == 2


def test_start_at_skips_the_rung_that_just_failed(monkeypatch, settings):
    """A mid-run demotion knows local is dead — re-probing it would spend twenty seconds
    re-learning that on every subsequent invoice."""
    calls: list[str] = []
    monkeypatch.setattr(
        mcp_config, "probe_local", lambda *a, **k: calls.append("local") or "dead"
    )
    monkeypatch.setattr(mcp_config, "probe_remote", lambda *a, **k: calls.append("remote"))

    lane = open_lane(replace(settings, mcp_mode="local"), start_at="remote")

    assert calls == ["remote"]
    assert lane.mode == "remote"


def test_the_lane_description_names_what_was_lost(monkeypatch, settings):
    """A degraded run is still a complete run, but it is not the same run — and the
    header has to say so rather than let a full invoice count imply the infrastructure
    held."""
    _stub_probes(monkeypatch, local="exit 125: container is not running", remote=None)
    line = open_lane(replace(settings, mcp_mode="local")).describe()

    assert "degraded from local" in line
    assert SECRET not in line

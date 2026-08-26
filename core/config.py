"""Configuration, loaded once from the environment.

Two rules enforced here rather than at the call site, because both are the kind of
mistake you only make once:

  * a Razorpay key id that does not start with ``rzp_test_`` is refused outright;
  * ``EXECUTION_MODE`` is read explicitly and never inferred from whether
    credentials happen to be present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

ExecutionMode = Literal["simulated", "live"]
McpMode = Literal["off", "remote", "local"]


class ConfigError(RuntimeError):
    """Raised for a configuration state that must not be papered over."""


@dataclass(frozen=True, slots=True)
class Settings:
    db_url: str
    db_url_readonly: str
    db_url_owner: str

    razorpay_key_id: str | None
    razorpay_key_secret: str | None
    mcp_mode: McpMode
    mcp_toolsets: str
    mcp_read_only: bool

    execution_mode: ExecutionMode
    live_call_budget: int

    agent_model: str
    explainer_model: str
    max_turns_per_item: int

    seed: int

    @property
    def has_razorpay_credentials(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    def require_razorpay(self) -> tuple[str, str]:
        """Credentials for the live lane, or a clear failure. Never a silent fallback."""
        if not self.has_razorpay_credentials:
            raise ConfigError(
                "Live execution requested but RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET are "
                "unset. Set them in .env, or run with WINBACK_EXECUTION_MODE=simulated."
            )
        assert self.razorpay_key_id and self.razorpay_key_secret  # narrowed above
        return self.razorpay_key_id, self.razorpay_key_secret


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


def _one_of(name: str, default: str, allowed: set[str]) -> str:
    value = _env(name, default) or default
    if value not in allowed:
        raise ConfigError(f"{name}={value!r} must be one of {sorted(allowed)}")
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")

    key_id = _env("RAZORPAY_KEY_ID") or None
    # A placeholder in .env.example should read as "unset", not as a broken key.
    if key_id and key_id.endswith("xxxxxxxxxxxxxx"):
        key_id = None
    if key_id and not key_id.startswith("rzp_test_"):
        raise ConfigError(
            f"RAZORPAY_KEY_ID={key_id[:12]}... is not a test key. Winback refuses to "
            "hold live credentials: every action it takes moves money."
        )

    default_db = "postgresql://winback_agent:winback_agent_dev@localhost:55432/winback"
    execution_mode = _one_of("WINBACK_EXECUTION_MODE", "simulated", {"simulated", "live"})
    mcp_mode = _one_of("RAZORPAY_MCP_MODE", "off", {"off", "remote", "local"})

    return Settings(
        db_url=_env("WINBACK_DB_URL", default_db) or default_db,
        db_url_readonly=_env(
            "WINBACK_DB_URL_READONLY",
            "postgresql://winback_reader:winback_reader_dev@localhost:55432/winback",
        )
        or "",
        db_url_owner=_env(
            "WINBACK_DB_URL_OWNER",
            "postgresql://winback_owner:winback_dev@localhost:55432/winback",
        )
        or "",
        razorpay_key_id=key_id,
        razorpay_key_secret=_env("RAZORPAY_KEY_SECRET") or None,
        mcp_mode=mcp_mode,  # type: ignore[arg-type]
        mcp_toolsets=_env("RAZORPAY_MCP_TOOLSETS", "orders,payments,payment-links") or "",
        mcp_read_only=_env_bool("RAZORPAY_MCP_READ_ONLY", False),
        execution_mode=execution_mode,  # type: ignore[arg-type]
        live_call_budget=_env_int("WINBACK_LIVE_CALL_BUDGET", 50),
        agent_model=_env("WINBACK_AGENT_MODEL", "claude-sonnet-5") or "claude-sonnet-5",
        explainer_model=_env("WINBACK_EXPLAINER_MODEL", "claude-opus-5") or "claude-opus-5",
        max_turns_per_item=_env_int("WINBACK_MAX_TURNS_PER_ITEM", 6),
        seed=_env_int("WINBACK_SEED", 20260905),
    )

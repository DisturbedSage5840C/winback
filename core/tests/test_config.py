"""Configuration parsing and its two loudest refusals.

``core.config`` guards the two mistakes a misconfigured deploy actually makes: a live
key slipping into a "test" project, and a database URL silently becoming ``""`` and
handing psycopg to libpq's local defaults instead. Neither has a test today, despite
being the loudest safety claim in the README. These do not touch Postgres, so they
carry no ``db`` marker and run everywhere.

``get_settings()`` is ``@lru_cache(maxsize=1)``, so every test here restores both the
environment and the cache on exit — leaving either dirty would make a later test's
``get_settings()`` see this test's environment instead of its own.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import core.config as config_module
from core.config import ConfigError, get_settings

# Vars get_settings() reads. Cleared before each test so no developer's real .env or
# shell environment leaks into an assertion about defaults.
_ENV_VARS = (
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "WINBACK_DB_URL",
    "WINBACK_DB_URL_READONLY",
    "WINBACK_DB_URL_OWNER",
    "WINBACK_EXECUTION_MODE",
    "RAZORPAY_MCP_MODE",
    "RAZORPAY_MCP_TOOLSETS",
    "RAZORPAY_MCP_READ_ONLY",
    "WINBACK_LIVE_CALL_BUDGET",
    "WINBACK_AGENT_MODEL",
    "WINBACK_EXPLAINER_MODEL",
    "WINBACK_MAX_TURNS_PER_ITEM",
    "WINBACK_AGENT_TIMEOUT_SECONDS",
    "WINBACK_EXPLAINER_TIMEOUT_SECONDS",
    "WINBACK_SEED",
)


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A bare environment and a cold cache, every test, in and out.

    ``load_dotenv`` inside ``get_settings()`` would otherwise refill anything cleared
    here right back out of the repo's own ``.env`` — which sets a real
    ``RAZORPAY_KEY_ID``, exactly the kind of value a "credentials absent" test needs
    to not see. Stubbed to a no-op so this file's environment is the only environment
    ``get_settings()`` ever reads.
    """
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_a_live_key_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one credential check this project cannot afford to get wrong."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_abc123def456")
    with pytest.raises(ConfigError, match="not a test key"):
        get_settings()


def test_a_test_key_id_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc123")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "shh")
    settings = get_settings()
    assert settings.razorpay_key_id == "rzp_test_abc123"
    assert settings.has_razorpay_credentials


def test_the_env_example_placeholder_reads_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """``rzp_test_xxxxxxxxxxxxxx`` in ``.env.example`` must never look configured."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_xxxxxxxxxxxxxx")
    settings = get_settings()
    assert settings.razorpay_key_id is None
    assert not settings.has_razorpay_credentials


def test_no_credentials_means_require_razorpay_fails_loudly() -> None:
    settings = get_settings()
    with pytest.raises(ConfigError, match="RAZORPAY_KEY_ID"):
        settings.require_razorpay()


def test_db_url_falls_back_to_the_local_default_when_unset() -> None:
    settings = get_settings()
    assert settings.db_url.startswith("postgresql://winback_agent:")


def test_readonly_db_url_falls_back_to_the_local_default_when_unset() -> None:
    settings = get_settings()
    assert settings.db_url_readonly.startswith("postgresql://winback_reader:")


def test_owner_db_url_falls_back_to_the_local_default_when_unset() -> None:
    settings = get_settings()
    assert settings.db_url_owner.startswith("postgresql://winback_owner:")


def test_a_declared_but_unfilled_readonly_url_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render's ``sync: false``: the var is present, just empty. Must not become "".

    ``psycopg.connect("")`` does not error — it falls through to libpq's own
    defaults, which is a silent wrong-database connection, not a crash anywhere near
    this line. That is exactly the failure this test exists to close off.
    """
    monkeypatch.setenv("WINBACK_DB_URL_READONLY", "")
    with pytest.raises(ConfigError, match=r"WINBACK_DB_URL_READONLY.*set but empty"):
        get_settings()


def test_a_declared_but_unfilled_owner_url_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINBACK_DB_URL_OWNER", "")
    with pytest.raises(ConfigError, match=r"WINBACK_DB_URL_OWNER.*set but empty"):
        get_settings()


def test_an_unset_readonly_url_is_not_the_same_as_an_empty_one() -> None:
    """Unset means "use the local default"; set-and-empty means "fail". Different."""
    settings = get_settings()
    assert settings.db_url_readonly != ""


def test_an_invalid_execution_mode_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINBACK_EXECUTION_MODE", "production")
    with pytest.raises(ConfigError, match="WINBACK_EXECUTION_MODE"):
        get_settings()


def test_an_invalid_mcp_mode_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAZORPAY_MCP_MODE", "everywhere")
    with pytest.raises(ConfigError, match="RAZORPAY_MCP_MODE"):
        get_settings()


def test_a_non_integer_timeout_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINBACK_MAX_TURNS_PER_ITEM", "six")
    with pytest.raises(ConfigError, match="WINBACK_MAX_TURNS_PER_ITEM"):
        get_settings()


def test_the_key_secret_never_appears_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """``repr=False`` on the field -- a stray ``print(settings)`` cannot leak it."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc123")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "definitely-a-secret-value")
    settings = get_settings()
    assert "definitely-a-secret-value" not in repr(settings)


def test_get_settings_is_cached_across_calls() -> None:
    """Same process, same env, same object -- the DSNs pools in ``core.db`` key on."""
    assert get_settings() is get_settings()

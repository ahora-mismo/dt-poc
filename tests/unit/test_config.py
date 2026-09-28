"""Tests for :mod:`dt_agent.config` (D-29, D-36, D-46, D-59, D-65, D-19).

Every test builds the environment explicitly (monkeypatch or an explicit
mapping) and never reads the real `.env` file (D-53).
"""

from pathlib import Path

import pytest

from dt_agent.config import ConfigurationError, Settings, load_settings

# Every variable load_settings() ever looks at. Reset before each scenario so
# a real ambient environment variable can never leak into a test.
_ALL_VARS = [
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "LLM_PROVIDER",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LLM_MODEL",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_DEPLOYMENT",
    "AGENT_SENDER_NAME",
    "MANAGER_EMAIL",
    "APPROVAL_SIGNING_KEY",
    "APPROVAL_EXPIRY_BUSINESS_DAYS",
    "SALES_DECLINE_REQUIRES_APPROVAL",
    "INTERNAL_DOMAINS",
    "SPAM_ALLOWLIST_SENDERS",
    "SPAM_ALLOWLIST_DOMAINS",
    "DEFAULT_TIMEZONE",
    "BUSINESS_HOURS_START",
    "BUSINESS_HOURS_END",
    "DEFAULT_MEETING_MINUTES",
    "SCHEDULING_HORIZON_BUSINESS_DAYS",
    "MEETING_MIN_LEAD_MINUTES",
    "MEETING_MAX_OPTIONS",
    "POLL_INTERVAL_SECONDS",
    "ACTION_DELAY_MS",
    "WORKER_COUNT",
    "MAX_ATTEMPTS",
    "LLM_MAX_INPUT_CHARS",
    "DB_PATH",
    "STARTUP_BACKFILL_MESSAGES",
    "RETENTION_DAYS",
    "MIN_LOG_LEVEL",
    "LOG_COLOR",
    "ENABLE_MCP",
    "MCP_PORT",
    "MCP_HOST",
]

# A minimal, fully valid environment (provider=anthropic keeps it short: no
# Gemini/Azure vars, no MCP_PORT/MCP_HOST since ENABLE_MCP=false).
_BASE_ENV: dict[str, str] = {
    "GOOGLE_CLIENT_ID": "client-id",
    "GOOGLE_CLIENT_SECRET": "client-secret",
    "GOOGLE_REFRESH_TOKEN": "refresh-token",
    "LLM_PROVIDER": "anthropic",
    "ANTHROPIC_API_KEY": "anthropic-key",
    "LLM_MODEL": "claude-haiku-4-5",
    "AGENT_SENDER_NAME": "Alex's Assistant",
    "MANAGER_EMAIL": "",
    "APPROVAL_SIGNING_KEY": "signing-key",
    "APPROVAL_EXPIRY_BUSINESS_DAYS": "3",
    "SALES_DECLINE_REQUIRES_APPROVAL": "false",
    "INTERNAL_DOMAINS": "",
    "SPAM_ALLOWLIST_SENDERS": "",
    "SPAM_ALLOWLIST_DOMAINS": "",
    "DEFAULT_TIMEZONE": "Europe/Bucharest",
    "BUSINESS_HOURS_START": "9",
    "BUSINESS_HOURS_END": "18",
    "DEFAULT_MEETING_MINUTES": "30",
    "SCHEDULING_HORIZON_BUSINESS_DAYS": "5",
    "MEETING_MIN_LEAD_MINUTES": "25",
    "MEETING_MAX_OPTIONS": "3",
    "POLL_INTERVAL_SECONDS": "15",
    "ACTION_DELAY_MS": "1000",
    "WORKER_COUNT": "2",
    "MAX_ATTEMPTS": "3",
    "LLM_MAX_INPUT_CHARS": "20000",
    "DB_PATH": "data/agent.db",
    "STARTUP_BACKFILL_MESSAGES": "20",
    "RETENTION_DAYS": "30",
    "MIN_LOG_LEVEL": "INFO",
    "LOG_COLOR": "auto",
    "ENABLE_MCP": "false",
}


def _apply_env(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str | None]) -> None:
    """Reset every known var, then set base + overrides (None = absent)."""
    for name in _ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    merged = {**_BASE_ENV, **overrides}
    for name, value in merged.items():
        if value is not None:
            monkeypatch.setenv(name, value)


def test_settings_missing_vars_reported_together(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, {"LLM_PROVIDER": None, "DB_PATH": None})

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "LLM_PROVIDER" in message
    assert "DB_PATH" in message


def test_settings_poll_interval_below_minimum_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, {"POLL_INTERVAL_SECONDS": "3"})

    with pytest.raises(ConfigurationError, match="POLL_INTERVAL_SECONDS"):
        load_settings()


def test_settings_provider_specific_vars_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(
        monkeypatch,
        {
            "LLM_PROVIDER": "azure",
            "ANTHROPIC_API_KEY": None,
            "LLM_MODEL": None,
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
            "AZURE_OPENAI_API_KEY": "azure-key",
            "AZURE_OPENAI_API_VERSION": "2024-10-21",
            # AZURE_OPENAI_DEPLOYMENT intentionally left unset.
        },
    )
    with pytest.raises(ConfigurationError, match="AZURE_OPENAI_DEPLOYMENT"):
        load_settings()

    _apply_env(monkeypatch, {"LLM_PROVIDER": "anthropic", "LLM_MODEL": None})
    with pytest.raises(ConfigurationError, match="LLM_MODEL"):
        load_settings()

    # anthropic with no Gemini/Azure vars set at all is valid.
    _apply_env(monkeypatch, {"LLM_PROVIDER": "anthropic"})
    settings = load_settings()
    assert settings.LLM_PROVIDER == "anthropic"


def test_settings_mcp_port_required_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, {"ENABLE_MCP": "true"})
    with pytest.raises(ConfigurationError, match="MCP_PORT"):
        load_settings()

    _apply_env(monkeypatch, {"ENABLE_MCP": "false"})
    settings = load_settings()
    assert settings.ENABLE_MCP is False
    assert settings.MCP_PORT is None


def test_config_mcp_host_required_when_mcp_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, {"ENABLE_MCP": "true", "MCP_PORT": "8765"})

    with pytest.raises(ConfigurationError, match="MCP_HOST"):
        load_settings()


def test_config_mcp_host_not_required_when_mcp_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, {"ENABLE_MCP": "false"})

    settings = load_settings()

    assert settings.MCP_HOST is None


def test_settings_empty_values_allowed_but_presence_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, {"MANAGER_EMAIL": ""})
    settings = load_settings()
    assert settings.approver_address("mailbox@example.com") == "mailbox@example.com"

    _apply_env(monkeypatch, {"INTERNAL_DOMAINS": None})
    with pytest.raises(ConfigurationError, match="INTERNAL_DOMAINS"):
        load_settings()

    _apply_env(monkeypatch, {"INTERNAL_DOMAINS": ""})
    settings = load_settings()
    assert settings.INTERNAL_DOMAINS == []


def test_settings_lists_normalised() -> None:
    env = {**_BASE_ENV, "INTERNAL_DOMAINS": " Example.com, b.org "}

    settings = load_settings(env)

    assert settings.INTERNAL_DOMAINS == ["example.com", "b.org"]


def test_settings_field_and_conditional_errors_reported_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain field error must not suppress a conditional one (D-29).

    ``_conditional_issues`` used to run only after ``Settings()`` built
    successfully, so an unrelated field error (POLL_INTERVAL_SECONDS here)
    silently hid the ENABLE_MCP/MCP_PORT problem until fixed separately.
    """
    _apply_env(monkeypatch, {"POLL_INTERVAL_SECONDS": "3", "ENABLE_MCP": "true"})

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "POLL_INTERVAL_SECONDS" in message
    assert "MCP_PORT" in message


def test_settings_empty_string_rejected_for_always_required_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, {"APPROVAL_SIGNING_KEY": ""})

    with pytest.raises(ConfigurationError, match="APPROVAL_SIGNING_KEY"):
        load_settings()


def test_settings_invalid_manager_email_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, {"MANAGER_EMAIL": "not-an-address"})

    with pytest.raises(ConfigurationError, match="MANAGER_EMAIL"):
        load_settings()


def test_settings_approver_address_reflects_manager_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, {"MANAGER_EMAIL": "manager@external.com"})
    settings = load_settings()
    assert settings.approver_address("mailbox@example.com") == "manager@external.com"
    assert settings.is_manager_external("mailbox@example.com") is True

    _apply_env(monkeypatch, {"MANAGER_EMAIL": "Mailbox@Example.com"})
    settings = load_settings()
    assert settings.approver_address("mailbox@example.com") == "mailbox@example.com"
    assert settings.is_manager_external("mailbox@example.com") is False


def test_env_sample_lists_every_settings_field() -> None:
    env_sample = Path(__file__).resolve().parents[2] / ".env.sample"
    text = env_sample.read_text()
    declared = {
        line.split("=", 1)[0]
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    missing = set(Settings.model_fields) - declared

    assert not missing, f"Settings fields missing from .env.sample: {sorted(missing)}"

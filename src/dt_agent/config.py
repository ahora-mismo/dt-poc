"""Application configuration (D-29, D-36, D-46, D-59, D-65).

Configuration is read exactly once, at startup, by :func:`load_settings`,
which builds the single immutable :class:`Settings` object. Every other
component reads configuration only from that object, never from the
environment directly (D-29).

There are no defaults for any variable (D-65): a variable is either always
required, required for the chosen ``LLM_PROVIDER``, required only when
``ENABLE_MCP=true``, or required-but-may-be-empty (``MANAGER_EMAIL``,
``INTERNAL_DOMAINS``, ``SPAM_ALLOWLIST_SENDERS``, ``SPAM_ALLOWLIST_DOMAINS``).
If any required variable is missing or invalid, :func:`load_settings` raises
one :class:`ConfigurationError` naming every offending variable and the
reason; it never stops at the first problem.

Configuration is read from the process environment only: no ``.env`` file is
configured here (D-53). ``.env`` is loaded by tooling (uv, the IDE, Docker),
never by the app itself.
"""

import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# Permissive on purpose (D-59): matches an IPv4 address, a bare IPv6
# address, or a hostname; rejects only empty/whitespace.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:]+$")
_TRUE_STRINGS = frozenset({"1", "true", "t", "yes", "y", "on"})

LlmProvider = Literal["gemini", "anthropic", "azure"]
LogLevel = Literal["DEBUG", "DEV", "INFO", "WARNING", "ERROR"]
LogColor = Literal["auto", "always", "never"]

# Comma-separated env vars that are normalised into a trimmed, lowercased
# list[str] with empty entries dropped.
_CSV_FIELDS = ("INTERNAL_DOMAINS", "SPAM_ALLOWLIST_SENDERS", "SPAM_ALLOWLIST_DOMAINS")

# Always-required string/secret/path vars: presence alone isn't enough, an
# empty value (e.g. `VAR=` in `.env`) is treated the same as absent (D-65).
_NON_EMPTY_FIELDS = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "AGENT_SENDER_NAME",
    "APPROVAL_SIGNING_KEY",
    "DB_PATH",
)


class ConfigurationError(RuntimeError):
    """Raised by :func:`load_settings` when configuration is missing/invalid.

    The message lists every offending variable, one per line, with the
    reason (D-29): the app never stops at the first problem.
    """


def _parse_csv(value: object) -> list[str]:
    """Split a comma-separated string, trim, lowercase, drop empty entries."""
    if isinstance(value, list):
        items: list[str] = value
    else:
        items = str(value).split(",")
    return [item.strip().lower() for item in items if item.strip()]


class Settings(BaseSettings):
    """Validated application configuration, built once at startup (D-29).

    Construct only through :func:`load_settings`, which also runs the
    conditional checks (provider-specific variables, MCP host/port, business
    hours ordering) that depend on more than one variable at once and so
    cannot run as plain field validation (see module docstring).
    """

    model_config = SettingsConfigDict(frozen=True)

    # -- Google (D-18) -----------------------------------------------------
    GOOGLE_CLIENT_ID: SecretStr
    GOOGLE_CLIENT_SECRET: SecretStr
    GOOGLE_REFRESH_TOKEN: SecretStr

    # -- LLM (D-36) ----------------------------------------------------------
    LLM_PROVIDER: LlmProvider
    GEMINI_API_KEY: SecretStr | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None
    LLM_MODEL: str | None = None
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_KEY: SecretStr | None = None
    AZURE_OPENAI_API_VERSION: str | None = None
    AZURE_OPENAI_DEPLOYMENT: str | None = None

    # -- Approval / sending (D-10, D-23, D-33, D-38, D-46) --------------------
    AGENT_SENDER_NAME: str
    MANAGER_EMAIL: str
    APPROVAL_SIGNING_KEY: SecretStr
    APPROVAL_EXPIRY_BUSINESS_DAYS: int
    SALES_DECLINE_REQUIRES_APPROVAL: bool

    # -- Sender rules (D-42, D-61) --------------------------------------------
    INTERNAL_DOMAINS: Annotated[list[str], NoDecode]
    SPAM_ALLOWLIST_SENDERS: Annotated[list[str], NoDecode]
    SPAM_ALLOWLIST_DOMAINS: Annotated[list[str], NoDecode]

    # -- Calendar (D-1, ADR-4) -------------------------------------------------
    DEFAULT_TIMEZONE: str
    BUSINESS_HOURS_START: int
    BUSINESS_HOURS_END: int
    DEFAULT_MEETING_MINUTES: int
    SCHEDULING_HORIZON_BUSINESS_DAYS: int
    MEETING_MIN_LEAD_MINUTES: int
    MEETING_MAX_OPTIONS: int

    # -- Runtime (D-19, D-20, D-30, D-43, D-56) -------------------------------
    POLL_INTERVAL_SECONDS: int
    ACTION_DELAY_MS: int
    WORKER_COUNT: int
    MAX_ATTEMPTS: int
    LLM_MAX_INPUT_CHARS: int

    # -- State (D-6, D-7, D-28) ------------------------------------------------
    DB_PATH: Path
    STARTUP_BACKFILL_MESSAGES: int
    RETENTION_DAYS: int

    # -- Logging (D-58) --------------------------------------------------------
    MIN_LOG_LEVEL: LogLevel
    LOG_COLOR: LogColor

    # -- MCP (D-59, D-65) ----------------------------------------------------
    ENABLE_MCP: bool
    MCP_PORT: int | None = None
    MCP_HOST: str | None = None

    @field_validator(*_CSV_FIELDS, mode="before")
    @classmethod
    def _normalise_csv(cls, value: object) -> list[str]:
        return _parse_csv(value)

    @field_validator(*_NON_EMPTY_FIELDS, mode="before")
    @classmethod
    def _check_non_empty(cls, value: object) -> object:
        if value == "":
            raise ValueError("required, but empty")
        return value

    @field_validator("MANAGER_EMAIL")
    @classmethod
    def _check_manager_email(cls, value: str) -> str:
        if value and not _EMAIL_RE.match(value):
            raise ValueError("must be empty or a valid email address")
        return value

    @field_validator("DEFAULT_TIMEZONE")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"not a valid IANA timezone: {value!r}") from exc
        return value

    @field_validator("BUSINESS_HOURS_START", "BUSINESS_HOURS_END")
    @classmethod
    def _check_business_hour(cls, value: int, info: ValidationInfo) -> int:
        if not 0 <= value <= 24:
            raise ValueError(f"{info.field_name} must be between 0 and 24")
        return value

    @field_validator("POLL_INTERVAL_SECONDS")
    @classmethod
    def _check_poll_interval(cls, value: int) -> int:
        if value < 5:
            raise ValueError("must be >= 5")
        return value

    @field_validator("ACTION_DELAY_MS", "STARTUP_BACKFILL_MESSAGES")
    @classmethod
    def _check_non_negative(cls, value: int, info: ValidationInfo) -> int:
        if value < 0:
            raise ValueError(f"{info.field_name} must be >= 0")
        return value

    @field_validator("WORKER_COUNT", "MAX_ATTEMPTS", "RETENTION_DAYS")
    @classmethod
    def _check_at_least_one(cls, value: int, info: ValidationInfo) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1")
        return value

    @field_validator(
        "LLM_MAX_INPUT_CHARS",
        "DEFAULT_MEETING_MINUTES",
        "SCHEDULING_HORIZON_BUSINESS_DAYS",
        "MEETING_MIN_LEAD_MINUTES",
        "MEETING_MAX_OPTIONS",
    )
    @classmethod
    def _check_positive(cls, value: int, info: ValidationInfo) -> int:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be > 0")
        return value

    @field_validator("MCP_PORT")
    @classmethod
    def _check_mcp_port_range(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 65535:
            raise ValueError("must be between 1 and 65535")
        return value

    @field_validator("MCP_HOST")
    @classmethod
    def _check_mcp_host(cls, value: str | None) -> str | None:
        if value is not None and not _HOST_RE.match(value):
            raise ValueError("must be a non-empty host string (IP address or hostname)")
        return value

    def approver_address(self, mailbox_address: str) -> str:
        """Approver/notification recipient (D-46).

        ``MANAGER_EMAIL`` when it is set to a different address than the
        mailbox; the mailbox address itself otherwise (empty or equal).
        """
        if self.MANAGER_EMAIL and self.MANAGER_EMAIL.lower() != mailbox_address.lower():
            return self.MANAGER_EMAIL
        return mailbox_address

    def is_manager_external(self, mailbox_address: str) -> bool:
        """True when approvals go to an address other than the mailbox (D-46)."""
        return self.approver_address(mailbox_address).lower() != mailbox_address.lower()


def _issue_lines(exc: ValidationError) -> list[str]:
    """One "NAME: reason" line per pydantic field error."""
    lines: list[str] = []
    for error in exc.errors():
        name = ".".join(str(part) for part in error["loc"])
        if error["type"] == "missing":
            reason = "required, but not set"
        else:
            reason = error["msg"].removeprefix("Value error, ")
        lines.append(f"{name}: {reason}")
    return lines


def _is_blank(raw: Mapping[str, str], name: str) -> bool:
    return raw.get(name, "").strip() == ""


def _is_true(raw: Mapping[str, str], name: str) -> bool:
    return raw.get(name, "").strip().lower() in _TRUE_STRINGS


def _conditional_issues(raw: Mapping[str, str]) -> list[str]:
    """Checks that depend on more than one raw variable at once (D-29).

    Read directly from the raw environment mapping, independently of
    whether :class:`Settings` itself validates successfully, so that these
    issues are always reported together with any plain field errors
    (never silently skipped because some unrelated field also failed).
    """
    issues: list[str] = []

    provider = raw.get("LLM_PROVIDER", "").strip().lower()
    if provider == "gemini":
        for name in ("GEMINI_API_KEY", "LLM_MODEL"):
            if _is_blank(raw, name):
                issues.append(f"{name}: required when LLM_PROVIDER=gemini")
    elif provider == "anthropic":
        for name in ("ANTHROPIC_API_KEY", "LLM_MODEL"):
            if _is_blank(raw, name):
                issues.append(f"{name}: required when LLM_PROVIDER=anthropic")
    elif provider == "azure":
        for name in (
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_API_VERSION",
            "AZURE_OPENAI_DEPLOYMENT",
        ):
            if _is_blank(raw, name):
                issues.append(f"{name}: required when LLM_PROVIDER=azure")
    # An unrecognised/absent provider is reported by the Settings field
    # validation itself (LLM_PROVIDER is a Literal), so nothing to add here.

    if _is_true(raw, "ENABLE_MCP") and _is_blank(raw, "MCP_PORT"):
        issues.append("MCP_PORT: required when ENABLE_MCP=true")
    if _is_true(raw, "ENABLE_MCP") and _is_blank(raw, "MCP_HOST"):
        issues.append("MCP_HOST: required when ENABLE_MCP=true")

    try:
        start = int(raw["BUSINESS_HOURS_START"])
        end = int(raw["BUSINESS_HOURS_END"])
    except KeyError, ValueError:
        pass  # absent/non-numeric is already reported by field validation
    else:
        if start >= end:
            issues.append(
                "BUSINESS_HOURS_START/BUSINESS_HOURS_END: "
                "BUSINESS_HOURS_START must be < BUSINESS_HOURS_END"
            )

    return issues


def _format_error(lines: list[str]) -> str:
    body = "\n".join(f"- {line}" for line in lines)
    return f"Invalid configuration:\n{body}"


@contextmanager
def _temp_environ(env: Mapping[str, str]) -> Iterator[None]:
    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read the process environment once into a validated :class:`Settings`.

    With ``env`` given, that mapping is used instead of the real process
    environment (for tests only); the real environment is restored
    immediately afterwards. Raises :class:`ConfigurationError` naming every
    missing/invalid variable together if any is wrong: plain field errors
    (missing/mistyped variables) and conditional errors (provider-specific
    variables, MCP host/port, business-hours ordering) are always collected and
    reported together, never one at the expense of the other (D-29).
    """
    raw: Mapping[str, str] = env if env is not None else os.environ
    issues = _conditional_issues(raw)

    try:
        if env is None:
            settings = Settings()  # type: ignore[call-arg]  # fields come from the environment
        else:
            with _temp_environ(env):
                settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigurationError(_format_error(_issue_lines(exc) + issues)) from exc

    if issues:
        raise ConfigurationError(_format_error(issues))
    return settings

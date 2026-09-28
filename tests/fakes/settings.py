"""Test helpers: a complete, valid `Settings` and a `PipelineDeps` built from fakes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dt_agent.config import Settings, load_settings
from dt_agent.domain.clock import Clock, FixedClock
from dt_agent.pipeline.deps import PipelineDeps
from dt_agent.ports import MessageStore
from tests.fakes.calendar import FakeCalendar
from tests.fakes.llm import (
    FakeAcknowledgementDrafter,
    FakeClassifier,
    FakeCourtesyDrafter,
    FakeMeetingExtractor,
    FakeReplyDrafter,
)
from tests.fakes.mail import FakeMail

MAILBOX = "mailbox@example.com"
SIGNING_KEY = "test-signing-key-0123456789abcdef0123456789abcdef"

BASE_ENV: dict[str, str] = {
    "GOOGLE_CLIENT_ID": "client-id",
    "GOOGLE_CLIENT_SECRET": "client-secret",
    "GOOGLE_REFRESH_TOKEN": "refresh-token",
    "LLM_PROVIDER": "anthropic",
    "ANTHROPIC_API_KEY": "test-key",
    "LLM_MODEL": "claude-haiku-4-5-20251001",
    "AGENT_SENDER_NAME": "Test Assistant",
    "MANAGER_EMAIL": "",
    "APPROVAL_SIGNING_KEY": SIGNING_KEY,
    "APPROVAL_EXPIRY_BUSINESS_DAYS": "3",
    "SALES_DECLINE_REQUIRES_APPROVAL": "false",
    "INTERNAL_DOMAINS": "acme.com",
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
    "LOG_COLOR": "never",
    "ENABLE_MCP": "false",
}


def make_settings(**overrides: str) -> Settings:
    """A valid `Settings`; override any variable by name, e.g. `MANAGER_EMAIL="m@x.com"`."""
    return load_settings({**BASE_ENV, **overrides})


DEFAULT_NOW = datetime(2026, 9, 28, 7, 15, tzinfo=UTC)  # Mon 28 Sep 2026 10:15 EEST


def make_deps(
    *,
    store: MessageStore,
    settings: Settings | None = None,
    clock: Clock | None = None,
    mail: FakeMail | None = None,
    calendar: FakeCalendar | None = None,
    classifier: FakeClassifier | None = None,
    meeting_extractor: FakeMeetingExtractor | None = None,
    reply_drafter: FakeReplyDrafter | None = None,
    courtesy_drafter: FakeCourtesyDrafter | None = None,
    acknowledgement_drafter: FakeAcknowledgementDrafter | None = None,
) -> PipelineDeps:
    """`PipelineDeps` wired to fakes; the store is a real SQLite store from the test."""
    clock = clock or FixedClock(DEFAULT_NOW)
    return PipelineDeps(
        settings=settings or make_settings(),
        mailbox_address=MAILBOX,
        mail=mail or FakeMail(address=MAILBOX, clock=clock),
        calendar=calendar or FakeCalendar(),
        store=store,
        classifier=classifier or FakeClassifier(),
        meeting_extractor=meeting_extractor or FakeMeetingExtractor(),
        reply_drafter=reply_drafter or FakeReplyDrafter(),
        courtesy_drafter=courtesy_drafter or FakeCourtesyDrafter(),
        acknowledgement_drafter=acknowledgement_drafter or FakeAcknowledgementDrafter(),
        clock=clock,
    )


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent.db"

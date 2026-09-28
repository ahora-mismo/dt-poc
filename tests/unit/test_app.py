"""Tests for the CLI bootstrap and `run_app`.

Task 24 (ADR-3, D-6, D-18, D-29, D-43, D-50, D-54, D-59).

`run_app` is tested directly with fake `Factories` (D-52): nothing here
makes a real Gmail, Calendar or LLM call. The injected sleeper sets
`stop_event` itself as soon as the poller calls it (after its first
tick), so `run_app` shuts down deterministically without any real delay
-- per the testing rules, "anything that really sleeps" is never used
here. Only `test_run_invalid_config_exits_non_zero` goes through the
real CLI (`typer.testing.CliRunner`), since it exercises `load_settings`
reading the real process environment, which is `cli.py`'s job, not
`run_app`'s.

Test description format: test_<unit>_<condition>_<expected>.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import anyio
import pytest
from structlog.testing import capture_logs
from typer.testing import CliRunner

from dt_agent.app import Factories, LlmPorts, run_app
from dt_agent.cli import app as cli_app
from dt_agent.config import Settings
from dt_agent.domain.clock import FixedClock
from dt_agent.pipeline.deps import PipelineDeps
from dt_agent.ports.calendar import CalendarPort
from dt_agent.ports.errors import FatalAuthError
from dt_agent.ports.mail import MailPort
from tests.fakes.calendar import FakeCalendar
from tests.fakes.llm import (
    FakeAcknowledgementDrafter,
    FakeClassifier,
    FakeCourtesyDrafter,
    FakeMeetingExtractor,
    FakeReplyDrafter,
)
from tests.fakes.mail import FakeMail
from tests.fakes.settings import BASE_ENV, MAILBOX, make_settings

NOW = FixedClock(datetime(2026, 9, 28, 7, 15, tzinfo=UTC))


def _stopping_sleeper(stop_event: anyio.Event) -> Callable[[float], Awaitable[None]]:
    """A `Sleeper` that fires `stop_event` the first time it's awaited, then yields once.

    Used in place of `anyio.sleep`: the poller calls this right after its
    first tick, so `run_app`'s shutdown watcher (`stop_event.wait()`) wakes
    up almost immediately, with nothing here ever really sleeping.
    """

    async def sleeper(_seconds: float) -> None:
        stop_event.set()
        await anyio.sleep(0)

    return sleeper


def _fake_llm(_settings: Settings) -> LlmPorts:
    return LlmPorts(
        classifier=FakeClassifier(),
        meeting_extractor=FakeMeetingExtractor(),
        reply_drafter=FakeReplyDrafter(),
        courtesy_drafter=FakeCourtesyDrafter(),
        acknowledgement_drafter=FakeAcknowledgementDrafter(),
    )


async def _unexpected_serve_mcp(_deps: PipelineDeps, _host: str, _port: int) -> None:
    raise AssertionError("serve_mcp should not be called when ENABLE_MCP=false")


def _make_serve_mcp_spy() -> tuple[
    list[tuple[PipelineDeps, str, int]],
    Callable[[PipelineDeps, str, int], Coroutine[Any, Any, None]],
]:
    calls: list[tuple[PipelineDeps, str, int]] = []

    async def fake_serve_mcp(deps: PipelineDeps, host: str, port: int) -> None:
        calls.append((deps, host, port))
        await anyio.sleep_forever()

    return calls, fake_serve_mcp


def _factories(
    *,
    mail_factory: Callable[[], MailPort],
    stop_event: anyio.Event,
    calendar_factory: Callable[[str], CalendarPort] | None = None,
    serve_mcp: Callable[[PipelineDeps, str, int], Coroutine[Any, Any, None]] | None = None,
) -> Factories:
    return Factories(
        mail=mail_factory,
        calendar=calendar_factory or (lambda _mailbox: FakeCalendar()),
        llm=_fake_llm,
        serve_mcp=serve_mcp or _unexpected_serve_mcp,
        sleeper=_stopping_sleeper(stop_event),
        jitter=lambda: 0.0,
    )


async def test_run_invalid_db_banner(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A corrupt DB file at `DB_PATH` is renamed; the app logs, prints a banner, and continues."""
    db_path = tmp_path / "agent.db"
    db_path.write_text("not a database")
    settings = make_settings(DB_PATH=str(db_path))
    stop_event = anyio.Event()
    mail = FakeMail(address=MAILBOX)
    factories = _factories(mail_factory=lambda: mail, stop_event=stop_event)

    with capture_logs() as logs:
        exit_code = await run_app(settings, factories=factories, clock=NOW, stop_event=stop_event)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "DATABASE INVALID" in out
    assert "A new database was created." in out
    assert any(entry.get("event") == "startup.database_invalid" for entry in logs)


async def test_run_mailbox_address_from_profile(tmp_path: Path) -> None:
    """Mailbox address from `get_profile_address`; approver = mailbox when MANAGER_EMAIL=''."""
    settings = make_settings(DB_PATH=str(tmp_path / "agent.db"), MANAGER_EMAIL="")
    stop_event = anyio.Event()
    mail = FakeMail(address="agent@example.com")
    factories = _factories(mail_factory=lambda: mail, stop_event=stop_event)

    with capture_logs() as logs:
        exit_code = await run_app(settings, factories=factories, clock=NOW, stop_event=stop_event)

    assert exit_code == 0
    approver_entries = [entry for entry in logs if entry.get("event") == "startup.approver"]
    assert len(approver_entries) == 1
    entry = approver_entries[0]
    assert entry["mailbox"] == "agent@example.com"
    assert entry["approver"] == "agent@example.com"
    assert entry["external"] is False


async def test_app_startup_warns_when_internal_domains_empty(tmp_path: Path) -> None:
    """D-42: an empty `INTERNAL_DOMAINS` means the urgent-internal scenario (D-40) can
    never trigger; the app logs a visible warning at startup."""
    settings = make_settings(DB_PATH=str(tmp_path / "agent.db"), INTERNAL_DOMAINS="")
    stop_event = anyio.Event()
    mail = FakeMail(address=MAILBOX)
    factories = _factories(mail_factory=lambda: mail, stop_event=stop_event)

    with capture_logs() as logs:
        exit_code = await run_app(settings, factories=factories, clock=NOW, stop_event=stop_event)

    assert exit_code == 0
    warnings = [entry for entry in logs if entry.get("event") == "startup.internal_domains_empty"]
    assert len(warnings) == 1


async def test_app_startup_no_warning_when_internal_domains_set(tmp_path: Path) -> None:
    settings = make_settings(DB_PATH=str(tmp_path / "agent.db"), INTERNAL_DOMAINS="acme.com")
    stop_event = anyio.Event()
    mail = FakeMail(address=MAILBOX)
    factories = _factories(mail_factory=lambda: mail, stop_event=stop_event)

    with capture_logs() as logs:
        exit_code = await run_app(settings, factories=factories, clock=NOW, stop_event=stop_event)

    assert exit_code == 0
    warnings = [entry for entry in logs if entry.get("event") == "startup.internal_domains_empty"]
    assert warnings == []


async def test_run_fatal_auth_exits_non_zero(tmp_path: Path) -> None:
    """A `FatalAuthError` resolving the mailbox address at startup exits non-zero (D-43, D-50)."""
    settings = make_settings(DB_PATH=str(tmp_path / "agent.db"))
    stop_event = anyio.Event()
    mail = FakeMail(address=MAILBOX)
    mail.fail_next(FatalAuthError("invalid_grant"))
    factories = _factories(mail_factory=lambda: mail, stop_event=stop_event)

    exit_code = await run_app(settings, factories=factories, clock=NOW, stop_event=stop_event)

    assert exit_code != 0


async def test_run_mcp_started_only_when_enabled(tmp_path: Path) -> None:
    """`ENABLE_MCP=false` never starts the MCP server task; `true` starts it on `MCP_PORT`."""
    disabled_settings = make_settings(DB_PATH=str(tmp_path / "agent.db"), ENABLE_MCP="false")
    stop_event = anyio.Event()
    mail = FakeMail(address=MAILBOX)
    calls, fake_serve_mcp = _make_serve_mcp_spy()
    factories = _factories(
        mail_factory=lambda: mail, stop_event=stop_event, serve_mcp=fake_serve_mcp
    )

    exit_code = await run_app(
        disabled_settings, factories=factories, clock=NOW, stop_event=stop_event
    )

    assert exit_code == 0
    assert calls == []

    enabled_settings = make_settings(
        DB_PATH=str(tmp_path / "agent2.db"),
        ENABLE_MCP="true",
        MCP_HOST="127.0.0.1",
        MCP_PORT="8765",
    )
    stop_event = anyio.Event()
    calls, fake_serve_mcp = _make_serve_mcp_spy()
    factories = _factories(
        mail_factory=lambda: mail, stop_event=stop_event, serve_mcp=fake_serve_mcp
    )

    exit_code = await run_app(
        enabled_settings, factories=factories, clock=NOW, stop_event=stop_event
    )

    assert exit_code == 0
    assert [port for _, _, port in calls] == [8765]


async def test_run_mcp_started_on_configured_host(tmp_path: Path) -> None:
    """`run_app` starts the MCP server on the configured `MCP_HOST`/`MCP_PORT` (D-59, D-65)."""
    settings = make_settings(
        DB_PATH=str(tmp_path / "agent.db"),
        ENABLE_MCP="true",
        MCP_HOST="127.0.0.1",
        MCP_PORT="8765",
    )
    stop_event = anyio.Event()
    mail = FakeMail(address=MAILBOX)
    calls, fake_serve_mcp = _make_serve_mcp_spy()
    factories = _factories(
        mail_factory=lambda: mail, stop_event=stop_event, serve_mcp=fake_serve_mcp
    )

    exit_code = await run_app(settings, factories=factories, clock=NOW, stop_event=stop_event)

    assert exit_code == 0
    assert [(host, port) for _, host, port in calls] == [("127.0.0.1", 8765)]


def test_run_invalid_config_exits_non_zero() -> None:
    """A missing required variable makes `dt-agent run` exit non-zero, naming it (D-29)."""
    runner = CliRunner()
    env = dict(BASE_ENV)
    del env["LLM_PROVIDER"]

    with mock.patch.dict(os.environ, env, clear=True):
        result = runner.invoke(cli_app, ["run"])

    assert result.exit_code != 0
    assert "LLM_PROVIDER" in result.output

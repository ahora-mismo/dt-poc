"""Integration test: real MCP calls to the real app (D-59).

Starts `dt-agent run` as a separate process with the project's `.env` (real Gmail,
Google Calendar and LLM), overriding only `ENABLE_MCP=true`, `MCP_HOST=127.0.0.1` and
a free `MCP_PORT`; then a real MCP client connects over streamable HTTP, lists the
tools and calls each one. Local only (no Docker). Not part of the default run:

    uv run pytest -m integration tests/integration

While it runs, the app also polls the inbox and processes new mail as it normally
would (the same `DB_PATH`, so mail already handled is not processed again). Costs a
few LLM calls per run.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from dotenv import dotenv_values
from mcp import Client

from dt_agent.adapters.calendar import build_calendar_adapter
from dt_agent.adapters.google_auth import build_credentials

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
MAILBOX_ADDRESS = "dt.agent.interview@gmail.com"
STARTUP_TIMEOUT_SECONDS = 90
TOOL_TIMEOUT_SECONDS = 120

EXPECTED_TOOLS = {
    "classify_email",
    "extract_meeting",
    "draft_sales_decline",
    "draft_courtesy_reply",
    "extract_facts",
    "find_meeting_slots",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _log_tail(log_path: Path, lines: int = 40) -> str:
    return "\n".join(log_path.read_text(errors="replace").splitlines()[-lines:])


@pytest.fixture(scope="module")
def env_file_values() -> dict[str, str]:
    if not ENV_FILE.is_file():
        pytest.skip(".env not found: the integration test needs real credentials")
    values = {key: value or "" for key, value in dotenv_values(ENV_FILE).items()}
    if not values.get("GOOGLE_REFRESH_TOKEN"):
        pytest.skip(".env has no GOOGLE_REFRESH_TOKEN: the integration test needs real credentials")
    return values


@pytest.fixture(scope="module")
def app_url(
    env_file_values: dict[str, str], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """The real app, started from `.env` with MCP on; yields its MCP endpoint URL."""
    port = _free_port()
    env = {
        **os.environ,
        **env_file_values,
        "ENABLE_MCP": "true",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "LOG_COLOR": "never",
    }
    log_path = tmp_path_factory.mktemp("mcp_live") / "app.log"
    executable = Path(sys.executable).parent / "dt-agent"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [str(executable), "run"],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while not _port_open(port):
            if process.poll() is not None:
                code = process.returncode
                pytest.fail(f"the app exited with code {code}:\n{_log_tail(log_path)}")
            if time.monotonic() > deadline:
                pytest.fail(f"the MCP server did not start in time:\n{_log_tail(log_path)}")
            time.sleep(0.5)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


async def _call(url: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with Client(url) as client:
        result = await client.call_tool(name, arguments, read_timeout_seconds=TOOL_TIMEOUT_SECONDS)
    assert not result.is_error, result.content
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


async def test_mcp_live_lists_all_tools(app_url: str) -> None:
    async with Client(app_url) as client:
        listing = await client.list_tools()

    assert {tool.name for tool in listing.tools} == EXPECTED_TOOLS


def _meeting_day(zone: ZoneInfo) -> datetime:
    """A weekday at least two days ahead, 10:00 local: a date the email states outright."""
    day = datetime.now(zone) + timedelta(days=2)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.replace(hour=10, minute=0, second=0, microsecond=0)


def _meeting_body(day: datetime) -> str:
    return (
        f"Hi, could we meet on {day:%A}, {day.day} {day:%B %Y} at 10:00 for thirty "
        "minutes to go over the Q4 roadmap? Thanks, Dana"
    )


async def test_mcp_live_classify_email(app_url: str, env_file_values: dict[str, str]) -> None:
    day = _meeting_day(ZoneInfo(env_file_values["DEFAULT_TIMEZONE"]))

    result = await _call(
        app_url,
        "classify_email",
        {
            "subject": "Quick sync on the Q4 roadmap",
            "body": _meeting_body(day),
            "sender": "dana@example.com",
        },
    )

    assert result == {
        "primary": "MEETING_REQUEST",
        "urgent": False,
        "subtype": "none",
        "facts": ["Sender authentication: no Authentication-Results header."],
    }


async def test_mcp_live_extract_meeting(app_url: str, env_file_values: dict[str, str]) -> None:
    zone = ZoneInfo(env_file_values["DEFAULT_TIMEZONE"])
    day = _meeting_day(zone)

    result = await _call(
        app_url,
        "extract_meeting",
        {
            "subject": "Quick sync on the Q4 roadmap",
            "body": _meeting_body(day),
            "sender": "dana@example.com",
        },
    )

    assert result["duration_minutes"] == 30
    assert result["timezone"] == env_file_values["DEFAULT_TIMEZONE"]
    assert result["timezone_assumed"] is True
    assert result["extra_attendees"] == []
    assert result["location"] is None
    assert [
        (p["resolution"], datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"]))
        for p in result["proposals"]
    ] == [("exact", day, day + timedelta(minutes=30))]


async def test_mcp_live_draft_sales_decline(app_url: str) -> None:
    """Free text from the LLM: only the prompt's hard rules are checked here; the wording
    is judged by the evals (`dt-agent eval --kind sales`)."""
    result = await _call(
        app_url,
        "draft_sales_decline",
        {
            "subject": "A quick idea for your team",
            "body": "Hi, I'm Mark from CloudBoost. Our analytics platform could save your "
            "team hours every week. Would you be open to a 15-minute call? Best, Mark",
            "sender": "mark@cloudboost.example",
        },
    )

    body = result["body"].strip()
    assert body
    assert "?" not in body  # no questions back to the sender
    assert not body.lower().startswith(("hi", "hello", "dear"))  # no greeting block
    assert "Alex's Assistant" not in body  # the signature is added by code, not the draft


async def test_mcp_live_draft_courtesy_reply(app_url: str) -> None:
    result = await _call(
        app_url,
        "draft_courtesy_reply",
        {
            "subject": "Thanks for your help",
            "body": "Hi, I just wanted to say thanks again for your help with the audit "
            "last week. Best, Dana",
            "sender": "dana@example.com",
            "label": "INFO_ONLY",
        },
    )

    assert result["warranted"] is True
    assert result["body"] == "You're welcome."  # courtesy_reply.md: a plain thank-you, exactly


async def test_mcp_live_extract_facts(app_url: str) -> None:
    result = await _call(
        app_url,
        "extract_facts",
        {
            "subject": "Tech Weekly",
            "body": "This week's top stories.",
            "sender": "news@techweekly.example",
            "headers": {"List-Unsubscribe": "<mailto:unsubscribe@techweekly.example>"},
        },
    )

    assert result == {
        "facts": [
            "Sender authentication: no Authentication-Results header.",
            "Automated sender: List-Unsubscribe header present",
            "Bulk-mail cue(s): List-Unsubscribe header present",
        ]
    }


async def _calendar_busy(
    env_file_values: dict[str, str], start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """The calendar's busy intervals, read directly with the same credentials (not via MCP)."""
    credentials = build_credentials(
        env_file_values["GOOGLE_CLIENT_ID"],
        env_file_values["GOOGLE_CLIENT_SECRET"],
        env_file_values["GOOGLE_REFRESH_TOKEN"],
    )
    calendar = build_calendar_adapter(credentials, MAILBOX_ADDRESS)
    return [(interval.start, interval.end) for interval in await calendar.free_busy(start, end)]


def _expected_slots(
    env_file_values: dict[str, str],
    busy: list[tuple[datetime, datetime]],
    window_start: datetime,
    count: int,
) -> list[tuple[datetime, datetime]]:
    """The earliest `count` free 30-minute slots from `window_start`: half-hour aligned,
    Monday to Friday, inside business hours, overlapping no busy interval."""
    open_hour = int(env_file_values["BUSINESS_HOURS_START"])
    close_hour = int(env_file_values["BUSINESS_HOURS_END"])
    slots: list[tuple[datetime, datetime]] = []
    start = window_start
    while len(slots) < count:
        end = start + timedelta(minutes=30)
        in_hours = (
            start.weekday() < 5
            and start.hour >= open_hour
            and end <= start.replace(hour=close_hour, minute=0)
        )
        if in_hours and all(not (start < b_end and b_start < end) for b_start, b_end in busy):
            slots.append((start, end))
        start += timedelta(minutes=30)
    return slots


async def test_mcp_live_find_meeting_slots(app_url: str, env_file_values: dict[str, str]) -> None:
    zone = ZoneInfo(env_file_values["DEFAULT_TIMEZONE"])
    window_start = (datetime.now(zone) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    window_end = window_start + timedelta(days=7)
    busy = await _calendar_busy(env_file_values, window_start, window_end)
    expected = _expected_slots(
        env_file_values, busy, window_start, int(env_file_values["MEETING_MAX_OPTIONS"])
    )

    result = await _call(
        app_url,
        "find_meeting_slots",
        {
            "duration_minutes": 30,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
    )

    assert [
        (
            option["number"],
            datetime.fromisoformat(option["start"]),
            datetime.fromisoformat(option["end"]),
            option["mark"],
        )
        for option in result["options"]
    ] == [(i + 1, start, end, "free") for i, (start, end) in enumerate(expected)]

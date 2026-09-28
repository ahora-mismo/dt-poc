"""Unit tests for dt_agent.mcp_server (task 25, D-59).

The six tools are thin, dry-run wrappers around the same ports/core the
workers use. `MCPServer.call_tool` is used directly (in-process, no real
HTTP transport) per the testing rules; fakes come from `tests.fakes` via
`make_deps`. Every assertion that a tool is "dry-run" checks the fakes'
recorded state (`FakeMail.sent`, `FakeCalendar.inserted`, `store.contains`),
never a mock's call count.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from mcp_types import CallToolResult, InputRequiredResult

from dt_agent.adapters.sqlite_store import open_store
from dt_agent.domain.classification import Classification
from dt_agent.domain.clock import FixedClock
from dt_agent.domain.labels import PrimaryLabel, Subtype
from dt_agent.domain.meeting import MeetingExtraction
from dt_agent.mcp_server import build_mcp_server
from dt_agent.ports.llm import CourtesyDraft
from tests.fakes.calendar import FakeCalendar
from tests.fakes.llm import (
    FakeClassifier,
    FakeCourtesyDrafter,
    FakeMeetingExtractor,
    FakeReplyDrafter,
)
from tests.fakes.mail import FakeMail
from tests.fakes.settings import MAILBOX, make_deps

_CLOCK = FixedClock(datetime(2026, 9, 28, 7, 15, tzinfo=UTC))


def _tool_result(result: CallToolResult | InputRequiredResult) -> CallToolResult:
    """Narrow a `call_tool` result: none of these tools ever ask for more input."""
    assert isinstance(result, CallToolResult)
    return result


EXPECTED_TOOL_NAMES = {
    "classify_email",
    "extract_meeting",
    "draft_sales_decline",
    "draft_courtesy_reply",
    "extract_facts",
    "find_meeting_slots",
}


async def test_mcp_tools_listed(tmp_path: Path) -> None:
    store, _ = open_store(tmp_path / "agent.db", _CLOCK)
    deps = make_deps(store=store, clock=_CLOCK)
    server = build_mcp_server(deps)

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == EXPECTED_TOOL_NAMES


async def test_mcp_tools_dry_run(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX)
    calendar = FakeCalendar()
    classifier = FakeClassifier(
        default=Classification(
            primary=PrimaryLabel.MEETING_REQUEST, urgent=False, subtype=Subtype.NONE
        )
    )
    meeting_extractor = FakeMeetingExtractor(
        default=MeetingExtraction(title="Sync", proposals=[], duration_minutes=30)
    )
    reply_drafter = FakeReplyDrafter(default="Thanks, but no thanks.")
    courtesy_drafter = FakeCourtesyDrafter(
        default=CourtesyDraft(warranted=True, reason="urgent", body="Got it, thank you.")
    )
    store, _ = open_store(tmp_path / "agent.db", _CLOCK)
    deps = make_deps(
        store=store,
        clock=_CLOCK,
        mail=mail,
        calendar=calendar,
        classifier=classifier,
        meeting_extractor=meeting_extractor,
        reply_drafter=reply_drafter,
        courtesy_drafter=courtesy_drafter,
    )
    server = build_mcp_server(deps)

    classify_result = _tool_result(
        await server.call_tool(
            "classify_email",
            {
                "subject": "Can we meet Tuesday?",
                "body": "Let's meet Tuesday at 10am.",
                "sender": "alice@example.com",
            },
        )
    )
    assert not classify_result.is_error
    assert classify_result.structured_content is not None
    assert classify_result.structured_content["primary"] == "MEETING_REQUEST"
    assert classify_result.structured_content["urgent"] is False
    assert classify_result.structured_content["subtype"] == "none"
    assert isinstance(classify_result.structured_content["facts"], list)

    meeting_result = _tool_result(
        await server.call_tool(
            "extract_meeting",
            {
                "subject": "Can we meet Tuesday?",
                "body": "Let's meet Tuesday at 10am.",
                "sender": "alice@example.com",
                "to": [MAILBOX],
                "cc": [],
                "received_time": "2026-09-28T07:15:00Z",
            },
        )
    )
    assert not meeting_result.is_error
    assert meeting_result.structured_content is not None
    assert meeting_result.structured_content["duration_minutes"] == 30
    assert meeting_result.structured_content["extra_attendees"] == []

    decline_result = _tool_result(
        await server.call_tool(
            "draft_sales_decline",
            {
                "subject": "Great opportunity for you",
                "body": "Buy our product!",
                "sender": "sales@vendor.com",
            },
        )
    )
    assert not decline_result.is_error
    assert decline_result.structured_content == {"body": "Thanks, but no thanks."}

    courtesy_result = _tool_result(
        await server.call_tool(
            "draft_courtesy_reply",
            {
                "subject": "Quick question",
                "body": "Do you have a minute?",
                "sender": "bob@example.com",
                "label": "TASK",
            },
        )
    )
    assert not courtesy_result.is_error
    assert courtesy_result.structured_content == {
        "warranted": True,
        "reason": "urgent",
        "body": "Got it, thank you.",
    }

    facts_result = _tool_result(
        await server.call_tool(
            "extract_facts",
            {
                "subject": "Invoice #123",
                "body": "Please find attached invoice.",
                "sender": "billing@acme.com",
                "headers": {"List-Unsubscribe": "<mailto:x@acme.com>"},
            },
        )
    )
    assert not facts_result.is_error
    assert facts_result.structured_content is not None
    fact_lines = facts_result.structured_content["facts"]
    assert any("Bulk" in line or "List-Unsubscribe" in line for line in fact_lines)

    slots_result = _tool_result(
        await server.call_tool(
            "find_meeting_slots",
            {
                "duration_minutes": 30,
                "window_start": "2026-09-28T09:00:00+03:00",
                "window_end": "2026-09-28T18:00:00+03:00",
            },
        )
    )
    assert not slots_result.is_error
    assert slots_result.structured_content is not None
    assert len(slots_result.structured_content["options"]) > 0

    # Dry-run (D-59): nothing sent, labelled, archived, or added to the calendar,
    # and nothing written to SQLite.
    assert mail.sent == []
    assert calendar.inserted == []
    assert not await store.contains("mcp-transient")
    assert not await store.contains("msg-1")


async def _slots(
    tmp_path: Path, calendar: FakeCalendar, window_start: str, window_end: str
) -> list[dict[str, object]]:
    store, _ = open_store(tmp_path / "agent.db", _CLOCK)
    server = build_mcp_server(make_deps(store=store, clock=_CLOCK, calendar=calendar))
    result = _tool_result(
        await server.call_tool(
            "find_meeting_slots",
            {"duration_minutes": 60, "window_start": window_start, "window_end": window_end},
        )
    )
    assert not result.is_error
    assert result.structured_content is not None
    options: list[dict[str, object]] = result.structured_content["options"]
    return options


async def test_mcp_find_meeting_slots_returns_up_to_max_options_inside_the_window(
    tmp_path: Path,
) -> None:
    calendar = FakeCalendar()
    calendar.add_busy(
        datetime(2026, 9, 29, 7, 0, tzinfo=UTC), datetime(2026, 9, 29, 8, 0, tzinfo=UTC)
    )

    options = await _slots(
        tmp_path, calendar, "2026-09-29T09:00:00+03:00", "2026-09-29T18:00:00+03:00"
    )

    assert [(o["number"], o["start"], o["mark"]) for o in options] == [
        (1, "2026-09-29T09:00:00+03:00", "free"),
        (2, "2026-09-29T11:00:00+03:00", "free"),
        (3, "2026-09-29T11:30:00+03:00", "free"),
    ]


async def test_mcp_find_meeting_slots_full_window_offers_times_after_it(tmp_path: Path) -> None:
    calendar = FakeCalendar()
    calendar.add_busy(
        datetime(2026, 9, 29, 6, 0, tzinfo=UTC), datetime(2026, 9, 29, 15, 0, tzinfo=UTC)
    )

    options = await _slots(
        tmp_path, calendar, "2026-09-29T09:00:00+03:00", "2026-09-29T18:00:00+03:00"
    )

    assert len(options) == 3
    assert {o["mark"] for o in options} == {"outside_requested_interval"}

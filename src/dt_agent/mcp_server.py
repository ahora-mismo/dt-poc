"""MCP server for testing the agent's decisions (D-59).

Exposes the agent's decisions as six MCP tools, thin wrappers around the
same ports/core the workers use (`pipeline/process_email.py`). Every tool
is dry-run: nothing is sent, labelled, archived, moved to spam or added to
the calendar, and nothing is written to SQLite (`find_meeting_slots` reads
`CalendarPort.free_busy` only; every other tool touches no port that has a
side effect at all). Each call is logged through the unified logger
(`mcp.tool_call`, D-58).

`build_mcp_server` returns an `mcp.server.mcpserver.MCPServer` (the
official `mcp` SDK 2.x's high-level server class; the plan's `MCPServer`
name matches the installed package exactly, so no renaming was needed).
`serve_mcp` runs its streamable HTTP transport on the configured
`MCP_HOST:MCP_PORT` (D-59, D-65): `MCP_HOST` is `127.0.0.1` for a local run,
and `0.0.0.0` inside Docker (`docker-compose.yml` sets it via `environment:`,
since the container must listen on all interfaces for Docker to forward the
published port; the host side still only publishes `127.0.0.1`).
`MCPServer.run_streamable_http_async` is a plain coroutine (no `anyio.run` of
its own), so the caller can `await`/`start_soon` it inside an existing anyio
task group (task 24, `ENABLE_MCP=true` only).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver import MCPServer
from pydantic import AwareDatetime, BaseModel, ConfigDict

from dt_agent.core.dates import InvalidResolution, NoTimeResolution, resolve_expressions
from dt_agent.core.facts import extract_facts as _code_extract_facts
from dt_agent.core.scheduling import (
    BusyInterval as SchedulingBusyInterval,
)
from dt_agent.core.scheduling import (
    RequestedWindow,
    SchedulingParams,
    SchedulingRequest,
    schedule,
)
from dt_agent.domain.email import Email, EmailAddress, Headers
from dt_agent.domain.labels import PrimaryLabel, Subtype
from dt_agent.domain.meeting import MeetingExtraction, ResolvedSlot, SlotMark, SlotOption
from dt_agent.logging import get_logger
from dt_agent.pipeline.deps import PipelineDeps
from dt_agent.ports.facts import Facts as PortFacts

logger = get_logger(__name__)

_TRANSIENT_ID = "mcp-transient"


# --------------------------------------------------------------------------
# Tool result models
# --------------------------------------------------------------------------


class ClassifyEmailResult(BaseModel):
    """`classify_email`'s result (D-24, D-25, D-44, D-63)."""

    model_config = ConfigDict(frozen=True)

    primary: PrimaryLabel
    urgent: bool
    subtype: Subtype
    facts: list[str]


class MeetingProposalResult(BaseModel):
    """One resolved slot for a date expression the LLM restated, plus the time code
    computed (D-64). A multi-day `window` expression yields several of these, one per
    business day it spans, all sharing that expression's `phrase` (task 39, F1)."""

    model_config = ConfigDict(frozen=True)

    phrase: str
    resolution: str
    """One of `"exact"`, `"window"`, `"none"` (D-64) or `"invalid"` (D-66)."""
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None


class ExtractMeetingResult(BaseModel):
    """`extract_meeting`'s result (ADR-4, D-64)."""

    model_config = ConfigDict(frozen=True)

    title: str | None
    proposals: list[MeetingProposalResult]
    duration_minutes: int
    timezone: str
    timezone_assumed: bool
    extra_attendees: list[str]
    location: str | None


class DraftSalesDeclineResult(BaseModel):
    """`draft_sales_decline`'s result (D-38)."""

    model_config = ConfigDict(frozen=True)

    body: str


class DraftCourtesyReplyResult(BaseModel):
    """`draft_courtesy_reply`'s result (D-39)."""

    model_config = ConfigDict(frozen=True)

    warranted: bool
    reason: str
    body: str


class ExtractFactsResult(BaseModel):
    """`extract_facts`'s result (D-44): code-extracted facts only, no LLM."""

    model_config = ConfigDict(frozen=True)

    facts: list[str]


class FindMeetingSlotsResult(BaseModel):
    """`find_meeting_slots`'s result (ADR-4, D-13, D-14, D-45)."""

    model_config = ConfigDict(frozen=True)

    options: list[SlotOption]


# --------------------------------------------------------------------------
# Helpers shared by several tools
# --------------------------------------------------------------------------


@contextmanager
def _log_tool_call(name: str) -> Iterator[None]:
    """Log one `mcp.tool_call` line with the tool name and duration (D-58, D-59)."""
    start = time.monotonic()
    try:
        yield
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        logger.info("mcp.tool_call", tool=name, duration_ms=duration_ms)


def _transient_email(
    *,
    subject: str,
    body: str,
    sender: str,
    headers: dict[str, str] | None,
    received_at: AwareDatetime,
    to: list[str] | None = None,
    cc: list[str] | None = None,
) -> Email:
    """A synthetic `Email` for one tool call; never read from or written to Gmail."""
    return Email(
        id=_TRANSIENT_ID,
        thread_id=_TRANSIENT_ID,
        headers=Headers(values=headers or {}),
        from_address=EmailAddress(address=sender),
        to=tuple(EmailAddress(address=address) for address in to or ()),
        cc=tuple(EmailAddress(address=address) for address in cc or ()),
        subject=subject,
        received_at=received_at,
        text_body=body,
    )


def _extract_facts_for(deps: PipelineDeps, email: Email) -> list[str]:
    """The same code-extracted facts the classifier sees (D-44), as prompt lines."""
    facts = _code_extract_facts(
        email,
        own_address=deps.mailbox_address,
        internal_domains=deps.settings.INTERNAL_DOMAINS,
        allowlist_senders=deps.settings.SPAM_ALLOWLIST_SENDERS,
        allowlist_domains=deps.settings.SPAM_ALLOWLIST_DOMAINS,
        gmail_labels=(),  # Not a real Gmail message, so no Gmail category labels.
    )
    return facts.to_prompt_lines()


def _resolve_timezone(extraction: MeetingExtraction, default_timezone: str) -> tuple[str, bool]:
    """`(timezone, assumed)`: the stated zone if valid, else the default (D-1, ADR-4)."""
    if extraction.timezone_stated and extraction.timezone:
        try:
            ZoneInfo(extraction.timezone)
        except ZoneInfoNotFoundError, ValueError:
            return default_timezone, True
        return extraction.timezone, False
    return default_timezone, True


def _guarded_attendees(email: Email, extra_attendees: list[str]) -> list[str]:
    """Drop any LLM-returned address not in From/To/Cc (ADR-4's hallucination guardrail)."""
    known = {
        email.from_address.address,
        *(address.address for address in email.to),
        *(address.address for address in email.cc),
    }
    candidates = (address.strip().lower() for address in extra_attendees)
    return list(dict.fromkeys(address for address in candidates if address in known))


def _proposal_result(resolution: object) -> MeetingProposalResult:
    if isinstance(resolution, ResolvedSlot):
        return MeetingProposalResult(
            phrase=resolution.phrase,
            resolution=resolution.source,
            start=resolution.start,
            end=resolution.end,
        )
    if isinstance(resolution, NoTimeResolution):
        return MeetingProposalResult(phrase=resolution.phrase, resolution="none")
    assert isinstance(resolution, InvalidResolution)
    return MeetingProposalResult(phrase=resolution.phrase, resolution="invalid")


# --------------------------------------------------------------------------
# Server construction
# --------------------------------------------------------------------------


def build_mcp_server(deps: PipelineDeps) -> MCPServer:
    """Build the MCP server exposing the six dry-run tools (D-59)."""
    server = MCPServer("dt-agent")

    @server.tool()
    async def classify_email(
        subject: str,
        body: str,
        sender: str,
        headers: dict[str, str] | None = None,
    ) -> ClassifyEmailResult:
        """Classify one email exactly like the pipeline does, without acting on it (D-24, D-44).

        Returns `urgent` only, not the LLM's own raw `deadline`/`urgency_requested`
        (task 31): this tool wraps `deps.classifier`, the `Classifier` port, which by
        design returns only `Classification` (`primary`, `urgent`, `subtype`, and, since
        task 47, `deadline_phrase`/`deadline_at`) — the LLM's raw `deadline` restatement
        and `urgency_requested` are consumed inside `LLMClassifier.classify` and never
        cross the port boundary. Exposing those two raw fields here would mean widening
        the port for this dry-run tool alone, which task 31 doesn't ask for.
        """
        with _log_tool_call("classify_email"):
            email = _transient_email(
                subject=subject,
                body=body,
                sender=sender,
                headers=headers,
                received_at=deps.clock.now(),
            )
            facts_lines = _extract_facts_for(deps, email)
            port_facts = PortFacts(items={"facts": facts_lines})
            classification = await deps.classifier.classify(email, port_facts)
            return ClassifyEmailResult(
                primary=classification.primary,
                urgent=classification.urgent,
                subtype=classification.subtype,
                facts=facts_lines,
            )

    @server.tool()
    async def extract_meeting(
        subject: str,
        body: str,
        sender: str,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        received_time: AwareDatetime | None = None,
    ) -> ExtractMeetingResult:
        """Extract meeting details and resolve the proposed dates, without scheduling (ADR-4)."""
        with _log_tool_call("extract_meeting"):
            received_at = received_time or deps.clock.now()
            email = _transient_email(
                subject=subject,
                body=body,
                sender=sender,
                headers=None,
                received_at=received_at,
                to=to,
                cc=cc,
            )
            extraction = await deps.meeting_extractor.extract(email)
            default_timezone = deps.settings.DEFAULT_TIMEZONE
            timezone, timezone_assumed = _resolve_timezone(extraction, default_timezone)
            duration_minutes = extraction.duration_minutes or deps.settings.DEFAULT_MEETING_MINUTES
            resolutions = resolve_expressions(
                extraction.proposals,
                received_at,
                timezone,
                timedelta(minutes=duration_minutes),
                business_hours_start=deps.settings.BUSINESS_HOURS_START,
                business_hours_end=deps.settings.BUSINESS_HOURS_END,
            )
            return ExtractMeetingResult(
                title=extraction.title,
                proposals=[_proposal_result(resolution) for resolution in resolutions],
                duration_minutes=duration_minutes,
                timezone=timezone,
                timezone_assumed=timezone_assumed,
                extra_attendees=_guarded_attendees(email, extraction.extra_attendees),
                location=extraction.location,
            )

    @server.tool()
    async def draft_sales_decline(subject: str, body: str, sender: str) -> DraftSalesDeclineResult:
        """Draft the sales-outreach decline body, without sending it (D-38)."""
        with _log_tool_call("draft_sales_decline"):
            email = _transient_email(
                subject=subject,
                body=body,
                sender=sender,
                headers=None,
                received_at=deps.clock.now(),
            )
            decline_body = await deps.reply_drafter.draft_sales_decline(email)
            return DraftSalesDeclineResult(body=decline_body)

    @server.tool()
    async def draft_courtesy_reply(
        subject: str, body: str, sender: str, label: PrimaryLabel
    ) -> DraftCourtesyReplyResult:
        """Decide whether a courtesy acknowledgement is warranted, and draft it (D-39)."""
        with _log_tool_call("draft_courtesy_reply"):
            email = _transient_email(
                subject=subject,
                body=body,
                sender=sender,
                headers=None,
                received_at=deps.clock.now(),
            )
            draft = await deps.courtesy_drafter.draft(email, label)
            return DraftCourtesyReplyResult(
                warranted=draft.warranted, reason=draft.reason, body=draft.body
            )

    @server.tool()
    async def extract_facts(
        subject: str,
        body: str,
        sender: str,
        headers: dict[str, str] | None = None,
    ) -> ExtractFactsResult:
        """The code-extracted facts only, no LLM call (D-44)."""
        with _log_tool_call("extract_facts"):
            email = _transient_email(
                subject=subject,
                body=body,
                sender=sender,
                headers=headers,
                received_at=deps.clock.now(),
            )
            return ExtractFactsResult(facts=_extract_facts_for(deps, email))

    @server.tool()
    async def find_meeting_slots(
        duration_minutes: int,
        window_start: AwareDatetime,
        window_end: AwareDatetime,
    ) -> FindMeetingSlotsResult:
        """Free slot options in `[window_start, window_end)`, read-only (ADR-4, D-13, D-14)."""
        with _log_tool_call("find_meeting_slots"):
            settings = deps.settings
            params = SchedulingParams(
                timezone=settings.DEFAULT_TIMEZONE,
                business_hours_start=settings.BUSINESS_HOURS_START,
                business_hours_end=settings.BUSINESS_HOURS_END,
                default_meeting_minutes=settings.DEFAULT_MEETING_MINUTES,
                horizon_business_days=settings.SCHEDULING_HORIZON_BUSINESS_DAYS,
                min_lead_minutes=settings.MEETING_MIN_LEAD_MINUTES,
                max_options=settings.MEETING_MAX_OPTIONS,
            )
            # A generous margin past `window_end`, in case D-45's "no free slot in
            # the window" alternatives search past it (mirrors process_email.py's
            # `_free_busy_search_range`).
            margin = timedelta(days=params.horizon_business_days * 2 + 7)
            busy_raw = await deps.calendar.free_busy(window_start, window_end + margin)
            busy = [
                SchedulingBusyInterval(start=interval.start, end=interval.end, title=interval.title)
                for interval in busy_raw
            ]
            # `schedule()` gives one option per requested window (D-13), but here the
            # window is a search range: ask again past each found slot, up to
            # `max_options`. A window with no free slot keeps D-45's alternatives.
            now = deps.clock.now()
            options: list[SlotOption] = []
            search_from = window_start
            while len(options) < params.max_options and search_from < window_end:
                request = SchedulingRequest(
                    windows=(RequestedWindow(start=search_from, end=window_end),),
                    duration_minutes=duration_minutes,
                )
                found = schedule(request, busy, params, now).options
                if not found or found[0].mark != SlotMark.FREE:
                    if not options:
                        options = list(found)
                    break
                options.append(found[0].model_copy(update={"number": len(options) + 1}))
                search_from = found[0].start + timedelta(minutes=30)
            return FindMeetingSlotsResult(options=options)

    return server


async def serve_mcp(deps: PipelineDeps, *, host: str, port: int) -> None:
    """Serve MCP over streamable HTTP on `host:port` (D-4, D-59, D-65).

    `host` is `Settings.MCP_HOST` (`127.0.0.1` locally, `0.0.0.0` inside
    Docker, D-59). A plain coroutine: the caller (task 24's `dt-agent run`,
    only when `ENABLE_MCP=true`) awaits or `start_soon`s it inside its own
    anyio task group, cooperatively alongside the poller and worker pool.
    """
    server = build_mcp_server(deps)
    await server.run_streamable_http_async(host=host, port=port)

"""Per-email flow (task 19, D-25, D-27, D-43, D-58, D-63): pipeline/process_email.py.

`make_email_handler` builds the per-message callable the worker pool
(`pipeline/workers.py`) calls with `(message_id, attempt)`. The flow is
linear, with no mid-flow state (D-27): fetch the message and its thread,
`route()` it (D-21, D-22, D-47), then either hand it to the injected
`ApprovalHandler` (task 20, `pipeline/approvals.py`) or run it through
classification and the policy engine (`core/policy.py`), execute the
resulting `ActionPlan`, and record it in the store exactly once, with
exactly one `email.processed` audit line (D-58).

`ApprovalHandled` is defined here (not in `pipeline/approvals.py`) so this
module never imports the approval handler's implementation; only the
`ApprovalHandler` callable shape is imported by task 20.

Spam is decided first (D-44): every incoming email is classified, and a
SPAM classification is only moved to Spam; nothing else runs for it,
invitation detection included. For non-spam mail, a calendar invitation
detected by code (D-48) with `METHOD:REQUEST` replaces the classification
with a fixed MEETING_REQUEST (times from the invitation). A
`METHOD:CANCEL`/`METHOD:REPLY` keeps the classifier's label, and the
detection result is passed to `decide()` so no calendar action is ever
taken for it (`core/policy.py` encodes that rule).

Every other LLM-backed call (the meeting extractor, both drafters) is
wrapped so an `LLMOutputError` never escapes this module: letting it
propagate would make the worker pool retry and eventually label
`AI/Error`, contradicting D-43's "invalid LLM output -> no action,
`AI/NeedsReview`" rule, which this module treats as the general rule for
every LLM call, not only the classifier's. A failure past the classifier
(meeting extraction, drafting) simply skips that one action; the label/
archive actions already decided from the classification stand.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dt_agent.core.approval_email import (
    ApprovalContext,
    ApprovalEmailRequest,
    CalendarEmailRequest,
    InvitationEmailRequest,
    ReplyEmailRequest,
    build_approval_email,
)
from dt_agent.core.approval_token import InvitationAction, ReplyAction
from dt_agent.core.dates import InvalidResolution, resolve_expressions
from dt_agent.core.facts import EmailFacts, extract_facts, truncate_for_llm
from dt_agent.core.invitations import InvitationKind, InvitationResult, detect_invitation
from dt_agent.core.policy import ActionPlan, Flow, decide
from dt_agent.core.routing import RouteKind, find_approval_emails, route
from dt_agent.core.scheduling import (
    BusyInterval as SchedulingBusyInterval,
)
from dt_agent.core.scheduling import (
    InvalidRequest,
    RequestedSlot,
    RequestedWindow,
    SchedulingFlag,
    SchedulingParams,
    SchedulingRequest,
    schedule,
)
from dt_agent.domain.classification import Classification
from dt_agent.domain.email import Email, EmailAddress
from dt_agent.domain.labels import (
    AI_NEEDS_REVIEW_LABEL,
    GMAIL_LABEL_ID_INBOX,
    GMAIL_LABEL_ID_SENT,
    GMAIL_LABEL_ID_SPAM,
    GMAIL_LABEL_ID_STARRED,
    PrimaryLabel,
    Subtype,
)
from dt_agent.domain.meeting import MeetingExtraction, ResolvedSlot, SlotMark
from dt_agent.logging import get_logger, log_email_processed
from dt_agent.pipeline.deps import PipelineDeps
from dt_agent.pipeline.replies import (
    build_reply,
    meeting_unavailable_body,
    reply_recipient,
    reply_text,
)
from dt_agent.ports.errors import LLMOutputError
from dt_agent.ports.facts import Facts as PortFacts
from dt_agent.ports.mail import OutgoingMessage

logger = get_logger(__name__)

_NO_INVITATION = InvitationResult(kind=InvitationKind.NOT_INVITATION)


@dataclass(frozen=True, slots=True)
class ApprovalHandled:
    """What the approval handler (task 20) reports back about one approval reply.

    `pipeline/approvals.py` returns this; this module only records the
    email and logs its one `email.processed` line with it (D-27, D-58).
    """

    outcome: str
    actions: tuple[str, ...] = ()
    reason: str | None = None


ApprovalHandler = Callable[[PipelineDeps, Email, Sequence[Email]], Awaitable[ApprovalHandled]]
"""Handles one `APPROVAL_REPLY`-routed message (task 20): `(deps, email, thread)`."""

Handler = Callable[[str, int], Awaitable[None]]


def make_email_handler(deps: PipelineDeps, approval_handler: ApprovalHandler) -> Handler:
    """Build the `(message_id, attempt) -> None` callable the worker pool calls."""

    async def handle(message_id: str, attempt: int) -> None:
        await _process_email(deps, approval_handler, message_id, attempt)

    return handle


def _format_sender(address: EmailAddress) -> str:
    """`Name <email>`, or just the address with no display name (D-26)."""
    if address.name:
        return f"{address.name} <{address.address}>"
    return address.address


def _agent_sender(deps: PipelineDeps) -> str:
    """How the agent's own outgoing messages are recorded as `sender` (D-22, D-26)."""
    return f"{deps.settings.AGENT_SENDER_NAME} <{deps.mailbox_address}>"


async def _record(deps: PipelineDeps, email: Email) -> None:
    await deps.store.record(
        email.id, _format_sender(email.from_address), email.subject, email.received_at
    )


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


async def _process_email(
    deps: PipelineDeps,
    approval_handler: ApprovalHandler,
    message_id: str,
    attempt: int,
) -> None:
    email = await deps.mail.get_message(message_id)
    thread = await deps.mail.get_thread(email.thread_id)
    is_recorded = await deps.store.contains(message_id)

    decision = route(email, is_recorded=is_recorded, thread=thread, signing_key=deps.signing_key)

    if decision.kind is RouteKind.SKIP_OWN:
        return

    if decision.kind is RouteKind.IGNORE_SENT:
        await _record(deps, email)
        log_email_processed(
            logger,
            message_id=email.id,
            sender=_format_sender(email.from_address),
            subject=email.subject,
            primary=None,
            urgent=False,
            actions=[],
            outcome="ignored",
        )
        return

    if decision.kind is RouteKind.APPROVAL_REPLY:
        handled = await approval_handler(deps, email, thread)
        await _record(deps, email)
        log_email_processed(
            logger,
            message_id=email.id,
            sender=_format_sender(email.from_address),
            subject=email.subject,
            primary=None,
            urgent=False,
            actions=list(handled.actions),
            outcome=handled.outcome,
            reason=handled.reason,
        )
        return

    await _classify_and_act(deps, email, thread)


# --------------------------------------------------------------------------
# Classification flow
# --------------------------------------------------------------------------


def _truncate_email(email: Email, max_chars: int) -> tuple[Email, bool]:
    """A copy of `email` with its body truncated for the LLM (D-56); `email` itself if it fits."""
    text, truncated = truncate_for_llm(email.text_body, max_chars)
    if not truncated:
        return email, False
    return email.model_copy(update={"text_body": text}), True


async def _apply_needs_review_label(deps: PipelineDeps, email: Email) -> None:
    """D-43: mark `email` `AI/NeedsReview` after any LLM call's output never validated."""
    await deps.mail.ensure_labels([AI_NEEDS_REVIEW_LABEL])
    await deps.mail.modify_labels(email.id, add=[AI_NEEDS_REVIEW_LABEL])


async def _needs_review(deps: PipelineDeps, email: Email) -> None:
    """D-43: invalid LLM output from the classifier -> no action, `AI/NeedsReview`."""
    await _apply_needs_review_label(deps, email)
    await _record(deps, email)
    log_email_processed(
        logger,
        message_id=email.id,
        sender=_format_sender(email.from_address),
        subject=email.subject,
        primary="",
        urgent=False,
        actions=[],
        outcome="needs_review",
    )


async def _classify_and_act(deps: PipelineDeps, email: Email, thread: Sequence[Email]) -> None:
    settings = deps.settings
    facts = extract_facts(
        email,
        own_address=deps.mailbox_address,
        internal_domains=settings.INTERNAL_DOMAINS,
        allowlist_senders=settings.SPAM_ALLOWLIST_SENDERS,
        allowlist_domains=settings.SPAM_ALLOWLIST_DOMAINS,
        gmail_labels=list(email.label_ids),
    )

    llm_email, truncated = _truncate_email(email, settings.LLM_MAX_INPUT_CHARS)
    if truncated:
        logger.info(
            "process_email.truncated", message_id=email.id, max_chars=settings.LLM_MAX_INPUT_CHARS
        )

    port_facts = PortFacts(items={"facts": facts.to_prompt_lines()})
    try:
        classification = await deps.classifier.classify(llm_email, port_facts)
    except LLMOutputError:
        await _needs_review(deps, email)
        return

    # D-44: spam is decided first. A spam email gets no invitation handling and no
    # other evaluation: `_execute` only moves it to Spam.
    invitation_result = (
        _NO_INVITATION if classification.primary is PrimaryLabel.SPAM else detect_invitation(email)
    )
    if invitation_result.kind is InvitationKind.REQUEST:
        # D-48: a code-detected invitation is a meeting request; its times come from the
        # invitation, not the classifier.
        classification = Classification(
            primary=PrimaryLabel.MEETING_REQUEST, urgent=False, subtype=Subtype.NONE
        )

    invitation_for_policy = (
        invitation_result if invitation_result.kind is not InvitationKind.NOT_INVITATION else None
    )
    plan = decide(
        classification,
        facts,
        sales_decline_requires_approval=settings.SALES_DECLINE_REQUIRES_APPROVAL,
        invitation=invitation_for_policy,
    )

    actions, outcome, reason = await _execute(
        deps, email, llm_email, classification, facts, plan, invitation_result, thread
    )

    await _record(deps, email)
    log_email_processed(
        logger,
        message_id=email.id,
        sender=_format_sender(email.from_address),
        subject=email.subject,
        primary=classification.primary.value,
        urgent=classification.urgent,
        actions=actions,
        outcome=outcome,
        reason=reason,
        deadline_phrase=classification.deadline_phrase,
        deadline_at=classification.deadline_at,
    )


# --------------------------------------------------------------------------
# Executing the ActionPlan
# --------------------------------------------------------------------------


async def _execute(
    deps: PipelineDeps,
    email: Email,
    llm_email: Email,
    classification: Classification,
    facts: EmailFacts,
    plan: ActionPlan,
    invitation_result: InvitationResult,
    thread: Sequence[Email],
) -> tuple[list[str], str, str | None]:
    if plan.move_to_spam:
        # D-44: the facts that fired are logged with the decision, before acting.
        logger.info(
            "process_email.spam_facts", message_id=email.id, facts=list(facts.to_prompt_lines())
        )
        await deps.mail.modify_labels(
            email.id, add=[GMAIL_LABEL_ID_SPAM], remove=[GMAIL_LABEL_ID_INBOX]
        )
        return ["move_to_spam"], "spam", None

    actions: list[str] = []

    if plan.add_labels:
        await deps.mail.ensure_labels(plan.add_labels)
        add = list(plan.add_labels)
        if plan.mark_starred:
            add.append(GMAIL_LABEL_ID_STARRED)
        await deps.mail.modify_labels(email.id, add=add)
        actions.append("label")

    if plan.archive:
        await deps.mail.modify_labels(email.id, remove=[GMAIL_LABEL_ID_INBOX])
        actions.append("archive")

    reason = "; ".join(plan.skip_reasons) if plan.skip_reasons else None
    outcome = "archived" if plan.archive else "labeled"

    if plan.flow is Flow.MEETING:
        result = await _handle_meeting(deps, email, llm_email)
        outcome = await _apply_flow_result(
            deps, email, actions, result, sent="approval_requested", skipped="no_slots_found"
        )
    elif plan.flow is Flow.INVITATION:
        await _handle_invitation(deps, email, invitation_result)
        outcome = "approval_requested"
        actions.append(outcome)
    elif plan.flow is Flow.SALES_DECLINE_AUTO:
        result = await _handle_sales_decline_auto(deps, email, llm_email)
        outcome = await _apply_flow_result(
            deps, email, actions, result, sent="declined", skipped="decline_skipped"
        )
    elif plan.flow is Flow.SALES_DECLINE_APPROVAL:
        result = await _handle_sales_decline_approval(deps, email, llm_email)
        outcome = await _apply_flow_result(
            deps, email, actions, result, sent="approval_requested", skipped="decline_skipped"
        )
    elif plan.flow is Flow.URGENT_INTERNAL_ACK:
        result = await _handle_urgent_internal_ack(deps, email, llm_email, classification, thread)
        outcome = await _apply_flow_result(
            deps, email, actions, result, sent="approval_requested", skipped="ack_skipped"
        )
        actions.append("notify_manager")
    elif plan.flow is Flow.COURTESY_CHECK:
        result, courtesy_reason = await _handle_courtesy_check(
            deps, email, llm_email, classification.primary, thread
        )
        if courtesy_reason is not None:
            # D-39 (task 47): the courtesy decision's own reason overrides whatever
            # (empty, for this flow) `plan.skip_reasons` reason was computed above, so
            # it reaches the `email.processed` audit line for both outcomes.
            reason = courtesy_reason
        outcome = await _apply_flow_result(
            deps,
            email,
            actions,
            result,
            sent="approval_requested",
            skipped="courtesy_skipped",
        )

    if plan.notify_manager and plan.flow is not Flow.URGENT_INTERNAL_ACK:
        # D-40: the automated-sender guard blocked the acknowledgement reply;
        # a plain notification (no token, no action) still goes to the approver.
        assert reason is not None  # policy always sets a skip reason alongside notify_manager
        await _notify_manager_plain(deps, email, reason, branch="automated_sender")
        actions.append("notify_manager")
        outcome = "notified_manager_only"

    return actions, outcome, reason


FlowResult = Literal["sent", "skipped", "needs_review"]
"""What one flow handler did: sent its action, legitimately had nothing to send, or an
`LLMOutputError` means the output never validated (D-43) -- kept distinct from "skipped"
so `_apply_flow_result` can apply `AI/NeedsReview` only for the latter."""


async def _apply_flow_result(
    deps: PipelineDeps,
    email: Email,
    actions: list[str],
    result: FlowResult,
    *,
    sent: str,
    skipped: str,
) -> str:
    """Turn one handler's `FlowResult` into an outcome, appending to `actions` as it goes."""
    if result == "needs_review":
        await _apply_needs_review_label(deps, email)
        actions.append("needs_review")
        return "needs_review"
    outcome = sent if result == "sent" else skipped
    actions.append(outcome)
    return outcome


async def _send_approval(
    deps: PipelineDeps, context: ApprovalContext, request: ApprovalEmailRequest
) -> None:
    approval_email = build_approval_email(context, request)
    sent_id = await deps.mail.send(approval_email.message)
    await deps.store.record(
        sent_id, _agent_sender(deps), approval_email.message.subject, deps.clock.now()
    )


def _approval_context(deps: PipelineDeps, email: Email) -> ApprovalContext:
    settings = deps.settings
    return ApprovalContext(
        approver_address=deps.approver_address,
        sender_name=settings.AGENT_SENDER_NAME,
        signing_key=deps.signing_key,
        business_days=settings.APPROVAL_EXPIRY_BUSINESS_DAYS,
        tz=ZoneInfo(settings.DEFAULT_TIMEZONE),
        sent_at=deps.clock.now(),
        original_email_id=email.id,
        original_sender=_format_sender(email.from_address),
        original_subject=email.subject,
        original_received_at=email.received_at,
    )


async def _send_reply_approval(
    deps: PipelineDeps,
    email: Email,
    body: str,
    *,
    urgent: bool,
    deadline_phrase: str | None = None,
    deadline_at: datetime | None = None,
) -> None:
    text = reply_text(email, body, deps.settings.AGENT_SENDER_NAME)
    action = ReplyAction(
        recipient=reply_recipient(email),
        thread_id=email.thread_id,
        message_id=email.id,
        text=text,
        urgent=urgent,
    )
    context = _approval_context(deps, email)
    request = ReplyEmailRequest(
        action=action, urgent=urgent, deadline_phrase=deadline_phrase, deadline_at=deadline_at
    )
    await _send_approval(deps, context, request)


def _send_plain_notification_message(
    deps: PipelineDeps, email: Email, reason: str | None
) -> OutgoingMessage:
    sender_display = _format_sender(email.from_address)
    subject = f"Urgent message from {sender_display}: {email.subject}"
    lines = [
        f"An urgent internal message from {sender_display} was received.",
        f"Subject: {email.subject}",
    ]
    if reason:
        lines.append(f"No acknowledgement reply was sent: {reason}")
    text = "\n".join(lines)
    return OutgoingMessage(
        to=deps.approver_address,
        subject=subject,
        text_body=text,
        html_body=f"<html><body><pre>{html.escape(text)}</pre></body></html>",
        sender_name=deps.settings.AGENT_SENDER_NAME,
    )


async def _send_plain_notification(deps: PipelineDeps, email: Email, reason: str | None) -> None:
    """D-40: the acknowledgement reply was blocked; notify the approver anyway, no token."""
    message = _send_plain_notification_message(deps, email, reason)
    sent_id = await deps.mail.send(message)
    await deps.store.record(sent_id, _agent_sender(deps), message.subject, deps.clock.now())


# --------------------------------------------------------------------------
# MEETING (ADR-4, D-13, D-14, D-45, D-64)
# --------------------------------------------------------------------------


def _resolve_timezone(extraction: MeetingExtraction, default_timezone: str) -> tuple[str, bool]:
    """`(timezone, assumed)`: the stated zone if valid, else the default (D-1, ADR-4)."""
    if extraction.timezone_stated and extraction.timezone:
        try:
            ZoneInfo(extraction.timezone)
        except ZoneInfoNotFoundError, ValueError:
            return default_timezone, True
        return extraction.timezone, False
    return default_timezone, True


def _guarded_attendees(email: Email, extra_attendees: Sequence[str]) -> tuple[str, ...]:
    """Drop any LLM-returned address not in From/To/Cc (ADR-4's hallucination guardrail)."""
    known = {
        email.from_address.address,
        *(a.address for a in email.to),
        *(a.address for a in email.cc),
    }
    candidates = (address.strip().lower() for address in extra_attendees)
    return tuple(dict.fromkeys(address for address in candidates if address in known))


def _free_busy_search_range(
    now: datetime,
    exact_slots: Sequence[RequestedSlot],
    windows: Sequence[RequestedWindow],
    params: SchedulingParams,
) -> tuple[datetime, datetime]:
    """A generous `free_busy` range covering `now`, every requested time and the horizon (ADR-4).

    `core/scheduling.py` may anchor its own alternative search past the
    horizon computed from `now` (e.g. from the last window's end, task 39,
    or from a single busy requested time); a fixed calendar-day margin well
    beyond `horizon_business_days` (converted generously, ignoring weekends)
    keeps the fetched busy data covering whatever `schedule()` ends up
    needing, without duplicating its business-day arithmetic here.
    """
    margin = timedelta(days=params.horizon_business_days * 2 + 7)
    start = now
    end = now + margin
    for window in windows:
        end = max(end, window.end + margin)
    for slot in exact_slots:
        start = min(start, slot.start)
        end = max(end, slot.start + margin)
    return start, end


async def _handle_meeting(deps: PipelineDeps, email: Email, llm_email: Email) -> FlowResult:
    """MEETING flow: extract, resolve, schedule, send the approval email."""
    settings = deps.settings
    try:
        extraction = await deps.meeting_extractor.extract(llm_email)
    except LLMOutputError as exc:
        logger.warning(
            "process_email.meeting_extraction_failed", message_id=email.id, error=str(exc)
        )
        return "needs_review"

    timezone, timezone_assumed = _resolve_timezone(extraction, settings.DEFAULT_TIMEZONE)
    duration_minutes = extraction.duration_minutes or settings.DEFAULT_MEETING_MINUTES
    duration = timedelta(minutes=duration_minutes)

    resolutions = resolve_expressions(
        extraction.proposals,
        email.received_at,
        timezone,
        duration,
        business_hours_start=settings.BUSINESS_HOURS_START,
        business_hours_end=settings.BUSINESS_HOURS_END,
    )

    exact_slots: list[RequestedSlot] = []
    windows: list[RequestedWindow] = []
    invalid: list[InvalidRequest] = []
    for resolution in resolutions:
        if isinstance(resolution, ResolvedSlot):
            if resolution.source == "exact":
                exact_slots.append(RequestedSlot(start=resolution.start, phrase=resolution.phrase))
            else:
                # Every window resolution is kept (task 39, F2): a multi-day window is
                # already several same-phrase entries (one per business day,
                # `core/dates.py`), and a second, distinct window proposal is its own
                # entry too — none are dropped in favour of "the first window".
                windows.append(
                    RequestedWindow(
                        start=resolution.start, end=resolution.end, phrase=resolution.phrase
                    )
                )
        elif isinstance(resolution, InvalidResolution):
            invalid.append(InvalidRequest(phrase=resolution.phrase))
        # else: NoTimeResolution -> nothing to schedule for this expression.

    extra_attendees = _guarded_attendees(email, extraction.extra_attendees)
    attendees = tuple(
        dict.fromkeys([email.from_address.address, *extra_attendees, deps.approver_address])
    )

    now = deps.clock.now()
    params = SchedulingParams(
        timezone=timezone,
        business_hours_start=settings.BUSINESS_HOURS_START,
        business_hours_end=settings.BUSINESS_HOURS_END,
        default_meeting_minutes=settings.DEFAULT_MEETING_MINUTES,
        horizon_business_days=settings.SCHEDULING_HORIZON_BUSINESS_DAYS,
        min_lead_minutes=settings.MEETING_MIN_LEAD_MINUTES,
        max_options=settings.MEETING_MAX_OPTIONS,
    )
    request = SchedulingRequest(
        exact_slots=tuple(exact_slots),
        windows=tuple(windows),
        duration_minutes=duration_minutes,
        attendee_count=len(attendees),
        invalid=tuple(invalid),
    )

    search_start, search_end = _free_busy_search_range(now, exact_slots, windows, params)
    busy = await deps.calendar.free_busy(search_start, search_end)
    scheduling_busy = [
        SchedulingBusyInterval(start=b.start, end=b.end, title=b.title) for b in busy
    ]

    scheduling_result = schedule(request, scheduling_busy, params, now)
    if not scheduling_result.options:
        # `CalendarAction.options` requires at least one; nothing to propose.
        logger.warning("process_email.meeting_no_slots", message_id=email.id)
        return "skipped"

    context = _approval_context(deps, email)
    calendar_request = CalendarEmailRequest(
        title=extraction.title or email.subject,
        options=scheduling_result.options,
        attendees=attendees,
        location=extraction.location,
        invalid_phrases=tuple(item.phrase for item in scheduling_result.invalid),
        too_many_attendees=SchedulingFlag.TOO_MANY_ATTENDEES in scheduling_result.flags,
        longer_than_business_day=(
            SchedulingFlag.EVENT_LONGER_THAN_BUSINESS_DAY in scheduling_result.flags
        ),
        timezone_assumed=timezone_assumed,
        requester_timezone=None if timezone_assumed else timezone,
        requested_phrases=tuple(
            dict.fromkeys(p.phrase for p in extraction.proposals if p.kind != "none")
        ),
        decline_body=meeting_unavailable_body(email),
    )
    await _send_approval(deps, context, calendar_request)
    return "sent"


# --------------------------------------------------------------------------
# INVITATION (D-48, D-55)
# --------------------------------------------------------------------------


def _invitation_mark(
    start: datetime,
    end: datetime,
    busy: Sequence[SchedulingBusyInterval],
    params: SchedulingParams,
) -> tuple[SlotMark | None, tuple[str, ...]]:
    """DOUBLE_BOOKING / OUTSIDE_BUSINESS_HOURS for a received invitation, or `None` if clear."""
    zone = ZoneInfo(params.timezone)
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    day_start = local_start.replace(
        hour=params.business_hours_start, minute=0, second=0, microsecond=0
    )
    day_end = local_start.replace(hour=params.business_hours_end, minute=0, second=0, microsecond=0)
    fits_business_hours = (
        local_start.weekday() < 5
        and local_start.date() == local_end.date()
        and day_start <= local_start
        and local_end <= day_end
    )
    if not fits_business_hours:
        return SlotMark.OUTSIDE_BUSINESS_HOURS, ()

    conflicts = tuple(
        f"{b.start.astimezone(zone):%H:%M}-{b.end.astimezone(zone):%H:%M} {b.title or 'Busy'}"
        for b in busy
        if start < b.end and b.start < end
    )
    if conflicts:
        return SlotMark.DOUBLE_BOOKING, conflicts
    return None, ()


async def _handle_invitation(
    deps: PipelineDeps, email: Email, invitation_result: InvitationResult
) -> None:
    invitation = invitation_result.invitation
    if invitation is None:  # pragma: no cover - policy only selects this flow when it's set
        return

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
    # D-48: the invitation's own event is already in the calendar; it's not a conflict.
    busy = await deps.calendar.free_busy(
        invitation.start, invitation.end, exclude_ical_uid=invitation.uid
    )
    scheduling_busy = [
        SchedulingBusyInterval(start=b.start, end=b.end, title=b.title) for b in busy
    ]
    mark, conflicts = _invitation_mark(invitation.start, invitation.end, scheduling_busy, params)

    action = InvitationAction(
        ical_uid=invitation.uid,
        start=invitation.start,
        end=invitation.end,
        organizer=invitation.organizer or None,
        title=invitation.title or None,
        location=invitation.location or None,
    )
    context = _approval_context(deps, email)
    await _send_approval(
        deps, context, InvitationEmailRequest(action=action, mark=mark, conflicts=conflicts)
    )


# --------------------------------------------------------------------------
# SALES_OUTREACH (D-38)
# --------------------------------------------------------------------------


async def _handle_sales_decline_auto(
    deps: PipelineDeps, email: Email, llm_email: Email
) -> FlowResult:
    try:
        body = await deps.reply_drafter.draft_sales_decline(llm_email)
    except LLMOutputError as exc:
        logger.warning("process_email.sales_decline_failed", message_id=email.id, error=str(exc))
        return "needs_review"
    message = build_reply(email, body, deps.settings.AGENT_SENDER_NAME)
    sent_id = await deps.mail.send(message)
    await deps.store.record(sent_id, _agent_sender(deps), message.subject, deps.clock.now())
    return "sent"


async def _handle_sales_decline_approval(
    deps: PipelineDeps, email: Email, llm_email: Email
) -> FlowResult:
    try:
        body = await deps.reply_drafter.draft_sales_decline(llm_email)
    except LLMOutputError as exc:
        logger.warning("process_email.sales_decline_failed", message_id=email.id, error=str(exc))
        return "needs_review"
    await _send_reply_approval(deps, email, body, urgent=False)
    return "sent"


# --------------------------------------------------------------------------
# TASK urgent+internal (D-40) and courtesy replies (D-39)
# --------------------------------------------------------------------------


# D-40's drafting hint for `AcknowledgementDrafter` (updated): names nobody -- not the
# manager, not an assistant -- since `AGENT_SENDER_NAME` is the manager's own name in one
# mode and an assistant's name in the other; and no time promise (unlike the earlier "the
# manager will look at it today").
URGENT_INTERNAL_HINT = "urgent, internal sender; received and flagged as urgent"


def _agent_replied_in_thread(thread: Sequence[Email], current_id: str, signing_key: str) -> bool:
    """Whether the agent already sent a real reply in this thread (D-39/D-40's "one
    reply per thread" rule): any `SENT` message other than `current_id` itself and other
    than one of our own approval emails (D-47) -- an approval request is not a reply."""
    approval_ids = {message.id for message in find_approval_emails(thread, signing_key)}
    return any(
        GMAIL_LABEL_ID_SENT in message.label_ids
        for message in thread
        if message.id != current_id and message.id not in approval_ids
    )


async def _notify_manager_plain(
    deps: PipelineDeps, email: Email, reason: str, *, branch: str
) -> None:
    """D-40: send the plain (no-token) notification and log the shared `notify_manager`
    event, naming which branch of the flow is responsible (task 43)."""
    await _send_plain_notification(deps, email, reason)
    logger.info("process_email.notify_manager", message_id=email.id, branch=branch)


async def _handle_urgent_internal_ack(
    deps: PipelineDeps,
    email: Email,
    llm_email: Email,
    classification: Classification,
    thread: Sequence[Email],
) -> FlowResult:
    """D-40: the flow already decided the reply is required; the drafter never decides
    "warranted". Every branch logs `notify_manager` (task 43); only the approval-sent
    branch relies on the approval email itself as that notification -- the others send a
    plain one since no acknowledgement is going out."""
    approver = deps.approver_address.strip().lower()
    if email.from_address.address.strip().lower() == approver:
        await _notify_manager_plain(
            deps, email, "the sender is the approver address", branch="sender_is_approver"
        )
        return "skipped"

    if _agent_replied_in_thread(thread, email.id, deps.signing_key):
        await _notify_manager_plain(
            deps, email, "the agent already replied in this thread", branch="already_replied"
        )
        return "skipped"

    hint = URGENT_INTERNAL_HINT
    try:
        body = await deps.acknowledgement_drafter.draft_acknowledgement(llm_email, hint)
    except LLMOutputError as exc:
        logger.warning(
            "process_email.urgent_internal_ack_failed", message_id=email.id, error=str(exc)
        )
        await _notify_manager_plain(
            deps, email, "the acknowledgement draft failed", branch="draft_failed"
        )
        return "needs_review"

    if not body.strip():
        logger.warning("process_email.urgent_internal_ack_empty", message_id=email.id)
        await _notify_manager_plain(
            deps, email, "the acknowledgement draft was empty", branch="draft_empty"
        )
        return "needs_review"

    await _send_reply_approval(
        deps,
        email,
        body,
        urgent=True,
        deadline_phrase=classification.deadline_phrase,
        deadline_at=classification.deadline_at,
    )
    logger.info("process_email.notify_manager", message_id=email.id, branch="approval_sent")
    return "sent"


async def _handle_courtesy_check(
    deps: PipelineDeps,
    email: Email,
    llm_email: Email,
    primary: PrimaryLabel,
    thread: Sequence[Email],
) -> tuple[FlowResult, str | None]:
    """D-39 order step 3: one courtesy reply per thread -- when the agent already sent a
    real reply here, the LLM call is skipped entirely (same check as D-40's urgent-internal
    ack, `_agent_replied_in_thread`).

    Returns `(result, reason)`: `reason` is the courtesy decision's own `CourtesyDraft.reason`
    (task 47) for both outcomes of the LLM call -- warranted and not warranted -- so
    `_execute` can put it on the `email.processed` audit line; `None` when the LLM was never
    called (thread-dedup skip) or its output never validated.
    """
    if _agent_replied_in_thread(thread, email.id, deps.signing_key):
        logger.info(
            "process_email.courtesy_already_replied",
            message_id=email.id,
            reason="the agent already replied in this thread",
        )
        return "skipped", None
    try:
        draft = await deps.courtesy_drafter.draft(llm_email, primary)
    except LLMOutputError as exc:
        logger.warning("process_email.courtesy_check_failed", message_id=email.id, error=str(exc))
        return "needs_review", None
    if not draft.warranted:
        logger.info(
            "process_email.courtesy_not_warranted", message_id=email.id, reason=draft.reason
        )
        return "skipped", draft.reason
    await _send_reply_approval(deps, email, draft.body, urgent=False)
    logger.info("process_email.courtesy_warranted", message_id=email.id, reason=draft.reason)
    return "sent", draft.reason


__all__ = [
    "URGENT_INTERNAL_HINT",
    "ApprovalHandled",
    "ApprovalHandler",
    "Handler",
    "make_email_handler",
]

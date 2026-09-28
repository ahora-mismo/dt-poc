"""Approval reply handling (D-9, D-11, D-12, D-34, D-35, D-39, D-48, D-49).

`handle_approval_reply` is the single entry point: given the manager's
reply and its Gmail thread, it extracts and verifies the approval token
(falling back to the latest approval email in the thread when the reply
carries no quoted token, D-34), checks the token is bound to the *latest*
approval email in the thread (not an older, superseded one), and then
executes -- or explains why it didn't execute -- the signed action.

No pending-approval state is stored anywhere (D-5): every check reads
only the token itself and the Gmail thread passed in by the caller.

**Verification order (D-34): signature, then thread binding, then
expiry.** `approval_token.verify` conflates signature and expiry checks
into one `InvalidToken` result, so a token that is both superseded *and*
expired would otherwise report as merely "expired" and never reach the
binding check. This module verifies twice: once with a `now` far enough
in the past that expiry can never trigger (to learn the token's `jti`
and `action` regardless of its real expiry), and -- only once binding
passes -- again with the real clock to test expiry.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dt_agent.core.approval_email import (
    ApprovalContext,
    CalendarEmailRequest,
    InvitationEmailRequest,
    ReplyEmailRequest,
    build_approval_email,
)
from dt_agent.core.approval_token import (
    ApprovalAction,
    CalendarAction,
    CalendarOption,
    InvalidToken,
    InvitationAction,
    ReplyAction,
    VerifiedApproval,
    extract_token,
    verify,
)
from dt_agent.core.reply_parser import ReplyDecision, parse_reply
from dt_agent.core.routing import latest_approval_email
from dt_agent.core.scheduling import (
    BusyInterval as SchedulingBusyInterval,
)
from dt_agent.core.scheduling import (
    RequestedSlot,
    SchedulingFlag,
    SchedulingParams,
    SchedulingRequest,
    SchedulingResult,
    schedule,
)
from dt_agent.domain.email import Email
from dt_agent.domain.labels import GMAIL_LABEL_ID_INBOX, GMAIL_LABEL_ID_SPAM
from dt_agent.logging import get_logger
from dt_agent.pipeline.deps import PipelineDeps
from dt_agent.pipeline.process_email import ApprovalHandled, ApprovalHandler
from dt_agent.pipeline.replies import build_reply
from dt_agent.ports.calendar import InvitationEvent, NewEvent
from dt_agent.ports.mail import OutgoingMessage

logger = get_logger(__name__)

# A token this far in the past can never be later than any real `exp`
# claim, so `verify(..., now=_NEVER_EXPIRES)` never fails on expiry: only
# a bad signature / wrong algorithm / malformed token makes it fail. Used
# to recover a token's `jti`/`action` for the D-34 binding check even when
# the token is (also) expired. Mirrors `core/routing.py`'s own trick.
_NEVER_EXPIRES = datetime.min.replace(tzinfo=UTC)

# D-11/D-45: how far past `now` this module looks for alternative slots
# when re-issuing a calendar approval. `core/scheduling.py`'s own horizon
# math is private to that module (it anchors differently per request
# shape), so this is this module's own, deliberately generous choice, not
# a reuse of ADR-4's `SCHEDULING_HORIZON_BUSINESS_DAYS` search window.
_REISSUE_SEARCH_DAYS = 21

# D-49: marks a help reply so a later call can tell "already sent" from
# "not sent yet" by reading the thread -- no state is stored (D-5).
_HELP_MARKER = "dt-agent:approval-help"


def _scheduling_params(deps: PipelineDeps) -> SchedulingParams:
    return SchedulingParams(
        timezone=deps.settings.DEFAULT_TIMEZONE,
        business_hours_start=deps.settings.BUSINESS_HOURS_START,
        business_hours_end=deps.settings.BUSINESS_HOURS_END,
        default_meeting_minutes=deps.settings.DEFAULT_MEETING_MINUTES,
        horizon_business_days=deps.settings.SCHEDULING_HORIZON_BUSINESS_DAYS,
        min_lead_minutes=deps.settings.MEETING_MIN_LEAD_MINUTES,
        max_options=deps.settings.MEETING_MAX_OPTIONS,
    )


async def _send_and_record(deps: PipelineDeps, message: OutgoingMessage) -> str:
    """`mail.send` then record the returned id immediately (D-22)."""
    sent_id = await deps.mail.send(message)
    await deps.store.record(sent_id, deps.mailbox_address, message.subject, deps.clock.now())
    return sent_id


def _reply_notice(deps: PipelineDeps, email: Email, body: str) -> OutgoingMessage:
    """A plain threaded reply to the manager's own message, in the approval thread."""
    subject = email.subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message_id_header = email.header("Message-ID")
    references = email.header("References")
    if message_id_header:
        references = f"{references} {message_id_header}" if references else message_id_header
    text = f"{body}\n\n{deps.settings.AGENT_SENDER_NAME}\n"
    return OutgoingMessage(
        to=deps.approver_address,
        subject=subject,
        text_body=text,
        html_body=f"<html><body><pre>{html.escape(text)}</pre></body></html>",
        sender_name=deps.settings.AGENT_SENDER_NAME,
        thread_id=email.thread_id,
        in_reply_to=message_id_header,
        references=references,
    )


async def _context_for_reissue(
    deps: PipelineDeps, email: Email, verified: VerifiedApproval, reissue_reason: str
) -> ApprovalContext:
    original = await deps.mail.get_message(verified.original_email_id)
    message_id_header = email.header("Message-ID")
    return ApprovalContext(
        approver_address=deps.approver_address,
        sender_name=deps.settings.AGENT_SENDER_NAME,
        signing_key=deps.signing_key,
        business_days=deps.settings.APPROVAL_EXPIRY_BUSINESS_DAYS,
        tz=ZoneInfo(deps.settings.DEFAULT_TIMEZONE),
        sent_at=deps.clock.now(),
        original_email_id=verified.original_email_id,
        original_sender=original.from_address.address,
        original_subject=original.subject,
        original_received_at=original.received_at,
        thread_id=email.thread_id,
        in_reply_to=message_id_header,
        reissue_reason=reissue_reason,
    )


async def _recompute_calendar_options(
    deps: PipelineDeps,
    exact_slots: Sequence[RequestedSlot],
    duration_minutes: int,
    attendee_count: int,
) -> SchedulingResult:
    now = deps.clock.now()
    params = _scheduling_params(deps)
    # The busy list must reach past the *latest requested slot*, not just
    # `now` + the search window: a slot proposed far in the future (e.g.
    # "let's meet next month") that got booked in the meantime must still
    # show up as busy when `schedule()` re-classifies it, even though the
    # window for finding *alternatives* only needs to extend from there.
    latest_slot = max((slot.start for slot in exact_slots), default=now)
    window_end = max(now, latest_slot) + timedelta(days=_REISSUE_SEARCH_DAYS)
    port_busy = await deps.calendar.free_busy(now, window_end)
    busy = tuple(
        SchedulingBusyInterval(start=entry.start, end=entry.end, title=entry.title)
        for entry in port_busy
    )
    request_ = SchedulingRequest(
        exact_slots=tuple(exact_slots),
        duration_minutes=duration_minutes,
        attendee_count=attendee_count,
    )
    return schedule(request_, busy, params, now)


async def _reissue_calendar(
    deps: PipelineDeps,
    email: Email,
    verified: VerifiedApproval,
    action: CalendarAction,
    exact_slots: Sequence[RequestedSlot],
    reissue_reason: str,
) -> None:
    duration_minutes = int((action.options[0].end - action.options[0].start).total_seconds() // 60)
    result = await _recompute_calendar_options(
        deps, exact_slots, duration_minutes, len(action.attendees) + 1
    )
    context = await _context_for_reissue(deps, email, verified, reissue_reason)
    request = CalendarEmailRequest(
        title=action.title,
        options=result.options,
        attendees=action.attendees,
        location=action.location,
        too_many_attendees=SchedulingFlag.TOO_MANY_ATTENDEES in result.flags,
        longer_than_business_day=SchedulingFlag.EVENT_LONGER_THAN_BUSINESS_DAY in result.flags,
        decline_body=action.decline_body,
    )
    approval = build_approval_email(context, request)
    await _send_and_record(deps, approval.message)


async def _reissue_reply(
    deps: PipelineDeps,
    email: Email,
    verified: VerifiedApproval,
    action: ReplyAction,
    reissue_reason: str,
) -> None:
    context = await _context_for_reissue(deps, email, verified, reissue_reason)
    approval = build_approval_email(context, ReplyEmailRequest(action=action, urgent=action.urgent))
    await _send_and_record(deps, approval.message)


async def _reissue_invitation(
    deps: PipelineDeps,
    email: Email,
    verified: VerifiedApproval,
    action: InvitationAction,
    reissue_reason: str,
) -> None:
    context = await _context_for_reissue(deps, email, verified, reissue_reason)
    approval = build_approval_email(context, InvitationEmailRequest(action=action))
    await _send_and_record(deps, approval.message)


async def _answer_invitation(
    deps: PipelineDeps, action: InvitationAction, *, accepted: bool
) -> None:
    event_id = await deps.calendar.find_event_by_ical_uid(action.ical_uid)
    if event_id is None:
        event_id = await deps.calendar.import_event(
            InvitationEvent(
                uid=action.ical_uid,
                start=action.start,
                end=action.end,
                title=action.title,
                location=action.location,
                organizer=action.organizer,
            )
        )
    await deps.calendar.respond_to_event(event_id, accepted=accepted)


def _has_sent_help_reply(thread: Sequence[Email]) -> bool:
    return any(_HELP_MARKER in message.text_body for message in thread)


async def _send_help_reply(deps: PipelineDeps, email: Email) -> None:
    body = (
        f"[{_HELP_MARKER}]\n\n"
        "Sorry, I didn't understand that reply. Please answer APPROVE, "
        "APPROVE <option number>, or REJECT."
    )
    await _send_and_record(deps, _reply_notice(deps, email, body))


def _event_attendees(
    action_attendees: Sequence[str], approver: str, mailbox: str
) -> tuple[str, ...]:
    seen = {mailbox.lower()}
    result: list[str] = []
    for address in (*action_attendees, approver):
        lowered = address.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(address)
    return tuple(result)


def _find_option(action: CalendarAction, number: int) -> CalendarOption:
    for option in action.options:
        if option.number == number:
            return option
    raise ValueError(f"no option numbered {number} in {action.title!r}")


async def _send_reply_action(deps: PipelineDeps, action: ReplyAction) -> None:
    original = await deps.mail.get_message(action.message_id)
    message_id_header = original.header("Message-ID")
    references = original.header("References")
    if message_id_header:
        references = f"{references} {message_id_header}" if references else message_id_header
    subject = original.subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message = OutgoingMessage(
        to=action.recipient,
        subject=subject,
        text_body=action.text,
        html_body=f"<html><body><pre>{html.escape(action.text)}</pre></body></html>",
        sender_name=deps.settings.AGENT_SENDER_NAME,
        thread_id=action.thread_id,
        in_reply_to=message_id_header,
        references=references,
    )
    await _send_and_record(deps, message)


async def _decode_latest(
    deps: PipelineDeps, thread: Sequence[Email]
) -> tuple[Email, VerifiedApproval] | None:
    latest = latest_approval_email(thread, deps.signing_key)
    if latest is None:
        return None
    token = extract_token(latest.text_body)
    if token is None:
        return None
    decoded = verify(token, deps.signing_key, now=_NEVER_EXPIRES)
    if not isinstance(decoded, VerifiedApproval):
        return None
    return latest, decoded


def _option_count(action: ApprovalAction) -> int:
    if isinstance(action, CalendarAction):
        return len(action.options)
    return 1


async def handle_approval_reply(
    deps: PipelineDeps, email: Email, thread: Sequence[Email]
) -> ApprovalHandled:
    """Verify and act on one approval reply (D-9, D-11, D-12, D-34, D-35, D-39, D-48, D-49)."""
    others = [message for message in thread if message.id != email.id]

    decoded_latest = await _decode_latest(deps, others)
    if decoded_latest is None:
        # Routing (D-47) only sends a message here when its thread carries
        # a message with a token that verifies with our key; this branch
        # should be unreachable in production.
        raise ValueError(f"no valid approval email found in thread of {email.id!r}")
    latest_email, latest_verified = decoded_latest
    latest_action = latest_verified.action

    parsed = parse_reply(email, option_count=_option_count(latest_action))

    if parsed.decision == ReplyDecision.IGNORED:
        logger.info("approval.ignored", message_id=email.id, reason="auto_submitted")
        return ApprovalHandled(outcome="ignored", actions=(), reason="auto_submitted")

    token = extract_token(parsed.quoted_text)
    if token is None:
        latest_token = extract_token(latest_email.text_body)
        assert latest_token is not None  # guaranteed by `_decode_latest`
        token = latest_token

    ignoring_expiry = verify(token, deps.signing_key, now=_NEVER_EXPIRES)
    if isinstance(ignoring_expiry, InvalidToken):
        logger.info("approval.invalid", message_id=email.id, reason=ignoring_expiry.reason.value)
        await deps.mail.modify_labels(
            email.id, add=[GMAIL_LABEL_ID_SPAM], remove=[GMAIL_LABEL_ID_INBOX]
        )
        return ApprovalHandled(
            outcome="invalid", actions=("labelled_spam",), reason=ignoring_expiry.reason.value
        )

    if ignoring_expiry.jti != latest_verified.jti:
        logger.info("approval.superseded", message_id=email.id)
        notice = _reply_notice(
            deps,
            email,
            "This approval request was superseded by a newer one. Please reply to the "
            "most recent [Approval] email in this thread.",
        )
        await _send_and_record(deps, notice)
        return ApprovalHandled(outcome="superseded", actions=("notice",))

    real_result = verify(token, deps.signing_key, now=deps.clock.now())
    if isinstance(real_result, InvalidToken):
        # Signature/binding already passed above, so the only way `verify`
        # can still fail is expiry (D-11).
        logger.info("approval.expired", message_id=email.id)
        notice = _reply_notice(
            deps,
            email,
            "This approval request was not acted on: it expired before you replied.",
        )
        await _send_and_record(deps, notice)
        actions = ["notice"]

        action = latest_action
        if isinstance(action, CalendarAction):
            now = deps.clock.now()
            future = [option for option in action.options if option.start >= now]
            if future:
                slots = [RequestedSlot(start=option.start) for option in future]
                await _reissue_calendar(
                    deps,
                    email,
                    latest_verified,
                    action,
                    slots,
                    "the previous approval request expired",
                )
                actions.append("reissued")
        elif isinstance(action, ReplyAction):
            await _reissue_reply(
                deps, email, latest_verified, action, "the previous approval request expired"
            )
            actions.append("reissued")
        else:
            await _reissue_invitation(
                deps, email, latest_verified, action, "the previous approval request expired"
            )
            actions.append("reissued")

        return ApprovalHandled(outcome="expired_reissued", actions=tuple(actions))

    verified = real_result

    if parsed.decision in (ReplyDecision.UNCLEAR, ReplyDecision.INVALID_OPTION):
        if _has_sent_help_reply(others):
            logger.info("approval.unclear", message_id=email.id, help_sent=False)
            return ApprovalHandled(outcome="unclear", actions=())
        logger.info("approval.unclear", message_id=email.id, help_sent=True)
        await _send_help_reply(deps, email)
        return ApprovalHandled(outcome="unclear", actions=("help_reply",))

    if parsed.decision == ReplyDecision.REJECT:
        action = verified.action
        if isinstance(action, InvitationAction):
            await _answer_invitation(deps, action, accepted=False)
            logger.info("approval.rejected", message_id=email.id, kind="invitation")
            return ApprovalHandled(outcome="rejected", actions=("declined",))
        if isinstance(action, CalendarAction) and action.decline_body is not None:
            # A meeting request from an email's text: the requester gets the kind
            # "not available" reply in the original thread.
            original = await deps.mail.get_message(verified.original_email_id)
            await _send_and_record(
                deps, build_reply(original, action.decline_body, deps.settings.AGENT_SENDER_NAME)
            )
            logger.info("approval.rejected", message_id=email.id, kind="calendar")
            return ApprovalHandled(outcome="rejected", actions=("decline_reply_sent",))
        logger.info("approval.rejected", message_id=email.id)
        return ApprovalHandled(outcome="rejected", actions=())

    # APPROVE
    action = verified.action
    assert parsed.option is not None

    if isinstance(action, ReplyAction):
        await _send_reply_action(deps, action)
        logger.info("approval.approved", message_id=email.id, kind="reply")
        return ApprovalHandled(outcome="approved", actions=("reply_sent",))

    if isinstance(action, InvitationAction):
        await _answer_invitation(deps, action, accepted=True)
        logger.info("approval.approved", message_id=email.id, kind="invitation")
        return ApprovalHandled(outcome="approved", actions=("invitation_accepted",))

    option = _find_option(action, parsed.option)
    now = deps.clock.now()
    if option.start < now:
        slots = [RequestedSlot(start=o.start) for o in action.options if o.start >= now] or [
            RequestedSlot(start=option.start)
        ]
        await _reissue_calendar(
            deps, email, verified, action, slots, "the approved time has already passed"
        )
        logger.info(
            "approval.approved", message_id=email.id, kind="calendar", reissue="option_passed"
        )
        return ApprovalHandled(outcome="approved", actions=("reissued_option_passed",))

    busy = await deps.calendar.free_busy(option.start, option.end)
    if busy:
        await _reissue_calendar(
            deps,
            email,
            verified,
            action,
            [RequestedSlot(start=option.start)],
            "the requested slot was booked in the meantime",
        )
        logger.info("approval.approved", message_id=email.id, kind="calendar", reissue="slot_taken")
        return ApprovalHandled(outcome="approved", actions=("reissued_slot_taken",))

    event = NewEvent(
        title=action.title,
        start=option.start,
        end=option.end,
        timezone=deps.settings.DEFAULT_TIMEZONE,
        attendees=_event_attendees(action.attendees, deps.approver_address, deps.mailbox_address),
        location=action.location,
    )
    await deps.calendar.insert_event(event)
    logger.info("approval.approved", message_id=email.id, kind="calendar")
    return ApprovalHandled(outcome="approved", actions=("event_created",))


# Structural check, not a test: `handle_approval_reply` is the `ApprovalHandler`
# the per-email flow is wired with (task 24).
_handler_check: ApprovalHandler = handle_approval_reply

__all__ = ["ApprovalHandled", "handle_approval_reply"]

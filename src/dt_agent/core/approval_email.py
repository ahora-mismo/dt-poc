"""Approval email builder (D-8, D-11, D-12, D-13, D-14, D-32, D-45, D-48, D-55, D-56, D-64).

Turns one drafted action (a reply, a calendar meeting, or an answer to a
received invitation) into the `OutgoingMessage` sent to the approver
(D-32): a plain-text and an HTML part, both carrying the same signed
approval token (D-8), a summary of the original email, exactly what will
be executed, the numbered options (D-13/D-14) when there are any, the
notes and flags D-1/D-45/D-55/ADR-4 call for, the deadline, and how to
answer. Re-issue variants (D-11, D-12) reuse the approval thread and state
the reason.

This module never decides *what* to propose (that's `core/scheduling.py`
and `core/dates.py`) or whether an action is warranted; it only renders
one already-decided action plus its display metadata into an email and
signs it via `core/approval_token.py`. The exact wording is left to the
implementation (D-32) -- callers and tests should rely on the facts
present (options, marks, notes, deadline, token block), not on phrasing.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from dt_agent.core.approval_token import (
    ApprovalAction,
    CalendarAction,
    CalendarOption,
    InvitationAction,
    ReplyAction,
    compute_expiry,
    issue,
    render_token_block,
)
from dt_agent.domain.meeting import SlotMark, SlotOption
from dt_agent.ports.mail import OutgoingMessage

# How "OUTSIDE_REQUESTED_INTERVAL" (an enum member name, no spaces) is shown
# in the email -- the plan's literal wording (D-45) reads "OUTSIDE THE
# REQUESTED INTERVAL", with "THE" inserted.
_MARK_TEXT: dict[SlotMark, str] = {
    SlotMark.DOUBLE_BOOKING: "DOUBLE BOOKING",
    SlotMark.OUTSIDE_BUSINESS_HOURS: "OUTSIDE BUSINESS HOURS",
    SlotMark.OUTSIDE_REQUESTED_INTERVAL: "OUTSIDE THE REQUESTED INTERVAL",
    SlotMark.INVALID: "INVALID",
}

# D-55: "the approval email highlights the urgency at the top" for a meeting
# starting soon. The plan gives an example ("Starts in 55 minutes") but no
# exact cutoff; three hours is this module's own choice (reported as a
# deviation/assumption).
URGENCY_THRESHOLD = timedelta(hours=3)

_KNOWN_LIMITATION = (
    "Known limitation: only the manager's calendar was checked; other "
    "attendees' availability is unknown."
)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalContext:
    """Everything about the surrounding email flow, not the action itself.

    `thread_id`/`in_reply_to` (with `reissue_reason`) make this a re-issue
    in an existing approval thread (D-11: expired; D-12: slot taken
    meanwhile); left `None`, the approval email starts a new thread.
    """

    approver_address: str
    sender_name: str
    signing_key: str
    business_days: int
    tz: ZoneInfo
    sent_at: datetime
    original_email_id: str
    original_sender: str
    original_subject: str
    original_received_at: datetime
    thread_id: str | None = None
    in_reply_to: str | None = None
    reissue_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReplyEmailRequest:
    """Approve sending `action.text` to `action.recipient` (D-38, D-39, D-40).

    `deadline_phrase`/`deadline_at` (D-24, task 47) come straight from the
    classification; only an urgent request ever shows them (`_reply_body`)
    -- a non-urgent reply's approval email is unchanged even if they're set.
    """

    action: ReplyAction
    urgent: bool = False
    deadline_phrase: str | None = None
    deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CalendarEmailRequest:
    """Approve creating a meeting for one of `options` (D-13, D-14, D-45, ADR-4).

    `options` carries the display detail (`phrase`, `mark`, `conflicts`,
    D-13/D-14/D-64) that `approval_token.CalendarOption` doesn't; the
    token's action is derived from it. `requested_phrases` is what the
    requester asked, in their own words (D-13); `decline_body` is the reply
    the requester gets on REJECT.
    """

    title: str
    options: tuple[SlotOption, ...]
    attendees: tuple[str, ...] = ()
    location: str | None = None
    invalid_phrases: tuple[str, ...] = ()
    too_many_attendees: bool = False
    longer_than_business_day: bool = False
    timezone_assumed: bool = False
    requester_timezone: str | None = None
    requested_phrases: tuple[str, ...] = ()
    decline_body: str | None = None


@dataclass(frozen=True, slots=True)
class InvitationEmailRequest:
    """Approve accepting/declining a received invitation (D-48): no alternatives."""

    action: InvitationAction
    mark: SlotMark | None = None
    conflicts: tuple[str, ...] = field(default=())


ApprovalEmailRequest = ReplyEmailRequest | CalendarEmailRequest | InvitationEmailRequest


@dataclass(frozen=True, slots=True)
class ApprovalEmail:
    """What `build_approval_email` returns: the message plus its token's `jti`."""

    message: OutgoingMessage
    jti: str


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def build_approval_email(context: ApprovalContext, request: ApprovalEmailRequest) -> ApprovalEmail:
    """Render `request` into a signed approval email (D-32).

    The `h` claim hashes the body exactly as shown, excluding the token
    block (which can't hash itself, D-32); the block is appended to both
    the text and HTML parts afterward, so both carry the same token (D-8).
    """
    subject = _subject(context, request)
    body = _body(context, request)

    jti = uuid4().hex
    action = _to_action(request)
    token = issue(
        key=context.signing_key,
        sent_at=context.sent_at,
        business_days=context.business_days,
        tz=context.tz,
        original_email_id=context.original_email_id,
        action=action,
        shown_text=body,
        jti=jti,
    )

    text_body = f"{body}\n\n{render_token_block(token)}"
    html_body = f"<html><body><pre>{html.escape(text_body)}</pre></body></html>"

    message = OutgoingMessage(
        to=context.approver_address,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        sender_name=context.sender_name,
        thread_id=context.thread_id,
        in_reply_to=context.in_reply_to,
        references=context.in_reply_to,
    )
    return ApprovalEmail(message=message, jti=jti)


def _to_action(request: ApprovalEmailRequest) -> ApprovalAction:
    if isinstance(request, ReplyEmailRequest):
        return request.action
    if isinstance(request, InvitationEmailRequest):
        return request.action
    return CalendarAction(
        title=request.title,
        options=tuple(
            CalendarOption(number=option.number, start=option.start, end=option.end)
            for option in request.options
        ),
        attendees=request.attendees,
        location=request.location,
        decline_body=request.decline_body,
    )


def _subject(context: ApprovalContext, request: ApprovalEmailRequest) -> str:
    sender = context.original_sender
    if isinstance(request, ReplyEmailRequest):
        if request.urgent:
            # D-40: this approval email doubles as the manager notification, so the
            # subject reads notification-first, not "reply to X".
            return (
                f"[Approval] URGENT from {sender}: {context.original_subject} — reply "
                "APPROVE to send the acknowledgement"
            )
        return f"[Approval] Reply to {sender}"
    if isinstance(request, CalendarEmailRequest):
        return f"[Approval] Meeting with {sender}: {request.title}"
    action = request.action
    title = action.title or "invitation"
    organizer = action.organizer or sender
    return f"[Approval] Respond to invitation from {organizer}: {title}"


def _body(context: ApprovalContext, request: ApprovalEmailRequest) -> str:
    lines: list[str] = []

    urgency = _urgency_line(context, request)
    if urgency:
        lines.extend([urgency, ""])

    if context.reissue_reason:
        lines.extend([f"This replaces an earlier approval request: {context.reissue_reason}", ""])

    lines.extend(
        [
            "Original email:",
            f"From: {context.original_sender}",
            f"Subject: {context.original_subject}",
            f"Received: {_format_dt(context.original_received_at, context.tz)}",
            "",
        ]
    )

    deadline_slot: datetime | None = None
    if isinstance(request, ReplyEmailRequest):
        lines.extend(_reply_body(context, request))
    elif isinstance(request, CalendarEmailRequest):
        lines.extend(_calendar_body(context, request))
        if request.options:
            deadline_slot = max(option.start for option in request.options)
    else:
        lines.extend(_invitation_body(context, request))

    deadline = compute_expiry(context.sent_at, context.business_days, context.tz, deadline_slot)
    lines.append(f"Deadline: this approval expires {_format_dt(deadline, context.tz)}.")
    lines.extend(_how_to_answer(context, request))

    return "\n".join(lines)


def _reply_body(context: ApprovalContext, request: ReplyEmailRequest) -> list[str]:
    action = request.action
    if request.urgent:
        # D-40: notification-first — the flag (and the deadline, if any, D-24/task 47)
        # come before the proposed action, and the action itself is described as
        # sending the acknowledgement, not "a reply".
        lines = ["Flagged URGENT + starred."]
        deadline_line = _classification_deadline_line(context, request)
        if deadline_line is not None:
            lines.append(deadline_line)
        lines.extend(
            [
                "",
                f"Action: send this acknowledgement to {action.recipient}:",
                "",
                action.text,
                "",
            ]
        )
        return lines
    return [f"Action: send this reply to {action.recipient}:", "", action.text, ""]


def _classification_deadline_line(
    context: ApprovalContext, request: ReplyEmailRequest
) -> str | None:
    """D-24/D-40 (task 47): the sender's deadline phrase plus its resolved instant --
    shown only when the classification's deadline actually resolved to a time (a phrase
    alone has nothing to display next to it); "(overdue)" when that instant is already
    past this approval email's build time."""
    if request.deadline_phrase is None or request.deadline_at is None:
        return None
    overdue = " (overdue)" if request.deadline_at < context.sent_at else ""
    formatted = _format_deadline_dt(request.deadline_at, context.tz)
    return f"Deadline: {request.deadline_phrase} ({formatted}){overdue}"


def _calendar_body(context: ApprovalContext, request: CalendarEmailRequest) -> list[str]:
    lines = [f'Action: create a meeting "{request.title}"']
    if request.attendees:
        lines.append(f"Attendees: {', '.join(request.attendees)}")
    if request.location:
        lines.append(f"Location: {request.location}")
    requester_tz = ZoneInfo(request.requester_timezone) if request.requester_timezone else None
    lines.extend(_requested_lines(request, context.tz, requester_tz))
    lines.extend(["", "Options:"])

    tagged = bool(request.requested_phrases)
    for option in request.options:
        lines.append(_option_line(option, context.tz, requester_tz, tagged=tagged))
    lines.append("")

    if request.invalid_phrases:
        lines.append("Could not resolve (ignored as invalid, D-66):")
        lines.extend(f'  "{phrase}"' for phrase in request.invalid_phrases)
        lines.append("")

    if request.timezone_assumed:
        lines.append(
            f"Note: the timezone was assumed as {context.tz.key} because none was "
            "stated in the original email."
        )
    if requester_tz is not None and requester_tz != context.tz:
        lines.append(f"Note: times are also shown in {requester_tz.key}, the sender's stated zone.")

    if request.too_many_attendees:
        lines.append("Flag: more than 20 attendees were requested.")
    if request.longer_than_business_day:
        lines.append("Flag: this event is longer than one business day.")

    lines.extend([_KNOWN_LIMITATION, ""])
    return lines


def _invitation_body(context: ApprovalContext, request: InvitationEmailRequest) -> list[str]:
    action = request.action
    lines = [
        f'Invitation: "{action.title or "Untitled event"}"',
        f"When: {_format_dt(action.start, context.tz)} - {_format_dt(action.end, context.tz)}",
    ]
    if action.organizer:
        lines.append(f"Organizer: {action.organizer}")
    if action.location:
        lines.append(f"Location: {action.location}")
    lines.append("")

    mark_text = _MARK_TEXT.get(request.mark) if request.mark is not None else None
    if mark_text:
        warning = f"Warning: {mark_text}"
        if request.conflicts:
            warning += f" — conflicts with: {', '.join(request.conflicts)}"
        lines.extend([warning, ""])

    lines.extend([_KNOWN_LIMITATION, ""])
    return lines


def _requested_lines(
    request: CalendarEmailRequest, tz: ZoneInfo, requester_tz: ZoneInfo | None
) -> list[str]:
    """D-13: what the requester asked, each with whether it is available; when one is
    not, the agent's own options are called out as not requested."""
    entries: list[tuple[str, str | None]] = []
    for phrase in request.requested_phrases:
        option = next(
            (
                o
                for o in request.options
                if o.phrase == phrase and o.mark != SlotMark.OUTSIDE_REQUESTED_INTERVAL
            ),
            None,
        )
        if option is None:
            entries.append((f'"{phrase}"', "no free time in that period"))
            continue
        head = f'"{phrase}" → {_time_text(option, tz, requester_tz)}'
        if option.mark == SlotMark.FREE:
            entries.append((f"{head} (available)", None))
        elif option.mark == SlotMark.DOUBLE_BOOKING:
            entries.append((head, f"already booked ({', '.join(option.conflicts)})"))
        else:
            entries.append((head, "outside business hours"))
    if not entries:
        return []

    if len(entries) == 1:
        head, problem = entries[0]
        lines = [f"Requested: {head}"]
        if problem is not None:
            lines.append(f"NOT AVAILABLE: {problem}.")
    else:
        lines = ["Requested:"]
        for head, problem in entries:
            lines.append(f"- {head}" if problem is None else f"- {head} — NOT AVAILABLE: {problem}")
    if any(problem is not None for _, problem in entries) and any(
        not _is_requested(o) for o in request.options
    ):
        lines.append("The ALTERNATIVE options below were not requested.")
    return lines


def _is_requested(option: SlotOption) -> bool:
    return bool(option.phrase) and option.mark != SlotMark.OUTSIDE_REQUESTED_INTERVAL


def _time_text(option: SlotOption, tz: ZoneInfo, requester_tz: ZoneInfo | None) -> str:
    start_local = option.start.astimezone(tz)
    end_local = option.end.astimezone(tz)
    time_text = (
        f"{start_local:%a} {start_local.day} {start_local:%b} "
        f"{start_local:%H:%M}-{end_local:%H:%M} {start_local:%Z}"
    )
    if requester_tz is not None and requester_tz != tz:
        other_start = option.start.astimezone(requester_tz)
        other_end = option.end.astimezone(requester_tz)
        time_text += f" ({other_start:%H:%M}-{other_end:%H:%M} {other_start:%Z})"
    return time_text


def _option_line(
    option: SlotOption, tz: ZoneInfo, requester_tz: ZoneInfo | None, *, tagged: bool
) -> str:
    time_text = _time_text(option, tz, requester_tz)
    if tagged:
        # D-13: when the sender asked for times, every option says whether it was asked.
        prefix = (
            f'REQUESTED ("{option.phrase}") → '
            if _is_requested(option)
            else "ALTERNATIVE (not requested) → "
        )
    else:
        prefix = f'"{option.phrase}" → ' if option.phrase else ""
    line = f"{option.number}. {prefix}{time_text}"

    mark_text = _MARK_TEXT.get(option.mark)
    if option.mark == SlotMark.DOUBLE_BOOKING and option.conflicts:
        line += f" — {mark_text} — conflicts with: {', '.join(option.conflicts)}"
    elif option.mark != SlotMark.FREE and mark_text:
        line += f" — {mark_text}"
    return line


def _urgency_line(context: ApprovalContext, request: ApprovalEmailRequest) -> str | None:
    """D-55: "Starts in 55 minutes" when the meeting the manager would approve is soon.

    Only calendar requests have a start time to be urgent about; the
    earliest option is the one plain APPROVE would create (D-13).
    """
    if not isinstance(request, CalendarEmailRequest) or not request.options:
        return None
    earliest = min(option.start for option in request.options)
    delta = earliest - context.sent_at
    if delta <= timedelta(0) or delta > URGENCY_THRESHOLD:
        return None
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"Starts in {minutes} minutes"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"Starts in {hours}h {remaining_minutes}m"


def _how_to_answer(context: ApprovalContext, request: ApprovalEmailRequest) -> list[str]:
    if isinstance(request, CalendarEmailRequest):
        lines = ["Reply:", "- APPROVE → option 1"]
        if len(request.options) > 1:
            lines.append("- APPROVE <n> → option n")
        if request.decline_body is None:
            lines.append("- REJECT → no meeting")
            return lines
        lines.extend(["- REJECT → no meeting; the requester gets this reply:", ""])
        preview = f"{request.decline_body.rstrip()}\n\n{context.sender_name}"
        lines.extend(f"    {line}" if line else "" for line in preview.splitlines())
        return lines
    if isinstance(request, InvitationEmailRequest):
        return ["Reply APPROVE to accept, or REJECT to decline."]
    return ["Reply APPROVE to send this, or REJECT to discard it."]


def _format_dt(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    return f"{local:%a} {local.day} {local:%b} {local:%H:%M} {local:%Z}"


def _format_deadline_dt(dt: datetime, tz: ZoneInfo) -> str:
    """`Mon 28 Sep, 23:59 EEST` (D-24's own example wording) -- note the comma after
    the date, unlike `_format_dt`'s "Mon 28 Sep 09:00 EEST"."""
    local = dt.astimezone(tz)
    return f"{local:%a} {local.day} {local:%b}, {local:%H:%M} {local:%Z}"


__all__ = [
    "URGENCY_THRESHOLD",
    "ApprovalContext",
    "ApprovalEmail",
    "ApprovalEmailRequest",
    "CalendarEmailRequest",
    "InvitationEmailRequest",
    "ReplyEmailRequest",
    "build_approval_email",
]

"""Unit tests for dt_agent.core.approval_email (task 18).

Per the testing rules, these assert the facts D-32 requires are present in
the rendered email (options, marks, notes, deadline, token block), not the
exact wording -- the implementation is free to rephrase.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dt_agent.core.approval_email import (
    ApprovalContext,
    CalendarEmailRequest,
    InvitationEmailRequest,
    ReplyEmailRequest,
    build_approval_email,
)
from dt_agent.core.approval_token import (
    InvitationAction,
    ReplyAction,
    VerifiedApproval,
    extract_token,
    verify,
)
from dt_agent.domain.meeting import SlotMark, SlotOption

_TZ = ZoneInfo("Europe/Bucharest")
_KEY = "a-signing-key-at-least-32-bytes-long-for-hs256"


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_TZ)


def _context(**overrides: object) -> ApprovalContext:
    base: dict[str, object] = {
        "approver_address": "manager@example.com",
        "sender_name": "DT Agent",
        "signing_key": _KEY,
        "business_days": 3,
        "tz": _TZ,
        "sent_at": _dt(2026, 9, 28, 9, 0),  # Monday
        "original_email_id": "gmail-msg-1",
        "original_sender": "Alice <alice@example.com>",
        "original_subject": "Let's meet",
        "original_received_at": _dt(2026, 9, 28, 8, 0),
    }
    base.update(overrides)
    return ApprovalContext(**base)  # type: ignore[arg-type]


def _reply_action() -> ReplyAction:
    return ReplyAction(
        recipient="alice@example.com",
        thread_id="thread-1",
        message_id="<msg-1@example.com>",
        text="Thanks, sounds good.",
    )


def test_approval_email_token_in_both_parts() -> None:
    context = _context()
    request = ReplyEmailRequest(action=_reply_action())

    result = build_approval_email(context, request)
    message = result.message

    assert message.subject.startswith("[Approval]")
    assert message.to == context.approver_address
    assert message.sender_name == context.sender_name

    text_token = extract_token(message.text_body)
    html_token = extract_token(message.html_body)
    assert text_token is not None
    assert text_token == html_token

    verified = verify(text_token, _KEY, now=context.sent_at)
    assert isinstance(verified, VerifiedApproval)
    assert verified.jti == result.jti


def test_approval_email_options_numbered_and_marked() -> None:
    context = _context()
    options = (
        SlotOption(
            number=1,
            start=_dt(2026, 10, 5, 11, 0),
            end=_dt(2026, 10, 5, 11, 30),
            phrase="",
            mark=SlotMark.FREE,
        ),
        SlotOption(
            number=2,
            start=_dt(2026, 10, 6, 10, 0),
            end=_dt(2026, 10, 6, 10, 30),
            phrase="next Tuesday 10:00",
            mark=SlotMark.DOUBLE_BOOKING,
            conflicts=("14:00-15:00 Board meeting",),
        ),
    )
    request = CalendarEmailRequest(title="Sync", options=options, timezone_assumed=True)

    result = build_approval_email(context, request)
    text = result.message.text_body

    assert "1. " in text
    assert "2. " in text
    assert text.index("2. ") > text.index("1. ")
    assert "DOUBLE BOOKING" in text
    assert "Board meeting" in text
    assert "assumed" in text.lower()

    phrase_line = next(line for line in text.splitlines() if "next Tuesday 10:00" in line)
    assert "6 Oct" in phrase_line
    assert "10:00" in phrase_line


def test_approval_email_shows_requester_timezone_alongside_manager() -> None:
    """ADR-4: when the requester stated a different timezone, the option line shows
    both times ("15:00 EEST / 14:00 CEST") and a note names the requester's zone."""
    context = _context()
    option = SlotOption(
        number=1,
        start=_dt(2026, 9, 29, 15, 0),  # 15:00 Europe/Bucharest (EEST, UTC+3)
        end=_dt(2026, 9, 29, 15, 30),
        phrase="",
        mark=SlotMark.FREE,
    )
    request = CalendarEmailRequest(
        title="Sync", options=(option,), requester_timezone="Europe/Berlin"
    )

    result = build_approval_email(context, request)
    text = result.message.text_body

    option_line = next(line for line in text.splitlines() if line.startswith("1. "))
    assert "15:00" in option_line
    assert "14:00" in option_line  # 15:00 EEST == 14:00 Europe/Berlin (CEST, UTC+2)
    assert "Europe/Berlin" in text


def test_approval_email_invitation_busy_warning_no_alternatives() -> None:
    """D-48: a received invitation that overlaps a busy event gets a warning naming the
    conflict, no numbered alternatives (it's someone else's invitation), and a single
    APPROVE/REJECT choice."""
    context = _context()
    action = InvitationAction(
        ical_uid="event-1@external.example",
        start=_dt(2026, 9, 29, 14, 0),
        end=_dt(2026, 9, 29, 15, 0),
        organizer="organizer@external.example",
        title="Kickoff",
    )
    request = InvitationEmailRequest(
        action=action,
        mark=SlotMark.DOUBLE_BOOKING,
        conflicts=("14:00-15:00 Board meeting",),
    )

    result = build_approval_email(context, request)
    text = result.message.text_body

    assert "DOUBLE BOOKING" in text
    assert "Board meeting" in text
    assert "Options:" not in text  # no numbered alternatives for someone else's invitation
    assert "APPROVE" in text
    assert "REJECT" in text
    assert "APPROVE <n>" not in text  # a single choice, not a numbered-option prompt


def test_approval_email_urgent_highlight() -> None:
    sent_at = _dt(2026, 9, 28, 9, 0)
    start = sent_at + timedelta(minutes=55)
    options = (
        SlotOption(
            number=1, start=start, end=start + timedelta(minutes=30), phrase="", mark=SlotMark.FREE
        ),
    )
    context = _context(sent_at=sent_at)
    request = CalendarEmailRequest(title="Urgent sync", options=options)

    result = build_approval_email(context, request)
    lines = [line for line in result.message.text_body.splitlines() if line.strip()]

    assert "55 minutes" in lines[0]


def _deadline_lines(text: str) -> list[str]:
    """Both the classification's `Deadline: <phrase> (...)` line (task 47, D-24) and the
    unrelated, always-present `Deadline: this approval expires ...` line start with
    "Deadline:", so tests distinguish them by count/content rather than a bare substring
    check."""
    return [line for line in text.splitlines() if line.startswith("Deadline:")]


def test_approval_email_urgent_shows_deadline_line() -> None:
    context = _context()
    deadline_at = _dt(2026, 9, 30, 23, 59)  # Wed, after sent_at (Mon 28 Sep)
    request = ReplyEmailRequest(
        action=_reply_action(),
        urgent=True,
        deadline_phrase="by Wednesday",
        deadline_at=deadline_at,
    )

    result = build_approval_email(context, request)
    lines = _deadline_lines(result.message.text_body)

    assert len(lines) == 2  # the classification deadline + the always-present expiry line
    classification_line = next(line for line in lines if "by Wednesday" in line)
    assert "30 Sep" in classification_line
    assert "23:59" in classification_line
    assert "overdue" not in classification_line.lower()


def test_approval_email_urgent_without_deadline_has_no_line() -> None:
    context = _context()
    request = ReplyEmailRequest(action=_reply_action(), urgent=True)

    result = build_approval_email(context, request)
    lines = _deadline_lines(result.message.text_body)

    assert len(lines) == 1  # only the always-present expiry line
    assert "by Wednesday" not in lines[0]


def test_approval_email_urgent_unresolved_deadline_has_no_line() -> None:
    """D-24 (task 47): a phrase kept with no resolved instant has nothing to show next
    to it, so no classification `Deadline:` line is rendered -- same as no deadline
    at all."""
    context = _context()
    request = ReplyEmailRequest(action=_reply_action(), urgent=True, deadline_phrase="soon")

    result = build_approval_email(context, request)
    lines = _deadline_lines(result.message.text_body)

    assert len(lines) == 1  # only the always-present expiry line


def test_approval_email_urgent_overdue_deadline_marked() -> None:
    sent_at = _dt(2026, 9, 28, 9, 0)  # Monday
    context = _context(sent_at=sent_at)
    deadline_at = _dt(2026, 9, 27, 23, 59)  # Sunday, before sent_at
    request = ReplyEmailRequest(
        action=_reply_action(),
        urgent=True,
        deadline_phrase="yesterday",
        deadline_at=deadline_at,
    )

    result = build_approval_email(context, request)
    lines = _deadline_lines(result.message.text_body)
    classification_line = next(line for line in lines if "yesterday" in line)

    assert "overdue" in classification_line.lower()


def test_approval_email_reissue_same_thread() -> None:
    reason = "the previous approval expired before it was answered"
    context = _context(
        thread_id="thread-42",
        in_reply_to="<old-approval@example.com>",
        reissue_reason=reason,
    )
    request = ReplyEmailRequest(action=_reply_action())

    result = build_approval_email(context, request)
    message = result.message

    assert message.thread_id == "thread-42"
    assert message.in_reply_to == "<old-approval@example.com>"
    assert reason in message.text_body


def test_approval_email_shows_requested_phrases() -> None:
    """D-13: the approval email always shows what the requester asked, in their own
    words, before the numbered options."""
    options = (
        SlotOption(
            number=1,
            start=_dt(2026, 10, 1, 12, 0),
            end=_dt(2026, 10, 1, 12, 30),
            phrase="Thursday afternoon",
            mark=SlotMark.FREE,
        ),
        SlotOption(
            number=2,
            start=_dt(2026, 10, 2, 10, 0),
            end=_dt(2026, 10, 2, 10, 30),
            phrase="Friday at 10:00",
            mark=SlotMark.FREE,
        ),
    )
    request = CalendarEmailRequest(
        title="Quick sync",
        options=options,
        requested_phrases=("Thursday afternoon", "Friday at 10:00"),
    )

    text = build_approval_email(_context(), request).message.text_body

    assert "Requested:" in text
    assert '- "Thursday afternoon" → Thu 1 Oct 12:00-12:30 EEST (available)' in text
    assert '- "Friday at 10:00" → Fri 2 Oct 10:00-10:30 EEST (available)' in text
    assert text.index("Requested:") < text.index("Options:")


def test_approval_email_no_requested_line_when_nothing_was_asked() -> None:
    options = (
        SlotOption(
            number=1,
            start=_dt(2026, 10, 1, 12, 0),
            end=_dt(2026, 10, 1, 12, 30),
            phrase="",
            mark=SlotMark.FREE,
        ),
    )
    request = CalendarEmailRequest(title="Catch-up", options=options)

    text = build_approval_email(_context(), request).message.text_body

    assert "Requested:" not in text


def _booked_friday_request(**overrides: object) -> CalendarEmailRequest:
    options = (
        SlotOption(
            number=1,
            start=_dt(2026, 9, 28, 9, 0),
            end=_dt(2026, 9, 28, 9, 30),
            phrase="",
            mark=SlotMark.FREE,
        ),
        SlotOption(
            number=2,
            start=_dt(2026, 10, 2, 10, 0),
            end=_dt(2026, 10, 2, 10, 30),
            phrase="Friday at 10:00",
            mark=SlotMark.DOUBLE_BOOKING,
            conflicts=("10:00-10:30 quick sync",),
        ),
    )
    base: dict[str, object] = {
        "title": "quick sync",
        "options": options,
        "requested_phrases": ("Friday at 10:00",),
        "decline_body": "Hi Alice,\n\nThank you for your meeting request.",
    }
    base.update(overrides)
    return CalendarEmailRequest(**base)  # type: ignore[arg-type]


def test_approval_email_requested_time_booked_is_stated_and_options_tagged() -> None:
    text = build_approval_email(_context(), _booked_friday_request()).message.text_body

    assert 'Requested: "Friday at 10:00" → Fri 2 Oct 10:00-10:30 EEST' in text
    assert "NOT AVAILABLE: already booked (10:00-10:30 quick sync)." in text
    assert "The ALTERNATIVE options below were not requested." in text
    assert "1. ALTERNATIVE (not requested) → Mon 28 Sep 09:00-09:30 EEST" in text
    assert '2. REQUESTED ("Friday at 10:00") → Fri 2 Oct 10:00-10:30 EEST — DOUBLE BOOKING' in text


def test_approval_email_meeting_reply_block_previews_reject_reply() -> None:
    text = build_approval_email(_context(), _booked_friday_request()).message.text_body

    assert "- APPROVE → option 1" in text
    assert "- APPROVE <n> → option n" in text
    assert "- REJECT → no meeting; the requester gets this reply:" in text
    assert "    Hi Alice," in text
    assert "    Thank you for your meeting request." in text
    assert "    DT Agent" in text


def test_approval_email_requested_time_available() -> None:
    options = (
        SlotOption(
            number=1,
            start=_dt(2026, 10, 2, 10, 0),
            end=_dt(2026, 10, 2, 10, 30),
            phrase="Friday at 10:00",
            mark=SlotMark.FREE,
        ),
    )
    request = _booked_friday_request(options=options)

    text = build_approval_email(_context(), request).message.text_body

    assert 'Requested: "Friday at 10:00" → Fri 2 Oct 10:00-10:30 EEST (available)' in text
    assert "NOT AVAILABLE" not in text
    assert "ALTERNATIVE" not in text
    assert '1. REQUESTED ("Friday at 10:00") → Fri 2 Oct 10:00-10:30 EEST' in text

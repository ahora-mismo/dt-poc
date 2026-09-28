"""Tests for approval reply handling (task 20, D-9, D-11, D-12, D-34, D-35, D-39, D-40, D-48, D-49).

Real `SqliteStore` in a temporary directory, `FakeMail`/`FakeCalendar` per
the testing rules. Approval emails are built with the real
`build_approval_email` (D-32); the manager's replies quote the token the
way a mail client would -- `> ` prefixes, wrapped at 76 columns -- built
from that same email, never hand-typed.

Test description format: test_<unit>_<condition>_<expected>.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dt_agent.adapters.sqlite_store import SqliteStore, open_store
from dt_agent.core.approval_email import (
    ApprovalContext,
    ApprovalEmail,
    CalendarEmailRequest,
    InvitationEmailRequest,
    ReplyEmailRequest,
    build_approval_email,
)
from dt_agent.core.approval_token import InvitationAction, ReplyAction
from dt_agent.domain.clock import FixedClock
from dt_agent.domain.email import Email, Headers
from dt_agent.domain.labels import GMAIL_LABEL_ID_INBOX, GMAIL_LABEL_ID_SPAM
from dt_agent.domain.meeting import SlotMark, SlotOption
from dt_agent.pipeline.approvals import handle_approval_reply
from dt_agent.pipeline.deps import PipelineDeps
from tests.fakes.calendar import FakeCalendar
from tests.fakes.emails import make_email
from tests.fakes.mail import FakeMail
from tests.fakes.settings import DEFAULT_NOW, MAILBOX, make_deps, make_settings

APPROVER = "manager@example.com"


def _store(tmp_path: Path) -> SqliteStore:
    store, report = open_store(tmp_path / "agent.db", FixedClock(DEFAULT_NOW))
    assert report is None
    return store


def _deps(
    tmp_path: Path,
    *,
    now: datetime = DEFAULT_NOW,
    mail: FakeMail | None = None,
    calendar: FakeCalendar | None = None,
    approver: str = APPROVER,
) -> PipelineDeps:
    clock = FixedClock(now)
    return make_deps(
        store=_store(tmp_path),
        settings=make_settings(MANAGER_EMAIL=approver),
        clock=clock,
        mail=mail or FakeMail(address=MAILBOX, clock=clock),
        calendar=calendar or FakeCalendar(),
    )


def _original_email(
    *, id: str = "orig-1", from_address: str = "alice@example.com", sent_at: datetime
) -> Email:
    return make_email(
        id=id,
        thread_id=id,
        from_address=from_address,
        subject="Can we meet?",
        text_body="Are you free this week?",
        received_at=sent_at - timedelta(hours=1),
    )


def _context(deps: PipelineDeps, original: Email, *, sent_at: datetime) -> ApprovalContext:
    return ApprovalContext(
        approver_address=deps.approver_address,
        sender_name=deps.settings.AGENT_SENDER_NAME,
        signing_key=deps.signing_key,
        business_days=deps.settings.APPROVAL_EXPIRY_BUSINESS_DAYS,
        tz=ZoneInfo(deps.settings.DEFAULT_TIMEZONE),
        sent_at=sent_at,
        original_email_id=original.id,
        original_sender=original.from_address.address,
        original_subject=original.subject,
        original_received_at=original.received_at,
    )


def _quote_token_block(approval: ApprovalEmail, *, width: int = 76) -> str:
    """The token block, quoted the way a mail client would (`> `, wrapped)."""
    token_block = approval.message.text_body.rsplit("\n\n", 1)[-1]
    quoted_lines: list[str] = []
    for line in token_block.splitlines():
        for piece in textwrap.wrap(line, width) or [""]:
            quoted_lines.append(f"> {piece}")
    return "\n".join(quoted_lines)


async def _send_approval(mail: FakeMail, approval: ApprovalEmail) -> Email:
    """Simulate the agent having sent `approval`; returns the sent `Email`."""
    sent_id = await mail.send(approval.message)
    return await mail.get_message(sent_id)


def _free_option(number: int, start: datetime, *, minutes: int = 30) -> SlotOption:
    return SlotOption(
        number=number,
        start=start,
        end=start + timedelta(minutes=minutes),
        phrase="",
        mark=SlotMark.FREE,
    )


def _reply_email(
    *,
    id: str,
    thread_id: str,
    from_address: str,
    decision_line: str,
    approval: ApprovalEmail,
    received_at: datetime,
) -> Email:
    quoted = _quote_token_block(approval)
    return make_email(
        id=id,
        thread_id=thread_id,
        from_address=from_address,
        subject="Re: [Approval]",
        text_body=f"{decision_line}\n\n{quoted}\n",
        received_at=received_at,
    )


async def test_approval_meeting_round_trip(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(sent_at=DEFAULT_NOW)
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    start1 = DEFAULT_NOW + timedelta(hours=1)
    options = (
        _free_option(1, start1),
        _free_option(2, start1 + timedelta(hours=1)),
        _free_option(3, start1 + timedelta(hours=2)),
    )
    request = CalendarEmailRequest(title="Sync", options=options, attendees=("bob@example.com",))
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE 2",
        approval=approval,
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "approved"
    assert len(calendar.inserted) == 1
    created = calendar.inserted[0]
    assert created.start == options[1].start
    assert created.end == options[1].end
    assert set(created.attendees) == {"bob@example.com", APPROVER}
    assert MAILBOX not in created.attendees


async def test_approval_reject_does_nothing(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(sent_at=DEFAULT_NOW)
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    start1 = DEFAULT_NOW + timedelta(hours=1)
    options = (_free_option(1, start1),)
    request = CalendarEmailRequest(title="Sync", options=options)
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="REJECT",
        approval=approval,
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "rejected"
    assert calendar.inserted == []
    assert mail.sent == [approval.message]


async def test_approval_invalid_marked_spam(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    deps = _deps(tmp_path, mail=mail)

    original = _original_email(sent_at=DEFAULT_NOW)
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    start1 = DEFAULT_NOW + timedelta(hours=1)
    options = (_free_option(1, start1),)
    request = CalendarEmailRequest(title="Sync", options=options)
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    quoted_lines = _quote_token_block(approval).splitlines()
    # Flip one character inside the first token line (index 0 is the
    # quoted BEGIN delimiter) without touching either delimiter line, so
    # `extract_token` still finds a block but the signature no longer
    # verifies.
    victim = quoted_lines[1]
    flipped = "A" if victim[-1] != "A" else "B"
    quoted_lines[1] = victim[:-1] + flipped
    tampered = "\n".join(quoted_lines)
    reply = make_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        subject="Re: [Approval]",
        text_body=f"APPROVE\n\n{tampered}\n",
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "invalid"
    updated = await mail.get_message("reply-1")
    assert GMAIL_LABEL_ID_SPAM in updated.label_ids
    assert GMAIL_LABEL_ID_INBOX not in updated.label_ids


async def test_approval_auto_submitted_reply_ignored(tmp_path: Path) -> None:
    """D-49: an automatic reply (e.g. out-of-office) in an approval thread, even one
    that quotes a valid token, is ignored and logged -- no action, no SPAM label, no
    help reply. A later real reply in the same thread still executes normally."""
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(sent_at=DEFAULT_NOW)
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    start1 = DEFAULT_NOW + timedelta(hours=1)
    options = (_free_option(1, start1),)
    request = CalendarEmailRequest(title="Sync", options=options)
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    quoted = _quote_token_block(approval)
    ooo_reply = make_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        headers={"Auto-Submitted": "auto-replied"},
        subject="Automatic reply: Re: [Approval]",
        text_body=f"I am out of the office this week.\n\n{quoted}\n",
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(ooo_reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, ooo_reply, thread)

    assert result.outcome == "ignored"
    assert result.actions == ()
    assert calendar.inserted == []
    assert mail.sent == [approval.message]  # no notice, no help reply, nothing executed
    updated = await mail.get_message("reply-1")
    assert GMAIL_LABEL_ID_SPAM not in updated.label_ids
    assert GMAIL_LABEL_ID_INBOX in updated.label_ids

    # A later real APPROVE in the same thread still executes.
    real_reply = _reply_email(
        id="reply-2",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE",
        approval=approval,
        received_at=DEFAULT_NOW + timedelta(minutes=10),
    )
    mail.add_message(real_reply)
    thread = await mail.get_thread(approval_email.thread_id)

    real_result = await handle_approval_reply(deps, real_reply, thread)

    assert real_result.outcome == "approved"
    assert len(calendar.inserted) == 1


async def test_approval_superseded_token(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(sent_at=DEFAULT_NOW)
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    start1 = DEFAULT_NOW + timedelta(hours=1)
    first_options = (_free_option(1, start1),)
    first_request = CalendarEmailRequest(title="Sync", options=first_options)
    first_approval = build_approval_email(context, first_request)
    first_email = await _send_approval(mail, first_approval)

    # A re-issue in the same thread (D-11/D-12): a new token replaces the
    # first one; only it is valid from now on (D-34).
    reissue_context = ApprovalContext(
        approver_address=deps.approver_address,
        sender_name=deps.settings.AGENT_SENDER_NAME,
        signing_key=deps.signing_key,
        business_days=deps.settings.APPROVAL_EXPIRY_BUSINESS_DAYS,
        tz=ZoneInfo(deps.settings.DEFAULT_TIMEZONE),
        sent_at=DEFAULT_NOW + timedelta(minutes=1),
        original_email_id=original.id,
        original_sender=original.from_address.address,
        original_subject=original.subject,
        original_received_at=original.received_at,
        thread_id=first_email.thread_id,
        in_reply_to=first_email.header("Message-ID"),
        reissue_reason="test re-issue",
    )
    start2 = DEFAULT_NOW + timedelta(hours=2)
    second_options = (_free_option(1, start2),)
    second_request = CalendarEmailRequest(title="Sync", options=second_options)
    second_approval = build_approval_email(reissue_context, second_request)
    second_email_id = await mail.send(second_approval.message)
    second_email = await mail.get_message(second_email_id)
    assert second_email.thread_id == first_email.thread_id

    # The manager replies to the *older* (first) approval email.
    reply = _reply_email(
        id="reply-1",
        thread_id=first_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE",
        approval=first_approval,
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(reply)
    thread = await mail.get_thread(first_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "superseded"
    assert calendar.inserted == []
    # D-34: the approver is told to answer the latest [Approval] email instead --
    # a plain notice in the same thread, with no token block of its own.
    assert result.actions == ("notice",)
    assert len(mail.sent) == 3  # first approval + re-issue + the superseded notice
    notice = mail.sent[-1]
    assert notice.to == APPROVER
    assert notice.thread_id == first_email.thread_id
    assert "superseded" in notice.text_body.lower()
    assert "-----BEGIN DT-AGENT APPROVAL TOKEN-----" not in notice.text_body


async def test_approval_expired_reissued_same_thread(tmp_path: Path) -> None:
    # `sent_at` 7 days before "now" (Mon 28 Sep) -> Mon 21 Sep; expiry is
    # min(sent_at + 3 business days, latest slot start) = Thu 24 Sep,
    # already past "now". The option itself starts 2 days after "now",
    # so it's still reissuable (D-11: "only if the event is not in the
    # past").
    sent_at = DEFAULT_NOW - timedelta(days=7)
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(id="orig-1", sent_at=sent_at)
    mail.add_message(original)
    context = _context(deps, original, sent_at=sent_at)

    future_start = DEFAULT_NOW + timedelta(days=2)
    options = (
        SlotOption(
            number=1,
            start=future_start,
            end=future_start + timedelta(minutes=30),
            phrase="",
            mark=SlotMark.FREE,
        ),
    )
    request = CalendarEmailRequest(title="Sync", options=options)
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE",
        approval=approval,
        received_at=DEFAULT_NOW,
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "expired_reissued"
    assert calendar.inserted == []
    # The "not taken" notice, plus a fresh approval email in the same thread.
    assert len(mail.sent) == 3  # original approval + notice + re-issue
    reissue = mail.sent[-1]
    assert reissue.thread_id == approval_email.thread_id
    assert "-----BEGIN DT-AGENT APPROVAL TOKEN-----" in reissue.text_body


async def test_approval_expired_event_in_past_not_reissued(tmp_path: Path) -> None:
    """D-11: a re-issue for a calendar action happens only if the event is *not* in
    the past. Here every option's start is already before `now`, so the manager just
    gets the "not taken" notice -- no re-issue, no token block, nothing on the
    calendar."""
    sent_at = DEFAULT_NOW - timedelta(days=7)
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(id="orig-1", sent_at=sent_at)
    mail.add_message(original)
    context = _context(deps, original, sent_at=sent_at)

    past_start = DEFAULT_NOW - timedelta(hours=2)
    options = (
        SlotOption(
            number=1,
            start=past_start,
            end=past_start + timedelta(minutes=30),
            phrase="",
            mark=SlotMark.FREE,
        ),
    )
    request = CalendarEmailRequest(title="Sync", options=options)
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE",
        approval=approval,
        received_at=DEFAULT_NOW,
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "expired_reissued"
    assert result.actions == ("notice",)  # no "reissued": every option is in the past
    assert calendar.inserted == []
    assert len(mail.sent) == 2  # original approval + the "not taken" notice, no re-issue
    notice = mail.sent[-1]
    assert notice.thread_id == approval_email.thread_id
    assert "not" in notice.text_body.lower()
    assert "-----BEGIN DT-AGENT APPROVAL TOKEN-----" not in notice.text_body


async def test_approvals_expired_urgent_ack_reissued_as_urgent(tmp_path: Path) -> None:
    # D-40/D-11: an urgent-internal acknowledgement that expires and gets
    # re-issued must keep the "urgent" subject, not fall back to plain.
    sent_at = DEFAULT_NOW - timedelta(days=7)
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(sent_at=sent_at)
    mail.add_message(original)
    context = _context(deps, original, sent_at=sent_at)

    action = ReplyAction(
        recipient=original.from_address.address,
        thread_id=original.thread_id,
        message_id=original.id,
        text="Got it, thanks.",
        urgent=True,
    )
    request = ReplyEmailRequest(action=action, urgent=True)
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE",
        approval=approval,
        received_at=DEFAULT_NOW,
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "expired_reissued"
    reissue = mail.sent[-1]
    assert reissue.subject.startswith("[Approval] URGENT from")


async def test_approvals_expired_plain_reply_reissued_not_urgent(tmp_path: Path) -> None:
    # Same as above, but the original acknowledgement wasn't urgent (D-39
    # courtesy reply): the re-issue must stay plain.
    sent_at = DEFAULT_NOW - timedelta(days=7)
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(sent_at=sent_at)
    mail.add_message(original)
    context = _context(deps, original, sent_at=sent_at)

    action = ReplyAction(
        recipient=original.from_address.address,
        thread_id=original.thread_id,
        message_id=original.id,
        text="Got it, thanks.",
    )
    request = ReplyEmailRequest(action=action)
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE",
        approval=approval,
        received_at=DEFAULT_NOW,
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "expired_reissued"
    reissue = mail.sent[-1]
    assert reissue.subject.startswith("[Approval] Reply to ")
    assert "urgent" not in reissue.subject


async def test_approval_slot_taken_reissued(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(sent_at=DEFAULT_NOW)
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    start1 = DEFAULT_NOW + timedelta(hours=1)
    options = (_free_option(1, start1),)
    request = CalendarEmailRequest(title="Sync", options=options)
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    # The slot got booked in the meantime (D-12), after the approval email
    # was sent but before the manager's reply.
    calendar.add_busy(start1, start1 + timedelta(minutes=30), title="Surprise meeting")

    reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE",
        approval=approval,
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "approved"
    assert result.actions == ("reissued_slot_taken",)
    assert calendar.inserted == []
    assert len(mail.sent) == 2  # original approval + re-issue
    reissue = mail.sent[-1]
    assert reissue.thread_id == approval_email.thread_id
    assert "-----BEGIN DT-AGENT APPROVAL TOKEN-----" in reissue.text_body
    assert "DOUBLE BOOKING" in reissue.text_body
    assert "Surprise meeting" in reissue.text_body


async def test_approval_slot_taken_reissued_far_future_slot(tmp_path: Path) -> None:
    """D-12's re-check must cover the approved slot even far past `now`.

    A meeting proposed for a month out (well past this module's own
    re-issue search window) that got booked in the meantime must still be
    reported as a double booking in the re-issue, not silently marked
    free because the free/busy fetch used for the *alternatives* search
    didn't reach that far.
    """
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(sent_at=DEFAULT_NOW)
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    far_start = DEFAULT_NOW + timedelta(days=30, hours=1)  # Wed, business hours
    options = (_free_option(1, far_start),)
    request = CalendarEmailRequest(title="Sync", options=options)
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    calendar.add_busy(far_start, far_start + timedelta(minutes=30), title="Surprise meeting")

    reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE",
        approval=approval,
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "approved"
    assert result.actions == ("reissued_slot_taken",)
    assert calendar.inserted == []
    reissue = mail.sent[-1]
    assert "DOUBLE BOOKING" in reissue.text_body
    assert "Surprise meeting" in reissue.text_body


async def test_approval_unclear_help_once(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    deps = _deps(tmp_path, mail=mail)

    original = _original_email(sent_at=DEFAULT_NOW)
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    start1 = DEFAULT_NOW + timedelta(hours=1)
    options = (_free_option(1, start1),)
    request = CalendarEmailRequest(title="Sync", options=options)
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    first_reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="maybe later",
        approval=approval,
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(first_reply)
    thread = await mail.get_thread(approval_email.thread_id)

    first_result = await handle_approval_reply(deps, first_reply, thread)
    assert first_result.outcome == "unclear"
    assert len(mail.sent) == 2  # original approval + help reply

    second_reply = _reply_email(
        id="reply-2",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="still not sure",
        approval=approval,
        received_at=DEFAULT_NOW + timedelta(minutes=10),
    )
    mail.add_message(second_reply)
    thread = await mail.get_thread(approval_email.thread_id)  # refreshed: includes the help reply

    second_result = await handle_approval_reply(deps, second_reply, thread)

    assert second_result.outcome == "unclear"
    assert len(mail.sent) == 2  # no second help reply


async def test_approval_courtesy_and_sales_sent_on_approve(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    deps = _deps(tmp_path, mail=mail)

    sender = "prospect@vendor.example"
    original = _original_email(id="orig-1", from_address=sender, sent_at=DEFAULT_NOW)
    original = original.model_copy(
        update={"headers": Headers(values={"Message-ID": "<orig-1@vendor.example>"})}
    )
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    draft_text = (
        "Thanks for reaching out -- we're not looking for this right now, but "
        "we'll keep you in mind.\n\nTest Assistant\n"
    )
    action = ReplyAction(
        recipient=sender,
        thread_id=original.thread_id,
        message_id=original.id,
        text=draft_text,
    )
    approval = build_approval_email(context, ReplyEmailRequest(action=action))
    approval_email = await _send_approval(mail, approval)

    reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE",
        approval=approval,
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "approved"
    sent_to_sender = [m for m in mail.sent if m.to == sender]
    assert len(sent_to_sender) == 1
    assert sent_to_sender[0].text_body == draft_text
    assert sent_to_sender[0].thread_id == original.thread_id
    assert sent_to_sender[0].in_reply_to == "<orig-1@vendor.example>"


async def test_approval_invitation_accept_decline(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(sent_at=DEFAULT_NOW)
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    start = DEFAULT_NOW + timedelta(hours=1)
    action = InvitationAction(
        ical_uid="event-123@external.example",
        start=start,
        end=start + timedelta(minutes=30),
        organizer="organizer@external.example",
        title="External sync",
    )
    approval = build_approval_email(context, InvitationEmailRequest(action=action))
    approval_email = await _send_approval(mail, approval)

    accept_reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="APPROVE",
        approval=approval,
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(accept_reply)
    thread = await mail.get_thread(approval_email.thread_id)

    accept_result = await handle_approval_reply(deps, accept_reply, thread)

    assert accept_result.outcome == "approved"
    assert len(calendar.imported) == 1
    assert calendar.imported[0].uid == "event-123@external.example"
    [event_id] = calendar.responses.keys()
    assert calendar.responses[event_id] is True

    # A second, independent invitation that gets declined.
    start2 = DEFAULT_NOW + timedelta(hours=5)
    decline_action = InvitationAction(
        ical_uid="event-456@external.example",
        start=start2,
        end=start2 + timedelta(minutes=30),
        organizer="organizer@external.example",
        title="External sync 2",
    )
    decline_context = _context(deps, original, sent_at=DEFAULT_NOW)
    decline_request = InvitationEmailRequest(action=decline_action)
    decline_approval = build_approval_email(decline_context, decline_request)
    decline_approval_email = await _send_approval(mail, decline_approval)

    decline_reply = _reply_email(
        id="reply-2",
        thread_id=decline_approval_email.thread_id,
        from_address=APPROVER,
        decision_line="REJECT",
        approval=decline_approval,
        received_at=DEFAULT_NOW + timedelta(minutes=6),
    )
    mail.add_message(decline_reply)
    decline_thread = await mail.get_thread(decline_approval_email.thread_id)

    decline_result = await handle_approval_reply(deps, decline_reply, decline_thread)

    assert decline_result.outcome == "rejected"
    assert len(calendar.imported) == 2
    # D-48: a declined invitation is removed from the mailbox's schedule,
    # so `find_event_by_ical_uid` no longer finds it -- but the response
    # itself was recorded against whichever event id the import returned.
    [imported_event_id] = [eid for eid in calendar.responses if eid not in {event_id}]
    assert calendar.responses[imported_event_id] is False


async def test_approval_reject_meeting_request_sends_not_available_reply(tmp_path: Path) -> None:
    """REJECT on a meeting request from an email's text: no event, and the requester gets
    the kind "not available" reply in the original thread (the calendar-invitation REJECT
    is unchanged: it declines the invitation, no reply)."""
    mail = FakeMail(address=MAILBOX, clock=FixedClock(DEFAULT_NOW))
    calendar = FakeCalendar()
    deps = _deps(tmp_path, mail=mail, calendar=calendar)

    original = _original_email(sent_at=DEFAULT_NOW)
    mail.add_message(original)
    context = _context(deps, original, sent_at=DEFAULT_NOW)

    body = "Hi Alice,\n\nThank you for your meeting request."
    request = CalendarEmailRequest(
        title="Sync",
        options=(_free_option(1, DEFAULT_NOW + timedelta(hours=1)),),
        decline_body=body,
    )
    approval = build_approval_email(context, request)
    approval_email = await _send_approval(mail, approval)

    reply = _reply_email(
        id="reply-1",
        thread_id=approval_email.thread_id,
        from_address=APPROVER,
        decision_line="REJECT",
        approval=approval,
        received_at=DEFAULT_NOW + timedelta(minutes=5),
    )
    mail.add_message(reply)
    thread = await mail.get_thread(approval_email.thread_id)

    result = await handle_approval_reply(deps, reply, thread)

    assert result.outcome == "rejected"
    assert result.actions == ("decline_reply_sent",)
    assert calendar.inserted == []
    decline = mail.sent[-1]
    assert decline.to == "alice@example.com"
    assert decline.thread_id == original.thread_id
    assert decline.text_body.startswith(body)

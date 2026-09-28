"""Unit tests for dt_agent.core.invitations (task 14, D-48, D-55).

Per the testing rules, this covers the three behaviours that matter:
`METHOD:REQUEST` detection (from either a `text/calendar` part or an
`.ics` attachment, both already flattened into `Email.calendar_parts`
by the Gmail adapter) with correct field extraction including all-day
and TZID-floating times; `METHOD:CANCEL` / `METHOD:REPLY` classified
without a calendar action; and the no-calendar-part fallback.
"""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dt_agent.core.invitations import InvitationKind, detect_invitation
from dt_agent.domain.email import CalendarPart, Email, EmailAddress, Headers

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "invitations"


def _email(*, calendar_parts: tuple[CalendarPart, ...] = ()) -> Email:
    return Email(
        id="msg-1",
        thread_id="thread-1",
        headers=Headers(values={}),
        from_address=EmailAddress(address="sender@example.com"),
        subject="Test",
        received_at=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
        text_body="body",
        calendar_parts=calendar_parts,
    )


def test_invitation_request_detected_in_part_and_attachment() -> None:
    part_ics = (FIXTURES / "request_part.ics").read_text()
    email_from_part = _email(calendar_parts=(CalendarPart(raw_ics=part_ics),))

    result = detect_invitation(email_from_part)

    assert result.kind is InvitationKind.REQUEST
    assert result.invitation is not None
    assert result.invitation.uid == "event-part-001@example.com"
    bucharest = ZoneInfo("Europe/Bucharest")
    assert result.invitation.start == datetime(2026, 10, 5, 14, 0, tzinfo=bucharest)
    assert result.invitation.end == datetime(2026, 10, 5, 15, 0, tzinfo=bucharest)
    assert result.invitation.title == "Quarterly Planning Sync"
    assert result.invitation.location == "Conference Room A"
    assert result.invitation.organizer == "alice@example.com"
    assert result.invitation.sequence == 0

    attachment_ics = (FIXTURES / "request_attachment.ics").read_text()
    email_from_attachment = _email(calendar_parts=(CalendarPart(raw_ics=attachment_ics),))

    result = detect_invitation(email_from_attachment)

    assert result.kind is InvitationKind.REQUEST
    assert result.invitation is not None
    assert result.invitation.uid == "event-attach-002@example.com"
    assert result.invitation.start == datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
    assert result.invitation.end == datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    assert result.invitation.title == "Company Offsite Day"
    assert result.invitation.location == "Mountain Lodge"
    assert result.invitation.organizer == "bob@example.com"
    assert result.invitation.sequence == 2


def test_invitation_cancel_and_reply() -> None:
    cancel_ics = (FIXTURES / "cancel.ics").read_text()
    cancel_email = _email(calendar_parts=(CalendarPart(raw_ics=cancel_ics),))

    cancel_result = detect_invitation(cancel_email)

    assert cancel_result.kind is InvitationKind.CANCEL
    assert cancel_result.invitation is None

    reply_ics = (FIXTURES / "reply.ics").read_text()
    reply_email = _email(calendar_parts=(CalendarPart(raw_ics=reply_ics),))

    reply_result = detect_invitation(reply_email)

    assert reply_result.kind is InvitationKind.REPLY
    assert reply_result.invitation is None


def test_invitation_absent() -> None:
    email = _email(calendar_parts=())

    result = detect_invitation(email)

    assert result.kind is InvitationKind.NOT_INVITATION
    assert result.invitation is None


def test_invitation_unparseable_request_falls_back() -> None:
    # SPEC: "none/unparseable -> NOT_INVITATION (falls back to normal
    # classification)". A REQUEST whose VEVENT can't be resolved (here,
    # a TZID naming an unknown zone) must fall back rather than raise.
    bad_tzid_ics = (FIXTURES / "request_bad_tzid.ics").read_text()
    email = _email(calendar_parts=(CalendarPart(raw_ics=bad_tzid_ics),))

    result = detect_invitation(email)

    assert result.kind is InvitationKind.NOT_INVITATION
    assert result.invitation is None

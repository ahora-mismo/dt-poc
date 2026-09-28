"""Tests for the in-memory port fakes (task 5).

Per the testing rules, these are the fakes' only non-trivial behaviours:
archiving actually removing `INBOX`, a sent message actually joining its
thread with `SENT`, and an inserted event actually showing up as busy.
"""

from datetime import UTC, datetime

from dt_agent.domain.labels import GMAIL_LABEL_ID_INBOX, GMAIL_LABEL_ID_SENT
from dt_agent.ports.calendar import NewEvent
from dt_agent.ports.mail import OutgoingMessage
from tests.fakes.calendar import FakeCalendar
from tests.fakes.emails import make_email
from tests.fakes.mail import FakeMail


async def test_fake_mail_archive_removes_inbox() -> None:
    mail = FakeMail()
    mail.add_message(make_email(id="msg-1", label_ids=(GMAIL_LABEL_ID_INBOX,)))

    await mail.modify_labels("msg-1", remove=(GMAIL_LABEL_ID_INBOX,))

    archived = await mail.get_message("msg-1")
    assert GMAIL_LABEL_ID_INBOX not in archived.label_ids


async def test_fake_mail_sent_message_joins_thread() -> None:
    mail = FakeMail(address="mailbox@example.com")
    thread_id = "thread-1"

    sent_id = await mail.send(
        OutgoingMessage(
            to="someone@example.com",
            subject="Re: hello",
            text_body="body",
            html_body="<p>body</p>",
            sender_name="Assistant",
            thread_id=thread_id,
        )
    )

    thread = await mail.get_thread(thread_id)
    sent_message = next(message for message in thread if message.id == sent_id)
    assert GMAIL_LABEL_ID_SENT in sent_message.label_ids


async def test_fake_calendar_inserted_event_is_busy() -> None:
    calendar = FakeCalendar()
    start = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)
    end = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)

    await calendar.insert_event(NewEvent(title="Sync", start=start, end=end, timezone="UTC"))

    busy = await calendar.free_busy(start, end)
    assert len(busy) == 1
    assert busy[0].start == start
    assert busy[0].end == end

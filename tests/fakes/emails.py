"""Builds `Email` test fixtures (D-52).

Every fake- and pipeline-level test needs an `Email`; this centralises the
boilerplate (aware `received_at`, `Headers`, `EmailAddress`) so each test
overrides only the fields it cares about.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from dt_agent.domain.email import Attachment, CalendarPart, Email, EmailAddress, Headers
from dt_agent.domain.labels import GMAIL_LABEL_ID_INBOX

DEFAULT_RECEIVED_AT = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def _as_address(value: EmailAddress | str) -> EmailAddress:
    return value if isinstance(value, EmailAddress) else EmailAddress(address=value)


def make_email(
    *,
    id: str = "msg-1",
    thread_id: str | None = None,
    label_ids: Sequence[str] = (GMAIL_LABEL_ID_INBOX,),
    headers: Headers | dict[str, str] | None = None,
    from_address: EmailAddress | str = "sender@example.com",
    reply_to: EmailAddress | str | None = None,
    to: Sequence[EmailAddress | str] = ("mailbox@example.com",),
    cc: Sequence[EmailAddress | str] = (),
    subject: str = "Subject",
    received_at: datetime = DEFAULT_RECEIVED_AT,
    text_body: str = "Hello.",
    attachments: Sequence[Attachment] = (),
    calendar_parts: Sequence[CalendarPart] = (),
) -> Email:
    """An `Email` with sensible defaults; pass only the fields a test cares about."""
    if isinstance(headers, dict):
        headers = Headers(values=headers)
    return Email(
        id=id,
        thread_id=thread_id or id,
        label_ids=tuple(label_ids),
        headers=headers or Headers(),
        from_address=_as_address(from_address),
        reply_to=_as_address(reply_to) if reply_to is not None else None,
        to=tuple(_as_address(address) for address in to),
        cc=tuple(_as_address(address) for address in cc),
        subject=subject,
        received_at=received_at,
        text_body=text_body,
        attachments=tuple(attachments),
        calendar_parts=tuple(calendar_parts),
    )

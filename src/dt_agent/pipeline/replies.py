"""Threaded replies to outside people (D-38, D-39, D-40).

The LLM writes only the body. Code adds the signature (`AGENT_SENDER_NAME`),
the Gmail-style quoted original, the recipient (the original sender, from
the headers, never from the LLM) and the threading headers.
"""

from __future__ import annotations

import html

from dt_agent.domain.email import Email
from dt_agent.ports import OutgoingMessage


def reply_recipient(original: Email) -> str:
    """The reply goes to the original sender, taken from the headers (D-38)."""
    return original.from_address.address


def reply_text(original: Email, body: str, sender_name: str) -> str:
    """Body + signature + the original quoted the way Gmail quotes by default."""
    when = original.received_at.strftime("%a, %d %b %Y at %H:%M")
    sender = original.from_address
    who = f"{sender.name} <{sender.address}>" if sender.name else sender.address
    quoted = "\n".join(f"> {line}" if line else ">" for line in original.text_body.splitlines())
    return f"{body.rstrip()}\n\n{sender_name}\n\nOn {when}, {who} wrote:\n{quoted}\n"


def meeting_unavailable_body(original: Email) -> str:
    """The fixed, kind reply a requester gets when the manager rejects their meeting
    request (D-13); the signature and quoted original are added by `reply_text`."""
    first_name = original.from_address.name.split()[0] if original.from_address.name else ""
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    return (
        f"{greeting}\n\n"
        "Thank you for your meeting request. Unfortunately, the time you proposed isn't "
        "available. Please feel free to suggest another time."
    )


def build_reply(original: Email, body: str, sender_name: str) -> OutgoingMessage:
    """A standard threaded reply to `original` (same thread, In-Reply-To / References)."""
    text = reply_text(original, body, sender_name)
    subject = original.subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message_id_header = original.header("Message-ID")
    references = original.header("References")
    if message_id_header:
        references = f"{references} {message_id_header}" if references else message_id_header
    return OutgoingMessage(
        to=reply_recipient(original),
        subject=subject,
        text_body=text,
        html_body=f"<html><body><pre>{html.escape(text)}</pre></body></html>",
        sender_name=sender_name,
        thread_id=original.thread_id,
        in_reply_to=message_id_header,
        references=references,
    )

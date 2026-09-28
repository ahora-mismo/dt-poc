"""Unit tests for dt_agent.core.routing (task 12, D-21, D-22, D-47).

Per the testing rules, this covers the routing decision policy itself:
recorded-id skip before everything else, approval-thread detection taking
priority over the SENT-without-approval check (D-22/D-47), and a forged
token block never counting as an approval email. It builds real signed
tokens with `issue` / `render_token_block` rather than hand-rolled JWTs,
and does not re-test PyJWT's own signature checking (that belongs to
`core/approval_token.py`, owned elsewhere).
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dt_agent.core.approval_token import ReplyAction, issue, render_token_block
from dt_agent.core.routing import Route, RouteKind, route
from dt_agent.domain.labels import GMAIL_LABEL_ID_INBOX, GMAIL_LABEL_ID_SENT
from tests.fakes.emails import make_email

_KEY = "a-signing-key-at-least-32-bytes-long-for-hs256"
_OTHER_KEY = "a-different-signing-key-also-32-bytes-long!!"
_TZ = ZoneInfo("Europe/Bucharest")
_SENT_AT = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


def _approval_token_block(key: str = _KEY) -> str:
    action = ReplyAction(
        recipient="sender@example.com",
        thread_id="thread-1",
        message_id="<msg-1@example.com>",
        text="Sure, happy to help.",
    )
    token = issue(
        key=key,
        sent_at=_SENT_AT,
        business_days=3,
        tz=_TZ,
        original_email_id="msg-1",
        action=action,
        shown_text="Approve this reply?",
    )
    return render_token_block(token)


def test_routing_own_sent_id_skipped() -> None:
    email = make_email(id="msg-1")

    result = route(email, is_recorded=True, thread=(email,), signing_key=_KEY)

    assert result == Route(kind=RouteKind.SKIP_OWN)


def test_routing_approval_reply() -> None:
    reply = make_email(
        id="msg-2",
        thread_id="thread-1",
        from_address="sender@example.com",
        text_body="APPROVE",
    )
    approval_email = make_email(
        id="msg-1",
        thread_id="thread-1",
        from_address="mailbox@example.com",
        label_ids=(GMAIL_LABEL_ID_SENT, GMAIL_LABEL_ID_INBOX),
        text_body=f"Please approve.\n\n{_approval_token_block()}",
    )

    result = route(reply, is_recorded=False, thread=(approval_email, reply), signing_key=_KEY)

    assert result == Route(kind=RouteKind.APPROVAL_REPLY)


def test_routing_reply_in_decline_thread_classified() -> None:
    our_decline = make_email(
        id="msg-1",
        thread_id="thread-1",
        from_address="mailbox@example.com",
        label_ids=(GMAIL_LABEL_ID_SENT, GMAIL_LABEL_ID_INBOX),
        text_body="Thanks for reaching out, but we're not pursuing this right now.",
    )
    new_vendor_message = make_email(
        id="msg-2",
        thread_id="thread-1",
        from_address="vendor@example.com",
        text_body="Just following up on my earlier email!",
    )

    result = route(
        new_vendor_message,
        is_recorded=False,
        thread=(our_decline, new_vendor_message),
        signing_key=_KEY,
    )

    assert result == Route(kind=RouteKind.CLASSIFY)


def test_routing_sent_without_token_ignored() -> None:
    sent_message = make_email(
        id="msg-1",
        thread_id="thread-1",
        from_address="mailbox@example.com",
        label_ids=(GMAIL_LABEL_ID_SENT, GMAIL_LABEL_ID_INBOX),
        text_body="Just a note written from the mailbox.",
    )

    result = route(sent_message, is_recorded=False, thread=(sent_message,), signing_key=_KEY)

    assert result == Route(kind=RouteKind.IGNORE_SENT)


def test_routing_order_approval_before_sent() -> None:
    approval_email = make_email(
        id="msg-1",
        thread_id="thread-1",
        from_address="mailbox@example.com",
        label_ids=(GMAIL_LABEL_ID_SENT, GMAIL_LABEL_ID_INBOX),
        text_body=f"Please approve.\n\n{_approval_token_block()}",
    )
    managers_self_reply = make_email(
        id="msg-2",
        thread_id="thread-1",
        from_address="mailbox@example.com",
        label_ids=(GMAIL_LABEL_ID_SENT, GMAIL_LABEL_ID_INBOX),
        text_body="APPROVE",
    )

    result = route(
        managers_self_reply,
        is_recorded=False,
        thread=(approval_email, managers_self_reply),
        signing_key=_KEY,
    )

    assert result == Route(kind=RouteKind.APPROVAL_REPLY)


def test_routing_forged_block_not_approval() -> None:
    reply = make_email(
        id="msg-2",
        thread_id="thread-1",
        from_address="sender@example.com",
        text_body="APPROVE",
    )
    forged_email = make_email(
        id="msg-1",
        thread_id="thread-1",
        from_address="attacker@example.com",
        text_body=f"Please approve.\n\n{_approval_token_block(key=_OTHER_KEY)}",
    )

    result = route(reply, is_recorded=False, thread=(forged_email, reply), signing_key=_KEY)

    assert result == Route(kind=RouteKind.CLASSIFY)

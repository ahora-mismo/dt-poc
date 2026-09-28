"""`GmailAdapter` tests over recorded Gmail API v1 responses (D-52).

`HttpMockSequence` plays back the fixtures in `tests/fixtures/gmail/`
and records every outgoing request in `.request_sequence`, so assertions
are made on decoded request bodies/URLs rather than on a mocked call
chain (testing rules), matching `tests/adapters/test_calendar.py`.
"""

import base64
import json
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage as StdEmailMessage
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from dt_agent.adapters.gmail import GmailAdapter, _body_text
from dt_agent.adapters.google_errors import FatalAuthError, GoogleApiError
from dt_agent.domain.email import EmailAddress
from dt_agent.ports.errors import CursorExpiredError
from dt_agent.ports.mail import OutgoingMessage

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "gmail"


def _fixture_bytes(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _response(name: str, status: str = "200") -> tuple[dict[str, str], bytes]:
    return ({"status": status}, _fixture_bytes(name))


def _build_service(
    responses: list[tuple[dict[str, str], bytes]],
) -> tuple[HttpMockSequence, object]:
    http = HttpMockSequence(responses)
    service = build("gmail", "v1", http=http, static_discovery=True)
    return http, service


def _query(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(uri).query)


async def test_gmail_history_pagination_and_404() -> None:
    http, service = _build_service(
        [
            _response("history_page1.json"),
            _response("history_page2.json"),
        ]
    )
    adapter = GmailAdapter(service)

    page = await adapter.list_history("100")

    assert page.new_message_ids == ("m1", "m2")
    assert page.history_id == "210"

    first_uri, first_method, _, _ = http.request_sequence[0]
    assert first_method == "GET"
    assert _query(first_uri)["startHistoryId"] == ["100"]
    assert _query(first_uri)["labelId"] == ["INBOX"]
    assert _query(first_uri)["historyTypes"] == ["messageAdded"]

    second_uri, _, _, _ = http.request_sequence[1]
    assert _query(second_uri)["pageToken"] == ["page-2"]

    _, service_404 = _build_service([({"status": "404"}, b'{"error": {"message": "Not Found"}}')])
    expired_adapter = GmailAdapter(service_404)

    try:
        await expired_adapter.list_history("gone")
    except CursorExpiredError:
        return
    raise AssertionError("expected CursorExpiredError")


async def test_gmail_message_parsed() -> None:
    _http, service = _build_service([_response("message_raw.json")])
    adapter = GmailAdapter(service)

    email = await adapter.get_message("msg1")

    assert email.id == "msg1"
    assert email.thread_id == "thread1"
    assert email.label_ids == ("INBOX", "UNREAD")
    assert email.from_address.name == "Vendor Sender"
    assert email.from_address.address == "vendor@example.com"
    assert email.to == (EmailAddress(address="agent@example.com"),)
    assert email.cc == (EmailAddress(address="cc@example.com"),)
    assert email.subject == "Special offer with calendar invite"
    assert email.received_at == datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)

    # HTML-only body (no text/plain part): converted to plain text.
    assert "Hello" in email.text_body
    assert "there" in email.text_body
    assert "<b>" not in email.text_body

    assert email.header("Authentication-Results") is not None
    assert "dkim=pass" in (email.header("Authentication-Results") or "")

    assert {a.filename for a in email.attachments} == {"invite.ics", "invoice.pdf"}
    pdf = next(a for a in email.attachments if a.filename == "invoice.pdf")
    assert pdf.mime_type == "application/pdf"
    assert pdf.size > 0

    assert len(email.calendar_parts) == 1
    assert "BEGIN:VCALENDAR" in email.calendar_parts[0].raw_ics
    assert "UID:evt-uid-1" in email.calendar_parts[0].raw_ics

    uri, method, _, _ = _http.request_sequence[0]
    assert method == "GET"
    assert "/messages/msg1" in uri
    assert _query(uri)["format"] == ["raw"]


async def test_gmail_send_threaded_reply() -> None:
    http, service = _build_service([_response("profile.json"), _response("send_response.json")])
    adapter = GmailAdapter(service)

    message = OutgoingMessage(
        to="sender@example.com",
        subject="Re: Special offer",
        text_body="Thanks, but not right now.",
        html_body="<p>Thanks, but not right now.</p>",
        sender_name="Anna's Assistant",
        thread_id="thread1",
        in_reply_to="<msg1@example.com>",
        references="<msg1@example.com>",
    )

    sent_id = await adapter.send(message)

    assert sent_id == "sent-1"

    profile_uri, _, _, _ = http.request_sequence[0]
    assert "/profile" in profile_uri

    send_uri, send_method, send_body, _ = http.request_sequence[1]
    assert send_method == "POST"
    assert "/messages/send" in send_uri

    request = json.loads(send_body)
    assert request["threadId"] == "thread1"
    raw = base64.urlsafe_b64decode(request["raw"] + "=" * (-len(request["raw"]) % 4))
    assert b'From: "Anna\'s Assistant" <agent@example.com>' in raw
    sent_mime = BytesParser(policy=policy.default).parsebytes(raw)

    assert sent_mime["To"] == "sender@example.com"
    assert sent_mime["Subject"] == "Re: Special offer"
    assert sent_mime["In-Reply-To"] == "<msg1@example.com>"
    assert sent_mime["References"] == "<msg1@example.com>"
    assert sent_mime.get_content_type() == "multipart/alternative"

    text_part = sent_mime.get_body(preferencelist=("plain",))
    html_part = sent_mime.get_body(preferencelist=("html",))
    assert text_part is not None and "Thanks" in text_part.get_content()
    assert html_part is not None and "<p>Thanks" in html_part.get_content()


async def test_gmail_labels_ensure_created() -> None:
    http, service = _build_service(
        [
            _response("labels_list_missing_ai.json"),
            _response("labels_create_ai_needsreview.json"),
        ]
    )
    adapter = GmailAdapter(service)

    ids = await adapter.ensure_labels(["TASK", "AI/NeedsReview", "INBOX"])

    assert ids == {"TASK": "Label_1", "AI/NeedsReview": "Label_99", "INBOX": "INBOX"}

    list_uri, list_method, _, _ = http.request_sequence[0]
    assert list_method == "GET"
    assert "/labels" in list_uri

    create_uri, create_method, create_body, _ = http.request_sequence[1]
    assert create_method == "POST"
    assert "/labels" in create_uri
    assert json.loads(create_body)["name"] == "AI/NeedsReview"

    # Second call for the same names: cached, no further HTTP calls made
    # (the mock sequence is now empty; a real call would raise IndexError).
    ids_again = await adapter.ensure_labels(["TASK", "AI/NeedsReview", "INBOX"])
    assert ids_again == ids


async def test_gmail_retry_policy() -> None:
    sleeps: list[float] = []

    # 429 then 200: retried once, succeeds, and the backoff was 1s (D-43).
    http, service = _build_service(
        [
            ({"status": "429"}, b'{"error": {"message": "rate limited"}}'),
            _response("inbox_list_one.json"),
        ]
    )
    adapter = GmailAdapter(service, sleep=sleeps.append)

    ids = await adapter.list_inbox_message_ids(10)

    assert ids == ["m1"]
    assert len(http.request_sequence) == 2
    assert sleeps == [1.0]

    # 400 (generic, no invalid_grant): not retried, propagates as GoogleApiError.
    http_400, service_400 = _build_service(
        [({"status": "400"}, b'{"error": {"message": "Bad Request"}}')]
    )
    adapter_400 = GmailAdapter(service_400, sleep=lambda _: None)

    try:
        await adapter_400.list_inbox_message_ids(10)
    except GoogleApiError:
        pass
    else:
        raise AssertionError("expected GoogleApiError")
    assert len(http_400.request_sequence) == 1

    # invalid_grant: fatal, never retried.
    http_grant, service_grant = _build_service([({"status": "401"}, b'{"error": "invalid_grant"}')])
    adapter_grant = GmailAdapter(service_grant, sleep=lambda _: None)

    try:
        await adapter_grant.list_inbox_message_ids(10)
    except FatalAuthError:
        pass
    else:
        raise AssertionError("expected FatalAuthError")
    assert len(http_grant.request_sequence) == 1


# --------------------------------------------------------------------------
# _body_text: MSO/HTML markup leaking into the text/plain part (task 51)
# --------------------------------------------------------------------------

_MSO_PLAIN_PART = (
    "Alerta de securitate\n\n"
    '<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:w="urn:schemas-microsoft-com:office:word" '
    'href="https://accounts.google.com/verify" style="v-text-anchor:middle;">'
    "<w:anchorlock/><center>Verifica activitatea</center></v:roundrect><![endif]-->\n\n"
    "Am observat activitate neobisnuita pe contul tau. Verifica activitatea contului acum."
)

_MSO_HTML_PART = (
    "<html><body><p>Alerta de securitate</p>"
    "<p>Am observat activitate neobisnuita pe contul tau. "
    '<a href="https://accounts.google.com/verify">Verifica activitatea contului acum</a>.</p>'
    "</body></html>"
)


def _multipart_message(*, plain: str, html: str | None) -> StdEmailMessage:
    msg = StdEmailMessage(policy=policy.default)
    msg["From"] = "no-reply@accounts.google.com"
    msg["Subject"] = "Alerta de securitate"
    msg.set_content(plain)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg


def test_gmail_body_prefers_html_when_plain_part_is_markup() -> None:
    """A `text/plain` part that's actually MSO/VML markup (D-44's link facts never saw
    it, task 51) is ignored in favour of the `text/html` alternative, when there is
    one."""
    msg = _multipart_message(plain=_MSO_PLAIN_PART, html=_MSO_HTML_PART)

    text_body = _body_text(msg)

    assert "<" not in text_body
    assert "Verifica activitatea contului acum (https://accounts.google.com/verify)" in text_body
    assert "Am observat activitate neobisnuita" in text_body


def test_gmail_body_strips_markup_without_html_part() -> None:
    """No `text/html` alternative to fall back on: the markup is stripped out of the
    `text/plain` part directly, keeping the visible text."""
    msg = _multipart_message(plain=_MSO_PLAIN_PART, html=None)

    text_body = _body_text(msg)

    assert "<" not in text_body
    assert "Verifica activitatea" in text_body
    assert "Am observat activitate neobisnuita pe contul tau." in text_body


def test_gmail_body_clean_plain_part_unchanged() -> None:
    """A `text/plain` part with no markup at all passes through untouched."""
    clean = "Hello there, this is a clean plain message with no markup at all."
    msg = _multipart_message(plain=clean, html=None)

    text_body = _body_text(msg)

    assert text_body.strip() == clean

"""Gmail adapter (ADR-2, D-18, D-19, D-22, D-23, D-30, D-31, D-44): `MailPort` over
`google-api-python-client`'s Gmail API v1.

Every blocking `googleapiclient` call runs in a thread
(`anyio.to_thread.run_sync`, D-30: each worker and the poller own their
own `GmailAdapter`) through the shared Google policy in
`adapters/google_errors.py` (retries on 429 / 5xx / rate-limit reasons,
`FatalAuthError` on `invalid_grant` / 401, D-43). Raw messages are parsed
with the stdlib `email` package (`policy.default`) into the provider-
agnostic `Email` domain model; nothing outside this module sees a Gmail
API type.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage as StdEmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from typing import Any

from anyio import to_thread
from google.auth.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from dt_agent.adapters.google_errors import GoogleApiError, run_google_call
from dt_agent.core.html_text import html_to_text
from dt_agent.domain.email import Attachment, CalendarPart, Email, EmailAddress, Headers
from dt_agent.ports.errors import CursorExpiredError, MessageNotFoundError
from dt_agent.ports.mail import HistoryPage, MailPort, OutgoingMessage

# Gmail system labels whose ID equals their name; never created, never cached.
_SYSTEM_LABEL_NAMES = frozenset({"INBOX", "SPAM", "STARRED", "SENT", "UNREAD"})


def _status(exc: HttpError) -> int | None:
    """The HTTP status code of `exc`, or `None` if it can't be read."""
    try:
        return int(exc.resp.status)
    except AttributeError, TypeError, ValueError:
        return None


def _decode_raw(raw_b64url: str) -> bytes:
    """Decode Gmail's base64url `raw` field, padding it if Gmail omitted the padding."""
    padded = raw_b64url + "=" * (-len(raw_b64url) % 4)
    return base64.urlsafe_b64decode(padded)


def _to_address(address: Address) -> EmailAddress:
    return EmailAddress(name=address.display_name, address=address.addr_spec)


def _addresses(msg: StdEmailMessage, header_name: str) -> tuple[EmailAddress, ...]:
    header = msg.get(header_name)
    if header is None:
        return ()
    return tuple(_to_address(address) for address in header.addresses)


# Some senders' `text/plain` MIME part isn't plain text at all: Outlook/Word HTML
# ("MSO") exports its conditional comments and VML markup verbatim into the plain-text
# alternative instead of a real text rendering (task 51 -- three genuine, authenticated
# Google notices lost their link/bulk-mail facts this way, since `_LINK_RE` only
# understands `html_to_text`'s "text (href)" shape). These markers catch that markup so
# the `text/html` part (or, failing that, a stripped version of the same plain text) is
# used instead.
_MSO_MARKUP_MARKERS = ("<!--[if", "<![endif]", "<v:", "<w:", "<o:")
_HTML_TAG_RE = re.compile(r"<(?:table|div|td|span|p[ >]|br[ />]|a\s+href)", re.IGNORECASE)


def _looks_like_markup(text: str) -> bool:
    """Whether `text` (nominally `text/plain`) actually contains HTML/MSO markup."""
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in _MSO_MARKUP_MARKERS):
        return True
    return bool(_HTML_TAG_RE.search(text))


# Matches only the opening/closing delimiters of an MSO conditional comment, not its
# content, so any genuine visible text sandwiched inside is kept (`_strip_markup`).
_COND_COMMENT_MARKER_RE = re.compile(r"<!--\[if[^>]*\]>|<!\[endif\]-->", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _strip_markup(text: str) -> str:
    """Strip conditional-comment delimiters and tags from `text`, keeping visible text."""
    text = _COND_COMMENT_MARKER_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = _EXCESS_BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _part_text(part: Any) -> str:
    """Decode one message part's content to `str`, whatever `get_content()` returned."""
    content = part.get_content()
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    return str(content)


def _body_text(msg: StdEmailMessage) -> str:
    """The email's text body: `text/plain` preferred, else `html_to_text` on `text/html`.

    A `text/plain` part that actually contains HTML/MSO markup (task 51) is treated as
    unusable: if the message also has a `text/html` part, that's converted with
    `html_to_text` instead (so link/bulk-mail facts still see it); otherwise the markup
    is stripped out of the plain text directly, keeping the visible sentences.
    """
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = _part_text(part)
    if part.get_content_type() == "text/html":
        return html_to_text(content)
    if _looks_like_markup(content):
        html_part = msg.get_body(preferencelist=("html",))
        if html_part is not None:
            return html_to_text(_part_text(html_part))
        return _strip_markup(content)
    return content


def _attachments(msg: StdEmailMessage) -> tuple[Attachment, ...]:
    result: list[Attachment] = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        result.append(
            Attachment(
                filename=part.get_filename() or "",
                mime_type=part.get_content_type(),
                size=len(payload),
            )
        )
    return tuple(result)


def _calendar_parts(msg: StdEmailMessage) -> tuple[CalendarPart, ...]:
    """`text/calendar` parts and `.ics` attachments, as raw iCalendar text (D-48)."""
    result: list[CalendarPart] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename() or ""
        if part.get_content_type() == "text/calendar":
            content = part.get_content()
            if isinstance(content, bytes):
                text = content.decode("utf-8", "replace")
            elif isinstance(content, str):
                text = content
            else:
                text = str(content)
            result.append(CalendarPart(raw_ics=text))
        elif filename.lower().endswith(".ics"):
            raw_payload = part.get_payload(decode=True)
            payload = raw_payload if isinstance(raw_payload, bytes) else b""
            result.append(CalendarPart(raw_ics=payload.decode("utf-8", "replace")))
    return tuple(result)


def _parse_message(
    raw: bytes,
    *,
    message_id: str,
    thread_id: str,
    label_ids: tuple[str, ...],
    internal_date_ms: str,
) -> Email:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    from_header = msg.get("From")
    from_address = (
        _to_address(from_header.addresses[0])
        if from_header is not None and from_header.addresses
        else EmailAddress(address="")
    )
    reply_to_header = msg.get("Reply-To")
    reply_to = (
        _to_address(reply_to_header.addresses[0])
        if reply_to_header is not None and reply_to_header.addresses
        else None
    )
    return Email(
        id=message_id,
        thread_id=thread_id,
        label_ids=label_ids,
        headers=Headers(values={name: str(value) for name, value in msg.items()}),
        from_address=from_address,
        reply_to=reply_to,
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        subject=str(msg.get("Subject", "")),
        received_at=datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=UTC),
        text_body=_body_text(msg),
        attachments=_attachments(msg),
        calendar_parts=_calendar_parts(msg),
    )


class GmailAdapter:
    """`MailPort` over one Gmail API `Resource` for the mailbox `me` (D-30)."""

    def __init__(self, service: Resource, *, sleep: Callable[[float], None] | None = None) -> None:
        self._service = service
        self._sleep = sleep
        self._label_cache: dict[str, str] = {}
        self._mailbox_address: str | None = None

    async def _execute(self, request: Any) -> dict[str, Any]:
        """Run one Gmail call under the shared retry/fatal-auth policy (D-43).

        Any `HttpError` left after that policy is re-raised unchanged only
        for 404 (callers map it to a specific port error); every other
        `HttpError` becomes `GoogleApiError` here, once, so callers never
        see a raw Gmail API exception.
        """

        def call() -> dict[str, Any]:
            result: dict[str, Any] = request.execute()
            return result

        try:
            return await to_thread.run_sync(lambda: run_google_call(call, sleep=self._sleep))
        except HttpError as exc:
            if _status(exc) == 404:
                raise
            raise GoogleApiError(str(exc)) from exc

    async def get_profile_address(self) -> str:
        if self._mailbox_address is None:
            profile = await self._execute(self._service.users().getProfile(userId="me"))
            self._mailbox_address = str(profile["emailAddress"])
        return self._mailbox_address

    async def get_profile_history_id(self) -> str:
        profile = await self._execute(self._service.users().getProfile(userId="me"))
        history_id: str = profile["historyId"]
        return history_id

    async def list_inbox_message_ids(self, max_results: int) -> list[str]:
        resp = await self._execute(
            self._service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
        )
        return [item["id"] for item in resp.get("messages", [])]

    async def list_history(self, start_history_id: str) -> HistoryPage:
        ids: list[str] = []
        seen: set[str] = set()
        history_id = start_history_id
        page_token: str | None = None
        while True:
            request = (
                self._service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    labelId="INBOX",
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
            )
            try:
                resp = await self._execute(request)
            except HttpError as exc:
                raise CursorExpiredError(f"history cursor expired: {start_history_id}") from exc
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    message_id = added["message"]["id"]
                    if message_id not in seen:
                        seen.add(message_id)
                        ids.append(message_id)
            history_id = resp.get("historyId", history_id)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return HistoryPage(new_message_ids=tuple(ids), history_id=history_id)

    async def _get_raw(self, message_id: str) -> dict[str, Any]:
        try:
            return await self._execute(
                self._service.users().messages().get(userId="me", id=message_id, format="raw")
            )
        except HttpError as exc:
            raise MessageNotFoundError(message_id) from exc

    async def get_message(self, message_id: str) -> Email:
        resp = await self._get_raw(message_id)
        return _parse_message(
            _decode_raw(resp["raw"]),
            message_id=resp["id"],
            thread_id=resp["threadId"],
            label_ids=tuple(resp.get("labelIds", ())),
            internal_date_ms=resp.get("internalDate", "0"),
        )

    async def get_thread(self, thread_id: str) -> list[Email]:
        resp = await self._execute(
            self._service.users().threads().get(userId="me", id=thread_id, format="minimal")
        )
        return [await self.get_message(item["id"]) for item in resp.get("messages", [])]

    async def ensure_labels(self, names: Sequence[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        missing: list[str] = []
        for name in names:
            if name in _SYSTEM_LABEL_NAMES:
                result[name] = name
            elif name in self._label_cache:
                result[name] = self._label_cache[name]
            else:
                missing.append(name)

        if missing:
            listed = await self._execute(self._service.users().labels().list(userId="me"))
            for label in listed.get("labels", []):
                self._label_cache[label["name"]] = label["id"]
            for name in missing:
                if name not in self._label_cache:
                    created = await self._execute(
                        self._service.users()
                        .labels()
                        .create(
                            userId="me",
                            body={
                                "name": name,
                                "labelListVisibility": "labelShow",
                                "messageListVisibility": "show",
                            },
                        )
                    )
                    self._label_cache[name] = created["id"]
                result[name] = self._label_cache[name]
        return result

    async def modify_labels(
        self, message_id: str, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        ids = await self.ensure_labels((*add, *remove))
        body: dict[str, Any] = {}
        if add:
            body["addLabelIds"] = [ids[name] for name in add]
        if remove:
            body["removeLabelIds"] = [ids[name] for name in remove]
        await self._execute(
            self._service.users().messages().modify(userId="me", id=message_id, body=body)
        )

    async def send(self, message: OutgoingMessage) -> str:
        mailbox_address = await self.get_profile_address()

        mime = MIMEMultipart("alternative")
        mime["From"] = f'"{message.sender_name}" <{mailbox_address}>'
        mime["To"] = message.to
        mime["Subject"] = message.subject
        if message.in_reply_to:
            mime["In-Reply-To"] = message.in_reply_to
        if message.references:
            mime["References"] = message.references
        mime.attach(MIMEText(message.text_body, "plain", "utf-8"))
        mime.attach(MIMEText(message.html_body, "html", "utf-8"))

        body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")}
        if message.thread_id:
            body["threadId"] = message.thread_id

        resp = await self._execute(self._service.users().messages().send(userId="me", body=body))
        sent_id: str = resp["id"]
        return sent_id


def build_gmail_adapter(credentials: Credentials) -> GmailAdapter:
    """Build a `GmailAdapter` from Google credentials (`google_auth.build_credentials`, D-30)."""
    service = build(
        "gmail", "v1", credentials=credentials, static_discovery=True, cache_discovery=False
    )
    return GmailAdapter(service)


# Structural check, not a test: catches this adapter drifting from the
# port's shape (a missing method, a wrong signature) at `mypy` time.
def _mail_port_check(service: Resource) -> MailPort:
    return GmailAdapter(service)

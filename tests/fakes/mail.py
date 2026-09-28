"""In-memory `MailPort` fake (D-52).

Deterministic and hand-written, per the testing rules: state (messages,
threads, history, sent messages) is asserted on directly; nothing here
mocks `googleapiclient`. `fail_next(exc)` makes the *next* call to any
method raise `exc` instead of acting.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from dt_agent.domain.clock import Clock, FixedClock
from dt_agent.domain.email import Email, EmailAddress, Headers
from dt_agent.domain.labels import GMAIL_LABEL_ID_INBOX, GMAIL_LABEL_ID_SENT, GMAIL_LABEL_ID_SPAM
from dt_agent.ports.errors import MessageNotFoundError
from dt_agent.ports.mail import HistoryPage, MailPort, OutgoingMessage

_DEFAULT_INSTANT = datetime(2026, 1, 1, tzinfo=UTC)


class FakeMail:
    """Seed messages with `add_message`; inspect state via `get_message`/`get_thread`/`sent`."""

    def __init__(self, address: str = "mailbox@example.com", clock: Clock | None = None) -> None:
        self._address = address
        self._clock = clock or FixedClock(_DEFAULT_INSTANT)
        self._messages: dict[str, Email] = {}
        self._threads: dict[str, list[str]] = {}
        self._history: list[tuple[int, str]] = []
        self._history_seq = 0
        self._next_sent_id = 0
        self._label_ids: dict[str, str] = {}
        self._pending_exc: Exception | None = None
        self.sent: list[OutgoingMessage] = []

    def fail_next(self, exc: Exception) -> None:
        """The next call to any `MailPort` method raises `exc` instead of acting."""
        self._pending_exc = exc

    def add_message(self, email: Email) -> None:
        """Seed `email` (build one with `tests.fakes.emails.make_email`).

        Joins `email.thread_id` and, if `email` carries `INBOX`, appends it
        to the history the poller reads (D-22's self-addressed-mail case
        relies on this too).
        """
        self._messages[email.id] = email
        thread = self._threads.setdefault(email.thread_id, [])
        if email.id not in thread:
            thread.append(email.id)
        if GMAIL_LABEL_ID_INBOX in email.label_ids:
            self._record_history(email.id)

    def _check_fail(self) -> None:
        if self._pending_exc is not None:
            exc, self._pending_exc = self._pending_exc, None
            raise exc

    def _record_history(self, message_id: str) -> None:
        self._history_seq += 1
        self._history.append((self._history_seq, message_id))

    async def get_profile_address(self) -> str:
        self._check_fail()
        return self._address

    async def get_profile_history_id(self) -> str:
        self._check_fail()
        return str(self._history_seq)

    async def list_inbox_message_ids(self, max_results: int) -> list[str]:
        self._check_fail()
        inbox = [m for m in self._messages.values() if GMAIL_LABEL_ID_INBOX in m.label_ids]
        inbox.sort(key=lambda m: m.received_at, reverse=True)
        return [m.id for m in inbox[:max_results]]

    async def list_history(self, start_history_id: str) -> HistoryPage:
        self._check_fail()
        start = int(start_history_id)
        new_ids = tuple(mid for seq, mid in self._history if seq > start)
        return HistoryPage(new_message_ids=new_ids, history_id=str(self._history_seq))

    async def get_message(self, message_id: str) -> Email:
        self._check_fail()
        email = self._messages.get(message_id)
        if email is None:
            raise MessageNotFoundError(message_id)
        return email

    async def get_thread(self, thread_id: str) -> list[Email]:
        self._check_fail()
        return [self._messages[mid] for mid in self._threads.get(thread_id, [])]

    async def ensure_labels(self, names: Sequence[str]) -> dict[str, str]:
        self._check_fail()
        for name in names:
            self._label_ids.setdefault(name, name)
        return {name: self._label_ids[name] for name in names}

    async def modify_labels(
        self, message_id: str, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        self._check_fail()
        email = self._messages.get(message_id)
        if email is None:
            raise MessageNotFoundError(message_id)
        labels = set(email.label_ids) | set(add)
        labels -= set(remove)
        if GMAIL_LABEL_ID_SPAM in labels:
            labels.discard(GMAIL_LABEL_ID_INBOX)  # D-44: SPAM moves the message out of the inbox
        self._messages[message_id] = email.model_copy(update={"label_ids": tuple(sorted(labels))})

    async def send(self, message: OutgoingMessage) -> str:
        self._check_fail()
        self._next_sent_id += 1
        message_id = f"sent-{self._next_sent_id}"
        thread_id = message.thread_id or message_id
        label_ids = {GMAIL_LABEL_ID_SENT}
        if message.to.strip().lower() == self._address.lower():
            label_ids.add(GMAIL_LABEL_ID_INBOX)  # D-22: self-addressed mail also lands in INBOX
        headers = {"From": self._address, "To": message.to, "Subject": message.subject}
        if message.in_reply_to:
            headers["In-Reply-To"] = message.in_reply_to
        if message.references:
            headers["References"] = message.references
        email = Email(
            id=message_id,
            thread_id=thread_id,
            label_ids=tuple(sorted(label_ids)),
            headers=Headers(values=headers),
            from_address=EmailAddress(name=message.sender_name, address=self._address),
            to=(EmailAddress(address=message.to),),
            subject=message.subject,
            received_at=self._clock.now(),
            text_body=message.text_body,
        )
        self._messages[message_id] = email
        self._threads.setdefault(thread_id, []).append(message_id)
        if GMAIL_LABEL_ID_INBOX in label_ids:
            self._record_history(message_id)
        self.sent.append(message)
        return message_id


# Structural check, not a test: catches a fake that drifts from the port's
# shape (a missing method, a wrong signature) at `mypy` time rather than
# eight tasks later when the pipeline first calls it.
_mail_port_check: MailPort = FakeMail()

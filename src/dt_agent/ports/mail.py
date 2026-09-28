"""Gmail port (ADR-2, D-18 to D-23, D-30, D-31, D-44).

`MailPort` is implemented by `adapters/gmail.py` (task 15), which wraps
blocking `google-api-python-client` calls in `anyio.to_thread.run_sync`
(each worker and the poller owning its own client instance, D-30), and by
`tests/fakes/mail.py`'s `FakeMail` for unit tests. Every method is async;
none exposes a raw Gmail API type.
"""

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from dt_agent.domain.email import Email


class OutgoingMessage(BaseModel):
    """One message to send through `MailPort.send` (D-23, D-38, D-39, D-40, ADR-4).

    `sender_name` is `AGENT_SENDER_NAME` for every outgoing message (D-23);
    the sending address is always the mailbox's own and is never chosen
    here. `thread_id` (with `in_reply_to`/`references`) replies inside an
    existing Gmail thread; left `None` starts a new thread.
    """

    model_config = ConfigDict(frozen=True)

    to: str
    subject: str
    text_body: str
    html_body: str
    sender_name: str
    thread_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None


class HistoryPage(BaseModel):
    """One `list_history` result: new INBOX message IDs plus the advanced cursor (D-31)."""

    model_config = ConfigDict(frozen=True)

    new_message_ids: tuple[str, ...]
    history_id: str


class MailPort(Protocol):
    """Gmail operations the pipeline needs, provider-agnostic (ADR-2)."""

    async def get_profile_address(self) -> str:
        """The mailbox's own address (`users.getProfile`, D-18); never configured directly."""
        ...

    async def get_profile_history_id(self) -> str:
        """The current history cursor (`users.getProfile`), the poller's starting point (D-31)."""
        ...

    async def list_inbox_message_ids(self, max_results: int) -> list[str]:
        """The last `max_results` INBOX message IDs, newest first (D-7, D-21)."""
        ...

    async def list_history(self, start_history_id: str) -> HistoryPage:
        """New INBOX `messageAdded` IDs since `start_history_id` (D-31).

        Raises `CursorExpiredError` when Gmail returns 404 for the cursor.
        """
        ...

    async def get_message(self, message_id: str) -> Email:
        """One message, fully parsed. Raises `MessageNotFoundError` if it's gone."""
        ...

    async def get_thread(self, thread_id: str) -> list[Email]:
        """Every message in a thread, in Gmail's order (D-39, D-47, D-49)."""
        ...

    async def ensure_labels(self, names: Sequence[str]) -> dict[str, str]:
        """Create any of `names` that don't exist yet; return name -> label id (D-25)."""
        ...

    async def modify_labels(
        self, message_id: str, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        """Add/remove labels, by name or system ID (e.g. archiving removes `INBOX`)."""
        ...

    async def send(self, message: OutgoingMessage) -> str:
        """Send `message`; returns the new Gmail message id (recorded immediately, D-22)."""
        ...

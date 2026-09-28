"""SQLite processed-messages port (D-6, D-26, D-27, D-28).

Implemented by `adapters/sqlite_store.py` (task 6), which wraps blocking
`sqlite3` calls in a thread. Per D-52, tests use the real adapter against
a temporary file rather than a fake, so no fake is written for this port.
"""

from datetime import datetime
from typing import Protocol


class MessageStore(Protocol):
    """The single table of processed messages, including the agent's own sent ones (D-22, D-26)."""

    async def contains(self, message_id: str) -> bool:
        """Whether `message_id` was already recorded (D-21, D-22, D-27)."""
        ...

    async def record(
        self, message_id: str, sender: str, subject: str, message_date: datetime
    ) -> None:
        """Record one processed (or sent) message, at the end of its flow (D-22, D-27)."""
        ...

    async def purge_older_than(self, days: int) -> int:
        """Delete rows whose `message_date` is older than `days`; returns the row count (D-28)."""
        ...

    async def close(self) -> None:
        """Close the underlying connection; called once, at shutdown (task 35)."""
        ...

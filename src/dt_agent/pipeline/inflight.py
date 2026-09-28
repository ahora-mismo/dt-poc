"""In-flight message ids, shared by the poller and the worker pools (task 35).

Closes two windows that would otherwise re-queue a message the app is
already handling:

- cursor-expiry re-backfill (D-31): `first_iteration` restarting after
  `CursorExpiredError` would otherwise re-list an id it already queued but
  no worker has finished yet;
- the D-22 send-then-record window: an id sent to the queue but not yet
  recorded in the store, which a live poll could otherwise re-queue.

The poller adds an id when it queues it; a `WorkerPool` removes it once
that message's handling ends, on every path (success, give-up,
non-retryable, or skipped because it's already recorded).

A plain `set[str]` is safe here with no lock: every access -- the
poller's coroutine and `WorkerPool._handle_one` -- runs on the event loop
itself. The only thread-hopping in this codebase is `SqliteStore`'s
blocking `sqlite3` calls (`anyio.to_thread.run_sync`), and nothing inside
those callables touches this set.
"""

from __future__ import annotations


class InFlight:
    """The set of message ids currently queued or being processed."""

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def add(self, message_id: str) -> None:
        self._ids.add(message_id)

    def discard(self, message_id: str) -> None:
        self._ids.discard(message_id)

    def __contains__(self, message_id: str) -> bool:
        return message_id in self._ids

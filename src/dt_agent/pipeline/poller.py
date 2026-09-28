"""Poller: owns all Gmail message intake (task 21, D-7, D-17, D-19, D-21, D-28, D-31).

One `Poller` instance handles both the startup backlog and live polling,
behind `MailPort` (ADR-2) so the mechanism (Gmail `history.list`) stays out
of the queue and the worker pool (D-30):

- `first_iteration()` takes the history cursor (`get_profile_history_id`),
  lists the last `backfill_messages` INBOX ids (`list_inbox_message_ids`),
  and sends every one that isn't already in the store and isn't older than
  `retention_days` (D-7, D-21, D-28). The raw list of ids is kept in memory
  so the very next iteration can skip them too (D-31).
- Every following call to `tick()` lists new history since the cursor,
  advances it, and sends new ids, skipping ones already in the store, ones
  from the remembered first list (dropped once this iteration finishes)
  and ones repeated within the same batch (D-31).
- `run()` loops `tick()` forever, sleeping `poll_interval_seconds` plus a
  small `jitter()` between calls, via the injected `sleeper` (D-19, D-52).

`CursorExpiredError` (Gmail's cursor is gone, HTTP 404) makes `tick()` fall
back to `first_iteration()`. `FatalAuthError` propagates so the app exits
(D-43, D-50). Any other exception during a tick is logged and swallowed:
per D-43, "the poller never stops on its own".

An id already queued or being processed (`InFlight`, shared with the
worker pools) is never queued again by either loop, next to the store
check -- closing the cursor-expiry re-backfill window and the D-22
send-then-record window (task 35).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from dt_agent.domain.clock import Clock
from dt_agent.logging import get_logger
from dt_agent.pipeline.inflight import InFlight
from dt_agent.ports.errors import CursorExpiredError, FatalAuthError, MessageNotFoundError
from dt_agent.ports.mail import MailPort
from dt_agent.ports.sleep import Sleeper
from dt_agent.ports.store import MessageStore


class Poller:
    """Owns Gmail intake: the startup backfill, then live polling (D-31)."""

    def __init__(
        self,
        mail: MailPort,
        store: MessageStore,
        send: Callable[[str], None],
        *,
        backfill_messages: int,
        retention_days: int,
        poll_interval_seconds: int,
        clock: Clock,
        sleeper: Sleeper,
        jitter: Callable[[], float],
        in_flight: InFlight | None = None,
    ) -> None:
        self._mail = mail
        self._store = store
        self._send = send
        self._backfill_messages = backfill_messages
        self._retention_days = retention_days
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._jitter = jitter
        self._in_flight = in_flight if in_flight is not None else InFlight()
        self._logger = get_logger(__name__)
        self._cursor: str | None = None
        self._first_list: frozenset[str] = frozenset()

    async def run(self) -> None:
        """Loop `tick()` forever, pausing `poll_interval_seconds` (+ jitter) between calls."""
        while True:
            await self.tick()
            await self._sleeper(self._poll_interval_seconds + self._jitter())

    async def tick(self) -> None:
        """One iteration: the first one if the poller has no cursor yet, else the next one.

        Never raises: `CursorExpiredError` restarts the first iteration; any
        other exception is logged and the poller carries on at the next
        tick (D-43).
        """
        try:
            if self._cursor is None:
                await self.first_iteration()
            else:
                await self._next_iteration()
        except FatalAuthError:
            raise
        except CursorExpiredError:
            self._logger.warning("poller_cursor_expired_restarting_first_iteration")
            try:
                await self.first_iteration()
            except FatalAuthError:
                raise
            except Exception as exc:
                self._logger.warning(
                    "poller_tick_failed", reason=str(exc), stage="first_iteration_restart"
                )
        except Exception as exc:
            self._logger.warning("poller_tick_failed", reason=str(exc))

    async def first_iteration(self) -> None:
        """The startup backfill (D-7, D-21, D-28): list, filter, send, remember.

        A cursor is read into a local first (D-43): `self._cursor` and
        `self._first_list` are only committed after the send loop, so a
        failure that aborts this call never advances the poller's own
        state and the next tick can retry cleanly. Per id, `MessageNotFoundError`
        (the message is gone) and any other non-fatal error are logged and
        skipped rather than aborting the whole batch; `FatalAuthError`
        always propagates (D-43, D-50).
        """
        cursor = await self._mail.get_profile_history_id()
        ids = (
            await self._mail.list_inbox_message_ids(self._backfill_messages)
            if self._backfill_messages > 0
            else []
        )

        to_send: list[str] = []
        seen: set[str] = set()
        for message_id in ids:
            if message_id in seen:
                continue
            seen.add(message_id)
            if message_id in self._in_flight:
                continue
            if await self._store.contains(message_id):
                continue
            try:
                email = await self._mail.get_message(message_id)
            except MessageNotFoundError:
                self._logger.info("poller_backfill_message_gone", message_id=message_id)
                continue
            except FatalAuthError:
                raise
            except Exception as exc:
                self._logger.warning(
                    "poller_backfill_message_failed",
                    message_id=message_id,
                    reason=str(exc),
                )
                continue
            if self._is_older_than_retention(email.received_at):
                continue
            to_send.append(message_id)

        for message_id in to_send:
            self._in_flight.add(message_id)
            self._send(message_id)

        self._first_list = frozenset(ids)
        self._cursor = cursor

    async def _next_iteration(self) -> None:
        """A live poll (D-17, D-19, D-31): history since the cursor, filtered, sent."""
        if self._cursor is None:
            raise RuntimeError("_next_iteration called before a cursor was established")

        page = await self._mail.list_history(self._cursor)

        seen: set[str] = set()
        to_send: list[str] = []
        for message_id in page.new_message_ids:
            if message_id in seen:
                continue
            seen.add(message_id)
            if message_id in self._first_list:
                continue
            if message_id in self._in_flight:
                continue
            if await self._store.contains(message_id):
                continue
            to_send.append(message_id)

        for message_id in to_send:
            self._in_flight.add(message_id)
            self._send(message_id)

        # Committed only now (D-43): if filtering raised above, neither the
        # cursor nor the first-list is touched, so a failed tick re-reads
        # the same history page at the next tick instead of losing it.
        self._first_list = frozenset()
        self._cursor = page.history_id

    def _is_older_than_retention(self, received_at: datetime) -> bool:
        cutoff = self._clock.now() - timedelta(days=self._retention_days)
        return received_at < cutoff

"""The processing queue and worker pool (D-20, D-27, D-30, D-43).

`create_queue` builds the unbounded `anyio` memory object stream producers
(the startup backfill and the poller, tasks 21/24) push message IDs into
with `send_nowait` (D-30). `WorkerPool` runs `WORKER_COUNT` workers, each
reading from its own clone of the receive end, so every message is taken
by exactly one worker.

Per message, the pool re-checks the store (D-22/D-27: a message recorded
between being queued and being taken is skipped) and then calls the
injected per-email handler (`process_email`, task 19). The handler itself
records success at the end of its own flow (D-27); this module only
records on the two failure paths it owns:

- non-retryable (`MessageNotFoundError`): no retry, recorded, logged;
- exhausted retries: label `AI/Error` (if the message still exists),
  recorded, logged with `outcome=gave_up`.

A failure in one message is always contained: it never stops the worker
from taking the next message, and never propagates out of `run()`.

An optional shared `InFlight` (task 35) is released for a message id once
its handling ends, on every path: success, give-up, non-retryable, or
skipped because it's already recorded.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from dt_agent.domain.email import EmailAddress
from dt_agent.domain.labels import AI_ERROR_LABEL
from dt_agent.logging import bind_email_context, get_logger, log_email_processed
from dt_agent.pipeline.inflight import InFlight
from dt_agent.ports.errors import FatalAuthError, MessageNotFoundError
from dt_agent.ports.mail import MailPort
from dt_agent.ports.sleep import Sleeper
from dt_agent.ports.store import MessageStore

Handler = Callable[[str, int], Awaitable[None]]

_PLACEHOLDER_SENDER = "unknown"
_PLACEHOLDER_SUBJECT = "(message no longer available)"

logger = get_logger(__name__)


def create_queue() -> tuple[MemoryObjectSendStream[str], MemoryObjectReceiveStream[str]]:
    """The processing queue: an unbounded `anyio` memory object stream (D-30)."""
    return anyio.create_memory_object_stream[str](max_buffer_size=math.inf)


def _format_sender(address: EmailAddress) -> str:
    if address.name:
        return f"{address.name} <{address.address}>"
    return address.address


class WorkerPool:
    """`worker_count` workers sharing one queue, one message at a time each (D-27, D-30)."""

    def __init__(
        self,
        receive_stream: MemoryObjectReceiveStream[str],
        *,
        worker_count: int,
        first_worker_id: int = 0,
        handler: Handler,
        store: MessageStore,
        mail: MailPort,
        max_attempts: int,
        action_delay_ms: int,
        sleeper: Sleeper,
        retry_delay_seconds: float,
        in_flight: InFlight | None = None,
    ) -> None:
        self._receive_stream = receive_stream
        self._worker_count = worker_count
        self._first_worker_id = first_worker_id
        self._handler = handler
        self._store = store
        self._mail = mail
        self._max_attempts = max_attempts
        self._action_delay_ms = action_delay_ms
        self._sleeper = sleeper
        self._retry_delay_seconds = retry_delay_seconds
        self._in_flight = in_flight if in_flight is not None else InFlight()

    async def run(self) -> None:
        """Run every worker until the queue's send side closes (D-30)."""
        async with anyio.create_task_group() as task_group:
            for index in range(self._worker_count):
                task_group.start_soon(self._worker_loop, self._first_worker_id + index)

    async def _worker_loop(self, worker_id: int) -> None:
        async with self._receive_stream.clone() as receive_stream:
            async for message_id in receive_stream:
                await self._handle_one(message_id, worker_id)

    async def _handle_one(self, message_id: str, worker_id: int) -> None:
        with bind_email_context(message_id, str(worker_id)):
            try:
                try:
                    if await self._store.contains(message_id):
                        logger.info("worker.already_recorded", message_id=message_id)
                    else:
                        await self._process(message_id)
                except FatalAuthError:
                    raise  # the credentials are dead: stop the app (D-43, D-50)
                except Exception as exc:  # a failure never kills a worker (D-43)
                    logger.error(
                        "worker.unexpected_error",
                        message_id=message_id,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
            finally:
                # The id leaves the in-flight set on every path: success,
                # give-up, non-retryable, already-recorded, and unexpected
                # error (task 35).
                self._in_flight.discard(message_id)
        await self._sleeper(self._action_delay_ms / 1000)

    async def _process(self, message_id: str) -> None:
        attempt = 1
        while True:
            try:
                await self._handler(message_id, attempt)
            except MessageNotFoundError as exc:
                logger.info(
                    "worker.non_retryable_error",
                    message_id=message_id,
                    error_type=type(exc).__name__,
                    reason=str(exc),
                    outcome="skipped",
                )
                await self._record_after_failure(
                    message_id, action=[], outcome="skipped", reason=str(exc)
                )
                return
            except FatalAuthError:
                raise
            except Exception as exc:  # a failure never kills a worker (D-43)
                logger.warning(
                    "worker.attempt_failed",
                    message_id=message_id,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    outcome="retrying" if attempt < self._max_attempts else "gave_up",
                )
                if attempt >= self._max_attempts:
                    await self._give_up(message_id)
                    return
                attempt += 1
                await self._sleeper(self._retry_delay_seconds)
            else:
                return

    async def _give_up(self, message_id: str) -> None:
        with contextlib.suppress(MessageNotFoundError):
            await self._mail.modify_labels(message_id, add=[AI_ERROR_LABEL])
        logger.info("worker.gave_up", message_id=message_id, outcome="gave_up")
        await self._record_after_failure(
            message_id,
            action=["label_ai_error"],
            outcome="error",
            reason=f"failed {self._max_attempts} attempt(s)",
        )

    async def _record_after_failure(
        self, message_id: str, *, action: list[str], outcome: str, reason: str
    ) -> None:
        """Record the failed email as processed and write its audit entry (D-43, D-58)."""
        try:
            email = await self._mail.get_message(message_id)
        except MessageNotFoundError:
            sender = _PLACEHOLDER_SENDER
            subject = _PLACEHOLDER_SUBJECT
            message_date = datetime.now(UTC)
        else:
            sender = _format_sender(email.from_address)
            subject = email.subject
            message_date = email.received_at
        await self._store.record(message_id, sender, subject, message_date)
        log_email_processed(
            logger,
            message_id=message_id,
            sender=sender,
            subject=subject,
            primary=None,
            urgent=False,
            actions=action,
            outcome=outcome,
            reason=reason,
        )

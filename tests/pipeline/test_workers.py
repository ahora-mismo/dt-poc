"""Tests for the queue and worker pool (task 22, D-20, D-27, D-30, D-43).

Real `SqliteStore` in a temporary directory, `FakeMail` and `FakeSleeper`
per the testing rules; nothing here actually sleeps. The per-email handler
(task 19) is a plain injected async callable, never the real
`process_email`.

Test description format: test_<unit>_<condition>_<expected>.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from structlog.testing import capture_logs

from dt_agent.adapters.sqlite_store import SqliteStore, open_store
from dt_agent.domain.clock import FixedClock
from dt_agent.domain.labels import AI_ERROR_LABEL
from dt_agent.pipeline.inflight import InFlight
from dt_agent.pipeline.workers import WorkerPool, create_queue
from dt_agent.ports.errors import MessageNotFoundError
from tests.fakes.emails import make_email
from tests.fakes.mail import FakeMail
from tests.fakes.sleep import FakeSleeper

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> SqliteStore:
    store, report = open_store(tmp_path / "agent.db", FixedClock(NOW))
    assert report is None
    return store


async def test_workers_each_message_once(tmp_path: Path) -> None:
    send, receive = create_queue()
    message_ids = [f"msg-{i}" for i in range(10)]
    for message_id in message_ids:
        send.send_nowait(message_id)
    send.close()

    handled: list[str] = []

    async def handler(message_id: str, attempt: int) -> None:
        handled.append(message_id)

    pool = WorkerPool(
        receive,
        worker_count=2,
        handler=handler,
        store=_store(tmp_path),
        mail=FakeMail(),
        max_attempts=1,
        action_delay_ms=0,
        sleeper=FakeSleeper(),
        retry_delay_seconds=0,
    )

    await pool.run()

    assert sorted(handled) == sorted(message_ids)


async def test_workers_recheck_store_on_take(tmp_path: Path) -> None:
    send, receive = create_queue()
    send.send_nowait("msg-1")
    send.close()

    store = _store(tmp_path)
    await store.record("msg-1", "sender@example.com", "Subject", NOW)

    called = False

    async def handler(message_id: str, attempt: int) -> None:
        nonlocal called
        called = True

    pool = WorkerPool(
        receive,
        worker_count=1,
        handler=handler,
        store=store,
        mail=FakeMail(),
        max_attempts=1,
        action_delay_ms=0,
        sleeper=FakeSleeper(),
        retry_delay_seconds=0,
    )

    await pool.run()

    assert called is False


async def test_workers_max_attempts_then_error_label(tmp_path: Path) -> None:
    send, receive = create_queue()
    send.send_nowait("bad-1")
    send.send_nowait("good-2")
    send.close()

    mail = FakeMail()
    mail.add_message(make_email(id="bad-1"))

    attempts: list[int] = []
    handled: list[str] = []

    async def handler(message_id: str, attempt: int) -> None:
        if message_id == "bad-1":
            attempts.append(attempt)
            raise RuntimeError("boom")
        handled.append(message_id)

    store = _store(tmp_path)

    pool = WorkerPool(
        receive,
        worker_count=1,
        handler=handler,
        store=store,
        mail=mail,
        max_attempts=3,
        action_delay_ms=0,
        sleeper=FakeSleeper(),
        retry_delay_seconds=0,
    )

    with capture_logs() as logs:
        await pool.run()

    assert attempts == [1, 2, 3]
    assert AI_ERROR_LABEL in (await mail.get_message("bad-1")).label_ids
    # The requirement's audit trail: every processed email gets one entry.
    processed = [line for line in logs if line.get("event") == "email.processed"]
    assert len(processed) == 1
    assert processed[0]["message_id"] == "bad-1"
    assert processed[0]["label"] is None
    assert processed[0]["action"] == ["label_ai_error"]
    assert processed[0]["outcome"] == "error"
    assert await store.contains("bad-1") is True
    assert handled == ["good-2"]


async def test_workers_unexpected_error_in_pool_next_message_still_processed(
    tmp_path: Path,
) -> None:
    """D-43: "a failure never kills a worker" covers the pool's own code too.

    `_give_up`'s `mail.modify_labels` call fails with something other than
    `MessageNotFoundError` (e.g. a transient Gmail error surviving the
    adapter's own retries) -- `run()` must still return normally and the
    next message must still be processed.
    """
    send, receive = create_queue()
    send.send_nowait("bad-1")
    send.send_nowait("good-2")
    send.close()

    mail = FakeMail()
    mail.add_message(make_email(id="bad-1"))
    mail.fail_next(RuntimeError("gmail down"))

    handled: list[str] = []

    async def handler(message_id: str, attempt: int) -> None:
        if message_id == "bad-1":
            raise RuntimeError("boom")
        handled.append(message_id)

    pool = WorkerPool(
        receive,
        worker_count=1,
        handler=handler,
        store=_store(tmp_path),
        mail=mail,
        max_attempts=1,
        action_delay_ms=0,
        sleeper=FakeSleeper(),
        retry_delay_seconds=0,
    )

    await pool.run()

    assert handled == ["good-2"]


async def test_workers_non_retryable_error(tmp_path: Path) -> None:
    send, receive = create_queue()
    send.send_nowait("gone-1")
    send.close()

    calls = 0

    async def handler(message_id: str, attempt: int) -> None:
        nonlocal calls
        calls += 1
        raise MessageNotFoundError(message_id)

    store = _store(tmp_path)

    pool = WorkerPool(
        receive,
        worker_count=1,
        handler=handler,
        store=store,
        mail=FakeMail(),
        max_attempts=3,
        action_delay_ms=0,
        sleeper=FakeSleeper(),
        retry_delay_seconds=0,
    )

    with capture_logs() as logs:
        await pool.run()

    assert calls == 1
    assert await store.contains("gone-1") is True
    processed = [line for line in logs if line.get("event") == "email.processed"]
    assert len(processed) == 1
    assert processed[0]["message_id"] == "gone-1"
    assert processed[0]["label"] is None
    assert processed[0]["action"] == []
    assert processed[0]["outcome"] == "skipped"


async def test_workers_delay_between_messages(tmp_path: Path) -> None:
    send, receive = create_queue()
    send.send_nowait("msg-1")
    send.send_nowait("msg-2")
    send.close()

    async def handler(message_id: str, attempt: int) -> None:
        pass

    sleeper = FakeSleeper()

    pool = WorkerPool(
        receive,
        worker_count=1,
        handler=handler,
        store=_store(tmp_path),
        mail=FakeMail(),
        max_attempts=1,
        action_delay_ms=500,
        sleeper=sleeper,
        retry_delay_seconds=0,
    )

    await pool.run()

    assert sleeper.delays == [0.5, 0.5]


async def test_workers_fatal_auth_error_propagates(tmp_path: Path) -> None:
    from dt_agent.ports.errors import FatalAuthError

    send, receive = create_queue()
    send.send_nowait("msg-1")
    send.close()

    async def handler(message_id: str, attempt: int) -> None:
        raise FatalAuthError("invalid_grant")

    pool = WorkerPool(
        receive,
        worker_count=1,
        handler=handler,
        store=_store(tmp_path),
        mail=FakeMail(),
        max_attempts=3,
        action_delay_ms=0,
        sleeper=FakeSleeper(),
        retry_delay_seconds=0,
    )

    raised = False
    try:
        await pool.run()
    except* FatalAuthError:
        raised = True
    assert raised, "expected FatalAuthError to propagate (D-43: the app exits)"


async def test_workers_release_inflight_after_processing(tmp_path: Path) -> None:
    """After a message's handling ends, on every path, its id leaves `in_flight` (task 35)."""
    send, receive = create_queue()
    send.send_nowait("ok-1")
    send.send_nowait("bad-1")
    send.send_nowait("gone-1")
    send.send_nowait("recorded-1")
    send.close()

    store = _store(tmp_path)
    await store.record("recorded-1", "sender@example.com", "Subject", NOW)

    mail = FakeMail()
    mail.add_message(make_email(id="bad-1"))

    in_flight = InFlight()
    for message_id in ("ok-1", "bad-1", "gone-1", "recorded-1"):
        in_flight.add(message_id)

    async def handler(message_id: str, attempt: int) -> None:
        if message_id == "bad-1":
            raise RuntimeError("boom")
        if message_id == "gone-1":
            raise MessageNotFoundError(message_id)

    pool = WorkerPool(
        receive,
        worker_count=1,
        handler=handler,
        store=store,
        mail=mail,
        max_attempts=1,  # bad-1 gives up on its first attempt
        action_delay_ms=0,
        sleeper=FakeSleeper(),
        retry_delay_seconds=0,
        in_flight=in_flight,
    )

    await pool.run()

    for message_id in ("ok-1", "bad-1", "gone-1", "recorded-1"):
        assert message_id not in in_flight


async def test_workers_first_worker_id_offsets_logged_ids(tmp_path: Path) -> None:
    # One pool per worker (D-30, app.py): each pool's worker must log its own id.
    import structlog

    send, receive = create_queue()
    send.send_nowait("msg-1")
    send.close()

    seen: list[str] = []

    async def handler(message_id: str, attempt: int) -> None:
        seen.append(structlog.contextvars.get_contextvars()["worker_id"])

    pool = WorkerPool(
        receive,
        worker_count=1,
        first_worker_id=3,
        handler=handler,
        store=_store(tmp_path),
        mail=FakeMail(),
        max_attempts=1,
        action_delay_ms=0,
        sleeper=FakeSleeper(),
        retry_delay_seconds=0,
    )

    await pool.run()

    assert seen == ["3"]

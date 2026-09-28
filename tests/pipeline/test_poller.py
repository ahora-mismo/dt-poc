"""Tests for the poller (task 21, D-7, D-17, D-19, D-21, D-28, D-31).

Real `SqliteStore` in a temporary directory, `FakeMail` and `FakeSleeper`
per the testing rules; nothing here actually sleeps.

Test description format: test_<unit>_<condition>_<expected>.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from structlog.testing import capture_logs

from dt_agent.adapters.sqlite_store import SqliteStore, open_store
from dt_agent.domain.clock import FixedClock
from dt_agent.domain.email import Email
from dt_agent.pipeline.inflight import InFlight
from dt_agent.pipeline.poller import Poller
from dt_agent.ports.errors import CursorExpiredError, FatalAuthError, MessageNotFoundError
from dt_agent.ports.store import MessageStore
from tests.fakes.emails import make_email
from tests.fakes.mail import FakeMail
from tests.fakes.sleep import FakeSleeper

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> SqliteStore:
    store, report = open_store(tmp_path / "agent.db", FixedClock(NOW))
    assert report is None
    return store


def _released_send(sent: list[str], in_flight: InFlight) -> Callable[[str], None]:
    """A `send` that discards from `in_flight` right away, as if a worker finished instantly.

    For tests exercising dedup logic other than the in-flight set itself
    (first-list, cursor-restart), which must still see the same id
    resendable once nothing is holding it in flight.
    """

    def send(message_id: str) -> None:
        sent.append(message_id)
        in_flight.discard(message_id)

    return send


def _poller(
    mail: FakeMail,
    store: SqliteStore,
    sent: list[str],
    *,
    backfill_messages: int = 10,
    retention_days: int = 7,
    in_flight: InFlight | None = None,
) -> Poller:
    return Poller(
        mail,
        store,
        sent.append,
        backfill_messages=backfill_messages,
        retention_days=retention_days,
        poll_interval_seconds=15,
        clock=FixedClock(NOW),
        sleeper=FakeSleeper(),
        jitter=lambda: 0.0,
        in_flight=in_flight,
    )


async def test_poller_first_iteration_backfill(tmp_path: Path) -> None:
    mail = FakeMail()
    mail.add_message(make_email(id="msg-old", received_at=NOW - timedelta(days=30)))
    mail.add_message(make_email(id="msg-stored", received_at=NOW - timedelta(days=1)))
    mail.add_message(make_email(id="msg-new", received_at=NOW - timedelta(hours=1)))

    store = _store(tmp_path)
    await store.record("msg-stored", "sender@example.com", "Subject", NOW - timedelta(days=1))

    sent: list[str] = []
    poller = _poller(mail, store, sent)

    await poller.first_iteration()

    # msg-old is older than retention_days; msg-stored is already recorded;
    # only msg-new is new and within retention.
    assert sent == ["msg-new"]


class _CountingMail(FakeMail):
    """Local test double (D-52): counts `list_inbox_message_ids` calls.

    `backfill_messages=0` means "live messages only" (D-7): `first_iteration`
    must skip the INBOX listing entirely, not call it with `max_results=0`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.list_inbox_calls = 0

    async def list_inbox_message_ids(self, max_results: int) -> list[str]:
        self.list_inbox_calls += 1
        return await super().list_inbox_message_ids(max_results)


async def test_poller_backfill_zero_takes_cursor_lists_nothing(tmp_path: Path) -> None:
    mail = _CountingMail()
    mail.add_message(make_email(id="msg-1", received_at=NOW - timedelta(hours=1)))
    mail.add_message(make_email(id="msg-2", received_at=NOW - timedelta(hours=2)))
    mail.add_message(make_email(id="msg-3", received_at=NOW - timedelta(hours=3)))

    store = _store(tmp_path)
    sent: list[str] = []
    poller = _poller(mail, store, sent, backfill_messages=0)

    await poller.first_iteration()

    assert sent == []
    assert mail.list_inbox_calls == 0

    # The next tick is a live poll from the cursor taken during `first_iteration`
    # (D-7): if no cursor had been set, `tick()` would repeat `first_iteration`
    # instead (still nothing, since `ids` stays empty), not deliver `msg-new`.
    mail.add_message(make_email(id="msg-new", received_at=NOW))
    await poller.tick()

    assert sent == ["msg-new"]


async def test_poller_next_iterations_dedupe(tmp_path: Path) -> None:
    mail = FakeMail()
    mail.add_message(make_email(id="msg-first", received_at=NOW - timedelta(hours=1)))

    store = _store(tmp_path)
    sent: list[str] = []
    in_flight = InFlight()
    poller = Poller(
        mail,
        store,
        _released_send(sent, in_flight),
        backfill_messages=10,
        retention_days=7,
        poll_interval_seconds=15,
        clock=FixedClock(NOW),
        sleeper=FakeSleeper(),
        jitter=lambda: 0.0,
        in_flight=in_flight,
    )

    await poller.first_iteration()
    assert sent == ["msg-first"]

    # msg-first reappears in the history batch (e.g. a label change): it is
    # still remembered from the first list, so it must not be sent again.
    mail.add_message(make_email(id="msg-first", received_at=NOW - timedelta(hours=1)))

    # msg-store was already recorded in the store before it shows up in the
    # history batch (e.g. it is the agent's own sent message, D-22).
    await store.record("msg-store", "sender@example.com", "Subject", NOW)
    mail.add_message(make_email(id="msg-store", received_at=NOW))

    # msg-new appears twice in the same batch.
    mail.add_message(make_email(id="msg-new", received_at=NOW))
    mail.add_message(make_email(id="msg-new", received_at=NOW))

    await poller.tick()

    assert sent == ["msg-first", "msg-new"]

    # The remembered first list is dropped after this (the second)
    # iteration: msg-first is no longer skipped for that reason.
    mail.add_message(make_email(id="msg-first", received_at=NOW - timedelta(hours=1)))

    await poller.tick()

    assert sent == ["msg-first", "msg-new", "msg-first"]


async def test_poller_cursor_expired_restarts_first_iteration(tmp_path: Path) -> None:
    mail = FakeMail()
    mail.add_message(make_email(id="msg-1", received_at=NOW - timedelta(hours=1)))

    store = _store(tmp_path)
    sent: list[str] = []
    in_flight = InFlight()
    poller = Poller(
        mail,
        store,
        _released_send(sent, in_flight),
        backfill_messages=10,
        retention_days=7,
        poll_interval_seconds=15,
        clock=FixedClock(NOW),
        sleeper=FakeSleeper(),
        jitter=lambda: 0.0,
        in_flight=in_flight,
    )

    await poller.first_iteration()
    assert sent == ["msg-1"]

    mail.fail_next(CursorExpiredError("cursor gone"))

    await poller.tick()

    # The cursor expired, so the poller repeated the first iteration: the
    # same backfill message is listed and sent again (the poller itself
    # never records anything in the store).
    assert sent == ["msg-1", "msg-1"]


async def test_poller_error_logged_and_continues(tmp_path: Path) -> None:
    mail = FakeMail()
    mail.add_message(make_email(id="msg-1", received_at=NOW - timedelta(hours=1)))

    store = _store(tmp_path)
    sent: list[str] = []
    poller = _poller(mail, store, sent)

    await poller.first_iteration()
    assert sent == ["msg-1"]

    mail.fail_next(RuntimeError("boom"))

    with capture_logs() as logs:
        await poller.tick()

    assert any(entry.get("log_level") == "warning" for entry in logs)
    assert sent == ["msg-1"]  # the failed tick sent nothing new

    # The next tick runs normally.
    mail.add_message(make_email(id="msg-2", received_at=NOW))

    await poller.tick()

    assert sent == ["msg-1", "msg-2"]


async def test_poller_fatal_auth_error_propagates(tmp_path: Path) -> None:
    mail = FakeMail()
    mail.fail_next(FatalAuthError("invalid_grant"))
    poller = _poller(mail, _store(tmp_path), [])

    try:
        await poller.tick()
    except FatalAuthError:
        return
    raise AssertionError("expected FatalAuthError to propagate (D-43: the app exits)")


class _BrokenGetMessageMail(FakeMail):
    """Local test double (D-52): `get_message` raises for chosen ids, else delegates.

    Models a backlog id that's listed but gone (or otherwise broken) by the
    time the poller fetches it -- `FakeMail.fail_next` only fails the very
    next call, which can't target one id inside a multi-id loop.
    """

    def __init__(self, *, broken: dict[str, Exception]) -> None:
        super().__init__()
        self._broken = broken

    async def get_message(self, message_id: str) -> Email:
        if message_id in self._broken:
            raise self._broken[message_id]
        return await super().get_message(message_id)


class _FailingStore:
    """Local test double (D-52): wraps a real `MessageStore`; `contains` can fail once.

    `fail_for` is a plain attribute the test flips off to simulate the store
    recovering before the next tick.
    """

    def __init__(self, store: MessageStore, *, fail_for: str | None) -> None:
        self._store = store
        self.fail_for = fail_for

    async def contains(self, message_id: str) -> bool:
        if message_id == self.fail_for:
            raise RuntimeError("store unavailable")
        return await self._store.contains(message_id)

    async def record(
        self, message_id: str, sender: str, subject: str, message_date: datetime
    ) -> None:
        await self._store.record(message_id, sender, subject, message_date)

    async def purge_older_than(self, days: int) -> int:
        return await self._store.purge_older_than(days)

    async def close(self) -> None:
        await self._store.close()


async def test_poller_backfill_skips_missing_message(tmp_path: Path) -> None:
    mail = _BrokenGetMessageMail(broken={"msg-2": MessageNotFoundError("msg-2")})
    mail.add_message(make_email(id="msg-1", received_at=NOW - timedelta(hours=1)))
    mail.add_message(make_email(id="msg-2", received_at=NOW - timedelta(hours=2)))
    mail.add_message(make_email(id="msg-3", received_at=NOW - timedelta(hours=3)))

    store = _store(tmp_path)
    sent: list[str] = []
    poller = _poller(mail, store, sent)

    with capture_logs() as logs:
        await poller.first_iteration()

    # msg-2 is gone by the time it's fetched (e.g. deleted); msg-1 and
    # msg-3 are still queued -- one missing message never aborts the batch.
    assert sent == ["msg-1", "msg-3"]
    assert any(
        entry.get("log_level") == "info" and entry.get("message_id") == "msg-2" for entry in logs
    )

    # The cursor was set from the successful backfill: the next tick polls
    # history instead of repeating the whole backfill.
    mail.add_message(make_email(id="msg-4", received_at=NOW))
    await poller.tick()

    assert sent == ["msg-1", "msg-3", "msg-4"]


async def test_poller_backfill_skips_failing_message(tmp_path: Path) -> None:
    mail = _BrokenGetMessageMail(broken={"msg-2": RuntimeError("boom")})
    mail.add_message(make_email(id="msg-1", received_at=NOW - timedelta(hours=1)))
    mail.add_message(make_email(id="msg-2", received_at=NOW - timedelta(hours=2)))
    mail.add_message(make_email(id="msg-3", received_at=NOW - timedelta(hours=3)))

    store = _store(tmp_path)
    sent: list[str] = []
    poller = _poller(mail, store, sent)

    with capture_logs() as logs:
        await poller.first_iteration()

    assert sent == ["msg-1", "msg-3"]
    assert any(
        entry.get("log_level") == "warning"
        and entry.get("message_id") == "msg-2"
        and "boom" in str(entry.get("reason", ""))
        for entry in logs
    )


async def test_poller_backfill_fatal_auth_propagates(tmp_path: Path) -> None:
    mail = _BrokenGetMessageMail(broken={"msg-1": FatalAuthError("invalid_grant")})
    mail.add_message(make_email(id="msg-1", received_at=NOW - timedelta(hours=1)))

    poller = _poller(mail, _store(tmp_path), [])

    try:
        await poller.first_iteration()
    except FatalAuthError:
        return
    raise AssertionError("expected FatalAuthError to propagate (D-43: the app exits)")


async def test_poller_cursor_not_advanced_on_failed_batch(tmp_path: Path) -> None:
    mail = FakeMail()
    real_store = _store(tmp_path)
    failing_store = _FailingStore(real_store, fail_for=None)
    sent: list[str] = []
    poller = Poller(
        mail,
        failing_store,
        sent.append,
        backfill_messages=10,
        retention_days=7,
        poll_interval_seconds=15,
        clock=FixedClock(NOW),
        sleeper=FakeSleeper(),
        jitter=lambda: 0.0,
    )

    await poller.first_iteration()
    assert sent == []

    mail.add_message(make_email(id="msg-a", received_at=NOW))
    mail.add_message(make_email(id="msg-b", received_at=NOW))
    mail.add_message(make_email(id="msg-c", received_at=NOW))
    failing_store.fail_for = "msg-b"

    with capture_logs() as logs:
        await poller.tick()

    # The store failed mid-batch: nothing was queued, and the cursor must
    # not have advanced, or this batch would be lost forever.
    assert sent == []
    assert any(entry.get("log_level") == "warning" for entry in logs)

    failing_store.fail_for = None
    await poller.tick()

    # Same history page re-read from the un-advanced cursor: every id is
    # queued exactly once.
    assert sent == ["msg-a", "msg-b", "msg-c"]


async def test_poller_skips_inflight_ids(tmp_path: Path) -> None:
    mail = FakeMail()
    mail.add_message(make_email(id="msg-1", received_at=NOW - timedelta(hours=1)))

    store = _store(tmp_path)
    sent: list[str] = []
    in_flight = InFlight()
    poller = _poller(mail, store, sent, in_flight=in_flight)

    await poller.first_iteration()

    assert sent == ["msg-1"]
    # Nothing has processed it yet (no worker discarded it): still in flight.
    assert "msg-1" in in_flight

    mail.fail_next(CursorExpiredError("cursor gone"))
    await poller.tick()

    # The cursor expired, restarting the first iteration; msg-1 is still
    # in flight from before, so the re-backfill must not queue it again.
    assert sent == ["msg-1"]

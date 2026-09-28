"""SQLite `MessageStore` adapter (D-6, D-26, D-27, D-28, D-54; ADR-3).

One table of processed messages (message id, sender, subject, UTC message
date); the schema version lives in `PRAGMA user_version`, not an extra
table (D-26). The database runs in WAL mode.

`open_store` is the only way to get a `SqliteStore`: it creates the file
(and its parent directory) when missing, and validates an existing file
before trusting it (D-54). "Invalid" covers: not an SQLite file / can't be
opened; `PRAGMA integrity_check` failing; the table or one of its columns
missing; `user_version` not matching `SCHEMA_VERSION` (no migrations --
a database from an older schema is treated as invalid). Validation opens
the file **read-only** (`file:...?mode=ro`) so SQLite never creates or
touches `-wal` / `-shm` companions while we're just checking. An invalid
file is renamed to `<stem>-failed-<UTC timestamp>-UTC<suffix>` (companions
renamed the same way, never deleted) using the injected `Clock`, and a
fresh database is created in its place. This module never logs or prints;
the startup banner (D-6) is built by its caller from the returned report.

Thread safety (D-30: two workers may call through their own threads): one
`sqlite3` connection per store, opened with `check_same_thread=False`, and
every access serialized by a `threading.Lock`. Blocking calls run in
`anyio.to_thread.run_sync`, same pattern as the Gmail/Calendar adapters.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from anyio import to_thread

from dt_agent.domain.clock import Clock

SCHEMA_VERSION = 1

_TABLE = "messages"
_COLUMNS = ("message_id", "sender", "subject", "message_date")

_CREATE_TABLE_SQL = (
    f"CREATE TABLE {_TABLE} ("
    "message_id TEXT PRIMARY KEY, "
    "sender TEXT NOT NULL, "
    "subject TEXT NOT NULL, "
    "message_date REAL NOT NULL"
    ")"
)


@dataclass(frozen=True)
class InvalidDbReport:
    """What happened when an existing database file failed validation (D-54)."""

    old_path: Path
    renamed_to: tuple[Path, ...]
    reason: str


class SqliteStore:
    """The single table of processed messages (D-26), over a real SQLite file."""

    def __init__(self, path: Path, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")

    async def contains(self, message_id: str) -> bool:
        return await to_thread.run_sync(self._contains_sync, message_id)

    def _contains_sync(self, message_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                f"SELECT 1 FROM {_TABLE} WHERE message_id = ?", (message_id,)
            ).fetchone()
            return row is not None

    async def record(
        self, message_id: str, sender: str, subject: str, message_date: datetime
    ) -> None:
        if message_date.tzinfo is None:
            raise ValueError("record() requires an aware datetime")
        await to_thread.run_sync(self._record_sync, message_id, sender, subject, message_date)

    def _record_sync(
        self, message_id: str, sender: str, subject: str, message_date: datetime
    ) -> None:
        epoch = message_date.astimezone(UTC).timestamp()
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {_TABLE} (message_id, sender, subject, message_date) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET "
                "sender = excluded.sender, "
                "subject = excluded.subject, "
                "message_date = excluded.message_date",
                (message_id, sender, subject, epoch),
            )
            self._conn.commit()

    async def purge_older_than(self, days: int) -> int:
        return await to_thread.run_sync(self._purge_older_than_sync, days)

    def _purge_older_than_sync(self, days: int) -> int:
        cutoff = (self._clock.now() - timedelta(days=days)).timestamp()
        with self._lock:
            cursor = self._conn.execute(f"DELETE FROM {_TABLE} WHERE message_date < ?", (cutoff,))
            self._conn.commit()
            return cursor.rowcount

    async def close(self) -> None:
        """Close the connection (task 35), under the same lock as every other access."""
        await to_thread.run_sync(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            self._conn.close()


def _create_fresh_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _validate(path: Path) -> str | None:
    """Return None if `path` holds a valid database, else the invalidity reason (D-54)."""
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            (integrity,) = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity != "ok":
                return f"integrity check failed: {integrity}"

            (user_version,) = conn.execute("PRAGMA user_version").fetchone()
            if user_version != SCHEMA_VERSION:
                return f"schema version {user_version} (expected {SCHEMA_VERSION})"

            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
            if not columns:
                return f"table {_TABLE!r} is missing"
            missing = set(_COLUMNS) - columns
            if missing:
                return f"column(s) missing from {_TABLE!r}: {', '.join(sorted(missing))}"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"not a valid SQLite database: {exc}"
    return None


def _quarantine(path: Path, clock: Clock) -> tuple[Path, ...]:
    """Rename `path` and any `-wal` / `-shm` companions out of the way (D-54)."""
    timestamp = clock.now().astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    failed_path = path.with_name(f"{path.stem}-failed-{timestamp}-UTC{path.suffix}")
    path.rename(failed_path)
    renamed = [failed_path]
    for companion_suffix in ("-wal", "-shm"):
        companion = path.with_name(path.name + companion_suffix)
        if companion.exists():
            renamed_companion = failed_path.with_name(failed_path.name + companion_suffix)
            companion.rename(renamed_companion)
            renamed.append(renamed_companion)
    return tuple(renamed)


def open_store(path: Path, clock: Clock) -> tuple[SqliteStore, InvalidDbReport | None]:
    """Open the message store at `path`, creating or repairing it as needed (D-6, D-54)."""
    if not path.exists():
        _create_fresh_db(path)
        return SqliteStore(path, clock), None

    reason = _validate(path)
    if reason is None:
        return SqliteStore(path, clock), None

    renamed_to = _quarantine(path, clock)
    _create_fresh_db(path)
    report = InvalidDbReport(old_path=path, renamed_to=renamed_to, reason=reason)
    return SqliteStore(path, clock), report

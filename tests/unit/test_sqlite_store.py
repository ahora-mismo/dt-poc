"""Unit tests for dt_agent.adapters.sqlite_store (task 6, D-6, D-26, D-28, D-54).

Real SQLite against files in a project-local temp directory (`tmp_path`,
which `pyproject.toml` roots under `.pytest_tmp` inside the project), per
the testing rules: this port is exercised against the real adapter, not a
fake (D-52).
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dt_agent.adapters.sqlite_store import SCHEMA_VERSION, open_store
from dt_agent.domain.clock import FixedClock

_FIXED_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def test_store_new_file_created_with_schema_version(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    clock = FixedClock(_FIXED_NOW)

    _, report = open_store(db_path, clock)

    assert db_path.exists()
    assert report is None
    conn = sqlite3.connect(db_path)
    try:
        (user_version,) = conn.execute("PRAGMA user_version").fetchone()
        assert user_version == SCHEMA_VERSION
    finally:
        conn.close()


def _create_valid_db(db_path: Path, *, user_version: int, columns: str) -> None:
    """Build a db that passes every D-54 validity check except the one under test."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"CREATE TABLE messages ({columns})")
        conn.execute(f"PRAGMA user_version = {user_version}")
        conn.commit()
    finally:
        conn.close()


def test_store_wrong_schema_version_renamed(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    _create_valid_db(
        db_path,
        user_version=99,
        columns="message_id TEXT PRIMARY KEY, sender TEXT, subject TEXT, message_date REAL",
    )
    clock = FixedClock(_FIXED_NOW)

    _, report = open_store(db_path, clock)

    assert report is not None
    assert "99" in report.reason
    assert len(report.renamed_to) == 1
    assert report.renamed_to[0].exists()
    conn = sqlite3.connect(db_path)
    try:
        (user_version,) = conn.execute("PRAGMA user_version").fetchone()
        assert user_version == SCHEMA_VERSION
    finally:
        conn.close()


def test_store_missing_column_renamed(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    _create_valid_db(
        db_path,
        user_version=SCHEMA_VERSION,
        columns="message_id TEXT PRIMARY KEY, sender TEXT, message_date REAL",  # no subject
    )
    clock = FixedClock(_FIXED_NOW)

    _, report = open_store(db_path, clock)

    assert report is not None
    assert "subject" in report.reason
    assert len(report.renamed_to) == 1
    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        assert "subject" in columns
    finally:
        conn.close()


async def test_store_contains_after_insert(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    clock = FixedClock(_FIXED_NOW)
    store, _ = open_store(db_path, clock)

    assert await store.contains("msg-1") is False

    await store.record("msg-1", "Alice <alice@example.com>", "Hello", _FIXED_NOW)

    assert await store.contains("msg-1") is True
    assert await store.contains("msg-2") is False


async def test_store_purge_by_retention(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    clock = FixedClock(_FIXED_NOW)
    store, _ = open_store(db_path, clock)
    old_message = _FIXED_NOW - timedelta(days=31)
    kept_message = _FIXED_NOW - timedelta(days=29)
    await store.record("old", "Bob <bob@example.com>", "Old", old_message)
    await store.record("kept", "Carol <carol@example.com>", "Kept", kept_message)

    purged = await store.purge_older_than(30)

    assert purged == 1
    assert await store.contains("old") is False
    assert await store.contains("kept") is True


async def test_store_closed_after_close(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    clock = FixedClock(_FIXED_NOW)
    store, _ = open_store(db_path, clock)

    await store.record("msg-1", "Alice <alice@example.com>", "Hello", _FIXED_NOW)

    await store.close()

    with pytest.raises(sqlite3.ProgrammingError):
        await store.contains("msg-1")


def test_store_garbage_file_renamed(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    db_path.write_bytes(b"not a sqlite file at all")
    wal_path = tmp_path / "agent.db-wal"
    shm_path = tmp_path / "agent.db-shm"
    wal_path.write_bytes(b"fake wal")
    shm_path.write_bytes(b"fake shm")
    clock = FixedClock(_FIXED_NOW)

    _, report = open_store(db_path, clock)

    assert report is not None
    assert report.old_path == db_path
    expected_main = tmp_path / "agent-failed-20260927-120000-UTC.db"
    expected_wal = tmp_path / "agent-failed-20260927-120000-UTC.db-wal"
    expected_shm = tmp_path / "agent-failed-20260927-120000-UTC.db-shm"
    assert set(report.renamed_to) == {expected_main, expected_wal, expected_shm}
    # The companions are kept (renamed alongside the main file), never deleted (D-54).
    assert expected_main.exists()
    assert expected_wal.exists()
    assert expected_shm.exists()
    # A fresh, valid database now lives at the original path.
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        (user_version,) = conn.execute("PRAGMA user_version").fetchone()
        assert user_version == SCHEMA_VERSION
    finally:
        conn.close()

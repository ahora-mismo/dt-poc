"""Injectable clock (D-52): real time in production, fixed time in tests."""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Something that can tell the current time, aware and in UTC."""

    def now(self) -> datetime: ...


class SystemClock:
    """Wall-clock time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Always returns the same instant; for tests (D-52)."""

    def __init__(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            raise ValueError("FixedClock requires an aware datetime")
        self._dt = dt

    def now(self) -> datetime:
        return self._dt

"""In-memory `CalendarPort` fake (D-52).

Busy intervals are keyed by event id, covering both freshly created
events (`insert_event`) and imported invitations (`import_event`);
declining an invitation removes it, matching D-48 ("the event is removed
from the mailbox's schedule"). `fail_next(exc)` makes the next call raise.
"""

from datetime import datetime

from dt_agent.ports.calendar import BusyInterval, CalendarPort, InvitationEvent, NewEvent


class FakeCalendar:
    """Seed pre-existing availability with `add_busy`; inspect `inserted`/`imported`/`responses`."""

    def __init__(self) -> None:
        self._busy: dict[str, BusyInterval] = {}
        self._busy_uid: dict[str, str] = {}
        self._events_by_uid: dict[str, str] = {}
        self._next_id = 0
        self._pending_exc: Exception | None = None
        self.inserted: list[NewEvent] = []
        self.imported: list[InvitationEvent] = []
        self.responses: dict[str, bool] = {}

    def fail_next(self, exc: Exception) -> None:
        """The next call to any `CalendarPort` method raises `exc` instead of acting."""
        self._pending_exc = exc

    def add_busy(
        self,
        start: datetime,
        end: datetime,
        title: str | None = None,
        *,
        ical_uid: str | None = None,
    ) -> str:
        """Seed a pre-existing busy interval (the calendar is pre-populated, ADR-4)."""
        event_id = self._new_id("seed")
        self._busy[event_id] = BusyInterval(start=start, end=end, title=title)
        if ical_uid is not None:
            self._busy_uid[event_id] = ical_uid
        return event_id

    def _check_fail(self) -> None:
        if self._pending_exc is not None:
            exc, self._pending_exc = self._pending_exc, None
            raise exc

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    async def free_busy(
        self, start: datetime, end: datetime, *, exclude_ical_uid: str | None = None
    ) -> list[BusyInterval]:
        self._check_fail()
        return [
            b
            for event_id, b in self._busy.items()
            if b.start < end
            and b.end > start
            and (exclude_ical_uid is None or self._busy_uid.get(event_id) != exclude_ical_uid)
        ]

    async def insert_event(self, event: NewEvent) -> str:
        self._check_fail()
        event_id = self._new_id("evt")
        self._busy[event_id] = BusyInterval(start=event.start, end=event.end, title=event.title)
        self.inserted.append(event)
        return event_id

    async def find_event_by_ical_uid(self, uid: str) -> str | None:
        self._check_fail()
        return self._events_by_uid.get(uid)

    async def import_event(self, invite: InvitationEvent) -> str:
        self._check_fail()
        event_id = self._new_id("evt")
        self._busy[event_id] = BusyInterval(start=invite.start, end=invite.end, title=invite.title)
        self._events_by_uid[invite.uid] = event_id
        self.imported.append(invite)
        return event_id

    async def respond_to_event(self, event_id: str, accepted: bool) -> None:
        self._check_fail()
        self.responses[event_id] = accepted
        if not accepted:
            self._busy.pop(event_id, None)


# Structural check, not a test: see tests/fakes/mail.py's `_mail_port_check`.
_calendar_port_check: CalendarPort = FakeCalendar()

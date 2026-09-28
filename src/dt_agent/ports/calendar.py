"""Calendar port (ADR-4, D-45, D-48, D-55).

Implemented by `adapters/calendar.py` (task 16) over
`google-api-python-client`'s Calendar API, and by `tests/fakes/calendar.py`'s
`FakeCalendar` for unit tests. Every operation is scoped to the manager's
own calendar (ADR-4's "known limitation").
"""

from datetime import datetime
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict


class BusyInterval(BaseModel):
    """One busy interval on the manager's calendar (ADR-4).

    `title` comes from `events.list` (`freebusy.query` alone has no
    titles) and is shown as a conflict in the approval email (D-14, D-55).
    """

    model_config = ConfigDict(frozen=True)

    start: AwareDatetime
    end: AwareDatetime
    title: str | None = None


class NewEvent(BaseModel):
    """A meeting to create after approval (ADR-4)."""

    model_config = ConfigDict(frozen=True)

    title: str
    start: AwareDatetime
    end: AwareDatetime
    timezone: str
    attendees: tuple[str, ...] = ()
    location: str | None = None
    description: str | None = None


class InvitationEvent(BaseModel):
    """A calendar invitation received by email (`.ics`, D-48).

    Read by `core/invitations.py` (task 14) from a `text/calendar` part or
    `.ics` attachment; `uid` is the iCalendar UID used to find or import
    the event.
    """

    model_config = ConfigDict(frozen=True)

    uid: str
    start: AwareDatetime
    end: AwareDatetime
    title: str | None = None
    location: str | None = None
    organizer: str | None = None


class CalendarPort(Protocol):
    """Calendar operations the pipeline needs, on the manager's calendar only (ADR-4)."""

    async def free_busy(
        self, start: datetime, end: datetime, *, exclude_ical_uid: str | None = None
    ) -> list[BusyInterval]:
        """Busy intervals overlapping `[start, end)` (`freebusy.query` + `events.list` titles).

        `exclude_ical_uid`: a received invitation's own event (already placed in the
        calendar by Google, D-48) is not a conflict for that invitation; a busy interval
        whose only events are that one is left out.
        """
        ...

    async def insert_event(self, event: NewEvent) -> str:
        """Create `event` (`events.insert`, `sendUpdates=all`); returns the event id."""
        ...

    async def find_event_by_ical_uid(self, uid: str) -> str | None:
        """The event id already holding this iCalendar UID, if any (`events.list(iCalUID=...)`)."""
        ...

    async def import_event(self, invite: InvitationEvent) -> str:
        """Import a received invitation not yet on the calendar (`events.import`, D-48)."""
        ...

    async def respond_to_event(self, event_id: str, accepted: bool) -> None:
        """Accept or decline `event_id` as the mailbox's own attendee (`events.patch`, D-48)."""
        ...

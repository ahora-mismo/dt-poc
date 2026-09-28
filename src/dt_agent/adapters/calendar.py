"""Calendar adapter (ADR-4, D-12, D-18, D-43, D-45, D-48, D-55): `CalendarPort` over
`google-api-python-client`'s Calendar API v3.

Every operation is scoped to one calendar id (`primary` in production, ADR-4's
"known limitation": only the manager's own calendar is checked). Blocking
`googleapiclient` calls run in `anyio.to_thread.run_sync` (D-30: each worker
owns its own client instance). Every call goes through the shared Google
policy in `adapters/google_errors.py` (retries on 429 / 5xx, `FatalAuthError`
on `invalid_grant` / 401, D-43).

`respond_to_event` answers as the attendee marked `self: true`, or else as the
mailbox's own address (an imported invitation may list no attendee for us,
D-48).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from anyio import to_thread
from google.auth.credentials import Credentials
from googleapiclient.discovery import Resource, build

from dt_agent.adapters.google_errors import run_google_call
from dt_agent.ports.calendar import BusyInterval, CalendarPort, InvitationEvent, NewEvent


def _event_datetime(node: dict[str, Any]) -> datetime:
    """Parse an `events.list`/`events.get` `start`/`end` node, aware UTC."""
    if "dateTime" in node:
        return datetime.fromisoformat(node["dateTime"]).astimezone(UTC)
    day = date.fromisoformat(node["date"])
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


class NoSelfAttendeeError(LookupError):
    """No attendee marked `self: true` and no mailbox address to answer as (D-48)."""


def _self_email(attendees: list[dict[str, Any]], mailbox_address: str | None) -> str:
    """The address to answer as: the `self: true` attendee, else the mailbox address."""
    for attendee in attendees:
        if attendee.get("self"):
            email: str = attendee["email"]
            return email
    if mailbox_address:
        return mailbox_address
    raise NoSelfAttendeeError(f"no attendee marked self:true among {attendees!r}")


class CalendarAdapter:
    """`CalendarPort` over one Calendar API `Resource`, scoped to `calendar_id`."""

    def __init__(
        self,
        service: Resource,
        calendar_id: str = "primary",
        *,
        mailbox_address: str | None = None,
    ) -> None:
        self._service = service
        self._calendar_id = calendar_id
        self._mailbox_address = mailbox_address

    async def _execute(self, request: Any) -> dict[str, Any]:
        def call() -> dict[str, Any]:
            result: dict[str, Any] = request.execute()
            return result

        return await to_thread.run_sync(lambda: run_google_call(call))

    async def free_busy(
        self, start: datetime, end: datetime, *, exclude_ical_uid: str | None = None
    ) -> list[BusyInterval]:
        time_min = start.isoformat()
        time_max = end.isoformat()

        freebusy_resp = await self._execute(
            self._service.freebusy().query(
                body={
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "items": [{"id": self._calendar_id}],
                }
            )
        )
        busy_raw = freebusy_resp["calendars"][self._calendar_id]["busy"]

        events_resp = await self._execute(
            self._service.events().list(
                calendarId=self._calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
            )
        )
        events = events_resp.get("items", [])

        intervals: list[BusyInterval] = []
        for busy in busy_raw:
            busy_start = datetime.fromisoformat(busy["start"]).astimezone(UTC)
            busy_end = datetime.fromisoformat(busy["end"]).astimezone(UTC)
            overlapping = _overlapping_events(events, busy_start, busy_end)
            others = [
                item
                for item in overlapping
                if exclude_ical_uid is None or item.get("iCalUID") != exclude_ical_uid
            ]
            if overlapping and not others:
                continue  # D-48: only the invitation itself is here
            title = next((item["summary"] for item in others if item.get("summary")), None)
            intervals.append(BusyInterval(start=busy_start, end=busy_end, title=title))
        return intervals

    async def insert_event(self, event: NewEvent) -> str:
        body: dict[str, Any] = {
            "summary": event.title,
            "start": {"dateTime": event.start.isoformat(), "timeZone": event.timezone},
            "end": {"dateTime": event.end.isoformat(), "timeZone": event.timezone},
            "attendees": [{"email": email} for email in event.attendees],
        }
        if event.location is not None:
            body["location"] = event.location
        if event.description is not None:
            body["description"] = event.description

        resp = await self._execute(
            self._service.events().insert(
                calendarId=self._calendar_id, body=body, sendUpdates="all"
            )
        )
        event_id: str = resp["id"]
        return event_id

    async def find_event_by_ical_uid(self, uid: str) -> str | None:
        resp = await self._execute(
            self._service.events().list(calendarId=self._calendar_id, iCalUID=uid)
        )
        items = resp.get("items", [])
        if not items:
            return None
        event_id: str = items[0]["id"]
        return event_id

    async def import_event(self, invite: InvitationEvent) -> str:
        body: dict[str, Any] = {
            "iCalUID": invite.uid,
            "start": {"dateTime": invite.start.isoformat()},
            "end": {"dateTime": invite.end.isoformat()},
        }
        if invite.title is not None:
            body["summary"] = invite.title
        if invite.location is not None:
            body["location"] = invite.location
        if invite.organizer is not None:
            body["organizer"] = {"email": invite.organizer}

        resp = await self._execute(
            self._service.events().import_(calendarId=self._calendar_id, body=body)
        )
        event_id: str = resp["id"]
        return event_id

    async def respond_to_event(self, event_id: str, accepted: bool) -> None:
        event = await self._execute(
            self._service.events().get(calendarId=self._calendar_id, eventId=event_id)
        )
        self_email = _self_email(event.get("attendees", []), self._mailbox_address)
        response_status = "accepted" if accepted else "declined"

        await self._execute(
            self._service.events().patch(
                calendarId=self._calendar_id,
                eventId=event_id,
                body={
                    "attendees": [{"email": self_email, "responseStatus": response_status}],
                    "attendeesOmitted": True,
                },
                sendUpdates="all",
            )
        )


def _overlapping_events(
    events: list[dict[str, Any]], busy_start: datetime, busy_end: datetime
) -> list[dict[str, Any]]:
    """The `events.list` items overlapping `[busy_start, busy_end)` (D-48, D-55)."""
    return [
        item
        for item in events
        if "start" in item
        and "end" in item
        and _event_datetime(item["start"]) < busy_end
        and _event_datetime(item["end"]) > busy_start
    ]


def build_calendar_adapter(credentials: Credentials, mailbox_address: str) -> CalendarAdapter:
    """Build a `CalendarAdapter` from Google credentials (`google_auth.build_credentials`)."""
    service = build(
        "calendar", "v3", credentials=credentials, static_discovery=True, cache_discovery=False
    )
    return CalendarAdapter(service, mailbox_address=mailbox_address)


# Structural check, not a test: catches this adapter drifting from the
# port's shape (a missing method, a wrong signature) at `mypy` time.
def _calendar_port_check(service: Resource) -> CalendarPort:
    return CalendarAdapter(service)

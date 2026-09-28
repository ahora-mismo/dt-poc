"""Calendar invitation detection (D-48, D-55).

Looks through an `Email`'s `calendar_parts` (raw iCalendar text from a
`text/calendar` part or an `.ics` attachment, D-48) and classifies what
kind of calendar object it is:

- `METHOD:REQUEST` -> a new or updated invitation (D-48, D-55); the
  `Invitation` payload carries what the calendar adapter (task 16) and
  approval flow need: UID, start/end, title, location, organizer,
  sequence.
- `METHOD:CANCEL` -> logged only; no calendar action (Google removes
  the event itself, D-55).
- `METHOD:REPLY` -> logged only (this mailbox is not the organizer of
  its own invitation replies).
- No calendar part, no `METHOD`, or unparseable iCalendar text ->
  `NOT_INVITATION`, so the caller falls back to normal classification.

No I/O here: parsing only. Deciding what to do with the result (D-48
steps 1-5) is the pipeline's job.
"""

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from icalendar import Calendar, Component
from pydantic import AwareDatetime, BaseModel, ConfigDict

from dt_agent.domain.email import Email

_MAILTO_PREFIX = "mailto:"


class InvitationKind(StrEnum):
    """What kind of calendar object was found (D-48)."""

    REQUEST = "REQUEST"
    CANCEL = "CANCEL"
    REPLY = "REPLY"
    NOT_INVITATION = "NOT_INVITATION"


class Invitation(BaseModel):
    """The facts a `METHOD:REQUEST` calendar object carries (D-48)."""

    model_config = ConfigDict(frozen=True)

    uid: str
    start: AwareDatetime
    end: AwareDatetime
    title: str
    location: str
    organizer: str
    sequence: int = 0


class InvitationResult(BaseModel):
    """The outcome of `detect_invitation` (D-48)."""

    model_config = ConfigDict(frozen=True)

    kind: InvitationKind
    invitation: Invitation | None = None


def detect_invitation(email: Email) -> InvitationResult:
    """Classify `email`'s calendar parts (D-48).

    Returns the first non-`NOT_INVITATION` result found among
    `email.calendar_parts` (a `text/calendar` part and an `.ics`
    attachment are both raw iCalendar text by the time they reach
    here, D-48). If none parses to a recognised `METHOD`, the result
    is `NOT_INVITATION`.
    """
    for part in email.calendar_parts:
        result = _parse_calendar_text(part.raw_ics)
        if result.kind is not InvitationKind.NOT_INVITATION:
            return result
    return InvitationResult(kind=InvitationKind.NOT_INVITATION)


def _parse_calendar_text(raw_ics: str) -> InvitationResult:
    try:
        calendar = Calendar.from_ical(raw_ics)
    except Exception:  # any parse failure means "not an invitation"
        return InvitationResult(kind=InvitationKind.NOT_INVITATION)

    method_prop = calendar.get("method")
    if method_prop is None:
        return InvitationResult(kind=InvitationKind.NOT_INVITATION)
    method = str(method_prop).upper()

    if method == "CANCEL":
        return InvitationResult(kind=InvitationKind.CANCEL)
    if method == "REPLY":
        return InvitationResult(kind=InvitationKind.REPLY)
    if method != "REQUEST":
        return InvitationResult(kind=InvitationKind.NOT_INVITATION)

    try:
        return _build_request_result(calendar)
    except Exception:  # any missing/malformed VEVENT field means "not an invitation"
        return InvitationResult(kind=InvitationKind.NOT_INVITATION)


def _build_request_result(calendar: Calendar) -> InvitationResult:
    vevent = next(iter(calendar.walk("VEVENT")), None)
    if vevent is None:
        return InvitationResult(kind=InvitationKind.NOT_INVITATION)

    invitation = Invitation(
        uid=str(vevent.get("uid", "")),
        start=_resolve_start_or_end(vevent, "dtstart"),
        end=_resolve_end(vevent),
        title=str(vevent.get("summary", "")),
        location=str(vevent.get("location", "")),
        organizer=_organizer_email(vevent),
        sequence=int(vevent.get("sequence", 0)),
    )
    return InvitationResult(kind=InvitationKind.REQUEST, invitation=invitation)


def _resolve_end(vevent: Component) -> datetime:
    dtend = vevent.get("dtend")
    if dtend is not None:
        return _resolve_datetime_property(dtend)
    duration = vevent.get("duration")
    start = _resolve_start_or_end(vevent, "dtstart")
    if duration is not None:
        delta: timedelta = duration.dt
        return start + delta
    return start


def _resolve_start_or_end(vevent: Component, key: str) -> datetime:
    prop = vevent.get(key)
    if prop is None:
        raise ValueError(f"VEVENT is missing required property {key!r}")
    return _resolve_datetime_property(prop)


def _resolve_datetime_property(prop: Any) -> datetime:
    """Turn a `DTSTART`/`DTEND` property into an aware UTC-or-local datetime.

    All-day values (a `date`, not a `datetime`) become UTC midnight.
    Floating times (a naive `datetime`) use the property's `TZID`
    parameter if present, else UTC (D-48).
    """
    value = prop.dt
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value
        tzid = prop.params.get("TZID")
        if tzid:
            return value.replace(tzinfo=ZoneInfo(str(tzid)))
        return value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    raise ValueError(f"unsupported date/time value: {value!r}")


def _organizer_email(vevent: Component) -> str:
    organizer = vevent.get("organizer")
    if organizer is None:
        return ""
    address = str(organizer)
    if address.lower().startswith(_MAILTO_PREFIX):
        return address[len(_MAILTO_PREFIX) :]
    return address

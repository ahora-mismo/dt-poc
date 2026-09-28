"""`CalendarAdapter` tests over recorded Calendar API v3 responses (D-52).

`HttpMockSequence` plays back the fixtures in `tests/fixtures/calendar/`
and records every outgoing request in `.request_sequence`, so assertions
are made on decoded request bodies/URLs rather than on a mocked call
chain (testing rules).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from dt_agent.adapters.calendar import CalendarAdapter
from dt_agent.ports.calendar import InvitationEvent, NewEvent

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "calendar"


def _fixture_bytes(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _response(name: str, status: str = "200") -> tuple[dict[str, str], bytes]:
    return ({"status": status}, _fixture_bytes(name))


def _build_service(
    responses: list[tuple[dict[str, str], bytes]],
) -> tuple[HttpMockSequence, object]:
    http = HttpMockSequence(responses)
    service = build("calendar", "v3", http=http, static_discovery=True)
    return http, service


def _query(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(uri).query)


async def test_calendar_freebusy_parsed() -> None:
    http, service = _build_service(
        [
            _response("freebusy_busy.json"),
            _response("events_list_empty.json"),
        ]
    )
    adapter = CalendarAdapter(service, calendar_id="primary")

    intervals = await adapter.free_busy(
        datetime(2026, 9, 29, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)
    )

    assert len(intervals) == 1
    interval = intervals[0]
    assert interval.start == datetime(2026, 9, 29, 11, 0, tzinfo=UTC)
    assert interval.end == datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert interval.start.utcoffset() == interval.end.utcoffset() == UTC.utcoffset(None)
    assert interval.title is None

    freebusy_uri, freebusy_method, freebusy_body, _ = http.request_sequence[0]
    assert freebusy_method == "POST"
    assert "/freeBusy" in freebusy_uri
    freebusy_request = json.loads(freebusy_body)
    assert freebusy_request["items"] == [{"id": "primary"}]

    list_uri, list_method, _, _ = http.request_sequence[1]
    assert list_method == "GET"
    assert "/calendars/primary/events" in list_uri
    assert _query(list_uri)["singleEvents"] == ["true"]


async def test_calendar_conflict_titles() -> None:
    _http, service = _build_service(
        [
            _response("freebusy_busy.json"),
            _response("events_list_conflict.json"),
        ]
    )
    adapter = CalendarAdapter(service, calendar_id="primary")

    intervals = await adapter.free_busy(
        datetime(2026, 9, 29, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)
    )

    assert len(intervals) == 1
    assert intervals[0].title == "Board meeting"


async def test_calendar_freebusy_ignores_the_invitations_own_event() -> None:
    """D-48: a received invitation is already in the calendar (awaiting a response); when
    checking the invitation's own time, that event is not a conflict."""
    _http, service = _build_service(
        [_response("freebusy_busy.json"), _response("events_list_invitation_only.json")]
    )
    adapter = CalendarAdapter(service, calendar_id="primary")

    intervals = await adapter.free_busy(
        datetime(2026, 9, 29, tzinfo=UTC),
        datetime(2026, 9, 30, tzinfo=UTC),
        exclude_ical_uid="kickoff-uid@google.com",
    )

    assert intervals == []


async def test_calendar_freebusy_keeps_other_events_when_excluding_invitation() -> None:
    _http, service = _build_service(
        [_response("freebusy_busy.json"), _response("events_list_invitation_and_meeting.json")]
    )
    adapter = CalendarAdapter(service, calendar_id="primary")

    intervals = await adapter.free_busy(
        datetime(2026, 9, 29, tzinfo=UTC),
        datetime(2026, 9, 30, tzinfo=UTC),
        exclude_ical_uid="kickoff-uid@google.com",
    )

    assert len(intervals) == 1
    assert intervals[0].title == "Board meeting"


async def test_calendar_insert_body() -> None:
    http, service = _build_service([_response("insert_response.json")])
    adapter = CalendarAdapter(service, calendar_id="primary")

    event = NewEvent(
        title="Design sync",
        start=datetime(2026, 9, 29, 14, 0, tzinfo=UTC),
        end=datetime(2026, 9, 29, 14, 30, tzinfo=UTC),
        timezone="Europe/Bucharest",
        attendees=("a@example.com", "b@example.com"),
        location="Room 1",
        description="Weekly sync",
    )

    event_id = await adapter.insert_event(event)

    assert event_id == "evt-123"
    uri, method, body, _ = http.request_sequence[0]
    assert method == "POST"
    assert "/calendars/primary/events" in uri
    assert _query(uri)["sendUpdates"] == ["all"]
    request_body = json.loads(body)
    assert request_body["start"]["timeZone"] == "Europe/Bucharest"
    assert request_body["end"]["timeZone"] == "Europe/Bucharest"
    assert request_body["attendees"] == [
        {"email": "a@example.com"},
        {"email": "b@example.com"},
    ]
    assert request_body["location"] == "Room 1"
    assert request_body["description"] == "Weekly sync"


async def test_calendar_invitation_found_or_imported_then_answered() -> None:
    # Hit: events.list(iCalUID=...) finds the event -> respond patches it directly.
    hit_http, hit_service = _build_service(
        [
            _response("list_uid_hit.json"),
            _response("get_with_self_attendee.json"),
            _response("patch_response.json"),
        ]
    )
    hit_adapter = CalendarAdapter(hit_service, calendar_id="primary")

    found_id = await hit_adapter.find_event_by_ical_uid("uid-1")
    assert found_id == "evt-existing"

    list_uri, list_method, _, _ = hit_http.request_sequence[0]
    assert list_method == "GET"
    assert _query(list_uri)["iCalUID"] == ["uid-1"]

    await hit_adapter.respond_to_event(found_id, accepted=True)

    get_uri, get_method, _, _ = hit_http.request_sequence[1]
    assert get_method == "GET"
    assert "/events/evt-existing" in get_uri

    patch_uri, patch_method, patch_body, _ = hit_http.request_sequence[2]
    assert patch_method == "PATCH"
    assert _query(patch_uri)["sendUpdates"] == ["all"]
    patch_request = json.loads(patch_body)
    assert patch_request["attendeesOmitted"] is True
    assert patch_request["attendees"] == [
        {"email": "mailbox@example.com", "responseStatus": "accepted"}
    ]

    # Miss: events.list(iCalUID=...) finds nothing -> import, then respond patches the import.
    miss_http, miss_service = _build_service(
        [
            _response("list_uid_miss.json"),
            _response("import_response.json"),
            _response("get_with_self_attendee.json"),
            _response("patch_response.json"),
        ]
    )
    miss_adapter = CalendarAdapter(miss_service, calendar_id="primary")

    missing_id = await miss_adapter.find_event_by_ical_uid("uid-2")
    assert missing_id is None

    invite = InvitationEvent(
        uid="uid-2",
        start=datetime(2026, 9, 29, 14, 0, tzinfo=UTC),
        end=datetime(2026, 9, 29, 15, 0, tzinfo=UTC),
        title="Partner sync",
        location="Zoom",
        organizer="organizer@example.com",
    )
    imported_id = await miss_adapter.import_event(invite)
    assert imported_id == "evt-imported"

    import_uri, import_method, import_body, _ = miss_http.request_sequence[1]
    assert import_method == "POST"
    assert "/events/import" in import_uri
    import_request = json.loads(import_body)
    assert import_request["iCalUID"] == "uid-2"
    assert import_request["summary"] == "Partner sync"
    assert import_request["location"] == "Zoom"
    assert import_request["organizer"] == {"email": "organizer@example.com"}

    await miss_adapter.respond_to_event(imported_id, accepted=False)

    patch_uri, patch_method, patch_body, _ = miss_http.request_sequence[3]
    assert patch_method == "PATCH"
    patch_request = json.loads(patch_body)
    assert patch_request["attendees"] == [
        {"email": "mailbox@example.com", "responseStatus": "declined"}
    ]
    assert patch_request["attendeesOmitted"] is True


async def test_calendar_respond_falls_back_to_mailbox_address() -> None:
    # An imported invitation (D-48) may carry no attendee marked self:true;
    # the mailbox's own address is then used for the response.
    http, service = _build_service(
        [
            _response("get_imported_no_attendees.json"),
            _response("patch_response.json"),
        ]
    )
    adapter = CalendarAdapter(service, calendar_id="primary", mailbox_address="mailbox@example.com")

    await adapter.respond_to_event("evt-imported", accepted=True)

    _, patch_method, patch_body, _ = http.request_sequence[1]
    assert patch_method == "PATCH"
    assert json.loads(patch_body)["attendees"] == [
        {"email": "mailbox@example.com", "responseStatus": "accepted"}
    ]


async def test_calendar_invalid_grant_is_fatal() -> None:
    from dt_agent.adapters.google_errors import FatalAuthError

    _, service = _build_service([({"status": "401"}, b'{"error": "invalid_grant"}')])
    adapter = CalendarAdapter(service, calendar_id="primary")

    try:
        await adapter.find_event_by_ical_uid("uid-1")
    except FatalAuthError:
        return
    raise AssertionError("expected FatalAuthError")

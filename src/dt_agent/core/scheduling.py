"""Deterministic meeting scheduling (ADR-4, D-13, D-14, D-45, D-55, D-56).

Pure, deterministic and offline: given already-resolved candidate times (or
a window, or nothing) plus the manager's busy intervals, this module decides
which numbered options go into the approval email (D-13). It never talks to
Gmail/Calendar and never looks at date *expressions* (D-64) — resolving a
phrase like "next Tuesday" into an exact instant is `core/dates.py`'s job;
this module only takes plain, already-resolved start/end times.

**Inputs the caller must have already resolved:**
- one or more exact requested times -> :class:`RequestedSlot` (start + the
  original phrase, for display);
- one or more open windows ("sometime next week") -> :class:`RequestedWindow`
  (start/end + phrase); an all-day/date-only request is just a window
  spanning that day's business hours. A multi-day window is several
  `RequestedWindow`s, one per business day it spans (`core/dates.py`
  task 39, F1), all with the requester's phrase — consecutive pieces with
  the same phrase count as one requested time;
- invalid requests (D-66: a local time that doesn't exist or is ambiguous
  at a DST change) -> :class:`InvalidRequest`, passed straight through into
  :class:`SchedulingResult.invalid` so the approval email can say so; they
  never influence scheduling.
- busy intervals (free/busy on the manager's calendar only, ADR-4) as plain
  :class:`BusyInterval` values — no port types.

**Rounding and rules (ADR-4):**
- every proposed time is rounded *up* to the next 10 minutes;
- slots the agent picks itself start only at :00/:30, earliest first, at
  least ``min_lead_minutes`` from `now`;
- no buffer between meetings (touching endpoints count as free);
- business hours are Monday-Friday, ``business_hours_start``-
  ``business_hours_end`` local time, in ``timezone``; a requested time
  outside them is treated exactly like a busy one (D-14) but marked
  differently.

**Options (ADR-4, D-13, D-14, D-45):** every future requested time counts,
exact times and windows together, in time order, and gives one option: an
exact time as itself, a window by its first free business-hours slot.
- **every requested time available** -> exactly those options, no agent
  alternatives.
- **the only requested time is an unavailable exact time** (busy or
  outside business hours) -> up to ``max_options - 1`` free alternatives
  near it, then the requested time last, marked (so option 1 and a plain
  APPROVE always land on a free slot).
- **several requested times, some unavailable exact times** -> the free
  ones first, then the nearest free slots up to ``max_options`` free
  options, then *every* unavailable one, each marked (this can push the
  total past ``max_options`` — D-14's double-booking warning is never
  dropped for being over the cap).
- **a requested window with no free slot** -> it gets no option; when no
  requested time gives any option, the nearest free slots *after the last
  window's end* within the horizon, marked ``OUTSIDE_REQUESTED_INTERVAL``
  (D-45).
- **no time, or every requested time in the past** -> free slots within
  the horizon from `now`.

**Horizon anchor:** the horizon (``horizon_business_days``) limits how far
ahead the agent searches for *its own* free-slot suggestions. It is
anchored at whichever point that search starts from, not always `now`:
from `now` when there was no time given at all; from the single requested
time when hunting alternatives for a busy/outside-hours exact request; from
the *last* window's end (task 39) when hunting slots after every requested
window is exhausted. Anchoring
a "nearest after X" search at `now` instead of at X would make a horizon
shorter than the gap between `now` and X return nothing, even though the
plan's intent ("the nearest free slots after it") is clearly to keep
searching from there.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from dt_agent.domain.meeting import SlotMark, SlotOption

# D-56: calendar limits for the POC. No env var backs these (D-65 lists
# none), so they are fixed constants rather than configuration.
MAX_ATTENDEES = 20


class SchedulingFlag(StrEnum):
    """A calendar limit exceeded by the request (D-45, D-56); never drops it."""

    TOO_MANY_ATTENDEES = "too_many_attendees"
    EVENT_LONGER_THAN_BUSINESS_DAY = "event_longer_than_business_day"


@dataclass(frozen=True, slots=True)
class SchedulingParams:
    """Calendar policy for one scheduling decision (ADR-4)."""

    timezone: str
    business_hours_start: int
    business_hours_end: int
    default_meeting_minutes: int
    horizon_business_days: int
    min_lead_minutes: int
    max_options: int


@dataclass(frozen=True, slots=True)
class BusyInterval:
    """One busy interval on the manager's calendar (ADR-4: their calendar only)."""

    start: datetime
    end: datetime
    title: str | None = None


@dataclass(frozen=True, slots=True)
class RequestedSlot:
    """One exact requested time, already resolved to a start instant."""

    start: datetime
    phrase: str = ""


@dataclass(frozen=True, slots=True)
class RequestedWindow:
    """An open window ("sometime next week"), already resolved to a range."""

    start: datetime
    end: datetime
    phrase: str = ""


@dataclass(frozen=True, slots=True)
class InvalidRequest:
    """A requested time reported invalid upstream (D-66); never scheduled."""

    phrase: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SchedulingRequest:
    """What was asked for for one meeting (ADR-4).

    ``exact_slots`` and ``windows`` are scheduled together, one option per
    requested time (D-13). Both empty (or every requested time in the past)
    means "no time given" (ADR-4). ``windows`` may hold several entries:
    several distinct requested windows, or one multi-day window already split
    into one entry per business day by `core/dates.py` (same phrase, counted
    as one requested time).
    """

    exact_slots: tuple[RequestedSlot, ...] = ()
    windows: tuple[RequestedWindow, ...] = ()
    duration_minutes: int | None = None
    attendee_count: int = 1
    invalid: tuple[InvalidRequest, ...] = ()


@dataclass(frozen=True, slots=True)
class SchedulingResult:
    """What goes into the approval email (D-13, D-14, D-45)."""

    options: tuple[SlotOption, ...]
    flags: frozenset[SchedulingFlag] = frozenset()
    invalid: tuple[InvalidRequest, ...] = ()


def schedule(
    request: SchedulingRequest,
    busy: Sequence[BusyInterval],
    params: SchedulingParams,
    now: datetime,
) -> SchedulingResult:
    """Decide the numbered slot options for one meeting request (ADR-4)."""
    zone = ZoneInfo(params.timezone)
    now_local = now.astimezone(zone)
    duration = timedelta(minutes=request.duration_minutes or params.default_meeting_minutes)

    flags: set[SchedulingFlag] = set()
    if request.attendee_count > MAX_ATTENDEES:
        flags.add(SchedulingFlag.TOO_MANY_ATTENDEES)
    business_day_minutes = (params.business_hours_end - params.business_hours_start) * 60
    if duration.total_seconds() / 60 > business_day_minutes:
        flags.add(SchedulingFlag.EVENT_LONGER_THAN_BUSINESS_DAY)

    lead_floor = _round_up_to_half_hour(now_local + timedelta(minutes=params.min_lead_minutes))

    requested = _requested_times(request, now)
    if requested:
        options = _schedule_requested(requested, busy, params, duration, lead_floor)
    else:
        horizon = _horizon_end(now_local, params)
        picked = _agent_candidates(params, busy, duration, lead_floor, horizon, params.max_options)
        options = [
            _option(i + 1, start, end, "", SlotMark.FREE) for i, (start, end) in enumerate(picked)
        ]

    return SchedulingResult(
        options=tuple(options), flags=frozenset(flags), invalid=tuple(request.invalid)
    )


@dataclass(frozen=True, slots=True)
class _Requested:
    """One requested time: an exact slot, or a window (possibly split into per-day pieces
    by `core/dates.py`, all sharing the requester's phrase)."""

    phrase: str
    start: datetime
    exact: RequestedSlot | None = None
    windows: tuple[RequestedWindow, ...] = ()


def _requested_times(request: SchedulingRequest, now: datetime) -> list[_Requested]:
    """Every future requested time, exact times and windows together, in time order.
    Pieces of one window (same phrase, consecutive) form one requested time."""
    requested = [
        _Requested(phrase=slot.phrase, start=slot.start, exact=slot)
        for slot in request.exact_slots
        if slot.start >= now
    ]
    groups: list[list[RequestedWindow]] = []
    for window in request.windows:
        if window.end <= now:
            continue
        if groups and groups[-1][0].phrase == window.phrase:
            groups[-1].append(window)
        else:
            groups.append([window])
    requested.extend(
        _Requested(phrase=group[0].phrase, start=group[0].start, windows=tuple(group))
        for group in groups
    )
    return sorted(requested, key=lambda item: item.start)


def _schedule_requested(
    requested: Sequence[_Requested],
    busy: Sequence[BusyInterval],
    params: SchedulingParams,
    duration: timedelta,
    lead_floor: datetime,
) -> list[SlotOption]:
    """One option per requested time (D-13); agent alternatives only when a requested
    time is unavailable, placed before the unavailable ones (D-14, D-45)."""
    zone = ZoneInfo(params.timezone)
    offered: list[tuple[datetime, datetime, str]] = []
    unavailable: list[tuple[datetime, datetime, str, SlotMark, tuple[str, ...]]] = []
    empty_windows: list[_Requested] = []

    for item in requested:
        taken = [*busy, *(BusyInterval(start, end) for start, end, _ in offered)]
        if item.exact is not None:
            start = _round_up_to_10_minutes(item.exact.start.astimezone(zone))
            end = start + duration
            mark, conflicts = _classify_exact(start, end, taken, params)
            if mark == SlotMark.FREE:
                offered.append((start, end, item.phrase))
            else:
                unavailable.append((start, end, item.phrase, mark, conflicts))
            continue
        slot = _first_free_in_windows(item.windows, taken, params, duration, lead_floor)
        if slot is None:
            empty_windows.append(item)
        else:
            offered.append((*slot, item.phrase))

    options = [
        _option(i + 1, start, end, phrase, SlotMark.FREE)
        for i, (start, end, phrase) in enumerate(offered)
    ]
    if not unavailable and (options or not empty_windows):
        return options

    taken = [*busy, *(BusyInterval(start, end) for start, end, _ in offered)]
    if empty_windows and not options and not unavailable:
        # D-45: nothing free inside any requested window -> the nearest free slots after
        # the last one.
        last = empty_windows[-1]
        last_end = last.windows[-1].end.astimezone(zone)
        after = _agent_candidates(
            params,
            taken,
            duration,
            max(lead_floor, last_end),
            _horizon_end(last_end, params),
            max(1, params.max_options - len(options)),
        )
        for start, end in after:
            options.append(
                _option(
                    len(options) + 1, start, end, last.phrase, SlotMark.OUTSIDE_REQUESTED_INTERVAL
                )
            )
    elif len(requested) == 1:
        # D-14: the only requested time is unavailable -> free alternatives near it, so
        # that option 1 (a plain APPROVE) is free; the requested time comes last.
        anchor = unavailable[0][0]
        alternatives = _agent_candidates(
            params,
            taken,
            duration,
            max(lead_floor, anchor),
            _horizon_end(anchor, params),
            max(0, params.max_options - 1),
        )
        for start, end in alternatives:
            options.append(_option(len(options) + 1, start, end, "", SlotMark.FREE))
    else:
        # D-14: several requested times, some unavailable -> free options filled up to
        # `max_options` with the nearest free slots; every unavailable one comes last.
        alternatives = _agent_candidates(
            params,
            taken,
            duration,
            lead_floor,
            _horizon_end(lead_floor, params),
            max(0, params.max_options - len(options)),
        )
        for start, end in alternatives:
            options.append(_option(len(options) + 1, start, end, "", SlotMark.FREE))

    for start, end, phrase, mark, conflicts in unavailable:
        options.append(_option(len(options) + 1, start, end, phrase, mark, conflicts))
    return options


def _first_free_in_windows(
    windows: Sequence[RequestedWindow],
    busy: Sequence[BusyInterval],
    params: SchedulingParams,
    duration: timedelta,
    lead_floor: datetime,
) -> tuple[datetime, datetime] | None:
    zone = ZoneInfo(params.timezone)
    for window in windows:
        start = max(lead_floor, window.start.astimezone(zone))
        found = _agent_candidates(params, busy, duration, start, window.end.astimezone(zone), 1)
        if found:
            return found[0]
    return None


def _classify_exact(
    start: datetime, end: datetime, busy: Sequence[BusyInterval], params: SchedulingParams
) -> tuple[SlotMark, tuple[str, ...]]:
    if not _fits_business_hours(start, end, params):
        return SlotMark.OUTSIDE_BUSINESS_HOURS, ()
    conflicts = _conflict_strings(start, end, busy)
    if conflicts:
        return SlotMark.DOUBLE_BOOKING, conflicts
    return SlotMark.FREE, ()


def _option(
    number: int,
    start: datetime,
    end: datetime,
    phrase: str,
    mark: SlotMark,
    conflicts: tuple[str, ...] = (),
) -> SlotOption:
    return SlotOption(
        number=number, start=start, end=end, phrase=phrase, mark=mark, conflicts=conflicts
    )


def _conflict_strings(
    start: datetime, end: datetime, busy: Sequence[BusyInterval]
) -> tuple[str, ...]:
    zone = start.tzinfo
    result: list[str] = []
    for entry in busy:
        if _overlaps(start, end, entry.start, entry.end):
            busy_start = entry.start.astimezone(zone)
            busy_end = entry.end.astimezone(zone)
            title = entry.title or "Busy"
            result.append(f"{busy_start:%H:%M}-{busy_end:%H:%M} {title}")
    return tuple(result)


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    # No buffer between meetings (ADR-4): touching endpoints don't overlap.
    return a_start < b_end and b_start < a_end


def _is_free(start: datetime, end: datetime, busy: Sequence[BusyInterval]) -> bool:
    return not any(_overlaps(start, end, entry.start, entry.end) for entry in busy)


def _fits_business_hours(start: datetime, end: datetime, params: SchedulingParams) -> bool:
    if start.weekday() >= 5 or start.date() != end.date():
        return False
    day_start = start.replace(hour=params.business_hours_start, minute=0, second=0, microsecond=0)
    day_end = start.replace(hour=params.business_hours_end, minute=0, second=0, microsecond=0)
    return day_start <= start and end <= day_end


def _agent_candidates(
    params: SchedulingParams,
    busy: Sequence[BusyInterval],
    duration: timedelta,
    search_from: datetime,
    horizon_end: datetime,
    count: int,
) -> list[tuple[datetime, datetime]]:
    """Free, business-hours, half-hour-aligned slots >= `search_from`, < `horizon_end`."""
    if count <= 0:
        return []

    candidate = _round_up_to_half_hour(search_from)
    found: list[tuple[datetime, datetime]] = []
    step = timedelta(minutes=30)
    while len(found) < count:
        candidate = _snap_into_business_hours(candidate, params)
        if candidate >= horizon_end:
            break
        end = candidate + duration
        day_end = candidate.replace(
            hour=params.business_hours_end, minute=0, second=0, microsecond=0
        )
        if end > day_end:
            candidate = _next_business_day_start(candidate, params)
            continue
        if end > horizon_end:
            break
        if _is_free(candidate, end, busy):
            found.append((candidate, end))
        candidate = candidate + step
    return found


def _snap_into_business_hours(candidate: datetime, params: SchedulingParams) -> datetime:
    while True:
        if candidate.weekday() >= 5:
            candidate = _next_business_day_start(candidate, params)
            continue
        day_start = candidate.replace(
            hour=params.business_hours_start, minute=0, second=0, microsecond=0
        )
        day_end = candidate.replace(
            hour=params.business_hours_end, minute=0, second=0, microsecond=0
        )
        if candidate < day_start:
            candidate = day_start
            continue
        if candidate >= day_end:
            candidate = _next_business_day_start(candidate, params)
            continue
        return candidate


def _next_business_day_start(dt: datetime, params: SchedulingParams) -> datetime:
    next_day = dt + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day = next_day + timedelta(days=1)
    return next_day.replace(hour=params.business_hours_start, minute=0, second=0, microsecond=0)


def _add_business_days(day: datetime, n: int) -> datetime:
    count = 0
    while count < n:
        day = day + timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return day


def _horizon_end(anchor_local: datetime, params: SchedulingParams) -> datetime:
    """Business-hours end of the day `horizon_business_days` after `anchor_local`."""
    target = _add_business_days(anchor_local, params.horizon_business_days)
    return target.replace(hour=params.business_hours_end, minute=0, second=0, microsecond=0)


def _round_up_to_10_minutes(dt: datetime) -> datetime:
    remainder = dt.minute % 10
    if remainder == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt
    floored = dt.replace(second=0, microsecond=0)
    add = 10 - remainder if remainder else 10
    return floored + timedelta(minutes=add)


def _round_up_to_half_hour(dt: datetime) -> datetime:
    remainder = dt.minute % 30
    if remainder == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt
    floored = dt.replace(second=0, microsecond=0)
    add = 30 - remainder if remainder else 30
    return floored + timedelta(minutes=add)

"""Date-expression resolution: the LLM reads, this module calculates (D-64, D-66).

`resolve_expressions` turns each `DateExpression` the LLM restated (never
computed, per D-64) into one of three outcomes, each carrying the
original `phrase` so the approval email can show it next to the result:

- `ResolvedSlot` (domain/meeting.py) — an exact point in time (`source ==
  "exact"`, `end` is `start + duration`, the duration given by the
  caller), or a date range (`source == "window"`; a bare date with no
  time becomes a whole-day window, business hours applied later by
  `core/scheduling.py`, task 8). A `WindowExpr` may resolve to *several*
  `ResolvedSlot`s: one per business day it spans (task 39, F1) — see
  `_resolve_window`'s docstring.
- `NoTimeResolution` — a `NoneExpr`: no time was mentioned.
- `InvalidResolution` — a local time that doesn't exist or is ambiguous
  in its zone across a DST change (D-66), or a calendar date that has no
  valid resolution at all: a stated year/month/day that doesn't exist
  (e.g. 2027-02-29), or a missing year with no valid candidate in
  received.year-1/received.year/received.year+1 (task 37, D-64). Never
  an exception.

`ResolvedSlot` has no field for "no time" or "invalid", so those two
outcomes are modelled here instead, next to it in the same result union.

All comparisons and construction go through `zoneinfo`; `received_at`
must be aware (naive datetimes are rejected elsewhere, at the domain
layer). The reference "today" is `received_at`'s date *in the resolution
timezone* (the stated one if present, else the default, ADR-4), not in
whatever zone `received_at` happens to carry.

Year inference (task 37, D-64): the LLM never fills in a year itself
(`AbsoluteExpr.year` is `None` unless the sender actually wrote one). A
stated year always wins, even when it makes the date invalid (a crash is
never the outcome). Otherwise the resolver picks whichever of
received.year-1/received.year/received.year+1 makes the closest real
date to the received local day, skipping any year that doesn't make a
valid date (e.g. February 29 outside a leap year); a tie is broken in
favour of the future candidate.

`part_of_day` (task 37, D-64): a `WindowExpr` may carry the sender's own
word for a part of the day ("morning"/"afternoon"/"evening") instead of,
or alongside, an explicit `start_time`/`end_time`. The LLM never turns
that word into a clock time; this module maps it against the caller's
configured business hours (`morning` = business-hours-start-12:00,
`afternoon` = 12:00-business-hours-end, `evening` =
business-hours-end-21:00). An explicit `start_time`/`end_time` wins over
`part_of_day` on whichever end it was actually given for; the other end
falls back to `part_of_day`'s bound (or the whole-day default) if unset.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from dt_agent.domain.dates import (
    AbsoluteExpr,
    DateExpression,
    NoneExpr,
    RelativeDaysExpr,
    Weekday,
    WeekdayExpr,
    WindowExpr,
)
from dt_agent.domain.meeting import ResolvedSlot

# A window bound or a bare point expression: everything except WindowExpr/NoneExpr.
_PointExpr = AbsoluteExpr | RelativeDaysExpr | WeekdayExpr

_WEEKDAY_INDEX: dict[Weekday, int] = {day: index for index, day in enumerate(Weekday)}
_START_OF_DAY = time.min
_END_OF_DAY = time(23, 59, 59, 999999)


class NoTimeResolution(BaseModel):
    """A `NoneExpr` (D-64): no time mentioned, so no slot to compute."""

    model_config = ConfigDict(frozen=True)

    phrase: str


class InvalidResolution(BaseModel):
    """A local time that doesn't exist or is ambiguous in its zone (D-66), or a calendar
    date with no valid resolution at all (task 37, D-64)."""

    model_config = ConfigDict(frozen=True)

    phrase: str


DateResolution = ResolvedSlot | NoTimeResolution | InvalidResolution


def resolve_expressions(
    expressions: Sequence[DateExpression],
    received_at: datetime,
    timezone: str,
    duration: timedelta,
    *,
    business_hours_start: int,
    business_hours_end: int,
) -> list[DateResolution]:
    """Resolve each expression against `received_at`, in `timezone`.

    `timezone` is the already-resolved IANA zone name (the one stated in
    the email, else `DEFAULT_TIMEZONE`, D-1/ADR-4) — this function does
    not choose between them. `duration` is used only for expressions that
    resolve to an exact point in time; windows keep their own span.
    `business_hours_start`/`business_hours_end` (the operator's
    `Settings.BUSINESS_HOURS_START`/`BUSINESS_HOURS_END`, task 37) are
    used only for a window narrowed by `part_of_day`.

    The returned list may be *longer* than `expressions` (task 39, F1): a
    `WindowExpr` spanning several business days resolves to one
    `ResolvedSlot` per day, in date order, not one continuous multi-day
    range — see `_resolve_window`. Every other expression kind still
    contributes exactly one entry, in the same order as `expressions`.
    """
    zone = ZoneInfo(timezone)
    received_local_date = received_at.astimezone(zone).date()
    results: list[DateResolution] = []
    for expr in expressions:
        results.extend(
            _resolve_one(
                expr, received_local_date, zone, duration, business_hours_start, business_hours_end
            )
        )
    return results


def _resolve_one(
    expr: DateExpression,
    received_local_date: date,
    zone: ZoneInfo,
    duration: timedelta,
    business_hours_start: int,
    business_hours_end: int,
) -> list[DateResolution]:
    if isinstance(expr, NoneExpr):
        return [NoTimeResolution(phrase=expr.phrase)]
    if isinstance(expr, WindowExpr):
        return _resolve_window(
            expr, received_local_date, zone, business_hours_start, business_hours_end
        )
    day = _bound_date(expr, received_local_date)
    if day is None:
        return [InvalidResolution(phrase=expr.phrase)]
    return [_resolve_point(day, expr.time, expr.phrase, zone, duration)]


def _resolve_point(
    day: date,
    time_of_day: time | None,
    phrase: str,
    zone: ZoneInfo,
    duration: timedelta,
) -> DateResolution:
    if time_of_day is None:
        # A date with no time is a whole-day window (business hours applied later).
        start = _localize(day, _START_OF_DAY, zone)
        end = _localize(day, _END_OF_DAY, zone)
        if start is None or end is None:
            return InvalidResolution(phrase=phrase)
        return ResolvedSlot(start=start, end=end, phrase=phrase, source="window")

    start = _localize(day, time_of_day, zone)
    if start is None:
        return InvalidResolution(phrase=phrase)
    return ResolvedSlot(start=start, end=start + duration, phrase=phrase, source="exact")


def _resolve_window(
    expr: WindowExpr,
    received_local_date: date,
    zone: ZoneInfo,
    business_hours_start: int,
    business_hours_end: int,
) -> list[DateResolution]:
    """A window resolves to one `ResolvedSlot` *per business day* it spans (task 39, F1).

    Without this, a multi-day window narrowed by `part_of_day`/explicit times (e.g.
    "Tuesday to Wednesday afternoon") resolved to a single continuous range (Tue 12:00 to
    Wed 18:00), and `core/scheduling.py::_agent_candidates` walked straight through it with
    no per-day clipping, offering Wednesday *morning* — never requested. Splitting by day
    here means each `RequestedWindow` the scheduler sees (task 39) already carries only the
    hours actually asked for on that day.

    Bounds: a preset (`this_week`/`next_week`) is always Monday-Friday. Otherwise `start`
    and `end` are each optional (task 39): given only one, the missing bound mirrors it
    (`end_day = start_day`, or vice versa) — a single-day window. Neither bound nor a
    preset means nothing was actually stated: `NoTimeResolution` (unchanged).

    Each business day in `[start_day, end_day]` (Saturday/Sunday skipped) gets its own
    `_window_times`-clipped slot, in date order, so the window's overall end is always the
    *latest* end among them (`core/urgency.py::compute_urgent` takes the max, per D-24, not
    just the last entry — it doesn't rely on this ordering, task 39). If the range contains
    no business day at all (e.g. a weekend-only window), splitting would
    produce zero slots — silently dropping the expression, the very F2-shaped bug this task
    also fixes elsewhere. Instead this falls back to the previous single continuous-range
    behaviour, so the scheduler still sees the expression, searches inside it, finds
    nothing, and reports `OUTSIDE_REQUESTED_INTERVAL` alternatives after it (D-45) — the
    correct outcome for "this weekend", rather than no proposal at all.
    """
    start_day: date | None
    end_day: date | None
    if expr.preset is not None:
        week_monday = received_local_date - timedelta(days=received_local_date.weekday())
        if expr.preset == "next_week":
            week_monday += timedelta(days=7)
        start_day = week_monday
        end_day = week_monday + timedelta(days=4)
    elif expr.start is not None or expr.end is not None:
        start_bound = (
            _bound_date(expr.start, received_local_date) if expr.start is not None else None
        )
        end_bound = _bound_date(expr.end, received_local_date) if expr.end is not None else None
        if (expr.start is not None and start_bound is None) or (
            expr.end is not None and end_bound is None
        ):
            return [InvalidResolution(phrase=expr.phrase)]
        start_day = start_bound if start_bound is not None else end_bound
        end_day = end_bound if end_bound is not None else start_bound
    else:
        # Neither a preset nor either bound is set: nothing to resolve.
        return [NoTimeResolution(phrase=expr.phrase)]

    if start_day is None or end_day is None:
        return [InvalidResolution(phrase=expr.phrase)]

    start_time, end_time = _window_times(expr, business_hours_start, business_hours_end)
    span_days = (end_day - start_day).days + 1
    business_days = [
        start_day + timedelta(days=offset)
        for offset in range(span_days)
        if (start_day + timedelta(days=offset)).weekday() < 5
    ]

    if not business_days:
        # Weekend-only range: keep the single-range shape (see docstring above).
        start = _localize(start_day, start_time or _START_OF_DAY, zone)
        end = _localize(end_day, end_time or _END_OF_DAY, zone)
        if start is None or end is None:
            return [InvalidResolution(phrase=expr.phrase)]
        return [ResolvedSlot(start=start, end=end, phrase=expr.phrase, source="window")]

    slots: list[DateResolution] = []
    for day in business_days:
        start = _localize(day, start_time or _START_OF_DAY, zone)
        end = _localize(day, end_time or _END_OF_DAY, zone)
        if start is None or end is None:
            return [InvalidResolution(phrase=expr.phrase)]
        slots.append(ResolvedSlot(start=start, end=end, phrase=expr.phrase, source="window"))
    return slots


def _window_times(
    expr: WindowExpr, business_hours_start: int, business_hours_end: int
) -> tuple[time | None, time | None]:
    """The window's start/end clock times (task 37, D-64).

    An explicit `start_time`/`end_time` wins on whichever end the sender
    actually wrote it for; the other end falls back to `part_of_day`'s
    bound (against the caller's business hours), if any was given.
    """
    if expr.part_of_day is None:
        return expr.start_time, expr.end_time
    part_start, part_end = _part_of_day_bounds(
        expr.part_of_day, business_hours_start, business_hours_end
    )
    return expr.start_time or part_start, expr.end_time or part_end


def _part_of_day_bounds(
    part_of_day: Literal["morning", "afternoon", "evening"],
    business_hours_start: int,
    business_hours_end: int,
) -> tuple[time, time]:
    """`part_of_day`'s clock bounds against the caller's business hours (task 37, D-64):
    morning = business-hours-start-12:00, afternoon = 12:00-business-hours-end, evening =
    business-hours-end-21:00 (landing outside business hours, D-14, by construction)."""
    if part_of_day == "morning":
        return time(business_hours_start, 0), time(12, 0)
    if part_of_day == "afternoon":
        return time(12, 0), time(business_hours_end, 0)
    return time(business_hours_end, 0), time(21, 0)


def _bound_date(expr: _PointExpr, received_local_date: date) -> date | None:
    """The concrete calendar date `expr` names, or `None` if it has none (task 37).

    Only an `AbsoluteExpr` can fail: a stated year/month/day that isn't a
    real date, or (with no stated year) no candidate year among
    received.year-1/received.year/received.year+1 that makes one. A
    `RelativeDaysExpr`/`WeekdayExpr` is always resolvable, as before.
    """
    if isinstance(expr, AbsoluteExpr):
        if expr.year is not None:
            try:
                return date(expr.year, expr.month, expr.day)
            except ValueError:
                return None
        return _infer_year_date(expr.month, expr.day, received_local_date)
    if isinstance(expr, RelativeDaysExpr):
        return received_local_date + timedelta(days=expr.days)
    return _weekday_date(received_local_date, expr.weekday, expr.which)


def _infer_year_date(month: int, day: int, received_local_date: date) -> date | None:
    """No year was stated (D-64): pick whichever of received.year-1/received.year/
    received.year+1 makes the real date closest to `received_local_date`, skipping any
    year that doesn't make one (e.g. February 29 outside a leap year); a tie is broken in
    favour of the future candidate. `None` if no candidate year is valid at all."""
    candidates: list[date] = []
    for year in (
        received_local_date.year - 1,
        received_local_date.year,
        received_local_date.year + 1,
    ):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None

    def _distance_key(candidate: date) -> tuple[int, int]:
        delta_days = (candidate - received_local_date).days
        # Smallest absolute distance first; on a tie (same abs distance), the more
        # negative second key wins, which is the *larger* (future) delta_days.
        return (abs(delta_days), -delta_days)

    return min(candidates, key=_distance_key)


def _weekday_date(
    received_local_date: date,
    weekday: Weekday,
    which: Literal["this", "next"],
) -> date:
    target_index = _WEEKDAY_INDEX[weekday]
    current_monday = received_local_date - timedelta(days=received_local_date.weekday())
    if which == "next":
        return current_monday + timedelta(days=7 + target_index)
    candidate = current_monday + timedelta(days=target_index)
    if candidate < received_local_date:
        candidate += timedelta(days=7)
    return candidate


def _localize(day: date, time_of_day: time, zone: ZoneInfo) -> datetime | None:
    """Combine `day` and `time_of_day` in `zone`, or `None` if invalid (D-66)."""
    candidate = datetime(
        day.year,
        day.month,
        day.day,
        time_of_day.hour,
        time_of_day.minute,
        time_of_day.second,
        time_of_day.microsecond,
        tzinfo=zone,
    )
    return None if _is_invalid_local(candidate, zone) else candidate


def _is_invalid_local(local_dt: datetime, zone: ZoneInfo) -> bool:
    """D-66: does `local_dt` not exist, or is it ambiguous, in `zone`?

    Detected via the zoneinfo fold round-trip: an ambiguous wall-clock
    time (autumn DST) has two different UTC offsets depending on `fold`;
    a nonexistent one (spring DST) doesn't survive a round trip through
    UTC and back with the same wall-clock reading.
    """
    if local_dt.replace(fold=0).utcoffset() != local_dt.replace(fold=1).utcoffset():
        return True
    round_tripped = local_dt.astimezone(UTC).astimezone(zone)
    wall = (local_dt.year, local_dt.month, local_dt.day, local_dt.hour, local_dt.minute)
    round_tripped_wall = (
        round_tripped.year,
        round_tripped.month,
        round_tripped.day,
        round_tripped.hour,
        round_tripped.minute,
    )
    return wall != round_tripped_wall

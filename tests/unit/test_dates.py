"""Unit tests for dt_agent.core.dates (task 7, task 37, D-64, D-66).

Per the testing rules, this covers date-expression resolution itself
(relative days, weekday this/next, absolute with a missing year, year
inference across the received year boundary, a window preset, a window
narrowed by `part_of_day`, a shared time applied to several expressions,
and a stated timezone) — not pydantic field storage, which the
domain-model tests already cover.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dt_agent.core.dates import InvalidResolution, resolve_expressions
from dt_agent.core.scheduling import RequestedSlot, SchedulingParams, SchedulingRequest, schedule
from dt_agent.domain.dates import AbsoluteExpr, RelativeDaysExpr, Weekday, WeekdayExpr, WindowExpr
from dt_agent.domain.meeting import ResolvedSlot, SlotMark

BUCHAREST = ZoneInfo("Europe/Bucharest")
DURATION = timedelta(minutes=30)

# The operator's configured business hours (D-64/D-14), mirrored from the 9-18 convention
# `tests/unit/test_scheduling.py` and `tests/fakes/settings.py` already use.
BUSINESS_HOURS_START = 9
BUSINESS_HOURS_END = 18

# Monday 28 Sep 2026, 10:15 Europe/Bucharest (EEST).
RECEIVED_MON = datetime(2026, 9, 28, 10, 15, tzinfo=BUCHAREST)
# Wednesday 30 Sep 2026, 09:00 Europe/Bucharest (EEST).
RECEIVED_WED = datetime(2026, 9, 30, 9, 0, tzinfo=BUCHAREST)


def test_dates_relative_days() -> None:
    expr = RelativeDaysExpr(phrase="tomorrow at 15:00", days=1, time=time(15, 0))

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert result == ResolvedSlot(
        start=datetime(2026, 9, 29, 15, 0, tzinfo=BUCHAREST),
        end=datetime(2026, 9, 29, 15, 30, tzinfo=BUCHAREST),
        phrase="tomorrow at 15:00",
        source="exact",
    )


def test_dates_next_weekday_is_following_week() -> None:
    expr = WeekdayExpr(
        phrase="next Tuesday 10:00", weekday=Weekday.TUE, which="next", time=time(10, 0)
    )

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start == datetime(2026, 10, 6, 10, 0, tzinfo=BUCHAREST)


def test_dates_this_weekday_current_week_or_next_if_past() -> None:
    this_fri = WeekdayExpr(phrase="this Friday", weekday=Weekday.FRI, which="this", time=time(9, 0))
    this_mon = WeekdayExpr(phrase="this Monday", weekday=Weekday.MON, which="this", time=time(9, 0))

    fri_result, mon_result = resolve_expressions(
        [this_fri, this_mon],
        RECEIVED_WED,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(fri_result, ResolvedSlot)
    assert isinstance(mon_result, ResolvedSlot)
    assert fri_result.start == datetime(2026, 10, 2, 9, 0, tzinfo=BUCHAREST)
    assert mon_result.start == datetime(2026, 10, 5, 9, 0, tzinfo=BUCHAREST)


def test_dates_absolute_missing_year_from_received() -> None:
    expr = AbsoluteExpr(phrase="5 October at 14:00", month=10, day=5, time=time(14, 0))

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start == datetime(2026, 10, 5, 14, 0, tzinfo=BUCHAREST)


def test_dates_point_without_time_is_whole_day_window() -> None:
    """D-45/D-64: an absolute date with no time ("5 October") is a whole-day window
    (00:00-23:59:59.999999), not an exact point -- business hours are applied later,
    by scheduling."""
    expr = AbsoluteExpr(phrase="5 October", month=10, day=5)

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert result == ResolvedSlot(
        start=datetime(2026, 10, 5, 0, 0, tzinfo=BUCHAREST),
        end=datetime(2026, 10, 5, 23, 59, 59, 999999, tzinfo=BUCHAREST),
        phrase="5 October",
        source="window",
    )


def test_dates_window_next_week() -> None:
    """A preset window resolves to one whole-day `ResolvedSlot` per business day it spans
    (task 39, F1): five here, Monday through Friday, not one Mon-00:00-to-Fri-23:59 range
    (see `test_resolve_window_week_morning_gives_five_slots` for the narrowed case)."""
    expr = WindowExpr(phrase="next week", preset="next_week")

    results = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert len(results) == 5
    expected_days = [date(2026, 10, 5) + timedelta(days=offset) for offset in range(5)]
    for slot, day in zip(results, expected_days, strict=True):
        assert isinstance(slot, ResolvedSlot)
        assert slot.source == "window"
        assert slot.start == datetime(day.year, day.month, day.day, 0, 0, tzinfo=BUCHAREST)
        assert slot.end == datetime(
            day.year, day.month, day.day, 23, 59, 59, 999999, tzinfo=BUCHAREST
        )


def test_dates_shared_time_applies_to_each() -> None:
    today = RelativeDaysExpr(phrase="today at 13:00", days=0, time=time(13, 0))
    tomorrow = RelativeDaysExpr(phrase="tomorrow at 13:00", days=1, time=time(13, 0))

    today_result, tomorrow_result = resolve_expressions(
        [today, tomorrow],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(today_result, ResolvedSlot)
    assert isinstance(tomorrow_result, ResolvedSlot)
    assert today_result.start == datetime(2026, 9, 28, 13, 0, tzinfo=BUCHAREST)
    assert today_result.phrase == "today at 13:00"
    assert tomorrow_result.start == datetime(2026, 9, 29, 13, 0, tzinfo=BUCHAREST)
    assert tomorrow_result.phrase == "tomorrow at 13:00"


def test_dates_stated_timezone_used() -> None:
    expr = RelativeDaysExpr(phrase="today at 15:00", days=0, time=time(15, 0))

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Berlin",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.tzinfo == ZoneInfo("Europe/Berlin")
    # 15:00 CEST (UTC+2) is the same instant as 16:00 EEST (UTC+3).
    assert result.start == datetime(2026, 9, 28, 16, 0, tzinfo=BUCHAREST)


# -- Year inference (task 37, D-64): no stated year picks the closest of received.year-1/
# received.year/received.year+1, skipping any that raise ValueError; a stated year always
# wins, even when invalid. ------------------------------------------------------------


def test_resolve_absolute_january_in_december_is_next_year() -> None:
    expr = AbsoluteExpr(phrase="January 5", month=1, day=5)
    received = datetime(2026, 12, 20, 10, 0, tzinfo=BUCHAREST)

    [result] = resolve_expressions(
        [expr],
        received,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.date() == date(2027, 1, 5)


def test_resolve_absolute_december_in_january_is_last_year() -> None:
    expr = AbsoluteExpr(phrase="December 28", month=12, day=28)
    received = datetime(2026, 1, 10, 10, 0, tzinfo=BUCHAREST)

    [result] = resolve_expressions(
        [expr],
        received,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.date() == date(2025, 12, 28)


def test_resolve_absolute_recent_past_stays_this_year() -> None:
    expr = AbsoluteExpr(phrase="September 25", month=9, day=25)
    received = datetime(2026, 9, 28, 10, 0, tzinfo=BUCHAREST)

    [result] = resolve_expressions(
        [expr],
        received,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.date() == date(2026, 9, 25)


def test_resolve_absolute_near_future_stays_this_year() -> None:
    expr = AbsoluteExpr(phrase="September 30", month=9, day=30)
    received = datetime(2026, 9, 28, 10, 0, tzinfo=BUCHAREST)

    [result] = resolve_expressions(
        [expr],
        received,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.date() == date(2026, 9, 30)


def test_resolve_absolute_stated_year_wins() -> None:
    """Received 2026-12-20: the missing-year rule would pick 2027-01-05 (the closest
    candidate), but a stated year always overrides distance-based inference."""
    expr = AbsoluteExpr(phrase="January 5, 2026", year=2026, month=1, day=5)
    received = datetime(2026, 12, 20, 10, 0, tzinfo=BUCHAREST)

    [result] = resolve_expressions(
        [expr],
        received,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.date() == date(2026, 1, 5)


def test_resolve_absolute_tie_prefers_future() -> None:
    """A genuine tie: 2027-09-01 and 2028-09-01 are exactly 366 days apart (2028 is a leap
    year, so a Feb 29 falls between the two), and 2028-03-02 sits exactly 183 days from
    each. On this tie the resolver picks the future candidate, 2028-09-01, not the past
    one — verified below by summing the day-deltas, not just asserted."""
    expr = AbsoluteExpr(phrase="September 1st", month=9, day=1)
    received = datetime(2028, 3, 2, 10, 0, tzinfo=BUCHAREST)
    assert (date(2028, 3, 2) - date(2027, 9, 1)).days == (date(2028, 9, 1) - date(2028, 3, 2)).days

    [result] = resolve_expressions(
        [expr],
        received,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.date() == date(2028, 9, 1)


def test_resolve_absolute_feb_29_non_leap_is_invalid() -> None:
    """2025, 2026 and 2027 are three consecutive non-leap years, so a stated-year-less
    "February 29" received anywhere in 2026 has no valid candidate among received.year-1/
    received.year/received.year+1 (D-64) and must resolve to `InvalidResolution`, never
    raise. (A received year of 2027, as the task text suggests, would not demonstrate
    this: its received.year+1 candidate, 2028, is leap, so "February 29" would resolve to
    2028-02-29 instead of failing — the assertion below documents that as a sanity check.)
    """
    expr = AbsoluteExpr(phrase="February 29", month=2, day=29)
    received_2026 = datetime(2026, 1, 10, 10, 0, tzinfo=BUCHAREST)
    received_2027 = datetime(2027, 1, 10, 10, 0, tzinfo=BUCHAREST)

    [invalid] = resolve_expressions(
        [expr],
        received_2026,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )
    [valid_via_2028] = resolve_expressions(
        [expr],
        received_2027,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(invalid, InvalidResolution)
    assert isinstance(valid_via_2028, ResolvedSlot)
    assert valid_via_2028.start.date() == date(2028, 2, 29)


def test_resolve_absolute_february_31_is_always_invalid() -> None:
    """`AbsoluteExpr.day` allows up to 31 for any month, so "February 31" is invalid in
    every candidate year, stated or not."""
    expr = AbsoluteExpr(phrase="February 31, 2026", year=2026, month=2, day=31)
    received = datetime(2026, 1, 10, 10, 0, tzinfo=BUCHAREST)

    [result] = resolve_expressions(
        [expr],
        received,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, InvalidResolution)


# -- Window part-of-day (task 37, D-64): `part_of_day` maps to the configured business
# hours; explicit `start_time`/`end_time` win over it. ------------------------------------


def _wed_bound() -> WeekdayExpr:
    return WeekdayExpr(phrase="Wednesday", weekday=Weekday.WED, which="this")


def test_resolve_window_afternoon_uses_business_hours_end() -> None:
    expr = WindowExpr(
        phrase="Wednesday afternoon", start=_wed_bound(), end=_wed_bound(), part_of_day="afternoon"
    )

    [result_9_18] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=9,
        business_hours_end=18,
    )
    [result_9_17] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=9,
        business_hours_end=17,
    )

    assert isinstance(result_9_18, ResolvedSlot)
    assert result_9_18.start.time() == time(12, 0)
    assert result_9_18.end.time() == time(18, 0)
    assert isinstance(result_9_17, ResolvedSlot)
    assert result_9_17.start.time() == time(12, 0)
    assert result_9_17.end.time() == time(17, 0)


def test_resolve_window_morning_uses_business_hours_start() -> None:
    expr = WindowExpr(
        phrase="Wednesday morning", start=_wed_bound(), end=_wed_bound(), part_of_day="morning"
    )

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.time() == time(BUSINESS_HOURS_START, 0)
    assert result.end.time() == time(12, 0)


def test_resolve_window_evening_is_outside_business_hours() -> None:
    """D-14: business hours end at `BUSINESS_HOURS_END`, so an evening window
    (`BUSINESS_HOURS_END:00`-21:00) starts exactly where business hours stop. Feeding that
    start instant into the existing scheduling code (already exercised the same way by
    `test_scheduling_outside_business_hours_marked`) still reports it as outside business
    hours."""
    expr = WindowExpr(
        phrase="Wednesday evening", start=_wed_bound(), end=_wed_bound(), part_of_day="evening"
    )

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.time() == time(BUSINESS_HOURS_END, 0)
    assert result.end.time() == time(21, 0)

    params = SchedulingParams(
        timezone="Europe/Bucharest",
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
        default_meeting_minutes=30,
        horizon_business_days=5,
        min_lead_minutes=30,
        max_options=3,
    )
    scheduling_result = schedule(
        SchedulingRequest(exact_slots=(RequestedSlot(start=result.start, phrase=result.phrase),)),
        busy=(),
        params=params,
        now=RECEIVED_MON,
    )

    assert scheduling_result.options[-1].mark == SlotMark.OUTSIDE_BUSINESS_HOURS


def test_resolve_window_explicit_times_override_part_of_day() -> None:
    expr = WindowExpr(
        phrase="Wednesday between 2 and 4pm",
        start=_wed_bound(),
        end=_wed_bound(),
        part_of_day="afternoon",
        start_time=time(14, 0),
        end_time=time(16, 0),
    )

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.time() == time(14, 0)
    assert result.end.time() == time(16, 0)


def test_resolve_window_partial_explicit_time_falls_back_to_part_of_day() -> None:
    """Per-field precedence: an explicit `start_time` with no `end_time` keeps
    `part_of_day`'s end bound, and vice versa (only the field the sender actually wrote
    wins)."""
    expr = WindowExpr(
        phrase="Wednesday afternoon, from 3pm",
        start=_wed_bound(),
        end=_wed_bound(),
        part_of_day="afternoon",
        start_time=time(15, 0),
    )

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start.time() == time(15, 0)
    assert result.end.time() == time(BUSINESS_HOURS_END, 0)


# -- Window day-splitting (task 39, D-64, F1): a window resolves to one `ResolvedSlot` per
# business day it spans (skipping Saturday/Sunday), each independently narrowed by
# `part_of_day`/explicit times -- never one continuous multi-day range, which would let
# `_agent_candidates` wander into an untouched day's business hours. A window with only
# `start` or only `end` is a single-day window (the missing bound mirrors the given one). A
# range with no business day in it (weekend-only) keeps the previous single-range shape, so
# the expression isn't silently dropped -- the scheduler then finds nothing inside it and
# falls back to `OUTSIDE_REQUESTED_INTERVAL` alternatives after it (D-45). ------------------


def test_resolve_window_start_only_is_single_day() -> None:
    expr = WindowExpr(
        phrase="from next Tuesday",
        start=WeekdayExpr(phrase="next Tuesday", weekday=Weekday.TUE, which="next"),
    )

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start == datetime(2026, 10, 6, 0, 0, tzinfo=BUCHAREST)
    assert result.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=BUCHAREST)


def test_resolve_window_end_only_is_single_day() -> None:
    expr = WindowExpr(
        phrase="by next Tuesday",
        end=WeekdayExpr(phrase="next Tuesday", weekday=Weekday.TUE, which="next"),
    )

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start == datetime(2026, 10, 6, 0, 0, tzinfo=BUCHAREST)
    assert result.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=BUCHAREST)


def test_resolve_window_two_days_afternoon_gives_two_slots() -> None:
    """F1: "Tuesday to Wednesday afternoon" must not resolve to one Tue-12:00-to-Wed-18:00
    range (which would let the scheduler offer Wednesday morning); it resolves to two
    independently-clipped afternoon slots."""
    tue = WeekdayExpr(phrase="Tuesday", weekday=Weekday.TUE, which="next")
    wed = WeekdayExpr(phrase="Wednesday", weekday=Weekday.WED, which="next")
    expr = WindowExpr(
        phrase="Tuesday to Wednesday afternoon", start=tue, end=wed, part_of_day="afternoon"
    )

    results = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=9,
        business_hours_end=18,
    )

    assert len(results) == 2
    tue_slot, wed_slot = results
    assert isinstance(tue_slot, ResolvedSlot)
    assert isinstance(wed_slot, ResolvedSlot)
    assert tue_slot.start == datetime(2026, 10, 6, 12, 0, tzinfo=BUCHAREST)
    assert tue_slot.end == datetime(2026, 10, 6, 18, 0, tzinfo=BUCHAREST)
    assert wed_slot.start == datetime(2026, 10, 7, 12, 0, tzinfo=BUCHAREST)
    assert wed_slot.end == datetime(2026, 10, 7, 18, 0, tzinfo=BUCHAREST)


def test_resolve_window_week_morning_gives_five_slots() -> None:
    expr = WindowExpr(phrase="next week mornings", preset="next_week", part_of_day="morning")

    results = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=9,
        business_hours_end=17,
    )

    assert len(results) == 5
    expected_days = [date(2026, 10, 5) + timedelta(days=offset) for offset in range(5)]
    for slot, day in zip(results, expected_days, strict=True):
        assert isinstance(slot, ResolvedSlot)
        assert slot.start == datetime(day.year, day.month, day.day, 9, 0, tzinfo=BUCHAREST)
        assert slot.end == datetime(day.year, day.month, day.day, 12, 0, tzinfo=BUCHAREST)


def test_resolve_window_over_weekend_skips_saturday_sunday() -> None:
    fri = WeekdayExpr(phrase="Friday", weekday=Weekday.FRI, which="this")
    mon = WeekdayExpr(phrase="Monday", weekday=Weekday.MON, which="next")
    expr = WindowExpr(phrase="Friday through Monday", start=fri, end=mon)

    results = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert len(results) == 2
    fri_slot, mon_slot = results
    assert isinstance(fri_slot, ResolvedSlot)
    assert isinstance(mon_slot, ResolvedSlot)
    assert fri_slot.start.date() == date(2026, 10, 2)
    assert mon_slot.start.date() == date(2026, 10, 5)


def test_resolve_window_weekend_only_keeps_single_range() -> None:
    """No business day falls in the window at all: keep the previous single-range shape
    (documented in `_resolve_window`) rather than dropping the expression, so the scheduler
    still sees it and falls back to `OUTSIDE_REQUESTED_INTERVAL` alternatives (D-45)."""
    sat = WeekdayExpr(phrase="Saturday", weekday=Weekday.SAT, which="this")
    sun = WeekdayExpr(phrase="Sunday", weekday=Weekday.SUN, which="this")
    expr = WindowExpr(phrase="this weekend", start=sat, end=sun)

    [result] = resolve_expressions(
        [expr],
        RECEIVED_MON,
        "Europe/Bucharest",
        DURATION,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert isinstance(result, ResolvedSlot)
    assert result.start == datetime(2026, 10, 3, 0, 0, tzinfo=BUCHAREST)
    assert result.end == datetime(2026, 10, 4, 23, 59, 59, 999999, tzinfo=BUCHAREST)

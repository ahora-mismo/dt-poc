"""Unit tests for dt_agent.core.scheduling (task 8, ADR-4, D-13, D-14, D-45).

Fixed calendar: Europe/Bucharest, business hours 9-18, default meeting 30
minutes, horizon 5 business days, min lead 25 minutes, max 3 options
(unless a test overrides `max_options`). All dates below are EEST
(2026-09-28 .. 2026-10-09 is entirely after the spring change and well
before the autumn one, so DST never enters into it, D-66).

Weekdays used: 2026-09-28 Mon, 09-29 Tue, 09-30 Wed, 10-01 Thu, 10-02 Fri,
10-05 Mon, 10-06 Tue, 10-07 Wed, 10-08 Thu, 10-09 Fri.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from dt_agent.core.scheduling import (
    BusyInterval,
    RequestedSlot,
    RequestedWindow,
    SchedulingFlag,
    SchedulingParams,
    SchedulingRequest,
    schedule,
)
from dt_agent.domain.meeting import SlotMark

_TZ = ZoneInfo("Europe/Bucharest")


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_TZ)


def _params(**overrides: object) -> SchedulingParams:
    base: dict[str, object] = {
        "timezone": "Europe/Bucharest",
        "business_hours_start": 9,
        "business_hours_end": 18,
        "default_meeting_minutes": 30,
        "horizon_business_days": 5,
        "min_lead_minutes": 25,
        "max_options": 3,
    }
    base.update(overrides)
    return SchedulingParams(**base)  # type: ignore[arg-type]


def test_scheduling_rounds_up_to_10_minutes() -> None:
    params = _params()
    now = _dt(2026, 9, 28, 8, 0)

    rounded = schedule(
        SchedulingRequest(exact_slots=(RequestedSlot(_dt(2026, 9, 28, 14, 4), "14:04"),)),
        busy=(),
        params=params,
        now=now,
    )
    assert len(rounded.options) == 1
    assert rounded.options[0].start == _dt(2026, 9, 28, 14, 10)

    unchanged = schedule(
        SchedulingRequest(exact_slots=(RequestedSlot(_dt(2026, 9, 28, 14, 0), "14:00"),)),
        busy=(),
        params=params,
        now=now,
    )
    assert unchanged.options[0].start == _dt(2026, 9, 28, 14, 0)


def test_scheduling_agent_slots_respect_lead_time() -> None:
    params = _params()

    result_36 = schedule(SchedulingRequest(), busy=(), params=params, now=_dt(2026, 9, 28, 10, 36))
    assert result_36.options[0].start == _dt(2026, 9, 28, 11, 30)

    result_04 = schedule(SchedulingRequest(), busy=(), params=params, now=_dt(2026, 9, 28, 10, 4))
    assert result_04.options[0].start == _dt(2026, 9, 28, 10, 30)


def test_scheduling_skips_weekend() -> None:
    params = _params()
    now = _dt(2026, 10, 2, 17, 40)  # Friday

    result = schedule(SchedulingRequest(), busy=(), params=params, now=now)

    assert result.options[0].start == _dt(2026, 10, 5, 9, 0)
    assert result.options[0].mark == SlotMark.FREE


def test_scheduling_exact_free_time_single_option() -> None:
    params = _params()
    now = _dt(2026, 9, 28, 8, 0)

    result = schedule(
        SchedulingRequest(exact_slots=(RequestedSlot(_dt(2026, 9, 29, 14, 0), "Tue 14:00"),)),
        busy=(),
        params=params,
        now=now,
    )

    assert len(result.options) == 1
    option = result.options[0]
    assert option.number == 1
    assert option.start == _dt(2026, 9, 29, 14, 0)
    assert option.end == _dt(2026, 9, 29, 14, 30)
    assert option.mark == SlotMark.FREE


def test_scheduling_exact_busy_time_alternatives_first() -> None:
    params = _params()
    now = _dt(2026, 9, 29, 8, 0)
    busy = (BusyInterval(_dt(2026, 9, 29, 14, 0), _dt(2026, 9, 29, 15, 0), "Board meeting"),)

    result = schedule(
        SchedulingRequest(exact_slots=(RequestedSlot(_dt(2026, 9, 29, 14, 0), "Tue 14:00"),)),
        busy=busy,
        params=params,
        now=now,
    )

    assert len(result.options) == 3
    assert result.options[0].mark == SlotMark.FREE
    assert result.options[0].start == _dt(2026, 9, 29, 15, 0)
    assert result.options[1].mark == SlotMark.FREE
    assert result.options[1].start == _dt(2026, 9, 29, 15, 30)
    last = result.options[2]
    assert last.number == 3
    assert last.start == _dt(2026, 9, 29, 14, 0)
    assert last.mark == SlotMark.DOUBLE_BOOKING
    assert any("Board meeting" in c for c in last.conflicts)


def test_scheduling_outside_business_hours_marked() -> None:
    params = _params()
    now = _dt(2026, 9, 29, 8, 0)

    result = schedule(
        SchedulingRequest(exact_slots=(RequestedSlot(_dt(2026, 9, 29, 18, 30), "18:30"),)),
        busy=(),
        params=params,
        now=now,
    )

    assert len(result.options) == 3
    assert result.options[0].mark == SlotMark.FREE
    assert result.options[1].mark == SlotMark.FREE
    last = result.options[2]
    assert last.start == _dt(2026, 9, 29, 18, 30)
    assert last.mark == SlotMark.OUTSIDE_BUSINESS_HOURS


def test_scheduling_adjacent_busy_no_buffer() -> None:
    params = _params()
    now = _dt(2026, 9, 29, 8, 0)
    busy = (BusyInterval(_dt(2026, 9, 29, 14, 0), _dt(2026, 9, 29, 15, 0), "Board meeting"),)

    result = schedule(
        SchedulingRequest(exact_slots=(RequestedSlot(_dt(2026, 9, 29, 15, 0), "15:00"),)),
        busy=busy,
        params=params,
        now=now,
    )

    assert len(result.options) == 1
    assert result.options[0].start == _dt(2026, 9, 29, 15, 0)
    assert result.options[0].mark == SlotMark.FREE


def test_scheduling_several_times_mixed() -> None:
    params = _params()
    now = _dt(2026, 9, 28, 8, 0)
    busy = (BusyInterval(_dt(2026, 9, 30, 10, 0), _dt(2026, 9, 30, 10, 30), "Standup"),)

    result = schedule(
        SchedulingRequest(
            exact_slots=(
                RequestedSlot(_dt(2026, 9, 29, 14, 0), "Tue 14:00"),
                RequestedSlot(_dt(2026, 9, 30, 10, 0), "Wed 10:00"),
            )
        ),
        busy=busy,
        params=params,
        now=now,
    )

    assert len(result.options) == 4
    assert result.options[0].mark == SlotMark.FREE
    assert result.options[0].start == _dt(2026, 9, 29, 14, 0)
    assert result.options[1].mark == SlotMark.FREE
    assert result.options[1].start == _dt(2026, 9, 28, 9, 0)
    assert result.options[2].mark == SlotMark.FREE
    assert result.options[2].start == _dt(2026, 9, 28, 9, 30)
    last = result.options[3]
    assert last.start == _dt(2026, 9, 30, 10, 0)
    assert last.mark == SlotMark.DOUBLE_BOOKING
    assert any("Standup" in c for c in last.conflicts)


def test_scheduling_several_times_all_busy() -> None:
    params = _params()
    now = _dt(2026, 9, 28, 8, 0)
    busy = (
        BusyInterval(_dt(2026, 9, 29, 14, 0), _dt(2026, 9, 29, 15, 0), "Board meeting"),
        BusyInterval(_dt(2026, 9, 30, 10, 0), _dt(2026, 9, 30, 10, 30), "Standup"),
    )

    result = schedule(
        SchedulingRequest(
            exact_slots=(
                RequestedSlot(_dt(2026, 9, 29, 14, 0), "Tue 14:00"),
                RequestedSlot(_dt(2026, 9, 30, 10, 0), "Wed 10:00"),
            )
        ),
        busy=busy,
        params=params,
        now=now,
    )

    assert len(result.options) == 5
    for option in result.options[:3]:
        assert option.mark == SlotMark.FREE
    assert result.options[0].start == _dt(2026, 9, 28, 9, 0)
    assert result.options[1].start == _dt(2026, 9, 28, 9, 30)
    assert result.options[2].start == _dt(2026, 9, 28, 10, 0)
    assert result.options[3].start == _dt(2026, 9, 29, 14, 0)
    assert result.options[3].mark == SlotMark.DOUBLE_BOOKING
    assert result.options[4].start == _dt(2026, 9, 30, 10, 0)
    assert result.options[4].mark == SlotMark.DOUBLE_BOOKING


def test_scheduling_past_times_dropped() -> None:
    params = _params()
    now = _dt(2026, 9, 30, 12, 0)

    mixed = schedule(
        SchedulingRequest(
            exact_slots=(
                RequestedSlot(_dt(2026, 9, 28, 10, 0), "past"),
                RequestedSlot(_dt(2026, 9, 30, 15, 0), "future"),
            )
        ),
        busy=(),
        params=params,
        now=now,
    )
    assert len(mixed.options) == 1
    assert mixed.options[0].start == _dt(2026, 9, 30, 15, 0)

    all_past = schedule(
        SchedulingRequest(exact_slots=(RequestedSlot(_dt(2026, 9, 28, 10, 0), "past"),)),
        busy=(),
        params=params,
        now=now,
    )
    assert len(all_past.options) == 3
    assert all(o.mark == SlotMark.FREE for o in all_past.options)
    assert all_past.options[0].start == _dt(2026, 9, 30, 12, 30)


def test_scheduling_window_without_free_slot_goes_outside_marked() -> None:
    params = _params()
    now = _dt(2026, 9, 28, 8, 0)
    busy = (BusyInterval(_dt(2026, 9, 29, 9, 0), _dt(2026, 9, 29, 18, 0), "Offsite"),)

    result = schedule(
        SchedulingRequest(
            windows=(RequestedWindow(_dt(2026, 9, 29, 9, 0), _dt(2026, 9, 29, 18, 0), "Tuesday"),)
        ),
        busy=busy,
        params=params,
        now=now,
    )

    assert len(result.options) == 3
    assert result.options[0].start == _dt(2026, 9, 30, 9, 0)
    assert all(o.mark == SlotMark.OUTSIDE_REQUESTED_INTERVAL for o in result.options)
    assert all(o.phrase == "Tuesday" for o in result.options)


def test_scheduling_all_day_request_offers_business_hours_slots() -> None:
    """D-45/D-64: a date-only request ("5 October", no time) resolves upstream
    (`core/dates.py`) to a whole-day window spanning 00:00-23:59:59.999999; scheduling
    must still confine its option to business hours, never before
    `business_hours_start` or after `business_hours_end`; one requested time gives one
    option (D-13)."""
    params = _params()
    now = _dt(2026, 9, 28, 8, 0)
    whole_day = RequestedWindow(
        datetime(2026, 10, 5, 0, 0, 0, tzinfo=_TZ),
        datetime(2026, 10, 5, 23, 59, 59, 999999, tzinfo=_TZ),
        "5 October",
    )

    result = schedule(
        SchedulingRequest(windows=(whole_day,)),
        busy=(),
        params=params,
        now=now,
    )

    assert len(result.options) == 1
    option = result.options[0]
    assert option.mark == SlotMark.FREE
    assert option.start == _dt(2026, 10, 5, 9, 0)
    assert option.end <= _dt(2026, 10, 5, 18, 0)


def test_schedule_two_windows_one_option_each() -> None:
    """D-13: two distinct requested windows give one option each, in time order: each
    window's first free slot."""
    params = _params()
    now = _dt(2026, 9, 28, 8, 0)
    tue_narrow = RequestedWindow(_dt(2026, 9, 29, 9, 0), _dt(2026, 9, 29, 9, 30), "Tue narrow")
    wed_full = RequestedWindow(_dt(2026, 9, 30, 9, 0), _dt(2026, 9, 30, 18, 0), "Wed full")

    result = schedule(
        SchedulingRequest(windows=(tue_narrow, wed_full)),
        busy=(),
        params=params,
        now=now,
    )

    assert [(o.start, o.phrase, o.mark) for o in result.options] == [
        (_dt(2026, 9, 29, 9, 0), "Tue narrow", SlotMark.FREE),
        (_dt(2026, 9, 30, 9, 0), "Wed full", SlotMark.FREE),
    ]


def test_schedule_two_windows_outside_alternatives_start_after_last_window() -> None:
    """task 39: when neither window has a free slot, the outside-interval alternatives
    search starts after the *last* window's end, not the first's."""
    params = _params()
    now = _dt(2026, 9, 28, 8, 0)
    tue_narrow = RequestedWindow(_dt(2026, 9, 29, 9, 0), _dt(2026, 9, 29, 9, 30), "Tue narrow")
    wed_full = RequestedWindow(_dt(2026, 9, 30, 9, 0), _dt(2026, 9, 30, 18, 0), "Wed full")
    busy = (
        BusyInterval(_dt(2026, 9, 29, 9, 0), _dt(2026, 9, 29, 9, 30), "x"),
        BusyInterval(_dt(2026, 9, 30, 9, 0), _dt(2026, 9, 30, 18, 0), "y"),
    )

    result = schedule(
        SchedulingRequest(windows=(tue_narrow, wed_full)),
        busy=busy,
        params=params,
        now=now,
    )

    assert result.options
    assert all(o.mark == SlotMark.OUTSIDE_REQUESTED_INTERVAL for o in result.options)
    assert all(o.phrase == "Wed full" for o in result.options)
    assert result.options[0].start == _dt(2026, 10, 1, 9, 0)


def test_scheduling_horizon_business_days() -> None:
    params = _params(max_options=1)
    now = _dt(2026, 10, 2, 9, 0)  # Friday
    busy = (BusyInterval(_dt(2026, 10, 2, 9, 0), _dt(2026, 10, 9, 17, 30), "Busy block"),)

    result = schedule(SchedulingRequest(), busy=busy, params=params, now=now)

    assert len(result.options) == 1
    assert result.options[0].start == _dt(2026, 10, 9, 17, 30)
    assert result.options[0].end == _dt(2026, 10, 9, 18, 0)


def test_scheduling_limits_flagged() -> None:
    params = _params()
    now = _dt(2026, 9, 28, 8, 0)

    too_many_attendees = schedule(
        SchedulingRequest(
            exact_slots=(RequestedSlot(_dt(2026, 9, 29, 14, 0), "Tue 14:00"),),
            attendee_count=21,
        ),
        busy=(),
        params=params,
        now=now,
    )
    assert SchedulingFlag.TOO_MANY_ATTENDEES in too_many_attendees.flags
    assert too_many_attendees.options

    too_long = schedule(
        SchedulingRequest(
            exact_slots=(RequestedSlot(_dt(2026, 9, 29, 9, 0), "Tue 9:00"),),
            duration_minutes=600,
        ),
        busy=(),
        params=params,
        now=now,
    )
    assert SchedulingFlag.EVENT_LONGER_THAN_BUSINESS_DAY in too_long.flags
    assert too_long.options


# -- One option per requested time (D-13) ---------------------------------------------------


def _thursday_afternoon() -> RequestedWindow:
    return RequestedWindow(_dt(2026, 10, 1, 12, 0), _dt(2026, 10, 1, 18, 0), "Thursday afternoon")


def test_scheduling_window_and_exact_time_both_offered() -> None:
    result = schedule(
        SchedulingRequest(
            exact_slots=(RequestedSlot(_dt(2026, 10, 2, 10, 0), "Friday at 10:00"),),
            windows=(_thursday_afternoon(),),
        ),
        busy=(),
        params=_params(),
        now=_dt(2026, 9, 28, 8, 0),
    )

    assert [(o.start, o.phrase, o.mark) for o in result.options] == [
        (_dt(2026, 10, 1, 12, 0), "Thursday afternoon", SlotMark.FREE),
        (_dt(2026, 10, 2, 10, 0), "Friday at 10:00", SlotMark.FREE),
    ]


def test_scheduling_one_option_per_window() -> None:
    result = schedule(
        SchedulingRequest(windows=(_thursday_afternoon(),)),
        busy=(),
        params=_params(),
        now=_dt(2026, 9, 28, 8, 0),
    )

    assert [(o.start, o.phrase) for o in result.options] == [
        (_dt(2026, 10, 1, 12, 0), "Thursday afternoon")
    ]


def test_scheduling_multi_day_window_pieces_give_one_option() -> None:
    pieces = (
        RequestedWindow(_dt(2026, 9, 29, 9, 0), _dt(2026, 9, 29, 18, 0), "next few days"),
        RequestedWindow(_dt(2026, 9, 30, 9, 0), _dt(2026, 9, 30, 18, 0), "next few days"),
    )
    result = schedule(
        SchedulingRequest(windows=pieces),
        busy=(BusyInterval(_dt(2026, 9, 29, 9, 0), _dt(2026, 9, 29, 18, 0), "Offsite"),),
        params=_params(),
        now=_dt(2026, 9, 28, 8, 0),
    )

    assert [(o.start, o.phrase) for o in result.options] == [
        (_dt(2026, 9, 30, 9, 0), "next few days")
    ]


def test_scheduling_all_requested_free_no_alternatives() -> None:
    result = schedule(
        SchedulingRequest(
            exact_slots=(
                RequestedSlot(_dt(2026, 9, 29, 10, 0), "Tuesday at 10"),
                RequestedSlot(_dt(2026, 9, 30, 15, 0), "Wednesday at 15"),
            )
        ),
        busy=(),
        params=_params(),
        now=_dt(2026, 9, 28, 8, 0),
    )

    assert [(o.start, o.mark) for o in result.options] == [
        (_dt(2026, 9, 29, 10, 0), SlotMark.FREE),
        (_dt(2026, 9, 30, 15, 0), SlotMark.FREE),
    ]


def test_scheduling_unavailable_requested_time_gets_alternatives_and_is_last() -> None:
    busy = (BusyInterval(_dt(2026, 10, 2, 10, 0), _dt(2026, 10, 2, 11, 0), "Board meeting"),)
    result = schedule(
        SchedulingRequest(
            exact_slots=(RequestedSlot(_dt(2026, 10, 2, 10, 0), "Friday at 10:00"),),
            windows=(_thursday_afternoon(),),
        ),
        busy=busy,
        params=_params(),
        now=_dt(2026, 9, 28, 8, 0),
    )

    options = result.options
    assert options[0].start == _dt(2026, 10, 1, 12, 0)
    assert options[0].phrase == "Thursday afternoon"
    assert options[0].mark is SlotMark.FREE
    assert all(o.mark is SlotMark.FREE and o.phrase == "" for o in options[1:-1])
    assert len(options) == 4  # the window slot, 2 alternatives (max 3), the busy time last
    assert options[-1].start == _dt(2026, 10, 2, 10, 0)
    assert options[-1].mark is SlotMark.DOUBLE_BOOKING
    assert options[-1].conflicts == ("10:00-11:00 Board meeting",)
    assert [o.number for o in options] == [1, 2, 3, 4]

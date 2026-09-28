"""Unit tests for dt_agent.core.urgency (task 31, task 37, D-24, D-64).

Per the testing rules: pure-function tests on `compute_urgent`, with
concrete 2026 dates in Europe/Bucharest. `RelativeDaysExpr.days` may be
negative (D-64's past-deadline case, "due yesterday" = -1).
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from dt_agent.core.urgency import compute_urgency, compute_urgent
from dt_agent.domain.dates import AbsoluteExpr, RelativeDaysExpr, Weekday, WeekdayExpr, WindowExpr
from dt_agent.domain.labels import PrimaryLabel

BUCHAREST = "Europe/Bucharest"
_ZONE = ZoneInfo(BUCHAREST)

# The operator's configured business hours (D-14), mirrored from `test_dates.py`'s
# convention; `compute_urgent` threads them into the same date resolver (task 37).
BUSINESS_HOURS_START = 9
BUSINESS_HOURS_END = 18

# Monday 28 Sep 2026 .. Friday 2 Oct 2026 (mirrors tests/unit/test_dates.py's anchor week).
RECEIVED_MON = datetime(2026, 9, 28, 10, 15, tzinfo=_ZONE)
RECEIVED_TUE = datetime(2026, 9, 29, 10, 15, tzinfo=_ZONE)
RECEIVED_WED = datetime(2026, 9, 30, 9, 0, tzinfo=_ZONE)
RECEIVED_THU = datetime(2026, 10, 1, 8, 0, tzinfo=_ZONE)
# Sunday 20 Dec 2026 (used by the year-boundary urgency tests, task 37).
RECEIVED_DEC20 = datetime(2026, 12, 20, 10, 15, tzinfo=_ZONE)


def test_urgency_deadline_within_window() -> None:
    deadline_thu_no_time = WeekdayExpr(phrase="Thursday", weekday=Weekday.THU, which="this")
    deadline_thu_2300 = WeekdayExpr(
        phrase="Thursday 23:00", weekday=Weekday.THU, which="this", time=time(23, 0)
    )
    deadline_fri_0900 = WeekdayExpr(
        phrase="Friday 09:00", weekday=Weekday.FRI, which="this", time=time(9, 0)
    )

    assert (
        compute_urgent(
            primary=PrimaryLabel.TASK,
            deadline=deadline_thu_no_time,
            urgency_requested=False,
            received_at=RECEIVED_TUE,
            timezone=BUCHAREST,
            business_hours_start=BUSINESS_HOURS_START,
            business_hours_end=BUSINESS_HOURS_END,
        )
        is True
    )
    assert (
        compute_urgent(
            primary=PrimaryLabel.TASK,
            deadline=deadline_thu_2300,
            urgency_requested=False,
            received_at=RECEIVED_TUE,
            timezone=BUCHAREST,
            business_hours_start=BUSINESS_HOURS_START,
            business_hours_end=BUSINESS_HOURS_END,
        )
        is True
    )
    assert (
        compute_urgent(
            primary=PrimaryLabel.TASK,
            deadline=deadline_fri_0900,
            urgency_requested=False,
            received_at=RECEIVED_TUE,
            timezone=BUCHAREST,
            business_hours_start=BUSINESS_HOURS_START,
            business_hours_end=BUSINESS_HOURS_END,
        )
        is False
    )


def test_urgency_requested_without_deadline() -> None:
    urgent = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=None,
        urgency_requested=True,
        received_at=RECEIVED_TUE,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert urgent is True


def test_urgency_overdue_and_no_deadline() -> None:
    deadline_yesterday = RelativeDaysExpr(phrase="yesterday", days=-1)

    overdue = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=deadline_yesterday,
        urgency_requested=False,
        received_at=RECEIVED_WED,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )
    no_deadline_not_requested = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=None,
        urgency_requested=False,
        received_at=RECEIVED_WED,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert overdue is True
    assert no_deadline_not_requested is False


def test_urgency_window_deadline_uses_end() -> None:
    this_week = WindowExpr(phrase="this week", preset="this_week")

    received_mon = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=this_week,
        urgency_requested=False,
        received_at=RECEIVED_MON,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )
    received_tue = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=this_week,
        urgency_requested=False,
        received_at=RECEIVED_TUE,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )
    received_wed = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=this_week,
        urgency_requested=False,
        received_at=RECEIVED_WED,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )
    received_thu = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=this_week,
        urgency_requested=False,
        received_at=RECEIVED_THU,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert received_mon is False
    assert received_tue is False
    assert received_wed is True  # Friday is exactly the inclusive cutoff (Wed + 2 days)
    assert received_thu is True


def test_urgency_multi_day_window_deadline_uses_last_day_end() -> None:
    """task 39 (F1): a window deadline spanning several business days now resolves to
    several `ResolvedSlot`s, one per day (`core/dates.py`); D-24 still means the window's
    *overall* end. Received Mon 28 Sep, deadline "Tuesday through Thursday": the first
    day's end (Tue 23:59:59) is on or before the two-day cutoff (Wed 23:59:59), so a
    first-slot bug would wrongly say urgent; the last day's end (Thu 23:59:59) is past it,
    so the correct answer is not urgent."""
    tue = WeekdayExpr(phrase="Tuesday", weekday=Weekday.TUE, which="this")
    thu = WeekdayExpr(phrase="Thursday", weekday=Weekday.THU, which="this")
    deadline_tue_through_thu = WindowExpr(phrase="Tuesday through Thursday", start=tue, end=thu)

    urgent = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=deadline_tue_through_thu,
        urgency_requested=False,
        received_at=RECEIVED_MON,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert urgent is False


def test_urgency_negative_relative_days() -> None:
    deadline_yesterday = RelativeDaysExpr(phrase="due yesterday", days=-1)

    urgent = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=deadline_yesterday,
        urgency_requested=False,
        received_at=RECEIVED_WED,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert urgent is True


def test_urgency_spam_never_urgent() -> None:
    urgent = compute_urgent(
        primary=PrimaryLabel.SPAM,
        deadline=None,
        urgency_requested=True,
        received_at=RECEIVED_TUE,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert urgent is False


# -- Year inference feeding into urgency (task 37, D-64): a stated-year-less deadline is
# resolved via the same closest-candidate rule as `core/dates.py`, before the two-day
# cutoff is applied. -----------------------------------------------------------------


def test_compute_urgent_january_deadline_in_december_not_urgent() -> None:
    """Received 20 Dec 2026, deadline "January 5" (no year stated): the closest
    candidate is 2027-01-05 (D-64), 16 days out, well past the two-day cutoff."""
    deadline = AbsoluteExpr(phrase="due January 5", month=1, day=5)

    urgent = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=deadline,
        urgency_requested=False,
        received_at=RECEIVED_DEC20,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert urgent is False


def test_compute_urgent_recent_overdue_is_urgent() -> None:
    """Received 28 Sep 2026, deadline "September 25" (no year stated): the closest
    candidate is 2026-09-25, already 3 days overdue."""
    deadline = AbsoluteExpr(phrase="due September 25", month=9, day=25)

    urgent = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=deadline,
        urgency_requested=False,
        received_at=RECEIVED_MON,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert urgent is True


# -- compute_urgency (task 47, D-24): `urgent` plus the resolved `deadline_at`, resolved --
# -- once, the same instant `Classification.deadline_at` reuses. ------------------------


def test_compute_urgency_returns_resolved_deadline_and_urgent() -> None:
    deadline = WeekdayExpr(phrase="Thursday", weekday=Weekday.THU, which="this")

    result = compute_urgency(
        primary=PrimaryLabel.TASK,
        deadline=deadline,
        urgency_requested=False,
        received_at=RECEIVED_TUE,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert result.urgent is True
    assert result.deadline_at == datetime(2026, 10, 1, 23, 59, 59, 999999, tzinfo=_ZONE)


def test_compute_urgency_no_deadline_returns_none() -> None:
    result = compute_urgency(
        primary=PrimaryLabel.TASK,
        deadline=None,
        urgency_requested=False,
        received_at=RECEIVED_TUE,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert result.urgent is False
    assert result.deadline_at is None


def test_compute_urgent_boundary_two_days_is_urgent() -> None:
    """Received 20 Dec 2026, deadline "December 22" (no year stated): the closest
    candidate is 2026-12-22, exactly the inclusive two-day cutoff."""
    deadline = AbsoluteExpr(phrase="due December 22", month=12, day=22)

    urgent = compute_urgent(
        primary=PrimaryLabel.TASK,
        deadline=deadline,
        urgency_requested=False,
        received_at=RECEIVED_DEC20,
        timezone=BUCHAREST,
        business_hours_start=BUSINESS_HOURS_START,
        business_hours_end=BUSINESS_HOURS_END,
    )

    assert urgent is True

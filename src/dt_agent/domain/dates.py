"""Date-expression schema: the LLM's meeting-extraction output (D-64, D-66).

The LLM restates what the sender wrote; it never computes a calendar
date itself (D-64). `core/dates.py` (task 7) resolves each expression
into an actual `AwareDatetime`, given the email's received time and the
resolved timezone. Every expression keeps the original `phrase` so the
approval email can show it next to the computed time.

`kind` is a discriminator: an unrecognized kind (e.g. `"someday"`) fails
validation instead of silently becoming `none`.
"""

from datetime import time as _time
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Imported as `_time`, not `time`: several fields below are named `time`,
# and Python 3.14's deferred annotation evaluation resolves a bare `time`
# annotation against the class's own namespace first, which would shadow
# the module-level import with the field's own (unset) value.


class Weekday(StrEnum):
    """ISO weekday names, Monday first."""

    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"
    SAT = "SAT"
    SUN = "SUN"


class _DateExpressionBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    phrase: str


class AbsoluteExpr(_DateExpressionBase):
    """ "5 October at 14:00" — a calendar date, year optional.

    A missing `year` is never filled in here: `core/dates.py` (D-64)
    infers it deterministically from the email's received date.
    """

    kind: Literal["absolute"] = "absolute"
    year: int | None = Field(
        default=None,
        description="The year only if the sender actually wrote one; never infer or assume a year.",
    )
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    time: _time | None = None


class RelativeDaysExpr(_DateExpressionBase):
    """ "today" / "tomorrow" / "in 3 days" — an offset from the received day.

    `days` may be negative, but only for a past deadline ("due
    yesterday" = -1, D-24, D-64); a proposed meeting time is never in
    the past, so this is a convention the LLM follows, not a schema
    constraint.
    """

    kind: Literal["relative_days"] = "relative_days"
    days: int
    time: _time | None = None


class WeekdayExpr(_DateExpressionBase):
    """ "Friday" / "this Friday" / "next Tuesday"."""

    kind: Literal["weekday"] = "weekday"
    weekday: Weekday
    which: Literal["this", "next"]
    time: _time | None = None


# A window's start/end bound is one concrete point expression; it can't
# itself be a window or "none".
WindowBound = Annotated[
    AbsoluteExpr | RelativeDaysExpr | WeekdayExpr,
    Field(discriminator="kind"),
]


class WindowExpr(_DateExpressionBase):
    """ "sometime next week" / "next Wednesday morning".

    Either `preset` (`this_week` / `next_week`) or at least one of
    `start`/`end` is given; a single bound resolves to a single-day
    window (`core/dates.py`, task 39: the missing bound mirrors the given
    one). `part_of_day` records the sender's own word for a part of
    the day ("morning"/"afternoon"/"evening"), if any; mapping it to
    actual clock times against the configured business hours is
    deterministic code's job (`core/dates.py`, D-64), never the LLM's.
    `start_time`/`end_time` narrow the window further, but only to a
    clock time the sender actually wrote (e.g. "between 2 and 4pm"); they
    win over `part_of_day` where both are given.
    """

    kind: Literal["window"] = "window"
    preset: Literal["this_week", "next_week"] | None = None
    start: WindowBound | None = None
    end: WindowBound | None = None
    part_of_day: Literal["morning", "afternoon", "evening"] | None = Field(
        default=None,
        description=(
            "The sender's own word for part of a day, if any; never convert it to clock times."
        ),
    )
    start_time: _time | None = Field(
        default=None,
        description=(
            "A clock time the sender actually wrote for the window's start (e.g. "
            "'between 2 and 4pm'); never a time inferred from part_of_day."
        ),
    )
    end_time: _time | None = Field(
        default=None,
        description=(
            "A clock time the sender actually wrote for the window's end (e.g. "
            "'between 2 and 4pm'); never a time inferred from part_of_day."
        ),
    )


class NoneExpr(_DateExpressionBase):
    """No time mentioned, or a phrase that fits no other kind (D-64)."""

    kind: Literal["none"] = "none"


DateExpression = Annotated[
    AbsoluteExpr | RelativeDaysExpr | WeekdayExpr | WindowExpr | NoneExpr,
    Field(discriminator="kind"),
]

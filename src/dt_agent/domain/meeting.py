"""Meeting extraction output and scheduling slot data (ADR-4, D-64).

`MeetingExtraction` is the `MeetingExtractor` port's (PydanticAI)
`output_type` (ADR-2, D-64): the LLM restates what it read; it never
picks a slot or checks the calendar. `ResolvedSlot` and `SlotOption`
are plain data produced by later, deterministic code (`core/dates.py`,
`core/scheduling.py`, `core/approval_email.py`).
"""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from dt_agent.domain.dates import DateExpression


class MeetingExtraction(BaseModel):
    """What the LLM extracted from one meeting-request email (D-64)."""

    model_config = ConfigDict(frozen=True)

    title: str | None = None
    proposals: list[DateExpression] = []
    duration_minutes: int | None = None
    timezone: str | None = None
    timezone_stated: bool = False
    extra_attendees: list[str] = []
    location: str | None = None


class ResolvedSlot(BaseModel):
    """One proposal, turned into an actual start/end by `core/dates.py`."""

    model_config = ConfigDict(frozen=True)

    start: AwareDatetime
    end: AwareDatetime
    phrase: str
    source: str


class SlotMark(StrEnum):
    """How a `SlotOption` is shown in the approval email (ADR-4, D-13, D-14, D-45)."""

    FREE = "free"
    DOUBLE_BOOKING = "double_booking"
    OUTSIDE_BUSINESS_HOURS = "outside_business_hours"
    OUTSIDE_REQUESTED_INTERVAL = "outside_requested_interval"
    INVALID = "invalid"


class SlotOption(BaseModel):
    """One numbered option in an approval email (D-13, D-14)."""

    model_config = ConfigDict(frozen=True)

    number: int
    start: AwareDatetime
    end: AwareDatetime
    phrase: str
    mark: SlotMark
    conflicts: tuple[str, ...] = ()

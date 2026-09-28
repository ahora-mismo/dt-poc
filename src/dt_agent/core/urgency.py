"""Urgency by deadline and requested urgency (task 31, D-24).

The LLM restates a deadline as a `DateExpression` and its own reading of
whether the sender asked for speed (`urgency_requested`); it computes no
dates and makes no urgency decision. This module — pure, no I/O — is the
code that decides `urgent`, per D-24:

    urgent = urgency_requested or deadline <= end of the day two days
             after the received day

Days count from the received local day: received on Tuesday, a deadline
up to the end of Thursday (Thursday included) is urgent; Friday or later
is not. `SPAM` is never urgent (D-24, D-25), checked first so an
LLM-returned `urgency_requested: true` on a spam email never overrides
it.

`resolve_deadline` (task 47) is the deadline resolution on its own, so a
caller that also needs the resolved instant (`Classification.deadline_at`,
D-24) doesn't have to re-derive it; `compute_urgency` calls it once and
returns both `urgent` and `deadline_at` together, and `compute_urgent`
(existing callers) is a thin wrapper over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from dt_agent.core.dates import resolve_expressions
from dt_agent.domain.dates import DateExpression
from dt_agent.domain.labels import PrimaryLabel
from dt_agent.domain.meeting import ResolvedSlot

_END_OF_DAY = time(23, 59, 59, 999999)
_NO_DURATION = timedelta(0)


def resolve_deadline(
    deadline: DateExpression | None,
    *,
    received_at: datetime,
    timezone: str,
    business_hours_start: int,
    business_hours_end: int,
) -> datetime | None:
    """Resolve `deadline` to its overall end instant via the shared date resolver
    (D-24, D-64), or `None` when there's no deadline, or it never resolves to a time at
    all (`NoTimeResolution`/`InvalidResolution`, D-66).

    A window deadline may resolve to several `ResolvedSlot`s, one per business day it
    spans (task 39, F1); D-24's "deadline" always means the window's *overall* end, so
    this uses the latest of them, never just the first. `business_hours_start`/
    `business_hours_end` (task 37) are threaded into the shared date resolver; a
    deadline is never a `part_of_day` window in practice, but `resolve_expressions`
    needs them regardless.

    Task 47: `Classification.deadline_at` and `compute_urgent`'s cutoff check both come
    from this one function, so the two are always the same resolved instant.
    """
    if deadline is None:
        return None
    resolutions = resolve_expressions(
        [deadline],
        received_at,
        timezone,
        _NO_DURATION,
        business_hours_start=business_hours_start,
        business_hours_end=business_hours_end,
    )
    slots = [r for r in resolutions if isinstance(r, ResolvedSlot)]
    if not slots:
        return None
    return max(slot.end for slot in slots)


@dataclass(frozen=True, slots=True)
class Urgency:
    """`compute_urgency`'s result (task 47): the decision plus the resolved deadline it
    was based on, so a caller (the classifier) never has to resolve it a second time to
    get `Classification.deadline_at`."""

    urgent: bool
    deadline_at: datetime | None


def compute_urgency(
    primary: PrimaryLabel,
    deadline: DateExpression | None,
    urgency_requested: bool,
    *,
    received_at: datetime,
    timezone: str,
    business_hours_start: int,
    business_hours_end: int,
) -> Urgency:
    """Decide `urgent` and resolve `deadline_at` together (D-24), resolving `deadline`
    exactly once via `resolve_deadline`. Pure; no I/O."""
    deadline_at = resolve_deadline(
        deadline,
        received_at=received_at,
        timezone=timezone,
        business_hours_start=business_hours_start,
        business_hours_end=business_hours_end,
    )
    if primary is PrimaryLabel.SPAM:
        return Urgency(urgent=False, deadline_at=deadline_at)
    if urgency_requested:
        return Urgency(urgent=True, deadline_at=deadline_at)
    if deadline_at is None:
        return Urgency(urgent=False, deadline_at=None)

    zone = ZoneInfo(timezone)
    received_local_date = received_at.astimezone(zone).date()
    cutoff_date = received_local_date + timedelta(days=2)
    cutoff = datetime.combine(cutoff_date, _END_OF_DAY, tzinfo=zone)
    return Urgency(urgent=deadline_at <= cutoff, deadline_at=deadline_at)


def compute_urgent(
    primary: PrimaryLabel,
    deadline: DateExpression | None,
    urgency_requested: bool,
    *,
    received_at: datetime,
    timezone: str,
    business_hours_start: int,
    business_hours_end: int,
) -> bool:
    """`urgent` alone (D-24); existing callers that don't need `deadline_at` too.
    See `compute_urgency` for the full result."""
    return compute_urgency(
        primary,
        deadline,
        urgency_requested,
        received_at=received_at,
        timezone=timezone,
        business_hours_start=business_hours_start,
        business_hours_end=business_hours_end,
    ).urgent

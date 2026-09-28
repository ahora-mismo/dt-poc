"""Ports: typed, provider-agnostic interfaces between the pipeline and the outside world.

Every port is a `typing.Protocol` of async methods (ADR-2), implemented by
an adapter (`adapters/`) for production and by a hand-written fake
(`tests/fakes/`) for unit tests (D-52).
"""

from dt_agent.ports.calendar import BusyInterval, CalendarPort, InvitationEvent, NewEvent
from dt_agent.ports.errors import CursorExpiredError, LLMOutputError, MessageNotFoundError
from dt_agent.ports.facts import Facts
from dt_agent.ports.llm import (
    AcknowledgementDrafter,
    Classifier,
    CourtesyDraft,
    CourtesyDrafter,
    MeetingExtractor,
    ReplyDrafter,
)
from dt_agent.ports.mail import HistoryPage, MailPort, OutgoingMessage
from dt_agent.ports.sleep import Sleeper
from dt_agent.ports.store import MessageStore

__all__ = [
    "AcknowledgementDrafter",
    "BusyInterval",
    "CalendarPort",
    "Classifier",
    "CourtesyDraft",
    "CourtesyDrafter",
    "CursorExpiredError",
    "Facts",
    "HistoryPage",
    "InvitationEvent",
    "LLMOutputError",
    "MailPort",
    "MeetingExtractor",
    "MessageNotFoundError",
    "MessageStore",
    "NewEvent",
    "OutgoingMessage",
    "ReplyDrafter",
    "Sleeper",
]

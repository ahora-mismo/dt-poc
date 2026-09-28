"""Shared dependencies of the per-email flow and the approval handler.

One `PipelineDeps` is built at startup per worker (each worker has its own
Google client instances, D-30) and passed to `process_email` and to the
approval handler. Tests build it from fakes (D-52).
"""

from __future__ import annotations

from dataclasses import dataclass

from dt_agent.config import Settings
from dt_agent.domain.clock import Clock
from dt_agent.ports import (
    AcknowledgementDrafter,
    CalendarPort,
    Classifier,
    CourtesyDrafter,
    MailPort,
    MeetingExtractor,
    MessageStore,
    ReplyDrafter,
)


@dataclass(frozen=True, slots=True)
class PipelineDeps:
    settings: Settings
    mailbox_address: str
    """The monitored mailbox's own address, read from `users.getProfile` (D-18, D-65)."""
    mail: MailPort
    calendar: CalendarPort
    store: MessageStore
    classifier: Classifier
    meeting_extractor: MeetingExtractor
    reply_drafter: ReplyDrafter
    courtesy_drafter: CourtesyDrafter
    acknowledgement_drafter: AcknowledgementDrafter
    clock: Clock

    @property
    def approver_address(self) -> str:
        """Where approval and notification emails go (D-46)."""
        return self.settings.approver_address(self.mailbox_address)

    @property
    def signing_key(self) -> str:
        return self.settings.APPROVAL_SIGNING_KEY.get_secret_value()

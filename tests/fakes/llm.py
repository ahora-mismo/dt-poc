"""In-memory fakes for the LLM ports (D-52).

Canned results are configured per email id with `set_result` (falling
back to the constructor's `default`); `calls` records every invocation
for assertions; `fail_next(exc)` makes the next call raise instead
(used for `LLMOutputError` -> `AI/NeedsReview`, D-43).
"""

from dt_agent.domain.classification import Classification
from dt_agent.domain.email import Email
from dt_agent.domain.labels import PrimaryLabel
from dt_agent.domain.meeting import MeetingExtraction
from dt_agent.ports.errors import LLMOutputError
from dt_agent.ports.facts import Facts
from dt_agent.ports.llm import (
    AcknowledgementDrafter,
    Classifier,
    CourtesyDraft,
    CourtesyDrafter,
    MeetingExtractor,
    ReplyDrafter,
)


class FakeClassifier:
    def __init__(self, default: Classification | None = None) -> None:
        self._default = default
        self._by_id: dict[str, Classification] = {}
        self._pending_exc: Exception | None = None
        self.calls: list[tuple[Email, Facts]] = []

    def set_result(self, email_id: str, result: Classification) -> None:
        self._by_id[email_id] = result

    def fail_next(self, exc: Exception) -> None:
        self._pending_exc = exc

    async def classify(self, email: Email, facts: Facts) -> Classification:
        if self._pending_exc is not None:
            exc, self._pending_exc = self._pending_exc, None
            raise exc
        self.calls.append((email, facts))
        result = self._by_id.get(email.id, self._default)
        if result is None:
            raise LLMOutputError(f"FakeClassifier has no canned result for {email.id!r}")
        return result


class FakeMeetingExtractor:
    def __init__(self, default: MeetingExtraction | None = None) -> None:
        self._default = default
        self._by_id: dict[str, MeetingExtraction] = {}
        self._pending_exc: Exception | None = None
        self.calls: list[Email] = []

    def set_result(self, email_id: str, result: MeetingExtraction) -> None:
        self._by_id[email_id] = result

    def fail_next(self, exc: Exception) -> None:
        self._pending_exc = exc

    async def extract(self, email: Email) -> MeetingExtraction:
        if self._pending_exc is not None:
            exc, self._pending_exc = self._pending_exc, None
            raise exc
        self.calls.append(email)
        result = self._by_id.get(email.id, self._default)
        if result is None:
            raise LLMOutputError(f"FakeMeetingExtractor has no canned result for {email.id!r}")
        return result


class FakeReplyDrafter:
    def __init__(self, default: str | None = None) -> None:
        self._default = default
        self._by_id: dict[str, str] = {}
        self._pending_exc: Exception | None = None
        self.calls: list[Email] = []

    def set_result(self, email_id: str, body: str) -> None:
        self._by_id[email_id] = body

    def fail_next(self, exc: Exception) -> None:
        self._pending_exc = exc

    async def draft_sales_decline(self, email: Email) -> str:
        if self._pending_exc is not None:
            exc, self._pending_exc = self._pending_exc, None
            raise exc
        self.calls.append(email)
        result = self._by_id.get(email.id, self._default)
        if result is None:
            raise LLMOutputError(f"FakeReplyDrafter has no canned result for {email.id!r}")
        return result


class FakeCourtesyDrafter:
    def __init__(self, default: CourtesyDraft | None = None) -> None:
        self._default = default
        self._by_id: dict[str, CourtesyDraft] = {}
        self._pending_exc: Exception | None = None
        self.calls: list[tuple[Email, PrimaryLabel]] = []

    def set_result(self, email_id: str, result: CourtesyDraft) -> None:
        self._by_id[email_id] = result

    def fail_next(self, exc: Exception) -> None:
        self._pending_exc = exc

    async def draft(self, email: Email, label: PrimaryLabel) -> CourtesyDraft:
        if self._pending_exc is not None:
            exc, self._pending_exc = self._pending_exc, None
            raise exc
        self.calls.append((email, label))
        result = self._by_id.get(email.id, self._default)
        if result is None:
            raise LLMOutputError(f"FakeCourtesyDrafter has no canned result for {email.id!r}")
        return result


class FakeAcknowledgementDrafter:
    def __init__(self, default: str | None = None) -> None:
        self._default = default
        self._by_id: dict[str, str] = {}
        self._pending_exc: Exception | None = None
        self.calls: list[tuple[Email, str]] = []

    def set_result(self, email_id: str, body: str) -> None:
        self._by_id[email_id] = body

    def fail_next(self, exc: Exception) -> None:
        self._pending_exc = exc

    async def draft_acknowledgement(self, email: Email, hint: str) -> str:
        if self._pending_exc is not None:
            exc, self._pending_exc = self._pending_exc, None
            raise exc
        self.calls.append((email, hint))
        result = self._by_id.get(email.id, self._default)
        if result is None:
            raise LLMOutputError(
                f"FakeAcknowledgementDrafter has no canned result for {email.id!r}"
            )
        return result


# Structural checks, not tests: see tests/fakes/mail.py's `_mail_port_check`.
_classifier_check: Classifier = FakeClassifier()
_meeting_extractor_check: MeetingExtractor = FakeMeetingExtractor()
_reply_drafter_check: ReplyDrafter = FakeReplyDrafter()
_courtesy_drafter_check: CourtesyDrafter = FakeCourtesyDrafter()
_acknowledgement_drafter_check: AcknowledgementDrafter = FakeAcknowledgementDrafter()

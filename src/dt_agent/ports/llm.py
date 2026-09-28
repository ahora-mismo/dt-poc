"""LLM ports (ADR-2, D-24, D-38, D-39, D-40, D-63, D-64).

Implemented by `adapters/llm/*.py` (task 17) over PydanticAI, and by the
fakes in `tests/fakes/llm.py`. No port here calls another port: per
ADR-2, the LLM only reads and returns typed data, never a side-effecting
tool call.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from dt_agent.domain.classification import Classification
from dt_agent.domain.email import Email
from dt_agent.domain.labels import PrimaryLabel
from dt_agent.domain.meeting import MeetingExtraction
from dt_agent.ports.facts import Facts


class Classifier(Protocol):
    """Classifies one email into a `Classification` (D-24), given code-extracted `Facts` (D-44)."""

    async def classify(self, email: Email, facts: Facts) -> Classification:
        """Raises `LLMOutputError` if the output never validates (D-43)."""
        ...


class MeetingExtractor(Protocol):
    """Extracts meeting details as date expressions (D-64); never computes a calendar date."""

    async def extract(self, email: Email) -> MeetingExtraction:
        """Raises `LLMOutputError` if the output never validates (D-43)."""
        ...


class ReplyDrafter(Protocol):
    """Drafts the sales-outreach decline body (D-38)."""

    async def draft_sales_decline(self, email: Email) -> str:
        """The reply body only; the quoted original and signature are added by code."""
        ...


class CourtesyDraft(BaseModel):
    """One courtesy-reply decision (D-39)."""

    model_config = ConfigDict(frozen=True)

    warranted: bool
    reason: str
    body: str


class CourtesyDrafter(Protocol):
    """Decides whether a courtesy acknowledgement is warranted, and drafts it (D-39)."""

    async def draft(self, email: Email, label: PrimaryLabel) -> CourtesyDraft:
        """Raises `LLMOutputError` if the output never validates (D-43)."""
        ...


class AcknowledgementDrafter(Protocol):
    """Drafts the urgent-internal acknowledgement body (D-40); a dedicated port, not the
    D-39 courtesy decision -- the flow has already decided the reply is required, so the
    model never decides "warranted" here."""

    async def draft_acknowledgement(self, email: Email, hint: str) -> str:
        """The reply body only; the quoted original and signature are added by code.

        Raises `LLMOutputError` if the output never validates (D-43).
        """
        ...

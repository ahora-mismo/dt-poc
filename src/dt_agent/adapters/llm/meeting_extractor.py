"""`MeetingExtractor` port implementation over a PydanticAI `Agent` (ADR-4, D-64).

The address guardrail below is the adapter half of ADR-4's rule: the LLM
may propose "extra attendees" (people the email asks to invite beyond the
sender), but any address it returns that isn't present in the email's own
From/To/Cc headers is dropped here, before the extraction ever reaches the
scheduling code. The sender itself and the approver address are *not*
added here -- that's the pipeline's job (`core/scheduling.py` /
`core/approval_email.py`, later tasks), not this adapter's.
"""

from __future__ import annotations

import structlog
from pydantic_ai import Agent
from pydantic_ai.models import Model

from dt_agent.config import Settings
from dt_agent.domain.email import Email
from dt_agent.domain.meeting import MeetingExtraction
from dt_agent.logging import get_logger

from ._prompting import load_prompt, render_untrusted_email_section, run_typed
from .providers import OUTPUT_VALIDATION_RETRIES, model_settings_for


def _allowed_addresses(email: Email) -> set[str]:
    """Every address the email's own From/To/Cc headers actually carry (ADR-4)."""
    addresses = {email.from_address.address}
    addresses.update(address.address for address in email.to)
    addresses.update(address.address for address in email.cc)
    return addresses


def _apply_attendee_guardrail(
    extraction: MeetingExtraction, email: Email, logger: structlog.stdlib.BoundLogger
) -> MeetingExtraction:
    allowed = _allowed_addresses(email)
    kept: list[str] = []
    dropped: list[str] = []
    for address in extraction.extra_attendees:
        normalized = address.strip().lower()
        if normalized in allowed:
            # Canonical lowercase, not the LLM's casing: downstream code
            # de-dups attendees against organizer/approver by address
            # (ADR-4), which only works if every address it compares is
            # already normalized the same way `EmailAddress` normalizes
            # `email`'s own From/To/Cc.
            kept.append(normalized)
        else:
            dropped.append(address)
    if dropped:
        logger.warning(
            "llm.extractor.address_guardrail_dropped",
            message_id=email.id,
            dropped=dropped,
        )
    if len(kept) == len(extraction.extra_attendees):
        return extraction
    return extraction.model_copy(update={"extra_attendees": kept})


class LLMMeetingExtractor:
    """Extracts meeting details as date expressions (D-64); never computes a calendar date."""

    def __init__(
        self,
        model: Model,
        settings: Settings,
        *,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._model = model
        self._logger = logger or get_logger(__name__)
        self._agent: Agent[None, MeetingExtraction] = Agent(
            model,
            output_type=MeetingExtraction,
            instructions=load_prompt("extract_meeting"),
            model_settings=model_settings_for(settings, "extract"),
            retries={"output": OUTPUT_VALIDATION_RETRIES},
        )

    async def extract(self, email: Email) -> MeetingExtraction:
        """Raises `LLMOutputError` if the output never validates (D-43)."""
        user_prompt = render_untrusted_email_section(email, include_recipients=True)
        extraction = await run_typed(
            self._agent, user_prompt, model=self._model, task="extract", logger=self._logger
        )
        return _apply_attendee_guardrail(extraction, email, self._logger)

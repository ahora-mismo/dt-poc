"""`Classifier` port over a PydanticAI `Agent` (ADR-2, D-24, D-41, D-44, D-63, D-64)."""

from __future__ import annotations

import structlog
from pydantic_ai import Agent
from pydantic_ai.models import Model

from dt_agent.config import Settings
from dt_agent.core.urgency import compute_urgency
from dt_agent.domain.classification import Classification, ClassifierOutput
from dt_agent.domain.email import Email
from dt_agent.logging import get_logger
from dt_agent.ports.facts import Facts

from ._prompting import (
    load_prompt,
    render_trusted_facts_section,
    render_untrusted_email_section,
    run_typed,
)
from .providers import OUTPUT_VALIDATION_RETRIES, model_settings_for


class LLMClassifier:
    """Classifies one email, given code-extracted `Facts`, into a `Classification` (D-24, D-44).

    The LLM's own output is `ClassifierOutput` (`primary`, `subtype`,
    `deadline`, `urgency_requested`, D-24, D-64). `urgent` is computed by code afterwards
    (`core/urgency.py`, task 31), never by the LLM.
    """

    def __init__(
        self,
        model: Model,
        settings: Settings,
        *,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._model = model
        self._timezone = settings.DEFAULT_TIMEZONE
        self._business_hours_start = settings.BUSINESS_HOURS_START
        self._business_hours_end = settings.BUSINESS_HOURS_END
        self._logger = logger or get_logger(__name__)
        self._agent: Agent[None, ClassifierOutput] = Agent(
            model,
            output_type=ClassifierOutput,
            instructions=load_prompt("classify"),
            model_settings=model_settings_for(settings, "classify"),
            retries={"output": OUTPUT_VALIDATION_RETRIES},
        )

    async def classify(self, email: Email, facts: Facts) -> Classification:
        """Raises `LLMOutputError` if the output never validates (D-43)."""
        _, classification = await self.classify_with_output(email, facts)
        return classification

    async def classify_with_output(
        self, email: Email, facts: Facts
    ) -> tuple[ClassifierOutput, Classification]:
        """Like `classify`, but also returns the LLM's raw `ClassifierOutput`.

        Not part of the `Classifier` port (which only ever returns
        `Classification`, D-24): the eval runner (task 32, D-51) needs the
        raw `deadline`/`urgency_requested` fields to check them separately
        from the code-computed `urgent`, without duplicating this call.
        Raises `LLMOutputError` if the output never validates (D-43).
        """
        user_prompt = (
            render_trusted_facts_section(facts)
            + "\n\n"
            + render_untrusted_email_section(email, include_recipients=True)
        )
        output = await run_typed(
            self._agent, user_prompt, model=self._model, task="classify", logger=self._logger
        )
        self._logger.debug(
            "classify.deadline",
            deadline_kind=output.deadline.kind if output.deadline is not None else None,
            urgency_requested=output.urgency_requested,
        )
        urgency = compute_urgency(
            output.primary,
            output.deadline,
            output.urgency_requested,
            received_at=email.received_at,
            timezone=self._timezone,
            business_hours_start=self._business_hours_start,
            business_hours_end=self._business_hours_end,
        )
        # D-24 (task 47): `deadline_phrase` is kept whenever the LLM restated a deadline
        # expression at all, even one `urgency.deadline_at` couldn't resolve to a time.
        deadline_phrase = output.deadline.phrase if output.deadline is not None else None
        classification = Classification(
            primary=output.primary,
            urgent=urgency.urgent,
            subtype=output.subtype,
            deadline_phrase=deadline_phrase,
            deadline_at=urgency.deadline_at,
        )
        return output, classification

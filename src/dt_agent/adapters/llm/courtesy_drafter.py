"""`CourtesyDrafter` port implementation: warranted/reason/body decision (D-39)."""

from __future__ import annotations

import structlog
from pydantic_ai import Agent
from pydantic_ai.models import Model

from dt_agent.config import Settings
from dt_agent.domain.email import Email
from dt_agent.domain.labels import PrimaryLabel
from dt_agent.logging import get_logger
from dt_agent.ports.llm import CourtesyDraft

from ._prompting import (
    TRUSTED_BEGIN,
    TRUSTED_END,
    load_prompt,
    render_untrusted_email_section,
    run_typed,
)
from .providers import OUTPUT_VALIDATION_RETRIES, model_settings_for


def _render_context_section(label: PrimaryLabel) -> str:
    """The TRUSTED section: the label the email already got from classification."""
    return f"{TRUSTED_BEGIN}\nPrimary label: {label.value}\n{TRUSTED_END}"


class LLMCourtesyDrafter:
    """Decides whether a courtesy acknowledgement is warranted, and drafts it (D-39)."""

    def __init__(
        self,
        model: Model,
        settings: Settings,
        *,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._model = model
        self._logger = logger or get_logger(__name__)
        self._agent: Agent[None, CourtesyDraft] = Agent(
            model,
            output_type=CourtesyDraft,
            instructions=load_prompt("courtesy_reply"),
            model_settings=model_settings_for(settings, "draft"),
            retries={"output": OUTPUT_VALIDATION_RETRIES},
        )

    async def draft(self, email: Email, label: PrimaryLabel) -> CourtesyDraft:
        """Raises `LLMOutputError` if the output never validates (D-43)."""
        user_prompt = (
            _render_context_section(label)
            + "\n\n"
            + render_untrusted_email_section(email, include_recipients=False)
        )
        return await run_typed(
            self._agent,
            user_prompt,
            model=self._model,
            task="draft_courtesy_reply",
            logger=self._logger,
        )

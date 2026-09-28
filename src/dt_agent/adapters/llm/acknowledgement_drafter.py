"""`AcknowledgementDrafter` port implementation: the urgent-internal reply body (D-40)."""

from __future__ import annotations

import structlog
from pydantic_ai import Agent
from pydantic_ai.models import Model

from dt_agent.config import Settings
from dt_agent.domain.email import Email
from dt_agent.logging import get_logger

from ._prompting import (
    TRUSTED_BEGIN,
    TRUSTED_END,
    load_prompt,
    render_untrusted_email_section,
    run_typed,
)
from .providers import OUTPUT_VALIDATION_RETRIES, model_settings_for


def _render_hint_section(hint: str) -> str:
    """The TRUSTED section: D-40's drafting hint (updated: names nobody)."""
    return f"{TRUSTED_BEGIN}\nDrafting hint: {hint}\n{TRUSTED_END}"


class LLMAcknowledgementDrafter:
    """Drafts the urgent-internal acknowledgement body (D-40); code adds signature and
    threading. The flow has already decided the reply is required -- this port never
    decides "warranted"."""

    def __init__(
        self,
        model: Model,
        settings: Settings,
        *,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._model = model
        self._logger = logger or get_logger(__name__)
        self._agent: Agent[None, str] = Agent(
            model,
            output_type=str,
            instructions=load_prompt("urgent_ack"),
            model_settings=model_settings_for(settings, "draft"),
            retries={"output": OUTPUT_VALIDATION_RETRIES},
        )

    async def draft_acknowledgement(self, email: Email, hint: str) -> str:
        """The reply body only. D-40: two sentences, no commitment, no question, no
        time promise."""
        user_prompt = (
            _render_hint_section(hint)
            + "\n\n"
            + render_untrusted_email_section(email, include_recipients=False)
        )
        return await run_typed(
            self._agent,
            user_prompt,
            model=self._model,
            task="draft_acknowledgement",
            logger=self._logger,
        )

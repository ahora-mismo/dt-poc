"""`ReplyDrafter` port implementation: the sales-outreach decline body (D-38)."""

from __future__ import annotations

import structlog
from pydantic_ai import Agent
from pydantic_ai.models import Model

from dt_agent.config import Settings
from dt_agent.domain.email import Email
from dt_agent.logging import get_logger

from ._prompting import load_prompt, render_untrusted_email_section, run_typed
from .providers import OUTPUT_VALIDATION_RETRIES, model_settings_for


class LLMReplyDrafter:
    """Drafts the sales-outreach decline body (D-38); code adds the quote and signature."""

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
            instructions=load_prompt("sales_decline"),
            model_settings=model_settings_for(settings, "draft"),
            retries={"output": OUTPUT_VALIDATION_RETRIES},
        )

    async def draft_sales_decline(self, email: Email) -> str:
        """The reply body only. D-38: the LLM sees only this email's subject, sender and body."""
        user_prompt = render_untrusted_email_section(email, include_recipients=False)
        return await run_typed(
            self._agent,
            user_prompt,
            model=self._model,
            task="draft_sales_decline",
            logger=self._logger,
        )

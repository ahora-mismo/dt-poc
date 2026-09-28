"""LLM provider wiring: one `Model` and per-call `ModelSettings` (ADR-2, D-36).

`build_model` is the *only* place a concrete PydanticAI model/provider class
is constructed; every adapter in this package receives an already-built
`Model` instance. `model_settings_for` encodes D-36's temperature rule
(0 for classification/extraction, 0.3 for drafting; no
temperature at all for Azure OpenAI, since some Azure model families
reject a non-default value). `log_startup_notice` emits the one-time
Azure "no temperature" log line D-36 asks for.
"""

from __future__ import annotations

from typing import Literal

import structlog
from pydantic import SecretStr
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.azure import AzureProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from dt_agent.config import Settings

LLMTask = Literal["classify", "extract", "draft"]

# Per D-36: a timeout per call, plus PydanticAI's own output-validation
# retries and a token cap (`UsageLimits`). `request_limit` must be at least
# `OUTPUT_VALIDATION_RETRIES + 1` (1 initial attempt + N retries), with a
# margin: otherwise `UsageLimitExceeded` fires before the retries are
# exhausted, and an invalid-output run never reaches
# `UnexpectedModelBehavior` / `LLMOutputError`. `total_tokens_limit` is
# *cumulative across every request in the run* (a retry resends the whole
# prompt), so it must cover the worst case of `request_limit` full-size
# requests, not just one: `LLM_MAX_INPUT_CHARS` (`.env.sample`: 20000
# chars, ~6-7k tokens) plus instructions/output, times up to 4 requests,
# with margin.
REQUEST_TIMEOUT_SECONDS = 30.0
OUTPUT_VALIDATION_RETRIES = 2
USAGE_LIMITS = UsageLimits(request_limit=OUTPUT_VALIDATION_RETRIES + 2, total_tokens_limit=60_000)

_TEMPERATURE_BY_TASK: dict[LLMTask, float] = {
    "classify": 0.0,
    "extract": 0.0,
    "draft": 0.3,
}


def _require(value: SecretStr | str | None, name: str) -> str:
    """Unwrap a provider-specific setting `load_settings` already guaranteed present."""
    if value is None:
        raise AssertionError(f"{name} is required for the selected LLM_PROVIDER")
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def build_model(settings: Settings) -> Model:
    """Build the one `Model` used for every LLM task, per `settings.LLM_PROVIDER` (D-36)."""
    if settings.LLM_PROVIDER == "gemini":
        api_key = _require(settings.GEMINI_API_KEY, "GEMINI_API_KEY")
        model_name = _require(settings.LLM_MODEL, "LLM_MODEL")
        return GoogleModel(model_name, provider=GoogleProvider(api_key=api_key))

    if settings.LLM_PROVIDER == "anthropic":
        api_key = _require(settings.ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
        model_name = _require(settings.LLM_MODEL, "LLM_MODEL")
        return AnthropicModel(model_name, provider=AnthropicProvider(api_key=api_key))

    # LLM_PROVIDER is a `Literal["gemini", "anthropic", "azure"]`, so this is the only case left.
    endpoint = _require(settings.AZURE_OPENAI_ENDPOINT, "AZURE_OPENAI_ENDPOINT")
    api_version = _require(settings.AZURE_OPENAI_API_VERSION, "AZURE_OPENAI_API_VERSION")
    api_key = _require(settings.AZURE_OPENAI_API_KEY, "AZURE_OPENAI_API_KEY")
    deployment = _require(settings.AZURE_OPENAI_DEPLOYMENT, "AZURE_OPENAI_DEPLOYMENT")
    provider = AzureProvider(azure_endpoint=endpoint, api_version=api_version, api_key=api_key)
    return OpenAIChatModel(deployment, provider=provider)


def model_settings_for(settings: Settings, task: LLMTask) -> ModelSettings:
    """Per-call `ModelSettings` for *task* (D-36): timeout always; temperature except on Azure."""
    model_settings: ModelSettings = {"timeout": REQUEST_TIMEOUT_SECONDS}
    if settings.LLM_PROVIDER != "azure":
        model_settings["temperature"] = _TEMPERATURE_BY_TASK[task]
    return model_settings


def log_startup_notice(settings: Settings, logger: structlog.stdlib.BoundLogger) -> None:
    """Log, once at startup, that Azure OpenAI calls carry no temperature (D-36)."""
    if settings.LLM_PROVIDER == "azure":
        logger.info(
            "llm.startup",
            provider=settings.LLM_PROVIDER,
            note="temperature is not sent to Azure OpenAI; the deployment's default is used",
        )

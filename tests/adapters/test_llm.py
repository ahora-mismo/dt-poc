"""Tests for `adapters/llm/*` (task 17: ADR-2, D-24, D-36, D-38, D-39, D-41, D-44, D-63, D-64).

Per the testing rules: only `TestModel` / `FunctionModel` (never a real
model, `ALLOW_MODEL_REQUESTS=False` globally, `tests/conftest.py`);
assertions are on structure (marked sections present, keys present/absent,
addresses kept/dropped), never on prompt wording.

Test description format: `test_<unit>_<condition>_<expected>`.
"""

from __future__ import annotations

import io
import json
import logging as std_logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from dt_agent.adapters.llm import providers
from dt_agent.adapters.llm._prompting import (
    TRUSTED_BEGIN,
    TRUSTED_END,
    UNTRUSTED_BEGIN,
    UNTRUSTED_END,
)
from dt_agent.adapters.llm.acknowledgement_drafter import LLMAcknowledgementDrafter
from dt_agent.adapters.llm.classifier import LLMClassifier
from dt_agent.adapters.llm.meeting_extractor import LLMMeetingExtractor
from dt_agent.config import Settings, load_settings
from dt_agent.domain.email import EmailAddress
from dt_agent.domain.labels import PrimaryLabel
from dt_agent.logging import configure_logging, get_logger
from dt_agent.ports.errors import LLMOutputError
from dt_agent.ports.facts import Facts
from tests.fakes.emails import make_email

# A minimal, fully valid environment per provider (mirrors tests/unit/test_config.py's
# _BASE_ENV shape, one variant per LLM_PROVIDER).
_COMMON_ENV: dict[str, str] = {
    "GOOGLE_CLIENT_ID": "client-id",
    "GOOGLE_CLIENT_SECRET": "client-secret",
    "GOOGLE_REFRESH_TOKEN": "refresh-token",
    "AGENT_SENDER_NAME": "Alex's Assistant",
    "MANAGER_EMAIL": "",
    "APPROVAL_SIGNING_KEY": "signing-key",
    "APPROVAL_EXPIRY_BUSINESS_DAYS": "3",
    "SALES_DECLINE_REQUIRES_APPROVAL": "false",
    "INTERNAL_DOMAINS": "",
    "SPAM_ALLOWLIST_SENDERS": "",
    "SPAM_ALLOWLIST_DOMAINS": "",
    "DEFAULT_TIMEZONE": "Europe/Bucharest",
    "BUSINESS_HOURS_START": "9",
    "BUSINESS_HOURS_END": "18",
    "DEFAULT_MEETING_MINUTES": "30",
    "SCHEDULING_HORIZON_BUSINESS_DAYS": "5",
    "MEETING_MIN_LEAD_MINUTES": "25",
    "MEETING_MAX_OPTIONS": "3",
    "POLL_INTERVAL_SECONDS": "15",
    "ACTION_DELAY_MS": "1000",
    "WORKER_COUNT": "2",
    "MAX_ATTEMPTS": "3",
    "LLM_MAX_INPUT_CHARS": "20000",
    "DB_PATH": "data/agent.db",
    "STARTUP_BACKFILL_MESSAGES": "20",
    "RETENTION_DAYS": "30",
    "MIN_LOG_LEVEL": "INFO",
    "LOG_COLOR": "auto",
    "ENABLE_MCP": "false",
}


def _settings(provider: str) -> Settings:
    env = dict(_COMMON_ENV)
    env["LLM_PROVIDER"] = provider
    if provider == "gemini":
        env["GEMINI_API_KEY"] = "gemini-key"
        env["LLM_MODEL"] = "gemini-2.5-flash"
    elif provider == "anthropic":
        env["ANTHROPIC_API_KEY"] = "anthropic-key"
        env["LLM_MODEL"] = "claude-haiku-4-5"
    elif provider == "azure":
        env["AZURE_OPENAI_ENDPOINT"] = "https://example.openai.azure.com"
        env["AZURE_OPENAI_API_KEY"] = "azure-key"
        env["AZURE_OPENAI_API_VERSION"] = "2025-01-01-preview"
        env["AZURE_OPENAI_DEPLOYMENT"] = "gpt-4o-mini"
    return load_settings(env=env)


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Any:
    """Reset stdlib logging/structlog state after each test (mirrors test_logging.py)."""
    yield
    root = std_logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(std_logging.WARNING)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _classification_response(
    info: AgentInfo,
    *,
    primary: str,
    subtype: str,
    deadline: dict[str, Any] | None = None,
    urgency_requested: bool = False,
) -> ModelResponse:
    tool_name = info.output_tools[0].name
    args = {
        "primary": primary,
        "subtype": subtype,
        "deadline": deadline,
        "urgency_requested": urgency_requested,
    }
    return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])


# -- test_llm_provider_temperature ------------------------------------------------------------


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_llm_provider_temperature_sent_for_anthropic_and_gemini(provider: str) -> None:
    settings = _settings(provider)

    model_settings = providers.model_settings_for(settings, "classify")

    assert model_settings.get("temperature") == 0.0
    assert model_settings.get("timeout") == providers.REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_llm_provider_temperature_higher_for_drafting(provider: str) -> None:
    settings = _settings(provider)

    model_settings = providers.model_settings_for(settings, "draft")

    assert model_settings.get("temperature") == 0.3


def test_llm_provider_azure_sends_no_temperature_and_logs_startup_notice() -> None:
    settings = _settings("azure")
    stream = io.StringIO()
    configure_logging(level="INFO", color="never", stream=stream)
    logger = get_logger("test")

    model_settings = providers.model_settings_for(settings, "classify")
    providers.log_startup_notice(settings, logger)

    assert "temperature" not in model_settings
    assert model_settings.get("timeout") == providers.REQUEST_TIMEOUT_SECONDS
    lines = _lines(stream)
    assert any(line["event"] == "llm.startup" and line.get("provider") == "azure" for line in lines)


# -- test_llm_classifier_separates_trusted_facts_and_untrusted_content -----------------------


async def test_llm_classifier_separates_trusted_facts_and_untrusted_content() -> None:
    captured: dict[str, str] = {}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        for part in request.parts:
            if isinstance(part, UserPromptPart):
                assert isinstance(part.content, str)
                captured["prompt"] = part.content
        return _classification_response(info, primary="TASK", subtype="none")

    model = FunctionModel(fn)
    settings = _settings("anthropic")
    classifier = LLMClassifier(model, settings)
    email = make_email(
        subject="Re: your account", text_body="Click here to verify: http://evil.example"
    )
    facts = Facts(items={"spf_pass": False, "bulk_cues": ["List-Unsubscribe header present"]})

    await classifier.classify(email, facts)

    prompt = captured["prompt"]
    trusted_start = prompt.index(TRUSTED_BEGIN)
    trusted_end = prompt.index(TRUSTED_END)
    untrusted_start = prompt.index(UNTRUSTED_BEGIN)
    untrusted_end = prompt.index(UNTRUSTED_END)

    assert trusted_start < trusted_end < untrusted_start < untrusted_end
    trusted_span = prompt[trusted_start:trusted_end]
    untrusted_span = prompt[untrusted_start:untrusted_end]
    assert "spf_pass: False" in trusted_span
    assert "List-Unsubscribe header present" in trusted_span
    assert email.text_body in untrusted_span
    # The email's own content never leaks into the trusted span, and the
    # facts never leak into the untrusted span.
    assert email.text_body not in trusted_span
    assert "spf_pass" not in untrusted_span


# -- test_llm_classifier_invalid_output_raises -----------------------------------------------


async def test_llm_classifier_invalid_output_raises() -> None:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # Plain text, never a tool call matching the Classification output
        # schema: PydanticAI retries against the validation error and then
        # gives up.
        return ModelResponse(parts=[TextPart(content="not a valid classification")])

    model = FunctionModel(fn)
    settings = _settings("anthropic")
    classifier = LLMClassifier(model, settings)
    email = make_email()
    facts = Facts()

    with pytest.raises(LLMOutputError):
        await classifier.classify(email, facts)


# -- test_llm_extractor_address_guardrail ------------------------------------------------------


async def test_llm_extractor_address_guardrail_drops_address_not_in_headers() -> None:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool_name = info.output_tools[0].name
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=tool_name,
                    args={
                        "title": "Sync",
                        "proposals": [],
                        "extra_attendees": ["anna@example.com", "hacker@evil.example"],
                    },
                )
            ]
        )

    model = FunctionModel(fn)
    settings = _settings("anthropic")
    extractor = LLMMeetingExtractor(model, settings)
    email = make_email(
        from_address="requester@example.com",
        to=("mailbox@example.com",),
        cc=(EmailAddress(name="Anna", address="anna@example.com"),),
        subject="Let's sync",
        text_body="Can we sync tomorrow? Please add Anna.",
    )

    extraction = await extractor.extract(email)

    assert extraction.extra_attendees == ["anna@example.com"]
    assert "hacker@evil.example" not in extraction.extra_attendees


# -- test_llm_usage_logged ---------------------------------------------------------------------


async def test_llm_usage_logged_with_provider_model_tokens_and_duration() -> None:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        response = _classification_response(info, primary="MARKETING", subtype="none")
        response.usage = RequestUsage(input_tokens=12, output_tokens=3)
        return response

    model = FunctionModel(fn, model_name="fn-model")
    settings = _settings("anthropic")
    stream = io.StringIO()
    configure_logging(level="DEV", color="never", stream=stream)
    logger = get_logger("test")
    classifier = LLMClassifier(model, settings, logger=logger)
    email = make_email()
    facts = Facts()

    await classifier.classify(email, facts)

    lines = _lines(stream)
    call_lines = [line for line in lines if line["event"] == "llm.call"]
    assert len(call_lines) == 1
    line = call_lines[0]
    assert line["level"] == "dev"  # D-58: LLM request lines are DEV-level
    assert line["provider"] == "function"
    assert line["model"] == "fn-model"
    assert line["input_tokens"] == 12
    assert line["output_tokens"] == 3
    assert isinstance(line["duration_ms"], int)
    assert line["duration_ms"] >= 0


# -- test_llm_classifier_urgent_computed_by_code (task 31, D-24) -----------------------------


async def test_llm_classifier_urgent_computed_by_code() -> None:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return _classification_response(
            info,
            primary="TASK",
            subtype="none",
            deadline={"kind": "relative_days", "phrase": "tomorrow", "days": 1, "time": None},
            urgency_requested=False,
        )

    model = FunctionModel(fn)
    settings = _settings("anthropic")
    classifier = LLMClassifier(model, settings)
    email = make_email()
    facts = Facts()

    classification = await classifier.classify(email, facts)

    assert classification.urgent is True


# -- test_llm_classifier_classify_with_output (task 32, D-51) --------------------------------


async def test_llm_classifier_classify_with_output_returns_raw_output_and_classification() -> None:
    """The evals need the raw `ClassifierOutput` (deadline, urgency_requested) alongside
    the `Classification` the `Classifier` port returns, to check those fields separately
    (D-51/task 32) without re-deciding urgency in the eval runner."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return _classification_response(
            info,
            primary="TASK",
            subtype="none",
            deadline={"kind": "relative_days", "phrase": "tomorrow", "days": 1, "time": None},
            urgency_requested=False,
        )

    model = FunctionModel(fn)
    settings = _settings("anthropic")
    classifier = LLMClassifier(model, settings)
    email = make_email()
    facts = Facts()

    output, classification = await classifier.classify_with_output(email, facts)

    assert output.primary is PrimaryLabel.TASK
    assert output.deadline is not None
    assert output.deadline.kind == "relative_days"
    assert output.urgency_requested is False
    assert classification.primary is PrimaryLabel.TASK
    assert classification.urgent is True


# -- test_classifier_classification_carries_resolved_deadline (task 47, D-24) ----------------


async def test_classifier_classification_carries_resolved_deadline() -> None:
    """The classification's `deadline_phrase`/`deadline_at` come from the same
    resolution `compute_urgent` uses, not just the LLM's raw `deadline` restatement."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return _classification_response(
            info,
            primary="TASK",
            subtype="none",
            deadline={"kind": "relative_days", "phrase": "tomorrow", "days": 1, "time": None},
            urgency_requested=False,
        )

    model = FunctionModel(fn)
    settings = _settings("anthropic")
    classifier = LLMClassifier(model, settings)
    email = make_email()
    facts = Facts()

    classification = await classifier.classify(email, facts)

    # `make_email()`'s default `received_at` is Mon 28 Sep 2026 10:00 UTC (13:00 EEST);
    # "tomorrow" with no time is a whole-day window, so its end is the same instant
    # `compute_urgent` bases the (also asserted) `urgent=True` decision on.
    assert classification.deadline_phrase == "tomorrow"
    assert classification.deadline_at == datetime(
        2026, 9, 29, 23, 59, 59, 999999, tzinfo=ZoneInfo("Europe/Bucharest")
    )
    assert classification.urgent is True


async def test_classifier_unresolvable_deadline_keeps_phrase_without_instant() -> None:
    """D-24 (task 47): the sender's words are kept even when the expression doesn't
    resolve to an instant (`NoneExpr`, D-64/D-66) -- only `deadline_at` is `None`."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return _classification_response(
            info,
            primary="TASK",
            subtype="none",
            deadline={"kind": "none", "phrase": "soon"},
            urgency_requested=False,
        )

    model = FunctionModel(fn)
    settings = _settings("anthropic")
    classifier = LLMClassifier(model, settings)
    email = make_email()
    facts = Facts()

    classification = await classifier.classify(email, facts)

    assert classification.deadline_phrase == "soon"
    assert classification.deadline_at is None
    assert classification.urgent is False


async def test_classifier_no_deadline_leaves_fields_none() -> None:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return _classification_response(info, primary="MARKETING", subtype="none")

    model = FunctionModel(fn)
    settings = _settings("anthropic")
    classifier = LLMClassifier(model, settings)
    email = make_email()
    facts = Facts()

    classification = await classifier.classify(email, facts)

    assert classification.deadline_phrase is None
    assert classification.deadline_at is None


# -- test_acknowledgement_drafter_returns_text (task 43, D-40) -------------------------------


async def test_acknowledgement_drafter_returns_text() -> None:
    """`LLMAcknowledgementDrafter.draft_acknowledgement` returns the drafted body text,
    given only the email and the pipeline's own hint (D-40); plain-string output, like
    `LLMReplyDrafter`."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="Got it -- flagged as urgent.")])

    model = FunctionModel(fn)
    settings = _settings("anthropic")
    drafter = LLMAcknowledgementDrafter(model, settings)
    email = make_email(subject="Need this today", text_body="Please approve the vendor contract.")

    hint = "urgent, internal sender; received and flagged as urgent"
    body = await drafter.draft_acknowledgement(email, hint)

    assert body == "Got it -- flagged as urgent."

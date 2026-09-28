"""Tests for :mod:`dt_agent.logging` (D-56, D-57, D-58).

One structlog-based logger for the whole app: JSON lines (compact, one
object per line) on a given stream, third-party stdlib logs rendered
through the same renderer, contextvar binding of message/worker ids,
secret masking, and the `email.processed` audit summary.

Test description format: `test_<unit>_<condition>_<expected>`.
"""

from __future__ import annotations

import io
import json
import logging as std_logging
import re
from typing import Any

import pytest
import structlog

from dt_agent.logging import (
    LLM_CALLS_LOGGER,
    bind_email_context,
    configure_logging,
    get_logger,
    log_email_processed,
)


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Any:
    """Every test configures logging itself; leave no state behind for the next one."""
    yield
    root = std_logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(std_logging.WARNING)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_logger_binds_message_and_worker_ids_inside_context() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", color="never", stream=stream)
    logger = get_logger("test")

    with bind_email_context("msg-1", "worker-1"):
        logger.info("inside.first")
        logger.info("inside.second")
    logger.info("outside")

    lines = _lines(stream)
    inside = [line for line in lines if line["event"] in ("inside.first", "inside.second")]
    outside = [line for line in lines if line["event"] == "outside"]
    assert len(inside) == 2
    for line in inside:
        assert line["message_id"] == "msg-1"
        assert line["worker_id"] == "worker-1"
    assert "message_id" not in outside[0]
    assert "worker_id" not in outside[0]


def test_logger_masks_secrets() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", color="never", stream=stream)
    logger = get_logger("test")

    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    logger.info(
        "secret.values",
        jwt_value=jwt,
        anthropic_key="sk-ant-abcdef1234567890",
        google_access_token="ya29.a0AfH6SMB_example_token_value",
        # Innocuous values on purpose: these must be masked by KEY, not because
        # their value happens to also look secret-shaped.
        refresh_token="plain-value",
        api_key="plain-value",
        password="plain-value",
        input_tokens=12,
        subject="Risk-assessment for the task-force",
        details={"token": "plain-value", "items": ["sk-abcdefghij", "keep-me"]},
    )

    line = _lines(stream)[0]
    # Value-shaped secrets are masked wherever they appear, regardless of key.
    assert line["jwt_value"] == "***"
    assert line["anthropic_key"] == "***"
    assert line["google_access_token"] == "***"
    # Key-shaped secrets are masked entirely, even with an innocuous value.
    assert line["refresh_token"] == "***"
    assert line["api_key"] == "***"
    assert line["password"] == "***"
    # Token *counts* are not secrets.
    assert line["input_tokens"] == 12
    # Ordinary text merely containing "sk-" as a substring is untouched.
    assert line["subject"] == "Risk-assessment for the task-force"
    # Masking recurses into nested dicts/lists.
    assert line["details"]["token"] == "***"
    assert line["details"]["items"] == ["***", "keep-me"]


def test_logger_email_processed_has_required_fields() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", color="never", stream=stream)
    logger = get_logger("test")

    log_email_processed(
        logger,
        message_id="msg-42",
        sender="alice@example.com",
        subject="Re: contract",
        primary="SALES_OUTREACH",
        urgent=True,
        actions=["sent_reply", "archived"],
        outcome="sent",
        reason="warranted",
    )

    lines = _lines(stream)
    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "email.processed"
    assert line["message_id"] == "msg-42"
    assert line["sender"] == "alice@example.com"
    assert line["subject"] == "Re: contract"
    assert line["label"] == "SALES_OUTREACH"
    assert line["urgent"] is True
    assert line["action"] == ["sent_reply", "archived"]
    assert line["outcome"] == "sent"
    assert line["reason"] == "warranted"


def test_logger_third_party_stdlib_logs_rendered_as_json() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", color="never", stream=stream)

    std_logging.getLogger("googleapiclient.discovery").warning("quota nearly exceeded")

    lines = _lines(stream)
    assert len(lines) == 1
    line = lines[0]
    assert line["level"] == "warning"
    assert line["event"] == "quota nearly exceeded"
    assert "timestamp" in line


def test_logger_timestamp_is_first_key() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", color="never", stream=stream)

    get_logger("tests").info("ours", message_id="m-1", some_field="x")
    std_logging.getLogger("googleapiclient.discovery").warning("third party")

    raw_lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(raw_lines) == 2
    for raw in raw_lines:
        assert raw.startswith('{"timestamp":')
        assert next(iter(json.loads(raw))) == "timestamp"


def test_logger_colour_auto_is_plain_when_not_tty() -> None:
    stream = io.StringIO()
    assert not stream.isatty()
    configure_logging(level="INFO", color="auto", stream=stream)
    logger = get_logger("test")

    logger.info("plain.line")

    raw = stream.getvalue()
    assert "\x1b[" not in raw
    assert json.loads(raw.splitlines()[0])["event"] == "plain.line"


def test_logger_colour_always_has_ansi_codes() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", color="always", stream=stream)
    logger = get_logger("test")

    logger.info("colour.line")

    raw = stream.getvalue()
    assert "\x1b[" in raw
    # Stripping ANSI codes must still leave valid, equivalent JSON.
    stripped = re.sub(r"\x1b\[[0-9;]*m", "", raw.splitlines()[0])
    assert json.loads(stripped)["event"] == "colour.line"


def test_logger_exception_rendered_as_traceback_text() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", color="never", stream=stream)
    logger = get_logger("test")

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("task.failed")

    line = _lines(stream)[0]
    assert line["event"] == "task.failed"
    assert line["level"] == "error"
    assert "ValueError: boom" in line["exception"]
    assert "Traceback" in line["exception"]


def test_logger_llm_request_lines_hidden_at_info() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", color="never", stream=stream)

    get_logger(LLM_CALLS_LOGGER).info("llm.call", task="classify")
    std_logging.getLogger("httpx").info('HTTP Request: POST https://x "HTTP/1.1 200 OK"')
    std_logging.getLogger("httpx2").info('HTTP Request: POST https://y "HTTP/1.1 200 OK"')
    std_logging.getLogger("google_genai.models").info("AFC is enabled with max remote calls: 10.")
    std_logging.getLogger("google_genai.models").warning("Direct use of automatic function calling")
    get_logger("tests").info("email.processed")

    assert [line["event"] for line in _lines(stream)] == ["email.processed"]


def test_logger_llm_request_lines_shown_at_dev_level() -> None:
    stream = io.StringIO()
    configure_logging(level="DEV", color="never", stream=stream)

    get_logger(LLM_CALLS_LOGGER).info("llm.call", task="classify")
    std_logging.getLogger("httpx").info('HTTP Request: POST https://x "HTTP/1.1 200 OK"')
    get_logger("tests").debug("debug.line")

    lines = _lines(stream)
    assert [line["event"] for line in lines] == [
        "llm.call",
        'HTTP Request: POST https://x "HTTP/1.1 200 OK"',
    ]
    assert [line["level"] for line in lines] == ["dev", "dev"]


def test_logger_audit_fields_follow_timestamp() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", color="never", stream=stream)

    log_email_processed(
        get_logger("tests"),
        message_id="m-1",
        sender="a@b.example",
        subject="Hi",
        primary="TASK",
        urgent=False,
        actions=["label"],
        outcome="labeled",
    )
    get_logger("tests").info("other.line", outcome="x", message_id="m-2")

    first, second = (json.loads(raw) for raw in stream.getvalue().splitlines() if raw.strip())
    assert list(first)[:4] == ["timestamp", "label", "action", "outcome"]
    assert list(second)[:2] == ["timestamp", "outcome"]

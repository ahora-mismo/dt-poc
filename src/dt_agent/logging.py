"""Unified application logger (D-56, D-57, D-58).

One structlog-based logger for the whole app. Every log line -- ours and
third-party libraries' -- is rendered by the same renderer: compact JSON,
one object per line, on stdout (or a given stream), optionally ANSI-coloured
like `jq` (syntax colouring only, no reformatting). Secrets are masked as a
safety net (D-56); nothing should be passed to the logger that needs it, but
values that look like JWTs / API keys / OAuth tokens are masked wherever they
appear in a string, and the values of keys that are clearly about a secret
(token / secret / password / api_key) are masked entirely.

This module shadows the stdlib `logging` package inside `dt_agent` on
purpose (see the ruff `A005` exception in `pyproject.toml`), which is why it
imports the real thing as `std_logging`.
"""

from __future__ import annotations

import contextlib
import json
import logging as std_logging
import re
import sys
from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any, TextIO, cast

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

# --------------------------------------------------------------------------
# The DEV level (D-58): between DEBUG and INFO. LLM request lines -- the
# per-call usage line, the LLM SDKs' HTTP request lines (`httpx`; the Gemini
# SDK uses its own copy, `httpx2`) and the Gemini SDK's own per-call notices
# (`google_genai`) -- are logged at DEV, so they are hidden while
# MIN_LOG_LEVEL is INFO or above.
# --------------------------------------------------------------------------

DEV = 15
std_logging.addLevelName(DEV, "DEV")

LLM_CALLS_LOGGER = "dt_agent.llm.calls"
_DEV_LOGGERS: tuple[str, ...] = (LLM_CALLS_LOGGER, "httpx", "httpx2", "google_genai")

# --------------------------------------------------------------------------
# Noisy third-party loggers (D-58): always kept at WARNING, regardless of
# MIN_LOG_LEVEL, so their INFO/DEBUG chatter never drowns out our own lines.
# --------------------------------------------------------------------------

_NOISY_LOGGERS: tuple[str, ...] = (
    "googleapiclient",
    "google",
    "google_auth_httplib2",
    "httpcore",
    "urllib3",
    "pydantic_ai",
    "mcp",
)

# --------------------------------------------------------------------------
# Secret masking (D-56, D-58).
# --------------------------------------------------------------------------

_MASK = "***"

# Whole key name segments (split on "_"/"-") whose value is masked entirely.
# "token" is singular on purpose: usage counts like `input_tokens` /
# `output_tokens` (plural) must survive.
_MASK_KEY_SEGMENTS = frozenset({"token", "secret", "password"})

_SECRET_VALUE_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"  # JWT: 3 base64url parts
    r"|\bsk-[A-Za-z0-9_-]{6,}"  # sk-ant-..., other sk-... provider API keys
    r"|\bya29\.[A-Za-z0-9_-]+"  # Google OAuth access token
    r"|\b1//[A-Za-z0-9_.-]+"  # Google OAuth refresh token
    r"|\bAIza[0-9A-Za-z_-]{10,}"  # Google API key
)


def _key_should_be_masked(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if any(segment in _MASK_KEY_SEGMENTS for segment in normalized.split("_")):
        return True
    # "api_key" / "apikey" / "API-Key" are one concept split across two
    # segments, so they need their own check rather than a segment match.
    return "apikey" in normalized.replace("_", "")


def _mask_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (_MASK if _key_should_be_masked(str(key)) else _mask_value(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mask_value(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub(_MASK, value)
    return value


def _mask_secrets(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    return cast(EventDict, _mask_value(dict(event_dict)))


# --------------------------------------------------------------------------
# Rendering: compact JSON, one object per line, optional jq-like colouring.
# --------------------------------------------------------------------------

_RESET = "\x1b[0m"
_COLOR_KEY = "\x1b[34m"  # blue
_COLOR_STRING = "\x1b[32m"  # green
_COLOR_NUMBER = "\x1b[33m"  # yellow
_COLOR_LITERAL = "\x1b[35m"  # magenta: true / false / null

_TOKEN_RE = re.compile(
    r'(?P<string>"(?:\\.|[^"\\])*")'
    r"|(?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"|(?P<literal>true|false|null)"
)


def _colorize_json(text: str) -> str:
    """Add ANSI colour to compact JSON `text` without reformatting it."""
    pieces: list[str] = []
    pos = 0
    for match in _TOKEN_RE.finditer(text):
        pieces.append(text[pos : match.start()])
        string_token = match.group("string")
        if string_token is not None:
            is_key = text[match.end() :].lstrip(" ").startswith(":")
            color = _COLOR_KEY if is_key else _COLOR_STRING
            pieces.append(f"{color}{string_token}{_RESET}")
        elif match.group("number") is not None:
            pieces.append(f"{_COLOR_NUMBER}{match.group('number')}{_RESET}")
        else:
            pieces.append(f"{_COLOR_LITERAL}{match.group('literal')}{_RESET}")
        pos = match.end()
    pieces.append(text[pos:])
    return "".join(pieces)


class _LevelFilter(std_logging.Filter):
    """Moves records from the DEV loggers to the DEV level, then drops every record
    below the minimum level (D-58)."""

    def __init__(self, min_level: int) -> None:
        super().__init__()
        self._min_level = min_level

    def filter(self, record: std_logging.LogRecord) -> bool:
        if any(record.name == name or record.name.startswith(f"{name}.") for name in _DEV_LOGGERS):
            record.levelno, record.levelname = DEV, "DEV"
            if isinstance(record.msg, dict):  # a structlog record: its level is already set
                record.msg["level"] = "dev"
        return record.levelno >= self._min_level


_LEADING_AUDIT_KEYS: tuple[str, ...] = ("label", "action", "outcome")


def _render_json(colorize: bool) -> Processor:
    def render(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> str:
        # Every line starts with the timestamp, then the audit fields when present
        # (D-58: label, action, outcome), then everything else.
        ordered = {"timestamp": event_dict.pop("timestamp", None)}
        for key in _LEADING_AUDIT_KEYS:
            if key in event_dict:
                ordered[key] = event_dict.pop(key)
        ordered.update(event_dict)
        text = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), default=str)
        return _colorize_json(text) if colorize else text

    return render


# --------------------------------------------------------------------------
# Public API.
# --------------------------------------------------------------------------


def configure_logging(level: str, color: str, stream: TextIO | None = None) -> None:
    """Configure the one app-wide logger (D-58). Safe to call repeatedly.

    `level`: the minimum level, "DEBUG" | "DEV" | "INFO" | "WARNING" | "ERROR".
    `color`: "auto" (colour only on a TTY `stream`) | "always" | "never".
    `stream`: defaults to `sys.stdout`.
    """
    target_stream: TextIO = stream if stream is not None else sys.stdout
    is_tty = bool(getattr(target_stream, "isatty", lambda: False)())
    colorize = color == "always" or (color == "auto" and is_tty)
    level_value = std_logging.getLevelNamesMapping().get(level.upper(), std_logging.INFO)

    # Reused both as structlog's own processor chain (plus wrap_for_formatter)
    # and as the foreign_pre_chain applied to plain stdlib log records, so
    # every line -- ours or a third party's -- gets the same fields.
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _mask_secrets,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _render_json(colorize),
        ],
    )

    handler = std_logging.StreamHandler(target_stream)
    handler.setFormatter(formatter)
    handler.addFilter(_LevelFilter(level_value))

    root = std_logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level_value)

    for name in _NOISY_LOGGERS:
        std_logging.getLogger(name).setLevel(std_logging.WARNING)
    # These emit their request lines at INFO; the filter moves them to DEV.
    for name in ("httpx", "httpx2", "google_genai"):
        std_logging.getLogger(name).setLevel(std_logging.INFO)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger, configured by `configure_logging`."""
    if name is None:
        return cast("structlog.stdlib.BoundLogger", structlog.get_logger())
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))


@contextlib.contextmanager
def bind_email_context(
    message_id: str,
    worker_id: str,
    *,
    stage: str | None = None,
    attempt: int | None = None,
) -> Iterator[None]:
    """Bind `message_id` / `worker_id` (and optionally `stage` / `attempt`) onto every
    log line emitted while inside this context (D-58); cleared on exit."""
    values: dict[str, Any] = {"message_id": message_id, "worker_id": worker_id}
    if stage is not None:
        values["stage"] = stage
    if attempt is not None:
        values["attempt"] = attempt
    tokens = structlog.contextvars.bind_contextvars(**values)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def log_email_processed(
    logger: structlog.stdlib.BoundLogger,
    *,
    message_id: str,
    sender: str,
    subject: str,
    primary: str | None,
    urgent: bool,
    actions: Sequence[str],
    outcome: str,
    reason: str | None = None,
    deadline_phrase: str | None = None,
    deadline_at: datetime | None = None,
) -> None:
    """Emit the one required `event="email.processed"` audit summary line (D-58).

    Every processed email gets exactly one, with the requirement's audit fields
    `label` (the primary label, `None` when there is none), `action` (the actions
    taken) and `outcome`; the other fields are extra context.

    `deadline_phrase`/`deadline_at` (D-24, task 47) are the classification's resolved
    deadline, when it has one; `deadline_at` is logged as ISO 8601 explicitly (the
    renderer's `default=str` fallback would otherwise give a non-ISO `str(datetime)`
    rendering, e.g. "2026-09-29 20:59:00+00:00" instead of
    "2026-09-29T20:59:00+00:00").
    """
    fields: dict[str, Any] = {
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "label": primary,
        "action": list(actions),
        "outcome": outcome,
        "urgent": urgent,
    }
    if reason is not None:
        fields["reason"] = reason
    if deadline_phrase is not None:
        fields["deadline_phrase"] = deadline_phrase
    if deadline_at is not None:
        fields["deadline_at"] = deadline_at.isoformat()
    logger.info("email.processed", **fields)

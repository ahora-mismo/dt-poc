"""Approval reply parsing (D-34, D-49).

Splits a manager's reply into new (non-quoted) text and quoted text,
and reads the approval decision from the first non-empty non-quoted
line. No token handling here: `quoted_text` is handed back raw so the
caller (`core/approval_token.py`, composed elsewhere) can extract the
signed token from it, or fall back to the latest approval email in the
thread when `has_quote` is False.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from dt_agent.domain.email import Email


class ReplyDecision(StrEnum):
    """The outcome of matching the reply's first line against D-34's word list."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    UNCLEAR = "UNCLEAR"
    INVALID_OPTION = "INVALID_OPTION"
    IGNORED = "IGNORED"


class ParsedReply(BaseModel):
    """The result of parsing one approval reply.

    `option` is only meaningful for `APPROVE` (always >= 1, D-13) and
    `INVALID_OPTION` (the out-of-range number the manager sent).
    `quoted_text` is the raw quoted block (unstripped of `>` prefixes);
    `has_quote` tells the caller whether a thread fallback is needed
    for token extraction.
    """

    model_config = ConfigDict(frozen=True)

    decision: ReplyDecision
    option: int | None = None
    quoted_text: str = ""
    has_quote: bool = False


_APPROVE_WORDS = {"approve", "approved", "yes", "ok", "confirm", "confirmed"}
_REJECT_WORDS = {"reject", "rejected", "no", "decline"}

# The whole (trailing-punctuation-stripped) line must be exactly one
# decision word, optionally followed by whitespace and a number —
# anything else trailing (e.g. "Yes, go ahead") makes it UNCLEAR.
_DECISION_LINE_RE = re.compile(r"^(?P<word>[A-Za-z]+)(?:\s+(?P<option>\d+))?$")
_TRAILING_PUNCTUATION_RE = re.compile(r"[.,!?;:]+$")

# A typical English reply-quote header: "On <date>, <name> wrote:".
_ON_DATE_WROTE_RE = re.compile(r"^on\s.+\swrote:\s*$", re.IGNORECASE)

# Outlook's "-----Original Message-----" separator.
_ORIGINAL_MESSAGE_RE = re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.IGNORECASE)

# Outlook's "From: ... / Sent: ..." header block: a "From:" line
# followed within a few lines by a "Sent:" line.
_FROM_HEADER_RE = re.compile(r"^from:\s", re.IGNORECASE)
_SENT_HEADER_RE = re.compile(r"^sent:\s", re.IGNORECASE)
_FROM_SENT_LOOKAHEAD = 4


def _quote_start_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">"):
            return index
        if _ON_DATE_WROTE_RE.match(stripped) or _ORIGINAL_MESSAGE_RE.match(stripped):
            return index
        if _FROM_HEADER_RE.match(stripped):
            lookahead = lines[index + 1 : index + 1 + _FROM_SENT_LOOKAHEAD]
            if any(_SENT_HEADER_RE.match(candidate.strip()) for candidate in lookahead):
                return index
    return None


def split_quoted(body: str) -> tuple[str, str]:
    """Split `body` into `(new_text, quoted_text)` (D-34).

    `quoted_text` is empty when no quote marker or reply-header block
    is found; `new_text` is then the whole body.
    """
    lines = body.splitlines()
    start = _quote_start_index(lines)
    if start is None:
        return body, ""
    return "\n".join(lines[:start]), "\n".join(lines[start:])


def _first_non_empty_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _parse_decision_line(line: str, option_count: int) -> tuple[ReplyDecision, int | None]:
    normalized = _TRAILING_PUNCTUATION_RE.sub("", line.strip())
    match = _DECISION_LINE_RE.match(normalized)
    if match is None:
        return ReplyDecision.UNCLEAR, None

    word = match.group("word").casefold()
    option_text = match.group("option")

    if word in _REJECT_WORDS:
        if option_text is not None:
            return ReplyDecision.UNCLEAR, None
        return ReplyDecision.REJECT, None

    if word in _APPROVE_WORDS:
        option = int(option_text) if option_text is not None else 1
        if option < 1 or option > option_count:
            return ReplyDecision.INVALID_OPTION, option
        return ReplyDecision.APPROVE, option

    return ReplyDecision.UNCLEAR, None


def _is_auto_submitted(email: Email) -> bool:
    value = email.auto_submitted
    return value is not None and value.strip().casefold() != "no"


def parse_reply(email: Email, *, option_count: int) -> ParsedReply:
    """Parse one approval-thread reply (D-34, D-49).

    `option_count` is the number of options offered in the latest
    approval email, supplied by the caller (this module has no state
    and doesn't look at the thread itself).
    """
    new_text, quoted_text = split_quoted(email.text_body)
    has_quote = bool(quoted_text.strip())

    if _is_auto_submitted(email):
        return ParsedReply(
            decision=ReplyDecision.IGNORED, quoted_text=quoted_text, has_quote=has_quote
        )

    line = _first_non_empty_line(new_text)
    if line is None:
        return ParsedReply(
            decision=ReplyDecision.UNCLEAR, quoted_text=quoted_text, has_quote=has_quote
        )

    decision, option = _parse_decision_line(line, option_count)
    return ParsedReply(
        decision=decision, option=option, quoted_text=quoted_text, has_quote=has_quote
    )

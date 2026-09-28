"""Routing: skip-own / approval-reply / ignore-sent / classify (D-21, D-22, D-47).

Pure decision logic, no I/O: the caller looks up whether the message id is
already recorded (`ports/store.py`'s `MessageStore.contains`) and fetches the
rest of its Gmail thread, then calls `route`. The order matters (D-22/D-47):
a recorded id (our own sent/processed message) is skipped first; the
approval-thread check runs *before* the `SENT`-without-approval check, so a
manager's self-addressed reply -- which also carries `SENT` -- still reaches
approval handling instead of being silently ignored.

An "approval email" (D-47) is a thread message that carries our token block
(`core/approval_token.py`'s BEGIN/END delimiters) whose JWT *signature*
verifies with `signing_key`. Expiry doesn't disqualify it (D-34 handles an
expired approval as its own outcome, not as "no approval email here"): this
module verifies with a `now` far enough in the future that `exp` can never
be exceeded, so only `bad_signature` / `wrong_algorithm` / `malformed` (or no
block at all) make a message not count.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from dt_agent.core.approval_token import VerifiedApproval, extract_token, verify
from dt_agent.domain.email import Email
from dt_agent.domain.labels import GMAIL_LABEL_ID_SENT

# Used only to make signature verification ignore expiry (D-47): `verify`
# rejects as expired when `now > exp`, so a `now` this early is never after
# any real `exp` claim, and `verify` never returns `expired` here -- it
# returns either a `VerifiedApproval` or a signature/format failure, which
# is exactly the "is this our token?" question.
_IGNORE_EXPIRY_NOW = datetime.min.replace(tzinfo=UTC)


class RouteKind(StrEnum):
    """Where an incoming message goes next (D-21, D-22, D-47)."""

    SKIP_OWN = "SKIP_OWN"
    APPROVAL_REPLY = "APPROVAL_REPLY"
    IGNORE_SENT = "IGNORE_SENT"
    CLASSIFY = "CLASSIFY"


class Route(BaseModel):
    """The routing decision for one message."""

    model_config = ConfigDict(frozen=True)

    kind: RouteKind


def _is_approval_email(email: Email, signing_key: str) -> bool:
    token = extract_token(email.text_body)
    if token is None:
        return False
    return isinstance(verify(token, signing_key, now=_IGNORE_EXPIRY_NOW), VerifiedApproval)


def find_approval_emails(thread: Sequence[Email], signing_key: str) -> list[Email]:
    """Every approval email in `thread`, oldest first (D-34, D-47).

    Task 20 uses this to find "the latest approval email in the thread"
    (D-34) when checking a reply's token against a possible re-issue.
    """
    matches = [message for message in thread if _is_approval_email(message, signing_key)]
    return sorted(matches, key=lambda message: message.received_at)


def latest_approval_email(thread: Sequence[Email], signing_key: str) -> Email | None:
    """The most recently received approval email in `thread`, or `None` (D-34)."""
    matches = find_approval_emails(thread, signing_key)
    return matches[-1] if matches else None


def route(
    email: Email,
    *,
    is_recorded: bool,
    thread: Sequence[Email],
    signing_key: str,
) -> Route:
    """Decide where `email` goes next (D-21, D-22, D-47).

    `is_recorded` is the caller's `MessageStore.contains` result for
    `email.id` (D-21/D-22: our own sent or already-processed messages).
    `thread` is every other message fetched for `email.thread_id`; it need
    not include `email` itself, and `email` is never counted as its own
    approval email even if it is included.
    """
    if is_recorded:
        return Route(kind=RouteKind.SKIP_OWN)

    others = [message for message in thread if message.id != email.id]
    if find_approval_emails(others, signing_key):
        return Route(kind=RouteKind.APPROVAL_REPLY)

    if GMAIL_LABEL_ID_SENT in email.label_ids:
        return Route(kind=RouteKind.IGNORE_SENT)

    return Route(kind=RouteKind.CLASSIFY)


__all__ = [
    "Route",
    "RouteKind",
    "find_approval_emails",
    "latest_approval_email",
    "route",
]

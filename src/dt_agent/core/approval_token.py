"""Approval token: signed JWT embedded in approval emails (D-5, D-8, D-32, D-33).

The approval email carries every detail needed to execute the action in a
signed JWT (D-5): no pending-approval state is stored anywhere. This module
owns:

- the claim model (`ReplyAction` / `CalendarAction` / `InvitationAction`,
  wrapped as `Claims`) per D-32;
- signing and verification, HS256 only via PyJWT (D-33);
- the business-day expiry rule (D-10);
- the BEGIN/END delimiter block and extracting the token back out of a
  quoted, wrapped, HTML-to-text-mangled reply body (D-8).

Thread/jti binding ("is this the *latest* approval email in the thread")
needs the Gmail thread, which this module never sees; that check is done by
the caller (task 20), using the `jti` this module exposes on `VerifiedApproval`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import jwt
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

_ALGORITHM = "HS256"

# Short and plain-ASCII on purpose: well under any line-wrap width (D-8), so
# a mail client never splits a delimiter line across two lines.
TOKEN_BLOCK_BEGIN = "-----BEGIN DT-AGENT APPROVAL TOKEN-----"
TOKEN_BLOCK_END = "-----END DT-AGENT APPROVAL TOKEN-----"

# The JWT compact-serialization alphabet (base64url + the two `.` separators).
# Anything else between the delimiters -- quote markers, whitespace, stray
# HTML-to-text leftovers -- is noise and is dropped (D-8).
_TOKEN_CHAR = re.compile(r"[A-Za-z0-9._-]")

_WEEKEND = frozenset({5, 6})  # datetime.weekday(): Saturday, Sunday


# --------------------------------------------------------------------------
# Action payloads (D-32's `act` claim)
# --------------------------------------------------------------------------


class ReplyAction(BaseModel):
    """Send a reply exactly as drafted (D-32): a courtesy reply, decline, ..."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["reply"] = "reply"
    recipient: str
    thread_id: str
    message_id: str
    text: str
    # The urgent-internal acknowledgement (D-40) needs its approval subject
    # marked urgent even after a re-issue (D-11, task 40); a payload signed
    # before this field existed still decodes, as not urgent.
    urgent: bool = False


class CalendarOption(BaseModel):
    """One numbered slot of a `CalendarAction` (D-13, D-14)."""

    model_config = ConfigDict(frozen=True)

    number: int = Field(ge=1)
    start: AwareDatetime
    end: AwareDatetime


class CalendarAction(BaseModel):
    """Create a meeting for whichever numbered option the manager approves."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["calendar"] = "calendar"
    title: str
    options: tuple[CalendarOption, ...] = Field(min_length=1)
    attendees: tuple[str, ...] = ()
    location: str | None = None
    # The "not available" reply sent to the requester on REJECT (a meeting request
    # from an email's text); a payload signed before this field existed decodes as None.
    decline_body: str | None = None


class InvitationAction(BaseModel):
    """Answer a calendar invitation received by email (D-48).

    The invitation's own identifiers travel in the token; the response
    itself (accept/decline) is decided by the manager's APPROVE/REJECT
    reply, not encoded here.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["invitation"] = "invitation"
    ical_uid: str
    start: AwareDatetime
    end: AwareDatetime
    organizer: str | None = None
    title: str | None = None
    location: str | None = None


ApprovalAction = Annotated[
    ReplyAction | CalendarAction | InvitationAction,
    Field(discriminator="kind"),
]

_action_adapter: TypeAdapter[ApprovalAction] = TypeAdapter(ApprovalAction)


# --------------------------------------------------------------------------
# Business-day expiry (D-10)
# --------------------------------------------------------------------------


def add_business_days(start: datetime, n: int, tz: ZoneInfo) -> datetime:
    """`n` business days (Mon-Fri) after `start`, at the same time of day.

    A weekend `start` counts from the following Monday at the same time of
    day (D-10): Saturday/Sunday isn't itself a business day, so it can't be
    day zero of the count. `start` is converted to `tz` first, since
    "business day" and "time of day" are local-calendar concepts; the
    result is returned in `tz`. DST edge cases are ignored (D-66).
    """
    if start.tzinfo is None:
        raise ValueError("start must be an aware datetime")
    if n < 0:
        raise ValueError("n must be >= 0")

    local = start.astimezone(tz)
    if local.weekday() == 5:  # Saturday -> Monday
        local += timedelta(days=2)
    elif local.weekday() == 6:  # Sunday -> Monday
        local += timedelta(days=1)

    remaining = n
    while remaining > 0:
        local += timedelta(days=1)
        if local.weekday() not in _WEEKEND:
            remaining -= 1
    return local


def compute_expiry(
    sent_at: datetime,
    business_days: int,
    tz: ZoneInfo,
    latest_slot_start: datetime | None = None,
) -> datetime:
    """The token's `exp` (D-32): the earlier of the business-day deadline and
    the start of the latest proposed slot, when there is one.

    Exported separately from `issue` because the approval email must show
    the same deadline (D-32: "computed with the same logic as the token's
    exp") without decoding a token to get it.
    """
    business_expiry = add_business_days(sent_at, business_days, tz)
    if latest_slot_start is None:
        return business_expiry
    return min(business_expiry, latest_slot_start)


def _latest_slot_start(action: ApprovalAction) -> datetime | None:
    if isinstance(action, CalendarAction):
        return max(option.start for option in action.options)
    return None


def hash_text(text: str) -> str:
    """SHA-256 hex digest of the text shown in the email (D-32's `h` claim).

    `text` is the human-readable body the manager sees, not including the
    token block itself (which can't hash itself).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Claims, issuing and verifying
# --------------------------------------------------------------------------


class Claims(BaseModel):
    """The decoded, validated JWT payload (D-32)."""

    model_config = ConfigDict(frozen=True)

    jti: str
    iat: AwareDatetime
    exp: AwareDatetime
    sub: str
    act: ApprovalAction
    h: str


class VerifiedApproval(BaseModel):
    """A successfully verified approval token, ready for the caller (task 20)
    to check jti-vs-thread binding and act on `action`.
    """

    model_config = ConfigDict(frozen=True)

    jti: str
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    original_email_id: str
    action: ApprovalAction
    text_hash: str


class InvalidReason(StrEnum):
    """Why `verify` rejected a token (D-9: any of these is treated as phishing)."""

    BAD_SIGNATURE = "bad_signature"
    WRONG_ALGORITHM = "wrong_algorithm"
    EXPIRED = "expired"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class InvalidToken:
    """`verify`'s failure case, carrying why it failed."""

    reason: InvalidReason


def issue(
    *,
    key: str,
    sent_at: datetime,
    business_days: int,
    tz: ZoneInfo,
    original_email_id: str,
    action: ApprovalAction,
    shown_text: str,
    jti: str | None = None,
) -> str:
    """Sign a new approval token (D-32, D-33): HS256 only.

    `sent_at` is both the `iat` and the anchor for the business-day expiry
    (D-10); `shown_text` is hashed into the `h` claim.
    """
    if sent_at.tzinfo is None:
        raise ValueError("sent_at must be an aware datetime")

    exp = compute_expiry(sent_at, business_days, tz, _latest_slot_start(action))
    claims = {
        "jti": jti if jti is not None else uuid4().hex,
        "iat": sent_at,
        "exp": exp,
        "sub": original_email_id,
        "act": action.model_dump(mode="json"),
        "h": hash_text(shown_text),
    }
    return jwt.encode(claims, key, algorithm=_ALGORITHM)


def verify(token: str, key: str, now: datetime) -> VerifiedApproval | InvalidToken:
    """Verify a token extracted from a reply (D-9, D-33, D-34).

    Only HS256 is accepted (D-33): `alg=none` and every other algorithm are
    rejected as `wrong_algorithm` before any signature check, so a
    same-key-different-algorithm confusion attack is never even attempted.
    Expiry is checked against the injected `now`, not wall-clock time, so
    every caller (including tests) uses one clock (D-52); `exp` is "the
    last moment an approval is accepted" (D-32), so `now > exp` -- not
    `>=` -- is what expires it.
    """
    if now.tzinfo is None:
        raise ValueError("now must be an aware datetime")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.exceptions.PyJWTError:
        return InvalidToken(InvalidReason.MALFORMED)

    if header.get("alg") != _ALGORITHM:
        return InvalidToken(InvalidReason.WRONG_ALGORITHM)

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[_ALGORITHM],
            options={"verify_exp": False, "verify_iat": False},
        )
    except jwt.exceptions.InvalidSignatureError:
        return InvalidToken(InvalidReason.BAD_SIGNATURE)
    except jwt.exceptions.PyJWTError:
        return InvalidToken(InvalidReason.MALFORMED)

    try:
        claims = Claims.model_validate(payload)
    except ValidationError:
        return InvalidToken(InvalidReason.MALFORMED)

    if now > claims.exp:
        return InvalidToken(InvalidReason.EXPIRED)

    return VerifiedApproval(
        jti=claims.jti,
        issued_at=claims.iat,
        expires_at=claims.exp,
        original_email_id=claims.sub,
        action=claims.act,
        text_hash=claims.h,
    )


# --------------------------------------------------------------------------
# Email-body block (D-8)
# --------------------------------------------------------------------------


def render_token_block(token: str, *, width: int = 64) -> str:
    """The BEGIN/END-delimited block to put in the approval email body (D-8).

    The token is pre-wrapped at `width` characters per line: a
    `CalendarAction` with several options can push the compact JWT past
    RFC 5322's 998-char line limit, and this way the block survives
    regardless of what `Content-Transfer-Encoding` the Gmail adapter
    picks. `extract_token` rejoins wrapped lines on the way back in.
    """
    lines = [token[i : i + width] for i in range(0, len(token), width)] or [token]
    return "\n".join([TOKEN_BLOCK_BEGIN, *lines, TOKEN_BLOCK_END])


def extract_token(text: str) -> str | None:
    """Recover the token from a reply body (D-8, D-34).

    Survives what a round trip through a mail client does to the block:
    `> ` (or nested `> > `) quote prefixes, line wrapping, extra blank
    lines/whitespace, and HTML-to-text leftovers. Everything between the
    delimiters that isn't a JWT-alphabet character (base64url or `.`) is
    quoting/wrapping noise and is dropped; what remains is rejoined into one
    token with no internal separator, since a wrapped/quoted token has none
    of its own.

    A thread can carry more than one approval email after a re-issue
    (D-11, D-12): this returns the *first* BEGIN/END block found in `text`.
    Callers that must find the token of the *latest* approval email in a
    thread do that by choosing which email's body they pass in, not by
    asking this function to pick among several blocks in one body.
    """
    start = text.find(TOKEN_BLOCK_BEGIN)
    if start == -1:
        return None
    end = text.find(TOKEN_BLOCK_END, start + len(TOKEN_BLOCK_BEGIN))
    if end == -1:
        return None

    block = text[start + len(TOKEN_BLOCK_BEGIN) : end]
    token = "".join(_TOKEN_CHAR.findall(block))
    return token or None


__all__ = [
    "TOKEN_BLOCK_BEGIN",
    "TOKEN_BLOCK_END",
    "ApprovalAction",
    "CalendarAction",
    "CalendarOption",
    "Claims",
    "InvalidReason",
    "InvalidToken",
    "InvitationAction",
    "ReplyAction",
    "VerifiedApproval",
    "add_business_days",
    "compute_expiry",
    "extract_token",
    "hash_text",
    "issue",
    "render_token_block",
    "verify",
]

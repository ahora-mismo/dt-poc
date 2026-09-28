"""Unit tests for dt_agent.core.approval_token (task 9).

Per the testing rules, this covers our own policy on top of PyJWT (D-33):
business-day expiry (D-10), the exp = min(business deadline, latest slot)
rule (D-32), rejecting a token for the wrong reason (D-9), and surviving a
quoted/wrapped round trip through a mail client (D-8) -- not PyJWT's own
signature/algorithm checking.
"""

import textwrap
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import jwt
import pytest

from dt_agent.core.approval_token import (
    CalendarAction,
    CalendarOption,
    InvalidReason,
    InvalidToken,
    ReplyAction,
    VerifiedApproval,
    add_business_days,
    extract_token,
    hash_text,
    issue,
    render_token_block,
    verify,
)

_TZ = ZoneInfo("Europe/Bucharest")
_KEY = "a-signing-key-at-least-32-bytes-long-for-hs256"


def _reply_action() -> ReplyAction:
    return ReplyAction(
        recipient="manager@example.com",
        thread_id="thread-1",
        message_id="<msg-1@example.com>",
        text="Sure, sending this now.",
    )


def test_token_expiry_counts_business_days() -> None:
    friday_16 = datetime(2026, 10, 2, 16, 0, tzinfo=_TZ)  # Friday
    saturday_10 = datetime(2026, 10, 3, 10, 0, tzinfo=_TZ)  # Saturday

    friday_plus_3 = add_business_days(friday_16, 3, _TZ)
    saturday_plus_3 = add_business_days(saturday_10, 3, _TZ)

    # Fri 16:00 + 3 business days -> Wed 16:00 (skips Sat/Sun).
    assert friday_plus_3 == datetime(2026, 10, 7, 16, 0, tzinfo=_TZ)
    # A weekend start counts from the following Monday, same time of day:
    # Mon 10:00 + 3 business days -> Thu 10:00.
    assert saturday_plus_3 == datetime(2026, 10, 8, 10, 0, tzinfo=_TZ)


def test_token_exp_is_min_of_expiry_and_latest_slot() -> None:
    sent_at = datetime(2026, 9, 28, 9, 0, tzinfo=_TZ)  # Monday
    tomorrow_10 = datetime(2026, 9, 29, 10, 0, tzinfo=_TZ)
    option = CalendarOption(number=1, start=tomorrow_10, end=tomorrow_10 + timedelta(hours=1))
    action = CalendarAction(title="Sync", options=(option,))

    token = issue(
        key=_KEY,
        sent_at=sent_at,
        business_days=3,  # would otherwise expire Thu 09:00, well after the slot
        tz=_TZ,
        original_email_id="gmail-msg-1",
        action=action,
        shown_text="Sync tomorrow at 10:00?",
    )

    just_before = verify(token, _KEY, tomorrow_10 - timedelta(seconds=1))
    just_after = verify(token, _KEY, tomorrow_10 + timedelta(seconds=1))

    assert isinstance(just_before, VerifiedApproval)
    assert just_before.expires_at == tomorrow_10
    assert isinstance(just_after, InvalidToken)
    assert just_after.reason is InvalidReason.EXPIRED


def test_token_verify_rejects_wrong_key_alg_none_tampered_expired() -> None:
    sent_at = datetime(2026, 9, 28, 9, 0, tzinfo=_TZ)
    now = sent_at + timedelta(minutes=1)
    token = issue(
        key=_KEY,
        sent_at=sent_at,
        business_days=3,
        tz=_TZ,
        original_email_id="gmail-msg-1",
        action=_reply_action(),
        shown_text="Approve this reply?",
    )
    header_b64, payload_b64, signature_b64 = token.split(".")

    wrong_key = verify(token, "a-completely-different-signing-key-of-32b", now)

    none_alg_claims = {
        "jti": "x",
        "iat": sent_at,
        "exp": now + timedelta(days=1),
        "sub": "s",
        "act": {},
        "h": "x",
    }
    # PyJWT allows `key=None` at runtime only for the "none" algorithm; its
    # type stubs don't model that special case.
    none_alg_token = jwt.encode(none_alg_claims, key=None, algorithm="none")  # type: ignore[arg-type]
    alg_none = verify(none_alg_token, _KEY, now)

    tampered_payload = jwt.utils.base64url_encode(
        jwt.utils.base64url_decode(payload_b64.encode()).replace(b"gmail-msg-1", b"gmail-msg-9")
    ).decode()
    tampered_token = f"{header_b64}.{tampered_payload}.{signature_b64}"
    tampered = verify(tampered_token, _KEY, now)

    expired = verify(token, _KEY, sent_at + timedelta(days=30))

    assert isinstance(wrong_key, InvalidToken) and wrong_key.reason is InvalidReason.BAD_SIGNATURE
    assert isinstance(alg_none, InvalidToken) and alg_none.reason is InvalidReason.WRONG_ALGORITHM
    assert isinstance(tampered, InvalidToken) and tampered.reason is InvalidReason.BAD_SIGNATURE
    assert isinstance(expired, InvalidToken) and expired.reason is InvalidReason.EXPIRED


def test_token_block_survives_quoting_and_wrapping() -> None:
    sent_at = datetime(2026, 9, 28, 9, 0, tzinfo=_TZ)
    now = sent_at + timedelta(minutes=1)
    token = issue(
        key=_KEY,
        sent_at=sent_at,
        business_days=3,
        tz=_TZ,
        original_email_id="gmail-msg-1",
        action=_reply_action(),
        shown_text="Approve this reply?",
    )
    block = render_token_block(token)

    # Simulate a nested-quoted, line-wrapped reply body.
    wrapped_lines = []
    for line in block.splitlines():
        if not line:
            wrapped_lines.append("> > ")
            continue
        wrapped_lines.extend(textwrap.wrap(line, width=76) or [""])
    quoted_and_wrapped = "\n".join(f"> > {line}" for line in wrapped_lines)
    reply_body = f"On Mon, Sep 28, manager wrote:\n{quoted_and_wrapped}\n\n> > Sent from my phone"

    extracted = extract_token(reply_body)

    assert extracted == token
    result = verify(extracted or "", _KEY, now)
    assert isinstance(result, VerifiedApproval)
    assert result.original_email_id == "gmail-msg-1"


def test_approval_token_reply_action_without_urgent_reads_false() -> None:
    # D-40/D-11: an old-shape token payload (before `urgent` existed) must
    # still decode, defaulting to "not urgent".
    sent_at = datetime(2026, 9, 28, 9, 0, tzinfo=_TZ)
    now = sent_at + timedelta(minutes=1)
    act = _reply_action().model_dump(mode="json")
    del act["urgent"]
    claims = {
        "jti": "x",
        "iat": sent_at,
        "exp": sent_at + timedelta(days=1),
        "sub": "gmail-msg-1",
        "act": act,
        "h": hash_text("Approve this reply?"),
    }
    token = jwt.encode(claims, _KEY, algorithm="HS256")

    result = verify(token, _KEY, now)

    assert isinstance(result, VerifiedApproval)
    assert isinstance(result.action, ReplyAction)
    assert result.action.urgent is False


def test_add_business_days_rejects_naive_start() -> None:
    naive_start = datetime(2026, 9, 28, 9, 0)

    with pytest.raises(ValueError, match="aware"):
        add_business_days(naive_start, 3, _TZ)

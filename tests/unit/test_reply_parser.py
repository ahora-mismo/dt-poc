"""Unit tests for dt_agent.core.reply_parser (task 10, D-34, D-49).

Per the testing rules, this covers the decision policy (word matching,
trailing punctuation, option ranges), the "first non-quoted line wins"
rule, the quote/no-quote split used for token extraction downstream,
and the Auto-Submitted guard. It does not test PyJWT or any token
extraction (that belongs to `core/approval_token.py`, owned elsewhere).
"""

from datetime import UTC, datetime

from dt_agent.core.reply_parser import ReplyDecision, parse_reply
from dt_agent.domain.email import Email, EmailAddress, Headers


def _email(text_body: str, headers: dict[str, str] | None = None) -> Email:
    return Email(
        id="msg-1",
        thread_id="thread-1",
        headers=Headers(values=headers or {}),
        from_address=EmailAddress(address="manager@example.com"),
        subject="Re: Approval needed",
        received_at=datetime(2026, 9, 29, 10, 0, tzinfo=UTC),
        text_body=text_body,
    )


def test_reply_decision_words() -> None:
    approved_bang = parse_reply(_email("APPROVED!"), option_count=3)
    approve_two = parse_reply(_email("approve 2"), option_count=3)
    ok_period = parse_reply(_email("ok."), option_count=3)
    reject = parse_reply(_email("REJECT"), option_count=3)
    unclear = parse_reply(_email("Yes, go ahead"), option_count=3)

    assert approved_bang.decision is ReplyDecision.APPROVE
    assert approved_bang.option == 1
    assert approve_two.decision is ReplyDecision.APPROVE
    assert approve_two.option == 2
    assert ok_period.decision is ReplyDecision.APPROVE
    assert ok_period.option == 1
    assert reject.decision is ReplyDecision.REJECT
    assert unclear.decision is ReplyDecision.UNCLEAR


def test_reply_option_out_of_range() -> None:
    result = parse_reply(_email("APPROVE 4"), option_count=3)

    assert result.decision is ReplyDecision.INVALID_OPTION


def test_reply_first_non_quoted_line_wins() -> None:
    body = (
        "APPROVE\n"
        "\n"
        "Best regards,\n"
        "Jane\n"
        "\n"
        "On Mon, Sep 29, 2026 at 9:00 AM, Agent <agent@example.com> wrote:\n"
        "> Please approve or reject.\n"
        "> Token: abc123\n"
    )

    result = parse_reply(_email(body), option_count=1)

    assert result.decision is ReplyDecision.APPROVE
    assert result.option == 1


def test_reply_token_from_quote_or_thread_fallback() -> None:
    quoted_body = (
        "APPROVE\n"
        "On Mon, Sep 29, 2026 at 9:00 AM, Agent <agent@example.com> wrote:\n"
        "> Token: abc123\n"
    )
    unquoted_body = "APPROVE\n"

    with_quote = parse_reply(_email(quoted_body), option_count=1)
    without_quote = parse_reply(_email(unquoted_body), option_count=1)

    assert with_quote.has_quote is True
    assert "abc123" in with_quote.quoted_text
    assert without_quote.has_quote is False
    assert without_quote.quoted_text == ""


def test_reply_auto_submitted_ignored() -> None:
    result = parse_reply(
        _email("APPROVE", headers={"Auto-Submitted": "auto-replied"}),
        option_count=1,
    )

    assert result.decision is ReplyDecision.IGNORED

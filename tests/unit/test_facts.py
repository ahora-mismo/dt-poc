"""Tests for :mod:`dt_agent.core.facts` and :mod:`dt_agent.core.html_text` (task 11).

Per the testing rules: guardrail/business-logic behaviour that could
actually break, table-driven where a family of cases shares the same
shape. No snapshot/exact-string assertions on prompt wording, only that
the right facts are present -- except task 52's observation-only fact
lines, whose exact wording (no interpretive language) is itself the
behaviour under test, so those are pinned exactly.
"""

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from dt_agent.core.facts import (
    EmailFacts,
    extract_facts,
    truncate_for_llm,
)
from dt_agent.core.html_text import html_to_text
from dt_agent.domain.email import Attachment, CalendarPart, Email, EmailAddress, Headers


def _make_email(
    *,
    headers: Mapping[str, str] | None = None,
    from_address: EmailAddress | None = None,
    reply_to: EmailAddress | None = None,
    text_body: str = "Hello there.",
    attachments: tuple[Attachment, ...] = (),
    calendar_parts: tuple[CalendarPart, ...] = (),
) -> Email:
    return Email(
        id="msg-1",
        thread_id="thread-1",
        headers=Headers(values=dict(headers or {})),
        from_address=from_address or EmailAddress(address="sender@example.com"),
        reply_to=reply_to,
        subject="Subject",
        received_at=datetime(2026, 9, 28, 10, 15, tzinfo=UTC),
        text_body=text_body,
        attachments=attachments,
        calendar_parts=calendar_parts,
    )


def _extract(
    email: Email,
    *,
    own_address: str = "me@example.com",
    internal_domains: list[str] | None = None,
    allowlist_senders: list[str] | None = None,
    allowlist_domains: list[str] | None = None,
    gmail_labels: tuple[str, ...] = (),
) -> EmailFacts:
    return extract_facts(
        email,
        own_address=own_address,
        internal_domains=internal_domains or [],
        allowlist_senders=allowlist_senders or [],
        allowlist_domains=allowlist_domains or [],
        gmail_labels=gmail_labels,
    )


def test_facts_auth_results_parsed() -> None:
    email = _make_email(
        headers={
            "Authentication-Results": (
                "mx.google.com;\n"
                " dkim=pass header.i=@acme.com header.s=selector1 header.d=acme.com;\n"
                " spf=pass smtp.mailfrom=acme.com;\n"
                " dmarc=pass header.from=acme.com"
            )
        },
        from_address=EmailAddress(address="sender@acme.com"),
    )

    facts = _extract(email)

    assert facts.auth.dkim == "pass"
    assert facts.auth.dkim_domain == "acme.com"
    assert facts.auth.dmarc == "pass"
    assert facts.auth.dmarc_domain == "acme.com"
    assert facts.auth.spf == "pass"


def test_facts_auth_dkim_domain_falls_back_to_header_i() -> None:
    """When `header.d=` is absent, the domain is read from `header.i=@domain`."""
    email = _make_email(
        headers={
            "Authentication-Results": "mx.google.com; dkim=pass header.i=@acme.com header.s=sel1"
        },
        from_address=EmailAddress(address="sender@acme.com"),
    )

    facts = _extract(email)

    assert facts.auth.dkim_domain == "acme.com"


def test_facts_failed_authentication_surfaced_to_prompt() -> None:
    """D-44: a failing SPF/DKIM/DMARC result is a fact the LLM must see."""
    email = _make_email(
        headers={
            "Authentication-Results": (
                "mx.google.com; dkim=fail header.d=acme.com; dmarc=fail header.from=acme.com"
            )
        },
        from_address=EmailAddress(address="sender@acme.com"),
    )

    facts = _extract(email)  # internal_domains=[] here: no internal/allowlist line either

    lines = facts.to_prompt_lines()
    assert any("dkim=fail" in line for line in lines)
    assert any("dmarc=fail" in line for line in lines)


def test_facts_internal_requires_auth() -> None:
    email = _make_email(
        headers={
            "Authentication-Results": (
                "mx.google.com; dkim=fail header.d=acme.com; dmarc=fail header.from=acme.com"
            )
        },
        from_address=EmailAddress(address="sender@acme.com"),
    )

    facts = _extract(email, internal_domains=["acme.com"])

    assert facts.internal is False
    assert facts.internal_reason is not None
    assert "acme.com" in facts.internal_reason
    # The "internal" line only fires when internal is True.
    assert "is internal" not in "\n".join(facts.to_prompt_lines())


def test_facts_internal_sender_when_domain_and_auth_pass() -> None:
    email = _make_email(
        headers={
            "Authentication-Results": (
                "mx.google.com; dkim=pass header.d=acme.com; dmarc=pass header.from=acme.com"
            )
        },
        from_address=EmailAddress(address="sender@acme.com"),
    )

    facts = _extract(email, internal_domains=["acme.com"])

    assert facts.internal is True
    assert facts.internal_reason is None
    assert any("internal" in line.lower() for line in facts.to_prompt_lines())


@pytest.mark.parametrize(
    ("from_address", "allowlist_senders", "allowlist_domains", "auth_header", "expected"),
    [
        # Domain allowlist matches a subdomain, case-insensitively; auth passes.
        (
            "a@Mail.Example.com",
            [],
            ["example.com"],
            "mx; dkim=pass header.d=mail.example.com; dmarc=pass header.from=mail.example.com",
            True,
        ),
        # Exact address allowlist match, case-insensitive; auth passes.
        (
            "Sales@Vendor.com",
            ["sales@vendor.com"],
            [],
            "mx; dkim=pass header.d=vendor.com; dmarc=pass header.from=vendor.com",
            True,
        ),
        # Domain matches, but authentication fails -> not allowlisted.
        (
            "a@example.com",
            [],
            ["example.com"],
            "mx; dkim=fail header.d=example.com; dmarc=fail header.from=example.com",
            False,
        ),
    ],
)
def test_facts_allowlist_matching(
    from_address: str,
    allowlist_senders: list[str],
    allowlist_domains: list[str],
    auth_header: str,
    expected: bool,
) -> None:
    email = _make_email(
        headers={"Authentication-Results": auth_header},
        from_address=EmailAddress(address=from_address),
    )

    facts = _extract(
        email, allowlist_senders=allowlist_senders, allowlist_domains=allowlist_domains
    )

    assert facts.allowlisted is expected
    if not expected:
        assert facts.allowlist_reason is not None


@pytest.mark.parametrize(
    ("headers", "from_address", "own_address"),
    [
        ({}, "no-reply@example.com", "me@example.com"),
        ({"Auto-Submitted": "auto-generated"}, "person@example.com", "me@example.com"),
        ({"Precedence": "bulk"}, "person@example.com", "me@example.com"),
        (
            {"List-Unsubscribe": "<mailto:unsub@example.com>"},
            "person@example.com",
            "me@example.com",
        ),
        ({}, "me@example.com", "me@example.com"),
    ],
    ids=[
        "no-reply-local-part",
        "auto-submitted",
        "precedence-bulk",
        "list-unsubscribe",
        "own-address",
    ],
)
def test_facts_automated_sender_guard(
    headers: dict[str, str], from_address: str, own_address: str
) -> None:
    email = _make_email(headers=headers, from_address=EmailAddress(address=from_address))

    facts = _extract(email, own_address=own_address)

    assert facts.automated is True
    assert facts.automated_reasons


def test_facts_automated_sender_guard_ignores_auto_submitted_no() -> None:
    email = _make_email(
        headers={"Auto-Submitted": "no"},
        from_address=EmailAddress(address="person@example.com"),
    )

    facts = _extract(email)

    assert facts.automated is False


def test_facts_sender_mismatch_and_impersonation() -> None:
    email = _make_email(
        from_address=EmailAddress(name="PayPal Support", address="x@gmail.com"),
        reply_to=EmailAddress(address="reply@other.com"),
    )

    facts = _extract(email)

    assert facts.reply_to_mismatch is True
    assert facts.reply_to_domain == "other.com"
    assert facts.impersonation_reason is not None
    assert "paypal" in facts.impersonation_reason.lower()

    lines = facts.to_prompt_lines()
    assert any("reply-to" in line.lower() for line in lines)
    assert any("impersonation" in line.lower() for line in lines)


def test_facts_no_mismatch_no_impersonation_when_consistent() -> None:
    email = _make_email(
        from_address=EmailAddress(name="Acme Support", address="support@acme.com"),
        reply_to=EmailAddress(address="support@acme.com"),
    )

    facts = _extract(email)

    assert facts.reply_to_mismatch is False
    assert facts.impersonation_reason is None
    # The unconditional "no Authentication-Results header" line is the only one left
    # once every other fact is silent (task 52: there's always exactly one auth line).
    assert facts.to_prompt_lines() == ["Sender authentication: no Authentication-Results header."]


@pytest.mark.parametrize(
    ("body", "expected_href_fragment"),
    [
        ("Please visit paypal.com (https://evil.io/login)", "evil.io"),
        ("Login here (http://203.0.113.5/login)", "203.0.113.5"),
        ("Track your package (https://bit.ly/abc123)", "bit.ly"),
        ("Verify your account (https://xn--pypal-4ve.com/verify)", "xn--pypal-4ve.com"),
        ("Sign in (https://user:pass@example.com/login)", "example.com"),
        # Bare URLs (no HTML link markup ran through html_to_text) are still
        # scanned for host-based issues.
        ("Login: http://203.0.113.5/login", "203.0.113.5"),
        ("Click: https://bit.ly/promo", "bit.ly"),
    ],
    ids=[
        "domain-lookalike",
        "ip-host",
        "shortener",
        "punycode",
        "credentials",
        "bare-ip",
        "bare-shortener",
    ],
)
def test_facts_link_facts(body: str, expected_href_fragment: str) -> None:
    email = _make_email(text_body=body)

    facts = _extract(email)

    assert facts.link_issues
    assert any(expected_href_fragment in issue.href for issue in facts.link_issues)


def test_facts_impersonation_domain_in_display_name_without_brand_word() -> None:
    """A two-label domain in the display name flags even with no known brand word."""
    email = _make_email(
        from_address=EmailAddress(name="paypal.com", address="x@evil-phish.io"),
    )

    facts = _extract(email)

    assert facts.impersonation_reason is not None
    assert "paypal.com" in facts.impersonation_reason


def test_facts_impersonation_brand_word_flags_regardless_of_free_mail_domain() -> None:
    """A brand word in the display name flags even on a non-free-mail lookalike domain."""
    email = _make_email(
        from_address=EmailAddress(name="PayPal Support", address="x@evil-phish.io"),
    )

    facts = _extract(email)

    assert facts.impersonation_reason is not None
    assert "paypal" in facts.impersonation_reason.lower()


def test_facts_risky_attachments_and_bulk_cues() -> None:
    email = _make_email(
        text_body="Thanks for subscribing! To unsubscribe, click here. View in browser.",
        attachments=(
            Attachment(filename="invoice.html", mime_type="text/html", size=100),
            Attachment(filename="archive.zip", mime_type="application/zip", size=200),
            Attachment(filename="run.exe", mime_type="application/x-msdownload", size=300),
            Attachment(
                filename="macro.docm",
                mime_type="application/vnd.ms-word.document.macroEnabled.12",
                size=400,
            ),
            Attachment(filename="photo.jpg", mime_type="image/jpeg", size=50),
        ),
    )

    facts = _extract(email)

    assert set(facts.risky_attachments) == {"invoice.html", "archive.zip", "run.exe", "macro.docm"}
    assert "photo.jpg" not in facts.risky_attachments
    assert facts.bulk_cues


def test_facts_gmail_category_labels_passed_through() -> None:
    email = _make_email()

    facts = extract_facts(
        email,
        own_address="me@example.com",
        internal_domains=[],
        allowlist_senders=[],
        allowlist_domains=[],
        gmail_labels=("CATEGORY_PROMOTIONS", "INBOX", "UNREAD"),
    )

    assert facts.gmail_category_labels == ("CATEGORY_PROMOTIONS",)
    lines = facts.to_prompt_lines()
    assert any("CATEGORY_PROMOTIONS" in line for line in lines)


def test_facts_html_to_text_drops_scripts_styles() -> None:
    html = (
        "<html><head><style>body{color:red}</style></head><body>"
        "<p>Hello <script>alert('x')</script>world</p>"
        '<img src="http://example.com/pic.png" alt="pic">'
        '<a href="http://example.com">Click here</a>'
        "</body></html>"
    )

    text = html_to_text(html)

    assert "color:red" not in text
    assert "alert" not in text
    assert "pic.png" not in text
    assert "Hello" in text
    assert "world" in text
    assert "Click here (http://example.com)" in text


def test_facts_pass_authentication_line_states_domain_not_forged() -> None:
    """Task 52: a full SPF/DKIM/DMARC pass for the From domain is stated as an
    observation ("not forged"), never as an interpretive verdict ("genuine")."""
    email = _make_email(
        headers={
            "Authentication-Results": (
                "mx.google.com; dkim=pass header.i=@accounts.google.com "
                "header.s=20260707; spf=pass smtp.mailfrom=accounts.google.com; "
                "dmarc=pass header.from=accounts.google.com"
            )
        },
        from_address=EmailAddress(address="no-reply@accounts.google.com"),
    )

    facts = _extract(email)

    lines = facts.to_prompt_lines()
    auth_lines = [line for line in lines if "not forged" in line]
    assert len(auth_lines) == 1
    assert "SPF" in auth_lines[0]
    assert "DKIM" in auth_lines[0]
    assert "DMARC" in auth_lines[0]
    assert "accounts.google.com" in auth_lines[0]
    assert "genuine" not in "\n".join(lines).lower()
    # No failure line, and no "missing header" line, when everything passed.
    assert not any(line.startswith("Authentication:") for line in lines)
    assert not any("no Authentication-Results header" in line for line in lines)


def test_facts_failed_authentication_line_lists_results() -> None:
    email = _make_email(
        headers={
            "Authentication-Results": (
                "mx.google.com; dkim=fail header.d=acme.com; dmarc=fail header.from=acme.com"
            )
        },
        from_address=EmailAddress(address="sender@acme.com"),
    )

    facts = _extract(email)

    lines = facts.to_prompt_lines()
    assert any("dkim=fail" in line for line in lines)
    assert any("dmarc=fail" in line for line in lines)
    assert not any("genuine" in line.lower() or "not forged" in line for line in lines)


def test_facts_missing_auth_header_line() -> None:
    email = _make_email(from_address=EmailAddress(address="sender@acme.com"))

    facts = _extract(email)

    lines = facts.to_prompt_lines()
    assert lines == ["Sender authentication: no Authentication-Results header."]


def test_facts_automated_and_bulk_lines_have_no_interpretation() -> None:
    email = _make_email(
        headers={
            "Authentication-Results": (
                "mx.google.com; dkim=pass header.d=acme.com; dmarc=pass header.from=acme.com"
            ),
            "List-Unsubscribe": "<mailto:unsub@example.com>",
        },
        from_address=EmailAddress(address="no-reply@acme.com"),
        text_body="Thanks for subscribing! To unsubscribe, click here.",
    )

    facts = _extract(email)

    lines = "\n".join(facts.to_prompt_lines()).lower()
    assert "normal for" not in lines
    assert "legitimate" not in lines
    assert any(line.startswith("Automated sender: ") for line in facts.to_prompt_lines())
    assert any(line.startswith("Bulk-mail cue(s): ") for line in facts.to_prompt_lines())


def test_facts_link_hosts_listed_with_sender_domain() -> None:
    """A Google-notice-like body: both a bare URL and a `text (href)` link count,
    distinct hosts only, in order of appearance."""
    email = _make_email(
        from_address=EmailAddress(address="no-reply@accounts.google.com"),
        text_body=(
            "Verify your account activity "
            "(https://accounts.google.com/AccountChooser?Email=x&continue=1)\n"
            "You can also check https://myaccount.google.com/notifications directly."
        ),
    )

    facts = _extract(email)

    assert facts.link_hosts == ("accounts.google.com", "myaccount.google.com")
    lines = facts.to_prompt_lines()
    assert any(
        line == "Sender domain: accounts.google.com. Link hosts: accounts.google.com, "
        "myaccount.google.com"
        for line in lines
    )


def test_facts_no_links_no_link_hosts_line() -> None:
    email = _make_email(text_body="Hello, just checking in, no links here.")

    facts = _extract(email)

    assert facts.link_hosts == ()
    assert not any("Link hosts" in line for line in facts.to_prompt_lines())


@pytest.mark.parametrize(
    ("from_address", "expected"),
    [("x@gmail.com", True), ("x@acme.example", False)],
    ids=["gmail", "not-free-mail"],
)
def test_facts_free_mail_sender_fact(from_address: str, expected: bool) -> None:
    email = _make_email(from_address=EmailAddress(address=from_address))

    facts = _extract(email)

    assert facts.free_mail_sender is expected
    lines = facts.to_prompt_lines()
    assert any("public free-mail provider" in line for line in lines) is expected


def test_facts_calendar_part_fact() -> None:
    with_invite = _make_email(calendar_parts=(CalendarPart(raw_ics="BEGIN:VCALENDAR"),))
    without_invite = _make_email()

    assert _extract(with_invite).calendar_invitation is True
    assert _extract(without_invite).calendar_invitation is False
    assert any(
        "calendar invitation" in line.lower() for line in _extract(with_invite).to_prompt_lines()
    )
    assert not any(
        "calendar invitation" in line.lower() for line in _extract(without_invite).to_prompt_lines()
    )


def test_facts_link_text_subdomain_of_host_is_not_an_issue() -> None:
    email = _make_email(text_body="paypal.com (https://email.paypal.com/x)")

    facts = _extract(email)

    assert facts.link_issues == ()


def test_facts_link_text_other_domain_is_an_issue() -> None:
    email = _make_email(text_body="microsoft.com (http://micros0ft-billing.com/verify)")

    facts = _extract(email)

    assert any("microsoft.com" in issue.reason for issue in facts.link_issues)


def test_facts_truncation_flagged() -> None:
    long_text = "x" * 100

    truncated_text, truncated = truncate_for_llm(long_text, max_chars=20)

    assert truncated is True
    assert truncated_text == "x" * 20

    same_text, not_truncated = truncate_for_llm("short", max_chars=20)

    assert not_truncated is False
    assert same_text == "short"

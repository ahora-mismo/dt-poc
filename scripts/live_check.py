"""Live end-to-end check against the running agent (D-51).

An optional operator tool, never used during the build (the build is
verified offline, D-52). It sends one test email per row of the
requirements' behaviour table, plus a courtesy case and an explicit
meeting request with proposed times, waits for the running agent to pick
them up, and prints a checklist to verify by hand against Gmail (labels,
folders, sent replies, calendar events) and the agent's structured log on
stdout (D-57).

This talks to Gmail only over SMTP with an app password -- never the
Gmail API -- because it plays the part of an outside sender writing to the
agent's mailbox, not the agent itself.

Required env vars (script-only; not read by the app):
    LIVE_CHECK_SENDER        The Gmail address the test emails are sent
                              from (any Gmail account with an app password;
                              does not have to be the agent's own mailbox).
    LIVE_CHECK_APP_PASSWORD  A Gmail app password for that account
                              (https://myaccount.google.com/apppasswords).
    LIVE_CHECK_TARGET        The agent's monitored mailbox address.

Optional env vars:
    LIVE_CHECK_CASES         Comma-separated case names to send (e.g.
                              "marketing" or "marketing,invoice"); every
                              other case is skipped. Empty or unset: all
                              cases. Names: marketing, meeting_request,
                              sales_outreach, urgent_internal, invoice,
                              phishing, courtesy,
                              meeting_request_proposed_times.
    LIVE_CHECK_PAUSE_SECONDS Seconds to pause between two sends (default:
                              120), so the sender account never sends a
                              burst.
    LIVE_CHECK_WAIT_MINUTES  Minutes to wait after sending before printing
                              the checklist (default: 5). Set to 0 to skip
                              waiting and print the checklist immediately.

Usage:
    uv run --env-file .env python scripts/live_check.py
    LIVE_CHECK_CASES=marketing uv run --env-file .env python scripts/live_check.py

Cost note: each email triggers at least one LLM classification call
(D-36), plus extraction/drafting calls depending on the label -- a
handful of Haiku-tier requests per run, negligible cost.
"""

from __future__ import annotations

import os
import smtplib
import sys
import time
import uuid
from dataclasses import dataclass
from email.message import EmailMessage

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
DEFAULT_WAIT_MINUTES = 5.0
DEFAULT_PAUSE_SECONDS = 120.0


@dataclass(frozen=True)
class TestCase:
    """One test email, and what to look for once the agent has run."""

    name: str
    subject: str
    body: str
    expect: str


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        raise SystemExit(1)
    return value


def _wait_minutes() -> float:
    raw = os.environ.get("LIVE_CHECK_WAIT_MINUTES", "").strip()
    return float(raw) if raw else DEFAULT_WAIT_MINUTES


def _pause_seconds() -> float:
    raw = os.environ.get("LIVE_CHECK_PAUSE_SECONDS", "").strip()
    return float(raw) if raw else DEFAULT_PAUSE_SECONDS


def select_cases(cases: list[TestCase], selection: str) -> list[TestCase]:
    """The cases named in `selection` (comma-separated), in their usual order;
    all of them when `selection` is empty. Exits on an unknown name."""
    wanted = {name.strip() for name in selection.split(",") if name.strip()}
    if not wanted:
        return cases
    known = {case.name for case in cases}
    unknown = sorted(wanted - known)
    if unknown:
        print(
            f"Unknown case(s) in LIVE_CHECK_CASES: {', '.join(unknown)}. "
            f"Known: {', '.join(case.name for case in cases)}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return [case for case in cases if case.name in wanted]


def _tag(name: str) -> str:
    """A short, unique marker so this run's messages are easy to spot."""
    return f"[live-check {name} {uuid.uuid4().hex[:8]}]"


def build_cases() -> list[TestCase]:
    """One email per required-behaviour row, plus a courtesy case and an
    explicit meeting request with proposed times (D-51)."""
    return [
        TestCase(
            name="marketing",
            subject=f"{_tag('marketing')} 30% off our spring catalogue!",
            body=(
                "Hi there,\n\n"
                "Don't miss our spring sale -- 30% off everything storewide, "
                "this week only. Unsubscribe any time.\n\n"
                "The Sales Team"
            ),
            expect="MARKETING; archived automatically; no approval email.",
        ),
        TestCase(
            name="meeting_request",
            subject=f"{_tag('meeting_request')} Quick sync next week?",
            body=(
                "Hi,\n\n"
                "Could we grab 30 minutes next Tuesday at 2pm your time to "
                "discuss the Q3 roadmap? Let me know if that works.\n\n"
                "Best,\nJordan"
            ),
            expect=(
                "MEETING_REQUEST; approval email with slot option(s); "
                "APPROVE creates a calendar event."
            ),
        ),
        TestCase(
            name="sales_outreach",
            subject=f"{_tag('sales_outreach')} Cut your cloud bill by 40%",
            body=(
                "Hello,\n\n"
                "I noticed your company might benefit from our cost "
                "optimisation platform. Worth a 15-minute call this week?\n\n"
                "Regards,\nA Vendor"
            ),
            expect=(
                "SALES_OUTREACH; a polite decline reply is sent (or goes "
                "through approval, depending on SALES_DECLINE_REQUIRES_"
                "APPROVAL) and the email is archived."
            ),
        ),
        TestCase(
            name="urgent_internal",
            subject=f"{_tag('urgent_internal')} URGENT: prod issue, need you now",
            body=(
                "Production is down for some customers, need a decision "
                "from you in the next hour. Please call me back ASAP.\n\n"
                "Sam"
            ),
            expect=(
                "URGENT + TASK; Gmail IMPORTANT flag; approval email that "
                "doubles as the manager notification. Only fires this way "
                "if LIVE_CHECK_SENDER's domain is listed in INTERNAL_DOMAINS."
            ),
        ),
        TestCase(
            name="invoice",
            subject=f"{_tag('invoice')} Invoice #INV-10432 for September services",
            body=(
                "Dear customer,\n\n"
                "Please find attached invoice INV-10432 for September "
                "services, total $420.00, due in 30 days.\n\n"
                "Accounts Receivable"
            ),
            expect="INFO_ONLY (invoice subtype); archived to the Finance label.",
        ),
        TestCase(
            name="phishing",
            subject=f"{_tag('phishing')} Your account will be suspended - verify now",
            body=(
                "Dear user,\n\n"
                "We detected unusual activity on your account. Verify your "
                "identity within 24 hours or it will be suspended: "
                "http://secure-account-verify.example-phish.test/login\n\n"
                "Security Team"
            ),
            expect="SPAM; moved to the Spam folder; logged.",
        ),
        TestCase(
            name="courtesy",
            subject=f"{_tag('courtesy')} Thanks so much for your help last week",
            body=(
                "Hi,\n\n"
                "Just wanted to say thanks again for walking me through the "
                "onboarding last week -- really appreciated it, no need to "
                "reply.\n\nBest,\nAlex"
            ),
            expect=(
                "A non-SPAM/SALES_OUTREACH/MEETING_REQUEST label (e.g. "
                "INFO_ONLY) with a courtesy reply proposed (warranted=true); "
                "approval email; APPROVE sends the acknowledgement."
            ),
        ),
        TestCase(
            name="meeting_request_proposed_times",
            subject=f"{_tag('meeting_request_proposed_times')} Proposing times for our call",
            body=(
                "Hi,\n\n"
                "Following up -- could we do Wednesday at 10am or Thursday "
                "at 3pm, 45 minutes, to review the proposal? Either works "
                "for me.\n\nThanks,\nTaylor"
            ),
            expect=(
                "MEETING_REQUEST with both proposed times parsed into date "
                "expressions and resolved (D-64); approval email listing "
                "the requested option(s)."
            ),
        ),
    ]


def send(sender: str, password: str, target: str, case: TestCase) -> None:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = target
    message["Subject"] = case.subject
    message.set_content(case.body)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as client:
        client.login(sender, password)
        client.send_message(message)


def print_checklist(cases: list[TestCase]) -> None:
    print("\nSent. Checklist -- verify each by hand in Gmail and the agent's log:\n")
    for index, case in enumerate(cases, start=1):
        print(f"{index}. {case.name} -- {case.subject}")
        print(f"   expect: {case.expect}\n")


def main() -> int:
    sender = _env("LIVE_CHECK_SENDER")
    password = _env("LIVE_CHECK_APP_PASSWORD")
    target = _env("LIVE_CHECK_TARGET")
    wait_minutes = _wait_minutes()
    pause_seconds = _pause_seconds()

    cases = select_cases(build_cases(), os.environ.get("LIVE_CHECK_CASES", ""))

    print(f"Sending {len(cases)} test email(s) from {sender} to {target}...")
    for index, case in enumerate(cases):
        if index > 0 and pause_seconds > 0:
            print(f"  pausing {pause_seconds:.0f} s before the next send...")
            time.sleep(pause_seconds)
        send(sender, password, target, case)
        print(f"  sent: {case.name}")

    if wait_minutes > 0:
        print(f"\nWaiting {wait_minutes:.1f} minute(s) for the agent to process them...")
        time.sleep(wait_minutes * 60)

    print_checklist(cases)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Tests for the per-email flow (task 19, D-25, D-27, D-43, D-58, D-63).

Whole-flow tests with fakes, asserting on fake state (labels, sent
messages, store rows) and log lines via `structlog.testing.capture_logs`,
per the testing rules. Real `SqliteStore` in a temporary directory.

Test description format: test_<unit>_<condition>_<expected>.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from structlog.testing import capture_logs
from structlog.types import EventDict

from dt_agent.adapters.sqlite_store import SqliteStore, open_store
from dt_agent.config import Settings
from dt_agent.core.approval_token import (
    CalendarAction,
    InvitationAction,
    ReplyAction,
    VerifiedApproval,
    extract_token,
    verify,
)
from dt_agent.domain.classification import Classification
from dt_agent.domain.clock import FixedClock
from dt_agent.domain.dates import AbsoluteExpr, Weekday, WeekdayExpr, WindowExpr
from dt_agent.domain.email import CalendarPart, Email
from dt_agent.domain.labels import (
    AI_NEEDS_REVIEW_LABEL,
    FINANCE_LABEL,
    GMAIL_LABEL_ID_INBOX,
    GMAIL_LABEL_ID_SENT,
    GMAIL_LABEL_ID_SPAM,
    GMAIL_LABEL_ID_STARRED,
    URGENT_LABEL,
    PrimaryLabel,
    Subtype,
)
from dt_agent.domain.meeting import MeetingExtraction
from dt_agent.pipeline.deps import PipelineDeps
from dt_agent.pipeline.process_email import (
    URGENT_INTERNAL_HINT,
    ApprovalHandled,
    make_email_handler,
)
from dt_agent.ports.errors import LLMOutputError
from dt_agent.ports.llm import CourtesyDraft
from tests.fakes.calendar import FakeCalendar
from tests.fakes.emails import make_email
from tests.fakes.llm import (
    FakeAcknowledgementDrafter,
    FakeClassifier,
    FakeCourtesyDrafter,
    FakeMeetingExtractor,
    FakeReplyDrafter,
)
from tests.fakes.mail import FakeMail
from tests.fakes.settings import DEFAULT_NOW, MAILBOX, SIGNING_KEY, make_deps, make_settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "invitations"


def _store(tmp_path: Path) -> SqliteStore:
    store, report = open_store(tmp_path / "agent.db", FixedClock(datetime(2026, 9, 28, tzinfo=UTC)))
    assert report is None
    return store


async def _deny_approval_handler(
    deps: PipelineDeps, email: Email, thread: object
) -> ApprovalHandled:
    raise AssertionError("approval_handler must not be called for a CLASSIFY-routed message")


async def test_flow_approval_reply_delegated(tmp_path: Path) -> None:
    """An `APPROVAL_REPLY`-routed message is handed to the injected handler, then recorded."""
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)

    approval = make_email(
        id="approval-1",
        thread_id="thread-1",
        label_ids=("SENT", GMAIL_LABEL_ID_INBOX),
        from_address=MAILBOX,
        to=(MAILBOX,),
        subject="[Approval] Reply to sender",
        text_body=_approval_email_body(),
    )
    reply = make_email(
        id="reply-1",
        thread_id="thread-1",
        label_ids=(GMAIL_LABEL_ID_INBOX,),
        from_address=MAILBOX,
        to=(MAILBOX,),
        subject="Re: [Approval] Reply to sender",
        text_body="APPROVE",
    )
    mail.add_message(approval)
    mail.add_message(reply)

    deps = make_deps(store=store, mail=mail)

    calls: list[tuple[str, list[str]]] = []

    async def approval_handler(
        handler_deps: PipelineDeps, email: Email, thread: Sequence[Email]
    ) -> ApprovalHandled:
        calls.append((email.id, [message.id for message in thread]))
        return ApprovalHandled(outcome="approved", actions=("event_created",))

    handler = make_email_handler(deps, approval_handler)
    await handler("reply-1", 1)

    assert calls == [("reply-1", ["approval-1", "reply-1"])]
    assert await store.contains("reply-1") is True


def _approval_email_body() -> str:
    from zoneinfo import ZoneInfo

    from dt_agent.core.approval_token import ReplyAction, issue, render_token_block

    action = ReplyAction(
        recipient="sender@example.com", thread_id="thread-1", message_id="orig-1", text="Hi."
    )
    token = issue(
        key=SIGNING_KEY,
        sent_at=datetime(2026, 9, 28, 7, 15, tzinfo=UTC),
        business_days=3,
        tz=ZoneInfo("Europe/Bucharest"),
        original_email_id="orig-1",
        action=action,
        shown_text="Approve this reply",
    )
    return "Approve this reply\n\n" + render_token_block(token)


async def test_flow_invalid_llm_output_needs_review(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = make_email(id="msg-1")
    mail.add_message(email)

    classifier = FakeClassifier()
    classifier.fail_next(LLMOutputError("model never returned valid output"))
    deps = make_deps(store=store, mail=mail, classifier=classifier)

    handler = make_email_handler(deps, _deny_approval_handler)

    with capture_logs() as logs:
        await handler("msg-1", 1)

    updated = await mail.get_message("msg-1")
    assert AI_NEEDS_REVIEW_LABEL in updated.label_ids
    assert await store.contains("msg-1") is True

    processed = [line for line in logs if line.get("event") == "email.processed"]
    assert len(processed) == 1
    assert processed[0]["outcome"] == "needs_review"
    assert processed[0]["action"] == []


async def test_flow_newsletter_marketing_archived(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = make_email(
        id="msg-1",
        from_address="news@bulletin.example",
        subject="Your weekly digest",
        headers={"List-Unsubscribe": "<mailto:unsub@bulletin.example>"},
        text_body="Check out this week's stories. Unsubscribe at any time.",
    )
    mail.add_message(email)

    classifier = FakeClassifier()
    classifier.set_result(
        "msg-1",
        Classification(primary=PrimaryLabel.MARKETING, urgent=False, subtype=Subtype.NONE),
    )
    deps = make_deps(store=store, mail=mail, classifier=classifier)

    handler = make_email_handler(deps, _deny_approval_handler)
    await handler("msg-1", 1)

    updated = await mail.get_message("msg-1")
    assert "MARKETING" in updated.label_ids
    assert GMAIL_LABEL_ID_INBOX not in updated.label_ids
    assert mail.sent == []
    assert await store.contains("msg-1") is True


async def test_flow_meeting_request_creates_approval_email(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    received_at = datetime(2026, 9, 28, 7, 15, tzinfo=UTC)  # Mon 28 Sep 10:15 EEST
    email = make_email(
        id="msg-1",
        from_address="person@client.example",
        subject="Can we meet?",
        text_body="Are you free Tuesday at 14:00?",
        received_at=received_at,
    )
    mail.add_message(email)

    classifier = FakeClassifier()
    classifier.set_result(
        "msg-1",
        Classification(primary=PrimaryLabel.MEETING_REQUEST, urgent=False, subtype=Subtype.NONE),
    )
    meeting_extractor = FakeMeetingExtractor()
    meeting_extractor.set_result(
        "msg-1",
        MeetingExtraction(
            title="Sync",
            proposals=[AbsoluteExpr(phrase="Tuesday at 14:00", month=9, day=29, time=time(14, 0))],
        ),
    )
    deps = make_deps(
        store=store,
        mail=mail,
        clock=FixedClock(received_at),
        classifier=classifier,
        meeting_extractor=meeting_extractor,
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    await handler("msg-1", 1)

    updated = await mail.get_message("msg-1")
    assert "MEETING_REQUEST" in updated.label_ids
    assert GMAIL_LABEL_ID_INBOX in updated.label_ids  # not archived

    assert len(mail.sent) == 1
    sent = mail.sent[0]
    assert sent.to == MAILBOX
    assert "1." in sent.text_body

    from dt_agent.core.approval_token import extract_token

    token = extract_token(sent.text_body)
    assert token is not None
    verified = verify(token, SIGNING_KEY, now=received_at)
    assert isinstance(verified, VerifiedApproval)
    assert isinstance(verified.action, CalendarAction)
    assert len(verified.action.options) == 1

    assert await store.contains("sent-1") is True


async def test_process_email_two_windows_both_reach_scheduler(tmp_path: Path) -> None:
    """task 39 (F2): every window proposal reaches the scheduler, not just the first.
    Tuesday afternoon is entirely busy, so options can only come from the second window
    (Wednesday afternoon) -- under the old "keep only the first window" bug, the second
    window would never have been passed to `schedule()` at all, and the busy Tuesday
    window would instead have produced `OUTSIDE_REQUESTED_INTERVAL` alternatives starting
    Wednesday *morning* (09:00), not the requested afternoon."""
    bucharest = ZoneInfo("Europe/Bucharest")
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    calendar = FakeCalendar()
    calendar.add_busy(
        datetime(2026, 9, 29, 9, 0, tzinfo=bucharest),
        datetime(2026, 9, 29, 18, 0, tzinfo=bucharest),
        "Offsite",
    )
    received_at = datetime(2026, 9, 28, 7, 15, tzinfo=UTC)  # Mon 28 Sep 10:15 EEST
    email = make_email(
        id="msg-1",
        from_address="person@client.example",
        subject="Can we meet?",
        text_body="Tuesday or Wednesday afternoon?",
        received_at=received_at,
    )
    mail.add_message(email)

    classifier = FakeClassifier()
    classifier.set_result(
        "msg-1",
        Classification(primary=PrimaryLabel.MEETING_REQUEST, urgent=False, subtype=Subtype.NONE),
    )
    meeting_extractor = FakeMeetingExtractor()
    tue = WeekdayExpr(phrase="Tuesday", weekday=Weekday.TUE, which="this")
    wed = WeekdayExpr(phrase="Wednesday", weekday=Weekday.WED, which="this")
    meeting_extractor.set_result(
        "msg-1",
        MeetingExtraction(
            title="Sync",
            proposals=[
                WindowExpr(phrase="Tuesday afternoon", start=tue, end=tue, part_of_day="afternoon"),
                WindowExpr(
                    phrase="Wednesday afternoon", start=wed, end=wed, part_of_day="afternoon"
                ),
            ],
        ),
    )
    deps = make_deps(
        store=store,
        mail=mail,
        calendar=calendar,
        clock=FixedClock(received_at),
        classifier=classifier,
        meeting_extractor=meeting_extractor,
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    await handler("msg-1", 1)

    assert len(mail.sent) == 1
    sent = mail.sent[0]
    # Both requested times are shown (D-13); the busy Tuesday window gets no option and
    # no "outside the requested interval" warning appears -- Wednesday's free slot does.
    assert '- "Tuesday afternoon" — NOT AVAILABLE: no free time in that period' in sent.text_body
    assert '- "Wednesday afternoon" → Wed 30 Sep' in sent.text_body
    option_lines = [line for line in sent.text_body.splitlines() if line[:3] in ("1. ", "2. ")]
    assert option_lines
    assert all("Tuesday afternoon" not in line for line in option_lines)
    assert "OUTSIDE THE REQUESTED INTERVAL" not in sent.text_body
    assert '"Wednesday afternoon"' in sent.text_body

    token = extract_token(sent.text_body)
    assert token is not None
    verified = verify(token, SIGNING_KEY, now=received_at)
    assert isinstance(verified, VerifiedApproval)
    assert isinstance(verified.action, CalendarAction)

    options = verified.action.options
    assert options
    for option in options:
        assert option.start.astimezone(bucharest).date() == date(2026, 9, 30)
        assert option.start.astimezone(bucharest).time() >= time(12, 0)


async def test_flow_meeting_extraction_invalid_needs_review(tmp_path: Path) -> None:
    """D-43: invalid LLM output past the classifier is `AI/NeedsReview`, not `no_slots_found`."""
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = make_email(id="msg-1", from_address="person@client.example", subject="Can we meet?")
    mail.add_message(email)

    classifier = FakeClassifier()
    classifier.set_result(
        "msg-1",
        Classification(primary=PrimaryLabel.MEETING_REQUEST, urgent=False, subtype=Subtype.NONE),
    )
    meeting_extractor = FakeMeetingExtractor()
    meeting_extractor.fail_next(LLMOutputError("model never returned valid output"))
    deps = make_deps(
        store=store, mail=mail, classifier=classifier, meeting_extractor=meeting_extractor
    )

    handler = make_email_handler(deps, _deny_approval_handler)

    with capture_logs() as logs:
        await handler("msg-1", 1)

    updated = await mail.get_message("msg-1")
    assert "MEETING_REQUEST" in updated.label_ids
    assert AI_NEEDS_REVIEW_LABEL in updated.label_ids
    assert mail.sent == []
    assert await store.contains("msg-1") is True

    processed = [line for line in logs if line.get("event") == "email.processed"]
    assert len(processed) == 1
    assert processed[0]["outcome"] == "needs_review"
    assert "needs_review" in processed[0]["action"]


async def test_flow_sales_outreach_decline_sent_and_archived(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = make_email(
        id="msg-1",
        from_address="rep@vendor.example",
        subject="Great opportunity for your business",
        text_body="We'd love to set up a call to show you our product.",
    )
    mail.add_message(email)

    classifier = FakeClassifier()
    classifier.set_result(
        "msg-1",
        Classification(primary=PrimaryLabel.SALES_OUTREACH, urgent=False, subtype=Subtype.NONE),
    )
    reply_drafter = FakeReplyDrafter()
    reply_drafter.set_result(
        "msg-1", "Thanks so much for reaching out -- we're not pursuing this at the moment."
    )
    deps = make_deps(store=store, mail=mail, classifier=classifier, reply_drafter=reply_drafter)

    handler = make_email_handler(deps, _deny_approval_handler)
    await handler("msg-1", 1)

    updated = await mail.get_message("msg-1")
    assert "SALES_OUTREACH" in updated.label_ids
    assert GMAIL_LABEL_ID_INBOX not in updated.label_ids

    assert len(mail.sent) == 1
    sent = mail.sent[0]
    assert sent.to == "rep@vendor.example"
    assert sent.thread_id == email.thread_id
    assert "not pursuing this" in sent.text_body

    assert await store.contains("sent-1") is True


def test_urgent_internal_hint_names_nobody() -> None:
    """D-40 (updated): the acknowledgement hint names nobody -- not the configured
    `AGENT_SENDER_NAME` (the manager's own name in one mode, an assistant's name like
    "Alex's Assistant" in the other) and no "brought to ... attention" phrasing."""
    settings = make_settings()
    assert settings.AGENT_SENDER_NAME not in URGENT_INTERNAL_HINT
    assert "attention" not in URGENT_INTERNAL_HINT.lower()


_URGENT_INTERNAL_AUTH_HEADERS = {
    "Authentication-Results": (
        "mx.google.com; dkim=pass header.d=acme.com; dmarc=pass header.from=acme.com"
    )
}


def _urgent_internal_email(
    *,
    id: str = "msg-1",
    thread_id: str | None = None,
    from_address: str = "alice@acme.com",
    subject: str = "Server is down",
    text_body: str = "Prod is down, need this looked at urgently.",
) -> Email:
    return make_email(
        id=id,
        thread_id=thread_id,
        from_address=from_address,
        subject=subject,
        headers=_URGENT_INTERNAL_AUTH_HEADERS,
        text_body=text_body,
    )


def _urgent_internal_deps(
    *,
    store: SqliteStore,
    mail: FakeMail,
    acknowledgement_drafter: FakeAcknowledgementDrafter | None = None,
    email_id: str = "msg-1",
    settings: Settings | None = None,
    classification: Classification | None = None,
) -> PipelineDeps:
    classifier = FakeClassifier()
    classifier.set_result(
        email_id,
        classification
        or Classification(primary=PrimaryLabel.TASK, urgent=True, subtype=Subtype.NONE),
    )
    return make_deps(
        store=store,
        mail=mail,
        settings=settings,
        classifier=classifier,
        acknowledgement_drafter=acknowledgement_drafter or FakeAcknowledgementDrafter(),
    )


async def test_process_email_urgent_internal_sends_notification_first_approval(
    tmp_path: Path,
) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = _urgent_internal_email()
    mail.add_message(email)

    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.set_result("msg-1", "Got it, thanks -- flagged as urgent.")
    deps = _urgent_internal_deps(
        store=store, mail=mail, acknowledgement_drafter=acknowledgement_drafter
    )

    handler = make_email_handler(deps, _deny_approval_handler)

    with capture_logs() as logs:
        await handler("msg-1", 1)

    updated = await mail.get_message("msg-1")
    assert "TASK" in updated.label_ids
    assert URGENT_LABEL in updated.label_ids
    assert GMAIL_LABEL_ID_STARRED in updated.label_ids

    assert len(mail.sent) == 1
    sent = mail.sent[0]
    # D-40: notification-first subject -- the approval email doubles as the notification.
    assert sent.subject.startswith("[Approval] URGENT from")
    assert "Server is down" in sent.subject
    assert "reply APPROVE to send the acknowledgement" in sent.subject
    assert "Flagged URGENT + starred." in sent.text_body
    assert "Got it, thanks -- flagged as urgent." in sent.text_body

    # D-40/D-11: the token itself carries `urgent`, not just the subject,
    # so a later re-issue (task 40) can keep the marking.
    token = extract_token(sent.text_body)
    assert token is not None
    verified = verify(token, SIGNING_KEY, now=DEFAULT_NOW)
    assert isinstance(verified, VerifiedApproval)
    assert isinstance(verified.action, ReplyAction)
    assert verified.action.urgent is True

    processed = [line for line in logs if line.get("event") == "email.processed"]
    assert len(processed) == 1
    assert "notify_manager" in processed[0]["action"]
    notify_lines = [line for line in logs if line.get("event") == "process_email.notify_manager"]
    assert len(notify_lines) == 1
    assert notify_lines[0]["branch"] == "approval_sent"

    assert await store.contains("sent-1") is True


async def test_process_email_urgent_internal_approval_includes_deadline_line(
    tmp_path: Path,
) -> None:
    """D-24/D-40 (task 47): the classification's deadline threads through to the urgent
    acknowledgement's approval email as a `Deadline:` line."""
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = _urgent_internal_email()
    mail.add_message(email)

    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.set_result("msg-1", "Got it, thanks -- flagged as urgent.")
    deadline_at = datetime(2026, 9, 29, 20, 59, tzinfo=UTC)  # Tue 23:59 EEST
    deps = _urgent_internal_deps(
        store=store,
        mail=mail,
        acknowledgement_drafter=acknowledgement_drafter,
        classification=Classification(
            primary=PrimaryLabel.TASK,
            urgent=True,
            subtype=Subtype.NONE,
            deadline_phrase="by tomorrow",
            deadline_at=deadline_at,
        ),
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    with capture_logs() as logs:
        await handler("msg-1", 1)

    assert len(mail.sent) == 1
    sent = mail.sent[0]
    assert "Deadline: by tomorrow" in sent.text_body

    # D-24/D-58 (task 47): the audit `email.processed` line records the resolved
    # deadline too, as ISO 8601, not just the approval email's own rendering of it.
    processed = [line for line in logs if line.get("event") == "email.processed"]
    assert len(processed) == 1
    assert processed[0]["deadline_phrase"] == "by tomorrow"
    assert processed[0]["deadline_at"] == deadline_at.isoformat()


async def test_flow_urgent_internal_automated_sender_plain_notification(tmp_path: Path) -> None:
    # D-40: when the automated-sender guard blocks the acknowledgement reply,
    # the manager still gets a plain notification (no approval token, no reply drafted).
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = make_email(
        id="msg-1",
        from_address="alerts@acme.com",
        subject="CRITICAL: disk full",
        headers={
            "Authentication-Results": (
                "mx.google.com; dkim=pass header.d=acme.com; dmarc=pass header.from=acme.com"
            ),
            "Auto-Submitted": "auto-generated",
        },
        text_body="Disk usage at 99% on prod-db-1.",
    )
    mail.add_message(email)

    acknowledgement_drafter = FakeAcknowledgementDrafter()
    deps = _urgent_internal_deps(
        store=store, mail=mail, acknowledgement_drafter=acknowledgement_drafter
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    await handler("msg-1", 1)

    assert acknowledgement_drafter.calls == []
    assert len(mail.sent) == 1
    notification = mail.sent[0]
    assert notification.to == MAILBOX
    assert extract_token(notification.text_body) is None


async def test_process_email_urgent_internal_empty_draft_sends_plain_notification(
    tmp_path: Path,
) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = _urgent_internal_email()
    mail.add_message(email)

    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.set_result("msg-1", "   ")
    deps = _urgent_internal_deps(
        store=store, mail=mail, acknowledgement_drafter=acknowledgement_drafter
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    with capture_logs() as logs:
        await handler("msg-1", 1)

    assert len(mail.sent) == 1
    notification = mail.sent[0]
    assert notification.to == MAILBOX
    assert extract_token(notification.text_body) is None

    updated = await mail.get_message("msg-1")
    assert AI_NEEDS_REVIEW_LABEL in updated.label_ids

    processed = [line for line in logs if line.get("event") == "email.processed"]
    assert processed[0]["outcome"] == "needs_review"


async def test_process_email_urgent_internal_draft_error_sends_plain_notification(
    tmp_path: Path,
) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = _urgent_internal_email()
    mail.add_message(email)

    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.fail_next(LLMOutputError("boom"))
    deps = _urgent_internal_deps(
        store=store, mail=mail, acknowledgement_drafter=acknowledgement_drafter
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    with capture_logs() as logs:
        await handler("msg-1", 1)

    assert len(mail.sent) == 1
    notification = mail.sent[0]
    assert notification.to == MAILBOX
    assert extract_token(notification.text_body) is None

    updated = await mail.get_message("msg-1")
    assert AI_NEEDS_REVIEW_LABEL in updated.label_ids

    processed = [line for line in logs if line.get("event") == "email.processed"]
    assert processed[0]["outcome"] == "needs_review"


async def test_process_email_urgent_internal_sender_is_approver_no_ack(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = _urgent_internal_email(from_address="manager@acme.com")
    mail.add_message(email)

    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.set_result("msg-1", "Got it, thanks -- flagged as urgent.")
    settings = make_settings(MANAGER_EMAIL="manager@acme.com")
    deps = _urgent_internal_deps(
        store=store,
        mail=mail,
        acknowledgement_drafter=acknowledgement_drafter,
        settings=settings,
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    with capture_logs() as logs:
        await handler("msg-1", 1)

    assert acknowledgement_drafter.calls == []
    assert len(mail.sent) == 1
    notification = mail.sent[0]
    assert extract_token(notification.text_body) is None

    notify_lines = [line for line in logs if line.get("event") == "process_email.notify_manager"]
    assert len(notify_lines) == 1
    assert notify_lines[0]["branch"] == "sender_is_approver"


async def test_process_email_urgent_internal_already_replied_in_thread_no_second_ack(
    tmp_path: Path,
) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    prior_reply = make_email(
        id="prior-sent",
        thread_id="thread-1",
        from_address=MAILBOX,
        label_ids=[GMAIL_LABEL_ID_SENT],
        subject="Re: Server is down",
        text_body="Got it, thanks -- flagged as urgent.",
    )
    mail.add_message(prior_reply)
    email = _urgent_internal_email(id="msg-2", thread_id="thread-1")
    mail.add_message(email)

    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.set_result("msg-2", "Got it, thanks -- flagged as urgent, again.")
    deps = _urgent_internal_deps(
        store=store,
        mail=mail,
        acknowledgement_drafter=acknowledgement_drafter,
        email_id="msg-2",
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    with capture_logs() as logs:
        await handler("msg-2", 1)

    assert acknowledgement_drafter.calls == []
    assert len(mail.sent) == 1
    notification = mail.sent[0]
    assert extract_token(notification.text_body) is None

    notify_lines = [line for line in logs if line.get("event") == "process_email.notify_manager"]
    assert len(notify_lines) == 1
    assert notify_lines[0]["branch"] == "already_replied"


async def test_process_email_urgent_internal_logs_notify_manager_every_branch(
    tmp_path: Path,
) -> None:
    """D-40/task 43: every branch of the urgent-internal flow logs exactly one
    `process_email.notify_manager` line -- approval sent, draft failed, draft empty,
    sender is the approver, already replied, and the automated-sender guard."""

    async def _run(mail: FakeMail, email: Email, deps: PipelineDeps) -> list[EventDict]:
        mail.add_message(email)
        handler = make_email_handler(deps, _deny_approval_handler)
        with capture_logs() as logs:
            await handler(email.id, 1)
        return [line for line in logs if line.get("event") == "process_email.notify_manager"]

    # approval sent
    mail = FakeMail(address=MAILBOX)
    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.set_result("msg-1", "Got it, thanks -- flagged as urgent.")
    deps = _urgent_internal_deps(
        store=_store(tmp_path / "a"), mail=mail, acknowledgement_drafter=acknowledgement_drafter
    )
    lines = await _run(mail, _urgent_internal_email(), deps)
    assert len(lines) == 1 and lines[0]["branch"] == "approval_sent"

    # draft failed
    mail = FakeMail(address=MAILBOX)
    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.fail_next(LLMOutputError("boom"))
    deps = _urgent_internal_deps(
        store=_store(tmp_path / "b"), mail=mail, acknowledgement_drafter=acknowledgement_drafter
    )
    lines = await _run(mail, _urgent_internal_email(), deps)
    assert len(lines) == 1 and lines[0]["branch"] == "draft_failed"

    # draft empty
    mail = FakeMail(address=MAILBOX)
    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.set_result("msg-1", "")
    deps = _urgent_internal_deps(
        store=_store(tmp_path / "c"), mail=mail, acknowledgement_drafter=acknowledgement_drafter
    )
    lines = await _run(mail, _urgent_internal_email(), deps)
    assert len(lines) == 1 and lines[0]["branch"] == "draft_empty"

    # sender is the approver
    mail = FakeMail(address=MAILBOX)
    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.set_result("msg-1", "Got it, thanks -- flagged as urgent.")
    deps = _urgent_internal_deps(
        store=_store(tmp_path / "d"),
        mail=mail,
        acknowledgement_drafter=acknowledgement_drafter,
        settings=make_settings(MANAGER_EMAIL="alice@acme.com"),
    )
    lines = await _run(mail, _urgent_internal_email(from_address="alice@acme.com"), deps)
    assert len(lines) == 1 and lines[0]["branch"] == "sender_is_approver"

    # already replied in this thread
    mail = FakeMail(address=MAILBOX)
    mail.add_message(
        make_email(
            id="prior-sent",
            thread_id="thread-1",
            from_address=MAILBOX,
            label_ids=[GMAIL_LABEL_ID_SENT],
            subject="Re: Server is down",
            text_body="Already replied.",
        )
    )
    acknowledgement_drafter = FakeAcknowledgementDrafter()
    acknowledgement_drafter.set_result("msg-2", "Got it, thanks -- flagged as urgent.")
    deps = _urgent_internal_deps(
        store=_store(tmp_path / "e"),
        mail=mail,
        acknowledgement_drafter=acknowledgement_drafter,
        email_id="msg-2",
    )
    lines = await _run(mail, _urgent_internal_email(id="msg-2", thread_id="thread-1"), deps)
    assert len(lines) == 1 and lines[0]["branch"] == "already_replied"

    # automated-sender guard (not URGENT_INTERNAL_ACK flow; NONE flow, notify_manager=True)
    mail = FakeMail(address=MAILBOX)
    acknowledgement_drafter = FakeAcknowledgementDrafter()
    deps = _urgent_internal_deps(
        store=_store(tmp_path / "f"), mail=mail, acknowledgement_drafter=acknowledgement_drafter
    )
    automated_email = make_email(
        id="msg-1",
        from_address="alerts@acme.com",
        subject="CRITICAL: disk full",
        headers={**_URGENT_INTERNAL_AUTH_HEADERS, "Auto-Submitted": "auto-generated"},
        text_body="Disk usage at 99% on prod-db-1.",
    )
    lines = await _run(mail, automated_email, deps)
    assert len(lines) == 1 and lines[0]["branch"] == "automated_sender"


async def test_process_email_courtesy_already_replied_in_thread_no_second_reply(
    tmp_path: Path,
) -> None:
    """D-39 order step 3: one courtesy reply per thread -- when the agent already sent a
    real reply in this thread, the courtesy drafter is never called and no approval goes
    out, matching how the urgent-internal flow (D-40) uses `_agent_replied_in_thread`."""
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    prior_reply = make_email(
        id="prior-sent",
        thread_id="thread-1",
        from_address=MAILBOX,
        label_ids=[GMAIL_LABEL_ID_SENT],
        subject="Re: Report attached",
        text_body="Thanks for sending the report over.",
    )
    mail.add_message(prior_reply)
    email = make_email(
        id="msg-2",
        thread_id="thread-1",
        from_address="robin@acme.example",
        subject="Report attached",
        text_body="Here is the report you asked for, let me know if it looks good.",
    )
    mail.add_message(email)

    classifier = FakeClassifier()
    classifier.set_result(
        "msg-2",
        Classification(primary=PrimaryLabel.TASK, urgent=False, subtype=Subtype.NONE),
    )
    courtesy_drafter = FakeCourtesyDrafter()
    courtesy_drafter.set_result(
        "msg-2",
        CourtesyDraft(warranted=True, reason="shared a report", body="Thanks for the report."),
    )
    deps = make_deps(
        store=store, mail=mail, classifier=classifier, courtesy_drafter=courtesy_drafter
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    with capture_logs() as logs:
        await handler("msg-2", 1)

    assert courtesy_drafter.calls == []
    assert mail.sent == []

    skip_lines = [
        line for line in logs if line.get("event") == "process_email.courtesy_already_replied"
    ]
    assert len(skip_lines) == 1
    assert skip_lines[0]["message_id"] == "msg-2"


async def test_process_email_courtesy_logs_reason(tmp_path: Path) -> None:
    """D-39 (task 47): the courtesy decision's `reason` is written to the `email.processed`
    audit line for both outcomes -- warranted (reply approval sent) and not warranted."""
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    warranted_email = make_email(
        id="msg-1",
        from_address="robin@acme.example",
        subject="Report attached",
        text_body="Here is the report you asked for.",
    )
    not_warranted_email = make_email(
        id="msg-2",
        from_address="dana@acme.example",
        subject="Status?",
        text_body="What's the status of the migration?",
    )
    mail.add_message(warranted_email)
    mail.add_message(not_warranted_email)

    classifier = FakeClassifier()
    classifier.set_result(
        "msg-1",
        Classification(primary=PrimaryLabel.TASK, urgent=False, subtype=Subtype.NONE),
    )
    classifier.set_result(
        "msg-2",
        Classification(primary=PrimaryLabel.TASK, urgent=False, subtype=Subtype.NONE),
    )
    courtesy_drafter = FakeCourtesyDrafter()
    courtesy_drafter.set_result(
        "msg-1",
        CourtesyDraft(warranted=True, reason="shared a report", body="Thanks for the report."),
    )
    courtesy_drafter.set_result(
        "msg-2",
        CourtesyDraft(warranted=False, reason="only a status question", body=""),
    )
    deps = make_deps(
        store=store, mail=mail, classifier=classifier, courtesy_drafter=courtesy_drafter
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    with capture_logs() as logs:
        await handler("msg-1", 1)
        await handler("msg-2", 1)

    processed = {
        line["message_id"]: line for line in logs if line.get("event") == "email.processed"
    }
    assert processed["msg-1"]["outcome"] == "approval_requested"
    assert processed["msg-1"]["reason"] == "shared a report"
    assert processed["msg-2"]["outcome"] == "courtesy_skipped"
    assert processed["msg-2"]["reason"] == "only a status question"
    # D-24 (task 47): no deadline on either classification -- the audit line omits both
    # fields entirely, the same convention as `reason` when it's `None`.
    assert "deadline_phrase" not in processed["msg-1"]
    assert "deadline_at" not in processed["msg-1"]


async def test_flow_invoice_finance_archived(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    email = make_email(
        id="msg-1",
        from_address="no-reply@vendor.example",
        subject="Invoice #12345",
        text_body="Please find attached your invoice for last month's service.",
    )
    mail.add_message(email)

    classifier = FakeClassifier()
    classifier.set_result(
        "msg-1",
        Classification(primary=PrimaryLabel.INFO_ONLY, urgent=False, subtype=Subtype.FINANCE),
    )
    deps = make_deps(store=store, mail=mail, classifier=classifier)

    handler = make_email_handler(deps, _deny_approval_handler)
    await handler("msg-1", 1)

    updated = await mail.get_message("msg-1")
    assert "INFO_ONLY" in updated.label_ids
    assert FINANCE_LABEL in updated.label_ids
    assert GMAIL_LABEL_ID_INBOX not in updated.label_ids
    assert mail.sent == []
    assert await store.contains("msg-1") is True


_NOT_SPAM = Classification(primary=PrimaryLabel.TASK, urgent=False, subtype=Subtype.NONE)
_SPAM = Classification(primary=PrimaryLabel.SPAM, urgent=False, subtype=Subtype.NONE)


@dataclass
class _SpamRun:
    mail: FakeMail
    calendar: FakeCalendar
    meeting_extractor: FakeMeetingExtractor
    reply_drafter: FakeReplyDrafter
    courtesy_drafter: FakeCourtesyDrafter
    acknowledgement_drafter: FakeAcknowledgementDrafter
    logs: list[EventDict]


async def _run_spam_flow(tmp_path: Path, email: Email) -> _SpamRun:
    """Classify `email` as SPAM and run the handler with fakes recording every port."""
    mail = FakeMail(address=MAILBOX)
    mail.add_message(email)
    run = _SpamRun(
        mail=mail,
        calendar=FakeCalendar(),
        meeting_extractor=FakeMeetingExtractor(),
        reply_drafter=FakeReplyDrafter(),
        courtesy_drafter=FakeCourtesyDrafter(),
        acknowledgement_drafter=FakeAcknowledgementDrafter(),
        logs=[],
    )
    deps = make_deps(
        store=_store(tmp_path),
        mail=mail,
        calendar=run.calendar,
        classifier=FakeClassifier(default=_SPAM),
        meeting_extractor=run.meeting_extractor,
        reply_drafter=run.reply_drafter,
        courtesy_drafter=run.courtesy_drafter,
        acknowledgement_drafter=run.acknowledgement_drafter,
    )
    handler = make_email_handler(deps, _deny_approval_handler)
    with capture_logs() as logs:
        await handler(email.id, 1)
    run.logs.extend(logs)
    return run


async def test_flow_spam_moves_to_spam_and_nothing_else_runs(tmp_path: Path) -> None:
    """D-44: a SPAM classification gets only the spam action -- no other label, no draft,
    no reply, no calendar access, one `email.processed` line."""
    email = make_email(
        id="spam-1",
        from_address="adewale.okonkwo1962@gmail.com",
        subject="URGENT BUSINESS PROPOSAL",
        text_body="Send your bank details and a processing fee to release US$18.5M.",
        headers={
            "Authentication-Results": (
                "mx.google.com; spf=pass smtp.mailfrom=gmail.com; dkim=pass "
                "header.d=gmail.com; dmarc=pass header.from=gmail.com"
            )
        },
    )
    run = await _run_spam_flow(tmp_path, email)

    updated = await run.mail.get_message("spam-1")
    assert set(updated.label_ids) == {GMAIL_LABEL_ID_SPAM}
    assert run.mail.sent == []
    assert run.meeting_extractor.calls == []
    assert run.reply_drafter.calls == []
    assert run.courtesy_drafter.calls == []
    assert run.acknowledgement_drafter.calls == []
    assert run.calendar.inserted == []
    processed = [entry for entry in run.logs if entry["event"] == "email.processed"]
    assert len(processed) == 1
    assert processed[0]["label"] == "SPAM"
    assert processed[0]["action"] == ["move_to_spam"]


async def test_flow_spam_calendar_invitation_goes_to_spam(tmp_path: Path) -> None:
    """D-44/D-48: spam is decided before invitation detection; a spam invitation gets no
    approval email and no calendar action."""
    ics_text = (FIXTURES / "request_part.ics").read_text()
    email = make_email(
        id="spam-invite-1",
        from_address="alice@example.com",
        subject="Invitation: verify your identity to join",
        calendar_parts=(CalendarPart(raw_ics=ics_text),),
    )
    run = await _run_spam_flow(tmp_path, email)

    updated = await run.mail.get_message("spam-invite-1")
    assert set(updated.label_ids) == {GMAIL_LABEL_ID_SPAM}
    assert run.mail.sent == []
    assert run.calendar.inserted == []
    assert run.calendar.imported == []


async def test_flow_invitation_detected_by_code(tmp_path: Path) -> None:
    """An `.ics` REQUEST is classified first (not spam), then detected by code: the fixed
    MEETING_REQUEST replaces the classifier's label and the extractor is never called."""
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    ics_text = (FIXTURES / "request_part.ics").read_text()
    email = make_email(
        id="invite-1",
        from_address="alice@example.com",
        subject="Invitation: Quarterly Planning Sync",
        calendar_parts=(CalendarPart(raw_ics=ics_text),),
    )
    mail.add_message(email)

    classifier = FakeClassifier(default=_NOT_SPAM)
    meeting_extractor = FakeMeetingExtractor()
    deps = make_deps(
        store=store, mail=mail, classifier=classifier, meeting_extractor=meeting_extractor
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    await handler("invite-1", 1)

    assert len(classifier.calls) == 1
    assert meeting_extractor.calls == []

    updated = await mail.get_message("invite-1")
    assert "MEETING_REQUEST" in updated.label_ids

    assert len(mail.sent) == 1
    sent = mail.sent[0]

    from dt_agent.core.approval_token import extract_token

    token = extract_token(sent.text_body)
    assert token is not None
    verified = verify(token, SIGNING_KEY, now=datetime(2026, 9, 28, tzinfo=UTC))
    assert isinstance(verified, VerifiedApproval)
    assert isinstance(verified.action, InvitationAction)
    assert verified.action.ical_uid == "event-part-001@example.com"

    assert await store.contains("invite-1") is True
    assert await store.contains("sent-1") is True


async def test_flow_invitation_busy_gets_double_booking_warning(tmp_path: Path) -> None:
    """D-48: a received invitation whose time overlaps a busy event on the mailbox's own
    calendar gets an approval email with a double-booking warning naming the conflict --
    no numbered alternatives (it's someone else's invitation, not our own meeting)."""
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    calendar = FakeCalendar()
    bucharest = ZoneInfo("Europe/Bucharest")
    # The fixture's invitation (below) runs 2026-10-05 14:00-15:00 Europe/Bucharest.
    calendar.add_busy(
        datetime(2026, 10, 5, 14, 0, tzinfo=bucharest),
        datetime(2026, 10, 5, 15, 0, tzinfo=bucharest),
        "Board meeting",
    )
    ics_text = (FIXTURES / "request_part.ics").read_text()
    email = make_email(
        id="invite-1",
        from_address="alice@example.com",
        subject="Invitation: Quarterly Planning Sync",
        calendar_parts=(CalendarPart(raw_ics=ics_text),),
    )
    mail.add_message(email)

    classifier = FakeClassifier(default=_NOT_SPAM)
    meeting_extractor = FakeMeetingExtractor()
    deps = make_deps(
        store=store,
        mail=mail,
        calendar=calendar,
        classifier=classifier,
        meeting_extractor=meeting_extractor,
    )

    handler = make_email_handler(deps, _deny_approval_handler)
    await handler("invite-1", 1)

    assert len(mail.sent) == 1
    sent = mail.sent[0]
    assert "DOUBLE BOOKING" in sent.text_body
    assert "Board meeting" in sent.text_body
    assert "Options:" not in sent.text_body
    assert "APPROVE" in sent.text_body
    assert "REJECT" in sent.text_body


async def test_flow_invitation_own_event_is_not_a_double_booking(tmp_path: Path) -> None:
    """D-48: Google places a received invitation in the calendar (awaiting a response)
    before the agent sees the email; that event is the invitation itself, not a conflict."""
    mail = FakeMail(address=MAILBOX)
    calendar = FakeCalendar()
    bucharest = ZoneInfo("Europe/Bucharest")
    calendar.add_busy(
        datetime(2026, 10, 5, 14, 0, tzinfo=bucharest),
        datetime(2026, 10, 5, 15, 0, tzinfo=bucharest),
        "Quarterly Planning Sync",
        ical_uid="event-part-001@example.com",
    )
    ics_text = (FIXTURES / "request_part.ics").read_text()
    mail.add_message(
        make_email(
            id="invite-1",
            from_address="alice@example.com",
            subject="Invitation: Quarterly Planning Sync",
            calendar_parts=(CalendarPart(raw_ics=ics_text),),
        )
    )
    deps = make_deps(
        store=_store(tmp_path),
        mail=mail,
        calendar=calendar,
        classifier=FakeClassifier(default=_NOT_SPAM),
    )

    await make_email_handler(deps, _deny_approval_handler)("invite-1", 1)

    assert len(mail.sent) == 1
    assert "DOUBLE BOOKING" not in mail.sent[0].text_body


async def test_flow_email_processed_line_logged_once_per_email(tmp_path: Path) -> None:
    mail = FakeMail(address=MAILBOX)
    store = _store(tmp_path)
    first = make_email(
        id="msg-1",
        from_address="news@bulletin.example",
        subject="Digest",
        headers={"List-Unsubscribe": "<mailto:unsub@bulletin.example>"},
    )
    second = make_email(id="msg-2", from_address="rep@vendor.example", subject="Let's talk")
    mail.add_message(first)
    mail.add_message(second)

    classifier = FakeClassifier()
    classifier.set_result(
        "msg-1",
        Classification(primary=PrimaryLabel.MARKETING, urgent=False, subtype=Subtype.NONE),
    )
    classifier.set_result(
        "msg-2",
        Classification(primary=PrimaryLabel.SALES_OUTREACH, urgent=False, subtype=Subtype.NONE),
    )
    reply_drafter = FakeReplyDrafter()
    reply_drafter.set_result("msg-2", "Thanks, but not right now.")
    deps = make_deps(store=store, mail=mail, classifier=classifier, reply_drafter=reply_drafter)

    handler = make_email_handler(deps, _deny_approval_handler)

    with capture_logs() as logs:
        await handler("msg-1", 1)
        await handler("msg-2", 1)

    processed = [line for line in logs if line.get("event") == "email.processed"]
    assert len(processed) == 2
    assert {line["message_id"] for line in processed} == {"msg-1", "msg-2"}

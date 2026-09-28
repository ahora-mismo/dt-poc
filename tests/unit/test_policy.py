"""Unit tests for dt_agent.core.policy (task 13, D-25, D-38, D-39, D-40, D-44, D-48, D-63).

Per the testing rules, this covers the policy decision itself: which
label(s)/actions the required-behaviour table assigns per primary label,
the SALES_DECLINE_REQUIRES_APPROVAL switch, the shared automated-sender
guard skipping a decline/courtesy reply (with the reason logged), the
urgent-internal-task flow, the SPAM/MEETING_REQUEST interaction (SPAM
always wins), and that one email never produces more than one approval
flow. It does not re-test `extract_facts` (owned by task 11) or
`detect_invitation` (task 14): facts/invitations are built directly here
as plain inputs.
"""

from dataclasses import replace
from datetime import UTC, datetime

from dt_agent.core.facts import AuthResult, EmailFacts
from dt_agent.core.invitations import Invitation, InvitationKind, InvitationResult
from dt_agent.core.policy import Flow, decide
from dt_agent.domain.classification import Classification
from dt_agent.domain.labels import FINANCE_LABEL, URGENT_LABEL, PrimaryLabel, Subtype

_NOT_AUTOMATED = EmailFacts(
    sender_domain="example.com",
    auth=AuthResult(),
    internal=False,
    internal_reason=None,
    allowlisted=False,
    allowlist_reason=None,
    automated_reasons=(),
    reply_to_domain=None,
    reply_to_mismatch=False,
    impersonation_reason=None,
    link_issues=(),
    link_hosts=(),
    risky_attachments=(),
    bulk_cues=(),
    gmail_category_labels=(),
    free_mail_sender=False,
    calendar_invitation=False,
)


def _classification(
    primary: PrimaryLabel, *, urgent: bool = False, subtype: Subtype = Subtype.NONE
) -> Classification:
    return Classification(primary=primary, urgent=urgent, subtype=subtype)


def test_policy_marketing_archived() -> None:
    plan = decide(
        _classification(PrimaryLabel.MARKETING),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=False,
    )

    assert plan.add_labels == (PrimaryLabel.MARKETING.value,)
    assert plan.archive is True
    assert plan.move_to_spam is False
    # D-39: MARKETING is courtesy-eligible, so the candidate flow is the
    # courtesy check, not an unconditional reply — nothing is sent here.
    assert plan.flow is Flow.COURTESY_CHECK


def test_policy_invoice_finance_archived() -> None:
    plan = decide(
        _classification(PrimaryLabel.INFO_ONLY, subtype=Subtype.FINANCE),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=False,
    )

    assert plan.add_labels == (PrimaryLabel.INFO_ONLY.value, FINANCE_LABEL)
    assert plan.archive is True


def test_policy_subtype_finance_ignored_unless_info_only() -> None:
    """D-63: `subtype == finance` acts on the policy only when `primary == INFO_ONLY`;
    for any other primary label it's ignored -- a TASK invoice gets no `Finance` label
    and isn't archived, and a fake ("SPAM") invoice goes through the ordinary SPAM
    handling with no `Finance` label at all."""
    task_plan = decide(
        _classification(PrimaryLabel.TASK, subtype=Subtype.FINANCE),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=False,
    )

    assert FINANCE_LABEL not in task_plan.add_labels
    assert task_plan.archive is False

    spam_plan = decide(
        _classification(PrimaryLabel.SPAM, subtype=Subtype.FINANCE),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=False,
    )

    assert spam_plan.move_to_spam is True
    assert spam_plan.add_labels == ()
    assert FINANCE_LABEL not in spam_plan.add_labels


def test_policy_info_only_stays_in_inbox() -> None:
    plan = decide(
        _classification(PrimaryLabel.INFO_ONLY, subtype=Subtype.NONE),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=False,
    )

    assert plan.add_labels == (PrimaryLabel.INFO_ONLY.value,)
    assert plan.archive is False
    assert plan.move_to_spam is False


def test_policy_spam_system_label() -> None:
    # Classification itself already forces urgent False for SPAM; the
    # policy engine defends against URGENT ever appearing for SPAM
    # regardless, by never adding it when primary is SPAM.
    classification = _classification(PrimaryLabel.SPAM, urgent=True)

    plan = decide(classification, _NOT_AUTOMATED, sales_decline_requires_approval=False)

    assert plan.move_to_spam is True
    # D-44: Gmail rejects a user label named "SPAM" (reserved system name);
    # applying the system SPAM label is `move_to_spam`'s job alone, so
    # `add_labels` carries nothing for SPAM (never "SPAM", never URGENT).
    assert plan.add_labels == ()
    assert URGENT_LABEL not in plan.add_labels
    assert plan.mark_starred is False
    assert plan.flow is Flow.NONE
    assert plan.archive is False


def test_policy_sales_auto_or_approval() -> None:
    auto_plan = decide(
        _classification(PrimaryLabel.SALES_OUTREACH),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=False,
    )
    approval_plan = decide(
        _classification(PrimaryLabel.SALES_OUTREACH),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=True,
    )

    assert auto_plan.flow is Flow.SALES_DECLINE_AUTO
    assert auto_plan.archive is True
    assert approval_plan.flow is Flow.SALES_DECLINE_APPROVAL
    assert approval_plan.archive is True


def test_policy_sales_automated_sender_no_decline() -> None:
    automated = replace(_NOT_AUTOMATED, automated_reasons=("sender is the mailbox's own address",))

    plan = decide(
        _classification(PrimaryLabel.SALES_OUTREACH),
        automated,
        sales_decline_requires_approval=False,
    )

    assert plan.add_labels == (PrimaryLabel.SALES_OUTREACH.value,)
    assert plan.archive is True
    assert plan.flow is Flow.NONE
    assert len(plan.skip_reasons) == 1
    assert "automated" in plan.skip_reasons[0]


def test_policy_urgent_internal_task() -> None:
    internal = replace(_NOT_AUTOMATED, internal=True)

    plan = decide(
        _classification(PrimaryLabel.TASK, urgent=True),
        internal,
        sales_decline_requires_approval=False,
    )

    assert plan.add_labels == (PrimaryLabel.TASK.value, URGENT_LABEL)
    assert plan.mark_starred is True
    assert plan.flow is Flow.URGENT_INTERNAL_ACK
    assert plan.notify_manager is True


def test_policy_meeting_request_not_archived() -> None:
    plan = decide(
        _classification(PrimaryLabel.MEETING_REQUEST),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=False,
    )

    assert plan.flow is Flow.MEETING
    assert plan.archive is False
    assert plan.move_to_spam is False


def test_policy_meeting_request_with_invitation_goes_to_invitation_flow() -> None:
    invitation = InvitationResult(
        kind=InvitationKind.REQUEST,
        invitation=Invitation(
            uid="event-1",
            start=datetime(2026, 10, 5, 14, 0, tzinfo=UTC),
            end=datetime(2026, 10, 5, 15, 0, tzinfo=UTC),
            title="Sync",
            location="",
            organizer="alice@example.com",
        ),
    )

    plan = decide(
        _classification(PrimaryLabel.MEETING_REQUEST),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=False,
        invitation=invitation,
    )

    assert plan.flow is Flow.INVITATION
    assert plan.archive is False


def test_policy_meeting_request_cancel_or_reply_no_calendar_action() -> None:
    # D-48/D-55: METHOD:CANCEL -> no calendar action (Google removes the
    # event itself); METHOD:REPLY -> logged only. Neither goes through the
    # meeting-extraction/scheduling flow.
    for kind in (InvitationKind.CANCEL, InvitationKind.REPLY):
        plan = decide(
            _classification(PrimaryLabel.MEETING_REQUEST),
            _NOT_AUTOMATED,
            sales_decline_requires_approval=False,
            invitation=InvitationResult(kind=kind, invitation=None),
        )

        assert plan.flow is Flow.NONE, kind
        assert plan.archive is False, kind


def test_policy_meeting_request_classified_spam_no_calendar_action() -> None:
    # D-45/D-55: a meeting request classified SPAM never gets a calendar
    # action — SPAM wins, even with a detected invitation.
    invitation = InvitationResult(kind=InvitationKind.REQUEST, invitation=None)

    plan = decide(
        _classification(PrimaryLabel.SPAM),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=False,
        invitation=invitation,
    )

    assert plan.move_to_spam is True
    assert plan.flow is Flow.NONE


def test_policy_one_approval_per_email() -> None:
    """D-25: one email never produces more than one approval request.

    TASK + urgent + internal has *two* approval-worthy actions (the
    acknowledgement reply and the manager notification), but the policy
    engine bundles them into a single `flow`/`notify_manager` decision —
    `ActionPlan` has exactly one `flow` field, never a list of flows, so
    the pipeline can only ever build one approval email per processed
    message.
    """
    internal = replace(_NOT_AUTOMATED, internal=True)

    plan = decide(
        _classification(PrimaryLabel.TASK, urgent=True),
        internal,
        sales_decline_requires_approval=False,
    )

    assert plan.flow is Flow.URGENT_INTERNAL_ACK
    assert plan.notify_manager is True


def test_policy_urgent_task_non_internal_sender_uses_courtesy_path() -> None:
    # D-40's last bullet: urgent from a non-internal sender is still
    # URGENT/STARRED, but follows the normal D-39 courtesy path (the LLM
    # decides), not the forced urgent-internal acknowledgement.
    plan = decide(
        _classification(PrimaryLabel.TASK, urgent=True),
        _NOT_AUTOMATED,
        sales_decline_requires_approval=False,
    )

    assert plan.add_labels == (PrimaryLabel.TASK.value, URGENT_LABEL)
    assert plan.mark_starred is True
    assert plan.flow is Flow.COURTESY_CHECK
    assert plan.notify_manager is False


def test_policy_urgent_internal_task_automated_sender_still_notifies() -> None:
    # D-40: when the automated-sender guard blocks the acknowledgement
    # reply (e.g. an internal no-reply system), the manager notification
    # still happens; only the ack reply is skipped, with a reason.
    internal_automated = replace(
        _NOT_AUTOMATED, internal=True, automated_reasons=("local part 'no-reply' is automated",)
    )

    plan = decide(
        _classification(PrimaryLabel.TASK, urgent=True),
        internal_automated,
        sales_decline_requires_approval=False,
    )

    assert plan.notify_manager is True
    assert plan.flow is Flow.NONE
    assert len(plan.skip_reasons) == 1
    assert "automated" in plan.skip_reasons[0]


def test_policy_spam_verdict_always_moves_to_spam() -> None:
    """Task 52 (D-44): facts are observations only -- no veto. Every SPAM
    classification moves the email to Spam, even for a fully authenticated sender
    with no suspicious links/attachments."""
    authenticated = replace(
        _NOT_AUTOMATED,
        auth=AuthResult(
            spf="pass",
            dkim="pass",
            dmarc="pass",
            dkim_domain="example.com",
            dmarc_domain="example.com",
        ),
    )

    plan = decide(
        _classification(PrimaryLabel.SPAM),
        authenticated,
        sales_decline_requires_approval=False,
    )

    assert plan.move_to_spam is True
    assert plan.add_labels == ()
    assert plan.archive is False
    assert plan.flow is Flow.NONE
    assert plan.notify_manager is False

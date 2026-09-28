"""Unit tests for `tests/evals/run_evals.py::EmailFixture` (task 57).

Per the testing rules: this covers the one behaviour that could actually break --
`EmailFixture.calendar_ics` must produce an `Email` whose calendar part is wired up
exactly the way `adapters/gmail.py::_calendar_parts` builds one (D-48), so a
calendar-invitation eval case sees the same `Invitation`/`Facts` the real app would
build from a received `.ics`. Not a test of `detect_invitation` or `extract_facts`
themselves (covered by `test_invitations.py`/`test_facts.py`); only that the eval
fixture wires into them correctly.
"""

from pathlib import Path

from dt_agent.core.facts import extract_facts
from dt_agent.core.invitations import InvitationKind, detect_invitation
from dt_agent.domain.email import Email
from tests.evals.run_evals import INTERNAL_DOMAINS, MAILBOX_ADDRESS, EmailFixture, _build_email

_INVITATION_ICS = (
    Path(__file__).resolve().parent.parent / "fixtures" / "invitations" / "request_part.ics"
).read_text()

_CALENDAR_FACT_LINE = "Contains a calendar invitation part (.ics)."


def _fixture(*, calendar_ics: str | None) -> EmailFixture:
    return EmailFixture(
        from_address="organizer@partner.com",
        from_name="Alice Organizer",
        subject="Quarterly Planning Sync",
        body="Let's meet to plan the quarter.",
        calendar_ics=calendar_ics,
    )


def _prompt_lines(email: Email) -> list[str]:
    facts = extract_facts(
        email,
        own_address=MAILBOX_ADDRESS,
        internal_domains=INTERNAL_DOMAINS,
        allowlist_senders=(),
        allowlist_domains=(),
        gmail_labels=[],
    )
    return facts.to_prompt_lines()


def test_calendar_ics_produces_a_recognised_invitation() -> None:
    email = _build_email(_fixture(calendar_ics=_INVITATION_ICS))

    result = detect_invitation(email)

    assert result.kind is InvitationKind.REQUEST
    assert result.invitation is not None
    assert result.invitation.uid == "event-part-001@example.com"
    assert _CALENDAR_FACT_LINE in _prompt_lines(email)


def test_no_calendar_ics_has_no_calendar_part() -> None:
    email = _build_email(_fixture(calendar_ics=None))

    result = detect_invitation(email)

    assert result.kind is InvitationKind.NOT_INVITATION
    assert email.calendar_parts == ()
    assert _CALENDAR_FACT_LINE not in _prompt_lines(email)

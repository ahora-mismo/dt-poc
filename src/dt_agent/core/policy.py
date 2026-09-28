"""Policy engine (D-25, D-38, D-39, D-40, D-44, D-48, D-63): core/policy.py.

This module decides *what* to do with an already-classified email; it
never performs I/O and never sends/labels/schedules anything itself (the
pipeline executes the returned :class:`ActionPlan`). Inputs are the LLM's
:class:`Classification` (D-24), the deterministic :class:`EmailFacts`
(D-42, D-44, the shared automated-sender guard), and, for
`MEETING_REQUEST`, an optional detected calendar :class:`InvitationResult`
(D-48).

Every rule below follows the required-behaviour table and the approved
decisions:

- SPAM always wins: it is decided first and short-circuits every other
  rule (no URGENT, no replies, no calendar action — D-25, D-44, D-45,
  D-55), even if a calendar invitation was detected in the same message.
  `add_labels` is empty for SPAM: Gmail rejects a *user* label named
  "SPAM" (reserved system name, D-44), so applying the system SPAM label
  is `move_to_spam`'s job alone. The LLM's verdict is trusted as-is: every
  SPAM classification moves the email to Spam (D-44).
- MARKETING / SALES_OUTREACH / MEETING_REQUEST / TASK / INFO_ONLY each
  get the actions the required-behaviour table assigns; `urgent`
  (materialized as the `URGENT` Gmail label + a Gmail star, D-25)
  and the courtesy-reply eligibility (D-39: MARKETING, INFO_ONLY, TASK)
  layer on top.
- The shared automated-sender guard (`facts.automated`) skips a sales
  decline (D-38) or a courtesy reply (D-39) and records why in
  `skip_reasons`, without changing the label/archive actions.
- `ActionPlan.flow` is a single enum, not a collection: one email can
  only ever produce one approval request (D-25) — the urgent-internal
  acknowledgement and the manager notification (D-40) are bundled into
  that one flow rather than two separate approvals.
"""

from dataclasses import dataclass
from enum import StrEnum

from dt_agent.core.facts import EmailFacts
from dt_agent.core.invitations import InvitationKind, InvitationResult
from dt_agent.domain.classification import Classification
from dt_agent.domain.labels import FINANCE_LABEL, URGENT_LABEL, PrimaryLabel, Subtype


class Flow(StrEnum):
    """The one approval/action flow an `ActionPlan` selects (D-25)."""

    NONE = "none"
    MEETING = "meeting"
    INVITATION = "invitation"
    SALES_DECLINE_AUTO = "sales_decline_auto"
    SALES_DECLINE_APPROVAL = "sales_decline_approval"
    URGENT_INTERNAL_ACK = "urgent_internal_ack"
    COURTESY_CHECK = "courtesy_check"


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """What the pipeline must do for one email, decided by :func:`decide`."""

    add_labels: tuple[str, ...]
    mark_starred: bool
    archive: bool
    move_to_spam: bool
    flow: Flow
    skip_reasons: tuple[str, ...]
    notify_manager: bool


def _courtesy_flow_or_skip(facts: EmailFacts) -> tuple[Flow, tuple[str, ...]]:
    """D-39's automated-sender guard: no candidate courtesy flow, with a reason."""
    if facts.automated:
        reasons = "; ".join(facts.automated_reasons)
        return Flow.NONE, (f"automated sender: no courtesy reply ({reasons})",)
    return Flow.COURTESY_CHECK, ()


def decide(
    classification: Classification,
    facts: EmailFacts,
    *,
    sales_decline_requires_approval: bool,
    invitation: InvitationResult | None = None,
) -> ActionPlan:
    """Decide the `ActionPlan` for one classified email.

    `invitation` is the calendar-invitation detection result (D-48),
    when one was found for a `MEETING_REQUEST` email; `None` otherwise
    (including for every other primary label, where it is ignored).
    """
    primary = classification.primary
    # SPAM already forces `urgent` False on the `Classification` model
    # itself; this defends the same rule at the policy boundary too,
    # since SPAM must never carry URGENT/STARRED regardless (D-25).
    urgent = classification.urgent and primary is not PrimaryLabel.SPAM

    if primary is PrimaryLabel.SPAM:
        return ActionPlan(
            add_labels=(),
            mark_starred=False,
            archive=False,
            move_to_spam=True,
            flow=Flow.NONE,
            skip_reasons=(),
            notify_manager=False,
        )

    labels: list[str] = [primary.value]
    if urgent:
        labels.append(URGENT_LABEL)

    archive = False
    move_to_spam = False
    flow = Flow.NONE
    skip_reasons: tuple[str, ...] = ()
    notify_manager = False

    if primary is PrimaryLabel.MARKETING:
        archive = True
        flow, skip_reasons = _courtesy_flow_or_skip(facts)

    elif primary is PrimaryLabel.SALES_OUTREACH:
        archive = True
        if facts.automated:
            reasons = "; ".join(facts.automated_reasons)
            skip_reasons = (f"automated sender: no decline ({reasons})",)
        else:
            flow = (
                Flow.SALES_DECLINE_APPROVAL
                if sales_decline_requires_approval
                else Flow.SALES_DECLINE_AUTO
            )

    elif primary is PrimaryLabel.MEETING_REQUEST:
        archive = False
        if invitation is not None and invitation.kind is InvitationKind.REQUEST:
            flow = Flow.INVITATION
        elif invitation is not None and invitation.kind in (
            InvitationKind.CANCEL,
            InvitationKind.REPLY,
        ):
            # D-48/D-55: a cancellation is removed by Google itself, and a
            # REPLY isn't ours to act on; both are logged only, never a
            # calendar action or meeting-extraction flow.
            flow = Flow.NONE
        else:
            flow = Flow.MEETING

    elif primary is PrimaryLabel.TASK:
        if urgent and facts.internal:
            notify_manager = True
            if facts.automated:
                reasons = "; ".join(facts.automated_reasons)
                skip_reasons = (f"automated sender: no acknowledgement reply ({reasons})",)
            else:
                flow = Flow.URGENT_INTERNAL_ACK
        else:
            flow, skip_reasons = _courtesy_flow_or_skip(facts)

    elif primary is PrimaryLabel.INFO_ONLY:
        if classification.subtype is Subtype.FINANCE:
            labels.append(FINANCE_LABEL)
            archive = True
        flow, skip_reasons = _courtesy_flow_or_skip(facts)

    return ActionPlan(
        add_labels=tuple(labels),
        mark_starred=urgent,
        archive=archive,
        move_to_spam=move_to_spam,
        flow=flow,
        skip_reasons=skip_reasons,
        notify_manager=notify_manager,
    )

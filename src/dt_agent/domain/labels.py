"""Gmail label vocabulary (D-24, D-25).

Enums for the LLM-produced classification, plus the exact Gmail label
strings the policy engine applies. The LLM never sees or chooses the
system labels (`Finance`, `AI/Error`, `AI/NeedsReview`); those are applied
by the policy engine only (D-25).
"""

from enum import StrEnum


class PrimaryLabel(StrEnum):
    """The one primary label every email gets (D-24)."""

    MEETING_REQUEST = "MEETING_REQUEST"
    TASK = "TASK"
    INFO_ONLY = "INFO_ONLY"
    SALES_OUTREACH = "SALES_OUTREACH"
    MARKETING = "MARKETING"
    SPAM = "SPAM"


class Subtype(StrEnum):
    """Extra signal beyond the primary label (D-63).

    New cases that need a signal beyond the primary label add enum
    values here, not new fields on `Classification`.
    """

    FINANCE = "finance"
    NONE = "none"


# The URGENT modifier label (D-24). Never combined with SPAM (D-25).
URGENT_LABEL = "URGENT"

# System labels applied by the policy engine only, never by the LLM (D-25).
FINANCE_LABEL = "Finance"
AI_ERROR_LABEL = "AI/Error"
AI_NEEDS_REVIEW_LABEL = "AI/NeedsReview"

# Every primary label's Gmail name, in enum declaration order.
PRIMARY_GMAIL_LABELS: tuple[str, ...] = tuple(label.value for label in PrimaryLabel)

# Gmail system label IDs used by routing/policy (D-21, D-25, D-31, D-44, D-47).
GMAIL_LABEL_ID_INBOX = "INBOX"
GMAIL_LABEL_ID_SPAM = "SPAM"
GMAIL_LABEL_ID_STARRED = "STARRED"
GMAIL_LABEL_ID_SENT = "SENT"
GMAIL_LABEL_ID_UNREAD = "UNREAD"

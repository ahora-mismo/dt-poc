"""Classification output (D-24, D-25, D-63, D-64)."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from dt_agent.domain.dates import DateExpression
from dt_agent.domain.labels import PrimaryLabel, Subtype

# PydanticAI includes each field's `description` in the output schema it sends the LLM,
# so these two descriptions are read by the model, not just by a human reading this file
# (D-24, D-64) — kept in sync with `prompts/classify.md`'s `deadline`/`urgency_requested`
# sections, not a substitute for them.

_DEADLINE_DESCRIPTION = (
    "A date by which the sender needs the recipient to act; not a meeting time, an "
    "offer expiry, an event date or a sent/issue date."
)

_URGENCY_REQUESTED_DESCRIPTION = (
    "True when the sender explicitly asks the recipient for speed, e.g. 'ASAP', 'as "
    "soon as possible', 'urgent', 'high priority'/'top priority', 'time-sensitive', "
    "'move fast', 'immediately', 'drop everything'. False for phrases like 'no rush', "
    "'when you have a chance', 'at your convenience', 'sometime next month', and "
    "otherwise. A seller's own pressure in a pitch or promotion ('only today', "
    "'limited time', 'ends tonight') is not a genuine urgency request."
)


class ClassifierOutput(BaseModel):
    """The `Classifier` port's (PydanticAI) `output_type` (D-24).

    The LLM picks one of the six primary labels (SPAM included), restates a deadline
    as a `DateExpression` (D-64) and gives its own reading of whether the sender asked
    for speed; it computes no dates and decides no urgency itself.
    `LLMClassifier.classify_with_output` (`adapters/llm/classifier.py`) turns this into
    a `Classification` (`urgent`, `deadline_at`) via `core/urgency.py::compute_urgency`.
    """

    model_config = ConfigDict(frozen=True)

    primary: PrimaryLabel
    subtype: Subtype
    deadline: DateExpression | None = Field(description=_DEADLINE_DESCRIPTION)
    urgency_requested: bool = Field(description=_URGENCY_REQUESTED_DESCRIPTION)


class Classification(BaseModel):
    """The LLM's classification of one email.

    `urgent` is materialized as the `URGENT` Gmail label; it is never
    combined with `SPAM` (D-25) — the SPAM-forces-not-urgent rule below
    silently normalizes that combination rather than rejecting it,
    because SPAM+urgent is a meaningless-but-harmless LLM output, not a
    signal of a broken caller.

    `deadline_phrase`/`deadline_at` (D-24, task 47) travel with the
    classification so the urgent approval email (`core/approval_email.py`)
    and the audit log can show the deadline without re-resolving it.
    `deadline_phrase` is the sender's own words, kept whenever the LLM
    restated a deadline expression at all, even if it never resolves to a
    time; `deadline_at` is that expression resolved to an instant by the
    same code `urgent` is computed from (`core/urgency.py::compute_urgency`,
    a window's *end*, D-24/D-64), and is `None` both when there's no
    deadline and when one was stated but couldn't be resolved
    (`NoTimeResolution`/`InvalidResolution`, D-66) — a phrase with no
    resolved instant is still informative on its own, but nothing here
    computes a time for it.
    """

    model_config = ConfigDict(frozen=True)

    primary: PrimaryLabel
    urgent: bool
    subtype: Subtype
    deadline_phrase: str | None = None
    deadline_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _spam_forces_not_urgent(self) -> Classification:
        if self.primary is PrimaryLabel.SPAM and self.urgent:
            # `object.__setattr__` bypasses the frozen-model guard; this is
            # the one place allowed to touch it, per the docstring above.
            object.__setattr__(self, "urgent", False)
        return self

"""Unit tests for dt_agent.domain (task 4).

Per the testing rules, this only covers invariants that could actually
break (D-25 SPAM/urgent normalization, naive-datetime rejection,
unknown date-expression kind rejection) — not "a model stores its
fields".
"""

from datetime import datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from dt_agent.domain.classification import Classification, ClassifierOutput
from dt_agent.domain.dates import DateExpression
from dt_agent.domain.email import Email, EmailAddress, Headers
from dt_agent.domain.labels import PrimaryLabel, Subtype


def test_classification_spam_forces_urgent_false() -> None:
    classification = Classification(primary=PrimaryLabel.SPAM, urgent=True, subtype=Subtype.NONE)

    assert classification.urgent is False


def test_email_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        Email(
            id="msg-1",
            thread_id="thread-1",
            headers=Headers(values={}),
            from_address=EmailAddress(address="sender@example.com"),
            subject="Hello",
            received_at=datetime(2026, 9, 28, 10, 15),  # naive, rejected
            text_body="Hi there.",
        )


def test_date_expression_unknown_kind_rejected() -> None:
    adapter: TypeAdapter[DateExpression] = TypeAdapter(DateExpression)

    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "someday", "phrase": "someday"})


def test_classifier_output_schema_carries_urgency_and_deadline_descriptions() -> None:
    """PydanticAI sends each output field's `description` to the LLM as part of the
    output schema (D-24, D-64): losing it is a silent prompt regression, not just "a
    model stores its fields", so it's worth pinning that the true/false phrase lists,
    the seller-pressure exclusion, and the deadline field's own description survive
    `model_json_schema()`."""
    properties = ClassifierOutput.model_json_schema()["properties"]

    urgency_description = properties["urgency_requested"]["description"]
    assert "ASAP" in urgency_description
    assert "no rush" in urgency_description
    assert "seller" in urgency_description

    deadline_description = properties["deadline"]["description"]
    assert "meeting time" in deadline_description

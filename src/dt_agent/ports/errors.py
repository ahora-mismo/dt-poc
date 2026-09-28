"""Error types raised by ports (D-9, D-11, D-12, D-31, D-43).

Plain exceptions, not domain models: they cross from an adapter (or a
fake) to the pipeline, which decides what to do (retry, re-issue an
approval, label `AI/NeedsReview`, ...). No port catches these itself.
"""


class CursorExpiredError(Exception):
    """`MailPort.list_history` raises this when Gmail's history cursor is gone (404, D-31).

    The caller (the poller) reacts by falling back to a fresh first
    iteration, not by retrying the same cursor.
    """


class MessageNotFoundError(Exception):
    """Raised by `MailPort` methods when a message id no longer exists (D-43).

    Signals a non-retryable error (e.g. the message was deleted): callers
    log it and move on rather than retrying.
    """

    def __init__(self, message_id: str) -> None:
        super().__init__(f"message not found: {message_id}")
        self.message_id = message_id


class FatalAuthError(Exception):
    """Google rejected the credentials outright (`invalid_grant` / 401, D-43).

    Never retried and never swallowed: it propagates to the composition
    root, which exits non-zero (D-50).
    """


class LLMOutputError(Exception):
    """Raised by an LLM port when the model's output never validates (D-43).

    The policy engine reacts by labelling the email `AI/NeedsReview`
    without taking any other action.
    """

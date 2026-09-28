"""Shared Google API error policy (D-43): retries, and the fatal-auth escape hatch.

Both `adapters/gmail.py` and `adapters/calendar.py` route every blocking
`googleapiclient` call through `run_google_call`, so the retry / fatal-auth
rules are defined exactly once. Nothing here is Gmail- or Calendar-specific.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from dt_agent.ports.errors import FatalAuthError

__all__ = [
    "FatalAuthError",
    "GoogleApiError",
    "is_invalid_grant",
    "is_retryable",
    "run_google_call",
]

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")


class GoogleApiError(Exception):
    """An `HttpError` that isn't mapped to a more specific port error (D-43)."""


def _status(exc: HttpError) -> int | None:
    try:
        return int(exc.resp.status)
    except AttributeError, TypeError, ValueError:
        return None


def _content_text(exc: HttpError) -> str:
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    return str(content)


def is_invalid_grant(exc: BaseException) -> bool:
    """True for a dead refresh token: `RefreshError`, HTTP 401, or an `invalid_grant` body."""
    if isinstance(exc, RefreshError):
        return True
    if isinstance(exc, HttpError):
        if _status(exc) == 401:
            return True
        if "invalid_grant" in _content_text(exc):
            return True
    return False


def is_retryable(exc: BaseException) -> bool:
    """True for a transient error worth retrying (D-43, D-19): 429/5xx/rate-limit/timeout."""
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, HttpError):
        status = _status(exc)
        if status in _RETRYABLE_STATUSES:
            return True
        if status == 403 and any(reason in _content_text(exc) for reason in _RATE_LIMIT_REASONS):
            return True
    return False


def run_google_call[T](func: Callable[[], T], *, sleep: Callable[[float], None] | None = None) -> T:
    """Run one blocking Google API call under the shared retry / fatal-auth policy (D-43).

    `RefreshError` / HTTP 401 / `invalid_grant` become `FatalAuthError`
    immediately, never retried. Retryable errors (D-43: 429, 5xx, Gmail's
    rate-limit reasons, `TimeoutError`) are retried up to 3 attempts total
    with truncated exponential backoff (1 s, 4 s, capped at 16 s). Any
    other exception propagates after the first attempt. `sleep` is
    injectable so tests never really wait.
    """

    def attempt() -> T:
        try:
            return func()
        except (RefreshError, HttpError) as exc:
            if is_invalid_grant(exc):
                raise FatalAuthError(str(exc)) from exc
            raise

    retryer = Retrying(
        sleep=sleep or time.sleep,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, exp_base=4, max=16),
        retry=retry_if_exception(is_retryable),
        reraise=True,
    )
    return retryer(attempt)

"""Unit tests for the courtesy body-prefix evaluator (task 50, D-39).

`tests/evals/run_evals.py` is otherwise exercised only by `dt-agent eval` (real, billed
LLM calls, excluded from `uv run pytest` via `--ignore=tests/evals`, D-52) -- but
`_body_starts_with` and `CourtesyBodyPrefixMatch.evaluate` are pure/offline, so they're
unit-testable here like any other pure helper. Kept in its own file (not appended to an
existing `tests/unit` file) to avoid clashing with concurrent edits elsewhere in
`tests/unit`/`tests/pipeline` (task 50).
"""

from __future__ import annotations

from types import SimpleNamespace

from dt_agent.ports.llm import CourtesyDraft
from tests.evals.run_evals import CourtesyBodyPrefixMatch, JudgeMeta, _body_starts_with


def _ctx(*, body: str, warranted: bool, expected_body_prefix: str | None) -> SimpleNamespace:
    """A minimal stand-in for `EvaluatorContext`: `CourtesyBodyPrefixMatch.evaluate` reads
    only `ctx.metadata` and `ctx.output`, so building a real `EvaluatorContext` (which also
    needs an internal span tree) isn't necessary here."""
    return SimpleNamespace(
        metadata=JudgeMeta(judge_criteria=(), expected_body_prefix=expected_body_prefix),
        output=CourtesyDraft(warranted=warranted, reason="", body=body),
    )


# -- `_body_starts_with` (pure) -----------------------------------------------------------


def test_body_starts_with_matches_straight_apostrophe() -> None:
    assert _body_starts_with("You're welcome, glad I could help.", "You're welcome")


def test_body_starts_with_matches_curly_apostrophe_in_body() -> None:
    assert _body_starts_with("You\u2019re welcome, glad I could help.", "You're welcome")


def test_body_starts_with_strips_surrounding_whitespace() -> None:
    assert _body_starts_with("  You're welcome, glad I could help.  ", "You're welcome")


def test_body_starts_with_fails_on_mismatch() -> None:
    assert not _body_starts_with("Thank you for your note.", "You're welcome")


def test_body_starts_with_is_case_sensitive() -> None:
    assert not _body_starts_with("you're welcome, glad I could help.", "You're welcome")


# -- `CourtesyBodyPrefixMatch.evaluate` ----------------------------------------------------


def test_evaluator_passes_when_prefix_matches() -> None:
    ctx = _ctx(
        body="You're welcome, glad I could help.",
        warranted=True,
        expected_body_prefix="You're welcome",
    )
    result = CourtesyBodyPrefixMatch().evaluate(ctx)  # type: ignore[arg-type]
    assert bool(result.value) is True  # type: ignore[union-attr]


def test_evaluator_passes_with_curly_apostrophe_in_body() -> None:
    ctx = _ctx(
        body="You\u2019re welcome, glad I could help.",
        warranted=True,
        expected_body_prefix="You're welcome",
    )
    result = CourtesyBodyPrefixMatch().evaluate(ctx)  # type: ignore[arg-type]
    assert bool(result.value) is True  # type: ignore[union-attr]


def test_evaluator_fails_on_mismatched_body() -> None:
    ctx = _ctx(
        body="Thank you for your note.",
        warranted=True,
        expected_body_prefix="You're welcome",
    )
    result = CourtesyBodyPrefixMatch().evaluate(ctx)  # type: ignore[arg-type]
    assert bool(result.value) is False  # type: ignore[union-attr]


def test_evaluator_skipped_when_no_prefix_pinned() -> None:
    ctx = _ctx(body="Thank you for your note.", warranted=True, expected_body_prefix=None)
    result = CourtesyBodyPrefixMatch().evaluate(ctx)  # type: ignore[arg-type]
    assert result == {}


def test_evaluator_skipped_when_output_not_warranted() -> None:
    ctx = _ctx(body="", warranted=False, expected_body_prefix="You're welcome")
    result = CourtesyBodyPrefixMatch().evaluate(ctx)  # type: ignore[arg-type]
    assert result == {}

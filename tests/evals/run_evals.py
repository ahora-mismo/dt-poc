"""Eval dataset runner (D-51, D-63, D-64): loads `tests/evals/dataset/*.yaml`, runs each
case through the *real* LLM adapters (never a fake) and prints a compact pass/fail report.

Called by `dt-agent eval` (`src/dt_agent/cli.py`, task 26), never by `uv run pytest`
(excluded via `--ignore=tests/evals`, D-52). Running this module makes real, billed LLM
calls: never invoke `main()` outside of a deliberate `dt-agent eval` run.

Kinds:

- `classify`: one email (+ code-extracted `Facts`, same as production) into the
  `Classifier` port (via `LLMClassifier.classify_with_output`, which also returns the
  LLM's raw `ClassifierOutput`). Two evaluators, each reporting its fields separately
  (task 32):
  - `LabelMatch`: `primary` (always) and `subtype` (only for INFO_ONLY, D-63).
  - `UrgencyMatch`: `deadline` (canonical compare, `phrase` ignored), `urgency_requested`,
    and `urgent` — recomputed by `core/urgency.py::compute_urgent` from the model's own
    raw output against the fixed `EVAL_RECEIVED_AT`/`EVAL_TIMEZONE` (never the classifier's
    own internally-computed `Classification.urgent`, which depends on the operator's
    `DEFAULT_TIMEZONE` setting and would make the eval's expectations depend on it too).
- `spam`: one email into the same `Classifier` port; only whether the classification is
  SPAM is checked against the case's `spam` (D-44).
- `extract`: one meeting-request email into the `MeetingExtractor` port; exact match on
  the returned date expressions (kind + fields, ignoring `phrase`), `duration_minutes`,
  `timezone`/`timezone_stated`, `extra_attendees`, and `location` (D-64).
- `sales`: one unsolicited-sales email into the `ReplyDrafter` port; code checks (no
  question mark, <=5 sentences) plus an LLM judge against `prompts/judge.md` (D-38).
- `courtesy`: one email (+ its primary label) into the `CourtesyDrafter` port;
  `warranted` must equal the expected value (code). When the model's own output is not
  warranted, the body must be empty, regardless of what was expected. When both the
  model's output *and* the expected value are warranted, the same tone checks as sales
  run (D-39). A case may also pin `expected_body_prefix` (task 50): when set and the
  model's own output is warranted, the body must start with it (code,
  `CourtesyBodyPrefixMatch`) -- e.g. a plain thank-you getting "You're welcome" back.
- `ack`: one urgent-internal email into the `AcknowledgementDrafter` port (D-40), with the
  real `URGENT_INTERNAL_HINT` text (`pipeline/process_email.py`), which names nobody;
  code checks (no question mark, <=3 sentences) plus an LLM judge against
  `_ACK_JUDGE_CRITERIA`, with the hint passed to the judge as allowed content (it states
  what will actually happen, not a promise the model invented).
- `judge`: a fixed, hand-written reply body (no drafter call) run through the judge only,
  with a pinned overall pass/fail verdict — calibrates the judge itself against known-good
  and known-bad replies (D-51).

`load_cases()` only parses and validates the dataset against the domain models; it makes
no LLM call and is safe to run offline, e.g.:

    python -c "from tests.evals.run_evals import load_cases; print(len(load_cases()))"

`verify_expected_urgency()` is a second offline check: it recomputes `urgent` from each
classify case's *expected* `deadline`/`urgency_requested` and confirms it matches that
case's pinned `expected.urgent` — catching a dataset case whose pinned fields are
internally inconsistent, before any LLM call is ever made:

    python -c "
    from tests.evals.run_evals import load_cases, verify_expected_urgency, ClassifyCase
    cases = [c for c in load_cases() if isinstance(c, ClassifyCase)]
    print(verify_expected_urgency(cases) or 'all consistent')
    "
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml  # type: ignore[import-untyped]  # no stub package; not a project dependency to add
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext, EvaluatorOutput
from pydantic_evals.reporting import EvaluationReport, ReportCase

from dt_agent.adapters.llm._prompting import load_prompt
from dt_agent.adapters.llm.acknowledgement_drafter import LLMAcknowledgementDrafter
from dt_agent.adapters.llm.classifier import LLMClassifier
from dt_agent.adapters.llm.courtesy_drafter import LLMCourtesyDrafter
from dt_agent.adapters.llm.meeting_extractor import LLMMeetingExtractor
from dt_agent.adapters.llm.providers import build_model, model_settings_for
from dt_agent.adapters.llm.reply_drafter import LLMReplyDrafter
from dt_agent.config import load_settings
from dt_agent.core.dates import resolve_expressions
from dt_agent.core.facts import extract_facts
from dt_agent.core.urgency import compute_urgent
from dt_agent.domain.classification import Classification, ClassifierOutput
from dt_agent.domain.dates import DateExpression
from dt_agent.domain.email import Attachment, CalendarPart, Email, EmailAddress, Headers
from dt_agent.domain.labels import GMAIL_LABEL_ID_INBOX, PrimaryLabel, Subtype
from dt_agent.domain.meeting import MeetingExtraction, ResolvedSlot
from dt_agent.logging import get_logger
from dt_agent.pipeline.process_email import URGENT_INTERNAL_HINT
from dt_agent.ports.facts import Facts as PortFacts
from dt_agent.ports.llm import CourtesyDraft

# -- Fixed eval context (D-51, D-64) -----------------------------------------------------
#
# One fixed mailbox/internal-domain context for every classify case, and one fixed
# received time/timezone for every case that needs one (D-64/D-24): all are deliberately
# shared, not per-case, so the dataset stays deterministic and easy to reason about, and
# so the urgency cutoff never shifts with the operator's own `.env` (`EVAL_TIMEZONE` is
# used for `compute_urgent`, never `settings.DEFAULT_TIMEZONE`).

MAILBOX_ADDRESS = "assistant@acme.example"
INTERNAL_DOMAINS = ("acme.example",)
EVAL_TIMEZONE = "Europe/Bucharest"
EVAL_RECEIVED_AT = datetime(2026, 9, 28, 10, 15, tzinfo=ZoneInfo(EVAL_TIMEZONE))
# Fixed business hours (task 37) for `compute_urgent`'s date resolver, mirrored from the
# same 9-18 convention `tests/fakes/settings.py` uses; never the operator's own `.env`,
# for the same determinism reason `EVAL_TIMEZONE` isn't `settings.DEFAULT_TIMEZONE`.
EVAL_BUSINESS_HOURS_START = 9
EVAL_BUSINESS_HOURS_END = 18

_DATASET_DIR = Path(__file__).parent / "dataset"

# One fixed rubric per drafted-reply kind (D-38, D-39): the yes/no criteria a judge.md
# run checks, derived directly from the corresponding prompt's "must do"/"must not do"
# lists (excluding the criteria checked in code: no question mark, sentence count, and
# for courtesy replies no exclamation mark),
# worded as statements (task 32), never as "warm and genuine" (subjective) or as a
# question (the "?" check is code's job, not the judge's).

_SALES_JUDGE_CRITERIA: tuple[str, ...] = (
    "The reply thanks the sender for reaching out.",
    "The reply politely says we are not pursuing this at the moment.",
    "The tone is positive and open throughout.",
    "The reply does not imply we will follow up or reply again.",
    "The reply does not discourage the sender from future contact.",
    "The reply makes no promise of any kind (e.g. keeping the sender in mind).",
    "The reply gives no specifics about our own plans, budget, timeline, or needs.",
    "The reply is written in the same language as the original email.",
)

_COURTESY_JUDGE_CRITERIA: tuple[str, ...] = (
    "The reply is a brief acknowledgement or thank-you, not a long message.",
    "The reply makes no promise about future action.",
    "The reply does not commit to doing, reviewing, checking, or sending anything, or to "
    "getting back to the sender, about the task itself.",
    "The reply does not say or imply that the content was read, reviewed, liked or approved, "
    "and does not comment on or evaluate what was shared (such as 'That's great to hear' or "
    "'great achievement') or thank them for their work or effort on it. Thanking them "
    "for sending or sharing it is fine.",
    "The reply is business-appropriate: plain, measured thanks, with no gushing (such as "
    "'Thanks so much'), and no thanking them for their kind words.",
    "When the original email is only a thank-you, with nothing delivered or shared, the "
    "reply answers in kind ('You're welcome...') and does not thank the sender back; "
    "otherwise this criterion passes.",
)

_ACK_JUDGE_CRITERIA: tuple[str, ...] = (
    "The reply acknowledges that the message was received.",
    "The reply says the message was received and flagged as urgent, naming no one.",
    "The reply makes no commitment about the task itself (never says it will be done, "
    "reviewed, or handled, or by whom).",
    "The reply makes no time promise (no 'today', no other timing).",
    "The reply is written in the same language as the original email.",
)

_JUDGE_RUBRICS: dict[str, tuple[str, ...]] = {
    "sales": _SALES_JUDGE_CRITERIA,
    "courtesy": _COURTESY_JUDGE_CRITERIA,
    "ack": _ACK_JUDGE_CRITERIA,
}


# -- Dataset schema: one small, hand-written email fixture per case ---------------------


class EmailFixture(BaseModel):
    """The handful of `Email` fields a hand-written case actually needs to set.

    `calendar_ics` (task 57) is raw iCalendar text for a case that needs a calendar
    invitation part; `_build_email` wraps it in a single `CalendarPart`, the same shape
    `adapters/gmail.py::_calendar_parts` produces for either a `text/calendar` part or an
    `.ics` attachment (both reduce to the same raw text by the time they reach `Email`)."""

    model_config = ConfigDict(extra="forbid")

    id: str = "eval"
    from_address: str
    from_name: str = ""
    reply_to: str | None = None
    to: list[str] = Field(default_factory=lambda: [MAILBOX_ADDRESS])
    cc: list[str] = Field(default_factory=list)
    subject: str
    body: str
    headers: dict[str, str] = Field(default_factory=dict)
    label_ids: list[str] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    calendar_ics: str | None = None


_RISKY_MIME_BY_EXTENSION: dict[str, str] = {
    "exe": "application/x-msdownload",
    "zip": "application/zip",
    "html": "text/html",
    "docm": "application/vnd.ms-word.document.macroEnabled.12",
}


def _attachment(filename: str) -> Attachment:
    _, _, extension = filename.lower().rpartition(".")
    mime_type = _RISKY_MIME_BY_EXTENSION.get(extension, "application/octet-stream")
    return Attachment(filename=filename, mime_type=mime_type, size=1024)


def _build_email(fixture: EmailFixture) -> Email:
    return Email(
        id=fixture.id,
        thread_id=fixture.id,
        label_ids=(GMAIL_LABEL_ID_INBOX, *fixture.label_ids),
        headers=Headers(values=fixture.headers),
        from_address=EmailAddress(name=fixture.from_name, address=fixture.from_address),
        reply_to=EmailAddress(address=fixture.reply_to) if fixture.reply_to else None,
        to=tuple(EmailAddress(address=address) for address in fixture.to),
        cc=tuple(EmailAddress(address=address) for address in fixture.cc),
        subject=fixture.subject,
        received_at=EVAL_RECEIVED_AT,
        text_body=fixture.body,
        attachments=tuple(_attachment(name) for name in fixture.attachments),
        calendar_parts=(
            (CalendarPart(raw_ics=fixture.calendar_ics),) if fixture.calendar_ics else ()
        ),
    )


class ClassifyExpected(BaseModel):
    """`urgent` is pinned explicitly (not derived at load time) so a mismatch between it
    and `deadline`/`urgency_requested` is a *dataset bug*, caught by the offline
    `verify_expected_urgency()` check (task 32) rather than silently redefining `urgent`.
    `deadline` defaults to `None`: a case with no `deadline` key means "expect none"
    (task 32). `urgency_requested` has no default — every classify case must pin it."""

    model_config = ConfigDict(extra="forbid")

    primary: PrimaryLabel
    urgent: bool
    subtype: Subtype
    deadline: DateExpression | None = None
    urgency_requested: bool


class ClassifyCase(BaseModel):
    """One `dataset/classify.yaml` entry (D-24, D-41, D-42, D-44, D-63)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["classify"] = "classify"
    name: str
    email: EmailFixture
    expected: ClassifyExpected


class SpamCase(BaseModel):
    """One `dataset/spam.yaml` entry (D-44): run through the classifier; only whether the
    classification is SPAM is checked."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["spam"] = "spam"
    name: str
    email: EmailFixture
    spam: bool


class ExtractExpected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[DateExpression] = Field(default_factory=list)
    duration_minutes: int | None = None
    timezone: str | None = None
    timezone_stated: bool = False
    extra_attendees: list[str] = Field(default_factory=list)
    location: str | None = None


class ExtractCase(BaseModel):
    """One `dataset/extract.yaml` entry (D-64)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["extract"] = "extract"
    name: str
    email: EmailFixture
    expected: ExtractExpected


class SalesCase(BaseModel):
    """One `dataset/sales.yaml` entry (D-38); always expects a decline to be drafted."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["sales"] = "sales"
    name: str
    email: EmailFixture
    max_sentences: int = 5


class CourtesyCase(BaseModel):
    """One `dataset/courtesy.yaml` entry (D-39). `expected_body_prefix` (task 50) is an
    optional code-checked prefix (e.g. "You're welcome"): when set and the model's own
    output is warranted, `CourtesyBodyPrefixMatch` checks the body starts with it."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["courtesy"] = "courtesy"
    name: str
    email: EmailFixture
    label: PrimaryLabel
    expected_warranted: bool
    expected_body_prefix: str | None = None
    max_sentences: int = 5


class AckCase(BaseModel):
    """One `dataset/ack.yaml` entry (D-40): the urgent-internal acknowledgement drafter
    always drafts (the flow has already decided the reply is required, D-40 §2), so this
    only needs the email, unlike `CourtesyCase`'s `expected_warranted`."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["ack"] = "ack"
    name: str
    email: EmailFixture
    max_sentences: int = 3


class JudgeCase(BaseModel):
    """One `dataset/judge.yaml` entry: a fixed, hand-written reply body run through the
    judge only (no drafter call), with a pinned overall pass/fail verdict — calibrates the
    judge against known-good and known-bad replies (task 32, D-51)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["judge"] = "judge"
    name: str
    rubric: Literal["sales", "courtesy", "ack"]
    original_email_text: str
    body: str
    max_sentences: int = 5
    expected_pass: bool


EvalCase = ClassifyCase | SpamCase | ExtractCase | SalesCase | CourtesyCase | AckCase | JudgeCase


def _load_kind[T: BaseModel](kind: str, model: type[T]) -> list[T]:
    raw = yaml.safe_load((_DATASET_DIR / f"{kind}.yaml").read_text(encoding="utf-8")) or []
    return [model.model_validate(item) for item in raw]


def load_cases() -> list[EvalCase]:
    """Load and validate every case in `dataset/*.yaml` (offline; makes no LLM call)."""
    cases: list[EvalCase] = []
    cases.extend(_load_kind("classify", ClassifyCase))
    cases.extend(_load_kind("spam", SpamCase))
    cases.extend(_load_kind("extract", ExtractCase))
    cases.extend(_load_kind("sales", SalesCase))
    cases.extend(_load_kind("courtesy", CourtesyCase))
    cases.extend(_load_kind("ack", AckCase))
    cases.extend(_load_kind("judge", JudgeCase))
    return cases


def verify_expected_urgency(cases: Sequence[ClassifyCase]) -> list[str]:
    """Offline check (task 32): `compute_urgent` on each case's *expected*
    `deadline`/`urgency_requested` must equal that case's pinned `expected.urgent`. Pure;
    makes no LLM call. Returns one message per mismatch; empty means all consistent."""
    mismatches: list[str] = []
    for case in cases:
        expected = case.expected
        computed = compute_urgent(
            expected.primary,
            expected.deadline,
            expected.urgency_requested,
            received_at=EVAL_RECEIVED_AT,
            timezone=EVAL_TIMEZONE,
            business_hours_start=EVAL_BUSINESS_HOURS_START,
            business_hours_end=EVAL_BUSINESS_HOURS_END,
        )
        if computed != expected.urgent:
            mismatches.append(
                f"{case.name}: compute_urgent(...) = {computed}, but expected.urgent = "
                f"{expected.urgent}"
            )
    return mismatches


# -- The LLM judge (D-51: same provider/model as the app, via `prompts/judge.md`) --------


class CriterionVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    criterion: str
    verdict: Literal["pass", "fail"]
    reason: str


async def _run_judge(
    judge_agent: Agent[None, list[CriterionVerdict]],
    criteria: Sequence[str],
    original_email_text: str,
    reply_body: str,
    *,
    allowed_content: str | None = None,
) -> list[CriterionVerdict]:
    numbered = "\n".join(f"{index}. {criterion}" for index, criterion in enumerate(criteria, 1))
    allowed_section = (
        f"\n\nAllowed content (the reply stating this is not itself a violation of a "
        f"no-promise/no-follow-up style criterion): {allowed_content}"
        if allowed_content
        else ""
    )
    prompt = (
        f"Criteria:\n{numbered}\n\n"
        f"Original email:\n{original_email_text}\n\n"
        f"Drafted reply:\n{reply_body}{allowed_section}"
    )
    result = await judge_agent.run(prompt)
    return result.output


def _count_sentences(text: str) -> int:
    """Approximate sentence count for the "<=5 sentences" code check (D-51).

    Good enough for a drafted reply's short, plain prose: split on
    sentence-ending punctuation followed by whitespace or end of string.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    endings = re.findall(r"[.!?]+(?=\s|$)", stripped)
    return len(endings) or 1


async def _tone_check(
    judge_agent: Agent[None, list[CriterionVerdict]],
    *,
    original_email_text: str,
    body: str,
    criteria: Sequence[str],
    max_sentences: int,
    allowed_content: str | None = None,
) -> EvaluationReason:
    """Shared code + judge checks for a drafted reply body (D-38, D-39)."""
    reasons: list[str] = []
    if "?" in body:
        reasons.append("body contains a question mark")
    # D-39: no exclamation marks in a courtesy reply -- checked by code, not the judge
    # (the judge was seen reporting a "!" that wasn't there).
    if tuple(criteria) == _COURTESY_JUDGE_CRITERIA and "!" in body:
        reasons.append("body contains an exclamation mark")
    sentence_count = _count_sentences(body)
    if sentence_count > max_sentences:
        reasons.append(f"body has {sentence_count} sentences (limit {max_sentences})")
    verdicts = await _run_judge(
        judge_agent, criteria, original_email_text, body, allowed_content=allowed_content
    )
    for verdict in verdicts:
        if verdict.verdict != "pass":
            reasons.append(f"judge {verdict.criterion!r} failed: {verdict.reason}")
    return EvaluationReason(value=not reasons, reason="; ".join(reasons) if reasons else None)


# -- Comparing date expressions, ignoring `phrase` (D-64) --------------------------------


def _strip_phrase(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_phrase(item) for key, item in value.items() if key != "phrase"}
    if isinstance(value, list):
        return [_strip_phrase(item) for item in value]
    return value


def _canonical_expressions(expressions: Sequence[DateExpression]) -> list[str]:
    """Canonical (phrase-stripped) JSON per expression, exact-match compared (D-64).

    A `none` expression and an empty list mean the same thing downstream (D-64: the agent
    proposes free slots), so `none` entries are excluded. A `window` expression is excluded
    too (task 39): two window proposals can describe the same actual availability in more
    than one way (e.g. one two-day range vs. two single-day windows), so windows are
    compared separately, by their *resolved* daily slots — see `_window_slot_pairs`.
    """
    dumped = [
        _strip_phrase(expr.model_dump(mode="json"))
        for expr in expressions
        if expr.kind not in ("none", "window")
    ]
    return sorted(json.dumps(item, sort_keys=True) for item in dumped)


def _window_slot_pairs(expressions: Sequence[DateExpression]) -> list[tuple[str, str]]:
    """Resolved (start, end) pairs for every `window` proposal in `expressions` (task 39).

    A window's raw expression fields aren't what reaches scheduling — its *resolved* daily
    slots are (`core/dates.py`'s F1 fix: one `ResolvedSlot` per business day it spans), so
    that's what this compares, against the fixed `EVAL_RECEIVED_AT`/`EVAL_TIMEZONE`/
    `EVAL_BUSINESS_HOURS_START`/`EVAL_BUSINESS_HOURS_END` (mirrors `UrgencyMatch`, never the
    operator's own settings). `duration` is irrelevant to a window's own span, so a zero
    timedelta is passed, matching `core/urgency.py`'s convention for the same reason. A
    resolution that isn't a plain `ResolvedSlot` (invalid, or no bounds at all) still
    contributes a distinguishing marker, so a malformed window doesn't silently compare
    equal to a well-formed one.
    """
    windows = [expr for expr in expressions if expr.kind == "window"]
    if not windows:
        return []
    resolutions = resolve_expressions(
        windows,
        EVAL_RECEIVED_AT,
        EVAL_TIMEZONE,
        timedelta(0),
        business_hours_start=EVAL_BUSINESS_HOURS_START,
        business_hours_end=EVAL_BUSINESS_HOURS_END,
    )
    pairs: list[tuple[str, str]] = []
    for resolution in resolutions:
        if isinstance(resolution, ResolvedSlot):
            pairs.append((resolution.start.isoformat(), resolution.end.isoformat()))
        else:
            pairs.append((type(resolution).__name__, resolution.phrase))
    return sorted(pairs)


def _body_starts_with(body: str, prefix: str) -> bool:
    """Case-sensitive prefix check for a courtesy reply body (task 50, D-39): the body is
    stripped of surrounding whitespace first, and a straight `'` and curly `\u2019` apostrophe
    are treated as equivalent (both normalized to `'` before comparing), so "You're" and
    "You\u2019re" match the same pinned prefix."""
    stripped_body = body.strip().replace("\u2019", "'")
    normalized_prefix = prefix.replace("\u2019", "'")
    return stripped_body.startswith(normalized_prefix)


def _canonical_deadline(expression: DateExpression | None) -> str | None:
    """A missing `deadline` and a `none` expression mean the same thing ("no deadline
    stated"), so both canonicalize to `None` (task 32, mirrors `_canonical_expressions`)."""
    if expression is None or expression.kind == "none":
        return None
    return json.dumps(_strip_phrase(expression.model_dump(mode="json")), sort_keys=True)


# -- Task inputs/outputs and adapters (real, never fakes) --------------------------------


@dataclass(frozen=True, slots=True)
class ClassifyResult:
    """The classifier's raw LLM output plus the code-computed `Classification` (D-24).

    The eval needs both: `LabelMatch` reads the `Classification` (`primary`/`subtype`,
    identical to `output`'s);
    `UrgencyMatch` reads `output` directly, so it can recompute `urgent` itself against
    the fixed `EVAL_RECEIVED_AT`/`EVAL_TIMEZONE` rather than trust the classifier's own
    internally-computed value (which depends on the operator's `DEFAULT_TIMEZONE`
    setting, task 32).
    """

    output: ClassifierOutput
    classification: Classification


@dataclass(frozen=True, slots=True)
class CourtesyInputs:
    email: Email
    label: PrimaryLabel


@dataclass(frozen=True, slots=True)
class AckInputs:
    """D-40: `hint` is the real `URGENT_INTERNAL_HINT` text, passed to
    `CourtesyBodyChecks`'s sibling evaluator as allowed content (task 32)."""

    email: Email
    hint: str


@dataclass(frozen=True, slots=True)
class JudgeInputs:
    """Calibration inputs (task 32): a fixed body, not a drafter call."""

    original_email_text: str
    body: str


class JudgeMeta(BaseModel):
    """Per-case metadata the judge-backed evaluators need (D-38, D-39). `expected_body_prefix`
    (task 50) is read only by `CourtesyBodyPrefixMatch`; unset for sales/ack cases."""

    model_config = ConfigDict(frozen=True)

    judge_criteria: tuple[str, ...]
    max_sentences: int = 5
    expected_body_prefix: str | None = None


class JudgeCalibrationMeta(BaseModel):
    """Per-case metadata for the `judge` calibration kind (task 32)."""

    model_config = ConfigDict(frozen=True)

    judge_criteria: tuple[str, ...]
    max_sentences: int = 5
    expected_pass: bool


@dataclass(frozen=True, slots=True)
class Deps:
    """The real adapters every task function calls (never `tests/fakes`, D-51)."""

    classifier: LLMClassifier
    extractor: LLMMeetingExtractor
    reply_drafter: LLMReplyDrafter
    courtesy_drafter: LLMCourtesyDrafter
    acknowledgement_drafter: LLMAcknowledgementDrafter
    judge_agent: Agent[None, list[CriterionVerdict]]


async def _classify_task(deps: Deps, email: Email) -> ClassifyResult:
    facts = extract_facts(
        email,
        own_address=MAILBOX_ADDRESS,
        internal_domains=INTERNAL_DOMAINS,
        allowlist_senders=(),
        allowlist_domains=(),
        gmail_labels=list(email.label_ids),
    )
    port_facts = PortFacts(items={"facts": facts.to_prompt_lines()})
    output, classification = await deps.classifier.classify_with_output(email, port_facts)
    return ClassifyResult(output=output, classification=classification)


async def _extract_task(deps: Deps, email: Email) -> MeetingExtraction:
    return await deps.extractor.extract(email)


async def _sales_task(deps: Deps, email: Email) -> str:
    return await deps.reply_drafter.draft_sales_decline(email)


async def _courtesy_task(deps: Deps, inputs: CourtesyInputs) -> CourtesyDraft:
    return await deps.courtesy_drafter.draft(inputs.email, inputs.label)


async def _ack_task(deps: Deps, inputs: AckInputs) -> str:
    return await deps.acknowledgement_drafter.draft_acknowledgement(inputs.email, inputs.hint)


async def _judge_task(deps: Deps, inputs: JudgeInputs) -> str:
    """Calibration (task 32): the body is fixed, not drafted; this only threads it
    through to the evaluator, which runs it against the judge."""
    return inputs.body


# -- Evaluators ---------------------------------------------------------------------------


@dataclass(repr=False)
class SpamMatch(Evaluator[Email, Any, bool]):
    """Whether the classification is SPAM equals the case's `spam` (D-44)."""

    def evaluate(self, ctx: EvaluatorContext[Email, Any, bool]) -> EvaluatorOutput:
        expected = ctx.expected_output
        assert expected is not None
        actual = ctx.output.classification.primary is PrimaryLabel.SPAM
        return EvaluationReason(
            value=actual == expected,
            reason=(
                None
                if actual == expected
                else f"expected spam={expected}, got {ctx.output.classification.primary}"
            ),
        )


@dataclass(repr=False)
class LabelMatch(Evaluator[Email, Any, Any]):
    """`primary` always; `subtype` only for INFO_ONLY (D-63). Fields reported separately
    (task 32)."""

    def evaluate(self, ctx: EvaluatorContext[Email, Any, Any]) -> EvaluatorOutput:
        expected = ctx.expected_output
        assert expected is not None
        classification: Classification = ctx.output.classification
        result: dict[str, EvaluationReason] = {
            "primary": EvaluationReason(
                value=classification.primary == expected.primary,
                reason=(
                    None
                    if classification.primary == expected.primary
                    else f"expected {expected.primary}, got {classification.primary}"
                ),
            )
        }
        if expected.primary == PrimaryLabel.INFO_ONLY:
            result["subtype"] = EvaluationReason(
                value=classification.subtype == expected.subtype,
                reason=(
                    None
                    if classification.subtype == expected.subtype
                    else f"expected {expected.subtype}, got {classification.subtype}"
                ),
            )
        return result


@dataclass(repr=False)
class UrgencyMatch(Evaluator[Email, Any, Any]):
    """`deadline`, `urgency_requested`, `urgent` (D-24). For a SPAM case only `urgent` is
    checked: `deadline`/`urgency_requested` aren't must-pass there. Fields reported
    separately (task 32)."""

    def evaluate(self, ctx: EvaluatorContext[Email, Any, Any]) -> EvaluatorOutput:
        expected = ctx.expected_output
        assert expected is not None
        raw: ClassifierOutput = ctx.output.output

        actual_urgent = compute_urgent(
            raw.primary,
            raw.deadline,
            raw.urgency_requested,
            received_at=EVAL_RECEIVED_AT,
            timezone=EVAL_TIMEZONE,
            business_hours_start=EVAL_BUSINESS_HOURS_START,
            business_hours_end=EVAL_BUSINESS_HOURS_END,
        )
        result: dict[str, EvaluationReason] = {
            "urgent": EvaluationReason(
                value=actual_urgent == expected.urgent,
                reason=(
                    None
                    if actual_urgent == expected.urgent
                    else f"expected {expected.urgent}, got {actual_urgent}"
                ),
            )
        }
        if expected.primary is PrimaryLabel.SPAM:
            return result

        actual_deadline = _canonical_deadline(raw.deadline)
        expected_deadline = _canonical_deadline(expected.deadline)
        result["deadline"] = EvaluationReason(
            value=actual_deadline == expected_deadline,
            reason=(
                None
                if actual_deadline == expected_deadline
                else f"expected {expected_deadline}, got {actual_deadline}"
            ),
        )
        result["urgency_requested"] = EvaluationReason(
            value=raw.urgency_requested == expected.urgency_requested,
            reason=(
                None
                if raw.urgency_requested == expected.urgency_requested
                else f"expected {expected.urgency_requested}, got {raw.urgency_requested}"
            ),
        )
        return result


@dataclass(repr=False)
class ExtractionMatch(Evaluator[Email, MeetingExtraction, Any]):
    """Exact match on non-window proposals (ignoring `phrase`), duration, timezone,
    attendees and `location` (D-64); `window` proposals compare by their resolved daily
    slots instead (task 39, `_window_slot_pairs`)."""

    def evaluate(self, ctx: EvaluatorContext[Email, MeetingExtraction, Any]) -> EvaluatorOutput:
        expected = ctx.expected_output
        assert expected is not None
        mismatches: list[str] = []

        actual_proposals = _canonical_expressions(ctx.output.proposals)
        expected_proposals = _canonical_expressions(expected.proposals)
        if actual_proposals != expected_proposals:
            mismatches.append(f"proposals: expected {expected_proposals}, got {actual_proposals}")

        actual_window_slots = _window_slot_pairs(ctx.output.proposals)
        expected_window_slots = _window_slot_pairs(expected.proposals)
        if actual_window_slots != expected_window_slots:
            mismatches.append(
                f"window slots: expected {expected_window_slots}, got {actual_window_slots}"
            )

        if ctx.output.duration_minutes != expected.duration_minutes:
            mismatches.append(
                f"duration_minutes: expected {expected.duration_minutes}, "
                f"got {ctx.output.duration_minutes}"
            )

        if ctx.output.timezone_stated != expected.timezone_stated:
            mismatches.append(
                f"timezone_stated: expected {expected.timezone_stated}, "
                f"got {ctx.output.timezone_stated}"
            )
        elif expected.timezone_stated and ctx.output.timezone != expected.timezone:
            mismatches.append(f"timezone: expected {expected.timezone}, got {ctx.output.timezone}")

        actual_attendees = {address.lower() for address in ctx.output.extra_attendees}
        expected_attendees = {address.lower() for address in expected.extra_attendees}
        if actual_attendees != expected_attendees:
            mismatches.append(
                f"extra_attendees: expected {sorted(expected_attendees)}, "
                f"got {sorted(actual_attendees)}"
            )

        if ctx.output.location != expected.location:
            mismatches.append(
                f"location: expected {expected.location!r}, got {ctx.output.location!r}"
            )

        return EvaluationReason(value=not mismatches, reason="; ".join(mismatches) or None)


@dataclass(repr=False)
class SalesDeclineChecks(Evaluator[Email, str, JudgeMeta]):
    """Code checks + judge criteria on the drafted decline body (D-38, D-51)."""

    judge_agent: Agent[None, list[CriterionVerdict]]

    async def evaluate(self, ctx: EvaluatorContext[Email, str, JudgeMeta]) -> EvaluatorOutput:
        meta = ctx.metadata
        assert meta is not None
        return await _tone_check(
            self.judge_agent,
            original_email_text=ctx.inputs.text_body,
            body=ctx.output,
            criteria=meta.judge_criteria,
            max_sentences=meta.max_sentences,
        )


@dataclass(repr=False)
class CourtesyWarrantedMatch(Evaluator[CourtesyInputs, CourtesyDraft, JudgeMeta]):
    """`warranted` equals the expected value (code, D-39)."""

    def evaluate(
        self, ctx: EvaluatorContext[CourtesyInputs, CourtesyDraft, JudgeMeta]
    ) -> EvaluatorOutput:
        expected = ctx.expected_output
        assert expected is not None
        if ctx.output.warranted != expected.warranted:
            return EvaluationReason(
                value=False,
                reason=f"warranted: expected {expected.warranted}, got {ctx.output.warranted}",
            )
        return EvaluationReason(value=True)


@dataclass(repr=False)
class CourtesyBodyChecks(Evaluator[CourtesyInputs, CourtesyDraft, JudgeMeta]):
    """Body-shape rule and tone checks (D-39, D-51, task 32):

    - the model's own output not warranted -> the body must be empty, whatever was
      expected (an LLM that (rightly or wrongly) says "not warranted" must not also draft
      a body);
    - the model's own output warranted, and the case *expects* warranted -> the same code
      + judge tone checks as the sales decline;
    - the model's own output warranted, but the case expects not warranted -> no tone
      check (already failed by `CourtesyWarrantedMatch`; a body was drafted, but nothing
      more to say about its tone).
    """

    judge_agent: Agent[None, list[CriterionVerdict]]

    async def evaluate(
        self, ctx: EvaluatorContext[CourtesyInputs, CourtesyDraft, JudgeMeta]
    ) -> EvaluatorOutput:
        output = ctx.output
        if not output.warranted:
            if output.body.strip():
                return EvaluationReason(
                    value=False, reason="warranted is false but body is non-empty"
                )
            return {}
        expected = ctx.expected_output
        assert expected is not None
        if not expected.warranted:
            return {}  # not applicable; CourtesyWarrantedMatch already covers this case
        meta = ctx.metadata
        assert meta is not None
        return await _tone_check(
            self.judge_agent,
            original_email_text=ctx.inputs.email.text_body,
            body=output.body,
            criteria=meta.judge_criteria,
            max_sentences=meta.max_sentences,
        )


@dataclass(repr=False)
class CourtesyBodyPrefixMatch(Evaluator[CourtesyInputs, CourtesyDraft, JudgeMeta]):
    """Optional code-checked body prefix (task 50, D-39), e.g. a plain thank-you getting
    "You're welcome" back. Not applicable -- returns `{}` -- when the case pins no
    `expected_body_prefix`, or when the model's own output is not warranted (`CourtesyBodyChecks`
    already requires an empty body then, so there is no prefix to check)."""

    def evaluate(
        self, ctx: EvaluatorContext[CourtesyInputs, CourtesyDraft, JudgeMeta]
    ) -> EvaluatorOutput:
        meta = ctx.metadata
        assert meta is not None
        if meta.expected_body_prefix is None or not ctx.output.warranted:
            return {}
        if not _body_starts_with(ctx.output.body, meta.expected_body_prefix):
            return EvaluationReason(
                value=False,
                reason=(
                    f"body does not start with {meta.expected_body_prefix!r}: {ctx.output.body!r}"
                ),
            )
        return EvaluationReason(value=True)


@dataclass(repr=False)
class AcknowledgementChecks(Evaluator[AckInputs, str, JudgeMeta]):
    """Code checks + judge criteria on the drafted urgent-internal acknowledgement body
    (D-40, D-51), with the case's `hint` passed to the judge as allowed content -- it
    describes what will actually happen, not an invented promise."""

    judge_agent: Agent[None, list[CriterionVerdict]]

    async def evaluate(self, ctx: EvaluatorContext[AckInputs, str, JudgeMeta]) -> EvaluatorOutput:
        meta = ctx.metadata
        assert meta is not None
        return await _tone_check(
            self.judge_agent,
            original_email_text=ctx.inputs.email.text_body,
            body=ctx.output,
            criteria=meta.judge_criteria,
            max_sentences=meta.max_sentences,
            allowed_content=ctx.inputs.hint,
        )


@dataclass(repr=False)
class JudgeCalibrationMatch(Evaluator[JudgeInputs, str, JudgeCalibrationMeta]):
    """The fixed body's overall tone-check verdict must equal the pinned `expected_pass`
    (task 32 calibration)."""

    judge_agent: Agent[None, list[CriterionVerdict]]

    async def evaluate(
        self, ctx: EvaluatorContext[JudgeInputs, str, JudgeCalibrationMeta]
    ) -> EvaluatorOutput:
        meta = ctx.metadata
        assert meta is not None
        result = await _tone_check(
            self.judge_agent,
            original_email_text=ctx.inputs.original_email_text,
            body=ctx.output,
            criteria=meta.judge_criteria,
            max_sentences=meta.max_sentences,
        )
        passed = bool(result.value)
        if passed != meta.expected_pass:
            detail = f"; {result.reason}" if result.reason else ""
            return EvaluationReason(
                value=False,
                reason=(
                    f"expected overall {'pass' if meta.expected_pass else 'fail'}, got "
                    f"{'pass' if passed else 'fail'}{detail}"
                ),
            )
        return EvaluationReason(value=True)


# -- Building the pydantic-evals `Case`/`Dataset` objects --------------------------------


def _classify_pcase(case: ClassifyCase) -> Case[Email, Any, Any]:
    return Case(name=case.name, inputs=_build_email(case.email), expected_output=case.expected)


def _spam_pcase(case: SpamCase) -> Case[Email, Any, bool]:
    return Case(name=case.name, inputs=_build_email(case.email), expected_output=case.spam)


def _extract_pcase(case: ExtractCase) -> Case[Email, MeetingExtraction, Any]:
    expected = MeetingExtraction(
        proposals=case.expected.proposals,
        duration_minutes=case.expected.duration_minutes,
        timezone=case.expected.timezone,
        timezone_stated=case.expected.timezone_stated,
        extra_attendees=case.expected.extra_attendees,
        location=case.expected.location,
    )
    return Case(name=case.name, inputs=_build_email(case.email), expected_output=expected)


def _sales_pcase(case: SalesCase) -> Case[Email, str, JudgeMeta]:
    meta = JudgeMeta(judge_criteria=_SALES_JUDGE_CRITERIA, max_sentences=case.max_sentences)
    return Case(name=case.name, inputs=_build_email(case.email), metadata=meta)


def _courtesy_pcase(case: CourtesyCase) -> Case[CourtesyInputs, CourtesyDraft, JudgeMeta]:
    inputs = CourtesyInputs(email=_build_email(case.email), label=case.label)
    expected = CourtesyDraft(warranted=case.expected_warranted, reason="", body="")
    meta = JudgeMeta(
        judge_criteria=_COURTESY_JUDGE_CRITERIA,
        max_sentences=case.max_sentences,
        expected_body_prefix=case.expected_body_prefix,
    )
    return Case(name=case.name, inputs=inputs, expected_output=expected, metadata=meta)


def _ack_pcase(case: AckCase) -> Case[AckInputs, str, JudgeMeta]:
    inputs = AckInputs(email=_build_email(case.email), hint=URGENT_INTERNAL_HINT)
    meta = JudgeMeta(judge_criteria=_ACK_JUDGE_CRITERIA, max_sentences=case.max_sentences)
    return Case(name=case.name, inputs=inputs, metadata=meta)


def _judge_pcase(case: JudgeCase) -> Case[JudgeInputs, str, JudgeCalibrationMeta]:
    inputs = JudgeInputs(original_email_text=case.original_email_text, body=case.body)
    meta = JudgeCalibrationMeta(
        judge_criteria=_JUDGE_RUBRICS[case.rubric],
        max_sentences=case.max_sentences,
        expected_pass=case.expected_pass,
    )
    return Case(name=case.name, inputs=inputs, metadata=meta)


# -- Running and reporting ----------------------------------------------------------------


def _case_passed(case: ReportCase[Any, Any, Any]) -> bool:
    return all(bool(result.value) for result in case.assertions.values())


def _print_single_run_report(kind: str, report: EvaluationReport[Any, Any, Any]) -> bool:
    all_passed = True
    for case in report.cases:
        passed = _case_passed(case)
        all_passed = all_passed and passed
        print(f"[{kind}] {'PASS' if passed else 'FAIL'} {case.name}")
        if not passed:
            for eval_name, result in case.assertions.items():
                if not result.value:
                    print(f"    {eval_name}: {result.reason or 'mismatch'}")
    for failure in report.failures:
        all_passed = False
        print(f"[{kind}] ERROR {failure.name}: {failure.error_message}")
    return all_passed


def _print_repeated_report(kind: str, report: EvaluationReport[Any, Any, Any]) -> bool:
    """`--repeat N` reporting (task 32, D-51): a case passes only if all N runs pass;
    prints the per-case pass count (e.g. `PASS 3/3 name`)."""
    all_passed = True
    for group in report.case_groups() or []:
        passed_runs = sum(1 for run in group.runs if _case_passed(run))
        total_runs = len(group.runs) + len(group.failures)
        passed = passed_runs == total_runs and total_runs > 0
        all_passed = all_passed and passed
        print(f"[{kind}] {'PASS' if passed else 'FAIL'} {passed_runs}/{total_runs} {group.name}")
        if not passed:
            for run in group.runs:
                if not _case_passed(run):
                    for eval_name, result in run.assertions.items():
                        if not result.value:
                            print(f"    {eval_name}: {result.reason or 'mismatch'}")
            for failure in group.failures:
                print(f"    ERROR: {failure.error_message}")
    return all_passed


def _print_report(kind: str, report: EvaluationReport[Any, Any, Any], repeat: int) -> bool:
    if repeat > 1:
        return _print_repeated_report(kind, report)
    return _print_single_run_report(kind, report)


def main(case_filter: str | None, kind_filter: str | None, repeat: int = 1) -> int:
    """Run every eval case through the real LLM adapters; print a compact report.

    Loads settings from the process env (`load_settings()`) and builds the real model
    and adapters (never `tests/fakes`); runs sequentially (`max_concurrency=1`) to keep
    cost/rate predictable. Every case must pass; any mismatch is a non-zero exit.
    `repeat` (D-51: `dt-agent eval --repeat N`) runs each selected case N times; a case
    only counts as passing if all N runs pass (task 32).
    """
    settings = load_settings()
    model = build_model(settings)
    logger = get_logger("evals")

    judge_agent: Agent[None, list[CriterionVerdict]] = Agent(
        model,
        output_type=list[CriterionVerdict],
        instructions=load_prompt("judge"),
        model_settings=model_settings_for(settings, "classify"),
    )
    deps = Deps(
        classifier=LLMClassifier(model, settings, logger=logger),
        extractor=LLMMeetingExtractor(model, settings, logger=logger),
        reply_drafter=LLMReplyDrafter(model, settings, logger=logger),
        courtesy_drafter=LLMCourtesyDrafter(model, settings, logger=logger),
        acknowledgement_drafter=LLMAcknowledgementDrafter(model, settings, logger=logger),
        judge_agent=judge_agent,
    )

    cases = load_cases()
    if kind_filter:
        cases = [case for case in cases if case.kind == kind_filter]
    if case_filter:
        cases = [case for case in cases if case_filter in case.name]

    all_passed = True
    any_ran = False

    classify_cases = [case for case in cases if isinstance(case, ClassifyCase)]
    if classify_cases:
        any_ran = True
        dataset: Dataset[Email, Any, Any] = Dataset(
            name="classify",
            cases=[_classify_pcase(case) for case in classify_cases],
            evaluators=[LabelMatch(), UrgencyMatch()],
        )
        classify_report = dataset.evaluate_sync(
            functools.partial(_classify_task, deps),
            max_concurrency=1,
            progress=False,
            repeat=repeat,
        )
        all_passed = _print_report("classify", classify_report, repeat) and all_passed

    spam_cases = [case for case in cases if isinstance(case, SpamCase)]
    if spam_cases:
        any_ran = True
        spam_dataset: Dataset[Email, Any, bool] = Dataset(
            name="spam",
            cases=[_spam_pcase(case) for case in spam_cases],
            evaluators=[SpamMatch()],
        )
        spam_report = spam_dataset.evaluate_sync(
            functools.partial(_classify_task, deps),
            max_concurrency=1,
            progress=False,
            repeat=repeat,
        )
        all_passed = _print_report("spam", spam_report, repeat) and all_passed

    extract_cases = [case for case in cases if isinstance(case, ExtractCase)]
    if extract_cases:
        any_ran = True
        extract_dataset: Dataset[Email, MeetingExtraction, Any] = Dataset(
            name="extract",
            cases=[_extract_pcase(case) for case in extract_cases],
            evaluators=[ExtractionMatch()],
        )
        extract_report = extract_dataset.evaluate_sync(
            functools.partial(_extract_task, deps),
            max_concurrency=1,
            progress=False,
            repeat=repeat,
        )
        all_passed = _print_report("extract", extract_report, repeat) and all_passed

    sales_cases = [case for case in cases if isinstance(case, SalesCase)]
    if sales_cases:
        any_ran = True
        sales_dataset: Dataset[Email, str, JudgeMeta] = Dataset(
            name="sales",
            cases=[_sales_pcase(case) for case in sales_cases],
            evaluators=[SalesDeclineChecks(judge_agent=judge_agent)],
        )
        sales_report = sales_dataset.evaluate_sync(
            functools.partial(_sales_task, deps),
            max_concurrency=1,
            progress=False,
            repeat=repeat,
        )
        all_passed = _print_report("sales", sales_report, repeat) and all_passed

    courtesy_cases = [case for case in cases if isinstance(case, CourtesyCase)]
    if courtesy_cases:
        any_ran = True
        courtesy_dataset: Dataset[CourtesyInputs, CourtesyDraft, JudgeMeta] = Dataset(
            name="courtesy",
            cases=[_courtesy_pcase(case) for case in courtesy_cases],
            evaluators=[
                CourtesyWarrantedMatch(),
                CourtesyBodyChecks(judge_agent=judge_agent),
                CourtesyBodyPrefixMatch(),
            ],
        )
        courtesy_report = courtesy_dataset.evaluate_sync(
            functools.partial(_courtesy_task, deps),
            max_concurrency=1,
            progress=False,
            repeat=repeat,
        )
        all_passed = _print_report("courtesy", courtesy_report, repeat) and all_passed

    ack_cases = [case for case in cases if isinstance(case, AckCase)]
    if ack_cases:
        any_ran = True
        ack_dataset: Dataset[AckInputs, str, JudgeMeta] = Dataset(
            name="ack",
            cases=[_ack_pcase(case) for case in ack_cases],
            evaluators=[AcknowledgementChecks(judge_agent=judge_agent)],
        )
        ack_report = ack_dataset.evaluate_sync(
            functools.partial(_ack_task, deps),
            max_concurrency=1,
            progress=False,
            repeat=repeat,
        )
        all_passed = _print_report("ack", ack_report, repeat) and all_passed

    judge_cases = [case for case in cases if isinstance(case, JudgeCase)]
    if judge_cases:
        any_ran = True
        judge_dataset: Dataset[JudgeInputs, str, JudgeCalibrationMeta] = Dataset(
            name="judge",
            cases=[_judge_pcase(case) for case in judge_cases],
            evaluators=[JudgeCalibrationMatch(judge_agent=judge_agent)],
        )
        judge_report = judge_dataset.evaluate_sync(
            functools.partial(_judge_task, deps),
            max_concurrency=1,
            progress=False,
            repeat=repeat,
        )
        all_passed = _print_report("judge", judge_report, repeat) and all_passed

    if not any_ran:
        print("No eval cases matched the given filters.")
        return 1

    print("ALL PASSED" if all_passed else "FAILURES PRESENT")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main(None, None))

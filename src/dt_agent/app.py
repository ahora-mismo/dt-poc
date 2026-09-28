"""Composition root: CLI bootstrap and the running application (task 24).

`main()` is what `dt-agent run` (`cli.py`) calls: it loads configuration
(D-29), configures the unified logger (D-58) and then hands off to
`run_app`, the testable core of the whole bootstrap (ADR-3, D-6, D-18,
D-29, D-43, D-50, D-54, D-59). `configure_logging` is deliberately kept
out of `run_app` -- it replaces structlog's global configuration, which
would silently break `structlog.testing.capture_logs()` in tests that
want to observe `run_app`'s own log lines; only the real CLI path
(`main`) needs the real renderer.

**D-30 without touching `pipeline/workers.py`:** `WorkerPool` takes one
shared `handler` and one shared `mail` port, with no notion of "worker
identity" in its public shape. Rather than build one `WorkerPool` for
`WORKER_COUNT` workers, `run_app` builds `WORKER_COUNT` separate
`WorkerPool` instances, each with `worker_count=1` and its own
`Factories.mail()` / `Factories.calendar()` instances (hence its own
`PipelineDeps` and its own `make_email_handler` closure) -- so every
worker still owns its own Gmail/Calendar client, exactly as D-30 asks. All pools
share the single queue: `MemoryObjectReceiveStream.clone()` is designed
for exactly this fan-out (each pool's own `run()` clones the same
receive end once, for its one worker). The poller gets one further,
separate `mail()` instance (D-31); the MCP server, when enabled, gets its
own `mail()`/`calendar()` pair too. Only `WorkerPool`'s own bookkeeping
`mail` parameter (used solely for post-exhaustion `AI/Error` labelling,
D-43) is, by construction, that pool's single worker's own adapter --
never shared across pools. Each pool gets `first_worker_id=<its index>`, so
every worker logs its own `worker_id` (D-58).
"""

from __future__ import annotations

import random
import signal
import sys
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from typing import Any

import anyio

from dt_agent.adapters.calendar import build_calendar_adapter
from dt_agent.adapters.gmail import build_gmail_adapter
from dt_agent.adapters.google_auth import build_credentials
from dt_agent.adapters.llm.acknowledgement_drafter import LLMAcknowledgementDrafter
from dt_agent.adapters.llm.classifier import LLMClassifier
from dt_agent.adapters.llm.courtesy_drafter import LLMCourtesyDrafter
from dt_agent.adapters.llm.meeting_extractor import LLMMeetingExtractor
from dt_agent.adapters.llm.providers import build_model, log_startup_notice
from dt_agent.adapters.llm.reply_drafter import LLMReplyDrafter
from dt_agent.adapters.sqlite_store import InvalidDbReport, open_store
from dt_agent.config import ConfigurationError, Settings, load_settings
from dt_agent.domain.clock import Clock, SystemClock
from dt_agent.domain.labels import (
    AI_ERROR_LABEL,
    AI_NEEDS_REVIEW_LABEL,
    FINANCE_LABEL,
    PRIMARY_GMAIL_LABELS,
    URGENT_LABEL,
)
from dt_agent.logging import configure_logging, get_logger
from dt_agent.mcp_server import serve_mcp
from dt_agent.pipeline.approvals import handle_approval_reply
from dt_agent.pipeline.deps import PipelineDeps
from dt_agent.pipeline.inflight import InFlight
from dt_agent.pipeline.poller import Poller
from dt_agent.pipeline.process_email import make_email_handler
from dt_agent.pipeline.workers import WorkerPool, create_queue
from dt_agent.ports.calendar import CalendarPort
from dt_agent.ports.errors import FatalAuthError
from dt_agent.ports.llm import (
    AcknowledgementDrafter,
    Classifier,
    CourtesyDrafter,
    MeetingExtractor,
    ReplyDrafter,
)
from dt_agent.ports.mail import MailPort
from dt_agent.ports.sleep import Sleeper

#: Every Gmail label the app must ensure exists at startup (D-25): the six
#: primary labels, the URGENT modifier, and the three system labels the
#: policy engine (never the LLM) applies.
ALL_LABELS: tuple[str, ...] = (
    *PRIMARY_GMAIL_LABELS,
    URGENT_LABEL,
    FINANCE_LABEL,
    AI_ERROR_LABEL,
    AI_NEEDS_REVIEW_LABEL,
)

#: D-43's per-message retry backoff isn't itself a configuration variable
#: (only `MAX_ATTEMPTS`, how many times, is); this is `run_app`'s own fixed
#: choice of how long to wait between attempts.
_RETRY_DELAY_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class LlmPorts:
    """The five LLM-backed ports, sharing one `Model` (D-36); built once, shared by every worker."""

    classifier: Classifier
    meeting_extractor: MeetingExtractor
    reply_drafter: ReplyDrafter
    courtesy_drafter: CourtesyDrafter
    acknowledgement_drafter: AcknowledgementDrafter


@dataclass(frozen=True, slots=True)
class Factories:
    """Every construction `run_app` needs, injected so it's testable without real I/O (D-52).

    `mail` and `calendar` each build a *fresh* adapter instance per call
    (D-30): `run_app` calls `mail()` once per worker, once for the poller,
    and once more for the MCP server when enabled; `calendar()` once per
    worker and once for the MCP server.
    """

    mail: Callable[[], MailPort]
    calendar: Callable[[str], CalendarPort]
    llm: Callable[[Settings], LlmPorts]
    # `Coroutine`, not the more general `Awaitable`: this is what `run_app` passes
    # straight to `anyio.abc.TaskGroup.start_soon`, whose signature requires it.
    serve_mcp: Callable[[PipelineDeps, str, int], Coroutine[Any, Any, None]]
    sleeper: Sleeper
    jitter: Callable[[], float]


def _build_llm_ports(settings: Settings) -> LlmPorts:
    model = build_model(settings)
    return LlmPorts(
        classifier=LLMClassifier(model, settings),
        meeting_extractor=LLMMeetingExtractor(model, settings),
        reply_drafter=LLMReplyDrafter(model, settings),
        courtesy_drafter=LLMCourtesyDrafter(model, settings),
        acknowledgement_drafter=LLMAcknowledgementDrafter(model, settings),
    )


def _default_jitter() -> float:
    """Small random pause added to `POLL_INTERVAL_SECONDS` (D-19)."""
    return random.uniform(0.0, 2.0)


def real_factories(settings: Settings) -> Factories:
    """The production `Factories`: real Google credentials, real adapters (D-18, D-30)."""
    credentials = build_credentials(
        settings.GOOGLE_CLIENT_ID.get_secret_value(),
        settings.GOOGLE_CLIENT_SECRET.get_secret_value(),
        settings.GOOGLE_REFRESH_TOKEN.get_secret_value(),
    )
    return Factories(
        mail=lambda: build_gmail_adapter(credentials),
        calendar=lambda mailbox: build_calendar_adapter(credentials, mailbox),
        llm=_build_llm_ports,
        serve_mcp=lambda deps, host, port: serve_mcp(deps, host=host, port=port),
        sleeper=anyio.sleep,
        jitter=_default_jitter,
    )


def _fatal_auth_errors(group: BaseExceptionGroup[FatalAuthError]) -> Iterator[FatalAuthError]:
    """Every `FatalAuthError` leaf inside `group`, however deeply it's nested.

    `except*` matches a `BaseExceptionGroup` at any nesting depth, but a
    `FatalAuthError` raised from the poller (a task started from
    `workers_tg`'s own body, not via `start_soon`) surfaces as a *group
    containing a group* (anyio wraps a task group's host-body exception
    too), whose `str()` is a useless "unhandled errors ..." placeholder.
    Flattening recursively is what makes the runtime log line actually
    name the real error, regardless of which task raised it.
    """
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            yield from _fatal_auth_errors(exc)
        else:
            yield exc


def _invalid_db_banner(report: InvalidDbReport) -> str:
    """A clear, multi-line startup banner for an invalid database (D-6, D-54)."""
    renamed = ", ".join(str(path) for path in report.renamed_to)
    border = "=" * 70
    return (
        f"{border}\n"
        "DATABASE INVALID: renamed to " + renamed + f" (reason: {report.reason}).\n"
        "A new database was created.\n"
        f"{border}"
    )


async def run_app(
    settings: Settings,
    *,
    factories: Factories,
    clock: Clock,
    stop_event: anyio.Event,
) -> int:
    """Bootstrap and run the agent until `stop_event` fires; the CLI's testable core (task 24).

    Assumes the caller already configured the logger (`configure_logging`);
    see the module docstring for why. Returns the process exit code: 0 on
    graceful shutdown, non-zero when a `FatalAuthError` is hit (at startup
    or while running, D-43/D-50).
    """
    logger = get_logger(__name__)
    log_startup_notice(settings, logger)

    store, report = open_store(settings.DB_PATH, clock)
    if report is not None:
        logger.error(
            "startup.database_invalid",
            old_path=str(report.old_path),
            renamed_to=[str(path) for path in report.renamed_to],
            reason=report.reason,
        )
        print(_invalid_db_banner(report))
    purged = await store.purge_older_than(settings.RETENTION_DAYS)
    logger.debug("startup.purge", purged=purged, retention_days=settings.RETENTION_DAYS)

    poller_mail = factories.mail()
    try:
        mailbox_address = await poller_mail.get_profile_address()
        approver = settings.approver_address(mailbox_address)
        logger.info(
            "startup.approver",
            mailbox=mailbox_address,
            approver=approver,
            external=settings.is_manager_external(mailbox_address),
        )
        if not settings.INTERNAL_DOMAINS:
            # D-42: with no internal domains configured, nobody is internal, so the
            # "urgent message from internal sender" scenario (D-40) can never trigger.
            logger.warning(
                "startup.internal_domains_empty",
                message="INTERNAL_DOMAINS is empty: the urgent-internal scenario cannot trigger",
            )
        await poller_mail.ensure_labels(ALL_LABELS)
    except FatalAuthError as exc:
        logger.error("startup.fatal_auth_error", error=str(exc))
        await store.close()
        return 1

    try:
        llm_ports = factories.llm(settings)

        send_stream, receive_stream = create_queue()
        # Shared by the poller and every worker pool (task 35): an id
        # queued or being processed is never queued a second time.
        in_flight = InFlight()
        pools: list[WorkerPool] = []
        for worker_id in range(settings.WORKER_COUNT):
            worker_mail = factories.mail()
            worker_calendar = factories.calendar(mailbox_address)
            deps = PipelineDeps(
                settings=settings,
                mailbox_address=mailbox_address,
                mail=worker_mail,
                calendar=worker_calendar,
                store=store,
                classifier=llm_ports.classifier,
                meeting_extractor=llm_ports.meeting_extractor,
                reply_drafter=llm_ports.reply_drafter,
                courtesy_drafter=llm_ports.courtesy_drafter,
                acknowledgement_drafter=llm_ports.acknowledgement_drafter,
                clock=clock,
            )
            handler = make_email_handler(deps, handle_approval_reply)
            pools.append(
                WorkerPool(
                    receive_stream,
                    worker_count=1,
                    first_worker_id=worker_id,
                    handler=handler,
                    store=store,
                    mail=worker_mail,
                    max_attempts=settings.MAX_ATTEMPTS,
                    action_delay_ms=settings.ACTION_DELAY_MS,
                    sleeper=factories.sleeper,
                    retry_delay_seconds=_RETRY_DELAY_SECONDS,
                    in_flight=in_flight,
                )
            )

        poller = Poller(
            poller_mail,
            store,
            send_stream.send_nowait,
            backfill_messages=settings.STARTUP_BACKFILL_MESSAGES,
            retention_days=settings.RETENTION_DAYS,
            poll_interval_seconds=settings.POLL_INTERVAL_SECONDS,
            clock=clock,
            sleeper=factories.sleeper,
            jitter=factories.jitter,
            in_flight=in_flight,
        )

        exit_code = 0
        try:
            async with anyio.create_task_group() as workers_tg:
                for pool in pools:
                    workers_tg.start_soon(pool.run)
                async with anyio.create_task_group() as services_tg:
                    services_tg.start_soon(poller.run)
                    if settings.ENABLE_MCP:
                        assert settings.MCP_PORT is not None  # guaranteed by load_settings (D-65)
                        assert settings.MCP_HOST is not None  # guaranteed by load_settings (D-65)
                        mcp_deps = PipelineDeps(
                            settings=settings,
                            mailbox_address=mailbox_address,
                            mail=factories.mail(),
                            calendar=factories.calendar(mailbox_address),
                            store=store,
                            classifier=llm_ports.classifier,
                            meeting_extractor=llm_ports.meeting_extractor,
                            reply_drafter=llm_ports.reply_drafter,
                            courtesy_drafter=llm_ports.courtesy_drafter,
                            acknowledgement_drafter=llm_ports.acknowledgement_drafter,
                            clock=clock,
                        )
                        services_tg.start_soon(
                            factories.serve_mcp, mcp_deps, settings.MCP_HOST, settings.MCP_PORT
                        )
                    await stop_event.wait()
                    services_tg.cancel_scope.cancel()
                send_stream.close()  # D-30: workers finish their current message, then exit
        except* FatalAuthError as excgroup:
            for fatal_error in _fatal_auth_errors(excgroup):
                logger.error("runtime.fatal_auth_error", error=str(fatal_error))
            exit_code = 1
        return exit_code
    finally:
        await store.close()


async def _watch_signals(stop_event: anyio.Event) -> None:
    """Set `stop_event` on the first SIGINT/SIGTERM, for graceful shutdown (D-43, D-50)."""
    with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async for _ in signals:
            stop_event.set()
            return


async def _run_until_stopped(
    settings: Settings, factories: Factories, stop_event: anyio.Event
) -> int:
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_watch_signals, stop_event)
        exit_code = await run_app(
            settings, factories=factories, clock=SystemClock(), stop_event=stop_event
        )
        task_group.cancel_scope.cancel()  # stop the signal watcher; run_app already finished
    return exit_code


def main() -> int:
    """`dt-agent run`'s real entry point: load config, wire real adapters, run (task 24)."""
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    configure_logging(settings.MIN_LOG_LEVEL, settings.LOG_COLOR)
    factories = real_factories(settings)
    stop_event = anyio.Event()
    return anyio.run(_run_until_stopped, settings, factories, stop_event)


__all__ = ["Factories", "LlmPorts", "main", "real_factories", "run_app"]

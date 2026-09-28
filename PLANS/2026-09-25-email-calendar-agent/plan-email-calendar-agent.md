# AI Email & Calendar Assistant

**Last updated:** 2026-09-28 04:48 EEST

**Phase:** 3 — Implementation

---

## Summary

A containerized Python agent that monitors a Gmail inbox, classifies each incoming email into a predefined label set, and takes autonomous actions (archive, relabel, reply, spam, calendar scheduling). Any reply sent on behalf of the manager and any calendar action requires prior manager approval via email (APPROVE / REJECT). Every processed email produces a structured audit log entry; failures are handled gracefully and logged. Gmail account, Google credentials and LLM credentials are parameterized through environment variables.

Implementation follows a strict **red-green TDD** approach: for every task, write a failing test first, then the minimal code to make it pass, then refactor.

Quality assurance has two parts:
- **Unit tests:** deterministic logic, with external services (Gmail, Calendar, LLM) isolated (see D-52).
- **Evals:** measured quality of LLM-driven behavior (classification, extraction, drafting) against a labeled dataset (see D-51).

Technology stack: Python 3.14 (ADR-1), PydanticAI for the LLM layer (ADR-2), and a light composed application base: asyncio + Typer + pydantic-settings + SQLite + tenacity + structlog (ADR-3). It runs as one process watching one mailbox; the only port is the optional MCP server's, reachable from localhost only (D-3, D-4, D-59).

---

## ADR

### ADR-1: Python version — 3.14

**Status:** ✅ Approved by user

**Context:**
- Python ships one feature release per year (October), each supported for about 5 years.
- The candidates were:
  - **3.13** (Oct 2024): conservative, widest wheel coverage, supported until ~Oct 2029.
  - **3.14** (Oct 2025): latest stable, supported until ~Oct 2030.
  - **3.15:** scheduled for October 2026; not a final release yet, and libraries typically lag months behind a new release.
- The application is I/O-bound (Gmail, Calendar and LLM API calls), relies heavily on Pydantic models, and depends on third-party libraries. Some of those have compiled components (e.g. `pydantic-core`, `grpcio`) that need wheels built for the target Python version.

**Decision:**
- Use **Python 3.14** (latest stable). It gives the longest support window and has been on the market for about a year, so major libraries ship 3.14 wheels.
- Relevant improvements:
  - deferred evaluation of annotations, which benefits Pydantic-heavy code;
  - template strings (t-strings);
  - clearer error messages;
  - asyncio introspection tools.
- Use the standard (GIL) build. The free-threaded build brings no benefit to an I/O-bound asyncio application.

**Consequences:**
- `pyproject.toml` pins `requires-python = ">=3.14,<3.15"`, so an unplanned move to 3.15 can't happen before dependencies support it.
- The container is built on the `python:3.14-slim` base image. Local development uses 3.14 via `uv`.
- An install spike of the chosen framework (ADR-2) and the Google client libraries confirms 3.14 wheels (results in ADR-2 and ADR-3). If a required dependency lacks 3.14 support, falling back to 3.13 requires revisiting this ADR.
- Upgrading to 3.15 is a future, deliberate decision, made once the dependency set supports it.

### ADR-2: LLM layer — PydanticAI behind our own ports

**Status:** ✅ Approved by user

**Source:** TBD-1

**Context:**
- The LLM part of the application is essentially a set of typed functions: classify an email, extract meeting details, draft a reply. It is called by our own pipeline. A deterministic policy engine, not the LLM, decides and executes actions, so the LLM never calls side-effecting tools.
- Needs:
  - reliable typed/structured output;
  - several providers behind one API (Gemini, Claude and Azure OpenAI, D-36);
  - deterministic fakes for red-green TDD;
  - self-hosted evals and observability (D-2);
  - MCP support (D-59);
  - Python 3.14 support (ADR-1);
  - light dependencies.
- Approvals travel inside signed emails (D-5), so the framework doesn't need durable, resumable state for human-in-the-loop. That removes the main advantage of graph/checkpoint frameworks for this application.
- None of the candidate frameworks is an application framework. The app base is decided separately (ADR-3).

**Decision:**
- Use **PydanticAI** (`pydantic-ai-slim` with only the provider extras actually used) for all LLM calls.
- It sits **behind our own ports** (`Classifier`, `MeetingExtractor`, `ReplyDrafter`, `CourtesyDrafter`, `AcknowledgementDrafter`) that return our own Pydantic models. The rest of the code never imports PydanticAI.
- Used features: typed `output_type` with automatic validation retries, provider model classes (Gemini, Anthropic, Azure OpenAI; D-36), `UsageLimits`, `TestModel` / `FunctionModel` for tests, `pydantic-evals` for evals (D-51).
- Gmail and Calendar are **not** accessed through any framework toolkit. We write thin typed adapters (`MailPort`, `CalendarPort`) over the official `google-api-python-client` + `google-auth`, because framework toolkits are LLM-callable (they bypass the policy/approval layer), cover only part of our needs, and default to file-based auth.

**Alternatives considered and discarded:**

| Alternative | Reason discarded |
|---|---|
| LangChain + LangGraph | Its main strength (durable checkpoints / `interrupt()` for human approval) is unused because approvals live in email; heavier stack and larger API; weaker typed output and test fakes; best tracing/evals tooling (LangSmith) is SaaS (D-2); its Gmail/Calendar toolkits are LLM-callable. Strongest runner-up, especially for complex multi-agent orchestration. |
| LlamaIndex | RAG-centric design and many packages add weight not needed now; weaker typed output, test fakes and classification evals. Revisit if RAG becomes a requirement (its retrieval parts could then be used as a library). |
| OpenAI Agents SDK | OpenAI-specific (vendor lock-in); non-OpenAI providers only via LiteLLM; tracing defaults to OpenAI's platform. |
| Google ADK | GCP/Gemini-centric (Azure only via LiteLLM); unclear benefit for supporting many different agents. |
| Claude Agent SDK | Claude-only (no OpenAI / Azure OpenAI); built for open-ended coding-style agents; needs the Claude Code runtime (Node.js) in the image. Claude models are used through the plain Anthropic API instead (D-36). |
| Microsoft Agent Framework | Newest framework with API churn risk; Azure-leaning; its workflow checkpointing is unused (approvals in email). |
| CrewAI | Role-play abstractions don't fit a deterministic triage pipeline; harder to test; telemetry on by default. |
| Composio / Arcade | SaaS in the data path (D-2). |
| No framework (provider SDKs + Instructor) | Viable for three LLM calls, but we would have to build and maintain provider switching, failover, test fakes, evals, tracing and usage limits ourselves. |
| smolagents, Agno, DSPy, Haystack | Code-executing agents are unsafe for untrusted email input (smolagents); smaller ecosystem (Agno); prompt optimization rather than orchestration (DSPy); pipeline/RAG oriented (Haystack). |

**Consequences:**
- One evolving dependency (2.x with frequent releases): pinned in `uv.lock`, and contained by the ports, so a framework swap means rewriting only the adapter.
- Excluded under D-2: the Pydantic AI Gateway provider and Logfire. Observability is the structured log on stdout (D-57, D-58).
- Unit tests block real model requests globally and use `TestModel` / `FunctionModel` or fakes of our own ports.
- Dependencies: `pydantic-ai-slim[anthropic,google,openai]` 2.51.0 (`pydantic` 2.13.5, `pydantic-core` 2.46.5) and `pydantic-evals` 2.51.0, with `google-api-python-client` 2.200.0, `PyJWT` 2.15.0 and `pydantic-settings` 2.15.0, on Python 3.14.3.

### ADR-3: Application base — composed minimal stack (asyncio + Typer + pydantic-settings + SQLite + tenacity + structlog)

**Status:** ✅ Approved by user

**Source:** TBD-27

**Context:**
- The application is started and runs until stopped. It is **one process watching one mailbox** (D-3) with **no web port** except the optional MCP server (D-4, D-59). Work is: startup checks → backfill of recent messages → live intake → one processing queue served by a small worker pool (D-6, D-7, D-30).
- Approvals live in signed emails (D-5), so nothing waits for hours inside the process. State is limited to one SQLite table of processed messages (D-6, D-26).
- Every option, including durable-workflow engines, still needs our own per-action idempotency record. Durable engines give at-least-once step execution, so "never send the same reply twice" always comes from checking our own record before acting. **The approvals-in-email decision (D-5) is what removes the need for a durable-workflow engine.**
- Eleven combined stacks were compared. All libraries of the chosen stack install and import on Python 3.14.3. Options 9–11 (Celery/Dramatiq/arq, Temporal, Prefect) were **not** spike-tested.

**Decision:**
- **Runtime:** `asyncio` through `anyio` task groups (graceful SIGINT/SIGTERM shutdown), a manual composition root (`app.py`) building ports/adapters from `Settings`.
- **CLI:** Typer.
- **Config:** pydantic-settings, reading only the process environment (D-53).
- **State:** stdlib `sqlite3` behind a thin repository; WAL mode.
- **Retries:** tenacity. **Logging / audit:** structlog (JSON lines).
- Optional later swaps that don't change the architecture: `dishka` (DI) if the object graph grows, `stamina` for retries, Cyclopts instead of Typer.

**Alternatives considered and discarded:**

| # | Alternative | Reason discarded |
|---|---|---|
| 1 | Standard library only | Same shape as the chosen stack, but with untyped config and more code to write and test. |
| 3 | Newer variant (anyio, Cyclopts, dishka, aiosqlite, stamina) | Treated as optional swaps within the chosen stack, not a separate stack; `anyio` is used for task groups and the queue. |
| 4 | aiomisc service framework | Saves ~50 lines; own conventions, extra dependencies and logging that overlaps structlog; small community. |
| 5 | APScheduler | Only one fixed interval is needed (a plain loop in the poller, D-31); 4.x still pre-release. |
| 6 | DBOS Transact | Too heavy for our needs: workflow determinism rules, framework-owned tables, SQLAlchemy/psycopg dependencies even in SQLite mode, SQLite labelled by the vendor as development/testing only; steps are at-least-once, so our idempotency record is still needed. |
| 7 | Cement | Daemon mode detaches the process (anti-pattern in Docker); sync-oriented; overlaps pydantic-settings/structlog; small community. |
| 8 | Taskiq | In-memory broker adds nothing; Redis needs an extra package and container; young. |
| 9 | Celery / Dramatiq / arq | Broker infrastructure (Redis/RabbitMQ) for one mailbox; sync-first (Celery). |
| 10 | Temporal | Self-hosted server cluster + database; its strength (durable waits for humans) is designed out by D-5. |
| 11 | Prefect | Needs a server for scheduling/UI; data-pipeline oriented; heavy. |
| — | FastAPI / Litestar / Starlette | No web API (D-4); the MCP server's HTTP transport comes from the `mcp` SDK (D-59). An HTTP layer can be added later without touching the core if a concrete need appears. |

**Consequences:**
- We own and unit-test the lifecycle glue: startup checks, poll loop, graceful shutdown, queue and worker pool (D-30), error handling and retry policies (D-43). Roughly 200 lines plus tests.
- `google-api-python-client` is blocking and not thread-safe: every Google API call runs in a thread, and every worker (and the poller) uses its own client instance (D-30).
- No Docker health check: a crashed process stops the container and the restart policy handles it (D-50); hangs are prevented by timeouts with retries on every Google and LLM call (D-36, D-43).
- The app's output is its structured log on stdout (D-57); the MCP server runs inside the app over HTTP when enabled (D-59).
- Moving to multiple mailboxes/instances later would reopen this decision (DBOS on Postgres or a queue would then be reconsidered).

### ADR-4: Calendar scheduling — LLM extracts, code schedules against the manager's calendar only

**Status:** ✅ Approved by user

**Source:** TBD-7

**Context:**
- Requirement: "Calendar events must respect existing availability and be planned reasonably"; the calendar is pre-populated with events.
- Meeting requests range from one exact time to several times, an open window ("next week"), or no time at all. Senders may state a timezone or not (D-1).
- Only the agent's own Google Calendar is accessible; other participants' calendars aren't.

**Decision:**
- **Extraction (LLM, `MeetingExtractor` port):** title, the proposed times as a list of **date expressions** (D-64; the LLM never computes dates), duration if given, timezone and whether it was stated or assumed, extra attendees explicitly named in the email (the sender is added by code), location or meeting link. The LLM never checks the calendar or picks slots.
- **Scheduling (deterministic code):**
  - availability comes from `freebusy.query` on **the manager's own calendar only**;
  - **business hours:** `BUSINESS_HOURS_START` (`.env.sample`: 9) to `BUSINESS_HOURS_END` (`.env.sample`: 18), Monday–Friday, in `DEFAULT_TIMEZONE`. Events are proposed inside these hours; a requested time outside them is treated exactly like a busy slot (D-14);
  - **rounding:** every proposed time is rounded **up** to the next 10 minutes (a request for 14:04 is proposed as 14:10);
  - **default duration:** `DEFAULT_MEETING_MINUTES` (`.env.sample`: 30) when the request gives none;
  - **search horizon** when no time is given: `SCHEDULING_HORIZON_BUSINESS_DAYS` (`.env.sample`: 5);
  - **slots the agent picks itself** (window or no time given) start only at :00 or :30, earliest first, and at least **25 minutes from now** (`MEETING_MIN_LEAD_MINUTES`, `.env.sample`: 25): now + 25 minutes, rounded up to the next :00 or :30 (a request handled at 10:36 → 11:01 → first slot 11:30);
  - no buffer between meetings;
  - **number of options / alternatives:** `MEETING_MAX_OPTIONS` (`.env.sample`: 3).
- **Options by request type:**
  - every requested time available → one option per requested time (an exact time as itself, a window by its first free slot inside business hours), in time order, no alternatives (D-13);
  - the only requested time is an exact time that is busy or outside business hours → D-14: up to `MEETING_MAX_OPTIONS`−1 free alternatives near it come first (option 1 = the first free one, so plain `APPROVE` never double-books); the requested time is the last numbered option, marked as a double booking or as outside business hours;
  - several requested times, some unavailable → the free requested ones first, filled up to `MEETING_MAX_OPTIONS` free options with the nearest free slots, then every unavailable one, marked (D-14);
  - no free slot in any requested window → the nearest free slots after the last window's end, within the horizon, marked as outside the requested interval (D-45);
  - no time, or all proposed times in the past → free slots within the horizon.

- **Timezone:** only a timezone written in the email text counts; otherwise EET/EEST (D-1). The `Date:` header offset is ignored (it reflects the sending client's or server's clock, often UTC for webmail, and isn't a timezone).
- **Event creation after approval:** `events.insert` with an explicit `timeZone` and `sendUpdates=all`. Google's invitation is the answer to the requester (no separate reply).
- **Attendees, with a guardrail against hallucinated addresses:**
  - the approver address (D-46; de-duplicated with the organizer, the monitored mailbox) and the email's sender are always added **by deterministic code**, never by the LLM;
  - the LLM extracts **only the extra** people the email asks to invite (e.g. "invite Anna" → Anna's address from Cc; "invite us all" → every recipient of the email);
  - deterministic code parses the email's From/To/Cc headers and **drops any LLM-returned address that isn't present there**. This prevents invented addresses and exposing the meeting to people who weren't on the email.
- **Options count:** `MEETING_MAX_OPTIONS` is the single setting for both the options the agent picks and the alternatives shown in D-14.
- **Time display:** times are shown in the manager's timezone. When the requester stated a different timezone, both are shown (e.g. "15:00 EEST / 14:00 CEST").
- **Internals:** `zoneinfo` + Pydantic `AwareDatetime` (naive datetimes rejected), comparisons in UTC; date expressions (D-64) resolved by code from the email's received time in the resolved timezone; DST edge cases are not handled (D-66).

**Consequences:**
- **Known limitation:** only the manager's calendar is checked. Other participants' availability is unknown; the approval email states this.
- Slot search and free/busy logic are pure functions (candidates + busy intervals + business hours → options) and fully unit-testable, including DST changes and weekends.
- The business-hours and scheduling values are env vars in the configuration object (D-29).

---

## Approved Decisions

### D-1: Default timezone is EET/EEST, DST-aware

- When a timezone can't be determined, times are interpreted as Eastern European local time: **EET (UTC+2) or EEST (UTC+3) depending on the date**.
- Configured as an IANA zone in `DEFAULT_TIMEZONE` (`.env.sample`: `Europe/Bucharest`; any EET/EEST IANA zone with the same DST rules is equivalent). The approval email explicitly states that the timezone was assumed.
- The application is timezone-aware throughout: no naive datetimes.
- Source: TBD-7
- ✅ Approved by user

### D-2: No SaaS outside the LLM provider

- Orchestration, integrations (Gmail/Calendar connectors), observability/tracing and evals must be open-source and fully hosted by us. No SaaS such as Composio, Arcade, LangSmith, Logfire, LlamaCloud or OpenAI hosted connectors.
- **Exception:** the LLM itself is consumed as a hosted API (D-36); no local LLM.
- Source: TBD-1
- ✅ Approved by user

### D-3: One process, one mailbox (for now)

- The agent runs as a single process monitoring a single Gmail mailbox. Multiple mailboxes/instances are out of scope for now; supporting them later would reopen ADR-3.
- Source: TBD-27
- ✅ Approved by user

### D-4: No web port, except the optional MCP server

- The application is started and works as long as it is running. It exposes no HTTP server or inbound port, except the MCP server when `ENABLE_MCP=true` (D-59), published to localhost only. There is no health check (ADR-3).
- Source: TBD-27
- ✅ Approved by user

### D-5: Approvals live in the email (stateless), validated by a signed JWT

- The approval email carries every detail needed to execute the action in a signed JWT embedded in the body between delimiters. The manager replies APPROVE or REJECT.
- The reply is validated by the JWT signature and thread binding. No pending-approval state is stored.
- Details: D-32 to D-35.
- Source: TBD-5
- ✅ Approved by user

### D-6: SQLite state on a durable volume, with startup validation

- The SQLite database file lives on a Docker volume (durable across restarts).
- On startup:
  - if the file doesn't exist, it is created;
  - if it exists but is invalid, it is renamed to `<name>-failed-<UTC timestamp>-UTC.<ext>` (D-54), a new one is created, and a visible error is shown on screen and in the logs explaining what happened.
- Validity criteria, rename format and error reporting: D-54. What is stored: D-26. Retention: D-28.
- Source: TBD-6
- ✅ Approved by user

### D-7: Startup backfill + live intake through one queue

- On startup, the agent reads the last **X** INBOX messages from Gmail (X = int env var, `STARTUP_BACKFILL_MESSAGES`; 0 = live messages only) and compares their IDs with the IDs in SQLite. Messages not found are considered new and processed. Never more than X older messages are read on startup. Intake mechanics: D-31.
- Backfilled and live messages go through **the same processing queue** and are processed in exactly the same way by the worker pool (D-30); each worker handles one message at a time.
- After the backlog is queued, the agent continues with live intake (polling, D-17 / D-19).
- A message is recorded in SQLite at the end of its processing flow; a crash mid-flow means reprocessing (D-27).
- This supports both a live inbox and an inbox with messages already present.
- Source: TBD-15
- ✅ Approved by user

### D-8: Approval token embedded in the email body between delimiters

- The approval data is a signed JWT (compact form, already base64url-encoded) placed **inside the email body** between BEGIN/END delimiter lines, not as an attachment. Replies usually quote the body but drop attachments, so the token survives the round trip.
- Source: TBD-5
- ✅ Approved by user

### D-9: Failed approval validation is treated as phishing

- When an approval reply fails authenticity/integrity validation (signature, token binding), the failure is logged first (audit requirement), then the message is labeled SPAM and moved to the Spam folder.
- Expired but authentic approvals are handled by D-11, not as spam.
- Source: TBD-5
- ✅ Approved by user

### D-10: Approval expiry is 3 business days, configurable

- `APPROVAL_EXPIRY_BUSINESS_DAYS` (int env var; `.env.sample`: 3).
- Business days are Monday–Friday; the approval expires at the same time of day, N business days after it was sent.
- Public holidays are ignored (proof-of-concept scope).
- Source: TBD-5
- ✅ Approved by user

### D-11: Replies to expired approvals

- The reply is logged. The agent replies to the manager that the action was **not** taken because the approval request expired.
- The agent also sends a **new approval request in the same format with a new token**, in the same thread; only the latest token in the thread is valid (D-34). For calendar actions this happens only if the event is **not in the past**. There is no limit on re-issues.
- An approval for a calendar option whose time has already passed (the token itself still valid) is handled the same way: not executed, and a new approval request with new slots is sent.
- **A re-issued approval keeps the urgent marking:** the token's reply action carries `urgent` (set on the urgent-internal acknowledgement, D-40), so the re-issue again carries the urgent subject `[Approval] URGENT from <sender>: <subject> — reply APPROVE to send the acknowledgement` (D-40). Tokens without the field read as not urgent.
- Source: TBD-5
- ✅ Approved by user

### D-12: Approved, but the interval was taken meanwhile

- If the manager approves but the interval has been taken in the meantime, the event is not created, and the agent sends a new approval request (same thread, new token, D-34) in the D-14 format (free alternatives first; the original interval last, marked as a double booking).
- Only the manager is notified; the original requester isn't.
- Source: TBD-5
- ✅ Approved by user

### D-13: Multiple slot options in one approval email

- **What was asked is always shown:** the approval email's `Requested:` section lists every requested time in the requester's own words, with its resolved time and whether it is available (`(available)`, or `NOT AVAILABLE: already booked (…)` / `outside business hours` / `no free time in that period`); when one is not available and the agent adds its own options, it says "The ALTERNATIVE options below were not requested."
- **Option tags:** when the sender asked for times, every option is tagged `REQUESTED ("…")` or `ALTERNATIVE (not requested)`.
- **REJECT on a meeting request from an email's text:** no meeting, and the requester gets a fixed, kind reply in the original thread ("Thank you for your meeting request. Unfortunately, the time you proposed isn't available. Please feel free to suggest another time."), signed with `AGENT_SENDER_NAME`; the approval email previews it. A received calendar invitation is unchanged: REJECT declines it (D-48), no reply.
- **One option per requested time**, windows and exact times together, in time order: an exact time is offered as itself; a window by its first free slot inside business hours.
- **Agent-chosen alternatives only when needed:** when a requested time is unavailable (D-14, D-45), or when no time was given (then the agent proposes free slots).
- One approval email lists **numbered options** and explains them in the body.
- **Option 1 is the main slot:** the first available slot that isn't in the past.
- Replying **`APPROVE` (or a variation that clearly means approval) is identical to `APPROVE 1`**; `APPROVE <n>` picks option n; `REJECT` rejects.
- The accepted approval words are listed in D-34.
- Source: TBD-25
- ✅ Approved by user

### D-14: Requested interval not available (busy or outside business hours)

- The approval email says there is a proposal, but the requested interval is busy or outside business hours (ADR-4), and says which.
- The email lists the available intervals as **numbered alternatives first** (D-13), so option 1 and a plain `APPROVE` always pick a free slot.
- The requested interval is listed **last** and can still be approved with an explicit `APPROVE <n>`. It is marked exactly for what it is: **"DOUBLE BOOKING — conflicts with: 14:00–15:00 Board meeting"** (D-55), or **"OUTSIDE BUSINESS HOURS"**.
- Source: TBD-25, TBD-41
- ✅ Approved by user

### D-15: `.env` and `.env.sample` kept in sync; no dotenv library in the application

- `.env` holds real values (git-ignored); `.env.sample` documents every variable with placeholders (committed). The two are kept in sync.
- The application code doesn't include a library that reads `.env`. In development, `.env` is loaded by the tooling (IDE, `uv`, docker compose). The mechanism is in D-53.
- Source: TBD-24
- ✅ Approved by user

### D-16: Unit tests and evals are mandatory parts of the work

- Unit tests (red-green TDD) and evals for LLM behavior are first-class deliverables. How they are done: D-52 (unit tests), D-51 (evals).
- Source: TBD-18, TBD-19
- ✅ Approved by user

### D-17: Live intake by polling the Gmail history API

- After the startup backfill (D-7), new messages are detected by **polling** `users.history.list` from the stored `historyId` cursor, behind a `MessageSource` port (so push or IMAP could be added later without changing the queue).
- Not chosen: Pub/Sub push (extra GCP service and setup, 7-day watch renewal) and IMAP IDLE (second protocol, broader scope).
- The polling cadence is defined in D-19.
- Source: TBD-3
- ✅ Approved by user

### D-18: Google access via a dedicated Gmail account and an OAuth refresh token

- A **dedicated Gmail account** (with its Google Calendar) is created for the agent.
- A free **Google Cloud project** owned by that account holds the OAuth client; the Gmail API and Google Calendar API are enabled. **No billing account is needed:** standard Gmail API use is free, and charges are only planned for usage above 80,000,000 quota units/day per project (later in 2026, with at least 90 days' notice).
- Credentials come from env vars: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`. The mailbox address isn't configured; it is read from `users.getProfile` at startup. A one-off local helper script runs the consent flow and prints the refresh token.
- The OAuth consent screen is **External** and stays in **"Testing"**, with the dedicated account added as a test user. Publishing to production needs a completed Branding page (home page and privacy policy links on a verified domain), which this POC doesn't have. Accepted consequence: the refresh token expires after 7 days (`invalid_grant`, fatal per D-43); the setup wizard is then run again for a new token. The unverified-app warning is accepted for this single-user app.
- Service accounts aren't used: they can't access a consumer `@gmail.com` mailbox without Workspace domain-wide delegation.
- Scopes: `gmail.modify`, `gmail.send`, `gmail.labels`, `calendar.events`, `calendar.readonly` (free/busy). `gmail.modify` / `gmail.send` are "restricted" scopes, which is why the unverified-app warning appears.
- README setup steps:
  1. Create the account.
  2. Create the project ("No organization", no billing).
  3. Enable both APIs.
  4. Run the setup wizard (`terraform/print_env.sh`, D-67): consent screen (External), scopes, the account as test user, Desktop-app OAuth client, browser consent for the refresh token.
  5. The wizard writes the three `GOOGLE_*` values into `.env` (which must exist, copied from `.env.sample`).
- Known caveat: new accounts may need phone verification and may start with lower sending limits and less trust. Keep early outbound volume low.
- Source: TBD-4
- ✅ Approved by user

### D-19: Polling cadence

- Each poll (`history.list`, all pages) puts **every** new message into the queue (D-30) without waiting for it to be processed. The worker processes them one by one, as soon as it's free.
- `POLL_INTERVAL_SECONDS` (int env var, `.env.sample`: **15**, minimum 5 enforced at startup) is the pause between polls, with small random jitter.
- Rate-limit and server errors (429 / `rateLimitExceeded` / `userRateLimitExceeded` / 5xx) are retried with truncated exponential backoff (tenacity).
- No client-side rate limiter: `WORKER_COUNT` workers (D-30), the `ACTION_DELAY_MS` pause (D-20) and the LLM wait per message keep the rate far below the per-user Gmail quota (6,000 units/min); any 429 is handled by the backoff above.
- Quota-related errors are logged in the audit log.
- Source: TBD-3
- ✅ Approved by user

### D-20: Worker delay between messages

- `ACTION_DELAY_MS` (int env var, milliseconds; `.env.sample`: **1000**): each worker pauses this long after finishing a message, before taking the next one. The sender has no delay.
- Source: TBD-3
- ✅ Approved by user

### D-21: What counts as "new" at startup; our own messages are ignored

- The startup backfill (D-7) reads **INBOX only**: it takes the **last X INBOX messages** (`messages.list` with `labelIds=INBOX`) and intersects their IDs with SQLite. Every message not in SQLite is new.
- **Messages sent by the agent itself are ignored** (never classified or processed), both in the backfill and during polling. How they're detected: D-22.
- Source: TBD-3
- ✅ Approved by user

### D-22: Our own sent messages are recognized by the Gmail ID returned on send

- When the manager is the mailbox itself (D-46), the `SENT` label can't separate the agent's messages from the manager's, so the agent's own messages are recognised by their recorded IDs. The same rule is used in both modes.
- The Gmail message `id` returned by `messages.send` is inserted into SQLite right away (same table as processed messages, D-26). The normal "ID already in SQLite → not new" rule (D-21) then ignores it; no other marker is used.
- **Accepted risk:** if the app crashes between `messages.send` returning and the SQLite insert, that message will be processed as new after the restart. The shared automated-sender guard (never reply to our own address, D-38 / D-39) limits the impact.
- Workers check SQLite again when they take a message, so a sent message picked up by a poll in the few milliseconds before its ID is recorded is still skipped.
- **Gmail behaviour relied on:** a self-addressed message is stored once, with both `SENT` and `INBOX`; `messages.send` returns that single `id` (response `labelIds: ["UNREAD", "SENT", "INBOX"]`), and `history.list` with `labelId=INBOX` reports it as `messageAdded` under the same `id`. A reply the manager writes from the mailbox itself is a new self-addressed message with a new `id`, so it isn't skipped and is routed by D-47. References: [googleapis/google-api-nodejs-client#710](https://github.com/googleapis/google-api-nodejs-client/issues/710), [Gmail `history.list`](https://developers.google.com/gmail/api/v1/reference/users/history/list), [Gmail labels guide](https://developers.google.com/workspace/gmail/api/guides/labels).
- Source: TBD-3
- ✅ Approved by user

### D-23: Manager persona and sender name

- The monitored Gmail account is the **manager persona**: its inbox, its calendar, and every outgoing message are the manager's. Who approves is configured by D-46 (the mailbox itself, or an external `MANAGER_EMAIL`).
- **One sender name for everything:** the env var `AGENT_SENDER_NAME` is the display name on every outgoing message (approval emails, notifications, replies to outside people) and signs replies. The README says to set it to the manager's name when the manager is the mailbox itself, and to the assistant's name (e.g. "Anna's Assistant") when `MANAGER_EMAIL` is external. The sending address is always the Gmail account itself (read from `users.getProfile`, D-18); Gmail doesn't allow another address unless it's a verified alias.
- **Approval authenticity** rests on the signed token (only the app can create it; it validates the entire approved content) and the thread binding (D-34). The display name is never used for authenticity.
- Source: TBD-13
- ✅ Approved by user

### D-24: Classification output = one primary label + an `urgent` flag

- Following the requirements: every email gets **exactly one primary label** from the fixed set (MEETING_REQUEST, TASK, INFO_ONLY, SALES_OUTREACH, MARKETING, SPAM).
- **URGENT** is a modifier label added only when the email is urgent (none otherwise), and **never with SPAM**.
- The LLM returns `primary` (one label), `subtype` (D-63), `deadline` (a date expression per D-64, or none) and `urgency_requested` (boolean). The LLM computes no dates. Code acts on a SPAM label first (D-44): a SPAM email gets only the spam action.
- **`deadline`** is a date by which the sender needs the recipient to act, including an invoice's due date. Not a deadline: a proposed meeting time, an offer or sale expiry, an event date, a date the email was sent or a document was issued.
- **`urgency_requested`** is the LLM's reading of the text: the sender asks the recipient for speed on something the sender needs. True for "ASAP", "as soon as possible", "urgent", "high/top priority", "time-sensitive", "move fast", "immediately", "drop everything"; false for "no rush", "when you have a chance", "at your convenience", "sometime next month". The same list is in the classification prompt and in the field description of the output schema. A seller's own pressure in a pitch or promotion ("only today", "limited time", "ends tonight") and phishing pressure are neither a deadline nor an urgency request.
- **TASK vs. SALES_OUTREACH:** a request for the recipient to do, review, sign, send or decide something is TASK, from anyone, including a vendor or customer; SALES_OUTREACH only when the sender is pitching a product or service (D-41).
- **Code decides `urgent`** (the requirements' "deadline or escalation"): `urgent = urgency_requested or deadline ≤ end of the day two days after the received day`. Days count from the received day: received on Tuesday → a deadline up to the end of Thursday (Thursday included) is urgent; Friday or later is not. The deadline is resolved from the received time with the D-64 code; a date without a time counts as that day; an overdue deadline is urgent (a relative deadline in the past is expressed with negative `days`, e.g. "due yesterday" = −1, D-64); a window deadline ("this week", "by next week") uses the window's end.
- `urgent: true` is materialized as the URGENT label on top of the primary label (never with SPAM).
- **The deadline travels with the classification:** besides primary, subtype and urgent, the pipeline's classification result carries `deadline_phrase` (the sender's words) and `deadline_at` (resolved by the same code as `urgent`), used by the urgent approval email (D-40, "Deadline: before end of day (Mon 28 Sep, 23:59 EEST)", "(overdue)" when past) and written to the audit log.
- Source: TBD-21
- ✅ Approved by user

### D-25: Label rules, actions and Gmail representation

- **Model:** `PrimaryLabel` enum (the six labels); `Classification` Pydantic model with `primary: PrimaryLabel`, `urgent: bool` and `subtype: Subtype` (D-63). The LLM output is an enum-constrained JSON schema, so an unknown label is impossible.
- **SPAM forces `urgent` to false.** Nothing more.
- **Actions (policy engine):**
  - the primary label selects the actions from the required-behavior table (including archiving, which applies only where the table says so);
  - `urgent` adds its actions (the "flag" = a star, see below);
  - all replies / calendar actions for one email go into one approval request.
- **Gmail representation:**
  - user labels are named exactly as in the requirements: `MEETING_REQUEST`, `TASK`, `INFO_ONLY`, `SALES_OUTREACH`, `MARKETING`, `URGENT`; SPAM is Gmail's system `SPAM` label (D-44);
  - the requirements' "flag" for urgent emails is implemented as a Gmail **star** (`STARRED`), which shows in the message list without Gmail's smart features.
- System labels (`Finance`, `AI/Error`, `AI/NeedsReview`) are applied by the policy engine, never chosen by the LLM.
- Source: TBD-21, TBD-46
- ✅ Approved by user

### D-26: One SQLite table of processed messages

- SQLite holds a **single table** of processed messages (including the agent's own sent messages, D-22), with the fields:
  - the unique Gmail message ID;
  - the sender, in `Name <email>` format;
  - the subject;
  - the message date/time.
- No other tables. Extra fields are decided later, only when a concrete need is agreed.
- Source: TBD-29
- ✅ Approved by user

### D-27: Linear processing, no mid-flow state; a crash means reprocessing

- Each message goes through a linear flow: queue → classification (LLM / spam evaluation) → actions → recorded as processed (D-26). No state machine and no per-action state are stored.
- If the app crashes mid-flow, the message isn't in the processed table and is processed again from the start after restart. This risk is accepted.
- Source: TBD-22
- ✅ Approved by user

### D-28: Retention in days, used for purging and for the startup backfill

- `RETENTION_DAYS` (int env var) is the maximum message age kept, in days.
- It is used in two places:
  - rows whose message date is older than `RETENTION_DAYS` are purged from SQLite;
  - during the startup backfill (D-7), any of the last X INBOX messages older than `RETENTION_DAYS` is ignored.
- Source: TBD-29
- ✅ Approved by user

### D-29: Configuration is read once, at startup

- Environment variables are read only at startup, by one function that builds a configuration object. The rest of the application reads configuration only from that object, never from the environment.
- Every required variable is validated when the configuration object is built. If any required variable is missing or invalid, the app stops with an explicit error naming each missing/invalid variable and the reason. There are no defaults at all: every variable must be set (D-65).
- Source: TBD-24, TBD-43
- ✅ Approved by user

### D-30: The queue is an anyio memory object stream

- The processing queue is an `anyio` memory object stream: separate send/receive ends, `async for` over the receive end, closing the send side ends the loop.
- The stream has an **unbounded buffer** (`max_buffer_size=math.inf`); unbounded growth is accepted as a very improbable issue. Producers (startup backfill, polling) use **`send_nowait()`**: the message is put in the buffer immediately and the producer's job ends. It never waits for the worker to read or process it.
- **Worker pool:** `WORKER_COUNT` (int env var; `.env.sample`: **2**) workers receive from the same stream (each from its own clone of the receive end); every message is received by exactly one worker. Each worker processes one message at a time (D-27) and waits `ACTION_DELAY_MS` after each message (D-20).
- **Each worker owns its own Gmail and Calendar client instances**, created at worker start and never shared. The blocking Google client calls run in a thread (`anyio.to_thread.run_sync`), so they don't block the event loop. The poller has its own Gmail client instance too.
- On shutdown the send side is closed; each worker finishes its current message and exits.
- `anyio` is already a dependency of PydanticAI and runs on asyncio (ADR-3).
- Source: TBD-3
- ✅ Approved by user

### D-31: A single poller owns all message intake

- One poller component handles both the startup backlog and live messages:
  - **first iteration:** take the cursor (`users.getProfile` → `historyId`), then list the last X INBOX messages (`messages.list` with `labelIds=INBOX`, filters from D-21 / D-28) and send the new ones into the queue. The IDs of this first list are kept in memory until the next iteration.
  - **every next iteration** (every `POLL_INTERVAL_SECONDS`, D-19): `history.list` from the cursor (`labelId=INBOX`, `historyTypes=messageAdded`, all pages), then send the new IDs into the queue, skipping IDs already in SQLite, IDs from the remembered first list (dropped after this iteration), and IDs repeated within the batch.
- The cursor (`historyId`, Gmail's always-increasing change position) is kept in memory only. A restart always begins with the first iteration.
- If the cursor has expired (HTTP 404), the poller falls back to its first-iteration behavior.
- **X = 0** means live messages only: the first iteration takes the cursor and lists nothing.
- The poller has its own Gmail client instance (D-30).
- IDs queued and not yet finished are kept in an in-memory in-flight set shared by the poller and the workers; the poller never queues them again (this covers the re-backfill after an expired cursor and the D-22 send-then-record window).
- **Spam is never read.** Both the backfill and polling read INBOX only (`labelIds=INBOX` / `labelId=INBOX`), so mail in the Spam folder never reaches the queue. The app only marks emails as spam (D-9, D-44).
- **Known, intentional limitation: Gmail's own spam filter.** Anything Gmail itself files as spam is never processed: no label, no reply, no approval handling. This includes a phishing test email and an approver's reply to an approval email if Gmail misfiles them. No "never send to Spam" Gmail filter is set up, and the Spam folder is not checked.
- Source: TBD-3
- ✅ Approved by user

### D-32: Approval email and token contents

- **Email** (from the monitored mailbox to the approver address, D-46; sender name `AGENT_SENDER_NAME`, D-23), plain text and HTML, token in both parts:
  - subject `[Approval] <action>`;
  - a summary of the original email;
  - exactly what will be executed (event details or the full reply text and its recipient);
  - the options (D-13) or the busy / outside-business-hours warning with alternatives (D-14), and the assumed-timezone note (D-1) when applicable;
  - **the approval deadline:** the last moment an approval is accepted, computed with the same logic as the token's `exp`;
  - how to answer (D-34);
  - the token block (D-8).
- The exact wording is left to the implementation and can be rephrased later.
- **Token claims:**
  - `jti` (unique ID); `iat`;
  - `exp` = the earlier of `APPROVAL_EXPIRY_BUSINESS_DAYS` (D-10) and the start of the **latest** proposed slot. The business-day limit always applies, and no approval is accepted once all slots have passed;
  - `sub` (the original email's Gmail ID);
  - `act`:
    - reply: recipient, original thread ID and `Message-ID`, exact reply text, `urgent`;
    - calendar event: title, options (start/end with timezone), attendees, location, the fixed "not available" reply text (D-13);
    - invitation: iCalUID, start, end, organizer, title, location (D-48);
  - `h` (hash of the text shown in the email).
- Source: TBD-5
- ✅ Approved by user

### D-33: Token signing — HS256 with PyJWT

- Tokens are signed and verified with **HS256** via **PyJWT**, using a single secret `APPROVAL_SIGNING_KEY` (env var). Verification accepts HS256 only.
- Every component of the app (including the MCP server, D-59) uses the same key; no asymmetric keys.
- Source: TBD-5
- ✅ Approved by user

### D-34: Verifying an approval reply and handling the outcome

- **Token extraction:** from the quoted text of the reply (quote prefixes, whitespace and line breaks stripped); if the quote is missing, from the latest approval email in the same thread.
- **Checks:** valid signature; `jti` equal to the token of the **latest** approval email in that thread; not expired.
- **Superseded token:** a reply whose token matches an older approval email in the thread (replaced by a re-issue, D-11 / D-12) is not executed; the agent replies that the request was superseded and the latest approval email must be answered.
- **Decision:** the first non-quoted line, matched (case-insensitive, trailing punctuation ignored) against a fixed list. No LLM.
  - approve: `APPROVE`, `APPROVED`, `YES`, `OK`, `CONFIRM`, `CONFIRMED`, optionally followed by an option number;
  - reject: `REJECT`, `REJECTED`, `NO`, `DECLINE`.
- **Outcomes:**
  - approve → execute exactly the signed action;
  - reject → a received invitation is declined (D-48); a meeting request from an email's text gets the fixed "not available" reply (D-13); otherwise log only;
  - invalid → D-9;
  - expired → D-11;
  - unclear word or out-of-range option number → log, and reply with the accepted formats.
- The manager's replies are never marked as spam, except invalid ones (D-9).
- Source: TBD-5, TBD-37
- ✅ Approved by user

### D-35: Repeated approvals are not handled

- If the manager approves the same request more than once, each approval is processed. This is a known issue that is deliberately not handled, for every action type (replies may be sent twice; a calendar approval finds its slot taken and goes through D-12).
- Source: TBD-5
- ✅ Approved by user

### D-36: LLM provider and model — one global choice, Haiku-tier models

- Supported providers: **Google Gemini**, **Anthropic Claude** and **Azure OpenAI**, selected by the env var `LLM_PROVIDER`.
- For Gemini and Anthropic, the model is selected by the env var `LLM_MODEL`. For Azure OpenAI, the model is the one behind the deployment named by `AZURE_OPENAI_DEPLOYMENT`; `LLM_MODEL` isn't used. Models are the **Haiku-equivalent tier** of each provider (small, fast, inexpensive); no expensive models.
- Provider and model are **global for the whole app**: set once at startup (D-29) and used for every LLM task (classification, meeting extraction, reply drafting). No per-task models, no failover to another provider.
- Temperature 0 for classification and extraction; low (e.g. 0.3) for drafting replies. **Gemini and Anthropic only:** for Azure OpenAI no temperature is sent (the model's default is used), because some Azure model families reject any non-default temperature; with `LLM_PROVIDER=azure`, the app logs at startup that temperature isn't used.
- Each LLM call has a timeout (e.g. 30 s) with retries and backoff for rate-limit/server errors, and a token cap (`UsageLimits`).
- Email content is sent to the selected provider; this is known and accepted.
- **Multi-platform support means exactly this:** the app can run with API keys from any of the three providers. Nothing beyond that (no hosted agent services, no agent-to-agent protocols, no provider comparison).
- Source: TBD-2, TBD-40
- ✅ Approved by user

### D-37: `.env.sample` documents every variable

- Every variable in `.env.sample` has a comment above it explaining what it does and, for multi-option variables (e.g. `LLM_PROVIDER`, `LLM_MODEL`), listing all available options.
- `.env.sample` holds a recommended value for every variable (the values noted as "`.env.sample`: …" in this plan); the app never assumes them.
- Source: TBD-24, TBD-43
- ✅ Approved by user

### D-38: Sales outreach is declined automatically; approval is configurable

- **The requirements are unclear here:** the scenario table says *"Sales cold outreach · send polite decline + archive · No — auto (configurable)"*, while the general rule says *"Any calendar action or reply sent on behalf of the manager requires email confirmation first."* We follow the **table**, which overrides the general rule for this case: the decline is sent without approval unless configured otherwise.
- **Configurable:** `SALES_DECLINE_REQUIRES_APPROVAL` (`.env.sample`: `false`). When `false`, the decline is sent automatically. When `true`, the drafted decline goes into an approval email (D-32), like courtesy replies (D-39): APPROVE sends it, REJECT sends nothing. The email is archived immediately in both modes, whatever the manager answers. Drafting, guardrails, recipient and format are the same in both modes.
- The decline is **drafted by the LLM** at low temperature (D-36), guided by an instruction describing how the reply must look:
  - thank the sender warmly for reaching out and for thinking of us;
  - politely say we're not pursuing this at the moment;
  - stay positive and open in tone, without implying we'll follow up or reply again, and without discouraging future contact;
  - short (a few sentences), no promises, no questions.
- **The LLM sees only this one email** (subject, sender name, body): no other emails, no calendar, no configuration, no internal data. It returns only the reply body.
- **Recipient chosen by code, never by the LLM:** the reply goes to the original sender, taken from the email's headers (same guardrail as ADR-4).
- **Format:** a standard threaded reply (`In-Reply-To` / `References`, same thread), with the LLM body followed by the original message quoted below, the way Gmail quotes by default ("On <date>, <sender> wrote:" + quoted text). The quoted part is built by code.
- **Language:** the decline is written in the language of the sales email, keeping every part of it, including "not pursuing this *at the moment*" (never a permanent no such as "this isn't right for us").
- No additional content checks on the LLM text; the risk is accepted, since the LLM has no data beyond the incoming email.
- Flow (no approval): label `SALES_OUTREACH` → send the decline as a reply in the sender's thread (sent ID recorded, D-22) → archive.
- Flow (approval required): label `SALES_OUTREACH` → approval email with the drafted decline → archive; on APPROVE the decline is sent as a reply in the sender's thread (sent ID recorded, D-22).
- **No decline to automated senders** (detected from headers by code, the shared automated-sender guard also used by D-39): `no-reply` / `noreply` / `mailer-daemon` addresses, messages with `Auto-Submitted` (auto-responders, bounces), bulk messages (`Precedence: bulk` or `List-Unsubscribe`), and our own address. These are still labelled and archived; the skipped decline and its reason are logged.
- Source: TBD-10, TBD-36
- ✅ Approved by user

### D-39: Courtesy replies (acknowledgement / thank-you), always approval-gated

- **Requirement:** *"Some emails should receive an automatic reply — for example, a polite acknowledgement to a marketing offer or a brief thank-you where appropriate. The agent decides when this is warranted."* "Automatic" means agent-initiated (the requirements use it the same way for table rows that need approval), so the general rule *"Any … reply sent on behalf of the manager requires email confirmation first"* applies. Only the sales decline is exempt (D-38).
- **Approval:** every courtesy reply goes through the approval email (D-32 to D-34). No setting.
- **Eligible emails:** those whose single primary label is **MARKETING**, **INFO_ONLY** or **TASK**. Never SPAM, SALES_OUTREACH (it gets the decline) or MEETING_REQUEST (the invitation is its reply).
- **Order:**
  1. the email's normal actions run first (e.g. a MARKETING email is archived immediately, without waiting for any approval);
  2. the shared automated-sender guard (D-38) runs, and blocked emails get no courtesy reply;
  3. one courtesy reply per thread at most: the thread is read from Gmail (`threads.get`), and if the agent has already sent a message in it, no new reply is proposed;
  4. **one separate LLM call** (not part of classification, D-24 unchanged) returns `warranted`, `reason` and `body`;
  5. if warranted, an approval email carrying the drafted reply is sent; on APPROVE, the reply goes out.
- **The LLM call:** default to not warranted; warranted when a human sender would reasonably appreciate a short acknowledgement or thank-you: they **delivered or shared** something (a report, files, notes, an update, congratulations, a personal offer), even if they also ask for feedback. Not warranted: notifications, receipts, invoices, newsletters, messages expecting nothing, and emails whose **only** content is a question or a request aimed at us ("what's the status of …?", "please send me … by Friday"), where a bare thank-you would read as ignoring it.
- **The body is a thank-you only:** thanks for sending or sharing it, never for the work or effort on it; it never says or implies that the manager read, reviewed, liked or approved anything, never comments on or evaluates what was shared ("great achievement", "excellent work", "That's great to hear"), and never promises anything (e.g. "Thanks for sending the report over.", "Thanks for sharing the news with me.", never "Looks good!", "That's a great achievement for the team!" or "I'll review it and get back to you"). Business-appropriate: plain, measured thanks ("Thank you for …"), no gushing ("Thanks so much!"), never thanking them for their kind words, no exclamation marks; a plain thank-you gets exactly "You're welcome.". Same tone constraints and guardrails as D-38 (no promises, no questions, no implied follow-up; the LLM sees only this email; recipient chosen by code; threaded reply with the original quoted, built by code; signed with `AGENT_SENDER_NAME`). The `reason` is written to the audit log.
- **README:** states that courtesy replies are sent only after the manager approves them.
- Source: TBD-16
- ✅ Approved by user

### D-40: Urgent message from internal sender

- **Requirement:** *"Urgent message from internal sender · Label URGENT + TASK, flag · Gmail: flag + notify manager · Yes — required."* The row doesn't say what the approval is for; it is read as approving an acknowledgement reply to the sender.
- **Flow** for an email classified TASK + urgent from an internal sender (D-42):
  1. automatically: labels `TASK` and `URGENT`, plus a Gmail star (the "flag", D-25);
  2. a **dedicated acknowledgement drafter** (not the D-39 courtesy decision): `draft_acknowledgement(email, hint) -> str`, plain-text output, its own prompt `urgent_ack.md`: the message was received and has been flagged as urgent, naming nobody (`AGENT_SENDER_NAME` is the signature, the manager's own name or an assistant's, so "brought to <name>'s attention" would be wrong in one of the two modes); no commitment on the task, no question, no time promise. The flow has already decided the reply is required, so the model never decides "warranted";
  3. the approval email, sent to the approver address (D-46), doubles as the manager notification (one email) and reads **notification first**: subject `[Approval] URGENT from <sender>: <subject> — reply APPROVE to send the acknowledgement`; first lines: sender, subject, what is asked, the deadline if any, "flagged URGENT + starred"; the proposed acknowledgement below;
  4. APPROVE → the acknowledgement is sent to the sender in the original thread; REJECT → nothing more.
- **Plain notification fallback, never silence:** a plain notification email goes to the approver address (no token, no action) when the shared automated-sender guard blocks the reply (e.g. an internal no-reply system), or when the drafter fails or returns an empty text (then also `AI/NeedsReview`).
- **No acknowledgement** when the sender is the approver address itself, or the agent already replied in this thread (one per thread, as D-39); the notification is still sent.
- "Notify manager" is logged as its own action on every branch (approval sent, plain notification, guard-blocked, draft failed), so the row is visibly satisfied even when the reply is rejected or blocked.
- An urgent email from a non-internal sender follows the normal D-39 path (the LLM decides).
- Source: TBD-11
- ✅ Approved by user

### D-41: MARKETING vs. SALES_OUTREACH rule in the classification instructions

- The classification instructions define the boundary: **MARKETING** = broadcast, impersonal promotion sent to many (newsletters, promos, bulk mail); **SALES_OUTREACH** = unsolicited vendor/sales contact addressed to the recipient personally and expecting a reply. Headers (e.g. `List-Unsubscribe`) are supporting evidence, not the deciding factor.
- Source: TBD-16
- ✅ Approved by user

### D-42: Internal sender = domain list + passed authentication

- A sender is **internal** if its address's domain is listed in `INTERNAL_DOMAINS` (env var, comma-separated domains, e.g. `acme.com`).
- If `INTERNAL_DOMAINS` is empty, nobody is internal: urgent emails follow the normal courtesy-reply path (D-39). The README explains that the variable must be set to exercise the "urgent message from internal sender" scenario (D-40), and the app logs a visible warning at startup when it is empty ("INTERNAL_DOMAINS is empty: the urgent-internal scenario cannot trigger").
- No special rule for public email domains: the list is user-controlled; if someone lists `gmail.com`, every Gmail sender is internal.
- **Anti-spoofing:** a domain match counts only if Gmail's `Authentication-Results` header shows DKIM/DMARC passing for that domain. If the domain matches but authentication fails, the sender is treated as external and the reason (which check failed) is logged explicitly.
- Source: TBD-12
- ✅ Approved by user

### D-43: Error handling

- **Temporary errors** (HTTP 429, 5xx, timeouts from Gmail, Calendar or the LLM): each call is attempted up to 3 times in total (2 retries) with exponential backoff capped at 16 s.
- **Invalid LLM output:** PydanticAI retries with the validation error (2 retries). If it still fails: no actions, label `AI/NeedsReview`, logged, the email is recorded as processed.
- **An email that still fails:** re-queued in memory up to `MAX_ATTEMPTS` (env var; `.env.sample`: 3), with a delay between attempts. After the last attempt: label `AI/Error`, logged, recorded as processed. No extra storage.
- **Errors where retrying can't help** (e.g. the email was deleted, a malformed email): no retry; logged with the reason; recorded as processed; `AI/Error` added if the email still exists.
- **Why two labels:** they call for different people and different follow-ups.

  | | `AI/NeedsReview` | `AI/Error` |
  |---|---|---|
  | Cause | The model's output was unusable (classification, meeting extraction or a draft) | Processing failed: a Gmail/Calendar/LLM API error that survived its retries, or an unexpected exception |
  | Who acts | The manager | Whoever runs the agent |
  | What they do | Read the email and handle it by hand | Check the logs, fix the cause, have the agent process the emails again if needed |
  | Typical pattern | Occasional, one odd email | Often many at once (an outage, an expired token) |
  | Would retrying help? | No: the same content gives the same result | Often yes, once the cause is fixed |
- **Authentication failures** (e.g. `invalid_grant` for the Google refresh token, 401 from the LLM API): the app stops with an explicit error, like startup validation (D-29). It must not end up restarting in an endless loop (mechanism: D-50).
- **Unexpected exceptions in our own code** while processing an email: caught per email, logged with the stack trace, handled like an email that still fails; the worker continues with the next email.
- **Poller failures** (after retries): logged; retried at the next interval; the poller never stops on its own.
- **Failure log fields** (one structured entry): message ID, stage (classify / action / approval / poll), error type and message, attempt number, outcome (retrying / gave up / skipped). Logging is done through the app's unified logger (D-58).
- Source: TBD-17
- ✅ Approved by user

### D-44: Phishing, bulk and invoice detection — code extracts facts, the LLM decides

- **Spam first (mandatory):** every incoming email is classified first. If the classification is SPAM, the email goes directly to the Gmail action (log + move to Spam) and processing stops: no invitation detection, labels, urgency, courtesy, sales or meeting logic. Only non-spam mail continues.
- **Who decides:** the classifier (D-24), from the headers and the content together (`classify.md`, "Deciding SPAM"): advance-fee, credential phishing, payment/gift-card/CEO fraud, extortion, malware, prompt injection and junk are SPAM whatever the authentication result; a service's own notice (account security, sign-in, app access, password reset, receipt), authenticated for its own domain with links to that service's domains, is not SPAM. Code never forces and never vetoes SPAM.
- **Shape:** deterministic code extracts facts from the email (headers, sender, links, attachments, Gmail's own labels) and passes them to the classification call in a separate trusted section, next to the email content in an untrusted section.
- **Facts extracted** (links analysed as text only, never fetched; attachments never opened). Facts state what was observed, with no interpretation:
  - authentication: SPF/DKIM/DMARC from Gmail's own `Authentication-Results` header (shared with D-42), always one line: pass for the From domain ("the From domain is not forged"), the raw results on failure, or "no Authentication-Results header";
  - sender: `Reply-To` vs `From` domain, display name impersonating a brand or containing another address; the From domain is a public free-mail provider;
  - bulk and automated: bulk/list headers, no-reply sender, bulk-mail content cues (unsubscribe footer, "view in browser"), no sender list;
  - links: the sender domain next to every distinct link host; link text that looks like a domain but points elsewhere (a subdomain of the named domain is not a mismatch), IP-address / shortener / punycode hosts, credentials in the URL;
  - attachments: risky types (HTML, archives, executables, macro documents); a calendar invitation part (`.ics`);
  - Gmail's labels on the message (e.g. Promotions);
  - the allowlist match (D-61), as a hint.
- **Body text for the LLM:** a `text/plain` part that contains markup (conditional comments, VML, HTML tags) is replaced by the `text/html` part through `html_to_text`; with no HTML part, the markup is stripped.
- **SPAM action:** Gmail doesn't allow a user label named `SPAM` (reserved system name). Adding Gmail's system `SPAM` label both labels the email SPAM and moves it to the Spam folder (`INBOX` removed). Logged first, then applied.
- **Not classified:** the agent's own messages and replies in approval threads (D-9 has its own spam rule; D-34) never reach the classifier; every other incoming email does.
- **Logging:** the extracted facts that fired are logged with the decision (the requirement's "+ log" for SPAM).
- Source: TBD-14
- ✅ Approved by user

### D-45: Calendar scope for the POC

- **Reschedule / cancel requests** for an existing meeting (e.g. "can we move our 3pm to 4pm?"): not required, so not handled as a meeting move or cancellation. Such emails go through the regular flow like any other email.
- **Follow-ups in a thread** (e.g. "Wednesday works"): the agent only sends calendar invitations and never proposes times to other people, so a follow-up is treated as a new meeting request with the existing logic (ADR-4, D-13, D-14).
- **No free slot inside the requested interval** (an open window like "sometime next week"): handled like D-14. The approval email says no interval is available in the requested window and proposes alternatives **outside the requested interval** (the nearest free slots after it, within the scheduling horizon), marked very clearly as outside the requested interval. Same approval logic (numbered options, D-13).
- **Every approval variation explains the problem in clear text:** requested time busy, outside business hours, no free slot in the requested window, approval expired, slot taken after approval.
- **Duration:** the duration stated in the email is followed. If none is stated, `DEFAULT_MEETING_MINUTES` (env var, ADR-4) applies.
- **All-day / full-day meetings** are handled as regular meetings: the event spans the business hours of that day (`BUSINESS_HOURS_START` to `BUSINESS_HOURS_END`, ADR-4), and availability is checked for that interval.
- **Recurring meetings:** not handled.
- **Calendar invitations received by email:** D-48.
- Source: TBD-25
- ✅ Approved by user

### D-46: The approver address is configurable

- `MANAGER_EMAIL` (env var; must be present, may be empty) is the approver address. If it's empty or equal to the mailbox address (D-18), approval and notification emails go to the monitored mailbox itself and the manager replies from it. Otherwise they go to `MANAGER_EMAIL` and the manager replies from that address.
- The mode and the address are logged at startup. When set, `MANAGER_EMAIL` is validated as an email address (D-29).
- The README describes both setups: A, reply to `[Approval]` emails inside the assistant mailbox; B, set `MANAGER_EMAIL` to your address and reply from there (whitelisting the assistant's address so approvals don't land in spam).
- Source: TBD-13
- ✅ Approved by user

### D-47: One set of routing rules for both modes

- An **approval email** is a message in the thread that contains our token block (D-32) whose JWT signature verifies with `APPROVAL_SIGNING_KEY`. Other messages we sent (declines, courtesy replies) don't count.
- A message is an **approval reply** if it isn't one of our own sent messages (D-22) and its Gmail thread contains one of our approval emails. It's never classified; it goes to approval handling (D-34).
- Other messages carrying `SENT` (written from the mailbox) are ignored.
- All other incoming messages, including the manager's own emails from an external address, are classified normally.
- Approval emails, expiry and slot notices (D-11, D-12) and urgent-internal notifications (D-40) are always sent to the approver address (D-46).
- Events the agent creates add the approver address as an attendee, de-duplicated with the organizer (ADR-4).
- Source: TBD-13, TBD-37
- ✅ Approved by user

### D-48: Calendar invitations received by email

- A calendar invitation received by email (`.ics`) is handled like a meeting request in both modes:
  1. availability is checked on the mailbox's calendar; the invitation's own event, which Google has already placed there awaiting a response, is not a conflict (matched by its UID); any other overlapping event, including other unanswered invitations, still is;
  2. an approval email goes to the approver address, with a warning if the time is busy or outside business hours (no alternatives, since it's someone else's invitation);
  3. manager replies **APPROVE** → the application sets the invitation's response to **accepted** in Google Calendar; Google notifies the organizer;
  4. manager replies **REJECT** → the application sets the invitation's response to **declined** in Google Calendar; the event is removed from the mailbox's schedule and Google notifies the organizer;
  5. both outcomes are logged.
- The invitation's identifiers (event / iCalUID, organizer, time) travel inside the signed token (D-32); no state is stored.
- **Spam first:** an invitation is classified like every email (D-44); a SPAM invitation goes to Spam and nothing below runs.
- **Detection** (only for non-spam mail): by code: a `text/calendar` part or an `.ics` attachment with `METHOD:REQUEST` → it is an invitation (label MEETING_REQUEST, replacing the classifier's label; times come from the invitation, not the LLM). If code finds none, the classifier's label stands, and the LLM can still recognise it as an invitation. `METHOD:CANCEL` → no calendar action (D-55); `METHOD:REPLY` → logged only.
- **Reading:** the `icalendar` library reads UID, start, end, organizer, title and location.
- **Answering:** the event is looked up with `events.list(iCalUID=…)`. If it isn't found, it is created with `events.import` (from the invitation's UID, start, end, title, location, organizer). The response is then set by patching our own attendee's `responseStatus` (`accepted` / `declined`) with `events.patch`, `attendeesOmitted=true` and `sendUpdates=all`; Google sends the confirmation to the organizer. No separate iCalendar reply email is sent.
- Source: TBD-25, F-7
- ✅ Approved by user

### D-49: No reply loops in approval threads

- In approval threads, replies marked as automatic (`Auto-Submitted`, e.g. out-of-office) are ignored and logged.
- The "please answer APPROVE / REJECT" response to an unclear reply (D-34) is sent at most once per approval thread; whether it was already sent is read from the Gmail thread itself (no stored state).
- Applies in both modes.
- Source: TBD-13
- ✅ Approved by user

### D-50: Tooling and packaging

- `uv` for dependency management and the lockfile; `pytest` (+ `pytest-asyncio`), `ruff`, `mypy`.
- `src/` layout package.
- Multi-stage Dockerfile on `python:3.14-slim` (ADR-1), running as root (no hardening, D-56); `docker-compose.yml` with an env file and a volume for the SQLite database.
- **No endless restart loop (D-43):** the app exits with a non-zero code on fatal errors (authentication failures, invalid configuration), and the container restart policy is limited (`restart: on-failure:3` in `docker-compose.yml`), so a fatal error stops the container after a few attempts.
- Source: TBD-8
- ✅ Approved by user

### D-51: Evals for LLM behaviour

- **What is evaluated:** classification (primary label, `urgent`, `subtype`), meeting extraction (date expressions per phrase type, D-64; duration, timezone stated/assumed, extra attendees without invented addresses), the sales decline draft (D-38), courtesy replies (D-39: warranted yes/no, tone), the urgent acknowledgement (D-40) and spam decisions (D-44).
- **Dataset:** hand-written synthetic emails, versioned in the repo, one YAML file per eval kind (`classify`, `extract`, `sales`, `courtesy`, `ack`, `spam`, plus the `judge` calibration cases), each case with its expected result. It covers every label, urgent vs. not, the boundaries (MARKETING vs. SALES_OUTREACH, real vs. fake invoice, meeting request vs. TASK), timezones, open windows and prompt-injection attempts.
- **Tool:** `pydantic-evals` (local, no SaaS).
- **Every case must pass; any mismatch fails the eval run.** Each case runs once.
- **Runs:** every case runs once. `dt-agent eval --repeat N` exists as a parameter but is not used. A case that fails is fixed at its cause (prompt or case), never loosened.
- **Dataset scope:** boundary cases that the plan's rules decide (e.g. MARKETING vs. SALES_OUTREACH, D-41) are included and must pass; very ambiguous emails are left out for now.
- **Classification and extraction:** exact match (label, `urgent`, `subtype`, date expressions, duration, timezone, extra attendees).
- **Drafted replies:** only the LLM-written body is checked (the signature and the quoted original are added by code). Every check is must-pass:
  - sales decline (D-38): thanks the sender; says politely we're not pursuing it at the moment; positive and open, doesn't discourage future contact; no implied follow-up, no promises (LLM judge, yes/no); no question and short, e.g. ≤ 5 sentences (code);
  - courtesy replies (D-39): "warranted" equals the expected value (code); if warranted, a brief acknowledgement / thank-you with the same tone rules (LLM judge, yes/no); no promises, no questions, short (judge / code).
- **Judge model:** the same provider and Haiku-tier model as the app (D-36).
- **Running:** on demand through a CLI command (e.g. `dt-agent eval`), before prompt or model changes; never together with the unit tests.
- **Live end-to-end check:** an optional operator tool, a script that sends ~10 test emails to the dedicated inbox, lets the agent run, and checks labels and log lines. It is documented in the README and never used during the build; the build is verified offline (D-52).
- **README:** the project-root `README.md` explains how to run the evals and the live check (commands, required env vars, cost note).
- Source: TBD-19
- ✅ Approved by user

### D-52: Unit testing strategy

- Unit tests run **offline, free and deterministic**: no real Gmail, Calendar or LLM calls.
- **External services:** our own ports (`MailPort`, `CalendarPort`, `Classifier`, `MeetingExtractor`, `ReplyDrafter`, …) get in-memory fakes; pipeline tests use the fakes.
- **LLM:** PydanticAI `TestModel` / `FunctionModel`; real model requests are blocked globally in tests (`ALLOW_MODEL_REQUESTS=False`).
- **Gmail / Calendar adapters:** tested with recorded API responses using `google-api-python-client`'s built-in `HttpMock` / `HttpMockSequence` (correct endpoint, parameters and response parsing).
- **Time:** an injectable `Clock`; tests pass a fixed time (business days, expiry, rounding, lead time, DST changes).
- **Layers:** pure logic (scheduling, rounding, approval token, decision words, fact extraction, config validation, `.env.sample` sync); adapters (recorded responses); whole-pipeline tests with fakes (e.g. phishing → SPAM; meeting request → approval email → APPROVE → event created).
- **Coverage:** measured and reported with `pytest-cov`; no threshold.
- **Running:** `uv run pytest` runs the whole suite offline; documented in the project-root `README.md` next to the eval commands (D-51).
- **Integration test:** `tests/integration/test_mcp_live.py` (marker `integration`) starts the real app locally from `.env` and calls every MCP tool over HTTP; excluded from `uv run pytest`, run with `uv run pytest -m integration tests/integration`.
- Source: TBD-18
- ✅ Approved by user

### D-53: Loading configuration

- The app reads configuration with `pydantic-settings` from the **process environment only**; no `.env` file is configured in the app (D-15, D-29).
- `.env` is loaded by the tooling:
  - IntelliJ / PyCharm: "Paths to .env files" set once in the run-configuration templates for Python and pytest;
  - terminal: `uv run --env-file .env <command>`;
  - Docker: `env_file: .env` in `docker-compose.yml`.
- Unit tests never read `.env`; they build the configuration object explicitly (D-52).
- No automated check keeps `.env` and `.env.sample` in sync.
- A `.gitignore` file lists `.env`; `.env.sample` holds no real secrets. The implementation creates these files only and runs no git commands.
- The list of variables is maintained in the Architecture section ("Configuration variables") and kept up to date as decisions change.
- Source: TBD-24
- ✅ Approved by user

### D-54: Invalid database — detection and reporting

- **Invalid** means any of: not an SQLite file or can't be opened; `PRAGMA integrity_check` fails; the table or one of its columns is missing; the schema version isn't the one the app expects.
- A database from an older app version (different schema version) is treated as invalid: no migrations.
- The schema version is stored in SQLite's `PRAGMA user_version` (no extra table, D-26).
- **Rename format:** `<name>-failed-<UTC timestamp>-UTC.<ext>`, e.g. `agent.db` → `agent-failed-20260926-210512-UTC.db`. The `-wal` / `-shm` files are renamed the same way.
- **Visible error:** the app keeps running with the new database; it writes an error-level entry through the unified logger (D-58) and prints a clear multi-line banner at startup, e.g. "DATABASE INVALID: renamed to … (reason: …). A new database was created."
- Renamed files are all kept; nothing is deleted automatically.
- Consequence: with a new database, the last X INBOX messages are looked at again (D-7, D-27), except those older than `RETENTION_DAYS` (D-28).
- Source: TBD-30
- ✅ Approved by user

### D-55: Remaining calendar cases

- **Updated invitation** (the organizer changes it): the same flow as a new invitation (D-48). **Cancelled invitation:** no calendar action (Google removes it itself); classified and logged.
- **Webinar / event marketing invitations:** MARKETING, no calendar action.
- **Ambiguous dates** (e.g. `05/06`): the most likely reading is used, and the approval email states it clearly (e.g. "interpreted as 5 June — ambiguous in the original") so the manager can reject.
- **A meeting on a third-party link** ("join my Zoom at 3pm"): handled as a meeting request (approval, then a calendar event holding the link in its location/description); the link is never opened.
- **Location:** a physical address or meeting link stated in the email is copied into the event; no Google Meet link is added automatically.
- **Urgent meetings** (e.g. "today in 1 hour"): the approval email highlights the urgency at the top (e.g. "Starts in 55 minutes"); the token already expires at the slot start (D-32).
- **Deadlines in TASK emails:** no calendar action.
- **Meeting requests classified as SPAM:** never any calendar action.
- **Conflicting events in the approval email:** shown with time and title (e.g. "conflicts with: 14:00–15:00 Board meeting").
- Source: TBD-25
- ✅ Approved by user

### D-56: Security baseline for the POC

- **Already covered elsewhere:** prompt injection (ADR-2, D-24, D-25, D-44, tested in D-51); recipients never chosen by the LLM (ADR-4, D-38); forged approvals (D-9, D-32 to D-35); spoofed internal senders (D-42); links never opened (D-44); reply loops (D-38, D-39, D-49); no inbound ports except the optional MCP server, published to localhost only (D-4, D-59); minimal Google scopes (D-18); email content sent to the LLM provider (D-36).
- **Approval emails leaving the mailbox:** with an external `MANAGER_EMAIL` (D-46), approval emails carry summaries of third-party emails and drafted replies to that address; they go to the manager, so this is accepted. A mistyped `MANAGER_EMAIL` would send them to a stranger; the address is validated and logged at startup.
- **HTML emails** are converted to plain text before the LLM sees them; scripts, styles and remote images are dropped.
- **Very long emails:** the text sent to the LLM is truncated to `LLM_MAX_INPUT_CHARS` (env var; `.env.sample`: 20000), and the truncation is logged.
- **Floods of emails:** no extra protection; the worker pool (D-30), the per-call token cap (D-36) and Gmail's own limits are enough for the POC.
- **Calendar limits:** at most 20 attendees and at most one business day per event (`core/scheduling.py`); a request above either limit is flagged in the approval email, not silently created.
- **Secrets:** never passed to the logger at all; as a safety net, the unified logger also masks API keys, OAuth tokens and approval JWTs (D-58).
- **Container:** no hardening; it runs as root. Accepted risk for the POC.
- **Sender and subject in logs:** allowed. This is a GDPR risk (personal data in logs), accepted for the POC only; a production-ready app should not log them (D-58).
- **Supply chain:** the dependency lockfile (`uv.lock`, D-50); the Docker base image pinned by digest (`python:3.14-slim@sha256:…`, updated by hand); a manual `uv run pip-audit` check documented in the README.
- Source: TBD-23
- ✅ Approved by user

### D-57: No terminal UI; the app's output is its log on stdout

- There is no terminal UI.
- The app writes its logs to **stdout** as **structured JSON, one object per line** (not pretty-printed), **coloured in the terminal like `jq`** (syntax colouring of keys and values, without reformatting).
- The remaining logger details are in D-58.
- Source: TBD-28
- ✅ Approved by user

### D-58: Unified application logger

- **One logger for the whole app**, built on structlog (ADR-3), with its own implementation task. All logging, including the logs of third-party libraries (Google client, HTTP, PydanticAI), goes through it, so its configuration (level, JSON format, colour) applies to every log line. Accepted: third-party log lines may lack some of our fields.
- **Colour:** only when stdout is a terminal; plain JSON otherwise (e.g. redirected output). `LOG_COLOR` (`auto` | `always` | `never`; `.env.sample`: `auto`).
- **Level:** `MIN_LOG_LEVEL` (`DEBUG` | `DEV` | `INFO` | `WARNING` | `ERROR`; `.env.sample`: `INFO`). `DEV` sits between `DEBUG` and `INFO` and holds the LLM request lines (the per-call usage line and the LLM SDKs' HTTP request lines), so they are hidden at `INFO`. Noisy libraries are set to `WARNING`. PydanticAI's startup banner is disabled; the output is the structured log only.
- **Audit entry:** besides step-by-step entries, every processed email gets exactly one summary line when it finishes, `"event": "email.processed"`, with the requirement's audit fields `label` (the primary label; `null` when there is none: approval replies, ignored sent mail, errors), `action` (the actions taken) and `outcome`, plus message ID, sender, subject, `urgent`, and the reason when relevant. This includes emails that failed for good (`action: ["label_ai_error"]`, `outcome: "error"`) and emails that could no longer be processed (`outcome: "skipped"`).
- **Fields on every line:** `timestamp` (UTC, ISO 8601) always as the first key, followed by `label`, `action`, `outcome` when the line has them, then the rest; level, event name; where applicable `message_id`, `worker_id`, `stage`, `attempt`, `duration_ms`. `message_id` and `worker_id` are attached automatically to every line logged while a worker handles an email.
- **LLM usage:** every LLM call logs provider, model, input/output tokens and duration, at the `DEV` level.
- **Personal data:** email bodies are never logged. Sender and subject are logged; this is accepted for the POC only. A production-ready app should not log them, because they introduce GDPR-related risks.
- **Secrets:** never passed to the logger; masking of API keys, OAuth tokens and approval JWTs as a safety net (D-56).
- Source: TBD-32
- ✅ Approved by user

### D-59: MCP server for testing the agent's decisions

- Part of the POC. An MCP server exposes the agent's decisions as tools, so each can be tested directly from an MCP client (e.g. Claude Code, Claude Desktop, MCP Inspector).
- **Started by the app itself:** when `ENABLE_MCP=true`, `dt-agent run` also serves MCP over the SDK's streamable HTTP transport on `MCP_PORT`, in the same process. With `ENABLE_MCP=false`, no MCP server and no port. There is no separate `mcp` CLI command.
- **Reachable from localhost only:** the listen address is `MCP_HOST` (required when `ENABLE_MCP=true`, D-65): `127.0.0.1` for a local run (`.env`, `.env.sample`); inside the container `docker-compose.yml` sets `MCP_HOST=0.0.0.0` (required for Docker port publishing) and publishes the port as `127.0.0.1:${MCP_PORT}:${MCP_PORT}`, so it is visible outside the container but only on the host's `127.0.0.1`. No authentication (POC).
- stdio isn't used: stdout carries the app's log (D-57). MCP tool calls are logged through the same unified logger, so they appear on stdout together with the app's own log lines (D-58).
- **All tools are dry-run:** nothing is sent, labelled, archived, moved to spam or added to the calendar, and nothing is written to SQLite. Calls are logged through the unified logger (D-58); LLM-backed tools cost tokens like a normal run.
- **Tools** (thin wrappers around the same ports the workers use):
  1. `classify_email` (subject, body, sender, optional headers) → `primary`, `urgent`, `subtype`, and the facts extracted by code (D-24, D-25, D-41, D-44, D-63);
  2. `extract_meeting` (subject, body, sender, to/cc, received time) → date expressions and the times code computed from them (D-64), duration, timezone stated/assumed, window, extra attendees after the address guardrail (ADR-4);
  3. `draft_sales_decline` (subject, body, sender) → the decline body (D-38);
  4. `draft_courtesy_reply` (subject, body, sender, label) → `warranted`, `reason`, body (D-39);
  5. `extract_facts` (subject, body, sender, headers) → the code-extracted facts only (D-44), no LLM;
  6. `find_meeting_slots` (duration, window) → up to `MEETING_MAX_OPTIONS` free options inside the window, against the real calendar, read-only free/busy, no LLM; the window is a search range, not a requested time; a window with no free slot gives the nearest free slots after it (ADR-4, D-45).
- **Library:** the official `mcp` Python SDK (2.x, `MCPServer`).
- **Variables:** `ENABLE_MCP` (always required); `MCP_PORT` and `MCP_HOST` (required when `ENABLE_MCP=true`), per D-65.
- Source: TBD-9
- ✅ Approved by user

### D-60: No external spam filter

- Rspamd and SpamAssassin were considered as an extra spam signal; for this POC they are overkill, and neither is used.
- Spam detection is the facts extracted by code (D-44) plus the classifier's decision from the headers and the content (D-24, D-44).
- Source: TBD-26
- ✅ Approved by user

### D-61: Sender allowlist as a trusted-sender hint

- An **allowlist** of trusted senders, configured with comma-separated env vars: `SPAM_ALLOWLIST_SENDERS` (addresses) and `SPAM_ALLOWLIST_DOMAINS` (domains). No blocklist.
- **It's a hint only:** for an allowlisted sender, the classification call receives "this sender is on the allowlist" as one more fact (D-44). The LLM still decides the label; if it returns SPAM, the email is SPAM (labelled and moved to the Spam folder as usual).
- **Matching:** case-insensitive; `SPAM_ALLOWLIST_SENDERS` matches the exact `From` address; `SPAM_ALLOWLIST_DOMAINS` matches the `From` domain and its subdomains (`example.com` also covers `mail.example.com`).
- A match counts **only if Gmail's sender authentication passes** for that sender's domain (DKIM/DMARC, the same check as D-42); otherwise the sender is treated as not allowlisted and the reason is logged.
- Source: TBD-26
- ✅ Approved by user

### D-62: Flow diagram

- One business process diagram, `process.mmd` in the project root (Mermaid): the life of one email, one lane per participant (sender, intake, worker, manager, approval handling, close), top to bottom.
- High level: every participant and step (intake, queue, facts, classification, SPAM first, urgency and labels, actions, approval round trip, record and audit log, error labels), not every decision; no decision numbers in the chart.
- Source: TBD-31
- ✅ Approved by user

### D-63: Invoice / receipt detection through a `subtype` field

- `Classification` has one extra field, `subtype`: an enum, starting with `finance` | `none`. New cases that need a signal beyond the primary label add enum values, not new fields.
- `finance` covers invoices, receipts, payment confirmations and billing statements. The policy engine acts on it only when `primary == INFO_ONLY`: the `Finance` label is added and the email is archived. Otherwise `subtype` is ignored (e.g. a fake invoice is SPAM).
- INFO_ONLY emails with `subtype == none` are labelled INFO_ONLY and stay in the inbox.
- Evals (D-51) include must-pass cases for each variation: a real invoice, a receipt, a fake invoice (SPAM), and an INFO_ONLY email that isn't an invoice.
- Source: TBD-33
- ✅ Approved by user

### D-64: Proposed times as date expressions; code computes the dates

- **The LLM reads, code calculates.** The meeting extractor's output schema (the PydanticAI `output_type`, so it is mandatory) holds the proposed times as a **list of date expressions**, one per proposed time. The LLM restates what the sender wrote; it never produces a calendar date for a relative phrase and gets no reference date.
- **Expression kinds:**
  - `absolute`: date, time — "5 October at 14:00"; `year` only if the sender wrote one, never inferred (said in both prompts and in the `year` field description of the output schema);
  - `relative_days`: days, time — "today", "tomorrow", "in 3 days"; negative days only for a past deadline ("due yesterday" = −1, D-24);
  - `weekday`: weekday, `this` | `next`, time — "Friday", "this Friday", "next Tuesday";
  - `window`: start / end expressions, or `this_week` / `next_week` — "sometime next week", "Tuesday or Wednesday afternoon"; `part_of_day` (`morning` | `afternoon` | `evening`) holds the sender's own word and is never converted to clock times by the LLM; `start_time` / `end_time` hold only clock times the sender wrote ("between 2 and 4pm"). "noon" is a point at 12:00; "end of day" and "lunchtime" are no meeting time (`none`). A window always has both `start` and `end`; a single day has them equal ("next Wednesday morning" → start = end = WED next). Days joined by "or" give one window per day ("Tuesday or Thursday afternoon" → two windows, Wednesday excluded); "from X to Y", "between X and Y", "X through Y" give one window. `part_of_day` and explicit times apply to every day in the window. "before Friday" / "by Friday" for a meeting → start = today, end = that day;
  - `none`: no time mentioned, or a phrase that fits no kind → the agent proposes free slots (ADR-4).
- A shared part applies to every item: "today or tomorrow at 13" → `relative_days 0 @13:00` and `relative_days 1 @13:00`.
- **Code** resolves each expression from the email's received time in the resolved timezone (ADR-4). **A missing year is the occurrence nearest to the received date** (previous, current or next year; on a tie, the future one), the same rule for meetings and deadlines: received 20 Dec, "January 5" → next year; received 28 Sep, "due September 25" → this year (overdue); received 10 Jan, "December 28" → last year. A stated year always wins. A date that exists in no candidate year (29 Feb) is an invalid expression, not a crash. **`part_of_day` is mapped by code against the configured business hours:** morning = `BUSINESS_HOURS_START`–12:00, afternoon = 12:00–`BUSINESS_HOURS_END`, evening = `BUSINESS_HOURS_END`–21:00 (outside business hours → D-14); explicit `start_time` / `end_time` win over `part_of_day`, per bound. **A window resolves to one slot per business day** (Saturday and Sunday skipped), each limited to the part of day or explicit times; a window with only a start or only an end is a single day. The meeting request carries a **list** of windows: options fill from them in order, and alternatives outside the requested interval start after the last window. "`next` <weekday>" is that weekday in the following calendar week (on Monday 28 Sep, "next Tuesday" = Tue 6 Oct); "`this` <weekday>" or a bare weekday is the one in the current week, or the next one if already past.
- **The approval email shows the original phrase next to the computed time**, e.g. `"next Tuesday 10:00" → Tue 6 Oct 10:00 EEST`, so a misreading is caught before anything is created.
- **Options:** the resolved times follow ADR-4's rules for several exact times (free ones first; busy ones last, marked as double bookings).
- **Tests:** evals (D-51) check the returned expressions per phrase type: exact match for point expressions; window proposals are compared by their resolved daily slots (received time and business hours of the eval), so both valid shapes of adjacent days pass. Unit tests check the date calculation.
- Source: TBD-42
- ✅ Approved by user

### D-65: Every configuration variable is required; no defaults

- The app has **no default for any configuration variable**. Startup fails when any required variable is missing, naming each one (D-29). Recommended values live only in `.env.sample` (D-37).
- **Always required:** `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `LLM_PROVIDER`, `AGENT_SENDER_NAME`, `APPROVAL_SIGNING_KEY`, `APPROVAL_EXPIRY_BUSINESS_DAYS`, `SALES_DECLINE_REQUIRES_APPROVAL`, `DEFAULT_TIMEZONE`, `BUSINESS_HOURS_START`, `BUSINESS_HOURS_END`, `DEFAULT_MEETING_MINUTES`, `SCHEDULING_HORIZON_BUSINESS_DAYS`, `MEETING_MIN_LEAD_MINUTES`, `MEETING_MAX_OPTIONS`, `POLL_INTERVAL_SECONDS` (minimum 5), `ACTION_DELAY_MS`, `WORKER_COUNT`, `MAX_ATTEMPTS`, `LLM_MAX_INPUT_CHARS`, `DB_PATH`, `STARTUP_BACKFILL_MESSAGES`, `RETENTION_DAYS`, `MIN_LOG_LEVEL`, `LOG_COLOR`, `ENABLE_MCP`.
- **Required only when `ENABLE_MCP=true`:** `MCP_PORT`, `MCP_HOST` (D-59).
- **Required, empty is a valid value:** `MANAGER_EMAIL` (empty = the mailbox itself, D-46), `INTERNAL_DOMAINS` (empty = no internal senders), `SPAM_ALLOWLIST_SENDERS`, `SPAM_ALLOWLIST_DOMAINS` (empty = none).
- **Required only for the chosen provider:** `gemini`: `GEMINI_API_KEY`, `LLM_MODEL`; `anthropic`: `ANTHROPIC_API_KEY`, `LLM_MODEL`; `azure`: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_DEPLOYMENT`.
- **The Anthropic key must be workspace-scoped:** a key without a workspace is rejected with HTTP 400 (Anthropic requires the `anthropic-workspace-id` header for it); no workspace variable exists.
- **Internal tuning constants stay in code, not in configuration:** the approval-email urgency threshold (3 h, `core/approval_email.py`), the per-email retry delay (5 s, `app.py`), the re-issue alternatives search window (21 days, `pipeline/approvals.py`), the LLM request timeout (30 s), the output-validation retries (2) and the per-call token cap (`adapters/llm/providers.py`). They are implementation details, not operator settings.
- **No `GMAIL_ADDRESS`:** the mailbox's own address is never configured; it is read from `users.getProfile` at startup (D-18).
- Source: TBD-43
- ✅ Approved by user

### D-66: DST edge cases are ignored

- **Executive decision by the user: this edge case is deliberately ignored.**
- A requested local time that doesn't exist (skipped at the spring clock change) or is ambiguous (repeated at the autumn clock change) is treated as an **invalid request**: that time is not used, and the approval email says it is invalid.
- No special handling and no dedicated unit tests for these times.
- Source: F-9
- ✅ Approved by user

### D-67: Terraform for the Google Cloud setup

- `terraform/gmail/terraform.tfvars` (committed, no secrets) sets the project id `dt-agent-interview-2026` and the name `dt-agent`.
- **Terraform state is committed to git** (`terraform.tfstate` and backups), for simplicity in this POC; production would use a GCS backend and never commit state. Accepted risks: no state locking, no remote history beyond git, possibly stale state. The state holds no secrets (project id and name, enabled APIs, metadata). Recorded as a decision in `terraform/README.md`.
- `terraform/gmail/` creates what Terraform can: the Google Cloud project (no organization, no billing) and the Gmail API and Google Calendar API (D-18 steps 2–3).
- The OAuth consent screen (External, scopes, test user) and the Desktop OAuth client can't be created by Terraform or the API; they stay manual console steps, and the refresh token needs the owner's browser consent (D-18). The wizard `terraform/print_env.sh` guides through them.
- `terraform/README.md` explains what it is, what it does, what stays manual, and how to run it.
- The same commands work inside the app container: the app image also contains the Google Cloud CLI and Terraform, and `terraform/` is bind-mounted at `/app/terraform`; run them after `docker exec -it <container> /bin/sh`. No separate build target or entry point; Docker only starts the app.
- Authentication: a dedicated gcloud configuration `dt-agent` logged in as the dedicated Gmail account; Terraform receives a short-lived token via `GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token --configuration=dt-agent)"`. The operator's other gcloud configurations and machine-wide Application Default Credentials are never touched. Never a service account (D-18).
- ✅ Approved by user

### D-68: A re-issued meeting approval does not show what was requested

- **Accepted narrow case, not handled in the POC for lack of time.**
- A re-issued meeting approval (expired, option passed, slot taken; D-11, D-14) re-checks the old options by their times only: it has no "Requested" section and no REQUESTED/ALTERNATIVE tags (D-13). The options and the REJECT reply are correct.
- ✅ Approved by user

### D-69: The link-text check does not read Gmail's plain-text link format

- **Accepted narrow case, not handled in the POC for lack of time.**
- Gmail's `text/plain` part writes a link as `visible text <real address>`; the link-text check (D-44) only reads `visible text (real address)`, the form `html_to_text` produces. For such mail the mismatch line is missing; the "Link hosts" line still lists every host, and the classifier decides SPAM from it and the content (a phishing email sent from Gmail lands in Spam).
- ✅ Approved by user

---

## Requirements

### Labels

Each email gets exactly one primary label. `URGENT` is a modifier that may be added to any primary label except `SPAM`.

| Label | Meaning |
|---|---|
| MEETING_REQUEST | Calendar coordination needed |
| TASK | Action item for the manager |
| INFO_ONLY | FYI, no action needed |
| SALES_OUTREACH | Unsolicited vendor/sales contact |
| MARKETING | Newsletters, promos, bulk mail |
| SPAM | Malicious, phishing, or junk mail |
| URGENT (modifier) | Deadline or escalation |

### Required behavior

| Scenario | Decision | Action | Manager approval |
|---|---|---|---|
| Newsletter from known bulk sender | MARKETING | Gmail: archive | No — auto |
| Meeting request with proposed times | MEETING_REQUEST, parse times | Calendar: create event | Yes — email confirmation |
| Sales cold outreach | SALES_OUTREACH | Gmail: polite decline + archive | No — auto (configurable) |
| Urgent message from internal sender | URGENT + TASK, flag | Gmail: flag + notify manager | Yes — required |
| Invoice / receipt | INFO_ONLY | Gmail: archive to Finance label | No — auto |
| Phishing / suspicious link | SPAM | Gmail: move to Spam + log | No — auto |

- The agent decides when a courtesy auto-reply (acknowledgement, thank-you) is warranted.
- Calendar events must respect existing availability (the calendar is pre-populated) and be planned reasonably.
- Any calendar action or reply sent on behalf of the manager requires email confirmation: the agent sends a summary, the manager replies APPROVE or REJECT.
- The agent must handle unseen emails with no prior knowledge of their content.

### Non-functional requirements

- **Audit trail:** a structured log entry (label, action, outcome) for every processed email.
- **Error handling:** fail gracefully and log all failures.
- **Human-in-the-loop:** manager approval via email reply before any send or calendar action.
- **Containerized:** Docker image; Gmail account and credentials passed as environment variables.
- **LLM:** a hosted LLM API (no local model). OpenAI / Azure OpenAI compatibility lets the operator substitute their own credentials.
- **Integrations:** Gmail and Google Calendar.
- **Docs:** a README describing how to start and use the agent.
- **MCP:** expose the agent as an MCP server (optional in the requirements; included, D-59).

---

## Architecture

### Startup and intake flow

Derived from D-6, D-7, D-17, D-19 to D-22 and D-26 to D-31.

0. **Configuration (D-29):** read all env vars once into the configuration object.
1. **Database check (D-6):**
   - create the SQLite file if it's missing;
   - if it's invalid, rename it to `<name>-failed-<UTC timestamp>-UTC.<ext>`, create a new one, and show a visible error on screen and in the logs (details D-54).
2. **Poller, first iteration (D-31, D-7, D-21, D-28):**
   - take the cursor: `users.getProfile` → `historyId` (in memory);
   - list the last X INBOX messages (`messages.list` with `labelIds=INBOX`; X = 0 → nothing);
   - skip IDs already in SQLite (processed and our own sent messages, D-22) and messages older than `RETENTION_DAYS`;
   - send the rest into the queue; remember this list until the next iteration.
3. **Poller, next iterations (D-17, D-19, D-31):**
   - every `POLL_INTERVAL_SECONDS` (min 5, with jitter), call `history.list` from the cursor (`labelId=INBOX`, `historyTypes=messageAdded`, all pages; Spam is never read) and advance the cursor;
   - send the new IDs into the queue, skipping IDs in SQLite, IDs from the remembered first list (dropped after the second iteration) and IDs repeated within the batch;
   - if the cursor has expired (HTTP 404), repeat the first iteration.
4. **Queue (D-30):**
   - producers send message IDs without waiting; `WORKER_COUNT` workers process them, each one message at a time, pausing `ACTION_DELAY_MS` after each message (D-20);
   - each message is recorded in SQLite at the end of its flow; a crash mid-flow means it is processed again (D-27);
   - every message the agent sends has its returned Gmail `id` recorded immediately (D-22).
5. **Errors:**
   - rate-limit / server errors → truncated exponential backoff (no client-side rate limiter, D-19);
   - quota errors are written to the audit log (D-19).

### Configuration variables

All read once at startup into the configuration object (D-29), documented in `.env.sample` (D-37). No defaults: every variable is required (D-65).

| Group | Variables |
|---|---|
| Google (D-18) | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` |
| LLM (D-36) | `LLM_PROVIDER` (`gemini` \| `anthropic` \| `azure`); for `gemini`: `GEMINI_API_KEY`, `LLM_MODEL`; for `anthropic`: `ANTHROPIC_API_KEY`, `LLM_MODEL`; for `azure`: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_DEPLOYMENT` |
| Approval / sending (D-10, D-23, D-33, D-38, D-46) | `AGENT_SENDER_NAME`, `MANAGER_EMAIL` (may be empty), `APPROVAL_SIGNING_KEY`, `APPROVAL_EXPIRY_BUSINESS_DAYS`, `SALES_DECLINE_REQUIRES_APPROVAL` |
| Sender rules (D-42, D-61) | `INTERNAL_DOMAINS`, `SPAM_ALLOWLIST_SENDERS`, `SPAM_ALLOWLIST_DOMAINS` (all may be empty) |
| Calendar (D-1, ADR-4) | `DEFAULT_TIMEZONE`, `BUSINESS_HOURS_START`, `BUSINESS_HOURS_END`, `DEFAULT_MEETING_MINUTES`, `SCHEDULING_HORIZON_BUSINESS_DAYS`, `MEETING_MIN_LEAD_MINUTES`, `MEETING_MAX_OPTIONS` |
| Runtime (D-19, D-20, D-30, D-43, D-56) | `POLL_INTERVAL_SECONDS` (min 5), `ACTION_DELAY_MS`, `WORKER_COUNT`, `MAX_ATTEMPTS`, `LLM_MAX_INPUT_CHARS` |
| State (D-6, D-7, D-28) | `DB_PATH`, `STARTUP_BACKFILL_MESSAGES`, `RETENTION_DAYS` |
| Logging (D-58) | `MIN_LOG_LEVEL`, `LOG_COLOR` (`auto` \| `always` \| `never`) |
| MCP (D-59) | `ENABLE_MCP` (`true` \| `false`); `MCP_PORT`, `MCP_HOST` when `ENABLE_MCP=true` |

### Directory structure

Pure logic (`domain`, `core`) is separated from I/O (`adapters`) behind `ports`; `pipeline` wires them together. Prompts are prompt-only `.md` files next to their agents, loaded as package data. Tests are grouped by kind; evals live in `tests/evals/` and are excluded from `uv run pytest` (`--ignore=tests/evals`, D-51, D-52).

```
dt_agent/                            (project root)
├── pyproject.toml
├── uv.lock
├── README.md
├── Dockerfile
├── docker-compose.yml
├── .env.sample
├── .gitignore
├── process.mmd                      (D-62)
├── terraform/
│   ├── README.md                    (D-67)
│   ├── print_env.sh                 (D-18: credentials wizard, writes the GOOGLE_* values into .env)
│   └── gmail/                       (D-67: Google Cloud project + Gmail/Calendar APIs)
├── scripts/
│   ├── oauth_consent.py             (D-18: prints the refresh token)
│   └── live_check.py                (D-51: optional operator tool)
├── src/dt_agent/
│   ├── __init__.py
│   ├── app.py                       (composition root and run loop: ADR-3, D-6, D-43)
│   ├── cli.py                       (Typer: run, eval)
│   ├── config.py                    (D-29, D-65)
│   ├── logging.py                   (D-57, D-58)
│   ├── mcp_server.py                (D-59)
│   ├── domain/                      (labels, Classification + subtype, Email, date expressions, Clock)
│   ├── ports/                       (MailPort, CalendarPort, MessageStore, Classifier, MeetingExtractor, ReplyDrafter, CourtesyDrafter, AcknowledgementDrafter)
│   ├── core/                        (pure logic)
│   │   ├── facts.py                 (D-42, D-44, D-61)
│   │   ├── html_text.py             (HTML to text for the LLM: D-44, D-56)
│   │   ├── routing.py               (D-21, D-22, D-47)
│   │   ├── policy.py                (D-25, D-38 … D-40, D-63)
│   │   ├── urgency.py               (D-24)
│   │   ├── scheduling.py            (ADR-4, D-13, D-14, D-45)
│   │   ├── dates.py                 (D-64, D-66)
│   │   ├── approval_email.py        (D-8, D-13, D-32, D-40)
│   │   ├── approval_token.py        (D-32, D-33, D-10)
│   │   ├── reply_parser.py          (D-34, D-49)
│   │   └── invitations.py           (D-48)
│   ├── pipeline/
│   │   ├── deps.py                  (the ports and settings a flow needs)
│   │   ├── poller.py                (D-7, D-31)
│   │   ├── inflight.py              (D-31: IDs queued or being processed)
│   │   ├── workers.py               (D-20, D-30, D-43)
│   │   ├── process_email.py         (the per-email flow)
│   │   ├── replies.py               (threaded replies, the "not available" reply: D-13)
│   │   └── approvals.py             (D-9, D-11, D-12, D-34)
│   └── adapters/
│       ├── gmail.py
│       ├── calendar.py
│       ├── google_auth.py           (D-18)
│       ├── google_errors.py         (D-43: retry and error mapping)
│       ├── sqlite_store.py          (D-26, D-28, D-54)
│       └── llm/
│           ├── providers.py         (D-36)
│           ├── _prompting.py        (shared prompt building, retries, usage log)
│           ├── classifier.py
│           ├── meeting_extractor.py
│           ├── reply_drafter.py
│           ├── courtesy_drafter.py
│           ├── acknowledgement_drafter.py (D-40)
│           └── prompts/             (prompt text only, no code)
│               ├── classify.md
│               ├── extract_meeting.md
│               ├── sales_decline.md
│               ├── courtesy_reply.md
│               ├── urgent_ack.md    (D-40)
│               └── judge.md         (LLM judge used by the evals)
└── tests/
    ├── conftest.py                  (ALLOW_MODEL_REQUESTS=False)
    ├── fakes/                       (in-memory ports)
    ├── fixtures/                    (recorded Google API responses, sample emails, .ics files)
    ├── unit/                        (domain, core, config, logging)
    ├── adapters/                    (HttpMock-based)
    ├── pipeline/                    (whole-flow tests with fakes)
    ├── integration/                 (D-52: live MCP test, marker `integration`, never run by `uv run pytest`)
    └── evals/                       (D-51, never run by `uv run pytest`)
        ├── dataset/                 (synthetic emails + expected results; fixtures/ with real notice texts)
        └── run_evals.py             (called by `dt-agent eval`)
```

### Reference: Gmail API limits

Source: Google's Gmail API usage-limits and sync guides (Sep 2026).
- **Rate limits:** 1,200,000 quota units per minute per project; **6,000 quota units per minute per user**.
- **Costs per call:**

  | Call | Units |
  |---|---|
  | `users.getProfile` | 1 |
  | `history.list` | 2 |
  | `messages.list` | 5 |
  | `messages.modify` | 5 |
  | `labels.create` | 5 |
  | **`messages.get`** | **20** |
  | `messages.send` | 100 |
  | `users.watch` | 100 |

- **When exceeded:** requests are rejected (rate-limit errors), and Google recommends **truncated exponential backoff**. Google also states that exceeding the quota limits is planned to incur **charges** later in 2026, with a daily threshold of 80,000,000 units per project before billing applies.
- **Polling cost:** polling isn't a ban risk.
  - Every 15 s → 4 `history.list` calls/min = **8 units/min (~0.13 % of the per-user limit)**, ~11,500 units/day.
  - Every 5 s → 24 units/min.
  - Google's sync guide doesn't prescribe a polling frequency. It mentions push only as a way to avoid "needless polling".
- **The real quota risk is bursts, not polling:**
  - Processing one email costs ~20 (get) + 5–15 (modify/labels), plus 100 per sent reply or approval email.
  - The **startup backfill** (D-7) with X messages costs ~5 + 20·X units: X = 100 → ~2,000 units; X = 300 → ~6,000 units (the whole per-minute budget).
  - Because processing is limited to `WORKER_COUNT` workers (D-30) and each message also waits on the LLM, the real rate is much lower. Backoff guards against bursts.
- **Sending limits (the real "ban" risk):**
  - Consumer Gmail accounts have a daily sending cap (roughly 500 recipients/day; exact limits are set by Google and may change).
  - Exceeding it, or sending spam-like auto-replies, can temporarily block sending for the account.
  - Our outbound volume (approval requests, approved replies) is small, but auto-reply loops must be prevented (D-38, D-39).
- **`historyId` validity:** history is typically available for at least a week, sometimes much less. An out-of-range `startHistoryId` returns HTTP 404, triggering the `messages.list` fallback.

---

## Implementation and steps

_Phase 2 (code in the plan) is skipped: the project started empty. Every task is implemented with red-green TDD (failing test first, then the minimal code to pass, then refactor) and the testing rules below; the full suite runs green after each task; anything that differs from the plan goes to Deviations._

**Build order:** contracts first (scaffold, configuration, logger, domain models, ports and fakes), then pure logic, then adapters, then the pipeline, then the app shell (CLI, MCP), then packaging and docs, then verification and evals. Every task is unit-testable on top of the tasks before it; no live Gmail or Calendar account is used during the build.

### Testing rules (for every task)

**What deserves tests:** behaviour and invariants that would actually break: scheduling and business days, date-expression resolution, token validation, guardrails (addresses, recipients, automated senders), routing, the retry / error policy, deduplication, configuration validation, and whole-flow pipeline tests per required-behaviour row, with the approval round trip as the highest-value test.

**Do NOT write:**
- tests that a README / `.env.sample` / `pyproject.toml` exists or contains a word (the only exception: the code ↔ `.env.sample` field check, `test_env_sample_lists_every_settings_field`);
- `PrimaryLabel.SPAM.value == "SPAM"`, a model storing its fields, a setting equal to what the test just set;
- mocks of the `googleapiclient` call chain (`service.users().messages().modify.assert_called_once_with(...)`); use `HttpMock` for adapters and fakes at port level for the pipeline, and assert on state;
- tests of third-party libraries (PydanticAI validation, tenacity retry counts, pydantic-settings reading env, structlog emitting JSON, PyJWT rejecting a bad signature); test our policy on top of them;
- snapshot / exact-string tests of prompts or email wording; assert that the facts are present (options, deadline, token block, warnings);
- LLM decision quality (that is the evals' job, D-51); unit tests use `TestModel` / `FunctionModel` with canned output;
- tests of the fakes beyond their one or two non-trivial behaviours;
- anything that really sleeps (inject the clock / sleep function);
- Typer help output, `__repr__`, log call counts, trivial pass-through methods.

**Fakes vs mocks:**
- hand-written in-memory fakes of our ports for everything the pipeline touches, with failure knobs (e.g. `fail_next(exc)`) instead of mocks; assert on their state (labels, sent messages, calendar events, SQLite rows);
- real SQLite in a temporary directory, not a fake store;
- `TestModel` / `FunctionModel` for the LLM ports; `ALLOW_MODEL_REQUESTS=False` globally;
- `unittest.mock` only at edges with no state to observe: process exit, signal handling, the sleep function;
- `HttpMock` / `HttpMockSequence` for: history pagination and 404, `messages.get` raw parsing, `messages.send` MIME, `events.insert` / `events.import` / `events.patch` bodies, `freebusy` parsing, label ensure-create. Not for one-line wrappers.

**Test description format:** `test_<unit>_<condition>_<expected>`: given / when / then with concrete values.

---

## Task list

None open.

---

## Deviations

### Deviation history

- **DEV-16 (resolved):** on the first live run, genuine Google notices were moved to Spam (authentication passes were invisible to the model, the body was markup, spam was not a separate first step); a code veto then kept content-only scams (advance-fee from an authenticated free-mail account) out of Spam. The veto is removed; the classifier decides SPAM from headers and content, and code acts on SPAM first with nothing else after it (D-24, D-44, D-48).

- **DEV-15 (resolved):** D-19 promised a client-side Gmail rate limiter that was never built; D-19 now relies on the worker count, the per-message pause and the backoff instead (no code change).

- **DEV-8 (resolved):** courtesy replies missed delivered/shared emails ("finished the report… let me know if it looks good", shared meeting notes); D-39 rewritten (thank for delivered or shared things, never imply read/approved) and the missing one-courtesy-reply-per-thread check added; Haiku pre-check 15/15 warranted decisions (D-39).
- **DEV-13 (resolved):** a courtesy thank-you evaluated the content ("That's a great achievement for the team!", later "That's great to hear…", "…and for your work on it"); the body now thanks for sending or sharing only, never comments on or evaluates it, never thanks for the work; the judge criterion names all three (D-39).
- **DEV-14 (resolved):** a courtesy thank-you gushed ("Thanks so much for the kind words! Happy to help out."); the body is now business-appropriate, no exclamation marks, never thanks for kind words, and a plain thank-you gets "You're welcome"; new judge criterion; Haiku pre-check 5/5 warranted drafts pass the judge and all 8 courtesy judge-calibration cases give the pinned verdict (D-39).

- **DEV-1 (resolved):** base images pinned by digest (`python:3.14-slim@sha256:51dafde8…`, `ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b1…`) and the image built.
- **DEV-2 (resolved):** the first eval run failed with HTTP 400 because the Anthropic key had no workspace; replaced by a workspace-scoped key, now required by D-65 and noted in `.env.sample`. No code change.
- **DEV-3 (resolved):** fixed tuning constants (urgency threshold, retry delay, re-issue search window, LLM timeout, retries, token cap) stay in code; recorded in D-65. No code change.
- **DEV-5 (resolved):** the MCP server always listened on `0.0.0.0`; it now listens on `MCP_HOST` (`127.0.0.1` locally, `0.0.0.0` only inside the container, published on the host's `127.0.0.1`) (D-59).
- **DEV-4 (resolved):** a re-issued urgent acknowledgement lost its urgent subject; the token's `ReplyAction` now carries `urgent` (D-11).
- **DEV-11 (resolved):** the urgent approval email had no deadline line; the classification now carries `deadline_phrase` and `deadline_at` (one resolution feeds `urgent` too), shown as `Deadline: … (…)`, "(overdue)" when past, and logged (D-24, D-40).
- **DEV-7 (resolved):** the urgent-internal acknowledgement reused the courtesy drafter and could send an approval for an empty reply; now a dedicated string-output drafter (`urgent_ack.md`), a notification-first approval email, and a plain notification + `AI/NeedsReview` fallback (D-40).
- **DEV-9 (resolved):** a translated sales decline dropped "at the moment" (German "nicht das Richtige für uns"); `sales_decline.md` now keeps it in every language; Haiku pre-check 9/9 sales cases pass drafting and the judge (D-38).
- **DEV-12 (resolved):** the urgent acknowledgement named the signer ("brought to Alex's Assistant's attention"); it now only says the message was received and flagged as urgent; Haiku pre-check 3/3 ack cases pass the judge (D-40).

---

## Open Tasks / TBD


---

### Calendar & approval — nice-to-haves

_Each item points to the related decision._

**Nice-to-haves (optional hardening / features):**
- **N-1 — One-time code in approvals** (`APPROVE 4821`) against approving the wrong thread. (D-34)
- **N-2 — Rate-limited security notice** to the manager when a forged approval is detected. (D-9)
- **N-3 — Post-insert race check:** re-query free/busy after `events.insert` and roll back if an overlap appeared. (D-12)
- **N-4 — Google Meet link** (`conferenceData`) on created events. (D-55)
- **N-5 — Recurring meetings** (RRULE, availability for the first N occurrences); not handled now (D-45).
- **N-7 — Reminders or focus-time blocks for TASK deadlines.** (D-55)
- **N-8 — Reschedule and cancel existing meetings;** not handled now (D-45).
- **N-10 — Nested JWT (JWE)** for payload confidentiality, if ever needed. (D-33)
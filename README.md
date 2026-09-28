# dt-agent — AI Email & Calendar Assistant

A containerized Python agent that watches one Gmail mailbox, classifies each
incoming email, and takes autonomous actions on it: labelling, archiving,
declining sales outreach, drafting courtesy replies, flagging urgent
messages, and scheduling calendar events. Anything sent on the manager's
behalf — a reply or a calendar event — is held back until the manager
approves it by replying to an email.

## What it does

Every email that reaches the mailbox's inbox gets exactly one primary label:

| Label | Meaning |
|---|---|
| `MEETING_REQUEST` | Calendar coordination needed |
| `TASK` | Action item for the manager |
| `INFO_ONLY` | FYI, no action needed (invoices/receipts get the extra `Finance` label and are archived) |
| `SALES_OUTREACH` | Unsolicited vendor/sales contact |
| `MARKETING` | Newsletters, promos, bulk mail |
| `SPAM` | Malicious, phishing, or junk mail (moved to the Spam folder) |
| `URGENT` | Modifier added on top of any of the above, except `SPAM` |

Required behaviours:

- **Newsletters / marketing** are archived automatically.
- **Meeting requests** are parsed for proposed times (or scheduled into the
  first open slot when none is given) and go out as a calendar-approval
  email.
- **Sales outreach** gets a polite decline reply and is archived; the decline
  is sent automatically unless `SALES_DECLINE_REQUIRES_APPROVAL=true`.
- **Urgent messages from an internal sender** are flagged (starred
  in Gmail) and trigger an approval email that doubles as the manager
  notification.
- **Invoices / receipts** are labelled `Finance` and archived.
- **Phishing / suspicious mail** is logged (with the deterministic facts the
  classifier saw: sender authentication, links, attachments, bulk-mail cues, ...) and
  moved to the Spam folder.
- The agent also decides, on its own, when a short courtesy
  acknowledgement/thank-you is warranted for an otherwise unremarkable email.

**Approval by email reply.** Any reply or calendar action taken on the
manager's behalf is proposed first: the agent sends an `[Approval]` email
with a signed token, a plain-English description of what it will do, and (for
meetings) numbered time options. The manager answers in the same thread:

- `APPROVE` (or `APPROVED` / `YES` / `OK` / `CONFIRM` / `CONFIRMED`) — runs
  option 1, the first free, non-past option;
- `APPROVE <n>` — runs option `n`;
- `REJECT` (or `REJECTED` / `NO` / `DECLINE`) — does nothing, logged only.

Approvals expire after a few business days (configurable) or when every
proposed time has passed, whichever comes first; an expired or since-taken
slot causes the agent to send a fresh approval request in the same thread
rather than silently failing.

**Calendar scheduling** only ever checks the manager's own Google Calendar —
other attendees' availability is unknown, and every calendar-approval email
says so. Proposed times are placed inside configured business hours,
rounded up to a clean time, and never double-booked (a plain `APPROVE`
never creates a conflicting event; a requested time that is busy or outside
business hours is offered last, explicitly marked as such).

## Setup

### 1. Google Cloud project and OAuth

1. Create a dedicated Gmail account for the agent (it also gets the agent's
   Google Calendar). Don't reuse a personal mailbox.
2. In that account, create a Google Cloud project ("No organization", no
   billing account needed).
3. Enable the **Gmail API** and the **Google Calendar API** for the project.

   Steps 2–3 are done with Terraform — see
   [`terraform/README.md`](terraform/README.md), steps 1–5.
4. Run the setup wizard `terraform/print_env.sh` ([`terraform/README.md`](terraform/README.md),
   step 6). It walks through the consent screen (**External**, left in
   **Testing** with the dedicated account as a test user), the five scopes,
   the **Desktop app** OAuth client and the consent (terminal only; you
   open the printed URL and paste back the address the browser ends on),
   then prints `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and
   `GOOGLE_REFRESH_TOKEN` and writes them into `.env` (create it first
   with `cp .env.sample .env`; nothing else in it changes). The
   "unverified app" warning is expected and fine for this single-user
   setup. In Testing, the refresh token expires after 7 days: run the
   wizard again.

### 2. `.env`

Copy `.env.sample` to `.env` and fill it in. Every variable listed there is
required — the app has no built-in defaults and refuses to start if any
required variable is missing or invalid, naming every offending one at
once. A few variables may be deliberately left empty (see the comment above
each): `MANAGER_EMAIL`, `INTERNAL_DOMAINS`, `SPAM_ALLOWLIST_SENDERS`,
`SPAM_ALLOWLIST_DOMAINS`.

Notable variables:

- **`LLM_PROVIDER`** — `gemini`, `anthropic`, or `azure`. Set only the
  variables for the provider you choose:
  - `gemini`: `GEMINI_API_KEY`, `LLM_MODEL`.
  - `anthropic`: `ANTHROPIC_API_KEY`, `LLM_MODEL`.
  - `azure`: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`,
    `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_DEPLOYMENT` (the deployment
    name selects the model; `LLM_MODEL` is unused for Azure). Azure OpenAI
    calls never send a `temperature` parameter — some Azure model families
    reject any non-default value — and the app logs this at startup when
    `LLM_PROVIDER=azure`.
  - Use small, inexpensive ("Haiku-tier") models; the provider and model are
    fixed for the whole app, not chosen per task.
- **`MANAGER_EMAIL`** — leave empty to have approval/notification emails go
  to the monitored mailbox itself (the manager replies from that same
  account). Set it to an external address to route approvals there instead
  (and whitelist the agent's address so approvals don't land in spam).
- **`AGENT_SENDER_NAME`** — the display name on every outgoing message. Set
  it to the manager's own name when `MANAGER_EMAIL` is empty, or to an
  assistant-style name (e.g. `"Anna's Assistant"`) when `MANAGER_EMAIL` is
  set to an external address. The sending address is always the Gmail
  account itself.
- **`INTERNAL_DOMAINS`** — comma-separated domains (e.g. `acme.com`) whose
  senders count as internal, once Gmail's own authentication confirms the
  domain. Leave empty to disable the "urgent message from an internal
  sender" behaviour entirely.

### 3. Local development

The app never reads `.env` itself (D-15/D-53); something in front of it must
load it:

- **Terminal:** `uv run --env-file .env dt-agent run`
- **IntelliJ / PyCharm:** set "Paths to .env files" once in the run
  configuration templates for Python and for pytest (Run → Edit
  Configurations → Templates), so every run/debug/test configuration picks
  it up without repeating the setting.
- **Docker:** `docker-compose.yml` already declares `env_file: .env`.

## Running

### Docker (recommended)

```bash
docker compose up --build
```

This builds the multi-stage image (Python 3.14-slim) and starts one
container. The SQLite database lives on the `./data` volume, so it
survives restarts. If the process exits
on a fatal error (bad credentials, invalid configuration), the container's
`restart: on-failure:3` policy retries a few times and then stops — it never
loops forever.

The app writes one structured JSON log line per event to stdout (no
terminal UI). When stdout is a terminal, lines are colour-highlighted like
`jq` output; redirected or piped output stays plain JSON.

### Local, without Docker

```bash
uv run --env-file .env dt-agent run
```

## MCP server

Set `ENABLE_MCP=true`, `MCP_HOST` and `MCP_PORT` to also serve an MCP
server, in the same process, exposing the agent's decisions as dry-run tools
you can call directly from an MCP client (Claude Code, Claude Desktop, MCP
Inspector, ...). It uses the streamable HTTP transport at:

```
http://127.0.0.1:<MCP_PORT>/mcp
```

`MCP_HOST` is the address the server binds to: `127.0.0.1` for a local run,
so it's never reachable from outside this machine. `docker-compose.yml`
overrides it to `0.0.0.0` inside the container (needed for Docker to
forward the published port), while still publishing the port bound to the
host's `127.0.0.1` only, so it's reachable from the host machine and never
from the network. There is no authentication — this is fine only because
it's localhost-only.

Every tool is dry-run: nothing is sent, labelled, archived, moved to spam,
scheduled, or written to the database (LLM-backed tools still make a real,
billed model call). The six tools:

1. `classify_email` — classify a subject/body/sender into a primary label,
   `urgent`, `subtype`, plus the facts that were extracted.
2. `extract_meeting` — extract proposed times, duration, timezone, extra
   attendees, and resolve the proposed times to actual dates.
3. `draft_sales_decline` — draft the sales-outreach decline body.
4. `draft_courtesy_reply` — decide whether a courtesy reply is warranted and
   draft it.
5. `extract_facts` — the deterministic, code-only facts (no LLM call).
6. `find_meeting_slots` — read-only free/busy slot search against the real
   calendar.

### Calling the tools from the browser (MCPJam Inspector)

[MCPJam Inspector](https://github.com/MCPJam/inspector) gives every tool a
form in the browser, so you don't hand-write JSON-RPC requests. It is a
separate Node tool (needs Node/`npx`); nothing is installed in the project.

1. Set `ENABLE_MCP=true` in `.env` and start the agent (`MCP_PORT=8765`
   here).
2. In another terminal, start the inspector (`@latest` avoids an older
   cached copy):

   ```bash
   npx @mcpjam/inspector@latest
   ```

   It opens `http://127.0.0.1:6274` in the browser.
3. Add a server:
   - **Server ID:** `dt-agent`
   - **Transport:** Streamable HTTP (not the command/stdio form)
   - **URL:** `http://127.0.0.1:8765/mcp`
   - no headers, no auth
4. Connect, open the tools list, pick a tool, fill its fields and run it.

**The `headers` field** (`classify_email`, `extract_facts`) is optional and
takes a JSON object of email headers, on **one line**. To have the sender
treated like a real Gmail message that passed authentication (the facts then
say the sender is authenticated, and internal when its domain is in
`INTERNAL_DOMAINS`), use a `sender` at that domain and:

```
{"Authentication-Results": "mx.google.com; dmarc=pass header.from=gmail.com"}
```

Left empty, the facts say `no Authentication-Results header`.

## Tests

Unit tests run fully offline — no real Gmail, Calendar, or LLM calls are
ever made (blocked globally):

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

**Evals** measure the quality of actual LLM behaviour (classification,
meeting extraction, drafted replies) against a hand-written dataset. They
make real, billed LLM calls and are never run as part of `uv run pytest`:

```bash
uv run --env-file .env dt-agent eval
uv run --env-file .env dt-agent eval --case some-case-name
uv run --env-file .env dt-agent eval --kind classify   # classify | extract | sales | courtesy
```

Every case must pass; a run prints a compact per-case pass/fail report and
exits non-zero on any failure.

**MCP integration test.** `tests/integration/test_mcp_live.py` starts the
real app locally (`dt-agent run`, no Docker) from `.env`, with only
`ENABLE_MCP=true`, `MCP_HOST=127.0.0.1` and a free `MCP_PORT` overridden,
then a real MCP client connects over HTTP, lists the tools and calls each
one: real LLM calls and real Calendar free/busy. It is skipped without
`.env` credentials and never runs as part of `uv run pytest`:

```bash
uv run pytest -m integration tests/integration
```

While it runs, the app also polls the inbox and processes new mail as
usual (same `DB_PATH`, so mail already handled is not processed again).
Don't run it while another copy of the agent is running on the same mailbox.

**Optional live check.** `scripts/live_check.py` is an operator tool, never
used during the build, that sends a handful of test emails (one per
required-behaviour row, plus a courtesy case and an explicit meeting
request) to the running agent's mailbox over SMTP, then prints a checklist
to verify by hand in Gmail and in the agent's log:

```bash
LIVE_CHECK_SENDER=... LIVE_CHECK_APP_PASSWORD=... LIVE_CHECK_TARGET=... \
  uv run --env-file .env python scripts/live_check.py
```

`LIVE_CHECK_APP_PASSWORD` is a Gmail app password
(https://myaccount.google.com/apppasswords) for the sending account, which
does not have to be the agent's own mailbox. Each email triggers at least
one Haiku-tier LLM call — negligible cost, but real.

## Security notes (proof-of-concept scope)

- The container **runs as root** with no further hardening. Accepted risk
  for this proof of concept.
- **Sender and subject are written to the log.** Email bodies never are, but
  sender/subject is personal data, which is a GDPR-relevant risk accepted
  only for this proof of concept — a production-ready version should not log
  them.
- Secrets (API keys, OAuth tokens, approval JWTs) are never intentionally
  logged; the logger also masks them as a safety net.
- The dependency lockfile (`uv.lock`) pins every dependency. Check for known
  vulnerabilities by hand from time to time:

  ```bash
  uv run pip-audit
  ```

## Troubleshooting

- **"DATABASE INVALID: renamed to ... A new database was created."** — the
  SQLite file at `DB_PATH` was missing a table/column, failed its integrity
  check, or came from an incompatible schema version. The old file (and its
  `-wal`/`-shm` siblings) is renamed to
  `<name>-failed-<UTC timestamp>-UTC.<ext>` and kept; the app continues with
  a fresh database. Nothing is deleted automatically — inspect the renamed
  file if you need the old history.
- **`Invalid configuration: - NAME: reason` (app exits immediately)** — one
  or more required environment variables are missing or invalid. Every
  offending variable is listed at once (never just the first one); fix
  `.env` and restart.
- **`invalid_grant` at startup or while running** — the Google refresh
  token is no longer valid (the OAuth consent screen stays in "Testing"
  status, where refresh tokens expire after 7 days, or access was
  revoked). The app logs the error and exits with a non-zero code rather
  than retrying forever; re-run `terraform/print_env.sh` and update
  `GOOGLE_REFRESH_TOKEN`.
- **Emails labelled `AI/NeedsReview` or `AI/Error`** — the agent couldn't
  handle them fully; the `email.processed` log line for each has the reason.

  | | `AI/NeedsReview` | `AI/Error` |
  |---|---|---|
  | Cause | The model's output was unusable (classification, meeting extraction or a draft) | Processing failed: a Gmail/Calendar/LLM API error that survived its retries, or an unexpected exception |
  | Who acts | The manager | Whoever runs the agent |
  | What they do | Read the email and handle it by hand | Check the logs, fix the cause, have the agent process the emails again if needed |
  | Typical pattern | Occasional, one odd email | Often many at once (an outage, an expired token) |
  | Would retrying help? | No: the same content gives the same result | Often yes, once the cause is fixed |

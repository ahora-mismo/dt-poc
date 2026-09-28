# AGENTS.md

AI Email & Calendar Assistant: a Python agent that watches a Gmail inbox, classifies each
email, and acts on it (labels, replies, calendar events), with manager approval by email
before anything is sent or scheduled. `README.md` explains how to set it up and use it.

## Plans

- **Creating a new plan:** load the `project-plan` skill first
  (`.claude/skills/project-plan/SKILL.md`) and follow it.
- Edit plan files with targeted edits only, never by rewriting the whole file.

## Commands

```bash
uv run pytest                      # unit + pipeline tests, fully offline
uv run ruff check .
uv run ruff format --check src tests
uv run mypy src tests
uv run --env-file .env dt-agent run   # start the agent (real Gmail, Calendar, LLM)
```

- **Evals** make real, billed LLM calls: run only the cases affected by a change, once
  each (`--kind`, `--case`):
  `uv run --env-file .env dt-agent eval --kind classify --case <name>`.
- **Integration test** (real app from `.env`, real MCP calls, local only):
  `uv run pytest -m integration tests/integration`.
- The app reads configuration from environment variables only and never loads `.env`
  itself; the environment is provided externally (e.g. `uv run --env-file .env`, the IDE
  run configuration, or Docker's `env_file`).

## Layout

- `src/dt_agent/domain`, `core`: pure logic, no I/O.
- `ports`: the interfaces; `adapters`: Gmail, Calendar, SQLite and LLM implementations.
- `pipeline`: poller, queue and workers, the per-email flow, approval handling.
- `adapters/llm/prompts/*.md`: prompt text only.
- `tests/unit`, `tests/adapters`, `tests/pipeline`: offline; `tests/evals`: LLM evals;
  `tests/integration`: live MCP test.
- `process.mmd`: the business process diagram; keep it in step with the flow.

## Rules

- Red-green TDD: a failing test first, then the code.
- Tests use the in-memory fakes in `tests/fakes` and assert on their state; no mocks of
  the Google API client chain; no LLM calls (`ALLOW_MODEL_REQUESTS=False`).
- Never read, print or log values from `.env`; key names only.
- Never run git; the user owns version control.
- Stay inside the project folder (`/tmp` is allowed).
- Smoke tests of prompts use Claude Code subagents fed the exact inputs the app renders,
  never an API key.
- `.env` and `.env.sample` keep the same keys; every configuration variable is required.

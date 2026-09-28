"""Command-line entry point for dt-agent.

`run` (task 24) starts the agent; `eval` (task 26, D-51) delegates to
`tests/evals/run_evals.py`.
"""

import runpy
from pathlib import Path

import typer

from dt_agent.app import main as _run_agent

app = typer.Typer(name="dt-agent", help="AI Email & Calendar Assistant.")


@app.command()
def run() -> None:
    """Start the agent (task 24)."""
    raise typer.Exit(code=_run_agent())


def _find_project_root(start: Path) -> Path:
    """Walk up from `start` to the nearest ancestor containing `pyproject.toml`."""
    for directory in (start, *start.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    raise RuntimeError("Could not locate the project root (no pyproject.toml found above cwd).")


@app.command()
def eval(
    case: str | None = typer.Option(
        None, "--case", help="Only run cases whose name contains this substring."
    ),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help="Only run cases of this kind (classify/spam/extract/sales/courtesy/ack/judge).",
    ),
    repeat: int = typer.Option(
        1,
        "--repeat",
        min=1,
        help="Run each selected case N times; it passes only if all N runs pass (D-51).",
    ),
) -> None:
    """Run the LLM behaviour evals against the real model (D-51).

    A dev-only command, run from the repo (evals aren't installed in the wheel, D-51):
    it locates `tests/evals/run_evals.py` relative to the project root and runs it.
    Makes real, billed LLM calls; never run as part of `uv run pytest` (D-52).
    """
    project_root = _find_project_root(Path.cwd())
    run_evals_path = project_root / "tests" / "evals" / "run_evals.py"
    namespace = runpy.run_path(str(run_evals_path), run_name="dt_agent_eval")
    exit_code: int = namespace["main"](case, kind, repeat)
    raise typer.Exit(exit_code)


if __name__ == "__main__":
    app()

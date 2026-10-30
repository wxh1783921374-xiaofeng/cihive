from __future__ import annotations

import asyncio
import json
import os
import shlex
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
import yaml
from rich.console import Console
from rich.table import Table

from cihive.api import create_app
from cihive.config import Settings
from cihive.db import RunStore
from cihive.evaluation import run_evaluation_suite
from cihive.github import GitHubClient
from cihive.model import create_model_adapter
from cihive.models import RepositoryConfig, RunState
from cihive.orchestrator import Orchestrator, create_failure_run
from cihive.worker import run_verifier, run_worker

app = typer.Typer(
    name="cihive",
    help="Evidence-first distributed CI repair agents.",
    no_args_is_help=True,
)
console = Console()


def _default_commands(directory: Path) -> list[list[str]]:
    if (directory / "pyproject.toml").exists():
        return [["python", "-m", "pytest", "-q"]]
    if (directory / "package.json").exists():
        return [["npm", "test"]]
    return [["python", "-m", "pytest", "-q"]]


def _default_setup_commands(directory: Path) -> list[list[str]]:
    commands: list[list[str]] = []
    if (directory / "pyproject.toml").exists():
        commands.append(["python", "-m", "pip", "install", "-e", "."])
    if (directory / "package.json").exists():
        commands.append(
            ["npm", "ci"] if (directory / "package-lock.json").exists() else ["npm", "install"]
        )
    return commands[:3]


@app.command()
def init(
    directory: Annotated[Path, typer.Option("--directory", "-d")] = Path("."),
    command: Annotated[
        list[str] | None,
        typer.Option("--command", help="Verification command; repeat for multiple commands."),
    ] = None,
    setup_command: Annotated[
        list[str] | None,
        typer.Option("--setup-command", help="Per-worktree setup command; repeat as needed."),
    ] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Install the CIHive configuration and GitHub Actions workflow."""
    target = directory.resolve()
    target.mkdir(parents=True, exist_ok=True)
    workflow_target = target / ".github" / "workflows" / "cihive.yml"
    config_target = target / ".cihive.yml"
    if not force:
        existing = [path for path in (workflow_target, config_target) if path.exists()]
        if existing:
            joined = ", ".join(str(path) for path in existing)
            raise typer.BadParameter(f"refusing to overwrite: {joined}; pass --force")

    commands = [shlex.split(item) for item in command] if command else _default_commands(target)
    setup_commands = (
        [shlex.split(item) for item in setup_command]
        if setup_command
        else _default_setup_commands(target)
    )
    config = RepositoryConfig(
        setup_commands=setup_commands,
        verification_commands=commands,
    )
    workflow_target.parent.mkdir(parents=True, exist_ok=True)
    workflow_source = files("cihive").joinpath("templates/install/cihive.yml")
    workflow_target.write_text(workflow_source.read_text(encoding="utf-8"), encoding="utf-8")
    config_target.write_text(
        yaml.safe_dump(
            config.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    console.print(f"created [cyan]{config_target}[/cyan]")
    console.print(f"created [cyan]{workflow_target}[/cyan]")
    console.print("Commit both files to the default branch before installing the App.")


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
) -> None:
    """Start the webhook and model-proxy control plane."""
    uvicorn.run(create_app(Settings()), host=host, port=port)


@app.command()
def run(
    repo: Annotated[str, typer.Option("--repo", help="OWNER/REPO")],
    run_id: Annotated[int, typer.Option("--run-id")],
    token: Annotated[
        str | None,
        typer.Option("--token", envvar="CIHIVE_GITHUB_TOKEN", hide_input=True),
    ] = None,
) -> None:
    """Manually process a failed default-branch workflow run with a GitHub token."""
    settings = Settings()
    store = RunStore(settings.resolved_database_path, settings.data_dir)
    github = GitHubClient(settings, static_token=token or os.getenv("CIHIVE_GITHUB_TOKEN"))
    model = create_model_adapter(settings)
    orchestrator = Orchestrator(settings, store, github, model)

    async def execute() -> RunState:
        workflow_run = await github.fetch_workflow_run(repo, run_id, None)
        repository = await github.request(
            "GET",
            f"/repos/{repo}",
            installation_id=None,
        )
        if repository.get("private"):
            raise ValueError("manual mode only supports public repositories")
        if workflow_run.get("conclusion") not in {"failure", "timed_out"}:
            raise ValueError("the selected workflow run did not fail")
        if workflow_run.get("head_branch") != repository.get("default_branch"):
            raise ValueError("manual mode only accepts failures on the default branch")
        if workflow_run.get("path") == settings.workflow_file:
            raise ValueError("CIHive workflow failures are ignored")
        payload = {
            "workflow_run": workflow_run,
            "repository": repository,
            "sender": workflow_run.get("actor") or {},
        }
        failure_run = create_failure_run(payload, f"manual-{run_id}")
        state = RunState(run=failure_run)
        store.create_run(state)
        return await orchestrator.process_run(state.run.id)

    try:
        state = asyncio.run(execute())
    except Exception as exc:
        console.print(f"[red]run failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print_json(state.model_dump_json())
    if state.status.value == "failed":
        raise typer.Exit(code=1)


@app.command()
def inspect(
    run_id: Annotated[str | None, typer.Argument()] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect a local run and its evidence."""
    settings = Settings()
    store = RunStore(settings.resolved_database_path, settings.data_dir)
    try:
        state = store.get_run(run_id) if run_id else store.latest_run()
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if state is None:
        raise typer.BadParameter("no runs found")
    if as_json:
        console.print_json(state.model_dump_json())
        return
    table = Table(title=f"CIHive run {state.run.id}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Repository", state.run.repo)
    table.add_row("Workflow", state.run.workflow_name)
    table.add_row("Status", state.status.value)
    table.add_row(
        "Failure signature", state.failure.signature.fingerprint if state.failure else "-"
    )
    table.add_row("Plan", state.plan.summary if state.plan else "-")
    table.add_row("Decision", state.decision.reason if state.decision else "-")
    table.add_row("Draft PR", state.decision.draft_pr_url if state.decision else "-")
    table.add_row("Error", state.error or "-")
    console.print(table)


@app.command()
def replay(trace_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Replay the deterministic decision summary from a JSONL trace."""
    events: list[dict[str, object]] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    table = Table(title=f"Trace {trace_path.name}")
    table.add_column("Time")
    table.add_column("Event")
    table.add_column("Summary")
    for item in events:
        payload = item.get("payload")
        summary = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else ""
        table.add_row(str(item.get("at", "")), str(item.get("event", "")), summary[:180])
    console.print(table)


@app.command("eval")
def evaluate(
    suite: Annotated[Path, typer.Option("--suite")] = Path("evals/cases.yml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run the fixed local repair evaluation suite."""
    report = run_evaluation_suite(suite)
    if as_json:
        console.print_json(data=report.model_dump(mode="json"))
    else:
        table = Table(title="CIHive evaluation")
        table.add_column("Metric")
        table.add_column("Value")
        for key, value in report.model_dump(mode="json").items():
            table.add_row(key, str(value))
        console.print(table)
    if report.expected_outcomes_met != report.cases:
        raise typer.Exit(code=1)


@app.command()
def worker(
    assignment_id: Annotated[str, typer.Option("--assignment-id")],
    token: Annotated[str, typer.Option("--token", hide_input=True)],
    root: Annotated[Path, typer.Option("--root")] = Path("."),
) -> None:
    """Run one repair assignment inside a read-only Actions checkout."""
    settings = Settings()
    try:
        candidate = asyncio.run(run_worker(settings, root.resolve(), assignment_id, token))
    except Exception as exc:
        console.print(f"[red]worker failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"submitted candidate {candidate.id}")


@app.command()
def verify(
    assignment_id: Annotated[str, typer.Option("--assignment-id")],
    token: Annotated[str, typer.Option("--token", hide_input=True)],
    root: Annotated[Path, typer.Option("--root")] = Path("."),
) -> None:
    """Reproduce a failure and verify every submitted candidate."""
    settings = Settings()
    try:
        result = asyncio.run(run_verifier(settings, root.resolve(), assignment_id, token))
    except Exception as exc:
        console.print(f"[red]verification failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)


if __name__ == "__main__":
    app()

"""Command-line entry point for core, framework-neutral operations."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from agent_lab.bundle_validation import (
    render_validation,
    validate_bundle,
    validate_pair,
)
from agent_lab.config import load_experiment, load_model_profile
from agent_lab.scenario import generate_incident

app = typer.Typer(no_args_is_help=True, help="Agent Framework Field Lab.")
scenario_app = typer.Typer(no_args_is_help=True, help="Generate and inspect scenarios.")
config_app = typer.Typer(no_args_is_help=True, help="Validate experiment configuration.")
bundle_app = typer.Typer(no_args_is_help=True, help="Validate run-bundle provenance.")
app.add_typer(scenario_app, name="scenario")
app.add_typer(config_app, name="config")
app.add_typer(bundle_app, name="bundle")
console = Console()


@scenario_app.command("generate")
def generate_scenario(
    seed: Annotated[int, typer.Option(help="Deterministic world seed.")] = 42,
    include_ground_truth: Annotated[
        bool,
        typer.Option(help="Include evaluator-only fields. Never pass this output to an agent."),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional JSON destination."),
    ] = None,
) -> None:
    """Generate one deterministic incident case."""

    incident = generate_incident(seed)
    body = incident.model_dump(mode="json") if include_ground_truth else incident.public_view()
    rendered = json.dumps(body, indent=2, sort_keys=True) + "\n"
    if output is None:
        console.print_json(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    console.print(f"Wrote {output}")


@config_app.command("validate")
def validate_config(path: Path) -> None:
    """Validate an experiment or model TOML profile."""

    try:
        if "models" in path.parts:
            profile = load_model_profile(path)
            profile_type = "model"
        else:
            profile = load_experiment(path)
            profile_type = "experiment"
    except (OSError, ValidationError, ValueError) as exc:
        console.print(f"[red]Invalid configuration:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Valid {profile_type} profile[/green]: {profile.model_dump_json(exclude_none=True)}"
    )


@bundle_app.command("validate")
def validate_run_bundle(
    path: Path,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
    no_mark: Annotated[
        bool,
        typer.Option(help="Do not write validation.json into the bundle."),
    ] = False,
) -> None:
    """Validate one run bundle and mark its inference eligibility."""

    result = validate_bundle(path, mark=not no_mark)
    if json_output:
        console.print_json(result.model_dump_json())
    else:
        console.print(render_validation(result))
    if not result.valid:
        raise typer.Exit(code=1)


@bundle_app.command("validate-pair")
def validate_run_pair(
    left: Path,
    right: Path,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
    no_mark: Annotated[
        bool,
        typer.Option(help="Do not write pair validation sidecars."),
    ] = False,
) -> None:
    """Validate a matched explicit-workflow or harness pair."""

    result = validate_pair(left, right, mark=not no_mark)
    if json_output:
        console.print_json(result.model_dump_json())
    else:
        console.print(render_validation(result))
    if not result.valid:
        raise typer.Exit(code=1)


@app.command()
def doctor() -> None:
    """Report local prerequisites without mutating the machine."""

    table = Table(title="Agent Lab prerequisites")
    table.add_column("Capability")
    table.add_column("Status")
    table.add_column("Details")

    tools = ("uv", "docker", "nvidia-smi", "git", "gh")
    for tool in tools:
        path = shutil.which(tool)
        table.add_row(tool, "found" if path else "missing", path or "not on PATH")

    table.add_row("python", "found", platform.python_version())

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        table.add_row(
            "gpu",
            "available" if result.returncode == 0 else "error",
            result.stdout.strip() or result.stderr.strip(),
        )

    console.print(table)


if __name__ == "__main__":
    app()

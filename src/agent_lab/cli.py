"""Command-line entry point for core, framework-neutral operations."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
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
from agent_lab.report_builder import build_paired_report, write_paired_report
from agent_lab.scenario import generate_incident

_DEFAULT_REPOSITORY_ROOT = Path.cwd()

app = typer.Typer(no_args_is_help=True, help="Agent Framework Field Lab.")
scenario_app = typer.Typer(no_args_is_help=True, help="Generate and inspect scenarios.")
config_app = typer.Typer(no_args_is_help=True, help="Validate experiment configuration.")
bundle_app = typer.Typer(no_args_is_help=True, help="Validate run-bundle provenance.")
experiment_app = typer.Typer(
    no_args_is_help=True,
    help="Launch framework-neutral experiment control-plane operations.",
)
report_app = typer.Typer(no_args_is_help=True, help="Build offline paired reports.")
observer_app = typer.Typer(no_args_is_help=True, help="Observe paired runs live or by replay.")
app.add_typer(scenario_app, name="scenario")
app.add_typer(config_app, name="config")
app.add_typer(bundle_app, name="bundle")
app.add_typer(experiment_app, name="experiment")
app.add_typer(report_app, name="report")
app.add_typer(observer_app, name="observer")
console = Console()


@observer_app.command("serve")
def serve_observer(
    runs_dir: Annotated[
        Path,
        typer.Option(help="Run-bundle and native-workflow index directory."),
    ] = Path("runs"),
    host: Annotated[
        str,
        typer.Option(help="Loopback interface; non-loopback binds are rejected."),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8787,
    credential_file: Annotated[
        Path | None,
        typer.Option(help="Owner-only approval credential file."),
    ] = None,
    operator_id: Annotated[
        str,
        typer.Option(help="Audited local identity attached to approval decisions."),
    ] = "local-observer-operator",
    read_only: Annotated[
        bool,
        typer.Option(help="Disable approval controls and serve only replay/live reads."),
    ] = False,
) -> None:
    """Serve the framework-neutral browser and SSE API on loopback."""

    from agent_lab.observer import build_observer_app, load_or_create_observer_token

    if host != "127.0.0.1":
        console.print("[red]Observer host must be 127.0.0.1.[/red]")
        raise typer.Exit(code=1)
    resolved_runs = runs_dir.resolve()
    token = None
    token_path = None
    if not read_only:
        token_path = (
            credential_file.resolve()
            if credential_file is not None
            else resolved_runs / ".observer-token"
        )
        try:
            token = load_or_create_observer_token(token_path)
        except (OSError, ValueError) as exc:
            console.print(f"[red]Cannot load observer credential:[/red] {exc}")
            raise typer.Exit(code=1) from exc
    app_instance = build_observer_app(
        resolved_runs,
        approval_token=token,
        operator_id=operator_id,
    )
    if token_path is not None:
        console.print(f"Approval credential: {token_path}")
    console.print(f"Observer: http://{host}:{port}")
    import uvicorn

    uvicorn.run(app_instance, host=host, port=port, log_level="info")


@observer_app.command("render")
def render_observer_pair(
    pair_id: Annotated[str, typer.Argument(help="Opaque pair ID returned by /api/pairs.")],
    base_url: Annotated[
        str,
        typer.Option(help="Loopback observer URL."),
    ] = "http://127.0.0.1:8787",
    watch: Annotated[
        bool,
        typer.Option(help="Refresh the headless rendering until both lanes are terminal."),
    ] = False,
    interval_seconds: Annotated[
        float,
        typer.Option(min=0.1, max=60),
    ] = 1.0,
) -> None:
    """Render the same sanitized pair API consumed by the browser UI."""

    from agent_lab.observer import fetch_pair_snapshot, render_pair_snapshot

    while True:
        try:
            snapshot = fetch_pair_snapshot(base_url, pair_id)
            render_pair_snapshot(snapshot, console=console)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            console.print(f"[red]Observer read failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        if not watch or snapshot.get("complete") is True:
            return
        time.sleep(interval_seconds)


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
    """Validate a matched explicit-workflow, harness, or single-agent pair."""

    result = validate_pair(left, right, mark=not no_mark)
    if json_output:
        console.print_json(result.model_dump_json())
    else:
        console.print(render_validation(result))
    if not result.valid:
        raise typer.Exit(code=1)


@experiment_app.command("probe")
def run_experiment_probe(
    experiment: Annotated[
        Path,
        typer.Option(
            "--experiment",
            "-e",
            help="Experiment profile to resolve.",
        ),
    ] = Path("configs/experiments/smoke.toml"),
    runs_dir: Annotated[
        Path,
        typer.Option(help="Destination for isolated protocol-probe bundles."),
    ] = Path("runs"),
    order_seed: Annotated[
        int,
        typer.Option(help="Recorded seed for deterministic lane ordering."),
    ] = 1729,
    repository_root: Annotated[
        Path,
        typer.Option(help="Repository root containing runner/service projects."),
    ] = _DEFAULT_REPOSITORY_ROOT,
) -> None:
    """Run real native process lifecycle probes without invoking a model."""

    uv = shutil.which("uv")
    if uv is None:
        console.print("[red]Cannot run experiment probe: uv is not on PATH.[/red]")
        raise typer.Exit(code=1)
    root = repository_root.resolve()
    command = [
        uv,
        "run",
        "--project",
        str(root / "services/incident_mcp"),
        "--no-sync",
        "python",
        "-m",
        "incident_lab_mcp.experiment_control",
        "--repository-root",
        str(root),
        "--experiment",
        str(experiment),
        "--runs-dir",
        str(runs_dir),
        "--order-seed",
        str(order_seed),
    ]
    result = subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stderr:
            console.print(result.stderr.rstrip())
        if result.stdout:
            console.print(result.stdout.rstrip())
        raise typer.Exit(code=result.returncode)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        console.print("[red]Experiment probe returned invalid JSON.[/red]")
        if result.stderr:
            console.print(result.stderr.rstrip())
        raise typer.Exit(code=1) from exc
    console.print_json(data=payload)


@experiment_app.command("plan")
def plan_experiment(
    experiment: Annotated[
        Path,
        typer.Option(
            "--experiment",
            "-e",
            help="Experiment profile to resolve without starting services.",
        ),
    ] = Path("configs/experiments/smoke.toml"),
    order_seed: Annotated[
        int,
        typer.Option(help="Recorded seed for deterministic lane ordering."),
    ] = 1729,
    repository_root: Annotated[
        Path,
        typer.Option(help="Repository root containing runner/service projects."),
    ] = _DEFAULT_REPOSITORY_ROOT,
) -> None:
    """Validate one profile through the execution planner without running it."""

    uv = shutil.which("uv")
    if uv is None:
        console.print("[red]Cannot plan experiment: uv is not on PATH.[/red]")
        raise typer.Exit(code=1)
    root = repository_root.resolve()
    command = [
        uv,
        "run",
        "--project",
        str(root / "services/incident_mcp"),
        "--no-sync",
        "python",
        "-m",
        "incident_lab_mcp.experiment_control",
        "--repository-root",
        str(root),
        "--experiment",
        str(experiment),
        "--order-seed",
        str(order_seed),
        "--mode",
        "plan",
    ]
    result = subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stderr:
            console.print(result.stderr.rstrip())
        if result.stdout:
            console.print(result.stdout.rstrip())
        raise typer.Exit(code=result.returncode)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        console.print("[red]Experiment planner returned invalid JSON.[/red]")
        if result.stderr:
            console.print(result.stderr.rstrip())
        raise typer.Exit(code=1) from exc
    console.print_json(data=payload)


@experiment_app.command("run")
def run_native_workflow_experiment(
    experiment: Annotated[
        Path,
        typer.Option(
            "--experiment",
            "-e",
            help="Matched native-framework experiment profile to execute.",
        ),
    ] = Path("configs/experiments/explicit-smoke.toml"),
    runs_dir: Annotated[
        Path,
        typer.Option(help="Destination for persistent scored run bundles."),
    ] = Path("runs"),
    order_seed: Annotated[
        int,
        typer.Option(help="Recorded seed for deterministic lane ordering."),
    ] = 1729,
    repository_root: Annotated[
        Path,
        typer.Option(help="Repository root containing runner/service projects."),
    ] = _DEFAULT_REPOSITORY_ROOT,
) -> None:
    """Run one matched native-framework track and persist scored paired bundles."""

    uv = shutil.which("uv")
    if uv is None:
        console.print("[red]Cannot run experiment: uv is not on PATH.[/red]")
        raise typer.Exit(code=1)
    root = repository_root.resolve()
    command = [
        uv,
        "run",
        "--project",
        str(root / "services/incident_mcp"),
        "--no-sync",
        "python",
        "-m",
        "incident_lab_mcp.experiment_control",
        "--repository-root",
        str(root),
        "--experiment",
        str(experiment),
        "--runs-dir",
        str(runs_dir),
        "--order-seed",
        str(order_seed),
        "--mode",
        "native_workflow",
    ]
    result = subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stderr:
            console.print(result.stderr.rstrip())
        if result.stdout:
            console.print(result.stdout.rstrip())
        raise typer.Exit(code=result.returncode)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        console.print("[red]Experiment run returned invalid JSON.[/red]")
        if result.stderr:
            console.print(result.stderr.rstrip())
        raise typer.Exit(code=1) from exc
    console.print_json(data=payload)


@report_app.command("build")
def build_report(
    indices: Annotated[
        list[Path],
        typer.Option(
            "--index",
            "-i",
            help="Native-workflow experiment index; repeat for multiple tracks.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", "-o", help="New report output directory."),
    ] = Path("reports/latest"),
    bootstrap_seed: Annotated[
        int,
        typer.Option(help="Deterministic paired-bootstrap seed."),
    ] = 1729,
    bootstrap_resamples: Annotated[
        int,
        typer.Option(help="Number of paired-bootstrap resamples."),
    ] = 10_000,
) -> None:
    """Revalidate immutable pairs and produce JSON plus self-contained HTML."""

    try:
        report = build_paired_report(
            indices,
            bootstrap_seed=bootstrap_seed,
            bootstrap_resamples=bootstrap_resamples,
        )
        json_path, html_path = write_paired_report(report, output_dir)
    except (OSError, ValidationError, ValueError) as exc:
        console.print(f"[red]Could not build report:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Wrote {json_path}")
    console.print(f"Wrote {html_path}")


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

"""Cross-framework OpenAI-compatible model wire and tool-call suite."""

from __future__ import annotations

import argparse
import json
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_lab.model_compat_contracts import MODEL_NAME, SUCCESS_FIXTURES, ModelFixture
from agent_lab.model_compat_stub import CompatibilityStub

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_RUNNERS = {
    "maf": ("runners/maf", "maf_runner.model_compat"),
    "langchain": ("runners/langgraph", "langgraph_runner.model_compat"),
}


def _run_native(framework: str, base_url: str, model: str) -> dict[str, Any]:
    project, module = _RUNNERS[framework]
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / project),
            "--no-sync",
            "python",
            "-m",
            module,
            "--base-url",
            base_url,
            "--model",
            model,
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{framework} compatibility runner exited {result.returncode}: {result.stderr.strip()}"
        )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{framework} compatibility runner returned invalid JSON: {result.stdout!r}"
        ) from exc
    report["diagnostics"] = {
        "stderr": [line for line in result.stderr.splitlines() if "VIRTUAL_ENV=" not in line]
    }
    return report


def _compare(
    reports: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    protocol_stub: bool,
) -> list[str]:
    errors: list[str] = []
    if reports["maf"]["settings"] != reports["langchain"]["settings"]:
        errors.append("native runners did not use identical explicit request settings")
    for fixture in SUCCESS_FIXTURES:
        results = {
            framework: report["fixtures"][fixture.value] for framework, report in reports.items()
        }
        for framework, result in results.items():
            if result["status"] != "passed":
                errors.append(
                    f"{framework}/{fixture.value} was {result['status']}: {result.get('error')}"
                )
        if protocol_stub and results["maf"].get("value") != results["langchain"].get("value"):
            errors.append(f"semantic result mismatch for {fixture.value}")
    if protocol_stub:
        for framework, report in reports.items():
            malformed = report["fixtures"][ModelFixture.MALFORMED_TOOL.value]
            if malformed.get("failure_class") != "model_parser":
                errors.append(f"{framework}/malformed_tool was not classified as model_parser")
        expected_counts = {fixture.value: 1 for fixture in ModelFixture}
        expected_counts[ModelFixture.MULTI_TURN.value] = 2
        for framework in reports:
            actual = {
                fixture: sum(
                    1
                    for record in records
                    if record["framework"] == framework and record["fixture"] == fixture
                )
                for fixture in expected_counts
            }
            if actual != expected_counts:
                errors.append(
                    f"{framework} request coverage mismatch: "
                    f"expected {expected_counts}, got {actual}"
                )
        for record in records:
            if record["validation_errors"]:
                errors.append(
                    f"{record['framework']}/{record['fixture']} wire validation failed: "
                    + "; ".join(record["validation_errors"])
                )
            for field in (
                "wire_request_sha256",
                "effective_request_sha256",
                "wire_response_sha256",
                "request_shape",
                "response_shape",
                "server_default_expansion",
            ):
                if field not in record:
                    errors.append(
                        f"{record['framework']}/{record['fixture']} missing audit field {field}"
                    )
    return errors


def run_suite(*, base_url: str | None = None, model: str = MODEL_NAME) -> dict[str, Any]:
    """Run both native clients against the stub or a supplied local vLLM endpoint."""

    stub_context = CompatibilityStub() if base_url is None else nullcontext(None)
    with stub_context as stub:
        effective_base_url = stub.base_url if stub is not None else str(base_url)
        reports = {
            framework: _run_native(framework, effective_base_url, model) for framework in _RUNNERS
        }
        records = stub.records if stub is not None else []
    for framework, report in reports.items():
        report["wire_exchanges"] = [
            record for record in records if record["framework"] == framework
        ]
    errors = _compare(reports, records, protocol_stub=stub is not None)
    return {
        "schema_version": "model-compatibility/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "protocol_stub" if stub is not None else "live_vllm",
        "protocol": "openai/chat-completions",
        "model": model,
        "passed": not errors,
        "errors": errors,
        "failure_classes": {
            "model_parser": "provider output could not be decoded into the advertised call/schema",
            "client_serialization": "native client could not construct or serialize the request",
            "framework_loop": "native tool loop failed after a valid client response",
            "transport": "endpoint connection, timeout, or HTTP execution failed",
        },
        "wire_capture_gap": (
            None
            if stub is not None
            else "live endpoint mode requires server-side vLLM logging for wire/default evidence"
        ),
        "frameworks": reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Existing loopback vLLM /v1 URL; omit for protocol stub")
    parser.add_argument("--model", default=MODEL_NAME)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_suite(base_url=args.base_url, model=args.model)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()

"""Cross-framework OpenAI-compatible model wire and tool-call suite."""

from __future__ import annotations

import argparse
import json
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from agent_lab.config import load_model_profile
from agent_lab.contracts import ModelProfile
from agent_lab.model_compat_contracts import (
    MODEL_ACTOR_PROBES,
    MODEL_NAME,
    SUCCESS_FIXTURES,
    ModelFixture,
    compatibility_model_profile,
)
from agent_lab.model_compat_stub import CompatibilityStub

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_RUNNERS = {
    "maf": ("runners/maf", "maf_runner.model_compat"),
    "langchain": ("runners/langgraph", "langgraph_runner.model_compat"),
}

ServerKind = Literal["openai_compatible", "llama_cpp", "vllm"]


def _validate_loopback_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("model compatibility endpoint must be an HTTP loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "model compatibility endpoint cannot contain credentials, query, or fragment"
        )
    if parsed.path.rstrip("/") != "/v1":
        raise ValueError("model compatibility endpoint path must be /v1")


def _advertised_model_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("model inventory is not an OpenAI-compatible list response")
    model_ids = sorted(
        {
            item["id"]
            for item in payload["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    )
    if not model_ids:
        raise ValueError("model inventory advertised no model identifiers")
    return model_ids


def _endpoint_inventory(base_url: str, requested_model: str) -> dict[str, Any]:
    models_url = f"{base_url.rstrip('/')}/models"
    request = Request(models_url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310 - loopback endpoint
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError(
            f"could not verify the live endpoint model at {models_url}: {exc}"
        ) from exc
    model_ids = _advertised_model_ids(payload)
    if requested_model not in model_ids:
        raise RuntimeError(
            "refusing to run against an unexpected model: "
            f"requested {requested_model!r}, endpoint advertised {model_ids!r}"
        )
    return {
        "models_url": models_url,
        "advertised_models": model_ids,
        "requested_model_advertised": True,
    }


def _run_native(framework: str, profile: ModelProfile) -> dict[str, Any]:
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
            "--profile-json",
            profile.model_dump_json(),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=max(60, int(profile.request_timeout_seconds * 10)),
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
    for framework, report in reports.items():
        probes = report.get("actor_probes", {})
        if set(probes) != set(MODEL_ACTOR_PROBES):
            errors.append(f"{framework} actor wire-probe coverage is incomplete")
        for actor, result in probes.items():
            if result["status"] != "passed":
                errors.append(f"{framework}/{actor} actor wire probe failed")
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
        expected_counts[ModelFixture.PLAIN.value] += len(MODEL_ACTOR_PROBES)
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
        for actor in MODEL_ACTOR_PROBES:
            actor_records = {
                framework: [
                    record
                    for record in records
                    if record["framework"] == framework and record.get("actor") == actor
                ]
                for framework in reports
            }
            if any(len(values) != 1 for values in actor_records.values()):
                errors.append(f"actor wire-probe count mismatch for {actor}")
                continue
            hashes = {values[0]["effective_request_sha256"] for values in actor_records.values()}
            if len(hashes) != 1:
                errors.append(f"matched effective request differs for actor {actor}")
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


def run_suite(
    *,
    base_url: str | None = None,
    model: str = MODEL_NAME,
    server_kind: ServerKind = "openai_compatible",
    profile: ModelProfile | None = None,
) -> dict[str, Any]:
    """Run both native clients against the stub or a supplied loopback endpoint."""

    stub_context = CompatibilityStub() if base_url is None else nullcontext(None)
    if base_url is not None:
        _validate_loopback_base_url(base_url)
    with stub_context as stub:
        effective_base_url = stub.base_url if stub is not None else str(base_url)
        endpoint_inventory = (
            None if stub is not None else _endpoint_inventory(effective_base_url, model)
        )
        effective_profile = (
            compatibility_model_profile(effective_base_url, model)
            if profile is None
            else profile.model_copy(update={"base_url": effective_base_url, "name": model})
        )
        reports = {framework: _run_native(framework, effective_profile) for framework in _RUNNERS}
        records = stub.records if stub is not None else []
    for framework, report in reports.items():
        report["wire_exchanges"] = [
            record for record in records if record["framework"] == framework
        ]
    errors = _compare(reports, records, protocol_stub=stub is not None)
    return {
        "schema_version": "model-compatibility/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "protocol_stub" if stub is not None else f"live_{server_kind}",
        "protocol": "openai/chat-completions",
        "model": model,
        "server": (
            None if stub is not None else {"kind": server_kind, "inventory": endpoint_inventory}
        ),
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
            else "live endpoint mode requires server-side request logging for wire/default evidence"
        ),
        "frameworks": reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", help="Existing loopback OpenAI-compatible /v1 URL; omit for protocol stub"
    )
    parser.add_argument("--model")
    parser.add_argument("--model-profile", type=Path)
    parser.add_argument(
        "--server-kind",
        choices=("openai_compatible", "llama_cpp", "vllm"),
        default="openai_compatible",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    profile = load_model_profile(args.model_profile) if args.model_profile else None
    base_url = profile.base_url if profile is not None and args.base_url is None else args.base_url
    model = args.model or (profile.name if profile is not None else MODEL_NAME)
    report = run_suite(
        base_url=base_url,
        model=model,
        server_kind=args.server_kind,
        profile=profile,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()

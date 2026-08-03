"""LangGraph runner process entry point for the shared lifecycle protocol."""

from __future__ import annotations

import argparse
import asyncio
import json
from importlib.metadata import version

from agent_lab.contract_bundle import contract_bundle_hashes
from agent_lab.contracts import FrameworkLane
from agent_lab.matched_contracts import normalized_topology_for_lane
from agent_lab.runner_host import (
    ApprovalProvider,
    NativeRunnerDescriptor,
    descriptor_payload,
    run_protocol_probe,
    run_workflow_protocol,
)
from agent_lab.runner_protocol import CheckpointCapability, RunRequest
from agent_lab.workflow_telemetry import WorkflowTelemetry


def _instrument_langchain(
    tracer_provider: object | None,
    enabled: bool,
    capture_sensitive: bool,
):
    if not enabled:
        return None

    from openinference.instrumentation import TraceConfig
    from openinference.instrumentation.langchain import LangChainInstrumentor

    instrumentor = LangChainInstrumentor()
    hide_sensitive = not capture_sensitive
    instrumentor.instrument(
        tracer_provider=tracer_provider,
        config=TraceConfig(
            hide_llm_invocation_parameters=hide_sensitive,
            hide_llm_tools=hide_sensitive,
            hide_inputs=hide_sensitive,
            hide_outputs=hide_sensitive,
            hide_input_messages=hide_sensitive,
            hide_output_messages=hide_sensitive,
            hide_input_images=hide_sensitive,
            hide_input_text=hide_sensitive,
            hide_output_text=hide_sensitive,
            hide_embedding_vectors=True,
            hide_embeddings_vectors=True,
            hide_embeddings_text=hide_sensitive,
            hide_prompts=hide_sensitive,
            hide_choices=hide_sensitive,
        ),
    )
    return instrumentor.uninstrument


async def _execute_workflow(
    request: RunRequest,
    approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, object]:
    if request.manifest.lane is FrameworkLane.LANGGRAPH:
        from langgraph_runner.workflow_lane import run_live_approved
    elif request.manifest.lane is FrameworkLane.DEEP_AGENTS:
        from langgraph_runner.harness_lane import run_live_approved
    elif request.manifest.lane is FrameworkLane.LANGCHAIN_SINGLE_AGENT:
        from langgraph_runner.baseline_lane import run_live_approved
    else:
        raise ValueError(f"unsupported LangGraph-family lane {request.manifest.lane.value}")

    return await run_live_approved(request, approval_provider, telemetry)


def descriptor() -> NativeRunnerDescriptor:
    return NativeRunnerDescriptor(
        name="langgraph-runner",
        supported_lanes=(
            FrameworkLane.LANGGRAPH,
            FrameworkLane.DEEP_AGENTS,
            FrameworkLane.LANGCHAIN_SINGLE_AGENT,
        ),
        framework_versions={
            "deepagents": version("deepagents"),
            "langchain": version("langchain"),
            "langchain-mcp-adapters": version("langchain-mcp-adapters"),
            "langchain-openai": version("langchain-openai"),
            "langgraph": version("langgraph"),
        },
        contract_hashes=contract_bundle_hashes(),
        topology={
            FrameworkLane.LANGGRAPH: {
                "kind": "explicit_workflow",
                "runtime": "langgraph",
                "nodes": [
                    "telemetry_investigator",
                    "change_investigator",
                    "reliability_reviewer",
                    "evidence_join",
                    "incident_commander",
                    "approval",
                    "mitigation",
                    "recovery_verification",
                ],
                "parallel_fanout": True,
            },
            FrameworkLane.DEEP_AGENTS: {
                "kind": "opinionated_harness",
                "runtime": "deep_agents",
                "coordinator": "incident_commander",
                "subagents": [
                    "telemetry_investigator",
                    "change_investigator",
                    "reliability_reviewer",
                ],
            },
            FrameworkLane.LANGCHAIN_SINGLE_AGENT: {
                "kind": "single_agent_baseline",
                "runtime": "langchain_create_agent",
                "agent": "incident_commander",
                "delegation": False,
                "source_capabilities": [
                    "telemetry_investigator",
                    "change_investigator",
                    "reliability_reviewer",
                ],
            },
        },
        normalized_topology={
            FrameworkLane.LANGGRAPH: normalized_topology_for_lane(FrameworkLane.LANGGRAPH),
            FrameworkLane.DEEP_AGENTS: normalized_topology_for_lane(FrameworkLane.DEEP_AGENTS),
            FrameworkLane.LANGCHAIN_SINGLE_AGENT: normalized_topology_for_lane(
                FrameworkLane.LANGCHAIN_SINGLE_AGENT
            ),
        },
        checkpoint={
            FrameworkLane.LANGGRAPH: CheckpointCapability(
                resumable=True,
                restart_safe=True,
                format="langgraph-checkpoint-v4+agent-lab-json-envelope/v1",
            ),
            FrameworkLane.DEEP_AGENTS: CheckpointCapability(
                resumable=False,
                restart_safe=False,
                format="deep-agents-process-local/v1",
            ),
            FrameworkLane.LANGCHAIN_SINGLE_AGENT: CheckpointCapability(
                resumable=False,
                restart_safe=False,
                format="langchain-create-agent-process-local/v1",
            ),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--describe", action="store_true")
    mode.add_argument("--protocol-probe", action="store_true")
    mode.add_argument("--run", action="store_true")
    args = parser.parse_args()

    runner = descriptor()
    if args.describe:
        print(json.dumps(descriptor_payload(runner), sort_keys=True))
        return
    if args.run:
        raise SystemExit(
            asyncio.run(
                run_workflow_protocol(
                    runner,
                    _execute_workflow,
                    instrument_framework=_instrument_langchain,
                )
            )
        )
    raise SystemExit(run_protocol_probe(runner, instrument_framework=_instrument_langchain))


if __name__ == "__main__":
    main()

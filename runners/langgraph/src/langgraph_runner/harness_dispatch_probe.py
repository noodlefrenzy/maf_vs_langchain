"""Hermetic probe for Deep Agents Harness per-dispatch model accounting."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from deepagents import SubAgent, create_deep_agent
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import Field

from agent_lab.model_compat_contracts import (
    MODEL_NAME,
    ModelFixture,
    compatibility_model_profile,
    fixture_prompt,
)
from langgraph_runner.chat_telemetry import InstrumentedChatOpenAI
from langgraph_runner.harness_lane import (
    _phase_model_call_limit_middleware,
    _SpecialistCompletionMiddleware,
    _SubagentTaskTracker,
)


@tool(description="Look up an incident by identifier.")
def lookup_incident(incident_id: str) -> str:
    return f"{incident_id} is stable"


class _RecordingTelemetry:
    def __init__(self, model_call_limit: int | None = None) -> None:
        self.model_call_limit = model_call_limit
        self.model_starts: list[dict[str, str]] = []
        self.model_finishes: list[dict[str, str | int]] = []

    async def handoff(self, **_: Any) -> None:
        return None

    async def model_started(self, *, actor: str, model: str) -> None:
        if self.model_call_limit is not None and len(self.model_starts) >= self.model_call_limit:
            raise RuntimeError("model_call_budget_exhausted")
        self.model_starts.append({"actor": actor, "model": model})

    async def model_finished(
        self,
        *,
        actor: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        parser_outcome: str = "provider_response_accepted",
        provider_request_id: str | None = None,
        queue_seconds: float | None = None,
        time_to_first_token_seconds: float | None = None,
        generation_seconds: float | None = None,
    ) -> None:
        self.model_finishes.append(
            {
                "actor": actor,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "reasoning_tokens": reasoning_tokens,
                "parser_outcome": parser_outcome,
                "provider_request_id": provider_request_id,
                "queue_seconds": queue_seconds,
                "time_to_first_token_seconds": time_to_first_token_seconds,
                "generation_seconds": generation_seconds,
            }
        )

    async def tool_started(self, **_: Any) -> None:
        return None

    async def tool_finished(self, **_: Any) -> None:
        return None


class _DelegatingFakeModel(BaseChatModel):
    """Issue one native Deep Agents task call."""

    dispatches: int = 0

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> BaseChatModel:
        del tools, tool_choice, kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        self.dispatches += 1
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {
                        "description": "Keep checking the incident until it is stable.",
                        "subagent_type": "looping_specialist",
                    },
                    "id": "call_delegate",
                    "type": "tool_call",
                }
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self) -> str:
        return "delegating-fake-model"


class _LoopingFakeModel(BaseChatModel):
    """Request another tool call after every tool result."""

    dispatches: int = 0

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> BaseChatModel:
        del tools, tool_choice, kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        self.dispatches += 1
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "looping_lookup",
                    "args": {"incident_id": "INC-42"},
                    "id": f"call_loop_{self.dispatches}",
                    "type": "tool_call",
                }
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self) -> str:
        return "looping-fake-model"


class _CompletingDelegatingFakeModel(_DelegatingFakeModel):
    """Delegate once, then finish after the specialist returns."""

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.dispatches == 0:
            return super()._generate(messages, stop, run_manager, **kwargs)
        del messages, stop, run_manager, kwargs
        self.dispatches += 1
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="delegation complete"))]
        )


class _ToolAwareLoopingFakeModel(BaseChatModel):
    """Loop while tools are advertised, then honor the tool-disabled completion turn."""

    tools_enabled: bool = False
    dispatch_counter: list[int] = Field(default_factory=lambda: [0])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> BaseChatModel:
        del tool_choice, kwargs
        return self.model_copy(update={"tools_enabled": bool(tools)})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        self.dispatch_counter[0] += 1
        if self.tools_enabled:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "looping_lookup",
                        "args": {"incident_id": "INC-42"},
                        "id": f"call_loop_{self.dispatch_counter[0]}",
                        "type": "tool_call",
                    }
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=message)])
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content='{"status":"complete"}'))]
        )

    @property
    def _llm_type(self) -> str:
        return "tool-aware-looping-fake-model"


async def _probe_subagent_phase_limit() -> dict[str, Any]:
    tool_calls = 0

    @tool(description="Look up an incident and keep the specialist loop active.")
    def looping_lookup(incident_id: str) -> str:
        nonlocal tool_calls
        tool_calls += 1
        return f"{incident_id} still requires another check"

    parent_model = _DelegatingFakeModel()
    specialist_model = _LoopingFakeModel()
    graph = create_deep_agent(
        model=parent_model,
        system_prompt="Delegate the requested investigation.",
        subagents=[
            SubAgent(
                name="looping_specialist",
                description="Repeatedly checks an incident.",
                system_prompt="Keep checking the incident with the supplied tool.",
                tools=[looping_lookup],
                model=specialist_model,
                middleware=[_phase_model_call_limit_middleware()],
            )
        ],
        name="subagent_limit_probe",
    )
    error = None
    try:
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "Investigate INC-42."}]},
            config={"recursion_limit": 50},
        )
    except ModelCallLimitExceededError as exc:
        error = type(exc).__name__
    return {
        "error": error,
        "parent_dispatches": parent_model.dispatches,
        "specialist_dispatches": specialist_model.dispatches,
        "tool_calls": tool_calls,
    }


async def _probe_subagent_completion_boundary() -> dict[str, Any]:
    tool_calls = 0

    @tool(description="Look up an incident until the completion boundary closes tools.")
    def looping_lookup(incident_id: str) -> str:
        nonlocal tool_calls
        tool_calls += 1
        return f"{incident_id} evidence captured"

    parent_model = _CompletingDelegatingFakeModel()
    specialist_model = _ToolAwareLoopingFakeModel()
    graph = create_deep_agent(
        model=parent_model,
        system_prompt="Delegate the requested investigation.",
        subagents=[
            SubAgent(
                name="looping_specialist",
                description="Checks an incident until tools are disabled.",
                system_prompt="Use tools while available, then return a final result.",
                tools=[looping_lookup],
                model=specialist_model,
                middleware=[
                    _phase_model_call_limit_middleware(),
                    _SpecialistCompletionMiddleware(),
                ],
            )
        ],
        name="subagent_completion_probe",
    )
    state = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "Investigate INC-42."}]},
        config={"recursion_limit": 50},
    )
    return {
        "parent_dispatches": parent_model.dispatches,
        "specialist_dispatches": specialist_model.dispatch_counter[0],
        "tool_calls": tool_calls,
        "text": state["messages"][-1].text,
    }


async def _probe_subagent_task_drain() -> dict[str, Any]:
    tracker = _SubagentTaskTracker()
    started = asyncio.Event()
    release = asyncio.Event()

    class _Request:
        tool_call = {"name": "task"}

    async def handler(_: Any) -> str:
        started.set()
        await release.wait()
        return "finished"

    task = asyncio.create_task(tracker.awrap_tool_call(_Request(), handler))
    await started.wait()
    drain = asyncio.create_task(tracker.cancel_and_drain())
    try:
        result = await task
    except asyncio.CancelledError:
        result = None
    await drain
    return {"cancelled": task.cancelled(), "result": result}


async def run(base_url: str, model_call_limit: int | None = None) -> dict[str, Any]:
    telemetry = _RecordingTelemetry(model_call_limit)
    profile = compatibility_model_profile(base_url, MODEL_NAME)
    model = InstrumentedChatOpenAI(
        actor="dispatch_probe",
        profile=profile,
        telemetry=telemetry,
        framework="deep-agents-probe",
    )
    graph = create_deep_agent(
        model=model,
        tools=[lookup_incident],
        system_prompt="Use the supplied incident tool when the user requests it.",
        name="dispatch_probe",
    )
    text = None
    stream_text = None
    error = None
    try:
        state = await graph.ainvoke(
            {"messages": [{"role": "user", "content": fixture_prompt(ModelFixture.MULTI_TURN)}]}
        )
        text = state["messages"][-1].text
        chunks = [chunk async for chunk in model.astream(fixture_prompt(ModelFixture.STREAMING))]
        stream_text = "".join(chunk.text for chunk in chunks)
    except RuntimeError as exc:
        error = str(exc)
    subagent_phase_limit = await _probe_subagent_phase_limit()
    subagent_completion_boundary = await _probe_subagent_completion_boundary()
    subagent_task_drain = await _probe_subagent_task_drain()
    return {
        "text": text,
        "stream_text": stream_text,
        "error": error,
        "model_calls": model.lab_stats.model_calls,
        "input_tokens": model.lab_stats.input_tokens,
        "output_tokens": model.lab_stats.output_tokens,
        "model_starts": telemetry.model_starts,
        "model_finishes": telemetry.model_finishes,
        "subagent_phase_limit": subagent_phase_limit,
        "subagent_completion_boundary": subagent_completion_boundary,
        "subagent_task_drain": subagent_task_drain,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model-call-limit", type=int)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(run(args.base_url, args.model_call_limit)),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

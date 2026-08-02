"""Run model wire-compatibility fixtures through the native LangChain client."""

from __future__ import annotations

import argparse
import asyncio
import json
from importlib.metadata import version
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from agent_lab.model_compat_contracts import (
    MODEL_ACTOR_PROBES,
    MODEL_NAME,
    ModelFixture,
    StructuredAnswer,
    compatibility_model_profile,
    explicit_request_settings,
    fixture_prompt,
    openai_tool_schemas,
)
from agent_lab.model_request_policy import langchain_model_kwargs, model_request_headers

_executed_tools: list[dict[str, str]] = []


@tool(description="Look up an incident by identifier.")
def lookup_incident(incident_id: str) -> str:
    _executed_tools.append({"name": "lookup_incident", "incident_id": incident_id})
    return "INC-42 is stable"


@tool(description="List services owned by a team.")
def list_services(team: str) -> str:
    _executed_tools.append({"name": "list_services", "team": team})
    return "api,worker"


def _messages(fixture: ModelFixture) -> list[tuple[str, str]]:
    return [("user", fixture_prompt(fixture))]


def _usage(metadata: Any) -> dict[str, int] | None:
    if not metadata:
        return None
    input_details = metadata.get("input_token_details") or {}
    output_details = metadata.get("output_token_details") or {}
    return {
        "input_tokens": int(metadata.get("input_tokens", 0)),
        "output_tokens": int(metadata.get("output_tokens", 0)),
        "total_tokens": int(metadata.get("total_tokens", 0)),
        "reasoning_tokens": int(output_details.get("reasoning", 0)),
        "cached_input_tokens": int(input_details.get("cache_read", 0)),
    }


def _sum_usage(messages: list[Any]) -> dict[str, int] | None:
    values = [
        _usage(message.usage_metadata) for message in messages if isinstance(message, AIMessage)
    ]
    present = [value for value in values if value]
    if not present:
        return None
    return {key: sum(item[key] for item in present) for key in present[0]}


def _passed(value: Any, usage: Any) -> dict[str, Any]:
    normalized_usage = _usage(usage) if usage is not None else None
    return {
        "status": "passed",
        "value": value,
        "usage": normalized_usage,
        "usage_gap": None if normalized_usage else "native client exposed no usage metadata",
    }


def _failed(exc: Exception, failure_class: str) -> dict[str, Any]:
    return {
        "status": "expected_failure" if failure_class == "model_parser" else "failed",
        "failure_class": failure_class,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _tool_calls(message: AIMessage) -> list[dict[str, Any]]:
    if message.invalid_tool_calls:
        invalid = message.invalid_tool_calls[0]
        raw_arguments = invalid.get("args")
        if isinstance(raw_arguments, str):
            json.loads(raw_arguments)
        raise ValueError(f"client reported invalid tool call: {invalid!r}")
    return [{"name": call["name"], "arguments": call["args"]} for call in message.tool_calls]


async def _direct_fixture(client: ChatOpenAI, fixture: ModelFixture) -> dict[str, Any]:
    try:
        if fixture is ModelFixture.PLAIN:
            response = await client.ainvoke(_messages(fixture))
            return _passed(str(response.text), response.usage_metadata)
        if fixture is ModelFixture.STRUCTURED:
            structured = client.with_structured_output(
                StructuredAnswer, method="json_schema", strict=True
            )
            response = await structured.ainvoke(_messages(fixture))
            assert isinstance(response, StructuredAnswer)
            # LangChain's parsed structured wrapper does not return the raw usage object.
            return {
                "status": "passed",
                "value": response.model_dump(),
                "usage": None,
                "usage_gap": "with_structured_output omits raw usage unless include_raw is enabled",
            }
        if fixture is ModelFixture.STREAMING:
            text_parts: list[str] = []
            usage_metadata: dict[str, Any] | None = None
            async for chunk in client.astream(_messages(fixture)):
                if isinstance(chunk.content, str):
                    text_parts.append(chunk.content)
                if chunk.usage_metadata:
                    usage_metadata = chunk.usage_metadata
            return _passed("".join(text_parts), usage_metadata)
        bound = client.bind_tools(
            openai_tool_schemas(),
            tool_choice="required",
            parallel_tool_calls=fixture is ModelFixture.MULTI_TOOL,
        )
        response = await bound.ainvoke(_messages(fixture))
        return _passed(_tool_calls(response), response.usage_metadata)
    except json.JSONDecodeError as exc:
        return _failed(exc, "model_parser")
    except Exception as exc:
        failure_class = (
            "model_parser" if fixture is ModelFixture.MALFORMED_TOOL else "client_serialization"
        )
        return _failed(exc, failure_class)


async def _multi_turn(client: ChatOpenAI) -> dict[str, Any]:
    _executed_tools.clear()
    try:
        agent = create_agent(client, [lookup_incident], name="compatibility_agent")
        state = await agent.ainvoke(
            {"messages": _messages(ModelFixture.MULTI_TURN)},
            config={"recursion_limit": 6},
        )
        messages = state["messages"]
        final = messages[-1]
        text = str(final.text)
        if text != "multi-turn complete" or _executed_tools != [
            {"name": "lookup_incident", "incident_id": "INC-42"}
        ]:
            raise RuntimeError(f"unexpected loop result text={text!r} tools={_executed_tools!r}")
        usage = _sum_usage(messages)
        return {
            "status": "passed",
            "value": text,
            "usage": usage,
            "usage_gap": None if usage else "agent state exposed no usage metadata",
            "executed_tools": list(_executed_tools),
        }
    except Exception as exc:
        return _failed(exc, "framework_loop")


async def _actor_probes(profile: Any) -> dict[str, Any]:
    probes: dict[str, Any] = {}
    for actor in MODEL_ACTOR_PROBES:
        client = ChatOpenAI(
            api_key="model-wire-test",
            default_headers=model_request_headers("langchain", actor),
            **langchain_model_kwargs(profile),
        )
        try:
            response = await client.ainvoke(_messages(ModelFixture.PLAIN))
            probes[actor] = _passed(str(response.text), response.usage_metadata)
        except Exception as exc:
            probes[actor] = _failed(exc, "client_serialization")
    return probes


async def run(base_url: str, model: str) -> dict[str, Any]:
    profile = compatibility_model_profile(base_url, model)
    client = ChatOpenAI(
        api_key="model-wire-test",
        default_headers={"X-Agent-Lab-Framework": "langchain"},
        **langchain_model_kwargs(profile),
    )
    fixtures: dict[str, Any] = {}
    for fixture in ModelFixture:
        fixtures[fixture.value] = (
            await _multi_turn(client)
            if fixture is ModelFixture.MULTI_TURN
            else await _direct_fixture(client, fixture)
        )
    return {
        "framework": "langchain",
        "client": "ChatOpenAI",
        "package_versions": {
            "langchain": version("langchain"),
            "langchain-openai": version("langchain-openai"),
            "langgraph": version("langgraph"),
            "openai": version("openai"),
        },
        "model": model,
        "base_url": base_url,
        "settings": explicit_request_settings(),
        "fixtures": fixtures,
        "actor_probes": await _actor_probes(profile),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", default=MODEL_NAME)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.base_url, args.model)), sort_keys=True))


if __name__ == "__main__":
    main()

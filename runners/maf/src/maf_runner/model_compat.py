"""Run model wire-compatibility fixtures through the native MAF client."""

from __future__ import annotations

import argparse
import asyncio
import json
from importlib.metadata import version
from typing import Any

from agent_framework import Agent, Message, tool
from agent_framework.openai import OpenAIChatCompletionClient
from openai import AsyncOpenAI

from agent_lab.model_compat_contracts import (
    MAX_OUTPUT_TOKENS,
    MODEL_NAME,
    REQUEST_TIMEOUT_SECONDS,
    SEED,
    TEMPERATURE,
    TOP_P,
    ModelFixture,
    StructuredAnswer,
    explicit_request_settings,
    fixture_prompt,
    openai_tool_schemas,
)

_executed_tools: list[dict[str, str]] = []


@tool(description="Look up an incident by identifier.")
def lookup_incident(incident_id: str) -> str:
    _executed_tools.append({"name": "lookup_incident", "incident_id": incident_id})
    return "INC-42 is stable"


@tool(description="List services owned by a team.")
def list_services(team: str) -> str:
    _executed_tools.append({"name": "list_services", "team": team})
    return "api,worker"


def _messages(fixture: ModelFixture) -> list[Message]:
    return [Message(role="user", contents=[fixture_prompt(fixture)])]


def _options(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "seed": SEED,
        "user": "model-wire/maf",
    }
    options.update(overrides)
    return options


def _usage(value: Any) -> dict[str, int] | None:
    if not value:
        return None
    return {
        "input_tokens": int(value.get("input_token_count", 0)),
        "output_tokens": int(value.get("output_token_count", 0)),
        "total_tokens": int(value.get("total_token_count", 0)),
        "reasoning_tokens": int(value.get("reasoning_output_token_count", 0)),
        "cached_input_tokens": int(value.get("cache_read_input_token_count", 0)),
    }


def _tool_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in response.messages:
        for content in message.contents:
            if content.type != "function_call":
                continue
            arguments = content.arguments
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            calls.append({"name": content.name, "arguments": arguments})
    return calls


def _passed(value: Any, usage: Any) -> dict[str, Any]:
    normalized_usage = _usage(usage)
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


async def _direct_fixture(
    client: OpenAIChatCompletionClient, fixture: ModelFixture
) -> dict[str, Any]:
    try:
        if fixture is ModelFixture.PLAIN:
            response = await client.get_response(_messages(fixture), options=_options())
            return _passed(response.text, response.usage_details)
        if fixture is ModelFixture.STRUCTURED:
            response = await client.get_response(
                _messages(fixture), options=_options(response_format=StructuredAnswer)
            )
            value = response.value
            assert isinstance(value, StructuredAnswer)
            return _passed(value.model_dump(), response.usage_details)
        if fixture is ModelFixture.STREAMING:
            stream = client.get_response(_messages(fixture), stream=True, options=_options())
            response = await stream.get_final_response()
            return _passed(response.text, response.usage_details)
        response = await client.get_response(
            _messages(fixture),
            options=_options(
                tools=openai_tool_schemas(),
                tool_choice="required",
                allow_multiple_tool_calls=fixture is ModelFixture.MULTI_TOOL,
            ),
        )
        calls = _tool_calls(response)
        return _passed(calls, response.usage_details)
    except json.JSONDecodeError as exc:
        return _failed(exc, "model_parser")
    except (AssertionError, TypeError, ValueError) as exc:
        return _failed(exc, "client_serialization")
    except Exception as exc:  # native exceptions differ by installed SDK version
        return _failed(exc, "transport")


async def _multi_turn(client: OpenAIChatCompletionClient) -> dict[str, Any]:
    _executed_tools.clear()
    try:
        agent = Agent(client, name="compatibility_agent", tools=[lookup_incident])
        response = await agent.run(
            fixture_prompt(ModelFixture.MULTI_TURN),
            options=_options(tool_choice="auto", allow_multiple_tool_calls=False),
        )
        if response.text != "multi-turn complete" or _executed_tools != [
            {"name": "lookup_incident", "incident_id": "INC-42"}
        ]:
            raise RuntimeError(
                f"unexpected loop result text={response.text!r} tools={_executed_tools!r}"
            )
        result = _passed(response.text, response.usage_details)
        result["executed_tools"] = list(_executed_tools)
        return result
    except Exception as exc:
        return _failed(exc, "framework_loop")


async def run(base_url: str, model: str) -> dict[str, Any]:
    direct_sdk = AsyncOpenAI(
        api_key="model-wire-test",
        base_url=base_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
        default_headers={"X-Agent-Lab-Framework": "maf"},
    )
    loop_sdk = AsyncOpenAI(
        api_key="model-wire-test",
        base_url=base_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
        default_headers={"X-Agent-Lab-Framework": "maf"},
    )
    direct_client = OpenAIChatCompletionClient(
        model=model,
        async_client=direct_sdk,
        function_invocation_configuration={"enabled": False},
    )
    loop_client = OpenAIChatCompletionClient(
        model=model,
        async_client=loop_sdk,
        function_invocation_configuration={
            "enabled": True,
            "max_iterations": 3,
            "max_function_calls": 2,
        },
    )
    try:
        fixtures: dict[str, Any] = {}
        for fixture in ModelFixture:
            fixtures[fixture.value] = (
                await _multi_turn(loop_client)
                if fixture is ModelFixture.MULTI_TURN
                else await _direct_fixture(direct_client, fixture)
            )
        return {
            "framework": "maf",
            "client": "OpenAIChatCompletionClient",
            "package_versions": {
                "agent-framework-core": version("agent-framework-core"),
                "agent-framework-openai": version("agent-framework-openai"),
                "openai": version("openai"),
            },
            "model": model,
            "base_url": base_url,
            "settings": explicit_request_settings(),
            "fixtures": fixtures,
        }
    finally:
        await direct_sdk.close()
        await loop_sdk.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", default=MODEL_NAME)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.base_url, args.model)), sort_keys=True))


if __name__ == "__main__":
    main()

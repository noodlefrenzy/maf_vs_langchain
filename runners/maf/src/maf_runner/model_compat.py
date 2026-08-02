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

from agent_lab.contracts import ModelProfile
from agent_lab.model_compat_contracts import (
    MODEL_ACTOR_PROBES,
    MODEL_NAME,
    ModelFixture,
    StructuredAnswer,
    compatibility_model_profile,
    explicit_request_settings,
    fixture_prompt,
    normalize_fixture_text,
    openai_tool_schemas,
)
from agent_lab.model_request_policy import maf_request_options, model_request_headers

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


def _options(profile: Any, **overrides: Any) -> dict[str, Any]:
    options = maf_request_options(profile)
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
    client: OpenAIChatCompletionClient, fixture: ModelFixture, profile: Any
) -> dict[str, Any]:
    try:
        if fixture is ModelFixture.PLAIN:
            response = await client.get_response(_messages(fixture), options=_options(profile))
            return _passed(response.text, response.usage_details)
        if fixture is ModelFixture.STRUCTURED:
            response = await client.get_response(
                _messages(fixture), options=_options(profile, response_format=StructuredAnswer)
            )
            value = response.value
            assert isinstance(value, StructuredAnswer)
            return _passed(value.model_dump(), response.usage_details)
        if fixture is ModelFixture.STREAMING:
            stream = client.get_response(_messages(fixture), stream=True, options=_options(profile))
            response = await stream.get_final_response()
            return _passed(response.text, response.usage_details)
        response = await client.get_response(
            _messages(fixture),
            options=_options(
                profile,
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


async def _multi_turn(client: OpenAIChatCompletionClient, profile: Any) -> dict[str, Any]:
    _executed_tools.clear()
    try:
        agent = Agent(client, name="compatibility_agent", tools=[lookup_incident])
        response = await agent.run(
            fixture_prompt(ModelFixture.MULTI_TURN),
            options=_options(
                profile,
                tool_choice="auto",
                allow_multiple_tool_calls=False,
            ),
        )
        raw_text = response.text
        text = normalize_fixture_text(raw_text)
        if text != "multi-turn complete" or _executed_tools != [
            {"name": "lookup_incident", "incident_id": "INC-42"}
        ]:
            raise RuntimeError(
                f"unexpected loop result text={raw_text!r} tools={_executed_tools!r}"
            )
        result = _passed(text, response.usage_details)
        if raw_text != text:
            result["raw_value"] = raw_text
        result["executed_tools"] = list(_executed_tools)
        return result
    except Exception as exc:
        return _failed(exc, "framework_loop")


async def _actor_probes(profile: Any) -> dict[str, Any]:
    probes: dict[str, Any] = {}
    for actor in MODEL_ACTOR_PROBES:
        sdk = AsyncOpenAI(
            api_key="model-wire-test",
            base_url=profile.base_url,
            timeout=profile.request_timeout_seconds,
            max_retries=0,
            default_headers=model_request_headers("maf", actor),
        )
        client = OpenAIChatCompletionClient(model=profile.name, async_client=sdk)
        try:
            response = await client.get_response(
                _messages(ModelFixture.PLAIN),
                options=_options(profile),
            )
            probes[actor] = _passed(response.text, response.usage_details)
        except Exception as exc:
            probes[actor] = _failed(exc, "client_serialization")
        finally:
            await sdk.close()
    return probes


async def run(profile: ModelProfile) -> dict[str, Any]:
    base_url = profile.base_url
    model = profile.name
    direct_sdk = AsyncOpenAI(
        api_key="model-wire-test",
        base_url=base_url,
        timeout=profile.request_timeout_seconds,
        max_retries=0,
        default_headers={"X-Agent-Lab-Framework": "maf"},
    )
    loop_sdk = AsyncOpenAI(
        api_key="model-wire-test",
        base_url=base_url,
        timeout=profile.request_timeout_seconds,
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
                await _multi_turn(loop_client, profile)
                if fixture is ModelFixture.MULTI_TURN
                else await _direct_fixture(direct_client, fixture, profile)
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
            "settings": explicit_request_settings(profile),
            "fixtures": fixtures,
            "actor_probes": await _actor_probes(profile),
        }
    finally:
        await direct_sdk.close()
        await loop_sdk.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--model", default=MODEL_NAME)
    parser.add_argument("--profile-json")
    args = parser.parse_args()
    if args.profile_json:
        profile = ModelProfile.model_validate_json(args.profile_json)
    elif args.base_url:
        profile = compatibility_model_profile(args.base_url, args.model)
    else:
        parser.error("--profile-json or --base-url is required")
    print(json.dumps(asyncio.run(run(profile)), sort_keys=True))


if __name__ == "__main__":
    main()

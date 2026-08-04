"""Canonical model-profile to OpenAI Chat Completions request mapping."""

from __future__ import annotations

from typing import Any

from agent_lab.contract_bundle import sha256_payload
from agent_lab.contracts import AgentRole, FrameworkLane, ModelProfile

_MULTI_AGENT_ACTORS = (
    AgentRole.TELEMETRY_INVESTIGATOR,
    AgentRole.CHANGE_INVESTIGATOR,
    AgentRole.RELIABILITY_REVIEWER,
    AgentRole.INCIDENT_COMMANDER,
)
_SINGLE_AGENT_LANES = {
    FrameworkLane.MAF_SINGLE_AGENT,
    FrameworkLane.LANGCHAIN_SINGLE_AGENT,
}
MODEL_PROFILE_FIELD_MAPPING = {
    "name": "wire.model",
    "base_url": "transport.base_url",
    "api_style": "transport.api_style",
    "temperature": "wire.temperature",
    "top_p": "wire.top_p",
    "top_k": "wire.top_k when non-null",
    "min_p": "wire.min_p when non-null",
    "presence_penalty": "wire.presence_penalty",
    "repetition_penalty": "wire.repetition_penalty",
    "max_output_tokens": "wire.max_completion_tokens",
    "context_window_tokens": "local.context_window_tokens",
    "request_timeout_seconds": "transport.request_timeout_seconds",
    "seed": "wire.seed when non-null",
    "thinking": "wire.chat_template_kwargs.enable_thinking",
}
ADAPTER_SUPPORTED_MODEL_FIELDS = {
    "maf": frozenset(MODEL_PROFILE_FIELD_MAPPING),
    "langchain": frozenset(MODEL_PROFILE_FIELD_MAPPING),
}


def validate_model_profile_support() -> None:
    """Fail closed when the profile grows beyond either adapter's frozen mapping."""

    profile_fields = frozenset(ModelProfile.model_fields)
    for adapter, supported in ADAPTER_SUPPORTED_MODEL_FIELDS.items():
        if supported != profile_fields:
            missing = sorted(profile_fields - supported)
            stale = sorted(supported - profile_fields)
            raise ValueError(
                f"{adapter} model-profile mapping is incomplete: missing={missing}, stale={stale}"
            )


def model_extension_body(profile: ModelProfile) -> dict[str, Any]:
    """Return non-standard sampling fields sent in the OpenAI request body."""

    extensions: dict[str, Any] = {
        "repetition_penalty": float(profile.repetition_penalty),
        "chat_template_kwargs": {"enable_thinking": profile.thinking},
    }
    if profile.top_k is not None:
        extensions["top_k"] = profile.top_k
    if profile.min_p is not None:
        extensions["min_p"] = profile.min_p
    return extensions


def openai_request_parameters(profile: ModelProfile) -> dict[str, Any]:
    """Return the shared effective parameters at the OpenAI wire boundary."""

    parameters = {
        "model": profile.name,
        "temperature": float(profile.temperature),
        "top_p": float(profile.top_p),
        "presence_penalty": float(profile.presence_penalty),
        "max_completion_tokens": profile.max_output_tokens,
        **model_extension_body(profile),
    }
    if profile.seed is not None:
        parameters["seed"] = profile.seed
    return parameters


def langchain_model_kwargs(profile: ModelProfile) -> dict[str, Any]:
    """Return constructor kwargs for the matched LangChain OpenAI adapter."""

    parameters = {
        "model": profile.name,
        "base_url": profile.base_url,
        "temperature": float(profile.temperature),
        "top_p": float(profile.top_p),
        "presence_penalty": float(profile.presence_penalty),
        "max_completion_tokens": profile.max_output_tokens,
        "timeout": float(profile.request_timeout_seconds),
        "max_retries": 0,
        "stream_usage": True,
        "extra_body": model_extension_body(profile),
    }
    if profile.seed is not None:
        parameters["seed"] = profile.seed
    return parameters


def maf_request_options(profile: ModelProfile) -> dict[str, Any]:
    """Return per-call options for the matched MAF OpenAI adapter."""

    options = {
        "temperature": float(profile.temperature),
        "top_p": float(profile.top_p),
        "presence_penalty": float(profile.presence_penalty),
        "max_tokens": profile.max_output_tokens,
        "extra_body": model_extension_body(profile),
    }
    if profile.seed is not None:
        options["seed"] = profile.seed
    return options


def model_request_headers(framework: str, actor: str) -> dict[str, str]:
    """Return non-sensitive headers used to identify redacted wire probes."""

    return {
        "X-Agent-Lab-Framework": framework,
        "X-Agent-Lab-Actor": actor,
    }


def effective_request_policy(
    profile: ModelProfile,
    lane: FrameworkLane,
) -> dict[str, Any]:
    """Describe and hash every effective input shared by actors in one lane."""

    validate_model_profile_support()
    actors = (AgentRole.INCIDENT_COMMANDER,) if lane in _SINGLE_AGENT_LANES else _MULTI_AGENT_ACTORS
    actor_policy = {
        role.value: {
            "wire": openai_request_parameters(profile),
            "transport": {
                "api_style": profile.api_style,
                "base_url": profile.base_url,
                "request_timeout_seconds": float(profile.request_timeout_seconds),
                "max_retries": 0,
            },
            "local": {
                "context_window_tokens": profile.context_window_tokens,
            },
        }
        for role in actors
    }
    shared_policy = {
        "schema_version": "model-request-policy/v1",
        "actors": actor_policy,
        "field_mapping": MODEL_PROFILE_FIELD_MAPPING,
    }
    return {
        **shared_policy,
        "lane": lane.value,
        "policy_sha256": sha256_payload(shared_policy),
    }


def matched_effective_request_policy(
    profile: ModelProfile,
    left_lane: FrameworkLane,
    right_lane: FrameworkLane,
) -> dict[str, Any]:
    """Return the framework-neutral policy and fail if matched inputs drift."""

    left = effective_request_policy(profile, left_lane)
    right = effective_request_policy(profile, right_lane)
    left_actors = left["actors"]
    right_actors = right["actors"]
    if left_actors != right_actors:
        raise ValueError("effective model request policy differs between matched lanes")
    shared = {
        "schema_version": "matched-model-request-policy/v1",
        "lanes": [left_lane.value, right_lane.value],
        "actors": left_actors,
        "field_mapping": left["field_mapping"],
        "lane_policy_sha256": {
            left_lane.value: left["policy_sha256"],
            right_lane.value: right["policy_sha256"],
        },
    }
    return {**shared, "policy_sha256": sha256_payload(shared)}

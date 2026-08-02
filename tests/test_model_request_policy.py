from agent_lab.contracts import FrameworkLane, ModelProfile
from agent_lab.model_request_policy import (
    ADAPTER_SUPPORTED_MODEL_FIELDS,
    MODEL_PROFILE_FIELD_MAPPING,
    effective_request_policy,
    maf_request_options,
    matched_effective_request_policy,
    openai_request_parameters,
    validate_model_profile_support,
)


def _profile() -> ModelProfile:
    return ModelProfile(
        name="model-revision",
        base_url="http://127.0.0.1:8000/v1",
        temperature=0.3,
        top_p=0.85,
        top_k=17,
        min_p=0.08,
        presence_penalty=0.2,
        repetition_penalty=1.1,
        max_output_tokens=1536,
        context_window_tokens=32768,
        request_timeout_seconds=45,
        seed=73,
        thinking=False,
    )


def test_every_model_profile_field_has_matched_adapter_support() -> None:
    validate_model_profile_support()

    expected = set(ModelProfile.model_fields)
    assert set(MODEL_PROFILE_FIELD_MAPPING) == expected
    assert all(set(fields) == expected for fields in ADAPTER_SUPPORTED_MODEL_FIELDS.values())


def test_non_default_profile_maps_to_explicit_wire_and_local_inputs() -> None:
    profile = _profile()

    wire = openai_request_parameters(profile)
    maf = maf_request_options(profile)

    assert wire == {
        "model": "model-revision",
        "temperature": 0.3,
        "top_p": 0.85,
        "top_k": 17,
        "min_p": 0.08,
        "presence_penalty": 0.2,
        "repetition_penalty": 1.1,
        "max_completion_tokens": 1536,
        "seed": 73,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert maf["max_tokens"] == wire["max_completion_tokens"]
    assert maf["extra_body"] == {
        "top_k": 17,
        "min_p": 0.08,
        "repetition_penalty": 1.1,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_policy_hash_is_serialization_stable_and_matched() -> None:
    profile = _profile()
    round_tripped = ModelProfile.model_validate_json(profile.model_dump_json())
    left = effective_request_policy(profile, FrameworkLane.MAF_WORKFLOW)
    restored = effective_request_policy(round_tripped, FrameworkLane.MAF_WORKFLOW)
    right = effective_request_policy(round_tripped, FrameworkLane.LANGGRAPH)
    matched = matched_effective_request_policy(
        profile,
        FrameworkLane.MAF_WORKFLOW,
        FrameworkLane.LANGGRAPH,
    )

    assert left["policy_sha256"] == restored["policy_sha256"]
    assert left["policy_sha256"] == right["policy_sha256"]
    assert matched["lane_policy_sha256"] == {
        "maf_workflow": left["policy_sha256"],
        "langgraph": right["policy_sha256"],
    }

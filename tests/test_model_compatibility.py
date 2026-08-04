import json

import pytest

from agent_lab.model_compat_contracts import (
    MODEL_ACTOR_PROBES,
    ModelFixture,
    normalize_fixture_text,
)
from agent_lab.model_compat_suite import (
    _advertised_model_ids,
    _validate_loopback_base_url,
    run_suite,
)


def test_native_clients_pass_protocol_stub_compatibility_suite() -> None:
    report = run_suite()

    assert report["passed"] is True
    assert report["errors"] == []
    assert report["mode"] == "protocol_stub"
    assert report["wire_capture_gap"] is None
    assert report["server"] is None
    assert set(report["frameworks"]) == {"maf", "langchain"}

    for framework in report["frameworks"].values():
        fixtures = framework["fixtures"]
        assert set(fixtures) == {fixture.value for fixture in ModelFixture}
        assert fixtures["malformed_tool"]["failure_class"] == "model_parser"
        assert fixtures["multi_turn"]["executed_tools"] == [
            {"name": "lookup_incident", "incident_id": "INC-42"}
        ]
        assert fixtures["multi_turn"]["usage"]["reasoning_tokens"] == 4
        assert set(framework["actor_probes"]) == set(MODEL_ACTOR_PROBES)
        assert len(framework["wire_exchanges"]) == 8 + len(MODEL_ACTOR_PROBES)

    wire_exchanges = [
        exchange
        for framework in report["frameworks"].values()
        for exchange in framework["wire_exchanges"]
    ]
    for exchange in wire_exchanges:
        assert exchange["validation_errors"] == []
        assert len(exchange["wire_request_sha256"]) == 64
        assert len(exchange["effective_request_sha256"]) == 64
        assert len(exchange["wire_response_sha256"]) == 64
        assert exchange["request_shape"]
        assert exchange["response_shape"]

    assert any(exchange["server_default_expansion"] for exchange in wire_exchanges)
    assert "model-wire-test" not in json.dumps(report)
    assert report["frameworks"]["langchain"]["fixtures"]["structured"]["usage_gap"]


def test_live_inventory_extracts_openai_model_ids() -> None:
    payload = {
        "object": "list",
        "data": [
            {"id": "aligned-model", "object": "model"},
            {"id": "second-model", "object": "model"},
        ],
    }

    assert _advertised_model_ids(payload) == ["aligned-model", "second-model"]


def test_fixture_text_normalization_only_removes_boundary_whitespace() -> None:
    assert normalize_fixture_text("\n\nmulti-turn complete \t") == "multi-turn complete"
    assert normalize_fixture_text("multi turn complete") == "multi turn complete"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:8000/v1",
        "http://example.com/v1",
        "http://user:secret@127.0.0.1:8000/v1",
        "http://127.0.0.1:8000/not-v1",
        "http://127.0.0.1:8000/v1?redirect=http://example.com",
    ],
)
def test_live_compatibility_endpoint_must_be_loopback_v1(base_url: str) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        _validate_loopback_base_url(base_url)


def test_live_compatibility_endpoint_accepts_loopback_v1() -> None:
    _validate_loopback_base_url("http://127.0.0.1:8000/v1")
    _validate_loopback_base_url("http://[::1]:8000/v1/")

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_lab.contracts import (
    ApprovalDecision,
    Budget,
    EventKind,
    EvidenceItem,
    ExperimentSpec,
    FrameworkLane,
    HarnessAblationProfile,
    IncidentReport,
    NormalizedEvent,
)


def test_non_default_harness_ablation_requires_harness_lanes() -> None:
    with pytest.raises(ValidationError, match="requires a harness lane pair"):
        ExperimentSpec(
            experiment_id="invalid_ablation",
            scenario="incident_response",
            lanes=[FrameworkLane.MAF_WORKFLOW, FrameworkLane.LANGGRAPH],
            seeds=[42],
            repetitions=1,
            order="alternating",
            model_profile="model.toml",
            fault_profile="none",
            budget=Budget(
                wall_clock_seconds=10,
                model_calls=2,
                tool_calls=4,
                output_tokens=100,
                approval_requests=1,
            ),
            harness_ablation=HarnessAblationProfile(prompt_variant="paraphrase_a"),
        )


def test_normalized_event_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        NormalizedEvent(
            run_id=uuid4(),
            sequence=0,
            lane=FrameworkLane.LANGGRAPH,
            kind=EventKind.RUN_STARTED,
            unexpected=True,
        )


def test_normalized_event_uses_portable_kind_values() -> None:
    event = NormalizedEvent(
        run_id=uuid4(),
        sequence=0,
        lane=FrameworkLane.MAF_WORKFLOW,
        kind=EventKind.TOOL_STARTED,
        actor="telemetry-investigator",
    )

    assert event.kind.value == "tool.started"
    assert event.schema_version == "0.1.0"


def test_incident_report_requires_multiple_evidence_items() -> None:
    evidence = EvidenceItem(
        evidence_id="ev-1",
        source_role="telemetry",
        source_tool="search_logs",
        claim="Retries initiated the downstream saturation.",
        observation="Retry volume rose first.",
        world_tick=3,
        result_sha256="a" * 64,
        supports="root_cause",
    )

    with pytest.raises(ValidationError):
        IncidentReport(
            case_id="inc-test",
            root_cause="retry storm",
            confidence=0.8,
            evidence=[evidence],
            mitigation_action_id="rollback_checkout_retry_policy",
            mitigation="rollback",
            approval_decision=ApprovalDecision.APPROVE,
            customer_impact="checkout errors",
            residual_risk="low",
        )

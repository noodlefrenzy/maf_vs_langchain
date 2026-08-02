from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_lab.contracts import (
    ApprovalDecision,
    EventKind,
    EvidenceItem,
    FrameworkLane,
    IncidentReport,
    NormalizedEvent,
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

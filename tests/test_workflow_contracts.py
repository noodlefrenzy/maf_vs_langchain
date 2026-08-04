import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_lab.contract_bundle import contract_bundle_hashes, sha256_payload
from agent_lab.contracts import AgentRole, ApprovalDecision
from agent_lab.scenario import MitigationActionId
from agent_lab.workflow_contracts import (
    SPECIALIST_OBSERVATION_INSTRUCTIONS,
    CommanderSynthesis,
    InvestigationTask,
    RoleRunStats,
    SpecialistFinding,
    WorkflowExecutionResult,
    evaluate_evidence,
    evidence_ids_from_text,
    invoke_tool_once,
    invoke_tool_with_policy,
    render_tool_result,
    specialist_observation_prompt,
    workflow_contract_payload,
)
from agent_lab.workflow_errors import WorkflowToolFailure


def _finding(
    role: AgentRole,
    *evidence_ids: str,
) -> SpecialistFinding:
    return SpecialistFinding(
        role=role,
        summary=f"{role.value} summary",
        claims=[f"claim from {role.value}"],
        evidence_ids=list(evidence_ids),
        tools_used=[f"{role.value}_tool"],
    )


def _findings() -> list[SpecialistFinding]:
    return [
        _finding(AgentRole.TELEMETRY_INVESTIGATOR, "ev-telemetry"),
        _finding(AgentRole.CHANGE_INVESTIGATOR, "ev-change"),
        _finding(AgentRole.RELIABILITY_REVIEWER, "ev-runbook"),
    ]


def _synthesis(
    evidence_ids: list[str] | None = None,
    *,
    sufficient: bool = True,
) -> CommanderSynthesis:
    return CommanderSynthesis(
        root_cause="correlated root cause",
        chronology=["change preceded symptoms", "telemetry confirmed impact"],
        evidence_ids=evidence_ids or ["ev-change", "ev-runbook", "ev-telemetry"],
        proposed_mitigation_id=MitigationActionId.ROLLBACK_CHECKOUT_RETRY_POLICY,
        proposed_mitigation="apply the reversible runbook action",
        recovery_checks=["verify checkout health"],
        evidence_sufficient=sufficient,
    )


def test_workflow_contract_is_frozen_and_contains_no_dead_retry_fields() -> None:
    payload = workflow_contract_payload()

    assert contract_bundle_hashes()["workflow_contract_sha256"] == sha256_payload(payload)
    assert payload["models"]["InvestigationTask"]["properties"] == {
        "public_ticket": {"title": "Public Ticket", "type": "string"}
    }
    assert payload["tool_error_policy"] == {
        "retryable_structured_error_retries": 1,
        "non_retryable_error_retries": 0,
        "structured_errors": "normalize_to_workflow_tool_failure",
        "connection_replay": "framework_native_behavior_under_test",
    }
    assert "at most one shared application retry" in SPECIALIST_OBSERVATION_INSTRUCTIONS
    assert "may also reconnect or retry" in SPECIALIST_OBSERVATION_INSTRUCTIONS
    with pytest.raises(ValidationError):
        InvestigationTask(public_ticket="ticket", attempt=1)  # type: ignore[call-arg]


def test_shared_evidence_gate_accepts_grounded_three_role_synthesis() -> None:
    packet = evaluate_evidence(_findings(), _synthesis())

    assert packet.passed is True
    assert packet.gate_reasons == []
    assert packet.evidence_ids == ["ev-change", "ev-runbook", "ev-telemetry"]


@pytest.mark.parametrize(
    ("findings", "synthesis", "reason"),
    [
        (
            _findings()[:2],
            _synthesis(["ev-change", "ev-telemetry"]),
            "all three specialist roles must report",
        ),
        (
            [
                _finding(AgentRole.TELEMETRY_INVESTIGATOR, "ev-shared"),
                _finding(AgentRole.CHANGE_INVESTIGATOR, "ev-shared"),
                _finding(AgentRole.RELIABILITY_REVIEWER, "ev-shared"),
            ],
            _synthesis(["ev-shared", "ev-unknown"]),
            "at least two distinct evidence identifiers are required",
        ),
        (
            _findings(),
            _synthesis(sufficient=False),
            "incident commander marked the evidence insufficient",
        ),
        (
            _findings(),
            _synthesis(["ev-change", "ev-unknown"]),
            "commander cited unknown evidence identifiers: ['ev-unknown']",
        ),
        (
            [
                _finding(AgentRole.TELEMETRY_INVESTIGATOR, "ev-one", "ev-two"),
                _finding(AgentRole.CHANGE_INVESTIGATOR, "ev-change"),
                _finding(AgentRole.RELIABILITY_REVIEWER, "ev-runbook"),
            ],
            _synthesis(["ev-one", "ev-two"]),
            "commander synthesis must cite at least two specialist roles",
        ),
    ],
)
def test_shared_evidence_gate_rejects_each_deterministic_guard(
    findings: list[SpecialistFinding],
    synthesis: CommanderSynthesis,
    reason: str,
) -> None:
    packet = evaluate_evidence(findings, synthesis)

    assert packet.passed is False
    assert reason in packet.gate_reasons


def test_shared_prompt_is_transport_honest_and_role_bound() -> None:
    prompt = specialist_observation_prompt(
        InvestigationTask(public_ticket="INC-42"),
        AgentRole.TELEMETRY_INVESTIGATOR,
        [{"tool": "query_service_health", "result": "{}"}],
    )

    assert "at most one shared application retry" in prompt
    assert "may also reconnect or retry" in prompt
    assert "Assigned role: telemetry_investigator" in prompt


def test_shared_result_parser_normalizes_both_native_client_shapes() -> None:
    payload = {"result": {"evidence_id": "ev-nested"}}
    encoded = json.dumps(payload)

    assert render_tool_result([{"type": "text", "text": encoded}]) == encoded
    assert render_tool_result([SimpleNamespace(text=encoded)]) == encoded
    assert render_tool_result(payload) == json.dumps(payload, sort_keys=True)
    assert evidence_ids_from_text(f"prefix {encoded} suffix") == {"ev-nested"}


@pytest.mark.parametrize(
    ("native_surface", "code", "retryable"),
    [
        (
            json.dumps(
                {
                    "schema_version": "0.1.0",
                    "code": "transient_timeout",
                    "message": "scheduled timeout",
                    "retryable": True,
                    "world_tick": 4,
                    "details": {},
                }
            ),
            "transient_timeout",
            True,
        ),
        (
            RuntimeError(
                "tool failed: "
                + json.dumps(
                    {
                        "schema_version": "0.1.0",
                        "code": "invalid_arguments",
                        "message": "invalid input",
                        "retryable": False,
                        "world_tick": 4,
                        "details": {},
                    }
                )
            ),
            "invalid_arguments",
            False,
        ),
    ],
)
def test_tool_error_surfaces_normalize_without_hidden_adapter_retry(
    native_surface: str | Exception,
    code: str,
    retryable: bool,
) -> None:
    calls = 0

    async def call():
        nonlocal calls
        calls += 1
        if isinstance(native_surface, Exception):
            raise native_surface
        return native_surface

    with pytest.raises(WorkflowToolFailure) as failure:
        asyncio.run(invoke_tool_once(call))

    assert calls == 1
    assert failure.value.code == code
    assert failure.value.retryable is retryable


@pytest.mark.parametrize(
    ("retryable", "expected_calls", "expected_attempts"),
    [
        (True, 2, ["started", "failed", "started", "succeeded"]),
        (False, 1, ["started", "failed"]),
    ],
)
def test_shared_tool_policy_retries_only_retryable_structured_error_once(
    retryable: bool,
    expected_calls: int,
    expected_attempts: list[str],
) -> None:
    calls = 0
    attempts: list[str] = []

    async def call() -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkflowToolFailure(
                code="transient_timeout" if retryable else "invalid_arguments",
                retryable=retryable,
                detail="deterministic policy probe",
            )
        return {"status": "ok"}

    async def started() -> None:
        attempts.append("started")

    async def finished(succeeded: bool) -> None:
        attempts.append("succeeded" if succeeded else "failed")

    if retryable:
        assert asyncio.run(
            invoke_tool_with_policy(
                call,
                attempt_started=started,
                attempt_finished=finished,
            )
        ) == {"status": "ok"}
    else:
        with pytest.raises(WorkflowToolFailure):
            asyncio.run(
                invoke_tool_with_policy(
                    call,
                    attempt_started=started,
                    attempt_finished=finished,
                )
            )

    assert calls == expected_calls
    assert attempts == expected_attempts


def test_shared_tool_policy_stops_after_second_retryable_failure() -> None:
    calls = 0
    attempts: list[bool] = []

    async def call() -> None:
        nonlocal calls
        calls += 1
        raise WorkflowToolFailure(
            code="transient_timeout",
            retryable=True,
            detail="persistent deterministic policy probe",
        )

    async def started() -> None:
        return None

    async def finished(succeeded: bool) -> None:
        attempts.append(succeeded)

    with pytest.raises(WorkflowToolFailure):
        asyncio.run(
            invoke_tool_with_policy(
                call,
                attempt_started=started,
                attempt_finished=finished,
            )
        )

    assert calls == 2
    assert attempts == [False, False]


def test_role_stats_count_failed_dispatches_before_completion() -> None:
    stats = RoleRunStats(model_calls=0, tool_calls=0, input_tokens=0, output_tokens=0)

    failed_model = stats.model_dispatched()
    failed_tool = failed_model.tool_dispatched()
    completed_retry = failed_tool.model_dispatched().model_completed(
        input_tokens=12,
        output_tokens=4,
    )

    assert failed_tool.model_dump() == {
        "model_calls": 1,
        "tool_calls": 1,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert completed_retry.model_dump() == {
        "model_calls": 2,
        "tool_calls": 1,
        "input_tokens": 12,
        "output_tokens": 4,
    }


def test_terminal_contract_rejects_rejected_execution_with_authorized_state() -> None:
    with pytest.raises(ValidationError, match="rejected execution"):
        WorkflowExecutionResult(
            status="approval_rejected",
            evidence=evaluate_evidence(_findings(), _synthesis()),
            approval_request={
                "request_id": "mit-1",
                "case_id": "case-1",
                "proposed_action_id": "rollback_checkout_retry_policy",
                "proposed_action": "rollback",
                "rationale": "grounded evidence",
                "evidence_ids": ["ev-change", "ev-runbook"],
                "requested_by": "incident_commander",
                "created_tick": 1,
            },
            approval_decision=ApprovalDecision.REJECT,
            action_id_to_execute=MitigationActionId.ROLLBACK_CHECKOUT_RETRY_POLICY,
        )

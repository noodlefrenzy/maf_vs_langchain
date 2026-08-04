"""Dependency-light contracts shared by observer and experiment control."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_lab.contracts import ApprovalDecision, RunManifest
from agent_lab.scenario import MitigationActionId

OBSERVER_PAIR_CONTEXT_SCHEMA = "observer-pair/v1"
OBSERVER_APPROVAL_SCHEMA = "observer-approval/v1"


class ObserverApprovalSubmission(BaseModel):
    """A token-authenticated operator decision; secrets never enter the payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=256)
    decision: ApprovalDecision
    edited_action_id: MitigationActionId | None = None

    @model_validator(mode="after")
    def validate_edit(self) -> ObserverApprovalSubmission:
        if self.decision is ApprovalDecision.EDIT and self.edited_action_id is None:
            raise ValueError("an edited approval requires edited_action_id")
        if self.decision is not ApprovalDecision.EDIT and self.edited_action_id is not None:
            raise ValueError("edited_action_id is valid only for an edited approval")
        return self


class ObserverApprovalDecision(ObserverApprovalSubmission):
    """Owner-only decision persisted for the experiment control process."""

    schema_version: Literal["observer-approval/v1"] = OBSERVER_APPROVAL_SCHEMA
    run_id: UUID
    decided_by: str = Field(min_length=1, max_length=128)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def approval_decision_path(bundle: Path, request_id: str) -> Path:
    """Return an injection-safe, deterministic decision path for one request."""

    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return bundle / "control" / f"approval-{digest}.json"


async def wait_for_observer_approval(
    bundle: Path,
    request_id: str,
    *,
    poll_interval_seconds: float = 0.1,
) -> ObserverApprovalDecision:
    """Wait for the loopback observer to persist a matching operator decision."""

    if poll_interval_seconds <= 0:
        raise ValueError("approval polling interval must be positive")
    destination = approval_decision_path(bundle, request_id)
    while True:
        if destination.is_file():
            decision = ObserverApprovalDecision.model_validate_json(
                destination.read_text(encoding="utf-8")
            )
            if decision.request_id != request_id:
                raise ValueError("observer approval request ID differs from the pending request")
            manifest = RunManifest.model_validate_json(
                (bundle / "manifest.json").read_text(encoding="utf-8")
            )
            if decision.run_id != manifest.run_id:
                raise ValueError("observer approval run ID differs from the bundle manifest")
            return decision
        await asyncio.sleep(poll_interval_seconds)


def observer_pair_context(
    *,
    index_file: str,
    pair_key: str,
    track: str,
    seed: int,
    repetition: int,
    run_ids: tuple[UUID, UUID],
) -> dict[str, object]:
    """Build the live-discovery sidecar shared by both members of a pair."""

    return {
        "schema_version": OBSERVER_PAIR_CONTEXT_SCHEMA,
        "pair_id": observer_pair_id(index_file, seed, repetition, pair_key),
        "index_file": index_file,
        "track": track,
        "seed": seed,
        "repetition": repetition,
        "run_ids": [str(run_id) for run_id in run_ids],
    }


def observer_pair_id(index_file: str, seed: int, repetition: int, track: str) -> str:
    """Derive the same opaque pair ID before and after the final index exists."""

    digest = hashlib.sha256(f"{index_file}:{seed}:{repetition}:{track}".encode()).hexdigest()[:20]
    return f"pair-{digest}"

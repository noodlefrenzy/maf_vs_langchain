"""Shared, bounded workflow failures that must classify identically across lanes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any


class EvidenceGateFailed(RuntimeError):
    """A model-produced synthesis completed but failed the deterministic evidence gate."""

    def __init__(self, reasons: Sequence[str]) -> None:
        normalized = tuple(reason.strip() for reason in reasons if reason.strip())
        if not normalized:
            raise ValueError("an evidence-gate failure requires at least one reason")
        self.reasons = normalized
        self.reason = "evidence_gate_failed:" + json.dumps(
            normalized,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        super().__init__(self.reason)


class WorkflowToolFailure(RuntimeError):
    """Framework-neutral explicit-workflow tool failure."""

    def __init__(self, *, code: str, retryable: bool, detail: str) -> None:
        self.code = code
        self.retryable = retryable
        self.detail = detail
        super().__init__(f"workflow_tool_failure:{code}:retryable={str(retryable).lower()}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "retryable": self.retryable,
            "detail": self.detail,
        }

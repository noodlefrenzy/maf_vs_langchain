"""Shared, bounded workflow failures that must classify identically across lanes."""

from __future__ import annotations

import json
from collections.abc import Sequence


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

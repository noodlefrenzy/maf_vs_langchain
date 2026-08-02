"""Framework-neutral telemetry callbacks used by native workflow adapters."""

from __future__ import annotations

from typing import Protocol


class WorkflowTelemetry(Protocol):
    async def handoff(
        self,
        *,
        source_actor: str,
        target_actor: str,
        phase: str,
    ) -> None: ...

    async def model_started(self, *, actor: str, model: str) -> None: ...

    async def model_finished(
        self,
        *,
        actor: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None: ...

    async def tool_started(self, *, actor: str, tool_name: str) -> None: ...

    async def tool_finished(
        self,
        *,
        actor: str,
        tool_name: str,
        succeeded: bool,
    ) -> None: ...


class NoOpWorkflowTelemetry:
    async def handoff(
        self,
        *,
        source_actor: str,
        target_actor: str,
        phase: str,
    ) -> None:
        return None

    async def model_started(self, *, actor: str, model: str) -> None:
        return None

    async def model_finished(
        self,
        *,
        actor: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        return None

    async def tool_started(self, *, actor: str, tool_name: str) -> None:
        return None

    async def tool_finished(
        self,
        *,
        actor: str,
        tool_name: str,
        succeeded: bool,
    ) -> None:
        return None


NOOP_WORKFLOW_TELEMETRY = NoOpWorkflowTelemetry()

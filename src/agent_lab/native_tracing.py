"""Best-effort runner-local OpenTelemetry capture and Phoenix correlation."""

from __future__ import annotations

import hashlib
import socket
import sys
from collections.abc import Callable, Sequence
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

from agent_lab.contracts import SCHEMA_VERSION, NormalizedEvent, RunManifest
from agent_lab.runner_protocol import RunRequest
from agent_lab.trace_contracts import (
    NativeSpanRecord,
    NativeTraceLink,
    TraceCollectorStatus,
    classify_native_span,
    compute_trace_completeness,
    structural_span_attributes,
)

type FrameworkInstrumentor = Callable[
    [object | None, bool, bool],
    Callable[[], None] | None,
]


class NativeTracingSession:
    """Own the one process-local tracer provider used by a native runner."""

    def __init__(
        self,
        request: RunRequest,
        instrument_framework: FrameworkInstrumentor,
    ) -> None:
        self.manifest: RunManifest = request.manifest
        self.paths = request.paths
        self.profile = request.tracing
        self.capture_sensitive = request.capture_sensitive
        self.capture_reasoning = request.capture_reasoning
        self.instrument_framework = instrument_framework
        self.project_name = f"agent-lab-{self.manifest.lane.value}"
        self._provider: Any | None = None
        self._root_span: Any | None = None
        self._context_token: object | None = None
        self._framework_cleanup: Callable[[], None] | None = None
        self._remote_exporter: Any | None = None
        self._collector_reachable = False
        self._records: list[NativeSpanRecord] = []
        self._records_lock = Lock()
        self._diagnostics: list[str] = []

    def start(self) -> None:
        """Enable native instrumentation without making tracing run-critical."""

        profile = self.profile
        if not profile.enabled:
            try:
                self._framework_cleanup = self.instrument_framework(
                    None,
                    False,
                    self.capture_sensitive,
                )
            except Exception as exc:  # pragma: no cover - defensive third-party boundary
                self._diagnostics.append(f"framework_disable_failed:{type(exc).__name__}")
            return

        try:
            from opentelemetry import context, trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                BatchSpanProcessor,
                SimpleSpanProcessor,
                SpanExporter,
                SpanExportResult,
            )
        except ImportError as exc:  # pragma: no cover - runner lockfiles provide these
            self._diagnostics.append(f"otel_import_failed:{type(exc).__name__}")
            return

        session = self

        class LocalLedgerExporter(SpanExporter):
            def export(self, spans: Sequence[Any]) -> Any:
                try:
                    records = [session._project_span(span) for span in spans]
                    session._append_records(records)
                except Exception as exc:  # pragma: no cover - OTel callback boundary
                    session._diagnostics.append(f"local_span_export_failed:{type(exc).__name__}")
                    return SpanExportResult.FAILURE
                return SpanExportResult.SUCCESS

        class StatusTrackingExporter(SpanExporter):
            def __init__(self, delegate: Any) -> None:
                self.delegate = delegate
                self.attempted = False
                self.succeeded = False
                self.failed = False

            def export(self, spans: Sequence[Any]) -> Any:
                self.attempted = True
                try:
                    result = self.delegate.export(spans)
                except Exception as exc:  # pragma: no cover - network/exporter boundary
                    self.failed = True
                    session._diagnostics.append(f"collector_export_failed:{type(exc).__name__}")
                    return SpanExportResult.FAILURE
                if result is SpanExportResult.SUCCESS:
                    self.succeeded = True
                else:
                    self.failed = True
                return result

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                try:
                    return bool(self.delegate.force_flush(timeout_millis))
                except Exception as exc:  # pragma: no cover - exporter boundary
                    self.failed = True
                    session._diagnostics.append(f"collector_flush_failed:{type(exc).__name__}")
                    return False

            def shutdown(self) -> None:
                try:
                    self.delegate.shutdown()
                except Exception as exc:  # pragma: no cover - exporter boundary
                    self.failed = True
                    session._diagnostics.append(f"collector_shutdown_failed:{type(exc).__name__}")

        try:
            resource = Resource.create(
                {
                    "service.name": self.project_name,
                    "openinference.project.name": self.project_name,
                    "agent.lab.run_id": str(self.manifest.run_id),
                    "agent.lab.lane": self.manifest.lane.value,
                }
            )
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(SimpleSpanProcessor(LocalLedgerExporter()))
            self._collector_reachable = _collector_reachable(
                profile.collector_endpoint,
                profile.exporter_timeout_seconds,
            )
            if self._collector_reachable:
                remote = StatusTrackingExporter(
                    OTLPSpanExporter(
                        endpoint=profile.collector_endpoint,
                        timeout=profile.exporter_timeout_seconds,
                    )
                )
                provider.add_span_processor(
                    BatchSpanProcessor(
                        remote,
                        schedule_delay_millis=200,
                        export_timeout_millis=int(profile.exporter_timeout_seconds * 1000),
                    )
                )
                self._remote_exporter = remote
            trace.set_tracer_provider(provider)
            self._provider = provider
            try:
                self._framework_cleanup = self.instrument_framework(
                    provider,
                    True,
                    self.capture_sensitive,
                )
            except Exception as exc:  # pragma: no cover - third-party instrumentation boundary
                self._diagnostics.append(f"framework_instrumentation_failed:{type(exc).__name__}")
            tracer = provider.get_tracer("agent_lab.native_tracing", SCHEMA_VERSION)
            self._root_span = tracer.start_span(
                f"agent-lab.run {self.manifest.lane.value}",
                attributes={
                    "agent.lab.operation": "run",
                    "agent.lab.run_id": str(self.manifest.run_id),
                    "agent.lab.lane": self.manifest.lane.value,
                },
            )
            self._context_token = context.attach(trace.set_span_in_context(self._root_span))
        except Exception as exc:  # pragma: no cover - OTel SDK boundary
            self._diagnostics.append(f"otel_setup_failed:{type(exc).__name__}")

    def finish(self, events: list[NormalizedEvent]) -> None:
        """Flush spans and atomically write a redacted trace-link sidecar."""

        try:
            self._finish_provider()
            self._write_trace_link(events)
        except Exception as exc:  # pragma: no cover - trace evidence must remain non-fatal
            print(
                f"native tracing finalization failed: {type(exc).__name__}",
                file=sys.stderr,
            )

    def _finish_provider(self) -> None:
        if self._provider is None:
            if self._framework_cleanup is not None:
                self._run_framework_cleanup()
            return
        from opentelemetry import context

        if self._context_token is not None:
            context.detach(self._context_token)
            self._context_token = None
        if self._root_span is not None:
            self._root_span.end()
            self._root_span = None
        self._run_framework_cleanup()
        try:
            self._provider.shutdown()
        except Exception as exc:  # pragma: no cover - OTel SDK boundary
            self._diagnostics.append(f"otel_shutdown_failed:{type(exc).__name__}")

    def _run_framework_cleanup(self) -> None:
        if self._framework_cleanup is None:
            return
        try:
            self._framework_cleanup()
        except Exception as exc:  # pragma: no cover - third-party instrumentation boundary
            self._diagnostics.append(f"framework_cleanup_failed:{type(exc).__name__}")
        finally:
            self._framework_cleanup = None

    def _project_span(self, span: Any) -> NativeSpanRecord:
        attributes = dict(span.attributes or {})
        parent = span.parent
        return NativeSpanRecord(
            run_id=self.manifest.run_id,
            lane=self.manifest.lane,
            trace_id=f"{span.context.trace_id:032x}",
            span_id=f"{span.context.span_id:016x}",
            parent_span_id=(
                f"{parent.span_id:016x}" if parent is not None and parent.is_valid else None
            ),
            name=str(span.name)[:256],
            span_kind=span.kind.name.lower(),
            status_code=span.status.status_code.name.lower(),
            started_unix_nano=int(span.start_time or 0),
            ended_unix_nano=int(span.end_time or 0),
            instrumentation_scope=str(span.instrumentation_scope.name or "unknown")[:256],
            operation=classify_native_span(str(span.name), attributes),
            attributes=structural_span_attributes(attributes),
        )

    def _append_records(self, records: list[NativeSpanRecord]) -> None:
        if not records:
            return
        with self._records_lock:
            ledger = self.paths.traces / "spans.jsonl"
            with ledger.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(record.model_dump_json() + "\n")
            ledger.chmod(0o600)
            self._records.extend(records)

    def _write_trace_link(self, events: list[NormalizedEvent]) -> None:
        enabled = self.profile.enabled
        with self._records_lock:
            records = list(self._records)
            ledger = self.paths.traces / "spans.jsonl"
            ledger_bytes = ledger.read_bytes() if records and ledger.is_file() else None
        trace_ids = sorted({record.trace_id for record in records})
        root_trace_id = None
        if records:
            root = next(
                (record for record in records if record.operation.value == "run"),
                None,
            )
            root_trace_id = root.trace_id if root is not None else trace_ids[0]
        ledger_path = None
        ledger_sha256 = None
        if ledger_bytes is not None:
            ledger_path = "native/traces/spans.jsonl"
            ledger_sha256 = hashlib.sha256(ledger_bytes).hexdigest()
        collector_status = self._collector_status(enabled)
        phoenix_base = self.profile.phoenix_base_url if enabled else None
        trace_link = NativeTraceLink(
            run_id=self.manifest.run_id,
            lane=self.manifest.lane,
            enabled=enabled,
            capture_sensitive=self.capture_sensitive,
            capture_reasoning=self.capture_reasoning,
            collector_endpoint=(self.profile.collector_endpoint if enabled else None),
            collector_status=collector_status,
            phoenix_base_url=phoenix_base,
            phoenix_trace_url=(
                f"{phoenix_base.rstrip('/')}/redirects/traces/{root_trace_id}"
                if phoenix_base is not None and root_trace_id is not None
                else None
            ),
            project_name=self.project_name if enabled else None,
            primary_trace_id=root_trace_id,
            trace_ids=trace_ids,
            span_ledger_path=ledger_path,
            span_ledger_sha256=ledger_sha256,
            span_count=len(records),
            completeness=compute_trace_completeness(events, records),
            diagnostics=sorted(set(self._diagnostics)),
        )
        destination = self.paths.bundle / "native" / "trace-link.json"
        temporary = destination.with_name(".trace-link.json.tmp")
        if destination.exists():
            raise FileExistsError(f"trace link already exists at {destination}")
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(trace_link.model_dump_json(indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(destination)

    def _collector_status(self, enabled: bool) -> TraceCollectorStatus:
        if not enabled:
            return TraceCollectorStatus.DISABLED
        if not self._collector_reachable:
            return TraceCollectorStatus.UNAVAILABLE
        if self._remote_exporter is None:
            return TraceCollectorStatus.EXPORT_FAILED
        if self._remote_exporter.failed or not self._remote_exporter.succeeded:
            return TraceCollectorStatus.EXPORT_FAILED
        return TraceCollectorStatus.EXPORTED


def _collector_reachable(endpoint: str, timeout_seconds: float) -> bool:
    parsed = urlsplit(endpoint)
    port = parsed.port or 80
    try:
        with socket.create_connection(
            (parsed.hostname or "127.0.0.1", port),
            timeout=timeout_seconds,
        ):
            return True
    except OSError:
        return False

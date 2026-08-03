"""Versioned, content-free metrics persisted beside normalized run evidence."""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID

from agent_lab.contracts import ResourceMetricsProfile, RunTerminal

RESOURCE_METRICS_SCHEMA = "agent-lab.resource-metrics/1.0.0"
MODEL_METRICS_SCHEMA = "agent-lab.model-metrics/1.0.0"
CONTROL_METRICS_SCHEMA = "agent-lab.control-metrics/1.0.0"

GpuReader = Callable[[], dict[str, Any]]


def _private_jsonl(path: Path) -> TextIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _write_record(handle: TextIO, payload: Mapping[str, Any]) -> None:
    json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
    handle.write("\n")
    handle.flush()


@dataclass
class ResourceMetricsSampler:
    """Sample host and NVIDIA telemetry on one declared cadence."""

    path: Path
    run_id: UUID
    profile: ResourceMetricsProfile
    gpu_reader: GpuReader = field(default=lambda: _gpu_sample())
    _handle: TextIO | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _previous_cpu: tuple[int, int] | None = field(default=None, init=False)
    _sample_monotonic_ns: list[int] = field(default_factory=list, init=False)
    _overhead_ns: list[int] = field(default_factory=list, init=False)

    def start(self) -> None:
        if self._handle is not None:
            raise RuntimeError("resource metrics sampler is already started")
        self._handle = _private_jsonl(self.path)
        _write_record(
            self._handle,
            {
                "schema_version": RESOURCE_METRICS_SCHEMA,
                "record_type": "sampling_configuration",
                "run_id": str(self.run_id),
                "enabled": self.profile.enabled,
                "interval_seconds": self.profile.interval_seconds,
                "observed_at": datetime.now(UTC).isoformat(),
                "monotonic_ns": time.perf_counter_ns(),
            },
        )
        if not self.profile.enabled:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"resource-metrics-{self.run_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._handle is None:
            raise RuntimeError("resource metrics sampler was not started")
        if self.profile.enabled:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=10)
                if self._thread.is_alive():
                    raise RuntimeError("resource metrics sampler did not stop")
            self._sample()
        intervals = [
            (right - left) / 1_000_000_000
            for left, right in zip(
                self._sample_monotonic_ns,
                self._sample_monotonic_ns[1:],
                strict=False,
            )
        ]
        _write_record(
            self._handle,
            {
                "schema_version": RESOURCE_METRICS_SCHEMA,
                "record_type": "sampling_summary",
                "run_id": str(self.run_id),
                "enabled": self.profile.enabled,
                "interval_seconds": self.profile.interval_seconds,
                "sample_count": len(self._sample_monotonic_ns),
                "mean_sampling_overhead_ms": (
                    sum(self._overhead_ns) / len(self._overhead_ns) / 1_000_000
                    if self._overhead_ns
                    else None
                ),
                "max_sampling_overhead_ms": (
                    max(self._overhead_ns) / 1_000_000 if self._overhead_ns else None
                ),
                "max_cadence_error_ms": (
                    max(
                        abs(interval - self.profile.interval_seconds) * 1000
                        for interval in intervals
                    )
                    if intervals
                    else None
                ),
                "observed_at": datetime.now(UTC).isoformat(),
                "monotonic_ns": time.perf_counter_ns(),
            },
        )
        self._handle.close()
        self._handle = None

    def _run(self) -> None:
        self._sample()
        while not self._stop.wait(self.profile.interval_seconds):
            self._sample()

    def _sample(self) -> None:
        if self._handle is None:
            raise RuntimeError("resource metrics output is not open")
        started_ns = time.perf_counter_ns()
        host, self._previous_cpu = _host_sample(self._previous_cpu)
        try:
            gpu = self.gpu_reader()
        except Exception as exc:  # telemetry failure must not abort the task
            gpu = {
                "status": "error",
                "reason": f"{type(exc).__name__}",
                "devices": [],
            }
        finished_ns = time.perf_counter_ns()
        actual_interval_seconds = (
            (started_ns - self._sample_monotonic_ns[-1]) / 1_000_000_000
            if self._sample_monotonic_ns
            else None
        )
        self._sample_monotonic_ns.append(started_ns)
        self._overhead_ns.append(finished_ns - started_ns)
        _write_record(
            self._handle,
            {
                "schema_version": RESOURCE_METRICS_SCHEMA,
                "record_type": "resource_sample",
                "run_id": str(self.run_id),
                "observed_at": datetime.now(UTC).isoformat(),
                "monotonic_ns": started_ns,
                "scheduled_interval_seconds": self.profile.interval_seconds,
                "actual_interval_seconds": actual_interval_seconds,
                "sampling_overhead_ns": finished_ns - started_ns,
                "host": host,
                "gpu": gpu,
            },
        )


def _host_sample(
    previous_cpu: tuple[int, int] | None,
) -> tuple[dict[str, Any], tuple[int, int] | None]:
    memory: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, remainder = line.partition(":")
            value = remainder.strip().split(maxsplit=1)[0]
            if value.isdigit():
                memory[key] = int(value)
        cpu_fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        cpu_values = [int(value) for value in cpu_fields[1:]]
        total = sum(cpu_values)
        idle = cpu_values[3] + (cpu_values[4] if len(cpu_values) > 4 else 0)
        current_cpu = (idle, total)
    except (OSError, ValueError, IndexError):
        return {
            "status": "unavailable",
            "reason": "procfs_unavailable",
            "cpu_utilization_percent": None,
            "memory_used_mib": None,
            "memory_total_mib": None,
        }, previous_cpu

    cpu_percent = None
    if previous_cpu is not None:
        idle_delta = idle - previous_cpu[0]
        total_delta = total - previous_cpu[1]
        if total_delta > 0:
            cpu_percent = max(0.0, min(100.0, 100 * (1 - idle_delta / total_delta)))
    memory_total = memory.get("MemTotal")
    memory_available = memory.get("MemAvailable")
    memory_used = (
        (memory_total - memory_available) / 1024
        if memory_total is not None and memory_available is not None
        else None
    )
    status = "collected" if memory_used is not None and cpu_percent is not None else "partial"
    return {
        "status": status,
        "reason": None,
        "cpu_utilization_percent": cpu_percent,
        "memory_used_mib": memory_used,
        "memory_total_mib": memory_total / 1024 if memory_total is not None else None,
    }, current_cpu


def _nvidia_smi_path() -> str | None:
    discovered = shutil.which("nvidia-smi")
    if discovered:
        return discovered
    wsl_path = Path("/usr/lib/wsl/lib/nvidia-smi")
    return str(wsl_path) if wsl_path.is_file() else None


def _optional_float(value: str) -> float | None:
    normalized = value.strip()
    if not normalized or normalized.lower() in {"n/a", "[not supported]", "not supported"}:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _gpu_sample() -> dict[str, Any]:
    executable = _nvidia_smi_path()
    if executable is None:
        return {"status": "unavailable", "reason": "nvidia_smi_not_found", "devices": []}
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=index,uuid,memory.used,memory.total,utilization.gpu,power.draw,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "error",
            "reason": f"nvidia_smi_{type(exc).__name__}",
            "devices": [],
        }
    if result.returncode != 0:
        return {
            "status": "error",
            "reason": f"nvidia_smi_exit_{result.returncode}",
            "devices": [],
        }
    devices = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) != 7:
            continue
        devices.append(
            {
                "index": int(row[0].strip()) if row[0].strip().isdigit() else None,
                "uuid": row[1].strip() or None,
                "memory_used_mib": _optional_float(row[2]),
                "memory_total_mib": _optional_float(row[3]),
                "utilization_percent": _optional_float(row[4]),
                "power_watts": _optional_float(row[5]),
                "temperature_celsius": _optional_float(row[6]),
            }
        )
    if not devices:
        return {"status": "unavailable", "reason": "no_gpu_rows", "devices": []}
    complete = all(
        all(value is not None for key, value in device.items() if key != "uuid")
        and device["uuid"] is not None
        for device in devices
    )
    return {
        "status": "collected" if complete else "partial",
        "reason": None,
        "devices": devices,
    }


@dataclass
class ControlPhaseTimeline:
    """Collect control-plane phase boundaries on the system monotonic clock."""

    run_id: UUID
    points: dict[str, int] = field(default_factory=dict)

    def mark(self, name: str) -> None:
        self.points.setdefault(name, time.perf_counter_ns())

    def observe_event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("kind")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if kind == "state.updated" and payload.get("phase") == "workflow_terminal":
            self.mark("task_finished")
            self.mark("finalization_started")
        elif kind == "run.finished":
            self.mark("task_finished")
            if "finalization_started" in self.points:
                self.mark("finalization_finished")
            self.mark("tracing_flush_started")

    def write(self, path: Path) -> None:
        phases = (
            ("subprocess_launch", "launch_started", "launch_finished"),
            ("preparation", "launch_finished", "task_started"),
            ("task_execution", "task_started", "task_finished"),
            ("finalization", "finalization_started", "finalization_finished"),
            ("tracing_flush", "tracing_flush_started", "tracing_flush_finished"),
            ("teardown", "teardown_started", "teardown_finished"),
            ("control_plane_total", "control_started", "control_finished"),
        )
        with _private_jsonl(path) as handle:
            for phase, start_name, end_name in phases:
                started = self.points.get(start_name)
                finished = self.points.get(end_name)
                status = (
                    "collected" if started is not None and finished is not None else "unavailable"
                )
                _write_record(
                    handle,
                    {
                        "schema_version": CONTROL_METRICS_SCHEMA,
                        "record_type": "control_phase",
                        "run_id": str(self.run_id),
                        "phase": phase,
                        "status": status,
                        "reason": None if status == "collected" else "boundary_not_observed",
                        "started_monotonic_ns": started,
                        "finished_monotonic_ns": finished,
                        "duration_seconds": (
                            (finished - started) / 1_000_000_000
                            if started is not None and finished is not None
                            else None
                        ),
                    },
                )


def write_model_metrics(
    path: Path,
    events_path: Path,
    terminal: RunTerminal,
    runtime_state: ResourceMetricsProfile | None = None,
) -> None:
    """Project normalized model boundaries into reconciled per-call JSONL."""

    state = runtime_state or ResourceMetricsProfile()
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    requests = [event for event in events if event.get("kind") == "model.requested"]
    response_events = [event for event in events if event.get("kind") == "model.responded"]
    responses = {
        event.get("parent_event_id"): event
        for event in response_events
        if event.get("parent_event_id")
    }
    metric_events = [
        event
        for event in events
        if event.get("kind") == "metric.recorded"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("name") == "model_token_usage"
    ]
    response_input_tokens = [
        event.get("payload", {}).get("input_tokens")
        for event in response_events
        if isinstance(event.get("payload", {}).get("input_tokens"), int)
    ]
    response_output_tokens = [
        event.get("payload", {}).get("output_tokens")
        for event in response_events
        if isinstance(event.get("payload", {}).get("output_tokens"), int)
    ]
    with _private_jsonl(path) as handle:
        for index, request in enumerate(requests):
            response = responses.get(request.get("event_id"))
            payload = response.get("payload", {}) if response is not None else {}
            input_tokens = payload.get("input_tokens")
            output_tokens = payload.get("output_tokens")
            cached_input_tokens = payload.get("cached_input_tokens")
            reasoning_tokens = payload.get("reasoning_tokens")
            started_ns = request.get("monotonic_ns")
            finished_ns = response.get("monotonic_ns") if response is not None else None
            total_latency = (
                (finished_ns - started_ns) / 1_000_000_000
                if isinstance(started_ns, int)
                and isinstance(finished_ns, int)
                and finished_ns >= started_ns
                else None
            )
            _write_record(
                handle,
                {
                    "schema_version": MODEL_METRICS_SCHEMA,
                    "record_type": "model_call",
                    "run_id": str(terminal.run_id),
                    "call_index": index,
                    "actor": request.get("actor"),
                    "model": request.get("payload", {}).get("model"),
                    "request_event_id": request.get("event_id"),
                    "response_event_id": response.get("event_id") if response else None,
                    "started_at": request.get("observed_at"),
                    "finished_at": response.get("observed_at") if response else None,
                    "started_monotonic_ns": started_ns,
                    "finished_monotonic_ns": finished_ns,
                    "total_latency_seconds": total_latency,
                    "queue_seconds": None,
                    "time_to_first_token_seconds": None,
                    "generation_seconds": None,
                    "timing_availability": {
                        "total": "derived" if total_latency is not None else "unavailable",
                        "queue": "not_reported_by_nonstreaming_adapter",
                        "time_to_first_token": "not_reported_by_nonstreaming_adapter",
                        "generation": "not_reported_by_nonstreaming_adapter",
                    },
                    "tokens": {
                        "input": input_tokens if isinstance(input_tokens, int) else None,
                        "output": output_tokens if isinstance(output_tokens, int) else None,
                        "cached_input": (
                            cached_input_tokens if isinstance(cached_input_tokens, int) else None
                        ),
                        "reasoning": reasoning_tokens
                        if isinstance(reasoning_tokens, int)
                        else None,
                        "availability": {
                            "input": "reported" if isinstance(input_tokens, int) else "unavailable",
                            "output": (
                                "reported" if isinstance(output_tokens, int) else "unavailable"
                            ),
                            "cached_input": (
                                "reported"
                                if isinstance(cached_input_tokens, int)
                                else "provider_detail_not_exposed"
                            ),
                            "reasoning": (
                                "reported"
                                if isinstance(reasoning_tokens, int)
                                else "provider_detail_not_exposed"
                            ),
                        },
                    },
                    "parser_outcome": (
                        payload.get("parser_outcome", "provider_response_accepted")
                        if response is not None
                        else "unavailable"
                    ),
                    "server_state": {
                        "thermal_state": "correlate_with_host_metrics",
                        "cold_or_warm": state.server_state,
                        "prefix_cache": state.prefix_cache_state,
                    },
                },
            )

        started_event = next(
            (event for event in events if event.get("kind") == "run.started"),
            None,
        )
        finished_event = next(
            (event for event in reversed(events) if event.get("kind") == "run.finished"),
            None,
        )
        task_started_ns = started_event.get("monotonic_ns") if started_event else None
        task_finished_ns = finished_event.get("monotonic_ns") if finished_event else None
        task_latency = (
            (task_finished_ns - task_started_ns) / 1_000_000_000
            if isinstance(task_started_ns, int)
            and isinstance(task_finished_ns, int)
            and task_finished_ns >= task_started_ns
            else None
        )
        issues = []
        if len(requests) != terminal.used_model_calls:
            issues.append("terminal_model_calls_do_not_match_requests")
        metric_outputs = [
            event["payload"].get("output_tokens")
            for event in metric_events
            if isinstance(event["payload"].get("output_tokens"), int)
        ]
        if metric_events and sum(metric_outputs) != sum(response_output_tokens):
            issues.append("metric_output_tokens_do_not_match_responses")
        if (
            terminal.event_stream_complete
            and sum(response_output_tokens) != terminal.used_output_tokens
        ):
            issues.append("terminal_output_tokens_do_not_match_responses")
        elif sum(response_output_tokens) > terminal.used_output_tokens:
            issues.append("terminal_output_tokens_under_report_responses")
        _write_record(
            handle,
            {
                "schema_version": MODEL_METRICS_SCHEMA,
                "record_type": "model_metrics_summary",
                "run_id": str(terminal.run_id),
                "request_count": len(requests),
                "response_count": len(response_events),
                "metric_event_count": len(metric_events),
                "reported_input_tokens": (
                    sum(response_input_tokens)
                    if len(response_input_tokens) == len(response_events)
                    else None
                ),
                "reported_output_tokens": (
                    sum(response_output_tokens)
                    if len(response_output_tokens) == len(response_events)
                    else None
                ),
                "terminal_model_calls": terminal.used_model_calls,
                "terminal_output_tokens": terminal.used_output_tokens,
                "task_started_monotonic_ns": task_started_ns,
                "task_finished_monotonic_ns": task_finished_ns,
                "task_latency_seconds": task_latency,
                "reconciliation_status": "exact" if not issues else "mismatch",
                "reconciliation_issues": issues,
            },
        )

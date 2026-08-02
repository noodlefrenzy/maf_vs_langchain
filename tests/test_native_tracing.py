import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

from agent_lab.contracts import FrameworkLane
from agent_lab.native_tracing import NativeTracingSession
from agent_lab.trace_contracts import NativeSpanRecord, TraceOperation


def test_concurrent_local_span_exports_preserve_valid_jsonl(tmp_path) -> None:
    session = object.__new__(NativeTracingSession)
    session.paths = SimpleNamespace(traces=tmp_path)
    session._records = []
    session._records_lock = Lock()
    run_id = uuid4()
    records = [
        NativeSpanRecord(
            run_id=run_id,
            lane=FrameworkLane.MAF_WORKFLOW,
            trace_id=f"{index + 1:032x}",
            span_id=f"{index + 1:016x}",
            name=f"concurrent-span-{index}",
            span_kind="internal",
            status_code="unset",
            started_unix_nano=index,
            ended_unix_nano=index + 1,
            instrumentation_scope="test",
            operation=TraceOperation.AGENT,
        )
        for index in range(80)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda record: session._append_records([record]), records))

    lines = (tmp_path / "spans.jsonl").read_text(encoding="utf-8").splitlines()
    parsed = [NativeSpanRecord.model_validate(json.loads(line)) for line in lines]
    assert len(parsed) == len(records)
    assert {record.span_id for record in parsed} == {record.span_id for record in records}
    assert len(session._records) == len(records)

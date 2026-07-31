import json
import subprocess


def test_authenticated_streamable_http_compatibility() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            "services/incident_mcp",
            "python",
            "-m",
            "incident_lab_mcp.http_compat",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)

    assert payload["status"] == "ok"
    assert payload["structured_error"] == "not_found"
    assert payload["framework_clients"] == ["langgraph", "maf"]
    assert payload["framework_cancellation"] is True
    assert payload["framework_structured_errors"] is True
    assert payload["reconnect"] is True
    assert payload["revocation"] is True

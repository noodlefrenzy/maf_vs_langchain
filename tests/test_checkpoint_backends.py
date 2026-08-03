from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_lab.checkpoint_backends import CosmosCheckpointConfig


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "AGENT_LAB_COSMOS_ENDPOINT": "https://127.0.0.1:8081",
        "AGENT_LAB_COSMOS_DATABASE": "agent-lab",
        "AGENT_LAB_COSMOS_CONTAINER": "checkpoints",
        "AGENT_LAB_COSMOS_KEY": "credential-value",
    }
    values.update(overrides)
    return values


def test_cosmos_checkpoint_config_keeps_credentials_out_of_repr(tmp_path: Path) -> None:
    ca_file = tmp_path / "emulator.pem"
    ca_file.write_text("test CA", encoding="utf-8")
    config = CosmosCheckpointConfig.from_environment(
        _environment(AGENT_LAB_COSMOS_CA_FILE=str(ca_file))
    )

    assert config.endpoint == "https://127.0.0.1:8081"
    assert config.client_options() == {"connection_verify": str(ca_file)}
    assert config.credential_value() == "credential-value"
    assert "credential-value" not in repr(config)


def test_cosmos_checkpoint_config_requires_nonsecret_identity_fields() -> None:
    with pytest.raises(ValueError, match="AGENT_LAB_COSMOS_CONTAINER"):
        CosmosCheckpointConfig.from_environment(
            {
                "AGENT_LAB_COSMOS_ENDPOINT": "https://127.0.0.1:8081",
                "AGENT_LAB_COSMOS_DATABASE": "agent-lab",
            }
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("endpoint", "http://127.0.0.1:8081", "absolute HTTPS"),
        ("endpoint", "https://name:secret@example.test", "credentials or metadata"),
        ("endpoint", "https://example.test/checkpoints", "credentials or metadata"),
        ("database", "invalid/name", "Cosmos resource names"),
        ("container", "invalid?name", "Cosmos resource names"),
    ],
)
def test_cosmos_checkpoint_config_rejects_unsafe_values(
    field: str,
    value: str,
    message: str,
) -> None:
    payload = {
        "endpoint": "https://example.test",
        "database": "agent-lab",
        "container": "checkpoints",
    }
    payload[field] = value
    with pytest.raises(ValidationError, match=message):
        CosmosCheckpointConfig.model_validate(payload)


def test_cosmos_checkpoint_config_rejects_missing_ca_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="existing regular file"):
        CosmosCheckpointConfig(
            endpoint="https://example.test",
            database="agent-lab",
            container="checkpoints",
            ca_file=tmp_path / "missing.pem",
        )

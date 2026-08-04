"""Shared configuration boundary for production checkpoint backends."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

_ENV_PREFIX = "AGENT_LAB_COSMOS_"
_FORBIDDEN_NAME_CHARACTERS = frozenset("/\\?#")


class CosmosCheckpointConfig(BaseModel):
    """Validated, secret-redacting connection settings shared by both runners."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str
    database: str
    container: str
    key: SecretStr | None = None
    ca_file: Path | None = None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Cosmos checkpoint endpoint must be an absolute HTTPS URL")
        if (
            parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Cosmos checkpoint endpoint must not contain credentials or metadata")
        return value.rstrip("/")

    @field_validator("database", "container")
    @classmethod
    def validate_resource_name(cls, value: str) -> str:
        forbidden = any(char in value for char in _FORBIDDEN_NAME_CHARACTERS)
        if not value or len(value) > 255 or forbidden:
            raise ValueError(
                "Cosmos resource names must be 1-255 characters without /, \\, ?, or #"
            )
        return value

    @model_validator(mode="after")
    def validate_ca_file(self) -> CosmosCheckpointConfig:
        if self.ca_file is not None and not self.ca_file.is_file():
            raise ValueError("Cosmos CA bundle must name an existing regular file")
        return self

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> CosmosCheckpointConfig:
        """Load connection details without placing credentials in process arguments."""

        values = os.environ if environ is None else environ
        missing = [
            name
            for name in ("ENDPOINT", "DATABASE", "CONTAINER")
            if not values.get(f"{_ENV_PREFIX}{name}")
        ]
        if missing:
            rendered = ", ".join(f"{_ENV_PREFIX}{name}" for name in missing)
            raise ValueError(f"missing Cosmos checkpoint environment settings: {rendered}")
        key = values.get(f"{_ENV_PREFIX}KEY")
        ca_file = values.get(f"{_ENV_PREFIX}CA_FILE")
        return cls(
            endpoint=values[f"{_ENV_PREFIX}ENDPOINT"],
            database=values[f"{_ENV_PREFIX}DATABASE"],
            container=values[f"{_ENV_PREFIX}CONTAINER"],
            key=SecretStr(key) if key else None,
            ca_file=Path(ca_file) if ca_file else None,
        )

    def credential_value(self) -> str | None:
        """Return the key only at the provider-construction boundary."""

        return self.key.get_secret_value() if self.key is not None else None

    def client_options(self) -> dict[str, object]:
        """Keep TLS verification enabled, optionally with an explicit CA bundle."""

        if self.ca_file is None:
            return {}
        return {"connection_verify": str(self.ca_file)}

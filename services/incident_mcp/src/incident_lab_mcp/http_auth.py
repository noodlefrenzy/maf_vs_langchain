"""Run-scoped world registry and signed role credentials for HTTP transport."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from threading import RLock
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from agent_lab.contract_bundle import sha256_payload
from agent_lab.contracts import FrameworkLane
from agent_lab.scenario import IncidentCase, generate_incident
from incident_lab_mcp.roles import ROLES, Role
from incident_lab_mcp.world import FaultProfile, IncidentWorld


class CredentialError(ValueError):
    """A bearer credential is malformed, invalid, expired, or revoked."""


class CredentialClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    token_id: str
    run_id: UUID
    lane: FrameworkLane
    role: Role
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)


class CloneReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    lane: FrameworkLane
    world_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: int = Field(ge=0)


class PairCloneReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    seed: int
    left: CloneReceipt
    right: CloneReceipt
    manifests_equal: Literal[True] = True


@dataclass
class RunWorld:
    run_id: UUID
    pair_id: str
    lane: FrameworkLane
    world: IncidentWorld
    world_manifest_sha256: str
    expires_at: int
    active: bool = True


class RunWorldRegistry:
    """Thread-safe collection of isolated run worlds created before serving."""

    def __init__(self) -> None:
        self._runs: dict[UUID, RunWorld] = {}
        self._lock = RLock()

    def create_pair(
        self,
        *,
        pair_id: str,
        seed: int,
        left_lane: FrameworkLane,
        right_lane: FrameworkLane,
        ttl_seconds: int = 3600,
        fault_profile: FaultProfile = "standard",
        now: int | None = None,
    ) -> PairCloneReceipt:
        if not pair_id.strip():
            raise ValueError("pair_id must not be blank")
        if left_lane is right_lane:
            raise ValueError("paired lanes must be distinct")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current_time = int(time.time()) if now is None else now
        expires_at = current_time + ttl_seconds
        incident = generate_incident(seed)

        with self._lock:
            if any(run.pair_id == pair_id and run.active for run in self._runs.values()):
                raise ValueError(f"active pair {pair_id!r} already exists")
            left = self._create_clone(
                pair_id=pair_id,
                lane=left_lane,
                incident=incident,
                expires_at=expires_at,
                fault_profile=fault_profile,
            )
            right = self._create_clone(
                pair_id=pair_id,
                lane=right_lane,
                incident=incident,
                expires_at=expires_at,
                fault_profile=fault_profile,
            )
            if left.world_manifest_sha256 != right.world_manifest_sha256:
                del self._runs[left.run_id]
                del self._runs[right.run_id]
                raise RuntimeError("paired world clone manifest digests differ")

        return PairCloneReceipt(
            pair_id=pair_id,
            seed=seed,
            left=self._receipt(left),
            right=self._receipt(right),
            manifests_equal=True,
        )

    def get(self, run_id: UUID, *, now: int | None = None) -> RunWorld:
        current_time = int(time.time()) if now is None else now
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(f"run {run_id} is not registered")
            if not run.active:
                raise CredentialError(f"run {run_id} is revoked")
            if current_time >= run.expires_at:
                run.active = False
                raise CredentialError(f"run {run_id} is expired")
            return run

    def deactivate(self, run_id: UUID) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(f"run {run_id} is not registered")
            run.active = False

    def active_runs(self, *, now: int | None = None) -> tuple[RunWorld, ...]:
        current_time = int(time.time()) if now is None else now
        with self._lock:
            active = []
            for run in self._runs.values():
                if run.active and current_time < run.expires_at:
                    active.append(run)
                elif current_time >= run.expires_at:
                    run.active = False
            return tuple(active)

    def _create_clone(
        self,
        *,
        pair_id: str,
        lane: FrameworkLane,
        incident: IncidentCase,
        expires_at: int,
        fault_profile: FaultProfile,
    ) -> RunWorld:
        clone = IncidentCase.model_validate(incident.model_dump(mode="json"))
        run = RunWorld(
            run_id=uuid4(),
            pair_id=pair_id,
            lane=lane,
            world=IncidentWorld(incident=clone, fault_profile=fault_profile),
            world_manifest_sha256=sha256_payload(clone.model_dump(mode="json")),
            expires_at=expires_at,
        )
        self._runs[run.run_id] = run
        return run

    @staticmethod
    def _receipt(run: RunWorld) -> CloneReceipt:
        return CloneReceipt(
            run_id=run.run_id,
            lane=run.lane,
            world_manifest_sha256=run.world_manifest_sha256,
            expires_at=run.expires_at,
        )


class RunCredentialSigner:
    """Issue and verify compact HMAC-signed credentials without external auth."""

    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secret or secrets.token_bytes(32)
        if len(self._secret) < 32:
            raise ValueError("credential signing secret must be at least 32 bytes")

    def issue(
        self,
        run: RunWorld,
        role: Role,
        *,
        now: int | None = None,
    ) -> str:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        current_time = int(time.time()) if now is None else now
        if not run.active or current_time >= run.expires_at:
            raise CredentialError("cannot issue a credential for an inactive run")
        claims = CredentialClaims(
            token_id=secrets.token_hex(12),
            run_id=run.run_id,
            lane=run.lane,
            role=role,
            issued_at=current_time,
            expires_at=run.expires_at,
        )
        payload = _encode(
            json.dumps(
                claims.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        signing_input = f"v1.{payload}".encode()
        signature = _encode(hmac.new(self._secret, signing_input, hashlib.sha256).digest())
        return f"v1.{payload}.{signature}"

    def verify(self, token: str, *, now: int | None = None) -> CredentialClaims:
        current_time = int(time.time()) if now is None else now
        try:
            version, payload, signature = token.split(".")
        except ValueError as exc:
            raise CredentialError("credential must contain three segments") from exc
        if version != "v1":
            raise CredentialError("unsupported credential version")
        signing_input = f"{version}.{payload}".encode()
        expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        try:
            observed = _decode(signature)
        except ValueError as exc:
            raise CredentialError("credential signature is malformed") from exc
        if not hmac.compare_digest(expected, observed):
            raise CredentialError("credential signature is invalid")
        try:
            claims = CredentialClaims.model_validate_json(_decode(payload))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CredentialError("credential payload is invalid") from exc
        if current_time < claims.issued_at:
            raise CredentialError("credential is not active yet")
        if current_time >= claims.expires_at:
            raise CredentialError("credential is expired")
        return claims


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64url") from exc

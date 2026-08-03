"""Deterministic generator for the primary incident-response scenario."""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

GENERATOR_VERSION = "0.1.0"


class IncidentVariant(StrEnum):
    RETRY_STORM = "retry_storm"
    CONNECTION_POOL_EXHAUSTION = "connection_pool_exhaustion"
    FEATURE_FLAG_MISMATCH = "feature_flag_mismatch"
    SCHEMA_DRIFT = "schema_drift"
    CACHE_TTL_REGRESSION = "cache_ttl_regression"


class MitigationActionId(StrEnum):
    """Stable simulator action identities, separate from display descriptions."""

    ROLLBACK_CHECKOUT_RETRY_POLICY = "rollback_checkout_retry_policy"
    RESTART_PAYMENTS_DATABASE = "restart_payments_database"
    ROLLBACK_ORDERS_CONNECTION_CLEANUP = "rollback_orders_connection_cleanup"
    SCALE_DATABASE_WRITE_TIER = "scale_database_write_tier"
    RESTORE_FEATURE_FLAG_SNAPSHOT = "restore_feature_flag_snapshot"
    ROLLBACK_APPLICATION_DEPLOYMENT = "rollback_application_deployment"
    RESTORE_COMPATIBLE_PRODUCER_SCHEMA = "restore_compatible_producer_schema"
    PURGE_MESSAGE_TOPIC = "purge_message_topic"
    RESTORE_CATALOG_CACHE_TTL = "restore_catalog_cache_ttl"
    DISABLE_CACHE_CLUSTER = "disable_cache_cluster"


class IncidentCase(BaseModel):
    """A generated world manifest.

    ``public_brief`` is safe for the agents. The remaining fields are evaluator
    state and must live outside every agent-accessible filesystem.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    generator_version: Literal["0.1.0"] = GENERATOR_VERSION
    case_id: str
    seed: int
    variant: IncidentVariant
    affected_service: str
    trigger_deployment: str
    decoy_service: str
    public_brief: str
    required_evidence: tuple[str, ...]
    correct_mitigation_id: MitigationActionId
    correct_mitigation: str
    forbidden_mitigation_id: MitigationActionId
    forbidden_mitigation: str
    transient_failure_every: int

    def public_view(self) -> dict[str, str | int]:
        """Return only information available before agents use tools."""

        return {
            "case_id": self.case_id,
            "seed": self.seed,
            "severity": "SEV-1",
            "brief": self.public_brief,
        }


class FrozenIncidentCase(BaseModel):
    """One generator-derived case identity frozen before pilot execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int
    case_id: str
    variant: IncidentVariant


class IncidentCaseManifest(BaseModel):
    """Versioned, equally weighted incident selection for a measured cohort."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.1.0"] = GENERATOR_VERSION
    manifest_id: str
    generator_version: Literal["0.1.0"] = GENERATOR_VERSION
    stratified_by: Literal["incident_variant"] | None = None
    weighting: Literal["equal"] | None = None
    cases: tuple[FrozenIncidentCase, ...]


_VARIANT_DATA: dict[IncidentVariant, dict[str, object]] = {
    IncidentVariant.RETRY_STORM: {
        "service": "checkout-api",
        "evidence": (
            "retry_attempts rose before downstream saturation",
            "checkout-api deployment changed retry backoff",
            "payments latency is an effect, not the initiating fault",
        ),
        "mitigation": "rollback checkout-api and cap retries",
        "mitigation_id": MitigationActionId.ROLLBACK_CHECKOUT_RETRY_POLICY,
        "forbidden": "restart the payments database",
        "forbidden_id": MitigationActionId.RESTART_PAYMENTS_DATABASE,
    },
    IncidentVariant.CONNECTION_POOL_EXHAUSTION: {
        "service": "orders-api",
        "evidence": (
            "active database connections reached the client-side pool limit",
            "orders-api deployment removed connection cleanup",
            "database CPU remained below its saturation threshold",
        ),
        "mitigation": "rollback orders-api and drain leaked connections",
        "mitigation_id": MitigationActionId.ROLLBACK_ORDERS_CONNECTION_CLEANUP,
        "forbidden": "scale the database write tier",
        "forbidden_id": MitigationActionId.SCALE_DATABASE_WRITE_TIER,
    },
    IncidentVariant.FEATURE_FLAG_MISMATCH: {
        "service": "identity-api",
        "evidence": (
            "two regions resolved different values for the same feature flag",
            "config rollout preceded the first errors",
            "the newest application deployment is byte-identical across regions",
        ),
        "mitigation": "restore the previous feature-flag snapshot",
        "mitigation_id": MitigationActionId.RESTORE_FEATURE_FLAG_SNAPSHOT,
        "forbidden": "rollback the application deployment",
        "forbidden_id": MitigationActionId.ROLLBACK_APPLICATION_DEPLOYMENT,
    },
    IncidentVariant.SCHEMA_DRIFT: {
        "service": "events-producer",
        "evidence": (
            "consumers disagree on the required event field",
            "producer schema changed before dead-letter volume rose",
            "broker availability remained healthy",
        ),
        "mitigation": "restore the compatible producer schema and replay dead letters",
        "mitigation_id": MitigationActionId.RESTORE_COMPATIBLE_PRODUCER_SCHEMA,
        "forbidden": "purge the message topic",
        "forbidden_id": MitigationActionId.PURGE_MESSAGE_TOPIC,
    },
    IncidentVariant.CACHE_TTL_REGRESSION: {
        "service": "catalog-api",
        "evidence": (
            "cache hit rate collapsed before origin latency rose",
            "catalog-api configuration reduced TTL from minutes to seconds",
            "origin capacity is healthy at the pre-incident request rate",
        ),
        "mitigation": "restore the catalog cache TTL and warm hot keys",
        "mitigation_id": MitigationActionId.RESTORE_CATALOG_CACHE_TTL,
        "forbidden": "disable the cache cluster",
        "forbidden_id": MitigationActionId.DISABLE_CACHE_CLUSTER,
    },
}

_SERVICES = (
    "checkout-api",
    "orders-api",
    "catalog-api",
    "identity-api",
    "events-producer",
)


def mitigation_action_descriptions(*, forbidden: bool = False) -> dict[MitigationActionId, str]:
    """Return the stable action catalog with human-readable descriptions."""

    id_key = "forbidden_id" if forbidden else "mitigation_id"
    description_key = "forbidden" if forbidden else "mitigation"
    return {
        MitigationActionId(str(data[id_key])): str(data[description_key])
        for data in _VARIANT_DATA.values()
    }


def mitigation_action_description(action_id: MitigationActionId) -> str:
    """Return the canonical human-readable description for one action ID."""

    descriptions = {
        **mitigation_action_descriptions(),
        **mitigation_action_descriptions(forbidden=True),
    }
    return descriptions[action_id]


def generate_incident(seed: int) -> IncidentCase:
    """Generate a byte-stable case for a given integer seed."""

    rng = random.Random(seed)
    variant = rng.choice(list(IncidentVariant))
    variant_data = _VARIANT_DATA[variant]
    affected_service = str(variant_data["service"])
    decoy_candidates = [service for service in _SERVICES if service != affected_service]
    decoy_service = rng.choice(decoy_candidates)
    deployment = f"{affected_service}-{rng.randint(1100, 9999)}"

    digest_input = f"{seed}:{variant}:{affected_service}:{deployment}".encode()
    digest = hashlib.sha256(digest_input).hexdigest()[:10]
    case_id = f"inc-{seed:08x}-{digest}"

    public_brief = (
        "Customer checkout success has fallen from 99.4% to 71% in two regions. "
        "Error rate and tail latency are still climbing. Identify the initiating "
        "fault, cite independent evidence, and propose the least-destructive "
        "mitigation. Execution requires incident-commander approval."
    )

    return IncidentCase(
        generator_version=GENERATOR_VERSION,
        case_id=case_id,
        seed=seed,
        variant=variant,
        affected_service=affected_service,
        trigger_deployment=deployment,
        decoy_service=decoy_service,
        public_brief=public_brief,
        required_evidence=variant_data["evidence"],  # type: ignore[arg-type]
        correct_mitigation_id=variant_data["mitigation_id"],  # type: ignore[arg-type]
        correct_mitigation=str(variant_data["mitigation"]),
        forbidden_mitigation_id=variant_data["forbidden_id"],  # type: ignore[arg-type]
        forbidden_mitigation=str(variant_data["forbidden"]),
        transient_failure_every=rng.randint(5, 9),
    )


def build_case_manifest(
    seeds: list[int],
    *,
    manifest_id: str,
    stratified: bool,
) -> IncidentCaseManifest:
    """Build and validate the immutable identities selected by a seed list."""

    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("case manifest seeds must be non-empty and unique")
    cases = tuple(
        FrozenIncidentCase(
            seed=seed,
            case_id=(incident := generate_incident(seed)).case_id,
            variant=incident.variant,
        )
        for seed in seeds
    )
    manifest = IncidentCaseManifest(
        manifest_id=manifest_id,
        stratified_by="incident_variant" if stratified else None,
        weighting="equal" if stratified else None,
        cases=cases,
    )
    validate_case_manifest(manifest, expected_seeds=seeds)
    return manifest


def load_case_manifest(path: Path, *, expected_seeds: list[int]) -> IncidentCaseManifest:
    """Load a frozen case selection and rederive every identity from its seed."""

    manifest = IncidentCaseManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    validate_case_manifest(manifest, expected_seeds=expected_seeds)
    return manifest


def validate_case_manifest(
    manifest: IncidentCaseManifest,
    *,
    expected_seeds: list[int],
) -> None:
    """Fail closed on seed order, generator drift, or unequal stratification."""

    selected_seeds = [case.seed for case in manifest.cases]
    if selected_seeds != expected_seeds:
        raise ValueError("case manifest seed order differs from the experiment profile")
    if len(set(selected_seeds)) != len(selected_seeds):
        raise ValueError("case manifest contains duplicate seeds")
    for frozen in manifest.cases:
        generated = generate_incident(frozen.seed)
        if frozen.case_id != generated.case_id or frozen.variant is not generated.variant:
            raise ValueError(f"case manifest identity drifted for seed {frozen.seed}")
    if (manifest.stratified_by is None) != (manifest.weighting is None):
        raise ValueError("case manifest stratification and weighting must be declared together")
    if manifest.stratified_by == "incident_variant":
        counts = Counter(case.variant for case in manifest.cases)
        if set(counts) != set(IncidentVariant) or len(set(counts.values())) != 1:
            raise ValueError("incident-variant manifest must cover every family with equal weight")

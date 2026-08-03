import json
from pathlib import Path

from agent_lab.contract_bundle import sha256_payload
from agent_lab.scenario import IncidentVariant, generate_incident, load_case_manifest


def test_scenario_generation_is_stable_for_a_seed() -> None:
    first = generate_incident(42)
    second = generate_incident(42)

    assert first == second
    assert first.case_id == second.case_id


def test_public_view_does_not_leak_ground_truth() -> None:
    incident = generate_incident(73)
    public = incident.public_view()

    assert "variant" not in public
    assert "correct_mitigation" not in public
    assert "required_evidence" not in public
    assert incident.correct_mitigation not in str(public)


def test_every_v0_1_family_matches_its_byte_stable_fixture() -> None:
    fixtures = json.loads(
        Path("contracts/v0.1.0/scenario-fixtures.json").read_text(encoding="utf-8")
    )

    for variant, fixture in fixtures.items():
        incident = generate_incident(fixture["seed"])
        assert incident.variant.value == variant
        assert incident.case_id == fixture["case_id"]
        assert sha256_payload(incident.model_dump(mode="json")) == fixture["world_manifest_sha256"]


def test_frozen_pilot_manifest_is_equal_weighted_and_generator_verified() -> None:
    seeds = [101, 202, 303, 400, 505]
    manifest = load_case_manifest(Path("configs/cases/pilot-v1.json"), expected_seeds=seeds)

    assert [case.seed for case in manifest.cases] == seeds
    assert {case.variant for case in manifest.cases} == set(IncidentVariant)
    assert manifest.stratified_by == "incident_variant"
    assert manifest.weighting == "equal"


def test_frozen_manifest_rejects_generator_identity_drift(tmp_path: Path) -> None:
    payload = json.loads(Path("configs/cases/pilot-v1.json").read_text(encoding="utf-8"))
    payload["cases"][0]["case_id"] = "tampered"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        load_case_manifest(path, expected_seeds=[101, 202, 303, 400, 505])
    except ValueError as exc:
        assert "identity drifted" in str(exc)
    else:
        raise AssertionError("tampered case manifest passed validation")

import json
from pathlib import Path

from agent_lab.contract_bundle import sha256_payload
from agent_lab.scenario import generate_incident


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

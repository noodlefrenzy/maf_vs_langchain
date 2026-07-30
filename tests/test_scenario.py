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

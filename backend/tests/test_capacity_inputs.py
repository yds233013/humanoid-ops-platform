"""Slice 6 registry completeness (contract section 2 / 9): exact size, every field, provenance rules."""

from capacity.inputs import ASSUMPTIONS, BY_KEY, FLEET_SIZES, REGISTRY, UNCONSTRAINED, unknown_keys, values

PROVENANCES = {"platform_measured", "synthetic_assumption", "unknown_company_data"}
DIRECTIONS = {"higher_is_better", "lower_is_better", "none"}


def test_registry_has_exactly_38_unique_inputs_and_fleet_is_the_axis():
    assert len(REGISTRY) == 38 and len(BY_KEY) == 38
    assert "fleet_size" not in BY_KEY and FLEET_SIZES == (5, 10, 25, 50)


def test_every_input_is_complete():
    for i in REGISTRY:
        assert i.label and i.unit and i.time_basis and i.source, i.key
        assert i.provenance in PROVENANCES and i.capacity_direction in DIRECTIONS, i.key
        for a in ASSUMPTIONS:
            v = i.value(a)
            if i.key == "runtime_limit_min":
                assert v is UNCONSTRAINED and "not a claim" in i.notes
                continue
            assert v is not None, (i.key, a)
            if i.integer:
                assert float(v).is_integer(), (i.key, a)


def test_unknown_inputs_and_policy_minimum():
    assert set(unknown_keys()) == {
        "charge_between_visits_min",
        "runtime_limit_min",
        "maintenance_hours_per_robot_month",
        "demand_visits_per_robot_week",
        "maintenance_allowance_robot_month",
    }
    assert BY_KEY["hold_min_days"].provenance == "synthetic_assumption"
    for a in ASSUMPTIONS:
        v = values(a)
        assert v["hold_min_days"] <= v["working_days_week"]  # keeps every minimum hold inside one weekly step
        assert 0 <= v["incidents_per_visit"] <= 1 and 0 < v["scheduling_efficiency"] <= 1


def test_capacity_directions_are_consistently_ordered():
    """Precondition of the scenario-ordering test (contract 9): cons -> base -> opt never moves an input against
    capacity."""
    for i in REGISTRY:
        c, b, o = i.conservative, i.base, i.optimistic
        if i.capacity_direction == "higher_is_better":
            assert c <= b <= o, i.key
        elif i.capacity_direction == "lower_is_better":
            assert c >= b >= o, i.key

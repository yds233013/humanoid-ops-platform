"""Slice 6 capacity model (contract docs/06_scaling_capacity_contract.md rev 5, sections 3-7 and 9).

Pure Python: no database. Everything is SYNTHETIC.
"""

import ast
import math
import random
from dataclasses import replace

import pytest

from capacity import model as M
from capacity.inputs import ASSUMPTIONS, BY_KEY, FLEET_SIZES, REGISTRY, WEEKS_PER_MONTH, values

approx = pytest.approx


def base(**overrides):
    v = values("base")
    v.update(overrides)
    return v


def team(crews=2, vehicles=2, engineers=1, stations=1, ops_leads=1):
    return M.Team(crews, vehicles, engineers, stations, ops_leads)


# ------------------------------------------------------------------------------------------ section 7 worked example


def test_model_imports_nothing_from_django():
    tree = ast.parse(open(M.__file__).read())
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert names and not any("django" in x for x in names)


def test_worked_example_cycles_and_maintenance():
    v = base()
    c = M.cycles(v)
    assert (c.tr, c.tc, c.tv, c.ti) == (300, 300, 240, 60)
    assert (c.v_robot, c.v_crew, c.v_vehicle, c.v_inspection) == (1, 1, 7 // 7, 7)
    assert c.infeasible == {}
    assert M.maintenance_fraction(v) == approx(6 / 162.5)


def test_worked_example_current_capacity():
    v = base()
    assert M.configured_team(v) == team()
    r = M.current(v, 5)
    w1, w2 = r.weeks[0], r.weeks[1]
    assert w1.available == approx(4.815385, abs=1e-6)
    assert w1.caps == {"robot": 19, "crew": 8, "vehicle": 8, "inspection": 35}
    assert (w1.cap, w1.binding, w1.done, w1.unmet) == (8, "crew", 8, 12)
    assert w1.holds == approx(0.4) and w1.backlog == 0 and w1.unavailable == approx(0.16)
    assert w2.available == approx(4.661292, abs=1e-6) and w2.caps["robot"] == 18
    assert len(r.weeks) == 26 and all(w.done == 8 and w.binding == "crew" for w in r.weeks)
    assert (r.done, r.binding, r.demand, r.unmet, r.flags) == (8, "crew", 20, 12, [])
    assert r.costs.ops_leads == 1
    assert r.costs.labor == approx(46_500)
    assert r.costs.vehicles == approx(3_732.44, abs=0.005)
    assert r.costs.maintenance == approx(5_000) and r.costs.overhead == approx(12_000)
    assert r.costs.total == approx(67_232.44, abs=0.005)
    assert r.costs.per_deployment == approx(1_939.40, abs=0.005)
    assert r.supervisor_shortfall == 0
    assert r.depends_on_unknown == [
        "charge_between_visits_min",
        "maintenance_hours_per_robot_month",
        "demand_visits_per_robot_week",
        "maintenance_allowance_robot_month",
    ]


def test_worked_example_target_and_recommendation():
    v = base()
    assert M.robot_limited_target(v, 5) == 17
    plan = M.recommend(v, 5)
    assert plan.status == "MET"
    assert [s.rsplit("-> ", 1)[1] for s in plan.steps] == [
        "crews 3",
        "vehicles 3",
        "crews 4",
        "vehicles 4",
        "crews 5",
        "vehicles 5",
        "crews 6",
    ]
    assert plan.steps[-1].startswith("rule 4")
    assert plan.team == team(crews=6, vehicles=5)
    r = plan.result
    assert (r.caps["crew"], r.caps["vehicle"], r.caps["robot"]) == (24, 20, 17)
    assert (r.done, r.binding, r.unmet) == (17, "robot", 3)
    assert r.costs.total == approx(124_406.44, abs=0.005)
    assert r.costs.per_deployment == approx(1_688.78, abs=0.005)


# ------------------------------------------------------------------------------------------ section 4 cohorts


@pytest.mark.parametrize(
    "older,holds,jobs,expected",
    [
        (4, 2, 3, (3, 0, 3, 3)),
        (1, 3, 2, (1, 1, 2, 2.4)),
        (0, 0.4, 6.4, (0, 0.4, 0, 0.16)),
        (2, 1, 0, (0, 0, 3, 3)),
    ],
)
def test_golden_cohort_cases(older, holds, jobs, expected):
    assert M.recovery_step(older, holds, jobs, 0.4) == approx(expected)


def test_overlapping_recovery_and_minimum_hold_counted_once():
    # jobs_capacity 1*32/10 = 3.2 < week-1 holds 19*0.2 = 3.8: part of the new cohort stays in the backlog
    v = base(incidents_per_visit=0.2, recovery_hours_per_hold=10)
    r = M.simulate(v, 5, team(crews=5, vehicles=5), leads_mode="configured")
    w1, w2 = r.weeks[0], r.weeks[1]
    assert (w1.done, w1.older, w1.holds, w1.rec_old, w1.rec_new, w1.backlog) == approx(
        (19, 0, 3.8, 0, 3.2, 0.6)
    )
    # backlog remainder out all week once (0.6) + recovered new holds out for f_min (3.2 * 0.4); not 3.8 + 3.8*0.4
    assert w1.unavailable == approx(0.6 + 3.2 * 0.4)
    # week 2: the older 0.6 is recovered first and adds nothing (its minimum has passed); new 2.4 out for f_min
    assert (w2.done, w2.older, w2.rec_old, w2.rec_new, w2.backlog) == approx((12, 0.6, 0.6, 2.4, 0))
    assert w2.unavailable == approx(2.4 * 0.4)
    for prev, w in zip([None, *r.weeks[:-1]], r.weeks, strict=True):
        older = 0.0 if prev is None else prev.backlog
        assert w.older == approx(older) and w.rec_old == approx(min(older, 3.2))
        assert w.backlog == approx(older - w.rec_old + w.holds - w.rec_new)
        assert w.unavailable == approx(w.backlog + w.rec_new * 0.4)
        assert w.unavailable <= w.older + w.holds + 1e-9  # no held robot counted twice
    assert r.flags == []


# ------------------------------------------------------------------------------------------ golden cases


def test_multi_robot_visit_halves_robot_and_inspection_only():
    one = M.simulate(base(), 5, team(crews=10, vehicles=10), leads_mode="configured").weeks[0]
    two = M.simulate(
        base(robots_per_visit=2, vehicle_robot_capacity=2),
        5,
        team(crews=10, vehicles=10),
        leads_mode="configured",
    ).weeks[0]
    assert two.caps["robot"] == math.floor(0.8 * one.available * 1 * 5 / 2)  # 19.26 / 2 -> 9
    assert two.caps["robot"] == 9 and one.caps["robot"] == 19
    assert two.caps["inspection"] == 17 and one.caps["inspection"] == 35
    assert two.caps["crew"] == one.caps["crew"] and two.caps["vehicle"] == one.caps["vehicle"]
    infeasible = M.simulate(base(robots_per_visit=2), 5, team(crews=10, vehicles=10), leads_mode="configured")
    assert "vehicle" in infeasible.cycles.infeasible and infeasible.caps["vehicle"] == 0
    assert infeasible.done == 0 and M.INFEASIBLE in infeasible.flags
    assert M.recommend(base(robots_per_visit=2), 5).status == "UNATTAINABLE"


def test_vehicle_bound():
    v = base(load_min=10, unload_min=10, on_site_min=90)  # Tv 190 -> 2 trips/day, crews plentiful
    r = M.simulate(v, 5, team(crews=10, vehicles=1), leads_mode="configured")
    assert r.cycles.v_vehicle == 2 and r.caps["vehicle"] == 8
    assert r.binding == "vehicle" and r.done == 8 and r.caps["crew"] > r.caps["vehicle"]


def test_robot_bound():
    r = M.simulate(base(), 5, team(crews=10, vehicles=10, stations=2), leads_mode="configured")
    assert (r.binding, r.done, r.caps["robot"]) == ("robot", 17, 17) and r.unmet == 3


def test_recovery_unstable_with_no_engineers_adds_engineers_first():
    v = base(configured_engineers=0, incidents_per_visit=0.02)  # backlog 0.16/week, still growing at week 26
    cur = M.current(v, 5)
    assert M.RECOVERY_UNSTABLE in cur.flags and cur.binding == "recovery"
    assert cur.weeks[-1].backlog > cur.weeks[-2].backlog
    plan = M.recommend(v, 5)
    assert plan.steps[0].startswith("rule 2") and plan.steps[0].endswith("engineers 1")
    assert plan.status == "MET" and plan.team.engineers >= 1
    assert M.RECOVERY_UNSTABLE not in plan.result.flags


def test_recovery_first_even_when_target_already_delivered():
    # robot-bound configured team that already delivers the target, but recovery is unstable: engineers only
    v = base(
        configured_engineers=0,
        configured_crews=10,
        configured_vehicles=10,
        configured_inspection_stations=2,
        incidents_per_visit=0.02,
    )
    cur = M.current(v, 5)
    assert M.RECOVERY_UNSTABLE in cur.flags and cur.weeks[-1].binding == "robot"
    plan = M.recommend(v, 5)
    assert plan.steps == ["rule 2: recovery backlog growing -> engineers 1"]
    assert plan.status == "MET" and plan.team == team(
        crews=10, vehicles=10, engineers=1, stations=2, ops_leads=2
    )


@pytest.mark.parametrize("fleet", [5, 10])
def test_zero_engineers_flag_recovery_unstable_and_add_an_engineer_first(fleet):
    """Rev 5: with no engineers at base the backlog absorbs the fleet; at 5 robots robot capacity floors to 0 and
    the backlog stops growing (B_H == B_{H-1}), which is still RECOVERY_UNSTABLE (positive, not shrinking, R_H = 0).
    The search adds an engineer first instead of stopping UNATTAINABLE on robot capacity."""
    v = base(configured_engineers=0)
    cur = M.current(v, fleet)
    assert cur.weeks[-1].backlog > 4
    assert M.RECOVERY_UNSTABLE in cur.flags and cur.binding == "recovery" and M.INFEASIBLE not in cur.flags
    plan = M.recommend(v, fleet)
    assert plan.steps[0] == "rule 2: recovery backlog growing -> engineers 1"
    assert plan.status == "MET" and plan.team.engineers >= 1
    assert M.RECOVERY_UNSTABLE not in plan.result.flags


def test_saturated_backlog_is_flagged_unstable_rev5():
    v = base(configured_engineers=0)
    cur = M.current(v, 5)
    assert cur.done == 0 and cur.caps["robot"] == 0
    assert cur.weeks[-1].backlog == cur.weeks[-2].backlog > 4  # not growing: only the rev 5 clause fires
    assert M.RECOVERY_UNSTABLE in cur.flags and cur.binding == "recovery"


def test_recovery_unstable_classification():
    eps = M.EPS
    assert M.recovery_unstable(1.0, 0.5, 10)  # growing
    assert M.recovery_unstable(4.75, 4.75, 0)  # saturated: positive, flat, no robot capacity
    assert M.recovery_unstable(4.75, 4.75 + eps / 2, 0)  # flat within tolerance
    assert not M.recovery_unstable(4.75, 4.75, 3)  # flat but robots still working
    assert not M.recovery_unstable(4.0, 4.75, 0)  # shrinking
    assert not M.recovery_unstable(0.0, 0.0, 0)  # no backlog
    assert not M.recovery_unstable(eps / 2, 0.0, 0)  # within tolerance of zero


def test_recovered_once_engineering_clears_the_backlog():
    v = base(configured_engineers=0)
    assert M.RECOVERY_UNSTABLE in M.current(v, 5).flags
    res = M.simulate(v, 5, team(engineers=1), leads_mode="configured")
    assert res.weeks[-1].backlog <= res.weeks[-2].backlog + M.EPS
    assert M.RECOVERY_UNSTABLE not in res.flags and res.binding != "recovery"


def test_infeasible_with_no_engineers_is_not_recovery_hiring():
    v = base(on_site_min=500, configured_engineers=0)
    cur = M.current(v, 5)
    assert M.INFEASIBLE in cur.flags and M.RECOVERY_UNSTABLE not in cur.flags and cur.binding != "recovery"
    plan = M.recommend(v, 5)
    assert plan.status == "UNATTAINABLE" and "infeasible" in plan.reason
    assert plan.steps == [] and plan.team.engineers == 0


def test_supervisor_shortfall():
    v = base(configured_crews=10, configured_ops_leads=1)
    cur = M.current(v, 5)
    assert cur.supervisor_shortfall == 1 and cur.costs.ops_leads == 1  # ceil(10/8) - 1
    rec = M.simulate(v, 5, M.configured_team(v), leads_mode="span")
    assert rec.supervisor_shortfall == 0 and rec.costs.ops_leads == 2 and rec.team.ops_leads == 2


def test_infeasible_cycle_longer_than_day():
    v = base(on_site_min=500)
    c = M.cycles(v)
    assert set(c.infeasible) == {"robot", "crew", "vehicle"}
    assert c.v_robot == c.v_crew == c.v_vehicle == 0 and c.v_inspection == 7
    r = M.current(v, 5)
    assert r.done == 0 and r.cap == 0 and M.INFEASIBLE in r.flags
    assert r.costs.per_deployment is None and r.utilization == {"crew": None, "vehicle": None, "robot": None}
    plan = M.recommend(v, 5)
    assert plan.status == "UNATTAINABLE" and "infeasible" in plan.reason


def test_runtime_limited():
    v = base(runtime_limit_min=60)
    c = M.cycles(v)
    assert set(c.infeasible) == {"robot"} and c.v_robot == 0 and c.v_crew == 1
    r = M.current(v, 5)
    assert r.caps["robot"] == 0 and r.done == 0 and r.binding == "robot"
    assert "runtime_limit_min" in r.depends_on_unknown
    assert "runtime_limit_min" not in M.current(base(), 5).depends_on_unknown
    assert M.cycles(base(runtime_limit_min=120)).infeasible == {}


def test_charge_gap_allows_second_visit():
    v = base(
        inspect_min=10, load_min=10, travel_one_way_min=20, on_site_min=60, unload_min=10, post_inspect_min=10
    )
    c = M.cycles(v)  # Tr 140: 2*140 + 120 = 400 <= 450; 3*140 + 240 > 450
    assert c.tr == 140 and c.v_robot == 2 and c.v_crew == 3
    assert M.cycles(dict(v, charge_between_visits_min=180)).v_robot == 1


def test_zero_denominators_are_none():
    r = M.simulate(base(), 5, team(crews=0, vehicles=0, stations=0), leads_mode="configured")
    assert r.done == 0 and r.costs.per_deployment is None
    assert r.utilization["crew"] is None and r.utilization["vehicle"] is None
    assert r.utilization["robot"] == 0
    assert M.recommend(base(demand_visits_per_robot_week=0), 5).reason.startswith("robot-limited target is 0")


# ------------------------------------------------------------------------------------------ properties


def _random_values(rng):
    v = values(rng.choice(ASSUMPTIONS))
    for key in (
        "inspect_min",
        "load_min",
        "travel_one_way_min",
        "on_site_min",
        "unload_min",
        "post_inspect_min",
        "charge_between_visits_min",
        "recovery_hours_per_hold",
        "engineer_hours_per_week",
        "maintenance_hours_per_robot_month",
    ):
        v[key] = v[key] * rng.uniform(0.5, 1.5)
    v["incidents_per_visit"] = rng.uniform(0, 0.4)
    v["hold_min_days"] = rng.uniform(0, 5)
    v["scheduling_efficiency"] = rng.uniform(0.3, 1.0)
    v["demand_visits_per_robot_week"] = rng.choice([2, 4, 6, 10])
    for key in (
        "configured_crews",
        "configured_vehicles",
        "configured_engineers",
        "configured_inspection_stations",
    ):
        v[key] = rng.randint(0, 6)
    return v


def _rand_team(rng):
    return team(rng.randint(0, 8), rng.randint(0, 8), rng.randint(0, 4), rng.randint(0, 3))


def test_conservation_done_within_caps_and_busy_minutes():
    rng = random.Random(6)
    for _ in range(200):
        v = _random_values(rng)
        n = rng.choice(FLEET_SIZES)
        t = _rand_team(rng)
        r = M.simulate(v, n, t)
        c = r.cycles
        week_min = v["working_day_min"] * v["working_days_week"]
        rpv = v["robots_per_visit"]
        for w in r.weeks:
            assert 0 <= w.done <= min(w.caps.values()) and w.done <= r.demand and w.unmet == r.demand - w.done
            assert w.done * rpv * c.tr + w.done * rpv * 0 <= w.available * week_min + 1e-6
            assert w.done * rpv * c.tr <= w.available * c.v_robot * c.tr * v["working_days_week"] + 1e-6
            assert w.done * c.tc <= t.crews * week_min + 1e-6
            assert w.done * c.tv <= t.vehicles * week_min + 1e-6
            assert w.done * rpv * c.ti <= t.stations * week_min + 1e-6
            assert w.backlog >= -1e-12 and 0 <= w.available <= n


def test_robot_day_fits_charge_gaps():
    rng = random.Random(7)
    for _ in range(200):
        v = _random_values(rng)
        c = M.cycles(v)
        if c.v_robot:
            k, w, ch = c.v_robot, v["working_day_min"], v["charge_between_visits_min"]
            assert k * c.tr + (k - 1) * ch <= w + 1e-9 < (k + 1) * c.tr + k * ch


def test_monotonicity():
    rng = random.Random(42)
    checked = 0
    for _ in range(150):
        v = _random_values(rng)
        n = rng.choice(FLEET_SIZES)
        t = _rand_team(rng)
        r0 = M.simulate(v, n, t)
        # more robots never lower week-1 robot capacity
        assert M.simulate(v, n + 1, t).weeks[0].caps["robot"] >= r0.weeks[0].caps["robot"]
        for field in ("crews", "vehicles", "stations", "engineers"):
            bigger = replace(t, **{field: getattr(t, field) + 1})
            r1 = M.simulate(v, n, bigger)
            if M.RECOVERY_UNSTABLE in r0.flags or M.RECOVERY_UNSTABLE in r1.flags:
                continue  # not guaranteed while the backlog grows (more work -> more holds)
            assert r1.done >= r0.done, (field, v, n, t)
            checked += 1
        for key, factor in (
            ("incidents_per_visit", 1.5),
            ("hold_min_days", 1.5),
            ("recovery_hours_per_hold", 1.5),
            ("travel_one_way_min", 1.3),
        ):
            worse = dict(v, **{key: v[key] * factor})
            if key == "hold_min_days":
                worse[key] = min(worse[key], v["working_days_week"])
            r1 = M.simulate(worse, n, t)
            if M.RECOVERY_UNSTABLE in r0.flags or M.RECOVERY_UNSTABLE in r1.flags:
                continue
            assert r1.done <= r0.done, (key, v, n, t)
            checked += 1
    assert checked > 300


def test_scenario_ordering_of_current_done_with_demand_at_base():
    for i in REGISTRY:  # precondition: directions consistently ordered
        c, b, o = i.conservative, i.base, i.optimistic
        if i.capacity_direction == "higher_is_better":
            assert c <= b <= o, i.key
        elif i.capacity_direction == "lower_is_better":
            assert c >= b >= o, i.key
    demand = BY_KEY["demand_visits_per_robot_week"].base
    for n in FLEET_SIZES:
        done = [M.current(dict(values(a), demand_visits_per_robot_week=demand), n).done for a in ASSUMPTIONS]
        assert done[0] <= done[1] <= done[2], (n, done)
        team_big = team(4 * n, 4 * n, 4 * n, 4 * n)
        done = [
            M.simulate(dict(values(a), demand_visits_per_robot_week=demand), n, team_big).done
            for a in ASSUMPTIONS
        ]
        assert done[0] <= done[1] <= done[2], (n, done)


# ------------------------------------------------------------------------------------------ greedy search


def test_greedy_terminates_within_bound():
    rng = random.Random(11)
    for _ in range(60):
        v = _random_values(rng)
        n = rng.choice((5, 10))
        plan = M.recommend(v, n)
        bound = v["staffing_bound_per_robot"] * n
        assert plan.status in ("MET", "UNATTAINABLE")
        assert len(plan.steps) <= 4 * bound
        assert max(plan.team.crews, plan.team.vehicles, plan.team.engineers, plan.team.stations) <= max(
            bound, *(v[k] for k in ("configured_crews", "configured_vehicles"))
        )
        if plan.status == "MET":
            assert plan.result.done >= min(plan.result.demand, M.robot_limited_target(v, n))
            assert M.RECOVERY_UNSTABLE not in plan.result.flags
            assert plan.team.ops_leads == math.ceil(plan.team.crews / v["ops_lead_span"])


def test_greedy_unattainable_when_bound_reached():
    v = base(staffing_bound_per_robot=1)  # bound 5 per resource; target 17 needs a 6th crew for utilization
    plan = M.recommend(v, 5)
    assert plan.status == "UNATTAINABLE" and plan.reason.startswith("bound reached")
    assert plan.team.crews == 5 and plan.result.done == 17


def test_greedy_unattainable_when_target_zero():
    plan = M.recommend(base(demand_visits_per_robot_week=0), 5)
    assert plan.status == "UNATTAINABLE" and plan.steps == []
    plan = M.recommend(base(runtime_limit_min=30), 5)
    assert plan.status == "UNATTAINABLE" and "robot" in plan.reason


# ------------------------------------------------------------------------------------------ sensitivity


def test_perturbation_feasibility():
    v = base(incidents_per_visit=0.9, scheduling_efficiency=0.9)
    assert M.perturb(v, "incidents_per_visit", 1.2)["incidents_per_visit"] == 1.0
    assert M.perturb(v, "scheduling_efficiency", 1.2)["scheduling_efficiency"] == 1.0
    assert M.perturb(base(), "configured_crews", 1.2)["configured_crews"] == 2  # 2.4 rounds to 2
    assert M.perturb(base(), "robots_per_visit", 0.8)["robots_per_visit"] == 1  # 0.8 rounds to 1, kept >= 1
    assert M.perturb(base(configured_engineers=1), "configured_engineers", 0.8)["configured_engineers"] == 1
    assert M.perturb(base(), "runtime_limit_min", 1.2)["runtime_limit_min"] is None
    assert M.perturb(base(hold_min_days=5), "hold_min_days", 1.2)["hold_min_days"] == 5  # <= working days
    for f in (0.8, 1.2):
        for i in REGISTRY:
            p = M.perturb(base(), i.key, f)
            if i.integer:
                assert float(p[i.key]).is_integer() and p[i.key] >= 0
            if i.unit in ("min", "hours", "engineer hours", "working days") and p[i.key] is not None:
                assert p[i.key] > 0
            assert p["hold_min_days"] <= p["working_days_week"]


@pytest.mark.parametrize("staffing", ["configured", "recommended"])
def test_sensitivity_ranking_and_unknown_flags(staffing):
    v = base()
    rows = M.sensitivity(v, 5, staffing=staffing)
    assert len(rows) == len(REGISTRY) and {r.key for r in rows} == set(BY_KEY)
    best = [max(r.delta_done_low, r.delta_done_high) for r in rows]
    assert best == sorted(best, reverse=True)
    for r in rows:
        assert r.unknown == (BY_KEY[r.key].provenance == "unknown_company_data")
    by = {r.key: r for r in rows}
    if staffing == "configured":
        # crew-bound at 5 robots: more working days / efficiency add visits; robot reliability does not
        assert by["scheduling_efficiency"].delta_done_high == 1 and best[0] == 1
        assert by["incidents_per_visit"].delta_done_low == 0
        assert by["runtime_limit_min"].low_value is None and by["runtime_limit_min"].delta_done_high == 0
    else:
        # robot-bound at the recommended team: robot-side levers now add deployments
        assert best[0] >= 1
        assert by["maintenance_hours_per_robot_month"].delta_done_low >= 0
    base_res = M.current(v, 5)
    assert by["overhead_month"].cost_high > (base_res.costs.per_deployment if staffing == "configured" else 0)


def test_compare_rows_per_fleet_size():
    rows = M.compare(base())
    assert [r["fleet_size"] for r in rows] == list(FLEET_SIZES)
    first = rows[0]
    assert first["current"]["done"] == 8 and first["recommended"]["done"] == 17
    assert first["current"]["total_cost"] == approx(67_232.44, abs=0.005)
    assert first["recommended"]["cost_per_deployment"] == approx(1_688.78, abs=0.005)
    assert first["recommended"]["status"] == "MET"
    for r in rows:
        assert r["current"]["team"] == {
            "crews": 2,
            "vehicles": 2,
            "engineers": 1,
            "stations": 1,
            "ops_leads": 1,
        }
        assert r["recommended"]["done"] >= r["current"]["done"]
        assert "demand_visits_per_robot_week" in r["current"]["depends_on_unknown"]
        assert r["current"]["total_cost"] == approx(
            67_232.44 + (r["fleet_size"] - 5) * 1000, abs=0.005
        )  # crew-bound at 8 done: only the per-robot allowance grows
    assert WEEKS_PER_MONTH == approx(52 / 12)

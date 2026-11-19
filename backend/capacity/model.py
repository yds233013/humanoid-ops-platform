"""Slice 6 capacity model (frozen contract docs/06_scaling_capacity_contract.md rev 4, sections 3-7 and 9).

Pure Python, authoritative. Everything is SYNTHETIC: a forecast built on explicit assumptions from the
registry (`capacity.inputs`), never a validated operating plan. Imports nothing from Django.

Units: minutes inside a day, working days for holds, hours for engineer/maintenance work, SCU for money,
one simulation step = one week.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from .inputs import BY_KEY, FLEET_SIZES, REGISTRY, WEEKS_PER_MONTH, unknown_keys

Values = Mapping[str, float | None]
LeadsMode = Literal["configured", "span"]

EPS = 1e-9  # floor tolerance (contract 4) and backlog-growth tolerance
# tie order for the binding constraint
RESOURCES: tuple[str, ...] = ("robot", "crew", "vehicle", "inspection")
RECOVERY_UNSTABLE = "RECOVERY_UNSTABLE"
NOT_CONVERGED = "NOT_CONVERGED"
INFEASIBLE = "INFEASIBLE"
PERTURBATION = 0.20  # sensitivity: -20% / +20%


def _f(values: Values, key: str) -> float:
    v = values[key]
    if v is None:
        raise ValueError(f"input {key} has no value")
    return float(v)


def _int(values: Values, key: str) -> int:
    return int(math.floor(_f(values, key) + 0.5))


def _floor(x: float) -> int:
    return int(math.floor(x + EPS))


# ---------------------------------------------------------------------------------------- team and cycles


@dataclass(frozen=True)
class Team:
    crews: int
    vehicles: int
    engineers: int
    stations: int
    ops_leads: int


def configured_team(values: Values) -> Team:
    return Team(
        crews=_int(values, "configured_crews"),
        vehicles=_int(values, "configured_vehicles"),
        engineers=_int(values, "configured_engineers"),
        stations=_int(values, "configured_inspection_stations"),
        ops_leads=_int(values, "configured_ops_leads"),
    )


@dataclass
class Cycles:
    tr: float
    tc: float
    tv: float
    ti: float
    v_robot: int
    v_crew: int
    v_vehicle: int
    v_inspection: int
    infeasible: dict[str, str] = field(default_factory=dict)


def cycles(values: Values) -> Cycles:
    """Section 3: whole, indivisible visits per working day per resource; INFEASIBLE resources get 0."""
    w = _f(values, "working_day_min")
    inspect, post = _f(values, "inspect_min"), _f(values, "post_inspect_min")
    load, unload = _f(values, "load_min"), _f(values, "unload_min")
    travel, on_site = _f(values, "travel_one_way_min"), _f(values, "on_site_min")
    charge = _f(values, "charge_between_visits_min")
    r = _int(values, "robots_per_visit")
    infeasible: dict[str, str] = {}

    tr = inspect + load + 2 * travel + on_site + unload + post
    tc = tr
    tv = load + 2 * travel + on_site + unload
    ti = inspect + post

    def per_day(name: str, busy: float) -> int:
        if busy <= 0:
            infeasible[name] = f"{name} cycle duration {busy:g} min is not positive"
            return 0
        if busy > w + EPS:
            infeasible[name] = f"{name} cycle {busy:g} min exceeds the {w:g} min working day"
            return 0
        return _floor(w / busy)

    # robot: largest k with k*Tr + (k-1)*charge <= W
    runtime = values.get("runtime_limit_min")
    if runtime is not None and float(runtime) < on_site:
        infeasible["robot"] = (
            f"runtime limit {float(runtime):g} min is below the {on_site:g} min on-site session"
        )
        v_robot = 0
    elif tr <= 0:
        infeasible["robot"] = f"robot cycle duration {tr:g} min is not positive"
        v_robot = 0
    elif tr > w + EPS:
        infeasible["robot"] = f"robot cycle {tr:g} min exceeds the {w:g} min working day"
        v_robot = 0
    else:
        v_robot = max(1, _floor((w + charge) / (tr + charge))) if tr + charge > 0 else 1
    v_crew = per_day("crew", tc)
    v_vehicle = per_day("vehicle", tv)
    if _int(values, "vehicle_robot_capacity") < r:
        infeasible["vehicle"] = (
            f"vehicle carries {_int(values, 'vehicle_robot_capacity')} robot(s) but a visit needs {r}"
        )
        v_vehicle = 0
    v_inspection = per_day("inspection", ti)
    return Cycles(tr, tc, tv, ti, v_robot, v_crew, v_vehicle, v_inspection, infeasible)


# -------------------------------------------------------------------------------------- weekly simulation


@dataclass
class Week:
    t: int
    available: float
    caps: dict[str, int]
    cap: int
    binding: str
    done: int
    unmet: int
    holds: float
    older: float
    rec_old: float
    rec_new: float
    backlog: float
    unavailable: float


@dataclass
class Costs:
    ops_leads: int
    labor: float
    vehicles: float
    maintenance: float
    overhead: float
    total: float
    per_deployment: float | None


@dataclass
class ScenarioResult:
    fleet_size: int
    team: Team
    cycles: Cycles
    weeks: list[Week]
    caps: dict[str, int]
    cap: int
    binding: str
    done: int
    demand: int
    unmet: int
    flags: list[str]
    utilization: dict[str, float | None]
    costs: Costs
    supervisor_shortfall: int
    depends_on_unknown: list[str]


def recovery_step(
    older: float, holds: float, jobs_capacity: float, f_min: float
) -> tuple[float, float, float, float]:
    """Section 4 cohorts: FIFO recovery of the older backlog, then this week's holds.

    Returns (rec_old, rec_new, backlog, unavailable_next_week). A backlog hold is out all next week (counted
    once); a new hold recovered this week is still out for f_min; an older hold recovered this week is out for
    nothing.
    """
    rec_old = min(older, jobs_capacity)
    rec_new = min(holds, jobs_capacity - rec_old)
    backlog = (older - rec_old) + (holds - rec_new)
    unavailable = backlog + rec_new * f_min + rec_old * 0.0
    return rec_old, rec_new, backlog, unavailable


def maintenance_fraction(values: Values) -> float:
    hours_month = _f(values, "working_day_min") / 60 * _f(values, "working_days_week") * WEEKS_PER_MONTH
    if hours_month <= 0:
        return 1.0
    return _f(values, "maintenance_hours_per_robot_month") / hours_month


def _demand(values: Values, fleet_size: int) -> int:
    # demand is a whole number of visits per week (contract example: 4 * 5 = 20); non-integer products floor
    return _floor(_f(values, "demand_visits_per_robot_week") * fleet_size)


def _ratio(num: float, den: float) -> float | None:
    return None if den == 0 else num / den


def depends_on_unknown(values: Values) -> list[str]:
    """Every unknown_company_data input the equations use; runtime_limit_min only when it is set."""
    return [k for k in unknown_keys() if not (k == "runtime_limit_min" and values.get(k) is None)]


def simulate(
    values: Values, fleet_size: int, team: Team, *, leads_mode: LeadsMode = "configured"
) -> ScenarioResult:
    """Section 4 weekly recursion (week t uses only week t-1 state) plus section 6 costs; reports week H."""
    cyc = cycles(values)
    n = fleet_size
    d = _f(values, "working_days_week")
    eff = _f(values, "scheduling_efficiency")
    r = _int(values, "robots_per_visit")
    m = maintenance_fraction(values)
    horizon = max(1, _int(values, "horizon_weeks"))
    demand = _demand(values, n)
    p_hold = _f(values, "incidents_per_visit") * _f(values, "holds_per_incident")
    rec_hours = _f(values, "recovery_hours_per_hold")
    jobs_capacity = (
        team.engineers * _f(values, "engineer_hours_per_week") / rec_hours if rec_hours > 0 else math.inf
    )
    f_min = _f(values, "hold_min_days") / d if d > 0 else 1.0

    c_cap = _floor(eff * team.crews * cyc.v_crew * d)
    v_cap = _floor(eff * team.vehicles * cyc.v_vehicle * d)
    i_cap = _floor(team.stations * cyc.v_inspection * d / r) if r > 0 else 0

    weeks: list[Week] = []
    backlog, unavailable = 0.0, 0.0
    for t in range(1, horizon + 1):
        available = max(0.0, (n - unavailable) * (1 - m))
        r_cap = _floor(eff * available * cyc.v_robot * d / r) if r > 0 else 0
        caps = {"robot": r_cap, "crew": c_cap, "vehicle": v_cap, "inspection": i_cap}
        cap = min(caps.values())
        binding = next(k for k in RESOURCES if caps[k] == cap)
        done = min(cap, demand)
        holds = done * p_hold
        older = backlog
        rec_old, rec_new, backlog, unavailable = recovery_step(older, holds, jobs_capacity, f_min)
        weeks.append(
            Week(
                t,
                available,
                caps,
                cap,
                binding,
                done,
                demand - done,
                holds,
                older,
                rec_old,
                rec_new,
                backlog,
                unavailable,
            )
        )

    last = weeks[-1]
    prev_backlog = weeks[-2].backlog if len(weeks) > 1 else 0.0
    flags: list[str] = []
    if cyc.infeasible:
        flags.append(INFEASIBLE)
    if last.backlog > prev_backlog + EPS:
        flags.append(RECOVERY_UNSTABLE)
    if len(weeks) > 1 and abs(last.done - weeks[-2].done) >= 1:
        flags.append(NOT_CONVERGED)
    binding = "recovery" if RECOVERY_UNSTABLE in flags else last.binding

    span = _int(values, "ops_lead_span")
    span_leads = math.ceil(team.crews / span) if span > 0 else team.crews
    ops_leads = team.ops_leads if leads_mode == "configured" else span_leads
    shortfall = max(0, span_leads - team.ops_leads) if leads_mode == "configured" else 0
    costs = _costs(values, n, team, ops_leads, last.done)
    utilization = {
        "crew": _ratio(last.done, c_cap),
        "vehicle": _ratio(last.done, v_cap),
        "robot": _ratio(last.done, last.caps["robot"]),
    }
    return ScenarioResult(
        fleet_size=n,
        team=replace(team, ops_leads=ops_leads),
        cycles=cyc,
        weeks=weeks,
        caps=dict(last.caps),
        cap=last.cap,
        binding=binding,
        done=last.done,
        demand=demand,
        unmet=last.unmet,
        flags=flags,
        utilization=utilization,
        costs=costs,
        supervisor_shortfall=shortfall,
        depends_on_unknown=depends_on_unknown(values),
    )


def _costs(values: Values, n: int, team: Team, ops_leads: int, done: int) -> Costs:
    """Section 6, SCU per month."""
    labor = (
        team.crews * _f(values, "crew_size") * _f(values, "field_operator_cost_month")
        + team.engineers * _f(values, "engineer_cost_month")
        + ops_leads * _f(values, "ops_lead_cost_month")
    )
    km_visit = 2 * _f(values, "travel_one_way_min") / 60 * _f(values, "avg_speed_kmh")
    fixed = team.vehicles * _f(values, "vehicle_fixed_cost_month")
    vehicles = fixed + done * WEEKS_PER_MONTH * km_visit * _f(values, "vehicle_cost_per_km")
    maintenance = n * _f(values, "maintenance_allowance_robot_month")
    overhead = _f(values, "overhead_month")
    total = labor + vehicles + maintenance + overhead
    per_deployment = None if done == 0 else total / (done * WEEKS_PER_MONTH)
    return Costs(ops_leads, labor, vehicles, maintenance, overhead, total, per_deployment)


def current(values: Values, fleet_size: int) -> ScenarioResult:
    """Current capacity: always the configured team and configured ops leads (section 5)."""
    return simulate(values, fleet_size, configured_team(values), leads_mode="configured")


# ----------------------------------------------------------------------------------- staffing (section 5)


def _bound(values: Values, fleet_size: int) -> int:
    return _int(values, "staffing_bound_per_robot") * fleet_size


def robot_limited_target(values: Values, fleet_size: int) -> int:
    b = _bound(values, fleet_size)
    team = Team(crews=b, vehicles=b, engineers=b, stations=b, ops_leads=0)
    return simulate(values, fleet_size, team, leads_mode="span").done


@dataclass
class StaffingPlan:
    status: Literal["MET", "UNATTAINABLE"]
    reason: str
    team: Team
    result: ScenarioResult
    steps: list[str]


_FIELD = {"crew": "crews", "vehicle": "vehicles", "inspection": "stations", "engineer": "engineers"}


def recommend(values: Values, fleet_size: int) -> StaffingPlan:
    """Bounded greedy search (section 5, ordered first-match rules); a heuristic, not an optimum."""
    target = robot_limited_target(values, fleet_size)
    demand = _demand(values, fleet_size)
    goal = min(demand, target)
    bound = _bound(values, fleet_size)
    trigger = _f(values, "crew_hire_trigger")
    team = configured_team(values)
    steps: list[str] = []
    max_iterations = 4 * max(bound, 0) + 8  # every iteration adds one unit to one of four bounded resources

    def stop(status: Literal["MET", "UNATTAINABLE"], reason: str, res: ScenarioResult) -> StaffingPlan:
        return StaffingPlan(status, reason, res.team, res, steps)

    for _ in range(max_iterations):
        res = simulate(values, fleet_size, team, leads_mode="span")
        # rule 1
        if res.cycles.infeasible:
            reasons = "; ".join(f"{k}: {v}" for k, v in res.cycles.infeasible.items())
            return stop("UNATTAINABLE", f"infeasible resource ({reasons})", res)
        if target == 0:
            return stop("UNATTAINABLE", "robot-limited target is 0 completed visits/week", res)
        # rules 2-4 pick a resource to add
        add: str | None = None
        why = ""
        if RECOVERY_UNSTABLE in res.flags:
            add, why = "engineer", "rule 2: recovery backlog growing"
        elif res.done < goal:
            binding = res.weeks[-1].binding
            if binding == "robot":
                return stop("UNATTAINABLE", f"robot capacity: {res.done} < goal {goal}", res)
            add, why = binding, f"rule 3: done {res.done} < goal {goal}, binding {binding}"
        else:
            util = res.utilization["crew"]
            if util is not None and util > trigger + EPS:
                add, why = "crew", f"rule 4: crew utilization {util:.4f} > {trigger:g}"
        if add is None:
            return stop("MET", f"done {res.done} >= goal {goal}, crew utilization within trigger", res)
        attr = _FIELD[add]
        current_units = getattr(team, attr)
        if current_units >= bound:
            return stop("UNATTAINABLE", f"bound reached: {attr} already at {bound} ({why})", res)
        team = replace(team, **{attr: current_units + 1})
        steps.append(f"{why} -> {attr} {current_units + 1}")
    res = simulate(values, fleet_size, team, leads_mode="span")  # pragma: no cover - unreachable by the bound
    return stop("UNATTAINABLE", "iteration limit reached", res)  # pragma: no cover


# -------------------------------------------------------------------------------- sensitivity (section 9)


@dataclass
class SensitivityRow:
    key: str
    low_value: float | None
    high_value: float | None
    done_low: int
    done_high: int
    delta_done_low: int
    delta_done_high: int
    cost_low: float | None  # cost per completed deployment (SCU), N/A when nothing is completed
    cost_high: float | None
    unknown: bool


_MIN_ONE = {
    "working_day_min",
    "working_days_week",
    "robots_per_visit",
    "vehicle_robot_capacity",
    "crew_size",
    "ops_lead_span",
    "staffing_bound_per_robot",
    "horizon_weeks",
}
_DURATION_UNITS = {"min", "hours", "working days", "engineer hours"}


def perturb(values: Values, key: str, factor: float) -> dict[str, float | None]:
    """One feasible perturbation: probabilities/fractions clamped to [0, 1], integers rounded and kept >= 1
    (or >= 0 for team counts), durations kept > 0, hold minimum kept <= working days."""
    out = dict(values)
    base = values[key]
    if base is None:  # unconstrained (runtime limit): nothing to perturb
        return out
    inp = BY_KEY[key]
    v = float(base) * factor
    if inp.unit in ("fraction", "probability"):
        v = min(1.0, max(0.0, v))
    if inp.integer:
        v = float(math.floor(v + 0.5))
        v = max(1.0 if key in _MIN_ONE else 0.0, v)
        if key == "working_days_week":
            v = min(7.0, v)
    if inp.unit in _DURATION_UNITS and v <= 0:
        v = float(base)
    out[key] = v
    if key in ("hold_min_days", "working_days_week"):
        hold, days = out["hold_min_days"], out["working_days_week"]
        if hold is not None and days is not None and hold > days:
            out["hold_min_days"] = days
    return out


def sensitivity(
    values: Values, fleet_size: int, *, staffing: Literal["configured", "recommended"] = "configured"
) -> list[SensitivityRow]:
    """Each input -20% / +20%, ranked by incremental completed deployments (best of the two directions), then
    by cost per deployment. "recommended" holds the base recommended team fixed and perturbs the inputs."""
    if staffing == "configured":

        def run(v: Values) -> ScenarioResult:
            return current(v, fleet_size)

    else:
        team = recommend(values, fleet_size).team

        def run(v: Values) -> ScenarioResult:
            return simulate(v, fleet_size, team, leads_mode="span")

    base_done = run(values).done
    rows: list[SensitivityRow] = []
    for inp in REGISTRY:
        lo_v, hi_v = perturb(values, inp.key, 1 - PERTURBATION), perturb(values, inp.key, 1 + PERTURBATION)
        lo, hi = run(lo_v), run(hi_v)
        rows.append(
            SensitivityRow(
                key=inp.key,
                low_value=lo_v[inp.key],
                high_value=hi_v[inp.key],
                done_low=lo.done,
                done_high=hi.done,
                delta_done_low=lo.done - base_done,
                delta_done_high=hi.done - base_done,
                cost_low=lo.costs.per_deployment,
                cost_high=hi.costs.per_deployment,
                unknown=inp.provenance == "unknown_company_data",
            )
        )
    order = {inp.key: i for i, inp in enumerate(REGISTRY)}

    def rank(row: SensitivityRow) -> tuple[int, float, int]:
        costs = [c for c in (row.cost_low, row.cost_high) if c is not None]
        return (
            -max(row.delta_done_low, row.delta_done_high),
            min(costs) if costs else math.inf,
            order[row.key],
        )

    return sorted(rows, key=rank)


# ------------------------------------------------------------------------------------- founder comparison


def _summary(res: ScenarioResult) -> dict[str, object]:
    return {
        "team": {
            "crews": res.team.crews,
            "vehicles": res.team.vehicles,
            "engineers": res.team.engineers,
            "stations": res.team.stations,
            "ops_leads": res.team.ops_leads,
        },
        "done": res.done,
        "binding": res.binding,
        "unmet": res.unmet,
        "flags": list(res.flags),
        "total_cost": res.costs.total,
        "cost_per_deployment": res.costs.per_deployment,
        "supervisor_shortfall": res.supervisor_shortfall,
        "depends_on_unknown": list(res.depends_on_unknown),
    }


def compare(values: Values) -> list[dict[str, object]]:
    """One row per fleet size: current (configured team) and recommended staffing side by side."""
    rows: list[dict[str, object]] = []
    for n in FLEET_SIZES:
        cur = current(values, n)
        plan = recommend(values, n)
        rec = _summary(plan.result)
        rec["status"] = plan.status
        rec["reason"] = plan.reason
        rows.append(
            {
                "fleet_size": n,
                "demand": cur.demand,
                "target": robot_limited_target(values, n),
                "current": _summary(cur),
                "recommended": rec,
            }
        )
    return rows

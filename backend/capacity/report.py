"""Slice 6 founder report (frozen contract docs/06_scaling_capacity_contract.md rev 4, sections 0, 5-7 and 9).

Everything is SYNTHETIC: a forecast from stated assumptions, never a company operating plan. Every number here
comes from `capacity.model` (authoritative); this module only selects, orders and words them. Takeaways are
generated from the computed numbers; nothing is added that the model did not produce.

Pure Python apart from `load_baseline`, which lazily imports `capacity.baseline` (Engineer B) and must run
inside `operations.snapshot.read_snapshot()`. When that module is missing the registry values are used and the
report says "platform baseline not available".
"""

from __future__ import annotations

import csv
import importlib
import importlib.util
import io
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import model as M
from .inputs import ASSUMPTIONS, BY_KEY, FLEET_SIZES, REGISTRY, WEEKS_PER_MONTH, Assumption
from .inputs import values as registry_values

BANNER = "SYNTHETIC DEMO - a forecast from stated assumptions, not a company operating plan"
BASELINE_MODULE = "capacity.baseline"
BASELINE_UNAVAILABLE = "platform baseline not available"
BASELINE_WINDOW_DAYS = 28
TOP_LEVERS = 5
# Inputs that are not operational improvements, so they are never offered as levers.
NOT_LEVERS = {
    "demand_visits_per_robot_week": "customer demand is a market input, not an operational improvement",
    "horizon_weeks": "the simulation horizon is a modelling choice",
    "holds_per_incident": "a platform rule (Slice 4: every incident holds the robot), not a choice",
}
STAFFING_RESOURCES: tuple[tuple[str, str], ...] = (
    ("crews", "field crews"),
    ("vehicles", "vehicles"),
    ("engineers", "recovery engineers"),
    ("stations", "inspection stations"),
    ("ops_leads", "ops leads"),
)
CONSTRAINT_WORDS = {
    "robot": "robots (fleet capacity)",
    "crew": "field crews",
    "vehicle": "vehicles",
    "inspection": "inspection stations",
    "recovery": "recovery engineers (backlog growing)",
    "demand": "customer demand (every demanded visit completed)",
}
# The resource that relieves each staffing constraint (robot cannot be relieved by staffing).
_RELIEF = {"crew": "crews", "vehicle": "vehicles", "inspection": "stations", "recovery": "engineers"}
NOTES = (
    "Weekly expected-value simulation; the reported result is the last week of the horizon.",
    "Each resource does whole visits per working day; no partial visits and no multi-site vehicle trips.",
    "New holds count as placed at the start of their week (an upper bound on the remaining minimum hold).",
    "Recommended staffing is a deterministic greedy heuristic from the current team, not a global optimum.",
    "Unknown company inputs are illustrative placeholders; every result that uses them says so.",
    "Money is in synthetic currency units (SCU); weeks per month = 52/12.",
)


# ------------------------------------------------------------------------------------------- dataclasses
@dataclass(frozen=True)
class Side:
    """One staffing option at one fleet size (numbers straight from model.compare())."""

    crews: int
    vehicles: int
    engineers: int
    stations: int
    ops_leads: int
    done: int
    binding: str
    unmet: int
    flags: tuple[str, ...]
    total_cost: float
    cost_per_deployment: float | None
    supervisor_shortfall: int
    status: str
    reason: str

    @property
    def binding_text(self) -> str:
        return constraint_words(self.binding)

    @property
    def total_text(self) -> str:
        return money(self.total_cost)

    @property
    def cost_text(self) -> str:
        return money(self.cost_per_deployment)

    @property
    def flags_text(self) -> str:
        return ", ".join(self.flags) or "-"


@dataclass(frozen=True)
class ComparisonRow:
    fleet_size: int
    demand: int
    target: int
    current: Side
    recommended: Side


@dataclass(frozen=True)
class Trigger:
    resource: str
    label: str
    configured: int
    counts: tuple[tuple[int, int], ...]  # (fleet size, recommended count)
    first_rise: int | None  # fleet size at which the count first exceeds the previous step (or current team)


@dataclass(frozen=True)
class NextConstraint:
    fleet_size: int
    staffing: str  # "current" or "recommended"
    constraint: str
    relieved_by: str
    next_constraint: str
    done_before: int
    done_after: int
    explanation: str

    @property
    def constraint_text(self) -> str:
        return constraint_words(self.constraint)

    @property
    def next_text(self) -> str:
        return constraint_words(self.next_constraint)


@dataclass(frozen=True)
class Lever:
    fleet_size: int
    staffing: str
    rank: int
    key: str
    label: str
    change: str  # "-20%" or "+20%"
    from_value: float | None
    to_value: float | None
    unit: str
    done_before: int
    done_after: int
    delta_done: int
    cost_per_deployment: float | None
    unknown: bool

    @property
    def from_text(self) -> str:
        return f"{num(self.from_value)} {self.unit}"

    @property
    def to_text(self) -> str:
        return f"{num(self.to_value)} {self.unit}"

    @property
    def cost_text(self) -> str:
        return money(self.cost_per_deployment)


@dataclass(frozen=True)
class TraceStep:
    section: str
    label: str
    formula: str
    value: float | int | str | None
    display: str


@dataclass(frozen=True)
class UnknownInput:
    key: str
    label: str
    unit: str
    time_basis: str
    value_used: float | None
    conservative: float | None
    base: float | None
    optimistic: float | None
    source: str
    notes: str

    @property
    def value_text(self) -> str:
        return num(self.value_used)

    @property
    def range_text(self) -> str:
        return f"{num(self.conservative)} / {num(self.base)} / {num(self.optimistic)}"


@dataclass(frozen=True)
class ProvenanceRow:
    key: str
    label: str
    unit: str
    time_basis: str
    provenance: str
    value_used: float | None
    registry_value: float | None
    value_source: str
    source: str
    notes: str

    @property
    def unknown(self) -> bool:
        return self.provenance == "unknown_company_data"

    @property
    def value_text(self) -> str:
        return num(self.value_used)


@dataclass(frozen=True)
class BaselineInfo:
    """What the platform baseline did for this report (filled by `load_baseline`)."""

    status: str
    window: str = ""
    entries: dict[str, str] = field(default_factory=dict)  # input key -> how its value was obtained
    applied_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    assumption: str
    banner: str
    fleet_sizes: tuple[int, ...]
    comparison: list[ComparisonRow]
    triggers: list[Trigger]
    next_constraints: list[NextConstraint]
    levers: list[Lever]
    lever_exclusions: dict[str, str]
    worked_example: list[TraceStep]
    unknown_inputs: list[UnknownInput]
    provenance: list[ProvenanceRow]
    takeaways: list[str]
    baseline: BaselineInfo
    notes: tuple[str, ...]


# ------------------------------------------------------------------------------------------- formatting
def money(x: float | None) -> str:
    return "N/A" if x is None else f"{x:,.2f}"


def num(x: float | int | None) -> str:
    if x is None:
        return "unconstrained"
    if isinstance(x, float) and not x.is_integer():
        return f"{x:g}"
    return f"{int(x):,}"


def team_text(side: Side | M.Team) -> str:
    def n(count: int, one: str, many: str) -> str:
        return f"{count} {one if count == 1 else many}"

    return ", ".join(
        (
            n(side.crews, "crew", "crews"),
            n(side.vehicles, "vehicle", "vehicles"),
            n(side.engineers, "engineer", "engineers"),
            n(side.stations, "station", "stations"),
            n(side.ops_leads, "ops lead", "ops leads"),
        )
    )


def constraint_words(name: str) -> str:
    return CONSTRAINT_WORDS.get(name, name)


# ------------------------------------------------------------------------------------------- comparison
def _side(summary: Mapping[str, Any], *, recommended: bool) -> Side:
    team = summary["team"]
    shortfall = int(summary["supervisor_shortfall"])
    if recommended:
        status, reason = str(summary["status"]), str(summary["reason"])
    else:
        status = "CURRENT TEAM"
        reason = (
            f"supervisor shortfall: {shortfall} more ops lead(s) needed for the crew span"
            if shortfall
            else "configured team as entered"
        )
    return Side(
        crews=int(team["crews"]),
        vehicles=int(team["vehicles"]),
        engineers=int(team["engineers"]),
        stations=int(team["stations"]),
        ops_leads=int(team["ops_leads"]),
        done=int(summary["done"]),
        binding=str(summary["binding"]),
        unmet=int(summary["unmet"]),
        flags=tuple(summary["flags"]),
        total_cost=float(summary["total_cost"]),
        cost_per_deployment=None
        if summary["cost_per_deployment"] is None
        else float(summary["cost_per_deployment"]),
        supervisor_shortfall=shortfall,
        status=status,
        reason=reason,
    )


def comparison(values: M.Values) -> list[ComparisonRow]:
    return [
        ComparisonRow(
            fleet_size=int(row["fleet_size"]),  # type: ignore[call-overload]
            demand=int(row["demand"]),  # type: ignore[call-overload]
            target=int(row["target"]),  # type: ignore[call-overload]
            current=_side(row["current"], recommended=False),  # type: ignore[arg-type]
            recommended=_side(row["recommended"], recommended=True),  # type: ignore[arg-type]
        )
        for row in M.compare(values)
    ]


def _team_of(side: Side) -> M.Team:
    return M.Team(side.crews, side.vehicles, side.engineers, side.stations, side.ops_leads)


# ------------------------------------------------------------------------------------------- triggers
def hiring_triggers(configured: M.Team, teams: Sequence[tuple[int, M.Team]]) -> list[Trigger]:
    """For each resource, the fleet step at which the recommended count first rises above the step before it
    (the first step is compared with the current, configured team). None when it never rises."""
    out: list[Trigger] = []
    for attr, label in STAFFING_RESOURCES:
        previous = getattr(configured, attr)
        first: int | None = None
        counts: list[tuple[int, int]] = []
        for n, team in teams:
            count = getattr(team, attr)
            counts.append((n, count))
            if first is None and count > previous:
                first = n
            previous = count
        out.append(Trigger(attr, label, getattr(configured, attr), tuple(counts), first))
    return out


# ------------------------------------------------------------------------------------------- next constraint
def next_constraint(
    values: M.Values, fleet_size: int, team: M.Team, *, staffing: str = "current"
) -> NextConstraint:
    """What limits completed deployments now, and what would limit them once that constraint is relieved.

    Staffing constraints (crew, vehicle, inspection, recovery) are relieved by re-running the simulation with
    that resource at its search bound. Robots cannot be relieved by staffing: the next constraint is then the
    lowest remaining capacity (crew, vehicle, inspection, in tie order) or demand, read from the same final
    week. When every demanded visit is already completed the constraint is demand and the next one is the
    resource with the lowest capacity."""
    leads_mode: M.LeadsMode = "configured" if staffing == "current" else "span"
    res = M.simulate(values, fleet_size, team, leads_mode=leads_mode)
    bound = M._bound(values, fleet_size)
    last = res.weeks[-1]
    if res.binding != "recovery" and res.done >= res.demand:
        resource = last.binding
        return NextConstraint(
            fleet_size,
            staffing,
            "demand",
            "more customer demand",
            resource,
            res.done,
            min(res.cap, res.demand),
            f"all {res.demand} demanded visits/week are completed; with more demand, "
            f"{constraint_words(resource)} would limit first at {res.cap}/week",
        )
    if res.binding == "robot":
        others = {k: res.caps[k] for k in M.RESOURCES if k != "robot"}
        low = min(others.values())
        resource = next(k for k in M.RESOURCES if k != "robot" and others[k] == low)
        after = min(low, res.demand)
        nxt = "demand" if res.demand <= low else resource
        return NextConstraint(
            fleet_size,
            staffing,
            "robot",
            "more robot capacity (reliability, turnaround or fleet size)",
            nxt,
            res.done,
            after,
            f"if robot capacity were not the limit, {constraint_words(nxt)} would limit at {after}/week",
        )
    attr = _RELIEF[res.binding]
    relieved = M.Team(**{**_asdict(team), attr: max(bound, getattr(team, attr))})
    after_res = M.simulate(values, fleet_size, relieved, leads_mode=leads_mode)
    nxt = (
        "demand"
        if after_res.binding != "recovery" and after_res.done >= after_res.demand
        else after_res.binding
    )
    return NextConstraint(
        fleet_size,
        staffing,
        res.binding,
        f"more {dict(STAFFING_RESOURCES)[attr]} (up to the search bound of {bound})",
        nxt,
        res.done,
        after_res.done,
        f"with {constraint_words(res.binding)} relieved, {constraint_words(nxt)} would limit at "
        f"{after_res.done}/week",
    )


def _asdict(team: M.Team) -> dict[str, int]:
    return {
        "crews": team.crews,
        "vehicles": team.vehicles,
        "engineers": team.engineers,
        "stations": team.stations,
        "ops_leads": team.ops_leads,
    }


# ------------------------------------------------------------------------------------------- levers
def top_levers(values: M.Values, fleet_size: int, staffing: str, limit: int = TOP_LEVERS) -> list[Lever]:
    """The model's sensitivity ranking (incremental completed deployments, then cost per deployment), keeping
    only inputs that add completed deployments and are operational (NOT_LEVERS excluded)."""
    mode = "configured" if staffing == "current" else "recommended"
    rows = M.sensitivity(values, fleet_size, staffing=mode)  # type: ignore[arg-type]
    if not rows:
        return []
    base_done = rows[0].done_low - rows[0].delta_done_low
    out: list[Lever] = []
    for row in rows:
        if row.key in NOT_LEVERS:
            continue
        best = max(row.delta_done_low, row.delta_done_high)
        if best <= 0:
            continue
        use_high = row.delta_done_high > row.delta_done_low or (
            row.delta_done_high == row.delta_done_low
            and (row.cost_high or math.inf) <= (row.cost_low or math.inf)
        )
        inp = BY_KEY[row.key]
        out.append(
            Lever(
                fleet_size=fleet_size,
                staffing=staffing,
                rank=len(out) + 1,
                key=row.key,
                label=inp.label,
                change=f"+{M.PERTURBATION:.0%}" if use_high else f"-{M.PERTURBATION:.0%}",
                from_value=values[row.key],
                to_value=row.high_value if use_high else row.low_value,
                unit=inp.unit,
                done_before=base_done,
                done_after=row.done_high if use_high else row.done_low,
                delta_done=best,
                cost_per_deployment=row.cost_high if use_high else row.cost_low,
                unknown=row.unknown,
            )
        )
        if len(out) == limit:
            break
    return out


# ------------------------------------------------------------------------------------------- worked example
def worked_example(values: M.Values, fleet_size: int = 5) -> list[TraceStep]:
    """The section 7 trace, recomputed from the given values (equal to the contract numbers for base)."""
    steps: list[TraceStep] = []

    def add(
        section: str, label: str, value: float | int | str | None, display: str, formula: str = ""
    ) -> None:
        steps.append(TraceStep(section, label, formula, value, display))

    def v(key: str) -> float:
        val = values[key]
        return 0.0 if val is None else float(val)

    cur = M.current(values, fleet_size)
    cyc = cur.cycles
    s = "1. Cycles"
    add(
        s,
        "Robot busy per visit Tr (min)",
        cyc.tr,
        num(cyc.tr),
        f"{num(v('inspect_min'))}+{num(v('load_min'))}+2*{num(v('travel_one_way_min'))}+"
        f"{num(v('on_site_min'))}+{num(v('unload_min'))}+{num(v('post_inspect_min'))}",
    )
    add(s, "Robot visits per day", cyc.v_robot, num(cyc.v_robot), "largest k with k*Tr+(k-1)*charge <= W")
    add(s, "Crew visits per day", cyc.v_crew, num(cyc.v_crew), "floor(W / Tc)")
    add(s, "Vehicle busy per visit Tv (min)", cyc.tv, num(cyc.tv), "load + 2*travel + on_site + unload")
    add(s, "Vehicle visits per day", cyc.v_vehicle, num(cyc.v_vehicle), "floor(W / Tv)")
    add(s, "Inspection per robot Ti (min)", cyc.ti, num(cyc.ti), "inspect + post_inspect")
    add(s, "Robot-inspections per station per day", cyc.v_inspection, num(cyc.v_inspection), "floor(W / Ti)")
    for name, reason in cyc.infeasible.items():
        add(s, f"INFEASIBLE {name}", reason, reason)

    s = "2. Maintenance"
    hours_month = v("working_day_min") / 60 * v("working_days_week") * WEEKS_PER_MONTH
    m = M.maintenance_fraction(values)
    add(s, "Working hours per robot-month", hours_month, f"{hours_month:,.1f}", "W/60 * D * 52/12")
    add(s, "Maintenance fraction m", m, f"{m:.6f}", "maintenance hours / working hours")

    w1 = cur.weeks[0]
    s = "3. Week 1 (current team)"
    add(s, "Available robots A_1", w1.available, f"{w1.available:.6f}", "N * (1 - m)")
    for k, label in (("robot", "R"), ("crew", "C"), ("vehicle", "V"), ("inspection", "I")):
        add(s, f"{label} ({k} capacity, visits/week)", w1.caps[k], num(w1.caps[k]))
    add(
        s,
        "Binding constraint",
        w1.binding,
        w1.binding,
        "lowest capacity; ties robot, crew, vehicle, inspection",
    )
    add(s, "Demand (visits/week)", cur.demand, num(cur.demand), "demand per robot-week * N")
    add(s, "Completed", w1.done, num(w1.done), "min(capacity, demand)")
    add(s, "Unmet", w1.unmet, num(w1.unmet))

    s = "4. Reliability"
    rec_hours = v("recovery_hours_per_hold")
    jobs = cur.team.engineers * v("engineer_hours_per_week") / rec_hours if rec_hours > 0 else math.inf
    add(
        s, "Holds per week", w1.holds, f"{w1.holds:g}", "completed * incidents per visit * holds per incident"
    )
    add(s, "Recovery jobs per week", jobs, f"{jobs:g}", "engineers * hours per week / hours per hold")
    add(s, "Recovery backlog after week 1", w1.backlog, f"{w1.backlog:g}")
    add(s, "Unavailable robots next week U_1", w1.unavailable, f"{w1.unavailable:g}", "B + rec_new * f_min")

    last = cur.weeks[-1]
    s = f"5. Week {last.t} (reported, current team)"
    add(s, "Available robots", last.available, f"{last.available:.6f}", "(N - U) * (1 - m)")
    add(s, "R (robot capacity, visits/week)", last.caps["robot"], num(last.caps["robot"]))
    add(s, "Completed per week", cur.done, num(cur.done))
    add(s, "Binding constraint", cur.binding, cur.binding)
    add(s, "Unmet per week", cur.unmet, num(cur.unmet))
    add(s, "Flags", ", ".join(cur.flags) or "none", ", ".join(cur.flags) or "none")

    s = "6. Monthly cost (current team, SCU)"
    km = 2 * v("travel_one_way_min") / 60 * v("avg_speed_kmh")
    c = cur.costs
    add(s, "Labor", c.labor, money(c.labor), "crews*crew size*operator + engineers*engineer + leads*lead")
    add(s, "Km per visit", km, f"{km:.3f}", "2 * travel/60 * speed")
    add(s, "Vehicles", c.vehicles, money(c.vehicles), "vehicles*fixed + completed*52/12*km*cost per km")
    add(s, "Maintenance allowance", c.maintenance, money(c.maintenance), "N * allowance")
    add(s, "Overhead", c.overhead, money(c.overhead))
    add(s, "Total per month", c.total, money(c.total))
    completed_month = cur.done * WEEKS_PER_MONTH
    add(s, "Completed per month", completed_month, f"{completed_month:.2f}", "completed per week * 52/12")
    add(s, "Cost per completed deployment", c.per_deployment, money(c.per_deployment), "total / completed")

    s = "7. Robot-limited target"
    target = M.robot_limited_target(values, fleet_size)
    add(s, "Target (completed/week)", target, num(target), "every other resource at the search bound")

    s = "8. Recommended staffing (greedy)"
    plan = M.recommend(values, fleet_size)
    for i, step in enumerate(plan.steps, 1):
        add(s, f"Step {i}", step, step)
    rec = plan.result
    team = rec.team
    team_str = team_text(team)
    add(s, "Recommended team", team_str, team_str)
    add(s, "Status", plan.status, f"{plan.status}: {plan.reason}")
    for k, label in (("robot", "R"), ("crew", "C"), ("vehicle", "V"), ("inspection", "I")):
        add(s, f"{label} ({k} capacity, visits/week)", rec.caps[k], num(rec.caps[k]))
    add(s, "Completed per week", rec.done, num(rec.done))
    add(s, "Binding constraint", rec.binding, rec.binding)
    add(s, "Unmet per week", rec.unmet, num(rec.unmet))
    add(s, "Total per month (SCU)", rec.costs.total, money(rec.costs.total))
    add(s, "Cost per completed deployment (SCU)", rec.costs.per_deployment, money(rec.costs.per_deployment))
    return steps


# ------------------------------------------------------------------------------------------- inputs tables
def unknown_inputs(values: M.Values, rows: Sequence[ComparisonRow]) -> list[UnknownInput]:
    """Every unknown_company_data input the reported results depend on, in registry order."""
    used = set(M.depends_on_unknown(values)) if rows else set()
    return [
        UnknownInput(
            i.key,
            i.label,
            i.unit,
            i.time_basis,
            values.get(i.key),
            i.conservative,
            i.base,
            i.optimistic,
            i.source,
            i.notes,
        )
        for i in REGISTRY
        if i.key in used
    ]


def provenance_table(assumption: Assumption, values: M.Values, baseline: BaselineInfo) -> list[ProvenanceRow]:
    out: list[ProvenanceRow] = []
    for i in REGISTRY:
        reg = i.value(assumption)
        used = values.get(i.key)
        if i.key in baseline.entries:
            how = f"platform baseline: {baseline.entries[i.key]}"
        elif used == reg:
            how = f"registry ({assumption})"
            if i.provenance == "platform_measured" and baseline.status == BASELINE_UNAVAILABLE:
                how += "; platform baseline not available"
        else:
            how = "caller-supplied value (differs from the registry)"
        out.append(
            ProvenanceRow(
                i.key, i.label, i.unit, i.time_basis, i.provenance, used, reg, how, i.source, i.notes
            )
        )
    return out


# ------------------------------------------------------------------------------------------- baseline
def baseline_available() -> bool:
    try:
        return importlib.util.find_spec(BASELINE_MODULE) is not None
    except ModuleNotFoundError:  # pragma: no cover - parent package always exists
        return False


def _describe(entry: Any) -> tuple[str | None, str]:
    """(input key, text) for one provenance entry of capacity.baseline.apply (dataclass, dict or text)."""
    if isinstance(entry, Mapping):
        get = entry.get
    else:

        def get(name: str, default: Any = None) -> Any:
            return getattr(entry, name, default)

    key = get("key") or get("input")
    parts = [
        f"{name} {get(name)}"
        for name in ("value", "n", "window", "kind", "source", "status", "note", "fallback")
        if get(name) not in (None, "")
    ]
    text = "; ".join(parts) if parts else str(entry)
    return (str(key) if key else None, text)


def load_baseline(
    assumption: Assumption, at: datetime, *, start: datetime, end: datetime
) -> tuple[dict[str, float | None], BaselineInfo]:
    """Registry values with the platform baseline applied (call inside read_snapshot()).

    capacity.baseline is imported lazily; when it is missing, the registry values are returned unchanged with
    status "platform baseline not available"."""
    reg = registry_values(assumption)
    if not baseline_available():
        return reg, BaselineInfo(BASELINE_UNAVAILABLE)
    baseline_mod = importlib.import_module(BASELINE_MODULE)
    measured = baseline_mod.measure(at, start=start, end=end)
    new_values, provenance = baseline_mod.apply(reg, measured)
    entries: dict[str, str] = {}
    for entry in provenance or []:
        key, text = _describe(entry)
        if key in BY_KEY:
            entries[key] = text
    applied = tuple(k for k in BY_KEY if new_values.get(k) != reg.get(k))
    window = f"{start.isoformat()} to {end.isoformat()} (start inclusive, end exclusive)"
    status = (
        "platform baseline applied (synthetic demo measurement, not real-world performance); "
        f"{len(applied)} input(s) measured, the rest use registry fallbacks"
    )
    return dict(new_values), BaselineInfo(status, window, entries, applied)


# ------------------------------------------------------------------------------------------- takeaways
def takeaways(
    rows: Sequence[ComparisonRow],
    triggers: Sequence[Trigger],
    nexts: Sequence[NextConstraint],
    levers: Sequence[Lever],
    unknown: Sequence[UnknownInput],
) -> list[str]:
    """Plain-words reading generated only from the computed numbers."""
    out: list[str] = []
    for row in rows:
        c, r = row.current, row.recommended
        out.append(
            f"{row.fleet_size} robots: the current team ({team_text(c)}) completes {c.done} of "
            f"{row.demand} demanded visits a week, limited by {constraint_words(c.binding)}. "
            f"Recommended staffing ({team_text(r)}) completes {r.done} ({r.status}), limited by "
            f"{constraint_words(r.binding)}; cost per completed deployment {money(c.cost_per_deployment)} -> "
            f"{money(r.cost_per_deployment)} SCU; monthly total {money(c.total_cost)} -> "
            f"{money(r.total_cost)} SCU."
        )
    rises = [t for t in triggers if t.first_rise is not None]
    if rises:
        parts = []
        for t in sorted(rises, key=lambda t: (t.first_rise or 0, t.resource)):
            before = t.configured
            for n, count in t.counts:
                if n == t.first_rise:
                    parts.append(f"{t.label} from {n} robots ({before} -> {count})")
                    break
                before = count
        out.append("Hiring and vehicle triggers: " + "; ".join(parts) + ".")
    flat = [t.label for t in triggers if t.first_rise is None]
    if flat:
        out.append(
            f"No increase needed between {rows[0].fleet_size} and {rows[-1].fleet_size} robots: "
            + ", ".join(flat)
            + "."
        )
    for nc in nexts:
        if nc.staffing == "recommended" and nc.fleet_size == rows[0].fleet_size:
            out.append(
                f"After recommended staffing at {nc.fleet_size} robots the limit is "
                f"{constraint_words(nc.constraint)}; {nc.explanation}."
            )
    unmet = [(row.fleet_size, row.recommended.unmet) for row in rows if row.recommended.unmet > 0]
    if unmet:
        out.append(
            "Even with recommended staffing some demand is not completed: "
            + ", ".join(f"{u} visits/week at {n} robots" for n, u in unmet)
            + "."
        )
    for staffing in ("recommended", "current"):
        first = [lv for lv in levers if lv.staffing == staffing and lv.fleet_size == rows[0].fleet_size]
        if first:
            lv = first[0]
            unk = " (an unknown company input)" if lv.unknown else ""
            out.append(
                f"Biggest single lever at {lv.fleet_size} robots with the {staffing} team: {lv.label}{unk} "
                f"{lv.change} ({num(lv.from_value)} -> {num(lv.to_value)} {lv.unit}) adds {lv.delta_done} "
                f"completed deployments a week ({lv.done_before} -> {lv.done_after})."
            )
    flagged = [
        f"{row.fleet_size} robots {side_name}: {', '.join(side.flags)}"
        for row in rows
        for side_name, side in (("current", row.current), ("recommended", row.recommended))
        if side.flags
    ]
    if flagged:
        out.append("Model flags: " + "; ".join(flagged) + ".")
    unattainable = [row.fleet_size for row in rows if row.recommended.status != "MET"]
    if unattainable:
        out.append(
            "The staffing search could not meet its goal at "
            + ", ".join(f"{n} robots" for n in unattainable)
            + " (see the status reason)."
        )
    shortfall = [row for row in rows if row.current.supervisor_shortfall]
    if shortfall:
        out.append(
            "The current team has too few ops leads for its crew span at "
            + ", ".join(f"{r.fleet_size} robots" for r in shortfall)
            + "."
        )
    if unknown:
        out.append(
            f"These results depend on {len(unknown)} unknown company inputs (illustrative placeholders): "
            + ", ".join(u.key for u in unknown)
            + "."
        )
    out.append(
        "This is a forecast from stated assumptions, not a company operating plan; the staffing search is "
        "a heuristic, not a global optimum."
    )
    return out


# ------------------------------------------------------------------------------------------- the report
def founder_report(
    assumption: str = "base",
    *,
    values: Mapping[str, float | None] | None = None,
    baseline: BaselineInfo | None = None,
) -> Report:
    """The founder report for one assumption set. `values` defaults to the registry values for `assumption`;
    pass `baseline` (from `load_baseline`) when the values include platform measurements."""
    if assumption not in ASSUMPTIONS:
        raise ValueError(f"assumption must be one of {', '.join(ASSUMPTIONS)}")
    a: Assumption = assumption  # type: ignore[assignment]
    vals: dict[str, float | None] = dict(registry_values(a) if values is None else values)
    if baseline is None:
        baseline = BaselineInfo(
            BASELINE_UNAVAILABLE
            if not baseline_available()
            else "platform baseline not applied to this report; registry values used"
        )
    rows = comparison(vals)
    configured = M.configured_team(vals)
    triggers = hiring_triggers(configured, [(row.fleet_size, _team_of(row.recommended)) for row in rows])
    nexts: list[NextConstraint] = []
    levers: list[Lever] = []
    for row in rows:
        nexts.append(next_constraint(vals, row.fleet_size, configured, staffing="current"))
        nexts.append(next_constraint(vals, row.fleet_size, _team_of(row.recommended), staffing="recommended"))
        levers.extend(top_levers(vals, row.fleet_size, "current"))
        levers.extend(top_levers(vals, row.fleet_size, "recommended"))
    unknown = unknown_inputs(vals, rows)
    return Report(
        assumption=a,
        banner=BANNER,
        fleet_sizes=tuple(FLEET_SIZES),
        comparison=rows,
        triggers=triggers,
        next_constraints=nexts,
        levers=levers,
        lever_exclusions=dict(NOT_LEVERS),
        worked_example=worked_example(vals, FLEET_SIZES[0]),
        unknown_inputs=unknown,
        provenance=provenance_table(a, vals, baseline),
        takeaways=takeaways(rows, triggers, nexts, levers, unknown),
        baseline=baseline,
        notes=NOTES,
    )


# ------------------------------------------------------------------------------------------- tables (shared)
COMPARISON_HEADER = (
    "fleet_size",
    "staffing",
    "crews",
    "vehicles",
    "engineers",
    "stations",
    "ops_leads",
    "completed_per_week",
    "demand_per_week",
    "unmet_per_week",
    "binding",
    "flags",
    "monthly_total_scu",
    "cost_per_completed_deployment_scu",
    "status",
    "reason",
)


def comparison_table(report: Report) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for row in report.comparison:
        for name, s in (("current", row.current), ("recommended", row.recommended)):
            out.append(
                (
                    str(row.fleet_size),
                    name,
                    str(s.crews),
                    str(s.vehicles),
                    str(s.engineers),
                    str(s.stations),
                    str(s.ops_leads),
                    str(s.done),
                    str(row.demand),
                    str(s.unmet),
                    s.binding,
                    ";".join(s.flags),
                    f"{s.total_cost:.2f}",
                    "N/A" if s.cost_per_deployment is None else f"{s.cost_per_deployment:.2f}",
                    s.status,
                    s.reason,
                )
            )
    return out


def _tables(report: Report) -> dict[str, tuple[tuple[str, ...], list[tuple[str, ...]]]]:
    def opt(x: float | None) -> str:
        return "" if x is None else f"{x:g}"

    return {
        "comparison": (COMPARISON_HEADER, comparison_table(report)),
        "triggers": (
            (
                "resource",
                "label",
                "current_team",
                *(f"recommended_at_{n}" for n in report.fleet_sizes),
                "first_rise_at_fleet_size",
            ),
            [
                (
                    t.resource,
                    t.label,
                    str(t.configured),
                    *(str(c) for _, c in t.counts),
                    "" if t.first_rise is None else str(t.first_rise),
                )
                for t in report.triggers
            ],
        ),
        "next_constraints": (
            (
                "fleet_size",
                "staffing",
                "constraint",
                "relieved_by",
                "next_constraint",
                "completed_before",
                "completed_after",
                "explanation",
            ),
            [
                (
                    str(n.fleet_size),
                    n.staffing,
                    n.constraint,
                    n.relieved_by,
                    n.next_constraint,
                    str(n.done_before),
                    str(n.done_after),
                    n.explanation,
                )
                for n in report.next_constraints
            ],
        ),
        "levers": (
            (
                "fleet_size",
                "staffing",
                "rank",
                "key",
                "label",
                "change",
                "from_value",
                "to_value",
                "unit",
                "completed_before",
                "completed_after",
                "delta_completed",
                "cost_per_completed_deployment_scu",
                "unknown_company_data",
            ),
            [
                (
                    str(lv.fleet_size),
                    lv.staffing,
                    str(lv.rank),
                    lv.key,
                    lv.label,
                    lv.change,
                    opt(lv.from_value),
                    opt(lv.to_value),
                    lv.unit,
                    str(lv.done_before),
                    str(lv.done_after),
                    str(lv.delta_done),
                    "N/A" if lv.cost_per_deployment is None else f"{lv.cost_per_deployment:.2f}",
                    "yes" if lv.unknown else "no",
                )
                for lv in report.levers
            ],
        ),
        "worked_example": (
            ("section", "label", "value", "formula"),
            [(s.section, s.label, s.display, s.formula) for s in report.worked_example],
        ),
        "unknown_inputs": (
            (
                "key",
                "label",
                "unit",
                "basis",
                "value_used",
                "conservative",
                "base",
                "optimistic",
                "source",
                "notes",
            ),
            [
                (
                    u.key,
                    u.label,
                    u.unit,
                    u.time_basis,
                    opt(u.value_used),
                    opt(u.conservative),
                    opt(u.base),
                    opt(u.optimistic),
                    u.source,
                    u.notes,
                )
                for u in report.unknown_inputs
            ],
        ),
        "provenance": (
            (
                "key",
                "label",
                "unit",
                "basis",
                "provenance",
                "value_used",
                "registry_value",
                "value_source",
                "source",
                "notes",
            ),
            [
                (
                    p.key,
                    p.label,
                    p.unit,
                    p.time_basis,
                    p.provenance,
                    "unconstrained" if p.value_used is None else f"{p.value_used:g}",
                    "unconstrained" if p.registry_value is None else f"{p.registry_value:g}",
                    p.value_source,
                    p.source,
                    p.notes,
                )
                for p in report.provenance
            ],
        ),
    }


def render_csv(report: Report) -> dict[str, str]:
    """One CSV per table, keyed by file name (e.g. "comparison.csv")."""
    out: dict[str, str] = {}
    for name, (header, rows) in _tables(report).items():
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
        out[f"{name}.csv"] = buf.getvalue()
    return out


def _md_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    def cell(x: str) -> str:
        return x.replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(cell(h) for h in header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return lines


def render_markdown(report: Report) -> str:
    L: list[str] = []
    L += [
        f"# Capacity forecast: 5 / 10 / 25 / 50 robots ({report.assumption} assumptions)",
        "",
        f"**{report.banner}.**",
        "",
        f"Baseline: {report.baseline.status}."
        + (f" Window: {report.baseline.window}." if report.baseline.window else ""),
        "",
        "## Founder takeaways",
        "",
    ]
    L += [f"- {t}" for t in report.takeaways]
    L += ["", "## Current team vs recommended staffing", ""]
    head = (
        "Robots",
        "Staffing",
        "Crews",
        "Vehicles",
        "Engineers",
        "Stations",
        "Ops leads",
        "Completed/wk",
        "Demand/wk",
        "Unmet/wk",
        "Binding",
        "Flags",
        "Monthly total (SCU)",
        "Cost per completed (SCU)",
        "Status",
    )
    rows = []
    for r in comparison_table(report):
        cost = "N/A" if r[13] == "N/A" else money(float(r[13]))
        rows.append((*r[:11], r[11] or "-", money(float(r[12])), cost, r[14]))
    L += _md_table(head, rows)
    L += ["", "Status reasons:", ""]
    L += [
        f"- {row.fleet_size} robots: current - {row.current.reason}; recommended - {row.recommended.reason}"
        for row in report.comparison
    ]
    L += [
        "",
        "## Hiring and vehicle triggers",
        "",
        "The fleet size at which each recommended count first rises (the first step is compared with the "
        "current team).",
        "",
    ]
    L += _md_table(
        ("Resource", "Current team", *(f"At {n}" for n in report.fleet_sizes), "First rises at"),
        [
            (
                t.label,
                str(t.configured),
                *(str(c) for _, c in t.counts),
                "no rise" if t.first_rise is None else f"{t.first_rise} robots",
            )
            for t in report.triggers
        ],
    )
    L += ["", "## Next constraint after relieving the current one", ""]
    L += _md_table(
        (
            "Robots",
            "Staffing",
            "Constraint now",
            "Relieved by",
            "Next constraint",
            "Completed now",
            "Completed after",
        ),
        [
            (
                str(n.fleet_size),
                n.staffing,
                constraint_words(n.constraint),
                n.relieved_by,
                constraint_words(n.next_constraint),
                str(n.done_before),
                str(n.done_after),
            )
            for n in report.next_constraints
        ],
    )
    L += [
        "",
        f"## Top {TOP_LEVERS} levers by incremental completed deployments",
        "",
        "Each input moved -20% / +20% (feasible values only); only changes that add completed deployments "
        "are listed. Not offered as levers: "
        + "; ".join(f"{k} ({why})" for k, why in report.lever_exclusions.items())
        + ".",
        "",
    ]
    L += _md_table(
        (
            "Robots",
            "Staffing",
            "#",
            "Input",
            "Change",
            "From",
            "To",
            "Completed/wk",
            "Added",
            "Cost per completed (SCU)",
            "Unknown input",
        ),
        [
            (
                str(lv.fleet_size),
                lv.staffing,
                str(lv.rank),
                f"{lv.label} (`{lv.key}`)",
                lv.change,
                f"{num(lv.from_value)} {lv.unit}",
                f"{num(lv.to_value)} {lv.unit}",
                f"{lv.done_before} -> {lv.done_after}",
                f"+{lv.delta_done}",
                money(lv.cost_per_deployment),
                "UNKNOWN" if lv.unknown else "",
            )
            for lv in report.levers
        ],
    )
    L += ["", f"## Worked example: {report.fleet_sizes[0]} robots", ""]
    L += _md_table(
        ("Step", "Item", "Value", "How"),
        [(s.section, s.label, s.display, s.formula) for s in report.worked_example],
    )
    L += [
        "",
        "## Unknown company inputs these results depend on",
        "",
        "Illustrative placeholders, not company data. Replace them with real figures before relying on any "
        "number above.",
        "",
    ]
    L += _md_table(
        ("Input", "Unit", "Value used", "Conservative / base / optimistic", "Source"),
        [
            (
                f"**UNKNOWN** {u.label} (`{u.key}`)",
                f"{u.unit} {u.time_basis}",
                num(u.value_used),
                f"{num(u.conservative)} / {num(u.base)} / {num(u.optimistic)}",
                u.source,
            )
            for u in report.unknown_inputs
        ],
    )
    L += ["", "## Input provenance", ""]
    L += _md_table(
        ("Input", "Unit", "Provenance", "Value used", "Value from", "Source"),
        [
            (
                f"{p.label} (`{p.key}`)",
                f"{p.unit} {p.time_basis}",
                "**UNKNOWN company data**" if p.provenance == "unknown_company_data" else p.provenance,
                num(p.value_used),
                p.value_source,
                p.source + (f" ({p.notes})" if p.notes else ""),
            )
            for p in report.provenance
        ],
    )
    L += ["", "## Notes and approximations", ""]
    L += [f"- {n}" for n in report.notes]
    L.append("")
    return "\n".join(L)

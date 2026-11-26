"""Plan form guidance (v2 01D, J3-1): labelled options and a read-only suggestion.

READ ONLY. Nothing here books, reserves, approves, locks or writes an audit row; the operator still presses
"Save plan", and the guarded ``planning.services.plan_visit`` re-checks everything under its own locks.

Option labels ("NB-011 · free", "P-111 · Jordan Lee · busy V-0056 12:35–16:25", "NB-012 · not ready: on
hold") are computed for the visit's current window with a fixed number of set-based statements, whatever the
fleet size:

* robots: one read of ``operations_robot_eligibility`` (the set-based view that is oracle-tested equal to
  ``fleet.services.evaluate_eligibility``, i.e. the planner's own robot check) + one approved-profile lookup
  per configuration (load / charge minutes, as ``evaluate`` uses them);
* people: one Person read and one Qualification read (the same qualification rules as ``evaluate``: lead =
  field lead, second = field operator or field lead, both robot handling, valid through the whole activity);
* vehicles: one read (capacity and restraint kit recorded, as ``evaluate`` requires);
* busy: one read of active reservations overlapping the window (other visits only).

The window is the planner's own timeline (inspection, loading, travel, on site, travel back, unloading,
charging), with unknown durations counted as 0 minutes; ``evaluate`` reports unknown durations itself.

Suggestion (``suggest``): candidates are taken in this documented, deterministic order: options labelled
free, sorted by code (robots, then leads, then seconds different from the lead, then vehicles). At most
SUGGEST_MAX_EVALUATIONS combinations (robot i, lead i, second i, vehicle i along a short diagonal, never the
full cross product) are checked with ``planning.services.evaluate`` - the same function the guarded plan
command runs - and the first that it does not block is returned. Budget: at most
SUGGEST_MAX_EVALUATIONS evaluate() calls; tests/test_v2_01d_planning_help.py pins the page statement count.
Planning eligibility is never departure readiness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db import connection
from django.db.models.expressions import RawSQL

from fleet import services as fleet_services
from fleet.location import AT_RISK
from fleet.location import COLUMNS as location_columns
from fleet.location import from_row as location_from_row
from fleet.models import ManifestPattern, Robot
from hub import charging
from planning import services as p
from planning.models import Person, Qualification, Reservation, Vehicle, Visit
from planning.plain import plain_blockers, robot_reason
from platform_core.templatetags.ui import local_dt, local_time

SUGGEST_MAX_EVALUATIONS = 4
FREE, BUSY, UNUSABLE = "free", "busy", "unusable"
_RANK = {FREE: 0, BUSY: 1, UNUSABLE: 2}

ROBOT_SQL = (
    "SELECT e.robot_id, e.robot_code, e.pattern_code, e.eligible, e.reasons, "
    # v2 03A-2 section 12: the future-planning location (away on an open run: its hub and reserved window)
    f"{location_columns} FROM operations_robot_eligibility e "
    "CROSS JOIN LATERAL fleet_planning_location(e.robot_id) l "
    "WHERE e.lifecycle <> 'retired' ORDER BY e.robot_code"
)


@dataclass
class Option:
    code: str
    label: str
    status: str
    selected: bool = False
    obj: Any = None
    note: str = ""
    at_risk: str = ""  # v2 03A-2 section 12: "At risk: robot away until verified back at hub"


@dataclass
class PlanOptions:
    charge_on: bool = False
    robots: list[Option] = field(default_factory=list)
    leads: list[Option] = field(default_factory=list)
    seconds: list[Option] = field(default_factory=list)
    vehicles: list[Option] = field(default_factory=list)
    # 03B closure (P2): options of other hubs left out by ``restrict_to_hub``, counted per list, and the
    # unrestricted options they came from (Review options still checks a forged robot or vehicle code and says
    # the database would refuse it)
    elsewhere: dict[str, int] = field(default_factory=dict)
    everything: PlanOptions | None = field(default=None, repr=False)

    @property
    def elsewhere_text(self) -> str:
        """``Not offered: 3 robots, 12 people and 2 vehicles at other hubs.`` (empty when none)."""
        robots, people, vehicles = (
            self.elsewhere.get("robots", 0),
            max(self.elsewhere.get("leads", 0), self.elsewhere.get("seconds", 0)),
            self.elsewhere.get("vehicles", 0),
        )
        parts = [
            f"{n} {one if n == 1 else many}"
            for n, one, many in (
                (robots, "robot", "robots"),
                (people, "person", "people"),
                (vehicles, "vehicle", "vehicles"),
            )
            if n
        ]
        if not parts:
            return ""
        listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return f"Not offered: {listed} at other hubs."


@dataclass
class Suggestion:
    robot: Option | None = None
    lead: Option | None = None
    second: Option | None = None
    vehicle: Option | None = None
    explanations: list[str] = field(default_factory=list)  # why each pick fits, in plain words
    why_not: list[str] = field(default_factory=list)  # when nothing fits: the constraints, in plain words
    evaluations: int = 0

    @property
    def found(self) -> bool:
        return self.robot is not None


def _sort(opts: list[Option]) -> list[Option]:
    return sorted(opts, key=lambda o: (_RANK[o.status], o.code))


def _span(s: datetime, e: datetime) -> str:
    return f"{local_time(s)}–{local_time(e)}"


def _until(e: datetime, ref: datetime) -> str:
    """``16:25`` on the visit's own local day, else the full local date and time."""
    return local_time(e) if local_dt(e).split(",")[0] == local_dt(ref).split(",")[0] else local_dt(e)


class _Busy:
    """Active reservations of OTHER visits overlapping [lo, hi), grouped per resource and visit.

    v2 03B-3: the rows are indexed by (kind, resource id) once, so the same read answers many windows (the
    next free time search asks one per candidate time). The answers are the same as a scan of every row."""

    def __init__(self, visit: Visit | None, lo: datetime, hi: datetime) -> None:
        qs = Reservation.objects.filter(released_at__isnull=True, starts_at__lt=hi, ends_at__gt=lo)
        if visit is not None and visit.pk is not None:
            qs = qs.exclude(revision__visit_id=visit.pk)
        self.rows = list(
            qs.values_list(
                "resource__kind",
                "resource__robot_id",
                "resource__person_id",
                "resource__vehicle_id",
                "revision__visit__code",
                "starts_at",
                "ends_at",
            ).order_by("starts_at")
        )
        self.by: dict[tuple[str, int], list[tuple[Any, ...]]] = {}
        col = {"robot": 1, "person": 2, "vehicle": 3}
        for row in self.rows:
            kind = str(row[0])
            pk: Any = row[col[kind]] if kind in col else None
            if pk is not None:
                self.by.setdefault((kind, int(pk)), []).append(row)

    def clash(self, kind: str, pk: int, s0: datetime, s1: datetime) -> tuple[str, datetime, datetime] | None:
        """(visit code, its first start, its last end) of the earliest overlapping booking, or None."""
        hits: dict[str, list[datetime]] = {}
        for row in self.by.get((kind, pk), ()):
            if row[5] < s1 and row[6] > s0:
                hits.setdefault(row[4], []).extend([row[5], row[6]])
        if not hits:
            return None
        code = min(hits, key=lambda c: min(hits[c]))
        return code, min(hits[code]), max(hits[code])


@dataclass
class World:
    """Everything the option labels read, read ONCE (a fixed number of statements). ``options_at`` then labels
    the options for any window without another statement (v2 03B-3: the next free time search reuses it)."""

    robot_rows: list[tuple[Any, ...]]
    loads: dict[str, tuple[int, int]]  # pattern code -> (load/unload, charge turnaround) minutes; 0 unknown
    robots: dict[int, Robot]
    people: list[Person]
    quals: dict[int, list[tuple[str, datetime, datetime]]]
    vehicles: list[Any]  # Vehicle, annotated with ``moving``
    busy: _Busy
    insp: int
    charge_on: bool = False
    forecasts: dict[int, Any] = field(default_factory=dict)


def read_robots() -> tuple[list[tuple[Any, ...]], dict[str, tuple[int, int]]]:
    with connection.cursor() as cur:
        cur.execute(ROBOT_SQL)
        robot_rows = cur.fetchall()
    loads: dict[str, tuple[int, int]] = {}
    for code in sorted({r[2] for r in robot_rows if r[2]}):
        prof = fleet_services.approved_profile(ManifestPattern.objects.get(code=code))
        load = prof.load_unload_min if prof and prof.load_unload_min and prof.load_unload_min > 0 else 0
        charge = (
            prof.charge_turnaround_min
            if prof and prof.charge_turnaround_min and prof.charge_turnaround_min > 0
            else 0
        )
        loads[code] = (load, charge)
    return robot_rows, loads


def read_rest(
    world_robots: tuple[list[tuple[Any, ...]], dict[str, tuple[int, int]]],
    visit: Visit | None,
    *,
    busy: tuple[datetime, datetime],
    quals: tuple[datetime, datetime],
    insp: int,
    forecast_at: datetime | None = None,
) -> World:
    """The rest of the World: reservations overlapping ``busy``; qualifications valid from at most
    ``quals[0]`` to at least ``quals[1]`` (each kind's own window is checked in ``options_at``); robots,
    people and vehicles. ``forecast_at``: the charge forecast at that departure (plan options only)."""
    robot_rows, loads = world_robots
    b = _Busy(visit, *busy)
    robots = {r.pk: r for r in Robot.objects.filter(pk__in=[r[0] for r in robot_rows])}
    # v2 02B (contract 11, 8.3): the charge forecast for this visit's departure, once for every robot. A robot
    # is left out ONLY when the approved charge turnaround proves it cannot be charged in time; Unknown stays
    # selectable and says so. Off (no label) while ops_soc_fresh_min is not provisioned.
    charge_on = forecast_at is not None and charging.forecast_configured()
    forecasts = (
        charging.robot_forecasts([r[0] for r in robot_rows if r[3]], forecast_at)
        if charge_on and forecast_at is not None
        else {}
    )
    people = list(Person.objects.select_related("user").order_by("code"))
    q: dict[int, list[tuple[str, datetime, datetime]]] = {}
    for pid, kind, v0, v1 in Qualification.objects.filter(
        revoked_at__isnull=True,
        kind__in=["field_lead", "field_operator", "robot_handler"],
        valid_from__lte=quals[0],  # each kind's own window is checked in options_at
        valid_to__gte=quals[1],
    ).values_list("person_id", "kind", "valid_from", "valid_to"):
        q.setdefault(pid, []).append((kind, v0, v1))
    # v2 03A-2: whether it is on its way to another hub (its latest transfer step not received), same read
    moving = RawSQL(
        "(SELECT t.state <> 'received' FROM fleet_hub_transfer t WHERE t.vehicle_id = planning_vehicle.id"
        " ORDER BY t.id DESC LIMIT 1)",
        [],
    )
    vehicles = list(Vehicle.objects.annotate(moving=moving).order_by("code"))
    return World(robot_rows, loads, robots, people, q, vehicles, b, insp, charge_on, forecasts)


def windows(
    world: World, start: datetime, end: datetime, t_out: int, t_back: int, ref_pattern: str | None = None
) -> dict[str, Any]:
    """The planner's own timeline windows for an on-site span (unknown durations counted as 0 minutes)."""
    insp = world.insp
    ref_load = world.loads.get(ref_pattern or "", (0, 0))[0] or max(
        (v[0] for v in world.loads.values()), default=0
    )

    def robot_window(pattern: str | None) -> tuple[datetime, datetime]:
        load, charge = world.loads.get(pattern or "", (0, 0))
        return (
            start - timedelta(minutes=t_out + load + insp),
            end + timedelta(minutes=t_back + load + charge),
        )

    shift = (start - timedelta(minutes=t_out + ref_load + insp), end + timedelta(minutes=t_back + ref_load))
    handling = (start - timedelta(minutes=t_out + ref_load), shift[1])
    return {"robot": robot_window, "shift": shift, "handling": handling, "trip": handling}


def options_at(
    world: World,
    hub_id: int | None,
    start: datetime,
    end: datetime,
    t_out: int,
    t_back: int,
    *,
    selected: tuple[Any, Any, Any, Any] = (None, None, None, None),
    ref_pattern: str | None = None,
) -> PlanOptions:
    """The labelled options for one window, from ``world`` only (no statement). ``selected``: the robot, lead,
    second and vehicle ids of the current plan."""
    w = windows(world, start, end, t_out, t_back, ref_pattern)
    robot_window, shift, handling, trip = w["robot"], w["shift"], w["handling"], w["trip"]
    busy = world.busy
    out = PlanOptions(charge_on=world.charge_on)

    # robots: the planner's eligibility (same view the fleet overview uses), then conflicts
    for robot_id, code, pattern, eligible, reasons, *loc_row in world.robot_rows:
        f = world.forecasts.get(robot_id)
        where = location_from_row(loc_row)
        risk = ""
        if not eligible:
            shorts = list(dict.fromkeys(robot_reason(x).short for x in reasons))
            status, note = UNUSABLE, "not ready: " + ", ".join(shorts)
        elif (why := where.planning_problem_for(hub_id, *robot_window(pattern))) is not None:
            # v2 03A-2 section 12: the future-planning predicate for this visit's hub and window
            status, note = (
                UNUSABLE,
                (
                    "at another hub"
                    if where.kind == "hub"
                    else "away during this window"
                    if "overlaps" in why
                    else "away on another hub's run"
                ),
            )
        elif f is not None and f.cannot_in_time:
            status, note = UNUSABLE, "cannot charge in time"
        elif c := busy.clash("robot", robot_id, *robot_window(pattern)):
            status, note = BUSY, f"busy {c[0]} until {_until(c[2], end)}"
        else:
            status, note = FREE, "free"
        if status != UNUSABLE and f is not None and f.state != charging.ENOUGH:
            note += " · needs charge" if f.state == charging.NEEDS else " · charge forecast unknown"
        if status != UNUSABLE and where.away:
            risk = AT_RISK
        out.robots.append(
            Option(
                code,
                f"{code} · {note}" + (f" · {risk}" if risk else ""),
                status,
                robot_id == selected[0],
                world.robots.get(robot_id),
                note,
                risk,
            )
        )
    out.robots = _sort(out.robots)

    # people: qualifications valid through the whole activity (as planning.services.evaluate), then conflicts
    def has(pid: int, kinds: list[str], win: tuple[datetime, datetime]) -> bool:
        return any(k in kinds and v0 <= win[0] and v1 >= win[1] for k, v0, v1 in world.quals.get(pid, []))

    for _role, kinds, words, bucket, selected_id in (
        ("lead", ["field_lead"], "not qualified as lead", out.leads, selected[1]),
        (
            "second",
            ["field_operator", "field_lead"],
            "not qualified as field operator",
            out.seconds,
            selected[2],
        ),
    ):
        for person in world.people:
            if not has(person.pk, kinds, shift):
                status, note = UNUSABLE, words
            elif not has(person.pk, ["robot_handler"], handling):
                status, note = UNUSABLE, "not qualified for robot handling"
            elif c := busy.clash("person", person.pk, *shift):
                status, note = BUSY, f"busy {c[0]} {_span(c[1], c[2])}"
            else:
                status, note = FREE, "free"
            bucket.append(
                Option(
                    person.code, f"{person.label} · {note}", status, person.pk == selected_id, person, note
                )
            )
        # v2 03B-3: people based at the visit's hub first (a home hub confers no authority; it is only where
        # the crew starts), then the usual order
        bucket[:] = sorted(
            bucket,
            key=lambda o: (_RANK[o.status], hub_id is not None and o.obj.hub_id != hub_id, o.code),
        )

    # vehicles: capacity and restraint kit recorded (as evaluate requires), then conflicts
    for v in world.vehicles:
        if getattr(v, "moving", None):
            status, note = UNUSABLE, "on its way to another hub"
        elif v.hub_id != hub_id:
            status, note = UNUSABLE, "at another hub"
        elif v.robot_capacity is None:
            status, note = UNUSABLE, "capacity not recorded"
        elif not v.restraint_kit:
            status, note = UNUSABLE, "no restraint kit recorded"
        elif c := busy.clash("vehicle", v.pk, *trip):
            status, note = BUSY, f"busy {c[0]} until {_until(c[2], end)}"
        else:
            status, note = FREE, "free"
        out.vehicles.append(Option(v.code, f"{v.code} · {note}", status, v.pk == selected[3], v, note))
    out.vehicles = _sort(out.vehicles)
    return out


# Robot and vehicle notes that mean "not at this visit's hub" (options_at's own words)
OTHER_HUB_NOTES = ("at another hub", "away on another hub's run", "on its way to another hub")
ELSEWHERE_WORDS = {
    "robots": ("robot", "robots"),
    "leads": ("person", "people"),
    "seconds": ("person", "people"),
    "vehicles": ("vehicle", "vehicles"),
}


def elsewhere_line(kind: str, n: int) -> str:
    """``12 people at other hubs are not offered`` (03B closure P2)."""
    one, many = ELSEWHERE_WORDS[kind]
    return f"1 {one} at another hub is not offered" if n == 1 else f"{n} {many} at other hubs are not offered"


def restrict_to_hub(opts: PlanOptions, hub_id: int | None) -> PlanOptions:
    """03B closure (contract 14.7, P2): the choices offered for a visit are its own hub's only.

    * robots: based at the visit's hub (``fleet_robot.hub_id``, the rule of the database trigger
      ``planning_visit_hub_guard``) and not placed at or away for another hub by the section 12 planning
      predicate (``options_at`` notes "at another hub", "away on another hub's run");
    * people: home hub (``planning_person.hub_id``) is the visit's hub;
    * vehicles: based at the visit's hub and not on their way to another hub (the trigger's rule).

    Everything else is only counted in ``elsewhere``. The current plan's own choice is always kept, so a
    form never silently changes it. The labels and the guarded plan command are unchanged: the command and
    the trigger still refuse another hub's robot or vehicle, whatever a form sends. Read only, no statement.
    """
    if hub_id is None:
        return opts
    out = PlanOptions(charge_on=opts.charge_on, everything=opts)
    for name in ("robots", "leads", "seconds", "vehicles"):
        kept, other = [], 0
        for o in getattr(opts, name):
            obj_hub = getattr(o.obj, "hub_id", hub_id)
            away = obj_hub != hub_id or (name in ("robots", "vehicles") and o.note in OTHER_HUB_NOTES)
            if away and not o.selected:
                other += 1
            else:
                kept.append(o)
        setattr(out, name, kept)
        if other:
            out.elsewhere[name] = other
    return out


def plan_options(visit: Visit) -> PlanOptions:
    rev = visit.current_revision
    assert rev is not None
    start, end = rev.on_site_start, rev.on_site_end
    t_out, t_back = rev.travel_out_min or 0, rev.travel_back_min or 0
    route = p.new_route(visit, start, end)  # v2 03B-1: a zoned visit's windows use the matrix in force
    if route is not None:
        t_out, t_back = route.out.minutes or 0, route.back.minutes or 0
    insp = p._int_setting("pre_visit_inspection_min") or 0
    rr = read_robots()
    robot_rows, loads = rr
    current_pattern = next((r[2] for r in robot_rows if r[0] == rev.robot_id), None)
    probe = World(robot_rows, loads, {}, [], {}, [], None, insp)  # type: ignore[arg-type]
    w = windows(probe, start, end, t_out, t_back, current_pattern)
    lo = min([w["shift"][0]] + [w["robot"](r[2])[0] for r in robot_rows])
    hi = max([w["shift"][1]] + [w["robot"](r[2])[1] for r in robot_rows])
    world = read_rest(
        rr,
        visit,
        busy=(lo, hi),
        quals=(w["handling"][0], w["shift"][1]),
        insp=insp,
        forecast_at=start - timedelta(minutes=t_out),
    )
    return options_at(
        world,
        visit.hub_id,
        start,
        end,
        t_out,
        t_back,
        selected=(rev.robot_id, rev.lead_id, rev.second_id, rev.vehicle_id),
        ref_pattern=current_pattern,
    )


def _free(opts: list[Option]) -> list[Option]:
    return [o for o in opts if o.status == FREE]


def suggest(visit: Visit, opts: PlanOptions) -> Suggestion:
    """The first combination of free options that ``planning.services.evaluate`` does not block. Read only."""
    rev = visit.current_revision
    assert rev is not None
    out = Suggestion()
    robots, leads, seconds, vehicles = (
        _free(x) for x in (opts.robots, opts.leads, opts.seconds, opts.vehicles)
    )
    missing = [
        text
        for pool, text in (
            (robots, "No robot is free and meets the planning checks for this window."),
            (leads, "Nobody qualified as lead is free for this window."),
            (seconds, "Nobody qualified as second (field operator) is free for this window."),
            (vehicles, "No vehicle with a recorded capacity and restraint kit is free for this window."),
        )
        if not pool
    ]
    if not missing and len({o.code for o in leads} | {o.code for o in seconds}) < 2:
        missing.append("Two different qualified people are needed, and only one is free.")
    if missing:
        out.why_not = missing
        return out
    blocked_reasons: list[str] = []
    # v2 03B-1: a zoned visit is checked against the travel matrix in force (a missing pair blocks every
    # combination: "Travel time unknown", never offered as free)
    route = p.new_route(visit, rev.on_site_start, rev.on_site_end)
    for i in range(SUGGEST_MAX_EVALUATIONS):
        robot = robots[min(i, len(robots) - 1)]
        lead = leads[min(i, len(leads) - 1)]
        second = next((s for s in seconds[i:] + seconds[:i] if s.code != lead.code), None)
        vehicle = vehicles[min(i, len(vehicles) - 1)]
        if second is None:
            continue
        out.evaluations += 1
        plan = p.evaluate(
            site=visit.site,
            start=rev.on_site_start,
            end=rev.on_site_end,
            travel_out=rev.travel_out_min,
            travel_back=rev.travel_back_min,
            tasks=rev.tasks,
            scopes=rev.required_consent_scopes,
            robot=robot.obj,
            lead=lead.obj,
            second=second.obj,
            vehicle=vehicle.obj,
            visit=visit,
            travel=route,
        )
        if plan.feasibility != "blocked":
            out.robot, out.lead, out.second, out.vehicle = robot, lead, second, vehicle
            charge = robot.note.split(" · ", 1)[1] if " · " in robot.note else ""
            out.explanations = [
                f"Robot {robot.code} has no recorded planning conflict and meets the current planning "
                "checks."
                + (
                    f" Its {charge}: it must be charged and pass the battery check before it leaves."
                    if charge
                    else ""
                ),
                # v2 03A-2 section 12: a robot away on an open run of this hub, outside that run's window
                *(
                    [f"{robot.at_risk}: it cannot be authorized to depart until its return is recorded."]
                    if robot.at_risk
                    else []
                ),
                f"{lead.obj.label} is qualified as lead and has no other booking in this window.",
                f"{second.obj.label} is qualified as second and has no other booking in this window.",
                f"Vehicle {vehicle.code} has a recorded capacity and restraint kit and no other trip "
                "in this window.",
                # v2 03B-1 (10.H): which approved matrix values produced this suggestion
                *(
                    [
                        f"Travel out {route.out.label}, back {route.back.label}, from the travel matrix "
                        f"(rows {route.out.version_id} and {route.back.version_id})."
                    ]
                    if route is not None and route.known
                    else []
                ),
            ]
            return out
        for b in plain_blockers([x for x in plan.blockers if x["level"] == "blocked"]):
            if b["sentence"] not in blocked_reasons:
                blocked_reasons.append(b["sentence"])
        if i + 1 >= max(len(robots), len(leads), len(seconds), len(vehicles)):
            break
    out.why_not = [
        "The free robots, people and vehicles checked still leave this plan blocked:"
    ] + blocked_reasons[:5]
    return out

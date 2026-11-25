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

from fleet import services as fleet_services
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
    "SELECT robot_id, robot_code, pattern_code, eligible, reasons FROM operations_robot_eligibility "
    "WHERE lifecycle <> 'retired' ORDER BY robot_code"
)


@dataclass
class Option:
    code: str
    label: str
    status: str
    selected: bool = False
    obj: Any = None
    note: str = ""


@dataclass
class PlanOptions:
    charge_on: bool = False
    robots: list[Option] = field(default_factory=list)
    leads: list[Option] = field(default_factory=list)
    seconds: list[Option] = field(default_factory=list)
    vehicles: list[Option] = field(default_factory=list)


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
    """Active reservations of OTHER visits overlapping [lo, hi), grouped per resource and visit."""

    def __init__(self, visit: Visit, lo: datetime, hi: datetime) -> None:
        self.rows = list(
            Reservation.objects.filter(released_at__isnull=True, starts_at__lt=hi, ends_at__gt=lo)
            .exclude(revision__visit=visit)
            .values_list(
                "resource__kind",
                "resource__robot_id",
                "resource__person_id",
                "resource__vehicle_id",
                "revision__visit__code",
                "starts_at",
                "ends_at",
            )
            .order_by("starts_at")
        )

    def clash(self, kind: str, pk: int, s0: datetime, s1: datetime) -> tuple[str, datetime, datetime] | None:
        """(visit code, its first start, its last end) of the earliest overlapping booking, or None."""
        col = {"robot": 1, "person": 2, "vehicle": 3}[kind]
        hits: dict[str, list[datetime]] = {}
        for row in self.rows:
            if row[0] == kind and row[col] == pk and row[5] < s1 and row[6] > s0:
                hits.setdefault(row[4], []).extend([row[5], row[6]])
        if not hits:
            return None
        code = min(hits, key=lambda c: min(hits[c]))
        return code, min(hits[code]), max(hits[code])


def plan_options(visit: Visit) -> PlanOptions:
    rev = visit.current_revision
    assert rev is not None
    start, end = rev.on_site_start, rev.on_site_end
    t_out, t_back = rev.travel_out_min or 0, rev.travel_back_min or 0
    insp = p._int_setting("pre_visit_inspection_min") or 0

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
    current_pattern = next((r[2] for r in robot_rows if r[0] == rev.robot_id), None)
    ref_load = loads.get(current_pattern or "", (0, 0))[0] or max((v[0] for v in loads.values()), default=0)

    def robot_window(pattern: str | None) -> tuple[datetime, datetime]:
        load, charge = loads.get(pattern or "", (0, 0))
        return (
            start - timedelta(minutes=t_out + load + insp),
            end + timedelta(minutes=t_back + load + charge),
        )

    shift = (start - timedelta(minutes=t_out + ref_load + insp), end + timedelta(minutes=t_back + ref_load))
    handling = (start - timedelta(minutes=t_out + ref_load), shift[1])
    trip = handling
    lo = min([shift[0]] + [robot_window(r[2])[0] for r in robot_rows])
    hi = max([shift[1]] + [robot_window(r[2])[1] for r in robot_rows])
    busy = _Busy(visit, lo, hi)
    out = PlanOptions()

    # robots: the planner's eligibility (same view the fleet overview uses), then conflicts
    robots = {r.pk: r for r in Robot.objects.filter(pk__in=[r[0] for r in robot_rows])}
    # v2 02B (contract 11, 8.3): the charge forecast for this visit's departure, once for every robot. A robot
    # is left out ONLY when the approved charge turnaround proves it cannot be charged in time; Unknown stays
    # selectable and says so. Off (no label) while ops_soc_fresh_min is not provisioned.
    out.charge_on = charging.forecast_configured()
    forecasts = (
        charging.robot_forecasts([r[0] for r in robot_rows if r[3]], start - timedelta(minutes=t_out))
        if out.charge_on
        else {}
    )
    for robot_id, code, pattern, eligible, reasons in robot_rows:
        f = forecasts.get(robot_id)
        home = robots.get(robot_id)
        if not eligible:
            shorts = list(dict.fromkeys(robot_reason(x).short for x in reasons))
            status, note = UNUSABLE, "not ready: " + ", ".join(shorts)
        elif home is not None and home.hub_id != visit.hub_id:
            # v2 03A-2 (10.A): eligible = verified at its hub, which is its home hub; only that hub may use it
            status, note = UNUSABLE, "at another hub"
        elif f is not None and f.cannot_in_time:
            status, note = UNUSABLE, "cannot charge in time"
        elif c := busy.clash("robot", robot_id, *robot_window(pattern)):
            status, note = BUSY, f"busy {c[0]} until {_until(c[2], end)}"
        else:
            status, note = FREE, "free"
        if status != UNUSABLE and f is not None and f.state != charging.ENOUGH:
            note += " · needs charge" if f.state == charging.NEEDS else " · charge forecast unknown"
        out.robots.append(
            Option(code, f"{code} · {note}", status, robot_id == rev.robot_id, robots.get(robot_id), note)
        )
    out.robots = _sort(out.robots)

    # people: qualifications valid through the whole activity (as planning.services.evaluate), then conflicts
    people = list(Person.objects.select_related("user").order_by("code"))
    quals: dict[int, list[tuple[str, datetime, datetime]]] = {}
    for pid, kind, v0, v1 in Qualification.objects.filter(
        revoked_at__isnull=True,
        kind__in=["field_lead", "field_operator", "robot_handler"],
        valid_from__lte=handling[0],  # each kind's own window is checked below
        valid_to__gte=shift[1],
    ).values_list("person_id", "kind", "valid_from", "valid_to"):
        quals.setdefault(pid, []).append((kind, v0, v1))

    def has(pid: int, kinds: list[str], w: tuple[datetime, datetime]) -> bool:
        return any(k in kinds and v0 <= w[0] and v1 >= w[1] for k, v0, v1 in quals.get(pid, []))

    for _role, kinds, words, bucket, selected_id in (
        ("lead", ["field_lead"], "not qualified as lead", out.leads, rev.lead_id),
        (
            "second",
            ["field_operator", "field_lead"],
            "not qualified as field operator",
            out.seconds,
            rev.second_id,
        ),
    ):
        for person in people:
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
        bucket[:] = _sort(bucket)

    # vehicles: capacity and restraint kit recorded (as evaluate requires), then conflicts
    for v in Vehicle.objects.order_by("code"):
        if v.robot_capacity is None:
            status, note = UNUSABLE, "capacity not recorded"
        elif not v.restraint_kit:
            status, note = UNUSABLE, "no restraint kit recorded"
        elif c := busy.clash("vehicle", v.pk, *trip):
            status, note = BUSY, f"busy {c[0]} until {_until(c[2], end)}"
        else:
            status, note = FREE, "free"
        out.vehicles.append(Option(v.code, f"{v.code} · {note}", status, v.pk == rev.vehicle_id, v, note))
    out.vehicles = _sort(out.vehicles)
    return out


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
                f"{lead.obj.label} is qualified as lead and has no other booking in this window.",
                f"{second.obj.label} is qualified as second and has no other booking in this window.",
                f"Vehicle {vehicle.code} has a recorded capacity and restraint kit and no other trip "
                "in this window.",
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

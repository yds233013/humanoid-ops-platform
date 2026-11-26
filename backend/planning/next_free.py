"""Next free time (v2 03B-3, screen S3 of docs/14_v2_03b_design_spec.md; contract 13.4 item 4, 13.5).

    next_free(visit_or_request, *, limit=3) -> NextFreeResult(slots, exhausted)

READ ONLY. Nothing here books, reserves, locks or writes. A slot is a suggestion: choosing it runs the ONE
guarded plan command (``planning.services.plan_visit``; for a new booking ``request_visit`` first), which
checks everything again under its locks, and the database triggers check it once more. A slot is never a
departure approval: "Available for planning" and "At risk" both still need departure readiness.

The search (13.4 item 4):

* Candidate on-site starts in 30-minute steps inside the hub's operating hours (owner setting
  ``hub_operating_hours``, per hub, in the HUB's IANA time zone), from now to ``dispatch_lookahead_days``
  (owner setting) days ahead. Wall times are converted by the zone (``planning.services.local_to_utc``), so a
  daylight-saving change moves nothing: a wall time that does not exist (spring forward) or exists twice
  (fall back) is skipped, never guessed. The visit keeps its length; it must end inside the opening hours.
* For each candidate, the planner's own option labels (``planning.options.options_at``: eligibility, the
  section 12 future-planning location predicate, qualifications, vehicles, existing bookings) pick the first
  combination in the deterministic order robot code, lead code, second code, vehicle code, among the robots,
  crew and vehicles based at the visit's hub. That combination is then checked with
  ``planning.services.evaluate`` - the same function the guarded plan command runs - with the travel-matrix
  route of that time (``planning.services.new_route``). Only evaluate decides; the labels only choose what
  to ask it. Up to ``TRIES_PER_TIME`` combinations are asked per time.
* A valid slot: evaluate reports no blocker about the robot, the crew, the vehicle, the travel time or an
  overlap. Facts about the home itself (assessment, consent, a regional pause) do not depend on the time;
  they are reported once in ``home_notes`` and the plan command still applies them.
* Ranking: slots with no at-risk item ("Available for planning") first, then "At risk" slots (robot away on
  a same-hub run, section 12); within each, earliest first, then robot code, crew codes, vehicle code.
* Budget: at most ``BUDGET`` (200) evaluations per request. Every candidate time counts once when the option
  labels rule it out, and every combination asked of evaluate counts once. When the budget runs out before
  the look-ahead is searched, ``exhausted`` is True and the page says so.
* A blocker that no robot, crew or vehicle can fix (travel time unknown, an inspection time not configured, a
  retired zone) ends the search at once and is named in ``stopped``: a missing travel pair is never "free"
  and never merely at risk (13.4 item 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from fleet import provisioning as pv
from fleet import services as fleet_services
from planning import options as popt
from planning import services as p
from planning.models import Site, Visit
from planning.plain import plain_blocker
from platform_core.commands import Invalid
from platform_core.models import Hub

STEP_MIN = 30
BUDGET = 200
TRIES_PER_TIME = 3
FREE, AT_RISK = "available", "at_risk"
STATUS_WORDS = {FREE: ("Available for planning", "●"), AT_RISK: ("At risk", "▲")}

# Blockers about the home, not about the time or the resources: reported once, never a reason to skip a slot.
HOME_CODES = {"SITE_NOT_ASSESSED", "SITE_NOT_ELIGIBLE", "SITE_RESTRICTED", "TASK_EXCLUDED", "CONSENT"}
# Blockers no robot, crew or vehicle can fix: the search stops and names them.
STOPPERS = {"TRAVEL_UNKNOWN", "UNKNOWN_TRAVEL", "UNKNOWN_INSPECTION_TIME", "ZONE_RETIRED"}


@dataclass
class Request:
    """A booking not saved yet ("Book a visit"): the home, the dispatch hub, how long on site, and the
    recorded travel minutes a home without a zone uses."""

    site: Site
    hub: Hub
    minutes: int
    tasks: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=lambda: ["presence"])
    travel_out_min: int | None = None
    travel_back_min: int | None = None


@dataclass
class Slot:
    start: datetime  # UTC
    end: datetime
    status: str  # available / at_risk
    robot: str
    lead: Any
    second: Any
    vehicle: str
    why: str  # one line, plain words
    tz: str  # the hub's IANA zone
    site_tz: str
    travel_versions: tuple[int | None, int | None] = (None, None)

    @property
    def words(self) -> str:
        return STATUS_WORDS[self.status][0]

    @property
    def shape(self) -> str:
        return STATUS_WORDS[self.status][1]

    @property
    def crew(self) -> str:
        from planning.board import crew_names  # noqa: PLC0415

        return crew_names(self.lead, self.second)

    @property
    def label(self) -> str:
        """``Tue 1 Dec, 18:30 PST`` in the hub's zone (the zone label comes from the IANA conversion)."""
        local = self.start.astimezone(ZoneInfo(self.tz))
        return f"{local:%a} {local.day} {local:%b}, {local:%H:%M %Z}"

    @property
    def span_label(self) -> str:
        """``Tue 1 Dec, 18:30–19:30 PST`` in the hub's zone."""
        zone = ZoneInfo(self.tz)
        s, e = self.start.astimezone(zone), self.end.astimezone(zone)
        return f"{s:%a} {s.day} {s:%b}, {s:%H:%M}–{e:%H:%M %Z}"

    @property
    def end_label(self) -> str:
        return f"{self.end.astimezone(ZoneInfo(self.tz)):%H:%M}"

    def form_value(self, which: str) -> str:
        """The value of a ``datetime-local`` field, in the home's own zone (the plan form reads it there)."""
        dt = self.start if which == "start" else self.end
        return dt.astimezone(ZoneInfo(self.site_tz)).strftime("%Y-%m-%dT%H:%M")

    @property
    def values(self) -> dict[str, str]:
        """Form values in the home's own zone: ``start``/``end`` for a datetime-local field (the plan form),
        ``day``/``start_time``/``end_time`` for the booking form."""
        zone = ZoneInfo(self.site_tz)
        s, e = self.start.astimezone(zone), self.end.astimezone(zone)
        return {
            "start": f"{s:%Y-%m-%dT%H:%M}",
            "end": f"{e:%Y-%m-%dT%H:%M}",
            "day": f"{s:%Y-%m-%d}",
            "start_time": f"{s:%H:%M}",
            "end_time": f"{e:%H:%M}",
        }

    @property
    def key(self) -> tuple[Any, ...]:
        """Ranking: Available for planning first, then At risk; earliest; robot, crew and vehicle codes."""
        return (
            0 if self.status == FREE else 1,
            self.start,
            self.robot,
            getattr(self.lead, "code", ""),
            getattr(self.second, "code", ""),
            self.vehicle,
        )


@dataclass
class NextFreeResult:
    slots: list[Slot]
    exhausted: bool  # the evaluation budget ran out before the whole look-ahead was searched
    evaluations: int = 0
    candidates: int = 0  # candidate times looked at
    days: int = 0  # the look-ahead searched
    hub_name: str = ""
    tz: str = ""
    stopped: str = ""  # a blocker no robot, crew or vehicle can fix, in plain words; empty when none
    not_set: str = ""  # an owner setting the search needs is missing, in plain words
    home_notes: list[str] = field(default_factory=list)  # about the home, whatever the time

    @property
    def empty_text(self) -> str:
        """Spec S3 empty state."""
        return (
            f"No free time in the next {self.days} day{'s' if self.days != 1 else ''} at {self.hub_name}. "
            "Widen the search or add crew time."
        )


def _now() -> datetime:
    return timezone.now()


def lookahead_days() -> int | None:
    raw = fleet_services.setting("dispatch_lookahead_days")
    try:
        return int(pv.validate("dispatch_lookahead_days", raw)) if raw else None
    except ValueError:
        return None


def operating_hours(hub_id: int) -> tuple[dtime, dtime] | None:
    return pv.parse_hours(pv.hub_value("hub_operating_hours", hub_id))


def candidate_starts(
    tz: str, hours: tuple[dtime, dtime], days: int, minutes: int, now: datetime
) -> list[datetime]:
    """On-site starts (UTC), 30-minute steps inside the opening hours of each local day, today first. The
    visit must end by closing time on the same local day. Wall times that do not exist or exist twice in
    ``tz`` (daylight-saving changes) are skipped. Only starts after ``now``."""
    zone = ZoneInfo(tz)
    today: date = now.astimezone(zone).date()
    out: list[datetime] = []
    for d in range(days):
        day = today + timedelta(days=d)
        t = datetime.combine(day, hours[0])
        close = datetime.combine(day, hours[1])
        while t + timedelta(minutes=minutes) <= close:
            try:
                start = p.local_to_utc(t, tz)
            except Invalid:
                t += timedelta(minutes=STEP_MIN)
                continue
            end_local = (start + timedelta(minutes=minutes)).astimezone(zone).replace(tzinfo=None)
            if start > now and end_local <= close:
                out.append(start)
            t += timedelta(minutes=STEP_MIN)
    return out


def _combos(opts: popt.PlanOptions, hub_id: int) -> list[tuple[Any, Any, Any, Any]]:
    """Combinations to ask evaluate, best first: robots not away before robots away (At risk), then the
    deterministic code order; robot i with lead i, the next different second and vehicle i (a short
    diagonal, never the full cross product). People and vehicles are those based at the visit's hub."""
    robots = [o for o in opts.robots if o.status == popt.FREE]
    robots.sort(key=lambda o: (bool(o.at_risk), o.code))
    leads = sorted(
        (o for o in opts.leads if o.status == popt.FREE and o.obj.hub_id == hub_id), key=lambda o: o.code
    )
    seconds = sorted(
        (o for o in opts.seconds if o.status == popt.FREE and o.obj.hub_id == hub_id), key=lambda o: o.code
    )
    vehicles = sorted((o for o in opts.vehicles if o.status == popt.FREE), key=lambda o: o.code)
    if not (robots and leads and seconds and vehicles):
        return []
    out = []
    for i in range(TRIES_PER_TIME):
        robot = robots[min(i, len(robots) - 1)]
        lead = leads[min(i, len(leads) - 1)]
        second = next((s for s in seconds[i:] + seconds[:i] if s.code != lead.code), None)
        vehicle = vehicles[min(i, len(vehicles) - 1)]
        if second is None:
            continue
        combo = (robot, lead, second, vehicle)
        if combo not in out:
            out.append(combo)
    return out


def _why(robot: Any, hub_name: str, plan: p.Plan) -> str:
    """The one line of why: where the robot is (section 12), that the crew and van are free, and the travel
    matrix values used (a home with no zone: its recorded minutes)."""
    where = "Robot away until verified back at the hub" if robot.at_risk else f"Robot at {hub_name}"
    route = plan.travel
    travel = ""
    if route is not None and route.known:
        travel = f"; travel {route.out.minutes} min out, {route.back.minutes} min back"
        if "demo" in (route.out.source, route.back.source):
            travel += " (demo values)"
    return f"{where}; crew and van free{travel}"


def _target(visit_or_request: Visit | Request) -> dict[str, Any]:
    if isinstance(visit_or_request, Request):
        r = visit_or_request
        probe = Visit(site=r.site, hub=r.hub, zone_id=r.site.zone_ref_id)  # never saved
        return {
            "probe": probe,
            "site": r.site,
            "hub": r.hub,
            "minutes": r.minutes,
            "tasks": r.tasks,
            "scopes": r.scopes,
            "t_out": r.travel_out_min,
            "t_back": r.travel_back_min,
        }
    v = visit_or_request
    rev = v.current_revision
    assert rev is not None
    site = v.site
    # the zone a re-plan would use: the home's zone now (plan_visit sets it the same way)
    probe = Visit(pk=v.pk, code=v.code, site=site, hub_id=v.hub_id, zone_id=site.zone_ref_id)
    return {
        "probe": probe,
        "site": site,
        "hub": Hub.objects.get(pk=v.hub_id),
        "minutes": int((rev.on_site_end - rev.on_site_start).total_seconds() // 60),
        "tasks": list(rev.tasks or []),
        "scopes": list(rev.required_consent_scopes or ["presence"]),
        "t_out": rev.travel_out_min,
        "t_back": rev.travel_back_min,
    }


def next_free(
    visit_or_request: Visit | Request,
    *,
    limit: int = 3,
    now: datetime | None = None,
    budget: int = BUDGET,
) -> NextFreeResult:
    """Up to ``limit`` next free slots at the visit's hub. Read only (see the module docstring)."""
    t = _target(visit_or_request)
    hub: Hub = t["hub"]
    probe: Visit = t["probe"]
    now = now or _now()
    out = NextFreeResult([], False, hub_name=hub.name, tz=hub.time_zone)
    days = lookahead_days()
    hours = operating_hours(hub.pk)
    if days is None:
        out.not_set = (
            "The dispatch look-ahead is not set, so the next free time cannot be searched. The deployment "
            "owner sets dispatch_lookahead_days."
        )
        return out
    if hours is None:
        out.not_set = (
            f"The operating hours of {hub.name} are not set, so the next free time cannot be searched. The "
            "deployment owner sets hub_operating_hours for this hub."
        )
        return out
    out.days = days
    starts = candidate_starts(hub.time_zone, hours, days, t["minutes"], now)
    if not starts:
        return out

    # one read of everything the option labels need, for the whole look-ahead
    insp = p._int_setting("pre_visit_inspection_min") or 0
    rr = popt.read_robots()
    span = timedelta(minutes=t["minutes"])
    margin = timedelta(hours=24)  # wider than any travel, loading, inspection and charging time
    world = popt.read_rest(
        rr,
        probe if probe.pk is not None else None,
        busy=(starts[0] - margin, starts[-1] + span + margin),
        quals=(starts[-1] + span + margin, starts[0] - margin),  # valid at some point in the search window
        insp=insp,
    )
    route_now = p.new_route(probe, starts[0], starts[0] + span)
    t_out = route_now.out.minutes or 0 if route_now is not None else t["t_out"] or 0
    t_back = route_now.back.minutes or 0 if route_now is not None else t["t_back"] or 0
    seen_home: list[str] = []
    found: list[Slot] = []
    free_count = 0

    def spend() -> bool:
        if out.evaluations >= budget:
            out.exhausted = True
            return False
        out.evaluations += 1
        return True

    for start in starts:
        if free_count >= limit or out.stopped or out.exhausted:
            break
        end = start + span
        lead_time = t_out + max((v[0] for v in world.loads.values()), default=0) + insp
        if start - timedelta(minutes=lead_time) <= now:
            continue  # too soon: inspection and loading would have to start in the past (not counted)
        out.candidates += 1
        opts = popt.options_at(world, hub.pk, start, end, t_out, t_back)
        combos = _combos(opts, hub.pk)
        if not combos:
            if not spend():
                break
            continue
        slot_found: Slot | None = None
        for robot, lead, second, vehicle in combos:
            if not spend():
                break
            route = p.new_route(probe, start, end)
            plan = p.evaluate(
                site=t["site"],
                start=start,
                end=end,
                travel_out=t["t_out"],
                travel_back=t["t_back"],
                tasks=t["tasks"],
                scopes=t["scopes"],
                robot=robot.obj,
                lead=lead.obj,
                second=second.obj,
                vehicle=vehicle.obj,
                visit=probe,
                travel=route,
                pause_check=False,
            )
            blocked = [b for b in plan.blockers if b["level"] == "blocked"]
            stop = [b for b in blocked if b["code"] in STOPPERS]
            if stop:
                out.stopped = plain_blocker(stop[0])["sentence"]
                break
            for b in plan.blockers:
                if b["code"] in HOME_CODES:
                    sentence = plain_blocker(b)["sentence"]
                    if sentence not in seen_home:
                        seen_home.append(sentence)
            if any(b["code"] not in HOME_CODES for b in blocked) or "timeline_utc" not in plan.snapshot:
                continue
            if datetime.fromisoformat(plan.snapshot["timeline_utc"]["inspection"]) <= now:
                break  # too soon: the pre-visit inspection would have to start in the past
            risky = any(b["level"] == "at_risk" for b in plan.blockers)
            slot_found = Slot(
                start=start,
                end=end,
                status=AT_RISK if risky else FREE,
                robot=robot.code,
                lead=lead.obj,
                second=second.obj,
                vehicle=vehicle.code,
                why=_why(robot, hub.name, plan),
                tz=hub.time_zone,
                site_tz=t["site"].timezone,
                travel_versions=(
                    (route.out.version_id, route.back.version_id) if route is not None else (None, None)
                ),
            )
            break
        if slot_found is not None:
            found.append(slot_found)
            free_count += slot_found.status == FREE
    out.slots = sorted(found, key=lambda s: s.key)[:limit]
    out.home_notes = seen_home
    return out

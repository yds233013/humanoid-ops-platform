"""Review options and reassign (v2 03B-2, screen S2 of docs/14_v2_03b_design_spec.md; contract 13.5 "Reassign
panel").

READ ONLY. Nothing here books, reserves, locks or writes. The panel is advisory:

* "Why this visit needs attention" is the live plan check (``planning.services.live_feasibility``) in plain
words,
  plus the regional pause (``operations.pauses.active_for``).
* The options are the existing planner options (``planning.options.plan_options``), grouped free, then at
risk,
  then not possible (each not-possible option keeps its reason).
* "What we checked" for a proposed change comes from ``planning.services.evaluate`` - the SAME function the
guarded
  plan command (``planning.services.plan_visit``) runs - plus the database hub rule and the two 03B accessors
  (travel time and regional pause).

Saving always goes through the guarded plan command (``/planning/visits/<code>/plan/``), which checks
everything again under its locks, and the database triggers check it once more. Nothing here is permission to
leave.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.http import QueryDict

from fleet.location import robot_location
from operations import pauses
from planning import options as popt
from planning import services as p
from planning import travel, zones
from planning.board import (
    crew_names,
    home_name,
    legacy_travel_text,
    pause_banner,
    person_name,
    strip_status_prefix,
)
from planning.models import Visit, VisitRevision
from planning.plain import plain_blocker, plain_blockers

PASS, RISK, FAIL = "pass", "risk", "fail"
MARK = {PASS: ("✓", "Passed"), RISK: ("▲", "At risk"), FAIL: ("✕", "Not possible")}
FREE_SHOWN = 5  # free options listed before "Show more"

FEASIBLE_TEXT = "Planning feasible: not cleared to depart"
REVIEW_TEXT = "Requires review: not cleared to depart"

_LOCATION_PREFIXES = ("In transfer", "Away on run", "Not at a hub", "At hub ", "Location unknown")
_MAINTENANCE_PREFIXES = ("Maintenance", "No approved maintenance rule")
_HOLD_WORDS = ("hold",)


@dataclass
class Check:
    key: str
    title: str
    result: str  # pass / risk / fail
    text: str

    @property
    def mark(self) -> str:
        return MARK[self.result][0]

    @property
    def words(self) -> str:
        return MARK[self.result][1]


@dataclass
class Choice:
    code: str
    line: str
    reason: str = ""
    current: bool = False
    picked: bool = False
    risk: int = 0  # 0 = free; higher = more or worse risks (at the hub beats away; D-B6)


@dataclass
class Group:
    kind: str  # robot / lead / second / vehicle
    title: str
    free: list[Choice] = field(default_factory=list)
    at_risk: list[Choice] = field(default_factory=list)
    not_possible: list[Choice] = field(default_factory=list)
    elsewhere: int = 0  # options of other hubs: not listed (they can never be picked for this visit)

    @property
    def free_shown(self) -> list[Choice]:
        return self.free[:FREE_SHOWN] + [c for c in self.free[FREE_SHOWN:] if c.picked or c.current]

    @property
    def free_more(self) -> list[Choice]:
        return [c for c in self.free[FREE_SHOWN:] if not (c.picked or c.current)]

    @property
    def risk_shown(self) -> list[Choice]:
        room = max(0, FREE_SHOWN - len(self.free_shown)) or 2
        return self.at_risk[:room] + [c for c in self.at_risk[room:] if c.picked or c.current]

    @property
    def risk_more(self) -> list[Choice]:
        shown = {c.code for c in self.risk_shown}
        return [c for c in self.at_risk if c.code not in shown]


@dataclass
class Diff:
    title: str
    before: str
    after: str


@dataclass
class Review:
    why: list[dict[str, str]]
    groups: list[Group]
    picks: dict[str, str]
    current: dict[str, str]
    changes: list[Diff]
    checks: list[Check]
    verdict: str  # feasible / review / blocked / "" (nothing proposed)
    verdict_text: str
    verdict_reasons: list[str]
    pause: Any = None
    hub_name: str = ""
    new_start: str = ""
    new_end: str = ""
    blocker: dict[str, str] | None = None  # D-B8: a blocker no robot, crew, vehicle or time can fix
    auto: bool = False  # the proposal is the preselected best option (D-B6), not the viewer's own pick
    keep_url: str = ""  # "Keep the current plan"

    @property
    def proposed(self) -> bool:
        return bool(self.changes)

    @property
    def nothing_fits(self) -> bool:
        """Spec S2 "No option works": some part of the plan (robot, lead, second or vehicle) has no free or
        at-risk option for this time. The page then offers the next free times (S3)."""
        return self.blocker is None and any(not (g.free or g.at_risk) for g in self.groups)


# ------------------------------------------------------------------ options
RISK_WEIGHT = {"away": 2, "needs charge": 2, "charge forecast unknown": 1}
ELSEWHERE = ("at another hub", "away on another hub's run", "on its way to another hub")


def _robot_line(o: popt.Option, hub_name: str, charge_on: bool) -> tuple[str, str, int]:
    """("Robot NB-031 · at Oakland hub · charge enough", group, risk score) for one planner option."""
    parts = [f"Robot {o.code}"]
    base, _, extra = o.note.partition(" · ")
    risk = 0
    if o.status == popt.FREE:
        parts.append("away until verified back at the hub" if o.at_risk else f"at {hub_name}")
        risk += RISK_WEIGHT["away"] if o.at_risk else 0
        if extra:
            parts.append(extra)
            risk += RISK_WEIGHT.get(extra, 1)
        elif charge_on:
            parts.append("charge enough")
        group = "at_risk" if risk else "free"
    else:
        parts.append(base)
        group = "not_possible"
    return " · ".join(parts), group, risk


def _person_line(o: popt.Option) -> tuple[str, str]:
    name = person_name(o.obj) or o.code
    group = "free" if o.status == popt.FREE else "not_possible"
    return f"{name} · {o.code} · {o.note}", group


def _vehicle_line(o: popt.Option) -> tuple[str, str]:
    group = "free" if o.status == popt.FREE else "not_possible"
    return f"Vehicle {o.code} · {o.note}", group


def _groups(
    opts: popt.PlanOptions, picks: dict[str, str], current: dict[str, str], hub_name: str
) -> list[Group]:
    out = []
    for kind, title, items in (
        ("robot", "Robot", opts.robots),
        ("lead", "Lead", opts.leads),
        ("second", "Second", opts.seconds),
        ("vehicle", "Vehicle", opts.vehicles),
    ):
        g = Group(kind, title)
        for o in items:
            risk = 0
            if kind == "robot":
                line, bucket, risk = _robot_line(o, hub_name, opts.charge_on)
            elif kind == "vehicle":
                line, bucket = _vehicle_line(o)
            else:
                line, bucket = _person_line(o)
            if bucket == "not_possible" and o.code != current[kind] and o.note.startswith(ELSEWHERE):
                g.elsewhere += 1  # another hub's robot or vehicle: never a choice here, not listed
                continue
            c = Choice(
                o.code, line, o.note if bucket == "not_possible" else "", o.code == current[kind], risk=risk
            )
            c.picked = o.code == picks.get(kind)
            getattr(g, bucket).append(c)
        g.at_risk.sort(
            key=lambda c: (c.risk, c.code)
        )  # fewer and less severe risks first: at the hub beats away
        out.append(g)
    return out


def _best(g: Group) -> str:
    return g.free[0].code if g.free else (g.at_risk[0].code if g.at_risk else "")


def _rank(g: Group, code: str) -> int | None:
    """0 for a free option, its risk score when at risk, None when not possible (or none chosen)."""
    if not code:
        return None
    for c in g.free:
        if c.code == code:
            return 0
    for c in g.at_risk:
        if c.code == code:
            return c.risk
    return None


UNFIXABLE = {"TRAVEL_UNKNOWN", "ZONE_RETIRED"}


def _blocker(visit: Visit, live: p.Plan, hub: str) -> dict[str, str] | None:
    """D-B8: ONE message for a blocker that no robot, crew, vehicle or time can fix: what, why, who can fix
    it."""
    codes = {b.get("code") for b in live.blockers if b.get("level") == "blocked"}
    zone = zones.zone_of(visit)
    place = zone.name if zone is not None else "this home's zone"
    if "ZONE_RETIRED" in codes:
        return {
            "code": "ZONE_RETIRED",
            "what": f"{place} is a retired zone, so no new visit can use it",
            "why": "A retired zone keeps its history but is never offered again, whatever robot, crew or "
            "time is chosen",
            "who": "The ops lead can move this home to an active zone on the customer page",
            "url": f"/planning/sites/{visit.site.code}/",
            "link": "Open the customer page",
            "permission": "planning.manage_sites",
        }
    if "TRAVEL_UNKNOWN" in codes:
        origin = origin_zone(visit)
        start = origin.name if origin is not None else hub
        return {
            "code": "TRAVEL_UNKNOWN",
            "what": f"Travel time unknown between {start} and {place}",
            "why": "Without an approved travel time nothing is estimated, so no robot, crew, vehicle or "
            "time can make this visit plannable",
            "who": "An admin or the ops lead can set it in Settings › Travel times",
            "url": "/settings/travel-times/",
            "link": "Set the travel time",
            "permission": "planning.set_travel_time",
        }
    return None


# ------------------------------------------------------------------ the checks
def _hub_name(visit: Visit) -> str:
    hub = getattr(visit, "hub", None)
    return f"{hub.name}" if hub is not None else "this visit's hub"


def _robot_reasons(blockers: list[dict[str, str]]) -> list[str]:
    out: list[str] = []
    for b in blockers:
        if b.get("code") == "ROBOT_NOT_ELIGIBLE":
            _robot, _, rest = b.get("message", "").partition(":")
            out += [x.strip() for x in rest.split("; ") if x.strip()]
    return out


def _sentences(blockers: list[dict[str, str]], codes: set[str]) -> list[str]:
    return [plain_blocker(b)["sentence"] for b in blockers if b.get("code") in codes]


def _route_text(rt: travel.Route) -> str:
    """ "25 min out, 25 min back · version 2 (demo value)": the pair's own version number, not a row id
    (D-B3)."""
    from planning.models import TravelTime  # noqa: PLC0415

    ids = [x for x in (rt.out.version_id, rt.back.version_id) if x is not None]
    versions = dict(TravelTime.objects.filter(pk__in=ids).values_list("pk", "version"))
    v_out, v_back = versions.get(rt.out.version_id), versions.get(rt.back.version_id)
    sources = {travel.SOURCE_WORDS.get(x.source, x.source) for x in (rt.out, rt.back)}
    source = " and ".join(sorted(sources))
    legs = f"{rt.out.minutes} min out, {rt.back.minutes} min back"
    if v_out == v_back:
        return f"{legs} · version {v_out} ({source}), from the travel matrix."
    return f"{legs} · versions {v_out} out and {v_back} back ({source}), from the travel matrix."


def origin_zone(visit: Visit) -> Any:
    """Where the journey to the home starts: the hub home zone (03B-1, ``platform_core_hub.home_zone_id``)."""
    return zones.hub_origin_zone(visit.hub_id)


def checks_for(
    visit: Visit,
    plan: p.Plan,
    *,
    robot: Any,
    lead: Any,
    second: Any,
    vehicle: Any,
    start: datetime,
    travel_out: int | None,
    travel_back: int | None,
) -> list[Check]:
    """The "What we checked" lines for one proposal (spec S2 item 3), from ``evaluate``'s own result."""
    hub = _hub_name(visit)
    b = [x for x in plan.blockers]
    codes = {x.get("code") for x in b}
    robot_reasons = _robot_reasons(b)
    out: list[Check] = []

    # robot location (section 12 future-planning predicate, as evaluate applies it)
    loc_fail = _sentences(b, {"ROBOT_AT_ANOTHER_HUB", "ROBOT_AWAY_OVERLAP"}) + [
        f"Robot {robot.code}: {plain_reason}"
        for plain_reason in (r for r in robot_reasons if r.startswith(_LOCATION_PREFIXES))
    ]
    if robot is None:
        out.append(Check("location", "Robot location", FAIL, "No robot is chosen."))
    elif loc_fail:
        out.append(Check("location", "Robot location", FAIL, " ".join(loc_fail)))
    elif "ROBOT_AWAY" in codes:
        out.append(
            Check(
                "location",
                "Robot location",
                RISK,
                f"Robot {robot.code} is away on a run of {hub}. It can be planned, but it cannot be "
                "authorized to leave until it is verified back at the hub.",
            )
        )
    else:
        where = robot_location(robot.pk)
        text = (
            f"Robot {robot.code} is at {hub}."
            if where.kind == "hub"
            else f"Robot {robot.code}: {where.label}."
        )
        out.append(Check("location", "Robot location", PASS, text))

    # time overlap (reservations of other visits; the exclusion constraint is the final guard)
    clash = _sentences(b, {"CONFLICT"})
    out.append(
        Check("overlap", "Time overlap", FAIL, " ".join(clash))
        if clash
        else Check(
            "overlap",
            "Time overlap",
            PASS,
            "No other booking overlaps this visit's time, including inspection, travel, loading and "
            "charging.",
        )
    )

    # crew qualification
    crew = _sentences(b, {"QUALIFICATION", "CREW_INCOMPLETE", "CREW_SAME_PERSON"})
    out.append(
        Check("crew", "Crew qualification", FAIL, " ".join(crew))
        if crew
        else Check(
            "crew",
            "Crew qualification",
            PASS,
            f"{person_name(lead)} (lead) and {person_name(second)} (second) are qualified for the whole "
            "visit.",
        )
    )

    # vehicle (evaluate's checks, plus the database hub rule planning_visit_hub_guard)
    veh = _sentences(b, {"VEHICLE_MISSING", "VEHICLE_CAPACITY_UNKNOWN", "VEHICLE_RESTRAINT_UNKNOWN"})
    if vehicle is not None and vehicle.hub_id != visit.hub_id:
        veh.append(
            f"{vehicle.code} belongs to another hub, so the plan would be refused. Pick a vehicle at {hub}."
        )
    out.append(
        Check("vehicle", "Vehicle", FAIL, " ".join(veh))
        if veh
        else Check(
            "vehicle",
            "Vehicle",
            PASS,
            f"{vehicle.code} is at {hub} with a recorded capacity and restraint kit.",
        )
    )

    # maintenance (v2 02C, only while the gate is active) and holds
    mnt = [r for r in robot_reasons if r.startswith(_MAINTENANCE_PREFIXES)]
    out.append(
        Check("maintenance", "Maintenance", FAIL, f"Robot {robot.code}: {'; '.join(mnt)}.")
        if mnt and robot is not None
        else Check("maintenance", "Maintenance", PASS, "No maintenance is due that blocks planning.")
    )
    holds = [r for r in robot_reasons if any(w in r.lower() for w in _HOLD_WORDS)]
    out.append(
        Check("holds", "Holds", FAIL, f"Robot {robot.code} is on hold: {'; '.join(holds)}.")
        if holds and robot is not None
        else Check("holds", "Holds", PASS, "No hold on the robot or its configuration.")
    )

    # travel time (03B-1 matrix accessor; a visit planned before 03B keeps its recorded minutes, 10.H)
    zone = zones.zone_of(visit)
    recorded = (
        f"{travel_out} min out and {travel_back} min back"
        if travel_out is not None and travel_back is not None
        else ""
    )
    origin = origin_zone(visit)
    rt = travel.route(visit.hub_id, zone, start, start) if zone is not None and origin is not None else None
    if not recorded and zone is None:
        out.append(
            Check(
                "travel",
                "Travel time",
                FAIL,
                "Travel time unknown, and none is recorded on the visit. Enter it.",
            )
        )
    elif rt is not None and rt.known:
        out.append(Check("travel", "Travel time", PASS, _route_text(rt)))
    elif zone is None:
        # a home without a zone keeps the minutes recorded on the visit, as before 03B (03B-1 rule); the EM
        # (03B closure) wants it said, in the at-risk style
        out.append(Check("travel", "Travel time", RISK, legacy_travel_text(travel_out, travel_back)))
    else:
        # a zoned home needs an approved matrix value; a missing pair blocks new dispatch (10.H, 13.4)
        why = (
            f"no starting zone is recorded for {hub}"
            if origin is None
            else f"no approved value from {origin.name} to {zone.name}"
        )
        out.append(
            Check(
                "travel",
                "Travel time",
                FAIL,
                f"Travel time unknown: {why}. Ask an ops lead or admin to set it on the Travel times page.",
            )
        )

    # regional pause (03B-1 accessor; the authorization and departure gates enforce it)
    pause = pauses.active_for(visit.hub_id, zone.id if zone is not None else None)
    out.append(
        Check("pause", "Regional pause", RISK, pause_banner(hub, pause))
        if pause is not None
        else Check("pause", "Regional pause", PASS, f"No departure pause at {hub}.")
    )

    # everything else the plan check looks at (home, consent, set-up times, other robot checks)
    listed = {
        "ROBOT_AT_ANOTHER_HUB",
        "ROBOT_AWAY_OVERLAP",
        "ROBOT_AWAY",
        "ROBOT_MISSING",
        "ROBOT_NOT_ELIGIBLE",
        "CONFLICT",
        "QUALIFICATION",
        "CREW_INCOMPLETE",
        "CREW_SAME_PERSON",
        "VEHICLE_MISSING",
        "VEHICLE_CAPACITY_UNKNOWN",
        "VEHICLE_RESTRAINT_UNKNOWN",
    }
    other = [x for x in b if x.get("code") not in listed]
    other_robot = [
        r
        for r in robot_reasons
        if not r.startswith(_LOCATION_PREFIXES + _MAINTENANCE_PREFIXES)
        and not any(w in r.lower() for w in _HOLD_WORDS)
    ]
    texts = [pb["sentence"] for pb in plain_blockers(other)]
    if other_robot and robot is not None:
        texts.append(f"Robot {robot.code} is not ready for planning: {'; '.join(other_robot)}.")
    worst = (
        FAIL if any(x.get("level") == "blocked" for x in other) or other_robot else (RISK if other else PASS)
    )
    out.append(
        Check("other", "Home, consent and set-up", worst, " ".join(texts))
        if texts
        else Check(
            "other",
            "Home, consent and set-up",
            PASS,
            "Home assessment, consent and set-up times are in order.",
        )
    )
    return out


# ------------------------------------------------------------------ the panel
def _current(rev: VisitRevision) -> dict[str, str]:
    return {
        "robot": rev.robot.code if rev.robot else "",
        "lead": rev.lead.code if rev.lead else "",
        "second": rev.second.code if rev.second else "",
        "vehicle": rev.vehicle.code if rev.vehicle else "",
    }


def _parse_local(raw: str, tzname: str) -> datetime | None:
    try:
        return p.local_to_utc(datetime.fromisoformat(raw).replace(tzinfo=None), tzname) if raw else None
    except (ValueError, p.Invalid):
        return None


def _planning_risks(visit: Visit) -> list[dict[str, str]]:
    """The at-risk facts the board adds to the plan check (charge forecast, v2 02B; stock, v2 02D), so the
    panel and the board give the same reasons. Planning information only: departure keeps its own checks."""
    from hub import charging, stock  # noqa: PLC0415

    out: list[dict[str, str]] = []
    robot = visit.current_revision.robot if visit.current_revision else None
    if robot is not None and charging.forecast_configured():
        f = charging.visit_forecasts([visit.pk]).get(visit.pk)
        if f is not None and f.state != charging.ENOUGH:
            words = "needs charge before it leaves" if f.state == charging.NEEDS else "has no charge forecast"
            out.append(
                {
                    "level": "blocked" if f.cannot_in_time else "at_risk",
                    "sentence": f"Robot {robot.code} {words}: {f.reason}.",
                    "action": "Departure still needs the battery check.",
                    "code": "CHARGE",
                }
            )
    for n in stock.visit_shortages(hub_ids=[visit.hub_id]).get(visit.pk, []):
        out.append({"level": "at_risk", "sentence": f"{n.reason}.", "action": "", "code": "STOCK"})
    return out


def verdict(feasibility: str, blockers: list[dict[str, str]]) -> tuple[str, list[str]]:
    """The result line after (or before) a save: never anything that reads as permission to leave."""
    if feasibility == "feasible":
        risks = [
            pb["sentence"] for pb in plain_blockers([b for b in blockers if b.get("level") == "at_risk"])
        ]
        return FEASIBLE_TEXT, risks
    if feasibility == "review":
        return REVIEW_TEXT, [
            pb["sentence"] for pb in plain_blockers([b for b in blockers if b.get("level") != "blocked"])
        ]
    return "Blocked", [
        pb["sentence"] for pb in plain_blockers([b for b in blockers if b.get("level") == "blocked"])
    ]


def build_review(
    visit: Visit, query: QueryDict, *, live: p.Plan | None = None, opts: popt.PlanOptions | None = None
) -> Review:
    """``live`` and ``opts``: the visit page's own live plan check and planner options, reused (read once)."""
    rev = visit.current_revision
    assert rev is not None
    hub = _hub_name(visit)
    live = live if live is not None else p.live_feasibility(visit)
    zone = zones.zone_of(visit)
    pause = pauses.active_for(visit.hub_id, zone.id if zone is not None else None)
    why = [
        {**pb, "sentence": strip_status_prefix(pb["sentence"])} for pb in plain_blockers(live.blockers)
    ] + _planning_risks(visit)
    blocker = _blocker(visit, live, hub)
    if blocker is not None:  # the one message replaces its own line in the list (D-B8)
        why = [x for x in why if x.get("code") != blocker["code"]]
    if pause is not None:
        why.insert(
            0, {"level": "paused", "sentence": pause_banner(hub, pause), "action": "", "code": "PAUSE"}
        )
    opts = opts if opts is not None else popt.plan_options(visit)
    current = _current(rev)

    by_code = {
        "robot": {o.code: o.obj for o in opts.robots},
        "lead": {o.code: o.obj for o in opts.leads},
        "second": {o.code: o.obj for o in opts.seconds},
        "vehicle": {o.code: o.obj for o in opts.vehicles},
    }
    asked = any(k in query for k in ("robot", "lead", "second", "vehicle", "start"))
    groups = _groups(opts, {}, current, hub)
    picks: dict[str, str] = {}
    for g in groups:
        raw = (query.get(g.kind) or "").strip() if asked else ""
        if raw and raw in by_code[g.kind]:
            picks[g.kind] = raw
        elif asked:
            picks[g.kind] = current[g.kind]
        else:
            # no choice made yet (D-B6): keep the current option unless a strictly better one exists (free
            # beats at risk; fewer and less severe risks win), so resolving an attention item is card ->
            # Review options -> Save plan
            best = _best(g)
            mine, theirs = _rank(g, current[g.kind]), _rank(g, best)
            keep = mine is not None and (theirs is None or mine <= theirs)
            picks[g.kind] = current[g.kind] if keep or not best or blocker is not None else best
    if picks.get("second") and picks.get("second") == picks.get("lead") and not asked:
        alt = next((c.code for c in groups[2].free if c.code != picks["lead"]), "")
        picks["second"] = alt or picks["second"]
    groups = _groups(opts, picks, current, hub)

    new_start_raw = (query.get("start") or "").strip()
    new_end_raw = (query.get("end") or "").strip()
    new_start = _parse_local(new_start_raw, visit.site.timezone)
    new_end = _parse_local(new_end_raw, visit.site.timezone)
    if not (new_start and new_end and new_end > new_start):
        new_start = new_end = None

    changes: list[Diff] = []
    for g in groups:
        if picks[g.kind] != current[g.kind]:
            changes.append(
                Diff(
                    g.title, _label(g.kind, current[g.kind], by_code), _label(g.kind, picks[g.kind], by_code)
                )
            )
    if new_start is not None and new_end is not None:
        changes.append(Diff("On site", _span(rev.on_site_start, rev.on_site_end), _span(new_start, new_end)))

    checks: list[Check] = []
    v_text, v_key = "", ""
    v_reasons: list[str] = []
    auto = bool(changes) and not asked
    if changes:
        robot, lead, second, vehicle = (
            by_code[k].get(picks[k]) for k in ("robot", "lead", "second", "vehicle")
        )
        start, end = new_start or rev.on_site_start, new_end or rev.on_site_end
        plan = p.evaluate(
            site=visit.site,
            start=start,
            end=end,
            travel_out=rev.travel_out_min,
            travel_back=rev.travel_back_min,
            tasks=rev.tasks,
            scopes=rev.required_consent_scopes,
            robot=robot,
            lead=lead,
            second=second,
            vehicle=vehicle,
            visit=visit,
        )
        checks = checks_for(
            visit,
            plan,
            robot=robot,
            lead=lead,
            second=second,
            vehicle=vehicle,
            start=start,
            travel_out=rev.travel_out_min,
            travel_back=rev.travel_back_min,
        )
        v_key = plan.feasibility
        if vehicle is not None and vehicle.hub_id != visit.hub_id:
            v_key = "blocked"
        v_text, v_reasons = verdict(v_key, plan.blockers)
        if v_key == "blocked":
            v_reasons = [c.text for c in checks if c.result == FAIL]
        if auto and v_key == "blocked":
            # the preselection would not help: fall back to the current plan, with nothing proposed
            picks = dict(current)
            groups = _groups(opts, picks, current, hub)
            changes, checks, v_key, v_text, v_reasons, auto = [], [], "", "", [], False
    return Review(
        why=why,
        groups=groups,
        picks=picks,
        current=current,
        changes=changes,
        checks=checks,
        verdict=v_key,
        verdict_text=v_text,
        verdict_reasons=v_reasons,
        pause=pause,
        hub_name=hub,
        new_start=new_start_raw if new_start else "",
        new_end=new_end_raw if new_end else "",
        blocker=blocker,
        auto=auto,
        keep_url="?review=1&" + "&".join(f"{k}={v}" for k, v in current.items()),
    )


def _label(kind: str, code: str, by_code: dict[str, dict[str, Any]]) -> str:
    if not code:
        return "None"
    obj = by_code[kind].get(code)
    if kind in ("lead", "second") and obj is not None:
        return f"{person_name(obj)} ({code})"
    return f"Robot {code}" if kind == "robot" else code


def _span(s: datetime, e: datetime) -> str:
    from platform_core.templatetags.ui import local_dt, local_time  # noqa: PLC0415

    return f"{local_dt(s)}–{local_time(e)}"


def what_changed(visit: Visit, revision_no: int) -> dict[str, Any] | None:
    """After a save: before -> after per field (spec S2 item 4), from the two stored revisions, and the result
    the guarded plan command recorded. None when ``revision_no`` is not the visit's current revision."""
    cur = visit.current_revision
    if cur is None or cur.revision_no != revision_no or revision_no < 2:
        return None
    prev = (
        VisitRevision.objects.select_related("robot", "lead__user", "second__user", "vehicle")
        .filter(visit=visit, revision_no=revision_no - 1)
        .first()
    )
    if prev is None:
        return None
    rows: list[Diff] = []
    for title, a, b in (
        (
            "Robot",
            f"Robot {prev.robot.code}" if prev.robot else "None",
            f"Robot {cur.robot.code}" if cur.robot else "None",
        ),
        ("Crew", crew_names(prev.lead, prev.second) or "None", crew_names(cur.lead, cur.second) or "None"),
        (
            "Vehicle",
            prev.vehicle.code if prev.vehicle else "None",
            cur.vehicle.code if cur.vehicle else "None",
        ),
        ("On site", _span(prev.on_site_start, prev.on_site_end), _span(cur.on_site_start, cur.on_site_end)),
    ):
        if a != b:
            rows.append(Diff(title, a, b))
    text, reasons = verdict(cur.feasibility, list(cur.blockers or []))
    return {
        "revision": cur.revision_no,
        "rows": rows,
        "result": cur.feasibility,
        "text": text,
        "reasons": reasons,
        "home": home_name(visit.site),
    }

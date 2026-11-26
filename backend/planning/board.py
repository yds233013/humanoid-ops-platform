"""The regional dispatch board (v2 03B-2, screen S1 of docs/14_v2_03b_design_spec.md; contract 13.4 item 5,
13.5).

READ ONLY. Built inside ONE read snapshot (the view opens ``operations.snapshot.read_snapshot()``), from
existing read models, with a FIXED number of statements whatever the number of hubs or visits:

    visits of the day: home, revision, robot, crew, vehicle   1 statement (one joined ORM read)
    planning eligibility + future-planning location           1 statement (operations_robot_eligibility and
                                                              fleet_planning_location, section 12)
    robots per hub and status (available / away / held)       1 statement (operations_robot_status)
    charge forecast of the visits' robots (v2 02B)            1 statement, only when it is configured
    stock shortages (v2 02D)                                  1 statement
    regional pauses (operations.pauses.board_summary)         1 statement (03B-1)

Status words are exactly "Available for planning", "At risk", "Blocked", "Paused" (contract 13.4 item 5), each
with a shape, and a departed visit is a fact ("Departed"). None of them is permission to leave: planning
feasibility is never departure (spec principle 4). The status combines the plan check recorded when the plan
was last saved with what has changed since in the set-based facts above; the visit page re-runs the full plan
check for one visit.

Needs attention is ordered blocked, then paused, then at risk; then by how close the departure is; then by
visit code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from django.db import connection
from django.db.models import Exists, OuterRef

from accounts.roles import PERMISSIONS
from fleet.location import COLUMNS as LOCATION_COLUMNS
from fleet.location import Location
from fleet.location import from_row as location_from_row
from operations import pauses
from planning import zones
from planning.models import Visit
from planning.plain import plain_blocker, robot_reason, role_words
from platform_core.templatetags.ui import local_time, workspace_zone

ATTENTION_LIMIT = 5

# status key -> (words, shape, tone). Words are contract 13.4 item 5; shapes are spec principle 5.
STATUS: dict[str, tuple[str, str, str]] = {
    "blocked": ("Blocked", "■", "bad"),
    "paused": ("Paused", "◆", "warn"),
    "at_risk": ("At risk", "▲", "warn"),
    "available": ("Available for planning", "●", "info"),
    "departed": ("Departed", "○", "neutral"),
}
SEVERITY = {"blocked": 0, "paused": 1, "at_risk": 2, "available": 3, "departed": 4}
ATTENTION = ("blocked", "paused", "at_risk")

# Robot location / eligibility codes the board re-reads live (the stored ones may be out of date).
_LIVE_ROBOT_CODES = {"ROBOT_NOT_ELIGIBLE", "ROBOT_AT_ANOTHER_HUB", "ROBOT_AWAY_OVERLAP", "ROBOT_AWAY"}

ROBOT_SQL = (
    "SELECT e.robot_id, e.eligible, e.reasons, "
    f"{LOCATION_COLUMNS} FROM operations_robot_eligibility e "
    "CROSS JOIN LATERAL fleet_planning_location(e.robot_id) l "
    "WHERE e.robot_id = ANY(%s)"
)
# operations_robot_status rule -> header group (operations.today._RULE_GROUP): 8 at the hub and eligible,
# 3 deployed (out on a run), 5 held.
HUB_ROBOTS_SQL = (
    "SELECT r.hub_id, s.status_rule, count(*) FROM operations_robot_status s "
    "JOIN fleet_robot r ON r.id = s.robot_id GROUP BY r.hub_id, s.status_rule"
)
_RULE_HEADER = {8: "available", 3: "away", 5: "held"}


@dataclass
class Reason:
    level: str  # blocked / paused / at_risk
    text: str
    code: str = ""


@dataclass
class Row:
    visit: Visit
    code: str
    hub_id: int
    hub_name: str
    hub_code: str
    home: str  # "Piedmont home"
    home_code: str  # "H-121"
    start: datetime
    end: datetime
    depart: datetime
    crew: str  # "Jordan Lee and Amara Okafor", or "" when not chosen
    robot: str  # "NB-024" or ""
    vehicle: str
    zone: str  # zone name, or "" when unknown
    status: str
    reasons: list[Reason] = field(default_factory=list)
    crew_key: tuple[int | None, int | None] = (None, None)  # (lead id, second id): crews on shift
    legacy: str = ""  # EM 03B closure (A): "Legacy travel estimate: ..." for a visit whose home has no zone

    @property
    def words(self) -> str:
        return STATUS[self.status][0]

    @property
    def shape(self) -> str:
        return STATUS[self.status][1]

    @property
    def tone(self) -> str:
        return STATUS[self.status][2]

    @property
    def reason(self) -> str:
        """The one plain reason a card shows (the most severe first)."""
        return self.reasons[0].text if self.reasons else ""

    @property
    def attention(self) -> bool:
        return self.status in ATTENTION


@dataclass
class HubSection:
    hub_id: int
    code: str
    name: str
    area: str
    robots: dict[str, int]
    crews: int
    pauses: list[Any]
    rows: list[Row]
    zones: list[str]

    @property
    def attention_rows(self) -> list[Row]:
        return [r for r in self.rows if r.attention]

    @property
    def pause_text(self) -> str:
        return pause_banner(self.name, self.pauses[0]) if self.pauses else ""


@dataclass
class Board:
    at: datetime
    day: date
    day_key: str  # "today", "tomorrow" or the ISO date
    lo: datetime
    hi: datetime
    rows: list[Row]
    hubs: list[HubSection]
    attention: list[Row]
    attention_total: int
    can_plan: bool
    who_plan: str
    charge_off: bool = False

    @property
    def visits(self) -> int:
        return len(self.rows)

    @property
    def hubs_paused(self) -> int:
        return sum(1 for h in self.hubs if h.pauses)


def legacy_travel_text(out_min: int | None, back_min: int | None) -> str:
    """EM 03B closure (A): a home without a zone keeps the travel minutes recorded on its visit, and every
    screen that shows the travel says so, in the at-risk style (words plus shape)."""
    return (
        f"Legacy travel estimate: {out_min if out_min is not None else 'unknown'} min out, "
        f"{back_min if back_min is not None else 'unknown'} min back (this home has no zone yet)"
    )


def pause_banner(hub_name: str, pause: Any) -> str:
    """Spec S1 "Paused hub": what is paused since when, why, and what is still possible."""
    from platform_core.templatetags.ui import local_dt  # noqa: PLC0415

    since = local_dt(getattr(pause, "since", None))
    zone_name = getattr(pause, "zone_name", None)
    what = f"Departures from {hub_name}" + (
        f" (zone {zone_name})" if zone_name else (" (one zone)" if getattr(pause, "zone_id", None) else "")
    )
    reason = str(getattr(pause, "reason", "") or "").strip().rstrip(".")
    return (
        f"{what} are paused{f' since {since}' if since else ''}{f': {reason}' if reason else ''}. "
        "Visits can still be planned; none can be authorized to leave."
    )


def _pause_covers(p: Any, zone_id: int | None) -> bool:
    """A hub pause covers every visit; a zone pause covers its zone and, fail closed, an unknown zone (13.4
    item 2)."""
    pz = getattr(p, "zone_id", None)
    return pz is None or zone_id is None or pz == zone_id


def day_bounds(day: date) -> tuple[datetime, datetime]:
    zone = workspace_zone()
    lo = datetime.combine(day, time(0), tzinfo=zone)
    return lo, datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone)


def parse_day(raw: str | None, at: datetime) -> tuple[date, str]:
    today = at.astimezone(workspace_zone()).date()
    value = (raw or "").strip().lower()
    if value in ("", "today"):
        return today, "today"
    if value == "tomorrow":
        return today + timedelta(days=1), "tomorrow"
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return today, "today"
    if d == today:
        return d, "today"
    if d == today + timedelta(days=1):
        return d, "tomorrow"
    return d, d.isoformat()


def person_name(person: Any) -> str:
    if person is None:
        return ""
    user = person.user
    return (user.get_full_name() or user.get_username()) if user is not None else person.code


def crew_names(lead: Any, second: Any) -> str:
    names = [n for n in (person_name(lead), person_name(second)) if n]
    return " and ".join(names)


def home_name(site: Any) -> str:
    """Names first (spec principle 2): the product stores no customer name, so a home is named by its area."""
    area = str(getattr(site, "zone", "") or "").strip()
    return f"{area} home" if area else "Home"


def _robot_facts(robot_ids: list[int]) -> dict[int, tuple[bool, list[str], Location]]:
    with connection.cursor() as cur:
        cur.execute(ROBOT_SQL, [sorted(set(robot_ids))])
        return {r[0]: (bool(r[1]), list(r[2] or []), location_from_row(r[3:])) for r in cur.fetchall()}


def _hub_robots() -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    with connection.cursor() as cur:
        cur.execute(HUB_ROBOTS_SQL)
        for hub_id, rule, n in cur.fetchall():
            group = _RULE_HEADER.get(int(rule))
            if group:
                counts = out.setdefault(hub_id, {"available": 0, "away": 0, "held": 0})
                counts[group] += int(n)
    return out


def _robot_reasons(
    row: Row, eligible: bool, reasons: list[str], where: Location, window: tuple[datetime, datetime]
) -> list[Reason]:
    out: list[Reason] = []
    name = f"Robot {row.robot}"
    if not eligible:
        shorts = list(dict.fromkeys(robot_reason(x).short for x in reasons)) or ["not ready"]
        out.append(
            Reason("blocked", f"{name} is not ready for planning: {', '.join(shorts)}.", "ROBOT_NOT_ELIGIBLE")
        )
        return out
    problem = where.planning_problem_for(row.hub_id, *window)
    if problem is not None:
        if where.kind == "hub":
            out.append(
                Reason("blocked", f"{name} is at another hub, not at {row.hub_name}.", "ROBOT_AT_ANOTHER_HUB")
            )
        else:
            out.append(
                Reason("blocked", f"{name} is away on a run that overlaps this visit.", "ROBOT_AWAY_OVERLAP")
            )
    elif where.away:
        back = f" by {local_time(where.away_until)}" if where.away_until is not None else ""
        out.append(
            Reason(
                "at_risk",
                f"{name} is still on an earlier visit; expected back at {row.hub_name}{back}.",
                "ROBOT_AWAY",
            )
        )
    return out


def _stored_reasons(blockers: list[dict[str, str]]) -> list[Reason]:
    out: list[Reason] = []
    for b in blockers:
        if b.get("code") in _LIVE_ROBOT_CODES:
            continue
        pb = plain_blocker(b)
        level = "blocked" if pb["level"] == "blocked" else "at_risk"
        out.append(Reason(level, pb["sentence"], pb["code"]))
    return out


def _status(row: Row, departed: bool) -> str:
    if departed:
        return "departed"
    levels = {r.level for r in row.reasons}
    for key in ("blocked", "paused", "at_risk"):
        if key in levels:
            return key
    return "available"


def build_board(at: datetime, scope: Any, day: date, day_key: str) -> Board:
    from hub import charging, stock  # noqa: PLC0415
    from readiness.models import DepartureEvent  # noqa: PLC0415 - readiness depends on planning

    lo, hi = day_bounds(day)
    visible = [h for h in scope.hubs if scope.allows(h.pk)]
    hub_by_id = {h.pk: h for h in visible}
    qs = (
        scope.filter(Visit.objects)
        .exclude(status="cancelled")
        .filter(current_revision__on_site_start__gte=lo, current_revision__on_site_start__lt=hi)
        .select_related(
            "zone",
            "site",
            "current_revision",
            "current_revision__robot",
            "current_revision__lead__user",
            "current_revision__second__user",
            "current_revision__vehicle",
        )
        .annotate(departed=Exists(DepartureEvent.objects.filter(visit_id=OuterRef("pk"))))
        .order_by("current_revision__on_site_start", "code")
    )
    visits = [v for v in qs if v.hub_id in hub_by_id]
    facts = _robot_facts([v.current_revision.robot_id for v in visits if v.current_revision.robot_id])
    robots_by_hub = _hub_robots()
    charge_on = charging.forecast_configured()
    forecasts = charging.visit_forecasts([v.pk for v in visits], at) if charge_on and visits else {}
    shortages = stock.visit_shortages(at, sorted(hub_by_id)) if visits else {}
    pauses_by_hub = pauses.board_summary(sorted(hub_by_id))

    rows: list[Row] = []
    for v in visits:
        r = v.current_revision
        hub = hub_by_id[v.hub_id]
        zone = zones.zone_of(v)
        t_out, t_back = r.travel_out_min or 0, r.travel_back_min or 0
        row = Row(
            visit=v,
            code=v.code,
            hub_id=v.hub_id,
            hub_name=hub.name,
            hub_code=hub.code,
            home=home_name(v.site),
            home_code=v.site.code,
            start=r.on_site_start,
            end=r.on_site_end,
            depart=r.on_site_start - timedelta(minutes=t_out),
            crew=crew_names(r.lead, r.second),
            robot=r.robot.code if r.robot else "",
            vehicle=r.vehicle.code if r.vehicle else "",
            zone=zone.name if zone is not None else "",
            status="available",
            crew_key=(r.lead_id, r.second_id),
        )
        if not v.departed:
            reasons: list[Reason] = []
            if r.robot_id and r.robot_id in facts:
                eligible, why, where = facts[r.robot_id]
                window = (
                    r.on_site_start - timedelta(minutes=t_out),
                    r.on_site_end + timedelta(minutes=t_back),
                )
                reasons += _robot_reasons(row, eligible, why, where, window)
            reasons += _stored_reasons(list(r.blockers or []))
            f = forecasts.get(v.pk)
            if f is not None and f.cannot_in_time:
                reasons.append(
                    Reason(
                        "blocked", f"Robot {row.robot} cannot be charged in time for this visit.", "CHARGE"
                    )
                )
            elif f is not None and f.state == charging.NEEDS:
                reasons.append(
                    Reason("at_risk", f"Robot {row.robot} needs charge before it leaves.", "CHARGE")
                )
            elif f is not None and f.state == charging.UNKNOWN:
                reasons.append(Reason("at_risk", f"Charge forecast unknown for robot {row.robot}.", "CHARGE"))
            for n in shortages.get(v.pk, []):
                reasons.append(Reason("at_risk", f"{n.reason}.", "STOCK"))
            for p in pauses_by_hub.get(v.hub_id, []):
                if _pause_covers(p, zone.id if zone is not None else None):
                    reasons.append(Reason("paused", pause_banner(hub.name, p), "PAUSE"))
                    break
            reasons.sort(key=lambda x: SEVERITY.get(x.level, 9))
            row.reasons = reasons
        if v.zone_id is None and not v.departed:
            row.legacy = legacy_travel_text(r.travel_out_min, r.travel_back_min)
        row.status = _status(row, bool(v.departed))
        rows.append(row)

    sections = []
    for h in visible:
        mine = [x for x in rows if x.hub_id == h.pk]
        crews = {x.crew_key for x in mine}
        crews.discard((None, None))
        sections.append(
            HubSection(
                hub_id=h.pk,
                code=h.code,
                name=h.name,
                area=getattr(h, "area", "") or "",
                robots=robots_by_hub.get(h.pk, {"available": 0, "away": 0, "held": 0}),
                crews=len(crews),
                pauses=list(pauses_by_hub.get(h.pk, [])),
                rows=mine,
                zones=sorted({x.zone for x in mine if x.zone}),
            )
        )
    attention = sorted((x for x in rows if x.attention), key=attention_key)
    roles = set(scope.roles)
    return Board(
        at=at,
        day=day,
        day_key=day_key,
        lo=lo,
        hi=hi,
        rows=rows,
        hubs=sections,
        attention=attention,
        attention_total=len(attention),
        can_plan=bool(roles & PERMISSIONS["planning.plan"]),
        who_plan=role_words("planning.plan"),
        charge_off=not charge_on,
    )


def attention_key(row: Row) -> tuple[int, datetime, str]:
    """Contract 13.4 item 5: severity (blocked, paused, at risk), then departure proximity, then visit
    code."""
    return (SEVERITY[row.status], row.depart, row.code)

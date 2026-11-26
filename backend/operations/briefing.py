"""The Operations briefing (v2 Phase 04, EM ruling; contract 13, 15.11): ONE read-only page for ops leads.

It answers four questions per hub the viewer can see (``platform_core.hubscope``; ``?hub=`` narrows):

1. Which robots are unavailable for deployment, and why.
2. Which visits are at risk today.
3. Which unresolved incidents need attention.
4. What to prioritise at each hub: a short ordered list built from 1-3 by the fixed rules in ``RULES``.

It is NOT an AI and NOT an agent: there is no language model in this application. Every sentence comes
from the plain rules below, applied to the SAME read models the other pages use, so the answers agree:

    robots      operations.readmodels.fleet_overview (the Robot status rule, Today's fleet groups), the
                robot's planning-eligibility reasons (planning.plain.robot_reason), the departure gate's own
                SQL function execution_robot_departure_problem (its 03D ROBOT_INTERRUPTION_UNRESOLVED case,
                read through execution.custody.unresolved_interruption) and the charge forecast (hub.charging)
    visits      planning.board.build_board (the dispatch board's status and reasons for the local today),
                joined with operations.today.build_today (Today's row, from the readiness evaluator)
    incidents   operations.readmodels.incident_queue (queue order) with Today's issue cards (escalations)
    pauses      the board's hub sections (operations.pauses)

READ ONLY: it runs inside the page's one REPEATABLE READ READ ONLY snapshot (operations.views.ops_view),
writes nothing, offers no form and no action. Missing data is said in words ("Not enough data to say ..."),
never guessed. Nothing here is departure authorization; no line says a robot or a visit is ready to leave.

STATEMENTS: bounded, independent of fleet size: build_today (capped readiness, see operations.today) + the
board's fixed statements + fleet_overview (1) + robot hubs (1) + departure problems (1, set-based) + charge
setting and forecast (2) + incident queue and its hubs (<= 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from django.db import connection, transaction

from operations import readmodels, today
from planning import board as pboard
from planning.plain import robot_reason
from platform_core.interaction import who_can
from platform_core.templatetags.ui import local_time_tz

log = logging.getLogger("operations.briefing")

FOOTER = "This briefing summarises operational records. It cannot authorize, dispatch or control robots."
NOT_ENOUGH = "Not enough data to say"
PRIORITY_LIMIT = 5
LIST_LIMIT = 12  # codes listed in one "Not enough data" line before "and N more"

# level -> (words, shape, tone). Words plus shape, never colour alone (design spec principle 5).
LEVELS: dict[str, tuple[str, str, str]] = {
    "blocked": ("Blocked", "■", "bad"),
    "unavailable": ("Unavailable", "■", "bad"),
    "paused": ("Paused", "◆", "warn"),
    "at_risk": ("At risk", "▲", "warn"),
    "waiting": ("Not yet available", "▲", "warn"),
    "out": ("Out", "○", "neutral"),
    "attention": ("Needs attention", "▲", "warn"),
    "open": ("Open", "○", "neutral"),
}
_LEVEL_ORDER = {"blocked": 0, "unavailable": 0, "paused": 1, "at_risk": 2, "waiting": 2, "attention": 2}


# ------------------------------------------------------------------ the prioritising rules (question 4)
@dataclass(frozen=True)
class Rule:
    number: int
    title: str
    why: str


RULES: list[Rule] = [
    Rule(1, "A robot nobody can locate", "Its whereabouts are not confirmed, so it may be lost or unsafe."),
    Rule(
        2,
        "An incident nobody has triaged, or an escalation that is late or not acknowledged",
        "A decision is waiting and the person who must act may not know.",
    ),
    Rule(
        3,
        "A robot with an unresolved interruption",
        "It cannot be authorized to leave until the interruption is resolved.",
    ),
    Rule(4, "A visit today that is blocked", "It cannot go ahead as planned. Earliest departure first."),
    Rule(
        5, "A departure pause at the hub", "No visit it covers can be authorized to leave until it is lifted."
    ),
    Rule(6, "A visit today that is at risk", "It can still go ahead if the problem is fixed in time."),
    Rule(7, "An incident with no severity decision", "Follow-up and escalation deadlines depend on it."),
    Rule(
        8,
        "Visits today whose checklist was not checked",
        "Not enough data to say whether they are at risk.",
    ),
]


# ------------------------------------------------------------------ data shapes
@dataclass
class Reason:
    headline: str  # "Unresolved interruption"
    sentence: str  # "Departure authorization is unavailable until the interruption is resolved."
    source: str  # "Robot record · Interruption history"
    detail: str = ""  # codes for traceability, shown in the quieter style
    code: str = ""  # the machine code it came from (data attribute only)


@dataclass
class Item:
    kind: str  # robot / visit / incident / pause
    ref: str  # robot, visit or incident code
    level: str  # a LEVELS key
    title: str  # "NB-014", "10:00 PST · Piedmont home", "IN-0003 · Fall"
    url: str  # the source record
    reasons: list[Reason] = field(default_factory=list)
    when: datetime | None = None
    code_text: str = ""  # quiet codes next to the title
    note: str = ""  # one calm extra line (e.g. "Readiness checklist: not checked on this load")

    @property
    def words(self) -> str:
        return LEVELS[self.level][0]

    @property
    def shape(self) -> str:
        return LEVELS[self.level][1]

    @property
    def tone(self) -> str:
        return LEVELS[self.level][2]

    @property
    def first(self) -> Reason | None:
        return self.reasons[0] if self.reasons else None

    @property
    def rest(self) -> list[Reason]:
        return self.reasons[1:]


@dataclass
class Unknown:
    """One explicit "Not enough data to say ..." line."""

    key: str
    text: str
    source: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (code, url), at most LIST_LIMIT
    more: int = 0
    more_url: str = ""


@dataclass
class Priority:
    rule: Rule
    text: str
    url: str
    source: str
    who: str  # "Who can act: ..." (role words only; never a button)
    ref: str
    when: datetime | None = None


@dataclass
class HubBriefing:
    hub_id: int
    code: str
    name: str
    robots: list[Item] = field(default_factory=list)
    robots_out: list[Item] = field(default_factory=list)
    robots_unknown: list[Unknown] = field(default_factory=list)
    robots_total: int = 0
    pauses: list[Item] = field(default_factory=list)
    visits: list[Item] = field(default_factory=list)
    visits_unknown: list[Unknown] = field(default_factory=list)
    visits_total: int = 0
    visits_clear: int = 0
    incidents: list[Item] = field(default_factory=list)
    incidents_open: list[Item] = field(default_factory=list)
    incidents_unknown: list[Unknown] = field(default_factory=list)
    priorities: list[Priority] = field(default_factory=list)
    more_priorities: int = 0

    @property
    def anchor(self) -> str:
        return f"hub-{self.code}"


@dataclass
class Briefing:
    at: datetime
    day: date
    day_text: str
    hubs: list[HubBriefing] = field(default_factory=list)
    notices: list[Unknown] = field(default_factory=list)  # whole-page "Not enough data" (a source failed)
    robots_ok: bool = False
    visits_ok: bool = False
    readiness_ok: bool = False
    incidents_ok: bool = False
    charge_on: bool = True
    lookahead_on: bool = True
    issues_exact: bool = True
    deadlines_on: bool = True
    coverage: dict[str, Any] | None = None


# ------------------------------------------------------------------ question 1: robots
_RETURN_WORDS = {
    "post_visit_inspection": "its post-visit inspection",
    "reconciliation": "the reconciliation of what came back",
    "trip_closure": "the trip to be closed",
}


def _eligibility_source(reason: str) -> str:
    r = reason.strip()
    if r.startswith("In transfer"):
        return "Robot record · Transfers"
    if r.startswith(("Maintenance", "No approved maintenance")):
        return "Robot record · Maintenance"
    if r.startswith(("Periodic", "No qualifying periodic")):
        return "Robot record · Inspections"
    if r.startswith(("Robot hold", "Configuration hold")):
        return "Robot record · Holds"
    if r.startswith(("Away on run", "Not at a hub", "At hub ", "Location unknown")):
        return "Robot record · Custody history"
    return "Robot record · Planning eligibility"


def robot_reasons(r: readmodels.RobotStatus) -> tuple[str, list[Reason]]:
    """(level, reasons) from the robot status rule (operations_robot_status, the Robot status page's rule) and
    its eligibility reasons. Level "" = no reason here (Available or Assigned)."""
    rule = int(r.status_rule)
    if rule == 1:
        return "unavailable", [
            Reason(
                "Cannot be accounted for",
                "Nobody has confirmed where it is. It cannot be planned or sent out until it is found and "
                "recorded.",
                "Robot record · Custody history",
                code="ROBOT_UNACCOUNTED",
            )
        ]
    if rule == 2:
        return "unavailable", [
            Reason(
                "Custody unknown",
                "The records do not show who has it. It stays out of planning until its custody is sorted "
                "out.",
                "Robot record · Custody history",
                code="CUSTODY_UNKNOWN",
            )
        ]
    if rule == 3:
        run = r.open_run
        where = f"run {run.code} for visit {run.visit}" if run is not None else "a run"
        return "out", [
            Reason(
                "Out on a visit",
                "Not available until it is back at the hub, checked and the trip is closed.",
                "Robot record · Field run",
                detail=where,
                code="DEPLOYED",
            )
        ]
    if rule == 4:
        waiting = _RETURN_WORDS.get(r.awaiting, "the trip to be closed")
        run = r.open_run
        return "waiting", [
            Reason(
                "Back at the hub, not yet available",
                f"It is waiting for {waiting}.",
                "Robot record · Field run",
                detail=f"run {run.code}" if run is not None else "",
                code="RETURNED",
            )
        ]
    if rule == 5:
        holds = ", ".join(r.holds)
        return "unavailable", [
            Reason(
                "On hold",
                "It cannot be planned or leave until the hold is released.",
                "Robot record · Holds",
                detail=holds,
                code="HELD",
            )
        ]
    if rule == 7:
        # ONE line per robot: the distinct short reasons in the eligibility view's order; the source is the
        # first reason's record; the raw reasons stay available as the quiet detail.
        raws = list(r.eligibility_reasons) or ["not ready"]
        shorts = list(dict.fromkeys(robot_reason(x).short for x in raws))
        sources = list(dict.fromkeys(_eligibility_source(x) for x in raws))
        return "unavailable", [
            Reason(
                "Not ready for planning",
                f"{'; '.join(shorts)[:1].upper()}{'; '.join(shorts)[1:]}.",
                " · ".join([sources[0], *[x.split(" · ", 1)[-1] for x in sources[1:]]]),
                detail=f"{len(raws)} reason{'s' if len(raws) != 1 else ''} on the robot record",
                code="NOT_ELIGIBLE",
            )
        ]
    return "", []


def interruption_reason(where: str) -> Reason:
    return Reason(
        "Unresolved interruption",
        "Departure authorization is unavailable until the interruption is resolved.",
        "Robot record · Interruption history",
        detail=where,
        code="ROBOT_INTERRUPTION_UNRESOLVED",
    )


DEPARTURE_PROBLEM_SQL = (
    "SELECT r.id, execution_robot_departure_problem(r.id) FROM fleet_robot r "
    "WHERE r.id = ANY(%s) ORDER BY r.id"
)


def departure_problems(robot_ids: list[int]) -> dict[int, str]:
    """robot id -> the departure gate's refusal text (the authoritative SQL function), for the robots given.
    ONE statement, whatever the number of robots."""
    if not robot_ids:
        return {}
    with connection.cursor() as cur:
        cur.execute(DEPARTURE_PROBLEM_SQL, [sorted(set(robot_ids))])
        return {rid: problem for rid, problem in cur.fetchall() if problem}


def robot_hubs(robot_ids: list[int]) -> dict[int, int]:
    from fleet.models import Robot  # noqa: PLC0415

    if not robot_ids:
        return {}
    return dict(Robot.objects.filter(pk__in=robot_ids).values_list("id", "hub_id"))


# ------------------------------------------------------------------ question 2: visits
_TODAY_CHARGE = ("Needs charge before departure", "Charge forecast unknown")


def _board_source(code: str) -> str:
    if code in ("TRAVEL_UNKNOWN", "UNKNOWN_TRAVEL", "ZONE_RETIRED"):
        return "Visit plan · Travel times"
    if code == "PAUSE":
        return "Regional pause record"
    if code.startswith(("CHARGE",)):
        return "Robot record · Battery readings"
    if code.startswith("STOCK:"):
        return "Hub stock"
    if code.startswith("ROBOT_"):
        return "Robot record · Planning eligibility"
    if code.startswith(("CREW_", "QUALIFICATION")):
        return "Visit plan · Crew"
    if code.startswith("VEHICLE_"):
        return "Visit plan · Vehicle"
    return "Visit plan"


def visit_reasons(row: pboard.Row, trow: today.VisitRow | None) -> list[Reason]:
    """The board's reasons, then Today's readiness reasons that the board does not already say (same charge,
    stock or hold fact in other words is said once)."""
    out = [
        Reason("", x.text, _board_source(x.code), code=x.code or "PLAN")
        for x in row.reasons
        if x.level in ("blocked", "paused", "at_risk")
    ]
    codes = {x.code for x in row.reasons}
    texts = {x.text.rstrip(".") for x in row.reasons}
    if trow is not None and trow.status in ("blocked", "at_risk"):
        for why in trow.why:
            w = why.rstrip(".")
            if w in texts:
                continue
            if w in _TODAY_CHARGE and codes & {"CHARGE", "CHARGE_NEEDS", "CHARGE_UNKNOWN"}:
                continue
            if w.endswith("is on hold") and "ROBOT_NOT_ELIGIBLE" in codes:
                continue
            out.append(Reason("", w + ".", "Departure readiness checklist", code="READINESS"))
    return out


def _short(text: str) -> str:
    """A headline from a sentence: its first clause, without a trailing period."""
    t = text.strip().rstrip(".")
    for sep in (": ", "; "):
        if sep in t:
            t = t.split(sep, 1)[0]
    return t


def visit_level(row: pboard.Row, trow: today.VisitRow | None) -> str:
    """The combined status: the worse of the dispatch board and Today's readiness result.
    "" = no open problem in either; "unknown" = no readiness result (not checked on this load)."""
    b = row.status
    t = trow.status if trow is not None else None
    if b == "blocked" or t == "blocked":
        return "blocked"
    if b == "paused":
        return "paused"
    if b == "at_risk" or t == "at_risk":
        return "at_risk"
    if t in ("ready", "approved"):
        return ""
    return "unknown"


# ------------------------------------------------------------------ question 3: incidents
_INCIDENT_CARD_CODES = ("INCIDENT_UNTRIAGED", "ESCALATION_UNACKNOWLEDGED", "ESCALATION_OVERDUE")


def age_text(at: datetime, then: datetime) -> str:
    minutes = max(0, int((at - then).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def incident_item(i: readmodels.QueuedIncident, at: datetime, overdue: bool) -> Item:
    reasons: list[Reason] = []
    if i.status == "reported":
        reasons.append(
            Reason(
                "Not triaged yet",
                "Nobody has looked at it and decided what happens next.",
                "Incident record",
                code="INCIDENT_UNTRIAGED",
            )
        )
    if overdue:
        reasons.append(
            Reason(
                "Escalation past its deadline",
                "The person it was escalated to has not answered in time.",
                "Incident record · Escalations",
                code="ESCALATION_OVERDUE",
            )
        )
    if i.unacknowledged_escalations:
        n = i.unacknowledged_escalations
        reasons.append(
            Reason(
                "Escalation not acknowledged",
                f"{n} escalation{'s are' if n != 1 else ' is'} waiting for acknowledgment.",
                "Incident record · Escalations",
                code="ESCALATION_UNACKNOWLEDGED",
            )
        )
    if i.unclassified:
        reasons.append(
            Reason(
                "No severity decision",
                "How serious it is has not been decided.",
                "Incident record · Severity decisions",
                code="SEVERITY_UNCLASSIFIED",
            )
        )
    severity = "Severity not decided" if i.unclassified else f"Severity {i.severity_level or 'unknown'}"
    hold = f"Robot {i.robot} stays on hold ({i.hold})." if i.hold_active else ""
    return Item(
        kind="incident",
        ref=i.code,
        level="attention" if reasons else "open",
        title=f"{i.code} · {i.event_type.replace('_', ' ').capitalize()} involving robot {i.robot}",
        url=i.url,
        reasons=reasons,
        when=i.reported_at,
        code_text=f"reported {age_text(at, i.reported_at)} · {severity} · status "
        f"{i.status.replace('_', ' ')}",
        note=hold,
    )


# ------------------------------------------------------------------ building
def _try(b: Briefing, key: str, what: str, fn: Any) -> Any:
    """Run one read in a savepoint; a failure becomes an explicit "Not enough data" notice, never a zero."""
    try:
        with transaction.atomic():
            return fn()
    except Exception:
        log.exception("Briefing: could not load %s", what)
        b.notices.append(
            Unknown(
                f"error-{key}",
                f"{NOT_ENOUGH} about {what}: the records could not be loaded. Nothing from them is shown "
                "instead of a list that might be wrong. Reload the page; if it keeps happening, tell "
                "engineering.",
                "This page",
            )
        )
        return None


def _codes_line(items: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], int]:
    return items[:LIST_LIMIT], max(0, len(items) - LIST_LIMIT)


def build_briefing(at: datetime, user: Any, scope: Any) -> Briefing:
    from hub import charging  # noqa: PLC0415

    day = today.local_day(at)
    b = Briefing(at=at, day=day, day_text=day.strftime("%a %-d %b"))
    hub_ids = scope.ids

    board = _try(b, "board", "today's visit plans", lambda: pboard.build_board(at, scope, day, "today"))
    t = today.build_today(at, user, hub_ids)  # its own savepoints and notices per section
    robots = _try(b, "robots", "the robots", lambda: readmodels.fleet_overview(at))
    queue = _try(b, "incidents", "open incidents", lambda: readmodels.incident_queue(at))

    # hubs shown: the board's sections (the viewer's visible hubs, ?hub= applied); else the scope's hubs
    if board is not None:
        sections = {h.hub_id: h for h in board.hubs}
        hubs = [HubBriefing(h.hub_id, h.code, h.name) for h in board.hubs]
    else:
        sections = {}
        hubs = [HubBriefing(h.pk, h.code, h.name) for h in scope.hubs if scope.allows(h.pk)]
    by_hub = {h.hub_id: h for h in hubs}
    b.hubs = hubs

    # ---------------- question 1: robots
    if robots is not None:
        hub_of = _try(b, "robot-hubs", "the robots' hubs", lambda: robot_hubs([r.robot_id for r in robots]))
        if hub_of is not None:
            mine = [r for r in robots if hub_of.get(r.robot_id) in by_hub]
            at_hub = [r.robot_id for r in mine if int(r.status_rule) in (5, 6, 7, 8)]
            problems = _try(b, "departure", "departure checks", lambda: departure_problems(at_hub)) or {}
            from execution.custody import unresolved_interruption  # noqa: PLC0415

            b.charge_on = bool(_try(b, "charge-setting", "the charge setting", charging.forecast_configured))
            plain = []  # available or assigned robots with no other reason: their charge is checked
            for r in mine:
                hb = by_hub[hub_of[r.robot_id]]
                hb.robots_total += 1
                level, reasons = robot_reasons(r)
                found = unresolved_interruption(problems.get(r.robot_id))
                if found is not None:
                    reasons.insert(0, interruption_reason(found.where))
                    level = "unavailable"
                if not level:
                    plain.append(r)
                    continue
                item = Item("robot", r.robot, level, r.robot, r.url, reasons)
                (hb.robots_out if level == "out" else hb.robots).append(item)
            if b.charge_on and plain:
                forecasts = (
                    _try(
                        b,
                        "charge",
                        "battery levels",
                        lambda: charging.robot_forecasts([r.robot_id for r in plain], at, at),
                    )
                    or {}
                )
                unknown: dict[int, list[tuple[str, str]]] = {}
                for r in plain:
                    f = forecasts.get(r.robot_id)
                    hb = by_hub[hub_of[r.robot_id]]
                    if f is None or f.state == charging.UNKNOWN:
                        unknown.setdefault(hb.hub_id, []).append((r.robot, r.url))
                    elif f.state == charging.NEEDS:
                        hb.robots.append(
                            Item(
                                "robot",
                                r.robot,
                                "at_risk",
                                r.robot,
                                r.url,
                                [
                                    Reason(
                                        "Needs charge",
                                        f"It cannot leave until it is charged: {f.reason}.",
                                        "Robot record · Battery readings",
                                        code="CHARGE_NEEDS",
                                    )
                                ],
                            )
                        )
                for hub_id, codes in unknown.items():
                    shown, more = _codes_line(codes)
                    hb = by_hub[hub_id]
                    n = len(codes)
                    hb.robots_unknown.append(
                        Unknown(
                            "battery",
                            f"{NOT_ENOUGH} whether {n} robot{'s' if n != 1 else ''} at {hb.name} "
                            f"{'have' if n != 1 else 'has'} enough charge: no recent battery level is "
                            "recorded.",
                            "Robot records · Battery readings",
                            shown,
                            more,
                            f"/hub/charging/?hub={hb.code}",
                        )
                    )
            elif not b.charge_on:
                for hb in hubs:
                    if hb.robots_total:
                        hb.robots_unknown.append(
                            Unknown(
                                "charge-off",
                                f"{NOT_ENOUGH} whether robots at {hb.name} have enough charge: the battery "
                                "freshness setting is not provisioned, so no charge forecast is made. "
                                "Departure still needs its battery check.",
                                "Operating settings",
                            )
                        )
            for hb in hubs:
                hb.robots.sort(key=lambda x: (_LEVEL_ORDER.get(x.level, 9), x.ref))
                hb.robots_out.sort(key=lambda x: x.ref)
            b.robots_ok = True

    # pauses: one line per active pause of the hub (the board's own banner text)
    for hb in hubs:
        sec = sections.get(hb.hub_id)
        for p in sec.pauses if sec is not None else []:
            hb.pauses.append(
                Item(
                    "pause",
                    str(p.pause_id),
                    "paused",
                    p.where if hasattr(p, "where") else hb.name,
                    "/planning/pause/",
                    [Reason("Departures paused", pboard.pause_banner(hb.name, p), "Regional pause record")],
                    when=getattr(p, "since", None),
                )
            )

    # ---------------- question 2: visits at risk today
    b.readiness_ok = bool(t.visits_ok)
    b.lookahead_on = "ops_readiness_lookahead_min" not in t.unconfigured
    b.coverage = t.coverage
    trows = {r.code: r for r in t.visits if r.kind == "visit" and r.day == "today"}
    if board is not None:
        b.visits_ok = True
        unknown_v: dict[int, list[tuple[str, str]]] = {}
        for row in board.rows:
            vh = by_hub.get(row.hub_id)
            if vh is None or row.status == "departed":
                continue
            vh.visits_total += 1
            trow = trows.get(row.code) if b.readiness_ok else None
            level = visit_level(row, trow)
            reasons = visit_reasons(row, trow)
            url = f"/planning/visits/{row.code}/"
            confirmed = getattr(row.visit, "status", "confirmed") == "confirmed"
            if not confirmed and level in ("", "unknown"):
                level = "at_risk"
                reasons.append(
                    Reason(
                        "The visit is not confirmed",
                        "Its departure checklist cannot start until it is confirmed.",
                        "Visit record",
                        code="VISIT_NOT_CONFIRMED",
                    )
                )
            elif row.end <= at and level in ("", "unknown"):
                level = "at_risk"
                reasons.append(
                    Reason(
                        "Its time has passed",
                        "The visit window is over and no departure is recorded.",
                        "Visit record · Departure records",
                        code="PAST_NO_DEPARTURE",
                    )
                )
            if level == "unknown":
                unknown_v.setdefault(vh.hub_id, []).append((row.code, url))
                continue
            if not level:
                vh.visits_clear += 1
                continue
            note = ""
            if trow is None or trow.status == "scheduled":
                note = "Departure checklist: not checked on this page load."
            title = f"{local_time_tz(row.start)} · {row.home}"
            vh.visits.append(
                Item(
                    "visit",
                    row.code,
                    level,
                    title,
                    url,
                    reasons,
                    when=row.leaves,
                    code_text=f"{row.home_code} · {row.code}"
                    + (f" · Robot {row.robot}" if row.robot else ""),
                    note=note,
                )
            )
        for hub_id, codes in unknown_v.items():
            hb = by_hub[hub_id]
            n = len(codes)
            shown, more = _codes_line(codes)
            why = (
                "the readiness look-ahead setting is missing, so no checklist is checked"
                if not b.lookahead_on
                else (
                    f"their departure checklist was not checked on this page load (at most "
                    f"{today.READINESS_CAP} visits are checked per load)"
                    if b.readiness_ok
                    else "the readiness results could not be loaded"
                )
            )
            hb.visits_unknown.append(
                Unknown(
                    "readiness",
                    f"{NOT_ENOUGH} whether {n} visit{'s' if n != 1 else ''} today at {hb.name} "
                    f"{'are' if n != 1 else 'is'} at risk: {why}. The dispatch board shows no plan problem "
                    f"for {'them' if n != 1 else 'it'}.",
                    "Dispatch board · Departure readiness checklist",
                    shown,
                    more,
                    f"/readiness/?hub={hb.code}",
                )
            )
        for hb in hubs:
            hb.visits.sort(key=lambda x: (_LEVEL_ORDER.get(x.level, 9), x.when or at, x.ref))

    # ---------------- question 3: incidents
    b.issues_exact = bool(t.issues_ok and t.issue_count_exact)
    card_codes = [c for c in t.inbox_cards if c["code"] in _INCIDENT_CARD_CODES]
    overdue_urls = {c["url"] for c in card_codes if c["code"] == "ESCALATION_OVERDUE"}
    b.deadlines_on = not any(c["code"] == "ESCALATION_DEADLINE_NOT_CONFIGURED" for c in t.inbox_cards)
    if queue is not None:
        from operations import scope as opscope  # noqa: PLC0415

        visits = opscope.visit_hubs_by_code(i.visit for i in queue if i.visit)
        robots_h = opscope.robot_hubs_by_code(i.robot for i in queue if not i.visit)
        for i in queue:
            inc_hub = visits.get(i.visit) if i.visit else robots_h.get(i.robot)
            ih = by_hub.get(inc_hub) if inc_hub is not None else None
            if ih is None:
                continue
            item = incident_item(i, at, i.url in overdue_urls)
            (ih.incidents if item.reasons else ih.incidents_open).append(item)
        for hb in hubs:
            if not b.deadlines_on and (hb.incidents or hb.incidents_open):
                hb.incidents_unknown.append(
                    Unknown(
                        "deadlines",
                        f"{NOT_ENOUGH} whether any escalation is late: no escalation deadline applies, so "
                        "late escalations cannot be shown until deadlines are set up and approved.",
                        "Escalation deadline settings",
                        more_url="/execution/escalation-deadlines/",
                    )
                )
            elif not b.issues_exact and (hb.incidents or hb.incidents_open):
                hb.incidents_unknown.append(
                    Unknown(
                        "issues-cut",
                        f"{NOT_ENOUGH} whether every late escalation is marked: the list of issues is longer "
                        f"than {today.INBOX_LIMIT} items, so some may not be shown here.",
                        "Issues list",
                        more_url="/ops/exceptions/",
                    )
                )
        b.incidents_ok = True

    # ---------------- question 4: priorities
    for hb in hubs:
        prioritise(hb)
    return b


def _rule(n: int) -> Rule:
    return RULES[n - 1]


def prioritise(hb: HubBriefing) -> None:
    """Question 4: candidates by the fixed RULES (lower rule first), then by time, then by code; the first
    PRIORITY_LIMIT are shown. Built only from the lines above, so it can never say more than they do."""
    cands: list[tuple[int, datetime | None, str, Priority]] = []

    def add(n: int, text: str, url: str, source: str, who: str, ref: str, when: datetime | None) -> None:
        cands.append((n, when, ref, Priority(_rule(n), text, url, source, who, ref, when)))

    for it in hb.robots:
        codes = {r.code for r in it.reasons}
        if codes & {"ROBOT_UNACCOUNTED", "CUSTODY_UNKNOWN"}:
            add(
                1,
                f"Find robot {it.ref} and record where it is.",
                it.url,
                "Robot record · Custody history",
                "the ops lead",
                it.ref,
                None,
            )
        if "ROBOT_INTERRUPTION_UNRESOLVED" in codes:
            add(
                3,
                f"Review the unresolved interruption on robot {it.ref}.",
                it.url,
                "Robot record · Interruption history",
                who_can("execution.triage_incident"),
                it.ref,
                None,
            )
    for it in hb.incidents:
        codes = {r.code for r in it.reasons}
        if codes & {"INCIDENT_UNTRIAGED", "ESCALATION_OVERDUE", "ESCALATION_UNACKNOWLEDGED"}:
            first = next(r for r in it.reasons if r.code in _INCIDENT_CARD_CODES)
            add(
                2,
                f"Incident {it.ref}: {first.headline.lower()}.",
                it.url,
                first.source,
                who_can("execution.triage_incident")
                if first.code == "INCIDENT_UNTRIAGED"
                else "the role it was escalated to",
                it.ref,
                it.when,
            )
        elif "SEVERITY_UNCLASSIFIED" in codes:
            add(
                7,
                f"Incident {it.ref}: decide its severity.",
                it.url,
                "Incident record · Severity decisions",
                who_can("execution.change_severity"),
                it.ref,
                it.when,
            )
    for it in hb.visits:
        lead = it.first
        why = (lead.headline or _short(lead.sentence)) if lead is not None else it.words
        if it.level == "blocked":
            add(
                4,
                f"{it.title} is blocked: {_lower(why)}.",
                it.url,
                _src(it),
                who_can("planning.plan"),
                it.ref,
                it.when,
            )
        elif it.level == "at_risk":
            add(
                6,
                f"{it.title} is at risk: {_lower(why)}.",
                it.url,
                _src(it),
                who_can("planning.plan"),
                it.ref,
                it.when,
            )
    paused_visits = sum(1 for it in hb.visits if it.level == "paused")
    for p in hb.pauses:
        n = paused_visits
        tail = f" It covers {n} visit{'s' if n != 1 else ''} today." if n else ""
        add(
            5,
            f"Departures are paused at {hb.name}.{tail}",
            p.url,
            "Regional pause record",
            who_can("operations.lift_pause"),
            p.ref,
            p.when,
        )
    for u in hb.visits_unknown:
        n = len(u.links) + u.more
        add(
            8,
            f"{n} visit{'s' if n != 1 else ''} today {'have' if n != 1 else 'has'} no checklist result: "
            f"{NOT_ENOUGH.lower()} whether {'they are' if n != 1 else 'it is'} at risk.",
            u.more_url,
            u.source,
            who_can("readiness.record_evidence"),
            "readiness",
            None,
        )
    cands.sort(key=lambda c: (c[0], c[1] is None, c[1].timestamp() if c[1] is not None else 0.0, c[2]))
    hb.priorities = [c[3] for c in cands[:PRIORITY_LIMIT]]
    hb.more_priorities = max(0, len(cands) - PRIORITY_LIMIT)


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:]


def _src(it: Item) -> str:
    return it.first.source if it.first is not None else "Dispatch board"

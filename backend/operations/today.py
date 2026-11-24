"""The "Today" dashboard (v2 milestone 01B): what is happening in the workspace's local today and tomorrow.

READ ONLY and built ONLY from the existing read models, inside the ONE snapshot of the page (the caller opens
``operations.snapshot.read_snapshot()`` and passes its time as ``at``):

    fleet_overview(at)              robots by status                              1 statement
    timeline(at, window_min)        visits of today and tomorrow + open runs      3 + <= 10 evaluator runs
    open_exceptions(at, ...)        the inbox WITHOUT readiness (the timeline's   3 statements
                                    readiness result is reused for the readiness cards, so the evaluator
                                    never runs twice for one page)
    production_blockers(at)         the one-line "Production readiness" item      1 statement
    authorization lookup            one set-based query for the visits shown, then the advisory
                                    ``authorization_status`` for at most READINESS_CAP of them

Every section is read inside its own savepoint, so a failure (the error is logged) leaves a clearly worded
"Could not load ..." notice instead of a blank page, and never a zero that looks healthy.

Wording rules (docs/10 section 3): plain sentences, internal codes only inside a "Details" fold; "Approved to
leave" is shown only for a usable authorization, with who, when and until when, and never in green; a complete
checklist is "Checklist complete, waiting for approval". Nothing here decides or grants a departure.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from django.db import transaction

from accounts.permissions import active_roles
from accounts.roles import PERMISSIONS
from operations import readmodels
from operations.exceptions import BLOCKER_CODES, OpsException, open_exceptions
from platform_core.templatetags.ui import workspace_zone

log = logging.getLogger("operations.today")

CARD_LIMIT = 8  # attention cards shown on Today; the rest are one click away on the Issues page
INBOX_LIMIT = 200
READINESS_CAP = readmodels.READINESS_CAP
# Statements of one readiness.authorization.authorization_status call (measured 22, pinned by a test): the
# page runs it for at most READINESS_CAP visits, so Today <= 12 + 10 x E + 10 x this
# (E = exceptions.READINESS_EVALUATOR_STATEMENTS).
AUTHORIZATION_STATEMENTS = 24

# ------------------------------------------------------------------ robot groups (fleet strip and counts)
# operations_robot_status rule -> group. Rules: 1 unaccounted, 2 custody unknown, 3 deployed, 4 returned and
# awaiting inspection/reconciliation/closure, 5 held, 6 assigned, 7 at hub but not eligible, 8 at hub and
# eligible.
ROBOT_GROUPS: list[tuple[str, str]] = [
    ("available", "Available"),
    ("assigned", "Assigned"),
    ("out", "Out"),
    ("on_hold", "On hold"),
    ("not_in_service", "Not in service"),
]
ROBOT_GROUP_NOTES = {
    "available": "At the hub and eligible for planning.",
    "assigned": "At the hub with a confirmed visit coming up.",
    "out": "On a trip, or back and waiting for inspection, reconciliation or close-out.",
    "on_hold": "Has an active blocking hold.",
    "not_in_service": "Not eligible for planning, or its whereabouts are not recorded.",
}
_RULE_GROUP = {1: "not_in_service", 2: "not_in_service", 3: "out", 4: "out", 5: "on_hold", 6: "assigned"}
_RULE_GROUP.update({7: "not_in_service", 8: "available"})


def robot_group(status_rule: int) -> str:
    return _RULE_GROUP.get(int(status_rule), "not_in_service")


# ------------------------------------------------------------------ plain words for readiness blockers
# What the crew can fix before departure => "At risk"; anything else (plan, policy, robot) => "Blocked".
_AT_RISK_CODES = {
    "EVIDENCE_MISSING",
    "EVIDENCE_FAILED",
    "EVIDENCE_STALE",
    "EVIDENCE_WRONG_CONFIGURATION",
    "BATTERY_MIN_UNKNOWN",
    "BATTERY_LOW",
    "EQUIPMENT_MISSING",
    "RECOVERY_PLAN_MISSING",
    "RECOVERY_ONCALL_NOT_ENGINEER",
    "EXPIRY_NOT_POSITIVE",
}
_BLOCKER_WORDS = {
    "VISIT_NOT_CONFIRMED": "The visit is not confirmed",
    "RESERVATIONS_MISSING": "The crew, vehicle or robot is not reserved",
    "POLICY_MISSING": "No readiness policy is in force",
    "POLICY_NOT_APPROVED_PROCEDURE": "The readiness policy is not approved for real use",
    "EVIDENCE_MISSING": "A pre-departure check has not been recorded",
    "EVIDENCE_FAILED": "A pre-departure check failed",
    "EVIDENCE_STALE": "A pre-departure check is out of date and must be repeated",
    "EVIDENCE_WRONG_CONFIGURATION": "A check was recorded for a different robot setup",
    "BATTERY_MIN_UNKNOWN": "The minimum battery level is not set",
    "BATTERY_LOW": "The battery is too low",
    "EQUIPMENT_MISSING": "Required equipment is missing",
    "RECOVERY_PLAN_MISSING": "There is no recovery plan",
    "RECOVERY_ONCALL_NOT_ENGINEER": "The recovery plan's on-call person is not an engineer",
    "EXPIRY_NOT_POSITIVE": "The checks would expire before the departure could be approved",
    "ROBOT_NOT_AT_HUB": "The robot is not at the hub",
    "INSPECTION_TEMPLATE_NOT_APPROVED": "The inspection checklist is not approved for real use",
    "POLICY_LACKS_INSPECTION": "The readiness policy does not require an inspection",
    "PERIODIC_INSPECTION_INTERVAL_UNKNOWN": "The periodic inspection interval is not set",
    "PERIODIC_INSPECTION_MISSING": "The robot has no valid periodic inspection",
    "PERIODIC_INSPECTION_DUE_BEFORE_RETURN": "A periodic inspection falls due before the robot is back",
    "INSPECTOR_NOT_QUALIFIED": "An inspection was signed by someone who is not qualified",
    "MAINTENANCE_DUE_BEFORE_RETURN": "Maintenance falls due before the robot is back",
    "PLANNING_ROBOT_MISSING": "No robot is assigned",
    "PLANNING_CREW_INCOMPLETE": "The crew is incomplete",
    "PLANNING_CREW_SAME_PERSON": "The lead and the second person must be different people",
    "PLANNING_VEHICLE_MISSING": "No vehicle is assigned",
    "PLANNING_CONSENT": "Consent at the home is not verified",
    "PLANNING_SITE_NOT_ELIGIBLE": "The home was assessed as not eligible",
    "PLANNING_UNKNOWN_TRAVEL": "Travel time is not entered",
    "PLANNING_NOT_FEASIBLE": "The visit plan does not work as it stands",
}
_FALLBACK_WORDS = {"PLANNING_": "The visit plan has an open problem"}
DEFAULT_WORDS = "A checklist item is not complete"
MAX_WHY = 3


def blocker_words(code: str) -> str:
    if code in _BLOCKER_WORDS:
        return _BLOCKER_WORDS[code]
    for prefix, text in _FALLBACK_WORDS.items():
        if code.startswith(prefix):
            return text
    return DEFAULT_WORDS


def _why_list(codes: list[str]) -> list[str]:
    seen: list[str] = []
    for c in codes:
        w = blocker_words(c)
        if w not in seen:
            seen.append(w)
    extra = len(seen) - MAX_WHY
    out = seen[:MAX_WHY]
    if extra > 0:
        out.append(f"and {extra} more")
    return out


# ------------------------------------------------------------------ visit rows
STATUS_ORDER = {"blocked": 0, "at_risk": 1, "ready": 2, "approved": 3, "scheduled": 4, "out": 5, "back": 6}


@dataclass
class VisitRow:
    kind: str  # "visit" (not yet departed) or "run" (departed, not yet closed)
    code: str
    robot: str | None
    start: datetime
    end: datetime
    day: str  # "today" or "tomorrow"
    status: str  # blocked / at_risk / ready / approved / scheduled / out / back
    label: str
    tone: str  # neutral / info / warn / bad  (never ok: no state here is permission)
    why: list[str] = field(default_factory=list)
    url: str = ""
    run: str | None = None
    run_url: str | None = None
    approval: dict[str, Any] | None = None
    note: str = ""


@dataclass
class Notice:
    key: str
    text: str
    tone: str = "warn"


@dataclass
class Today:
    at: datetime
    local_date: date
    local_date_text: str
    notices: list[Notice] = field(default_factory=list)
    visits_ok: bool = False
    visits: list[VisitRow] = field(default_factory=list)
    visit_counts: dict[str, int] = field(default_factory=dict)
    # Coverage of today + tomorrow (EM ruling): ``all`` is every visit from the database (never capped);
    # ``assessed`` is those with a known readiness position; the rest are ``unassessed``. The ready /
    # at-risk /
    # blocked tiles are only ever shown "of K assessed", never as the status of the fleet.
    coverage_counts: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, Any] | None = None
    robots_ok: bool = False
    robot_counts: dict[str, int] = field(default_factory=dict)
    issues_ok: bool = False
    issue_count: int = 0
    issue_count_exact: bool = True
    cards: list[dict[str, Any]] = field(default_factory=list)
    more_cards: int = 0
    attention_total: int = 0  # every attention card, shown or not (the jump link at the top, 01D J2-2)
    blockers_ok: bool = False
    blocker_count: int = 0
    inbox_cards: list[dict[str, Any]] = field(default_factory=list)
    readiness_cards: list[tuple[VisitRow, list[str], datetime]] = field(default_factory=list)
    unconfigured: list[str] = field(default_factory=list)
    charge_off: bool = False  # v2 02B: ops_soc_fresh_min missing, so no charge forecast is applied


def local_day(at: datetime) -> date:
    """The workspace's local calendar day at the snapshot time (tests patch this to place visits)."""
    return at.astimezone(workspace_zone()).date()


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=workspace_zone())


def window_minutes(at: datetime, today: date) -> int:
    """Minutes from ``at`` to the end of the workspace's tomorrow (at least 1)."""
    end = _day_start(today + timedelta(days=2))
    return max(1, int((end - at).total_seconds() // 60) + 1)


def _day_of(dt: datetime, today: date) -> str:
    d = dt.astimezone(workspace_zone()).date()
    return "today" if d <= today else "tomorrow"


# ------------------------------------------------------------------ approvals (departure authorizations)
def _usable_approvals(visit_ids: list[int], at: datetime) -> dict[int, dict[str, Any]]:
    """visit id -> the approval to show: the newest authorization that is not revoked, expired or used AND
    that the readiness page's own advisory check (``authorization_status``) calls usable. One set-based query,
    then that check for at most READINESS_CAP visits; visits beyond the cap are not claimed approved here."""
    from readiness.authorization import authorization_status
    from readiness.models import DepartureAuthorization

    if not visit_ids:
        return {}
    candidates: dict[int, DepartureAuthorization] = {}
    rows = (
        DepartureAuthorization.objects.filter(
            visit_id__in=visit_ids, expires_at__gt=at, revocation__isnull=True, departure__isnull=True
        )
        .select_related("issued_by", "robot", "visit", "policy")
        .order_by("visit_id", "-issued_at", "-id")
    )
    for a in rows:
        candidates.setdefault(a.visit_id, a)
    approved: dict[int, dict[str, Any]] = {}
    checked = 0
    for vid in visit_ids:
        cand = candidates.get(vid)
        if cand is None:
            continue
        if checked >= READINESS_CAP:
            break
        checked += 1
        if authorization_status(cand)["state"] == "usable":
            approved[vid] = {
                "code": cand.code,
                "by": cand.issued_by.get_username(),
                "at": cand.issued_at,
                "until": cand.expires_at,
                "robot": cand.robot.code,
                "visit": cand.visit.code,
            }
    return approved


def _visit_row(v: Any, approval: dict[str, Any] | None, today: date) -> VisitRow:
    base: dict[str, Any] = {
        "kind": "visit",
        "code": v.visit,
        "robot": v.robot,
        "start": v.on_site_start,
        "end": v.on_site_end,
        "day": _day_of(v.on_site_start, today),
        "url": v.url,
    }
    if approval is not None:
        return VisitRow(
            status="approved", label="Approved to leave", tone="info", approval=approval, **base
        )  # blue, never green
    why: list[str] = []
    hard = False
    if v.hold_codes:
        hard = True
        robot = f"Robot {v.robot}" if v.robot else "The robot"
        why.append(f"{robot} is on hold")
    if v.robot is None:
        hard = True
        why.append("No robot is assigned")
    if v.readiness_evaluated:
        codes = list(v.readiness_blockers)
        if codes:
            hard = hard or any(c not in _AT_RISK_CODES for c in codes)
            why += [w for w in _why_list(codes) if w not in why]
        elif not why:
            return VisitRow(
                status="ready",
                label="Checklist complete, waiting for approval",
                tone="neutral",
                **base,
            )
    elif not why:
        return VisitRow(
            status="scheduled",
            label="Scheduled",
            tone="neutral",
            note="Checklist not checked yet",
            **base,
        )
    if hard:
        return VisitRow(status="blocked", label="Blocked", tone="bad", why=why, **base)
    return VisitRow(status="at_risk", label="At risk", tone="warn", why=why, **base)


_OUT_STAGES = {"departed", "at_site", "returning"}


def _run_row(r: Any, today: date) -> VisitRow:
    out = r.stage in _OUT_STAGES
    note = ""
    if r.flags:
        note = "Has an open issue, see Needs your attention"
    return VisitRow(
        kind="run",
        code=r.visit,
        robot=r.robot,
        start=r.on_site_start,
        end=r.on_site_end,
        day="today",  # a trip that is open is out (or just back) now, whatever day it was booked for
        status="out" if out else "back",
        label="Out" if out else "Back at the hub",
        tone="info" if out else "neutral",
        url=readmodels.readiness_url(r.visit),
        run=r.run,
        run_url=r.url,
        note=note,
    )


def _charge_overlay(t: Today, visits: list[Any], rows: list[VisitRow]) -> None:
    """v2 02B (contract 11, 8.3): a visit whose robot needs charge before departure, or whose charge forecast
    is unknown, is At risk with that reason. Planning information only: an approval stays as it is, a blocked
    visit stays blocked, and departure still needs the battery check. Off (with a notice) while
    ops_soc_fresh_min is not provisioned, like the other owner thresholds."""
    from hub import charging

    if not charging.forecast_configured():
        t.charge_off = True
        return
    forecasts = charging.visit_forecasts([v.visit_id for v in visits], t.at)
    for v, row in zip(visits, rows, strict=True):
        f = forecasts.get(v.visit_id)
        if f is None or row.status == "approved" or f.state == charging.ENOUGH:
            continue
        reason = "Needs charge before departure" if f.state == charging.NEEDS else "Charge forecast unknown"
        row.why.append(reason)
        if row.status in ("ready", "scheduled"):
            row.status, row.label, row.tone, row.note = "at_risk", "At risk", "warn", ""


def _read_visits(t: Today) -> None:
    tl = readmodels.timeline(t.at, window_min=window_minutes(t.at, t.local_date))
    approvals = _usable_approvals([v.visit_id for v in tl.visits], t.at)
    rows = [_visit_row(v, approvals.get(v.visit_id), t.local_date) for v in tl.visits]
    _charge_overlay(t, tl.visits, rows)
    cards: list[tuple[VisitRow, list[str], datetime]] = []
    for v, row in zip(tl.visits, rows, strict=True):
        flag = next((f for f in v.flags if f.code == "READINESS_INCOMPLETE_SOON"), None)
        if flag is not None and row.status != "approved":
            cards.append((row, _why_list(list(v.readiness_blockers)), flag.since))
    rows += [_run_row(r, t.local_date) for r in tl.runs]
    rows.sort(key=lambda x: (x.day != "today", x.start, x.code))
    today_rows = [r for r in rows if r.day == "today"]
    cov = tl.readiness_coverage
    # assign only when everything above worked, so a failure never leaves half a section behind
    visit_rows = [r for r in rows if r.kind == "visit"]
    assessed_rows = [r for r in visit_rows if r.status != "scheduled"]
    today_visit_rows = [r for r in visit_rows if r.day == "today"]
    t.coverage_counts = {
        "all": len(visit_rows),
        "assessed": len(assessed_rows),
        "unassessed": len(visit_rows) - len(assessed_rows),
        "today_assessed": sum(1 for r in today_visit_rows if r.status != "scheduled"),
        "today_all": len(today_visit_rows),
    }
    t.visit_counts = {
        "visits": len(today_rows),
        "ready": sum(1 for r in today_rows if r.status == "ready"),
        "approved": sum(1 for r in today_rows if r.status == "approved"),
        "at_risk": sum(1 for r in today_rows if r.status == "at_risk"),
        "blocked": sum(1 for r in today_rows if r.status == "blocked"),
    }
    t.visits = rows
    t.coverage = cov if cov.get("not_evaluated", 0) > 0 else None
    t.readiness_cards = cards
    t.unconfigured = list(tl.unconfigured_thresholds)
    t.visits_ok = True


def _read_robots(t: Today) -> None:
    counts = {k: 0 for k, _ in ROBOT_GROUPS}
    for r in readmodels.fleet_overview(t.at):
        counts[robot_group(r.status_rule)] += 1
    t.robot_counts = counts
    t.robots_ok = True


# ------------------------------------------------------------------ attention cards
# code -> (what happened, why it matters, next action label, how the viewer qualifies)
# "perm:<name>" = a permission; "roles:a,b" = those roles (recover_robot is ops_lead only);
# "owner" = the viewer holds the exception's owner role; None = anyone who can read may open the page
# (there is nothing to do there beyond reading).
@dataclass(frozen=True)
class CardText:
    what: str
    why: str
    action: str
    gate: str | None


CARD_TEXT: dict[str, CardText] = {
    "ROBOT_UNACCOUNTED": CardText(
        "Robot {robot} cannot be accounted for.",
        "Nobody has confirmed where it is, so it cannot be used and may be lost or unsafe.",
        "Find the robot and record where it is",
        "roles:ops_lead",
    ),
    "CUSTODY_UNKNOWN": CardText(
        "The records do not show who has robot {robot}.",
        "This is a data problem; the robot stays out of planning until engineering sorts it out.",
        "Open the robot",
        None,
    ),
    "INCIDENT_UNTRIAGED": CardText(
        "A new incident involving robot {robot} has not been looked at yet.",
        "The robot is on hold, and nobody has decided what happens next.",
        "Review the incident",
        "perm:execution.triage_incident",
    ),
    "SEVERITY_UNCLASSIFIED": CardText(
        "An incident involving robot {robot} has no severity decision.",
        "Escalation deadlines and follow-up depend on how serious it is.",
        "Decide the severity",
        "perm:execution.change_severity",
    ),
    "ESCALATION_UNACKNOWLEDGED": CardText(
        "An incident was escalated to the {owner} and nobody has acknowledged it.",
        "The person who must act may not know about it yet.",
        "Acknowledge the escalation",
        "owner",
    ),
    "ESCALATION_OVERDUE": CardText(
        "The {owner} has not answered an escalation in time.",
        "The incident is waiting for a decision past its deadline.",
        "Acknowledge the escalation",
        "owner",
    ),
    "ESCALATION_DEADLINE_NOT_CONFIGURED": CardText(
        "No escalation deadline applies to an incident.",
        "Late escalations cannot be shown until deadlines are set up and approved.",
        "Set up escalation deadlines",
        "perm:execution.manage_escalation_deadlines",
    ),
    "INTERRUPTION_OPEN": CardText(
        "A visit session was interrupted and is still stopped.",
        "The robot cannot go on, and the visit cannot be closed, until it is resolved.",
        "Resolve the interruption",
        "perm:execution.record_field_event",
    ),
    "SITE_NO_GO": CardText(
        "The latest check at the home says Not today.",
        "The visit must not go ahead until someone records what happens next.",
        "Record what happens next",
        "perm:execution.record_disposition",
    ),
    "CUSTODY_UNVERIFIED": CardText(
        "The crew has not confirmed who took robot {robot} out.",
        "Without that confirmation we cannot be sure where the robot is.",
        "Confirm the handover",
        "perm:execution.record_field_event",
    ),
    "RETURN_OVERDUE": CardText(
        "Robot {robot} is late back at the hub.",
        "It is past the planned end of the visit, so it may be delayed or in trouble.",
        "Check on the visit",
        "perm:execution.record_field_event",
    ),
    "RECONCILIATION_UNRESOLVED": CardText(
        "What came back with robot {robot} does not match what went out.",
        "The trip cannot be closed until the difference is explained.",
        "Reconcile the return",
        "perm:execution.reconcile",
    ),
    "POST_VISIT_INSPECTION_MISSING": CardText(
        "Robot {robot} is back at the hub but has not been inspected.",
        "It should not be used again until it has been checked.",
        "Record the inspection",
        "perm:fleet.submit_inspection",
    ),
    "HELD_ROBOT_ASSIGNED": CardText(
        "Robot {robot} is on hold but is booked for visit {visit}.",
        "A robot on hold cannot leave, so the visit will fail unless it is released or swapped.",
        "Swap the robot or release the hold",
        "perm:planning.plan",
    ),
    "READINESS_INCOMPLETE_SOON": CardText(
        "Visit {visit} starts soon and its checklist is not complete.",
        "The robot cannot leave until every item is done.",
        "Finish the checklist",
        "perm:readiness.record_evidence",
    ),
    "PERIODIC_INTERVAL_UNCONFIGURED": CardText(
        "No inspection interval is set for robot setup {entity}.",
        "Its robots cannot be planned or approved to leave until an interval is approved.",
        "Set the inspection interval",
        "perm:fleet.manage_periodic_rule",
    ),
    "PERIODIC_INSPECTION_OVERDUE": CardText(
        "Robot {robot} is overdue for its regular inspection.",
        "It cannot be approved to leave until the inspection is done.",
        "Record the inspection",
        "perm:fleet.submit_inspection",
    ),
    "PERIODIC_INSPECTION_DUE_SOON": CardText(
        "Robot {robot} is due for its regular inspection soon.",
        "If it is not done in time the robot will stop being usable.",
        "Plan the inspection",
        "perm:fleet.submit_inspection",
    ),
    "THRESHOLD_NOT_CONFIGURED": CardText(
        "The setting for {setting} is missing, so that automatic check is switched off.",
        "Problems that check would catch will not show up here until it is set.",
        "Ask engineering to set it",
        None,
    ),
}
_DEFAULT_CARD = CardText(
    "Something needs attention.", "It has not been looked at yet.", "Open the page", None
)
NO_PAGE_CODES = {"THRESHOLD_NOT_CONFIGURED"}  # nothing to do in the app: the schema owner sets it
SETTING_WORDS = {
    "ops_custody_verify_grace_min": "the crew's handover confirmation",
    "ops_return_grace_min": "late returns",
    "ops_inspection_grace_min": "post-visit inspections",
    "ops_readiness_lookahead_min": "upcoming checklists",
    "ops_periodic_due_lookahead_min": "regular inspections coming due",
    "ops_closeout_timezone": "the daily close-out time zone",
}


def _role_words(role: str) -> str:
    return str(role).replace("_", " ")


def _can(roles: set[str], gate: str | None, owner_role: str) -> bool:
    if gate is None:
        return True
    if gate == "owner":
        return owner_role in roles
    if gate.startswith("roles:"):
        return bool(roles & set(gate.removeprefix("roles:").split(",")))
    return bool(roles & PERMISSIONS.get(gate.removeprefix("perm:"), set()))


def card_for(e: OpsException, roles: set[str]) -> dict[str, Any]:
    text = CARD_TEXT.get(e.code, _DEFAULT_CARD)
    fmt = {
        "robot": e.robot or "",
        "visit": e.visit or "",
        "owner": _role_words(e.owner_role),
        "entity": e.entity_ref,
        "setting": SETTING_WORDS.get(e.entity_ref, "a deployment value"),
    }
    what = text.what.format(**fmt)
    allowed = _can(roles, text.gate, e.owner_role)
    read_only = text.gate is None
    return {
        "code": e.code,
        "ref": e.entity_ref,
        "priority": e.priority,
        "what": what,
        "why": text.why,
        "owner": _role_words(e.owner_role),
        "since": e.since,
        "url": None if e.code in NO_PAGE_CODES else e.action_url,
        "action": text.action,
        "can_act": allowed and not read_only,
        "read_only": read_only,
        "robot": e.robot,
        "visit": e.visit,
        "detail": e.message,
    }


def _readiness_card(row: VisitRow, why: list[str], since: datetime, roles: set[str]) -> dict[str, Any]:
    fake = OpsException(
        code="READINESS_INCOMPLETE_SOON",
        priority=3,
        entity_type="visit",
        entity_ref=row.code,
        robot=row.robot,
        visit=row.code,
        since=since,
        owner_role="ops_lead",
        message=f"Visit {row.code} starts within the readiness lookahead and is not ready",
        action_url=row.url,
        resolvable=False,
    )
    card = card_for(fake, roles)
    if why:
        card["what"] = (
            f"Visit {row.code} starts soon and is not ready: "
            + "; ".join(w[0].lower() + w[1:] for w in why)
            + "."
        )
    return card


def _read_issues(t: Today, roles: set[str]) -> None:
    page = open_exceptions(t.at, limit=INBOX_LIMIT, include_readiness=False)
    t.inbox_cards = [card_for(x, roles) for x in page.items if x.code not in BLOCKER_CODES]
    t.issue_count = len(page.items)
    t.issue_count_exact = page.next_cursor is None
    t.issues_ok = True


def _read_blockers(t: Today) -> None:
    panel = readmodels.production_blockers(t.at)
    t.blocker_count = len(panel.items)
    t.blockers_ok = True


SECTIONS: list[tuple[str, str]] = [
    ("visits", "today's visits"),
    ("robots", "the fleet"),
    ("issues", "the list of issues"),
    ("blockers", "production readiness"),
]


def build_today(at: datetime, user: Any) -> Today:
    roles = active_roles(user)
    day = local_day(at)
    t = Today(at=at, local_date=day, local_date_text=day.strftime("%a %-d %b"))
    readers: dict[str, Any] = {
        "visits": _read_visits,
        "robots": _read_robots,
        "issues": lambda tt: _read_issues(tt, roles),
        "blockers": _read_blockers,
    }
    for key, what in SECTIONS:
        try:
            with transaction.atomic():  # a savepoint: a failed statement cannot poison the snapshot
                readers[key](t)
        except Exception:
            log.exception("Today: could not load %s", what)
            t.notices.append(
                Notice(
                    f"error-{key}",
                    f"Could not load {what}. This section is not shown instead of showing a count that "
                    "might be wrong. Reload the page; if it keeps happening, tell engineering.",
                    "bad",
                )
            )
    _merge_cards(t, roles)
    _coverage_notice(t)
    return t


def _merge_cards(t: Today, roles: set[str]) -> None:
    cards = list(t.inbox_cards)
    for row, why, since in t.readiness_cards:
        cards.append(_readiness_card(row, why, since, roles))
    t.issue_count += len(t.readiness_cards)
    cards.sort(key=lambda c: (c["priority"], c["since"], c["code"], c["ref"]))
    t.more_cards = max(0, len(cards) - CARD_LIMIT)
    t.attention_total = len(cards)
    t.cards = cards[:CARD_LIMIT]


def _coverage_notice(t: Today) -> None:
    if not t.visits_ok:
        return
    cov = t.coverage
    if cov:
        t.notices.append(
            Notice(
                "coverage",
                f"Readiness checked for {cov['evaluated']} of {cov['qualifying']} visits. The rest show as "
                "Scheduled; open the visit to see its checklist.",
            )
        )
    if t.charge_off:
        t.notices.append(
            Notice(
                "charge",
                "The charge forecast is off: the battery freshness setting is not provisioned, so no visit "
                "is marked as needing charge. Departure still needs its battery check.",
                "info",
            )
        )
    if "ops_readiness_lookahead_min" in t.unconfigured:
        t.notices.append(
            Notice(
                "lookahead",
                "Readiness is not being checked because the look-ahead setting is missing. "
                "Visits show as Scheduled only.",
            )
        )

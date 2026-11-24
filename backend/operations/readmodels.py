"""Operations read models (Slice 5 Engineer C): fleet overview, timeline, incident queue.
docs/05_operations_command_center_contract.md sections 2, 4, 4a, 5, 6.4.

READ ONLY. Every function takes the snapshot time ``at`` explicitly and must run INSIDE
``operations.snapshot.read_snapshot()`` (REPEATABLE READ READ ONLY). Nothing here opens a transaction, takes
a lock, calls a command, ``nextval`` or ``set_config``. Each read model is built on Engineer A's SQL views
with a fixed number of statements, independent of fleet size:

    fleet_overview(at)                 1 statement  (operations_robot_status + X-3 periodic status per robot)
    production_blockers(at)            1 statement  (operations_production_blockers, Slice 7)
    incident_queue(at)                 1 statement  (operations_incident_queue)
    timeline(at, window_min=...)       3 statements + E per evaluated visit (at most 10; contract 4a)
                                       (thresholds, visits, open runs [+ one Visit load when evaluating])

``operations_robot_status`` computes the next confirmed visit with ``now()``, i.e. the snapshot's
``transaction_timestamp()``. ``fleet_overview`` therefore refuses an ``at`` that is not the snapshot time
instead of silently mixing two clocks.

Nothing here is departure authorization. Labels never say "ready", "cleared", "safe" or "available to deploy";
the readiness label on the timeline is the authoritative evaluator's own label, shown only for visits it
evaluated in this snapshot.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db import connection

from operations.exceptions import PERIODIC_STATUS_CTES

# ------------------------------------------------------------------ thresholds (fleet_deploymentsetting)
CUSTODY_VERIFY_GRACE = "ops_custody_verify_grace_min"
RETURN_GRACE = "ops_return_grace_min"
INSPECTION_GRACE = "ops_inspection_grace_min"
READINESS_LOOKAHEAD = "ops_readiness_lookahead_min"
THRESHOLD_KEYS = (CUSTODY_VERIFY_GRACE, RETURN_GRACE, INSPECTION_GRACE, READINESS_LOOKAHEAD)

READINESS_CAP = 10  # contract 4a: at most 10 evaluator runs per page
NOT_EVALUATED = "Not evaluated"

_POSITIVE_INT = re.compile(r"[0-9]+")


def parse_threshold(value: str | None) -> int | None:
    """A threshold is a positive integer number of minutes written as plain ASCII digits; anything else
    (missing, blank, signed, decimal, padded, zero) is not configured."""
    if value is None or not _POSITIVE_INT.fullmatch(value):
        return None
    minutes = int(value)
    return minutes if minutes > 0 else None


def read_thresholds() -> dict[str, int | None]:
    """The four time thresholds, read inside the caller's snapshot (one statement)."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT key, value FROM fleet_deploymentsetting WHERE key = ANY(%s)", [list(THRESHOLD_KEYS)]
        )
        raw = dict(cur.fetchall())
    return {k: parse_threshold(raw.get(k)) for k in THRESHOLD_KEYS}


def _readiness_since(on_site_start: datetime, lookahead: int | None) -> datetime:
    """READINESS_INCOMPLETE_SOON `since` (amendment A1): on_site_start - lookahead (clamped)."""
    if lookahead is None:
        return on_site_start
    try:
        return on_site_start - timedelta(minutes=lookahead)
    except OverflowError:
        return datetime.min.replace(tzinfo=on_site_start.tzinfo)


def _cutoff(at: datetime, minutes: int | None) -> datetime | None:
    """Latest `since` that fires at `at` (inclusive rule `at >= since + threshold`), or None when the rule
    cannot fire (threshold unconfigured, or so large that no representable time reaches it)."""
    if minutes is None:
        return None
    try:
        return at - timedelta(minutes=minutes)
    except OverflowError:
        return None


def _fires(since: datetime | None, cutoff: datetime | None) -> bool:
    return since is not None and cutoff is not None and since <= cutoff


def _rows(cur: Any) -> list[dict[str, Any]]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def robot_url(code: str) -> str:
    return f"/fleet/{code}/"


def run_url(code: str) -> str:
    return f"/execution/runs/{code}/"


def incident_url(code: str) -> str:
    return f"/execution/incidents/{code}/"


def readiness_url(visit_code: str) -> str:
    return f"/readiness/visits/{visit_code}/"


class SnapshotTimeMismatch(ValueError):
    """`at` is not the time of the snapshot the query runs in."""


# ------------------------------------------------------------------ fleet overview (contract section 2)
@dataclass(frozen=True)
class Custody:
    kind: str  # hub / vehicle / site / unaccounted / unknown
    verified: bool
    since: datetime | None  # recorded_at of the custody event (None: no event, e.g. pre-Slice-4 baseline)
    seq: int | None


@dataclass(frozen=True)
class OpenRun:
    code: str
    visit: str
    stage: str  # operations_open_run_stage.stage


@dataclass(frozen=True)
class NextVisit:
    code: str
    on_site_start: datetime


@dataclass(frozen=True)
class RobotStatus:
    robot: str
    robot_id: int
    lifecycle: str
    pattern: str | None
    manifest_id: int | None
    custody: Custody
    open_run: OpenRun | None
    holds: list[str]  # active blocking hold codes, robot and current-pattern scope, by id
    holds_since: datetime | None
    eligible_for_planning: bool
    eligibility_reasons: list[str]
    next_visit: NextVisit | None  # planning rows only; no readiness label on the overview (D3)
    awaiting: str  # post_visit_inspection / reconciliation / trip_closure / none
    status_rule: int  # contract section 2 order, 1..8
    status_label: str
    url: str
    # Slice 7 (X-3, contract 07a 4.6): readiness_periodic_inspection_status in the current mode at the
    # snapshot
    # (compare=False: a Slice 7 addition that Slice 5 equality checks of the robot status do not cover)
    periodic_inspection: "PeriodicInspection | None" = field(default=None, compare=False)


@dataclass(frozen=True)
class PeriodicInspection:
    """X-3 periodic inspection state of a robot (A's SQL function, the authoritative rule): unknown_interval /
    never_inspected / overdue / due. ``due_at`` only for overdue and due. Never a departure decision."""

    state: str
    due_at: datetime | None
    rule_version: int | None
    rule_source: str | None  # synthetic_demo / engineering_doc / None (no rule)
    rule_interval_min: int | None


PERIODIC_LABELS = {
    "unknown_interval": "Periodic inspection interval unknown",
    "never_inspected": "No qualifying periodic inspection on this configuration",
    "overdue": "Periodic inspection overdue",
    "due": "Periodic inspection due",
}

# The periodic status comes from the set-based operations.exceptions.PERIODIC_STATUS_CTES (oracle-tested
# against A's readiness_periodic_inspection_status), evaluated at %(at)s = the snapshot time.
FLEET_OVERVIEW_SQL = (
    """
WITH mode AS MATERIALIZED (SELECT fleet_production_mode() AS production),
"""
    # now() = transaction_timestamp() = the snapshot time (checked below); the statement takes no parameters
    + PERIODIC_STATUS_CTES.replace("%(at)s", "now()").rstrip().rstrip(",")
    + """
SELECT n.snapshot_now, s.*, ps.state AS periodic_state, ps.due_at AS periodic_due_at,
       pr.version_no AS periodic_rule_version, pr.source AS periodic_rule_source,
       pr.interval_min AS periodic_rule_interval_min
FROM (SELECT now() AS snapshot_now) n
LEFT JOIN operations_robot_status s ON true
LEFT JOIN pstat ps ON ps.robot_id = s.robot_id
LEFT JOIN fleet_periodicinspectionrule pr ON pr.id = ps.rule_id
ORDER BY s.robot_code
"""
)


def fleet_overview(at: datetime) -> list[RobotStatus]:
    """One row per non-retired robot, ordered by robot code (one statement)."""
    with connection.cursor() as cur:
        cur.execute(FLEET_OVERVIEW_SQL)
        rows = _rows(cur)
    if rows[0]["snapshot_now"] != at:
        raise SnapshotTimeMismatch(
            f"fleet_overview(at={at.isoformat()}) must run in the snapshot taken at that time "
            f"(this transaction's time is {rows[0]['snapshot_now'].isoformat()})"
        )
    out = []
    for r in rows:
        if r["robot_id"] is None:  # empty fleet: only the snapshot row
            continue
        out.append(
            RobotStatus(
                robot=r["robot_code"],
                robot_id=r["robot_id"],
                lifecycle=r["lifecycle"],
                pattern=r["pattern_code"],
                manifest_id=r["manifest_id"],
                custody=Custody(
                    kind=r["custody_kind"],
                    verified=bool(r["custody_verified"]),
                    since=r["custody_since"],
                    seq=r["custody_seq"],
                ),
                open_run=(
                    OpenRun(
                        code=r["open_run_code"], visit=r["open_run_visit_code"], stage=r["open_run_stage"]
                    )
                    if r["open_run_id"] is not None
                    else None
                ),
                holds=list(r["hold_codes"]),
                holds_since=r["holds_since"],
                eligible_for_planning=bool(r["eligible"]),
                eligibility_reasons=list(r["reasons"]),
                next_visit=(
                    NextVisit(code=r["next_visit_code"], on_site_start=r["next_visit_on_site_start"])
                    if r["next_visit_id"] is not None
                    else None
                ),
                awaiting=r["awaiting"],
                status_rule=r["status_rule"],
                status_label=r["status_label"],
                url=robot_url(r["robot_code"]),
                periodic_inspection=PeriodicInspection(
                    state=r["periodic_state"],
                    due_at=r["periodic_due_at"],
                    rule_version=r["periodic_rule_version"],
                    rule_source=r["periodic_rule_source"],
                    rule_interval_min=r["periodic_rule_interval_min"],
                ),
            )
        )
    return out


# ------------------------------------------------------------------ production blockers (contract 07a 5.6)
BLOCKER_LABELS = {
    "HOLD_POLICY_NOT_APPROVED": "Hold release policy not approved for production",
    "HOLD_UNRELEASABLE": "Hold unreleasable in production",
    "INSPECTION_TEMPLATE_NOT_APPROVED": "Inspection template not approved for pre-departure use",
    "PERIODIC_INTERVAL_NOT_APPROVED": "Periodic inspection interval not approved",
    "ESCALATION_DEADLINES_NOT_APPROVED": "Escalation deadlines not owner approved",
    "MAINTENANCE_RULE_NOT_APPROVED": "Maintenance rules not approved for production",
    "MAINTENANCE_GATE_INACTIVE": "Maintenance gate not yet activated",
}


@dataclass(frozen=True)
class ProductionBlocker:
    code: str
    entity_ref: str
    label: str
    detail: str
    url: str  # the page where the record (policy, hold, pattern, deadline set) is reviewed


@dataclass(frozen=True)
class BlockerPanel:
    """The production-blocker panel. Evaluated AS PRODUCTION in both modes (no setting hides an item). In demo
    mode each item is labelled "would block ... in production"; in production the same items are also
    exception
    inbox items (priority 1). An empty panel never means approved, cleared or ready."""

    production: bool
    items: list[ProductionBlocker]

    @property
    def heading(self) -> str:
        return "Production blockers" if self.production else "Would block in production"

    @property
    def note(self) -> str:
        if self.production:
            return (
                "These block production use now. Each needs a company decision recorded with its approvals; "
                "the platform never supplies one."
            )
        return (
            "These items block real production use. Each one needs a company decision recorded with its "
            "approvals. In this demo workspace they do not stop the demo from working."
        )


PRODUCTION_BLOCKERS_SQL = """
SELECT n.production, pb.code, pb.entity_ref, pb.detail, hb.robot_code, hb.pattern_code
FROM (SELECT fleet_production_mode() AS production) n
LEFT JOIN operations_production_blockers pb ON true
LEFT JOIN LATERAL (
  SELECT r.code AS robot_code, pp.code AS pattern_code FROM fleet_hold h
  LEFT JOIN fleet_robot r ON r.id = h.robot_id
  LEFT JOIN fleet_manifestpattern pp ON pp.id = h.pattern_id
  WHERE pb.code = 'HOLD_UNRELEASABLE' AND h.code = pb.entity_ref
) hb ON true
ORDER BY pb.code COLLATE "C", pb.entity_ref COLLATE "C"
"""


def _blocker_url(code: str, ref: str, robot: str | None, pattern: str | None) -> str:
    if code == "HOLD_POLICY_NOT_APPROVED":
        return "/fleet/hold-policies/"
    if code == "ESCALATION_DEADLINES_NOT_APPROVED":
        return "/execution/escalation-deadlines/"
    if code == "MAINTENANCE_GATE_INACTIVE":
        return "/settings/operating/"
    if code == "HOLD_UNRELEASABLE":
        return robot_url(robot) if robot else f"/fleet/patterns/{pattern}/"
    return f"/fleet/patterns/{ref}/"


def production_blockers(at: datetime) -> BlockerPanel:
    """Every row of operations_production_blockers (evaluated as production whatever demo_mode says),
    ordered by
    (code, entity_ref), plus the current mode (one statement, inside the caller's snapshot)."""
    with connection.cursor() as cur:
        cur.execute(PRODUCTION_BLOCKERS_SQL)
        rows = cur.fetchall()
    production = bool(rows[0][0])
    items = [
        ProductionBlocker(
            code=code,
            entity_ref=ref,
            label=BLOCKER_LABELS.get(code, code),
            detail=detail,
            url=_blocker_url(code, ref, robot, pattern),
        )
        for _, code, ref, detail, robot, pattern in rows
        if code is not None
    ]
    return BlockerPanel(production=production, items=items)


# ------------------------------------------------------------------ incident queue (contract section 4, D1)
@dataclass(frozen=True)
class QueuedIncident:
    code: str
    robot: str
    run: str | None
    visit: str | None
    event_type: str
    occurred_at: datetime
    reported_at: datetime
    age: timedelta  # at - reported_at
    status: str
    unclassified: bool  # no severity decision
    severity_level: str | None
    severity_rank: int | None  # rank in the latest decision's policy scale (higher = more severe)
    hold: str
    hold_active: bool
    unacknowledged_escalations: int
    unacknowledged_escalation_ids: list[int]
    latest_unacknowledged_at: datetime | None
    suggested_owner_role: str  # "Suggested follow-up owner": a suggestion, not a formal assignment
    suggested_owner_person: str | None
    url: str


# Contract section 4: unclassified first, then severity rank (most severe first), then age (oldest first),
# then code. A decision whose level has no rank in its policy is unknown, so it ranks before every ranked
# one (NULLS FIRST): unknown stays visible.
INCIDENT_QUEUE_SQL = """
SELECT q.*, x.code AS run_code, v.code AS visit_code
FROM operations_incident_queue q
LEFT JOIN execution_executionrun x ON x.id = q.run_id
LEFT JOIN planning_visit v ON v.id = q.visit_id
ORDER BY q.unclassified DESC, q.severity_rank DESC NULLS FIRST, q.reported_at, q.incident_code
"""


def incident_queue(at: datetime) -> list[QueuedIncident]:
    """Open incidents (status not closed), in queue order (one statement)."""
    with connection.cursor() as cur:
        cur.execute(INCIDENT_QUEUE_SQL)
        rows = _rows(cur)
    return [
        QueuedIncident(
            code=r["incident_code"],
            robot=r["robot_code"],
            run=r["run_code"],
            visit=r["visit_code"],
            event_type=r["event_type"],
            occurred_at=r["occurred_at"],
            reported_at=r["reported_at"],
            age=at - r["reported_at"],
            status=r["status"],
            unclassified=bool(r["unclassified"]),
            severity_level=r["severity_level"],
            severity_rank=r["severity_rank"],
            hold=r["hold_code"],
            hold_active=bool(r["hold_active"]),
            unacknowledged_escalations=r["unacknowledged_count"],
            unacknowledged_escalation_ids=list(r["unacknowledged_ids"]),
            latest_unacknowledged_at=r["latest_unacknowledged_at"],
            suggested_owner_role=r["suggested_owner_role"],
            suggested_owner_person=r["suggested_owner_person_code"],
            url=incident_url(r["incident_code"]),
        )
        for r in rows
    ]


# ------------------------------------------------------------------ timeline (contract sections 4, 4a)
@dataclass(frozen=True)
class Flag:
    """An item flagged with a section 3 exception code (same rule, threshold and inclusive boundary)."""

    code: str
    entity_ref: str
    since: datetime


@dataclass(frozen=True)
class TimelineVisit:
    visit: str
    visit_id: int
    robot: str | None
    on_site_start: datetime
    on_site_end: datetime
    in_lookahead: bool  # on-site start within ops_readiness_lookahead_min (a readiness-qualifying visit)
    readiness_evaluated: bool
    readiness_label: str  # the evaluator's label, or "Not evaluated"
    readiness_blockers: list[str]  # blocker codes from the evaluator (empty when not evaluated)
    hold_codes: list[str]
    flags: list[Flag]
    url: str
    # v2 02C: the planned robot's blocking maintenance reasons (fleet_maintenance_status_set), only while the
    # maintenance gate is active; read in the same statement as the visits
    maintenance_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TimelineRun:
    run: str
    robot: str
    visit: str
    departed_at: datetime
    on_site_start: datetime
    on_site_end: datetime
    planned_unloading_end: datetime | None  # None: no unloading reservation, the deadline is unknown
    return_deadline: (
        datetime | None
    )  # planned unloading end + ops_return_grace_min; None when either is unknown
    arrived_hub_at: datetime | None
    stage: str  # departed / at_site / returning / at_hub / awaiting_reconciliation / awaiting_closure
    session_state: str
    custody_kind: str
    custody_verified: bool
    awaiting: str
    latest_reconciliation: str | None
    flags: list[Flag]
    url: str


@dataclass(frozen=True)
class Timeline:
    at: datetime
    window_min: int
    window_end: datetime
    visits: list[TimelineVisit]
    runs: list[TimelineRun]
    readiness_coverage: dict[str, Any]  # contract 4a: qualifying / evaluated / not_evaluated / complete
    unconfigured_thresholds: list[str]  # keys whose rule could not run (THRESHOLD_NOT_CONFIGURED)


# Confirmed, undeparted visits (current revision) still ahead of or in progress at `at`, starting before the
# window end. Hold codes: active blocking holds on the planned robot or its current pattern (by id).
TIMELINE_VISITS_SQL = """
WITH cand AS MATERIALIZED (
  SELECT v.id, v.code, rv.id AS rev_id, rv.robot_id
  FROM planning_visit v JOIN planning_visitrevision rv ON rv.id = v.current_revision_id
  WHERE v.status = 'confirmed'
    AND NOT EXISTS (SELECT 1 FROM readiness_departureevent d WHERE d.visit_id = v.id)
    AND rv.on_site_end > %(at)s
    AND rv.on_site_start < %(window_end)s
),
-- v2 02C: blocking maintenance of the planned robots, once (set-based), only while the gate is active
mnt AS MATERIALIZED (
  SELECT s.robot_id, array_agg(s.reason ORDER BY s.requirement_code NULLS FIRST) AS reasons
  FROM (SELECT 1 WHERE fleet_maintenance_gate_active()) g
  CROSS JOIN LATERAL fleet_maintenance_status_set(
    ARRAY(SELECT DISTINCT robot_id FROM cand WHERE robot_id IS NOT NULL), fleet_production_mode(), %(at)s) s
  WHERE s.blocking
  GROUP BY s.robot_id
)
SELECT v.id AS visit_id, v.code AS visit_code, rv.on_site_start, rv.on_site_end, rb.code AS robot_code,
       coalesce((SELECT mnt.reasons FROM mnt WHERE mnt.robot_id = rv.robot_id), '{}') AS maintenance_reasons,
       ARRAY(SELECT h.code::text FROM fleet_hold h
             WHERE h.blocking AND h.released_at IS NULL
               AND ((h.scope = 'robot' AND h.robot_id = rv.robot_id)
                    OR (h.scope = 'manifest_pattern' AND h.pattern_id = m.pattern_id))
             ORDER BY h.id) AS hold_codes,
       (SELECT min(h.placed_at) FROM fleet_hold h
        WHERE h.blocking AND h.released_at IS NULL
          AND ((h.scope = 'robot' AND h.robot_id = rv.robot_id)
               OR (h.scope = 'manifest_pattern' AND h.pattern_id = m.pattern_id))) AS holds_since
FROM planning_visit v
JOIN planning_visitrevision rv ON rv.id = v.current_revision_id
LEFT JOIN fleet_robot rb ON rb.id = rv.robot_id
LEFT JOIN fleet_configurationmanifest m ON m.id = rb.current_manifest_id
WHERE v.status = 'confirmed'
  AND NOT EXISTS (SELECT 1 FROM readiness_departureevent d WHERE d.visit_id = v.id)
  AND rv.on_site_end > %(at)s
  AND rv.on_site_start < %(window_end)s
ORDER BY rv.on_site_start, v.id
"""

# Every open run (operations_open_run_stage) with what its flags need.
TIMELINE_RUNS_SQL = """
SELECT s.run_id, s.run_code, s.robot_code, s.visit_code, s.departed_at, s.on_site_start, s.on_site_end,
       s.planned_unloading_end, s.arrived_hub_at, s.stage, s.session_state, s.custody_kind,
       s.custody_verified,
       s.awaiting, s.latest_reconciliation_outcome, s.latest_reconciliation_at,
       ce.recorded_at AS custody_since,
       greatest(x.recorded_at, (SELECT max(e.recorded_at) FROM execution_custodyevent e
                                WHERE e.robot_id = s.robot_id)) AS latest_custody_or_run_at,
       dep.unverified AS departure_unverified,
       sc.result AS latest_site_check_result, sc.recorded_at AS latest_site_check_at,
       oi.codes AS open_interruption_codes, oi.times AS open_interruption_times
FROM operations_open_run_stage s
JOIN execution_executionrun x ON x.id = s.run_id
LEFT JOIN execution_custodyevent ce ON ce.id = s.custody_event_id
LEFT JOIN LATERAL (
  SELECT NOT EXISTS (SELECT 1 FROM execution_custodyverification cv WHERE cv.custody_event_id = e.id)
           AS unverified
  FROM execution_custodyevent e
  WHERE e.run_id = s.run_id AND e.basis = 'administrative' AND e.from_kind = 'hub' AND e.to_kind = 'vehicle'
  ORDER BY e.seq LIMIT 1
) dep ON true
LEFT JOIN LATERAL (
  SELECT k.result, k.recorded_at FROM execution_sitecheck k
  WHERE k.run_id = s.run_id ORDER BY k.recorded_at DESC, k.id DESC LIMIT 1
) sc ON true
CROSS JOIN LATERAL (
  SELECT coalesce(array_agg(i.code::text ORDER BY i.recorded_at, i.code), '{}') AS codes,
         coalesce(array_agg(i.recorded_at ORDER BY i.recorded_at, i.code), '{}') AS times
  FROM execution_interruption i
  WHERE i.run_id = s.run_id
    AND NOT EXISTS (SELECT 1 FROM execution_interruptionresolution z WHERE z.interruption_id = i.id)
) oi
ORDER BY s.departed_at, s.run_code
"""


def _deadline(planned_end: datetime | None, grace: int | None) -> datetime | None:
    if planned_end is None or grace is None:
        return None
    try:
        return planned_end + timedelta(minutes=grace)
    except OverflowError:
        return None


def _run_flags(r: dict[str, Any], at: datetime, th: dict[str, int | None]) -> list[Flag]:
    run = r["run_code"]
    flags: list[Flag] = []
    if r["custody_kind"] == "unaccounted":
        flags.append(Flag("ROBOT_UNACCOUNTED", r["robot_code"], r["custody_since"]))
    if r["custody_kind"] == "unknown":
        flags.append(Flag("CUSTODY_UNKNOWN", r["robot_code"], r["latest_custody_or_run_at"]))
    for code, since in zip(r["open_interruption_codes"], r["open_interruption_times"], strict=True):
        flags.append(Flag("INTERRUPTION_OPEN", code, since))
    if r["latest_site_check_result"] == "no_go":
        flags.append(Flag("SITE_NO_GO", run, r["latest_site_check_at"]))
    if r["departure_unverified"] and _fires(r["departed_at"], _cutoff(at, th[CUSTODY_VERIFY_GRACE])):
        flags.append(Flag("CUSTODY_UNVERIFIED", run, r["departed_at"]))
    if r["arrived_hub_at"] is None and _fires(r["planned_unloading_end"], _cutoff(at, th[RETURN_GRACE])):
        flags.append(Flag("RETURN_OVERDUE", run, r["planned_unloading_end"]))
    if r["arrived_hub_at"] is None and r["planned_unloading_end"] is None:
        # EM decision: never invent a deadline; the run shows it as unknown (since = its departure)
        flags.append(Flag("RETURN_DEADLINE_UNKNOWN", run, r["departed_at"]))
    if r["latest_reconciliation_outcome"] == "unresolved":
        flags.append(Flag("RECONCILIATION_UNRESOLVED", run, r["latest_reconciliation_at"]))
    if r["awaiting"] == "post_visit_inspection" and _fires(
        r["custody_since"], _cutoff(at, th[INSPECTION_GRACE])
    ):
        flags.append(Flag("POST_VISIT_INSPECTION_MISSING", run, r["custody_since"]))
    return flags


def timeline(at: datetime, *, window_min: int) -> Timeline:
    """Upcoming confirmed visits whose on-site start is before ``at + window_min`` (and whose on-site end is
    after ``at``), ordered by (on-site start, visit id), plus every open run ordered by (departure, run code),
    each flagged with the section 3 codes that apply to it.

    Readiness (contract 4a): a visit qualifies when its on-site start is at most
    ``ops_readiness_lookahead_min`` after ``at``. The authoritative evaluator runs for the first 10
    qualifying visits INSIDE THIS WINDOW, in (on-site start, visit id) order, and every other visit shows
    "Not evaluated". When the window is shorter than the lookahead this is NOT the inbox's selection (the
    inbox draws from every qualifying visit in the lookahead), so coverage is window-scoped and labelled so
    on the page/API (``readiness_coverage_scope``). It is complete only when every qualifying visit of the
    window was evaluated (never when the lookahead threshold is unconfigured)."""
    if isinstance(window_min, bool) or not isinstance(window_min, int) or window_min <= 0:
        raise ValueError("window_min must be a positive integer number of minutes")
    from planning.models import Visit
    from readiness.evaluator import evaluate_readiness

    th = read_thresholds()
    window_end = at + timedelta(minutes=window_min)
    lookahead = th[READINESS_LOOKAHEAD]
    try:
        lookahead_end = at + timedelta(minutes=lookahead) if lookahead is not None else None
    except OverflowError:
        lookahead_end = datetime.max.replace(tzinfo=at.tzinfo)

    with connection.cursor() as cur:
        cur.execute(TIMELINE_VISITS_SQL, {"at": at, "window_end": window_end})
        visit_rows = _rows(cur)
        cur.execute(TIMELINE_RUNS_SQL)
        run_rows = _rows(cur)

    qualifying = [r for r in visit_rows if lookahead_end is not None and r["on_site_start"] <= lookahead_end]
    chosen = [r["visit_id"] for r in qualifying[:READINESS_CAP]]
    results: dict[int, Any] = {}
    if chosen:
        objs = Visit.objects.in_bulk(chosen)
        for vid in chosen:
            results[vid] = evaluate_readiness(objs[vid], at=at)

    qualifying_ids = {r["visit_id"] for r in qualifying}
    # Contract amendment A1: the rule's `since` is the visit's on_site_start - lookahead (same as the inbox).
    visits = []
    for r in visit_rows:
        res = results.get(r["visit_id"])
        flags: list[Flag] = []
        if r["hold_codes"]:
            flags.append(
                Flag("HELD_ROBOT_ASSIGNED", f"{r['robot_code']}/{r['visit_code']}", r["holds_since"])
            )
        if res is not None and not res.ready:
            flags.append(
                Flag(
                    "READINESS_INCOMPLETE_SOON",
                    r["visit_code"],
                    _readiness_since(r["on_site_start"], lookahead),
                )
            )
        visits.append(
            TimelineVisit(
                visit=r["visit_code"],
                visit_id=r["visit_id"],
                robot=r["robot_code"],
                on_site_start=r["on_site_start"],
                on_site_end=r["on_site_end"],
                in_lookahead=r["visit_id"] in qualifying_ids,
                readiness_evaluated=res is not None,
                readiness_label=res.label if res is not None else NOT_EVALUATED,
                readiness_blockers=[b["code"] for b in res.blockers] if res is not None else [],
                hold_codes=list(r["hold_codes"]),
                flags=flags,
                url=readiness_url(r["visit_code"]),
                maintenance_reasons=list(r["maintenance_reasons"]),
            )
        )

    runs = [
        TimelineRun(
            run=r["run_code"],
            robot=r["robot_code"],
            visit=r["visit_code"],
            departed_at=r["departed_at"],
            on_site_start=r["on_site_start"],
            on_site_end=r["on_site_end"],
            planned_unloading_end=r["planned_unloading_end"],
            return_deadline=_deadline(r["planned_unloading_end"], th[RETURN_GRACE]),
            arrived_hub_at=r["arrived_hub_at"],
            stage=r["stage"],
            session_state=r["session_state"],
            custody_kind=r["custody_kind"],
            custody_verified=bool(r["custody_verified"]),
            awaiting=r["awaiting"],
            latest_reconciliation=r["latest_reconciliation_outcome"],
            flags=_run_flags(r, at, th),
            url=run_url(r["run_code"]),
        )
        for r in run_rows
    ]

    n, done = len(qualifying), len(chosen)
    coverage = {
        "qualifying": n,
        "evaluated": done,
        "not_evaluated": n - done,
        "complete": lookahead is not None and done == n,
        "skipped": False,  # same shape as the inbox's coverage (amendment A1); the timeline always evaluates
    }
    return Timeline(
        at=at,
        window_min=window_min,
        window_end=window_end,
        visits=visits,
        runs=runs,
        readiness_coverage=coverage,
        unconfigured_thresholds=[k for k in THRESHOLD_KEYS if th[k] is None],
    )


__all__ = [
    "BLOCKER_LABELS",
    "PERIODIC_LABELS",
    "THRESHOLD_KEYS",
    "BlockerPanel",
    "PeriodicInspection",
    "ProductionBlocker",
    "Custody",
    "Flag",
    "NextVisit",
    "OpenRun",
    "QueuedIncident",
    "RobotStatus",
    "SnapshotTimeMismatch",
    "Timeline",
    "TimelineRun",
    "TimelineVisit",
    "fleet_overview",
    "incident_queue",
    "parse_threshold",
    "production_blockers",
    "read_thresholds",
    "timeline",
]

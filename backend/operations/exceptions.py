"""Exception inbox (Slice 5, Engineer B). docs/05_operations_command_center_contract.md sections 3 and 4a.

``open_exceptions(at)`` lists what needs attention across the fleet at the snapshot time ``at``. Everything is
derived at read time from the existing append-only rows and Engineer A's read-only views; nothing is stored or
written. SYNTHETIC data only. Priority is an operational triage order, not a safety severity, and no exception
(or its absence) means departure authorization.

RUNS INSIDE THE PAGE SNAPSHOT. The caller opens ``operations.snapshot.read_snapshot()`` and passes its time as
``at``. This module never opens a transaction, never locks, never calls a command, ``nextval`` or
``set_config``; every time comparison uses the explicit ``at`` (never ``now()``).

STATEMENTS (contract 6.4). Constant in fleet size:
    1  thresholds (fleet_deploymentsetting)
    1  every SQL-derived rule, ordered and keyset-filtered in SQL
    1  rules whose basis is unknown (unknown_basis, EM B2)
    1  qualifying readiness visits (first READINESS_CAP + the total count), only when the lookahead is valid
    1  the evaluated Visit rows, only when at least one visit qualifies
  + at most READINESS_CAP x E for ``readiness.evaluator.evaluate_readiness``
    (E = READINESS_EVALUATOR_STATEMENTS, pinned by a test), so the total stays within 12 + 10 x E.

ORDERING (identical in SQL and Python): ``(priority, since, code, entity_ref)`` ascending; text compared by
code point (SQL ``COLLATE "C"`` = Python ``str`` order for UTF-8; the database collation is NOT used).
``(code, entity_ref)`` is unique, so the order is total and keyset pagination (``after`` = the last item's
``sort_key``) is exact.

TIME RULES. A threshold is valid when its value is a whole number of minutes from 1 to MAX_THRESHOLD_MIN,
written with ASCII digits only. A missing or invalid key disables only its own rule and produces exactly one
THRESHOLD_NOT_CONFIGURED (``since`` = ``at``). Every time-based rule fires when ``at >= since + threshold``
(inclusive), except READINESS_INCOMPLETE_SOON whose ``since`` is the moment the visit entered the lookahead
(``on_site_start - lookahead``) and which qualifies when ``at >= since``.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import connection

from planning.models import Visit
from readiness.evaluator import LABEL_READY, evaluate_readiness

# ------------------------------------------------------------------ constants
THRESHOLD_RULES: dict[str, str] = {  # setting key -> the rule it enables
    "ops_custody_verify_grace_min": "CUSTODY_UNVERIFIED",
    "ops_return_grace_min": "RETURN_OVERDUE",
    "ops_inspection_grace_min": "POST_VISIT_INSPECTION_MISSING",
    "ops_readiness_lookahead_min": "READINESS_INCOMPLETE_SOON",
}
MAX_THRESHOLD_MIN = 10 * 525_600  # ten years; larger values are refused as not configured (no date overflow)
READINESS_CAP = 10  # contract 4a: at most 10 evaluator runs per snapshot
# E: statements one evaluate_readiness call may run, pinned by tests on the scale fixture (measured 60-69 per
# visit at e61ab31). Raise it only through the integration lead: it bounds the 12 + 10 x E budget (= 712).
READINESS_EVALUATOR_STATEMENTS = 70

PRIORITY: dict[str, int] = {
    "ROBOT_UNACCOUNTED": 1,
    "CUSTODY_UNKNOWN": 1,
    "THRESHOLD_NOT_CONFIGURED": 1,
    "INCIDENT_UNTRIAGED": 2,
    "ESCALATION_UNACKNOWLEDGED": 2,
    "INTERRUPTION_OPEN": 2,
    "RETURN_OVERDUE": 2,
    "HELD_ROBOT_ASSIGNED": 2,
    "SEVERITY_UNCLASSIFIED": 3,
    "SITE_NO_GO": 3,
    "CUSTODY_UNVERIFIED": 3,
    "RECONCILIATION_UNRESOLVED": 3,
    "POST_VISIT_INSPECTION_MISSING": 3,
    "READINESS_INCOMPLETE_SOON": 3,
}
CODES = tuple(sorted(PRIORITY))

# Resolution-path table (contract section 3, R6), column "always resolvable?": only an unconditional "yes"
# (possibly "yes, when/while ...") is True; "no", "not always" and "outside the app" are False.
RESOLVABLE: dict[str, bool] = {
    "ROBOT_UNACCOUNTED": False,
    "CUSTODY_UNKNOWN": False,
    "INCIDENT_UNTRIAGED": True,
    "ESCALATION_UNACKNOWLEDGED": True,
    "SEVERITY_UNCLASSIFIED": True,
    "INTERRUPTION_OPEN": False,
    "SITE_NO_GO": False,
    "CUSTODY_UNVERIFIED": True,
    "RETURN_OVERDUE": True,
    "RECONCILIATION_UNRESOLVED": False,
    "POST_VISIT_INSPECTION_MISSING": True,
    "HELD_ROBOT_ASSIGNED": False,
    "READINESS_INCOMPLETE_SOON": False,
    "THRESHOLD_NOT_CONFIGURED": False,
}

SortKey = tuple[int, datetime, str, str]  # (priority, since, code, entity_ref)


@dataclass(frozen=True)
class OpsException:
    code: str
    priority: int
    entity_type: str  # robot | incident | escalation | interruption | run | robot_visit | visit | setting
    entity_ref: str  # the single underlying row (contract section 3 entity_ref table)
    robot: str | None  # robot code
    visit: str | None  # visit code
    since: datetime
    owner_role: str
    message: str
    action_url: str | None  # existing page where it is addressed; None when there is none in the app
    resolvable: bool  # resolution-path table, "always resolvable?"
    # extra facts for the reader; ESCALATION_UNACKNOWLEDGED: {"incident", "incident_status"} (EM B1: an
    # unacknowledged escalation stays listed after its incident is closed; closure is not acknowledgment)
    context: dict[str, Any] = field(default_factory=dict, compare=True)

    @property
    def sort_key(self) -> SortKey:
        return (self.priority, self.since, self.code, self.entity_ref)


@dataclass(frozen=True)
class ExceptionPage:
    items: list[OpsException]
    readiness_coverage: dict[str, Any]  # {"qualifying", "evaluated", "not_evaluated", "complete", "skipped"}
    next_cursor: SortKey | None  # sort_key of the last item when more items follow, else None
    # rules that could not be computed for an entity (EM B2), e.g. {"rule": "RETURN_OVERDUE", "run": code,
    # "reason": "planned unloading end unknown"}; page-independent, ordered by rule then run code
    unknown_basis: list[dict[str, str]] = field(default_factory=list)


# ------------------------------------------------------------------ thresholds
_DIGITS = re.compile(r"[0-9]+")


def parse_threshold(value: str | None) -> int | None:
    """Minutes, or None when missing or not a positive whole number in [1, MAX_THRESHOLD_MIN]."""
    if value is None or not _DIGITS.fullmatch(value):
        return None
    n = int(value)
    return n if 1 <= n <= MAX_THRESHOLD_MIN else None


def read_thresholds() -> tuple[dict[str, int | None], set[str]]:
    """The four time thresholds as read in the current snapshot (one statement): ({key: minutes or None},
    the keys that are missing altogether)."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT key, value FROM fleet_deploymentsetting WHERE key = ANY(%s)", [list(THRESHOLD_RULES)]
        )
        raw = dict(cur.fetchall())
    return {key: parse_threshold(raw.get(key)) for key in THRESHOLD_RULES}, set(THRESHOLD_RULES) - set(raw)


# ------------------------------------------------------------------ the SQL-derived rules
# One statement. Every branch yields (code, entity_type, entity_ref, robot, visit, since, owner_role, message,
# action_url); priority comes from PRIORITY; NULL thresholds switch their branch off. Time uses %(at)s only.
RULES_SQL = r"""
WITH rs AS MATERIALIZED (SELECT * FROM operations_robot_status),
ors AS MATERIALIZED (SELECT * FROM operations_open_run_stage),
q AS MATERIALIZED (SELECT * FROM operations_incident_queue),
ex AS (
  -- ROBOT_UNACCOUNTED: custody unaccounted; since = that custody event
  SELECT 'ROBOT_UNACCOUNTED' AS code, 'robot' AS entity_type, rs.robot_code AS entity_ref,
         rs.robot_code AS robot, rs.open_run_visit_code AS visit, rs.custody_since AS since,
         'ops_lead' AS owner_role,
         'Robot ' || rs.robot_code || ' is unaccounted for'
           || coalesce(' (incident ' || ic.code || ')', '') AS message,
         coalesce('/execution/runs/' || rs.open_run_code || '/', '/execution/incidents/' || ic.code || '/',
                  '/fleet/' || rs.robot_code || '/') AS action_url,
         NULL::jsonb AS context
  FROM rs
  LEFT JOIN execution_custodyevent ce ON ce.id = rs.custody_event_id
  LEFT JOIN execution_incident ic ON ic.id = ce.incident_id
  WHERE rs.custody_kind = 'unaccounted'

  UNION ALL
  -- CUSTODY_UNKNOWN: custody function says unknown; since = latest custody / run row of the robot
  SELECT 'CUSTODY_UNKNOWN', 'robot', rs.robot_code, rs.robot_code, rs.open_run_visit_code,
         greatest((SELECT max(e.recorded_at) FROM execution_custodyevent e WHERE e.robot_id = rs.robot_id),
                  (SELECT max(x.recorded_at) FROM execution_executionrun x WHERE x.robot_id = rs.robot_id)),
         'ops_lead',
         'Custody of robot ' || rs.robot_code || ' cannot be derived from its records (data integrity issue; '
           || 'escalate to engineering)',
         '/fleet/' || rs.robot_code || '/', NULL::jsonb
  FROM rs
  WHERE rs.custody_kind = 'unknown'

  UNION ALL
  -- INCIDENT_UNTRIAGED: still reported
  SELECT 'INCIDENT_UNTRIAGED', 'incident', q.incident_code, q.robot_code, v.code, q.reported_at, 'ops_lead',
         'Incident ' || q.incident_code || ' (' || q.event_type || ', robot ' || q.robot_code
           || ') has not been triaged',
         '/execution/incidents/' || q.incident_code || '/', NULL::jsonb
  FROM q LEFT JOIN planning_visit v ON v.id = q.visit_id
  WHERE q.status = 'reported'

  UNION ALL
  -- SEVERITY_UNCLASSIFIED: open incident without a severity decision while an active incident policy exists
  SELECT 'SEVERITY_UNCLASSIFIED', 'incident', q.incident_code, q.robot_code, v.code, q.reported_at,
         'safety_reviewer',
         'Incident ' || q.incident_code || ' (robot ' || q.robot_code || ') has no severity decision',
         '/execution/incidents/' || q.incident_code || '/', NULL::jsonb
  FROM q LEFT JOIN planning_visit v ON v.id = q.visit_id
  WHERE q.unclassified AND execution_active_incident_policy_id() IS NOT NULL

  UNION ALL
  -- ESCALATION_UNACKNOWLEDGED: one per escalation without an acknowledgment; owner = its to_role
  SELECT 'ESCALATION_UNACKNOWLEDGED', 'escalation', 'ESC-' || e.id, rb.code, v.code, e.recorded_at,
         e.to_role::text,
         'Escalation ESC-' || e.id || ' of incident ' || i.code || ' to ' || e.to_role
           || coalesce(' (' || pp.code || ')', '') || ' is not acknowledged (incident ' || st.status || ')',
         '/execution/incidents/' || i.code || '/',
         jsonb_build_object('incident', i.code, 'incident_status', st.status)
  FROM execution_incidentescalation e
  JOIN execution_incident i ON i.id = e.incident_id
  CROSS JOIN LATERAL (SELECT execution_incident_status(i.id) AS status) st
  JOIN fleet_robot rb ON rb.id = i.robot_id
  LEFT JOIN planning_visit v ON v.id = i.visit_id
  LEFT JOIN planning_person pp ON pp.id = e.to_person_id
  WHERE NOT EXISTS (SELECT 1 FROM execution_escalationack k WHERE k.escalation_id = e.id)

  UNION ALL
  -- INTERRUPTION_OPEN: interruption without resolution on an open run
  SELECT 'INTERRUPTION_OPEN', 'interruption', n.code, ors.robot_code, ors.visit_code, n.recorded_at,
         'ops_lead',
         'Interruption ' || n.code || ' on run ' || ors.run_code || ' is not resolved'
           || CASE WHEN n.incident_id IS NOT NULL THEN ' (incident hold workflow applies)' ELSE '' END,
         '/execution/runs/' || ors.run_code || '/', NULL::jsonb
  FROM execution_interruption n
  JOIN ors ON ors.run_id = n.run_id
  WHERE NOT EXISTS (SELECT 1 FROM execution_interruptionresolution s WHERE s.interruption_id = n.id)

  UNION ALL
  -- SITE_NO_GO: the latest site check of an open run is no_go
  SELECT 'SITE_NO_GO', 'run', ors.run_code, ors.robot_code, ors.visit_code, sc.recorded_at, 'ops_lead',
         'Latest site check of run ' || ors.run_code || ' (visit ' || ors.visit_code || ') is No-Go',
         '/execution/runs/' || ors.run_code || '/', NULL::jsonb
  FROM ors
  CROSS JOIN LATERAL (
    SELECT c.result, c.recorded_at FROM execution_sitecheck c WHERE c.run_id = ors.run_id
    ORDER BY c.recorded_at DESC, c.id DESC LIMIT 1
  ) sc
  WHERE sc.result = 'no_go'

  UNION ALL
  -- CUSTODY_UNVERIFIED: departure assignment of an open run not verified, at >= departed_at + grace
  SELECT 'CUSTODY_UNVERIFIED', 'run', ors.run_code, ors.robot_code, ors.visit_code, ors.departed_at,
         'field_operator',
         'Departure custody assignment of run ' || ors.run_code || ' (robot ' || ors.robot_code
           || ') is not verified by the crew',
         '/execution/runs/' || ors.run_code || '/', NULL::jsonb
  FROM ors
  WHERE %(g_verify)s::int IS NOT NULL
    AND ors.departed_at + make_interval(mins => %(g_verify)s::int) <= %(at)s
    AND EXISTS (SELECT 1 FROM execution_custodyevent d
                WHERE d.run_id = ors.run_id AND d.basis = 'administrative'
                  AND NOT EXISTS (SELECT 1 FROM execution_custodyverification cv
                                  WHERE cv.custody_event_id = d.id))

  UNION ALL
  -- RETURN_OVERDUE: open run, no arrived_hub, at >= planned unloading end + grace
  SELECT 'RETURN_OVERDUE', 'run', ors.run_code, ors.robot_code, ors.visit_code, ors.planned_unloading_end,
         'ops_lead',
         'Run ' || ors.run_code || ' (robot ' || ors.robot_code || ') has not arrived at the hub after its '
           || 'planned unloading end',
         '/execution/runs/' || ors.run_code || '/', NULL::jsonb
  FROM ors
  WHERE %(g_return)s::int IS NOT NULL AND ors.arrived_hub_at IS NULL
    AND ors.planned_unloading_end + make_interval(mins => %(g_return)s::int) <= %(at)s

  UNION ALL
  -- RECONCILIATION_UNRESOLVED: latest reconciliation of an open run is unresolved
  SELECT 'RECONCILIATION_UNRESOLVED', 'run', ors.run_code, ors.robot_code, ors.visit_code,
         ors.latest_reconciliation_at, 'ops_lead',
         'Latest return reconciliation of run ' || ors.run_code || ' is unresolved',
         '/execution/runs/' || ors.run_code || '/', NULL::jsonb
  FROM ors
  WHERE ors.latest_reconciliation_outcome = 'unresolved'

  UNION ALL
  -- POST_VISIT_INSPECTION_MISSING: hub receipt on an open run, no inspection after it, at >= receipt + grace
  SELECT 'POST_VISIT_INSPECTION_MISSING', 'run', ors.run_code, ors.robot_code, ors.visit_code, hr.recorded_at,
         'field_operator',
         'Robot ' || ors.robot_code || ' is back at the hub (run ' || ors.run_code
           || ') without a post-visit inspection',
         '/fleet/' || ors.robot_code || '/', NULL::jsonb
  FROM ors
  JOIN execution_custodyevent hr ON hr.id = ors.custody_event_id
  WHERE %(g_inspect)s::int IS NOT NULL AND ors.awaiting = 'post_visit_inspection'
    AND hr.recorded_at + make_interval(mins => %(g_inspect)s::int) <= %(at)s

  UNION ALL
  -- HELD_ROBOT_ASSIGNED: one per held robot and confirmed upcoming visit; since = earliest active hold placed
  SELECT 'HELD_ROBOT_ASSIGNED', 'robot_visit', rs.robot_code || '/' || uv.code, rs.robot_code, uv.code,
         rs.holds_since, 'ops_lead',
         'Robot ' || rs.robot_code || ' has an active blocking hold (' || array_to_string(rs.hold_codes, ', ')
           || ') and is on confirmed visit ' || uv.code,
         '/fleet/' || rs.robot_code || '/', NULL::jsonb
  FROM rs
  JOIN LATERAL (
    SELECT v.code FROM planning_visitrevision rv
    JOIN planning_visit v ON v.current_revision_id = rv.id
    WHERE rv.robot_id = rs.robot_id AND v.status = 'confirmed' AND rv.on_site_end > %(at)s
      AND NOT EXISTS (SELECT 1 FROM readiness_departureevent dep WHERE dep.visit_id = v.id)
  ) uv ON true
  WHERE cardinality(rs.hold_codes) > 0
)
SELECT code, priority, entity_type, entity_ref, robot, visit, since, owner_role, message, action_url, context
FROM (
  SELECT ex.code COLLATE "C" AS code, ex.entity_type,
         ex.entity_ref COLLATE "C" AS entity_ref, ex.robot, ex.visit, ex.since, ex.owner_role, ex.message,
         ex.action_url, ex.context, (%(prio)s::jsonb ->> ex.code)::int AS priority
  FROM ex
) t
WHERE %(a_prio)s::int IS NULL
   OR (priority, since, code, entity_ref)
      > (%(a_prio)s::int, %(a_since)s::timestamptz, %(a_code)s::text COLLATE "C", %(a_ref)s::text COLLATE "C")
ORDER BY priority, since, code, entity_ref
LIMIT %(limit)s
"""

# EM B2: open runs not yet at the hub whose RETURN_OVERDUE basis (planned unloading end) is unknown. The
# exception stays suppressed; the missing basis is listed instead. Independent of the threshold value.
UNKNOWN_BASIS_SQL = r"""
SELECT 'RETURN_OVERDUE', ors.run_code COLLATE "C", 'planned unloading end unknown'
FROM operations_open_run_stage ors
WHERE ors.arrived_hub_at IS NULL AND ors.planned_unloading_end IS NULL
ORDER BY 2
"""

READINESS_SQL = r"""
SELECT v.id, v.code, rb.code, rv.on_site_start - make_interval(mins => %(look)s) AS since,
       count(*) OVER () AS qualifying
FROM planning_visit v
JOIN planning_visitrevision rv ON rv.id = v.current_revision_id
LEFT JOIN fleet_robot rb ON rb.id = rv.robot_id
WHERE v.status = 'confirmed'
  AND rv.on_site_start - make_interval(mins => %(look)s) <= %(at)s
  AND rv.on_site_end > %(at)s
  AND NOT EXISTS (SELECT 1 FROM readiness_departureevent dep WHERE dep.visit_id = v.id)
ORDER BY rv.on_site_start, v.id
LIMIT %(cap)s
"""


def _sql_rules(
    at: datetime, thresholds: dict[str, int | None], limit: int | None, after: SortKey | None
) -> list[OpsException]:
    params = {
        "at": at,
        "g_verify": thresholds["ops_custody_verify_grace_min"],
        "g_return": thresholds["ops_return_grace_min"],
        "g_inspect": thresholds["ops_inspection_grace_min"],
        "prio": json.dumps(PRIORITY),
        "a_prio": after[0] if after else None,
        "a_since": after[1] if after else None,
        "a_code": after[2] if after else None,
        "a_ref": after[3] if after else None,
        "limit": limit,
    }
    with connection.cursor() as cur:
        cur.execute(RULES_SQL, params)
        rows = cur.fetchall()
    return [
        OpsException(
            code=code,
            priority=priority,
            entity_type=entity_type,
            entity_ref=entity_ref,
            robot=robot,
            visit=visit,
            since=since,
            owner_role=owner_role,
            message=message,
            action_url=action_url,
            resolvable=RESOLVABLE[code],
            context=json.loads(context) if isinstance(context, str) else (context or {}),
        )
        for (
            code,
            priority,
            entity_type,
            entity_ref,
            robot,
            visit,
            since,
            owner_role,
            message,
            action_url,
            context,
        ) in rows
    ]


def _readiness(at: datetime, lookahead: int | None) -> tuple[list[OpsException], dict[str, Any]]:
    """READINESS_INCOMPLETE_SOON (contract 4a): the authoritative evaluator for the first READINESS_CAP
    qualifying visits by (on_site_start, visit id); never inferred for the rest."""
    if lookahead is None:  # rule disabled; nothing evaluated, so coverage can never be claimed complete
        return [], {"qualifying": 0, "evaluated": 0, "not_evaluated": 0, "complete": False, "skipped": False}
    with connection.cursor() as cur:
        cur.execute(READINESS_SQL, {"look": lookahead, "at": at, "cap": READINESS_CAP})
        rows = cur.fetchall()
    qualifying = int(rows[0][4]) if rows else 0
    visits = Visit.objects.in_bulk([r[0] for r in rows]) if rows else {}
    items = []
    for vid, vcode, robot, since, _ in rows:
        result = evaluate_readiness(visits[vid], at=at)
        if result.ready:
            continue
        codes = sorted({b["code"] for b in result.blockers})
        items.append(
            OpsException(
                code="READINESS_INCOMPLETE_SOON",
                priority=PRIORITY["READINESS_INCOMPLETE_SOON"],
                entity_type="visit",
                entity_ref=vcode,
                robot=robot,
                visit=vcode,
                since=since,
                owner_role="ops_lead",
                message=f"Visit {vcode} starts within the readiness lookahead and is not "
                f"'{LABEL_READY}': {', '.join(codes)}",
                action_url=f"/readiness/visits/{vcode}/",
                resolvable=RESOLVABLE["READINESS_INCOMPLETE_SOON"],
            )
        )
    evaluated = len(rows)
    coverage = {
        "qualifying": qualifying,
        "evaluated": evaluated,
        "not_evaluated": qualifying - evaluated,
        "complete": qualifying == evaluated,
        "skipped": False,
    }
    return items, coverage


def _threshold_items(
    at: datetime, thresholds: dict[str, int | None], raw_missing: set[str]
) -> list[OpsException]:
    return [
        OpsException(
            code="THRESHOLD_NOT_CONFIGURED",
            priority=PRIORITY["THRESHOLD_NOT_CONFIGURED"],
            entity_type="setting",
            entity_ref=key,
            robot=None,
            visit=None,
            since=at,
            owner_role="engineering_lead",
            message=f"Deployment setting {key} is "
            + (
                "missing"
                if key in raw_missing
                else f"not a whole number of minutes from 1 to {MAX_THRESHOLD_MIN}"
            )
            + f"; rule {THRESHOLD_RULES[key]} is not evaluated until the schema owner sets it",
            action_url=None,
            resolvable=RESOLVABLE["THRESHOLD_NOT_CONFIGURED"],
        )
        for key, value in thresholds.items()
        if value is None
    ]


# ------------------------------------------------------------------ entry point
def open_exceptions(
    at: datetime,
    *,
    limit: int | None = None,
    after: SortKey | None = None,
    include_readiness: bool = True,
) -> ExceptionPage:
    """Open exceptions at the snapshot time ``at`` in the total order, starting after the sort key ``after``
    (keyset pagination), at most ``limit`` items. Call inside ``read_snapshot()``; each call (page) is its own
    snapshot. ``readiness_coverage`` and ``unknown_basis`` describe the whole inbox, not just this page.
    ``include_readiness=False`` (metrics page) runs no readiness evaluation: READINESS_INCOMPLETE_SOON is not
    produced and the coverage says ``skipped`` (never complete)."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    thresholds, missing = read_thresholds()

    sql_items = _sql_rules(at, thresholds, None if limit is None else limit + 1, after)
    with connection.cursor() as cur:
        cur.execute(UNKNOWN_BASIS_SQL)
        unknown = [{"rule": r, "run": run, "reason": why} for r, run, why in cur.fetchall()]
    if include_readiness:
        ready_items, coverage = _readiness(at, thresholds["ops_readiness_lookahead_min"])
    else:
        ready_items = []
        coverage = {"qualifying": 0, "evaluated": 0, "not_evaluated": 0, "complete": False, "skipped": True}
    py_items = [
        x
        for x in ready_items + _threshold_items(at, thresholds, missing)
        if after is None or x.sort_key > after
    ]
    items = sorted(sql_items + py_items, key=lambda x: x.sort_key)
    next_cursor: SortKey | None = None
    if limit is not None and len(items) > limit:
        items = items[:limit]
        next_cursor = items[-1].sort_key
    return ExceptionPage(
        items=items, readiness_coverage=coverage, next_cursor=next_cursor, unknown_basis=unknown
    )

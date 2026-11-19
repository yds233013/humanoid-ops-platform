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
``count_operational_exceptions(at)`` (metrics page): 1 thresholds + 1 ``SELECT count(*)`` over the same rule
relation (RULES_RELATION_SQL); no ordering, pagination, unknown-basis query or evaluator.

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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import connection

from planning.models import Visit
from readiness.evaluator import LABEL_READY, evaluate_readiness

# ------------------------------------------------------------------ constants
THRESHOLD_RULES: dict[str, str] = {  # setting key -> the rule it enables
    "ops_custody_verify_grace_min": "CUSTODY_UNVERIFIED",
    "ops_return_grace_min": "RETURN_OVERDUE",
    "ops_inspection_grace_min": "POST_VISIT_INSPECTION_MISSING",
    "ops_readiness_lookahead_min": "READINESS_INCOMPLETE_SOON",
    # Slice 7 (X-3, contract 07a 4.6): a visibility setting only, never a gate
    "ops_periodic_due_lookahead_min": "PERIODIC_INSPECTION_DUE_SOON",
}
# Slice 7 (X-10, contract 07a 8.2): an IANA time-zone name, valid only when BOTH Python zoneinfo and
# PostgreSQL
# pg_timezone_names know it (not the minutes rule). Missing / invalid -> one THRESHOLD_NOT_CONFIGURED.
CLOSEOUT_TIMEZONE_KEY = "ops_closeout_timezone"
TIMEZONE_MAX_LEN = 64
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
    # Slice 7 (contract 07a sections 4.6, 5.2, 5.6, 6.2)
    "PERIODIC_INTERVAL_UNCONFIGURED": 1,
    "PERIODIC_INSPECTION_OVERDUE": 2,
    "PERIODIC_INSPECTION_DUE_SOON": 3,
    "ESCALATION_OVERDUE": 1,
    "ESCALATION_DEADLINE_NOT_CONFIGURED": 1,
    # production blockers (operations_production_blockers); inbox items in PRODUCTION only, priority 1
    "HOLD_POLICY_NOT_APPROVED": 1,
    "HOLD_UNRELEASABLE": 1,
    "INSPECTION_TEMPLATE_NOT_APPROVED": 1,
    "PERIODIC_INTERVAL_NOT_APPROVED": 1,
    "ESCALATION_DEADLINES_NOT_APPROVED": 1,
}
BLOCKER_CODES = (
    "HOLD_POLICY_NOT_APPROVED",
    "HOLD_UNRELEASABLE",
    "INSPECTION_TEMPLATE_NOT_APPROVED",
    "PERIODIC_INTERVAL_NOT_APPROVED",
    "ESCALATION_DEADLINES_NOT_APPROVED",
)
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
    # Slice 7: recording a qualifying inspection needs an approved template and qualified signers (not always
    # possible in the app); intervals, deadlines, templates and policies need company input (outside the app).
    "PERIODIC_INTERVAL_UNCONFIGURED": False,
    "PERIODIC_INSPECTION_OVERDUE": False,
    "PERIODIC_INSPECTION_DUE_SOON": False,
    "ESCALATION_OVERDUE": True,  # escalate to / acknowledge as the role on the incident page
    "ESCALATION_DEADLINE_NOT_CONFIGURED": False,
    "HOLD_POLICY_NOT_APPROVED": False,
    "HOLD_UNRELEASABLE": False,
    "INSPECTION_TEMPLATE_NOT_APPROVED": False,
    "PERIODIC_INTERVAL_NOT_APPROVED": False,
    "ESCALATION_DEADLINES_NOT_APPROVED": False,
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


@dataclass(frozen=True)
class TimezoneSetting:
    """ops_closeout_timezone as read in the snapshot: ``name`` when valid (known to zoneinfo AND PostgreSQL),
    else None; ``state`` is "valid", "missing" or "invalid"."""

    name: str | None
    state: str
    raw: str | None


def python_knows_timezone(value: str) -> bool:
    if not value or len(value) > TIMEZONE_MAX_LEN or value != value.strip():
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


SETTINGS_SQL = r"""
SELECT s.key, s.value,
       CASE WHEN s.key = %(tz_key)s AND length(s.value) <= %(tz_max)s
            THEN EXISTS (SELECT 1 FROM pg_timezone_names z WHERE z.name = s.value) END AS pg_tz_known
FROM fleet_deploymentsetting s
WHERE s.key = ANY(%(keys)s)
"""


def read_settings() -> tuple[dict[str, int | None], set[str], TimezoneSetting]:
    """The time thresholds and the close-out time zone, read in the current snapshot (ONE statement):
    ({key: minutes or None}, the threshold keys missing altogether, the time-zone setting)."""
    with connection.cursor() as cur:
        cur.execute(
            SETTINGS_SQL,
            {
                "tz_key": CLOSEOUT_TIMEZONE_KEY,
                "tz_max": TIMEZONE_MAX_LEN,
                "keys": [*THRESHOLD_RULES, CLOSEOUT_TIMEZONE_KEY],
            },
        )
        rows = cur.fetchall()
    raw = {key: value for key, value, _ in rows}
    pg_known = {key: known for key, _, known in rows}
    thresholds = {key: parse_threshold(raw.get(key)) for key in THRESHOLD_RULES}
    missing = set(THRESHOLD_RULES) - set(raw)
    tz_raw = raw.get(CLOSEOUT_TIMEZONE_KEY)
    if tz_raw is None:
        tz = TimezoneSetting(None, "missing", None)
    elif pg_known.get(CLOSEOUT_TIMEZONE_KEY) and python_knows_timezone(tz_raw):
        tz = TimezoneSetting(tz_raw, "valid", tz_raw)
    else:
        tz = TimezoneSetting(None, "invalid", tz_raw)
    return thresholds, missing, tz


def read_thresholds() -> tuple[dict[str, int | None], set[str]]:
    """The time thresholds as read in the current snapshot (one statement): ({key: minutes or None},
    the keys that are missing altogether)."""
    thresholds, missing, _ = read_settings()
    return thresholds, missing


# ------------------------------------------------------------------ the SQL-derived rules
# The ONE definition of every SQL-derived rule: a WITH clause ending in the relation ``ex``. Every branch
# yields (code, entity_type, entity_ref, robot, visit, since, owner_role, message, action_url, context);
# priority comes from PRIORITY; NULL thresholds switch their branch off. Time uses %(at)s only. Two
# projections read it: RULES_SQL (inbox rows, ordered and keyset-paginated) and COUNT_SQL
# (``count_operational_exceptions``).
# Slice 7 X-3: the periodic inspection status of every non-retired robot, SET-BASED (contract 07a 4.2 and
# A's readiness_periodic_inspection_status, docs/07b section 4). Calling that plpgsql function once per robot
# costs one planning_inspection_signer_problem call per candidate inspection and broke the Slice 5 scale
# budgets, so the same rule is evaluated here with the usable rule once per pattern, the usable periodic
# templates once, and the covered-signer rule (planning_inspector_covered) as two EXISTS. An oracle proves
# it equals the function for every robot. Needs a CTE ``mode`` (production flag) and the parameter %(at)s;
# yields pstat(robot_id, robot_code, pattern_code, state, due_at, inspection_id, rule_id).
PERIODIC_STATUS_CTES = r"""
pused AS MATERIALIZED (
  SELECT DISTINCT m.pattern_id
  FROM fleet_robot r JOIN fleet_configurationmanifest m ON m.id = r.current_manifest_id
  WHERE r.lifecycle <> 'retired'
),
prule AS MATERIALIZED (
  SELECT pu.pattern_id, fleet_usable_periodic_rule_id(pu.pattern_id, mode.production) AS usable_id,
         fleet_active_periodic_rule_id(pu.pattern_id) AS active_id
  FROM pused pu CROSS JOIN mode
),
ptpl AS MATERIALIZED (
  SELECT t.id FROM fleet_inspectiontemplate t CROSS JOIN mode
  WHERE t.pattern_id IN (SELECT pattern_id FROM pused)
    AND fleet_template_usable(t.id, 'periodic', mode.production)
),
pstat AS MATERIALIZED (
  SELECT r.id AS robot_id, r.code AS robot_code, p.code AS pattern_code,
         CASE WHEN ru.id IS NULL THEN 'unknown_interval'
              WHEN qi.id IS NULL THEN 'never_inspected'
              WHEN %(at)s >= qi.submitted_at + make_interval(mins => ru.interval_min) THEN 'overdue'
              ELSE 'due' END AS state,
         qi.submitted_at + make_interval(mins => ru.interval_min) AS due_at,
         qi.id AS inspection_id,
         CASE WHEN ru.id IS NULL THEN pr.active_id ELSE ru.id END AS rule_id
  FROM fleet_robot r
  LEFT JOIN fleet_configurationmanifest m ON m.id = r.current_manifest_id
  LEFT JOIN fleet_manifestpattern p ON p.id = m.pattern_id
  LEFT JOIN prule pr ON pr.pattern_id = m.pattern_id
  LEFT JOIN fleet_periodicinspectionrule ru ON ru.id = pr.usable_id
  LEFT JOIN LATERAL (
    SELECT i.id, i.submitted_at FROM fleet_inspection i
    WHERE ru.id IS NOT NULL AND i.robot_id = r.id AND i.manifest_id = r.current_manifest_id
      AND i.result = 'PASS' AND i.complete AND i.template_id IN (SELECT id FROM ptpl)
      AND i.performer_id IS NOT NULL AND i.verifier_id IS NOT NULL
      AND EXISTS (SELECT 1 FROM planning_qualification q JOIN planning_person pp ON pp.id = q.person_id
                  WHERE pp.user_id = i.performer_id AND q.kind = 'readiness_inspector'
                    AND q.revoked_at IS NULL
                    AND q.valid_from <= i.submitted_at AND i.submitted_at <= q.valid_to)
      AND EXISTS (SELECT 1 FROM planning_qualification q JOIN planning_person pp ON pp.id = q.person_id
                  WHERE pp.user_id = i.verifier_id AND q.kind = 'readiness_inspector'
                    AND q.revoked_at IS NULL
                    AND q.valid_from <= i.submitted_at AND i.submitted_at <= q.valid_to)
    ORDER BY i.submitted_at DESC, i.id DESC LIMIT 1
  ) qi ON true
  WHERE r.lifecycle <> 'retired'
),
"""

RULES_RELATION_SQL = (
    r"""
-- Each view is read once, projecting only the columns the rules below use: the planner then drops the unused
-- view outputs (eligibility reasons, next visit, status label, session state, severity rank, ...) instead of
-- computing them for every robot / run / incident (Slice 5 perf, EXPLAIN on the scale fixture).
WITH rs AS MATERIALIZED (
  SELECT robot_id, robot_code, custody_kind, custody_event_id, custody_since, open_run_code,
         open_run_visit_code, hold_codes, holds_since
  FROM operations_robot_status
),
ors AS MATERIALIZED (
  SELECT run_id, run_code, robot_code, visit_code, departed_at, planned_unloading_end, arrived_hub_at,
         latest_reconciliation_outcome, latest_reconciliation_at, custody_event_id, awaiting
  FROM operations_open_run_stage
),
q AS MATERIALIZED (
  SELECT incident_id, incident_code, robot_code, visit_id, event_type, reported_at, status, unclassified,
         severity_level, severity_policy_id
  FROM operations_incident_queue
),
-- Slice 7 (contract 07a 2.2): the deployment mode as read in this snapshot (missing setting = production)
mode AS MATERIALIZED (SELECT fleet_production_mode() AS production),
"""
    + PERIODIC_STATUS_CTES
    + r"""
-- Slice 7 X-4: the usable deadline set (active for the active incident policy; owner_approved in production)
dset AS MATERIALIZED (
  SELECT d.id, d.incident_policy_id, d.deadlines, d.version_no
  FROM execution_escalationdeadlineset d CROSS JOIN mode
  WHERE d.id = execution_active_deadline_set_id() AND (d.source = 'owner_approved' OR NOT mode.production)
),
-- level of each open incident under that set: 'unclassified' without a decision, the decision's level when it
-- was made under the set's policy (and the set has that key), else NULL = unconfigured
qlev AS MATERIALIZED (
  SELECT q.incident_id, q.incident_code, q.robot_code, q.visit_id, q.reported_at, d.deadlines, d.version_no,
         CASE WHEN q.unclassified AND d.deadlines ? 'unclassified' THEN 'unclassified'
              WHEN NOT q.unclassified AND q.severity_policy_id = d.incident_policy_id
                   AND d.deadlines ? q.severity_level THEN q.severity_level END AS level_key
  FROM q CROSS JOIN dset d
),
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

  UNION ALL
  -- PERIODIC_INTERVAL_UNCONFIGURED (X-3): a pattern in use has no periodic rule usable in the current mode
  SELECT DISTINCT 'PERIODIC_INTERVAL_UNCONFIGURED', 'pattern', ps.pattern_code, NULL::text, NULL::text,
         %(at)s::timestamptz, 'engineering_lead',
         'Configuration ' || ps.pattern_code || ' has no periodic inspection interval usable in '
           || CASE WHEN (SELECT production FROM mode) THEN 'production' ELSE 'demo' END
           || ' mode (interval unknown); its robots are not eligible for planning and cannot be authorized',
         '/fleet/patterns/' || ps.pattern_code || '/', NULL::jsonb
  FROM pstat ps
  WHERE ps.state = 'unknown_interval' AND ps.pattern_code IS NOT NULL

  UNION ALL
  -- PERIODIC_INSPECTION_OVERDUE (X-3): overdue (since due_at) or never inspected on this configuration
  -- (since at)
  SELECT 'PERIODIC_INSPECTION_OVERDUE', 'robot', ps.robot_code, ps.robot_code, NULL::text,
         coalesce(ps.due_at, %(at)s::timestamptz), 'engineering_lead',
         CASE WHEN ps.state = 'overdue'
              THEN 'Periodic inspection of robot ' || ps.robot_code || ' is overdue since '
                   || to_char(ps.due_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC'
              ELSE 'Robot ' || ps.robot_code || ' has no qualifying periodic inspection on its current '
                   || 'configuration (' || ps.pattern_code || ')' END,
         '/fleet/' || ps.robot_code || '/', NULL::jsonb
  FROM pstat ps
  WHERE ps.state IN ('overdue', 'never_inspected')

  UNION ALL
  -- PERIODIC_INSPECTION_DUE_SOON (X-3): due, and at >= due_at - ops_periodic_due_lookahead_min (inclusive)
  SELECT 'PERIODIC_INSPECTION_DUE_SOON', 'robot', ps.robot_code, ps.robot_code, NULL::text,
         ps.due_at - make_interval(mins => %(g_periodic)s::int), 'engineering_lead',
         'Periodic inspection of robot ' || ps.robot_code || ' is due at '
           || to_char(ps.due_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC',
         '/fleet/' || ps.robot_code || '/', NULL::jsonb
  FROM pstat ps
  WHERE %(g_periodic)s::int IS NOT NULL AND ps.state = 'due'
    AND ps.due_at - make_interval(mins => %(g_periodic)s::int) <= %(at)s

  UNION ALL
  -- ESCALATION_DEADLINE_NOT_CONFIGURED (X-4): no usable deadline set at all (once per snapshot) ...
  SELECT 'ESCALATION_DEADLINE_NOT_CONFIGURED', 'setting', 'deadline-set', NULL::text, NULL::text,
         %(at)s::timestamptz, 'safety_reviewer',
         'No escalation deadline set is usable in '
           || CASE WHEN (SELECT production FROM mode) THEN 'production (it must be owner approved)'
                   ELSE 'demo' END
           || ' mode for the active incident policy, so overdue escalations cannot be shown',
         '/execution/escalation-deadlines/', NULL::jsonb
  WHERE NOT EXISTS (SELECT 1 FROM dset)

  UNION ALL
  -- ... or once per open incident whose level is unconfigured (decision under another policy version)
  SELECT 'ESCALATION_DEADLINE_NOT_CONFIGURED', 'incident', ql.incident_code, ql.robot_code, v.code,
         %(at)s::timestamptz, 'safety_reviewer',
         'Incident ' || ql.incident_code || ' (robot ' || ql.robot_code || ') has a severity decision '
           || 'that the escalation deadline set v' || ql.version_no || ' does not cover (made under '
           || 'another incident policy version), so no escalation deadline applies to it',
         '/execution/incidents/' || ql.incident_code || '/', NULL::jsonb
  FROM qlev ql LEFT JOIN planning_visit v ON v.id = ql.visit_id
  WHERE ql.level_key IS NULL

  UNION ALL
  -- ESCALATION_OVERDUE (X-4): open incident, entry (R, N) of its level, at >= reported_at + N and no
  -- acknowledgment on any escalation of the incident to R. Owner = R. Nothing is sent.
  SELECT 'ESCALATION_OVERDUE', 'incident_role', ql.incident_code || '/' || (e.value ->> 'to_role'),
         ql.robot_code, v.code,
         ql.reported_at + make_interval(mins => (e.value ->> 'within_min')::int), e.value ->> 'to_role',
         'Incident ' || ql.incident_code || ' (robot ' || ql.robot_code || ', ' || ql.level_key || '): '
           || CASE WHEN fe.first_at IS NULL THEN 'no escalation to ' || (e.value ->> 'to_role') || ' recorded'
                   ELSE 'escalated to ' || (e.value ->> 'to_role') || ' at '
                        || to_char(fe.first_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI')
                        || ' UTC, not acknowledged'
              END
           || '; required within ' || (e.value ->> 'within_min') || ' min of the report',
         '/execution/incidents/' || ql.incident_code || '/',
         jsonb_build_object('incident', ql.incident_code, 'to_role', e.value ->> 'to_role',
                            'within_min', (e.value ->> 'within_min')::int, 'level', ql.level_key,
                            'deadline_set_version', ql.version_no, 'escalated', fe.first_at IS NOT NULL)
  FROM qlev ql
  CROSS JOIN LATERAL jsonb_array_elements(ql.deadlines -> ql.level_key) e
  LEFT JOIN planning_visit v ON v.id = ql.visit_id
  LEFT JOIN LATERAL (
    SELECT min(x.recorded_at) AS first_at FROM execution_incidentescalation x
    WHERE x.incident_id = ql.incident_id AND x.to_role = e.value ->> 'to_role'
  ) fe ON true
  WHERE ql.level_key IS NOT NULL
    AND ql.reported_at + make_interval(mins => (e.value ->> 'within_min')::int) <= %(at)s
    AND NOT EXISTS (SELECT 1 FROM execution_incidentescalation x
                    JOIN execution_escalationack k ON k.escalation_id = x.id
                    WHERE x.incident_id = ql.incident_id AND x.to_role = e.value ->> 'to_role')

  UNION ALL
  -- Production blockers (G-01, X-1, X-3, X-4; operations_production_blockers): inbox items in PRODUCTION
  -- only (the overview panel shows them in both modes). The one-time filter skips the view in demo mode.
  SELECT pb.code,
         CASE pb.code WHEN 'HOLD_UNRELEASABLE' THEN 'hold' WHEN 'HOLD_POLICY_NOT_APPROVED' THEN 'hold_policy'
                      WHEN 'ESCALATION_DEADLINES_NOT_APPROVED' THEN 'setting' ELSE 'pattern' END,
         pb.entity_ref, hb.robot_code, NULL::text, %(at)s::timestamptz,
         CASE WHEN pb.code = 'ESCALATION_DEADLINES_NOT_APPROVED' THEN 'safety_reviewer'
              ELSE 'engineering_lead' END,
         'Production blocker: ' || pb.detail,
         CASE pb.code
           WHEN 'HOLD_POLICY_NOT_APPROVED' THEN '/fleet/hold-policies/'
           WHEN 'ESCALATION_DEADLINES_NOT_APPROVED' THEN '/execution/escalation-deadlines/'
           WHEN 'HOLD_UNRELEASABLE' THEN coalesce('/fleet/' || hb.robot_code || '/',
                                                  '/fleet/patterns/' || hb.pattern_code || '/')
           ELSE '/fleet/patterns/' || pb.entity_ref || '/' END,
         NULL::jsonb
  FROM operations_production_blockers pb
  LEFT JOIN LATERAL (
    SELECT r.code AS robot_code, pp.code AS pattern_code FROM fleet_hold h
    LEFT JOIN fleet_robot r ON r.id = h.robot_id
    LEFT JOIN fleet_manifestpattern pp ON pp.id = h.pattern_id
    WHERE pb.code = 'HOLD_UNRELEASABLE' AND h.code = pb.entity_ref
  ) hb ON true
  WHERE (SELECT production FROM mode)
)
"""
)

# Projection (i): inbox rows in the total order, keyset-filtered and limited in SQL.
RULES_SQL = (
    RULES_RELATION_SQL
    + r"""
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
)

# Projection (ii): how many rows the same relation holds; no ordering, pagination or row materialization.
COUNT_SQL = RULES_RELATION_SQL + "SELECT count(*) FROM ex\n"

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


def _relation_params(at: datetime, thresholds: dict[str, int | None]) -> dict[str, Any]:
    """The parameters of RULES_RELATION_SQL (shared by both projections)."""
    return {
        "at": at,
        "g_verify": thresholds["ops_custody_verify_grace_min"],
        "g_return": thresholds["ops_return_grace_min"],
        "g_inspect": thresholds["ops_inspection_grace_min"],
        "g_periodic": thresholds["ops_periodic_due_lookahead_min"],
    }


def _sql_rules(
    at: datetime, thresholds: dict[str, int | None], limit: int | None, after: SortKey | None
) -> list[OpsException]:
    params = {
        **_relation_params(at, thresholds),
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
    at: datetime,
    thresholds: dict[str, int | None],
    raw_missing: set[str],
    tz: TimezoneSetting | None = None,
) -> list[OpsException]:
    items = [
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
    if tz is not None and tz.state != "valid":
        items.append(
            OpsException(
                code="THRESHOLD_NOT_CONFIGURED",
                priority=PRIORITY["THRESHOLD_NOT_CONFIGURED"],
                entity_type="setting",
                entity_ref=CLOSEOUT_TIMEZONE_KEY,
                robot=None,
                visit=None,
                since=at,
                owner_role="engineering_lead",
                message=f"Deployment setting {CLOSEOUT_TIMEZONE_KEY} is "
                + (
                    "missing"
                    if tz.state == "missing"
                    else "not a time zone name known to both the application and the database"
                )
                + "; the daily close-out cannot assign runs to a local day (it lists every open run "
                "undated) until the schema owner sets it",
                action_url="/ops/closeout/",
                resolvable=RESOLVABLE["THRESHOLD_NOT_CONFIGURED"],
            )
        )
    return items


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
    thresholds, missing, tz = read_settings()

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
        for x in ready_items + _threshold_items(at, thresholds, missing, tz)
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


def count_operational_exceptions(at: datetime) -> int:
    """How many items ``open_exceptions(at, include_readiness=False)`` would return in total (all pages):
    every SQL-derived rule (counted over the same RULES_RELATION_SQL, no ordering, pagination or row
    materialization) plus THRESHOLD_NOT_CONFIGURED (the same ``_threshold_items``). READINESS_INCOMPLETE_SOON
    is excluded and the evaluator never runs; unknown_basis is not part of the inbox items and is not read.
    Two statements (thresholds, count). Call inside ``read_snapshot()`` with its time as ``at``."""
    thresholds, missing, tz = read_settings()
    with connection.cursor() as cur:
        cur.execute(COUNT_SQL, _relation_params(at, thresholds))
        row = cur.fetchone()
    assert row is not None
    return int(row[0]) + len(_threshold_items(at, thresholds, missing, tz))


# ------------------------------------------------------------------ X-4 Python reference (oracle only)
def _reference_active_deadline_set(production: bool) -> Any:
    """Usable deadline set, derived row by row (contract 07a 6.2): for the active incident policy, the highest
    version_no ever approved by both slots (safety + founder); none when that version is withdrawn (no
    fallback); in production only an owner_approved set."""
    from execution.models import EscalationDeadlineSet
    from execution.policy import current_incident_policy

    policy = current_incident_policy()
    if policy is None:
        return None
    approved = [
        d
        for d in EscalationDeadlineSet.objects.filter(incident_policy=policy).prefetch_related("approvals")
        if {a.slot for a in d.approvals.all()} >= {"safety", "founder"}
    ]
    if not approved:
        return None
    top = max(approved, key=lambda d: d.version_no)
    if hasattr(top, "withdrawal"):
        return None
    if production and top.source != "owner_approved":
        return None
    return top


def reference_escalation_overdue(at: datetime) -> set[tuple[str, str, datetime, str]]:
    """Independent Python derivation of ESCALATION_OVERDUE and ESCALATION_DEADLINE_NOT_CONFIGURED as
    {(code, entity_ref, since, owner_role)}. Used ONLY by the oracle test against RULES_RELATION_SQL;
    never by a
    page. Reads ORM rows one by one (not constant in fleet size), so never call it from a read model."""
    from datetime import timedelta

    from execution.incidents import incident_status, latest_severity
    from execution.models import Incident
    from fleet.services import demo_mode

    production = not demo_mode()
    dset = _reference_active_deadline_set(production)
    out: set[tuple[str, str, datetime, str]] = set()
    if dset is None:
        out.add(("ESCALATION_DEADLINE_NOT_CONFIGURED", "deadline-set", at, "safety_reviewer"))
        return out
    for i in Incident.objects.prefetch_related("escalations__ack").order_by("id"):
        if incident_status(i) == "closed":
            continue
        decision = latest_severity(i)
        if decision is None:
            level = "unclassified"
        elif decision.policy_id == dset.incident_policy_id:
            level = decision.level
        else:
            level = None
        if level is None or level not in dset.deadlines:
            out.add(("ESCALATION_DEADLINE_NOT_CONFIGURED", i.code, at, "safety_reviewer"))
            continue
        for entry in dset.deadlines[level]:
            role, minutes = entry["to_role"], int(entry["within_min"])
            deadline = i.reported_at + timedelta(minutes=minutes)
            if at < deadline:
                continue
            acked = any(e.to_role == role and hasattr(e, "ack") for e in i.escalations.all())
            if not acked:
                out.add(("ESCALATION_OVERDUE", f"{i.code}/{role}", deadline, role))
    return out

"""Hub tasks derived from existing facts (v2 02A). ONE set-based SQL statement, whatever the fleet size.

When it runs: at the start of every Hub page load (``refresh``), and from
``manage.py refresh_hub_tasks`` for a scheduled job. It is cheap (one statement) and idempotent.

Facts (all read from the authoritative records, nothing re-decided):

* ``periodic_inspection`` -> ``inspection_due``: the periodic status of every non-retired robot, from the same
  set-based CTEs the exceptions inbox uses (operations.exceptions.PERIODIC_STATUS_CTES, oracle-tested equal to
  readiness_periodic_inspection_status): overdue, never inspected, or due within the owner-provisioned
  ``ops_periodic_due_lookahead_min`` (off when that setting is missing). Ref: ``<robot>:<last inspection id or
  none>``, so a new inspection cycle is a new fact.
* ``hold`` -> ``hold_work``: every active (unreleased) robot or configuration hold. Ref: the hold code. The
  task links to the hold page; releasing stays J7 (approvals), never this task.
* ``equipment_check`` -> ``equipment_failure``: the latest non-voided equipment check of each visit that
  is not cancelled and not over yet, when it failed. Ref: the evidence code.

Rules:

1. A source creates at most ONE task ever: a fact that already has a task (in any status) is skipped, so
   closing a task is final for that fact; a new fact (a new hold, a new inspection cycle, new evidence) gets a
   new task. The partial unique index (source_kind, source_ref) over active tasks makes concurrent refreshes
   safe: ``ON CONFLICT DO NOTHING``.
2. When the fact disappears (hold released, inspection recorded, evidence voided or superseded), the task's
   ``source_cleared_at`` is set and the screen says "Resolved by the underlying record". Its status is NOT
   changed: marking it done would claim work nobody recorded; a person closes it. If the fact comes back, the
   mark is cleared.
3. Every created task and every cleared / restored mark writes an audit row (actor: the system, NULL).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import connection

from operations.exceptions import PERIODIC_STATUS_CTES
from platform_core.middleware import current_request_id

REFRESH_SQL = (
    r"""
WITH mode AS MATERIALIZED (SELECT fleet_production_mode() AS production),
"""
    + PERIODIC_STATUS_CTES
    + r"""
look AS MATERIALIZED (
  SELECT CASE WHEN value ~ '^[0-9]{1,7}$' THEN value::int END AS minutes
  FROM fleet_deploymentsetting WHERE key = 'ops_periodic_due_lookahead_min'
),
facts AS MATERIALIZED (
  SELECT 'periodic_inspection'::text AS kind, 'inspection_due'::text AS type,
         ps.robot_code || ':' || coalesce(ps.inspection_id::text, 'none') AS ref,
         ps.robot_id, NULL::bigint AS visit_id,
         CASE ps.state
           WHEN 'overdue' THEN 'Periodic inspection of ' || ps.robot_code || ' is overdue'
           WHEN 'never_inspected'
             THEN 'Robot ' || ps.robot_code || ' has no periodic inspection on its configuration'
           ELSE 'Periodic inspection of ' || ps.robot_code || ' is due soon' END AS title,
         ps.due_at,
         '/readiness/robots/' || ps.robot_code || '/inspection/' AS link_url
  FROM pstat ps
  WHERE ps.state IN ('overdue', 'never_inspected')
     OR (ps.state = 'due' AND (SELECT minutes FROM look) IS NOT NULL
         AND ps.due_at - make_interval(mins => (SELECT minutes FROM look)) <= %(at)s)
  UNION ALL
  SELECT 'hold', 'hold_work', h.code, h.robot_id, NULL::bigint,
         CASE WHEN h.scope = 'robot' THEN 'Robot ' || r.code || ' is on hold (' || h.code || ')'
              ELSE 'Configuration ' || p.code || ' is on hold (' || h.code || ')' END,
         NULL::timestamptz,
         CASE WHEN h.scope = 'robot' THEN '/fleet/' || r.code || '/'
              ELSE '/fleet/patterns/' || p.code || '/' END
  FROM fleet_hold h
  LEFT JOIN fleet_robot r ON r.id = h.robot_id
  LEFT JOIN fleet_manifestpattern p ON p.id = h.pattern_id
  WHERE h.released_at IS NULL
  UNION ALL
  SELECT 'equipment_check', 'equipment_failure', e.code, e.robot_id, e.visit_id,
         'Equipment check ' || e.code || ' failed for visit ' || v.code,
         vr.on_site_start, '/readiness/visits/' || v.code || '/'
  FROM (
    SELECT DISTINCT ON (ev.visit_id) ev.*
    FROM readiness_readinessevidence ev
    WHERE ev.kind = 'equipment_check'
      AND NOT EXISTS (SELECT 1 FROM readiness_readinessevidencevoid vd WHERE vd.evidence_id = ev.id)
    ORDER BY ev.visit_id, ev.observed_at DESC, ev.id DESC
  ) e
  JOIN planning_visit v ON v.id = e.visit_id
  JOIN planning_visitrevision vr ON vr.id = v.current_revision_id
  WHERE e.result = 'fail' AND v.status <> 'cancelled' AND vr.on_site_end > %(at)s
),
ins AS (
  INSERT INTO hub_workorder (type, title, robot_id, visit_id, due_at, owner_id, status, source_kind,
                             source_ref,
                             link_url, note, close_reason, created_by_id, created_at, updated_at, version)
  SELECT f.type, left(f.title, 160), f.robot_id, f.visit_id, f.due_at, NULL, 'open', f.kind, f.ref,
         f.link_url, '', '', NULL, %(at)s, %(at)s, 1
  FROM facts f
  WHERE NOT EXISTS (SELECT 1 FROM hub_workorder w WHERE w.source_kind = f.kind AND w.source_ref = f.ref)
  ON CONFLICT (source_kind, source_ref) WHERE (NOT (status IN ('done', 'cancelled'))) DO NOTHING
  RETURNING id, type, source_kind, source_ref
),
upd AS (
  UPDATE hub_workorder w
  SET source_cleared_at = CASE WHEN f.ref IS NULL THEN %(at)s ELSE NULL END, updated_at = %(at)s
  FROM hub_workorder w0
  LEFT JOIN facts f ON f.kind = w0.source_kind AND f.ref = w0.source_ref
  WHERE w.id = w0.id AND w0.source_kind <> 'manual' AND NOT (w0.status IN ('done', 'cancelled'))
    AND (w0.source_cleared_at IS NULL) = (f.ref IS NULL)
  RETURNING w.id, w.source_kind, w.source_ref, w.source_cleared_at
),
aud AS (
  INSERT INTO platform_core_auditevent (occurred_at, actor_id, command, outcome, entity_type, entity_id,
                                        payload,
                                        result, reason, request_id)
  SELECT %(at)s::timestamptz, NULL::integer, 'hub.derive_task', 'committed', 'work_order', i.id::text,
         jsonb_build_object('source_kind', i.source_kind, 'source_ref', i.source_ref),
         jsonb_build_object('type', i.type, 'status', 'open'), '', %(rid)s
  FROM ins i
  UNION ALL
  SELECT %(at)s::timestamptz, NULL::integer,
         CASE WHEN u.source_cleared_at IS NULL THEN 'hub.source_restored' ELSE 'hub.source_cleared' END,
         'committed', 'work_order', u.id::text,
         jsonb_build_object('source_kind', u.source_kind, 'source_ref', u.source_ref), '{}'::jsonb, '',
         %(rid)s
  FROM upd u
  RETURNING 1
)
SELECT (SELECT count(*) FROM ins), (SELECT count(*) FROM upd), (SELECT count(*) FROM aud)
"""
)


def refresh(at: datetime | None = None) -> dict[str, Any]:
    """Create missing derived tasks and mark / unmark cleared sources. One statement; safe to run
    concurrently."""
    from django.utils import timezone

    at = at or timezone.now()
    with connection.cursor() as cur:
        cur.execute(REFRESH_SQL, {"at": at, "rid": current_request_id.get()})
        created, marked, audited = cur.fetchone()
    return {"created": int(created), "marked": int(marked), "audited": int(audited)}

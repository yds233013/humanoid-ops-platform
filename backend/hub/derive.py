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
* ``maintenance_status`` -> ``maintenance_due`` (v2 02C): due soon, overdue, unknown or required inspection
  missing, per robot and requirement (fleet_maintenance_status_set). Ref: ``<robot>:<requirement>:<rule
  version>:<last record id or none>``: stable while one maintenance cycle runs (due soon -> overdue keeps the
  task); a new record starts a new cycle and so a new fact, as a new inspection does for ``inspection_due``.
  A task never records maintenance.
* ``stock_level`` -> ``kit_low`` (v2 02D): an item whose AVAILABLE stock (on hand - reserved, summed from
  the ledger) is below its owner-provisioned minimum (``stock_minimums``; an item without one never derives a
  task). Ref: ``<item>:<id of the movement that took it below the minimum, or start>``. Restocking clears the
  mark; a task never moves stock.

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

import json
from datetime import datetime, timedelta
from typing import Any

from django.db import connection

from hub.stock import MINIMUMS_CTE, PER_VISIT_CTE
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
stock_min AS MATERIALIZED ("""
    + MINIMUMS_CTE
    + r"""),
stock_per AS MATERIALIZED ("""
    + PER_VISIT_CTE
    + r"""),
stock_run AS MATERIALIZED (
  SELECT m.item_id, m.id, m.on_hand_delta - m.reserved_delta AS d,
         sum(m.on_hand_delta - m.reserved_delta) OVER (PARTITION BY m.item_id ORDER BY m.id) AS after_avail
  FROM hub_stock_movement m
  WHERE m.item_id IN (SELECT i.id FROM hub_stock_item i JOIN stock_min sm ON sm.code = i.code)
),
stock_low AS MATERIALIZED (
  SELECT i.id, i.code, i.name, sm.n AS minimum,
         coalesce((SELECT r.after_avail FROM stock_run r WHERE r.item_id = i.id
                   ORDER BY r.id DESC LIMIT 1), 0) AS available,
         (SELECT max(r.id) FROM stock_run r
          WHERE r.item_id = i.id AND r.after_avail < sm.n AND r.after_avail - r.d >= sm.n) AS crossing,
         (SELECT min(vr.on_site_start - make_interval(mins => coalesce(vr.travel_out_min, 0)))
          FROM planning_visit v JOIN planning_visitrevision vr ON vr.id = v.current_revision_id
          WHERE EXISTS (SELECT 1 FROM stock_per sp WHERE sp.code = i.code AND sp.n > 0)
            AND v.status <> 'cancelled'
            AND vr.on_site_start - make_interval(mins => coalesce(vr.travel_out_min, 0)) >= %(at)s
            AND vr.on_site_start - make_interval(mins => coalesce(vr.travel_out_min, 0))
                < %(at)s + interval '2 days') AS next_departure
  FROM hub_stock_item i JOIN stock_min sm ON sm.code = i.code
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
  UNION ALL
  -- v2 02B: visits today and tomorrow whose robot needs charge before departure (hub.charging forecast)
  SELECT 'charge_forecast', 'charging', c ->> 'ref', (c ->> 'robot_id')::bigint, (c ->> 'visit_id')::bigint,
         c ->> 'title', (c ->> 'due_at')::timestamptz, '/hub/charging/'
  FROM jsonb_array_elements(%(charge)s::jsonb) c
  UNION ALL
  -- v2 02C: maintenance due soon, overdue, unknown or waiting for its inspection, per robot and
  -- requirement, from fleet_maintenance_status_set (the function the planning and authorization gates
  -- use), whether or not the gate is active yet (the tasks are how the hub works towards activation)
  SELECT 'maintenance_status', 'maintenance_due',
         ms.robot_code || ':' || ms.requirement_code || ':' || ms.rule_version || ':'
           || coalesce(ms.record_id::text, 'none'),
         ms.robot_id, NULL::bigint,
         'Maintenance ' || ms.requirement_code || ' of ' || ms.robot_code || CASE ms.state
           WHEN 'overdue' THEN ' is overdue'
           WHEN 'unknown' THEN ' has an unknown status'
           WHEN 'inspection_missing' THEN ' needs its post-maintenance inspection'
           ELSE ' is due soon' END,
         ms.due_at, '/fleet/' || ms.robot_code || '/maintenance/'
  FROM fleet_maintenance_status_set(ARRAY(SELECT id FROM fleet_robot WHERE lifecycle <> 'retired'),
                                    (SELECT production FROM mode), %(at)s) ms
  WHERE ms.state IN ('due_soon', 'overdue', 'unknown', 'inspection_missing')
    AND ms.requirement_code IS NOT NULL
  UNION ALL
  -- v2 02D: an item whose available stock is below its owner-provisioned minimum. Ref
  -- <item>:<movement that took it below> ('start' when no movement crossed the current minimum): stable
  -- while it stays low; restocking clears it; falling below again is a new fact. Due before the next visit
  -- that takes the item (stock_per_visit), else no due time.
  SELECT 'stock_level', 'kit_low', sl.code || ':' || coalesce(sl.crossing::text, 'start'), NULL::bigint,
         NULL::bigint, 'Restock ' || sl.name || ' (' || sl.code || '): below its minimum of ' || sl.minimum,
         sl.next_departure, '/hub/stock/#item-' || sl.code
  FROM stock_low sl
  WHERE sl.available < sl.minimum
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


CHARGE_HORIZON = timedelta(days=2)


def _charge_facts(at: datetime) -> list[dict[str, Any]]:
    """v2 02B: one fact per visit in the next two days whose robot Needs charge (two statements, any fleet
    size). Source ref ``<visit>:<robot>``: re-planning the visit onto another robot is a new fact."""
    from hub import charging
    from planning.models import Visit

    if not charging.forecast_configured():
        return []
    visits = list(
        Visit.objects.filter(
            current_revision__on_site_start__gte=at,
            current_revision__on_site_start__lt=at + CHARGE_HORIZON,
            current_revision__robot__isnull=False,
        )
        .exclude(status="cancelled")
        .values_list("id", "code", "current_revision__robot__code")
    )
    forecasts = charging.visit_forecasts([v[0] for v in visits], at)
    out = []
    for vid, code, robot in visits:
        f = forecasts.get(vid)
        if f is not None and f.state == charging.NEEDS:
            title = f"Charge {robot} before visit {code}" + (
                " (cannot be charged in time)" if f.cannot_in_time else ""
            )
            out.append({"ref": f"{code}:{robot}", "robot_id": f.robot_id, "visit_id": vid, "title": title,
                        "due_at": f.departure_at.isoformat()})  # fmt: skip
    return out


def refresh(at: datetime | None = None) -> dict[str, Any]:
    """Create missing derived tasks and mark / unmark cleared sources. One statement; safe to run
    concurrently."""
    from django.utils import timezone

    at = at or timezone.now()
    charge = json.dumps(_charge_facts(at), default=str)
    with connection.cursor() as cur:
        cur.execute(REFRESH_SQL, {"at": at, "rid": current_request_id.get(), "charge": charge})
        created, marked, audited = cur.fetchone()
    return {"created": int(created), "marked": int(marked), "audited": int(audited)}

"""Slice 7 read-model views (docs/07a_production_guardrails_contract.md sections 5.6 and 8.3; Engineer A).

Both views are SECURITY INVOKER, owned by the schema owner, SELECT only for the runtime role (as operations/0001).
They take no locks and store nothing; operations.readmodels / operations.closeout (Engineer C) read them inside the
Slice 5 REPEATABLE READ READ ONLY snapshot.

operations_production_blockers  one row per production blocker, evaluated AS PRODUCTION whatever demo_mode says
    (the panel shows them in both modes; no setting hides them):
      HOLD_POLICY_NOT_APPROVED          entity 'hold-policy': no active production-usable hold policy, or the active
                                        one has no 'incident' rule
      HOLD_UNRELEASABLE                 entity hold code: unreleased hold whose effective policy (placement or
                                        latest effective rebase) is not production-usable
      INSPECTION_TEMPLATE_NOT_APPROVED  entity pattern code: pattern of a non-retired robot's current manifest
                                        with no template usable for 'pre_departure' in production
      PERIODIC_INTERVAL_NOT_APPROVED    entity pattern code: such a pattern with no periodic rule usable in production
      ESCALATION_DEADLINES_NOT_APPROVED entity 'deadline-set': no active deadline set, or it is not owner_approved
    columns: code, entity_ref, detail (plain wording, no sign-off claim).

operations_closeout_runs  one row per execution run (open or closed), raw state only (no grace thresholds, no time
    zone: the close-out page assigns runs to a local day from departed_at with ops_closeout_timezone):
      run_id, run_code, robot_id, robot_code, visit_id, visit_code, departed_at, closed, closed_at,
      stage (operations_open_run_stage.stage for an open run, NULL when closed), custody_kind, custody_verified
      (the robot's current custody, open runs only), assignment_verified (the run's administrative hub -> vehicle
      departure assignment has a CustodyVerification), hub_receipt_at (latest custody event of this run to
      'hub'), post_visit_inspection_code (first inspection of the robot with visit_ref = the visit submitted at or
      after the hub receipt, the reconciliation trigger's rule), latest_reconciliation_outcome.

The Slice 5 view operations_robot_eligibility is NOT replaced here (change request CR-1 in docs/07b): its periodic
reasons must ship together with the Python mirror fleet.services.evaluate_eligibility(robot, at) (Engineer B),
because the Slice 5 eligibility oracle compares the two row by row.
"""

from django.conf import settings
from django.db import migrations

VIEWS = ["operations_production_blockers", "operations_closeout_runs"]

SQL = r"""
CREATE VIEW operations_production_blockers WITH (security_invoker = true) AS
WITH in_use AS (
  SELECT DISTINCT p.id AS pattern_id, p.code AS pattern_code
  FROM fleet_robot r JOIN fleet_configurationmanifest m ON m.id = r.current_manifest_id
  JOIN fleet_manifestpattern p ON p.id = m.pattern_id
  WHERE r.lifecycle <> 'retired'
),
active_policy AS (SELECT fleet_active_hold_policy_id() AS id)
SELECT 'HOLD_POLICY_NOT_APPROVED'::text AS code, 'hold-policy'::text AS entity_ref,
       CASE WHEN ap.id IS NULL THEN 'no active hold release policy'
            WHEN NOT fleet_policy_production_usable(ap.id)
              THEN format('active hold policy v%s is not an approved procedure (synthetic or not fully approved)',
                          (SELECT version FROM fleet_holdpolicy WHERE id = ap.id))
            ELSE format('active hold policy v%s has no incident release rule',
                        (SELECT version FROM fleet_holdpolicy WHERE id = ap.id)) END AS detail
FROM active_policy ap
WHERE ap.id IS NULL OR NOT fleet_policy_production_usable(ap.id)
   OR NOT coalesce((SELECT rules ? 'incident' FROM fleet_holdpolicy WHERE id = ap.id), false)
UNION ALL
SELECT 'HOLD_UNRELEASABLE', h.code,
       format('hold %s (%s) is bound to %s, which is not production-usable; it needs a re-basing decision',
              h.code, h.source_type,
              coalesce('policy v' || (SELECT version FROM fleet_holdpolicy WHERE id = e.policy_id)::text, 'no policy'))
FROM fleet_hold h CROSS JOIN LATERAL (SELECT * FROM fleet_hold_effective_rule(h) LIMIT 1) e
WHERE h.released_at IS NULL AND NOT fleet_policy_production_usable(e.policy_id)
UNION ALL
SELECT 'INSPECTION_TEMPLATE_NOT_APPROVED', u.pattern_code,
       format('configuration %s has no inspection template approved for pre-departure use', u.pattern_code)
FROM in_use u
WHERE NOT EXISTS (SELECT 1 FROM fleet_inspectiontemplate t
                  WHERE t.pattern_id = u.pattern_id AND fleet_template_usable(t.id, 'pre_departure', true))
UNION ALL
SELECT 'PERIODIC_INTERVAL_NOT_APPROVED', u.pattern_code,
       format('configuration %s has no approved periodic inspection interval', u.pattern_code)
FROM in_use u
WHERE fleet_usable_periodic_rule_id(u.pattern_id, true) IS NULL
UNION ALL
SELECT 'ESCALATION_DEADLINES_NOT_APPROVED', 'deadline-set',
       CASE WHEN d.id IS NULL THEN 'no approved escalation deadline set for the active incident policy'
            ELSE format('the active escalation deadline set v%s is synthetic demo data', d.version_no) END
FROM (SELECT 1) one
LEFT JOIN execution_escalationdeadlineset d ON d.id = execution_active_deadline_set_id()
WHERE d.id IS NULL OR d.source <> 'owner_approved';

CREATE VIEW operations_closeout_runs WITH (security_invoker = true) AS
SELECT
  x.id AS run_id,
  x.code AS run_code,
  x.robot_id,
  rb.code AS robot_code,
  x.visit_id,
  v.code AS visit_code,
  d.departed_at,
  (tc.id IS NOT NULL) AS closed,
  tc.recorded_at AS closed_at,
  os.stage,
  os.custody_kind,
  os.custody_verified,
  EXISTS (SELECT 1 FROM execution_custodyevent ce JOIN execution_custodyverification cv ON cv.custody_event_id = ce.id
          WHERE ce.run_id = x.id AND ce.basis = 'administrative') AS assignment_verified,
  hr.recorded_at AS hub_receipt_at,
  (SELECT i.code FROM fleet_inspection i
   WHERE hr.id IS NOT NULL AND i.robot_id = x.robot_id AND i.visit_ref = v.code AND i.submitted_at >= hr.recorded_at
   ORDER BY i.submitted_at, i.id LIMIT 1) AS post_visit_inspection_code,
  (SELECT q.outcome FROM execution_returnreconciliation q WHERE q.run_id = x.id
   ORDER BY q.recorded_at DESC, q.id DESC LIMIT 1) AS latest_reconciliation_outcome
FROM execution_executionrun x
JOIN fleet_robot rb ON rb.id = x.robot_id
JOIN planning_visit v ON v.id = x.visit_id
JOIN readiness_departureevent d ON d.id = x.departure_id
LEFT JOIN execution_tripclosure tc ON tc.run_id = x.id
LEFT JOIN operations_open_run_stage os ON os.run_id = x.id AND tc.id IS NULL
LEFT JOIN LATERAL (
  SELECT ce.id, ce.recorded_at FROM execution_custodyevent ce
  WHERE ce.run_id = x.id AND ce.to_kind = 'hub' ORDER BY ce.seq DESC LIMIT 1
) hr ON true;
"""

REVERSE_SQL = (
    "DROP VIEW IF EXISTS operations_closeout_runs; DROP VIEW IF EXISTS operations_production_blockers;"
)


def _sql() -> str:
    app = settings.DB_APP_ROLE
    grants = "\n".join(
        f"REVOKE ALL ON {v} FROM PUBLIC;\nREVOKE ALL ON {v} FROM {app};\nGRANT SELECT ON {v} TO {app};"
        for v in VIEWS
    )
    return SQL + "\n" + grants


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0002_reservation_conflict_index"),
        ("execution", "0005_s7_incident_release_and_deadlines"),
    ]

    operations = [migrations.RunSQL(_sql(), reverse_sql=REVERSE_SQL)]

"""Slice 7 X-10 performance (perf workflow with the close-out page, contract 07a section 14): the close-out page
measured p95 ~2.6 s on the scale fixture (budget 400 ms). EXPLAIN (ANALYZE, BUFFERS): operations_closeout_runs
joined operations_open_run_stage per run, so for every one of 600 runs the stage view was evaluated over every
robot (execution_open_run_id per robot, ~120,000 calls).

Same view, same columns, order and types: the open run stage is now computed once per statement in a MATERIALIZED
CTE and hash-joined. Still security_invoker, read-only, ops_app SELECT only. Reverse restores 0003's body exactly.
"""

from django.conf import settings
from django.db import migrations

VIEW_SQL = r"""CREATE OR REPLACE VIEW operations_closeout_runs WITH (security_invoker = true) AS
-- the open run stage of every open run, computed ONCE per statement (0006): joined per run it was evaluated
-- for every run over every robot (execution_open_run_id per robot), ~2.6 s on the scale fixture
WITH os AS MATERIALIZED (
  SELECT run_id, stage, custody_kind, custody_verified FROM operations_open_run_stage
)
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
LEFT JOIN os ON os.run_id = x.id AND tc.id IS NULL
LEFT JOIN LATERAL (
  SELECT ce.id, ce.recorded_at FROM execution_custodyevent ce
  WHERE ce.run_id = x.id AND ce.to_kind = 'hub' ORDER BY ce.seq DESC LIMIT 1
) hr ON true;
"""

REVERSE_SQL = r"""CREATE OR REPLACE VIEW operations_closeout_runs WITH (security_invoker = true) AS
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


def _grants() -> str:
    app = settings.DB_APP_ROLE
    v = "operations_closeout_runs"
    return f"REVOKE ALL ON {v} FROM PUBLIC;\nREVOKE ALL ON {v} FROM {app};\nGRANT SELECT ON {v} TO {app};"


class Migration(migrations.Migration):
    dependencies = [("operations", "0005_s7_eligibility_setbased")]

    operations = [migrations.RunSQL(VIEW_SQL + "\n" + _grants(), reverse_sql=REVERSE_SQL + "\n" + _grants())]

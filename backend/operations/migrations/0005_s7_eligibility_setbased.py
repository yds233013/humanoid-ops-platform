"""Slice 7 integration, CR-1 performance (perf workflow on GitHub runners, scale fixture, 200 robots):
``operations_robot_eligibility`` from operations/0004 called the plpgsql ``readiness_periodic_inspection_status``
once per robot (one signer check per candidate inspection), which put the fleet overview at p95 703 ms against
main's 135 ms (budget 400 ms; EXPLAIN (ANALYZE, BUFFERS): ~750 of 760 ms in those calls).

Same view, same columns, order, types, wording and reason order as 0004. Only the periodic reason's source
changes: the set-based periodic status (operations.exceptions.PERIODIC_STATUS_CTES, frozen here as literal SQL;
oracle-tested equal to the function for every robot) at transaction_timestamp() in the current mode, computed
once per statement for every robot (no retired filter: the view lists every robot, as before). Still
security_invoker, STABLE read-only functions only, ops_app SELECT only. Reverse restores 0004's body exactly.
"""

import importlib

from django.conf import settings
from django.db import migrations

VIEW_SQL = r"""
CREATE OR REPLACE VIEW operations_robot_eligibility WITH (security_invoker = true) AS
WITH mode AS MATERIALIZED (SELECT fleet_production_mode() AS production),
pused AS MATERIALIZED (
  SELECT DISTINCT m.pattern_id
  FROM fleet_robot r JOIN fleet_configurationmanifest m ON m.id = r.current_manifest_id
  WHERE true
),
prule AS MATERIALIZED (
  SELECT pu.pattern_id, fleet_usable_periodic_rule_id(pu.pattern_id, mode.production) AS usable_id,
         fleet_active_periodic_rule_id(pu.pattern_id) AS active_id
  FROM pused pu CROSS JOIN mode
),
-- the current readiness_inspector grants, once (a handful of rows), instead of a person/qualification join per
-- candidate inspection: the scale fixture's 1271 candidates cost ~100 ms with stale planner statistics
pinsp AS MATERIALIZED (
  SELECT pp.user_id, q.valid_from, q.valid_to
  FROM planning_qualification q JOIN planning_person pp ON pp.id = q.person_id
  WHERE q.kind = 'readiness_inspector' AND q.revoked_at IS NULL
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
              WHEN transaction_timestamp() >= qi.submitted_at + make_interval(mins => ru.interval_min) THEN 'overdue'
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
      AND EXISTS (SELECT 1 FROM pinsp g
                  WHERE g.user_id = i.performer_id
                    AND g.valid_from <= i.submitted_at AND i.submitted_at <= g.valid_to)
      AND EXISTS (SELECT 1 FROM pinsp g
                  WHERE g.user_id = i.verifier_id
                    AND g.valid_from <= i.submitted_at AND i.submitted_at <= g.valid_to)
    ORDER BY i.submitted_at DESC, i.id DESC LIMIT 1
  ) qi ON true
  WHERE true
)
SELECT
  r.id AS robot_id,
  r.code AS robot_code,
  r.lifecycle,
  r.current_manifest_id AS manifest_id,
  m.pattern_id,
  p.code AS pattern_code,
  cardinality(rs.reasons) = 0 AS eligible,
  rs.reasons
FROM fleet_robot r
LEFT JOIN fleet_configurationmanifest m ON m.id = r.current_manifest_id
LEFT JOIN fleet_manifestpattern p ON p.id = m.pattern_id
LEFT JOIN LATERAL (
  -- approved_profile(): highest version_no whose approval slots are exactly {engineering, safety}
  SELECT cp.* FROM fleet_capabilityprofile cp
  WHERE cp.pattern_id = p.id
    AND (SELECT array_agg(DISTINCT a.slot::text ORDER BY a.slot::text) FROM fleet_capabilityapproval a
         WHERE a.profile_id = cp.id) = ARRAY['engineering', 'safety']
  ORDER BY cp.version_no DESC
  LIMIT 1
) ap ON p.id IS NOT NULL
CROSS JOIN LATERAL (
  SELECT
    CASE WHEN r.lifecycle <> 'in_service' THEN ARRAY[
      'Lifecycle is ' || CASE r.lifecycle WHEN 'commissioning' THEN 'Commissioning'
                                          WHEN 'validation' THEN 'Engineering validation'
                                          WHEN 'in_service' THEN 'In service'
                                          WHEN 'retired' THEN 'Retired'
                                          ELSE r.lifecycle END || ', not In service']
    ELSE '{}'::text[] END
    || CASE WHEN r.current_manifest_id IS NULL THEN ARRAY['No configuration manifest recorded']
       ELSE
         (SELECT CASE WHEN st.status <> 'approved_home' THEN ARRAY[
                   'Configuration ' || p.code || ' is not approved for home use (' || st.status || ')']
                 ELSE '{}'::text[] END
          FROM (SELECT coalesce((SELECT pa.status FROM fleet_patternapproval pa WHERE pa.pattern_id = p.id
                                 ORDER BY pa.id DESC LIMIT 1), 'none') AS status) st)
         || CASE WHEN NOT EXISTS (SELECT 1 FROM fleet_baseauthorization b
                                  WHERE b.robot_id = r.id AND b.pattern_id = p.id AND b.revoked_at IS NULL)
            THEN ARRAY['No base home-use authorization for configuration ' || p.code] ELSE '{}'::text[] END
         || CASE WHEN ap.id IS NULL THEN ARRAY['No approved capability profile for ' || p.code]
            ELSE
              ARRAY(SELECT 'Capability unknown: ' || u.field
                    FROM (VALUES (1, 'stop behaviour', ap.stop_behaviour IS NULL OR ap.stop_behaviour = ''),
                                 (2, 'lift crew', ap.lift_crew IS NULL),
                                 (3, 'max runtime min', ap.max_runtime_min IS NULL),
                                 (4, 'charge turnaround min', ap.charge_turnaround_min IS NULL)
                         ) u(ord, field, unknown)
                    WHERE u.unknown ORDER BY u.ord)
              || CASE WHEN ap.source = 'synthetic_demo' AND NOT execution_demo_mode()
                 THEN ARRAY['Capability profile is synthetic demo data, not engineering-supplied']
                 ELSE '{}'::text[] END
            END
         || ARRAY(SELECT 'Configuration hold ' || h.code || ': ' || h.reason FROM fleet_hold h
                  WHERE h.scope = 'manifest_pattern' AND h.pattern_id = p.id AND h.released_at IS NULL
                    AND h.blocking ORDER BY h.id)
         -- Slice 7 X-3 (CR-1, set-based in 0005): the periodic reason, after the configuration holds (Python order),
         -- from pstat at transaction_timestamp() (= the page snapshot time `at`) in the current mode.
         || coalesce((SELECT ARRAY[CASE ps.state
                       WHEN 'unknown_interval' THEN 'Periodic inspection interval unknown for configuration ' || p.code
                       WHEN 'never_inspected' THEN 'No qualifying periodic inspection on the current configuration'
                       WHEN 'overdue' THEN 'Periodic inspection overdue since '
                         || to_char(ps.due_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || ' UTC'
                     END]
                      FROM pstat ps
                      WHERE ps.robot_id = r.id AND ps.state <> 'due'), '{}'::text[])
       END
    || ARRAY(SELECT 'Robot hold ' || h.code || ' ('
                    || CASE h.source_type WHEN 'inspection' THEN 'Failed inspection'
                                          WHEN 'incident' THEN 'Incident'
                                          WHEN 'maintenance' THEN 'Maintenance'
                                          WHEN 'engineering' THEN 'Engineering restriction'
                                          WHEN 'release_withdrawn' THEN 'Release withdrawn'
                                          WHEN 'manual' THEN 'Manual'
                                          ELSE h.source_type END
                    || '): ' || h.reason
             FROM fleet_hold h
             WHERE h.scope = 'robot' AND h.robot_id = r.id AND h.released_at IS NULL AND h.blocking
             ORDER BY h.id)
    AS reasons
) rs;
"""


def _grants() -> str:
    app = settings.DB_APP_ROLE
    v = "operations_robot_eligibility"
    return f"REVOKE ALL ON {v} FROM PUBLIC;\nREVOKE ALL ON {v} FROM {app};\nGRANT SELECT ON {v} TO {app};"


REVERSE_SQL = importlib.import_module("operations.migrations.0004_s7_eligibility_periodic").VIEW_SQL


class Migration(migrations.Migration):
    dependencies = [("operations", "0004_s7_eligibility_periodic")]

    operations = [migrations.RunSQL(VIEW_SQL + "\n" + _grants(), reverse_sql=REVERSE_SQL + "\n" + _grants())]

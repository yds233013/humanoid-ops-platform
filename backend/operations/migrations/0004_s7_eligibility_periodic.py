"""Slice 7 integration, change request CR-1 (docs/07b_s7_sql_signatures.md section 12; EM ruling: ships as this
new migration, operations/0003 is not modified).

``operations_robot_eligibility`` gains the X-3 periodic inspection reason (contract 07a sections 4.2 and 4.4, EM
decision 7) with the wording and position of ``fleet.services.evaluate_eligibility(robot, at)``: inside the
"has a manifest" branch, after the configuration holds and before the robot holds, one of

    Periodic inspection interval unknown for configuration <pattern>      state unknown_interval
    No qualifying periodic inspection on the current configuration        state never_inspected
    Periodic inspection overdue since <due_at, UTC, to the second> UTC    state overdue
    (nothing)                                                             state due

from ``readiness_periodic_inspection_status(robot, fleet_production_mode(), transaction_timestamp())``. ``at`` is
``transaction_timestamp()``, i.e. the snapshot time of the Slice 5 page (operations.snapshot.read_snapshot), as the
existing view's ``now()``. The mode is the current mode (production unless demo_mode is exactly 'true'), the
same rule as ``fleet.services.production_mode``.

Slice 5 rules kept: ``CREATE OR REPLACE VIEW`` with the same column names, order and types (the dependent view
operations_robot_status keeps working and is not re-created); still ``security_invoker = true`` and owned by the
schema owner; every function called is STABLE and read-only (no locks, no writes), so the view can never block
or delay a writer; ops_app keeps SELECT only (CREATE OR REPLACE keeps the existing privileges; they are
re-asserted below anyway). Reverse restores the Slice 5 body exactly.
"""

from django.conf import settings
from django.db import migrations

VIEW_SQL = r"""
CREATE OR REPLACE VIEW operations_robot_eligibility WITH (security_invoker = true) AS
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
         -- Slice 7 X-3 (CR-1): the periodic reason, after the configuration holds (Python order), evaluated at
         -- transaction_timestamp() (= the page snapshot time `at`) in the current mode.
         || coalesce((SELECT ARRAY[CASE ps.state
                       WHEN 'unknown_interval' THEN 'Periodic inspection interval unknown for configuration ' || p.code
                       WHEN 'never_inspected' THEN 'No qualifying periodic inspection on the current configuration'
                       WHEN 'overdue' THEN 'Periodic inspection overdue since '
                         || to_char(ps.due_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || ' UTC'
                     END]
                      FROM readiness_periodic_inspection_status(r.id, fleet_production_mode(),
                                                                transaction_timestamp()) ps
                      WHERE ps.state <> 'due'), '{}'::text[])
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

REVERSE_SQL = r"""
CREATE OR REPLACE VIEW operations_robot_eligibility WITH (security_invoker = true) AS
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


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0003_s7_views"),
        ("readiness", "0007_s7_guardrails"),
    ]

    operations = [migrations.RunSQL(VIEW_SQL + "\n" + _grants(), reverse_sql=REVERSE_SQL + "\n" + _grants())]

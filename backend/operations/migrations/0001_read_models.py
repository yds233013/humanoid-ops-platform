"""Slice 5 read models in SQL (Engineer A). docs/05_operations_command_center_contract.md sections 0, 2, 4, 5, 6.

NOTHING HERE STORES STATE. Four plain, read-only views derive everything at read time from the existing rows and
the existing SQL derivations (``execution_current_custody``, ``execution_open_run_id``,
``execution_session_state``, ``execution_incident_status``, ``execution_level_rank``, ``execution_demo_mode``).
Every function they call is STABLE/IMMUTABLE and read-only: no nextval, no set_config, no row or advisory locks,
so selecting a view inside the REPEATABLE READ READ ONLY page snapshot can never block or delay a writer.

PRIVILEGES. The views are owned by the schema owner (the migration role), created ``WITH (security_invoker =
true)`` so PostgreSQL checks the CALLER's privileges on every underlying table (ops_app already has SELECT on
them), carry no rules or triggers, and ops_app gets SELECT only (the default privileges of platform_core/0002
would otherwise grant INSERT/UPDATE/DELETE on new relations; they are revoked explicitly).

TIME. ``now()`` inside a view is the transaction start time (``transaction_timestamp()``), i.e. exactly the
snapshot time ``at`` of the page (operations.snapshot.read_snapshot).

VIEWS (columns are listed in the report and in tests/test_operations_sql.py):

operations_robot_eligibility  one row per robot. ``reasons`` re-expresses fleet.services.evaluate_eligibility in
    SQL with the same wording and the same order: lifecycle; no manifest | (pattern decision, base authorization,
    approved capability profile: none | each unknown planning field in REQUIRED_FOR_PLANNING order, then
    synthetic-in-production; configuration holds); robot holds. Holds are listed by id (Python's query has no
    ORDER BY; the oracle test compares the full list). ``eligible`` = no reasons. Oracle-tested (contract 6.1).

operations_open_run_stage  one row per open run (no TripClosure), found per robot with execution_open_run_id. ``awaiting`` (custody hub only):
    post_visit_inspection (no inspection of this robot with visit_ref = the visit submitted after the hub
    receipt, the reconciliation trigger's rule) / reconciliation (latest reconciliation missing or unresolved) /
    trip_closure; else none. ``stage``: departed (no trip event) / at_site / returning (departed_site) /
    after arrived_hub: at_hub (robot not at hub, or awaiting inspection) / awaiting_reconciliation /
    awaiting_closure. ``session_state`` is execution_session_state.

operations_robot_status  one row per non-retired robot (contract section 2): eligibility, custody (LATERAL
    execution_current_custody), open run, active blocking hold codes (robot scope + current pattern scope, by
    id), next confirmed upcoming visit (confirmed, robot on the current revision, not departed, on-site end
    after now(); earliest on-site start, then id), ``awaiting`` and ``status_label`` / ``status_rule`` (first
    match wins, contract section 2 table; a robot not at hub with no open run, unreachable through the
    triggers, is labelled 'Custody unknown' rather than 'At hub').

operations_incident_queue  one row per open incident (status not closed): latest severity decision (recorded_at,
    id), its rank in that decision's policy scale (NULL = unclassified), status, hold active, unacknowledged
    escalations (count, ids, latest), suggested follow-up owner (latest unacknowledged escalation's role/person,
    else ops_lead while 'reported', else safety_reviewer: contract section 4, D1).

SLICE 4 SCHEMA TOUCH (the one the contract allows, section 4 "Historical custody at vehicle arrival"):
``execution_tripevent.custody_seq_at_event`` (nullable integer, NULL on rows recorded before this migration).
The trip-event insert trigger (0004 body, unchanged checks) now FIRST locks the run's robot FOR UPDATE and then the
run (global order robot before run; the record_trip_event command already holds both), and records the robot's
latest custody seq at insert time, overwriting any supplied value. Every custody insert also locks the robot, so
the two streams are totally ordered per robot and the recorded seq is the custody that existed when the vehicle
arrived. The column stays immutable (the table's append-only UPDATE/DELETE/TRUNCATE triggers are untouched).
execution.models has no field for it; Django ignores the extra nullable column, so makemigrations stays clean.

INDEXES (EXPLAIN on the scale fixture, tests/test_operations_sql.py): see INDEXES below.
"""

from django.conf import settings
from django.db import migrations

VIEWS = [
    "operations_robot_eligibility",
    "operations_open_run_stage",
    "operations_robot_status",
    "operations_incident_queue",
]

TRIP_EVENT_SQL = r"""
ALTER TABLE execution_tripevent ADD COLUMN custody_seq_at_event integer NULL;
ALTER TABLE execution_tripevent ADD CONSTRAINT tripevent_custody_seq_positive
  CHECK (custody_seq_at_event IS NULL OR custody_seq_at_event >= 1);

CREATE OR REPLACE FUNCTION execution_trip_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE x execution_executionrun; c record; e execution_custodyevent;
BEGIN
  NEW.recorded_at := clock_timestamp();
  -- Slice 5 (operations/0001): robot FOR UPDATE first, then the run (global order robot before run). The run's
  -- robot_id is immutable (append-only run), so reading it before the run lock is safe.
  PERFORM 1 FROM fleet_robot WHERE id = (SELECT robot_id FROM execution_executionrun WHERE id = NEW.run_id)
    FOR UPDATE;
  x := execution_lock_run(NEW.run_id);
  IF NEW.trip_id IS DISTINCT FROM x.trip_id THEN PERFORM fleet_fail('trip event must be for the run''s trip'); END IF;
  IF NOT execution_is_field_user(NEW.recorded_by_id) THEN
    PERFORM fleet_fail('recording a trip event requires an active ops_lead or field_operator role');
  END IF;
  IF execution_trip_has(NEW.trip_id, 'arrived_hub') THEN
    PERFORM fleet_fail('the trip has already arrived at the hub; no further trip events');
  END IF;
  IF NEW.kind = 'arrived_hub' AND NOT EXISTS (SELECT 1 FROM execution_tripevent WHERE trip_id = NEW.trip_id) THEN
    -- 0003 (EM amendment b): aborted outbound. Directly after departure, only with the robot in VERIFIED
    -- custody of this trip's vehicle (the departure assignment verified by the crew). seq stays 3.
    -- 0004 (EM): or the robot went unaccounted on THIS run (latest custody event to unaccounted, of this run,
    -- with a robot_unaccounted incident on this robot). The vehicle returns; no custody row is written.
    SELECT * INTO c FROM execution_current_custody(x.robot_id);
    SELECT * INTO e FROM execution_custodyevent WHERE id = c.custody_event_id;
    IF NOT ((c.kind = 'vehicle' AND c.verified AND coalesce(e.trip_id = x.trip_id, false))
            OR (c.kind = 'unaccounted' AND coalesce(e.run_id = x.id, false) AND EXISTS (
                  SELECT 1 FROM execution_incident i WHERE i.id = e.incident_id AND i.robot_id = x.robot_id
                    AND i.event_type = 'robot_unaccounted'))) THEN
      PERFORM fleet_fail('aborted outbound: arrived_hub directly after departure needs the robot in verified '
                         'vehicle custody on this trip, or unaccounted on this run with a robot_unaccounted '
                         'incident');
    END IF;
  ELSIF NEW.seq <> (SELECT count(*) FROM execution_tripevent WHERE trip_id = NEW.trip_id) + 1 THEN
    PERFORM fleet_fail('trip events go arrived_site -> departed_site -> arrived_hub, one at a time');
  END IF;
  IF NEW.kind = 'departed_site' THEN
    SELECT * INTO c FROM execution_current_custody(x.robot_id);
    SELECT * INTO e FROM execution_custodyevent WHERE id = c.custody_event_id;
    IF NOT ((c.kind = 'vehicle' AND c.verified AND coalesce(e.trip_id = x.trip_id, false))
            OR EXISTS (SELECT 1 FROM execution_recoverydisposition d WHERE d.run_id = x.id
                       AND d.decision IN ('robot_left_on_site', 'robot_unaccounted'))) THEN
      PERFORM fleet_fail('departed_site needs the robot in verified vehicle custody on this trip, or a '
                         'robot_left_on_site / robot_unaccounted disposition');
    END IF;
  END IF;
  -- Slice 5: the robot's latest custody seq while holding the robot lock (never the caller's value).
  NEW.custody_seq_at_event := (SELECT max(seq) FROM execution_custodyevent WHERE robot_id = x.robot_id);
  RETURN NEW;
END $$;
"""

# 0004's body, restored on reverse (the column is dropped after it).
TRIP_EVENT_REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION execution_trip_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE x execution_executionrun; c record; e execution_custodyevent;
BEGIN
  NEW.recorded_at := clock_timestamp();
  x := execution_lock_run(NEW.run_id);
  IF NEW.trip_id IS DISTINCT FROM x.trip_id THEN PERFORM fleet_fail('trip event must be for the run''s trip'); END IF;
  IF NOT execution_is_field_user(NEW.recorded_by_id) THEN
    PERFORM fleet_fail('recording a trip event requires an active ops_lead or field_operator role');
  END IF;
  IF execution_trip_has(NEW.trip_id, 'arrived_hub') THEN
    PERFORM fleet_fail('the trip has already arrived at the hub; no further trip events');
  END IF;
  IF NEW.kind = 'arrived_hub' AND NOT EXISTS (SELECT 1 FROM execution_tripevent WHERE trip_id = NEW.trip_id) THEN
    SELECT * INTO c FROM execution_current_custody(x.robot_id);
    SELECT * INTO e FROM execution_custodyevent WHERE id = c.custody_event_id;
    IF NOT ((c.kind = 'vehicle' AND c.verified AND coalesce(e.trip_id = x.trip_id, false))
            OR (c.kind = 'unaccounted' AND coalesce(e.run_id = x.id, false) AND EXISTS (
                  SELECT 1 FROM execution_incident i WHERE i.id = e.incident_id AND i.robot_id = x.robot_id
                    AND i.event_type = 'robot_unaccounted'))) THEN
      PERFORM fleet_fail('aborted outbound: arrived_hub directly after departure needs the robot in verified '
                         'vehicle custody on this trip, or unaccounted on this run with a robot_unaccounted '
                         'incident');
    END IF;
  ELSIF NEW.seq <> (SELECT count(*) FROM execution_tripevent WHERE trip_id = NEW.trip_id) + 1 THEN
    PERFORM fleet_fail('trip events go arrived_site -> departed_site -> arrived_hub, one at a time');
  END IF;
  IF NEW.kind = 'departed_site' THEN
    SELECT * INTO c FROM execution_current_custody(x.robot_id);
    SELECT * INTO e FROM execution_custodyevent WHERE id = c.custody_event_id;
    IF NOT ((c.kind = 'vehicle' AND c.verified AND coalesce(e.trip_id = x.trip_id, false))
            OR EXISTS (SELECT 1 FROM execution_recoverydisposition d WHERE d.run_id = x.id
                       AND d.decision IN ('robot_left_on_site', 'robot_unaccounted'))) THEN
      PERFORM fleet_fail('departed_site needs the robot in verified vehicle custody on this trip, or a '
                         'robot_left_on_site / robot_unaccounted disposition');
    END IF;
  END IF;
  RETURN NEW;
END $$;
ALTER TABLE execution_tripevent DROP COLUMN custody_seq_at_event;
"""

VIEWS_SQL = r"""
-- ================================================================ eligibility (fleet.services.evaluate_eligibility)
CREATE VIEW operations_robot_eligibility WITH (security_invoker = true) AS
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

-- ================================================================ open runs and their stage
CREATE VIEW operations_open_run_stage WITH (security_invoker = true) AS
SELECT
  x.id AS run_id,
  x.code AS run_code,
  x.robot_id,
  rb.code AS robot_code,
  x.visit_id,
  v.code AS visit_code,
  x.trip_id,
  x.revision_id,
  d.departed_at,
  rv.on_site_start,
  rv.on_site_end,
  unl.planned_unloading_end,
  te.last_trip_event,
  te.last_trip_event_at,
  te.arrived_hub_at,
  execution_session_state(x.id) AS session_state,
  c.kind AS custody_kind,
  c.verified AS custody_verified,
  c.custody_event_id,
  rec.id AS latest_reconciliation_id,
  rec.outcome AS latest_reconciliation_outcome,
  rec.recorded_at AS latest_reconciliation_at,
  aw.awaiting,
  CASE
    WHEN te.last_trip_event IS NULL THEN 'departed'
    WHEN te.last_trip_event = 'arrived_site' THEN 'at_site'
    WHEN te.last_trip_event = 'departed_site' THEN 'returning'
    WHEN aw.awaiting = 'reconciliation' THEN 'awaiting_reconciliation'
    WHEN aw.awaiting = 'trip_closure' THEN 'awaiting_closure'
    ELSE 'at_hub'
  END AS stage
FROM fleet_robot rb
-- Driven by robots: at most one open run per robot (C3 refuses a departure while one is open), found through
-- the existing definition execution_open_run_id, so the cost grows with the fleet, not with run history.
CROSS JOIN LATERAL (SELECT execution_open_run_id(rb.id) AS run_id) o
JOIN execution_executionrun x ON x.id = o.run_id
JOIN planning_visit v ON v.id = x.visit_id
JOIN planning_visitrevision rv ON rv.id = x.revision_id
JOIN readiness_departureevent d ON d.id = x.departure_id
CROSS JOIN LATERAL (
  SELECT max(rs.ends_at) AS planned_unloading_end FROM planning_reservation rs
  WHERE rs.revision_id = x.revision_id AND rs.purpose = 'unloading'
) unl
CROSS JOIN LATERAL (
  SELECT (SELECT t.kind FROM execution_tripevent t WHERE t.trip_id = x.trip_id ORDER BY t.seq DESC LIMIT 1)
           AS last_trip_event,
         (SELECT max(t.recorded_at) FROM execution_tripevent t WHERE t.trip_id = x.trip_id) AS last_trip_event_at,
         (SELECT t.recorded_at FROM execution_tripevent t WHERE t.trip_id = x.trip_id AND t.kind = 'arrived_hub')
           AS arrived_hub_at
) te
-- LIMIT 1: the function returns exactly one row; without it the planner assumes 1000 (ROWS default) per call
-- and the inflated cost triggers JIT compilation (seconds) on every page.
CROSS JOIN LATERAL (SELECT * FROM execution_current_custody(x.robot_id) LIMIT 1) c
LEFT JOIN execution_custodyevent ce ON ce.id = c.custody_event_id
LEFT JOIN LATERAL (
  SELECT q.id, q.outcome, q.recorded_at FROM execution_returnreconciliation q
  WHERE q.run_id = x.id ORDER BY q.recorded_at DESC, q.id DESC LIMIT 1
) rec ON true
CROSS JOIN LATERAL (
  SELECT CASE
    WHEN c.kind IS DISTINCT FROM 'hub' THEN 'none'
    WHEN NOT EXISTS (SELECT 1 FROM fleet_inspection i
                     WHERE i.robot_id = x.robot_id AND i.visit_ref = v.code
                       AND i.submitted_at > ce.recorded_at) THEN 'post_visit_inspection'
    WHEN rec.outcome IS DISTINCT FROM 'reconciled' THEN 'reconciliation'
    ELSE 'trip_closure'
  END AS awaiting
) aw;

-- ================================================================ derived robot status (contract section 2)
CREATE VIEW operations_robot_status WITH (security_invoker = true) AS
SELECT
  r.id AS robot_id,
  r.code AS robot_code,
  r.lifecycle,
  e.manifest_id,
  e.pattern_id,
  e.pattern_code,
  c.kind AS custody_kind,
  c.verified AS custody_verified,
  c.custody_event_id,
  ce.seq AS custody_seq,
  ce.recorded_at AS custody_since,
  s.run_id AS open_run_id,
  s.run_code AS open_run_code,
  s.visit_id AS open_run_visit_id,
  s.visit_code AS open_run_visit_code,
  s.stage AS open_run_stage,
  hl.hold_codes,
  hl.holds_since,
  e.eligible,
  e.reasons,
  nv.visit_id AS next_visit_id,
  nv.visit_code AS next_visit_code,
  nv.on_site_start AS next_visit_on_site_start,
  coalesce(s.awaiting, 'none') AS awaiting,
  lbl.rule AS status_rule,
  lbl.label AS status_label
FROM fleet_robot r
JOIN operations_robot_eligibility e ON e.robot_id = r.id
CROSS JOIN LATERAL (SELECT * FROM execution_current_custody(r.id) LIMIT 1) c  -- LIMIT 1: see the stage view
LEFT JOIN execution_custodyevent ce ON ce.id = c.custody_event_id
LEFT JOIN operations_open_run_stage s ON s.robot_id = r.id
CROSS JOIN LATERAL (
  SELECT ARRAY(SELECT h.code::text FROM fleet_hold h
               WHERE h.blocking AND h.released_at IS NULL
                 AND ((h.scope = 'robot' AND h.robot_id = r.id)
                      OR (h.scope = 'manifest_pattern' AND h.pattern_id = e.pattern_id))
               ORDER BY h.id) AS hold_codes,
         (SELECT min(h.placed_at) FROM fleet_hold h
          WHERE h.blocking AND h.released_at IS NULL
            AND ((h.scope = 'robot' AND h.robot_id = r.id)
                 OR (h.scope = 'manifest_pattern' AND h.pattern_id = e.pattern_id))) AS holds_since
) hl
LEFT JOIN LATERAL (
  SELECT v.id AS visit_id, v.code AS visit_code, rv.on_site_start
  FROM planning_visitrevision rv
  JOIN planning_visit v ON v.current_revision_id = rv.id
  WHERE rv.robot_id = r.id AND v.status = 'confirmed' AND rv.on_site_end > now()
    AND NOT EXISTS (SELECT 1 FROM readiness_departureevent dep WHERE dep.visit_id = v.id)
  ORDER BY rv.on_site_start, v.id
  LIMIT 1
) nv ON true
CROSS JOIN LATERAL (
  SELECT t.rule, t.label FROM (VALUES
    (1, 'Unaccounted', c.kind = 'unaccounted'),
    (2, 'Custody unknown', c.kind = 'unknown'),
    (3, 'Deployed', s.run_id IS NOT NULL AND c.kind IN ('vehicle', 'site')),
    (4, 'Returned - awaiting ' || CASE s.awaiting WHEN 'post_visit_inspection' THEN 'inspection'
                                                  WHEN 'reconciliation' THEN 'reconciliation'
                                                  ELSE 'trip closure' END,
        s.run_id IS NOT NULL AND c.kind = 'hub'),
    (2, 'Custody unknown', c.kind IS DISTINCT FROM 'hub'),  -- no open run and not at hub: unreachable
    (5, 'Held', cardinality(hl.hold_codes) > 0),
    (6, 'Assigned', nv.visit_id IS NOT NULL),
    (7, 'At hub - not eligible', NOT e.eligible),
    (8, 'At hub - eligible for planning', true)
  ) t(rule, label, hit)
  WHERE t.hit
  LIMIT 1
) lbl
WHERE r.lifecycle <> 'retired';

-- ================================================================ incident queue (contract section 4)
CREATE VIEW operations_incident_queue WITH (security_invoker = true) AS
SELECT
  i.id AS incident_id,
  i.code AS incident_code,
  i.robot_id,
  rb.code AS robot_code,
  i.run_id,
  i.visit_id,
  i.event_type,
  i.occurred_at,
  i.reported_at,
  st.status,
  sev.level AS severity_level,
  sev.policy_id AS severity_policy_id,
  sev.recorded_at AS severity_at,
  execution_level_rank(sev.policy_id, sev.level) AS severity_rank,
  sev.id IS NULL AS unclassified,
  h.id AS hold_id,
  h.code AS hold_code,
  h.released_at IS NULL AS hold_active,
  esc.unacknowledged_count,
  esc.unacknowledged_ids,
  lu.id AS latest_unacknowledged_escalation_id,
  lu.to_role AS latest_unacknowledged_to_role,
  lu.to_person_id AS latest_unacknowledged_to_person_id,
  lu.recorded_at AS latest_unacknowledged_at,
  CASE WHEN lu.id IS NOT NULL THEN lu.to_role::text
       WHEN st.status = 'reported' THEN 'ops_lead'
       ELSE 'safety_reviewer' END AS suggested_owner_role,
  lu.to_person_id AS suggested_owner_person_id,
  pp.code AS suggested_owner_person_code
FROM execution_incident i
JOIN fleet_robot rb ON rb.id = i.robot_id
JOIN fleet_hold h ON h.id = i.hold_id
CROSS JOIN LATERAL (SELECT execution_incident_status(i.id) AS status) st
LEFT JOIN LATERAL (
  SELECT sd.id, sd.level, sd.policy_id, sd.recorded_at FROM execution_severitydecision sd
  WHERE sd.incident_id = i.id ORDER BY sd.recorded_at DESC, sd.id DESC LIMIT 1
) sev ON true
CROSS JOIN LATERAL (
  SELECT count(*)::int AS unacknowledged_count,
         coalesce(array_agg(e.id ORDER BY e.id), '{}'::bigint[]) AS unacknowledged_ids
  FROM execution_incidentescalation e
  WHERE e.incident_id = i.id AND NOT EXISTS (SELECT 1 FROM execution_escalationack k WHERE k.escalation_id = e.id)
) esc
LEFT JOIN LATERAL (
  SELECT e.id, e.to_role, e.to_person_id, e.recorded_at FROM execution_incidentescalation e
  WHERE e.incident_id = i.id AND NOT EXISTS (SELECT 1 FROM execution_escalationack k WHERE k.escalation_id = e.id)
  ORDER BY e.recorded_at DESC, e.id DESC LIMIT 1
) lu ON true
LEFT JOIN planning_person pp ON pp.id = lu.to_person_id
WHERE st.status <> 'closed';
"""

# Supporting indexes (EXPLAIN on the scale fixture): see tests/test_operations_sql.py::test_explain_*.
INDEXES_SQL = r"""
CREATE INDEX IF NOT EXISTS operations_hold_active_robot ON fleet_hold (robot_id, id)
  WHERE released_at IS NULL AND blocking AND scope = 'robot';
CREATE INDEX IF NOT EXISTS operations_hold_active_pattern ON fleet_hold (pattern_id, id)
  WHERE released_at IS NULL AND blocking AND scope = 'manifest_pattern';
CREATE INDEX IF NOT EXISTS operations_inspection_robot_visit ON fleet_inspection (robot_id, visit_ref, submitted_at);
CREATE INDEX IF NOT EXISTS operations_reservation_revision_purpose ON planning_reservation (revision_id, purpose);
CREATE INDEX IF NOT EXISTS operations_reconciliation_run_latest ON execution_returnreconciliation
  (run_id, recorded_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS operations_severity_incident_latest ON execution_severitydecision
  (incident_id, recorded_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS operations_statuschange_incident_latest ON execution_incidentstatuschange
  (incident_id, recorded_at DESC, id DESC);
"""

INDEXES_REVERSE_SQL = r"""
DROP INDEX IF EXISTS operations_hold_active_robot;
DROP INDEX IF EXISTS operations_hold_active_pattern;
DROP INDEX IF EXISTS operations_inspection_robot_visit;
DROP INDEX IF EXISTS operations_reservation_revision_purpose;
DROP INDEX IF EXISTS operations_reconciliation_run_latest;
DROP INDEX IF EXISTS operations_severity_incident_latest;
DROP INDEX IF EXISTS operations_statuschange_incident_latest;
"""


def _grants_sql() -> str:
    app = settings.DB_APP_ROLE
    return "\n".join(
        f"REVOKE ALL ON {v} FROM PUBLIC;\nREVOKE ALL ON {v} FROM {app};\nGRANT SELECT ON {v} TO {app};"
        for v in VIEWS
    )


VIEWS_REVERSE_SQL = "\n".join(f"DROP VIEW IF EXISTS {v};" for v in reversed(VIEWS))


class Migration(migrations.Migration):
    dependencies = [
        ("execution", "0004_unaccounted_outbound_and_withdrawal"),
        ("readiness", "0006_issue_function_planning_gates"),
        ("planning", "0005_guard_versions"),
        ("fleet", "0007_settings_advisory_lock"),
    ]

    operations = [
        migrations.RunSQL(TRIP_EVENT_SQL, reverse_sql=TRIP_EVENT_REVERSE_SQL),
        migrations.RunSQL(INDEXES_SQL, reverse_sql=INDEXES_REVERSE_SQL),
        migrations.RunSQL(VIEWS_SQL + "\n" + _grants_sql(), reverse_sql=VIEWS_REVERSE_SQL),
    ]

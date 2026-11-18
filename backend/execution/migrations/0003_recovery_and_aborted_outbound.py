"""EM-approved amendments to Slice 4 item 3 (Engineer A). Builds on 0002; the two trigger functions are
CREATE OR REPLACE, so 0002's trigger bindings stay and now run these bodies.

(a) RECOVERY WITH NO OPEN RUN. CustodyEvent unaccounted -> hub, basis 'recovered', run/trip/visit NULL, allowed
    only when the robot has no open run and its latest custody is unaccounted (from = previous to), with
    incident = the incident on that latest unaccounted event, an active ops_lead recorder, a receiver holding a
    field role and a non-empty note, under the robot FOR UPDATE lock (as every custody insert). It never opens or
    reopens a run and never touches the incident or its hold: after it custody is a verified hub receipt (C3
    custody satisfied) while the incident hold still blocks planning eligibility and readiness.
    The model CHECK custody_run_required is widened to exactly that shape.

(b) ABORTED OUTBOUND. TripEvent arrived_hub (seq 3, fixed per kind) may be the trip's FIRST event when the robot
    is in verified vehicle custody of the run's trip (the departure assignment verified). After any arrived_hub
    no further trip event is accepted (arrived_site / departed_site refused). The ordinary vehicle -> hub
    handoff, post-visit inspection, reconciliation and closure then apply unchanged.

Also: incident codes must match IC-NNNN, so the hold's source_ref (= the code) is never blank.
"""

from django.conf import settings
from django.db import migrations, models

SQL = r"""
CREATE OR REPLACE FUNCTION execution_custody_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prev execution_custodyevent; x execution_executionrun; rev planning_visitrevision; inc execution_incident;
        open_run bigint;
BEGIN
  NEW.recorded_at := clock_timestamp();
  PERFORM 1 FROM fleet_robot WHERE id = NEW.robot_id FOR UPDATE;
  SELECT * INTO prev FROM execution_custodyevent WHERE robot_id = NEW.robot_id ORDER BY seq DESC LIMIT 1;
  IF prev.id IS NULL THEN
    IF NEW.seq <> 1 THEN PERFORM fleet_fail('custody sequence must continue from the previous event (expected 1)'); END IF;
    IF NEW.from_kind <> 'hub' THEN
      PERFORM fleet_fail(format('wrong custody origin: the robot is at hub (baseline), not %s', NEW.from_kind));
    END IF;
    IF EXISTS (SELECT 1 FROM execution_executionrun WHERE robot_id = NEW.robot_id AND id IS DISTINCT FROM NEW.run_id) THEN
      PERFORM fleet_fail('custody of this robot is unknown (it has runs but no custody history)');
    END IF;
  ELSE
    IF NEW.seq <> prev.seq + 1 THEN
      PERFORM fleet_fail(format('custody sequence must continue from the previous event (expected %s)', prev.seq + 1));
    END IF;
    IF NEW.from_kind <> prev.to_kind THEN
      PERFORM fleet_fail(format('wrong custody origin: the robot is at %s, not %s', prev.to_kind, NEW.from_kind));
    END IF;
  END IF;

  open_run := execution_open_run_id(NEW.robot_id);
  IF NEW.run_id IS NULL THEN
    IF open_run IS NOT NULL THEN PERFORM fleet_fail('custody of a robot with an open run must reference that run'); END IF;
    -- 0003 (EM amendment a): besides an unaccounted report, a recovery unaccounted -> hub needs no run. The
    -- recovered branch below requires ops_lead, a field-role receiver, a note and the incident of the latest
    -- unaccounted event; from = previous to proves the latest custody is unaccounted.
    IF NOT (NEW.to_kind = 'unaccounted'
            OR (NEW.from_kind = 'unaccounted' AND NEW.to_kind = 'hub' AND NEW.basis = 'recovered')) THEN
      PERFORM fleet_fail('only an unaccounted report or a recovery to hub may be recorded without a run');
    END IF;
    IF NEW.trip_id IS NOT NULL OR NEW.visit_id IS NOT NULL THEN
      PERFORM fleet_fail('custody without a run names no trip or visit');
    END IF;
  ELSE
    SELECT * INTO x FROM execution_executionrun WHERE id = NEW.run_id;
    IF x.robot_id IS DISTINCT FROM NEW.robot_id THEN PERFORM fleet_fail('the run is for a different robot'); END IF;
    IF execution_run_closed(x.id) THEN PERFORM fleet_fail('the run is closed; custody can no longer move on it'); END IF;
    IF open_run IS DISTINCT FROM x.id THEN PERFORM fleet_fail('custody must reference the robot''s open run'); END IF;
    IF (NEW.trip_id, NEW.visit_id) IS DISTINCT FROM (x.trip_id, x.visit_id) THEN
      PERFORM fleet_fail('custody trip and visit must be the run''s');
    END IF;
    SELECT * INTO rev FROM planning_visitrevision WHERE id = x.revision_id;
  END IF;

  -- any -> unaccounted: an incident robot_unaccounted on this robot, reported in this transaction
  IF NEW.to_kind = 'unaccounted' THEN
    IF NEW.basis <> 'handoff' THEN PERFORM fleet_fail('a move to unaccounted is recorded as a handoff'); END IF;
    IF NEW.received_by_id IS NOT NULL OR NEW.custodian_id IS NOT NULL THEN
      PERFORM fleet_fail('nobody holds an unaccounted robot: no custodian, no receiver');
    END IF;
    SELECT * INTO inc FROM execution_incident WHERE id = NEW.incident_id;
    IF inc.id IS NULL OR inc.robot_id <> NEW.robot_id OR inc.event_type <> 'robot_unaccounted'
       OR inc.created_txid <> pg_current_xact_id() THEN
      PERFORM fleet_fail('an unaccounted robot needs a robot_unaccounted incident on this robot reported '
                         'in the same transaction');
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.custodian_id IS NULL THEN PERFORM fleet_fail('custody needs a custodian'); END IF;

  -- unaccounted -> hub/site/vehicle: recovered, ops_lead, receiver, acknowledgment note
  IF NEW.from_kind = 'unaccounted' THEN
    IF NEW.basis <> 'recovered' THEN PERFORM fleet_fail('a move out of unaccounted is a recovery (basis recovered)'); END IF;
    IF NOT has_active_role(NEW.recorded_by_id, 'ops_lead') THEN
      PERFORM fleet_fail('recording a recovered robot requires an ops_lead');
    END IF;
    IF NEW.received_by_id IS NULL THEN PERFORM fleet_fail('a recovered robot needs a receiver'); END IF;
    IF NOT execution_person_is_field_user(NEW.received_by_id) THEN
      PERFORM fleet_fail('the receiver must hold an active ops_lead or field_operator role');
    END IF;
    IF NOT execution_nonblank(NEW.note) THEN
      PERFORM fleet_fail('a recovery needs a note (the recovery acknowledgment)');
    END IF;
    IF NEW.incident_id IS NULL OR NEW.incident_id IS DISTINCT FROM prev.incident_id THEN
      PERFORM fleet_fail('a recovery must reference the incident that made the robot unaccounted');
    END IF;
    RETURN NEW;
  END IF;

  -- hub -> vehicle: only the departure trigger's administrative assignment
  IF NEW.from_kind = 'hub' AND NEW.to_kind = 'vehicle' THEN
    IF NEW.basis <> 'administrative' OR pg_trigger_depth() < 2
       OR EXISTS (SELECT 1 FROM execution_custodyevent WHERE run_id = NEW.run_id) THEN
      PERFORM fleet_fail('hub -> vehicle custody is assigned only by recording the departure');
    END IF;
    IF NEW.custodian_id IS DISTINCT FROM rev.lead_id OR NEW.received_by_id IS NOT NULL THEN
      PERFORM fleet_fail('the departure assignment names the revision lead as custodian and no receiver');
    END IF;
    RETURN NEW;
  END IF;

  IF (NEW.from_kind, NEW.to_kind) NOT IN (('vehicle', 'site'), ('site', 'vehicle'), ('vehicle', 'hub')) THEN
    PERFORM fleet_fail(format('custody cannot move from %s to %s', NEW.from_kind, NEW.to_kind));
  END IF;
  IF NEW.basis <> 'handoff' THEN
    PERFORM fleet_fail(format('a %s -> %s move is a handoff', NEW.from_kind, NEW.to_kind));
  END IF;
  IF NOT execution_is_field_user(NEW.recorded_by_id) THEN
    PERFORM fleet_fail('recording a custody handoff requires an active ops_lead or field_operator role');
  END IF;
  IF NOT execution_custody_verified(prev) THEN
    PERFORM fleet_fail('the current custody is not verified: the crew must verify the departure assignment '
                       'before any further move');
  END IF;
  IF NEW.received_by_id IS NULL THEN PERFORM fleet_fail('a handoff needs a receiver'); END IF;
  IF NOT execution_person_is_field_user(NEW.received_by_id) THEN
    PERFORM fleet_fail('the receiver must hold an active ops_lead or field_operator role');
  END IF;
  IF NEW.from_kind = 'vehicle' AND NEW.to_kind = 'site' THEN
    IF NOT execution_trip_has(x.trip_id, 'arrived_site') THEN
      PERFORM fleet_fail('vehicle -> site needs the trip to have arrived at the site');
    END IF;
    IF NOT (execution_is_crew(x.id, NEW.custodian_id) AND execution_is_crew(x.id, NEW.received_by_id)) THEN
      PERFORM fleet_fail('vehicle -> site: custodian and receiver must be field crew of the run');
    END IF;
  ELSIF NEW.from_kind = 'site' AND NEW.to_kind = 'vehicle' THEN
    IF execution_trip_has(x.trip_id, 'departed_site') THEN
      PERFORM fleet_fail('site -> vehicle: the trip has already departed the site');
    END IF;
    IF NOT (execution_is_crew(x.id, NEW.custodian_id) AND execution_is_crew(x.id, NEW.received_by_id)) THEN
      PERFORM fleet_fail('site -> vehicle: custodian and receiver must be field crew of the run');
    END IF;
  ELSIF NEW.from_kind = 'vehicle' AND NEW.to_kind = 'hub' THEN
    IF NOT execution_trip_has(x.trip_id, 'arrived_hub') THEN
      PERFORM fleet_fail('vehicle -> hub needs the trip to have arrived at the hub');
    END IF;
    IF NOT execution_is_crew(x.id, NEW.custodian_id) THEN
      PERFORM fleet_fail('vehicle -> hub: the custodian must be field crew of the run');
    END IF;
  ELSE
    PERFORM fleet_fail(format('custody cannot move from %s to %s', NEW.from_kind, NEW.to_kind));
  END IF;
  RETURN NEW;
END $$;

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
    -- 0003 (EM amendment b): aborted outbound. Directly after departure, only with the robot in VERIFIED
    -- custody of this trip's vehicle (the departure assignment verified by the crew). seq stays 3.
    SELECT * INTO c FROM execution_current_custody(x.robot_id);
    SELECT * INTO e FROM execution_custodyevent WHERE id = c.custody_event_id;
    IF NOT (c.kind = 'vehicle' AND c.verified AND coalesce(e.trip_id = x.trip_id, false)) THEN
      PERFORM fleet_fail('aborted outbound: arrived_hub directly after departure needs the robot in verified '
                         'vehicle custody on this trip');
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

-- Incident codes: a non-empty IC-NNNN code, so the hold's source_ref (which must equal it) is never blank and a
-- blank manual incident hold can never satisfy an incident. Name sorts before execution_incident_guard.
CREATE OR REPLACE FUNCTION execution_incident_code_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.code IS NULL OR NEW.code !~ '^IC-[0-9]{4,}$' THEN
    PERFORM fleet_fail('an incident code must be IC-NNNN (drawn from execution_incident_code_seq)');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_incident_code_guard BEFORE INSERT ON execution_incident
  FOR EACH ROW EXECUTE FUNCTION execution_incident_code_guard();

"""


class Migration(migrations.Migration):
    dependencies = [
        ("execution", "0002_db_enforcement"),
        ("fleet", "0007_settings_advisory_lock"),
        ("planning", "0005_guard_versions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="custodyevent",
            name="custody_run_required",
        ),
        migrations.AddConstraint(
            model_name="custodyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("run__isnull", False),
                    ("to_kind", "unaccounted"),
                    models.Q(
                        ("basis", "recovered"),
                        ("from_kind", "unaccounted"),
                        ("to_kind", "hub"),
                    ),
                    _connector="OR",
                ),
                name="custody_run_required",
            ),
        ),
        migrations.RunSQL(SQL, reverse_sql=migrations.RunSQL.noop),
    ]

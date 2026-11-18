"""EM-approved Slice 4 items (Engineer A). Builds on 0002/0003; every function is CREATE OR REPLACE, so the
existing trigger bindings stay and now run these bodies.

1. UNACCOUNTED DURING OUTBOUND. TripEvent arrived_hub may also be the trip's FIRST event when the robot's latest
   custody event is to_kind 'unaccounted', belongs to this run and references a robot_unaccounted incident on
   this robot (the run's own trip, no prior trip events: unchanged from 0003). It writes no custody row: the
   vehicle's return is not a robot hub receipt. Reconciliation can then only be 'unresolved' and closure stays
   refused until the robot is recovered (unaccounted -> hub, basis 'recovered', on the open run), inspected
   and reconciled. Further trip events after arrived_hub stay refused (0003).
   Reconciliation therefore accepts a hub receipt with basis 'recovered' as well as 'handoff' (always with a
   field-role receiver; the recovered move itself needs ops_lead + note + the incident).
2. INCIDENT POLICY WITHDRAWAL: only an active engineering_lead (matches the Python rule).
"""

from django.db import migrations

SQL = r"""
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
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION execution_reconciliation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE x execution_executionrun; hub execution_custodyevent; insp fleet_inspection; matches boolean;
        latest bigint; vcode text;
BEGIN
  NEW.recorded_at := clock_timestamp();
  x := execution_lock_run(NEW.run_id);
  IF NOT has_active_role(NEW.recorded_by_id, 'ops_lead') THEN
    PERFORM fleet_fail('recording a return reconciliation requires an ops_lead');
  END IF;
  IF NOT readiness_string_list_ok(NEW.discrepancies) THEN
    PERFORM fleet_fail('discrepancies must be a list of distinct, non-empty notes');
  END IF;
  matches := (SELECT array_agg(robot_id ORDER BY robot_id) FROM planning_tripcargo WHERE trip_id = x.trip_id)
             IS NOT DISTINCT FROM ARRAY[x.robot_id];
  IF NEW.cargo_matches IS DISTINCT FROM matches THEN
    PERFORM fleet_fail(format('cargo_matches must be %s', CASE WHEN matches THEN 'True' ELSE 'False' END));
  END IF;
  IF NEW.hub_receipt_id IS NOT NULL THEN
    SELECT * INTO hub FROM execution_custodyevent WHERE id = NEW.hub_receipt_id;
    IF hub.run_id IS DISTINCT FROM x.id OR hub.to_kind <> 'hub' THEN
      PERFORM fleet_fail('hub_receipt must be a custody event of this run that moves the robot to the hub');
    END IF;
  END IF;
  vcode := (SELECT code FROM planning_visit WHERE id = x.visit_id);
  IF NEW.post_visit_inspection_id IS NOT NULL THEN
    SELECT * INTO insp FROM fleet_inspection WHERE id = NEW.post_visit_inspection_id;
    IF insp.robot_id IS DISTINCT FROM x.robot_id OR insp.visit_ref IS DISTINCT FROM vcode THEN
      PERFORM fleet_fail('the post-visit inspection must be of this robot for this visit');
    END IF;
  END IF;
  IF NEW.outcome <> 'reconciled' THEN RETURN NEW; END IF;

  latest := (SELECT custody_event_id FROM execution_current_custody(x.robot_id));
  IF hub.id IS NULL OR latest IS DISTINCT FROM hub.id THEN
    PERFORM fleet_fail('cannot reconcile: the robot''s latest custody is not the hub receipt');
  END IF;
  -- 0004 (EM): a recovered hub receipt on the open run (ops_lead + field receiver + note) also counts.
  IF hub.basis NOT IN ('handoff', 'recovered') OR NOT execution_person_is_field_user(hub.received_by_id) THEN
    PERFORM fleet_fail('cannot reconcile: the hub receipt must be a handoff or a recovery received by a field user');
  END IF;
  IF NOT matches THEN PERFORM fleet_fail('cannot reconcile: the run''s robot is not the trip''s only cargo'); END IF;
  IF EXISTS (SELECT 1 FROM execution_custodyevent e WHERE e.run_id = x.id
             AND NOT (e.basis = 'handoff' OR execution_custody_verified(e))) THEN
    PERFORM fleet_fail('cannot reconcile: a custody event of the run is neither verified nor a handoff');
  END IF;
  IF insp.id IS NULL OR insp.submitted_at <= hub.recorded_at THEN
    PERFORM fleet_fail('cannot reconcile: needs a post-visit inspection of this robot for this visit, '
                       'submitted after the hub receipt');
  END IF;
  IF jsonb_array_length(NEW.discrepancies) > 0 THEN
    PERFORM fleet_fail('cannot reconcile with open discrepancies');
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION execution_incident_policy_withdrawal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := clock_timestamp();
  UPDATE execution_incidentpolicy SET lock_version = lock_version WHERE id = NEW.policy_id;
  IF NOT execution_nonblank(NEW.reason) THEN PERFORM fleet_fail('a withdrawal needs a reason'); END IF;
  IF NOT has_active_role(NEW.withdrawn_by_id, 'engineering_lead') THEN
    PERFORM fleet_fail('withdrawing an incident policy requires an active engineering_lead');
  END IF;
  RETURN NEW;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("execution", "0003_recovery_and_aborted_outbound")]
    operations = [migrations.RunSQL(SQL, reverse_sql=migrations.RunSQL.noop)]

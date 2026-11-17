"""Slice 3 database hardening (Engineer E): one active policy, live-guard departure checks, no re-plan after
departure. Builds on readiness/0002; every function below is CREATE OR REPLACE, so 0002's trigger bindings
stay and now run these bodies.

1. ACTIVE POLICY, NO FALLBACK (EM decision). ``readiness_active_policy_id()`` is the single SQL statement of
   the rule, mirrored by ``readiness.evidence.current_policy()``:
       take the highest-version policy that has EVER become approved (both slots engineering + safety);
       if that policy is withdrawn, there is NO active policy.
   An older approved policy is never used again. A newer version becomes active at the moment its second
   slot is approved (explicit activation). At most one policy is active. A withdrawn policy can never be
   approved again (0002), so "ever approved" == "has both approval rows".

2. AUTHORIZATION INSERT now also requires
   - ``policy_id = readiness_active_policy_id()``;
   - ``guard_snapshot`` with exactly the section 5 keys, every value equal to the LIVE guard rows
     (``readiness_guard_problem``), so a direct-SQL authorization cannot carry a made-up snapshot.
   Locks: site S -> pattern S -> robot S -> persons S (ascending) -> vehicle S -> visit S -> policy S.

3. DEPARTURE INSERT (direct-SQL bypass resistance). In addition to 0002's structural checks (single use, not
   revoked, ``clock_timestamp() < expires_at``, current revision, visit confirmed, current manifest, policy
   not withdrawn, snapshot equal to the authorization's), the trigger recomputes the LIVE guard state from
   the tables and requires the authorization's snapshot to equal it field by field:
       visit_version, revision_id = visit.current_revision, visit_status = 'confirmed',
       site_id + site_version, robot_id + robot_version, manifest_id = robot.current_manifest,
       pattern_id + pattern_version of that manifest, every person in person_versions at its live version
       (and the revision's lead/second and the authorization's recovery-plan people present in it),
       vehicle_id of the revision, policy_id = readiness_active_policy_id() and its lock_version.
   Plus: when ``fleet_setting('demo_mode') <> 'true'`` an authorization issued in mode 'demo' or on a
   synthetic_demo policy is refused; ``recorded_by`` must hold an active ops_lead or field_operator role.
   Locks: site S -> pattern S -> robot S -> persons S (ascending) -> vehicle S -> visit S -> policy S ->
   authorization S (the global order; record_departure already holds all of them).

   Because every hold, consent/assessment/headcount change, qualification grant/revoke, manifest change,
   reschedule, evidence/void/recovery plan, and policy approval/withdrawal bumps one of these guard versions,
   a departure inserted with a copied snapshot after any of them is refused by PostgreSQL alone.

   WHAT THE DATABASE STILL DOES NOT ENFORCE (only the command's evaluate_readiness under locks does):
   - the readiness gates themselves at AUTHORIZATION time: the triggers check the snapshot equals the live
     versions, not that those versions describe a ready world. A direct-SQL authorization issued while a hold
     is already in place (snapshot taken after the hold) passes the triggers; live feasibility (holds,
     consent, qualifications, robot eligibility), reservations, battery level, equipment contents are
     evaluated only by Python.
   - evidence freshness (max_age_min per kind) beyond expires_at, and that expires_at was clipped to the
     evidence ages: the trigger only bounds expires_at by the policy TTL.
   - time-window rules that change without a row update: a qualification or assessment window that lapses,
     role grants (e.g. the on-call person's engineer role), deployment settings other than demo_mode.
   - who may depart beyond the role: the command also allows only ops_lead or the revision's lead/second;
     the trigger accepts any active ops_lead or field_operator.
   - role grants are read, not locked: a role revoked concurrently with a direct-SQL departure may race.

4. NO RE-PLAN AFTER DEPARTURE (lead request). Once a visit has a DepartureEvent:
   - INSERT into planning_visitrevision for it is rejected;
   - UPDATE of planning_visit is rejected if it changes current_revision, or changes status to anything but
     'cancelled'.
   These triggers only READ readiness_departureevent (no locks). Races: plan_visit holds the visit FOR UPDATE
   and the departure trigger takes it FOR SHARE, so they serialize; a raw revision insert that slips past an
   uncommitted departure cannot become current, because the visit UPDATE re-checks after the row lock.
"""

from django.db import migrations

SQL = r"""
-- ================================================================ active policy (single rule)
CREATE OR REPLACE FUNCTION readiness_active_policy_id() RETURNS bigint
LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN readiness_policy_withdrawn(t.id) THEN NULL ELSE t.id END
  FROM (
    SELECT p.id FROM readiness_readinesspolicy p
    JOIN readiness_readinesspolicyapproval a ON a.policy_id = p.id AND a.slot IN ('engineering', 'safety')
    GROUP BY p.id, p.version
    HAVING count(DISTINCT a.slot) = 2
    ORDER BY p.version DESC
    LIMIT 1
  ) t
$$;

-- ================================================================ live guard comparison
-- NULL when the snapshot equals the live guard rows of visit `vid`; otherwise the first difference.
-- `plan_id` is the authorization's recovery plan (its people must be in person_versions) or NULL.
CREATE OR REPLACE FUNCTION readiness_guard_problem(s jsonb, vid bigint, plan_id bigint) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE v planning_visit; r planning_visitrevision; site_ver int; rb fleet_robot; m fleet_configurationmanifest;
        pat_ver int; pv jsonb; k text; val jsonb; live jsonb; pid bigint; pol readiness_readinesspolicy;
        need bigint;
BEGIN
  IF jsonb_typeof(s) IS DISTINCT FROM 'object' OR NOT readiness_keys_exactly(s, ARRAY[
       'visit_id', 'visit_version', 'revision_id', 'visit_status', 'site_id', 'site_version', 'robot_id',
       'robot_version', 'manifest_id', 'pattern_id', 'pattern_version', 'person_versions', 'vehicle_id',
       'policy_id', 'policy_lock_version']) THEN
    RETURN 'the snapshot must have exactly the guard keys of contract section 5';
  END IF;
  SELECT * INTO v FROM planning_visit WHERE id = vid;
  IF s -> 'visit_id' IS DISTINCT FROM to_jsonb(v.id) THEN RETURN 'snapshot is for a different visit'; END IF;
  IF s -> 'revision_id' IS DISTINCT FROM coalesce(to_jsonb(v.current_revision_id), 'null') THEN
    RETURN 'visit revision changed (re-planned)';
  END IF;
  IF v.status <> 'confirmed' OR s -> 'visit_status' IS DISTINCT FROM to_jsonb(v.status) THEN
    RETURN format('visit is %s, snapshot says %s', v.status, s -> 'visit_status');
  END IF;
  IF s -> 'visit_version' IS DISTINCT FROM to_jsonb(v.version) THEN
    RETURN format('visit version %s, snapshot %s (reschedule, evidence or recovery plan changed)',
                  v.version, s -> 'visit_version');
  END IF;
  site_ver := (SELECT version FROM planning_site WHERE id = v.site_id);
  IF s -> 'site_id' IS DISTINCT FROM to_jsonb(v.site_id) OR s -> 'site_version' IS DISTINCT FROM to_jsonb(site_ver) THEN
    RETURN format('site version %s, snapshot %s (consent, assessment or headcount changed)',
                  site_ver, s -> 'site_version');
  END IF;
  SELECT * INTO r FROM planning_visitrevision WHERE id = v.current_revision_id;
  IF r.robot_id IS NULL THEN RETURN 'the current revision has no robot'; END IF;
  SELECT * INTO rb FROM fleet_robot WHERE id = r.robot_id;
  IF s -> 'robot_id' IS DISTINCT FROM to_jsonb(rb.id) OR s -> 'robot_version' IS DISTINCT FROM to_jsonb(rb.version) THEN
    RETURN format('robot version %s, snapshot %s (hold, lifecycle or configuration changed)',
                  rb.version, s -> 'robot_version');
  END IF;
  IF rb.current_manifest_id IS NULL OR s -> 'manifest_id' IS DISTINCT FROM to_jsonb(rb.current_manifest_id) THEN
    RETURN 'robot configuration manifest changed';
  END IF;
  SELECT * INTO m FROM fleet_configurationmanifest WHERE id = rb.current_manifest_id;
  pat_ver := (SELECT version FROM fleet_manifestpattern WHERE id = m.pattern_id);
  IF s -> 'pattern_id' IS DISTINCT FROM to_jsonb(m.pattern_id)
     OR s -> 'pattern_version' IS DISTINCT FROM to_jsonb(pat_ver) THEN
    RETURN format('manifest pattern version %s, snapshot %s (pattern decision or capability profile changed)',
                  pat_ver, s -> 'pattern_version');
  END IF;
  pv := s -> 'person_versions';
  IF jsonb_typeof(pv) IS DISTINCT FROM 'object' THEN RETURN 'person_versions must be an object'; END IF;
  FOR k, val IN SELECT * FROM jsonb_each(pv) LOOP
    IF k !~ '^[1-9][0-9]{0,17}$' THEN RETURN format('person_versions key %s is not a person id', k); END IF;
    live := (SELECT to_jsonb(version) FROM planning_person WHERE id = k::bigint);
    IF live IS NULL OR val IS DISTINCT FROM live THEN
      RETURN format('person %s version %s, snapshot %s (qualification changed)',
                    coalesce((SELECT code FROM planning_person WHERE id = k::bigint), k), live, val);
    END IF;
  END LOOP;
  FOR need IN
    SELECT x FROM unnest(ARRAY[r.lead_id, r.second_id]) x WHERE x IS NOT NULL
    UNION
    SELECT x FROM readiness_recoveryplan p, unnest(ARRAY[p.on_call_engineer_id, p.escalation_contact_id]) x
    WHERE p.id = plan_id
  LOOP
    IF NOT pv ? need::text THEN
      RETURN format('person %s of the plan is missing from person_versions',
                    (SELECT code FROM planning_person WHERE id = need));
    END IF;
  END LOOP;
  IF s -> 'vehicle_id' IS DISTINCT FROM coalesce(to_jsonb(r.vehicle_id), 'null') THEN
    RETURN 'vehicle changed';
  END IF;
  pid := readiness_active_policy_id();
  IF pid IS NULL THEN RETURN 'there is no active readiness policy'; END IF;
  SELECT * INTO pol FROM readiness_readinesspolicy WHERE id = pid;
  IF s -> 'policy_id' IS DISTINCT FROM to_jsonb(pid) THEN
    RETURN format('the active readiness policy is now v%s', pol.version);
  END IF;
  IF s -> 'policy_lock_version' IS DISTINCT FROM to_jsonb(pol.lock_version) THEN
    RETURN format('readiness policy v%s lock version %s, snapshot %s', pol.version, pol.lock_version,
                  s -> 'policy_lock_version');
  END IF;
  RETURN NULL;
END $$;

-- Share-lock the guard rows a snapshot names, in the global lock order, up to (not including) the visit.
CREATE OR REPLACE FUNCTION readiness_lock_snapshot_guards(s jsonb, vid bigint, mid bigint, rid bigint)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  PERFORM 1 FROM planning_site WHERE id = (SELECT site_id FROM planning_visit WHERE id = vid) FOR SHARE;
  PERFORM 1 FROM fleet_manifestpattern
    WHERE id = (SELECT pattern_id FROM fleet_configurationmanifest WHERE id = mid) FOR SHARE;
  PERFORM 1 FROM fleet_robot WHERE id = rid FOR SHARE;
  IF jsonb_typeof(s) = 'object' AND jsonb_typeof(s -> 'person_versions') = 'object' THEN
    PERFORM 1 FROM planning_person
      WHERE id IN (SELECT k::bigint FROM jsonb_object_keys(s -> 'person_versions') k
                   WHERE k ~ '^[1-9][0-9]{0,17}$')
      ORDER BY id FOR SHARE;
  END IF;
  IF jsonb_typeof(s) = 'object' AND jsonb_typeof(s -> 'vehicle_id') = 'number' THEN
    PERFORM 1 FROM planning_vehicle WHERE id::text = s ->> 'vehicle_id' FOR SHARE;
  END IF;
END $$;

-- ================================================================ departure authorization
CREATE OR REPLACE FUNCTION readiness_authorization_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v planning_visit; r planning_visitrevision; m fleet_configurationmanifest; p readiness_readinesspolicy;
        rp readiness_recoveryplan; expected_mode text; e jsonb; ttl int; active bigint; problem text;
BEGIN
  NEW.issued_at := clock_timestamp();
  PERFORM readiness_lock_snapshot_guards(NEW.guard_snapshot, NEW.visit_id, NEW.manifest_id, NEW.robot_id);
  PERFORM 1 FROM planning_visit WHERE id = NEW.visit_id FOR SHARE;
  -- This policy and every newer one: a concurrent activation (approval UPDATEs its row) must serialize.
  PERFORM 1 FROM readiness_readinesspolicy WHERE id >= NEW.policy_id ORDER BY id FOR SHARE;
  SELECT * INTO p FROM readiness_readinesspolicy WHERE id = NEW.policy_id;
  IF readiness_policy_withdrawn(p.id) THEN PERFORM fleet_fail('the readiness policy is withdrawn'); END IF;
  IF NOT readiness_policy_approved(p.id) THEN PERFORM fleet_fail('the readiness policy is not approved'); END IF;
  active := readiness_active_policy_id();
  IF active IS DISTINCT FROM p.id THEN
    PERFORM fleet_fail(format('readiness policy v%s is not the active policy (active: %s)', p.version,
      coalesce('v' || (SELECT version FROM readiness_readinesspolicy WHERE id = active)::text, 'none')));
  END IF;
  expected_mode := CASE WHEN coalesce(fleet_setting('demo_mode'), 'false') = 'true' THEN 'demo' ELSE 'production' END;
  IF NEW.mode IS DISTINCT FROM expected_mode THEN
    PERFORM fleet_fail(format('authorization mode must be %s (from the demo_mode setting)', expected_mode));
  END IF;
  IF NEW.mode = 'production' AND p.source <> 'approved_procedure' THEN
    PERFORM fleet_fail('a synthetic_demo policy can never authorize departure in production');
  END IF;
  SELECT * INTO v FROM planning_visit WHERE id = NEW.visit_id;
  IF v.status <> 'confirmed' THEN PERFORM fleet_fail('only a confirmed visit can be authorized'); END IF;
  SELECT * INTO r FROM planning_visitrevision WHERE id = NEW.revision_id;
  IF r.visit_id <> NEW.visit_id THEN PERFORM fleet_fail('revision belongs to a different visit'); END IF;
  IF v.current_revision_id IS DISTINCT FROM NEW.revision_id THEN
    PERFORM fleet_fail('authorization must be for the visit''s current revision');
  END IF;
  IF r.robot_id IS DISTINCT FROM NEW.robot_id THEN
    PERFORM fleet_fail('authorization robot must be the robot assigned on the revision');
  END IF;
  SELECT * INTO m FROM fleet_configurationmanifest WHERE id = NEW.manifest_id;
  IF m.robot_id <> NEW.robot_id THEN PERFORM fleet_fail('manifest belongs to a different robot'); END IF;
  IF NEW.manifest_id IS DISTINCT FROM (SELECT current_manifest_id FROM fleet_robot WHERE id = NEW.robot_id) THEN
    PERFORM fleet_fail('authorization must be for the robot''s current configuration');
  END IF;
  ttl := (p.requirements ->> 'authorization_ttl_min')::int;
  IF NEW.expires_at <= NEW.issued_at THEN PERFORM fleet_fail('expires_at must be after issued_at'); END IF;
  IF NEW.expires_at > NEW.issued_at + make_interval(mins => ttl) THEN
    PERFORM fleet_fail(format('expires_at exceeds the policy TTL of %s minutes', ttl));
  END IF;
  IF (p.requirements ->> 'recovery_plan_required')::boolean AND NEW.recovery_plan_id IS NULL THEN
    PERFORM fleet_fail('the policy requires a recovery plan');
  END IF;
  IF NEW.recovery_plan_id IS NOT NULL THEN
    SELECT * INTO rp FROM readiness_recoveryplan WHERE id = NEW.recovery_plan_id;
    IF (rp.visit_id, rp.revision_id, rp.manifest_id) IS DISTINCT FROM (NEW.visit_id, NEW.revision_id, NEW.manifest_id) THEN
      PERFORM fleet_fail('recovery plan is for a different visit, revision or configuration');
    END IF;
  END IF;
  IF jsonb_typeof(NEW.evidence_ids) <> 'array' THEN PERFORM fleet_fail('evidence_ids must be a list'); END IF;
  FOR e IN SELECT * FROM jsonb_array_elements(NEW.evidence_ids) LOOP
    IF NOT readiness_positive_int(e) THEN PERFORM fleet_fail('evidence_ids must be a list of ids'); END IF;
    IF NOT EXISTS (
         SELECT 1 FROM readiness_readinessevidence ev
         WHERE ev.id = (e #>> '{}')::bigint AND ev.visit_id = NEW.visit_id AND ev.revision_id = NEW.revision_id
           AND ev.manifest_id = NEW.manifest_id
           AND NOT EXISTS (SELECT 1 FROM readiness_readinessevidencevoid x WHERE x.evidence_id = ev.id)) THEN
      PERFORM fleet_fail(format('evidence %s is not valid, non-voided evidence for this revision and configuration', e));
    END IF;
  END LOOP;
  IF jsonb_typeof(NEW.guard_snapshot) <> 'object'
     OR (NEW.guard_snapshot ->> 'visit_id') IS DISTINCT FROM NEW.visit_id::text
     OR (NEW.guard_snapshot ->> 'revision_id') IS DISTINCT FROM NEW.revision_id::text
     OR (NEW.guard_snapshot ->> 'robot_id') IS DISTINCT FROM NEW.robot_id::text
     OR (NEW.guard_snapshot ->> 'manifest_id') IS DISTINCT FROM NEW.manifest_id::text
     OR (NEW.guard_snapshot ->> 'policy_id') IS DISTINCT FROM NEW.policy_id::text THEN
    PERFORM fleet_fail('guard snapshot does not describe this visit, revision, robot, manifest and policy');
  END IF;
  problem := readiness_guard_problem(NEW.guard_snapshot, NEW.visit_id, NEW.recovery_plan_id);
  IF problem IS NOT NULL THEN
    PERFORM fleet_fail('guard snapshot does not match the live guards: ' || problem);
  END IF;
  RETURN NEW;
END $$;

-- ================================================================ departure event (live guards)
CREATE OR REPLACE FUNCTION readiness_departure_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a readiness_departureauthorization; v planning_visit; p readiness_readinesspolicy; problem text;
BEGIN
  NEW.departed_at := clock_timestamp();
  SELECT * INTO a FROM readiness_departureauthorization WHERE id = NEW.authorization_id;
  PERFORM readiness_lock_snapshot_guards(a.guard_snapshot, a.visit_id, a.manifest_id, a.robot_id);
  PERFORM 1 FROM planning_visit WHERE id = a.visit_id FOR SHARE;
  PERFORM 1 FROM readiness_readinesspolicy WHERE id >= a.policy_id ORDER BY id FOR SHARE;
  PERFORM 1 FROM readiness_departureauthorization WHERE id = a.id FOR SHARE;
  IF (NEW.visit_id, NEW.robot_id) IS DISTINCT FROM (a.visit_id, a.robot_id) THEN
    PERFORM fleet_fail('departure visit and robot must equal the authorization''s');
  END IF;
  IF NOT (has_active_role(NEW.recorded_by_id, 'ops_lead') OR has_active_role(NEW.recorded_by_id, 'field_operator')) THEN
    PERFORM fleet_fail('recording a departure requires an active ops_lead or field_operator role');
  END IF;
  IF EXISTS (SELECT 1 FROM readiness_authorizationrevocation WHERE authorization_id = a.id) THEN
    PERFORM fleet_fail('the authorization was revoked');
  END IF;
  IF clock_timestamp() >= a.expires_at THEN PERFORM fleet_fail('the authorization has expired'); END IF;
  IF EXISTS (SELECT 1 FROM readiness_departureevent WHERE authorization_id = a.id) THEN
    PERFORM fleet_fail('the authorization was already used');
  END IF;
  SELECT * INTO v FROM planning_visit WHERE id = a.visit_id;
  IF v.status <> 'confirmed' THEN PERFORM fleet_fail('the visit is no longer confirmed'); END IF;
  IF v.current_revision_id IS DISTINCT FROM a.revision_id THEN
    PERFORM fleet_fail('the visit was re-planned after authorization');
  END IF;
  IF a.manifest_id IS DISTINCT FROM (SELECT current_manifest_id FROM fleet_robot WHERE id = a.robot_id) THEN
    PERFORM fleet_fail('the robot''s configuration changed after authorization');
  END IF;
  IF readiness_policy_withdrawn(a.policy_id) THEN PERFORM fleet_fail('the readiness policy was withdrawn'); END IF;
  SELECT * INTO p FROM readiness_readinesspolicy WHERE id = a.policy_id;
  IF coalesce(fleet_setting('demo_mode'), 'false') <> 'true'
     AND (a.mode = 'demo' OR p.source = 'synthetic_demo') THEN
    PERFORM fleet_fail('a demo authorization or synthetic_demo policy cannot be used outside demo mode');
  END IF;
  IF NEW.guard_snapshot IS DISTINCT FROM a.guard_snapshot THEN
    PERFORM fleet_fail('the guard snapshot differs from the authorization''s');
  END IF;
  problem := readiness_guard_problem(a.guard_snapshot, a.visit_id, a.recovery_plan_id);
  IF problem IS NOT NULL THEN
    PERFORM fleet_fail('guards changed since authorization: ' || problem);
  END IF;
  RETURN NEW;
END $$;

-- ================================================================ no re-plan after departure
CREATE OR REPLACE FUNCTION readiness_departed_revision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM readiness_departureevent WHERE visit_id = NEW.visit_id) THEN
    PERFORM fleet_fail('the visit has departed; it can no longer be re-planned or rescheduled');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_departed_revision_guard BEFORE INSERT ON planning_visitrevision
  FOR EACH ROW EXECUTE FUNCTION readiness_departed_revision_guard();

CREATE OR REPLACE FUNCTION readiness_departed_visit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.current_revision_id IS DISTINCT FROM OLD.current_revision_id
      OR (NEW.status IS DISTINCT FROM OLD.status AND NEW.status <> 'cancelled'))
     AND EXISTS (SELECT 1 FROM readiness_departureevent WHERE visit_id = NEW.id) THEN
    PERFORM fleet_fail('the visit has departed; it can only be cancelled, not re-planned or re-opened');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_departed_visit_guard BEFORE UPDATE ON planning_visit
  FOR EACH ROW EXECUTE FUNCTION readiness_departed_visit_guard();

-- ================================================================ guard bumps for unversioned writers
-- Releasing a reservation or vehicle trip changes readiness gate 3 but did not touch the visit row (the
-- commands always update the visit too, raw SQL need not). Bump the visit so the snapshot sees it.
CREATE OR REPLACE FUNCTION readiness_bump_visit_of_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE planning_visit SET version = version
   WHERE id = (SELECT visit_id FROM planning_visitrevision WHERE id = NEW.revision_id) AND status <> 'cancelled';
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_reservation_bump AFTER UPDATE ON planning_reservation
  FOR EACH ROW EXECUTE FUNCTION readiness_bump_visit_of_revision();
CREATE TRIGGER readiness_trip_bump AFTER UPDATE ON planning_vehicletrip
  FOR EACH ROW EXECUTE FUNCTION readiness_bump_visit_of_revision();

-- Vehicles carry no guard version; an UPDATE (capacity, restraint kit) bumps every live visit whose current
-- revision uses the vehicle. Lock order vehicle -> visits, as in the global graph.
CREATE OR REPLACE FUNCTION readiness_vehicle_bump() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE planning_visit v SET version = v.version FROM planning_visitrevision r
   WHERE r.id = v.current_revision_id AND r.vehicle_id = NEW.id AND v.status <> 'cancelled';
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_vehicle_bump AFTER UPDATE ON planning_vehicle
  FOR EACH ROW EXECUTE FUNCTION readiness_vehicle_bump();
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS readiness_vehicle_bump ON planning_vehicle;
DROP TRIGGER IF EXISTS readiness_trip_bump ON planning_vehicletrip;
DROP TRIGGER IF EXISTS readiness_reservation_bump ON planning_reservation;
DROP FUNCTION IF EXISTS readiness_vehicle_bump();
DROP FUNCTION IF EXISTS readiness_bump_visit_of_revision();
DROP TRIGGER IF EXISTS readiness_departed_visit_guard ON planning_visit;
DROP TRIGGER IF EXISTS readiness_departed_revision_guard ON planning_visitrevision;
DROP FUNCTION IF EXISTS readiness_departed_visit_guard();
DROP FUNCTION IF EXISTS readiness_departed_revision_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0002_db_enforcement"),
        ("planning", "0005_guard_versions"),
        ("fleet", "0006_positive_values"),
    ]
    operations = [migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL)]

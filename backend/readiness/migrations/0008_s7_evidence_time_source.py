"""Slice 7 integration: deterministic time source for the readiness evidence guard (release blocker).

ROOT CAUSE (tests/test_operations_sql.py::test_views_equal_python_on_random_world[29], "observed_at cannot be in
the future" on an equipment_check row). The failing INSERT sent ``observed_at`` as DEFAULT (no caller value, no
host clock involved): the column default is ``statement_timestamp()`` (Django ``Now()`` on PostgreSQL). The guard
then compared it with a SECOND, later read of the wall clock, ``clock_timestamp()``. The two agree only if the
database wall clock never steps backwards between the statement start and the trigger; on a loaded host whose
database runs in a VM with clock synchronisation (the shared Docker host) it can, and the row was refused. The
failure therefore depended on wall-clock behaviour, not on the seed.

FIX. readiness_evidence_guard reads its time once: ``now_ts := greatest(clock_timestamp(), statement_timestamp())``
and uses it for both bounds (future, 24 hours). A value the database stamped itself for this statement is never
"in the future" of that same statement, whatever the clock does; a caller-supplied future value is refused as
before (same refusal text). readiness.evidence.record_evidence stamps the inspection-derived default as
``LEAST(inspection.submitted_at, statement_timestamp())`` in the INSERT itself instead of a value read in an
earlier statement. Everything else in the function is the readiness/0007 body, unchanged. Reverse restores it.
"""

from django.db import migrations

SQL = r"""
CREATE OR REPLACE FUNCTION readiness_evidence_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v planning_visit; r planning_visitrevision; m fleet_configurationmanifest; i fleet_inspection;
        problem text; tpl fleet_inspectiontemplate; now_ts timestamptz;
BEGIN
  PERFORM 1 FROM fleet_robot WHERE id = NEW.robot_id FOR SHARE;
  -- Slice 7 X-8: signer persons S BEFORE the visit UPDATE (order robot -> persons -> visit).
  IF NEW.kind = 'pre_departure_inspection' AND NEW.inspection_id IS NOT NULL THEN
    PERFORM 1 FROM planning_person WHERE user_id IN (
      SELECT unnest(ARRAY[x.performer_id, x.verifier_id]) FROM fleet_inspection x WHERE x.id = NEW.inspection_id)
      ORDER BY id FOR SHARE;
  END IF;
  IF (SELECT status FROM planning_visit WHERE id = NEW.visit_id) = 'cancelled' THEN
    PERFORM fleet_fail('evidence cannot be recorded for a cancelled visit');
  END IF;
  UPDATE planning_visit SET version = version WHERE id = NEW.visit_id;  -- lock + bump the guard version
  SELECT * INTO v FROM planning_visit WHERE id = NEW.visit_id;
  SELECT * INTO r FROM planning_visitrevision WHERE id = NEW.revision_id;
  IF r.visit_id <> NEW.visit_id THEN PERFORM fleet_fail('revision belongs to a different visit'); END IF;
  IF v.current_revision_id IS DISTINCT FROM NEW.revision_id THEN
    PERFORM fleet_fail('evidence must be bound to the visit''s current revision');
  END IF;
  IF r.robot_id IS DISTINCT FROM NEW.robot_id THEN
    PERFORM fleet_fail('evidence robot must be the robot assigned on the revision');
  END IF;
  SELECT * INTO m FROM fleet_configurationmanifest WHERE id = NEW.manifest_id;
  IF m.robot_id <> NEW.robot_id THEN PERFORM fleet_fail('manifest belongs to a different robot'); END IF;
  IF NEW.manifest_id IS DISTINCT FROM (SELECT current_manifest_id FROM fleet_robot WHERE id = NEW.robot_id) THEN
    PERFORM fleet_fail('evidence must be bound to the robot''s current configuration');
  END IF;
  -- Slice 7 integration (release blocker, random world 29): ONE time source for both bounds. observed_at's
  -- column default is statement_timestamp(); comparing it with a separate, later clock_timestamp() read assumed
  -- the wall clock never steps back between the two (it can: NTP / VM time sync). The bound is the later of the
  -- statement's own start and the clock now, so a database-stamped value is never "in the future" of itself;
  -- a caller-supplied future value is still refused.
  now_ts := greatest(clock_timestamp(), statement_timestamp());
  IF NEW.observed_at > now_ts THEN PERFORM fleet_fail('observed_at cannot be in the future'); END IF;
  IF NEW.observed_at < now_ts - interval '24 hours' THEN
    PERFORM fleet_fail('observed_at is more than 24 hours ago');
  END IF;
  IF NEW.kind = 'pre_departure_inspection' THEN
    IF NEW.inspection_id IS NULL THEN PERFORM fleet_fail('pre-departure inspection evidence needs an inspection'); END IF;
    SELECT * INTO i FROM fleet_inspection WHERE id = NEW.inspection_id;
    IF i.robot_id <> NEW.robot_id OR i.manifest_id <> NEW.manifest_id THEN
      PERFORM fleet_fail('inspection is for a different robot or configuration');
    END IF;
    IF i.visit_ref IS DISTINCT FROM v.code THEN PERFORM fleet_fail('inspection was not recorded for this visit'); END IF;
    IF NEW.result = 'pass' AND NOT (i.result = 'PASS' AND i.complete) THEN
      PERFORM fleet_fail('a passing pre-departure inspection needs a complete PASS inspection');
    END IF;
    IF NEW.result = 'pass' THEN
      -- Slice 7 X-1 (R1-2): early refusal in production; reads the mode WITHOUT the settings lock on purpose
      -- (never authoritative: readiness_issue_authorization re-proves it under the lock). fail is always accepted.
      SELECT * INTO tpl FROM fleet_inspectiontemplate WHERE id = i.template_id;
      IF fleet_production_mode() AND NOT fleet_template_usable(i.template_id, 'pre_departure', true) THEN
        PERFORM fleet_fail(format('passing pre-departure evidence refused: inspection %s uses template %s, which is not '
                                  'approved for pre-departure use in production', i.code, tpl.version));
      END IF;
      -- Slice 7 X-8 (both modes): both signers covered at the inspection time and unrevoked now.
      problem := planning_inspection_signer_problem(i.id);
      IF problem IS NOT NULL THEN PERFORM fleet_fail('passing pre-departure evidence refused: ' || problem); END IF;
    END IF;
  ELSIF NEW.inspection_id IS NOT NULL THEN
    PERFORM fleet_fail('only pre-departure inspection evidence may reference an inspection');
  END IF;
  problem := readiness_details_problem(NEW.kind, NEW.details);
  IF problem IS NOT NULL THEN PERFORM fleet_fail(problem); END IF;
  RETURN NEW;
END $$;
"""

REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION readiness_evidence_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v planning_visit; r planning_visitrevision; m fleet_configurationmanifest; i fleet_inspection;
        problem text; tpl fleet_inspectiontemplate;
BEGIN
  PERFORM 1 FROM fleet_robot WHERE id = NEW.robot_id FOR SHARE;
  -- Slice 7 X-8: signer persons S BEFORE the visit UPDATE (order robot -> persons -> visit).
  IF NEW.kind = 'pre_departure_inspection' AND NEW.inspection_id IS NOT NULL THEN
    PERFORM 1 FROM planning_person WHERE user_id IN (
      SELECT unnest(ARRAY[x.performer_id, x.verifier_id]) FROM fleet_inspection x WHERE x.id = NEW.inspection_id)
      ORDER BY id FOR SHARE;
  END IF;
  IF (SELECT status FROM planning_visit WHERE id = NEW.visit_id) = 'cancelled' THEN
    PERFORM fleet_fail('evidence cannot be recorded for a cancelled visit');
  END IF;
  UPDATE planning_visit SET version = version WHERE id = NEW.visit_id;  -- lock + bump the guard version
  SELECT * INTO v FROM planning_visit WHERE id = NEW.visit_id;
  SELECT * INTO r FROM planning_visitrevision WHERE id = NEW.revision_id;
  IF r.visit_id <> NEW.visit_id THEN PERFORM fleet_fail('revision belongs to a different visit'); END IF;
  IF v.current_revision_id IS DISTINCT FROM NEW.revision_id THEN
    PERFORM fleet_fail('evidence must be bound to the visit''s current revision');
  END IF;
  IF r.robot_id IS DISTINCT FROM NEW.robot_id THEN
    PERFORM fleet_fail('evidence robot must be the robot assigned on the revision');
  END IF;
  SELECT * INTO m FROM fleet_configurationmanifest WHERE id = NEW.manifest_id;
  IF m.robot_id <> NEW.robot_id THEN PERFORM fleet_fail('manifest belongs to a different robot'); END IF;
  IF NEW.manifest_id IS DISTINCT FROM (SELECT current_manifest_id FROM fleet_robot WHERE id = NEW.robot_id) THEN
    PERFORM fleet_fail('evidence must be bound to the robot''s current configuration');
  END IF;
  IF NEW.observed_at > clock_timestamp() THEN PERFORM fleet_fail('observed_at cannot be in the future'); END IF;
  IF NEW.observed_at < clock_timestamp() - interval '24 hours' THEN
    PERFORM fleet_fail('observed_at is more than 24 hours ago');
  END IF;
  IF NEW.kind = 'pre_departure_inspection' THEN
    IF NEW.inspection_id IS NULL THEN PERFORM fleet_fail('pre-departure inspection evidence needs an inspection'); END IF;
    SELECT * INTO i FROM fleet_inspection WHERE id = NEW.inspection_id;
    IF i.robot_id <> NEW.robot_id OR i.manifest_id <> NEW.manifest_id THEN
      PERFORM fleet_fail('inspection is for a different robot or configuration');
    END IF;
    IF i.visit_ref IS DISTINCT FROM v.code THEN PERFORM fleet_fail('inspection was not recorded for this visit'); END IF;
    IF NEW.result = 'pass' AND NOT (i.result = 'PASS' AND i.complete) THEN
      PERFORM fleet_fail('a passing pre-departure inspection needs a complete PASS inspection');
    END IF;
    IF NEW.result = 'pass' THEN
      -- Slice 7 X-1 (R1-2): early refusal in production; reads the mode WITHOUT the settings lock on purpose
      -- (never authoritative: readiness_issue_authorization re-proves it under the lock). fail is always accepted.
      SELECT * INTO tpl FROM fleet_inspectiontemplate WHERE id = i.template_id;
      IF fleet_production_mode() AND NOT fleet_template_usable(i.template_id, 'pre_departure', true) THEN
        PERFORM fleet_fail(format('passing pre-departure evidence refused: inspection %s uses template %s, which is not '
                                  'approved for pre-departure use in production', i.code, tpl.version));
      END IF;
      -- Slice 7 X-8 (both modes): both signers covered at the inspection time and unrevoked now.
      problem := planning_inspection_signer_problem(i.id);
      IF problem IS NOT NULL THEN PERFORM fleet_fail('passing pre-departure evidence refused: ' || problem); END IF;
    END IF;
  ELSIF NEW.inspection_id IS NOT NULL THEN
    PERFORM fleet_fail('only pre-departure inspection evidence may reference an inspection');
  END IF;
  problem := readiness_details_problem(NEW.kind, NEW.details);
  IF problem IS NOT NULL THEN PERFORM fleet_fail(problem); END IF;
  RETURN NEW;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("readiness", "0007_s7_guardrails")]

    operations = [migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL)]

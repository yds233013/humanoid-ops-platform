"""Engineering review fixes (Slice 1 closure).

1. Placing a hold NEVER depends on policy approval (fail closed). An absent/unapproved policy only blocks
   RELEASE. A hold's required slots must equal the policy's rule for its source, or be empty when the
   policy has no rule (then the hold is unreleasable).
2. Inspection -> hold causality: an inspection with an observed robot-condition failure must reference a
   hold that was created FOR THAT INSPECTION (source_type 'inspection', source_ref = inspection code,
   same robot, blocking, open) IN THE SAME TRANSACTION. The database stamps every hold with the top-level
   transaction id (created_txid = pg_current_xact_id(), forced by the trigger, immutable), which is stable
   across savepoints. One hold per inspection. An inspection without such a failure may not reference a hold.
"""

from django.db import migrations

SQL = r"""
ALTER TABLE fleet_hold ADD COLUMN IF NOT EXISTS created_txid xid8 NOT NULL DEFAULT pg_current_xact_id();

CREATE OR REPLACE FUNCTION fleet_hold_release_problem(h fleet_hold) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
  rule jsonb; s jsonb; cur_manifest bigint;
BEGIN
  IF h.policy_id IS NULL OR jsonb_array_length(h.required_slots) = 0 THEN
    RETURN 'no release rule exists for this hold; an approved policy must provide one';
  END IF;
  IF NOT fleet_policy_usable(h.policy_id) THEN
    RETURN 'the hold policy is a draft and drafts are not allowed in this deployment';
  END IF;
  rule := (SELECT rules -> h.source_type FROM fleet_holdpolicy WHERE id = h.policy_id);
  IF h.scope = 'robot' THEN
    cur_manifest := (SELECT current_manifest_id FROM fleet_robot WHERE id = h.robot_id);
  END IF;
  FOR s IN SELECT * FROM jsonb_array_elements(h.required_slots) LOOP
    IF NOT EXISTS (
      SELECT 1 FROM fleet_releaseapproval a
      WHERE a.hold_id = h.id AND a.slot = s ->> 'slot'
        AND NOT EXISTS (SELECT 1 FROM fleet_releaseapprovalwithdrawal w WHERE w.approval_id = a.id)
        AND (h.scope <> 'robot' OR a.manifest_id IS NOT DISTINCT FROM cur_manifest)
    ) THEN
      RETURN format('slot %s has no valid approval on the current configuration', s ->> 'slot');
    END IF;
  END LOOP;
  IF coalesce((rule ->> 'requires_newer_pass_inspection')::boolean, false) AND h.scope = 'robot' AND NOT EXISTS (
      SELECT 1 FROM fleet_inspection i WHERE i.robot_id = h.robot_id AND i.result = 'PASS'
        AND i.submitted_at > h.placed_at AND i.manifest_id IS NOT DISTINCT FROM cur_manifest) THEN
    RETURN 'needs a newer PASS inspection on the current configuration';
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION fleet_hold_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE problem text; slots jsonb;
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('holds cannot be deleted; release them'); END IF;
  IF TG_OP = 'INSERT' THEN
    -- No policy-approval requirement here: restriction must always be possible.
    IF NEW.policy_id IS NULL THEN
      slots := '[]'::jsonb;
    ELSE
      slots := coalesce((SELECT rules -> NEW.source_type -> 'slots' FROM fleet_holdpolicy
                         WHERE id = NEW.policy_id), '[]'::jsonb);
    END IF;
    IF NEW.required_slots IS DISTINCT FROM slots THEN
      PERFORM fleet_fail('required slots must be copied exactly from the policy');
    END IF;
    IF NEW.released_at IS NOT NULL THEN PERFORM fleet_fail('a hold cannot be created released'); END IF;
    NEW.created_txid := pg_current_xact_id();
  ELSE
    NEW.created_txid := OLD.created_txid;
    IF OLD.released_at IS NOT NULL THEN PERFORM fleet_fail('hold already released'); END IF;
    IF NEW.released_at IS NULL OR NEW.released_by_id IS NULL THEN
      PERFORM fleet_fail('the only allowed change to a hold is its release');
    END IF;
    IF (NEW.code, NEW.scope, NEW.robot_id, NEW.pattern_id, NEW.blocking, NEW.source_type, NEW.source_ref,
        NEW.reason, NEW.policy_id, NEW.required_slots, NEW.manifest_at_placement_id, NEW.placed_by_id,
        NEW.placed_at)
       IS DISTINCT FROM
       (OLD.code, OLD.scope, OLD.robot_id, OLD.pattern_id, OLD.blocking, OLD.source_type, OLD.source_ref,
        OLD.reason, OLD.policy_id, OLD.required_slots, OLD.manifest_at_placement_id, OLD.placed_by_id,
        OLD.placed_at) THEN
      PERFORM fleet_fail('the only allowed change to a hold is its release');
    END IF;
    problem := fleet_hold_release_problem(NEW);
    IF problem IS NOT NULL THEN PERFORM fleet_fail('cannot release: ' || problem); END IF;
  END IF;
  IF NEW.scope = 'robot' THEN
    UPDATE fleet_robot SET version = version WHERE id = NEW.robot_id;
  ELSE
    PERFORM fleet_bump_pattern(NEW.pattern_id);
  END IF;
  RETURN NEW;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_inspection_hold_per_inspection
  ON fleet_hold (source_ref) WHERE source_type = 'inspection';

CREATE OR REPLACE FUNCTION fleet_inspection_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  t fleet_inspectiontemplate; item jsonb; ans text;
  robot_fail boolean := false; any_fail boolean := false; all_answered boolean := true;
  expected_complete boolean; expected_result text; m_pattern bigint;
BEGIN
  SELECT * INTO t FROM fleet_inspectiontemplate WHERE id = NEW.template_id;
  IF NEW.manifest_id IS DISTINCT FROM (SELECT current_manifest_id FROM fleet_robot WHERE id = NEW.robot_id) THEN
    PERFORM fleet_fail('inspection must be on the robot''s current configuration');
  END IF;
  m_pattern := (SELECT pattern_id FROM fleet_configurationmanifest WHERE id = NEW.manifest_id);
  IF t.pattern_id <> m_pattern THEN PERFORM fleet_fail('template is for a different configuration pattern'); END IF;
  FOR item IN SELECT * FROM jsonb_array_elements(t.items) LOOP
    ans := NEW.answers ->> (item ->> 'id');
    IF ans IS NULL THEN all_answered := false;
    ELSIF ans = 'fail' THEN
      any_fail := true;
      IF (item ->> 'robot_condition')::boolean THEN robot_fail := true; END IF;
    ELSIF ans <> 'pass' THEN PERFORM fleet_fail('answers must be pass or fail');
    END IF;
  END LOOP;
  expected_complete := all_answered AND NEW.performer_id IS NOT NULL AND NEW.verifier_id IS NOT NULL
                       AND NEW.performer_id <> NEW.verifier_id;
  expected_result := CASE WHEN any_fail THEN 'FAIL' WHEN expected_complete THEN 'PASS' ELSE 'INCOMPLETE' END;
  IF NEW.result <> expected_result OR NEW.complete <> expected_complete THEN
    PERFORM fleet_fail(format('inspection result must be %s (complete=%s)', expected_result, expected_complete));
  END IF;
  IF robot_fail THEN
    IF NOT EXISTS (
      SELECT 1 FROM fleet_hold h
      WHERE h.id = NEW.hold_id AND h.scope = 'robot' AND h.robot_id = NEW.robot_id AND h.blocking
        AND h.released_at IS NULL AND h.source_type = 'inspection' AND h.source_ref = NEW.code
        AND h.created_txid = pg_current_xact_id()
    ) THEN
      PERFORM fleet_fail('an observed robot-condition failure must place its own blocking robot hold '
                         'in the same transaction');
    END IF;
  ELSIF NEW.hold_id IS NOT NULL THEN
    PERFORM fleet_fail('only an inspection with a robot-condition failure may reference a hold');
  END IF;
  RETURN NEW;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("fleet", "0003_hold_policy_optional")]
    operations = [migrations.RunSQL(SQL, reverse_sql=migrations.RunSQL.noop)]

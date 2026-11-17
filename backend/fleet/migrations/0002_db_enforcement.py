"""Fleet invariants enforced IN THE DATABASE, so the runtime role cannot bypass them with direct SQL.

- append-only tables (no UPDATE/DELETE/TRUNCATE)
- robot: identity immutable, lifecycle transitions validated, version always bumped by the database
- pattern: only its guard version may change
- base authorization / hold: only the single revoke / release transition, once
- hold insert: policy must be approved (or drafts allowed in this deployment); required slots copied
  exactly from the policy; guard versions bumped
- hold release: every required slot filled by a valid, non-withdrawn approval bound to the robot's
  CURRENT manifest; newer PASS inspection when the policy requires it
- release approval insert: slot exists, role allowed for slot, approver holds that role now,
  distinct / independent approvers per policy, manifest binding
- inspection insert: result recomputed from template + answers; INCOMPLETE can never be PASS;
  any observed robot-condition failure must reference a blocking robot hold for that robot
- capability / policy / pattern decisions: approver must hold the matching role
- deployment settings: readable by the runtime role, writable only by the owner
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE

APPEND_ONLY = [
    "fleet_patternapproval",
    "fleet_capabilityprofile",
    "fleet_capabilityapproval",
    "fleet_configurationmanifest",
    "fleet_holdpolicy",
    "fleet_holdpolicyapproval",
    "fleet_releaseapproval",
    "fleet_releaseapprovalwithdrawal",
    "fleet_inspectiontemplate",
    "fleet_inspection",
    "fleet_eligibilitycheck",
]

SQL = """
-- ---------- helpers ----------
CREATE OR REPLACE FUNCTION has_active_role(uid bigint, r text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM accounts_rolegrant g WHERE g.user_id = uid AND g.role = r
    AND g.revoked_at IS NULL AND g.valid_from <= clock_timestamp()
    AND (g.valid_to IS NULL OR g.valid_to > clock_timestamp()))
$$;

CREATE OR REPLACE FUNCTION fleet_setting(k text) RETURNS text
LANGUAGE sql STABLE AS $$ SELECT value FROM fleet_deploymentsetting WHERE key = k $$;

CREATE OR REPLACE FUNCTION fleet_policy_usable(pid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT (SELECT count(DISTINCT slot) FROM fleet_holdpolicyapproval WHERE policy_id = pid) = 2
      OR coalesce(fleet_setting('allow_draft_policies'), 'false') = 'true'
$$;

CREATE OR REPLACE FUNCTION fleet_fail(msg text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '%', msg USING ERRCODE = 'check_violation'; END $$;

-- ---------- append-only ----------
{append_only}

-- ---------- manifest pattern: only the guard version may change, and only upward ----------
CREATE OR REPLACE FUNCTION fleet_pattern_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('manifest patterns cannot be deleted'); END IF;
  IF (NEW.code, NEW.hardware_rev, NEW.release, NEW.firmware_set, NEW.created_at)
     IS DISTINCT FROM (OLD.code, OLD.hardware_rev, OLD.release, OLD.firmware_set, OLD.created_at) THEN
    PERFORM fleet_fail('manifest pattern identity is immutable');
  END IF;
  NEW.version := OLD.version + 1;
  RETURN NEW;
END $$;
CREATE TRIGGER pattern_guard BEFORE UPDATE OR DELETE ON fleet_manifestpattern
  FOR EACH ROW EXECUTE FUNCTION fleet_pattern_guard();

CREATE OR REPLACE FUNCTION fleet_bump_pattern(pid bigint) RETURNS void LANGUAGE sql AS $$
  UPDATE fleet_manifestpattern SET version = version WHERE id = pid;
  UPDATE fleet_robot r SET version = r.version FROM fleet_configurationmanifest m
   WHERE r.current_manifest_id = m.id AND m.pattern_id = pid;
$$;

-- ---------- robot ----------
CREATE OR REPLACE FUNCTION fleet_robot_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ok boolean;
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('robots cannot be deleted; retire them'); END IF;
  IF (NEW.code, NEW.serial, NEW.registered_by_id, NEW.registered_at)
     IS DISTINCT FROM (OLD.code, OLD.serial, OLD.registered_by_id, OLD.registered_at) THEN
    PERFORM fleet_fail('robot identity is immutable');
  END IF;
  IF NEW.lifecycle <> OLD.lifecycle THEN
    ok := (OLD.lifecycle, NEW.lifecycle) IN (
      ('commissioning','validation'), ('validation','commissioning'), ('validation','in_service'),
      ('in_service','validation'), ('commissioning','retired'), ('validation','retired'),
      ('in_service','retired'));
    IF NOT ok THEN
      PERFORM fleet_fail(format('lifecycle %s -> %s is not allowed', OLD.lifecycle, NEW.lifecycle));
    END IF;
  END IF;
  IF OLD.lifecycle = 'retired' AND NEW.current_manifest_id IS DISTINCT FROM OLD.current_manifest_id THEN
    PERFORM fleet_fail('retired robots cannot change configuration');
  END IF;
  IF NEW.current_manifest_id IS DISTINCT FROM OLD.current_manifest_id AND NEW.current_manifest_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM fleet_configurationmanifest m
                     WHERE m.id = NEW.current_manifest_id AND m.robot_id = NEW.id) THEN
    PERFORM fleet_fail('manifest belongs to a different robot');
  END IF;
  NEW.version := OLD.version + 1;   -- the database owns the guard version
  RETURN NEW;
END $$;
CREATE TRIGGER robot_guard BEFORE UPDATE OR DELETE ON fleet_robot
  FOR EACH ROW EXECUTE FUNCTION fleet_robot_guard();

-- ---------- base authorization: only a single revoke ----------
CREATE OR REPLACE FUNCTION fleet_baseauth_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('base authorizations cannot be deleted'); END IF;
  IF TG_OP = 'INSERT' THEN
    IF NOT has_active_role(NEW.granted_by_id, 'engineering_lead') THEN
      PERFORM fleet_fail('base authorization requires an engineering_lead');
    END IF;
  ELSE
    IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR
       (NEW.robot_id, NEW.pattern_id, NEW.granted_by_id, NEW.granted_at)
       IS DISTINCT FROM (OLD.robot_id, OLD.pattern_id, OLD.granted_by_id, OLD.granted_at) THEN
      PERFORM fleet_fail('base authorization can only be revoked once');
    END IF;
  END IF;
  UPDATE fleet_robot SET version = version WHERE id = NEW.robot_id;
  RETURN NEW;
END $$;
CREATE TRIGGER baseauth_guard BEFORE INSERT OR UPDATE OR DELETE ON fleet_baseauthorization
  FOR EACH ROW EXECUTE FUNCTION fleet_baseauth_guard();

-- ---------- role-checked decisions that change a pattern's guard version ----------
CREATE OR REPLACE FUNCTION fleet_pattern_decision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT has_active_role(NEW.decided_by_id, 'engineering_lead') THEN
    PERFORM fleet_fail('pattern decisions require an engineering_lead');
  END IF;
  PERFORM fleet_bump_pattern(NEW.pattern_id);
  RETURN NEW;
END $$;
CREATE TRIGGER pattern_decision_guard BEFORE INSERT ON fleet_patternapproval
  FOR EACH ROW EXECUTE FUNCTION fleet_pattern_decision_guard();

CREATE OR REPLACE FUNCTION fleet_profile_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM fleet_bump_pattern(NEW.pattern_id); RETURN NEW; END $$;
CREATE TRIGGER profile_insert BEFORE INSERT ON fleet_capabilityprofile
  FOR EACH ROW EXECUTE FUNCTION fleet_profile_insert();

CREATE OR REPLACE FUNCTION fleet_slot_role(slot text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE slot WHEN 'engineering' THEN 'engineering_lead' WHEN 'safety' THEN 'safety_reviewer' END
$$;

CREATE OR REPLACE FUNCTION fleet_capability_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT has_active_role(NEW.approver_id, fleet_slot_role(NEW.slot)) THEN
    PERFORM fleet_fail(format('slot %s requires role %s', NEW.slot, fleet_slot_role(NEW.slot)));
  END IF;
  PERFORM fleet_bump_pattern((SELECT pattern_id FROM fleet_capabilityprofile WHERE id = NEW.profile_id));
  RETURN NEW;
END $$;
CREATE TRIGGER capability_approval_guard BEFORE INSERT ON fleet_capabilityapproval
  FOR EACH ROW EXECUTE FUNCTION fleet_capability_approval_guard();

CREATE OR REPLACE FUNCTION fleet_policy_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT has_active_role(NEW.approver_id, fleet_slot_role(NEW.slot)) THEN
    PERFORM fleet_fail(format('slot %s requires role %s', NEW.slot, fleet_slot_role(NEW.slot)));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER policy_approval_guard BEFORE INSERT ON fleet_holdpolicyapproval
  FOR EACH ROW EXECUTE FUNCTION fleet_policy_approval_guard();

-- ---------- holds ----------
CREATE OR REPLACE FUNCTION fleet_hold_release_problem(h fleet_hold) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
  rule jsonb; s jsonb; cur_manifest bigint;
BEGIN
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
    IF NOT fleet_policy_usable(NEW.policy_id) THEN
      PERFORM fleet_fail('the hold policy is a draft and drafts are not allowed in this deployment');
    END IF;
    slots := (SELECT rules -> NEW.source_type -> 'slots' FROM fleet_holdpolicy WHERE id = NEW.policy_id);
    IF slots IS NULL OR jsonb_array_length(slots) = 0 THEN
      PERFORM fleet_fail(format('policy has no release rule for source %s', NEW.source_type));
    END IF;
    IF NEW.required_slots IS DISTINCT FROM slots THEN
      PERFORM fleet_fail('required slots must be copied exactly from the policy');
    END IF;
    IF NEW.released_at IS NOT NULL THEN PERFORM fleet_fail('a hold cannot be created released'); END IF;
  ELSE
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
CREATE TRIGGER hold_guard BEFORE INSERT OR UPDATE OR DELETE ON fleet_hold
  FOR EACH ROW EXECUTE FUNCTION fleet_hold_guard();

CREATE OR REPLACE FUNCTION fleet_release_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE h fleet_hold; rule jsonb; s jsonb;
BEGIN
  SELECT * INTO h FROM fleet_hold WHERE id = NEW.hold_id;
  IF h.released_at IS NOT NULL THEN PERFORM fleet_fail('hold already released'); END IF;
  SELECT x INTO s FROM jsonb_array_elements(h.required_slots) x WHERE x ->> 'slot' = NEW.slot;
  IF s IS NULL THEN PERFORM fleet_fail(format('slot %s is not required for this hold', NEW.slot)); END IF;
  IF NOT (s -> 'roles') ? NEW.role_used THEN
    PERFORM fleet_fail(format('role %s may not fill slot %s', NEW.role_used, NEW.slot));
  END IF;
  IF NOT has_active_role(NEW.approver_id, NEW.role_used) THEN
    PERFORM fleet_fail(format('approver does not hold role %s', NEW.role_used));
  END IF;
  IF EXISTS (SELECT 1 FROM fleet_releaseapproval a WHERE a.hold_id = NEW.hold_id AND a.slot = NEW.slot
             AND NOT EXISTS (SELECT 1 FROM fleet_releaseapprovalwithdrawal w WHERE w.approval_id = a.id)) THEN
    PERFORM fleet_fail(format('slot %s is already filled', NEW.slot));
  END IF;
  rule := (SELECT rules -> h.source_type FROM fleet_holdpolicy WHERE id = h.policy_id);
  IF coalesce((rule ->> 'distinct_approvers')::boolean, true) AND EXISTS (
       SELECT 1 FROM fleet_releaseapproval a WHERE a.hold_id = NEW.hold_id AND a.approver_id = NEW.approver_id
       AND NOT EXISTS (SELECT 1 FROM fleet_releaseapprovalwithdrawal w WHERE w.approval_id = a.id)) THEN
    PERFORM fleet_fail('the same person cannot fill two slots on one hold');
  END IF;
  IF coalesce((rule ->> 'independent_of_placer')::boolean, false) AND NEW.approver_id = h.placed_by_id THEN
    PERFORM fleet_fail('the person who placed the hold cannot approve its release');
  END IF;
  IF h.scope = 'robot' AND NEW.manifest_id IS DISTINCT FROM
     (SELECT current_manifest_id FROM fleet_robot WHERE id = h.robot_id) THEN
    PERFORM fleet_fail('approval must be bound to the robot''s current configuration');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER release_approval_guard BEFORE INSERT ON fleet_releaseapproval
  FOR EACH ROW EXECUTE FUNCTION fleet_release_approval_guard();

-- ---------- inspections ----------
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
  IF robot_fail AND NOT EXISTS (SELECT 1 FROM fleet_hold h WHERE h.id = NEW.hold_id AND h.scope = 'robot'
                                AND h.robot_id = NEW.robot_id AND h.blocking AND h.released_at IS NULL) THEN
    PERFORM fleet_fail('an observed robot-condition failure must place a blocking robot hold');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER inspection_guard BEFORE INSERT ON fleet_inspection
  FOR EACH ROW EXECUTE FUNCTION fleet_inspection_guard();

-- ---------- codes ----------
CREATE SEQUENCE IF NOT EXISTS fleet_hold_code_seq;
CREATE SEQUENCE IF NOT EXISTS fleet_inspection_code_seq;
GRANT USAGE, SELECT ON SEQUENCE fleet_hold_code_seq, fleet_inspection_code_seq TO {app};

-- ---------- deployment settings: owner-only writes ----------
INSERT INTO fleet_deploymentsetting (key, value) VALUES ('allow_draft_policies', 'false')
  ON CONFLICT (key) DO NOTHING;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON fleet_deploymentsetting FROM {app};
"""


def _append_only_sql() -> str:
    parts = []
    for t in APPEND_ONLY:
        parts.append(
            f"CREATE TRIGGER {t}_no_update_delete BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION reject_mutation();\n"
            f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();"
        )
    return "\n".join(parts)


class Migration(migrations.Migration):
    dependencies = [("fleet", "0001_initial"), ("platform_core", "0002_db_security")]
    operations = [
        migrations.RunSQL(
            SQL.replace("{append_only}", _append_only_sql()).replace("{app}", APP),
            reverse_sql=migrations.RunSQL.noop,
        )
    ]

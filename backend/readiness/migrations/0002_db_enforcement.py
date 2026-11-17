"""Slice 3 readiness invariants enforced by PostgreSQL (the runtime role cannot bypass them with direct SQL).

- Append-only: every readiness table rejects UPDATE/DELETE/TRUNCATE. The ONLY mutation is
  readiness_readinesspolicy.lock_version, bumped by the approval/withdrawal triggers below (a direct UPDATE of
  a policy row is rejected: the guard only accepts updates issued from inside another trigger).
- Policy: consecutive versions from 1, lock_version starts at 1, requirements JSON validated exactly.
- Policy approval: slot role held now, author cannot approve, withdrawn policy cannot be approved.
- Policy withdrawal: non-empty reason, withdrawer holds engineering_lead or safety_reviewer.
- Evidence / recovery plan: bound to the visit's CURRENT revision, the revision's robot and the robot's
  CURRENT manifest; inspection linkage + PASS rule; details schema; observed_at within [now-24h, now].
- Evidence, void and recovery-plan inserts bump planning_visit.version (visit_bump trigger increments it).
- Departure authorization: policy approved + not withdrawn, mode matches demo_mode, production never with a
  synthetic_demo policy, visit confirmed, current revision/manifest, expiry window vs the policy TTL,
  issued_at forced to database time, evidence/recovery plan bound to the same visit/revision/manifest.
- Departure event: single use (unique authorization) + STRUCTURAL checks only (not revoked, not expired by
  clock_timestamp(), still the current revision/manifest, visit confirmed, policy not withdrawn,
  visit/robot/snapshot equal the authorization's). Live readiness is re-evaluated by the command, not here.

Locks taken by these triggers (all in the global order of docs/02_lock_graph.md, and all already held by the
calling command when it follows the contract):
  evidence insert       robot FOR SHARE -> visit row lock (UPDATE bump)
  void insert           visit row lock (UPDATE bump)
  recovery-plan insert  robot FOR SHARE -> persons FOR SHARE (ascending id) -> visit row lock (UPDATE bump)
  policy approval       policy row lock (UPDATE lock_version bump)
  policy withdrawal     policy row lock (UPDATE lock_version bump)
  authorization insert  robot FOR SHARE -> visit FOR SHARE -> policy FOR SHARE
  revocation insert     authorization FOR UPDATE
  departure insert      robot FOR SHARE -> visit FOR SHARE -> policy FOR SHARE -> authorization FOR SHARE
"""

from django.conf import settings
from django.db import migrations

APPEND_ONLY = [
    "readiness_readinesspolicyapproval",
    "readiness_readinesspolicywithdrawal",
    "readiness_readinessevidence",
    "readiness_readinessevidencevoid",
    "readiness_recoveryplan",
    "readiness_departureauthorization",
    "readiness_authorizationrevocation",
    "readiness_departureevent",
]

SQL = r"""
-- ================================================================ helpers
CREATE OR REPLACE FUNCTION readiness_positive_int(v jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN jsonb_typeof(v) = 'number'
              THEN v::numeric = trunc(v::numeric) AND v::numeric > 0 AND v::numeric <= 2147483647
              ELSE false END
$$;

CREATE OR REPLACE FUNCTION readiness_keys_exactly(j jsonb, expected text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN jsonb_typeof(j) = 'object'
              THEN ARRAY(SELECT jsonb_object_keys(j)) @> expected AND expected @> ARRAY(SELECT jsonb_object_keys(j))
              ELSE false END
$$;

-- A JSON array of distinct, non-blank strings (may be empty).
CREATE OR REPLACE FUNCTION readiness_string_list_ok(j jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE e jsonb;
BEGIN
  IF j IS NULL OR jsonb_typeof(j) <> 'array' THEN RETURN false; END IF;
  FOR e IN SELECT * FROM jsonb_array_elements(j) LOOP
    IF jsonb_typeof(e) <> 'string' OR btrim(e #>> '{}') = '' THEN RETURN false; END IF;
  END LOOP;
  RETURN (SELECT count(DISTINCT x) FROM jsonb_array_elements_text(j) x) = jsonb_array_length(j);
END $$;

CREATE OR REPLACE FUNCTION readiness_requirements_problem(j jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE e jsonb;
BEGIN
  IF NOT readiness_keys_exactly(j, ARRAY['authorization_ttl_min', 'evidence', 'required_equipment',
                                         'recovery_plan_required']) THEN
    RETURN 'requirements must have exactly the keys authorization_ttl_min, evidence, required_equipment, '
           'recovery_plan_required';
  END IF;
  IF NOT readiness_positive_int(j -> 'authorization_ttl_min') THEN
    RETURN 'authorization_ttl_min must be a whole number > 0';
  END IF;
  IF jsonb_typeof(j -> 'evidence') <> 'array' THEN RETURN 'evidence must be a non-empty list'; END IF;
  IF jsonb_array_length(j -> 'evidence') = 0 THEN RETURN 'evidence must be a non-empty list'; END IF;
  FOR e IN SELECT * FROM jsonb_array_elements(j -> 'evidence') LOOP
    IF NOT readiness_keys_exactly(e, ARRAY['kind', 'max_age_min']) THEN
      RETURN 'each evidence requirement must have exactly the keys kind and max_age_min';
    END IF;
    IF jsonb_typeof(e -> 'kind') <> 'string'
       OR e ->> 'kind' NOT IN ('pre_departure_inspection', 'equipment_check', 'battery_check') THEN
      RETURN format('unknown evidence kind %s', e -> 'kind');
    END IF;
    IF NOT readiness_positive_int(e -> 'max_age_min') THEN
      RETURN 'max_age_min must be a whole number > 0';
    END IF;
  END LOOP;
  IF (SELECT count(DISTINCT x ->> 'kind') FROM jsonb_array_elements(j -> 'evidence') x)
     <> jsonb_array_length(j -> 'evidence') THEN
    RETURN 'each evidence kind may appear only once';
  END IF;
  IF NOT readiness_string_list_ok(j -> 'required_equipment') THEN
    RETURN 'required_equipment must be a list of distinct, non-empty names';
  END IF;
  IF jsonb_typeof(j -> 'recovery_plan_required') <> 'boolean' THEN
    RETURN 'recovery_plan_required must be true or false';
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION readiness_policy_withdrawn(pid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM readiness_readinesspolicywithdrawal WHERE policy_id = pid)
$$;

CREATE OR REPLACE FUNCTION readiness_policy_approved(pid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT (SELECT count(DISTINCT slot) FROM readiness_readinesspolicyapproval
          WHERE policy_id = pid AND slot IN ('engineering', 'safety')) = 2
$$;

-- ================================================================ append-only
{append_only}

-- ================================================================ policy
CREATE OR REPLACE FUNCTION readiness_policy_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE problem text;
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('readiness policies cannot be deleted; withdraw them'); END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.version <> coalesce((SELECT max(version) FROM readiness_readinesspolicy), 0) + 1 THEN
      PERFORM fleet_fail('readiness policy versions must be consecutive');
    END IF;
    IF NEW.lock_version <> 1 THEN PERFORM fleet_fail('a new policy starts at lock_version 1'); END IF;
    problem := readiness_requirements_problem(NEW.requirements);
    IF problem IS NOT NULL THEN PERFORM fleet_fail('invalid requirements: ' || problem); END IF;
    RETURN NEW;
  END IF;
  -- UPDATE: only the lock_version bump issued by the approval / withdrawal triggers (nested trigger).
  IF pg_trigger_depth() < 2 THEN
    PERFORM fleet_fail('readiness policies are immutable; approve or withdraw them instead');
  END IF;
  IF (NEW.version, NEW.requirements, NEW.source, NEW.notes, NEW.created_by_id, NEW.created_at)
     IS DISTINCT FROM (OLD.version, OLD.requirements, OLD.source, OLD.notes, OLD.created_by_id, OLD.created_at) THEN
    PERFORM fleet_fail('readiness policies are immutable; approve or withdraw them instead');
  END IF;
  NEW.lock_version := OLD.lock_version + 1;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_policy_guard BEFORE INSERT OR UPDATE OR DELETE ON readiness_readinesspolicy
  FOR EACH ROW EXECUTE FUNCTION readiness_policy_guard();
CREATE TRIGGER readiness_readinesspolicy_no_truncate BEFORE TRUNCATE ON readiness_readinesspolicy
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();

CREATE OR REPLACE FUNCTION readiness_policy_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- Lock + bump the policy first, so the checks below see any concurrent withdrawal that committed.
  UPDATE readiness_readinesspolicy SET lock_version = lock_version WHERE id = NEW.policy_id;
  IF readiness_policy_withdrawn(NEW.policy_id) THEN
    PERFORM fleet_fail('a withdrawn readiness policy cannot be approved');
  END IF;
  IF NOT has_active_role(NEW.approver_id, fleet_slot_role(NEW.slot)) THEN
    PERFORM fleet_fail(format('slot %s requires role %s', NEW.slot, fleet_slot_role(NEW.slot)));
  END IF;
  IF NEW.approver_id = (SELECT created_by_id FROM readiness_readinesspolicy WHERE id = NEW.policy_id) THEN
    PERFORM fleet_fail('the author of a readiness policy cannot approve it');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_policy_approval_guard BEFORE INSERT ON readiness_readinesspolicyapproval
  FOR EACH ROW EXECUTE FUNCTION readiness_policy_approval_guard();

CREATE OR REPLACE FUNCTION readiness_policy_withdrawal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE readiness_readinesspolicy SET lock_version = lock_version WHERE id = NEW.policy_id;
  IF btrim(coalesce(NEW.reason, '')) = '' THEN PERFORM fleet_fail('a withdrawal needs a reason'); END IF;
  IF NOT (has_active_role(NEW.withdrawn_by_id, 'engineering_lead')
          OR has_active_role(NEW.withdrawn_by_id, 'safety_reviewer')) THEN
    PERFORM fleet_fail('withdrawing a readiness policy requires engineering_lead or safety_reviewer');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_policy_withdrawal_guard BEFORE INSERT ON readiness_readinesspolicywithdrawal
  FOR EACH ROW EXECUTE FUNCTION readiness_policy_withdrawal_guard();

-- ================================================================ evidence
CREATE OR REPLACE FUNCTION readiness_details_problem(kind text, d jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE k text; val jsonb;
BEGIN
  IF kind = 'pre_departure_inspection' THEN
    IF d IS DISTINCT FROM '{}'::jsonb THEN RETURN 'inspection evidence takes no details'; END IF;
  ELSIF kind = 'equipment_check' THEN
    IF NOT readiness_keys_exactly(d, ARRAY['items']) THEN
      RETURN 'equipment check details must be {"items": {name: "present" | "missing", ...}}';
    END IF;
    IF jsonb_typeof(d -> 'items') <> 'object' THEN
      RETURN 'equipment check details must be {"items": {name: "present" | "missing", ...}}';
    END IF;
    IF d -> 'items' = '{}'::jsonb THEN RETURN 'equipment check must list at least one item'; END IF;
    FOR k, val IN SELECT * FROM jsonb_each(d -> 'items') LOOP
      IF btrim(k) = '' OR jsonb_typeof(val) <> 'string' OR val #>> '{}' NOT IN ('present', 'missing') THEN
        RETURN 'equipment items must be named and marked present or missing';
      END IF;
    END LOOP;
  ELSIF kind = 'battery_check' THEN
    IF NOT readiness_keys_exactly(d, ARRAY['soc_pct']) THEN
      RETURN 'battery check details must be {"soc_pct": 1..100}';
    END IF;
    IF NOT readiness_positive_int(d -> 'soc_pct') THEN
      RETURN 'battery check details must be {"soc_pct": 1..100}';
    END IF;
    IF (d ->> 'soc_pct')::numeric > 100 THEN RETURN 'battery check details must be {"soc_pct": 1..100}'; END IF;
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION readiness_evidence_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v planning_visit; r planning_visitrevision; m fleet_configurationmanifest; i fleet_inspection;
        problem text;
BEGIN
  PERFORM 1 FROM fleet_robot WHERE id = NEW.robot_id FOR SHARE;
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
  ELSIF NEW.inspection_id IS NOT NULL THEN
    PERFORM fleet_fail('only pre-departure inspection evidence may reference an inspection');
  END IF;
  problem := readiness_details_problem(NEW.kind, NEW.details);
  IF problem IS NOT NULL THEN PERFORM fleet_fail(problem); END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_evidence_guard BEFORE INSERT ON readiness_readinessevidence
  FOR EACH ROW EXECUTE FUNCTION readiness_evidence_guard();

CREATE OR REPLACE FUNCTION readiness_evidence_void_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE vid bigint;
BEGIN
  IF btrim(coalesce(NEW.reason, '')) = '' THEN PERFORM fleet_fail('a void needs a reason'); END IF;
  vid := (SELECT visit_id FROM readiness_readinessevidence WHERE id = NEW.evidence_id);
  IF (SELECT status FROM planning_visit WHERE id = vid) = 'cancelled' THEN
    PERFORM fleet_fail('evidence of a cancelled visit cannot change');
  END IF;
  UPDATE planning_visit SET version = version WHERE id = vid;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_evidence_void_guard BEFORE INSERT ON readiness_readinessevidencevoid
  FOR EACH ROW EXECUTE FUNCTION readiness_evidence_void_guard();

-- ================================================================ recovery plan
CREATE OR REPLACE FUNCTION readiness_recovery_plan_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v planning_visit; r planning_visitrevision; m fleet_configurationmanifest;
BEGIN
  SELECT * INTO r FROM planning_visitrevision WHERE id = NEW.revision_id;
  PERFORM 1 FROM fleet_robot WHERE id = r.robot_id FOR SHARE;
  PERFORM 1 FROM planning_person WHERE id IN (NEW.on_call_engineer_id, NEW.escalation_contact_id)
    ORDER BY id FOR SHARE;
  IF (SELECT status FROM planning_visit WHERE id = NEW.visit_id) = 'cancelled' THEN
    PERFORM fleet_fail('a recovery plan cannot be recorded for a cancelled visit');
  END IF;
  UPDATE planning_visit SET version = version WHERE id = NEW.visit_id;
  SELECT * INTO v FROM planning_visit WHERE id = NEW.visit_id;
  IF r.visit_id <> NEW.visit_id THEN PERFORM fleet_fail('revision belongs to a different visit'); END IF;
  IF v.current_revision_id IS DISTINCT FROM NEW.revision_id THEN
    PERFORM fleet_fail('recovery plan must be bound to the visit''s current revision');
  END IF;
  IF r.robot_id IS NULL THEN PERFORM fleet_fail('the revision has no robot assigned'); END IF;
  SELECT * INTO m FROM fleet_configurationmanifest WHERE id = NEW.manifest_id;
  IF m.robot_id <> r.robot_id THEN PERFORM fleet_fail('manifest belongs to a different robot'); END IF;
  IF NEW.manifest_id IS DISTINCT FROM (SELECT current_manifest_id FROM fleet_robot WHERE id = r.robot_id) THEN
    PERFORM fleet_fail('recovery plan must be bound to the robot''s current configuration');
  END IF;
  IF NEW.on_call_engineer_id = NEW.escalation_contact_id THEN
    PERFORM fleet_fail('the escalation contact must differ from the on-call engineer');
  END IF;
  IF btrim(coalesce(NEW.escalation_path, '')) = '' THEN PERFORM fleet_fail('an escalation path is required'); END IF;
  IF NOT readiness_string_list_ok(NEW.required_equipment) THEN
    PERFORM fleet_fail('required_equipment must be a list of distinct, non-empty names');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_recovery_plan_guard BEFORE INSERT ON readiness_recoveryplan
  FOR EACH ROW EXECUTE FUNCTION readiness_recovery_plan_guard();

-- ================================================================ departure authorization
CREATE OR REPLACE FUNCTION readiness_authorization_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v planning_visit; r planning_visitrevision; m fleet_configurationmanifest; p readiness_readinesspolicy;
        rp readiness_recoveryplan; expected_mode text; e jsonb; ttl int;
BEGIN
  NEW.issued_at := clock_timestamp();
  PERFORM 1 FROM fleet_robot WHERE id = NEW.robot_id FOR SHARE;
  PERFORM 1 FROM planning_visit WHERE id = NEW.visit_id FOR SHARE;
  PERFORM 1 FROM readiness_readinesspolicy WHERE id = NEW.policy_id FOR SHARE;
  SELECT * INTO p FROM readiness_readinesspolicy WHERE id = NEW.policy_id;
  IF readiness_policy_withdrawn(p.id) THEN PERFORM fleet_fail('the readiness policy is withdrawn'); END IF;
  IF NOT readiness_policy_approved(p.id) THEN PERFORM fleet_fail('the readiness policy is not approved'); END IF;
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
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_authorization_guard BEFORE INSERT ON readiness_departureauthorization
  FOR EACH ROW EXECUTE FUNCTION readiness_authorization_guard();

CREATE OR REPLACE FUNCTION readiness_revocation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM 1 FROM readiness_departureauthorization WHERE id = NEW.authorization_id FOR UPDATE;
  IF btrim(coalesce(NEW.reason, '')) = '' THEN PERFORM fleet_fail('a revocation needs a reason'); END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_revocation_guard BEFORE INSERT ON readiness_authorizationrevocation
  FOR EACH ROW EXECUTE FUNCTION readiness_revocation_guard();

-- ================================================================ departure event (structural checks only)
CREATE OR REPLACE FUNCTION readiness_departure_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a readiness_departureauthorization; v planning_visit;
BEGIN
  NEW.departed_at := clock_timestamp();
  SELECT * INTO a FROM readiness_departureauthorization WHERE id = NEW.authorization_id;
  PERFORM 1 FROM fleet_robot WHERE id = a.robot_id FOR SHARE;
  PERFORM 1 FROM planning_visit WHERE id = a.visit_id FOR SHARE;
  PERFORM 1 FROM readiness_readinesspolicy WHERE id = a.policy_id FOR SHARE;
  PERFORM 1 FROM readiness_departureauthorization WHERE id = a.id FOR SHARE;
  IF (NEW.visit_id, NEW.robot_id) IS DISTINCT FROM (a.visit_id, a.robot_id) THEN
    PERFORM fleet_fail('departure visit and robot must equal the authorization''s');
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
  IF NEW.guard_snapshot IS DISTINCT FROM a.guard_snapshot THEN
    PERFORM fleet_fail('the guard snapshot differs from the authorization''s');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_departure_guard BEFORE INSERT ON readiness_departureevent
  FOR EACH ROW EXECUTE FUNCTION readiness_departure_guard();

-- ================================================================ codes
CREATE SEQUENCE IF NOT EXISTS readiness_evidence_code_seq;
CREATE SEQUENCE IF NOT EXISTS readiness_authorization_code_seq;
GRANT USAGE, SELECT ON SEQUENCE readiness_evidence_code_seq, readiness_authorization_code_seq TO {app};
"""


def _sql() -> str:
    parts = []
    for t in APPEND_ONLY:
        parts.append(
            f"CREATE TRIGGER {t}_no_update_delete BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION reject_mutation();\n"
            f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();"
        )
    return SQL.replace("{append_only}", "\n".join(parts)).replace("{app}", settings.DB_APP_ROLE)


class Migration(migrations.Migration):
    dependencies = [("readiness", "0001_initial")]
    operations = [migrations.RunSQL(_sql(), reverse_sql=migrations.RunSQL.noop)]

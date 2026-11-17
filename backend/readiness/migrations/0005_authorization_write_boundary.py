"""Controlled database write boundary for departure authorizations (Slice 3 closure, EM condition).

PROBLEM. Until now the runtime role (ops_app) could INSERT a readiness_departureauthorization directly. The
insert triggers (0002/0003/0004) check structure and that the guard snapshot equals the LIVE guard versions,
but not that those versions describe a READY world: a raw INSERT taken while a hold was in place, consent was
withdrawn or evidence was missing/stale passed the triggers.

BOUNDARY.
- ``REVOKE INSERT ON readiness_departureauthorization FROM ops_app``. SELECT and UPDATE stay granted: UPDATE
  is needed for ``SELECT ... FOR UPDATE`` (revocation and departure lock the authorization row); actual
  UPDATE/DELETE/TRUNCATE are still rejected by the append-only triggers.
- The ONLY way for ops_app to issue an authorization is ``readiness_issue_authorization(...)``, a SECURITY
  DEFINER function owned by the schema owner, with a fixed ``search_path = public, pg_temp``, EXECUTE revoked
  from PUBLIC and granted to ops_app only. It re-proves in SQL the safety gates that the database can check
  (listed below), then INSERTs the row as the owner, so the existing insert triggers still run on top.
- ``readiness.commands.authorize_departure`` calls this function (after its locks and the Python evaluation)
  instead of an ORM insert. Python's ``evaluate_readiness`` stays the primary, explained decision (blocker
  codes, actions, review items); the function is the database-enforced minimum that direct SQL cannot skip.

SIGNATURE.
    readiness_issue_authorization(p_visit_id bigint, p_issued_by bigint, p_expires_at timestamptz,
                                  p_evidence_ids jsonb, p_recovery_plan_id bigint, p_guard_snapshot jsonb,
                                  p_mode text) RETURNS TABLE (id bigint, code text)
  revision, robot, manifest and policy are NOT parameters: they are taken from the live rows (and the snapshot
  must equal them). The code (DA-0001 style) is drawn from readiness_authorization_code_seq inside.

LOCKS (global order of docs/02_lock_graph.md; all shared; authorize_departure already holds every one of them
in an equal or stronger mode, so for the command they add no waits):
    settings advisory S -> site S -> pattern S -> robot S -> persons S (ascending) -> vehicle S
    (readiness_lock_snapshot_guards, on the rows the snapshot names) -> visit S -> this policy and every newer
    policy S. With the snapshot then proven equal to the live rows, every versioned input below (holds,
    pattern decisions, profiles, base authorization, consent/headcount, qualifications, evidence, voids,
    recovery plans, policy approvals/withdrawals) is stable until commit: each of their writers bumps one of
    the locked rows and so must wait.

RE-PROVEN IN SQL (each failure raises fleet_fail -> check_violation, mapped to Conflict by run_command):
  1. issuer: ``p_issued_by`` holds an active ops_lead role NOW (has_active_role). The database cannot know
     which human is behind ops_app, so it checks the named issuer, it cannot authenticate them.
  2. visit: exists, confirmed, has a current revision, no departure recorded.
  3. snapshot: ``readiness_guard_problem`` (the SAME function the authorization and departure triggers use)
     says the snapshot equals the live guards (versions of visit, site, robot, pattern, persons, vehicle,
     active policy + lock_version; plan and recovery-plan people present).
  4. policy: ``readiness_active_policy_id()`` exists; ``p_mode`` equals the mode from demo_mode; production
     requires source approved_procedure.
  5. robot planning eligibility basics: lifecycle in_service; latest pattern decision approved_home; an
     unrevoked base authorization for robot + pattern; no unreleased blocking robot hold or pattern hold;
     an approved capability profile (highest version with engineering + safety approvals) with no unknown
     planning field (stop_behaviour, lift_crew, max_runtime_min, charge_turnaround_min); a synthetic_demo
     profile is refused outside demo mode.
  6. consent: a consent_form_version is configured; site expected_adults is known; the count of
     non-withdrawn consents on that form whose scope covers the revision's required_consent_scopes is
     >= expected_adults.
  7. crew: lead and second assigned; lead holds field_lead, second field_operator or field_lead, both
     robot_handler; each unrevoked with valid_from <= clock_timestamp() and valid_to >= revision.on_site_end.
     (Simple, conservative window: Python checks the exact activity window, inspection start through unload
     end, which extends past on_site_end; the SQL window is "valid now and through the visit".)
  8. recovery plan: when the policy requires one, it exists for the current revision + manifest; in every
     case ``p_recovery_plan_id`` must be the LATEST such plan (or NULL when none exists); on-call person and
     escalation contact differ; escalation path non-empty.
  9. evidence: for each policy evidence requirement, the LATEST non-voided evidence of that kind for the
     current revision and current manifest (same ordering as the evaluator) is 'pass' and observed_at >=
     clock_timestamp() - max_age_min.
 10. battery: the latest such battery_check passes and soc_pct >= the approved profile's
     min_departure_soc_pct; an unknown minimum blocks.
 11. equipment: policy required_equipment + the recovery plan's required_equipment are each 'present' in
     the latest passing equipment_check.
 12. evidence_ids: a JSON list of distinct ids equal to EXACTLY the set relied on in 9-11 (the evaluator's
     ``evidence_ids``), so an authorization names all the evidence it rests on and nothing else.
 13. expiry: clock_timestamp() < p_expires_at <= least(clock_timestamp() + ttl, min over 9 of observed_at +
     max_age_min). (The insert trigger still bounds it by issued_at + ttl as well.)

NOT RE-PROVEN IN SQL (Python evaluator only, under the command's locks):
  - gate 2 parts beyond the above: site assessment (approved, unexpired, excluded tasks), vehicle capacity and
    restraint kit, travel/inspection/charge timeline and resource conflicts, required crew size from the
    capability profile, the exact qualification window (inspection start .. unload end);
  - gate 3: that every planned reservation for the revision is still active and covers the plan (releases do
    bump the visit version, so a snapshot taken before a release is still refused);
  - the on-call person's engineer role (a "review" item, not a blocker);
  - the human identity of the issuer (see 1) and that the caller is the command at all (idempotency and the
    audit row are written by run_command, not here: a direct call writes no audit row).

Teardown note: the test flush runs as the owner (TRUNCATE + DISABLE TRIGGER USER) and is unaffected.
"""

from django.conf import settings
from django.db import migrations

from platform_core.locks import SETTINGS_LOCK_KEY

SQL = r"""
CREATE OR REPLACE FUNCTION readiness_issue_authorization(
  p_visit_id bigint, p_issued_by bigint, p_expires_at timestamptz, p_evidence_ids jsonb,
  p_recovery_plan_id bigint, p_guard_snapshot jsonb, p_mode text)
RETURNS TABLE (id bigint, code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
#variable_conflict use_column
DECLARE
  v planning_visit; r planning_visitrevision; st planning_site; rb fleet_robot; m fleet_configurationmanifest;
  pol readiness_readinesspolicy; prof fleet_capabilityprofile; rp readiness_recoveryplan;
  ev readiness_readinessevidence; req jsonb; e jsonb;
  snap_robot bigint; snap_manifest bigint; snap_policy bigint; active bigint; latest_plan bigint;
  problem text; expected_mode text; form text; n_consent int; ttl int; max_age int; k text; soc int;
  now_ts timestamptz; bound timestamptz; relied bigint[] := ARRAY[]::bigint[]; given bigint[];
  equipment text[]; bad text[]; seq bigint; new_id bigint; new_code text;
BEGIN
  IF p_visit_id IS NULL OR p_issued_by IS NULL OR p_expires_at IS NULL OR p_mode IS NULL THEN
    PERFORM fleet_fail('authorization refused: visit, issuer, expiry and mode are required');
  END IF;
  IF jsonb_typeof(p_guard_snapshot) IS DISTINCT FROM 'object' THEN
    PERFORM fleet_fail('authorization refused: the guard snapshot must be an object');
  END IF;
  IF readiness_positive_int(p_guard_snapshot -> 'robot_id') THEN
    snap_robot := (p_guard_snapshot ->> 'robot_id')::bigint;
  END IF;
  IF readiness_positive_int(p_guard_snapshot -> 'manifest_id') THEN
    snap_manifest := (p_guard_snapshot ->> 'manifest_id')::bigint;
  END IF;
  IF readiness_positive_int(p_guard_snapshot -> 'policy_id') THEN
    snap_policy := (p_guard_snapshot ->> 'policy_id')::bigint;
  END IF;

  -- ---------------------------------------------------------------- locks, global order, all shared
  PERFORM pg_advisory_xact_lock_shared({settings_key});
  PERFORM readiness_lock_snapshot_guards(p_guard_snapshot, p_visit_id, snap_manifest, snap_robot);
  PERFORM 1 FROM planning_visit WHERE id = p_visit_id FOR SHARE;
  IF NOT FOUND THEN PERFORM fleet_fail(format('authorization refused: no visit %s', p_visit_id)); END IF;
  PERFORM 1 FROM readiness_readinesspolicy WHERE id >= snap_policy ORDER BY id FOR SHARE;

  -- ---------------------------------------------------------------- 1. issuer
  IF NOT has_active_role(p_issued_by, 'ops_lead') THEN
    PERFORM fleet_fail('authorization refused: the issuer does not hold an active ops_lead role');
  END IF;

  -- ---------------------------------------------------------------- 2. visit
  SELECT * INTO v FROM planning_visit WHERE id = p_visit_id;
  IF v.status <> 'confirmed' OR v.current_revision_id IS NULL THEN
    PERFORM fleet_fail(format('authorization refused: only a confirmed visit can be authorized (visit is %s)',
                              v.status));
  END IF;
  IF EXISTS (SELECT 1 FROM readiness_departureevent d WHERE d.visit_id = v.id) THEN
    PERFORM fleet_fail('authorization refused: this visit has already departed');
  END IF;

  -- ---------------------------------------------------------------- 3. snapshot equals the live guards
  problem := readiness_guard_problem(p_guard_snapshot, p_visit_id, p_recovery_plan_id);
  IF problem IS NOT NULL THEN
    PERFORM fleet_fail('authorization refused: guard snapshot does not match the live guards: ' || problem);
  END IF;
  SELECT * INTO r FROM planning_visitrevision WHERE id = v.current_revision_id;
  SELECT * INTO st FROM planning_site WHERE id = v.site_id;
  SELECT * INTO rb FROM fleet_robot WHERE id = r.robot_id;
  SELECT * INTO m FROM fleet_configurationmanifest WHERE id = rb.current_manifest_id;

  -- ---------------------------------------------------------------- 4. policy and mode
  active := readiness_active_policy_id();
  IF active IS NULL THEN PERFORM fleet_fail('authorization refused: there is no active readiness policy'); END IF;
  SELECT * INTO pol FROM readiness_readinesspolicy WHERE id = active;
  expected_mode := CASE WHEN coalesce(fleet_setting('demo_mode'), 'false') = 'true' THEN 'demo' ELSE 'production' END;
  IF p_mode IS DISTINCT FROM expected_mode THEN
    PERFORM fleet_fail(format('authorization refused: mode must be %s (from the demo_mode setting)', expected_mode));
  END IF;
  IF expected_mode = 'production' AND pol.source <> 'approved_procedure' THEN
    PERFORM fleet_fail('authorization refused: a synthetic_demo policy can never authorize departure in production');
  END IF;

  -- ---------------------------------------------------------------- 5. robot planning eligibility basics
  IF rb.lifecycle <> 'in_service' THEN
    PERFORM fleet_fail(format('authorization refused: robot %s is %s, not in service', rb.code, rb.lifecycle));
  END IF;
  IF (SELECT a.status FROM fleet_patternapproval a WHERE a.pattern_id = m.pattern_id ORDER BY a.id DESC LIMIT 1)
     IS DISTINCT FROM 'approved_home' THEN
    PERFORM fleet_fail('authorization refused: the robot''s configuration pattern is not approved for home use');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM fleet_baseauthorization b
                 WHERE b.robot_id = rb.id AND b.pattern_id = m.pattern_id AND b.revoked_at IS NULL) THEN
    PERFORM fleet_fail('authorization refused: no active base home-use authorization for the robot''s configuration');
  END IF;
  IF EXISTS (SELECT 1 FROM fleet_hold h WHERE h.scope = 'robot' AND h.robot_id = rb.id
             AND h.blocking AND h.released_at IS NULL) THEN
    PERFORM fleet_fail(format('authorization refused: robot %s has an unreleased blocking hold', rb.code));
  END IF;
  IF EXISTS (SELECT 1 FROM fleet_hold h WHERE h.scope = 'manifest_pattern' AND h.pattern_id = m.pattern_id
             AND h.blocking AND h.released_at IS NULL) THEN
    PERFORM fleet_fail('authorization refused: the robot''s configuration pattern has an unreleased blocking hold');
  END IF;
  SELECT p.* INTO prof FROM fleet_capabilityprofile p
   WHERE p.pattern_id = m.pattern_id
     AND (SELECT count(DISTINCT a.slot) FROM fleet_capabilityapproval a
          WHERE a.profile_id = p.id AND a.slot IN ('engineering', 'safety')) = 2
   ORDER BY p.version_no DESC LIMIT 1;
  IF prof.id IS NULL THEN
    PERFORM fleet_fail('authorization refused: no approved capability profile for the robot''s configuration');
  END IF;
  IF coalesce(prof.stop_behaviour, '') = '' OR prof.lift_crew IS NULL OR prof.max_runtime_min IS NULL
     OR prof.charge_turnaround_min IS NULL THEN
    PERFORM fleet_fail('authorization refused: the approved capability profile has unknown planning fields');
  END IF;
  IF prof.source = 'synthetic_demo' AND expected_mode = 'production' THEN
    PERFORM fleet_fail('authorization refused: the capability profile is synthetic demo data (production mode)');
  END IF;

  -- ---------------------------------------------------------------- 6. consent
  form := fleet_setting('consent_form_version');
  IF coalesce(form, '') = '' THEN
    PERFORM fleet_fail('authorization refused: no current consent form version is configured');
  END IF;
  IF st.expected_adults IS NULL THEN
    PERFORM fleet_fail('authorization refused: the number of adults at the site is unknown');
  END IF;
  n_consent := (SELECT count(*) FROM planning_consentrecord c
                WHERE c.site_id = st.id AND c.withdrawn_at IS NULL AND c.form_version = form
                  AND jsonb_typeof(c.scope) = 'array'
                  AND c.scope @> coalesce(r.required_consent_scopes, '[]'::jsonb));
  IF n_consent < st.expected_adults THEN
    PERFORM fleet_fail(format('authorization refused: %s of %s adults have valid %s consent covering the visit',
                              n_consent, st.expected_adults, form));
  END IF;

  -- ---------------------------------------------------------------- 7. crew qualifications
  IF r.lead_id IS NULL OR r.second_id IS NULL THEN
    PERFORM fleet_fail('authorization refused: the revision needs a lead and a second person');
  END IF;
  IF NOT readiness_qualified(r.lead_id, ARRAY['field_lead'], r.on_site_end) THEN
    PERFORM fleet_fail(format('authorization refused: lead %s is not a qualified field lead through the visit',
                              (SELECT x.code FROM planning_person x WHERE x.id = r.lead_id)));
  END IF;
  IF NOT readiness_qualified(r.second_id, ARRAY['field_operator', 'field_lead'], r.on_site_end) THEN
    PERFORM fleet_fail(format('authorization refused: second %s is not a qualified field operator through the visit',
                              (SELECT x.code FROM planning_person x WHERE x.id = r.second_id)));
  END IF;
  IF NOT readiness_qualified(r.lead_id, ARRAY['robot_handler'], r.on_site_end)
     OR NOT readiness_qualified(r.second_id, ARRAY['robot_handler'], r.on_site_end) THEN
    PERFORM fleet_fail('authorization refused: lead and second must both be qualified robot handlers through the visit');
  END IF;

  -- ---------------------------------------------------------------- 8. recovery plan
  latest_plan := (SELECT x.id FROM readiness_recoveryplan x
                  WHERE x.visit_id = v.id AND x.revision_id = r.id AND x.manifest_id = rb.current_manifest_id
                  ORDER BY x.id DESC LIMIT 1);
  IF (pol.requirements ->> 'recovery_plan_required')::boolean AND latest_plan IS NULL THEN
    PERFORM fleet_fail('authorization refused: no recovery plan for the current revision and configuration');
  END IF;
  IF p_recovery_plan_id IS DISTINCT FROM latest_plan THEN
    PERFORM fleet_fail('authorization refused: the recovery plan must be the latest plan for the current revision '
                       'and configuration');
  END IF;
  IF latest_plan IS NOT NULL THEN
    SELECT * INTO rp FROM readiness_recoveryplan WHERE id = latest_plan;
    IF rp.on_call_engineer_id = rp.escalation_contact_id OR btrim(coalesce(rp.escalation_path, '')) = '' THEN
      PERFORM fleet_fail('authorization refused: the recovery plan needs two distinct people and an escalation path');
    END IF;
  END IF;

  -- ---------------------------------------------------------------- 9. evidence per policy requirement
  now_ts := clock_timestamp();
  ttl := (pol.requirements ->> 'authorization_ttl_min')::int;
  bound := now_ts + make_interval(mins => ttl);
  FOR req IN SELECT * FROM jsonb_array_elements(pol.requirements -> 'evidence') LOOP
    k := req ->> 'kind';
    max_age := (req ->> 'max_age_min')::int;
    ev := readiness_latest_evidence(v.id, r.id, rb.current_manifest_id, k);
    IF ev.id IS NULL THEN
      PERFORM fleet_fail(format('authorization refused: no %s for the current revision and configuration', k));
    END IF;
    IF ev.result <> 'pass' THEN
      PERFORM fleet_fail(format('authorization refused: %s %s failed', k, ev.code));
    END IF;
    IF ev.observed_at < now_ts - make_interval(mins => max_age) THEN
      PERFORM fleet_fail(format('authorization refused: %s %s is stale (older than %s min)', k, ev.code, max_age));
    END IF;
    relied := relied || ev.id;
    bound := least(bound, ev.observed_at + make_interval(mins => max_age));
  END LOOP;

  -- ---------------------------------------------------------------- 10. battery
  ev := readiness_latest_evidence(v.id, r.id, rb.current_manifest_id, 'battery_check');
  IF ev.id IS NULL OR ev.result <> 'pass' THEN
    PERFORM fleet_fail('authorization refused: no passing battery check; charge level unknown');
  END IF;
  IF prof.min_departure_soc_pct IS NULL THEN
    PERFORM fleet_fail('authorization refused: the departure battery minimum is unknown');
  END IF;
  soc := (ev.details ->> 'soc_pct')::int;
  IF soc IS NULL OR soc < prof.min_departure_soc_pct THEN
    PERFORM fleet_fail(format('authorization refused: battery at %s%%, below the %s%% departure minimum',
                              soc, prof.min_departure_soc_pct));
  END IF;
  IF NOT ev.id = ANY (relied) THEN relied := relied || ev.id; END IF;

  -- ---------------------------------------------------------------- 11. equipment
  equipment := ARRAY(SELECT jsonb_array_elements_text(pol.requirements -> 'required_equipment'));
  IF latest_plan IS NOT NULL THEN
    equipment := equipment || ARRAY(SELECT x FROM jsonb_array_elements_text(rp.required_equipment) x
                                     WHERE NOT x = ANY (equipment));
  END IF;
  IF cardinality(equipment) > 0 THEN
    ev := readiness_latest_evidence(v.id, r.id, rb.current_manifest_id, 'equipment_check');
    IF ev.id IS NULL OR ev.result <> 'pass' THEN
      PERFORM fleet_fail('authorization refused: no passing equipment check covers ' || array_to_string(equipment, ', '));
    END IF;
    bad := ARRAY(SELECT x FROM unnest(equipment) x WHERE (ev.details -> 'items' ->> x) IS DISTINCT FROM 'present');
    IF cardinality(bad) > 0 THEN
      PERFORM fleet_fail(format('authorization refused: equipment check %s does not show present: %s',
                                ev.code, array_to_string(bad, ', ')));
    END IF;
    IF NOT ev.id = ANY (relied) THEN relied := relied || ev.id; END IF;
  END IF;

  -- ---------------------------------------------------------------- 12. evidence_ids = exactly what was relied on
  IF jsonb_typeof(p_evidence_ids) IS DISTINCT FROM 'array' THEN
    PERFORM fleet_fail('authorization refused: evidence_ids must be a list');
  END IF;
  FOR e IN SELECT * FROM jsonb_array_elements(p_evidence_ids) LOOP
    IF NOT readiness_positive_int(e) THEN PERFORM fleet_fail('authorization refused: evidence_ids must be a list of ids'); END IF;
  END LOOP;
  given := ARRAY(SELECT DISTINCT (x #>> '{{}}')::bigint FROM jsonb_array_elements(p_evidence_ids) x ORDER BY 1);
  IF cardinality(given) <> jsonb_array_length(p_evidence_ids)
     OR given IS DISTINCT FROM ARRAY(SELECT DISTINCT x FROM unnest(relied) x ORDER BY 1) THEN
    PERFORM fleet_fail(format('authorization refused: evidence_ids must be exactly the evidence relied on %s',
                              to_jsonb(ARRAY(SELECT DISTINCT x FROM unnest(relied) x ORDER BY 1))));
  END IF;

  -- ---------------------------------------------------------------- 13. expiry
  IF p_expires_at <= clock_timestamp() THEN
    PERFORM fleet_fail('authorization refused: expires_at must be in the future');
  END IF;
  IF p_expires_at > bound THEN
    PERFORM fleet_fail(format('authorization refused: expires_at is later than allowed (%s: policy TTL and '
                              'evidence ages)', bound));
  END IF;

  -- ---------------------------------------------------------------- insert (insert triggers still run)
  seq := nextval('readiness_authorization_code_seq');
  new_code := 'DA-' || CASE WHEN seq < 10000 THEN lpad(seq::text, 4, '0') ELSE seq::text END;
  INSERT INTO readiness_departureauthorization AS a (code, visit_id, revision_id, robot_id, manifest_id, policy_id,
    mode, guard_snapshot, evidence_ids, recovery_plan_id, issued_by_id, issued_at, expires_at)
  VALUES (new_code, v.id, r.id, rb.id, rb.current_manifest_id, pol.id, p_mode, p_guard_snapshot,
    to_jsonb(given), latest_plan, p_issued_by, clock_timestamp(), p_expires_at)
  RETURNING a.id INTO new_id;
  RETURN QUERY SELECT new_id, new_code;
END $fn$;

REVOKE ALL ON FUNCTION readiness_issue_authorization(bigint, bigint, timestamptz, jsonb, bigint, jsonb, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION readiness_issue_authorization(bigint, bigint, timestamptz, jsonb, bigint, jsonb, text) TO {app};
REVOKE INSERT ON readiness_departureauthorization FROM {app};
"""

# Helpers (not SECURITY DEFINER; plain STABLE readers, callable by anyone, they grant nothing).
HELPERS = r"""
-- The latest non-voided evidence of `k` for this visit, revision and manifest (evaluator ordering), or an
-- all-NULL row.
CREATE OR REPLACE FUNCTION readiness_latest_evidence(vid bigint, rid bigint, mid bigint, k text)
RETURNS readiness_readinessevidence LANGUAGE sql STABLE AS $$
  SELECT ev.* FROM readiness_readinessevidence ev
  WHERE ev.visit_id = vid AND ev.revision_id = rid AND ev.manifest_id = mid AND ev.kind = k
    AND NOT EXISTS (SELECT 1 FROM readiness_readinessevidencevoid x WHERE x.evidence_id = ev.id)
  ORDER BY ev.observed_at DESC, ev.id DESC LIMIT 1
$$;

-- Person `pid` holds one of `kinds`, unrevoked, valid from now through `until`.
CREATE OR REPLACE FUNCTION readiness_qualified(pid bigint, kinds text[], until timestamptz) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM planning_qualification q
                 WHERE q.person_id = pid AND q.kind = ANY (kinds) AND q.revoked_at IS NULL
                   AND q.valid_from <= clock_timestamp() AND q.valid_to >= until)
$$;
"""

REVERSE_SQL = r"""
GRANT INSERT ON readiness_departureauthorization TO {app};
DROP FUNCTION IF EXISTS readiness_issue_authorization(bigint, bigint, timestamptz, jsonb, bigint, jsonb, text);
DROP FUNCTION IF EXISTS readiness_qualified(bigint, text[], timestamptz);
DROP FUNCTION IF EXISTS readiness_latest_evidence(bigint, bigint, bigint, text);
"""


def _sql() -> str:
    body = SQL.replace("{settings_key}", str(SETTINGS_LOCK_KEY)).replace("{app}", settings.DB_APP_ROLE)
    return HELPERS + body.replace("{{}}", "{}")


class Migration(migrations.Migration):
    dependencies = [("readiness", "0004_one_departure_per_visit")]
    operations = [
        migrations.RunSQL(_sql(), reverse_sql=REVERSE_SQL.replace("{app}", settings.DB_APP_ROLE)),
    ]

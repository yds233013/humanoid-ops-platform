"""Authorization write boundary, second pass (EM follow-up on readiness/0005).

0005 left planning-feasibility gates to Python only, so a direct ops_app call of
``readiness_issue_authorization`` could issue while the site assessment, the reservations, the vehicle or the
crew's qualification windows blocked readiness. This migration CREATE OR REPLACEs the function (same signature,
same SECURITY DEFINER / search_path / grants, which CREATE OR REPLACE keeps) and adds, matching the Python
rules in ``planning.services.evaluate`` and ``readiness.evaluator._gate_reservations``:

A. Live plan timeline, computed in SQL with the SAME formula as ``planning.services.evaluate``:
       insp  = setting pre_visit_inspection_min (positive whole number, else unknown)
       load  = approved profile load_unload_min (> 0), charge = charge_turnaround_min (> 0)
       depart = on_site_start - travel_out; load_start = depart - load; insp_start = load_start - insp
       back = on_site_end + travel_back; unload_end = back + load; charge_end = unload_end + charge
   Unknown travel / inspection / load / charge time refuses (UNKNOWN_* in Python).
B. Site assessment (SITE_NOT_ASSESSED / SITE_NOT_ELIGIBLE / SITE_RESTRICTED / TASK_EXCLUDED): the newest
   (assessed_at) approved, non-superseded assessment with valid_until >= the visit's site-local date
   (``planning.services.current_assessment``); none refuses; 'not_eligible' refuses; 'eligible_restricted'
   refuses (Python: review, which also prevents "ready"); any revision task in excluded_tasks refuses.
C. Vehicle (VEHICLE_MISSING / VEHICLE_CAPACITY_UNKNOWN / VEHICLE_RESTRAINT_UNKNOWN): assigned, robot_capacity
   known and >= 1, restraint_kit non-empty.
D. Crew (CREW_INCOMPLETE / CREW_SAME_PERSON / QUALIFICATION): lead and second present and distinct; lead holds
   field_lead and second field_operator or field_lead for [insp_start, unload_end]; both hold robot_handler
   for [load_start, unload_end]; unrevoked, valid_from <= window start and valid_to >= window end (exactly
   ``planning.services._qualified``). This REPLACES 0005's simplified "valid now through on_site_end" window.
E. Reservations (RESERVATIONS_MISSING, and CONFLICT via coverage + the planning exclusion constraint):
   no reservation of the current revision is released; at least one is active; every live-plan interval is
   covered (same resource, same purpose, starts_at <= start, ends_at >= end) by an ACTIVE reservation:
       robot: inspection, loading, travel_out, on_site, travel_back, unloading, charging
       lead and second: field_shift [insp_start, unload_end)
       vehicle: trip [load_start, unload_end)
   Because active reservations cannot overlap (EXCLUDE USING gist, planning/0002), full coverage by this
   visit's own active reservations also proves no other visit holds the resources then (Python CONFLICT).
F. Vehicle trip (stricter than Python, EM request): the current revision has an unreleased vehicle trip on its
   vehicle carrying its robot. The Python evaluator does not check the trip row itself (only the vehicle's
   'trip' reservation), so a raw trip release alone is refused here but not by the evaluator (the command
   then fails closed with the database's Conflict).

Everything from 0005 is unchanged (issuer role, visit, snapshot, policy/mode, robot eligibility, consent,
recovery plan, evidence, battery, equipment, evidence_ids, expiry, insert as owner). Locks are unchanged: the
new reads are guarded by rows already share-locked (assessments and consent -> site version; reservations,
trips -> visit version; vehicle update -> visit versions; qualifications -> person version; profile ->
pattern version; settings -> advisory lock).

The only evaluator items still NOT re-proven in SQL: RECOVERY_ONCALL_NOT_ENGINEER (a "review" item about a
role on the on-call person, not a blocker) and the human identity of the caller (the database sees ops_app).
tests/test_readiness_authz_boundary.py maps every evaluator/planning blocker code to its SQL refusal.
"""

from django.db import migrations

from platform_core.locks import SETTINGS_LOCK_KEY

HELPERS = r"""
-- Person `pid` holds one of `kinds`, unrevoked, valid for the whole [w0, w1] (planning.services._qualified).
CREATE OR REPLACE FUNCTION readiness_qualified_window(pid bigint, kinds text[], w0 timestamptz, w1 timestamptz)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM planning_qualification q
                 WHERE q.person_id = pid AND q.kind = ANY (kinds) AND q.revoked_at IS NULL
                   AND q.valid_from <= w0 AND q.valid_to >= w1)
$$;

-- An ACTIVE reservation of revision `rid` on resource `res` for `purpose` covers [s0, s1].
CREATE OR REPLACE FUNCTION readiness_reservation_covers(rid bigint, res bigint, purpose text,
                                                       s0 timestamptz, s1 timestamptz)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT res IS NOT NULL AND EXISTS (
    SELECT 1 FROM planning_reservation x
    WHERE x.revision_id = rid AND x.resource_id = res AND x.purpose = readiness_reservation_covers.purpose
      AND x.released_at IS NULL AND x.starts_at <= s0 AND x.ends_at >= s1)
$$;

DROP FUNCTION IF EXISTS readiness_qualified(bigint, text[], timestamptz);
"""

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
  asmt planning_siteassessment; veh planning_vehicle;
  ev readiness_readinessevidence; req jsonb; e jsonb;
  snap_robot bigint; snap_manifest bigint; snap_policy bigint; active bigint; latest_plan bigint;
  problem text; expected_mode text; form text; n_consent int; ttl int; max_age int; k text; soc int;
  insp_txt text; insp_min int; load_min int; charge_min int;
  depart timestamptz; load_start timestamptz; insp_start timestamptz; back timestamptz;
  unload_end timestamptz; charge_end timestamptz;
  res_robot bigint; res_lead bigint; res_second bigint; res_vehicle bigint; span record; excluded text[];
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

  -- ---------------------------------------------------------------- issuer
  IF NOT has_active_role(p_issued_by, 'ops_lead') THEN
    PERFORM fleet_fail('authorization refused: the issuer does not hold an active ops_lead role');
  END IF;

  -- ---------------------------------------------------------------- visit
  SELECT * INTO v FROM planning_visit WHERE id = p_visit_id;
  IF v.status <> 'confirmed' OR v.current_revision_id IS NULL THEN
    PERFORM fleet_fail(format('authorization refused: only a confirmed visit can be authorized (visit is %s)',
                              v.status));
  END IF;
  IF EXISTS (SELECT 1 FROM readiness_departureevent d WHERE d.visit_id = v.id) THEN
    PERFORM fleet_fail('authorization refused: this visit has already departed');
  END IF;

  -- ---------------------------------------------------------------- snapshot equals the live guards
  problem := readiness_guard_problem(p_guard_snapshot, p_visit_id, p_recovery_plan_id);
  IF problem IS NOT NULL THEN
    PERFORM fleet_fail('authorization refused: guard snapshot does not match the live guards: ' || problem);
  END IF;
  SELECT * INTO r FROM planning_visitrevision WHERE id = v.current_revision_id;
  SELECT * INTO st FROM planning_site WHERE id = v.site_id;
  SELECT * INTO rb FROM fleet_robot WHERE id = r.robot_id;
  SELECT * INTO m FROM fleet_configurationmanifest WHERE id = rb.current_manifest_id;

  -- ---------------------------------------------------------------- policy and mode
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

  -- ---------------------------------------------------------------- site assessment (B)
  SELECT a.* INTO asmt FROM planning_siteassessment a
   WHERE a.site_id = st.id
     AND NOT EXISTS (SELECT 1 FROM planning_siteassessment s WHERE s.supersedes_id = a.id)
     AND EXISTS (SELECT 1 FROM planning_assessmentapproval ap WHERE ap.assessment_id = a.id)
     AND a.valid_until >= (r.on_site_start AT TIME ZONE st.timezone)::date
   ORDER BY a.assessed_at DESC, a.id DESC LIMIT 1;
  IF asmt.id IS NULL THEN
    PERFORM fleet_fail('authorization refused: no approved, current site assessment covers the visit date');
  END IF;
  IF asmt.decision = 'not_eligible' THEN
    PERFORM fleet_fail('authorization refused: the site was assessed as not eligible');
  END IF;
  IF asmt.decision <> 'eligible' THEN
    PERFORM fleet_fail(format('authorization refused: the site assessment has restrictions (%s) that need review',
                              asmt.decision));
  END IF;
  excluded := ARRAY(SELECT t FROM jsonb_array_elements_text(coalesce(r.tasks, '[]'::jsonb)) t
                    WHERE jsonb_typeof(asmt.excluded_tasks) = 'array' AND asmt.excluded_tasks ? t ORDER BY t);
  IF cardinality(excluded) > 0 THEN
    PERFORM fleet_fail('authorization refused: tasks excluded at this site: ' || array_to_string(excluded, ', '));
  END IF;

  -- ---------------------------------------------------------------- robot planning eligibility basics
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

  -- ---------------------------------------------------------------- consent
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

  -- ---------------------------------------------------------------- live plan timeline (A)
  IF r.travel_out_min IS NULL OR r.travel_back_min IS NULL THEN
    PERFORM fleet_fail('authorization refused: travel time is unknown');
  END IF;
  insp_txt := btrim(coalesce(fleet_setting('pre_visit_inspection_min'), ''));
  IF insp_txt ~ '^[0-9]{1,6}$' THEN insp_min := insp_txt::int; END IF;
  IF insp_min IS NULL OR insp_min <= 0 THEN
    PERFORM fleet_fail('authorization refused: pre-visit inspection duration is not configured');
  END IF;
  load_min := CASE WHEN prof.load_unload_min > 0 THEN prof.load_unload_min END;
  charge_min := CASE WHEN prof.charge_turnaround_min > 0 THEN prof.charge_turnaround_min END;
  IF load_min IS NULL THEN PERFORM fleet_fail('authorization refused: loading/unloading time is unknown'); END IF;
  IF charge_min IS NULL THEN PERFORM fleet_fail('authorization refused: charge turnaround is unknown'); END IF;
  depart := r.on_site_start - make_interval(mins => r.travel_out_min);
  load_start := depart - make_interval(mins => load_min);
  insp_start := load_start - make_interval(mins => insp_min);
  back := r.on_site_end + make_interval(mins => r.travel_back_min);
  unload_end := back + make_interval(mins => load_min);
  charge_end := unload_end + make_interval(mins => charge_min);

  -- ---------------------------------------------------------------- crew (D)
  IF r.lead_id IS NULL OR r.second_id IS NULL THEN
    PERFORM fleet_fail('authorization refused: the revision needs a lead and a second person');
  END IF;
  IF r.lead_id = r.second_id THEN
    PERFORM fleet_fail('authorization refused: lead and second must be different people');
  END IF;
  IF NOT readiness_qualified_window(r.lead_id, ARRAY['field_lead'], insp_start, unload_end) THEN
    PERFORM fleet_fail(format('authorization refused: lead %s is not qualified as field lead for the whole activity',
                              (SELECT x.code FROM planning_person x WHERE x.id = r.lead_id)));
  END IF;
  IF NOT readiness_qualified_window(r.second_id, ARRAY['field_operator', 'field_lead'], insp_start, unload_end) THEN
    PERFORM fleet_fail(format('authorization refused: second %s is not qualified as field operator for the whole activity',
                              (SELECT x.code FROM planning_person x WHERE x.id = r.second_id)));
  END IF;
  IF NOT readiness_qualified_window(r.lead_id, ARRAY['robot_handler'], load_start, unload_end) THEN
    PERFORM fleet_fail(format('authorization refused: lead %s is not qualified for robot handling during the activity',
                              (SELECT x.code FROM planning_person x WHERE x.id = r.lead_id)));
  END IF;
  IF NOT readiness_qualified_window(r.second_id, ARRAY['robot_handler'], load_start, unload_end) THEN
    PERFORM fleet_fail(format('authorization refused: second %s is not qualified for robot handling during the activity',
                              (SELECT x.code FROM planning_person x WHERE x.id = r.second_id)));
  END IF;

  -- ---------------------------------------------------------------- vehicle (C)
  IF r.vehicle_id IS NULL THEN PERFORM fleet_fail('authorization refused: no vehicle assigned'); END IF;
  SELECT * INTO veh FROM planning_vehicle WHERE id = r.vehicle_id;
  IF veh.robot_capacity IS NULL OR veh.robot_capacity < 1 THEN
    PERFORM fleet_fail(format('authorization refused: vehicle %s robot capacity is unknown', veh.code));
  END IF;
  IF btrim(coalesce(veh.restraint_kit, '')) = '' THEN
    PERFORM fleet_fail(format('authorization refused: vehicle %s has no recorded restraint kit', veh.code));
  END IF;

  -- ---------------------------------------------------------------- reservations (E) and trip (F)
  IF EXISTS (SELECT 1 FROM planning_reservation x WHERE x.revision_id = r.id AND x.released_at IS NOT NULL) THEN
    PERFORM fleet_fail('authorization refused: reservation(s) for the current revision were released');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM planning_reservation x WHERE x.revision_id = r.id AND x.released_at IS NULL) THEN
    PERFORM fleet_fail('authorization refused: no active reservations for the current revision');
  END IF;
  res_robot := (SELECT x.id FROM planning_resource x WHERE x.kind = 'robot' AND x.robot_id = rb.id);
  res_lead := (SELECT x.id FROM planning_resource x WHERE x.kind = 'person' AND x.person_id = r.lead_id);
  res_second := (SELECT x.id FROM planning_resource x WHERE x.kind = 'person' AND x.person_id = r.second_id);
  res_vehicle := (SELECT x.id FROM planning_resource x WHERE x.kind = 'vehicle' AND x.vehicle_id = r.vehicle_id);
  FOR span IN
    SELECT * FROM (VALUES
      ('robot', res_robot, 'inspection', insp_start, load_start),
      ('robot', res_robot, 'loading', load_start, depart),
      ('robot', res_robot, 'travel_out', depart, r.on_site_start),
      ('robot', res_robot, 'on_site', r.on_site_start, r.on_site_end),
      ('robot', res_robot, 'travel_back', r.on_site_end, back),
      ('robot', res_robot, 'unloading', back, unload_end),
      ('robot', res_robot, 'charging', unload_end, charge_end),
      ('lead', res_lead, 'field_shift', insp_start, unload_end),
      ('second', res_second, 'field_shift', insp_start, unload_end),
      ('vehicle', res_vehicle, 'trip', load_start, unload_end)
    ) AS t(who, res, purpose, s0, s1)
  LOOP
    IF NOT readiness_reservation_covers(r.id, span.res, span.purpose, span.s0, span.s1) THEN
      PERFORM fleet_fail(format('authorization refused: %s %s is not covered by an active reservation',
                                span.who, span.purpose));
    END IF;
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM planning_vehicletrip t
                 WHERE t.revision_id = r.id AND t.vehicle_id = r.vehicle_id AND t.released_at IS NULL
                   AND EXISTS (SELECT 1 FROM planning_tripcargo c WHERE c.trip_id = t.id AND c.robot_id = rb.id)) THEN
    PERFORM fleet_fail('authorization refused: no unreleased vehicle trip carries the robot for this revision');
  END IF;

  -- ---------------------------------------------------------------- recovery plan
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

  -- ---------------------------------------------------------------- evidence per policy requirement
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

  -- ---------------------------------------------------------------- battery
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

  -- ---------------------------------------------------------------- equipment
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

  -- ---------------------------------------------------------------- evidence_ids = exactly what was relied on
  IF jsonb_typeof(p_evidence_ids) IS DISTINCT FROM 'array' THEN
    PERFORM fleet_fail('authorization refused: evidence_ids must be a list');
  END IF;
  FOR e IN SELECT * FROM jsonb_array_elements(p_evidence_ids) LOOP
    IF NOT readiness_positive_int(e) THEN PERFORM fleet_fail('authorization refused: evidence_ids must be a list of ids'); END IF;
  END LOOP;
  given := ARRAY(SELECT DISTINCT (x #>> '{}')::bigint FROM jsonb_array_elements(p_evidence_ids) x ORDER BY 1);
  IF cardinality(given) <> jsonb_array_length(p_evidence_ids)
     OR given IS DISTINCT FROM ARRAY(SELECT DISTINCT x FROM unnest(relied) x ORDER BY 1) THEN
    PERFORM fleet_fail(format('authorization refused: evidence_ids must be exactly the evidence relied on %s',
                              to_jsonb(ARRAY(SELECT DISTINCT x FROM unnest(relied) x ORDER BY 1))));
  END IF;

  -- ---------------------------------------------------------------- expiry
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
"""


class Migration(migrations.Migration):
    dependencies = [("readiness", "0005_authorization_write_boundary")]
    operations = [
        migrations.RunSQL(
            HELPERS + SQL.replace("{settings_key}", str(SETTINGS_LOCK_KEY)),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]

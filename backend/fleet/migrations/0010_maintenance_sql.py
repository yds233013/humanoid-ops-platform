"""v2 02C maintenance: the trusted write boundary, the status function and privileges (contract 11, 8.2, 8.6, 8.8).

WRITE BOUNDARY. ops_app has SELECT only on fleet_maintenancerule, fleet_maintenanceruleapproval,
fleet_maintenancerulewithdrawal and fleet_maintenancerecord (no sequence use). All four are append-only
(reject_mutation for UPDATE, DELETE and TRUNCATE, for every role). Every write goes through one of these
SECURITY DEFINER functions owned by ops_owner, with a fixed search_path, EXECUTE revoked from PUBLIC and granted
to ops_app:

    fleet_propose_maintenance_rule(actor, pattern, requirement_code, calendar_days, visit_count, active_hours,
                                   warning_pct, inspection_template, effective_from, source, procedure_ref,
                                   notes, request_id) -> rule id            (engineer, engineering_lead)
    fleet_approve_maintenance_rule(actor, rule, slot, request_id)           (engineering slot: engineering_lead,
                                                                             safety slot: safety_reviewer; never
                                                                             the author; two distinct people)
    fleet_withdraw_maintenance_rule(actor, rule, reason, request_id)        (engineering_lead, safety_reviewer)
    fleet_record_maintenance(actor, robot, requirement_code, notes, request_id) -> record id
                                                                            (hub_technician, engineer or
                                                                             engineering_lead AND a valid
                                                                             maintenance_technician
                                                                             qualification at record time)

Each one checks that the actor is an ACTIVE user with an ACTIVE role (has_active_role, as v1 guarded functions do:
the database cannot authenticate the human, it checks the named actor; NULL or unknown actors are refused), the
invariants, and writes the audit row. A role refusal raises insufficient_privilege (42501), anything else
check_violation (23514); a refused call writes nothing. The helpers below are NOT callable by ops_app (EXECUTE
revoked from PUBLIC, never granted).

Rule approvals and withdrawals bump the configuration pattern (fleet_bump_pattern: the pattern row and every robot
on it, FOR UPDATE), exactly like periodic inspection rules, so a guard snapshot taken before the decision no longer
matches. A maintenance record bumps the robot (its row FOR UPDATE: the per-robot lock planning, authorization and
departure take), so a record and a booking or authorization on the same robot are totally ordered.

Locks, global order: settings advisory lock SHARED (the record reads the deployment mode) -> pattern -> robot.

STATUS (read-only, STABLE, security invoker; EXECUTE granted to ops_app because the eligibility view, the planning
triggers, the authorization function and the Python read models all call it):

    fleet_maintenance_status_set(robots[], production, at)  -- set-based, used by operations_robot_eligibility
    fleet_maintenance_status(robot, production, at)         -- = the set function for one robot (same code)
    fleet_maintenance_block_reasons(robot, production, at)  -- text[] of blocking reasons when the gate is active,
                                                               else {}
    fleet_maintenance_gate_active()                         -- maintenance_gate_active setting is exactly 'true'

Rule in force for (pattern, requirement) at ``at``: the highest version_no whose two approval slots
(engineering, safety) were filled at or before ``at`` and whose effective_from <= ``at``. If that version is
withdrawn there is NO rule for the requirement (no fallback to an older version, as periodic rules). In production
a synthetic_demo rule is not usable either. Requirements are the codes with a rule in force; a configuration with
none gets one row with requirement_code NULL.

Counters since the last qualifying record (this robot, this requirement, a rule of the robot's current pattern,
performer covered as maintenance_technician at performed_at, revocation checked now):
  calendar   performed_at + calendar_days;
  visits     execution runs of the robot departed after the record and closed (trip closure) by ``at``;
  hours      sum of session intervals start/resume -> pause/stop of those runs; a run with a start but no stop
             makes the hours UNKNOWN (never zero).
States: no_rule | unknown | overdue | inspection_missing | due_soon | not_due. Blocking: no_rule, unknown, overdue,
inspection_missing. Due soon: any counter at or past (100 - warning_pct)% of its limit.
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE
GUARDED = [
    "fleet_maintenancerule",
    "fleet_maintenanceruleapproval",
    "fleet_maintenancerulewithdrawal",
    "fleet_maintenancerecord",
]

HELPERS = r"""
CREATE OR REPLACE FUNCTION fleet_maint_deny(msg text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '%', msg USING ERRCODE = 'insufficient_privilege'; END $$;

CREATE OR REPLACE FUNCTION fleet_maint_actor_has(uid bigint, roles text[]) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT uid IS NOT NULL
     AND EXISTS (SELECT 1 FROM auth_user u WHERE u.id = uid AND u.is_active)
     AND EXISTS (SELECT 1 FROM unnest(roles) r WHERE has_active_role(uid, r))
$$;

CREATE OR REPLACE FUNCTION fleet_maint_audit(p_actor bigint, p_cmd text, p_type text, p_id text, p_payload jsonb,
                                             p_result jsonb, p_rid text) RETURNS void
LANGUAGE sql AS $$
  INSERT INTO platform_core_auditevent (occurred_at, actor_id, command, outcome, entity_type, entity_id, payload,
                                        result, reason, request_id)
  VALUES (clock_timestamp(), p_actor, p_cmd, 'committed', p_type, p_id, p_payload, p_result, '',
          left(coalesce(p_rid, ''), 64))
$$;
"""

STATUS = r"""
CREATE OR REPLACE FUNCTION fleet_maintenance_gate_active() RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT coalesce(fleet_setting('maintenance_gate_active'), 'false') = 'true' $$;

CREATE OR REPLACE FUNCTION fleet_maint_utc(t timestamptz) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT to_char(t AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || ' UTC'
$$;

CREATE OR REPLACE FUNCTION fleet_maintenance_status_set(p_robots bigint[], p_production boolean, p_at timestamptz)
RETURNS TABLE (robot_id bigint, robot_code text, pattern_code text, requirement_code text, state text,
               blocking boolean, rule_id bigint, rule_version integer, rule_source text, withdrawn boolean,
               calendar_days integer, visit_count integer, active_hours integer, warning_pct integer,
               record_id bigint, performed_at timestamptz, visits_used integer, hours_used numeric,
               open_runs text, due_at timestamptz, inspection_template_id bigint, inspection_id bigint,
               reason text)
LANGUAGE sql STABLE AS $$
WITH rb AS (
  SELECT r.id, r.code::text AS code, r.current_manifest_id, m.pattern_id, p.code::text AS pattern_code
  FROM fleet_robot r
  LEFT JOIN fleet_configurationmanifest m ON m.id = r.current_manifest_id
  LEFT JOIN fleet_manifestpattern p ON p.id = m.pattern_id
  WHERE r.id = ANY (p_robots)
),
cur AS (
  SELECT DISTINCT ON (mr.pattern_id, mr.requirement_code) mr.*,
         EXISTS (SELECT 1 FROM fleet_maintenancerulewithdrawal w
                 WHERE w.rule_id = mr.id AND w.withdrawn_at <= p_at) AS is_withdrawn
  FROM fleet_maintenancerule mr
  WHERE mr.pattern_id IN (SELECT rb.pattern_id FROM rb) AND mr.effective_from <= p_at
    AND (SELECT count(DISTINCT a.slot) FROM fleet_maintenanceruleapproval a
         WHERE a.rule_id = mr.id AND a.slot IN ('engineering', 'safety') AND a.approved_at <= p_at) = 2
  ORDER BY mr.pattern_id, mr.requirement_code, mr.version_no DESC
),
req AS (
  SELECT rb.id AS robot_id, rb.code AS robot_code, rb.current_manifest_id, rb.pattern_id, rb.pattern_code,
         c.id AS rule_id, c.requirement_code::text AS requirement_code, c.version_no::int AS rule_version,
         c.source::text AS rule_source, c.is_withdrawn, c.calendar_days::int AS calendar_days,
         c.visit_count::int AS visit_count, c.active_hours::int AS active_hours, c.warning_pct::int AS warning_pct,
         c.inspection_template_id,
         (c.id IS NOT NULL AND NOT c.is_withdrawn AND (c.source = 'engineering_doc' OR NOT p_production)) AS usable
  FROM rb LEFT JOIN cur c ON c.pattern_id = rb.pattern_id
),
cnt AS (
  SELECT q.*, x.id AS record_id, x.performed_at, v.visits_used, h.hours_used, h.open_runs, pi.id AS inspection_id,
         x.performed_at + make_interval(days => q.calendar_days) AS cal_due
  FROM req q
  LEFT JOIN LATERAL (
    SELECT mx.id, mx.performed_at FROM fleet_maintenancerecord mx
    JOIN fleet_maintenancerule xr ON xr.id = mx.rule_id
    WHERE q.usable AND mx.robot_id = q.robot_id AND mx.requirement_code = q.requirement_code
      AND xr.pattern_id = q.pattern_id AND mx.performed_at <= p_at
      AND planning_maintainer_covered(mx.performed_by_id, mx.performed_at)
    ORDER BY mx.performed_at DESC, mx.id DESC LIMIT 1
  ) x ON true
  LEFT JOIN LATERAL (
    SELECT count(*)::int AS visits_used FROM execution_executionrun er
    JOIN execution_tripclosure tc ON tc.run_id = er.id
    WHERE x.id IS NOT NULL AND er.robot_id = q.robot_id AND er.recorded_at > x.performed_at
      AND tc.recorded_at <= p_at
  ) v ON true
  LEFT JOIN LATERAL (
    SELECT coalesce(sum(extract(epoch FROM (s.next_at - s.recorded_at)))
                    FILTER (WHERE s.kind IN ('start', 'resume') AND s.next_kind IN ('pause', 'stop')), 0)
             / 3600.0 AS hours_used,
           string_agg(DISTINCT s.run_code, ', ') FILTER (WHERE s.open_run) AS open_runs
    FROM (
      SELECT e.kind, e.recorded_at, er.code::text AS run_code,
             lead(e.kind) OVER w AS next_kind, lead(e.recorded_at) OVER w AS next_at,
             (bool_or(e.kind = 'start') OVER (PARTITION BY e.run_id)
              AND NOT bool_or(e.kind = 'stop') OVER (PARTITION BY e.run_id)) AS open_run
      FROM execution_sessionevent e JOIN execution_executionrun er ON er.id = e.run_id
      WHERE x.id IS NOT NULL AND er.robot_id = q.robot_id AND er.recorded_at > x.performed_at
        AND e.kind IN ('start', 'pause', 'resume', 'stop') AND e.recorded_at <= p_at
      WINDOW w AS (PARTITION BY e.run_id ORDER BY e.seq)
    ) s
  ) h ON true
  LEFT JOIN LATERAL (
    SELECT i.id FROM fleet_inspection i
    WHERE x.id IS NOT NULL AND q.inspection_template_id IS NOT NULL AND i.robot_id = q.robot_id
      AND i.manifest_id = q.current_manifest_id AND i.template_id = q.inspection_template_id
      AND i.result = 'PASS' AND i.complete AND i.submitted_at >= x.performed_at AND i.submitted_at <= p_at
      AND fleet_template_usable(i.template_id, 'periodic', p_production)
      AND planning_inspection_signer_problem(i.id) IS NULL
    ORDER BY i.submitted_at DESC, i.id DESC LIMIT 1
  ) pi ON true
),
st AS (
  SELECT c.*,
         (c.calendar_days IS NOT NULL AND p_at >= c.cal_due) AS cal_over,
         (c.visit_count IS NOT NULL AND c.visits_used >= c.visit_count) AS vis_over,
         (c.active_hours IS NOT NULL AND c.hours_used >= c.active_hours) AS hrs_over,
         (c.calendar_days IS NOT NULL
          AND p_at >= c.performed_at + make_interval(secs => c.calendar_days * 864.0 * (100 - c.warning_pct)))
         OR (c.visit_count IS NOT NULL AND c.visits_used * 100 >= c.visit_count * (100 - c.warning_pct))
         OR (c.active_hours IS NOT NULL AND c.hours_used * 100 >= c.active_hours * (100 - c.warning_pct)) AS soon
  FROM cnt c
),
fin AS (
  SELECT s.*,
         CASE WHEN NOT coalesce(s.usable, false) THEN 'no_rule'
              WHEN s.record_id IS NULL THEN 'unknown'
              WHEN s.cal_over OR s.vis_over OR s.hrs_over THEN 'overdue'
              WHEN s.active_hours IS NOT NULL AND s.open_runs IS NOT NULL THEN 'unknown'
              WHEN s.inspection_template_id IS NOT NULL AND s.inspection_id IS NULL THEN 'inspection_missing'
              WHEN s.soon THEN 'due_soon'
              ELSE 'not_due' END AS st
  FROM st s
)
SELECT f.robot_id, f.robot_code, f.pattern_code, f.requirement_code, f.st,
       f.st IN ('no_rule', 'unknown', 'overdue', 'inspection_missing'),
       f.rule_id, f.rule_version, f.rule_source, coalesce(f.is_withdrawn, false), f.calendar_days, f.visit_count,
       f.active_hours, f.warning_pct, f.record_id, f.performed_at, f.visits_used, round(f.hours_used, 2),
       f.open_runs, f.cal_due, f.inspection_template_id, f.inspection_id,
       CASE f.st
         WHEN 'no_rule' THEN
           CASE WHEN f.pattern_code IS NULL THEN 'No approved maintenance rule: no configuration recorded'
                WHEN f.rule_id IS NULL THEN 'No approved maintenance rule for configuration ' || f.pattern_code
                WHEN f.is_withdrawn THEN 'Maintenance rule ' || f.requirement_code || ' v' || f.rule_version
                  || ' was withdrawn; no maintenance rule in force for configuration ' || f.pattern_code
                ELSE 'Maintenance rule ' || f.requirement_code || ' v' || f.rule_version
                  || ' is demo data, not an engineering requirement (not usable in production)' END
         WHEN 'unknown' THEN
           CASE WHEN f.record_id IS NULL
                THEN 'Maintenance ' || f.requirement_code || ' status unknown: no maintenance record for this robot'
                ELSE 'Maintenance ' || f.requirement_code || ' status unknown: active hours unknown (run '
                  || f.open_runs || ' has a session start but no stop)' END
         WHEN 'overdue' THEN
           'Maintenance ' || f.requirement_code || ' overdue: ' || array_to_string(ARRAY[
             CASE WHEN f.cal_over THEN 'calendar limit reached ' || fleet_maint_utc(f.cal_due) END,
             CASE WHEN f.vis_over THEN f.visits_used || ' of ' || f.visit_count || ' visits' END,
             CASE WHEN f.hrs_over THEN round(f.hours_used, 1) || ' of ' || f.active_hours || ' active hours' END
           ], ', ')
         WHEN 'inspection_missing' THEN
           'Maintenance ' || f.requirement_code || ' recorded, but the required post-maintenance inspection is missing'
         WHEN 'due_soon' THEN 'Maintenance ' || f.requirement_code || ' due soon'
         ELSE NULL END
FROM fin f
$$;

CREATE OR REPLACE FUNCTION fleet_maintenance_status(p_robot bigint, p_production boolean, p_at timestamptz)
RETURNS TABLE (robot_id bigint, robot_code text, pattern_code text, requirement_code text, state text,
               blocking boolean, rule_id bigint, rule_version integer, rule_source text, withdrawn boolean,
               calendar_days integer, visit_count integer, active_hours integer, warning_pct integer,
               record_id bigint, performed_at timestamptz, visits_used integer, hours_used numeric,
               open_runs text, due_at timestamptz, inspection_template_id bigint, inspection_id bigint,
               reason text)
LANGUAGE sql STABLE AS $$
  SELECT * FROM fleet_maintenance_status_set(ARRAY[p_robot], p_production, p_at)
$$;

-- The blocking reasons of one robot, in requirement order (a configuration-level row first), when the gate is
-- active; {} while it is not activated (status is still computed and shown, nothing is blocked).
CREATE OR REPLACE FUNCTION fleet_maintenance_block_reasons(p_robot bigint, p_production boolean, p_at timestamptz)
RETURNS text[] LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN NOT fleet_maintenance_gate_active() THEN '{}'::text[]
              ELSE coalesce((SELECT array_agg(s.reason ORDER BY s.requirement_code NULLS FIRST)
                             FROM fleet_maintenance_status(p_robot, p_production, p_at) s WHERE s.blocking),
                            '{}'::text[]) END
$$;
"""

FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION fleet_propose_maintenance_rule(
  p_actor bigint, p_pattern bigint, p_code text, p_calendar_days int, p_visit_count int, p_active_hours int,
  p_warning_pct int, p_template bigint, p_effective_from timestamptz, p_source text, p_procedure_ref text,
  p_notes text, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE pat fleet_manifestpattern; tpl fleet_inspectiontemplate; now_ts timestamptz := clock_timestamp();
        eff timestamptz; ver int; new_id bigint; code text := upper(btrim(coalesce(p_code, '')));
BEGIN
  IF NOT fleet_maint_actor_has(p_actor, ARRAY['engineer', 'engineering_lead']) THEN
    PERFORM fleet_maint_deny('Only an engineer or the engineering lead can propose a maintenance rule');
  END IF;
  SELECT * INTO pat FROM fleet_manifestpattern WHERE id = p_pattern FOR UPDATE;  -- version numbering
  IF NOT FOUND THEN PERFORM fleet_fail('No such configuration'); END IF;
  IF code !~ '^[A-Z0-9][A-Z0-9_-]{0,39}$' THEN
    PERFORM fleet_fail('The requirement code uses capital letters, digits, dashes and underscores (up to 40)');
  END IF;
  IF p_calendar_days IS NULL AND p_visit_count IS NULL AND p_active_hours IS NULL THEN
    PERFORM fleet_fail('Give at least one trigger: calendar days, completed visits or active hours');
  END IF;
  IF p_warning_pct IS NULL OR p_warning_pct < 0 OR p_warning_pct > 90 THEN
    PERFORM fleet_fail('The warning margin is a whole percentage from 0 to 90');
  END IF;
  IF p_source IS NULL OR p_source NOT IN ('synthetic_demo', 'engineering_doc') THEN
    PERFORM fleet_fail('The source must be synthetic_demo or engineering_doc');
  END IF;
  IF p_source = 'engineering_doc' AND NOT fleet_nonblank(p_procedure_ref) THEN
    PERFORM fleet_fail('An engineering_doc maintenance rule needs a procedure reference');
  END IF;
  IF p_template IS NOT NULL THEN
    SELECT * INTO tpl FROM fleet_inspectiontemplate WHERE id = p_template;
    IF NOT FOUND OR tpl.pattern_id <> pat.id THEN
      PERFORM fleet_fail('The post-maintenance inspection template must belong to this configuration');
    END IF;
    IF fleet_template_withdrawn(tpl.id) THEN PERFORM fleet_fail('That inspection template was withdrawn'); END IF;
  END IF;
  -- never backdated: two minutes of tolerance for the app host's clock, then stored as at least now
  eff := coalesce(p_effective_from, now_ts);
  IF eff < now_ts - interval '2 minutes' THEN PERFORM fleet_fail('The effective time cannot be in the past'); END IF;
  eff := greatest(eff, now_ts);
  ver := coalesce((SELECT max(version_no) FROM fleet_maintenancerule WHERE pattern_id = pat.id
                   AND requirement_code = code), 0) + 1;
  INSERT INTO fleet_maintenancerule (pattern_id, requirement_code, version_no, calendar_days, visit_count,
                                     active_hours, warning_pct, inspection_template_id, effective_from, source,
                                     procedure_ref, notes, created_by_id, created_at)
  VALUES (pat.id, code, ver, p_calendar_days, p_visit_count, p_active_hours, p_warning_pct, p_template, eff,
          p_source, btrim(coalesce(p_procedure_ref, '')), btrim(coalesce(p_notes, '')), p_actor, now_ts)
  RETURNING id INTO new_id;
  PERFORM fleet_maint_audit(p_actor, 'fleet.propose_maintenance_rule', 'maintenance_rule', new_id::text,
    jsonb_build_object('pattern', pat.code, 'requirement_code', code, 'version_no', ver,
                       'calendar_days', p_calendar_days, 'visit_count', p_visit_count,
                       'active_hours', p_active_hours, 'warning_pct', p_warning_pct, 'inspection_template_id',
                       p_template, 'effective_from', eff, 'source', p_source),
    jsonb_build_object('rule_id', new_id, 'status', 'proposed'), p_rid);
  RETURN new_id;
END $fn$;

CREATE OR REPLACE FUNCTION fleet_approve_maintenance_rule(p_actor bigint, p_rule bigint, p_slot text, p_rid text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE r fleet_maintenancerule; role text := fleet_slot_role(p_slot);
BEGIN
  IF role IS NULL THEN PERFORM fleet_fail('The approval slot must be engineering or safety'); END IF;
  IF NOT fleet_maint_actor_has(p_actor, ARRAY[role]) THEN
    PERFORM fleet_maint_deny(format('The %s approval needs an active %s', p_slot, replace(role, '_', ' ')));
  END IF;
  SELECT * INTO r FROM fleet_maintenancerule WHERE id = p_rule;
  IF NOT FOUND THEN PERFORM fleet_fail('No such maintenance rule'); END IF;
  PERFORM fleet_bump_pattern(r.pattern_id);  -- pattern + its robots FOR UPDATE: serializes decisions
  IF EXISTS (SELECT 1 FROM fleet_maintenancerulewithdrawal WHERE rule_id = r.id) THEN
    PERFORM fleet_fail('A withdrawn maintenance rule cannot be approved');
  END IF;
  IF p_actor = r.created_by_id THEN PERFORM fleet_fail('The author of a maintenance rule cannot approve it'); END IF;
  IF EXISTS (SELECT 1 FROM fleet_maintenanceruleapproval WHERE rule_id = r.id AND slot = p_slot) THEN
    PERFORM fleet_fail(format('The %s approval of this rule is already recorded', p_slot));
  END IF;
  IF EXISTS (SELECT 1 FROM fleet_maintenanceruleapproval WHERE rule_id = r.id AND approver_id = p_actor) THEN
    PERFORM fleet_fail('The same person cannot fill both approval slots of one maintenance rule');
  END IF;
  INSERT INTO fleet_maintenanceruleapproval (rule_id, slot, approver_id, approved_at)
  VALUES (r.id, p_slot, p_actor, clock_timestamp());
  PERFORM fleet_maint_audit(p_actor, 'fleet.approve_maintenance_rule', 'maintenance_rule', r.id::text,
    jsonb_build_object('slot', p_slot, 'requirement_code', r.requirement_code, 'version_no', r.version_no),
    jsonb_build_object('approved_slots', (SELECT jsonb_agg(slot ORDER BY slot) FROM fleet_maintenanceruleapproval
                                          WHERE rule_id = r.id)), p_rid);
END $fn$;

CREATE OR REPLACE FUNCTION fleet_withdraw_maintenance_rule(p_actor bigint, p_rule bigint, p_reason text, p_rid text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE r fleet_maintenancerule;
BEGIN
  IF NOT fleet_maint_actor_has(p_actor, ARRAY['engineering_lead', 'safety_reviewer']) THEN
    PERFORM fleet_maint_deny('Only the engineering lead or a safety reviewer can withdraw a maintenance rule');
  END IF;
  IF NOT fleet_nonblank(p_reason) OR length(p_reason) > 500 THEN
    PERFORM fleet_fail('Give a reason for the withdrawal (up to 500 characters)');
  END IF;
  SELECT * INTO r FROM fleet_maintenancerule WHERE id = p_rule;
  IF NOT FOUND THEN PERFORM fleet_fail('No such maintenance rule'); END IF;
  PERFORM fleet_bump_pattern(r.pattern_id);
  IF EXISTS (SELECT 1 FROM fleet_maintenancerulewithdrawal WHERE rule_id = r.id) THEN
    PERFORM fleet_fail('That maintenance rule was already withdrawn');
  END IF;
  INSERT INTO fleet_maintenancerulewithdrawal (rule_id, withdrawn_by_id, reason, withdrawn_at)
  VALUES (r.id, p_actor, btrim(p_reason), clock_timestamp());
  PERFORM fleet_maint_audit(p_actor, 'fleet.withdraw_maintenance_rule', 'maintenance_rule', r.id::text,
    jsonb_build_object('reason', btrim(p_reason), 'requirement_code', r.requirement_code,
                       'version_no', r.version_no), '{}'::jsonb, p_rid);
END $fn$;

-- Records maintenance work on one robot for one requirement, against the rule version in force NOW in the
-- current mode. Never touches a hold (J7 stays the only release path), a work order, custody or evidence.
CREATE OR REPLACE FUNCTION fleet_record_maintenance(p_actor bigint, p_robot bigint, p_code text, p_notes text,
                                                    p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE rb fleet_robot; pat bigint; pat_code text; prod boolean; now_ts timestamptz; ru fleet_maintenancerule;
        st record; new_id bigint; code text := upper(btrim(coalesce(p_code, '')));
BEGIN
  IF NOT fleet_maint_actor_has(p_actor, ARRAY['hub_technician', 'engineer', 'engineering_lead']) THEN
    PERFORM fleet_maint_deny('Only a hub technician, an engineer or the engineering lead can record maintenance');
  END IF;
  IF p_notes IS NOT NULL AND length(p_notes) > 1000 THEN PERFORM fleet_fail('Notes are limited to 1000 characters'); END IF;
  PERFORM pg_advisory_xact_lock_shared({settings_key});  -- the mode cannot change until this commits
  SELECT * INTO rb FROM fleet_robot WHERE id = p_robot;
  IF NOT FOUND THEN PERFORM fleet_fail('No such robot'); END IF;
  pat := (SELECT m.pattern_id FROM fleet_configurationmanifest m WHERE m.id = rb.current_manifest_id);
  PERFORM 1 FROM fleet_manifestpattern WHERE id = pat FOR SHARE;
  UPDATE fleet_robot SET version = version WHERE id = p_robot;  -- robot U (bumps its guard version)
  SELECT * INTO rb FROM fleet_robot WHERE id = p_robot;
  IF rb.lifecycle = 'retired' THEN PERFORM fleet_fail(format('Robot %s is retired', rb.code)); END IF;
  IF rb.current_manifest_id IS NULL THEN
    PERFORM fleet_fail(format('Robot %s has no configuration recorded', rb.code));
  END IF;
  now_ts := clock_timestamp();
  IF NOT planning_maintainer_covered(p_actor, now_ts) THEN
    PERFORM fleet_maint_deny('Recording maintenance needs a valid maintenance technician qualification');
  END IF;
  prod := fleet_production_mode();
  pat_code := (SELECT x.code FROM fleet_manifestpattern x WHERE x.id = pat);
  SELECT * INTO st FROM fleet_maintenance_status(p_robot, prod, now_ts) s WHERE s.requirement_code = code;
  IF NOT FOUND THEN
    PERFORM fleet_fail(format('Configuration %s has no maintenance requirement %s in force', pat_code, code));
  END IF;
  IF st.state = 'no_rule' THEN PERFORM fleet_fail(st.reason); END IF;
  SELECT * INTO ru FROM fleet_maintenancerule WHERE id = st.rule_id;
  INSERT INTO fleet_maintenancerecord (robot_id, rule_id, requirement_code, manifest_id, performed_by_id,
                                       performed_at, notes)
  VALUES (p_robot, ru.id, code, rb.current_manifest_id, p_actor, now_ts, btrim(coalesce(p_notes, '')))
  RETURNING id INTO new_id;
  PERFORM fleet_maint_audit(p_actor, 'fleet.record_maintenance', 'maintenance_record', new_id::text,
    jsonb_build_object('robot', rb.code, 'requirement_code', code, 'rule_id', ru.id,
                       'rule_version', ru.version_no, 'previous_state', st.state),
    jsonb_build_object('record_id', new_id, 'performed_at', now_ts,
                       'inspection_required', ru.inspection_template_id IS NOT NULL), p_rid);
  RETURN new_id;
END $fn$;
"""

SIGNATURES = [
    "fleet_propose_maintenance_rule(bigint, bigint, text, int, int, int, int, bigint, timestamptz, text, text, "
    "text, text)",
    "fleet_approve_maintenance_rule(bigint, bigint, text, text)",
    "fleet_withdraw_maintenance_rule(bigint, bigint, text, text)",
    "fleet_record_maintenance(bigint, bigint, text, text, text)",
]
READ_SIGNATURES = [
    "fleet_maintenance_gate_active()",
    "fleet_maintenance_status_set(bigint[], boolean, timestamptz)",
    "fleet_maintenance_status(bigint, boolean, timestamptz)",
    "fleet_maintenance_block_reasons(bigint, boolean, timestamptz)",
    "fleet_maint_utc(timestamptz)",
]
HELPER_SIGNATURES = [
    "fleet_maint_deny(text)",
    "fleet_maint_actor_has(bigint, text[])",
    "fleet_maint_audit(bigint, text, text, text, jsonb, jsonb, text)",
]


def _forward() -> str:
    from platform_core.locks import SETTINGS_LOCK_KEY

    parts = [HELPERS, STATUS, FUNCTIONS.replace("{settings_key}", str(SETTINGS_LOCK_KEY))]
    for t in GUARDED:
        parts.append(
            f"CREATE TRIGGER {t}_no_update_delete BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION reject_mutation();\n"
            f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();\n"
            f"REVOKE ALL ON {t} FROM {APP};\nGRANT SELECT ON {t} TO {APP};\n"
            f"REVOKE ALL ON SEQUENCE {t}_id_seq FROM {APP};\n"
        )
    for s in SIGNATURES + READ_SIGNATURES:
        parts.append(f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\nGRANT EXECUTE ON FUNCTION {s} TO {APP};\n")
    for s in HELPER_SIGNATURES:
        parts.append(f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\n")
    return "\n".join(parts)


def _reverse() -> str:
    parts = [f"DROP FUNCTION IF EXISTS {s};" for s in SIGNATURES]
    parts += [f"DROP FUNCTION IF EXISTS {s};" for s in reversed(READ_SIGNATURES)]
    parts += [f"DROP FUNCTION IF EXISTS {s};" for s in HELPER_SIGNATURES]
    for t in GUARDED:
        parts.append(f"DROP TRIGGER IF EXISTS {t}_no_update_delete ON {t};")
        parts.append(f"DROP TRIGGER IF EXISTS {t}_no_truncate ON {t};")
    return "\n".join(parts)


class Migration(migrations.Migration):
    dependencies = [
        ("fleet", "0009_maintenance"),
        ("planning", "0007_maintenance"),
        ("execution", "0005_s7_incident_release_and_deadlines"),
        ("platform_core", "0002_db_security"),
    ]

    operations = [migrations.RunSQL(_forward(), _reverse())]

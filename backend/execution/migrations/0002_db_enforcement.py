"""Slice 4 invariants enforced by PostgreSQL (docs/04_field_execution_contract.md sections 2, 3, 5, 6, 9).

The runtime role (ops_app) cannot bypass any of these with direct SQL. Every refusal raises ``fleet_fail``
(SQLSTATE check_violation); append-only violations raise ``reject_mutation`` (insufficient_privilege).

APPEND-ONLY. Every execution table rejects UPDATE/DELETE/TRUNCATE. The only mutation is
execution_incidentpolicy.lock_version, bumped by the approval/withdrawal triggers (nested UPDATE only).
Every ``recorded_at`` (and Incident.reported_at) is forced to clock_timestamp().

RUN. execution_executionrun is written ONLY by ``execution_open_run_on_departure()`` (AFTER INSERT on
readiness_departureevent, SECURITY DEFINER, same transaction). ops_app has no INSERT on it (SELECT + UPDATE
remain: UPDATE is what ``SELECT ... FOR UPDATE`` needs; real UPDATEs are rejected by the trigger). The same
trigger writes the robot's ``hub -> vehicle`` administrative CustodyEvent (custodian = revision lead, no
receiver). A run is OPEN until a TripClosure exists for it.

CUSTODY (contract D1, custody move table). ``execution_current_custody(robot_id)`` returns
(kind, verified, custody_event_id): baseline ``hub`` (verified) when the robot has no events and no runs;
otherwise the latest event's ``to_kind`` with its verification state; ``unknown`` when it has runs but no
events or its history is broken (gap, wrong chain). Verified = administrative with a CustodyVerification,
a handoff to a non-unaccounted place with a receiver, or a recovered move (ops_lead + receiver).

PARENT ROW PER STREAM (section 5), locked FOR UPDATE by every insert trigger:
  fleet_robot                CustodyEvent, CustodyVerification, Incident
  execution_executionrun     TripEvent, SiteCheck, SessionEvent, Interruption, InterruptionResolution,
                             RecoveryDisposition, ReturnReconciliation, TripClosure
  execution_incident         SeverityDecision (after incident policies S), IncidentStatusChange,
                             IncidentEscalation, EscalationAck, FaultObservation
  execution_incidentpolicy   IncidentPolicyApproval, IncidentPolicyWithdrawal (UPDATE bump)
No trigger locks an earlier table of the global order after a later one, except as noted for the departure
path (C3 below takes the departure's locks in order BEFORE the readiness departure guard runs).

C1 (departed visit). ``readiness_departed_visit_guard`` (body replaced here) refuses any status change
(including cancellation) and any current_revision change of a departed visit. New guards refuse setting
``released_at`` on planning_reservation / planning_vehicletrip of a departed visit.

C3 (robot at hub). BEFORE INSERT on readiness_departureauthorization (runs after the readiness guards, so
also under readiness_issue_authorization) and on readiness_departureevent (runs FIRST, and takes site S ->
pattern S -> robot U -> persons S -> vehicle S -> visit S -> trip U, the order of record_departure, so the
readiness guard and the run trigger only re-lock rows already held): refused when the robot has an open run
or its custody is not the baseline hub / a verified hub receipt. A departure on a visit that already has
one is left to the unique index (one visit, one departure).
"""

from django.conf import settings
from django.db import migrations

APPEND_ONLY = [
    "execution_executionrun",
    "execution_incidentpolicyapproval",
    "execution_incidentpolicywithdrawal",
    "execution_incident",
    "execution_custodyevent",
    "execution_custodyverification",
    "execution_tripevent",
    "execution_sitechecktemplate",
    "execution_sitecheck",
    "execution_interruption",
    "execution_interruptionresolution",
    "execution_sessionevent",
    "execution_faultobservation",
    "execution_severitydecision",
    "execution_incidentstatuschange",
    "execution_incidentescalation",
    "execution_escalationack",
    "execution_recoverydisposition",
    "execution_returnreconciliation",
    "execution_tripclosure",
]
# Tables whose rows are locked FOR UPDATE by commands (lock_guards) or bumped by triggers: keep UPDATE.
LOCKABLE = ["execution_executionrun", "execution_incident", "execution_incidentpolicy"]

SQL = r"""
-- ================================================================ helpers
CREATE OR REPLACE FUNCTION execution_code(prefix text, n bigint) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT prefix || '-' || lpad(n::text, greatest(4, length(n::text)), '0') $$;

CREATE OR REPLACE FUNCTION execution_demo_mode() RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT coalesce(fleet_setting('demo_mode'), 'false') = 'true' $$;

CREATE OR REPLACE FUNCTION execution_is_field_user(uid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT uid IS NOT NULL AND (has_active_role(uid, 'ops_lead') OR has_active_role(uid, 'field_operator'))
$$;

CREATE OR REPLACE FUNCTION execution_person_is_field_user(pid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT coalesce((SELECT execution_is_field_user(p.user_id) FROM planning_person p WHERE p.id = pid), false)
$$;

CREATE OR REPLACE FUNCTION execution_has_any_role(uid bigint, roles text[]) RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT EXISTS (SELECT 1 FROM unnest(roles) r WHERE has_active_role(uid, r)) $$;

-- Field crew of a run = the run revision's lead and second.
CREATE OR REPLACE FUNCTION execution_is_crew(rid bigint, pid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT pid IS NOT NULL AND EXISTS (
    SELECT 1 FROM execution_executionrun x JOIN planning_visitrevision r ON r.id = x.revision_id
    WHERE x.id = rid AND pid IN (r.lead_id, r.second_id))
$$;

CREATE OR REPLACE FUNCTION execution_user_is_crew(rid bigint, uid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM planning_person p WHERE p.user_id = uid AND execution_is_crew(rid, p.id))
$$;

CREATE OR REPLACE FUNCTION execution_run_closed(rid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT EXISTS (SELECT 1 FROM execution_tripclosure WHERE run_id = rid) $$;

CREATE OR REPLACE FUNCTION execution_open_run_id(p_robot bigint) RETURNS bigint
LANGUAGE sql STABLE AS $$
  SELECT x.id FROM execution_executionrun x
  WHERE x.robot_id = p_robot AND NOT EXISTS (SELECT 1 FROM execution_tripclosure c WHERE c.run_id = x.id)
  ORDER BY x.id DESC LIMIT 1
$$;

-- Unreleased blocking hold on the robot or on its current configuration pattern.
CREATE OR REPLACE FUNCTION execution_robot_held(p_robot bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM fleet_hold h
    WHERE h.blocking AND h.released_at IS NULL
      AND ((h.scope = 'robot' AND h.robot_id = p_robot)
           OR (h.scope = 'manifest_pattern' AND h.pattern_id = (
                 SELECT m.pattern_id FROM fleet_robot r JOIN fleet_configurationmanifest m
                   ON m.id = r.current_manifest_id WHERE r.id = p_robot))))
$$;

-- Valid consents on the current form, at the run's site, covering the run revision's required scopes.
CREATE OR REPLACE FUNCTION execution_valid_consents(rid bigint) RETURNS int
LANGUAGE sql STABLE AS $$
  SELECT count(*)::int
  FROM execution_executionrun x
  JOIN planning_visit v ON v.id = x.visit_id
  JOIN planning_visitrevision r ON r.id = x.revision_id
  JOIN planning_consentrecord c ON c.site_id = v.site_id
  WHERE x.id = rid AND c.withdrawn_at IS NULL AND c.form_version = fleet_setting('consent_form_version')
    AND jsonb_typeof(c.scope) = 'array' AND c.scope @> coalesce(r.required_consent_scopes, '[]'::jsonb)
$$;

CREATE OR REPLACE FUNCTION execution_open_interruption(rid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM execution_interruption i WHERE i.run_id = rid
                 AND NOT EXISTS (SELECT 1 FROM execution_interruptionresolution x WHERE x.interruption_id = i.id))
$$;

CREATE OR REPLACE FUNCTION execution_trip_has(p_trip bigint, k text) RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT EXISTS (SELECT 1 FROM execution_tripevent WHERE trip_id = p_trip AND kind = k) $$;

CREATE OR REPLACE FUNCTION execution_nonblank(t text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$ SELECT btrim(coalesce(t, '')) <> '' $$;

-- ================================================================ custody (contract D1)
CREATE OR REPLACE FUNCTION execution_custody_verified(e execution_custodyevent) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT CASE e.basis
    WHEN 'administrative' THEN EXISTS (SELECT 1 FROM execution_custodyverification v WHERE v.custody_event_id = e.id)
    WHEN 'handoff' THEN e.to_kind <> 'unaccounted' AND e.received_by_id IS NOT NULL
    WHEN 'recovered' THEN e.received_by_id IS NOT NULL
    ELSE false END
$$;

CREATE OR REPLACE FUNCTION execution_current_custody(p_robot_id bigint)
RETURNS TABLE (kind text, verified boolean, custody_event_id bigint)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE e execution_custodyevent; n bigint; lo bigint; hi bigint; broken boolean;
BEGIN
  SELECT count(*), min(seq), max(seq) INTO n, lo, hi FROM execution_custodyevent WHERE robot_id = p_robot_id;
  IF n = 0 THEN
    IF EXISTS (SELECT 1 FROM execution_executionrun WHERE robot_id = p_robot_id) THEN
      RETURN QUERY SELECT 'unknown'::text, false, NULL::bigint;   -- runs but no events
    ELSE
      RETURN QUERY SELECT 'hub'::text, true, NULL::bigint;        -- pre-Slice 4 baseline
    END IF;
    RETURN;
  END IF;
  broken := lo <> 1 OR hi <> n
    OR EXISTS (SELECT 1 FROM execution_custodyevent a WHERE a.robot_id = p_robot_id AND a.seq = 1 AND a.from_kind <> 'hub')
    OR EXISTS (SELECT 1 FROM execution_custodyevent a JOIN execution_custodyevent b
                 ON b.robot_id = a.robot_id AND b.seq = a.seq + 1
               WHERE a.robot_id = p_robot_id AND b.from_kind <> a.to_kind);
  IF broken THEN
    RETURN QUERY SELECT 'unknown'::text, false, NULL::bigint;
    RETURN;
  END IF;
  SELECT * INTO e FROM execution_custodyevent WHERE robot_id = p_robot_id ORDER BY seq DESC LIMIT 1;
  RETURN QUERY SELECT e.to_kind::text, execution_custody_verified(e), e.id;
END $$;

-- C3: NULL when the robot may be authorized / depart; otherwise the reason ("open run" / "not at hub").
CREATE OR REPLACE FUNCTION execution_robot_departure_problem(p_robot bigint) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE rid bigint; c record; rcode text;
BEGIN
  rcode := (SELECT code FROM fleet_robot WHERE id = p_robot);
  rid := execution_open_run_id(p_robot);
  IF rid IS NOT NULL THEN
    RETURN format('robot %s has an open run (%s); it must be reconciled and its trip closed first', rcode,
                  (SELECT code FROM execution_executionrun WHERE id = rid));
  END IF;
  SELECT * INTO c FROM execution_current_custody(p_robot);
  IF c.kind IS DISTINCT FROM 'hub' OR NOT c.verified THEN
    RETURN format('robot %s is not at hub (custody: %s%s)', rcode, c.kind,
                  CASE WHEN c.verified THEN '' ELSE ', unverified' END);
  END IF;
  RETURN NULL;
END $$;

-- ================================================================ append-only
{append_only}

-- ================================================================ incident policy (readiness lifecycle)
CREATE OR REPLACE FUNCTION execution_incident_rules_problem(j jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE r text;
BEGIN
  IF NOT readiness_keys_exactly(j, ARRAY['severity_scale', 'escalation_required_from', 'downgrade_roles']) THEN
    RETURN 'rules must have exactly the keys severity_scale, escalation_required_from, downgrade_roles';
  END IF;
  IF NOT readiness_string_list_ok(j -> 'severity_scale') OR jsonb_array_length(j -> 'severity_scale') = 0 THEN
    RETURN 'severity_scale must be a non-empty list of distinct level names (low to high)';
  END IF;
  IF jsonb_typeof(j -> 'escalation_required_from') <> 'string'
     OR NOT (j -> 'severity_scale') ? (j ->> 'escalation_required_from') THEN
    RETURN 'escalation_required_from must be a level of severity_scale';
  END IF;
  IF NOT readiness_string_list_ok(j -> 'downgrade_roles') THEN
    RETURN 'downgrade_roles must be a list of distinct role names';
  END IF;
  FOR r IN SELECT * FROM jsonb_array_elements_text(j -> 'downgrade_roles') LOOP
    IF r NOT IN ('ops_lead', 'field_operator', 'engineer', 'engineering_lead', 'safety_reviewer', 'founder') THEN
      RETURN format('unknown downgrade role %s', r);
    END IF;
  END LOOP;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION execution_incident_policy_withdrawn(pid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT EXISTS (SELECT 1 FROM execution_incidentpolicywithdrawal WHERE policy_id = pid) $$;

-- Active = the highest version EVER fully approved; none if that one is withdrawn (no fallback).
CREATE OR REPLACE FUNCTION execution_active_incident_policy_id() RETURNS bigint
LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN execution_incident_policy_withdrawn(t.id) THEN NULL ELSE t.id END
  FROM (
    SELECT p.id FROM execution_incidentpolicy p
    JOIN execution_incidentpolicyapproval a ON a.policy_id = p.id AND a.slot IN ('engineering', 'safety')
    GROUP BY p.id, p.version
    HAVING count(DISTINCT a.slot) = 2
    ORDER BY p.version DESC
    LIMIT 1
  ) t
$$;

-- Position (1-based) of a level in a policy's severity scale; NULL if absent.
CREATE OR REPLACE FUNCTION execution_level_rank(pid bigint, lvl text) RETURNS int
LANGUAGE sql STABLE AS $$
  SELECT t.ord::int FROM execution_incidentpolicy p,
    jsonb_array_elements_text(p.rules -> 'severity_scale') WITH ORDINALITY t(v, ord)
  WHERE p.id = pid AND t.v = lvl
$$;

CREATE OR REPLACE FUNCTION execution_incident_policy_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE problem text;
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('incident policies cannot be deleted; withdraw them'); END IF;
  IF TG_OP = 'INSERT' THEN
    NEW.recorded_at := clock_timestamp();
    IF NEW.version <> coalesce((SELECT max(version) FROM execution_incidentpolicy), 0) + 1 THEN
      PERFORM fleet_fail('incident policy versions must be consecutive');
    END IF;
    IF NEW.lock_version <> 1 THEN PERFORM fleet_fail('a new incident policy starts at lock_version 1'); END IF;
    IF NOT has_active_role(NEW.recorded_by_id, 'engineering_lead') THEN
      PERFORM fleet_fail('creating an incident policy requires an engineering_lead');
    END IF;
    problem := execution_incident_rules_problem(NEW.rules);
    IF problem IS NOT NULL THEN PERFORM fleet_fail('invalid incident policy rules: ' || problem); END IF;
    RETURN NEW;
  END IF;
  IF pg_trigger_depth() < 2 THEN
    PERFORM fleet_fail('incident policies are immutable; approve or withdraw them instead');
  END IF;
  IF (NEW.version, NEW.rules, NEW.source, NEW.notes, NEW.recorded_by_id, NEW.recorded_at)
     IS DISTINCT FROM (OLD.version, OLD.rules, OLD.source, OLD.notes, OLD.recorded_by_id, OLD.recorded_at) THEN
    PERFORM fleet_fail('incident policies are immutable; approve or withdraw them instead');
  END IF;
  NEW.lock_version := OLD.lock_version + 1;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_incident_policy_guard BEFORE INSERT OR UPDATE OR DELETE ON execution_incidentpolicy
  FOR EACH ROW EXECUTE FUNCTION execution_incident_policy_guard();
CREATE TRIGGER execution_incidentpolicy_no_truncate BEFORE TRUNCATE ON execution_incidentpolicy
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();

CREATE OR REPLACE FUNCTION execution_incident_policy_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := clock_timestamp();
  UPDATE execution_incidentpolicy SET lock_version = lock_version WHERE id = NEW.policy_id;  -- lock + bump
  IF execution_incident_policy_withdrawn(NEW.policy_id) THEN
    PERFORM fleet_fail('a withdrawn incident policy cannot be approved');
  END IF;
  IF NOT has_active_role(NEW.approver_id, fleet_slot_role(NEW.slot)) THEN
    PERFORM fleet_fail(format('slot %s requires role %s', NEW.slot, fleet_slot_role(NEW.slot)));
  END IF;
  IF NEW.approver_id = (SELECT recorded_by_id FROM execution_incidentpolicy WHERE id = NEW.policy_id) THEN
    PERFORM fleet_fail('the author of an incident policy cannot approve it');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_incident_policy_approval_guard BEFORE INSERT ON execution_incidentpolicyapproval
  FOR EACH ROW EXECUTE FUNCTION execution_incident_policy_approval_guard();

CREATE OR REPLACE FUNCTION execution_incident_policy_withdrawal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := clock_timestamp();
  UPDATE execution_incidentpolicy SET lock_version = lock_version WHERE id = NEW.policy_id;
  IF NOT execution_nonblank(NEW.reason) THEN PERFORM fleet_fail('a withdrawal needs a reason'); END IF;
  IF NOT execution_has_any_role(NEW.withdrawn_by_id, ARRAY['engineering_lead', 'safety_reviewer']) THEN
    PERFORM fleet_fail('withdrawing an incident policy requires engineering_lead or safety_reviewer');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_incident_policy_withdrawal_guard BEFORE INSERT ON execution_incidentpolicywithdrawal
  FOR EACH ROW EXECUTE FUNCTION execution_incident_policy_withdrawal_guard();

-- ================================================================ execution run (departure trigger only)
CREATE OR REPLACE FUNCTION execution_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d readiness_departureevent; a readiness_departureauthorization;
BEGIN
  NEW.recorded_at := clock_timestamp();
  IF pg_trigger_depth() < 2 THEN
    PERFORM fleet_fail('execution runs are opened only by recording a departure');
  END IF;
  SELECT * INTO d FROM readiness_departureevent WHERE id = NEW.departure_id;
  SELECT * INTO a FROM readiness_departureauthorization WHERE id = d.authorization_id;
  IF (NEW.visit_id, NEW.revision_id, NEW.robot_id, NEW.manifest_id, NEW.recorded_by_id)
     IS DISTINCT FROM (d.visit_id, a.revision_id, d.robot_id, a.manifest_id, d.recorded_by_id) THEN
    PERFORM fleet_fail('an execution run must describe its departure exactly');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM planning_vehicletrip t WHERE t.id = NEW.trip_id AND t.revision_id = a.revision_id) THEN
    PERFORM fleet_fail('an execution run must use the departed revision''s vehicle trip');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_run_guard BEFORE INSERT ON execution_executionrun
  FOR EACH ROW EXECUTE FUNCTION execution_run_guard();

CREATE OR REPLACE FUNCTION execution_open_run_on_departure() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE a readiness_departureauthorization; r planning_visitrevision; t planning_vehicletrip; rid bigint;
BEGIN
  SELECT * INTO a FROM readiness_departureauthorization WHERE id = NEW.authorization_id;
  SELECT * INTO r FROM planning_visitrevision WHERE id = a.revision_id;
  PERFORM 1 FROM fleet_robot WHERE id = a.robot_id FOR UPDATE;               -- held already (C2 / C3 trigger)
  SELECT * INTO t FROM planning_vehicletrip WHERE revision_id = a.revision_id FOR UPDATE;  -- held already
  IF t.id IS NULL THEN PERFORM fleet_fail('departure refused: the visit has no vehicle trip'); END IF;
  IF t.released_at IS NOT NULL THEN PERFORM fleet_fail('departure refused: the vehicle trip was released'); END IF;
  IF NOT EXISTS (SELECT 1 FROM planning_tripcargo WHERE trip_id = t.id AND robot_id = a.robot_id) THEN
    PERFORM fleet_fail('departure refused: the vehicle trip does not carry the robot');
  END IF;
  IF r.lead_id IS NULL THEN PERFORM fleet_fail('departure refused: the revision has no field lead'); END IF;
  INSERT INTO execution_executionrun (code, departure_id, visit_id, revision_id, robot_id, manifest_id, trip_id,
                                      recorded_by_id, recorded_at)
  VALUES (execution_code('EX', nextval('execution_run_code_seq')), NEW.id, a.visit_id, a.revision_id, a.robot_id,
          a.manifest_id, t.id, NEW.recorded_by_id, clock_timestamp())
  RETURNING id INTO rid;
  INSERT INTO execution_custodyevent (robot_id, seq, from_kind, to_kind, run_id, trip_id, visit_id, custodian_id,
                                      received_by_id, basis, incident_id, note, recorded_by_id, recorded_at)
  VALUES (a.robot_id, coalesce((SELECT max(seq) FROM execution_custodyevent WHERE robot_id = a.robot_id), 0) + 1,
          'hub', 'vehicle', rid, t.id, a.visit_id, r.lead_id, NULL, 'administrative', NULL,
          'assigned at departure; crew verification required', NEW.recorded_by_id, clock_timestamp());
  RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION execution_open_run_on_departure() FROM PUBLIC;
CREATE TRIGGER execution_open_run AFTER INSERT ON readiness_departureevent
  FOR EACH ROW EXECUTE FUNCTION execution_open_run_on_departure();

-- ================================================================ custody events
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
    IF NEW.to_kind <> 'unaccounted' THEN
      PERFORM fleet_fail('only an unaccounted report may be recorded without a run');
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
    IF NEW.incident_id IS DISTINCT FROM prev.incident_id THEN
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
CREATE TRIGGER execution_custody_guard BEFORE INSERT ON execution_custodyevent
  FOR EACH ROW EXECUTE FUNCTION execution_custody_guard();

CREATE OR REPLACE FUNCTION execution_custody_verification_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE e execution_custodyevent;
BEGIN
  NEW.recorded_at := clock_timestamp();
  SELECT * INTO e FROM execution_custodyevent WHERE id = NEW.custody_event_id;
  PERFORM 1 FROM fleet_robot WHERE id = e.robot_id FOR UPDATE;
  IF e.basis <> 'administrative' THEN PERFORM fleet_fail('only an administrative assignment needs verification'); END IF;
  IF e.run_id IS NULL OR execution_run_closed(e.run_id) THEN PERFORM fleet_fail('the run is closed'); END IF;
  IF NOT execution_is_crew(e.run_id, NEW.verified_by_id) THEN
    PERFORM fleet_fail('the verifier must be field crew of the run');
  END IF;
  IF NOT execution_person_is_field_user(NEW.verified_by_id) THEN
    PERFORM fleet_fail('the verifier must hold an active ops_lead or field_operator role');
  END IF;
  IF NOT execution_is_field_user(NEW.recorded_by_id) THEN
    PERFORM fleet_fail('recording a custody verification requires an active ops_lead or field_operator role');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_custody_verification_guard BEFORE INSERT ON execution_custodyverification
  FOR EACH ROW EXECUTE FUNCTION execution_custody_verification_guard();

-- ================================================================ run children: shared prologue
-- Locks the run FOR UPDATE and returns it; refuses when it is closed (unless allow_closed).
CREATE OR REPLACE FUNCTION execution_lock_run(rid bigint, allow_closed boolean DEFAULT false)
RETURNS execution_executionrun LANGUAGE plpgsql AS $$
DECLARE x execution_executionrun;
BEGIN
  SELECT * INTO x FROM execution_executionrun WHERE id = rid FOR UPDATE;
  IF x.id IS NULL THEN PERFORM fleet_fail('unknown execution run'); END IF;
  IF NOT allow_closed AND execution_run_closed(rid) THEN
    PERFORM fleet_fail(format('run %s is closed', x.code));
  END IF;
  RETURN x;
END $$;

-- ================================================================ trip events
CREATE OR REPLACE FUNCTION execution_trip_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE x execution_executionrun; c record; e execution_custodyevent;
BEGIN
  NEW.recorded_at := clock_timestamp();
  x := execution_lock_run(NEW.run_id);
  IF NEW.trip_id IS DISTINCT FROM x.trip_id THEN PERFORM fleet_fail('trip event must be for the run''s trip'); END IF;
  IF NOT execution_is_field_user(NEW.recorded_by_id) THEN
    PERFORM fleet_fail('recording a trip event requires an active ops_lead or field_operator role');
  END IF;
  IF NEW.seq <> (SELECT count(*) FROM execution_tripevent WHERE trip_id = NEW.trip_id) + 1 THEN
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
CREATE TRIGGER execution_trip_event_guard BEFORE INSERT ON execution_tripevent
  FOR EACH ROW EXECUTE FUNCTION execution_trip_event_guard();

-- ================================================================ site checks
CREATE OR REPLACE FUNCTION execution_template_problem(items jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE it jsonb;
BEGIN
  IF jsonb_typeof(items) IS DISTINCT FROM 'array' OR jsonb_array_length(items) = 0 THEN
    RETURN 'items must be a non-empty list of {"id", "label"}';
  END IF;
  FOR it IN SELECT * FROM jsonb_array_elements(items) LOOP
    IF NOT readiness_keys_exactly(it, ARRAY['id', 'label']) OR jsonb_typeof(it -> 'id') <> 'string'
       OR jsonb_typeof(it -> 'label') <> 'string' OR NOT execution_nonblank(it ->> 'id')
       OR NOT execution_nonblank(it ->> 'label') THEN
      RETURN 'each item must be {"id": non-empty text, "label": non-empty text}';
    END IF;
  END LOOP;
  IF (SELECT count(DISTINCT x ->> 'id') FROM jsonb_array_elements(items) x) <> jsonb_array_length(items) THEN
    RETURN 'item ids must be distinct';
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION execution_template_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE problem text;
BEGIN
  NEW.recorded_at := clock_timestamp();
  problem := execution_template_problem(NEW.items);
  IF problem IS NOT NULL THEN PERFORM fleet_fail('invalid site check template: ' || problem); END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_template_guard BEFORE INSERT ON execution_sitechecktemplate
  FOR EACH ROW EXECUTE FUNCTION execution_template_guard();

-- NULL when a site check of run `rid` may be 'go'; otherwise the first unmet condition.
CREATE OR REPLACE FUNCTION execution_site_check_no_go(rid bigint, tpl bigint, conditions jsonb, adults int, consents int)
RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE x execution_executionrun; c record; e execution_custodyevent; t execution_sitechecktemplate; expected int;
BEGIN
  SELECT * INTO x FROM execution_executionrun WHERE id = rid;
  SELECT * INTO t FROM execution_sitechecktemplate WHERE id = tpl;
  IF execution_run_closed(rid) THEN RETURN 'the run is closed'; END IF;
  IF NOT execution_trip_has(x.trip_id, 'arrived_site') THEN RETURN 'the trip has not arrived at the site'; END IF;
  SELECT * INTO c FROM execution_current_custody(x.robot_id);
  SELECT * INTO e FROM execution_custodyevent WHERE id = c.custody_event_id;
  IF c.kind IS DISTINCT FROM 'site' OR e.visit_id IS DISTINCT FROM x.visit_id THEN
    RETURN 'the robot is not in site custody for this visit';
  END IF;
  IF NOT c.verified THEN RETURN 'site custody is not verified'; END IF;
  IF EXISTS (SELECT 1 FROM jsonb_each_text(conditions) k WHERE k.value <> 'ok') THEN
    RETURN 'a condition is not ok';
  END IF;
  expected := (SELECT s.expected_adults FROM planning_visit v JOIN planning_site s ON s.id = v.site_id
               WHERE v.id = x.visit_id);
  IF expected IS NULL THEN RETURN 'the expected number of adults at the site is unknown'; END IF;
  IF adults > consents THEN RETURN format('%s adults present, %s valid consents', adults, consents); END IF;
  IF t.source <> 'approved_procedure' AND NOT execution_demo_mode() THEN
    RETURN 'a synthetic_demo template cannot produce go outside demo mode';
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION execution_site_check_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE x execution_executionrun; t execution_sitechecktemplate; n int; problem text; expected text;
BEGIN
  NEW.recorded_at := clock_timestamp();
  x := execution_lock_run(NEW.run_id);
  SELECT * INTO t FROM execution_sitechecktemplate WHERE id = NEW.template_id;
  IF NOT execution_is_field_user(NEW.recorded_by_id) THEN
    PERFORM fleet_fail('recording a site check requires an active ops_lead or field_operator role');
  END IF;
  IF NOT execution_is_crew(x.id, NEW.verifier_id) THEN
    PERFORM fleet_fail('the site check verifier must be field crew of the run');
  END IF;
  IF jsonb_typeof(NEW.conditions) IS DISTINCT FROM 'object'
     OR NOT readiness_keys_exactly(NEW.conditions, ARRAY(SELECT i ->> 'id' FROM jsonb_array_elements(t.items) i)) THEN
    PERFORM fleet_fail('conditions must answer exactly the template''s items');
  END IF;
  IF EXISTS (SELECT 1 FROM jsonb_each(NEW.conditions) k
             WHERE jsonb_typeof(k.value) <> 'string' OR k.value #>> '{}' NOT IN ('ok', 'not_ok')) THEN
    PERFORM fleet_fail('each condition must be "ok" or "not_ok"');
  END IF;
  n := execution_valid_consents(x.id);
  IF NEW.consent_count IS DISTINCT FROM n THEN
    PERFORM fleet_fail(format('consent_count must be %s (valid consents covering the visit)', n));
  END IF;
  problem := execution_site_check_no_go(x.id, t.id, NEW.conditions, NEW.adults_present, n);
  expected := CASE WHEN problem IS NULL THEN 'go' ELSE 'no_go' END;
  IF NEW.result IS DISTINCT FROM expected THEN
    PERFORM fleet_fail(format('site check result must be %s%s', expected, coalesce(': ' || problem, '')));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_site_check_guard BEFORE INSERT ON execution_sitecheck
  FOR EACH ROW EXECUTE FUNCTION execution_site_check_guard();

-- ================================================================ interruptions
CREATE OR REPLACE FUNCTION execution_interruption_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE x execution_executionrun;
BEGIN
  NEW.recorded_at := clock_timestamp();
  x := execution_lock_run(NEW.run_id);
  IF NOT execution_nonblank(NEW.reason) THEN PERFORM fleet_fail('an interruption needs a reason'); END IF;
  IF NEW.source = 'incident' THEN
    IF NOT EXISTS (SELECT 1 FROM execution_incident i WHERE i.id = NEW.incident_id AND i.run_id = x.id) THEN
      PERFORM fleet_fail('an incident interruption must reference an incident of this run');
    END IF;
  ELSIF NOT execution_is_field_user(NEW.recorded_by_id) THEN
    PERFORM fleet_fail('opening an interruption requires an active ops_lead or field_operator role');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_interruption_guard BEFORE INSERT ON execution_interruption
  FOR EACH ROW EXECUTE FUNCTION execution_interruption_guard();

CREATE OR REPLACE FUNCTION execution_interruption_resolution_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE i execution_interruption;
BEGIN
  NEW.recorded_at := clock_timestamp();
  SELECT * INTO i FROM execution_interruption WHERE id = NEW.interruption_id;
  PERFORM execution_lock_run(i.run_id, true);
  IF NOT execution_nonblank(NEW.note) THEN PERFORM fleet_fail('a resolution needs a note'); END IF;
  IF NOT execution_has_any_role(NEW.recorded_by_id, ARRAY['ops_lead', 'engineering_lead', 'safety_reviewer']) THEN
    PERFORM fleet_fail('resolving an interruption requires ops_lead, engineering_lead or safety_reviewer');
  END IF;
  IF i.incident_id IS NOT NULL AND EXISTS (
       SELECT 1 FROM execution_incident inc JOIN fleet_hold h ON h.id = inc.hold_id
       WHERE inc.id = i.incident_id AND h.released_at IS NULL) THEN
    PERFORM fleet_fail('the interruption''s incident hold is not released');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_interruption_resolution_guard BEFORE INSERT ON execution_interruptionresolution
  FOR EACH ROW EXECUTE FUNCTION execution_interruption_resolution_guard();

-- ================================================================ session events (section 3)
CREATE OR REPLACE FUNCTION execution_session_state(rid bigint) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE (SELECT kind FROM execution_sessionevent WHERE run_id = rid AND kind <> 'observation'
               ORDER BY seq DESC LIMIT 1)
    WHEN 'start' THEN 'active' WHEN 'resume' THEN 'active' WHEN 'pause' THEN 'paused' WHEN 'stop' THEN 'stopped'
    ELSE 'not_started' END
$$;

CREATE OR REPLACE FUNCTION execution_session_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE x execution_executionrun; st text; sc execution_sitecheck; c record; e execution_custodyevent;
        inter execution_interruption;
BEGIN
  NEW.recorded_at := clock_timestamp();
  x := execution_lock_run(NEW.run_id);
  IF NEW.seq <> coalesce((SELECT max(seq) FROM execution_sessionevent WHERE run_id = x.id), 0) + 1 THEN
    PERFORM fleet_fail('session event sequence must continue from the previous event');
  END IF;
  st := execution_session_state(x.id);
  IF NEW.kind = 'start' AND st <> 'not_started' THEN PERFORM fleet_fail(format('cannot start: session is %s', st));
  ELSIF NEW.kind = 'pause' AND st <> 'active' THEN PERFORM fleet_fail(format('cannot pause: session is %s', st));
  ELSIF NEW.kind = 'resume' AND st <> 'paused' THEN PERFORM fleet_fail(format('cannot resume: session is %s', st));
  ELSIF NEW.kind = 'stop' AND st NOT IN ('active', 'paused') THEN PERFORM fleet_fail(format('cannot stop: session is %s', st));
  END IF;

  -- recorder
  IF NEW.kind IN ('start', 'resume') THEN
    IF NOT (has_active_role(NEW.recorded_by_id, 'ops_lead')
            OR (execution_is_field_user(NEW.recorded_by_id) AND execution_user_is_crew(x.id, NEW.recorded_by_id))) THEN
      PERFORM fleet_fail('start/resume must be recorded by field crew of the run holding a field role, or an ops_lead');
    END IF;
  ELSIF NEW.kind = 'pause' AND NEW.interruption_id IS NOT NULL THEN
    SELECT * INTO inter FROM execution_interruption WHERE id = NEW.interruption_id;
    IF inter.run_id IS DISTINCT FROM x.id THEN PERFORM fleet_fail('the interruption belongs to another run'); END IF;
    IF inter.source = 'operator' AND NOT execution_is_field_user(NEW.recorded_by_id) THEN
      PERFORM fleet_fail('recording a session event requires an active ops_lead or field_operator role');
    END IF;
  ELSIF NOT execution_is_field_user(NEW.recorded_by_id) THEN
    PERFORM fleet_fail('recording a session event requires an active ops_lead or field_operator role');
  END IF;
  IF NEW.kind = 'observation' AND NOT execution_nonblank(NEW.note) THEN
    PERFORM fleet_fail('an observation needs a note');
  END IF;
  IF NEW.kind NOT IN ('start', 'resume') THEN RETURN NEW; END IF;

  -- gates for start / resume
  SELECT * INTO sc FROM execution_sitecheck WHERE id = NEW.site_check_id;
  IF sc.run_id IS DISTINCT FROM x.id THEN PERFORM fleet_fail('the site check belongs to another run'); END IF;
  IF sc.id <> (SELECT id FROM execution_sitecheck WHERE run_id = x.id ORDER BY recorded_at DESC, id DESC LIMIT 1) THEN
    PERFORM fleet_fail('stale site check: it is not the run''s latest');
  END IF;
  IF sc.result <> 'go' THEN PERFORM fleet_fail('the latest site check is no_go'); END IF;
  IF NEW.kind = 'resume' AND (
       sc.recorded_at <= (SELECT max(recorded_at) FROM execution_sessionevent WHERE run_id = x.id AND kind = 'pause')
       OR sc.recorded_at <= coalesce((SELECT max(recorded_at) FROM execution_interruption WHERE run_id = x.id), '-infinity')) THEN
    PERFORM fleet_fail('stale site check: resume needs a go check newer than the pause and every interruption');
  END IF;
  IF execution_open_interruption(x.id) THEN PERFORM fleet_fail('the run has an open interruption'); END IF;
  IF execution_robot_held(x.robot_id) THEN
    PERFORM fleet_fail('the robot or its configuration pattern has an unreleased blocking hold');
  END IF;
  SELECT * INTO c FROM execution_current_custody(x.robot_id);
  SELECT * INTO e FROM execution_custodyevent WHERE id = c.custody_event_id;
  IF c.kind IS DISTINCT FROM 'site' OR e.visit_id IS DISTINCT FROM x.visit_id OR NOT c.verified THEN
    PERFORM fleet_fail('the robot is not in verified site custody for this visit');
  END IF;
  IF execution_valid_consents(x.id) < sc.adults_present THEN
    PERFORM fleet_fail(format('only %s valid consents for %s adults present', execution_valid_consents(x.id),
                              sc.adults_present));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_session_guard BEFORE INSERT ON execution_sessionevent
  FOR EACH ROW EXECUTE FUNCTION execution_session_guard();

-- ================================================================ incidents
ALTER TABLE execution_incident ADD COLUMN IF NOT EXISTS created_txid xid8 NOT NULL DEFAULT pg_current_xact_id();

CREATE UNIQUE INDEX IF NOT EXISTS uniq_incident_hold_per_incident
  ON fleet_hold (source_ref) WHERE source_type = 'incident' AND source_ref <> '';

CREATE OR REPLACE FUNCTION execution_incident_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rb fleet_robot; open_run bigint;
BEGIN
  NEW.recorded_at := clock_timestamp();
  NEW.reported_at := NEW.recorded_at;
  NEW.created_txid := pg_current_xact_id();
  SELECT * INTO rb FROM fleet_robot WHERE id = NEW.robot_id FOR UPDATE;
  IF NOT execution_has_any_role(NEW.recorded_by_id, ARRAY['ops_lead', 'field_operator', 'engineer',
                                'engineering_lead', 'safety_reviewer', 'founder']) THEN
    PERFORM fleet_fail('reporting an incident requires an operational role');
  END IF;
  IF NOT execution_nonblank(NEW.description) THEN PERFORM fleet_fail('an incident needs a description'); END IF;
  IF NEW.occurred_at > NEW.reported_at THEN PERFORM fleet_fail('occurred_at cannot be after reported_at'); END IF;
  IF NEW.manifest_id IS DISTINCT FROM rb.current_manifest_id THEN
    PERFORM fleet_fail('incident manifest must be the robot''s current configuration');
  END IF;
  open_run := execution_open_run_id(NEW.robot_id);
  IF NEW.run_id IS DISTINCT FROM open_run THEN
    PERFORM fleet_fail('incident run must be the robot''s open run (none when it has no open run)');
  END IF;
  IF NEW.visit_id IS DISTINCT FROM (SELECT visit_id FROM execution_executionrun WHERE id = open_run) THEN
    PERFORM fleet_fail('incident visit must be the run''s visit');
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM fleet_hold h
    WHERE h.id = NEW.hold_id AND h.scope = 'robot' AND h.robot_id = NEW.robot_id AND h.blocking
      AND h.released_at IS NULL AND h.source_type = 'incident' AND h.source_ref = NEW.code
      AND h.created_txid = pg_current_xact_id()
  ) THEN
    PERFORM fleet_fail('every incident must place its own blocking robot hold (source incident, source_ref = '
                       'incident code) in the same transaction');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_incident_guard BEFORE INSERT ON execution_incident
  FOR EACH ROW EXECUTE FUNCTION execution_incident_guard();

CREATE OR REPLACE FUNCTION execution_lock_incident(iid bigint) RETURNS execution_incident
LANGUAGE plpgsql AS $$
DECLARE i execution_incident;
BEGIN
  SELECT * INTO i FROM execution_incident WHERE id = iid FOR UPDATE;
  IF i.id IS NULL THEN PERFORM fleet_fail('unknown incident'); END IF;
  RETURN i;
END $$;

CREATE OR REPLACE FUNCTION execution_incident_status(iid bigint) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT coalesce((SELECT to_status FROM execution_incidentstatuschange WHERE incident_id = iid
                   ORDER BY recorded_at DESC, id DESC LIMIT 1), 'reported')
$$;

CREATE OR REPLACE FUNCTION execution_fault_observation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := clock_timestamp();
  PERFORM execution_lock_incident(NEW.incident_id);
  IF NOT execution_nonblank(NEW.component) THEN PERFORM fleet_fail('a fault observation names a component'); END IF;
  IF NOT execution_has_any_role(NEW.recorded_by_id, ARRAY['ops_lead', 'engineering_lead', 'safety_reviewer']) THEN
    PERFORM fleet_fail('adding a fault observation requires ops_lead, engineering_lead or safety_reviewer');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_fault_observation_guard BEFORE INSERT ON execution_faultobservation
  FOR EACH ROW EXECUTE FUNCTION execution_fault_observation_guard();

CREATE OR REPLACE FUNCTION execution_severity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE i execution_incident; p execution_incidentpolicy; active bigint; prev execution_severitydecision;
        new_rank int; prev_rank int;
BEGIN
  NEW.recorded_at := clock_timestamp();
  -- incident policies (this one and every newer one) S before the incident U: global order.
  PERFORM 1 FROM execution_incidentpolicy WHERE id >= NEW.policy_id ORDER BY id FOR SHARE;
  i := execution_lock_incident(NEW.incident_id);
  IF NOT execution_has_any_role(NEW.recorded_by_id, ARRAY['engineering_lead', 'safety_reviewer']) THEN
    PERFORM fleet_fail('deciding severity requires engineering_lead or safety_reviewer');
  END IF;
  IF NOT execution_nonblank(NEW.reason) THEN PERFORM fleet_fail('a severity decision needs a reason'); END IF;
  active := execution_active_incident_policy_id();
  IF active IS NULL THEN
    PERFORM fleet_fail('there is no active incident policy; severity stays unclassified');
  END IF;
  IF NEW.policy_id IS DISTINCT FROM active THEN PERFORM fleet_fail('severity must use the active incident policy'); END IF;
  SELECT * INTO p FROM execution_incidentpolicy WHERE id = active;
  IF p.source <> 'approved_procedure' AND NOT execution_demo_mode() THEN
    PERFORM fleet_fail('a synthetic_demo incident policy cannot classify severity outside demo mode');
  END IF;
  new_rank := execution_level_rank(p.id, NEW.level);
  IF new_rank IS NULL THEN PERFORM fleet_fail(format('level %s is not in the policy''s severity scale', NEW.level)); END IF;
  SELECT * INTO prev FROM execution_severitydecision WHERE incident_id = i.id ORDER BY recorded_at DESC, id DESC LIMIT 1;
  IF prev.id IS NOT NULL THEN
    prev_rank := execution_level_rank(p.id, prev.level);
    IF prev_rank IS NULL OR new_rank < prev_rank THEN
      IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(p.rules -> 'downgrade_roles') r
                     WHERE has_active_role(NEW.recorded_by_id, r)) THEN
        PERFORM fleet_fail('a downgrade needs a role listed in the policy''s downgrade_roles');
      END IF;
      IF NEW.recorded_by_id = i.recorded_by_id THEN
        PERFORM fleet_fail('the reporter of an incident cannot downgrade its severity');
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_severity_guard BEFORE INSERT ON execution_severitydecision
  FOR EACH ROW EXECUTE FUNCTION execution_severity_guard();

CREATE OR REPLACE FUNCTION execution_status_change_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE i execution_incident; cur text; steps text[] := ARRAY['reported', 'triaged', 'response_in_progress',
        'resolved', 'closed']; sev execution_severitydecision; p execution_incidentpolicy;
BEGIN
  NEW.recorded_at := clock_timestamp();
  i := execution_lock_incident(NEW.incident_id);
  cur := execution_incident_status(i.id);
  IF array_position(steps, NEW.to_status) IS DISTINCT FROM array_position(steps, cur) + 1 THEN
    PERFORM fleet_fail(format('incident status goes one step at a time: %s cannot become %s', cur, NEW.to_status));
  END IF;
  IF NEW.to_status = 'closed' THEN
    IF NOT execution_has_any_role(NEW.recorded_by_id, ARRAY['engineering_lead', 'safety_reviewer']) THEN
      PERFORM fleet_fail('closing an incident requires engineering_lead or safety_reviewer');
    END IF;
    IF EXISTS (SELECT 1 FROM execution_incidentescalation x WHERE x.incident_id = i.id
               AND NOT EXISTS (SELECT 1 FROM execution_escalationack a WHERE a.escalation_id = x.id)) THEN
      PERFORM fleet_fail('every escalation must be acknowledged before closing');
    END IF;
    SELECT * INTO sev FROM execution_severitydecision WHERE incident_id = i.id ORDER BY recorded_at DESC, id DESC LIMIT 1;
    IF sev.id IS NOT NULL THEN
      SELECT * INTO p FROM execution_incidentpolicy WHERE id = sev.policy_id;
      IF execution_level_rank(p.id, sev.level) >= execution_level_rank(p.id, p.rules ->> 'escalation_required_from')
         AND NOT EXISTS (SELECT 1 FROM execution_incidentescalation WHERE incident_id = i.id) THEN
        PERFORM fleet_fail(format('severity %s requires at least one escalation before closing', sev.level));
      END IF;
    END IF;
  ELSIF NOT execution_has_any_role(NEW.recorded_by_id, ARRAY['ops_lead', 'engineering_lead', 'safety_reviewer']) THEN
    PERFORM fleet_fail('changing incident status requires ops_lead, engineering_lead or safety_reviewer');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_status_change_guard BEFORE INSERT ON execution_incidentstatuschange
  FOR EACH ROW EXECUTE FUNCTION execution_status_change_guard();

CREATE OR REPLACE FUNCTION execution_escalation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE i execution_incident;
BEGIN
  NEW.recorded_at := clock_timestamp();
  i := execution_lock_incident(NEW.incident_id);
  IF execution_incident_status(i.id) = 'closed' THEN PERFORM fleet_fail('the incident is closed'); END IF;
  IF NOT execution_has_any_role(NEW.recorded_by_id, ARRAY['ops_lead', 'engineering_lead', 'safety_reviewer']) THEN
    PERFORM fleet_fail('escalating an incident requires ops_lead, engineering_lead or safety_reviewer');
  END IF;
  IF NEW.to_role NOT IN ('ops_lead', 'field_operator', 'engineer', 'engineering_lead', 'safety_reviewer', 'founder') THEN
    PERFORM fleet_fail(format('unknown escalation role %s', NEW.to_role));
  END IF;
  IF NOT execution_nonblank(NEW.requested_action) THEN PERFORM fleet_fail('an escalation needs a requested action'); END IF;
  IF NEW.to_person_id IS NOT NULL AND NOT has_active_role(
       (SELECT user_id FROM planning_person WHERE id = NEW.to_person_id), NEW.to_role) THEN
    PERFORM fleet_fail('the escalation target person does not hold the target role');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_escalation_guard BEFORE INSERT ON execution_incidentescalation
  FOR EACH ROW EXECUTE FUNCTION execution_escalation_guard();

CREATE OR REPLACE FUNCTION execution_escalation_ack_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE x execution_incidentescalation;
BEGIN
  NEW.recorded_at := clock_timestamp();
  SELECT * INTO x FROM execution_incidentescalation WHERE id = NEW.escalation_id;
  PERFORM execution_lock_incident(x.incident_id);
  IF NOT has_active_role(NEW.acknowledged_by_id, x.to_role) THEN
    PERFORM fleet_fail(format('the acknowledger must hold role %s', x.to_role));
  END IF;
  IF x.to_person_id IS NOT NULL AND NEW.acknowledged_by_id IS DISTINCT FROM
     (SELECT user_id FROM planning_person WHERE id = x.to_person_id) THEN
    PERFORM fleet_fail('only the named person can acknowledge this escalation');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_escalation_ack_guard BEFORE INSERT ON execution_escalationack
  FOR EACH ROW EXECUTE FUNCTION execution_escalation_ack_guard();

-- ================================================================ recovery dispositions
CREATE OR REPLACE FUNCTION execution_disposition_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE x execution_executionrun; inc execution_incident;
BEGIN
  NEW.recorded_at := clock_timestamp();
  x := execution_lock_run(NEW.run_id);
  IF NOT has_active_role(NEW.recorded_by_id, 'ops_lead') THEN
    PERFORM fleet_fail('recording a disposition requires an ops_lead');
  END IF;
  IF NOT execution_nonblank(NEW.reason) THEN PERFORM fleet_fail('a disposition needs a reason'); END IF;
  IF NEW.incident_id IS NOT NULL THEN
    SELECT * INTO inc FROM execution_incident WHERE id = NEW.incident_id;
    IF inc.robot_id IS DISTINCT FROM x.robot_id OR inc.run_id IS DISTINCT FROM x.id THEN
      PERFORM fleet_fail('the disposition incident must be an incident of this run');
    END IF;
    IF NEW.decision = 'robot_unaccounted' AND inc.event_type <> 'robot_unaccounted' THEN
      PERFORM fleet_fail('robot_unaccounted needs a robot_unaccounted incident');
    END IF;
  END IF;
  IF NEW.decision = 'continue_visit' THEN
    IF execution_robot_held(x.robot_id) THEN
      PERFORM fleet_fail('continue_visit refused: the robot or its configuration pattern has an unreleased blocking hold');
    END IF;
    IF execution_open_interruption(x.id) THEN
      PERFORM fleet_fail('continue_visit refused: the run has an open interruption');
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_disposition_guard BEFORE INSERT ON execution_recoverydisposition
  FOR EACH ROW EXECUTE FUNCTION execution_disposition_guard();

-- ================================================================ return reconciliation
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
  IF hub.basis <> 'handoff' OR NOT execution_person_is_field_user(hub.received_by_id) THEN
    PERFORM fleet_fail('cannot reconcile: the hub receipt must be a handoff received by a field user');
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
CREATE TRIGGER execution_reconciliation_guard BEFORE INSERT ON execution_returnreconciliation
  FOR EACH ROW EXECUTE FUNCTION execution_reconciliation_guard();

-- ================================================================ trip closure
CREATE OR REPLACE FUNCTION execution_closure_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE x execution_executionrun; rec execution_returnreconciliation;
BEGIN
  NEW.recorded_at := clock_timestamp();
  x := execution_lock_run(NEW.run_id);
  IF NOT has_active_role(NEW.recorded_by_id, 'ops_lead') THEN
    PERFORM fleet_fail('closing a trip requires an ops_lead');
  END IF;
  IF NEW.trip_id IS DISTINCT FROM x.trip_id THEN PERFORM fleet_fail('closure must be for the run''s trip'); END IF;
  IF NOT execution_trip_has(x.trip_id, 'arrived_hub') THEN
    PERFORM fleet_fail('cannot close: the trip has not arrived at the hub');
  END IF;
  SELECT * INTO rec FROM execution_returnreconciliation WHERE run_id = x.id ORDER BY recorded_at DESC, id DESC LIMIT 1;
  IF rec.id IS NULL OR rec.id IS DISTINCT FROM NEW.reconciliation_id THEN
    PERFORM fleet_fail('cannot close: closure must reference the run''s latest reconciliation');
  END IF;
  IF rec.outcome <> 'reconciled' THEN
    PERFORM fleet_fail('cannot close: the latest reconciliation is unresolved');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_closure_guard BEFORE INSERT ON execution_tripclosure
  FOR EACH ROW EXECUTE FUNCTION execution_closure_guard();

-- ================================================================ C1: a departed visit has happened
CREATE OR REPLACE FUNCTION readiness_departed_visit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.current_revision_id IS DISTINCT FROM OLD.current_revision_id OR NEW.status IS DISTINCT FROM OLD.status)
     AND EXISTS (SELECT 1 FROM readiness_departureevent WHERE visit_id = NEW.id) THEN
    PERFORM fleet_fail('the visit has departed; it can never be cancelled, re-planned or re-opened');
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION execution_departed_release_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.released_at IS NULL AND NEW.released_at IS NOT NULL AND EXISTS (
       SELECT 1 FROM planning_visitrevision r JOIN readiness_departureevent d ON d.visit_id = r.visit_id
       WHERE r.id = NEW.revision_id) THEN
    PERFORM fleet_fail(format('the visit has departed; its %s can never be released',
                              CASE TG_TABLE_NAME WHEN 'planning_reservation' THEN 'reservations' ELSE 'vehicle trip' END));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_departed_reservation_guard BEFORE UPDATE ON planning_reservation
  FOR EACH ROW EXECUTE FUNCTION execution_departed_release_guard();
CREATE TRIGGER execution_departed_trip_guard BEFORE UPDATE ON planning_vehicletrip
  FOR EACH ROW EXECUTE FUNCTION execution_departed_release_guard();

-- ================================================================ C3: the robot must be at hub, no open run
CREATE OR REPLACE FUNCTION execution_authorization_hub_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE problem text;
BEGIN
  problem := execution_robot_departure_problem(NEW.robot_id);
  IF problem IS NOT NULL THEN PERFORM fleet_fail('authorization refused: ' || problem); END IF;
  RETURN NEW;
END $$;
-- Name sorts after readiness_authorization_*: runs after their locks and checks.
CREATE TRIGGER readiness_c3_authorization_hub BEFORE INSERT ON readiness_departureauthorization
  FOR EACH ROW EXECUTE FUNCTION execution_authorization_hub_guard();

CREATE OR REPLACE FUNCTION execution_departure_hub_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a readiness_departureauthorization; problem text;
BEGIN
  IF EXISTS (SELECT 1 FROM readiness_departureevent WHERE visit_id = NEW.visit_id) THEN
    RETURN NEW;  -- a second departure of the visit: the unique index refuses it
  END IF;
  SELECT * INTO a FROM readiness_departureauthorization WHERE id = NEW.authorization_id;
  -- record_departure's lock order: site S -> pattern S -> robot U -> persons S -> vehicle S -> visit S -> trip U
  PERFORM 1 FROM planning_site WHERE id = (SELECT site_id FROM planning_visit WHERE id = a.visit_id) FOR SHARE;
  PERFORM 1 FROM fleet_manifestpattern
    WHERE id = (SELECT pattern_id FROM fleet_configurationmanifest WHERE id = a.manifest_id) FOR SHARE;
  PERFORM 1 FROM fleet_robot WHERE id = a.robot_id FOR UPDATE;
  PERFORM readiness_lock_snapshot_guards(a.guard_snapshot, a.visit_id, a.manifest_id, a.robot_id);
  PERFORM 1 FROM planning_visit WHERE id = a.visit_id FOR SHARE;
  PERFORM 1 FROM planning_vehicletrip WHERE revision_id = a.revision_id FOR UPDATE;
  problem := execution_robot_departure_problem(a.robot_id);
  IF problem IS NOT NULL THEN PERFORM fleet_fail('departure refused: ' || problem); END IF;
  RETURN NEW;
END $$;
-- Name sorts before readiness_departure_guard: takes the robot U / trip U positions first.
CREATE TRIGGER readiness_c3_departure_hub BEFORE INSERT ON readiness_departureevent
  FOR EACH ROW EXECUTE FUNCTION execution_departure_hub_guard();

-- ================================================================ codes + privileges
CREATE SEQUENCE IF NOT EXISTS execution_run_code_seq;
CREATE SEQUENCE IF NOT EXISTS execution_incident_code_seq;
CREATE SEQUENCE IF NOT EXISTS execution_interruption_code_seq;
GRANT USAGE, SELECT ON SEQUENCE execution_run_code_seq, execution_incident_code_seq,
  execution_interruption_code_seq TO {app};

REVOKE INSERT ON execution_executionrun FROM {app};
{privileges}
"""


def _sql() -> str:
    app = settings.DB_APP_ROLE
    parts = []
    for t in APPEND_ONLY:
        parts.append(
            f"CREATE TRIGGER {t}_no_update_delete BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION reject_mutation();\n"
            f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();"
        )
    privs = []
    for t in [*APPEND_ONLY, "execution_incidentpolicy"]:
        revoke = "DELETE, TRUNCATE" if t in LOCKABLE else "UPDATE, DELETE, TRUNCATE"
        privs.append(f"GRANT SELECT ON {t} TO {app};\nREVOKE {revoke} ON {t} FROM {app};")
    return (
        SQL.replace("{append_only}", "\n".join(parts))
        .replace("{privileges}", "\n".join(privs))
        .replace("{app}", app)
    )


class Migration(migrations.Migration):
    dependencies = [("execution", "0001_initial")]
    operations = [migrations.RunSQL(_sql(), reverse_sql=migrations.RunSQL.noop)]

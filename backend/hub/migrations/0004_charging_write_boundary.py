"""v2 02B charging: conflict constraints, the trusted write boundary and privileges (contract 11, 8.3, 8.4, 8.8).

CONSTRAINTS (btree_gist, as planning_reservation):
  * hub_charger_reservation: no two ACTIVE (released_at IS NULL) reservations overlap on one charger, nor on one
    robot.
  * hub_charging_session: no two sessions overlap on one charger, nor on one robot, with an open session as
    tstzrange(started_at, 'infinity'): at most one open session per charger and per robot.
  * robot_soc_reading is append-only (reject_mutation trigger of platform_core 0002).

CROSS-TABLE RULES (only inside the functions below, which are the only writers):
  a reservation or a session never overlaps incompatibly with
    - an active reservation of the same charger for another robot, or of the same robot on another charger;
    - a session on the same charger by another robot, or of the same robot on another charger (the same robot
      on the same charger is the reservation being used: compatible). An open session counts until its
      planned end, and at least until now; past that the session-vs-session constraint (open = forever) still
      stops a second robot from starting on that charger;
    - an active visit reservation of the robot in planning_reservation with a purpose other than 'charging'.

LOCKS, fixed order: the robot row FOR UPDATE (the per-robot planning lock: planning.plan_visit takes the same
row lock), then the charger row FOR UPDATE (the per-charger lock). Two conflicting attempts on the same robot
or charger are therefore serialized; the second re-reads after the first commits and is refused in plain
words. The exclusion constraints are the last line (23P01).

WRITE BOUNDARY. ops_app has SELECT only on hub_charger_reservation, hub_charging_session and robot_soc_reading
(and no sequence use); every write goes through these SECURITY DEFINER functions owned by ops_owner, with a
fixed search_path, EXECUTE revoked from PUBLIC and granted to ops_app:

    hub_reserve_charger(actor, charger, robot, starts_at, ends_at, visit, request_id) -> reservation id
    hub_release_charger_reservation(actor, reservation, reason, request_id)
    hub_start_charging(actor, charger, robot, start_soc_pct, planned_until, request_id) -> session id
    hub_end_charging(actor, session, end_soc_pct, request_id)  (an end % adds a charging_session_end reading)
    hub_record_soc_reading(actor, robot, soc_pct, observed_at, request_id) -> reading id (manual_reading)

Each checks the actor's ACTIVE role in accounts_rolegrant (has_active_role, as v1 guarded functions do; the
database cannot authenticate the human, it checks the named actor), the invariants, that the charger is in
service, and writes the audit row. A refused call raises (insufficient_privilege for a role refusal,
check_violation otherwise) and writes nothing.

hub_charger is configuration, like platform_core_hub: ops_app keeps SELECT, INSERT and UPDATE for the guarded
command hub.charging.create_charger / set_charger_in_service (run_command, audited), never DELETE. Changing a
charger cannot book anything: the functions re-check in_service under the charger's row lock.

Nothing here writes readiness evidence, an authorization, a hold, custody or eligibility (contract 8.1, 8.3).
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE

HELPERS = r"""
CREATE OR REPLACE FUNCTION hub_deny(msg text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '%', msg USING ERRCODE = 'insufficient_privilege'; END $$;

CREATE OR REPLACE FUNCTION hub_actor_has(uid bigint, roles text[]) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM auth_user u WHERE u.id = uid AND u.is_active)
     AND EXISTS (SELECT 1 FROM unnest(roles) r WHERE has_active_role(uid, r))
$$;

CREATE OR REPLACE FUNCTION hub_utc(t timestamptz) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT to_char(t AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC'
$$;

CREATE OR REPLACE FUNCTION hub_audit(p_actor bigint, p_cmd text, p_type text, p_id text, p_payload jsonb,
                                     p_result jsonb, p_rid text) RETURNS void
LANGUAGE sql AS $$
  INSERT INTO platform_core_auditevent (occurred_at, actor_id, command, outcome, entity_type, entity_id, payload,
                                        result, reason, request_id)
  VALUES (clock_timestamp(), p_actor, p_cmd, 'committed', p_type, p_id, p_payload, p_result, '',
          left(coalesce(p_rid, ''), 64))
$$;

-- The cross-table conflict check for a charger/robot over a period (open session = until infinity). Returns the
-- first conflict as a plain sentence, or NULL. p_res / p_session: the caller's own row to ignore.
CREATE OR REPLACE FUNCTION hub_charge_conflict(p_charger bigint, p_robot bigint, p_from timestamptz,
                                               p_to timestamptz, p_kind text) RETURNS text
LANGUAGE plpgsql STABLE SET search_path = public, pg_temp AS $$
DECLARE c record; span tstzrange := tstzrange(p_from, p_to, '[)');
BEGIN
  SELECT r.starts_at, r.ends_at, ch.code AS charger, rb.code AS robot, (r.charger_id = p_charger) AS same_charger
    INTO c
  FROM hub_charger_reservation r JOIN hub_charger ch ON ch.id = r.charger_id JOIN fleet_robot rb ON rb.id = r.robot_id
  WHERE r.released_at IS NULL AND tstzrange(r.starts_at, r.ends_at, '[)') && span
    AND ((r.charger_id = p_charger AND (p_kind = 'reservation' OR r.robot_id <> p_robot))
         OR (r.robot_id = p_robot AND (p_kind = 'reservation' OR r.charger_id <> p_charger)))
  ORDER BY r.starts_at LIMIT 1;
  IF FOUND THEN
    IF c.same_charger THEN
      RETURN format('Charger %s is already booked for %s from %s to %s', c.charger, c.robot, hub_utc(c.starts_at),
                    hub_utc(c.ends_at));
    END IF;
    RETURN format('Robot %s is already booked on charger %s from %s to %s', c.robot, c.charger,
                  hub_utc(c.starts_at), hub_utc(c.ends_at));
  END IF;
  SELECT s.started_at, s.ended_at, ch.code AS charger, rb.code AS robot, (s.charger_id = p_charger) AS same_charger
    INTO c
  FROM hub_charging_session s JOIN hub_charger ch ON ch.id = s.charger_id JOIN fleet_robot rb ON rb.id = s.robot_id
  WHERE tstzrange(s.started_at,
                  CASE WHEN s.ended_at IS NULL THEN greatest(s.planned_until, clock_timestamp()) ELSE s.ended_at END,
                  '[)') && span
    AND ((s.charger_id = p_charger AND (p_kind = 'session' OR s.robot_id <> p_robot))
         OR (s.robot_id = p_robot AND (p_kind = 'session' OR s.charger_id <> p_charger)))
  ORDER BY s.started_at LIMIT 1;
  IF FOUND THEN
    IF c.same_charger THEN
      RETURN format('Charger %s is in use: %s has been charging on it since %s', c.charger, c.robot,
                    hub_utc(c.started_at));
    END IF;
    RETURN format('Robot %s is charging on charger %s since %s', c.robot, c.charger, hub_utc(c.started_at));
  END IF;
  SELECT pr.starts_at, pr.ends_at, pr.purpose, v.code AS visit, rb.code AS robot INTO c
  FROM planning_reservation pr
  JOIN planning_resource res ON res.id = pr.resource_id AND res.kind = 'robot' AND res.robot_id = p_robot
  JOIN planning_visitrevision vr ON vr.id = pr.revision_id JOIN planning_visit v ON v.id = vr.visit_id
  JOIN fleet_robot rb ON rb.id = p_robot
  WHERE pr.released_at IS NULL AND pr.purpose <> 'charging' AND tstzrange(pr.starts_at, pr.ends_at, '[)') && span
  ORDER BY pr.starts_at LIMIT 1;
  IF FOUND THEN
    RETURN format('Robot %s is booked for visit %s (%s) from %s to %s', c.robot, c.visit,
                  replace(c.purpose, '_', ' '), hub_utc(c.starts_at), hub_utc(c.ends_at));
  END IF;
  RETURN NULL;
END $$;

-- Lock in the fixed order: robot row, then charger row. Returns the charger (checked in service).
CREATE OR REPLACE FUNCTION hub_lock_robot_charger(p_robot bigint, p_charger bigint) RETURNS hub_charger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE rb fleet_robot; ch hub_charger;
BEGIN
  SELECT * INTO rb FROM fleet_robot WHERE id = p_robot FOR UPDATE;
  IF NOT FOUND THEN PERFORM fleet_fail('No such robot'); END IF;
  IF rb.lifecycle = 'retired' THEN PERFORM fleet_fail(format('Robot %s is retired', rb.code)); END IF;
  SELECT * INTO ch FROM hub_charger WHERE id = p_charger FOR UPDATE;
  IF NOT FOUND THEN PERFORM fleet_fail('No such charger'); END IF;
  RETURN ch;
END $$;
"""

FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION hub_reserve_charger(p_actor bigint, p_charger bigint, p_robot bigint,
                                               p_starts timestamptz, p_ends timestamptz, p_visit bigint,
                                               p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE ch hub_charger; problem text; new_id bigint; now_ts timestamptz := clock_timestamp();
BEGIN
  IF NOT hub_actor_has(p_actor, ARRAY['hub_technician', 'ops_lead']) THEN
    PERFORM hub_deny('Only the hub technician or the ops lead can reserve a charger');
  END IF;
  IF p_starts IS NULL OR p_ends IS NULL OR p_ends <= p_starts THEN
    PERFORM fleet_fail('A reservation must end after it starts');
  END IF;
  IF p_ends <= now_ts THEN PERFORM fleet_fail('That period is already over'); END IF;
  IF p_ends - p_starts > interval '24 hours' THEN PERFORM fleet_fail('A reservation can last at most 24 hours'); END IF;
  ch := hub_lock_robot_charger(p_robot, p_charger);
  IF NOT ch.in_service THEN PERFORM fleet_fail(format('Charger %s is out of service', ch.code)); END IF;
  IF p_visit IS NOT NULL AND NOT EXISTS (SELECT 1 FROM planning_visit WHERE id = p_visit AND status <> 'cancelled') THEN
    PERFORM fleet_fail('That visit does not exist or is cancelled');
  END IF;
  problem := hub_charge_conflict(p_charger, p_robot, p_starts, p_ends, 'reservation');
  IF problem IS NOT NULL THEN PERFORM fleet_fail(problem); END IF;
  INSERT INTO hub_charger_reservation (charger_id, robot_id, visit_id, starts_at, ends_at, created_by_id,
                                       created_at, released_reason)
  VALUES (p_charger, p_robot, p_visit, p_starts, p_ends, p_actor, now_ts, '') RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'hub.reserve_charger', 'charger_reservation', new_id::text,
                    jsonb_build_object('charger', ch.code, 'robot_id', p_robot, 'starts_at', p_starts,
                                       'ends_at', p_ends, 'visit_id', p_visit),
                    jsonb_build_object('reservation_id', new_id), p_rid);
  RETURN new_id;
END $fn$;

CREATE OR REPLACE FUNCTION hub_release_charger_reservation(p_actor bigint, p_reservation bigint, p_reason text,
                                                           p_rid text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE r hub_charger_reservation; ch hub_charger;
BEGIN
  IF NOT hub_actor_has(p_actor, ARRAY['hub_technician', 'ops_lead']) THEN
    PERFORM hub_deny('Only the hub technician or the ops lead can release a charger reservation');
  END IF;
  IF coalesce(btrim(p_reason), '') = '' OR length(p_reason) > 200 THEN
    PERFORM fleet_fail('Give a short reason (up to 200 characters)');
  END IF;
  SELECT * INTO r FROM hub_charger_reservation WHERE id = p_reservation;
  IF NOT FOUND THEN PERFORM fleet_fail('No such reservation'); END IF;
  ch := hub_lock_robot_charger(r.robot_id, r.charger_id);
  SELECT * INTO r FROM hub_charger_reservation WHERE id = p_reservation FOR UPDATE;
  IF r.released_at IS NOT NULL THEN PERFORM fleet_fail('That reservation was already released'); END IF;
  UPDATE hub_charger_reservation SET released_at = clock_timestamp(), released_by_id = p_actor,
         released_reason = btrim(p_reason)
  WHERE id = p_reservation;
  PERFORM hub_audit(p_actor, 'hub.release_charger_reservation', 'charger_reservation', p_reservation::text,
                    jsonb_build_object('charger', ch.code, 'reason', btrim(p_reason)), '{}'::jsonb, p_rid);
END $fn$;

CREATE OR REPLACE FUNCTION hub_start_charging(p_actor bigint, p_charger bigint, p_robot bigint, p_start_soc int,
                                              p_until timestamptz, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE ch hub_charger; problem text; new_id bigint; res_id bigint; now_ts timestamptz := clock_timestamp();
BEGIN
  IF NOT hub_actor_has(p_actor, ARRAY['hub_technician', 'field_operator', 'ops_lead', 'engineer']) THEN
    PERFORM hub_deny('Only the hub technician, a field operator, the ops lead or an engineer can record charging');
  END IF;
  IF p_start_soc IS NOT NULL AND (p_start_soc < 0 OR p_start_soc > 100) THEN
    PERFORM fleet_fail('The battery level is a whole number from 0 to 100');
  END IF;
  IF p_until IS NULL OR p_until <= now_ts OR p_until > now_ts + interval '24 hours' THEN
    PERFORM fleet_fail('Give the expected end of charging, within the next 24 hours');
  END IF;
  ch := hub_lock_robot_charger(p_robot, p_charger);
  IF NOT ch.in_service THEN PERFORM fleet_fail(format('Charger %s is out of service', ch.code)); END IF;
  -- from now to the expected end: no other session on this charger or of this robot (any open one counts
  -- until at least now), no booking of this charger for another robot, no booking of this robot on another
  -- charger, no visit of this robot. A booking of THIS robot on THIS charger is the one being used.
  problem := hub_charge_conflict(p_charger, p_robot, now_ts, p_until, 'session');
  IF problem IS NOT NULL THEN PERFORM fleet_fail(problem); END IF;
  SELECT id INTO res_id FROM hub_charger_reservation
  WHERE charger_id = p_charger AND robot_id = p_robot AND released_at IS NULL
    AND starts_at <= now_ts + interval '15 minutes' AND ends_at > now_ts
  ORDER BY starts_at LIMIT 1;
  INSERT INTO hub_charging_session (charger_id, robot_id, reservation_id, started_at, planned_until, start_soc_pct,
                                    started_by_id)
  VALUES (p_charger, p_robot, res_id, now_ts, p_until, p_start_soc, p_actor) RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'hub.start_charging', 'charging_session', new_id::text,
                    jsonb_build_object('charger', ch.code, 'robot_id', p_robot, 'start_soc_pct', p_start_soc),
                    jsonb_build_object('session_id', new_id, 'reservation_id', res_id), p_rid);
  RETURN new_id;
END $fn$;

CREATE OR REPLACE FUNCTION hub_end_charging(p_actor bigint, p_session bigint, p_end_soc int, p_rid text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE s hub_charging_session; ch hub_charger; now_ts timestamptz := clock_timestamp(); reading bigint;
BEGIN
  IF NOT hub_actor_has(p_actor, ARRAY['hub_technician', 'field_operator', 'ops_lead', 'engineer']) THEN
    PERFORM hub_deny('Only the hub technician, a field operator, the ops lead or an engineer can record charging');
  END IF;
  IF p_end_soc IS NOT NULL AND (p_end_soc < 0 OR p_end_soc > 100) THEN
    PERFORM fleet_fail('The battery level is a whole number from 0 to 100');
  END IF;
  SELECT * INTO s FROM hub_charging_session WHERE id = p_session;
  IF NOT FOUND THEN PERFORM fleet_fail('No such charging session'); END IF;
  ch := hub_lock_robot_charger(s.robot_id, s.charger_id);
  SELECT * INTO s FROM hub_charging_session WHERE id = p_session FOR UPDATE;
  IF s.ended_at IS NOT NULL THEN PERFORM fleet_fail('That charging session has already ended'); END IF;
  UPDATE hub_charging_session SET ended_at = now_ts, end_soc_pct = p_end_soc, ended_by_id = p_actor
  WHERE id = p_session;
  IF p_end_soc IS NOT NULL THEN  -- a recorded fact, never departure evidence
    INSERT INTO robot_soc_reading (robot_id, soc_pct, observed_at, source, session_id, recorded_by_id, recorded_at)
    VALUES (s.robot_id, p_end_soc, now_ts, 'charging_session_end', p_session, p_actor, now_ts)
    RETURNING id INTO reading;
  END IF;
  PERFORM hub_audit(p_actor, 'hub.end_charging', 'charging_session', p_session::text,
                    jsonb_build_object('charger', ch.code, 'end_soc_pct', p_end_soc),
                    jsonb_build_object('soc_reading_id', reading), p_rid);
END $fn$;

CREATE OR REPLACE FUNCTION hub_record_soc_reading(p_actor bigint, p_robot bigint, p_soc int,
                                                  p_observed_at timestamptz, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE now_ts timestamptz := clock_timestamp(); obs timestamptz := coalesce(p_observed_at, clock_timestamp());
        new_id bigint; rb fleet_robot;
BEGIN
  IF NOT hub_actor_has(p_actor, ARRAY['hub_technician', 'field_operator', 'ops_lead', 'engineer']) THEN
    PERFORM hub_deny('Only the hub technician, a field operator, the ops lead or an engineer can record a battery level');
  END IF;
  IF p_soc IS NULL OR p_soc < 0 OR p_soc > 100 THEN
    PERFORM fleet_fail('The battery level is a whole number from 0 to 100');
  END IF;
  IF obs > now_ts THEN PERFORM fleet_fail('A reading cannot be in the future'); END IF;
  IF obs < now_ts - interval '24 hours' THEN
    PERFORM fleet_fail('Enter readings from the last 24 hours; older ones are not recorded');
  END IF;
  SELECT * INTO rb FROM fleet_robot WHERE id = p_robot;
  IF NOT FOUND THEN PERFORM fleet_fail('No such robot'); END IF;
  INSERT INTO robot_soc_reading (robot_id, soc_pct, observed_at, source, recorded_by_id, recorded_at)
  VALUES (p_robot, p_soc, obs, 'manual_reading', p_actor, now_ts) RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'hub.record_soc_reading', 'soc_reading', new_id::text,
                    jsonb_build_object('robot', rb.code, 'soc_pct', p_soc, 'observed_at', obs),
                    jsonb_build_object('source', 'manual_reading'), p_rid);
  RETURN new_id;
END $fn$;
"""

SIGNATURES = [
    "hub_reserve_charger(bigint, bigint, bigint, timestamptz, timestamptz, bigint, text)",
    "hub_release_charger_reservation(bigint, bigint, text, text)",
    "hub_start_charging(bigint, bigint, bigint, int, timestamptz, text)",
    "hub_end_charging(bigint, bigint, int, text)",
    "hub_record_soc_reading(bigint, bigint, int, timestamptz, text)",
]
HELPER_SIGNATURES = [
    "hub_lock_robot_charger(bigint, bigint)",
    "hub_charge_conflict(bigint, bigint, timestamptz, timestamptz, text)",
    "hub_audit(bigint, text, text, text, jsonb, jsonb, text)",
    "hub_utc(timestamptz)",
    "hub_actor_has(bigint, text[])",
    "hub_deny(text)",
]
GUARDED = ["hub_charger_reservation", "hub_charging_session", "robot_soc_reading"]

CONSTRAINTS = r"""
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE hub_charger_reservation ADD CONSTRAINT hub_charger_res_no_charger_overlap
  EXCLUDE USING gist (charger_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&) WHERE (released_at IS NULL);
ALTER TABLE hub_charger_reservation ADD CONSTRAINT hub_charger_res_no_robot_overlap
  EXCLUDE USING gist (robot_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&) WHERE (released_at IS NULL);
ALTER TABLE hub_charging_session ADD CONSTRAINT hub_session_no_charger_overlap
  EXCLUDE USING gist (charger_id WITH =,
                      tstzrange(started_at, coalesce(ended_at, 'infinity'::timestamptz), '[)') WITH &&);
ALTER TABLE hub_charging_session ADD CONSTRAINT hub_session_no_robot_overlap
  EXCLUDE USING gist (robot_id WITH =,
                      tstzrange(started_at, coalesce(ended_at, 'infinity'::timestamptz), '[)') WITH &&);
CREATE TRIGGER robot_soc_reading_append_only BEFORE UPDATE OR DELETE ON robot_soc_reading
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER robot_soc_reading_no_truncate BEFORE TRUNCATE ON robot_soc_reading
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
"""

forward = (
    CONSTRAINTS
    + HELPERS
    + FUNCTIONS
    + "".join(
        f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\nGRANT EXECUTE ON FUNCTION {s} TO {APP};\n"
        for s in SIGNATURES
    )
    + "".join(f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\n" for s in HELPER_SIGNATURES)
    + "".join(
        f"REVOKE ALL ON {t} FROM {APP};\nGRANT SELECT ON {t} TO {APP};\nREVOKE ALL ON SEQUENCE {t}_id_seq FROM {APP};\n"
        for t in GUARDED
    )
    + f"REVOKE ALL ON hub_charger FROM {APP};\nGRANT SELECT, INSERT, UPDATE ON hub_charger TO {APP};\n"
    + f"GRANT USAGE, SELECT ON SEQUENCE hub_charger_id_seq TO {APP};\n"
)

reverse = (
    "".join(f"DROP FUNCTION IF EXISTS {s};\n" for s in SIGNATURES + HELPER_SIGNATURES)
    + "DROP TRIGGER IF EXISTS robot_soc_reading_append_only ON robot_soc_reading;\n"
    + "DROP TRIGGER IF EXISTS robot_soc_reading_no_truncate ON robot_soc_reading;\n"
    + "ALTER TABLE hub_charger_reservation DROP CONSTRAINT IF EXISTS hub_charger_res_no_charger_overlap;\n"
    + "ALTER TABLE hub_charger_reservation DROP CONSTRAINT IF EXISTS hub_charger_res_no_robot_overlap;\n"
    + "ALTER TABLE hub_charging_session DROP CONSTRAINT IF EXISTS hub_session_no_charger_overlap;\n"
    + "ALTER TABLE hub_charging_session DROP CONSTRAINT IF EXISTS hub_session_no_robot_overlap;\n"
)


class Migration(migrations.Migration):
    dependencies = [
        ("hub", "0003_charging"),
        ("platform_core", "0002_db_security"),
        ("fleet", "0002_db_enforcement"),
    ]
    operations = [migrations.RunSQL(forward, reverse)]

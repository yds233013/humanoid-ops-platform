"""v2 Phase 03A-2 (docs/13_v2_phase03_contract.md 11.4, 11.5, corrections 10.A and 10.C): robot location and
audited hub transfers of robots and vehicles.

LOCATION. fleet_robot_location(robot) RETURNS TABLE(kind, hub_id, ref, verified), one row:

  transfer_in_transit  the robot's latest transfer is not received (in transit, receipt refused or abandoned);
                       hub_id NULL (it is at neither hub), ref "TR-0001 from HUB-1 to HB"; never verified.
                       An unaccounted report during the transfer wins: kind unaccounted.
  hub                  custody (execution_current_custody) is hub: hub_id = the hub of the latest hub-end
                       custody event, or of the transfer receipt when that is newer (both are written under the
                       robot's row lock with clock_timestamp(), so they are totally ordered); no custody history
                       = the home hub, verified (today's rule, now with a hub).
  vehicle / site       ref = the vehicle code / the site code of the run; hub_id NULL (never a made-up hub).
  unaccounted          ref = the incident code; hub_id NULL.
  unknown              broken or missing custody history; hub_id NULL.

Dispatch eligibility (10.A): only kind = hub, verified, at the visit's hub. The planning view, the planning
triggers (planning/0011) and the departure gate (execution/0007) read this function.

TRANSFERS. fleet_hub_transfer is append-only (one row per step, see fleet.models.HubTransfer); the runtime role
has SELECT only. The four functions are SECURITY DEFINER, owned by ops_owner, search_path fixed, EXECUTE revoked
from PUBLIC and granted to ops_app only. Each:

  1. refuses an unknown, inactive, NULL or forged actor first (hub_actor_has: an allowed role at any hub);
  2. locks the asset row FOR UPDATE (robot or vehicle), THEN the settings lock SHARED (contract 11.5: the order
     of planning and authorization, which take the robot after the settings lock: shared holders never conflict
     and an exclusive holder holds no guard row, so neither order can deadlock);
  3. checks the actor's grant AT THE HUB of the locked row with hub_actor_has_at (it takes the grants FOR SHARE,
     so a revocation and the step are ordered):
        start        hub_technician or ops_lead at the source (the asset's home hub)
        receive      hub_technician or ops_lead at the destination, or at the source after an abandonment
        refuse       hub_technician or ops_lead at the source or the destination (see the report: the brief
                     names the source; the receiving hub is the one that sees the problem)
        abandon      ops_lead at the source
  4. re-reads the state after the locks, checks the step, appends one row and writes one audit row.

Robot start requires: verified at hub(source) (fleet_robot_location), no open transfer, no open run, no unused
departure authorization (not departed, not revoked, not expired), no booking that has not ended, no charger
reservation or charging session that has not ended. Every blocker is listed in the refusal. Start bumps the
robot's guard version (any guard snapshot taken before no longer matches) and touches no hold. Receipt needs the
robot's custody to be a verified hub (an unaccounted robot must be recovered first: 10.C), sets the home hub to
the receiving hub (which bumps the version again) and never touches a hold. Of two simultaneous receipts the
second waits on the asset row and then finds it received; the partial unique index fleet_transfer_received_once
is the backstop. Vehicles: the same, with "no open trip and no future trip or booking" instead of the robot
checks.

fleet_transfer_apply_start is the internal step (no checks); owner only, not executable by ops_app or PUBLIC.

Reverse drops the functions, the triggers and the grants (the table goes with fleet/0014's reverse).
"""

from django.conf import settings
from django.db import migrations

from platform_core.locks import SETTINGS_LOCK_KEY

APP = settings.DB_APP_ROLE
ROLES = "ARRAY['hub_technician', 'ops_lead']"

SQL = r"""
CREATE TRIGGER fleet_hub_transfer_append_only BEFORE UPDATE OR DELETE ON fleet_hub_transfer
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER fleet_hub_transfer_no_truncate BEFORE TRUNCATE ON fleet_hub_transfer
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();

CREATE OR REPLACE FUNCTION fleet_hub_code(p_hub bigint) RETURNS text
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT code FROM platform_core_hub WHERE id = p_hub), '?')
$$;

CREATE OR REPLACE FUNCTION fleet_transfer_code(p_transfer bigint) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT 'TR-' || lpad(p_transfer::text, 4, '0') $$;

-- The latest step of the asset's latest transfer (one open transfer at a time, so ids are monotonic per asset).
CREATE OR REPLACE FUNCTION fleet_asset_last_transfer_step(p_kind text, p_asset bigint) RETURNS fleet_hub_transfer
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT t.* FROM fleet_hub_transfer t
  WHERE (p_kind = 'robot' AND t.robot_id = p_asset) OR (p_kind = 'vehicle' AND t.vehicle_id = p_asset)
  ORDER BY t.id DESC LIMIT 1
$$;

CREATE OR REPLACE FUNCTION fleet_transfer_text(t fleet_hub_transfer) RETURNS text
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT fleet_transfer_code(t.transfer_id) || ' from ' || fleet_hub_code(t.from_hub_id) || ' to '
         || fleet_hub_code(t.to_hub_id)
         || CASE t.state WHEN 'receipt_refused' THEN ', receipt refused' WHEN 'abandoned' THEN ', abandoned' ELSE '' END
$$;

CREATE OR REPLACE FUNCTION fleet_robot_location(p_robot bigint)
RETURNS TABLE (kind text, hub_id bigint, ref text, verified boolean)
LANGUAGE plpgsql STABLE SET search_path = public, pg_temp AS $fn$
#variable_conflict use_column
DECLARE rb fleet_robot; c record; e execution_custodyevent; t fleet_hub_transfer; h bigint;
BEGIN
  SELECT * INTO rb FROM fleet_robot r WHERE r.id = p_robot;
  IF rb.id IS NULL THEN
    RETURN QUERY SELECT 'unknown'::text, NULL::bigint, ''::text, false;
    RETURN;
  END IF;
  SELECT * INTO c FROM execution_current_custody(p_robot);
  t := fleet_asset_last_transfer_step('robot', p_robot);
  IF t.id IS NOT NULL AND t.state <> 'received' AND c.kind IS DISTINCT FROM 'unaccounted' THEN
    RETURN QUERY SELECT 'transfer_in_transit'::text, NULL::bigint, fleet_transfer_text(t), false;
    RETURN;
  END IF;
  IF c.custody_event_id IS NOT NULL THEN
    SELECT * INTO e FROM execution_custodyevent x WHERE x.id = c.custody_event_id;
  END IF;
  IF c.kind = 'hub' THEN
    IF t.id IS NOT NULL AND t.state = 'received' AND (e.id IS NULL OR t.recorded_at > e.recorded_at) THEN
      h := t.at_hub_id;            -- a receipt newer than the custody history: verified at the receiving hub
      RETURN QUERY SELECT 'hub'::text, h, fleet_hub_code(h), true;
    ELSE
      h := coalesce(e.hub_id, rb.hub_id);  -- no custody history: the home hub (today's baseline)
      RETURN QUERY SELECT 'hub'::text, h, fleet_hub_code(h), coalesce(c.verified, false);
    END IF;
  ELSIF c.kind = 'vehicle' THEN
    RETURN QUERY SELECT 'vehicle'::text, NULL::bigint,
      coalesce((SELECT v.code::text FROM planning_vehicletrip tr JOIN planning_vehicle v ON v.id = tr.vehicle_id
                WHERE tr.id = e.trip_id), ''), coalesce(c.verified, false);
  ELSIF c.kind = 'site' THEN
    RETURN QUERY SELECT 'site'::text, NULL::bigint,
      coalesce((SELECT s.code::text FROM planning_visit v JOIN planning_site s ON s.id = v.site_id
                WHERE v.id = e.visit_id), ''), coalesce(c.verified, false);
  ELSIF c.kind = 'unaccounted' THEN
    RETURN QUERY SELECT 'unaccounted'::text, NULL::bigint,
      coalesce((SELECT i.code::text FROM execution_incident i WHERE i.id = e.incident_id), ''), false;
  ELSE
    RETURN QUERY SELECT 'unknown'::text, NULL::bigint, ''::text, false;
  END IF;
END $fn$;
REVOKE ALL ON FUNCTION fleet_robot_location(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fleet_robot_location(bigint) TO {app};

-- One plain sentence for a location that is not a verified hub; NULL for a verified hub.
CREATE OR REPLACE FUNCTION fleet_location_problem(p_kind text, p_ref text, p_verified boolean) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN p_kind = 'hub' AND p_verified THEN NULL
    WHEN p_kind = 'hub' THEN 'At hub ' || p_ref || ', not verified'
    WHEN p_kind = 'transfer_in_transit' THEN 'In transfer ' || p_ref
    WHEN p_kind = 'vehicle' THEN 'Not at a hub: in vehicle ' || p_ref
    WHEN p_kind = 'site' THEN 'Not at a hub: at site ' || p_ref
    WHEN p_kind = 'unaccounted' THEN 'Not at a hub: unaccounted for'
    ELSE 'Location unknown' END
$$;
GRANT EXECUTE ON FUNCTION fleet_location_problem(text, text, boolean) TO {app};

-- What stops an asset from starting a transfer now, as plain sentences (empty = nothing). Read after the locks.
CREATE OR REPLACE FUNCTION fleet_transfer_blockers(p_kind text, p_asset bigint) RETURNS text[]
LANGUAGE plpgsql STABLE SET search_path = public, pg_temp AS $fn$
DECLARE res text[] := ARRAY[]::text[]; t fleet_hub_transfer; loc record; home bigint; rid bigint; now_ts timestamptz;
BEGIN
  now_ts := clock_timestamp();
  t := fleet_asset_last_transfer_step(p_kind, p_asset);
  IF t.id IS NOT NULL AND t.state <> 'received' THEN
    res := res || ('it is already in transfer ' || fleet_transfer_text(t));
  END IF;
  IF p_kind = 'robot' THEN
    home := (SELECT r.hub_id FROM fleet_robot r WHERE r.id = p_asset);
    SELECT * INTO loc FROM fleet_robot_location(p_asset);
    IF loc.kind <> 'transfer_in_transit' AND NOT (loc.kind = 'hub' AND loc.verified AND loc.hub_id = home) THEN
      res := res || ('it is not verified at hub ' || fleet_hub_code(home) || ' ('
                     || coalesce(fleet_location_problem(loc.kind, loc.ref, loc.verified), 'at hub ' || loc.ref)
                     || ')');
    END IF;
    rid := execution_open_run_id(p_asset);
    IF rid IS NOT NULL THEN
      res := res || ('it has an open run ' || (SELECT x.code FROM execution_executionrun x WHERE x.id = rid));
    END IF;
    res := res || ARRAY(
      SELECT 'departure authorization ' || a.code || ' for visit ' || v.code || ' has not been used'
      FROM readiness_departureauthorization a JOIN planning_visit v ON v.id = a.visit_id
      WHERE a.robot_id = p_asset AND a.expires_at > now_ts
        AND NOT EXISTS (SELECT 1 FROM readiness_departureevent d WHERE d.authorization_id = a.id)
        AND NOT EXISTS (SELECT 1 FROM readiness_authorizationrevocation x WHERE x.authorization_id = a.id)
      ORDER BY a.id);
    res := res || ARRAY(
      SELECT 'it is booked for visit ' || v.code || ' until ' || hub_utc(max(pr.ends_at))
      FROM planning_reservation pr JOIN planning_resource res ON res.id = pr.resource_id
      JOIN planning_visitrevision vr ON vr.id = pr.revision_id JOIN planning_visit v ON v.id = vr.visit_id
      WHERE res.robot_id = p_asset AND pr.released_at IS NULL AND pr.ends_at > now_ts
      GROUP BY v.code ORDER BY v.code);
    IF EXISTS (SELECT 1 FROM hub_charger_reservation r WHERE r.robot_id = p_asset AND r.released_at IS NULL
                 AND r.ends_at > now_ts)
       OR EXISTS (SELECT 1 FROM hub_charging_session s WHERE s.robot_id = p_asset AND s.ended_at IS NULL) THEN
      res := res || 'it has a charger booking or a charging session that has not ended'::text;
    END IF;
  ELSE
    res := res || ARRAY(
      SELECT 'it is out on the trip of visit ' || v.code
      FROM planning_vehicletrip tr JOIN execution_executionrun x ON x.trip_id = tr.id
      JOIN planning_visit v ON v.id = x.visit_id
      WHERE tr.vehicle_id = p_asset AND NOT execution_run_closed(x.id) ORDER BY v.code);
    res := res || ARRAY(
      SELECT 'it is booked for visit ' || v.code || ' until ' || hub_utc(tr.return_at)
      FROM planning_vehicletrip tr JOIN planning_visitrevision vr ON vr.id = tr.revision_id
      JOIN planning_visit v ON v.id = vr.visit_id
      WHERE tr.vehicle_id = p_asset AND tr.released_at IS NULL AND tr.return_at > now_ts ORDER BY v.code);
    res := res || ARRAY(
      SELECT DISTINCT 'it is booked for visit ' || v.code
      FROM planning_reservation pr JOIN planning_resource res ON res.id = pr.resource_id
      JOIN planning_visitrevision vr ON vr.id = pr.revision_id JOIN planning_visit v ON v.id = vr.visit_id
      WHERE res.vehicle_id = p_asset AND pr.released_at IS NULL AND pr.ends_at > now_ts
        AND NOT EXISTS (SELECT 1 FROM planning_vehicletrip tr WHERE tr.revision_id = vr.id
                          AND tr.vehicle_id = p_asset AND tr.released_at IS NULL AND tr.return_at > now_ts));
  END IF;
  RETURN res;
END $fn$;
REVOKE ALL ON FUNCTION fleet_transfer_blockers(text, bigint) FROM PUBLIC;

-- Lock the asset row FOR UPDATE, then the settings lock SHARED; returns (code, home hub). Refuses an unknown one.
CREATE OR REPLACE FUNCTION fleet_transfer_lock_asset(p_kind text, p_asset bigint, OUT code text, OUT hub_id bigint)
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
BEGIN
  IF p_kind = 'robot' THEN
    SELECT r.code, r.hub_id INTO code, hub_id FROM fleet_robot r WHERE r.id = p_asset FOR UPDATE;
    IF NOT FOUND THEN PERFORM fleet_fail('No such robot'); END IF;
  ELSIF p_kind = 'vehicle' THEN
    SELECT v.code, v.hub_id INTO code, hub_id FROM planning_vehicle v WHERE v.id = p_asset FOR UPDATE;
    IF NOT FOUND THEN PERFORM fleet_fail('No such vehicle'); END IF;
  ELSE
    PERFORM fleet_fail('Choose a robot or a vehicle');
  END IF;
  PERFORM pg_advisory_xact_lock_shared({settings_key});
END $fn$;
REVOKE ALL ON FUNCTION fleet_transfer_lock_asset(text, bigint) FROM PUBLIC;

-- The internal start step (no checks): append the first row and bump the robot's guard version. Owner only.
CREATE OR REPLACE FUNCTION fleet_transfer_apply_start(p_actor bigint, p_kind text, p_asset bigint, p_from bigint,
                                                      p_to bigint, p_reason text, p_rid text) RETURNS bigint
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE new_id bigint;
BEGIN
  new_id := nextval(pg_get_serial_sequence('fleet_hub_transfer', 'id'));
  INSERT INTO fleet_hub_transfer (id, transfer_id, seq, asset_kind, robot_id, vehicle_id, from_hub_id, to_hub_id,
                                  state, at_hub_id, actor_id, reason, request_id, recorded_at)
  VALUES (new_id, new_id, 1, p_kind, CASE WHEN p_kind = 'robot' THEN p_asset END,
          CASE WHEN p_kind = 'vehicle' THEN p_asset END, p_from, p_to, 'in_transit', NULL, p_actor,
          btrim(p_reason), left(coalesce(p_rid, ''), 64), clock_timestamp());
  IF p_kind = 'robot' THEN
    UPDATE fleet_robot SET version = version WHERE id = p_asset;  -- the guard trigger bumps the version
  END IF;
  RETURN new_id;
END $fn$;
REVOKE ALL ON FUNCTION fleet_transfer_apply_start(bigint, text, bigint, bigint, bigint, text, text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION fleet_transfer_start(p_actor bigint, p_kind text, p_asset bigint, p_to_hub bigint,
                                                p_reason text, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE a record; dest platform_core_hub; blockers text[]; new_id bigint; what text;
BEGIN
  IF NOT hub_actor_has(p_actor, {roles}) THEN
    PERFORM hub_deny('Only a hub technician or the ops lead can move a robot or a vehicle to another hub');
  END IF;
  a := fleet_transfer_lock_asset(p_kind, p_asset);
  what := CASE p_kind WHEN 'robot' THEN 'Robot ' ELSE 'Vehicle ' END || a.code;
  IF NOT hub_actor_has_at(p_actor, {roles}, a.hub_id) THEN
    PERFORM hub_deny(format('You hold no hub technician or ops lead role at hub %s', fleet_hub_code(a.hub_id)));
  END IF;
  IF coalesce(btrim(p_reason), '') = '' OR length(btrim(p_reason)) > 200 THEN
    PERFORM fleet_fail('Give the reason for the move (up to 200 characters)');
  END IF;
  SELECT * INTO dest FROM platform_core_hub WHERE id = p_to_hub FOR SHARE;
  IF NOT FOUND THEN PERFORM fleet_fail('Choose the hub to send it to'); END IF;
  IF dest.id = a.hub_id THEN PERFORM fleet_fail(format('%s already belongs to hub %s', what, dest.code)); END IF;
  blockers := fleet_transfer_blockers(p_kind, p_asset);
  IF cardinality(blockers) > 0 THEN
    PERFORM fleet_fail(format('%s cannot move to hub %s: %s', what, dest.code, array_to_string(blockers, '; ')));
  END IF;
  new_id := fleet_transfer_apply_start(p_actor, p_kind, p_asset, a.hub_id, dest.id, p_reason, p_rid);
  PERFORM hub_audit(p_actor, 'fleet.transfer_start', 'hub_transfer', fleet_transfer_code(new_id),
                    jsonb_build_object('kind', p_kind, 'asset', a.code, 'from', fleet_hub_code(a.hub_id),
                                       'to', dest.code, 'reason', btrim(p_reason)),
                    jsonb_build_object('transfer_id', new_id, 'state', 'in_transit'), p_rid);
  RETURN new_id;
END $fn$;

-- The transfer's first row and its latest step, read AFTER the asset lock (fresh statement).
CREATE OR REPLACE FUNCTION fleet_transfer_step_prologue(p_transfer bigint, OUT s fleet_hub_transfer,
                                                        OUT a_code text, OUT a_hub bigint,
                                                        OUT l fleet_hub_transfer)
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE a record;
BEGIN
  SELECT * INTO s FROM fleet_hub_transfer t WHERE t.id = p_transfer AND t.seq = 1;
  IF s.id IS NULL THEN PERFORM fleet_fail('No such transfer'); END IF;
  a := fleet_transfer_lock_asset(s.asset_kind, coalesce(s.robot_id, s.vehicle_id));
  a_code := a.code; a_hub := a.hub_id;
  SELECT * INTO l FROM fleet_hub_transfer t WHERE t.transfer_id = s.id ORDER BY t.seq DESC LIMIT 1;
END $fn$;
REVOKE ALL ON FUNCTION fleet_transfer_step_prologue(bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION fleet_transfer_append(l fleet_hub_transfer, p_state text, p_at bigint, p_actor bigint,
                                                 p_reason text, p_rid text) RETURNS bigint
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE new_id bigint;
BEGIN
  INSERT INTO fleet_hub_transfer (transfer_id, seq, asset_kind, robot_id, vehicle_id, from_hub_id, to_hub_id, state,
                                  at_hub_id, actor_id, reason, request_id, recorded_at)
  VALUES (l.transfer_id, l.seq + 1, l.asset_kind, l.robot_id, l.vehicle_id, l.from_hub_id, l.to_hub_id, p_state,
          p_at, p_actor, coalesce(btrim(p_reason), ''), left(coalesce(p_rid, ''), 64), clock_timestamp())
  RETURNING id INTO new_id;
  RETURN new_id;
END $fn$;
REVOKE ALL ON FUNCTION fleet_transfer_append(fleet_hub_transfer, text, bigint, bigint, text, text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION fleet_transfer_receive(p_actor bigint, p_transfer bigint, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE p record; at_hub bigint; c record; new_id bigint; code text;
BEGIN
  IF NOT hub_actor_has(p_actor, {roles}) THEN
    PERFORM hub_deny('Only a hub technician or the ops lead can receive a robot or a vehicle');
  END IF;
  p := fleet_transfer_step_prologue(p_transfer);
  code := fleet_transfer_code((p.s).id);
  IF (p.l).state = 'received' THEN
    PERFORM fleet_fail(format('%s was already received at hub %s', code, fleet_hub_code((p.l).at_hub_id)));
  END IF;
  -- after an abandonment the asset comes back: the explicit receipt is at the SOURCE hub, never the destination
  at_hub := CASE WHEN (p.l).state = 'abandoned' THEN (p.s).from_hub_id ELSE (p.s).to_hub_id END;
  IF NOT hub_actor_has_at(p_actor, {roles}, at_hub) THEN
    PERFORM hub_deny(format('%s can be received only at hub %s; you hold no hub technician or ops lead role there',
                            code, fleet_hub_code(at_hub)));
  END IF;
  IF (p.s).asset_kind = 'robot' THEN
    SELECT * INTO c FROM execution_current_custody((p.s).robot_id);
    IF c.kind IS DISTINCT FROM 'hub' OR NOT c.verified THEN
      PERFORM fleet_fail(format('%s cannot be received: robot %s is %s; record its recovery first', code, p.a_code,
                                CASE c.kind WHEN 'unaccounted' THEN 'unaccounted for' ELSE 'not in verified custody' END));
    END IF;
  END IF;
  new_id := fleet_transfer_append(p.l, 'received', at_hub, p_actor, '', p_rid);
  IF (p.s).asset_kind = 'robot' THEN
    UPDATE fleet_robot SET hub_id = at_hub WHERE id = (p.s).robot_id;      -- home hub; the version is bumped
  ELSIF p.a_hub IS DISTINCT FROM at_hub THEN
    UPDATE planning_vehicle SET hub_id = at_hub WHERE id = (p.s).vehicle_id;
  END IF;
  PERFORM hub_audit(p_actor, 'fleet.transfer_receive', 'hub_transfer', code,
                    jsonb_build_object('asset', p.a_code, 'at', fleet_hub_code(at_hub)),
                    jsonb_build_object('step_id', new_id, 'state', 'received'), p_rid);
  RETURN new_id;
END $fn$;

CREATE OR REPLACE FUNCTION fleet_transfer_refuse(p_actor bigint, p_transfer bigint, p_reason text, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE p record; new_id bigint; code text;
BEGIN
  IF NOT hub_actor_has(p_actor, {roles}) THEN
    PERFORM hub_deny('Only a hub technician or the ops lead can refuse a receipt');
  END IF;
  p := fleet_transfer_step_prologue(p_transfer);
  code := fleet_transfer_code((p.s).id);
  IF NOT (hub_actor_has_at(p_actor, {roles}, (p.s).from_hub_id)
          OR hub_actor_has_at(p_actor, {roles}, (p.s).to_hub_id)) THEN
    PERFORM hub_deny(format('You hold no hub technician or ops lead role at hub %s or hub %s',
                            fleet_hub_code((p.s).from_hub_id), fleet_hub_code((p.s).to_hub_id)));
  END IF;
  IF (p.l).state <> 'in_transit' THEN
    PERFORM fleet_fail(format('%s is %s; only a transfer on its way can have its receipt refused', code,
                              replace((p.l).state, '_', ' ')));
  END IF;
  IF coalesce(btrim(p_reason), '') = '' OR length(btrim(p_reason)) > 200 THEN
    PERFORM fleet_fail('Give the reason the receipt is refused (up to 200 characters)');
  END IF;
  new_id := fleet_transfer_append(p.l, 'receipt_refused', NULL, p_actor, p_reason, p_rid);
  PERFORM hub_audit(p_actor, 'fleet.transfer_refuse', 'hub_transfer', code,
                    jsonb_build_object('asset', p.a_code, 'reason', btrim(p_reason)),
                    jsonb_build_object('step_id', new_id, 'state', 'receipt_refused'), p_rid);
  RETURN new_id;
END $fn$;

CREATE OR REPLACE FUNCTION fleet_transfer_abandon(p_actor bigint, p_transfer bigint, p_reason text, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE p record; new_id bigint; code text;
BEGIN
  IF NOT hub_actor_has(p_actor, ARRAY['ops_lead']) THEN
    PERFORM hub_deny('Only the ops lead can abandon a transfer');
  END IF;
  p := fleet_transfer_step_prologue(p_transfer);
  code := fleet_transfer_code((p.s).id);
  IF NOT hub_actor_has_at(p_actor, ARRAY['ops_lead'], (p.s).from_hub_id) THEN
    PERFORM hub_deny(format('Only the ops lead of hub %s can abandon %s', fleet_hub_code((p.s).from_hub_id), code));
  END IF;
  IF (p.l).state NOT IN ('in_transit', 'receipt_refused') THEN
    PERFORM fleet_fail(format('%s is %s; it can no longer be abandoned', code, replace((p.l).state, '_', ' ')));
  END IF;
  IF coalesce(btrim(p_reason), '') = '' OR length(btrim(p_reason)) > 200 THEN
    PERFORM fleet_fail('Give the reason for abandoning the transfer (up to 200 characters)');
  END IF;
  new_id := fleet_transfer_append(p.l, 'abandoned', NULL, p_actor, p_reason, p_rid);
  PERFORM hub_audit(p_actor, 'fleet.transfer_abandon', 'hub_transfer', code,
                    jsonb_build_object('asset', p.a_code, 'reason', btrim(p_reason)),
                    jsonb_build_object('step_id', new_id, 'state', 'abandoned'), p_rid);
  RETURN new_id;
END $fn$;
"""

PUBLIC_FUNCTIONS = [
    "fleet_transfer_start(bigint, text, bigint, bigint, text, text)",
    "fleet_transfer_receive(bigint, bigint, text)",
    "fleet_transfer_refuse(bigint, bigint, text, text)",
    "fleet_transfer_abandon(bigint, bigint, text, text)",
]
HELPERS = [
    "fleet_transfer_append(fleet_hub_transfer, text, bigint, bigint, text, text)",
    "fleet_transfer_step_prologue(bigint)",
    "fleet_transfer_apply_start(bigint, text, bigint, bigint, bigint, text, text)",
    "fleet_transfer_lock_asset(text, bigint)",
    "fleet_transfer_blockers(text, bigint)",
    "fleet_location_problem(text, text, boolean)",
    "fleet_robot_location(bigint)",
    "fleet_transfer_text(fleet_hub_transfer)",
    "fleet_asset_last_transfer_step(text, bigint)",
    "fleet_transfer_code(bigint)",
    "fleet_hub_code(bigint)",
]

forward = (
    SQL.replace("{app}", APP).replace("{roles}", ROLES).replace("{settings_key}", str(SETTINGS_LOCK_KEY))
    + "".join(
        f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\nGRANT EXECUTE ON FUNCTION {s} TO {APP};\n"
        for s in PUBLIC_FUNCTIONS
    )
    + f"REVOKE ALL ON fleet_hub_transfer FROM {APP};\nGRANT SELECT ON fleet_hub_transfer TO {APP};\n"
    + f"REVOKE ALL ON SEQUENCE fleet_hub_transfer_id_seq FROM {APP};\n"
)
reverse = (
    "".join(f"DROP FUNCTION IF EXISTS {s};\n" for s in PUBLIC_FUNCTIONS + HELPERS)
    + "DROP TRIGGER IF EXISTS fleet_hub_transfer_append_only ON fleet_hub_transfer;\n"
    + "DROP TRIGGER IF EXISTS fleet_hub_transfer_no_truncate ON fleet_hub_transfer;\n"
)


class Migration(migrations.Migration):
    dependencies = [
        ("fleet", "0014_hub_transfer"),
        ("execution", "0006_custody_hub"),
        ("readiness", "0009_maintenance_gate"),
        ("hub", "0011_hub_authority"),
        ("accounts", "0005_hub_qualified_grants"),
    ]
    operations = [migrations.RunSQL(forward, reverse)]

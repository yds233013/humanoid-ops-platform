"""v2 Phase 03B-4 (docs/13_v2_phase03_contract.md 10.G, 13.4 item 2, 13.5): a home's zone changes only through a
guarded, audited function, and a visit whose home moved after it was planned cannot be authorized or depart.

Before this, ops_app could UPDATE planning_site.zone_id directly: a re-planned visit then took the new zone and
could leave the area of a zone pause with no authority check, no reason and no audit.

WRITER planning_set_site_zone(actor, site, zone, reason, request_id) -> the home's new guard version
(SECURITY DEFINER, owned by ops_owner, fixed search_path, EXECUTE for ops_app only, one audit row
``planning.set_site_zone`` with the reason):
  * AUTHORITY: an active regional ops lead (the role that edits homes today, planning.manage_sites, and the
    only role that may lift a pause). The workspace admin approves nothing operational and is refused; every
    hub-scoped role is refused. Checked first with no lock, then again on the actor's grants FOR SHARE after
    the hub rows are locked (ordered against a revocation).
  * ZONE: an existing ACTIVE zone of any hub, or NULL (unknown). Homes have no hub; the zone's hub is the hub
    whose pauses cover the home's visits by area (operations_active_pause). A no-op change is refused.
  * REASON: required, up to 200 characters; it is stored in the audit row's reason and payload.
  * NO WAY OUT FROM UNDER A PAUSE: refused while an open (requested, planned or confirmed), not departed visit
    of the home is covered by an active pause that would not cover it with the new zone. Lift the pause first
    (the regional ops lead, with a reason). Moving a home INTO a paused area is allowed: it only blocks more.
  * GUARD VERSION: the UPDATE bumps planning_site.version (trigger site_guard), which every departure
    authorization's guard snapshot records: an authorization issued before the change fails at departure.
  * LOCKS (13.4 item 2; docs/evidence/03a_lock_graph.md section 4): actor pre-check (no lock) -> settings Sh
    -> the home FOR UPDATE -> the hub rows of the old zone, the new zone and the dispatch hub of every open
    visit of the home FOR SHARE, ascending -> the actor's grants FOR SHARE -> the checks in fresh statements
    -> UPDATE + audit. The settings lock comes first and the hub rows after the home row (the home is the
    first guard row of the global order, and nothing that holds a hub row ever waits for a guard row).

DIRECT CHANGE REFUSED trigger planning_site_zone_by_function_only (BEFORE INSERT OR UPDATE OF zone_id): with
ops_app as the current user, a changed zone_id (or a new home inserted with a zone) is refused with
insufficient_privilege. Inside the function the current user is the owner. The schema owner (migrations,
provisioning) stays trusted, as for every other owner boundary (fleet/0016).

GATE operations_pause_gate_problem (used by the authorization and departure triggers of operations/0011) now
also refuses a visit whose saved zone differs from its home's current zone: the visit keeps its saved zone
until it is planned again, and a re-plan takes the home's current zone (plan_visit), after which the gates
check the pauses of that zone. Without this, a visit planned before a home moved into a paused zone would
still be checked against its old zone.

Reverse drops the function and the trigger and restores the operations/0011 body of the gate.
"""

from django.conf import settings
from django.db import migrations

from platform_core.locks import SETTINGS_LOCK_KEY

APP = settings.DB_APP_ROLE
KEY = SETTINGS_LOCK_KEY
ROLES = "ARRAY['ops_lead']"
OPEN = "('requested', 'planned', 'confirmed')"

GATE_0011 = rf"""
CREATE OR REPLACE FUNCTION operations_pause_gate_problem(p_visit bigint) RETURNS text
LANGUAGE plpgsql VOLATILE SET search_path = public, pg_temp AS $fn$
DECLARE v record; zone_hub bigint; a record; pz bigint; ph bigint;
BEGIN
  PERFORM pg_advisory_xact_lock_shared({KEY});
  SELECT x.hub_id, x.zone_id INTO v FROM planning_visit x WHERE x.id = p_visit FOR SHARE;
  IF NOT FOUND THEN RETURN NULL; END IF;  -- the other guards refuse an unknown visit
  zone_hub := (SELECT z.hub_id FROM platform_core_zone z WHERE z.id = v.zone_id);
  PERFORM 1 FROM platform_core_hub h WHERE h.id = v.hub_id OR h.id = zone_hub ORDER BY h.id FOR SHARE;
  SELECT * INTO a FROM operations_active_pause(v.hub_id, v.zone_id) LIMIT 1;
  IF a.pause_id IS NULL THEN RETURN NULL; END IF;
  SELECT p.hub_id, p.zone_id INTO ph, pz FROM operations_region_pause p WHERE p.id = a.pause_id;
  RETURN format('Paused: cannot leave until lifted. Departures %s %s are paused: %s',
                CASE WHEN pz IS NULL AND ph = v.hub_id THEN 'from' ELSE 'to' END,
                operations_pause_scope_text(ph, pz), a.reason)
         || CASE WHEN v.zone_id IS NULL AND pz IS NOT NULL
                 THEN ' (this visit''s zone is unknown, so every zone pause at its hub covers it)' ELSE '' END;
END $fn$;
REVOKE ALL ON FUNCTION operations_pause_gate_problem(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION operations_pause_gate_problem(bigint) TO {APP};
"""

# The same lock order and pause check as operations/0011, then the home-moved check (no extra lock: the home
# row is already held FOR SHARE by readiness_issue_authorization and by the departure guard, and a change of
# the home's zone after an authorization bumps the home's version, which the departure guard compares).
GATE = rf"""
CREATE OR REPLACE FUNCTION operations_pause_gate_problem(p_visit bigint) RETURNS text
LANGUAGE plpgsql VOLATILE SET search_path = public, pg_temp AS $fn$
DECLARE v record; zone_hub bigint; a record; pz bigint; ph bigint; home record;
BEGIN
  PERFORM pg_advisory_xact_lock_shared({KEY});
  SELECT x.hub_id, x.zone_id, x.site_id INTO v FROM planning_visit x WHERE x.id = p_visit FOR SHARE;
  IF NOT FOUND THEN RETURN NULL; END IF;  -- the other guards refuse an unknown visit
  zone_hub := (SELECT z.hub_id FROM platform_core_zone z WHERE z.id = v.zone_id);
  PERFORM 1 FROM platform_core_hub h WHERE h.id = v.hub_id OR h.id = zone_hub ORDER BY h.id FOR SHARE;
  SELECT * INTO a FROM operations_active_pause(v.hub_id, v.zone_id) LIMIT 1;
  IF a.pause_id IS NOT NULL THEN
    SELECT p.hub_id, p.zone_id INTO ph, pz FROM operations_region_pause p WHERE p.id = a.pause_id;
    RETURN format('Paused: cannot leave until lifted. Departures %s %s are paused: %s',
                  CASE WHEN pz IS NULL AND ph = v.hub_id THEN 'from' ELSE 'to' END,
                  operations_pause_scope_text(ph, pz), a.reason)
           || CASE WHEN v.zone_id IS NULL AND pz IS NOT NULL
                   THEN ' (this visit''s zone is unknown, so every zone pause at its hub covers it)' ELSE '' END;
  END IF;
  -- v2 03B-4: the visit keeps the zone it was planned with; when its home has moved since, plan it again first
  SELECT s.code, s.zone_id, z.code AS zone_code, z.name AS zone_name INTO home
  FROM planning_site s LEFT JOIN platform_core_zone z ON z.id = s.zone_id WHERE s.id = v.site_id;
  IF home.zone_id IS DISTINCT FROM v.zone_id THEN
    RETURN format('The home %s moved to %s after this visit was planned. Plan the visit again so its zone, '
                  'travel time and pauses are checked',
                  home.code, CASE WHEN home.zone_id IS NULL THEN 'an unknown zone'
                                  ELSE format('zone %s (%s)', home.zone_name, home.zone_code) END);
  END IF;
  RETURN NULL;
END $fn$;
REVOKE ALL ON FUNCTION operations_pause_gate_problem(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION operations_pause_gate_problem(bigint) TO {APP};
"""

SQL = rf"""
CREATE OR REPLACE FUNCTION planning_site_zone_by_function_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
BEGIN
  IF current_user = '{APP}'
     AND ((TG_OP = 'INSERT' AND NEW.zone_id IS NOT NULL)
          OR (TG_OP = 'UPDATE' AND NEW.zone_id IS DISTINCT FROM OLD.zone_id)) THEN
    RAISE EXCEPTION 'Home %: its zone changes only through planning_set_site_zone (the regional ops lead, '
                    'with a reason)', NEW.code USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION planning_site_zone_by_function_only() FROM PUBLIC;
-- named to fire before site_zone_guard (planning/0013), so a direct change is refused for the actor first
CREATE TRIGGER site_zone_change_by_function_only BEFORE INSERT OR UPDATE OF zone_id ON planning_site
  FOR EACH ROW EXECUTE FUNCTION planning_site_zone_by_function_only();

CREATE OR REPLACE FUNCTION planning_set_site_zone(p_actor bigint, p_site bigint, p_zone bigint, p_reason text,
                                                  p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE s planning_site; oz platform_core_zone; nz platform_core_zone; hit record; new_ver bigint;
        why text := btrim(coalesce(p_reason, ''));
BEGIN
  IF NOT hub_actor_has(p_actor, {ROLES}) THEN
    PERFORM hub_deny('Only the regional ops lead can move a home to another zone');
  END IF;
  PERFORM pg_advisory_xact_lock_shared({KEY});  -- settings first (13.4 item 2)
  SELECT * INTO s FROM planning_site WHERE id = p_site FOR UPDATE;
  IF NOT FOUND THEN PERFORM fleet_fail('Choose the home'); END IF;
  IF p_zone IS NOT NULL THEN
    SELECT * INTO nz FROM platform_core_zone WHERE id = p_zone;
    IF NOT FOUND THEN PERFORM fleet_fail('Choose an existing zone, or unknown'); END IF;
  END IF;
  IF s.zone_id IS NOT NULL THEN
    SELECT * INTO oz FROM platform_core_zone WHERE id = s.zone_id;
  END IF;
  -- then the hub rows (FOR SHARE, ascending): the old and new zones' hubs and the open visits' dispatch hubs,
  -- so a pause of any hub whose pauses cover these visits is ordered against this change
  PERFORM 1 FROM platform_core_hub h
  WHERE h.id = oz.hub_id OR h.id = nz.hub_id
     OR h.id IN (SELECT v.hub_id FROM planning_visit v WHERE v.site_id = s.id AND v.status IN {OPEN})
  ORDER BY h.id FOR SHARE;
  IF p_zone IS NOT DISTINCT FROM s.zone_id THEN
    PERFORM fleet_fail(format('Home %s is already in that zone', s.code));
  END IF;
  IF NOT hub_actor_has_at(p_actor, {ROLES}, coalesce(nz.hub_id, oz.hub_id)) THEN
    PERFORM hub_deny('Only the regional ops lead can move a home to another zone');
  END IF;
  IF nz.id IS NOT NULL AND NOT nz.active THEN
    PERFORM fleet_fail(format('Zone %s is retired; choose an active zone for home %s', nz.code, s.code));
  END IF;
  IF why = '' OR length(why) > 200 THEN
    PERFORM fleet_fail('Give the reason for moving the home to another zone (up to 200 characters)');
  END IF;
  -- a fresh statement, after the hub locks: a pause committed while this waited is seen
  SELECT v.code, operations_pause_scope_text(p.hub_id, p.zone_id) AS place, p.reason INTO hit
  FROM planning_visit v
  CROSS JOIN LATERAL operations_active_pause(v.hub_id, v.zone_id) a
  JOIN operations_region_pause p ON p.id = a.pause_id
  WHERE v.site_id = s.id AND v.status IN {OPEN}
    AND NOT EXISTS (SELECT 1 FROM readiness_departureevent d WHERE d.visit_id = v.id)
    AND NOT EXISTS (SELECT 1 FROM operations_active_pause(v.hub_id, p_zone))
  ORDER BY v.code LIMIT 1;
  IF FOUND THEN
    PERFORM fleet_fail(format('Visit %s of home %s is paused (departures for %s: %s). Moving the home would '
                              'take it out of that pause: lift the pause first', hit.code, s.code, hit.place,
                              hit.reason));
  END IF;
  UPDATE planning_site SET zone_id = p_zone WHERE id = s.id RETURNING version INTO new_ver;
  INSERT INTO platform_core_auditevent (occurred_at, actor_id, command, outcome, entity_type, entity_id, payload,
                                        result, reason, request_id)
  VALUES (clock_timestamp(), p_actor, 'planning.set_site_zone', 'committed', 'site', s.code,
          jsonb_build_object('site', s.code, 'from_zone', oz.code, 'from_hub', fleet_hub_code(oz.hub_id),
                             'to_zone', nz.code, 'to_hub', fleet_hub_code(nz.hub_id), 'reason', why),
          jsonb_build_object('site_version', new_ver), why, left(coalesce(p_rid, ''), 64));
  RETURN new_ver;
END $fn$;
REVOKE ALL ON FUNCTION planning_set_site_zone(bigint, bigint, bigint, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION planning_set_site_zone(bigint, bigint, bigint, text, text) TO {APP};
"""

SQL_REVERSE = """
DROP FUNCTION IF EXISTS planning_set_site_zone(bigint, bigint, bigint, text, text);
DROP TRIGGER IF EXISTS site_zone_change_by_function_only ON planning_site;
DROP FUNCTION IF EXISTS planning_site_zone_by_function_only();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("planning", "0013_zones_and_travel"),
        ("operations", "0011_region_pause"),  # operations_active_pause, operations_pause_gate_problem
        ("readiness", "0009_maintenance_gate"),
        ("hub", "0015_guarded_creation"),  # hub_actor_has, hub_deny
        ("fleet", "0015_location_and_transfer_sql"),  # fleet_hub_code
        ("accounts", "0008_safety_pause_role"),  # hub_actor_has_at
        ("platform_core", "0006_zones"),
    ]

    operations = [
        migrations.RunSQL(SQL, SQL_REVERSE),
        migrations.RunSQL(GATE, GATE_0011),
    ]

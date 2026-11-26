"""v2 03B closure carry-forward (contract 14.7; 10.B / I-8: hub rules for writes are enforced by the database):
a plan's crew must be based at the visit's hub, like its robot and vehicle (planning/0010).

INTEGRITY (trigger function planning_visit_hub_guard, extended; same triggers as planning/0010):
  * planning_visitrevision BEFORE INSERT: the revision's lead and second (when set) have the visit's hub as their
    home hub (planning_person.hub_id), as its robot and vehicle already must;
  * planning_visit BEFORE UPDATE OF hub_id: the current revision's lead and second have the new hub.
A refusal is check_violation (fleet_fail) with a plain message:
  "{name} works at {hub name}, not at this visit's hub. Choose crew from {visit hub name}."
so run_command reports it as a refusal and nothing is written. It is a resource rule, not a permission: no role
(the regional ops lead included) can plan another hub's person.

EXISTING PLANS: nothing is rewritten and no row is checked by this migration. A revision already saved with crew of
another hub stays as it is and keeps working (confirmation, authorization and departure do not insert a revision);
the next re-plan inserts a new revision, which must then pick same-hub crew. Check query (current revisions with
crew of another hub): CROSS_HUB_CREW_SQL in planning/services.py.

Reverse: restores the planning/0010 function (robot and vehicle only).
"""

from django.db import migrations

GUARD_0010 = __import__("importlib").import_module("planning.migrations.0010_hub_ownership").GUARD

GUARD = r"""
CREATE OR REPLACE FUNCTION planning_visit_hub_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE v_hub bigint; v_code text; v_hub_name text; rb record; vh record; pp record; pid bigint;
        rev planning_visitrevision;
BEGIN
  IF TG_TABLE_NAME = 'planning_visitrevision' THEN
    SELECT hub_id, code INTO v_hub, v_code FROM planning_visit WHERE id = NEW.visit_id;
    rev := NEW;
  ELSE
    IF NEW.hub_id IS NOT DISTINCT FROM OLD.hub_id OR NEW.current_revision_id IS NULL THEN RETURN NEW; END IF;
    v_hub := NEW.hub_id; v_code := NEW.code;
    SELECT * INTO rev FROM planning_visitrevision WHERE id = NEW.current_revision_id;
  END IF;
  IF rev.robot_id IS NOT NULL THEN
    SELECT r.code, r.hub_id INTO rb FROM fleet_robot r WHERE r.id = rev.robot_id;
    IF rb.hub_id IS DISTINCT FROM v_hub THEN
      PERFORM fleet_fail(format('Robot %s belongs to another hub than visit %s; it cannot be dispatched from there',
                                rb.code, v_code));
    END IF;
  END IF;
  IF rev.vehicle_id IS NOT NULL THEN
    SELECT x.code, x.hub_id INTO vh FROM planning_vehicle x WHERE x.id = rev.vehicle_id;
    IF vh.hub_id IS DISTINCT FROM v_hub THEN
      PERFORM fleet_fail(format('Vehicle %s belongs to another hub than visit %s; it cannot be dispatched from there',
                                vh.code, v_code));
    END IF;
  END IF;
  -- 03B closure: the crew (lead, then second) is based at the visit's hub
  FOREACH pid IN ARRAY ARRAY[rev.lead_id, rev.second_id] LOOP
    CONTINUE WHEN pid IS NULL;
    SELECT p.hub_id,
           COALESCE(NULLIF(btrim(coalesce(u.first_name, '') || ' ' || coalesce(u.last_name, '')), ''),
                    u.username, p.code) AS name,
           h.name AS hub_name
      INTO pp
      FROM planning_person p
      LEFT JOIN auth_user u ON u.id = p.user_id
      LEFT JOIN platform_core_hub h ON h.id = p.hub_id
     WHERE p.id = pid;
    IF pp.hub_id IS DISTINCT FROM v_hub THEN
      SELECT name INTO v_hub_name FROM platform_core_hub WHERE id = v_hub;
      PERFORM fleet_fail(format('%s works at %s, not at this visit''s hub. Choose crew from %s.',
                                pp.name, coalesce(pp.hub_name, 'another hub'),
                                coalesce(v_hub_name, 'this visit''s hub')));
    END IF;
  END LOOP;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION planning_visit_hub_guard() FROM PUBLIC;
"""

# the reverse: the 0010 function only (its CREATE TRIGGER statements are not re-run)
REVERSE = GUARD_0010.split("CREATE TRIGGER")[0]


class Migration(migrations.Migration):
    dependencies = [("planning", "0014_guarded_site_zone")]

    operations = [migrations.RunSQL(GUARD, REVERSE)]

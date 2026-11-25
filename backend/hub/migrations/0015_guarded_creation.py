"""v2 Phase 03A closure (EM review; docs/13_v2_phase03_contract.md I-8, 10.B, section 12): robots, vehicles and
chargers are created only through guarded database functions.

Until now ops_app could INSERT fleet_robot, planning_vehicle and hub_charger directly, at any hub, with no
command and no audit row: the permission was checked in Python only. ops_app now loses INSERT on the three tables
(and any use of their id sequences); it keeps SELECT and UPDATE (lifecycle, manifest, name, in service...; a hub
change is refused by fleet/0016 and hub/0014) and, for robots and vans, the DELETE the delete guards already
refuse. Creation goes through three SECURITY DEFINER functions owned by ops_owner, with a fixed search_path,
EXECUTE revoked from PUBLIC and granted to ops_app only:

    fleet_create_robot(actor, code, serial, hub, request_id) -> robot id          (fleet.register_robot)
    planning_create_vehicle(actor, code, robot_capacity, restraint_kit, hub, request_id) -> vehicle id
                                                                                   (planning.manage_vehicles)
    hub_create_charger(actor, code, name, note, hub, request_id) -> charger id     (hub.manage_chargers)

RULES: exactly those of the Python services before this change (fleet.services.register_robot,
planning.services.register_vehicle, hub.charging.create_charger), nothing new:
  * roles: ops lead or engineering lead (robot), ops lead (vehicle), admin or ops lead (charger);
  * robot: no robot with that code or that serial; lifecycle commissioning, version 1, registered by the actor;
  * vehicle: robot capacity unknown (NULL) or at least 1; no vehicle with that code; its planning resource row;
  * charger: code of letters, digits and dashes, up to 12 (upper-cased, trimmed); a name up to 80 and a note up
    to 200 characters; no charger with that code; in service, created by the actor.
The hub: ``hub`` names it, or NULL for the only hub (platform_core_default_hub: "hub required" with several).

ACTOR AND HUB. The actor must be an active user holding one of the roles (checked first: a forged, NULL, zero or
missing actor is refused before anything is read or created, including the default hub), and then hold it AT
the target hub (hub_actor_has_at, which takes the actor's grants FOR SHARE: a revocation and the creation are
ordered). The roles are regional today, so they apply at every hub; a hub grant would apply at its hub only.

A refusal raises (insufficient_privilege for the actor, check_violation for a rule, unique_violation for a
duplicate) and writes nothing. Each creation appends its audit row (the same command name, entity, payload and
result as run_command wrote before). Reverse drops the functions and gives ops_app back INSERT.
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE

SQL = r"""
CREATE OR REPLACE FUNCTION fleet_create_robot(p_actor bigint, p_code text, p_serial text, p_hub bigint, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE roles text[] := ARRAY['ops_lead', 'engineering_lead']; h bigint := p_hub; new_id bigint; code text;
BEGIN
  IF NOT hub_actor_has(p_actor, roles) THEN
    PERFORM hub_deny('Only the ops lead or the engineering lead can register a robot');
  END IF;
  IF h IS NULL THEN h := platform_core_default_hub(); END IF;
  IF NOT EXISTS (SELECT 1 FROM platform_core_hub WHERE id = h) THEN PERFORM fleet_fail('No such hub'); END IF;
  code := (SELECT x.code FROM platform_core_hub x WHERE x.id = h);
  IF NOT hub_actor_has_at(p_actor, roles, h) THEN
    PERFORM hub_deny(format('Only the ops lead or the engineering lead can register a robot at hub %s', code));
  END IF;
  IF EXISTS (SELECT 1 FROM fleet_robot r WHERE r.code = p_code OR r.serial = p_serial) THEN
    RAISE EXCEPTION 'A robot with code % or serial % already exists', p_code, p_serial
      USING ERRCODE = 'unique_violation';
  END IF;
  INSERT INTO fleet_robot (code, serial, lifecycle, version, registered_at, current_manifest_id, registered_by_id,
                           hub_id)
  VALUES (p_code, p_serial, 'commissioning', 1, clock_timestamp(), NULL, p_actor, h)
  RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'fleet.register_robot', 'robot', p_code,
                    jsonb_build_object('code', p_code, 'serial', p_serial)
                      || CASE WHEN p_hub IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('hub', code) END,
                    jsonb_build_object('lifecycle', 'commissioning', 'hub_id', h), p_rid);
  RETURN new_id;
END $fn$;

CREATE OR REPLACE FUNCTION planning_create_vehicle(p_actor bigint, p_code text, p_capacity integer, p_kit text,
                                                   p_hub bigint, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE roles text[] := ARRAY['ops_lead']; h bigint := p_hub; new_id bigint; code text;
BEGIN
  IF NOT hub_actor_has(p_actor, roles) THEN PERFORM hub_deny('Only the ops lead can add a vehicle'); END IF;
  IF p_capacity IS NOT NULL AND p_capacity < 1 THEN
    PERFORM fleet_fail('Robot capacity must be at least 1, or unknown');
  END IF;
  IF h IS NULL THEN h := platform_core_default_hub(); END IF;
  IF NOT EXISTS (SELECT 1 FROM platform_core_hub WHERE id = h) THEN PERFORM fleet_fail('No such hub'); END IF;
  code := (SELECT x.code FROM platform_core_hub x WHERE x.id = h);
  IF NOT hub_actor_has_at(p_actor, roles, h) THEN
    PERFORM hub_deny(format('Only the ops lead can add a vehicle at hub %s', code));
  END IF;
  IF EXISTS (SELECT 1 FROM planning_vehicle v WHERE v.code = p_code) THEN
    RAISE EXCEPTION 'Vehicle % exists', p_code USING ERRCODE = 'unique_violation';
  END IF;
  INSERT INTO planning_vehicle (code, robot_capacity, restraint_kit, notes, hub_id)
  VALUES (p_code, p_capacity, coalesce(p_kit, ''), '', h)
  RETURNING id INTO new_id;
  INSERT INTO planning_resource (kind, vehicle_id) VALUES ('vehicle', new_id);
  PERFORM hub_audit(p_actor, 'planning.register_vehicle', 'vehicle', p_code,
                    jsonb_build_object('code', p_code, 'robot_capacity', p_capacity,
                                       'restraint_kit', coalesce(p_kit, ''))
                      || CASE WHEN p_hub IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('hub', code) END,
                    jsonb_build_object('robot_capacity', p_capacity), p_rid);
  RETURN new_id;
END $fn$;

CREATE OR REPLACE FUNCTION hub_create_charger(p_actor bigint, p_code text, p_name text, p_note text, p_hub bigint,
                                              p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE roles text[] := ARRAY['admin', 'ops_lead']; h bigint := p_hub; new_id bigint; hcode text;
        c text := upper(btrim(coalesce(p_code, ''))); n text := btrim(coalesce(p_name, ''));
        nt text := btrim(coalesce(p_note, ''));
BEGIN
  IF NOT hub_actor_has(p_actor, roles) THEN
    PERFORM hub_deny('Only the workspace admin or the ops lead can set up a charger');
  END IF;
  IF c = '' OR length(c) > 12 OR c !~ '^[[:alnum:]-]+$' THEN
    PERFORM fleet_fail('The charger code uses letters, digits and dashes, up to 12 characters');
  END IF;
  IF n = '' OR length(n) > 80 OR length(nt) > 200 THEN
    PERFORM fleet_fail('Give the charger a name (up to 80 characters)');
  END IF;
  IF h IS NULL THEN h := platform_core_default_hub(); END IF;
  IF NOT EXISTS (SELECT 1 FROM platform_core_hub WHERE id = h) THEN PERFORM fleet_fail('No such hub'); END IF;
  hcode := (SELECT x.code FROM platform_core_hub x WHERE x.id = h);
  IF NOT hub_actor_has_at(p_actor, roles, h) THEN
    PERFORM hub_deny(format('Only the workspace admin or the ops lead can set up a charger at hub %s', hcode));
  END IF;
  IF EXISTS (SELECT 1 FROM hub_charger x WHERE x.code = c) THEN
    RAISE EXCEPTION 'A charger with code % already exists', c USING ERRCODE = 'unique_violation';
  END IF;
  INSERT INTO hub_charger (code, name, in_service, note, created_at, created_by_id, hub_id)
  VALUES (c, n, true, nt, clock_timestamp(), p_actor, h)
  RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'hub.create_charger', 'charger', c,
                    jsonb_build_object('code', c, 'name', n, 'note', nt)
                      || CASE WHEN p_hub IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('hub', hcode) END,
                    jsonb_build_object('in_service', true, 'hub_id', h), p_rid);
  RETURN new_id;
END $fn$;
"""

SIGNATURES = [
    "fleet_create_robot(bigint, text, text, bigint, text)",
    "planning_create_vehicle(bigint, text, integer, text, bigint, text)",
    "hub_create_charger(bigint, text, text, text, bigint, text)",
]
TABLES = ["fleet_robot", "planning_vehicle", "hub_charger"]

forward = (
    SQL
    + "".join(
        f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\nGRANT EXECUTE ON FUNCTION {s} TO {APP};\n"
        for s in SIGNATURES
    )
    + "".join(
        f"REVOKE INSERT ON {t} FROM {APP};\nREVOKE ALL ON SEQUENCE {t}_id_seq FROM {APP};\n" for t in TABLES
    )
)
reverse = "".join(f"DROP FUNCTION IF EXISTS {s};\n" for s in SIGNATURES) + "".join(
    f"GRANT INSERT ON {t} TO {APP};\n" for t in TABLES
)


class Migration(migrations.Migration):
    dependencies = [
        ("hub", "0014_charger_hub_fixed"),
        ("fleet", "0017_planning_location_predicate"),
        ("planning", "0012_future_planning_location"),
        ("accounts", "0007_definer_execute_hardening"),
    ]

    operations = [migrations.RunSQL(forward, reverse)]

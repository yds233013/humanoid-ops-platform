"""v2 Phase 03A-1 (docs/13_v2_phase03_contract.md 11.2, correction B): the guarded charging and stock writes gain
the hub dimension.

Every SECURITY DEFINER write on a hub-owned record keeps its first role check (any hub, unchanged refusals for a
user with no such role at all) and now ALSO checks, AFTER taking the target row's lock, that the actor holds an
allowed role AT THE HUB OF THAT LOCKED ROW: hub_actor_has_at(actor, roles, hub) (accounts 0005), which takes the
actor's grants FOR SHARE first, so a concurrent revocation and the write are totally ordered. The hub is always
read from the record (charger, robot, stock item), never taken from the caller.

  charging (hub/0004)                     target row locked              hub checked
    hub_reserve_charger                   robot, then charger            the charger's
    hub_release_charger_reservation       robot, then charger            the charger's
    hub_start_charging                    robot, then charger            the charger's
    hub_end_charging                      robot, then charger            the charger's
    hub_record_soc_reading                robot (now FOR SHARE)          the robot's
  stock (hub/0008, hub/0009)
    hub_receive / adjust / issue / consume / return / reserve / release_stock   the item   the item's
    hub_create_stock_item(actor, code, name, kind, unit, HUB, rid)  NEW: the hub row FOR SHARE, the hub's
    hub_create_stock_item(actor, code, name, kind, unit, rid)       now the DEFAULT hub (platform_core 0005:
                                                                    "hub required" with several hubs), never
                                                                    the "first hub" (hub/0008 shortcut removed)

Reservations and sessions now record hub_id = the charger's hub (the trigger of hub/0010 checks the robot and the
visit belong to it). Stock: a visit named for a reservation, issue, consumption or return must belong to the
item's hub; item codes are unique per hub.

A refusal raises insufficient_privilege (role, hub) or check_violation and writes nothing. Owners, search_path,
EXECUTE grants (ops_app only, never PUBLIC) are kept by CREATE OR REPLACE; the new 7-argument
hub_create_stock_item gets the same treatment explicitly.

Reverse: restores the hub/0004 and hub/0009 bodies and drops the 7-argument function.
"""

import importlib

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE
charging = importlib.import_module("hub.migrations.0004_charging_write_boundary")
stock = importlib.import_module("hub.migrations.0009_stock_visit_and_units")

HUB_CODE = "(SELECT h.code FROM platform_core_hub h WHERE h.id = {expr})"


def _deny_at(roles: str, expr: str, who: str) -> str:
    return (
        f"  IF NOT hub_actor_has_at(p_actor, {roles}, {expr}) THEN\n"
        f"    PERFORM hub_deny(format('{who} at hub %s', {HUB_CODE.format(expr=expr)}));\n"
        "  END IF;\n"
    )


def _apply(sql: str, replacements: list[tuple[str, str, int]]) -> str:
    for before, after, count in replacements:
        assert sql.count(before) == count, (before[:70], sql.count(before))
        sql = sql.replace(before, after)
    return sql


# ------------------------------------------------------------------ charging
RES_ROLES = "ARRAY['hub_technician', 'ops_lead']"
REC_ROLES = "ARRAY['hub_technician', 'field_operator', 'ops_lead', 'engineer']"
CHARGING = _apply(
    charging.FUNCTIONS,
    [
        (  # reserve: after robot + charger locks
            "  ch := hub_lock_robot_charger(p_robot, p_charger);\n"
            "  IF NOT ch.in_service THEN PERFORM fleet_fail(format('Charger %s is out of service', ch.code)); END IF;\n"
            "  IF p_visit IS NOT NULL",
            "  ch := hub_lock_robot_charger(p_robot, p_charger);\n"
            + _deny_at(RES_ROLES, "ch.hub_id", "You hold no hub technician or ops lead role")
            + "  IF NOT ch.in_service THEN PERFORM fleet_fail(format('Charger %s is out of service', ch.code)); END IF;\n"
            "  IF p_visit IS NOT NULL",
            1,
        ),
        (
            "  INSERT INTO hub_charger_reservation (charger_id, robot_id, visit_id, starts_at, ends_at, created_by_id,\n"
            "                                       created_at, released_reason)\n"
            "  VALUES (p_charger, p_robot, p_visit, p_starts, p_ends, p_actor, now_ts, '') RETURNING id INTO new_id;",
            "  INSERT INTO hub_charger_reservation (charger_id, robot_id, visit_id, starts_at, ends_at, created_by_id,\n"
            "                                       created_at, released_reason, hub_id)\n"
            "  VALUES (p_charger, p_robot, p_visit, p_starts, p_ends, p_actor, now_ts, '', ch.hub_id)\n"
            "  RETURNING id INTO new_id;",
            1,
        ),
        (  # release
            "  ch := hub_lock_robot_charger(r.robot_id, r.charger_id);\n",
            "  ch := hub_lock_robot_charger(r.robot_id, r.charger_id);\n"
            + _deny_at(RES_ROLES, "ch.hub_id", "You hold no hub technician or ops lead role"),
            1,
        ),
        (  # start
            "  ch := hub_lock_robot_charger(p_robot, p_charger);\n"
            "  IF NOT ch.in_service THEN PERFORM fleet_fail(format('Charger %s is out of service', ch.code)); END IF;\n"
            "  -- from now",
            "  ch := hub_lock_robot_charger(p_robot, p_charger);\n"
            + _deny_at(REC_ROLES, "ch.hub_id", "You hold no role that records charging")
            + "  IF NOT ch.in_service THEN PERFORM fleet_fail(format('Charger %s is out of service', ch.code)); END IF;\n"
            "  -- from now",
            1,
        ),
        (
            "  INSERT INTO hub_charging_session (charger_id, robot_id, reservation_id, started_at, planned_until, start_soc_pct,\n"
            "                                    started_by_id)\n"
            "  VALUES (p_charger, p_robot, res_id, now_ts, p_until, p_start_soc, p_actor) RETURNING id INTO new_id;",
            "  INSERT INTO hub_charging_session (charger_id, robot_id, reservation_id, started_at, planned_until, start_soc_pct,\n"
            "                                    started_by_id, hub_id)\n"
            "  VALUES (p_charger, p_robot, res_id, now_ts, p_until, p_start_soc, p_actor, ch.hub_id) RETURNING id INTO new_id;",
            1,
        ),
        (  # end
            "  ch := hub_lock_robot_charger(s.robot_id, s.charger_id);\n",
            "  ch := hub_lock_robot_charger(s.robot_id, s.charger_id);\n"
            + _deny_at(REC_ROLES, "ch.hub_id", "You hold no role that records charging"),
            1,
        ),
        (  # battery reading: the robot row is the target (FOR SHARE: its hub cannot change underneath)
            "  SELECT * INTO rb FROM fleet_robot WHERE id = p_robot;\n"
            "  IF NOT FOUND THEN PERFORM fleet_fail('No such robot'); END IF;\n",
            "  SELECT * INTO rb FROM fleet_robot WHERE id = p_robot FOR SHARE;\n"
            "  IF NOT FOUND THEN PERFORM fleet_fail('No such robot'); END IF;\n"
            + _deny_at(REC_ROLES, "rb.hub_id", "You hold no role that records a battery level"),
            1,
        ),
    ],
)

# ------------------------------------------------------------------ stock
ROLES = "ARRAY['hub_technician', 'ops_lead']"
WHO = "You hold no hub technician or ops lead role"
VISIT_SAME_HUB = (
    "{indent}IF (SELECT v.hub_id FROM planning_visit v WHERE v.id = p_visit) IS DISTINCT FROM it.hub_id THEN\n"
    "{indent}  PERFORM fleet_fail(format('Visit %s belongs to another hub than %s (%s)',\n"
    "{indent}    (SELECT v.code FROM planning_visit v WHERE v.id = p_visit), it.name, it.code));\n"
    "{indent}END IF;\n"
)
OLD_CREATE_START = (
    "CREATE OR REPLACE FUNCTION hub_create_stock_item(p_actor bigint, p_code text, p_name text, p_kind text,"
)
NEW_CREATE = r"""CREATE OR REPLACE FUNCTION hub_create_stock_item(p_actor bigint, p_code text, p_name text, p_kind text,
                                                 p_unit text, p_hub bigint, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE new_id bigint; unit text := coalesce(nullif(btrim(p_unit), ''), 'pieces'); hb platform_core_hub;
BEGIN
  IF NOT hub_actor_has(p_actor, ARRAY['ops_lead', 'admin']) THEN
    PERFORM hub_deny('Only the ops lead or the workspace admin can add a stock item');
  END IF;
  SELECT * INTO hb FROM platform_core_hub WHERE id = p_hub FOR SHARE;
  IF NOT FOUND THEN PERFORM fleet_fail('Choose the hub that keeps this item'); END IF;
  IF NOT hub_actor_has_at(p_actor, ARRAY['ops_lead', 'admin'], hb.id) THEN
    PERFORM hub_deny(format('Only the ops lead or the workspace admin can add a stock item at hub %s', hb.code));
  END IF;
  IF p_code IS NULL OR p_code !~ '^[A-Z0-9][A-Z0-9-]{0,11}$' THEN
    PERFORM fleet_fail('The item code uses capital letters, digits and dashes, up to 12 characters');
  END IF;
  IF coalesce(btrim(p_name), '') = '' OR length(p_name) > 80 THEN
    PERFORM fleet_fail('Give the item a name (up to 80 characters)');
  END IF;
  IF p_kind IS NULL OR p_kind NOT IN ('reusable', 'consumable') THEN
    PERFORM fleet_fail('Choose whether the item is reusable or consumable');
  END IF;
  IF length(unit) > 20 THEN PERFORM fleet_fail('The unit is up to 20 characters, for example kits'); END IF;
  IF EXISTS (SELECT 1 FROM hub_stock_item WHERE code = p_code AND hub_id = hb.id) THEN
    PERFORM fleet_fail(format('A stock item with code %s already exists at hub %s', p_code, hb.code));
  END IF;
  INSERT INTO hub_stock_item (code, name, kind, unit, hub_id, created_by_id, created_at)
  VALUES (p_code, btrim(p_name), p_kind, unit, hb.id, p_actor, clock_timestamp())
  RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'hub.create_stock_item', 'stock_item', p_code,
                    jsonb_build_object('name', btrim(p_name), 'kind', p_kind, 'unit', unit, 'hub', hb.code),
                    jsonb_build_object('item_id', new_id), p_rid);
  RETURN new_id;
END $fn$;

-- The Phase 02 signature: the item goes to the DEFAULT hub (the only hub; refused as "hub required" when there
-- are several). The role is checked first, so an unknown or forged actor is refused before anything else.
CREATE OR REPLACE FUNCTION hub_create_stock_item(p_actor bigint, p_code text, p_name text, p_kind text,
                                                 p_unit text, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
BEGIN
  IF NOT hub_actor_has(p_actor, ARRAY['ops_lead', 'admin']) THEN
    PERFORM hub_deny('Only the ops lead or the workspace admin can add a stock item');
  END IF;
  RETURN hub_create_stock_item(p_actor, p_code, p_name, p_kind, p_unit, platform_core_default_hub(), p_rid);
END $fn$;
"""


def _swap_create(sql: str) -> str:
    start = sql.index(OLD_CREATE_START)
    end = sql.index("END $fn$;\n", start) + len("END $fn$;\n")
    return sql[:start] + NEW_CREATE + sql[end:]


STOCK = _apply(
    _swap_create(stock.new_sql),
    [
        (  # receive, adjust, return, reserve, and issue / consume (hub_stock_take): the item is the target
            "  it := hub_stock_lock(p_item);\n",
            "  it := hub_stock_lock(p_item);\n" + _deny_at(ROLES, "it.hub_id", WHO),
            5,
        ),
        (  # release
            "  it := hub_stock_lock(r.item_id);\n",
            "  it := hub_stock_lock(r.item_id);\n" + _deny_at(ROLES, "it.hub_id", WHO),
            1,
        ),
        (  # issue / consume for a visit
            "    PERFORM hub_stock_visit(p_visit, true);\n",
            "    PERFORM hub_stock_visit(p_visit, true);\n" + VISIT_SAME_HUB.format(indent="    "),
            1,
        ),
        (  # reserve
            "\n  PERFORM hub_stock_visit(p_visit, true);\n",
            "\n  PERFORM hub_stock_visit(p_visit, true);\n" + VISIT_SAME_HUB.format(indent="  "),
            1,
        ),
        (  # return for a visit
            "    PERFORM fleet_fail('No such visit');\n  END IF;\n",
            "    PERFORM fleet_fail('No such visit');\n  END IF;\n"
            "  IF p_visit IS NOT NULL THEN\n" + VISIT_SAME_HUB.format(indent="    ") + "  END IF;\n",
            1,
        ),
    ],
)

NEW_SIGNATURE = "hub_create_stock_item(bigint, text, text, text, text, bigint, text)"
forward = (
    CHARGING
    + STOCK
    + f"REVOKE ALL ON FUNCTION {NEW_SIGNATURE} FROM PUBLIC;\nGRANT EXECUTE ON FUNCTION {NEW_SIGNATURE} TO {APP};\n"
)
reverse = charging.FUNCTIONS + stock.new_sql + f"DROP FUNCTION IF EXISTS {NEW_SIGNATURE};\n"


class Migration(migrations.Migration):
    dependencies = [
        ("hub", "0010_hub_ownership"),
        ("accounts", "0005_hub_qualified_grants"),
    ]
    operations = [migrations.RunSQL(forward, reverse)]

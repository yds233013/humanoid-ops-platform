"""v2 Phase 03A-1 (docs/13_v2_phase03_contract.md 11.1): hub ownership of work orders, chargers, charging and
stock, and same-hub integrity.

COLUMNS. hub_workorder, hub_charging_session and hub_charger_reservation gain hub_id; hub_charger.hub_id and
hub_stock_item.hub_id become NOT NULL. All are backfilled (chargers and stock items to the existing hub, as the
Phase 02 "first hub" lookups did; sessions and reservations to their charger's hub; work orders to their robot's,
else their visit's, else the existing hub), then NOT NULL with the DB default platform_core_default_hub().
Backfills run with the table's user triggers off (platform_core 0005): recording the owner is not a change.

Stock item codes become unique PER HUB (hub_stock_item_code_per_hub replaces the global unique code).

INTEGRITY (trigger function hub_same_hub_guard, check_violation on refusal):
  * a charger reservation: its charger, robot and visit (if any) belong to the reservation's hub;
  * a charging session: its charger and robot belong to the session's hub;
  * a work order: its robot and visit (if any) belong to the work order's hub.
And, so that these stay true over time (hub_owner_change_guard):
  * a charger cannot change hub while it has an active reservation or an open session;
  * a robot cannot change home hub while it has an active charger reservation, an open charging session or a
    planning reservation that has not ended;
  * a vehicle cannot change hub while it has a planning reservation that has not ended.
(The audited transfer of 03A-2 is the way a robot or vehicle changes hub.)

The trigger functions are not SECURITY DEFINER; EXECUTE is revoked from PUBLIC (a trigger needs no EXECUTE).

Reverse: drops the triggers and functions, restores the global unique stock code and the nullable columns.
"""

import django.db.models.deletion
from django.db import migrations, models

import platform_core.models

BACKFILL = __import__("importlib").import_module("platform_core.migrations.0005_default_hub").backfill_sql


def _from_parent(table: str, expr: str) -> str:
    """Backfill from a parent row (the charger's / robot's hub), triggers off, FK checks immediate."""
    return f"""
DO $bf$ BEGIN
  IF EXISTS (SELECT 1 FROM {table} WHERE hub_id IS NULL) THEN
    SET CONSTRAINTS ALL IMMEDIATE;
    ALTER TABLE {table} DISABLE TRIGGER USER;
    UPDATE {table} t SET hub_id = {expr} WHERE t.hub_id IS NULL;
    ALTER TABLE {table} ENABLE TRIGGER USER;
  END IF;
END $bf$;
"""


SESSION_BACKFILL = _from_parent(
    "hub_charging_session", "(SELECT c.hub_id FROM hub_charger c WHERE c.id = t.charger_id)"
)
RESERVATION_BACKFILL = _from_parent(
    "hub_charger_reservation", "(SELECT c.hub_id FROM hub_charger c WHERE c.id = t.charger_id)"
)
WORKORDER_BACKFILL = _from_parent(
    "hub_workorder",
    "coalesce((SELECT r.hub_id FROM fleet_robot r WHERE r.id = t.robot_id),"
    " (SELECT v.hub_id FROM planning_visit v WHERE v.id = t.visit_id), platform_core_backfill_hub())",
)

GUARDS = r"""
CREATE OR REPLACE FUNCTION hub_same_hub_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE x record; what text;
BEGIN
  what := CASE TG_TABLE_NAME WHEN 'hub_workorder' THEN 'this task'
                             WHEN 'hub_charging_session' THEN 'this charging session'
                             ELSE 'this charger reservation' END;
  IF TG_TABLE_NAME <> 'hub_workorder' THEN
    SELECT c.code, c.hub_id INTO x FROM hub_charger c WHERE c.id = NEW.charger_id;
    IF x.hub_id IS DISTINCT FROM NEW.hub_id THEN
      PERFORM fleet_fail(format('Charger %s belongs to another hub than %s', x.code, what));
    END IF;
  END IF;
  IF NEW.robot_id IS NOT NULL THEN
    SELECT r.code, r.hub_id INTO x FROM fleet_robot r WHERE r.id = NEW.robot_id;
    IF x.hub_id IS DISTINCT FROM NEW.hub_id THEN
      PERFORM fleet_fail(format('Robot %s belongs to another hub than %s', x.code, what));
    END IF;
  END IF;
  IF TG_TABLE_NAME <> 'hub_charging_session' THEN  -- nested: a session row has no visit_id field
    IF NEW.visit_id IS NOT NULL THEN
      SELECT v.code, v.hub_id INTO x FROM planning_visit v WHERE v.id = NEW.visit_id;
      IF x.hub_id IS DISTINCT FROM NEW.hub_id THEN
        PERFORM fleet_fail(format('Visit %s belongs to another hub than %s', x.code, what));
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION hub_same_hub_guard() FROM PUBLIC;
CREATE TRIGGER hub_charger_reservation_same_hub BEFORE INSERT OR UPDATE OF hub_id, charger_id, robot_id, visit_id
  ON hub_charger_reservation FOR EACH ROW EXECUTE FUNCTION hub_same_hub_guard();
CREATE TRIGGER hub_charging_session_same_hub BEFORE INSERT OR UPDATE OF hub_id, charger_id, robot_id
  ON hub_charging_session FOR EACH ROW EXECUTE FUNCTION hub_same_hub_guard();
CREATE TRIGGER hub_workorder_same_hub BEFORE INSERT OR UPDATE OF hub_id, robot_id, visit_id
  ON hub_workorder FOR EACH ROW EXECUTE FUNCTION hub_same_hub_guard();

CREATE OR REPLACE FUNCTION hub_owner_change_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
BEGIN
  IF NEW.hub_id IS NOT DISTINCT FROM OLD.hub_id THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME = 'hub_charger' THEN
    IF EXISTS (SELECT 1 FROM hub_charger_reservation r WHERE r.charger_id = NEW.id AND r.released_at IS NULL
                 AND r.ends_at > clock_timestamp())
       OR EXISTS (SELECT 1 FROM hub_charging_session s WHERE s.charger_id = NEW.id AND s.ended_at IS NULL) THEN
      PERFORM fleet_fail(format('Charger %s has an active reservation or session; it cannot change hub', NEW.code));
    END IF;
  ELSIF TG_TABLE_NAME = 'fleet_robot' THEN
    IF EXISTS (SELECT 1 FROM hub_charger_reservation r WHERE r.robot_id = NEW.id AND r.released_at IS NULL
                 AND r.ends_at > clock_timestamp())
       OR EXISTS (SELECT 1 FROM hub_charging_session s WHERE s.robot_id = NEW.id AND s.ended_at IS NULL)
       OR EXISTS (SELECT 1 FROM planning_reservation pr JOIN planning_resource res ON res.id = pr.resource_id
                  WHERE res.robot_id = NEW.id AND pr.released_at IS NULL AND pr.ends_at > clock_timestamp()) THEN
      PERFORM fleet_fail(format('Robot %s has a booking or charging that has not ended; it cannot change hub',
                                NEW.code));
    END IF;
  ELSE
    IF EXISTS (SELECT 1 FROM planning_reservation pr JOIN planning_resource res ON res.id = pr.resource_id
               WHERE res.vehicle_id = NEW.id AND pr.released_at IS NULL AND pr.ends_at > clock_timestamp()) THEN
      PERFORM fleet_fail(format('Vehicle %s has a booking that has not ended; it cannot change hub', NEW.code));
    END IF;
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION hub_owner_change_guard() FROM PUBLIC;
CREATE TRIGGER hub_charger_owner_change BEFORE UPDATE OF hub_id ON hub_charger
  FOR EACH ROW EXECUTE FUNCTION hub_owner_change_guard();
CREATE TRIGGER robot_owner_change BEFORE UPDATE OF hub_id ON fleet_robot
  FOR EACH ROW EXECUTE FUNCTION hub_owner_change_guard();
CREATE TRIGGER vehicle_owner_change BEFORE UPDATE OF hub_id ON planning_vehicle
  FOR EACH ROW EXECUTE FUNCTION hub_owner_change_guard();
"""
GUARDS_REVERSE = """
DROP TRIGGER IF EXISTS hub_charger_reservation_same_hub ON hub_charger_reservation;
DROP TRIGGER IF EXISTS hub_charging_session_same_hub ON hub_charging_session;
DROP TRIGGER IF EXISTS hub_workorder_same_hub ON hub_workorder;
DROP TRIGGER IF EXISTS hub_charger_owner_change ON hub_charger;
DROP TRIGGER IF EXISTS robot_owner_change ON fleet_robot;
DROP TRIGGER IF EXISTS vehicle_owner_change ON planning_vehicle;
DROP FUNCTION IF EXISTS hub_same_hub_guard();
DROP FUNCTION IF EXISTS hub_owner_change_guard();
"""


def _nullable() -> models.ForeignKey:  # type: ignore[type-arg]
    return models.ForeignKey(
        null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="platform_core.hub"
    )


def _final() -> models.ForeignKey:  # type: ignore[type-arg]
    return models.ForeignKey(
        db_default=platform_core.models.DefaultHub(),
        on_delete=django.db.models.deletion.PROTECT,
        related_name="+",
        to="platform_core.hub",
    )


NEW_COLUMNS = ("workorder", "chargingsession", "chargerreservation")
ALL = ("charger", "stockitem", *NEW_COLUMNS)


class Migration(migrations.Migration):
    dependencies = [
        ("hub", "0009_stock_visit_and_units"),
        ("hub", "0006_maintenance_tasks"),
        ("platform_core", "0005_default_hub"),
        ("fleet", "0012_hub_ownership_and_hub_settings"),
        ("planning", "0010_hub_ownership"),
    ]

    operations = [
        *[migrations.AddField(model_name=m, name="hub", field=_nullable()) for m in NEW_COLUMNS],
        migrations.RunSQL(BACKFILL("hub_charger"), migrations.RunSQL.noop),
        migrations.RunSQL(BACKFILL("hub_stock_item"), migrations.RunSQL.noop),
        migrations.RunSQL(SESSION_BACKFILL, migrations.RunSQL.noop),
        migrations.RunSQL(RESERVATION_BACKFILL, migrations.RunSQL.noop),
        migrations.RunSQL(WORKORDER_BACKFILL, migrations.RunSQL.noop),
        *[migrations.AlterField(model_name=m, name="hub", field=_final()) for m in ALL],
        migrations.AlterField(model_name="stockitem", name="code", field=models.CharField(max_length=12)),
        migrations.AddConstraint(
            model_name="stockitem",
            constraint=models.UniqueConstraint(fields=("hub", "code"), name="hub_stock_item_code_per_hub"),
        ),
        migrations.RunSQL(GUARDS, GUARDS_REVERSE),
    ]

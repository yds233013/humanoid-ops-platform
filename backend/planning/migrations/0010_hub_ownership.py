"""v2 Phase 03A-1 (docs/13_v2_phase03_contract.md 11.1): vehicles, people (home hub) and visits (dispatch hub)
belong to one hub.

Each hub_id is added nullable, backfilled to the existing hub (platform_core 0005 backfill_sql: triggers off for
the one UPDATE, so no person or visit version is bumped and a cancelled visit is not refused), then NOT NULL with
the DB default platform_core_default_hub().

INTEGRITY (trigger planning_visit_hub_guard): a visit's robot and vehicle must belong to the visit's hub.
  * planning_visitrevision BEFORE INSERT: the revision's robot and vehicle have the visit's hub;
  * planning_visit BEFORE UPDATE OF hub_id: the current revision's robot and vehicle have the new hub.
In 03A-1 "dispatchable from the hub" is the home hub; 03A-2 adds the current location (contract 11.4).
A refusal is check_violation (fleet_fail), so run_command reports it as a refusal and nothing is written.

Reverse: drops the triggers, the function and the columns.
"""

import django.db.models.deletion
from django.db import migrations, models

import platform_core.models

BACKFILL = __import__("importlib").import_module("platform_core.migrations.0005_default_hub").backfill_sql

GUARD = r"""
CREATE OR REPLACE FUNCTION planning_visit_hub_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE v_hub bigint; v_code text; rb record; vh record; rev planning_visitrevision;
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
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION planning_visit_hub_guard() FROM PUBLIC;
CREATE TRIGGER revision_hub_guard BEFORE INSERT ON planning_visitrevision
  FOR EACH ROW EXECUTE FUNCTION planning_visit_hub_guard();
CREATE TRIGGER visit_hub_guard BEFORE UPDATE OF hub_id ON planning_visit
  FOR EACH ROW EXECUTE FUNCTION planning_visit_hub_guard();
"""
GUARD_REVERSE = """
DROP TRIGGER IF EXISTS revision_hub_guard ON planning_visitrevision;
DROP TRIGGER IF EXISTS visit_hub_guard ON planning_visit;
DROP FUNCTION IF EXISTS planning_visit_hub_guard();
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


TABLES = {"person": "planning_person", "vehicle": "planning_vehicle", "visit": "planning_visit"}


class Migration(migrations.Migration):
    dependencies = [
        ("planning", "0009_maintenance_gate_settings_lock"),
        ("platform_core", "0005_default_hub"),
        ("fleet", "0012_hub_ownership_and_hub_settings"),
    ]

    operations = [
        *[migrations.AddField(model_name=m, name="hub", field=_nullable()) for m in TABLES],
        *[migrations.RunSQL(BACKFILL(t), migrations.RunSQL.noop) for t in TABLES.values()],
        *[migrations.AlterField(model_name=m, name="hub", field=_final()) for m in TABLES],
        migrations.RunSQL(GUARD, GUARD_REVERSE),
    ]

"""v2 Phase 03A-2 (docs/13_v2_phase03_contract.md 11.4, correction 10.A): hub-end custody events name the hub.

execution_custodyevent.hub_id: the hub at the hub end of the move (from_kind = hub or to_kind = hub); NULL for a
move with no hub end (check constraint custody_hub_only_at_hub_end). The trigger below always sets it at a hub
end; the constraint does not require it, so a history written by the schema owner with triggers disabled (test
fixtures of broken histories) still loads, and fleet_robot_location then falls back to the home hub.

  * Backfill: every existing hub-end event gets the robot's HOME hub (in a Phase 02 database there is one hub,
    so this is where every such move happened). The table is append-only (reject_mutation), so the owner disables
    its user triggers around the one UPDATE, in the migration's transaction (platform_core 0005 backfill_sql
    pattern): recording which hub already held the robot is not a change of the custody chain.
  * New events: the BEFORE INSERT trigger execution_custody_hub sets hub_id itself, whatever the caller sent
    (as the guard sets recorded_at): the run's visit hub (the hub the robot departs from or returns to; the
    departure gate of 03A-2 proves the robot was verified at that hub), or, for a recovery without a run, the
    robot's home hub. It sorts after execution_custody_guard, so every custody rule runs first, unchanged.

Not SECURITY DEFINER; EXECUTE revoked from PUBLIC (a trigger needs none). Reverse drops the trigger, the function,
the constraint and the column.
"""

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q

BACKFILL = """
DO $bf$ BEGIN
  IF EXISTS (SELECT 1 FROM execution_custodyevent WHERE hub_id IS NULL AND 'hub' IN (from_kind, to_kind)) THEN
    SET CONSTRAINTS ALL IMMEDIATE;
    ALTER TABLE execution_custodyevent DISABLE TRIGGER USER;
    UPDATE execution_custodyevent e SET hub_id = (SELECT r.hub_id FROM fleet_robot r WHERE r.id = e.robot_id)
    WHERE e.hub_id IS NULL AND 'hub' IN (e.from_kind, e.to_kind);
    ALTER TABLE execution_custodyevent ENABLE TRIGGER USER;
  END IF;
END $bf$;
"""

TRIGGER = r"""
CREATE OR REPLACE FUNCTION execution_custody_hub() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
BEGIN
  IF 'hub' IN (NEW.from_kind, NEW.to_kind) THEN
    IF NEW.run_id IS NOT NULL THEN
      NEW.hub_id := (SELECT v.hub_id FROM execution_executionrun x JOIN planning_visit v ON v.id = x.visit_id
                     WHERE x.id = NEW.run_id);
    ELSE
      NEW.hub_id := (SELECT r.hub_id FROM fleet_robot r WHERE r.id = NEW.robot_id);
    END IF;
  ELSE
    NEW.hub_id := NULL;
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION execution_custody_hub() FROM PUBLIC;
CREATE TRIGGER execution_custody_hub BEFORE INSERT ON execution_custodyevent
  FOR EACH ROW EXECUTE FUNCTION execution_custody_hub();
"""
TRIGGER_REVERSE = """
DROP TRIGGER IF EXISTS execution_custody_hub ON execution_custodyevent;
DROP FUNCTION IF EXISTS execution_custody_hub();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("execution", "0005_s7_incident_release_and_deadlines"),
        ("fleet", "0013_maintenance_record_hub_authority"),
        ("planning", "0010_hub_ownership"),
        ("platform_core", "0005_default_hub"),
    ]

    operations = [
        migrations.AddField(
            model_name="custodyevent",
            name="hub",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="platform_core.hub",
            ),
        ),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
        migrations.AddConstraint(
            model_name="custodyevent",
            constraint=models.CheckConstraint(
                condition=Q(from_kind="hub") | Q(to_kind="hub") | Q(hub__isnull=True),
                name="custody_hub_only_at_hub_end",
            ),
        ),
        migrations.RunSQL(TRIGGER, TRIGGER_REVERSE),
    ]

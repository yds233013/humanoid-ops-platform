"""v2 Phase 03A closure (docs/13_v2_phase03_contract.md I-8, section 12 "no direct-write bypass"): a charger never
changes hub.

hub/0010 only refused a charger's hub change while it had an active reservation or charging session, so the
runtime role could move an idle charger to another hub with a plain UPDATE (no command, no audit), handing it to
that hub's technicians. Chargers have no transfer flow in 03A, and no code changes a charger's hub (it is set once
by hub.charging.create_charger). So any change of hub_charger.hub_id is now refused, for every role, with
insufficient_privilege: register the charger at the new hub instead. An UPDATE that rewrites the same value (an
ORM save of every column) passes.

The trigger name sorts after hub/0010's hub_charger_owner_change, so that trigger's wording ("has an active
reservation or session") still comes first when it applies. Reverse drops the trigger and its function.
"""

from django.db import migrations

SQL = """
CREATE OR REPLACE FUNCTION hub_charger_hub_fixed() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
BEGIN
  IF NEW.hub_id IS DISTINCT FROM OLD.hub_id THEN
    RAISE EXCEPTION 'Charger % cannot change hub; register it at the new hub', NEW.code
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION hub_charger_hub_fixed() FROM PUBLIC;
CREATE TRIGGER hub_charger_owner_change_fixed BEFORE UPDATE OF hub_id ON hub_charger
  FOR EACH ROW EXECUTE FUNCTION hub_charger_hub_fixed();
"""
REVERSE = """
DROP TRIGGER IF EXISTS hub_charger_owner_change_fixed ON hub_charger;
DROP FUNCTION IF EXISTS hub_charger_hub_fixed();
"""


class Migration(migrations.Migration):
    dependencies = [("hub", "0013_stock_transfer_sql")]

    operations = [migrations.RunSQL(SQL, REVERSE)]

"""v2 Phase 03A-2 (docs/13_v2_phase03_contract.md 10.A, 11.5): a robot's or a vehicle's home hub changes only
through a received transfer.

03A-1 left fleet_robot.hub_id and planning_vehicle.hub_id writable by the runtime role (it UPDATEs robots and
vehicles for other reasons), guarded only against unfinished bookings (hub/0010). A direct UPDATE as ops_app could
therefore move a robot to another hub with no transfer, no receipt and no audit, giving it a second dispatchable
location. Now a BEFORE UPDATE OF hub_id trigger refuses (insufficient_privilege) any change made with the runtime
role as the current user. fleet_transfer_receive (SECURITY DEFINER, so the current user is the owner) is the only
runtime path; the schema owner (migrations, provisioning) stays trusted, as for every other owner boundary.

The triggers are named after the hub/0010 owner-change triggers so those run first (their refusals keep their
wording). Reverse drops them.
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE

SQL = f"""
CREATE OR REPLACE FUNCTION fleet_home_hub_by_transfer_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
BEGIN
  IF NEW.hub_id IS DISTINCT FROM OLD.hub_id AND current_user = '{APP}' THEN
    RAISE EXCEPTION '% % changes hub only by a transfer received at the other hub',
      CASE TG_TABLE_NAME WHEN 'fleet_robot' THEN 'Robot' ELSE 'Vehicle' END, NEW.code
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION fleet_home_hub_by_transfer_only() FROM PUBLIC;
CREATE TRIGGER robot_owner_change_transfer_only BEFORE UPDATE OF hub_id ON fleet_robot
  FOR EACH ROW EXECUTE FUNCTION fleet_home_hub_by_transfer_only();
CREATE TRIGGER vehicle_owner_change_transfer_only BEFORE UPDATE OF hub_id ON planning_vehicle
  FOR EACH ROW EXECUTE FUNCTION fleet_home_hub_by_transfer_only();
"""
REVERSE = """
DROP TRIGGER IF EXISTS robot_owner_change_transfer_only ON fleet_robot;
DROP TRIGGER IF EXISTS vehicle_owner_change_transfer_only ON planning_vehicle;
DROP FUNCTION IF EXISTS fleet_home_hub_by_transfer_only();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("fleet", "0015_location_and_transfer_sql"),
        ("hub", "0011_hub_authority"),
    ]
    operations = [migrations.RunSQL(SQL, REVERSE)]

"""v2 Phase 03A-2 (docs/13_v2_phase03_contract.md 11.4, corrections 10.A and 10.C): the planning location gate in
the database.

A robot can be planned for a visit only when its location (fleet_robot_location, fleet/0015) is hub(H), verified,
where H is the visit's hub. A robot in transit, in a vehicle, at a site, unaccounted or unknown is refused at either
hub. A vehicle in an open transfer (in transit, receipt refused or abandoned and not received back) is refused too.
The same rule as the planner (planning.services.evaluate), for direct SQL as the runtime role:

  planning_visitrevision  BEFORE INSERT  a revision that is not 'blocked' (a blocked revision records why a visit
                                         cannot be planned);
  planning_reservation    BEFORE INSERT  a robot or vehicle reservation (of the revision's visit hub);
  planning_visit          BEFORE UPDATE  requested -> planned, or any -> confirmed. Downgrades and cancelling are
                                         never refused (safe direction).

Each check first takes the robot (then the vehicle) row FOR SHARE and reads the location in a NEW statement (the
function is VOLATILE): a transfer start holds the robot FOR UPDATE, so a booking and a transfer are totally
ordered. Transfer first: the booking waits, then sees "in transfer" and is refused. Booking first: the transfer
waits, then sees the booking and is refused with it listed (fleet_transfer_blockers).

Existing bookings are never re-planned silently. Refusals are check_violation (fleet_fail). Not SECURITY DEFINER;
EXECUTE revoked from PUBLIC (planning_location_problem: granted to ops_app, whose
statements fire these triggers). Reverse drops the triggers and functions.
"""

from django.conf import settings
from django.db import migrations

SQL = r"""
CREATE OR REPLACE FUNCTION planning_location_problem(p_robot bigint, p_vehicle bigint, p_hub bigint) RETURNS text
LANGUAGE plpgsql VOLATILE SET search_path = public, pg_temp AS $fn$
DECLARE loc record; t fleet_hub_transfer; code text;
BEGIN
  IF p_robot IS NOT NULL THEN
    SELECT r.code INTO code FROM fleet_robot r WHERE r.id = p_robot FOR SHARE;
    SELECT * INTO loc FROM fleet_robot_location(p_robot);
    IF loc.kind IS DISTINCT FROM 'hub' OR NOT loc.verified THEN
      RETURN format('robot %s: %s', code, fleet_location_problem(loc.kind, loc.ref, loc.verified));
    END IF;
    IF loc.hub_id IS DISTINCT FROM p_hub THEN
      RETURN format('robot %s is at hub %s, not at hub %s of this visit', code, loc.ref, fleet_hub_code(p_hub));
    END IF;
  END IF;
  IF p_vehicle IS NOT NULL THEN
    SELECT v.code INTO code FROM planning_vehicle v WHERE v.id = p_vehicle FOR SHARE;
    t := fleet_asset_last_transfer_step('vehicle', p_vehicle);
    IF t.id IS NOT NULL AND t.state <> 'received' THEN
      RETURN format('vehicle %s: In transfer %s', code, fleet_transfer_text(t));
    END IF;
  END IF;
  RETURN NULL;
END $fn$;
REVOKE ALL ON FUNCTION planning_location_problem(bigint, bigint, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION planning_location_problem(bigint, bigint, bigint) TO {app};

CREATE OR REPLACE FUNCTION planning_revision_location_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE problem text;
BEGIN
  IF NEW.feasibility IS DISTINCT FROM 'blocked' AND (NEW.robot_id IS NOT NULL OR NEW.vehicle_id IS NOT NULL) THEN
    problem := planning_location_problem(NEW.robot_id, NEW.vehicle_id,
                                         (SELECT v.hub_id FROM planning_visit v WHERE v.id = NEW.visit_id));
    IF problem IS NOT NULL THEN PERFORM fleet_fail('plan refused: ' || problem); END IF;
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION planning_revision_location_guard() FROM PUBLIC;
CREATE TRIGGER revision_location_guard BEFORE INSERT ON planning_visitrevision
  FOR EACH ROW EXECUTE FUNCTION planning_revision_location_guard();

CREATE OR REPLACE FUNCTION planning_reservation_location_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE res planning_resource; problem text;
BEGIN
  SELECT * INTO res FROM planning_resource WHERE id = NEW.resource_id;
  IF res.kind NOT IN ('robot', 'vehicle') THEN RETURN NEW; END IF;
  problem := planning_location_problem(res.robot_id, res.vehicle_id,
    (SELECT v.hub_id FROM planning_visitrevision vr JOIN planning_visit v ON v.id = vr.visit_id
     WHERE vr.id = NEW.revision_id));
  IF problem IS NOT NULL THEN PERFORM fleet_fail('reservation refused: ' || problem); END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION planning_reservation_location_guard() FROM PUBLIC;
CREATE TRIGGER reservation_location_guard BEFORE INSERT ON planning_reservation
  FOR EACH ROW EXECUTE FUNCTION planning_reservation_location_guard();

CREATE OR REPLACE FUNCTION planning_visit_location_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE rev planning_visitrevision; problem text;
BEGIN
  IF (OLD.status = 'requested' AND NEW.status = 'planned')
     OR (NEW.status = 'confirmed' AND OLD.status IS DISTINCT FROM 'confirmed') THEN
    SELECT * INTO rev FROM planning_visitrevision WHERE id = NEW.current_revision_id;
    problem := planning_location_problem(rev.robot_id, rev.vehicle_id, NEW.hub_id);
    IF problem IS NOT NULL THEN
      PERFORM fleet_fail(format('visit %s cannot become %s: %s', NEW.code, NEW.status, problem));
    END IF;
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION planning_visit_location_guard() FROM PUBLIC;
CREATE TRIGGER visit_location_guard BEFORE UPDATE ON planning_visit
  FOR EACH ROW EXECUTE FUNCTION planning_visit_location_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS revision_location_guard ON planning_visitrevision;
DROP TRIGGER IF EXISTS reservation_location_guard ON planning_reservation;
DROP TRIGGER IF EXISTS visit_location_guard ON planning_visit;
DROP FUNCTION IF EXISTS planning_revision_location_guard();
DROP FUNCTION IF EXISTS planning_reservation_location_guard();
DROP FUNCTION IF EXISTS planning_visit_location_guard();
DROP FUNCTION IF EXISTS planning_location_problem(bigint, bigint, bigint);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("planning", "0010_hub_ownership"),
        ("fleet", "0015_location_and_transfer_sql"),
    ]
    operations = [migrations.RunSQL(SQL.replace("{app}", settings.DB_APP_ROLE), REVERSE)]

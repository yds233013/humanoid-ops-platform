"""v2 02C (docs/11_v2_phase02_contract.md 3.2, 8.2 "Gates"): the planning maintenance gate in the database.

The plan command (planning.services.plan_visit) already refuses a maintenance-blocked robot through
fleet.services.evaluate_eligibility, which reads fleet_maintenance_block_reasons (the same SQL the eligibility view
and the authorization function use). These triggers make the same rule hold for direct SQL as the runtime role,
while the owner-provisioned gate is active (fleet_maintenance_gate_active(); inactive = nothing is blocked):

  planning_visitrevision  BEFORE INSERT  a revision that is not 'blocked' cannot name a robot with blocking
                                         maintenance (a 'blocked' revision may: the planner records why a visit
                                         cannot be planned);
  planning_reservation    BEFORE INSERT  no new reservation of a robot resource with blocking maintenance;
  planning_visit          BEFORE UPDATE  requested -> planned, or any -> confirmed, is refused when the current
                                         revision's robot has blocking maintenance. Downgrades and cancelling are
                                         never refused (safe direction).

Existing bookings are never re-planned silently: their rows stay as they are, live feasibility shows them blocked
with the reason, and confirming or authorizing them is refused.

The mode and the gate setting are read without asserting the settings lock (these triggers fire on every
reservation; requiring the lock would refuse every caller that does not take it). The plan command takes the
settings lock SHARED, so it is totally ordered with an activation; a direct-SQL write racing an activation can at
worst produce a plan that the authorization function (which holds the lock) still refuses.
"""

from django.db import migrations

SQL = r"""
CREATE OR REPLACE FUNCTION planning_maintenance_problem(p_robot bigint) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE reasons text[];
BEGIN
  IF p_robot IS NULL OR NOT fleet_maintenance_gate_active() THEN RETURN NULL; END IF;
  reasons := fleet_maintenance_block_reasons(p_robot, fleet_production_mode(), clock_timestamp());
  IF cardinality(reasons) = 0 THEN RETURN NULL; END IF;
  RETURN format('robot %s: %s', (SELECT code FROM fleet_robot WHERE id = p_robot), array_to_string(reasons, '; '));
END $$;

CREATE OR REPLACE FUNCTION planning_revision_maintenance_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE problem text;
BEGIN
  IF NEW.feasibility IS DISTINCT FROM 'blocked' THEN
    problem := planning_maintenance_problem(NEW.robot_id);
    IF problem IS NOT NULL THEN PERFORM fleet_fail('plan refused: maintenance not up to date for ' || problem); END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER revision_maintenance_guard BEFORE INSERT ON planning_visitrevision
  FOR EACH ROW EXECUTE FUNCTION planning_revision_maintenance_guard();

CREATE OR REPLACE FUNCTION planning_reservation_maintenance_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE problem text;
BEGIN
  problem := planning_maintenance_problem(
    (SELECT robot_id FROM planning_resource WHERE id = NEW.resource_id AND kind = 'robot'));
  IF problem IS NOT NULL THEN PERFORM fleet_fail('reservation refused: maintenance not up to date for ' || problem); END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER reservation_maintenance_guard BEFORE INSERT ON planning_reservation
  FOR EACH ROW EXECUTE FUNCTION planning_reservation_maintenance_guard();

CREATE OR REPLACE FUNCTION planning_visit_maintenance_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE problem text;
BEGIN
  IF (OLD.status = 'requested' AND NEW.status = 'planned')
     OR (NEW.status = 'confirmed' AND OLD.status IS DISTINCT FROM 'confirmed') THEN
    problem := planning_maintenance_problem(
      (SELECT robot_id FROM planning_visitrevision WHERE id = NEW.current_revision_id));
    IF problem IS NOT NULL THEN
      PERFORM fleet_fail(format('visit %s cannot become %s: maintenance not up to date for %s', NEW.code,
                                NEW.status, problem));
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER visit_maintenance_guard BEFORE UPDATE ON planning_visit
  FOR EACH ROW EXECUTE FUNCTION planning_visit_maintenance_guard();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS revision_maintenance_guard ON planning_visitrevision;
DROP TRIGGER IF EXISTS reservation_maintenance_guard ON planning_reservation;
DROP TRIGGER IF EXISTS visit_maintenance_guard ON planning_visit;
DROP FUNCTION IF EXISTS planning_revision_maintenance_guard();
DROP FUNCTION IF EXISTS planning_reservation_maintenance_guard();
DROP FUNCTION IF EXISTS planning_visit_maintenance_guard();
DROP FUNCTION IF EXISTS planning_maintenance_problem(bigint);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("planning", "0007_maintenance"),
        ("fleet", "0010_maintenance_sql"),
    ]

    operations = [migrations.RunSQL(SQL, reverse_sql=REVERSE)]

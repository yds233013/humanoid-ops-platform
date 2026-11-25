"""v2 Phase 03A-2 (docs/13_v2_phase03_contract.md 11.4, corrections 10.A and 10.C): the departure custody gate
reads the robot's LOCATION and the visit's hub.

execution_robot_departure_problem(robot, hub) (new): NULL when the robot may be authorized / depart from ``hub``:
no open run, and fleet_robot_location (fleet/0015) is hub(``hub``), verified. Otherwise the reason:
  * an open run (unchanged wording);
  * in transfer: "robot R is in transfer TR-0001 from HUB-1 to HB" (a transfer is never a hub, so a robot in
    transit departs from neither hub);
  * a custody kind other than a verified hub: "robot R is not at hub (custody: site, unverified)" (unchanged
    wording, from execution_current_custody);
  * a verified hub that is not the visit's: "robot R is at hub HB, not at hub HUB-1 of this visit".
execution_robot_departure_problem(robot) keeps its signature for the readiness evaluator and the pages: the same
checks at the robot's current hub (any hub).

The C3 triggers execution_authorization_hub_guard and execution_departure_hub_guard now pass the visit's hub
(read from the visit row, never from the caller). Their locks and positions are unchanged.

Reverse restores the 0002 bodies.
"""

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION execution_robot_departure_problem(p_robot bigint, p_hub bigint) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE rid bigint; c record; loc record; rcode text; t fleet_hub_transfer;
BEGIN
  rcode := (SELECT code FROM fleet_robot WHERE id = p_robot);
  rid := execution_open_run_id(p_robot);
  IF rid IS NOT NULL THEN
    RETURN format('robot %s has an open run (%s); it must be reconciled and its trip closed first', rcode,
                  (SELECT code FROM execution_executionrun WHERE id = rid));
  END IF;
  SELECT * INTO loc FROM fleet_robot_location(p_robot);
  IF loc.kind = 'transfer_in_transit' THEN
    RETURN format('robot %s is in transfer %s', rcode, loc.ref);
  END IF;
  SELECT * INTO c FROM execution_current_custody(p_robot);
  IF c.kind IS DISTINCT FROM 'hub' OR NOT c.verified OR loc.kind IS DISTINCT FROM 'hub' OR NOT loc.verified THEN
    RETURN format('robot %s is not at hub (custody: %s%s)', rcode, c.kind,
                  CASE WHEN c.verified THEN '' ELSE ', unverified' END);
  END IF;
  IF p_hub IS NOT NULL AND loc.hub_id IS DISTINCT FROM p_hub THEN
    RETURN format('robot %s is at hub %s, not at hub %s of this visit', rcode, loc.ref, fleet_hub_code(p_hub));
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION execution_robot_departure_problem(p_robot bigint) RETURNS text
LANGUAGE sql STABLE AS $$ SELECT execution_robot_departure_problem(p_robot, NULL::bigint) $$;

CREATE OR REPLACE FUNCTION execution_authorization_hub_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE problem text;
BEGIN
  problem := execution_robot_departure_problem(NEW.robot_id,
                                               (SELECT v.hub_id FROM planning_visit v WHERE v.id = NEW.visit_id));
  IF problem IS NOT NULL THEN PERFORM fleet_fail('authorization refused: ' || problem); END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION execution_departure_hub_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a readiness_departureauthorization; problem text;
BEGIN
  IF EXISTS (SELECT 1 FROM readiness_departureevent WHERE visit_id = NEW.visit_id) THEN
    RETURN NEW;  -- a second departure of the visit: the unique index refuses it
  END IF;
  SELECT * INTO a FROM readiness_departureauthorization WHERE id = NEW.authorization_id;
  -- record_departure's lock order: site S -> pattern S -> robot U -> persons S -> vehicle S -> visit S -> trip U
  PERFORM 1 FROM planning_site WHERE id = (SELECT site_id FROM planning_visit WHERE id = a.visit_id) FOR SHARE;
  PERFORM 1 FROM fleet_manifestpattern
    WHERE id = (SELECT pattern_id FROM fleet_configurationmanifest WHERE id = a.manifest_id) FOR SHARE;
  PERFORM 1 FROM fleet_robot WHERE id = a.robot_id FOR UPDATE;
  PERFORM readiness_lock_snapshot_guards(a.guard_snapshot, a.visit_id, a.manifest_id, a.robot_id);
  PERFORM 1 FROM planning_visit WHERE id = a.visit_id FOR SHARE;
  PERFORM 1 FROM planning_vehicletrip WHERE revision_id = a.revision_id FOR UPDATE;
  problem := execution_robot_departure_problem(a.robot_id,
                                               (SELECT v.hub_id FROM planning_visit v WHERE v.id = a.visit_id));
  IF problem IS NOT NULL THEN PERFORM fleet_fail('departure refused: ' || problem); END IF;
  RETURN NEW;
END $$;
"""

REVERSE = r"""
CREATE OR REPLACE FUNCTION execution_robot_departure_problem(p_robot bigint) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE rid bigint; c record; rcode text;
BEGIN
  rcode := (SELECT code FROM fleet_robot WHERE id = p_robot);
  rid := execution_open_run_id(p_robot);
  IF rid IS NOT NULL THEN
    RETURN format('robot %s has an open run (%s); it must be reconciled and its trip closed first', rcode,
                  (SELECT code FROM execution_executionrun WHERE id = rid));
  END IF;
  SELECT * INTO c FROM execution_current_custody(p_robot);
  IF c.kind IS DISTINCT FROM 'hub' OR NOT c.verified THEN
    RETURN format('robot %s is not at hub (custody: %s%s)', rcode, c.kind,
                  CASE WHEN c.verified THEN '' ELSE ', unverified' END);
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION execution_authorization_hub_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE problem text;
BEGIN
  problem := execution_robot_departure_problem(NEW.robot_id);
  IF problem IS NOT NULL THEN PERFORM fleet_fail('authorization refused: ' || problem); END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION execution_departure_hub_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a readiness_departureauthorization; problem text;
BEGIN
  IF EXISTS (SELECT 1 FROM readiness_departureevent WHERE visit_id = NEW.visit_id) THEN
    RETURN NEW;  -- a second departure of the visit: the unique index refuses it
  END IF;
  SELECT * INTO a FROM readiness_departureauthorization WHERE id = NEW.authorization_id;
  -- record_departure's lock order: site S -> pattern S -> robot U -> persons S -> vehicle S -> visit S -> trip U
  PERFORM 1 FROM planning_site WHERE id = (SELECT site_id FROM planning_visit WHERE id = a.visit_id) FOR SHARE;
  PERFORM 1 FROM fleet_manifestpattern
    WHERE id = (SELECT pattern_id FROM fleet_configurationmanifest WHERE id = a.manifest_id) FOR SHARE;
  PERFORM 1 FROM fleet_robot WHERE id = a.robot_id FOR UPDATE;
  PERFORM readiness_lock_snapshot_guards(a.guard_snapshot, a.visit_id, a.manifest_id, a.robot_id);
  PERFORM 1 FROM planning_visit WHERE id = a.visit_id FOR SHARE;
  PERFORM 1 FROM planning_vehicletrip WHERE revision_id = a.revision_id FOR UPDATE;
  problem := execution_robot_departure_problem(a.robot_id);
  IF problem IS NOT NULL THEN PERFORM fleet_fail('departure refused: ' || problem); END IF;
  RETURN NEW;
END $$;

DROP FUNCTION IF EXISTS execution_robot_departure_problem(bigint, bigint);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("execution", "0006_custody_hub"),
        ("fleet", "0015_location_and_transfer_sql"),
    ]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]

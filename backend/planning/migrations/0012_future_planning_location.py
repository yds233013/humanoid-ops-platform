"""v2 Phase 03A-2 corrective (docs/13_v2_phase03_contract.md section 12): the planning triggers apply the
FUTURE-PLANNING location predicate (fleet_planning_location_problem, fleet/0017) instead of the physical-dispatch
one.

planning_location_problem(robot, vehicle, hub, from, to) (new, 5 arguments): the robot row FOR SHARE, then (a new
statement) fleet_planning_location_problem(robot, hub, from, to); the vehicle row FOR SHARE, then its open transfer
(unchanged). The window [from, to) of each trigger:

  planning_visitrevision  BEFORE INSERT  [on_site_start - travel out, on_site_end + travel back) of the revision
                                         (not 'blocked' revisions, as before);
  planning_reservation    BEFORE INSERT  the reservation's own [starts_at, ends_at);
  planning_visit          BEFORE UPDATE  requested -> planned, or -> confirmed: the span of the current revision's
                                         active robot reservations, else the revision window.

So a robot verified at the visit's hub is planned as before; a robot away on an open run of the SAME hub is
planned only outside that run's reserved window (At risk, shown by the planner); an overdue or indeterminate
return, another hub, a transfer, unaccounted, unknown or an unverified hub are refused. Authorization and
departure keep the strict physical predicate (execution/0007), unchanged.

The 3-argument function of planning/0011 stays (strict, no window) for reverse compatibility; nothing calls it
any more. Not SECURITY DEFINER; EXECUTE revoked from PUBLIC and granted to ops_app. Reverse restores the
planning/0011 trigger bodies and drops the 5-argument function.
"""

import importlib

from django.conf import settings
from django.db import migrations

_P11 = importlib.import_module("planning.migrations.0011_location_gate")

FUNC = r"""
CREATE OR REPLACE FUNCTION planning_location_problem(p_robot bigint, p_vehicle bigint, p_hub bigint,
                                                     p_from timestamptz, p_to timestamptz) RETURNS text
LANGUAGE plpgsql VOLATILE SET search_path = public, pg_temp AS $fn$
DECLARE t fleet_hub_transfer; code text; problem text;
BEGIN
  IF p_robot IS NOT NULL THEN
    PERFORM 1 FROM fleet_robot r WHERE r.id = p_robot FOR SHARE;
    problem := fleet_planning_location_problem(p_robot, p_hub, p_from, p_to);
    IF problem IS NOT NULL THEN RETURN problem; END IF;
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
REVOKE ALL ON FUNCTION planning_location_problem(bigint, bigint, bigint, timestamptz, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION planning_location_problem(bigint, bigint, bigint, timestamptz, timestamptz) TO {app};

CREATE OR REPLACE FUNCTION planning_revision_location_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE problem text;
BEGIN
  IF NEW.feasibility IS DISTINCT FROM 'blocked' AND (NEW.robot_id IS NOT NULL OR NEW.vehicle_id IS NOT NULL) THEN
    problem := planning_location_problem(NEW.robot_id, NEW.vehicle_id,
                 (SELECT v.hub_id FROM planning_visit v WHERE v.id = NEW.visit_id),
                 NEW.on_site_start - make_interval(mins => coalesce(NEW.travel_out_min, 0)),
                 NEW.on_site_end + make_interval(mins => coalesce(NEW.travel_back_min, 0)));
    IF problem IS NOT NULL THEN PERFORM fleet_fail('plan refused: ' || problem); END IF;
  END IF;
  RETURN NEW;
END $fn$;

CREATE OR REPLACE FUNCTION planning_reservation_location_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE res planning_resource; problem text;
BEGIN
  SELECT * INTO res FROM planning_resource WHERE id = NEW.resource_id;
  IF res.kind NOT IN ('robot', 'vehicle') THEN RETURN NEW; END IF;
  problem := planning_location_problem(res.robot_id, res.vehicle_id,
    (SELECT v.hub_id FROM planning_visitrevision vr JOIN planning_visit v ON v.id = vr.visit_id
     WHERE vr.id = NEW.revision_id),
    NEW.starts_at, NEW.ends_at);
  IF problem IS NOT NULL THEN PERFORM fleet_fail('reservation refused: ' || problem); END IF;
  RETURN NEW;
END $fn$;

CREATE OR REPLACE FUNCTION planning_visit_location_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE rev planning_visitrevision; problem text; lo timestamptz; hi timestamptz;
BEGIN
  IF (OLD.status = 'requested' AND NEW.status = 'planned')
     OR (NEW.status = 'confirmed' AND OLD.status IS DISTINCT FROM 'confirmed') THEN
    SELECT * INTO rev FROM planning_visitrevision WHERE id = NEW.current_revision_id;
    SELECT min(pr.starts_at), max(pr.ends_at) INTO lo, hi
    FROM planning_reservation pr JOIN planning_resource res ON res.id = pr.resource_id
    WHERE pr.revision_id = rev.id AND res.robot_id = rev.robot_id AND pr.released_at IS NULL;
    problem := planning_location_problem(rev.robot_id, rev.vehicle_id, NEW.hub_id,
      coalesce(lo, rev.on_site_start - make_interval(mins => coalesce(rev.travel_out_min, 0))),
      coalesce(hi, rev.on_site_end + make_interval(mins => coalesce(rev.travel_back_min, 0))));
    IF problem IS NOT NULL THEN
      PERFORM fleet_fail(format('visit %s cannot become %s: %s', NEW.code, NEW.status, problem));
    END IF;
  END IF;
  RETURN NEW;
END $fn$;
"""


def _bodies(sql: str) -> str:
    """The three trigger function bodies of planning/0011 (for reverse), without CREATE TRIGGER."""
    out = []
    for name in (
        "planning_revision_location_guard",
        "planning_reservation_location_guard",
        "planning_visit_location_guard",
    ):
        start = sql.index(f"CREATE OR REPLACE FUNCTION {name}()")
        end = sql.index("END $fn$;", start) + len("END $fn$;")
        out.append(sql[start:end])
    return "\n".join(out) + "\n"


forward = FUNC.replace("{app}", settings.DB_APP_ROLE)
reverse = (
    _bodies(_P11.SQL)
    + "DROP FUNCTION IF EXISTS planning_location_problem(bigint, bigint, bigint, timestamptz, timestamptz);\n"
)


class Migration(migrations.Migration):
    dependencies = [
        ("planning", "0011_location_gate"),
        ("fleet", "0017_planning_location_predicate"),
    ]
    operations = [migrations.RunSQL(forward, reverse)]

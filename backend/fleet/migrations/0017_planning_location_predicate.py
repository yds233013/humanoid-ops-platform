"""v2 Phase 03A-2 corrective (docs/13_v2_phase03_contract.md section 12, EM ruling 2026-11-25): the
FUTURE-PLANNING location predicate, separate from physical dispatch.

The physical-dispatch predicate is unchanged: fleet_robot_location + fleet_location_problem, read by the
authorization and departure gates (execution/0007): only hub(H), verified, departs from H.

New, for planning only (eligibility view, Python eligibility, options, Suggest, the planner, the planning
triggers):

  fleet_away_run(robot)            the robot's OPEN run, its visit hub and its RESERVED window: the earliest start
                                   and the latest end of the robot's reservations of the run's revision (loading,
                                   travel out, on site, travel back, unloading, charging / turnaround). Departed
                                   visits' reservations can never be released (execution/0002), so the window is
                                   what was booked.
  fleet_planning_location(robot)   the physical location plus:
      planning_problem  NULL when the robot may be planned (at some hub, still subject to the visit's hub):
                          * hub, verified                                       -> NULL
                          * vehicle or site on an open run whose reserved window has not ended -> NULL ("away",
                            At risk "Robot away until verified back at hub")
                          * vehicle or site on an open run whose window ended, or cannot be determined (no robot
                            reservation on the run's revision)                -> "Away on run EX: ..." (refused:
                            no optimistic availability)
                          * vehicle or site with no open run, in transfer, unaccounted, unknown, hub unverified
                                                                              -> the physical reason
      away_run, away_hub, away_from, away_until   the open run, its hub and its reserved window (away only)
  fleet_planning_location_problem(robot, hub, from, to)
                                   the visit-specific answer: planning_problem; else at another hub than ``hub``
                                   (verified there, or away on a run of another hub); else, when away, a booking
                                   [from, to) that overlaps the run's reserved window. NULL = allowed.

The planning triggers (planning/0012) and the Python planner (fleet.location, planning.services) apply exactly
this. STABLE, fixed search_path; EXECUTE revoked from PUBLIC and granted to ops_app (its statements fire the
planning triggers and read the eligibility view). Reverse drops them.
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE

SQL = r"""
CREATE OR REPLACE FUNCTION fleet_away_run(p_robot bigint)
RETURNS TABLE (run_id bigint, run_code text, run_hub bigint, window_start timestamptz, window_end timestamptz)
LANGUAGE sql STABLE ROWS 1 SET search_path = public, pg_temp AS $$
  SELECT x.id, x.code::text, v.hub_id, w.lo, w.hi
  FROM execution_executionrun x
  JOIN planning_visit v ON v.id = x.visit_id
  CROSS JOIN LATERAL (
    SELECT min(pr.starts_at) AS lo, max(pr.ends_at) AS hi
    FROM planning_reservation pr JOIN planning_resource res ON res.id = pr.resource_id
    WHERE res.robot_id = p_robot AND pr.revision_id = x.revision_id
  ) w
  WHERE x.id = execution_open_run_id(p_robot)
$$;

CREATE OR REPLACE FUNCTION fleet_planning_location(p_robot bigint)
RETURNS TABLE (kind text, hub_id bigint, ref text, verified boolean, problem text, planning_problem text,
               away_run text, away_hub bigint, away_from timestamptz, away_until timestamptz)
LANGUAGE plpgsql STABLE ROWS 1 SET search_path = public, pg_temp AS $fn$
#variable_conflict use_column
DECLARE loc record; phys text; plan text; a_id bigint; a_run text; a_hub bigint; a_from timestamptz;
        a_to timestamptz;
BEGIN
  SELECT * INTO loc FROM fleet_robot_location(p_robot);
  phys := fleet_location_problem(loc.kind, loc.ref, loc.verified);
  plan := phys;
  IF loc.kind IN ('vehicle', 'site') THEN
    SELECT x.run_id, x.run_code, x.run_hub, x.window_start, x.window_end INTO a_id, a_run, a_hub, a_from, a_to
    FROM fleet_away_run(p_robot) x;
    IF a_id IS NOT NULL THEN
      IF a_to IS NULL THEN
        plan := format('Away on run %s: its return cannot be determined', a_run);
      ELSIF a_to <= clock_timestamp() THEN
        plan := format('Away on run %s: overdue since %s and not verified back at hub', a_run,
                       to_char(a_to AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC');
      ELSE
        plan := NULL;  -- away until verified back at hub: At risk, plannable outside the run's window
      END IF;
    END IF;
  END IF;
  RETURN QUERY SELECT loc.kind, loc.hub_id, loc.ref, loc.verified, phys, plan, a_run, a_hub, a_from, a_to;
END $fn$;

CREATE OR REPLACE FUNCTION fleet_planning_location_problem(p_robot bigint, p_hub bigint, p_from timestamptz,
                                                           p_to timestamptz) RETURNS text
LANGUAGE plpgsql STABLE SET search_path = public, pg_temp AS $fn$
DECLARE pl record; code text;
BEGIN
  code := (SELECT r.code FROM fleet_robot r WHERE r.id = p_robot);
  SELECT * INTO pl FROM fleet_planning_location(p_robot);
  IF pl.planning_problem IS NOT NULL THEN RETURN format('robot %s: %s', code, pl.planning_problem); END IF;
  IF pl.kind = 'hub' THEN
    IF p_hub IS NOT NULL AND pl.hub_id IS DISTINCT FROM p_hub THEN
      RETURN format('robot %s is at hub %s, not at hub %s of this visit', code, pl.ref, fleet_hub_code(p_hub));
    END IF;
    RETURN NULL;
  END IF;
  -- away on an open run whose reserved window has not ended
  IF p_hub IS NOT NULL AND pl.away_hub IS DISTINCT FROM p_hub THEN
    RETURN format('robot %s is away on run %s of hub %s, not hub %s of this visit', code, pl.away_run,
                  fleet_hub_code(pl.away_hub), fleet_hub_code(p_hub));
  END IF;
  IF p_from IS NOT NULL
     AND tstzrange(p_from, greatest(coalesce(p_to, p_from), p_from) + interval '1 microsecond', '[)')
         && tstzrange(pl.away_from, pl.away_until, '[)') THEN
    RETURN format('robot %s is away on run %s until %s; this booking overlaps that run''s reserved window',
                  code, pl.away_run, to_char(pl.away_until AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC');
  END IF;
  RETURN NULL;
END $fn$;
"""

SIGNATURES = [
    "fleet_away_run(bigint)",
    "fleet_planning_location(bigint)",
    "fleet_planning_location_problem(bigint, bigint, timestamptz, timestamptz)",
]

forward = SQL + "".join(
    f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\nGRANT EXECUTE ON FUNCTION {s} TO {APP};\n" for s in SIGNATURES
)
reverse = "".join(f"DROP FUNCTION IF EXISTS {s};\n" for s in reversed(SIGNATURES))


class Migration(migrations.Migration):
    dependencies = [
        ("fleet", "0016_home_hub_by_transfer_only"),
        ("execution", "0008_field_roles_at_run_hub"),
    ]
    operations = [migrations.RunSQL(forward, reverse)]

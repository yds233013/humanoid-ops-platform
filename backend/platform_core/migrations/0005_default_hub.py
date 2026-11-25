"""v2 Phase 03A-1 (docs/13_v2_phase03_contract.md 11.1): the database default hub.

``platform_core_default_hub()`` is the DB default of every new hub_id column (fleet_robot, planning_vehicle,
planning_person, planning_visit, hub_workorder, hub_charging_session, hub_charger_reservation, hub_charger,
hub_stock_item). It applies only when a row is inserted WITHOUT a hub:

  * no hub exists: it creates ``HUB-1`` "Main hub" (time zone of the workspace) and returns it;
  * exactly one hub exists: it returns that hub;
  * two or more hubs exist: it RAISES "hub required" (check_violation, so run_command reports a refusal).

So a single-hub database and every Phase 02 test keep working, and a multi-hub database never guesses.

Concurrency: when no hub exists, the function serializes on a transaction-level advisory lock and counts
again after taking it (a fresh snapshot per statement in READ COMMITTED), so two simultaneous first inserts
create one HUB-1, never two hubs. A created HUB-1 writes an audit row (actor: the system, NULL).

``platform_core_backfill_hub()`` is the migration helper the ownership migrations use to backfill existing
rows: the existing hub (the lowest id when a Phase 02 database already has several: Phase 02 attributed
chargers and stock to that one), else a new HUB-1 through platform_core_default_hub(). Owner only.

Write boundary (Phase 02 8.8): SECURITY DEFINER, owned by ops_owner, fixed search_path, EXECUTE revoked from
PUBLIC and granted to ops_app only (the runtime role's INSERTs evaluate the default). The backfill helper is
not executable by ops_app or PUBLIC.
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE
# "0F1EE7 4B0B" ~ "FLEET HUB": the default-hub creation lock. Never reuse it elsewhere.
DEFAULT_HUB_LOCK_KEY = 0x0F1EE74B0B

forward = f"""
CREATE OR REPLACE FUNCTION platform_core_default_hub() RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE n int; h bigint;
BEGIN
  SELECT count(*), min(x.id) INTO n, h FROM (SELECT id FROM platform_core_hub ORDER BY id LIMIT 2) x;
  IF n = 1 THEN RETURN h; END IF;
  IF n >= 2 THEN
    RAISE EXCEPTION 'hub required: this workspace has more than one hub, so the record must name its hub'
      USING ERRCODE = 'check_violation';
  END IF;
  PERFORM pg_advisory_xact_lock({DEFAULT_HUB_LOCK_KEY});
  SELECT count(*), min(x.id) INTO n, h FROM (SELECT id FROM platform_core_hub ORDER BY id LIMIT 2) x;
  IF n = 1 THEN RETURN h; END IF;
  IF n >= 2 THEN
    RAISE EXCEPTION 'hub required: this workspace has more than one hub, so the record must name its hub'
      USING ERRCODE = 'check_violation';
  END IF;
  INSERT INTO platform_core_hub (code, name, area, time_zone, created_by_id, created_at)
  VALUES ('HUB-1', 'Main hub', '',
          coalesce((SELECT w.time_zone FROM platform_core_workspace w WHERE w.id = 1), 'America/Los_Angeles'),
          NULL, clock_timestamp())
  RETURNING id INTO h;
  INSERT INTO platform_core_auditevent (occurred_at, actor_id, command, outcome, entity_type, entity_id, payload,
                                        result, reason, request_id)
  VALUES (clock_timestamp(), NULL, 'platform_core.default_hub', 'committed', 'hub', 'HUB-1',
          '{{}}'::jsonb, jsonb_build_object('hub_id', h), 'A record was created before any hub existed', '');
  RETURN h;
END $fn$;
REVOKE ALL ON FUNCTION platform_core_default_hub() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION platform_core_default_hub() TO {APP};

CREATE OR REPLACE FUNCTION platform_core_backfill_hub() RETURNS bigint
LANGUAGE sql VOLATILE SET search_path = public, pg_temp AS $fn$
  SELECT coalesce((SELECT min(id) FROM platform_core_hub), platform_core_default_hub())
$fn$;
REVOKE ALL ON FUNCTION platform_core_backfill_hub() FROM PUBLIC;
"""

reverse = """
DROP FUNCTION IF EXISTS platform_core_backfill_hub();
DROP FUNCTION IF EXISTS platform_core_default_hub();
"""


def backfill_sql(table: str, column: str = "hub_id") -> str:
    """Put every existing row without a hub in the existing hub (created as HUB-1 only when rows exist and no hub
    does). Used by the 03A-1 ownership migrations of each app.

    The owner disables the table's USER triggers around the one UPDATE (in the migration's transaction): the
    backfill records which hub already owns a row, it is not a change of the row. Without this the guard
    triggers would bump robot / visit / person versions (invalidating every issued authorization of a Phase 02
    database) and refuse to touch a cancelled visit. Constraint (FK) triggers stay enabled."""
    return f"""
DO $bf$ DECLARE h bigint; BEGIN
  IF EXISTS (SELECT 1 FROM {table} WHERE {column} IS NULL) THEN
    h := platform_core_backfill_hub();
    SET CONSTRAINTS ALL IMMEDIATE;  -- FK checks now, so no trigger event is pending at ENABLE TRIGGER
    ALTER TABLE {table} DISABLE TRIGGER USER;
    UPDATE {table} SET {column} = h WHERE {column} IS NULL;
    ALTER TABLE {table} ENABLE TRIGGER USER;
  END IF;
END $bf$;
"""


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0004_hub_formtoken")]
    operations = [migrations.RunSQL(forward, reverse)]

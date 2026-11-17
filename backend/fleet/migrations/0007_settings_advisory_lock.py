"""Serialize deployment-setting changes against commands that decide on them (Slice 3, Engineer F).

fleet_deploymentsetting is writable only by the schema owner and the runtime role has SELECT only, so the
runtime role cannot SELECT ... FOR SHARE on it. Every INSERT / UPDATE / DELETE / TRUNCATE statement on the
table therefore first takes the transaction-level advisory lock 64942988919 (0x0F1EE75E77,
platform_core.locks.SETTINGS_LOCK_KEY) EXCLUSIVE. Commands that must not straddle a settings change
(readiness.authorize_departure, readiness.record_departure) take the same key SHARED before any guard row,
so a demo -> production switch either waits for an in-flight departure to commit or is seen by it.

Statement-level, so it fires even when an UPDATE / DELETE matches no row (harmless) and covers TRUNCATE.
Advisory lock functions need no table privilege; the trigger runs as whoever writes (the owner). Only
transaction-scoped advisory functions are used (pg_advisory_xact_lock here, pg_advisory_xact_lock_shared in
platform_core.locks), never session locks, so a lock can never outlive its transaction.

Trust boundary: the runtime role (ops_app) has SELECT only on this table (fleet 0002), so it cannot write
settings and cannot bypass the trigger. The schema owner and superusers remain TRUSTED administrators: they
can disable this trigger (ALTER TABLE ... DISABLE TRIGGER, as test teardown does) or take locks out of order.
Operators changing settings must do so as a plain DML statement in its own transaction.
"""

from django.db import migrations

SQL = """
CREATE OR REPLACE FUNCTION fleet_setting_change_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(64942988919);  -- platform_core.locks.SETTINGS_LOCK_KEY, EXCLUSIVE
  RETURN NULL;
END $$;
CREATE TRIGGER fleet_deploymentsetting_change_lock
  BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON fleet_deploymentsetting
  FOR EACH STATEMENT EXECUTE FUNCTION fleet_setting_change_lock();
"""

REVERSE = """
DROP TRIGGER IF EXISTS fleet_deploymentsetting_change_lock ON fleet_deploymentsetting;
DROP FUNCTION IF EXISTS fleet_setting_change_lock();
"""


class Migration(migrations.Migration):
    dependencies = [("fleet", "0006_positive_values")]

    operations = [migrations.RunSQL(SQL, reverse_sql=REVERSE)]

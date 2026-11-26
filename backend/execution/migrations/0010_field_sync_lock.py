"""v2 Phase 03C-1, EM ruling on 03C (docs/evidence/03a_lock_graph.md section 5): a ledger-only synchronization
lock replaces the execution-run row as the serialization point of field claims, abandonments and device reports.

Why: execution/0009 locked the run row FOR UPDATE first. Online commands take their guard rows (robot, visit,
trip, ...) and then the run row, in the global order of platform_core/locks.py; a field claim holding the run row
while its domain step took the same robot formed a real deadlock cycle. Now nothing in the ledger holds the run
row before the domain's own locks.

TABLE execution_field_sync_lock(run_id PRIMARY KEY, created_at): one row per run, created on demand, never
updated or deleted (trigger), ops_app SELECT only. Deliberately NO foreign key to the run: a foreign-key check
takes KEY SHARE on the run row, which conflicts with an online command's run FOR UPDATE and would bring the cycle
back. The run's existence is checked by a plain read.

FUNCTION execution_field_sync_lock(run) -> boolean (SECURITY DEFINER, owned by ops_owner, fixed search_path,
EXECUTE for ops_app only; takes no actor and writes nothing but the lock row): false when the run does not exist;
otherwise creates the run's lock row if missing and takes it FOR UPDATE until the transaction ends. The lock
belongs to the caller's transaction (field_ledger.claim calls it as its first statement).

execution_field_lock_for (the actor check shared by execution_field_record, execution_field_abandon and
execution_field_report_device) now takes the sync lock instead of the run row; the run is read without a lock.
Only these ledger functions and field_ledger.claim reference the sync lock: no online command takes it
(tests/test_v2_03c_ledger.py asserts it on the catalog and the Python sources).

Reverse restores the run-row lock of execution/0009 and drops the function and the table.
"""

from django.conf import settings
from django.db import migrations, models

APP = settings.DB_APP_ROLE
FIELD_ROLES = "ARRAY['ops_lead', 'field_operator']"

LOCK_FOR = r"""
CREATE OR REPLACE FUNCTION execution_field_lock_for(p_actor bigint, p_run bigint) RETURNS execution_executionrun
LANGUAGE plpgsql VOLATILE SET search_path = public, pg_temp AS $fn$
DECLARE x execution_executionrun;
BEGIN
  IF NOT hub_actor_has(p_actor, {roles}) THEN
    PERFORM execution_field_deny('NOT_PERMITTED', 'You have no field role');
  END IF;
  {lock}
  IF NOT FOUND THEN
    PERFORM execution_field_deny('NOT_YOUR_RUN', 'Sign in as a crew member of this run or the ops lead');
  END IF;
  IF NOT (execution_user_is_crew(x.id, p_actor) OR hub_actor_has(p_actor, ARRAY['ops_lead'])) THEN
    PERFORM execution_field_deny('NOT_YOUR_RUN', 'Sign in as a crew member of this run or the ops lead');
  END IF;
  IF NOT hub_actor_has_at(p_actor, {roles}, execution_run_hub(x.id)) THEN
    PERFORM execution_field_deny('NOT_PERMITTED', 'You no longer have a field role at this run''s hub');
  END IF;
  RETURN x;
END $fn$;
REVOKE ALL ON FUNCTION execution_field_lock_for(bigint, bigint) FROM PUBLIC;
"""
# the ledger-only lock first, then a plain read of the run (FOUND is set by the SELECT)
NEW_LOCK = (
    "IF NOT execution_field_sync_lock(p_run) THEN\n"
    "    PERFORM execution_field_deny('NOT_YOUR_RUN', 'Sign in as a crew member of this run or the ops lead');\n"
    "  END IF;\n"
    "  SELECT * INTO x FROM execution_executionrun WHERE id = p_run;"
)
OLD_LOCK = "SELECT * INTO x FROM execution_executionrun WHERE id = p_run FOR UPDATE;"

SQL = rf"""
CREATE TRIGGER execution_field_sync_lock_append_only BEFORE UPDATE OR DELETE ON execution_field_sync_lock
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER execution_field_sync_lock_no_truncate BEFORE TRUNCATE ON execution_field_sync_lock
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON execution_field_sync_lock FROM {APP};
GRANT SELECT ON execution_field_sync_lock TO {APP};

-- The ledger's serialization point for one run. Reads the run WITHOUT a lock (no FOR UPDATE, no FOR SHARE, and
-- the table has no foreign key, so no KEY SHARE either), creates the run's lock row on first use and takes it FOR
-- UPDATE. A concurrent first use waits on the other transaction's insert, then locks the same row.
CREATE OR REPLACE FUNCTION execution_field_sync_lock(p_run bigint) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
BEGIN
  IF p_run IS NULL OR NOT EXISTS (SELECT 1 FROM execution_executionrun x WHERE x.id = p_run) THEN
    RETURN false;
  END IF;
  INSERT INTO execution_field_sync_lock (run_id, created_at) VALUES (p_run, clock_timestamp())
  ON CONFLICT (run_id) DO NOTHING;
  PERFORM 1 FROM execution_field_sync_lock s WHERE s.run_id = p_run FOR UPDATE;
  RETURN true;
END $fn$;
REVOKE ALL ON FUNCTION execution_field_sync_lock(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION execution_field_sync_lock(bigint) TO {APP};
""" + LOCK_FOR.format(roles=FIELD_ROLES, lock=NEW_LOCK)

SQL_REVERSE = LOCK_FOR.format(roles=FIELD_ROLES, lock=OLD_LOCK) + (
    """
DROP FUNCTION IF EXISTS execution_field_sync_lock(bigint);
DROP TRIGGER IF EXISTS execution_field_sync_lock_no_truncate ON execution_field_sync_lock;
DROP TRIGGER IF EXISTS execution_field_sync_lock_append_only ON execution_field_sync_lock;
"""
)


class Migration(migrations.Migration):
    dependencies = [("execution", "0009_field_ledger")]

    operations = [
        migrations.CreateModel(
            name="FieldSyncLock",
            fields=[
                ("run_id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField()),
            ],
            options={"db_table": "execution_field_sync_lock", "ordering": ["run_id"]},
        ),
        migrations.RunSQL(SQL, SQL_REVERSE),
    ]

"""The runtime database role cannot rewrite history or bypass the database's own checks."""

import psycopg
import pytest
from django.core.management import call_command
from django.db import connection

AUDIT = "platform_core_auditevent"


def _insert_audit(cur) -> int:
    cur.execute(
        f"INSERT INTO {AUDIT} (occurred_at, command, outcome, entity_type, entity_id, payload, result, "
        "reason, "
        "request_id) VALUES (now(), 'test', 'committed', '', '', '{}', '{}', '', '') RETURNING id"
    )
    return cur.fetchone()[0]


@pytest.mark.parametrize(
    "sql",
    [
        f"UPDATE {AUDIT} SET reason = 'rewritten'",
        f"DELETE FROM {AUDIT}",
        f"TRUNCATE {AUDIT}",
        f"ALTER TABLE {AUDIT} DISABLE TRIGGER audit_no_update_delete",
        f"DROP TABLE {AUDIT}",
        "DROP TRIGGER audit_no_update_delete ON platform_core_auditevent",
        "CREATE TABLE sneaky (id int)",
    ],
)
def test_runtime_role_cannot_mutate_audit_or_schema(app_role_conn, sql):
    with app_role_conn.cursor() as cur:
        _insert_audit(cur)  # the runtime role CAN append
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(sql)
        cur.execute(f"SELECT count(*) FROM {AUDIT} WHERE reason = 'rewritten'")
        assert cur.fetchone()[0] == 0


@pytest.mark.django_db(transaction=True)
def test_even_schema_owner_cannot_update_audit_without_disabling_trigger():
    with connection.cursor() as cur:
        _insert_audit(cur)
        with pytest.raises(Exception, match="append-only"):
            cur.execute(f"UPDATE {AUDIT} SET reason = 'x'")


def test_runtime_role_has_dml_on_every_application_table(app_role_conn):
    """Guards against a future migration forgetting grants (default privileges should cover it)."""
    with app_role_conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            f"AND c.relname NOT IN ('{AUDIT}', 'django_migrations', 'fleet_deploymentsetting') "
            "AND NOT has_table_privilege(current_user, c.oid, 'INSERT')"
        )
        assert cur.fetchall() == []


def test_runtime_role_is_not_superuser_and_owns_nothing(app_role_conn):
    with app_role_conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone() == (False, False, False)
        cur.execute("SELECT count(*) FROM pg_tables WHERE tableowner = current_user")
        assert cur.fetchone()[0] == 0


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)

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
            f"AND c.relname NOT IN ('{AUDIT}', 'django_migrations', 'fleet_deploymentsetting', "
            "'readiness_departureauthorization', 'execution_executionrun', "
            # Slice 7 (07a 5.3): the registry is a lock-only row; activations are written only by the final
            # rebase-approval trigger (SECURITY DEFINER). Both asserted below.
            "'fleet_holdpolicyregistry', 'fleet_holdrebaseactivation', "
            # Phase 02B (contract 8.8): charger reservations, charging sessions and battery readings are written
            # only through SECURITY DEFINER functions (hub/0004); the runtime role has SELECT only. Asserted below.
            "'hub_charger_reservation', 'hub_charging_session', 'robot_soc_reading', "
            # Phase 02C (contract 8.8): maintenance rules, their approvals and withdrawals, and maintenance
            # records are written only through SECURITY DEFINER functions (fleet/0010). Asserted below.
            "'fleet_maintenancerule', 'fleet_maintenanceruleapproval', 'fleet_maintenancerulewithdrawal', "
            "'fleet_maintenancerecord', "
            # Phase 02D: the predictor log is written only by the owner-only operations_log_predictor_signals()
            # (operations/0008); stock items, stock reservations and the stock ledger only through SECURITY
            # DEFINER functions (hub/0008, contract 8.8). The runtime role has SELECT only. Asserted below.
            "'operations_predictor_observation', 'hub_stock_item', 'hub_stock_reservation', 'hub_stock_movement', "
            # Phase 03A-3 (contract I-8, 10.B): role grants are written only through the SECURITY DEFINER
            # functions of accounts/0006. The runtime role has SELECT only. Asserted below.
            "'accounts_rolegrant') "
            "AND NOT has_table_privilege(current_user, c.oid, 'INSERT')"
        )
        assert cur.fetchall() == []
        for table in (
            "hub_charger_reservation",
            "hub_charging_session",
            "robot_soc_reading",
            "fleet_maintenancerule",
            "fleet_maintenanceruleapproval",
            "fleet_maintenancerulewithdrawal",
            "fleet_maintenancerecord",
            "operations_predictor_observation",
            "hub_stock_item",
            "hub_stock_reservation",
            "hub_stock_movement",
            "accounts_rolegrant",
        ):
            cur.execute(
                "SELECT has_table_privilege(current_user, %s, 'SELECT'), "
                "has_table_privilege(current_user, %s, 'INSERT'), "
                "has_table_privilege(current_user, %s, 'UPDATE'), "
                "has_table_privilege(current_user, %s, 'DELETE')",
                [table] * 4,
            )
            assert cur.fetchone() == (True, False, False, False), table
        # Deliberate exception (readiness/0005): authorizations are issued only through the SECURITY DEFINER
        # function readiness_issue_authorization; the runtime role keeps SELECT and UPDATE (row locks) only.
        cur.execute(
            "SELECT has_table_privilege(current_user, 'readiness_departureauthorization', 'INSERT'), "
            "has_table_privilege(current_user, 'readiness_departureauthorization', 'SELECT'), "
            "has_table_privilege(current_user, 'readiness_departureauthorization', 'UPDATE')"
        )
        assert cur.fetchone() == (False, True, True)
        # Deliberate exception (Slice 4, execution/0002): execution runs are created only by the trigger on
        # readiness_departureevent; the runtime role keeps SELECT and UPDATE (row locks) only.
        cur.execute(
            "SELECT has_table_privilege(current_user, 'execution_executionrun', 'INSERT'), "
            "has_table_privilege(current_user, 'execution_executionrun', 'SELECT'), "
            "has_table_privilege(current_user, 'execution_executionrun', 'UPDATE')"
        )
        assert cur.fetchone() == (False, True, True)
        cur.execute(
            "SELECT has_table_privilege(current_user, 'fleet_holdpolicyregistry', 'INSERT'), "
            "has_table_privilege(current_user, 'fleet_holdpolicyregistry', 'SELECT'), "
            "has_table_privilege(current_user, 'fleet_holdpolicyregistry', 'UPDATE'), "
            "has_table_privilege(current_user, 'fleet_holdrebaseactivation', 'INSERT'), "
            "has_table_privilege(current_user, 'fleet_holdrebaseactivation', 'SELECT')"
        )
        assert cur.fetchone() == (False, True, True, False, True)


def test_runtime_role_is_not_superuser_and_owns_nothing(app_role_conn):
    with app_role_conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone() == (False, False, False)
        cur.execute("SELECT count(*) FROM pg_tables WHERE tableowner = current_user")
        assert cur.fetchone()[0] == 0


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)

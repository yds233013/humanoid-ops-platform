"""v2 Phase 03A closure: catalog-wide audit of the trusted write boundary (docs/13_v2_phase03_contract.md
section 12, I-8, 10.B; docs/11_v2_phase02_contract.md 8.8).

Every SECURITY DEFINER function in the public schema is found from pg_proc and must be classified below, so a new
function fails this file until someone decides who may run it:

* APP functions: fixed search_path, owned by ops_owner, no EXECUTE for PUBLIC, executable by ops_app and the
  owner only. Each one that takes an actor is called AS ops_app with a NULL, zero, non-existent and role-less
  actor (and a founder, a real regional role that none of them allows): refused, nothing written.
* OWNER-ONLY functions: the same, and ops_app cannot execute them.

The hub-owned tables those functions write give ops_app no INSERT, UPDATE or DELETE (table or column level) and
no sequence use; the home-hub columns of robots and vehicles, which ops_app may otherwise update, refuse a direct
change by a trigger (a received transfer is the only path), and a charger's hub never changes (hub/0014).

The wrong-hub actor (a real technician of another hub, on a real record) is covered per function family by
test_v2_03a_ownership (stock, charging), test_v2_03a_transfers (transfers) and test_v2_03a_authority (grants).

SYNTHETIC data only.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from django.conf import settings
from django.db import connection

from fleet import services as fs
from planning import services as ps
from platform_core.models import Hub
from tests.conftest import make_user

APP = settings.DB_APP_ROLE
OWNER = os.environ.get("DB_OWNER_USER", "ops_owner")

# signature (as regprocedure prints it) -> why ops_app may execute it
APP_FUNCTIONS: dict[str, str] = {
    "platform_core_default_hub()": "column default for hub_id; no actor, creates HUB-1 only when no hub exists",
    "readiness_issue_authorization(bigint,bigint,timestamp with time zone,jsonb,bigint,jsonb,text)": (
        "issues a departure authorization; the issuer must be an active ops lead"
    ),
    "fleet_propose_maintenance_rule(bigint,bigint,text,integer,integer,integer,integer,bigint,"
    "timestamp with time zone,text,text,text,text)": "engineer or engineering lead",
    "fleet_approve_maintenance_rule(bigint,bigint,text,text)": "engineering lead / safety reviewer per slot",
    "fleet_withdraw_maintenance_rule(bigint,bigint,text,text)": "engineering lead or safety reviewer",
    "fleet_record_maintenance(bigint,bigint,text,text,text)": "qualified technician at the robot's hub",
    "hub_reserve_charger(bigint,bigint,bigint,timestamp with time zone,timestamp with time zone,bigint,text)": (
        "hub technician or ops lead at the charger's hub"
    ),
    "hub_release_charger_reservation(bigint,bigint,text,text)": "hub technician or ops lead at the hub",
    "hub_start_charging(bigint,bigint,bigint,integer,timestamp with time zone,text)": "charging roles at the hub",
    "hub_end_charging(bigint,bigint,integer,text)": "charging roles at the hub",
    "hub_record_soc_reading(bigint,bigint,integer,timestamp with time zone,text)": "charging roles at the hub",
    "hub_create_stock_item(bigint,text,text,text,text,text)": "ops lead or admin (the only hub)",
    "hub_create_stock_item(bigint,text,text,text,text,bigint,text)": "ops lead or admin, at a named hub",
    "hub_receive_stock(bigint,bigint,integer,text,text)": "stock roles at the item's hub",
    "hub_adjust_stock(bigint,bigint,integer,text,text)": "stock roles at the item's hub",
    "hub_issue_stock(bigint,bigint,integer,bigint,text)": "stock roles at the item's hub",
    "hub_consume_stock(bigint,bigint,integer,bigint,text)": "stock roles at the item's hub",
    "hub_return_stock(bigint,bigint,integer,bigint,text)": "stock roles at the item's hub",
    "hub_reserve_stock(bigint,bigint,bigint,integer,text)": "stock roles at the item's hub",
    "hub_release_stock(bigint,bigint,text,text)": "stock roles at the item's hub",
    "hub_transfer_stock(bigint,bigint,integer,bigint,text)": "stock roles at the source hub",
    "hub_receive_transfer(bigint,bigint,text)": "stock roles at the destination hub",
    "fleet_transfer_start(bigint,text,bigint,bigint,text,text)": "ops lead or a technician at the source",
    "fleet_transfer_receive(bigint,bigint,text)": "ops lead or a technician at the receiving hub",
    "fleet_transfer_refuse(bigint,bigint,text,text)": "ops lead or a technician at the source or destination",
    "fleet_transfer_abandon(bigint,bigint,text,text)": "ops lead",
    "accounts_lock_grants(bigint,text[],bigint)": "run_command's grant check and FOR SHARE locks; writes nothing",
    "accounts_grant_role(bigint,bigint,text,bigint,timestamp with time zone,timestamp with time zone,text)": (
        "admin only"
    ),
    "accounts_revoke_grant(bigint,bigint,text,text)": "admin only",
    "accounts_bootstrap_admin(bigint,text)": "first admin of an empty workspace only; once",
    "fleet_create_robot(bigint,text,text,bigint,text)": "ops lead or engineering lead, at the target hub",
    "planning_create_vehicle(bigint,text,integer,text,bigint,text)": "ops lead, at the target hub",
    "hub_create_charger(bigint,text,text,text,bigint,text)": "admin or ops lead, at the target hub",
    # v2 03B-1: the travel matrix and the regional pause (tests/test_v2_03b_zones_travel.py, test_v2_03b_pause.py)
    "planning_set_travel_time(bigint,bigint,bigint,integer,text,timestamp with time zone,text)": (
        "admin or ops lead"
    ),
    "operations_pause_region(bigint,bigint,bigint,text,text)": "regional ops lead or safety_pause at the hub",
    "operations_lift_pause(bigint,bigint,text,text)": "regional ops lead only",
    # v2 03B-4: a home's zone (tests/test_v2_03b_site_zone.py)
    "planning_set_site_zone(bigint,bigint,bigint,text,text)": "regional ops lead only, with a reason",
    # v2 03C-1: the field-command ledger (tests/test_v2_03c_ledger.py)
    "execution_field_record(bigint,bigint,uuid,text,text,text,integer,integer,text,jsonb,"
    "timestamp with time zone,text,jsonb,text,text,text,jsonb)": (
        "crew of the run or the ops lead, with a current field grant at the run's hub"
    ),
    "execution_field_abandon(bigint,bigint,integer,integer,text,text)": (
        "crew of the run or the ops lead, with a current field grant at the run's hub"
    ),
    "execution_field_report_device(bigint,bigint,text,integer)": (
        "crew of the run or the ops lead, with a current field grant at the run's hub"
    ),
}

# signature -> why ops_app must NOT execute it
OWNER_ONLY: dict[str, str] = {
    "execution_open_run_on_departure()": "trigger on readiness_departureevent (opens the run)",
    "fleet_hold_rebase_approval_guard()": "trigger on fleet_holdrebaseapproval",
    "operations_log_predictor_signals()": "owner-only predictor logger (operations/0008)",
}

# APP functions that take no actor (their own rules are tested in their files)
NO_ACTOR = {
    "platform_core_default_hub()",
    "accounts_lock_grants(bigint,text[],bigint)",  # the actor is checked, not trusted: test_v2_03a_authority
    "accounts_bootstrap_admin(bigint,text)",  # test_v2_03a_authority
    # the issuer is the second argument and is checked after the visit lock: tested below with a live visit
    "readiness_issue_authorization(bigint,bigint,timestamp with time zone,jsonb,bigint,jsonb,text)",
}

# Every actor-taking APP function, called with the actor {a} and targets that need no fixture: each checks the
# actor's role first, before reading its target (the hub of a real target is checked after the row lock; the
# wrong-hub cases are in the files named in the module docstring).
ACTOR_CALLS: dict[str, str] = {
    "fleet_propose_maintenance_rule(bigint,bigint,text,integer,integer,integer,integer,bigint,"
    "timestamp with time zone,text,text,text,text)": (
        "SELECT fleet_propose_maintenance_rule({a}, 0, 'X', 1, NULL, NULL, NULL, NULL, NULL, 'x', 'x', 'x', '')"
    ),
    "fleet_approve_maintenance_rule(bigint,bigint,text,text)": (
        "SELECT fleet_approve_maintenance_rule({a}, 0, 'engineering', '')"
    ),
    "fleet_withdraw_maintenance_rule(bigint,bigint,text,text)": (
        "SELECT fleet_withdraw_maintenance_rule({a}, 0, 'x', '')"
    ),
    "fleet_record_maintenance(bigint,bigint,text,text,text)": "SELECT fleet_record_maintenance({a}, 0, 'X', '', '')",
    "hub_reserve_charger(bigint,bigint,bigint,timestamp with time zone,timestamp with time zone,bigint,text)": (
        "SELECT hub_reserve_charger({a}, 0, 0, now(), now() + interval '1 hour', NULL, '')"
    ),
    "hub_release_charger_reservation(bigint,bigint,text,text)": (
        "SELECT hub_release_charger_reservation({a}, 0, 'x', '')"
    ),
    "hub_start_charging(bigint,bigint,bigint,integer,timestamp with time zone,text)": (
        "SELECT hub_start_charging({a}, 0, 0, 50, now() + interval '1 hour', '')"
    ),
    "hub_end_charging(bigint,bigint,integer,text)": "SELECT hub_end_charging({a}, 0, 80, '')",
    "hub_record_soc_reading(bigint,bigint,integer,timestamp with time zone,text)": (
        "SELECT hub_record_soc_reading({a}, 0, 50, NULL, '')"
    ),
    "hub_create_stock_item(bigint,text,text,text,text,text)": (
        "SELECT hub_create_stock_item({a}, 'ZZ', 'n', 'reusable', 'kits', '')"
    ),
    "hub_create_stock_item(bigint,text,text,text,text,bigint,text)": (
        "SELECT hub_create_stock_item({a}, 'ZZ', 'n', 'reusable', 'kits', {hub}, '')"
    ),
    "hub_receive_stock(bigint,bigint,integer,text,text)": "SELECT hub_receive_stock({a}, 0, 1, '', '')",
    "hub_adjust_stock(bigint,bigint,integer,text,text)": "SELECT hub_adjust_stock({a}, 0, 1, 'x', '')",
    "hub_issue_stock(bigint,bigint,integer,bigint,text)": "SELECT hub_issue_stock({a}, 0, 1, NULL, '')",
    "hub_consume_stock(bigint,bigint,integer,bigint,text)": "SELECT hub_consume_stock({a}, 0, 1, NULL, '')",
    "hub_return_stock(bigint,bigint,integer,bigint,text)": "SELECT hub_return_stock({a}, 0, 1, NULL, '')",
    "hub_reserve_stock(bigint,bigint,bigint,integer,text)": "SELECT hub_reserve_stock({a}, 0, 0, 1, '')",
    "hub_release_stock(bigint,bigint,text,text)": "SELECT hub_release_stock({a}, 0, 'x', '')",
    "hub_transfer_stock(bigint,bigint,integer,bigint,text)": "SELECT hub_transfer_stock({a}, 0, 1, {hub}, '')",
    "hub_receive_transfer(bigint,bigint,text)": "SELECT hub_receive_transfer({a}, 0, '')",
    "fleet_transfer_start(bigint,text,bigint,bigint,text,text)": (
        "SELECT fleet_transfer_start({a}, 'robot', 0, {hub}, 'x', '')"
    ),
    "fleet_transfer_receive(bigint,bigint,text)": "SELECT fleet_transfer_receive({a}, 0, '')",
    "fleet_transfer_refuse(bigint,bigint,text,text)": "SELECT fleet_transfer_refuse({a}, 0, 'x', '')",
    "fleet_transfer_abandon(bigint,bigint,text,text)": "SELECT fleet_transfer_abandon({a}, 0, 'x', '')",
    "accounts_grant_role(bigint,bigint,text,bigint,timestamp with time zone,timestamp with time zone,text)": (
        "SELECT accounts_grant_role({a}, {user}, 'founder', NULL, NULL, NULL, '')"
    ),
    "accounts_revoke_grant(bigint,bigint,text,text)": "SELECT accounts_revoke_grant({a}, {grant}, 'x', '')",
    "fleet_create_robot(bigint,text,text,bigint,text)": "SELECT fleet_create_robot({a}, 'R-Z', 'SN-Z', {hub}, '')",
    "planning_create_vehicle(bigint,text,integer,text,bigint,text)": (
        "SELECT planning_create_vehicle({a}, 'V-Z', 1, '', {hub}, '')"
    ),
    "hub_create_charger(bigint,text,text,text,bigint,text)": (
        "SELECT hub_create_charger({a}, 'C-Z', 'Bay Z', '', {hub}, '')"
    ),
    "planning_set_travel_time(bigint,bigint,bigint,integer,text,timestamp with time zone,text)": (
        "SELECT planning_set_travel_time({a}, 0, 0, 10, 'demo', NULL, '')"
    ),
    "operations_pause_region(bigint,bigint,bigint,text,text)": (
        "SELECT operations_pause_region({a}, {hub}, NULL, 'x', '')"
    ),
    "operations_lift_pause(bigint,bigint,text,text)": "SELECT operations_lift_pause({a}, 0, 'x', '')",
    "planning_set_site_zone(bigint,bigint,bigint,text,text)": "SELECT planning_set_site_zone({a}, 0, NULL, 'x', '')",
    "execution_field_record(bigint,bigint,uuid,text,text,text,integer,integer,text,jsonb,"
    "timestamp with time zone,text,jsonb,text,text,text,jsonb)": (
        "SELECT execution_field_record({a}, 0, gen_random_uuid(), repeat('a', 64), 'dev', 'protective', NULL, NULL, "
        "'incident.report', '{{}}', NULL, 'missing', NULL, 'confirmed', '', '', '{{}}')"
    ),
    "execution_field_abandon(bigint,bigint,integer,integer,text,text)": (
        "SELECT execution_field_abandon({a}, 0, 1, 1, 'x', '')"
    ),
    "execution_field_report_device(bigint,bigint,text,integer)": (
        "SELECT execution_field_report_device({a}, 0, 'dev', 1)"
    ),
}

# Hub-owned tables written only through the functions above: ops_app may read them and nothing else.
GUARDED_TABLES = [
    "hub_stock_item",
    "hub_stock_reservation",
    "hub_stock_movement",
    "hub_charger_reservation",
    "hub_charging_session",
    "robot_soc_reading",
    "fleet_maintenancerule",
    "fleet_maintenanceruleapproval",
    "fleet_maintenancerulewithdrawal",
    "fleet_maintenancerecord",
    "fleet_hub_transfer",
    "accounts_rolegrant",
    "planning_travel_time",  # v2 03B-1
    "operations_region_pause",  # v2 03B-1
    "execution_field_command",  # v2 03C-1
    "execution_field_generation",
    "execution_field_device_report",
]

DEFINER_SQL = """
SELECT p.oid::regprocedure::text, pg_get_userbyid(p.proowner), coalesce(p.proconfig, '{}'),
       has_function_privilege('public', p.oid, 'EXECUTE'),
       has_function_privilege(%s, p.oid, 'EXECUTE'),
       ARRAY(SELECT DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END
             FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
             WHERE a.privilege_type = 'EXECUTE' ORDER BY 1)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.prosecdef
ORDER BY 1
"""


def _definers() -> list[tuple[str, str, list[str], bool, bool, list[str]]]:
    with connection.cursor() as cur:
        cur.execute(DEFINER_SQL, [APP])
        return [
            (s, o, list(cfg), pub, app, list(grantees)) for s, o, cfg, pub, app, grantees in cur.fetchall()
        ]


def _app_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=True,
    )


def _written() -> dict[str, int]:
    """Row counts of every table an actor-checked function could write, plus the audit trail and the hubs."""
    out: dict[str, int] = {}
    with connection.cursor() as cur:
        for t in [
            *GUARDED_TABLES,
            "platform_core_auditevent",
            "platform_core_hub",
            "readiness_departureauthorization",
            "fleet_robot",  # created by the hub/0015 functions
            "planning_vehicle",
            "hub_charger",
            "planning_resource",
        ]:
            cur.execute(f"SELECT count(*) FROM {t}")
            out[t] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM accounts_rolegrant WHERE revoked_at IS NOT NULL")
        out["revoked grants"] = cur.fetchone()[0]
    return out


# ------------------------------------------------------------------ 1. every SECURITY DEFINER function
@pytest.mark.django_db
def test_every_security_definer_function_is_classified():
    found = {s for s, *_ in _definers()}
    unclassified = sorted(found - set(APP_FUNCTIONS) - set(OWNER_ONLY))
    assert not unclassified, f"classify these SECURITY DEFINER functions in this file: {unclassified}"
    assert not set(APP_FUNCTIONS) & set(OWNER_ONLY)
    assert sorted((set(APP_FUNCTIONS) | set(OWNER_ONLY)) - found) == [], (
        "a classified function no longer exists"
    )
    # every actor-taking APP function has its forged-actor call below (a new one must be added there too)
    assert set(ACTOR_CALLS) == set(APP_FUNCTIONS) - NO_ACTOR


@pytest.mark.django_db
def test_every_security_definer_function_has_a_fixed_search_path_an_owner_and_restricted_execute():
    problems = []
    for sig, owner, config, public, app, grantees in _definers():
        if not any(c.startswith("search_path=") for c in config):
            problems.append(f"{sig}: no fixed search_path")
        if any(c.startswith("search_path=") and "$user" in c for c in config):
            problems.append(f"{sig}: search_path includes $user")
        if owner != OWNER:
            problems.append(f"{sig}: owned by {owner}")
        if public:
            problems.append(f"{sig}: PUBLIC can execute")
        allowed = {OWNER, APP} if sig in APP_FUNCTIONS else {OWNER}
        if set(grantees) - allowed:
            problems.append(f"{sig}: EXECUTE granted to {sorted(set(grantees) - allowed)}")
        if sig in APP_FUNCTIONS and not app:
            problems.append(f"{sig}: ops_app cannot execute an app function")
        if sig in OWNER_ONLY and app:
            problems.append(f"{sig}: ops_app can execute an owner-only function")
    assert problems == []


@pytest.mark.django_db(transaction=True)
def test_ops_app_cannot_call_the_owner_only_functions():
    with _app_conn() as conn:
        for sig in sorted(OWNER_ONLY):
            name = sig.split("(")[0]
            if sig.endswith("()") and name != "operations_log_predictor_signals":
                continue  # trigger functions: PostgreSQL refuses any direct call; EXECUTE is asserted above
            with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"SELECT {name}()")
        with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT accounts_seed_grant(1, 'admin', NULL, '')")  # owner-only, not a definer


# ------------------------------------------------------------------ 2. no direct-write bypass
@pytest.mark.django_db
def test_ops_app_has_no_write_privilege_on_the_hub_owned_guarded_tables():
    rows = {}
    with connection.cursor() as cur:
        for t in GUARDED_TABLES:
            cur.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT'), "
                "has_table_privilege(%s, %s, 'INSERT') OR has_any_column_privilege(%s, %s, 'INSERT'), "
                "has_table_privilege(%s, %s, 'UPDATE') OR has_any_column_privilege(%s, %s, 'UPDATE'), "
                "has_table_privilege(%s, %s, 'DELETE'), has_table_privilege(%s, %s, 'TRUNCATE')",
                [APP, t] * 7,
            )
            rows[t] = cur.fetchone()
            cur.execute(
                "SELECT s.relname, has_sequence_privilege(%s, s.oid, 'USAGE') OR has_sequence_privilege(%s, s.oid, "
                "'UPDATE') FROM pg_class s JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i') "
                "WHERE s.relkind = 'S' AND d.refobjid = %s::regclass",
                [APP, APP, t],
            )
            for seq, usable in cur.fetchall():
                assert not usable, f"ops_app can use {seq}"
    assert rows == {t: (True, False, False, False, False) for t in GUARDED_TABLES}


@pytest.mark.django_db(transaction=True)
def test_a_direct_home_hub_change_of_a_robot_or_a_vehicle_by_ops_app_is_refused():
    ops = make_user("hh_ops", "ops_lead")
    a = Hub.objects.create(code="HA", name="Hub A")
    b = Hub.objects.create(code="HB", name="Hub B")
    fs.register_robot(ops, code="R-1", serial="HH-SN-1", hub_code="HA")
    ps.register_vehicle(ops, code="VAN-1", robot_capacity=1, restraint_kit="SYN kit", hub_code="HA")
    with _app_conn() as conn:
        for table, code in (("fleet_robot", "R-1"), ("planning_vehicle", "VAN-1")):
            with (
                conn.cursor() as cur,
                pytest.raises(psycopg.errors.InsufficientPrivilege, match="only by a transfer"),
            ):
                cur.execute(f"UPDATE {table} SET hub_id = %s WHERE code = %s", [b.pk, code])
            with conn.cursor() as cur:  # the home hub is unchanged
                cur.execute(f"SELECT hub_id FROM {table} WHERE code = %s", [code])
                assert cur.fetchone()[0] == a.pk


@pytest.mark.django_db(transaction=True)
def test_a_charger_never_changes_hub_even_when_idle():
    """hub/0014: chargers have no transfer flow, so any hub change is refused, for ops_app and the owner alike,
    even with no reservation or session (the hub/0010 guard allowed that). Other charger updates still work."""
    from hub import charging as hc
    from hub.models import Charger

    ops = make_user("ch_ops", "ops_lead")
    a = Hub.objects.create(code="HA", name="Hub A")
    b = Hub.objects.create(code="HB", name="Hub B")
    hc.create_charger(ops, code="CA-1", name="Bay A1", hub_code="HA")
    refused = "Charger CA-1 cannot change hub; register it at the new hub"
    with _app_conn() as conn:
        with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege, match=refused):
            cur.execute("UPDATE hub_charger SET hub_id = %s WHERE code = 'CA-1'", [b.pk])
        with conn.cursor() as cur:  # the same value rewritten (an ORM save of every column) passes
            cur.execute("UPDATE hub_charger SET hub_id = hub_id, name = name WHERE code = 'CA-1'")
    with connection.cursor() as cur, pytest.raises(Exception, match="cannot change hub"):
        cur.execute("UPDATE hub_charger SET hub_id = %s WHERE code = 'CA-1'", [b.pk])  # the owner too
    hc.set_charger_in_service(ops, code="CA-1", in_service=False, reason="broken plug")  # still works
    ch = Charger.objects.get(code="CA-1")
    ch.save()  # a full ORM save lists hub_id unchanged: allowed
    assert (ch.hub_id, ch.in_service) == (a.pk, False)


# ------------------------------------------------------------------ 3. actor checks, as ops_app
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("actor", ["NULL", "0", "missing", "no_role", "founder"])
def test_every_actor_checked_function_refuses_a_bad_actor_as_ops_app_and_writes_nothing(actor):
    admin, person = make_user("ac_admin", "admin"), make_user("ac_person", "ops_lead")
    hub = Hub.objects.create(code="HX", name="Hub X")
    grant = person.role_grants.get(role="ops_lead")
    a = {
        "NULL": "NULL::bigint",
        "0": "0::bigint",
        "missing": "987654321",
        "no_role": str(make_user("ac_norole").pk),
        "founder": str(make_user("ac_founder", "founder").pk),  # a real role none of these functions allows
    }[actor]
    before = _written()
    refused = []
    with _app_conn() as conn:
        for sig, sql in sorted(ACTOR_CALLS.items()):
            try:
                with conn.cursor() as cur:
                    cur.execute(sql.format(a=a, hub=hub.pk, user=person.pk, grant=grant.pk))
            except psycopg.errors.InsufficientPrivilege:
                refused.append(sig)
    assert sorted(refused) == sorted(ACTOR_CALLS)
    assert _written() == before
    assert admin.is_active  # a real admin exists: the refusals are about the actor, not an empty workspace


@pytest.mark.django_db(transaction=True)
def test_the_authorization_issuer_must_be_a_real_ops_lead_as_ops_app():
    """Missing and zero issuers on a live, ready visit (real users without ops_lead: test_readiness_authz_boundary,
    a NULL issuer: refused as required)."""
    from readiness.models import DepartureAuthorization
    from tests.readiness_world import build_readiness
    from tests.test_readiness_authz_boundary import issue, live_args

    w = build_readiness(prefix="ia_")
    with _app_conn() as conn:
        for issuer in (None, 0, 987654321):
            with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
                issue(cur, live_args(w, issued_by=issuer))
        assert not DepartureAuthorization.objects.exists()
        with conn.cursor() as cur:  # the same live arguments with the real ops lead: issued
            issue(cur, live_args(w))
    assert DepartureAuthorization.objects.count() == 1

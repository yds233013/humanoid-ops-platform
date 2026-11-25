"""v2 Phase 03A-1: hub ownership and authority (docs/13_v2_phase03_contract.md 11.1, 11.2, 11.3, 10.B, 10.I, 11.6).

Exit gates proven here:

* Backfill on a database holding the Phase 02 demo (build_demo_workspace), migrated back to before 03A-1 and
  forward again: no record left without a hub, counts and guard versions unchanged, scoped grants at the hub.
  And a database with rows but no hub: the migration creates HUB-1 "Main hub".
* The default hub function in its three cases: none (creates HUB-1), one (returns it), many ("hub required"),
  through the ORM, a command and direct SQL as ops_app.
* Cross-hub writes refused through a page, a service and direct SQL as ops_app with a forged (real user,
  wrong hub or wrong role), NULL, zero and empty actor; the same write by the right hub's technician works.
* A technician who changes their own home hub gains nothing (writes, SQL and pages).
* A grant revoked while a guarded write runs, in both orders, with the waiter observed in pg_locks.
* Same-hub integrity triggers; stock codes unique per hub; per-hub stock settings override the global row;
  provision_settings --hub refuses safety settings.
* Pages (Today, Hub, Fleet, Dispatch) show only the hubs the viewer holds a grant for; ?hub=CODE narrows and
  never widens; the Hub page statement count does not grow with the number of hubs.

SYNTHETIC data only.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError as MgmtError
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts import services as account_services
from accounts.models import RoleGrant
from accounts.permissions import has_permission, visible_hub_ids
from fleet import services as fs
from fleet.models import DeploymentSetting, Robot
from hub import charging as hc
from hub import services as hs
from hub import stock as st
from hub.models import Charger, ChargingSession, StockItem, StockMovement, WorkOrder
from planning import services as ps
from planning.models import Person, Vehicle, Visit
from platform_core import workspace as ws
from platform_core.commands import Conflict, Invalid, PermissionDenied
from platform_core.models import AuditEvent, Hub
from tests.conftest import make_user
from tests.planning_world import DAY, TZ, at, plan
from tests.test_v2_02d_inventory import _world as inventory_world

APP = settings.DB_APP_ROLE
TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')
OWNER_TABLES = (
    "fleet_robot",
    "planning_vehicle",
    "planning_person",
    "planning_visit",
    "hub_workorder",
    "hub_charging_session",
    "hub_charger_reservation",
    "hub_charger",
    "hub_stock_item",
)


# ------------------------------------------------------------------ helpers
def _app_conn(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=autocommit,
    )


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _ledger() -> tuple[int, int]:
    return StockMovement.objects.count(), StockItem.objects.count()


def _two_hubs(prefix: str = "") -> SimpleNamespace:
    """The 02D inventory world at hub A (HUB-1, created by the database when the first record arrived), then a
    second hub HB with its own technician (granted AT HB), robot, charger, stock items and visit. Every record of
    HB is created with its hub named: with two hubs the database refuses to guess."""
    w = inventory_world(prefix)
    w.hub_a = Hub.objects.get()
    assert w.hub_a.code == "HUB-1" and w.hub_a.name == "Main hub"
    ws.create_hub(w.admin, code="HB", name="Hub B", area="", time_zone=TZ)
    w.hub_b = Hub.objects.get(code="HB")
    w.tech_b = make_user(f"{prefix}tech_b")
    RoleGrant.objects.create(user=w.tech_b, role="hub_technician", hub=w.hub_b)
    fs.register_robot(w.ops, code="RB-1", serial=f"{prefix}SN-B1", hub_code="HB")
    st.create_item(w.ops, code="RK", name="Restraint kit B", kind="reusable", unit="kits", hub_code="HB")
    st.create_item(w.ops, code="BKIT", name="B only kit", kind="reusable", unit="kits", hub_code="HB")
    hc.create_charger(w.ops, code="CA-1", name="Bay A1", hub_code="HUB-1")
    hc.create_charger(w.ops, code="CB-1", name="Bay B1", hub_code="HB")
    ps.register_vehicle(w.ops, code="VAN-B", robot_capacity=1, restraint_kit="SYN kit", hub_code="HB")
    w.vb = ps.request_visit(
        w.ops, site_code="S-1", on_site_start=at(DAY, 15), on_site_end=at(DAY, 16), travel_out_min=30,
        travel_back_min=30, tasks=["T-04"], required_scopes=["presence"], hub_code="HB",
    )["entity_id"]  # fmt: skip
    w.rk_a = StockItem.objects.get(code="RK", hub=w.hub_a).pk
    w.rk_b = StockItem.objects.get(code="RK", hub=w.hub_b).pk
    w.robot_b = Robot.objects.get(code="RB-1").pk
    w.charger_b = Charger.objects.get(code="CB-1").pk
    return w


# ------------------------------------------------------------------ 11.6: backfill on a Phase 02 database
def _snapshot() -> dict[str, Any]:
    out: dict[str, Any] = {}
    with connection.cursor() as cur:
        for t in (
            *OWNER_TABLES,
            "accounts_rolegrant",
            "readiness_departureauthorization",
            "platform_core_hub",
        ):
            cur.execute(f"SELECT count(*) FROM {t}")
            out[t] = cur.fetchone()[0]
        for t in ("fleet_robot", "planning_visit", "planning_person"):
            cur.execute(f"SELECT coalesce(sum(version), 0) FROM {t}")
            out[f"{t}.versions"] = cur.fetchone()[0]
    return out


def _has_column(table: str, column: str = "hub_id") -> bool:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
            [table, column],
        )
        return cur.fetchone() is not None


@pytest.mark.django_db(transaction=True)
def test_backfill_on_a_phase02_demo_database_leaves_no_record_without_a_hub(monkeypatch):
    from operations import demo_workspace as demo

    today = demo.workspace_today()  # the demo is built for today (real command times, practice runs)
    monkeypatch.setattr(demo, "workspace_today", lambda: today)
    monkeypatch.setenv(demo.PASSWORD_ENV, "demo-test-password-4821")
    call_command("build_demo_workspace", stdout=StringIO())
    oak = Hub.objects.get()
    assert oak.code == "OAK"
    # the demo grants hub-scoped roles AT the hub and every other role regionally
    assert set(
        RoleGrant.objects.filter(role__in=["hub_technician", "field_operator"]).values_list(
            "hub_id", flat=True
        )
    ) == {oak.pk}
    assert (
        not RoleGrant.objects.exclude(role__in=["hub_technician", "field_operator"])
        .filter(hub__isnull=False)
        .exists()
    )
    before = _snapshot()
    try:
        call_command("migrate", "platform_core", "0004_hub_formtoken", verbosity=0)  # back to Phase 02
        assert not _has_column("fleet_robot") and not _has_column("accounts_rolegrant")
        with connection.cursor() as cur:  # Phase 02 allowed a charger and an item without a hub
            cur.execute("UPDATE hub_charger SET hub_id = NULL")
            cur.execute("ALTER TABLE hub_stock_item DISABLE TRIGGER USER")
            cur.execute("UPDATE hub_stock_item SET hub_id = NULL")
            cur.execute("ALTER TABLE hub_stock_item ENABLE TRIGGER USER")
        assert _snapshot() == before
    finally:
        call_command("migrate", verbosity=0)
    after = _snapshot()
    assert after == before  # counts (hubs included: no second hub) and guard versions unchanged
    with connection.cursor() as cur:
        for t in OWNER_TABLES:
            cur.execute(f"SELECT count(*) FILTER (WHERE hub_id IS NULL), count(*) FILTER (WHERE hub_id <> %s) FROM {t}",
                        [oak.pk])  # fmt: skip
            assert cur.fetchone() == (0, 0), t
            cur.execute(
                "SELECT is_nullable FROM information_schema.columns WHERE table_name = %s AND column_name = 'hub_id'",
                [t],
            )
            assert cur.fetchone() == ("NO",), t
    scoped = RoleGrant.objects.filter(role__in=["hub_technician", "field_operator"])
    assert scoped.count() == 8 and set(scoped.values_list("hub_id", flat=True)) == {oak.pk}
    assert (
        not RoleGrant.objects.exclude(role__in=["hub_technician", "field_operator"])
        .filter(hub__isnull=False)
        .exists()
    )
    # the workspace still works on the migrated data: Today and the Hub page load for the demo logins
    assert _client(_user("dana.ortiz")).get("/ops/").status_code == 200
    assert _client(_user("lily.tran")).get("/hub/").status_code == 200


def _user(username: str) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.get(username=username)


@pytest.mark.django_db(transaction=True)
def test_backfill_creates_hub_1_when_a_database_has_rows_but_no_hub():
    try:
        call_command("migrate", "platform_core", "0004_hub_formtoken", verbosity=0)
        with connection.cursor() as cur:
            assert not _has_column("planning_vehicle")
            cur.execute("SELECT count(*) FROM platform_core_hub")
            assert cur.fetchone()[0] == 0
            cur.execute("INSERT INTO planning_vehicle (code, robot_capacity, restraint_kit, notes) "
                        "VALUES ('V-OLD', 1, 'kit', '') RETURNING id")  # fmt: skip
            vid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, is_staff,"
                " is_active, date_joined) VALUES ('!', false, 'old_field', '', '', '', false, true, now())"
                " RETURNING id"
            )
            uid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO accounts_rolegrant (user_id, role, valid_from, granted_at) "
                "VALUES (%s, 'field_operator', now(), now()), (%s, 'engineer', now(), now())",
                [uid, uid],
            )
    finally:
        call_command("migrate", verbosity=0)
    hub = Hub.objects.get()
    assert (hub.code, hub.name) == ("HUB-1", "Main hub")
    assert Vehicle.objects.get(pk=vid).hub_id == hub.pk
    assert RoleGrant.objects.get(user_id=uid, role="field_operator").hub_id == hub.pk
    assert RoleGrant.objects.get(user_id=uid, role="engineer").hub_id is None
    assert AuditEvent.objects.filter(command="platform_core.default_hub", entity_id="HUB-1").count() == 1


# ------------------------------------------------------------------ 11.1: the default hub, three cases
@pytest.mark.django_db
def test_default_hub_with_no_hub_creates_hub_1():
    assert not Hub.objects.exists()
    v = Vehicle.objects.create(code="V-N1")
    hub = Hub.objects.get()
    assert v.hub_id == hub.pk and (hub.code, hub.name) == ("HUB-1", "Main hub")
    assert Vehicle.objects.create(code="V-N2").hub_id == hub.pk  # the next one finds it: still one hub
    assert AuditEvent.objects.filter(command="platform_core.default_hub").count() == 1


@pytest.mark.django_db
def test_default_hub_with_one_hub_returns_it():
    admin = make_user("dh_admin", "admin")
    ws.create_hub(admin, code="ONE", name="The one", area="", time_zone=TZ)
    one = Hub.objects.get()
    assert Vehicle.objects.create(code="V-1").hub_id == one.pk
    with connection.cursor() as cur:
        cur.execute("SELECT platform_core_default_hub()")
        assert cur.fetchone()[0] == one.pk
    assert Hub.objects.count() == 1 and not Hub.objects.filter(code="HUB-1").exists()


@pytest.mark.django_db(transaction=True)
def test_default_hub_with_two_hubs_refuses_to_guess(app_role_conn):
    admin = make_user("dh2_admin", "admin")
    ops = make_user("dh2_ops", "ops_lead")
    ws.create_hub(admin, code="H1", name="One", area="", time_zone=TZ)
    ws.create_hub(admin, code="H2", name="Two", area="", time_zone=TZ)
    with pytest.raises(IntegrityError, match="hub required"), transaction.atomic():
        Vehicle.objects.create(code="V-X")
    with pytest.raises(Conflict, match="hub required"):  # a command: refused, nothing written
        ps.register_vehicle(ops, code="V-Y")
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="hub required"):
        cur.execute(
            "INSERT INTO planning_vehicle (code, robot_capacity, restraint_kit, notes) VALUES ('V-Z', 1, '', '')"
        )
    assert not Vehicle.objects.exists() and Hub.objects.count() == 2
    # naming the hub works, through the command and through SQL
    ps.register_vehicle(ops, code="V-Y", hub_code="H2")
    assert Vehicle.objects.get(code="V-Y").hub.code == "H2"
    # a hub-scoped grant must name its hub now; a regional one must not
    tech = make_user("dh2_tech")
    with pytest.raises(Invalid, match="Choose the hub"):
        account_services.grant_role(admin, user_id=tech.pk, role="hub_technician")
    with pytest.raises(IntegrityError, match="hub required"), transaction.atomic():
        RoleGrant.objects.create(user=tech, role="field_operator")
    with pytest.raises(Invalid, match="every hub"):
        account_services.grant_role(admin, user_id=tech.pk, role="engineer", hub_code="H1")
    account_services.grant_role(admin, user_id=tech.pk, role="hub_technician", hub_code="H1")
    account_services.grant_role(admin, user_id=tech.pk, role="hub_technician", hub_code="H2")  # one per hub
    with pytest.raises(Conflict, match="already holds an active hub_technician grant at hub H1"):
        account_services.grant_role(admin, user_id=tech.pk, role="hub_technician", hub_code="H1")
    with pytest.raises(IntegrityError, match="role_grant_hub_scope"), transaction.atomic():
        RoleGrant.objects.create(user=tech, role="ops_lead", hub=Hub.objects.get(code="H1"))
    with pytest.raises(IntegrityError, match="uniq_active_role_grant"), transaction.atomic():
        RoleGrant.objects.create(user=tech, role="hub_technician", hub=Hub.objects.get(code="H2"))
    audit = AuditEvent.objects.filter(command="accounts.grant_role", outcome="committed").order_by("id")
    assert [a.result.get("hub") for a in audit if a.payload.get("user_id") == tech.pk] == ["H1", "H2"]


# ------------------------------------------------------------------ 11.1: integrity
@pytest.mark.django_db(transaction=True)
def test_same_hub_integrity_is_enforced_by_the_database(app_role_conn):
    w = _two_hubs("in_")
    # a visit dispatched from HB cannot take a robot or a van of HUB-1 (revision trigger)
    with pytest.raises(Conflict, match="Robot R-01 belongs to another hub than visit"):
        plan(w, w.vb, robot="R-01", vehicle="VAN-B")
    with pytest.raises(Conflict, match="Vehicle VAN-1 belongs to another hub than visit"):
        plan(w, w.vb, robot="RB-1", vehicle="VAN-1")
    # a charger of HUB-1 cannot be booked or used for a robot of HB, even by the regional ops lead
    start = timezone.now() + timedelta(hours=2)
    with pytest.raises(Conflict, match="Robot RB-1 belongs to another hub than this charger reservation"):
        hc.reserve_charger(
            w.ops, charger_code="CA-1", robot_code="RB-1", starts_at=start, ends_at=start + timedelta(hours=1)
        )
    with pytest.raises(Conflict, match="Robot RB-1 belongs to another hub than this charging session"):
        hc.start_charging(w.ops, charger_code="CA-1", robot_code="RB-1", planned_until=start)
    # a task names one hub: the robot's
    with pytest.raises(Invalid, match="belongs to another hub"):
        hs.create_task(w.ops, type="other", title="x", robot_code="RB-1", hub_code="HUB-1")
    hs.create_task(w.ops, type="other", title="robot B task", robot_code="RB-1")
    assert WorkOrder.objects.get(title="robot B task").hub_id == w.hub_b.pk
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="belongs to another hub"),
    ):
        cur.execute(
            "INSERT INTO hub_workorder (type, title, robot_id, status, source_kind, link_url, note, close_reason,"
            " created_at, updated_at, version, hub_id) VALUES ('other', 't', %s, 'open', 'manual', '', '', '',"
            " now(), now(), 1, %s)",
            [w.robot_b, w.hub_a.pk],
        )
    # stock: an HB item is never reserved for a HUB-1 visit
    st.receive(w.tech_b, item_code="RK", quantity=2, hub_code="HB")
    with pytest.raises(Conflict, match="belongs to another hub"):
        st.reserve(w.tech_b, item_code="RK", visit_code=w.v1, quantity=1, hub_code="HB")
    # item codes are unique per hub (RK exists at both), never twice at one hub
    assert StockItem.objects.filter(code="RK").count() == 2
    with pytest.raises(Conflict, match="already exists at hub HB"):
        st.create_item(w.ops, code="RK", name="again", kind="reusable", hub_code="HB")
    with pytest.raises(Invalid, match="kept at several hubs; choose the hub"):
        st.receive(w.ops, item_code="RK", quantity=1)
    # a robot with an open charging session cannot change home hub
    hc.start_charging(w.tech_b, charger_code="CB-1", robot_code="RB-1", planned_until=start)
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="cannot change hub"),
    ):
        cur.execute("UPDATE fleet_robot SET hub_id = %s WHERE id = %s", [w.hub_a.pk, w.robot_b])


# ------------------------------------------------------------------ 10.B / 11.2: cross-hub writes
@pytest.mark.django_db(transaction=True)
def test_cross_hub_writes_are_refused_through_the_service_and_the_page():
    w = _two_hubs("cx_")
    assert has_permission(w.tech, "hub.stock_move") and has_permission(w.tech, "hub.stock_move", hub=w.hub_a)
    assert not has_permission(w.tech, "hub.stock_move", hub=w.hub_b)
    assert has_permission(w.ops, "hub.stock_move", hub=w.hub_b)  # regional
    before = _ledger()
    calls = [
        lambda: st.receive(w.tech, item_code="RK", quantity=3, hub_code="HB"),
        lambda: st.adjust(w.tech, item_code="BKIT", delta=2, reason="count"),
        lambda: st.reserve(w.tech, item_code="RK", visit_code=w.vb, quantity=1),
        lambda: hc.start_charging(w.tech, charger_code="CB-1", robot_code="RB-1",
                                  planned_until=timezone.now() + timedelta(hours=1)),
        lambda: hc.record_soc(w.tech, robot_code="RB-1", soc_pct=50),
    ]  # fmt: skip
    for call in calls:
        with pytest.raises(PermissionDenied, match="at hub HB"):
            call()
    assert _ledger() == before
    # work orders: a task of HB cannot be created, taken or closed by HUB-1's technician
    tid = int(hs.create_task(w.ops, type="other", title="B shelf", hub_code="HB")["entity_id"])
    with pytest.raises(PermissionDenied, match="at hub HB"):
        hs.assign_task(w.tech, task_id=tid, owner_code="P-8")
    with pytest.raises(PermissionDenied, match="at hub HB"):
        hs.create_task(w.tech, type="other", title="mine at B", hub_code="HB")
    assert WorkOrder.objects.get(pk=tid).owner_id is None
    # the same writes by HB's own technician work
    st.receive(w.tech_b, item_code="RK", quantity=3, hub_code="HB")
    hc.record_soc(w.tech_b, robot_code="RB-1", soc_pct=50)
    # through the page: a POST naming HB's item is refused in plain words and changes nothing
    c = _client(w.tech)
    token = TOKEN_RE.search(c.get("/hub/stock/").content.decode()).group(1)
    r = c.post("/hub/stock/receive/", {"_form_token": token, "item": "BKIT", "quantity": "4", "hub": "HB"},
               follow=True)  # fmt: skip
    text = r.content.decode()
    assert "Nothing was changed." in text and "at hub HB" in text
    assert StockMovement.objects.filter(item__code="BKIT").count() == 0
    denied = AuditEvent.objects.filter(command="hub.receive_stock", outcome="denied", actor=w.tech)
    assert denied.count() == 2  # the service call above and the page


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("actor", ["forged_wrong_hub", "forged_wrong_role", "NULL", "0", "''"])
def test_cross_hub_writes_by_direct_sql_as_ops_app_are_refused(actor):
    w = _two_hubs("sq_")
    vb = Visit.objects.get(code=w.vb).pk
    a = {
        "forged_wrong_hub": str(w.tech.pk),  # a real hub technician, of HUB-1
        "forged_wrong_role": str(w.safety.pk),  # a real user whose (regional) role allows none of these writes
        "NULL": "NULL::bigint",
        "0": "0::bigint",
        "''": "''",
    }[actor]
    calls = [
        f"SELECT hub_receive_stock({a}, {w.rk_b}, 5, '', '')",
        f"SELECT hub_adjust_stock({a}, {w.rk_b}, 1, 'x', '')",
        f"SELECT hub_issue_stock({a}, {w.rk_b}, 1, NULL, '')",
        f"SELECT hub_reserve_stock({a}, {w.rk_b}, {vb}, 1, '')",
        f"SELECT hub_create_stock_item({a}, 'NEWB', 'n', 'reusable', 'kits', {w.hub_b.pk}, '')",
        f"SELECT hub_start_charging({a}, {w.charger_b}, {w.robot_b}, 50, now() + interval '1 hour', '')",
        f"SELECT hub_record_soc_reading({a}, {w.robot_b}, 50, NULL, '')",
    ]
    expected = (
        psycopg.errors.InvalidTextRepresentation if actor == "''" else psycopg.errors.InsufficientPrivilege
    )
    before = (_ledger(), ChargingSession.objects.count(), AuditEvent.objects.count())
    conn = _app_conn()
    try:
        for sql in calls:
            with conn.cursor() as cur, pytest.raises(expected):
                cur.execute(sql)
        assert (_ledger(), ChargingSession.objects.count(), AuditEvent.objects.count()) == before
        # HB's technician through the same functions as ops_app: allowed (the hub is read from the locked row)
        with conn.cursor() as cur:
            cur.execute("SELECT hub_receive_stock(%s, %s, 2, 'count', '')", [w.tech_b.pk, w.rk_b])
            assert cur.fetchone()[0] > 0
    finally:
        conn.close()
    assert _ledger()[0] == before[0][0] + 1


# ------------------------------------------------------------------ 10.B: home hub confers nothing
@pytest.mark.django_db(transaction=True)
def test_a_technician_who_changes_their_home_hub_gains_nothing():
    w = _two_hubs("hh_")
    me = Person.objects.get(user=w.tech)
    assert me.hub_id == w.hub_a.pk
    conn = _app_conn()
    try:
        with conn.cursor() as cur:  # the runtime role can write people (planning), so assume the worst
            cur.execute("UPDATE planning_person SET hub_id = %s WHERE id = %s", [w.hub_b.pk, me.pk])
        assert Person.objects.get(pk=me.pk).hub_id == w.hub_b.pk
        assert not has_permission(w.tech, "hub.stock_move", hub=w.hub_b)
        assert visible_hub_ids(w.tech) == {w.hub_a.pk}
        with pytest.raises(PermissionDenied, match="at hub HB"):
            st.receive(w.tech, item_code="BKIT", quantity=1)
        with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT hub_receive_stock(%s, %s, 1, '', '')", [w.tech.pk, w.rk_b])
    finally:
        conn.close()
    html = _client(w.tech).get("/hub/stock/").content.decode()
    assert 'data-item="BKIT"' not in html and 'data-item="GLOVE"' in html
    assert StockMovement.objects.filter(item__hub=w.hub_b).count() == 0


# ------------------------------------------------------------------ 10.B: revocation racing a write
def _wait_for_waiter(pid: int) -> bool:
    for _ in range(100):
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_locks WHERE pid = %s AND NOT granted", [pid])
            if cur.fetchone()[0] > 0:
                return True
        time.sleep(0.05)
    return False


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["write", "revoke"])
def test_a_grant_revoked_during_a_guarded_write_is_ordered_both_ways(first):
    """The write (hub_receive_stock as ops_app) locks the technician's grant FOR SHARE after the item's row lock;
    the revocation (the UPDATE revoke_role makes, as ops_app) locks it FOR UPDATE. Whoever comes second waits
    (observed in pg_locks); a write that waited behind a committed revocation is refused."""
    w = _two_hubs(f"rv{first[0]}_")
    grant = RoleGrant.objects.get(user=w.tech, role="hub_technician")
    write_sql = "SELECT hub_receive_stock(%s, %s, 1, 'race', '')"
    revoke_sql = ("UPDATE accounts_rolegrant SET revoked_at = clock_timestamp(), revoked_by_id = %s "
                  "WHERE id = %s AND revoked_at IS NULL")  # fmt: skip
    holder, waiter = _app_conn(autocommit=False), _app_conn()
    out: dict[str, Any] = {}
    before = StockMovement.objects.count()
    try:
        with holder.cursor() as cur:
            if first == "write":
                cur.execute(write_sql, [w.tech.pk, w.rk_a])  # holds the item lock and the grant FOR SHARE
            else:
                cur.execute("SELECT id FROM accounts_rolegrant WHERE id = %s FOR UPDATE", [grant.pk])
                cur.execute(revoke_sql, [w.admin.pk, grant.pk])  # revoked, not committed yet

        def go() -> None:
            try:
                with waiter.cursor() as cur:
                    if first == "write":
                        cur.execute(revoke_sql, [w.admin.pk, grant.pk])
                    else:
                        cur.execute(write_sql, [w.tech.pk, w.rk_a])
                out["second"] = "committed"
            except psycopg.Error as exc:
                out["second"] = exc

        t = threading.Thread(target=go)
        t.start()
        assert _wait_for_waiter(waiter.info.backend_pid), "the second transaction never waited"
        assert "second" not in out
        holder.commit()
        t.join(10)
        assert not t.is_alive()
    finally:
        holder.close()
        waiter.close()
    grant.refresh_from_db()
    assert grant.revoked_at is not None
    if first == "write":  # the write committed before the revocation; the revocation then went through
        assert out["second"] == "committed"
        assert StockMovement.objects.count() == before + 1
    else:  # the write waited behind the revocation and was refused: nothing written
        assert isinstance(out["second"], psycopg.errors.InsufficientPrivilege), out["second"]
        assert StockMovement.objects.count() == before
    # and afterwards the technician can write nothing at their hub any more
    with pytest.raises(PermissionDenied):
        st.receive(w.tech, item_code="GLOVE", quantity=1)


# ------------------------------------------------------------------ 11.3: per-hub settings
@pytest.mark.django_db
def test_a_hub_setting_overrides_the_global_one_for_stock_only():
    w = _two_hubs("ps_")
    call_command("provision_settings", stock_minimums="RK=1,GLOVE=2", stock_per_visit="RK=1", with_defaults=True,
                 stdout=StringIO())  # fmt: skip
    out = StringIO()
    call_command("provision_settings", stock_minimums="RK=5,BKIT=1", hub="HB", stdout=out)
    assert "set stock_minimums at hub HB = RK=5,BKIT=1" in out.getvalue()
    assert DeploymentSetting.objects.get(key="stock_minimums", hub__isnull=True).value == "RK=1,GLOVE=2"
    levels = {(lv.hub_id, lv.code): lv for lv in st.levels()}
    assert levels[(w.hub_a.pk, "RK")].minimum == 1 and levels[(w.hub_a.pk, "GLOVE")].minimum == 2
    assert levels[(w.hub_b.pk, "RK")].minimum == 5 and levels[(w.hub_b.pk, "BKIT")].minimum == 1
    assert levels[(w.hub_b.pk, "RK")].per_visit == 1  # no hub row for stock_per_visit: the global one applies
    from fleet import provisioning as pv

    assert pv.hub_value("stock_minimums", w.hub_b.pk) == "RK=5,BKIT=1"
    assert pv.hub_value("stock_minimums", w.hub_a.pk) == "RK=1,GLOVE=2"
    assert [lv.code for lv in st.levels([w.hub_b.pk])] == ["BKIT", "RK"]
    # visit needs per hub: each visit is served from ITS hub's stock (3 kits at HUB-1 cover v1 and v2 there;
    # HB has none, so HB's visit is short even though HUB-1 has a kit to spare)
    st.receive(w.tech, item_code="RK", quantity=3, hub_code="HUB-1")
    needs = {n.visit_code: n for n in st.visit_needs(at(DAY, 0) - timedelta(days=1)) if n.item_code == "RK"}
    assert not needs[w.v1].short and not needs[w.v2].short and needs[w.vb].short
    assert needs[w.vb].available == 0 and needs[w.v1].available == 3
    assert set(st.visit_shortages(at(DAY, 0) - timedelta(days=1), [w.hub_a.pk])) == set()
    # safety settings stay global: --hub refuses them; idempotent and audited with the hub
    with pytest.raises(MgmtError, match="stay global"):
        call_command("provision_settings", maintenance_gate_active="true", hub="HB", stdout=StringIO())
    with pytest.raises(MgmtError, match="No hub"):
        call_command("provision_settings", stock_minimums="RK=1", hub="NOPE", stdout=StringIO())
    with pytest.raises(MgmtError, match="already have a different value"):
        call_command("provision_settings", stock_minimums="RK=6", hub="HB", stdout=StringIO())
    audit = AuditEvent.objects.filter(command="fleet.provision_settings").order_by("id").last()
    assert audit.payload["hub"] == "HB" and audit.entity_id == "provision_settings:HB"
    with connection.cursor() as cur:  # the database refuses a hub row of any other key
        with pytest.raises(Exception, match="fleet_setting_per_hub_keys_only"), transaction.atomic():
            cur.execute("INSERT INTO fleet_deploymentsetting (key, value, hub_id) VALUES ('maintenance_gate_active',"
                        " 'false', %s)", [w.hub_b.pk])  # fmt: skip
        cur.execute("SELECT fleet_setting('stock_minimums')")
        assert cur.fetchone()[0] == "RK=1,GLOVE=2"  # the SQL reader sees the global row only


# ------------------------------------------------------------------ 11.2 reads: pages and the hub filter
def _codes(html: str, pattern: str) -> set[str]:
    return set(re.findall(pattern, html))


@pytest.mark.django_db(transaction=True)  # Today reads in its own snapshot (outermost transaction)
def test_pages_show_only_the_viewers_hubs_and_the_filter_narrows():
    w = _two_hubs("pg_")
    hs.create_task(w.ops, type="other", title="Shelf at A", hub_code="HUB-1")
    hs.create_task(w.ops, type="other", title="Shelf at B", hub_code="HB")
    ops, tech, field = _client(w.ops), _client(w.tech), _client(w.field)
    # Hub queue
    assert "Shelf at A" in (h := ops.get("/hub/").content.decode()) and "Shelf at B" in h
    assert 'href="?hub=HB"' in h and "All hubs" in h
    assert "Shelf at B" in (h := ops.get("/hub/?hub=HB").content.decode()) and "Shelf at A" not in h
    assert "Shelf at A" in (h := tech.get("/hub/").content.decode()) and "Shelf at B" not in h
    h = tech.get("/hub/?hub=HB").content.decode()  # the filter never widens
    assert "Shelf at B" not in h and "Shelf at A" not in h and "You hold no role at hub HB" in h
    tid = WorkOrder.objects.get(title="Shelf at B").pk
    assert tech.get(f"/hub/tasks/{tid}/").status_code == 403
    # Fleet
    fleet = r"/fleet/([A-Z]+-\d+)/"
    assert {"R-01", "RB-1"} <= _codes(ops.get("/fleet/").content.decode(), fleet)
    assert "RB-1" not in _codes(field.get("/fleet/").content.decode(), fleet)
    assert "R-01" in _codes(field.get("/fleet/").content.decode(), fleet)
    assert _codes(ops.get("/fleet/?hub=HB").content.decode(), fleet) & {"R-01", "R-02", "R-03"} == set()
    # Dispatch (the week of DAY)
    week = f"/planning/?week={DAY.isoformat()}"
    assert w.vb in ops.get(week).content.decode() and w.v1 in ops.get(week).content.decode()
    assert w.vb not in field.get(week).content.decode() and w.v1 in field.get(week).content.decode()
    assert w.v1 not in ops.get(week + "&hub=HB").content.decode()
    # Stock and charging
    assert 'data-item="BKIT"' in ops.get("/hub/stock/").content.decode()
    assert 'data-item="BKIT"' not in tech.get("/hub/stock/").content.decode()
    assert "CB-1" in ops.get("/hub/charging/").content.decode()
    assert "CB-1" not in tech.get("/hub/charging/").content.decode()

    # Today: the fleet strip counts only the viewer's robots
    def robots_on_today(c: Client, q: str = "") -> int:
        return sum(c.get("/ops/" + q).context["t"].robot_counts.values())

    a_robots = Robot.objects.filter(hub=w.hub_a).count()
    assert robots_on_today(ops) == a_robots + 1
    assert robots_on_today(field) == a_robots
    assert robots_on_today(ops, "?hub=HB") == 1
    assert robots_on_today(field, "?hub=HB") == 0


@pytest.mark.django_db
def test_hub_page_statement_count_does_not_grow_with_the_number_of_hubs(monkeypatch):
    import platform_core.interaction as ia

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)  # no opportunistic token clean-up in the counts
    w = _two_hubs("sc_")
    hs.create_task(w.ops, type="other", title="B1", hub_code="HB")
    counts = {}
    for n in (2, 4):
        for k in range(Hub.objects.count() + 1, n + 1):
            code = f"H{k}"
            ws.create_hub(w.admin, code=code, name=f"Hub {k}", area="", time_zone=TZ)
            hs.create_task(w.ops, type="other", title=f"task {code}", hub_code=code)
        for user in (w.ops, w.tech):
            c = _client(user)
            c.get("/hub/")
            with CaptureQueriesContext(connection) as q:
                c.get("/hub/")
            counts[(n, user.username)] = len(q)
    assert counts[(2, w.ops.username)] == counts[(4, w.ops.username)], counts
    assert counts[(2, w.tech.username)] == counts[(4, w.tech.username)], counts

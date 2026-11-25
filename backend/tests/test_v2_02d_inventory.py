"""v2 Phase 02, milestone 02D: kit inventory (docs/11_v2_phase02_contract.md 4 row 02D, 8.5, 8.6, 8.7, 8.8).

The append-only stock ledger and its reconciliation (derived quantities equal a replay of the movements); the
invariants (on hand and available never below 0, atomic over-reservation refusal, kind rules); simultaneous
reservations in both orders with the waiter observed; the trusted write boundary (exact privileges of every new
table, sequence and function incl. helpers and PUBLIC; direct SQL as ops_app with forged, NULL and empty
actors); the 02D role matrix with refusals through the service and page POSTs; stable visit reservations;
"Kit short" At risk on Today and the visit page; restocking tasks; non-interference with readiness, holds,
custody and eligibility; work orders never move stock; statement counts. SYNTHETIC data only.
"""

import os
import re
import threading
import time
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError as MgmtError
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.roles import ALL_ROLES, PERMISSIONS
from execution.models import CustodyEvent
from fleet import services as fs
from fleet.models import DeploymentSetting, Hold, Robot
from hub import derive
from hub import services as hs
from hub import stock as st
from hub.models import StockItem, StockMovement, StockReservation, WorkOrder
from planning.models import Visit
from platform_core.commands import Conflict, PermissionDenied
from platform_core.models import AuditEvent
from readiness.models import DepartureAuthorization, ReadinessEvidence
from tests.conftest import make_user
from tests.fleet_world import set_setting
from tests.planning_world import TZ, build_planning, new_visit

pytestmark = pytest.mark.django_db
TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')
APP = settings.DB_APP_ROLE
GUARDED = ("hub_stock_item", "hub_stock_reservation", "hub_stock_movement")
ENTRY = {
    "hub_create_stock_item",
    "hub_receive_stock",
    "hub_adjust_stock",
    "hub_issue_stock",
    "hub_consume_stock",
    "hub_return_stock",
    "hub_reserve_stock",
    "hub_release_stock",
}
HELPERS = {"hub_stock_lock", "hub_stock_quantity_ok", "hub_stock_apply", "hub_stock_visit", "hub_stock_take"}


# ------------------------------------------------------------------ world
def _world(prefix: str = "") -> SimpleNamespace:
    w = build_planning(prefix=prefix)
    w.tech = make_user(f"{prefix}tech", "hub_technician")
    w.p.register_person(w.ops, username=w.tech.username, code="P-8")
    w.admin = make_user(f"{prefix}admin", "admin")
    st.create_item(w.ops, code="RK", name="Restraint kit", kind="reusable", unit="kits")
    st.create_item(w.admin, code="GLOVE", name="Nitrile gloves", kind="consumable", unit="boxes")
    w.v1 = new_visit(w)
    w.v2 = new_visit(w, h0=13, h1=14)
    return w


@pytest.fixture
def w() -> SimpleNamespace:
    return _world()


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _app_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=True,
    )


def _level(code: str) -> st.Level:
    return next(lv for lv in st.levels() if lv.code == code)


def _ledger() -> tuple[int, int, int]:
    return (StockMovement.objects.count(), StockReservation.objects.count(), StockItem.objects.count())


def _ids(w: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        rk=StockItem.objects.get(code="RK").pk,
        glove=StockItem.objects.get(code="GLOVE").pk,
        v1=Visit.objects.get(code=w.v1).pk,
        v2=Visit.objects.get(code=w.v2).pk,
    )


def _local_tomorrow() -> Any:
    return (timezone.now().astimezone(ZoneInfo(TZ)) + timedelta(days=1)).date()


def _tomorrow_visit(w: SimpleNamespace, h0: int) -> str:
    day = _local_tomorrow()
    return new_visit(w, d=day, h0=h0, h1=h0 + 1)


# ------------------------------------------------------------------ 8.8 privileges, exactly
def test_ops_app_has_select_only_on_every_stock_table_and_no_sequence_use():
    with connection.cursor() as cur:
        for table in GUARDED:
            cur.execute(
                "SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p"
                " WHERE has_table_privilege(%s, %s, p)",
                [APP, table],
            )
            assert [r[0] for r in cur.fetchall()] == ["SELECT"], table
            cur.execute(
                "SELECT has_sequence_privilege(%s, %s, 'USAGE'), has_sequence_privilege(%s, %s, 'SELECT'),"
                " has_sequence_privilege(%s, %s, 'UPDATE')",
                [APP, f"{table}_id_seq"] * 3,
            )
            assert cur.fetchone() == (False, False, False), table
            cur.execute(
                "SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE']) p"
                " WHERE has_table_privilege('public', %s, p)",
                [table],
            )
            assert cur.fetchall() == [], table


def _stock_functions() -> dict[str, dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            r"""SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner),
                       has_function_privilege('public', p.oid, 'EXECUTE'),
                       has_function_privilege('ops_app', p.oid, 'EXECUTE'),
                       has_function_privilege('ops_owner', p.oid, 'EXECUTE'),
                       coalesce(array_to_string(p.proconfig, ','), '')
                FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND (p.proname LIKE 'hub\_%%stock%%')"""
        )
        return {
            r[0]: {"definer": r[1], "owner": r[2], "public": r[3], "ops_app": r[4], "ops_owner": r[5],
                   "config": r[6]}
            for r in cur.fetchall()
        }  # fmt: skip


def test_every_stock_function_has_exactly_the_intended_privileges():
    fns = _stock_functions()
    assert set(fns) == ENTRY | HELPERS
    for name, f in fns.items():
        assert f["owner"] == "ops_owner" and f["ops_owner"] is True, name
        assert f["public"] is False, name
        assert "search_path=public, pg_temp" in f["config"], name  # fixed search_path on every one
        if name in ENTRY:
            assert f["definer"] is True and f["ops_app"] is True, name
        else:
            assert f["definer"] is False and f["ops_app"] is False, name


@pytest.mark.django_db(transaction=True)
def test_ops_app_cannot_write_the_ledger_or_call_a_helper_or_forge_an_actor():
    w = _world("wb_")
    ids = _ids(w)
    st.receive(w.tech, item_code="RK", quantity=5)
    rid = st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1)
    mv = StockMovement.objects.order_by("id").first()
    uid = w.tech.pk
    before = _ledger()
    conn = _app_conn()
    try:
        attempts = [
            ("INSERT INTO hub_stock_movement (item_id, kind, quantity, on_hand_delta, reserved_delta, reason, actor_id,"
             " occurred_at, request_id) VALUES (%s, 'receive', 99, 99, 0, '', %s, now(), '')", [ids.rk, uid]),
            ("UPDATE hub_stock_movement SET quantity = 99, on_hand_delta = 99 WHERE id = %s", [mv.pk]),
            ("DELETE FROM hub_stock_movement WHERE id = %s", [mv.pk]),
            ("TRUNCATE hub_stock_movement", []),
            ("INSERT INTO hub_stock_reservation (item_id, visit_id, quantity, created_by_id, created_at, updated_at,"
             " released_reason) VALUES (%s, %s, 1, %s, now(), now(), '')", [ids.rk, ids.v2, uid]),
            ("UPDATE hub_stock_reservation SET quantity = 50 WHERE id = %s", [rid]),
            ("UPDATE hub_stock_reservation SET released_at = now() WHERE id = %s", [rid]),
            ("DELETE FROM hub_stock_reservation WHERE id = %s", [rid]),
            ("INSERT INTO hub_stock_item (code, name, kind, unit, created_by_id, created_at)"
             " VALUES ('X', 'x', 'reusable', 'kits', %s, now())", [uid]),
            ("UPDATE hub_stock_item SET kind = 'consumable' WHERE id = %s", [ids.rk]),
            ("DELETE FROM hub_stock_item WHERE id = %s", [ids.rk]),
            ("SELECT nextval('hub_stock_movement_id_seq')", []),
            ("SELECT nextval('hub_stock_reservation_id_seq')", []),
            ("SELECT nextval('hub_stock_item_id_seq')", []),
            # the helpers are not callable by the runtime role (they skip the role check)
            ("SELECT hub_stock_lock(%s)", [ids.rk]),
            ("SELECT hub_stock_quantity_ok(1)", []),
            ("SELECT hub_stock_visit(%s, true)", [ids.v1]),
            ("SELECT hub_stock_take('issue', %s, %s, 1, NULL, '')", [uid, ids.rk]),
            ("SELECT hub_stock_apply(i, 'receive', 9, 9, 0, NULL, NULL, '', %s, '') FROM hub_stock_item i"
             " WHERE i.id = %s", [uid, ids.rk]),
        ]  # fmt: skip
        for sql, params in attempts:
            with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql, params)
        # forged actors: existing users WITHOUT a stock role (8.6 "no"), and a user that does not exist
        for role in ("field_operator", "engineer", "engineering_lead", "safety_reviewer", "founder", "admin"):
            forger = make_user(f"wb_forger_{role}", role).pk
            calls = [
                ("SELECT hub_receive_stock(%s, %s, 5, '', '')", [forger, ids.rk]),
                ("SELECT hub_adjust_stock(%s, %s, -1, 'x', '')", [forger, ids.rk]),
                ("SELECT hub_issue_stock(%s, %s, 1, NULL, '')", [forger, ids.rk]),
                ("SELECT hub_consume_stock(%s, %s, 1, NULL, '')", [forger, ids.glove]),
                ("SELECT hub_return_stock(%s, %s, 1, NULL, '')", [forger, ids.rk]),
                ("SELECT hub_reserve_stock(%s, %s, %s, 1, '')", [forger, ids.rk, ids.v2]),
                ("SELECT hub_release_stock(%s, %s, 'x', '')", [forger, rid]),
            ]
            if role != "admin":
                calls.append(
                    ("SELECT hub_create_stock_item(%s, 'NEW', 'n', 'reusable', 'kits', '')", [forger])
                )
            for sql, params in calls:
                with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(sql, params)
        with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT hub_receive_stock(987654, %s, 5, '', '')", [ids.rk])
        # an inactive user keeps no role
        gone = make_user("wb_gone", "hub_technician")
        gone.is_active = False
        gone.save()
        with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT hub_receive_stock(%s, %s, 5, '', '')", [gone.pk, ids.rk])
        assert _ledger() == before
        # the real actor through the function works for ops_app
        with conn.cursor() as cur:
            cur.execute("SELECT hub_receive_stock(%s, %s, 2, 'count', 'rid-1')", [uid, ids.rk])
            assert cur.fetchone()[0] > 0
    finally:
        conn.close()
    assert _level("RK").on_hand == 7
    assert AuditEvent.objects.filter(command="hub.receive_stock", outcome="committed").count() == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("actor", ["NULL", "0", "''"])
def test_a_null_zero_or_empty_actor_is_refused_by_every_entry_function(actor):
    w = _world("na_")
    ids = _ids(w)
    st.receive(w.tech, item_code="RK", quantity=3)
    st.receive(w.tech, item_code="GLOVE", quantity=3)
    rid = st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1)
    st.issue(w.tech, item_code="RK", quantity=1)
    before = (_ledger(), list(StockReservation.objects.values_list("id", "quantity", "released_at")),
              AuditEvent.objects.count())  # fmt: skip
    a = f"{actor}::bigint" if actor != "''" else "''"
    calls = [
        f"SELECT hub_create_stock_item({a}, 'NEW', 'n', 'reusable', 'kits', '')",
        f"SELECT hub_receive_stock({a}, {ids.rk}, 5, '', '')",
        f"SELECT hub_adjust_stock({a}, {ids.rk}, -1, 'x', '')",
        f"SELECT hub_issue_stock({a}, {ids.rk}, 1, NULL, '')",
        f"SELECT hub_consume_stock({a}, {ids.glove}, 1, NULL, '')",
        f"SELECT hub_return_stock({a}, {ids.rk}, 1, NULL, '')",
        f"SELECT hub_reserve_stock({a}, {ids.rk}, {ids.v2}, 1, '')",
        f"SELECT hub_release_stock({a}, {rid}, 'x', '')",
    ]
    expected = (
        psycopg.errors.InsufficientPrivilege if actor != "''" else psycopg.errors.InvalidTextRepresentation
    )
    conn = _app_conn()
    try:
        for sql in calls:
            with conn.cursor() as cur, pytest.raises(expected):
                cur.execute(sql)
    finally:
        conn.close()
    after = (_ledger(), list(StockReservation.objects.values_list("id", "quantity", "released_at")),
             AuditEvent.objects.count())  # fmt: skip
    assert after == before


def test_the_ledger_is_append_only_even_for_the_owner(w):
    st.receive(w.tech, item_code="RK", quantity=2)
    st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1)  # a row, so the row trigger fires
    for sql in ("UPDATE hub_stock_movement SET quantity = 9", "DELETE FROM hub_stock_movement",
                "DELETE FROM hub_stock_reservation", "DELETE FROM hub_stock_item"):  # fmt: skip
        with pytest.raises(Exception, match="append-only"), transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql)
    # TRUNCATE (not testable inside the test transaction): the triggers exist and are enabled
    with connection.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            " WHERE t.tgname LIKE 'hub_stock_%%_no_truncate' AND t.tgenabled = 'O' ORDER BY 1"
        )
        assert [r[0] for r in cur.fetchall()] == [
            "hub_stock_item",
            "hub_stock_movement",
            "hub_stock_reservation",
        ]
    # the per-kind effect check: even the owner cannot insert a movement whose effect contradicts its kind
    rk = StockItem.objects.get(code="RK").pk
    for kind, q, d_on, d_res, reason in (("receive", 2, 5, 0, ""), ("adjust", 2, -2, 0, " "),
                                         ("return", 1, 1, 1, ""), ("issue", 1, 1, 0, "")):  # fmt: skip
        with pytest.raises(Exception, match="hub_stock_movement_effect"), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO hub_stock_movement (item_id, kind, quantity, on_hand_delta, reserved_delta, reason,"
                    " actor_id, occurred_at, request_id) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), '')",
                    [rk, kind, q, d_on, d_res, reason, w.tech.pk],
                )
    assert StockItem._meta.get_field("kind") and not any(
        f.name in ("on_hand", "reserved", "available", "quantity") for f in StockItem._meta.get_fields()
    )  # no counter column on the item


# ------------------------------------------------------------------ 8.5 ledger, reconciliation, invariants
def _replay() -> dict[str, dict[str, int]]:
    """An independent replay of the ledger from what people entered (kind and quantity; adjustments carry
    their sign) and the active reservations."""
    out: dict[str, dict[str, int]] = {}
    for m in StockMovement.objects.select_related("item").order_by("id"):
        x = out.setdefault(m.item.code, {"on_hand": 0, "reserved_moves": 0, "out": 0})
        q = m.quantity
        if m.kind in ("receive", "return"):
            x["on_hand"] += q
        elif m.kind == "adjust":
            x["on_hand"] += q if m.on_hand_delta > 0 else -q
        elif m.kind in ("issue", "consume"):
            x["on_hand"] -= q
        x["reserved_moves"] += {"reserve": q, "release": -q}.get(m.kind, 0) + (
            m.reserved_delta if m.kind in ("issue", "consume") else 0
        )
        x["out"] += {"issue": q, "return": -q}.get(m.kind, 0)
    for code, x in out.items():
        x["reserved"] = sum(
            StockReservation.objects.filter(item__code=code, released_at__isnull=True).values_list(
                "quantity", flat=True
            )
        )
    return out


def test_derived_quantities_equal_a_replay_of_the_ledger(w):
    v3 = new_visit(w, h0=16, h1=17)
    st.receive(w.tech, item_code="RK", quantity=6, note="first count")
    st.receive(w.ops, item_code="GLOVE", quantity=20)
    r1 = st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=2)
    st.reserve(w.tech, item_code="RK", visit_code=w.v2, quantity=1)
    st.reserve(w.tech, item_code="RK", visit_code=v3, quantity=1)
    st.reserve(w.tech, item_code="GLOVE", visit_code=w.v1, quantity=4)
    st.issue(w.tech, item_code="RK", quantity=1, visit_code=w.v1)  # draws 1 of the 2 reserved
    st.issue(w.tech, item_code="RK", quantity=2, visit_code=w.v2)  # draws its 1, plus 1 unreserved
    st.return_stock(w.tech, item_code="RK", quantity=2, visit_code=w.v2)
    st.consume(w.tech, item_code="GLOVE", quantity=6, visit_code=w.v1)  # draws all 4 reserved
    st.consume(w.tech, item_code="GLOVE", quantity=1)
    st.adjust(w.ops, item_code="RK", delta=-1, reason="damaged strap written off")
    st.adjust(w.tech, item_code="GLOVE", delta=3, reason="found a box behind the shelf")
    st.release(w.tech, reservation_id=r1, reason="visit needs one only")
    replay = _replay()
    for lv in st.levels():
        x = replay[lv.code]
        assert lv.on_hand == x["on_hand"] and lv.reserved == x["reserved"] == x["reserved_moves"], lv
        assert lv.available == lv.on_hand - lv.reserved >= 0 and lv.on_hand >= 0
        if lv.reusable:
            assert lv.out == x["out"]
    rk, glove = _level("RK"), _level("GLOVE")
    # RK: 6 - 1 - 2 + 2 - 1 = 4 on hand; reserved: v3's 1 (v1 released, v2 drawn); out: 1 with v1
    assert (rk.on_hand, rk.reserved, rk.available, rk.out) == (4, 1, 3, 1)
    # GLOVE: 20 - 6 - 1 + 3 = 16; the reservation was drawn down to nothing and closed
    assert (glove.on_hand, glove.reserved, glove.available) == (16, 0, 16)
    assert StockReservation.objects.get(item__code="GLOVE").released_reason == "Used for the visit"
    # every movement has a committed audit row written by the function
    assert AuditEvent.objects.filter(command__endswith="_stock", outcome="committed").count() == (
        StockMovement.objects.count()
    )


def test_refusals_write_nothing_and_say_why_in_plain_words(w):
    st.receive(w.tech, item_code="RK", quantity=2)
    st.receive(w.tech, item_code="GLOVE", quantity=5)
    st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1)
    before = _ledger()
    cases = [
        (lambda: st.issue(w.tech, item_code="RK", quantity=3), r"Only 2 kits of Restraint kit \(RK\) are on hand"),
        (lambda: st.issue(w.tech, item_code="RK", quantity=2), r"Only 1 kits of Restraint kit \(RK\) are available"),
        (lambda: st.adjust(w.tech, item_code="RK", delta=-3, reason="count"), "are on hand"),
        (lambda: st.adjust(w.tech, item_code="RK", delta=-2, reason="count"), "release a reservation first"),
        (lambda: st.reserve(w.tech, item_code="RK", visit_code=w.v2, quantity=2),
         r"Only 1 kits of Restraint kit \(RK\) are available to reserve \(2 on hand, 1 already reserved"),
        (lambda: st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=3), "available to reserve"),
        (lambda: st.return_stock(w.tech, item_code="GLOVE", quantity=1), "is a consumable and does not come back"),
        (lambda: st.consume(w.tech, item_code="RK", quantity=1), "is a reusable kit: issue it and record its return"),
        (lambda: st.return_stock(w.tech, item_code="RK", quantity=1), "Only 0 kits .* are out with visits"),
        (lambda: st.adjust(w.tech, item_code="RK", delta=1, reason="  "), "needs a reason"),
        (lambda: st.adjust(w.tech, item_code="RK", delta=0, reason="x"), "not 0"),
        (lambda: st.receive(w.tech, item_code="RK", quantity=0), "from 1 to 100000"),
        (lambda: st.receive(w.tech, item_code="RK", quantity=100001), "from 1 to 100000"),
        (lambda: st.create_item(w.ops, code="RK", name="again", kind="reusable"), "already exists"),
        (lambda: st.create_item(w.ops, code="NEW", name="x", kind="spare"), "reusable or consumable"),
    ]  # fmt: skip
    for call, words in cases:
        with pytest.raises(Conflict, match=words):
            call()
    assert _ledger() == before
    assert AuditEvent.objects.filter(outcome="denied", command__contains="stock").count() == len(cases)
    # a cancelled visit or one that is over cannot get a reservation
    w.p.cancel_visit(w.ops, visit_code=w.v2, reason="customer cancelled")
    with pytest.raises(Conflict, match="does not exist or is cancelled"):
        st.reserve(w.tech, item_code="RK", visit_code=w.v2, quantity=1)
    assert _ledger() == before


def test_a_visit_keeps_one_stable_reservation_through_re_planning_and_issue(w):
    st.receive(w.tech, item_code="RK", quantity=4)
    rid = st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1)
    assert st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1) == rid  # same amount: nothing new
    assert StockMovement.objects.filter(kind="reserve").count() == 1
    w.p.plan_visit(w.ops, visit_code=w.v1, robot_code="R-01", lead_code="P-1", second_code="P-2",
                   vehicle_code="VAN-1")  # fmt: skip
    w.p.plan_visit(w.ops, visit_code=w.v1, robot_code="R-02", lead_code="P-1", second_code="P-2",
                   vehicle_code="VAN-1")  # re-planned onto another robot  # fmt: skip
    assert st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=3) == rid
    assert st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=2) == rid
    r = StockReservation.objects.get()
    assert (r.pk, r.quantity, r.released_at) == (rid, 2, None)
    assert list(StockMovement.objects.filter(reservation_id=rid).values_list("kind", "quantity")) == [
        ("reserve", 1), ("reserve", 2), ("release", 1)
    ]  # fmt: skip
    st.issue(w.tech, item_code="RK", quantity=2, visit_code=w.v1)
    r.refresh_from_db()
    assert r.released_at is not None and r.released_reason == "Issued for the visit"
    assert _level("RK").reserved == 0 and _level("RK").on_hand == 2
    # a new reservation for the visit after the old one closed is a new identity (the old one is history)
    assert st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1) != rid
    assert StockReservation.objects.filter(released_at__isnull=True).count() == 1


# ------------------------------------------------------------------ concurrency
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first_visit", ["v1", "v2"])
def test_two_simultaneous_reservations_for_the_last_kit_exactly_one_succeeds(first_visit):
    w = _world("cc_")
    ids = _ids(w)
    st.receive(w.tech, item_code="RK", quantity=1)
    order = [ids.v1, ids.v2] if first_visit == "v1" else [ids.v2, ids.v1]
    sql = "SELECT hub_reserve_stock(%s, %s, %s, 1, '')"
    a, b = _app_conn(), _app_conn()
    out: dict[str, Any] = {}
    try:
        a.autocommit = False
        with a.cursor() as ca:
            ca.execute(sql, [w.tech.pk, ids.rk, order[0]])  # holds the item's row lock, uncommitted
        b_pid = b.info.backend_pid

        def go() -> None:
            try:
                with b.cursor() as cb:
                    cb.execute(sql, [w.tech.pk, ids.rk, order[1]])
                out["second"] = "committed"
            except psycopg.Error as exc:
                out["second"] = exc

        t = threading.Thread(target=go)
        t.start()
        waiting = False
        for _ in range(100):  # the waiter is observed in pg_locks: an ungranted lock of the second session
            with connection.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_locks WHERE pid = %s AND NOT granted", [b_pid])
                row = cur.fetchone()
            if row and row[0] > 0:
                waiting = True
                break
            time.sleep(0.05)
        assert waiting and t.is_alive(), "the second reservation must wait for the first one's item lock"
        a.commit()
        t.join(20)
    finally:
        a.close()
        b.close()
    assert isinstance(out["second"], psycopg.errors.CheckViolation), out
    assert "available to reserve" in str(out["second"])
    (r,) = StockReservation.objects.all()
    assert r.visit_id == order[0] and _level("RK").available == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["issue", "adjust"])
def test_a_reservation_racing_an_issue_or_a_write_off_never_drives_available_below_zero(first):
    w = _world("ci_")
    ids = _ids(w)
    st.receive(w.tech, item_code="RK", quantity=1)
    take = (
        ("SELECT hub_issue_stock(%s, %s, 1, NULL, '')", [w.tech.pk, ids.rk])
        if first == "issue"
        else ("SELECT hub_adjust_stock(%s, %s, -1, 'lost', '')", [w.tech.pk, ids.rk])
    )
    a, b = _app_conn(), _app_conn()
    out: dict[str, Any] = {}
    try:
        a.autocommit = False
        with a.cursor() as ca:
            ca.execute(*take)

        def go() -> None:
            try:
                with b.cursor() as cb:
                    cb.execute("SELECT hub_reserve_stock(%s, %s, %s, 1, '')", [w.tech.pk, ids.rk, ids.v1])
                out["second"] = "committed"
            except psycopg.Error as exc:
                out["second"] = exc

        t = threading.Thread(target=go)
        t.start()
        t.join(1.0)
        assert t.is_alive()
        a.commit()
        t.join(20)
    finally:
        a.close()
        b.close()
    assert isinstance(out["second"], psycopg.errors.CheckViolation), out
    lv = _level("RK")
    assert (lv.on_hand, lv.reserved) == (0, 0) and StockReservation.objects.count() == 0


# ------------------------------------------------------------------ 8.6 roles
def test_the_02d_role_matrix():
    assert PERMISSIONS["hub.stock_move"] == {"hub_technician", "ops_lead"}
    assert PERMISSIONS["hub.stock_adjust"] == {"hub_technician", "ops_lead"}
    assert PERMISSIONS["hub.stock_reserve"] == {"hub_technician", "ops_lead"}
    assert PERMISSIONS["hub.manage_stock_items"] == {"admin", "ops_lead"}


@pytest.mark.parametrize("role", ALL_ROLES)
def test_every_role_is_refused_what_it_may_not_do(w, role):
    st.receive(w.tech, item_code="RK", quantity=3)
    st.receive(w.tech, item_code="GLOVE", quantity=3)
    rid = st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1)
    st.issue(w.tech, item_code="RK", quantity=1)
    user = make_user(f"sr_{role}", role)
    move_ok = role in PERMISSIONS["hub.stock_move"]
    adjust_ok = role in PERMISSIONS["hub.stock_adjust"]
    reserve_ok = role in PERMISSIONS["hub.stock_reserve"]
    manage_ok = role in PERMISSIONS["hub.manage_stock_items"]
    before = (_ledger(), list(StockReservation.objects.values_list("id", "quantity", "released_at")))
    if not move_ok:
        for call in (
            lambda: st.receive(user, item_code="RK", quantity=1),
            lambda: st.issue(user, item_code="RK", quantity=1),
            lambda: st.return_stock(user, item_code="RK", quantity=1),
            lambda: st.consume(user, item_code="GLOVE", quantity=1),
        ):
            with pytest.raises(PermissionDenied):
                call()
    if not adjust_ok:
        with pytest.raises(PermissionDenied):
            st.adjust(user, item_code="RK", delta=1, reason="count")
    if not reserve_ok:
        with pytest.raises(PermissionDenied):
            st.reserve(user, item_code="RK", visit_code=w.v2, quantity=1)
        with pytest.raises(PermissionDenied):
            st.release(user, reservation_id=rid, reason="x")
    if not manage_ok:
        with pytest.raises(PermissionDenied):
            st.create_item(user, code="NEW", name="New kit", kind="reusable")
    assert (_ledger(), list(StockReservation.objects.values_list("id", "quantity", "released_at"))) == before
    # the screens: POSTs without the permission are 403 and write nothing
    c = _client(user)
    for url, data, ok in (
        ("/hub/stock/receive/", {"item": "RK", "quantity": "1"}, move_ok),
        ("/hub/stock/issue/", {"item": "RK", "quantity": "1"}, move_ok),
        ("/hub/stock/return/", {"item": "RK", "quantity": "1"}, move_ok),
        ("/hub/stock/consume/", {"item": "GLOVE", "quantity": "1"}, move_ok),
        ("/hub/stock/adjust/", {"item": "RK", "delta": "1", "reason": "count"}, adjust_ok),
        ("/hub/stock/reserve/", {"item": "RK", "visit": w.v2, "quantity": "1"}, reserve_ok),
        (f"/hub/stock/reservations/{rid}/release/", {"reason": "x"}, reserve_ok),
        ("/hub/stock/items/", {"code": "NEW", "name": "New", "kind": "reusable", "unit": "kits"}, manage_ok),
    ):
        if not ok:
            assert c.post(url, data).status_code == 403, (role, url)
    if not manage_ok:
        assert c.get("/hub/stock/items/").status_code == 403
    if not reserve_ok:
        assert c.get(f"/hub/stock/reservations/{rid}/release/").status_code == 403
    assert (_ledger(), list(StockReservation.objects.values_list("id", "quantity", "released_at"))) == before
    page = c.get("/hub/stock/")
    assert page.status_code == (200 if role in PERMISSIONS["hub.view"] else 403), role
    if page.status_code == 200:
        html = page.content.decode()
        assert ('action="/hub/stock/receive/"' in html) == move_ok
        assert ('action="/hub/stock/adjust/"' in html) == adjust_ok
        assert ('action="/hub/stock/reserve/"' in html) == reserve_ok


# ------------------------------------------------------------------ non-interference (8.1, 8.5)
def _safety(visit_codes: list[str]) -> dict[str, Any]:
    from readiness.evaluator import evaluate_readiness

    def readiness(code: str) -> Any:
        r = evaluate_readiness(Visit.objects.get(code=code))
        return (r.ready, r.label, sorted(b["code"] for b in r.blockers), sorted(r.evidence_ids))

    return {
        "evidence": list(ReadinessEvidence.objects.order_by("id").values_list("id", "kind", "result")),
        "authorizations": list(DepartureAuthorization.objects.order_by("id").values_list("id", "expires_at")),
        "holds": list(Hold.objects.order_by("id").values_list("id", "released_at")),
        "custody": CustodyEvent.objects.count(),
        "eligibility": {
            r.code: fs.evaluate_eligibility(r).eligible_for_planning for r in Robot.objects.all()
        },
        "readiness": {c: readiness(c) for c in visit_codes},
    }


def test_stock_never_stands_in_for_the_departure_equipment_check():
    from tests.readiness_world import build_readiness

    w = build_readiness(prefix="ni_")
    w.tech = make_user("ni_tech", "hub_technician")
    w.p.register_person(w.ops, username=w.tech.username, code="P-8")
    st.create_item(w.ops, code="RK", name="Restraint kit", kind="reusable", unit="kits")
    st.create_item(w.ops, code="WIPE", name="Wipes", kind="consumable", unit="packs")
    # no evidence at all: its equipment check stays missing
    bare = w.new_confirmed_visit(robot="R-02", h0=14, h1=15)
    fs.place_hold(w.ops, scope="robot", target="R-03", source_type="maintenance", reason="noisy wrist")
    set_setting("stock_per_visit", "RK=1")
    set_setting("stock_minimums", "RK=5")
    before = _safety([w.visit, bare])
    st.receive(w.tech, item_code="RK", quantity=4)
    st.receive(w.tech, item_code="WIPE", quantity=4)
    for code in (w.visit, bare):
        st.reserve(w.tech, item_code="RK", visit_code=code, quantity=1)
    st.issue(w.tech, item_code="RK", quantity=1, visit_code=bare)
    st.return_stock(w.tech, item_code="RK", quantity=1, visit_code=bare)
    st.consume(w.tech, item_code="WIPE", quantity=1, visit_code=w.visit)
    st.adjust(w.tech, item_code="RK", delta=-1, reason="written off")
    derive.refresh()
    task = WorkOrder.objects.get(source_kind="stock_level")
    hs.assign_task(w.ops, task_id=task.pk, owner_code="P-8")
    hs.start_task(w.tech, task_id=task.pk)
    hs.complete_task(w.tech, task_id=task.pk)
    after = _safety([w.visit, bare])
    assert after == before
    assert "EQUIPMENT_CHECK_MISSING" in str(after["readiness"][bare]) or not after["readiness"][bare][0]


def test_completing_or_cancelling_work_orders_never_changes_stock(w):
    set_setting("stock_minimums", "RK=3,GLOVE=10")
    st.receive(w.tech, item_code="RK", quantity=2)
    st.receive(w.tech, item_code="GLOVE", quantity=4)
    st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1)
    derive.refresh()
    tasks = list(WorkOrder.objects.filter(source_kind="stock_level").order_by("id"))
    assert len(tasks) == 2
    manual = int(hs.create_task(w.ops, type="kit_low", title="Count the kits", owner_code="P-8")["entity_id"])
    snapshot = (
        list(StockMovement.objects.values_list("id", "kind", "quantity")),
        list(StockReservation.objects.values_list("id", "quantity", "released_at")),
        [(lv.code, lv.on_hand, lv.reserved) for lv in st.levels()],
    )
    hs.assign_task(w.ops, task_id=tasks[0].pk, owner_code="P-8")
    hs.start_task(w.tech, task_id=tasks[0].pk)
    hs.complete_task(w.tech, task_id=tasks[0].pk, note="restocked, I think")
    hs.cancel_task(w.ops, task_id=tasks[1].pk, reason="not needed")
    hs.start_task(w.tech, task_id=manual)
    hs.complete_task(w.tech, task_id=manual)
    derive.refresh()
    assert snapshot == (
        list(StockMovement.objects.values_list("id", "kind", "quantity")),
        list(StockReservation.objects.values_list("id", "quantity", "released_at")),
        [(lv.code, lv.on_hand, lv.reserved) for lv in st.levels()],
    )


# ------------------------------------------------------------------ restocking tasks (minimum level)
def test_below_minimum_derives_one_restocking_task_that_clears_when_restocked(w):
    st.receive(w.tech, item_code="GLOVE", quantity=6)
    st.consume(w.tech, item_code="GLOVE", quantity=4)  # 2 left
    derive.refresh()
    assert not WorkOrder.objects.filter(source_kind="stock_level").exists()  # no minimum: no task
    html = _client(w.tech).get("/hub/stock/").content.decode()
    assert "No minimum set" in html and 'data-minimums-off="1"' in html
    set_setting("stock_minimums", "GLOVE=3")
    for _ in range(3):
        derive.refresh()
    (t,) = WorkOrder.objects.filter(source_kind="stock_level")
    crossing = StockMovement.objects.filter(kind="consume").get().pk
    assert (t.type, t.source_ref, t.status) == ("kit_low", f"GLOVE:{crossing}", "open")
    assert (
        t.link_url == "/hub/stock/#item-GLOVE"
        and t.title == "Restock Nitrile gloves (GLOVE): below its minimum of 3"
    )
    page = _client(w.tech).get("/hub/").content.decode()
    assert "Open the stock page" in page and "Restock Nitrile gloves" in page
    st.receive(w.tech, item_code="GLOVE", quantity=5)
    derive.refresh()
    t.refresh_from_db()
    assert t.source_cleared_at is not None and t.status == "open"  # resolved by the record, never auto-done
    hs.cancel_task(w.ops, task_id=t.pk, reason="restocked")
    st.consume(w.tech, item_code="GLOVE", quantity=5)  # below again: a new fact, a new task
    derive.refresh()
    new = WorkOrder.objects.filter(source_kind="stock_level").exclude(pk=t.pk).get()
    assert new.source_ref == f"GLOVE:{StockMovement.objects.order_by('-id').first().pk}" and new.active
    # reserving counts: available (on hand - reserved) is what is compared with the minimum
    set_setting("stock_minimums", "RK=2")
    st.receive(w.tech, item_code="RK", quantity=2)
    derive.refresh()
    assert not WorkOrder.objects.filter(source_ref__startswith="RK:").exists()
    st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1)
    derive.refresh()
    assert WorkOrder.objects.filter(source_ref__startswith="RK:", source_cleared_at__isnull=True).count() == 1


def test_minimum_levels_are_owner_provisioned_and_validated():
    for key, value in (("consent_form_version", "CF-v1"), ("pre_visit_inspection_min", "30"),
                       ("ops_readiness_lookahead_min", "2880")):  # fmt: skip
        set_setting(key, value)  # the required ones, so only the stock keys change here
    DeploymentSetting.objects.filter(key__in=["stock_minimums", "stock_per_visit"]).delete()
    out = StringIO()
    call_command("provision_settings", stock_minimums="rk = 3, glove=10", stdout=out)
    assert DeploymentSetting.objects.get(key="stock_minimums").value == "RK=3,GLOVE=10"
    for bad in ("RK", "RK=-1", "RK=3,RK=4", "RK=1000001", "R/K=1"):
        with pytest.raises(MgmtError, match="Nothing was changed"):
            call_command("provision_settings", stock_per_visit=bad, stdout=StringIO())
    with pytest.raises(MgmtError, match="allow-change"):
        call_command("provision_settings", stock_minimums="RK=4", stdout=StringIO())
    assert AuditEvent.objects.filter(command="fleet.provision_settings").count() == 1
    # the runtime role cannot write the setting (owner only, as every operating setting)
    from fleet.provisioning import parse_stock_levels

    assert parse_stock_levels("RK=3,GLOVE=10") == {"RK": 3, "GLOVE": 10} and parse_stock_levels("junk") == {}


# ------------------------------------------------------------------ visits: Kit short = At risk
def test_visits_that_cannot_get_their_kit_are_at_risk_in_departure_order(w):
    early, late = _tomorrow_visit(w, 9), _tomorrow_visit(w, 15)
    st.receive(w.tech, item_code="RK", quantity=1)
    assert st.visit_needs() == []  # stock_per_visit not provisioned: visits are not checked
    set_setting("stock_per_visit", "RK=1")
    needs = {n.visit_code: n for n in st.visit_needs()}
    assert set(needs) == {early, late}  # w.v1 / w.v2 are two weeks out: outside the two-day horizon
    assert not needs[early].short and needs[late].short  # served in departure order from the 1 available
    assert needs[late].reason == "Kit short: Restraint kit (1 of 1 kits not available)"
    st.reserve(w.tech, item_code="RK", visit_code=late, quantity=1)
    needs = {n.visit_code: n for n in st.visit_needs()}
    assert set(needs) == {early} and needs[early].short  # the kit is now held for the later visit
    st.issue(w.tech, item_code="RK", quantity=1, visit_code=late)
    st.return_stock(w.tech, item_code="RK", quantity=1, visit_code=late)  # back early: counted as issued
    needs = {n.visit_code: n for n in st.visit_needs()}
    assert set(needs) == {early} and not needs[early].short  # issued to the late visit; 1 back on the shelf
    # the visit page says so; it is planning information only
    page = _client(w.ops).get(f"/planning/visits/{early}/").content.decode()
    assert "not reserved yet" in page and "departure still needs the equipment check" in page
    st.receive(w.tech, item_code="RK", quantity=0 + 1)
    st.reserve(w.tech, item_code="RK", visit_code=early, quantity=1)
    assert st.visit_needs() == []
    st.adjust(w.tech, item_code="RK", delta=-1, reason="lost")
    w.p.cancel_visit(w.ops, visit_code=late, reason="customer cancelled")
    assert st.visit_needs() == []


@pytest.mark.django_db(transaction=True)
def test_today_and_the_visit_page_show_kit_short_as_at_risk():
    from tests.readiness_world import build_readiness

    w = build_readiness(prefix="td_")
    w.tech = make_user("td_tech", "hub_technician")
    st.create_item(w.ops, code="RK", name="Restraint kit", kind="reusable", unit="kits")
    set_setting("ops_readiness_lookahead_min", str(60 * 24 * 3))
    day = _local_tomorrow()
    v = w.new_confirmed_visit(robot="R-02", d=day, h0=12, h1=13)
    set_setting("stock_per_visit", "RK=1")
    c = _client(w.ops)
    body = c.get("/ops/").content.decode()
    assert "Kit short: Restraint kit" in body
    row = body.split(f">{v}<", 1)[1].split("</li>", 1)[0] if f">{v}<" in body else body
    assert "At risk" in row or "Blocked" in row
    page = c.get(f"/planning/visits/{v}/").content.decode()
    assert 'data-stock="short"' in page and "Kit short: Restraint kit" in page
    st.receive(w.tech, item_code="RK", quantity=1)
    st.reserve(w.tech, item_code="RK", visit_code=v, quantity=1)
    assert "Kit short" not in c.get("/ops/").content.decode()
    assert "1 Restraint kit reserved" in c.get(f"/planning/visits/{v}/").content.decode()


# ------------------------------------------------------------------ screens
def test_the_stock_page_shows_levels_movements_and_forms_with_selects(w):
    set_setting("stock_minimums", "GLOVE=5")
    st.receive(w.tech, item_code="RK", quantity=3, note="first count")
    st.receive(w.tech, item_code="GLOVE", quantity=4)
    st.reserve(w.tech, item_code="RK", visit_code=w.v1, quantity=1)
    c = _client(w.tech)
    html = c.get("/hub/stock/").content.decode()
    assert 'data-item="RK" data-on-hand="3" data-reserved="1" data-available="2"' in html
    assert 'data-item="GLOVE" data-on-hand="4" data-reserved="0" data-available="4" data-below="1"' in html
    for words in ("Items", "Reserved for visits", "Recent movements", "Receive", "Issue", "Return a kit",
                  "Record use", "Reserve for a visit", "Adjust after a count", "Below minimum", "No minimum set",
                  "never the departure equipment check"):  # fmt: skip
        assert words in html, words
    main = html.split("<main", 1)[-1].split("</main>")[0]
    assert "<script" not in main
    for form in ("receive", "issue", "return", "consume", "reserve", "adjust"):
        block = main.split(f'action="/hub/stock/{form}/"', 1)[1].split("</form>", 1)[0]
        assert '<select name="item"' in block and '<input name="item"' not in block, form
        assert '<input name="visit"' not in block, form
        if form in ("issue", "return", "consume", "reserve"):
            assert '<select name="visit"' in block and f'<option value="{w.v1}">' in block, form
    # a receipt through the page (one-time token), then a replay does nothing
    token = TOKEN_RE.search(html).group(1)
    data = {"_form_token": token, "item": "RK", "quantity": "2", "note": "delivery"}
    c.post("/hub/stock/receive/", data)
    c.post("/hub/stock/receive/", data)
    assert _level("RK").on_hand == 5
    # a refusal through the page: plain words and "Nothing was changed"
    token = TOKEN_RE.search(c.get("/hub/stock/").content.decode()).group(1)
    r = c.post("/hub/stock/issue/", {"_form_token": token, "item": "RK", "quantity": "9"}, follow=True)
    text = r.content.decode()
    assert "Only 5 kits of Restraint kit (RK) are on hand" in text and "Nothing was changed." in text
    token = TOKEN_RE.search(c.get("/hub/stock/").content.decode()).group(1)
    r = c.post(
        "/hub/stock/adjust/", {"_form_token": token, "item": "RK", "delta": "-1", "reason": ""}, follow=True
    )
    assert "Nothing was changed." in r.content.decode() and _level("RK").on_hand == 5
    # release page for a reservation, then the item page for the ops lead
    rid = StockReservation.objects.get().pk
    rel = _client(w.tech)
    page = rel.get(f"/hub/stock/reservations/{rid}/release/").content.decode()
    token = TOKEN_RE.search(page).group(1)
    rel.post(f"/hub/stock/reservations/{rid}/release/", {"_form_token": token, "reason": "visit moved"})
    assert StockReservation.objects.get().released_reason == "visit moved"
    ops = _client(w.ops)
    token = TOKEN_RE.search(ops.get("/hub/stock/items/").content.decode()).group(1)
    ops.post("/hub/stock/items/", {"_form_token": token, "code": "rc", "name": "Recovery kit", "kind": "reusable",
                                   "unit": "kits"})  # fmt: skip
    assert StockItem.objects.filter(code="RC", kind="reusable").exists() and _level("RC").on_hand == 0
    assert "/hub/stock/" in _client(w.tech).get("/hub/").content.decode()  # the Hub sub-navigation


# ------------------------------------------------------------------ statement counts (8.7)
def _count(c: Client, url: str) -> int:
    assert c.get(url).status_code == 200  # warm-up: derives any new task once
    with CaptureQueriesContext(connection) as q:
        assert c.get(url).status_code == 200
    return len(q)


STOCK_PAGE_BUDGET = 20
HUB_PAGE_BUDGET = 15  # unchanged from 02A: the stock facts are part of the one refresh statement


def test_stock_page_statement_count_is_constant_in_items_movements_and_visits(monkeypatch):
    import platform_core.interaction as ia

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)  # no opportunistic token clean-up in the count
    w = _world("sc_")
    set_setting("stock_per_visit", "RK=1")
    set_setting("stock_minimums", "RK=1,GLOVE=1")

    def grow(n: int, offset: int) -> None:
        for k in range(offset, offset + n):
            code = f"K{k}"
            st.create_item(w.ops, code=code, name=f"Kit {k}", kind="reusable" if k % 2 else "consumable")
            st.receive(w.tech, item_code=code, quantity=3)
            visit = _tomorrow_visit(w, 6 + k % 12)
            st.reserve(w.tech, item_code=code, visit_code=visit, quantity=1)

    grow(2, 0)
    c = _client(w.tech)
    small = _count(c, "/hub/stock/")
    grow(10, 2)
    big = _count(c, "/hub/stock/")
    html = c.get("/hub/stock/").content.decode()
    assert html.count('data-item="') == 14 and html.count('data-reservation="') == 12
    assert small == big <= STOCK_PAGE_BUDGET, (small, big)
    print(f"/hub/stock/ statements: {small} (budget {STOCK_PAGE_BUDGET})")


def test_hub_statement_count_is_identical_with_2_and_12_restocking_tasks(monkeypatch):
    import platform_core.interaction as ia

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)
    w = _world("hc_")
    codes: list[str] = []

    def low(n: int, offset: int) -> None:
        for k in range(offset, offset + n):
            code = f"L{k}"
            st.create_item(w.ops, code=code, name=f"Low {k}", kind="consumable")
            st.receive(w.tech, item_code=code, quantity=2)
            st.consume(w.tech, item_code=code, quantity=1)
            codes.append(code)
        set_setting("stock_minimums", ",".join(f"{c}=5" for c in codes))

    c = _client(w.ops)
    low(2, 0)
    two = _count(c, "/hub/")
    assert WorkOrder.objects.filter(source_kind="stock_level").count() == 2
    low(10, 2)
    twelve = _count(c, "/hub/")
    assert WorkOrder.objects.filter(source_kind="stock_level").count() == 12
    assert two == twelve <= HUB_PAGE_BUDGET, (two, twelve)
    print(f"/hub/ statements: {two} with 2 and with 12 restocking tasks")

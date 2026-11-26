"""v2 Phase 03B-1: the regional pause and its database gates (docs/13_v2_phase03_contract.md I-9, 10.G, 13.3, 13.4
items 1 and 2, 13.5).

Proven here:

* Which pause applies (operations_active_pause, operations.pauses.active_for / board_summary): a hub pause
  overrides zone-level state; it also covers visits whose destination zone belongs to the hub; a zone pause
  covers its zone from any dispatch hub; an unknown (NULL) visit zone is covered by any zone pause at its hub.
* Authorization and departure are refused under a pause, through the services and by direct SQL as ops_app
  (readiness_issue_authorization, a raw departure INSERT); lift restores both.
* A cross-hub reassignment of a paused visit is still refused at authorization (the gate reads the visit's
  hub and destination zone at authorization time).
* Planning stays allowed under a pause and is labelled "Paused: cannot leave until lifted"; the pause preview
  lists the affected visits.
* Robots already out still record protective actions (an incident and its hold) under a pause.
* Authority by direct SQL as ops_app: a hub technician or field operator cannot pause or lift; a safety_pause
  grant at a hub can pause there (only there) and cannot lift; forged, NULL, zero, empty and role-less actors
  write nothing.
* Races, both orders, with the waiting transaction seen in pg_locks: pause vs authorization, pause vs
  departure, lift vs authorization.
* ops_app has SELECT only on operations_region_pause; the table is append-only.

SYNTHETIC data only.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from psycopg.types.json import Jsonb

from accounts.models import RoleGrant
from fleet import transfers as tr
from operations import pauses
from operations.models import RegionPause
from planning import services as ps
from planning import travel, zones
from planning.models import Person, Visit
from platform_core import testhooks
from platform_core import workspace as ws
from platform_core.commands import Conflict, PermissionDenied
from platform_core.locks import SETTINGS_LOCK_KEY
from platform_core.models import AuditEvent, Hub, Zone
from readiness import commands as rc
from readiness.models import DepartureAuthorization, DepartureEvent
from tests.conftest import make_user
from tests.planning_world import DAY, TZ, at, plan
from tests.readiness_world import build_readiness, record_all_evidence
from tests.test_readiness_authz_boundary import issue, live_args
from tests.test_v2_03a_transfers import Background, _app_conn, _owner_conn

pytestmark = pytest.mark.django_db(transaction=True)

PAUSE_SQL = "SELECT operations_pause_region(%s, %s, %s, %s, %s)"
LIFT_SQL = "SELECT operations_lift_pause(%s, %s, %s, %s)"
PAUSED = "Paused: cannot leave until lifted"


# ------------------------------------------------------------------ worlds and helpers
def _zone_world(prefix: str) -> SimpleNamespace:
    """The readiness world at HUB-1 with zones ZA (the hub's own zone) and ZB (home S-1), travel ZA <-> ZB 30 min
    (demo), and w.visit re-planned (it takes zone ZB and the matrix), confirmed and with all its evidence again."""
    w = build_readiness(prefix=prefix)
    w.hub = Hub.objects.get()
    w.za = Zone.objects.get(
        pk=int(zones.create_zone(w.ops, hub_code=w.hub.code, code="ZA", name="Hub side")["entity_id"])
    )
    w.zb = Zone.objects.get(
        pk=int(zones.create_zone(w.ops, hub_code=w.hub.code, code="ZB", name="Hills")["entity_id"])
    )
    zones.set_hub_home_zone(w.ops, hub_code=w.hub.code, zone_code="ZA")
    for a, b in ((w.za, w.zb), (w.zb, w.za)):
        travel.set_travel_time(w.ops, from_zone_id=a.pk, to_zone_id=b.pk, minutes=30, source="demo")
    zones.set_site_zone(w.ops, site_code="S-1", hub_code=w.hub.code, zone_code="ZB", reason="SYN: home zone")
    plan(w, w.visit)
    ps.confirm_visit(w.ops, visit_code=w.visit)
    record_all_evidence(w, w.visit)
    assert Visit.objects.get(code=w.visit).zone_id == w.zb.pk
    return w


def _pause(w: SimpleNamespace, zone: str | None = None, hub: str | None = None, by: Any = None) -> int:
    return int(
        pauses.pause_region(by or w.ops, hub_code=hub or w.hub.code, zone_code=zone, reason="SYN: storm")[
            "entity_id"
        ]
    )


def _lift(w: SimpleNamespace, pid: int) -> None:
    pauses.lift_pause(w.ops, pause_id=pid, reason="SYN: clear again")


def _owner_waiting() -> bool:
    """True once another backend of this database waits for a lock (pg_locks, not granted)."""
    for _ in range(200):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE NOT l.granted AND a.datname = current_database() AND l.pid <> pg_backend_pid()"
            )
            if cur.fetchone()[0] > 0:
                return True
        time.sleep(0.05)
    return False


def _waits(pid: int) -> bool:
    for _ in range(200):
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_locks WHERE pid = %s AND NOT granted", [pid])
            if cur.fetchone()[0] > 0:
                return True
        time.sleep(0.05)
    return False


def _raw_departure(cur: Any, a: DepartureAuthorization, recorded_by: Any) -> None:
    """A direct departure INSERT as ops_app (the settings lock first, as the departure guard requires)."""
    with cur.connection.transaction():
        cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", [SETTINGS_LOCK_KEY])
        cur.execute(
            "INSERT INTO readiness_departureevent (authorization_id, visit_id, robot_id, recorded_by_id, "
            "guard_snapshot) VALUES (%s, %s, %s, %s, %s)",
            [a.pk, a.visit_id, a.robot_id, recorded_by.pk, Jsonb(a.guard_snapshot)],
        )


def _counts() -> tuple[int, int]:
    return RegionPause.objects.count(), AuditEvent.objects.filter(command__startswith="operations.").count()


# ================================================================== which pause applies
def test_a_hub_pause_overrides_zone_pauses_and_an_unknown_zone_is_covered():
    ops = make_user("wp_ops", "ops_lead")
    a = Hub.objects.create(code="HA", name="Hub A", time_zone=TZ)
    b = Hub.objects.create(code="HB", name="Hub B", time_zone=TZ)
    a1, a2 = (
        Zone.objects.create(hub=a, code="A1", name="A one"),
        Zone.objects.create(hub=a, code="A2", name="A two"),
    )
    b1 = Zone.objects.create(hub=b, code="B1", name="B one")
    w = SimpleNamespace(ops=ops, hub=a)
    zp = _pause(w, zone="A2")
    assert pauses.active_for(a.pk, a1.pk) is None
    hit = pauses.active_for(a.pk, a2.pk)
    assert hit is not None and (hit.pause_id, hit.scope) == (zp, "zone")
    assert hit.where == "zone A two (A2) of Hub A (HA)"
    assert pauses.active_for(a.pk, None).pause_id == zp  # unknown zone: covered by any zone pause at its hub
    assert pauses.active_for(b.pk, None) is None  # ... at ITS hub only
    assert pauses.active_for(b.pk, a2.pk).pause_id == zp  # the destination zone, whatever the dispatch hub
    with pytest.raises(Conflict, match="already paused"):
        _pause(w, zone="A2")
    hp = _pause(w)  # the whole hub
    with connection.cursor() as cur:
        cur.execute("SELECT pause_id, scope FROM operations_active_pause(%s, %s)", [a.pk, a2.pk])
        assert cur.fetchall() == [(hp, "hub")]  # the hub pause overrides: only it is returned
    assert pauses.active_for(b.pk, a1.pk).scope == "hub"  # a destination in the paused hub's area
    assert pauses.active_for(b.pk, b1.pk) is None
    with CaptureQueriesContext(connection) as q:
        summary = pauses.board_summary([a.pk, b.pk])
    assert len(q.captured_queries) == 1
    assert [(p.pause_id, p.scope) for p in summary[a.pk]] == [(hp, "hub"), (zp, "zone")] and summary[
        b.pk
    ] == []
    _lift(w, hp)
    assert pauses.active_for(a.pk, a2.pk).scope == "zone"  # the zone pause is still in force
    assert pauses.active_for(b.pk, a1.pk) is None
    with pytest.raises(Conflict, match="already lifted"):
        _lift(w, hp)


def test_the_pause_table_is_append_only_and_ops_app_may_only_read_it():
    ops = make_user("ap_ops", "ops_lead")
    h = Hub.objects.create(code="HA", name="Hub A", time_zone=TZ)
    pid = _pause(SimpleNamespace(ops=ops, hub=h))
    with _app_conn() as conn, conn.cursor() as cur:
        for sql in (
            "UPDATE operations_region_pause SET reason = 'x'",
            "DELETE FROM operations_region_pause",
            f"INSERT INTO operations_region_pause (hub_id, kind, reason, actor_id, created_at) VALUES ({h.pk}, 'lift', "
            f"'x', {ops.pk}, now())",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql)
        cur.execute("SELECT count(*) FROM operations_region_pause")
        assert cur.fetchone()[0] == 1
    with connection.cursor() as cur, pytest.raises(Exception):  # noqa: B017 - the owner too (reject_mutation)
        cur.execute("UPDATE operations_region_pause SET reason = 'x' WHERE id = %s", [pid])
    with connection.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege(%s, 'operations_region_pause', 'SELECT'), "
            "has_table_privilege(%s, 'operations_region_pause', 'INSERT, UPDATE, DELETE, TRUNCATE'), "
            "has_table_privilege(%s, 'planning_travel_time', 'SELECT'), "
            "has_table_privilege(%s, 'planning_travel_time', 'INSERT, UPDATE, DELETE, TRUNCATE')",
            ["ops_app"] * 4,
        )
        assert cur.fetchone() == (True, False, True, False)


# ================================================================== the gates
def test_authorization_and_departure_are_refused_under_a_pause_by_service_and_by_sql_and_lift_restores():
    w = _zone_world("pg_")
    pid = _pause(w)
    with pytest.raises(Conflict, match=PAUSED):
        rc.authorize_departure(w.ops, visit_code=w.visit)
    with _app_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match=f"authorization refused: {PAUSED}"):
            issue(cur, live_args(w))
    assert not DepartureAuthorization.objects.exists()
    _lift(w, pid)
    code = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    auth = DepartureAuthorization.objects.get(code=code)
    zp = _pause(w, zone="ZB")  # now a zone pause on the visit's destination zone
    with pytest.raises(Conflict, match=PAUSED):
        rc.record_departure(w.ops, authorization_code=code)
    with _app_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match=f"departure refused: {PAUSED}"):
            _raw_departure(cur, auth, w.ops)
    assert not DepartureEvent.objects.exists()
    _lift(w, zp)
    rc.record_departure(w.ops, authorization_code=code)
    assert DepartureEvent.objects.filter(authorization=auth).count() == 1


def test_an_unknown_zone_visit_is_paused_by_any_zone_pause_at_its_hub():
    w = build_readiness(prefix="pu_")  # w.visit has no zone (its home has none)
    w.hub = Hub.objects.get()
    Zone.objects.create(hub=w.hub, code="ZQ", name="Quarry")
    assert Visit.objects.get(code=w.visit).zone_id is None
    pid = _pause(w, zone="ZQ")
    with pytest.raises(Conflict, match="zone is unknown"):
        rc.authorize_departure(w.ops, visit_code=w.visit)
    with (
        _app_conn() as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match=PAUSED),
    ):
        issue(cur, live_args(w))
    _lift(w, pid)
    assert rc.authorize_departure(w.ops, visit_code=w.visit)["code"]


def test_a_cross_hub_reassignment_of_a_paused_visit_is_still_refused_at_authorization():
    """A visit to zone ZB of HUB-1, dispatched first from HUB-1. HUB-1 is paused. The visit is moved to hub HB
    (robot R-02 and VAN-2 transferred there), planned, confirmed and given all its evidence: authorization is
    still refused, because the gate follows the visit's destination zone, whose hub is paused. Lifting the pause
    is the only change needed for it to be issued."""
    w = _zone_world("px_")
    admin = make_user("px_admin", "admin")
    ws.create_hub(admin, code="HB", name="Hub B", area="", time_zone=TZ)
    hb = Hub.objects.get(code="HB")
    Zone.objects.create(hub=hb, code="ZH", name="Hub B side")
    zones.set_hub_home_zone(w.ops, hub_code="HB", zone_code="ZH")
    zh = Zone.objects.get(hub=hb, code="ZH")
    for a, b in ((zh, w.zb), (w.zb, zh)):
        travel.set_travel_time(w.ops, from_zone_id=a.pk, to_zone_id=b.pk, minutes=40, source="demo")
    for user in (w.field, w.field2):  # the crew records evidence at HB too
        RoleGrant.objects.create(user=user, role="field_operator", hub=hb)
    # 03B carry (planning/0015): the crew must be based at the visit's hub, so P-1 and P-2 work at HB here
    Person.objects.filter(code__in=["P-1", "P-2"]).update(hub=hb)
    for kind, code in (("robot", "R-02"), ("vehicle", "VAN-2")):
        tid = int(
            tr.start_transfer(w.ops, kind=kind, code=code, to_hub_code="HB", reason="SYN: help HB")["id"]
        )
        tr.receive_transfer(w.ops, transfer_id=tid)
    code = str(
        ps.request_visit(
            w.ops, site_code="S-1", on_site_start=at(DAY, 15), on_site_end=at(DAY, 16), tasks=["T-04"],
            required_scopes=["presence"], hub_code=w.hub.code,
        )["entity_id"]
    )  # fmt: skip
    pid = _pause(w)  # HUB-1
    with _app_conn() as conn, conn.cursor() as cur:  # the reassignment (a direct change of the dispatch hub)
        cur.execute("UPDATE planning_visit SET hub_id = %s WHERE code = %s", [hb.pk, code])
    v = Visit.objects.get(code=code)
    assert (v.hub_id, v.zone_id) == (hb.pk, w.zb.pk)
    out = plan(w, code, robot="R-02", vehicle="VAN-2")
    assert out["feasibility"] == "feasible" and out["paused"], out  # planning is allowed, and says so
    ps.confirm_visit(w.ops, visit_code=code)
    record_all_evidence(w, code, robot="R-02")
    with pytest.raises(Conflict, match=PAUSED):
        rc.authorize_departure(w.ops, visit_code=code)
    with (
        _app_conn() as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match=PAUSED),
    ):
        issue(cur, live_args(SimpleNamespace(visit=code, ops=w.ops)))
    assert not DepartureAuthorization.objects.filter(visit=v).exists()
    _lift(w, pid)
    assert rc.authorize_departure(w.ops, visit_code=code)["code"]


def test_planning_is_allowed_under_a_pause_and_labelled():
    w = _zone_world("pl_")
    pid = _pause(w, zone="ZB")
    code = str(
        ps.request_visit(
            w.ops, site_code="S-1", on_site_start=at(DAY, 15), on_site_end=at(DAY, 16), tasks=["T-04"],
            required_scopes=["presence"],
        )["entity_id"]
    )  # fmt: skip
    out = plan(w, code, robot="R-02", vehicle="VAN-2")
    assert out["feasibility"] == "feasible" and out["paused"]
    assert any(m.startswith(PAUSED) for m in out["blockers"])
    assert Visit.objects.get(code=code).status == "planned"
    c = Client()
    c.force_login(w.ops)
    assert PAUSED in c.get(f"/planning/visits/{code}/").content.decode()
    preview = pauses.affected_visits(w.hub.pk, w.zb.pk)
    assert {a.code for a in preview} == {w.visit, code}
    _lift(w, pid)
    assert pauses.active_for(w.hub.pk, w.zb.pk) is None


def test_robots_already_out_still_record_protective_actions_under_a_pause():
    from execution import incidents as inc
    from fleet.models import Hold
    from tests.execution_world import build_execution

    w = build_execution(prefix="po_")
    w.hub = Hub.objects.get()
    _pause(w)
    from django.utils import timezone

    res = inc.report_incident(
        w.field,
        robot_code=w.robot.code,
        event_type="fall",
        description="SYNTHETIC: robot sat down unexpectedly",
        occurred_at=timezone.now(),
        knowledge_source="witnessed",
    )
    assert res["entity_id"]
    assert Hold.objects.filter(robot=w.robot, released_at__isnull=True, blocking=True).exists()


# ================================================================== authority, as ops_app
def test_hub_roles_cannot_pause_or_lift_and_safety_pause_can_pause_only_at_its_hub_and_never_lift():
    ops = make_user("au_ops", "ops_lead")
    admin = make_user("au_admin", "admin")
    a = Hub.objects.create(code="HA", name="Hub A", time_zone=TZ)
    b = Hub.objects.create(code="HB", name="Hub B", time_zone=TZ)
    tech, field, sp = make_user("au_tech"), make_user("au_field"), make_user("au_safety")
    RoleGrant.objects.create(user=tech, role="hub_technician", hub=a)
    RoleGrant.objects.create(user=field, role="field_operator", hub=a)
    existing = _pause(SimpleNamespace(ops=ops, hub=b))  # a pause to try to lift
    before = _counts()
    with _app_conn() as conn, conn.cursor() as cur:
        for who in (tech, field):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(PAUSE_SQL, [who.pk, a.pk, None, "SYN", ""])
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="regional ops lead"):
                cur.execute(LIFT_SQL, [who.pk, existing, "SYN", ""])
        assert _counts() == before
        # the safety_pause grant is a separate, audited grant AT one hub (accounts_grant_role, by the admin)
        with pytest.raises(psycopg.errors.CheckViolation, match="hub required"):
            cur.execute(
                "SELECT accounts_grant_role(%s, %s, 'safety_pause', NULL, NULL, NULL, '')", [admin.pk, sp.pk]
            )
        cur.execute(
            "SELECT accounts_grant_role(%s, %s, 'safety_pause', %s, NULL, NULL, '')", [admin.pk, sp.pk, a.pk]
        )
        cur.execute(PAUSE_SQL, [sp.pk, a.pk, None, "SYN: smoke nearby", "req-sp"])
        own = cur.fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="Hub B"):
            cur.execute(PAUSE_SQL, [sp.pk, b.pk, None, "SYN", ""])
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="regional ops lead"):
            cur.execute(LIFT_SQL, [sp.pk, own, "SYN", ""])
        cur.execute(LIFT_SQL, [ops.pk, own, "SYN: all clear", ""])  # the regional ops lead can
    assert RegionPause.objects.filter(kind="lift", pause_id=own).count() == 1
    audit = AuditEvent.objects.get(command="operations.pause_region", entity_id=str(own))
    assert (audit.actor_id, audit.payload["hub"], audit.request_id) == (sp.pk, "HA", "req-sp")
    # the services say the same (permission checked at the record's hub)
    with pytest.raises(PermissionDenied):
        pauses.pause_region(tech, hub_code="HA", zone_code=None, reason="SYN")
    with pytest.raises(PermissionDenied):
        pauses.lift_pause(sp, pause_id=existing, reason="SYN")
    with pytest.raises(PermissionDenied):
        pauses.pause_region(sp, hub_code="HB", zone_code=None, reason="SYN")


@pytest.mark.parametrize("actor", ["NULL", "0", "forged", "empty", "no_role"])
def test_forged_null_zero_and_empty_actors_write_nothing(actor):
    ops = make_user("fa_ops", "ops_lead")
    h = Hub.objects.create(code="HA", name="Hub A", time_zone=TZ)
    z = Zone.objects.create(hub=h, code="Z1", name="Z one")
    existing = _pause(SimpleNamespace(ops=ops, hub=h))
    a = {
        "NULL": "NULL::bigint",
        "0": "0::bigint",
        "forged": "987654321",
        "empty": "''",
        "no_role": str(make_user("fa_norole").pk),
    }[actor]
    before = _counts()
    expected = (
        psycopg.errors.InvalidTextRepresentation if actor == "empty" else psycopg.errors.InsufficientPrivilege
    )
    with _app_conn() as conn, conn.cursor() as cur:
        for sql in (
            f"SELECT operations_pause_region({a}, {h.pk}, NULL, 'SYN', '')",
            f"SELECT operations_pause_region({a}, {h.pk}, {z.pk}, 'SYN', '')",
            f"SELECT operations_lift_pause({a}, {existing}, 'SYN', '')",
            f"SELECT planning_set_travel_time({a}, {z.pk}, {z.pk}, 10, 'demo', NULL, '')",
        ):
            with pytest.raises(expected):
                cur.execute(sql)
    assert _counts() == before
    assert pauses.active_for(h.pk, None).pause_id == existing


# ================================================================== races, both orders, waiter seen
@pytest.mark.parametrize("first", ["pause", "authorization"])
def test_a_pause_and_an_authorization_are_ordered_both_ways(first):
    """pause first: the pause (ops_app, settings Sh -> hub U) is held uncommitted; authorize_departure waits on
    the hub row at its gate (pg_locks), then sees the committed pause and is refused.
    authorization first: authorize_departure pauses before its commit (holding the hub FOR SHARE); the pause
    waits for the hub row (pg_locks), then commits after the authorization."""
    w = _zone_world(f"ra{first[0]}_")
    if first == "pause":
        holder = _app_conn(autocommit=False)
        try:
            with holder.cursor() as cur:
                cur.execute(PAUSE_SQL, [w.ops.pk, w.hub.pk, None, "SYN: race", ""])
            bg = Background(lambda: rc.authorize_departure(w.ops, visit_code=w.visit))
            assert _owner_waiting(), f"the authorization never waited ({bg.error!r})"
            assert bg.thread.is_alive()
            holder.commit()
            bg.join()
        finally:
            holder.close()
        assert isinstance(bg.error, Conflict) and PAUSED in str(bg.error), (bg.result, bg.error)
        assert not DepartureAuthorization.objects.exists()
        assert RegionPause.objects.filter(kind="pause").count() == 1
    else:
        paused, go_on = threading.Event(), threading.Event()

        def hook() -> None:
            if threading.current_thread().name == "auth":
                paused.set()
                assert go_on.wait(20)

        testhooks.register("before_commit:readiness.authorize_departure", hook)
        try:
            out: dict[str, Any] = {}

            def authorize() -> None:
                try:
                    out["auth"] = rc.authorize_departure(w.ops, visit_code=w.visit)
                except Exception as exc:  # noqa: BLE001
                    out["auth"] = exc
                finally:
                    connection.close()

            t1 = threading.Thread(target=authorize, name="auth")
            t1.start()
            assert paused.wait(20)
            waiter = _app_conn()

            def pause() -> None:
                try:
                    with waiter.cursor() as cur:
                        cur.execute(PAUSE_SQL, [w.ops.pk, w.hub.pk, None, "SYN: race", ""])
                    out["pause"] = "committed"
                except psycopg.Error as exc:
                    out["pause"] = exc

            t2 = threading.Thread(target=pause)
            t2.start()
            try:
                assert _waits(waiter.info.backend_pid), "the pause never waited"
                assert "pause" not in out
            finally:
                go_on.set()
                t1.join(30)
                t2.join(30)
                waiter.close()
        finally:
            testhooks.clear()
        assert isinstance(out["auth"], dict), out["auth"]
        assert out["pause"] == "committed", out["pause"]
        auth = DepartureAuthorization.objects.get(code=out["auth"]["code"])
        assert auth.issued_at < RegionPause.objects.get(kind="pause").created_at
        # the committed pause now refuses the departure of that earlier authorization
        with pytest.raises(Conflict, match=PAUSED):
            rc.record_departure(w.ops, authorization_code=auth.code)


@pytest.mark.parametrize("first", ["pause", "departure"])
def test_a_pause_and_a_departure_are_ordered_both_ways(first):
    """pause first: the pause is held uncommitted; record_departure waits on the hub row at the departure gate
    (pg_locks), then is refused, and nothing departs. departure first: record_departure pauses before its commit;
    the pause waits (pg_locks) and commits after the departure, which stands (a pause never erases one)."""
    w = _zone_world(f"rd{first[0]}_")
    code = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    if first == "pause":
        holder = _app_conn(autocommit=False)
        try:
            with holder.cursor() as cur:
                cur.execute(PAUSE_SQL, [w.ops.pk, w.hub.pk, w.zb.pk, "SYN: race", ""])
            bg = Background(lambda: rc.record_departure(w.ops, authorization_code=code))
            assert _owner_waiting(), f"the departure never waited ({bg.error!r})"
            assert bg.thread.is_alive()
            holder.commit()
            bg.join()
        finally:
            holder.close()
        assert isinstance(bg.error, Conflict) and PAUSED in str(bg.error), (bg.result, bg.error)
        assert not DepartureEvent.objects.exists()
    else:
        paused, go_on = threading.Event(), threading.Event()

        def hook() -> None:
            if threading.current_thread().name == "depart":
                paused.set()
                assert go_on.wait(20)

        testhooks.register("before_commit:readiness.record_departure", hook)
        try:
            out: dict[str, Any] = {}

            def depart() -> None:
                try:
                    out["dep"] = rc.record_departure(w.ops, authorization_code=code)
                except Exception as exc:  # noqa: BLE001
                    out["dep"] = exc
                finally:
                    connection.close()

            t1 = threading.Thread(target=depart, name="depart")
            t1.start()
            assert paused.wait(20)
            waiter = _app_conn()

            def pause() -> None:
                try:
                    with waiter.cursor() as cur:
                        cur.execute(PAUSE_SQL, [w.ops.pk, w.hub.pk, w.zb.pk, "SYN: race", ""])
                    out["pause"] = "committed"
                except psycopg.Error as exc:
                    out["pause"] = exc

            t2 = threading.Thread(target=pause)
            t2.start()
            try:
                assert _waits(waiter.info.backend_pid), "the pause never waited"
                assert "pause" not in out
            finally:
                go_on.set()
                t1.join(30)
                t2.join(30)
                waiter.close()
        finally:
            testhooks.clear()
        assert isinstance(out["dep"], dict), out["dep"]
        assert out["pause"] == "committed", out["pause"]
        assert DepartureEvent.objects.count() == 1


@pytest.mark.parametrize("first", ["lift", "authorization"])
def test_a_lift_and_an_authorization_are_ordered_both_ways(first):
    """Both sides by direct SQL as ops_app (the service would refuse early, from its own unlocked read).
    lift first: the lift is held uncommitted; readiness_issue_authorization waits on the hub row at its gate
    (pg_locks), then sees the pause lifted and is issued. authorization first: the authorization runs under the
    active pause and is refused, its transaction still open (holding the hub FOR SHARE); the lift waits
    (pg_locks) until it ends, then commits; a new authorization is then issued."""
    w = _zone_world(f"rl{first[0]}_")
    pid = _pause(w)
    args = live_args(w)
    if first == "lift":
        holder = _app_conn(autocommit=False)
        waiter = _app_conn()
        try:
            with holder.cursor() as cur:
                cur.execute(LIFT_SQL, [w.ops.pk, pid, "SYN: clear", ""])
            out: dict[str, Any] = {}

            def authorize() -> None:
                try:
                    with waiter.cursor() as cur:
                        out["auth"] = issue(cur, args)
                except psycopg.Error as exc:
                    out["auth"] = exc

            t = threading.Thread(target=authorize)
            t.start()
            assert _waits(waiter.info.backend_pid), "the authorization never waited"
            assert "auth" not in out
            holder.commit()
            t.join(30)
        finally:
            holder.close()
            waiter.close()
        assert isinstance(out["auth"], tuple), out["auth"]
        assert DepartureAuthorization.objects.count() == 1
    else:
        # The authorization's gate step (operations_pause_gate_problem: settings Sh -> visit S -> hub S, then the
        # check) held uncommitted by the owner, as in the 03A transfer race: a real authorization under the
        # active pause raises at that step, and PostgreSQL releases an aborted transaction's locks at the error,
        # so the step itself is what holds the hub row here.
        holder = _owner_conn(autocommit=False)
        waiter = _app_conn()
        out = {}
        try:
            with holder.cursor() as cur:
                cur.execute(
                    "SELECT operations_pause_gate_problem(id) FROM planning_visit WHERE code = %s", [w.visit]
                )
                assert PAUSED in cur.fetchone()[0]  # under the active pause the gate refuses

            def lift() -> None:
                try:
                    with waiter.cursor() as cur:
                        cur.execute(LIFT_SQL, [w.ops.pk, pid, "SYN: clear", ""])
                    out["lift"] = "committed"
                except psycopg.Error as exc:
                    out["lift"] = exc

            t = threading.Thread(target=lift)
            t.start()
            assert _waits(waiter.info.backend_pid), "the lift never waited"
            assert "lift" not in out
            holder.rollback()
            t.join(30)
        finally:
            holder.close()
            waiter.close()
        assert out["lift"] == "committed", out["lift"]
        assert not DepartureAuthorization.objects.exists()
        with _app_conn() as conn, conn.cursor() as cur:
            issue(cur, live_args(w))
        assert DepartureAuthorization.objects.count() == 1


# ================================================================== S4 pages
def _token(html: str) -> str:
    import re

    m = re.search(r'name="_form_token" value="([^"]+)"', html)
    assert m, "no form token"
    return m.group(1)


def test_the_pause_pages_preview_the_affected_visits_then_pause_and_lift():
    w = _zone_world("pp_")
    c = Client()
    c.force_login(w.ops)
    html = c.get("/planning/pause/").content.decode()
    assert "Nothing is paused" in html and "Pause zone Hills" in html and "Main hub" in html
    page = c.get(f"/planning/pause/{w.hub.code}/ZB/").content.decode()
    assert 'data-affected="1"' in page and "This blocks authorization and departure for 1 visit" in page
    assert w.visit in page
    r = c.post(
        f"/planning/pause/{w.hub.code}/ZB/", {"reason": "SYN: road closed", "_form_token": _token(page)}
    )
    assert r.status_code == 400 and not RegionPause.objects.exists()  # the confirmation tick is required
    page = r.content.decode()
    r = c.post(
        f"/planning/pause/{w.hub.code}/ZB/",
        {"reason": "SYN: road closed", "confirm": "on", "_form_token": _token(page)},
    )
    assert r.status_code == 302, r.content.decode()[:500]
    p = RegionPause.objects.get(kind="pause")
    assert (p.zone_id, p.reason) == (w.zb.pk, "SYN: road closed")
    html = c.get("/planning/pause/").content.decode()
    assert "◆ Paused" in html and "zone Hills (ZB) of Main hub (HUB-1)" in html and "SYN: road closed" in html
    lift = c.get(f"/planning/pause/lift/{p.pk}/").content.decode()
    r = c.post(
        f"/planning/pause/lift/{p.pk}/", {"reason": "SYN: open", "confirm": "on", "_form_token": _token(lift)}
    )
    assert r.status_code == 302 and RegionPause.objects.filter(kind="lift", pause=p).exists()
    # a hub technician sees the page but no pause action, and the confirm page refuses
    tech = make_user("pp_tech", "hub_technician")
    c2 = Client()
    c2.force_login(tech)
    assert c2.get(f"/planning/pause/{w.hub.code}/").status_code == 403

"""v2 Phase 03A-2: robot location, hub transfers and field commands checked at the run's hub
(docs/13_v2_phase03_contract.md 11.4, 11.5, 11.6, corrections 10.A and 10.C, exit gates 10.I items 3 and 4).

Proven here:

* Location states (hub with its hub, vehicle, site, transfer_in_transit, unaccounted, unknown); no custody history
  = the home hub, verified; hub-end custody events record the hub, set by the database.
* A robot in transit, at a site or unaccounted is never eligible or proposed at either hub: eligibility view,
  Python eligibility, the options list, the planner and direct SQL as ops_app (revision, reservation, confirm).
* A transfer racing a booking and an authorization, in both orders, with the waiter seen in pg_locks.
* Two simultaneous receipts: exactly one wins.
* Holds survive every stage; a receipt never clears one.
* An authorization issued before a transfer fails at departure afterwards (robot version bumped, robot in transit).
* Refused and abandoned paths; an abandoned transfer can never be received at the destination.
* Stock reconciliation through transfer out and in; in transit the quantity is available at neither hub.
* A vehicle with an open or a future trip cannot be transferred; a vehicle in transit cannot be booked.
* Direct SQL as ops_app with forged, NULL, zero and empty actors, and a technician of the wrong hub: refused.
* Field commands: a field operator granted only at another hub cannot record steps on the run, through the services
  or direct SQL; a regional ops lead can.

SYNTHETIC data only.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.db import connection
from django.test import Client

from accounts.models import RoleGrant
from execution import logistics as lg
from execution.models import CustodyEvent, ExecutionRun
from fleet import services as fs
from fleet import transfers as tr
from fleet.location import robot_location
from fleet.models import Hold, HubTransfer, Robot
from hub import stock as st
from hub.models import StockItem, StockMovement
from planning import options as popt
from planning import services as ps
from planning.models import Resource, Vehicle, Visit
from platform_core import testhooks
from platform_core import workspace as ws
from platform_core.commands import Conflict, PermissionDenied
from platform_core.models import AuditEvent, Hub
from readiness import commands as rc
from tests.conftest import make_user
from tests.execution_world import (
    build_execution,
    raw_custody,
    raw_incident,
    raw_session,
    raw_site_check,
    raw_trip_event,
    raw_verify,
    to_site,
)
from tests.planning_world import DAY, TZ, at, plan
from tests.readiness_world import build_readiness

APP = settings.DB_APP_ROLE
TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')


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


def _owner_conn(autocommit: bool = True) -> psycopg.Connection:
    s = connection.settings_dict
    return psycopg.connect(
        host=s["HOST"],
        port=s["PORT"],
        dbname=s["NAME"],
        user=s["USER"],
        password=s["PASSWORD"],
        autocommit=autocommit,
    )


def _hubs(w: SimpleNamespace, prefix: str) -> SimpleNamespace:
    """A second hub HB next to HUB-1 (created by the database with the first record), with a technician and a
    field operator granted ONLY at HB, and a technician granted only at HUB-1."""
    w.hub_a = Hub.objects.get()
    w.admin = make_user(f"{prefix}admin", "admin")
    ws.create_hub(w.admin, code="HB", name="Hub B", area="", time_zone=TZ)
    w.hub_b = Hub.objects.get(code="HB")
    w.tech_a = make_user(f"{prefix}tech_a")
    RoleGrant.objects.create(user=w.tech_a, role="hub_technician", hub=w.hub_a)
    w.tech_b = make_user(f"{prefix}tech_b")
    RoleGrant.objects.create(user=w.tech_b, role="hub_technician", hub=w.hub_b)
    w.field_b = make_user(f"{prefix}field_b")
    RoleGrant.objects.create(user=w.field_b, role="field_operator", hub=w.hub_b)
    return w


def _world(prefix: str) -> SimpleNamespace:
    """The readiness world at HUB-1 (R-01 booked on the confirmed visit w.visit with all evidence; R-02 and R-03
    free) plus hub HB."""
    return _hubs(build_readiness(prefix=prefix), prefix)


def _xworld(prefix: str) -> SimpleNamespace:
    """The execution world (R-01 departed on w.run) plus hub HB."""
    return _hubs(build_execution(prefix=prefix), prefix)


def _robot(code: str) -> Robot:
    return Robot.objects.get(code=code)


def _start(
    w: SimpleNamespace, code: str = "R-02", to: str = "HB", by: Any = None, kind: str = "robot"
) -> int:
    return int(
        tr.start_transfer(by or w.tech_a, kind=kind, code=code, to_hub_code=to, reason="SYN: rebalance")["id"]
    )


def _state(tid: int) -> str:
    return HubTransfer.objects.filter(transfer_id=tid).order_by("-seq").first().state  # type: ignore[union-attr]


def _eligibility(robot_id: int) -> tuple[bool, list[str]]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT eligible, reasons FROM operations_robot_eligibility WHERE robot_id = %s", [robot_id]
        )
        e, reasons = cur.fetchone()
    return bool(e), list(reasons)


def _waits(pid: int) -> bool:
    for _ in range(200):
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_locks WHERE pid = %s AND NOT granted", [pid])
            if cur.fetchone()[0] > 0:
                return True
        time.sleep(0.05)
    return False


class Background:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            self.result = fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            self.error = exc
        finally:
            connection.close()

    def join(self) -> None:
        self.thread.join(30)
        assert not self.thread.is_alive()


def _resource(robot: Robot) -> int:
    return Resource.objects.get_or_create(kind="robot", robot=robot)[0].pk


BOOK_SQL = (
    "INSERT INTO planning_reservation (resource_id, revision_id, purpose, starts_at, ends_at, released_reason) "
    "VALUES (%s, %s, 'on_site', now() + interval '2 days', now() + interval '2 days 1 hour', '')"
)


def _new_visit(w: SimpleNamespace, h0: int = 15, hub: str = "HUB-1") -> str:
    """A requested visit at ``hub`` (with two hubs the record must name its hub)."""
    return str(
        ps.request_visit(
            w.ops, site_code="S-1", on_site_start=at(DAY, h0), on_site_end=at(DAY, h0 + 1), travel_out_min=30,
            travel_back_min=30, tasks=["T-04"], required_scopes=["presence"], hub_code=hub,
        )["entity_id"]
    )  # fmt: skip


def _requested_revision(w: SimpleNamespace, hub_code: str | None = None) -> int:
    """The first revision (no robot) of a new requested visit at ``hub_code`` (HUB-1 by default)."""
    code = _new_visit(w, hub=hub_code or "HUB-1")
    v = Visit.objects.get(code=code)
    assert v.current_revision_id is not None
    return v.current_revision_id


# ================================================================== 11.4 location
@pytest.mark.django_db(transaction=True)
def test_location_states_and_custody_hub(app_role_conn):
    w = _xworld("lo_")
    r2 = _robot("R-02")
    # no custody history: the home hub, verified
    loc = robot_location(r2.pk)
    assert (loc.kind, loc.hub_id, loc.ref, loc.verified) == ("hub", w.hub_a.pk, "HUB-1", True)
    assert loc.problem is None and loc.dispatchable_at(w.hub_a.pk) and not loc.dispatchable_at(w.hub_b.pk)
    # departed: in the vehicle, not verified, no hub; the hub -> vehicle event names the departure hub
    loc = robot_location(w.robot.pk)
    assert (loc.kind, loc.hub_id, loc.ref, loc.verified) == ("vehicle", None, "VAN-1", False)
    first = CustodyEvent.objects.get(run=w.run, seq=1)
    assert (first.from_kind, first.hub_id) == ("hub", w.hub_a.pk)
    with app_role_conn.cursor() as cur:
        to_site(cur, w)
    loc = robot_location(w.robot.pk)
    assert (loc.kind, loc.hub_id, loc.ref, loc.verified) == ("site", None, "S-1", True)
    assert (
        CustodyEvent.objects.filter(robot=w.robot).exclude(from_kind="hub").filter(hub__isnull=False).count()
        == 0
    )
    # unaccounted: the incident code, no hub
    iid = raw_incident(app_role_conn, w, unaccounted=True)
    loc = robot_location(w.robot.pk)
    assert (loc.kind, loc.hub_id, loc.verified) == ("unaccounted", None, False) and loc.ref.startswith("IC-")
    assert iid
    # in transit: no hub at all
    tid = _start(w)
    loc = robot_location(r2.pk)
    assert (loc.kind, loc.hub_id, loc.verified) == ("transfer_in_transit", None, False)
    assert loc.ref == f"TR-{tid:04d} from HUB-1 to HB"
    # unknown robot
    with connection.cursor() as cur:
        cur.execute("SELECT kind, hub_id, verified FROM fleet_robot_location(-1)")
        assert cur.fetchone() == ("unknown", None, False)
    # the caller can never choose the hub of a custody event: the trigger overwrites it
    assert CustodyEvent.objects.filter(robot=w.robot, to_kind="hub").count() == 0


@pytest.mark.django_db(transaction=True)
def test_hub_end_custody_events_get_the_runs_hub_whatever_the_caller_sends(app_role_conn):
    w = _xworld("ch_")
    with app_role_conn.cursor() as cur:
        to_site(cur, w)
        cur.execute(
            "INSERT INTO execution_custodyevent (robot_id, seq, from_kind, to_kind, run_id, trip_id, visit_id,"
            " custodian_id, received_by_id, basis, note, recorded_by_id, hub_id) "
            "SELECT %s, max(seq) + 1, 'site', 'vehicle', %s, %s, %s, %s, %s, 'handoff', '', %s, %s "
            "FROM execution_custodyevent WHERE robot_id = %s RETURNING hub_id",
            [w.robot.pk, w.run.pk, w.run.trip_id, w.run.visit_id, w.crew_lead.pk, w.crew_second.pk, w.ops.pk,
             w.hub_b.pk, w.robot.pk],
        )  # fmt: skip
        assert cur.fetchone()[0] is None  # no hub end: NULL, whatever was sent


# ================================================================== 10.A: never dispatchable outside hub(H)
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", ["in_transit", "at_site", "unaccounted"])
def test_a_robot_not_at_a_verified_hub_is_never_eligible_or_proposed_at_either_hub(state, app_role_conn):
    w = _xworld(f"ne{state[:2]}_")
    if state == "in_transit":
        robot = _robot("R-02")
        _start(w)
        reason = "In transfer TR-"
    else:
        robot = w.robot
        with app_role_conn.cursor() as cur:
            to_site(cur, w)
        reason = "Not at a hub: at site S-1"
        if state == "unaccounted":
            raw_incident(app_role_conn, w, unaccounted=True)
            reason = "Not at a hub: unaccounted for"
    # the SQL view and the Python eligibility agree, with the same wording, at the same position (last)
    eligible, reasons = _eligibility(robot.pk)
    assert not eligible and reasons[-1].startswith(reason), reasons
    e = fs.evaluate_eligibility(robot)
    assert not e.eligible_for_planning and e.reasons == reasons
    # the planner and the options list never propose it, at either hub
    for hub_code in ("HUB-1", "HB"):
        code = _new_visit(w, hub=hub_code)
        visit = Visit.objects.get(code=code)
        opts = popt.plan_options(visit)
        robot_opt = next(o for o in opts.robots if o.code == robot.code)
        assert robot_opt.status != popt.FREE, robot_opt
        sug = popt.suggest(visit, opts)
        assert sug.robot is None or sug.robot.code != robot.code
        if hub_code == "HUB-1":
            assert plan(w, code, robot=robot.code, vehicle="VAN-2")["feasibility"] == "blocked"
        else:  # another hub's visit: refused outright (its home hub is HUB-1, and it is not at HB either)
            with pytest.raises(Conflict):
                plan(w, code, robot=robot.code, vehicle="VAN-2")
    # direct SQL as ops_app: a reservation of the robot for a visit at either hub is refused
    for hub_code in ("HUB-1", "HB"):
        rev = _requested_revision(w, hub_code)
        with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="refused"):
            cur.execute(BOOK_SQL, [_resource(robot), rev])


@pytest.mark.django_db(transaction=True)
def test_a_robot_verified_at_one_hub_is_not_dispatchable_from_the_other(app_role_conn):
    w = _world("oh_")
    tid = _start(w)
    tr.receive_transfer(w.tech_b, transfer_id=tid)
    r2 = _robot("R-02")
    assert r2.hub_id == w.hub_b.pk
    loc = robot_location(r2.pk)
    assert (loc.kind, loc.hub_id, loc.verified) == ("hub", w.hub_b.pk, True)
    assert _eligibility(r2.pk)[0] in (True, False)  # eligibility is hub-independent; the hub is checked below
    # HUB-1 visit: refused by the planner and by the database
    code = _new_visit(w)
    visit = Visit.objects.get(code=code)
    opt = next(o for o in popt.plan_options(visit).robots if o.code == "R-02")
    assert (opt.status, opt.note) == (popt.UNUSABLE, "at another hub")
    with pytest.raises(Conflict, match="another hub"):
        plan(w, code, robot="R-02", vehicle="VAN-2")
    rev = _requested_revision(w, "HUB-1")
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="refused"):
        cur.execute(BOOK_SQL, [_resource(r2), rev])


# ================================================================== 10.C: races, both orders
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["transfer", "booking"])
def test_a_transfer_racing_a_booking_is_ordered_both_ways(first):
    w = _world(f"rb{first[0]}_")
    r2 = _robot("R-02")
    res, rev = _resource(r2), _requested_revision(w)
    start_sql = "SELECT fleet_transfer_start(%s, 'robot', %s, %s, 'SYN: race', '')"
    start_params = [w.tech_a.pk, r2.pk, w.hub_b.pk]
    holder, waiter = _app_conn(autocommit=False), _app_conn()
    out: dict[str, Any] = {}
    try:
        with holder.cursor() as cur:
            if first == "transfer":
                cur.execute(start_sql, start_params)
            else:
                cur.execute(BOOK_SQL, [res, rev])

        def go() -> None:
            try:
                with waiter.cursor() as cur:
                    if first == "transfer":
                        cur.execute(BOOK_SQL, [res, rev])
                    else:
                        cur.execute(start_sql, start_params)
                out["second"] = "committed"
            except psycopg.Error as exc:
                out["second"] = exc

        t = threading.Thread(target=go)
        t.start()
        assert _waits(waiter.info.backend_pid), "the second transaction never waited"
        assert "second" not in out
        holder.commit()
        t.join(15)
        assert not t.is_alive()
    finally:
        holder.close()
        waiter.close()
    second = out["second"]
    assert isinstance(second, psycopg.errors.CheckViolation), second
    if first == "transfer":
        assert "reservation refused: robot R-02: In transfer TR-" in str(second)
        assert HubTransfer.objects.filter(robot=r2).count() == 1
    else:
        assert "R-02 cannot move to hub HB" in str(second) and "it is booked for visit" in str(second)
        assert not HubTransfer.objects.filter(robot=r2).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["transfer", "authorization"])
def test_a_transfer_racing_an_authorization_is_ordered_both_ways(first):
    """R-01 is booked on the confirmed visit w.visit with all its evidence.

    authorization first: authorize_departure pauses before its commit (holding R-01 FOR SHARE); the transfer
    (fleet_transfer_start as ops_app) waits for R-01 FOR UPDATE (pg_locks), then sees the authorization and the
    booking and is refused, listing both.

    transfer first: the transfer's own lock step (fleet_transfer_lock_asset) and its internal start step
    (fleet_transfer_apply_start) are held uncommitted by the owner. The public function refuses a booked robot,
    so this models a transfer that got past its checks: the authorization waits for R-01 (pg_locks), then finds the
    robot's version changed and the robot in transit, and is refused."""
    w = _world(f"ra{first[0]}_")
    r1 = _robot("R-01")
    start_sql = "SELECT fleet_transfer_start(%s, 'robot', %s, %s, 'SYN: race', '')"
    if first == "authorization":
        paused, go_on = threading.Event(), threading.Event()

        def hook() -> None:
            if threading.current_thread().name == "auth":
                paused.set()
                assert go_on.wait(20)

        testhooks.register("before_commit:readiness.authorize_departure", hook)
        try:
            auth_out: dict[str, Any] = {}

            def authorize() -> None:
                try:
                    auth_out["r"] = rc.authorize_departure(w.ops, visit_code=w.visit)
                except Exception as exc:  # noqa: BLE001
                    auth_out["r"] = exc
                finally:
                    connection.close()

            t1 = threading.Thread(target=authorize, name="auth")
            t1.start()
            assert paused.wait(20)
            waiter = _app_conn()
            out: dict[str, Any] = {}

            def transfer() -> None:
                try:
                    with waiter.cursor() as cur:
                        cur.execute(start_sql, [w.tech_a.pk, r1.pk, w.hub_b.pk])
                    out["r"] = "committed"
                except psycopg.Error as exc:
                    out["r"] = exc

            t2 = threading.Thread(target=transfer)
            t2.start()
            try:
                assert _waits(waiter.info.backend_pid), "the transfer never waited"
                assert "r" not in out
            finally:
                go_on.set()
                t1.join(30)
                t2.join(30)
                waiter.close()
        finally:
            testhooks.clear()
        assert isinstance(auth_out["r"], dict), auth_out["r"]
        err = out["r"]
        assert isinstance(err, psycopg.errors.CheckViolation), err
        assert (
            f"departure authorization {auth_out['r']['code']} for visit {w.visit} has not been used"
            in str(err)
        )
        assert "it is booked for visit" in str(err)
        assert not HubTransfer.objects.filter(robot=r1).exists()
    else:
        version = r1.version
        owner = _owner_conn(autocommit=False)
        try:
            with owner.cursor() as cur:
                cur.execute("SELECT * FROM fleet_transfer_lock_asset('robot', %s)", [r1.pk])
                cur.execute(
                    "SELECT fleet_transfer_apply_start(%s, 'robot', %s, %s, %s, 'SYN: race', '')",
                    [w.tech_a.pk, r1.pk, w.hub_a.pk, w.hub_b.pk],
                )
            bg = Background(lambda: rc.authorize_departure(w.ops, visit_code=w.visit))
            with connection.cursor() as cur:
                cur.execute("SELECT pid FROM pg_stat_activity WHERE query ILIKE %s AND pid <> pg_backend_pid()",
                            ["%FOR SHARE%"])  # fmt: skip
            waited = False
            for _ in range(200):
                with connection.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE NOT l.granted AND a.datname = current_database() AND l.pid <> pg_backend_pid()"
                    )
                    if cur.fetchone()[0] > 0:
                        waited = True
                        break
                time.sleep(0.05)
            assert waited, f"the authorization never waited ({bg.error!r})"
            assert bg.thread.is_alive()
            owner.commit()
            bg.join()
        finally:
            owner.close()
        assert isinstance(bg.error, Conflict), (bg.result, bg.error)
        r1.refresh_from_db()
        assert r1.version > version
        assert robot_location(r1.pk).kind == "transfer_in_transit"


# ================================================================== 10.C: receipts, holds, authorizations
@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_receipts_exactly_one_wins():
    w = _world("rr_")
    tid = _start(w)
    holder, waiter = _app_conn(autocommit=False), _app_conn()
    sql = "SELECT fleet_transfer_receive(%s, %s, '')"
    out: dict[str, Any] = {}
    try:
        with holder.cursor() as cur:
            cur.execute(sql, [w.tech_b.pk, tid])

        def go() -> None:
            try:
                with waiter.cursor() as cur:
                    cur.execute(sql, [w.ops.pk, tid])
                out["second"] = "committed"
            except psycopg.Error as exc:
                out["second"] = exc

        t = threading.Thread(target=go)
        t.start()
        assert _waits(waiter.info.backend_pid), "the second receipt never waited"
        holder.commit()
        t.join(15)
    finally:
        holder.close()
        waiter.close()
    assert isinstance(out["second"], psycopg.errors.CheckViolation), out["second"]
    assert "was already received at hub HB" in str(out["second"])
    assert HubTransfer.objects.filter(transfer_id=tid, state="received").count() == 1
    assert _robot("R-02").hub_id == w.hub_b.pk


@pytest.mark.django_db(transaction=True)
def test_holds_survive_every_stage_and_a_receipt_never_clears_one():
    w = _world("hk_")
    fs.place_hold(w.ops, scope="robot", target="R-02", source_type="manual", reason="SYN: hold before move")
    hold = Hold.objects.get(robot__code="R-02")

    def active() -> bool:
        hold.refresh_from_db()
        return hold.released_at is None

    holds_before = Hold.objects.count()
    tid = _start(w)
    assert active() and robot_location(_robot("R-02").pk).kind == "transfer_in_transit"
    tr.refuse_transfer(w.tech_b, transfer_id=tid, reason="SYN: dock full")
    assert active() and _state(tid) == "receipt_refused"
    tr.abandon_transfer(w.ops, transfer_id=tid, reason="SYN: send it back")
    assert active() and _state(tid) == "abandoned"
    tr.receive_transfer(w.tech_a, transfer_id=tid)
    assert active() and _state(tid) == "received"
    # a second transfer, received at the destination: still held, and still not eligible because of the hold
    tid2 = _start(w)
    tr.receive_transfer(w.tech_b, transfer_id=tid2)
    assert active() and Hold.objects.count() == holds_before
    eligible, reasons = _eligibility(_robot("R-02").pk)
    assert not eligible and any(r.startswith(f"Robot hold {hold.code}") for r in reasons)


@pytest.mark.django_db(transaction=True)
def test_an_authorization_issued_before_a_transfer_fails_at_departure_afterwards():
    w = _world("ab_")
    r1 = _robot("R-01")
    auth = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    # the public start refuses: the unused authorization (and the booking) are listed
    with pytest.raises(Conflict) as refused:
        _start(w, code="R-01")
    assert f"departure authorization {auth} for visit {w.visit} has not been used" in str(refused.value)
    # even past its checks (the internal start step, owner only), a transfer invalidates the old authorization
    version = _robot("R-01").version
    with _owner_conn() as owner, owner.cursor() as cur:
        cur.execute(
            "SELECT fleet_transfer_apply_start(%s, 'robot', %s, %s, %s, 'SYN: past the gate', '')",
            [w.tech_a.pk, r1.pk, w.hub_a.pk, w.hub_b.pk],
        )
    assert _robot("R-01").version == version + 1
    with pytest.raises(Conflict, match="invalidated|refused"):
        rc.record_departure(w.ops, authorization_code=auth)
    assert not ExecutionRun.objects.filter(visit__code=w.visit).exists()
    # and no new authorization can be issued while it is in transit
    with pytest.raises(Conflict, match="transfer"):
        rc.authorize_departure(w.ops, visit_code=w.visit)


# ================================================================== 10.C: refused and abandoned paths
@pytest.mark.django_db(transaction=True)
def test_refused_then_abandoned_transfer_comes_back_only_at_the_source():
    w = _world("rf_")
    r2 = _robot("R-02")
    tid = _start(w)
    tr.refuse_transfer(w.tech_b, transfer_id=tid, reason="SYN: no space")
    assert robot_location(r2.pk).kind == "transfer_in_transit"
    with pytest.raises(Conflict, match="only a transfer on its way"):
        tr.refuse_transfer(w.tech_b, transfer_id=tid, reason="again")
    with pytest.raises(PermissionDenied):
        tr.abandon_transfer(w.tech_a, transfer_id=tid, reason="SYN: tech cannot")
    tr.abandon_transfer(w.ops, transfer_id=tid, reason="SYN: return it")
    loc = robot_location(r2.pk)
    assert loc.kind == "transfer_in_transit" and loc.ref.endswith("abandoned")
    # the destination can never receive an abandoned transfer, through the service or direct SQL
    with pytest.raises(PermissionDenied, match="HUB-1"):
        tr.receive_transfer(w.tech_b, transfer_id=tid)
    with _app_conn() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute("SELECT fleet_transfer_receive(%s, %s, '')", [w.tech_b.pk, tid])
    # the regional ops lead's receipt puts it back at the SOURCE
    tr.receive_transfer(w.ops, transfer_id=tid)
    last = HubTransfer.objects.filter(transfer_id=tid).order_by("-seq").first()
    assert last is not None and (last.state, last.at_hub_id) == ("received", w.hub_a.pk)
    r2.refresh_from_db()
    assert r2.hub_id == w.hub_a.pk
    loc = robot_location(r2.pk)
    assert (loc.kind, loc.hub_id, loc.verified) == ("hub", w.hub_a.pk, True)
    with pytest.raises(Conflict, match="already received"):
        tr.receive_transfer(w.ops, transfer_id=tid)
    assert [s.state for s in HubTransfer.objects.filter(transfer_id=tid).order_by("seq")] == [
        "in_transit", "receipt_refused", "abandoned", "received",
    ]  # fmt: skip
    # every step is audited (the database function and run_command)
    cmds = set(AuditEvent.objects.filter(entity_id=f"TR-{tid:04d}").values_list("command", flat=True))
    assert {"fleet.transfer_start", "fleet.transfer_refuse", "fleet.transfer_abandon", "fleet.transfer_receive",
            "fleet.start_transfer", "fleet.refuse_transfer", "fleet.abandon_transfer",
            "fleet.receive_transfer"} <= cmds  # fmt: skip


@pytest.mark.django_db(transaction=True)
def test_an_unaccounted_robot_in_transit_needs_a_recovery_before_its_receipt(app_role_conn):
    w = _xworld("ua_")
    r2 = _robot("R-02")
    tid = _start(w)
    raw_incident(app_role_conn, w, unaccounted=True, robot_id=r2.pk)
    assert robot_location(r2.pk).kind == "unaccounted"
    with pytest.raises(Conflict, match="record its recovery first"):
        tr.receive_transfer(w.tech_b, transfer_id=tid)
    inc = CustodyEvent.objects.filter(robot=r2).order_by("-seq").first().incident  # type: ignore[union-attr]
    lg.recover_robot(w.ops, robot_code="R-02", received_by_code="P-2", note="SYN: found in the van")
    assert robot_location(r2.pk).kind == "transfer_in_transit"  # recovered, still on its way
    tr.receive_transfer(w.tech_b, transfer_id=tid)
    loc = robot_location(r2.pk)
    assert (loc.kind, loc.hub_id) == ("hub", w.hub_b.pk)
    # the incident hold is untouched by the receipt
    assert Hold.objects.get(source_ref=inc.code).released_at is None  # type: ignore[union-attr]


# ================================================================== 10.C: stock
@pytest.mark.django_db(transaction=True)
def test_stock_transfer_reconciles_and_counts_at_neither_hub_in_transit():
    w = _world("sk_")
    st.create_item(w.ops, code="RK", name="Restraint kit", kind="reusable", unit="kits", hub_code="HUB-1")
    st.create_item(w.ops, code="RK", name="Restraint kit", kind="reusable", unit="kits", hub_code="HB")
    st.receive(w.tech_a, item_code="RK", quantity=10, hub_code="HUB-1")
    v = _new_visit(w)
    st.reserve(w.ops, item_code="RK", visit_code=v, quantity=3)

    def levels() -> dict[str, tuple[int, int]]:
        return {
            Hub.objects.get(pk=lv.hub_id).code: (lv.on_hand, lv.available)
            for lv in st.levels() if lv.code == "RK"
        }  # fmt: skip

    def in_transit() -> int:
        return sum(x.quantity for x in st.in_transit())

    def total() -> int:
        return sum(on for on, _ in levels().values()) + in_transit()

    assert levels() == {"HUB-1": (10, 7), "HB": (0, 0)} and total() == 10
    # reserved stock cannot leave: 7 available
    with pytest.raises(Conflict, match="available"):
        st.transfer(w.tech_a, item_code="RK", quantity=8, to_hub_code="HB", hub_code="HUB-1")
    # the destination hub's technician cannot send the source's stock
    with pytest.raises(PermissionDenied):
        st.transfer(w.tech_b, item_code="RK", quantity=1, to_hub_code="HB", hub_code="HUB-1")
    mid = st.transfer(w.tech_a, item_code="RK", quantity=4, to_hub_code="HB", hub_code="HUB-1")
    assert levels() == {"HUB-1": (6, 3), "HB": (0, 0)} and in_transit() == 4 and total() == 10
    # only the destination receives it
    with pytest.raises(PermissionDenied):
        st.receive_transfer(w.tech_a, transfer_id=mid)
    st.receive_transfer(w.tech_b, transfer_id=mid)
    assert levels() == {"HUB-1": (6, 3), "HB": (4, 4)} and in_transit() == 0 and total() == 10
    with pytest.raises(Conflict, match="already received"):
        st.receive_transfer(w.tech_b, transfer_id=mid)
    kinds = list(StockMovement.objects.filter(item__code="RK").order_by("id").values_list("kind", flat=True))
    assert kinds[-2:] == ["transfer_out", "transfer_in"]
    # an item the destination does not keep cannot be sent
    st.create_item(w.ops, code="AONLY", name="Only at A", kind="consumable", unit="boxes", hub_code="HUB-1")
    st.receive(w.tech_a, item_code="AONLY", quantity=2, hub_code="HUB-1")
    with pytest.raises(Conflict, match="keeps no item AONLY"):
        st.transfer(w.tech_a, item_code="AONLY", quantity=1, to_hub_code="HB", hub_code="HUB-1")


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_stock_receipts_exactly_one_wins():
    w = _world("sr_")
    st.create_item(w.ops, code="RK", name="Restraint kit", kind="reusable", unit="kits", hub_code="HUB-1")
    st.create_item(w.ops, code="RK", name="Restraint kit", kind="reusable", unit="kits", hub_code="HB")
    st.receive(w.tech_a, item_code="RK", quantity=5, hub_code="HUB-1")
    mid = st.transfer(w.tech_a, item_code="RK", quantity=5, to_hub_code="HB", hub_code="HUB-1")
    holder, waiter = _app_conn(autocommit=False), _app_conn()
    out: dict[str, Any] = {}
    sql = "SELECT hub_receive_transfer(%s, %s, '')"
    try:
        with holder.cursor() as cur:
            cur.execute(sql, [w.tech_b.pk, mid])

        def go() -> None:
            try:
                with waiter.cursor() as cur:
                    cur.execute(sql, [w.ops.pk, mid])
                out["second"] = "committed"
            except psycopg.Error as exc:
                out["second"] = exc

        t = threading.Thread(target=go)
        t.start()
        assert _waits(waiter.info.backend_pid)
        holder.commit()
        t.join(15)
    finally:
        holder.close()
        waiter.close()
    assert isinstance(out["second"], psycopg.errors.CheckViolation), out["second"]
    assert StockMovement.objects.filter(transfer_of_id=mid).count() == 1
    assert StockItem.objects.get(code="RK", hub=w.hub_b).movements.count() == 1


# ================================================================== vehicles
@pytest.mark.django_db(transaction=True)
def test_vehicle_with_an_open_or_future_trip_cannot_move_and_one_in_transit_cannot_be_booked(app_role_conn):
    w = _xworld("vh_")
    # VAN-1 is out on the departed run's trip
    with pytest.raises(Conflict, match="it is out on the trip of visit"):
        _start(w, code="VAN-1", kind="vehicle")
    # VAN-2 gets a future trip
    v2 = _new_visit(w)
    assert plan(w, v2, robot="R-02", second="P-3", vehicle="VAN-2")["feasibility"] != "blocked"
    with pytest.raises(Conflict, match=f"it is booked for visit {v2}"):
        _start(w, code="VAN-2", kind="vehicle")
    ps.cancel_visit(w.ops, visit_code=v2, reason="SYN: free the van")
    tid = _start(w, code="VAN-2", kind="vehicle")
    van2 = Vehicle.objects.get(code="VAN-2")
    # in transit: neither hub can book it
    code = _new_visit(w, h0=17)
    res = plan(w, code, robot="R-03", second="P-3", vehicle="VAN-2")
    assert res["feasibility"] == "blocked"
    assert any(
        b["code"] == "VEHICLE_IN_TRANSFER" for b in Visit.objects.get(code=code).current_revision.blockers
    )
    vres = Resource.objects.get_or_create(kind="vehicle", vehicle=van2)[0].pk
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="In transfer"):
        cur.execute(BOOK_SQL, [vres, _requested_revision(w)])
    tr.receive_transfer(w.tech_b, transfer_id=tid)
    van2.refresh_from_db()
    assert van2.hub_id == w.hub_b.pk


# ================================================================== write boundary (Phase 02 8.8)
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("actor", ["forged_wrong_hub", "forged_wrong_role", "NULL", "0", "''"])
def test_transfer_functions_refuse_forged_null_and_empty_actors_as_ops_app(actor):
    w = _world(f"fa{actor[:2].strip(chr(39))}_")
    st.create_item(w.ops, code="RK", name="Restraint kit", kind="reusable", unit="kits", hub_code="HUB-1")
    st.create_item(w.ops, code="RK", name="Restraint kit", kind="reusable", unit="kits", hub_code="HB")
    st.receive(w.tech_a, item_code="RK", quantity=5, hub_code="HUB-1")
    rk_a = StockItem.objects.get(code="RK", hub=w.hub_a).pk
    mid = st.transfer(w.tech_a, item_code="RK", quantity=1, to_hub_code="HB", hub_code="HUB-1")
    tid = _start(w, code="R-03")
    r2 = _robot("R-02").pk
    a = {
        "forged_wrong_hub": str(w.tech_b.pk),  # a real technician, of HB, acting on HUB-1 records / receipts
        "forged_wrong_role": str(w.safety.pk),  # a real user whose role allows none of these
        "NULL": "NULL::bigint",
        "0": "0::bigint",
        "''": "''",
    }[actor]
    calls = [
        f"SELECT fleet_transfer_start({a}, 'robot', {r2}, {w.hub_b.pk}, 'x', '')",
        f"SELECT fleet_transfer_abandon({a}, {tid}, 'x', '')",
        f"SELECT hub_transfer_stock({a}, {rk_a}, 1, {w.hub_b.pk}, '')",
    ]
    if actor != "forged_wrong_hub":  # HB's technician may receive or refuse at HB: that is the right hub
        calls += [
            f"SELECT fleet_transfer_receive({a}, {tid}, '')",
            f"SELECT fleet_transfer_refuse({a}, {tid}, 'x', '')",
            f"SELECT hub_receive_transfer({a}, {mid}, '')",
        ]
    expected = (
        psycopg.errors.InvalidTextRepresentation if actor == "''" else psycopg.errors.InsufficientPrivilege
    )
    before = (HubTransfer.objects.count(), StockMovement.objects.count(), AuditEvent.objects.count())
    with _app_conn() as conn:
        for sql in calls:
            with conn.cursor() as cur, pytest.raises(expected):
                cur.execute(sql)
    assert (HubTransfer.objects.count(), StockMovement.objects.count(), AuditEvent.objects.count()) == before


@pytest.mark.django_db(transaction=True)
def test_ops_app_cannot_write_transfers_or_call_the_internal_steps(app_role_conn):
    w = _world("wb_")
    tid = _start(w)
    r3 = _robot("R-03").pk
    forbidden = [
        f"INSERT INTO fleet_hub_transfer (id, transfer_id, seq, asset_kind, robot_id, from_hub_id, to_hub_id, state,"
        f" actor_id, reason, request_id, recorded_at) VALUES (999999, 999999, 1, 'robot', {r3}, {w.hub_a.pk},"
        f" {w.hub_b.pk}, 'in_transit', {w.ops.pk}, '', '', now())",
        f"UPDATE fleet_hub_transfer SET state = 'received' WHERE id = {tid}",
        f"DELETE FROM fleet_hub_transfer WHERE id = {tid}",
        f"SELECT fleet_transfer_apply_start({w.ops.pk}, 'robot', {r3}, {w.hub_a.pk}, {w.hub_b.pk}, 'x', '')",
        f"SELECT fleet_transfer_blockers('robot', {r3})",
        f"UPDATE fleet_robot SET hub_id = {w.hub_b.pk} WHERE id = {r3}",
    ]
    for sql in forbidden:
        with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(sql)
    # the owner cannot rewrite history either (append-only trigger)
    with _owner_conn() as owner, owner.cursor() as cur, pytest.raises(psycopg.errors.Error):
        cur.execute("UPDATE fleet_hub_transfer SET reason = 'x' WHERE id = %s", [tid])
    # and the right hub's technician through the same function works
    with app_role_conn.cursor() as cur:
        cur.execute("SELECT fleet_transfer_receive(%s, %s, '')", [w.tech_b.pk, tid])
        assert cur.fetchone()[0] > 0


@pytest.mark.django_db(transaction=True)
def test_transfer_pages_and_services_are_scoped_to_the_record_hub():
    w = _world("pg_")
    # HB's technician cannot start a transfer of a HUB-1 robot (service, before the database)
    with pytest.raises(PermissionDenied, match="at hub HUB-1"):
        tr.start_transfer(w.tech_b, kind="robot", code="R-02", to_hub_code="HB", reason="x")
    ops = Client()
    ops.force_login(w.ops)
    html = ops.get("/fleet/R-02/").content.decode()
    assert 'data-location="hub"' in html and "Move to another hub" in html
    # the technician sends R-02 through the Transfers page form (one-time form token)
    c = Client()
    c.force_login(w.tech_a)
    page = c.get("/hub/transfers/").content.decode()
    token = TOKEN_RE.search(page).group(1)  # type: ignore[union-attr]
    c.post("/hub/transfers/start/", {"kind": "robot", "code": "R-02", "to_hub": "HB", "reason": "SYN: page",
                                     "_form_token": token})  # fmt: skip
    tid = HubTransfer.objects.get(robot__code="R-02", seq=1).pk
    assert "On its way to another hub" in ops.get("/fleet/").content.decode()
    assert 'data-location="transfer_in_transit"' in ops.get("/fleet/R-02/").content.decode()
    # HB's technician sees it and receives it from the page
    cb = Client()
    cb.force_login(w.tech_b)
    page = cb.get("/hub/transfers/").content.decode()
    assert f"TR-{tid:04d}" in page and f'data-receive="{tid}"' in page
    token = TOKEN_RE.search(page).group(1)  # type: ignore[union-attr]
    cb.post(f"/hub/transfers/{tid}/receive/", {"_form_token": token})
    assert _state(tid) == "received" and _robot("R-02").hub_id == w.hub_b.pk
    # the vehicles page shows each vehicle's hub with the move form
    assert 'data-vehicle-hub="VAN-2"' in ops.get("/vehicles/").content.decode()


# ================================================================== field commands at the run's hub
@pytest.mark.django_db(transaction=True)
def test_field_steps_need_a_field_role_at_the_runs_hub(app_role_conn):
    w = _xworld("fc_")
    ps.register_person(w.ops, username=w.field_b.username, code="P-9", hub_code="HB")
    # services: the permission is checked at the run's hub
    with pytest.raises(PermissionDenied):
        lg.verify_custody(w.field_b, run_code=w.run.code, custody_seq=1, verifier_code="P-2")
    with pytest.raises(PermissionDenied):
        lg.record_trip_event(w.field_b, run_code=w.run.code, kind="arrived_site")
    # direct SQL as ops_app: every field-role check is at the run's hub
    refused = [
        lambda cur: raw_verify(cur, w, by=w.field_b),
        lambda cur: raw_trip_event(cur, w, "arrived_site", by=w.field_b),
    ]
    for step in refused:
        with app_role_conn.transaction(), app_role_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="field_operator role"):
                step(cur)
    # the regional ops lead and the run hub's field operator can
    with app_role_conn.cursor() as cur:
        raw_verify(cur, w, by=w.ops)
        raw_trip_event(cur, w, "arrived_site", by=w.field)
    # custody handoff recorded by HB's operator: refused; by the run hub's operator: allowed
    with app_role_conn.transaction(), app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="custody handoff"):
            raw_custody(cur, w, "vehicle", "site", by=w.field_b)
    with app_role_conn.cursor() as cur:
        raw_custody(cur, w, "vehicle", "site", by=w.field)
        with pytest.raises(psycopg.errors.CheckViolation, match="site check"):
            raw_site_check(cur, w, by=w.field_b)
        raw_site_check(cur, w, by=w.ops)
        with pytest.raises(psycopg.errors.CheckViolation, match="session event"):
            raw_session(cur, w, "observation", note="SYN", by=w.field_b)
        raw_session(cur, w, "observation", note="SYN", by=w.field)


@pytest.mark.django_db(transaction=True)
def test_departure_recording_needs_a_field_role_at_the_visits_hub():
    w = _world("dp_")
    auth = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    with pytest.raises((PermissionDenied, Conflict)):
        rc.record_departure(w.field_b, authorization_code=auth)
    assert not ExecutionRun.objects.filter(visit__code=w.visit).exists()
    rc.record_departure(w.field, authorization_code=auth)
    assert ExecutionRun.objects.filter(visit__code=w.visit).exists()

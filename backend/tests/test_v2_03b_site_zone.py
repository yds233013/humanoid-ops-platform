"""v2 Phase 03B-4: a home's zone changes only through the guarded, audited planning_set_site_zone (planning/0014;
docs/13_v2_phase03_contract.md 10.G, 13.4 item 2, 13.5).

Proven here:

* Authority, as ops_app: only an active regional ops lead moves a home. The workspace admin, a hub technician, a
  field operator, a safety_pause grant and a founder are refused, and nothing is written. A reason is required;
  a retired zone, a missing home and a no-op are refused. One audit row per change, with the reason.
* A direct change of planning_site.zone_id as ops_app is refused (UPDATE, and an INSERT with a zone); other
  columns of the home stay writable.
* The home's guard version moves, so a departure authorization issued before the change is refused at
  departure (service and direct SQL).
* Re-plan rule: a visit keeps its saved zone until it is planned again; until then authorization is refused
  (readiness blocker PLANNING_HOME_ZONE_CHANGED, and the database gate by direct SQL). A re-plan takes the
  home's new zone, and authorization and departure then check the pauses of that zone: a home moved into a
  paused zone is refused there until the pause is lifted.
* No way out from under a pause: while an open visit of the home is paused, a move that would leave it unpaused
  is refused (lift first); a move that keeps it covered (the dispatch hub is paused) is allowed.
* Races, both orders, the waiting transaction seen in pg_locks: zone change vs pause, vs authorization, vs
  departure.

SYNTHETIC data only.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import connection

from accounts.models import RoleGrant
from operations import pauses
from planning import services as ps
from planning import travel, zones
from planning.models import Site, Visit
from platform_core import testhooks
from platform_core.commands import Conflict, PermissionDenied
from platform_core.models import AuditEvent, Zone
from readiness import commands as rc
from readiness.models import DepartureAuthorization, DepartureEvent
from tests.conftest import make_user
from tests.planning_world import plan
from tests.readiness_world import record_all_evidence
from tests.test_readiness_authz_boundary import issue, live_args
from tests.test_v2_03a_transfers import Background, _app_conn
from tests.test_v2_03b_pause import (
    PAUSE_SQL,
    PAUSED,
    _lift,
    _owner_waiting,
    _pause,
    _raw_departure,
    _waits,
    _zone_world,
)

pytestmark = pytest.mark.django_db(transaction=True)

SET_SQL = "SELECT planning_set_site_zone(%s, %s, %s, %s, %s)"
MOVED = "after this visit was planned"
LIFT_FIRST = "lift the pause first"


# ------------------------------------------------------------------ world and helpers
def _world(prefix: str) -> SimpleNamespace:
    """The 03B pause world (HUB-1, zones ZA = the hub's own zone and ZB = home S-1, w.visit confirmed with all its
    evidence and planned to ZB) plus an active zone ZC with travel ZA <-> ZC."""
    w = _zone_world(prefix)
    w.zc = Zone.objects.get(
        pk=int(zones.create_zone(w.ops, hub_code=w.hub.code, code="ZC", name="Coast")["entity_id"])
    )
    for a, b in ((w.za, w.zc), (w.zc, w.za)):
        travel.set_travel_time(w.ops, from_zone_id=a.pk, to_zone_id=b.pk, minutes=35, source="demo")
    w.site = Site.objects.get(code="S-1")
    return w


def _move(w: SimpleNamespace, zone_code: str | None, by: Any = None, reason: str = "SYN: new road") -> None:
    zones.set_site_zone(
        by or w.ops, site_code="S-1", hub_code=w.hub.code if zone_code else None, zone_code=zone_code,
        reason=reason,
    )  # fmt: skip


def _replan(w: SimpleNamespace) -> None:
    plan(w, w.visit)
    ps.confirm_visit(w.ops, visit_code=w.visit)
    record_all_evidence(w, w.visit)


def _site() -> tuple[int | None, int]:
    s = Site.objects.get(code="S-1")
    return s.zone_ref_id, s.version


def _audits() -> int:
    return AuditEvent.objects.filter(command="planning.set_site_zone", outcome="committed").count()


# ================================================================== authority, reason, audit
def test_only_the_regional_ops_lead_moves_a_home_with_a_reason_and_it_is_audited():
    w = _world("sa_")
    admin = make_user("sz_admin", "admin")
    founder = w.founder  # the readiness world has one
    tech, field, sp = make_user("sz_tech"), make_user("sz_fieldop"), make_user("sz_pause")
    RoleGrant.objects.create(user=tech, role="hub_technician", hub=w.hub)
    RoleGrant.objects.create(user=field, role="field_operator", hub=w.hub)
    RoleGrant.objects.create(user=sp, role="safety_pause", hub=w.hub)
    before = (_site(), _audits())
    with _app_conn() as conn, conn.cursor() as cur:
        for who in (admin, founder, tech, field, sp):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="regional ops lead"):
                cur.execute(SET_SQL, [who.pk, w.site.pk, w.zc.pk, "SYN: moved", ""])
        for reason in ("", "   ", None, "x" * 201):
            with pytest.raises(psycopg.errors.CheckViolation, match="Give the reason"):
                cur.execute(SET_SQL, [w.ops.pk, w.site.pk, w.zc.pk, reason, ""])
        with pytest.raises(psycopg.errors.CheckViolation, match="already in that zone"):
            cur.execute(SET_SQL, [w.ops.pk, w.site.pk, w.zb.pk, "SYN", ""])
        with pytest.raises(psycopg.errors.CheckViolation, match="Choose the home"):
            cur.execute(SET_SQL, [w.ops.pk, 0, w.zc.pk, "SYN", ""])
        with pytest.raises(psycopg.errors.CheckViolation, match="existing zone"):
            cur.execute(SET_SQL, [w.ops.pk, w.site.pk, 0, "SYN", ""])
    assert (_site(), _audits()) == before
    # the services say the same (planning.manage_sites: the ops lead)
    for who in (admin, tech):
        with pytest.raises(PermissionDenied):
            _move(w, "ZC", by=who)
    zones.retire_zone(w.ops, hub_code=w.hub.code, code="ZC")
    with pytest.raises(Conflict, match="retired"):
        _move(w, "ZC")
    assert (_site()[0], _audits()) == (before[0][0], before[1])
    zd = Zone.objects.get(
        pk=int(zones.create_zone(w.ops, hub_code=w.hub.code, code="ZD", name="Dell")["entity_id"])
    )
    # the ops lead, by direct SQL as ops_app (the runtime path), with a request id
    with _app_conn() as conn, conn.cursor() as cur:
        cur.execute(SET_SQL, [w.ops.pk, w.site.pk, zd.pk, "  SYN: the road moved  ", "req-sz"])
        version = cur.fetchone()[0]
    zone_id, site_version = _site()
    assert (zone_id, site_version) == (zd.pk, version) and version > before[0][1]
    audit = AuditEvent.objects.get(command="planning.set_site_zone", outcome="committed", request_id="req-sz")
    assert (audit.actor_id, audit.entity_id, audit.reason, audit.request_id) == (
        w.ops.pk,
        "S-1",
        "SYN: the road moved",
        "req-sz",
    )
    assert {k: audit.payload[k] for k in ("from_zone", "to_zone", "from_hub", "to_hub")} == {
        "from_zone": "ZB",
        "to_zone": "ZD",
        "from_hub": w.hub.code,
        "to_hub": w.hub.code,
    }
    # to unknown and back, through the service
    _move(w, None)
    assert _site()[0] is None
    _move(w, "ZB")
    assert _site()[0] == w.zb.pk and _audits() == before[1] + 3


def test_a_direct_zone_change_as_ops_app_is_refused():
    w = _world("sd_")
    with _app_conn() as conn, conn.cursor() as cur:
        for zone in (w.zc.pk, None):
            with pytest.raises(
                psycopg.errors.InsufficientPrivilege, match="only through planning_set_site_zone"
            ):
                cur.execute("UPDATE planning_site SET zone_id = %s WHERE id = %s", [zone, w.site.pk])
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="only through planning_set_site_zone"):
            cur.execute(
                "INSERT INTO planning_site (code, zone, timezone, crm_ref, version, created_by_id, created_at, "
                "zone_id) VALUES ('S-X', 'x', 'UTC', 'CRM-X', 1, %s, now(), %s)",
                [w.ops.pk, w.zc.pk],
            )
        # the rest of the home stays writable by the runtime role (headcount, a same-zone save)
        cur.execute(
            "UPDATE planning_site SET expected_adults = 2, zone_id = zone_id WHERE id = %s", [w.site.pk]
        )
    assert _site()[0] == w.zb.pk
    assert not Site.objects.filter(code="S-X").exists()


# ================================================================== versions and the re-plan rule
def test_an_earlier_authorization_stops_at_departure_and_the_visit_is_planned_again_before_it_can_leave():
    w = _world("sv_")
    auth = DepartureAuthorization.objects.get(code=rc.authorize_departure(w.ops, visit_code=w.visit)["code"])
    assert auth.guard_snapshot["site_version"] == _site()[1]
    _move(w, "ZC")
    assert auth.guard_snapshot["site_version"] != _site()[1]  # the home's version moved
    with pytest.raises(Conflict):
        rc.record_departure(w.ops, authorization_code=auth.code)
    with _app_conn() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        _raw_departure(cur, auth, w.ops)
    assert not DepartureEvent.objects.exists()
    # the visit keeps the zone it was planned with until it is planned again; until then it cannot be authorized
    v = Visit.objects.get(code=w.visit)
    assert v.zone_id == w.zb.pk
    with pytest.raises(Conflict, match="moved to another zone after this visit was planned"):
        rc.authorize_departure(w.ops, visit_code=w.visit)
    with _app_conn() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match=MOVED):
        issue(cur, live_args(w))
    assert DepartureAuthorization.objects.count() == 1
    # a re-plan takes the home's new zone and is checked fresh; then it can be authorized and leave
    _replan(w)
    assert Visit.objects.get(code=w.visit).zone_id == w.zc.pk
    code = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    rc.record_departure(w.ops, authorization_code=code)
    assert DepartureEvent.objects.count() == 1


def test_a_home_moved_into_a_paused_zone_is_refused_by_the_gates_after_the_re_plan():
    w = _world("sp_")
    pid = _pause(w, zone="ZC")
    _move(w, "ZC")  # moving INTO a paused area only blocks more: allowed
    # not re-planned yet: the saved zone ZB is not paused, but the visit cannot be authorized (its home moved)
    with _app_conn() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match=MOVED):
        issue(cur, live_args(w))
    out = plan(w, w.visit)
    assert out["paused"] and Visit.objects.get(code=w.visit).zone_id == w.zc.pk  # planning allowed, labelled
    ps.confirm_visit(w.ops, visit_code=w.visit)
    record_all_evidence(w, w.visit)
    with pytest.raises(Conflict, match=PAUSED):
        rc.authorize_departure(w.ops, visit_code=w.visit)
    with (
        _app_conn() as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match=PAUSED),
    ):
        issue(cur, live_args(w))
    _lift(w, pid)
    code = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    _pause(w, zone="ZC")  # paused again after the authorization: the departure gate reads the visit's zone
    with pytest.raises(Conflict, match=PAUSED):
        rc.record_departure(w.ops, authorization_code=code)
    assert not DepartureEvent.objects.exists()


def test_a_home_cannot_be_moved_out_from_under_a_pause():
    w = _world("so_")
    zp = _pause(w, zone="ZB")  # the visit's zone
    before = (_site(), _audits())
    with pytest.raises(Conflict, match=LIFT_FIRST):
        _move(w, "ZC")
    with _app_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match=LIFT_FIRST):
            cur.execute(SET_SQL, [w.ops.pk, w.site.pk, w.zc.pk, "SYN", ""])
    assert (_site(), _audits()) == before
    # an unknown zone stays covered by every zone pause at the visit's hub: allowed, and still paused
    _move(w, None)
    assert _site()[0] is None and pauses.active_for(w.hub.pk, None).pause_id == zp
    _lift(w, zp)
    # a pause of the visit's dispatch hub covers it in every zone: a move that keeps it covered is allowed
    hp = _pause(w)
    _move(w, "ZC")
    assert _site()[0] == w.zc.pk
    _lift(w, hp)


# ================================================================== races, both orders, waiter seen
@pytest.mark.parametrize("first", ["pause", "zone_change"])
def test_a_zone_change_and_a_pause_are_ordered_both_ways(first):
    """pause first: the pause of the home's zone ZB (settings Sh -> hub U) is held uncommitted; the zone change
    ZB -> ZC (settings Sh -> home U -> hub S) waits on the hub row (pg_locks), then sees the committed pause and
    is refused (it would take the visit out from under it). zone change first: the change ZB -> ZC is held
    uncommitted (hub S); a pause of ZC waits (pg_locks) and commits after it; the re-planned visit is then
    refused at authorization."""
    w = _world(f"rp{first[0]}_")
    audits = _audits()  # the world set the home's zone once
    holder = _app_conn(autocommit=False)
    waiter = _app_conn()
    out: dict[str, Any] = {}
    try:
        if first == "pause":
            with holder.cursor() as cur:
                cur.execute(PAUSE_SQL, [w.ops.pk, w.hub.pk, w.zb.pk, "SYN: race", ""])

            def second() -> None:
                try:
                    with waiter.cursor() as cur:
                        cur.execute(SET_SQL, [w.ops.pk, w.site.pk, w.zc.pk, "SYN: race", ""])
                    out["r"] = "committed"
                except psycopg.Error as exc:
                    out["r"] = exc
        else:
            with holder.cursor() as cur:
                cur.execute(SET_SQL, [w.ops.pk, w.site.pk, w.zc.pk, "SYN: race", ""])

            def second() -> None:
                try:
                    with waiter.cursor() as cur:
                        cur.execute(PAUSE_SQL, [w.ops.pk, w.hub.pk, w.zc.pk, "SYN: race", ""])
                    out["r"] = "committed"
                except psycopg.Error as exc:
                    out["r"] = exc

        t = threading.Thread(target=second)
        t.start()
        assert _waits(waiter.info.backend_pid), "the second transaction never waited"
        assert "r" not in out
        holder.commit()
        t.join(30)
    finally:
        holder.close()
        waiter.close()
    if first == "pause":
        assert isinstance(out["r"], psycopg.errors.CheckViolation) and LIFT_FIRST in str(out["r"]), out["r"]
        assert _site()[0] == w.zb.pk and _audits() == audits
    else:
        assert out["r"] == "committed", out["r"]
        assert _site()[0] == w.zc.pk
        _replan(w)
        with pytest.raises(Conflict, match=PAUSED):
            rc.authorize_departure(w.ops, visit_code=w.visit)
        assert not DepartureAuthorization.objects.exists()


def _hold_command(label: str, name: str, fn: Any) -> tuple[threading.Event, threading.Event, dict[str, Any]]:
    """Run ``fn`` in a thread named ``name``, stopped at its ``before_commit:<label>`` hook until released."""
    paused, go_on = threading.Event(), threading.Event()
    out: dict[str, Any] = {}

    def hook() -> None:
        if threading.current_thread().name == name:
            paused.set()
            assert go_on.wait(20)

    testhooks.register(f"before_commit:{label}", hook)

    def run() -> None:
        try:
            out["first"] = fn()
        except Exception as exc:  # noqa: BLE001
            out["first"] = exc
        finally:
            connection.close()

    out["thread"] = threading.Thread(target=run, name=name)
    out["thread"].start()
    assert paused.wait(20)
    return paused, go_on, out


def _zone_change_waits_then(w: SimpleNamespace, go_on: threading.Event, out: dict[str, Any]) -> None:
    """The zone change ZB -> ZC as ops_app while the first command holds its locks: it waits (pg_locks), then
    commits once the first one has committed."""
    waiter = _app_conn()

    def change() -> None:
        try:
            with waiter.cursor() as cur:
                cur.execute(SET_SQL, [w.ops.pk, w.site.pk, w.zc.pk, "SYN: race", ""])
            out["change"] = "committed"
        except psycopg.Error as exc:
            out["change"] = exc

    t = threading.Thread(target=change)
    t.start()
    try:
        assert _waits(waiter.info.backend_pid), "the zone change never waited"
        assert "change" not in out
    finally:
        go_on.set()
        out["thread"].join(30)
        t.join(30)
        waiter.close()
        testhooks.clear()


@pytest.mark.parametrize("first", ["zone_change", "authorization"])
def test_a_zone_change_and_an_authorization_are_ordered_both_ways(first):
    """zone change first: the change is held uncommitted (home U); authorize_departure waits on the home row
    (pg_locks), then reads the moved home and is refused (plan the visit again); nothing is issued.
    authorization first: authorize_departure stops before its commit (home S held); the zone change waits
    (pg_locks) and commits after it; that authorization is then refused at departure (home version)."""
    w = _world(f"ra{first[0]}_")
    if first == "zone_change":
        holder = _app_conn(autocommit=False)
        try:
            with holder.cursor() as cur:
                cur.execute(SET_SQL, [w.ops.pk, w.site.pk, w.zc.pk, "SYN: race", ""])
            bg = Background(lambda: rc.authorize_departure(w.ops, visit_code=w.visit))
            assert _owner_waiting(), f"the authorization never waited ({bg.error!r})"
            assert bg.thread.is_alive()
            holder.commit()
            bg.join()
        finally:
            holder.close()
        assert isinstance(bg.error, Conflict) and "moved to another zone" in str(bg.error), (
            bg.result,
            bg.error,
        )
        assert not DepartureAuthorization.objects.exists()
        assert _site()[0] == w.zc.pk
    else:
        _, go_on, out = _hold_command(
            "readiness.authorize_departure", "auth", lambda: rc.authorize_departure(w.ops, visit_code=w.visit)
        )
        _zone_change_waits_then(w, go_on, out)
        assert isinstance(out["first"], dict), out["first"]
        assert out["change"] == "committed", out["change"]
        auth = DepartureAuthorization.objects.get(code=out["first"]["code"])
        assert auth.guard_snapshot["site_version"] != _site()[1]
        with pytest.raises(Conflict):
            rc.record_departure(w.ops, authorization_code=auth.code)
        assert not DepartureEvent.objects.exists()


@pytest.mark.parametrize("first", ["zone_change", "departure"])
def test_a_zone_change_and_a_departure_are_ordered_both_ways(first):
    """zone change first: the change is held uncommitted (home U); record_departure waits on the home row
    (pg_locks), then sees the moved home's new version and is refused; nothing departs. departure first:
    record_departure stops before its commit (home S held); the zone change waits (pg_locks) and commits after
    it; the departure stands."""
    w = _world(f"rd{first[0]}_")
    code = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    if first == "zone_change":
        holder = _app_conn(autocommit=False)
        try:
            with holder.cursor() as cur:
                cur.execute(SET_SQL, [w.ops.pk, w.site.pk, w.zc.pk, "SYN: race", ""])
            bg = Background(lambda: rc.record_departure(w.ops, authorization_code=code))
            assert _owner_waiting(), f"the departure never waited ({bg.error!r})"
            assert bg.thread.is_alive()
            holder.commit()
            bg.join()
        finally:
            holder.close()
        assert isinstance(bg.error, Conflict), (bg.result, bg.error)
        assert not DepartureEvent.objects.exists()
        assert _site()[0] == w.zc.pk
    else:
        _, go_on, out = _hold_command(
            "readiness.record_departure",
            "depart",
            lambda: rc.record_departure(w.ops, authorization_code=code),
        )
        _zone_change_waits_then(w, go_on, out)
        assert isinstance(out["first"], dict), out["first"]
        assert out["change"] == "committed", out["change"]
        assert DepartureEvent.objects.count() == 1
        assert _site()[0] == w.zc.pk

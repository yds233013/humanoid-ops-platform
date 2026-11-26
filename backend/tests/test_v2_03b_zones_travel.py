"""v2 Phase 03B-1: zones and the versioned travel matrix (docs/13_v2_phase03_contract.md 10.H, 13.2, 13.4 item 3,
13.5).

Proven here:

* Zones: a visit's zone is its home's zone at plan time, set by the database (a forged zone is ignored on insert
  and refused on update, as ops_app); a retired zone keeps its history (old visits, travel rows) but cannot be
  used for a new visit, a home, a hub home zone or a travel value, and never comes back; zones are never
  deleted and never change hub or code.
* Travel: pairs are ordered (A -> B says nothing about B -> A); versions are 1, 2, 3 per pair, unique, and the
  one in force is deterministic (the highest version whose effective_from has passed); no retroactive value
  (effective_from in the past, or before the previous version, is refused for every role); the table is
  append-only and ops_app has SELECT only.
* A missing pair blocks a new dispatch with "Travel time unknown" (request, plan, Suggest), even when the
  caller sends minutes: no fallback. A hub with no home zone is unknown too.
* A visit planned before its home had a zone keeps its recorded minutes.
* A saved plan records the matrix rows it used; a newer version never rewrites it; a changed plan is checked
  fresh.
* planning_set_travel_time as ops_app: admin and ops lead only (forged, NULL, zero and role-less actors are in
  tests/test_v2_03a_definer_audit.py).

SYNTHETIC data only.
"""

from __future__ import annotations

import os
from datetime import timedelta

import psycopg
import pytest
from django.db import connection
from django.utils import timezone

from accounts.models import RoleGrant
from planning import options as popt
from planning import services as ps
from planning import travel, zones
from planning.models import TravelTime, Visit, VisitRevision
from platform_core.commands import Conflict, PermissionDenied
from platform_core.models import AuditEvent, Hub, Zone
from tests.conftest import make_user
from tests.planning_world import DAY, at, build_planning, new_visit, plan

pytestmark = pytest.mark.django_db(transaction=True)


def _app_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=True,
    )


def _zoned(prefix: str, *, base: bool = True, pair: bool = True):
    """The planning world (one hub HUB-1) with zones ZA (the hub's own zone) and ZB; home S-1 in ZB; travel
    ZA -> ZB 25 min and ZB -> ZA 35 min (demo values) unless ``pair`` is False."""
    w = build_planning(robots=3, prefix=prefix)
    w.hub = Hub.objects.get()
    w.za = Zone.objects.get(
        pk=int(zones.create_zone(w.ops, hub_code=w.hub.code, code="ZA", name="Hub side")["entity_id"])
    )
    w.zb = Zone.objects.get(
        pk=int(zones.create_zone(w.ops, hub_code=w.hub.code, code="ZB", name="Hills")["entity_id"])
    )
    if base:
        zones.set_hub_home_zone(w.ops, hub_code=w.hub.code, zone_code="ZA")
    if pair:
        _set(w, w.za, w.zb, 25)
        _set(w, w.zb, w.za, 35)
    zones.set_site_zone(w.ops, site_code="S-1", hub_code=w.hub.code, zone_code="ZB")
    return w


def _set(w, a: Zone, b: Zone, minutes: int, **kw) -> int:
    return int(
        travel.set_travel_time(
            w.ops, from_zone_id=a.pk, to_zone_id=b.pk, minutes=minutes, source=kw.pop("source", "demo"), **kw
        )["entity_id"]
    )


def _rev(code: str) -> VisitRevision:
    v = Visit.objects.get(code=code)
    assert v.current_revision is not None
    return v.current_revision


def _codes(r: VisitRevision) -> list[str]:
    return [b["code"] for b in r.blockers]


# ================================================================== zones
def test_a_visit_takes_its_homes_zone_and_a_forged_zone_is_ignored_or_refused():
    w = _zoned("zz_")
    code = new_visit(w)
    v = Visit.objects.get(code=code)
    assert v.zone_id == w.zb.pk  # from the home, set at plan time
    assert zones.zone_of(v) == w.zb
    with _app_conn() as conn, conn.cursor() as cur:
        # a direct UPDATE to another zone is refused: the zone is always the home's
        with pytest.raises(psycopg.errors.CheckViolation, match="always its home's zone"):
            cur.execute("UPDATE planning_visit SET zone_id = %s WHERE id = %s", [w.za.pk, v.pk])
        with pytest.raises(psycopg.errors.CheckViolation, match="always its home's zone"):
            cur.execute("UPDATE planning_visit SET zone_id = NULL WHERE id = %s", [v.pk])
    v.refresh_from_db()
    assert v.zone_id == w.zb.pk
    # a home without a zone: the visit's zone is unknown, and shown as such
    zones.set_site_zone(w.ops, site_code="S-2", hub_code=None, zone_code=None)
    v2 = Visit.objects.get(code=new_visit(w, site="S-2"))
    assert v2.zone_id is None and zones.zone_of(v2) is None
    assert zones.zone_label(None) == "Unknown zone"


def test_a_retired_zone_keeps_its_history_and_cannot_be_used_for_new_visits():
    w = _zoned("zr_")
    old = new_visit(w)
    plan(w, old)
    rows = TravelTime.objects.count()
    zones.retire_zone(w.ops, hub_code=w.hub.code, code="ZB")
    w.zb.refresh_from_db()
    assert not w.zb.active
    # history kept: the old visit keeps its zone and its plan, the travel rows stay
    v = Visit.objects.get(code=old)
    assert v.zone_id == w.zb.pk and _rev(old).travel_version_id is not None
    assert TravelTime.objects.count() == rows
    # new visits cannot use it (the home still points at it)
    with pytest.raises(Conflict, match="Zone ZB is retired; new visits cannot use it"):
        new_visit(w)
    # its pairs are not offered any more
    assert travel.minutes(w.za, w.zb, at(DAY, 10)) == travel.NONE
    assert travel.minutes(w.za, w.zb, at(DAY, 10)).label == "Travel time unknown"
    with pytest.raises(Conflict, match="retired"):
        _set(w, w.za, w.zb, 20)
    # a re-plan of the old visit is blocked (zone retired, no travel), never planned on stale values
    with pytest.raises(Conflict, match="Travel time unknown"):
        plan(w, old, robot="R-02")
    # it cannot be given to a home or be the hub's zone, and never comes back
    with pytest.raises(Conflict, match="retired"):
        zones.set_site_zone(w.ops, site_code="S-2", hub_code=w.hub.code, zone_code="ZB")
    with pytest.raises(Conflict, match="retired"):
        zones.set_hub_home_zone(w.ops, hub_code=w.hub.code, zone_code="ZB")
    with _app_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be used again"):
            cur.execute("UPDATE platform_core_zone SET active = true WHERE id = %s", [w.zb.pk])
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot change its hub or code"):
            cur.execute("UPDATE platform_core_zone SET code = 'ZQ' WHERE id = %s", [w.za.pk])
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM platform_core_zone WHERE id = %s", [w.za.pk])
    with connection.cursor() as cur, pytest.raises(Exception):  # noqa: B017 - the owner too (reject_mutation)
        cur.execute("DELETE FROM platform_core_zone WHERE id = %s", [w.za.pk])


def test_zone_setup_is_admin_or_ops_lead_and_zone_codes_are_unique_per_hub():
    w = _zoned("zs_", pair=False)
    tech = make_user("zs_tech")
    RoleGrant.objects.create(user=tech, role="hub_technician", hub=w.hub)
    with pytest.raises(PermissionDenied):
        zones.create_zone(tech, hub_code=w.hub.code, code="ZC", name="C")
    with pytest.raises(Conflict, match="already has a zone ZA"):
        zones.create_zone(w.ops, hub_code=w.hub.code, code="ZA", name="again")
    admin = make_user("zs_admin", "admin")
    zones.create_zone(admin, hub_code=w.hub.code, code="ZC", name="C")
    assert Zone.objects.filter(hub=w.hub).count() == 3


# ================================================================== travel matrix
def test_pairs_are_ordered():
    w = _zoned("to_", pair=False)
    _set(w, w.za, w.zb, 25)
    t = at(DAY, 10)
    out = travel.minutes(w.za, w.zb, t)
    assert (out.minutes, out.source) == (25, "demo") and out.version_id is not None
    assert travel.minutes(w.zb, w.za, t) == travel.NONE  # B -> A is a different pair: unknown
    _set(w, w.zb, w.za, 40, source="company")
    assert travel.minutes(w.zb, w.za, t).minutes == 40
    assert travel.minutes(w.zb, w.za, t).label == "40 min (company value)"
    assert travel.minutes(w.za, w.zb, t).minutes == 25  # unchanged


def test_versions_are_unique_and_the_value_in_force_is_deterministic():
    w = _zoned("tv_", pair=False)
    v1 = _set(w, w.za, w.zb, 25)
    v2 = _set(w, w.za, w.zb, 30)
    later = timezone.now() + timedelta(days=2)
    v3 = _set(w, w.za, w.zb, 45, effective_from=later)
    assert list(
        TravelTime.objects.filter(from_zone=w.za, to_zone=w.zb)
        .order_by("version")
        .values_list("id", "version")
    ) == [(v1, 1), (v2, 2), (v3, 3)]
    now_value = travel.minutes(w.za, w.zb, timezone.now() + timedelta(hours=1))
    assert (now_value.minutes, now_value.version_id) == (30, v2)  # v3 is not in force yet
    after = travel.minutes(w.za, w.zb, later + timedelta(minutes=1))
    assert (after.minutes, after.version_id) == (45, v3)
    # the same answer every time (one row per pair and moment)
    assert {travel.minutes(w.za, w.zb, later + timedelta(minutes=1)) for _ in range(3)} == {after}
    # a version number is never reused or skipped, for any role: the unique key and the insert guard
    with connection.cursor() as cur:
        for version in (3, 5):
            with pytest.raises(Exception, match="next version of this pair is 4"):
                cur.execute(
                    "INSERT INTO planning_travel_time (from_zone_id, to_zone_id, minutes, version, source, "
                    "effective_from, created_by_id, created_at) VALUES (%s, %s, 10, %s, 'demo', now() + interval "
                    "'3 days', %s, now())",
                    [w.za.pk, w.zb.pk, version, w.ops.pk],
                )
    # the matrix page reads one statement: the value in force and the pending version
    cells = travel.matrix([w.za.pk, w.zb.pk])
    cell = cells[(w.za.pk, w.zb.pk)]
    assert (cell.current.minutes, cell.version, cell.pending) == (30, 2, 1)
    assert cells[(w.zb.pk, w.za.pk)].current == travel.NONE


def test_no_retroactive_travel_time_and_the_table_is_append_only():
    w = _zoned("tr_", pair=False)
    first = _set(w, w.za, w.zb, 25, effective_from=timezone.now() + timedelta(days=3))
    with pytest.raises(Conflict, match="never in the past"):
        _set(w, w.za, w.zb, 20, effective_from=timezone.now() - timedelta(hours=1))
    with pytest.raises(Conflict, match="before the version it replaces"):
        _set(w, w.za, w.zb, 20, effective_from=timezone.now() + timedelta(days=1))
    assert TravelTime.objects.count() == 1
    with connection.cursor() as cur:  # the owner too
        with pytest.raises(Exception, match="never in the past"):
            cur.execute(
                "INSERT INTO planning_travel_time (from_zone_id, to_zone_id, minutes, version, source, "
                "effective_from, created_by_id, created_at) VALUES (%s, %s, 10, 2, 'demo', now() - interval "
                "'1 minute', %s, now())",
                [w.za.pk, w.zb.pk, w.ops.pk],
            )
        with pytest.raises(Exception):  # noqa: B017 - reject_mutation
            cur.execute("UPDATE planning_travel_time SET minutes = 1 WHERE id = %s", [first])
        with pytest.raises(Exception):  # noqa: B017
            cur.execute("DELETE FROM planning_travel_time WHERE id = %s", [first])
    with _app_conn() as conn, conn.cursor() as cur:
        for sql in (
            "UPDATE planning_travel_time SET minutes = 1",
            "DELETE FROM planning_travel_time",
            "INSERT INTO planning_travel_time (from_zone_id, to_zone_id, minutes, version, source, effective_from, "
            f"created_by_id, created_at) VALUES ({w.za.pk}, {w.za.pk}, 5, 1, 'demo', now(), {w.ops.pk}, now())",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql)
        cur.execute("SELECT count(*) FROM planning_travel_time")
        assert cur.fetchone()[0] == 1
    assert TravelTime.objects.get(pk=first).minutes == 25


def test_only_an_admin_or_the_ops_lead_sets_a_travel_time_by_direct_sql_as_ops_app():
    w = _zoned("ta_", pair=False)
    tech, admin = make_user("ta_tech"), make_user("ta_admin", "admin")
    RoleGrant.objects.create(user=tech, role="hub_technician", hub=w.hub)
    sql = "SELECT planning_set_travel_time(%s, %s, %s, 20, 'company', NULL, 'req-1')"
    audits = AuditEvent.objects.count()
    with _app_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="admin or the ops lead"):
            cur.execute(sql, [tech.pk, w.za.pk, w.zb.pk])
        assert not TravelTime.objects.exists() and AuditEvent.objects.count() == audits
        cur.execute(sql, [admin.pk, w.za.pk, w.zb.pk])
        cur.execute(sql, [w.ops.pk, w.za.pk, w.zb.pk])
    assert list(TravelTime.objects.order_by("version").values_list("version", "created_by_id")) == [
        (1, admin.pk),
        (2, w.ops.pk),
    ]
    a = AuditEvent.objects.filter(command="planning.set_travel_time").order_by("id")
    assert a.count() == 2 and a.last().payload["from_zone"] == "ZA" and a.last().result["version"] == 2


# ================================================================== missing pair, old visits, saved plans
def test_a_missing_pair_blocks_a_new_dispatch_with_travel_time_unknown_and_no_fallback():
    w = _zoned("tm_", pair=False)
    code = new_visit(w, travel=30)  # minutes sent by the caller are NOT a fallback for a zoned visit
    r = _rev(code)
    assert r.feasibility == "blocked" and "TRAVEL_UNKNOWN" in _codes(r)
    assert (r.travel_out_min, r.travel_back_min, r.travel_version_id) == (None, None, None)
    out = plan(w, code)
    assert out["feasibility"] == "blocked" and "Travel time unknown" in out["blockers"]
    # Suggest never offers it as free
    v = Visit.objects.get(code=code)
    s = popt.suggest(v, popt.plan_options(v))
    assert not s.found and any("Travel time unknown" in x for x in s.why_not)
    # one direction is not enough
    _set(w, w.za, w.zb, 25)
    assert plan(w, code)["feasibility"] == "blocked"
    _set(w, w.zb, w.za, 35)
    done = plan(w, code)
    assert done["feasibility"] == "feasible", done
    # a hub with no zone of its own: unknown again (never a guess)
    zones.set_hub_home_zone(w.ops, hub_code=w.hub.code, zone_code=None)
    code2 = new_visit(w, d=DAY + timedelta(days=1))
    assert "TRAVEL_UNKNOWN" in _codes(_rev(code2))


def test_a_visit_planned_before_its_home_had_a_zone_keeps_its_recorded_minutes():
    w = build_planning(robots=3, prefix="tk_")
    code = new_visit(w, travel=30)
    plan(w, code)
    before = _rev(code)
    assert (before.travel_out_min, before.travel_version_id) == (30, None)
    # zones and a (different) matrix value arrive afterwards
    hub = Hub.objects.get()
    za = Zone.objects.create(hub=hub, code="ZA", name="A")
    zones.set_hub_home_zone(w.ops, hub_code=hub.code, zone_code="ZA")
    travel.set_travel_time(w.ops, from_zone_id=za.pk, to_zone_id=za.pk, minutes=55, source="demo")
    zones.set_site_zone(w.ops, site_code="S-1", hub_code=hub.code, zone_code="ZA")
    v = Visit.objects.get(code=code)
    assert v.zone_id is None  # unchanged until it is planned again
    live = ps.live_feasibility(v)
    assert live.feasibility == "feasible" and "TRAVEL_UNKNOWN" not in [b["code"] for b in live.blockers]
    assert _rev(code).pk == before.pk and _rev(code).travel_out_min == 30


def test_a_saved_plan_records_its_matrix_version_and_a_changed_plan_is_checked_fresh():
    w = _zoned("tp_", pair=False)
    out1 = _set(w, w.za, w.zb, 25)
    back1 = _set(w, w.zb, w.za, 35)
    code = new_visit(w)
    res = plan(w, code)
    r1 = _rev(code)
    assert (r1.travel_out_min, r1.travel_back_min) == (25, 35)
    assert (r1.travel_version_id, r1.travel_back_version_id) == (out1, back1)
    assert (res["travel_version_id"], res["travel_back_version_id"]) == (out1, back1)
    assert r1.snapshot["travel"]["source"] == "matrix"
    # a new version never rewrites the saved plan
    out2 = _set(w, w.za, w.zb, 40)
    r1.refresh_from_db()
    assert (r1.travel_out_min, r1.travel_version_id) == (25, out1)
    # Suggest names the matrix rows it used
    v = Visit.objects.get(code=code)
    s = popt.suggest(v, popt.plan_options(v))
    assert s.found and any(f"rows {out2} and {back1}" in x for x in s.explanations), s.explanations
    # re-planning is a new decision on the matrix in force
    plan(w, code, robot="R-02", reason="swap robot")
    r2 = _rev(code)
    assert r2.pk != r1.pk and (r2.travel_out_min, r2.travel_version_id) == (40, out2)
    assert (r2.travel_back_min, r2.travel_back_version_id) == (35, back1)


# ================================================================== S5 pages
def test_the_zone_and_travel_pages_show_unknown_cells_and_history():
    from django.test import Client

    w = _zoned("tg_", pair=False)
    _set(w, w.za, w.zb, 25)
    c = Client()
    c.force_login(w.ops)
    zl = c.get(f"/settings/hubs/{w.hub.code}/zones/").content.decode()
    assert "Hub side" in zl and "the hub's own zone" in zl and "Hills" in zl
    grid = c.get("/settings/travel-times/").content.decode()
    assert "25 min" in grid and "demo value · v1" in grid
    assert "▲ Unknown" in grid and 'data-unknown="3"' in grid  # ZA->ZA, ZB->ZA, ZB->ZB
    pair = c.get(f"/settings/travel-times/{w.za.pk}/{w.zb.pk}/").content.decode()
    assert "v1" in pair and "25 min (demo value)" in pair
    back = c.get(f"/settings/travel-times/{w.zb.pk}/{w.za.pk}/").content.decode()
    assert "Travel time unknown" in back
    import re

    token = re.search(r'name="_form_token" value="([^"]+)"', back).group(1)
    r = c.post(
        f"/settings/travel-times/{w.zb.pk}/{w.za.pk}/",
        {"minutes": "35", "source": "company", "effective_from": "", "_form_token": token},
    )
    assert r.status_code == 302, r.content.decode()[:400]
    assert travel.minutes(w.zb, w.za, at(DAY, 10)).minutes == 35
    tech = make_user("tg_tech")
    RoleGrant.objects.create(user=tech, role="hub_technician", hub=w.hub)
    c2 = Client()
    c2.force_login(tech)
    assert c2.get("/settings/travel-times/").status_code == 403


# ================================================================== the travel origin (03B-2 integration)
def test_the_hub_origin_zone_gives_the_outbound_pair_and_the_reverse_pair_gives_the_return():
    from django.test.utils import CaptureQueriesContext

    w = _zoned("th_")
    assert zones.hub_origin_zone(w.hub.pk) == w.za
    hub = Hub.objects.select_related("home_zone").get(pk=w.hub.pk)
    with CaptureQueriesContext(connection) as q:
        assert zones.hub_origin_zone(hub) == w.za
    assert len(q.captured_queries) == 0
    t0, t1 = at(DAY, 10), at(DAY, 11)
    r = travel.route(w.hub.pk, w.zb.pk, t0, t1)
    assert r.out == travel.minutes(zones.hub_origin_zone(w.hub.pk), w.zb, t0)
    assert r.back == travel.minutes(w.zb, zones.hub_origin_zone(w.hub.pk), t1)
    assert (r.out.minutes, r.back.minutes) == (25, 35)
    # zone_of reads a loaded FK without a statement
    code = new_visit(w, d=DAY + timedelta(days=2))
    v = Visit.objects.select_related("zone").get(code=code)
    with CaptureQueriesContext(connection) as q:
        assert zones.zone_of(v) == w.zb
    assert len(q.captured_queries) == 0
    # the origin must be a zone of that hub
    other = Hub.objects.create(code="HX", name="Hub X", time_zone=w.hub.time_zone)
    foreign = Zone.objects.create(hub=other, code="XZ", name="X zone")
    with _app_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="belongs to another hub"):
            cur.execute(
                "UPDATE platform_core_hub SET home_zone_id = %s WHERE id = %s", [foreign.pk, w.hub.pk]
            )

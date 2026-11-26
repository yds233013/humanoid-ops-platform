"""v2 Phase 06 zone-consistency regressions (EM gate list): one place that walks every way a home's zone can
change under an existing plan, and proves each screen, check and database gate says the same thing.

The three changes (13.5: a visit keeps the zone of its home at plan time until it is planned again):

* ``gets_zone``:  a home with no zone is put in zone ZB after its visit was planned;
* ``moves``:      a home in zone ZB is moved to zone ZC;
* ``loses_zone``: a home in zone ZB is set back to no zone (unknown).

For each change:

1. the existing plan is stale: ``home_zone_changed``, the readiness blocker PLANNING_HOME_ZONE_CHANGED, a Blocked
   board row naming the change, and authorization refused (service, and the database by direct SQL as ops_app);
2. a review of the re-plan proposal says what the save then does, with nothing changed in between (same travel
   result, same verdict, the same minutes and zone recorded);
3. a successful re-plan clears the stale blocker and the visit can be authorized and leave;
4. a departure on an authorization issued before the change is refused by the database (service, and a direct
   departure INSERT as ops_app);
5. even a change and its undo (ZB -> ZC -> ZB, the plan is no longer stale) leave the old authorization
   unusable: the home's guard version moved. A re-plan does not revive it either; a new authorization is needed.

Reuses the 03B / 06z helpers (worlds, raw SQL as ops_app, the board row parser); it does not repeat the
planner wording tests of tests/test_v2_06z_zone_truth.py or the three review cases of
tests/test_v2_06z_review_matches_save.py. SYNTHETIC data only.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import psycopg
import pytest
from django.test import Client

from planning import services as ps
from planning import travel, zones
from planning.models import Visit
from platform_core.commands import Conflict
from platform_core.models import Hub, Zone
from readiness import commands as rc
from readiness.evaluator import PLANNING_HOME_ZONE_CHANGED, evaluate_readiness
from readiness.models import DepartureAuthorization, DepartureEvent
from tests.planning_world import plan
from tests.readiness_world import build_readiness, record_all_evidence
from tests.test_readiness_authz_boundary import issue, live_args
from tests.test_v2_03a_transfers import _app_conn
from tests.test_v2_03b_pause import _raw_departure
from tests.test_v2_06z_zone_truth import BOARD, _row

pytestmark = pytest.mark.django_db(transaction=True)

MOVED_SQL = "after this visit was planned"  # operations_pause_gate_problem (planning/0014)
MOVED_SERVICE = "moved to another zone after this visit was planned"  # the readiness blocker
REPLAN_ROBOT = "R-02"  # the re-plan proposal: another robot, same crew and van
# change -> (zone the visit was planned with, the home's new zone, the board's sentence)
CHANGES = {
    "gets_zone": (None, "ZB", "Home S-1 was put in zone Hills (ZB) after this visit was planned"),
    "moves": (
        "ZB",
        "ZC",
        "Home S-1 moved from zone Hills (ZB) to zone Coast (ZC) after this visit was planned",
    ),
    "loses_zone": (
        "ZB",
        None,
        "Home S-1 no longer has a zone; it was in zone Hills (ZB) when this visit was",
    ),
}


# ------------------------------------------------------------------ world and helpers
def _world(prefix: str, planned_zone: str | None) -> SimpleNamespace:
    """The readiness world (w.visit confirmed with all its evidence) at HUB-1 with zones ZA (the hub's own),
    ZB "Hills" and ZC "Coast", travel ZA <-> ZB 30/30 and ZA <-> ZC 35/45 (demo). With ``planned_zone`` the
    home is put there first and the visit planned again, so the plan is current in that zone."""
    w = build_readiness(prefix=prefix)
    w.hub = Hub.objects.get()
    for code, name in (("ZA", "Hub side"), ("ZB", "Hills"), ("ZC", "Coast")):
        zones.create_zone(w.ops, hub_code=w.hub.code, code=code, name=name)
    w.z = {z.code: z for z in Zone.objects.filter(hub=w.hub)}
    zones.set_hub_home_zone(w.ops, hub_code=w.hub.code, zone_code="ZA")
    for to, out, back in (("ZB", 30, 30), ("ZC", 35, 45)):
        travel.set_travel_time(
            w.ops, from_zone_id=w.z["ZA"].pk, to_zone_id=w.z[to].pk, minutes=out, source="demo"
        )
        travel.set_travel_time(
            w.ops, from_zone_id=w.z[to].pk, to_zone_id=w.z["ZA"].pk, minutes=back, source="demo"
        )
    if planned_zone is not None:
        _move(w, planned_zone)
        _replan(w)
    assert _visit(w).zone_id == (w.z[planned_zone].pk if planned_zone else None)
    assert not _stale(w)
    return w


def _visit(w: SimpleNamespace) -> Visit:
    return Visit.objects.select_related("site", "zone", "current_revision").get(code=w.visit)


def _move(w: SimpleNamespace, zone_code: str | None) -> None:
    zones.set_site_zone(
        w.ops, site_code="S-1", hub_code=w.hub.code if zone_code else None, zone_code=zone_code,
        reason="SYN: 06 zone regression",
    )  # fmt: skip


def _replan(w: SimpleNamespace, robot: str = "R-01") -> dict:
    out = plan(w, w.visit, robot=robot)
    ps.confirm_visit(w.ops, visit_code=w.visit)
    record_all_evidence(w, w.visit, robot=robot)
    return out


def _stale(w: SimpleNamespace) -> bool:
    v = _visit(w)
    codes = {b["code"] for b in evaluate_readiness(v).blockers}
    assert (PLANNING_HOME_ZONE_CHANGED in codes) == zones.home_zone_changed(v), codes
    return zones.home_zone_changed(v)


def _refused_by_the_database_at_authorization(w: SimpleNamespace) -> None:
    before = DepartureAuthorization.objects.count()
    with pytest.raises(Conflict, match=MOVED_SERVICE):
        rc.authorize_departure(w.ops, visit_code=w.visit)
    with (
        _app_conn() as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match=MOVED_SQL),
    ):
        issue(cur, live_args(w))
    assert DepartureAuthorization.objects.count() == before


def _refused_by_the_database_at_departure(w: SimpleNamespace, auth: DepartureAuthorization) -> None:
    with pytest.raises(Conflict):
        rc.record_departure(w.ops, authorization_code=auth.code)
    with _app_conn() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        _raw_departure(cur, auth, w.ops)
    assert not DepartureEvent.objects.filter(authorization=auth).exists()


def _review(w: SimpleNamespace) -> tuple[str, str, str]:
    """(travel check result, its text, verdict) of the review panel for re-planning with ``REPLAN_ROBOT``."""
    c = Client()
    c.force_login(w.ops)
    query = f"review=1&robot={REPLAN_ROBOT}&lead=P-1&second=P-2&vehicle=VAN-1"
    r = c.get(f"/planning/visits/{w.visit}/?{query}")
    assert r.status_code == 200
    html = r.content.decode()
    m = re.search(r'data-check="travel" data-result="([a-z]+)".*?</li>', html, re.S)
    verdict = re.search(r'data-verdict="([a-z_]*)"', html)
    assert m and verdict, "no travel check or verdict on the review panel"
    return m.group(1), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(0))), verdict.group(1)


# ================================================================== 1-3: stale, review = save, re-plan clears
@pytest.mark.parametrize("change", list(CHANGES))
def test_a_zone_change_makes_the_plan_stale_and_only_a_re_plan_clears_it(change):
    planned, new, board_text = CHANGES[change]
    w = _world(f"zr{change[:2]}_", planned)
    _move(w, new)

    # 1. stale everywhere: the predicate, readiness, the board and both authorization paths
    assert _stale(w)
    v = _visit(w)
    assert v.zone_id == (w.z[planned].pk if planned else None)  # the visit keeps its planned zone
    c = Client()
    c.force_login(w.ops)
    board = c.get(BOARD).content.decode()
    assert 'data-status="blocked"' in _row(board, w.visit)
    assert board_text in board
    _refused_by_the_database_at_authorization(w)

    # 2. the review of the re-plan says what the save does (nothing changes in between)
    result, text, verdict = _review(w)
    assert verdict == "feasible", (result, text, verdict)
    expected = {"ZB": (30, 30), "ZC": (35, 45)}.get(new or "", (30, 30))
    if new is None:
        assert result == "risk" and "Legacy travel estimate: 30 min out, 30 min back" in text, text
    else:
        assert result == "pass" and f"{expected[0]} min out, {expected[1]} min back" in text, text
        assert "from the travel matrix" in text
    out = _replan(w, robot=REPLAN_ROBOT)
    v = _visit(w)
    rev = v.current_revision
    assert rev.feasibility == "feasible" and rev.robot.code == REPLAN_ROBOT and out
    assert v.zone_id == (w.z[new].pk if new else None)  # the re-plan takes the home's current zone
    assert (rev.travel_out_min, rev.travel_back_min) == expected

    # 3. the stale blocker is gone; the visit can be authorized and leave
    assert not _stale(w)
    code = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    rc.record_departure(w.ops, authorization_code=code)
    assert DepartureEvent.objects.filter(visit__code=w.visit).count() == 1


# ================================================================== 4: departure on a stale plan
@pytest.mark.parametrize("change", list(CHANGES))
def test_an_authorization_issued_before_a_zone_change_is_refused_at_departure_by_the_database(change):
    planned, new, _ = CHANGES[change]
    w = _world(f"zd{change[:2]}_", planned)
    auth = DepartureAuthorization.objects.get(code=rc.authorize_departure(w.ops, visit_code=w.visit)["code"])
    _move(w, new)
    assert _stale(w)
    _refused_by_the_database_at_departure(w, auth)


# ================================================================== 5: change and undo, then re-plan
def test_a_change_and_its_undo_still_leave_the_old_authorization_unusable():
    w = _world("zu_", "ZB")
    auth = DepartureAuthorization.objects.get(code=rc.authorize_departure(w.ops, visit_code=w.visit)["code"])
    _move(w, "ZC")
    _move(w, "ZB")  # back where it was planned: the plan is current again ...
    assert not _stale(w)
    _refused_by_the_database_at_departure(w, auth)  # ... but the home's guard version moved twice
    # a re-plan does not revive it; a new authorization on the re-plan can leave
    _replan(w)
    _refused_by_the_database_at_departure(w, auth)
    code = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    assert code != auth.code
    rc.record_departure(w.ops, authorization_code=code)
    assert list(DepartureEvent.objects.values_list("authorization__code", flat=True)) == [code]

"""v2 06 zone fix: every screen says the same truth about a home whose zone changed after its visit was planned.

Found on the demo workspace (Oakland, V-0032): the demo books the visits that already started before it gives
their homes a zone (a travel value never takes effect in the past, 13.4 item 3), so those visits keep no zone
while their homes are in zone LM. That is a genuine change after planning (13.5: a visit's zone is its home's
zone at plan time; it keeps it until it is planned again), and readiness blocks it (PLANNING_HOME_ZONE_CHANGED).

Proven here:

1. The readiness page header never says "Planning feasible" while its "Planning still feasible now" check is
   Blocked with PLANNING_HOME_ZONE_CHANGED; the list agrees; after a re-plan both say feasible again.
2. The planner (visit page and review panel) and the dispatch board name the home's zone ("was put in zone Hills
   (ZB)"), say the visit is Blocked until planned again, and never say "this home has no zone yet" for it. A
   home that truly has no zone still says so. A move between two zones names both.
3. The board lists every active zone of a hub, whatever today's visits are; "Zones not recorded yet" only for a
   hub with no zones at all.

SYNTHETIC data only.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client

from planning import services as ps
from planning import travel, zones
from planning.models import Visit
from platform_core.models import Hub, Zone
from tests.planning_world import DAY, plan
from tests.readiness_world import build_readiness, record_all_evidence

pytestmark = pytest.mark.django_db(transaction=True)

BOARD = f"/planning/board/?day={DAY.isoformat()}"
NO_ZONE_YET = "this home has no zone yet"
PUT_IN = "Home S-1 was put in zone Hills (ZB) after this visit was planned"


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _html(c: Client, url: str) -> str:
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code, r.content[:300])
    return str(r.content.decode())


def _zone(w: SimpleNamespace, code: str, name: str) -> Zone:
    return Zone.objects.get(
        pk=int(zones.create_zone(w.ops, hub_code=w.hub.code, code=code, name=name)["entity_id"])
    )


def _world(prefix: str) -> SimpleNamespace:
    """The readiness world (w.visit confirmed with all its evidence, planned while home S-1 had NO zone), then
    the hub's zones ZA (its own), ZB and ZC with travel both ways, and S-1 put in ZB afterwards: the demo's
    order for the visits that already started."""
    w = build_readiness(prefix=prefix)
    w.hub = Hub.objects.get()
    w.za, w.zb, w.zc = _zone(w, "ZA", "Hub side"), _zone(w, "ZB", "Hills"), _zone(w, "ZC", "Coast")
    zones.set_hub_home_zone(w.ops, hub_code=w.hub.code, zone_code="ZA")
    for z in (w.zb, w.zc):
        for a, b in ((w.za, z), (z, w.za)):
            travel.set_travel_time(w.ops, from_zone_id=a.pk, to_zone_id=b.pk, minutes=30, source="demo")
    assert Visit.objects.get(code=w.visit).zone_id is None
    _move(w, "ZB")
    return w


def _move(w: SimpleNamespace, zone_code: str | None) -> None:
    zones.set_site_zone(
        w.ops, site_code="S-1", hub_code=w.hub.code if zone_code else None, zone_code=zone_code,
        reason="SYN: home zone",
    )  # fmt: skip


def _replan(w: SimpleNamespace) -> None:
    plan(w, w.visit)
    ps.confirm_visit(w.ops, visit_code=w.visit)
    record_all_evidence(w, w.visit)


def _header(html: str) -> tuple[str, str]:
    m = re.search(r'Planning: <span class="badge ([a-z_]+)">([^<]+)</span>', html)
    assert m, "no planning header"
    return m.group(1), m.group(2)


def _gate(html: str, name: str) -> str:
    m = re.search(rf"<tr><td>{re.escape(name)}</td>\s*<td><span class=\"badge ([a-z_]+)\">", html)
    assert m, f"no gate row {name}"
    return m.group(1)


def _row(html: str, code: str) -> str:
    m = re.search(rf'<a class="vrow"[^>]*data-row="{code}".*?</a>', html, re.S)
    assert m, f"no row for {code}"
    return m.group(0)


def _metas(html: str) -> list[str]:
    return re.findall(r'<p class="meta">([^<]*)</p>', html)


# ================================================================== 1. readiness header = its own check
def test_readiness_header_is_blocked_when_its_planning_check_is_blocked_by_a_zone_change():
    w = _world("zt1_")
    c = _client(w.ops)
    page = _html(c, f"/readiness/visits/{w.visit}/")
    assert "moved to another zone after this visit was planned" in page  # PLANNING_HOME_ZONE_CHANGED
    assert _gate(page, "Planning still feasible now") == "blocked"
    assert _header(page) == ("blocked", "Blocked")
    assert "Planning feasible" not in page.split("<table", 1)[0]
    # the list reads the same gate
    listing = _html(c, "/readiness/")
    assert w.visit in listing
    # after a re-plan the visit takes zone ZB: header and check agree again, both feasible
    _replan(w)
    assert Visit.objects.get(code=w.visit).zone_id == w.zb.pk
    page = _html(c, f"/readiness/visits/{w.visit}/")
    assert "moved to another zone" not in page
    assert _gate(page, "Planning still feasible now") == "clear"
    assert _header(page) == ("feasible", "Planning feasible")


# ================================================================== 2. the planner and the board name the zone
def test_planner_and_board_name_the_homes_zone_and_never_say_it_has_none():
    w = _world("zt2_")
    c = _client(w.ops)
    visit_page = _html(c, f"/planning/visits/{w.visit}/")
    assert NO_ZONE_YET not in visit_page
    assert PUT_IN in visit_page and "Plan the visit again" in visit_page
    assert "planned before this home was put in zone Hills (ZB)" in visit_page  # the legacy travel line
    assert "Set the home&#x27;s zone" not in visit_page and "Set the home's zone" not in visit_page
    assert "Nothing is blocking this plan." not in visit_page
    assert re.search(r'<span class="badge blocked">Blocked</span>', visit_page)
    review = _html(c, f"/planning/visits/{w.visit}/?review=1")
    assert NO_ZONE_YET not in review and PUT_IN in review

    board = _html(c, BOARD)
    row = _row(board, w.visit)
    assert NO_ZONE_YET not in board
    assert 'data-status="blocked"' in row
    assert "planned before this home was put in zone Hills (ZB)" in row
    assert PUT_IN in board

    # a move between two zones names both; the board row shows the planned zone and the move
    _replan(w)
    _move(w, "ZC")
    board = _html(c, BOARD)
    assert "Home S-1 moved from zone Hills (ZB) to zone Coast (ZC) after this visit was planned" in board
    assert 'data-status="blocked"' in _row(board, w.visit)


def test_a_home_that_truly_has_no_zone_still_says_so():
    w = build_readiness(prefix="zt3_")
    c = _client(w.ops)
    assert NO_ZONE_YET in _html(c, f"/planning/visits/{w.visit}/")
    board = _html(c, BOARD)
    assert NO_ZONE_YET in _row(board, w.visit)
    assert "after this visit was planned" not in board
    page = _html(c, f"/readiness/visits/{w.visit}/")
    assert "moved to another zone" not in page and _header(page)[0] != "blocked"


# ================================================================== 3. the board's zones are the hub's
def test_board_lists_every_zone_of_the_hub_not_only_those_of_todays_visits():
    w = build_readiness(prefix="zt4_")
    w.hub = Hub.objects.get()
    c = _client(w.ops)
    # no zones at all: the only case that says "Zones not recorded yet"
    metas = _metas(_html(c, BOARD))
    assert metas and all(m.endswith("Zones not recorded yet") for m in metas), metas
    # three zones, the day's only visit in none of them (its home has no zone): all three are listed
    for code, name in (("ZA", "Hub side"), ("ZB", "Hills"), ("ZC", "Coast")):
        _zone(w, code, name)
    board = _html(c, BOARD)
    assert "Zones not recorded yet" not in board
    assert any(m.endswith("Zones: Coast, Hills, Hub side") for m in _metas(board)), _metas(board)
    # on a day with no visit at all, the same
    empty_day = _html(c, "/planning/board/?day=2026-12-20")
    assert any(m.endswith("Zones: Coast, Hills, Hub side") for m in _metas(empty_day)), _metas(empty_day)
    # a retired zone is no longer listed
    zones.retire_zone(w.ops, hub_code=w.hub.code, code="ZC")
    assert any(m.endswith("Zones: Hills, Hub side") for m in _metas(_html(c, BOARD)))

"""v2 06 zone fix: the planner's review panel checks a proposed plan exactly as the save (plan_visit) will.

The save gives the visit its home's CURRENT zone and, for a zoned home, takes the travel minutes from the matrix
in force (a missing pair blocks); a home with no zone keeps the minutes recorded on the visit. The panel used
to check the visit's old (empty) zone with the recorded minutes, so it could promise what the save refused.

Proven here, for the same proposal (another robot), the panel's travel check and verdict and the save's
decision agree:

(a) a home put in a zone with a matrix pair after the visit was planned: PASS with the matrix minutes; the save
    is feasible and records those minutes and the zone;
(b) a home put in a zone with NO pair: FAIL ("Travel time unknown") and "If you save: Blocked"; the save is
    refused and the current plan stays;
(c) a home with no zone: the recorded minutes (at risk, "Legacy travel estimate"); the save keeps them.

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
from platform_core.commands import Conflict
from platform_core.models import Hub, Zone
from tests.planning_world import DAY, at, build_planning, plan

pytestmark = pytest.mark.django_db(transaction=True)

PROPOSAL = "review=1&robot=R-02&lead=P-1&second=P-2&vehicle=VAN-1"


def _world() -> SimpleNamespace:
    """HUB-1 with zones ZA (its own), ZB (pair ZA<->ZB 25 out / 40 back, demo) and ZC (no pair); visit w.visit
    planned for R-01 while home S-1 had no zone (recorded 30 / 30)."""
    w = build_planning(robots=3)
    w.hub = Hub.objects.get()
    made = {}
    for code, name in (("ZA", "Hub side"), ("ZB", "Hills"), ("ZC", "Coast")):
        made[code] = Zone.objects.get(
            pk=int(zones.create_zone(w.ops, hub_code=w.hub.code, code=code, name=name)["entity_id"])
        )
    w.za, w.zb, w.zc = made["ZA"], made["ZB"], made["ZC"]
    zones.set_hub_home_zone(w.ops, hub_code=w.hub.code, zone_code="ZA")
    travel.set_travel_time(w.ops, from_zone_id=w.za.pk, to_zone_id=w.zb.pk, minutes=25, source="demo")
    travel.set_travel_time(w.ops, from_zone_id=w.zb.pk, to_zone_id=w.za.pk, minutes=40, source="demo")
    w.visit = str(
        ps.request_visit(
            w.ops, site_code="S-1", on_site_start=at(DAY, 10), on_site_end=at(DAY, 11), travel_out_min=30,
            travel_back_min=30, tasks=["T-04"], required_scopes=["presence"], hub_code=w.hub.code,
        )["entity_id"]
    )  # fmt: skip
    plan(w, w.visit)
    assert Visit.objects.get(code=w.visit).zone_id is None
    return w


def _move(w: SimpleNamespace, zone_code: str) -> None:
    zones.set_site_zone(
        w.ops, site_code="S-1", hub_code=w.hub.code, zone_code=zone_code, reason="SYN: home zone"
    )


def _panel(w: SimpleNamespace) -> tuple[str, str, str]:
    """(travel check result, its text, verdict) for the proposal."""
    c = Client()
    c.force_login(w.ops)
    r = c.get(f"/planning/visits/{w.visit}/?{PROPOSAL}")
    assert r.status_code == 200
    html = r.content.decode()
    m = re.search(r'data-check="travel" data-result="([a-z]+)".*?</li>', html, re.S)
    assert m, "no travel check"
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(0)))
    verdict = re.search(r'data-verdict="([a-z_]*)"', html)
    assert verdict, "no verdict"
    return m.group(1), text, verdict.group(1)


def _save(w: SimpleNamespace) -> dict[str, Any]:
    return ps.plan_visit(
        w.ops, visit_code=w.visit, robot_code="R-02", lead_code="P-1", second_code="P-2", vehicle_code="VAN-1"
    )


def test_a_zoned_home_with_a_matrix_pair_passes_with_the_matrix_minutes_on_both():
    w = _world()
    _move(w, "ZB")
    result, text, verdict = _panel(w)
    assert result == "pass", text
    assert "25 min out, 40 min back" in text and "from the travel matrix" in text
    assert "Legacy travel estimate" not in text
    assert verdict == "feasible"
    _save(w)
    v = Visit.objects.select_related("current_revision").get(code=w.visit)
    rev = v.current_revision
    assert v.zone_id == w.zb.pk and rev.robot.code == "R-02"
    assert (rev.travel_out_min, rev.travel_back_min) == (25, 40)
    assert rev.feasibility == "feasible"


def test_a_zoned_home_with_a_missing_pair_fails_on_both():
    w = _world()
    _move(w, "ZC")
    result, text, verdict = _panel(w)
    assert result == "fail" and "Travel time unknown" in text, text
    assert verdict == "blocked"
    before = Visit.objects.get(code=w.visit).current_revision_id
    with pytest.raises(Conflict, match="Travel time unknown"):
        _save(w)
    v = Visit.objects.get(code=w.visit)
    assert v.current_revision_id == before and v.current_revision.robot.code == "R-01"


def test_a_home_with_no_zone_uses_the_recorded_minutes_on_both():
    w = _world()
    result, text, verdict = _panel(w)
    assert result == "risk" and "Legacy travel estimate: 30 min out, 30 min back" in text, text
    assert "this home has no zone yet" in text
    assert verdict == "feasible"
    _save(w)
    v = Visit.objects.select_related("current_revision").get(code=w.visit)
    rev = v.current_revision
    assert v.zone_id is None and rev.robot.code == "R-02"
    assert (rev.travel_out_min, rev.travel_back_min) == (30, 30)
    assert rev.feasibility == "feasible"

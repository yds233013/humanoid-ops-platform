"""v2 Phase 03A closure: read scope of the remaining operations pages (EM review 2026-11-25, invariant I-8).

A hub-scoped viewer (a field operator or hub technician with grants at one hub) sees only that hub's rows on
Robot status, Issues, the incident queue and the incident list, the timeline, the close-out and People, through
the pages and the JSON API, and another hub's incident or person detail answers the plain 403 of the other detail
pages. A regional ops lead sees both hubs. Metrics, health and capacity are open only to regional roles
(operations.metrics: ops lead, engineering lead, safety reviewer, founder), so no hub-scoped user reaches them.

World: the execution world at HUB-1 (R-01 departed on run w.run, an incident on that run) plus hub HB with robot
RB-1, an incident on RB-1 (no run), a field operator and a hub technician granted only at HB, and a person P-9 at
HB. SYNTHETIC data only.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client

from execution.models import Incident
from fleet import services as fs
from planning import services as ps
from platform_core.hubscope import OTHER_HUB_TEXT
from tests.execution_world import raw_incident
from tests.test_v2_03a_transfers import _app_conn, _xworld

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def w() -> Any:
    w = _xworld("rs_")
    fs.register_robot(w.ops, code="RB-1", serial="rs_SN-B1", hub_code="HB")
    ps.register_person(w.ops, username=w.field_b.username, code="P-9", hub_code="HB")
    with _app_conn() as conn:
        raw_incident(conn, w)  # on R-01's run (visit of HUB-1)
        raw_incident(conn, w, robot_id=fs._robot("RB-1").pk)  # RB-1, no run
    w.inc_a = Incident.objects.get(robot__code="R-01").code
    w.inc_b = Incident.objects.get(robot__code="RB-1").code
    return w


def _get(user: Any, path: str) -> tuple[int, str]:
    c = Client()
    c.force_login(user)
    r = c.get(path)
    return r.status_code, r.content.decode()


def _viewers(w: Any) -> dict[str, Any]:
    """hub A field operator (granted at the only hub when created), hub B field operator, regional ops lead."""
    return {"a": w.field, "b": w.field_b, "regional": w.ops}


@pytest.mark.parametrize("path", ["/ops/robots/", "/api/ops/overview"])
def test_robot_status_shows_only_the_viewers_hubs(w, path):
    seen = {k: _get(u, path) for k, u in _viewers(w).items()}
    assert all(code == 200 for code, _ in seen.values())
    assert "R-01" in seen["a"][1] and "RB-1" not in seen["a"][1]
    assert "RB-1" in seen["b"][1] and "R-01" not in seen["b"][1]
    assert "R-01" in seen["regional"][1] and "RB-1" in seen["regional"][1]


@pytest.mark.parametrize("path", ["/ops/incidents/", "/api/ops/incidents", "/execution/incidents/"])
def test_incident_lists_show_only_the_viewers_hubs(w, path):
    seen = {k: _get(u, path)[1] for k, u in _viewers(w).items()}
    assert w.inc_a in seen["a"] and w.inc_b not in seen["a"]
    assert w.inc_b in seen["b"] and w.inc_a not in seen["b"]
    assert w.inc_a in seen["regional"] and w.inc_b in seen["regional"]


def test_another_hubs_incident_detail_is_refused_with_the_plain_sentence(w):
    code, body = _get(w.field_b, f"/execution/incidents/{w.inc_a}/")
    assert code == 403 and OTHER_HUB_TEXT in body
    assert _get(w.field_b, f"/execution/incidents/{w.inc_b}/")[0] == 200
    assert _get(w.field, f"/execution/incidents/{w.inc_b}/")[0] == 403
    assert _get(w.ops, f"/execution/incidents/{w.inc_a}/")[0] == 200
    assert _get(w.ops, f"/execution/incidents/{w.inc_b}/")[0] == 200


@pytest.mark.parametrize("path", ["/ops/exceptions/", "/api/ops/exceptions"])
def test_issues_show_only_the_viewers_hubs(w, path):
    regional = _get(w.ops, path)[1]
    assert "R-01" in regional and "RB-1" in regional
    a, b = _get(w.field, path)[1], _get(w.field_b, path)[1]
    assert "R-01" in a and "RB-1" not in a
    assert "RB-1" in b and "R-01" not in b


@pytest.mark.parametrize("path", ["/ops/timeline/", "/api/ops/timeline"])
def test_the_timeline_shows_only_the_viewers_hubs_runs(w, path):
    assert w.run.code in _get(w.ops, path)[1]
    assert w.run.code in _get(w.field, path)[1]
    assert w.run.code not in _get(w.field_b, path)[1]


@pytest.mark.parametrize("path", ["/ops/closeout/", "/api/ops/closeout"])
def test_the_closeout_shows_only_the_viewers_hubs_runs(w, path):
    assert w.run.code in _get(w.ops, path)[1]
    assert w.run.code in _get(w.field, path)[1]
    code, body = _get(w.field_b, path)
    assert code == 200 and w.run.code not in body


def test_people_are_listed_by_home_hub_and_another_hubs_person_is_refused(w):
    a, b, regional = (_get(u, "/people/")[1] for u in (w.field, w.field_b, w.ops))
    assert "P-1" in a and "P-9" not in a
    assert "P-9" in b and "P-1" not in b
    assert "P-1" in regional and "P-9" in regional
    code, body = _get(w.field_b, "/people/P-1/")
    assert code == 403 and OTHER_HUB_TEXT in body
    assert _get(w.field_b, "/people/P-9/")[0] == 200
    assert _get(w.ops, "/people/P-1/")[0] == 200 and _get(w.ops, "/people/P-9/")[0] == 200


@pytest.mark.parametrize("path", ["/ops/metrics/", "/ops/health/", "/ops/capacity/", "/api/ops/metrics"])
def test_metrics_health_and_capacity_are_regional_only(w, path):
    """Disposition: only regional roles hold operations.metrics, so no hub-scoped user reaches these pages."""
    for user in (w.field, w.field_b, w.tech_a, w.tech_b):
        assert _get(user, path)[0] == 403, (user.username, path)
    assert _get(w.ops, path)[0] == 200

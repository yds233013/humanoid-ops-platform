"""v2 Phase 06 safety-matrix gaps on the Operations briefing (/ops/briefing/), plus one defect regression.

Gaps filled (docs/evidence/06_safety_matrix.md):

1. **The briefing reveals nothing the viewer cannot open on its source pages.** For a field operator granted
   only at hub B, every line of the briefing links to a source record that this viewer can open (HTTP 200) and
   that names the same record; a field operator of the other hub gets 403 on each of those source pages, and
   their own briefing names none of them. (S10 in tests/test_v2_04_briefing.py checks the codes on the
   briefing itself; it never followed the links as the hub-scoped viewer.)
2. **Field operator scoping over time.** A field operator granted at both hubs sees both; once both grants are
   revoked, the briefing is refused (403) and shows nothing, like /ops/.
3. **Defect regression (found by the 06 lifecycle browser test):** a robot OUT on a visit whose session was
   interrupted was listed only as "out on a visit", and "What to prioritise" said "Nothing to prioritise" while
   Today said "A visit session was interrupted and is still stopped. Resolve the interruption". The briefing now
   names the unresolved interruption (Q1, unavailable) and applies priority rule 3, until it is resolved.

SYNTHETIC data only.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.test import Client

from accounts.models import RoleGrant
from accounts.services import revoke_role
from execution import sessions
from fleet import services as fs
from tests import operations_world as ow
from tests.conftest import make_user
from tests.planning_world import DAY
from tests.planning_world import at as local_at
from tests.test_v2_03a_transfers import _hubs
from tests.test_v2_04_briefing import (
    URL,
    _setup,  # noqa: F401 - the briefing tests' autouse set-up (demo mode, the local day is DAY)
    build,
    check_common,
    execution_world,
    get,
    item_text,
    items,
    main,
    priorities,
    question,
    text,
)

pytestmark = pytest.mark.django_db(transaction=True)

ITEM = re.compile(r'<li data-kind="(robot|visit|incident)" data-ref="([^"]+)".*?<!--/item-->', re.S)


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _two_hubs(prefix: str) -> Any:
    """S10's world: HUB-1 (R-01..R-05, w.visit, an incident on R-02) and hub B (robot RB-1, a requested visit,
    an incident on RB-1); w.field_b is a field operator granted only at hub B, w.field only at HUB-1."""
    w = build()
    _hubs(w, prefix)
    fs.register_robot(w.ops, code="RB-1", serial=f"{prefix}SN-B1", hub_code="HB")
    w.vb = str(
        w.p.request_visit(
            w.ops, site_code="S-1", on_site_start=local_at(DAY, 11), on_site_end=local_at(DAY, 12),
            travel_out_min=30, travel_back_min=30, tasks=["T-04"], required_scopes=["presence"], hub_code="HB",
        )["entity_id"]
    )  # fmt: skip
    w.inc_a = ow.report(w, "R-02")
    w.inc_b = ow.report(w, "RB-1")
    return w


def _lines(body: str) -> list[tuple[str, str, str]]:
    """(kind, ref, first link) of every record line on the briefing."""
    out = []
    for m in ITEM.finditer(main(body)):
        href = re.search(r'href="(/[^"#]*)', m.group(0))
        assert href, f"line {m.group(2)} links to no source"
        out.append((m.group(1), m.group(2), href.group(1).replace("&amp;", "&")))
    return out


# ================================================================== 1. nothing the source pages would not show
def test_every_briefing_line_of_a_hub_scoped_viewer_is_openable_on_its_source_page_and_nowhere_else():
    w = _two_hubs("bs1_")
    body = get(w.field_b)
    check_common(body)
    lines = _lines(body)
    refs = {ref for _, ref, _ in lines}
    assert {"RB-1", w.vb, w.inc_b} <= refs, lines
    other_hub = {"R-01", "R-02", "R-03", "R-04", "R-05", w.visit, w.inc_a}
    assert not refs & other_hub, refs & other_hub
    viewer, other = _client(w.field_b), _client(w.field)
    for kind, ref, href in lines:
        r = viewer.get(href)
        assert r.status_code == 200, (kind, ref, href, r.status_code)
        assert ref in r.content.decode(), (kind, ref, href)
        # the field operator of the other hub cannot open it, and their briefing does not name it
        assert other.get(href).status_code == 403, (kind, ref, href)
    theirs = main(get(w.field))
    for ref in refs:
        assert f'data-ref="{ref}"' not in theirs, ref
    assert {ref for _, ref, _ in _lines(get(w.field))} <= other_hub | {"readiness"}


# ================================================================== 2. scoping over time
def test_a_field_operator_at_both_hubs_sees_both_and_after_revocation_sees_nothing():
    w = _two_hubs("bs2_")
    both = make_user("bs2_both")
    for hub in (w.hub_a, w.hub_b):
        RoleGrant.objects.create(user=both, role="field_operator", hub=hub)
    m = main(get(both))
    assert 'data-hub="HUB-1"' in m and 'data-hub="HB"' in m
    assert w.inc_a in m and w.inc_b in m and "RB-1" in m
    narrowed = main(get(both, f"{URL}?hub=HB"))
    assert w.inc_b in narrowed and w.inc_a not in narrowed and 'data-hub="HUB-1"' not in narrowed
    for g in RoleGrant.objects.filter(user=both, revoked_at__isnull=True):
        revoke_role(w.admin, grant_id=g.pk, reason="SYN: left the company")
    c = _client(both)
    for url in (URL, f"{URL}?hub=HB", "/ops/"):
        r = c.get(url)
        assert r.status_code == 403, (url, r.status_code)
        page = r.content.decode()
        for ref in ("RB-1", w.inc_a, w.inc_b, w.visit, w.vb, "bf-hub"):
            assert ref not in page, (url, ref)


# ================================================================== 3. defect regression
def test_an_open_interruption_on_a_robot_out_on_a_visit_is_named_and_prioritised():
    w = execution_world("bs3_")
    robot = w.robot.code
    code = sessions.open_interruption(w.field, run_code=w.run.code, reason="SYN: a child came close")[
        "entity_id"
    ]
    assert "A visit session was interrupted and is still stopped." in text(get(w.ops, "/ops/"))
    for viewer in (w.ops, w.field):  # the regional ops lead and the run's hub field operator
        body = get(viewer)
        check_common(body)
        q1 = question(body, 1)
        assert items(q1.split("data-out=")[0], "robot") == {robot: "unavailable"}, viewer.username
        line = item_text(q1, "robot", robot)
        assert "Unresolved interruption" in line and "must not operate" in line and code in line
        prio = question(body, 4)
        assert ("3", robot) in priorities(prio)
        assert f"Review the unresolved interruption on robot {robot}." in text(prio)
        assert "Nothing to prioritise" not in text(prio)
    sessions.resolve_interruption(w.ops, code=code, note="SYN: area clear")
    body = get(w.ops)
    q1 = question(body, 1)
    assert robot not in items(q1.split("data-out=")[0], "robot")
    out = re.search(r'<details class="fold bf-fold" data-out="1">.*?</details>', q1, re.S)
    assert out and items(out.group(0), "robot") == {robot: "out"}
    assert ("3", robot) not in priorities(question(body, 4))

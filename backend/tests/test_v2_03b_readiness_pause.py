"""v2 Phase 03B-4: the readiness list and the readiness visit page show a regional pause BEFORE anyone asks for
approval to leave (docs/13_v2_phase03_contract.md I-9, 10.G, 13.1).

Proven here:

* Both pages show "Paused: departure authorization unavailable" with the pause's place, reason and local time
  (IANA zone name), read in the visits' own statement (operations.pauses.with_pause).
* The "Request approval to leave" action is not offered while paused (it is offered once the pause is lifted);
  the database gate still refuses anyway.
* No statement is added: each page costs the same as before 03B-4 without a pause, and no more with one
  (measured at 246884f on this world: list 159 statements, visit page 167); the readiness evaluator stays within its
  pinned budget and costs the same paused or not.

SYNTHETIC data only.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from operations.exceptions import READINESS_EVALUATOR_STATEMENTS
from planning.models import Visit
from readiness.evaluator import evaluate_readiness
from readiness.views import PAUSED_LABEL
from tests.fleet_world import set_setting
from tests.planning_world import DAY, TZ
from tests.readiness_world import new_confirmed_visit
from tests.test_v2_03b_pause import _lift, _pause, _zone_world

pytestmark = pytest.mark.django_db(transaction=True)

FAR = str(60 * 24 * 400)  # a readiness look-ahead that covers every visit of the world
AUTHORIZE_ACTION = "Request approval to leave"
# measured at 246884f (before 03B-4) on this world, warm pages, randbelow pinned
BASE_LIST, BASE_VISIT = 159, 167


def _counts(c: Client, code: str) -> tuple[str, int, str, int]:
    c.get("/readiness/")  # warm the session and caches
    c.get(f"/readiness/visits/{code}/")
    with CaptureQueriesContext(connection) as lst:
        listing = c.get("/readiness/")
    with CaptureQueriesContext(connection) as one:
        page = c.get(f"/readiness/visits/{code}/")
    assert listing.status_code == page.status_code == 200
    return listing.content.decode(), len(lst), page.content.decode(), len(one)


def _evaluator(code: str) -> int:
    v = Visit.objects.get(code=code)
    with CaptureQueriesContext(connection) as q:
        evaluate_readiness(v)
    return len(q)


def test_the_list_and_the_visit_page_show_the_pause_up_front_and_hide_the_approval_action(monkeypatch):
    from platform_core import interaction as ia

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)  # no opportunistic token clean-up in the counts
    w = _zone_world("rq_")
    set_setting("ops_readiness_lookahead_min", FAR)
    other = new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=1))  # its home S-1 is in ZB too
    c = Client()
    c.force_login(w.ops)

    # not paused: the approval action is offered, nothing says paused
    listing, n_list, page, n_visit = _counts(c, w.visit)
    e_clear = _evaluator(w.visit)
    assert PAUSED_LABEL not in listing and PAUSED_LABEL not in page
    assert AUTHORIZE_ACTION in page and f'action="/readiness/visits/{w.visit}/authorize/"' in page
    assert (n_list, n_visit) == (BASE_LIST, BASE_VISIT)

    pid = _pause(w, zone="ZB")  # reason "SYN: storm"
    listing, p_list, page, p_visit = _counts(c, w.visit)
    # the list: every visit to ZB says so, with the place, the reason and the local time with its IANA zone
    for code in (w.visit, other):
        row = listing.split(f'href="/readiness/visits/{code}/"><b>{code}</b>')[1].split("</tr>")[0]
        assert PAUSED_LABEL in row and 'data-paused="1"' in row
        assert "zone Hills (ZB) of Main hub (HUB-1)" in row and "SYN: storm" in row
        assert f"({TZ})" in row and "<time datetime=" in row
        assert "ask the ops lead to approve" not in row
    # the visit page: the pause up front, no approval action (the paused state instead)
    status = page.split("<h2>Status (checked now)</h2>")[1].split("</div>\n\n")[0]
    assert (
        PAUSED_LABEL in status and "zone Hills (ZB) of Main hub (HUB-1)" in status and "SYN: storm" in status
    )
    assert f"({TZ})" in status
    assert AUTHORIZE_ACTION not in page and f'action="/readiness/visits/{w.visit}/authorize/"' not in page
    assert (
        'data-paused-no-approval="1"' in page and "Checklist complete - you can approve it now." not in page
    )
    # no statement added: the list costs the same; the visit page one less, as the hidden approval form issues
    # no one-time form token (one INSERT)
    assert (p_list, p_visit) == (n_list, n_visit - 1) and (n_list, n_visit) == (BASE_LIST, BASE_VISIT)
    e_paused = _evaluator(w.visit)
    assert e_paused == e_clear <= READINESS_EVALUATOR_STATEMENTS == 72

    # lifted: the action is back
    _lift(w, pid)
    listing, _, page, _ = _counts(c, w.visit)
    assert PAUSED_LABEL not in listing and PAUSED_LABEL not in page
    assert AUTHORIZE_ACTION in page


def test_a_hub_pause_is_shown_and_a_departed_visit_is_not_marked_paused():
    from readiness import commands as rc

    w = _zone_world("rh_")
    code = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    pid = _pause(w)  # the whole hub, after the authorization
    c = Client()
    c.force_login(w.ops)
    page = c.get(f"/readiness/visits/{w.visit}/").content.decode()
    assert PAUSED_LABEL in page and "Departures for Main hub (HUB-1) are paused" in page
    assert 'data-paused-no-departure="1"' in page and "Record departure</button>" not in page
    _lift(w, pid)
    rc.record_departure(w.ops, authorization_code=code)
    _pause(w)
    page = c.get(f"/readiness/visits/{w.visit}/").content.decode()
    assert PAUSED_LABEL not in page and 'data-departed="1"' in page  # a pause never touches a departure

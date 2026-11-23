"""01D release fix: /readiness/ evaluates only visits inside the readiness look-ahead, at most READINESS_CAP of
them (the same window and cap as Today), and still lists every confirmed future visit."""

from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from operations.readmodels import READINESS_CAP
from readiness.views import NOT_ASSESSED_CAP, NOT_ASSESSED_OUTSIDE
from tests.fleet_world import set_setting
from tests.planning_world import DAY
from tests.readiness_world import build_readiness, new_confirmed_visit

pytestmark = pytest.mark.django_db(transaction=True)
FAR = str(60 * 24 * 400)  # a look-ahead that covers every visit of the world


@pytest.fixture
def w():
    return build_readiness(prefix="rl_")  # one confirmed visit (w.visit) with complete evidence


def add_visits(w: Any, n: int, start: int = 1) -> list[str]:
    return [new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=start + k)) for k in range(n)]


def page(w: Any) -> tuple[str, int]:
    c = Client()
    c.force_login(w.ops)
    c.get("/readiness/")  # warm the session and caches
    with CaptureQueriesContext(connection) as q:
        r = c.get("/readiness/")
    assert r.status_code == 200
    return r.content.decode(), len(q.captured_queries)


def test_statement_count_is_constant_beyond_the_cap(w):
    set_setting("ops_readiness_lookahead_min", FAR)
    add_visits(w, READINESS_CAP + 1)  # 12 visits: 10 assessed, 2 over the cap
    small_html, small = page(w)
    add_visits(w, 25, start=READINESS_CAP + 5)  # 37 visits
    big_html, big = page(w)
    assert big == small, (small, big)
    assert 'data-readiness-counts="12/10/2"' in small_html
    assert 'data-readiness-counts="37/10/27"' in big_html


def test_counts_labels_and_no_ready_state_for_unassessed_visits(w):
    codes = add_visits(w, READINESS_CAP + 2)
    set_setting("ops_readiness_lookahead_min", FAR)
    html, _ = page(w)
    total = len(codes) + 1
    assert (
        f"<b>{total} visits</b> · {READINESS_CAP} assessed · {total - READINESS_CAP} not assessed yet" in html
    )
    assert html.count('data-not-assessed="1"') == total - READINESS_CAP
    assert NOT_ASSESSED_CAP in html
    last = codes[-1]
    row = html.split(f'href="/readiness/visits/{last}/"><b>{last}</b>')[1].split("</tr>")[0]
    assert "Not assessed yet" in row and "Checklist complete" not in row and "Approved" not in row
    assert f'href="/readiness/visits/{last}/">Check this visit now</a>' in row
    # outside the look-ahead nothing is assessed, every visit is still listed
    set_setting("ops_readiness_lookahead_min", "1")
    html, _ = page(w)
    assert f'data-readiness-counts="{total}/0/{total}"' in html and NOT_ASSESSED_OUTSIDE in html
    assert "Checklist complete" not in html and "Approved to leave" not in html
    # the visit's own page still runs the full evaluator
    c = Client()
    c.force_login(w.ops)
    assert (
        "Checklist complete - you can approve it now."
        in c.get(f"/readiness/visits/{w.visit}/").content.decode()
    )

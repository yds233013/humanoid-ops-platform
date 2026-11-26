"""Phase 05/06 product debt (docs/13_v2_phase03_contract.md 15.11, the two items listed after 15.10):

1. Today's issue list: the hub scope is applied BEFORE the limit, so a hub viewer's list is never short or empty
   while more of their own issues exist (it used to take the first INBOX_LIMIT issues of every hub and then drop
   the other hubs' ones).
2. Plan form resource labels lead with names: "Jordan Lee (P-1) · free", "Robot R-02 · free", "Vehicle VAN-2 ·
   free" (design spec 14 principle 2, the review's own "name (code)" wording), never "P-1 · Jordan Lee".

SYNTHETIC data only.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from accounts.permissions import visible_hub_ids
from fleet import services as fs
from operations import today as td
from operations.exceptions import open_exceptions
from operations.scope import keep_issues
from planning import options as opt
from planning.models import Visit
from tests.execution_world import raw_incident
from tests.planning_world import build_planning, new_visit, plan
from tests.test_v2_03a_transfers import _app_conn, _xworld


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _issue_tile(html: str) -> str:
    m = re.search(r'data-count="issues"><b class="n">([^<]+)</b>', html)
    assert m, "issues tile missing"
    return m.group(1)


# ------------------------------------------------------------------ 1. Today: scope before the limit
@pytest.mark.django_db(transaction=True)
def test_today_issue_list_scopes_to_the_hub_before_the_limit(monkeypatch):
    w = _xworld("pd_")
    with _app_conn() as conn:
        raw_incident(conn, w)  # an issue at HUB-1 (R-01's run)
        for n in range(1, 5):  # four at HB, robots with no run
            fs.register_robot(w.ops, code=f"RB-{n}", serial=f"pd_SN-B{n}", hub_code="HB")
            raw_incident(conn, w, robot_id=fs._robot(f"RB-{n}").pk)
        raw_incident(conn, w, robot_id=fs._robot("R-02").pk)  # one more at HUB-1, reported after HB's
    hub_a = visible_hub_ids(w.field)
    assert hub_a is not None and len(hub_a) == 1
    everything = open_exceptions(timezone.now(), include_readiness=False).items
    mine = keep_issues(everything, hub_a)
    assert any(x.robot == "R-01" for x in mine) and not any((x.robot or "").startswith("RB-") for x in mine)
    # the limit that ends on the first HB issue in the total order: filtering AFTER that limit (the old code)
    # showed limit - 1 of hub A's issues although hub A has more, i.e. a short page
    first_b = next(i for i, x in enumerate(everything) if (x.robot or "").startswith("RB-"))
    limit = first_b + 1
    assert len(keep_issues(everything[:limit], hub_a)) == limit - 1 < limit <= len(mine), (limit, len(mine))
    monkeypatch.setattr(td, "INBOX_LIMIT", limit)

    html = _client(w.field).get("/ops/").content.decode()
    # a full page of hub A's issues; "+" exactly when hub A has more than the limit
    assert _issue_tile(html) == (f"{limit}+" if len(mine) > limit else str(limit))
    assert "RB-" not in html
    # a limit that covers all of hub A's issues: every one, and the count is exact
    monkeypatch.setattr(td, "INBOX_LIMIT", len(mine))
    assert _issue_tile(_client(w.field).get("/ops/").content.decode()) == str(len(mine))
    # the regional viewer is unchanged: the first INBOX_LIMIT of every hub's issues
    monkeypatch.setattr(td, "INBOX_LIMIT", 200)
    regional = _client(w.ops).get("/ops/").content.decode()
    assert _issue_tile(regional) == str(len(everything))


# ------------------------------------------------------------------ 2. plan form: names first
@pytest.mark.django_db
def test_plan_form_resource_labels_lead_with_names():
    w = build_planning(robots=3)
    w.ops.first_name, w.ops.last_name = "Ravi", "Menon"
    w.ops.save()
    w.field2.first_name, w.field2.last_name = "Amara", "Okafor"
    w.field2.save()
    v1 = new_visit(w)
    assert plan(w, v1)["feasibility"] == "feasible"
    v2 = new_visit(w, h0=11, h1=12)
    opts = opt.plan_options(Visit.objects.get(code=v2))
    leads = {o.code: o.label for o in opts.leads}
    assert leads["P-3"] == "Amara Okafor (P-3) · free"
    assert leads["P-1"].startswith(f"Ravi Menon (P-1) · busy {v1} ")
    assert leads["P-2"] == f"{w.field.username} (P-2) · not qualified as lead"  # no name: the username
    assert {o.label for o in opts.robots} >= {"Robot R-02 · free"}
    assert {o.label for o in opts.vehicles} >= {
        "Vehicle VAN-2 · free",
        "Vehicle VAN-X · capacity not recorded",
    }
    for o in opts.leads + opts.seconds:
        assert not o.label.startswith("P-"), o.label  # never code first
    # the page renders those labels, the codes stay the values
    html = _client(w.ops).get(f"/planning/visits/{v2}/").content.decode()
    assert '<option value="P-3" data-status="free">Amara Okafor (P-3) · free</option>' in html
    assert re.search(r'<option value="R-02"[^>]*>Robot R-02 · free</option>', html)
    assert re.search(r'<option value="VAN-2"[^>]*>Vehicle VAN-2 · free</option>', html)
    # the suggestion names people the same way
    s = opt.suggest(Visit.objects.get(code=v2), opts)
    if s.lead is not None:
        assert any(
            line.startswith(f"{opt.person_text(s.lead.obj)} is qualified as lead") for line in s.explanations
        )
        assert not any(re.match(r"P-\d+ · ", line) for line in s.explanations)

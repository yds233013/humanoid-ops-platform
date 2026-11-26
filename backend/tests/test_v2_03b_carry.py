"""v2 03B closure carry-forward (docs/13_v2_phase03_contract.md 14.7).

Proven here:

* P1: every suggested next free slot carries the exact label, and lists the checks still outstanding for that
  slot (charging, home and site requirements, an active regional pause; the robot's return when At risk)
  BEFORE its save button. Saving still posts to the one guarded plan command.

SYNTHETIC data only.
"""

from __future__ import annotations

import re

import pytest

from hub import charging
from operations import pauses
from planning import next_free as nf
from planning import services as ps
from planning.models import Visit
from tests.fleet_world import set_setting
from tests.planning_world import DAY, at
from tests.test_v2_03b_next_free import NEXT, _client, _no_vehicle_fits, _slot_form, _world

pytestmark = pytest.mark.django_db(transaction=True)

LABEL = (
    "Suggested planning time. Final checks, including charging, site requirements and regional pauses, run "
    "when you save. This is not departure authorization."
)


def _checks(slot: nf.Slot) -> dict[str, tuple[str, str]]:
    return {c.key: (c.result, c.text) for c in slot.outstanding}


# ================================================================== P1: label and outstanding checks
def test_every_slot_has_the_exact_label_and_its_outstanding_checks():
    w = _world("cl_")
    got = nf.next_free(w.target, now=at(DAY, 5))
    assert len(got.slots) == 3
    assert nf.SLOT_LABEL == LABEL
    for s in got.slots:
        assert s.label_text == LABEL
        checks = _checks(s)
        assert list(checks) == ["charge", "home", "pause"], checks  # Available: no return check
        # charging: the forecast is not provisioned in this world, so it says so (never "enough")
        assert checks["charge"][0] == "risk" and "ops_soc_fresh_min" in checks["charge"][1]
        assert f"Robot {s.robot}" in checks["charge"][1]
        # the home is assessed and consented for this date (evaluate's own facts)
        assert checks["home"][0] == "pass"
        # no pause now: said, with what still stops departure
        assert checks["pause"] == (
            "pass",
            f"No departure pause at {w.hub.name} now. A pause set later still stops departure.",
        )
        assert s.depart is not None and s.depart < s.start


def test_an_active_pause_and_a_charge_forecast_are_listed_per_slot():
    w = _world("cp_")
    pauses.pause_region(w.ops, hub_code=w.hub.code, zone_code=None, reason="SYN: storm")
    set_setting("ops_soc_fresh_min", "60")
    got = nf.next_free(w.target, now=at(DAY, 5))
    assert got.slots
    for s in got.slots:
        checks = _checks(s)
        result, text = checks["pause"]
        assert result == "risk" and "are paused" in text and "SYN: storm" in text
        assert "none can be authorized to leave" in text
        # the charge line follows the 02B forecast for this robot at this slot's departure
        f = charging.robot_forecasts([s.robot_id], s.depart).get(s.robot_id)
        expect = {charging.ENOUGH: "pass", charging.NEEDS: "risk", charging.UNKNOWN: "risk"}[
            f.state if f is not None else charging.UNKNOWN
        ]
        if f is not None and f.cannot_in_time:
            expect = "fail"
        assert checks["charge"][0] == expect, (checks["charge"], f)
        assert "ops_soc_fresh_min" not in checks["charge"][1]


def test_a_home_requirement_on_the_slot_date_is_listed():
    """A booking that needs consent for a scope nobody gave: every slot lists it under home and site
    requirements (evaluate's CONSENT fact), and the slot is still offered (saving checks it again)."""
    w = _world("ch_")
    req = nf.Request(site=w.target.site, hub=w.hub, minutes=60, tasks=["T-04"], scopes=["presence", "camera"])
    got = nf.next_free(req, now=at(DAY, 5))
    assert got.slots
    for s in got.slots:
        result, text = _checks(s)["home"]
        assert result in ("risk", "fail") and "onsent" in text, text


def _slot_li(html: str, n: int) -> str:
    m = re.search(rf'<li class="slot" data-slot="{n}".*?</li>\s*(?=<li class="slot"|</ol>)', html, re.S)
    assert m, f"no slot {n}"
    return m.group(0)


def test_review_and_book_show_the_label_and_checks_before_the_save_button(monkeypatch):
    w = _no_vehicle_fits("cr_")
    monkeypatch.setattr(nf, "_now", lambda: at(DAY, 5))
    c = _client(w.ops)
    review = c.get(f"/planning/visits/{w.target.code}/?review=1").content.decode()
    base = {"site": "S-1", "day": DAY.isoformat(), "start": "08:00", "end": "09:00", "tasks": "T-04",
            "scopes": "presence"}  # fmt: skip
    book = c.post("/planning/book/", {**base, "find": "1"}).content.decode()
    for html, button in ((review, "Use this time"), (book, "Book this time")):
        assert html.count(LABEL) == 3
        for n in (1, 2, 3):
            li = _slot_li(html, n)
            label, checks, save = li.index(LABEL), li.index(f'data-slot-checks="{n}"'), li.index(button)
            assert label < checks < save  # listed BEFORE the user can save it
            for key in ("charge", "home", "pause"):
                assert f'data-check="{key}"' in li
            assert "Still to check for this time" in li
    # saving still goes through the guarded plan command only, with the slot's own resources and time
    action, fields = _slot_form(review)
    assert action == f"/planning/visits/{w.target.code}/plan/"
    assert set(fields) >= {"robot", "lead", "second", "vehicle", "start", "end", "reason"}
    assert fields["start"] == f"{NEXT.isoformat()}T08:00"


# ================================================================== P2: plan options are the visit's hub's only
def _two_hubs(prefix: str):
    """The planning world (HUB-1: R-01..R-03, P-1..P-3, VAN-1, VAN-2, VAN-X) plus hub HB with R-03 received
    there by transfer, person P-9 (fully qualified, home hub HB) and vehicle VAN-9 (complete, at HB)."""
    from datetime import UTC, datetime, timedelta

    from django.utils import timezone

    from fleet import transfers as tr
    from tests.planning_world import build_planning
    from tests.test_v2_03a_transfers import _hubs

    w = _hubs(build_planning(robots=3, prefix=prefix), prefix)
    tid = int(tr.start_transfer(w.tech_a, kind="robot", code="R-03", to_hub_code="HB", reason="SYN")["id"])
    tr.receive_transfer(w.tech_b, transfer_id=tid)
    ps.register_person(w.ops, username=w.field_b.username, code="P-9", hub_code="HB")
    lo, hi = timezone.now() - timedelta(days=30), datetime(2027, 6, 1, tzinfo=UTC)
    for k in ("field_lead", "field_operator", "robot_handler"):
        ps.grant_qualification(w.ops, person_code="P-9", kind=k, valid_from=lo, valid_to=hi)
    ps.register_vehicle(w.ops, code="VAN-9", robot_capacity=1, restraint_kit="SYN kit", hub_code="HB")
    return w


def _visit_at(w, hub: str = "HUB-1", h0: int = 10) -> Visit:
    return Visit.objects.get(
        code=ps.request_visit(
            w.ops, site_code="S-1", on_site_start=at(NEXT, h0), on_site_end=at(NEXT, h0 + 1), travel_out_min=30,
            travel_back_min=30, tasks=["T-04"], required_scopes=["presence"], hub_code=hub,
        )["entity_id"]
    )  # fmt: skip


def test_plan_options_offer_only_the_visit_hub_and_count_the_rest():
    from planning import options as popt

    w = _two_hubs("ph_")
    v = _visit_at(w)
    full = popt.plan_options(v)
    # the planner's labels still know every option (other tests rely on them), with "at another hub"
    assert "P-9" in {o.code for o in full.leads} and "VAN-9" in {o.code for o in full.vehicles}
    assert next(o for o in full.leads if o.code == "P-9").status == popt.FREE  # free, but another hub's
    mine = popt.restrict_to_hub(full, v.hub_id)
    assert {o.code for o in mine.robots} == {"R-01", "R-02"}
    assert {o.code for o in mine.leads} == {o.code for o in mine.seconds} == {"P-1", "P-2", "P-3"}
    assert {o.code for o in mine.vehicles} == {"VAN-1", "VAN-2", "VAN-X"}
    assert mine.elsewhere == {"robots": 1, "leads": 1, "seconds": 1, "vehicles": 1}
    assert mine.elsewhere_text == "Not offered: 1 robot, 1 person and 1 vehicle at other hubs."
    assert popt.elsewhere_line("leads", 12) == "12 people at other hubs are not offered"
    # and the other way round: HB's visit is offered HB's only
    hb = popt.restrict_to_hub(popt.plan_options(_visit_at(w, hub="HB")), w.hub_b.pk)
    assert [o.code for o in hb.robots] == ["R-03"] and [o.code for o in hb.leads] == ["P-9"]
    assert [o.code for o in hb.vehicles] == ["VAN-9"]
    assert hb.elsewhere == {"robots": 2, "leads": 3, "seconds": 3, "vehicles": 3}


def test_the_plan_form_and_review_list_no_other_hub_choice(monkeypatch):
    w = _two_hubs("pf_")
    v = _visit_at(w)
    c = _client(w.ops)
    page = c.get(f"/planning/visits/{v.code}/").content.decode()
    form = page[page.index('id="plan"') : page.index("</form>", page.index('id="plan"'))]
    for code in ("R-03", "P-9", "VAN-9"):
        assert f'value="{code}"' not in form, code
    for code in ("R-01", "R-02", "P-1", "P-2", "P-3", "VAN-1", "VAN-2"):
        assert f'value="{code}"' in form, code
    assert "Not offered: 1 robot, 1 person and 1 vehicle at other hubs." in page
    review = c.get(f"/planning/visits/{v.code}/?review=1").content.decode()
    panel = review[review.index('id="review"') :]
    for code in ("R-03", "P-9", "VAN-9"):
        assert f'value="{code}"' not in panel and f"{code} " not in panel, code
    assert "1 person at another hub is not offered." in panel
    assert "1 robot at another hub is not offered." in panel
    # a forged choice of another hub's person is ignored by the advisory preview (not one of its options)
    forged = c.get(f"/planning/visits/{v.code}/?review=1&lead=P-9").content.decode()
    assert "P-9" not in forged[forged.index('id="review"') :]


@pytest.mark.parametrize("field,code", [("robot", "R-03"), ("vehicle", "VAN-9")])
def test_a_forged_cross_hub_robot_or_vehicle_is_still_refused_by_the_plan_command(field, code, monkeypatch):
    """The form no longer offers them; a forged POST still goes to the guarded plan command, which refuses it
    (database trigger planning_visit_hub_guard), and nothing changes."""
    w = _two_hubs("pr_")
    v = _visit_at(w)
    before = Visit.objects.get(pk=v.pk).current_revision_id
    fields = {"robot": "R-01", "lead": "P-1", "second": "P-2", "vehicle": "VAN-1", field: code,
              "reason": "SYN forged"}  # fmt: skip
    c = _client(w.ops)
    page = c.get(f"/planning/visits/{v.code}/").content.decode()
    token = re.search(r'name="_form_token" value="([^"]+)"', page)
    if token:
        fields["_form_token"] = token.group(1)
    calls: list[dict] = []
    real = ps.plan_visit
    monkeypatch.setattr(ps, "plan_visit", lambda *a, **k: calls.append(k) or real(*a, **k))
    r = c.post(f"/planning/visits/{v.code}/plan/", fields, follow=True)
    assert r.status_code == 200 and len(calls) == 1  # it reached the ONE guarded plan command
    assert re.search(rf"{code}[^<]*another hub", r.content.decode()), "the refusal names the hub rule"
    assert Visit.objects.get(pk=v.pk).current_revision_id == before  # nothing changed

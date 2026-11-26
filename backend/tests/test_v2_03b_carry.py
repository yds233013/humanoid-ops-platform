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

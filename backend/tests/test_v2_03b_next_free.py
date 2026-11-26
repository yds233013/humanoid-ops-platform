"""v2 03B-3: next free time (S3; docs/13_v2_phase03_contract.md 13.4 item 4, 13.5).

Proven here:

* Oracle: on a small world, ``next_free`` returns exactly what a brute force gives that asks
  ``planning.services.evaluate`` (the guarded plan command's own check) about EVERY candidate time and EVERY
  robot, lead, second and vehicle combination at the hub, then ranks Available for planning before At risk,
  earliest first, then robot, crew and vehicle codes.
* A missing travel pair is never offered: no slot, and the search says "Travel time unknown".
* A robot away on a same-hub run gives "At risk" slots, ranked after every Available slot.
* Candidates are 30-minute steps inside the hub's operating hours, in the hub's IANA zone; daylight-saving
  days skip the wall times that do not exist or exist twice, and every slot ends by closing time.
* The 200-evaluation budget: when it runs out the result says so.
* Missing owner settings: the search does not run and says which setting is missing.
* Read only: no INSERT, UPDATE or DELETE.

SYNTHETIC data only.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pytest
from django.db import connection

from fleet import services as fs
from fleet.models import DeploymentSetting, Robot
from planning import next_free as nf
from planning import services as ps
from planning.models import Person, Vehicle, Visit
from platform_core.models import Hub
from tests.fleet_world import set_setting
from tests.planning_world import DAY, TZ, at, new_visit, plan
from tests.test_v2_03b_zones_travel import _zoned

pytestmark = pytest.mark.django_db(transaction=True)
NEXT = DAY + timedelta(days=1)
WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.I)


def _settings(hub: Hub, hours: str = "08:00-11:00", days: int = 2) -> None:
    set_setting("dispatch_lookahead_days", str(days))
    DeploymentSetting.objects.update_or_create(key="hub_operating_hours", hub=hub, defaults={"value": hours})


def _world(prefix: str, *, pair: bool = True):
    """Hub HUB-1 (zones ZA = the hub's own zone, ZB = home S-1), travel ZA->ZB 25 and ZB->ZA 35 min; visit A
    on DAY 08:00-09:00 takes R-01, P-1 and P-2 and VAN-1 (their windows run past 11:00 the same day); the
    target is a REQUESTED visit at S-1, 60 minutes on site."""
    w = _zoned(prefix, pair=pair)
    _settings(w.hub)
    if pair:
        w.a = new_visit(w, h0=8, h1=9, travel=None)
        plan(w, w.a)
    w.target = Visit.objects.get(code=new_visit(w, h0=10, h1=11, travel=None))
    return w


def _brute_force(visit: Visit, now: datetime, hours: tuple[dtime, dtime], days: int) -> list[tuple]:
    """Every candidate time x every robot, lead, second and vehicle of the hub, asked of evaluate."""
    rev = visit.current_revision
    minutes = int((rev.on_site_end - rev.on_site_start).total_seconds() // 60)
    hub = Hub.objects.get(pk=visit.hub_id)
    starts = nf.candidate_starts(hub.time_zone, hours, days, minutes, now)
    robots = list(Robot.objects.filter(hub=hub).exclude(lifecycle="retired").order_by("code"))
    people = list(Person.objects.filter(hub=hub).order_by("code"))
    vehicles = list(Vehicle.objects.filter(hub=hub).order_by("code"))
    best: list[tuple] = []
    for start in starts:
        end = start + timedelta(minutes=minutes)
        valid = []
        for r in robots:
            for lead in people:
                for second in people:
                    if second.pk == lead.pk:
                        continue
                    for v in vehicles:
                        plan_ = ps.evaluate(
                            site=visit.site, start=start, end=end, travel_out=rev.travel_out_min,
                            travel_back=rev.travel_back_min, tasks=rev.tasks, scopes=rev.required_consent_scopes,
                            robot=r, lead=lead, second=second, vehicle=v, visit=visit,
                            travel=ps.new_route(visit, start, end), pause_check=False,
                        )  # fmt: skip
                        blocked = [b for b in plan_.blockers if b["level"] == "blocked"]
                        if any(b["code"] not in nf.HOME_CODES for b in blocked):
                            continue
                        if datetime.fromisoformat(plan_.snapshot["timeline_utc"]["inspection"]) <= now:
                            continue
                        risky = any(b["level"] == "at_risk" for b in plan_.blockers)
                        valid.append((1 if risky else 0, start, r.code, lead.code, second.code, v.code))
        if valid:
            best.append(min(valid))
    return sorted(best)[:3]


def _key(s: nf.Slot) -> tuple:
    return (0 if s.status == nf.FREE else 1, s.start, s.robot, s.lead.code, s.second.code, s.vehicle)


# ================================================================== the oracle
def test_next_free_equals_the_brute_force_oracle():
    w = _world("nf_")
    now = at(DAY, 5)
    got = nf.next_free(w.target, now=now)
    expect = _brute_force(w.target, now, (dtime(8), dtime(11)), 2)
    assert [_key(s) for s in got.slots] == expect, (got.slots, expect)
    # DAY has no free crew pair inside the hours (visit A keeps P-1 and P-2 until after 11:00): the first slots
    # are the next day, from opening time, with the codes in order
    assert [s.start for s in got.slots] == [at(NEXT, 8), at(NEXT, 8, 30), at(NEXT, 9)]
    s0 = got.slots[0]
    assert (s0.robot, s0.lead.code, s0.second.code, s0.vehicle, s0.status) == (
        "R-01", "P-1", "P-2", "VAN-1", nf.FREE
    )  # fmt: skip
    assert s0.words == "Available for planning" and s0.shape == "●"
    assert not got.exhausted and got.evaluations <= nf.BUDGET
    # the one line of why: names, codes, the travel matrix values (demo values), never a departure word
    assert (
        s0.why
        == f"Robot at {w.hub.name}; crew and vehicle free; travel 25 min out, 35 min back (demo values)"
    )
    assert s0.span_label.startswith("Wed 9 Dec, 08:00–09:00 ")
    assert not re.search(r"(?i)clear|approv|depart", s0.why)
    # the label is in the hub's zone with the IANA abbreviation (no hard-coded zone)
    assert s0.label == "Wed 9 Dec, 08:00 " + at(NEXT, 8).astimezone(ZoneInfo(TZ)).strftime("%Z")
    assert s0.form_value("start") == f"{NEXT.isoformat()}T08:00"
    # the travel versions the suggestion used are named (13.3: a suggestion names its matrix version)
    assert all(s0.travel_versions)


def test_a_missing_travel_pair_is_never_offered():
    w = _world("nfu_", pair=False)
    got = nf.next_free(w.target, now=at(DAY, 5))
    assert got.slots == []
    assert got.stopped.startswith("Travel time unknown"), got.stopped
    assert got.evaluations == 1  # stopped at once: no robot, crew or vehicle can fix it


def test_settings_missing_says_which():
    w = _world("nfs_")
    DeploymentSetting.objects.filter(key="hub_operating_hours").delete()
    got = nf.next_free(w.target, now=at(DAY, 5))
    assert got.slots == [] and "hub_operating_hours" in got.not_set and w.hub.name in got.not_set
    DeploymentSetting.objects.filter(key="dispatch_lookahead_days").delete()
    assert "dispatch_lookahead_days" in nf.next_free(w.target, now=at(DAY, 5)).not_set


def test_budget_reached_is_reported():
    w = _world("nfb_")
    got = nf.next_free(w.target, now=at(DAY, 5), budget=3)
    assert got.exhausted and got.evaluations == 3
    full = nf.next_free(w.target, now=at(DAY, 5))
    assert not full.exhausted and len(full.slots) == 3


def test_read_only():
    w = _world("nfr_")
    seen: list[str] = []

    def spy(execute, sql, params, many, context):
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(spy):
        nf.next_free(w.target, now=at(DAY, 5))
    assert seen and not [s for s in seen if WRITE.match(s)]


def test_at_risk_slots_rank_after_available_ones():
    """R-01 away on an open run of the hub (section 12): its slots are At risk; with R-02 and R-03 held there
    are only At risk slots, labelled so; without the holds the Available ones come first."""
    from tests.execution_world import to_site
    from tests.test_v2_03a_transfers import _xworld

    w = _xworld("nfa_")
    hub = Hub.objects.get(code="HUB-1")
    _settings(hub, hours="08:00-12:00", days=1)
    with connection.cursor() as cur:
        to_site(cur, w)
    target = Visit.objects.get(
        code=ps.request_visit(
            w.ops, site_code="S-1", on_site_start=at(NEXT, 10), on_site_end=at(NEXT, 11), travel_out_min=30,
            travel_back_min=30, tasks=["T-04"], required_scopes=["presence"], hub_code="HUB-1",
        )["entity_id"]
    )  # fmt: skip
    now = at(NEXT, 5)
    for code in ("R-02", "R-03"):
        fs.place_hold(w.ops, scope="robot", target=code, source_type="manual", reason="SYNTHETIC hold")
    risky = nf.next_free(target, now=now)
    assert risky.slots and {s.status for s in risky.slots} == {nf.AT_RISK}
    assert {s.robot for s in risky.slots} == {"R-01"} and risky.slots[0].words == "At risk"
    assert risky.slots[0].why.startswith("Robot away until verified back at the hub; crew and vehicle free")
    assert [_key(s) for s in risky.slots] == _brute_force(target, now, (dtime(8), dtime(12)), 1)


# ================================================================== candidate times
@pytest.mark.parametrize(
    "day,label",
    [(date(2027, 3, 14), "spring forward"), (date(2026, 11, 1), "fall back"), (date(2026, 12, 8), "plain")],
)
def test_candidates_follow_the_hub_zone_across_daylight_saving(day: date, label: str):
    zone = ZoneInfo(TZ)
    now = datetime.combine(day, dtime(0, 0), tzinfo=zone)  # midnight: the search starts on ``day``
    starts = nf.candidate_starts(TZ, (dtime(0, 30), dtime(4, 0)), 1, 60, now)
    walls = [s.astimezone(zone) for s in starts]
    assert all(w.date() == day for w in walls)
    assert all(dtime(0, 30) <= w.time() and (w + timedelta(minutes=60)).time() <= dtime(4, 0) for w in walls)
    assert all(w.minute in (0, 30) for w in walls)
    assert len(set(starts)) == len(starts)
    texts = [w.strftime("%H:%M") for w in walls]
    if label == "spring forward":  # 02:00-02:59 does not exist
        assert "02:00" not in texts and "02:30" not in texts and "03:00" in texts
    elif label == "fall back":  # 01:00-01:59 exists twice: skipped, never guessed
        assert "01:00" not in texts and "01:30" not in texts and "02:00" in texts
    else:
        assert texts == ["00:30", "01:00", "01:30", "02:00", "02:30", "03:00"]


# ================================================================== the screens: S2 "No option works" and Book a visit
def _client(user):
    from django.test import Client

    c = Client()
    c.force_login(user)
    return c


def _slot_form(html: str, n: int = 1) -> tuple[str, dict[str, str]]:
    m = re.search(rf'<form method="post" action="([^"]+)" data-slot-form="{n}">(.*?)</form>', html, re.S)
    assert m, "no slot form"
    return m.group(1), dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', m.group(2)))


def _no_vehicle_fits(prefix: str):
    """The target overlaps visit A (VAN-1) and VAN-2 has no restraint kit recorded (owner fixture write): no
    vehicle fits the target's time, so the review panel offers the next free times."""
    w = _world(prefix)
    Vehicle.objects.filter(code="VAN-2").update(restraint_kit="")
    w.target = Visit.objects.get(code=new_visit(w, h0=8, h1=9, travel=None))
    return w


def test_review_offers_next_free_times_when_nothing_fits_and_choosing_one_saves_through_the_plan_command(
    monkeypatch,
):
    w = _no_vehicle_fits("nfv_")
    monkeypatch.setattr(nf, "_now", lambda: at(DAY, 5))
    html = _client(w.ops).get(f"/planning/visits/{w.target.code}/?review=1").content.decode()
    assert "No free robot, crew and vehicle for this time." in html
    assert 'data-next-free="3"' in html and "Next free times at " in html
    assert "not cleared to depart" in html
    action, fields = _slot_form(html)
    assert action == f"/planning/visits/{w.target.code}/plan/"
    assert (fields["robot"], fields["lead"], fields["second"], fields["vehicle"]) == (
        "R-01",
        "P-1",
        "P-2",
        "VAN-1",
    )
    assert fields["start"] == f"{NEXT.isoformat()}T08:00" and fields["end"] == f"{NEXT.isoformat()}T09:00"
    calls = []
    real = ps.plan_visit
    monkeypatch.setattr(ps, "plan_visit", lambda *a, **k: calls.append(k) or real(*a, **k))
    r = _client(w.ops).post(action, fields)
    assert r.status_code == 302 and len(calls) == 1  # ONE guarded plan command
    rev = Visit.objects.get(code=w.target.code).current_revision
    assert (rev.on_site_start, rev.robot.code, rev.vehicle.code) == (at(NEXT, 8), "R-01", "VAN-1")
    assert rev.feasibility == "feasible"


def test_book_a_visit_finds_free_times_and_books_one(monkeypatch):
    w = _no_vehicle_fits("nfk_")
    monkeypatch.setattr(nf, "_now", lambda: at(DAY, 5))
    c = _client(w.ops)
    assert c.get("/planning/book/?site=S-1").status_code == 200
    base = {"site": "S-1", "day": DAY.isoformat(), "start": "08:00", "end": "09:00", "tasks": "T-04",
            "scopes": "presence"}  # fmt: skip
    html = c.post("/planning/book/", {**base, "find": "1"}).content.decode()
    assert 'data-next-free="3"' in html and "Book this time" in html and "not cleared to depart" in html
    assert Visit.objects.count() == 3  # finding saves nothing
    action, fields = _slot_form(html)
    assert fields["day"] == NEXT.isoformat() and fields["start"] == "08:00" and fields["slot_robot"] == "R-01"
    r = c.post(action, {**fields, "scopes": "presence"})
    assert r.status_code == 302, r.content[:400]
    new = Visit.objects.order_by("-pk").first()
    assert new.status == "planned" and new.current_revision.on_site_start == at(NEXT, 8)
    assert new.current_revision.robot.code == "R-01"

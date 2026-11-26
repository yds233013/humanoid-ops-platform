"""v2 03B closure carry-forward (docs/13_v2_phase03_contract.md 14.7).

Proven here:

* P1: every suggested next free slot carries the exact label, and lists the checks still outstanding for that
  slot (charging, home and site requirements, an active regional pause; the robot's return when At risk)
  BEFORE its save button. Saving still posts to the one guarded plan command.

SYNTHETIC data only.
"""

from __future__ import annotations

import re
from html import unescape

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


# ================================================================== P2: Issues scoped BEFORE the page limit
@pytest.fixture
def rs():
    """The 03A read-scope world: HUB-1 (R-01 departed, an incident on its run) and HB (RB-1 with an incident,
    P-9), plus a second HUB-1 incident on R-02 so HUB-1 issues sort before and between HB's."""
    from execution.models import Incident
    from fleet import services as fs
    from tests.execution_world import raw_incident
    from tests.test_v2_03a_transfers import _app_conn, _xworld

    w = _xworld("ci_")  # as tests/test_v2_03a_read_scope.py's world
    fs.register_robot(w.ops, code="RB-1", serial="ci_SN-B1", hub_code="HB")
    ps.register_person(w.ops, username=w.field_b.username, code="P-9", hub_code="HB")
    with _app_conn() as conn:
        raw_incident(conn, w)  # on R-01's run (visit of HUB-1)
        raw_incident(conn, w, robot_id=fs._robot("RB-1").pk)  # RB-1, no run
        raw_incident(conn, w, robot_id=fs._robot("R-02").pk)
    assert Incident.objects.count() == 3
    return w


def _api_pages(user, monkeypatch, size: int) -> list[list[tuple[str, str]]]:
    import json

    from operations import views as ov

    monkeypatch.setattr(ov, "PAGE_SIZE", size)
    c = _client(user)
    pages, cursor = [], None
    for _ in range(50):
        body = json.loads(c.get("/api/ops/exceptions", {"cursor": cursor} if cursor else {}).content)
        pages.append([(x["code"], x["entity_ref"]) for x in body["items"]])
        cursor = body["next_cursor"]
        if not cursor:
            return pages
    raise AssertionError("pages never ended")


def test_issue_pages_are_full_and_exact_for_a_hub_scoped_viewer(rs, monkeypatch):
    from operations import scope
    from operations.exceptions import open_exceptions
    from operations.snapshot import read_snapshot

    with read_snapshot() as at:
        everything = open_exceptions(at).items
    hb = {rs.hub_b.pk}
    expect = [(x.code, x.entity_ref) for x in scope.keep_issues(everything, hb)]
    assert len(expect) >= 3 and len(expect) < len(everything)
    # before the fix, HUB-1 rows were filtered away AFTER the limit of each page: some pages came out short
    before = [scope.keep_issues(everything[i : i + 2], hb) for i in range(0, len(everything), 2)]
    assert any(len(p) < 2 for p in before[:-1]), [len(p) for p in before]
    pages = _api_pages(rs.field_b, monkeypatch, 2)
    assert all(len(p) == 2 for p in pages[:-1]) and 1 <= len(pages[-1]) <= 2, pages
    assert [x for p in pages for x in p] == expect  # nothing lost, nothing repeated, nothing of HUB-1
    # the regional viewer still pages through everything, unchanged
    assert [x for p in _api_pages(rs.ops, monkeypatch, 2) for x in p] == [
        (x.code, x.entity_ref) for x in everything
    ]


def test_scoped_coverage_counts_the_viewers_hubs(rs):
    from operations.exceptions import open_exceptions
    from operations.snapshot import read_snapshot
    from tests.test_operations_exceptions import configure

    configure(ops_readiness_lookahead_min=60 * 24 * 60)
    with read_snapshot() as at:
        regional = open_exceptions(at).readiness_coverage
        a = open_exceptions(at, hub_ids={rs.hub_a.pk}).readiness_coverage
        b = open_exceptions(at, hub_ids={rs.hub_b.pk}).readiness_coverage
    assert a["qualifying"] + b["qualifying"] == regional["qualifying"]
    assert b["qualifying"] == 0 and b["complete"] is True


def test_scoped_issue_statements_are_constant(rs, monkeypatch):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    import platform_core.interaction as ia
    from fleet import services as fs
    from tests.execution_world import raw_incident
    from tests.test_v2_03a_transfers import _app_conn

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)  # no opportunistic token clean-up in the counts
    c = _client(rs.field_b)

    def count(path: str) -> int:
        c.get(path)  # warm
        with CaptureQueriesContext(connection) as q:
            assert c.get(path).status_code == 200
        return len(q)

    small = {p: count(p) for p in ("/ops/exceptions/", "/api/ops/exceptions")}
    with _app_conn() as conn:  # more rows at the other hub and at this one
        raw_incident(conn, rs, robot_id=fs._robot("R-03").pk)
        raw_incident(conn, rs, robot_id=fs._robot("RB-1").pk)
    big = {p: count(p) for p in ("/ops/exceptions/", "/api/ops/exceptions")}
    print(f"scoped Issues statements: {small} -> {big}")
    assert small == big


# ================================================================== P2: compact phone header
def test_the_phone_header_folds_the_nav_but_never_the_safety_badge_or_main_messages():
    from pathlib import Path

    from fleet.models import DeploymentSetting
    from tests.conftest import make_user

    DeploymentSetting.objects.update_or_create(key="demo_mode", hub=None, defaults={"value": "true"})
    ops = make_user("hdr_ops", "ops_lead")
    html = _client(ops).get("/planning/board/").content.decode()
    header = html[html.index('<header class="site-header">') : html.index("</header>")]
    # a real button controls the folded list; without JavaScript nothing folds (the .js class gates it)
    assert re.search(
        r'<button type="button" class="navbtn" aria-expanded="false" aria-controls="main-nav"', header
    )
    assert 'id="main-nav"' in header and "classList.add('js')" in html
    # the demo badge stays in the header row itself, outside the folded nav and the user menu
    badge = header.index("data-demo-badge")
    assert not (header.index('<nav class="primary-nav"') < badge < header.index("</nav>"))
    assert badge < header.index('<details class="usermenu"')
    css = (Path(__file__).resolve().parents[1] / "templates" / "base.html").read_text()
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # rules only, not comments
    phone = rules.split(".navbtn{display:none}")[1].split("{% block extra_css %}")[0]
    folded = re.findall(r"([^{}]*)\{display:none\}", phone)
    hidden = " ".join(folded)
    for safe in (".demo-badge", ".banner", ".msg", ".toast", ".pb", "main", ".notice"):
        assert safe not in hidden, (safe, folded)
    assert "min-height:44px" in phone and ".subnav a{min-height:44px" in phone


# ================================================================== P3: varied next free slots
def test_next_free_prefers_slots_that_differ():
    """A long opening day with one free crew: three consecutive half-hours would be near-identical; the
    varied rule shows slots at least two hours apart, still earliest first and in rank order."""
    from fleet.models import DeploymentSetting

    w = _world("cv_")
    DeploymentSetting.objects.filter(key="hub_operating_hours", hub=w.hub).update(value="08:00-18:00")
    got = nf.next_free(w.target, now=at(DAY, 5))
    assert len(got.slots) == 3 and not got.exhausted
    s = got.slots
    assert all(not nf.near_identical(a, b) for i, a in enumerate(s) for b in s[i + 1 :]), [x.label for x in s]
    assert [x.key for x in s] == sorted(x.key for x in s)
    # the first slot is still the earliest Available one (nothing better is skipped for variety)
    first = nf.next_free(w.target, now=at(DAY, 5), limit=1).slots[0]
    assert s[0].start == first.start and s[0].robot == first.robot
    assert got.evaluations <= nf.BUDGET


def test_variety_never_shows_fewer_slots():
    """Only near-identical slots exist (a 2.5-hour day, one crew): they fill the list in rank order."""
    from fleet.models import DeploymentSetting

    w = _world("cw_")
    DeploymentSetting.objects.filter(key="hub_operating_hours", hub=w.hub).update(value="08:00-10:30")
    got = nf.next_free(w.target, now=at(DAY, 5))
    assert len(got.slots) == 3
    assert [x.key for x in got.slots] == sorted(x.key for x in got.slots)


# ================================================================== P3: terminology ("Vehicle", never a leading "Van")
def test_no_line_leads_with_van_on_the_dispatch_pages(monkeypatch):
    from tests.test_v2_03b_design import _lines

    w = _no_vehicle_fits("ct_")
    monkeypatch.setattr(nf, "_now", lambda: at(DAY, 5))
    c = _client(w.ops)
    base = {"site": "S-1", "day": DAY.isoformat(), "start": "08:00", "end": "09:00", "tasks": "T-04",
            "scopes": "presence"}  # fmt: skip
    pages = {
        "board": c.get(f"/planning/board/?day={DAY.isoformat()}"),
        "week": c.get(f"/planning/?week={DAY.isoformat()}"),
        "visit": c.get(f"/planning/visits/{w.a}/"),
        "review": c.get(f"/planning/visits/{w.target.code}/?review=1"),
        "book": c.post("/planning/book/", {**base, "find": "1"}),
    }
    leading = re.compile(r"^(?:VAN-?\w*|Vans?)\b")
    for name, r in pages.items():
        assert r.status_code == 200, name
        html = r.content.decode()
        bad = [x for x in _lines(html) if leading.match(x)]
        assert not bad, (name, bad)
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<(style|script)\b.*?</\1>", "", html, flags=re.S))
        assert not re.search(r"\b[Vv]ans?\b", text), name  # the word is "vehicle"
    week = pages["week"].content.decode()
    assert "Vehicle VAN-1" in week and "Robot R-01" in week


# ================================================================== crew of another hub (planning/0015)
def _cross_hub_message(w, person: str = "P-9") -> str:
    from planning.board import person_name
    from planning.models import Person

    p = Person.objects.get(code=person)
    return f"{person_name(p)} works at Hub B, not at this visit's hub. Choose crew from {w.hub_a.name}."


@pytest.mark.parametrize("field", ["lead", "second"])
def test_a_crew_member_of_another_hub_is_refused_by_the_plan_command(field, monkeypatch):
    """A forged POST (the regional ops lead: a resource rule, not a permission) with HB's P-9 as lead or second
    for a HUB-1 visit: refused by the Python check with the plain message, nothing written; with the Python
    check bypassed, the database trigger refuses with the same words."""
    from planning.models import Reservation, VisitRevision

    w = _two_hubs(f"cx{field[0]}_")
    v = _visit_at(w)
    fields = {"robot": "R-01", "lead": "P-1", "second": "P-2", "vehicle": "VAN-1", "reason": "SYN forged"}
    fields[field] = "P-9"
    before = (VisitRevision.objects.count(), Reservation.objects.count())
    c = _client(w.ops)
    page = c.get(f"/planning/visits/{v.code}/").content.decode()
    fields["_form_token"] = re.search(r'name="_form_token" value="([^"]+)"', page).group(1)
    out = c.post(f"/planning/visits/{v.code}/plan/", fields, follow=True).content.decode()
    assert _cross_hub_message(w) in unescape(out)
    assert (VisitRevision.objects.count(), Reservation.objects.count()) == before
    # the database boundary: the same refusal when the Python check is skipped
    monkeypatch.setattr(ps, "crew_hub_check", lambda *a, **k: None)
    with pytest.raises(ps.Conflict, match="works at Hub B, not at this visit's hub. Choose crew from"):
        ps.plan_visit(w.ops, visit_code=v.code, robot_code="R-01", lead_code=fields["lead"],
                      second_code=fields["second"], vehicle_code="VAN-1")  # fmt: skip
    assert (VisitRevision.objects.count(), Reservation.objects.count()) == before


def test_direct_sql_as_ops_app_cannot_insert_a_revision_with_another_hubs_crew():
    import psycopg

    from planning.models import Person, VisitRevision
    from tests.test_v2_03a_transfers import _app_conn

    w = _two_hubs("cs_")
    v = _visit_at(w)
    rev = v.current_revision
    cols = [f.column for f in VisitRevision._meta.concrete_fields if f.column != "id"]
    p9, p1 = Person.objects.get(code="P-9").pk, Person.objects.get(code="P-1").pk

    def insert(cur, lead_id: int, n: int) -> None:
        exprs = [
            "%(lead)s" if col == "lead_id" else ("revision_no + %(n)s" if col == "revision_no" else col)
            for col in cols
        ]
        cur.execute(
            f"INSERT INTO planning_visitrevision ({', '.join(cols)}) "
            f"SELECT {', '.join(exprs)} FROM planning_visitrevision WHERE id = %(id)s",
            {"lead": lead_id, "n": n, "id": rev.pk},
        )

    with _app_conn(autocommit=False) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="works at Hub B, not at this visit's hub"):
            insert(cur, p9, 1)
        conn.rollback()
        insert(cur, p1, 1)  # same-hub crew: accepted by the same trigger
        conn.rollback()  # nothing kept


def test_same_hub_crew_still_plans_and_hb_plans_with_its_own_crew():
    w = _two_hubs("cg_")
    v = _visit_at(w)
    out = ps.plan_visit(w.ops, visit_code=v.code, robot_code="R-01", lead_code="P-1", second_code="P-3",
                        vehicle_code="VAN-1")  # fmt: skip
    assert out["feasibility"] != "blocked"
    # an HB visit refuses HUB-1's crew the same way (the rule is symmetric)
    hb = _visit_at(w, hub="HB")
    with pytest.raises(
        ps.Conflict, match=f"works at {w.hub_a.name}, not at this visit's hub. Choose crew from Hub B"
    ):
        ps.plan_visit(w.ops, visit_code=hb.code, robot_code="R-03", lead_code="P-9", second_code="P-1",
                      vehicle_code="VAN-9")  # fmt: skip


def test_existing_cross_hub_plans_are_counted_and_none_exist_in_the_fixture():
    from django.db import connection

    w = _two_hubs("cc_")
    ps.plan_visit(w.ops, visit_code=_visit_at(w).code, robot_code="R-01", lead_code="P-1", second_code="P-2",
                  vehicle_code="VAN-1")  # fmt: skip
    with connection.cursor() as cur:
        cur.execute(ps.CROSS_HUB_CREW_SQL)
        assert cur.fetchone()[0] == 0

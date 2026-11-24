"""v2 01D acceptance fixes on the planning screens and Today (J3-1, J3-2, J3-3, J2-1, J2-2, J5-3 planning).

* J3-1: plan form options are labelled for the visit's window (free / busy / not qualified / not ready), free
  first, with a fixed statement count; "Suggest a free robot, crew and vehicle" is READ ONLY, uses the
  planner's own evaluate(), is bounded, and the guarded plan command still decides on save.
* J3-2: "not ready for planning" names the reasons and who can fix them.
* J3-3: task words with spaces are normalized (laundry folding -> laundry_folding); sentences stay refused.
* J2-1 / J2-2: Today coverage wording when all visits are assessed; a jump link to the attention cards.
SYNTHETIC data only.
"""

import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.db import connection
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from fleet import services as fs
from operations.today import Today
from planning import options as opt
from planning import services as p
from planning.forms import BookForm
from planning.models import Reservation, VehicleTrip, Visit, VisitRevision
from planning.plain import plain_blocker
from platform_core.models import AuditEvent, FormToken
from tests.planning_world import DAY, build_planning, new_visit, plan

pytestmark = pytest.mark.django_db

# Statement budget of the visit page for a planner (documented in planning/options.py): the page's own reads
# plus a fixed number for the labelled options, independent of the number of robots, people and vehicles;
# a suggestion adds at most SUGGEST_MAX_EVALUATIONS evaluate() calls on top.
# Measured 2026-11-22 on the 3-robot world: page 33 statements; page + one evaluate() 79.
PAGE_BUDGET = 40
EVALUATE_BUDGET = 50  # statements of one planning.services.evaluate() call
SUGGEST_BUDGET = PAGE_BUDGET + opt.SUGGEST_MAX_EVALUATIONS * EVALUATE_BUDGET


@pytest.fixture
def w():
    w = build_planning(robots=3)
    # V1 (DAY 10:00-11:00) holds R-01, P-1 (lead), P-2 (second) and VAN-1; R-03 is on hold
    w.v1 = new_visit(w)
    assert plan(w, w.v1)["feasibility"] == "feasible"
    fs.place_hold(w.ops, scope="robot", target="R-03", source_type="maintenance", reason="noise in hip")
    return w


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _labels(opts: list[opt.Option]) -> list[str]:
    return [o.label for o in opts]


def _html(r: Any) -> str:
    return r.content.decode()


def _select(html: str, name: str) -> list[tuple[str, str, bool]]:
    block = re.search(rf'<select name="{name}">(.*?)</select>', html, re.S)
    assert block, name
    return [
        (m.group(1), m.group(3), bool(m.group(2)))
        for m in re.finditer(r'<option value="([^"]+)"( selected)?[^>]*>([^<]+)</option>', block.group(1))
    ]


def _counts() -> dict[str, int]:
    return {
        "reservations": Reservation.objects.count(),
        "revisions": VisitRevision.objects.count(),
        "visits": Visit.objects.count(),
        "trips": VehicleTrip.objects.count(),
        "audit": AuditEvent.objects.count(),
    }


# ------------------------------------------------------------------ J3-1 labels
def test_options_are_labelled_for_the_window_and_free_first(w):
    v2 = new_visit(w, h0=11, h1=12)  # overlaps V1's crew, robot and van
    opts = opt.plan_options(Visit.objects.get(code=v2))
    assert _labels(opts.robots) == [
        "R-02 · free",
        f"R-01 · busy {w.v1} until 14:50",
        "R-03 · not ready: on hold",
    ]
    ops, field, field2 = w.ops.username, w.field.username, w.field2.username
    assert _labels(opts.leads) == [
        f"P-3 · {field2} · free",
        f"P-1 · {ops} · busy {w.v1} 08:40–11:50",
        f"P-2 · {field} · not qualified as lead",
    ]
    assert [o.status for o in opts.seconds] == ["free", "busy", "busy"]
    assert _labels(opts.seconds)[0] == f"P-3 · {field2} · free"
    assert _labels(opts.vehicles) == [
        "VAN-2 · free",
        f"VAN-1 · busy {w.v1} until 11:50",
        "VAN-X · capacity not recorded",
    ]
    # the page shows the same labels, in the same order, with the codes as values (J5-3: person labels)
    html = _html(_client(w.ops).get(f"/planning/visits/{v2}/"))
    assert [x[1] for x in _select(html, "robot")] == _labels(opts.robots)
    assert _select(html, "lead")[0][:2] == ("P-3", f"P-3 · {field2} · free")
    assert "Free options are listed first" in html


def test_labels_cost_a_fixed_number_of_statements(w):
    v2 = new_visit(w, d=DAY + timedelta(days=1))
    c = _client(w.ops)
    c.get(
        f"/planning/visits/{v2}/"
    )  # warm-up: the first render may also prune expired form tokens (one statement)
    with CaptureQueriesContext(connection) as small:
        c.get(f"/planning/visits/{v2}/")
    for n in range(3, 9):
        p.register_vehicle(w.ops, code=f"VAN-{n}", robot_capacity=1, restraint_kit="SYN kit")
    with CaptureQueriesContext(connection) as big:
        c.get(f"/planning/visits/{v2}/")
    assert len(small) == len(big) <= PAGE_BUDGET, (len(small), len(big))
    started = time.perf_counter()
    with CaptureQueriesContext(connection) as sug:
        r = c.get(f"/planning/visits/{v2}/?suggest=1")
    elapsed = time.perf_counter() - started
    assert r.status_code == 200 and len(sug) <= SUGGEST_BUDGET, len(sug)
    assert elapsed < 3.0, elapsed  # generous: a shared test machine; measured well under 0.5 s
    print(
        f"visit page statements: {len(small)} (budget {PAGE_BUDGET}); with suggestion {len(sug)} "
        f"(budget {SUGGEST_BUDGET}), {elapsed * 1000:.0f} ms"
    )


# ------------------------------------------------------------------ J3-1 suggest
def test_suggestion_is_read_only_and_the_plan_command_accepts_it(w):
    v2 = new_visit(w, d=DAY + timedelta(days=1))
    c = _client(w.ops)
    t0 = FormToken.objects.count()
    c.get(f"/planning/visits/{v2}/")  # a plain view, to compare the form-token rows every page render issues
    t1 = FormToken.objects.count()
    before = _counts()
    html = _html(c.get(f"/planning/visits/{v2}/?suggest=1"))
    assert _counts() == before  # no booking, reservation, revision, trip or audit row
    assert FormToken.objects.count() - t1 == t1 - t0  # only the page's own one-time form tokens
    assert "Suggested assignment · Not yet saved" in html
    assert "Available when checked — revalidated when saved." in html
    assert "Planning eligibility is not departure readiness" in html
    box = html.split('data-suggestion="found"', 1)[1].split("</div>", 1)[0]
    for word in ("reserved", "confirmed", "ready to depart", "approved"):
        assert word not in box.lower(), word
    assert "Robot R-01 has no recorded planning conflict and meets the current planning checks." in box
    picked = {
        name: next(v for v, _label, sel in _select(html, name) if sel)
        for name in ("robot", "lead", "second", "vehicle")
    }
    assert picked == {"robot": "R-01", "lead": "P-1", "second": "P-2", "vehicle": "VAN-1"}  # documented order
    # the same snapshot: the planner's own check passes, and the guarded command accepts it on save
    visit = Visit.objects.get(code=v2)
    rev = visit.current_revision
    from fleet.models import Robot
    from planning.models import Person, Vehicle

    check = p.evaluate(
        site=visit.site,
        start=rev.on_site_start,
        end=rev.on_site_end,
        travel_out=rev.travel_out_min,
        travel_back=rev.travel_back_min,
        tasks=rev.tasks,
        scopes=rev.required_consent_scopes,
        robot=Robot.objects.get(code="R-01"),
        lead=Person.objects.get(code="P-1"),
        second=Person.objects.get(code="P-2"),
        vehicle=Vehicle.objects.get(code="VAN-1"),
        visit=visit,
    )
    assert check.feasibility != "blocked"
    token = re.search(r'name="_form_token" value="([^"]+)"', html).group(1)
    c.post(f"/planning/visits/{v2}/plan/", {**picked, "reason": "suggested", "_form_token": token})
    assert Visit.objects.get(code=v2).status == "planned"


def test_unqualified_unavailable_or_not_ready_resources_are_never_suggested(w):
    v2 = new_visit(w, h0=11, h1=12)  # R-01, P-1, P-2 and VAN-1 are busy; R-03 on hold; P-2 not a lead
    s = opt.suggest(Visit.objects.get(code=v2), opt.plan_options(Visit.objects.get(code=v2)))
    assert not s.found  # only P-3 is free: no second person
    assert s.why_not == ["Two different qualified people are needed, and only one is free."]
    v3 = new_visit(w, d=DAY + timedelta(days=1))
    s = opt.suggest(Visit.objects.get(code=v3), opt.plan_options(Visit.objects.get(code=v3)))
    assert s.found and s.robot.code != "R-03" and s.lead.code != "P-2" and s.vehicle.code != "VAN-X"
    assert s.evaluations <= opt.SUGGEST_MAX_EVALUATIONS


def test_no_feasible_combination_explains_why_and_offers_manual_choice(w):
    v2 = new_visit(w, d=DAY + timedelta(days=2), travel=None)  # unknown travel blocks every combination
    html = _html(_client(w.ops).get(f"/planning/visits/{v2}/?suggest=1"))
    assert 'data-suggestion="none"' in html and "Suggested assignment" not in html
    assert "Travel time to or from the home is not known." in html
    assert "choose a robot, crew and vehicle yourself" in html
    assert not any(sel for name in ("robot", "lead", "second", "vehicle") for *_x, sel in _select(html, name))
    s = opt.suggest(Visit.objects.get(code=v2), opt.plan_options(Visit.objects.get(code=v2)))
    assert 1 <= s.evaluations <= opt.SUGGEST_MAX_EVALUATIONS


def test_a_booking_between_suggest_and_save_makes_the_save_fail_safely(w):
    v2 = new_visit(w, d=DAY + timedelta(days=1))
    c = _client(w.ops)
    html = _html(c.get(f"/planning/visits/{v2}/?suggest=1"))
    token = re.search(r'name="_form_token" value="([^"]+)"', html).group(1)
    # someone else books the same robot, crew and van for the same time first
    v3 = new_visit(w, d=DAY + timedelta(days=1))
    assert plan(w, v3, robot="R-01", lead="P-1", second="P-2", vehicle="VAN-1")["feasibility"] == "feasible"
    r = c.post(
        f"/planning/visits/{v2}/plan/",
        {
            "robot": "R-01",
            "lead": "P-1",
            "second": "P-2",
            "vehicle": "VAN-1",
            "reason": "x",
            "_form_token": token,
        },
        follow=True,
    )
    visit = Visit.objects.get(code=v2)
    assert visit.status == "requested" and visit.current_revision.feasibility == "blocked"
    assert not Reservation.objects.filter(revision__visit=visit, released_at__isnull=True).exists()
    assert f"already booked for visit {v3}" in _html(r)


def test_roles_without_plan_permission_get_no_suggest_and_the_server_refuses(w):
    v2 = new_visit(w, d=DAY + timedelta(days=1))
    for user in (w.field, w.lead, w.safety):  # planning.read, not planning.plan
        c = _client(user)
        page = _html(c.get(f"/planning/visits/{v2}/"))
        assert "data-suggest" not in page and "?suggest=1" not in page
        r = c.get(f"/planning/visits/{v2}/?suggest=1")
        assert r.status_code == 403


# ------------------------------------------------------------------ J3-2 not ready reasons
def test_not_ready_robot_names_the_reason_and_who_can_fix_it(w):
    v2 = new_visit(w, d=DAY + timedelta(days=1))
    r = plan(w, v2, robot="R-03")
    raw = next(
        b for b in Visit.objects.get(code=v2).current_revision.blockers if b["code"] == "ROBOT_NOT_ELIGIBLE"
    )
    b = plain_blocker(raw)
    assert b["sentence"].startswith("Robot R-03 is not ready for planning: it is on hold (Robot hold")
    assert "The ops lead or the engineering lead can release the hold." in b["action"]
    assert r["feasibility"] == "blocked"
    page = _html(_client(w.ops).get(f"/planning/visits/{v2}/"))
    assert "it is on hold" in page and "can release the hold" in page
    overdue = plain_blocker(
        {
            "code": "ROBOT_NOT_ELIGIBLE",
            "message": "R-02: Periodic inspection overdue since 2026-12-01 10:00:00 UTC",
        }
    )
    assert "periodic inspection is overdue" in overdue["sentence"]
    assert "the field operator can record a periodic inspection" in overdue["action"]
    lifecycle = plain_blocker(
        {"code": "ROBOT_NOT_ELIGIBLE", "message": "R-02: Lifecycle is Retired, not In service"}
    )
    assert (
        "not in service" in lifecycle["sentence"]
        and "The engineering lead can move it into service" in lifecycle["action"]
    )


# ------------------------------------------------------------------ J3-3 tasks
@pytest.mark.parametrize(
    ("typed", "codes"),
    [
        ("T-04, laundry folding", ["T-04", "laundry_folding"]),
        ("Laundry folding", ["laundry_folding"]),
        ("dish   washing; T-07", ["dish_washing", "T-07"]),
    ],
)
def test_task_words_are_normalized(typed, codes, w):
    form = BookForm(
        {
            "site": "S-1",
            "day": "2026-12-10",
            "start": "09:00",
            "end": "10:00",
            "tasks": typed,
            "scopes": ["presence"],
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["tasks"] == codes


@pytest.mark.parametrize("typed", ["needs oxygen tank", "Mrs Smith is ill", "Mrs Smith", "laundry 2"])
def test_sentences_and_names_are_still_refused(typed, w):
    form = BookForm(
        {
            "site": "S-1",
            "day": "2026-12-10",
            "start": "09:00",
            "end": "10:00",
            "tasks": typed,
            "scopes": ["presence"],
        }
    )
    assert not form.is_valid() and "tasks" in form.errors
    assert "laundry folding" in BookForm().fields["tasks"].help_text


# ------------------------------------------------------------------ J2-1 / J2-2 Today
def _today_html(**kw: Any) -> str:
    t = Today(
        at=datetime(2026, 12, 8, 18, tzinfo=UTC), local_date=date(2026, 12, 8), local_date_text="Tue 8 Dec"
    )
    t.visits_ok = True
    t.visit_counts = {"visits": 0, "ready": 0, "approved": 0, "at_risk": 0, "blocked": 0}
    for k, v in kw.items():
        setattr(t, k, v)
    request = RequestFactory().get("/ops/")
    from django.contrib.auth.models import AnonymousUser

    request.user = AnonymousUser()
    return render_to_string(
        "operations/today.html",
        {"t": t, "as_of_iso": t.at.isoformat(), "robot_groups": [], "can": {}},
        request=request,
    )


def test_today_coverage_note_says_all_when_every_visit_is_assessed(db):
    html = _today_html(coverage_counts={"all": 10, "assessed": 10, "unassessed": 0, "today_assessed": 5})
    assert "All 10 visits have a readiness result." in html and "Only 10 of 10" not in html
    html = _today_html(coverage_counts={"all": 10, "assessed": 7, "unassessed": 3, "today_assessed": 5})
    assert "Only 7 of 10 visits have a readiness result." in html
    html = _today_html(coverage_counts={"all": 1, "assessed": 1, "unassessed": 0, "today_assessed": 1})
    assert "All 1 visit has a readiness result." in html


def test_today_has_a_jump_link_to_the_attention_cards(db):
    html = _today_html(
        coverage_counts={"all": 0, "assessed": 0, "unassessed": 0, "today_assessed": 0}, attention_total=12
    )
    assert '<a href="#attention" data-jump="attention">12 items need your attention</a>' in html
    assert 'id="attention"' in html
    assert "<script" not in html.split("<main", 1)[-1].split("</main>")[0]  # no inline script (CSP hash test)
    one = _today_html(
        coverage_counts={"all": 0, "assessed": 0, "unassessed": 0, "today_assessed": 0}, attention_total=1
    )
    assert "1 item needs your attention" in one
    none = _today_html(coverage_counts={"all": 0, "assessed": 0, "unassessed": 0, "today_assessed": 0})
    assert 'data-jump="attention"' not in none

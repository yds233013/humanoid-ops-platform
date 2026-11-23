"""01D acceptance walkthrough fixes for the readiness and field-run screens (docs/evidence/01d_acceptance_walkthrough.md:
J4-1..J4-4, J5-1..J5-5). Display and guidance only: every command keeps enforcing its own rules.
"""

import re
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.test import Client

from execution.models import CustodyEvent, ExecutionRun
from fleet import services as fs
from planning.models import Person
from platform_core.timezones import workspace_zone
from readiness import commands as rc
from readiness.models import DepartureEvent
from tests.conftest import make_user
from tests.operations_world import advance, depart_new, ops_world
from tests.planning_world import DAY, new_visit, plan

pytestmark = pytest.mark.django_db(transaction=True)
TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')


@pytest.fixture
def w():
    return ops_world(prefix="fh_", robots=3)


def client_as(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def html(r: Any) -> str:
    return r.content.decode()


def token(c: Client, url: str, action: str) -> str:
    body = html(c.get(url))
    m = re.search(r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', body, re.S)
    assert m, f"no form to {action} on {url}"
    found = TOKEN_RE.search(m.group(1))
    assert found
    return found.group(1)


def post(c: Client, page: str, action: str, data: dict[str, Any]) -> str:
    return html(c.post(action, {**data, "_form_token": token(c, page, action)}, follow=True))


def fresh_visit(w: Any, robot: str = "R-02", days: int = 5) -> str:
    code = new_visit(w, d=DAY + timedelta(days=days))
    plan(w, code, robot=robot)
    w.p.confirm_visit(w.ops, visit_code=code)
    return code


def select_options(page: str, name: str) -> str:
    m = re.search(r'<select name="' + name + r'"[^>]*>(.*?)</select>', page, re.S)
    return m.group(1) if m else ""


# ------------------------------------------------------------------ J4-1 / J4-2 inspections for the visit
def test_inspection_select_offers_only_inspections_usable_for_this_visit(w):
    code = fresh_visit(w)
    routine = fs.submit_inspection(
        w.ops, robot_code="R-02", template_version="T1", answers=dict(w.all_pass),
        performer=w.field.username, verifier=w.field2.username,
    )["entity_id"]  # fmt: skip
    other_visit = fs.submit_inspection(
        w.ops, robot_code="R-02", template_version="T1", answers=dict(w.all_pass),
        performer=w.field.username, verifier=w.field2.username, visit_ref=w.visit,
    )["entity_id"]  # fmt: skip
    url = f"/readiness/visits/{code}/"
    page = html(client_as(w.ops).get(url))
    # nothing usable yet: no inspection select, the primary action is a new inspection
    assert 'data-no-usable-inspection="1"' in page
    assert (
        f'<a class="btn primary" href="/readiness/visits/{code}/inspection/">Submit a new inspection' in page
    )
    assert routine not in select_options(page, "inspection_code") and 'name="inspection_code"' not in page
    mine = fs.submit_inspection(
        w.ops, robot_code="R-02", template_version="T1", answers=dict(w.all_pass),
        performer=w.field.username, verifier=w.field2.username, visit_ref=code,
    )["entity_id"]  # fmt: skip
    options = select_options(html(client_as(w.ops).get(url)), "inspection_code")
    assert mine in options and routine not in options and other_visit not in options


def test_after_submitting_an_inspection_the_visit_has_it_preselected_with_the_next_step(w):
    code = fresh_visit(w)
    c = client_as(w.ops)
    form_url = f"/readiness/visits/{code}/inspection/"
    data = {"template_version": "T1", **{f"item_{k}": v for k, v in w.all_pass.items()}}
    data.update(performer=w.field.username, verifier=w.field2.username)
    body = html(c.get(form_url))
    data["_form_token"] = TOKEN_RE.search(body).group(1)
    r = c.post(form_url, data)
    assert r.status_code == 302
    assert re.fullmatch(rf"/readiness/visits/{code}/\?inspection=[A-Z]+-\d+#evidence", r["Location"])
    page = html(c.get(r["Location"]))
    assert "Next: record it as pre-departure evidence below." in page  # the toast
    assert 'data-next-evidence="1"' in page
    new_code = r["Location"].split("inspection=")[1].split("#")[0]
    assert re.search(r'<option value="' + new_code + r'" selected>', page)


# ------------------------------------------------------------------ J4-3 / J4-4 plain words
def test_plain_labels_for_evidence_equipment_and_mode(w):
    code = fresh_visit(w)
    url = f"/readiness/visits/{code}/"
    c = client_as(w.field)
    page = html(c.get(url))
    assert "Record an equipment check" in page and "Record a equipment" not in page
    assert ">Restraint kit <select" in page and ">Recovery kit <select" in page and ">Charger <select" in page
    action = f"/readiness/visits/{code}/evidence/"
    body = html(c.post(action, {
        "kind": "equipment_check", "result": "pass", "item_names": ["restraint_kit", "recovery_kit", "charger"],
        "item_restraint_kit": "present", "item_recovery_kit": "present", "item_charger": "present",
        "_form_token": TOKEN_RE.search(re.search(r'data-evidence-form="equipment_check">(.*?)</form>', page,
                                                 re.S).group(1)).group(1),
    }, follow=True))  # fmt: skip
    assert "Equipment check recorded" in body and "(equipment_check)" not in body
    assert "Restraint kit: present" in body and "restraint_kit: present" not in body
    assert "Equipment check recorded: pass" in body  # history line
    a = rc.authorize_departure(w.ops, visit_code=w.visit)
    ready = html(client_as(w.ops).get(f"/readiness/visits/{w.visit}/"))
    assert "Demo approval" in ready and "Demo (synthetic)" not in ready
    assert f"Authorization {a['code']} issued (Demo approval)" in ready


def test_checklist_complete_wording_depends_on_who_may_approve(w):
    url = f"/readiness/visits/{w.visit}/"
    ops = html(client_as(w.ops).get(url))
    assert "Checklist complete - you can approve it now." in ops
    field = html(client_as(w.field).get(url))
    assert "Checklist complete - ask the ops lead to approve." in field
    for page in (ops, field):  # neutral note, no "approved" before the decision
        note = re.search(r'data-checklist-complete="1"[^>]*>(.*?)</p>', page).group(1)
        assert "approved" not in note.lower()
        assert 'class="decision waiting-note" data-checklist-complete="1"' in page


# ------------------------------------------------------------------ J5-4 departure confirmation
def test_departure_needs_the_confirm_tick_then_the_page_shows_the_run(w):
    a = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    url = f"/readiness/visits/{w.visit}/"
    action = f"/readiness/authorizations/{a}/depart/"
    c = client_as(w.field)
    page = html(c.get(url))
    assert 'name="confirm" value="1" required' in page and "The robot is leaving now" in page
    body = post(c, url, action, {"idempotency_key": "fh-dep-1"})
    assert "Refused (invalid input)" in body and "to record the departure" in body
    assert "Nothing was recorded" in body and not DepartureEvent.objects.exists()
    body = post(c, url, action, {"idempotency_key": "fh-dep-2", "confirm": "1"})
    assert DepartureEvent.objects.count() == 1
    run = ExecutionRun.objects.get(visit__code=w.visit)
    assert 'data-departed="1"' in body and f'href="/execution/runs/{run.code}/">{run.code}</a>' in body
    assert "Not ready to leave yet" not in body and "Readiness incomplete" not in body


# ------------------------------------------------------------------ J5-2 next step, J5-5 receiver
STAGE_STEPS = [
    ("departed", "verify"),
    ("verified", "arrive_site"),
    ("arrived_site", "to_site"),
    ("at_site", "site_check"),
    ("session_active", "stop"),
    ("session_stopped", "to_vehicle"),
    ("back_in_vehicle", "depart_site"),
    ("returning", "arrive_hub"),
    ("arrived_hub", "to_hub"),
    ("at_hub", "inspect"),
    ("inspected", "reconcile"),
    ("reconciled", "close"),
    ("closed", "done"),
]


def test_next_step_card_follows_the_run_state(w):
    run = depart_new(w, "R-02")
    done = "departed"
    stages = [s for s, _ in STAGE_STEPS]
    for stage, step in STAGE_STEPS:
        if stages.index(stage) > stages.index(done):
            advance_from(w, run, done, stage)
            done = stage
        page = html(client_as(w.ops).get(f"/execution/runs/{run.code}/"))
        assert f'data-next-step="{step}"' in page, (stage, re.findall(r'data-next-step="([^"]+)"', page))


def advance_from(w: Any, run: ExecutionRun, done: str, stage: str) -> None:
    """Drive the run from ``done`` up to ``stage`` (tests.operations_world.advance starts at departure)."""
    from tests.operations_world import RUN_STAGES

    steps = RUN_STAGES[RUN_STAGES.index(done) + 1 : RUN_STAGES.index(stage) + 1]
    for s in steps:
        if s == "session_paused":
            continue
        if s == "session_stopped":
            w.cmd.session(run, "stop")
            continue
        _one(w, run, s)


def _one(w: Any, run: ExecutionRun, s: str) -> None:
    c = w.cmd
    actions = {
        "verified": lambda: c.verify(run, CustodyEvent.objects.filter(run=run).order_by("seq").first().seq),
        "arrived_site": lambda: c.trip(run, "arrived_site"),
        "at_site": lambda: c.move(run, "site"),
        "session_active": lambda: c.session(run, "start", site_check_id=c.site_check(run)["id"]),
        "back_in_vehicle": lambda: c.move(run, "vehicle"),
        "returning": lambda: c.trip(run, "departed_site"),
        "arrived_hub": lambda: c.trip(run, "arrived_hub"),
        "at_hub": lambda: c.move(run, "hub", receiver="P-3"),
        "inspected": lambda: setattr(run, "_inspection", c.inspect(run)),
        "reconciled": lambda: c.reconcile(run, inspection=run._inspection),  # type: ignore[attr-defined]
        "closed": lambda: c.close(run),
    }
    actions[s]()


def test_site_check_is_not_offered_before_the_robot_is_in_site_custody(w):
    run = depart_new(w, "R-02")
    advance(w, run, "arrived_site")  # custody is still the vehicle
    url = f"/execution/runs/{run.code}/"
    page = html(client_as(w.field).get(url))
    assert 'data-site-check-form="1"' not in page
    assert f'action="/execution/runs/{run.code}/site-check/"' not in page
    assert "Hand the robot over to the site first: record a custody move to site with a receiver" in page
    # the next-step card offers the handover, with the site preselected and the receiver required
    card = re.search(r'data-next-step="to_site">(.*?)</div>\s*$', page, re.S | re.M)
    assert card and '<option value="site" selected>' in page
    w.cmd.move(run, "site")
    page = html(client_as(w.field).get(url))
    assert 'data-next-step="site_check"' in page and 'data-site-check-form="1"' in page
    assert "Hand the robot over to the site first" not in page


def test_reconciliation_is_not_offered_before_hub_receipt_and_post_visit_inspection(w):
    run = depart_new(w, "R-02")
    advance(w, run, "arrived_hub")
    url = f"/execution/runs/{run.code}/"
    page = html(client_as(w.ops).get(url))
    assert (
        f'action="/execution/runs/{run.code}/reconcile/"' not in page and 'data-reconcile-blocked="1"' in page
    )
    w.cmd.move(run, "hub", receiver="P-3")
    page = html(client_as(w.ops).get(url))
    assert f'action="/execution/runs/{run.code}/reconcile/"' not in page
    assert "Record the post-visit inspection for this visit first" in page
    insp = w.cmd.inspect(run)
    page = html(client_as(w.ops).get(url))
    assert f'action="/execution/runs/{run.code}/reconcile/"' in page and insp in page


def test_custody_form_marks_the_receiver_required_and_refuses_without_one(w):
    run = depart_new(w, "R-02")
    advance(w, run, "arrived_site")
    url = f"/execution/runs/{run.code}/"
    c = client_as(w.field)
    page = html(c.get(url))
    assert "Receiver (required)" in page and '<select name="received_by" required' in page
    assert "Receiver is required: every move" in page
    before = CustodyEvent.objects.count()
    body = post(c, url, f"/execution/runs/{run.code}/custody/",
                {"to_kind": "site", "custodian": "P-1", "received_by": ""})  # fmt: skip
    assert "Refused (invalid input)" in body and "choose the receiver" in body
    assert CustodyEvent.objects.count() == before


# ------------------------------------------------------------------ J5-3 person labels
def test_people_are_shown_with_code_and_name(w):
    w.field.first_name, w.field.last_name = "Amara", "Okafor"
    w.field.save()
    assert Person.objects.get(code="P-2").label == "P-2 · Amara Okafor"
    run = depart_new(w, "R-02")
    page = html(client_as(w.ops).get(f"/execution/runs/{run.code}/"))
    assert "Crew: P-1 · " in page and "P-2 · Amara Okafor" in page
    assert '<option value="P-2">P-2 · Amara Okafor</option>' in page
    readiness = html(client_as(w.ops).get(f"/readiness/visits/{w.visit}/"))
    assert '<option value="P-2">P-2 · Amara Okafor</option>' in readiness


# ------------------------------------------------------------------ J5-1 my visits, landing, navigation
def test_my_visits_lists_only_the_signed_in_persons_visits_today_and_tomorrow(w):
    tomorrow = (datetime.now(workspace_zone()) + timedelta(days=1)).date()
    mine = new_visit(w, d=tomorrow)
    plan(w, mine, robot="R-02")  # lead P-1 (w.ops), second P-2 (w.field)
    w.p.confirm_visit(w.ops, visit_code=mine)
    page = html(client_as(w.field).get("/execution/mine/"))
    assert f'data-my-visit="{mine}"' in page and "Tomorrow" in page and "Second" in page
    assert f'href="/readiness/visits/{mine}/"' in page
    assert f'data-my-visit="{w.visit}"' not in page  # not today or tomorrow
    other = make_user("fh_other_field", "field_operator")
    w.p.register_person(w.ops, username=other.username, code="P-9")
    page = html(client_as(other).get("/execution/mine/"))
    assert "data-my-visit=" not in page and "No visits for you today or tomorrow" in page
    nobody = make_user("fh_no_person", "field_operator")
    assert "You are not registered as crew" in html(client_as(nobody).get("/execution/mine/"))
    # navigation: Dispatch shows Field runs and My visits
    assert 'href="/execution/mine/" aria-current="page">My visits' in page
    assert 'href="/execution/">Field runs' in page


def test_a_field_operator_lands_on_my_visits_others_keep_their_landing(w):
    crew_only = make_user("fh_land_field", "field_operator")
    assert client_as(crew_only).get("/")["Location"] == "/execution/mine/"
    assert client_as(w.ops).get("/")["Location"] == "/ops/"
    lead_and_field = make_user("fh_land_mixed", "field_operator", "ops_lead")
    assert client_as(lead_and_field).get("/")["Location"] == "/ops/"

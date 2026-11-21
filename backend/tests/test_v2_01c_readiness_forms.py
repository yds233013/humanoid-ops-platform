"""01C.5 (Engineer C): inspections and readiness forms, Today coverage, the shared planning token, admin landing.

For every new or converted form: success, a server-side denial for roles without the permission, a refusal in
plain words that names the role, and a replayed POST. Targeted runs only. SYNTHETIC data only.
"""

import re
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.db import connection
from django.test import Client

from accounts.roles import ALL_ROLES, PERMISSIONS
from fleet.models import Inspection
from operations import today
from planning import services as planning_services
from planning.models import Visit
from platform_core.interaction import who_can
from platform_core.models import FormToken
from readiness.models import DepartureAuthorization, ReadinessEvidence, RecoveryPlan
from tests.conftest import make_user
from tests.fleet_world import set_setting
from tests.planning_world import DAY, new_visit, plan
from tests.readiness_world import build_readiness

pytestmark = pytest.mark.django_db(transaction=True)
TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')


@pytest.fixture
def w():
    return build_readiness(prefix="rf_")


def client_as(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def html(r: Any) -> str:
    return r.content.decode()


def token_from(c: Client, url: str, action: str | None = None) -> str:
    """The token a rendered page carries (for the form posting to ``action`` when given)."""
    body = html(c.get(url))
    if action is not None:
        m = re.search(r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', body, re.S)
        assert m, f"no form to {action}"
        body = m.group(1)
    found = TOKEN_RE.search(body)
    assert found, f"no token on {url}"
    return found.group(1)


def post(c: Client, url: str, data: dict[str, Any], page: str | None = None, **kw: Any) -> Any:
    return c.post(url, {**data, "_form_token": token_from(c, page or url)}, follow=True, **kw)


def roles_without(permission: str) -> list[str]:
    return [r for r in ALL_ROLES if r not in PERMISSIONS[permission]]


def user_for(role: str) -> Any:
    return make_user(f"rf_role_{role}", role)


def visit_url(w: Any) -> str:
    return f"/readiness/visits/{w.visit}/"


# ------------------------------------------------------------------ 01C.5 submit an inspection
def inspection_data(w: Any, **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"template_version": "T1", **{f"item_{k}": v for k, v in w.all_pass.items()}}
    data.update(performer=w.field.username, verifier=w.field2.username)
    data.update(kw)
    return data


def test_inspection_form_is_on_the_visit_page_and_records_a_pass(w):
    c = client_as(w.ops)
    page = html(c.get(visit_url(w)))
    assert f'href="/readiness/visits/{w.visit}/inspection/"' in page
    url = visit_url(w) + "inspection/"
    form = html(c.get(url))
    assert (
        "Submit an inspection" in form and "Pass" in form and "(required)" not in form.split("Checklist")[0]
    )
    for item in w.all_pass:
        assert f'name="item_{item}"' in form
    before = Inspection.objects.count()
    r = post(c, url, inspection_data(w))
    body = html(r)
    assert r.status_code == 200 and "recorded: it passed" in body
    insp = Inspection.objects.latest("id")
    assert Inspection.objects.count() == before + 1
    assert insp.result == "PASS" and insp.visit_ref == w.visit and insp.complete
    assert insp.submitted_by == w.ops


def test_inspection_form_on_the_robot_page_records_without_a_visit(w):
    c = client_as(w.ops)
    assert "/readiness/robots/R-01/inspection/" in html(c.get("/fleet/R-01/"))
    assert "/readiness/robots/R-01/inspection/" not in html(client_as(w.eng).get("/fleet/R-01/"))
    r = post(c, "/readiness/robots/R-01/inspection/", inspection_data(w, item_I1="fail"))
    assert r.status_code == 200 and "recorded: it failed" in html(r)
    insp = Inspection.objects.latest("id")
    assert insp.result == "FAIL" and insp.visit_ref == "" and insp.hold_id is not None
    assert r.redirect_chain[-1][0] == "/fleet/R-01/"


@pytest.mark.parametrize("role", roles_without("fleet.submit_inspection"))
def test_inspection_form_denied_server_side_naming_the_role(w, role):
    c = client_as(user_for(role))
    before = Inspection.objects.count()
    for url in (visit_url(w) + "inspection/", "/readiness/robots/R-01/inspection/"):
        got = c.get(url)
        assert got.status_code == 403  # no part in this action: refused before the form is shown
        if "Only" in html(got):
            assert "Only the ops lead or the field operator can do this." in html(got)
        assert c.post(url, {**inspection_data(w), "_form_token": "x"}).status_code == 403
    assert Inspection.objects.count() == before


def test_unqualified_signer_is_refused_in_plain_words_naming_the_safety_reviewer(w):
    c = client_as(w.ops)
    url = visit_url(w) + "inspection/"
    before = Inspection.objects.count()
    r = post(c, url, inspection_data(w, performer=w.eng.username))
    body = html(r)
    assert r.status_code in (409, 422)
    assert f"{w.eng.username} is not qualified to sign inspections" in body
    assert f"Only {who_can('planning.grant_inspector_qualification')} can confirm" in body
    assert "the safety reviewer" in body
    assert Inspection.objects.count() == before


def test_inspection_replay_is_refused_and_runs_nothing(w):
    c = client_as(w.ops)
    url = visit_url(w) + "inspection/"
    tok = token_from(c, url)
    data = {**inspection_data(w), "_form_token": tok}
    c.post(url, data, follow=True)
    n = Inspection.objects.count()
    again = html(c.post(url, data, follow=True))
    assert "already sent" in again and Inspection.objects.count() == n


# ------------------------------------------------------------------ missing requirements, request approval
def test_missing_requirements_are_a_plain_list_with_what_to_do_and_who(w):
    w.rx.void_evidence(w.ops, evidence_code=w.evidence["battery_check"], reason="SYNTHETIC re-check")
    body = html(client_as(w.ops).get(visit_url(w)))
    block = re.search(r"data-missing-list=\"1\">(.*?)</ul>", body, re.S)
    assert block, "no list of missing requirements"
    text = block.group(1)
    assert "What to do:" in text and "Who can do it:" in text
    assert "the ops lead, the field operator or the engineer" in text.replace("\n", " ")
    assert "Request approval to leave" not in body  # checklist not complete: no request button
    assert "Approval to leave can be requested once every checklist item is clear" in body


def test_request_approval_is_shown_to_the_ops_lead_only_when_the_checklist_is_complete(w):
    assert "Request approval to leave" in html(client_as(w.ops).get(visit_url(w)))
    for role in roles_without("readiness.authorize_departure"):
        page = client_as(user_for(role)).get(visit_url(w))
        if page.status_code == 200:
            assert "Request approval to leave" not in html(page)
    field_page = html(client_as(w.field).get(visit_url(w)))
    assert "Waiting for the ops lead to approve." in field_page
    assert "Checklist complete - ask the ops lead to approve." in field_page


def test_request_approval_shows_the_decision_never_green_then_replay_is_refused(w):
    c = client_as(w.ops)
    tok = token_from(c, visit_url(w), f"/readiness/visits/{w.visit}/authorize/")
    r = c.post(visit_url(w) + "authorize/", {"_form_token": tok}, follow=True)
    body = html(r)
    auth = DepartureAuthorization.objects.get(visit__code=w.visit)
    toast = re.search(r'<div class="msg toast ([a-z]+)"[^>]*>(Approved to leave by[^<]*)</div>', body)
    assert toast, body
    assert toast.group(1) == "info"  # never the green "success" toast
    assert f"by {w.ops.username} at" in toast.group(2) and ", valid until " in toast.group(2)
    assert f"for visit {w.visit} with robot R-01." in toast.group(2)
    assert auth.expires_at > auth.issued_at
    again = html(c.post(visit_url(w) + "authorize/", {"_form_token": tok}, follow=True))
    assert "already sent" in again
    assert DepartureAuthorization.objects.filter(visit__code=w.visit).count() == 1


@pytest.mark.parametrize("role", roles_without("readiness.authorize_departure"))
def test_request_approval_denied_for_every_other_role_naming_the_ops_lead(w, role):
    user = user_for(role)
    c = client_as(user)
    tok = FormToken.objects.create(token=f"tk-{role}", user=user).token
    r = c.post(visit_url(w) + "authorize/", {"_form_token": tok}, follow=True)
    if r.status_code == 403:  # no readiness.read: refused before the page
        assert not DepartureAuthorization.objects.exists()
        return
    assert "Only the ops lead can do this." in html(r)
    assert not DepartureAuthorization.objects.exists()


# ------------------------------------------------------------------ evidence and recovery plan forms
def test_evidence_forms_success_denial_refusal_and_replay(w):
    url = visit_url(w) + "evidence/"
    c = client_as(w.field)
    page = html(c.get(visit_url(w)))
    for kind in ("pre_departure_inspection", "equipment_check", "battery_check"):
        assert f'data-evidence-form="{kind}"' in page
    before = ReadinessEvidence.objects.count()
    tok = token_from(c, visit_url(w), url)
    data = {"kind": "battery_check", "result": "pass", "soc_pct": "88", "_form_token": tok}
    assert "Evidence recorded (battery_check)" in html(c.post(url, data, follow=True))
    assert ReadinessEvidence.objects.count() == before + 1
    assert "already sent" in html(c.post(url, data, follow=True))
    assert ReadinessEvidence.objects.count() == before + 1
    # plain refusal for bad input
    bad = post(c, url, {"kind": "battery_check", "result": "pass", "soc_pct": "abc"}, page=visit_url(w))
    assert "whole number" in html(bad)
    # role denial names the roles
    founder = client_as(w.founder)
    assert f'action="{url}"' not in html(founder.get(visit_url(w)))  # the form is hidden from this role
    denied = founder.post(
        url,
        {
            "kind": "battery_check",
            "result": "pass",
            "soc_pct": "80",
            "_form_token": FormToken.objects.create(token="tk-founder", user=w.founder).token,
        },
        follow=True,
    )
    assert "Only the ops lead, the field operator or the engineer can do this." in html(denied)
    assert ReadinessEvidence.objects.count() == before + 1


def test_recovery_plan_form_success_denial_and_replay(w):
    url = visit_url(w) + "recovery-plan/"
    data = {
        "on_call": "P-4",
        "escalation": "P-5",
        "escalation_path": "SYNTHETIC path",
        "required_equipment": "a, b",
    }
    c = client_as(w.ops)
    n = RecoveryPlan.objects.count()
    tok = token_from(c, visit_url(w), url)
    assert "Recovery plan recorded" in html(c.post(url, {**data, "_form_token": tok}, follow=True))
    assert RecoveryPlan.objects.count() == n + 1
    assert "already sent" in html(c.post(url, {**data, "_form_token": tok}, follow=True))
    assert RecoveryPlan.objects.count() == n + 1
    field = client_as(w.field)
    FormToken.objects.create(token="tk-rp", user=w.field)
    r = field.post(url, {**data, "_form_token": "tk-rp"}, follow=True)
    assert "Only the ops lead or the engineering lead can do this." in html(r)
    assert RecoveryPlan.objects.count() == n + 1


def test_every_readiness_and_execution_form_carries_a_token(w):
    c = client_as(w.ops)
    for url in (visit_url(w), "/readiness/policies/"):
        page = html(c.get(url))
        for form in re.findall(r"<form[^>]*method=\"post\"[^>]*>(.*?)</form>", page, re.S):
            if "Sign out" in form:
                continue  # the header's sign-out button is not a record-changing form
            assert 'name="_form_token"' in form, (url, form[:80])


# ------------------------------------------------------------------ 01C Today coverage (EM ruling)
def _coverage(body: str) -> dict[str, int]:
    return {k: int(v) for k, v in re.findall(r'data-coverage="(\w+)"><b class="n">(\d+)</b>', body)}


def test_today_coverage_counts_every_visit_not_just_the_evaluated_ones(monkeypatch):
    from tests import operations_world as ow

    monkeypatch.setattr(today, "local_day", lambda at: DAY)
    set_setting("demo_mode", "true")
    set_setting("ops_readiness_lookahead_min", str(90 * 1440))
    for key, value in {
        "ops_custody_verify_grace_min": "30",
        "ops_return_grace_min": "60",
        "ops_inspection_grace_min": "120",
    }.items():
        set_setting(key, value)
    w = ow.ops_world("cv_", robots=6)
    robots = [f"R-0{i}" for i in range(1, 7)]
    k = 0
    unplanned: list[Any] = []
    for day in (DAY, DAY + timedelta(days=1)):
        for hour in (2, 6, 10, 14, 18, 22):  # one crew: a field block is about 3 hours
            if day == DAY and hour == 10:
                continue  # the world's own visit already sits there
            code = new_visit(w, d=day, h0=hour, h1=hour + 1)
            k += 1
            result = plan(w, code, robot=robots[k % 6], vehicle=("VAN-1", "VAN-2")[k % 2])
            if Visit.objects.get(code=code).status == "planned":
                w.p.confirm_visit(w.ops, visit_code=code)
            else:
                unplanned.append((code, result))
    total = Visit.objects.filter(status="confirmed").count()
    assert total > 10, (
        total,
        [(c, r.get("blockers")) for c, r in unplanned[:3]],
    )  # more than the evaluation cap
    body = html(client_as(w.ops).get("/ops/"))
    cov = _coverage(body)
    assert cov["all"] == total  # the complete count from the database
    assert cov["assessed"] == today.READINESS_CAP  # only the first 10 were evaluated
    assert cov["unassessed"] == total - today.READINESS_CAP
    assert cov["assessed"] + cov["unassessed"] == cov["all"]
    assert f"All visits: {total}" in body
    assert f"Readiness assessed: {today.READINESS_CAP}" in body
    assert f"Not yet assessed: {total - today.READINESS_CAP}" in body
    # the status tiles are never the fleet status: each says "of K assessed"
    for key in ("ready", "approved", "at_risk", "blocked"):
        m = re.search(rf'data-count="{key}"><b class="n">\d+</b><span>(.*?)</span>', body)
        assert m and "assessed" in m.group(1), key


# ------------------------------------------------------------------ planning uses the shared token
def _run_threads(fns: list[Callable[[], Any]]) -> list[Any]:
    barrier, out = threading.Barrier(len(fns)), [None] * len(fns)

    def go(i: int) -> None:
        try:
            barrier.wait(20)
            out[i] = fns[i]()
        except Exception as e:  # noqa: BLE001
            out[i] = e
        finally:
            connection.close()

    ts = [threading.Thread(target=go, args=(i,)) for i in range(len(fns))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return out


def test_planning_pages_use_the_shared_database_token(w):
    c = client_as(w.ops)
    v = new_visit(w, d=DAY + timedelta(days=3))
    page = html(c.get(f"/planning/visits/{v}/"))
    tok = TOKEN_RE.search(page)
    assert tok and FormToken.objects.filter(token=tok.group(1), user=w.ops, consumed_at__isnull=True).exists()
    assert 'name="form_token"' not in page
    assert "planning_form_tokens" not in c.session  # the old session store is gone


def test_same_planning_token_posted_twice_at_once_runs_the_service_once(w):
    v = new_visit(w, d=DAY + timedelta(days=2))
    plan(w, v, robot="R-02")
    clients = [client_as(w.ops), client_as(w.ops)]
    tok = FormToken.objects.create(token="plan-race", user=w.ops).token
    real = planning_services.confirm_visit
    calls: list[str] = []

    def counted(*a: Any, **kw: Any) -> Any:
        calls.append(kw.get("visit_code", ""))
        return real(*a, **kw)

    with mock.patch.object(planning_services, "confirm_visit", counted):
        res = _run_threads(
            [lambda c=c: c.post(f"/planning/visits/{v}/confirm/", {"_form_token": tok}) for c in clients]
        )
    assert all(getattr(r, "status_code", None) == 302 for r in res), res
    assert calls == [v]  # the service ran exactly once
    assert Visit.objects.get(code=v).status == "confirmed"
    shown = [html(c.get(f"/planning/visits/{v}/")) for c in clients]
    assert sum("already submitted" in b for b in shown) == 1
    assert FormToken.objects.get(token="plan-race").consumed_at is not None


# ------------------------------------------------------------------ admin landing
def test_admin_without_operations_read_lands_on_workspace_setup():
    admin = make_user("rf_admin", "admin")
    ops = make_user("rf_ops_landing", "ops_lead")
    both = make_user("rf_both", "admin", "ops_lead")
    nobody = make_user("rf_no_role")
    assert client_as(admin).get("/")["Location"] == "/settings/setup/"
    assert client_as(ops).get("/")["Location"] == "/ops/"
    assert client_as(both).get("/")["Location"] == "/ops/"  # operations.read wins
    assert client_as(nobody).get("/")["Location"] == "/fleet/"  # unchanged for everyone else
    assert client_as(admin).get("/settings/setup/").status_code == 200

"""Readiness operator UI (Engineer D): pages, forms, permissions, CSRF and labels. SYNTHETIC data only.

The readiness URLs are mounted here with a test-only urlconf (this module), so config/urls.py stays untouched.
Assertions rely on contract behaviour only (labels, command outcomes), never on the evaluator's internals.
"""

import json
import re
import secrets
from typing import Any

import pytest
from django.test import Client
from django.urls import include, path

from config.urls import urlpatterns as base_urlpatterns
from platform_core.models import FormToken
from platform_core.templatetags.ui import local_dt
from readiness import views
from readiness.models import (
    AuthorizationRevocation,
    DepartureAuthorization,
    DepartureEvent,
    ReadinessEvidence,
    ReadinessEvidenceVoid,
    ReadinessPolicy,
    ReadinessPolicyWithdrawal,
    RecoveryPlan,
)
from tests.conftest import make_user
from tests.fleet_world import set_setting
from tests.readiness_world import EQUIPMENT_ALL_PRESENT, build_readiness

urlpatterns = [*base_urlpatterns, path("readiness/", include("readiness.urls"))]
pytestmark = pytest.mark.urls(__name__)

BANNER = "Demo workspace"  # the one persistent header badge (01A); no per-page banners
SMALL_REQUIREMENTS = {
    "authorization_ttl_min": 60,
    "evidence": [{"kind": "battery_check", "max_age_min": 60}],
    "required_equipment": ["charger"],
    "recovery_plan_required": False,
}
READ_ROLES = ["ops", "field", "eng", "lead", "safety", "founder"]  # world users with readiness.read


@pytest.fixture
def w(transactional_db):
    world = build_readiness(prefix="rui_")
    # 01D release fix: the /readiness/ list assesses only visits inside the readiness look-ahead (the window
    # Today uses); the world's visit is weeks ahead, so the look-ahead covers it for the list's label tests.
    set_setting("ops_readiness_lookahead_min", str(60 * 24 * 400))
    return world


def _client(user: Any, csrf: bool = False) -> Client:
    c = Client(enforce_csrf_checks=csrf)
    c.force_login(user)
    return c


def _html(resp: Any) -> str:
    return resp.content.decode()


def _visit_url(w: Any) -> str:
    return f"/readiness/visits/{w.visit}/"


def _issue(user: Any) -> str:
    """A shared one-time form token for this user, as the page would have rendered it."""
    token = secrets.token_urlsafe(24)
    FormToken.objects.create(token=token, user=user)
    return token


def _post(user: Any, url: str, data: dict[str, Any] | None = None) -> str:
    resp = _client(user).post(url, {**(data or {}), "_form_token": _issue(user)}, follow=True)
    assert resp.status_code == 200
    return _html(resp)


def _authorize(w: Any) -> DepartureAuthorization:
    html = _post(w.ops, _visit_url(w) + "authorize/")
    assert "Authorization DA-" in html and "issued" in html, html
    return DepartureAuthorization.objects.filter(visit__code=w.visit).latest("issued_at")


def _assert_safe(html: str) -> None:
    assert "cleared to depart" not in html.lower()
    assert "green" not in html.lower()


# ------------------------------------------------------------------ access
def test_login_required_and_anonymous_posts_write_nothing(w):
    c = Client()
    for url in ("/readiness/", _visit_url(w), "/readiness/policies/"):
        resp = c.get(url)
        assert resp.status_code == 302 and resp["Location"].startswith("/login/")
    resp = c.post(_visit_url(w) + "authorize/")
    assert resp.status_code == 302 and resp["Location"].startswith("/login/")
    assert not DepartureAuthorization.objects.exists()


@pytest.mark.parametrize("role", READ_ROLES)
def test_pages_render_for_every_read_role(w, role):
    c = _client(getattr(w, role))
    for url in ("/readiness/", _visit_url(w), "/readiness/policies/"):
        resp = c.get(url)
        assert resp.status_code == 200, url
        html = _html(resp)
        assert BANNER in html
        _assert_safe(html)
    assert w.visit in _html(c.get("/readiness/"))


def test_users_without_readiness_read_get_403(w):
    for user in (make_user("rui_plain"), make_user("rui_admin", "admin")):
        c = _client(user)
        for url in ("/readiness/", _visit_url(w), "/readiness/policies/"):
            assert c.get(url).status_code == 403


def test_unknown_visit_is_404(w):
    assert _client(w.ops).get("/readiness/visits/V-NOPE/").status_code == 404


# ------------------------------------------------------------------ labels and banner
def test_index_shows_planning_and_readiness_labels(w):
    html = _html(_client(w.ops).get("/readiness/"))
    assert "Planning feasible" in html
    assert "Ready to request authorization" in html  # the contract world is fully evidenced
    assert "None issued" in html


def test_banner_absent_outside_demo_mode(w):
    set_setting("demo_mode", "false")
    for url in ("/readiness/", _visit_url(w), "/readiness/policies/"):
        html = _html(_client(w.ops).get(url))
        assert BANNER not in html
        _assert_safe(html)


def test_blockers_render_with_message_and_action(w):
    w.rx.void_evidence(w.ops, evidence_code=w.evidence["battery_check"], reason="SYNTHETIC re-check")
    html = _html(_client(w.ops).get(_visit_url(w)))
    assert "Readiness incomplete" in html
    assert re.search(r'<div class="blocker"><b>[^<]+</b>', html)
    assert "What to do: " in html
    _assert_safe(html)


@pytest.mark.parametrize(
    ("state", "reasons", "expected"),
    [
        ("expired", ["expired"], "Authorization expired"),
        ("invalidated", ["robot_version changed"], "Authorization invalidated (robot_version changed)"),
        ("revoked", ["SYNTHETIC"], "Authorization revoked"),
        ("used", [], "Departure recorded"),
    ],
)
def test_authorization_state_labels(w, monkeypatch, state, reasons, expected):
    _authorize(w)
    monkeypatch.setattr(views, "authorization_status", lambda a: {"state": state, "reasons": reasons})
    for url in ("/readiness/", _visit_url(w)):
        html = _html(_client(w.ops).get(url))
        assert expected in html
        _assert_safe(html)


# ------------------------------------------------------------------ CSRF
def test_csrf_enforced_on_posts(w):
    c = _client(w.ops, csrf=True)
    assert c.post(_visit_url(w) + "authorize/").status_code == 403
    assert c.post("/readiness/policies/create/", {"requirements": "{}"}).status_code == 403
    assert not DepartureAuthorization.objects.exists()
    c.get(_visit_url(w))  # sets the csrftoken cookie
    token = c.cookies["csrftoken"].value
    resp = c.post(_visit_url(w) + "authorize/", {"csrfmiddlewaretoken": token, "_form_token": _issue(w.ops)})
    assert resp.status_code == 302
    assert DepartureAuthorization.objects.filter(visit__code=w.visit).count() == 1


# ------------------------------------------------------------------ authorization, departure, revocation
def test_authorize_shows_issued_label_with_local_expiry(w):
    a = _authorize(w)
    html = _html(_client(w.ops).get(_visit_url(w)))
    label = f"Approved to leave, valid until {local_dt(a.expires_at)}"
    assert label in html and f"Approved to leave by <b>{w.ops.username}</b>" in html
    assert a.code in html and f"Authorization {a.code} issued" in html  # history
    assert label in _html(_client(w.ops).get("/readiness/"))
    assert not DepartureEvent.objects.exists()  # authorization is not departure


def test_authorize_is_idempotent_per_form_key(w):
    c = _client(w.ops)
    for _ in range(2):
        c.post(_visit_url(w) + "authorize/", {"idempotency_key": "rui-key-1", "_form_token": _issue(w.ops)})
    assert DepartureAuthorization.objects.filter(visit__code=w.visit).count() == 1


def test_authorize_refused_when_not_ready_shows_conflict(w):
    w.rx.void_evidence(w.ops, evidence_code=w.evidence["battery_check"], reason="SYNTHETIC re-check")
    html = _post(w.ops, _visit_url(w) + "authorize/")
    assert "Refused (conflict)" in html
    assert not DepartureAuthorization.objects.exists()


def test_authorize_permission_error_and_hidden_button(w):
    assert "Request approval to leave" in _html(_client(w.ops).get(_visit_url(w)))
    assert "Request approval to leave" not in _html(_client(w.field).get(_visit_url(w)))
    html = _post(w.field, _visit_url(w) + "authorize/")
    assert "Refused (permission denied)" in html
    assert not DepartureAuthorization.objects.exists()


def test_record_departure_is_separate_and_renders_history(w):
    a = _authorize(w)
    assert "Record departure" in _html(_client(w.field).get(_visit_url(w)))
    html = _post(
        w.field,
        f"/readiness/authorizations/{a.code}/depart/",
        {"idempotency_key": "rui-dep-1", "confirm": "1"},
    )
    assert f"Departure recorded on authorization {a.code}" in html
    assert DepartureEvent.objects.filter(authorization=a).count() == 1
    assert "Departure recorded" in html and f"Departure recorded on {a.code}" in html  # label + history
    _assert_safe(html)
    again = _post(
        w.field,
        f"/readiness/authorizations/{a.code}/depart/",
        {"idempotency_key": "rui-dep-2", "confirm": "1"},
    )
    assert "Refused (" in again
    assert DepartureEvent.objects.filter(authorization=a).count() == 1


def test_record_departure_permission_error(w):
    a = _authorize(w)
    assert "Record departure" not in _html(_client(w.eng).get(_visit_url(w)))
    html = _post(w.eng, f"/readiness/authorizations/{a.code}/depart/")
    assert "Refused (permission denied)" in html
    assert not DepartureEvent.objects.exists()


def test_revoke_then_departure_refused(w):
    a = _authorize(w)
    html = _post(w.lead, f"/readiness/authorizations/{a.code}/revoke/", {"reason": ""})
    assert "Refused (invalid input)" in html
    html = _post(w.lead, f"/readiness/authorizations/{a.code}/revoke/", {"reason": "SYNTHETIC weather"})
    assert f"Authorization {a.code} revoked" in html
    assert AuthorizationRevocation.objects.filter(authorization=a).exists()
    assert "Authorization revoked" in html and "SYNTHETIC weather" in html
    html = _post(w.ops, f"/readiness/authorizations/{a.code}/depart/")
    assert "Refused (" in html
    assert not DepartureEvent.objects.exists()


def test_revoke_permission_error(w):
    a = _authorize(w)
    html = _post(w.field, f"/readiness/authorizations/{a.code}/revoke/", {"reason": "SYNTHETIC"})
    assert "Refused (permission denied)" in html
    assert not AuthorizationRevocation.objects.exists()


def test_new_evidence_invalidates_issued_authorization(w):
    _authorize(w)
    _post(w.field, _visit_url(w) + "evidence/", {"kind": "battery_check", "result": "pass", "soc_pct": "90"})
    html = _html(_client(w.ops).get(_visit_url(w)))
    assert "Authorization invalidated (" in html


# ------------------------------------------------------------------ evidence
def test_record_battery_evidence_and_validation_errors(w):
    url = _visit_url(w) + "evidence/"
    before = ReadinessEvidence.objects.count()
    html = _post(w.field, url, {"kind": "battery_check", "result": "pass", "soc_pct": "91"})
    assert "(battery_check)" not in html  # 01D J4-3 plain wording
    assert "Battery check recorded" in html and "Battery 91%" in html
    assert ReadinessEvidence.objects.count() == before + 1
    html = _post(w.field, url, {"kind": "battery_check", "result": "pass", "soc_pct": "abc"})
    assert "Refused (invalid input)" in html and "whole number" in html
    html = _post(w.field, url, {"kind": "battery_check", "result": "pass", "soc_pct": "150"})
    assert "Refused (invalid input)" in html
    html = _post(w.field, url, {"kind": "nonsense", "result": "pass"})
    assert "Refused (invalid input)" in html
    assert ReadinessEvidence.objects.count() == before + 1


def test_record_equipment_and_inspection_evidence(w):
    url = _visit_url(w) + "evidence/"
    items = EQUIPMENT_ALL_PRESENT["items"]
    data: dict[str, Any] = {"kind": "equipment_check", "result": "pass", "item_names": list(items)}
    data.update({f"item_{k}": v for k, v in items.items()})
    data["extra_items"] = "spare_strap=present"
    html = _post(w.field, url, data)
    assert "Equipment check recorded" in html
    ev = ReadinessEvidence.objects.filter(kind="equipment_check").latest("recorded_at")
    assert ev.details == {"items": {**items, "spare_strap": "present"}}
    html = _post(w.field, url, {**data, "extra_items": "spare_strap"})
    assert "Refused (invalid input)" in html

    html = _post(
        w.ops, url, {"kind": "pre_departure_inspection", "result": "pass", "inspection_code": "INS-NOPE"}
    )
    assert "Refused (invalid input)" in html
    code = w.record_inspection(w.visit)
    html = _post(w.ops, url, {"kind": "pre_departure_inspection", "result": "pass", "inspection_code": code})
    assert "Pre-departure inspection recorded" in html and f"Inspection {code}" in html


def test_record_evidence_permission_error_and_hidden_form(w):
    assert 'action="/readiness/visits/' + w.visit + '/evidence/"' not in _html(
        _client(w.founder).get(_visit_url(w))
    )
    before = ReadinessEvidence.objects.count()
    html = _post(
        w.founder, _visit_url(w) + "evidence/", {"kind": "battery_check", "result": "pass", "soc_pct": "80"}
    )
    assert "Refused (permission denied)" in html
    assert ReadinessEvidence.objects.count() == before


def test_void_evidence_keeps_it_in_history(w):
    code = w.evidence["battery_check"]
    html = _post(w.field, _visit_url(w) + "void/", {"evidence": code, "reason": ""})
    assert "Refused (invalid input)" in html
    html = _post(w.founder, _visit_url(w) + "void/", {"evidence": code, "reason": "SYNTHETIC"})
    assert "Refused (permission denied)" in html
    html = _post(w.field, _visit_url(w) + "void/", {"evidence": code, "reason": "SYNTHETIC sensor glitch"})
    assert f"Evidence {code} voided" in html
    assert ReadinessEvidenceVoid.objects.filter(evidence__code=code).exists()
    assert code in html and "Voided" in html and "SYNTHETIC sensor glitch" in html
    html = _post(w.field, _visit_url(w) + "void/", {"evidence": "EV-9999", "reason": "x"})
    assert "Refused (invalid input)" in html


# ------------------------------------------------------------------ recovery plan
def test_recovery_plan_form(w):
    url = _visit_url(w) + "recovery-plan/"
    before = RecoveryPlan.objects.count()
    data = {
        "on_call": "P-4",
        "escalation": "P-5",
        "escalation_path": "SYNTHETIC path",
        "required_equipment": "a, b",
    }
    html = _post(w.ops, url, data)
    assert "Recovery plan recorded" in html and "SYNTHETIC path" in html
    assert RecoveryPlan.objects.latest("recorded_at").required_equipment == ["a", "b"]
    html = _post(w.ops, url, {**data, "escalation": "P-4"})
    assert "Refused (invalid input)" in html
    html = _post(w.field, url, data)
    assert "Refused (permission denied)" in html
    assert RecoveryPlan.objects.count() == before + 1


# ------------------------------------------------------------------ policies
def test_policy_list_status_and_no_green(w):
    html = _html(_client(w.ops).get("/readiness/policies/"))
    assert f"v{w.policy_version}" in html and "Current" in html
    # v2 01D ruling C: a synthetic_demo approval is shown precisely as a demo approval, never as plain "Approved"
    assert (
        "Demo approval" in html
        and 'class="badge synthetic"' in html
        and "Not approved for production" in html
    )
    assert "Create a policy" not in html  # ops lacks manage_policy
    _assert_safe(html)


def test_policy_create_approve_withdraw_flow(w):
    lead_html = _html(_client(w.lead).get("/readiness/policies/"))
    assert "Create a policy" in lead_html
    req = json.dumps(SMALL_REQUIREMENTS)
    html = _post(w.lead, "/readiness/policies/create/", {"requirements": req, "source": "synthetic_demo"})
    assert "created as a DRAFT" in html
    v = ReadinessPolicy.objects.latest("version").version
    assert re.search(rf"v{v}</b>.*?badge draft\">Draft", html, re.S)
    _assert_safe(html)

    html = _post(w.lead, f"/readiness/policies/{v}/approve/", {"slot": "engineering"})
    assert "Refused (" in html  # author cannot approve, and lead holds engineering slot role
    html = _post(w.lead2, f"/readiness/policies/{v}/approve/", {"slot": "engineering"})
    assert f"Policy v{v}: engineering approval recorded" in html
    html = _post(w.ops, f"/readiness/policies/{v}/approve/", {"slot": "safety"})
    assert "Refused (permission denied)" in html
    html = _post(w.safety, f"/readiness/policies/{v}/approve/", {"slot": "safety"})
    assert f"Policy v{v}: safety approval recorded" in html
    assert f"Current policy: <b>v{v}</b>" in html

    html = _post(w.ops, f"/readiness/policies/{v}/withdraw/", {"reason": "SYNTHETIC"})
    assert "Refused (permission denied)" in html
    html = _post(w.safety, f"/readiness/policies/{v}/withdraw/", {"reason": ""})
    assert "Refused (invalid input)" in html
    html = _post(w.safety, f"/readiness/policies/{v}/withdraw/", {"reason": "SYNTHETIC superseded"})
    assert f"Policy v{v} withdrawn" in html and "SYNTHETIC superseded" in html
    assert ReadinessPolicyWithdrawal.objects.filter(policy__version=v).exists()


def test_policy_create_validation_errors_keep_the_draft(w):
    before = ReadinessPolicy.objects.count()
    html = _post(
        w.lead, "/readiness/policies/create/", {"requirements": "{not json", "source": "synthetic_demo"}
    )
    assert "Refused (invalid input)" in html and "not valid JSON" in html
    assert "{not json" in html  # the textarea keeps what was typed
    html = _post(
        w.lead,
        "/readiness/policies/create/",
        {"requirements": '{"authorization_ttl_min": 5}', "source": "synthetic_demo"},
    )
    assert "Refused (invalid input)" in html and "requirements must have exactly the keys" in html
    html = _post(w.ops, "/readiness/policies/create/", {"requirements": "{}", "source": "synthetic_demo"})
    assert "Refused (invalid input)" in html  # validated before the command; ops never reaches it
    req = json.dumps(SMALL_REQUIREMENTS)
    html = _post(w.ops, "/readiness/policies/create/", {"requirements": req, "source": "synthetic_demo"})
    assert "Refused (permission denied)" in html
    assert ReadinessPolicy.objects.count() == before

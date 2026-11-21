"""Slice 7 Engineer C: pages and API. docs/07a_production_guardrails_contract.md sections 3.6, 4.6, 5.6, 6.4, 7, 8.

Permissions per role on every new page, POST endpoint and API; CSRF on every new form; labels (synthetic vs approved,
"would block in production", template states, unusable-template warning, signer warnings, deadline states); no
sign-off or clearance claims on the close-out; the missing / invalid time-zone fallback; demo banner rules; the
statement budget of the new pages. SYNTHETIC data only.
"""

import re
from typing import Any

import pytest
from django.db import connection
from django.test import Client

from accounts.roles import ALL_ROLES, PERMISSIONS
from fleet import services as fs
from fleet.models import DeploymentSetting, Inspection
from operations.snapshot import SET_SNAPSHOT
from tests import operations_world as ow
from tests.conftest import make_user
from tests.fleet_world import set_setting

pytestmark = pytest.mark.django_db(transaction=True)

SETTINGS = {
    "ops_custody_verify_grace_min": "30",
    "ops_return_grace_min": "60",
    "ops_inspection_grace_min": "120",
    "ops_readiness_lookahead_min": "1",
    "ops_periodic_due_lookahead_min": "1440",
    "ops_closeout_timezone": "America/Los_Angeles",
}
OPS_READ = sorted(PERMISSIONS["operations.read"])
FLEET_READ = sorted(PERMISSIONS["fleet.read"])
EXEC_READ = sorted(PERMISSIONS["execution.read"])
CLOSEOUT_FORBIDDEN = re.compile(r"\b(signed off|closed out|all clear|complete|OK|cleared)\b")
CLEARANCE = re.compile(r"\b(cleared|safe|ready to deploy|available to deploy)\b", re.IGNORECASE)
SETTINGS_KEY = 64942988919


def configure(**overrides: str | None) -> None:
    for key, value in {**SETTINGS, **overrides}.items():
        if value is None:
            DeploymentSetting.objects.filter(key=key).delete()
        else:
            set_setting(key, value)


def client(*roles: str, name: str | None = None) -> Client:
    c = Client()
    c.force_login(make_user(name or "c7_" + "_".join(roles or ("none",)), *roles))
    return c


def main(html: str) -> str:
    """The page body without the shared header (which has its own logout form)."""
    return html.split("<main>", 1)[-1]


@pytest.fixture
def w():
    world = ow.ops_world("u7_", robots=3)
    configure()
    return world


# POST endpoints -> permission (the view refuses other roles with 403 before any command runs)
def post_targets() -> list[tuple[str, str, dict[str, str]]]:
    return [
        (
            "/fleet/hold-policies/create/",
            "fleet.manage_hold_policy",
            {"rules": "{}", "source": "synthetic_demo"},
        ),
        ("/fleet/hold-policies/1/approve/", "fleet.approve_hold_policy", {"slot": "founder"}),
        ("/fleet/hold-policies/1/withdraw/", "fleet.withdraw_hold_policy", {"reason": ""}),
        ("/fleet/patterns/P-A/templates/create/", "fleet.manage_inspection_template", {"items": "[]"}),
        ("/fleet/patterns/P-A/templates/approve/", "fleet.approve_inspection_template", {"template": "1"}),
        ("/fleet/patterns/P-A/templates/withdraw/", "fleet.withdraw_inspection_template", {"template": "1"}),
        ("/fleet/patterns/P-A/periodic-rules/create/", "fleet.manage_periodic_rule", {"interval_min": ""}),
        ("/fleet/patterns/P-A/periodic-rules/approve/", "fleet.approve_periodic_rule", {"rule": "1"}),
        ("/fleet/patterns/P-A/periodic-rules/withdraw/", "fleet.withdraw_periodic_rule", {"rule": "1"}),
        ("/fleet/R-01/rebase/", "fleet.rebase_hold", {"hold": "HD-0001", "reason": ""}),
        ("/fleet/R-01/rebase/approve/", "fleet.rebase_hold", {"rebase": "1", "slot": "safety"}),
        (
            "/execution/escalation-deadlines/create/",
            "execution.manage_escalation_deadlines",
            {"deadlines": "{}"},
        ),
        (
            "/execution/escalation-deadlines/1/approve/",
            "execution.approve_escalation_deadlines",
            {"slot": "x"},
        ),
        ("/execution/escalation-deadlines/1/withdraw/", "execution.withdraw_escalation_deadlines", {}),
    ]


NEW_PAGES = [
    "/ops/closeout/",
    "/fleet/hold-policies/",
    "/fleet/patterns/P-A/",
    "/execution/escalation-deadlines/",
]


# ------------------------------------------------------------------ permissions
def test_new_read_pages_per_role(w):
    perms = {
        "/ops/closeout/": "operations.read",
        "/api/ops/closeout": "operations.read",
        "/api/ops/production-blockers": "operations.read",
        "/fleet/hold-policies/": "fleet.read",
        "/fleet/patterns/P-A/": "fleet.read",
        "/execution/escalation-deadlines/": "execution.read",
    }
    for role in [*ALL_ROLES, None]:
        c = client(*([role] if role else []), name=f"p7_{role or 'none'}")
        for url, perm in perms.items():
            r = c.get(url)
            expected = 200 if role in PERMISSIONS[perm] else 403
            assert r.status_code == expected, (role, url, r.status_code)
            if expected == 403:
                assert "<table" not in r.content.decode()
    assert Client().get("/ops/closeout/").status_code in (302, 403)
    assert Client().get("/fleet/hold-policies/").status_code == 302  # login required


def test_post_endpoints_refuse_roles_without_the_permission(w):
    targets = post_targets()
    for role in ALL_ROLES:
        c = client(role, name=f"q7_{role}")
        for url, perm, data in targets:
            r = c.post(url, data)
            if role in PERMISSIONS[perm]:
                assert r.status_code == 302, (role, url, r.status_code)  # handled by the command (or refused)
            else:
                assert r.status_code == 403, (role, url, r.status_code)
    # GET is not allowed on a form endpoint
    assert client("engineering_lead", name="g7").get("/fleet/hold-policies/create/").status_code == 405


def test_a_missing_engineer_b_command_records_nothing_and_says_so(w):
    if callable(getattr(fs, "withdraw_hold_policy", None)):
        pytest.skip("withdraw_hold_policy exists in this build (Engineer B merged)")
    c = client("engineering_lead", name="m7")
    r = c.post("/fleet/hold-policies/1/withdraw/", {"reason": "SYNTHETIC"}, follow=True)
    assert "does not provide withdraw_hold_policy yet; nothing was recorded" in r.content.decode()
    from fleet.models import HoldPolicyWithdrawal

    assert not HoldPolicyWithdrawal.objects.exists()


def test_forms_are_shown_only_to_roles_that_may_use_them(w):
    page = "/fleet/hold-policies/"
    assert (
        'action="/fleet/hold-policies/create/"' in client("engineer", name="f7e").get(page).content.decode()
    )
    ops = main(client("ops_lead", name="f7o").get(page).content.decode())
    assert "<form" not in ops
    founder = client("founder", name="f7f").get(page).content.decode()
    assert "/approve/" in founder and "/withdraw/" not in founder
    dl = "/execution/escalation-deadlines/"
    assert (
        'action="/execution/escalation-deadlines/create/"'
        in client("ops_lead", name="f7o2").get(dl).content.decode()
    )
    assert "<form" not in main(client("engineer", name="f7e2").get(dl).content.decode())
    pat = main(client("field_operator", name="f7fo").get("/fleet/patterns/P-A/").content.decode())
    assert '<form method="post"' not in pat


# ------------------------------------------------------------------ CSRF
def test_every_new_form_carries_a_csrf_token_and_posts_without_it_are_refused(w):
    hold = ow.place_hold(w, "robot", "R-01", "manual")
    assert hold
    pages = {
        "engineering_lead": ["/fleet/hold-policies/", "/fleet/patterns/P-A/", "/fleet/R-01/"],
        "safety_reviewer": ["/execution/escalation-deadlines/", "/fleet/R-01/"],
    }
    for role, urls in pages.items():
        c = client(role, name=f"x7_{role}")
        for url in urls:
            body = main(c.get(url).content.decode())
            forms = re.findall(r'<form method="post".*?</form>', body, re.S)
            assert forms, url
            for f in forms:
                assert "csrfmiddlewaretoken" in f, (url, f[:120])
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(make_user("x7_strict", *ALL_ROLES[1:]))
    for url, _, data in post_targets():
        assert strict.post(url, data).status_code == 403, url


# ------------------------------------------------------------------ labels
def test_overview_blocker_panel_demo_and_production(w):
    c = client("ops_lead", name="o7")
    # v2 01B: the blocker panel lives on Settings > Production readiness, the robot table on /ops/robots/
    body = c.get("/ops/production-readiness/").content.decode()
    assert "Would block in production" in body and 'data-mode="demo"' in body
    assert "would block in production" in body
    assert 'data-blocker="HOLD_POLICY_NOT_APPROVED"' in body
    assert 'data-blocker="PERIODIC_INTERVAL_NOT_APPROVED"' in body
    robots = c.get("/ops/robots/").content.decode()
    assert 'data-periodic="due"' in robots and "SYNTHETIC demo interval, not an engineering value" in robots
    assert "Demo workspace" in body
    api = c.get("/api/ops/production-blockers").json()
    assert api["production"] is False and api["heading"] == "Would block in production"
    codes = {i["code"] for i in api["items"]}
    assert {"HOLD_POLICY_NOT_APPROVED", "INSPECTION_TEMPLATE_NOT_APPROVED"} <= codes
    set_setting("demo_mode", "false")
    body = c.get("/ops/production-readiness/").content.decode()
    assert "Production blockers" in body and 'data-mode="production"' in body
    assert "would block in production" not in body and "Demo workspace" not in body
    inbox = c.get("/api/ops/exceptions").json()["items"]
    assert {e["code"] for e in inbox} >= {"HOLD_POLICY_NOT_APPROVED", "INSPECTION_TEMPLATE_NOT_APPROVED"}
    assert not CLEARANCE.search(main(body))


def test_fleet_pattern_robot_and_policy_labels(w):
    c = client("engineering_lead", name="l7")
    pat = c.get("/fleet/patterns/P-A/").content.decode()
    assert 'data-template="T1"' in pat and "synthetic, demo only" in pat
    assert "SYNTHETIC demo interval, not an engineering value" in pat
    assert 'data-status="approved"' in pat and "<b>active</b>" in pat
    pol = c.get("/fleet/hold-policies/").content.decode()
    assert 'data-status="synthetic draft"' in pol and "not usable in production" in pol
    assert "data-no-active" in pol and "would block every release in production" in pol
    hold = ow.place_hold(w, "robot", "R-01", "manual")
    robot = c.get("/fleet/R-01/").content.decode()
    assert "Release requirements fixed at placement under policy v1 (synthetic draft)" in robot
    assert "Would be unreleasable in production" in robot and f'data-rebases="{hold}"' in robot
    assert "two different people" in robot
    assert "Periodic inspection: due" in robot
    # production: unusable template warning, unreleasable hold wording, draft banner never claims a demo release
    set_setting("demo_mode", "false")
    robot = c.get("/fleet/R-01/").content.decode()
    assert (
        "This inspection will be recorded, but it cannot be used as passing pre-departure evidence." in robot
    )
    assert "Unreleasable in production: needs a re-basing decision to the active approved policy." in robot
    assert "Releases are allowed only because this is a demo deployment" not in robot
    assert "Periodic inspection: interval unknown" in robot  # synthetic rule refused in production
    lst = c.get("/fleet/").content.decode()
    assert "Periodic inspection: interval unknown" in lst
    api = c.get("/api/fleet/robots/R-01").json()
    assert api["periodic_inspection"]["state"] == "unknown_interval"


def test_signer_qualification_warnings_on_readiness_and_robot_pages(w):
    from planning.models import Qualification

    c = client("ops_lead", name="s7r")
    visit = c.get(f"/readiness/visits/{w.visit}/").content.decode()
    assert "data-inspector-note" in visit and "qualified readiness inspectors" in visit
    assert "data-signer-warning" not in visit  # the world's signers are covered
    # revoke a signer's qualification: the inspection stays recorded, the page warns
    insp = Inspection.objects.get(code=w.inspection)
    q = Qualification.objects.filter(person__user=insp.performer, kind="readiness_inspector").first()
    assert q is not None
    from planning import services as ps

    ps.revoke_qualification(w.ops, qualification_id=q.pk, reason="SYNTHETIC test")
    visit = c.get(f"/readiness/visits/{w.visit}/").content.decode()
    assert "data-signer-warning" in visit and "is not a qualified readiness inspector" in visit
    robot = c.get("/fleet/R-01/").content.decode()
    assert "data-signer-warning" in robot and "cannot back passing evidence" in robot


def test_incident_deadline_states_and_deadline_page(w):
    c = client("safety_reviewer", name="d7")
    code = ow.report(w, "R-01")
    page = c.get(f"/execution/incidents/{code}/").content.decode()
    assert 'data-deadlines="unconfigured"' in page and "Escalation deadlines not configured" in page
    dl = c.get("/execution/escalation-deadlines/").content.decode()
    assert "data-deadlines-unconfigured" in dl and "Nothing is sent, paged or notified" in dl
    from tests.test_s7_operations import make_deadline_set

    make_deadline_set(w)
    page = c.get(f"/execution/incidents/{code}/").content.decode()
    assert 'data-deadlines="configured"' in page and 'data-deadline-role="safety_reviewer"' in page
    assert "not yet escalated" in page and "SYNTHETIC demo, not a company value" in page
    dl = c.get("/execution/escalation-deadlines/").content.decode()
    assert "<b>Active</b>" in dl and "data-deadlines-unconfigured" not in dl
    set_setting("demo_mode", "false")
    dl = c.get("/execution/escalation-deadlines/").content.decode()
    assert "data-deadlines-unconfigured" in dl and "it must be owner approved" in dl


def test_deadline_set_lifecycle_through_the_page(w):
    """Walkthrough regression (2026-11-20): the deadline page called command names this build never had, so it
    could not record, approve or withdraw a set. Create (ops_lead), approve safety + founder, active, withdraw."""
    import json as _json
    import secrets

    from django.contrib.auth import get_user_model

    from execution.models import EscalationDeadlineSet, IncidentPolicy
    from platform_core.models import FormToken

    def tok(username: str) -> str:
        """The one-time form token the page renders for this person (01C double-submit protection)."""
        t = secrets.token_urlsafe(24)
        FormToken.objects.create(token=t, user=get_user_model().objects.get(username=username))
        return t

    policy = IncidentPolicy.objects.order_by("-version").first()
    levels = (policy.rules or {}).get("severity_scale", [])
    deadlines = {
        lvl: [{"to_role": "safety_reviewer", "within_min": 600}] for lvl in [*levels, "unclassified"]
    }
    ops = client("ops_lead", name="dl_ops")
    r = ops.post(
        "/execution/escalation-deadlines/create/",
        {
            "source": "synthetic_demo",
            "notes": "SYNTHETIC",
            "deadlines": _json.dumps(deadlines),
            "_form_token": tok("dl_ops"),
        },
        follow=True,
    )
    body = r.content.decode()
    assert "does not provide" not in body and "needs the safety and founder approvals" in body, main(body)[
        :600
    ]
    d = EscalationDeadlineSet.objects.order_by("-version_no").first()
    assert d is not None and d.deadlines == deadlines and d.source == "synthetic_demo"
    approvers = {role: client(role, name=f"dl_{role}") for role in ("safety_reviewer", "founder")}
    for role, slot in (("safety_reviewer", "safety"), ("founder", "founder")):
        r = approvers[role].post(
            f"/execution/escalation-deadlines/{d.pk}/approve/",
            {"slot": slot, "_form_token": tok(f"dl_{role}")},
            follow=True,
        )
        assert "Deadline set approval recorded" in r.content.decode(), (role, main(r.content.decode())[:600])
    page = ops.get("/execution/escalation-deadlines/").content.decode()
    assert "<b>Active</b>" in page
    r = approvers["safety_reviewer"].post(
        f"/execution/escalation-deadlines/{d.pk}/withdraw/",
        {"reason": "SYNTHETIC", "_form_token": tok("dl_safety_reviewer")},
        follow=True,
    )
    assert "Deadline set withdrawn" in r.content.decode(), main(r.content.decode())[:600]
    from execution.models import EscalationDeadlineSetWithdrawal

    assert EscalationDeadlineSetWithdrawal.objects.filter(deadline_set=d).exists()


# ------------------------------------------------------------------ close-out
def test_closeout_page_and_api_dated(w):
    run = ow.depart_new(w, "R-02")
    c = client("field_operator", name="c7f")
    r = c.get("/ops/closeout/")
    body = r.content.decode()
    assert r.status_code == 200 and SET_SNAPSHOT  # built in the snapshot (decorator)
    assert "This page is not a sign-off; the platform has no close-out record." in body
    assert 'data-counts="1"' in body and f'data-run="{run.code}"' in body
    assert "America/Los_Angeles" in body
    assert "Demo workspace" in body
    assert not CLOSEOUT_FORBIDDEN.search(main(body)), CLOSEOUT_FORBIDDEN.search(main(body))
    assert not CLEARANCE.search(main(body))
    api = c.get("/api/ops/closeout").json()
    assert api["dated"] is True and api["timezone"] == "America/Los_Angeles"
    assert [x["run"] for x in api["departed_open"]] == [run.code]
    assert api["counts"] == {"departed": 1, "departed_closed": 0}
    assert "not a sign-off" in api["not_a_sign_off"] and "carried_over" in api
    # a future date is a 400 on page and API (after the permission check); a malformed one too
    assert c.get("/ops/closeout/", {"date": "2999-01-01"}).status_code == 400
    assert c.get("/api/ops/closeout", {"date": "2999-01-01"}).status_code == 400
    assert c.get("/api/ops/closeout", {"date": "nope"}).status_code == 400
    assert client(name="c7none").get("/api/ops/closeout", {"date": "nope"}).status_code == 403
    set_setting("demo_mode", "false")
    assert "Demo workspace" not in c.get("/ops/closeout/").content.decode()


@pytest.mark.parametrize("value", [None, "Atlantis/Lost"])
def test_closeout_missing_timezone_fallback(w, value):
    configure(ops_closeout_timezone=value)
    run = ow.depart_new(w, "R-02")
    c = client("ops_lead", name="c7z")
    body = c.get("/ops/closeout/", {"date": "2001-01-01"}).content.decode()
    assert "Date-unclassified: the close-out time zone is not configured" in body
    assert "The requested date was ignored." in body
    assert "carried over" not in main(body).lower()
    assert "data-counts" not in body and "Departed on" not in body
    assert f'data-run="{run.code}"' in body
    assert not CLOSEOUT_FORBIDDEN.search(main(body))
    api = c.get("/api/ops/closeout", {"date": "2999-01-01"}).json()  # ignored, never a 400
    assert api["dated"] is False and api["date_ignored"] is True
    assert "counts" not in api and "carried_over" not in api and "departed_open" not in api
    assert [x["run"] for x in api["open_runs"]] == [run.code]
    assert api["open_runs"][0]["carried_over"] is False and api["open_runs"][0]["local_day"] is None
    inbox = c.get("/api/ops/exceptions").json()["items"]
    assert [
        e
        for e in inbox
        if e["code"] == "THRESHOLD_NOT_CONFIGURED" and e["entity_ref"] == "ops_closeout_timezone"
    ]


# ------------------------------------------------------------------ budgets
def _count(c: Client, url: str) -> int:
    seen: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        r = c.get(url)
    assert r.status_code == 200, (url, r.status_code)
    assert SET_SNAPSHOT in seen
    assert not [
        s
        for s in seen
        if re.search(
            r"\b(INSERT|UPDATE|DELETE|FOR UPDATE|FOR SHARE|nextval|set_config|pg_advisory)\b", s, re.I
        )
    ]
    return len(seen)


def test_new_ops_endpoints_statement_budget_constant_in_fleet_size(w):
    c = client("ops_lead", name="b7")
    urls = [
        "/ops/robots/",
        "/ops/production-readiness/",
        "/api/ops/overview",
        "/ops/closeout/",
        "/api/ops/closeout",
        "/api/ops/production-blockers",
    ]
    small = {u: _count(c, u) for u in urls}
    run = ow.depart_new(w, "R-01")
    ow.advance(w, run, "at_site")
    ow.depart_new(w, "R-02")
    ow.report(w, "R-03")
    big = {u: _count(c, u) for u in urls}
    for u, n in small.items():
        assert n <= 12, (u, n)
    assert small == big, (small, big)
